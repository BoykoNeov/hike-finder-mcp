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
        "list_pois", "download_area", "list_areas",
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
        "list_pois", "download_area", "list_areas",
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
    assert text.startswith("2 objects: 1 church, 1 ruin")
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
