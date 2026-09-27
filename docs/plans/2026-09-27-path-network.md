# Path network as the base — plan, 2026-09-27

The user asked for the app to "work from individual paths mainly" after meeting the
"No hiking route relations are mapped in that area" message. Decisions taken with them
up front:

- **Scope:** every walkable path is the base network, everywhere. Named route relations
  stay, laid on top: they still name things and say how much of a hike is waymarked.
- **What counts as walkable:** `highway=path|footway|track|bridleway|steps`, minus
  sidewalks and crossings (`footway=sidewalk|crossing`) and `access=private|no`.
- **Plain area search** returns the named routes as today **plus** loops built from the
  path network. A bare path fragment is never listed as a result.

This supersedes Tier 3.4 of `2026-09-04-improvement-plan.md` (opt-in unmarked-path
fallback): the user chose the wider version on purpose.

## What was measured first (probe kept at `W:\temp\claude\path-probe\`)

| box | named routes | path ways | download | loops at today's settings |
|---|---|---|---|---|
| Krkonoše 10×10 km | 52 | 1,050 | 5.0 MB | 15 shown (4 of 10–15 km) |
| Kamikōchi 10×10 km | **0** | 544 | 0.7 MB | **7** (was 0) |
| Krkonoše 400 km² | 137 | 4,777 | 10.1 MB, 5 s | search budget exhausted |

Two problems surfaced, both in the loop finder, and both fixed cheaply:

1. **Pass-through junctions left behind by dead-end pruning.** A path network is full of
   spur paths (to a viewpoint, a hut, a car park). The finder prunes them but did not
   re-join the chain they split, so each old spur junction still ended a segment and
   used up one of the 12 segments a loop may have. Re-contracting the pruned core took
   the 10 km box from 1,126 to 511 segments (median 159 m → 271 m) and the 10–15 km
   loops found from 4 to 26. No coordinate moves and nothing is welded, so the old
   "over-merging invents cycles" lesson is not in play.
2. **One start node can eat the whole search budget.** The DFS enumerates from each
   start node in turn under one global budget; on a dense graph the first few starts
   consume it and later ones never run (raising the segment cap to 30 returned *zero*
   loops). A per-start share of the budget (floor 2,000) fixed it: on the 400 km² box,
   4× more loops found, 368 of them 10–15 km, in ~1 s. This flaw exists on relation
   data too; paths only exposed it.

The worry that short compact loops would be town-block walks was checked and is false:
the returned loops are 60–100 % path/track, and 40–100 % of their length rides on
named routes in Krkonoše.

## Design

- **Data (overpass.py):** a new `AreaData.paths` list, filled from one more statement
  in the same Overpass query (`out tags geom`). Kept apart from `routes` for the same
  reason `ferrata_routes` is: `find_hikes` lists every entry of `routes`, and a path
  fragment must never be one. Tri-state like the other late fields — `None` means "this
  data never fetched paths" (an old snapshot, an old cache entry), `[]` means "looked,
  none here". The query change changes the Overpass cache key, so old cache entries
  are simply not hit — no manual invalidation.
- **Graph (compose.py):** every graph build takes `routes + paths`. A path contributes
  geometry and way tags but **no ref** — so `composed_of` still lists only named trails,
  and a segment's refs being empty means "unmarked". The existing micro-edge dedup
  merges a path way with the relation member that is the same OSM way, exactly as it
  already merges two relations sharing a way.
- **Loop search (compose.find_loops):** re-contract the active core before the DFS;
  per-start budget share. Both are inert on the existing fixtures' results or improve
  them; the live-fixture tests will say which.
- **Marked share:** a synthesised hike reports what fraction of its length follows a
  named route (`Hike.marked_frac`), so an all-unmarked loop is visibly that.
- **"No routes" message:** now fires only when there are neither named routes nor
  walkable paths. When paths exist but no named routes, nothing is wrong any more.
- **Area search = named routes + loops**, in all three frontends. `--compose-loops`
  keeps meaning "loops only".
- **Offline snapshots: unchanged for now.** Offline loops would need elevation for the
  whole path network at download time (~80k samples for 400 km², ~800 API requests,
  near the daily quota). Snapshots keep `paths=None`, offline search behaves as today,
  and a notice says loops need a live search. Revisit with local-DEM downloads.

## Stages (each its own commit, suite green)

1. Data layer: query + parse + `AreaData.paths`, cache key moves, parse tests.
2. Engine: graph from routes+paths at every build site; path refs empty; re-contract
   + fair budget; `marked_frac`; no-routes semantics.
3. Frontends: area search returns named routes + loops (CLI, web, MCP) + parity test.
4. Docs: README/GUIDE/HANDOFF/CHANGELOG; live validation in Krkonoše and Kamikōchi.

## Status — all four stages DONE (2026-09-27, unreleased)

- Stage 1 `4b52bf6`, stage 2 `b5a60cf`, stage 3 `3a0fc44`, stage 4 = the docs commit.
- Live, public Overpass + elevation API:
  - Kamikōchi 10 km box, plain area search (no flag): **7 loops** (was the "No hiking
    route relations are mapped" message), 3–12 km, all "unmarked paths only", gain ≈ loss
    on every one (e.g. 5.45 km, +1025/−1023 m).
  - Krkonoše 10 km box, `--compose-loops --min-distance 8 --max-distance 15`: **44
    distinct loops**, 15 shown, 45–99 % waymarked, 41 s, 43 elevation requests.
- Two 504s from the public instance on the heavier query, each answered on a retry a few
  seconds later — retries now wait 2/4/8 s.
- Deviations from the design above: the per-start budget floor is capped at the caller's
  budget (so `budget=0` still reports `capped`); the web checkbox is relabelled
  "Loops only".
- Open, not blocking: long "composed of …" lists on big loops; no difficulty filter for
  demanding unmarked alpine paths (`sac_scale`); offline loops would need a local-DEM
  download path.
