"""MCP server exposing OSM-based hike search with real computed stats.

Tools:
  find_hikes(south, west, north, east, min_gain_m, max_gain_m,
             min_distance_km, max_distance_km,
             circular, car_access, chairlift_access,
             near_misses, area)
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
from .search import compose_loops, download_area, search_hikes, search_snapshot
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


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "find_hikes":
        return await _call_find_hikes(arguments)
    if name == "download_area":
        return await _call_download_area(arguments)
    raise ValueError(f"unknown tool: {name}")


async def _call_find_hikes(arguments: dict) -> list[TextContent]:
    criteria = _criteria(arguments)
    near_miss = _near_miss(arguments)
    area_path = arguments.get("area")

    # Offline: search a saved snapshot (no network), bbox comes from the snapshot.
    if area_path:
        snap = await asyncio.to_thread(load_snapshot, area_path)
        hikes = await asyncio.to_thread(search_snapshot, snap, criteria, CFG, near_miss=near_miss)
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
        search = compose_loops if arguments.get("compose_loops") else search_hikes
        hikes = await asyncio.to_thread(search, bbox, criteria, CFG, near_miss=near_miss)

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
    snap = await asyncio.to_thread(download_area, bbox, CFG)
    await asyncio.to_thread(save_snapshot, snap, path)
    return [
        TextContent(
            type="text",
            text=(
                f"Saved snapshot to {path}: {snap.route_count} routes, "
                f"{snap.sample_count} elevation samples. "
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
