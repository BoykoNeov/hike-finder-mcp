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
    TRANSIT_RAIL_RADIUS_M,
    TRANSIT_STOP_RADIUS_M,
    car_accessible,
    chairlift_access,
    is_circular,
    matched_access_points,
    nearest_lift_m,
    nearest_parking_m,
    nearest_transit_m,
    route_endpoints,
    transit_access,
)
from .elevation import ElevationError, ElevationProvider, cumulative_gain_loss
from .geometry import (
    Coord,
    haversine_m,
    polyline_length_m,
    resample_by_distance,
    route_termini,
    stitch_ways,
    total_way_length_m,
)
from .overpass import AreaData
from .poi import PoiHit, PoiIndex, route_pois
from .surface import SurfaceSummary, summarise_surface, summarise_tracktype


def _within(d: float | None, radius_m: float) -> bool:
    """True if a measured distance exists and is within ``radius_m``."""
    return d is not None and d <= radius_m


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
    # Public-transport access. The ONLY access field that is `bool | None` rather than
    # `bool`, because it is the only one that postdates existing snapshots: `None` means
    # "this area's data never recorded transit", which is a different claim from False
    # ("nothing is mapped near the ends"). Answering a transit filter from a v1 snapshot
    # would confidently label every route "no transit access" — and around here that
    # would be absurd, since a Czech trailhead is very often a railway halt. `Criteria`
    # therefore refuses the question rather than guessing; see `accepts_geometry`.
    transit_access: bool | None = None
    transit_type: str | None = None  # which kind granted it (access.TRANSIT_KINDS key)
    transit_distance_m: float | None = None  # near-miss measurement, like car/lift
    # What you walk ON (see surface.py): length-weighted `surface` / `tracktype`
    # breakdowns over the member ways, each carrying the fraction of the route the
    # answer covers. `None` — not an empty summary — when the area data never fetched
    # member-way tags (a snapshot predating the feature), so "we didn't look" stays
    # distinct from "nothing is tagged". Report-only: nothing filters on these, which
    # is why they need no Criteria tri-state the way transit_access does.
    surface: SurfaceSummary | None = None
    tracktype: SurfaceSummary | None = None
    # Near-miss annotation. `near_miss` marks a route that does NOT meet the strict
    # criteria but is within tolerance of them; `notes` says exactly how it misses
    # ("gain 720 m — 80 m below the 800 m minimum"), so it is never mistaken for a
    # match. Both stay at their defaults on a true match. The two *_distance_m
    # fields are the nearest mapped parking / lift station to an access point (the
    # measured input behind an access near-miss), computed only when near-miss is
    # engaged — None otherwise.
    near_miss: bool = False
    notes: tuple[str, ...] = ()
    car_distance_m: float | None = None
    lift_distance_m: float | None = None
    # Composed-loop provenance. A composed loop is synthesised from several connected
    # marked trails (see compose.py), so it has NO single OSM relation id — `composed`
    # flags it and `composed_of` lists the constituent trail refs/colours, which the
    # renderer shows instead of a (dishonest) single relation id. Default off for every
    # ordinary route.
    composed: bool = False
    composed_of: tuple[str, ...] = ()
    # Route geometry: the member ways as ordered (lat, lon) polylines, exactly as
    # mapped (a composed loop carries its single synthesised ring). This is the RAW
    # member-way geometry, NOT the stitched line — `stitch_ways` silently drops members
    # it can't chain, so the stitched line under-represents a branched/gap-split relation
    # exactly as it under-counts distance (see geometry.total_way_length_m). The GPX /
    # GeoJSON export and the web-map draw read this; it is deliberately left out of the
    # one-line summary and the default `hike_to_dict`, so ordinary output is byte-for-byte
    # unchanged. Default empty so every Hike construction that predates export keeps working.
    ways: tuple[tuple[Coord, ...], ...] = ()
    # Reverse-geocode naming (opt-in, see naming.py / search.name_places). `unnamed`
    # is the truthful "no signed name/ref in OSM" signal carried from overpass.parse_area
    # — NOT reconstructed from the `route/<id>` fallback string. `place_name` is a
    # DERIVED label built from the place names at the route's ends (e.g. "Pec → Sněžka");
    # it never overwrites the honest `name`/`ref`, and the renderer marks the route as
    # unnamed so a geocoded label is never mistaken for a signed trail name. Both default
    # off so every prior Hike construction and output stays byte-for-byte unchanged.
    unnamed: bool = False
    place_name: str | None = None
    # Per-point elevation track — the "single clean track" for GPS export. The
    # walking-order resampled line zipped with its sampled elevations, as
    # (lat, lon, ele_m). Filled by `add_elevation` ONLY when the elevation lookup
    # succeeded AND the stitched walking line faithfully covers all member ways (see
    # `_stitch_is_faithful`), so a fragmented relation whose stitch drops legs is
    # NEVER exported as a track missing them — it falls back to the raw-`ways`
    # multi-segment export (full geometry, no `<ele>`). A composed loop is a single
    # synthesised ring, so it is faithful by construction. Like `ways`, it is left out
    # of `hike_to_dict` / `format_hike`, so ordinary output is byte-for-byte unchanged.
    # Default empty so every Hike construction that predates it keeps working.
    track: tuple[tuple[float, float, float], ...] = ()
    # Points of interest this route passes (see poi.py), nearest first, each with the
    # measured distance from the route's geometry. Filled by the CHEAP pass only when a
    # POI filter is active — so the ordinary search costs nothing extra — and it is what
    # `Criteria.poi_kinds` filters on. Empty on every non-POI search.
    pois: tuple[PoiHit, ...] = ()
    # The object this route was DRAWN TO (see search.routes_to_poi), as opposed to the
    # objects in `pois`, which it merely passes. Kept in its own field precisely because
    # the two are different claims: `pois` is a filter's evidence, this is the route's
    # reason for existing. Its `distance_m` is how far the route's end sits from the
    # object — the route ends at the nearest point ON THE TRAIL NETWORK, which is not
    # the object itself, and the renderer says "ends N m from" rather than "arrives at"
    # so that gap is never papered over. None on every other kind of search.
    destination: PoiHit | None = None


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
    transit_access: bool | None = None
    # Destination filter (see poi.py): keep only routes that pass within the POI radius
    # of an object of one of these registered kinds ("a hike that goes to a ruin").
    # Empty = don't care, which is the default, so every existing search is unchanged.
    # Several kinds are OR-ed — "a church OR a ruin" — because that is what picking two
    # entries from a list means to a user; AND-ing several would almost always return
    # nothing. Which objects were actually reached is reported in `Hike.pois`.
    poi_kinds: tuple[str, ...] = ()

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
        if self.transit_access is not None:
            # Unknown fails an active filter — the same rule `accepts_gain` applies to a
            # route with no measured gain. A pre-transit snapshot yields None here, and
            # dropping the route is the honest answer: we cannot say either way, so the
            # search comes back empty (and says why) instead of returning routes labelled
            # with an access verdict nothing ever measured.
            if h.transit_access is None or h.transit_access != self.transit_access:
                return False
        if self.poi_kinds and not h.pois:
            return False
        return True

    def accepts_gain(self, h: Hike) -> bool:
        """Gain bounds. A route with unknown gain fails an active gain bound."""
        if self.min_gain_m is not None and (h.gain_m is None or h.gain_m < self.min_gain_m):
            return False
        if self.max_gain_m is not None and (h.gain_m is None or h.gain_m > self.max_gain_m):
            return False
        return True

    def accepts_geometry_relaxed(
        self,
        h: Hike,
        *,
        dist_km_margin: float,
        radius_frac: float,
        car_radius_m: float,
        lift_radius_m: float,
        transit_radius_m: float = TRANSIT_RAIL_RADIUS_M,
    ) -> bool:
        """The widened cheap-pass gate that admits a route into the near-miss pool.

        Same shape as ``accepts_geometry`` but tolerant on the dimensions where
        "close" is meaningful, so a route just outside the strict cut still earns an
        elevation lookup and a chance to be reported as a near-miss:

          - distance bounds are widened by ``dist_km_margin`` (km);
          - a *required* car/lift access (``True``) is satisfied by a feature within
            the *relaxed* radius ``radius * (1 + radius_frac)`` — measured via
            ``h.*_distance_m`` (populated only when near-miss is engaged), so a
            parking lot just past the limit still counts;
          - ``circular`` is NOT relaxed: a loop is not "almost point-to-point", and
            relaxing shape would surface wrong-shape routes mislabelled "close";
          - ``poi_kinds`` is NOT relaxed either, but for a different reason than shape:
            the POI radius is itself a *per-search user knob* (``--poi-radius``), unlike
            the car/lift radii, which are fixed config a user is not expected to tune
            per search. Someone who wants "within 600 m of a ruin" says so directly, so
            an invisible second tolerance on top would only blur what they asked for;
          - an *excluded* access (``False``) stays strict — "almost excluded" is not
            a useful near-miss.
        """
        if self.max_distance_km is not None and h.distance_km > self.max_distance_km + dist_km_margin:
            return False
        if self.min_distance_km is not None and h.distance_km < self.min_distance_km - dist_km_margin:
            return False
        if self.circular is not None and h.circular != self.circular:
            return False
        if self.car_access is True:
            if not _within(h.car_distance_m, car_radius_m * (1 + radius_frac)):
                return False
        elif self.car_access is False and h.car_access:
            return False
        if self.chairlift_access is True:
            if not _within(h.lift_distance_m, lift_radius_m * (1 + radius_frac)):
                return False
        elif self.chairlift_access is False and h.chairlift_access:
            return False
        if self.transit_access is True:
            # Relaxed against the RAIL radius, the larger of the two, matching the
            # `transit_max_m` the measurement was capped at. An unknown route measures
            # None here, so `_within` rejects it — the strict gate's "refuse to answer
            # from data nobody gathered" survives into the near-miss pool rather than
            # being quietly relaxed away.
            if not _within(h.transit_distance_m, transit_radius_m * (1 + radius_frac)):
                return False
        elif self.transit_access is False and h.transit_access:
            return False
        if self.poi_kinds and not h.pois:
            return False
        return True

    def near_miss_notes(
        self,
        h: Hike,
        *,
        gain_frac: float,
        car_radius_m: float,
        lift_radius_m: float,
        transit_radius_m: float = TRANSIT_RAIL_RADIUS_M,
    ) -> tuple[str, ...] | None:
        """Human notes for each strict criterion ``h`` misses but stays close to —
        or ``None`` if it misses one too hard to call a near-miss.

        Only ever called on a route that already cleared ``accepts_geometry_relaxed``
        (so its distance/access misses are within tolerance by construction) but is
        not a strict match. The one dimension that can still be a HARD miss here is
        gain — it is not part of the cheap gate — so a gain shortfall beyond
        ``gain_frac`` of the bound, or an unknown gain against an active gain bound,
        returns ``None`` (drop). Every emitted note states the measured value and the
        gap, so a near-miss can never be read as a match.
        """
        notes: list[str] = []
        if self.min_gain_m is not None:
            if h.gain_m is None:
                return None
            if h.gain_m < self.min_gain_m:
                short = self.min_gain_m - h.gain_m
                if short > self.min_gain_m * gain_frac:
                    return None
                notes.append(
                    f"gain {round(h.gain_m)} m — {round(short)} m below the "
                    f"{round(self.min_gain_m)} m minimum"
                )
        if self.max_gain_m is not None:
            if h.gain_m is None:
                return None
            if h.gain_m > self.max_gain_m:
                over = h.gain_m - self.max_gain_m
                if over > self.max_gain_m * gain_frac:
                    return None
                notes.append(
                    f"gain {round(h.gain_m)} m — {round(over)} m above the "
                    f"{round(self.max_gain_m)} m maximum"
                )
        if self.min_distance_km is not None and h.distance_km < self.min_distance_km:
            short = self.min_distance_km - h.distance_km
            notes.append(
                f"{h.distance_km} km — {round(short, 2)} km below the "
                f"{self.min_distance_km} km minimum"
            )
        if self.max_distance_km is not None and h.distance_km > self.max_distance_km:
            over = h.distance_km - self.max_distance_km
            notes.append(
                f"{h.distance_km} km — {round(over, 2)} km above the "
                f"{self.max_distance_km} km maximum"
            )
        if self.car_access is True and not h.car_access and h.car_distance_m is not None:
            notes.append(
                f"nearest parking {round(h.car_distance_m)} m away — "
                f"just past the {round(car_radius_m)} m limit"
            )
        if self.chairlift_access is True and not h.chairlift_access and h.lift_distance_m is not None:
            notes.append(
                f"nearest lift {round(h.lift_distance_m)} m away — "
                f"just past the {round(lift_radius_m)} m limit"
            )
        if (
            self.transit_access is True
            and not h.transit_access
            and h.transit_distance_m is not None
        ):
            notes.append(
                f"nearest public transport {round(h.transit_distance_m)} m away — "
                f"just past the {round(transit_radius_m)} m limit"
            )
        return tuple(notes) if notes else None


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
    transit: list[dict] | None = None,
    loop_tolerance_m: float = 150.0,
    car_radius_m: float = 300.0,
    lift_radius_m: float = 400.0,
    transit_rail_radius_m: float = TRANSIT_RAIL_RADIUS_M,
    transit_stop_radius_m: float = TRANSIT_STOP_RADIUS_M,
    car_max_m: float | None = None,
    lift_max_m: float | None = None,
    transit_max_m: float | None = None,
    poi_index: PoiIndex | None = None,
    poi_kinds: tuple[str, ...] = (),
    poi_radius_m: float = 250.0,
) -> tuple[Hike, list[Coord]] | None:
    """Cheap pass: distance, shape, access, and reached points of interest.
    Returns (hike, stitched line).

    When ``car_max_m`` / ``lift_max_m`` are given (the near-miss path), also records
    the nearest mapped parking / lift station to an access point — capped at those
    relaxed radii — onto the Hike, so a feature just past the strict limit can later
    be reported as a near-miss. They default to ``None`` (no measurement, no extra
    cost) so the ordinary search path is byte-for-byte unchanged.

    ``poi_index`` + ``poi_kinds`` (both supplied only when a POI filter is active)
    likewise record which registered objects — churches, ruins, peaks — the route passes
    within ``poi_radius_m`` of. Absent them the POI scan does not run at all, so a
    non-POI search pays nothing.
    """
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

    # `transit is None` means the area data never recorded stops (a pre-transit
    # snapshot) — propagate the not-known state rather than collapsing it to False.
    # An empty LIST is a real answer ("none mapped here") and gives False as usual.
    if transit is None:
        transit_ok: bool | None = None
        transit_kind: str | None = None
    else:
        transit_ok, transit_kind = transit_access(
            access_pts,
            transit,
            rail_radius_m=transit_rail_radius_m,
            stop_radius_m=transit_stop_radius_m,
        )

    # Near-miss measurement (only when a relaxed cap is supplied): how far is the
    # closest parking / lift, even if it sits just beyond the access radius? Measured
    # against the SAME access_pts the booleans use, so "within radius" and "nearest
    # distance" can never disagree about which feature is closest.
    car_distance_m = (
        nearest_parking_m(access_pts, parking, car_max_m) if car_max_m is not None else None
    )
    lift_distance_m = (
        nearest_lift_m(access_pts, lifts, lift_max_m)[0] if lift_max_m is not None else None
    )
    transit_distance_m = (
        nearest_transit_m(access_pts, transit, transit_max_m)[0]
        if transit_max_m is not None and transit
        else None
    )

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

    # Reached points of interest, measured against the RAW member ways — not the stitched
    # line — for the same reason distance sums the members: a church beside a member
    # `stitch_ways` couldn't chain is still a church you walk past. `route_pois` measures
    # to the line itself, so a straight member mapped with two far-apart nodes still
    # reports the true closest approach.
    # Surface/tracktype, length-weighted over the member ways. `way_tags` is parallel
    # to `ways`; an EMPTY list means the data predates the member-tag fetch, so the
    # summaries stay None rather than claiming a fully untagged route.
    way_tags = route.get("way_tags") or []
    if way_tags:
        members = list(zip(ways, way_tags))
        surface_summary = summarise_surface(members)
        tracktype_summary = summarise_tracktype(members)
    else:
        surface_summary = tracktype_summary = None

    pois: tuple[PoiHit, ...] = ()
    if poi_index is not None and poi_kinds:
        pois = route_pois(ways, poi_index, poi_kinds, poi_radius_m)

    hike = Hike(
        osm_id=route["id"],
        name=route["name"],
        distance_km=round(distance_km, 2),
        circular=circular,
        car_access=car,
        chairlift_access=lift_ok,
        start=_route_start(line, termini, access_points),
        lift_type=lift_kind,
        transit_access=transit_ok,
        transit_type=transit_kind,
        ref=route.get("ref"),
        car_distance_m=car_distance_m,
        lift_distance_m=lift_distance_m,
        transit_distance_m=transit_distance_m,
        surface=surface_summary,
        tracktype=tracktype_summary,
        # Carry the raw member-way geometry for export / map draw (immutable copy).
        ways=tuple(tuple(w) for w in ways),
        # Truthful "no signed name/ref" flag from the parser (default False for the
        # synthetic compose routes, which carry their own provenance instead).
        unnamed=bool(route.get("unnamed", False)),
        pois=pois,
    )
    return hike, line


