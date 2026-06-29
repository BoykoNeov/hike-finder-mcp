# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
