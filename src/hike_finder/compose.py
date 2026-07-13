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

from .access import _bbox_pad
from .geometry import Coord, haversine_m, polyline_length_m, resample_by_distance

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
    # When the loop is access-anchored (see ``find_loops``' ``anchors``), the on-loop
    # vertex nearest the trailhead (parking/lift) you start from — rendered as the
    # loop's start, since a pure loop has no natural terminus. ``None`` when anchoring
    # is off (the start then stays at the loop's arbitrary geometric head).
    anchor: Coord | None = None
    # The loop's traversal, retained so its elevation can be assembled per-segment
    # (``assemble_loop_series``): ``start_node`` is the junction the walk begins at and
    # ``ordered_segs`` is the ordered list of segment ids visited — the same sequence
    # ``_assemble`` walked to build ``coords``. Unlike the ``seg_ids`` frozenset (which
    # is direction/rotation-free, for dedup), this preserves order and start so the
    # per-segment elevation lists concatenate in exactly the loop's geometric order.
    start_node: int = -1
    ordered_segs: tuple[int, ...] = ()


class ComposeResult(NamedTuple):
    loops: list[ComposedLoop]  # what to show — after collapse AND the max_loops cap
    found: int  # distinct cycles in band before near-duplicate collapse
    distinct: int  # loops after near-duplicate collapse, before the max_loops cap
    capped: bool  # True if the cycle search hit its expansion budget (results incomplete)
    slivered: int = 0  # in-band cycles dropped by the min_compactness sliver filter


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
        start_node=start,
        ordered_segs=tuple(seg_ids),
    )


def resample_segments(
    graph: TrailGraph, seg_ids, interval_m: float = 25.0
) -> dict[int, list[Coord]]:
    """Resample each segment in ``seg_ids`` to even spacing, ONCE per distinct segment.

    The key to segment-level shared sampling: a trail segment shared by several
    composed loops is resampled (and, by the caller, elevation-looked-up) exactly
    once, keyed by segment id, instead of once per loop that traverses it. Each
    segment is sampled in its own ``a -> b`` direction (the order of ``Segment.coords``)
    so the points are identical regardless of which loop, or which direction, uses it
    — which is what makes them dedup within a run and recur across runs (cache-hot).
    """
    return {
        i: resample_by_distance(graph.segments[i].coords, interval_m) for i in seg_ids
    }


def assemble_loop_series(graph: TrailGraph, loop: "ComposedLoop", per_segment: dict):
    """Concatenate per-segment values into one series following the loop's traversal.

    Walks ``loop.ordered_segs`` from ``loop.start_node`` exactly as :func:`_assemble`
    walked them to build ``loop.coords`` — orienting each segment's value list to
    continue from the current node (reversed when traversed ``b -> a``) and dropping the
    shared junction value between consecutive segments. ``per_segment`` maps segment id
    to a value list in that segment's ``a -> b`` order (e.g. its resampled points, or the
    elevations of those points).

    Returns the assembled list, or ``None`` if any segment the loop needs is missing
    from ``per_segment`` (e.g. its elevation lookup failed) — so the caller degrades the
    whole loop to n/a rather than stitching a gap. Because the first and last values of
    the assembled series are the same (closed-loop) start-node sample, an elevation
    series assembled this way is closed, so gain ≈ loss holds just as for a whole-line
    resample.
    """
    out: list = []
    cur = loop.start_node
    for idx in loop.ordered_segs:
        s = graph.segments[idx]
        vals = per_segment.get(idx)
        if vals is None:
            return None
        oriented = vals if s.a == cur else list(reversed(vals))
        nxt = s.b if s.a == cur else s.a
        out.extend(oriented[1:] if out else oriented)
        cur = nxt
    return out


