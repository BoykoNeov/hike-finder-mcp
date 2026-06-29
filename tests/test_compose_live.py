"""Loop composition on REAL OSM data — pins the live findings offline.

Uses the same one-Overpass-round-trip fixture as the closure/coupling live tests
(tests/fixtures/spindl_area.json — Špindlerův Mlýn bbox, 15 routes). It locks:

  * the go/no-go connectivity signal — every relation welds into ONE graph spanning
    all 15 trail refs, and coincident-edge dedup keeps the degree distribution sane
    (no degree-4 sliver explosion);
  * the one in-bbox loop the clipped graph composes (3.38 km from five marked trails);
  * the full ``search.compose_loops`` orchestration end-to-end (synthetic route ->
    find_hikes -> composed Hike), with the closed-loop gain≈loss cross-check, using a
    deterministic stub elevation provider so it stays offline.
"""
import json
from collections import Counter
from pathlib import Path

from hike_finder import config as _config
from hike_finder import search as S
from hike_finder.compose import build_trail_graph, clip_routes_to_bbox, find_loops
from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Criteria
from hike_finder.format import hike_to_dict
from hike_finder.geometry import resample_by_distance
from hike_finder.overpass import parse_area

FIXTURE = Path(__file__).parent / "fixtures" / "spindl_area.json"
BBOX = (50.72, 15.58, 50.74, 15.62)  # s, w, n, e
KNOWN_LOOP_REFS = {
    "0402", "1801", "Medvědí okruh", "[Z] Špindlerův mlýn - okruh", "Špindlmanova mise",
}


def _area():
    return parse_area(json.loads(FIXTURE.read_text(encoding="utf-8"))["elements"])


def test_go_signal_one_component_spanning_every_relation():
    # The make-or-break: member ways from DIFFERENT relations share exact OSM nodes,
    # so the whole area is ONE connected trail graph — otherwise nothing composes.
    g = build_trail_graph(_area().routes)
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for s in g.segments:
        parent[find(s.a)] = find(s.b)
    refs: set = set()
    comps = set()
    for s in g.segments:
        comps.add(find(s.a))
        refs.update(s.refs)
    assert len(comps) == 1            # single connected component
    assert len(refs) == 15           # spanning all 15 trail refs


def test_coincident_dedup_keeps_degree_distribution_sane():
    # Without coincident-edge dedup every shared interior node would inflate to degree
    # 4 and spawn slivers (raw build had >400 degree-4 nodes / 1042 segments). After
    # dedup the graph is a realistic trail network: dominated by dead-ends + T-junctions.
    g = build_trail_graph(_area().routes)
    assert len(g.segments) == 67
    deg = Counter(g.degree(n) for n in g.adj)
    assert deg[3] > deg[4]           # T-junctions dominate X-junctions (was the reverse)


def test_clipped_graph_composes_the_known_in_bbox_loop():
    g = build_trail_graph(clip_routes_to_bbox(_area().routes, BBOX))
    res = find_loops(g, min_m=2000, max_m=14000)
    assert res.capped is False
    assert len(res.loops) == 1
    loop = res.loops[0]
    assert set(loop.refs) == KNOWN_LOOP_REFS
    assert 3.3 <= loop.length_m / 1000 <= 3.5         # ~3.38 km
    assert loop.coords[0] == loop.coords[-1]          # genuinely closed


class _RampProvider(ElevationProvider):
    """Deterministic offline elevation: height rises with latitude, so a closed loop
    reads gain ≈ loss (the pipeline cross-check) without any network."""

    def lookup(self, points):
        return [(lat - 50.0) * 5000.0 for lat, _ in points]


def test_compose_loops_pipeline_offline(monkeypatch):
    area = _area()
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: area)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _RampProvider())
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)

    hikes = S.compose_loops(BBOX, Criteria(min_distance_km=2, max_distance_km=14))
    assert len(hikes) == 1
    h = hikes[0]
    assert h.composed is True
    assert set(h.composed_of) == KNOWN_LOOP_REFS
    assert h.circular is True
    assert h.gain_m is not None and abs(h.gain_m - h.loss_m) <= 5   # closed loop
    assert hike_to_dict(h)["osm_id"] is None                       # no fake relation id
    # Access is computed along the loop line (real parking/lift in the fixture).
    assert h.car_access is True and h.chairlift_access is True
    # The composed loop carries its synthesised ring as geometry, so it exports too.
    from hike_finder.export import hikes_to_gpx

    assert h.ways and len(h.ways[0]) >= 4          # a closed ring (>= 4 vertices)
    assert "<trk>" in hikes_to_gpx([h])            # and serialises as a GPX track


