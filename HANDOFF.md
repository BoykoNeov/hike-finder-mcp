# HANDOFF — hike-finder-mcp

Read this first. It tells you what the project is, what's already proven, what's
untested, and the exact next steps — so you can continue in Claude Code without
reverse-engineering intent.

## Goal in one sentence

Replace "search the web and trust whatever gain number a trail site printed"
with "query OpenStreetMap for marked routes and compute gain/distance ourselves,"
exposed as an MCP tool `find_hikes(bbox, gain range, distance range, circular?,
car_access?, chairlift_access?)`.

## The user's context (don't lose this)

- They plan hikes with **mapy.cz** and specifically want **OSM-based** data, not
  AllTrails' proprietary data. That's why we go to Overpass for route relations,
  not a trail-site API. The KČT trail markings they rely on live in OSM tags.
- They explicitly asked for **both** elevation backends (API *and* local DEM),
  selectable — already implemented as `mode = api | local | auto`.
- AllTrails / Felt / TomTom MCP connectors were offered and **declined** in favour
  of building this. Don't reach for them.

## Architecture

The pipeline is deliberately **two-pass**: everything cheap (geometry + access)
runs first and filters the candidate set; the expensive elevation lookup runs
*only on the survivors*. That's what keeps the elevation API from being hammered
(was: elevation for every route, then filter → minutes and rate-limit bans).

**Three frontends, one engine.** As of 2026-06-23 the tool runs standalone — no
LLM required. All three frontends build the same `Criteria` and call
`search.search_hikes`, then render via `format.format_hike` / `hike_to_dict`, so
results are identical:

- `cli.py` → `hike-finder` (primary console script; argparse). **No LLM/MCP.**
- `web.py` → `hike-finder-web` (stdlib `http.server` + Leaflet map; pan to pick
  the bbox). **No LLM/MCP, no web framework.**
- `server.py` → `hike-finder-mcp` (MCP over stdio, for LLM clients). `mcp` is now
  an **optional** extra (`pip install -e ".[mcp]"`); the base install omits it.

```
frontends (pick one; cli/web need no LLM):
  cli.py  ─┐
  web.py  ─┼─→ search.search_hikes(bbox, criteria, cfg)   # shared orchestration
  server.py┘     (MCP tool find_hikes; needs the optional `mcp` extra)
       ├─ overpass.fetch_area(bbox)          # routes + parking + lifts  [NETWORK]
       │    └─ overpass.parse_area(elements) # split mixed response      [PURE, TESTED]
       ├─ elevation.get_provider(mode)       # api | local | auto        [NETWORK/DISK]
       └─ filters.find_hikes(area, elevation, criteria, bbox)
            ├─ CHEAP pass  → filters.measure_geometry(route, parking, lifts)
            │    ├─ geometry.stitch_ways          # join member ways  [PURE, TESTED]
            │    ├─ geometry.polyline_length_m     # distance          [PURE, TESTED]
            │    └─ access.is_circular / car_accessible / chairlift_access [PURE, TESTED]
            │  → apply over-length guard + distance/shape/access filters
            └─ EXPENSIVE pass (survivors only) → filters.add_elevation(hike, line)
                 ├─ geometry.resample_by_distance  # even spacing      [PURE, TESTED]
                 ├─ elevation.lookup(points)       # api/local/auto    [NETWORK/DISK]
                 └─ elevation.cumulative_gain_loss # smoothing+thresh  [PURE, TESTED]
               → apply gain filter, sort
  → results rendered by format.format_hike / format.hike_to_dict (shared)
```

### The three filters added on top of gain/distance

- **`circular`** (loop vs point-to-point) — `access.is_circular`. Order:
  the OSM `roundtrip` tag is authoritative; else the member ways are tested for
  closure by *circuit rank* — `geometry.route_cycle_count`, the **full vertex
  graph's** `E - V + C` (>0 ⇒ a loop exists; stitch-order independent, counts a
  *lollipop* loop-plus-stem, and — because nodes are exact shared vertices —
  detects T-junction closures while NOT inventing cycles from clustered
  endpoints); else the stitched line returning within `HIKE_LOOP_TOLERANCE` of
  its start (catches a loop left open only by a digitization gap). High
  confidence — validated live (see below).
