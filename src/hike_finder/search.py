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

Plus one pair that returns objects rather than hikes — the *inventory* mode, "show me
every ruin in this area, no routes":
  - ``list_area_pois``     — live: one Overpass call, no elevation, no quota spent.
  - ``list_snapshot_pois`` — offline: the same selection over a saved area.

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
from . import ferrata as _ferrata
from . import poi as _poi
from .compose import (
    _assemble,
    _dijkstra,
    assemble_loop_series,
    assemble_tag_runs,
    build_trail_graph,
    clip_routes_to_bbox,
    find_loops,
    k_shortest_paths,
    resample_segments,
    snap_points,
)
from .config import Config
from .elevation import ElevationError, get_provider
from .ferrata import FerrataSummary, summarise_ferrata
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
from .surface import summarise_surface, summarise_tracktype

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
    diagnostics: dict | None = None,
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

    ``diagnostics``, if given, is filled with facts about the FETCH that the returned
    hikes cannot carry — currently ``no_routes``, for telling "nothing matched" apart
    from "nothing is mapped" (see ``area_has_no_routes``). It is an out-parameter rather
    than a richer return type on purpose: the three frontends all dispatch
    ``compose_loops if composing else search_hikes`` and call the winner with one shared
    kwargs dict, so a keyword both accept is the only seam that stays uniform across
    them. Re-fetching the area in the caller was the alternative and is worse than it
    looks — it is free only while the Overpass cache is on (``ttl_days > 0``), and with
    caching disabled it would fire a second live request against a public instance
    merely to word an error message.
    """
    cfg = cfg or _config.load()
    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )
    if diagnostics is not None:
        diagnostics["no_routes"] = area_has_no_routes(area)
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
        transit_rail_radius_m=cfg.transit_rail_radius_m,
        transit_stop_radius_m=cfg.transit_stop_radius_m,
        near_miss=near_miss,
        poi_radius_m=cfg.poi_radius_m,
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
    # Cabled sections, computed HERE rather than attached afterwards the way surface is —
    # and the difference is not cosmetic. Surface is report-only, so `_attach_composed_surface`
    # can run after `find_hikes` and nothing depends on the ordering. Ferrata is FILTERED on,
    # so a summary attached afterwards would arrive with the cheap pass already over:
    # `--compose-loops --no-ferrata` would see `ferrata=None` on every synthetic route, fail
    # the unknown-drops rule, and return an empty list for a landscape full of walkable loops.
    # This is also the case where avoidance matters most — a loop synthesised through a cabled
    # pitch is one nobody chose to walk.
    pre_ferrata_by_id: dict[int, FerrataSummary | None] = {}
    for i, route in enumerate(routes):
        sid = -(i + 1)
        route_by_id[sid] = route
        # `assemble_tag_runs` returns None when the graph was built from data carrying no
        # member-way tags, which `summarise_ferrata` turns straight back into "we could not
        # look" — the pre-surface-snapshot state, preserved rather than read as "clean".
        pre_ferrata_by_id[sid] = summarise_ferrata(assemble_tag_runs(graph, route))
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
    # Carry the area's POIs onto the synthetic area: without them a composed loop could
    # never match a POI filter, and "a 12 km loop past a ruin" is exactly the query this
    # mode exists for.
    syn_area = AreaData(
        routes=syn_routes,
        parking=area.parking,
        lifts=area.lifts,
        pois=area.pois,
        # …and the kind set they were classified against, or the synthetic area would
        # claim not to know its own coverage while holding the very objects that prove
        # it. Nothing downstream of `find_hikes` reads it today; carrying it keeps the
        # field a property of the POI list rather than of one constructor.
        poi_kinds=area.poi_kinds,
    )
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
        transit_rail_radius_m=cfg.transit_rail_radius_m,
        transit_stop_radius_m=cfg.transit_stop_radius_m,
        near_miss=near_miss,
        poi_radius_m=cfg.poi_radius_m,
        pre_elevations_by_id=pre_elev_by_id,
        pre_points_by_id=pre_points_by_id,
        pre_ferrata_by_id=pre_ferrata_by_id,
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
            # A route drawn TO an object (``routes_to_poi``) carries its destination on the
            # assembled route — set from outside, like ``anchor``, so the compose layer stays
            # unaware of POIs. Handing it over here rather than re-deriving it in the caller
            # keeps the synthetic-id scheme above the ONLY place that knows how a Hike maps
            # back to its route.
            dest = getattr(route, "destination", None)
            if dest is not None:
                h.destination = dest
            _attach_composed_surface(graph, route, h)
    return hikes


def _attach_composed_surface(graph, route, h: Hike) -> None:
    """Report what a SYNTHESISED route is underfoot, from the graph's per-step way tags.

    Set from outside ``find_hikes``, like ``anchor`` and ``destination``. A composed route's
    synthetic dict is one assembled polyline (``"ways": [route.coords]``) with no member list
    for ``way_tags`` to be parallel to, so ``measure_geometry`` can only leave surface at
    None; the tags survive on the graph instead, per step of each contracted segment.

    Splitting the polyline into tag-uniform "member ways" so ``measure_geometry`` could do
    this itself was the tempting alternative and is worse: a self-touching route (a
    ``--via-loop`` that falls back to a forced retrace) would then meet greedy stitching with
    four candidate ends at the revisited vertex, and one wrong pairing trips export's ≥98 %
    faithfulness gate — turning a clean single track with per-point elevation into a raw-ways
    export with none. The measurement is shared where it matters (``surface.summarise_*`` is
    called verbatim); only the call site differs.

    Silent when the area data never fetched member-way tags — ``assemble_tag_runs`` makes
    that call, so a pre-surface snapshot keeps saying "we didn't look" rather than "nothing
    is tagged", and only one place decides it.
    """
    runs = assemble_tag_runs(graph, route)
    if runs is None:
        return
    h.surface = summarise_surface(runs)
    h.tracktype = summarise_tracktype(runs)


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
    diagnostics: dict | None = None,
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

    ``diagnostics`` behaves exactly as in ``search_hikes`` — the two share the seam so a
    frontend that dispatches between them can ask the same question either way. It
    matters as much here: composing needs relations to build its graph from, so an area
    with none produces "no loops could be composed", which reads like the length band
    was wrong when nothing was ever there to compose.
    """
    cfg = cfg or _config.load()
    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )
    if diagnostics is not None:
        diagnostics["no_routes"] = area_has_no_routes(area)

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