def _stitch_is_faithful(line: list[Coord], ways, *, rel_tol: float = 0.02) -> bool:
    """True if the stitched walking ``line`` covers (within ``rel_tol``) all member-way
    length — i.e. ``stitch_ways`` dropped no meaningful leg.

    The elevation track is sampled along the stitched ``line``, but ``stitch_ways``
    greedily drops members it can't chain (branched / gap-split relations), so on such
    a route the stitched line — and any track built from it — omits whole legs. We
    export the single elevated track only when it is faithful; otherwise we keep the
    raw-``ways`` multi-segment export, which drops nothing. Clean linear routes (the
    common case) pass comfortably (stitched ≈ summed, the bridge gaps stitch adds are
    negligible); the fragmented relations that recover length via ``total_way_length_m``
    are exactly the ones this rejects (they dropped large fractions — 36/70, 19/31 of
    members in the live fixtures).
    """
    summed = total_way_length_m([list(w) for w in ways])
    if summed <= 0:
        return False
    return polyline_length_m(line) >= summed * (1.0 - rel_tol)


def _line_closes(
    line: list[Coord],
    distance_km: float,
    *,
    tol_m: float = 150.0,
    rel_tol: float = 0.05,
) -> bool:
    """True if the stitched ``line`` returns to its own start — i.e. it really is the
    closed walk that a ``circular`` route's gain/loss is measured along.

    This is NOT what ``_stitch_is_faithful`` measures, and the two disagree in both
    directions on live data. Faithfulness is *length recovery* (the stitch kept ≥98 % of
    member length); a stitch can recover every metre in an order that never returns to
    the start. Measured over one Krkonoše run: `NS hornická Berghaus` is unfaithful yet
    closes perfectly (70 m gain / 66 m loss), while `route/8464045` is faithful yet ends
    85 % of its own length away from its start (0 m gain / 117 m loss).

    The tolerance is two-sided on purpose, because neither bound alone works. Absolute
    metres cannot separate a 69 m gap on a 0.1 km route (not a loop at all) from the
    same gap on a 10 km one (a digitization seam), so the gap is taken as a FRACTION of
    the route's own length. But an unbounded fraction lets a 20 km loop end a kilometre
    from its start, so it is also capped by ``tol_m`` — the project's existing "how close
    is closed" number, shared with ``access.is_circular``'s line fallback.

    5 % has an enormous margin on the measured partition: every closing loop in that run
    scored ≤ 0.02 and every broken one ≥ 0.69, with nothing in between.
    """
    if len(line) < 2:
        return False
    limit = min(tol_m, rel_tol * distance_km * 1000.0)
    return haversine_m(line[0], line[-1]) <= limit


