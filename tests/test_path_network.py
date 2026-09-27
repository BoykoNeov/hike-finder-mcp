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

from hike_finder import search as S
from hike_finder.compose import (
    build_trail_graph,
    find_loops,
    marked_length_m,
    paths_as_routes,
    snap_points,
)
from hike_finder.config import Config
from hike_finder.filters import Criteria
from hike_finder.format import format_hike
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


# ------------------------------------------------------------------------- the graph
#
# A 1 km square, west half on a named relation, east half on bare paths. Coordinates
# are ~0.005° apart (≈ 350–550 m at 50° N), far above the 1 m weld.

SW, NW, NE, SE = (50.000, 15.000), (50.005, 15.000), (50.005, 15.007), (50.000, 15.007)
MID_N, MID_S = (50.005, 15.0035), (50.000, 15.0035)


def _relation(ways, rid=1, ref="red"):
    return {"id": rid, "name": ref, "ref": ref, "tags": {}, "ways": ways,
            "way_tags": [{"highway": "path"} for _ in ways]}


def _path(pid, coords, **tags):
    return {"id": pid, "coords": coords, "tags": {"highway": "path", **tags}}


def _square():
    rel = _relation([[MID_S, SW, NW, MID_N]])
    paths = [_path(10, [MID_N, NE, SE, MID_S], surface="gravel")]
    return rel, paths


def test_a_path_adds_geometry_but_no_name():
    rel, paths = _square()
    g = build_trail_graph([rel, *paths_as_routes(paths)])
    (loop,) = find_loops(g, min_m=0, max_m=1e9).loops
    assert loop.refs == ("red",)  # the path contributes no provenance label


def test_the_same_way_fetched_twice_is_one_marked_edge():
    """A relation member and the bare path copy of the same OSM way weld to the same
    node pairs: one edge, still marked, not doubled into a parallel sliver."""
    rel, _ = _square()
    dup = _path(99, [MID_S, SW, NW, MID_N])
    g = build_trail_graph([rel, *paths_as_routes([dup])])
    assert len(g.segments) == 1
    assert all(g.segments[0].step_marked)


def test_marked_length_counts_only_the_relation_half():
    rel, paths = _square()
    g = build_trail_graph([rel, *paths_as_routes(paths)])
    (loop,) = find_loops(g, min_m=0, max_m=1e9).loops
    frac = marked_length_m(g, loop) / loop.length_m
    assert 0.45 < frac < 0.55


def test_a_split_keeps_the_marking_aligned_with_the_steps():
    """`snap_points` cuts a segment mid-way; the marking must be sliced by the same rule
    as the tags, or a cut would smear "unmarked" onto a relation stretch."""
    rel, paths = _square()
    g = build_trail_graph([rel, *paths_as_routes(paths)])
    g2, _ = snap_points(g, [(50.0025, 14.9999)])  # on the relation's west side
    assert len(g2.segments) == 2  # the one ring piece, cut at the point
    for s in g2.segments:
        assert len(s.step_marked) == len(s.coords) - 1
        steps = zip(s.coords, s.coords[1:], strict=False)
        for (p, q), marked in zip(steps, s.step_marked, strict=True):
            # Ground truth by geometry: the relation is the west half (lon ≤ MID).
            assert marked == (max(p[1], q[1]) <= MID_N[1]), (p, q, marked)


# --------------------------------------------------------------- through the search


class _Flat:
    def lookup(self, points):
        return [0.0] * len(points)


def _live(monkeypatch, area):
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: area)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _Flat())
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)


BBOX = (49.99, 14.99, 50.01, 15.02)


def test_loops_come_from_paths_where_no_relation_is_mapped(monkeypatch):
    """The Kamikōchi case in miniature: zero relations, a ring of paths — a loop, and
    the area is not reported as empty."""
    ring = [SW, NW, NE, SE, SW]
    area = AreaData(routes=[], paths=[_path(1, ring[:3]), _path(2, ring[2:])])
    _live(monkeypatch, area)
    diag: dict = {}
    hikes = S.compose_loops(BBOX, Criteria(min_distance_km=1, max_distance_km=5),
                            Config(), diagnostics=diag)
    assert diag["no_routes"] is False
    (h,) = hikes
    assert h.composed and h.composed_of == () and h.marked_frac == 0.0
    assert "unmarked paths only" in format_hike(h)


def test_a_mixed_loop_says_how_much_is_waymarked(monkeypatch):
    rel, paths = _square()
    _live(monkeypatch, AreaData(routes=[rel], paths=paths))
    (h,) = S.compose_loops(BBOX, Criteria(min_distance_km=1, max_distance_km=5), Config())
    assert h.composed_of == ("red",)
    assert 0.45 < h.marked_frac < 0.55
    assert "% on waymarked trails" in format_hike(h)


