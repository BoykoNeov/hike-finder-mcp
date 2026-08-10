"""Ferrata end to end: query -> parse -> snapshot -> filter -> frontends.

`test_ferrata.py` pins the pure predicate and summary. This file pins the wiring, and
every case here guards a way the wiring could quietly betray the pure part:

  - a `route=via_ferrata` relation landing in `routes` (where an ordinary search would
    return it, and `--compose-loops` would stitch loops through it);
  - a cabled route surviving `--no-ferrata`;
  - a pre-ferrata snapshot answering "none here" instead of "nobody looked";
  - a synthesised route measured too LATE to be filtered on.

The last one is not hypothetical: surface is attached after `find_hikes` and that is
fine because nothing filters on it. Doing the same for ferrata emptied every
`--compose-loops --no-ferrata` search, and only an ordering test catches it.
"""
import json
import logging

import pytest

from hike_finder.ferrata import FerrataSummary, select_ferrata
from hike_finder.filters import Criteria, Hike, find_hikes
from hike_finder.format import format_hike, hike_to_dict
from hike_finder.overpass import AreaData, build_query, parse_area
from hike_finder.search import (
    area_ferrata_readable,
    area_records_ferrata,
    ferrata_coverage_caveat,
    ferrata_gap_message,
    list_snapshot_ferrata,
)
from hike_finder.snapshot import AreaSnapshot, snapshot_from_json, snapshot_to_json


class _FlatElevation:
    """A flat world. Every assertion here is about the CHEAP pass, so the elevation
    numbers are irrelevant — but survivors of the cheap pass do get looked up, and a
    stub that refused would only be testing the two-pass ordering all over again."""

    def lookup(self, points):
        return [0.0] * len(points)


def _leg(lat0, metres):
    return [{"lat": lat0, "lon": 12.1}, {"lat": lat0 + metres / 111_195.0, "lon": 12.1}]


def _relation(rid, route, tags=None, members=(("100", 46.5, 800),)):
    return {
        "type": "relation",
        "id": rid,
        "tags": {"route": route, "name": f"rel-{rid}", **(tags or {})},
        "members": [
            {"type": "way", "ref": int(ref), "role": "", "geometry": _leg(lat, m)}
            for ref, lat, m in members
        ],
    }


# ------------------------------------------------------------------------ the query


def test_query_asks_for_ferrata_relations_and_ways():
    q = build_query(46.4, 12.0, 46.6, 12.3)
    assert '"route"="via_ferrata"' in q
    assert '"highway"="via_ferrata"' in q
    # The grade key gets its OWN clause: a third of the graded ways in Cortina are
    # `highway=path`, so the highway clause alone would not fetch them.
    assert '"via_ferrata_scale"' in q


def test_ferrata_relations_ride_the_existing_member_way_join():
    """They sit in the same union as the hiking relations, so the one `way(r); out tags;`
    statement covers their members too — no second join, no extra round trip."""
    q = build_query(46.4, 12.0, 46.6, 12.3)
    member_join = q.index("way(r);")
    # The relation is fetched BEFORE the member-way join, so `way(r)` covers it.
    assert q.index('"route"="via_ferrata"') < member_join
    # The standalone-way clause comes after, and is a separate statement — it must not
    # be mistaken for part of the union the join reads from.
    assert q.index('"highway"="via_ferrata"') > member_join


# ------------------------------------------------------------------------ the parse


def test_a_ferrata_relation_never_lands_in_routes():
    """THE containment case. In `routes` it would be returned by an ordinary search and
    stitched into synthesised loops by --compose-loops."""
    area = parse_area([
        _relation(1, "hiking"),
        _relation(2, "via_ferrata"),
    ])
    assert [r["id"] for r in area.routes] == [1]
    assert [r["id"] for r in area.ferrata_routes] == [2]