def _anchor_vertex(
    coords: list[Coord],
    anchors: list[tuple[list[Coord], float]],
) -> Coord | None:
    """Decide whether a loop is reachable from the requested access, and where to start.

    ``anchors`` is a list of ``(access_points, radius_m)`` requirements that must ALL be
    met — a loop asked to have car *and* lift access has to come within range of both.
    The test is exactly the one ``access.car_accessible`` / ``chairlift_access`` run on
    the synthesised route in ``find_hikes`` (``haversine <= radius`` against every loop
    vertex), over the same whole-loop point set, so a loop kept here is precisely one
    ``find_hikes`` will accept — no loop is anchored-then-filtered or kept-then-dropped.

    Returns the on-loop vertex to use as the start: the loop vertex nearest the closest
    access point of the *first* requirement, so callers that order requirements
    parking-first get a start "where you park" even when a lift is also in range.
    Returns ``None`` when any requirement is unmet (the loop is then dropped).
    """
    start_vtx: Coord | None = None
    for ai, (points, radius) in enumerate(anchors):
        # Skip access points that provably can't be within `radius` of ANY loop vertex,
        # before the O(vertices) inner scan — the exact same EXACT prune access.py uses on
        # the whole-line scan (it only drops points too far to ever match). Without it the
        # pre-collapse pool × every access point × every vertex is seconds on a dense area
        # (the 24× whole-line regression all over again); with it, ~unchanged.
        lo_lat, hi_lat, lo_lon, hi_lon = _bbox_pad(coords, radius)
        best_d = float("inf")
        best_vtx: Coord | None = None
        for ap in points:
            if not (lo_lat <= ap[0] <= hi_lat and lo_lon <= ap[1] <= hi_lon):
                continue
            for v in coords:
                d = haversine_m(v, ap)
                # Tie-break by coordinate (not iteration order) so the start is member-
                # order independent — the same discipline as _route_start / matched_access.
                if d <= radius and (best_vtx is None or (d, v) < (best_d, best_vtx)):
                    best_d = d
                    best_vtx = v
        if best_vtx is None:
            return None  # this access type isn't reachable from the loop -> drop it
        if ai == 0:
            start_vtx = best_vtx
    return start_vtx


def find_loops(
    graph: TrailGraph,
    *,
    min_m: float,
    max_m: float,
    max_segments: int = 12,
    max_loops: int | None = None,
    budget: int = 500_000,
    overlap_frac: float = 0.6,
    min_compactness: float = 0.0,
    anchors: list[tuple[list[Coord], float]] | None = None,
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
      * **sliver filter** (``min_compactness`` > 0) — a hard compactness floor that
        *drops* degenerate near-zero-area loops outright (an out-and-back along two
        near-parallel marked trails: high perimeter, almost no enclosed area). This
        runs **before** both the near-duplicate collapse and the cap, so a sliver can
        neither sway a collapse decision nor consume a returned slot. Compactness
        (``4πA/P²``, scale-invariant) is the right discriminator — it separates *thin*
        from merely *small*, and short loops are already excluded by the ``min_m``
        length band; an absolute-area floor would instead wrongly kill a small but
        round loop. Off by default (0.0): real marked-trail loops sit well above any
        sliver (observed ≥ 0.18 on a dense real bbox), so the engine sets a small
        positive floor while the pure default stays inert. ``ComposeResult.slivered``
        counts the drops so the filter is never silent.
      * **max_loops cap** — the survivors are ranked by **compactness** (roundest /
        most loop-like first; this also demotes any thin near-sliver) and, if
        ``max_loops`` is set, truncated to it. This is not cosmetic: the caller pays
        an elevation lookup *per returned loop*, so on a dense area an uncapped set
        (72 loops were observed on a 13×14 km box) would break the two-pass economy
        and blow the elevation API quota. ``ComposeResult.distinct`` reports the
        pre-cap count so a truncation is never silent.

    **Access anchoring** (``anchors``, optional): a list of ``(access_points,
    radius_m)`` requirements (see :func:`_anchor_vertex`). When given, only loops
    reachable from *every* requirement survive — and crucially this filter runs
    BEFORE the collapse and the cap, so both spend their budget on the accessible
    subset. Without it the compactness cap can fill up with compact-but-unreachable
    loops that ``find_hikes`` then filters out, hiding genuine "loops from where I
    park" behind the cap. Each surviving loop is tagged with its start vertex
    (:attr:`ComposedLoop.anchor`). ``found`` still counts every in-band cycle, so the
    accessible-vs-found funnel stays visible (never a silent filter).

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

    # Access anchoring (optional): keep only loops reachable from a requested access
    # feature, tagging each with its on-loop start vertex. Done BEFORE collapse and the
    # cap so both operate on the accessible subset (see the `anchors` note above).
    pool: list[ComposedLoop] = found + selfloops
    in_band = len(pool)

    # Sliver filter (before anchoring, collapse, AND the cap): drop degenerate
    # near-zero-area loops by a hard compactness floor, so a thin out-and-back along
    # two near-parallel trails can't reach the results, sway a near-dup collapse, or
    # eat a returned slot. `found`/`in_band` still counts every in-band cycle, so the
    # funnel stays visible; `slivered` reports how many of them this dropped.
    slivered = 0
    if min_compactness > 0.0:
        kept_shape = [L for L in pool if L.compactness >= min_compactness]
        slivered = len(pool) - len(kept_shape)
        pool = kept_shape

    if anchors:
        anchored: list[ComposedLoop] = []
        for L in pool:
            vtx = _anchor_vertex(L.coords, anchors)
            if vtx is not None:
                L.anchor = vtx
                anchored.append(L)
        pool = anchored

    # Near-duplicate collapse: keep shortest first; drop a loop that re-uses more
    # than `overlap_frac` of its own length from an already-kept loop.
    seg_len = {i: graph.segments[i].length_m for i in active}
    candidates = sorted(pool, key=lambda L: (round(L.length_m, 3), L.coords))
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
        found=in_band,
        distinct=len(kept),
        capped=state["capped"],
        slivered=slivered,
    )


