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

from .geometry import Coord, haversine_m

# Aerialways you can ride UP sitting/standing in a cabin — the realistic
# "let the lift do the climbing" set. Excludes drag/T-bar/platter/rope_tow
# (ski-only) and pylons. The actual type is reported so the broadening is
# never hidden from the user.
RIDE_UP_AERIALWAYS = frozenset({"chair_lift", "gondola", "cable_car", "mixed_lift"})

# OSM truthy/falsy spellings for the roundtrip tag.
_TRUE = {"yes", "true", "1"}
_FALSE = {"no", "false", "0"}


def endpoints_closed(ways: list[list[Coord]], snap_m: float = 30.0) -> bool:
    """True if the member ways form a closed structure (a loop).

    Stitch-order independent — unlike comparing the stitched line's first/last
    point, which inherits greedy-stitch fragility (a loop-with-spur can stitch
    to end on the spur tip). We instead look at the *degree* of every way
    endpoint: in a single closed loop every endpoint is shared by an even
    number of way-ends; an open path has exactly two odd-degree termini.
    """
    ends: list[Coord] = []
    for w in ways:
        if len(w) >= 2:
            ends.append(w[0])
            ends.append(w[-1])
    if not ends:
        return False

    # Cluster nearby endpoints (different ways rarely share the exact float).
    clusters: list[list] = []  # each: [representative_coord, count]
    for p in ends:
        for c in clusters:
            if haversine_m(p, c[0]) <= snap_m:
                c[1] += 1
                break
        else:
            clusters.append([p, 1])

    return all(count % 2 == 0 for _, count in clusters)


def is_circular(
    ways: list[list[Coord]],
    line: list[Coord],
    tags: dict,
    *,
    tol_m: float = 150.0,
    snap_m: float = 30.0,
) -> bool:
    """Decide whether a route is a loop.

    Priority: an explicit ``roundtrip`` tag is authoritative (respects the
    mapper's intent). Otherwise fall back to geometry: closed endpoint degree,
    or the stitched line returning to within ``tol_m`` of its start.
    """
    rt = (tags or {}).get("roundtrip", "").strip().lower()
    if rt in _TRUE:
        return True
    if rt in _FALSE:
        return False
    if endpoints_closed(ways, snap_m=snap_m):
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


def car_accessible(
    endpoints: list[Coord],
    parking: list[dict],
    radius_m: float = 300.0,
) -> bool:
    """True if any mapped parking lot is within ``radius_m`` of an endpoint."""
    return any(
        haversine_m(e, p["coord"]) <= radius_m for e in endpoints for p in parking
    )


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
    best_kind: str | None = None
    best_d = float("inf")
    for lift in lifts:
        for station in lift.get("stations", []):
            for e in endpoints:
                d = haversine_m(e, station)
                if d <= radius_m and d < best_d:
                    best_d = d
                    best_kind = lift.get("kind")
    return (best_kind is not None, best_kind)
