"""Shared test fixtures.

The elevation provider now keeps a *persistent* daily-request counter on disk
(see elevation/quota.py). Default-constructed providers in the suite would
otherwise read and write the developer's REAL per-user cache file — polluting it
and making tests depend on accumulated state. This autouse fixture points every
test at a throwaway directory so the counter is fully isolated and hermetic.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_quota_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKE_API_STATE_DIR", str(tmp_path / "quota-state"))
