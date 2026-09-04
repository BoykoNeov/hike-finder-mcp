"""Points of interest: the registry round-trip, the grid, and the route predicate.

Pure and network-free, per the project's trust-anchor convention. The two tests that
matter most here are:

  * ``test_every_registered_kind_round_trips`` — the query and the classifier are both
    derived from ``POI_KINDS``; this pins that they agree, because a drift between them
    fails as a silently-empty result set, not an error. The single deliberate exception
    is a kind's ``exclude`` list — objects fetched and then dropped on purpose — which
    has its own pins below, because the round-trip test cannot see them.
  * ``test_index_matches_brute_force`` — the grid is an optimisation, so it must be
    *exactly* equivalent to scanning every POI. A false negative there would silently
    drop real matches.
"""
from __future__ import annotations

import json
import logging
import math
import random
from itertools import pairwise

import pytest

from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Criteria, find_hikes
from hike_finder.geometry import haversine_m
from hike_finder.overpass import AreaData, build_query, parse_area
from hike_finder.poi import (
    POI_KINDS,
    PoiHit,
    PoiIndex,
    _probe_points,
    classify,
    kind_labels,
    normalise_kinds,
    required_selectors,
    route_pois,
    selectors_by_key,
)
from hike_finder.search import search_snapshot
from hike_finder.snapshot import AreaSnapshot, snapshot_from_json, snapshot_to_json

# --------------------------------------------------------------------------- registry


def _satisfying_tags(spec, value: str) -> dict:
    """The minimal element that a kind's own clause would fetch: its primary tag plus
    whatever its ``require`` list insists on (a placeholder for a presence-only rule)."""
    tags = {spec.key: value}
    for key, values in spec.require:
        tags[key] = values[0] if values else "anything"
    return tags


def test_every_registered_kind_round_trips():
    """Everything the query fetches is classifiable *or excluded*, and vice versa.

    For each registered kind, a synthetic element carrying its accepted tag value (plus
    any tags the kind REQUIRES, since without them the query would never have fetched it
    under this kind) must classify back to that same kind, AND that value must appear in
    the selector set the Overpass query is built from. This is the anti-drift pin.

    It says nothing about ``exclude``: the synthetic element carries no deny-listed tag,
    so no exclusion can fire here. That is deliberate — the round-trip is about the query
    and the classifier agreeing on which kinds EXIST — and it is why the exclusions need
    the separate pins below.
    """
    merged = {(key, v) for key, vals in selectors_by_key().items() for v in vals}
    required = {(key, v) for key, vals, _req in required_selectors() for v in vals}
    for kind, spec in POI_KINDS.items():
        assert spec.values, f"{kind} registers no tag values"
        for value in spec.values:
            assert classify(_satisfying_tags(spec, value)) == kind
            fetched = required if spec.require else merged
            assert (spec.key, value) in fetched, f"{kind}/{value} is not fetched"


def test_the_two_selector_sets_partition_the_registry():
    """A kind is fetched by exactly ONE of the merged or the required clauses.

    The partition is what keeps the anti-drift round-trip total after ``require`` split
    the query in two. A kind in neither set is unfetchable; a kind in BOTH would have its
    primary tag fetched unfiltered by the merged clause — precisely the density the
    requirement exists to prevent, and invisible to the round-trip above, which would
    still pass.
    """
    merged = {(key, v) for key, vals in selectors_by_key().items() for v in vals}
    required = {(key, v) for key, vals, _req in required_selectors() for v in vals}
    assert not (merged & required)
    every = {(spec.key, v) for spec in POI_KINDS.values() for v in spec.values}
    assert merged | required == every
    # And a required kind really does carry a requirement to render.
    for _key, _values, require in required_selectors():
        assert require