# Cheap-pass width for --to-poi: how many candidate destinations survive the crow-flies
# sort, as a multiple of the routes asked for (never fewer than the floor). Each survivor
# costs a projection against every segment in ``snap_points`` plus one Dijkstra, so this is
# what keeps "--to-poi peak" in a summit-dense range from paying for fifty of them. The
# multiple exists because crow-flies rank is NOT trail rank — the nearest ruin as the crow
# flies can sit across a gorge with no trail to it — so a straight top-n would confidently
# hand back the wrong n. Whether the margin was wide enough is never assumed: it is checked
# against the first EXCLUDED candidate's crow-flies distance (see the certificate in
# ``routes_to_poi``), which is a lower bound on its trail distance.
_POI_CANDIDATE_FACTOR = 4
_POI_CANDIDATE_FLOOR = 10

# The fetched box is padded this much beyond the length cap. ``_bbox_around`` converts
# metres to degrees with 111 320 m/deg, while ``haversine_m`` — the metric every distance
# in this mode is actually measured with — puts a degree of latitude at ~111 195 m, so a
# box asked for "5 km" is ~4994 m by the ruler that matters. Nowhere near enough to matter
# in practice, but the pad here is carrying a COMPLETENESS argument ("no qualifying route
# can be clipped"), and an argument that is 0.1 % false is false. A 1 % margin buries the
# projection mismatch and float slop alike, for a fetched area 2 % larger. Same reasoning,
# same shape, as ``poi._CELL_MARGIN``.
_POI_PAD_MARGIN = 1.01


def _poi_route_cap_m(crow_m: float, criteria: Criteria, cfg: Config) -> float:
    """How long a route to a destination ``crow_m`` away in a straight line may run.

    ``--max-distance`` wins outright when given. Otherwise it is ``routes_max_factor`` x the
    straight-line distance (the ``routes_between`` rule, applied PER destination so a ruin
    400 m away can't justify a 15 km wander), with a ``routes_pad_km`` floor above that
    distance — without it an object 100 m off the trail would get a 300 m cap and read as
    unreachable, when the trail to it simply starts by heading the other way.

    Monotone non-decreasing in ``crow_m``, which is what lets the *search radius* stand in
    for the worst case: ``_poi_route_cap_m(search_radius, …)`` bounds the cap of every
    candidate, and so bounds every returned route's length. That is the whole basis of the
    fetched-bbox completeness argument in :func:`routes_to_poi`.
    """
    if criteria.max_distance_km is not None:
        return criteria.max_distance_km * 1000.0
    return max(cfg.routes_max_factor * crow_m, crow_m + cfg.routes_pad_km * 1000.0)


