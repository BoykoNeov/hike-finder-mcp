"""Transparent Overpass + elevation cache (cache.py).

Two layers under test:
  * the SQLite store (round-trip, source isolation, TTL, chunking, failure-isolation);
  * the ``CachingElevationProvider`` decorator (serve hits, fetch misses once, preserve
    order, propagate errors without storing).

Plus the load-bearing end-to-end claim — the whole point of this feature — proven at the
``search_hikes`` level with the network stubbed:
  * a repeated search makes ZERO further elevation requests and leaves the daily-quota
    counter UNCHANGED (the "respect usage policies" goal, stated literally);
  * the elevation cache hits across *different overlapping bboxes* (a route relation
    carries full member geometry regardless of bbox, so the same route resamples to the
    same points — the higher-value half of the cache), while Overpass stays bbox-keyed;
  * ``--no-cache`` / ``HIKE_CACHE=0`` re-fetches everything.

All offline: the SQLite cache needs no network, and the search-level tests stub
``fetch_area`` and ``requests.post``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hike_finder import cache as cache_mod
from hike_finder import config as config_mod
from hike_finder.cache import Cache, CachingElevationProvider
from hike_finder.elevation.base import ElevationError, ElevationProvider
from hike_finder.overpass import AreaData

EP = "https://api.opentopodata.org/v1/srtm30m"
EP2 = "https://api.opentopodata.org/v1/aster30m"  # same host, different dataset


# --------------------------------------------------------------------------- helpers


class _Counting(ElevationProvider):
    """Deterministic per-point elevation that records every batch it was asked for."""

    def __init__(self, fail=False):
        self.calls: list[list] = []
        self.fail = fail

    def lookup(self, points):
        self.calls.append(list(points))
        if self.fail:
            raise ElevationError("inner boom")
        return [round(lat * 10 + lon, 4) for lat, lon in points]

    @property
    def n_points(self) -> int:
        return sum(len(c) for c in self.calls)


def _cache(tmp_path) -> Cache:
    return Cache(tmp_path / "c.sqlite3")


# --------------------------------------------------------------------------- SQLite store


def test_elevation_round_trip_and_source_isolation(tmp_path):
    c = _cache(tmp_path)
    pts = [(50.0, 14.0), (50.1, 14.2)]
    c.put_elevations(EP, {pts[0]: 100.0, pts[1]: 250.0})
    assert c.get_elevations(EP, pts) == {pts[0]: 100.0, pts[1]: 250.0}
    # A different endpoint (same host) must NOT see the other dataset's values.
    assert c.get_elevations(EP2, pts) == {}


def test_elevation_partial_hit(tmp_path):
    c = _cache(tmp_path)
    a, b, miss = (50.0, 14.0), (50.1, 14.0), (50.2, 14.0)
    c.put_elevations(EP, {a: 1.0, b: 2.0})
    assert c.get_elevations(EP, [a, miss, b]) == {a: 1.0, b: 2.0}


def test_elevation_rounding_tolerates_float_drift(tmp_path):
    c = _cache(tmp_path)
    stored = (50.123456789, 14.987654321)   # rounds to 50.1234568, 14.9876543
    c.put_elevations(EP, {stored: 333.0})
    # A coord differing only past the 7th decimal rounds to the same ~1cm key, so it
    # still hits — a lookup never depends on bit-exact float reproduction.
    drifted = (50.12345681, 14.98765434)
    assert c.get_elevations(EP, [drifted]) == {drifted: 333.0}


def test_elevation_chunking_over_sqlite_var_limit(tmp_path):
    c = _cache(tmp_path)
    pts = [(50.0 + i * 1e-4, 14.0) for i in range(500)]  # > _SELECT_CHUNK (400)
    c.put_elevations(EP, {p: float(i) for i, p in enumerate(pts)})
    got = c.get_elevations(EP, pts)
    assert len(got) == 500
    assert got[pts[499]] == 499.0


def test_overpass_area_round_trip(tmp_path):
    c = _cache(tmp_path)
    area = AreaData(
        routes=[{"id": 7, "name": "R", "ref": None, "osmc_color": None, "tags": {},
                 "ways": [[(50.0, 14.0), (50.01, 14.0)]]}],
        parking=[{"coord": (50.0, 14.0), "name": "P"}],
        lifts=[{"stations": [(50.01, 14.0)], "kind": "gondola", "name": "G"}],
    )
    c.put_area("k", area)
    got = c.get_area("k", ttl_seconds=86400)
    assert got is not None
    assert got.routes[0]["id"] == 7
    assert got.routes[0]["ways"][0][0] == (50.0, 14.0)  # restored as a tuple
    assert got.parking[0]["coord"] == (50.0, 14.0)
    assert got.lifts[0]["kind"] == "gondola"


def test_overpass_ttl_expiry(tmp_path):
    c = _cache(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    c.put_area("k", AreaData(routes=[]), now=base)
    fresh = base + timedelta(days=10)
    stale = base + timedelta(days=40)
    assert c.get_area("k", ttl_seconds=30 * 86400, now=fresh) is not None
    assert c.get_area("k", ttl_seconds=30 * 86400, now=stale) is None
    # ttl_seconds=None disables expiry entirely.
    assert c.get_area("k", ttl_seconds=None, now=stale) is not None


def test_overpass_miss_returns_none(tmp_path):
    assert _cache(tmp_path).get_area("absent", ttl_seconds=86400) is None


def test_clear_empties_both_stores(tmp_path):
    c = _cache(tmp_path)
    c.put_elevations(EP, {(50.0, 14.0): 1.0})
    c.put_area("k", AreaData(routes=[]))
    c.clear()
    assert c.get_elevations(EP, [(50.0, 14.0)]) == {}
    assert c.get_area("k", ttl_seconds=86400) is None


def test_unusable_cache_degrades_silently(tmp_path):
    # Point the DB at a directory: it can't be opened, so every op is a clean no-op
    # rather than an exception — a broken cache must never break a search.
    bad = Cache(tmp_path)  # tmp_path is a directory
    assert bad._ok is False
    assert bad.get_elevations(EP, [(50.0, 14.0)]) == {}
    bad.put_elevations(EP, {(50.0, 14.0): 1.0})  # no raise
    assert bad.get_area("k", ttl_seconds=86400) is None
    bad.put_area("k", AreaData(routes=[]))  # no raise
    bad.clear()  # no raise


# --------------------------------------------------------------------------- decorator


def test_caching_provider_second_lookup_is_a_hit(tmp_path):
    inner = _Counting()
    prov = CachingElevationProvider(_cache(tmp_path), EP, inner)
    pts = [(50.0, 14.0), (50.1, 14.2), (50.2, 14.4)]
    first = prov.lookup(pts)
    assert inner.n_points == 3
    second = prov.lookup(pts)
    assert second == first              # identical values
    assert len(inner.calls) == 1        # inner never called again


def test_caching_provider_fetches_only_misses_in_order(tmp_path):
    inner = _Counting()
    prov = CachingElevationProvider(_cache(tmp_path), EP, inner)
    a, b, c = (50.0, 14.0), (50.1, 14.0), (50.2, 14.0)
    prov.lookup([a, b])
    inner.calls.clear()
    out = prov.lookup([a, b, c])        # only c is new
    assert inner.calls == [[c]]
    assert out == [round(a[0] * 10 + a[1], 4), round(b[0] * 10 + b[1], 4),
                   round(c[0] * 10 + c[1], 4)]  # order preserved


def test_caching_provider_dedupes_repeated_points(tmp_path):
    inner = _Counting()
    prov = CachingElevationProvider(_cache(tmp_path), EP, inner)
    a, b = (50.0, 14.0), (50.1, 14.0)
    out = prov.lookup([a, b, a, b])
    assert inner.calls == [[a, b]]      # deduped to the inner provider
    assert out == [out[0], out[1], out[0], out[1]]


def test_caching_provider_error_propagates_without_storing(tmp_path):
    c = _cache(tmp_path)
    failing = _Counting(fail=True)
    prov = CachingElevationProvider(c, EP, failing)
    with pytest.raises(ElevationError):
        prov.lookup([(50.0, 14.0)])
    assert c.get_elevations(EP, [(50.0, 14.0)]) == {}  # nothing cached on failure


# --------------------------------------------------------------------------- config


def test_env_bool_parsing():
    assert config_mod._env_bool("X_MISSING", True) is True
    assert config_mod._env_bool("X_MISSING", False) is False


def test_from_config_disabled_returns_none():
    # Disabling is what `--no-cache` does: flip the resolved Config attribute.
    cfg = config_mod.load()
    cfg.cache_enabled = False
    assert cache_mod.from_config(cfg) is None


def test_from_config_enabled_builds_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HIKE_CACHE_DIR", str(tmp_path / "cdir"))
    c = cache_mod.from_config(config_mod.load())
    assert isinstance(c, Cache)
    assert c.path == (tmp_path / "cdir" / "cache.sqlite3")


# --------------------------------------------------------------------------- search-level


class _Resp:
    def __init__(self, n):
        self.status_code = 200
        self.headers = {}
        self._n = n

    def raise_for_status(self):
        pass

    def json(self):
        return {"results": [{"elevation": 100.0} for _ in range(self._n)]}


class _FakePost:
    """Stand-in for the elevation API's requests.post; counts calls + locations."""

    def __init__(self):
        self.calls = 0

    def __call__(self, url, json=None, timeout=None, **kw):
        self.calls += 1
        locs = json["locations"]
        n = len(locs.split("|")) if isinstance(locs, str) else len(locs)
        return _Resp(n)


