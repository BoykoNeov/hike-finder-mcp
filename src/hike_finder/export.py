"""Export measured hikes to GPX and GeoJSON — the file you load into a phone / GPS.

A hike finder that can only print a summary stops one step short: the last mile is
handing you a track you can actually follow in the field. This module turns the
measured ``Hike`` list — matched routes AND composed loops, with near-misses included
and flagged — into the two interchange formats every mapping app reads:

  * **GPX 1.1** — one ``<trk>`` per hike (one ``<trkseg>`` per member way), preceded by
    a ``<wpt>`` at each hike's start (the trailhead you drive / ride to). Loads into
    Garmin, Komoot, OsmAnd, Gaia GPS, mapy.cz, ...
  * **GeoJSON** (RFC 7946) — a ``FeatureCollection`` of ``MultiLineString`` features,
    one per hike, with the full measured stats in ``properties``.

Both read ``Hike.ways`` — the RAW member-way geometry, not the stitched line — so the
exported track keeps every leg and matches the reported distance (the stitched line
silently drops unchainable members; see filters.py / geometry.total_way_length_m).

Pure and network-free: no I/O, just string building, so it is unit-tested offline like
the rest of the trustworthy core. Coordinates live as ``(lat, lon)`` everywhere else in
the project; this module is the ONE place that knows GPX wants ``lat="" lon=""``
attributes while GeoJSON wants ``[lon, lat]`` pairs (RFC 7946). Get that swap right here
and every frontend is correct — so the tests pin a known point to a known axis.
"""
from __future__ import annotations

import json
from xml.sax.saxutils import escape, quoteattr

from .filters import Hike
from .format import format_hike, hike_to_dict

GPX_CREATOR = "hike-finder-mcp"

# GPX/GeoJSON media types (RFC 4287-style for GPX; RFC 7946 for GeoJSON).
GPX_MIME = "application/gpx+xml"
GEOJSON_MIME = "application/geo+json"


def _coord(x: float) -> str:
    """A lat/lon as a compact fixed-precision string (~1 cm at 7 decimals)."""
    return f"{x:.7f}"


def _display_name(h: Hike) -> str:
    """The track/waypoint name, marking a near-miss with the same ``~`` prefix every
    other frontend uses.

    GPS track lists and map labels show the NAME, not the description — and GPX has no
    structured near-miss field — so without this a near-miss exported under ``auto`` (when
    nothing matched) would load looking like a clean match. The CLI and web both prefix
    ``~``; the export keeps that contract.
    """
    return ("~ " if h.near_miss else "") + (h.name or "hike")


def hikes_to_gpx(hikes: list[Hike], *, creator: str = GPX_CREATOR) -> str:
    """Serialise hikes as a GPX 1.1 document — one ``<trk>`` per hike.

    Each hike's member ways become ``<trkseg>`` segments, with the canonical one-line
    summary in ``<desc>``; a ``<wpt>`` marks each start. GPX 1.1 fixes element order
    (all ``<wpt>`` before all ``<trk>``), so the waypoints are emitted first. An empty
    hike list yields a valid empty ``<gpx>`` — never a crash or zero bytes.
    """
    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<gpx version="1.1" creator={quoteattr(creator)} '
        'xmlns="http://www.topografix.com/GPX/1/1">',
    ]
    # Start markers first (schema order: wpt* then trk*).
    for h in hikes:
        lat, lon = h.start
        out.append(f'  <wpt lat="{_coord(lat)}" lon="{_coord(lon)}">')
        out.append(f"    <name>{escape(_display_name(h) + ' (start)')}</name>")
        out.append("  </wpt>")
    for h in hikes:
        out.append("  <trk>")
        out.append(f"    <name>{escape(_display_name(h))}</name>")
        out.append(f"    <desc>{escape(format_hike(h))}</desc>")
        for way in h.ways:
            if len(way) < 2:
                continue  # a single-point "way" is not a drawable segment
            out.append("    <trkseg>")
            for lat, lon in way:
                out.append(f'      <trkpt lat="{_coord(lat)}" lon="{_coord(lon)}"/>')
            out.append("    </trkseg>")
        out.append("  </trk>")
    out.append("</gpx>")
    return "\n".join(out) + "\n"


def hike_to_feature(h: Hike) -> dict:
    """One GeoJSON ``Feature`` for a hike: a ``MultiLineString`` of its member ways.

    Coordinates are GeoJSON order — ``[lon, lat]`` (RFC 7946) — NOT the ``(lat, lon)``
    used elsewhere in the project. Properties are the canonical ``hike_to_dict`` view
    (name, distance, gain/loss, shape, access, provenance, near-miss notes). A hike with
    no usable geometry gets ``geometry: null``, which is valid GeoJSON.
    """
    segments = [
        [[lon, lat] for lat, lon in way] for way in h.ways if len(way) >= 2
    ]
    geometry = {"type": "MultiLineString", "coordinates": segments} if segments else None
    return {"type": "Feature", "geometry": geometry, "properties": hike_to_dict(h)}


def hikes_to_geojson(hikes: list[Hike]) -> str:
    """Serialise hikes as a GeoJSON ``FeatureCollection`` — one feature per hike.

    An empty hike list yields an empty (but valid) FeatureCollection.
    """
    fc = {
        "type": "FeatureCollection",
        "features": [hike_to_feature(h) for h in hikes],
    }
    return json.dumps(fc, ensure_ascii=False, indent=2)