def test_build_query_contains_every_poi_selector():
    """The Overpass query really asks for the registered objects.

    Without this, a refactor that drops the POI clauses turns every POI search into an
    empty result with no error anywhere.
    """
    q = build_query(50.7, 15.5, 50.8, 15.7)
    for key, values in selectors_by_key().items():
        assert f'nwr["{key}"~' in q
        for value in values:
            assert value in q
    # A required kind is asked for by its OWN clause, primary selector plus every
    # required filter appended — not by the merged one for its key.
    for key, values, require in required_selectors():
        clause = f'nwr["{key}"~"^({"|".join(values)})$"]'
        for rk, rvals in require:
            clause += f'["{rk}"]' if not rvals else f'["{rk}"~"^({"|".join(rvals)})$"]'
        assert clause in q, f"the {key}={values} clause is missing its requirement"
    # And it still asks for the things it always did.
    assert 'relation["route"="hiking"]' in q
    assert 'nwr["amenity"="parking"]' in q
    assert '["aerialway"~' in q


def test_classify_ignores_unregistered_tags():
    assert classify({}) is None
    assert classify({"amenity": "parking"}) is None  # access, not a destination
    assert classify({"highway": "path"}) is None
    assert classify(None) is None


def test_a_secondary_tag_can_disqualify_a_primary_match():
    """The deny-list drops what OSM has positively said is something else.

    `man_made=tower` covers transmission masts and water towers; `amenity=shelter`
    covers bus shelters. Both were being reported as walk destinations — the tower kind
    while calling itself "lookout towers".
    """
    assert classify({"man_made": "tower", "tower:type": "communication"}) is None
    assert classify({"man_made": "tower", "tower:type": "water_tower"}) is None
    assert classify({"amenity": "shelter", "shelter_type": "public_transport"}) is None
    # Co-tagged on one node — the case `shelter_type` alone misses.
    assert classify({"amenity": "shelter", "highway": "bus_stop"}) is None
    # …while the commoner mapping never builds an `amenity=shelter` element to begin with.
    assert classify({"highway": "bus_stop", "shelter": "yes"}) is None


def test_a_missing_secondary_tag_never_disqualifies():
    """"Not recorded" must not collapse into "no" — the `transit_access` rule.

    Most real lookout towers carry no `tower:type` at all, so an allow-list would drop
    them. The deny-list keeps anything OSM has not positively excluded, and this pins
    that conservative direction rather than leaving it to the implementation.
    """
    assert classify({"man_made": "tower"}) == "tower"
    assert classify({"man_made": "tower", "tower:type": "observation"}) == "tower"
    assert classify({"amenity": "shelter"}) == "shelter"
    assert classify({"amenity": "shelter", "shelter_type": "weather_shelter"}) == "shelter"


def test_an_exclusion_falls_through_to_the_remaining_kinds():
    """Disqualifying an object from one kind must not cost it another it really is.

    `classify` continues rather than returning `None`, so a communication tower that is
    also a museum classifies as a museum. Registry order puts `tower` before `museum`,
    so an early return would silently lose it.
    """
    tags = {"man_made": "tower", "tower:type": "communication", "tourism": "museum"}
    assert classify(tags) == "museum"


def test_exclusions_never_reach_the_query():
    """Adding an exclusion must NOT change the Overpass query text.

    The query text is the Overpass cache key, so a deny-list applied in the query would
    invalidate every cached area — the price `POI_KINDS` pays for a new KIND. Filtering
    in the classifier instead keeps that cost at zero.

    Pinned as: every MERGED POI clause is exactly its primary selector followed by the
    bbox, and no deny-listed key is ever APPENDED to a selector. The blunter "no clause
    anywhere carries a second bracket" this used to assert stopped being available when
    `require` landed — a required kind's clause is a second bracket by definition — so
    the pin now names the shape a deny-list would take instead.

    Note why the appended form is the thing to test, and not the bare key: `highway` is
    a deny-list key (bus shelters) AND a legitimate transit clause of its own. Asserting
    it absent from the query would fail on a statement that has nothing to do with POIs.
    """
    selectors = selectors_by_key()
    assert "tower" in selectors["man_made"]  # fetched wholesale, narrowed downstream
    assert "shelter" in selectors["amenity"]
    q = build_query(50.7, 15.5, 50.8, 15.7)
    for key, values in selectors.items():
        # `](` — the bbox follows IMMEDIATELY, so nothing was appended to this clause.
        assert f'nwr["{key}"~"^({"|".join(values)})$"](' in q
    for spec in POI_KINDS.values():
        for deny_key, _deny_values in spec.exclude:
            # `…$"]["tower:type"…` — a filter tacked onto a primary selector.
            assert f'$"]["{deny_key}"' not in q


