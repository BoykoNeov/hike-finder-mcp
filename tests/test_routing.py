"""Point-to-point routing on synthetic graphs — the pure trust-anchor tests.

These pin ``compose.snap_points`` / ``_dijkstra`` / ``k_shortest_paths`` on hand-built
coordinate graphs where the right answer is obvious, so the mid-segment snapping, the
multigraph-aware Yen search, and its determinism can't silently regress. The live
behaviour on real OSM data is covered by ``test_routing_live.py``.

Coordinates are spaced far above the 1 m weld tolerance, so distinct vertices never fuse.
"""
from hike_finder.compose import (
    _dijkstra,
    build_trail_graph,
    k_shortest_paths,
    snap_points,
)
from hike_finder.geometry import polyline_length_m

S = (50.00, 15.00)
T = (50.00, 15.03)
MID_C = (50.00, 15.015)   # straight middle -> the shortest S->T route
MID_A = (50.01, 15.015)   # bows north
MID_B = (49.99, 15.015)   # bows south (mirror of A -> equal length)


def _route(ref, ways, rid=1):
    return {"id": rid, "name": ref, "ref": ref, "osmc_color": None, "tags": {}, "ways": ways}


def _three_paths():
    # Junctions S, T joined by three DISTINCT trails (a multigraph: three parallel
    # segments between the same junction pair). C is straight (shortest); A and B bow
    # symmetrically north/south so they are equal length and strictly longer than C.
    return [
        _route("C", [[S, MID_C, T]], rid=1),
        _route("A", [[S, MID_A, T]], rid=2),
        _route("B", [[S, MID_B, T]], rid=3),
    ]


# --------------------------------------------------------------------------- snapping


def test_snap_to_endpoint_uses_the_junction_no_split():
    # A point sitting on junction S snaps to that node without inventing a temp node.
    g = build_trail_graph(_three_paths())
    n_before = len(g.segments)
    g2, snapped = snap_points(g, [S])
    node, dist = snapped[0]
    assert dist < 1.0                       # essentially on the junction
    assert g2.coords[node] == S             # snapped to S itself
    assert len(g2.segments) == n_before     # no split happened


def test_snap_midsegment_splits_the_segment():
    # One straight trail S-M-T (a single contracted segment). A point off to the side of
    # its middle must SPLIT the segment at the projected point, not jump to an endpoint.
    M = (50.00, 15.015)
    g = build_trail_graph([_route("R", [[S, M, T]])])
    assert len(g.segments) == 1
    off = (50.002, 15.015)                  # ~220 m north of the middle
    g2, snapped = snap_points(g, [off])
    node, dist = snapped[0]
    assert len(g2.segments) == 2            # split into two pieces
    assert abs(g2.coords[node][1] - 15.015) < 1e-6   # projected onto the line at lon 15.015
    assert 150.0 < dist < 300.0             # reported snap distance is the ~220 m gap
    # The two pieces still cover the original trail end to end.
    assert _dijkstra(g2, *_ends(g2, S, T)) is not None


def test_two_points_on_one_segment_split_it_into_three():
    # Both picked points land on the SAME long segment: it splits at both, so the direct
    # sub-segment between them exists (routing between the two temp nodes is that piece).
    M = (50.00, 15.015)
    g = build_trail_graph([_route("R", [[S, M, T]])])
    p1, p2 = (50.001, 15.008), (50.001, 15.022)
    g2, snapped = snap_points(g, [p1, p2])
    assert len(g2.segments) == 3            # a -> t1 -> t2 -> b
    (n1, _), (n2, _) = snapped
    seg = _dijkstra(g2, n1, n2)
    assert seg is not None and len(seg[0]) == 1   # a single direct piece between them


# --------------------------------------------------------------------------- dijkstra


def test_dijkstra_picks_the_shortest_of_parallel_trails():
    g = build_trail_graph(_three_paths())
    g2, snapped = snap_points(g, [S, T])
    path = _dijkstra(g2, snapped[0][0], snapped[1][0])
    assert path is not None
    segs, _nodes, length = path
    assert len(segs) == 1                    # one contracted segment end to end
    assert g2.segments[segs[0]].refs == ("C",)   # the straight one is shortest
    assert abs(length - polyline_length_m([S, MID_C, T])) < 1e-6


