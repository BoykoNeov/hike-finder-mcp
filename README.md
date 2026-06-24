# hike-finder-mcp

Find **marked hiking routes from OpenStreetMap** and filter them by **real,
locally-computed elevation gain and distance** — not numbers scraped from
trail-description websites — plus **shape and access**: whether a route is a loop,
and whether you can reach it by **car** or **chairlift**.

It runs three ways on one engine: a **command-line tool**, a **local web UI** (a
map you pan to your area), or an **MCP server** for LLM clients. The CLI and web
UI need **no LLM and no MCP client** — they're plain standalone programs.

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
| `api` | Open-Elevation / OpenTopoData | none | coarser | rate-limited (per-sec throttle + daily counter, both managed) |
| `local` | SRTM/ASTER GeoTIFF tiles | download tiles once | high | none |
| `auto` | local if available, else api | optional tiles | best available | graceful fallback |

Set via `HIKE_ELEVATION_MODE`. See `src/hike_finder/config.py`.

## Quickstart

```bash
pip install -e .                   # CLI + web UI; no LLM / MCP stack required

# browser: pan a map to your area, set filters, search
hike-finder-web                    # then open http://127.0.0.1:8765

# terminal: one command, prints results
hike-finder --bbox 50.72 15.58 50.74 15.62 --circular --user-agent you@example.com
```

## Using it

Three frontends, one engine. **The CLI and web UI need no LLM and no MCP client.**

### Install

```bash
pip install -e .                  # base: the `hike-finder` CLI and `hike-finder-web` UI
pip install -e ".[mcp]"           # + the MCP server (`hike-finder-mcp`)
pip install -e ".[local-dem]"     # + the local GeoTIFF DEM elevation backend (needs rasterio)
pip install -e ".[dev]"           # + pytest
```

Extras combine: `pip install -e ".[mcp,local-dem]"`.

