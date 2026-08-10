"""Cabled climbing — via ferrata / klettersteig — detected from OSM tags.

A via ferrata is a route equipped with fixed steel cable, rungs and ladders. You walk
it in a harness clipped to the cable. It is not a hard hike; it is a different
activity, and rendering one beside a forest stroll with nothing but a distance and a
gain figure is the single most consequential thing this app could get wrong.

Two questions, and they are NOT symmetric:

  * **Find them** (``--ferrata``) — a target search. Tolerates false negatives: miss
    one and the user still gets a list.
  * **Avoid them** (``--no-ferrata``) — tolerates none. A cabled section that slips
    through the filter is the failure that matters, so avoidance is built to be
    complete over the route universe rather than merely good.

**Why avoidance can be complete.** Every route this app can return is built from the
member ways of ``route=hiking``/``route=foot`` relations — relation routes directly,
synthesised routes (``--compose-loops``, ``--around``, ``--from``/``--to``…) through a
graph assembled from those same members. So a cabled section that is *part of a route
we can hand you* is, by construction, a member way whose tags we already hold. The
residual risk is cabled terrain carrying neither tag below — a mapping gap no query
change can close, and named as such in the docs rather than papered over.

**The predicate is EITHER key, not one with the other as a bonus.** Measured with
``out count`` over two real boxes:

  =====================================  =======  =======
  in the box                             Cortina  Ehrwald
  =====================================  =======  =======
  ``highway=via_ferrata`` ways                46       30
  ``via_ferrata_scale`` ways                  70       68
  ...of those, ``highway=path``               25        —
  ``route=hiking`` relations w/ a grade       13        —
  ``route=via_ferrata`` relations              2        0
  =====================================  =======  =======

``via_ferrata_scale`` is the *dominant* carrier, and in Cortina 25 of the 70 ways
carrying it are tagged ``highway=path`` — ordinary-looking paths with cable on them.
Keying on ``highway=via_ferrata`` alone would miss a third of the graded ways, so the
test is presence of *either*.

**Presence, never dominance.** :mod:`surface` deliberately suppresses a value holding
under 40 % of a route and stays silent under 50 % coverage, because there a plurality
posing as the answer is the lie. A hazard inverts that rule exactly: 300 m of cable on
a 12 km walk is precisely what must not be averaged away. Nothing here is gated on a
share — any tagged metre fires — and the measured length and fraction ride *alongside*
the flag so the caller can say how much, never *instead* of it.

Grades pass through raw. Real data in the two boxes holds ``0``, ``1``, ``1+``, ``2``,
``2+``, ``3``, ``3+``, ``3.5``, ``4``, ``4+``, and OSM carries A–F and word scales
elsewhere; there is no ordering that is safe across those schemes, so this module
reports the distinct values and never computes a maximum or buckets them into
"easy/hard". A reader who sees ``3.5`` can look it up; a reader shown a bucket a
different scale was silently mapped into cannot.

Pure and network-free, per the project's trust-anchor convention.
"""
from __future__ import annotations

from dataclasses import dataclass

from .geometry import Coord, polyline_length_m

# The way-level path type. Secondary to the grade key, not primary — see the module
# docstring's measurement.
FERRATA_HIGHWAY = "via_ferrata"

# The grade key. Carried by ways AND by route relations, which is why both are read.
SCALE_KEY = "via_ferrata_scale"

# `route=via_ferrata` — a relation whose whole reason for existing is the cabled climb.
FERRATA_ROUTE = "via_ferrata"


def scale_of(tags: dict | None) -> str | None:
    """The raw ``via_ferrata_scale`` value, or None. Never normalised — see module doc."""
    value = (tags or {}).get(SCALE_KEY)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def way_is_ferrata(tags: dict | None) -> bool:
    """True if a single way is cabled, by EITHER signal.

    Membership is tested with ``in``, not truthiness: ``via_ferrata_scale=0`` is a real
    grade (the easiest rung of the scale, and it appears twice in the Cortina box). A
    truthiness test would read that as untagged and let the gentlest cabled route pass
    an avoidance filter — the exact class of miss this module exists to prevent.
    """
    tags = tags or {}
    return tags.get("highway") == FERRATA_HIGHWAY or SCALE_KEY in tags


def relation_is_ferrata(tags: dict | None) -> bool:
    """True if a route RELATION declares itself cabled.

    Two ways it can. ``route=via_ferrata`` is the dedicated relation — and it is the
    load-bearing half: in the Cortina box ``rel/19063986`` (*Via Ferrata Lucio Dalaiti*)
    carries no grade at all, so a grade-only test would leave the one object that exists
    *solely* as a ferrata relation looking like an ordinary walk. The other way is a
    ``route=hiking`` relation carrying ``via_ferrata_scale`` itself, which is how 13 of
    Cortina's 212 hiking relations are mapped.
    """
    tags = tags or {}
    return tags.get("route") == FERRATA_ROUTE or SCALE_KEY in tags


@dataclass(frozen=True)
class FerrataSummary:
    """What is cabled about one route, and where that claim came from.

    ``length_m``/``fraction`` may be 0 on a route that IS ferrata: a relation-level
    claim (``route=via_ferrata``, or a grade on the relation) says the route is cabled
    without saying which metres are. That is why ``present`` is its own field rather
    than being derived from a length — deriving it would turn "cabled, extent unknown"
    into "not cabled", which is the one direction this must never fail in.
    """

    present: bool
    length_m: float = 0.0  # summed length of member ways tagged as cabled
    fraction: float = 0.0  # of the route's TOTAL measured length, 0..1
    grades: tuple[str, ...] = ()  # distinct raw scale values, no ordering implied
    relation_tagged: bool = False  # the relation itself carried the claim


