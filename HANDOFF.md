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
  **Validated live over stdio and pinned by `tests/test_server.py`** (2026-06-24).

Each frontend also has a **thin launcher** in `scripts/` (`cli`/`web`/`mcp`, in
`.sh` + `.ps1`): it sets a default `HIKE_OVERPASS_UA` (only if unset) and forwards
args to the entry point — no logic, so it can't drift. The MCP launcher writes
NOTHING to stdout (that's the JSON-RPC channel). `.gitattributes` pins `*.sh` to
LF so the bash launchers survive a Windows (`autocrlf=true`) checkout. All three
are pinned by `tests/test_launchers.py` (MCP via a real stdio handshake). The
default-contact path is **validated live** (2026-06-24): with no `HIKE_OVERPASS_UA`
and no `--user-agent`, `scripts/cli.sh` reached Overpass (no 406) and returned the
Špindl okruh — so the baked-in default UA genuinely satisfies the public server.

```
frontends (pick one; cli/web need no LLM):
  cli.py  ─┐
  web.py  ─┼─→ search.search_hikes(bbox, criteria, cfg)   # shared orchestration
  server.py┘     (MCP tool find_hikes; needs the optional `mcp` extra)
       ├─ overpass.fetch_area(bbox)          # routes + parking + lifts  [NETWORK, CACHED (TTL)]
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
                 ├─ elevation.lookup(points)       # api/local/auto    [NETWORK/DISK; API CACHED, no TTL]
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

### Saved areas (offline snapshots) and near-miss results — added 2026-06-24

Two features added in response to "stop wasting API calls re-searching one area"
and "show close results, especially when 0 match":

- **Area snapshots (`snapshot.py` + `search.download_area`/`search_snapshot`).**
  `download_area` fetches Overpass ONCE and warms elevation for *every* geometry-
  plausible route (it runs `find_hikes` with empty `Criteria`, so the over-length
  guard still prunes through-routes), then prunes the stored routes to exactly the
  guard survivors and saves `{bbox, area, elevations, sample_interval}` to JSON.
  `search_snapshot` searches that file with **zero network**, reusing the *unchanged*
  `find_hikes` driven by two swapped seams: the saved `AreaData` instead of
  `fetch_area`, and a `SnapshotElevationProvider` instead of the API. So offline ==
  online **by construction**, not a parallel code path. Key mechanics:
    - `RecordingElevationProvider` wraps the real provider during download and
      records every `point → elevation`.
    - `SnapshotElevationProvider` replays them; a missing point raises
      `ElevationError` (the route degrades to n/a, same all-or-nothing as live).
    - Elevation keys are rounded to **7 decimals (~1 cm)** at store *and* lookup, so
      a hit never depends on bit-exact float reproduction across the two processes.
    - `search_snapshot` **locks** the snapshot's `sample_interval_m` (the saved
      points were taken at it) but leaves `gain_threshold`/`smooth_window`/access
      radii/`loop_tolerance` tunable; the over-length guard reuses the snapshot bbox.
  Coords round-trip JSON as lists and are restored to **tuples** on load (the vertex
  graph / `dict.fromkeys` need hashable points).
    - **Fail-safe, never wrong:** a snapshot lookup is all-or-nothing per route, so any
      key that doesn't match degrades that whole route to `n/a` — it can never return a
      *wrong* elevation. Same-machine the resample is bit-identical (validated), so this
      never fires. The only edge is *cross-machine* sharing: a coordinate on a 7th-decimal
      rounding boundary that two platforms round oppositely would degrade that one route
      to `n/a`. Harmless for the real use case (download then search offline on the same
      laptop); revisit the rounding only if snapshots are ever shared between machines.
- **Near-misses (`filters.find_hikes(near_miss=…)`).** Tri-state `False | True |
  "auto"`. `"auto"` (the frontend default) shows near-misses only when there are
  **zero** strict matches. A relaxed cheap gate (`Criteria.accepts_geometry_relaxed`)
  admits routes just outside the cut — distance within `HIKE_NEAR_MISS_DIST_KM`,
  parking/lift within `radius × (1 + HIKE_NEAR_MISS_RADIUS_FRAC)` — into a second
  bucket; their elevation is paid for **only when near-misses actually engage**, so
  the API economy is intact when matches exist. `near_miss_notes` annotates each
  with the literal gap (`gain 709 m — 41 m below the 750 m minimum`). Shape is never
  relaxed (a loop is not "almost point-to-point") and *excluded* access stays strict,
  so a near-miss always shares the requested shape/exclusions — the "don't label
  wrong-shape routes close" hazard. Near-misses sort after matches and carry
  `Hike.near_miss=True` + `Hike.notes`; `format_hike` prefixes `~` and appends
  `[near miss: …]`, `hike_to_dict` adds `near_miss`/`notes`. Access distance comes
  from `access.nearest_parking_m`/`nearest_lift_m` (measuring siblings of the boolean
  predicates — the live-pinned `car_accessible`/`matched_access_points` are untouched).

**Frontends.** CLI: `--download FILE` / `--area FILE` / `--near-misses`
(`BooleanOptionalAction` → on/off, default `auto`). Web: a **"Download view"**
button + area-name field, a saved-area selector, a near-miss select; new routes
`/api/download`, `/api/areas`, and `area=`/`near_misses=` on `/api/hikes`
(snapshots saved under `HIKE_SNAPSHOT_DIR`, name slugified so it can't escape the
dir). MCP: `find_hikes` gains optional `area` (offline) + `near_misses` params (its
schema `required` is now `[]`, validated in code), plus a new `download_area` tool.

**Validated live (2026-06-24, local DEM so zero API quota).** On bbox
`50.72,15.58,50.74,15.62`: `--download` (process 1) → `--area` (process 2) →
gain/loss/distance/booleans for **all 11 routes equal a live `--bbox` search
byte-for-byte, 0 `n/a`** — proves cross-process key matching on real geometry. A
`--min-gain 750` query (nothing meets it) surfaced 2 near-misses (`+709 m`, 41 m
short; `+693 m`, 57 m short) with notes; `--no-near-misses` returned the empty
message. Identical results via the web UI (live `/api/download` → 11 routes, offline
`/api/hikes?area=`) and the MCP `find_hikes(area=…)` tool over the real protocol.
Tests: `test_snapshot.py`, `test_near_miss.py`, `test_web.py`, plus new cases in
`test_cli.py`/`test_server.py`. Suite now 144 offline (excludes the 3 env-broken
`.sh` launcher cases on this WSL-less box; `.ps1` equivalents pass).

### Loop composition (compose.py) — added 2026-06-24

A fourth, opt-in search mode beside `search_hikes`/`download_area`/`search_snapshot`:
`search.compose_loops(bbox, criteria, …)` builds one trail-network graph from every
relation's member ways (`compose.build_trail_graph`, clipped to the bbox), searches it
for cycles of a target length (`compose.find_loops`), and wraps each as a synthetic
`roundtrip=yes` route fed through the **unchanged** `find_hikes` — so a composed loop's
elevation/distance/access are computed by the same engine, and it sorts/filters alongside
ordinary routes. It is NOT folded into `circular=true` (that still reports mapped loops
as-is). Surfaced as `--compose-loops` (CLI) / a checkbox (web) / `compose_loops` arg
(MCP). Full design, knobs, and the live byte-for-byte validation are in **Known
limitations → "Loop composition — DONE and VALIDATED LIVE"** below.

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

- `elevation/local_dem.py` — the synthetic-GeoTIFF regression (`tests/test_local_dem.py`,
  guarded by `pytest.importorskip("rasterio")` so it skips without the `local-dem`
  extra): known-cell round-trip, the two-tile **merge seam**, nodata→raise *and*
  nodata→fill, out-of-coverage→raise/fill, and the empty-dir error. Pins the
  sampling/merge/nodata logic that the live Copernicus run exercised.

Run it: `pytest` → 186 tests (183 pass; the 3 `.sh` launcher cases need bash).

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

- **Start-coupling** (`filters._route_start` aiming `start` at the terminus nearest
  a matched parking/lift) — validated live 2026-06-24 on the Špindlerův Mlýn bbox,
  the gap the closure fixture (no parking) had left. One Overpass round-trip saved
  as `tests/fixtures/spindl_area.json` (15 routes / 31 parking / 5 lifts). Findings:
  - **iff holds on all 15 routes:** `car_access or chairlift_access` ⟺
    `matched_access_points` non-empty — the "verdict and start can't disagree"
    guarantee, now on real data, not just synthetic.
  - **Fires on 14/15** (every route with termini ∧ matched access); the lone
    abstainer is the pure loop *[Z] Špindlerův mlýn – okruh* (0 termini), correctly
    **not** coupled — start stays at the head even with ring parking matched (the
    documented loop limitation, now pinned live).
  - **Discriminates on 10 routes** (coupled `start` ≠ old fallback), so the branch
    isn't a no-op on this data. Two cross-checked against real geography:
    *Špindlmanova mise* (point-to-point) couples onto the **Medvědín chairlift
    base, ~31 m away, ~1.9 km from the fallback head**; the branched *Medvědí okruh*
    (4 termini, stitch covers ~42%) couples onto the **Horní Mísečky–Medvědín lift,
    ~29 m**, at a terminus the greedy stitch can't even reach. Both land on a named,
    real trailhead you drive/ride to — exactly the intent.
  - Pinned by `tests/test_coupling_live.py` (iff on all routes, both headline
    couplings, the pure-loop non-coupling, and a ≥5-routes-moved no-op guard).
  - **Residual synthetic-only gaps (no live case in this bbox):** a *lollipop* with
    parking on the ring (start should stay at the stem tip — unit-tested only) and
    *zero-churn on a no-access route* (every route in this dense resort bbox has
    some access, so the live check was vacuous — unit-tested in `test_access.py`).

- **Local DEM backend** (`elevation/local_dem.py`) — validated live 2026-06-24 with
  `rasterio` 1.5.0 (cp314 wheel installs clean on Python 3.14; GDAL 3.12.1 bundled).
  DEM source is **Copernicus GLO-30 on AWS — anonymous, no auth** (SRTM via USGS needs
  a login; skip it). Tiles are keyed by SW corner; the **N50 E015** COG (one ~29 MB
  GeoTIFF, EPSG:4326, float32, `nodata=None`) covers Sněžka + Špindl + Medvědín:
  `https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N50_00_E015_00_DEM/...tif`.
  - **Ground truth:** `LocalDemElevationProvider` read **Sněžka = 1601.4 m vs the known
    1603 m (−1.6 m)** — a near-exact hit on the country's highest peak that *simultaneously*
    confirms lat/lon axis order (the code calls `rowcol(transform, lon, lat)`; a swap sends
    the point off-tile → `ElevationError`, verified), band, float scaling, and that nodata
    doesn't leak. Off-summit refs (Luční hora, Medvědín) missed by 50–230 m at my
    from-memory coords, but a ±0.005° grid-max around each recovered the summit height
    (Luční −19 m, Medvědín *overshoots* +44 m onto higher ridge terrain) — i.e. the misses
    are coordinate error, not a provider bug (a systematic bug could never nail Sněžka).
  - **End-to-end:** `find_hikes` on the Špindl bbox with a local provider returned all
    **11 routes with 0 nulls**; the pure loop read **+30/−23 m** (loop invariant gain≈loss
    holds; documented API value was +34/−34, the looser closure is the raw-DEM-is-noisier
    effect), and `[Z] Richtrovy Boudy → Špindlerův mlýn` read **+693/−268 m** vs the
    documented API **+678/−251 m** — two independent anchors confirming local gains track
    the API (~2–7% higher, as expected; raw 30 m DEM wants a higher gain threshold — a
    documented per-source tuning TODO, not retuned here on one bbox).
  - **Exercised through both no-LLM frontends, not just the provider:** the real
    `hike-finder` CLI in `--elevation-mode local --dem-dir <tiles>` returned all 11
    routes with gains and an empty stderr (no quota line — local never hits the API);
    `auto --dem-dir` likewise answered from disk and left the daily counter unchanged
    (the gate fix below); and the real `hike-finder-web` server (env `HIKE_ELEVATION_MODE=local`)
    served `/api/hikes` → HTTP 200, 11 routes, 0 nulls, identical gains, with `/api/quota`
    reading 0→0 across the request. So local DEM is validated live on CLI **and** web,
    both via the shared `search_hikes` path.
  - Code note: `self._nodata = srcs[0].nodata` uses only the first tile's nodata; Copernicus
    reports `nodata=None`, so a void/ocean point would leak a raw value — but the bounds-check
    catches off-tile regardless. Pinned offline by `tests/test_local_dem.py` (synthetic tiles).

## What is WRITTEN but UNVALIDATED (needs a networked machine)

**Nothing left here — all three frontends are now validated live.** Overpass,
the API elevation backend, the local DEM backend, and the MCP server are all
exercised end-to-end.

`server.py` — **VALIDATED LIVE (2026-06-24)** with `mcp` 1.28 on Python 3.14.
Spawned `python -m hike_finder.server` and spoke MCP over real OS stdio (the
SDK's `stdio_client`): `initialize` + `list_tools` advertised `find_hikes` with
`required = [south, west, north, east]` and all 11 properties; `call_tool
find_hikes` against the Špindlerův Mlýn bbox returned a real engine-computed,
`format_hike`-rendered result (*Špindlerův mlýn - okruh — 1.11 km, +34 m / -34 m
[loop, car, lift:chair_lift]*, OSM relation 6282999); an impossible filter gave
the friendly "No matching hikes found" message (`isError=False`); an unknown
tool surfaced as `isError=True` "unknown tool: …". Pinned offline by
`tests/test_server.py` (6 tests): five drive the **real MCP protocol over an
in-memory client/server session** — `search_hikes` stubbed for the glue tests
(schema, the tri-state argument→Criteria mapping, shared rendering, empty case,
unknown-tool error), and only the two network boundaries (`fetch_area`,
`get_provider`) stubbed for one engine-integration test against the live
`spindl_area.json` fixture — and a sixth spawns the **real `python -m
hike_finder.server` subprocess** and does `initialize` + `list_tools` over OS
stdio pipes (network-free, so hermetic) to guard the actual `stdio_server()` /
`main()` transport the in-memory session can't reach. The test bodies are sync
`asyncio.run(...)` wrappers, not bare `async def`, so they run regardless of
whether `pytest-asyncio` is present. The `mcp` extra is now also in the `dev` extra, and the module
`pytest.importorskip("mcp")`s so a base install stays green. The SDK's decorator
API has shifted across versions; adjust imports in `server.py` if a different
`mcp` version won't start.

(`web.py` is validated live too — `/` serves the page, `/api/hikes` reuses the
validated `search_hikes` path and returns correct UTF-8 JSON.)

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
     `_route_start` is **coupled to the access result** (refinement, 2026-06-24):
     when the route has matched access AND termini, `start` is the terminus
     *nearest a matched parking/lift* (`access.matched_access_points`, the same
     `<= radius` predicate as the booleans — a test pins `car or lift True` ⟺
     non-empty), tie-broken by coordinate; so the marker lands on the trailhead you
     actually drive/ride to. With no matched access it falls back to the prior rule
     (keep `line[0]` when it's a terminus — zero churn on clean routes — else the
     smallest terminus). Candidates are termini ONLY, so a lollipop's start stays at
     the stem tip even with parking on the ring; a **pure loop has no terminus, so
     its start is never coupled** (stays at the arbitrary head — known limitation,
     loop start is geometrically arbitrary anyway). Live-gate caveat (now closed):
     the "Medvěd*" fixture carries no parking/lift data, so the coupling branch
     couldn't be exercised there — it was **validated separately 2026-06-24 on the
     parking-bearing Špindlerův Mlýn bbox** (`tests/fixtures/spindl_area.json`,
     `tests/test_coupling_live.py`): iff on all 15 routes, the start couples onto the
     Medvědín / Horní Mísečky lift trailheads (point-to-point + branched), the pure
     loop stays uncoupled, 10 routes move vs the fallback. See the start-coupling
     bullet under "VALIDATED LIVE". (`start` is also a label-only field — no filter
     reads it; `add_elevation` uses `line` — so a mis-couple is low-stakes regardless.)
     Validated live on the fixture for the termini themselves: the branched
     *Medvědí okruh* (rel 6285306, only 42% stitch coverage) recovers **4 genuine
     termini ~2.46 km apart** (the old code tested 2 ends, only 1 a real terminus);
     the real KČT okruhs are lollipops whose stem tip is tested *in addition to* the
     ring point; every clean linear route is unchanged. Pinned by
     `tests/test_closure_live.py` termini ground truth plus a lollipop ring-parking
     guard and the start-coupling cases in `tests/test_access.py`. Distance no longer
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
6. **Local DEM backend** — **DONE and VALIDATED LIVE (2026-06-24).** Copernicus
   GLO-30 (anonymous AWS), Sněžka read 1601.4 m vs 1603 m, 11/11 routes 0 nulls,
   loop invariant holds, gains track the API. Offline regression in
   `tests/test_local_dem.py`. **Large-region VRT now DONE + LIVE too (2026-06-29)**
   — the in-memory merge is replaced by a GDAL VRT, see the resolved item below.
7. ~~**Wire MCP end-to-end** and call `find_hikes` from Claude Code (the last
   unvalidated frontend).~~ **DONE and VALIDATED LIVE (2026-06-24)** — driven
   over real OS stdio with `mcp` 1.28, pinned offline by `tests/test_server.py`.
   See "What is WRITTEN but UNVALIDATED" above. Only the polish items below remain.

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
    is **coupled to access**: the terminus nearest a matched parking/lift
    (`access.matched_access_points`, same predicate as the booleans), else it
    keeps `line[0]` when that is already a terminus, so clean routes without access
    don't move. Candidates are termini only (lollipop start stays at the stem tip).
    **A pure loop has no terminus, so its start is never coupled** — it stays at the
    arbitrary head even when parking is matched; low-stakes, loop start is
    geometrically arbitrary. Live-validated on the "Medvěd*" fixture for the termini
    (branched *Medvědí okruh* recovers all 4 real trailheads; see Next-steps); the
    coupling itself is now **live-validated** on the parking-bearing Špindlerův Mlýn
    bbox (`tests/test_coupling_live.py`) — start couples onto the Medvědín / Horní
    Mísečky lift trailheads, pure loop stays uncoupled. Residual synthetic-only
    gaps: a lollipop with ring parking, and zero-churn on a no-access route (no such
    case exists in that dense-resort bbox).
- **Closure T-junctions: handled.** `route_cycle_count` now nodes on *every*
  vertex (welded by coordinate), so a way whose endpoint lands on another way's
  interior vertex shares that exact node and the join is seen — a loop closed
  only through a T-junction reads as closed. (The earlier endpoint-only version
  missed this; it was the headline of the 2026-06-23 live test.) Residual gap:
  closure welds at `weld_m≈1 m`, so a loop left open by a digitization gap wider
  than that reads as open in `route_cycle_count` — `is_circular`'s start≈end line
  fallback (`HIKE_LOOP_TOLERANCE`, 150 m) is the backstop, and `roundtrip=yes`
  still wins regardless.
- **Local DEM large-region VRT — DONE and VALIDATED LIVE (2026-06-29).** The old
  in-memory `rasterio.merge` (fine for one tile, didn't scale) is replaced by a
  GDAL **VRT** that is point-sampled, so memory stays flat regardless of region
  size. We *build the VRT XML directly* from each tile's georeferencing rather
  than calling `gdalbuildvrt`: rasterio doesn't wrap GDAL's VRT builder, and
  neither that CLI nor the `osgeo` bindings ship with the `local-dem` (rasterio)
  extra — confirmed both absent here. The generated doc is what `gdalbuildvrt`
  would emit for homogeneous single-band tiles; a user-supplied `*.vrt` in
  `dem_dir` wins (escape hatch for mixed-resolution tiles needing resampling,
  e.g. GLO-30 across a latitude band — our builder *raises* `ElevationError` on
  mixed CRS/resolution rather than silently misregistering). The first-tile-only
  nodata leak (`srcs[0].nodata`) is fixed: each VRT source declares its own
  nodata, masked against one band nodata value. The extent **bounds-check stays
  load-bearing** — a `nodata=None` (Copernicus) DEM samples off-coverage points
  as `0.0` (a valid sea-level reading), so `sample()` alone can't catch them.
  `lookup()` bounds-checks every point, samples only the in-bounds ones, and
  scatters results back by index (order/length preserved). Offline gates in
  `tests/test_local_dem.py` (overlap → no phantom seam + top-tile-wins, single
  tile, mixed CRS/res raise, no-nodata off-coverage raise, user `.vrt`).
  Live: two adjacent real GLO-30 tiles (N50_E015 + N50_E016) → 4800×3600 mosaic,
  18 seam-straddling points sampled identically through the VRT vs each tile
  standalone (0 mismatches → no off-by-one), Sněžka VRT == tile-A standalone
  byte-for-byte (1597.66 m), and the Špindl fixture run end-to-end through the
  2-tile VRT gave 11/11 routes 0 nulls with the pure loop +30/−23 m (gain≈loss).
- **Caching — DONE and VALIDATED LIVE (2026-06-24).** A transparent SQLite cache
  (`cache.py`, stdlib `sqlite3`) sits at the two network seams so repeat/overlapping
  searches don't re-hit the public servers — the OSM-usage-policy ask, now satisfied.
  On by default; opt out with `--no-cache` / `HIKE_CACHE=0`. Two stores, different
  staleness models:
  - **elevation** — keyed by `(endpoint, rounded coord)`, **no TTL** (terrain is
    immutable). Keyed by the **full endpoint, not the host** — OpenTopoData `srtm30m`
    and `aster30m` share a host but return different elevations, so host-keying (what
    `quota.py` does, fine for a counter) would cross-serve. Because route relations
    carry full member geometry regardless of bbox, the *same route resamples to the
    same points across different overlapping bboxes*, so this cache hits across bbox
    changes, not just exact re-runs — the higher-value half.
  - **overpass** — keyed by `sha256(url + build_query(bbox))` (auto-invalidates if the
    query shape changes), **TTL-gated** (`HIKE_OVERPASS_CACHE_TTL_DAYS`, default 30;
    trails change slowly; 0 disables Overpass caching). Stores `AreaData` as JSON via
    snapshot's `_area_to_json`/`_area_from_json`.
  Wiring: `get_provider(cache=…)` wraps **only the `ApiElevationProvider`** with
  `CachingElevationProvider` (local DEM is fast disk, never cached; per-endpoint key
  keeps DEM values out of an API-mode cache). `search._fetch_area` consults the
  overpass cache; `download_area` deliberately **bypasses the overpass read** (a named
  snapshot must reflect current OSM) but still refreshes both caches. **Failure-
  isolated:** every DB op degrades to a clean miss / no-op on any sqlite/OS error
  (corrupt, locked, disk-full, read-only) — a broken cache is invisible, never fatal,
  mirroring `quota._read`. `--clear-cache` empties it. Default location: the per-user
  cache dir alongside the quota counter (`HIKE_CACHE_DIR` to override; resolved live so
  it isolates in tests). Validated live on the Špindl bbox: cold search **4.2 s** (live
  Overpass + elevation) → warm **0.4 s**, **byte-identical** results, 0 network; the DB
  held 1 area + 163 elevation points keyed by `…/v1/srtm30m`; `--no-cache` re-fetched
  in **3.9 s**. Pinned offline by `tests/test_cache.py` (19 tests) — store round-trip,
  source isolation, TTL expiry, `IN(...)` chunking past SQLite's var limit, failure-
  isolation, the decorator's hit/miss/order/error-propagation contract, and the
  **headline goal**: a repeated `search_hikes` makes zero further elevation requests
  and leaves the `DailyQuota` counter unchanged, plus an elevation-cache-hits-across-
  bboxes case.
  - *Caveat for future live re-validation:* the byte-for-byte offline==live checks
    elsewhere in this doc assume a cold network. Run those with `--no-cache` (or a
    throwaway `HIKE_CACHE_DIR`) so the cache doesn't mask whether the network was
    actually hit. The elevation table grows unbounded (~tens of bytes/point → tens of
    MB even for millions of points); no eviction for a personal tool.
- **CLI quota line — gate tightened to "API actually hit this run" (DONE
  2026-06-24).** `cli.run` now snapshots the daily counter before and after
  `search_hikes` and prints the line only when it went up (and `limit > 0`).
  So `auto`-with-working-tiles, which answers from the local DEM and never touches
  the API, stays silent (live-verified: counter `0→0`, no line); `api` and
  DEM-less `auto` still show it (verified by recording one request → line prints).
  No provider plumbing needed — the persistent counter already reflects real usage.
- **Round-trip vs point-to-point gain:** we report cumulative gain over the
  stitched line as-is. If a route is one-way, decide whether to report return
  gain too. Currently `loss` gives you the reverse direction's gain.
- **Naming (reverse-geocode) — DONE and VALIDATED LIVE (2026-06-29).** Routes with
  no OSM `name`/`ref` used to render as the synthetic `route/<id>`. They can now be
  **opt-in** labelled from the place names at their ends — e.g. `Labská → Špindlerův
  Mlýn`, `loop near <town>`. Design, in the project's seam-and-pure-core style:
  - **`naming.py` (pure):** `label_endpoints` picks the (start, end) points to look up
    — start is the coupled start marker; end is the terminus/stitched-end *farthest*
    from start, tie-broken by coordinate (deterministic); a LOOP has no end (switch on
    `circular`, so a lollipop still reads "loop near X"). `compose_label` assembles the
    string ("A → B", "near X" when both ends resolve to one place, "loop near X", or
    `None` so the `route/<id>` fallback is kept). `enrich_names` is glue over an
    INJECTED geocoder (testable with a stub), skipping named and composed routes.
  - **`geocode.py` (network seam):** `NominatimGeocoder` honours Nominatim's policy —
    a ≥1 req/s throttle (across the search, not per route), a contact `User-Agent`
    (the Overpass contact, threaded through), and **no retry** (a 429 means back off).
    Best-effort: ANY failure (network/HTTP/parse/no-place) returns `None`, so a miss
    just keeps `route/<id>` and never breaks a search. Endpoint configurable
    (`HIKE_NOMINATIM_URL`). `_parse_place` picks the most specific settlement.
  - **Cache seam:** a third `geocode` store in `cache.py` (TTL-gated, default 365 d;
    place names change slowly) + `CachingGeocoder`, so a trailhead coordinate is looked
    up **at most once across runs and across routes that share it** (the two relations
    6282997/6282998 that share endpoints geocode once). A **negative** result is cached
    as `""` so an empty point isn't re-queried. Failure-isolated like the other stores;
    `--clear-cache` empties it too.
  - **The "unnamed" signal is carried from the source of truth** (`overpass.parse_area`
    sets `route["unnamed"] = not (name or ref)`) → `Hike.unnamed`, NOT reconstructed
    from the `route/<id>` string downstream (the advisor's catch — a reconstructed
    magic string silently breaks if either side drifts).
  - **Honesty:** `Hike.name`/`ref` stay the truthful OSM values; the derived label lives
    in the separate `Hike.place_name` (default `None`, so every prior construction is
    byte-for-byte unchanged). `format_hike` shows the label but marks the identifier
    clause `unnamed OSM relation <id>` so a geocoded label is never mistaken for a
    signed trail name; `hike_to_dict` exposes BOTH `name` and `place_name` + `unnamed`.
  - **Opt-in everywhere** (Nominatim policy): `--name-places` (CLI), a "Name unnamed
    routes from places" checkbox (web), a `name_places` arg (MCP), or `HIKE_GEOCODE=1`.
    Only the *matched* survivors are geocoded (the same two-pass economy as elevation).
    Composed loops are skipped (they carry "composed of …", never `route/<id>`).
  - **Export carries the label too** (advisor catch — the "last mile" must agree with the
    terminal): `export.py`'s GPX `<trk>`/`<wpt>` name uses `place_name or name`, so a GPS
    gets `Labská → …` not `route/<id>`; GeoJSON keeps `name` truthful and gains `place_name`
    + `unnamed` via `hike_to_dict`. `--name-places --download` is a logged no-op too (a
    snapshot stores raw routes). Pinned in `tests/test_export.py`.
  - **Offline `--area` is an HONEST no-op:** geocoding needs the network a snapshot
    search never touches, so `search_snapshot(name_places=True)` LOGS a warning rather
    than silently dropping it (the advisor's point — don't contradict offline==online).
    v2: record place names into the snapshot at download time, like elevations.
  - **VALIDATED LIVE (2026-06-29):** the 3 genuinely-unnamed routes in the Špindlerův
    Mlýn fixture (rels 6133825, 6282997, 6282998) were driven through a REAL Nominatim
    call (elevation stubbed, since that's already validated) and all 3 got real Czech
    place labels: `Labská → Štěpanická Lhota` and `Labská → Špindlerův Mlýn` (×2). Pinned
    offline by `tests/test_naming.py` (pure label logic + enrich + search-layer wiring +
    offline no-op log + format marker) and `tests/test_geocode.py` (parse + a mocked
    `requests.get` for request-shape/best-effort-failure + the geocode cache & negative
    caching & dead-cache degrade). Suite **261** (258 without bash).
- **Access is best-effort, not ground truth.** `car_access=False` /
  `chairlift_access=False` mean "nothing of that kind is *mapped* in OSM near the
  route's ends," not "you can't get there." The tool description says this; keep
  it honest if you change the output. Loop detection, by contrast, is reliable.
- **Car access is parking-only.** We deliberately don't query drivable roads
  (dense → proximity cost, and tag-fragile: `highway=track` + `motor_vehicle=no`).
  If recall is too low (real trailheads with a road but no mapped parking), add
  drivable-highway *nodes* near endpoints as a second signal — not all road geometry.
- **Access is measured at endpoints for point-to-point routes; along the whole
  line for LOOPS** (2026-06-24). A point-to-point trail that passes a car park or
  lift mid-route but starts/ends elsewhere reads as no-access — intentional (you
  want to start/finish where the car/lift is). But a *loop* has no meaningful
  "end": its stitched ends are arbitrary points on the ring, so testing only there
  missed a lift the loop merely passes (it returned **0** lift-served loops on a
  Cadore bbox where 2→4 loops actually have a lift). For a circular route the
  car/lift booleans now test proximity along the whole line, UNIONed with the
  termini (so a feature at a terminus on a stitch-dropped member is not lost —
  recall-monotonic, a strict superset of the old endpoints). The `start` marker is
  still coupled to the termini only, so a pure loop's start stays at the head. An
  exact radius-padded-bbox pre-filter (`access._bbox_pad`) keeps the whole-line
  scan cheap (Cadore cheap pass 5.6s→1.4s). See `filters.measure_geometry`.
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
  segments. The plain search reports each relation as-is, so `circular=true`
  returns the genuinely-mapped loops — correct but thin.
- **Loop composition — DONE and VALIDATED LIVE (2026-06-24).** `compose.py` (pure,
  network-free) answers "give me a loop of ~N km" by combining connected marked
  trails, the natural fix for the sparsity above. Two stages:
  - `build_trail_graph` welds every relation's member ways into ONE full-vertex
    multigraph (same weld rule / 1 m tolerance as `geometry._vertex_graph`, so
    junctions are exact shared OSM nodes — **NOT** raised to bridge gaps, which is the
    live-falsified endpoint-cluster bug from the Medvěd* closure work), then
    **dedups coincident micro-edges by welded node-pair** (the same OSM way belongs
    to many relations → without dedup every shared interior node inflates to degree-4
    and spawns zero-area *sliver* loops; observed: dedup cut the Špindl graph from
    1042 noisy segments to 67 clean ones, degree dist 15 dead-ends / 32 T / 5 X),
    and **contracts** degree-2 chains into `Segment`s (junctions = degree≠2). Genuine
    parallel trails keep distinct intermediate nodes, so they survive as a real
    two-segment loop; an all-degree-2 component is itself a loop (self-loop segment).
  - `find_loops` is a bounded, **deterministic** simple-cycle search: min-node-start +
    edge-set-frozenset dedup collapse both directions/rotations; length-prune the
    instant a partial path exceeds `max_m`; cap segments/loop (`compose_max_segments`,
    12) and a global expansion budget that aborts with `capped=True` (logged, never
    silent); leaf-prune dead-ends to the 2-edge-connected core; **near-duplicate
    collapse** drops a loop sharing > `compose_overlap_frac` (0.6) of its length with
    an already-kept shorter loop. Sorted neighbours + stable seg ids ⇒ identical output
    run-to-run.
  - **`max_loops` cap (`compose_max_loops`, 15) — load-bearing, not cosmetic.** A
    realistic-bbox run (broader Krkonoše, 50.68/15.50/50.80/15.70, 269 segments) found
    **72** distinct loops in 8–15 km. Since `compose_loops` pays an elevation lookup
    *per returned loop*, an uncapped set would break the two-pass economy and blow the
    elevation quota (72 × ~400 pts over the ~1 req/s public API = hours). So the
    survivors are ranked by **Polsby–Popper compactness** (4πA/P²; roundest first, which
    also demotes any thin near-sliver for free) and truncated to `max_loops`;
    `ComposeResult.distinct` carries the pre-cap count so `compose_loops` can log
    "N distinct … showing top M" (truncation never silent). The realistic run returned
    a clean, varied 15 (compactness 0.42–0.62, 8–12.4 km, 6–13 trails each) in 127 ms,
    not capped. NB the ranking empirically showed slivers are rare: **0 of 72** loops were
    below 0.10 compactness. A dedicated **sliver filter is now DONE** (`min_compactness`,
    default `HIKE_COMPOSE_MIN_COMPACTNESS=0.05`): a hard compactness *floor* that DROPS a
    degenerate near-zero-area loop (out-and-back along two near-parallel trails) outright —
    before the near-dup collapse and the cap, so a sliver can neither sway a collapse nor
    eat a returned slot. The 0.05 default is a provable no-op on real data (observed min
    compactness 0.18 on the wide Špindl bbox, 0.39 on the known loop), while the pure
    `find_loops` default stays 0.0 (inert). `ComposeResult.slivered` carries the drop count
    so `compose_loops` logs it (never silent).
  - **Clipped to the bbox** (`clip_routes_to_bbox`): a composed loop must lie inside
    the searched area — without it 13 of 14 Špindl loops wandered out on a through-
    route. Coarse vertex-granularity clip (true geometric bbox-clip was deferred, then
    found to be a no-op for loops — boundary stems never sit on a cycle; see below).
  - **Wiring:** `search.compose_loops` wraps each loop as a synthetic `roundtrip=yes`
    route (one way = the closed loop line) and runs the **unchanged** `find_hikes`, so
    elevation/gain, distance, and car/lift access are computed identically and offline==
    online holds by construction. A composed loop carries no OSM id (`Hike.composed` +
    `composed_of` = constituent trail refs; `format_hike`/`hike_to_dict` show "composed
    of …", `osm_id: None`). The over-length guard is disabled for composed loops (they're
    already clipped + length-banded). Live on the Špindl bbox: **3.38 km, +114/−112 m**
    (gain≈loss invariant holds), `composed of 0402 + 1801 + Medvědí okruh + …` —
    **byte-identical across CLI, Web UI (HTTP), and MCP over real stdio.** Frontends:
    `--compose-loops` (CLI), a "Compose loops" checkbox (web), `compose_loops` arg on
    `find_hikes` (MCP). Knobs: `HIKE_COMPOSE_MIN_KM`/`MAX_KM`/`MAX_SEGMENTS`/
    `OVERLAP_FRAC`. Pinned by `tests/test_compose.py` (synthetic graphs: square, T-
    junction, figure-eight, parallel bigon, coincident dedup, determinism, budget cap,
    near-dup collapse, clipping) + `tests/test_compose_live.py` (Špindl fixture:
    go-signal connectivity, degree sanity, the known in-bbox loop, full pipeline).
    Residual future work: true geometric bbox-clipping (vs vertex-granularity) — but this
    is now understood to be a **provable no-op for composed loops**: a boundary-clipped
    trail ends at a degree-1 vertex, which `_active_segments` always prunes, so a boundary
    segment can never lie on a cycle; loop membership and length come from the 2-edge-
    connected core, which never touches the bbox edge. So it's not implemented (it would
    change no loop and no length). The *sliver filter* and *access-anchored loops* once
    listed here are both now DONE (see above).
  - **Elevation cost — segment-level shared sampling — DONE (2026-06-24).** Earlier this
    bullet claimed a dense compose run "exhausts the 1000/day quota" by assuming **~1 API
    request per point** (~5716 points → ~5716 requests). That was WRONG: `ApiElevationProvider`
    batches `batch_size=100` points/POST and `DailyQuota.record()` fires **once per POST**,
    so ~5716 points ≈ **58 requests**, and a default `max_loops=15` compose run is ~55
    requests — comfortably under the cap. Compose was never quota-bound on the API; it is
    *throttle*-bound (~1.1 s/request → ~60 s cold). The genuine inefficiency was redundancy:
    `find_hikes` resampled each loop's WHOLE line *from that loop's own start vertex*, so a
    trail segment shared by several loops was looked up once **per loop**, and the seam
    shifted per loop so the SQLite point cache barely hit across runs.
    The fix (now implemented): `search.compose_loops` resamples each **distinct used
    segment** once on its own canonical `a→b` grid (`compose.resample_segments`), looks it
    up once through the provider, and assembles each loop's elevation **series** from those
    shared per-segment results (`compose.assemble_loop_series`), handing it to `find_hikes`
    via the new `pre_elevations_by_id` / `add_elevation(use_presampled=…)` hook — which
    skips the redundant whole-loop resample/lookup and runs the *unchanged*
    `cumulative_gain_loss` on the series. The assembled series is closed (first = last =
    start-node sample), so **gain ≈ loss still holds**; only the sample positions move
    (segment-anchored vs loop-anchored), which **shifts published composed-loop gain values**
    — a deliberate behavior change. **LIVE-VALIDATED 2026-06-24** (controlled A/B against the
    real OpenTopoData API via a one-off harness — not committed; one live fetch → identical
    geometry, only the sampling grid differing): the OLD whole-loop path re-measured the documented
    Špindl loop at **+114/−112 m exactly** (harness gate PASS — live geometry matches the
    fixture), and the NEW segment-level path reads **+115/−109 m** there (Δ +1/−3 m; the
    shipped `hike-finder --compose-loops` CLI prints the same +115/−109, confirming the full
    wiring). On a wider real Krkonoše bbox (`50.68 15.52 50.80 15.70`, 15 loops) the per-loop
    shift ranges **−15…+19 m** (mostly single-digit; the largest are ~3–5 % on 300–500 m / 8–12 km
    loops) and **gain ≈ loss holds on all 15** (max |gain−loss| = 13 m) — i.e. the live shift is
    exactly the expected per-junction resampling noise, with no asymmetric or symmetric
    inflation. Smoothing/hysteresis are preserved because the *series* (not
    per-segment gains) is assembled, then gain is taken once. Dedup is **intrinsic** (each segment looked up once regardless of the cache, so
    `--no-cache` benefits too) and makes the points **cache-hot across runs** (segment-
    canonical points recur; loop-anchored ones don't). Measured on the Špindl fixture
    (offline, pure, on the **unclipped** fixture geometry): default 3–15 km band / 14 loops →
    **2.12×** fewer points (4865→2298, ~55→23 requests); wider 2–20 km / 15 loops → **2.85×**.
    The factor is ~2–3×, not the ~6× once implied, because `find_loops`' near-dup collapse
    (`overlap_frac=0.6`) selects for *diverse* loops, capping segment reuse among the shown 15.
    **Measured LIVE** on the wider real Krkonoše bbox above (15 loops, a real *clipped* search —
    `compose_loops` always clips to bbox): whole-loop 4624 pts over **15** lookup calls
    (~55 batched requests) → segment-level 2782 pts in **one** combined call (~28 requests),
    i.e. **1.66× fewer points / 55→28 ≈ 2.0× fewer API requests** (the request ratio beats the
    point ratio because the single combined call also saves each loop's per-batch tail waste).
    This sits *below* the offline 2.1–2.9×, and the live number is the **representative** one:
    the offline band was on unclipped fixture geometry, which holds more — and more overlapping —
    loops than any real (clipped) search returns. Pinned by
    `tests/test_compose.py` (`resample_segments`/`assemble_loop_series`: closed series,
    a→b grid, ramp gain≈loss, missing-segment → n/a, self-loop) and
    `tests/test_compose_live.py::test_compose_looks_up_each_shared_segment_once_not_per_loop`
    (counting provider, cache off: the run requests **exactly** the distinct-segment point
    count, strictly fewer than the per-loop total). **Local DEM remains the recommended
    compose backend** — it's already fast/free, so it gains ~nothing here; this optimization
    helps the API backend (fewer requests, faster, cache-hot re-runs).

- **GPX / GeoJSON export — DONE (2026-06-24).** The "last mile": a hike finder that can't
  hand you a file to load into your phone/GPS was missing the point of finding hikes. New
  pure `export.py` (`hikes_to_gpx`, `hikes_to_geojson`, `hike_to_feature`) serialises the
  matched + composed routes (near-misses included, flagged) to **GPX 1.1** (one `<trk>` per
  hike, one `<trkseg>` per member way, a `<wpt>` at each start) and **GeoJSON** (RFC 7946
  `FeatureCollection` of `MultiLineString`s, stats in `properties`). Geometry now rides on
  the `Hike`: a new `Hike.ways` field is populated in `measure_geometry` from the route's
  **raw member ways** — deliberately NOT the stitched line, so the export keeps every leg
  and matches the reported `distance_km` (the stitched line silently drops unchainable
  members, the same reason `total_way_length_m` sums the ways; see filters.py). Default `()`
  so every prior `Hike` construction and the one-line/`hike_to_dict` output are byte-for-byte
  unchanged. **Coordinate-order is the landmine and is pinned both ways**: GPX writes
  `lat=/lon=` attributes, GeoJSON writes `[lon, lat]`, `hike_to_dict(geometry=True)` writes
  `[lat, lon]` (Leaflet's `L.polyline`) — a known fixture point is asserted onto a known axis
  in `test_export.py`/`test_web.py`. Wired into all three frontends: **CLI** `--gpx FILE` /
  `--geojson FILE` (an *extra* output beside text/`--json`; confirmation to stderr so it never
  pollutes a `--json` pipe; rejected with `--download`; works on live, `--compose-loops`, and
  offline `--area`); **web** `/api/gpx` + `/api/geojson` download endpoints (share one
  `_resolve_hikes` with `/api/hikes`, `Content-Disposition: attachment`) plus Download
  buttons, and `/api/hikes` now carries `geometry` so the map **draws each route line**
  (amber near-miss, dashed-purple composed loop) with no second search; **MCP** a
  `format: "text"|"gpx"|"geojson"` arg on `find_hikes` (empty result still returns the
  helpful text). XML names are `xml.sax`-escaped, files written UTF-8; an empty result yields
  a valid empty `<gpx>` / empty `FeatureCollection`. **Validated end-to-end** on the
  `spindl_area.json` fixture through the real engine (11 routes → 11 GPX tracks / 3281
  trkpts, well-formed XML; first trkpt `lat=50.726 lon=15.607`, GeoJSON `[15.607, 50.726]`).
  A near-miss is marked in the exported `<name>` with the same `~` prefix every frontend
  uses (GPS lists show the name, not the desc; GeoJSON keeps the structured `near_miss`/
  `notes` properties). Pure + frontend tests in `test_export.py` (19 cases), `test_cli.py`,
  `test_web.py`, `test_server.py`, plus a composed-loop `ways` assertion in
  `test_compose_live.py`. **Suite 228**.

- **GPX / GeoJSON export v2 — per-point `<ele>` + single clean track — DONE (2026-06-29).**
  The export v1 "left" item. The gain pass already resamples the walking line and looks up
  an elevation per point, then *discarded* both. Now `filters.add_elevation` keeps them as a
  new `Hike.track` — the resampled walking-order line zipped with its sampled elevations as
  `(lat, lon, ele)`. The export prefers it when present: **GPX** emits ONE `<trkseg>` with an
  `<ele>` on every `<trkpt>` (the "single clean track", walking order), **GeoJSON** emits one
  3D `[lon, lat, ele]` line (RFC 7946's optional altitude element) — still wrapped as
  `MultiLineString` so the geometry *type* never varies between hikes. With no track it falls
  back to the v1 raw-`ways` multi-segment export unchanged.
  - **Honesty gate (the load-bearing decision):** the track is sampled along the *stitched*
    line, which `stitch_ways` builds by dropping members it can't chain — so on a branched /
    gap-split relation a track would silently omit whole legs (the very thing the v1 export
    avoids by reading raw `ways`). So the track is recorded ONLY when the stitch is *faithful*:
    `_stitch_is_faithful` checks `polyline_length_m(line) >= total_way_length_m(ways)*(1-2%)`.
    Clean linear routes (the common case — 13/15 live) pass comfortably; the fragmented
    relations that recover length via `total_way_length_m` (36/70, 19/31 members dropped) fail
    it and keep the full-geometry raw-`ways` export (no `<ele>`). Gain/loss are unaffected
    either way (still computed from the partial line, exactly as before). A route whose
    elevation lookup fails gets no track (and no gain), as before.
  - **Composed loops covered too:** a composed loop is a single synthesised ring (faithful by
    construction). `search.compose_loops` already assembles the per-segment elevation series;
    it now also assembles the matching *points* (`assemble_loop_series(graph, loop, seg_points)`)
    and passes them via the new `find_hikes(pre_points_by_id=…)` → `add_elevation(pre_points=…,
    use_presampled=True)` hook, so the presampled path builds the track without re-touching the
    provider and without the faithfulness gate. Absent the points, gain is unaffected and only
    the track is skipped (back-compat).
  - **Zero churn elsewhere:** `track` defaults `()` and is left out of `hike_to_dict` /
    `format_hike` (like `ways`), so every other frontend output is byte-for-byte unchanged.
    The web map still draws from `ways` (2D Leaflet). Built deterministically from the same
    resample + elevations as gain, so the **offline==live byte-for-byte invariant extends to
    the track** (pinned by `test_snapshot.py::test_offline_search_track_matches_online`).
  - **Validated on the live fixture (matches v1's bar).** `test_track_live.py` drives all
    **15 real routes** of `spindl_area.json` (one Overpass round-trip) through
    `measure_geometry` + `add_elevation` with a deterministic ramp (the gate is geometry-only)
    and pins the discriminator: a per-point track exists IFF the stitch is faithful. Result
    matches the HANDOFF's earlier closure/distance finding **exactly** — **13 clean routes get
    a single elevated `<trkseg>` / `<ele>` + GeoJSON 3D**, and the gate falls back to the
    full raw-`ways` export (no `<ele>`, GeoJSON 2D) on **precisely the two fragmented
    relations**: the route named "4207" (id 237097, stitched/summed 7.54/18.28 km = 0.41) and
    "Medvědí okruh" (id 6285306, 3.37/7.98 = 0.42). The fragmented fallback keeps full
    geometry (≥2 way segments) and still reports gain. Unit coverage: `test_track.py` (6:
    direct-path aligned track, gate drops the track on a member-dropping stitch while keeping
    gain, lookup-failure no-op, presampled track from supplied points / no-points / degraded
    series), `test_export.py` (+3: GPX single elevated trkseg, track-wins-over-raw-ways,
    GeoJSON 3D line keeping the MultiLineString type), `test_snapshot.py` (+1 offline==live
    track byte-identical), `test_web.py` (offline GeoJSON download now carries 3D coords).
    **Suite 285** (282 pass; the 3 `.sh` launcher cases still need bash).

- **Repo hygiene — DONE and CI-GREEN (2026-06-24).** The public repo now carries
  the basics a serious project needs: an **MIT `LICENSE`**, a **`CHANGELOG.md`**
  (Keep-a-Changelog, documents the 0.1.0 feature set), and **GitHub Actions CI**
  (`.github/workflows/ci.yml`) that runs the suite on every push + PR. The CI
  matrix is shaped to the suite's optional-dep guards: a **Linux matrix
  (3.10–3.13)** installs *all* extras (`.[dev,local-dem]` → pytest + mcp + rasterio)
  so the `importorskip`-guarded suites (`test_server`, `test_local_dem`) actually
  run, plus a **windows-latest** smoke job (`.[dev]`, rasterio omitted) that
  exercises the native `.ps1` launchers. **First run was red** and taught two real
  things: (1) `ubuntu-latest` ships `pwsh`, so the `.ps1` launcher tests run on
  Linux too (they passed — the wrappers are clean cross-platform PS); (2) a latent
  test bug — `test_local_dem.py` did `import numpy` *before* its rasterio
  `importorskip`, so the Windows job (no local-dem extra → no transitive numpy)
  errored on collection instead of skipping. Fixed by guarding numpy with
  `importorskip` too (it's not a declared dep, only arrives via rasterio). Second
  run **all green** (4 Linux + 1 Windows). `pyproject.toml` packaging metadata is
  now complete for an eventual PyPI publish: SPDX `license = "MIT"` + `license-files`
  (bumped `setuptools>=77`), `authors`, `readme`, `keywords`, classifiers (Py
  3.10–3.13), and project URLs — validated through the setuptools backend
  (`License-Expression: MIT`, `License-File: LICENSE`). README carries CI + license
  badges. Not done (deliberately): the actual PyPI publish (needs an account +
  token) and a `v0.1.0` git tag (the CHANGELOG's release link is dead until tagged).

## Conventions

- Pure math stays network-free and tested. Keep it that way — it's the trust
  anchor. Any new measurement logic gets a unit test.
- Coordinates are `(lat, lon)` tuples everywhere. Don't flip them; Overpass and
  rasterio disagree on order, and the seams are already handled in their modules.
- Config is env-driven (`config.py`). Don't hardcode endpoints in logic modules.

## Quick commands

```bash
pip install -e .             # CLI + web UI (no LLM); extras: ".[mcp]" ".[local-dem]" ".[dev]"
pytest -q                    # 186 tests, all offline (pure math + Overpass parser + CLI + MCP server (incl. a real-stdio subprocess) + elevation API + daily quota + transparent cache + local-DEM synthetic tiles + loop composition (synthetic graphs + Špindl fixture) + live closure & coupling fixtures + launcher scripts); 183 pass on a WSL-less box (the 3 `.sh` launcher cases need bash); MCP tests skip without the `mcp` extra
hike-finder --bbox 50.72 15.58 50.74 15.62 --user-agent you@example.com
hike-finder --clear-cache    # empty the on-disk cache; --no-cache bypasses it for a run
hike-finder-web              # local web UI on http://127.0.0.1:8765
hike-finder-mcp              # MCP server over stdio (needs the `mcp` extra)

# Launcher scripts (one per interface; set a default HIKE_OVERPASS_UA, forward args):
./scripts/cli.sh ... | .\scripts\cli.ps1 ...     # -> hike-finder
./scripts/web.sh     | .\scripts\web.ps1         # -> hike-finder-web
./scripts/mcp.sh     | .\scripts\mcp.ps1         # -> hike-finder-mcp (stdout kept clean for JSON-RPC)
```
