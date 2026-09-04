"""Geocoding via Nominatim, in both directions.

**Reverse** (coordinate -> place name) labels routes that carry no OSM name/ref (see
``naming.py``). **Forward** (place name -> coordinate + extent) is what lets a frontend
take ``--place "Špindlerův Mlýn"`` instead of four bbox corners (see ``places.py``).

Both go through one throttle and one contact ``User-Agent``, because Nominatim's usage
policy is strict and counted per client, not per direction: an absolute maximum of
**1 request/second**, a valid ``User-Agent`` identifying the app with a contact, and
**no bulk/systematic querying**. We honour all three — a ``>= min_interval_s`` throttle
shared by ``reverse`` and ``search``, a contact UA threaded through from config, and we
only ever look up the handful of unnamed routes a search returns (reverse) or the one
name the user typed (forward), cached so neither is fetched twice (see
``cache.CachingGeocoder`` / ``cache.CachingPlaceSearch``).

**The two directions handle failure in OPPOSITE ways, on purpose.** Reverse is
best-effort: ANY failure (network, rate-limit, unparseable response, no place found)
returns ``None``, so a labelling miss simply leaves the route at its ``route/<id>``
fallback and never breaks the search. Forward cannot be best-effort, because its answer
*is* where we search: falling back to some default area would search ground the user
never asked about and report the result as if they had. So ``search`` raises
``GeocodeError`` when Nominatim could not be reached or understood, and returns an empty
list only when Nominatim positively answered "no such place" — two different situations
that call for two different things from the user (retry vs. re-spell), which a shared
``None`` would merge.

Both endpoints are configurable (``HIKE_NOMINATIM_URL``, ``HIKE_NOMINATIM_SEARCH_URL``)
so heavy users can point at their own instance.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .geometry import Coord

DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
DEFAULT_NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

# A default UA that names no contact (works, but set HIKE_OVERPASS_UA to a real one).
# Config threads the user's Overpass contact through as the UA, so this is a fallback.
DEFAULT_USER_AGENT = (
    "hike-finder-mcp/0.1 (OSM hiking route search; set HIKE_OVERPASS_UA with your contact)"
)

# Address fields from most to least specific — the first present wins, so a trailhead
# in a village reads as that village, falling back to broader admin areas only when no
# settlement is mapped. (Nominatim ``address`` keys, jsonv2.)
_PLACE_KEYS = (
    "village", "town", "city", "hamlet", "municipality", "suburb",
    "city_district", "locality", "isolated_dwelling", "county", "state",
)

_log = logging.getLogger(__name__)


class GeocodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlaceMatch:
    """One candidate a forward lookup returned for a typed name.

    ``bbox`` is the extent Nominatim maps for the place, already reordered into this
    project's ``(south, west, north, east)``; it is ``None`` when the response carried
    no usable extent. It is deliberately NOT defaulted to a zero-size box at ``point``:
    "this place is a few metres across" (a summit node) and "nobody told us how big this
    is" both need widening, but only the second is a gap in what we know, and the
    frontends say so differently.
    """

    name: str
    point: Coord
    bbox: tuple[float, float, float, float] | None = None
    country: str | None = None
    kind: str | None = None
    osm_type: str | None = None
    osm_id: int | None = None

    @property
    def label(self) -> str:
        """``"Sněžka, Czechia"`` — the match as a person would say it back."""
        if self.country and not self.name.endswith(self.country):
            return f"{self.name}, {self.country}"
        return self.name


def search_endpoint_for(reverse_url: str | None) -> str:
    """The ``/search`` endpoint that belongs with a configured ``/reverse`` one.

    Someone who self-hosts sets ``HIKE_NOMINATIM_URL`` to their instance's reverse
    endpoint; forward lookups must then go to the SAME instance, not silently back to
    the public server (which would leak their queries and burn the public rate limit).
    Nominatim serves both from one base, so swapping the last path segment is the whole
    rule — ``.../reverse`` -> ``.../search``, ``.../reverse.php`` -> ``.../search.php``.
    Anything unrecognisable falls back to a sibling ``search``; ``HIKE_NOMINATIM_SEARCH_URL``
    is the escape hatch when an instance is laid out some other way.
    """
    if not reverse_url or reverse_url == DEFAULT_NOMINATIM_URL:
        return DEFAULT_NOMINATIM_SEARCH_URL
    base, _, last = reverse_url.rstrip("/").rpartition("/")
    if not base:
        return DEFAULT_NOMINATIM_SEARCH_URL
    if last.startswith("reverse"):
        return f"{base}/search{last[len('reverse'):]}"
    return f"{base}/search"


class PlaceSearcher(ABC):
    """Forward lookup: a typed name -> the places it could mean.

    Separate from :class:`Geocoder` rather than another method on it, because the two
    have different failure contracts (see the module docstring) and different
    implementations: the snapshot and recording geocoders in ``snapshot.py`` answer
    reverse lookups offline and have no forward answer to give.
    """

    @abstractmethod
    def search(self, query: str, *, limit: int = 5) -> list[PlaceMatch]:
        """Candidates for ``query``, best match first.

        An empty list means Nominatim found nothing. Anything that stops us from
        knowing — network, HTTP, unparseable response — raises
        :class:`GeocodeError`, because a forward lookup's answer decides where we
        search and a wrong area reported as a right one is the failure to avoid.
        """
        raise NotImplementedError


class Geocoder(ABC):
    @abstractmethod
    def reverse(self, point: Coord) -> str | None:
        """Return a concise place name for a ``(lat, lon)`` point, or ``None`` if
        unknown. Implementations are best-effort: a failure returns ``None``, never
        raises, so labelling can't break a search."""
        raise NotImplementedError


