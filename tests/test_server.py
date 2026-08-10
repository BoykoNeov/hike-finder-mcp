"""Pin the MCP frontend the way test_cli.py pins the CLI.

The MCP server (``src/hike_finder/server.py``) is the third frontend over the
one shared engine. ``test_cli.py`` pins the CLI's own glue *offline* — the
args->Criteria tri-state mapping and the shared formatter — without running
``search_hikes``. This does the same for the MCP server: it drives the REAL
server through the REAL MCP protocol (an in-memory client/server session — the
same JSON-RPC machinery as stdio, just without OS pipes), with the
network-touching engine stubbed, and asserts:

  - ``list_tools`` advertises ``find_hikes`` with the right schema (the four
    required corners and the tri-state boolean filters);
  - ``call_tool`` maps the flat arguments dict onto a bbox in (S, W, N, E)
    order and a ``Criteria`` with every field, INCLUDING the tri-state booleans
    (omit -> None, true -> True, false -> False) — the easy-to-break part,
    exactly as test_cli emphasises for the CLI;
  - the result is rendered with the SAME ``format_hike`` as the CLI/web, the
    empty case is the friendly message, and an unknown tool surfaces as an error.

A final test runs the call through the REAL engine (geometry + access + format)
against the live Spindleruv Mlyn fixture, with only the two network boundaries
(Overpass fetch, elevation provider) stubbed — confirming the MCP entry point
reaches the shared engine and ships sane, real-data hikes.

The end-to-end run over a real OS-pipe subprocess (``python -m hike_finder.server``)
is a manual, network-touching validation, not part of this offline suite (see
HANDOFF.md). This module needs the optional ``mcp`` extra; it is skipped without it.
"""
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")  # the MCP server is an optional extra; skip if absent
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.memory import create_client_server_memory_streams

from dataclasses import replace


from hike_finder import server
from hike_finder.filters import Criteria, Hike
from hike_finder.format import format_hike

_SRC = str(Path(__file__).resolve().parent.parent / "src")


@asynccontextmanager
async def create_connected_server_and_client_session(srv):
    """Stand-in for the helper of the same name that mcp 2.x removed.

    Keeping the name and the shape means the ~23 call sites below read exactly as they
    did under 1.x, so this port shows up as a change of PLUMBING and not as a rewrite of
    what is being asserted. It is still the real JSON-RPC machinery — an in-memory pair
    of streams instead of OS pipes — so these tests still drive the real protocol.

    `raise_exceptions` is deliberately left at its default False, matching what `main()`
    runs in production: with it on, a handler that raises tears down the task group
    instead of coming back as a JSON-RPC error, and `test_unknown_tool_is_an_error`
    would be testing the harness rather than the server.
    """
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: srv.run(
                    server_read, server_write, srv.create_initialization_options()
                )
            )
            async with ClientSession(client_read, client_write) as session:
                await session.initialize()
                yield session
            tg.cancel_scope.cancel()


# A bare `async def test_*` would be COLLECTED and reported PASSED without ever
# running — the dev extra has no pytest-asyncio to await it. So every test body
# is a sync function that drives its coroutine to completion through asyncio.run,
# and patches `server.search_hikes` BEFORE the run (the server task resolves the
# module global at call time, so set-then-run is sufficient).


SAMPLE_HIKES = [
    Hike(osm_id=1, name="Alpha loop", distance_km=8.3, circular=True,
         car_access=True, chairlift_access=True, start=(50.7, 15.6),
         gain_m=540, loss_m=535, lift_type="chair_lift", ref="A1"),
    Hike(osm_id=2, name="Beta traverse", distance_km=12.0, circular=False,
         car_access=False, chairlift_access=False, start=(50.8, 15.7),
         gain_m=None, loss_m=None, lift_type=None, ref=None),
]


def test_list_tools_advertises_find_hikes(monkeypatch):
    monkeypatch.setattr(server, "search_hikes", lambda *a, **k: [])

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.list_tools()

    result = asyncio.run(_impl())
    tools = {t.name: t for t in result.tools}
    assert set(tools) == {
        "find_hikes", "circular_routes", "routes_between", "route_via", "routes_to_poi",
        "list_pois", "list_ferrata", "download_area", "list_areas",
    }

    schema = tools["find_hikes"].input_schema
    assert schema["type"] == "object"
    # No field is unconditionally required now: a live search needs the four corners,
    # an offline search needs `area` instead — validated in call_tool, not the schema.
    assert schema["required"] == []
    # the corners and the tri-state filters are still advertised
    for key in ("south", "west", "north", "east"):
        assert schema["properties"][key]["type"] == "number"
    for key in ("circular", "car_access", "chairlift_access", "near_misses", "compose_loops"):
        assert schema["properties"][key]["type"] == "boolean"
    assert schema["properties"]["area"]["type"] == "string"
    # the export format selector
    assert schema["properties"]["format"]["type"] == "string"
    assert set(schema["properties"]["format"]["enum"]) == {"text", "gpx", "geojson"}

    # download_area requires the corners plus a destination path.
    dl = tools["download_area"].input_schema
    assert dl["required"] == ["south", "west", "north", "east", "path"]


