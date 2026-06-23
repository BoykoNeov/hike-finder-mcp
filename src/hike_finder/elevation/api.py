"""API-based elevation: Open-Elevation or OpenTopoData (configurable).

Zero setup, but rate-limited and coarser. Good for getting started and for
areas where you don't want to host DEM tiles.

OpenTopoData public endpoint allows 100 locations/request and ~1 req/sec.
Open-Elevation has a similar shape. Both accept POST with JSON locations.

NOTE: not runnable in the build sandbox (network is restricted to package
registries). Test against the live endpoint on your own machine.
"""
from __future__ import annotations

import time

import requests

from .base import Coord, ElevationError, ElevationProvider

# OpenTopoData datasets: "srtm30m" (global, 30 m), "aster30m", "mapzen", etc.
DEFAULT_ENDPOINT = "https://api.opentopodata.org/v1/srtm30m"
OPEN_ELEVATION_ENDPOINT = "https://api.open-elevation.com/api/v1/lookup"


class ApiElevationProvider(ElevationProvider):
    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        batch_size: int = 100,
        sleep_between_batches_s: float = 1.0,
        timeout_s: float = 30.0,
    ):
        self.endpoint = endpoint
        self.batch_size = batch_size
        self.sleep = sleep_between_batches_s
        self.timeout = timeout_s

    def lookup(self, points: list[Coord]) -> list[float]:
        out: list[float] = []
        for i in range(0, len(points), self.batch_size):
            batch = points[i : i + self.batch_size]
            out.extend(self._lookup_batch(batch))
            if i + self.batch_size < len(points):
                time.sleep(self.sleep)
        return out

    def _lookup_batch(self, batch: list[Coord]) -> list[float]:
        locations = [{"latitude": lat, "longitude": lon} for lat, lon in batch]
        try:
            resp = requests.post(
                self.endpoint,
                json={"locations": locations},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            raise ElevationError(f"elevation API request failed: {e}") from e

        results = data.get("results")
        if not results or len(results) != len(batch):
            raise ElevationError("elevation API returned unexpected result count")
        return [float(r["elevation"]) for r in results]
