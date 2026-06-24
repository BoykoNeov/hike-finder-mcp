"""Pure tests for the new shape/access filters and the Overpass parser.

No network: circular detection, proximity, tri-state acceptance, and parsing
a hand-built Overpass element list (the failure-prone part of the net layer).
"""
from hike_finder.access import (
    car_accessible,
    chairlift_access,
    endpoints_closed,
    is_circular,
    route_endpoints,
)
from hike_finder.filters import Criteria, Hike, measure_geometry
from hike_finder.overpass import parse_area

# A degree of longitude at ~50°N is ~71 km; 0.001° ~= 71 m. Handy for spacing
# test features just inside / just outside a radius.


# ----------------------------------------------------------------------------
# Circular detection
# ----------------------------------------------------------------------------


def test_endpoints_closed_simple_loop():
    # Two ways forming a square loop: a->b->c and c->...->a.
    a, b, c, d = (50.0, 14.0), (50.0, 14.01), (50.01, 14.01), (50.01, 14.0)
    ways = [[a, b, c], [c, d, a]]
    assert endpoints_closed(ways) is True


def test_endpoints_open_path():
    a, b, c = (50.0, 14.0), (50.0, 14.01), (50.0, 14.02)
    ways = [[a, b], [b, c]]
    assert endpoints_closed(ways) is False


def test_endpoints_lollipop_is_closed():
    # Loop a-b-c-a plus a dead-end approach stem c-d. This is the lollipop
    # ("okruh with a spur") that most real KČT loop relations take. The old
    # even-degree test reported it one-way (the stem tip is odd-degree); circuit
    # rank counts the loop and ignores the stem, so it is correctly circular.
    a, b, c, d = (50.0, 14.0), (50.0, 14.01), (50.01, 14.01), (50.02, 14.02)
    ways = [[a, b], [b, c], [c, a], [c, d]]
    assert endpoints_closed(ways) is True


def test_endpoints_closed_independent_of_member_order():
    # Same lollipop, members shuffled and individually reversed: the answer must
    # not depend on order or orientation (the greedy-stitch fragility we fixed).
    a, b, c, d = (50.0, 14.0), (50.0, 14.01), (50.01, 14.01), (50.02, 14.02)
    ways = [[d, c], [a, b], [c, a], [c, b]]
    assert endpoints_closed(ways) is True


def test_endpoints_figure_eight_is_closed():
    # Two loops sharing a central node — both are real cycles.
    center, p1, p2 = (50.0, 14.0), (50.0, 14.01), (50.01, 14.0)
    ways = [[center, p1], [p1, center], [center, p2], [p2, center]]
    assert endpoints_closed(ways) is True


def test_endpoints_single_ring_way_is_closed():
    # A single way whose own ends coincide is already a loop.
    a, b, c = (50.0, 14.0), (50.0, 14.01), (50.01, 14.01)
    assert endpoints_closed([[a, b, c, a]]) is True


def test_is_circular_roundtrip_tag_authoritative():
    # Tag overrides geometry both ways.
    open_line = [(50.0, 14.0), (50.0, 14.02)]
    open_ways = [[(50.0, 14.0), (50.0, 14.02)]]
    assert is_circular(open_ways, open_line, {"roundtrip": "yes"}) is True

    closed_ways = [[(50.0, 14.0), (50.0, 14.01)], [(50.0, 14.01), (50.0, 14.0)]]
    closed_line = [(50.0, 14.0), (50.0, 14.01), (50.0, 14.0)]
    assert is_circular(closed_ways, closed_line, {"roundtrip": "no"}) is False


def test_is_circular_geometry_fallback_start_near_end():
    # No tag; stitched line returns to ~within tolerance of start.
    line = [(50.0, 14.0), (50.01, 14.01), (50.0001, 14.0)]  # end ~11 m from start
    ways = [[(50.0, 14.0), (50.01, 14.01)], [(50.01, 14.01), (50.0001, 14.0)]]
    assert is_circular(ways, line, {}, tol_m=150.0) is True


