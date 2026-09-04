"""Place names instead of coordinates — the forward-geocoding seam and its cache.

Four layers, all offline (``requests.get`` is stubbed):
  * ``_parse_bbox`` / ``_parse_matches`` — read a Nominatim ``/search`` response, and
    in particular REORDER its bounding box into this project's ``(south, west, north,
    east)``; Nominatim groups the latitudes together and we interleave them, so copying
    the four numbers straight across yields a box that is wrong and still plausible;
  * ``NominatimGeocoder.search`` — request shape, and the failure contract that is the
    OPPOSITE of the reverse direction (it raises rather than returning ``None``);
  * the forward cache (``Cache.get_place_search``/``put_place_search`` +
    ``CachingPlaceSearch``) — hits, negative caching, TTL, and that a FAILURE is never
    cached;
  * ``places.resolve_place`` — which match, how big an area, and the sentences that say
    so out loud.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hike_finder.cache import Cache, CachingPlaceSearch
from hike_finder.config import Config, load
from hike_finder.geocode import (
    DEFAULT_NOMINATIM_SEARCH_URL,
    GeocodeError,
    NominatimGeocoder,
    _parse_bbox,
    _parse_matches,
    search_endpoint_for,
)
from hike_finder.places import PlaceNotFound, describe_place, resolve_place

# A town: a real mapped extent, several km across.
_TOWN = {
    "lat": "50.7256",
    "lon": "15.6062",
    "display_name": "Špindlerův Mlýn, okres Trutnov, Czechia",
    "boundingbox": ["50.6600", "50.7800", "15.5300", "15.6900"],
    "address": {"country": "Czechia"},
    "addresstype": "town",
    "osm_type": "relation",
    "osm_id": "441323",
}
# A summit: mapped as a box a few metres across, which is the widening case.
_PEAK = {
    "lat": "50.7360",
    "lon": "15.7400",
    "display_name": "Sněžka, Czechia",
    "boundingbox": ["50.7358", "50.7362", "15.7396", "15.7404"],
    "address": {"country": "Czechia"},
    "addresstype": "peak",
}


# ------------------------------------------------------------------ parsing (pure)

def test_bbox_is_reordered_not_copied():
    """Nominatim gives [min_lat, max_lat, min_lon, max_lon]; we want (S, W, N, E).

    The whole point of this test: a straight copy would give (50.6, 50.75, 15.5, 15.7),
    a box whose "west" is north of the Arctic — wrong in a way that still parses, and
    near 50N/15E still looks like numbers someone might have typed.
    """
    assert _parse_bbox(["50.60", "50.75", "15.50", "15.70"]) == (50.60, 15.50, 50.75, 15.70)


def test_bbox_none_when_absent_or_unparseable():
    assert _parse_bbox(None) is None
    assert _parse_bbox(["50.6", "50.75"]) is None
    assert _parse_bbox(["50.6", "north", "15.5", "15.7"]) is None


def test_parse_matches_reads_name_country_point_and_extent():
    (m,) = _parse_matches([_TOWN])
    assert m.point == (50.7256, 15.6062)
    assert m.bbox == (50.66, 15.53, 50.78, 15.69)
    assert m.country == "Czechia"
    assert m.kind == "town"
    assert m.osm_id == 441323
    assert m.label.endswith("Czechia")


def test_parse_matches_falls_back_to_display_name_tail_for_country():
    """Without addressdetails the country is still the last chunk of display_name."""
    (m,) = _parse_matches([{**_TOWN, "address": None}])
    assert m.country == "Czechia"


def test_parse_matches_drops_a_result_it_cannot_place():
    """No usable coordinate = not a match. Dropping keeps "found nothing" and "found
    something we mangled" distinguishable, which a (0, 0) fallback would not."""
    assert _parse_matches([{"display_name": "Nowhere"}]) == []
    assert _parse_matches([{"lat": "50.7", "lon": "15.6"}]) == []  # no name
    assert _parse_matches("not a list") == []