def _one_route_area(*_args, **_kw) -> AreaData:
    # ~330 m route -> a handful of resample points, well under the over-length guard.
    return AreaData(
        routes=[{"id": 42, "name": "Test", "ref": None, "osmc_color": None, "tags": {},
                 "ways": [[(50.000, 14.000), (50.003, 14.000)]]}],
        parking=[], lifts=[],
    )


@pytest.fixture
def _stub_network(monkeypatch):
    """Stub both network seams (Overpass fetch counted + elevation API post) and
    return a fully-pinned ``cfg``.

    The config knobs are set on the object, NOT via env: ``Config`` snapshots env at
    import time, so ``monkeypatch.setenv`` here would be a no-op (the same gotcha the
    cache-dir fix works around). We pin them explicitly so these tests are hermetic
    regardless of the developer's environment — in particular ``dem_dir=None`` forces
    elevation through the (cached) API rather than letting a stray ``HIKE_DEM_DIR``
    serve points from local tiles and zero out ``post.calls``.
    """
    fetches = {"n": 0}

    def fake_fetch(*args, **kw):
        fetches["n"] += 1
        return _one_route_area()

    post = _FakePost()
    monkeypatch.setattr("hike_finder.search.fetch_area", fake_fetch)
    monkeypatch.setattr("hike_finder.elevation.api.requests.post", post)

    cfg = config_mod.load()
    cfg.elevation_mode = "api"      # not 'auto' -> no DEM in the chain
    cfg.dem_dir = None              # belt-and-braces: never read local tiles
    cfg.api_min_interval_s = 0      # no real throttle sleeps
    return fetches, post, cfg


