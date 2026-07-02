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

  HIKE_NEAR_MISS_GAIN_FRAC   near-miss gain tolerance, fraction of the bound
                             (default 0.2 -> within 20% of a min/max gain)
  HIKE_NEAR_MISS_DIST_KM     near-miss distance tolerance, km past a min/max (default 2.0)
  HIKE_NEAR_MISS_RADIUS_FRAC near-miss access tolerance: parking/lift within
                             radius x (1 + this) still counts (default 0.5)
  HIKE_SNAPSHOT_DIR     directory for named area snapshots saved by the web UI
                        (default: a per-user cache subdir, .../hike-finder/snapshots)

  HIKE_CACHE            transparent on-disk cache of Overpass + elevation results,
                        on by default. Set 0/false/no/off to disable (--no-cache).
                        Spares the public servers on repeat/overlapping searches.
  HIKE_CACHE_DIR        directory for the cache SQLite file (default: the same
                        per-user cache dir as the quota counter, .../hike-finder)
  HIKE_OVERPASS_CACHE_TTL_DAYS  how long a cached Overpass area stays fresh, days
                        (default 30; trails change slowly). 0 disables Overpass
                        caching (elevation, being immutable terrain, is never TTL'd).

  HIKE_GEOCODE          opt-in reverse-geocode naming of UNNAMED routes (route/<id>)
                        from place names via Nominatim. Off by default (Nominatim's
                        policy is strict); a frontend flag turns it on per search.
  HIKE_NOMINATIM_URL    override the Nominatim reverse endpoint (self-host for heavy use)
  HIKE_NOMINATIM_MIN_INTERVAL  min seconds between Nominatim requests (default 1.1;
                        the public server caps at ~1 req/sec)
  HIKE_GEOCODE_CACHE_TTL_DAYS  how long a cached place name stays fresh, days
                        (default 365; place names change slowly). 0 disables.

  HIKE_COMPOSE_MIN_KM   compose mode: default min loop length when no --min-distance (3)
  HIKE_COMPOSE_MAX_KM   compose mode: default max loop length when no --max-distance (15)
  HIKE_COMPOSE_MAX_SEGMENTS  compose mode: max trail segments per composed loop (12)
  HIKE_COMPOSE_OVERLAP_FRAC  compose mode: drop a loop sharing more than this fraction
                        of its length with an already-kept loop (0.6)
  HIKE_COMPOSE_MAX_LOOPS  compose mode: max loops returned, ranked by compactness;
                        bounds the per-loop elevation cost (15)
  HIKE_COMPOSE_MIN_COMPACTNESS  compose mode: drop a loop below this compactness
                        (4πA/P²) — a degenerate thin sliver, not a real loop (0.05).
                        0 disables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no", "off", "")


def _f(name: str, default: str):
    """``field`` factory that reads ``float(os.getenv(name, default))`` at
    instantiation — see the module-level note on why env is read in ``load()``."""
    return field(default_factory=lambda: float(os.getenv(name, default)))


def _i(name: str, default: str):
    return field(default_factory=lambda: int(os.getenv(name, default)))


def _s(name: str, default: str | None = None):
    return field(default_factory=lambda: os.getenv(name, default))


def _b(name: str, default: bool):
    return field(default_factory=lambda: _env_bool(name, default))


# Env is read when a ``Config`` is *instantiated* (via ``load()``), not when this
# module is imported: each field uses ``default_factory`` so ``os.getenv`` runs
# per ``Config()``. This keeps env changes made after import (tests, launchers)
# visible. ``cache.cache_path_from_config`` and ``elevation.quota`` also re-read
# their own env vars live as belt-and-suspenders.
@dataclass
class Config:
    elevation_mode: str = _s("HIKE_ELEVATION_MODE", "auto")
    dem_dir: str | None = _s("HIKE_DEM_DIR")
    api_endpoint: str | None = _s("HIKE_API_ENDPOINT")
    overpass_url: str | None = _s("HIKE_OVERPASS_URL")
    overpass_user_agent: str | None = _s("HIKE_OVERPASS_UA")
    api_min_interval_s: float = _f("HIKE_API_MIN_INTERVAL", "1.1")
    api_max_retries: int = _i("HIKE_API_MAX_RETRIES", "3")
    api_backoff_s: float = _f("HIKE_API_BACKOFF", "2.0")
    api_max_backoff_s: float = _f("HIKE_API_MAX_BACKOFF", "30.0")
    api_daily_limit: int = _i("HIKE_API_DAILY_LIMIT", "1000")
    api_state_dir: str | None = _s("HIKE_API_STATE_DIR")
    gain_threshold_m: float = _f("HIKE_GAIN_THRESHOLD", "10")
    sample_interval_m: float = _f("HIKE_SAMPLE_INTERVAL", "25")
    smooth_window: int = _i("HIKE_SMOOTH_WINDOW", "3")

    loop_tolerance_m: float = _f("HIKE_LOOP_TOLERANCE", "150")
    car_radius_m: float = _f("HIKE_CAR_RADIUS", "300")
    lift_radius_m: float = _f("HIKE_LIFT_RADIUS", "400")
    max_route_factor: float = _f("HIKE_MAX_ROUTE_FACTOR", "4.0")

    near_miss_gain_frac: float = _f("HIKE_NEAR_MISS_GAIN_FRAC", "0.2")
    near_miss_dist_km: float = _f("HIKE_NEAR_MISS_DIST_KM", "2.0")
    near_miss_radius_frac: float = _f("HIKE_NEAR_MISS_RADIUS_FRAC", "0.5")

    cache_enabled: bool = _b("HIKE_CACHE", True)
    cache_dir: str | None = _s("HIKE_CACHE_DIR")
    overpass_cache_ttl_days: float = _f("HIKE_OVERPASS_CACHE_TTL_DAYS", "30")

    # Reverse-geocode naming (opt-in; see geocode.py / naming.py). OFF by default
    # because Nominatim's policy is strict (1 req/s, no bulk) — a frontend flag turns
    # it on per search. The Overpass contact UA is reused for Nominatim. Place names
    # change slowly, so the cache TTL is long (a year); 0 disables geocode caching.
    geocode_enabled: bool = _b("HIKE_GEOCODE", False)
    nominatim_url: str | None = _s("HIKE_NOMINATIM_URL")
    nominatim_min_interval_s: float = _f("HIKE_NOMINATIM_MIN_INTERVAL", "1.1")
    geocode_cache_ttl_days: float = _f("HIKE_GEOCODE_CACHE_TTL_DAYS", "365")

    # Loop composition (compose.py): default target length band when the user gives no
    # --min/--max-distance, plus the cycle-search bounds (segments per loop, near-
    # duplicate overlap fraction). The expansion budget is an internal runaway guard.
    compose_min_km: float = _f("HIKE_COMPOSE_MIN_KM", "3")
    compose_max_km: float = _f("HIKE_COMPOSE_MAX_KM", "15")
    compose_max_segments: int = _i("HIKE_COMPOSE_MAX_SEGMENTS", "12")
    compose_overlap_frac: float = _f("HIKE_COMPOSE_OVERLAP_FRAC", "0.6")
    # Cap on composed loops returned (ranked by compactness). Bounds the elevation cost
    # — the caller looks up elevation per returned loop — and keeps the list manageable.
    compose_max_loops: int = _i("HIKE_COMPOSE_MAX_LOOPS", "15")
    # Sliver filter: drop a composed loop whose Polsby–Popper compactness (4πA/P²) is
    # below this floor — a degenerate near-zero-area out-and-back along two near-parallel
    # trails, not a real loop. 0.05 is a safe floor: real marked-trail loops sit well
    # above it (observed ≥ 0.18 on a dense real bbox), so this drops nothing on real data
    # while guarding the degenerate case. 0 disables the filter.
    compose_min_compactness: float = _f("HIKE_COMPOSE_MIN_COMPACTNESS", "0.05")


def load() -> Config:
    return Config()