def test_call_tool_maps_arguments_and_renders(monkeypatch):
    captured = {}

    def _stub(bbox, criteria, cfg=None, **kwargs):
        captured["bbox"] = bbox
        captured["criteria"] = criteria
        return SAMPLE_HIKES

    monkeypatch.setattr(server, "search_hikes", _stub)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes",
                {
                    "south": 50.72, "west": 15.58, "north": 50.74, "east": 15.62,
                    "min_gain_m": 100, "max_gain_m": 800,
                    "min_distance_km": 5, "max_distance_km": 20,
                    "circular": True, "car_access": False,
                    # chairlift_access omitted on purpose -> must map to None
                },
            )

    result = asyncio.run(_impl())
    assert not result.is_error

    # bbox is forwarded as (south, west, north, east) IN THAT ORDER
    assert captured["bbox"] == (50.72, 15.58, 50.74, 15.62)

    crit = captured["criteria"]
    assert isinstance(crit, Criteria)
    assert crit.min_gain_m == 100 and crit.max_gain_m == 800
    assert crit.min_distance_km == 5 and crit.max_distance_km == 20
    # the crown jewel, same as test_cli: tri-state booleans
    assert crit.circular is True            # present  -> require
    assert crit.car_access is False         # false    -> exclude
    assert crit.chairlift_access is None    # omitted  -> don't care

    # rendered through the SAME formatter the CLI prints and the web serialises
    assert len(result.content) == 1
    assert result.content[0].text == "\n".join(format_hike(h) for h in SAMPLE_HIKES)


def test_call_tool_area_searches_snapshot_offline(monkeypatch):
    captured = {}

    def _fail_live(*a, **k):  # the live path must NOT run when `area` is given
        raise AssertionError("search_hikes should not be called in offline mode")

    monkeypatch.setattr(server, "search_hikes", _fail_live)
    monkeypatch.setattr(server, "load_snapshot", lambda path: f"SNAP:{path}")

    def _stub_snapshot(snap, criteria, cfg=None, *, near_miss=False, name_places=None):
        captured["snap"] = snap
        captured["near_miss"] = near_miss
        captured["name_places"] = name_places
        captured["circular"] = criteria.circular
        return SAMPLE_HIKES

    monkeypatch.setattr(server, "search_snapshot", _stub_snapshot)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes", {"area": "krkonose.json", "circular": True}
            )

    result = asyncio.run(_impl())
    assert not result.is_error
    assert captured["snap"] == "SNAP:krkonose.json"
    assert captured["near_miss"] == "auto"      # near_misses omitted -> auto
    assert captured["circular"] is True
    assert result.content[0].text == "\n".join(format_hike(h) for h in SAMPLE_HIKES)


def test_call_tool_near_misses_flag_forwarded(monkeypatch):
    captured = {}

    def _stub(bbox, criteria, cfg=None, *, near_miss=False, **kwargs):
        captured["near_miss"] = near_miss
        return []

    monkeypatch.setattr(server, "search_hikes", _stub)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes",
                {"south": 1, "west": 2, "north": 3, "east": 4, "near_misses": True},
            )

    asyncio.run(_impl())
    assert captured["near_miss"] is True


def test_call_tool_compose_loops_routes_to_compose_engine(monkeypatch):
    # compose_loops=true must call the composition engine (NOT search_hikes) and render
    # the composed loop with its provenance, no relation id.
    captured = {}

    def _fail_live(*a, **k):
        raise AssertionError("search_hikes must not run when compose_loops is set")

    def _stub_compose(bbox, criteria, cfg=None, *, near_miss=False, **kwargs):
        captured["bbox"] = bbox
        return [
            Hike(osm_id=-1, name="Composed loop", distance_km=9.0, circular=True,
                 car_access=True, chairlift_access=False, start=(50.7, 15.6),
                 gain_m=300, loss_m=300, lift_type=None, ref=None,
                 composed=True, composed_of=("0402", "1801")),
        ]

    monkeypatch.setattr(server, "search_hikes", _fail_live)
    monkeypatch.setattr(server, "compose_loops", _stub_compose)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes",
                {"south": 50.72, "west": 15.58, "north": 50.74, "east": 15.62,
                 "compose_loops": True},
            )

    result = asyncio.run(_impl())
    assert not result.is_error
    assert captured["bbox"] == (50.72, 15.58, 50.74, 15.62)
    text = result.content[0].text
    assert "composed of 0402 + 1801" in text
    assert "OSM relation" not in text


def test_circular_routes_tool_maps_point_and_renders(monkeypatch):
    # The circular_routes tool forwards the point + radius to compose_loops_around and
    # renders its composed loops through the shared formatter.
    captured = {}

    def _stub(point, criteria, cfg=None, *, radius_m=None, near_miss=False, **kwargs):
        captured["point"] = point
        captured["radius_m"] = radius_m
        captured["criteria"] = criteria
        return [
            Hike(osm_id=-1, name="Composed loop", distance_km=9.0, circular=True,
                 car_access=True, chairlift_access=False, start=(50.73, 15.60),
                 gain_m=300, loss_m=300, composed=True, composed_of=("0402", "1801")),
        ]

    monkeypatch.setattr(server, "compose_loops_around", _stub)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "circular_routes",
                {"lat": 50.73, "lon": 15.60, "radius_m": 500,
                 "min_distance_km": 5, "max_distance_km": 12, "car_access": True},
            )

    result = asyncio.run(_impl())
    assert not result.is_error
    assert captured["point"] == (50.73, 15.60)
    assert captured["radius_m"] == 500
    assert captured["criteria"].car_access is True
    assert captured["criteria"].max_distance_km == 12
    assert "composed of 0402 + 1801" in result.content[0].text


