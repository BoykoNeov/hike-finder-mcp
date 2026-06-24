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

from hike_finder import search as S
from hike_finder.compose import build_trail_graph, clip_routes_to_bbox, find_loops
from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Criteria
from hike_finder.format import hike_to_dict
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
