"""``--place`` / ``place`` on the two frontends that need it.

The engine half lives in ``test_places.py``. This file is about the surfaces: that a
typed name reaches the SAME modes on the CLI and over MCP, that what it resolved to is
always said out loud, and that the two shapes of mistake a name introduces — an
ambiguous name, and a name where a coordinate pair was meant — are caught rather than
guessed at.

The web UI is deliberately absent; it has a map, which IS a place picker. See
``HANDOFF.md`` — and note that ``test_frontend_parity.py`` cannot catch that gap,
because it tables ``Criteria`` fields and a place name is not one.

No network: ``places.place_searcher`` is stubbed with a small gazetteer.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")  # the MCP server is an optional extra; skip if absent

from test_server import create_connected_server_and_client_session

from hike_finder import server
from hike_finder.cli import build_parser, run
from hike_finder.geocode import GeocodeError, _parse_matches

_GAZETTEER = {
    "spindleruv mlyn": [
        {
            "lat": "50.7256",
            "lon": "15.6062",
            "display_name": "Špindlerův Mlýn, Czechia",
            "boundingbox": ["50.6600", "50.7800", "15.5300", "15.6900"],
            "address": {"country": "Czechia"},
        }
    ],
    "snezka": [
        {
            "lat": "50.7360",
            "lon": "15.7400",
            "display_name": "Sněžka, Czechia",
            "boundingbox": ["50.7358", "50.7362", "15.7396", "15.7404"],
            "address": {"country": "Czechia"},
        }
    ],
    "pec": [
        {
            "lat": "50.6930",
            "lon": "15.7320",
            "display_name": "Pec pod Sněžkou, Czechia",
            "boundingbox": ["50.6800", "50.7100", "15.7100", "15.7600"],
            "address": {"country": "Czechia"},
        }
    ],
    "lhota": [
        {
            "lat": f"50.{n}",
            "lon": "14.5",
            "display_name": f"Lhota {n}, Czechia",
            "boundingbox": ["50.0", "50.1", "14.4", "14.6"],
            "address": {"country": "Czechia"},
        }
        for n in (1, 2, 3)
    ],
}


class _Gazetteer:
    def __init__(self):
        self.queries: list[str] = []

    def search(self, query, *, limit=5):
        self.queries.append(query)
        return _parse_matches(_GAZETTEER.get(" ".join(query.split()).casefold(), []))


@pytest.fixture(autouse=True)
def gazetteer(monkeypatch):
    g = _Gazetteer()
    monkeypatch.setattr("hike_finder.places.place_searcher", lambda cfg, cache=None: g)
    return g


# ---------------------------------------------------------------------- the CLI

def _run(argv, monkeypatch, searcher=None):
    """Parse and run, stubbing the search itself — we are testing the name, not the hike."""
    args = build_parser().parse_args(argv)
    seen: dict = {}

    def _search(bbox, criteria, cfg=None, **kw):
        seen["bbox"] = bbox
        return []

    for name in ("search_hikes", "compose_loops_around", "routes_between", "route_via"):
        def _capture(*a, _n=name, **kw):
            seen[_n] = a
            return []

        stub = _search if name == "search_hikes" else _capture
        monkeypatch.setattr(f"hike_finder.cli.{name}", stub)
    return run(args), seen


def test_place_becomes_the_bbox(monkeypatch, capsys):
    _, seen = _run(["--place", "Spindleruv Mlyn"], monkeypatch)
    assert seen["bbox"] == (50.66, 15.53, 50.78, 15.69)
    # Provenance goes to STDERR: --json output has to stay machine-readable, and a
    # search of the wrong Lhota looks exactly like a search of the right one.
    err = capsys.readouterr().err
    assert "Špindlerův Mlýn" in err and "Czechia" in err


def test_place_provenance_stays_off_stdout_under_json(monkeypatch, capsys):
    _run(["--place", "Spindleruv Mlyn", "--json"], monkeypatch)
    out = capsys.readouterr()
    assert "Špindlerův Mlýn" in out.err
    assert "Špindlerův Mlýn" not in out.out


def test_place_radius_replaces_the_mapped_extent(monkeypatch, capsys):
    _, seen = _run(["--place", "Snezka", "--place-radius", "5"], monkeypatch)
    south, _west, north, _east = seen["bbox"]
    assert north - south == pytest.approx(0.0898, abs=0.002)  # ~10 km tall
    assert "radius 5 km" in capsys.readouterr().err


def test_a_point_sized_place_says_it_was_widened(monkeypatch, capsys):
    _run(["--place", "Snezka"], monkeypatch)
    err = capsys.readouterr().err
    assert "mapped extent" in err and "widened to" in err


def test_named_points_reach_the_point_modes(monkeypatch, capsys):
    _, seen = _run(["--from", "Pec", "--to", "Snezka"], monkeypatch)
    start, finish = seen["routes_between"][0], seen["routes_between"][1]
    assert start == (50.693, 15.732)
    assert finish == (50.736, 15.74)
    err = capsys.readouterr().err
    assert "From: Pec pod Sněžkou" in err and "To: Sněžka" in err


def test_named_waypoints_reach_via(monkeypatch):
    _, seen = _run(["--via", "Pec", "--via", "Snezka"], monkeypatch)
    assert seen["route_via"][0] == [(50.693, 15.732), (50.736, 15.74)]


def test_coordinates_still_work_including_negative_longitudes(monkeypatch, gazetteer):
    """The point flags take free tokens now so a name can reach them. Every existing
    invocation must be untouched — and a western longitude looks like an option flag,
    which is exactly the sort of thing a loosened parser breaks silently."""
    _, seen = _run(["--from", "50.7", "-3.5", "--to", "50.8", "-3.4"], monkeypatch)
    assert seen["routes_between"][0] == (50.7, -3.5)
    assert seen["routes_between"][1] == (50.8, -3.4)
    assert gazetteer.queries == []  # no lookup happened at all


def test_several_coordinates_under_one_via_is_an_error_not_a_place_name(monkeypatch, capsys):
    """This used to be a loud argparse error. Now that --via takes free tokens it could
    become a lookup of the "place" 50.7 15.6 50.8 15.7, which Nominatim answers with
    nothing — or worse, with something."""
    code, _ = _run(["--via", "50.7", "15.6", "50.8", "15.7"], monkeypatch)
    assert code == 2
    err = capsys.readouterr().err
    assert "--via takes LAT LON" in err and "4 number(s)" in err


def test_a_name_containing_a_number_is_still_a_name(monkeypatch, gazetteer):
    """The guard above tests ALL tokens being numeric, so "Chata 1000" is unaffected."""
    _run(["--around", "Chata", "1000"], monkeypatch)
    assert gazetteer.queries == ["Chata 1000"]


def test_ambiguous_name_lists_the_alternatives(monkeypatch, capsys):
    _run(["--place", "Lhota"], monkeypatch)
    err = capsys.readouterr().err
    assert "match 1 of 3" in err
    assert "Lhota 2" in err and "Lhota 3" in err


def test_place_index_picks_another_match(monkeypatch):
    _, seen = _run(["--place", "Lhota", "--place-index", "2"], monkeypatch)
    # Every Lhota shares a bbox in the fixture, so check the one that differs.
    assert seen["bbox"] == (50.0, 14.4, 50.1, 14.6)


def test_unknown_place_fails_with_a_spelling_hint(monkeypatch, capsys):
    code, _ = _run(["--place", "Atlantis"], monkeypatch)
    assert code == 2
    assert "no place matched" in capsys.readouterr().err


def test_place_and_bbox_together_is_rejected_without_a_lookup(monkeypatch, capsys, gazetteer):
    code, _ = _run(["--place", "Snezka", "--bbox", "1", "2", "3", "4"], monkeypatch)
    assert code == 2
    assert "both say where to search" in capsys.readouterr().err
    assert gazetteer.queries == []  # the contradiction costs no Nominatim request


def test_place_with_a_point_mode_points_at_the_right_flag(monkeypatch, capsys):
    code, _ = _run(["--place", "Snezka", "--around", "50.7", "15.6"], monkeypatch)
    assert code == 2
    err = capsys.readouterr().err
    assert "--around" in err and "--place names the AREA" in err


def test_place_radius_without_place_is_rejected(monkeypatch, capsys):
    code, _ = _run(["--bbox", "1", "2", "3", "4", "--place-radius", "5"], monkeypatch)
    assert code == 2
    assert "--place-radius only applies to --place" in capsys.readouterr().err


def test_place_index_without_any_name_is_rejected(monkeypatch, capsys):
    """A flag that silently does nothing is the shape of bug this repo keeps finding."""
    code, _ = _run(["--bbox", "1", "2", "3", "4", "--place-index", "2"], monkeypatch)
    assert code == 2
    assert "gave none" in capsys.readouterr().err


def test_place_feeds_the_download_and_browse_modes(monkeypatch):
    """`--place` is simply another way to give `--bbox`, so it has to reach the modes
    that take an area and draw no routes — saving a snapshot, and listing what is there.
    Those dispatch on their own branches well before the search path, which is exactly
    where a resolution wired only into the search would go missing."""
    seen: dict = {}

    def _download(bbox, cfg=None, **kw):
        seen["download"] = bbox
        raise SystemExit  # far enough: the bbox is what this test is about

    def _pois(bbox, kinds, cfg, **kw):
        seen["pois"] = bbox
        return []

    def _ferrata(bbox, cfg, **kw):
        seen["ferrata"] = bbox
        return []

    monkeypatch.setattr("hike_finder.cli.download_area", _download)
    monkeypatch.setattr("hike_finder.cli.list_area_pois", _pois)
    monkeypatch.setattr("hike_finder.cli.list_area_ferrata", _ferrata)

    with pytest.raises(SystemExit):
        _run(["--place", "Spindleruv Mlyn", "--download", "out.json"], monkeypatch)
    assert seen["download"] == (50.66, 15.53, 50.78, 15.69)

    _run(["--place", "Spindleruv Mlyn", "--show-pois"], monkeypatch)
    assert seen["pois"] == (50.66, 15.53, 50.78, 15.69)

    _run(["--place", "Spindleruv Mlyn", "--show-ferrata"], monkeypatch)
    assert seen["ferrata"] == (50.66, 15.53, 50.78, 15.69)


def test_every_missing_area_message_names_place_too(monkeypatch, capsys):
    """A user who mistypes a place name lands on one of these. Telling them to use
    --bbox would send them back to the coordinates --place exists to spare them."""
    for argv in (["--min-gain", "100"], ["--show-pois"], ["--show-ferrata"]):
        assert _run(argv, monkeypatch)[0] == 2
        err = capsys.readouterr().err
        assert "--place" in err and "--bbox" in err and "--area" in err, argv


# ------------------------------------------------------------------ the MCP server

def _call(tool, args):
    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(tool, args)

    return asyncio.run(_impl())


def test_every_area_tool_advertises_place():
    """Not just find_hikes. A capability true of some tools and not others is how every
    previous filter reached the frontends on three different days."""
    tools = {t.name: t.input_schema for t in asyncio.run(server.list_tools()).tools}
    for name in ("find_hikes", "list_pois", "list_ferrata", "download_area"):
        props = tools[name]["properties"]
        assert "place" in props, name
        assert "place_radius_km" in props, name
        assert "place_index" in props, name


def test_every_point_tool_advertises_a_named_point():
    tools = {t.name: t.input_schema for t in asyncio.run(server.list_tools()).tools}
    assert "place" in tools["circular_routes"]["properties"]
    assert "place" in tools["routes_to_poi"]["properties"]
    assert "start_place" in tools["routes_between"]["properties"]
    assert "finish_place" in tools["routes_between"]["properties"]
    assert "place" in tools["route_via"]["properties"]["points"]["items"]["properties"]


def test_point_tools_no_longer_require_the_coordinates():
    """`required` is what an LLM reads as the contract, and it cannot express "either
    a name or a pair" — so neither shape is listed and the descriptions carry the rule.
    `kinds` and `path`, which are unconditional, stay."""
    tools = {t.name: t.input_schema for t in asyncio.run(server.list_tools()).tools}
    assert tools["circular_routes"]["required"] == []
    assert tools["routes_between"]["required"] == []
    assert tools["routes_to_poi"]["required"] == ["kinds"]
    assert tools["download_area"]["required"] == ["path"]


def test_find_hikes_accepts_a_place_and_reports_what_it_took(monkeypatch):
    seen: dict = {}

    def _search(bbox, criteria, cfg=None, **kw):
        seen["bbox"] = bbox
        return []

    monkeypatch.setattr(server, "search_hikes", _search)
    result = _call("find_hikes", {"place": "Spindleruv Mlyn"})
    assert not result.is_error
    assert seen["bbox"] == (50.66, 15.53, 50.78, 15.69)
    assert any("Špindlerův Mlýn" in c.text for c in result.content)


def test_circular_routes_accepts_a_place(monkeypatch):
    seen: dict = {}

    def _around(point, criteria, cfg=None, **kw):
        seen["point"] = point
        return []

    monkeypatch.setattr(server, "compose_loops_around", _around)
    result = _call("circular_routes", {"place": "Snezka"})
    assert seen["point"] == (50.736, 15.74)
    assert any("Around: Sněžka" in c.text for c in result.content)


def test_routes_between_accepts_two_names(monkeypatch):
    seen: dict = {}

    def _between(start, finish, criteria, cfg=None, **kw):
        seen["ends"] = (start, finish)
        return []

    monkeypatch.setattr(server, "routes_between", _between)
    result = _call(
        "routes_between", {"start_place": "Pec", "finish_place": "Snezka"}
    )
    assert seen["ends"] == ((50.693, 15.732), (50.736, 15.74))
    text = "\n".join(c.text for c in result.content)
    assert "From: Pec pod Sněžkou" in text and "To: Sněžka" in text


def test_route_via_accepts_named_waypoints(monkeypatch):
    seen: dict = {}

    def _via(points, criteria, cfg=None, **kw):
        seen["points"] = points
        return []

    monkeypatch.setattr(server, "route_via", _via)
    result = _call("route_via", {"points": [{"place": "Pec"}, {"place": "Snezka"}]})
    assert seen["points"] == [(50.693, 15.732), (50.736, 15.74)]
    text = "\n".join(c.text for c in result.content)
    assert "Via 1: Pec pod Sněžkou" in text and "Via 2: Sněžka" in text


def test_a_waypoint_may_be_a_name_and_its_neighbour_a_coordinate(monkeypatch):
    seen: dict = {}

    def _via(points, criteria, cfg=None, **kw):
        seen["points"] = points
        return []

    monkeypatch.setattr(server, "route_via", _via)
    _call("route_via", {"points": [{"place": "Pec"}, {"lat": 50.8, "lon": 15.8}]})
    assert seen["points"] == [(50.693, 15.732), (50.8, 15.8)]


def test_place_index_reaches_a_waypoint(monkeypatch):
    """The index is given once for the whole call, so it has to be carried into each
    waypoint's own little argument object — or a named waypoint would always take match
    1 while the same name on circular_routes honoured the index.

    One name per call is the documented limit of a call-level index: it applies to EVERY
    name, so pairing an ambiguous "Lhota" with a "Snezka" that has one match asks for a
    second match that does not exist, and the call fails rather than quietly taking the
    first. The next test pins that.
    """
    seen: dict = {}

    def _via(points, criteria, cfg=None, **kw):
        seen["points"] = points
        return []

    monkeypatch.setattr(server, "route_via", _via)
    _call(
        "route_via",
        {"points": [{"place": "Lhota"}, {"lat": 50.8, "lon": 15.8}], "place_index": 2},
    )
    assert seen["points"] == [(50.2, 14.5), (50.8, 15.8)]


def test_an_index_past_a_names_matches_fails_loudly(monkeypatch):
    """An index the name cannot honour is an error naming how many matches there were —
    never a silent fall back to the first, which would be the wrong walk reported as the
    right one."""
    monkeypatch.setattr(server, "route_via", lambda *a, **kw: [])
    result = _call(
        "route_via",
        {"points": [{"place": "Lhota"}, {"place": "Snezka"}], "place_index": 2},
    )
    assert result.is_error
    assert "1..1" in result.content[0].text


def test_the_note_trails_the_payload_so_gpx_stays_the_first_block(monkeypatch):
    """Under format="gpx" the first content block IS the file. A client writing
    content[0] out must still get a valid track, so provenance rides behind it."""
    from hike_finder.filters import Hike

    hike = Hike(
        osm_id=1, name="Alpha loop", distance_km=8.3, circular=True,
        car_access=True, chairlift_access=False, start=(50.7, 15.6),
        gain_m=540, loss_m=535,
    )
    monkeypatch.setattr(server, "search_hikes", lambda *a, **kw: [hike])
    result = _call("find_hikes", {"place": "Spindleruv Mlyn", "format": "gpx"})
    assert result.content[0].text.lstrip().startswith("<?xml")
    assert "Špindlerův Mlýn" in result.content[-1].text


def test_an_unknown_place_comes_back_readable_not_as_a_protocol_error():
    """An LLM has to READ "no place matched" to re-spell it; a JSON-RPC error it never
    sees is useless. Same channel as an unregistered POI kind."""
    result = _call("find_hikes", {"place": "Atlantis"})
    assert result.is_error
    assert "no place matched" in result.content[0].text


def test_a_lookup_failure_comes_back_readable_too(monkeypatch):
    def _boom(cfg, cache=None):
        class _Down:
            def search(self, q, *, limit=5):
                raise GeocodeError("could not look up 'Snezka': connection refused")

        return _Down()

    monkeypatch.setattr("hike_finder.places.place_searcher", _boom)
    result = _call("circular_routes", {"place": "Snezka"})
    assert result.is_error
    assert "could not look up" in result.content[0].text


def test_missing_both_a_name_and_coordinates_names_both_ways():
    result = _call("circular_routes", {})
    assert result.is_error
    text = result.content[0].text
    assert "`place`" in text and "lat" in text
