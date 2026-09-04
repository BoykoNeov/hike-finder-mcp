# HANDOFF — hike-finder-mcp

Orientation for anyone (human or agent) picking up the project: what it is, how it's
built, what's done, and the design caveats that aren't obvious from the code. For the
release-by-release feature history see `CHANGELOG.md`; for user docs see `README.md`
(reference) and `GUIDE.md` (walkthrough).

## Goal in one sentence

Replace "search the web and trust whatever gain number a trail site printed" with "query
OpenStreetMap for marked routes and compute gain/distance ourselves" — exposed as a CLI, a
local web UI, and an MCP tool `find_hikes(bbox, gain range, distance range, circular?,
car_access?, chairlift_access?, transit_access?)`.

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
       ├─ overpass.fetch_area(bbox)          # routes + parking + lifts + transit + POIs [NETWORK, CACHED (TTL)]
       │    └─ overpass.parse_area(elements) # split mixed response      [PURE, TESTED]
       ├─ elevation.get_provider(mode)       # api | local | auto        [NETWORK/DISK]
       └─ filters.find_hikes(area, elevation, criteria, bbox)
            ├─ poi.PoiIndex(area.pois)        # ONE grid per search, if a POI filter is set
            ├─ CHEAP pass  → filters.measure_geometry(route, parking, lifts, poi_index)
            │    ├─ geometry._vertex_graph → route_cycle_count / route_termini  [PURE, TESTED]
            │    ├─ geometry.total_way_length_m    # distance = sum of member ways [PURE, TESTED]
            │    ├─ access.is_circular / car_accessible / chairlift_access / transit_access [PURE, TESTED]
            │    └─ poi.route_pois            # which churches/ruins/peaks it passes [PURE, TESTED]
            │  → apply over-length guard + distance/shape/access/POI filters
            └─ EXPENSIVE pass (survivors only) → filters.add_elevation(hike, line)
                 ├─ geometry.resample_by_distance  # even spacing      [PURE, TESTED]
                 ├─ elevation.lookup(points)       # api/local/auto    [NETWORK/DISK; API CACHED, no TTL]
                 └─ elevation.cumulative_gain_loss # smoothing+thresh  [PURE, TESTED]
               → apply gain filter, sort
  → results rendered by format.format_hike / format.hike_to_dict (shared)