def routes_to_poi(
    start: Coord,
    kinds,
    criteria: Criteria,
    cfg: Config | None = None,
    *,
    n: int | None = None,
    search_radius_m: float | None = None,
    user_agent: str | None = None,
    overpass_url: str | None = None,
    elevation_mode: str | None = None,
    dem_dir: str | None = None,
) -> list[Hike]:
    """Routes from a picked ``start`` to the ``n`` nearest objects of ``kinds`` — "draw me
    a route to the nearest ruin".

    The inverse of ``criteria.poi_kinds``, which FILTERS routes that already exist by what
    they happen to pass. Here the object is the *destination*: candidates are found first,
    then a route is drawn to each. Nearest means nearest **along the trails**, not as the
    crow flies — one Dijkstra per candidate over the same graph the other point-based modes
    use — because "the nearest ruin" that needs a 9 km detour around a gorge is not the one
    you want to walk to.

    Like the other point-based modes it derives its own area, snaps everything onto the
    network, and measures the result through the *unchanged* ``find_hikes``, so offline ==
    online holds. Two properties are worth stating precisely, because "nearest" is a
    superlative and a superlative can be quietly wrong:

    * **The fetched box cannot clip a qualifying route.** A shortest path of length ``L``
      has every vertex within ``L`` of its start, so padding the box by the length cap
      (:func:`_poi_route_cap_m` at the search radius, the worst case) makes any route within
      that cap unclippable. This deliberately follows ``compose_loops_around``'s
      *provably-tight-pad* precedent rather than ``routes_between``'s accepted-clipping one:
      there, clipping loses an alternative and the user sees fewer routes; here it would
      silently promote the second-nearest ruin to "the nearest". The price is that
      ``--max-distance`` sizes the FETCH as well as capping the results — a heavy query
      when it is set high, exactly as ``--around``'s max-loop does.
    * **"Nearest" is checked, not asserted.** Straight-line distance is a lower bound on
      trail distance, so any object beyond the search radius has a trail distance beyond it
      too — and likewise for any candidate dropped by the cheap pass. When the last route
      returned is longer than both of those bounds, a nearer object *could* be hiding
      outside them, and that is said out loud rather than assumed away.

    Returns the routes sorted nearest-first by trail distance, each carrying its
    ``Hike.destination`` (with the measured distance from the route's end to the object —
    the route ends at the nearest point ON THE NETWORK, which is not the object itself).
    An empty result is never silent: the three causes — nothing of that kind mapped nearby,
    everything found sitting off-network, and nothing reachable within the cap — are
    distinguished in the log, because they need three different fixes.
    """
    cfg = cfg or _config.load()
    kinds = _poi.normalise_kinds(kinds)
    if not kinds:
        raise ValueError(
            "routes_to_poi needs at least one destination kind — pick from: "
            + ", ".join(sorted(_poi.POI_KINDS))
        )
    n = max(1, n if n is not None else cfg.routes_k)
    radius_m = (
        search_radius_m if search_radius_m is not None else cfg.poi_search_radius_m
    )
    # A drawn route is synthesised, not a mapped relation, so a stray `circular` shape filter
    # would drop it to nothing — neutralise it exactly as routes_between/route_via do, so all
    # three frontends behave identically. Distance/gain/access filters still apply.
    if criteria.circular is not None:
        criteria = replace(criteria, circular=None)

    # The pad IS the worst-case length cap — see the docstring: a route within the cap has
    # every vertex within the cap of the start, so it cannot be clipped out of this box.
    pad_m = _poi_route_cap_m(radius_m, criteria, cfg) * _POI_PAD_MARGIN
    bbox = _bbox_around(start, pad_m)
    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )

    # Candidates: registered objects of the asked-for kinds within the search radius. The
    # radius is measured crow-flies, which is a LOWER bound on the walk, so nothing inside
    # the true "nearest by trail" set can be excluded here without also being farther than
    # the radius on foot.
    labels = ", ".join(_poi.POI_KINDS[k].plural for k in kinds)
    scored = [
        (haversine_m(start, p["coord"]), p)
        for p in area.pois
        if p.get("kind") in kinds
    ]
    scored = [(d, p) for d, p in scored if d <= radius_m]
    if not scored:
        _log.warning(
            "route to POI: nothing of that kind (%s) is mapped within %.1f km of your point "
            "— raise the search radius (HIKE_POI_SEARCH_RADIUS_M / --to-poi-radius), or pick "
            "another kind. A miss means nothing of that kind is *mapped* in OSM here, not "
            "that nothing is there.",
            labels, radius_m / 1000.0,
        )
        return []
    # Deterministic cheap-pass order: distance, then coordinate/name, so an area with two
    # equidistant churches ranks them the same way every run.
    scored.sort(key=lambda dp: (dp[0], dp[1]["coord"], dp[1].get("name") or ""))
    keep = max(_POI_CANDIDATE_FACTOR * n, _POI_CANDIDATE_FLOOR)
    # The nearest candidate the cheap pass DROPPED, as a crow-flies (lower-bound) distance.
    # Nothing dropped can beat this on foot, which is what makes the certificate below sound.
    excluded_crow_m = scored[keep][0] if len(scored) > keep else math.inf
    candidates = scored[:keep]

    graph = build_trail_graph(clip_routes_to_bbox(area.routes, bbox))
    if not graph.segments:
        _log.warning("route to POI: no trails found in the area around your point")
        return []
    # Snap the start AND every candidate in ONE call, so points landing on the same segment
    # split it at every position at once (see compose.snap_points).
    graph, snapped = snap_points(graph, [start] + [p["coord"] for _d, p in candidates])
    (src, src_d), poi_snaps = snapped[0], snapped[1:]
    if src < 0:
        _log.warning("route to POI: no trails found in the area around your point")
        return []
    max_snap_m = cfg.routes_max_snap_km * 1000.0
    if src_d > max_snap_m:
        _log.warning(
            "route to POI: your start point is %.1f km from the nearest trail (limit %.1f km) "
            "— no routes drawn; move it closer to a marked trail or raise "
            "HIKE_ROUTES_MAX_SNAP_KM",
            src_d / 1000.0, cfg.routes_max_snap_km,
        )
        return []

    # One shortest path per candidate. `_dijkstra` is reused untouched (it is load-bearing for
    # Yen and route_via) and hands back the ordered segment ids `_assemble` needs, so a
    # per-candidate call is both the lower-risk and the smaller option next to a shortest-path
    # tree with its own path reconstruction.
    found: list[tuple[float, float, dict, list[int]]] = []  # (trail_m, snap_m, poi, segs)
    off_network = unreachable = too_far = at_start = 0
    for (crow_m, p), (node, snap_d) in zip(candidates, poi_snaps):
        if node < 0 or snap_d > max_snap_m:
            off_network += 1
            continue
        if node == src:
            at_start += 1  # you are already standing at it — there is no route to draw
            continue
        res = _dijkstra(graph, src, node)
        if res is None:
            unreachable += 1  # a gap in the network, or a different connected component
            continue
        segs, _nodes, length_m = res
        if not segs:
            at_start += 1
            continue
        if length_m > _poi_route_cap_m(crow_m, criteria, cfg):
            too_far += 1
            continue
        found.append((length_m, snap_d, p, segs))
    if not found:
        _log.warning(
            "route to POI: found %d %s within %.1f km, but none could be routed to "
            "(%d off-network, %d not connected to your start by any trail, %d only via a "
            "route longer than the cap, %d at your start point) — move the start onto a "
            "marked trail, raise --max-distance, or widen the search radius.",
            len(candidates), labels, radius_m / 1000.0,
            off_network, unreachable, too_far, at_start,
        )
        return []
    # Nearest ON FOOT first; coordinate/name break ties so the order is stable run to run.
    found.sort(key=lambda f: (f[0], f[2]["coord"], f[2].get("name") or ""))
    chosen = found[:n]

    # The certificate. Straight-line distance bounds trail distance from below, so the answer
    # is provably the true nearest-n only while the longest route returned stays inside BOTH
    # the search radius and the nearest dropped candidate. Past either, a nearer object may
    # be hiding outside what was looked at — which is said, not assumed away.
    worst_m = chosen[-1][0]
    bound_m = min(radius_m, excluded_crow_m)
    if worst_m > bound_m:
        _log.warning(
            "route to POI: the farthest route returned is %.1f km on foot, past the %.1f km "
            "%s — an object closer on foot may lie outside it, so these are the nearest "
            "*found*, not provably the nearest. Widen the search radius to be sure.",
            worst_m / 1000.0, bound_m / 1000.0,
            "search radius" if bound_m == radius_m else "nearest candidate not examined",
        )
    _log.warning(
        "route to POI: %d route(s) to the nearest %s (start snapped %.0f m to the network); "
        "trail distances %s",
        len(chosen), labels, src_d,
        ", ".join(f"{m / 1000.0:.1f} km" for m, _s, _p, _g in chosen),
    )
    # A SHORT answer is as quiet a failure as an empty one, and the mode exists to not be
    # quiet: the counters that explain an empty result explain a partial one too, so they
    # are reported whenever they are non-zero — not only when everything failed.
    reasons = ", ".join(
        f"{count} {why}"
        for count, why in (
            (off_network, "off-network"),
            (unreachable, "not connected to your start by any trail"),
            (too_far, "only via a route past the length cap"),
            (at_start, "at your start point already"),
        )
        if count
    )
    if reasons:
        _log.warning("route to POI: %s candidate(s) were skipped along the way.", reasons)
    if len(chosen) < n:
        _log.warning(
            "route to POI: %d route(s), not the %d you asked for — nothing else of that "
            "kind within %.1f km could be routed to; widen the search radius or raise "
            "--max-distance.",
            len(chosen), n, radius_m / 1000.0,
        )

    start_coord = graph.coords[src]
    routes = []
    for length_m, snap_d, p, segs in chosen:
        route = _assemble(graph, src, segs)
        # Render the route from where you picked, not from the assembled line's head.
        route.anchor = start_coord
        # Set from outside, like `anchor` — see the note in `_measure_composed`. The distance
        # is the object's own snap distance: the route ends at that snapped point on the
        # network, so this is exactly how far its end lands from the object.
        route.destination = _poi.PoiHit(
            kind=p["kind"], name=p.get("name"), coord=p["coord"], distance_m=snap_d
        )
        routes.append(route)

    provider = _provider(cfg, elevation_mode, dem_dir, cache)
    hikes = _measure_composed(
        graph, routes, area, criteria, cfg, provider, bbox,
        near_miss=False, roundtrip="no", name="Route to a point of interest",
    )
    for h in hikes:
        if h.destination is not None:
            # Name each route for what it was drawn to, so a list of them reads as a list of
            # destinations. The "how far the end lands from it" caveat rides on `destination`
            # and is rendered separately (format.format_hike) — never folded into the name.
            named = f' “{h.destination.name}”' if h.destination.name else ""
            h.name = f"Route to {h.destination.label}{named}"
    # Nearest-first on the FINAL measured distance, so the reported km is what the order
    # claims (graph length and measured length differ slightly — see routes_between).
    hikes.sort(key=lambda h: h.distance_km)
    return hikes


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
        # Empty criteria: no POI filter either, so the download samples every plausible
        # route regardless of what it passes. The snapshot carries the area's POIs
        # verbatim, and the *offline* search applies whichever POI filter is asked for
        # then — a snapshot is not specialised to one destination question.
        Criteria(),
        bbox=bbox,
        max_route_factor=cfg.max_route_factor,
        sample_interval_m=cfg.sample_interval_m,
        gain_threshold_m=cfg.gain_threshold_m,
        smooth_window=cfg.smooth_window,
        loop_tolerance_m=cfg.loop_tolerance_m,
        car_radius_m=cfg.car_radius_m,
        lift_radius_m=cfg.lift_radius_m,
        transit_rail_radius_m=cfg.transit_rail_radius_m,
        transit_stop_radius_m=cfg.transit_stop_radius_m,
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