# ------------------------------------------------------------- point-to-point routing
#
# ``find_loops`` searches for *cycles*; the two-point feature ("draw me the N shortest
# routes between A and B") is the *path* problem on the same contracted graph. It reuses
# ``build_trail_graph`` and ``_assemble`` unchanged: the only new machinery is (1) snapping
# each picked point onto the network by SPLITTING the nearest segment at the projected
# point — so a route genuinely starts at where you pointed, not at a junction kilometres
# away — and (2) Yen's k-shortest-loopless-paths over the junction multigraph, tracked by
# *segment id* (the graph is a multigraph: two trails can join the same junction pair, and
# removing one must not remove its parallel twin).


# A position on a polyline: ``(edge_index, frac)`` = a fraction ``frac`` along the edge from
# vertex ``edge_index`` to ``edge_index + 1``. A vertex ``k`` is ``(k, 0.0)`` (or the
# previous edge's ``(k-1, 1.0)``); the two endpoints are ``(0, 0.0)`` and ``(len-2, 1.0)``.
_Pos = tuple[int, float]


def _interp(line: list[Coord], pos: _Pos) -> Coord:
    """The (lat, lon) at position ``pos`` on ``line`` (linear in lat/lon — trail scale)."""
    e, f = pos
    a, b = line[e], line[e + 1]
    return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))


def _project_point(line: list[Coord], p: Coord) -> tuple[float, _Pos, Coord]:
    """Nearest point on polyline ``line`` to ``p``: ``(dist_m, position, coord)``.

    Projects onto each edge in a local equirectangular frame about ``p`` (metres, exact
    enough at trail scale), clamped to the edge, and keeps the closest — deterministic
    tie-break by ``(dist, edge, frac)`` so a point equidistant to two edges snaps to the
    lower-indexed one every run.
    """
    lat0 = math.radians(p[0])
    kx = 111_320.0 * math.cos(lat0)
    ky = 111_320.0
    px, py = p[1] * kx, p[0] * ky
    best: tuple[float, _Pos] | None = None
    for i in range(len(line) - 1):
        ax, ay = line[i][1] * kx, line[i][0] * ky
        bx, by = line[i + 1][1] * kx, line[i + 1][0] * ky
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0.0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
        cx, cy = ax + t * dx, ay + t * dy
        d = math.hypot(px - cx, py - cy)
        cand = (d, (i, t))
        if best is None or cand < best:
            best = cand
    assert best is not None  # callers guarantee len(line) >= 2
    d, pos = best
    return d, pos, _interp(line, pos)


def _subpolyline(line: list[Coord], p1: _Pos, p2: _Pos) -> list[Coord]:
    """The ordered coords of ``line`` between positions ``p1`` and ``p2`` (``p1`` before
    ``p2``), with the interpolated boundary points at each end. Consecutive duplicates
    (a boundary landing exactly on a vertex) are collapsed."""
    (e1, _), (e2, _) = p1, p2
    pts = [_interp(line, p1)]
    pts.extend(line[e1 + 1 : e2 + 1])  # interior vertices strictly between p1 and p2
    pts.append(_interp(line, p2))
    out = [pts[0]]
    for q in pts[1:]:
        if q != out[-1]:
            out.append(q)
    return out


