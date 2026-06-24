"""Turn raw Overpass data into measured, filtered hike candidates.

This is where the trustworthy local math (geometry + gain + access) meets the
route data to produce real, queryable stats — the whole point of the project.

Two-pass by design (see HANDOFF):

  1. CHEAP pass — stitch, distance, circular, car/chairlift access, over-length
     guard. All geometry, no network. Filter on these first.
  2. EXPENSIVE pass — elevation lookup + gain/loss, run ONLY on routes that
     already survived the cheap filters. Then apply the gain filter.

The cheap-first ordering is what keeps the elevation API from being hammered:
we never pay per-point elevation cost for a route the user already excluded by
distance/shape/access.
"""
from __future__ import annotations

from dataclasses import dataclass

from .access import (
    car_accessible,
    chairlift_access,
    is_circular,
    matched_access_points,
    route_endpoints,
)
from .elevation import ElevationError, ElevationProvider, cumulative_gain_loss
from .geometry import (
    Coord,
    haversine_m,
    resample_by_distance,
    route_termini,
    stitch_ways,
    total_way_length_m,
)
from .overpass import AreaData


@dataclass
class Hike:
    osm_id: int
    name: str
    distance_km: float
    circular: bool
    car_access: bool
    chairlift_access: bool
    start: tuple[float, float]
    gain_m: float | None = None  # filled in the elevation pass (None if unknown)
    loss_m: float | None = None
    lift_type: str | None = None
    ref: str | None = None


@dataclass
class Criteria:
    min_gain_m: float | None = None
    max_gain_m: float | None = None
    max_distance_km: float | None = None
    min_distance_km: float | None = None
    # Tri-state: None = don't care, True = must have, False = must not have.
    circular: bool | None = None
    car_access: bool | None = None
    chairlift_access: bool | None = None

    def accepts_geometry(self, h: Hike) -> bool:
        """Everything decidable from the cheap pass (no elevation)."""
        if self.max_distance_km is not None and h.distance_km > self.max_distance_km:
            return False
        if self.min_distance_km is not None and h.distance_km < self.min_distance_km:
            return False
        if self.circular is not None and h.circular != self.circular:
            return False
        if self.car_access is not None and h.car_access != self.car_access:
            return False
        if self.chairlift_access is not None and h.chairlift_access != self.chairlift_access:
            return False
        return True

    def accepts_gain(self, h: Hike) -> bool:
        """Gain bounds. A route with unknown gain fails an active gain bound."""
        if self.min_gain_m is not None and (h.gain_m is None or h.gain_m < self.min_gain_m):
            return False
        if self.max_gain_m is not None and (h.gain_m is None or h.gain_m > self.max_gain_m):
            return False
        return True


def _route_start(
    line: list[Coord],
    termini: list[Coord],
    access_points: list[Coord] = (),
    weld_m: float = 1.0,
) -> Coord:
    """Pick the start-marker coordinate.

    When the route has matched access (a parking lot or lift station near one of
    its ends) AND genuine termini, start at the terminus nearest a matched access
    feature — so the marker lands on the trailhead you actually drive or ride to,
    not an arbitrary geometric end. Ties break by coordinate, keeping the pick
    member-order independent.

    Otherwise (no access matched, or a pure loop with no degree-1 vertex) keep the
    stitched line's head when it is already a genuine terminus — true of every
    cleanly connected route, so correct starts never move. Only on a branched
    relation, whose head ``stitch_ways`` can leave mid-route (an interior junction),
    fall through to a deterministic terminus: the smallest by coordinate, so the
    pick is member-order independent. With no termini (a loop) the head is the
    conventional single start point.
    """
    if termini and access_points:
        return min(
            termini,
            key=lambda t: (min(haversine_m(t, ap) for ap in access_points), t),
        )
    head = line[0]
    if not termini or any(haversine_m(head, t) <= weld_m for t in termini):
        return head
    return min(termini)


