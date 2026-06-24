"""Render a measured Hike — shared by every frontend (CLI, web UI, MCP server).

Keeping the one-line summary in one place means the terminal CLI and the MCP
server print *identically*, and the web UI serialises the same fields. No logic
here beyond presentation.
"""
from __future__ import annotations

from .filters import Hike


def format_hike(h: Hike) -> str:
    """The canonical one-line summary of a hike.

    A near-miss is prefixed with ``~`` and gets a trailing ``[near miss: ...]`` clause
    spelling out how it falls short, so it reads as "close, but not a match".
    """
    flags = ["loop" if h.circular else "one-way"]
    if h.car_access:
        flags.append("car")
    if h.chairlift_access:
        flags.append(f"lift:{h.lift_type}")
    if h.gain_m is not None:
        elev = f"+{h.gain_m} m / -{h.loss_m} m"
    else:
        elev = "gain n/a"
    prefix = "~ " if h.near_miss else ""
    suffix = f"  [near miss: {'; '.join(h.notes)}]" if h.near_miss and h.notes else ""
    return (
        f"{prefix}{h.name} — {h.distance_km} km, {elev} [{', '.join(flags)}] "
        f"(start {h.start[0]:.4f},{h.start[1]:.4f}, OSM relation {h.osm_id}){suffix}"
    )


def hike_to_dict(h: Hike) -> dict:
    """JSON-serialisable view of a hike (for CLI --json and the web UI)."""
    return {
        "osm_id": h.osm_id,
        "name": h.name,
        "ref": h.ref,
        "distance_km": h.distance_km,
        "gain_m": h.gain_m,
        "loss_m": h.loss_m,
        "circular": h.circular,
        "car_access": h.car_access,
        "chairlift_access": h.chairlift_access,
        "lift_type": h.lift_type,
        "start": {"lat": h.start[0], "lon": h.start[1]},
        "near_miss": h.near_miss,
        "notes": list(h.notes),
    }