def test_ferrata_relations_keep_the_ordinary_route_shape():
    """Same dict as a hiking route, so every downstream measurement works unchanged."""
    area = parse_area([_relation(2, "via_ferrata", {"via_ferrata_scale": "3"})])
    r = area.ferrata_routes[0]
    assert set(r) >= {"id", "name", "ref", "tags", "ways", "way_tags", "unnamed"}
    assert r["tags"]["via_ferrata_scale"] == "3"


def test_cabled_ways_are_collected_with_geometry_and_deduped():
    """A way matching BOTH ferrata clauses is emitted twice by Overpass."""
    way = {
        "type": "way",
        "id": 500,
        "tags": {"highway": "via_ferrata", "via_ferrata_scale": "2+", "name": "VF Test"},
        "geometry": _leg(46.5, 400),
    }
    area = parse_area([way, dict(way)])
    assert len(area.ferrata_ways) == 1
    assert area.ferrata_ways[0]["name"] == "VF Test"
    assert area.ferrata_ways[0]["scale"] == "2+"
    assert len(area.ferrata_ways[0]["coords"]) == 2


def test_a_graded_path_is_collected_too():
    """25 of Cortina's 70 graded ways are `highway=path` — the clause exists for them."""
    area = parse_area([{
        "type": "way", "id": 501,
        "tags": {"highway": "path", "via_ferrata_scale": "1+"},
        "geometry": _leg(46.5, 300),
    }])
    assert len(area.ferrata_ways) == 1


def test_a_cabled_way_is_never_filed_as_a_point_of_interest():
    """The branch sits above the POI branch, like the access branches: a way that is
    both cabled and registry-tagged is a hazard first."""
    area = parse_area([{
        "type": "way", "id": 502,
        "tags": {"highway": "via_ferrata", "tourism": "viewpoint"},
        "geometry": _leg(46.5, 200),
    }])
    assert len(area.ferrata_ways) == 1
    assert area.pois == []


def test_tag_only_member_records_are_still_not_features():
    """`way(r); out tags;` records have no geometry; a cabled one must join into
    way_tags rather than become a standalone entry in the ways list."""
    area = parse_area([
        _relation(1, "hiking", members=(("100", 46.5, 800),)),
        {"type": "way", "id": 100, "tags": {"highway": "via_ferrata"}},
    ])
    assert area.ferrata_ways == []
    assert area.routes[0]["way_tags"] == [{"highway": "via_ferrata"}]


def test_a_live_parse_always_records_both_lists():
    """Even with nothing to put in them — that pairing is what tells a fresh empty area
    apart from a file that never looked."""
    area = parse_area([_relation(1, "hiking")])
    assert area.ferrata_routes == [] and area.ferrata_ways == []


# --------------------------------------------------------------------- the snapshot


def _snapshot(area):
    return AreaSnapshot(bbox=(46.4, 12.0, 46.6, 12.3), area=area, elevations={},
                        sample_interval_m=25.0)


def test_snapshot_round_trips_both_ferrata_lists():
    area = parse_area([
        _relation(1, "hiking"),
        _relation(2, "via_ferrata", {"via_ferrata_scale": "3"}),
        {"type": "way", "id": 500, "tags": {"highway": "via_ferrata", "name": "VF"},
         "geometry": _leg(46.5, 400)},
    ])
    back = snapshot_from_json(json.loads(json.dumps(snapshot_to_json(_snapshot(area)))))
    assert [r["id"] for r in back.area.routes] == [1]
    assert [r["id"] for r in back.area.ferrata_routes] == [2]
    assert back.area.ferrata_routes[0]["tags"]["via_ferrata_scale"] == "3"
    assert back.area.ferrata_ways[0]["name"] == "VF"
    # Coords come back as tuples — the graph code needs them hashable.
    assert isinstance(back.area.ferrata_ways[0]["coords"][0], tuple)