def test_a_required_tag_must_be_present_and_a_missing_one_disqualifies():
    """`require` is the mirror of `exclude`, and reverses its "not recorded ≠ no" rule.

    An unnamed `natural=tree` is a street tree, not a destination — and there are ~4000 of
    them in a hiking box against ~70 named ones. Keeping the untagged (the conservative
    direction everywhere else in this file) would make the kind useless rather than merely
    imprecise, which is why this one field insists.
    """
    assert classify({"natural": "tree"}) is None
    assert classify({"natural": "tree", "name": "Žižkův dub"}) == "tree"
    # Presence, not truthiness: Overpass's bare `["name"]` fetches `name=`, so the
    # classifier has to accept it or the round-trip has a hole the tests can't see.
    assert classify({"natural": "tree", "name": ""}) == "tree"


def test_a_failed_requirement_falls_through_to_the_remaining_kinds():
    """Same rule as an exclusion: losing one kind must not cost an object another.

    An unnamed tree that OSM also tags as a memorial is still a memorial.
    """
    assert classify({"natural": "tree", "historic": "memorial"}) == "memorial"


def test_the_broad_primary_tag_of_a_required_kind_is_never_fetched_wholesale():
    """The density guard, stated as a property rather than as a comment.

    `natural=tree` must not appear in the merged `natural` regex: if it ever does, every
    area query starts pulling thousands of street trees into every snapshot, and nothing
    else in the suite would notice — the results would be *more* complete, just useless.
    """
    assert "tree" not in selectors_by_key()["natural"]
    q = build_query(50.7, 15.5, 50.8, 15.7)
    assert '"^(tree)$"]["name"]' in q


def test_kind_labels_covers_the_registry():
    assert {k for k, _ in kind_labels()} == set(POI_KINDS)


def test_normalise_kinds_validates_and_dedups():
    assert normalise_kinds(["Church", "ruins", "church", ""]) == ("church", "ruins")
    assert normalise_kinds(None) == ()
    with pytest.raises(ValueError) as e:
        normalise_kinds(["cathedral"])
    # The error names the offender and the valid set, so a typo is actionable.
    assert "cathedral" in str(e.value) and "church" in str(e.value)


def test_parse_area_collects_pois_and_keeps_access_separate():
    """A parking lot stays car access; a church becomes a POI; duplicates collapse."""
    elements = [
        {"type": "node", "id": 1, "lat": 50.70, "lon": 15.60,
         "tags": {"amenity": "parking"}},
        {"type": "node", "id": 2, "lat": 50.71, "lon": 15.61,
         "tags": {"amenity": "place_of_worship", "name": "Sv. Petr"}},
        {"type": "way", "id": 3, "center": {"lat": 50.72, "lon": 15.62},
         "tags": {"historic": "ruins", "name": "Hrad"}},
        # Same element emitted twice (it matched two registry clauses) -> one POI.
        {"type": "way", "id": 3, "center": {"lat": 50.72, "lon": 15.62},
         "tags": {"historic": "ruins", "name": "Hrad"}},
        {"type": "node", "id": 4, "lat": 50.73, "lon": 15.63, "tags": {"shop": "bakery"}},
    ]
    area = parse_area(elements)
    assert len(area.parking) == 1
    assert [(p["kind"], p["name"]) for p in area.pois] == [
        ("church", "Sv. Petr"),
        ("ruins", "Hrad"),
    ]


# --------------------------------------------------------------------------- the grid