def test_a_relation_only_loop_reads_exactly_as_before(monkeypatch):
    """No paths at all (a saved area, or data from before paths): fully marked, and the
    rendered line carries no waymarking clause — byte-identical to the old output."""
    rel = _relation([[MID_S, SW, NW, MID_N], [MID_N, NE, SE, MID_S]])
    _live(monkeypatch, AreaData(routes=[rel]))
    (h,) = S.compose_loops(BBOX, Criteria(min_distance_km=1, max_distance_km=5), Config())
    assert h.marked_frac == 1.0
    assert "waymarked" not in format_hike(h) and "unmarked" not in format_hike(h)


def test_point_to_point_routes_run_on_paths_too(monkeypatch):
    """Every synthesising mode builds its graph in `search._network`, so the paths reach
    `--from/--to` exactly as they reach loops."""
    area = AreaData(routes=[], paths=[_path(1, [SW, NW, NE])])
    _live(monkeypatch, area)
    hikes = S.routes_between(SW, NE, Criteria(), Config())
    assert hikes and hikes[0].marked_frac == 0.0


# ------------------------------------------------------------ the plain area search


def test_an_area_mapped_only_as_paths_is_no_longer_empty(monkeypatch):
    """The headline: zero relations, a ring of paths — the plain area search (no
    compose flag) returns a loop, where it used to say "nothing mapped here"."""
    ring = [SW, NW, NE, SE, SW]
    _live(monkeypatch, AreaData(routes=[], paths=[_path(1, ring[:3]), _path(2, ring[2:])]))
    diag: dict = {}
    hikes = S.search_hikes(BBOX, Criteria(min_distance_km=1, max_distance_km=5), Config(),
                           diagnostics=diag)
    assert diag["no_routes"] is False
    assert [h.composed for h in hikes] == [True]


def _linear_relation_plus_paths():
    """A linear named trail (west side) and paths closing it into a ring."""
    rel = _relation([[MID_S, SW, NW, MID_N]])
    return AreaData(routes=[rel], paths=[_path(10, [MID_N, NE, SE, MID_S])])


def test_named_routes_come_first_then_loops(monkeypatch):
    _live(monkeypatch, _linear_relation_plus_paths())
    hikes = S.search_hikes(BBOX, Criteria(min_distance_km=0.5), Config())
    assert [h.composed for h in hikes] == [False, True]
    assert hikes[0].name == "red" and hikes[1].composed_of == ("red",)


def test_loops_can_be_switched_off(monkeypatch):
    _live(monkeypatch, _linear_relation_plus_paths())
    hikes = S.search_hikes(BBOX, Criteria(min_distance_km=0.5), Config(area_loops=False))
    assert [h.composed for h in hikes] == [False]


def test_a_linear_only_search_spends_nothing_on_loops(monkeypatch):
    """`circular=False` rejects every loop; composing them first would only spend
    elevation lookups on results that cannot be shown."""
    _live(monkeypatch, _linear_relation_plus_paths())
    monkeypatch.setattr(S, "_compose_from_graph", _boom)
    hikes = S.search_hikes(BBOX, Criteria(circular=False, min_distance_km=0.5), Config())
    assert [h.composed for h in hikes] == [False]


def _boom(*a, **k):
    raise AssertionError("composed loops for a search that cannot show one")


def test_a_relation_mapped_as_a_ring_is_not_listed_twice(monkeypatch):
    """A closed relation is listed by name; composing its own ring again would show the
    same walk twice. A loop that LEAVES it (onto a path shortcut) is a different walk."""
    ring = _relation([[SW, NW, NE, SE, SW]], ref="ring")
    _live(monkeypatch, AreaData(routes=[ring], paths=[]))
    hikes = S.search_hikes(BBOX, Criteria(min_distance_km=0.5), Config())
    assert [(h.composed, h.name) for h in hikes] == [(False, "ring")]

    shortcut = _path(20, [NW, SE])  # a diagonal: cuts the ring into two triangles
    _live(monkeypatch, AreaData(routes=[ring], paths=[shortcut]))
    hikes = S.search_hikes(BBOX, Criteria(min_distance_km=0.5), Config())
    assert hikes[0].name == "ring" and not hikes[0].composed
    assert any(h.composed and h.marked_frac < 0.99 for h in hikes)


def test_near_misses_never_rank_above_a_matching_loop(monkeypatch):
    """The named trail (~1 km) misses a 1.5 km minimum; the loop it is part of (~2 km)
    matches. The two passes each ran their own near-miss rule: under "auto" (the default)
    the named pass saw zero matches and added its near-miss — which then sat ABOVE the
    real match. One rule over the combined list: matches first, near-misses after, and
    under "auto" none at all once anything matched."""
    _live(monkeypatch, _linear_relation_plus_paths())
    crit = Criteria(min_distance_km=1.5)

    auto = S.search_hikes(BBOX, crit, Config(), near_miss="auto")
    assert [(h.composed, h.near_miss) for h in auto] == [(True, False)]

    always = S.search_hikes(BBOX, crit, Config(), near_miss=True)
    assert [(h.composed, h.near_miss) for h in always] == [(True, False), (False, True)]
