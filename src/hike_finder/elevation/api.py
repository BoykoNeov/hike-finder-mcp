"""API-based elevation: OpenTopoData or Open-Elevation (configurable).

Zero setup, but rate-limited and coarser. Good for getting started and for
areas where you don't want to host DEM tiles.

OpenTopoData public endpoint allows 100 locations/request and ~1 req/sec.
Open-Elevation has a similar shape. Both POST JSON and return
``results[].elevation`` — but their REQUEST bodies differ:

  - OpenTopoData wants ``{"locations": "lat,lon|lat,lon"}`` (one pipe string).
  - Open-Elevation wants ``{"locations": [{"latitude": .., "longitude": ..}]}``.

So the request body is keyed off the endpoint host (see ``_encode_locations``);
response parsing is shared. Validated live 2026-06-23 against OpenTopoData
(srtm30m): the Špindlerův Mlýn point returned 794 m.

Rate limiting and resilience: one provider instance is reused for every route in
a search, so it throttles ALL requests to >= ``min_interval_s`` apart (keeps us
under the ~1 req/sec public limit), and retries transient failures (429 / 5xx /
network blips) with exponential backoff, honouring a ``Retry-After`` header when
the server sends one. Deterministic 4xx (e.g. 400) are not retried — that would
just burn the daily quota.
"""
from __future__ import annotations

import time

import requests

from .base import Coord, ElevationError, ElevationProvider

# OpenTopoData datasets: "srtm30m" (global, 30 m), "aster30m", "mapzen", etc.
DEFAULT_ENDPOINT = "https://api.opentopodata.org/v1/srtm30m"
OPEN_ELEVATION_ENDPOINT = "https://api.open-elevation.com/api/v1/lookup"

# Transient HTTP statuses worth retrying: 429 (rate limit) + the usual 5xx server
# hiccups. A 400/404 is deterministic, so retrying it only wastes the daily quota.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class ApiElevationProvider(ElevationProvider):
    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        batch_size: int = 100,
        min_interval_s: float = 1.1,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        backoff_base_s: float = 2.0,
        max_backoff_s: float = 30.0,
    ):
        self.endpoint = endpoint
        self.batch_size = batch_size
        # OpenTopoData's public server allows ~1 request/second; go over and it
        # 429s. One provider instance is reused for every route in a search, so
        # we throttle ACROSS routes/batches (not just within one route) — see
        # _throttle. A hair over 1 s absorbs jitter.
        self.min_interval_s = min_interval_s
        self.timeout = timeout_s
        # Retry transient failures (429 / 5xx / network) up to max_retries times
        # with exponential backoff (backoff_base_s * 2**attempt), bounded by the
        # server's Retry-After when present. max_backoff_s caps any single wait:
        # a Retry-After longer than that (e.g. a daily-quota 429 saying "come back
        # in an hour") means we stop and degrade to n/a rather than freeze the
        # search — see _lookup_batch / _backoff.
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.max_backoff_s = max_backoff_s
        self._last_request_t: float | None = None
        # OpenTopoData and Open-Elevation take different request bodies; pick the
        # dialect from the host so a plain endpoint override is all a user needs.
        self.api_format = "opentopodata" if "opentopodata" in endpoint else "open-elevation"

    def lookup(self, points: list[Coord]) -> list[float]:
        out: list[float] = []
        for i in range(0, len(points), self.batch_size):
            out.extend(self._lookup_batch(points[i : i + self.batch_size]))
        return out

    def _throttle(self) -> None:
        """Sleep so consecutive requests stay >= min_interval_s apart."""
        if self.min_interval_s <= 0:
            return
        if self._last_request_t is not None:
            wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
            if wait > 0:
                time.sleep(wait)
        self._last_request_t = time.monotonic()

    def _encode_locations(self, batch: list[Coord]) -> dict:
        if self.api_format == "opentopodata":
            return {"locations": "|".join(f"{lat},{lon}" for lat, lon in batch)}
        return {"locations": [{"latitude": lat, "longitude": lon} for lat, lon in batch]}

    def _lookup_batch(self, batch: list[Coord]) -> list[float]:
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                resp = requests.post(
                    self.endpoint,
                    json=self._encode_locations(batch),
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                # Connection reset / timeout / DNS — transient, worth a retry.
                last_err = e
                if attempt < self.max_retries:
                    self._backoff(attempt, None)
                continue

            if resp.status_code in _RETRY_STATUS:
                last_err = ElevationError(f"elevation API returned HTTP {resp.status_code}")
                retry_after = self._retry_after(resp)
                if retry_after is not None and retry_after > self.max_backoff_s:
                    # A cooldown longer than our ceiling (typically a daily-quota
                    # 429): retrying soon just gets rejected again, so stop now and
                    # degrade to n/a instead of stalling the search for minutes.
                    break
                if attempt < self.max_retries:
                    self._backoff(attempt, retry_after)
                continue

            # Non-transient outcome (2xx, or a 4xx we shouldn't retry): commit.
            try:
                resp.raise_for_status()
                data = resp.json()
            except (requests.RequestException, ValueError) as e:
                raise ElevationError(f"elevation API request failed: {e}") from e

            results = data.get("results")
            if not results or len(results) != len(batch):
                raise ElevationError("elevation API returned unexpected result count")
            try:
                return self._parse_elevations(results)
            except (KeyError, TypeError, ValueError) as e:
                raise ElevationError(
                    f"elevation API returned unparseable elevations: {e}"
                ) from e

        raise ElevationError(
            f"elevation API failed after {self.max_retries + 1} attempt(s): {last_err}"
        )

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        """Sleep before the next retry: exponential, but never less than the
        server's Retry-After when it sent one — and never more than max_backoff_s
        (a long Retry-After is handled by giving up in _lookup_batch, so this cap
        is just a backstop)."""
        delay = self.backoff_base_s * (2 ** attempt)
        if retry_after is not None:
            delay = max(delay, retry_after)
        delay = min(delay, self.max_backoff_s)
        if delay > 0:
            time.sleep(delay)

    @staticmethod
    def _retry_after(resp) -> float | None:
        """Parse a Retry-After header in delta-seconds form. The HTTP-date form
        is ignored (we fall back to exponential backoff) — these APIs use seconds."""
        val = resp.headers.get("Retry-After")
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_elevations(results: list[dict]) -> list[float]:
        # Some datasets return null elevation for nodata points (ocean, tile
        # edges). Forward-fill from the last valid reading, seeded with the first
        # valid value so leading gaps are back-filled too: a nodata point then
        # contributes ~0 gain instead of voiding the whole route. Fail only if
        # every point is nodata.
        raw = [float(r["elevation"]) if r.get("elevation") is not None else None for r in results]
        valid = [v for v in raw if v is not None]
        if not valid:
            raise ElevationError("elevation API returned only nodata values")
        last = valid[0]
        out: list[float] = []
        for v in raw:
            if v is not None:
                last = v
            out.append(last)
        return out