_SNAPSHOT_NO_POIS = (
    "poi: this snapshot carries no points of interest (it predates the feature) — "
    "re-download the area to browse or export them offline; for now the listing can "
    "only be empty"
)


def area_has_no_routes(area: AreaData) -> bool:
    """True when an area carries no hiking route relations AT ALL.

    The one distinction an empty result cannot make on its own. "Your criteria excluded
    everything" and "nothing here is mapped as a hiking route" are different facts about
    different things — the second is about the map, not the search — and they take
    different fixes, so collapsing them into one sentence sends the user to widen a
    distance band that was never the problem.

    Not a hypothetical: measured over a ~400 km² box on Japan's North Alps (Kamikōchi),
    OSM carries **zero** `route=hiking`/`route=foot` relations, against 138 in the
    Krkonoše box the project was built on. The terrain there is mapped in detail — as
    individual ways, which this app does not read (see overpass.build_query) — so every
    search over it comes back empty and, before this, said so as though the filters were
    at fault.
    """
    return not area.routes


def no_routes_message() -> str:
    """The one sentence every frontend says when an area has no route relations.

    Shared for the same reason as ``snapshot_kinds_missing_message``: the CLI, the web UI
    and the MCP server are answering one question about one area, and three phrasings of
    "this is about the map, not your filters" is three chances for one of them to imply
    otherwise.

    Deliberately does NOT quote how many paths are mapped there instead, tempting as that
    is — the app never fetches `highway=path`, and adding it to the query to word an
    error message would widen every request and invalidate every cached area.
    """
    return (
        "No hiking route relations are mapped in that area — this is about the map data, "
        "not your filters. The search reads OSM route=hiking / route=foot relations, and "
        "some regions map their trails as individual paths without collecting them into "
        "route relations. Try a nearby area or a wider bounding box."
    )


