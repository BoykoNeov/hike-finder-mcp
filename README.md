# hike-finder-mcp

An MCP server that finds **marked hiking routes from OpenStreetMap** and filters
them by **real, locally-computed elevation gain and distance** — not numbers
scraped from trail-description websites — plus **shape and access**: whether a
route is a loop, and whether you can reach it by **car** or **chairlift**.

It targets OSM route *relations* (`route=hiking`/`foot`), the same signed,
maintained trail data — including the Czech **KČT** network — that **mapy.cz**
renders. Distance and elevation gain are computed in this codebase, so the
numbers are consistent and tunable instead of inherited from a third party.

## Why this exists

Trail sites (AllTrails, Komoot, mapy.cz) all report *different* gain for the
same trail because elevation gain depends entirely on how you sample and
de-noise the terrain. This tool makes that step explicit and consistent: it
resamples each track to even spacing, smooths the elevation series, and counts
climbs with a hysteresis threshold so DEM noise isn't mistaken for ascent.

## Filters

`find_hikes(south, west, north, east, …)` takes these optional filters:

| Filter | Meaning | Confidence |
|--------|---------|------------|
| `min_gain_m` / `max_gain_m` | elevation gain bounds (m), computed locally | high |
| `min_distance_km` / `max_distance_km` | route length bounds | high |
| `circular` | `true` = loops only, `false` = point-to-point only | high |
| `car_access` | `true`/`false`: is `amenity=parking` mapped near a trail end? | best-effort |
| `chairlift_access` | `true`/`false`: is a ride-up aerialway (chairlift/gondola/cable car) mapped near a trail end? | best-effort |

The three boolean filters are **tri-state**: omit = don't care, `true` = require,
`false` = exclude. **Honesty note:** `car_access`/`chairlift_access` reflect OSM
*mapping*, not the world — a `false` means nothing of that kind is mapped near the
route's ends, not that it's impossible to get there. Loop detection is reliable.

Internally the search is two-pass: cheap geometry/shape/access filters run first
and a long through-route that merely crosses the area is dropped, so the
elevation backend is only queried for routes that already match.

## Two elevation backends (both supported)

| Mode | Source | Setup | Accuracy | Limits |
|------|--------|-------|----------|--------|
| `api` | Open-Elevation / OpenTopoData | none | coarser | rate-limited |
| `local` | SRTM/ASTER GeoTIFF tiles | download tiles once | high | none |
| `auto` | local if available, else api | optional tiles | best available | graceful fallback |

Set via `HIKE_ELEVATION_MODE`. See `src/hike_finder/config.py`.

## Quickstart

```bash
pip install -e ".[dev]"          # add ",local-dem" for the rasterio backend
pytest                            # core math is unit-tested
python -m hike_finder.server      # start the MCP server (stdio)
```

Point Claude Code / any MCP client at the command `python -m hike_finder.server`.

## Status

Core geometry, gain, access/shape math, and the Overpass response parser:
**implemented and unit-tested** (27 tests). The live network layers (the
Overpass HTTP call, elevation backends) and the MCP entry point:
**implemented, validate on a networked machine**. See `HANDOFF.md` for exactly
what's done and what's next.
