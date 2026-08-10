"""Render a measured Hike — shared by every frontend (CLI, web UI, MCP server).

Keeping the one-line summary in one place means the terminal CLI and the MCP
server print *identically*, and the web UI serialises the same fields. No logic
here beyond presentation.

The same applies to the *inventory* mode's output (:func:`format_poi`,
:func:`format_poi_summary`): a listed church reads the same in all three frontends.
"""
from __future__ import annotations

from .access import transit_label
from .ferrata import summary_label as ferrata_label
from .filters import Hike
from .poi import count_by_kind, kind_label


def format_poi(p) -> str:
    """The canonical one-line summary of a listed point of interest.

    Thin on purpose — :meth:`poi.PoiPlace.describe` owns the wording so the terminal, the
    web list and the MCP text output can't drift, exactly as ``PoiHit.describe`` does for
    the objects a route passes.
    """
    return p.describe()


def format_poi_summary(places) -> str:
    """``43 objects: 12 places of worship, 8 viewpoints, …`` — the inventory's header.

    A count per kind, in registry order, because the first question about a list of
    ninety pins is what the mix is. Returns the honest empty phrasing for an empty
    selection; the *why* and the lever to pull belong to the caller's empty message.
    """
    total = len(places)
    if not total:
        return "no points of interest"
    # Singular label for a count of one ("1 ruin", not "1 ruins") — the plural label is a
    # category name and reads wrong against a count.
    mix = ", ".join(
        f"{n} {kind_label(kind, plural=n != 1)}" for kind, n in count_by_kind(places)
    )
    return f"{total} object{'s' if total != 1 else ''}: {mix}"


def _surface_to_dict(s) -> dict | None:
    """Serialise a SurfaceSummary, or None when nothing was ever measured."""
    if s is None:
        return None
    return {
        "coverage": round(s.coverage, 3),
        "shares": [
            {"value": sh.value, "label": sh.label, "fraction": round(sh.fraction, 3)}
            for sh in s.shares
        ],
    }


def _ferrata_to_dict(f) -> dict | None:
    """Serialise a FerrataSummary, or None when the data could not say.

    ``length_m``/``fraction`` stay in the payload even at 0 on a route flagged
    ``present`` — that pairing (cabled, extent unmeasured) is a relation-level claim,
    and a consumer that drops the flag because the extent is 0 would lose exactly the
    routes mapped that way.
    """
    if f is None:
        return None
    return {
        "present": f.present,
        "length_m": round(f.length_m),
        "fraction": round(f.fraction, 3),
        # Raw OSM values, unordered and unbucketed — mixed scales, see ferrata.py.
        "grades": list(f.grades),
        "relation_tagged": f.relation_tagged,
        "label": ferrata_label(f),
    }


def format_hike(h: Hike) -> str:
    """The canonical one-line summary of a hike.

    A near-miss is prefixed with ``~`` and gets a trailing ``[near miss: ...]`` clause
    spelling out how it falls short, so it reads as "close, but not a match".
    """
    flags = ["loop" if h.circular else "one-way"]
    if h.car_access:
        flags.append("car")
    if h.chairlift_access:
        flags.append(f"lift:{h.lift_type}")
    # Only a positive transit result gets a flag, and it NAMES the kind: "train station"
    # and "bus stop" are very different promises about actually getting there. False and
    # "never recorded" (None) both stay silent — the one-liner has no room to explain the
    # difference, and printing a bare "no transit" would assert something a pre-transit
    # snapshot cannot support.
    if h.transit_access:
        flags.append(f"transit:{transit_label(h.transit_type) or h.transit_type}")
    # Two gates, because they catch different lies. COVERAGE guards against speaking
    # for a route that is mostly untagged — "ground" off 8 % of the length is a fact
    # about three slivers, not about the walk. DOMINANCE guards against a plurality
    # posing as an answer: `surface:grass 21%` reads as "this is a grass walk" when
    # 79 % of it is something else, so a route with no real majority says `mixed` and
    # names nothing. The full breakdown stays in `hike_to_dict` either way.
    if h.surface is not None and h.surface.coverage >= 0.5 and h.surface.dominant:
        dom = h.surface.dominant
        if dom.fraction >= 0.4:
            flags.append(f"surface:{dom.label} {round(dom.fraction * 100)}%")
        else:
            flags.append(f"surface:mixed ({round(h.surface.coverage * 100)}% known)")
    # Cabled climbing. FIRST among the report-only flags and gated on nothing: this is
    # the one flag whose whole purpose is to survive a route where it describes a small
    # minority of the metres. The surface gates directly above are the opposite policy
    # for the opposite reason — see ferrata.py on why a hazard inverts the dominance
    # rule. Silent when absent AND when clean, so its absence is never a safety claim.
    ferrata_flag = ferrata_label(h.ferrata)
    if ferrata_flag:
        flags.append(ferrata_flag)
    if h.gain_m is not None:
        elev = f"+{h.gain_m} m / -{h.loss_m} m"
    else:
        elev = "gain n/a"
    prefix = "~ " if h.near_miss else ""
    suffix = f"  [near miss: {'; '.join(h.notes)}]" if h.near_miss and h.notes else ""
    # What the route actually reaches, nearest first — the answer to "does this one go
    # past a ruin?". Only ever populated when a POI filter was set, so the ordinary
    # one-line summary is byte-for-byte unchanged.
    if h.pois:
        suffix += "  [passes " + "; ".join(p.describe() for p in h.pois) + "]"
    # A route DRAWN TO an object (--to-poi) ends at the nearest point on the trail
    # network, which is not the object itself — say "ends N m from" and never "arrives
    # at", the same register access.py uses for "nothing of that kind is *mapped* here".
    if h.destination is not None:
        suffix += (
            f"  [ends {round(h.destination.distance_m)} m from the {h.destination.label}]"
        )
    # A composed loop has no single OSM relation — name its constituent trails instead
    # of a (dishonest) relation id, so it always reads as a stitched-together suggestion.
    # An UNNAMED route given a reverse-geocoded place label shows that label as its
    # name, but the identifier clause says "unnamed OSM relation" so a place-derived
    # label is never mistaken for the route's signed trail name (which it has none of).
    if h.composed:
        ident = f"composed of {' + '.join(h.composed_of)}" if h.composed_of else "composed loop"
        display_name = h.name
    elif h.place_name:
        ident = f"unnamed OSM relation {h.osm_id}"
        display_name = h.place_name
    else:
        ident = f"OSM relation {h.osm_id}"
        display_name = h.name
    return (
        f"{prefix}{display_name} — {h.distance_km} km, {elev} [{', '.join(flags)}] "
        f"(start {h.start[0]:.4f},{h.start[1]:.4f}, {ident}){suffix}"
    )


