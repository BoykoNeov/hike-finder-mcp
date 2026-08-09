"""What you are actually walking on — length-weighted, from OSM member-way tags.

Gain and distance say how hard a route is to *climb*. They say nothing about whether
it is forest singletrack or four hours of asphalt, which for a lot of walks is the
thing that decides it.

The measurement is **length-weighted, never a count of ways**. OSM splits a trail at
every attribute change, so a route can be ten short asphalt slivers and one long
forest way; counting ways would report that as mostly asphalt. Weighting by metres
answers the question a walker actually asked.

Two values are read, both from the *member ways* (the route relation itself carries
neither — see overpass.py, which has to fetch them with a second `way(r)` statement):

  * ``surface`` — asphalt / gravel / ground / rock…, mapped on ~62 % of member ways
    in a real Krkonoše sample.
  * ``tracktype`` — the grade1..grade5 firmness scale for tracks, ~45 % mapped.

**Coverage is reported, not hidden.** ``sac_scale`` and ``trail_visibility`` were
deliberately left out after measuring them at 4 % and 1 % on the same sample: a
difficulty claim that is absent 96 % of the time reads as "easy" rather than as
"unknown", which is exactly the kind of confident-but-unfounded output this project
avoids. For the same reason every summary here carries the fraction of the route's
length the answer is based on, and a caller that knows nothing reports nothing.

Pure and network-free, per the project's trust-anchor convention.
"""
from __future__ import annotations

from dataclasses import dataclass

from .geometry import Coord, polyline_length_m

# Readable names for the values worth naming. Anything unlisted passes through as its
# raw OSM value rather than being dropped or bucketed — a walker who sees "sett" can
# look it up, whereas a silent re-bucket into "other" destroys the information.
SURFACE_LABELS: dict[str, str] = {
    "asphalt": "asphalt",
    "paved": "paved",
    "concrete": "concrete",
    "paving_stones": "paving stones",
    "sett": "sett (cobbles)",
    "cobblestone": "cobblestone",
    "compacted": "compacted gravel",
    "fine_gravel": "fine gravel",
    "gravel": "gravel",
    "pebblestone": "pebbles",
    "unpaved": "unpaved",
    "ground": "ground",
    "dirt": "dirt",
    "earth": "earth",
    "grass": "grass",
    "sand": "sand",
    "rock": "rock",
    "wood": "boardwalk",
}

# grade1 (firmest) .. grade5 (softest). Spelled out because "grade3" alone tells a
# reader nothing, and the scale runs the opposite way to most people's intuition.
TRACKTYPE_LABELS: dict[str, str] = {
    "grade1": "grade1 (solid)",
    "grade2": "grade2 (mostly solid)",
    "grade3": "grade3 (even mix)",
    "grade4": "grade4 (mostly soft)",
    "grade5": "grade5 (soft)",
}


def surface_label(value: str | None) -> str | None:
    if not value:
        return None
    return SURFACE_LABELS.get(value, value.replace("_", " "))


def tracktype_label(value: str | None) -> str | None:
    if not value:
        return None
    return TRACKTYPE_LABELS.get(value, value)


@dataclass(frozen=True)
class Share:
    """One value's share of the route's measured length."""

    value: str
    label: str
    fraction: float  # 0..1 of the route's TOTAL length, not of the tagged part


@dataclass(frozen=True)
class SurfaceSummary:
    """What a route is made of, and how much of it we actually know.

    ``coverage`` is the fraction of the route's total length carrying the tag at all.
    It is the honesty term: ``shares`` alone would let 200 m of tagged asphalt out of
    10 km speak for the whole walk.
    """

    shares: tuple[Share, ...] = ()  # descending by fraction
    coverage: float = 0.0

    @property
    def dominant(self) -> Share | None:
        return self.shares[0] if self.shares else None


def _summarise(
    members: list[tuple[list[Coord], dict]],
    key: str,
    labeller,
) -> SurfaceSummary:
    total_m = 0.0
    by_value: dict[str, float] = {}
    for coords, tags in members:
        length = polyline_length_m(coords)
        if length <= 0:
            continue
        total_m += length
        value = (tags or {}).get(key)
        if value:
            by_value[value] = by_value.get(value, 0.0) + length
    if total_m <= 0:
        return SurfaceSummary()
    shares = tuple(
        Share(value=v, label=labeller(v), fraction=m / total_m)
        for v, m in sorted(by_value.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return SurfaceSummary(shares=shares, coverage=sum(by_value.values()) / total_m)


def summarise_surface(members: list[tuple[list[Coord], dict]]) -> SurfaceSummary:
    """Length-weighted ``surface`` breakdown over a route's member ways."""
    return _summarise(members, "surface", surface_label)


def summarise_tracktype(members: list[tuple[list[Coord], dict]]) -> SurfaceSummary:
    """Length-weighted ``tracktype`` breakdown over a route's member ways."""
    return _summarise(members, "tracktype", tracktype_label)
