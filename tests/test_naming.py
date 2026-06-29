"""Pure-layer tests for reverse-geocode naming (no network).

`label_endpoints` and `compose_label` are pure; `enrich_names` is exercised with a
stub geocoder so the whole module is testable offline.
"""
from __future__ import annotations

from hike_finder import config as config_mod
from hike_finder import search as search_mod
from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Criteria, Hike
from hike_finder.format import format_hike
from hike_finder.naming import compose_label, enrich_names, label_endpoints
from hike_finder.overpass import AreaData


# -- label_endpoints --------------------------------------------------------

def _hike(**kw):
    base = dict(
        osm_id=1, name="route/1", distance_km=5.0, circular=False,
        car_access=False, chairlift_access=False, start=(0.0, 0.0),
    )
    base.update(kw)
    return Hike(**base)


def test_label_endpoints_linear_picks_far_end():
    ways = (((0.0, 0.0), (0.0, 1.0), (0.0, 2.0)),)
    start, end = label_endpoints(ways, (0.0, 0.0), circular=False)
    assert start == (0.0, 0.0)
    assert end == (0.0, 2.0)  # the terminus farthest from start


def test_label_endpoints_loop_has_no_far_end():
    # circular short-circuits regardless of geometry: a loop has no meaningful end.
    ways = (((0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.0, 0.0)),)
    start, end = label_endpoints(ways, (0.0, 0.0), circular=True)
    assert start == (0.0, 0.0)
    assert end is None


def test_label_endpoints_branched_is_deterministic_and_far():
    # Y-shape: junction at (0,1), three termini. Far end from (0,0) is the most distant.
    ways = (
        ((0.0, 0.0), (0.0, 1.0)),
        ((0.0, 1.0), (0.0, 2.0)),
        ((0.0, 1.0), (1.0, 1.0)),
    )
    s1, e1 = label_endpoints(ways, (0.0, 0.0), circular=False)
    s2, e2 = label_endpoints(ways, (0.0, 0.0), circular=False)
    assert (s1, e1) == (s2, e2)  # deterministic run-to-run
    assert s1 == (0.0, 0.0)
    assert e1 == (0.0, 2.0)  # 2 deg lon east > the (1,1) branch from (0,0)


def test_label_endpoints_none_start():
    assert label_endpoints((), None, circular=False) == (None, None)


def test_label_endpoints_single_point_route_has_no_end():
    # Degenerate: start is the only candidate -> no distinct far end.
    ways = (((0.0, 0.0),),)
    start, end = label_endpoints(ways, (0.0, 0.0), circular=False)
    assert start == (0.0, 0.0)
    assert end is None


# -- compose_label ----------------------------------------------------------

def test_compose_label_point_to_point():
    assert compose_label("Pec", "Sněžka", circular=False) == "Pec → Sněžka"


def test_compose_label_loop():
    assert compose_label("Špindl", None, circular=True) == "loop near Špindl"


def test_compose_label_loop_without_place_is_none():
    assert compose_label(None, None, circular=True) is None


def test_compose_label_same_place_reads_near():
    assert compose_label("Pec", "Pec", circular=False) == "near Pec"


def test_compose_label_one_sided():
    assert compose_label("Pec", None, circular=False) == "near Pec"
    assert compose_label(None, "Sněžka", circular=False) == "near Sněžka"


def test_compose_label_nothing_resolved():
    assert compose_label(None, None, circular=False) is None
    assert compose_label("  ", "", circular=False) is None


# -- enrich_names (stub geocoder) -------------------------------------------

class StubGeocoder:
    def __init__(self, table):
        self.table = table
        self.calls = []

    def reverse(self, point):
        self.calls.append(point)
        return self.table.get(point)


def test_enrich_names_labels_only_unnamed_routes():
    linear = _hike(
        osm_id=10, unnamed=True, circular=False, start=(0.0, 0.0),
        ways=(((0.0, 0.0), (0.0, 1.0), (0.0, 2.0)),),
    )
    named = _hike(osm_id=11, name="Hřebenovka", unnamed=False, start=(0.0, 0.0))
    loop = _hike(
        osm_id=12, unnamed=True, circular=True, start=(5.0, 5.0),
        ways=(((5.0, 5.0), (5.0, 6.0), (6.0, 6.0), (5.0, 5.0)),),
    )
    geo = StubGeocoder({
        (0.0, 0.0): "Alpha", (0.0, 2.0): "Beta", (5.0, 5.0): "Gamma",
    })

    n = enrich_names([linear, named, loop], geo)

    assert n == 2
    assert linear.place_name == "Alpha → Beta"
    assert loop.place_name == "loop near Gamma"
    assert named.place_name is None  # a signed route is never relabelled
    assert (0.0, 0.0) not in [named.start] or named.place_name is None


