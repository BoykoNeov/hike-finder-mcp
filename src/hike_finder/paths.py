"""Shared per-user directory convention.

Three call sites keep durable state under the same per-user base — the Overpass/
elevation cache (:mod:`cache`), the daily-quota counter (:mod:`elevation.quota`),
and named web-UI snapshots (:mod:`snapshot`). Only the *base* is common; each
caller layers its own env override (``HIKE_CACHE_DIR`` / ``HIKE_API_STATE_DIR`` /
``HIKE_SNAPSHOT_DIR``) and its own subdir on top. Keeping just the base here
prevents the three copies from drifting without unifying the overrides (which
would silently relocate an existing cache/quota/snapshots).
"""
from __future__ import annotations

import os
from pathlib import Path


def user_cache_dir() -> Path:
    """``%LOCALAPPDATA%\\hike-finder`` on Windows, else ``$XDG_CACHE_HOME`` or
    ``~/.cache`` + ``hike-finder``. Read live so a late env change is honoured."""
    base = (
        os.getenv("LOCALAPPDATA")
        or os.getenv("XDG_CACHE_HOME")
        or os.path.join(Path.home(), ".cache")
    )
    return Path(base) / "hike-finder"