def test_routes_between_tool_maps_two_points_and_k(monkeypatch):
    # The routes_between tool forwards start/finish + k to search.routes_between and renders
    # the shortest-first routes.
    captured = {}

    def _stub(start, finish, criteria, cfg=None, *, k=None, **kwargs):
        captured["start"] = start
        captured["finish"] = finish
        captured["k"] = k
        return [
            Hike(osm_id=-1, name="Route", distance_km=4.0, circular=False,
                 car_access=False, chairlift_access=False, start=start,
                 gain_m=120, loss_m=90, composed=True, composed_of=("0402",)),
            Hike(osm_id=-2, name="Route", distance_km=6.5, circular=False,
                 car_access=False, chairlift_access=False, start=start,
                 gain_m=200, loss_m=150, composed=True, composed_of=("1801",)),
        ]

    monkeypatch.setattr(server, "routes_between", _stub)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "routes_between",
                {"start_lat": 50.72, "start_lon": 15.58,
                 "finish_lat": 50.74, "finish_lon": 15.62, "routes": 2},
            )

    result = asyncio.run(_impl())
    assert not result.is_error
    assert captured["start"] == (50.72, 15.58)
    assert captured["finish"] == (50.74, 15.62)
    assert captured["k"] == 2
    text = result.content[0].text
    assert "4.0 km" in text and "6.5 km" in text        # both routes rendered
    assert "loop" not in text                           # point-to-point, not loops


def test_call_tool_format_gpx_returns_a_gpx_document(monkeypatch):
    # format=gpx serialises the matched routes as GPX (not the one-line summaries).
    monkeypatch.setattr(
        server, "search_hikes",
        lambda *a, **k: [
            Hike(osm_id=1, name="Alpha loop", distance_km=8.3, circular=True,
                 car_access=True, chairlift_access=False, start=(50.7, 15.6),
                 gain_m=540, loss_m=535, ways=(((50.7, 15.6), (50.71, 15.61)),)),
        ],
    )

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes",
                {"south": 1, "west": 2, "north": 3, "east": 4, "format": "gpx"},
            )

    result = asyncio.run(_impl())
    assert not result.is_error
    text = result.content[0].text
    import xml.etree.ElementTree as ET

    assert ET.fromstring(text).tag.endswith("gpx")
    assert "Alpha loop" in text


def test_call_tool_format_geojson_returns_a_feature_collection(monkeypatch):
    monkeypatch.setattr(
        server, "search_hikes",
        lambda *a, **k: [
            Hike(osm_id=1, name="Alpha loop", distance_km=8.3, circular=True,
                 car_access=True, chairlift_access=False, start=(50.7, 15.6),
                 gain_m=540, loss_m=535, ways=(((50.7, 15.6), (50.71, 15.61)),)),
        ],
    )

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes",
                {"south": 1, "west": 2, "north": 3, "east": 4, "format": "geojson"},
            )

    result = asyncio.run(_impl())
    assert not result.is_error
    obj = json.loads(result.content[0].text)
    assert obj["type"] == "FeatureCollection" and len(obj["features"]) == 1


def test_call_tool_empty_result_is_friendly(monkeypatch):
    monkeypatch.setattr(server, "search_hikes", lambda *a, **k: [])

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes", {"south": 1, "west": 2, "north": 3, "east": 4}
            )

    result = asyncio.run(_impl())
    assert not result.is_error
    assert result.content[0].text == "No matching hikes found in that area."


def test_call_tool_compose_empty_message_is_compose_specific(monkeypatch):
    monkeypatch.setattr(server, "compose_loops", lambda *a, **k: [])

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes",
                {"south": 1, "west": 2, "north": 3, "east": 4, "compose_loops": True},
            )

    result = asyncio.run(_impl())
    assert not result.is_error
    assert "compose" in result.content[0].text.lower()


def test_unknown_tool_is_an_error(monkeypatch):
    """A tool name we don't serve is a PROTOCOL error, so it arrives as a raised
    exception rather than as tool output — the opposite channel from a bad *argument*
    (see test_unknown_poi_kind_is_an_error_not_an_empty_result, which stays readable
    content on purpose). Under mcp 1.x the framework caught the raise and both looked
    the same from here; 2.x lets them differ, and they should."""
    monkeypatch.setattr(server, "search_hikes", lambda *a, **k: [])

    async def _impl():
        # Caught HERE, inside the session, on purpose: letting it escape the harness's
        # task group wraps it in nested ExceptionGroups whose str() is "unhandled errors
        # in a TaskGroup" — which would assert on anyio's plumbing, not on the server.
        async with create_connected_server_and_client_session(server.app) as session:
            try:
                await session.call_tool("does_not_exist", {})
            except Exception as exc:  # MCPError
                return exc
            return None

    err = asyncio.run(_impl())
    assert err is not None, "an unknown tool must raise, not return a result"
    assert "unknown tool" in str(err).lower()


# --- the call reaches the REAL engine, against live fixture data --------------

FIXTURE = Path(__file__).parent / "fixtures" / "spindl_area.json"


