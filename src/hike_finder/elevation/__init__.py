"""Elevation backend selection.

Both options are first-class, per the design:
  - "api":   ApiElevationProvider  (zero setup, rate-limited, coarser)
  - "local": LocalDemElevationProvider (accurate/fast, needs DEM tiles)
  - "auto":  try local DEM, fall back to API on any ElevationError

Configure via env (see config.py) or pass explicitly.
"""
from __future__ import annotations

from .base import Coord, ElevationError, ElevationProvider
from .api import ApiElevationProvider
from .gain import cumulative_gain_loss
from .local_dem import LocalDemElevationProvider

__all__ = [
    "Coord",
    "ElevationError",
    "ElevationProvider",
    "ApiElevationProvider",
    "LocalDemElevationProvider",
    "cumulative_gain_loss",
    "get_provider",
    "FallbackElevationProvider",
]


class FallbackElevationProvider(ElevationProvider):
    """Try providers in order; use the first that succeeds for the whole set."""

    def __init__(self, providers: list[ElevationProvider]):
        if not providers:
            raise ValueError("need at least one provider")
        self.providers = providers

    def lookup(self, points):
        last_err = None
        for p in self.providers:
            try:
                return p.lookup(points)
            except ElevationError as e:
                last_err = e
        raise ElevationError(f"all elevation providers failed: {last_err}")


def get_provider(
    mode: str = "auto",
    dem_dir: str | None = None,
    api_endpoint: str | None = None,
) -> ElevationProvider:
    api_kwargs = {"endpoint": api_endpoint} if api_endpoint else {}
    if mode == "api":
        return ApiElevationProvider(**api_kwargs)
    if mode == "local":
        if not dem_dir:
            raise ValueError("mode='local' requires dem_dir")
        return LocalDemElevationProvider(dem_dir)
    if mode == "auto":
        chain = []
        if dem_dir:
            try:
                chain.append(LocalDemElevationProvider(dem_dir))
            except ElevationError:
                pass  # no tiles / no rasterio -> just use API
        chain.append(ApiElevationProvider(**api_kwargs))
        return FallbackElevationProvider(chain)
    raise ValueError(f"unknown elevation mode: {mode!r}")
