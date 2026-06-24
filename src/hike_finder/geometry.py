"""Geometry helpers: distance, polyline assembly, and resampling.

All math here is network-free and fully unit-tested. This is the part of the
pipeline you can trust without an external service.
"""
from __future__ import annotations

import math
from typing import Iterable

# A coordinate is (lat, lon) in degrees.
Coord = tuple[float, float]

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(a: Coord, b: Coord) -> float:
    """Great-circle distance between two (lat, lon) points, in metres."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def polyline_length_m(points: list[Coord]) -> float:
    """Total length of an ordered polyline, in metres."""
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


def total_way_length_m(ways: list[list[Coord]]) -> float:
    """Total mapped length of a route's member ways, in metres.

    Sums each member way's own polyline length, independently of the others, so
    it counts ALL mapped geometry regardless of member order or whether the
    members chain into one connected line.

    Use this — not ``polyline_length_m(stitch_ways(ways))`` — for route distance.
    ``stitch_ways`` greedily chains by matching endpoints and silently *drops*
    any member it can't connect to the growing chain's two ends, so a branched
    or gap-split relation's stitched line omits whole legs and *under*-counts
    distance. Summing the members drops nothing, so it can only be correct or
    *over*-count (e.g. a relation that maps the same stretch as both a
    ``forward`` and a ``backward`` variant counts it twice) — the opposite, and
    the less misleading, failure direction. Order-independent by construction.
    """
    return sum(polyline_length_m(w) for w in ways)


def stitch_ways(ways: list[list[Coord]]) -> list[Coord]:
    """Join an OSM route relation's member ways into one ordered polyline.

    OSM relation members are not guaranteed to be ordered or consistently
    oriented. This greedily chains ways by matching endpoints, flipping a way
    when its tail (not head) is the nearest continuation. Good enough for v1;
    see HANDOFF.md "Known limitations" for the robust-ordering TODO.
    """
    ways = [w for w in ways if len(w) >= 2]
    if not ways:
        return []

    remaining = ways[:]
    chain = list(remaining.pop(0))

    def near(p: Coord, q: Coord, tol_m: float = 30.0) -> bool:
        return haversine_m(p, q) <= tol_m

    progress = True
    while remaining and progress:
        progress = False
        for i, w in enumerate(remaining):
            head, tail = chain[0], chain[-1]
            w_head, w_tail = w[0], w[-1]
            if near(tail, w_head):
                chain.extend(w[1:])
            elif near(tail, w_tail):
                chain.extend(reversed(w[:-1]))
            elif near(head, w_tail):
                chain[:0] = w[:-1]
            elif near(head, w_head):
                chain[:0] = list(reversed(w))[:-1]
            else:
                continue
            remaining.pop(i)
            progress = True
            break
    return chain


class _UnionFind:
    """Tiny disjoint-set over integer ids, with path compression."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:  # path-compress the walked chain
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb

    def num_components(self) -> int:
        return len({self.find(i) for i in range(len(self._parent))})


def _vertex_graph(
    ways: list[list[Coord]], weld_m: float = 1.0
) -> tuple[list[tuple[int, int]], list[Coord]]:
    """Build the welded vertex multigraph shared by closure and termini.

    Nodes are every distinct *vertex* (welded by coordinate) — not just way
    endpoints — so two OSM ways that connect, including at a T-junction where one
    passes through the other's interior vertex, map to the same node. Edges are
    the consecutive-vertex segments of each way, kept as a *multigraph* (parallel
    edges preserved, e.g. a route mapped both forward and backward). Returns
    ``(edges, coords)`` where ``edges`` are ``(u, v)`` node-id pairs and
    ``coords[i]`` is a representative original coordinate for node ``i``.

    This is the single construction behind both ``route_cycle_count`` (closure)
    and ``route_termini`` (open ends), so the two can never drift on ``weld_m`` or
    on what counts as the same node — the graph is the project's trust anchor.

    ``weld_m`` is a small coincidence tolerance (metres) that merges vertices
    representing the same node despite float noise; it sits well below trail
    vertex spacing (~5-15 m), so it never fuses genuinely distinct points. Grid-
    welds on latitude metres in O(V) — identical OSM nodes hash to one cell, while
    distinct vertices (metres apart) do not; no pairwise scan.
    """
    cell = weld_m / 111_320.0 if weld_m > 0 else 0.0

    def key(pt: Coord):
        if cell <= 0:
            return pt
        return (round(pt[0] / cell), round(pt[1] / cell))

    node_id: dict = {}
    coords: list[Coord] = []
    edges: list[tuple[int, int]] = []

    def intern(k, pt: Coord) -> int:
        i = node_id.get(k)
        if i is None:
            i = len(coords)
            node_id[k] = i
            coords.append(pt)
        return i

    for w in ways:
        if len(w) < 2:
            continue
        for a, b in zip(w, w[1:]):
            ka, kb = key(a), key(b)
            if ka == kb:
                continue  # zero-length or sub-weld segment contributes no edge
            edges.append((intern(ka, a), intern(kb, b)))
    return edges, coords


