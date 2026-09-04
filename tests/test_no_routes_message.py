"""An empty result must say WHICH empty it is.

"Your criteria excluded everything" and "nothing here is mapped as a hiking route" are
different facts and take different fixes, and before this the app said the first when it
meant the second — sending the user to widen a distance band that was never the problem.

Not hypothetical. The app reads only ``route=hiking`` / ``route=foot`` relations, and a
~400 km² box over Japan's North Alps (Kamikōchi) carries **zero** of them against 138 in
the Krkonoše box the project was built on, while mapping 824 individual path ways the
app never looks at. Every search there comes back empty.

Network-free: the live path is exercised with a stubbed ``_fetch_area``, the same trick
the compose / routing / POI-listing live tests use.
"""
from __future__ import annotations

import pytest

from hike_finder import search as S
from hike_finder.cli import build_parser, run
from hike_finder.overpass import AreaData
from hike_finder.snapshot import AreaSnapshot, save_snapshot

BBOX = (50.72, 15.58, 50.78, 15.68)

# One real relation, enough to make the area "mapped" without matching anything.
_ROUTE = {
    "id": 1,
    "name": "T",
    "ref": None,
    "unnamed": False,
    "tags": {"route": "hiking", "name": "T"},
    "ways": [[(50.73, 15.60), (50.74, 15.61)]],
    "way_tags": [{}],
}


class _Flat:
    """Deterministic elevation, no network — these tests never reach a real route."""

    def lookup(self, pts):
        return [0.0 for _ in pts]


def _parse(*argv):
    return build_parser().parse_args(list(argv))


def _snapshot(path, routes):
    save_snapshot(
        AreaSnapshot(
            bbox=BBOX,
            area=AreaData(routes=list(routes)),
            elevations={},
            sample_interval_m=25.0,
        ),
        path,
    )


# --- the fact itself ----------------------------------------------------------


def test_area_with_no_route_relations_is_recognised():
    assert S.area_has_no_routes(AreaData(routes=[])) is True


def test_an_area_with_routes_is_not_a_no_routes_area():
    """Routes that all fail the filters are still routes — that is the whole distinction."""
    assert S.area_has_no_routes(AreaData(routes=[_ROUTE])) is False


def test_the_message_does_not_blame_the_user_s_filters():
    msg = S.no_routes_message()
    assert "map data" in msg and "not your filters" in msg
    # Names the actual data source, so the reader can check the claim themselves.
    assert "route=hiking" in msg


def test_the_message_is_frontend_neutral():
    """One sentence, several frontends — so it cannot name a CLI flag.

    The MCP server and the web UI have no ``--bbox`` to widen, and telling an LLM to pass
    one sends it looking for an argument that does not exist. Checks the specific flags
    this sentence would plausibly reach for rather than a bare ``"--"``, which the message
    already trips over for innocent reasons (an em-dash reads as two hyphens to nobody,
    but ``route=hiking`` and future punctuation make a blanket ban a trap).
    """
    msg = S.no_routes_message()
    for flag in ("--bbox", "--area", "--min-distance", "--max-distance", "--poi"):
        assert flag not in msg


# --- the live path ------------------------------------------------------------


@pytest.mark.parametrize("routes, expected", [([], True), ([_ROUTE], False)])
def test_search_hikes_reports_the_fact_through_diagnostics(monkeypatch, routes, expected):
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: AreaData(routes=list(routes)))
    diagnostics: dict = {}
    S.search_hikes(BBOX, S.Criteria(), diagnostics=diagnostics)
    assert diagnostics["no_routes"] is expected


def test_diagnostics_is_optional(monkeypatch):
    """Every existing caller omits it, and must keep working untouched."""
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: AreaData(routes=[]))
    assert S.search_hikes(BBOX, S.Criteria()) == []


# --- the offline path ---------------------------------------------------------


def test_offline_snapshot_of_an_unmapped_region_says_so(tmp_path, capsys):
    """A snapshot of a region with no route relations saves fine and simply holds none."""
    path = tmp_path / "kamikochi.json"
    _snapshot(path, [])
    assert run(_parse("--area", str(path))) == 0
    out = capsys.readouterr().out
    assert "No hiking route relations are mapped" in out
    assert "No matching hikes found" not in out


def test_offline_snapshot_with_routes_keeps_the_criteria_wording(tmp_path, capsys):
    """The override must not swallow the ordinary case — routes exist, filters excluded them."""
    path = tmp_path / "krkonose.json"
    _snapshot(path, [_ROUTE])
    assert run(_parse("--area", str(path), "--min-distance", "500")) == 0
    out = capsys.readouterr().out
    assert "No matching hikes found" in out
    assert "No hiking route relations are mapped" not in out


# --- the point-based modes ----------------------------------------------------
#
# `--around`, `--from/--to`, `--via` and `--to-poi` derive their own bbox from the point(s)
# you picked, and until now none of them could report this fact at all: they returned
# hikes and nothing else, so every frontend answered an empty search there by blaming a
# radius, a snap distance or a filter. Kamikochi is the case — 824 mapped paths, zero
# route relations — and picking a point in it is the most natural way to search it.


