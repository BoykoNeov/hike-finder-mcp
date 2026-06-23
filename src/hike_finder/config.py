"""Runtime config, read from environment variables.

  HIKE_ELEVATION_MODE   api | local | auto   (default: auto)
  HIKE_DEM_DIR          path to GeoTIFF DEM tiles (for local/auto)
  HIKE_API_ENDPOINT     override elevation API endpoint
  HIKE_OVERPASS_URL     override Overpass endpoint
  HIKE_OVERPASS_UA      User-Agent for Overpass (REQUIRED by the public server;
                        set a real contact per OSM etiquette)
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
    gain_threshold_m: float = float(os.getenv("HIKE_GAIN_THRESHOLD", "10"))
    sample_interval_m: float = float(os.getenv("HIKE_SAMPLE_INTERVAL", "25"))
    smooth_window: int = int(os.getenv("HIKE_SMOOTH_WINDOW", "3"))

    loop_tolerance_m: float = float(os.getenv("HIKE_LOOP_TOLERANCE", "150"))
    car_radius_m: float = float(os.getenv("HIKE_CAR_RADIUS", "300"))
    lift_radius_m: float = float(os.getenv("HIKE_LIFT_RADIUS", "400"))
    max_route_factor: float = float(os.getenv("HIKE_MAX_ROUTE_FACTOR", "4.0"))


def load() -> Config:
    return Config()
