"""Pure, network-free predicates for the new route filters.

These answer three questions about a route, using only geometry already
fetched from OSM — no extra network calls:

  - is it CIRCULAR (a loop you return to your car on) vs point-to-point?
  - is there CAR access near an endpoint (a mapped parking lot)?
  - is there CHAIRLIFT access near an endpoint (a ride-up aerialway station)?

All of this is cheap (geometry + proximity), so the pipeline runs it BEFORE the
expensive elevation pass and filters on it first — see filters.py.

Honesty note: car/chairlift access is best-effort from OSM completeness. A
"False" means "nothing of that kind is mapped near the route's ends," NOT
"you cannot get there." Loop detection is high-confidence; access is not.

Kept pure and unit-tested, per the project's "pure math is the trust anchor"
convention.
"""
from __future__ import annotations

import math

from .geometry import Coord, haversine_m, route_cycle_count

# Aerialways you can ride UP sitting/standing in a cabin — the realistic
# "let the lift do the climbing" set. Excludes drag/T-bar/platter/rope_tow
# (ski-only) and pylons. The actual type is reported so the broadening is
# never hidden from the user.
RIDE_UP_AERIALWAYS = frozenset({"chair_lift", "gondola", "cable_car", "mixed_lift"})

# OSM truthy/falsy spellings for the roundtrip tag.
_TRUE = {"yes", "true", "1"}
_FALSE = {"no", "false", "0"}


def endpoints_closed(ways: list[list[Coord]], weld_m: float = 1.0) -> bool:
    """True if the member ways enclose at least one loop.

    Delegates to the route's circuit rank over the full vertex graph
    (``geometry.route_cycle_count``): the ways contain a cycle iff
    ``E - V + C > 0``. Stitch-order independent, counts a *lollipop* (a loop
    reached by an approach stem) as closed, and — because nodes are exact shared
    vertices, not endpoints clustered within a tolerance — sees T-junction
    closures while NOT inventing cycles from piled-up endpoints in dense
    relations (the bug that mislabelled linear KČT routes as loops; validated
    live, see HANDOFF). ``weld_m`` is the small same-node tolerance.
    """
    return route_cycle_count(ways, weld_m=weld_m) > 0


def is_circular(
    ways: list[list[Coord]],
    line: list[Coord],
    tags: dict,
    *,
    tol_m: float = 150.0,
    weld_m: float = 1.0,
) -> bool:
    """Decide whether a route is a loop.

    Priority: an explicit ``roundtrip`` tag is authoritative (respects the
    mapper's intent). Otherwise fall back to geometry: the member ways enclose a
    loop (circuit rank), or the stitched line returns to within ``tol_m`` of its
    start (catches a loop left open only by a digitization gap).
    """
    rt = (tags or {}).get("roundtrip", "").strip().lower()
    if rt in _TRUE:
        return True
    if rt in _FALSE:
        return False
    if endpoints_closed(ways, weld_m=weld_m):
        return True
    if len(line) >= 2 and haversine_m(line[0], line[-1]) <= tol_m:
        return True
    return False


def route_endpoints(line: list[Coord]) -> list[Coord]:
    """The points where you'd actually start/finish: the line's two ends.

    For a loop the two ends coincide, so we de-duplicate to a single point.
    """
    if not line:
        return []
    if len(line) == 1 or line[0] == line[-1]:
        return [line[0]]
    return [line[0], line[-1]]


def _bbox_pad(points: list[Coord], radius_m: float):
    """Lat/lon bounds of ``points`` expanded so anything within ``radius_m`` of a
    point is inside. Used to skip features that provably cannot be in range before
    the O(points × features) haversine scan — an EXACT speedup (it only drops
    features too far to ever match), worth it once ``points`` is a whole loop line.

    Longitude padding uses the bbox's worst-case (highest-|lat|) cosine, so the
    box is always a superset and the filter never wrongly drops a real candidate.
    """
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    dlat = radius_m / 111_320.0
    worst_lat = max(abs(min(lats)), abs(max(lats)))
    dlon = radius_m / (111_320.0 * max(0.05, math.cos(math.radians(worst_lat))))
    return (min(lats) - dlat, max(lats) + dlat, min(lons) - dlon, max(lons) + dlon)


def car_accessible(
    endpoints: list[Coord],
    parking: list[dict],
    radius_m: float = 300.0,
) -> bool:
    """True if any mapped parking lot is within ``radius_m`` of an endpoint."""
    if not endpoints or not parking:
        return False
    lo_lat, hi_lat, lo_lon, hi_lon = _bbox_pad(endpoints, radius_m)
    for p in parking:
        plat, plon = p["coord"]
        if not (lo_lat <= plat <= hi_lat and lo_lon <= plon <= hi_lon):
            continue  # outside the radius-padded bbox -> can't be within radius
        if any(haversine_m(e, p["coord"]) <= radius_m for e in endpoints):
            return True
    return False