def test_compose_loops_car_access_anchors_start_at_parking(monkeypatch):
    # Access-anchored loops on real data: requiring car access starts the loop at the
    # trailhead you drive to (a real parking lot), not the loop's arbitrary head — while
    # leaving the loop's geometry (and thus its gain/loss) byte-identical.
    from hike_finder import config as _config
    from hike_finder.geometry import haversine_m

    area = _area()
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: area)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _RampProvider())
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)

    plain = S.compose_loops(BBOX, Criteria(min_distance_km=2, max_distance_km=14))
    anchored = S.compose_loops(
        BBOX, Criteria(min_distance_km=2, max_distance_km=14, car_access=True)
    )
    assert len(plain) == 1 and len(anchored) == 1
    p, a = plain[0], anchored[0]

    # Same loop, same provenance, same elevation — anchoring only moves the start marker
    # (coords are not rotated, so the gain/loss seam is unchanged).
    assert set(a.composed_of) == set(p.composed_of)
    assert a.gain_m == p.gain_m and a.loss_m == p.loss_m

    # The anchored start lies on the loop within the car-access radius of a real parking
    # lot, and it actually moved off the unanchored geometric head.
    radius = _config.load().car_radius_m
    assert min(haversine_m(a.start, pk["coord"]) for pk in area.parking) <= radius
    assert a.start != p.start


# A bbox large enough to contain the whole fixture's geometry, so compose's internal
# clip is a no-op and the full multi-loop trail graph composes — the realistic scenario
# where several loops overlap and segment-level sharing actually pays off.
_WIDE_BBOX = (50.0, 15.0, 51.0, 16.0)


class _CountingProvider(ElevationProvider):
    """Records every point passed to lookup AND every call, so a test can assert both how
    many distinct elevation points the run requested and how many provider calls it made
    (the call count maps to batched API requests — the metric the throttle/quota meter)."""

    def __init__(self):
        self.seen = []
        self.calls = []

    def lookup(self, points):
        self.calls.append(len(points))
        self.seen.extend(points)
        return [(lat - 50.0) * 5000.0 for lat, _ in points]


def test_compose_looks_up_each_shared_segment_once_not_per_loop(monkeypatch):
    # The headline of segment-level shared sampling: with several overlapping loops,
    # the run looks up each DISTINCT trail segment's points exactly once — not once per
    # loop that traverses it. Cache OFF, so the provider sees exactly what compose sends
    # (proving the dedup is INTRINSIC, not a side effect of the SQLite point cache).
    area = _area()
    counter = _CountingProvider()
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: area)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: counter)
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)

    cfg = _config.load()
    hikes = S.compose_loops(_WIDE_BBOX, Criteria())  # default 3-15 km band, max_loops=15
    assert len(hikes) > 1                            # genuinely a multi-loop scenario

    # Reconstruct the exact loop set compose searched (deterministic), then compute both
    # the segment-level point count (what we DO look up) and the whole-loop point count
    # (what looking each loop up separately WOULD have cost).
    graph = build_trail_graph(clip_routes_to_bbox(area.routes, _WIDE_BBOX))
    res = find_loops(
        graph, min_m=cfg.compose_min_km * 1000, max_m=cfg.compose_max_km * 1000,
        max_segments=cfg.compose_max_segments, max_loops=cfg.compose_max_loops,
        overlap_frac=cfg.compose_overlap_frac, min_compactness=cfg.compose_min_compactness,
    )
    used = set().union(*(L.seg_ids for L in res.loops))
    distinct_pts = sum(
        len(resample_by_distance(graph.segments[i].coords, cfg.sample_interval_m)) for i in used
    )
    whole_loop_pts = sum(
        len(resample_by_distance(L.coords, cfg.sample_interval_m)) for L in res.loops
    )

    # Exactly the distinct-segment points were requested — no segment looked up twice,
    # no whole-loop re-sampling — and that is a real reduction over the per-loop cost.
    assert len(counter.seen) == distinct_pts
    assert distinct_pts < whole_loop_pts
    # And in ONE combined provider call, not one per loop or per segment — so the real
    # (100-point-batched) API request count is ~ceil(distinct_pts/100), the metric the
    # throttle and daily quota actually meter, not the raw point count.
    assert len(counter.calls) == 1
    # Closed series preserved end-to-end: every composed loop still reads gain ≈ loss
    # (within the usual hysteresis-threshold-scale up-vs-down asymmetry, which grows with
    # loop size — so a relative tolerance, not a fixed 5 m that only fits tiny loops).
    for h in hikes:
        assert h.gain_m is not None
        assert abs(h.gain_m - h.loss_m) <= 0.2 * max(h.gain_m, h.loss_m) + 5