def test_is_circular_lollipop_no_tag():
    # The reported symptom: an untagged okruh that is a loop plus an approach
    # stem. is_circular must report it circular off geometry alone now that
    # closure uses circuit rank. The stitched line ends on the stem tip, far
    # from the start, so the start-near-end fallback would NOT catch it — the
    # circuit-rank closure test is what makes this pass.
    a, b, c, d = (50.0, 14.0), (50.0, 14.01), (50.01, 14.01), (50.02, 14.02)
    ways = [[a, b], [b, c], [c, a], [c, d]]
    line = [d, c, a, b, c]  # walked: stem tip -> loop -> around
    assert is_circular(ways, line, {}) is True


def test_is_circular_point_to_point_false():
    line = [(50.0, 14.0), (50.0, 14.05)]  # ~3.5 km apart
    ways = [[(50.0, 14.0), (50.0, 14.05)]]
    assert is_circular(ways, line, {}, tol_m=150.0) is False


def test_route_endpoints_dedups_loop():
    loop = [(50.0, 14.0), (50.0, 14.01), (50.0, 14.0)]
    assert route_endpoints(loop) == [(50.0, 14.0)]
    line = [(50.0, 14.0), (50.0, 14.02)]
    assert route_endpoints(line) == [(50.0, 14.0), (50.0, 14.02)]


# ----------------------------------------------------------------------------
# Proximity (car / chairlift)
# ----------------------------------------------------------------------------


def test_car_access_just_inside_and_outside():
    endpoints = [(50.0, 14.0)]
    near = [{"coord": (50.0, 14.0015), "name": "P"}]  # ~107 m east
    far = [{"coord": (50.0, 14.006), "name": "P"}]  # ~430 m east
    assert car_accessible(endpoints, near, radius_m=300.0) is True
    assert car_accessible(endpoints, far, radius_m=300.0) is False
    assert car_accessible(endpoints, [], radius_m=300.0) is False


def test_chairlift_access_reports_kind():
    endpoints = [(50.0, 14.0)]
    lifts = [
        {"stations": [(49.9, 13.9), (50.0, 14.002)], "kind": "gondola", "name": "G"},
    ]
    ok, kind = chairlift_access(endpoints, lifts, radius_m=400.0)
    assert ok is True and kind == "gondola"


def test_chairlift_access_out_of_range():
    endpoints = [(50.0, 14.0)]
    lifts = [{"stations": [(49.9, 13.9)], "kind": "chair_lift", "name": "C"}]
    ok, kind = chairlift_access(endpoints, lifts, radius_m=400.0)
    assert ok is False and kind is None


# ----------------------------------------------------------------------------
# measure_geometry: access + start from genuine termini, not the stitched line
# ----------------------------------------------------------------------------


def test_measure_geometry_access_uses_dropped_member_terminus():
    # Branched relation: stitch keeps leg A and DROPS the disconnected leg B, whose
    # far end is the only one near parking. Testing the stitched line's two ends
    # (old behaviour) misses it; testing the route's termini sees leg B's real end.
    leg_a = [(50.0, 14.0), (50.0, 14.01)]
    far_end = (50.1, 14.2)
    leg_b = [far_end, (50.1, 14.21)]  # disconnected -> stitch cannot chain it
    route = {"id": 1, "name": "Branched", "ways": [leg_a, leg_b], "tags": {}}
    parking = [{"coord": (50.1, 14.2001), "name": "P"}]  # ~7 m from leg_b's far end
    measured = measure_geometry(route, parking, [])
    assert measured is not None
    hike, _ = measured
    assert hike.car_access is True


def test_measure_geometry_start_stays_when_head_is_a_terminus():
    # Clean linear route: the stitched head is already a genuine end, so the start
    # marker must NOT move (no churn on already-correct routes).
    a, b, c = (50.0, 14.0), (50.0, 14.01), (50.0, 14.02)
    route = {"id": 2, "name": "Linear", "ways": [[a, b], [b, c]], "tags": {}}
    hike, line = measure_geometry(route, [], [])
    assert line[0] in (a, c)
    assert hike.start == line[0]


def test_measure_geometry_start_moves_off_interior_head():
    # The first member starts at a junction the second way passes THROUGH (a
    # T-junction interior vertex), which stitch can't attach, so the stitched head
    # lands mid-route. start must move to a real degree-1 terminus.
    j, e1 = (50.0, 14.01), (50.0, 14.0)
    a, b = (49.99, 14.01), (50.02, 14.03)
    route = {"id": 3, "name": "Tee", "ways": [[j, e1], [a, j, b]], "tags": {}}
    hike, line = measure_geometry(route, [], [])
    assert line[0] == j  # stitch left an interior head (the through-way was dropped)
    assert hike.start in {e1, a, b} and hike.start != j


