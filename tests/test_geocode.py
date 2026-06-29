"""Reverse-geocode naming network seam + its cache.

Three layers under test, all offline (``requests.get`` is stubbed):
  * ``_parse_place`` — pick a concise settlement name from a Nominatim response;
  * ``NominatimGeocoder.reverse`` — request shape + best-effort failure (-> None);
  * the geocode cache (``Cache.get_place``/``put_place`` + ``CachingGeocoder``) —
    serve hits, fetch a miss once, cache a NEGATIVE result, TTL expiry, and degrade
    to the inner geocoder when the cache is dead.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hike_finder.cache import Cache, CachingGeocoder
from hike_finder.geocode import NominatimGeocoder, _parse_place


# --------------------------------------------------------------- _parse_place (pure)

def test_parse_place_prefers_most_specific_settlement():
    data = {"address": {"village": "Pec pod Sněžkou", "county": "Trutnov", "state": "CZ"}}
    assert _parse_place(data) == "Pec pod Sněžkou"


def test_parse_place_falls_back_through_keys():
    assert _parse_place({"address": {"county": "Trutnov"}}) == "Trutnov"


def test_parse_place_falls_back_to_name_when_no_admin_area():
    assert _parse_place({"address": {}, "name": "Sněžka"}) == "Sněžka"


def test_parse_place_none_when_nothing_resolves():
    assert _parse_place({}) is None
    assert _parse_place({"address": {}}) is None
    assert _parse_place("not a dict") is None


# ----------------------------------------------------------- NominatimGeocoder.reverse

class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_reverse_returns_place_and_sends_contact_ua(monkeypatch):
    import requests

    captured = {}

    def _get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers)
        return _Resp({"address": {"town": "Špindlerův Mlýn"}})

    monkeypatch.setattr(requests, "get", _get)
    g = NominatimGeocoder(user_agent="me@example.com", min_interval_s=0)  # no throttle
    assert g.reverse((50.73, 15.61)) == "Špindlerův Mlýn"
    assert captured["params"]["lat"] == 50.73 and captured["params"]["lon"] == 15.61
    assert captured["headers"]["User-Agent"] == "me@example.com"  # policy: identify


def test_reverse_none_on_http_error(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({}, status=429))
    g = NominatimGeocoder(min_interval_s=0)
    # Best-effort: a rate-limit / server error yields no label, never raises.
    assert g.reverse((0.0, 0.0)) is None


def test_reverse_none_on_network_error(monkeypatch):
    import requests

    def _boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", _boom)
    assert NominatimGeocoder(min_interval_s=0).reverse((0.0, 0.0)) is None


# ----------------------------------------------------------------- the geocode cache

class _StubGeo:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def reverse(self, point):
        self.calls.append(point)
        return self.table.get(point)


def _cache(tmp_path) -> Cache:
    return Cache(tmp_path / "c.sqlite3")


def test_place_store_round_trip_and_source_isolation(tmp_path):
    c = _cache(tmp_path)
    c.put_place("nomA", (50.0, 15.0), "Pec")
    assert c.get_place("nomA", (50.0, 15.0), None) == "Pec"
    # A different endpoint (source) must not cross-serve.
    assert c.get_place("nomB", (50.0, 15.0), None) is None
    # An unseen coord is a miss (None), distinct from a cached negative ("").
    assert c.get_place("nomA", (1.0, 1.0), None) is None


def test_place_store_caches_negative_result(tmp_path):
    c = _cache(tmp_path)
    c.put_place("nom", (50.0, 15.0), "")  # resolved to no place
    assert c.get_place("nom", (50.0, 15.0), None) == ""  # negative HIT, not a miss


def test_place_store_ttl_expiry(tmp_path):
    c = _cache(tmp_path)
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    c.put_place("nom", (50.0, 15.0), "Pec", now=old)
    fresh = old + timedelta(days=1)
    assert c.get_place("nom", (50.0, 15.0), 30 * 86400, now=fresh) == "Pec"
    stale = old + timedelta(days=40)
    assert c.get_place("nom", (50.0, 15.0), 30 * 86400, now=stale) is None


def test_caching_geocoder_serves_hit_without_calling_inner(tmp_path):
    c = _cache(tmp_path)
    inner = _StubGeo({(50.0, 15.0): "Pec"})
    g = CachingGeocoder(c, "nom", inner, ttl_seconds=10**9)
    assert g.reverse((50.0, 15.0)) == "Pec"      # miss -> inner, stored
    assert g.reverse((50.0, 15.0)) == "Pec"      # hit -> from cache
    assert inner.calls == [(50.0, 15.0)]          # inner asked exactly once


def test_caching_geocoder_caches_negative(tmp_path):
    c = _cache(tmp_path)
    inner = _StubGeo({})  # nothing resolves
    g = CachingGeocoder(c, "nom", inner, ttl_seconds=10**9)
    assert g.reverse((9.0, 9.0)) is None
    assert g.reverse((9.0, 9.0)) is None
    assert inner.calls == [(9.0, 9.0)]            # negative cached -> not re-queried


def test_caching_geocoder_degrades_when_cache_dead(tmp_path):
    c = _cache(tmp_path)
    c._ok = False  # simulate a broken/unavailable cache
    inner = _StubGeo({(1.0, 2.0): "X"})
    g = CachingGeocoder(c, "nom", inner, ttl_seconds=10**9)
    assert g.reverse((1.0, 2.0)) == "X"           # falls straight through to inner
    assert g.reverse((1.0, 2.0)) == "X"
    assert inner.calls == [(1.0, 2.0), (1.0, 2.0)]  # no caching, asked each time


def test_clear_empties_geocode_store(tmp_path):
    c = _cache(tmp_path)
    c.put_place("nom", (50.0, 15.0), "Pec")
    c.clear()
    assert c.get_place("nom", (50.0, 15.0), None) is None
