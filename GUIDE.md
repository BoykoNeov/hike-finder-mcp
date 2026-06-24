# Using hike-finder — a step-by-step walkthrough

This is the **verbose, hold-your-hand guide**: every step says *what to do*, *why
you're doing it*, *what you should see*, and *how to read what comes back*. If you
just want the terse reference (full flag list, every environment variable, the
filter table), that lives in the [README](README.md) — this guide links to it
rather than repeating it, so there's a single source of truth for the numbers.

Throughout, each step follows the same four beats:

- **Do** — the exact command or click.
- **Why** — what it's for, so you can adapt it.
- **Expect** — the real output you should see (the samples below are captured
  from an actual run against the Krkonoše/Špindlerův Mlýn area, not invented).
- **Read it** — how to interpret the result and what to do next.

Commands are shown for both shells: `bash` (Linux/macOS) and PowerShell (Windows).

---

## Before you start — what this tool does and what you'll get

hike-finder searches **OpenStreetMap for marked hiking-route relations**
(`route=hiking`/`foot` — the signed, maintained trails, e.g. the Czech **KČT**
network) inside a bounding box you choose, then for each route it computes the
**distance** and **elevation gain/loss** locally and tags the route's **shape**
(loop vs. point-to-point) and **access** (parking or chairlift near an end). You
end up with a ranked list of real, named trails with trustworthy numbers.

There are three ways to run it, all on the same engine:

| Frontend | Command | Best when |
|----------|---------|-----------|
| **Web UI** | `hike-finder-web` | You don't want to type coordinates — pan a map instead. Start here. |
| **Command line** | `hike-finder` | You know the area's coordinates and want scriptable/JSON output. |
| **MCP server** | `hike-finder-mcp` | You want an LLM client (Claude, etc.) to call it in plain language. |

The Web UI and CLI need **no LLM and no MCP client** — they're plain programs.

---

## Step 1 — Install

**Do**

```bash
# from the repo root, ideally inside a virtual environment
pip install -e .
```

(New to the repo entirely? The [README's "Getting started (from a fresh
clone)"](README.md#getting-started-from-a-fresh-clone) covers Python, git, clone,
and venv first. Come back here once `pip install -e .` succeeds.)

