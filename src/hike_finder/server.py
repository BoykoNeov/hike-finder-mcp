"""MCP server exposing OSM-based hike search with real computed stats.

Tools:
  find_hikes(south, west, north, east, min_gain_m, max_gain_m,
             min_distance_km, max_distance_km,
             circular, car_access, chairlift_access,
             near_misses, area, compose_loops, name_places, format)
  list_pois(south, west, north, east | area, kinds, format)  — list the churches /
             ruins / peaks in an area WITHOUT routing to them. One Overpass call, no
             elevation, no quota; `area` makes it fully offline.
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
from dataclasses import replace

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import config as _config
from .export import hikes_to_geojson, hikes_to_gpx, pois_to_geojson, pois_to_gpx
from .filters import Criteria
from .format import format_hike, format_poi, format_poi_summary
from .poi import POI_KINDS, kind_labels, normalise_kinds
from .search import (
    compose_loops,
    compose_loops_around,
    download_area,
    list_area_pois,
    list_snapshot_pois,
    route_via,
    routes_between,
    routes_to_poi,
    search_hikes,
    search_snapshot,
)
from .snapshot import list_snapshots, load_snapshot, save_snapshot

app = Server("hike-finder")
CFG = _config.load()

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


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
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
            inputSchema={
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
                    **_POI_SCHEMA,
                    "near_misses": {
                        "type": "boolean",
                        "description": "Also return routes that just miss the filters, each "
                        "flagged and annotated with how it falls short. Omit = show them only "
                        "when nothing matches; true = always; false = never.",
                    },
                    "area": {
                        "type": "string",
                        "description": "Path to a snapshot from download_area. When set, the "
                        "search runs OFFLINE against the snapshot and south/west/north/east "
                        "are ignored.",
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
                "from the point — no bounding box needed. Combine with car_access / "
                "chairlift_access to require a parking lot / lift near the loop. Results are "
                "stitched from several trails and have no single OSM relation id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude of the point."},
                    "lon": {"type": "number", "description": "Longitude of the point."},
                    "radius_m": {
                        "type": "number",
                        "description": "How near a loop must pass to the point, metres (default 1000).",
                    },
                    "min_distance_km": {"type": "number"},
                    "max_distance_km": {"type": "number"},
                    "car_access": {
                        "type": "boolean",
                        "description": "true = require parking mapped near the loop.",
                    },
                    "chairlift_access": {
                        "type": "boolean",
                        "description": "true = require a ride-up aerialway near the loop.",
                    },
                    **_POI_SCHEMA,
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
                "derived from the two points — no bounding box needed. `max_distance_km` caps a "
                "route's length; a point more than ~2 km from any trail is treated as off-network "
                "and yields no routes. Results are stitched from several trails (no single OSM id)."
            ),
            inputSchema={
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
                        "description": "Cap a route's length, km (default: 3x the straight-line gap).",
                    },
                    **_POI_SCHEMA,
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
                "are stitched from several trails (no single OSM id)."
            ),
            inputSchema={
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
                        "description": "true = close into a non-retracing circular route (default false).",
                    },
                    "min_distance_km": {"type": "number"},
                    "max_distance_km": {
                        "type": "number",
                        "description": "Drop the linked route if it runs longer than this, km.",
                    },
                    **_POI_SCHEMA,
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
            inputSchema={
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
                        "description": "How many destinations to route to, nearest first (default 3).",
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
                    "min_gain_m": {"type": "number"},
                    "max_gain_m": {"type": "number"},
                    "car_access": {
                        "type": "boolean",
                        "description": "true = require parking mapped near the route's ends.",
                    },
                    **_POI_SCHEMA,
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
            inputSchema={
                "type": "object",
                "properties": {
                    "south": {"type": "number"},
                    "west": {"type": "number"},
                    "north": {"type": "number"},
                    "east": {"type": "number"},
                    "area": {
                        "type": "string",
                        "description": "Path to a snapshot saved by download_area — list what "
                        "is in an already-downloaded area, with zero network. Takes the place "
                        "of the bounding box.",
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
            name="download_area",
            description=(
                "Fetch a bounding box once — its hiking routes plus computed elevation for "
                "every plausible route — and save it to `path`. This spends the elevation "
                "budget up front; afterwards find_hikes(area=path) searches it offline with "
                "no further API calls. Use it to avoid re-hitting the rate-limited elevation "
                "API while exploring an area with different filters."
            ),
            inputSchema={
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
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


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
        poi_kinds=normalise_kinds(arguments.get("poi")),
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


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "find_hikes":
        return await _call_find_hikes(arguments)
    if name == "circular_routes":
        return await _call_circular_routes(arguments)
    if name == "routes_between":
        return await _call_routes_between(arguments)
    if name == "route_via":
        return await _call_route_via(arguments)
    if name == "routes_to_poi":
        return await _call_routes_to_poi(arguments)
    if name == "list_pois":
        return await _call_list_pois(arguments)
    if name == "download_area":
        return await _call_download_area(arguments)
    if name == "list_areas":
        return await _call_list_areas(arguments)
    raise ValueError(f"unknown tool: {name}")


async def _call_circular_routes(arguments: dict) -> list[TextContent]:
    missing = [k for k in ("lat", "lon") if k not in arguments]
    if missing:
        return [TextContent(type="text", text="provide lat and lon for the point to search around.")]
    point = (arguments["lat"], arguments["lon"])
    hikes = await asyncio.to_thread(
        compose_loops_around,
        point,
        _criteria(arguments),
        _cfg(arguments),
        radius_m=arguments.get("radius_m"),
        near_miss=_near_miss(arguments),
    )
    return _serialize(
        hikes,
        arguments.get("format") or "text",
        "No circular routes pass within the radius of your point — widen radius_m, the "
        "min/max_distance_km band, or drop car_access/chairlift_access.",
    )


async def _call_routes_between(arguments: dict) -> list[TextContent]:
    missing = [
        k for k in ("start_lat", "start_lon", "finish_lat", "finish_lon") if k not in arguments
    ]
    if missing:
        return [
            TextContent(
                type="text",
                text="provide start_lat/start_lon and finish_lat/finish_lon for the two points.",
            )
        ]
    start = (arguments["start_lat"], arguments["start_lon"])
    finish = (arguments["finish_lat"], arguments["finish_lon"])
    hikes = await asyncio.to_thread(
        routes_between, start, finish, _criteria(arguments), _cfg(arguments), k=arguments.get("routes")
    )
    return _serialize(
        hikes,
        arguments.get("format") or "text",
        "No routes could be drawn between your two points — they may sit on disconnected "
        "trail networks, be off-network (more than ~2 km from any trail), or every route "
        "exceeds the length cap.",
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
    hikes = await asyncio.to_thread(
        route_via, points, _criteria(arguments), _cfg(arguments), loop=loop
    )
    empty = (
        "No circular route could be drawn through your points — a point may be off-network "
        "(more than ~2 km from any trail) or a leg crosses a gap in the network."
        if loop
        else "No route could be drawn through your points — a point may be off-network (more "
        "than ~2 km from any trail), a leg crosses a gap, or the route falls outside the "
        "min/max_distance_km band."
    )
    return _serialize(hikes, arguments.get("format") or "text", empty)


async def _call_routes_to_poi(arguments: dict) -> list[TextContent]:
    missing = [k for k in ("lat", "lon") if k not in arguments]
    if missing or not arguments.get("kinds"):
        return [
            TextContent(
                type="text",
                text="provide lat and lon for the start point, and `kinds` — what to walk to "
                "(e.g. [\"ruins\"]).",
            )
        ]
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
    )
    # Destination-shaped, never the `poi`-filter wording: nothing was filtered out of an
    # area here, a route to an object could not be drawn.
    return _serialize(
        hikes,
        arguments.get("format") or "text",
        "No route could be drawn to an object of that kind — either nothing of it is mapped "
        "within search_radius_m of your point (widen it), the ones found sit off the trail "
        "network, or every route to them runs past the length cap (raise max_distance_km). "
        "A miss means nothing of that kind is MAPPED in OSM near your point, not that "
        "nothing is there.",
    )


async def _call_find_hikes(arguments: dict) -> list[TextContent]:
    criteria = _criteria(arguments)
    near_miss = _near_miss(arguments)
    area_path = arguments.get("area")

    name_places = arguments.get("name_places")

    # Offline: search a saved snapshot (no network), bbox comes from the snapshot.
    if area_path:
        snap = await asyncio.to_thread(load_snapshot, area_path)
        hikes = await asyncio.to_thread(
            search_snapshot, snap, criteria, _cfg(arguments), near_miss=near_miss,
            name_places=name_places
        )
    else:
        missing = [k for k in ("south", "west", "north", "east") if k not in arguments]
        if missing:
            return [
                TextContent(
                    type="text",
                    text="provide south/west/north/east for a live search, or `area` for an "
                    "offline snapshot search.",
                )
            ]
        bbox = (arguments["south"], arguments["west"], arguments["north"], arguments["east"])
        # search_hikes / compose_loops are synchronous (network + math); run off the loop.
        composing = arguments.get("compose_loops")
        search = compose_loops if composing else search_hikes
        # Naming only applies to ordinary routes — composed loops carry their own label.
        kwargs = {"near_miss": near_miss}
        if not composing:
            kwargs["name_places"] = name_places
        hikes = await asyncio.to_thread(search, bbox, criteria, _cfg(arguments), **kwargs)

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
        return [TextContent(type="text", text=msg)]

    # Optional GPX / GeoJSON serialisation (only when there ARE routes — an empty
    # result returns the helpful text above, more useful than an empty document).
    fmt = arguments.get("format") or "text"
    if fmt == "gpx":
        return [TextContent(type="text", text=hikes_to_gpx(hikes))]
    if fmt == "geojson":
        return [TextContent(type="text", text=hikes_to_geojson(hikes))]
    return [TextContent(type="text", text="\n".join(format_hike(h) for h in hikes))]


async def _call_list_pois(arguments: dict) -> list[TextContent]:
    """The browse mode: what objects are in this area, with no route to any of them."""
    kinds = normalise_kinds(arguments.get("kinds"))  # empty = every registered kind
    area_path = arguments.get("area")
    stale = False
    if area_path:
        snap = await asyncio.to_thread(load_snapshot, area_path)
        # Read BEFORE the listing: an empty result from a pre-POI snapshot is not an
        # answer about the landscape, and an LLM client will report it as one unless the
        # difference is spelled out in the text it gets back.
        stale = not snap.area.pois
        places = await asyncio.to_thread(list_snapshot_pois, snap, kinds)
    else:
        missing = [k for k in ("south", "west", "north", "east") if k not in arguments]
        if missing:
            return [
                TextContent(
                    type="text",
                    text="provide south/west/north/east for a live listing, or `area` for a "
                    "downloaded snapshot.",
                )
            ]
        bbox = (arguments["south"], arguments["west"], arguments["north"], arguments["east"])
        places = await asyncio.to_thread(list_area_pois, bbox, kinds, CFG)

    if not places:
        if stale:
            return [TextContent(type="text", text=(
                f"That downloaded area carries no points of interest — it was saved before "
                f"the feature existed. Re-download it with download_area to browse and "
                f"export them offline."
            ))]
        listed = ", ".join(kinds) if kinds else "any registered kind"
        return [TextContent(type="text", text=(
            f"Nothing of that kind ({listed}) is mapped in that area — try other kinds, omit "
            f"`kinds` for all of them, or look at a wider area. (A miss means nothing of that "
            f"kind is MAPPED in OSM there, not that nothing is there.)"
        ))]

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
    return [TextContent(type="text", text=f"{format_poi_summary(places)}\n{body}")]


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


def main() -> None:
    async def _run():
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