def measure_geometry(
    route: dict,
    parking: list[dict],
    lifts: list[dict],
    *,
    loop_tolerance_m: float = 150.0,
    car_radius_m: float = 300.0,
    lift_radius_m: float = 400.0,
) -> tuple[Hike, list[Coord]] | None:
    """Cheap pass: distance, shape, and access. Returns (hike, stitched line)."""
    line = stitch_ways(route["ways"])
    if len(line) < 2:
        return None
    ways = route["ways"]

    # Distance sums the member ways directly, NOT the stitched line: stitch_ways
    # drops members it can't chain (branched/gap-split relations), so the line
    # under-counts. The stitched line is still used for the is_circular gap
    # fallback and as the loop start fallback.
    distance_km = total_way_length_m(ways) / 1000.0
    circular = is_circular(ways, line, route.get("tags", {}), tol_m=loop_tolerance_m)

    # Termini are the route's genuine open ends (degree-1 vertices of the full
    # vertex graph). They drive the START marker's access coupling below — the
    # trailhead you reach by car/lift. They also matter for access on branched/
    # gap-split relations: stitch_ways drops members it can't chain, so the
    # stitched line's two ends alone can fall mid-route and hide a real trailhead's
    # parking/lift on a dropped member; the termini recover it.
    termini = route_termini(ways)
    endpoints = list(dict.fromkeys(termini + route_endpoints(line)))

    # The car/lift BOOLEANS test a wider point set than `endpoints` on a LOOP: a
    # loop has no real "end", so its stitched ends are arbitrary points on the ring
    # and a lift or parking elsewhere on the loop (the common case — you ride a lift
    # the loop merely passes) would be missed by an ends-only test. For a circular
    # route we therefore test proximity along the WHOLE line, still UNIONed with the
    # termini so a feature at a terminus on a dropped member is not lost. The set is
    # a strict superset of `endpoints`, so it is recall-monotonic — it can only add
    # access hits, never remove one — and a point-to-point route is unchanged (its
    # access_pts collapse back to `endpoints`). NB the switch is `circular`, not
    # `termini`: lollipops and gap-closed loops HAVE termini yet are exactly where
    # the ends-only test misses a lift on the ring.
    access_pts = list(
        dict.fromkeys(termini + (line if circular else route_endpoints(line)))
    )
    car = car_accessible(access_pts, parking, car_radius_m)
    lift_ok, lift_kind = chairlift_access(access_pts, lifts, lift_radius_m)

    # Couple the start marker to the access result: aim it at the terminus
    # nearest a parking lot / lift station that actually granted access, so a
    # route's `start` points at the trailhead you drive or ride to. This uses
    # `endpoints` (the genuine ends), NOT the loop-widened `access_pts`: the start
    # belongs at a real trailhead, not an arbitrary mid-loop point. On a
    # point-to-point route `access_pts == endpoints`, so the matched features share
    # the booleans' exact `<= radius` predicate and verdict and start can't
    # disagree. On a loop the booleans may also fire on a mid-loop feature the start
    # won't couple to — harmless, since a loop's start is arbitrary anyway (and this
    # only fires for routes WITH termini: a pure loop's start stays at the head).
    access_points = matched_access_points(
        endpoints, parking, lifts, car_radius_m=car_radius_m, lift_radius_m=lift_radius_m
    )

    hike = Hike(
        osm_id=route["id"],
        name=route["name"],
        distance_km=round(distance_km, 2),
        circular=circular,
        car_access=car,
        chairlift_access=lift_ok,
        start=_route_start(line, termini, access_points),
        lift_type=lift_kind,
        ref=route.get("ref"),
    )
    return hike, line


def add_elevation(
    hike: Hike,
    line: list[Coord],
    elevation: ElevationProvider,
    *,
    sample_interval_m: float = 25.0,
    gain_threshold_m: float = 10.0,
    smooth_window: int = 3,
) -> None:
    """Expensive pass: fill gain/loss in place. Leaves them None on failure."""
    sampled = resample_by_distance(line, sample_interval_m)
    try:
        elevations = elevation.lookup(sampled)
    except ElevationError:
        return  # gain/loss stay None; the route is still listed unless gain-filtered
    gain, loss = cumulative_gain_loss(
        elevations, threshold_m=gain_threshold_m, smooth_window=smooth_window
    )
    hike.gain_m = round(gain)
    hike.loss_m = round(loss)


def find_hikes(
    area: AreaData,
    elevation: ElevationProvider,
    criteria: Criteria,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    max_route_factor: float = 4.0,
    sample_interval_m: float = 25.0,
    gain_threshold_m: float = 10.0,
    smooth_window: int = 3,
    loop_tolerance_m: float = 150.0,
    car_radius_m: float = 300.0,
    lift_radius_m: float = 400.0,
) -> list[Hike]:
    # Over-length guard: a through-route (e.g. a national trail) intersecting the
    # bbox comes back with its FULL geometry, so its length and endpoints belong
    # to another region. Drop anything much longer than the query area itself.
    max_len_m: float | None = None
    if bbox is not None:
        south, west, north, east = bbox
        diagonal_m = haversine_m((south, west), (north, east))
        max_len_m = diagonal_m * max_route_factor

    # Cheap pass + cheap filters.
    survivors: list[tuple[Hike, list[Coord]]] = []
    for r in area.routes:
        measured = measure_geometry(
            r,
            area.parking,
            area.lifts,
            loop_tolerance_m=loop_tolerance_m,
            car_radius_m=car_radius_m,
            lift_radius_m=lift_radius_m,
        )
        if measured is None:
            continue
        hike, line = measured
        if max_len_m is not None and hike.distance_km * 1000.0 > max_len_m:
            continue
        if not criteria.accepts_geometry(hike):
            continue
        survivors.append((hike, line))

    # Expensive pass — only for routes that already match the cheap criteria.
    for hike, line in survivors:
        add_elevation(
            hike,
            line,
            elevation,
            sample_interval_m=sample_interval_m,
            gain_threshold_m=gain_threshold_m,
            smooth_window=smooth_window,
        )

    hikes = [h for h, _ in survivors if criteria.accepts_gain(h)]
    hikes.sort(key=lambda h: (h.gain_m if h.gain_m is not None else -1.0), reverse=True)
    return hikes