class _FlatElevation:
    """Offline elevation provider: flat ground, so gain/loss are a deterministic
    0. Keeps the engine fully offline while still exercising the real geometry,
    access, and formatting path end-to-end behind the MCP boundary."""

    def lookup(self, points):
        return [0.0] * len(points)


def test_call_tool_runs_the_real_engine_on_fixture(monkeypatch):
    from hike_finder import search as search_mod
    from hike_finder.overpass import parse_area

    area = parse_area(json.loads(FIXTURE.read_text(encoding="utf-8"))["elements"])

    # Stub ONLY the two network boundaries; the engine (filters, geometry,
    # access, format) runs for real, through the server's own call_tool -> CFG.
    monkeypatch.setattr(search_mod, "fetch_area", lambda *a, **k: area)
    monkeypatch.setattr(search_mod, "get_provider", lambda *a, **k: _FlatElevation())

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes",
                {"south": 50.72, "west": 15.58, "north": 50.74, "east": 15.62},
            )

    result = asyncio.run(_impl())
    assert not result.is_error

    lines = result.content[0].text.splitlines()
    assert len(lines) >= 5                                  # 11 survive on this bbox
    assert all("OSM relation" in ln for ln in lines)        # all real formatted hikes
    assert any("OSM relation 6282999" in ln for ln in lines)  # the known Spindl loop


# --- the REAL stdio transport: spawn the server as a subprocess ---------------