def test_search_endpoint_follows_a_self_hosted_reverse_endpoint():
    """Configure one direction at your own instance and the other must follow it.

    Falling back to the public server would leak a self-hoster's queries onto it and
    burn its rate limit — a silent change of who is being asked.
    """
    assert search_endpoint_for(None) == DEFAULT_NOMINATIM_SEARCH_URL
    assert search_endpoint_for("https://h/nominatim/reverse") == "https://h/nominatim/search"
    assert search_endpoint_for("https://h/nominatim/reverse.php") == "https://h/nominatim/search.php"
    assert search_endpoint_for("https://h/lookup") == "https://h/search"


# ------------------------------------------------------- NominatimGeocoder.search

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


def test_search_sends_the_query_the_contact_ua_and_the_limit(monkeypatch):
    import requests

    captured = {}

    def _get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers)
        return _Resp([_TOWN])

    monkeypatch.setattr(requests, "get", _get)
    geo = NominatimGeocoder(user_agent="hike-finder test <me@example.com>")
    (m,) = geo.search("Spindleruv Mlyn", limit=3)
    assert m.point == (50.7256, 15.6062)
    assert captured["url"] == DEFAULT_NOMINATIM_SEARCH_URL
    assert captured["params"]["q"] == "Spindleruv Mlyn"
    assert captured["params"]["limit"] == 3
    assert "me@example.com" in captured["headers"]["User-Agent"]


def test_search_raises_where_reverse_would_return_none(monkeypatch):
    """The two directions fail in opposite ways, and that is the design.

    A reverse miss costs a route its label. A forward miss would cost us the ground we
    search: silently falling back to anything at all would search somewhere the user
    never named and report it as though they had.
    """
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp([], status=503))
    geo = NominatimGeocoder()
    with pytest.raises(GeocodeError):
        geo.search("Spindleruv Mlyn")
    # ...and the reverse direction on the SAME instance still swallows the failure.
    assert geo.reverse((50.7, 15.6)) is None


def test_search_raises_on_a_network_error(monkeypatch):
    import requests

    def _boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(GeocodeError):
        NominatimGeocoder().search("anywhere")


def test_search_returns_empty_for_a_name_nominatim_does_not_know(monkeypatch):
    """Empty is an ANSWER — "no such place" — and must not raise: the caller words it
    as a spelling problem, which is a different instruction to the user than "retry"."""
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp([]))
    assert NominatimGeocoder().search("Atlantis") == []


def test_search_does_not_call_out_for_an_empty_query(monkeypatch):
    import requests

    def _boom(*a, **k):
        raise AssertionError("should not have been called")

    monkeypatch.setattr(requests, "get", _boom)
    assert NominatimGeocoder().search("   ") == []


# -------------------------------------------------------------------- the cache

class _StubSearch:
    def __init__(self, matches=None, error=None):
        self.matches = matches if matches is not None else _parse_matches([_TOWN])
        self.error = error
        self.calls = 0

    def search(self, query, *, limit=5):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.matches


def test_place_search_store_round_trip(tmp_path):
    c = Cache(tmp_path / "c.sqlite3")
    c.put_place_search("src", "spindl", _parse_matches([_TOWN]))
    (m,) = c.get_place_search("src", "spindl", None)
    assert m.point == (50.7256, 15.6062)
    assert m.bbox == (50.66, 15.53, 50.78, 15.69)
    assert m.country == "Czechia"
    # Another endpoint is another question.
    assert c.get_place_search("other", "spindl", None) is None


def test_place_search_store_caches_a_negative_result(tmp_path):
    """"Nominatim knows no such place" is worth storing: a typo in a script should not
    re-hit a rate-limited public server on every run. ``[]`` is a hit, ``None`` a miss."""
    c = Cache(tmp_path / "c.sqlite3")
    c.put_place_search("src", "atlantis", [])
    assert c.get_place_search("src", "atlantis", None) == []
    assert c.get_place_search("src", "never asked", None) is None


def test_place_search_store_ttl_expiry(tmp_path):
    c = Cache(tmp_path / "c.sqlite3")
    old = datetime.now(timezone.utc) - timedelta(days=400)
    c.put_place_search("src", "spindl", _parse_matches([_TOWN]), now=old)
    assert c.get_place_search("src", "spindl", 365 * 86400) is None
    assert c.get_place_search("src", "spindl", 500 * 86400) is not None


