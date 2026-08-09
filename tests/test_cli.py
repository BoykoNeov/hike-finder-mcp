"""Offline tests for the CLI's pure parts and the shared formatter.

No network: we only exercise argument parsing, the args -> Criteria mapping
(especially the tri-state booleans, which are easy to get wrong), and the
one-line / dict rendering shared by every frontend.
"""
import json

from hike_finder.cli import build_criteria, build_parser, run
from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Criteria, Hike, find_hikes
from hike_finder.format import format_hike, hike_to_dict
from hike_finder.overpass import AreaData
from hike_finder.snapshot import AreaSnapshot, RecordingElevationProvider, save_snapshot


def _parse(*argv):
    return build_parser().parse_args(list(argv))


def test_bbox_parsed_in_order():
    args = _parse("--bbox", "50.72", "15.58", "50.74", "15.62")
    assert args.bbox == [50.72, 15.58, 50.74, 15.62]


def test_boolean_filters_are_tristate():
    # omitted -> None (don't care)
    a = _parse("--bbox", "1", "2", "3", "4")
    assert a.circular is None and a.car_access is None and a.chairlift_access is None

    # present -> True (require)
    b = _parse("--bbox", "1", "2", "3", "4", "--circular", "--car-access", "--chairlift-access")
    assert b.circular is True and b.car_access is True and b.chairlift_access is True

    # negated -> False (exclude)
    c = _parse("--bbox", "1", "2", "3", "4", "--no-circular", "--no-car-access", "--no-chairlift-access")
    assert c.circular is False and c.car_access is False and c.chairlift_access is False


def test_build_criteria_maps_all_fields():
    args = _parse(
        "--bbox", "1", "2", "3", "4",
        "--min-gain", "100", "--max-gain", "800",
        "--min-distance", "5", "--max-distance", "20",
        "--circular", "--no-car-access",
    )
    crit = build_criteria(args)
    assert crit.min_gain_m == 100 and crit.max_gain_m == 800
    assert crit.min_distance_km == 5 and crit.max_distance_km == 20
    assert crit.circular is True
    assert crit.car_access is False
    assert crit.chairlift_access is None  # untouched -> don't care


def _sample_hike(**over):
    base = dict(
        osm_id=42, name="Test loop", distance_km=8.3, circular=True,
        car_access=True, chairlift_access=True, start=(50.7312, 15.6044),
        gain_m=540, loss_m=535, lift_type="chair_lift", ref="0001",
    )
    base.update(over)
    return Hike(**base)


def test_format_hike_full():
    line = format_hike(_sample_hike())
    assert line.startswith("Test loop — 8.3 km, +540 m / -535 m")
    assert "[loop, car, lift:chair_lift]" in line
    assert "start 50.7312,15.6044" in line
    assert "OSM relation 42" in line


def test_format_hike_oneway_no_access_no_gain():
    line = format_hike(_sample_hike(
        circular=False, car_access=False, chairlift_access=False,
        gain_m=None, loss_m=None, lift_type=None,
    ))
    assert "[one-way]" in line
    assert "gain n/a" in line
    assert "car" not in line and "lift:" not in line


def test_hike_to_dict_shape():
    d = hike_to_dict(_sample_hike())
    assert d["osm_id"] == 42 and d["name"] == "Test loop"
    assert d["start"] == {"lat": 50.7312, "lon": 15.6044}
    assert d["lift_type"] == "chair_lift"
    assert set(d) == {
        "osm_id", "name", "ref", "distance_km", "gain_m", "loss_m",
        "circular", "car_access", "chairlift_access", "lift_type", "start",
        "near_miss", "notes", "composed", "composed_of", "unnamed", "place_name",
        "pois",
    }
    # A plain match serialises as not-a-near-miss with no notes, and not composed.
    assert d["near_miss"] is False and d["notes"] == []
    assert d["composed"] is False and d["composed_of"] == []
    # No POI filter was set, so nothing was scanned and nothing is claimed.
    assert d["pois"] == []
    # An ordinary named route is not flagged unnamed and carries no derived label.
    assert d["unnamed"] is False and d["place_name"] is None


def test_hike_to_dict_geometry_is_opt_in_and_lat_lon():
    h = _sample_hike(ways=(((50.0, 14.0), (50.1, 14.1)),))
    assert "geometry" not in hike_to_dict(h)  # default stays lean for --json
    d = hike_to_dict(h, geometry=True)
    # [lat, lon] order (Leaflet's L.polyline), NOT GeoJSON's [lon, lat]
    assert d["geometry"] == [[[50.0, 14.0], [50.1, 14.1]]]


def test_format_hike_near_miss_is_flagged():
    h = _sample_hike(near_miss=True, notes=("gain 720 m — 80 m below the 800 m minimum",))
    line = format_hike(h)
    assert line.startswith("~ Test loop")
    assert "[near miss: gain 720 m — 80 m below the 800 m minimum]" in line


# --- offline --area mode through the CLI's run() (no network) -----------------


class _Ramp(ElevationProvider):
    def lookup(self, points):
        return [(lat - 50.0) * 20000.0 for lat, _ in points]


def _write_snapshot(path):
    area = AreaData(routes=[{"id": 1, "name": "North", "ways": [[(50.0, 14.0), (50.05, 14.0)]], "tags": {}}])
    rec = RecordingElevationProvider(_Ramp())
    bbox = (49.9, 13.9, 50.2, 14.2)
    find_hikes(area, rec, Criteria(), bbox=bbox)
    save_snapshot(
        AreaSnapshot(bbox=bbox, area=area, elevations=rec.samples, sample_interval_m=25.0), path
    )


