# HANDOFF — hike-finder-mcp

Orientation for anyone (human or agent) picking up the project: what it is, how it's
built, what's done, and the design caveats that aren't obvious from the code. For the
release-by-release feature history see `CHANGELOG.md`; for user docs see `README.md`
(reference) and `GUIDE.md` (walkthrough).

## Goal in one sentence

Replace "search the web and trust whatever gain number a trail site printed" with "query
OpenStreetMap for marked routes and compute gain/distance ourselves" — exposed as a CLI, a
local web UI, and an MCP tool `find_hikes(bbox, gain range, distance range, circular?,
car_access?, chairlift_access?)`.

## The user's context (don't lose this)

- They plan hikes with **mapy.cz** and specifically want **OSM-based** data, not AllTrails'
  proprietary data. That's why we go to Overpass for route relations, not a trail-site API.
  The KČT trail markings they rely on live in OSM tags.
- They explicitly asked for **both** elevation backends (API *and* local DEM), selectable —
  `mode = api | local | auto`.
- AllTrails / Felt / TomTom MCP connectors were offered and **declined** in favour of building
  this. Don't reach for them.

## Architecture

The pipeline is deliberately **two-pass**: everything cheap (geometry + access) runs first and
filters the candidate set; the expensive elevation lookup runs *only on the survivors*. That's
what keeps the elevation API from being hammered.

**Three frontends, one engine.** The tool runs standalone — no LLM required. All three
frontends build the same `Criteria` and call `search.search_hikes`, then render via
`format.format_hike` / `hike_to_dict`, so results are identical:

- `cli.py` → `hike-finder` — primary console script (argparse). No LLM/MCP.
- `web.py` → `hike-finder-web` — local web UI, stdlib `http.server` + a Leaflet map you pan to
  pick the bbox. No LLM/MCP, no web framework.
- `server.py` → `hike-finder-mcp` — MCP over stdio, for LLM clients. `mcp` is an **optional**
  extra (`pip install -e ".[mcp]"`); the base install omits it. (Breaking: the `hike-finder`
  command used to launch the MCP server — that moved to `hike-finder-mcp`.)

