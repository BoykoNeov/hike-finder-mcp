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

from . import cache as _cache
from . import config as _config
from .compose import (
    assemble_loop_series,
    build_trail_graph,
    clip_routes_to_bbox,
    find_loops,
    resample_segments,
)
from .config import Config
from .elevation import ElevationError, get_provider
from .filters import Criteria, Hike, find_hikes
from .geocode import DEFAULT_NOMINATIM_URL, NominatimGeocoder
from .naming import enrich_names
from .overpass import AreaData, DEFAULT_OVERPASS_URL, build_query, fetch_area
from .snapshot import AreaSnapshot, RecordingElevationProvider, SnapshotElevationProvider

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
    min_km = criteria.min_distance_km if criteria.min_distance_km is not None else cfg.compose_min_km
    max_km = (
        criteria.max_distance_km
        if criteria.max_distance_km is not None
        else max(cfg.compose_max_km, min_km)
    )

    # Access anchoring: when the search requires car/lift access, restrict the composed
    # loops to those reachable from a matched parking lot / lift station BEFORE the cap,
    # and start each at that trailhead — "a 12 km loop from where I park". The
    # requirement set mirrors find_hikes (the SAME radii and access-point sets, AND-ed
    # across the requested types), so the loops kept here are exactly the ones find_hikes
    # accepts. Parking is listed first, so a loop with both car and lift access starts
    # where you park, not at the lift.
    anchors: list[tuple[list, float]] = []
    if criteria.car_access is True:
        anchors.append(([p["coord"] for p in area.parking], cfg.car_radius_m))
    if criteria.chairlift_access is True:
        anchors.append(
            ([s for lift in area.lifts for s in lift.get("stations", [])], cfg.lift_radius_m)
        )
    anchored = bool(anchors)

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
    # and whether the bounded search hit its budget — so a truncated/capped result is
    # never mistaken for "that's all there is". When anchoring, also surface the
    # accessible-vs-found funnel, so a filtered-down result isn't read as "that's all
    # the loops there are".
    truncated = (
        f" (showing the {len(result.loops)} most loop-like; raise HIKE_COMPOSE_MAX_LOOPS for more)"
        if result.distinct > len(result.loops) else ""
    )
    capped_note = (
        " [cycle search capped — results may be incomplete; narrow the distance band]"
        if result.capped else ""
    )
    # Never silent: report how many in-band cycles the compactness floor dropped as
    # degenerate slivers (out-and-backs along near-parallel trails), so a filtered-down
    # result isn't mistaken for "that's all there is".
    sliver_note = (
        f" ({result.slivered} thin sliver(s) dropped below compactness "
        f"{cfg.compose_min_compactness:g})"
        if result.slivered else ""
    )
    if anchored:
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

    provider = _provider(cfg, elevation_mode, dem_dir, cache)

    # Segment-level shared elevation sampling. Composed loops overlap — several share
    # the same marked-trail segments — so resampling and looking up each WHOLE loop
    # (the obvious `find_hikes` reuse) pays for a shared climb once per loop that uses
    # it. Instead, resample each DISTINCT used segment once on its own canonical grid
    # and look the WHOLE distinct-point set up in ONE provider call, then slice the
    # results back per segment. Loops then assemble their elevation series from those
    # shared per-segment results (`assemble_loop_series`). This dedups within the run
    # (measured ~2-3x fewer points -> the same factor fewer batched API requests on a
    # real bbox, since the request count is what the throttle and daily quota meter) AND
    # makes the points cache-hot across runs, because a segment's canonical samples are
    # identical regardless of which loop enters it where — whereas a whole-loop resample
    # seam shifts per loop, so today's cross-run cache barely helps compose.
    #
    # One combined lookup (not one per segment) is deliberate: the API batches 100
    # points/request, so packing all distinct points into a single call costs
    # ~ceil(total/100) requests instead of >= one per segment. It is all-or-nothing on
    # failure — a mid-run quota exhaustion fails the whole batch and every loop degrades
    # to gain n/a (graceful, never a ban) — but that is rare (a default run is tens of
    # requests, far under the daily cap) and a quota-dead compose is mostly n/a anyway.
    used_segs = (
        sorted(set().union(*(L.seg_ids for L in result.loops))) if result.loops else []
    )
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

    # Wrap each loop as a synthetic route and run the SAME engine. The negative id keys
    # the loop back to its provenance after find_hikes (which preserves osm_id per Hike).
    # Each loop's elevation series is pre-assembled from the shared per-segment lookups
    # above and handed to find_hikes, so its elevation pass skips the redundant whole-
    # loop resample/lookup and just runs the (unchanged) gain math on the series.
    loop_by_id: dict[int, object] = {}
    syn_routes: list[dict] = []
    pre_elev_by_id: dict[int, list[float]] = {}
    pre_points_by_id: dict[int, list] = {}
    for i, loop in enumerate(result.loops):
        sid = -(i + 1)
        loop_by_id[sid] = loop
        syn_routes.append(
            {
                "id": sid,
                "name": "Composed loop",
                "ref": None,
                "osmc_color": None,
                "tags": {"roundtrip": "yes"},
                "ways": [loop.coords],
            }
        )
        series = assemble_loop_series(graph, loop, seg_elev)
        if series is not None:
            pre_elev_by_id[sid] = series
            # The resampled points behind that series, assembled identically (same
            # traversal, same junction-dedup), so they align 1:1 with the elevations
            # and let find_hikes record a per-point `track` for the GPS export. Built
            # only when the series exists (elevation succeeded) — a degraded loop has
            # no usable track anyway.
            pre_points_by_id[sid] = assemble_loop_series(graph, loop, seg_points)
    syn_area = AreaData(routes=syn_routes, parking=area.parking, lifts=area.lifts)
    hikes = find_hikes(
        syn_area,
        provider,
        criteria,
        bbox=bbox,
        # Composed loops are already clipped to the bbox and bounded by the length band,
        # so the through-route over-length guard (meant for relations that merely cross
        # the area) doesn't apply — disabling it avoids wrongly dropping a big in-area loop.
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
        loop = loop_by_id.get(h.osm_id)
        if loop is not None:
            h.composed = True
            h.composed_of = loop.refs
            if loop.anchor is not None:
                # Access-anchored: put the start at the trailhead you drive/ride to (the
                # on-loop point nearest your parking/lift), not the loop's arbitrary
                # geometric head. We override the label only — the loop's `coords` stay
                # unrotated, so the elevation resample seam, and thus gain/loss, is
                # byte-identical to an unanchored run (a loop's start is just a marker;
                # no filter reads it).
                h.start = loop.anchor
    return hikes


def download_area(
    bbox: Bbox,
    cfg: Config | None = None,
    *,
    user_agent: str | None = None,
    overpass_url: str | None = None,
    elevation_mode: str | None = None,
    dem_dir: str | None = None,
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
    return AreaSnapshot(
        bbox=tuple(bbox),
        area=area,
        elevations=recorder.samples,
        sample_interval_m=cfg.sample_interval_m,
        user_agent=user_agent or cfg.overpass_user_agent,
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
    # Reverse-geocode naming needs the network, which an offline snapshot search
    # deliberately never touches — so honour the offline==online promise loudly: log
    # that the request is a no-op here rather than silently dropping it. (v2: record
    # place names into the snapshot at download time, like elevations, for parity.)
    if _wants_geocode(name_places, cfg):
        _log.warning(
            "name_places: reverse-geocode naming needs the network and is skipped for "
            "an offline --area search; unnamed routes keep their route/<id> label"
        )
    provider = SnapshotElevationProvider(snapshot.elevations)
    return find_hikes(
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
