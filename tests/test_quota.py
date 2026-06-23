"""Offline tests for the persistent daily-request quota (elevation/quota.py).

The counter exists because each search blows through a fresh process, so an
in-memory tally can't enforce a *daily* cap. These pin the behaviours that make
it correct: UTC-day rollover, at-limit enforcement that wastes no network call,
persistence across separate provider instances, the disable switch, and that the
*process-wide* lock (not a per-instance one) serialises concurrent writers.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import requests

from hike_finder.elevation.api import DEFAULT_ENDPOINT, ApiElevationProvider
from hike_finder.elevation.base import ElevationError
from hike_finder.elevation.quota import DailyQuota


def _at(year, month, day):
    """A fixed-clock callable for DailyQuota(now=...)."""
    return lambda: datetime(year, month, day, 12, 0, tzinfo=timezone.utc)


class FakeResp:
    def __init__(self, payload, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _otd(elevs):
    return {"results": [{"elevation": e} for e in elevs]}


def test_counts_and_persists_across_instances(tmp_path):
    q1 = DailyQuota(DEFAULT_ENDPOINT, daily_limit=1000, state_dir=tmp_path, now=_at(2026, 6, 23))
    q1.record()
    q1.record()
    assert q1.snapshot() == (2, 1000)
    # A FRESH instance (mimics the next CLI run / another provider) reads the
    # same file — proving the count isn't merely instance state.
    q2 = DailyQuota(DEFAULT_ENDPOINT, daily_limit=1000, state_dir=tmp_path, now=_at(2026, 6, 23))
    assert q2.snapshot() == (2, 1000)
    assert q2.has_quota() is True


def test_rolls_over_at_utc_midnight(tmp_path):
    yest = DailyQuota(DEFAULT_ENDPOINT, daily_limit=5, state_dir=tmp_path, now=_at(2026, 6, 22))
    yest.record()
    yest.record()
    yest.record()
    assert yest.snapshot() == (3, 5)
    # New UTC day, same file: the count resets to 0.
    today = DailyQuota(DEFAULT_ENDPOINT, daily_limit=5, state_dir=tmp_path, now=_at(2026, 6, 23))
    assert today.snapshot() == (0, 5)
    assert today.has_quota() is True


def test_has_quota_false_at_limit(tmp_path):
    q = DailyQuota(DEFAULT_ENDPOINT, daily_limit=2, state_dir=tmp_path, now=_at(2026, 6, 23))
    q.record()
    q.record()
    assert q.snapshot() == (2, 2)
    assert q.has_quota() is False


def test_disabled_when_limit_zero(tmp_path):
    q = DailyQuota(DEFAULT_ENDPOINT, daily_limit=0, state_dir=tmp_path, now=_at(2026, 6, 23))
    assert q.has_quota() is True
    q.record()
    q.record()
    assert q.snapshot() == (0, 0)
    # Disabled means no disk side effects at all.
    assert list(tmp_path.iterdir()) == []


def test_separate_counters_per_endpoint_host(tmp_path):
    otd = DailyQuota("https://api.opentopodata.org/v1/srtm30m", state_dir=tmp_path, now=_at(2026, 6, 23))
    oe = DailyQuota("https://api.open-elevation.com/api/v1/lookup", state_dir=tmp_path, now=_at(2026, 6, 23))
    otd.record()
    assert otd.snapshot()[0] == 1
    assert oe.snapshot()[0] == 0  # different host -> different file


def test_threadsafe_concurrent_record(tmp_path):
    # Each thread builds its OWN DailyQuota on the SAME file — exactly the
    # threaded-web-server shape. A per-instance lock would let the concurrent
    # read-modify-writes lose updates; the module-level lock keeps it exact.
    per_thread = 50

    def worker():
        q = DailyQuota(DEFAULT_ENDPOINT, daily_limit=10_000, state_dir=tmp_path, now=_at(2026, 6, 23))
        for _ in range(per_thread):
            q.record()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = DailyQuota(DEFAULT_ENDPOINT, daily_limit=10_000, state_dir=tmp_path, now=_at(2026, 6, 23))
    assert final.snapshot() == (4 * per_thread, 10_000)


def test_provider_skips_request_when_quota_exhausted(tmp_path):
    # Pre-fill the file to the limit, then prove the provider raises WITHOUT
    # touching the network (check-before-send). That ElevationError is what
    # degrades the route to n/a via FallbackElevationProvider.
    DailyQuota(DEFAULT_ENDPOINT, daily_limit=1, state_dir=tmp_path).record()
    prov = ApiElevationProvider(
        endpoint=DEFAULT_ENDPOINT, min_interval_s=0, daily_limit=1, state_dir=str(tmp_path)
    )
    with patch("hike_finder.elevation.api.requests.post") as post:
        with pytest.raises(ElevationError):
            prov.lookup([(0, 0)])
    assert post.call_count == 0


def test_provider_records_each_request(tmp_path):
    prov = ApiElevationProvider(
        endpoint=DEFAULT_ENDPOINT, min_interval_s=0, batch_size=1,
        daily_limit=1000, state_dir=str(tmp_path),
    )
    with patch("hike_finder.elevation.api.requests.post") as post:
        post.return_value = FakeResp(_otd([1.0]))
        prov.lookup([(0, 0), (1, 1)])  # batch_size=1 -> 2 requests
    assert prov.quota.snapshot()[0] == 2
