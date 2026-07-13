"""MCP server exposing OSM-based hike search with real computed stats.

Tools:
  find_hikes(south, west, north, east, min_gain_m, max_gain_m,
             min_distance_km, max_distance_km,
             circular, car_access, chairlift_access,
             near_misses, area, compose_loops, name_places, format)
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

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import config as _config
from .export import hikes_to_geojson, hikes_to_gpx
from .filters import Criteria
from .format import format_hike
from .search import (
    compose_loops,
    compose_loops_around,
    download_area,
    routes_between,
    search_hikes,
    search_snapshot,
)
from .snapshot import load_snapshot, save_snapshot

app = Server("hike-finder")
CFG = _config.load()


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
    ]


def _near_miss(arguments: dict) -> bool | str:
    """Tri-state from the optional `near_misses` flag: omit -> 'auto'."""
    v = arguments.get("near_misses")
    return "auto" if v is None else v


def _criteria(arguments: dict) -> Criteria:
    return Criteria(
        min_gain_m=arguments.get("min_gain_m"),
        max_gain_m=arguments.get("max_gain_m"),
        min_distance_km=arguments.get("min_distance_km"),
        max_distance_km=arguments.get("max_distance_km"),
        circular=arguments.get("circular"),
        car_access=arguments.get("car_access"),
        chairlift_access=arguments.get("chairlift_access"),
    )


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
    if name == "download_area":
        return await _call_download_area(arguments)
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
        CFG,
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
        routes_between, start, finish, _criteria(arguments), CFG, k=arguments.get("routes")
    )
    return _serialize(
        hikes,
        arguments.get("format") or "text",
        "No routes could be drawn between your two points — they may sit on disconnected "
        "trail networks, be off-network (more than ~2 km from any trail), or every route "
        "exceeds the length cap.",
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
            search_snapshot, snap, criteria, CFG, near_miss=near_miss, name_places=name_places
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
        hikes = await asyncio.to_thread(search, bbox, criteria, CFG, **kwargs)

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
                f"{snap.sample_count} elevation samples{baked}. "
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
