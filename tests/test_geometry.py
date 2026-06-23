import math

from hike_finder.geometry import (
    haversine_m,
    polyline_length_m,
    resample_by_distance,
    route_cycle_count,
    stitch_ways,
    total_way_length_m,
)


def test_haversine_known_distance():
    # Prague (50.0875, 14.4214) -> Brno (49.1951, 16.6068) ~= 184 km
    d = haversine_m((50.0875, 14.4214), (49.1951, 16.6068))
    assert 180_000 < d < 188_000


def test_haversine_zero():
    assert haversine_m((50.0, 14.0), (50.0, 14.0)) == 0.0


def test_polyline_length_additive():
    pts = [(50.0, 14.0), (50.0, 14.01), (50.0, 14.02)]
    total = polyline_length_m(pts)
    leg = haversine_m((50.0, 14.0), (50.0, 14.01))
    assert math.isclose(total, 2 * leg, rel_tol=1e-6)


def test_resample_even_spacing():
    # ~1.1 km west-east leg, resample at 100 m
    pts = [(50.0, 14.0), (50.0, 14.0157)]
    out = resample_by_distance(pts, interval_m=100.0)
    gaps = [haversine_m(out[i], out[i + 1]) for i in range(len(out) - 1)]
    # interior gaps should be ~100 m (last one may be shorter)
    for g in gaps[:-1]:
        assert 95 < g < 105
    assert out[0] == pts[0] and out[-1] == pts[-1]


def test_resample_many_fine_vertices():
    # Regression: real OSM tracks have vertices every few metres. The old carry
    # logic accumulated without ever emitting, collapsing this 1 km line to 2
    # points. A 1 km line resampled at 25 m must give ~40 evenly spaced points.
    line = [(50.0, 14.0 + i * 0.0001) for i in range(141)]  # ~7 m spacing, ~1 km
    total = polyline_length_m(line)
    out = resample_by_distance(line, interval_m=25.0)
    assert abs(len(out) - round(total / 25.0)) <= 2
    gaps = [haversine_m(out[i], out[i + 1]) for i in range(len(out) - 1)]
    for g in gaps[:-1]:
        assert 20 < g < 30  # interior gaps ~25 m
    assert out[0] == line[0] and out[-1] == line[-1]


def test_resample_segments_shorter_than_interval():
    # Every segment is far below the interval; samples must still land ~interval
    # apart, not be dropped.
    line = [(50.0, 14.0 + i * 0.00002) for i in range(50)]  # ~1.4 m spacing
    out = resample_by_distance(line, interval_m=25.0)
    gaps = [haversine_m(out[i], out[i + 1]) for i in range(len(out) - 1)]
    for g in gaps[:-1]:
        assert 20 < g < 30


def test_route_cycle_count_topologies():
    # Circuit rank E - V + C over the FULL vertex graph. >0 means the ways
    # enclose a loop. Nodes are distinct vertices welded by coordinate.
    a, b, c, d = (50.0, 14.0), (50.0, 14.01), (50.01, 14.01), (50.01, 14.0)
    # Clean loop: V4 E4 C1 -> 1.
    assert route_cycle_count([[a, b, c], [c, d, a]]) == 1
    # Open path: V3 E2 C1 -> 0.
    assert route_cycle_count([[a, b], [b, c]]) == 0
    # Lollipop (loop + stem): V4 E4 C1 -> 1.
    spur = (50.02, 14.02)
    assert route_cycle_count([[a, b], [b, c], [c, a], [c, spur]]) == 1
    # Figure-8: two loops on a shared node, two independent cycles -> 2.
    assert route_cycle_count([[a, b], [b, a], [a, c], [c, a]]) == 2
    # Empty / sub-two-point members contribute nothing.
    assert route_cycle_count([]) == 0
    assert route_cycle_count([[a]]) == 0


def test_route_cycle_count_closes_through_t_junction():
    # A way passes THROUGH j as an interior vertex; a second way runs from b back
    # to that same node j. They share the exact node j, closing a loop. An
    # endpoint-only graph (j is not an endpoint of the through way) would miss it
    # and report 0 — capturing the T-junction is the point of the vertex graph.
    a, j, b = (50.0, 14.0), (50.0, 14.01), (50.0, 14.02)
    m = (50.005, 14.015)
    through = [a, j, b]
    back = [b, m, j]  # b -> m -> j, ending on the shared interior node j
    assert route_cycle_count([through, back]) == 1


def test_route_cycle_count_ignores_unshared_near_endpoints():
    # Two ways whose ends are ~5.5 m apart but are NOT the same node do not form
    # a loop: a gap is not a closure. (The previous endpoint-clustering version
    # welded anything within 30 m and so invented cycles on dense real relations,
    # mislabelling linear KČT routes as circular.) A genuinely near-closed loop
    # is handled by is_circular's start-near-end line fallback, not here.
    a, b = (50.0, 14.0), (50.01, 14.01)
    a2 = (50.00005, 14.0)  # ~5.5 m from a, a distinct node (well over weld_m)
    assert route_cycle_count([[a, b], [b, a2]]) == 0


def test_stitch_orders_and_flips():
    # Two ways given tail-first / reversed; should chain into one line.
    way_a = [(50.0, 14.0), (50.0, 14.01)]
    way_b = [(50.0, 14.02), (50.0, 14.01)]  # reversed, shares 14.01 endpoint
    chain = stitch_ways([way_a, way_b])
    assert chain[0] == (50.0, 14.0)
    assert chain[-1] == (50.0, 14.02)


def test_total_way_length_counts_members_stitch_drops():
    # Regression: stitch_ways drops members it can't chain to the growing line's
    # two ends, so its length under-counts. Two disconnected legs -> the stitch
    # keeps only the first; the member-sum keeps both.
    leg_a = [(50.0, 14.0), (50.0, 14.01)]
    leg_b = [(50.1, 14.2), (50.1, 14.21)]  # far away, cannot chain to leg_a
    stitched = polyline_length_m(stitch_ways([leg_a, leg_b]))
    summed = total_way_length_m([leg_a, leg_b])
    # the stitch saw only leg_a; the sum recovers the dropped leg_b exactly.
    assert math.isclose(stitched, polyline_length_m(leg_a))
    assert math.isclose(summed, stitched + polyline_length_m(leg_b))


def test_total_way_length_matches_stitch_for_clean_linear_route():
    # Invariant: a cleanly connected linear route stitches with nothing dropped,
    # so the honest member-sum equals the stitched-line length. Pins that the
    # common case is unperturbed by switching distance to the member-sum.
    way_a = [(50.0, 14.0), (50.0, 14.01)]
    way_b = [(50.0, 14.01), (50.0, 14.02)]  # shares the 14.01 node, chains clean
    stitched = polyline_length_m(stitch_ways([way_a, way_b]))
    summed = total_way_length_m([way_a, way_b])
    assert math.isclose(summed, stitched, rel_tol=1e-9)


def test_total_way_length_is_order_independent():
    way_a = [(50.0, 14.0), (50.0, 14.01)]
    way_b = [(50.0, 14.01), (50.0, 14.02)]
    way_c = [(50.05, 14.0), (50.06, 14.0)]  # disconnected third leg
    assert math.isclose(
        total_way_length_m([way_a, way_b, way_c]),
        total_way_length_m([way_c, way_b, way_a]),
    )