def add_elevation(
    hike: Hike,
    line: list[Coord],
    elevation: ElevationProvider,
    *,
    sample_interval_m: float = 25.0,
    gain_threshold_m: float = 10.0,
    smooth_window: int = 3,
    loop_tolerance_m: float = 150.0,
    pre_elevations: list[float] | None = None,
    pre_points: list[Coord] | None = None,
    use_presampled: bool = False,
) -> None:
    """Expensive pass: fill gain/loss in place. Leaves them None on failure — and on a
    loop whose stitched line does not close (see ``_line_closes``), which is a failure
    of the *geometry* rather than of the lookup but is reported the same honest way.

    Normally this resamples ``line`` and looks the points up through ``elevation``.
    When ``use_presampled`` is set, it instead uses the caller-supplied
    ``pre_elevations`` series directly — skipping BOTH the resample and the provider
    call — and only runs the smoothing + hysteresis gain math on it. Composed loops
    use this: their elevation is pre-assembled from per-segment samples in
    ``search.compose_loops`` so a trail segment shared by several loops is looked up
    once, not once per loop (see HANDOFF "segment-level shared sampling"). A
    ``pre_elevations`` of ``None`` under ``use_presampled`` means the series was
    unavailable (e.g. a segment's lookup failed / quota ran out), so gain/loss stay
    None and the loop degrades to n/a — exactly as a failed direct lookup would.

    On success it also records ``hike.track`` — the resampled walking line zipped with
    its sampled elevations — for the GPX/GeoJSON exporters' per-point ``<ele>``. On the
    direct path that needs the stitched line to faithfully cover all member ways
    (``_stitch_is_faithful``); on the presampled path the caller passes the matching
    ``pre_points`` and the route is a single synthesised ring, so it is always faithful.
    When the track can't be built faithfully (or its points are unavailable) it stays
    empty and the export falls back to the raw-``ways`` geometry — gain/loss are
    unaffected either way.
    """
    if use_presampled:
        if pre_elevations is None:
            return  # series unavailable -> gain/loss stay None (degraded to n/a)
        elevations = pre_elevations
        sampled = pre_points  # caller-supplied points aligned with the series (or None)
    else:
        sampled = resample_by_distance(line, sample_interval_m)
        try:
            elevations = elevation.lookup(sampled)
        except ElevationError:
            return  # gain/loss stay None; the route is still listed unless gain-filtered
    gain, loss = cumulative_gain_loss(
        elevations, threshold_m=gain_threshold_m, smooth_window=smooth_window
    )
    # A loop's gain must equal its loss, and it only can if the line we just sampled
    # along is the closed walk the route claims to be. `circular` and this are two
    # different objects: `circular` comes off the member ways' vertex graph (circuit
    # rank), while gain rides on the STITCHED line, and `stitch_ways` greedily drops
    # members it can't chain. When they disagree, the elevation series belongs to some
    # other path than the route, and the answer is not merely imprecise but impossible —
    # live, Krkonoše: `gain=240 loss=0`, `gain=0 loss=117`, on routes labelled [loop].
    # Report nothing rather than that, exactly as a failed lookup or an exhausted quota
    # does. The track is dropped with it: it would be built from the same line.
    #
    # This is the same move HANDOFF records for distance and termini, which were taken
    # off greedy stitching for this class of reason; gain was left behind.
    #
    # Deliberately NOT a general "is the stitch sane" check. It fires only on loops,
    # because closure is the only cheap contradiction the geometry offers — a LINEAR
    # route whose stitch is misordered has no such signal, and its gain stays as
    # unverified after this as before. And not on the presampled path, whose route is a
    # single synthesised ring, closed by construction (same reasoning the track's
    # `_stitch_is_faithful` gate already skips there).
    if (
        hike.circular
        and not use_presampled
        and not _line_closes(line, hike.distance_km, tol_m=loop_tolerance_m)
    ):
        return
    hike.gain_m = round(gain)
    hike.loss_m = round(loss)

    # Per-point elevation track for export. Build it only with points aligned 1:1 to
    # the looked-up elevations, and — on the direct path — only when the stitched line
    # didn't drop legs (a presampled composed loop is a single faithful ring).
    if sampled is not None and len(sampled) >= 2 and len(sampled) == len(elevations):
        if use_presampled or _stitch_is_faithful(line, hike.ways):
            hike.track = tuple(
                (lat, lon, float(ele)) for (lat, lon), ele in zip(sampled, elevations)
            )