- **`car_access`** — `access.car_accessible`. A mapped `amenity=parking` within
  `HIKE_CAR_RADIUS` of a trail *endpoint*. Parking-only by design (roads are dense
  and tag-fragile; revisit if recall complaints surface). Best-effort confidence.
- **`chairlift_access`** — `access.chairlift_access`. A ride-up aerialway
  (`chair_lift`/`gondola`/`cable_car`/`mixed_lift` — drag/T-bar excluded) station
  within `HIKE_LIFT_RADIUS` of an endpoint; the actual lift type is reported.
  Best-effort confidence.

All three are tri-state in `Criteria` (None = don't care, True = require,
False = exclude). The **over-length guard** (`HIKE_MAX_ROUTE_FACTOR`) drops
routes longer than N× the bbox diagonal — a through-route (national trail) that
merely crosses the area returns its *full* geometry, which would otherwise
report a 200 km "hike" and test parking/lifts at endpoints in another region.

## What is DONE and PROVEN (unit-tested, runs offline)

- `geometry.py` — haversine distance, polyline length, **`total_way_length_m`
  (route distance = sum of member-way lengths, drops nothing)**, way stitching
  (with endpoint matching + flipping), distance-based resampling.
  `tests/test_geometry.py`.
- `elevation/gain.py` — moving-average smoothing + hysteresis-threshold gain/loss.
  `tests/test_gain.py`. Verified: rejects pure noise, captures gradual climbs,
  symmetric up/down, and does NOT overcount under heavy sawtooth noise.
- `access.py` — circular detection via circuit rank over the full vertex graph
  (closed loop / open path / lollipop / figure-8 / single ring way / T-junction
  closure, order-independent — see `geometry.route_cycle_count`), car & chairlift
  proximity (just-inside vs just-outside a radius), and tri-state `Criteria`
  acceptance. `tests/test_access.py`. Plus a real-OSM closure regression on the
  live "Medvěd*" relations (`tests/test_closure_live.py` + fixture).
- `overpass.parse_area` — the mixed-response parser (routes vs parking-node vs
  parking-area-via-`center` vs aerialway, with drag-lifts excluded). Tested on a
  hand-built element list, so the failure-prone parsing is covered *offline* even
  though the live HTTP call is not. `tests/test_access.py`.
- Full pipeline smoke-tested end-to-end with a stub elevation provider:
  two-pass ordering, the new filters, and the over-length guard all verified
  (loop flagged circular + lift access; point-to-point with car access; a 70 km
  through-route correctly dropped).

- `cli.py` / `format.py` — argument parsing, the args→`Criteria` mapping
  (including the tri-state booleans), and the shared one-line / dict rendering.
  `tests/test_cli.py`. The CLI's *live* path is identical to the server's (both
  call `search_hikes`), so validating one validates the other.

- `elevation/api.py` — the request body PER endpoint (OpenTopoData pipe-string
  vs Open-Elevation dict-list), shared response parsing, nodata forward-fill,
  the cross-request throttle, and transient-error retry/backoff (retries on
  429/5xx/network, honours `Retry-After`, does NOT retry deterministic 4xx, gives
  up after `max_retries`, and gives up rather than stall on a `Retry-After` above
  `max_backoff_s`). `tests/test_api.py` (mocks `requests.post`, so offline). These
  tests exist *because* the body-format bug below shipped untested.

- `elevation/quota.py` — the **persistent daily-request counter**. A file-backed
  per-UTC-day tally (keyed by API host, in a per-user cache dir) so cumulative
  searches can't blow the API's daily cap even though each CLI run is a fresh
  process. Check-before-send → at the limit, `_lookup_batch` raises (route
  degrades to `n/a`) without a network call; count is incremented after each
  response. A *process-wide* lock + atomic file replace serialise the
  read-modify-write across the threaded web server's concurrent providers (a
  per-instance lock would NOT — each search builds a fresh provider). `tests/
  test_quota.py`: UTC rollover, at-limit enforcement, persistence across separate
  instances, per-host separation, the `limit<=0` disable switch, a 4-thread
  concurrency test, and — the linchpin — an **end-to-end** test that drives a real
  `FallbackElevationProvider([api])` (what `auto` builds with no DEM) through
  `find_hikes` with the counter pre-exhausted, asserting the search *completes*
  with routes at `gain_m=None` (not abort) and never touches the network.
  `tests/conftest.py` isolates the counter to a tmp dir so the suite never touches
  the real cache. Tunable via `HIKE_API_DAILY_LIMIT` (0 = off) /
  `HIKE_API_STATE_DIR`; surfaced by the CLI (stderr line) and the web UI
  (`/api/quota`).

