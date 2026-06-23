"""MCP server exposing OSM-based hike search with real computed stats.

Tool: find_hikes(south, west, north, east, min_gain_m, max_gain_m,
                 min_distance_km, max_distance_km,
                 circular, car_access, chairlift_access)

Uses the official `mcp` Python SDK. Run with:  python -m hike_finder.server
(stdio transport — point your MCP client / Claude Code at this command).

NOTE: requires network at runtime (Overpass + elevation). The build sandbox
can't reach those, so this entry point is validated by you on your machine.
The pure-math core it depends on (geometry, gain, access, parsing) is unit-tested.
"""
from __future__ import annotations

import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import config as _config
from .filters import Criteria
from .format import format_hike
from .search import search_hikes

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
                "to get there."
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
                },
                "required": ["south", "west", "north", "east"],
            },
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "find_hikes":
        raise ValueError(f"unknown tool: {name}")

    bbox = (
        arguments["south"],
        arguments["west"],
        arguments["north"],
        arguments["east"],
    )
    criteria = Criteria(
        min_gain_m=arguments.get("min_gain_m"),
        max_gain_m=arguments.get("max_gain_m"),
        min_distance_km=arguments.get("min_distance_km"),
        max_distance_km=arguments.get("max_distance_km"),
        circular=arguments.get("circular"),
        car_access=arguments.get("car_access"),
        chairlift_access=arguments.get("chairlift_access"),
    )

    # search_hikes is synchronous (network + math); run it off the event loop.
    hikes = await asyncio.to_thread(search_hikes, bbox, criteria, CFG)

    if not hikes:
        return [TextContent(type="text", text="No matching hikes found in that area.")]

    return [TextContent(type="text", text="\n".join(format_hike(h) for h in hikes))]


def main() -> None:
    async def _run():
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
