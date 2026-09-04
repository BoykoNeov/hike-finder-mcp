"""One table, three surfaces: every filter has to be reachable from all of them.

This project's recurring bug is not a wrong answer, it is a filter that reaches the
CLI, the web UI and the MCP server on three different days. The changelog says so in
its own words — "MCP `find_hikes` was the last frontend that answered from nothing" —
and the same shape produced the ferrata caveat three times and the no-routes notice
four. What keeps the three honest today is that they all build one ``Criteria`` and
call one engine. What did not exist until this file is anything that checks the three
*surfaces* offer the same set of criteria.

So: one case per ``Criteria`` field, giving how that filter is spelled at each surface
and what value it should produce. The first test asserts the table covers every field,
which is what makes a NEW filter fail here on the day it is added rather than on the
day someone notices one frontend cannot ask for it.

The MCP half is checked twice on purpose, because the two halves break separately: the
handler reading the key (``server._criteria``) and the tool schema DECLARING it. A
filter missing from the schema is honoured by the engine and invisible to the client —
which is exactly the state four of the point-mode tools are in; see ``NOT_ADVERTISED``
and the test beneath it at the bottom of this file.
"""
from dataclasses import dataclass, fields
from urllib.parse import parse_qs

import pytest

from hike_finder import web
from hike_finder.cli import build_criteria as cli_criteria
from hike_finder.cli import build_parser
from hike_finder.filters import Criteria


@dataclass(frozen=True)
class Case:
    """How one ``Criteria`` field is asked for at each of the three surfaces."""

    cli: list[str]  # the flag, as typed at a shell prompt
    mcp: dict  # the JSON argument an MCP client sends
    mcp_property: str  # the name that argument must have in the tool's inputSchema
    web: str  # the query string the browser page sends
    value: object  # what all three must put in the Criteria field


# Values are deliberately distinguishable from each other and from the defaults, so a
# case that silently lands in the wrong field fails rather than passing by coincidence.
CASES: dict[str, Case] = {
    "min_gain_m": Case(
        cli=["--min-gain", "300"],
        mcp={"min_gain_m": 300.0},
        mcp_property="min_gain_m",
        web="min_gain_m=300",
        value=300.0,
    ),
    "max_gain_m": Case(
        cli=["--max-gain", "900"],
        mcp={"max_gain_m": 900.0},
        mcp_property="max_gain_m",
        web="max_gain_m=900",
        value=900.0,
    ),
    "min_distance_km": Case(
        cli=["--min-distance", "4"],
        mcp={"min_distance_km": 4.0},
        mcp_property="min_distance_km",
        web="min_distance_km=4",
        value=4.0,
    ),
    "max_distance_km": Case(
        cli=["--max-distance", "12"],
        mcp={"max_distance_km": 12.0},
        mcp_property="max_distance_km",
        web="max_distance_km=12",
        value=12.0,
    ),
    "circular": Case(
        cli=["--circular"],
        mcp={"circular": True},
        mcp_property="circular",
        web="circular=true",
        value=True,
    ),
    "car_access": Case(
        cli=["--car-access"],
        mcp={"car_access": True},
        mcp_property="car_access",
        web="car_access=true",
        value=True,
    ),
    "chairlift_access": Case(
        cli=["--chairlift-access"],
        mcp={"chairlift_access": True},
        mcp_property="chairlift_access",
        web="chairlift_access=true",
        value=True,
    ),
    "transit_access": Case(
        cli=["--transit-access"],
        mcp={"transit_access": True},
        mcp_property="transit_access",
        web="transit_access=true",
        value=True,
    ),
    # The one field whose spelling differs per surface: the flag and the JSON key are
    # both `poi` (a kind of object to pass), the Criteria field is the normalised tuple.
    "poi_kinds": Case(
        cli=["--poi", "ruins"],
        mcp={"poi": ["ruins"]},
        mcp_property="poi",
        web="poi=ruins",
        value=("ruins",),
    ),
    "ferrata": Case(
        cli=["--ferrata"],
        mcp={"ferrata": True},
        mcp_property="ferrata",
        web="ferrata=true",
        value=True,
    ),
}

CRITERIA_FIELDS = [f.name for f in fields(Criteria)]


def test_every_criteria_field_has_a_parity_case():
    """The gate. A new filter must be given a spelling at all three surfaces here."""
    assert sorted(CASES) == sorted(CRITERIA_FIELDS), (
        "A field was added to or removed from Criteria without updating this table. "
        "Add the case (CLI flag, MCP argument, web query parameter) — the tests below "
        "then check the filter actually reaches all three frontends."
    )


@pytest.mark.parametrize("field", CRITERIA_FIELDS)
def test_the_cli_exposes_every_filter(field):
    case = CASES[field]
    args = build_parser().parse_args(["--bbox", "1", "2", "3", "4", *case.cli])
    assert getattr(cli_criteria(args), field) == case.value