def _poi(lat, lon, kind="church", name=None):
    return {"coord": (lat, lon), "kind": kind, "name": name}


def _brute(pois, point, radius_m, kinds=None):
    out = []
    for p in pois:
        if kinds is not None and p["kind"] not in kinds:
            continue
        d = haversine_m(point, p["coord"])
        if d <= radius_m:
            out.append((p["coord"], round(d, 6)))
    return sorted(out)


def _from_index(index, point, radius_m, kinds=None):
    return sorted(
        (p["coord"], round(d, 6)) for p, d in index.near(point, radius_m, kinds)
    )


def test_index_matches_brute_force():
    """The grid is EXACTLY a scan of every POI — randomised, over several latitudes.

    A cell-boundary error here would silently drop real matches, which is the one
    failure mode the worst-case-cosine cell sizing exists to prevent.
    """
    rng = random.Random(20260809)
    for base_lat in (0.0, 50.7, 68.0, -33.9):
        pois = [
            _poi(
                base_lat + rng.uniform(-0.05, 0.05),
                rng.uniform(-0.05, 0.05) + 15.6,
                kind=rng.choice(("church", "ruins", "peak")),
            )
            for _ in range(300)
        ]
        for radius in (50.0, 250.0, 900.0):
            index = PoiIndex(pois, cell_m=radius, worst_lat=abs(base_lat) + 0.05)
            for _ in range(60):
                pt = (
                    base_lat + rng.uniform(-0.06, 0.06),
                    15.6 + rng.uniform(-0.06, 0.06),
                )
                assert _from_index(index, pt, radius) == _brute(pois, pt, radius)
                assert _from_index(index, pt, radius, frozenset({"ruins"})) == _brute(
                    pois, pt, radius, {"ruins"}
                )


def test_index_query_north_of_everything_it_indexed():
    """A query at a HIGHER latitude than the index was built for must still be exact.

    Longitude cells are sized off the index's worst-case latitude; a query further
    poleward sits in narrower true-metre cells, so ``near`` widens its column span
    instead of missing a hit. This is the belt to the worst-case-cosine braces.
    """
    pois = [_poi(60.0, 15.60), _poi(60.0, 15.6045), _poi(60.0, 15.62)]
    index = PoiIndex(pois, cell_m=300.0, worst_lat=60.0)
    # Query 9 degrees north, where a degree of longitude is much shorter.
    pt = (69.0, 15.6045)
    for radius in (300.0, 1500.0):
        assert _from_index(index, pt, radius) == _brute(pois, pt, radius)
    # And a genuinely nearby query at the indexed latitude finds its neighbour.
    got = index.near((60.0, 15.6040), 300.0)
    assert (60.0, 15.6045) in [p["coord"] for p, _ in got]


def test_index_reports_present_kinds_and_size():
    index = PoiIndex([_poi(50.7, 15.6, "ruins"), _poi(50.8, 15.7, "peak")], cell_m=250.0)
    assert index.kinds == {"ruins", "peak"}
    assert len(index) == 2
    assert PoiIndex([], cell_m=250.0).kinds == set()


def test_empty_index_never_matches():
    index = PoiIndex([], cell_m=250.0)
    assert index.near((50.7, 15.6), 10_000.0) == []


# ----------------------------------------------------------------- route_pois


def test_route_pois_finds_only_requested_kinds_nearest_first():
    # A route running east along a parallel, with objects strewn near it.
    way = [(50.7000, 15.6000 + i * 0.001) for i in range(20)]
    pois = [
        _poi(50.7005, 15.6050, "church", "Kostel"),   # ~55 m off the line
        _poi(50.7000, 15.6100, "ruins", "Zřícenina"),  # right on it
        _poi(50.7300, 15.6050, "peak", "Kopec"),       # >3 km away
    ]
    index = PoiIndex(pois, cell_m=250.0)

    hits = route_pois([way], index, ("church", "ruins"), 250.0)
    assert [h.name for h in hits] == ["Zřícenina", "Kostel"]  # nearest first
    assert hits[0].kind == "ruins" and hits[0].distance_m < 5
    assert 40 < hits[1].distance_m < 80

    # The far peak is out of range even when asked for.
    assert route_pois([way], index, ("peak",), 250.0) == ()
    # No kinds requested -> no work, no claims.
    assert route_pois([way], index, (), 250.0) == ()
    # No ways -> nothing.
    assert route_pois([], index, ("ruins",), 250.0) == ()