Each frontend also has a **thin launcher** in `scripts/` (`cli`/`web`/`mcp`, `.sh` + `.ps1`):
it sets a default `HIKE_OVERPASS_UA` (only if unset) and forwards args — no logic, so it can't
drift. The MCP launcher writes NOTHING to stdout (that's the JSON-RPC channel). `.gitattributes`
pins `*.sh` to LF so bash launchers survive a Windows (`autocrlf=true`) checkout.

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
            │    ├─ geometry._vertex_graph → route_cycle_count / route_termini  [PURE, TESTED]
            │    ├─ geometry.total_way_length_m    # distance = sum of member ways [PURE, TESTED]
            │    └─ access.is_circular / car_accessible / chairlift_access [PURE, TESTED]
            │  → apply over-length guard + distance/shape/access filters
            └─ EXPENSIVE pass (survivors only) → filters.add_elevation(hike, line)
                 ├─ geometry.resample_by_distance  # even spacing      [PURE, TESTED]
                 ├─ elevation.lookup(points)       # api/local/auto    [NETWORK/DISK; API CACHED, no TTL]
                 └─ elevation.cumulative_gain_loss # smoothing+thresh  [PURE, TESTED]
               → apply gain filter, sort
  → results rendered by format.format_hike / format.hike_to_dict (shared)
```

**The three filters on top of gain/distance** (all tri-state in `Criteria`: None = don't care,
True = require, False = exclude):

- **`circular`** — `access.is_circular`. The OSM `roundtrip` tag is authoritative; else closure
  by *circuit rank* `E − V + C` over the **full vertex graph** (`geometry.route_cycle_count`,
  stitch-order independent, counts a lollipop, detects T-junction closures because nodes are
  exact shared vertices, and does NOT invent cycles from clustered endpoints); else the stitched
  line returning within `HIKE_LOOP_TOLERANCE` of its start (a loop left open by a digitization gap).
- **`car_access`** — `access.car_accessible`. A mapped `amenity=parking` within `HIKE_CAR_RADIUS`
  of a trail terminus (parking-only by design). Best-effort.
- **`chairlift_access`** — `access.chairlift_access`. A ride-up aerialway
  (`chair_lift`/`gondola`/`cable_car`/`mixed_lift`; drag/T-bar excluded) within `HIKE_LIFT_RADIUS`.
  Best-effort; the lift type is reported.

The **over-length guard** (`HIKE_MAX_ROUTE_FACTOR` × bbox diagonal) drops routes longer than N×
the bbox — a through-route (national trail) that merely crosses the area would otherwise report
a 200 km "hike" and test access at endpoints in another region.

## Search modes

Entry points on the shared engine, all rendering identically:

- `search.search_hikes` — the live search (Overpass + elevation).
- `search.download_area` / `search.search_snapshot` — **offline snapshots**: `download_area`
  fetches Overpass once and warms elevation for every geometry-plausible route, saving a JSON
  snapshot; `search_snapshot` searches it with **zero network** by swapping two seams (saved
  `AreaData` for `fetch_area`, `SnapshotElevationProvider` for the API) → offline == online *by
  construction*, not a parallel path. Snapshots also bake reverse-geocoded place names.
- `search.compose_loops` — **loop composition**: builds one trail-network graph from every
  relation's member ways, finds cycles of a target length, and wraps each as a synthetic
  `roundtrip=yes` route through the *unchanged* `find_hikes`. Not folded into `circular=true`.
- `search.compose_loops_around` — **circular routes near a point** (`--around`, MCP
  `circular_routes`): the same loop engine, but with the picked point as a compose *anchor*
  (only loops within `around_radius_m` survive, started there) and a **point-derived bbox**
  (`radius + max-loop/2`, provably non-clipping). Shares `_compose_from_graph` with
  `compose_loops`; the length band is a *length* constraint, not a spatial one.
- `search.routes_between` — **N shortest routes between two points** (`--from`/`--to`, MCP
  `routes_between`): Yen's k-shortest-loopless-paths (`compose.k_shortest_paths`) on the
  junction **multigraph** (edges removed by *segment id*, so parallel trails survive), with each
  point snapped by **splitting the nearest segment** at the projected spot (`compose.snap_points`).
  Assembled routes reuse `_assemble` (an open path is just an ordered segment list) and are
  measured through the shared `_measure_composed` (the same per-segment shared-elevation block
  `compose_loops` uses). An overlap filter yields N *distinct* routes; a >2 km snap is rejected.

**Near-misses** (`find_hikes(near_miss="auto")`, the frontend default) surface close-but-not-
matching routes only when there are 0 strict matches, annotated with the literal gap. Shape is
never relaxed and excluded access stays strict.

## What is DONE and validated

The core is unit-tested (pure math is the trust anchor and stays network-free) and the whole
thing is validated live against real OSM. Highlights:

- **Geometry / closure / distance / termini** — all off one shared `geometry._vertex_graph`.
  Closure and distance are live-validated on real CZ relations (`tests/fixtures/medved_relations.json`,
  `spindl_area.json`).
- **Elevation** — both backends trustworthy (a detected closed loop reads gain≈loss). API
  backend: per-endpoint request dialect, cross-request throttle, retry/backoff with a
  `Retry-After` ceiling, and a persistent cross-process daily-quota counter. Local DEM: Copernicus
  GLO-30 tiles mosaicked via a hand-built GDAL VRT (memory-flat), point-sampled. Local DEM is the
  recommended backend (fast, never hits the quota).
- **Transparent SQLite cache** at the Overpass + API-elevation seams, on by default,
  failure-isolated.
- **Offline snapshots + near-misses**, **loop composition** (incl. access-anchoring, segment-
  shared elevation sampling, sliver filter), **reverse-geocode naming** of unnamed routes, and
  **GPX/GeoJSON export** (per-point elevation on a single clean track) — all live across all three
  frontends. See `CHANGELOG.md` for the per-release breakdown.
- **Point-based route drawing** (`--around` / `--from`/`--to`) — the pure engine (mid-segment
  snapping + Yen on the junction multigraph) is unit-tested on hand-built graphs
  (`test_routing.py`) and offline end-to-end through the full search stack on the Špindl fixture
  (`test_routing_live.py`, incl. a bbox-derivation spy so a lat/lon swap can't slip past).
  **User-verify-pending:** the live Overpass + elevation paths for these two modes haven't been
  run against the network yet (no UA/DEM configured in this session) — smoke-test one real
  `--from/--to` and one `--around` before trusting them, per the same convention the MCP/web live
  paths follow.
- **All three frontends validated live**, including the MCP server over real stdio.
- **Repo hygiene**: MIT license, CHANGELOG, green CI (Linux 3.10–3.14 + Windows), complete
  pyproject; v0.1.0 and v0.2.0 tagged + GitHub-released.

Run it: `pytest -q` — the full suite is offline (a few `.sh` launcher cases need bash; MCP tests
skip without the `mcp` extra).

## Known limitations / TODOs (design notes, not bugs)

- **Gain threshold vs noise:** the threshold must exceed the *peak-to-peak* noise amplitude, not
  half of it (±5 m jitter = 10 m peak-to-peak; a 10 m threshold sits on the boundary). Tune per
  source — API data is pre-smoothed; raw SRTM/GLO-30 is noisier and wants a higher threshold.
  Don't tune to a single route (overfitting); the defaults (10 m / 25 m) are validated.
- **Way stitching is greedy** (30 m endpoint tolerance) and silently drops members it can't
  chain. Distance and termini no longer ride on it (they use the vertex graph / member-way sum);
  only the benign `is_circular` gap fallback and a loop `start` fallback still do. GPX/GeoJSON
  export exposes this via a faithfulness gate (per-point `<ele>` track only when the stitched line
  recovers ≥98% of the summed length; else full raw-ways export, no `<ele>`).
- **Closure digitization gap:** closure welds at `weld_m≈1 m`, so a loop left open by a gap wider
  than that reads as open in `route_cycle_count` — the `HIKE_LOOP_TOLERANCE` (150 m) start≈end
  fallback is the backstop, and `roundtrip=yes` always wins.
- **Access is best-effort, not ground truth.** `car_access=False`/`chairlift_access=False` mean
  "nothing of that kind is *mapped* in OSM near the route," not "you can't get there." Keep the
  output honest if you change it. Loop detection, by contrast, is reliable.
- **Car access is parking-only** (roads are dense and tag-fragile). If recall is too low, add
  drivable-highway *nodes* near termini as a second signal — not all road geometry.
- **Access is measured at termini for point-to-point routes, along the whole line for loops** (a
  loop has no meaningful "end"). The `start` marker stays coupled to termini only, so a pure
  loop's start stays at the arbitrary head. An exact radius-padded-bbox pre-filter
  (`access._bbox_pad`) keeps the whole-line scan cheap.
- **Over-length guard is a heuristic, not bbox-clipping.** It drops through-routes cheaply but
  can also drop a genuinely long loop in a small bbox, and it doesn't *clip* a route to the area
  (distance is still the whole stitched line). True member-way bbox-clipping is deliberately
  deferred (and provably a no-op for *composed* loops — a boundary-clipped trail ends degree-1 and
  can't lie on a cycle).
- **Loops are genuinely sparse in raw data** (~1 of 12 around Špindl): most KČT relations are
  linear A→B segments. That's what loop composition addresses.
- **`routes_between` fetches a corridor, not the whole plane.** The area is the two points'
  bounding box padded `max(HIKE_ROUTES_PAD_KM, HIKE_ROUTES_PAD_FRAC×separation)` (2 km / 0.4), then
  `clip_routes_to_bbox` drops the rest. The *shortest* route stays in-corridor, but a longer
  *alternative* that bows well outside the pad gets clipped — so "N shortest" can under-deliver a
  wide detour. `--max-distance` caps a route's length but does **not** widen the fetch; raise the
  pad knobs for that. `--around` similarly fetches `radius + max-loop/2` (a 15 km band → ~17 km
  Overpass box, a heavy query). Both are point-derived-bbox trade-offs, not bugs.
- **Round-trip vs point-to-point gain:** we report cumulative gain over the line as-is; `loss`
  gives the reverse direction's gain.
- **Daily quota** assumes a UTC-midnight reset and can lose an update under a cross-*process*
  race (acceptable for a soft advisory limit; no file locking).
- **PyPI publish** is deliberately parked — GitHub-only for now. Metadata is publish-ready; the
  clean path when revisited is Trusted Publishing (OIDC) via a tag-triggered workflow.

## Conventions

- Pure math stays network-free and tested — it's the trust anchor. Any new measurement logic gets
  a unit test.
- Coordinates are `(lat, lon)` tuples everywhere. Don't flip them; Overpass and rasterio disagree
  on order, and the seams are already handled in their modules. Export pins the axis both ways.
- Config is env-driven (`config.py`), read at `load()` not import. Don't hardcode endpoints in
  logic modules.
- Guard optional-extra deps (rasterio, mcp, numpy) behind `importorskip` in tests — don't
  bare-import them, or a base-install env errors on collection instead of skipping.

## Quick commands

```bash
pip install -e .             # CLI + web UI (no LLM); extras: ".[mcp]" ".[local-dem]" ".[dev]"
pytest -q                    # full offline suite (3 .sh launcher cases need bash; MCP skips without the extra)
hike-finder --bbox 50.72 15.58 50.74 15.62 --user-agent you@example.com
hike-finder --clear-cache    # empty the on-disk cache; --no-cache bypasses it for a run
hike-finder-web              # local web UI on http://127.0.0.1:8765
hike-finder-mcp              # MCP server over stdio (needs the `mcp` extra)

# Launcher scripts (one per interface; set a default HIKE_OVERPASS_UA, forward args):
./scripts/cli.sh ...  | .\scripts\cli.ps1 ...    # -> hike-finder
./scripts/web.sh      | .\scripts\web.ps1        # -> hike-finder-web
./scripts/mcp.sh      | .\scripts\mcp.ps1        # -> hike-finder-mcp (stdout kept clean for JSON-RPC)
```