def test_real_stdio_transport_lists_the_tool():
    """Pin what the in-memory session can't: the actual stdio wiring + ``main()``.

    Spawns the real ``python -m hike_finder.server`` and speaks MCP over its OS
    stdin/stdout pipes. ``initialize`` + ``list_tools`` touch NO network (the
    handler returns the static tool list), so this stays hermetic — we never
    call ``find_hikes``, which would hit Overpass. PYTHONPATH points at ``src``
    so the child finds the package whether or not it's pip-installed, and we
    extend ``get_default_environment()`` (not replace it) so Windows keeps
    SystemRoot/PATH and Python can start at all.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "hike_finder.server"],
        env={**get_default_environment(), "PYTHONPATH": _SRC},
    )

    async def _impl():
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=30
            ) as session:
                await session.initialize()
                return await session.list_tools()

    result = asyncio.run(asyncio.wait_for(_impl(), timeout=60))
    assert {t.name for t in result.tools} == {
        "find_hikes", "circular_routes", "routes_between", "route_via", "routes_to_poi",
        "list_pois", "list_ferrata", "download_area", "list_areas",
    }


# --------------------------------------------------- points of interest + area listing


def test_every_search_tool_offers_the_same_poi_filter():
    """"Does it go past a ruin?" is the same question in every mode, so they all
    advertise the same two parameters, generated from the ONE registry.

    ``routes_to_poi`` is in the list too: its `kinds` say where to walk TO, and `poi`
    still filters what the drawn route must pass — two different questions that stay
    separately askable.
    """
    from hike_finder.poi import POI_KINDS

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.list_tools()

    tools = {t.name: t for t in asyncio.run(_impl()).tools}
    for name in (
        "find_hikes", "circular_routes", "routes_between", "route_via", "routes_to_poi",
    ):
        props = tools[name].input_schema["properties"]
        assert props["poi"]["type"] == "array"
        # The enum comes from the registry, so the schema can never offer a kind the
        # engine would reject (nor omit one it accepts).
        assert set(props["poi"]["items"]["enum"]) == set(POI_KINDS)
        assert props["poi_radius_m"]["type"] == "number"
    # list_areas takes nothing — it is an inventory, not a search.
    assert tools["list_areas"].input_schema["required"] == []


def test_poi_arguments_reach_the_engine(monkeypatch):
    captured = {}

    def _stub(bbox, criteria, cfg=None, **kwargs):
        captured["criteria"] = criteria
        captured["cfg"] = cfg
        return SAMPLE_HIKES

    monkeypatch.setattr(server, "search_hikes", _stub)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes",
                {"south": 50.7, "west": 15.5, "north": 50.8, "east": 15.7,
                 "poi": ["ruins", "church"], "poi_radius_m": 600},
            )

    assert not asyncio.run(_impl()).is_error
    assert captured["criteria"].poi_kinds == ("ruins", "church")
    assert captured["cfg"].poi_radius_m == 600
    # The per-call override must NOT leak into the shared module-level config.
    assert server.CFG.poi_radius_m != 600


def test_unknown_poi_kind_is_an_error_not_an_empty_result(monkeypatch):
    """An LLM client would read an empty list as "there are no ruins here"."""
    monkeypatch.setattr(server, "search_hikes", lambda *a, **k: SAMPLE_HIKES)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes",
                {"south": 50.7, "west": 15.5, "north": 50.8, "east": 15.7,
                 "poi": ["cathedral"]},
            )

    result = asyncio.run(_impl())
    assert result.is_error
    assert "cathedral" in result.content[0].text


def test_list_areas_reports_the_offline_inventory(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKE_SNAPSHOT_DIR", str(tmp_path))

    async def _call():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool("list_areas", {})

    # Empty: a sentence that says what to do next, not an empty array.
    empty = asyncio.run(_call())
    assert not empty.is_error and "No named areas downloaded" in empty.content[0].text

    from hike_finder.overpass import AreaData
    from hike_finder.snapshot import AreaSnapshot, save_snapshot

    save_snapshot(
        AreaSnapshot(
            bbox=(49.9, 13.9, 50.2, 14.2),
            area=AreaData(
                routes=[{"id": 1, "name": "N", "ways": [[(50.0, 14.0), (50.05, 14.0)]],
                         "tags": {}}],
                pois=[{"coord": (50.02, 14.001), "kind": "ruins", "name": "Hrad"}],
            ),
            elevations={},
            sample_interval_m=25.0,
        ),
        tmp_path / "krkonose.json",
    )
    listed = json.loads(asyncio.run(_call()).content[0].text)
    assert [a["name"] for a in listed] == ["krkonose"]
    assert listed[0]["bbox"] == [49.9, 13.9, 50.2, 14.2]
    assert listed[0]["routes"] == 1 and listed[0]["pois"] == 1


# --------------------------------------------- routes_to_poi (route TO an object)


def test_routes_to_poi_reaches_the_engine(monkeypatch):
    """The tool hands the start, the destination kinds and both knobs to the engine —
    and keeps `kinds` (where to walk to) apart from `poi` (what the route must pass)."""
    from hike_finder.poi import PoiHit

    captured = {}

    def _stub(start, kinds, criteria, cfg=None, *, n=None, search_radius_m=None, **kw):
        captured.update(start=start, kinds=kinds, n=n, radius=search_radius_m,
                        poi_filter=criteria.poi_kinds)
        return [
            replace(
                SAMPLE_HIKES[0], name="Route to ruin “Rotštejn”",
                destination=PoiHit(kind="ruins", name="Rotštejn", coord=(50.75, 15.61),
                                   distance_m=85.0),
            )
        ]

    monkeypatch.setattr(server, "routes_to_poi", _stub)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "routes_to_poi",
                {"lat": 50.73, "lon": 15.60, "kinds": ["ruins"], "routes": 2,
                 "search_radius_m": 4500, "poi": ["refreshment"]},
            )

    result = asyncio.run(_impl())
    assert not result.is_error
    assert captured["start"] == (50.73, 15.60)
    assert captured["kinds"] == ("ruins",) and captured["poi_filter"] == ("refreshment",)
    assert captured["n"] == 2 and captured["radius"] == 4500
    # The rendered line says what it was drawn to AND that it stops short of it.
    text = result.content[0].text
    assert "Route to ruin “Rotštejn”" in text and "ends 85 m from the ruin" in text


def test_routes_to_poi_without_kinds_asks_for_them(monkeypatch):
    """No destination kind is refused, never answered with an unasked-for search.

    The schema marks `kinds` required, but under mcp 2.x that is advice to the CLIENT —
    the server does not validate arguments against `inputSchema` — so the call lands in
    the handler and the handler's own guard is the only thing standing between a missing
    `kinds` and an unasked-for search. It raises, which `call_tool` turns into a result
    the client can read, with `is_error` set. (Under 1.x the framework rejected this
    before the handler saw it; when that stopped being true, this test is what noticed.)
    """
    def _fail(*a, **k):
        raise AssertionError("the engine must not run without a destination kind")

    monkeypatch.setattr(server, "routes_to_poi", _fail)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool("routes_to_poi", {"lat": 50.73, "lon": 15.60})

    result = asyncio.run(_impl())
    assert result.is_error and "kinds" in result.content[0].text

    # An EMPTY kinds list is the same refusal, straight at the handler.
    with pytest.raises(ValueError, match="what to walk to"):
        asyncio.run(
            server._call_routes_to_poi({"lat": 50.73, "lon": 15.60, "kinds": []})
        )


def test_routes_to_poi_empty_result_is_destination_shaped(monkeypatch):
    """The three causes are named, and the wording never reads as a filtered area search."""
    monkeypatch.setattr(server, "routes_to_poi", lambda *a, **k: [])

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "routes_to_poi", {"lat": 50.73, "lon": 15.60, "kinds": ["ruins"]}
            )

    text = asyncio.run(_impl()).content[0].text
    assert "No route could be drawn" in text
    assert "search_radius_m" in text and "off the trail network" in text
    assert "max_distance_km" in text
    assert "MAPPED" in text          # a miss is about the map, not the world
    assert "in the selected area" not in text


# ------------------------------------------- list_pois (browse the objects, no routing)


def _call(tool, args):
    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(tool, args)

    return asyncio.run(_impl())


_BROWSE_POIS = [
    {"coord": (50.7301, 15.6001), "kind": "ruins", "name": "Nistejka"},
    {"coord": (50.7211, 15.5902), "kind": "church", "name": "Sv. Petr"},
]


_CURRENT_REGISTRY = object()  # "stamp whatever this build knows" — NOT `None`, which is
                              # itself a meaningful value here (a file that records nothing)


def _browse_snapshot(path, pois, *, poi_kinds=_CURRENT_REGISTRY):
    from hike_finder.overpass import AreaData
    from hike_finder.poi import all_kinds
    from hike_finder.snapshot import AreaSnapshot, save_snapshot

    save_snapshot(
        AreaSnapshot(
            bbox=(50.72, 15.58, 50.78, 15.68),
            area=AreaData(
                pois=list(pois),
                # A real download stamps the registry it classified against (parse_area
                # does it). Default to the current one so a fixture stands for a
                # freshly-downloaded area; pass a SHORTER tuple to stand for one saved
                # by an older build, and `None` for one saved before the field existed.
                poi_kinds=(
                    all_kinds()
                    if poi_kinds is _CURRENT_REGISTRY
                    else (None if poi_kinds is None else tuple(poi_kinds))
                ),
            ),
            elevations={},
            sample_interval_m=25.0,
        ),
        path,
    )


def test_list_pois_advertises_the_browse():
    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.list_tools()

    tool = {t.name: t for t in asyncio.run(_impl()).tools}["list_pois"]
    schema = tool.input_schema
    # Nothing is required: a live listing needs the corners, an offline one needs `area`
    # — validated in call_tool, like find_hikes.
    assert schema["required"] == []
    assert set(schema["properties"]) >= {"south", "west", "north", "east", "area", "kinds", "format"}
    assert "ruins" in schema["properties"]["kinds"]["items"]["enum"]
    # The description has to keep the three POI questions apart, or an LLM client will
    # reach for the wrong one.
    assert "WITHOUT drawing any route" in tool.description


def test_list_pois_reads_a_snapshot_offline(tmp_path):
    path = tmp_path / "browse.json"
    _browse_snapshot(path, _BROWSE_POIS)
    result = _call("list_pois", {"area": str(path)})
    assert not result.is_error
    text = result.content[0].text
    assert text.startswith("2 objects: 1 place of worship, 1 ruin")
    assert "Sv. Petr" in text and "Nistejka" in text


def test_list_pois_formats(tmp_path):
    path = tmp_path / "browse.json"
    _browse_snapshot(path, _BROWSE_POIS)
    gpx = _call("list_pois", {"area": str(path), "kinds": ["ruins"], "format": "gpx"})
    assert "<wpt" in gpx.content[0].text and "<trk>" not in gpx.content[0].text
    geo = _call("list_pois", {"area": str(path), "format": "geojson"})
    assert json.loads(geo.content[0].text)["features"][0]["geometry"]["type"] == "Point"
    js = _call("list_pois", {"area": str(path), "format": "json"})
    assert [d["kind"] for d in json.loads(js.content[0].text)] == ["church", "ruins"]


def test_list_pois_live_forwards_bbox_and_kinds(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        server, "list_area_pois",
        lambda bbox, kinds, cfg=None, **kw: captured.update(bbox=bbox, kinds=kinds) or (),
    )
    _call("list_pois", {
        "south": 50.72, "west": 15.58, "north": 50.78, "east": 15.68, "kinds": ["ruins"],
    })
    assert captured["bbox"] == (50.72, 15.58, 50.78, 15.68)
    assert captured["kinds"] == ("ruins",)


def test_list_pois_needs_a_box_or_an_area():
    result = _call("list_pois", {"kinds": ["ruins"]})
    assert "provide south/west/north/east" in result.content[0].text


def test_list_pois_separates_empty_from_cannot_know(tmp_path):
    """Two empty results, two different answers — the distinction an LLM must not lose."""
    current = tmp_path / "current.json"
    _browse_snapshot(current, _BROWSE_POIS)
    nothing = _call("list_pois", {"area": str(current), "kinds": ["cave"]})
    assert "Nothing of that kind (cave) is mapped" in nothing.content[0].text
    assert "not that nothing is there" in nothing.content[0].text

    old = tmp_path / "old.json"
    # Neither objects nor a kind record — the file predates points of interest outright.
    _browse_snapshot(old, [], poi_kinds=None)
    stale = _call("list_pois", {"area": str(old)})
    assert "saved before the feature existed" in stale.content[0].text


def test_list_pois_tells_an_llm_a_kind_was_never_looked_for(tmp_path):
    """The case an LLM client will otherwise report as fact.

    A file saved by an older build holds plenty of objects and never looked for a tree.
    Both shapes matter: the empty answer must not read as "there are none there", and the
    NON-empty one (ask for ruins and trees, get ruins) must not read as the whole answer.
    """
    from hike_finder.poi import all_kinds

    path = tmp_path / "older.json"
    _browse_snapshot(path, _BROWSE_POIS, poi_kinds=[k for k in all_kinds() if k != "tree"])

    empty = _call("list_pois", {"area": str(path), "kinds": ["tree"]})
    assert not empty.is_error
    assert "predates the kind tree" in empty.content[0].text
    assert "nobody looked" in empty.content[0].text
    assert "Nothing of that kind" not in empty.content[0].text

    partial = _call("list_pois", {"area": str(path), "kinds": ["ruins", "tree"]})
    assert "predates the kind tree" in partial.content[0].text
    assert "1 ruin" in partial.content[0].text   # the objects it does carry, still listed


def test_list_pois_accepts_a_bare_area_name(tmp_path, monkeypatch):
    """An LLM reads a `name` out of list_areas; it must work here verbatim.

    Without the named-directory fallback this raised a bare FileNotFoundError from deep
    inside load_snapshot — an error about a path the caller never typed.
    """
    monkeypatch.setenv("HIKE_SNAPSHOT_DIR", str(tmp_path))
    from hike_finder.snapshot import snapshot_path

    _browse_snapshot(snapshot_path("ceskyraj"), _BROWSE_POIS)
    result = _call("list_pois", {"area": "ceskyraj"})
    assert not result.is_error
    assert "Sv. Petr" in result.content[0].text


def test_list_pois_unreadable_area_is_a_message_not_a_traceback(tmp_path):
    result = _call("list_pois", {"area": str(tmp_path / "nope.json")})
    assert "Could not read the area" in result.content[0].text
    assert "shown by list_areas" in result.content[0].text


# ------------------------------------------ find_hikes carries the ferrata gap in its text
#
# The channel question, which is per-frontend even though the sentences are shared: the
# CLI's copy goes to stderr (search_snapshot logs it) and the web UI's rides in the
# /api/hikes envelope, while an MCP client only ever sees the reply text. Until this
# landed, `find_hikes(area=…, ferrata=false)` over a file that cannot read cable answered
# "No matching hikes found in that area." to a reader that paraphrases confidently.
#
# The fixture shapes mirror test_web.py's `server` fixture on purpose — an unreadable
# file, a tagged-but-unrecorded one, a current one, and one with no routes at all — so
# the two frontends can be read side by side and cannot drift apart on the same file.


class _Ramp:
    def lookup(self, points):
        return [(lat - 50.0) * 20000.0 for lat, _ in points]


def _ferrata_snapshot(path, *, way_tags=None, ferrata=False, routes=None):
    """A real saved area, written to disk, differing only in what it can say about cable.

    ``way_tags`` (parallel to ``ways``, index for index) is what AVOIDANCE is measured
    from; the two ferrata lists are what FINDING needs. Both default to the older file —
    neither present — because that is the case the caveat exists for. ``routes=[]`` builds
    a stretch of map OSM collects no hiking relations in.

    Deliberately a real `load_snapshot` source rather than a stub: the caveat is a claim
    about a FILE, and every fixture in the ferrata suite that skipped `save_snapshot` also
    skipped the round-trip that decides whether the record survived it.
    """
    from hike_finder.filters import find_hikes
    from hike_finder.overpass import AreaData
    from hike_finder.snapshot import AreaSnapshot, RecordingElevationProvider, save_snapshot

    route = {"id": 7, "name": "Cable ridge", "ways": [[(50.0, 14.0), (50.05, 14.0)]], "tags": {}}
    if way_tags is not None:
        route["way_tags"] = [dict(t) for t in way_tags]
    area = AreaData(
        routes=[route] if routes is None else list(routes),
        pois=[],
    )
    if ferrata:
        # What a live parse always sets: empty lists are a real answer ("looked, found
        # none"), which is the one thing `None` cannot say.
        area.ferrata_routes, area.ferrata_ways = [], []
    rec = RecordingElevationProvider(_Ramp())
    bbox = (49.9, 13.9, 50.2, 14.2)
    find_hikes(area, rec, Criteria(), bbox=bbox)
    save_snapshot(
        AreaSnapshot(bbox=bbox, area=area, elevations=rec.samples, sample_interval_m=25.0),
        path,
    )
    return str(path)


def test_find_hikes_says_a_file_cannot_read_cable_instead_of_answering_from_nothing(tmp_path):
    """The gap this whole section exists for, in the direction that reads as a safety claim.

    `webtest`'s counterpart here carries no member-way tags, so no route can be examined
    for cable and `ferrata=false` drops every one of them. The empty list alone paraphrases
    into "there are no cabled routes there" — a claim about the terrain, made from a file
    that holds no evidence either way.
    """
    path = _ferrata_snapshot(tmp_path / "unreadable.json")
    result = _call("find_hikes", {"area": path, "ferrata": False})
    assert not result.is_error
    text = result.content[0].text
    # The caveat leads, matching list_pois: the reader must meet it before the result.
    assert text.startswith("ferrata:")
    assert "carry no member-way tags" in text
    assert "NOT a report that the routes are free of cable" in text
    assert "Do NOT turn this into a statement about cable on the ground" in text
    # The WRONG sentence, and the reason ferrata_gap_message picks between the two in the
    # order it does: this file cannot honour a promise that avoidance still works on it.
    assert "AVOIDING them still works" not in text
    # The empty-result sentence is still there — the caveat explains it, it does not
    # replace it (that is `no_routes`' job, and only `no_routes`').
    assert "No matching hikes found in that area." in text

    # And it goes quiet when nobody asked about cable. A caveat that never switches off is
    # noise; the same file with no ferrata flag says nothing about ferrata at all.
    quiet = _call("find_hikes", {"area": path})
    assert "ferrata" not in quiet.content[0].text


def test_find_hikes_gives_the_two_ferrata_flags_different_answers_from_one_file(tmp_path):
    """A file with member-way tags that never fetched ferrata objects — the asymmetry.

    Avoiding cable needs only the member tags it has; finding it needs the objects it
    never downloaded. One file, two questions, and only one of them short.
    """
    path = _ferrata_snapshot(tmp_path / "tagged.json", way_tags=[{"highway": "path"}])

    finding = _call("find_hikes", {"area": path, "ferrata": True})
    text = finding.content[0].text
    assert "predates cabled-route fetching" in text
    # Here the closing promise IS true, which is what earns this file the other sentence.
    assert "AVOIDING them still works" in text
    assert "Do NOT turn this into a statement about cable on the ground" in text

    avoiding = _call("find_hikes", {"area": path, "ferrata": False})
    assert "ferrata" not in avoiding.content[0].text.lower()


def test_find_hikes_says_nothing_about_cable_when_the_file_can_answer(tmp_path):
    """The complement, and the half that makes the rest a signal rather than a banner."""
    path = _ferrata_snapshot(
        tmp_path / "current.json", way_tags=[{"highway": "path"}], ferrata=True
    )
    result = _call("find_hikes", {"area": path, "ferrata": False})
    text = result.content[0].text
    assert "Cable ridge" in text        # an ordinary path survives avoidance
    assert "ferrata" not in text.lower()


def test_the_ferrata_caveat_is_not_gated_on_an_empty_result(tmp_path):
    """The trap HANDOFF booked with this task, pinned.

    Hiding the caveat behind an empty list looks like noise suppression and is not. The
    concrete case, and the reason this fixture carries a CABLED member way: asked to FIND
    cable, a file that never fetched ferrata objects still returns the hiking routes whose
    own members are tagged as cabled — a real, non-empty answer — while the dedicated
    `route=via_ferrata` relations it never downloaded stay missing from it. A non-empty
    result is exactly when a short list is hardest to notice, so the sentence has to ride
    on it. Nothing is stubbed here; the real engine produces the pair.
    """
    path = _ferrata_snapshot(tmp_path / "cabled.json", way_tags=[{"highway": "via_ferrata"}])
    text = _call("find_hikes", {"area": path, "ferrata": True}).content[0].text
    assert "predates cabled-route fetching" in text
    # Both, in that order: the caveat, then the route it qualifies.
    assert text.startswith("ferrata:")
    assert "Cable ridge" in text.split("\n")[-1]


def test_the_ferrata_caveat_never_reaches_a_gpx_or_geojson_document(tmp_path):
    """Prose in front of a GPX file is not a caveat, it is invalid XML.

    The export formats are documents with nowhere to put a sentence — the same call the
    web export path makes, and the search that produced the file already showed it. Note
    this needs the CABLED fixture too: the empty-result branch runs before `format` is
    read, so a query that matches nothing never reaches these returns at all.
    """
    path = _ferrata_snapshot(tmp_path / "cabled.json", way_tags=[{"highway": "via_ferrata"}])
    gpx = _call("find_hikes", {"area": path, "ferrata": True, "format": "gpx"})
    geo = _call("find_hikes", {"area": path, "ferrata": True, "format": "geojson"})
    assert gpx.content[0].text.startswith("<?xml")
    # Not "no mention of ferrata": the route's OWN flag belongs in the track description
    # (`ferrata 5.6 km`, which the live run over stdio shows there). What must be absent is
    # the CAVEAT — prose about the file, in a document that has nowhere to put it.
    assert "predates cabled-route fetching" not in gpx.content[0].text
    assert "Do NOT turn this into a statement" not in gpx.content[0].text
    # Parses as JSON, i.e. nothing was prepended to it either.
    assert json.loads(geo.content[0].text)["type"] == "FeatureCollection"
    assert "predates cabled-route fetching" not in geo.content[0].text


def test_a_live_ferrata_search_is_never_caveated(monkeypatch):
    """The seam that can provably never fire, pinned so nobody helpfully wires it.

    A live fetch always parses both ferrata lists and the member-way tags, and the ferrata
    clause changed the query TEXT — the Overpass cache key — so a pre-feature response
    cannot be served under it either. `ferrata_gap_message` is None on this path by
    construction; computing it here would read as a case that might happen.
    """
    monkeypatch.setattr(server, "search_hikes", lambda *a, **k: SAMPLE_HIKES)
    result = _call("find_hikes", {
        "south": 50.72, "west": 15.58, "north": 50.78, "east": 15.68, "ferrata": False,
    })
    assert result.content[0].text == "\n".join(format_hike(h) for h in SAMPLE_HIKES)


def test_no_routes_and_the_ferrata_gap_are_both_said_when_both_are_true(tmp_path):
    """Two different facts about one file, and neither one substitutes for the other.

    An area OSM maps no hiking relations in, asked to FIND cable, is *also* a file that
    never fetched ferrata objects. Re-downloading it fixes the second and cannot fix the
    first, so an LLM told only one of them would send its user to do the wrong thing. The
    web UI's `_area_notices` emits exactly this pair.
    """
    path = _ferrata_snapshot(tmp_path / "noroutes.json", routes=[])
    both = _call("find_hikes", {"area": path, "ferrata": True}).content[0].text
    assert "predates cabled-route fetching" in both
    assert "No hiking route relations are mapped in that area" in both
    assert "No matching hikes found in that area." not in both   # no_routes outranks it
    # Reading this reply is what turned up a wording bug the CLI and the web UI had been
    # hiding by splitting the two sentences across streams and boxes: the unrecorded
    # message used to end by promising that avoidance still works "from the member ways it
    # already has", on a file that has no member ways at all. Fixed in
    # search.ferrata_unrecorded_message; pinned at the source in test_ferrata_search.py.
    assert "still works on this file" not in both

    # The complement falls out rather than being arranged: asked to AVOID cable, the same
    # file has nothing to disclaim — there are no routes to be unable to read — and
    # `no_routes` stands alone. Telling someone to re-download would be advice against
    # a problem the download cannot solve.
    alone = _call("find_hikes", {"area": path, "ferrata": False}).content[0].text
    assert alone == server.no_routes_message()
    assert "ferrata" not in alone.lower()
