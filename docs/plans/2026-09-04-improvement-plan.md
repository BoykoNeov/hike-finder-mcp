# Improvement plan — 2026-09-04

A survey of the repo at v0.6.0 + 3 unreleased fixes (commit `5e80217`), and what is
worth doing next. Nothing here is broken in the "tests fail" sense: the suite is
624 tests green in ~35 s, engine coverage is 93–100 % per module, and the design
notes in `HANDOFF.md` are honest about every known gap. The items below are what the
survey turned up beyond that, ranked by value-for-effort.

Effort key: **S** = under half a day, **M** = one to two days, **L** = several days
and worth its own plan.

---

## Tier 1 — small, concrete, do first

### 1.1 `find_hikes` over MCP cannot take a bare area name (S) — DONE

`list_pois` and `list_ferrata` accept `area="cortina"` — the name an LLM has just
read out of `list_areas` — and fall back to the named snapshot directory. `find_hikes`
calls `load_snapshot(area_path)` straight (`server.py` ~line 819) and raises before any
search or caveat runs. It is the only tool on the server that behaves this way, and it
is the one an LLM calls most.

- Fix: route `find_hikes` through the same "try the path, then the named directory,
  turn a read failure into a sentence" helper the other two handlers already use.
- Pin it in `tests/test_server.py` with a name-only call over a hand-built snapshot,
  plus the "no such area" wording.

### 1.2 Doc/metadata drift that will actually mislead someone (S) — DONE

- `requirements.txt` still pins `mcp>=1.2`. `pyproject.toml` requires `mcp>=2` and the
  server no longer runs on 1.x. Anyone who installs from the requirements file gets a
  server that fails at import. Either align it or delete the file and point at
  `pip install -e ".[dev]"` (the README already does).
- `geometry.py` `stitch_ways` docstring says "see HANDOFF.md 'Known limitations' for
  the robust-ordering TODO". No such entry exists any more. Reword to what is true
  now: stitching is greedy and only the `is_circular` gap fallback and export's
  faithfulness gate still ride on it.
- `HIKE_ROUTES_MAX_FACTOR` is read in `config.py` but absent from the README's
  environment-variable table (the other 45 are documented).

### 1.3 Cut v0.6.1 (S)

The changelog's Unreleased section holds three real fixes (short-route loop
mis-labelling, the MCP ferrata caveat, the "avoidance still works" promise on a
routeless file). Follow the existing ritual: tag only after CI is green on the
release commit.

### 1.4 Web point-based modes never carry a notice (S–M) — DONE (all three frontends)

`/api/hikes` grew a `notices` list so the page could say what the *source* could not
answer. The point-based modes (`--around`, `--from/--to`, `--via`, `--to-poi`) return
`notices: []` unconditionally (`web.py` `_area_notices` is only called on the area
path). Decide one of:

- compute the same notices for them (they build a bbox too, so the no-routes and
  ferrata-gap checks apply), or
- document in `HANDOFF.md` that point modes word their failures in the page and are
  exempt on purpose.

The first is better: the ferrata gap was found "silent" in three frontends one at a
time; the fourth silence is this one.

**Done, and wider than written here.** The seam belongs to the engine, so the fix was
not the web's alone: the four point functions fill the same `diagnostics` out-parameter
`search_hikes` does, and the CLI and the MCP server read it too. Wiring only the web
would have shipped the fifth instance of this exact pattern in the release that fixes
the fourth. Verified live at Kamikōchi. See "The point modes say which empty they are"
in `HANDOFF.md`.

---

## Tier 2 — code health (pays for itself on the next feature)

### 2.1 Lint and type baseline in CI (M) — DONE

Nothing runs a linter or type checker today. Measured on the current tree:

| check | result |
|---|---|
| `ruff check` (default rules) | 9 findings, all auto-fixable (unused imports, empty f-strings) |
| `ruff --select E,F,W,I,B,UP,SIM,C4,RUF` | 936, of which 859 are line-length; ~75 real (`zip` without `strict`, collapsible ifs, unused loop vars, unsorted imports) |
| `mypy --ignore-missing-imports` | 51 errors in 13 files; `server.py` 14, `filters.py` 10 |

Plan:
1. Add `[tool.ruff]` to `pyproject.toml` with a line length that matches the existing
   style and the rule set above. Measured: 11 354 source lines are ≤ 88 characters,
   794 sit in 89–100, and only 69 exceed 100. A limit of 100 means wrapping 69 lines
   rather than 859. Auto-fix the safe findings, hand-fix the rest.
