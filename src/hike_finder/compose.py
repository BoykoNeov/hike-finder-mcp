"""Compose loops from connected marked-trail segments.

Most KČT ``route=hiking`` relations are *linear* marked segments (a coloured
trail A→B); a circular day-hike is usually an ad-hoc combination of several
connected segments. The rest of the engine reports each relation as-is, so
``circular=true`` only surfaces the few loops mapped as a single relation. This
module synthesises loops: it builds one graph from every relation's member ways
and searches it for cycles of a target length.

Pure and network-free, like the other geometry math — the trust anchor. The
build is in two stages:

  1. ``build_trail_graph`` welds all member ways into one full-vertex multigraph
     (same welding rule as ``geometry._vertex_graph``, so junctions are exact
     shared OSM nodes — never endpoint clusters, the bug that invented false
     cycles in the Medvěd* work), then **contracts** every degree-2 chain into a
     single :class:`Segment` between two junctions. Junctions are the degree≠2
     nodes — the only places a route choice exists. Parallel segments between the
     same junction pair are kept (a multigraph), because two trails between the
     same two junctions are themselves a valid loop.
  2. ``find_loops`` (separate step) searches that contracted graph for cycles.

A composed loop is a *suggestion* stitched from several marked trails, not one
named trail — callers must render its provenance (the constituent trail refs),
never a single OSM relation id.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import NamedTuple

from .geometry import Coord, haversine_m

# Same coincidence tolerance as geometry._vertex_graph: merges vertices that are
# the same OSM node despite float noise, well below trail vertex spacing so it
# never fuses genuinely distinct points. Do NOT raise this to bridge digitization
# gaps — a larger weld clusters distinct endpoints and invents cycles (the live-
# falsified Medvěd* failure). Bridging, if ever needed, is a separate degree-1
# snap, not a global weld bump.
WELD_M = 1.0


def _weld_cell(weld_m: float) -> float:
    return weld_m / 111_320.0 if weld_m > 0 else 0.0


def _weld_key(pt: Coord, cell: float):
    """Grid cell a vertex welds into (mirrors geometry._vertex_graph's key)."""
    if cell <= 0:
        return pt
    return (round(pt[0] / cell), round(pt[1] / cell))


@dataclass
class Segment:
    """A contracted trail segment: one degree-2 chain between two junctions.

    ``a``/``b`` are junction node ids; ``coords`` is the full polyline from node
    ``a`` to node ``b`` (node-representative coords, so segments sharing a junction
    share the *exact* coordinate and an assembled loop is truly closed). ``refs``
    are the distinct trail refs/colours traversed, for provenance. A segment with
    ``a == b`` is an already-closed loop component that has no junction to split.
    """

    a: int
    b: int
    coords: list[Coord]
    length_m: float
    refs: tuple[str, ...] = ()


@dataclass
class TrailGraph:
    """Contracted junction multigraph of an area's marked trails."""

    coords: list[Coord]  # node id -> representative coordinate
    segments: list[Segment]
    # junction node id -> indices into `segments` incident to it
    adj: dict[int, list[int]] = field(default_factory=dict)

    def degree(self, node: int) -> int:
        return len(self.adj.get(node, []))


def _route_ref(route: dict) -> str:
    """Short provenance label for a route: its ref, else name, else osm id."""
    return (
        route.get("ref")
        or route.get("name")
        or f"route/{route.get('id')}"
    )


def clip_routes_to_bbox(routes: list[dict], bbox: tuple[float, float, float, float]) -> list[dict]:
    """Drop every way-vertex outside ``bbox`` (``south, west, north, east``), splitting
    a way into its contiguous in-bbox runs.

    Composition uses this so a synthesised loop lies *inside the searched area*, the
    same constraint every other result obeys — without it a loop wanders out of the
    view on a through-route that merely clips the box (observed: 13 of 14 loops on the
    Špindl bbox left it). Clipping at vertex granularity (~5–15 m spacing) is a coarse
    stand-in for true geometric bbox-clipping; a trail leaving and re-entering becomes
    two runs with a gap at the boundary (correct — the out-of-area arc is unavailable).
    Route metadata (``ref``/``name``/``id``) is preserved for provenance.
    """
    south, west, north, east = bbox

    def inside(pt: Coord) -> bool:
        return south <= pt[0] <= north and west <= pt[1] <= east

    out: list[dict] = []
    for r in routes:
        ways: list[list[Coord]] = []
        for w in r.get("ways", []):
            run: list[Coord] = []
            for pt in w:
                if inside(pt):
                    run.append(pt)
                else:
                    if len(run) >= 2:
                        ways.append(run)
                    run = []
            if len(run) >= 2:
                ways.append(run)
        out.append({**r, "ways": ways})
    return out


def build_trail_graph(routes: list[dict], weld_m: float = WELD_M) -> TrailGraph:
    """Weld all routes' member ways into one graph and contract degree-2 chains.

    Returns a :class:`TrailGraph` whose nodes are trail junctions/dead-ends and
    whose segments are the trail stretches between them, each tagged with the
    trail refs it traverses. The contraction is what makes cycle search tractable:
    long runs of degree-2 vertices collapse to a single edge.
    """
    cell = _weld_cell(weld_m)
    node_id: dict = {}
    coords: list[Coord] = []

    def intern(pt: Coord) -> int:
        k = _weld_key(pt, cell)
        i = node_id.get(k)
        if i is None:
            i = len(coords)
            node_id[k] = i
            coords.append(pt)
        return i

    # Micro-edges: one per *distinct* welded adjacent-node pair. A physical trail
    # edge shared by several relations (the same OSM way belongs to many route
    # relations) welds to the SAME node pair, so we keep ONE micro-edge and union
    # the route ids onto it — rather than one parallel edge per relation, which
    # would inflate every shared interior node to degree 4 and spawn zero-area
    # "sliver" loops between two coincident trails. Genuinely parallel trails take
    # *different* intermediate nodes, so their node pairs differ and they survive
    # as separate edges (a real two-segment loop). Two adjacent welded nodes admit
    # only one physical edge, so deduping by the node pair loses no real geometry.
    edge_routes: dict[tuple[int, int], set[int]] = {}
    for ri, route in enumerate(routes):
        for way in route.get("ways", []):
            if len(way) < 2:
                continue
            prev = intern(way[0])
            for pt in way[1:]:
                cur = intern(pt)
                if cur != prev:  # skip zero-length / sub-weld steps
                    key = (prev, cur) if prev < cur else (cur, prev)
                    edge_routes.setdefault(key, set()).add(ri)
                prev = cur

    # Stable micro-edge ids (sorted node pairs) so the contracted graph — and every
    # loop derived from it — is deterministic regardless of dict iteration order.
    micro_ends: list[tuple[int, int]] = sorted(edge_routes)
    micro_routes: list[set[int]] = [edge_routes[k] for k in micro_ends]
    micro_adj: dict[int, list[tuple[int, int]]] = {}
    for eid, (u, v) in enumerate(micro_ends):
        micro_adj.setdefault(u, []).append((v, eid))
        micro_adj.setdefault(v, []).append((u, eid))

    degree = {n: len(adj) for n, adj in micro_adj.items()}
    refs = [_route_ref(r) for r in routes]

    segments: list[Segment] = []
    adj: dict[int, list[int]] = {}
    consumed = [False] * len(micro_ends)

    def register(seg: Segment) -> None:
        idx = len(segments)
        segments.append(seg)
        adj.setdefault(seg.a, []).append(idx)
        if seg.b != seg.a:
            adj.setdefault(seg.b, []).append(idx)

    def walk(start: int, first_other: int, first_eid: int) -> Segment:
        """Walk a degree-2 chain from junction ``start`` until the next non-degree-2
        node (or back to ``start`` for an isolated loop)."""
        seg_coords = [coords[start]]
        seg_routes: set[int] = set()
        length = 0.0
        cur, other, eid = start, first_other, first_eid
        while True:
            consumed[eid] = True
            seg_coords.append(coords[other])
            length += haversine_m(coords[cur], coords[other])
            seg_routes |= micro_routes[eid]
            if degree.get(other, 0) != 2 or other == start:
                return Segment(
                    a=start,
                    b=other,
                    coords=seg_coords,
                    length_m=length,
                    refs=tuple(sorted({refs[r] for r in seg_routes})),
                )
            # Continue through the degree-2 node to its other micro-edge.
            nxt_other, nxt_eid = next(
                (o, e) for (o, e) in micro_adj[other] if e != eid
            )
            cur, other, eid = other, nxt_other, nxt_eid

    # 1) Contract every chain anchored at a junction (degree != 2 node).
    for node in sorted(n for n, d in degree.items() if d != 2):
        for other, eid in micro_adj[node]:
            if not consumed[eid]:
                register(walk(node, other, eid))

    # 2) Pure-loop components: every node degree 2, no junction to seed. Any
    #    micro-edge still unconsumed sits on such a ring — walk it to a self-loop
    #    segment (a == b). These are already-closed loops a single relation maps.
    for eid in range(len(micro_ends)):
        if not consumed[eid]:
            u, v = micro_ends[eid]
            register(walk(u, v, eid))

    return TrailGraph(coords=coords, segments=segments, adj=adj)


# --------------------------------------------------------------------------- loop search


@dataclass
class ComposedLoop:
    """One synthesised loop: a closed polyline stitched from several segments.

    ``refs`` is the set of constituent trail refs/colours (the provenance — a
    composed loop is a suggestion across these marked trails, not one named
    relation). ``seg_ids`` indexes the source :class:`TrailGraph`'s segments and is
    kept for near-duplicate detection; it is not part of the rendered result.
    """

    coords: list[Coord]
    length_m: float
    refs: tuple[str, ...]
    segment_count: int
    # Polsby–Popper compactness 4πA/P² in [0,1]: ~1 = round, ~0 = a thin out-and-back
    # sliver. Used to rank/cap loops so the roundest (most loop-like) ones come first.
    compactness: float = 0.0
    seg_ids: frozenset[int] = frozenset()


class ComposeResult(NamedTuple):
    loops: list[ComposedLoop]  # what to show — after collapse AND the max_loops cap
    found: int  # distinct cycles in band before near-duplicate collapse
    distinct: int  # loops after near-duplicate collapse, before the max_loops cap
    capped: bool  # True if the cycle search hit its expansion budget (results incomplete)


def _compactness(coords: list[Coord]) -> float:
    """Polsby–Popper compactness 4πA/P² of a closed polyline, in [0, 1].

    ~1 is a circle; values near 0 are long and thin (a sliver — out on one trail, back
    on a near-parallel one). Area uses an equirectangular projection about the loop's
    mean latitude — fine at trail scale, and only a *ranking* signal, not a measurement.
    """
    if len(coords) < 4:
        return 0.0
    lat0 = sum(p[0] for p in coords) / len(coords)
    k = 111_320.0
    kx = k * math.cos(math.radians(lat0))
    xs = [(lon) * kx for _, lon in coords]
    ys = [(lat) * k for lat, _ in coords]
    area = abs(sum(xs[i] * ys[i + 1] - xs[i + 1] * ys[i] for i in range(len(xs) - 1))) / 2
    perim = sum(
        math.dist((xs[i], ys[i]), (xs[i + 1], ys[i + 1])) for i in range(len(xs) - 1)
    )
    return 4 * math.pi * area / (perim * perim) if perim else 0.0


def _active_segments(graph: TrailGraph) -> set[int]:
    """Segment ids that can lie on a simple cycle: drop self-loops, then iteratively
    drop any segment touching a degree-1 node (a dead-end stem can never close a
    cycle). Shrinks the search to the graph's 2-edge-connected core."""
    alive = {i for i, s in enumerate(graph.segments) if s.a != s.b}
    changed = True
    while changed:
        changed = False
        deg: dict[int, int] = {}
        for i in alive:
            s = graph.segments[i]
            deg[s.a] = deg.get(s.a, 0) + 1
            deg[s.b] = deg.get(s.b, 0) + 1
        leaves = {n for n, d in deg.items() if d == 1}
        if leaves:
            alive = {
                i
                for i in alive
                if not ({graph.segments[i].a, graph.segments[i].b} & leaves)
            }
            changed = True
    return alive


def _assemble(graph: TrailGraph, start: int, seg_ids: list[int]) -> ComposedLoop:
    """Stitch an ordered segment list (a cycle from ``start`` back to ``start``) into
    one closed polyline, orienting each segment to continue from the current node."""
    coords: list[Coord] = []
    refs: set[str] = set()
    total = 0.0
    cur = start
    for idx in seg_ids:
        s = graph.segments[idx]
        pts = s.coords if s.a == cur else list(reversed(s.coords))
        nxt = s.b if s.a == cur else s.a
        refs.update(s.refs)
        total += s.length_m
        coords.extend(pts[1:] if coords else pts)
        cur = nxt
    return ComposedLoop(
        coords=coords,
        length_m=total,
        refs=tuple(sorted(refs)),
        segment_count=len(seg_ids),
        compactness=_compactness(coords),
        seg_ids=frozenset(seg_ids),
    )


def find_loops(
    graph: TrailGraph,
    *,
    min_m: float,
    max_m: float,
    max_segments: int = 12,
    max_loops: int | None = None,
    budget: int = 500_000,
    overlap_frac: float = 0.6,
) -> ComposeResult:
    """Search the contracted graph for loops with total length in ``[min_m, max_m]``.

    A bounded, deterministic enumeration of *simple* cycles (no repeated junction):

      * **min-node start** — a cycle is enumerated only from its smallest node id,
        and only steps to nodes ``>= start``, so each cycle is reached once per
        direction (the two directions are then collapsed by edge-set identity);
      * **length prune** — a partial path is abandoned the moment it exceeds
        ``max_m`` (a simple cycle only gets longer), the key tractability lever;
      * **segment cap** — at most ``max_segments`` segments per loop (real day loops
        are a handful of junctions), and a **global expansion budget** that aborts
        with ``capped=True`` rather than running away on a dense graph;
      * **edge-set dedup** — cycles are keyed by their frozenset of segment ids, so
        the two traversal directions (and any rotation) collapse to one;
      * **near-duplicate collapse** — among the in-band cycles (shortest first), a
        loop sharing more than ``overlap_frac`` of its length with an already-kept
        loop is dropped, so "the same loop plus a short detour" doesn't flood the
        results. Self-loop segments (already-closed single-relation loops) in band
        are included directly.
      * **max_loops cap** — the survivors are ranked by **compactness** (roundest /
        most loop-like first; this also demotes any thin near-sliver) and, if
        ``max_loops`` is set, truncated to it. This is not cosmetic: the caller pays
        an elevation lookup *per returned loop*, so on a dense area an uncapped set
        (72 loops were observed on a 13×14 km box) would break the two-pass economy
        and blow the elevation API quota. ``ComposeResult.distinct`` reports the
        pre-cap count so a truncation is never silent.

    Determinism: neighbours are visited in sorted order and segment ids are stable
    (sorted node pairs in ``build_trail_graph``), so the output is identical run to
    run — required by the project's byte-for-byte ethos.
    """
    active = _active_segments(graph)

    # Already-closed loops (a single relation mapping a ring) within the band.
    selfloops = [
        _assemble(graph, s.a, [i])
        for i, s in enumerate(graph.segments)
        if s.a == s.b and min_m <= s.length_m <= max_m
    ]

    # Incidence over active segments only, neighbours sorted for determinism.
    inc: dict[int, list[int]] = {}
    for i in active:
        s = graph.segments[i]
        inc.setdefault(s.a, []).append(i)
        inc.setdefault(s.b, []).append(i)
    for n in inc:
        inc[n].sort(key=lambda i: (graph.segments[i].b if graph.segments[i].a == n
                                   else graph.segments[i].a, i))

    seen: set[frozenset[int]] = set()
    found: list[ComposedLoop] = []
    state = {"exp": 0, "capped": False}

    def dfs(start: int, cur: int, path: list[int], length: float, visited: set[int]) -> None:
        if state["exp"] >= budget:
            state["capped"] = True
            return
        for idx in inc.get(cur, ()):
            s = graph.segments[idx]
            other = s.b if s.a == cur else s.a
            if other < start:
                continue  # the cycle's min node must be `start`
            new_len = length + s.length_m
            if new_len > max_m:
                continue  # prune: a simple cycle only grows from here
            if other == start:
                key = frozenset(path + [idx])
                if len(key) == len(path) + 1 and new_len >= min_m and key not in seen:
                    seen.add(key)
                    found.append(_assemble(graph, start, path + [idx]))
                continue
            if other in visited or len(path) >= max_segments:
                continue
            state["exp"] += 1
            dfs(start, other, path + [idx], new_len, visited | {other})

    for start in sorted(inc):
        dfs(start, start, [], 0.0, {start})

    # Near-duplicate collapse: keep shortest first; drop a loop that re-uses more
    # than `overlap_frac` of its own length from an already-kept loop.
    seg_len = {i: graph.segments[i].length_m for i in active}
    candidates = sorted(found + selfloops, key=lambda L: (round(L.length_m, 3), L.coords))
    kept: list[ComposedLoop] = []
    for L in candidates:
        dup = False
        for K in kept:
            shared = sum(seg_len.get(i, 0.0) for i in (L.seg_ids & K.seg_ids))
            if L.length_m > 0 and shared / L.length_m > overlap_frac:
                dup = True
                break
        if not dup:
            kept.append(L)

    # Rank the survivors by compactness (roundest first; thin loops sink) and cap, so
    # the caller only elevations a bounded, most-loop-like set. Ties break by length
    # then coords for determinism.
    ranked = sorted(kept, key=lambda L: (-round(L.compactness, 6), round(L.length_m, 3), L.coords))
    shown = ranked if max_loops is None else ranked[:max_loops]
    return ComposeResult(
        loops=shown,
        found=len(found) + len(selfloops),
        distinct=len(kept),
        capped=state["capped"],
    )