def snap_points(
    graph: TrailGraph, points: list[Coord], *, snap_weld_m: float = 1.0
) -> tuple[TrailGraph, list[tuple[int, float]]]:
    """Snap each point onto the trail network, returning an augmented graph and, per point,
    ``(node_id, snap_distance_m)``.

    Each point is projected to the nearest point on the nearest segment. When that lands on
    an existing junction (within ``snap_weld_m``) the junction is used directly; otherwise
    the segment is **split** at the projection into a fresh temporary node, so a route can
    start/end exactly there — the alternative, snapping to the nearest junction, silently
    moves a mid-trail trailhead to the next fork, which can be kilometres off. Multiple
    points landing on the same segment split it at every position at once (so a pair of
    points on one long segment yields the direct sub-segment between them). The returned
    graph is a superset: original segment ids are preserved except for split segments, which
    are replaced by their pieces — so Yen output assembled through it stays valid.
    """
    if not graph.segments:
        return graph, [(-1, float("inf")) for _ in points]

    # Project every point; group the ones that need a mid-segment split by target segment.
    snaps: list[dict] = []
    for p in points:
        best_sid, best = -1, None
        for sid, s in enumerate(graph.segments):
            if len(s.coords) < 2:
                continue
            d, pos, coord = _project_point(s.coords, p)
            cand = (d, sid, pos)
            if best is None or cand < (best[0], best_sid, best[1]):
                best, best_sid = (d, pos, coord), sid
        snaps.append({"sid": best_sid, "dist": best[0], "pos": best[1], "coord": best[2]})

    coords = list(graph.coords)

    def add_node(coord: Coord) -> int:
        coords.append(coord)
        return len(coords) - 1

    # Resolve each point to a node id, splitting where needed. First pass: decide, per
    # point, whether it snaps to an existing endpoint or needs a temp node on a segment.
    splits: dict[int, list[tuple[_Pos, int]]] = {}  # sid -> [(pos, temp_node_id)]
    result: list[tuple[int, float]] = []
    for snap in snaps:
        sid, pos, coord, dist = snap["sid"], snap["pos"], snap["coord"], snap["dist"]
        s = graph.segments[sid]
        # Snap to an endpoint when the projection is essentially on it (avoids a needless
        # zero-length split and keeps degenerate cases as plain junction routing).
        if haversine_m(coord, coords[s.a]) <= snap_weld_m:
            result.append((s.a, dist))
            continue
        if haversine_m(coord, coords[s.b]) <= snap_weld_m:
            result.append((s.b, dist))
            continue
        # Reuse an existing split on this segment at (near) the same position.
        node = None
        for (ppos, pnode) in splits.get(sid, ()):
            if haversine_m(coord, coords[pnode]) <= snap_weld_m:
                node = pnode
                break
        if node is None:
            node = add_node(coord)
            splits.setdefault(sid, []).append((pos, node))
        result.append((node, dist))

    if not splits:
        return graph, result

    # Rebuild the segment list: keep every un-split segment; replace each split segment
    # with its ordered pieces between consecutive cut positions (endpoints included).
    segments: list[Segment] = []
    for sid, s in enumerate(graph.segments):
        cuts = splits.get(sid)
        if not cuts:
            segments.append(s)
            continue
        last = len(s.coords) - 1
        marks: list[tuple[_Pos, int]] = [((0, 0.0), s.a)]
        marks.extend(sorted(cuts, key=lambda pn: pn[0]))
        marks.append(((last - 1, 1.0), s.b))
        for (pa, na), (pb, nb) in zip(marks, marks[1:]):
            piece = _subpolyline(s.coords, pa, pb)
            segments.append(
                Segment(a=na, b=nb, coords=piece, length_m=polyline_length_m(piece), refs=s.refs)
            )

    adj: dict[int, list[int]] = {}
    for idx, s in enumerate(segments):
        adj.setdefault(s.a, []).append(idx)
        if s.b != s.a:
            adj.setdefault(s.b, []).append(idx)
    return TrailGraph(coords=coords, segments=segments, adj=adj), result