2. Add `[tool.mypy]` and burn the 51 errors down. Most are `Optional` narrowing and
   the `**kwargs` to `asyncio.to_thread` call in `server.py`. None looked like a real
   bug at a glance, but 51 is few enough to get to zero and keep there.
3. Add a `lint` job to `ci.yml` (one Python version is enough). Ship `py.typed` once
   mypy is clean so downstream users of the engine get types.

**Done, all three steps.** Line length 100 as planned; the rule set gained `BLE` and `SLF`
because the tree already carried `noqa` comments naming them, which `RUF100` would
otherwise have deleted along with their reasoning. mypy measured 61 at the declared 3.10
floor (not 51 — a newer mypy), now zero. None was a bug; the six that looked like one are
written up in `HANDOFF.md`. The lint job is pinned while the test matrix stays floating,
and that asymmetry is deliberate — see the comment in `ci.yml`.

### 2.2 The three frontends are wired by hand, three times (L)

The CLI declares 39 flags in `build_parser` (288 lines). `server.py`'s `list_tools`
is 452 lines of hand-written JSON schema for 10 tools. `web.py` parses its own query
parameters. Every filter added in the last five releases was wired three times, and
the changelog shows the cost: "MCP `find_hikes` was the last frontend that answered
from nothing" — the caveat reached each frontend on a different day.

What already keeps them honest: all three build one `Criteria` and call one engine.
What does not exist: anything that checks the three *surfaces* expose the same set of
criteria.

Options, cheapest first:
- **Parity test only (S) — DONE:** `tests/test_frontend_parity.py`. One table per
  `Criteria` field (CLI flag, MCP argument, web query parameter, expected value) plus a
  gate asserting the table covers every field. The MCP side is checked twice — the
  handler reading the key and the schema declaring it — because those break separately,
  and the schema half is the one that breaks an LLM client. It found eight filters the
  point-mode tools honour but did not advertise — gain and length, on modes where the CLI
  has always offered them; those are now in the four schemas, with
  `tests/test_point_mode_filters.py` checking each mode applies what it promises. The
  twelve arguable/inapplicable omissions stay listed in `HANDOFF.md`.
- **One option table (L):** a single declarative list of options (name, type, help,
  which frontends) that generates the argparse arguments, the MCP `inputSchema`, and
  the web parser. `list_tools` shrinks to a loop. This is a refactor of ~800 lines
  across three files; the payoff is every future filter being wired once.

Recommendation: do the parity test now; do the table only when the next filter lands.
The parity test is in. The one option table (L) is still open, and the parity test is
what would keep it honest if it is ever built.

### 2.3 Long functions in the frontends (M)

The engine's biggest functions are justified by the maths they carry
(`routes_to_poi` 238 lines, `find_loops` 167). The frontend ones are dispatch:

- `cli.run` — 433 lines, one function that owns every mode. `_show_pois`,
  `_show_ferrata` and `_print_areas` show the pattern already taken for three modes;
  finish it (one handler per mode, `run` becomes a dispatcher).
- `server.list_tools` — 452 lines; see 2.2.
- `web._resolve_hikes` — 157 lines, area and point modes interleaved.

Frontend coverage is the lowest in the repo (`web.py` 71 %, `cli.py` 73 %,
`server.py` 74 %) and the uncovered lines are almost all error branches inside these
functions. Splitting them makes those branches testable with a monkeypatched fetch.

---

## Tier 3 — features (user decides; listed with the case for each)

### 3.1 Place names instead of coordinates (M) — DONE

Today the CLI and MCP need four bbox corners, or a lat/lon pair for `--around`,
`--from`, `--to`, `--via`. The README sends you to openstreetmap.org's Export tab to
read them off. An LLM client has it worse: it guesses coordinates.

`geocode.py` already talks to Nominatim (reverse direction, for naming routes). The
forward direction is one endpoint (`/search`) and gives a bounding box for free:

- `--place "Špindlerův Mlýn"` → bbox, with `--radius-km` to grow it.
- `--from "Špindlerův Mlýn" --to "Sněžka"` → the two points.
- MCP `find_hikes(place=…)` alongside the existing corners.

Goes through the same cache and user-agent rules as reverse geocoding; one call per
name. Ambiguity ("Lhota") must be surfaced, not resolved silently: print the chosen
match and its country, and offer `--place-index`.