class NominatimGeocoder(Geocoder, PlaceSearcher):
    """Talk to a Nominatim instance in both directions.

    One instance is reused for every lookup in a search, so it throttles ALL requests
    to ``>= min_interval_s`` apart — Nominatim's hard 1 req/s cap is across the whole
    client, not per route, and not per direction, so ``reverse`` and ``search`` share
    one throttle. A descriptive ``User-Agent`` (the user's contact, from config) is
    sent on every request, as the policy requires.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_NOMINATIM_URL,
        *,
        search_endpoint: str | None = None,
        user_agent: str | None = None,
        min_interval_s: float = 1.1,
        timeout_s: float = 10.0,
        zoom: int = 14,
    ):
        self.endpoint = endpoint
        # Derived from the reverse endpoint unless given, so configuring one instance
        # moves both directions to it (see ``search_endpoint_for``).
        self.search_url = search_endpoint or search_endpoint_for(endpoint)
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        # zoom 14 ≈ town/suburb level: the settlement you'd name a trailhead by.
        self.zoom = zoom
        self.timeout = timeout_s
        # Nominatim's public server caps at ~1 request/second; go over and it 429s.
        # One instance is reused for every lookup in a search, so we throttle ACROSS
        # routes (not just within one), mirroring ApiElevationProvider._throttle.
        self.min_interval_s = min_interval_s
        self._last_request_t: float | None = None

    def _throttle(self) -> None:
        if self.min_interval_s <= 0:
            return
        if self._last_request_t is not None:
            wait = self.min_interval_s - (time.monotonic() - self._last_request_t)
            if wait > 0:
                time.sleep(wait)
        self._last_request_t = time.monotonic()

    def reverse(self, point: Coord) -> str | None:
        import requests  # lazy: a base install that never geocodes doesn't pay for it

        lat, lon = point
        self._throttle()
        params: dict[str, str | float | int] = {
            "format": "jsonv2",
            "lat": lat,
            "lon": lon,
            "zoom": self.zoom,
            "addressdetails": 1,
        }
        try:
            resp = requests.get(
                self.endpoint,
                params=params,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            # Best-effort: any network/HTTP/parse failure -> no label, never fatal.
            # We deliberately do NOT retry — a Nominatim 429 means back off, and a
            # missing label is harmless, so retrying would only risk the rate cap.
            _log.debug("reverse geocode failed for %s: %s", point, e)
            return None
        return _parse_place(data)


    def search(self, query: str, *, limit: int = 5) -> list[PlaceMatch]:
        import requests  # lazy: a base install that never geocodes doesn't pay for it

        text = (query or "").strip()
        if not text:
            return []
        self._throttle()
        params: dict[str, str | int] = {
            "format": "jsonv2",
            "q": text,
            "limit": max(1, int(limit)),
            "addressdetails": 1,
        }
        try:
            resp = requests.get(
                self.search_url,
                params=params,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            # Unlike ``reverse``, this RAISES: the caller is about to decide which
            # ground to search, and "we could not ask" must not read as "no such
            # place". No retry, for reverse's reason — a 429 means back off.
            raise GeocodeError(f"could not look up {text!r}: {e}") from e
        return _parse_matches(data)


def _parse_matches(data) -> list[PlaceMatch]:
    """Turn a Nominatim jsonv2 ``/search`` response into matches (pure).

    A result we cannot place (no usable lat/lon) is dropped rather than guessed at,
    so the caller's "no match" and "a match we mangled" stay distinguishable.
    """
    if not isinstance(data, list):
        return []
    out: list[PlaceMatch] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            point = (float(item["lat"]), float(item["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        name = str(item.get("display_name") or item.get("name") or "").strip()
        if not name:
            continue
        address = item.get("address")
        country = None
        if isinstance(address, dict) and address.get("country"):
            country = str(address["country"])
        else:
            # display_name ends with the country when addressdetails are absent.
            tail = name.rsplit(",", 1)[-1].strip()
            country = tail or None
        raw_id = item.get("osm_id")
        osm_id = (
            int(raw_id)
            if isinstance(raw_id, (int, str)) and str(raw_id).lstrip("-").isdigit()
            else None
        )
        out.append(
            PlaceMatch(
                name=name,
                point=point,
                bbox=_parse_bbox(item.get("boundingbox")),
                country=country,
                kind=str(item.get("addresstype") or item.get("type") or "") or None,
                osm_type=str(item.get("osm_type") or "") or None,
                osm_id=osm_id,
            )
        )
    return out


def _parse_bbox(raw) -> tuple[float, float, float, float] | None:
    """Nominatim's ``boundingbox`` -> this project's ``(south, west, north, east)``.

    Nominatim orders it ``[min_lat, max_lat, min_lon, max_lon]`` — latitudes together,
    then longitudes — while every bbox in this codebase is ``(south, west, north, east)``,
    which interleaves them. Copying the four numbers across in order yields a box that is
    silently wrong (and, near 50°N/15°E, still plausible-looking), so the reorder lives
    here alone and is pinned by its own test.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        min_lat, max_lat, min_lon, max_lon = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (min_lat, min_lon, max_lat, max_lon)


def _parse_place(data) -> str | None:
    """Pick a concise settlement name from a Nominatim jsonv2 response (pure)."""
    if not isinstance(data, dict):
        return None
    address = data.get("address")
    if isinstance(address, dict):
        for key in _PLACE_KEYS:
            val = address.get(key)
            if val:
                return str(val)
    # No admin area resolved (e.g. open countryside): fall back to the POI 'name'.
    name = data.get("name")
    return str(name) if name else None