def _dijkstra(
    graph: TrailGraph,
    src: int,
    dst: int,
    removed_nodes: frozenset[int] = frozenset(),
    removed_edges: frozenset[int] = frozenset(),
) -> tuple[list[int], list[int], float] | None:
    """Shortest path ``src -> dst`` by segment length, or ``None`` if disconnected.

    Returns ``(segment_ids, node_ids, length_m)`` — the ordered segment ids let the caller
    assemble geometry through the *multigraph* (a node pair alone can't say which of two
    parallel trails was taken). ``removed_nodes`` / ``removed_edges`` (edges are segment ids)
    are Yen's spur exclusions. Deterministic: the heap breaks ties by node id, neighbours are
    scanned in sorted segment-id order, and relaxation keeps the first (strict ``<``) path, so
    equal-length alternatives resolve the same way every run.
    """
    import heapq

    dist = {src: 0.0}
    prev: dict[int, tuple[int, int]] = {}
    heap: list[tuple[float, int]] = [(0.0, src)]
    done: set[int] = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        if u == dst:
            break
        for sid in sorted(graph.adj.get(u, ())):
            if sid in removed_edges:
                continue
            s = graph.segments[sid]
            v = s.b if s.a == u else s.a
            if v in removed_nodes or v in done:
                continue
            nd = d + s.length_m
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                prev[v] = (u, sid)
                heapq.heappush(heap, (nd, v))
    if dst not in dist:
        return None
    segs: list[int] = []
    nodes = [dst]
    cur = dst
    while cur != src:
        pu, sid = prev[cur]
        segs.append(sid)
        nodes.append(pu)
        cur = pu
    segs.reverse()
    nodes.reverse()
    return segs, nodes, dist[dst]


def k_shortest_paths(
    graph: TrailGraph,
    src: int,
    dst: int,
    *,
    k: int,
    overlap_frac: float = 0.6,
    max_m: float = math.inf,
    max_candidates: int = 200,
) -> list[ComposedLoop]:
    """The ``k`` shortest *distinct* routes ``src -> dst``, shortest first (Yen's algorithm).

    Yen enumerates loopless paths in non-decreasing length. Literal k-shortest paths tend to
    be the same line ± one segment, which isn't "several routes between two points" — so a
    candidate that re-uses more than ``overlap_frac`` of its length from an already-kept route
    is skipped (the same near-duplicate rule ``find_loops`` uses), and Yen is pulled until
    ``k`` *distinct* routes are kept (or it runs dry / hits ``max_candidates``). ``max_m`` caps
    a route's length. Each route is assembled with :func:`_assemble` (which needs only an
    ordered segment list from a start node, so it serves open paths as well as loops).
    """
    if src == dst or k <= 0:
        return []
    first = _dijkstra(graph, src, dst)
    if first is None or first[2] > max_m:
        return []

    seg_len = {i: s.length_m for i, s in enumerate(graph.segments)}

    def path_len(segs: list[int]) -> float:
        return sum(seg_len[i] for i in segs)

    A: list[tuple[list[int], list[int]]] = [(first[0], first[1])]  # accepted, shortest-first
    A_keys = {tuple(first[0])}
    B: list[tuple[float, list[int], list[int]]] = []  # candidate (length, segs, nodes)
    B_keys: set[tuple[int, ...]] = set()
    kept: list[ComposedLoop] = [_assemble(graph, src, first[0])]

    def overlaps(segs: list[int], length: float) -> bool:
        s = set(segs)
        for K in kept:
            shared = sum(seg_len.get(i, 0.0) for i in (s & K.seg_ids))
            if length > 0 and shared / length > overlap_frac:
                return True
        return False

    while len(kept) < k and len(A) < max_candidates:
        prev_segs, prev_nodes = A[-1]
        for i in range(len(prev_nodes) - 1):
            root_nodes = prev_nodes[: i + 1]
            root_segs = prev_segs[:i]
            removed_edges = set()
            for p_segs, p_nodes in A:
                if len(p_segs) > i and p_nodes[: i + 1] == root_nodes:
                    removed_edges.add(p_segs[i])  # ban the edge each known path took here
            removed_nodes = frozenset(root_nodes[:-1])  # root minus the spur node
            spur = _dijkstra(graph, root_nodes[-1], dst, removed_nodes, frozenset(removed_edges))
            if spur is None:
                continue
            total_segs = root_segs + spur[0]
            key = tuple(total_segs)
            if key in A_keys or key in B_keys:
                continue
            total_len = path_len(total_segs)
            if total_len > max_m:
                continue
            total_nodes = root_nodes + spur[1][1:]
            B.append((total_len, total_segs, total_nodes))
            B_keys.add(key)
        if not B:
            break
        # Move the best candidate to the accepted list; keep it if it's a distinct route.
        B.sort(key=lambda c: (round(c[0], 3), tuple(c[1])))
        _, best_segs, best_nodes = B.pop(0)
        B_keys.discard(tuple(best_segs))
        A.append((best_segs, best_nodes))
        A_keys.add(tuple(best_segs))
        if not overlaps(best_segs, path_len(best_segs)):
            kept.append(_assemble(graph, src, best_segs))
    return kept[:k]