def test_caching_place_search_asks_once_then_serves_the_hit(tmp_path):
    inner = _StubSearch()
    wrapped = CachingPlaceSearch(Cache(tmp_path / "c.sqlite3"), "src", inner, 86400)
    assert wrapped.search("Spindleruv Mlyn")[0].country == "Czechia"
    # Same question, differently typed and cased: still one request.
    assert wrapped.search("  spindleruv   MLYN ")[0].country == "Czechia"
    assert inner.calls == 1


def test_caching_place_search_keys_on_the_limit(tmp_path):
    """Asking for 3 candidates and asking for 8 are different questions: serving the
    short answer to the long one would hide alternatives the user is being offered."""
    inner = _StubSearch()
    wrapped = CachingPlaceSearch(Cache(tmp_path / "c.sqlite3"), "src", inner, 86400)
    wrapped.search("Lhota", limit=3)
    wrapped.search("Lhota", limit=8)
    assert inner.calls == 2


def test_caching_place_search_never_caches_a_failure(tmp_path):
    """A Nominatim outage must not become a stored "no such place" outliving it by a
    year. The exception passes straight through and nothing is written."""
    inner = _StubSearch(error=GeocodeError("down"))
    cache = Cache(tmp_path / "c.sqlite3")
    wrapped = CachingPlaceSearch(cache, "src", inner, 86400)
    with pytest.raises(GeocodeError):
        wrapped.search("Spindleruv Mlyn")
    assert cache.get_place_search("src", "5|spindleruv mlyn", None) is None


def test_clear_empties_the_place_search_store(tmp_path):
    c = Cache(tmp_path / "c.sqlite3")
    c.put_place_search("src", "spindl", _parse_matches([_TOWN]))
    c.clear()
    assert c.get_place_search("src", "spindl", None) is None


# --------------------------------------------------------------- resolve_place

@pytest.fixture
def stub_search(monkeypatch):
    """Point ``places`` at a canned candidate list without touching the network."""

    def _install(payloads):
        matches = _parse_matches(payloads)
        monkeypatch.setattr(
            "hike_finder.places.place_searcher",
            lambda cfg, cache=None: _StubSearch(matches),
        )
        return matches

    return _install


def test_resolve_uses_the_mapped_extent_of_a_town(stub_search):
    stub_search([_TOWN])
    res = resolve_place("Spindleruv Mlyn", Config(), cache=None)
    assert res.bbox == (50.66, 15.53, 50.78, 15.69)
    assert not res.widened
    assert res.point == (50.7256, 15.6062)


def test_resolve_widens_a_point_sized_place_and_records_that_it_did(stub_search):
    """A summit is mapped ~50 m across. Searching that literally returns nothing, with
    no cause the user can see — so it is widened to the floor, and the widening is
    recorded so the frontend can SAY it happened."""
    stub_search([_PEAK])
    res = resolve_place("Snezka", Config(), cache=None)
    assert res.widened
    assert res.mapped_extent_km is not None and max(res.mapped_extent_km) < 0.1
    assert res.extent_km[0] == pytest.approx(2.0, abs=0.05)
    assert res.extent_km[1] == pytest.approx(2.0, abs=0.05)


def test_resolve_widens_only_the_narrow_axis(stub_search):
    """A long thin valley keeps its length: squaring it off would search two ridgelines
    the user never named."""
    valley = {**_TOWN, "boundingbox": ["50.6000", "50.6540", "15.6000", "15.6042"]}
    stub_search([valley])
    res = resolve_place("valley", Config(), cache=None)
    w, h = res.extent_km
    assert w == pytest.approx(2.0, abs=0.05)  # widened
    assert h == pytest.approx(6.0, abs=0.2)  # left alone


def test_place_radius_replaces_the_extent(stub_search):
    stub_search([_TOWN])
    res = resolve_place("Spindleruv Mlyn", Config(), cache=None, radius_km=5)
    assert res.radius_km == 5
    assert res.extent_km[0] == pytest.approx(10.0, abs=0.1)
    assert not res.widened


def test_place_min_km_is_configurable(stub_search):
    stub_search([_PEAK])
    res = resolve_place("Snezka", Config(place_min_km=8.0), cache=None)
    assert res.extent_km[0] == pytest.approx(8.0, abs=0.1)


