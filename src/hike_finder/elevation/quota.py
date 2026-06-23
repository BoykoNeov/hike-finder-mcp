"""Persistent daily-request counter for the elevation API.

The per-second throttle (``api._throttle``) keeps us under the public ~1 req/sec
limit, but nothing stops a day's worth of searches from cumulatively blowing the
*daily* cap (OpenTopoData allows ~1000 calls/day). And the CLI is a fresh process
every run, so an in-memory count can't see this morning's searches — let alone a
second ``hike-finder`` running at the same time. So the count lives in a small
JSON file on disk, keyed by the API host (different hosts have different quotas).

It's a **soft, advisory** limit: when today's count reaches the limit we stop
sending and let the route degrade to ``n/a`` (via ``ElevationError`` →
``FallbackElevationProvider``), rather than hammering the server into 429s. A
single *process-wide* lock serialises the read-modify-write — crucially NOT a
per-instance lock, because every search builds a fresh provider (and thus a
fresh ``DailyQuota``), so a per-instance lock would not serialise concurrent
web-server requests against the shared file. Cross-*process* races (two CLI runs
at once) can still lose an update, but for an advisory limit that degrades
gracefully, being off by one near the boundary is harmless — not worth
platform-specific ``fcntl``/``msvcrt`` file locking.

Reset boundary: assumes the quota resets at **UTC midnight** (OpenTopoData's
day). If a provider's real reset is offset by a few hours the only cost is
degrading to ``n/a`` slightly early or late — nothing breaks.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ONE lock for every DailyQuota in this process (see module docstring). A
# per-instance lock would not serialise the shared file across the concurrent
# providers a threaded web server creates.
_LOCK = threading.Lock()


def _default_state_dir() -> Path:
    """Per-user state dir: %LOCALAPPDATA% on Windows, $XDG_CACHE_HOME or
    ~/.cache elsewhere. Override with HIKE_API_STATE_DIR."""
    env = os.getenv("HIKE_API_STATE_DIR")
    if env:
        return Path(env)
    base = (
        os.getenv("LOCALAPPDATA")
        or os.getenv("XDG_CACHE_HOME")
        or os.path.join(Path.home(), ".cache")
    )
    return Path(base) / "hike-finder"


def _host_key(endpoint: str) -> str:
    host = urlparse(endpoint).hostname or "elevation"
    return host.replace(":", "_")


class DailyQuota:
    """File-backed counter of elevation-API requests made today.

    ``daily_limit <= 0`` disables tracking entirely: no file is read or written
    and ``has_quota`` is always true. ``now`` is injectable for tests.
    """

    def __init__(
        self,
        endpoint: str,
        daily_limit: int = 1000,
        state_dir: str | os.PathLike | None = None,
        now=None,
    ):
        self.daily_limit = daily_limit
        self.enabled = daily_limit > 0
        self._now = now or (lambda: datetime.now(timezone.utc))
        if self.enabled:
            d = Path(state_dir) if state_dir is not None else _default_state_dir()
            self.path: Path | None = d / f"quota-{_host_key(endpoint)}.json"
        else:
            self.path = None

    # -- internals (callers below hold _LOCK) -------------------------------

    def _today(self) -> str:
        return self._now().date().isoformat()

    def _read(self) -> tuple[str, int]:
        """(date, count) from disk, or (today, 0) if missing/corrupt."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return str(data["date"]), int(data["count"])
        except (OSError, ValueError, KeyError, TypeError):
            return self._today(), 0

    def _write(self, date: str, count: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace so a concurrent reader never sees a half-written file.
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"date": date, "count": count}, f)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _current(self) -> int:
        """Today's count, rolling over to 0 at UTC midnight."""
        date, count = self._read()
        return 0 if date != self._today() else count

    # -- public API ---------------------------------------------------------

    def has_quota(self) -> bool:
        """True if at least one more request is allowed today."""
        if not self.enabled:
            return True
        with _LOCK:
            return self._current() < self.daily_limit

    def record(self) -> None:
        """Count one request that reached the server (call AFTER a response)."""
        if not self.enabled:
            return
        with _LOCK:
            today = self._today()
            date, count = self._read()
            self._write(today, (count + 1) if date == today else 1)

    def snapshot(self) -> tuple[int, int]:
        """``(used_today, limit)``; ``(0, 0)`` when disabled."""
        if not self.enabled:
            return (0, 0)
        with _LOCK:
            return (self._current(), self.daily_limit)
