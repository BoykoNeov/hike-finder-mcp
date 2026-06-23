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


def _cluster_points(points: list[Coord], snap_m: float) -> list[int]:
    """Cluster nearby points; return a contiguous node id (0-based) per point.

    Transitive single-linkage within ``snap_m`` via union-find, so the clustering
    is independent of the order the points arrive in — unlike greedy first-match,
    which can split or mis-merge endpoints depending on member order.
    """
    n = len(points)
    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if haversine_m(points[i], points[j]) <= snap_m:
                uf.union(i, j)
    root_to_id: dict[int, int] = {}
    node_of: list[int] = []
    for i in range(n):
        r = uf.find(i)
        if r not in root_to_id:
            root_to_id[r] = len(root_to_id)
        node_of.append(root_to_id[r])
    return node_of


def route_cycle_count(ways: list[list[Coord]], snap_m: float = 30.0) -> int:
    """Independent cycles enclosed by a route's member ways (its circuit rank).

    Models the members as a multigraph: nodes are clustered way *endpoints*
    (heads/tails), edges are the ways. Returns the first Betti number
    ``E - V + C`` (edges - nodes + connected components) — the number of
    independent loops. ``> 0`` means the route closes into at least one loop, no
    matter what order or orientation the members arrive in.

    Why circuit rank and not "every endpoint has even degree": the even-degree
    test demands a full Eulerian circuit, so it misses a *lollipop* (a loop
    reached by an approach stem) — exactly the okruh-with-a-spur shape most real
    KČT loop relations take. Circuit rank counts the loop and ignores the stem.

    Order-independent by construction (endpoints clustered by proximity, not
    greedy first-match). Limitation: only way *endpoints* are nodes, so a way
    whose endpoint touches another way's *interior* vertex (a T-junction) is not
    seen as joined there — fixing that needs vertex-splitting; the old code
    missed it too.
    """
    segs = [w for w in ways if len(w) >= 2]
    e = len(segs)
    if e == 0:
        return 0

    # Pass 1 (geometry): cluster the 2E raw endpoints into nodes. endpoints[2i]
    # and endpoints[2i+1] are way i's head and tail.
    endpoints: list[Coord] = []
    for w in segs:
        endpoints.append(w[0])
        endpoints.append(w[-1])
    node_of = _cluster_points(endpoints, snap_m)
    v = len(set(node_of))

    # Pass 2 (topology): join the two nodes each way connects, then count the
    # connected components of the node graph. This is graph connectivity, a
    # separate question from the geometric clustering that produced the nodes.
    comp = _UnionFind(v)
    for i in range(e):
        comp.union(node_of[2 * i], node_of[2 * i + 1])
    c = comp.num_components()

    return e - v + c


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
