"""The walking network: every walkable way, not only the ways route relations collect.

Why this exists: OSM maps some ranges only as individual paths. Japan's North Alps box
measured 824 path ways and zero route relations, so every mode came back empty there.
The fix builds the network from the ways themselves and lays relations on top.

This file pins the wiring the rest leans on:

  - which ways count as walkable (and which street furniture does not);
  - that a path lands in `AreaData.paths` and NEVER in `routes`, which `find_hikes` lists;
  - that a path emitted twice (it is also a POI, or cabled) is filed once, and that the
    POI/ferrata branches keep behaving exactly as before paths were fetched;
  - the "never fetched" vs "fetched, none here" tri-state through the cache/snapshot
    serialiser, and that a saved snapshot drops paths.
"""
import json

from hike_finder.overpass import AreaData, build_query, parse_area, way_is_walkable
from hike_finder.snapshot import AreaSnapshot, snapshot_from_json, snapshot_to_json


def _geom(lat0, n=3, step=0.001):
    return [{"lat": lat0 + i * step, "lon": 15.6} for i in range(n)]


def _way(wid, tags, lat0=50.7, **extra):
    return {"type": "way", "id": wid, "tags": tags, "geometry": _geom(lat0), **extra}


# ------------------------------------------------------------------------ the query


def test_query_fetches_walkable_ways_with_geometry_and_skips_street_footways():
    q = build_query(50.7, 15.5, 50.8, 15.7)
    assert 'way["highway"~"^(path|footway|track|bridleway|steps)$"]' in q
    assert '["footway"!~"^(sidewalk|crossing)$"]' in q
    # `out tags geom` — the graph needs the geometry, surface/ferrata need the tags.
    line = q[q.index('way["highway"~"^(path'):]
    assert line.split("\n")[1].strip() == "out tags geom;"


# ------------------------------------------------------------------ the predicate


def test_walkable_highways():
    for hw in ("path", "footway", "track", "bridleway", "steps"):
        assert way_is_walkable({"highway": hw})
    for hw in ("residential", "unclassified", "service", "via_ferrata", None):
        assert not way_is_walkable({"highway": hw})
    assert not way_is_walkable(None)


def test_street_footways_and_plazas_are_not_walks():
    assert not way_is_walkable({"highway": "footway", "footway": "sidewalk"})
    assert not way_is_walkable({"highway": "footway", "footway": "crossing"})
    assert not way_is_walkable({"highway": "footway", "area": "yes"})


def test_foot_wins_over_access_in_both_directions():
    assert not way_is_walkable({"highway": "track", "access": "private"})
    assert not way_is_walkable({"highway": "track", "access": "no"})
    assert way_is_walkable({"highway": "track", "access": "private", "foot": "yes"})
    assert way_is_walkable({"highway": "track", "access": "no", "foot": "designated"})
    assert not way_is_walkable({"highway": "path", "foot": "no"})
    assert not way_is_walkable({"highway": "path", "access": "yes", "foot": "private"})
    assert way_is_walkable({"highway": "track", "access": "forestry"})


# --------------------------------------------------------------------- the parser


def test_paths_land_in_their_own_list_never_in_routes():
    area = parse_area([
        _way(1, {"highway": "path", "surface": "ground"}),
        _way(2, {"highway": "track", "access": "private"}),  # closed: dropped
        _way(3, {"highway": "residential"}),                  # not a walk: dropped
    ])
    assert area.routes == []
    assert [p["id"] for p in area.paths] == [1]
    p = area.paths[0]
    assert p["tags"] == {"highway": "path", "surface": "ground"}
    assert p["coords"][0] == (50.7, 15.6) and len(p["coords"]) == 3


def test_a_live_parse_always_records_paths_even_when_there_are_none():
    """`[]` from a live parse is an answer; `None` is reserved for "never fetched"."""
    assert parse_area([]).paths == []
    assert AreaData().paths is None


def test_a_degenerate_way_is_not_a_path():
    area = parse_area([_way(1, {"highway": "path"}) | {"geometry": _geom(50.7, n=1)}])
    assert area.paths == []


def test_a_path_that_is_also_a_poi_keeps_its_poi_at_the_center_point():
    """The path statement runs BEFORE the POI statements. If its copy reached the POI
    branch, the POI would be filed at the way's first vertex instead of `out center`."""
    tags = {"highway": "path", "tourism": "viewpoint", "name": "Look"}
    area = parse_area([
        _way(7, tags),                                                    # path copy
        {"type": "way", "id": 7, "tags": tags, "center": {"lat": 50.9, "lon": 15.9}},
    ])
    assert [p["id"] for p in area.paths] == [7]
    assert [(p["kind"], p["coord"]) for p in area.pois] == [("viewpoint", (50.9, 15.9))]


def test_a_cabled_path_is_both_a_path_and_a_ferrata_way_once_each():
    tags = {"highway": "path", "via_ferrata_scale": "2", "name": "Cable"}
    area = parse_area([_way(9, tags), _way(9, tags)])  # emitted by two statements
    assert [p["id"] for p in area.paths] == [9]
    assert [w["id"] for w in area.ferrata_ways] == [9]


def test_relation_member_ways_still_come_through_as_before():
    """A tag-only member record (`way(r); out tags;`) has no geometry and must not be
    mistaken for a path — it is only a tag join for the relation."""
    area = parse_area([
        {"type": "way", "id": 100, "tags": {"highway": "path", "surface": "rock"}},
        {"type": "relation", "id": 1, "tags": {"route": "hiking", "name": "R"},
         "members": [{"type": "way", "ref": 100, "role": "", "geometry": _geom(50.7)}]},
    ])
    assert area.paths == []
    assert area.routes[0]["way_tags"] == [{"highway": "path", "surface": "rock"}]


# ------------------------------------------------------- cache / snapshot serialiser


def _snap(area):
    return AreaSnapshot(bbox=(50.6, 15.5, 50.8, 15.7), area=area, elevations={},
                        sample_interval_m=25.0)


def _round_trip(area):
    return snapshot_from_json(json.loads(json.dumps(snapshot_to_json(_snap(area))))).area


def test_paths_round_trip_with_tuple_coords():
    area = parse_area([_way(1, {"highway": "path", "sac_scale": "hiking"})])
    back = _round_trip(area)
    assert back.paths == area.paths  # exact: JSON round-trips a float bit for bit
    assert back.paths[0]["tags"] == {"highway": "path", "sac_scale": "hiking"}
    assert isinstance(back.paths[0]["coords"][0], tuple)


def test_never_fetched_and_none_here_stay_apart_through_the_serialiser():
    assert _round_trip(AreaData()).paths is None
    assert "paths" not in snapshot_to_json(_snap(AreaData()))["area"]
    assert _round_trip(parse_area([])).paths == []