def snapshot_poi_gap(snapshot: AreaSnapshot, kinds=()) -> tuple[str, tuple[str, ...] | None]:
    """How far a saved area can answer a question about ``kinds`` — one place decides.

    Returns ``(state, kinds)`` where *state* is one of:

    * ``"none"`` — the file predates points of interest entirely and carries no kind
      record either. It cannot answer anything; the second element is ``None``.
    * ``"missing"`` — it recorded which kinds it was classified against, and the named
      ones are not among them. They postdate the download and can only come back empty,
      which is a fact about the FILE.
    * ``"unknown"`` — it has objects but no kind record (saved between the POI feature
      and this one). Coverage is unverifiable, and that is worth saying whenever such a
      file is asked a POI question at all. Gating it on "only if the result came back
      empty" was tried and is subtly wrong: ``--poi ruins,tree`` against an unrecorded
      file returns the ruins and stays quiet, while ``tree`` — the kind that may never
      have been looked for — is exactly the half of the question nobody answered. A
      non-empty result proves only that SOME requested kind was classified. It is also
      the precedent already set here: the pre-POI and ``transit_access`` warnings fire on
      the filter being active, not on the result being empty.
    * ``"ok"`` — every requested kind was looked for. An empty result is about the
      landscape, and should read that way.

    The three frontends and both offline entry points all route through this, so "which
    of these four things is true of this file" is answered once. Splitting it per caller
    is how the *live* and *offline* paths would start disagreeing about the same file —
    the drift the shared ``poi.select_pois`` call already rules out for the listing
    itself.
    """
    area = snapshot.area
    if area.poi_kinds is None and not area.pois:
        return "none", None
    unrecorded = _poi.unrecorded_kinds(area.poi_kinds, kinds)
    if unrecorded is None:
        return "unknown", None
    if unrecorded:
        return "missing", unrecorded
    return "ok", ()