**Set a contact for Overpass.** OSM's public server rejects the default User-Agent
with `406`. Provide a real email/URL via `--user-agent` (CLI), the Contact field
(web UI), or `HIKE_OVERPASS_UA=you@example.com` in the environment — per
[OSM etiquette](https://operations.osmfoundation.org/policies/nominatim/).

### Option A — Web UI (easiest; no coordinates to type)

```bash
hike-finder-web                   # serves http://127.0.0.1:8765 (--host/--port to change)
```

Open it, **pan/zoom the map to your area**, fill in the contact field, choose
filters (shape, car/chairlift access, gain and distance ranges), then click
**"Search this map area"**. Matches are listed and pinned at their start point —
click one to jump to it. This is the answer to "how do I get a bounding box": you
draw it by moving the map. Pure standard library, no web-framework dependency.

### Option B — Command line

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 \
            --circular --chairlift-access \
            --user-agent you@example.com
```

`--bbox` is **`south west north east`** (min-lat min-lon max-lat max-lon). The
three boolean filters are **tri-state**: omit = don't care, `--circular` = require,
`--no-circular` = exclude (same for `--car-access` and `--chairlift-access`).
Numeric filters: `--min-gain`/`--max-gain` (m), `--min-distance`/`--max-distance`
(km). Add `--json` for machine-readable output. `hike-finder --help` lists all.

Each match prints as one line:

```text
<name> — <km> km, +<gain> m / -<loss> m [loop, car, lift:chair_lift] (start <lat>,<lon>, OSM relation <id>)
```

The `[...]` flags: `loop`/`one-way`, then `car` and/or `lift:<type>` when access
is mapped near an endpoint.

### Option C — MCP server (drive it from an LLM client)

Needs the `mcp` extra. Register the `hike-finder-mcp` command:

```bash
claude mcp add hike-finder --env HIKE_OVERPASS_UA=you@example.com -- hike-finder-mcp
```

**`.mcp.json` / Claude Desktop config (equivalent):**

```json
{
  "mcpServers": {
    "hike-finder": {
      "command": "hike-finder-mcp",
      "env": { "HIKE_OVERPASS_UA": "you@example.com" }
    }
  }
}
```

Then ask in plain language ("find loop hikes near Špindlerův Mlýn reachable by
chairlift") and the client calls `find_hikes(south, west, north, east, …)` with
the same filters as the CLI.

> The MCP registration form isn't live-verified in this repo (the build env has no
> `mcp` SDK). The SDK's decorator API has shifted across versions — if the server
> won't start, check the imports in `src/hike_finder/server.py` against your
> installed `mcp` version (see `HANDOFF.md`).

### Getting a bounding box (CLI / MCP)

The web UI gives you the box for free. For the CLI or MCP you supply four corners
in the order **`south, west, north, east`** (min latitude, min longitude, max
latitude, max longitude):

- **openstreetmap.org → "Export" tab** draws a draggable box and shows its four
  edges — copy them straight in.
- Or read the corners off **mapy.cz** for the area you're planning.

> **Validated live** (2026-06-23): the bbox `50.72,15.58,50.74,15.62` (Špindlerův
> Mlýn) returned 12 routes (each flagged for `car`/`lift`/shape), with a computed
> gain/loss for **every** one — e.g. *[Z] Richtrovy Boudy → Špindlerův mlýn* at
> **+678 m / −251 m**. The detected loop *Špindlerův mlýn – okruh* came back
> **+34 m / −34 m** — gain ≈ loss, exactly as a closed loop must, which
> cross-checks the whole sampling/gain pipeline. Loop detection was also
> validated live (2026-06-23) against the real "Medvěd*" relations — which caught
> and corrected an over-reporting bug; closure now reads the member ways as a
> vertex graph (circuit rank), independent of way-stitching. Distance was also
> hardened here (2026-06-23): it now sums every member way's length rather than
> the greedily-stitched line, so branched relations that the stitch couldn't
> chain no longer under-count (validated live by a per-route stitched-vs-summed
> diff). The trail's **start and car/lift endpoints** were hardened the same way
> (2026-06-24): they now come from the route's genuine termini (the degree-1
> vertices of that same vertex graph), so a branched relation whose stitch drops
> members no longer tests access at the wrong ends — validated live against the
> "Medvěd*" relations, where the branched *Medvědí okruh* (42% stitch coverage)
> recovers all four real trailheads. Remaining caveat: the **local DEM** backend
> (`mode=local`) is still untested — tracked in `HANDOFF.md`.

### Configuration (environment variables)

All optional except where noted; defaults come from `src/hike_finder/config.py`.

| Variable | Meaning | Default |
|----------|---------|---------|
| `HIKE_OVERPASS_UA` | User-Agent for Overpass — **required by the public server**; use a real contact | generic UA naming no contact |
| `HIKE_OVERPASS_URL` | Override the Overpass endpoint (use a regional/self-hosted instance for heavy use) | `overpass-api.de` |
| `HIKE_ELEVATION_MODE` | `api` \| `local` \| `auto` | `auto` |
| `HIKE_DEM_DIR` | GeoTIFF DEM tile directory (for `local`/`auto`) | — |
| `HIKE_API_ENDPOINT` | Override the elevation API endpoint | provider default |
| `HIKE_API_MIN_INTERVAL` | Min seconds between elevation-API requests (keeps you under the public ~1 req/sec limit) | `1.1` |
| `HIKE_API_MAX_RETRIES` | Retries on transient API errors (429 / 5xx / network), with exponential backoff honouring `Retry-After` | `3` |
| `HIKE_API_BACKOFF` | Backoff base seconds, doubled each retry | `2.0` |
| `HIKE_API_MAX_BACKOFF` | Cap on any single wait, seconds; a `Retry-After` above this (e.g. a daily-quota 429) makes the route degrade to `n/a` instead of stalling | `30` |
| `HIKE_API_DAILY_LIMIT` | Max elevation-API requests per UTC day, counted in a persistent file across runs; at the cap, routes degrade to `n/a` instead of getting the IP banned. `0` disables tracking | `1000` |
| `HIKE_API_STATE_DIR` | Directory holding the daily-counter file | per-user cache (`%LOCALAPPDATA%/hike-finder` or `~/.cache/hike-finder`) |
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

Core geometry, gain, access/shape math, the Overpass response parser, the
elevation-API client (including its rate-limit throttle, transient-error
retry/backoff, and a persistent daily-request counter that degrades to `n/a`
before blowing the API's daily cap), and the CLI argument/formatter layer:
**implemented and unit-tested** (72 tests, all offline). The Overpass HTTP call **and the API
elevation backend** are **validated live** (CLI + web), with computed gain
cross-checked against the loop invariant (gain ≈ loss). The local-DEM backend
and the MCP entry point are **implemented; validate on a networked machine**.
See `HANDOFF.md` for exactly what's done and what's next.
