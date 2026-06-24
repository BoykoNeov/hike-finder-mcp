"""Loop composition on synthetic graphs — the pure trust-anchor tests.

These pin ``compose.build_trail_graph`` / ``find_loops`` / ``clip_routes_to_bbox`` on
hand-built coordinate graphs where the right answer is obvious, so the contraction,
the coincident-edge dedup, and the bounded cycle search can't silently regress. The
live behaviour on real OSM data is covered by ``test_compose_live.py``.

Coordinates are spaced ~0.001-0.01° apart (≈ 100 m – 1 km at 50° N), far above the
1 m weld tolerance, so distinct vertices never fuse.
"""
from hike_finder.compose import (
    build_trail_graph,
    clip_routes_to_bbox,
    find_loops,
)
from hike_finder.geometry import polyline_length_m


def _route(ref, ways, rid=1):
    return {"id": rid, "name": ref, "ref": ref, "osmc_color": None, "tags": {}, "ways": ways}


def _loops(routes, **kw):
    kw.setdefault("min_m", 0.0)
    kw.setdefault("max_m", 1e9)
    return find_loops(build_trail_graph(routes), **kw)


# --------------------------------------------------------------------------- graph build


def test_single_closed_way_is_one_self_loop():
    # A square mapped as one closed way: every vertex degree 2, no junction -> one
    # already-closed loop (the all-degree-2 component case).
    ring = [(50.00, 15.00), (50.00, 15.01), (50.01, 15.01), (50.01, 15.00), (50.00, 15.00)]
    g = build_trail_graph([_route("R", [ring])])
    assert len(g.segments) == 1
    assert g.segments[0].a == g.segments[0].b  # a self-loop: no junction to split it
    res = _loops([_route("R", [ring])])
    assert len(res.loops) == 1
    assert res.loops[0].refs == ("R",)


def test_t_junction_is_a_degree_3_node():
    # A straight trail A-B-C with a spur M-B touching its MIDDLE vertex B: B is shared
    # and becomes a degree-3 junction (the T-junction the full vertex graph must see).
    A, B, C, M = (50.00, 15.00), (50.00, 15.01), (50.00, 15.02), (50.01, 15.01)
    g = build_trail_graph([_route("main", [[A, B, C]]), _route("spur", [[M, B]])])
    # main splits at B into A-B and B-C; the spur M-B is the third segment.
    assert len(g.segments) == 3
    # B is the lone junction (degree 3); A, C, M are degree-1 dead-ends.
    assert sorted(g.degree(n) for n in g.adj) == [1, 1, 1, 3]


def test_coincident_trails_dedup_to_one_segment_with_both_refs():
    # The same physical ring mapped by TWO relations (a way belongs to many relations):
    # must collapse to ONE segment carrying both refs, NOT two parallel sliver edges.
    ring = [(50.00, 15.00), (50.00, 15.02), (50.02, 15.02), (50.02, 15.00), (50.00, 15.00)]
    g = build_trail_graph([_route("red", [ring], rid=1), _route("blue", [ring], rid=2)])
    assert len(g.segments) == 1
    assert g.segments[0].refs == ("blue", "red")  # sorted, both relations
    res = _loops([_route("red", [ring], rid=1), _route("blue", [ring], rid=2)])
    assert len(res.loops) == 1 and res.loops[0].refs == ("blue", "red")


# --------------------------------------------------------------------------- loop search


def test_two_parallel_trails_between_two_junctions_make_one_loop():
    # Junctions P, Q (kept degree-3 by a dead-end stub each) joined by two DISTINCT
    # paths P-A-Q and P-B-Q -> exactly one composed loop (the bigon), both directions
    # collapsed by edge-set identity. Stubs are leaf-pruned away.
    P, Q = (50.00, 15.00), (50.00, 15.02)
    A, B = (50.01, 15.01), (49.99, 15.01)
    S1, S2 = (50.00, 14.99), (50.00, 15.03)
    routes = [
        _route("path1", [[P, A, Q]], rid=1),
        _route("path2", [[P, B, Q]], rid=2),
        _route("stubP", [[P, S1]], rid=3),
        _route("stubQ", [[Q, S2]], rid=4),
    ]
    res = _loops(routes)
    assert len(res.loops) == 1
    L = res.loops[0]
    assert L.segment_count == 2
    assert set(L.refs) == {"path1", "path2"}  # the stubs are not on the loop
    # Closed polyline: first point == last point (segments share exact junction coords).
    assert L.coords[0] == L.coords[-1]


def test_figure_eight_yields_two_loops():
    # Two squares sharing a single centre vertex X (degree 4). A simple cycle can't
    # pass X twice, so there are exactly two loops — one per square.
    X = (50.00, 15.00)
    sq1 = [X, (50.01, 15.00), (50.01, 15.01), (50.00, 15.01), X]
    sq2 = [X, (49.99, 15.00), (49.99, 14.99), (50.00, 14.99), X]
    res = _loops([_route("A", [sq1], rid=1), _route("B", [sq2], rid=2)])
    assert len(res.loops) == 2


def test_length_band_filters_loops():
    ring = [(50.00, 15.00), (50.00, 15.02), (50.02, 15.02), (50.02, 15.00), (50.00, 15.00)]
    perim = polyline_length_m(ring)
    g = build_trail_graph([_route("R", [ring])])
    assert len(find_loops(g, min_m=0, max_m=perim - 100).loops) == 0  # too long for band
    assert len(find_loops(g, min_m=perim + 100, max_m=1e9).loops) == 0  # too short for band
    assert len(find_loops(g, min_m=perim - 100, max_m=perim + 100).loops) == 1  # in band