def summarise_ferrata(
    members: list[tuple[list[Coord], dict]] | None,
    relation_tags: dict | None = None,
) -> FerrataSummary | None:
    """Measure one route's cabled sections. ``None`` means *we could not look*.

    ``members`` is the ``(coords, tags)`` pairing :mod:`surface` uses — an empty list or
    ``None`` means the data carries no member-way tags (a snapshot predating the
    member-tag fetch). In that case a relation-level claim is still answerable, and is
    still returned; only when the relation says nothing either is the answer ``None``.

    That distinction is the whole not-known contract: ``None`` is "this data cannot
    say", ``present=False`` is "we looked at every member way and none is cabled". A
    caller that collapses the two reports an unexamined route as safe.
    """
    rel_tags = relation_tags or {}
    rel_ferrata = relation_is_ferrata(rel_tags)
    rel_grade = scale_of(rel_tags)

    if not members:
        if rel_ferrata:
            return FerrataSummary(
                present=True,
                grades=(rel_grade,) if rel_grade else (),
                relation_tagged=True,
            )
        return None

    total_m = 0.0
    ferrata_m = 0.0
    grades: set[str] = set()
    for coords, tags in members:
        length = polyline_length_m(coords)
        if length <= 0:
            continue
        total_m += length
        if way_is_ferrata(tags):
            ferrata_m += length
            grade = scale_of(tags)
            if grade:
                grades.add(grade)
    if rel_grade:
        grades.add(rel_grade)

    return FerrataSummary(
        present=ferrata_m > 0 or rel_ferrata,
        length_m=ferrata_m,
        fraction=(ferrata_m / total_m) if total_m > 0 else 0.0,
        # Sorted for a stable rendering only. The order carries NO difficulty meaning:
        # these are raw values off a mixed set of scales (see the module docstring).
        grades=tuple(sorted(grades)),
        relation_tagged=rel_ferrata,
    )


@dataclass(frozen=True)
class FerrataLine:
    """One cabled line in an area, for the *inventory* browse — not a route to walk.

    The counterpart of :class:`poi.PoiPlace`: the objects ARE the answer, nothing is
    measured against a route, and no elevation is looked up. ``source`` keeps the two
    origins apart, because they are different kinds of claim — ``"route"`` is a
    ``route=via_ferrata`` relation somebody assembled and named, ``"way"`` is a single
    cabled way, which may be one pitch of a longer climb rather than a climb in itself.
    """

    name: str | None
    scale: str | None
    length_m: float
    start: Coord
    source: str  # "route" | "way"

    def describe(self) -> str:
        name = self.name or "unnamed via ferrata"
        bits = [name]
        if self.scale:
            bits.append(f"grade {self.scale}")
        bits.append(f"{self.length_m / 1000:.1f} km")
        if self.source == "way":
            # Said out loud: an unnamed 200 m way is very often one pitch of a climb the
            # map never collected into a relation, and listing it as though it were a
            # whole route would overstate what is actually known about it.
            bits.append("single way")
        return f"{bits[0]} ({', '.join(bits[1:])})"


def select_ferrata(
    ferrata_routes: list[dict] | None,
    ferrata_ways: list[dict] | None,
) -> tuple[FerrataLine, ...]:
    """The cabled inventory of one area — dedicated relations first, then single ways.

    Deterministic and unclipped, the two properties ``poi.select_pois`` guarantees and
    for the same reason: the live and offline listings call this identically, so
    "offline == live" is a shared call rather than a claim to be re-verified.

    A way that is ALSO a member of one of the relations is still listed. Suppressing it
    would need a member-id join the ways list does not carry, and the honest failure here
    is to under-report a cabled line, never to over-report one.
    """
    out: list[FerrataLine] = []
    for r in ferrata_routes or ():
        coords = [pt for way in r.get("ways") or () for pt in way]
        if not coords:
            continue
        out.append(
            FerrataLine(
                name=r.get("name"),
                scale=scale_of(r.get("tags")),
                length_m=sum(polyline_length_m(w) for w in r.get("ways") or ()),
                start=coords[0],
                source="route",
            )
        )
    for w in ferrata_ways or ():
        coords = w.get("coords") or []
        if not coords:
            continue
        out.append(
            FerrataLine(
                name=w.get("name"),
                scale=w.get("scale"),
                length_m=polyline_length_m(coords),
                start=coords[0],
                source="way",
            )
        )
    # Relations before ways (the sort key's first term), then longest first — the longer
    # a cabled line, the more likely it is the climb somebody means. Name breaks ties so
    # two identical-length ways keep a stable order between runs.
    return tuple(
        sorted(out, key=lambda f: (f.source != "route", -f.length_m, f.name or ""))
    )


def summary_label(summary: FerrataSummary | None) -> str | None:
    """The one-line flag, or None when there is nothing to say.

    Silent on both ``None`` (we could not look) and ``present=False`` (we looked, it is
    not cabled): a route with no ferrata should read exactly as it did before this
    feature existed. The *absence* of this flag is therefore never a safety claim, and
    the docs say so — a caller that needs "we checked and it is clean" has to read
    ``present is False`` rather than infer it from a missing string.
    """
    if summary is None or not summary.present:
        return None
    parts = ["ferrata"]
    if summary.grades:
        parts.append("/".join(summary.grades))
    # Extent only when it was actually measured. A relation-level claim carries no
    # metres, and printing "0.0 km" for it would read as "barely any", which is the
    # opposite of what an unmeasured extent means.
    if summary.length_m > 0:
        parts.append(f"{summary.length_m / 1000:.1f} km")
    return " ".join(parts)