def hike_to_dict(h: Hike, *, geometry: bool = False) -> dict:
    """JSON-serialisable view of a hike (for CLI --json and the web UI).

    ``geometry=True`` adds a ``geometry`` key — the member ways as ``[lat, lon]``
    polylines (the project's lat/lon order, ready for Leaflet's ``L.polyline``; NOT
    GeoJSON's ``[lon, lat]``). It is opt-in so the default summary stays lean: the CLI
    ``--json`` keeps its compact shape and the web map opts in only when it needs to
    draw the lines.
    """
    d = {
        # A composed loop carries no single OSM relation id — expose None and list its
        # constituent trails in `composed_of` instead.
        "osm_id": None if h.composed else h.osm_id,
        "name": h.name,
        "ref": h.ref,
        # `name`/`ref` stay the truthful OSM values (route/<id> when unnamed); a
        # reverse-geocoded label, when present, is exposed separately so a client can
        # show it without losing the provenance that the route is `unnamed`.
        "unnamed": h.unnamed,
        "place_name": h.place_name,
        "distance_km": h.distance_km,
        "gain_m": h.gain_m,
        "loss_m": h.loss_m,
        "circular": h.circular,
        "car_access": h.car_access,
        "chairlift_access": h.chairlift_access,
        "lift_type": h.lift_type,
        # null (not false) when the area's data never recorded transit — a JSON consumer
        # must be able to tell "none mapped" from "not measured".
        "transit_access": h.transit_access,
        "transit_type": h.transit_type,
        "transit_label": transit_label(h.transit_type),
        # null when member-way tags were never fetched (a pre-feature snapshot);
        # `coverage` is the fraction of the route's length the breakdown is based on,
        # so a consumer can weigh it instead of trusting a bare dominant value.
        "surface": _surface_to_dict(h.surface),
        "tracktype": _surface_to_dict(h.tracktype),
        # Cabled climbing. `null` when the data could not answer; an OBJECT with
        # `present: false` when every member way was read and none is cabled. A consumer
        # that needs "checked and clear" must test `present is False` — the two are kept
        # apart here precisely so it can, rather than both collapsing to a falsy value.
        "ferrata": _ferrata_to_dict(h.ferrata),
        "start": {"lat": h.start[0], "lon": h.start[1]},
        "near_miss": h.near_miss,
        "notes": list(h.notes),
        "composed": h.composed,
        "composed_of": list(h.composed_of),
        # Reached points of interest (empty unless a POI filter was set). Each carries
        # its own coordinate so a client can pin it without a second lookup.
        "pois": [p.to_dict() for p in h.pois],
        # The object this route was drawn TO (--to-poi), distinct from the ones it merely
        # passes; `distance_m` is how far its end sits from the object. None otherwise, so
        # every other search's JSON gains one null key and changes in no other way.
        "destination": h.destination.to_dict() if h.destination else None,
    }
    if geometry:
        d["geometry"] = [[[lat, lon] for lat, lon in way] for way in h.ways]
    return d
