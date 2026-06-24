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
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("mcp")  # the MCP server is an optional extra; skip if absent
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from hike_finder import server
from hike_finder.filters import Criteria, Hike
from hike_finder.format import format_hike

_SRC = str(Path(__file__).resolve().parent.parent / "src")


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
    assert set(tools) == {"find_hikes", "download_area"}

    schema = tools["find_hikes"].inputSchema
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

    # download_area requires the corners plus a destination path.
    dl = tools["download_area"].inputSchema
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
    assert not result.isError

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

    def _stub_snapshot(snap, criteria, cfg=None, *, near_miss=False):
        captured["snap"] = snap
        captured["near_miss"] = near_miss
        captured["circular"] = criteria.circular
        return SAMPLE_HIKES

    monkeypatch.setattr(server, "search_snapshot", _stub_snapshot)

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes", {"area": "krkonose.json", "circular": True}
            )

    result = asyncio.run(_impl())
    assert not result.isError
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
    assert not result.isError
    assert captured["bbox"] == (50.72, 15.58, 50.74, 15.62)
    text = result.content[0].text
    assert "composed of 0402 + 1801" in text
    assert "OSM relation" not in text


def test_call_tool_empty_result_is_friendly(monkeypatch):
    monkeypatch.setattr(server, "search_hikes", lambda *a, **k: [])

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool(
                "find_hikes", {"south": 1, "west": 2, "north": 3, "east": 4}
            )

    result = asyncio.run(_impl())
    assert not result.isError
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
    assert not result.isError
    assert "compose" in result.content[0].text.lower()


def test_unknown_tool_is_an_error(monkeypatch):
    monkeypatch.setattr(server, "search_hikes", lambda *a, **k: [])

    async def _impl():
        async with create_connected_server_and_client_session(server.app) as session:
            return await session.call_tool("does_not_exist", {})

    result = asyncio.run(_impl())
    assert result.isError
    assert "unknown tool" in result.content[0].text.lower()


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
    assert not result.isError

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
                read, write, read_timeout_seconds=timedelta(seconds=30)
            ) as session:
                await session.initialize()
                return await session.list_tools()

    result = asyncio.run(asyncio.wait_for(_impl(), timeout=60))
    assert {t.name for t in result.tools} == {"find_hikes", "download_area"}