**Why** — the base install gives you the `hike-finder` CLI and the
`hike-finder-web` UI with only one dependency (`requests`). The MCP server, the
local-DEM elevation backend, and the test suite are **optional extras** you add
only if you need them — see [Install](README.md#install) for `[mcp]`,
`[local-dem]`, and `[dev]`.

**Expect** — pip finishes with a line like
`Successfully installed hike-finder-mcp-0.1.0`. Two new commands are now on your
PATH.

**Read it** — verify the install resolved the entry points *without touching the
network*:

```bash
hike-finder --help
```

You should see the usage block (`usage: hike-finder [-h] --bbox SOUTH WEST NORTH
EAST ...`). If that prints, the install worked. If you want deeper assurance,
`pip install -e ".[dev]"` then `pytest` runs the full **offline** suite (114
tests) — all green means the engine is sound on your machine.

---

## Step 2 — Set a contact for OpenStreetMap (the one setup step that matters)

**Do** — pick whichever fits how you'll run it:

```bash
# Linux / macOS — set once for the shell session
export HIKE_OVERPASS_UA="you@example.com"
```

```powershell
# Windows PowerShell — set once for the shell session
$env:HIKE_OVERPASS_UA = "you@example.com"
```

Or pass it per-command with `--user-agent you@example.com` (CLI) / the **Contact**
field (Web UI).

**Why** — the data comes from OSM's **public Overpass** server, and that server
**rejects the default Python User-Agent with HTTP 406**. It asks every client to
identify itself with a real contact (an email or URL) so abuse can be traced —
this is standard [OSM etiquette](https://operations.osmfoundation.org/policies/nominatim/).
Use a real address you control.

**Expect** — nothing visible yet; this just primes the environment. You'll know
it's right when searches return data instead of a `406` error.

**Read it** — if you ever see `406 Not Acceptable` or "every Overpass request
fails", this is the cause: the contact wasn't set or wasn't picked up. Set it and
retry.

---

## Shortcut — launcher scripts (one per interface)

Don't want to set the contact (Step 2) every time? There's one small launcher
per interface in [`scripts/`](scripts/), in both shells.

**Do** — run the launcher for the frontend you want:

```bash
# Linux / macOS
./scripts/cli.sh --bbox 50.72 15.58 50.74 15.62   # CLI (forwards all args)
./scripts/web.sh                                   # Web UI
./scripts/mcp.sh                                   # MCP server (stdio)
```

```powershell
# Windows PowerShell
.\scripts\cli.ps1 --bbox 50.72 15.58 50.74 15.62
.\scripts\web.ps1
.\scripts\mcp.ps1
```

**Why** — each launcher does two things and nothing more: it sets a **default
Overpass contact** (only if you haven't set `HIKE_OVERPASS_UA` yourself — so Step
2 becomes optional), then hands every argument straight to the real entry point
(`hike-finder` / `hike-finder-web` / `hike-finder-mcp`). They're deliberately
*thin* wrappers, not re-implementations, so when the tool changes they stay
correct for free.

**Expect** — identical output to running the entry point directly: `cli.*` prints
the same result lines, `web.*` prints `hike-finder web UI on http://127.0.0.1:8765`
and serves the map, `mcp.*` produces **nothing on stdout** and waits to speak the
MCP protocol to a client.

**Read it**

- **Override the contact** any time by setting it first — the launcher won't
  clobber your value:
  ```powershell
  $env:HIKE_OVERPASS_UA = "you@example.com"; .\scripts\cli.ps1 --bbox ...
  ```
- **Point an MCP client at the launcher** instead of the bare command, so the
  contact is always set: `claude mcp add hike-finder -- /abs/path/to/scripts/mcp.sh`
  (on Windows, `-- powershell -NoProfile -ExecutionPolicy Bypass -File C:\path\to\scripts\mcp.ps1`).
  The MCP launcher is silent on stdout on purpose — stdout is the JSON-RPC
  channel, and any banner there would corrupt the handshake.
- All three are regression-pinned by `tests/test_launchers.py`.

---

## Step 3A — Web UI (easiest; no coordinates to type)

**Do**

```bash
hike-finder-web
```

Then open **http://127.0.0.1:8765** in a browser. In the page:

1. Fill the **Contact** field (top of the right panel) with your email.
2. **Pan and zoom the map** so it frames the area you want to search.
3. Set any filters you care about — **Shape**, **Car access**, **Chairlift
   access** (each: *any / required / excluded*), and **gain/distance** min/max.
4. Click **"Search this map area"**.

**Why** — the visible map *is* your bounding box: "search this map area" sends the
current map edges as the search box, so you never type coordinates. The filters
narrow results before any elevation work happens, so tighter filters = faster,
more relevant results.

**Expect** — the console where you launched it prints:

```text
hike-finder web UI on http://127.0.0.1:8765  (Ctrl+C to stop)
```

In the browser, the status line shows `Searching…`, then settles to something
like `11 hike(s) found.  ·  elevation API: 110/1000 requests today`. Each match
appears as a **card** in the right panel (name · distance · gain/loss · flag
chips · OSM relation id) **and** as a **pin** on the map at the route's start.

**Read it**

- **Click a card** → the map flies to that route's start and opens its popup.
  Click the matching pin to see name + distance + gain.
- The **flag chips** (`loop`/`one-way`, `car`, `lift:chair_lift`) tell you shape
  and access at a glance — see [Reading your results](#reading-your-results).
- **No results?** The map area is genuinely empty for your filters — zoom out, or
  relax a filter (loops especially are sparse; see the troubleshooting section).
- The **`elevation API: x/1000`** tail is your daily-quota gauge (only shown when
  the API backend was actually used). Plenty of headroom = keep searching.

Stop the server with **Ctrl+C** when done.

---

## Step 3B — Command line

**Do**

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 --user-agent you@example.com
```

Add filters as needed, e.g. loops reachable by chairlift:

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 \
            --circular --chairlift-access \
            --user-agent you@example.com
```

**Why** — `--bbox` is the area, in the order **`south west north east`** (min-lat
min-lon max-lat max-lon). You need the coordinates here (the Web UI hands them to
you for free); get them from **openstreetmap.org → "Export" tab** (drag a box, copy
the four edges) or read them off mapy.cz. The boolean filters are **tri-state**:
omit = don't care, `--circular` = require loops, `--no-circular` = exclude loops
(same pattern for `--car-access` and `--chairlift-access`). Numeric filters:
`--min-gain`/`--max-gain` (m), `--min-distance`/`--max-distance` (km). Run
`hike-finder --help` for the complete list.

**Expect** — one line per matching route (this is a **real** capture of the bbox
above, no filters):

```text
elevation API: 110/1000 requests used today (890 remaining, resets at UTC midnight)
0402 — 9.86 km, +704 m / -320 m [one-way, lift:chair_lift] (start 50.7331,15.5724, OSM relation 6133813)
[Z] Richtrovy Boudy - Špindlerův mlýn — 7.81 km, +678 m / -251 m [one-way, car] (start 50.7257,15.6071, OSM relation 237053)
Medvědí okruh — 7.98 km, +290 m / -0 m [one-way, car, lift:chair_lift] (start 50.7413,15.5821, OSM relation 6285306)
Dřevařská cesta — 7.9 km, +106 m / -474 m [one-way, car] (start 50.7272,15.6148, OSM relation 3280873)
[Z] Špindlerův mlýn - okruh — 1.11 km, +34 m / -34 m [loop, car, lift:chair_lift] (start 50.7253,15.6057, OSM relation 6282999)
```

(The full run returned 11 routes; trimmed here for length.)

**Read it** — decode one line field by field:

```text
[Z] Špindlerův mlýn - okruh — 1.11 km, +34 m / -34 m [loop, car, lift:chair_lift] (start 50.7253,15.6057, OSM relation 6282999)
└── name ──────────────────┘   └ dist ┘  └ gain ┘ └ loss┘ └──── flags ──────────┘  └─── start lat,lon ──┘  └── OSM id ──┘
```

- The **quota line** prints to *stderr* (so it never pollutes `--json` on stdout)
  and only appears when the **API** backend was actually queried this run.
- `+34 m / -34 m` on a route flagged `loop` is the **loop sanity check working**:
  on a true closed loop gain must ≈ loss, and it does — that cross-checks the
  whole sampling/gain pipeline. (On a `one-way` route the two differ freely.)
- See [Reading your results](#reading-your-results) for the flags and `gain n/a`.

**Machine-readable output** — add `--json` (here with `--circular`, a real capture):

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 --circular --json --user-agent you@example.com
```

```json
[
  {
    "osm_id": 6282999,
    "name": "[Z] Špindlerův mlýn - okruh",
    "ref": null,
    "distance_km": 1.11,
    "gain_m": 34,
    "loss_m": 34,
    "circular": true,
    "car_access": true,
    "chairlift_access": true,
    "lift_type": "chair_lift",
    "start": { "lat": 50.7253429, "lon": 15.6056996 }
  }
]
```

**Read it** — `--json` is for piping into other tools. Note `gain_m`/`loss_m` can
be `null` (renders as `gain n/a` in text) when elevation couldn't be resolved, and
`start` carries full precision here vs. 4 decimals in the text line. Exit code is
`0` on success (including "no matches" — you'll see `No matching hikes found in
that area.` in text mode), `1` on a fetch error (with a hint printed to stderr).

---

## Step 3C — MCP server (drive it from an LLM client)

**Do** — install the extra and register the command (example for Claude Code):

```bash
pip install -e ".[mcp]"
claude mcp add hike-finder --env HIKE_OVERPASS_UA=you@example.com -- hike-finder-mcp
```

For a `.mcp.json` / Claude Desktop config block instead, see
[Option C in the README](README.md#option-c--mcp-server-drive-it-from-an-llm-client).

**Why** — the MCP server exposes the same engine as a tool, `find_hikes(south,
west, north, east, …)`, that an LLM client can call. You then ask in plain
language and the client fills in the bounding box and filters for you.

**Expect** — after registering, the client lists a `find_hikes` tool. Ask
something like *"find loop hikes near Špindlerův Mlýn reachable by chairlift"* and
the client calls the tool and renders the **same one-line summaries** as the CLI
(e.g. *Špindlerův mlýn - okruh — 1.11 km, +34 m / -34 m [loop, car,
lift:chair_lift]*).

**Read it** — results mean exactly what the CLI's do (next section). If the server
won't start, the usual cause is an `mcp` SDK version mismatch — check the imports
in `src/hike_finder/server.py` against your installed `mcp` version.

---

## Reading your results — how to treat what comes back

This is the part worth slowing down on. The numbers are trustworthy *and* honest
about their limits.

**The one-line format** (identical in CLI and MCP):

```text
<name> — <km> km, +<gain> m / -<loss> m [<flags>] (start <lat>,<lon>, OSM relation <id>)
```

**The flags in `[...]`:**

| Flag | Meaning |
|------|---------|
| `loop` / `one-way` | route shape — closed loop, or point-to-point |
| `car` | `amenity=parking` is mapped near one of the route's ends |
| `lift:<type>` | a ride-up aerialway (e.g. `lift:chair_lift`, gondola, cable car) is mapped near an end |

**Gain / loss:** computed in *this* codebase by resampling the track to even
spacing, smoothing the elevation series, and counting climbs with a hysteresis
threshold so DEM noise isn't mistaken for ascent. Two consequences:

- **`+34 m / -34 m` on a `loop` is the correctness check, not a coincidence** — a
  closed loop returns to its start, so total ascent must ≈ total descent. Seeing
  that hold is your signal the pipeline is sound.
- **`gain n/a`** (text) / `"gain_m": null` (JSON) means elevation couldn't be
  resolved for that route — typically the API daily cap was hit or a tile was
  missing. The route is still real and its distance/shape/access are still valid;
  only the elevation is unknown. Re-run later (the API counter resets at UTC
  midnight) or switch to the local-DEM backend (next section).

**Honesty note on access — read this before trusting `car`/`lift`:** these flags
reflect what's **mapped in OSM**, not the physical world. `car` present means
someone mapped parking near an end; `car` **absent does not mean you can't drive
there** — it means nothing of that kind is mapped near the route's ends. Treat the
access flags as "OSM says yes" / "OSM is silent", never as "impossible". Loop
detection, by contrast, is reliable. (This distinction is also stated in the
[README's filter table](README.md#filters).)

**What to do with a result you like:** the **OSM relation id** is your handle.
Open `https://www.openstreetmap.org/relation/<id>` to see the full route, or look
the area up on mapy.cz to plan it. The **start** coordinate is where the pin lands
and is coupled to access where possible — when a route has mapped parking/lift near
an end, `start` is the terminus nearest it, so the pin usually marks the trailhead
you'd actually drive or ride to.

**`~` and `[near miss: …]`** mark a route that does **not** meet your filters but
is close — and the note says exactly how (`gain 709 m — 41 m below the 750 m
minimum`, or `nearest parking 380 m away — just past the 300 m limit`). By
default these appear only when nothing matches, so an empty result still gives you
the next-best options instead of a blank. Treat them as "almost, but check the
note" — they keep the shape and exclusions you asked for, only the numbers/access
are relaxed. See the next section to turn them on always or off.

---

## Searching an area offline (and seeing "close" results)

Two options that save API calls and rescue empty searches.

**Download once, search many times — offline, no API calls.** If you're going to
try several filters on one area, fetch it once and search the saved copy:

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 --download krkonose.json   # one fetch + elevation
hike-finder --area krkonose.json --min-gain 600 --circular            # offline
hike-finder --area krkonose.json --max-distance 8 --car-access         # offline, re-filter freely
```

The `--download` step is the only one that touches the network: it fetches the
routes and computes elevation for **every** plausible route (so it spends the
elevation budget once, up front), then writes a `.json` snapshot. Every `--area`
search after that is **completely offline** and gives the *same* numbers a live
search would — validated: the offline gains match a live search exactly. Handy on a
plane, on a metered connection, or just to stop re-hitting the elevation API while
you explore. In the **web UI** this is the **"Download view"** button plus the
"Search area" dropdown; an LLM driving the **MCP** server has a `download_area` tool
and an `area` argument on `find_hikes`.

> Only the *sample interval* is frozen into a snapshot. You can still re-tune the
> gain threshold, smoothing, access radii and loop tolerance on an offline search.

**Show close-but-not-matching routes.** Add `--near-misses` to always list them,
`--no-near-misses` to never. The default (omit the flag) shows them only when
nothing strictly matches. Tolerances are tunable — `HIKE_NEAR_MISS_GAIN_FRAC`
(default 0.2 = within 20% of a gain bound), `HIKE_NEAR_MISS_DIST_KM` (2 km),
`HIKE_NEAR_MISS_RADIUS_FRAC` (0.5 = parking/lift up to 1.5× the radius still counts).

---

## Choosing an elevation backend

You normally don't touch this — the default `auto` does the right thing. But it
controls where the gain/loss numbers come from, so it's worth understanding.

| Mode | Where elevation comes from | When to choose it |
|------|----------------------------|-------------------|
| `api` (default reach) | public Open-Elevation / OpenTopoData | zero setup; fine for occasional use; coarser and rate-limited |
| `local` | GeoTIFF DEM tiles on your disk (SRTM/ASTER/Copernicus) | highest accuracy, no rate limits — worth it for heavy use or precise gain |
| `auto` (default) | `local` if tiles are configured, else `api` | leave it here; it uses the best available and falls back gracefully |

**Do** — to use local tiles, download a DEM for your area once, point the tool at
the folder, and select the mode:

```bash
export HIKE_ELEVATION_MODE=local      # or leave as auto
export HIKE_DEM_DIR=/path/to/dem/tiles
pip install -e ".[local-dem]"          # needs rasterio
```

```powershell
$env:HIKE_ELEVATION_MODE = "local"
$env:HIKE_DEM_DIR = "C:\path\to\dem\tiles"
```

**Why** — the API is convenient but coarse and rate-limited; local tiles are
exact and unlimited. With `auto`, a working local DEM answers from disk and the
**API quota line disappears** (because the API was never touched) — that's the
signal your tiles are being used.

**Read it** — this backend was validated live against Copernicus GLO-30 tiles: it
read Sněžka at **1601 m** vs. the known **1603 m**, and the loop invariant
(gain ≈ loss) still held. Accuracy you can trust.

---

## Tuning the numbers that change results

The elevation and filter behaviour is governed by environment variables — the
**full table with every default is in the
[README](README.md#configuration-environment-variables)** (not repeated here so it
can't drift). The handful most likely to change *which routes you get* or *what
gain they report*:

- `HIKE_GAIN_THRESHOLD` (default `10` m) — the hysteresis climb threshold. Raise
  it if you suspect DEM noise is inflating gain; it must stay above the terrain
  data's peak-to-peak noise.
- `HIKE_SAMPLE_INTERVAL` (default `25` m) — track resample spacing. Finer = more
  detail and more elevation lookups (more API requests).
- `HIKE_LOOP_TOLERANCE` (default `150` m) — how close start and end must be to
  count as a loop. Bump it if a route you know is circular shows as `one-way`.
- `HIKE_CAR_RADIUS` / `HIKE_LIFT_RADIUS` (default `300` / `400` m) — how near an
  endpoint parking/a lift must be to flag access. Widen if you're getting false
  negatives in sprawly trailhead areas.

**Why** — these are exactly the knobs that make different trail sites report
different gain for the same trail. Making them explicit is the point of the tool:
your numbers stay consistent and tunable instead of inherited from a third party.

---

## When something looks wrong — situations and how to react

| You see… | What it means | What to do |
|----------|---------------|------------|
| `406 Not Acceptable` / every request fails | The public Overpass server rejected the default User-Agent | Set a real contact: `HIKE_OVERPASS_UA`, `--user-agent`, or the web Contact field (Step 2) |
| `No matching hikes found in that area.` | The bbox + filters genuinely matched nothing | Widen the bbox or loosen filters. Loops are sparse in KČT data (most relations are linear segments), so `--circular` legitimately returns few |
| Many routes show `gain n/a` | Elevation backend couldn't answer — usually the API daily cap | Wait for the UTC-midnight reset, or set up the local-DEM backend |
| Slow, or occasional `504` | Public Overpass is overloaded; the client retries with backoff | Wait it out, or point `HIKE_OVERPASS_URL` at a regional/self-hosted instance for heavy use |
| A route you know is a loop shows `one-way` | Its ends are farther apart than the loop tolerance | Raise `HIKE_LOOP_TOLERANCE` |
| `car`/`lift` absent on a trail you can drive to | OSM has no parking/lift mapped near its ends — not a claim it's unreachable | Trust your local knowledge; the flag only reports what OSM maps |

For anything deeper (what's implemented vs. validated live, and what's next), see
[`HANDOFF.md`](HANDOFF.md) and the [Status section](README.md#status) of the README.
