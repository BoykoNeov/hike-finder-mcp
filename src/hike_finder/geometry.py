"""Geometry helpers: distance, polyline assembly, and resampling.

All math here is network-free and fully unit-tested. This is the part of the
pipeline you can trust without an external service.
"""
from __future__ import annotations

import math
from typing import Iterable

# A coordinate is (lat, lon) in degrees.
Coord = tuple[float, float]

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(a: Coord, b: Coord) -> float:
    """Great-circle distance between two (lat, lon) points, in metres."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def polyline_length_m(points: list[Coord]) -> float:
    """Total length of an ordered polyline, in metres."""
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


def stitch_ways(ways: list[list[Coord]]) -> list[Coord]:
    """Join an OSM route relation's member ways into one ordered polyline.

    OSM relation members are not guaranteed to be ordered or consistently
    oriented. This greedily chains ways by matching endpoints, flipping a way
    when its tail (not head) is the nearest continuation. Good enough for v1;
    see HANDOFF.md "Known limitations" for the robust-ordering TODO.
    """
    ways = [w for w in ways if len(w) >= 2]
    if not ways:
        return []

    remaining = ways[:]
    chain = list(remaining.pop(0))

    def near(p: Coord, q: Coord, tol_m: float = 30.0) -> bool:
        return haversine_m(p, q) <= tol_m

    progress = True
    while remaining and progress:
        progress = False
        for i, w in enumerate(remaining):
            head, tail = chain[0], chain[-1]
            w_head, w_tail = w[0], w[-1]
            if near(tail, w_head):
                chain.extend(w[1:])
            elif near(tail, w_tail):
                chain.extend(reversed(w[:-1]))
            elif near(head, w_tail):
                chain[:0] = w[:-1]
            elif near(head, w_head):
                chain[:0] = list(reversed(w))[:-1]
            else:
                continue
            remaining.pop(i)
            progress = True
            break
    return chain


def resample_by_distance(points: list[Coord], interval_m: float = 25.0) -> list[Coord]:
    """Resample a polyline to roughly even spacing.

    Raw OSM vertices are irregularly spaced, which biases elevation gain
    (dense vertices -> more samples -> more counted noise). Resampling to a
    fixed interval makes gain independent of vertex density. This is essential
    for consistent numbers across trails.
    """
    if len(points) < 2:
        return list(points)

    out: list[Coord] = [points[0]]
    carry = 0.0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        seg = haversine_m(a, b)
        if seg == 0:
            continue
        dist = carry
        while dist + interval_m <= seg:
            dist += interval_m
            t = dist / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
        carry = (dist + interval_m) - seg
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out