def test_dijkstra_returns_none_when_disconnected():
    # Two disjoint trails (no shared node): no path from one to the other.
    far = (52.00, 16.00)
    g = build_trail_graph([_route("R", [[S, T]]), _route("F", [[far, (52.0, 16.01)]])])
    g2, snapped = snap_points(g, [S, far])
    assert _dijkstra(g2, snapped[0][0], snapped[1][0]) is None


# --------------------------------------------------------------------------- k-shortest (Yen)


def test_k_shortest_returns_distinct_parallel_routes_shortest_first():
    # Three parallel trails -> the three routes, C (shortest) then A and B. This is the
    # multigraph trap: Yen must remove edges by SEGMENT id, or removing C would also drop
    # its parallel twins and it would never find A and B.
    g = build_trail_graph(_three_paths())
    g2, snapped = snap_points(g, [S, T])
    paths = k_shortest_paths(g2, snapped[0][0], snapped[1][0], k=3)
    assert len(paths) == 3
    assert paths[0].refs == ("C",)                       # shortest first
    lengths = [round(p.length_m, 3) for p in paths]
    assert lengths == sorted(lengths)                    # non-decreasing
    assert {p.refs for p in paths} == {("C",), ("A",), ("B",)}
    # Each assembled route runs from S to T (open path, not a closed loop).
    for p in paths:
        assert p.coords[0] == S and p.coords[-1] == T


def test_k_shortest_caps_at_k():
    g = build_trail_graph(_three_paths())
    g2, snapped = snap_points(g, [S, T])
    assert len(k_shortest_paths(g2, snapped[0][0], snapped[1][0], k=2)) == 2


def test_k_shortest_overlap_filter_drops_near_duplicates():
    # The overlap filter is SEGMENT-based (like find_loops' collapse): it drops a route that
    # re-uses most of an already-kept route's *segments*, i.e. "same trunk + a tiny alternate
    # tail". Graph: a long shared trunk S-X, then two short parallel legs X-T, plus a wholly
    # separate detour route S-T. The two X-T legs both re-use the trunk (~80% of their length),
    # so the strict k-shortest returns the two near-twins, while the diverse set skips the
    # second twin and takes the genuinely different detour.
    X = (50.00, 15.04)
    T2 = (50.00, 15.05)
    m1, m2 = (50.0002, 15.045), (49.9998, 15.045)   # two short parallel X->T legs
    D = (50.02, 15.025)                              # a long, edge-disjoint detour S->T
    g = build_trail_graph([
        _route("shared", [[S, X]], rid=1),
        _route("leg1", [[X, m1, T2]], rid=2),
        _route("leg2", [[X, m2, T2]], rid=3),
        _route("det", [[S, D, T2]], rid=4),
        _route("stub", [[S, (50.00, 14.99)]], rid=5),  # keeps S a degree-3 junction
    ])
    g2, snapped = snap_points(g, [S, T2])
    src, dst = snapped[0][0], snapped[1][0]
    strict = k_shortest_paths(g2, src, dst, k=2, overlap_frac=1.1)   # never collapse
    diverse = k_shortest_paths(g2, src, dst, k=2, overlap_frac=0.6)  # skip trunk-sharing twins
    # Strict: both near-twins survive (both use the "shared" trunk).
    assert len(strict) == 2 and all("shared" in p.refs for p in strict)
    # Diverse: first is a trunk route, second is the separate detour, not the twin.
    assert "shared" in diverse[0].refs
    assert diverse[1].refs == ("det",)


def test_k_shortest_respects_max_length():
    g = build_trail_graph(_three_paths())
    g2, snapped = snap_points(g, [S, T])
    short_only = polyline_length_m([S, MID_C, T]) + 1.0
    paths = k_shortest_paths(g2, snapped[0][0], snapped[1][0], k=3, max_m=short_only)
    assert len(paths) == 1 and paths[0].refs == ("C",)   # A and B exceed the cap


def test_k_shortest_is_deterministic():
    g = build_trail_graph(_three_paths())
    g2, snapped = snap_points(g, [S, T])
    src, dst = snapped[0][0], snapped[1][0]
    a = k_shortest_paths(g2, src, dst, k=3)
    b = k_shortest_paths(g2, src, dst, k=3)
    assert [(round(p.length_m, 6), p.coords, p.refs) for p in a] == \
           [(round(p.length_m, 6), p.coords, p.refs) for p in b]


def _ends(graph, p_from, p_to):
    """Snap two coords onto ``graph`` and return their node ids (test helper)."""
    _, snapped = snap_points(graph, [p_from, p_to])
    return snapped[0][0], snapped[1][0]
