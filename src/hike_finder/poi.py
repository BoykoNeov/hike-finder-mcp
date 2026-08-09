"""Points of interest a hike can reach — churches, ruins, peaks, viewpoints…

The feature this serves: "find me a 10 km / 400 m hike that goes past a ruin."
Distance and gain were already queryable; this adds the *destination* dimension.

Three pieces, all pure and network-free (the project's trust-anchor convention):

  * :data:`POI_KINDS` — the registry. ONE table that is the single source of truth
    for BOTH the Overpass selectors (``overpass.build_query``) and the classifier
    (:func:`classify`). Deriving the query and the parser from the same table is
    deliberate: written independently they drift, and a kind that is *fetchable but
    unclassifiable* (or the reverse) fails as a silently-empty result set rather than
    an error — the same drift hazard ``access.matched_access_points`` and the shared
    ``geometry._vertex_graph`` are built to remove. ``test_poi.py`` asserts the
    round-trip for every registered kind.
  * :class:`PoiIndex` — a cell grid over an area's POIs, so "does this route pass a
    church?" costs ~O(route points) instead of O(route points × POIs). Built ONCE per
    search (POIs are few and constant across the whole ``find_hikes`` call) and reused
    for every route.
  * :func:`route_pois` — the predicate itself: which registered POIs lie within
    ``radius_m`` of any point of a route's geometry, and how far away.
  * :func:`select_pois` — the *inventory*: every registered object of the chosen kinds in
    an area, with no route in the picture at all ("show me the ruins around here"). Same
    registry, same labels, so the browse and the filter can never name a kind differently.

Proximity, not termination: a route "reaches" a POI when it passes within the radius,
which is measured and reported. Requiring the route to *end* at the POI was considered
and rejected — a marked KČT relation almost never ends at a church, it passes it, so an
end-anchored filter would return near-nothing and read as broken. ``access.py`` already
set this precedent for loops ("a loop has no meaningful end" → test the whole line); it
applies more strongly to a POI. Because the measured distance rides along on every hit,
"ends at" stays readable from the output.

Honesty note, same register as ``access.py``: a POI hit means "OSM maps an object of
that kind within the radius of this route's geometry". No hit means nothing of that kind
is *mapped* nearby — not that nothing is there. And proximity is measured to the route's
mapped VERTICES, not to the continuous line, so a POI sitting off the middle of one long
straight member way can read farther than it walks (see HANDOFF's limitations).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .geometry import EARTH_RADIUS_M, Coord, haversine_m, project_on_polyline

# Metres per degree of latitude, derived from the SAME sphere ``haversine_m`` uses
# (≈111 195, not the 111 320 of the WGS84-ish figure used for bbox padding elsewhere).
# It has to agree with the metric the grid's results are checked against, or a cell is
# very slightly smaller than it claims and a POI can sit in the sliver between "one cell
# away" and "within the radius".
_M_PER_DEG_LAT = EARTH_RADIUS_M * math.pi / 180.0

# Cosine floor for longitude scaling, mirroring ``access._bbox_pad``: keeps the
# projection finite near the poles instead of dividing by ~0.
_COS_FLOOR = 0.05

# Cells are built this much larger than the search radius. Sizing them at *exactly* the
# radius leaves the 3x3 neighbourhood correct only up to the second-order difference
# between the flat projection and the true great-circle distance (and floating point).
# A 5 % margin buries both with no measurable cost — the neighbourhood is still 3x3.
_CELL_MARGIN = 1.05


@dataclass(frozen=True)
class PoiKind:
    """One selectable kind of object: its OSM tag, and how to say it in English."""

    key: str  # OSM tag key, e.g. "historic"
    values: tuple[str, ...]  # accepted values for that key
    label: str  # singular human label, e.g. "ruin"
    plural: str  # human label for the UI list, e.g. "ruins"


# The registry. Every entry is fetched on EVERY area query (see overpass.build_query),
# so a snapshot always carries POIs and an offline POI search works by construction —
# the alternative (fetch only what was asked for) gives two Overpass cache keys and
# snapshots that only sometimes contain POIs, which breaks offline == online.
#
# Kept to landscape/heritage/refreshment objects a walk is actually planned around. Each
# kind maps to exactly ONE tag key so the query stays one compact regex per key; a
# concept spanning two keys is registered as two kinds rather than complicating the
# table.
POI_KINDS: dict[str, PoiKind] = {
    "church": PoiKind("amenity", ("place_of_worship",), "church", "churches & chapels"),
    "shrine": PoiKind(
        "historic", ("wayside_shrine", "wayside_cross"), "shrine", "wayside shrines & crosses"
    ),
    "ruins": PoiKind("historic", ("ruins",), "ruin", "ruins"),
    "castle": PoiKind("historic", ("castle", "fort"), "castle", "castles & forts"),
    "memorial": PoiKind("historic", ("memorial", "monument"), "memorial", "memorials"),
    "archaeology": PoiKind(
        "historic", ("archaeological_site",), "archaeological site", "archaeological sites"
    ),
    "peak": PoiKind("natural", ("peak",), "peak", "summits"),
    "rock": PoiKind("natural", ("rock", "arch", "stone"), "rock", "rock formations"),
    "cave": PoiKind("natural", ("cave_entrance",), "cave", "caves"),
    "spring": PoiKind("natural", ("spring",), "spring", "springs"),
    "waterfall": PoiKind("waterway", ("waterfall",), "waterfall", "waterfalls"),
    "viewpoint": PoiKind("tourism", ("viewpoint",), "viewpoint", "viewpoints"),
    "tower": PoiKind("man_made", ("tower",), "tower", "lookout towers"),
    "museum": PoiKind("tourism", ("museum",), "museum", "museums"),
    "hut": PoiKind(
        "tourism", ("alpine_hut", "wilderness_hut"), "mountain hut", "mountain huts"
    ),
    "shelter": PoiKind("amenity", ("shelter",), "shelter", "shelters"),
    "picnic": PoiKind("tourism", ("picnic_site",), "picnic site", "picnic sites"),
    "refreshment": PoiKind(
        "amenity", ("pub", "restaurant", "cafe"), "pub/restaurant", "pubs & restaurants"
    ),
}


# Registry position per kind. The ONE deterministic order every listing sorts by, so a
# printed inventory comes out in the same order as the `--list-poi-kinds` menu the user
# just read. Derived from the table, never written out, like everything else here.
_KIND_ORDER: dict[str, int] = {kind: i for i, kind in enumerate(POI_KINDS)}


def kind_labels() -> list[tuple[str, str]]:
    """``(kind, plural label)`` for every registered kind, for the CLI/web/MCP lists."""
    return [(k, v.plural) for k, v in POI_KINDS.items()]


def kind_label(kind: str, *, plural: bool = False) -> str:
    """The human label for a registered kind, falling back to the raw kind.

    The SINGLE lookup both :class:`PoiHit` and :class:`PoiPlace` render through. Two
    types each doing their own ``POI_KINDS.get(...)`` is the same drift hazard the
    query/classifier pair is built to avoid — a kind relabelled in the table has to
    change everywhere it is shown, or the same object reads two different ways in two
    frontends.
    """
    spec = POI_KINDS.get(kind)
    if spec is None:
        return kind
    return spec.plural if plural else spec.label


def selectors_by_key() -> dict[str, tuple[str, ...]]:
    """``tag key -> every accepted value`` across the registry, de-duplicated and
    sorted — the exact set ``overpass.build_query`` turns into one regex per key.

    Derived from :data:`POI_KINDS`, never written out by hand, so a kind added to the
    registry is fetched automatically and can't become classifiable-but-unfetchable.
    """
    by_key: dict[str, set[str]] = {}
    for spec in POI_KINDS.values():
        by_key.setdefault(spec.key, set()).update(spec.values)
    return {key: tuple(sorted(values)) for key, values in sorted(by_key.items())}


def classify(tags: dict) -> str | None:
    """The registered kind an OSM element belongs to, or ``None``.

    The inverse of :func:`selectors_by_key` over the SAME table, so anything the query
    fetches is classifiable and anything classifiable is fetched. First match wins;
    kinds keyed on different tags can't collide, and within a key the registered value
    sets are disjoint.
    """
    if not tags:
        return None
    for kind, spec in POI_KINDS.items():
        if tags.get(spec.key) in spec.values:
            return kind
    return None


def normalise_kinds(kinds) -> tuple[str, ...]:
    """Validate a user-supplied kind list, preserving order and dropping duplicates.

    Raises ``ValueError`` naming the offender (and the valid set) on an unknown kind,
    so a typo is a loud error rather than a silently-empty result — the one outcome
    this project's conventions forbid.
    """
    out: list[str] = []
    for raw in kinds or ():
        kind = str(raw).strip().lower()
        if not kind:
            continue
        if kind not in POI_KINDS:
            raise ValueError(
                f"unknown point-of-interest kind {kind!r} — pick from: "
                + ", ".join(sorted(POI_KINDS))
            )
        if kind not in out:
            out.append(kind)
    return tuple(out)


@dataclass(frozen=True)
class PoiHit:
    """A registered object a route passes, with the measured distance to it."""

    kind: str
    name: str | None
    coord: Coord
    distance_m: float

    @property
    def label(self) -> str:
        return kind_label(self.kind)

    def describe(self) -> str:
        """``church “St. Peter” (120 m)`` — the shared one-line rendering."""
        named = f' “{self.name}”' if self.name else ""
        return f"{self.label}{named} ({round(self.distance_m)} m)"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "name": self.name,
            "lat": self.coord[0],
            "lon": self.coord[1],
            "distance_m": round(self.distance_m, 1),
        }


@dataclass(frozen=True)
class PoiPlace:
    """One registered object in an area, on its own — no route, no distance.

    Deliberately NOT a :class:`PoiHit` with ``distance_m=0``. Every distance in this
    module answers "how close does a route come?", and this mode has no route to measure
    against; a zero would render as ``ruin “Zřícenina” (0 m)`` and read as *touching the
    trail*, which is a claim nobody made. Two small types beat one type that lies. They
    share :func:`kind_label`, so the two can't disagree about what a kind is called.
    """

    kind: str
    name: str | None
    coord: Coord

    @property
    def label(self) -> str:
        return kind_label(self.kind)

    def describe(self) -> str:
        """``ruin “Nístějka” — 50.6821, 15.5533`` — the shared one-line rendering."""
        named = f' “{self.name}”' if self.name else ""
        return f"{self.label}{named} — {self.coord[0]:.5f}, {self.coord[1]:.5f}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "name": self.name,
            "lat": self.coord[0],
            "lon": self.coord[1],
        }


def select_pois(pois: list[dict], kinds=()) -> tuple[PoiPlace, ...]:
    """Every object of ``kinds`` in an area's POI list — the inventory, not a filter.

    ``pois`` is ``AreaData.pois`` (live or from a snapshot, identically shaped, which is
    what makes offline == online here free rather than argued). ``kinds`` is validated
    through :func:`normalise_kinds`, so a typo raises instead of quietly listing nothing.

    **An empty ``kinds`` means every registered kind**, expanded here and never passed on
    as a bare ``()``. That expansion is explicit because the opposite convention already
    exists three functions down: :func:`route_pois` treats ``()`` as *match nothing*, and
    the two readings differ by an entire result set. "Show me what's here" with nothing
    selected is a browse, not an empty question.

    NOT clipped to any bounding box. The list is what Overpass returned for the fetched
    area, and a large object straddling the edge (a monastery way, an archaeological area)
    has its ``out center`` representative point wherever its centroid lands — possibly just
    outside the box it genuinely intersects. Over-showing by a few metres is visible and
    self-explanatory on a map; dropping a real object because of a centroid is the silent
    failure this project's conventions forbid. ``routes_to_poi`` reads ``area.pois`` the
    same unclipped way.

    Ordered by registry position, then name, then coordinate — so an inventory comes out
    grouped the way the kind menu lists them, and identically on every run. Each real-world
    object appears once (``parse_area`` already de-dupes by element identity; the coord/kind
    key here also covers a snapshot hand-assembled from two sources).
    """
    wanted = normalise_kinds(kinds) or tuple(POI_KINDS)
    allowed = frozenset(wanted)
    seen: set[tuple[Coord, str]] = set()
    out: list[PoiPlace] = []
    for p in pois or ():
        kind = p.get("kind")
        if kind not in allowed:
            continue
        coord = p.get("coord")
        if coord is None:
            continue
        coord = (coord[0], coord[1])
        key = (coord, kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(PoiPlace(kind=kind, name=p.get("name"), coord=coord))
    out.sort(key=lambda q: (_KIND_ORDER.get(q.kind, len(_KIND_ORDER)), q.name or "", q.coord))
    return tuple(out)


def count_by_kind(places) -> list[tuple[str, int]]:
    """``(kind, count)`` for a selection, in registry order — the inventory's summary.

    Kinds with no objects are omitted: "0 caves" in a list of what IS here is noise, and
    the honest "nothing of that kind is mapped" line belongs to the empty-result path,
    where it can also say what to do about it.
    """
    counts: dict[str, int] = {}
    for p in places:
        counts[p.kind] = counts.get(p.kind, 0) + 1
    return sorted(
        counts.items(), key=lambda kv: (_KIND_ORDER.get(kv[0], len(_KIND_ORDER)), kv[0])
    )


class PoiIndex:
    """A cell grid over an area's POIs: "anything of these kinds near this point?"

    Built ONCE per search and shared by every route, because the POIs are a property of
    the fetched area while the route points are not — the reverse (index each route's
    points) would rebuild the structure per route for no gain.

    Cells are ``cell_m`` on a side. Latitude maps to cells exactly (degrees of latitude
    are uniform); longitude is scaled by the cosine of ``worst_lat`` — the HIGHEST
    absolute latitude in play, so the cosine used is the SMALLEST and every cell is at
    least ``cell_m`` wide in true metres across the area. That is the same
    worst-case-cosine reasoning as ``access._bbox_pad``, and for the same reason: the
    grid must never drop a feature that is genuinely in range. As a second belt,
    :meth:`near` computes the longitude cell span from the QUERY point's own latitude,
    so a query north of every indexed POI simply widens its neighbourhood instead of
    silently missing a hit. ``test_poi.py`` pins the whole thing against brute force.
    """

    def __init__(self, pois: list[dict], cell_m: float, worst_lat: float | None = None):
        # ``cell_m`` is the search radius the index will be queried at; the stored cell
        # is a touch larger (see _CELL_MARGIN) so one cell always covers one radius.
        self.cell_m = max(float(cell_m), 1.0) * _CELL_MARGIN
        lats = [abs(p["coord"][0]) for p in pois] if pois else []
        if worst_lat is not None:
            lats.append(abs(worst_lat))
        self._worst_lat = max(lats) if lats else 0.0
        self._cos_ref = max(_COS_FLOOR, math.cos(math.radians(self._worst_lat)))
        self._dlat = self.cell_m / _M_PER_DEG_LAT
        self._dlon = self.cell_m / (_M_PER_DEG_LAT * self._cos_ref)
        self._cells: dict[tuple[int, int], list[dict]] = {}
        self._kinds: set[str] = set()
        for p in pois:
            kind = p.get("kind")
            if kind is None:
                continue
            self._kinds.add(kind)
            lat, lon = p["coord"]
            self._cells.setdefault(self._cell(lat, lon), []).append(p)

    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return (math.floor(lat / self._dlat), math.floor(lon / self._dlon))

    @property
    def kinds(self) -> set[str]:
        """Which kinds are actually present in the indexed area."""
        return set(self._kinds)

    def __len__(self) -> int:
        return sum(len(v) for v in self._cells.values())

    def near(
        self, point: Coord, radius_m: float, kinds: frozenset[str] | None = None
    ) -> list[tuple[dict, float]]:
        """Every indexed POI of ``kinds`` within ``radius_m`` of ``point``, with distance.

        Scans only the neighbouring cells: one row of cells each way in latitude (a cell
        is at least ``cell_m >= radius_m`` tall), and as many columns as ``radius_m``
        genuinely spans at this point's latitude — normally one, more only if the query
        sits at a higher latitude than the index was built for.
        """
        lat, lon = point
        ci, cj = self._cell(lat, lon)
        span_i = max(1, math.ceil(radius_m / self.cell_m))
        # Degrees of longitude that radius_m covers HERE, in cells.
        cos_here = max(_COS_FLOOR, math.cos(math.radians(lat)))
        dlon_needed = radius_m / (_M_PER_DEG_LAT * cos_here)
        span_j = max(1, math.ceil(dlon_needed / self._dlon))
        out: list[tuple[dict, float]] = []
        for i in range(ci - span_i, ci + span_i + 1):
            for j in range(cj - span_j, cj + span_j + 1):
                for p in self._cells.get((i, j), ()):
                    if kinds is not None and p.get("kind") not in kinds:
                        continue
                    d = haversine_m(point, p["coord"])
                    if d <= radius_m:
                        out.append((p, d))
        return out


def _probe_points(line: list[Coord], step_m: float) -> list[Coord]:
    """``line``'s vertices plus interpolated points, so no two are more than ``step_m``
    apart along the way.

    Needed because OSM node spacing is arbitrary: a dead-straight member way is often
    mapped with just its two end nodes, kilometres apart. Probing only the vertices
    would then answer "how close does this route come to the church?" with the distance
    to a node far up the trail. The probes exist purely to interrogate the grid — the
    distance finally reported is measured against the real line, below.
    """
    out: list[Coord] = [line[0]]
    for a, b in zip(line, line[1:]):
        d = haversine_m(a, b)
        if d > step_m:
            for k in range(1, int(d // step_m) + 1):
                f = (k * step_m) / d
                if f < 1.0:
                    out.append((a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])))
        out.append(b)
    return out


def route_pois(
    ways: list[list[Coord]],
    index: PoiIndex,
    kinds: tuple[str, ...],
    radius_m: float,
) -> tuple[PoiHit, ...]:
    """The registered objects of ``kinds`` this route passes, nearest first.

    ``ways`` is the route's RAW member ways — not the stitched line — for the same reason
    ``geometry.total_way_length_m`` sums the members: ``stitch_ways`` silently drops
    members it can't chain, so a stitched line would hide a church on a dropped leg.

    Distance is to the trail LINE, not to its mapped vertices. Two stages:

      1. *Candidates*, cheaply. Walk each way in steps of ``radius_m`` (see
         :func:`_probe_points`) and ask the grid for anything within ``1.5 x radius`` of
         each probe. Since every point of the line is within half a step of some probe,
         that radius provably catches every object within ``radius_m`` of the line — it
         over-collects, never under-collects. Cost is ~length/radius lookups, which for a
         10 km route at 250 m is forty, regardless of how densely the way is mapped.
      2. *The real distance*, exactly. Each candidate is projected onto the way with
         ``geometry.project_on_polyline`` — the same primitive the routing modes snap
         picked points with — and kept only if it truly lies within ``radius_m``.

    Each distinct object is reported once, at its CLOSEST approach over all member ways,
    so a route running past a viewpoint for a kilometre doesn't list it twice. Identity
    is the object's coordinate (Overpass gives one representative coord per element).
    """
    if not ways or not kinds:
        return ()
    wanted = frozenset(kinds)
    if not (wanted & index.kinds):
        return ()  # nothing of the requested kinds is mapped in this area at all
    step = max(radius_m, 1.0)
    probe_radius = radius_m * 1.5  # radius + half a step: covers the gaps between probes
    best: dict[tuple[Coord, str], tuple[dict, float]] = {}
    for way in ways:
        if len(way) < 2:
            # A degenerate one-node member: nothing to project onto, so measure directly.
            if len(way) == 1:
                for p, d in index.near(way[0], radius_m, wanted):
                    key = (p["coord"], p["kind"])
                    if key not in best or d < best[key][1]:
                        best[key] = (p, d)
            continue
        candidates: dict[tuple[Coord, str], dict] = {}
        for probe in _probe_points(way, step):
            for p, _d in index.near(probe, probe_radius, wanted):
                candidates[(p["coord"], p["kind"])] = p
        for key, p in candidates.items():
            d, _edge, _frac = project_on_polyline(way, p["coord"])
            if d <= radius_m and (key not in best or d < best[key][1]):
                best[key] = (p, d)
    hits = [
        PoiHit(kind=p["kind"], name=p.get("name"), coord=p["coord"], distance_m=d)
        for p, d in best.values()
    ]
    # Nearest first, then by kind/name so the order is deterministic on ties.
    hits.sort(key=lambda h: (h.distance_m, h.kind, h.name or ""))
    return tuple(hits)