# ----------------------------------------------------------------------------
# Tri-state acceptance
# ----------------------------------------------------------------------------


def _hike(**kw):
    base = dict(
        osm_id=1,
        name="t",
        distance_km=5.0,
        circular=False,
        car_access=False,
        chairlift_access=False,
        start=(50.0, 14.0),
        gain_m=300.0,
        loss_m=300.0,
    )
    base.update(kw)
    return Hike(**base)


def test_criteria_none_means_dont_care():
    assert Criteria().accepts_geometry(_hike(circular=False)) is True
    assert Criteria().accepts_geometry(_hike(car_access=False)) is True


def test_criteria_circular_must_match():
    c = Criteria(circular=True)
    assert c.accepts_geometry(_hike(circular=True)) is True
    assert c.accepts_geometry(_hike(circular=False)) is False


def test_criteria_false_excludes():
    c = Criteria(car_access=False)
    assert c.accepts_geometry(_hike(car_access=False)) is True
    assert c.accepts_geometry(_hike(car_access=True)) is False


def test_criteria_gain_split_from_geometry():
    # Geometry passes even though gain is out of range; gain check is separate.
    c = Criteria(min_gain_m=500.0, car_access=True)
    h = _hike(car_access=True, gain_m=300.0)
    assert c.accepts_geometry(h) is True
    assert c.accepts_gain(h) is False


def test_criteria_unknown_gain_fails_active_bound():
    c = Criteria(min_gain_m=100.0)
    assert c.accepts_gain(_hike(gain_m=None)) is False
    # ...but with no gain bound, unknown gain is fine.
    assert Criteria().accepts_gain(_hike(gain_m=None)) is True


# ----------------------------------------------------------------------------
# Pure Overpass parsing (the risky net-layer code, tested offline)
# ----------------------------------------------------------------------------


def test_parse_area_splits_routes_parking_lifts():
    elements = [
        {
            "type": "relation",
            "id": 42,
            "tags": {"route": "hiking", "name": "Ridge Loop", "ref": "KČT 1"},
            "members": [
                {
                    "type": "way",
                    "geometry": [
                        {"lat": 50.0, "lon": 14.0},
                        {"lat": 50.0, "lon": 14.01},
                    ],
                },
                # A non-way member (e.g. a guidepost node) must be ignored.
                {"type": "node", "lat": 50.0, "lon": 14.0},
            ],
        },
        # Parking as a node.
        {"type": "node", "id": 1, "lat": 50.001, "lon": 14.001,
         "tags": {"amenity": "parking", "name": "Trailhead P"}},
        # Parking as an area (way) with `out center`.
        {"type": "way", "id": 2, "center": {"lat": 50.002, "lon": 14.002},
         "tags": {"amenity": "parking"}},
        # A ride-up aerialway way with geometry → two stations.
        {"type": "way", "id": 3,
         "geometry": [{"lat": 50.0, "lon": 14.0}, {"lat": 50.02, "lon": 14.03}],
         "tags": {"aerialway": "chair_lift", "name": "North Lift"}},
        # A drag lift must be EXCLUDED (ski-only, not ride-up family).
        {"type": "way", "id": 4,
         "geometry": [{"lat": 50.0, "lon": 14.0}, {"lat": 50.01, "lon": 14.0}],
         "tags": {"aerialway": "drag_lift"}},
    ]
    area = parse_area(elements)

    assert len(area.routes) == 1
    route = area.routes[0]
    assert route["id"] == 42 and route["name"] == "Ridge Loop"
    assert len(route["ways"]) == 1  # node member dropped
    assert route["ways"][0][0] == (50.0, 14.0)

    assert len(area.parking) == 2
    coords = {p["coord"] for p in area.parking}
    assert (50.001, 14.001) in coords  # node
    assert (50.002, 14.002) in coords  # area via center

    assert len(area.lifts) == 1  # drag_lift excluded
    lift = area.lifts[0]
    assert lift["kind"] == "chair_lift"
    assert lift["stations"] == [(50.0, 14.0), (50.02, 14.03)]


def test_parse_area_route_without_ways_skipped():
    elements = [
        {"type": "relation", "id": 7, "tags": {"route": "hiking"}, "members": []},
    ]
    assert parse_area(elements).routes == []
