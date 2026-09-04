"""Turn a typed place name into ground to search — the forward half of geocoding.

Every mode in this project ultimately needs either a bbox or a point. Until now the
user had to supply those as numbers: four corners read off openstreetmap.org's Export
tab, or a lat/lon pair for ``--around`` / ``--from`` / ``--to`` / ``--via``. That is a
chore for a person and a hazard for an LLM client, which has no Export tab and will
happily invent coordinates that look right and are forty kilometres out.

This module is the layer between ``geocode.PlaceSearcher`` (one HTTP call) and the
frontends (which want a bbox, a point, and a sentence to show the user). It owns three
decisions, all of which exist so that a named search cannot quietly become a search of
somewhere else:

**Ambiguity is surfaced, never resolved silently.** "Lhota" names dozens of Czech
villages. We fetch several candidates, take the first, and hand the caller *all* of
them plus the index it used, so the frontend can print "match 1 of 5" with the rest
listed and an index flag to pick another. Choosing for the user is fine; choosing
without telling them is not.

**A point-sized place is widened, and the widening is reported.** Nominatim maps a
summit or a hut as a box a few metres across. Searching that literally returns nothing,
with no cause the user can see — the worst kind of empty answer, and the exact shape of
mistake this repo keeps finding (a label promising what the selector never checked). So
an extent below ``cfg.place_min_km`` is grown, per axis, around its own centre, and the
result records what the mapped extent *was* so the frontend can say it widened.

**A lookup failure is an error, not a fallback.** ``geocode.PlaceSearcher.search``
raises when Nominatim could not be reached or understood, and that propagates through
here. There is no default area to fall back to that would not be a lie about what was
searched.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import cache as _cache
from .config import Config
from .geocode import (
    DEFAULT_NOMINATIM_URL,
    GeocodeError,
    NominatimGeocoder,
    PlaceMatch,
    PlaceSearcher,
    search_endpoint_for,
)
from .geometry import Coord, bbox_around, haversine_m

Bbox = tuple[float, float, float, float]


class PlaceNotFound(GeocodeError):
    """Nominatim positively answered "no such place" (a typo, or nothing mapped).

    Distinct from a plain :class:`~hike_finder.geocode.GeocodeError`, which means we
    could not ask — the user's next move is to re-spell in the first case and to retry
    in the second, so the two must not arrive as one message.
    """


@dataclass(frozen=True)
class ResolvedPlace:
    """What a typed name resolved to, and everything needed to say so out loud.

    ``bbox`` is the ground to search: the mapped extent, widened to the floor, or a box
    around the centre when a radius was forced. ``point`` is the same match as a single
    coordinate, for the point-based modes. ``matches`` holds every candidate (including
    the chosen one) so a frontend can list the alternatives it did not take.
    """

    match: PlaceMatch
    bbox: Bbox
    index: int  # 1-based, into ``matches``
    matches: tuple[PlaceMatch, ...]
    extent_km: tuple[float, float]  # (width, height) of ``bbox``
    mapped_extent_km: tuple[float, float] | None  # what OSM mapped; None = not given
    widened: bool
    radius_km: float | None  # set when the caller replaced the extent outright

    @property
    def point(self) -> Coord:
        return self.match.point

    @property
    def label(self) -> str:
        return self.match.label

    @property
    def ambiguous(self) -> bool:
        return len(self.matches) > 1


def place_searcher(cfg: Config, cache=None) -> PlaceSearcher:
    """A forward searcher for ``cfg``, wrapped in the persistent cache when it is on.

    The endpoint doubles as the cache's source key, so pointing the tool at a different
    Nominatim instance cannot serve answers the previous one gave — the same rule the
    elevation cache follows for OpenTopoData datasets sharing a host.
    """
    endpoint = cfg.nominatim_search_url or search_endpoint_for(
        cfg.nominatim_url or DEFAULT_NOMINATIM_URL
    )
    inner = NominatimGeocoder(
        cfg.nominatim_url or DEFAULT_NOMINATIM_URL,
        search_endpoint=endpoint,
        user_agent=cfg.overpass_user_agent,
        min_interval_s=cfg.nominatim_min_interval_s,
    )
    if cache is not None and cfg.geocode_cache_ttl_days > 0:
        return _cache.CachingPlaceSearch(
            cache, endpoint, inner, cfg.geocode_cache_ttl_days * 86400
        )
    return inner


def _extent_km(bbox: Bbox) -> tuple[float, float]:
    """``(width, height)`` of a box in km, measured on the ground.

    Width is taken along the box's middle latitude rather than a corner, so a tall box
    is described by its typical width instead of its widest or narrowest edge.
    """
    south, west, north, east = bbox
    mid_lat = (south + north) / 2.0
    mid_lon = (west + east) / 2.0
    width = haversine_m((mid_lat, west), (mid_lat, east)) / 1000.0
    height = haversine_m((south, mid_lon), (north, mid_lon)) / 1000.0
    return (width, height)


def _widen_to_floor(bbox: Bbox, min_km: float) -> Bbox:
    """Grow each axis of ``bbox``, about its own centre, to at least ``min_km``.

    Per axis on purpose: a long thin valley mapped 0.3 km wide and 6 km long needs its
    width fixed and its length left alone, and squaring it off would search two
    ridgelines the user never named.
    """
    if min_km <= 0:
        return bbox
    south, west, north, east = bbox
    width, height = _extent_km(bbox)
    mid_lat = (south + north) / 2.0
    mid_lon = (west + east) / 2.0
    if height < min_km:
        dlat = (min_km * 1000.0 / 2.0) / 111_320.0
        south, north = mid_lat - dlat, mid_lat + dlat
    if width < min_km:
        dlon = (min_km * 1000.0 / 2.0) / (
            111_320.0 * max(math.cos(math.radians(mid_lat)), 1e-6)
        )
        west, east = mid_lon - dlon, mid_lon + dlon
    return (south, west, north, east)


def resolve_place(
    query: str,
    cfg: Config | None = None,
    *,
    index: int = 1,
    radius_km: float | None = None,
    cache=None,
    limit: int | None = None,
) -> ResolvedPlace:
    """Look ``query`` up and work out the ground it means.

    ``index`` is 1-based and picks among the candidates when a name is ambiguous;
    ``radius_km`` replaces the mapped extent with a box of that radius about the
    centre (so the box is twice that across).

    Raises :class:`PlaceNotFound` when nothing matched, :class:`ValueError` when
    ``index`` is past the end of the candidate list (the message names how many there
    were, since that is what the user needs in order to pick again), and
    :class:`~hike_finder.geocode.GeocodeError` when the lookup itself failed.
    """
    cfg = cfg or Config()
    text = " ".join((query or "").split())
    if not text:
        raise ValueError("a place name is required")
    if cache is None:
        cache = _cache.from_config(cfg)  # None when caching is off; the wrapper is skipped
    want = max(1, int(limit if limit is not None else cfg.place_matches))
    matches = place_searcher(cfg, cache).search(text, limit=want)
    if not matches:
        raise PlaceNotFound(
            f"no place matched {text!r}. Check the spelling, or add the region "
            f"(e.g. 'Lhota, Czechia'); the search covers all of OpenStreetMap."
        )
    if index < 1 or index > len(matches):
        raise ValueError(
            f"--place-index {index} is out of range: {text!r} matched "
            f"{len(matches)} place(s), so the index must be 1..{len(matches)}."
        )
    match = matches[index - 1]
    mapped = _extent_km(match.bbox) if match.bbox else None

    if radius_km is not None and radius_km > 0:
        bbox = bbox_around(match.point, radius_km * 1000.0)
        widened = False
    elif match.bbox is None:
        # No extent given at all: fall back to the floor, centred on the match.
        bbox = bbox_around(match.point, cfg.place_min_km * 1000.0 / 2.0)
        widened = True
    else:
        bbox = _widen_to_floor(match.bbox, cfg.place_min_km)
        widened = bbox != match.bbox
    return ResolvedPlace(
        match=match,
        bbox=bbox,
        index=index,
        matches=tuple(matches),
        extent_km=_extent_km(bbox),
        mapped_extent_km=mapped,
        widened=widened,
        radius_km=radius_km if radius_km and radius_km > 0 else None,
    )


def describe_place(
    res: ResolvedPlace,
    *,
    label: str = "Place",
    extent: bool = True,
    index_flag: str = "--place-index",
) -> list[str]:
    """The lines a frontend shows so the user can see what the name became.

    One implementation for all frontends, so the CLI, the MCP server and anything else
    cannot word the same resolution differently — this project's recurring bug is a fact
    that reaches each surface on its own day. ``extent=False`` is for the point-based
    modes, where the box is plumbing and only the coordinate matters.

    The widening case gets its own clause rather than only the final size: "widened to
    2.0 km" tells the user their summit is being searched as a 2 km square, which is the
    difference between a trustworthy empty answer and a baffling one.
    """
    lat, lon = res.point
    head = f"{label}: {res.label} ({lat:.4f}, {lon:.4f})"
    if extent:
        w, h = res.extent_km
        if res.radius_km is not None:
            head += f" — searching {w:.1f} x {h:.1f} km (radius {res.radius_km:g} km)"
        elif res.widened and res.mapped_extent_km is not None:
            mw, mh = res.mapped_extent_km
            head += (
                f" — mapped extent {mw:.2f} x {mh:.2f} km, widened to "
                f"{w:.1f} x {h:.1f} km"
            )
        elif res.widened:
            head += f" — no mapped extent, searching {w:.1f} x {h:.1f} km"
        else:
            head += f" — searching {w:.1f} x {h:.1f} km"
    lines = [head]
    if res.ambiguous:
        others = [
            f"    {i}. {m.label}"
            for i, m in enumerate(res.matches, 1)
            if i != res.index
        ]
        lines.append(
            f"  match {res.index} of {len(res.matches)}; "
            f"{index_flag} N picks another:"
        )
        lines.extend(others)
    return lines
