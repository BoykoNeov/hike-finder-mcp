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
        "pois", "destination",
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


# ------------------------------------------------------- --to-poi (route to an object)


def test_to_poi_reaches_the_engine_with_start_kinds_and_knobs(monkeypatch, capsys):
    """--from + --to-poi dispatches to routes_to_poi, not to routes_between."""
    from hike_finder import cli as C

    captured = {}

    def _stub(start, kinds, criteria, cfg=None, *, n=None, search_radius_m=None, **kw):
        captured.update(start=start, kinds=kinds, n=n, radius=search_radius_m,
                        poi_filter=criteria.poi_kinds)
        return []

    def _fail(*a, **k):
        raise AssertionError("--to-poi must not fall through to another mode")

    monkeypatch.setattr(C, "routes_to_poi", _stub)
    monkeypatch.setattr(C, "routes_between", _fail)
    monkeypatch.setattr(C, "search_hikes", _fail)
    code = run(_parse(
        "--from", "50.73", "15.60", "--to-poi", "ruins,castle",
        "--routes", "2", "--to-poi-radius", "4500",
    ))
    assert code == 0
    assert captured["start"] == (50.73, 15.60)
    assert captured["kinds"] == ("ruins", "castle")   # comma list, order preserved
    assert captured["n"] == 2 and captured["radius"] == 4500
    # --poi was not given, so the "must pass" filter stays empty — the destination kinds
    # are NOT smuggled into it (that would let poi_radius_m drop a route whose destination
    # snapped farther off the trail than the filter allows).
    assert captured["poi_filter"] == ()
    # The empty-result message is destination-shaped, never the area-filter wording.
    out = capsys.readouterr().out
    assert "No route could be drawn to an object of that kind" in out
    assert "widen --poi-radius" not in out


def test_to_poi_and_poi_are_separate_questions(monkeypatch):
    """--poi keeps filtering what the drawn route must pass — the two coexist."""
    from hike_finder import cli as C

    captured = {}
    monkeypatch.setattr(
        C, "routes_to_poi",
        lambda start, kinds, criteria, cfg=None, **kw: captured.update(
            kinds=kinds, poi_filter=criteria.poi_kinds
        ) or [],
    )
    assert run(_parse(
        "--from", "50.73", "15.60", "--to-poi", "ruins", "--poi", "refreshment",
    )) == 0
    assert captured["kinds"] == ("ruins",) and captured["poi_filter"] == ("refreshment",)


def test_to_poi_validation_rejects_the_wrong_combinations(capsys):
    # A destination with no start.
    assert run(_parse("--to-poi", "ruins")) == 2
    assert "need a --from start point" in capsys.readouterr().err
    # A start with no destination at all.
    assert run(_parse("--from", "50.73", "15.60")) == 2
    assert "--from needs a destination" in capsys.readouterr().err
    # Two different destinations at once.
    assert run(_parse("--from", "50.73", "15.60", "--to", "50.74", "15.61",
                      "--to-poi", "ruins")) == 2
    assert "two different destinations" in capsys.readouterr().err
    # A different point-based mode at the same time.
    assert run(_parse("--from", "50.73", "15.60", "--to-poi", "ruins",
                      "--around", "50.73", "15.60")) == 2
    assert "use one, not several" in capsys.readouterr().err
    # A typo'd kind is loud, not an empty result set.
    assert run(_parse("--from", "50.73", "15.60", "--to-poi", "dragon")) == 2
    assert "unknown point-of-interest kind" in capsys.readouterr().err
    # The mode derives its own area, so a bbox is a contradiction.
    assert run(_parse("--from", "50.73", "15.60", "--to-poi", "ruins",
                      "--bbox", "50.7", "15.5", "50.8", "15.7")) == 2
    assert "omit --bbox" in capsys.readouterr().err
    # It is a live search — a snapshot cannot answer it (see HANDOFF: deliberately so).
    assert run(_parse("--from", "50.73", "15.60", "--to-poi", "ruins",
                      "--area", "krkonose")) == 2
    assert "can't be combined with --area" in capsys.readouterr().err


# --------------------------------------------------------- --show-pois (browse, no routes)


def _poi_snapshot(path, pois):
    """A minimal snapshot carrying only POIs — the browse needs no routes at all."""
    save_snapshot(
        AreaSnapshot(
            bbox=(50.72, 15.58, 50.78, 15.68),
            area=AreaData(pois=list(pois)),
            elevations={},
            sample_interval_m=25.0,
        ),
        path,
    )


_DEMO_POIS = [
    {"coord": (50.7301, 15.6001), "kind": "ruins", "name": "Nístějka"},
    {"coord": (50.7411, 15.6102), "kind": "church", "name": None},
    {"coord": (50.7211, 15.5902), "kind": "church", "name": "Sv. Petr"},
]


def test_show_pois_lists_a_downloaded_area(tmp_path, capsys):
    path = tmp_path / "demo.json"
    _poi_snapshot(path, _DEMO_POIS)
    assert run(_parse("--show-pois", "--area", str(path))) == 0
    out = capsys.readouterr().out
    assert "3 objects: 2 churches & chapels, 1 ruin" in out
    assert "Nístějka" in out and "Sv. Petr" in out
    # An unnamed object still gets a line, labelled by its kind rather than blank.
    assert out.count("church") >= 2


