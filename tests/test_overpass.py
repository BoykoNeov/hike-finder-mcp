"""Offline pins for the two hardening tweaks in ``overpass``:

  - the User-Agent version is derived from the installed distribution metadata
    (no hardcoded literal that silently drifts from ``pyproject``);
  - ``fetch_area`` with ``max_retries < 1`` fails with a clean ``ValueError``
    instead of an ``AttributeError`` on ``resp = None`` (defensive; unreachable
    via the default callers, which keep the default ``max_retries``).

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


def test_a_busy_server_is_retried_patiently(monkeypatch):
    """The query carries the whole walking network, and the public instance answers a
    heavy query under load with 504 — then succeeds some seconds later. Pins the waits
    (2 s, 4 s, 8 s: long enough to be worth it, bounded at 14 s) and that the answer
    after the storm is the one parsed."""
    class _Resp:
        def __init__(self, status):
            self.status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"status {self.status_code}")

        def json(self):
            return {"elements": []}

    replies = iter([504, 504, 429, 200])
    monkeypatch.setattr(overpass.requests, "post", lambda *a, **k: _Resp(next(replies)))
    waits: list[float] = []
    monkeypatch.setattr(overpass.time, "sleep", waits.append)
    area = overpass.fetch_area(50.0, 15.0, 50.1, 15.1)
    assert waits == [2, 4, 8]
    assert area.paths == []  # a real parse of the final answer


class _Ok:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_a_timed_out_answer_is_refused_not_parsed_as_an_empty_area(monkeypatch):
    """Overpass reports a mid-query timeout as HTTP 200 plus a `remark`, with whatever
    it had printed so far. Parsed, that is "no parking, no stops, no POIs here" — and it
    would be cached. Measured live on a 400 km² box with a 2 s limit."""
    payload = {
        "elements": [{"type": "way", "id": 1, "tags": {"highway": "path"},
                      "geometry": [{"lat": 50.0, "lon": 15.0}, {"lat": 50.1, "lon": 15.0}]}],
        "remark": 'runtime error: Query timed out in "print" at line 17 after 3 seconds.',
    }
    monkeypatch.setattr(overpass.requests, "post", lambda *a, **k: _Ok(payload))
    with pytest.raises(overpass.OverpassIncomplete, match="smaller area"):
        overpass.fetch_area(50.0, 15.0, 50.1, 15.1)


def test_a_benign_remark_is_not_an_error(monkeypatch):
    payload = {"elements": [], "remark": "runtime remark: nothing to report"}
    monkeypatch.setattr(overpass.requests, "post", lambda *a, **k: _Ok(payload))
    assert overpass.fetch_area(50.0, 15.0, 50.1, 15.1).paths == []


def test_the_walking_network_is_the_last_statement():
    """A cut-off query stops wherever it got to. With the heaviest statement last, a cut
    can only cost paths — never the parking/transit/POI lists read as "none here"."""
    q = overpass.build_query(50.7, 15.5, 50.8, 15.7)
    tail = q.rstrip().splitlines()[-2:]
    assert tail[0].strip().startswith('way["highway"~"^(path|')
    assert tail[1].strip() == "out tags geom;"
