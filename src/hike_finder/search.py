"""Orchestration shared by every frontend (CLI, web UI, MCP server).

One place that wires the three runtime pieces together — fetch OSM data, pick an
elevation provider, run the two-pass filter — so the frontends stay thin and
behave identically. The pure math lives in its own modules; this is the glue that
touches the network.
"""
from __future__ import annotations

from . import config as _config
from .config import Config
from .elevation import get_provider
from .filters import Criteria, Hike, find_hikes
from .overpass import DEFAULT_OVERPASS_URL, fetch_area

Bbox = tuple[float, float, float, float]


def search_hikes(
    bbox: Bbox,
    criteria: Criteria,
    cfg: Config | None = None,
    *,
    user_agent: str | None = None,
    overpass_url: str | None = None,
    elevation_mode: str | None = None,
    dem_dir: str | None = None,
) -> list[Hike]:
    """Fetch OSM data for ``bbox`` and return measured, filtered hikes.

    ``bbox`` is ``(south, west, north, east)``. Keyword overrides (used by the
    CLI's flags / the web form) win over ``cfg``; ``cfg`` defaults to the
    environment (see config.py).
    """
    cfg = cfg or _config.load()
    area = fetch_area(
        *bbox,
        overpass_url or cfg.overpass_url or DEFAULT_OVERPASS_URL,
        user_agent=user_agent or cfg.overpass_user_agent,
    )
    provider = get_provider(
        mode=elevation_mode or cfg.elevation_mode,
        dem_dir=dem_dir or cfg.dem_dir,
        api_endpoint=cfg.api_endpoint,
        api_min_interval_s=cfg.api_min_interval_s,
        api_max_retries=cfg.api_max_retries,
        api_backoff_s=cfg.api_backoff_s,
    )
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
    )