def test_route_pois_measures_to_the_line_not_the_nearest_vertex():
    """A church beside the MIDDLE of a long straight member way is still found.

    OSM maps a dead-straight stretch with two nodes; here they are 5.5 km apart and the
    ruin sits at the midpoint, ~11 m off the line but ~2.8 km from either vertex. A
    vertex-only proximity test would report "no ruins near this hike", which is wrong.
    """
    way = [(50.0, 14.0), (50.05, 14.0)]  # two nodes, ~5.5 km apart
    target = (50.0250, 14.0015)          # ~107 m off the line, ~2.8 km from each node
    index = PoiIndex([_poi(*target, "ruins", "Zřícenina")], cell_m=250.0)
    assert min(haversine_m(v, target) for v in way) > 2500  # far from every vertex

    hits = route_pois([way], index, ("ruins",), 250.0)
    assert [h.name for h in hits] == ["Zřícenina"]
    # 0.0015 deg of longitude at 50 N, i.e. the true perpendicular offset.
    expected = haversine_m(target, (target[0], 14.0))
    assert hits[0].distance_m == pytest.approx(expected, rel=0.02)


def test_route_pois_reports_each_object_once_at_closest_approach():
    """A route hugging a viewpoint for a kilometre lists it ONCE, at its nearest point."""
    way = [(50.7000 + i * 0.0001, 15.6000) for i in range(100)]
    target = (50.7050, 15.6002)  # ~14 m from the line, near many vertices
    index = PoiIndex([_poi(*target, "viewpoint", "Vyhlídka")], cell_m=250.0)
    hits = route_pois([way], index, ("viewpoint",), 250.0)
    assert len(hits) == 1
    # Densely mapped here, so line distance and nearest-vertex distance agree.
    brute = min(haversine_m(p, target) for p in way)
    assert hits[0].distance_m == pytest.approx(brute, rel=0.02)


def test_route_pois_scans_all_member_ways_not_just_a_stitched_line():
    """A church beside a member that ``stitch_ways`` would drop is still found.

    ``measure_geometry`` passes the RAW member ways for exactly this reason; the
    predicate must not care whether the ways chain into one line.
    """
    connected = [(50.70, 15.60), (50.70, 15.61)]
    orphan = [(50.80, 15.90), (50.80, 15.91)]  # nowhere near the first way
    index = PoiIndex([_poi(50.8010, 15.9050, "church", "Kaple")], cell_m=250.0)
    hits = route_pois([connected, orphan], index, ("church",), 250.0)
    assert [h.name for h in hits] == ["Kaple"]


def test_route_pois_respects_the_radius_exactly_at_the_boundary():
    """Whatever the probing does internally, the reported set is exactly "within R"."""
    way = [(50.0, 14.0), (50.05, 14.0)]
    # ~111 m east of the line's midpoint.
    target = (50.025, 14.0 + 111.0 / (111_320.0 * math.cos(math.radians(50.025))))
    index = PoiIndex([_poi(*target, "peak", "Vrch")], cell_m=250.0)
    assert route_pois([way], index, ("peak",), 150.0) != ()   # inside
    assert route_pois([way], index, ("peak",), 80.0) == ()    # outside, not over-collected