def test_repeat_search_hits_cache_and_spares_the_api(_stub_network):
    """The headline goal: a second identical search re-fetches NOTHING and the daily
    quota counter does not move."""
    from hike_finder.elevation import api_quota_snapshot
    from hike_finder.filters import Criteria
    from hike_finder.search import search_hikes

    fetches, post, cfg = _stub_network
    bbox = (49.99, 13.99, 50.01, 14.01)

    search_hikes(bbox, Criteria(), cfg=cfg)
    used_after_first, _ = api_quota_snapshot(cfg)
    assert fetches["n"] == 1 and post.calls == 1 and used_after_first >= 1

    search_hikes(bbox, Criteria(), cfg=cfg)
    used_after_second, _ = api_quota_snapshot(cfg)
    assert fetches["n"] == 1                     # Overpass served from cache
    assert post.calls == 1                       # elevation served from cache
    assert used_after_second == used_after_first  # quota untouched on the repeat


def test_elevation_cache_hits_across_different_bboxes(_stub_network):
    """A different (overlapping) bbox is an Overpass miss but, because the route's
    geometry is identical, every elevation point is a cache hit."""
    from hike_finder.filters import Criteria
    from hike_finder.search import search_hikes

    fetches, post, cfg = _stub_network

    search_hikes((49.99, 13.99, 50.01, 14.01), Criteria(), cfg=cfg)
    search_hikes((49.98, 13.98, 50.02, 14.02), Criteria(), cfg=cfg)  # different bbox

    assert fetches["n"] == 2   # Overpass is bbox-keyed -> two fetches
    assert post.calls == 1     # elevation spans bboxes -> one fetch total


def test_no_cache_refetches_everything(_stub_network):
    from hike_finder.filters import Criteria
    from hike_finder.search import search_hikes

    fetches, post, cfg = _stub_network
    cfg.cache_enabled = False  # what `--no-cache` sets
    bbox = (49.99, 13.99, 50.01, 14.01)

    search_hikes(bbox, Criteria(), cfg=cfg)
    search_hikes(bbox, Criteria(), cfg=cfg)
    assert fetches["n"] == 2 and post.calls == 2  # cache disabled: every call live
