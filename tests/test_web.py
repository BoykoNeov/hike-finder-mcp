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


def _get_raw(url):
    """Fetch a non-JSON download: returns (status, headers, body-text)."""
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.headers, resp.read().decode("utf-8")


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
    # /api/hikes carries geometry so the map can draw the line without a 2nd search;
    # it is [lat, lon] (Leaflet order), and the known first vertex proves the axis.
    assert h["geometry"][0][0] == [50.0, 14.0]


def test_gpx_download_offline(server):
    status, headers, body = _get_raw(server + "/api/gpx?area=webtest")
    assert status == 200
    assert "attachment" in headers["Content-Disposition"]
    assert "hikes.gpx" in headers["Content-Disposition"]
    import xml.etree.ElementTree as ET

    assert ET.fromstring(body).tag.endswith("gpx")
    assert "WebNorth" in body


def test_geojson_download_offline(server):
    status, headers, body = _get_raw(server + "/api/geojson?area=webtest")
    assert status == 200
    assert "hikes.geojson" in headers["Content-Disposition"]
    obj = json.loads(body)
    assert obj["type"] == "FeatureCollection" and len(obj["features"]) == 1
    # GeoJSON is [lon, lat] (RFC 7946) — the opposite axis order from /api/hikes.
    assert obj["features"][0]["geometry"]["coordinates"][0][0] == [14.0, 50.0]


def test_gpx_unknown_area_is_404(server):
    import urllib.error

    with pytest.raises(urllib.error.HTTPError) as ei:
        _get_raw(server + "/api/gpx?area=nope")
    assert ei.value.code == 404


def test_hikes_unknown_area_is_404(server):
    import urllib.error

    with pytest.raises(urllib.error.HTTPError) as ei:
        _get(server + "/api/hikes?area=nope")
    assert ei.value.code == 404


def test_hikes_compose_loops_routes_to_compose_engine(server, monkeypatch):
    # compose_loops=true on the live bbox route must call the composition engine
    # (NOT search_hikes) and serialise the composed loop's provenance with no relation id.
    from hike_finder.filters import Hike

    def _fail(*a, **k):
        raise AssertionError("search_hikes must not run when compose_loops is set")

    def _stub(bbox, criteria, *, user_agent=None, near_miss=False, **k):
        return [
            Hike(osm_id=-1, name="Composed loop", distance_km=9.0, circular=True,
                 car_access=False, chairlift_access=False, start=(50.7, 15.6),
                 gain_m=200, loss_m=200, composed=True, composed_of=("0402", "1801")),
        ]

    monkeypatch.setattr(web, "search_hikes", _fail)
    monkeypatch.setattr(web, "compose_loops", _stub)
    status, hikes = _get(
        server + "/api/hikes?south=50.72&west=15.58&north=50.74&east=15.62&compose_loops=true"
    )
    assert status == 200 and len(hikes) == 1
    h = hikes[0]
    assert h["composed"] is True and h["composed_of"] == ["0402", "1801"]
    assert h["osm_id"] is None
