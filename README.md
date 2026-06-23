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

## Using it

### 1. Register the server with an MCP client

The server speaks MCP over stdio. Register the command `python -m hike_finder.server`
(or the installed `hike-finder` console script).

**Claude Code (CLI):**

```bash
claude mcp add hike-finder --env HIKE_OVERPASS_UA=you@example.com -- python -m hike_finder.server
```

**`.mcp.json` / Claude Desktop config (equivalent):**

```json
{
  "mcpServers": {
    "hike-finder": {
      "command": "python",
      "args": ["-m", "hike_finder.server"],
      "env": { "HIKE_OVERPASS_UA": "you@example.com" }
    }
  }
}
```

`HIKE_OVERPASS_UA` is **effectively required**: the public Overpass server rejects
the default Python User-Agent with `406 Not Acceptable`. Set it to a real contact
(email or project URL) per [OSM etiquette](https://operations.osmfoundation.org/policies/nominatim/).

> This is the standard MCP registration form; it isn't live-verified in this repo
> (no `mcp` SDK installed in the build env). The SDK's decorator API has shifted
> across versions — if the server won't start, check the imports in
> `src/hike_finder/server.py` against your installed `mcp` version (see `HANDOFF.md`).

### 2. Call the tool

Ask your MCP client for hikes in a bounding box. For example, "find loop hikes near
Špindlerův Mlýn reachable by chairlift" makes the client call:

```text
find_hikes(south=50.72, west=15.58, north=50.74, east=15.62,
           circular=true, chairlift_access=true)
```

Each match comes back as one line:

```text
<name> — <km> km, +<gain> m / -<loss> m [loop, car, lift:chair_lift] (start <lat>,<lon>, OSM relation <id>)
```

The `[...]` flags are always present: `loop`/`one-way`, then `car` and/or
`lift:<type>` when access is mapped near an endpoint.

> **Validated live** (2026-06-23): this exact bbox returned 15 routes / 31 parking
> / 5 lifts, and the route *Špindlmanova mise* came back flagged `car` +
> `lift:chair_lift`. Gain/loss numbers depend on the elevation backend, which is
> not yet live-validated — see `HANDOFF.md`.

### 3. Getting a bounding box

The tool takes four corners in the order **`south, west, north, east`**
(min latitude, min longitude, max latitude, max longitude). To get them:

- **openstreetmap.org → "Export" tab** draws a draggable box and shows its four
  edges — copy them straight in.
- Or read the corners off **mapy.cz** for the area you're planning.

### Configuration (environment variables)

All optional except where noted; defaults come from `src/hike_finder/config.py`.

| Variable | Meaning | Default |
|----------|---------|---------|
| `HIKE_OVERPASS_UA` | User-Agent for Overpass — **required by the public server**; use a real contact | generic UA naming no contact |
| `HIKE_OVERPASS_URL` | Override the Overpass endpoint (use a regional/self-hosted instance for heavy use) | `overpass-api.de` |
| `HIKE_ELEVATION_MODE` | `api` \| `local` \| `auto` | `auto` |
| `HIKE_DEM_DIR` | GeoTIFF DEM tile directory (for `local`/`auto`) | — |
| `HIKE_API_ENDPOINT` | Override the elevation API endpoint | provider default |
| `HIKE_GAIN_THRESHOLD` | Hysteresis climb threshold, metres (must exceed peak-to-peak DEM noise) | `10` |
| `HIKE_SAMPLE_INTERVAL` | Resample spacing along the track, metres | `25` |
| `HIKE_SMOOTH_WINDOW` | Elevation smoothing window, samples | `3` |
| `HIKE_LOOP_TOLERANCE` | start≈end distance that closes a loop, metres | `150` |
| `HIKE_CAR_RADIUS` | Parking-near-endpoint radius, metres | `300` |
| `HIKE_LIFT_RADIUS` | Lift-station-near-endpoint radius, metres | `400` |
| `HIKE_MAX_ROUTE_FACTOR` | Drop routes longer than this × the bbox diagonal (kills through-routes) | `4.0` |

### Troubleshooting

- **`406 Not Acceptable` / every Overpass request fails** → set `HIKE_OVERPASS_UA`
  to a real contact. The public server rejects the default Python User-Agent.
- **No hikes returned** → widen the bbox or loosen the filters. Note that loops are
  genuinely sparse in KČT data (most relations are linear marked segments), so
  `circular=true` legitimately returns few results.
- **Slow / occasional `504`** → public Overpass overload; the client retries with
  backoff. Point `HIKE_OVERPASS_URL` at a regional instance for heavy use.

## Status

Core geometry, gain, access/shape math, and the Overpass response parser:
**implemented and unit-tested** (27 tests). The live network layers (the
Overpass HTTP call, elevation backends) and the MCP entry point:
**implemented, validate on a networked machine**. See `HANDOFF.md` for exactly
what's done and what's next.
