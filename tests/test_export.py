"""Offline tests for the GPX / GeoJSON exporters.

The export module is pure (no network, no I/O), so it is unit-tested in isolation
like the rest of the trustworthy core. The two failure modes a serialiser like this
actually hits are pinned hard:

  * **coordinate order** — the project keeps ``(lat, lon)`` internally; GPX wants
    ``lat=/lon=`` attributes and GeoJSON wants ``[lon, lat]`` pairs (RFC 7946). One
    wrong swap plots every route in the ocean, so a KNOWN point is asserted onto a
    KNOWN axis in each format.
  * **escaping / encoding** — names are routinely non-ASCII (Czech KČT trails) and can
    contain ``&``/``<``; GPX is XML, so those must be escaped and the document must stay
    well-formed (we parse it back to prove it).

Empty input, multi-way (branched) routes, composed loops, and near-misses are all
covered, since each is a real shape the engine returns.
"""
import json
import xml.etree.ElementTree as ET

from hike_finder.export import (
    GEOJSON_MIME,
    GPX_MIME,
    hikes_to_geojson,
    hikes_to_gpx,
)
from hike_finder.filters import Hike

GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}


def _hike(**over):
    base = dict(
        osm_id=42, name="Test loop", distance_km=8.3, circular=True,
        car_access=True, chairlift_access=False, start=(50.7312, 15.6044),
        gain_m=540, loss_m=535, lift_type=None, ref="0001",
        ways=(((50.0, 14.0), (50.01, 14.02)),),
    )
    base.update(over)
    return Hike(**base)


# --- GPX ----------------------------------------------------------------------


def test_gpx_is_wellformed_with_one_track_and_segment():
    root = ET.fromstring(hikes_to_gpx([_hike()]))
    assert root.tag.endswith("gpx")
    trks = root.findall("g:trk", GPX_NS)
    assert len(trks) == 1
    segs = trks[0].findall("g:trkseg", GPX_NS)
    assert len(segs) == 1
    assert len(segs[0].findall("g:trkpt", GPX_NS)) == 2


def test_gpx_uses_reverse_geocoded_label_for_unnamed_route():
    # An unnamed route given a place label exports THAT into the GPS track/waypoint
    # name (matching the terminal), not the route/<id> fallback.
    h = _hike(osm_id=99, name="route/99", ref=None, unnamed=True, place_name="Pec → Sněžka")
    root = ET.fromstring(hikes_to_gpx([h]))
    trk_name = root.find("g:trk/g:name", GPX_NS).text
    wpt_name = root.find("g:wpt/g:name", GPX_NS).text
    assert trk_name == "Pec → Sněžka"
    assert wpt_name == "Pec → Sněžka (start)"
    assert "route/99" not in hikes_to_gpx([h])


def test_geojson_keeps_name_truthful_and_adds_place_name():
    h = _hike(osm_id=99, name="route/99", ref=None, unnamed=True, place_name="Pec → Sněžka")
    props = json.loads(hikes_to_geojson([h]))["features"][0]["properties"]
    assert props["name"] == "route/99"          # truthful OSM value, untouched
    assert props["place_name"] == "Pec → Sněžka"  # derived label carried separately
    assert props["unnamed"] is True


def test_gpx_coordinate_order_is_lat_lon():
    # The known point (50.0, 14.0) must land as lat=50.0, lon=14.0 — never swapped.
    root = ET.fromstring(hikes_to_gpx([_hike(ways=(((50.0, 14.0), (50.5, 14.5)),))]))
    pt = root.find(".//g:trkpt", GPX_NS)
    assert float(pt.get("lat")) == 50.0
    assert float(pt.get("lon")) == 14.0


def test_gpx_start_waypoint_precedes_tracks():
    xml = hikes_to_gpx([_hike()])
    # GPX 1.1 fixes element order: every <wpt> before any <trk>.
    assert xml.index("<wpt") < xml.index("<trk")
    wpt = ET.fromstring(xml).find("g:wpt", GPX_NS)
    assert float(wpt.get("lat")) == 50.7312 and float(wpt.get("lon")) == 15.6044


