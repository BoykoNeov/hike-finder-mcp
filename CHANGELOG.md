# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Hike to something — filter routes by the objects they pass** (`--poi KIND`, MCP `poi`,
  web "Must pass"). Distance and gain were already queryable; this adds the *destination*
  dimension: "a 12 km hike, 400 m of climbing, that goes to a ruin." Eighteen kinds
  (`--list-pois`): churches & chapels, wayside shrines & crosses, ruins, castles & forts,
  memorials, archaeological sites, summits, rock formations, caves, springs, waterfalls,
  viewpoints, lookout towers, museums, mountain huts, shelters, picnic sites, and
  pubs & restaurants. Several kinds are OR-ed. Each result reports what it reaches and how
  far off the trail it sits (`passes castle "Valdštejn" (85 m)`); the web UI pins them on
  the map. `--poi-radius` / `HIKE_POI_RADIUS_M` (default 250 m) sets how close counts.
  - Works in **every** mode — a bbox search, `--compose-loops`, the point-based modes, and
    an offline `--area` search — because "does it pass a ruin?" is a property of the route,
    not of how the route was found.
  - Proximity is measured to the trail **line**, not to its mapped nodes: a straight member
    way with two nodes kilometres apart still reports its true closest approach.
  - The filter runs in the **cheap** pass, so a POI-filtered search spends *less* elevation
    budget than the same search without it.
  - Honest about misses, as with car/lift access: no match means nothing of that kind is
    *mapped* in OSM near a route, not that nothing is there. An unknown kind is a loud
    error, never a silent empty result.
- **See which areas you have already downloaded** (`--list-areas`, MCP `list_areas`, web
  "Already downloaded"). Name, bounding box, when it was fetched, and what it holds
  (routes, elevation samples, points of interest, file size). The web UI **outlines every
  downloaded area on the map** and lets you click one to search it offline. `--area` now
  also accepts a bare area *name*, not only a path. Areas downloaded before this release
  carry no points of interest and are flagged for re-download rather than silently failing
  a `--poi` search.
- **Draw a box to pick the area** (web UI). Drag a rectangle on the map instead of relying
  on however the map happens to be panned; the size is shown in km before you spend a
  query on it. The **same** box drives both the download and the search, so you can no
  longer download one rectangle and search another. With nothing drawn it falls back to the
  map view, exactly as before.

### Changed

- The Overpass query now also fetches the registered points of interest, on every request.
  This is what lets one snapshot answer any destination question later, and keeps a single
  Overpass cache key. **It changes the query text, so every cached Overpass area is
  invalidated and re-fetched once.** Elevation caches are unaffected.
- Snapshots gained an optional `pois` key (no version bump — older files load unchanged and
  simply report no points of interest).

