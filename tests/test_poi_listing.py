"""Browsing an area's points of interest — the objects themselves, with no routes.

The third POI question the project answers, after "which routes pass one?" (``--poi``)
and "draw me a route to the nearest one" (``--to-poi``). Being a *listing*, its failure
modes are different from a search's, and the tests here target those specifically:

  * ``test_offline_matches_live`` — the invariant this project re-pins for every feature.
    The same ``AreaData`` through the live path and the snapshot path must produce
    byte-identical output, including the exported file. Two code paths that answer the
    same question are two code paths that can drift.
  * ``test_empty_kinds_lists_everything`` — ``()`` means *match nothing* to ``route_pois``
    and *everything* to ``select_pois``. The two readings differ by a whole result set, so
    the browse's expansion is pinned rather than assumed.
  * ``test_listing_never_touches_elevation`` — the mode's selling point is that it spends
    no elevation quota. A property nobody checks is a property that quietly goes away.
  * ``test_pre_poi_snapshot_says_it_cannot_know`` — "this area has no ruins" and "this file
    predates the feature" must never be the same answer.

Pure and network-free throughout: the one live entry point is exercised with a stubbed
``_fetch_area``, the same trick the compose/routing live tests use.
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET

import pytest

from hike_finder import search as S
from hike_finder.export import pois_to_geojson, pois_to_gpx
from hike_finder.format import format_poi, format_poi_summary
from hike_finder.overpass import AreaData
from hike_finder.poi import (
    POI_KINDS,
    PoiHit,
    PoiPlace,
    all_kinds,
    count_by_kind,
    kind_label,
    select_pois,
    unrecorded_kinds,
)
from hike_finder.snapshot import AreaSnapshot, load_snapshot, save_snapshot

BBOX = (50.72, 15.58, 50.78, 15.68)

# A deliberately unsorted list: two churches (one unnamed), a ruin, a viewpoint. Order
# here is registry-*violating* on purpose, so a passing order test means the code sorted
# rather than that the input happened to be sorted.
RAW_POIS = [
    {"coord": (50.7511, 15.6202), "kind": "viewpoint", "name": "Vyhlídka"},
    {"coord": (50.7301, 15.6001), "kind": "ruins", "name": "Nístějka"},
    {"coord": (50.7411, 15.6102), "kind": "church", "name": None},
    {"coord": (50.7211, 15.5902), "kind": "church", "name": "Sv. Petr"},
]


_CURRENT_REGISTRY = object()  # NOT `None`: `None` is itself a meaningful kind-set value


def _area(pois=None, *, poi_kinds=_CURRENT_REGISTRY) -> AreaData:
    """An area as a download leaves it: POIs plus the kind set they were sorted into.

    ``poi_kinds`` is what makes a fixture stand for one build or another — the current
    registry by default (what ``parse_area`` stamps), a shorter tuple for an area saved
    by an older build, ``None`` for one saved before the field existed.
    """
    return AreaData(
        pois=[dict(p) for p in (pois if pois is not None else RAW_POIS)],
        poi_kinds=(
            all_kinds()
            if poi_kinds is _CURRENT_REGISTRY
            else (None if poi_kinds is None else tuple(poi_kinds))
        ),
    )


# --------------------------------------------------------------------------- selection


def test_selects_only_the_asked_for_kinds():
    got = select_pois(RAW_POIS, ("church",))
    assert [p.kind for p in got] == ["church", "church"]
    assert {p.name for p in got} == {None, "Sv. Petr"}


def test_empty_kinds_lists_everything():
    """``()`` means EVERY kind here — the opposite of ``route_pois``, where it means none.

    The two conventions live in the same module, so the difference is pinned rather than
    left to a reader's memory. "Show me what's here" with nothing ticked is a browse.
    """
    assert len(select_pois(RAW_POIS)) == len(RAW_POIS)
    assert len(select_pois(RAW_POIS, ())) == len(RAW_POIS)
    assert len(select_pois(RAW_POIS, [])) == len(RAW_POIS)


def test_order_is_registry_then_name_then_coord():
    # church < ruins < viewpoint in POI_KINDS insertion order, regardless of the input
    # order or alphabetical order of the labels.
    got = select_pois(RAW_POIS)
    assert [p.kind for p in got] == ["church", "church", "ruins", "viewpoint"]
    # Within a kind: unnamed ("") sorts before "Sv. Petr".
    assert [p.name for p in got[:2]] == [None, "Sv. Petr"]
    # And it is stable: the same input twice gives the same tuple.
    assert select_pois(RAW_POIS) == got


def test_duplicate_objects_are_listed_once():
    doubled = RAW_POIS + [dict(RAW_POIS[1])]
    assert len(select_pois(doubled)) == len(RAW_POIS)


def test_unknown_kind_raises_rather_than_listing_nothing():
    with pytest.raises(ValueError) as e:
        select_pois(RAW_POIS, ("castel",))
    assert "castel" in str(e.value) and "castle" in str(e.value)


def test_selection_is_not_clipped_to_any_box():
    """A large object's ``out center`` point can land outside the box it intersects.

    Dropping it would be the one failure this project forbids — silent. Over-showing is
    visible on the map and reasonable-about, so the listing keeps everything Overpass
    returned for the area.
    """
    far = {"coord": (0.0, 0.0), "kind": "ruins", "name": "Way outside the bbox"}
    got = select_pois(RAW_POIS + [far])
    assert any(p.name == "Way outside the bbox" for p in got)


def test_objects_with_no_coordinate_are_skipped():
    got = select_pois([{"kind": "ruins", "name": "no coord"}] + RAW_POIS)
    assert len(got) == len(RAW_POIS)


# ------------------------------------------------------------------------ the two types


def test_place_and_hit_share_one_label_source():
    """Both POI types render a kind through ``kind_label``, so they cannot disagree.

    Two independent ``POI_KINDS.get(...)`` lookups is exactly the drift this module's
    registry design exists to prevent.
    """
    for kind in POI_KINDS:
        place = PoiPlace(kind=kind, name=None, coord=(50.0, 15.0))
        hit = PoiHit(kind=kind, name=None, coord=(50.0, 15.0), distance_m=10.0)
        assert place.label == hit.label == kind_label(kind)
    # An unregistered kind degrades to itself rather than raising or rendering blank.
    assert kind_label("not-a-kind") == "not-a-kind"


def test_place_never_claims_a_distance():
    """The reason ``PoiPlace`` exists: a listed object has no route to be measured from.

    A ``PoiHit`` reused with ``distance_m=0`` would render "(0 m)" and read as sitting on
    the trail — a claim nobody made.
    """
    p = PoiPlace(kind="ruins", name="Nístějka", coord=(50.73, 15.60))
    assert "0 m" not in p.describe() and "m)" not in p.describe()
    assert "distance_m" not in p.to_dict()
    assert "Nístějka" in p.describe() and "ruin" in p.describe()


# ---------------------------------------------------------------------------- rendering


def test_summary_counts_by_kind_in_registry_order():
    s = format_poi_summary(select_pois(RAW_POIS))
    assert s.startswith("4 objects: ")
    assert s.index("places of worship") < s.index("ruin") < s.index("viewpoint")


def test_summary_uses_the_singular_label_for_one():
    assert "1 ruin," in format_poi_summary(select_pois(RAW_POIS))
    assert "1 ruins" not in format_poi_summary(select_pois(RAW_POIS))
    assert format_poi_summary([]) == "no points of interest"


def test_count_by_kind_omits_empty_kinds():
    counts = dict(count_by_kind(select_pois(RAW_POIS)))
    assert counts == {"church": 2, "ruins": 1, "viewpoint": 1}
    assert "cave" not in counts


def test_format_poi_is_the_shared_one_liner():
    p = select_pois(RAW_POIS, ("ruins",))[0]
    assert format_poi(p) == p.describe()


# ------------------------------------------------------------------------------ exports


def test_gpx_is_waypoints_with_the_right_axes():
    places = select_pois(RAW_POIS, ("ruins",))
    root = ET.fromstring(pois_to_gpx(places))
    ns = "{http://www.topografix.com/GPX/1/1}"
    wpts = root.findall(f"{ns}wpt")
    assert len(wpts) == 1
    assert not root.findall(f"{ns}trk")  # a listing has no tracks — nothing is walked
    assert float(wpts[0].get("lat")) == pytest.approx(50.7301)
    assert float(wpts[0].get("lon")) == pytest.approx(15.6001)
    assert wpts[0].find(f"{ns}name").text == "Nístějka"
    assert wpts[0].find(f"{ns}type").text == "ruins"   # the machine-readable kind


def test_gpx_names_an_unnamed_object_by_its_kind():
    """A GPS shows the name and nothing else — an empty one is an anonymous pin."""
    unnamed = [p for p in select_pois(RAW_POIS, ("church",)) if p.name is None]
    root = ET.fromstring(pois_to_gpx(unnamed))
    ns = "{http://www.topografix.com/GPX/1/1}"
    assert root.find(f"{ns}wpt/{ns}name").text == "place of worship"


def test_geojson_is_points_in_lon_lat_order():
    fc = json.loads(pois_to_geojson(select_pois(RAW_POIS, ("ruins",))))
    assert fc["type"] == "FeatureCollection"
    feat = fc["features"][0]
    assert feat["geometry"]["type"] == "Point"
    # RFC 7946 order: longitude first. The one axis swap this project keeps in export.py.
    assert feat["geometry"]["coordinates"] == [pytest.approx(15.6001), pytest.approx(50.7301)]
    assert feat["properties"]["kind"] == "ruins"
    assert feat["properties"]["label"] == "ruin"


def test_empty_exports_are_valid_documents():
    """A downstream script always gets a file, never zero bytes or a crash."""
    ET.fromstring(pois_to_gpx([]))  # parses => well-formed
    assert json.loads(pois_to_geojson([])) == {"type": "FeatureCollection", "features": []}


def test_export_escapes_markup_in_a_name():
    place = [PoiPlace(kind="ruins", name='A & B <hall> "x"', coord=(50.0, 15.0))]
    ns = "{http://www.topografix.com/GPX/1/1}"
    root = ET.fromstring(pois_to_gpx(place))          # would raise if unescaped
    assert root.find(f"{ns}wpt/{ns}name").text == 'A & B <hall> "x"'


# ------------------------------------------------------------------ the two search paths


def test_offline_matches_live(monkeypatch, tmp_path):
    """The invariant: one area, two paths, byte-identical answers — listing AND file.

    ``list_area_pois`` fetches and selects; ``list_snapshot_pois`` loads and selects. They
    are two functions answering one question, which is precisely when two code paths
    drift. Pinned through a real snapshot round-trip, not a shared in-memory object, so a
    serialisation bug counts as drift too.
    """
    area = _area()
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: area)
    live = S.list_area_pois(BBOX, (), S._config.load())

    path = tmp_path / "snap.json"
    save_snapshot(
        AreaSnapshot(bbox=BBOX, area=_area(), elevations={}, sample_interval_m=25.0), path
    )
    offline = S.list_snapshot_pois(load_snapshot(path), ())

    assert live == offline
    assert format_poi_summary(live) == format_poi_summary(offline)
    assert pois_to_gpx(live) == pois_to_gpx(offline)
    assert pois_to_geojson(live) == pois_to_geojson(offline)


def test_offline_matches_live_for_a_kind_subset(monkeypatch, tmp_path):
    area = _area()
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: area)
    live = S.list_area_pois(BBOX, ("church", "viewpoint"), S._config.load())
    path = tmp_path / "snap.json"
    save_snapshot(
        AreaSnapshot(bbox=BBOX, area=_area(), elevations={}, sample_interval_m=25.0), path
    )
    assert live == S.list_snapshot_pois(load_snapshot(path), ("church", "viewpoint"))
    assert {p.kind for p in live} == {"church", "viewpoint"}


def test_listing_never_touches_elevation(monkeypatch):
    """One Overpass call, no elevation provider, nothing spent from the daily cap.

    That economy is the mode's selling point and is stated in its docstring, so it is
    pinned: constructing a provider here would burn quota for a question that measures
    nothing.
    """
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: _area())

    def _boom(*a, **k):  # pragma: no cover - the test fails by being called
        raise AssertionError("the listing must not build an elevation provider")

    monkeypatch.setattr(S, "_provider", _boom)
    assert len(S.list_area_pois(BBOX, (), S._config.load())) == 4


def test_pre_poi_snapshot_says_it_cannot_know(tmp_path, caplog):
    """"No ruins here" vs "this file predates the feature" are different answers."""
    path = tmp_path / "old.json"
    save_snapshot(
        AreaSnapshot(
            bbox=BBOX,
            area=AreaData(),  # no objects AND no kind record: predates POIs outright
            elevations={},
            sample_interval_m=25.0,
        ),
        path,
    )
    with caplog.at_level(logging.WARNING):
        assert S.list_snapshot_pois(load_snapshot(path), ()) == ()
    assert "predates the feature" in caplog.text


def test_snapshot_with_pois_but_none_of_that_kind_is_not_flagged_stale(tmp_path, caplog):
    """The complement: a real, current snapshot that simply has no caves must NOT claim
    to predate anything — that would send the user off to re-download for nothing."""
    path = tmp_path / "new.json"
    save_snapshot(
        AreaSnapshot(bbox=BBOX, area=_area(), elevations={}, sample_interval_m=25.0), path
    )
    with caplog.at_level(logging.WARNING):
        assert S.list_snapshot_pois(load_snapshot(path), ("cave",)) == ()
    assert "predates the feature" not in caplog.text
    # Nor may it hedge: this file states the kind set it was sorted into, `cave` is in it,
    # so the empty answer is about the landscape and must read that way.
    assert "does not record which kinds" not in caplog.text


# --------------------------------------------------------------- the kind-set diff
#
# The gap the nine v0.5.0 kinds opened, and the one an emptiness check cannot see: a file
# downloaded against a 19-kind registry is FULL of objects and holds none of the nine
# added after it, so asking it for a `tree` returns a confident empty list for a question
# the download never asked. The snapshot therefore records the kind set it was classified
# against, and `poi.unrecorded_kinds` diffs it against the registry. Four states, and the
# tests below exist because collapsing any two of them is a lie with a different shape.


def test_unrecorded_kinds_separates_covered_missing_and_unrecorded():
    """The three answers, at the source. `()` and `None` are NOT interchangeable."""
    # Covered: the file looked for what is being asked.
    assert unrecorded_kinds(all_kinds(), ("cave", "ruins")) == ()
    # Missing: named, so a caller can say WHICH questions this file never asked.
    older = tuple(k for k in all_kinds() if k not in ("tree", "mill"))
    assert unrecorded_kinds(older, ("ruins", "tree", "mill")) == ("tree", "mill")
    # Unrecorded: `None` in, `None` out — "cannot say", which is not "covers nothing"
    # (that would warn on every browse of a good older file) and not "covers everything"
    # (a confident empty list for a question never asked).
    assert unrecorded_kinds(None, ("tree",)) is None
    # An empty request expands to the whole registry, matching `select_pois`'s convention
    # — the opposite of `route_pois`'s, so it is pinned rather than assumed. The result
    # keeps REGISTRY order (not the caller's, not sorted), so a frontend listing the gap
    # names it in the same order as the `--list-pois` menu the user just read.
    assert unrecorded_kinds(older) == tuple(
        k for k in all_kinds() if k in ("tree", "mill")
    )
    assert set(unrecorded_kinds(older)) == {"tree", "mill"}


def test_a_parsed_area_stamps_the_registry_it_was_classified_against():
    """The record has to come from where `classify` runs, or it is a second source of
    truth free to disagree with the objects it describes."""
    from hike_finder.overpass import parse_area

    area = parse_area([])
    assert area.poi_kinds == all_kinds()
    # And an area nobody parsed says so, rather than inheriting the current registry.
    assert AreaData().poi_kinds is None


def test_the_kind_set_round_trips_and_a_legacy_file_stays_unrecorded(tmp_path):
    """Three files, three answers on load — including the one that must NOT be invented."""
    from hike_finder.snapshot import snapshot_from_json, snapshot_to_json

    path = tmp_path / "current.json"
    save_snapshot(
        AreaSnapshot(bbox=BBOX, area=_area(), elevations={}, sample_interval_m=25.0), path
    )
    assert load_snapshot(path).area.poi_kinds == all_kinds()

    # A file written by an older build: the key is there but shorter.
    older = tuple(k for k in all_kinds() if k != "tree")
    p2 = tmp_path / "older.json"
    save_snapshot(
        AreaSnapshot(
            bbox=BBOX, area=_area(poi_kinds=older), elevations={}, sample_interval_m=25.0
        ),
        p2,
    )
    assert load_snapshot(p2).area.poi_kinds == older

    # A file written before the key existed. Loading must NOT fabricate a set, and
    # re-saving it must not either: writing `[]` would upgrade "cannot say" into
    # "positively covered nothing", a stronger claim than the file supports.
    legacy = snapshot_to_json(
        AreaSnapshot(bbox=BBOX, area=_area(), elevations={}, sample_interval_m=25.0)
    )
    legacy["area"].pop("poi_kinds")
    reloaded = snapshot_from_json(legacy)
    assert reloaded.area.poi_kinds is None
    assert "poi_kinds" not in snapshot_to_json(reloaded)["area"]


def test_a_kind_newer_than_the_snapshot_is_named_not_reported_as_absent(tmp_path, caplog):
    """The whole point: an empty `tree` listing from a 19-kind file is about the FILE."""
    older = tuple(k for k in all_kinds() if k not in ("tree", "mill"))
    path = tmp_path / "older.json"
    save_snapshot(
        AreaSnapshot(
            bbox=BBOX, area=_area(poi_kinds=older), elevations={}, sample_interval_m=25.0
        ),
        path,
    )
    with caplog.at_level(logging.WARNING):
        assert S.list_snapshot_pois(load_snapshot(path), ("tree",)) == ()
    assert "tree" in caplog.text and "re-download" in caplog.text
    # It must not fall back to the pre-POI wording: this file has plenty of objects.
    assert "predates the feature" not in caplog.text


def test_a_missing_kind_is_announced_even_when_other_kinds_returned_objects(tmp_path, caplog):
    """A non-empty list is the sneaky case — ruins come back, trees were never looked
    for, and printed bare the ruins read as the whole answer to "ruins and trees"."""
    older = tuple(k for k in all_kinds() if k != "tree")
    path = tmp_path / "older.json"
    save_snapshot(
        AreaSnapshot(
            bbox=BBOX, area=_area(poi_kinds=older), elevations={}, sample_interval_m=25.0
        ),
        path,
    )
    with caplog.at_level(logging.WARNING):
        places = S.list_snapshot_pois(load_snapshot(path), ("ruins", "tree"))
    assert [p.kind for p in places] == ["ruins"]     # the objects it does carry
    assert "tree" in caplog.text                      # and the question it never asked


def test_an_unrecorded_kind_set_hedges_even_when_objects_came_back(tmp_path, caplog):
    """A file saved between the POI feature and this one cannot vouch for any kind, and
    a non-empty result does NOT discharge that.

    Gating the hedge on emptiness was the first design and it has a hole worth keeping a
    test on: ask an unrecorded file for ``ruins,tree``, get the ruins, and the half of
    the question nobody may have asked disappears behind the half that was answered. A
    non-empty result proves only that SOME requested kind was classified. The pre-POI and
    ``transit_access`` warnings already fire on the filter being active rather than on
    the result being empty; this matches them.
    """
    path = tmp_path / "unrecorded.json"
    save_snapshot(
        AreaSnapshot(
            bbox=BBOX, area=_area(poi_kinds=None), elevations={}, sample_interval_m=25.0
        ),
        path,
    )
    snap = load_snapshot(path)
    with caplog.at_level(logging.WARNING):
        assert S.list_snapshot_pois(snap, ("cave",)) == ()
    assert "does not record which kinds" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        places = S.list_snapshot_pois(snap, ("ruins", "tree"))
    assert [p.kind for p in places] == ["ruins"]        # one half of the question…
    assert "does not record which kinds" in caplog.text  # …and the caveat on the other


def test_an_empty_area_that_recorded_its_kinds_is_a_real_answer(tmp_path, caplog):
    """The state the record buys: "looked for all 28, found none" — a fact about the
    landscape. Before the field this was indistinguishable from a pre-POI file, so it
    was reported as one and sent the user off to re-download for nothing."""
    path = tmp_path / "empty.json"
    save_snapshot(
        AreaSnapshot(
            bbox=BBOX, area=_area([]), elevations={}, sample_interval_m=25.0
        ),
        path,
    )
    with caplog.at_level(logging.WARNING):
        assert S.list_snapshot_pois(load_snapshot(path), ()) == ()
    assert caplog.text == ""
    assert S.snapshot_poi_gap(load_snapshot(path), ()) == ("ok", ())