def snapshot_kinds_missing_message(gap: tuple[str, ...]) -> str:
    """The one sentence every frontend says about kinds a saved area postdates.

    Shared rather than reworded per frontend: the CLI, the web UI and the MCP server are
    all answering the same question about the same file, and three phrasings of "this is
    about the file, not the terrain" is three chances for one of them to imply otherwise.
    """
    named = ", ".join(gap)
    is_one = len(gap) == 1
    return (
        f"this downloaded area predates the "
        f"{'kind' if is_one else 'kinds'} {named} — it was never sorted into "
        f"{'it' if is_one else 'them'}, so {'it' if is_one else 'they'} can only come "
        f"back empty. That is a fact about the file, not the landscape: re-download the "
        f"area to ask it about {'this kind' if is_one else 'these kinds'}."
    )


_SNAPSHOT_KINDS_UNKNOWN = (
    "poi: this snapshot does not record which kinds it was downloaded with, so an empty "
    "result cannot be told apart from a kind that was never looked for — re-download the "
    "area (--download) if you expected something here"
)


def _warn_poi_gap(snapshot: AreaSnapshot, kinds=()) -> None:
    """Log the one thing worth saying about a saved area's POI coverage, if any.

    Nothing here is gated on whether the caller's result was empty. A non-empty result
    proves that *some* requested kind was classified, not all of them, so hiding the
    caveat behind one is how ``--poi ruins,tree`` comes back looking complete when the
    file never held a tree (see :func:`snapshot_poi_gap`).
    """
    state, gap = snapshot_poi_gap(snapshot, kinds)
    if state == "none":
        _log.warning(_SNAPSHOT_NO_POIS)
    elif state == "missing":
        _log.warning("poi: %s", snapshot_kinds_missing_message(gap or ()))
    elif state == "unknown":
        _log.warning(_SNAPSHOT_KINDS_UNKNOWN)


def area_records_ferrata(area: AreaData) -> bool:
    """True if this area was fetched by a build that asks Overpass for ferrata objects.

    Governs the FIND half only. Avoidance does not depend on it: a route this app can
    return is assembled from `route=hiking`/`route=foot` member ways, so a cabled section
    inside one is already described by that route's own `way_tags`, on any file carrying
    them — including files written before ferrata objects were ever fetched.
    """
    return area.ferrata_routes is not None or area.ferrata_ways is not None


def area_ferrata_readable(area: AreaData) -> bool:
    """True if the area's routes carry the member-way tags avoidance is measured from.

    A pre-surface snapshot has none, so every route measures `ferrata=None` and an active
    filter drops all of them. Returning an empty list for that is correct; returning it
    *silently* is not, which is what this predicate exists to prevent.

    Vacuously True when the area holds no routes at all — there is nothing to be unable
    to read, and `no_routes_message` already owns that case. Reporting "cannot detect
    ferrata" there would send a user to re-download an area whose real problem is that
    OSM maps no route relations in it.
    """
    if not area.routes:
        return True
    return any(r.get("way_tags") for r in area.routes)


def ferrata_unrecorded_message(*, avoidance_works: bool = True) -> str:
    """Said when a saved area is asked to FIND ferrata it never fetched.

    The closing promise — that avoidance still works — is only true because this message
    is reached solely via :func:`ferrata_gap_message`, which has already established that
    the file carries member-way tags. Said unconditionally it would be a lie on the
    oldest files, which carry neither (measured: `ceskyraj.json` is exactly such a file).

    ``avoidance_works=False`` drops that closing sentence, and exists for the ONE file the
    readable check waves through vacuously: an area holding no route relations at all has
    no member ways either, so there is nothing for avoidance to be measured from. The
    promise there is not false the way `ceskyraj`'s was — nothing would be silently
    dropped — but it is empty, and an "it still works" said about a file with nothing in
    it is the kind of sentence a client repeats as reassurance. The rest of the message is
    NOT dropped with it: `route=via_ferrata` relations are not hiking routes, so a
    re-download of an area with no hiking routes can still turn up cabled climbs, which is
    exactly what `no_routes_message` does not cover.
    """
    promise = (
        " AVOIDING them still works on this file, since that is measured from the member "
        "ways it already has."
        if avoidance_works
        else ""
    )
    return (
        "ferrata: this downloaded area predates cabled-route fetching — it holds no "
        "via ferrata relations or ways, so a search for them can only come back empty. "
        "That is a fact about the file, not the terrain: re-download the area to search "
        f"it for ferrata.{promise}"
    )


