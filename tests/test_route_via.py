"""route_via — linking several picked points into one route, open or circular.

The orchestration (fetch → build graph → snap all → chain dijkstra → assemble →
measure) is exercised on hand-built coordinate graphs with the network stubbed out,
so the linking, the non-retracing loop closure, the forced out-and-back detection,
and the off-network guard are pinned offline. Live behaviour is covered separately.

Coordinates are spaced far above the 1 m weld tolerance, so distinct vertices never fuse.
"""
import logging

import pytest

from hike_finder import search as S
from hike_finder.filters import Criteria
from hike_finder.overpass import AreaData


class _RisingProvider:
    """Deterministic elevation: rises 100 000 m per degree north — no network."""

    def lookup(self, pts):
        return [(lat - 50.0) * 100000.0 for (lat, _lon) in pts]


def _route(ref, ways, rid):
    return {"id": rid, "name": ref, "ref": ref, "osmc_color": None, "tags": {}, "ways": ways}


@pytest.fixture
def stub_network(monkeypatch):
    """Return a runner that executes route_via fully offline against `routes`."""

    def run(points, routes, *, loop=False, criteria=None):
        monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: AreaData(routes=routes, parking=[], lifts=[]))
        monkeypatch.setattr(S, "_provider", lambda *a, **k: _RisingProvider())
        monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)
        return S.route_via(points, criteria or Criteria(), loop=loop)

    return run


# Junctions A and C joined by two parallel trails P (straight, shorter) and Q (bows
# north, longer); a stub at each end keeps A and C degree-3 junctions so P and Q stay
# distinct segments instead of contracting into one ring.
A = (50.00, 15.00)
C = (50.00, 15.03)
P_MID = (50.00, 15.015)   # straight middle -> P is the shortest A->C route
Q_MID = (50.008, 15.015)  # bows north -> Q is strictly longer (but inside the derived bbox)
_LENS = [
    _route("P", [[A, P_MID, C]], 1),
    _route("Q", [[A, Q_MID, C]], 2),
    _route("stubA", [[A, (50.00, 14.99)]], 3),
    _route("stubC", [[C, (50.00, 15.04)]], 4),
]


def test_open_route_links_points_in_order(stub_network):
    # An open route A -> C takes the single shortest trail (P) and does not double back.
    hikes = stub_network([A, C], _LENS, loop=False)
    assert len(hikes) == 1
    h = hikes[0]
    assert h.composed_of == ("P",)              # only the shortest trail, no return leg
    assert h.start == A                          # rendered start is the first point
    assert h.loss_m == 0                          # P is flat -> no descent, and no return leg


def test_circular_loop_is_non_repeating(stub_network, caplog):
    # A circular route A -> C -> A goes out on P and back on Q: two DISTINCT trails, a
    # genuine loop with nothing retraced.
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network([A, C], _LENS, loop=True)
    assert len(hikes) == 1
    h = hikes[0]
    assert h.composed_of == ("P", "Q")           # out on one trail, back on the other
    g, ls = h.gain_m or 0, h.loss_m or 0
    assert abs(g - ls) <= max(2.0, 0.03 * g)     # closed loop -> gain ~= loss
    assert "0% retraced" in caplog.text          # nothing walked twice


# Linear network A - B - C with a stub keeping A a junction: no disjoint return exists,
# so a circular route through A and C is forced into an out-and-back.
B = (50.01, 15.00)
_LINE = [
    _route("AB", [[A, B]], 1),
    _route("BC", [[B, C]], 2),
    _route("stubA", [[A, (50.00, 14.99)]], 3),
]


def test_circular_loop_falls_back_to_out_and_back(stub_network, caplog):
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network([A, C], _LINE, loop=True)
    assert len(hikes) == 1
    h = hikes[0]
    # Every segment is walked twice, so gain ~= loss and the retrace report is 100%.
    g, ls = h.gain_m or 0, h.loss_m or 0
    assert abs(g - ls) <= max(2.0, 0.03 * g)
    assert "100% retraced" in caplog.text
    assert "out-and-back" in caplog.text         # flagged loudly


def test_point_off_network_yields_no_route(stub_network):
    # A waypoint kilometres from any trail is treated as off-network -> no route drawn.
    far = (52.00, 16.00)
    assert stub_network([A, far], _LENS, loop=False) == []


def test_leg_across_a_gap_yields_no_route(stub_network):
    # Two points on DISJOINT trail networks: the linking leg crosses a gap -> no route.
    D = (50.00, 16.00)
    disjoint = [
        _route("left", [[A, C]], 1),
        _route("right", [[D, (50.00, 16.03)]], 2),
    ]
    assert stub_network([A, D], disjoint, loop=False) == []


def test_three_waypoints_visited_in_order(stub_network):
    # Three points on one straight trail link into a single route through all of them.
    line = [_route("R", [[A, P_MID, C]], 1), _route("stub", [[A, (50.00, 14.99)]], 2)]
    hikes = stub_network([A, P_MID, C], line, loop=False)
    assert len(hikes) == 1
    assert hikes[0].start == A


# Triangle T1-T2-T3 with a direct trail on each side and a stub at each vertex (so the
# vertices stay degree-3 junctions and the three sides don't contract into one ring). All
# vertices sit well inside the derived bbox pad.
T1 = (50.000, 15.000)
T2 = (50.000, 15.020)
T3 = (50.012, 15.010)
_TRIANGLE = [
    _route("s12", [[T1, T2]], 1),
    _route("s23", [[T2, T3]], 2),
    _route("s31", [[T3, T1]], 3),
    _route("stub1", [[T1, (50.000, 14.996)]], 4),
    _route("stub2", [[T2, (50.000, 15.024)]], 5),
    _route("stub3", [[T3, (50.016, 15.010)]], 6),
]


def test_circular_loop_through_three_points_is_non_repeating(stub_network, caplog):
    # THE headline case: a circular route through THREE points. Each leg (T1->T2, T2->T3,
    # and the closing T3->T1) excludes the segments the earlier legs used, so the loop walks
    # all three distinct sides once — proving the cumulative exclusion, not just one leg's.
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network([T1, T2, T3], _TRIANGLE, loop=True)
    assert len(hikes) == 1
    h = hikes[0]
    assert h.composed_of == ("s12", "s23", "s31")   # one lap of the triangle, no stub, no repeat
    assert "0% retraced" in caplog.text              # leg3's exclusion of legs 1+2 held
    g, ls = h.gain_m or 0, h.loss_m or 0
    assert abs(g - ls) <= max(2.0, 0.03 * g)         # closed loop -> gain ~= loss


def test_open_route_detours_to_an_off_path_waypoint(stub_network):
    # A middle waypoint OFF the direct line must be reached: a main trail T1-M-C with a spur
    # from M up to W. Linking [T1, W, C] must climb the spur to W and back, so the route is
    # strictly longer than the direct T1->C and its geometry includes the spur.
    M = (50.000, 15.010)
    W = (50.006, 15.010)          # off the main line, at the tip of a spur from M
    net = [
        _route("main", [[T1, M, C]], 1),
        _route("spur", [[M, W]], 2),
        _route("stub", [[T1, (50.000, 14.996)]], 3),   # keep T1 a junction
    ]
    direct = stub_network([T1, C], net, loop=False)[0].distance_km
    hikes = stub_network([T1, W, C], net, loop=False)
    assert len(hikes) == 1
    h = hikes[0]
    assert "spur" in h.composed_of                  # the route actually reached W via the spur
    assert h.distance_km > direct + 1.0             # ~1.3 km of detour up the spur and back