```

**The four filters on top of gain/distance** (all tri-state in `Criteria`: None = don't care,
True = require, False = exclude):

- **`circular`** — `access.is_circular`. The OSM `roundtrip` tag is authoritative; else closure
  by *circuit rank* `E − V + C` over the **full vertex graph** (`geometry.route_cycle_count`,
  stitch-order independent, counts a lollipop, detects T-junction closures because nodes are
  exact shared vertices, and does NOT invent cycles from clustered endpoints); else the stitched
  line returning within `access.closure_limit_m` of its start (a loop left open by a digitization
  gap) — `HIKE_LOOP_TOLERANCE` capping a 5 %-of-route-length bound, shared with the gain gate so
  the label and the gain cannot disagree about one geometry.
- **`car_access`** — `access.car_accessible`. A mapped `amenity=parking` within `HIKE_CAR_RADIUS`
  of a trail terminus (parking-only by design). Best-effort.
- **`chairlift_access`** — `access.chairlift_access`. A ride-up aerialway
  (`chair_lift`/`gondola`/`cable_car`/`mixed_lift`; drag/T-bar excluded) within `HIKE_LIFT_RADIUS`.
  Best-effort; the lift type is reported.
- **`transit_access`** — `access.transit_access`. "Can I get here without a car?" A mapped
  `railway=station|halt|tram_stop` or `highway=bus_stop` near an endpoint, driven by the
  `access.TRANSIT_KINDS` registry (one table → both the Overpass selectors and the classifier,
  the `poi.POI_KINDS` pattern). `railway=halt` is the load-bearing entry: a Czech trailhead is
  far more often a request-stop *zastávka* than a full station. Three design points:
  - **Two radii, not one** (`HIKE_TRANSIT_RAIL_RADIUS` 1000 m, `HIKE_TRANSIT_STOP_RADIUS`
    400 m). `highway=bus_stop` is mapped along nearly every rural road, so a single generous
    radius makes `transit_access=True` almost free and the filter stops discriminating.
  - **Rail beats road when both qualify**, rather than `chairlift_access`'s nearest-wins (all
    of whose candidates are one family). A bus stop at 50 m is not the more useful fact when a
    station sits at 300 m. Nearest still wins *within* a class.
  - **It is the only `bool | None` access field**, and that is the honesty-critical part —
    see the tri-state note under Known limitations.
  Live-validated over Adršpach/Teplice: 23 routes in the box, 16 with transit and 7 without —
  an exact partition, so the filter genuinely discriminates. The top hit is a route OSM itself
  names `… - Teplice nad Metují žst.` (*žst.* = railway station), which our independent
  geometry measurement agreed reaches a train halt.

**The destination filter** (`criteria.poi_kinds`, `poi.py`) is the fifth, and the only one
that is a *list* rather than tri-state: empty = don't care, otherwise keep routes passing
within `HIKE_POI_RADIUS_M` (250 m) of an object of ANY listed kind. Design points worth
keeping:

- **One registry, two derivations.** `poi.POI_KINDS` is the single source of truth for both
  the Overpass selectors (`overpass._poi_clauses` reads `poi.selectors_by_key`) and the
  classifier (`poi.classify`). Written independently they would drift, and a kind that is
  *fetchable but unclassifiable* fails as a silently-empty result set, not an error — the
  same hazard `access.matched_access_points` and the shared `_vertex_graph` exist to remove.
  `test_poi.py` pins the round-trip and pins that `build_query` still contains the clauses.
  The one thing fetched but deliberately not classified is a kind's `exclude` deny-list
  (see Known limitations); the round-trip test cannot see it, so it has its own pins.
- **`require` is the mirror of `exclude`, and inverts every one of its rules.** A kind can
  demand a secondary tag (`tree` = `natural=tree` **and** a `name`). Four differences, each
  load-bearing: (1) a **missing** required tag DOES disqualify, reversing the "not recorded
  ≠ no" rule `exclude` and `transit_access` follow — a requirement exists because the
  primary tag is too broad to be usable, so keeping the untagged makes the kind worthless
  rather than merely imprecise; (2) it **must reach the query**, where a deny-list must not
  — that is the whole point of it, and it therefore **does** invalidate the Overpass cache;
  (3) a required kind gets its **own clause** and is **excluded from the merged regex for
  its key**, or the merged clause would fetch the broad tag anyway and defeat the exercise
  (`selectors_by_key` and `required_selectors` partition the registry; `test_poi.py` pins
  that they do, because a kind in *both* would still pass the round-trip); (4) an empty
  `values` tuple means **presence**, tested as `key in tags` rather than truthiness, so it
  matches Overpass's bare `["name"]` exactly — including `name=`, which the query fetches
  and the classifier must therefore accept.
  The numbers that justify it: `natural=tree` is **4044** objects over four CZ regions
  against **72** named. Fetching it merged would have multiplied a Český ráj snapshot's POI
  count several-fold to reach 17 destinations.
- **POIs are fetched on EVERY query**, not only when asked for. That keeps one Overpass
  cache key and makes every snapshot able to answer any destination question later; the
  alternative gives two cache keys and snapshots that only sometimes carry POIs, breaking
  offline == online for POI search.
- **The grid is built once per search, over the POIs** (`poi.PoiIndex`) — they belong to the
  fetched area and don't change while routes are iterated. Indexing each route's points
  instead would rebuild the structure per route for nothing. Longitude cells use the
  worst-case (highest-|lat|) cosine, like `access._bbox_pad`, plus a 5 % margin, and `near()`
  re-derives its column span from the query point's own latitude — so a query poleward of
  everything indexed widens instead of silently missing. `test_poi.py` pins it against brute
  force. (`_M_PER_DEG_LAT` is derived from `geometry.EARTH_RADIUS_M`, *not* the 111 320 used
  for bbox padding: the grid must agree with the metric its results are checked against, or
  cells come out ~0.1 % small and a POI can hide in the sliver.)
- **Proximity is to the LINE, not the vertices.** `poi.route_pois` walks each member way in
  probe steps of one radius, over-collects candidates from the grid at 1.5×, then measures
  each exactly with `geometry.project_on_polyline` (shared with `compose._project_point`, so
  "nearest point on the trail" means one thing project-wide). Vertex-only proximity was tried
  first and is wrong: OSM maps a straight stretch with two nodes, so a church at the midpoint
  of a 5 km member reads as 2.8 km away.
- **Passing, not terminating.** A marked KČT relation almost never *ends* at a church — it
  passes it — so an end-anchored filter would return near-nothing and read as broken. The
  measured distance rides on every hit, so "ends at" stays readable from the output.
- **It lands in the CHEAP pass**, so a POI-filtered search spends *less* elevation budget than
  the same search without it: routes reaching nothing are dropped before anyone pays for their
  gain profile. Pinned by `test_poi.py`.
- **Listing (`poi.select_pois`) is a different shape from matching, on purpose.** Three
  decisions worth not re-litigating: (1) it returns a `PoiPlace`, **not** a `PoiHit` with
  `distance_m=0` — there is no route to measure from, and a zero renders "(0 m)", which reads
  as *on the trail*; both types render kinds through the one `poi.kind_label` so they can't
  drift. (2) An empty `kinds` expands to **every** kind, the opposite of `route_pois` where
  `()` means match nothing — the expansion is explicit at the top of the function and pinned,
  because the two readings differ by a whole result set. (3) It is **not clipped to the
  bbox**: a large object's `out center` representative point can land just outside a box it
  genuinely intersects, and dropping a real object is the silent failure this project forbids
  while over-showing one is visible on a map. `routes_to_poi` reads `area.pois` unclipped for
  the same reason.

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
- `search.route_via` — **one route linking several points** (`--via`, repeatable, `--via-loop`,
  MCP `route_via`): snaps ≥2 picked points in a single `snap_points` call, then chains
  `compose._dijkstra` between consecutive pairs (in the given order — no reorder) and `_assemble`s
  the concatenated ordered-seg list. `--via-loop` adds a closing leg to point 1 and routes each leg
  with `removed_edges = union(earlier legs' seg_ids)` → an **edge-disjoint non-retracing loop**
  where the network allows; a leg with no disjoint alternative falls back to a plain `_dijkstra`
  (forced retrace). The retraced fraction is logged; ≥50 % on a loop is flagged as a largely
  out-and-back. Reuses the `routes_pad`/`routes_max_snap` guards and `_measure_composed`; a
  segment repeated in `ordered_segs` measures correctly because `assemble_loop_series` walks the
  sequence per-occurrence.

- `search.list_area_pois` / `search.list_snapshot_pois` — **the inventory** (`--show-pois`, MCP
  `list_pois`, web "Show points of interest (no routes)"): every registered object of the chosen
  kinds in an area, with **no route drawn to any of them**. The odd one out among the modes and
  deliberately so — it returns `poi.PoiPlace` objects, not `Hike`s, and touches neither the trail
  graph nor an elevation provider (one Overpass call, nothing spent from the daily quota). Both
  entry points funnel into the pure `poi.select_pois`, which is where offline == live comes from
  here: it is a *shared call*, not two paths that agree. Live and offline are both first-class —
  unlike the routing modes, this one is happy to read a saved snapshot, which is the whole point
  of "only in the downloaded area". Exports through `export.pois_to_gpx` / `pois_to_geojson` as
  **waypoints**, not tracks.

**Near-misses** (`find_hikes(near_miss="auto")`, the frontend default) surface close-but-not-
matching routes only when there are 0 strict matches, annotated with the literal gap. Shape is
never relaxed and excluded access stays strict.

## What is DONE and validated

The core is unit-tested (pure math is the trust anchor and stays network-free) and the whole
thing is validated live against real OSM. Highlights:

- **Geometry / closure / distance / termini** — all off one shared `geometry._vertex_graph`.
  Closure and distance are live-validated on real CZ relations (`tests/fixtures/medved_relations.json`,
  `spindl_area.json`).
- **The "how close is closed" bound is scaled by route length, and lives in ONE place.**
  This file used to carry `is_circular`'s absolute 150 m fallback as the next candidate;
  it is done. `access.closure_limit_m` returns `min(tol_m, rel_tol × distance)` (150 m /
  5 %) and is called by both `access.is_circular`'s start≈end fallback (the LABEL) and
  `filters._line_closes` (the gain gate). Five things worth keeping:
  - **The two halves cross at exactly 3 km** (`5 % × 3 km = 150 m`), so the scaling bites
    *only* on routes shorter than that and everything longer keeps the old behaviour to
    the metre. That bounds the blast radius precisely, and it also disposes of the worry
    about fragmented relations: where the summed member length dwarfs the stitched line,
    5 % of the sum already exceeds 150 m, `tol_m` caps it, and nothing changes. Re-run
    over both fixtures — all 23 relations returned an unchanged verdict.
  - **The bug was two rules disagreeing about one geometry, not a loose constant.**
    `[M] Labský vodopád` (0.1 km, line ending 69 m from its start) read "loop, gain n/a":
    the gain gate had already learned to scale and refused, while the label — written with
    the same 150 m constant and never updated — still said loop. Sharing the function is
    the fix; a matching constant in two files is what produced the drift in the first
    place. A route labelled a loop via the end-gap fallback now always has a gain, pinned
    across the disagreement window by `test_the_label_and_the_gain_gate_read_the_same_bound`.
  - **The first draft of the fix reintroduced the defect, and the near miss is the
    reusable part.** `_line_closes` kept its own `rel_tol: float = 0.05` and forwarded it,
    so the shared function's fraction was dead on that path and changing it would have
    moved the label alone — the same two-copies-of-one-number arrangement, one refactor
    later. Extracting a shared function is not enough if callers keep re-declaring its
    defaults. `tol_m` is the instructive contrast and stays a parameter: it traces to
    `HIKE_LOOP_TOLERANCE`, so the caller is reading a real source of truth rather than
    holding a second copy. A duplicate default is invisible to a behaviour test until
    someone changes one of the two, so it is pinned structurally by
    `test_only_one_function_declares_the_fraction`.
  - **`is_circular`'s `distance_km` is a CACHE, not a mode.** Omitted, it derives the
    identical sum from `ways`; `measure_geometry` passes the value it just computed only
    to save the second walk. Do not turn the default into "don't scale" — two callers
    would then disagree about one route. Pinned.
  - **`circular` has four readers, and this touched more than the label.** The filter
    (`Criteria.accepts_*`), the rendered `loop`/`one-way` flag, the gain gate, and — the
    non-obvious one — the access-point set in `measure_geometry`: a loop's parking/lift/
    transit is tested along the WHOLE line, a non-loop's at its two ends. A route that
    stops being called a loop therefore also stops being measured along its length. That
    is the correct reading for a non-loop and the two sets barely differ under 3 km, but
    it is a second behaviour change riding on one flag.
  - **`roundtrip=yes` still short-circuits ahead of all of it**, which is load-bearing
    rather than incidental: `compose_loops` stamps that tag on every synthesised loop and
    plenty of them are short, so a distance-scaled bound reaching them would empty out
    loop composition. Now pinned, which it was not before.
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
- **Route to a point of interest** (`--from` + `--to-poi`, MCP `routes_to_poi`, web "Route to
  the nearest…") — the fourth point-based mode, and the inverse of the `--poi` filter: pick a
  start and a KIND, get routes to the N nearest objects of it, ranked by **trail** distance
  (one `_dijkstra` per candidate, after a crow-flies cheap pass). Unit-tested offline on a
  hand-built graph whose crow-flies-nearest and on-foot-nearest destinations differ
  (`test_routes_to_poi.py`), plus three cases on the real Špindl trail topology
  (`test_routing_live.py`). The superlative is *checked*: crow-flies distance lower-bounds the
  walk, so when the longest route returned exceeds the search radius (or the nearest candidate
  the cheap pass dropped), the mode says the answer is "not provably the nearest" instead of
  leaving the claim standing — pinned by a test where it demonstrably returns the wrong one and
  admits it. **Verified live** over Český ráj — the same area the `--poi` filter and `--show-pois`
  were verified on, so the three POI modes can be cross-checked against each other. From
  Sedmihorky (50.5578, 15.1836, start snapped 32 m), `--to-poi ruins` drew a real route to ruin
  **Radeč** — 3.46 km, +117 m / −34 m, over four named KČT trails, ending 101 m from the object.
  Three properties came out of it that the offline tests could only assert in miniature:
  - **The guard fires, and it also goes quiet.** At the default 3 km radius the 3.46 km route
    exceeded the bound and the mode logged "the nearest *found*, not provably the nearest";
    re-run at `--to-poi-radius 6000` with the same single route now inside the bound, the hedge
    **disappeared**. A warning that never turns off is just noise — this one is load-bearing in
    both directions. Note the log hedges the **whole result set** off the farthest route; it
    makes no per-rank distinction, so a far Nth route re-arms the warning for the 1st as well.
  - **The hedge caught a REAL miss — it is not merely conservative.** Rank 1 is stable: Radeč
    stayed #1 at 3, 6 and 7 km. Rank 3 was not. At 6 km the three were Radeč 3.46 / Kozlov
    (Chlum) 6.66 / **Rotštejn 12.63** km — and the mode warned. At `--to-poi-radius 7000` the
    third became **Nebákov at 10.36 km**, genuinely nearer than Rotštejn. So the warning was
    pointing at an object that really did beat the answer being shown. Nebákov sits 6.28 km
    crow-flies — just outside the 6 km radius, which is precisely the case the bound exists to
    catch. (At 7 km it still fires, still correctly: 10.36 km > 7 km, so a 5th ruin outside the
    radius could in principle beat it. The recursion is honest, not a bug.) All four bounds
    held: crow-flies 1.36 / 4.10 / 4.97 / 6.28 km each came in under its trail distance.
  - **The modes agree on the landscape.** `--show-pois --poi ruins,castle` over the original box
    lists exactly two ruins, Radeč and Rotštejn — the two in-box objects `--to-poi ruins` routed
    to. Widening the *listing* to cover the 6 km radius (`--bbox 50.50 15.09 50.62 15.28`)
    returns four ruins, adding Kozlov (Chlum) at lon 15.126 and Nebákov at lat 50.502 — both
    outside the original box on one axis, and both classified `ruins` by the listing exactly as
    the router classified them. Valdštejn, which the `--poi` verification recorded, is classified
    **castle**, so its absence from a `ruins` search is the registry working, not a dropped
    object. Rotštejn's "20 m" here matches the figure the `--poi` run recorded for it.

  **The second bound is now live too, and it also caught a real miss.** The runs above were
  all bounded by the *search radius*; the **cheap-pass-drop** bound (`_POI_CANDIDATE_FACTOR`×N,
  floor 10) needed a kind denser than Český ráj's ruins. `--to-poi peak` from the same
  Sedmihorky start reaches it immediately: at `--routes 1` the pool is the 10 nearest
  crow-flies summits, the 11th sits **300 m** away, and the mode logged the distinct wording
  "past the 0.3 km **nearest candidate not examined**" — the `bound_m == excluded_crow_m`
  branch, not the radius one. It was right to. That run's answer was *Čertova skála* at
  1.21 km on foot; re-run at `--routes 5` (pool 20) rank 1 becomes an unnamed peak at
  **0.43 km**, nearly three times nearer and sitting in the tail the cheap pass had dropped.
  So the `--routes 1` answer really was not the nearest, and the mode never claimed it was.
  The hedge stays armed at `--routes 5` as well (farthest 1.2 km vs a 0.4 km bound), which is
  the same honest recursion the radius bound shows — nothing outside the pool is ever assumed
  away.
  **And it goes quiet, which is what makes it a certificate rather than noise** — the same
  two-directions test the radius bound had to pass. Density is what flips it: the identical
  `--to-poi peak --routes 1` from Špindlerův Mlýn at `--to-poi-radius 5000` puts **21**
  summits in radius (so the pool of 10 is armed and `excluded_crow_m` = 3627 m is the
  *binding* bound, well inside the 5 km radius) and answers Kozí hřbety at 1.83 km — under
  the bound, so **no hedge is printed at all**. Note the near miss in setting this up: at the
  default 3 km radius the same start has only 6 summits in range, so the pool never fills,
  `excluded_crow_m` is infinite and the *radius* is the binding bound again. A quiet run only
  demonstrates the cheap-pass bound when ≥11 candidates are in radius — count them
  (`--show-pois --poi peak`) before claiming which bound went quiet.
  Both bounds have now fired live, both have been shown to point at something real, and both
  have been shown to switch off; `test_the_cheap_pass_admits_when_it_may_have_dropped_a
  _nearer_one` still pins the branch offline.

  Export round-tripped: 1 track / 143 trkpt / **143 `<ele>`** (the faithfulness gate passed) plus
  a single start `<wpt>`, diacritics intact; the GeoJSON carries a `destination` property and its
  `[lon, lat]` land on the right axes (lon ≈ 15.18, lat ≈ 50.55). The track's last point sits
  101 m from Radeč's own coordinate — the reported gap, independently recomputed from the file.
- **Point-based route drawing** (`--around` / `--from`/`--to` / `--via`) — the pure engine
  (mid-segment snapping + Yen on the junction multigraph, plus chained-Dijkstra `route_via` with
  its non-retracing loop closure) is unit-tested on hand-built graphs (`test_routing.py`,
  `test_route_via.py`) and offline end-to-end through the full search stack on the Špindl fixture
  (`test_routing_live.py`, incl. a bbox-derivation spy so a lat/lon swap can't slip past).
  `--around` and `--from/--to` are also **verified live** against real Overpass + the elevation API
  over Krkonoše: `--around` composed real named loops near the point (gain≈loss), `--from/--to`
  returned the N shortest-first distinct routes (snap distances reported, off-network guard
  respected), and both round-tripped to GPX/GeoJSON with per-point elevation. **`--via`/`--via-loop`
  is now also verified live** over Krkonoše: an open `--via` linked 3 Špindlerův Mlýn points into a
  5.4 km route over 8 real KČT trails; `--via-loop` on a wide triangle drew a genuine non-repeating
  loop (12.75 km, 9 % retraced, gain ≈ loss); and `--via-loop` on near-collinear points correctly fell
  back and loudly flagged a 100 %-retrace out-and-back.
- **Surface / tracktype on SYNTHESISED routes** — `tests/test_composed_surface.py` pins the
  pure side (step/coord parallelism, length-weighting surviving the assembly, an exact
  `snap_points` split against the prorated answer it must not give, per-occurrence weighting
  of a retraced leg, and `None`-not-empty when the data carried no member tags), plus two
  end-to-end cases through `compose_loops`. **Verified live** over Krkonoše and Český ráj,
  and the earlier "0 surface flags" observation is what it replaces:
  - `--compose-loops` over the Špindl box, the exact run HANDOFF recorded as silent, now
    prints a surface on **14 of 15** loops (`surface:asphalt 55%`, `surface:mixed (85% known)`,
    …). The 15th is quiet for the right reason and not a gap: its summary exists with
    **15.3 %** coverage, under the 50 % gate. All 15 carry a summary object; the *flag* is
    what the gate withholds.
  - **The retrace is weighted per occurrence, live.** `--via-loop` on near-collinear Špindl
    points falls back to a 100 %-retraced out-and-back — 1.3 km of distinct trail, 2.7 km
    walked — and reports `coverage: 1.0` with shares summing over the **doubled** length
    (asphalt 36 % / fine gravel 35 % / unpaved 26 % / dirt 2 %). Had the runs covered the
    distinct trail once, coverage would have read ~50 %. Four surfaces, none over 40 %, so it
    prints `surface:mixed (100% known)` — the dominance gate doing its job on real data.
  - **The mid-segment split path is live too**: `--to-poi peak` snapped its start 32 m onto a
    segment (a real `_subpolyline` cut) and the resulting route still reported a breakdown.
  - **All five synthesised modes were run**, not four: `--around` over Špindl reports a flag
    on 6 of 8 loops, and `--from`/`--to` on both its routes. Two silences were chased down
    rather than assumed, and both are the coverage gate to the decimal — the `--from`/`--to`
    runner-up sits at **49.3 %**, just under the 50 % line, and prints nothing while its
    5.85 km sibling at 60.1 % prints `surface:asphalt 58%`. A flag that appears on some
    results and not others is the gate working; check `--json` before reading it as a bug.
- **Destination filter** (`--poi`, MCP `poi`, web "Must pass") — the registry round-trip, the
  grid-vs-brute-force equivalence, line-not-vertex proximity, the snapshot round-trip, and the
  cheap-pass economy are all unit-tested (`test_poi.py`); the HTTP surface and the loud
  400-on-typo are covered in `test_web.py`/`test_server.py`. **Verified live** over Český ráj:
  `--poi ruins,castle` returned real KČT relations annotated with Valdštejn (85 m), zámek
  Hrubá Skála (90 m), Rotštejn (20 m) and Hrubý Rohozec (28 m); a download baked 280 POIs into
  the snapshot and the same filter re-ran **offline** against it by bare area name, with
  `--poi-radius 30` correctly emptying the result.
- **POI inventory / browse** (`--show-pois`, MCP `list_pois`, web "Show points of interest") —
  covered by `tests/test_poi_listing.py` plus frontend cases in `test_cli.py` / `test_web.py` /
  `test_server.py` and 22 new browser-side checks in `webui_harness.cjs`. The four tests that
  carry the design: **offline == live** (same `AreaData` → both paths → byte-identical listing
  *and* exported GPX/GeoJSON, through a real snapshot round-trip); **no elevation provider is
  ever constructed** (`_provider` monkeypatched to raise — the mode's zero-quota claim is a
  pinned property, not a docstring); **empty kinds expand to all** (the opposite convention to
  `route_pois`, where `()` means match nothing); and **a pre-POI snapshot says it cannot know**
  rather than reporting an empty landscape, with the complement pinned too (a current snapshot
  with no caves must *not* be flagged stale). **Verified live** over Český ráj (the same box
  the `--poi` filter was verified on): `--show-pois --poi ruins,castle` listed the four
  objects HANDOFF already records for it — Valdštejn, zámek Hrubá Skála, Rotštejn, Hrubý
  Rohozec — plus Radeč and Kavčiny; a download baked **887 POIs** (a figure from the
  19-kind registry, and from before the `tower`/`shelter` deny-lists — the same box now
  lists **957**, so do not read the two as a like-for-like pair; the "501 summits"
  sub-figure below still verifies exactly) into a named snapshot, and
  the offline browse of it by bare name produced **byte-identical output to the live run**.
  The 887-object export was checked in bulk: 887 `<wpt>`, **zero tracks, zero empty names**
  (unnamed objects carry their kind), zero mojibake in the file, and the GeoJSON's
  `[lon, lat]` ranges land inside the bbox on the right axes. Unfiltered listings are big at
  real density (501 of those 887 are summits) — deliberately uncapped, with the
  count-by-kind header carrying the "what's the mix" answer; noted in README/GUIDE.
- **Nine more POI kinds + the `require` mechanism** (v0.5.0) — `boundary_stone`, `mill`,
  `mine`, `sinkhole`, `tree`, `artwork`, `firepit`, `camp`, `toilets`, taking the registry
  from 19 to 28. Chosen by a **density probe over four CZ regions** rather than from
  recalled taginfo (see the two Known-limitations entries this added), and the choice was
  the user's, made against the counts. What was actually run live, so the claim matches the
  evidence:
  - **`--show-pois` over Moravský kras** (the one box where all nine are non-zero) matched
    the raw Overpass tag counts **exactly on eight of nine**: boundary_stone 9, mill 11,
    mine 77, sinkhole 85, artwork 166, firepit 71, camp 7, toilets 44. The ninth, `tree`,
    read 21 against 24 — and the three-object gap was chased to the actual OSM elements
    rather than assumed: `natural=tree` + `historic=wayside_shrine`, named "Panna Maria"
    and "sv. Kryštof", which classify as shrines because the name is the shrine's. That is
    the exact failure advisor flagged as invisible to the test suite (a new kind can
    classify zero while looking wired), checked and clean.
  - **`--poi artwork` over Český ráj** — the *filter* path, not just the listing —
    returned real KČT relations annotated with named objects (`Rozhovor`,
    `Věčný optimista`, `sousoší Piety`, `Adam a Eva`), including `NS Kopicův statek
    (Skalní reliéfy)` passing **26 artworks within 42 m**, which is precisely what that
    trail is (a rock-relief gallery). Many artworks are unnamed and render as bare
    `artwork (N m)` — honest, not a bug.
  - **The count moved and the docs were corrected rather than left standing**: the same
    Český ráj box now lists **957** objects (119 of them the new kinds) where HANDOFF
    recorded 887. The two are NOT comparable — 887 predates both the `tower`/`shelter`
    deny-lists and these kinds — and the file now says so instead of carrying a stale
    precise number in a document whose whole argument is against stale precise numbers.
  Offline pins: the merged/required selector **partition**, the round-trip extended to
  carry required tags, "a missing requirement disqualifies", "a failed requirement falls
  through to the remaining kinds", and the density guard stated as a property
  (`"tree" not in selectors_by_key()["natural"]`) so a refactor cannot quietly start
  pulling 4000 street trees into every snapshot — the one regression here that would make
  results *more* complete and entirely useless.
- **A snapshot records the POI kind set it was classified against** (v0.5.0) — the honesty gap the nine kinds opened, closed with the mechanism this file
  had already named as the right one. **Verified live**, and the interesting half is the
  read side, because no fixture can build it: the Český ráj snapshot this file records
  from 2026-08-09 — a real 887-POI file written by the *previous* build — is read by this
  one as `kinds not recorded` in `--list-areas`, and `--show-pois --area ceskyraj --poi
  tree` hedges instead of answering. That is the `"unknown"` state on a genuinely old
  file rather than on a hand-made `AreaData`. The write side was run too, which no
  fixture touches either (every one of them skips `parse_area`): a fresh
  `--bbox 50.55 15.17 50.58 15.21 --download` stamped the registry through
  `parse_area` → `save_snapshot` → load → diff, and `--show-pois --poi tree` over it
  returned **3 named trees** (`Dub`, `Přáslavický dub` ×2) with **no hedge at all** —
  the same two-directions test the `--to-poi` bounds had to pass. Both files sit in
  `--list-areas` side by side, one flagged and one not.
  `AreaData.poi_kinds` is stamped in `parse_area`,
  round-trips through the snapshot, and is diffed against the registry on load by
  `poi.unrecorded_kinds`; `search.snapshot_poi_gap` turns that into the one of four
  states every frontend words. So an older area asked for a `tree` says it predates the
  kind rather than returning a confident empty list, `--list-areas` (and the web panel,
  and MCP `list_areas`) flags how far behind a file is *before* it is searched, and
  "looked for all 28, found none" became expressible for the first time. Offline-pinned
  throughout — see the Known-limitations entry for the four states, the "never gate on
  emptiness" rule that a first draft got wrong, and why the on-disk key is written
  conditionally where `transit`'s is not.
- **Downloaded-area inventory + drawn-box selection** (`--list-areas`, MCP `list_areas`, web
  "Already downloaded" / "Draw a box") — live-verified for the CLI/HTTP paths. The web UI's
  *browser* logic is covered by `tests/test_web_js.py`, which runs the page's real script under
  node against a stubbed Leaflet/DOM (skipped without node). That harness is not decoration: it
  caught a drag's trailing `click` being taken for a point-pick.
- **Via ferrata: `--ferrata` / `--no-ferrata` / `--show-ferrata`** (MCP `ferrata` on every
  search tool + `list_ferrata`; a select in the web UI). All three frontends live-verified
  against a Cortina box and its downloaded snapshot, and offline == live: 20 routes
  unfiltered, 19 under `--no-ferrata`, 1 under `--ferrata`, identical through the CLI, the
  web `/api/hikes`, and the MCP server over real stdio.
  **The old entry here claimed 63 standalone `highway=via_ferrata` ways were "invisible
  entirely". That structural claim was wrong**, and the plan built on it was wrong with
  it. Measured with `out count` over two boxes: of Cortina's 46 `highway=via_ferrata`
  ways only **6** are not already member ways of a hiking relation, and of Ehrwald's 30
  only **4** (the ~87 % that ARE members were being fetched all along, sitting unread in
  `way_tags`). Don't re-derive the count from the number 63 — neither bbox was recorded,
  and it is the *structure* that mattered.
  Two measurements shaped the design and are worth keeping:
  **(1) `via_ferrata_scale` is the dominant carrier, not `highway=via_ferrata`** — 70
  graded ways in Cortina against 46 with the highway value, and **25 of the 70 are
  `highway=path`**, ordinary-looking paths with cable on them. The predicate is *either
  key*; keying on the path type alone loses a third of them.
  **(2) The two directions are not symmetric.** Finding tolerates a false negative;
  avoiding tolerates none. Avoidance is complete over the route universe *by construction*
  — every returnable route is assembled from `route=hiking`/`route=foot` member ways, so a
  cabled section inside one is always described by tags already held — which is why the
  query widening bought nothing for `--no-ferrata` and was justified only by the target
  search. The residual is cable nobody tagged, and it is real: `rel/15791498`
  (*Via Ferrata Ivano Dibona ascent parte superiore*) survives `--no-ferrata`, and a
  direct Overpass check confirms its relation and all ten member ways carry `sac_scale`
  and no ferrata tag whatsoever. That is a mapping gap, not a bug — but it is why every
  frontend says "filter, not a safety guarantee" rather than leaving it to a docstring.
  Two traps this cost real time to get right, both worth not re-learning:
  **Presence, never dominance** — `surface`'s 40 %/50 % gates exist to stop a plurality
  posing as an answer, and a hazard inverts that rule exactly. Route the summary through
  `surface._summarise` or copy its flag-line shape and 300 m of cable on a 12 km walk
  vanishes *with every test still green*; `test_a_short_cabled_section_still_trips_
  avoidance` is the guard.
  **Composed routes must be measured BEFORE the cheap pass, not after.**
  `_attach_composed_surface` runs after `find_hikes`, which is fine for surface because
  nothing filters on it. Ferrata IS filtered on, so the same placement left every
  synthetic route `None` at filter time and emptied `--compose-loops --no-ferrata` over
  landscapes full of walkable loops. It rides `pre_ferrata_by_id` instead, alongside
  `pre_elevations_by_id`.
  One wording bug got caught only by running it: `ferrata_unrecorded_message` ends by
  promising that avoidance still works — true only for a file that HAS member-way tags,
  and `ceskyraj.json` has neither. `ferrata_gap_message` is now the single place that
  picks between the two sentences, and the ordering (unreadable first) is what keeps each
  one true.
- **The point modes say which empty they are — on all three frontends in one go.**
  `--around`, `--from/--to`, `--via` and `--to-poi` derive their own bbox from the
  point(s) you pick, and until this they could not report a single fact about the fetch:
  the four engine functions returned hikes and nothing else, so every frontend answered
  an empty search there with its own advice — widen the radius, move your points closer
  to a trail, raise the length cap. In Kamikōchi (824 mapped path ways, zero route
  relations) every one of those sentences sends the reader to fix something that was
  never the problem, and picking a point is the most natural way to search a place like
  that. Worth not re-deriving:
  - **The seam is the SAME `diagnostics` out-parameter** the bbox search already fills,
    added to all four functions, set from the ONE area fetch each of them makes — before
    any snapping or routing, so the answer never depends on how far a picked point landed
    from a trail. That has its own message and its own fix.
  - **All three frontends were wired in the same change, deliberately.** The ferrata
    caveat reached the CLI, the web UI and the MCP server on three different days, and
    this is the same shape one level down. The web's `_live_notices`, the CLI's
    `empty_msg` override and the server's `_point_empty` are three call sites of one
    fact.
  - **`--to-poi` is the case that had to be got right.** One fetch answers both of its
    empties, and they are different facts: `no_routes` reads `area.routes` only, so an
    area full of trails and free of ruins keeps the destination-shaped sentence. The
    other direction is the dangerous one — "nothing of that kind is mapped in OSM near
    your start" is a claim about churches made by a search that never found a trail.
  - **One kind here, never the ferrata gap, and `web._live_notices` says why in prose.**
    A point mode is always a LIVE fetch, which parses both cable lists and the member-way
    tags, and the ferrata clause changed the query text — the Overpass cache key — so a
    pre-feature response cannot be served under one either. `ferrata_gap_message` is
    provably `None` there; a seam that can never fire reads as one that might.
  - **The browser proof does not come free from the area path's.** §14's assertion that
    `no_routes` *displaces* the empty-result advice runs in the area mode, whose fallback
    contains "widen the map" — a real check there and a vacuous one in every point mode,
    since none of their four sentences says it. `webui_harness.cjs` §15 drives the two
    that can actually fail (`topoi`, `around`) at Kamikōchi's coordinates, and its stub
    rule sits ABOVE the `to_poi` one or that shadows it and answers with routes.
  **Verified live over real Overpass at Kamikōchi** (36.24, 137.63), the region the fact
  was measured in: `--around` printed the map-data sentence in place of "widen
  --around-radius", and `--to-poi ruins` printed it in place of its three-cause
  destination sentence. Noted while reading that second run: the mode's own stderr log
  line ("nothing of that kind is mapped within 3.0 km") still prints beside it. It is a
  log, not the answer, and it is true there — but it is the closest thing to a
  contradiction this change leaves on screen, so it is written down rather than
  discovered again.
  Pinned: 10 engine cases + 8 CLI cases (`test_no_routes_message.py`), 13 HTTP cases
  (`test_web.py`, including that the live and saved paths word the one fact identically),
  8 MCP cases (`test_server.py`) and 4 browser checks — each in both directions, because
  a caveat that never switches off is noise.
- **The web UI carries its caveats: `/api/hikes` is an envelope, not a bare array.**
  `{"hikes": [...], "notices": [{"kind", "message"}]}`. The array was the reason the
  browser was the one frontend that answered a question it could not answer with a
  silent empty list; `/api/poi-list` had already set the object precedent. Four things
  worth not re-deriving:
  - **The two kinds are rendered DIFFERENTLY, and that is the point of carrying a
    `kind` at all.** `ferrata_gap` gets its own persistent box (`.notice`), shown
    whenever the filter is active and the file falls short — *including when routes came
    back*, the "never gate a caveat on an empty result" rule. `no_routes` **replaces**
    the page's empty-result sentence instead of joining it: "widen the map or relax the
    filters" is precisely the advice `no_routes_message` exists to delete, and rendering
    both would put two contradictory answers on screen. Wiring both into one box was the
    obvious first draft and is the bug.
  - **No ferrata notice is computed on a live path, deliberately.** A live parse always
    sets `ferrata_routes`/`ferrata_ways` and member-way tags, and the ferrata clause
    changed the query text — the Overpass cache key — so a pre-feature response cannot be
    served under it either. `ferrata_gap_message` is provably `None` there, and a seam
    that can never fire reads as one that might. The saved-file branch is the only source
    that can fall short, so `web._area_notices` is called from exactly one place.
  - **`no_routes` reuses the `diagnostics` out-parameter** the CLI and MCP server read,
    rather than re-fetching the area to word a message (free only while the Overpass
    cache is on).
  - The notice text is set with `textContent`, not `innerHTML`: it is server prose and
    there is no reason to hand it a parser.
  **Verified live over real HTTP** (the server, on hand-built offline snapshots — no
  network), all four cases and both directions: `untagged` (no member-way tags) +
  `ferrata=false` → 0 routes and the **unreadable** sentence, never the "avoiding still
  works" one; `tagged` + `ferrata=false` → 1 route and **no notice at all**; `tagged` +
  `ferrata=true` → the **unrecorded** sentence, whose closing promise is true only on a
  file like this one; `norelations` → `no_routes`; and `untagged` with no ferrata filter
  → 1 route, no notice. The served page carries `id="notices"` and `showNotices`, and
  `/api/gpx` still streams (the export path drops notices — a GPX has nowhere to put a
  sentence, and the search that produced it already showed one).
  Offline pins: seven HTTP cases in `test_web.py` (the envelope shape itself, so the
  `_hikes` unwrapping helper cannot tunnel a shape change past the suite; both ferrata
  sentences; both quiet directions; `no_routes` offline and live) and ten browser-side
  checks in `webui_harness.cjs` — including the one the server cannot currently produce
  and the browser must still honour: **a gap notice rendered while routes are listed**.
  The ferrata cases are pinned at the page's DEFAULT `near_misses` ("auto"), not the
  explicit `false` they were first written with, and the near-miss pass does not relax
  `ferrata` in either direction (checked live and stated in `Criteria.accepts_geometry`)
  — so the empty list really is empty and the notice really is all there is.
  The real browser was NOT driven (no extension available in that session); the node
  harness runs the page's own script, which is this repo's standing substitute.
  **`clearNotices()` runs ahead of every mode guard**, not beside the fetch: several
  guards return early ("drop your point first") and a caveat left standing describes a
  search nobody is looking at. And the harness could not have caught that as first
  written — its DOM stub kept `_children` when the page set `innerHTML = ''`, so "this
  search rendered nothing" was unobservable and every call site reset the list by hand.
  The stub drains on empty now. That is the same class of defect as the deferred
  `setTimeout` this harness already documents: **a stub that is more forgiving than the
  DOM hides exactly the bugs the harness exists to catch.**
- **MCP `find_hikes` carries the ferrata gap in its reply text.** The last frontend that
  answered a cable question it could not answer, and the one whose reader paraphrases:
  `find_hikes(area=…, ferrata=false)` over a file with no member-way tags used to return
  "No matching hikes found in that area." and nothing else. `server._ferrata_caveat` now
  prepends `search.ferrata_gap_message`'s sentence plus one server-local instruction —
  *do not turn this into a statement about cable on the ground, in either direction*.
  Direction-neutral on purpose: the same silence misreads as "nothing cabled here" when
  avoiding and "no ferrata here" when finding, and one sentence has to cover both.
  Five decisions, all of them the shape an earlier feature already argued for:
  - **Recomputed in `server.py`, not plumbed out of `search_snapshot`.** That function
    already computes and logs it — which is exactly how the CLI gets its copy — and a log
    line reaches a terminal, not a client's reply. `web._area_notices` recomputes for the
    same reason. The message is shared; the *channel* is per-frontend.
  - **One call site, on the saved-file branch only.** A live fetch always parses both
    ferrata lists and the member-way tags, and the ferrata clause changed the query text
    (the Overpass cache key), so a pre-feature response cannot be served under it either:
    `ferrata_gap_message` is provably `None` there. A seam that can never fire reads as
    one that might, so there isn't one. Pinned by `test_a_live_ferrata_search_is_never
    _caveated`.
  - **Not gated on an empty result**, the trap this file booked with the task. The
    concrete shape is sharper than the general rule: asked to FIND cable, a file holding
    member-way tags but no fetched ferrata objects returns the hiking routes whose own
    members are tagged cabled — a real, non-empty answer — while the dedicated
    `route=via_ferrata` relations it never downloaded stay missing. A partial answer is
    where a missing caveat does the most damage.
  - **Three return sites, and the third must NOT carry it.** The empty-result reply and
    the final text reply do; `gpx` and `geojson` are documents with nowhere to put prose,
    and prose in front of a GPX is not a caveat but invalid XML. Note the empty branch
    runs *before* `format` is read, so only a matching search reaches those returns at
    all — a test on a non-matching fixture would pass without proving anything.
  - **`_ferrata_caveat` takes the SNAPSHOT, not its area**, so the "was a ferrata question
    even asked" guard runs before anything is read off the file.
  Both notices can fire together and both are said: an area with no route relations, asked
  to find cable, is *also* a file that never fetched ferrata objects, and re-downloading
  fixes the second while nothing fixes the first. Asked to avoid cable the same file
  yields no caveat (`area_ferrata_readable` is vacuously true with no routes) so
  `no_routes` stands alone — the complement falls out of the predicates rather than being
  arranged, and is pinned as such.
  **Putting those two sentences in one paragraph exposed a wording bug the other two
  frontends had been hiding**, and it is the second instance of the same defect this
  feature has produced. `ferrata_unrecorded_message` ended by promising that avoidance
  still works "from the member ways it already has" — and the ordering in
  `ferrata_gap_message` guarantees that only for a file shown to CARRY member-way tags,
  which a routeless file never was: it passes the readable check *vacuously*, by design.
  So the promise landed on a file with no member ways at all. Milder than the `ceskyraj`
  original (empty rather than false — nothing is silently dropped) and invisible where the
  CLI splits the two across streams and the web UI across boxes, but "it still works" said
  about a file with nothing in it is read as reassurance. `ferrata_unrecorded_message`
  takes `avoidance_works=` now and `ferrata_gap_message` passes `bool(area.routes)`. The
  rest of the sentence stays, deliberately: `route=via_ferrata` relations are **not**
  hiking routes, so re-downloading an area with no hiking routes can still turn up cabled
  climbs, and `no_routes_message` says nothing about that. The general lesson is about
  *channels*, not wording — **a message pair that three frontends render in three places
  is only proof-read where they land together**, and until this reply existed nothing put
  them together.
  **Verified over real stdio** (a `python -m hike_finder.server` subprocess, OS pipes,
  JSON-RPC — offline, on hand-built snapshots, so no Overpass and no elevation API), all
  twelve cases across four files: unreadable + `ferrata=false` → the **unreadable**
  sentence, never the "avoiding still works" one; the same file with no ferrata flag →
  the route and **no caveat**; tagged + `ferrata=true` → the **unrecorded** sentence,
  whose closing promise is true only on a file like that; tagged + `ferrata=false` →
  **silent**; current file → silent in both directions; no-routes + `ferrata=true` →
  **both** sentences, + `ferrata=false` → `no_routes` alone; and the cabled fixture,
  which is the one that matters: `ferrata=true` returned the caveat **followed by a real
  route** (`ferrata 5.6 km`), its `gpx` opened on `<?xml` and its `geojson` parsed, both
  free of the prose. Seven offline cases in `test_server.py` pin the lot; four of them
  fail on the pre-change server, and the three that pass are the ones asserting silence.
  One assertion had to be narrowed after the live run: a GPX legitimately carries the
  route's OWN `ferrata 5.6 km` flag in its `<desc>`, so "the document says nothing about
  ferrata" is the wrong property — "the document does not carry the CAVEAT" is the right
  one.
- **All three area tools read an area the same way** (`server._read_area`). `find_hikes`
  called `load_snapshot(area_path)` straight, so `find_hikes(area="cortina")` — the bare
  `name` an LLM has just read out of `list_areas` — raised a `FileNotFoundError` from deep
  inside the loader, about a path the caller never typed, before any search or caveat ran.
  `list_pois` and `list_ferrata` each already tried the path, fell back to the named
  snapshot directory, and turned a read failure into a sentence; the fix is one helper
  they all three call, not a third copy. Wording unchanged, so the two tools that had it
  keep their exact reply.
  The seam is the point rather than the two lines of logic: this fallback was written
  twice and the third tool went five releases without it, which is the same shape as the
  ferrata caveat reaching three frontends on three different days. `test_the_three_area
  _tools_answer_an_unknown_name_identically` reads all three replies and compares them, so
  a fourth tool, or a reworded one, has to agree on purpose. Two more tests pin what the
  helper decides: a bare name resolves, and an explicit path still WINS over a
  same-stemmed file in the snapshot directory (the fallback only fires when the argument
  is not itself a readable file). The `find_hikes` schema now says so too — and the
  `list_pois` schema's "note that `find_hikes(area=…)` takes a path only" is gone, which
  is the kind of sentence that outlives the fact it describes.
- **Lint and type baseline, enforced in CI.** Nothing had ever run either over this tree.
  `ruff` (rules `E,F,W,I,B,UP,SIM,C4,RUF,BLE,SLF`, line length 100) and `mypy` over `src`
  both sit at zero, configured in `pyproject.toml` and run by a `lint` job.
  Four decisions worth keeping:
  - **The lint job is PINNED and the test matrix is not, deliberately.** The matrix floats
    so the Monday cron catches the world moving (that is how mcp 2.0.0 broke this repo on
    a commit that touched none of it). A linter doing the same would fail commits that
    touched none of *it* — a new ruff rule is not a defect in the change being tested. The
    job installs `mcp` and `rasterio` too, because mypy reads them: without `mcp`, all of
    `server.py` types as `Any`, which is where 21 of the original 61 errors lived.
  - **`BLE` and `SLF` are in the rule set BECAUSE the tree already carried `noqa: BLE001`
    and `noqa: SLF001` comments.** Someone reached for those rules before anything
    enforced them; leaving them unselected would have let `RUF100` delete the reasoning in
    those comments as dead.
  - **`zip(strict=)` was decided per site, never blanket-applied** — it is the only rule
    here that changes runtime behaviour. `strict=True` where a length mismatch violates an
    invariant the code already states in prose (one elevation per point; `way_tags`
    parallel to `ways`, which both producers maintain and `clip_routes_to_bbox` rebuilds
    to keep); `itertools.pairwise` where the zip was only walking successive pairs.
  - **`web.py` is exempt from the line limit and nothing else.** Two thirds of it is
    `INDEX_HTML`, the browser page as a string constant, and every long line lives in
    there. Rewrapping HTML and JS to Python's column is a readability loss in the other
    language. Extracting the page to its own module would let the rule come back; the
    per-file-ignore says so.
  The 61 errors produced no bug. Six looked like one and each turned out to be the checker
  seeing an invariant stated in prose — the most interesting being
  `ComposedLoop.destination`, set from outside by `routes_to_poi` and never declared. It
  is now a field under `TYPE_CHECKING`, so composition still imports nothing from
  `poi.py`: the separation that file's comment asks for, kept by the dataclass instead of
  by a `getattr` with a default. One real fragility did surface: `Tool(inputSchema=…)` is
  the *wire alias*, `input_schema` the field. Both work at construction, so the server ran
  and every test that read the model back as an object passed. The switch comes with a
  test that asserts the SERIALISED form still carries `inputSchema`, which is the only
  place a client would ever have seen the difference.
- **The three frontends are checked for filter parity** (`tests/test_frontend_parity.py`).
  One table per `Criteria` field — the CLI flag, the MCP argument, the web query parameter
  and the value all three must produce — plus a gate asserting the table covers every
  field, so a new filter fails here on the day it is added instead of on the day someone
  notices one frontend cannot ask for it. This repo's recurring bug is not a wrong answer;
  it is a filter reaching the three surfaces on three different days.
  **The MCP side is checked in two halves because they break separately**: `_criteria`
  reads a key whether or not the tool schema declares it, so a handler test alone passes
  on a tool no client can discover the filter on. Verified by deleting `transit_access`
  from `find_hikes`'s schema — exactly the schema test fails, the handler test does not.
  That second half found the gap recorded under Known limitations below.
- **All three frontends validated live**, including the MCP server over real stdio.
- **Repo hygiene**: MIT license, CHANGELOG, complete pyproject, and CI across Linux 3.10–3.14 +
  Windows; v0.1.0 through v0.7.0 tagged + GitHub-released. **CI being green is a
  per-commit fact, not a standing property of this repo** — check it (`gh run list`) before
  asserting it anywhere, a release body most of all. An unpinned optional extra silently
  falsified this very line for four commits while the sentence sat here unchanged; see the
  `mcp>=2` entry under Known limitations.

Run it: `pytest -q` — the full suite is offline (a few `.sh` launcher cases need bash; MCP tests
skip without the `mcp` extra).

## Known limitations / TODOs (design notes, not bugs)

- **Four MCP point-mode tools honour filters they do not advertise.** `circular_routes`,
  `routes_between`, `route_via` and `routes_to_poi` build their filters with the same
  `server._criteria` as `find_hikes`, so all four apply all ten `Criteria` fields — but
  their `inputSchema`s offer fewer. Some omissions are the filter being meaningless for
  the mode: a `circular_routes` result IS a loop, so `circular` has nothing to select.
  Eight are gaps with no stated reason — an LLM cannot ask `circular_routes` for a gain
  range that the engine applies and that the CLI exposes on the very same mode
  (`--min-gain` works with `--around`). Both sets are tabled in
  `tests/test_frontend_parity.py` (`DELIBERATELY_ABSENT` and `UNADVERTISED`) and the test
  fails on drift in either direction: an omission in neither table, and an entry that has
  gone stale because the gap was closed. Left listed rather than fixed because closing one
  changes what the MCP surface advertises to every client, which is a decision, not a
  cleanup. Adding a property to a schema is additive and cheap when that decision is made.

- **The app is only as good as `route=hiking`/`route=foot` coverage, and that varies by
  region far more than by terrain.** Every mode — area search, `--around`, `--from/--to`,
  `--via`, `--to-poi`, `--compose-loops` — builds its graph from relation member ways, so
  relation sparsity is a *whole-app* failure, not a per-feature degradation. Measured with
  `out count;` over six ~400 km² boxes (CZ Krkonoše = the control at 138 relations):
  IT Dolomites **207** (1.50× — the app works better there than where it was built),
  BG Rila 28, US Rocky Mtn NP 13, JP Kumano Kodo 6, **JP N. Alps / Kamikōchi 0**.
  Relation *count* is not comparable across regions and the live runs prove it: CZ's are
  short A→B fragments while Kumano's 6 are long trunk pilgrimage routes (~128 km of trail
  between them) — Kumano and Rocky Mtn both work fine. Kamikōchi's zero is immune to that
  caveat: 824 path ways mapped, none collected into a route. A `highway=path` fallback is
  the obvious answer and is deliberately NOT taken — it widens every query, invalidates
  the Overpass cache, and changes what "a route" means. Decide that on purpose, not as a
  side effect.
- **`/api/hikes` answers with an object now, and the reason is worth keeping.** It
  returned a bare JSON array for four releases, which had nowhere to put a sentence about
  what the *source* could not answer — so the web UI showed a silent empty list where the
  CLI logs to stderr. See "The web UI carries its caveats" under What is DONE. MCP
  `find_hikes` was the worst instance of this class and is now wired (see "MCP
  `find_hikes` carries the ferrata gap" under What is DONE). Both residual limits are
  now closed:
  - ~~The web's *point-based* modes return `notices: []` unconditionally~~ — fixed, and
    on all three frontends at once; see "The point modes say which empty they are" under
    What is DONE.
  - ~~MCP `find_hikes` cannot take a bare area name~~ — fixed; see "All three area tools
    read an area the same way" under What is DONE.
  One PREMISE in that fix is unpinned and worth knowing before it is broken. "A point mode
  never carries a ferrata caveat" is true only because a point mode is always a LIVE
  fetch. Let one read a saved snapshot — `--around` against a downloaded area is the
  obvious next feature — and the silence becomes wrong, while
  `test_a_point_mode_never_carries_a_ferrata_notice` keeps passing and hides it: it
  asserts the silence, not the reason for it. Whoever adds an offline point mode has to
  route it through `_area_notices` (or its MCP/CLI equivalents) in the same change.
- **`--show-ferrata` cannot export.** `--gpx`/`--geojson` are named in its ignored-flags
  note rather than wired up, because `ferrata.FerrataLine` carries only a start point,
  not the cabled line's geometry. Exporting means widening that record and adding a
  LINE exporter (`pois_to_gpx` writes waypoints; these are lines, so GeoJSON LineStrings
  / GPX tracks). A real feature, not a footnote — `--show-pois` sets the precedent that
  a browse mode SHOULD export.

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
  pad knobs for that. `--via` uses the same pad (off the widest leg) with a sharper failure mode:
  clipping a return that bows outside the corridor can drop the endpoint junctions to degree-2,
  collapsing the line to one segment and forcing an out-and-back where a real disjoint return sat
  just outside — raise `HIKE_ROUTES_PAD_*` if a `--via-loop` retraces unexpectedly. `--around`
  similarly fetches `radius + max-loop/2` (a 15 km band → ~17 km Overpass box, a heavy query). All
  are point-derived-bbox trade-offs, not bugs.
- **Round-trip vs point-to-point gain:** we report cumulative gain over the line as-is; `loss`
  gives the reverse direction's gain.
- **Daily quota** assumes a UTC-midnight reset and can lose an update under a cross-*process*
  race (acceptable for a soft advisory limit; no file locking).
- **POI proximity is best-effort, like access.** No hit means nothing of that kind is *mapped*
  in OSM near the route. The registry is also a curated subset — 28 kinds a walk is planned
  around — so a concept nobody registered is simply not askable until someone adds it to
  `POI_KINDS` (one line; the query and classifier follow automatically).
- **Weigh a candidate kind by MEASURING it, not by recalling taginfo.** This file used to
  offer "a monastery or a windmill" as the example of a missing kind. Counted over four CZ
  hiking regions (Český ráj / Krkonoše / Moravský kras / Šumava), `historic=monastery`
  returns **1** object, `amenity=monastery` 2, `building=monastery` 1, and **zero** carry
  `amenity=place_of_worship` as well. The recalled global taginfo ordering (`historic` ~22k
  vs `amenity` ~12k) was real and completely irrelevant: it describes the world, not
  walking country. `windmill` fared little better at 14. Both would have shipped as kinds
  that look wired and return nothing. The same probe settles the tag-key question, the
  density question and the live-verification numbers at once, and it is cheap — `out count;`
  per candidate, node/way/relation separately. It is the `sac_scale`/`trail_visibility`
  precedent (measured at 4 % and 1 %, rejected on the data) applied before the fact.
- **Registry ORDER is a real decision, because `classify` is first-match-wins.** With one
  kind per tag key that could not bite; with 28 kinds over 7 keys it can, and a new kind
  appended after an existing one can classify **zero** objects while looking perfectly
  wired — which `test_poi.py`'s round-trip cannot see, since it pins query↔classifier
  agreement rather than real-world yield. So the collisions were counted too, and almost
  none exist: artwork↔any historic **0**, adit↔cave_entrance **0**, firepit↔picnic **0**,
  camp↔shelter **0**, mill↔ruins **0**, boundary_stone↔man_made **0**. Three do:
  mill↔museum (2, resolved to `mill` — a watermill running as a museum is still what you
  walked to) and `natural=tree`↔`historic=wayside_shrine` (3, resolved to `shrine` — the
  NAME belongs to the shrine, not the tree). That last one is why a live Moravský kras
  listing returns 21 named trees against a raw tag count of 24, and the 3 are not missing.
- **Two kinds carry a deny-list, and it lives in the classifier, not the query.**
  `man_made=tower` is every tower (transmission, water, chimney) and `amenity=shelter`
  includes bus shelters, so both were reporting objects nobody plans a walk around — the
  tower kind while labelling itself *"lookout towers"*, which is the label promising what
  the selector never checked. `PoiKind.exclude` disqualifies on a secondary tag. Three
  design points: (1) a **missing** secondary tag never disqualifies (most real lookouts
  have no `tower:type`), the same "not recorded ≠ no" rule as `transit_access`; (2) it
  filters in `classify`, so the query TEXT is unchanged and the Overpass **cache is not
  invalidated** — unlike adding a kind, which is why this was cheap to ship alone; (3) an
  exclusion **falls through** to the remaining kinds rather than returning `None`, or a
  communication tower tagged `tourism=museum` would lose its museum classification.
  Consequence to know: an **already-downloaded snapshot keeps its old classifications**,
  because `classify` runs upstream in `parse_area`. Re-download to reclassify. That is the
  surface/tracktype precedent (nothing *filters* on ungathered data), not a staleness bug,
  and it needs no version field.
- **Adding a KIND is strictly worse than adding an exclusion on that last point — which
  is why a snapshot now records the kind set it was classified against.** An exclusion
  makes a stale snapshot over-report by a few objects. A new kind made it answer
  `--show-pois --poi tree` with a confident **empty list** for a question the file never
  asked — "there are no named trees here", exactly the `transit_access` failure mode the
  tri-state exists to prevent. The old staleness signal could not catch it: `stale` was
  `not snap.area.pois`, i.e. "this file predates POIs entirely", and a v0.4.0 snapshot has
  plenty of POIs — just none of these nine kinds. This file previously argued the fix was
  documentation, and named the mechanism it would take if the registry kept growing; the
  registry then grew by nine in one release, so it was built. What it is NOT is a version
  number: `AreaData.poi_kinds` stores the **set** (`poi.all_kinds()`, stamped in
  `parse_area` where `classify` runs, so it cannot describe a different registry than the
  one that sorted the objects), and `poi.unrecorded_kinds` diffs it on load. A version
  scheme would be a second source of truth about the registry; a set is the registry.
  Four states, kept apart because collapsing any two is a differently-shaped lie —
  `search.snapshot_poi_gap` is the one function that decides which holds, and all three
  frontends plus both offline entry points route through it:
  - `"none"` — no objects *and* no kind record: predates POIs outright (the old signal,
    unchanged, and still its own wording).
  - `"missing"` — records a kind set, and the asked-for kinds are not in it. Names them.
  - `"unknown"` — has objects, records no kind set (saved between the POI feature and
    this one). It cannot say which questions it was asked, and neither can we: inferring
    coverage from *which kinds appear in the data* is circular, since absence there is
    precisely the ambiguity being resolved.
  - `"ok"` — everything asked for was looked for. This state is what the record buys and
    it did not exist before: "looked for all 28, found none" was previously
    indistinguishable from a pre-POI file and was reported as one.
  **Nothing is gated on the result being empty, and that was a real bug in the first
  draft.** Hiding the caveat behind a non-empty result looks like noise-suppression and
  is not: `--poi ruins,tree` against a 19-kind file returns the ruins and goes quiet,
  while `tree` — the half nobody may have answered — disappears behind the half that was.
  A non-empty result proves only that *some* requested kind was classified. The pre-POI
  and `transit_access` warnings already fire on the filter being active rather than on
  the result being empty; this matches them. (Note `find_hikes` does not relax
  `poi_kinds` for near-misses either, so an empty route list is not the only shape the
  failure takes.)
  On disk the key is written **conditionally**, unlike `transit`: both are tri-state, but
  a snapshot is only ever *written* from a live parse, which always sets `transit`,
  whereas an `AreaData` with `poi_kinds=None` is reachable — load an old file and save it
  again. Writing `[]` for it would upgrade "cannot say" into "positively covered
  nothing", a stronger claim than the file supports. `SNAPSHOT_VERSION` stays 1: the key
  is additive, and an older file loading without it is exactly what puts it in
  `"unknown"`. The whole thing is offline-pinned in `test_poi_listing.py` (the three-way
  diff, the round-trip incl. the legacy file that must not gain a key, the "announced
  even when other kinds returned objects" case, and the new `"ok"` state), with frontend
  cases in `test_cli.py` / `test_web.py` / `test_server.py` and four browser-side checks
  in `webui_harness.cjs`.
- **Adding a POI kind widens every Overpass query** and invalidates the Overpass cache (the
  query text is the cache key), which is the price of the single-query-shape design. Weigh a
  new kind's density before adding it: `amenity=restaurant` in a city bbox is hundreds of
  elements, though still trivial next to relation geometry.
- **`--poi` filters, `--to-poi` navigates.** The two take the same kinds and answer opposite
  questions, and they compose ("a route to the nearest ruin that also passes a pub"). The
  destination kinds are deliberately NOT copied into `criteria.poi_kinds`: a route whose
  destination snapped 400 m off the trail would then be dropped by the 250 m `poi_radius_m`
  filter — the destination gap and the pass-by radius are different measurements.
- **`--to-poi` sizes its fetch by the length cap, and that is the point.** A shortest path of
  length L has every vertex within L of its start, so padding the box by the cap makes a
  qualifying route unclippable — the `compose_loops_around` tight-pad precedent, chosen over
  `routes_between`'s accepted-clipping one *because the mode makes a superlative claim*: there,
  clipping costs you an alternative; here it would silently promote the second-nearest ruin to
  "the nearest". The price is a heavy query at a high `--max-distance` or `--to-poi-radius`
  (default 3 km radius → ~9 km pad → an 18 km box, comparable to `--around`'s). A 1 %
  `_POI_PAD_MARGIN` covers `_bbox_around`'s 111 320 m/deg vs `haversine_m`'s ~111 195 —
  without it the "provably unclippable" pad is 0.1 % short, and an argument that is 0.1 % false
  is false.
- **`--to-poi`'s "nearest" is bounded, not absolute.** Two lower bounds make it checkable
  (crow-flies ≤ trail distance): objects outside the search radius, and candidates the
  crow-flies cheap pass dropped (`_POI_CANDIDATE_FACTOR`×N, min 10). When the longest route
  returned exceeds either bound the mode logs "not provably the nearest" rather than assuming
  the margin held. It *can* return the wrong one — `test_the_cheap_pass_admits_when_it_may_have
  _dropped_a_nearer_one` constructs exactly that, and a live `--to-poi peak --routes 1` over
  Český ráj *did* it (1.21 km answer; the dropped tail held one at 0.43 km) — but never
  silently. What decides whether the hedge fires is the **density** of the kind against the
  pool of 10, not the kind itself: in a rock town the 11th-nearest summit is 300 m out, so
  the pool is hopeless and the warning is on; in Krkonoše the 11th sits 3.6 km out and the
  same query goes quiet. Raising `--routes` is the only lever on the pool
  (`keep = max(4×N, 10)`) — the factor exists to keep the Dijkstra count proportional to
  what was asked for, so there is no separate knob.
- **`--to-poi` is live-only, deliberately.** A snapshot carries both routes and POIs, so an
  offline variant is conceivable, but the mode needs a trail graph and the elevation pass, and
  a snapshot's bbox is fixed while this mode derives its own from the start point and the cap.
  The CLI and the web both reject the pairing loudly rather than quietly returning a filtered
  area search that *looks* like an answer.
- **`--list-areas` can only enumerate the NAMED snapshot directory.** A CLI
  `--download some/path.json` writes wherever you point it and is tracked nowhere; there is no
  registry of arbitrary paths and inventing one would be a second source of truth. Said in the
  `--list-areas` help text so it isn't discovered the hard way.
- **The MCP extra now requires `mcp>=2`** (it was capped `<2` in v0.3.1, while the port was
  outstanding). A floor, not a cap: 1.x is no longer supported, because supporting both would
  mean a version branch in `server.py` *and* in the test harness — two code paths, one of which
  nobody runs. Only MCP users are affected; `mcp` is an optional extra, so a base CLI/web
  install never saw any of this. What the port actually changed, all of it plumbing:
  handlers move from the `@app.list_tools()` / `@app.call_tool()` decorators to the `Server`
  constructor's `on_list_tools` / `on_call_tool` arguments (**not** `add_request_handler`, which
  is what this file predicted before anyone read the 2.x source), taking `(ctx, params)` and
  returning `ListToolsResult` / `CallToolResult`; `app` is therefore built at the BOTTOM of the
  module, after its handlers exist. All eight `Tool(...)` definitions are untouched —
  `inputSchema=` still validates, via the field's alias. `stdio_server` and `main()` are
  unchanged. On the client side `isError`/`inputSchema` became `is_error`/`input_schema`, and
  `read_timeout_seconds` takes float seconds, not a `timedelta` (it reaches
  `anyio.fail_after`, so a `timedelta` fails with `float + timedelta` at call time, not at
  construction — `test_launchers.py` had the same line).
- **mcp 2.x does NOT validate arguments against a tool's `inputSchema` server-side.** The
  `required` list is advice to the client. So each handler's own argument guard is the only
  thing between a malformed call and an unasked-for search, and those guards `raise` rather
  than return: `call_tool` maps a raised `ValueError` to `CallToolResult(is_error=True)`, which
  is a *readable* complaint. Returning the message instead — which is what they did under 1.x,
  where the framework rejected the call before the handler saw it — ships a complaint with
  `is_error` false, i.e. dressed as a successful answer.
  **An unknown tool NAME is the opposite case and deliberately still raises**: that is
  protocol-level, there is no sensible tool *output* for it, and it reaches the client as an
  `MCPError`. Under 1.x both looked identical from the test's seat; 2.x lets them differ.
- **The dependency lesson from the `mcp<2` incident stands, and is the reusable part.** An
  unpinned optional dep silently broke CI the moment 2.0.0 shipped upstream, on a commit that
  touched none of it. Because the failure was an *import* error in a test module, pytest
  reported it as a **collection** error (exit 2) — all six jobs red, *no* test run, not just
  the MCP ones. Nothing in the repo was wrong; the world moved. The lesson is narrower than
  "pin everything": a test-only import of a third-party symbol takes down the whole suite,
  while the same dep behind `importorskip` only skips. Bound majors on optional extras, and
  treat a suite that fails at collection as a dependency question first. Note too that the
  imports still *resolved* under 2.0 (`Server`, `stdio_server`, `mcp.types`) — a version probe
  that only checks importability reports a false all-clear; the break was in the API those
  names expose. **CI now runs weekly on a cron** (`schedule` in `ci.yml`) so the *next* dep to
  move is caught by a dated run on main rather than by whatever unrelated commit lands first —
  capping `mcp` fixed one instance, the schedule addresses the class. The concurrency group
  includes `github.event_name` so a Monday cron can't cancel an in-flight push run and be
  misread as that commit failing.
- **`transit_access` is tri-state `bool | None`, and collapsing it to `False` would be a lie.**
  Every other access field is a plain `bool`, because every other one predates the snapshots on
  disk. Transit does not: a snapshot written before the feature has NO transit data, so `None`
  ("never measured") has to stay distinguishable from `False` ("nothing mapped near the ends")
  all the way from `snapshot._area_from_json` through `Criteria.accepts_geometry`. The failure
  it prevents is specific and bad: with a plain bool, `--no-transit-access` against an old
  snapshot returns a *full list of routes* confidently labelled by a measurement that never
  happened, and `--transit-access` returns empty in a way that reads as "nowhere here is
  reachable by train" — absurd in a valley with a railway halt in it. So an active transit
  filter REJECTS an unknown route (the rule `accepts_gain` already applies to unmeasured gain),
  and `search_snapshot` logs that the *data* is missing, not the transport. The distinction is
  carried in the file format too: `AreaData.transit` defaults to `None`, a live parse always
  sets a list (empty is a real answer), and the loader reads the key with `.get`-style presence
  detection rather than an `[]` default.
- **Adding transit widened the Overpass query**, which invalidates the Overpass cache (the
  query text is the key) — the same price `poi.POI_KINDS` pays, and the reason transit and any
  new POI kinds are worth batching into one release rather than dribbling out.
- **Surface/tracktype are report-only, and that is why they need no tri-state filter.**
  `Hike.surface`/`Hike.tracktype` are `SurfaceSummary | None`, with `None` meaning "member-way
  tags were never fetched" (a pre-feature snapshot) as opposed to an empty summary meaning
  "fetched, nobody tagged it" — the same distinction `transit_access` draws. But nothing
  *filters* on them, so the dangerous case (a filter answering confidently from data that was
  never gathered) cannot arise; the renderer simply prints nothing.
- **Getting member-way tags costs a second Overpass statement.** A route relation carries no
  `surface`, and `out body geom` returns member geometry WITHOUT member tags — verified against
  the Špindl fixture, where 0 of 15 relations and 0 members carry one. `way(r); out tags;`
  returns them with no geometry, to be joined back by way id. Measured cost: 712 KB → 866 KB
  (+22 %) on a Krkonoše box. `way_tags` is stored PARALLEL to `ways` (index for index) rather
  than as a dict keyed by way id, because a relation can include the same way twice (an
  out-and-back leg) and a dict would collapse the pair and under-weight that surface.
- **Two gates on the surface flag, catching two different lies.** Coverage ≥ 50 % stops a
  mostly-untagged route from being described at all; dominance ≥ 40 % stops a plurality from
  posing as the answer (seen live: a route whose commonest surface was 21 % printed
  `surface:grass 21%`, which reads as "a grass walk" when four fifths of it is not — it now
  reads `surface:mixed`).
- **`sac_scale` and `trail_visibility` were measured and rejected, not overlooked.** On 689
  real member ways they are mapped at 4 % and 1 % (against `surface` 62 %, `tracktype` 45 %).
  A difficulty claim absent 96 % of the time reads as "easy" rather than "unknown", which is
  the failure mode this project spends most of its comments avoiding. Revisit if OSM coverage
  improves; the fetch mechanism is already in place, so it is a rendering decision now.
- **Surface on SYNTHESISED routes rides on the graph, not on `ways` — deliberately.**
  `--compose-loops`, `--around`, `--from`/`--to`, `--via` and `--to-poi` build their route
  dicts from contracted graph segments (`"ways": [route.coords]` — one assembled polyline),
  so there is no per-member list for `way_tags` to be parallel to and `measure_geometry`
  can only leave the summary at `None`. The tags travel on `Segment.step_tags` instead —
  one per *step* of the segment's polyline, `len(coords) - 1` of them — and
  `compose.assemble_tag_runs` turns a traversal back into the `(sub-polyline, tags)` pairs
  `surface.summarise_*` already consumes, with `search._attach_composed_surface` setting the
  result from outside `find_hikes` (the `anchor`/`destination` idiom). Five things to keep:
  - **The rejected alternative was splitting the polyline into tag-uniform "member ways"**
    so `measure_geometry` could do it unchanged. It puts a self-touching route — a
    `--via-loop` that falls back to a forced retrace, a live-verified case — into greedy
    stitching with four candidate ends at the revisited vertex; one wrong pairing trips
    export's ≥98 % faithfulness gate and a clean single track with per-point `<ele>`
    degrades to raw-ways with none. The measurement is still shared (`summarise_*` is called
    verbatim); only the call site is second.
  - **Step-aligned, not point-aligned**, which is why `assemble_loop_series` must NOT be
    reused: it drops the junction value shared between consecutive segments (`oriented[1:]`),
    right for N point-aligned values and off by one per segment for N−1 step-aligned ones.
  - **A `snap_points` split is exact, not prorated.** `_subpolyline` slices `step_tags[e1:e2+1]`,
    so a cut landing mid-asphalt gives each piece the surface actually under it. Prorating
    the parent's mix by piece length was the cheaper option and smears; pinned by a test
    where the two answers differ (3/7 vs 1/2).
  - **Traversal is per occurrence**, so a retraced leg is weighted twice exactly as its
    length is counted twice.
  - **`TrailGraph.has_way_tags` is presence detection**, not "is anything tagged": a
    pre-surface snapshot must keep reporting `None` ("we never looked") rather than a
    0 %-coverage summary ("nobody tagged it"), the same rule as `transit_access` and
    `AreaData.transit`. `snap_points` propagates it, or every point-based mode would go
    silent. The check lives in `assemble_tag_runs` so one place decides.
  `clip_routes_to_bbox` rebuilds `way_tags` alongside `ways` so the parallel invariant holds
  by construction — and it is now load-bearing rather than merely defensive, since
  `build_trail_graph` reads those tags. Every `build_trail_graph` call site in `search.py`
  passes clipped routes, so all five modes are tag-bearing.
  Micro-edge tag collisions (one physical edge claimed by several relations) resolve to the
  first route-then-way index carrying tags, so the graph's determinism guarantee holds.
- **Unrecognised `surface` values pass through verbatim**, e.g. a real route near Špindl tagged
  `surface=pfad, wurzeln, steine` (German free text stuffed into an enum field). It prints
  looking like a bug and is not one — it is what OSM says. Bucketing it into "other" would
  hide real data, and dropping it would silently *raise* the apparent coverage of everything
  else.
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
- `ruff check src tests` and `mypy` are both at zero and CI enforces it. Their config is in
  `pyproject.toml`; every ignore there carries the reason next to it, so widen the rule set
  by editing that block rather than by sprinkling `noqa` (an unexplained `noqa` naming a
  non-enabled rule is what `RUF100` deletes).

## Quick commands

```bash
pip install -e .             # CLI + web UI (no LLM); extras: ".[mcp]" ".[local-dem]" ".[dev]"
pytest -q                    # full offline suite (3 .sh launcher cases need bash; MCP skips without the extra)
ruff check src tests         # lint (config in pyproject.toml; CI runs this pinned)
mypy                         # type-check src/ (config in pyproject.toml; zero errors)
hike-finder --bbox 50.72 15.58 50.74 15.62 --user-agent you@example.com
hike-finder --list-pois      # the --poi / --to-poi kinds
hike-finder --list-areas     # what is already downloaded (the NAMED snapshot dir)
hike-finder --bbox 50.52 15.15 50.60 15.28 --poi ruins,castle --max-distance 25
hike-finder --from 50.73 15.60 --to-poi ruins --routes 3   # route TO the nearest ruin
hike-finder --clear-cache    # empty the on-disk cache; --no-cache bypasses it for a run
hike-finder-web              # local web UI on http://127.0.0.1:8765
hike-finder-mcp              # MCP server over stdio (needs the `mcp` extra)

# Launcher scripts (one per interface; set a default HIKE_OVERPASS_UA, forward args):
./scripts/cli.sh ...  | .\scripts\cli.ps1 ...    # -> hike-finder
./scripts/web.sh      | .\scripts\web.ps1        # -> hike-finder-web
./scripts/mcp.sh      | .\scripts\mcp.ps1        # -> hike-finder-mcp (stdout kept clean for JSON-RPC)
```