@pytest.mark.parametrize("routes, expected", [([], True), ([_ROUTE], False)])
@pytest.mark.parametrize("mode", ["around", "between", "via", "to_poi"])
def test_the_point_modes_report_the_fact_through_the_same_diagnostics(
    monkeypatch, mode, routes, expected
):
    """One seam, four entry points, and the same key `search_hikes` fills.

    Set from the ONE area fetch each of them makes, before any snapping or routing, so
    the answer does not depend on how far the picked points landed from a trail — those
    have their own messages and their own fixes.
    """
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: AreaData(routes=list(routes)))
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _Flat())
    p, q = (50.73, 15.60), (50.74, 15.62)
    diagnostics: dict = {}
    if mode == "around":
        S.compose_loops_around(p, S.Criteria(), diagnostics=diagnostics)
    elif mode == "between":
        S.routes_between(p, q, S.Criteria(), diagnostics=diagnostics)
    elif mode == "via":
        S.route_via([p, q], S.Criteria(), diagnostics=diagnostics)
    else:
        S.routes_to_poi(p, ("ruins",), S.Criteria(), diagnostics=diagnostics)
    assert diagnostics["no_routes"] is expected


def test_to_poi_reports_missing_TRAILS_not_missing_destinations(monkeypatch):
    """The one place the two empties could be conflated, since one fetch answers both.

    `--to-poi` comes back empty either because the map holds no route relations or
    because it holds no churches — different facts, different fixes, and only the first
    is what `no_routes` means. An area full of trails and free of ruins must NOT be
    reported as a stretch of map with nothing mapped in it.
    """
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: AreaData(routes=[_ROUTE], pois=[]))
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _Flat())
    diagnostics: dict = {}
    assert S.routes_to_poi((50.73, 15.60), ("ruins",), S.Criteria(),
                           diagnostics=diagnostics) == []
    assert diagnostics["no_routes"] is False


def test_the_point_modes_diagnostics_stay_optional(monkeypatch):
    """Every existing caller omits the keyword and must keep working untouched."""
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: AreaData(routes=[]))
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _Flat())
    p, q = (50.73, 15.60), (50.74, 15.62)
    assert S.compose_loops_around(p, S.Criteria()) == []
    assert S.routes_between(p, q, S.Criteria()) == []
    assert S.route_via([p, q], S.Criteria()) == []
    assert S.routes_to_poi(p, ("ruins",), S.Criteria()) == []


# --- the point modes on the CLI -----------------------------------------------
#
# The engine seam above is only half the fix: it makes the fact AVAILABLE, and a frontend
# that never reads it still answers Kamikochi by blaming a radius. This is the shape the
# repo keeps re-learning — the ferrata caveat reached three frontends on three different
# days — so all three read it in the same release this time. Stubbed at the engine's fetch
# rather than at the CLI's imports, so the whole chain (flag → engine → diagnostics →
# printed sentence) is what is under test.


def _live_stub(monkeypatch, routes):
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: AreaData(routes=list(routes)))
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _Flat())
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)


_CLI_POINT_MODES = {
    "around": ("--around", "50.73", "15.60"),
    "between": ("--from", "50.72", "15.58", "--to", "50.74", "15.62"),
    "via": ("--via", "50.72", "15.58", "--via", "50.74", "15.62"),
    "to_poi": ("--from", "50.73", "15.60", "--to-poi", "ruins"),
}

# What each mode says INSTEAD when it does not know — every one of them names a lever
# that was not the problem, which is the reason this fix exists.
_CLI_ADVICE = {
    "around": "widen --around-radius",
    "between": "disconnected trail networks",
    "via": "off-network",
    "to_poi": "widen --to-poi-radius",
}


@pytest.mark.parametrize("mode", sorted(_CLI_POINT_MODES))
def test_cli_point_modes_blame_the_map_not_your_point(monkeypatch, capsys, mode):
    _live_stub(monkeypatch, [])
    assert run(_parse(*_CLI_POINT_MODES[mode])) == 0
    out = capsys.readouterr().out
    assert "No hiking route relations are mapped" in out
    assert _CLI_ADVICE[mode] not in out          # displaced, not printed beside it


@pytest.mark.parametrize("mode", sorted(_CLI_POINT_MODES))
def test_cli_point_modes_keep_their_own_wording_where_the_map_has_routes(
    monkeypatch, capsys, mode
):
    """The half that makes it a signal. One relation nowhere near the picked point: the
    search is still empty, and the honest answer is the mode's own advice."""
    _live_stub(monkeypatch, [_ROUTE])
    assert run(_parse(*_CLI_POINT_MODES[mode], "--min-distance", "500")) == 0
    out = capsys.readouterr().out
    assert "No hiking route relations are mapped" not in out
    assert _CLI_ADVICE[mode] in out
