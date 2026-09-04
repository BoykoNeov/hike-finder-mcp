"""Pure, network-free predicates for the new route filters.

These answer three questions about a route, using only geometry already
fetched from OSM — no extra network calls:

  - is it CIRCULAR (a loop you return to your car on) vs point-to-point?
  - is there CAR access near an endpoint (a mapped parking lot)?
  - is there CHAIRLIFT access near an endpoint (a ride-up aerialway station)?
  - is there TRANSIT access near an endpoint (a train/tram/bus stop)?

All of this is cheap (geometry + proximity), so the pipeline runs it BEFORE the
expensive elevation pass and filters on it first — see filters.py.

Honesty note: car/chairlift/transit access is best-effort from OSM completeness. A
"False" means "nothing of that kind is mapped near the route's ends," NOT
"you cannot get there." Loop detection is high-confidence; access is not.

Kept pure and unit-tested, per the project's "pure math is the trust anchor"
convention.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .geometry import Coord, haversine_m, route_cycle_count, total_way_length_m

# Aerialways you can ride UP sitting/standing in a cabin — the realistic
# "let the lift do the climbing" set. Excludes drag/T-bar/platter/rope_tow
# (ski-only) and pylons. The actual type is reported so the broadening is
# never hidden from the user.
RIDE_UP_AERIALWAYS = frozenset({"chair_lift", "gondola", "cable_car", "mixed_lift"})


@dataclass(frozen=True)
class TransitKind:
    """One kind of public-transport stop you could arrive on."""

    key: str  # OSM tag key, e.g. "railway"
    values: tuple[str, ...]  # accepted values for that key
    label: str  # how to say it, e.g. "train station"
    rail: bool  # rail gets the generous radius; see TRANSIT_* defaults below


# The transit registry — ONE table driving both the Overpass selectors
# (``overpass._transit_clauses``) and the classifier (:func:`classify_transit`),
# exactly as ``poi.POI_KINDS`` does for destinations. Written separately they drift,
# and a stop that is *fetchable but unclassifiable* fails as a silently-empty result
# rather than an error.
#
# ``railway=halt`` is the load-bearing entry here, not an afterthought: a Czech
# trailhead is far more often a request-stop *zastávka* than a full station.
#
# Deliberately NOT included: ``public_transport=stop_position``. Those nodes sit on
# the rails/carriageway rather than where you get off, and they duplicate the
# station/halt node that is already matched — including them would double-count every
# stop and pull the measured distance toward the track centre. Don't "fix" this.
TRANSIT_KINDS: dict[str, TransitKind] = {
    "station": TransitKind("railway", ("station",), "train station", True),
    "halt": TransitKind("railway", ("halt",), "train halt", True),
    "tram_stop": TransitKind("railway", ("tram_stop",), "tram stop", False),
    "bus_stop": TransitKind("highway", ("bus_stop",), "bus stop", False),
}

# Two radii, not one, and the asymmetry is the whole point. A station 1 km from the
# trailhead is a genuine way to arrive; a bus stop 1 km away is not much of a fact,
# because `highway=bus_stop` is mapped along essentially every rural road — including
# roads that see three buses a day. One generous radius for both would make
# `transit_access=True` almost free and the filter would stop discriminating.
TRANSIT_RAIL_RADIUS_M = 1000.0
TRANSIT_STOP_RADIUS_M = 400.0


def transit_selectors_by_key() -> dict[str, tuple[str, ...]]:
    """``{tag key: accepted values}`` for the Overpass query, from the registry."""
    by_key: dict[str, tuple[str, ...]] = {}
    for spec in TRANSIT_KINDS.values():
        by_key[spec.key] = by_key.get(spec.key, ()) + spec.values
    return by_key


def classify_transit(tags: dict) -> str | None:
    """The registry key for an element's tags, or None if it isn't a stop we serve."""
    for kind, spec in TRANSIT_KINDS.items():
        if (tags or {}).get(spec.key) in spec.values:
            return kind
    return None


def transit_label(kind: str | None) -> str | None:
    """Human label for a registry key ("halt" -> "train halt")."""
    spec = TRANSIT_KINDS.get(kind or "")
    return spec.label if spec else None


def _transit_radius(kind: str, rail_radius_m: float, stop_radius_m: float) -> float:
    spec = TRANSIT_KINDS.get(kind)
    return rail_radius_m if (spec and spec.rail) else stop_radius_m

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


def closure_limit_m(
    distance_km: float,
    *,
    tol_m: float = 150.0,
    rel_tol: float = 0.05,
) -> float:
    """How far a route's two ends may sit apart and still count as closed.

    The single source of truth for "how close is closed", shared by
    ``is_circular``'s start≈end fallback (which decides the LABEL) and
    ``filters._line_closes`` (which decides whether a loop's gain is trustworthy).
    They were written with the same 150 m constant and drifted anyway — the label
    kept an absolute bound after the gain gate learned to scale — so the two now
    read from here and cannot disagree about the same geometry again.

    The bound is two-sided because neither half works alone. Absolute metres
    cannot separate a 69 m gap on a 0.1 km route (not a loop at all) from the same
    gap on a 10 km one (a digitization seam), so the gap is taken as a FRACTION of
    the route's own length. But an unbounded fraction lets a 20 km loop end a
    kilometre from its start, so ``tol_m`` caps it.

    Note where the two halves cross: ``rel_tol × d ≤ tol_m`` exactly when
    ``d ≤ 3 km`` at the defaults. Above 3 km this IS the old flat 150 m, to the
    metre — the scaling only ever bites on short routes, which is the whole
    population it was added for.
    """
    return min(tol_m, rel_tol * distance_km * 1000.0)


def is_circular(
    ways: list[list[Coord]],
    line: list[Coord],
    tags: dict,
    *,
    tol_m: float = 150.0,
    weld_m: float = 1.0,
    distance_km: float | None = None,
) -> bool:
    """Decide whether a route is a loop.

    Priority: an explicit ``roundtrip`` tag is authoritative (respects the
    mapper's intent). Otherwise fall back to geometry: the member ways enclose a
    loop (circuit rank), or the stitched line returns to within
    ``closure_limit_m`` of its start (catches a loop left open only by a
    digitization gap).

    That last bound used to be a flat ``tol_m``, and on a short route it was far
    too loose: `[M] Labský vodopád` is a 0.1 km route whose line ends 69 m from
    its start, which is most of the route and no kind of loop, yet it was labelled
    one. Worse, the gain gate had ALREADY learned to scale, so the route came out
    as "loop, gain n/a" — two rules disagreeing about one geometry, with the
    wrong one holding the label. Both now call ``closure_limit_m``.

    ``distance_km`` is a CACHE, not a mode. Omitted, it is derived from ``ways``
    by the same sum the caller would pass (``measure_geometry`` has it to hand and
    passes it to save the second walk). Do not "optimize" the default into meaning
    "don't scale": every caller must get the same answer for the same route.
    """
    rt = (tags or {}).get("roundtrip", "").strip().lower()
    if rt in _TRUE:
        return True
    if rt in _FALSE:
        return False
    if endpoints_closed(ways, weld_m=weld_m):
        return True
    if len(line) < 2:
        return False
    if distance_km is None:
        distance_km = total_way_length_m([list(w) for w in ways]) / 1000.0
    return haversine_m(line[0], line[-1]) <= closure_limit_m(distance_km, tol_m=tol_m)


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


def transit_access(
    endpoints: list[Coord],
    transit: list[dict],
    *,
    rail_radius_m: float = TRANSIT_RAIL_RADIUS_M,
    stop_radius_m: float = TRANSIT_STOP_RADIUS_M,
) -> tuple[bool, str | None]:
    """Is there a public-transport stop within reach of an endpoint?

    Returns ``(accessible, kind)`` with ``kind`` a :data:`TRANSIT_KINDS` key, so the
    output can say *which* — "train halt" and "bus stop" are very different promises
    and collapsing them to a bare True would hide that.

    Each stop is tested against the radius for ITS OWN kind (see
    ``TRANSIT_RAIL_RADIUS_M`` / ``TRANSIT_STOP_RADIUS_M``), so the bbox prune is
    padded by the larger of the two — pruning by the smaller would drop a station
    that legitimately qualifies.

    **Rail wins over road when both qualify**, rather than nearest-wins (which is what
    ``chairlift_access`` does, all its candidates being one family). A bus stop 50 m
    away is not the more useful fact when there is also a station 300 m away — the
    question this answers is "can I get here without a car?", and the train is the
    answer a hiker plans around. Within a class, nearest wins.
    """
    if not endpoints or not transit:
        return (False, None)
    lo_lat, hi_lat, lo_lon, hi_lon = _bbox_pad(
        endpoints, max(rail_radius_m, stop_radius_m)
    )
    best_rail: tuple[float, str] | None = None
    best_road: tuple[float, str] | None = None
    for stop in transit:
        # Read as a string up front: `kind` reaches `_transit_radius` and the
        # best-so-far tuples below, and a missing key is not a registered kind
        # anyway (the lookup on the next line drops it).
        kind = str(stop.get("kind") or "")
        spec = TRANSIT_KINDS.get(kind)
        if spec is None:
            continue
        slat, slon = stop["coord"]
        if not (lo_lat <= slat <= hi_lat and lo_lon <= slon <= hi_lon):
            continue
        radius = _transit_radius(kind, rail_radius_m, stop_radius_m)
        for e in endpoints:
            d = haversine_m(e, stop["coord"])
            if d > radius:
                continue
            slot = best_rail if spec.rail else best_road
            if slot is None or d < slot[0]:
                if spec.rail:
                    best_rail = (d, kind)
                else:
                    best_road = (d, kind)
    best = best_rail or best_road
    return (best is not None, best[1] if best else None)


def nearest_transit_m(
    endpoints: list[Coord],
    transit: list[dict],
    max_m: float,
) -> tuple[float | None, str | None]:
    """``(distance, kind)`` of the nearest stop of ANY kind within ``max_m``.

    The measuring sibling of :func:`transit_access` (see :func:`nearest_parking_m`).
    Unlike the boolean it applies ONE relaxed cap to every kind and reports the plain
    nearest: a near-miss note is answering "how close did it come?", where the
    per-kind radius has already been missed and rail-preference would only obscure
    which stop the number refers to.
    """
    if not endpoints or not transit:
        return (None, None)
    lo_lat, hi_lat, lo_lon, hi_lon = _bbox_pad(endpoints, max_m)
    best_d: float | None = None
    best_kind: str | None = None
    for stop in transit:
        slat, slon = stop["coord"]
        if not (lo_lat <= slat <= hi_lat and lo_lon <= slon <= hi_lon):
            continue
        for e in endpoints:
            d = haversine_m(e, stop["coord"])
            if d <= max_m and (best_d is None or d < best_d):
                best_d = d
                best_kind = stop.get("kind")
    return (best_d, best_kind)


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