def ferrata_unreadable_message() -> str:
    """Said when neither ferrata question can be answered from an area's routes.

    The wording never implies safety. An area whose member-way tags were never fetched
    cannot distinguish a cabled route from a walkable one, and the honest report of that
    is "we cannot tell", not an empty result that reads as "nothing cabled here".
    """
    return (
        "ferrata: this area's routes carry no member-way tags, so cabled sections "
        "cannot be detected on them at all — this is NOT a report that the routes are "
        "free of cable. Re-download the area to ask either ferrata question of it."
    )


def ferrata_gap_message(area: AreaData, *, finding: bool) -> str | None:
    """The one sentence about what this area cannot answer about cable — or None.

    ONE place picks between the two messages, because they are not interchangeable and
    the ordering is what makes each true. A file with no member-way tags cannot answer
    EITHER question, so it gets the unreadable sentence whichever flag is set; only once
    that is ruled out is "avoidance still works here" a claim worth making. Getting this
    backwards printed exactly that promise over `ceskyraj.json`, which cannot honour it.

    ``finding`` distinguishes the two flags: `--ferrata` needs the fetched objects,
    `--no-ferrata` does not.

    The `avoidance_works` argument closes the one hole the ordering leaves open. An area
    with no route relations passes the readable check *vacuously* (by design — see
    :func:`area_ferrata_readable`), so it reaches the unrecorded message without ever
    having been shown to carry member-way tags, and collects a promise about measuring
    something it holds none of. Surfaced by reading an MCP reply, where this is the one
    place the two sentences land in a single paragraph: the CLI splits them across streams
    and the web UI across boxes.
    """
    if not area_ferrata_readable(area):
        return ferrata_unreadable_message()
    if finding and not area_records_ferrata(area):
        return ferrata_unrecorded_message(avoidance_works=bool(area.routes))
    return None


def ferrata_coverage_caveat() -> str:
    """The standing limit on avoidance, said wherever `--no-ferrata` is offered.

    Detection is a tag read, so it is exactly as complete as OSM's tagging. Every route
    the app can return is built from member ways whose tags we hold — that is what makes
    the filter complete over the route universe — but a cabled way carrying neither
    `highway=via_ferrata` nor `via_ferrata_scale` is invisible to any query, and no
    amount of fetching fixes it. Said plainly so the flag is never read as a guarantee.
    """
    return (
        "--no-ferrata drops routes KNOWN to include cabled sections, detected from OSM "
        "tags (highway=via_ferrata / via_ferrata_scale). Cable that nobody has tagged "
        "cannot be detected — treat this as a filter, not a safety guarantee."
    )


def list_area_ferrata(
    bbox: Bbox,
    cfg: Config | None = None,
    *,
    user_agent: str | None = None,
    overpass_url: str | None = None,
) -> tuple[_ferrata.FerrataLine, ...]:
    """Every cabled line in ``bbox`` — the ferrata inventory, live.

    "What's cabled around here", with no route drawn to any of it. Same economy as
    ``list_area_pois``: ONE cached Overpass call, no elevation provider constructed, so
    the mode spends nothing from the daily quota.
    """
    cfg = cfg or _config.load()
    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )
    return _ferrata.select_ferrata(area.ferrata_routes, area.ferrata_ways)


def list_snapshot_ferrata(snapshot: AreaSnapshot) -> tuple[_ferrata.FerrataLine, ...]:
    """Every cabled line in a saved area — the ferrata inventory, offline.

    Warns and returns empty on a file that predates the fetch, rather than letting the
    empty tuple pass for an answer about the landscape — the same distinction
    ``list_snapshot_pois`` draws, and for the same reason: a browse never goes through
    ``search_snapshot``, so it has to repeat the check rather than inherit it.
    """
    gap = ferrata_gap_message(snapshot.area, finding=True)
    if gap is not None:
        _log.warning("%s", gap)
    if not area_records_ferrata(snapshot.area):
        return ()
    return _ferrata.select_ferrata(
        snapshot.area.ferrata_routes, snapshot.area.ferrata_ways
    )


