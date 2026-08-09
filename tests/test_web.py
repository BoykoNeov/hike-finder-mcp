"""Offline tests for the web frontend.

The snapshot-name helpers are pure. The end-to-end check starts the real
``ThreadingHTTPServer`` on an ephemeral port and drives the *offline* routes
(``/api/areas`` and ``/api/hikes?area=``) over real HTTP — no network, because a
snapshot search never leaves the box. The live ``/api/download`` and bbox
``/api/hikes`` routes touch Overpass and are validated manually (see HANDOFF).
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from hike_finder import web
from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Criteria, find_hikes
from hike_finder.overpass import AreaData
from hike_finder.snapshot import (
    AreaSnapshot,
    RecordingElevationProvider,
    save_snapshot,
    slug,
    snapshot_path,
)


def test_slug_is_path_safe():
    # Unicode letters are kept (Czech names are everywhere here); the safety property
    # is that path separators and dots can never survive, so a slug is always a bare
    # filename stem and never escapes the snapshots dir. The helper now lives in
    # snapshot.py, shared by the web UI and the CLI's --area / --list-areas.
    assert slug("Krkonoše 2026") == "Krkonoše_2026"
    assert slug("../etc/passwd") == "etc_passwd"
    assert "/" not in slug("a/b") and "\\" not in slug("a\\b")
    assert "." not in slug("a.b..c")
    assert slug("   ") == ""
    # An unusable name yields no path at all, rather than a directory or a bare ".json".
    assert snapshot_path("   ") is None
    assert snapshot_path("krkonose").name == "krkonose.json"


class _Ramp(ElevationProvider):
    def lookup(self, points):
        return [(lat - 50.0) * 20000.0 for lat, _ in points]


def _make_snapshot(path, pois=None):
    area = AreaData(
        routes=[{"id": 7, "name": "WebNorth", "ways": [[(50.0, 14.0), (50.05, 14.0)]], "tags": {}}],
        pois=list(pois or []),
    )
    rec = RecordingElevationProvider(_Ramp())
    bbox = (49.9, 13.9, 50.2, 14.2)
    find_hikes(area, rec, Criteria(), bbox=bbox)
    save_snapshot(AreaSnapshot(bbox=bbox, area=area, elevations=rec.samples, sample_interval_m=25.0), path)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKE_SNAPSHOT_DIR", str(tmp_path))
    _make_snapshot(tmp_path / "webtest.json")
    # A second area carrying points of interest, so the destination filter can be driven
    # end-to-end over real HTTP without touching the network.
    _make_snapshot(
        tmp_path / "webpoi.json",
        pois=[
            {"coord": (50.0250, 14.0015), "kind": "ruins", "name": "Zřícenina"},
            {"coord": (50.0300, 14.0500), "kind": "church", "name": "Faraway"},
        ],
    )
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


def test_areas_carries_enough_to_show_what_is_downloaded(server):
    """"What have I already got?" needs the bbox to outline it and the counts to judge it."""
    _, areas = _get(server + "/api/areas")
    entry = next(a for a in areas if a["name"] == "webpoi")
    assert entry["bbox"] == [49.9, 13.9, 50.2, 14.2]   # so the map can draw the rectangle
    assert entry["created_at"] and entry["bytes"] > 0
    assert entry["samples"] > 0
    assert entry["pois"] == 2
    # And the pre-POI area reports 0, which is what drives the UI's "re-download" hint.
    assert next(a for a in areas if a["name"] == "webtest")["pois"] == 0


def test_poi_kinds_endpoint_mirrors_the_registry(server):
    from hike_finder.poi import POI_KINDS

    status, kinds = _get(server + "/api/pois")
    assert status == 200
    assert {k["kind"] for k in kinds} == set(POI_KINDS)
    assert all(k["label"] for k in kinds)


def test_hikes_offline_filters_by_poi(server):
    """The destination filter runs over HTTP against a snapshot, with zero network."""
    status, hikes = _get(server + "/api/hikes?area=webpoi&poi=ruins")
    assert status == 200 and len(hikes) == 1
    assert [(p["kind"], p["name"]) for p in hikes[0]["pois"]] == [("ruins", "Zřícenina")]
    # The POI carries its own coordinate, so the map can pin it without a second lookup.
    assert hikes[0]["pois"][0]["lat"] == 50.0250

    # The church is ~3 km off the route: no match at the default radius...
    _, none = _get(server + "/api/hikes?area=webpoi&poi=church&near_misses=false")
    assert none == []
    # ...and the radius is the lever that finds it.
    _, wide = _get(
        server + "/api/hikes?area=webpoi&poi=church&poi_radius_m=5000&near_misses=false"
    )
    assert len(wide) == 1 and wide[0]["pois"][0]["name"] == "Faraway"


def test_hikes_accepts_comma_separated_and_repeated_poi(server):
    for query in ("poi=ruins,church", "poi=ruins&poi=church"):
        _, hikes = _get(server + "/api/hikes?area=webpoi&poi_radius_m=5000&" + query)
        assert len(hikes) == 1
        assert {p["kind"] for p in hikes[0]["pois"]} == {"ruins", "church"}


def test_unknown_poi_kind_is_a_loud_400_not_an_empty_list(server):
    """A typo must fail visibly — an empty result would read as "none out there"."""
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/api/hikes?area=webpoi&poi=cathedral")
    assert e.value.code == 400
    body = json.loads(e.value.read().decode("utf-8"))
    assert "cathedral" in body["error"] and "church" in body["error"]


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
    # The clean single-way route gets a faithful per-point elevation track, so the offline
    # GeoJSON carries it through as 3D positions: [lon, lat, ele] (RFC 7946's optional 3rd
    # element) — the opposite axis order from /api/hikes. The ramp reads 0 m at lat 50.0.
    assert obj["features"][0]["geometry"]["coordinates"][0][0] == [14.0, 50.0, 0.0]


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


def test_hikes_around_point_routes_to_compose_around(server, monkeypatch):
    # around_lat/around_lon route to compose_loops_around with the point + radius, and NOT
    # to search_hikes / compose_loops (which are for the bbox modes).
    from hike_finder.filters import Hike

    captured = {}

    def _fail(*a, **k):
        raise AssertionError("bbox search must not run in the around mode")

    def _stub(point, criteria, *, radius_m=None, user_agent=None, near_miss=False, **k):
        captured["point"] = point
        captured["radius_m"] = radius_m
        return [
            Hike(osm_id=-1, name="Composed loop", distance_km=8.0, circular=True,
                 car_access=False, chairlift_access=False, start=(50.73, 15.60),
                 gain_m=250, loss_m=250, composed=True, composed_of=("0402",)),
        ]

    monkeypatch.setattr(web, "search_hikes", _fail)
    monkeypatch.setattr(web, "compose_loops", _fail)
    monkeypatch.setattr(web, "compose_loops_around", _stub)
    status, hikes = _get(
        server + "/api/hikes?around_lat=50.73&around_lon=15.60&around_radius_m=750"
    )
    assert status == 200 and len(hikes) == 1
    assert captured["point"] == (50.73, 15.60) and captured["radius_m"] == 750
    assert hikes[0]["composed"] is True and hikes[0]["circular"] is True


def test_hikes_between_two_points_routes_to_routes_between(server, monkeypatch):
    # from_/to_ route to routes_between with the two points + k, ordered shortest-first.
    from hike_finder.filters import Hike

    captured = {}

    def _fail(*a, **k):
        raise AssertionError("bbox search must not run in the between mode")

    def _stub(start, finish, criteria, *, k=None, user_agent=None, **kw):
        captured["start"] = start
        captured["finish"] = finish
        captured["k"] = k
        return [
            Hike(osm_id=-1, name="Route", distance_km=3.5, circular=False,
                 car_access=False, chairlift_access=False, start=start,
                 gain_m=100, loss_m=80, composed=True, composed_of=("0402",)),
        ]

    monkeypatch.setattr(web, "search_hikes", _fail)
    monkeypatch.setattr(web, "routes_between", _stub)
    status, hikes = _get(
        server + "/api/hikes?from_lat=50.72&from_lon=15.58&to_lat=50.74&to_lon=15.62&routes_k=4"
    )
    assert status == 200 and len(hikes) == 1
    assert captured["start"] == (50.72, 15.58) and captured["finish"] == (50.74, 15.62)
    assert captured["k"] == 4
    assert hikes[0]["composed"] is True and hikes[0]["circular"] is False


# ----------------------------------------------- route to the nearest object (--to-poi)


def test_hikes_to_poi_routes_to_the_destination_engine(server, monkeypatch):
    """to_poi_* params reach routes_to_poi with the start, kinds and both knobs — and the
    destination survives into the JSON so the map can pin it."""
    from hike_finder.filters import Hike
    from hike_finder.poi import PoiHit

    captured = {}

    def _fail(*a, **k):
        raise AssertionError("an area/bbox search must not run in the to-poi mode")

    def _stub(start, kinds, criteria, *, n=None, search_radius_m=None, cfg=None, **kw):
        captured.update(start=start, kinds=kinds, n=n, radius=search_radius_m,
                        poi_filter=criteria.poi_kinds)
        return [
            Hike(osm_id=-1, name="Route to ruin “Rotštejn”", distance_km=4.2, circular=False,
                 car_access=False, chairlift_access=False, start=start, gain_m=180, loss_m=95,
                 composed=True, composed_of=("0402",),
                 destination=PoiHit(kind="ruins", name="Rotštejn", coord=(50.75, 15.61),
                                    distance_m=85.0)),
        ]

    monkeypatch.setattr(web, "search_hikes", _fail)
    monkeypatch.setattr(web, "routes_between", _fail)
    monkeypatch.setattr(web, "routes_to_poi", _stub)
    status, hikes = _get(
        server + "/api/hikes?to_poi_lat=50.73&to_poi_lon=15.60&to_poi=ruins&to_poi=castle"
        "&to_poi_n=2&to_poi_radius_m=4500"
    )
    assert status == 200 and len(hikes) == 1
    assert captured["start"] == (50.73, 15.60)
    assert captured["kinds"] == ("ruins", "castle")
    assert captured["n"] == 2 and captured["radius"] == 4500
    # The destination kinds are NOT copied into the "must pass" filter — two questions.
    assert captured["poi_filter"] == ()
    d = hikes[0]["destination"]
    assert d["kind"] == "ruins" and d["name"] == "Rotštejn" and d["distance_m"] == 85.0
    assert d["lat"] == 50.75 and d["lon"] == 15.61


def test_a_search_without_to_poi_reports_no_destination(server):
    """Every other search serialises `destination: null` — never a stale or invented one."""
    status, hikes = _get(server + "/api/hikes?area=webtest")
    assert status == 200 and hikes
    assert all(h["destination"] is None for h in hikes)


def test_unknown_to_poi_kind_is_a_loud_400(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/api/hikes?to_poi_lat=50.73&to_poi_lon=15.60&to_poi=dragon")
    assert e.value.code == 400


def test_to_poi_with_half_a_point_is_a_400_not_a_silent_area_search(server, monkeypatch):
    # A lat with no lon must not fall through to the bbox branch and answer a different
    # question than the one asked.
    monkeypatch.setattr(
        web, "search_hikes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fell through"))
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/api/hikes?to_poi_lat=50.73&to_poi=ruins&south=50.7&west=15.5&north=50.8&east=15.7")
    assert e.value.code == 400


def test_to_poi_against_a_saved_area_is_rejected(server):
    # Drawing a route is a live search; a snapshot cannot answer it, and quietly returning
    # a filtered offline search instead would look like an answer to a question not asked.
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/api/hikes?area=webtest&to_poi_lat=50.0&to_poi_lon=14.0&to_poi=ruins")
    assert e.value.code == 400


def test_gpx_download_replays_a_to_poi_search(server, monkeypatch):
    """The download button replays the last query through the SAME resolver, so a route
    drawn to an object must come out as a track — not fall through to a bbox search."""
    import xml.etree.ElementTree as ET

    from hike_finder.filters import Hike
    from hike_finder.poi import PoiHit

    def _fail(*a, **k):
        raise AssertionError("the download must not re-run a different search")

    monkeypatch.setattr(web, "search_hikes", _fail)
    monkeypatch.setattr(
        web, "routes_to_poi",
        lambda start, kinds, criteria, **kw: [
            Hike(osm_id=-1, name="Route to ruin “Zřícenina”", distance_km=4.2, circular=False,
                 car_access=False, chairlift_access=False, start=start, gain_m=180, loss_m=95,
                 composed=True, composed_of=("0402",),
                 ways=(((50.73, 15.60), (50.75, 15.61)),),
                 destination=PoiHit(kind="ruins", name="Zřícenina", coord=(50.75, 15.61),
                                    distance_m=85.0)),
        ],
    )
    status, headers, body = _get_raw(
        server + "/api/gpx?to_poi_lat=50.73&to_poi_lon=15.60&to_poi=ruins"
    )
    assert status == 200 and "attachment" in headers["Content-Disposition"]
    assert ET.fromstring(body).tag.endswith("gpx")
    # The destination-derived (non-ASCII) name survives into the XML.
    assert "Zřícenina" in body


# ------------------------------------------- browsing points of interest (no routes drawn)


def test_poi_list_reads_a_downloaded_area(server):
    """The "or only in the downloaded area" half of the browse — zero network."""
    status, body = _get(server + "/api/poi-list?area=webpoi")
    assert status == 200
    assert [p["kind"] for p in body["pois"]] == ["church", "ruins"]   # registry order
    assert body["summary"] == "2 objects: 1 church, 1 ruin"
    assert body["stale_area"] is False
    # A listed object carries no distance: there is no route to measure it from.
    assert "distance_m" not in body["pois"][0]


def test_poi_list_filters_by_kind(server):
    _, body = _get(server + "/api/poi-list?area=webpoi&show_poi=ruins")
    assert [p["name"] for p in body["pois"]] == ["Zřícenina"]


def test_poi_list_with_no_kinds_shows_everything(server):
    """Empty selection is a browse ("what's here?"), not an empty question."""
    _, all_kinds = _get(server + "/api/poi-list?area=webpoi")
    _, explicit = _get(server + "/api/poi-list?area=webpoi&show_poi=church&show_poi=ruins")
    assert all_kinds["pois"] == explicit["pois"]


def test_poi_list_flags_a_pre_poi_area(server):
    """`webtest` was saved with no POIs — the UI must be able to say "can't know"."""
    _, body = _get(server + "/api/poi-list?area=webtest")
    assert body["pois"] == [] and body["stale_area"] is True


def test_poi_list_pairs_with_a_saved_area_where_routing_may_not(server):
    """The reason the browse gets its own resolver.

    `/api/hikes` rejects area + to_poi (routing needs a live graph); the browse REQUIRES
    that pairing. Pinned together so a future refactor can't merge the two rules.
    """
    _, browsed = _get(server + "/api/poi-list?area=webpoi")
    assert browsed["pois"]
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/api/hikes?area=webpoi&to_poi=ruins&to_poi_lat=50&to_poi_lon=14")
    assert e.value.code == 400


def test_poi_list_rejects_an_unknown_kind(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/api/poi-list?area=webpoi&show_poi=castel")
    assert e.value.code == 400
    assert "unknown point-of-interest kind" in e.value.read().decode("utf-8")


def test_poi_list_needs_a_box_or_an_area(server):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(server + "/api/poi-list")
    assert e.value.code == 400


def test_poi_kinds_registry_is_still_its_own_endpoint(server):
    """/api/pois is the MENU of kinds; /api/poi-list is the objects. Distinct on purpose."""
    _, kinds = _get(server + "/api/pois")
    assert {"kind", "label"} <= set(kinds[0])
    assert "ruins" in {k["kind"] for k in kinds}


def test_poi_export_streams_waypoints_not_tracks(server):
    """`pois=true` is part of the params the page stores, so the download matches the list."""
    status, headers, body = _get_raw(server + "/api/gpx?pois=true&area=webpoi&show_poi=ruins")
    assert status == 200
    assert headers["Content-Disposition"] == 'attachment; filename="pois.gpx"'
    assert "<wpt" in body and "<trk>" not in body
    assert "Zřícenina" in body

    status, headers, body = _get_raw(server + "/api/geojson?pois=true&area=webpoi")
    assert headers["Content-Disposition"] == 'attachment; filename="pois.geojson"'
    fc = json.loads(body)
    assert {f["geometry"]["type"] for f in fc["features"]} == {"Point"}


def test_hike_export_is_unaffected_by_the_new_branch(server):
    """Without `pois`, the export is the route export it always was."""
    status, headers, body = _get_raw(server + "/api/gpx?area=webtest")
    assert headers["Content-Disposition"] == 'attachment; filename="hikes.gpx"'
    assert "<trk>" in body
