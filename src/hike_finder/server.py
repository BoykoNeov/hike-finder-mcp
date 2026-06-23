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
from .elevation import get_provider
from .filters import Criteria, Hike, find_hikes
from .overpass import fetch_area

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


def _format(h: Hike) -> str:
    flags = ["loop" if h.circular else "one-way"]
    if h.car_access:
        flags.append("car")
    if h.chairlift_access:
        flags.append(f"lift:{h.lift_type}")
    if h.gain_m is not None:
        elev = f"+{h.gain_m} m / -{h.loss_m} m"
    else:
        elev = "gain n/a"
    return (
        f"{h.name} — {h.distance_km} km, {elev} [{', '.join(flags)}] "
        f"(start {h.start[0]:.4f},{h.start[1]:.4f}, OSM relation {h.osm_id})"
    )


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

    area = await asyncio.to_thread(
        fetch_area,
        *bbox,
        CFG.overpass_url or "https://overpass-api.de/api/interpreter",
        user_agent=CFG.overpass_user_agent,
    )

    provider = get_provider(
        mode=CFG.elevation_mode,
        dem_dir=CFG.dem_dir,
        api_endpoint=CFG.api_endpoint,
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

    hikes = await asyncio.to_thread(
        find_hikes,
        area,
        provider,
        criteria,
        bbox=bbox,
        max_route_factor=CFG.max_route_factor,
        sample_interval_m=CFG.sample_interval_m,
        gain_threshold_m=CFG.gain_threshold_m,
        smooth_window=CFG.smooth_window,
        loop_tolerance_m=CFG.loop_tolerance_m,
        car_radius_m=CFG.car_radius_m,
        lift_radius_m=CFG.lift_radius_m,
    )

    if not hikes:
        return [TextContent(type="text", text="No matching hikes found in that area.")]

    return [TextContent(type="text", text="\n".join(_format(h) for h in hikes))]


def main() -> None:
    async def _run():
        async with stdio_server() as (read, write):
            await app.run(read, write, app.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
