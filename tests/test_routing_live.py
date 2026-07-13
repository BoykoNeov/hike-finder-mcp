"""Point-based route drawing on REAL OSM data — pins the live findings offline.

Reuses the Špindlerův Mlýn fixture (tests/fixtures/spindl_area.json, 15 routes) that the
compose live tests use, and the same monkeypatch trick (stub ``_fetch_area`` + a
deterministic ramp elevation provider) to exercise the full ``search`` orchestration for
the two new features end to end, offline:

  * ``compose_loops_around`` — circular routes near a picked point (reuses the loop engine
    with the point as a compose anchor + a point-derived bbox);
  * ``routes_between`` — the k shortest distinct routes between two picked points (Yen on
    the real trail graph, with mid-segment snapping).
"""
import json
from pathlib import Path

from hike_finder import search as S
from hike_finder.compose import build_trail_graph, clip_routes_to_bbox, find_loops
from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Criteria
from hike_finder.geometry import haversine_m
from hike_finder.overpass import parse_area

FIXTURE = Path(__file__).parent / "fixtures" / "spindl_area.json"
BBOX = (50.72, 15.58, 50.74, 15.62)  # s, w, n, e
KNOWN_LOOP_REFS = {
    "0402", "1801", "Medvědí okruh", "[Z] Špindlerův mlýn - okruh", "Špindlmanova mise",
}


def _area():
    return parse_area(json.loads(FIXTURE.read_text(encoding="utf-8"))["elements"])


class _RampProvider(ElevationProvider):
    """Deterministic offline elevation: height rises with latitude, so a closed loop reads
    gain ≈ loss without any network."""

    def lookup(self, points):
        return [(lat - 50.0) * 5000.0 for lat, _ in points]


def _known_loop():
    g = build_trail_graph(clip_routes_to_bbox(_area().routes, BBOX))
    res = find_loops(g, min_m=2000, max_m=14000)
    assert len(res.loops) == 1
    return res.loops[0]


def _stub(monkeypatch, area):
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: area)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _RampProvider())
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)


# --------------------------------------------------------------------------- --around


def test_around_finds_loops_near_the_point_each_started_there(monkeypatch):
    # A point sitting ON the known loop: compose_loops_around surfaces the circular routes
    # that pass near it. The point-derived bbox (radius + max-loop/2) is wider than the tiny
    # test BBOX, so it legitimately finds SEVERAL loops through that junction — each must be
    # a genuine closed loop and each must start within the radius of the picked point.
    area = _area()
    _stub(monkeypatch, area)
    point = _known_loop().coords[0]

    hikes = S.compose_loops_around(
        point, Criteria(min_distance_km=2, max_distance_km=14), radius_m=500.0
    )
    assert len(hikes) >= 1
    for h in hikes:
        assert h.composed is True and h.composed_of
        assert h.circular is True
        assert h.gain_m is not None and abs(h.gain_m - h.loss_m) <= 0.2 * max(h.gain_m, h.loss_m) + 5
        assert haversine_m(h.start, point) <= 500.0   # started near where you pointed


def test_around_radius_gates_the_loop(monkeypatch):
    # A point far from every trail (well beyond the radius) yields no circular routes — the
    # anchor filter is real, not cosmetic.
    area = _area()
    _stub(monkeypatch, area)
    far = (50.73, 15.75)  # east of the fixture, > 1 km from any trail
    hikes = S.compose_loops_around(
        far, Criteria(min_distance_km=2, max_distance_km=14), radius_m=300.0
    )
    assert hikes == []


# --------------------------------------------------------------------------- --from / --to


