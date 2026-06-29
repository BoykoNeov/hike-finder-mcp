"""Human-readable names for UNNAMED routes, derived from place names.

Most OSM hiking relations carry a ``name`` or ``ref``, but some carry neither and
fall back to a synthetic ``route/<id>`` label (see ``overpass.parse_area``). This
module turns such a route into a readable label from the place names at its ends —
e.g. ``"Pec pod Sněžkou → Sněžka"`` or ``"loop near Špindlerův Mlýn"``.

Honesty: a derived label is NOT the route's signed trail name. It never overwrites
``Hike.name``/``ref`` (which stay the truthful OSM values); it is carried separately
in ``Hike.place_name``, and the renderer marks the route as *unnamed* so a geocoded
label is never mistaken for a signed name.

Two layers, both network-free *here*:
  - ``label_endpoints`` / ``compose_label`` are PURE — picking which points to look
    up and assembling the final string. Unit-tested without any network.
  - ``enrich_names`` is glue over an INJECTED ``Geocoder`` (see ``geocode.py``), so
    it is testable with a stub and the actual Nominatim call lives behind the seam.
"""
from __future__ import annotations

from .access import route_endpoints
from .geometry import Coord, haversine_m, route_termini, stitch_ways


def label_endpoints(
    ways, start: Coord | None, circular: bool
) -> tuple[Coord | None, Coord | None]:
    """The ``(start, end)`` points to reverse-geocode for a route's label.

    ``start`` is the route's already-chosen start marker (coupled to a trailhead when
    one is mapped). For a LOOP there is no meaningful far end, so ``end`` is ``None``
    ("loop near <start>"). For a point-to-point route, ``end`` is the route's genuine
    far end: the terminus (degree-1 vertex of the vertex graph) or stitched line end
    FARTHEST from ``start``, tie-broken by coordinate so the choice is deterministic.

    The switch is ``circular`` (not "has termini"): a lollipop has termini yet should
    still read "loop near X", matching ``measure_geometry``'s own circular-based
    reasoning about where a loop has no real end.
    """
    if start is None:
        return None, None
    if circular:
        return start, None
    ways_list = [list(w) for w in ways]
    candidates = list(
        dict.fromkeys(route_termini(ways_list) + route_endpoints(stitch_ways(ways_list)))
    )
    others = [c for c in candidates if c != start]
    if not others:
        return start, None
    # Farthest from start; tie-broken by coordinate for run-to-run determinism (the
    # codebase values deterministic output — cf. _route_start's coordinate tie-break).
    others.sort(key=lambda c: (-haversine_m(start, c), c))
    return start, others[0]


def compose_label(
    start_place: str | None, end_place: str | None, circular: bool
) -> str | None:
    """Assemble the display label from reverse-geocoded place names (each may be None).

    Returns ``None`` when nothing resolved, so the caller keeps the ``route/<id>``
    fallback rather than inventing a name. A point-to-point route whose two ends
    resolve to the *same* place reads "near X", not "X → X".
    """
    s = (start_place or "").strip()
    e = (end_place or "").strip()
    if circular:
        return f"loop near {s}" if s else None
    if s and e and s != e:
        return f"{s} → {e}"
    if s or e:
        return f"near {s or e}"
    return None


def enrich_names(hikes, geocoder) -> int:
    """In place: set ``place_name`` on each UNNAMED, non-composed hike from its
    reverse-geocoded endpoints. Returns the count actually labelled.

    Named/ref'd routes and composed loops are skipped — their identity is already
    honest (a real name, or "composed of …"). A geocode miss leaves ``place_name``
    ``None``, so the route keeps its ``route/<id>`` fallback. The injected
    ``geocoder`` owns throttling/caching, and a point looked up for two routes is
    deduped there, not here.
    """
    labelled = 0
    for h in hikes:
        if not getattr(h, "unnamed", False) or getattr(h, "composed", False):
            continue
        start_pt, end_pt = label_endpoints(h.ways, h.start, h.circular)
        s = geocoder.reverse(start_pt) if start_pt is not None else None
        e = geocoder.reverse(end_pt) if end_pt is not None else None
        label = compose_label(s, e, h.circular)
        if label:
            h.place_name = label
            labelled += 1
    return labelled
