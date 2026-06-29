"""Export measured hikes to GPX and GeoJSON — the file you load into a phone / GPS.

A hike finder that can only print a summary stops one step short: the last mile is
handing you a track you can actually follow in the field. This module turns the
measured ``Hike`` list — matched routes AND composed loops, with near-misses included
and flagged — into the two interchange formats every mapping app reads:

  * **GPX 1.1** — one ``<trk>`` per hike, preceded by a ``<wpt>`` at each hike's start
    (the trailhead you drive / ride to). Loads into Garmin, Komoot, OsmAnd, Gaia GPS,
    mapy.cz, ...
  * **GeoJSON** (RFC 7946) — a ``FeatureCollection`` of ``MultiLineString`` features,
    one per hike, with the full measured stats in ``properties``.

The track geometry comes from one of two sources, in order:

  * **``Hike.track``** when present — the resampled walking-order line with per-point
    elevation (filled by ``filters.add_elevation``). GPX emits this as a single
    ``<trkseg>`` with an ``<ele>`` on every ``<trkpt>``; GeoJSON as one 3D line of
    ``[lon, lat, ele]`` positions (RFC 7946's optional altitude element). This is the
    "single clean track" — one continuous walking line carrying the elevation profile
    behind the reported gain. It exists ONLY when the stitch faithfully covered all
    member ways (see ``filters._stitch_is_faithful``), so it never silently drops legs.
  * **``Hike.ways``** otherwise — the RAW member-way geometry (one ``<trkseg>`` /
    ``MultiLineString`` element per way, no elevation). Used for a fragmented relation
    whose stitch drops members, and whenever elevation was unavailable, so the export
    keeps every leg and matches the reported distance (the stitched line silently drops
    unchainable members; see filters.py / geometry.total_way_length_m).

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


def _ele(x: float) -> str:
    """An elevation in metres as a compact fixed-precision string (GPX/GeoJSON)."""
    return f"{x:.1f}"


def _display_name(h: Hike) -> str:
    """The track/waypoint name, marking a near-miss with the same ``~`` prefix every
    other frontend uses.

    GPS track lists and map labels show the NAME, not the description — and GPX has no
    structured near-miss field — so without this a near-miss exported under ``auto`` (when
    nothing matched) would load looking like a clean match. The CLI and web both prefix
    ``~``; the export keeps that contract.

    Uses the reverse-geocoded ``place_name`` when one was derived (so a ``route/<id>``
    route exports the friendly "Labská → Špindlerův Mlýn" into the GPS, matching what the
    terminal/web show), else the truthful OSM name. The structured GeoJSON properties keep
    BOTH (via ``hike_to_dict``), so provenance isn't lost there.
    """
    return ("~ " if h.near_miss else "") + (h.place_name or h.name or "hike")


def hikes_to_gpx(hikes: list[Hike], *, creator: str = GPX_CREATOR) -> str:
    """Serialise hikes as a GPX 1.1 document — one ``<trk>`` per hike.

    When a hike carries a per-point elevation ``track`` it becomes ONE ``<trkseg>`` with
    an ``<ele>`` on every ``<trkpt>`` (the single clean walking line); otherwise its raw
    member ways each become a ``<trkseg>`` (no elevation). The canonical one-line summary
    rides in ``<desc>`` and a ``<wpt>`` marks each start. GPX 1.1 fixes element order
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
        if h.track:
            # Single clean walking-order track with per-point elevation.
            out.append("    <trkseg>")
            for lat, lon, ele in h.track:
                out.append(
                    f'      <trkpt lat="{_coord(lat)}" lon="{_coord(lon)}">'
                    f"<ele>{_ele(ele)}</ele></trkpt>"
                )
            out.append("    </trkseg>")
        else:
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
    """One GeoJSON ``Feature`` for a hike: a ``MultiLineString`` of its geometry.

    When the hike carries a per-point elevation ``track`` the geometry is a single 3D
    line — ``[lon, lat, ele]`` positions (RFC 7946's optional altitude element) — wrapped
    as a one-element ``MultiLineString`` so the geometry *type* never varies between
    hikes. Otherwise it is the raw member ways, one 2D line each. Coordinates are GeoJSON
    order — ``[lon, lat]`` (RFC 7946) — NOT the ``(lat, lon)`` used elsewhere. Properties
    are the canonical ``hike_to_dict`` view (name, distance, gain/loss, shape, access,
    provenance, near-miss notes). A hike with no usable geometry gets ``geometry: null``,
    which is valid GeoJSON.
    """
    if h.track:
        segments = [[[lon, lat, round(ele, 1)] for lat, lon, ele in h.track]]
    else:
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