def test_the_place_knobs_are_read_from_the_environment(monkeypatch):
    """The test above overrides the dataclass FIELD, which bypasses the env read — so it
    proves the floor is honoured, not that the documented variable reaches it. Config
    reads env per instantiation (``default_factory``), so this is the half that checks
    the name in the README is the name in the code."""
    monkeypatch.setenv("HIKE_PLACE_MIN_KM", "8")
    monkeypatch.setenv("HIKE_PLACE_MATCHES", "9")
    monkeypatch.setenv("HIKE_NOMINATIM_SEARCH_URL", "https://example.test/search")
    cfg = load()
    assert cfg.place_min_km == 8.0
    assert cfg.place_matches == 9
    assert cfg.nominatim_search_url == "https://example.test/search"


def test_ambiguity_is_reported_not_resolved(stub_search):
    """The first match is taken — but every alternative comes back with it, so the
    frontend can list them. Choosing for the user is fine; choosing silently is not."""
    lhotas = [
        {**_TOWN, "display_name": f"Lhota {n}, Czechia", "lat": f"50.{n}", "lon": "14.5"}
        for n in (1, 2, 3)
    ]
    stub_search(lhotas)
    res = resolve_place("Lhota", Config(), cache=None)
    assert res.ambiguous and len(res.matches) == 3
    assert res.index == 1
    lines = describe_place(res)
    assert "match 1 of 3" in lines[1]
    assert any("Lhota 2" in ln for ln in lines)
    assert any("Lhota 3" in ln for ln in lines)


def test_place_index_picks_another_match(stub_search):
    lhotas = [
        {**_TOWN, "display_name": f"Lhota {n}, Czechia", "lat": f"50.{n}", "lon": "14.5"}
        for n in (1, 2, 3)
    ]
    stub_search(lhotas)
    res = resolve_place("Lhota", Config(), cache=None, index=2)
    assert res.match.name.startswith("Lhota 2")
    assert res.point[0] == pytest.approx(50.2)


def test_place_index_out_of_range_says_how_many_there_were(stub_search):
    stub_search([_TOWN])
    with pytest.raises(ValueError) as e:
        resolve_place("Spindleruv Mlyn", Config(), cache=None, index=4)
    assert "1..1" in str(e.value)


def test_no_match_is_its_own_error(stub_search):
    """Separate from a plain GeocodeError: the user's next move is to re-spell here and
    to retry there, so the two cannot arrive as one message."""
    stub_search([])
    with pytest.raises(PlaceNotFound):
        resolve_place("Atlantis", Config(), cache=None)


def test_empty_query_is_rejected_before_any_lookup(stub_search):
    stub_search([_TOWN])
    with pytest.raises(ValueError):
        resolve_place("   ", Config(), cache=None)


# ------------------------------------------------------------- describe_place

def test_description_names_the_place_the_country_and_the_area(stub_search):
    stub_search([_TOWN])
    (line,) = describe_place(resolve_place("Spindleruv Mlyn", Config(), cache=None))
    assert "Špindlerův Mlýn" in line and "Czechia" in line
    assert "50.7256" in line and "15.6062" in line
    assert "searching" in line and "km" in line


def test_description_says_when_it_widened_and_from_what(stub_search):
    """The widening clause is the point: "widened to 2.0 km" is the difference between
    a trustworthy empty answer and a baffling one."""
    stub_search([_PEAK])
    (line,) = describe_place(resolve_place("Snezka", Config(), cache=None))
    assert "mapped extent" in line
    assert "widened to" in line


def test_description_for_a_point_mode_drops_the_extent(stub_search):
    """The box is plumbing for --from/--to/--around; only the coordinate matters."""
    stub_search([_PEAK])
    res = resolve_place("Snezka", Config(), cache=None)
    (line,) = describe_place(res, label="From", extent=False)
    assert line.startswith("From: Sněžka")
    assert "km" not in line


def test_description_names_the_index_flag_it_is_given(stub_search):
    """The CLI and the MCP server offer the same choice under different names, so the
    sentence takes the name rather than hard-coding one frontend's spelling."""
    stub_search([_TOWN, _PEAK])
    res = resolve_place("x", Config(), cache=None)
    assert any("place_index" in ln for ln in describe_place(res, index_flag="place_index"))
    assert any("--place-index" in ln for ln in describe_place(res))
