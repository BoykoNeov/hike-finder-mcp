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
from .compose import build_trail_graph, clip_routes_to_bbox, find_loops
from .config import Config
from .elevation import get_provider
from .filters import Criteria, Hike, find_hikes
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
) -> list[Hike]:
    """Fetch OSM data for ``bbox`` and return measured, filtered hikes.

    ``bbox`` is ``(south, west, north, east)``. Keyword overrides (used by the
    CLI's flags / the web form) win over ``cfg``; ``cfg`` defaults to the
    environment (see config.py).
    """
    cfg = cfg or _config.load()
    cache = _cache.from_config(cfg)
    area = _fetch_area(
        bbox, cfg, cache, user_agent=user_agent, overpass_url=overpass_url, read_cache=True
    )
    provider = _provider(cfg, elevation_mode, dem_dir, cache)
    return find_hikes(
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
    result = find_loops(
        graph,
        min_m=min_km * 1000.0,
        max_m=max_km * 1000.0,
        max_segments=cfg.compose_max_segments,
        max_loops=cfg.compose_max_loops,
        overlap_frac=cfg.compose_overlap_frac,
    )
    # Logged, never silent: how many distinct loops exist vs how many we elevation+show,
    # and whether the bounded search hit its budget — so a truncated/capped result is
    # never mistaken for "that's all there is".
    truncated = (
        f" (showing the {len(result.loops)} most loop-like; raise HIKE_COMPOSE_MAX_LOOPS for more)"
        if result.distinct > len(result.loops) else ""
    )
    _log.warning(
        "compose: %d distinct loop(s) in %.0f-%.0f km from %d trail segments%s%s",
        result.distinct, min_km, max_km, len(graph.segments), truncated,
        " [cycle search capped — results may be incomplete; narrow the distance band]"
        if result.capped else "",
    )

    # Wrap each loop as a synthetic route and run the SAME engine. The negative id keys
    # the loop back to its provenance after find_hikes (which preserves osm_id per Hike).
    loop_by_id: dict[int, object] = {}
    syn_routes: list[dict] = []
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
    syn_area = AreaData(routes=syn_routes, parking=area.parking, lifts=area.lifts)
    provider = _provider(cfg, elevation_mode, dem_dir, cache)
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
        **_near_miss_kwargs(cfg),
    )
    for h in hikes:
        loop = loop_by_id.get(h.osm_id)
        if loop is not None:
            h.composed = True
            h.composed_of = loop.refs
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