def chairlift_access(
    endpoints: list[Coord],
    lifts: list[dict],
    radius_m: float = 400.0,
) -> tuple[bool, str | None]:
    """Nearest ride-up aerialway station to an endpoint, within ``radius_m``.

    Returns ``(accessible, kind)`` where ``kind`` is the OSM aerialway value
    of the closest qualifying lift (e.g. ``"chair_lift"``, ``"gondola"``), so
    the output can name what the access actually is.
    """
    if not endpoints or not lifts:
        return (False, None)
    lo_lat, hi_lat, lo_lon, hi_lon = _bbox_pad(endpoints, radius_m)
    best_kind: str | None = None
    best_d = float("inf")
    for lift in lifts:
        for station in lift.get("stations", []):
            slat, slon = station
            if not (lo_lat <= slat <= hi_lat and lo_lon <= slon <= hi_lon):
                continue  # outside the radius-padded bbox -> skip the haversine
            for e in endpoints:
                d = haversine_m(e, station)
                if d <= radius_m and d < best_d:
                    best_d = d
                    best_kind = lift.get("kind")
    return (best_kind is not None, best_kind)


def nearest_parking_m(
    endpoints: list[Coord],
    parking: list[dict],
    max_m: float,
) -> float | None:
    """Distance (m) to the nearest mapped parking within ``max_m`` of an endpoint,
    or ``None`` if none is that close.

    A *measuring* sibling of ``car_accessible`` for the near-miss path: where the
    boolean asks "is parking within the access radius?", this answers "and how far
    is the closest one?" so a result just past the threshold can be reported
    ("parking 380 m away, just past the 300 m limit"). Same radius-padded
    bbox prune (here padded by ``max_m``, the *relaxed* radius) keeps it cheap, and
    it never scans past ``max_m`` so the cost stays bounded on a whole-loop endpoint
    set. Returns the distance only — the boolean verdict stays with ``car_accessible``
    so the live-pinned access predicate is never duplicated/forked.
    """
    if not endpoints or not parking:
        return None
    lo_lat, hi_lat, lo_lon, hi_lon = _bbox_pad(endpoints, max_m)
    best: float | None = None
    for p in parking:
        plat, plon = p["coord"]
        if not (lo_lat <= plat <= hi_lat and lo_lon <= plon <= hi_lon):
            continue
        for e in endpoints:
            d = haversine_m(e, p["coord"])
            if d <= max_m and (best is None or d < best):
                best = d
    return best


def nearest_lift_m(
    endpoints: list[Coord],
    lifts: list[dict],
    max_m: float,
) -> tuple[float | None, str | None]:
    """``(distance, kind)`` of the nearest ride-up station within ``max_m`` of an
    endpoint, or ``(None, None)``. The measuring sibling of ``chairlift_access``
    (see ``nearest_parking_m``)."""
    if not endpoints or not lifts:
        return (None, None)
    lo_lat, hi_lat, lo_lon, hi_lon = _bbox_pad(endpoints, max_m)
    best_d: float | None = None
    best_kind: str | None = None
    for lift in lifts:
        for station in lift.get("stations", []):
            slat, slon = station
            if not (lo_lat <= slat <= hi_lat and lo_lon <= slon <= hi_lon):
                continue
            for e in endpoints:
                d = haversine_m(e, station)
                if d <= max_m and (best_d is None or d < best_d):
                    best_d = d
                    best_kind = lift.get("kind")
    return (best_d, best_kind)


def matched_access_points(
    endpoints: list[Coord],
    parking: list[dict],
    lifts: list[dict],
    *,
    car_radius_m: float = 300.0,
    lift_radius_m: float = 400.0,
) -> list[Coord]:
    """Coordinates of the parking lots / lift stations that actually grant access.

    A feature qualifies when it sits within its access radius of *some* endpoint
    — the **exact same** ``<= radius`` test, with the same car and lift radii,
    that ``car_accessible`` and ``chairlift_access`` use to return their
    booleans. Keeping the predicate byte-identical is the whole point: it
    guarantees ``car_accessible(...) or chairlift_access(...)[0]`` is True iff
    this returns a non-empty list, so the access *verdict* and the points we
    couple a route's start marker to can never silently disagree (the same drift
    hazard the shared ``_vertex_graph`` removed between closure and termini).

    Used by the cheap pass to aim ``start`` at the trailhead that has the access
    (the parking/lift you drive or ride to), instead of an arbitrary route end.
    """
    points: list[Coord] = []
    for p in parking:
        if any(haversine_m(e, p["coord"]) <= car_radius_m for e in endpoints):
            points.append(p["coord"])
    for lift in lifts:
        for station in lift.get("stations", []):
            if any(haversine_m(e, station) <= lift_radius_m for e in endpoints):
                points.append(station)
    return points
