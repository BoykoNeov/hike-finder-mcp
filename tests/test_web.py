"""Offline tests for the web frontend.

The snapshot-name helpers are pure. The end-to-end check starts the real
``ThreadingHTTPServer`` on an ephemeral port and drives the *offline* routes
(``/api/areas`` and ``/api/hikes?area=``) over real HTTP — no network, because a
snapshot search never leaves the box. The live ``/api/download`` and bbox
``/api/hikes`` routes touch Overpass and are validated manually (see HANDOFF).
"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from hike_finder import web
from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Criteria, find_hikes
from hike_finder.overpass import AreaData
from hike_finder.snapshot import AreaSnapshot, RecordingElevationProvider, save_snapshot


def test_slug_is_path_safe():
    # Unicode letters are kept (Czech names are everywhere here); the safety property
    # is that path separators and dots can never survive, so a slug is always a bare
    # filename stem and never escapes the snapshots dir.
    assert web._slug("Krkonoše 2026") == "Krkonoše_2026"
    assert web._slug("../etc/passwd") == "etc_passwd"
    assert "/" not in web._slug("a/b") and "\\" not in web._slug("a\\b")
    assert "." not in web._slug("a.b..c")
    assert web._slug("   ") == ""


class _Ramp(ElevationProvider):
    def lookup(self, points):
        return [(lat - 50.0) * 20000.0 for lat, _ in points]


def _make_snapshot(path):
    area = AreaData(routes=[{"id": 7, "name": "WebNorth", "ways": [[(50.0, 14.0), (50.05, 14.0)]], "tags": {}}])
    rec = RecordingElevationProvider(_Ramp())
    bbox = (49.9, 13.9, 50.2, 14.2)
    find_hikes(area, rec, Criteria(), bbox=bbox)
    save_snapshot(AreaSnapshot(bbox=bbox, area=area, elevations=rec.samples, sample_interval_m=25.0), path)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKE_SNAPSHOT_DIR", str(tmp_path))
    _make_snapshot(tmp_path / "webtest.json")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_areas_lists_saved_snapshot(server):
    status, areas = _get(server + "/api/areas")
    assert status == 200
    names = {a["name"] for a in areas}
    assert "webtest" in names
    entry = next(a for a in areas if a["name"] == "webtest")
    assert entry["routes"] == 1


def test_hikes_offline_by_area(server):
    status, hikes = _get(server + "/api/hikes?area=webtest")
    assert status == 200
    assert len(hikes) == 1
    h = hikes[0]
    assert h["osm_id"] == 7 and h["name"] == "WebNorth"
    assert h["gain_m"] is not None  # answered from saved samples, not degraded


def test_hikes_unknown_area_is_404(server):
    import urllib.error

    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(server + "/api/hikes?area=nope")
    assert ei.value.code == 404
