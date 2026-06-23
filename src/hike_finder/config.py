"""Runtime config, read from environment variables.

  HIKE_ELEVATION_MODE   api | local | auto   (default: auto)
  HIKE_DEM_DIR          path to GeoTIFF DEM tiles (for local/auto)
  HIKE_API_ENDPOINT     override elevation API endpoint
  HIKE_OVERPASS_URL     override Overpass endpoint
  HIKE_OVERPASS_UA      User-Agent for Overpass (REQUIRED by the public server;
                        set a real contact per OSM etiquette)
  HIKE_API_MIN_INTERVAL seconds between elevation-API requests (default 1.1;
                        keeps us under the public ~1 req/sec limit)
  HIKE_API_MAX_RETRIES  retries on transient API errors 429/5xx/network (default 3)
  HIKE_API_BACKOFF      backoff base seconds, doubled each retry (default 2.0)
  HIKE_API_MAX_BACKOFF  cap on any single wait, seconds; a Retry-After above this
                        (e.g. a daily-quota 429) makes us give up, not stall
                        (default 30)
  HIKE_API_DAILY_LIMIT  max elevation-API requests per UTC day, counted across
                        runs in a persistent file; at the limit we degrade routes
                        to n/a instead of getting banned. 0 disables (default 1000)
  HIKE_API_STATE_DIR    directory for the daily-counter file (default: a per-user
                        cache dir — %LOCALAPPDATA%/hike-finder or ~/.cache/hike-finder)
  HIKE_GAIN_THRESHOLD   metres (default 10)
  HIKE_SAMPLE_INTERVAL  metres (default 25)
  HIKE_SMOOTH_WINDOW    samples (default 3)

  HIKE_LOOP_TOLERANCE   metres; start≈end closes a loop (default 150)
  HIKE_CAR_RADIUS       metres; parking within this of an endpoint = car access (default 300)
  HIKE_LIFT_RADIUS      metres; lift station within this of an endpoint = lift access (default 400)
  HIKE_MAX_ROUTE_FACTOR drop routes longer than factor x bbox diagonal (default 4.0).
                        Guards against through-routes (national trails) that merely
                        cross the area being returned with their full geometry.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    elevation_mode: str = os.getenv("HIKE_ELEVATION_MODE", "auto")
    dem_dir: str | None = os.getenv("HIKE_DEM_DIR")
    api_endpoint: str | None = os.getenv("HIKE_API_ENDPOINT")
    overpass_url: str | None = os.getenv("HIKE_OVERPASS_URL")
    overpass_user_agent: str | None = os.getenv("HIKE_OVERPASS_UA")
    api_min_interval_s: float = float(os.getenv("HIKE_API_MIN_INTERVAL", "1.1"))
    api_max_retries: int = int(os.getenv("HIKE_API_MAX_RETRIES", "3"))
    api_backoff_s: float = float(os.getenv("HIKE_API_BACKOFF", "2.0"))
    api_max_backoff_s: float = float(os.getenv("HIKE_API_MAX_BACKOFF", "30.0"))
    api_daily_limit: int = int(os.getenv("HIKE_API_DAILY_LIMIT", "1000"))
    api_state_dir: str | None = os.getenv("HIKE_API_STATE_DIR")
    gain_threshold_m: float = float(os.getenv("HIKE_GAIN_THRESHOLD", "10"))
    sample_interval_m: float = float(os.getenv("HIKE_SAMPLE_INTERVAL", "25"))
    smooth_window: int = int(os.getenv("HIKE_SMOOTH_WINDOW", "3"))

    loop_tolerance_m: float = float(os.getenv("HIKE_LOOP_TOLERANCE", "150"))
    car_radius_m: float = float(os.getenv("HIKE_CAR_RADIUS", "300"))
    lift_radius_m: float = float(os.getenv("HIKE_LIFT_RADIUS", "400"))
    max_route_factor: float = float(os.getenv("HIKE_MAX_ROUTE_FACTOR", "4.0"))


def load() -> Config:
    return Config()