def test_routes_between_two_points_on_a_loop_returns_both_arcs(monkeypatch):
    # Two points on the known loop: the routes between them are the loop's two arcs, shortest
    # first. They are genuinely distinct (edge-disjoint), non-circular, and each starts at the
    # snapped start point.
    area = _area()
    _stub(monkeypatch, area)
    loop = _known_loop()
    start = loop.coords[0]
    finish = loop.coords[len(loop.coords) // 2]

    hikes = S.routes_between(start, finish, Criteria(), k=2)
    assert 1 <= len(hikes) <= 2
    # Shortest first.
    assert [round(h.distance_km, 6) for h in hikes] == sorted(round(h.distance_km, 6) for h in hikes)
    for h in hikes:
        assert h.composed is True                     # synthesised, no single OSM id
        assert h.circular is False                    # a point-to-point route, not a loop
        assert h.osm_id is None or h.osm_id < 0
        # Route starts at the snapped start (within a small snap tolerance of the picked point).
        assert haversine_m(h.start, start) <= 50.0
        assert h.gain_m is not None
    if len(hikes) == 2:
        # The two arcs differ — the overlap filter kept a genuinely different alternative.
        assert set(hikes[0].composed_of) != set(hikes[1].composed_of)


def test_routes_between_respects_max_distance_cap(monkeypatch):
    # Capping the length to just above the shorter arc drops the longer one.
    area = _area()
    _stub(monkeypatch, area)
    loop = _known_loop()
    start = loop.coords[0]
    finish = loop.coords[len(loop.coords) // 2]

    both = S.routes_between(start, finish, Criteria(), k=2)
    if len(both) < 2:
        return  # only one arc within the corridor; nothing to cap
    shortest_km = min(h.distance_km for h in both)
    capped = S.routes_between(start, finish, Criteria(max_distance_km=shortest_km + 0.05), k=2)
    assert len(capped) == 1
    assert capped[0].distance_km <= shortest_km + 0.05


def test_around_derived_bbox_contains_the_point(monkeypatch):
    # The fixture stub ignores the bbox, so this is the ONLY thing pinning the point-derived
    # bbox math: capture the bbox handed to _fetch_area and assert it actually surrounds the
    # picked point (a lat/lon swap or sign error would slip past every other test).
    area = _area()
    seen = {}

    def _spy_fetch(bbox, *a, **k):
        seen["bbox"] = bbox
        return area

    monkeypatch.setattr(S, "_fetch_area", _spy_fetch)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _RampProvider())
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)

    point = (50.73, 15.60)
    S.compose_loops_around(point, Criteria(min_distance_km=2, max_distance_km=14), radius_m=500.0)
    s, w, n, e = seen["bbox"]
    assert s < point[0] < n and w < point[1] < e     # the point sits inside the fetched box
    # And the pad is the radius + half the max loop, not something tiny or absurd.
    assert 3000 < (n - s) * 111_320.0 < 30_000       # ~2 x (500 m + 7 km) = ~15 km tall


def test_between_derived_bbox_contains_both_points(monkeypatch):
    area = _area()
    seen = {}

    def _spy_fetch(bbox, *a, **k):
        seen["bbox"] = bbox
        return area

    monkeypatch.setattr(S, "_fetch_area", _spy_fetch)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _RampProvider())
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)

    start, finish = (50.725, 15.585), (50.735, 15.615)
    S.routes_between(start, finish, Criteria(), k=2)
    s, w, n, e = seen["bbox"]
    for pt in (start, finish):
        assert s < pt[0] < n and w < pt[1] < e       # both picked points inside the fetched box


def test_routes_between_ignores_a_stray_circular_filter(monkeypatch):
    # A point-to-point route is never circular; a leftover --circular must NOT empty the
    # result. The engine neutralises `circular` so all frontends behave the same.
    area = _area()
    _stub(monkeypatch, area)
    loop = _known_loop()
    start = loop.coords[0]
    finish = loop.coords[len(loop.coords) // 2]
    with_flag = S.routes_between(start, finish, Criteria(circular=True), k=2)
    without = S.routes_between(start, finish, Criteria(), k=2)
    assert len(with_flag) == len(without) and len(with_flag) >= 1


def test_routes_between_off_network_point_returns_empty(monkeypatch):
    # A finish point far from any trail must NOT silently route to a distant trail: the
    # max-snap guard rejects a point that snaps beyond the limit (the fixture's trails span
    # ~20 km as one component, so without the guard an empty-space point would "connect").
    area = _area()
    _stub(monkeypatch, area)
    loop = _known_loop()
    start = loop.coords[0]
    far = (50.60, 15.40)   # ~13 km from the nearest trail — well beyond the 2 km snap limit
    hikes = S.routes_between(start, far, Criteria(), k=3)
    assert hikes == []