def test_determinism_identical_across_runs():
    # Same input -> byte-identical loops (sorted neighbours + stable segment ids).
    X = (50.00, 15.00)
    sq1 = [X, (50.01, 15.00), (50.01, 15.01), (50.00, 15.01), X]
    sq2 = [X, (49.99, 15.00), (49.99, 14.99), (50.00, 14.99), X]
    routes = [_route("A", [sq1], rid=1), _route("B", [sq2], rid=2)]
    a = _loops(routes).loops
    b = _loops(routes).loops
    assert [(round(L.length_m, 6), L.coords, L.refs) for L in a] == \
           [(round(L.length_m, 6), L.coords, L.refs) for L in b]


def test_budget_cap_is_reported_not_silent():
    # With a graph that needs DFS expansion, a zero budget aborts the search and is
    # flagged (capped=True) rather than silently returning a truncated list.
    P, Q = (50.00, 15.00), (50.00, 15.02)
    A, B = (50.01, 15.01), (49.99, 15.01)
    S1, S2 = (50.00, 14.99), (50.00, 15.03)
    routes = [
        _route("p1", [[P, A, Q]], rid=1),
        _route("p2", [[P, B, Q]], rid=2),
        _route("sP", [[P, S1]], rid=3),
        _route("sQ", [[Q, S2]], rid=4),
    ]
    res = find_loops(build_trail_graph(routes), min_m=0, max_m=1e9, budget=0)
    assert res.capped is True


def test_near_duplicate_collapse_keeps_one():
    # Two loops that share a long common segment (>60% of the larger's length) collapse
    # to the shorter; a genuinely different loop survives. Build a "theta" graph: two
    # junctions P,Q joined by three paths, so the three pairwise loops heavily overlap.
    P, Q = (50.00, 15.00), (50.00, 15.05)
    # near-identical short paths (small detours) + one long path
    mid1, mid2 = (50.0005, 15.025), (50.0006, 15.025)
    longmid = (50.02, 15.025)
    S1, S2 = (50.00, 14.99), (50.00, 15.06)
    routes = [
        _route("a", [[P, mid1, Q]], rid=1),
        _route("b", [[P, mid2, Q]], rid=2),
        _route("c", [[P, longmid, Q]], rid=3),
        _route("sP", [[P, S1]], rid=4),
        _route("sQ", [[Q, S2]], rid=5),
    ]
    g = build_trail_graph(routes)
    # Without collapse there are 3 pairwise loops; the a+b loop is a thin sliver and
    # the a+c / b+c loops share path c. Collapse should drop the near-duplicate(s).
    full = find_loops(g, min_m=0, max_m=1e9, overlap_frac=1.1)  # 1.1 => never collapse
    collapsed = find_loops(g, min_m=0, max_m=1e9, overlap_frac=0.6)
    assert len(collapsed.loops) < len(full.loops)


def test_max_loops_caps_and_reports_distinct_count():
    # Figure-eight has two loops; max_loops=1 returns one but still reports distinct=2,
    # so the truncation is visible (the caller logs it) — never silent.
    X = (50.00, 15.00)
    sq1 = [X, (50.01, 15.00), (50.01, 15.01), (50.00, 15.01), X]
    sq2 = [X, (49.99, 15.00), (49.99, 14.99), (50.00, 14.99), X]
    g = build_trail_graph([_route("A", [sq1], rid=1), _route("B", [sq2], rid=2)])
    res = find_loops(g, min_m=0, max_m=1e9, max_loops=1)
    assert len(res.loops) == 1
    assert res.distinct == 2


def test_loops_ranked_by_compactness_round_before_thin():
    # A compact ring and a long thin ring (separate components). The round one must
    # rank first — the cap keeps loop-like loops and demotes thin near-slivers.
    square = [(50.00, 15.00), (50.00, 15.01), (50.01, 15.01), (50.01, 15.00), (50.00, 15.00)]
    thin = [(52.00, 16.00), (52.00, 16.02), (52.0005, 16.02), (52.0005, 16.00), (52.00, 16.00)]
    g = build_trail_graph([_route("sq", [square], rid=1), _route("thin", [thin], rid=2)])
    res = find_loops(g, min_m=0, max_m=1e9)
    assert len(res.loops) == 2
    assert res.loops[0].compactness > res.loops[1].compactness
    assert res.loops[0].refs == ("sq",)          # the compact one is first
    assert res.loops[0].compactness > 0.5 and res.loops[1].compactness < 0.3


# --------------------------------------------------------------------------- clipping


def test_clip_splits_way_at_bbox_boundary():
    # A trail that leaves and re-enters the bbox becomes two in-bbox runs; the
    # out-of-area vertices are dropped.
    bbox = (50.00, 15.00, 50.10, 15.10)  # s, w, n, e
    way = [
        (50.05, 15.05),  # in
        (50.05, 15.06),  # in
        (50.05, 15.20),  # OUT (east)
        (50.05, 15.08),  # in
        (50.05, 15.09),  # in
    ]
    clipped = clip_routes_to_bbox([_route("R", [way])], bbox)
    ways = clipped[0]["ways"]
    assert len(ways) == 2
    assert all(15.00 <= lon <= 15.10 for w in ways for _, lon in w)


def test_clip_keeps_route_metadata():
    bbox = (50.00, 15.00, 50.10, 15.10)
    way = [(50.05, 15.05), (50.05, 15.06)]
    clipped = clip_routes_to_bbox([_route("trail-x", [way], rid=99)], bbox)
    assert clipped[0]["ref"] == "trail-x" and clipped[0]["id"] == 99