def test_show_pois_filters_by_kind_and_exports(tmp_path, capsys):
    path = tmp_path / "demo.json"
    _poi_snapshot(path, _DEMO_POIS)
    gpx, geo = tmp_path / "p.gpx", tmp_path / "p.geojson"
    assert run(_parse(
        "--show-pois", "--area", str(path), "--poi", "ruins",
        "--gpx", str(gpx), "--geojson", str(geo),
    )) == 0
    cap = capsys.readouterr()
    assert "1 object: 1 ruin" in cap.out
    assert "Sv. Petr" not in cap.out
    # The confirmation goes to STDERR so a --json pipe stays clean, and counts objects
    # rather than routes — nothing here is a route.
    assert "point(s) of interest" in cap.err and "route(s)" not in cap.err
    # Waypoints, not tracks: a listing has nothing to walk.
    assert "<wpt" in gpx.read_text(encoding="utf-8")
    assert "<trk>" not in gpx.read_text(encoding="utf-8")
    assert json.loads(geo.read_text(encoding="utf-8"))["features"][0]["geometry"]["type"] == "Point"


def test_show_pois_json_is_a_bare_array(tmp_path, capsys):
    path = tmp_path / "demo.json"
    _poi_snapshot(path, _DEMO_POIS)
    assert run(_parse("--show-pois", "--area", str(path), "--json")) == 0
    data = json.loads(capsys.readouterr().out)
    assert [d["kind"] for d in data] == ["church", "church", "ruins"]
    assert "distance_m" not in data[0]   # nothing was measured, so nothing is claimed


def test_show_pois_empty_names_the_lever(tmp_path, capsys):
    path = tmp_path / "demo.json"
    _poi_snapshot(path, _DEMO_POIS)
    assert run(_parse("--show-pois", "--area", str(path), "--poi", "cave")) == 0
    out = capsys.readouterr().out
    assert "pick other --poi kinds" in out
    assert "not that nothing is there" in out


def test_show_pois_distinguishes_a_pre_poi_snapshot(tmp_path, capsys):
    """An old snapshot cannot answer — that must not read as "no ruins here"."""
    path = tmp_path / "old.json"
    _poi_snapshot(path, [])
    assert run(_parse("--show-pois", "--area", str(path))) == 0
    out = capsys.readouterr().out
    assert "saved before the feature existed" in out
    assert "pick other --poi kinds" not in out


def test_show_pois_rejects_the_wrong_combinations(capsys):
    # It draws no routes, so there is nothing to compose.
    assert run(_parse("--show-pois", "--bbox", "1", "2", "3", "4", "--compose-loops")) == 2
    assert "nothing for --compose-loops" in capsys.readouterr().err
    # It lists, it does not save an area.
    assert run(_parse("--show-pois", "--bbox", "1", "2", "3", "4", "--download", "x.json")) == 2
    assert "Download first, then browse" in capsys.readouterr().err
    # The point-based modes are about routing to things; this one deliberately is not.
    assert run(_parse("--show-pois", "--from", "50.7", "15.6", "--to-poi", "ruins")) == 2
    assert "lists objects without routing to them" in capsys.readouterr().err
    assert run(_parse("--show-pois", "--around", "50.7", "15.6")) == 2
    assert "lists objects without routing to them" in capsys.readouterr().err
    # Neither a box nor an area to look in.
    assert run(_parse("--show-pois")) == 2
    assert "--show-pois needs --bbox" in capsys.readouterr().err
    # A typo'd kind is loud, not an empty list.
    assert run(_parse("--show-pois", "--bbox", "1", "2", "3", "4", "--poi", "dragon")) == 2
    assert "unknown point-of-interest kind" in capsys.readouterr().err


def test_show_pois_live_path_passes_the_kinds_through(monkeypatch):
    """The live branch forwards bbox + kinds and builds no elevation provider."""
    from hike_finder import cli as C

    captured = {}
    monkeypatch.setattr(
        C, "list_area_pois",
        lambda bbox, kinds, cfg=None, **kw: captured.update(bbox=bbox, kinds=kinds) or (),
    )
    assert run(_parse(
        "--show-pois", "--bbox", "50.72", "15.58", "50.78", "15.68", "--poi", "ruins,church",
    )) == 0
    assert captured["bbox"] == (50.72, 15.58, 50.78, 15.68)
    assert captured["kinds"] == ("ruins", "church")


def test_list_poi_kinds_keeps_its_old_spelling(capsys):
    """--list-pois is the historical name; --list-poi-kinds is the unambiguous one.

    argparse derives `dest` from the FIRST long option, so this also pins that the
    explicit dest survived — without it `run()` would silently stop seeing the flag.
    """
    assert _parse("--list-pois").list_pois is True
    assert _parse("--list-poi-kinds").list_pois is True
    assert run(_parse("--list-poi-kinds")) == 0
    out = capsys.readouterr().out
    assert "ruins" in out and "--show-pois LISTS the objects" in out
