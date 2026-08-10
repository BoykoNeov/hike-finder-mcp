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
    """One sentence, three frontends — so it cannot carry a CLI flag name."""
    assert "--" not in S.no_routes_message()


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