def route_cycle_count(ways: list[list[Coord]], weld_m: float = 1.0) -> int:
    """Independent cycles in a route's member ways (its circuit rank), built from
    the FULL vertex graph.

    Models the members as a multigraph whose nodes are every distinct *vertex*
    (welded by coordinate) and whose edges are the consecutive-vertex segments of
    each way. Returns the first Betti number ``E - V + C`` (edges - nodes +
    connected components) — the number of independent loops; ``> 0`` means the
    route closes into at least one loop, independent of member order/orientation.

    Why the full vertex graph and not just way *endpoints*: OSM ways that connect
    share the *identical* node, so two ways meeting at a T-junction share an
    interior vertex. Keying on every vertex therefore detects T-junction closures
    that an endpoint-only graph misses. Just as important, it does NOT cluster
    distinct endpoints within a tolerance: on dense real relations a 30 m endpoint
    cluster over-merges piled-up endpoints and *invents* cycles, which mislabelled
    linear KČT routes as loops (validated live against the "Medvěd*" relations —
    see HANDOFF). Exact vertex sharing has neither failure mode.

    ``weld_m`` is a small coincidence tolerance (metres) that merges vertices
    representing the same node despite float noise; it sits well below trail
    vertex spacing (~5–15 m), so it never fuses genuinely distinct points. A loop
    closed only by a digitization *gap* wider than ``weld_m`` reads as open here —
    ``access.is_circular`` catches that with its start≈end line fallback.
    """
    edges, coords = _vertex_graph(ways, weld_m=weld_m)
    e = len(edges)
    if e == 0:
        return 0
    v = len(coords)  # only vertices that carry an edge are registered
    comp = _UnionFind(v)
    for u, w_ in edges:
        comp.union(u, w_)
    c = comp.num_components()
    return e - v + c


def route_termini(ways: list[list[Coord]], weld_m: float = 1.0) -> list[Coord]:
    """The route's genuine open ends: the degree-1 vertices of the full vertex graph.

    A route's termini — where you actually start and finish — are the vertices that
    exactly one segment touches (dead-ends). Built from the SAME welded vertex graph
    as closure (``_vertex_graph``), so it is stitch-order- and orientation-
    independent and — unlike the greedy stitched line's two ends — captures EVERY
    end, including ones on members ``stitch_ways`` drops on a branched or gap-split
    relation. That is the point: car/lift access is tested at these ends, and a real
    trailhead on a dropped member would otherwise be missed.

    Returns each degree-1 vertex's representative coordinate, in node-discovery
    order. Two cases yield NO degree-1 vertex, both correct and not regressions:
      * a pure loop — every vertex has even degree (callers fall back to the
        stitched line's ends, i.e. one representative point on the loop);
      * a route mapping the same stretch both forward and backward — the parallel
        edges give every vertex even degree too (no gain over today, no loss).
    A gap-split relation yields the two real outer ends PLUS two spurious interior
    ends at the gap; testing all four only *improves* recall — the real outer ends,
    which the stitch may drop, are now included — at the low-stakes cost of a
    possible access hit at a mid-route gap.
    """
    edges, coords = _vertex_graph(ways, weld_m=weld_m)
    if not edges:
        return []
    degree = [0] * len(coords)
    for u, v in edges:
        degree[u] += 1
        degree[v] += 1
    return [coords[i] for i in range(len(coords)) if degree[i] == 1]


def resample_by_distance(points: list[Coord], interval_m: float = 25.0) -> list[Coord]:
    """Resample a polyline to roughly even spacing.

    Raw OSM vertices are irregularly spaced, which biases elevation gain
    (dense vertices -> more samples -> more counted noise). Resampling to a
    fixed interval makes gain independent of vertex density. This is essential
    for consistent numbers across trails.
    """
    if len(points) < 2:
        return list(points)

    out: list[Coord] = [points[0]]
    # Distance walked along the polyline since the last emitted sample. We emit
    # whenever it reaches `interval_m` partway through a segment, then carry the
    # remainder forward. The invariant `since_last < interval_m` holds at every
    # segment boundary, so fine sub-interval vertices ACCUMULATE toward the next
    # sample instead of being skipped. (The previous version grew its carry
    # without ever emitting, collapsing finely-vertexed OSM lines to 2 points.)
    since_last = 0.0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        seg = haversine_m(a, b)
        if seg == 0:
            continue
        pos = interval_m - since_last  # offset into THIS segment of the next sample
        while pos <= seg:
            t = pos / seg
            out.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
            pos += interval_m
        since_last = seg - (pos - interval_m)  # leftover from last sample to b
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out
