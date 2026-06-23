import math

from hike_finder.geometry import (
    haversine_m,
    polyline_length_m,
    resample_by_distance,
    route_cycle_count,
    stitch_ways,
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
    # Circuit rank E - V + C over the endpoint graph. >0 means the ways enclose
    # a loop. Interior vertices are not nodes; only way endpoints are.
    a, b, c, d = (50.0, 14.0), (50.0, 14.01), (50.01, 14.01), (50.01, 14.0)
    # Clean loop: V2 E2 C1 -> 1.
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


def test_route_cycle_count_snaps_near_endpoints():
    # Endpoints within snap_m but not bit-identical must still close the loop.
    a, b = (50.0, 14.0), (50.01, 14.01)
    a2 = (50.00005, 14.0)  # ~5.5 m from a, inside the 30 m snap
    assert route_cycle_count([[a, b], [b, a2]]) == 1


def test_stitch_orders_and_flips():
    # Two ways given tail-first / reversed; should chain into one line.
    way_a = [(50.0, 14.0), (50.0, 14.01)]
    way_b = [(50.0, 14.02), (50.0, 14.01)]  # reversed, shares 14.01 endpoint
    chain = stitch_ways([way_a, way_b])
    assert chain[0] == (50.0, 14.0)
    assert chain[-1] == (50.0, 14.02)