- `geometry.resample_by_distance` — now has a multi-segment regression
  (`tests/test_geometry.py`). The old single-segment test missed a carry bug
  that collapsed finely-vertexed real OSM lines to 2 points (see bug #3 below).

Run it: `pytest` → 72 passing.

## What is now VALIDATED LIVE (run against real OSM, 2026-06-23)

- `overpass.fetch_area` + `parse_area` — exercised against a real bbox (Špindlerův
  Mlýn, `50.72,15.58,50.74,15.62`). Returned 15 routes / 31 parking / 5 lifts;
  `parse_area`'s assumed response shape matched live data exactly. The over-length
  guard dropped 3 through-routes; circular/car/chairlift flags came out sane
  (e.g. "Špindlmanova mise" → car + chair_lift).
- **Bug found & fixed during this validation:** overpass-api.de sits behind
  Apache/mod_security and rejects the default python-requests User-Agent with
  **406 Not Acceptable** *before parsing the query*. `fetch_area` now sends a
  descriptive `User-Agent` (`overpass.USER_AGENT`) and retries the transient
  504/429/502/503 that the public instance throws under load. Without the UA,
  every request fails — this also affected the original `fetch_routes`.

- `elevation/api.py` + `geometry.resample_by_distance` (the **gain pipeline**) —
  validated against OpenTopoData `srtm30m`. CLI run on the Špindlerův Mlýn bbox
  now returns a computed gain/loss for **every** one of the 12 returned routes
  (0 nulls, 0 stubs). Accuracy cross-checked two ways: a returned climb
  *[Z] Richtrovy Boudy → Špindlerův mlýn* = **+678 / −251 m**, and — decisively —
  the detected loop *Špindlerův mlýn – okruh* = **+34 / −34 m**, i.e. gain ≈ loss
  exactly as a closed loop must (a closed line returns to its start elevation).
  That invariant exercises sampling + alignment + the gain math end-to-end.
  **Three bugs found & fixed here:**
  1. **Wrong request body.** The provider POSTed Open-Elevation's
     `[{latitude, longitude}]` shape to OpenTopoData, which 400s every call
     (`INVALID_REQUEST`) → caught as `ElevationError` → gain silently `n/a`.
     `_encode_locations` now picks the dialect from the endpoint host
     (OpenTopoData wants one `"lat,lon|lat,lon"` string).
  2. **429 across routes.** The old code slept only *between batches within one
     route*; back-to-back routes breached OpenTopoData's ~1 req/s and got
     **429** → `n/a` for the later routes. One provider instance is reused per
     search, so it now throttles *all* requests via `_throttle` (≥1.1 s apart).
     Also: nodata elevations are forward-filled (fail only if every point is
     nodata), so a stray `null` no longer escapes as an uncaught `TypeError`.
  3. **Resampling collapsed real tracks to 2 points** (the accuracy killer).
     `resample_by_distance`'s carry term accumulated without ever emitting a
     sample when segments were shorter than the interval — and real OSM vertices
     sit ~5–10 m apart, well under the 25 m interval. So multi-km lines sampled
     to `[start, end]`, giving endpoint-only "gain" (the loop read 0/0). With the
     fixed carry logic, sample counts now match `length / interval` (ratios
     1.00–1.04) and gains are trustworthy. This bug was invisible because the
     only resample test used a single long segment.

- **Loop closure** (`geometry.route_cycle_count` / `access.is_circular`) —
  validated against the live "Medvěd*" `route=hiking/foot` relations in CZ (13
  relations, one Overpass call, full member geometry, saved as
  `tests/fixtures/medved_relations.json`). **This live test FALSIFIED the first
  closure fix and drove a rewrite — the headline finding of the day:**
  - The shipped fix (commit 702c0f5) built the circuit-rank graph from clustered
    way *endpoints* (30 m). On dense real relations that over-merges piled-up
    endpoints and **invents cycles**, flipping six linear/branched routes to
    `circular=true` — including "Medvědí okruh" itself. Ground truth from the
    exact-coordinate **vertex** graph (which captures T-junctions, since
    connected OSM ways share the identical node): all six have circuit rank 0 —
    they are genuinely **not loops**. "Medvědí okruh" (rel 6285306) is a branched
    linear route, 4 termini, ends ~2.4 km apart. The reported "bug" (it read
    non-circular) was the *old* code being right; the endpoint-cluster fix had
    turned it into a false positive.
  - **Fix:** `route_cycle_count` now builds the graph from the **full vertex
    graph** (every vertex welded by coordinate, `weld_m≈1 m`), not way endpoints.
    Exact vertex sharing detects T-junction closures *and* never invents cycles.
    Re-validated: the 5 genuine okruhs read circular (rank ≥ 1, *structurally* —
    no longer propped by the stitch-collapse line fallback), all 6
    linear/branched routes read non-circular, and **no relation's circular
    verdict regressed vs the pre-702c0f5 code**. Lollipop/T-junction/figure-8
    covered by `tests/test_geometry.py`; the real split is pinned in
    `tests/test_closure_live.py`.

- **Distance under-count from `stitch_ways` dropping members** — fixed and
  validated live on the same Špindlerův Mlýn bbox via a per-route stitched-vs-
  summed diff. Distance now sums member-way lengths (`total_way_length_m`); 13/15
  routes unchanged, two fragmented relations recovered real geometry, no
  over-count, guard verdict shifted on exactly one (correctly-dropped) route. Full
  detail in Next-steps step 3; pinned offline by `tests/test_geometry.py`
  (dropped-member recovery + summed==stitched invariant + order-independence).

## What is WRITTEN but UNVALIDATED (needs a networked machine)

Logic is complete; you still need to exercise these live (the Overpass layer and
the API elevation backend above are now done):

1. `elevation/local_dem.py` — needs `rasterio`. Confirm tile merge + `rowcol`
   sampling against a known summit elevation. Watch nodata handling.
2. `server.py` — confirm it speaks MCP over stdio with your `mcp` SDK version
   (the decorator API has shifted across versions; adjust imports if needed).
   Now needs the optional `mcp` extra (`pip install -e ".[mcp]"`).

(`web.py` is now validated live too — `/` serves the page, `/api/hikes` reuses
the validated `search_hikes` path and returns correct UTF-8 JSON.)

## Next steps, in priority order

1. **Validate Overpass live** — DONE (2026-06-23, see "VALIDATED LIVE" above).
   Špindlerův Mlýn bbox returned 15 routes / 31 parking / 5 lifts; guard + filters
   sane. The User-Agent bug was found and fixed here.
2. **Validate the `api` elevation backend** — DONE (2026-06-23, see "VALIDATED
   LIVE"). Every route now gets a computed gain/loss; gain tracks the profile.
   Defaults (threshold 10 m, interval 25 m) left as-is — *not* tuned to one
   route (that would overfit); revisit once several known-profile trails exist.
3. **Loop-closure detection** — **DONE and VALIDATED LIVE (2026-06-23).** Two
   attempts; the live test caught the first one being wrong:
   - **First attempt (commit 702c0f5), now superseded:** replaced the old
     every-endpoint-even-degree test with circuit rank `E - V + C` over the
     *endpoint* graph (clustered way endpoints, 30 m). Passed clean synthetic
     unit tests. **Live testing falsified it:** clustering endpoints at 30 m
     over-merges in dense real relations and invents cycles, flipping six
     linear/branched "Medvěd*" routes — *including "Medvědí okruh" itself* — to a
     false `circular=true`. The reported symptom ("Medvědí okruh reads
     non-circular") turned out to be the *old* code being right: that relation is
     genuinely not a loop (vertex-graph rank 0, 4 termini, ends 2.4 km apart).
   - **Second attempt (current), validated:** `route_cycle_count` now builds the
     graph from the **full vertex graph** — every vertex welded by coordinate,
     not just way endpoints. Because connected OSM ways share the identical node,
     exact vertex sharing detects T-junction closures (the old deferred
     limitation, now fixed) *and* never invents cycles. Re-run on the live
     fixture: 5 genuine okruhs read circular (rank ≥ 1, structurally), all 6
     linear/branched read non-circular, no verdict regressed vs the pre-702c0f5
     code. Pinned by `tests/test_closure_live.py` (real fixture) +
     `tests/test_geometry.py` (lollipop / T-junction / figure-8 / no-fuzzy-weld).
   - **Distance under-count — DONE and VALIDATED LIVE (2026-06-23).** `stitch_ways`
     greedily *drops* members it can't chain (branched/disconnected relations), so
     the stitched-line length under-counts. `measure_geometry` now takes distance
     from `geometry.total_way_length_m(route["ways"])` — the sum of every member
     way's length, order-independent, dropping nothing. Live-validated on the
     Špindlerův Mlýn bbox with a **per-route stitched-vs-summed diff** (not a bare
     before/after, which can't tell honest recovery from silent over-count): 13/15
     routes unchanged within float noise (clean linear routes — the invariant
     `summed≈stitched` held); two fragmented relations recovered real geometry
     (`4207` 7.54→18.28 km, 36/70 members had been dropped; `Medvědí okruh`
     3.37→7.98 km, 19/31 dropped). Both verified as genuine recovery, NOT
     over-count: every member is role-empty (no `forward`/`backward` variants —
     the realistic double-count mode), and the recovered length far exceeds the
     kept chain so it can't be duplication of it. (An endpoint-pair check also
     found 0 repeats, but that misses *partial* overlaps, so role-empty is the
     load-bearing signal.) Over-length guard stayed healthy — exactly one verdict
     change: `4207` newly dropped. Confirmed correct by an **independent
     geographic check** (not the guard's own length metric, which would be
     circular): 93% of `4207`'s vertices lie *outside* the query bbox and its
     geometry protrudes 5.8 km S / 6.9 km E (own span 11.4 km) — a genuine
     through-route. Note this means the *old* stitched 7.54 km had wrongly passed
     it as a local hike; the fix improves recall correctness. So
     `max_route_factor=4.0` was left as-is (no retune, no bbox-clip needed — no
     genuine in-bbox route sits near the boundary).
   - **Endpoint/`start`-pick half — DONE and VALIDATED LIVE 2026-06-24.** `start`
     and the car/lift access endpoints now incorporate `geometry.route_termini` —
     the **degree-1 vertices of the same full vertex graph** that drives closure
     (`_vertex_graph` is now the single shared builder, so the two can't drift).
     A branched/disconnected relation whose stitch drops members therefore tests
     access at the route's *genuine* open ends, including ends on dropped members.
     Access uses the **UNION** of the termini and the stitched line's two ends:
     `endpoints = list(dict.fromkeys(termini + route_endpoints(line)))` — *adding*
     the termini, not replacing the stitched ends, because replacing would move a
     lollipop's access point off its ring to the stem tip and could drop a parking
     mapped on the loop (a false negative). Union is recall-monotonic — it can only
     add access hits, never remove one. A pure loop / fwd+back-duplicated route has
     no degree-1 vertex, so the union is just the stitched ends (today's behaviour).
     `_route_start` keeps `line[0]` when it is already a terminus (zero churn on
     clean routes) else moves to the smallest terminus by coordinate. Validated
     live on the "Medvěd*" fixture: the branched *Medvědí okruh* (rel 6285306,
     only 42% stitch coverage) recovers **4 genuine termini ~2.46 km apart**
     (the old code tested 2 ends, only 1 a real terminus); the real KČT okruhs are
     lollipops whose stem tip is now tested *in addition to* the ring point; every
     clean linear route is unchanged. Pinned by `tests/test_closure_live.py`
     termini ground truth plus a lollipop ring-parking guard. Distance no longer
     depends on the stitch; closure never did. **Nothing
     in this relation's geometry pipeline still rides on the greedy stitch except
     the `is_circular` gap fallback and the loop `start` fallback (both benign).**
4. ~~Add API retry/backoff on transient 5xx / daily-cap 429.~~ **DONE.**
   `_lookup_batch` now retries 429/5xx/network up to `max_retries` (default 3)
   with exponential backoff (`backoff_base_s` × 2^attempt), honouring a
   `Retry-After` header; deterministic 4xx are not retried. Any single wait is
   capped at `max_backoff_s` (default 30 s): a `Retry-After` above that — the
   shape a **daily-quota** 429 takes (seconds-until-reset, often an hour) — makes
   us give up immediately and degrade the route to `n/a` rather than freeze the
   search (and, in the web UI, freeze that HTTP request). Tunable via
   `HIKE_API_MIN_INTERVAL` / `HIKE_API_MAX_RETRIES` / `HIKE_API_BACKOFF` /
   `HIKE_API_MAX_BACKOFF`.
5. ~~Track the daily request cap across searches and show it.~~ **DONE.**
   `elevation/quota.py` is a persistent, cross-process per-UTC-day counter (see
   the DONE bullet above). The per-second *and* daily limits are now both managed:
   at the daily cap we degrade routes to `n/a` instead of getting the IP banned,
   and the count is shown (CLI stderr line; web `/api/quota`, appended to the
   status line). `HIKE_API_DAILY_LIMIT=0` disables it. Caveats: the UTC-midnight
   reset is *assumed* (low-stakes — misalignment only degrades early/late); the
   enforcement path is unit-tested (mocked) but, like the retry path, not yet
   live-exercised against a real daily-cap rejection.
6. **Wire MCP end-to-end** and call `find_hikes` from Claude Code.
7. **Then** add the local DEM path and the polish items below.

## Known limitations / TODOs (design notes, not bugs)

- **Gain threshold vs noise (important):** the threshold must exceed the
  *peak-to-peak* noise amplitude, not half of it. A unit test caught this — ±5 m
  jitter is 10 m peak-to-peak and a 10 m threshold sits exactly on the boundary.
  Use threshold > peak-to-peak, and lean on smoothing. Tune per elevation source
  (API data is pre-smoothed; raw SRTM is noisier → higher threshold).
- **Way stitching is greedy** with a 30 m endpoint tolerance. Fine for simple
  linear routes; multi-branch/disconnected relations stitch oddly and silently
  **drop** any member `stitch_ways` can't chain to the current chain's ends.
  - *Distance:* **fixed.** No longer read from the stitched line —
    `measure_geometry` sums member-way lengths via `geometry.total_way_length_m`,
    which drops nothing (live-validated; see Next-steps step 3). Trade-off: a
    relation that maps a stretch as both `forward` and `backward` variants would
    now double-count, but the public KČT relations checked live carry no such
    members (all role-empty), so the honest member-sum strictly beats the
    arbitrary greedy subset. Closure never depended on the stitch (vertex graph).
  - *Endpoints / `start`:* **fixed.** `measure_geometry` now folds in
    `geometry.route_termini`, the **degree-1 vertices of the vertex graph**
    (`_vertex_graph` is the single builder shared with `route_cycle_count`, so
    closure and termini can't drift). Stitch-order independent; captures ends on
    members the stitch drops. Access tests the **union** of the termini and the
    stitched ends (`termini + route_endpoints(line)`, deduped) — adding the
    termini rather than replacing the stitched ends, so a lollipop's ring point
    isn't dropped in favour of the stem tip (union is recall-monotonic). `start`
    keeps `line[0]` when it is already a terminus, so clean routes don't move.
    Live-validated on the "Medvěd*" fixture (branched *Medvědí okruh* recovers all
    4 real trailheads; see Next-steps).
- **Closure T-junctions: handled.** `route_cycle_count` now nodes on *every*
  vertex (welded by coordinate), so a way whose endpoint lands on another way's
  interior vertex shares that exact node and the join is seen — a loop closed
  only through a T-junction reads as closed. (The earlier endpoint-only version
  missed this; it was the headline of the 2026-06-23 live test.) Residual gap:
  closure welds at `weld_m≈1 m`, so a loop left open by a digitization gap wider
  than that reads as open in `route_cycle_count` — `is_circular`'s start≈end line
  fallback (`HIKE_LOOP_TOLERANCE`, 150 m) is the backstop, and `roundtrip=yes`
  still wins regardless.
- **Local DEM merges tiles in memory.** For large regions, switch to a GDAL VRT
  over `dem_dir` (`gdalbuildvrt`) and sample the VRT — avoids loading everything.
- **No caching.** Overpass and elevation results should be cached (disk/SQLite)
  to respect usage policies and speed up repeat queries. Add before heavy use.
- **CLI quota line gates on `mode != "local"`, not on "the API was actually hit
  this run."** Today that's equivalent (no rasterio/tiles → `auto` always uses the
  API). But once the local DEM backend is live, `auto`-with-working-tiles won't
  touch the API yet would still print a (possibly stale) `N/1000` line. When you
  wire up local DEM, tighten the gate to "API actually used this run" (e.g. track
  an in-process request count on the provider and surface it) so the line only
  shows when relevant.
- **Round-trip vs point-to-point gain:** we report cumulative gain over the
  stitched line as-is. If a route is one-way, decide whether to report return
  gain too. Currently `loss` gives you the reverse direction's gain.
- **Naming:** routes without `name`/`ref` fall back to `route/<id>`. Could
  enrich with start/end place names via reverse geocoding (Felt/TomTom/Nominatim).
- **Access is best-effort, not ground truth.** `car_access=False` /
  `chairlift_access=False` mean "nothing of that kind is *mapped* in OSM near the
  route's ends," not "you can't get there." The tool description says this; keep
  it honest if you change the output. Loop detection, by contrast, is reliable.
- **Car access is parking-only.** We deliberately don't query drivable roads
  (dense → proximity cost, and tag-fragile: `highway=track` + `motor_vehicle=no`).
  If recall is too low (real trailheads with a road but no mapped parking), add
  drivable-highway *nodes* near endpoints as a second signal — not all road geometry.
- **Access is measured at endpoints only.** A trail that passes a car park or
  lift mid-route but starts/ends elsewhere reads as no-access. That's intentional
  (you want to start/finish where the car/lift is), but note it before "fixing."
- **Over-length guard is a heuristic, not bbox-clipping.** `HIKE_MAX_ROUTE_FACTOR`
  × bbox-diagonal drops through-routes cheaply, but it can also drop a genuinely
  long loop in a small bbox, and it doesn't *clip* a route to the area (distance is
  still the whole stitched line). Real fix is clipping member ways to the bbox —
  a larger change, deliberately deferred.
- **Loop-with-spur (lollipop):** now reported **circular** — circuit rank counts
  the loop and ignores the dangling stem, where the old even-degree test reported
  it one-way. This was the headline fix of step 3. (A bare out-and-back with no
  loop has circuit rank 0 and is still correctly non-circular.)
- **Loops are genuinely sparse in the raw data** (observed live: 1 of 12 around
  Špindl). Most KČT `route=hiking` relations are *linear* marked segments (a
  coloured trail A→B); circular day-hikes are usually ad-hoc *combinations* of
  segments. This tool reports each relation as-is — it does NOT compose loops from
  multiple segments. So `circular=true` returns the genuinely-mapped loops, which
  is correct but will feel thin. Composing loops (graph search over connected
  segments returning to start) is the natural future feature if the user wants
  "give me a loop of ~12 km" rather than "find mapped loops."

## Conventions

- Pure math stays network-free and tested. Keep it that way — it's the trust
  anchor. Any new measurement logic gets a unit test.
- Coordinates are `(lat, lon)` tuples everywhere. Don't flip them; Overpass and
  rasterio disagree on order, and the seams are already handled in their modules.
- Config is env-driven (`config.py`). Don't hardcode endpoints in logic modules.

## Quick commands

```bash
pip install -e .             # CLI + web UI (no LLM); extras: ".[mcp]" ".[local-dem]" ".[dev]"
pytest -q                    # 72 tests, all offline (pure math + Overpass parser + CLI + elevation API + daily quota + live closure fixture)
hike-finder --bbox 50.72 15.58 50.74 15.62 --user-agent you@example.com
hike-finder-web              # local web UI on http://127.0.0.1:8765
hike-finder-mcp              # MCP server over stdio (needs the `mcp` extra)
```
