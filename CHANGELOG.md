# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/BoykoNeov/hike-finder-mcp/releases/tag/v0.1.0