def test_run_area_mode_searches_offline(tmp_path, capsys):
    path = tmp_path / "area.json"
    _write_snapshot(path)
    rc = run(build_parser().parse_args(["--area", str(path)]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "North" in out and "OSM relation 1" in out


def test_run_area_and_download_mutually_exclusive(capsys):
    rc = run(build_parser().parse_args(["--bbox", "1", "2", "3", "4", "--area", "a", "--download", "b"]))
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_export_flags_parse():
    a = _parse("--bbox", "1", "2", "3", "4", "--gpx", "out.gpx", "--geojson", "out.geojson")
    assert a.gpx == "out.gpx" and a.geojson == "out.geojson"


def test_export_with_download_is_rejected(capsys):
    rc = run(build_parser().parse_args(
        ["--bbox", "1", "2", "3", "4", "--download", "d.json", "--gpx", "x.gpx"]
    ))
    assert rc == 2
    assert "can't be combined with --download" in capsys.readouterr().err


def test_run_area_mode_writes_gpx_and_geojson(tmp_path, capsys):
    import xml.etree.ElementTree as ET

    snap = tmp_path / "area.json"
    _write_snapshot(snap)
    gpx = tmp_path / "out.gpx"
    geojson = tmp_path / "out.geojson"
    rc = run(build_parser().parse_args(
        ["--area", str(snap), "--gpx", str(gpx), "--geojson", str(geojson)]
    ))
    assert rc == 0
    # GPX: well-formed, and the snapshot route "North" is exported as a track.
    body = gpx.read_text(encoding="utf-8")
    assert ET.fromstring(body).tag.endswith("gpx")
    assert "North" in body
    # GeoJSON: a single-feature collection.
    obj = json.loads(geojson.read_text(encoding="utf-8"))
    assert obj["type"] == "FeatureCollection" and len(obj["features"]) == 1
    # The confirmation goes to stderr (never pollutes a --json pipe).
    assert "Wrote 1 route(s)" in capsys.readouterr().err


def test_run_requires_bbox_without_area(capsys):
    rc = run(build_parser().parse_args(["--min-gain", "100"]))
    assert rc == 2
    assert "--bbox is required" in capsys.readouterr().err


# --------------------------------------------------- points of interest + area listing


def test_poi_flag_is_repeatable_and_comma_separated():
    # Both spellings are natural at a prompt, so both are accepted and normalise the same.
    a = _parse("--bbox", "1", "2", "3", "4", "--poi", "ruins", "--poi", "church")
    b = _parse("--bbox", "1", "2", "3", "4", "--poi", "ruins,church")
    assert build_criteria(a).poi_kinds == ("ruins", "church")
    assert build_criteria(b).poi_kinds == ("ruins", "church")
    # Omitted -> no destination filter at all.
    assert build_criteria(_parse("--bbox", "1", "2", "3", "4")).poi_kinds == ()


def test_unknown_poi_kind_exits_2_with_a_named_error(capsys):
    # A typo must fail loudly: an empty result set would read as "none of those exist".
    code = run(_parse("--bbox", "1", "2", "3", "4", "--poi", "cathedral"))
    assert code == 2
    err = capsys.readouterr().err
    assert "cathedral" in err and "church" in err


def test_list_pois_prints_the_registry_and_exits(capsys):
    from hike_finder.poi import POI_KINDS

    assert run(_parse("--list-pois")) == 0
    out = capsys.readouterr().out
    for kind in POI_KINDS:
        assert kind in out


def test_list_areas_reports_what_is_downloaded(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HIKE_SNAPSHOT_DIR", str(tmp_path))
    # Nothing downloaded yet: say so, and point at how to get one.
    assert run(_parse("--list-areas")) == 0
    assert "No downloaded areas" in capsys.readouterr().out

    area = AreaData(
        routes=[{"id": 7, "name": "N", "ways": [[(50.0, 14.0), (50.05, 14.0)]], "tags": {}}],
        pois=[{"coord": (50.025, 14.001), "kind": "ruins", "name": "Hrad"}],
    )
    save_snapshot(
        AreaSnapshot(bbox=(49.9, 13.9, 50.2, 14.2), area=area, elevations={},
                     sample_interval_m=25.0),
        tmp_path / "krkonose.json",
    )
    assert run(_parse("--list-areas")) == 0
    out = capsys.readouterr().out
    assert "krkonose" in out and "1 routes" in out and "1 POIs" in out
    assert "49.9000,13.9000" in out  # the bbox, so you can see WHERE it covers

    # --json gives the same inventory machine-readably.
    assert run(_parse("--list-areas", "--json")) == 0
    data = json.loads(capsys.readouterr().out)
    assert [a["name"] for a in data] == ["krkonose"]
    assert data[0]["bbox"] == [49.9, 13.9, 50.2, 14.2] and data[0]["pois"] == 1


def test_area_accepts_a_bare_name_from_list_areas(tmp_path, monkeypatch, capsys):
    """The names --list-areas prints are usable verbatim, not just full paths."""
    monkeypatch.setenv("HIKE_SNAPSHOT_DIR", str(tmp_path))
    area = AreaData(
        routes=[{"id": 7, "name": "WebNorth", "ways": [[(50.0, 14.0), (50.05, 14.0)]], "tags": {}}]
    )
    rec = RecordingElevationProvider(_Ramp())
    bbox = (49.9, 13.9, 50.2, 14.2)
    find_hikes(area, rec, Criteria(), bbox=bbox)
    save_snapshot(
        AreaSnapshot(bbox=bbox, area=area, elevations=rec.samples, sample_interval_m=25.0),
        tmp_path / "krkonose.json",
    )
    assert run(_parse("--area", "krkonose", "--json")) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "WebNorth"