@pytest.mark.parametrize("field", CRITERIA_FIELDS)
def test_the_web_query_parser_exposes_every_filter(field):
    case = CASES[field]
    criteria = web.build_criteria(parse_qs(case.web))
    assert getattr(criteria, field) == case.value


@pytest.mark.parametrize("field", CRITERIA_FIELDS)
def test_the_page_offers_a_control_for_every_filter(field):
    """The other half of the web surface: the parser accepting a parameter is no use
    if the page has no control that sends it. Both halves have to be there, so both
    are asserted — and separately, so a failure names which one is missing."""
    case = CASES[field]
    name = case.web.split("=")[0]
    assert f'id="{name}"' in web.INDEX_HTML, (
        f"the query parser reads {name!r} but the page has no control with that id"
    )


@pytest.mark.parametrize("field", CRITERIA_FIELDS)
def test_the_mcp_handler_reads_every_filter(field):
    server = pytest.importorskip("hike_finder.server")
    case = CASES[field]
    assert getattr(server._criteria(case.mcp), field) == case.value


@pytest.mark.parametrize("field", CRITERIA_FIELDS)
def test_find_hikes_declares_every_filter_in_its_schema(field):
    """The half that actually breaks an LLM client. ``_criteria`` reads the key whether
    or not the schema mentions it, so a handler test alone passes on a tool no client
    can discover the filter on."""
    import asyncio

    server = pytest.importorskip("hike_finder.server")
    tools = {t.name: t for t in asyncio.run(server.list_tools()).tools}
    props = tools["find_hikes"].input_schema["properties"]
    assert CASES[field].mcp_property in props


# --- the point-mode tools ---------------------------------------------------------
#
# `circular_routes`, `routes_between`, `route_via` and `routes_to_poi` all build their
# filters with the SAME `server._criteria`, so all four honour all ten filters. Their
# schemas advertise fewer, and NOTHING IN `server.py` SAYS WHY for any of them — there is
# no comment at any of the four schemas explaining an omission. So this is one table, not
# a "deliberate" set and a "gap" set: sorting them into intent nobody recorded would be
# this repo's own standing lesson (a label promising what the selector never checked)
# committed in the test that exists to catch it.
#
# Each entry carries how it reads today. The test asserts the table is EXACT, so both
# drifts fail: a filter quietly dropped from a schema, and a gap quietly closed.
NOT_ADVERTISED = {
    # Reads as inapplicable: every result of these modes is a loop / is not one, so the
    # shape filter has nothing left to select.
    ("circular_routes", "circular"),
    ("routes_between", "circular"),
    ("route_via", "circular"),
    ("routes_to_poi", "circular"),
    # Reads as a gap. The engine applies these, and the CLI exposes every one of them on
    # the same mode — `--min-gain` works with `--around`. An LLM simply cannot ask.
    ("circular_routes", "min_gain_m"),
    ("circular_routes", "max_gain_m"),
    ("routes_between", "min_gain_m"),
    ("routes_between", "max_gain_m"),
    ("routes_between", "min_distance_km"),
    ("route_via", "min_gain_m"),
    ("route_via", "max_gain_m"),
    ("routes_to_poi", "min_distance_km"),
    # Reads as arguable, and is left listed for that reason rather than filed as decided.
    # You pick the endpoints in these modes, but you pick COORDINATES — whether parking, a
    # lift or a bus stop is mapped near them is exactly what these filters answer, and
    # picking a point is not knowing that. `circular_routes` advertises all three.
    ("routes_between", "car_access"),
    ("routes_between", "chairlift_access"),
    ("routes_between", "transit_access"),
    ("route_via", "car_access"),
    ("route_via", "chairlift_access"),
    ("route_via", "transit_access"),
    ("routes_to_poi", "chairlift_access"),
    ("routes_to_poi", "transit_access"),
}

POINT_MODE_TOOLS = ["circular_routes", "routes_between", "route_via", "routes_to_poi"]


@pytest.mark.parametrize("tool_name", POINT_MODE_TOOLS)
def test_point_mode_tools_advertise_the_filters_they_honour(tool_name):
    """What each point-mode tool leaves out of its schema is exactly what is tabled.

    Exact rather than one-directional, so both drifts fail: a filter quietly dropped
    from one of these schemas (it appears in neither the schema nor the table), and one
    quietly added without deleting its entry (the table would then describe a gap that
    no longer exists, which is how a table like this rots).
    """
    import asyncio

    server = pytest.importorskip("hike_finder.server")
    tools = {t.name: t for t in asyncio.run(server.list_tools()).tools}
    props = set(tools[tool_name].input_schema["properties"])

    absent = {f for f in CRITERIA_FIELDS if CASES[f].mcp_property not in props}
    tabled = {f for (tool, f) in NOT_ADVERTISED if tool == tool_name}
    assert absent == tabled, (
        f"{tool_name} honours every Criteria field via server._criteria. It now leaves "
        f"{sorted(absent - tabled)} out of its schema without an entry in "
        f"NOT_ADVERTISED, and advertises {sorted(tabled - absent)} which is still "
        f"listed there."
    )