def test_a_pre_ferrata_file_loads_as_unknown_not_empty():
    """The whole point of key presence. `[]` would claim the area was checked."""
    d = snapshot_to_json(_snapshot(parse_area([_relation(1, "hiking")])))
    del d["area"]["ferrata_routes"]
    del d["area"]["ferrata_ways"]
    back = snapshot_from_json(d)
    assert back.area.ferrata_routes is None
    assert back.area.ferrata_ways is None
    assert not area_records_ferrata(back.area)
    assert back.ferrata_count is None


def test_resaving_an_old_file_does_not_upgrade_unknown_to_empty():
    """`AreaData(ferrata_ways=None)` is reachable — load an old file and save it again.
    Writing `[]` then would turn "never looked" into "looked and found none"."""
    area = AreaData(routes=[], ferrata_routes=None, ferrata_ways=None)
    d = snapshot_to_json(_snapshot(area))
    assert "ferrata_routes" not in d["area"]
    assert "ferrata_ways" not in d["area"]


def test_a_fresh_empty_area_records_that_it_looked():
    area = parse_area([_relation(1, "hiking")])
    back = snapshot_from_json(snapshot_to_json(_snapshot(area)))
    assert back.area.ferrata_ways == [] and area_records_ferrata(back.area)
    assert back.ferrata_count == 0


def test_listing_a_pre_ferrata_snapshot_warns_and_returns_empty(caplog):
    d = snapshot_to_json(_snapshot(parse_area([_relation(1, "hiking")])))
    del d["area"]["ferrata_routes"]
    del d["area"]["ferrata_ways"]
    with caplog.at_level(logging.WARNING):
        assert list_snapshot_ferrata(snapshot_from_json(d)) == ()
    assert "predates" in caplog.text


# ----------------------------------------------------------------------- the filter


def _find(area, criteria):
    return find_hikes(area, _FlatElevation(), criteria, bbox=(46.4, 12.0, 46.6, 12.3))


def test_ferrata_routes_stay_invisible_to_an_ordinary_search():
    area = parse_area([_relation(1, "hiking"), _relation(2, "via_ferrata")])
    assert [h.osm_id for h in _find(area, Criteria())] == [1]


def test_ferrata_routes_appear_only_when_asked_for():
    area = parse_area([_relation(1, "hiking"), _relation(2, "via_ferrata")])
    assert [h.osm_id for h in _find(area, Criteria(ferrata=True))] == [2]


def test_a_hiking_route_with_a_cabled_member_is_found_by_the_target_search():
    """The commoner case by far: ~87 % of cabled ways are members of hiking relations."""
    area = parse_area([
        _relation(1, "hiking", members=(("100", 46.5, 800), ("200", 46.52, 300))),
        {"type": "way", "id": 100, "tags": {"highway": "path"}},
        {"type": "way", "id": 200, "tags": {"highway": "via_ferrata", "via_ferrata_scale": "2"}},
    ])
    found = _find(area, Criteria(ferrata=True))
    assert [h.osm_id for h in found] == [1]
    assert found[0].ferrata.present and found[0].ferrata.grades == ("2",)


def test_the_same_route_is_dropped_by_avoidance():
    area = parse_area([
        _relation(1, "hiking", members=(("100", 46.5, 800), ("200", 46.52, 300))),
        {"type": "way", "id": 100, "tags": {"highway": "path"}},
        {"type": "way", "id": 200, "tags": {"highway": "via_ferrata"}},
        _relation(3, "hiking", members=(("300", 46.4, 900),)),
        {"type": "way", "id": 300, "tags": {"highway": "path"}},
    ])
    assert [h.osm_id for h in _find(area, Criteria(ferrata=False))] == [3]


def test_a_short_cabled_section_still_trips_avoidance():
    """2.5 % of the length — under every gate `surface` applies. If this ever fails,
    someone routed the summary through a share-based helper."""
    area = parse_area([
        _relation(1, "hiking", members=(("100", 46.5, 11_700), ("200", 46.6, 300))),
        {"type": "way", "id": 100, "tags": {"highway": "path"}},
        {"type": "way", "id": 200, "tags": {"highway": "via_ferrata"}},
    ])
    assert _find(area, Criteria(ferrata=False)) == []