def list_area_pois(
    bbox: Bbox,
    kinds=(),
    cfg: Config | None = None,
    *,
    user_agent: str | None = None,
    overpass_url: str | None = None,
) -> tuple[_poi.PoiPlace, ...]:
    """Every registered object of ``kinds`` in ``bbox`` — the inventory, live.

    "Show me all the ruins around here", with no route drawn to any of them. The
    counterpart of ``--poi`` (which filters routes by what they pass) and of
    ``routes_to_poi`` (which draws a route to one): here the objects ARE the answer.

    Cheap by construction, and worth stating because every other mode in this module is
    not: ONE Overpass call, transparently cached, and **no elevation provider is ever
    constructed** — so the mode spends nothing from the daily elevation quota and needs
    no DEM. Nothing is measured because nothing is walked.

    ``kinds`` empty means every registered kind (see ``poi.select_pois``); an unknown kind
    raises ``ValueError`` naming it. Results are unclipped, deterministic and de-duplicated
    — all three properties belong to ``select_pois``, which the offline path below calls
    identically, so offline == live here is a shared call rather than a claim.
    """
    cfg = cfg or _config.load()
    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )
    # Deliberately silent on success. Every frontend already prints the count-by-kind
    # summary, and a log line here would appear on the live path only — making the live
    # and offline listings differ in what the USER sees, which is exactly the kind of
    # drift the shared `select_pois` call exists to rule out.
    return _poi.select_pois(area.pois, kinds)


def list_snapshot_pois(snapshot: AreaSnapshot, kinds=()) -> tuple[_poi.PoiPlace, ...]:
    """Every registered object of ``kinds`` in a saved area — the inventory, offline.

    Zero network. The snapshot stores ``area.pois`` verbatim (never filtered to whatever
    the download was interested in), so the same selection runs against the same shape of
    data as the live path, through the same ``poi.select_pois``.

    A snapshot downloaded before POIs existed carries none, and "this area has no ruins"
    must never be how that reads. ``search_snapshot`` already draws that distinction for
    the *filter*; the listing repeats it rather than inheriting it, because a browse never
    goes through ``search_snapshot`` and would otherwise print a confident empty list for
    a file that simply cannot know.

    The same hazard has a *finer* form the emptiness check cannot see: a file downloaded
    when the registry held 19 kinds carries plenty of POIs and none of the nine added
    after it, so a browse for one of those returns a confident empty list from a question
    the download never asked. ``snapshot_poi_gap`` is what tells the two apart — the file
    records the kind set it was classified against, and the diff is taken against the
    registry rather than guessed from which kinds happen to appear in the data (absence
    there is exactly the ambiguity being resolved, so reading it as evidence would be
    circular).
    """
    if snapshot_poi_gap(snapshot, kinds)[0] == "none":
        _log.warning(_SNAPSHOT_NO_POIS)
        return ()
    places = _poi.select_pois(snapshot.area.pois, kinds)
    _warn_poi_gap(snapshot, kinds)
    return places


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
    # A snapshot downloaded before POIs existed carries none, so a POI filter would
    # match nothing — and "no churches here" must never be confused with "this file
    # doesn't know about churches". Say which it is, loudly, before returning empty.
    if criteria.poi_kinds and snapshot_poi_gap(snapshot, criteria.poi_kinds)[0] == "none":
        _log.warning(
            "poi: this snapshot carries no points of interest (it predates the feature) "
            "— re-download the area to search it for %s offline; for now the filter can "
            "only return nothing",
            ", ".join(criteria.poi_kinds),
        )
    elif criteria.poi_kinds:
        # The finer gaps: the file HAS points of interest but was classified against an
        # older registry (or records no registry at all), so a filter on a kind added
        # since matches nothing for a reason that is about the file, not the terrain.
        # Said BEFORE the search, like the two warnings around it, and not gated on the
        # result — `find_hikes` does not relax `poi_kinds` for near-misses either, so an
        # empty result is not the only shape this failure takes (see `_warn_poi_gap`).
        _warn_poi_gap(snapshot, criteria.poi_kinds)
    # Same hazard, sharper: an unanswerable transit filter would otherwise return a
    # confident verdict. `find_hikes` already drops every route whose transit is
    # unknown, so the result is empty either way — this is what stops that emptiness
    # from reading as "nowhere here is reachable by public transport".
    if criteria.transit_access is not None and snapshot.area.transit is None:
        _log.warning(
            "transit: this snapshot has no public-transport data (it predates the "
            "feature), so the transit filter cannot be answered from it — re-download "
            "the area. Returning nothing rather than labelling every route unreachable"
        )
    # Ferrata, and the two halves warn on DIFFERENT conditions — the asymmetry is the
    # whole point (see ferrata.py). Finding them needs the fetched objects, so a file
    # predating that fetch cannot answer. Avoiding them needs only member-way tags, so
    # the same file answers fine; what breaks avoidance is a file with no member tags at
    # all, and that breaks finding too. Neither warning is gated on the result being
    # empty: `find_hikes` drops unknown routes, so an empty result is the *symptom* being
    # explained, not the trigger.
    if criteria.ferrata is not None:
        gap = ferrata_gap_message(snapshot.area, finding=criteria.ferrata is True)
        if gap is not None:
            _log.warning("%s", gap)
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
        transit_rail_radius_m=cfg.transit_rail_radius_m,
        transit_stop_radius_m=cfg.transit_stop_radius_m,
        near_miss=near_miss,
        poi_radius_m=cfg.poi_radius_m,
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
