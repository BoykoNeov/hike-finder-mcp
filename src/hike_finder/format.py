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
    # A composed loop has no single OSM relation — name its constituent trails instead
    # of a (dishonest) relation id, so it always reads as a stitched-together suggestion.
    if h.composed:
        ident = f"composed of {' + '.join(h.composed_of)}" if h.composed_of else "composed loop"
    else:
        ident = f"OSM relation {h.osm_id}"
    return (
        f"{prefix}{h.name} — {h.distance_km} km, {elev} [{', '.join(flags)}] "
        f"(start {h.start[0]:.4f},{h.start[1]:.4f}, {ident}){suffix}"
    )


def hike_to_dict(h: Hike, *, geometry: bool = False) -> dict:
    """JSON-serialisable view of a hike (for CLI --json and the web UI).

    ``geometry=True`` adds a ``geometry`` key — the member ways as ``[lat, lon]``
    polylines (the project's lat/lon order, ready for Leaflet's ``L.polyline``; NOT
    GeoJSON's ``[lon, lat]``). It is opt-in so the default summary stays lean: the CLI
    ``--json`` keeps its compact shape and the web map opts in only when it needs to
    draw the lines.
    """
    d = {
        # A composed loop carries no single OSM relation id — expose None and list its
        # constituent trails in `composed_of` instead.
        "osm_id": None if h.composed else h.osm_id,
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
        "composed": h.composed,
        "composed_of": list(h.composed_of),
    }
    if geometry:
        d["geometry"] = [[[lat, lon] for lat, lon in way] for way in h.ways]
    return d
