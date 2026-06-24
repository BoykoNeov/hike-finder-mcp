"""Shared test fixtures.

Two on-disk stores would otherwise touch the developer's REAL per-user cache and
make tests depend on accumulated state: the persistent daily-request counter (see
elevation/quota.py) and the transparent Overpass/elevation cache (see cache.py).
This autouse fixture points every test at throwaway directories so both are fully
isolated and hermetic — and so the cache, which is on by default, starts empty for
each test (a fresh cache reproduces today's first-fetch behaviour exactly).
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_quota_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKE_API_STATE_DIR", str(tmp_path / "quota-state"))
    monkeypatch.setenv("HIKE_CACHE_DIR", str(tmp_path / "cache"))
