"""MCP server exposing OSM-based hike search with real computed stats.

Tools:
  find_hikes(south, west, north, east, min_gain_m, max_gain_m,
             min_distance_km, max_distance_km,
             circular, car_access, chairlift_access, ferrata,
             near_misses, area, compose_loops, name_places, format)
  list_pois(south, west, north, east | area, kinds, format)  — list the churches /
             ruins / peaks in an area WITHOUT routing to them. One Overpass call, no
             elevation, no quota; `area` makes it fully offline.
  list_ferrata(south, west, north, east | area, format)  — the same, for via ferrata:
             list an area's cabled lines without routing to them. The counterpart of
             the `ferrata` flag, which filters ROUTES by whether they include cable.
  download_area(south, west, north, east, path)  — fetch an area once and save it
             so find_hikes(area=path) can search it offline with no further API calls.

Uses the official `mcp` Python SDK. Run with:  python -m hike_finder.server
(stdio transport — point your MCP client / Claude Code at this command).

NOTE: requires network at runtime (Overpass + elevation), EXCEPT find_hikes with
`area` set, which is fully offline. The build sandbox can't reach the network, so the
live paths are validated by you on your machine. The pure-math core (geometry, gain,
access, parsing, snapshot round-trip) is unit-tested offline.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

from . import config as _config
from .export import hikes_to_geojson, hikes_to_gpx, pois_to_geojson, pois_to_gpx
from .filters import Criteria
from .format import format_hike, format_poi, format_poi_summary
from .poi import POI_KINDS, kind_labels, normalise_kinds
from .search import (
    area_has_no_routes,
    area_records_ferrata,
    compose_loops,
    compose_loops_around,
    download_area,
    ferrata_gap_message,
    list_area_ferrata,
    list_area_pois,
    list_snapshot_ferrata,
    list_snapshot_pois,
    no_routes_message,
    route_via,
    routes_between,
    routes_to_poi,
    search_hikes,
    search_snapshot,
    snapshot_kinds_missing_message,
    snapshot_poi_gap,
)
from .snapshot import (
    AreaSnapshot,
    list_snapshots,
    load_snapshot,
    save_snapshot,
    snapshot_path,
)

CFG = _config.load()

# `app` is built at the BOTTOM of this module, not here: the mcp 2.x `Server` takes its
# handlers as constructor arguments (`on_list_tools` / `on_call_tool`) rather than through
# decorators, so it cannot exist until they are defined. Everything else about the nine
# tools is unchanged — `Tool(inputSchema=...)` still validates, via the field's alias.

# The destination filter, offered identically by every search tool — "a 10 km hike that
# goes to a ruin" is the same question whether the routes come from a bounding box, a
# composed loop, or a point-to-point draw. Defined once so the wording can't drift, and
# the kind list is generated from the ONE registry (poi.py) so it can never offer
# something the engine would reject.
_POI_SCHEMA = {
    "poi": {
        "type": "array",
        "items": {"type": "string", "enum": sorted(POI_KINDS)},
        "description": (
            "Keep only routes that pass a point of interest of one of these kinds — "
            "e.g. [\"ruins\"] for 'a hike that goes to a ruin'. Several kinds are OR-ed. "
            "Each result lists what it reaches and how far off the trail it sits. "
            "Available: "
            + "; ".join(f"{k} ({lbl})" for k, lbl in kind_labels())
            + ". A miss means nothing of that kind is MAPPED in OSM near the route, not "
            "that nothing is there."
        ),
    },
    "poi_radius_m": {
        "type": "number",
        "description": "How close a route must pass to count as reaching a `poi`, in "
        "metres (default 250). Measured to the trail line, not to its mapped nodes.",
    },
}


_FERRATA_SCHEMA = {
    "ferrata": {
        "type": "boolean",
        "description": (
            "Cabled climbing (via ferrata / klettersteig), which you walk in a harness "
            "clipped to a fixed steel cable — a different activity from hiking, not a "
            "harder hike. true = keep ONLY routes with cable, including dedicated "
            "route=via_ferrata relations that no other search returns; false = drop "
            "routes known to include cable. Detected from OSM tags "
            "(highway=via_ferrata / via_ferrata_scale) on the route's member ways and "
            "on the relation itself. IMPORTANT: false is a filter, not a safety "
            "guarantee — cable that nobody has tagged in OSM cannot be detected, so "
            "never report a surviving route as verified free of cable. Each result "
            "reports the grades found and how much of its length is cabled."
        ),
    },
}


async def list_tools(_ctx=None, _params=None) -> ListToolsResult:
    """The nine tools. Both arguments are the mcp 2.x handler signature (request
    context, pagination params); neither is used — the list is small and static, so it
    ships in one page with a null cursor."""
    return ListToolsResult(tools=[
        Tool(
            name="find_hikes",
            description=(
                "Find marked OSM hiking routes in a bounding box, filtered by real "
                "computed elevation gain and distance, plus shape and access. Data is "
                "OpenStreetMap route relations (same source family as mapy.cz); "
                "gain/distance are computed locally, not scraped.\n\n"
                "Filters (all optional): elevation gain (m), distance (km), `circular` "
                "(loop vs point-to-point), `car_access` (parking mapped near a trail "
                "end), `chairlift_access` (a ride-up aerialway — chairlift/gondola/"
                "cable car — mapped near a trail end). Boolean filters are tri-state: "
                "omit = don't care, true = require, false = exclude.\n\n"
                "Confidence: shape (circular) is reliable. car_access/chairlift_access "
                "are best-effort from OSM completeness — a `false` means nothing of "
                "that kind is MAPPED near the route's ends, not that it is impossible "
                "to get there.\n\n"
                "Bounding box: pass south/west/north/east for a live search, OR `area` "
                "(a snapshot path from download_area) to search offline with no API calls "
                "— then the box is taken from the snapshot.\n\n"
                "Set `compose_loops` true to SYNTHESISE loops by combining connected "
                "marked trails inside the box, instead of reporting each OSM relation "
                "as-is — useful for day-loops that aren't mapped as a single relation. "
                "Target length comes from min/max_distance_km (default 3-15 km); results "
                "are stitched from several trails and have no single relation id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "south": {"type": "number"},
                    "west": {"type": "number"},
                    "north": {"type": "number"},
                    "east": {"type": "number"},
                    "min_gain_m": {"type": "number"},
                    "max_gain_m": {"type": "number"},
                    "min_distance_km": {"type": "number"},
                    "max_distance_km": {"type": "number"},
                    "circular": {
                        "type": "boolean",
                        "description": "true = loops only, false = point-to-point only.",
                    },
                    "car_access": {
                        "type": "boolean",
                        "description": "true = require parking mapped near an endpoint.",
                    },
                    "chairlift_access": {
                        "type": "boolean",
                        "description": "true = require a ride-up aerialway near an endpoint.",
                    },
                    "transit_access": {
                        "type": "boolean",
                        "description": "true = require public transport near an endpoint — a "
                        "train station or halt within 1 km, or a tram/bus stop within 400 m "
                        "('a hike I can reach without a car'). Each result names which kind. "
                        "An `area` snapshot downloaded before this feature carries no transit "
                        "data; the search then returns nothing and says so, rather than "
                        "reporting every route as unreachable.",
                    },
                    **_POI_SCHEMA,
                    **_FERRATA_SCHEMA,
                    "near_misses": {
                        "type": "boolean",
                        "description": "Also return routes that just miss the filters, each "
                        "flagged and annotated with how it falls short. Omit = show them only "
                        "when nothing matches; true = always; false = never.",
                    },
                    "area": {
                        "type": "string",
                        "description": "An already-downloaded area to search. When set, the "
                        "search runs OFFLINE against the snapshot and south/west/north/east "
                        "are ignored. Accepts either a path written by download_area OR the "
                        "bare `name` shown by list_areas.",
                    },
                    "compose_loops": {
                        "type": "boolean",
                        "description": "true = synthesise loops from connected marked trails "
                        "inside the box (live only; ignored with `area`). Target length from "
                        "min/max_distance_km. Results are stitched from several trails.",
                    },
                    "name_places": {
                        "type": "boolean",
                        "description": "true = reverse-geocode UNNAMED routes (route/<id>) to a "
                        "place-derived label like 'Pec → Sněžka' via Nominatim. Off by default; "
                        "only matched routes are looked up (throttled + cached). Live only — an "
                        "offline `area` search can't reach the network.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "gpx", "geojson"],
                        "description": "Output format. 'text' (default) returns the one-line "
                        "human summaries; 'gpx' returns a GPX 1.1 document and 'geojson' a "
                        "GeoJSON FeatureCollection of the matched + composed routes (the file "
                        "you load into a GPS / phone / Komoot / OsmAnd / mapy.cz), as text. "
                        "When nothing matches, the helpful text message is returned regardless.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="circular_routes",
            description=(
                "Draw circular day-loops (round trips) that pass NEAR a single picked point "
                "and start there. Give a point (lat/lon); the tool synthesises loops from the "
                "connected marked trails around it whose total length is in the min/max "
                "distance band (default 3-15 km) and that come within `radius_m` of the point "
                "(default 1000 m), each started at the on-loop spot nearest your point.\n\n"
                "Use this for 'find me a ~10 km loop starting near HERE'. The area is derived "
                "from the point — no bounding box needed.\n\n"
                "Filters (all optional): elevation gain (min/max_gain_m), length "
                "(min/max_distance_km, which is also the target band the loops are built to), "
                "car_access / chairlift_access / transit_access (a parking lot / lift / stop "
                "mapped near the loop), `poi` (loops passing an object of a kind) and "
                "`ferrata`. Results are stitched from several trails and have no single OSM "
                "relation id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude of the point."},
                    "lon": {"type": "number", "description": "Longitude of the point."},
                    "radius_m": {
                        "type": "number",
                        "description": (
                            "How near a loop must pass to the point, metres (default 1000)."
                        ),
                    },
                    "min_distance_km": {"type": "number"},
                    "max_distance_km": {"type": "number"},
                    "min_gain_m": {
                        "type": "number",
                        "description": "Keep only loops climbing at least this much, metres.",
                    },
                    "max_gain_m": {
                        "type": "number",
                        "description": "Keep only loops climbing at most this much, metres "
                        "('a flat one, please').",
                    },
                    "car_access": {
                        "type": "boolean",
                        "description": "true = require parking mapped near the loop.",
                    },
                    "chairlift_access": {
                        "type": "boolean",
                        "description": "true = require a ride-up aerialway near the loop.",
                    },
                    "transit_access": {
                        "type": "boolean",
                        "description": "true = require public transport near the loop (rail "
                        "within 1 km, tram/bus within 400 m).",
                    },
                    **_POI_SCHEMA,
                    **_FERRATA_SCHEMA,
                    "near_misses": {
                        "type": "boolean",
                        "description": "Also return loops that just miss the filters, annotated.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "gpx", "geojson"],
                        "description": "Output format (default 'text'); 'gpx'/'geojson' return the "
                        "downloadable route document as text.",
                    },
                },
                "required": ["lat", "lon"],
            },
        ),
        Tool(
            name="routes_between",
            description=(
                "Draw the N shortest DISTINCT walking routes between two picked points, "
                "shortest first. Give a start and a finish (lat/lon each); the tool snaps each "
                "onto the nearest marked trail and returns up to `routes` alternatives ordered "
                "by length (the shortest, then a genuinely different second-shortest, etc.).\n\n"
                "Use this for 'how do I walk from A to B, and what are my options'. The area is "
                "derived from the two points — no bounding box needed. A point more than ~2 km "
                "from any trail is treated as off-network and yields no routes. Results are "
                "stitched from several trails (no single OSM id).\n\n"
                "Filters (all optional): elevation gain (min/max_gain_m), length "
                "(min/max_distance_km), `poi` (routes passing an object of a kind) and "
                "`ferrata`. They SELECT among the shortest-first alternatives — they do not "
                "make the search look for a longer or hillier way round."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "start_lat": {"type": "number"},
                    "start_lon": {"type": "number"},
                    "finish_lat": {"type": "number"},
                    "finish_lon": {"type": "number"},
                    "routes": {
                        "type": "integer",
                        "description": "How many routes to return, shortest first (default 3).",
                    },
                    "max_distance_km": {
                        "type": "number",
                        "description": (
                            "Cap a route's length, km (default: 3x the straight-line gap)."
                        ),
                    },
                    "min_distance_km": {
                        "type": "number",
                        "description": "Discard routes shorter than this, km. A filter on the "
                        "shortest-first alternatives, NOT a request for a scenic detour: asking "
                        "for 10 km between points 2 km apart returns nothing at all.",
                    },
                    "min_gain_m": {
                        "type": "number",
                        "description": "Discard routes climbing less than this, metres.",
                    },
                    "max_gain_m": {
                        "type": "number",
                        "description": "Discard routes climbing more than this, metres.",
                    },
                    **_POI_SCHEMA,
                    **_FERRATA_SCHEMA,
                    "format": {
                        "type": "string",
                        "enum": ["text", "gpx", "geojson"],
                        "description": "Output format (default 'text').",
                    },
                },
                "required": ["start_lat", "start_lon", "finish_lat", "finish_lon"],
            },
        ),
        Tool(
            name="route_via",
            description=(
                "Draw ONE walking route linking SEVERAL picked points in the order given, each "
                "snapped to the nearest marked trail. Give `points` (>=2 lat/lon pairs). With "
                "`loop` false (default) it draws the shortest open route point1 -> point2 -> ... "
                "-> pointN. With `loop` true it closes the route back to the first point into a "
                "CIRCULAR route whose return avoids retracing the way out where the trail network "
                "allows (a genuine loop, not an out-and-back).\n\n"
                "Use this for 'link these spots into one walk' or 'give me a loop passing through "
                "these places'. The area is derived from the points — no bounding box needed. "
                "Points are visited in the order given (no reordering). A point more than ~2 km "
                "from any trail, or a leg crossing a gap in the network, yields no route. Results "
                "are stitched from several trails (no single OSM id).\n\n"
                "Filters (all optional): elevation gain (min/max_gain_m), length "
                "(min/max_distance_km), `poi` and `ferrata`. Only ONE route is drawn through "
                "your points, so these DISCARD it rather than choosing between alternatives — a "
                "band the linked route misses returns nothing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "points": {
                        "type": "array",
                        "minItems": 2,
                        "items": {
                            "type": "object",
                            "properties": {
                                "lat": {"type": "number"},
                                "lon": {"type": "number"},
                            },
                            "required": ["lat", "lon"],
                        },
                        "description": "Waypoints to link, in visiting order (>=2).",
                    },
                    "loop": {
                        "type": "boolean",
                        "description": (
                            "true = close into a non-retracing circular route (default false)."
                        ),
                    },
                    "min_distance_km": {
                        "type": "number",
                        "description": "Drop the linked route if it runs shorter than this, km.",
                    },
                    "max_distance_km": {
                        "type": "number",
                        "description": "Drop the linked route if it runs longer than this, km.",
                    },
                    "min_gain_m": {
                        "type": "number",
                        "description": "Drop the linked route if it climbs less than this, metres.",
                    },
                    "max_gain_m": {
                        "type": "number",
                        "description": "Drop the linked route if it climbs more than this, metres.",
                    },
                    **_POI_SCHEMA,
                    **_FERRATA_SCHEMA,
                    "format": {
                        "type": "string",
                        "enum": ["text", "gpx", "geojson"],
                        "description": "Output format (default 'text').",
                    },
                },
                "required": ["points"],
            },
        ),
        Tool(
            name="routes_to_poi",
            description=(
                "Draw walking routes FROM a picked point TO the nearest churches / ruins / "
                "peaks / viewpoints — 'draw me a route to the nearest ruin'. Give a start "
                "(lat/lon) and one or more destination `kinds`; the tool finds the objects of "
                "those kinds around the start and returns up to `routes` routes, one per "
                "destination, nearest first.\n\n"
                "Nearest means nearest ALONG THE TRAILS, not as the crow flies — a ruin just "
                "across a gorge with no path to it does not win. Each result names what it "
                "was drawn to and how far its end lands from it: the route ends at the "
                "nearest point ON A TRAIL, which is not the object itself.\n\n"
                "This is the OPPOSITE of the `poi` filter offered by the other tools. `poi` "
                "keeps routes that happen to pass an object; this one draws the route to it. "
                "The area is derived from the start point — no bounding box needed. If "
                "nothing is found the reply says which of the three causes it was (nothing "
                "of that kind mapped nearby / found but off-network / too far to route to), "
                "because they need different fixes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude of the start point."},
                    "lon": {"type": "number", "description": "Longitude of the start point."},
                    "kinds": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "enum": sorted(POI_KINDS)},
                        "description": (
                            "What to walk to; several kinds are OR-ed (\"the nearest ruin OR "
                            "castle\"). Available: "
                            + "; ".join(f"{k} ({lbl})" for k, lbl in kind_labels())
                        ),
                    },
                    "routes": {
                        "type": "integer",
                        "description": (
                            "How many destinations to route to, nearest first (default 3)."
                        ),
                    },
                    "search_radius_m": {
                        "type": "number",
                        "description": "How far from the start to look for destinations, metres "
                        "(default 3000). It also sizes the fetched area, so raising it makes the "
                        "query heavier — but it is the lever when nothing of the kind is found.",
                    },
                    "max_distance_km": {
                        "type": "number",
                        "description": "Cap a route's length, km (default: 3x the straight-line "
                        "distance to that destination). It also sizes the fetched area.",
                    },
                    "min_distance_km": {
                        "type": "number",
                        "description": "Discard routes shorter than this, km — a filter on the "
                        "nearest-first results, not a search for a farther destination. Unlike "
                        "max_distance_km it does NOT size the fetched area, so raising it never "
                        "brings more distant objects into view.",
                    },
                    "min_gain_m": {"type": "number"},
                    "max_gain_m": {"type": "number"},
                    "car_access": {
                        "type": "boolean",
                        "description": "true = require parking mapped near the route's ends.",
                    },
                    **_POI_SCHEMA,
                    **_FERRATA_SCHEMA,
                    "format": {
                        "type": "string",
                        "enum": ["text", "gpx", "geojson"],
                        "description": "Output format (default 'text').",
                    },
                },
                "required": ["lat", "lon", "kinds"],
            },
        ),
        Tool(
            name="list_pois",
            description=(
                "List the churches / ruins / peaks / viewpoints / huts themselves in an "
                "area — 'what ruins are there around here?' — WITHOUT drawing any route to "
                "them. Give a bounding box for a live look, or `area` to read a downloaded "
                "snapshot with no network at all.\n\n"
                "This is the third and simplest of the three POI questions this server "
                "answers. `poi` on the search tools FILTERS routes by what they pass; "
                "routes_to_poi DRAWS a route to the nearest one; this one just LISTS the "
                "objects. Nothing is measured, because nothing is walked: it makes ONE "
                "Overpass call, spends nothing from the elevation API budget, and needs no "
                "DEM.\n\n"
                "Omit `kinds` to list every registered kind. Results are grouped by kind and "
                "carry each object's name and coordinate, so they can be pinned or handed "
                "onward directly; `format` can also emit them as GPX waypoints or GeoJSON "
                "points to load into a GPS. An empty result means nothing of that kind is "
                "MAPPED in OSM there, not that nothing is there."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "south": {"type": "number"},
                    "west": {"type": "number"},
                    "north": {"type": "number"},
                    "east": {"type": "number"},
                    "area": {
                        "type": "string",
                        "description": "An already-downloaded area to list, with zero network — "
                        "takes the place of the bounding box. Accepts either a path written by "
                        "download_area OR the bare `name` shown by list_areas.",
                    },
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(POI_KINDS)},
                        "description": (
                            "Which kinds to list; several are OR-ed. Omit for ALL of them. "
                            "Available: "
                            + "; ".join(f"{k} ({lbl})" for k, lbl in kind_labels())
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "json", "gpx", "geojson"],
                        "description": "Output format (default 'text'). 'gpx' emits waypoints, "
                        "'geojson' a FeatureCollection of points — both loadable into a GPS "
                        "or phone.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="list_ferrata",
            description=(
                "List the via ferrata / klettersteig routes in an area — 'what cabled "
                "climbs are around here?' — WITHOUT drawing any hiking route to them. "
                "Give a bounding box for a live look, or `area` to read a downloaded "
                "snapshot with no network at all.\n\n"
                "A via ferrata is a climb equipped with fixed steel cable, rungs and "
                "ladders, walked in a harness clipped to the cable. It is a different "
                "activity from hiking, not a harder hike — do not present these as walks.\n\n"
                "The counterpart of the `ferrata` flag on the search tools, which "
                "filters ROUTES by whether they include cable. This one lists the cabled "
                "lines themselves. Cheap: ONE Overpass call, no elevation lookup, nothing "
                "from the API budget. Dedicated route=via_ferrata relations come first, "
                "then individual cabled ways — the latter are often one pitch of a longer "
                "climb rather than a climb in themselves, and are labelled as such.\n\n"
                "Each entry carries its OSM grade verbatim (mixed scales are in use — "
                "numeric 0-6 with +/- modifiers, and A-F elsewhere — so do not compare or "
                "rank them without checking which scale applies). An empty result means "
                "nothing is TAGGED as cabled in OSM there, not that nothing is there."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "south": {"type": "number"},
                    "west": {"type": "number"},
                    "north": {"type": "number"},
                    "east": {"type": "number"},
                    "area": {
                        "type": "string",
                        "description": "An already-downloaded area to list, with zero "
                        "network — takes the place of the bounding box. Accepts a path "
                        "from download_area or the bare `name` from list_areas. An area "
                        "saved before cabled routes were fetched holds none and says so.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["text", "json"],
                        "description": "Output format (default 'text').",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="download_area",
            description=(
                "Fetch a bounding box once — its hiking routes plus computed elevation for "
                "every plausible route — and save it to `path`. This spends the elevation "
                "budget up front; afterwards find_hikes(area=path) searches it offline with "
                "no further API calls. Use it to avoid re-hitting the rate-limited elevation "
                "API while exploring an area with different filters."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "south": {"type": "number"},
                    "west": {"type": "number"},
                    "north": {"type": "number"},
                    "east": {"type": "number"},
                    "path": {"type": "string", "description": "Where to write the snapshot JSON."},
                    "name_places": {
                        "type": "boolean",
                        "description": "true = also bake reverse-geocoded names for the unnamed "
                        "routes into the snapshot, so a later offline find_hikes(area=path, "
                        "name_places=true) can label them with no network. Off by default "
                        "(it queries Nominatim at ~1 req/s for every unnamed route).",
                    },
                },
                "required": ["south", "west", "north", "east", "path"],
            },
        ),
        Tool(
            name="list_areas",
            description=(
                "List the areas already downloaded for offline searching — name, bounding "
                "box, when it was fetched, and what it contains (routes, elevation samples, "
                "points of interest). Use it before download_area to avoid re-fetching "
                "ground you already have, and to find the `area` value for an offline "
                "find_hikes. "
                "Scope: this lists the NAMED snapshot directory (HIKE_SNAPSHOT_DIR, the one "
                "the web UI downloads into). A snapshot written by download_area to an "
                "arbitrary `path` is not tracked here — pass that path to find_hikes(area=…) "
                "directly. An entry reporting 0 points of interest predates the POI feature "
                "and cannot answer a `poi` search until it is re-downloaded."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
        ),
    ])


def _near_miss(arguments: dict) -> bool | str:
    """Tri-state from the optional `near_misses` flag: omit -> 'auto'."""
    v = arguments.get("near_misses")
    return "auto" if v is None else v


def _criteria(arguments: dict) -> Criteria:
    # `poi` is validated against the registry: an unknown kind raises here (surfaced to
    # the caller by call_tool) rather than quietly matching nothing, which an LLM client
    # would read as "there are no ruins in this valley".
    return Criteria(
        min_gain_m=arguments.get("min_gain_m"),
        max_gain_m=arguments.get("max_gain_m"),
        min_distance_km=arguments.get("min_distance_km"),
        max_distance_km=arguments.get("max_distance_km"),
        circular=arguments.get("circular"),
        car_access=arguments.get("car_access"),
        chairlift_access=arguments.get("chairlift_access"),
        transit_access=arguments.get("transit_access"),
        poi_kinds=normalise_kinds(arguments.get("poi")),
        ferrata=arguments.get("ferrata"),
    )


def _cfg(arguments: dict):
    """The shared config, with any per-call knob applied to a COPY.

    ``CFG`` is a module-level singleton and tool calls run on worker threads, so
    mutating it in place would leak one caller's POI radius into another's search.
    """
    radius = arguments.get("poi_radius_m")
    return CFG if radius is None else replace(CFG, poi_radius_m=float(radius))


def _serialize(hikes: list, fmt: str, empty_msg: str) -> list[TextContent]:
    """Render a hike list as text / GPX / GeoJSON, or the helpful message when empty."""
    if not hikes:
        return [TextContent(type="text", text=empty_msg)]
    if fmt == "gpx":
        return [TextContent(type="text", text=hikes_to_gpx(hikes))]
    if fmt == "geojson":
        return [TextContent(type="text", text=hikes_to_geojson(hikes))]
    return [TextContent(type="text", text="\n".join(format_hike(h) for h in hikes))]


def _handler_for(name: str):
    """Resolve a tool name to its implementation, or raise for a name we don't serve.

    Deliberately OUTSIDE the try in `call_tool`: an unknown tool name and a bad argument
    are different failures and belong on different channels (see `call_tool`).
    """
    if name == "find_hikes":
        return _call_find_hikes
    if name == "circular_routes":
        return _call_circular_routes
    if name == "routes_between":
        return _call_routes_between
    if name == "route_via":
        return _call_route_via
    if name == "routes_to_poi":
        return _call_routes_to_poi
    if name == "list_pois":
        return _call_list_pois
    if name == "list_ferrata":
        return _call_list_ferrata
    if name == "download_area":
        return _call_download_area
    if name == "list_areas":
        return _call_list_areas
    raise ValueError(f"unknown tool: {name}")


async def call_tool(_ctx, params: CallToolRequestParams) -> CallToolResult:
    """Dispatch one tool call. `_ctx` is the mcp 2.x request context; unused.

    The two failure modes go back on deliberately different channels:

    - **Unknown tool name** raises, and the SDK turns that into a JSON-RPC error. The
      client asked for something this server does not have; that is protocol-level, and
      there is no sensible tool *output* for it.
    - **Bad arguments** (an unregistered POI kind, say) come back as ordinary content
      with `is_error` set. An LLM client has to be able to READ the message to correct
      its own call — "cathedral is not a kind, try ruins" is useless as an exception it
      never sees. Under mcp 1.x both landed on the same channel because the framework
      caught the raise for us; splitting them is the point of doing this by hand.

    Which makes the handlers' own `raise ValueError(...)` argument guards LOAD-BEARING:
    mcp 2.x does not validate arguments against a tool's `inputSchema` server-side (the
    `required` list is advice to the client), so a call missing `kinds` reaches the
    handler. Those guards used to *return* their message, which under 2.x comes back
    with `is_error` false — i.e. a complaint dressed as a successful answer. Raising
    puts them on this channel instead. `test_routes_to_poi_without_kinds_asks_for_them`
    is what caught it.
    """
    handler = _handler_for(params.name)
    try:
        return CallToolResult(content=await handler(params.arguments or {}))
    except ValueError as exc:
        return CallToolResult(
            content=[TextContent(type="text", text=str(exc))], is_error=True
        )


def _point_empty(diagnostics: dict, msg: str) -> str:
    """The empty-result sentence for a point mode, or the one fact that outranks it.

    The four point tools derive their own bbox from the point(s) the caller passed, so
    "OSM maps no hiking route relations here" is exactly as true of that box as of a
    drawn one — and every sentence below blames a radius, a snap distance or a length cap
    for excluding something that was never there. It matters more on this frontend than
    on the CLI: an LLM reading "widen search_radius_m" will dutifully retry, and each
    retry costs another Overpass request to learn the same thing.

    One kind only, never the ferrata gap: a point mode is always a LIVE fetch, which
    parses both cable lists and the member-way tags — and the ferrata clause changed the
    query text, i.e. the Overpass cache key, so a pre-feature response cannot be served
    under one either. Same reasoning as ``find_hikes``' live branch.
    """
    return no_routes_message() if diagnostics.get("no_routes") else msg


async def _call_circular_routes(arguments: dict) -> list[TextContent]:
    if [k for k in ("lat", "lon") if k not in arguments]:
        raise ValueError("provide lat and lon for the point to search around.")
    point = (arguments["lat"], arguments["lon"])
    diagnostics: dict = {}
    hikes = await asyncio.to_thread(
        compose_loops_around,
        point,
        _criteria(arguments),
        _cfg(arguments),
        radius_m=arguments.get("radius_m"),
        near_miss=_near_miss(arguments),
        diagnostics=diagnostics,
    )
    return _serialize(
        hikes,
        arguments.get("format") or "text",
        _point_empty(
            diagnostics,
            "No circular routes pass within the radius of your point — widen radius_m, the "
            "min/max_distance_km or min/max_gain_m band, or drop car_access/chairlift_access.",
        ),
    )


async def _call_routes_between(arguments: dict) -> list[TextContent]:
    if [
        k for k in ("start_lat", "start_lon", "finish_lat", "finish_lon") if k not in arguments
    ]:
        raise ValueError(
            "provide start_lat/start_lon and finish_lat/finish_lon for the two points."
        )
    start = (arguments["start_lat"], arguments["start_lon"])
    finish = (arguments["finish_lat"], arguments["finish_lon"])
    diagnostics: dict = {}
    hikes = await asyncio.to_thread(
        routes_between, start, finish, _criteria(arguments), _cfg(arguments),
        k=arguments.get("routes"), diagnostics=diagnostics,
    )
    return _serialize(
        hikes,
        arguments.get("format") or "text",
        _point_empty(
            diagnostics,
            "No routes could be drawn between your two points — they may sit on disconnected "
            "trail networks, be off-network (more than ~2 km from any trail), or every route "
            "falls outside the min/max_distance_km or min/max_gain_m band.",
        ),
    )


async def _call_route_via(arguments: dict) -> list[TextContent]:
    raw = arguments.get("points") or []
    points = [
        (p["lat"], p["lon"])
        for p in raw
        if isinstance(p, dict) and "lat" in p and "lon" in p
    ]
    if len(points) < 2:
        return [
            TextContent(
                type="text",
                text="provide at least two points ({lat, lon}) to link into a route.",
            )
        ]
    loop = bool(arguments.get("loop"))
    diagnostics: dict = {}
    hikes = await asyncio.to_thread(
        route_via, points, _criteria(arguments), _cfg(arguments), loop=loop,
        diagnostics=diagnostics,
    )
    empty = (
        "No circular route could be drawn through your points — a point may be off-network "
        "(more than ~2 km from any trail), a leg crosses a gap in the network, or the closed "
        "route falls outside the min/max_distance_km or min/max_gain_m band."
        if loop
        else "No route could be drawn through your points — a point may be off-network (more "
        "than ~2 km from any trail), a leg crosses a gap, or the route falls outside the "
        "min/max_distance_km or min/max_gain_m band."
    )
    return _serialize(hikes, arguments.get("format") or "text", _point_empty(diagnostics, empty))


async def _call_routes_to_poi(arguments: dict) -> list[TextContent]:
    if [k for k in ("lat", "lon") if k not in arguments] or not arguments.get("kinds"):
        raise ValueError(
            "provide lat and lon for the start point, and `kinds` — what to walk to "
            "(e.g. [\"ruins\"])."
        )
    diagnostics: dict = {}
    hikes = await asyncio.to_thread(
        routes_to_poi,
        (arguments["lat"], arguments["lon"]),
        # Validated against the registry, so an unknown kind raises (surfaced by call_tool)
        # rather than reading to the client as "there are no ruins in this valley".
        normalise_kinds(arguments["kinds"]),
        _criteria(arguments),
        _cfg(arguments),
        n=arguments.get("routes"),
        search_radius_m=arguments.get("search_radius_m"),
        diagnostics=diagnostics,
    )
    # Destination-shaped, never the `poi`-filter wording: nothing was filtered out of an
    # area here, a route to an object could not be drawn. `no_routes` outranks even that:
    # this sentence names three causes and all three are about the destination, so on a
    # box with no trails in it, it reports missing CHURCHES to someone whose real problem
    # is a map with no walking routes. `diagnostics["no_routes"]` reads `area.routes`
    # only — an area full of trails and free of ruins keeps the sentence below.
    return _serialize(
        hikes,
        arguments.get("format") or "text",
        _point_empty(
            diagnostics,
            "No route could be drawn to an object of that kind — either nothing of it is mapped "
            "within search_radius_m of your point (widen it), the ones found sit off the trail "
            "network, or every route to them falls outside your length band (raise "
            "max_distance_km, lower min_distance_km). "
            "A miss means nothing of that kind is MAPPED in OSM near your point, not that "
            "nothing is there.",
        ),
    )


def _ferrata_caveat(snapshot, criteria: Criteria) -> str:
    """What a SAVED area cannot answer about cable, worded for an LLM client — or "".

    The message itself comes verbatim from ``search.ferrata_gap_message``, the one place
    that picks between the two sentences (and whose ORDER is what keeps each one true).
    Only the closing instruction is server-local, and it is the whole reason this frontend
    needs its own wording: the CLI writes its copy to stderr, where a human reads an empty
    result and a warning as two facts, while an LLM reads a tool reply and paraphrases it
    into prose for someone who never sees the original. "No matching hikes found" with the
    caveat missing becomes "there is nothing cabled there" (asked to avoid) or "there are
    no via ferrata there" (asked to find) — opposite misreadings of the same silence,
    which is why the sentence added here is direction-neutral rather than one per flag.

    Takes the SNAPSHOT rather than its area so the flag guard runs before anything is read
    off the file at all: no ferrata question was asked, nothing to disclaim, nothing to
    look at.
    """
    if criteria.ferrata is None:
        return ""
    gap = ferrata_gap_message(snapshot.area, finding=criteria.ferrata is True)
    if gap is None:
        return ""
    return (
        f"{gap} Do NOT turn this into a statement about cable on the ground, in either "
        f"direction — this file cannot say.\n"
    )


async def _read_area(area: str) -> AreaSnapshot | list[TextContent]:
    """Read a saved area given EITHER a path OR the bare name ``list_areas`` prints.

    Returns the snapshot, or the sentence to hand back instead. One value rather than a
    pair, so "exactly one of the two is filled" is a fact about the type instead of a
    promise in prose: a caller's whole obligation is ``if isinstance(x, list): return x``.

    Two behaviours, and both exist because of how this frontend is driven. An LLM reads a
    ``name`` out of ``list_areas`` and passes it straight back; a path wins when one is
    given, and otherwise the named snapshot directory is tried, so that name works here
    verbatim. And a file that cannot be read comes back as a SENTENCE naming what the
    caller actually typed, not as a ``FileNotFoundError`` raised from deep inside
    ``load_snapshot`` about a path they never wrote.

    Shared by all three area-reading tools rather than copied into each. It was copied
    into two of them, and the third — ``find_hikes``, the tool an LLM calls most — never
    got the copy and raised on a bare name. That is this project's recurring shape: the
    ferrata caveat reached its three frontends on three different days. One function is
    what makes "all three agree" checkable instead of remembered.
    """
    path = area
    if not os.path.isfile(path):
        named = snapshot_path(path)
        if named is not None and named.is_file():
            path = str(named)
    try:
        return await asyncio.to_thread(load_snapshot, path)
    except (OSError, ValueError) as e:
        return [TextContent(type="text", text=(
            f"Could not read the area {area!r}: {e}. Pass a path written by "
            f"download_area, or the bare name of an area shown by list_areas."
        ))]


async def _call_find_hikes(arguments: dict) -> list[TextContent]:
    criteria = _criteria(arguments)
    near_miss = _near_miss(arguments)
    area_path = arguments.get("area")

    name_places = arguments.get("name_places")

    # Facts about the fetch the hikes can't carry (see search.area_has_no_routes); the
    # offline branch reads the same thing straight off the snapshot's own area.
    diagnostics: dict = {}
    # The saved area, on an offline search; `None` on the live path. The two reads
    # after the branch key off THIS rather than off `area_path`, so each is tied to
    # the snapshot it actually has in hand.
    saved: AreaSnapshot | None = None
    # Offline: search a saved snapshot (no network), bbox comes from the snapshot.
    if area_path:
        # A bare name works here too (see _read_area). It is the input an LLM has just
        # read out of `list_areas`, and until this went through the shared helper this
        # tool — alone among the three that take an `area` — raised on it.
        snap = await _read_area(area_path)
        if isinstance(snap, list):
            return snap
        saved = snap
        hikes = await asyncio.to_thread(
            search_snapshot, snap, criteria, _cfg(arguments), near_miss=near_miss,
            name_places=name_places
        )
    else:
        if [k for k in ("south", "west", "north", "east") if k not in arguments]:
            raise ValueError(
                "provide south/west/north/east for a live search, or `area` for an "
                "offline snapshot search."
            )
        bbox = (arguments["south"], arguments["west"], arguments["north"], arguments["east"])
        # search_hikes / compose_loops are synchronous (network + math); run off the loop.
        composing = arguments.get("compose_loops")
        search = compose_loops if composing else search_hikes
        # Naming only applies to ordinary routes — composed loops carry their own label.
        kwargs: dict[str, Any] = {"near_miss": near_miss, "diagnostics": diagnostics}
        if not composing:
            kwargs["name_places"] = name_places
        hikes = await asyncio.to_thread(search, bbox, criteria, _cfg(arguments), **kwargs)

    # Only a saved file can fall short of the cable question, so the caveat is computed on
    # exactly one branch — the same single call site, for the same reason, as the web UI's
    # `web._area_notices`. A live fetch always parses `ferrata_routes`/`ferrata_ways` and
    # member-way tags, and the ferrata clause changed the query TEXT (the Overpass cache
    # key), so a pre-feature response cannot be served under it either: here
    # `ferrata_gap_message` is provably None, and a seam that can never fire reads as one
    # that might. Recomputed rather than plumbed out of `search_snapshot`, which already
    # logs it — a log line reaches a terminal, not a client's reply text.
    caveat = _ferrata_caveat(saved, criteria) if saved is not None else ""

    if not hikes:
        composing = arguments.get("compose_loops") and not area_path
        # When access is required, "nothing" may mean "loops exist, none near a parking/
        # lift" rather than "no loops at all" — say so, matching the CLI/web frontends.
        anchored = composing and (
            arguments.get("car_access") is True or arguments.get("chairlift_access") is True
        )
        if anchored:
            msg = (
                "No loops could be composed reachable from a parking lot / lift in that "
                "area — drop car_access/chairlift_access, or widen the bbox or distance band."
            )
        elif composing:
            msg = (
                "No loops could be composed in that area — try a wider bounding box or a "
                "wider min/max_distance_km band."
            )
        else:
            msg = "No matching hikes found in that area."
        # Outranks the rest: with no route relations in the area, none of the messages
        # above is true — each blames a filter, and nothing was there to filter. Matters
        # more here than in the CLI, since an LLM reading "try a wider bounding box" will
        # dutifully retry, and every retry costs another Overpass request to learn the
        # same thing.
        # Read here rather than after every search: it exists only to word an empty
        # result. Offline it comes straight off the snapshot's own area; live, the search
        # filled it during the fetch that already happened.
        no_routes = (
            area_has_no_routes(saved.area)
            if saved is not None
            else diagnostics.get("no_routes")
        )
        if no_routes:
            msg = no_routes_message()
        # Both can be true at once and both are said: an area with no route relations,
        # asked to FIND cable, is a file that never fetched ferrata objects AND a stretch
        # of map with nothing to filter. The web UI's `_area_notices` produces exactly
        # that pair. (Asked to AVOID cable the same file yields no caveat —
        # `area_ferrata_readable` is vacuously true with no routes — so `no_routes` stands
        # alone, which is what it should do: re-downloading fixes nothing there.)
        return [TextContent(type="text", text=f"{caveat}{msg}")]

    # Optional GPX / GeoJSON serialisation (only when there ARE routes — an empty
    # result returns the helpful text above, more useful than an empty document).
    # Neither carries the caveat: they are documents with nowhere to put prose, and
    # prepending a sentence to a GPX file would make it invalid XML. Same call the web
    # export path makes, and the search that produced the file showed the sentence.
    fmt = arguments.get("format") or "text"
    if fmt == "gpx":
        return [TextContent(type="text", text=hikes_to_gpx(hikes))]
    if fmt == "geojson":
        return [TextContent(type="text", text=hikes_to_geojson(hikes))]
    # Not inside the `if not hikes:` block above, deliberately: a caveat gated on an empty
    # result is the failure the web version was built to avoid, and here it has a concrete
    # shape. Asked to FIND cable, a file that holds member-way tags but never fetched
    # ferrata objects returns the hiking routes whose own members are tagged as cabled —
    # a real, non-empty answer — while the dedicated `route=via_ferrata` relations it
    # never downloaded stay missing from it. The caveat is the part that says the list is
    # short, and a non-empty result is exactly when that is hardest to notice.
    return [TextContent(
        type="text", text=caveat + "\n".join(format_hike(h) for h in hikes)
    )]


async def _call_list_pois(arguments: dict) -> list[TextContent]:
    """The browse mode: what objects are in this area, with no route to any of them."""
    kinds = normalise_kinds(arguments.get("kinds"))  # empty = every registered kind
    area_path = arguments.get("area")
    # Which of the four coverage cases the source is in (see search.snapshot_poi_gap).
    # A live listing is always "ok" — the fetch just happened against this build.
    gap_state: str = "ok"
    # `None` (not `()`) when the file cannot say which kinds it holds — the third
    # answer `snapshot_poi_gap` returns, which the callers below spell `or ()`.
    gap_kinds: tuple[str, ...] | None = ()
    if area_path:
        snap = await _read_area(area_path)
        if isinstance(snap, list):
            return snap
        # Read BEFORE the listing: an empty result from a pre-POI snapshot is not an
        # answer about the landscape, and an LLM client will report it as one unless the
        # difference is spelled out in the text it gets back.
        gap_state, gap_kinds = snapshot_poi_gap(snap, kinds)
        places = await asyncio.to_thread(list_snapshot_pois, snap, kinds)
    else:
        if [k for k in ("south", "west", "north", "east") if k not in arguments]:
            raise ValueError(
                "provide south/west/north/east for a live listing, or `area` for a "
                "downloaded snapshot."
            )
        bbox = (arguments["south"], arguments["west"], arguments["north"], arguments["east"])
        places = await asyncio.to_thread(list_area_pois, bbox, kinds, CFG)

    if not places:
        if gap_state == "none":
            return [TextContent(type="text", text=(
                "That downloaded area carries no points of interest — it was saved before "
                "the feature existed. Re-download it with download_area to browse and "
                "export them offline."
            ))]
        if gap_state == "missing":
            return [TextContent(type="text", text=(
                f"{snapshot_kinds_missing_message(gap_kinds or ())} Re-download it with "
                f"download_area. Do NOT report this as 'there are none there' — nobody "
                f"looked."
            ))]
        if gap_state == "unknown":
            return [TextContent(type="text", text=(
                "That downloaded area does not record which kinds it was saved with, so "
                "this empty result cannot be told apart from a kind nobody looked for. "
                "Re-download it with download_area before concluding there are none there."
            ))]
        listed = ", ".join(kinds) if kinds else "any registered kind"
        return [TextContent(type="text", text=(
            f"Nothing of that kind ({listed}) is mapped in that area — try other kinds, omit "
            f"`kinds` for all of them, or look at a wider area. (A miss means nothing of that "
            f"kind is MAPPED in OSM there, not that nothing is there.)"
        ))]

    # A NON-empty listing can still be missing whole kinds: ask for ruins and trees
    # against a file that never looked for trees and the ruins come back looking like the
    # complete answer. The caveat rides on the text formats below (the machine formats
    # carry the objects themselves, and inventing a wrapper object for them would change
    # a documented export shape over a caveat).
    caveat = (
        snapshot_kinds_missing_message(gap_kinds or ()) + "\n"
        if gap_state == "missing"
        else ""
    )

    fmt = arguments.get("format") or "text"
    if fmt == "gpx":
        return [TextContent(type="text", text=pois_to_gpx(places))]
    if fmt == "geojson":
        return [TextContent(type="text", text=pois_to_geojson(places))]
    if fmt == "json":
        return [TextContent(type="text", text=json.dumps(
            [p.to_dict() for p in places], ensure_ascii=False, indent=2
        ))]
    body = "\n".join(format_poi(p) for p in places)
    return [TextContent(type="text", text=f"{caveat}{format_poi_summary(places)}\n{body}")]


async def _call_list_ferrata(arguments: dict) -> list[TextContent]:
    """The cabled inventory: what via ferrata are here, with no route drawn to any."""
    area_path = arguments.get("area")
    if area_path:
        snap = await _read_area(area_path)
        if isinstance(snap, list):
            return snap
        # Read BEFORE the listing, like the POI gap above: a pre-ferrata file returns an
        # empty tuple, and an LLM client will report that as "there are none there"
        # unless the difference arrives in the text.
        gap = ferrata_gap_message(snap.area, finding=True)
        if gap is not None and not area_records_ferrata(snap.area):
            return [TextContent(type="text", text=(
                f"{gap} Do NOT report this as 'there are no via ferrata there' — "
                f"nobody looked."
            ))]
        lines = await asyncio.to_thread(list_snapshot_ferrata, snap)
    else:
        if [k for k in ("south", "west", "north", "east") if k not in arguments]:
            raise ValueError(
                "provide south/west/north/east for a live listing, or `area` for a "
                "downloaded snapshot."
            )
        bbox = (arguments["south"], arguments["west"], arguments["north"], arguments["east"])
        lines = await asyncio.to_thread(list_area_ferrata, bbox, CFG)

    if not lines:
        return [TextContent(type="text", text=(
            "No via ferrata are mapped in that area. (A miss means nothing is TAGGED as "
            "cabled in OSM there, not that nothing is there — and it is never evidence "
            "that the area's hiking routes are free of cable.)"
        ))]

    if (arguments.get("format") or "text") == "json":
        return [TextContent(type="text", text=json.dumps(
            [
                {
                    "name": f.name,
                    "scale": f.scale,
                    "length_m": round(f.length_m),
                    "start": {"lat": f.start[0], "lon": f.start[1]},
                    "source": f.source,
                }
                for f in lines
            ],
            ensure_ascii=False,
            indent=2,
        ))]
    routes = sum(1 for f in lines if f.source == "route")
    header = (
        f"{len(lines)} cabled line(s): {routes} mapped as a via ferrata route, "
        f"{len(lines) - routes} as individual ways. Grades are raw OSM values on mixed "
        f"scales — do not rank them without checking which scale applies."
    )
    body = "\n".join(f.describe() for f in lines)
    return [TextContent(type="text", text=f"{header}\n{body}")]


async def _call_list_areas(arguments: dict) -> list[TextContent]:
    """"What have I already downloaded?" — the offline inventory, as JSON."""
    areas = await asyncio.to_thread(list_snapshots)
    if not areas:
        return [
            TextContent(
                type="text",
                text="No named areas downloaded yet. Fetch one with download_area, or "
                "search a snapshot you saved elsewhere with find_hikes(area=\"<path>\").",
            )
        ]
    return [TextContent(type="text", text=json.dumps(areas, ensure_ascii=False, indent=2))]


async def _call_download_area(arguments: dict) -> list[TextContent]:
    bbox = (arguments["south"], arguments["west"], arguments["north"], arguments["east"])
    path = arguments["path"]
    name_places = arguments.get("name_places")
    snap = await asyncio.to_thread(download_area, bbox, CFG, name_places=name_places)
    await asyncio.to_thread(save_snapshot, snap, path)
    baked = f", {snap.place_count} baked place name(s)" if name_places else ""
    return [
        TextContent(
            type="text",
            text=(
                f"Saved snapshot to {path}: {snap.route_count} routes, "
                f"{snap.sample_count} elevation samples, {snap.poi_count} points of "
                f"interest{baked}. "
                f"Search it offline with find_hikes(area=\"{path}\")."
            ),
        )
    ]


# Built here, after the handlers exist — mcp 2.x wires them through the constructor.
app = Server("hike-finder", on_list_tools=list_tools, on_call_tool=call_tool)


def main() -> None:
    async def _run():
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