def _gain_desc(h: Hike) -> float:
    """Sort key: most climbing first; unknown gain sinks to the bottom."""
    return h.gain_m if h.gain_m is not None else -1.0


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
    transit_rail_radius_m: float = TRANSIT_RAIL_RADIUS_M,
    transit_stop_radius_m: float = TRANSIT_STOP_RADIUS_M,
    near_miss: bool | str = False,
    near_miss_gain_frac: float = 0.2,
    near_miss_dist_km: float = 2.0,
    near_miss_radius_frac: float = 0.5,
    near_miss_trigger: int = 1,
    poi_radius_m: float = 250.0,
    pre_elevations_by_id: dict[int, list[float]] | None = None,
    pre_points_by_id: dict[int, list[Coord]] | None = None,
) -> list[Hike]:
    """Run the two-pass filter and return matches, optionally with near-misses.

    ``near_miss`` is tri-state:
      * ``False`` (default) — strict matches only; the path is byte-for-byte the
        original two-pass filter (no extra geometry or elevation cost).
      * ``True`` — always also return near-misses: routes just outside the strict
        cut (distance/gain within a tolerance, or a parking/lift just past its
        access radius), each annotated in ``Hike.notes``.
      * ``"auto"`` — return near-misses only when fewer than ``near_miss_trigger``
        strict matches were found (default 1, i.e. only when there are *zero*).

    Near-misses are appended AFTER the strict matches and flagged with
    ``near_miss=True``, so a frontend renders them distinctly and never confuses a
    "close" route for a match. The relaxed pool's extra elevation lookups happen
    ONLY when near-misses are actually engaged, keeping the API economy intact when
    strict matches already exist.

    ``pre_elevations_by_id`` (used only by ``search.compose_loops``) maps a route's
    id to a pre-computed elevation series. When supplied, the elevation pass uses
    that series for each route instead of resampling + querying the provider — see
    ``add_elevation``'s ``use_presampled``. This lets compose look each shared trail
    segment up once (not once per loop). A route absent from the map (or mapped to a
    failed series) keeps gain/loss None. ``None`` (the default) leaves the ordinary
    path byte-for-byte unchanged. ``pre_points_by_id`` (compose only) maps the same ids
    to the resampled points behind each series, so the presampled route can still record
    a per-point elevation ``track`` for export; absent it, gain/loss are unaffected and
    only the track is skipped.

    ``criteria.poi_kinds`` adds the destination filter (see poi.py). It lands in the
    CHEAP pass, so a POI-filtered search spends *less* elevation budget than the same
    search without it — the routes that reach nothing are dropped before anyone pays for
    their gain profile.
    """
    use_pre = pre_elevations_by_id is not None
    want_band = near_miss is True or near_miss == "auto"
    # Cap the nearest-feature scan at the relaxed radius so an access near-miss can
    # report a feature just past the limit; None disables the scan on the plain path.
    car_max_m = car_radius_m * (1 + near_miss_radius_frac) if want_band else None
    lift_max_m = lift_radius_m * (1 + near_miss_radius_frac) if want_band else None
    # Relaxed by the rail radius: it is the larger of the two, so the near-miss note
    # can reach a station just past its own limit as well as a bus stop past its.
    transit_max_m = (
        transit_rail_radius_m * (1 + near_miss_radius_frac) if want_band else None
    )

    # Over-length guard: a through-route (e.g. a national trail) intersecting the
    # bbox comes back with its FULL geometry, so its length and endpoints belong
    # to another region. Drop anything much longer than the query area itself.
    max_len_m: float | None = None
    if bbox is not None:
        south, west, north, east = bbox
        diagonal_m = haversine_m((south, west), (north, east))
        max_len_m = diagonal_m * max_route_factor

    # Build the POI grid ONCE for the whole search, not once per route: the POIs belong
    # to the fetched area and never change while we iterate the routes, so every route
    # queries the same structure (see poi.PoiIndex). Skipped entirely when no POI filter
    # is set, keeping the ordinary path free of it. The bbox's worst-case latitude is
    # handed over so the grid's longitude cells stay wide enough across the whole area.
    poi_kinds = tuple(criteria.poi_kinds or ())
    poi_index: PoiIndex | None = None
    if poi_kinds:
        worst_lat = max(abs(bbox[0]), abs(bbox[2])) if bbox is not None else None
        poi_index = PoiIndex(area.pois, cell_m=poi_radius_m, worst_lat=worst_lat)

    # Cheap pass. Bucket each route by the STRICT cheap filter; when near-misses are
    # wanted, also collect the ones that clear only the RELAXED cheap filter (the
    # near-miss pool) so they can earn an elevation lookup below.
    strict_survivors: list[tuple[Hike, list[Coord]]] = []
    relaxed_only: list[tuple[Hike, list[Coord]]] = []
    for r in area.routes:
        measured = measure_geometry(
            r,
            area.parking,
            area.lifts,
            transit=area.transit,
            loop_tolerance_m=loop_tolerance_m,
            car_radius_m=car_radius_m,
            lift_radius_m=lift_radius_m,
            transit_rail_radius_m=transit_rail_radius_m,
            transit_stop_radius_m=transit_stop_radius_m,
            car_max_m=car_max_m,
            lift_max_m=lift_max_m,
            transit_max_m=transit_max_m,
            poi_index=poi_index,
            poi_kinds=poi_kinds,
            poi_radius_m=poi_radius_m,
        )
        if measured is None:
            continue
        hike, line = measured
        if max_len_m is not None and hike.distance_km * 1000.0 > max_len_m:
            continue
        if criteria.accepts_geometry(hike):
            strict_survivors.append((hike, line))
        elif want_band and criteria.accepts_geometry_relaxed(
            hike,
            dist_km_margin=near_miss_dist_km,
            radius_frac=near_miss_radius_frac,
            car_radius_m=car_radius_m,
            lift_radius_m=lift_radius_m,
            transit_radius_m=transit_rail_radius_m,
        ):
            relaxed_only.append((hike, line))

    # Expensive pass — strict survivors first (always), so we know the match count.
    for hike, line in strict_survivors:
        add_elevation(
            hike,
            line,
            elevation,
            sample_interval_m=sample_interval_m,
            gain_threshold_m=gain_threshold_m,
            smooth_window=smooth_window,
            loop_tolerance_m=loop_tolerance_m,
            pre_elevations=pre_elevations_by_id.get(hike.osm_id) if use_pre else None,
            pre_points=pre_points_by_id.get(hike.osm_id) if pre_points_by_id else None,
            use_presampled=use_pre,
        )

    matches = [h for h, _ in strict_survivors if criteria.accepts_gain(h)]

    # Engage near-misses only when asked (True) or when 'auto' and matches are scarce.
    engage = near_miss is True or (near_miss == "auto" and len(matches) < near_miss_trigger)
    near: list[Hike] = []
    if engage:
        # Pay elevation for the relaxed pool ONLY now (bounded by the tolerance band).
        for hike, line in relaxed_only:
            add_elevation(
                hike,
                line,
                elevation,
                sample_interval_m=sample_interval_m,
                gain_threshold_m=gain_threshold_m,
                smooth_window=smooth_window,
                loop_tolerance_m=loop_tolerance_m,
                pre_elevations=pre_elevations_by_id.get(hike.osm_id) if use_pre else None,
                pre_points=pre_points_by_id.get(hike.osm_id) if pre_points_by_id else None,
                use_presampled=use_pre,
            )
        # Candidates: strict survivors that failed only on gain, plus the relaxed
        # pool. Each gets notes (or is dropped if it misses too hard — gain only).
        candidates = [h for h, _ in strict_survivors if not criteria.accepts_gain(h)]
        candidates += [h for h, _ in relaxed_only]
        for h in candidates:
            notes = criteria.near_miss_notes(
                h,
                gain_frac=near_miss_gain_frac,
                car_radius_m=car_radius_m,
                lift_radius_m=lift_radius_m,
                transit_radius_m=transit_rail_radius_m,
            )
            if notes is not None:
                h.near_miss = True
                h.notes = notes
                near.append(h)

    matches.sort(key=_gain_desc, reverse=True)
    near.sort(key=_gain_desc, reverse=True)
    return matches + near
