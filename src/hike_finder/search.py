"""Orchestration shared by every frontend (CLI, web UI, MCP server).

One place that wires the three runtime pieces together — fetch OSM data, pick an
elevation provider, run the two-pass filter — so the frontends stay thin and
behave identically. The pure math lives in its own modules; this is the glue that
touches the network.

Three entry points, all returning the same filtered ``Hike`` list:
  - ``search_hikes``    — live: fetch the area and search it (one Overpass call + the
                          elevation API for surviving routes).
  - ``download_area``   — live: fetch an area and warm elevation for *every* plausible
                          route, returning a snapshot you can search offline forever.
  - ``search_snapshot`` — offline: search a saved snapshot with zero network.

``near_miss`` (tri-state ``False`` / ``True`` / ``"auto"``) is forwarded to
``find_hikes`` unchanged on both the live and offline paths — see filters.py.
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import replace

from . import cache as _cache
from . import config as _config
from .compose import (
    _assemble,
    _dijkstra,
    assemble_loop_series,
    build_trail_graph,
    clip_routes_to_bbox,
    find_loops,
    k_shortest_paths,
    resample_segments,
    snap_points,
)
from .config import Config
from .elevation import ElevationError, get_provider
from .filters import Criteria, Hike, find_hikes
from .geocode import DEFAULT_NOMINATIM_URL, NominatimGeocoder
from .geometry import Coord, haversine_m
from .naming import enrich_names
from .overpass import AreaData, DEFAULT_OVERPASS_URL, build_query, fetch_area
from .snapshot import (
    AreaSnapshot,
    RecordingElevationProvider,
    RecordingGeocoder,
    SnapshotElevationProvider,
    SnapshotGeocoder,
)

Bbox = tuple[float, float, float, float]

_log = logging.getLogger(__name__)


def _provider(cfg: Config, elevation_mode: str | None, dem_dir: str | None, cache=None):
    return get_provider(
        mode=elevation_mode or cfg.elevation_mode,
        dem_dir=dem_dir or cfg.dem_dir,
        api_endpoint=cfg.api_endpoint,
        api_min_interval_s=cfg.api_min_interval_s,
        api_max_retries=cfg.api_max_retries,
        api_backoff_s=cfg.api_backoff_s,
        api_max_backoff_s=cfg.api_max_backoff_s,
        api_daily_limit=cfg.api_daily_limit,
        api_state_dir=cfg.api_state_dir,
        cache=cache,
    )


def _fetch_area(
    bbox: Bbox,
    cfg: Config,
    cache,
    *,
    user_agent: str | None,
    overpass_url: str | None,
    read_cache: bool,
):
    """Fetch an area, transparently cached. ``read_cache=False`` forces a live fetch
    (used by ``download_area`` so a saved snapshot is always current) but still
    *refreshes* the cache on the way through, so later live searches benefit."""
    url = overpass_url or cfg.overpass_url or DEFAULT_OVERPASS_URL
    ua = user_agent or cfg.overpass_user_agent
    ttl_days = getattr(cfg, "overpass_cache_ttl_days", 0)
    cache_on = cache is not None and ttl_days > 0
    key = _cache.area_cache_key(url, build_query(*bbox)) if cache_on else None
    if cache_on and read_cache:
        hit = cache.get_area(key, ttl_days * 86400)
        if hit is not None:
            return hit
    area = fetch_area(*bbox, url, user_agent=ua)
    if cache_on:
        cache.put_area(key, area)
    return area


def _geocoder(cfg: Config, cache):
    """A reverse geocoder for naming unnamed routes, wrapped in the transparent place
    cache when caching is on (so a trailhead coordinate is looked up at most once)."""
    endpoint = cfg.nominatim_url or DEFAULT_NOMINATIM_URL
    inner = NominatimGeocoder(
        endpoint,
        user_agent=cfg.overpass_user_agent,
        min_interval_s=cfg.nominatim_min_interval_s,
    )
    if cache is not None and cfg.geocode_cache_ttl_days > 0:
        return _cache.CachingGeocoder(
            cache, endpoint, inner, cfg.geocode_cache_ttl_days * 86400
        )
    return inner


def _wants_geocode(name_places: bool | None, cfg: Config) -> bool:
    """Resolve the tri-state naming switch: an explicit frontend flag wins; otherwise
    fall back to ``HIKE_GEOCODE`` (off by default)."""
    return cfg.geocode_enabled if name_places is None else bool(name_places)


def _near_miss_kwargs(cfg: Config) -> dict:
    return {
        "near_miss_gain_frac": cfg.near_miss_gain_frac,
        "near_miss_dist_km": cfg.near_miss_dist_km,
        "near_miss_radius_frac": cfg.near_miss_radius_frac,
    }


def search_hikes(
    bbox: Bbox,
    criteria: Criteria,
    cfg: Config | None = None,
    *,
    user_agent: str | None = None,
    overpass_url: str | None = None,
    elevation_mode: str | None = None,
    dem_dir: str | None = None,
    near_miss: bool | str = False,
    name_places: bool | None = None,
) -> list[Hike]:
    """Fetch OSM data for ``bbox`` and return measured, filtered hikes.

    ``bbox`` is ``(south, west, north, east)``. Keyword overrides (used by the
    CLI's flags / the web form) win over ``cfg``; ``cfg`` defaults to the
    environment (see config.py).

    ``name_places`` (tri-state ``None``/``True``/``False``; ``None`` = follow
    ``HIKE_GEOCODE``) opt-in reverse-geocodes the *unnamed* survivors so a
    ``route/<id>`` route reads as a place-derived label. It runs only on the routes
    that already matched — the same two-pass economy the elevation pass uses — and is
    cached, so it stays a polite Nominatim citizen.
    """
    cfg = cfg or _config.load()
    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )
    provider = _provider(cfg, elevation_mode, dem_dir, cache)
    hikes = find_hikes(
        area,
        provider,
        criteria,
        bbox=bbox,
        max_route_factor=cfg.max_route_factor,
        sample_interval_m=cfg.sample_interval_m,
        gain_threshold_m=cfg.gain_threshold_m,
        smooth_window=cfg.smooth_window,
        loop_tolerance_m=cfg.loop_tolerance_m,
        car_radius_m=cfg.car_radius_m,
        lift_radius_m=cfg.lift_radius_m,
        near_miss=near_miss,
        **_near_miss_kwargs(cfg),
    )
    if _wants_geocode(name_places, cfg):
        enrich_names(hikes, _geocoder(cfg, cache))
    return hikes


def _measure_composed(
    graph,
    routes: list,
    area: AreaData,
    criteria: Criteria,
    cfg: Config,
    provider,
    bbox: Bbox,
    *,
    near_miss: bool | str,
    roundtrip: str,
    name: str,
) -> list[Hike]:
    """Measure a list of synthesised routes (composed loops OR point-to-point paths).

    Shared by ``compose_loops`` / ``compose_loops_around`` (closed loops, ``roundtrip="yes"``)
    and ``routes_between`` (open paths, ``roundtrip="no"``). Each ``routes`` item is a
    :class:`compose.ComposedLoop`-shaped object (``coords``/``seg_ids``/``ordered_segs``/
    ``start_node``/``refs``/``anchor``) — ``_assemble`` produces this for both loops and paths.

    Elevation is looked up ONCE per distinct trail segment and sliced back per route, so a
    segment shared by several routes (loops overlap; Yen paths share a trunk) is sampled a
    single time — the same per-segment economy the loop path always used (see the long note
    below, preserved from ``compose_loops``). Each route is then wrapped as a synthetic route
    and run through the *unchanged* ``find_hikes``, so its elevation/distance/access are
    computed exactly as for a real relation, and offline == online holds by construction.
    """
    # Segment-level shared elevation sampling. Composed routes overlap — several share the
    # same marked-trail segments — so resampling and looking up each WHOLE route (the obvious
    # `find_hikes` reuse) pays for a shared climb once per route that uses it. Instead,
    # resample each DISTINCT used segment once on its own canonical grid and look the WHOLE
    # distinct-point set up in ONE provider call, then slice the results back per segment.
    # Routes then assemble their elevation series from those shared per-segment results
    # (`assemble_loop_series`). This dedups within the run AND makes the points cache-hot
    # across runs, because a segment's canonical samples are identical regardless of which
    # route enters it where. One combined lookup (not one per segment) is deliberate: the API
    # batches 100 points/request, so packing all distinct points into a single call costs
    # ~ceil(total/100) requests. It is all-or-nothing on failure — a mid-run quota exhaustion
    # fails the whole batch and every route degrades to gain n/a (graceful, never a ban).
    used_segs = sorted(set().union(*(r.seg_ids for r in routes))) if routes else []
    seg_points = resample_segments(graph, used_segs, cfg.sample_interval_m)
    flat: list = []
    spans: dict[int, tuple[int, int]] = {}
    for sid in used_segs:
        pts = seg_points[sid]
        spans[sid] = (len(flat), len(flat) + len(pts))
        flat.extend(pts)
    seg_elev: dict[int, list[float] | None]
    try:
        all_elev = provider.lookup(flat) if flat else []
        seg_elev = {sid: all_elev[lo:hi] for sid, (lo, hi) in spans.items()}
    except ElevationError:
        seg_elev = {sid: None for sid in used_segs}

    # Wrap each route as a synthetic route and run the SAME engine. The negative id keys the
    # route back to its provenance after find_hikes (which preserves osm_id per Hike). Each
    # route's elevation series is pre-assembled from the shared per-segment lookups above and
    # handed to find_hikes, so its elevation pass skips the redundant whole-route resample.
    route_by_id: dict[int, object] = {}
    syn_routes: list[dict] = []
    pre_elev_by_id: dict[int, list[float]] = {}
    pre_points_by_id: dict[int, list] = {}
    for i, route in enumerate(routes):
        sid = -(i + 1)
        route_by_id[sid] = route
        syn_routes.append(
            {
                "id": sid,
                "name": name,
                "ref": None,
                "osmc_color": None,
                "tags": {"roundtrip": roundtrip},
                "ways": [route.coords],
            }
        )
        series = assemble_loop_series(graph, route, seg_elev)
        if series is not None:
            pre_elev_by_id[sid] = series
            pre_points_by_id[sid] = assemble_loop_series(graph, route, seg_points)
    syn_area = AreaData(routes=syn_routes, parking=area.parking, lifts=area.lifts)
    hikes = find_hikes(
        syn_area,
        provider,
        criteria,
        bbox=bbox,
        # Composed routes are already clipped and bounded, so the through-route over-length
        # guard (meant for relations that merely cross the area) doesn't apply.
        max_route_factor=float("inf"),
        sample_interval_m=cfg.sample_interval_m,
        gain_threshold_m=cfg.gain_threshold_m,
        smooth_window=cfg.smooth_window,
        loop_tolerance_m=cfg.loop_tolerance_m,
        car_radius_m=cfg.car_radius_m,
        lift_radius_m=cfg.lift_radius_m,
        near_miss=near_miss,
        pre_elevations_by_id=pre_elev_by_id,
        pre_points_by_id=pre_points_by_id,
        **_near_miss_kwargs(cfg),
    )
    for h in hikes:
        route = route_by_id.get(h.osm_id)
        if route is not None:
            h.composed = True
            h.composed_of = route.refs
            if getattr(route, "anchor", None) is not None:
                # Access-anchored loop: start at the trailhead you drive/ride to (the on-route
                # point nearest your parking/lift), not the geometric head. Label only — the
                # coords stay unrotated, so gain/loss is byte-identical to an unanchored run.
                h.start = route.anchor
    return hikes


def compose_loops(
    bbox: Bbox,
    criteria: Criteria,
    cfg: Config | None = None,
    *,
    user_agent: str | None = None,
    overpass_url: str | None = None,
    elevation_mode: str | None = None,
    dem_dir: str | None = None,
    near_miss: bool | str = False,
) -> list[Hike]:
    """Synthesise loops from connected marked-trail segments, then measure them.

    Where ``search_hikes`` reports each OSM relation as-is (so ``circular`` only finds
    loops mapped as a single relation), this builds ONE graph from every relation's
    member ways and searches it for cycles of a target length — the day-loops that are
    really ad-hoc combinations of several marked trails (see compose.py).

    The target length band comes from ``criteria.min/max_distance_km`` (falling back to
    ``cfg.compose_min_km``/``compose_max_km``). Each composed loop is wrapped as a
    synthetic ``roundtrip=yes`` route and run through the *unchanged* ``find_hikes``, so
    its elevation/gain, distance, and car/lift access are computed exactly as for a real
    route — and offline == online holds by construction. Composed loops carry no single
    OSM id; ``Hike.composed_of`` lists their constituent trail refs for the renderer.

    The graph is clipped to ``bbox`` first, so a loop stays inside the searched area.
    """
    cfg = cfg or _config.load()
    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )

    graph = build_trail_graph(clip_routes_to_bbox(area.routes, bbox))
    provider = _provider(cfg, elevation_mode, dem_dir, cache)
    return _compose_from_graph(
        graph, area, criteria, cfg, provider, bbox, near_miss=near_miss, point_anchor=None
    )


def _compose_from_graph(
    graph,
    area: AreaData,
    criteria: Criteria,
    cfg: Config,
    provider,
    bbox: Bbox,
    *,
    near_miss: bool | str,
    point_anchor: tuple[Coord, float] | None = None,
) -> list[Hike]:
    """Find + measure composed loops on an already-built graph.

    Shared by ``compose_loops`` (bbox-driven) and ``compose_loops_around`` (point-driven).
    ``point_anchor`` (``(point, radius_m)``, listed first so the loop starts at the point)
    requires each loop to pass within ``radius_m`` of the picked point; car/lift ``criteria``
    add further anchors, AND-ed, exactly as before.
    """
    min_km = criteria.min_distance_km if criteria.min_distance_km is not None else cfg.compose_min_km
    max_km = (
        criteria.max_distance_km
        if criteria.max_distance_km is not None
        else max(cfg.compose_max_km, min_km)
    )

    # Access anchoring: each requirement (point / car / lift) restricts the composed loops to
    # those reachable from it BEFORE the cap, and starts each at that anchor — "a loop from
    # where I point/park". The requirement set mirrors find_hikes (SAME radii + access-point
    # sets, AND-ed), so the loops kept here are exactly the ones find_hikes accepts. The
    # point (when given) is listed FIRST, so the loop starts where you pointed.
    anchors: list[tuple[list, float]] = []
    if point_anchor is not None:
        anchors.append(([point_anchor[0]], point_anchor[1]))
    if criteria.car_access is True:
        anchors.append(([p["coord"] for p in area.parking], cfg.car_radius_m))
    if criteria.chairlift_access is True:
        anchors.append(
            ([s for lift in area.lifts for s in lift.get("stations", [])], cfg.lift_radius_m)
        )

    result = find_loops(
        graph,
        min_m=min_km * 1000.0,
        max_m=max_km * 1000.0,
        max_segments=cfg.compose_max_segments,
        max_loops=cfg.compose_max_loops,
        overlap_frac=cfg.compose_overlap_frac,
        min_compactness=cfg.compose_min_compactness,
        anchors=anchors or None,
    )
    # Logged, never silent: how many distinct loops exist vs how many we elevation+show,
    # whether the bounded search hit its budget, and (when anchored) the accessible-vs-found
    # funnel — so a truncated/capped/filtered result is never mistaken for "that's all there is".
    truncated = (
        f" (showing the {len(result.loops)} most loop-like; raise HIKE_COMPOSE_MAX_LOOPS for more)"
        if result.distinct > len(result.loops) else ""
    )
    capped_note = (
        " [cycle search capped — results may be incomplete; narrow the distance band]"
        if result.capped else ""
    )
    sliver_note = (
        f" ({result.slivered} thin sliver(s) dropped below compactness "
        f"{cfg.compose_min_compactness:g})"
        if result.slivered else ""
    )
    if point_anchor is not None:
        _log.warning(
            "compose: %d loop(s) within %.0f m of your point in %.0f-%.0f km, of %d cycle(s) "
            "found in band from %d trail segments%s%s%s",
            result.distinct, point_anchor[1], min_km, max_km, result.found,
            len(graph.segments), sliver_note, truncated, capped_note,
        )
    elif anchors:
        _log.warning(
            "compose: %d loop(s) in %.0f-%.0f km reachable from the requested "
            "car/lift access, of %d cycle(s) found in band from %d trail segments%s%s%s",
            result.distinct, min_km, max_km, result.found, len(graph.segments),
            sliver_note, truncated, capped_note,
        )
    else:
        _log.warning(
            "compose: %d distinct loop(s) in %.0f-%.0f km from %d trail segments%s%s%s",
            result.distinct, min_km, max_km, len(graph.segments),
            sliver_note, truncated, capped_note,
        )
    return _measure_composed(
        graph, result.loops, area, criteria, cfg, provider, bbox,
        near_miss=near_miss, roundtrip="yes", name="Composed loop",
    )


def _bbox_around(point: Coord, pad_m: float) -> Bbox:
    """A (south, west, north, east) box centred on ``point``, padded ``pad_m`` metres."""
    lat, lon = point
    dlat = pad_m / 111_320.0
    dlon = pad_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def compose_loops_around(
    point: Coord,
    criteria: Criteria,
    cfg: Config | None = None,
    *,
    radius_m: float | None = None,
    user_agent: str | None = None,
    overpass_url: str | None = None,
    elevation_mode: str | None = None,
    dem_dir: str | None = None,
    near_miss: bool | str = False,
) -> list[Hike]:
    """Circular routes that pass within ``radius_m`` of a picked ``point`` and start there.

    Feature: "I pick a point and get circular day-loops near it, of a set length." It reuses
    the loop-composition engine (``compose_loops``) with the point as a compose *anchor*:
    only loops coming within ``radius_m`` (default ``cfg.around_radius_m``) of the point
    survive, each started at the on-loop vertex nearest the point. The target length band is
    ``criteria.min/max_distance_km`` (falling back to ``cfg.compose_min/max_km``).

    Unlike ``compose_loops``, no ``bbox`` is given: it is derived from the point as
    ``radius + max-loop/2`` — the tight bound below which a qualifying loop (length ≤ max,
    passing within radius of the point) can never be clipped, so completeness holds.
    """
    cfg = cfg or _config.load()
    radius_m = radius_m if radius_m is not None else cfg.around_radius_m
    max_km = (
        criteria.max_distance_km
        if criteria.max_distance_km is not None
        else max(cfg.compose_max_km, criteria.min_distance_km or 0.0)
    )
    # A loop of length <= max_km passing within radius of the point has every vertex within
    # radius + max_km/2 of it (go out along the loop and back), so this pad can't clip one.
    pad_m = radius_m + max_km * 1000.0 / 2.0
    bbox = _bbox_around(point, pad_m)
    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )
    graph = build_trail_graph(clip_routes_to_bbox(area.routes, bbox))
    provider = _provider(cfg, elevation_mode, dem_dir, cache)
    return _compose_from_graph(
        graph, area, criteria, cfg, provider, bbox,
        near_miss=near_miss, point_anchor=(point, radius_m),
    )


def routes_between(
    start: Coord,
    finish: Coord,
    criteria: Criteria,
    cfg: Config | None = None,
    *,
    k: int | None = None,
    user_agent: str | None = None,
    overpass_url: str | None = None,
    elevation_mode: str | None = None,
    dem_dir: str | None = None,
) -> list[Hike]:
    """The ``k`` shortest distinct trail routes from ``start`` to ``finish``, shortest first.

    Feature: "I pick two points and get several routes between them, starting with the
    shortest." Builds the trail graph for a bbox derived from the two points, snaps each to
    the nearest point ON the network (splitting the nearest segment, so a route reaches
    exactly where you pointed), then runs Yen's k-shortest-loopless-paths (see
    ``compose.k_shortest_paths``) — with an overlap filter so the routes are genuinely
    distinct alternatives, not one line ± a segment.

    ``k`` defaults to ``cfg.routes_k``. A route's length is capped by ``criteria.max_distance_km``
    if given, else ``cfg.routes_max_factor x`` the straight-line separation. Each route is
    measured through the *unchanged* ``find_hikes`` (elevation/gain, access), so offline ==
    online holds; the results are ordered shortest-first by measured distance.
    """
    cfg = cfg or _config.load()
    k = k if k is not None else cfg.routes_k
    # A point-to-point route is never a loop, so a stray `circular` filter (e.g. a --circular
    # flag left on from another search) would drop every route to nothing — neutralise it here
    # so the shape filter can't silently empty the result. Distance/gain/access filters still
    # apply. Done in the engine so all three frontends (CLI, MCP, web) behave identically.
    if criteria.circular is not None:
        criteria = replace(criteria, circular=None)
    sep_m = haversine_m(start, finish)
    pad_m = max(cfg.routes_pad_km * 1000.0, cfg.routes_pad_frac * sep_m)
    # Bounding box of BOTH points, padded (a route may bow out of the direct corridor).
    lats = (start[0], finish[0])
    lons = (start[1], finish[1])
    dlat = pad_m / 111_320.0
    lat0 = sum(lats) / 2.0
    dlon = pad_m / (111_320.0 * max(math.cos(math.radians(lat0)), 1e-6))
    bbox: Bbox = (min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon)

    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )
    graph = build_trail_graph(clip_routes_to_bbox(area.routes, bbox))
    graph, snapped = snap_points(graph, [start, finish])
    (src, src_d), (dst, dst_d) = snapped
    if src < 0 or dst < 0:
        _log.warning("routes: no trails found in the area around your two points")
        return []
    max_snap_m = cfg.routes_max_snap_km * 1000.0
    if src_d > max_snap_m or dst_d > max_snap_m:
        # A point sits far from any trail — routing it to a distant trail would be
        # misleading ("your finish" ends up km from where you pointed). Bail out loudly.
        _log.warning(
            "routes: a picked point is %.1f km from the nearest trail (limit %.1f km) — "
            "no routes drawn; move it closer to a marked trail or raise HIKE_ROUTES_MAX_SNAP_KM",
            max(src_d, dst_d) / 1000.0, cfg.routes_max_snap_km,
        )
        return []

    max_m = (
        criteria.max_distance_km * 1000.0
        if criteria.max_distance_km is not None
        else cfg.routes_max_factor * sep_m
    )
    paths = k_shortest_paths(
        graph, src, dst, k=k, overlap_frac=cfg.routes_overlap_frac, max_m=max_m
    )
    _log.warning(
        "routes: %d route(s) from your start (snapped %.0f m to the network) to your finish "
        "(snapped %.0f m); straight-line separation %.1f km, length cap %.1f km",
        len(paths), src_d, dst_d, sep_m / 1000.0, max_m / 1000.0,
    )
    if src == dst:
        _log.warning("routes: your two points snapped to the SAME trail vertex — nothing to route")

    # Start each route at the snapped start vertex (a path's `anchor`, so _measure_composed
    # sets Hike.start there) rather than find_hikes' arbitrary geometric head.
    start_coord = graph.coords[src]
    for p in paths:
        p.anchor = start_coord

    provider = _provider(cfg, elevation_mode, dem_dir, cache)
    hikes = _measure_composed(
        graph, paths, area, criteria, cfg, provider, bbox,
        near_miss=False, roundtrip="no", name="Route",
    )
    # Shortest-first by measured distance (Yen orders by graph length; re-sort on the final
    # measured km so the user's "starting with the shortest" holds on the reported number).
    hikes.sort(key=lambda h: h.distance_km)
    return hikes


def route_via(
    points: list[Coord],
    criteria: Criteria,
    cfg: Config | None = None,
    *,
    loop: bool = False,
    user_agent: str | None = None,
    overpass_url: str | None = None,
    elevation_mode: str | None = None,
    dem_dir: str | None = None,
) -> list[Hike]:
    """ONE route linking several picked points in the order given, each snapped to the
    nearest trail.

    Feature: "I pick several points and get a single route linking them." With ``loop=False``
    this draws the shortest open route ``p1 -> p2 -> ... -> pn`` — visiting the points in the
    order given, with no reordering, so the result is predictable. With ``loop=True`` it closes
    the route back to ``p1`` into a *circular* route whose legs avoid retracing one another:
    each leg is routed with the segments already used by earlier legs removed from the graph,
    so the circuit is edge-disjoint where the network allows and retraces only a leg that has
    no disjoint alternative. The retraced fraction is measured and logged; a circuit forced
    into a mostly-out-and-back (no disjoint return near the points) is flagged loudly.

    Like ``routes_between`` it derives its own bbox from the points, snaps each onto the
    nearest point ON the network (splitting the nearest segment so the route reaches exactly
    where you pointed), and measures the assembled route through the *unchanged* ``find_hikes``
    so offline == online holds. A point more than ``cfg.routes_max_snap_km`` from any trail, or
    a leg crossing a gap in the network, aborts loudly rather than routing to a distant trail.
    Length/gain/access filters in ``criteria`` still apply (e.g. ``--max-distance`` drops a
    linked route that runs longer than you allow).
    """
    cfg = cfg or _config.load()
    if len(points) < 2:
        _log.warning("route via: need at least two points to link")
        return []
    # A linked/looped route is synthesised, not a mapped relation, so a stray `circular` shape
    # filter would drop it to nothing — neutralise it here (distance/gain/access filters still
    # apply), exactly as routes_between does, so all three frontends behave identically.
    if criteria.circular is not None:
        criteria = replace(criteria, circular=None)

    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    # The widest consecutive separation drives the bbox pad, so any single leg can bow out of
    # the direct corridor between its two points without being clipped.
    seps = [haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1)]
    if loop:
        seps.append(haversine_m(points[-1], points[0]))
    pad_m = max(cfg.routes_pad_km * 1000.0, cfg.routes_pad_frac * (max(seps) if seps else 0.0))
    lat0 = sum(lats) / len(lats)
    dlat = pad_m / 111_320.0
    dlon = pad_m / (111_320.0 * max(math.cos(math.radians(lat0)), 1e-6))
    bbox: Bbox = (min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon)

    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )
    graph = build_trail_graph(clip_routes_to_bbox(area.routes, bbox))
    graph, snapped = snap_points(graph, points)
    nodes = [n for (n, _) in snapped]
    if any(n < 0 for n in nodes):
        _log.warning("route via: no trails found in the area around your points")
        return []
    max_snap_m = cfg.routes_max_snap_km * 1000.0
    far = [(i + 1, d) for i, (_, d) in enumerate(snapped) if d > max_snap_m]
    if far:
        _log.warning(
            "route via: point(s) %s sit farther than %.1f km from the nearest trail — no route "
            "drawn; move them closer to a marked trail or raise HIKE_ROUTES_MAX_SNAP_KM",
            ", ".join(f"#{i} ({d / 1000.0:.1f} km)" for i, d in far), cfg.routes_max_snap_km,
        )
        return []

    # Chain the legs. For a loop, remove segments used by earlier legs so the circuit stays
    # edge-disjoint where the network allows; a leg with no disjoint alternative falls back to
    # reusing them (a retrace on that leg), which the overlap report below surfaces. Open routes
    # take the plain shortest path per leg (consecutive legs may share a junction stub — fine).
    legs = list(zip(nodes, nodes[1:]))
    if loop:
        legs.append((nodes[-1], nodes[0]))
    ordered: list[int] = []
    used: set[int] = set()
    for li, (u, v) in enumerate(legs, start=1):
        if u == v:
            continue  # two consecutive points snapped to the same trail vertex — empty leg
        res = _dijkstra(graph, u, v, removed_edges=frozenset(used)) if loop else _dijkstra(graph, u, v)
        if res is None and loop:
            res = _dijkstra(graph, u, v)  # no disjoint path for this leg — allow a retrace
        if res is None:
            _log.warning(
                "route via: leg %d crosses a gap in the trail network (no connected path "
                "between those two points) — no route drawn", li,
            )
            return []
        segs, _leg_nodes, _leg_len = res
        ordered.extend(segs)
        used.update(segs)
    if not ordered:
        _log.warning("route via: all your points snapped to the same trail vertex — nothing to route")
        return []

    # Retrace report: how much of the trail covered is walked more than once (0 = a clean
    # non-repeating loop; 1.0 = a full out-and-back). Reported so "not repeating in its major
    # part" is a stated, measured property, not a hope.
    counts = Counter(ordered)
    seg_len = {i: graph.segments[i].length_m for i in counts}
    distinct_len = sum(seg_len.values())
    retraced_len = sum(seg_len[i] * (c - 1) for i, c in counts.items())
    overlap = retraced_len / distinct_len if distinct_len else 0.0
    _log.warning(
        "route via: %d-point %s over %d segment(s), %.1f km of distinct trail, %.0f%% "
        "retraced (snap distances: %s)",
        len(points), "circular route" if loop else "route", len(counts), distinct_len / 1000.0,
        overlap * 100.0, ", ".join(f"{d:.0f} m" for (_, d) in snapped),
    )
    if loop and overlap >= 0.5:
        _log.warning(
            "route via: this circular route retraces %.0f%% of its trail — no disjoint return "
            "exists near your points, so it is largely an out-and-back; showing it anyway",
            overlap * 100.0,
        )

    start_node = nodes[0]
    route = _assemble(graph, start_node, ordered)
    # Start the rendered route at the first point you picked (its snapped vertex), not the
    # assembled ring's arbitrary head — _measure_composed honours `anchor` for Hike.start.
    route.anchor = graph.coords[start_node]
    provider = _provider(cfg, elevation_mode, dem_dir, cache)
    return _measure_composed(
        graph, [route], area, criteria, cfg, provider, bbox,
        near_miss=False, roundtrip="yes" if loop else "no",
        name="Circular route via points" if loop else "Route via points",
    )


def download_area(
    bbox: Bbox,
    cfg: Config | None = None,
    *,
    user_agent: str | None = None,
    overpass_url: str | None = None,
    elevation_mode: str | None = None,
    dem_dir: str | None = None,
    name_places: bool | None = None,
) -> AreaSnapshot:
    """Fetch an area and warm elevation for *every* geometry-plausible route.

    This deliberately spends the elevation budget up front — you download before you
    know your filters, so every route the over-length guard keeps is sampled. The
    cost is one-time: the returned snapshot is then searchable offline with no further
    API calls (see ``search_snapshot``). Routes whose elevation lookup fails (e.g. the
    daily quota runs out mid-download) are simply left unsampled and degrade to n/a
    offline, exactly as they would live.

    A download deliberately bypasses the Overpass *read* cache (``read_cache=False``)
    so a freshly-named snapshot always reflects current OSM, never a weeks-old cached
    area — but it still refreshes the cache and warms the elevation cache, both pure
    wins for later live searches.

    ``name_places`` (opt-in, like the live search) additionally **bakes** reverse-geocoded
    names for the unnamed survivors into the snapshot, so an offline ``--area`` search can
    label them with zero network. It is off by default because it hits Nominatim at the
    polite ≥1 req/s — and a download geocodes *every* unnamed plausible route, not just a
    filtered handful — so we only pay it when asked. The recording wraps the *cached*
    geocoder, so the download also warms the persistent place cache.
    """
    cfg = cfg or _config.load()
    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=False
    )
    recorder = RecordingElevationProvider(_provider(cfg, elevation_mode, dem_dir, cache))
    # Empty criteria => no filtering: find_hikes still runs the cheap pass (so the
    # over-length guard drops through-routes, sparing their elevation) and the
    # elevation pass on every survivor, which is exactly what the recorder captures.
    hikes = find_hikes(
        area,
        recorder,
        Criteria(),
        bbox=bbox,
        max_route_factor=cfg.max_route_factor,
        sample_interval_m=cfg.sample_interval_m,
        gain_threshold_m=cfg.gain_threshold_m,
        smooth_window=cfg.smooth_window,
        loop_tolerance_m=cfg.loop_tolerance_m,
        car_radius_m=cfg.car_radius_m,
        lift_radius_m=cfg.lift_radius_m,
    )
    # Keep ONLY the routes the over-length guard accepted (exactly the ones we
    # sampled). Pruning the unsampled through-routes makes the snapshot self-
    # consistent: a later offline search can't surface a route with no elevation as
    # n/a just because its max_route_factor is looser than this download's.
    kept = {h.osm_id for h in hikes}
    area.routes = [r for r in area.routes if r.get("id") in kept]
    places: dict = {}
    if _wants_geocode(name_places, cfg):
        # Bake place names for the unnamed survivors, recording every point->place the
        # geocoder resolves. enrich_names mutates these (discarded) hikes' place_name in
        # passing — harmless; we keep only the recording, which a later offline search
        # replays through the SAME enrich_names (see search_snapshot).
        geo = RecordingGeocoder(_geocoder(cfg, cache))
        labelled = enrich_names(hikes, geo)
        places = geo.places
        _log.warning(
            "download: baked place names for %d unnamed route(s) (%d point(s))",
            labelled, len(places),
        )
    return AreaSnapshot(
        bbox=tuple(bbox),
        area=area,
        elevations=recorder.samples,
        sample_interval_m=cfg.sample_interval_m,
        user_agent=user_agent or cfg.overpass_user_agent,
        places=places,
    )


def search_snapshot(
    snapshot: AreaSnapshot,
    criteria: Criteria,
    cfg: Config | None = None,
    *,
    near_miss: bool | str = False,
    name_places: bool | None = None,
) -> list[Hike]:
    """Search a saved snapshot offline (no network).

    The snapshot's ``sample_interval_m`` is LOCKED in — the saved elevation points were
    sampled at that interval, so the offline search must resample identically to hit
    them. Everything else stays tunable from ``cfg``: ``gain_threshold``/``smooth_window``
    re-derive gain/loss from the saved elevations, and the geometry knobs
    (loop tolerance, access radii) re-decide shape/access from the saved geometry — all
    without touching the network. The over-length guard reuses the snapshot's own bbox.
    """
    cfg = cfg or _config.load()
    provider = SnapshotElevationProvider(snapshot.elevations)
    hikes = find_hikes(
        snapshot.area,
        provider,
        criteria,
        bbox=snapshot.bbox,
        max_route_factor=cfg.max_route_factor,
        sample_interval_m=snapshot.sample_interval_m,  # locked to the snapshot
        gain_threshold_m=cfg.gain_threshold_m,
        smooth_window=cfg.smooth_window,
        loop_tolerance_m=cfg.loop_tolerance_m,
        car_radius_m=cfg.car_radius_m,
        lift_radius_m=cfg.lift_radius_m,
        near_miss=near_miss,
        **_near_miss_kwargs(cfg),
    )
    if _wants_geocode(name_places, cfg):
        if snapshot.places:
            # v2: replay the names baked at download time through the SAME enrich_names
            # that drives the live geocoder, with zero network. Offline == online by
            # construction, modulo access-radius changes that move a route's start off a
            # recorded point (then it degrades to its route/<id> fallback — see snapshot.py).
            enrich_names(hikes, SnapshotGeocoder(snapshot.places))
        else:
            # No baked names (downloaded without naming, or a pre-v2 snapshot): geocoding
            # needs the network an offline search never touches, so honour the
            # offline==online promise loudly rather than silently dropping the request.
            _log.warning(
                "name_places: this snapshot has no baked place names — re-download the "
                "area with naming enabled to label its unnamed routes offline; for now "
                "they keep their route/<id> label"
            )
    return hikes