- **Point-based route drawing — pick point(s) on a map instead of a bounding box.** Three
  new live modes, each deriving its own search area from the point(s) (no `--bbox`):
  - **Circular routes near a point** (`--around LAT LON`, MCP `circular_routes`, web "Mode →
    Circular routes near a point"). Reuses the loop-composition engine anchored to the picked
    point: only loops passing within `--around-radius` metres (default 1000,
    `HIKE_AROUND_RADIUS_M`) survive, each started at the on-loop spot nearest the point. Loop
    *length* comes from `--min-distance`/`--max-distance` (a length band, not a geofence).
    Composes with `--car-access`/`--chairlift-access`.
  - **N shortest routes between two points** (`--from LAT LON --to LAT LON`, MCP
    `routes_between`, web "Mode → Routes between two points"). Yen's k-shortest-loopless-paths
    on the trail graph, with each point snapped onto the nearest trail by **splitting the
    segment at the projected spot** (so a route reaches exactly where you pointed, not the next
    junction). Returns the shortest route first, then the next, up to `--routes N` (default 3,
    `HIKE_ROUTES_K`); `--max-distance` caps a route's length. Results are N *distinct* routes
    (a candidate re-using more than `HIKE_ROUTES_OVERLAP_FRAC`, default 0.6, of an already-kept
    route's length is skipped — set 0 for literal k-shortest). A point more than
    `HIKE_ROUTES_MAX_SNAP_KM` (default 2 km) from any trail is treated as off-network and
    yields no routes rather than routing to a distant trail.
  - **One route linking several points** (`--via LAT LON`, repeatable, plus `--via-loop`; MCP
    `route_via`; web "Mode → Route linking several points"). Snaps two-or-more picked points to
    the network (same mid-segment snapping) and chains shortest paths between them **in the
    order given** (no reordering) into a single route. With `--via-loop` it closes the route
    back to the first point into a **circular route that avoids retracing**: each leg is routed
    with the segments used by earlier legs removed from the graph, so the loop is edge-disjoint
    where the network allows and retraces only a leg with no disjoint alternative. The retraced
    fraction is measured and logged (0 % = a clean non-repeating loop); a circuit with no
    disjoint return is flagged as a largely out-and-back rather than presented as a loop. Same
    off-network and corridor-clip limits as `--from`/`--to`; `--min`/`--max-distance` filter the
    linked route by total length. Web adds an *Undo last point* button and a *Close into a
    circular route* checkbox.

  All measure elevation/distance/access through the *unchanged* two-pass engine (offline ==
  online holds), export to GPX/GeoJSON like any route, and are wired into all three frontends
  (CLI, MCP, web). The pure engine (mid-segment snapping + Yen on the junction multigraph, and
  the chained-Dijkstra `route_via` with its non-retracing loop closure) is unit-tested on
  hand-built graphs and, via the Špindl fixture, offline end-to-end; all three modes are
  verified live against real Overpass + the elevation API (Krkonoše) — including `--via-loop`
  drawing a genuine ~13 km non-repeating triangle loop (9 % retraced, gain ≈ loss) and honestly
  flagging a near-collinear set of points as a 100 %-retrace out-and-back.

## [0.2.0] - 2026-06-29

### Added

- **GPX/GeoJSON export now embeds per-point elevation.** When a route's elevation
  was computed, the export carries the full profile: GPX writes a single
  walking-order `<trkseg>` with an `<ele>` on every `<trkpt>`, and GeoJSON writes a
  3D `[lon, lat, ele]` line (RFC 7946's optional altitude element). This is recorded
  only when the stitched walking line faithfully covers every member way, so a
  fragmented relation whose stitch drops legs still exports its full raw geometry
  (no elevation) rather than a track missing legs. Composed loops (a single
  synthesised ring) carry their profile too. Gain/loss and all other output are
  unchanged.

- **Reverse-geocode naming (opt-in).** Routes with no OSM `name`/`ref` (which fall
  back to `route/<id>`) can be labelled from the place names at their ends — e.g.
  `Labská → Špindlerův Mlýn` or `loop near <town>` — via Nominatim. Off by default
  (`--name-places` / a web checkbox / a `name_places` MCP arg / `HIKE_GEOCODE=1`),
  since Nominatim's usage policy is strict: a ≥1 req/s throttle, a contact User-Agent,
  and only the *matched* routes are looked up, cached so a coordinate is fetched at
  most once. The derived label is carried separately (`Hike.place_name`); the truthful
  OSM `name`/`ref` are untouched and the route is marked `unnamed` so a geocoded label
  is never mistaken for a signed trail name. Endpoint configurable (`HIKE_NOMINATIM_URL`).

- **Offline naming — bake place names into a snapshot at download time.** Passing the
  naming opt-in to a download (`--name-places --download`, the web naming checkbox while
  downloading, or `name_places` on the MCP `download_area` tool) now reverse-geocodes the
  unnamed routes and records the place names into the snapshot, so a later offline `--area`
  search labels them with **zero network** — the same way a download already warms
  elevation. A snapshot downloaded without the opt-in (or an older snapshot) keeps the
  honest no-op: an offline naming request on it logs that it has no baked names and to
  re-download. Existing snapshots remain readable (the on-disk format is unchanged and the
  new place-name map is optional).

- **Loop composition drops degenerate "sliver" loops.** A hard Polsby–Popper
  compactness floor (`HIKE_COMPOSE_MIN_COMPACTNESS`, default `0.05`) removes
  near-zero-area loops (an out-and-back along two near-parallel trails) before the
  near-duplicate collapse and the result cap, so a sliver can neither sway a collapse
  nor consume a returned slot. The default is a no-op on real data (observed minimum
  compactness ~0.18 on a wide bbox); the dropped count is logged, never silent.

### Changed

- **Local DEM tiles are now mosaicked through a GDAL VRT instead of an in-memory
  merge.** Multiple GeoTIFF tiles in `HIKE_DEM_DIR` are assembled into a virtual
  raster that is point-sampled, so memory stays flat regardless of region size
  (the previous `rasterio.merge` loaded the whole mosaic and didn't scale). The
  VRT is built directly from the tiles' georeferencing — no `gdalbuildvrt` CLI or
  `osgeo` bindings needed (neither ships with the `local-dem` extra). A
  user-supplied `*.vrt` in the directory is used as-is (escape hatch for
  mixed-resolution tiles needing resampling); mixed CRS/resolution otherwise
  raises a clear error rather than silently misregistering.

### Fixed

- **Local DEM nodata was read from the first tile only**, so a void/ocean pixel
  in any other tile could leak a raw value. Each VRT source now declares its own
  nodata, masked against a single band nodata value, so voids in any tile resolve
  correctly.

## [0.1.0] - 2026-06-24

Initial development release. Find marked hiking routes from OpenStreetMap and
filter them by locally-computed elevation gain, distance, loop shape, and access.

### Added

- **Core search.** Query OSM route relations (`route=hiking`/`foot`, including the
  Czech KČT network) in a bounding box; compute distance and elevation gain/loss
  in-codebase by resampling each track to even spacing, smoothing, and counting
  climbs with a hysteresis threshold so DEM noise isn't mistaken for ascent.
- **Filters.** Gain bounds, distance bounds, `circular` (loops vs point-to-point),
  and tri-state `car_access` / `chairlift_access` (parking / ride-up aerialway
  mapped near a route's ends). Two-pass: cheap shape/access filters run before the
  elevation backend is queried.
- **Three frontends on one engine.** A command-line tool, a local web UI (pan a
  map to your area), and an MCP server for LLM clients. CLI and web need no LLM.
- **Two elevation backends.** An API backend (OpenTopoData / open-elevation) with
  retry/backoff, rate-limit knobs, and a persistent daily-request counter; and a
  local DEM backend (Copernicus GLO-30 via rasterio) that is fast, free, offline.
- **Near-miss results.** When a query returns little or nothing, list routes that
  *just* miss, each annotated with how it falls short.
- **Saved areas.** Download an area once, then search the saved snapshot offline
  with zero API calls (offline results are byte-identical to live).
- **Transparent SQLite caching.** Sits at the Overpass and elevation network seams
  so repeat/overlapping searches don't re-hit the public servers; on by default,
  failure-isolated, with `--no-cache` / `--clear-cache`.
- **Loop composition.** Synthesize loops from connected marked trails
  (`--compose-loops`), including access-anchored loops ("a loop from where I park")
  and segment-level shared elevation sampling to cut redundant lookups.
- **GPX / GeoJSON export.** Serialize matched and composed routes to GPX 1.1 and
  RFC 7946 GeoJSON for loading into a phone/GPS, wired into all three frontends;
  the web map also draws each route line.
- **Reliable loop detection** via a full vertex graph (handles T-junctions), with
  termini-based access and start-point coupling to matched parking/lifts.
- **Thin per-interface launchers** in `scripts/` (cli/web/mcp × `.sh` + `.ps1`).

### Project hygiene

- MIT license, CI (GitHub Actions running the test suite on Linux 3.10–3.13 plus a
  Windows smoke job), and this changelog.

[Unreleased]: https://github.com/BoykoNeov/hike-finder-mcp/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/BoykoNeov/hike-finder-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/BoykoNeov/hike-finder-mcp/releases/tag/v0.1.0
