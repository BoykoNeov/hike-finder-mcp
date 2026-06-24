"""Near-miss results: the relaxed second look that surfaces "close" routes.

Two layers, both offline:
  * pure classification — ``Criteria.accepts_geometry_relaxed`` / ``near_miss_notes``
    decide what counts as close and spell out how it misses;
  * orchestration — ``find_hikes`` appends near-misses after matches, only pays for
    the relaxed pool's elevation when engaged, and honours the tri-state switch.
"""
from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Criteria, Hike, find_hikes
from hike_finder.geometry import haversine_m
from hike_finder.overpass import AreaData


def _hike(**kw):
    base = dict(
        osm_id=1,
        name="t",
        distance_km=5.0,
        circular=False,
        car_access=False,
        chairlift_access=False,
        start=(50.0, 14.0),
        gain_m=300.0,
        loss_m=300.0,
    )
    base.update(kw)
    return Hike(**base)


# --------------------------------------------------------------------------- pure


def test_near_miss_note_gain_below_minimum():
    c = Criteria(min_gain_m=800.0)
    h = _hike(gain_m=720.0)
    notes = c.near_miss_notes(h, gain_frac=0.2, car_radius_m=300, lift_radius_m=400)
    assert notes is not None
    assert notes == ("gain 720 m — 80 m below the 800 m minimum",)


def test_near_miss_gain_too_far_below_is_dropped():
    # 600 vs an 800 minimum is 200 m short — beyond 20% (160 m) — so NOT a near-miss.
    c = Criteria(min_gain_m=800.0)
    assert c.near_miss_notes(_hike(gain_m=600.0), gain_frac=0.2, car_radius_m=300, lift_radius_m=400) is None


def test_near_miss_note_gain_above_maximum():
    c = Criteria(max_gain_m=500.0)
    notes = c.near_miss_notes(_hike(gain_m=560.0), gain_frac=0.2, car_radius_m=300, lift_radius_m=400)
    assert notes == ("gain 560 m — 60 m above the 500 m maximum",)


def test_near_miss_unknown_gain_against_active_bound_is_dropped():
    c = Criteria(min_gain_m=500.0)
    assert c.near_miss_notes(_hike(gain_m=None), gain_frac=0.2, car_radius_m=300, lift_radius_m=400) is None


def test_near_miss_note_distance_below_minimum():
    c = Criteria(min_distance_km=8.0)
    notes = c.near_miss_notes(_hike(distance_km=7.3), gain_frac=0.2, car_radius_m=300, lift_radius_m=400)
    assert notes == ("7.3 km — 0.7 km below the 8.0 km minimum",)


def test_near_miss_note_access_parking_just_past_limit():
    c = Criteria(car_access=True)
    h = _hike(car_access=False, car_distance_m=380.0)
    notes = c.near_miss_notes(h, gain_frac=0.2, car_radius_m=300, lift_radius_m=400)
    assert notes == ("nearest parking 380 m from an end — just past the 300 m limit",)


def test_near_miss_combines_multiple_reasons():
    c = Criteria(min_gain_m=800.0, car_access=True)
    h = _hike(gain_m=750.0, car_access=False, car_distance_m=340.0)
    notes = c.near_miss_notes(h, gain_frac=0.2, car_radius_m=300, lift_radius_m=400)
    assert notes is not None and len(notes) == 2
    assert any("gain" in n for n in notes) and any("parking" in n for n in notes)


def test_relaxed_geometry_admits_distance_and_access_band():
    c = Criteria(max_distance_km=8.0, car_access=True)
    # 9 km is within the 2 km distance margin; parking at 420 m within 300*1.5=450.
    h = _hike(distance_km=9.0, car_access=False, car_distance_m=420.0)
    assert c.accepts_geometry_relaxed(
        h, dist_km_margin=2.0, radius_frac=0.5, car_radius_m=300, lift_radius_m=400
    ) is True


def test_relaxed_geometry_never_relaxes_shape():
    # A loop is not "almost point-to-point": circular is strict even when relaxed.
    c = Criteria(circular=True)
    h = _hike(circular=False)
    assert c.accepts_geometry_relaxed(
        h, dist_km_margin=2.0, radius_frac=0.5, car_radius_m=300, lift_radius_m=400
    ) is False


def test_relaxed_geometry_keeps_exclusion_strict():
    # Excluding access ("must NOT have a lift") stays strict — "almost excluded" is
    # not a useful near-miss.
    c = Criteria(car_access=False)
    assert c.accepts_geometry_relaxed(
        _hike(car_access=True), dist_km_margin=2.0, radius_frac=0.5, car_radius_m=300, lift_radius_m=400
    ) is False


# --------------------------------------------------------------------------- find_hikes