def test_an_unexaminable_route_fails_both_filters():
    """No member tags and a silent relation: not evidence of cable, and not evidence of
    safety either. Dropped in both directions, and the caller says why."""
    d = snapshot_to_json(_snapshot(parse_area([_relation(1, "hiking")])))
    for r in d["area"]["routes"]:
        r["way_tags"] = []
    area = snapshot_from_json(d).area
    assert _find(area, Criteria(ferrata=False)) == []
    assert _find(area, Criteria(ferrata=True)) == []
    # ...and the area is correctly reported as unable to answer, which is what turns
    # that empty list into a sentence instead of a verdict.
    assert not area_ferrata_readable(area)


def test_an_area_with_no_routes_is_not_reported_as_unreadable():
    """Vacuously readable: `no_routes_message` owns that case, and sending someone to
    re-download an area whose real problem is unmapped trails helps nobody."""
    assert area_ferrata_readable(AreaData(routes=[]))


def test_near_miss_never_relaxes_a_ferrata_filter():
    """"Almost not cabled" is not a thing. A relaxed pass must not surface a ferrata as
    close to what you asked for when you asked to avoid one."""
    area = parse_area([
        _relation(1, "hiking", members=(("100", 46.5, 800),)),
        {"type": "way", "id": 100, "tags": {"highway": "via_ferrata"}},
    ])
    assert find_hikes(
        area, _FlatElevation(), Criteria(ferrata=False),
        bbox=(46.4, 12.0, 46.6, 12.3), near_miss=True,
    ) == []


def test_composed_routes_are_filtered_on_ferrata_not_merely_labelled_with_it():
    """The ordering trap. A synthesised route's summary arrives through
    `pre_ferrata_by_id` BEFORE the cheap pass; attaching it afterwards (as surface is)
    leaves every route unknown at filter time and empties the search."""
    area = AreaData(routes=[{
        "id": -1, "name": "composed", "ref": None, "osmc_color": None,
        "tags": {"roundtrip": "yes"},
        "ways": [[(46.5, 12.1), (46.51, 12.1), (46.51, 12.11)]],
    }])
    clean = find_hikes(
        area, _FlatElevation(), Criteria(ferrata=False), bbox=(46.4, 12.0, 46.6, 12.3),
        pre_ferrata_by_id={-1: FerrataSummary(present=False)},
    )
    assert [h.osm_id for h in clean] == [-1]
    cabled = find_hikes(
        area, _FlatElevation(), Criteria(ferrata=False), bbox=(46.4, 12.0, 46.6, 12.3),
        pre_ferrata_by_id={-1: FerrataSummary(present=True, length_m=300.0)},
    )
    assert cabled == []


def test_an_id_missing_from_the_precomputed_map_stays_unknown():
    area = AreaData(routes=[{
        "id": -1, "name": "composed", "ref": None, "osmc_color": None,
        "tags": {"roundtrip": "yes"},
        "ways": [[(46.5, 12.1), (46.51, 12.1)]],
    }])
    hikes = find_hikes(
        area, _FlatElevation(), Criteria(), bbox=(46.4, 12.0, 46.6, 12.3),
        pre_ferrata_by_id={},
    )
    assert hikes[0].ferrata is None


# -------------------------------------------------------------------- the inventory


def test_inventory_puts_named_routes_before_single_ways():
    area = parse_area([
        _relation(2, "via_ferrata", {"name": "VF Alpha", "via_ferrata_scale": "3"}),
        {"type": "way", "id": 500, "tags": {"highway": "via_ferrata", "name": "Long way"},
         "geometry": _leg(46.5, 2000)},
        {"type": "way", "id": 501, "tags": {"highway": "via_ferrata"},
         "geometry": _leg(46.5, 100)},
    ])
    lines = select_ferrata(area.ferrata_routes, area.ferrata_ways)
    assert [f.source for f in lines] == ["route", "way", "way"]
    assert lines[0].name == "VF Alpha" and lines[0].scale == "3"
    # ...and within the ways, longest first.
    assert lines[1].name == "Long way"