def test_gpx_escapes_xml_special_chars_in_name():
    xml = hikes_to_gpx([_hike(name="A & B <loop>")])
    assert "A &amp; B &lt;loop&gt;" in xml
    ET.fromstring(xml)  # still well-formed


def test_gpx_preserves_unicode_name():
    xml = hikes_to_gpx([_hike(name="Špindlmanova mise")])
    assert "Špindlmanova mise" in xml
    ET.fromstring(xml.encode("utf-8"))  # parses as UTF-8 bytes too


def test_gpx_empty_input_is_a_valid_empty_document():
    root = ET.fromstring(hikes_to_gpx([]))
    assert root.findall("g:trk", GPX_NS) == []
    assert root.findall("g:wpt", GPX_NS) == []


def test_gpx_one_trkseg_per_member_way():
    h = _hike(ways=(
        ((50.0, 14.0), (50.1, 14.0)),
        ((50.2, 14.1), (50.3, 14.2), (50.4, 14.3)),
    ))
    segs = ET.fromstring(hikes_to_gpx([h])).findall(".//g:trkseg", GPX_NS)
    assert len(segs) == 2
    assert len(segs[0].findall("g:trkpt", GPX_NS)) == 2
    assert len(segs[1].findall("g:trkpt", GPX_NS)) == 3


def test_gpx_desc_carries_the_one_line_summary():
    xml = hikes_to_gpx([_hike()])
    assert "8.3 km" in xml and "+540 m / -535 m" in xml


def test_gpx_track_emits_single_segment_with_per_point_elevation():
    # A hike carrying a per-point track exports as ONE <trkseg> in walking order, each
    # <trkpt> bearing an <ele> — the v2 "single clean track", not the raw-ways multi-seg.
    h = _hike(track=((50.0, 14.0, 100.0), (50.01, 14.0, 137.5), (50.02, 14.0, 90.0)))
    root = ET.fromstring(hikes_to_gpx([h]))
    segs = root.findall(".//g:trkseg", GPX_NS)
    assert len(segs) == 1
    pts = segs[0].findall("g:trkpt", GPX_NS)
    assert len(pts) == 3
    # Axis order is preserved and the elevation rides on each point.
    assert float(pts[0].get("lat")) == 50.0 and float(pts[0].get("lon")) == 14.0
    assert pts[0].find("g:ele", GPX_NS).text == "100.0"
    assert pts[1].find("g:ele", GPX_NS).text == "137.5"


def test_gpx_track_takes_precedence_over_raw_ways():
    # With a track present the multi-way `ways` is NOT emitted as separate segments —
    # the single clean elevated track wins (and is the only <trkseg>).
    h = _hike(
        ways=(((50.0, 14.0), (50.1, 14.0)), ((50.2, 14.1), (50.3, 14.2))),
        track=((50.0, 14.0, 10.0), (50.1, 14.0, 20.0)),
    )
    segs = ET.fromstring(hikes_to_gpx([h])).findall(".//g:trkseg", GPX_NS)
    assert len(segs) == 1
    assert len(segs[0].findall("g:trkpt", GPX_NS)) == 2


# --- GeoJSON ------------------------------------------------------------------


def test_geojson_is_a_feature_collection_of_multilinestrings():
    obj = json.loads(hikes_to_geojson([_hike()]))
    assert obj["type"] == "FeatureCollection"
    assert len(obj["features"]) == 1
    f = obj["features"][0]
    assert f["type"] == "Feature"
    assert f["geometry"]["type"] == "MultiLineString"


def test_geojson_coordinate_order_is_lon_lat():
    # RFC 7946 mandates [lon, lat] — the OPPOSITE of the project's internal order.
    obj = json.loads(hikes_to_geojson([_hike(ways=(((50.0, 14.0), (50.5, 14.5)),))]))
    coords = obj["features"][0]["geometry"]["coordinates"]
    assert coords[0][0] == [14.0, 50.0]