def test_probe_points_never_leave_a_gap_wider_than_the_step():
    """The candidate stage rests on this: no point of the line is far from a probe."""
    line = [(50.0, 14.0), (50.05, 14.0), (50.05, 14.08)]
    probes = _probe_points(line, 250.0)
    gaps = [haversine_m(a, b) for a, b in pairwise(probes)]
    # Interpolation is linear in lat/lon while the gap is measured great-circle, so a
    # long edge overshoots by a hair. Irrelevant in practice: the candidate lookup uses
    # 1.5x the radius, i.e. 125 m of slack against a ~0.01 m discrepancy.
    assert max(gaps) <= 250.0 * 1.001
    # The real vertices survive, so a way shorter than one step is still probed.
    assert probes[0] == line[0] and probes[-1] == line[-1]
    assert _probe_points([(50.0, 14.0), (50.0, 14.0001)], 250.0) == [
        (50.0, 14.0), (50.0, 14.0001)
    ]


def test_poi_hit_renders_with_measured_distance():
    hit = PoiHit(kind="ruins", name="Hrad", coord=(50.7, 15.6), distance_m=123.4)
    assert hit.label == "ruin"
    assert hit.describe() == "ruin “Hrad” (123 m)"
    d = hit.to_dict()
    assert d == {
        "kind": "ruins", "label": "ruin", "name": "Hrad",
        "lat": 50.7, "lon": 15.6, "distance_m": 123.4,
    }
    # An unnamed object still renders honestly, without empty quotes.
    assert PoiHit("peak", None, (50.7, 15.6), 10.0).describe() == "peak (10 m)"


# ------------------------------------------------- through the real find_hikes engine


class _Flat(ElevationProvider):
    """Constant elevation, and it COUNTS the points it was asked for — so a test can
    show the POI filter saved the elevation pass from ever seeing the dropped routes."""

    def __init__(self):
        self.points = 0

    def lookup(self, points):
        self.points += len(points)
        return [500.0] * len(points)


def _way_route(osm_id, lat, name="r"):
    """A 1 km-ish west->east way at latitude ``lat``."""
    return {
        "id": osm_id,
        "name": name,
        "ways": [[(lat, 15.600), (lat, 15.610), (lat, 15.620)]],
        "tags": {},
    }


def _poi_area():
    """Two routes; only the northern one runs past a ruin, and only it past a church."""
    return AreaData(
        routes=[_way_route(1, 50.700, "past the ruin"), _way_route(2, 50.900, "empty")],
        pois=[
            _poi(50.7010, 15.6100, "ruins", "Zřícenina"),  # ~110 m off route 1
            _poi(50.7015, 15.6200, "church", "Kostel"),    # ~170 m off route 1
            _poi(50.9500, 15.6100, "peak", "Kopec"),       # ~5.5 km off route 2
        ],
    )


def test_find_hikes_keeps_only_routes_reaching_the_requested_kind():
    area = _poi_area()
    hikes = find_hikes(area, _Flat(), Criteria(poi_kinds=("ruins",)), poi_radius_m=250.0)
    assert [h.osm_id for h in hikes] == [1]
    assert [(p.kind, p.name) for p in hikes[0].pois] == [("ruins", "Zřícenina")]


def test_find_hikes_ors_several_poi_kinds_and_reports_all_reached():
    area = _poi_area()
    hikes = find_hikes(
        area, _Flat(), Criteria(poi_kinds=("ruins", "church")), poi_radius_m=250.0
    )
    assert [h.osm_id for h in hikes] == [1]
    # Both reached objects are listed, nearest first.
    assert [p.kind for p in hikes[0].pois] == ["ruins", "church"]


def test_find_hikes_poi_radius_is_the_lever():
    area = _poi_area()
    # Too tight for anything: the ruin is ~110 m away.
    assert find_hikes(area, _Flat(), Criteria(poi_kinds=("ruins",)), poi_radius_m=50.0) == []
    # Wide enough to reach the far peak from the northern route too.
    hikes = find_hikes(
        area, _Flat(), Criteria(poi_kinds=("peak",)), poi_radius_m=6000.0
    )
    assert [h.osm_id for h in hikes] == [2]