def test_enrich_names_skips_composed_loops():
    comp = _hike(osm_id=-1, unnamed=True, composed=True, start=(0.0, 0.0),
                 ways=(((0.0, 0.0), (0.0, 1.0)),))
    geo = StubGeocoder({(0.0, 0.0): "Anywhere"})
    assert enrich_names([comp], geo) == 0
    assert comp.place_name is None
    assert geo.calls == []  # never even queried


def test_enrich_names_geocode_miss_leaves_fallback():
    h = _hike(osm_id=20, unnamed=True, circular=False, start=(0.0, 0.0),
              ways=(((0.0, 0.0), (0.0, 1.0), (0.0, 2.0)),))
    geo = StubGeocoder({})  # nothing resolves
    assert enrich_names([h], geo) == 0
    assert h.place_name is None


# -- format_hike rendering (honest 'unnamed' marker) ------------------------

def test_format_hike_shows_place_label_and_marks_unnamed():
    h = _hike(osm_id=77, name="route/77", unnamed=True, place_name="Pec → Sněžka",
              gain_m=100, loss_m=90, distance_km=5.0)
    line = format_hike(h)
    assert line.startswith("Pec → Sněžka — 5.0 km")
    assert "unnamed OSM relation 77" in line  # provenance: not a signed name
    assert "route/77" not in line


def test_format_hike_unnamed_without_label_keeps_fallback():
    h = _hike(osm_id=77, name="route/77", unnamed=True, place_name=None)
    line = format_hike(h)
    assert "route/77 —" in line
    assert "OSM relation 77" in line and "unnamed OSM relation" not in line


# -- search_hikes wiring (name_places plumbing) -----------------------------

class _FlatElev(ElevationProvider):
    def lookup(self, points):
        return [100.0] * len(points)


def _two_route_area():
    unnamed = {
        "id": 1, "name": "route/1", "ref": None, "unnamed": True, "tags": {},
        "ways": [[(50.70, 15.58), (50.71, 15.59), (50.72, 15.60)]],
    }
    named = {
        "id": 2, "name": "Hřebenovka", "ref": None, "unnamed": False, "tags": {},
        "ways": [[(50.70, 15.61), (50.71, 15.62)]],
    }
    return AreaData(routes=[unnamed, named], parking=[], lifts=[])


def _patch_seams(monkeypatch, geo_or_raiser):
    monkeypatch.setattr(search_mod, "_fetch_area", lambda *a, **k: _two_route_area())
    monkeypatch.setattr(search_mod, "_provider", lambda *a, **k: _FlatElev())
    monkeypatch.setattr(search_mod, "_geocoder", geo_or_raiser)


def test_search_hikes_enriches_only_unnamed_when_name_places(monkeypatch):
    geo = StubGeocoder({(50.70, 15.58): "Alpha", (50.72, 15.60): "Beta"})
    _patch_seams(monkeypatch, lambda cfg, cache: geo)
    cfg = config_mod.load()
    cfg.cache_enabled = False  # don't touch the real on-disk cache
    hikes = search_mod.search_hikes(
        (50.69, 15.57, 50.73, 15.63), Criteria(), cfg, name_places=True
    )
    by_id = {h.osm_id: h for h in hikes}
    assert by_id[1].place_name == "Alpha → Beta"  # unnamed route -> derived label
    assert by_id[2].place_name is None            # signed route -> untouched


def test_search_hikes_skips_geocode_when_flag_off(monkeypatch):
    def _no_geocoder(*a, **k):
        raise AssertionError("geocoder must not be built when name_places is off")

    _patch_seams(monkeypatch, _no_geocoder)
    cfg = config_mod.load()
    cfg.cache_enabled = False
    # name_places omitted -> follows cfg.geocode_enabled (off by default): no geocode.
    hikes = search_mod.search_hikes((50.69, 15.57, 50.73, 15.63), Criteria(), cfg)
    assert all(h.place_name is None for h in hikes)


def test_search_snapshot_name_places_is_logged_noop(caplog):
    """Offline naming can't geocode (no network) — honour offline==online loudly:
    log the no-op rather than silently dropping the request."""
    import logging

    from hike_finder.snapshot import AreaSnapshot

    snap = AreaSnapshot(
        bbox=(50.69, 15.57, 50.73, 15.63), area=_two_route_area(),
        elevations={}, sample_interval_m=25.0,
    )
    cfg = config_mod.load()
    with caplog.at_level(logging.WARNING):
        hikes = search_mod.search_snapshot(snap, Criteria(), cfg, name_places=True)
    assert any("name_places" in r.message for r in caplog.records)  # told the user
    assert all(h.place_name is None for h in hikes)                 # genuinely no-op
