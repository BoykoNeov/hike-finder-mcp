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

from .access import car_accessible, chairlift_access, is_circular, route_endpoints
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


def _route_start(line: list[Coord], termini: list[Coord], weld_m: float = 1.0) -> Coord:
    """Pick the start-marker coordinate.

    Keep the stitched line's head when it is already a genuine terminus — true of
    every cleanly connected route, so correct starts never move. Only on a branched
    relation, whose head ``stitch_ways`` can leave mid-route (an interior junction),
    fall through to a deterministic terminus: the smallest by coordinate, so the
    pick is member-order independent. With no termini (a loop) the head is the
    conventional single start point.
    """
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

    # Access + start come from the route's GENUINE termini (degree-1 vertices of
    # the full vertex graph), not the stitched line's two ends. stitch_ways drops
    # members on branched/gap-split relations, so its ends — and line[0] — can fall
    # mid-route and hide a real trailhead's parking/lift. Termini are stitch-order
    # independent and include ends on dropped members. A pure loop (or a fwd+back
    # duplicated route) has no degree-1 vertex; fall back to the stitched ends,
    # preserving today's loop behaviour.
    termini = route_termini(ways)
    endpoints = termini or route_endpoints(line)
    car = car_accessible(endpoints, parking, car_radius_m)
    lift_ok, lift_kind = chairlift_access(endpoints, lifts, lift_radius_m)

    hike = Hike(
        osm_id=route["id"],
        name=route["name"],
        distance_km=round(distance_km, 2),
        circular=circular,
        car_access=car,
        chairlift_access=lift_ok,
        start=_route_start(line, termini),
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