def test_poi_filter_runs_in_the_cheap_pass_and_saves_elevation():
    """A POI-filtered search must cost LESS elevation than the same search without it.

    The filter lands before ``add_elevation``, so the routes that reach nothing are
    dropped before anyone pays for their gain profile. Pinned because it is the
    architectural claim, not just an implementation detail.
    """
    unfiltered, filtered = _Flat(), _Flat()
    find_hikes(_poi_area(), unfiltered, Criteria())
    find_hikes(
        _poi_area(), filtered, Criteria(poi_kinds=("ruins",)), poi_radius_m=250.0
    )
    assert 0 < filtered.points < unfiltered.points


def test_no_poi_filter_means_no_poi_claims():
    """Without a filter the scan never runs — nothing is measured and nothing claimed."""
    hikes = find_hikes(_poi_area(), _Flat(), Criteria())
    assert len(hikes) == 2
    assert all(h.pois == () for h in hikes)


def test_poi_filter_against_an_area_with_no_pois_is_simply_empty():
    area = AreaData(routes=[_way_route(1, 50.700)])  # no pois at all
    assert find_hikes(area, _Flat(), Criteria(poi_kinds=("ruins",))) == []


def test_snapshot_round_trips_pois():
    """POIs survive save/load, and a pre-POI snapshot loads with none rather than failing."""
    snap = AreaSnapshot(
        bbox=(50.6, 15.5, 50.8, 15.7),
        area=_poi_area(),
        elevations={},
        sample_interval_m=25.0,
    )
    payload = snapshot_to_json(snap)
    assert len(payload["area"]["pois"]) == 3
    back = snapshot_from_json(json.loads(json.dumps(payload)))
    assert [(p["kind"], p["name"]) for p in back.area.pois] == [
        ("ruins", "Zřícenina"),
        ("church", "Kostel"),
        ("peak", "Kopec"),
    ]
    # Coordinates come back as hashable tuples, like every other geometry seam.
    assert all(isinstance(p["coord"], tuple) for p in back.area.pois)
    assert back.poi_count == 3

    # A snapshot written before this feature has no "pois" key at all.
    legacy = json.loads(json.dumps(payload))
    del legacy["area"]["pois"]
    assert snapshot_from_json(legacy).area.pois == []


def test_search_snapshot_warns_loudly_on_a_pre_poi_snapshot(caplog):
    """"No churches here" and "this file doesn't know about churches" must not look alike."""
    legacy = AreaSnapshot(
        bbox=(50.6, 15.5, 50.8, 15.7),
        area=AreaData(routes=[_way_route(1, 50.700)]),  # no POIs, as if pre-feature
        elevations={},
        sample_interval_m=25.0,
    )
    with caplog.at_level(logging.WARNING):
        assert search_snapshot(legacy, Criteria(poi_kinds=("church",))) == []
    assert "no points of interest" in caplog.text
    assert "re-download" in caplog.text


def test_index_cell_sizing_is_sane():
    """One cell always spans at least one search radius, in BOTH axes and in true metres.

    This is the invariant the 3x3 neighbourhood rests on. It is pinned directly rather
    than left to the randomised test, because the margin it needs (the projection is
    flat, the measurement is great-circle) is small enough that random sampling can miss
    a violation — as it did while ``_M_PER_DEG_LAT`` disagreed with ``haversine_m``.
    """
    radius = 250.0
    index = PoiIndex([_poi(50.7, 15.6)], cell_m=radius, worst_lat=50.75)
    # Two points one cell apart must be at least one radius apart on the ground —
    # checked at the WORST (highest) latitude the index was built for, where east-west
    # cells are narrowest.
    dlon = index._dlon
    dlat = index._dlat
    assert haversine_m((50.75, 15.6), (50.75, 15.6 + dlon)) >= radius
    assert haversine_m((50.7, 15.6), (50.7 + dlat, 15.6)) >= radius
    # And the reference cosine came from the worst-case latitude, not the mean.
    assert index._cos_ref == pytest.approx(math.cos(math.radians(50.75)))
