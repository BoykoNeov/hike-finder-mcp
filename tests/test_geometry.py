import math

from hike_finder.geometry import (
    haversine_m,
    polyline_length_m,
    resample_by_distance,
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


def test_stitch_orders_and_flips():
    # Two ways given tail-first / reversed; should chain into one line.
    way_a = [(50.0, 14.0), (50.0, 14.01)]
    way_b = [(50.0, 14.02), (50.0, 14.01)]  # reversed, shares 14.01 endpoint
    chain = stitch_ways([way_a, way_b])
    assert chain[0] == (50.0, 14.0)
    assert chain[-1] == (50.0, 14.02)
