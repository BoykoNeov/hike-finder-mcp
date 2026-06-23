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
from hike_finder.filters import Criteria, Hike
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


def test_endpoints_loop_with_spur_not_closed():
    # Loop a-b-c-a plus a dead-end spur c-d. The spur tip is odd-degree.
    a, b, c, d = (50.0, 14.0), (50.0, 14.01), (50.01, 14.01), (50.02, 14.02)
    ways = [[a, b], [b, c], [c, a], [c, d]]
    assert endpoints_closed(ways) is False


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
