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
  closure by *endpoint degree* (stitch-order independent — a loop has no
  odd-degree endpoint); else the stitched line returning within `HIKE_LOOP_TOLERANCE`
  of its start. High confidence.
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

- `geometry.py` — haversine distance, polyline length, way stitching (with
  endpoint matching + flipping), distance-based resampling. `tests/test_geometry.py`.
- `elevation/gain.py` — moving-average smoothing + hysteresis-threshold gain/loss.
  `tests/test_gain.py`. Verified: rejects pure noise, captures gradual climbs,
  symmetric up/down, and does NOT overcount under heavy sawtooth noise.
- `access.py` — circular detection (closed loop / open path / loop-with-spur),
  car & chairlift proximity (just-inside vs just-outside a radius), and tri-state
  `Criteria` acceptance. `tests/test_access.py`.
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
  and the cross-request throttle. `tests/test_api.py` (mocks `requests.post`, so
  offline). These tests exist *because* the body-format bug below shipped untested.

Run it: `pytest` → 41 passing.

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

- `elevation/api.py` (the **API elevation backend**) — validated against
  OpenTopoData `srtm30m`. CLI run on the Špindlerův Mlýn bbox now returns a
  computed gain/loss for **every** route (0 nulls); the gain math tracks the
  profile (e.g. 740 m elevation spread → +765 m gain). **Two bugs found & fixed
  here:**
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
3. **Robust way-stitching — the real next quality issue (surfaced during the
   elevation validation).** `geometry.stitch_ways` is greedy and order-dependent,
   so some relations stitch to a **2-point stub** instead of the full line — those
   routes then report an endpoint-only gain (e.g. `gain 0 / loss 354`, and the
   short town loop as `0 / 0`). Distance and gain are only as good as the stitch.
   Fix: build an endpoint graph and extract the longest path / proper loop (see
   "Known limitations" below). This now gates gain *accuracy* per route, not just
   presence.
4. **Wire MCP end-to-end** and call `find_hikes` from Claude Code.
5. **Then** add the local DEM path and the polish items below.

## Known limitations / TODOs (design notes, not bugs)

- **Gain threshold vs noise (important):** the threshold must exceed the
  *peak-to-peak* noise amplitude, not half of it. A unit test caught this — ±5 m
  jitter is 10 m peak-to-peak and a 10 m threshold sits exactly on the boundary.
  Use threshold > peak-to-peak, and lean on smoothing. Tune per elevation source
  (API data is pre-smoothed; raw SRTM is noisier → higher threshold).
- **Way stitching is greedy** with a 30 m endpoint tolerance. Fine for simple
  linear routes; multi-branch relations or loops with spurs may stitch oddly.
  Robust fix: order members by the relation's role/sequence, or build a graph and
  extract the longest path. Until then, distance is reliable; stitched *order*
  may not be for complex relations.
- **Local DEM merges tiles in memory.** For large regions, switch to a GDAL VRT
  over `dem_dir` (`gdalbuildvrt`) and sample the VRT — avoids loading everything.
- **No caching.** Overpass and elevation results should be cached (disk/SQLite)
  to respect usage policies and speed up repeat queries. Add before heavy use.
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
- **Loop-with-spur:** the endpoint-degree test reports a loop with a dead-end spur
  as non-circular (the spur tip is odd-degree). A `roundtrip=yes` tag still wins.
  Acceptable; same family as the greedy-stitch caveat above.
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
pytest -q                    # 41 tests, all offline (pure math + Overpass parser + CLI + elevation API)
hike-finder --bbox 50.72 15.58 50.74 15.62 --user-agent you@example.com
hike-finder-web              # local web UI on http://127.0.0.1:8765
hike-finder-mcp              # MCP server over stdio (needs the `mcp` extra)
```
