"""Reverse geocoding via Nominatim — turn a trailhead coordinate into a place name.

Used ONLY to label routes that carry no OSM name/ref (see ``naming.py``); it is
opt-in (``search.name_places`` / ``--name-places``) because Nominatim's usage policy
is strict: an absolute maximum of **1 request/second**, a valid ``User-Agent``
identifying the app with a contact, and **no bulk/systematic querying**. We honour
all three — a ``>= min_interval_s`` throttle, a contact UA threaded through from
config, and we only ever look up the handful of unnamed routes a search actually
returns, cached so a coordinate is fetched at most once (see ``cache.CachingGeocoder``).

Best-effort by design: ANY failure (network, rate-limit, unparseable response, no
place found) returns ``None``, so a labelling miss simply leaves the route at its
``route/<id>`` fallback and never breaks the search. The endpoint is configurable
(``HIKE_NOMINATIM_URL``) so heavy users can point at their own instance.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from .geometry import Coord

DEFAULT_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

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


class Geocoder(ABC):
    @abstractmethod
    def reverse(self, point: Coord) -> str | None:
        """Return a concise place name for a ``(lat, lon)`` point, or ``None`` if
        unknown. Implementations are best-effort: a failure returns ``None``, never
        raises, so labelling can't break a search."""
        raise NotImplementedError


class NominatimGeocoder(Geocoder):
    """Reverse-geocode a coordinate to a place name via a Nominatim instance.

    One instance is reused for every lookup in a search, so it throttles ALL requests
    to ``>= min_interval_s`` apart — Nominatim's hard 1 req/s cap is across the whole
    client, not per route. A descriptive ``User-Agent`` (the user's contact, from
    config) is sent on every request, as the policy requires.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_NOMINATIM_URL,
        *,
        user_agent: str | None = None,
        min_interval_s: float = 1.1,
        timeout_s: float = 10.0,
        zoom: int = 14,
    ):
        self.endpoint = endpoint
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
        try:
            resp = requests.get(
                self.endpoint,
                params={
                    "format": "jsonv2",
                    "lat": lat,
                    "lon": lon,
                    "zoom": self.zoom,
                    "addressdetails": 1,
                },
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
