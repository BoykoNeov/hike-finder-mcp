# hike-finder-mcp

[![CI](https://github.com/BoykoNeov/hike-finder-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/BoykoNeov/hike-finder-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

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

> **New here? Read [`GUIDE.md`](GUIDE.md)** — a verbose, step-by-step walkthrough
> covering what to do, why, what output to expect, and how to read the results.
> This README is the terse reference (full flag list, every env var, the filter
> table); the guide is the tutorial.

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

### Near-miss results (close-but-not-matching)

When a query returns little or nothing, the search can also list routes that
*just* miss — each flagged and annotated with **how** it falls short, so a "close"
route is never mistaken for a match:

```text
~ 0402 — 9.86 km, +709 m / -327 m [one-way, lift:chair_lift] (…)  [near miss: gain 709 m — 41 m below the 750 m minimum]
```

A route qualifies when it is within tolerance of a numeric bound (gain within a
percentage, distance within a few km) **or** has parking/a lift just past its
access radius (`nearest parking 380 m away — just past the 300 m limit`).
Shape is never relaxed — a loop is not "almost point-to-point" — and an *excluded*
access stays strict, so near-misses always share the shape and exclusions you
asked for. By default they appear **only when nothing matches** (`auto`); you can
force them always on or off. Tolerances are tunable (see the env vars below).

### Saved areas — fetch once, search offline (no API calls)

Exploring one area with several filters re-hits Overpass and the elevation API
each time. Instead, **download the area once** and search the saved copy offline:

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 --download krkonose.json   # one fetch + elevation warm-up
hike-finder --area krkonose.json --min-gain 600 --circular            # offline, zero API calls
hike-finder --area krkonose.json --max-distance 8 --car-access        # …re-filter freely
```

`--download` fetches the routes once and computes elevation for **every** plausible
route (it spends the elevation budget up front, since you download before knowing
your filters), saving geometry + elevation to a JSON snapshot. `--area` then runs
the *same* engine against the snapshot with **no network at all** — results are
identical to a live search by construction (validated: offline gains match a live
search byte-for-byte). Only the sample interval is frozen into the snapshot; gain
threshold, smoothing, access radii and shape tolerance stay tunable offline. The
web UI exposes this as **"Download view"** + a saved-area selector; MCP gains a
`download_area` tool and an `area` argument on `find_hikes`.

### Transparent cache (automatic, on by default)

Even without an explicit snapshot, a **transparent on-disk cache** (SQLite, stdlib
only) sits at the two network seams so you don't re-hit the public servers when you
re-run or pan around an area:

- **Elevation** points are cached forever (terrain doesn't change) and — because a
  route relation carries its full geometry regardless of the query box — they're
  reused even across *different overlapping* bounding boxes, not just exact re-runs.
- **Overpass** areas are cached with a time-to-live (`HIKE_OVERPASS_CACHE_TTL_DAYS`,
  default 30 days; trails change slowly).

It's invisible — cached runs return exactly what a live run would — and it's
fail-safe: any cache error degrades to a normal live fetch. Disable it for a run
with `--no-cache` (or `HIKE_CACHE=0`); empty it with `hike-finder --clear-cache`.
This is what makes repeat exploration cheap *and* keeps the tool a polite OSM
citizen. (Unlike a snapshot, the cache isn't a portable file you manage — it's just
plumbing. A `--download` snapshot stays the way to search a fixed area fully offline.)

### Composing loops (stitch connected trails into a day-loop)

Most KČT relations are *linear* marked segments (a coloured trail A→B); a circular
day-hike is usually an ad-hoc combination of several connected segments. So
`circular=true` only finds the few loops mapped as a single relation, and legitimately
returns little. **Compose mode** instead builds one graph from *every* relation's member
ways and searches it for cycles of a target length — synthesising loops that aren't
mapped as a single trail:

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 --compose-loops \
            --min-distance 5 --max-distance 12 --user-agent you@example.com
```

Each result is stitched from several marked trails, so it has **no single OSM relation
id** — it's rendered with its constituent trails instead:

```text
Composed loop — 9.86 km, +540 m / -538 m [loop, car] (start 50.73,15.61, composed of 0402 + 1801 + Medvědí okruh)
```

The target length comes from `--min-distance`/`--max-distance` (default 3–15 km).
Composed loops are kept **inside the searched bbox** (a loop that would wander out on a
through-route is excluded), so widen the area for longer loops. On a dense area there can
be dozens of candidates; degenerate near-zero-area **slivers** (an out-and-back along two
near-parallel trails) are dropped outright by a compactness floor (`HIKE_COMPOSE_MIN_COMPACTNESS`),
then the tool returns the **15 most loop-like** of the rest (ranked by compactness, so thin
shapes sink — tune with `HIKE_COMPOSE_MAX_LOOPS`) and logs how many distinct loops it found
(and how many slivers it dropped). Elevation, distance, and car/lift access are
computed exactly as for a real route, and a composed loop is circular by construction
(gain ≈ loss). The web UI exposes this as a **"Compose loops from connected trails"**
checkbox; MCP via a `compose_loops` argument on `find_hikes`.

Add `--car-access` (or `--chairlift-access`) to get **"a loop from where I park"**: only
loops reachable from a mapped parking lot / lift survive, each started at that trailhead.
The reachability test runs *before* the compactness cap, so the returned loops are ones
you can actually drive/ride to (otherwise the cap can fill with compact loops far from any
trailhead). The loop geometry — and its gain/loss — is unchanged; only the start moves.

> **Honesty note:** a composed loop is a *suggestion* — it asserts only that these
> connected marked segments form a loop of that length, not that anyone signs or walks it
> as one route. Loop closure itself is high-confidence (exact shared OSM nodes); the
> composition is geometric, not editorial.

> **Use a local DEM for compose.** Composed loops are long (8–15 km), so each one needs
> hundreds of elevation samples. On the **public elevation API** (throttled to ~1
> request/second, batched 100 points/request) a default compose run is **slow** — dozens
> of requests, roughly a minute cold — but it stays well under the daily cap (a default
> 15-loop run is on the order of 50 requests, not 1000). The cap only becomes a real risk
> if you raise `HIKE_COMPOSE_MAX_LOOPS` far past the default or do many runs, in which
> case later loops degrade to `gain n/a`. Either way, for fast, unlimited elevation on
> every composed loop, point it at a
> [local DEM](#two-elevation-backends-both-supported) (`HIKE_ELEVATION_MODE=local`).

### Point-based route drawing (pick point(s) on a map, get routes)

Two modes that take **points instead of a bounding box** — you don't draw a box, you drop
a pin (or two). Both derive their own search area from the point(s), so **omit `--bbox`**.

**Circular routes near a point** (`--around LAT LON`) — "draw me a ~10 km loop starting
*here*":

```bash
hike-finder --around 50.73 15.60 --min-distance 8 --max-distance 12 \
            --user-agent you@example.com
```

It reuses the loop-composition engine, but anchored to your point: only loops that pass
within `--around-radius` metres of it survive (default 1000; also `HIKE_AROUND_RADIUS_M`),
and each loop is **started at the on-loop spot nearest your point**. Combine with
`--car-access` / `--chairlift-access` to also require a trailhead near the loop.

> **"within a set distance boundary" = total loop *length*, not a geofence.** The
> `--min-distance`/`--max-distance` band (default 3–15 km) sets how *long* the loop is, not
> how far it may stray — a 12 km loop anchored at your point can still roam a few km away.
> The point is where the loop *passes through and starts*, controlled by `--around-radius`.

**N shortest routes between two points** (`--from LAT LON --to LAT LON`) — "how do I walk
from A to B, and what are my options":

```bash
hike-finder --from 50.72 15.58 --to 50.76 15.63 --routes 3 \
            --user-agent you@example.com
```

Each point is snapped onto the nearest marked trail (splitting it at the projected spot, so
a route reaches exactly where you pointed — not the next junction kilometres away), then the
tool draws the **shortest route first, then the next-shortest**, and so on. `--routes N`
(default 3; also `HIKE_ROUTES_K`) sets how many; `--max-distance` caps a route's length.

> **`--routes N` returns N *distinct* routes, not the literal 2nd/3rd shortest.** A candidate
> that re-uses more than `HIKE_ROUTES_OVERLAP_FRAC` (default 0.6) of an already-kept route's
> length is skipped, so you get genuinely different alternatives rather than the same line
> ± one segment. Set `HIKE_ROUTES_OVERLAP_FRAC=0` for literal k-shortest.
>
> **Known limitations.** A point more than ~2 km from any trail (`HIKE_ROUTES_MAX_SNAP_KM`)
> is treated as off-network and yields no routes, rather than silently routing to a distant
> trail. And the fetched area is a corridor padded `max(2 km, 0.4×separation)` around the two
> points (`HIKE_ROUTES_PAD_KM`/`HIKE_ROUTES_PAD_FRAC`): a longer *alternative* that bows well
> outside that corridor can be clipped, so raise those knobs if a detour you expect is
> missing.

Both modes are **live-map only** and exposed on every frontend: the web UI has a **Mode**
selector (pick "Circular routes near a point" or "Routes between two points", then click the
map to drop your pin(s)); MCP has the `circular_routes` and `routes_between` tools. Results
carry full computed stats and export to GPX/GeoJSON like any other route.

### Export — GPX / GeoJSON (load into your phone or GPS)

Once a search (live, offline `--area`, or `--compose-loops`) gives you routes you like,
hand them off to the device you'll actually navigate with. `--gpx` / `--geojson` write
the **matched + composed routes** (near-misses included, flagged) to a file *alongside*
the normal output:

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 --circular --gpx loops.gpx     # text + a GPX file
hike-finder --area krkonose.json --min-gain 600 --geojson picks.geojson    # offline, still exports
hike-finder --bbox 50.72 15.58 50.74 15.62 --compose-loops --gpx day.gpx   # composed loops too
```

- **GPX 1.1** — one `<trk>` per route plus a `<wpt>` at each start (the trailhead you
  drive/ride to). Loads into Komoot, OsmAnd, Gaia GPS, Garmin, **mapy.cz**, …
- **GeoJSON** (RFC 7946) — a `FeatureCollection` of route lines carrying the full computed
  stats in `properties` (gain/loss, distance, shape, access, provenance).

When a route's elevation was computed, the exported track carries the **full per-point
profile** — GPX puts an `<ele>` on every point of one clean walking-order track; GeoJSON
writes 3D `[lon, lat, ele]` coordinates. For a fragmented relation whose legs can't be
stitched into one line, the export instead falls back to the **raw mapped geometry** (every
member way, no elevation) so it keeps all legs and matches the reported distance rather than
shipping a track missing legs. The web UI has **Download GPX / Download GeoJSON** buttons
(and draws the route lines on the map); MCP's `find_hikes` takes a `format:
"gpx"｜"geojson"` argument that returns the file as text.

### Naming unnamed routes (reverse geocoding)

Most KČT relations carry a `name` or `ref`, but some carry **neither** and show up as
the synthetic `route/<id>`. Opt in to label those from the **place names at their ends**:

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 --name-places --user-agent you@example.com
```

```text
Labská → Špindlerův Mlýn — 7.65 km, +312 m / -180 m [one-way, car, lift:chair_lift] (start 50.7069,15.6166, unnamed OSM relation 6282997)
```

A point-to-point route reads `<start place> → <end place>`, a loop reads `loop near
<place>`. It's **off by default** (also `HIKE_GEOCODE=1`) because
[Nominatim's usage policy](https://operations.osmfoundation.org/policies/nominatim/) is
strict — so it throttles to ≤1 request/second, sends your contact as the User-Agent, only
looks up the routes that already **matched** (not every candidate), and **caches** every
coordinate so a trailhead is geocoded at most once across runs. A derived label never
overwrites the real OSM `name`/`ref` (those stay truthful in `--json`); the identifier
clause says `unnamed OSM relation <id>` so a geocoded label is never mistaken for a signed
trail name. The web UI exposes a **"Name unnamed routes from places"** checkbox; MCP a
`name_places` argument. Point `HIKE_NOMINATIM_URL` at your own instance for heavy use.

> **Honesty note:** a place-derived label is a *convenience*, not the route's signed name
> (it has none). Offline `--area` searches can't geocode (no network) and say so.

## Two elevation backends (both supported)

| Mode | Source | Setup | Accuracy | Limits |
|------|--------|-------|----------|--------|
| `api` | Open-Elevation / OpenTopoData | none | coarser | rate-limited (per-sec throttle + daily counter, both managed) |
| `local` | SRTM/ASTER GeoTIFF tiles | download tiles once | high | none |
| `auto` | local if available, else api | optional tiles | best available | graceful fallback |

Set via `HIKE_ELEVATION_MODE`. See `src/hike_finder/config.py`.

For `local`/`auto`, drop the GeoTIFF DEM tiles (`*.tif`) for your region in
`HIKE_DEM_DIR`. Multiple tiles are mosaicked through a GDAL **VRT** that is
point-sampled, so only the pixels under each query point are read and memory
stays flat no matter how large the region. The tiles must share a CRS and
resolution (true for a single DEM product); for mixed-resolution sets (e.g.
Copernicus GLO-30 spanning a latitude band, which needs resampling) build your
own with `gdalbuildvrt *.tif mosaic.vrt` and drop the `.vrt` in the directory —
it is used as-is.

## Getting started (from a fresh clone)

New here? Five steps from nothing to a working tool. Already have the repo and a
Python environment? Skip to [Quickstart](#quickstart).

**1. Prerequisites** — [Python 3.10+](https://www.python.org/downloads/) and
[git](https://git-scm.com/downloads). Confirm with `python --version`.

**2. Clone the repo**

```bash
git clone https://github.com/BoykoNeov/hike-finder-mcp.git
cd hike-finder-mcp
```

**3. Create and activate a virtual environment** (recommended — keeps the deps isolated)

```bash
python -m venv .venv
source .venv/bin/activate          # Linux / macOS
# .venv\Scripts\Activate.ps1       # Windows PowerShell
```

**4. Install** — base install gives the CLI and web UI (no LLM / MCP stack). See
[Install](#install) below for the `mcp`, `local-dem`, and `dev` extras.

```bash
pip install -e .
```

**5. Verify** — offline, no network or contact needed:

```bash
hike-finder --help                 # prints usage → the entry points resolve
```

For deeper assurance, `pip install -e ".[dev]"` then `pytest` runs the full
offline suite (a few `.sh` launcher cases need `bash`; MCP tests skip without
the `mcp` extra). From here, pick a frontend: the **Web UI** (Option A),
**command line** (Option B), or **MCP server** (Option C) below.

Want the slower, fully-explained version of all of this — with sample output and
how to interpret it? See **[`GUIDE.md`](GUIDE.md)**.

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
Add `--compose-loops` to synthesise loops from connected trails (see
[Composing loops](#composing-loops-stitch-connected-trails-into-a-day-loop)), and
`--gpx FILE` / `--geojson FILE` to also write the results as a track you can load
into a GPS or phone (see [Export](#export--gpx--geojson-load-into-your-phone-or-gps)).
Add `--name-places` to label unnamed `route/<id>` routes from their endpoints' place
names (see [Naming unnamed routes](#naming-unnamed-routes-reverse-geocoding)).

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
the same filters as the CLI — plus `compose_loops` (stitch connected trails into
loops) and `area` (search a snapshot offline).

> The server is **validated live**: with `mcp` 1.28 it was driven over real OS
> stdio (`python -m hike_finder.server`) — `list_tools` advertises `find_hikes`,
> and a `find_hikes` call against Špindlerův Mlýn returned real engine-computed
> hikes (e.g. *Špindlerův mlýn - okruh — 1.11 km, +34 m / -34 m [loop, car,
> lift:chair_lift]*). It is also pinned offline by `tests/test_server.py` (the
> real MCP protocol over an in-memory session). The SDK's decorator API has
> shifted across versions — if the server won't start, check the imports in
> `src/hike_finder/server.py` against your installed `mcp` version.

### Launcher scripts (one file per interface)

Thin wrappers in [`scripts/`](scripts/) start each frontend with a default
Overpass contact already set, then forward your arguments to the entry point
above — so they never go stale. Override the contact by exporting
`HIKE_OVERPASS_UA` first. One file per interface, both shells:

| Interface | Linux / macOS | Windows |
|-----------|---------------|---------|
| CLI | `./scripts/cli.sh --bbox 50.72 15.58 50.74 15.62` | `.\scripts\cli.ps1 --bbox 50.72 15.58 50.74 15.62` |
| Web UI | `./scripts/web.sh` | `.\scripts\web.ps1` |
| MCP server | `./scripts/mcp.sh` | `.\scripts\mcp.ps1` |

The MCP launcher keeps **stdout clean** (stdout is the JSON-RPC channel), so a
client can point straight at it instead of `hike-finder-mcp`:

```bash
claude mcp add hike-finder -- /abs/path/to/scripts/mcp.sh
# Windows: ... -- powershell -NoProfile -ExecutionPolicy Bypass -File C:\path\to\scripts\mcp.ps1
```

All three are pinned by `tests/test_launchers.py` (the MCP one via a real stdio
handshake — the check that proves nothing leaked to stdout).

### Getting a bounding box (CLI / MCP)

The web UI gives you the box for free. For the CLI or MCP you supply four corners
in the order **`south, west, north, east`** (min latitude, min longitude, max
latitude, max longitude):

- **openstreetmap.org → "Export" tab** draws a draggable box and shows its four
  edges — copy them straight in.
- Or read the corners off **mapy.cz** for the area you're planning.

> **Example** — the bbox `50.72,15.58,50.74,15.62` (Špindlerův Mlýn) returns ~11
> routes, each flagged for `car`/`lift`/shape with a locally computed gain/loss;
> the detected loop *Špindlerův mlýn – okruh* reads **+34 m / −34 m** (gain ≈ loss,
> as a closed loop must — the pipeline's built-in sanity check). The **start** pin
> is coupled to access where possible: with a mapped parking/lift near an end,
> `start` is the terminus nearest it, so it usually lands on the trailhead you'd
> drive or ride to. See [`HANDOFF.md`](HANDOFF.md) for how each piece was validated.

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
| `HIKE_NEAR_MISS_GAIN_FRAC` | Near-miss gain tolerance, as a fraction of the bound (0.2 = within 20%) | `0.2` |
| `HIKE_NEAR_MISS_DIST_KM` | Near-miss distance tolerance, km past a min/max | `2.0` |
| `HIKE_NEAR_MISS_RADIUS_FRAC` | Near-miss access tolerance: parking/lift within radius × (1 + this) still counts | `0.5` |
| `HIKE_SNAPSHOT_DIR` | Directory for named area snapshots saved by the web UI | per-user cache (`…/hike-finder/snapshots`) |
| `HIKE_CACHE` | Transparent on-disk cache of Overpass + elevation results, so repeat/overlapping searches don't re-hit the public servers. `0`/`false`/`no`/`off` disables (same as `--no-cache`) | on |
| `HIKE_CACHE_DIR` | Directory for the cache SQLite file | per-user cache (`…/hike-finder`) |
| `HIKE_OVERPASS_CACHE_TTL_DAYS` | How long a cached Overpass area stays fresh, days (trails change slowly). `0` disables Overpass caching; elevation is immutable terrain and never expires | `30` |
| `HIKE_GEOCODE` | Opt-in reverse-geocode naming of **unnamed** routes (`route/<id>`) from place names via Nominatim (same as `--name-places`). Off by default — Nominatim's policy is strict | off |
| `HIKE_NOMINATIM_URL` | Override the Nominatim reverse-geocoding endpoint (self-host for heavy use) | `nominatim.openstreetmap.org` |
| `HIKE_NOMINATIM_MIN_INTERVAL` | Min seconds between Nominatim requests (the public server caps at ~1 req/sec) | `1.1` |
| `HIKE_GEOCODE_CACHE_TTL_DAYS` | How long a cached place name stays fresh, days (place names change slowly). `0` disables geocode caching | `365` |
| `HIKE_COMPOSE_MIN_KM` | Compose mode: default min loop length when no `--min-distance` | `3` |
| `HIKE_COMPOSE_MAX_KM` | Compose mode: default max loop length when no `--max-distance` | `15` |
| `HIKE_COMPOSE_MAX_SEGMENTS` | Compose mode: max trail segments stitched per loop | `12` |
| `HIKE_COMPOSE_OVERLAP_FRAC` | Compose mode: drop a loop sharing more than this fraction of its length with an already-kept loop (near-duplicate collapse) | `0.6` |
| `HIKE_COMPOSE_MAX_LOOPS` | Compose mode: max loops returned, ranked by compactness (roundest first); bounds the per-loop elevation cost | `15` |
| `HIKE_COMPOSE_MIN_COMPACTNESS` | Compose mode: drop a loop below this Polsby–Popper compactness (4πA/P²) — a degenerate thin sliver, not a real loop; `0` disables | `0.05` |

> **Snapshot caveat:** `--area` locks the snapshot's sample interval (the saved
> elevation points were taken at it), so `HIKE_SAMPLE_INTERVAL` can't break an
> offline search. `HIKE_MAX_ROUTE_FACTOR` is the one knob that still applies
> offline; the download already prunes over-length routes, so loosening it offline
> is safe and tightening it only drops a subset.

### Troubleshooting

- **`406 Not Acceptable` / every Overpass request fails** → set `HIKE_OVERPASS_UA`
  to a real contact. The public server rejects the default Python User-Agent.
- **No hikes returned** → widen the bbox or loosen the filters. Note that loops are
  genuinely sparse in KČT data (most relations are linear marked segments), so
  `circular=true` legitimately returns few results — try `--compose-loops` to stitch
  connected trails into loops instead (see
  [Composing loops](#composing-loops-stitch-connected-trails-into-a-day-loop)).
- **`--compose-loops` returns few/no loops** → the target loop must fit *inside* the
  searched bbox; widen the area or the `--min/--max-distance` band.
- **Slow / occasional `504`** → public Overpass overload; the client retries with
  backoff. Point `HIKE_OVERPASS_URL` at a regional instance for heavy use.

## Status

The whole pipeline — geometry/gain/access math, the Overpass parser, both
elevation backends (API with rate-limit throttle, retry/backoff, and a persistent
daily-request counter; local DEM via a point-sampled GDAL VRT), the transparent
cache, loop composition, offline snapshots, near-misses, reverse-geocode naming,
and GPX/GeoJSON export — is **implemented, unit-tested (offline), and validated
live** across all three frontends (CLI + web + MCP), with computed gain
cross-checked against the loop invariant (gain ≈ loss). Released as v0.2.0. See
[`CHANGELOG.md`](CHANGELOG.md) for the per-release breakdown and
[`HANDOFF.md`](HANDOFF.md) for the architecture and open design notes.