def test_inventory_says_when_an_entry_is_only_one_way():
    """An unnamed 200 m way is often one pitch of a climb, not a climb."""
    area = parse_area([{
        "type": "way", "id": 501, "tags": {"highway": "via_ferrata", "via_ferrata_scale": "2"},
        "geometry": _leg(46.5, 200),
    }])
    text = select_ferrata(None, area.ferrata_ways)[0].describe()
    assert "unnamed via ferrata" in text and "single way" in text and "grade 2" in text


def test_inventory_is_empty_but_not_crashing_on_unknown_lists():
    assert select_ferrata(None, None) == ()


# --------------------------------------------------------------------- the rendering


def _hike(**over):
    base = dict(osm_id=1, name="R", distance_km=5.0, circular=False, car_access=False,
                chairlift_access=False, start=(46.5, 12.1), gain_m=100, loss_m=100)
    base.update(over)
    return Hike(**base)


def test_the_flag_survives_a_tiny_share_of_the_route():
    line = format_hike(_hike(ferrata=FerrataSummary(
        present=True, length_m=300.0, fraction=0.025, grades=("2+",))))
    assert "ferrata 2+ 0.3 km" in line


def test_no_flag_for_a_clean_route_and_none_for_an_unknown_one():
    assert "ferrata" not in format_hike(_hike(ferrata=FerrataSummary(present=False)))
    assert "ferrata" not in format_hike(_hike(ferrata=None))


def test_json_keeps_checked_and_clear_apart_from_never_checked():
    assert hike_to_dict(_hike(ferrata=None))["ferrata"] is None
    clean = hike_to_dict(_hike(ferrata=FerrataSummary(present=False)))["ferrata"]
    assert clean is not None and clean["present"] is False


def test_an_unreadable_file_never_gets_told_that_avoidance_still_works():
    """The wording bug this caught live. `ferrata_unrecorded_message` ends by promising
    that --no-ferrata works anyway — true only for a file that HAS member-way tags. The
    oldest snapshots (`ceskyraj.json` measurably among them) have neither, and told
    plainly that avoidance works they would silently return nothing instead."""
    d = snapshot_to_json(_snapshot(parse_area([_relation(1, "hiking")])))
    for r in d["area"]["routes"]:
        r["way_tags"] = []
    del d["area"]["ferrata_routes"]
    del d["area"]["ferrata_ways"]
    area = snapshot_from_json(d).area
    for finding in (True, False):
        msg = ferrata_gap_message(area, finding=finding)
        assert "cannot be detected on them at all" in msg
        assert "still" not in msg  # ...i.e. never the "avoidance works anyway" promise


def test_a_file_with_member_tags_but_no_ferrata_objects_gets_the_other_message():
    """The genuinely different case: finding is blocked, avoiding is not."""
    area = parse_area([
        _relation(1, "hiking", members=(("100", 46.5, 800),)),
        {"type": "way", "id": 100, "tags": {"highway": "path"}},
    ])
    area.ferrata_routes = area.ferrata_ways = None  # as a pre-ferrata file loads
    assert "predates cabled-route fetching" in ferrata_gap_message(area, finding=True)
    # ...and avoidance is not blocked at all, so there is nothing to say about it.
    assert ferrata_gap_message(area, finding=False) is None


def test_a_current_file_has_nothing_to_apologise_for():
    area = parse_area([
        _relation(1, "hiking", members=(("100", 46.5, 800),)),
        {"type": "way", "id": 100, "tags": {"highway": "path"}},
    ])
    assert ferrata_gap_message(area, finding=True) is None
    assert ferrata_gap_message(area, finding=False) is None


def test_the_avoidance_caveat_never_promises_safety():
    text = ferrata_coverage_caveat().lower()
    assert "not a safety guarantee" in text
    assert "cannot be detected" in text