**Done, and wider than written here.** `--place` / `--place-radius` / `--place-index` on
the CLI, names accepted by all four point flags, and `place` on every MCP tool that took
an area or a point — not just `find_hikes`, because a capability true of six tools out of
ten is how every previous filter reached the frontends on three different days. Two things
the plan did not anticipate: a summit's mapped extent is 0.01 km across, so an extent floor
(`HIKE_PLACE_MIN_KM`, widened per axis and always *reported* as widened) is what stops the
feature returning a baffling empty answer; and letting the point flags take a name turns
`--via 50.7 15.6 50.8 15.7` from an argparse error into a bad lookup, so that is caught by
hand. Forward geocoding raises where the reverse direction returns `None` — its answer is
which ground gets searched. The web UI is exempt (its map is the place picker) and
`HANDOFF.md` records both the decision and the fact that the parity test cannot catch it.

### 3.2 `--show-ferrata` export (M)

Already a named gap: the browse mode cannot write GPX/GeoJSON because
`ferrata.FerrataLine` carries a start point, not the line. Widen the record to carry
the member geometry, add a line exporter (`pois_to_gpx` writes waypoints; these are
tracks). `--show-pois` set the precedent that a browse mode exports.

### 3.3 Web UI without network for the page itself (S)

The web UI loads Leaflet from `unpkg.com`. "Search offline from a snapshot" therefore
still needs the internet to draw the page. Vendor Leaflet 1.9.4 (~150 KB JS+CSS) into
the package under `hike_finder/static/` and serve it locally. Map tiles stay online
(unavoidable); say so in the page when they fail to load.

### 3.4 Regions with no route relations (L) — needs a deliberate decision

Kamikōchi has 824 mapped paths and zero route relations, so every mode returns
nothing there. `HANDOFF.md` rejects a silent `highway=path` fallback (it widens every
query, invalidates the cache, and changes what "a route" means) and asks for it to be
decided on purpose. The shape that respects those objections:

- opt-in only: `--include-unmarked-paths`;
- a separate Overpass query (so the cache key for the default search is untouched);
- results labelled distinctly (`unmarked path`, no route ref), never mixed into the
  "marked route" count;
- composed loops only, since raw paths are fragments — which is what
  `--compose-loops` already exists for.

Worth it only if the user plans to walk somewhere OSM has no relations. Otherwise
leave it as the documented limitation it is.

### 3.5 Smaller things, take or leave

- **Snapshot compression:** a 400 km² area is a 732 KB JSON. Writing `.json.gz` and
  loading either would make snapshots ten times smaller. Only matters if snapshots
  start being shared or archived.
- **Overpass mirrors:** transient-status retry exists; a configurable fallback
  endpoint list (`HIKE_OVERPASS_URLS`) would survive an `overpass-api.de` outage.
  Not needed until it bites.
- **PyPI publish:** parked on purpose, metadata ready, Trusted Publishing via a
  tag-triggered workflow is the path when wanted.

---

## Tier 4 — documentation shape (optional)

`HANDOFF.md` is 82 KB. The "Known limitations" section alone is 330 lines of
design essays, each one valuable and each one hard to find. The README is 900 lines
and `GUIDE.md` 1100. All three are accurate; none is short.

If the orientation doc should orient: keep `HANDOFF.md` to the goal, the user's
context, the architecture, and a one-line index; move each design essay to
`docs/design/<topic>.md` (the ferrata-detection rules, the four snapshot states, the
`--to-poi` "nearest" bounds, the surface gates, the mcp 2.x port) and link them. The
content moves, it does not shrink. Do this only if a new reader is expected; for a
single-maintainer repo the current form works.

---

## Suggested order

1. Tier 1 in one sitting (1.1 → 1.2 → 1.4), then 1.3 releases v0.6.1.
2. 2.1 lint/type baseline into CI, then 2.2's parity test.
3. 3.1 place names — the one feature that changes how the tool is used day to day.
4. 2.3 and 3.2 as time allows; 3.3 is a free afternoon.
5. 3.4 and Tier 4 only on an explicit decision.

## What the survey did not find

- No failing or flaky tests; no skipped tests outside the optional-extra guards.
- No `TODO`/`FIXME` markers in the source beyond the stale docstring in 1.2.
- No secrets, no committed build artefacts, `.gitignore` is right.
- The two elevation backends, the cache, the quota counter, and the snapshot format
  all look sound; nothing in them made the list.