class _LatRamp(ElevationProvider):
    """Elevation rises linearly with latitude, so a north-bound route's gain scales
    with how far north it reaches — a controllable, deterministic stand-in."""

    SCALE = 20000.0  # 0.01 deg lat (~1.11 km) climbs ~200 m

    def lookup(self, points):
        return [(lat - 50.0) * self.SCALE for lat, _ in points]


def _north_route(osm_id, lon, extent):
    """A straight south->north way starting at (50.0, lon) reaching 50.0+extent."""
    return {"id": osm_id, "name": f"r{osm_id}", "ways": [[(50.0, lon), (50.0 + extent, lon)]], "tags": {}}


def _gain_of(route):
    """The gain _LatRamp yields for a route (read once, so tests aren't brittle)."""
    area = AreaData(routes=[route])
    h = find_hikes(area, _LatRamp(), Criteria())[0]
    return h.gain_m


def test_find_hikes_off_by_default_hides_near_miss():
    route = _north_route(1, 14.0, 0.05)
    g = _gain_of(route)
    area = AreaData(routes=[route])
    # Ask for more gain than the route has, by a small amount: with near_miss off it
    # simply isn't returned.
    out = find_hikes(area, _LatRamp(), Criteria(min_gain_m=g + 50))
    assert out == []


def test_find_hikes_true_appends_flagged_near_miss():
    route = _north_route(1, 14.0, 0.05)
    g = _gain_of(route)
    area = AreaData(routes=[route])
    out = find_hikes(area, _LatRamp(), Criteria(min_gain_m=g + 50), near_miss=True)
    assert len(out) == 1
    h = out[0]
    assert h.near_miss is True
    assert h.notes and "below the" in h.notes[0]


def test_find_hikes_matches_sort_before_near_misses():
    match = _north_route(1, 14.0, 0.055)   # more gain -> a match
    miss = _north_route(2, 14.1, 0.05)     # less gain, but within tolerance -> near-miss
    gm, gn = _gain_of(match), _gain_of(miss)
    assert gm > gn
    area = AreaData(routes=[match, miss])
    # Threshold between them: match passes, miss falls just short (within 20%).
    out = find_hikes(area, _LatRamp(), Criteria(min_gain_m=gm), near_miss=True)
    assert [h.osm_id for h in out] == [1, 2]
    assert out[0].near_miss is False and out[1].near_miss is True


def test_find_hikes_auto_engages_only_when_no_matches():
    match = _north_route(1, 14.0, 0.055)
    miss = _north_route(2, 14.1, 0.05)
    gm, gn = _gain_of(match), _gain_of(miss)
    area = AreaData(routes=[match, miss])

    # A match exists -> 'auto' stays quiet, the near-miss is NOT shown.
    out = find_hikes(area, _LatRamp(), Criteria(min_gain_m=gm), near_miss="auto")
    assert [h.osm_id for h in out] == [1]

    # No match clears the bar -> 'auto' surfaces the near-miss.
    high = max(gm, gn) + 50
    out = find_hikes(area, _LatRamp(), Criteria(min_gain_m=high), near_miss="auto")
    assert any(h.near_miss for h in out)
    assert all(h.near_miss for h in out)  # nothing strictly matched


def test_find_hikes_access_near_miss_end_to_end():
    # The access near-miss chain only fires inside find_hikes: measure_geometry(car_max_m)
    # -> car_distance_m on the Hike -> near_miss_notes. A short east-west route with
    # parking ~380 m past one end and car_access required: strict car_access is False
    # (300 m radius), but it's within the relaxed 450 m, so it comes back flagged.
    end = (50.0, 14.0)
    route = {"id": 1, "name": "ParkRoute", "ways": [[end, (50.0, 14.02)]], "tags": {}}
    parking = [{"coord": (50.0, 13.99469), "name": "P"}]  # ~380 m west of `end`
    area = AreaData(routes=[route], parking=parking)
    out = find_hikes(area, _LatRamp(), Criteria(car_access=True), near_miss=True)
    assert len(out) == 1
    h = out[0]
    assert h.car_access is False and h.near_miss is True
    assert h.notes and "parking" in h.notes[0] and "just past" in h.notes[0]


def test_find_hikes_distance_near_miss_from_relaxed_pool():
    # A route just over a max-distance cut is admitted only via the relaxed pool, then
    # reported as a near-miss with a distance note. Confirms the relaxed cheap pass +
    # its deferred elevation actually wire together end to end.
    route = _north_route(1, 14.0, 0.05)
    dist_km = haversine_m((50.0, 14.0), (50.05, 14.0)) / 1000.0
    area = AreaData(routes=[route])
    out = find_hikes(area, _LatRamp(), Criteria(max_distance_km=dist_km - 0.5), near_miss=True)
    assert len(out) == 1 and out[0].near_miss is True
    assert "above the" in out[0].notes[0]