def test_geojson_properties_carry_stats_but_not_geometry():
    props = json.loads(hikes_to_geojson([_hike()]))["features"][0]["properties"]
    assert props["name"] == "Test loop"
    assert props["distance_km"] == 8.3 and props["gain_m"] == 540
    assert "geometry" not in props  # geometry lives on the Feature, not in properties


def test_geojson_multiple_ways_become_multiple_lines():
    h = _hike(ways=(((50.0, 14.0), (50.1, 14.0)), ((50.2, 14.1), (50.3, 14.2))))
    coords = json.loads(hikes_to_geojson([h]))["features"][0]["geometry"]["coordinates"]
    assert len(coords) == 2


def test_geojson_empty_input_is_a_valid_empty_collection():
    assert json.loads(hikes_to_geojson([])) == {"type": "FeatureCollection", "features": []}


def test_geojson_no_geometry_when_ways_absent():
    obj = json.loads(hikes_to_geojson([_hike(ways=())]))
    assert obj["features"][0]["geometry"] is None


def test_geojson_track_is_one_3d_line_keeping_multilinestring_type():
    # A track exports as a single 3D line [lon, lat, ele] (RFC 7946 altitude element),
    # still wrapped as MultiLineString so the geometry TYPE never varies between hikes.
    h = _hike(track=((50.0, 14.0, 100.0), (50.5, 14.5, 250.0)))
    f = json.loads(hikes_to_geojson([h]))["features"][0]
    assert f["geometry"]["type"] == "MultiLineString"
    coords = f["geometry"]["coordinates"]
    assert len(coords) == 1                 # one continuous line, not per-way segments
    assert coords[0][0] == [14.0, 50.0, 100.0]   # lon, lat, ele
    assert coords[0][1] == [14.5, 50.5, 250.0]


def test_geojson_preserves_unicode():
    assert "Krkonoše" in hikes_to_geojson([_hike(name="Krkonoše")])


# --- composed loops & near-misses are real shapes the engine returns ----------


def test_composed_loop_exports_provenance_not_relation_id():
    h = _hike(
        osm_id=-1, name="Composed loop", composed=True, composed_of=("0402", "1801"),
        ways=(((50.0, 14.0), (50.0, 14.1), (50.1, 14.1), (50.0, 14.0)),),
    )
    assert "composed of 0402 + 1801" in hikes_to_gpx([h])  # via format_hike in <desc>
    props = json.loads(hikes_to_geojson([h]))["features"][0]["properties"]
    assert props["osm_id"] is None and props["composed"] is True


def test_near_miss_is_included_and_flagged():
    h = _hike(near_miss=True, notes=("gain 720 m — 80 m below the 800 m minimum",))
    props = json.loads(hikes_to_geojson([h]))["features"][0]["properties"]
    assert props["near_miss"] is True
    assert props["notes"] == ["gain 720 m — 80 m below the 800 m minimum"]
    assert "near miss" in hikes_to_gpx([h])  # the annotation rides along in <desc>


def test_near_miss_marks_the_gpx_name_like_every_other_frontend():
    # A GPS track list shows the <name>, not the <desc>, so a near-miss must be marked
    # there too (~ prefix) — otherwise an auto near-miss export looks like a clean match.
    h = _hike(near_miss=True, notes=("gain 720 m — 80 m below the 800 m minimum",))
    root = ET.fromstring(hikes_to_gpx([h]))
    assert root.find("g:trk/g:name", GPX_NS).text == "~ Test loop"
    assert root.find("g:wpt/g:name", GPX_NS).text == "~ Test loop (start)"
    # A plain match is NOT prefixed.
    plain = ET.fromstring(hikes_to_gpx([_hike()]))
    assert plain.find("g:trk/g:name", GPX_NS).text == "Test loop"


def test_mime_constants():
    assert GPX_MIME == "application/gpx+xml"
    assert GEOJSON_MIME == "application/geo+json"
