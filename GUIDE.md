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
`Successfully installed hike-finder-mcp-<version>`. Two new commands are now on
your PATH.

**Read it** — verify the install resolved the entry points *without touching the
network*:

```bash
hike-finder --help
```

You should see the usage block (`usage: hike-finder [-h] --bbox SOUTH WEST NORTH
EAST ...`). If that prints, the install worked. If you want deeper assurance,
`pip install -e ".[dev]"` then `pytest` runs the full **offline** suite (a few
`.sh` launcher cases need `bash`) — all green means the engine is sound on your machine.

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
  and access at a glance — see [Reading your results](#reading-your-results--how-to-treat-what-comes-back).
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
- See [Reading your results](#reading-your-results--how-to-treat-what-comes-back) for the flags and `gain n/a`.

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

### Download once, search many times — offline, no API calls

**Do** — fetch the area one time, then re-filter the saved copy as often as you like:

```bash
# Linux / macOS
hike-finder --bbox 50.72 15.58 50.74 15.62 --download krkonose.json   # one fetch + elevation
hike-finder --area krkonose.json --min-gain 600 --circular            # offline
hike-finder --area krkonose.json --max-distance 8 --car-access        # offline, re-filter freely
```

```powershell
# Windows PowerShell — identical (no env vars; quote the path if it has spaces)
hike-finder --bbox 50.72 15.58 50.74 15.62 --download krkonose.json
hike-finder --area krkonose.json --min-gain 600 --circular
hike-finder --area krkonose.json --max-distance 8 --car-access
```

**Why** — the `--download` step is the **only** one that touches the network: it
makes the single Overpass call and then computes elevation for **every** plausible
route in the box, spending the elevation budget once, up front, and writes it all to a
`.json` snapshot. Every `--area` search afterwards reads that file and touches nothing
— no Overpass, no elevation API, no daily-quota spend. Ideal when you want to try a
dozen filter combinations on one area, or you're on a plane / a metered connection.

**Expect** — the download prints one confirmation line on stdout, and (because it *did*
hit the elevation API) the usual quota tail on stderr:

```text
Saved snapshot to krkonose.json: 11 routes, ~1,500 elevation samples. Search it offline with --area krkonose.json.
elevation API: 120/1000 requests used today (880 remaining, resets at UTC midnight)
```

(The exact sample count scales with the routes' total length, so yours will differ.)
Each later `--area` search prints the normal one-line results — and, tellingly, **no
quota line**:

```text
[Z] Richtrovy Boudy - Špindlerův mlýn — 7.81 km, +678 m / -251 m [one-way, car] (start 50.7257,15.6071, OSM relation 237053)
[Z] Špindlerův mlýn - okruh — 1.11 km, +34 m / -34 m [loop, car, lift:chair_lift] (start 50.7253,15.6057, OSM relation 6282999)
```

**Read it** — the **missing quota line is your proof it stayed offline**: `--area`
never reaches the elevation API, so the daily counter doesn't move and the line is
suppressed (see [Step 3B](#step-3b--command-line) — it only prints when the API was
hit). The numbers are identical to a live search — this was validated by downloading an
area and confirming every route's gain/loss matched a fresh live run exactly. The one
thing frozen into a snapshot is the **sample interval**; you can still re-tune the gain
threshold, smoothing, access radii and loop tolerance on each offline search.

> A snapshot for one bbox is independent of any other. Download as many areas as you
> like to separate files (or names, in the Web UI) and search whichever you need.

### The same thing in the Web UI

**Do** — in the page (see [Step 3A](#step-3a--web-ui-easiest-no-coordinates-to-type)
for the basics):

1. Pan/zoom the map to frame the area and fill in **Contact**.
2. Type a name in the **"name this view"** box (e.g. `krkonose`) and click **"Download
   view"**.
3. Pick the saved view from the **"Search area"** dropdown (instead of the default
   *"— live map (fetches OSM) —"*), set your filters, and click **Search**.

**Expect** — while downloading, the status line reads
`Downloading "krkonose" (one-time fetch + elevation)…`, then settles to
`Saved "krkonose": 11 routes, ~1,500 elevation samples. Now searchable offline.` The
new view appears in the **"Search area"** dropdown immediately. A search against a saved
view shows the same cards and pins as a live one — but **with no `elevation API:` tail**
on the status line, the same offline tell as the CLI.

**Read it** — Web-UI snapshots are saved **by name** into a per-user cache folder, so a
view you downloaded last week is still offered in the dropdown today:

- Windows: `%LOCALAPPDATA%\hike-finder\snapshots`
- Linux/macOS: `~/.cache/hike-finder/snapshots`
- Override either with the `HIKE_SNAPSHOT_DIR` environment variable.

(The **CLI** is different on purpose: it writes to the exact path you pass to
`--download`, wherever you want the file — the dropdown convenience is a Web-UI thing.)

### The same thing from MCP

An LLM driving the server gets two offline hooks: a **`download_area`** tool (give it
the bbox and a file path to fetch-and-save once) and an **`area`** argument on
`find_hikes` (point it at a saved snapshot to search it offline). So you can ask *"download
the Špindlerův Mlýn area for offline use,"* then later *"search that saved area for loops
over 600 m"* and the client routes the second request through the snapshot — no API calls.

### Even without downloading: the automatic cache

A snapshot is the deliberate, portable way to go offline. But you don't have to think
about it for everyday repeat searches — **hike-finder caches network results on disk
automatically**, on by default. The first search of an area fetches Overpass and the
elevation API as usual; a second search of the *same or an overlapping* area answers
from the cache, hitting the network only for anything genuinely new.

**Expect** — re-run the very same search and it returns near-instantly (no `elevation
API:` movement, no Overpass wait). On a live test a cold search took 4.2 s and the warm
re-run **0.4 s**, with byte-identical results. Because a trail relation carries its full
shape no matter how you draw the box, the elevation half of the cache even pays off when
you pan to a *different* nearby area — the shared trails are already known.

**Read it** — two knobs and two staleness rules:

- Elevation is cached **forever** (terrain doesn't change). Overpass areas expire after
  `HIKE_OVERPASS_CACHE_TTL_DAYS` (default **30 days**, since marked trails change slowly).
- The cache lives next to the daily-counter file (`%LOCALAPPDATA%\hike-finder` or
  `~/.cache/hike-finder`); point it elsewhere with `HIKE_CACHE_DIR`.
- Bypass it for one run with **`--no-cache`** (or `HIKE_CACHE=0`); wipe it with
  **`hike-finder --clear-cache`**. A broken or unwritable cache never breaks a search —
  it just falls back to fetching live.

This is both a convenience (repeat exploration is cheap) and good manners: it's exactly
the "cache results, don't re-fetch" the OpenStreetMap usage policy asks of clients.

### Show close-but-not-matching routes

Add `--near-misses` to always list them, `--no-near-misses` to never. The default
(omit the flag) shows them only when nothing strictly matches. Tolerances are tunable —
`HIKE_NEAR_MISS_GAIN_FRAC` (default 0.2 = within 20% of a gain bound),
`HIKE_NEAR_MISS_DIST_KM` (2 km), `HIKE_NEAR_MISS_RADIUS_FRAC` (0.5 = parking/lift up to
1.5× the radius still counts). In the Web UI it's the **"Near misses"** dropdown
(auto / always / never); over MCP, a `near_misses` boolean on `find_hikes`.

---

## Composing loops from connected trails

You ask for loops (`--circular`) and get almost nothing. That's not a bug: in the KČT
data most relations are **linear** marked segments (a coloured trail from A to B). The
circular day-hikes people actually walk are usually *combinations* of several connected
segments — and the tool, by default, reports each relation as-is rather than inventing
combinations. So `--circular` honestly returns only the handful of loops someone mapped
as a single relation.

**`--compose-loops` builds the combinations for you.** It takes every marked trail in the
area, joins them at their shared junctions into one network graph, and searches that
graph for cycles of a length you ask for:

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 --compose-loops \
            --min-distance 5 --max-distance 12 --user-agent you@example.com
```

**What you get** — loops stitched from several trails. Because a composed loop isn't a
single mapped trail, it has no OSM relation number; instead it names the trails it's made
of:

```text
Composed loop — 9.86 km, +540 m / -538 m [loop, car] (start 50.73,15.61, composed of 0402 + 1801 + Medvědí okruh)
```

**How to read it** — the distance, the gain/loss, and the car/lift flags are computed the
same way as for any route, so they're as trustworthy here as anywhere. A composed loop is
circular by construction, so its gain and loss come back roughly equal — a good sanity
check (you end where you started). The "composed of …" list is its provenance.

**The two knobs that matter:**

- **Length** — `--min-distance` / `--max-distance` set the target band (default 3–15 km).
  Want a ~10 km loop? `--min-distance 8 --max-distance 12`.
- **Area size** — a composed loop is kept **inside the box you searched**. A 12 km loop
  simply can't fit inside a 2 km-wide view, so if you get nothing, **widen the map / bbox**
  (this is the most common reason for an empty result). Pan out and try again.

A dense area can yield dozens of candidate loops. Rather than flood you (and rather than
spend an elevation lookup on every one), the tool returns the **15 most "loop-like"** —
ranked by how round/compact they are, so thin out-and-back shapes sink to the bottom and
drop off — and tells you how many distinct loops it found in total. Want more? Raise
`HIKE_COMPOSE_MAX_LOOPS`.

**"A loop from where I park."** Add `--car-access` (or `--chairlift-access`) to a compose
search and you get only loops that come within reach of a mapped parking lot / lift station,
each **started at that trailhead** — the on-loop point nearest the parking or lift, instead
of an arbitrary spot on the ring:

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 --compose-loops --car-access \
            --min-distance 8 --max-distance 12
```

This isn't just the access *filter* applied afterwards: the reachability test runs **before**
the 15-loop cap, so the cap is spent on loops you can actually drive/ride to — without it a
busy area's most-compact loops can all be far from any trailhead and you'd get nothing back.
Require both `--car-access` and `--chairlift-access` and a loop must come near *both*; the
start then sits **where you park** (parking wins over the lift). The loop's geometry — and
so its gain/loss — is unchanged; only the start marker moves.

**In the Web UI** it's the **"Compose loops from connected trails"** checkbox (live map
only — it needs the fetched trail network); over MCP, a `compose_loops: true` argument on
`find_hikes`. The same `--min/--max-distance` (or `min/max_distance_km`) set the band.

**Honesty note** — a composed loop is a *suggestion*. It's geometrically real (the trails
genuinely connect at shared OpenStreetMap nodes), but nobody necessarily signs or walks it
as one named route. Loop closure is high-confidence; the *composition* is the tool's idea,
not the trail network's.

**One practical caveat — use local elevation tiles for this.** Composed loops are long, so
each needs hundreds of elevation samples. On the **public elevation API** (throttled to ~1
request/second, batched 100 points per request) a compose run is **slow** — dozens of
requests, roughly a minute cold — but it does *not* exhaust the daily cap at the defaults (a
15-loop run is on the order of 50 requests, not 1000). The cap only becomes a risk if you
raise `HIKE_COMPOSE_MAX_LOOPS` well past its default or do many runs, in which case the later
loops come back with **`gain n/a`** (nothing breaks — they just lack a gain number). Either
way, for fast, unlimited elevation on *every* composed loop, set up a
[local DEM](#choosing-an-elevation-backend) (`HIKE_ELEVATION_MODE=local`).

---

## Point-based routes — pick a spot instead of drawing a box

Two modes take **points, not a bounding box** — you don't frame an area, you drop a pin (or
two). Leave `--bbox` off; the tool works out its own area from the point(s).

**"Give me a ~10 km loop starting near here."** Point at a spot and get circular day-hikes
through it:

```bash
hike-finder --around 50.73 15.60 --min-distance 8 --max-distance 12 \
            --user-agent you@example.com
```

Every loop returned passes within `--around-radius` metres of your point (default 1000) and
**starts there**. The `--min-distance`/`--max-distance` numbers set how *long* the loop is —
not how far it may wander, so an 11 km loop can still swing a couple of km out and back. Add
`--car-access` to only get loops with parking near them.

**"How do I walk from A to B — and what are my options?"** Give a start and a finish; the
tool draws the shortest way first, then the next-shortest, and so on:

```bash
hike-finder --from 50.72 15.58 --to 50.76 15.63 --routes 3 \
            --user-agent you@example.com
```

Each point is placed onto the nearest marked trail (so the route reaches exactly where you
pointed). `--routes` sets how many alternatives (default 3); they're **genuinely different**
routes, not the same line with a tiny detour, and `--max-distance` caps how long a route may
be. If you point somewhere with no trail within ~2 km, you'll get nothing back rather than a
route to a trail far away — move the pin closer to a marked path.

In the **Web UI** these are the **Mode** dropdown ("Circular routes near a point" / "Routes
between two points") — pick one, then click the map to drop your pin(s) and press Search. Over
**MCP** they're the `circular_routes` and `routes_between` tools. Both are live-map only.

---

## Taking a route with you — GPX / GeoJSON export

A summary on screen is the start; the last mile is a file your **phone or GPS** can follow
in the field. Any search — live, offline `--area`, or `--compose-loops` — can also write
its results to a track file. The flag is an *extra* output: you still get the normal text
(or `--json`), and the file is written beside it.

```bash
hike-finder --bbox 50.72 15.58 50.74 15.62 --circular --gpx loops.gpx
```

```text
Medvědí okruh — 11.7 km, +505 m / -505 m [loop, car] (start 50.7312,15.6044, OSM relation 6282999)
… (the usual lines on stdout) …
Wrote 4 route(s) to loops.gpx (GPX).        ← this confirmation goes to stderr
```

- **`--gpx FILE`** writes **GPX 1.1**: one track per route, preceded by a waypoint at each
  **start** (the trailhead you drive/ride to). Drop it into **Komoot, OsmAnd, Gaia GPS, a
  Garmin, or mapy.cz** and the route is there to navigate — with the elevation profile.
- **`--geojson FILE`** writes **GeoJSON** (RFC 7946): a `FeatureCollection` of route lines
  with the full computed stats in each feature's `properties` — handy for QGIS, a web map,
  or any data pipeline.

It works on every search mode, including **offline** ones (no network needed to export a
snapshot search) and **composed loops** (the stitched loop exports as one closed ring):

```bash
hike-finder --area krkonose.json --min-gain 600 --geojson picks.geojson    # offline
hike-finder --bbox 50.72 15.58 50.74 15.62 --compose-loops --gpx day.gpx    # composed loops
```

**Read it** — the confirmation prints to *stderr* (so `--json --gpx out.gpx` keeps stdout a
clean JSON pipe). **Near-misses are exported too**, flagged exactly as on screen. When a
route's elevation was computed, the exported track carries the **full per-point profile**
(GPX `<ele>` on every point of one clean walking-order track; GeoJSON 3D `[lon, lat, ele]`
coordinates) — so your GPS draws the climb, not just the line. A fragmented relation whose
legs can't be stitched into one continuous line falls back instead to the **raw mapped
geometry** — every member way, no elevation — so it keeps all legs and matches the reported
distance rather than dropping a leg; its gain/loss still ride in the track's description.

**In the Web UI**, the same two formats are **Download GPX / Download GeoJSON** buttons —
they hand you the routes currently listed — and the map now **draws each route line** (amber
for a near-miss, dashed purple for a composed loop) so you can see the shape before you save
it. Over **MCP**, `find_hikes` takes a `format: "gpx" | "geojson"` argument that returns the
serialised file as text.

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
