"""Offline pins for the two hardening tweaks in ``overpass``:

  - the User-Agent version is derived from the installed distribution metadata
    (no hardcoded literal that silently drifts from ``pyproject``);
  - ``fetch_area`` with ``max_retries < 1`` fails with a clean ``ValueError``
    instead of an ``AttributeError`` on ``resp = None`` (defensive; unreachable
    via the default callers, which pass ``max_retries=3``).

Neither test touches the network: the UA is a module constant, and the retry
guard raises before any request is sent.
"""
from importlib.metadata import PackageNotFoundError, version

import pytest

from hike_finder import overpass


def test_user_agent_carries_installed_version():
    try:
        v = version("hike-finder-mcp")
    except PackageNotFoundError:  # raw checkout, not pip-installed — like overpass itself
        pytest.skip("hike-finder-mcp not installed — run `pip install -e .`")
    ua = overpass.USER_AGENT
    assert ua.startswith(f"hike-finder-mcp/{v} ")
    assert "set HIKE_OVERPASS_UA" in ua  # keep the contact hint


def test_fetch_area_rejects_zero_retries():
    # range(0) sends nothing -> resp stays None. Guard turns that into a clean
    # error, not AttributeError. Raises before any HTTP call, so no network.
    with pytest.raises(ValueError):
        overpass.fetch_area(50.0, 15.0, 50.1, 15.1, max_retries=0)
