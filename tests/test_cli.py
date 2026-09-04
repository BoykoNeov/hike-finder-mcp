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
from hike_finder.poi import all_kinds
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
    c = _parse(
        "--bbox", "1", "2", "3", "4", "--no-circular", "--no-car-access",
        "--no-chairlift-access",
    )
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
    base = {
        "osm_id": 42, "name": "Test loop", "distance_km": 8.3, "circular": True,
        "car_access": True, "chairlift_access": True, "start": (50.7312, 15.6044),
        "gain_m": 540, "loss_m": 535, "lift_type": "chair_lift", "ref": "0001",
    }
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
        "transit_access", "transit_type", "transit_label", "surface", "tracktype",
        "near_miss", "notes", "composed", "composed_of", "unnamed", "place_name",
        "pois", "destination", "ferrata",
    }
    # Transit is null, not false, on a Hike whose area never recorded it — the JSON
    # consumer has to be able to tell "none mapped nearby" from "never measured".
    assert d["transit_access"] is None and d["transit_label"] is None
    # Same rule for what you walk on: null means member-way tags were never fetched,
    # which is not the same as a route nobody has tagged.
    assert d["surface"] is None and d["tracktype"] is None
    # And for cable, where the distinction carries the most weight: null is "this data
    # could not say", NOT "checked and clear". A consumer wanting the latter has to see
    # an object with present=False.
    assert d["ferrata"] is None
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
    area = AreaData(
        routes=[
            {"id": 1, "name": "North", "ways": [[(50.0, 14.0), (50.05, 14.0)]], "tags": {}}
        ]
    )
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
    rc = run(
        build_parser().parse_args(
            ["--bbox", "1", "2", "3", "4", "--area", "a", "--download", "b"]
        )
    )
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


def test_run_requires_an_area(capsys):
    """The message names ALL three ways to give one — a name, the corners, a snapshot.

    It is what a user sees after mistyping a place name, so telling them to use --bbox
    would send them back to the coordinates --place exists to spare them.
    """
    rc = run(build_parser().parse_args(["--min-gain", "100"]))
    assert rc == 2
    err = capsys.readouterr().err
    assert "--place" in err and "--bbox" in err and "--area" in err


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


_CURRENT_REGISTRY = object()  # "stamp whatever this build knows" — NOT `None`, which is
                              # itself a meaningful value here (a file that records nothing)


def _poi_snapshot(path, pois, *, poi_kinds=_CURRENT_REGISTRY):
    """A minimal snapshot carrying only POIs — the browse needs no routes at all.

    ``poi_kinds`` stands in for which build downloaded it: the current registry by
    default (a fresh download, as ``parse_area`` stamps it), a shorter tuple for an area
    saved by an older build, ``None`` for one saved before the field existed at all.
    """
    save_snapshot(
        AreaSnapshot(
            bbox=(50.72, 15.58, 50.78, 15.68),
            area=AreaData(
                pois=list(pois),
                poi_kinds=(
                    all_kinds()
                    if poi_kinds is _CURRENT_REGISTRY
                    else (None if poi_kinds is None else tuple(poi_kinds))
                ),
            ),
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
    assert "3 objects: 2 places of worship, 1 ruin" in out
    assert "Nístějka" in out and "Sv. Petr" in out
    # An unnamed object still gets a line, labelled by its kind rather than blank.
    assert out.count("place of worship") >= 2


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
    # No objects AND no kind record: the file predates the feature entirely, which is a
    # different claim from the "kinds not recorded" case below it (that one HAS objects).
    _poi_snapshot(path, [], poi_kinds=None)
    assert run(_parse("--show-pois", "--area", str(path))) == 0
    out = capsys.readouterr().out
    assert "saved before the feature existed" in out
    assert "pick other --poi kinds" not in out


def test_show_pois_names_kinds_the_area_predates(tmp_path, capsys):
    """An area saved by an older build is full of objects and never looked for a tree.
    Asking it for one must not print the "nothing of that kind is mapped" line."""
    path = tmp_path / "older.json"
    _poi_snapshot(path, _DEMO_POIS, poi_kinds=[k for k in all_kinds() if k != "tree"])
    assert run(_parse("--show-pois", "--area", str(path), "--poi", "tree")) == 0
    cap = capsys.readouterr()
    assert "predates the kind tree" in cap.err
    assert "fact about the file, not the landscape" in cap.err

    # And it is said even when the listing is NOT empty: the ruins come back, the tree
    # question was never asked, and printed bare the ruins read as the whole answer.
    assert run(_parse("--show-pois", "--area", str(path), "--poi", "ruins,tree")) == 0
    cap = capsys.readouterr()
    assert "1 object: 1 ruin" in cap.out
    assert "predates the kind tree" in cap.err


def test_list_areas_says_how_far_behind_the_registry_a_file_is(tmp_path, capsys, monkeypatch):
    """The inventory is where an area is picked, so it is the cheapest place to find out
    the file is behind — before a search returns a confident empty list from it."""
    monkeypatch.setenv("HIKE_SNAPSHOT_DIR", str(tmp_path))
    _poi_snapshot(tmp_path / "current.json", _DEMO_POIS)
    _poi_snapshot(
        tmp_path / "older.json", _DEMO_POIS, poi_kinds=[k for k in all_kinds() if k != "tree"]
    )
    _poi_snapshot(tmp_path / "unrecorded.json", _DEMO_POIS, poi_kinds=None)
    assert run(_parse("--list-areas")) == 0
    out = capsys.readouterr().out
    # Each area prints over TWO lines (name + bbox, then the counts), so an entry is the
    # header line plus the one after it — matching only the first would look at the bbox.
    rows = out.splitlines()
    lines = {}
    for name in ("current", "older", "unrecorded"):
        i = next(i for i, line in enumerate(rows) if line.strip().startswith(name))
        lines[name] = "\n".join(rows[i:i + 2])
    # The current file says nothing beyond its count — a warning that never turns off is
    # noise, and this is the state everything is meant to end up in.
    assert "newer than it" not in lines["current"] and "not recorded" not in lines["current"]
    assert "1 kind(s) newer than it" in lines["older"]
    assert "kinds not recorded" in lines["unrecorded"]


def test_list_areas_separates_an_empty_area_from_one_that_never_looked(
    tmp_path, capsys, monkeypatch
):
    """Zero POIs is two different facts, and only one of them is worth a re-download.

    A file that recorded the kind set and still holds nothing is reporting the ground.
    Telling that user to re-download is the lie the kind record retires — and the count
    alone cannot tell the two apart, which is why the inventory reads the record.
    """
    monkeypatch.setenv("HIKE_SNAPSHOT_DIR", str(tmp_path))
    _poi_snapshot(tmp_path / "barren.json", [])                    # looked, found none
    _poi_snapshot(tmp_path / "prepoi.json", [], poi_kinds=None)    # never looked
    assert run(_parse("--list-areas")) == 0
    rows = capsys.readouterr().out.splitlines()
    entry = lambda name: "\n".join(  # noqa: E731
        rows[next(i for i, r in enumerate(rows) if r.strip().startswith(name)) :][:2]
    )
    assert "no POIs mapped here" in entry("barren")
    assert "re-download" not in entry("barren")
    assert "no POIs (re-download for --poi)" in entry("prepoi")


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
    err = capsys.readouterr().err
    assert "--show-pois needs an area" in err
    assert "--place" in err and "--bbox" in err and "--area" in err
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


def test_show_pois_says_which_flags_it_ignored(tmp_path, capsys):
    """A filter that silently does nothing is the outcome this project forbids.

    Not an error — they are plausibly left over from the previous command — but the run
    must say the walk-shaped flags had nothing to act on.
    """
    path = tmp_path / "demo.json"
    _poi_snapshot(path, _DEMO_POIS)
    assert run(_parse(
        "--show-pois", "--area", str(path), "--min-gain", "500", "--circular",
        "--poi-radius", "100",
    )) == 0
    err = capsys.readouterr().err
    assert "--min-gain" in err and "--circular" in err and "--poi-radius" in err
    assert "do not apply and were ignored" in err


def test_show_pois_stays_quiet_when_nothing_was_ignored(tmp_path, capsys):
    path = tmp_path / "demo.json"
    _poi_snapshot(path, _DEMO_POIS)
    assert run(_parse("--show-pois", "--area", str(path), "--poi", "ruins")) == 0
    assert "ignored" not in capsys.readouterr().err


def _ferrata_snapshot(path):
    """A saved area holding one cabled way and one walkable hiking route."""
    from hike_finder.overpass import parse_area
    from hike_finder.snapshot import AreaSnapshot, save_snapshot

    area = parse_area([
        {"type": "relation", "id": 1, "tags": {"route": "hiking", "name": "Walk"},
         "members": [{"type": "way", "ref": 100, "role": "", "geometry": [
             {"lat": 46.50, "lon": 12.10}, {"lat": 46.51, "lon": 12.10}]}]},
        {"type": "way", "id": 100, "tags": {"highway": "path"}},
        {"type": "way", "id": 500, "tags": {"highway": "via_ferrata", "name": "VF Test"},
         "geometry": [{"lat": 46.52, "lon": 12.11}, {"lat": 46.525, "lon": 12.11}]},
    ])
    save_snapshot(
        AreaSnapshot(bbox=(46.4, 12.0, 46.6, 12.3), area=area, elevations={},
                     sample_interval_m=25.0),
        path,
    )


def test_show_ferrata_says_which_flags_it_ignored(tmp_path, capsys):
    """Same convention as --show-pois, and it has to be repeated rather than inherited:
    the two browse modes share no code path, so nothing else would catch a silent drop.
    `--gpx` is on the list deliberately — the mode cannot export (see HANDOFF), and
    accepting the flag while writing no file is exactly the silence forbidden here."""
    path = tmp_path / "vf.json"
    _ferrata_snapshot(path)
    assert run(_parse(
        "--show-ferrata", "--area", str(path), "--min-gain", "500",
        "--gpx", str(tmp_path / "out.gpx"),
    )) == 0
    err = capsys.readouterr().err
    assert "--min-gain" in err and "--gpx" in err
    assert "do not apply and were ignored" in err
    # ...and it really did not write the file it was handed.
    assert not (tmp_path / "out.gpx").exists()


def test_show_ferrata_stays_quiet_when_nothing_was_ignored(tmp_path, capsys):
    path = tmp_path / "vf.json"
    _ferrata_snapshot(path)
    assert run(_parse("--show-ferrata", "--area", str(path))) == 0
    captured = capsys.readouterr()
    assert "ignored" not in captured.err
    assert "VF Test" in captured.out


def test_the_two_inventories_refuse_to_run_together(tmp_path, capsys):
    """The guard has to sit ABOVE both branches: --show-pois returns first, so a check
    inside the --show-ferrata branch could never fire and the pair would silently list
    only the points of interest."""
    path = tmp_path / "vf.json"
    _ferrata_snapshot(path)
    assert run(_parse("--show-ferrata", "--show-pois", "--area", str(path))) == 2
    assert "two different inventories" in capsys.readouterr().err


def test_show_ferrata_rejects_the_point_based_flags(capsys):
    assert run(_parse(
        "--show-ferrata", "--bbox", "46.5", "12.0", "46.6", "12.2",
        "--around", "46.5", "12.1",
    )) == 2
    assert "without routing to them" in capsys.readouterr().err


def test_show_ferrata_and_the_route_filter_are_different_questions(capsys):
    assert run(_parse(
        "--show-ferrata", "--bbox", "46.5", "12.0", "46.6", "12.2", "--no-ferrata",
    )) == 2
    assert "filter ROUTES" in capsys.readouterr().err


def test_avoidance_always_states_that_it_is_not_a_guarantee(tmp_path, capsys):
    """Printed on every --no-ferrata run, not buried in --help: the flag reads as a
    safety promise unless something says otherwise, every single time."""
    path = tmp_path / "vf.json"
    _ferrata_snapshot(path)
    run(_parse("--no-ferrata", "--area", str(path)))
    assert "not a safety guarantee" in capsys.readouterr().err
