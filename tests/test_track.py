"""Per-point elevation track building in ``filters.add_elevation``.

The track is what the GPX/GeoJSON exporters turn into per-point ``<ele>`` / 3D coords.
It is the resampled walking line zipped with the elevations the gain pass already
looks up — but only recorded when it is *faithful*:

  * **Direct path** — only when the stitched walking line covers all member ways. A
    branched / gap-split relation whose ``stitch_ways`` drops legs gets gain (from the
    partial line, as before) but NO track, so the export falls back to the raw ways and
    never ships a track missing legs.
  * **Presampled path** (composed loops) — the caller hands in the points behind the
    pre-assembled series, and a composed loop is a single synthesised ring, so it is
    faithful by construction; without the points, gain is unaffected and only the track
    is skipped.
"""
import pytest

from hike_finder.elevation.base import ElevationError, ElevationProvider
from hike_finder.filters import Hike, add_elevation
from hike_finder.geometry import stitch_ways


class _LatRamp(ElevationProvider):
    """Deterministic elevation: 20 km per degree of latitude (0 m at lat 50.0)."""

    SCALE = 20000.0

    def lookup(self, points):
        return [(lat - 50.0) * self.SCALE for lat, _ in points]


class _NeverCalled(ElevationProvider):
    def lookup(self, points):  # pragma: no cover - asserts it is never reached
        raise AssertionError("provider must not be called on the presampled path")


def _hike(ways):
    return Hike(
        osm_id=1, name="T", distance_km=1.0, circular=False, car_access=False,
        chairlift_access=False, start=ways[0][0], ways=ways,
    )


# --- direct path --------------------------------------------------------------


def test_direct_path_builds_track_aligned_with_resampled_elevations():
    way = ((50.0, 14.0), (50.02, 14.0))
    h = _hike((way,))
    line = stitch_ways([list(way)])  # a single clean way stitches to itself
    add_elevation(h, line, _LatRamp(), sample_interval_m=25.0)

    assert h.gain_m is not None
    assert len(h.track) >= 2
    assert h.track[0][:2] == (50.0, 14.0)        # starts at the first vertex, (lat, lon)
    assert h.track[0][2] == pytest.approx(0.0)   # ramp = 0 m at lat 50.0
    # Every point's elevation is the ramp value for its own latitude.
    for lat, _lon, ele in h.track:
        assert ele == pytest.approx((lat - 50.0) * _LatRamp.SCALE)


def test_direct_path_skips_track_when_stitch_drops_members():
    # Two disjoint ways can't chain, so stitch_ways keeps only the first: the stitched
    # line under-covers the summed member length -> not faithful -> no track. Gain is
    # still computed (from the partial line), exactly as before this feature.
    way_a = ((50.0, 14.0), (50.02, 14.0))
    way_b = ((50.5, 15.0), (50.52, 15.0))  # far away; unchainable to way_a
    h = _hike((way_a, way_b))
    line = stitch_ways([list(way_a), list(way_b)])
    assert line == list(way_a)  # confirm the second leg was dropped

    add_elevation(h, line, _LatRamp(), sample_interval_m=25.0)
    assert h.gain_m is not None     # gain still answered from the partial line
    assert h.track == ()            # but no track -> export falls back to raw ways


def test_direct_path_no_track_when_lookup_fails():
    class _Boom(ElevationProvider):
        def lookup(self, points):
            raise ElevationError("down")

    way = ((50.0, 14.0), (50.02, 14.0))
    h = _hike((way,))
    add_elevation(h, stitch_ways([list(way)]), _Boom())
    assert h.gain_m is None and h.track == ()


# --- presampled path (composed loops) -----------------------------------------


def test_presampled_path_builds_track_from_supplied_points():
    # A composed loop: the caller hands in the assembled points + elevations; the
    # provider is never touched and no faithfulness gate applies (single ring).
    h = _hike((((50.0, 14.0), (50.0, 14.01), (50.0, 14.0)),))
    pts = [(50.0, 14.0), (50.0, 14.005), (50.0, 14.01), (50.0, 14.0)]
    eles = [100.0, 150.0, 120.0, 100.0]
    add_elevation(h, [], _NeverCalled(), pre_elevations=eles, pre_points=pts,
                  use_presampled=True)

    assert h.gain_m is not None
    assert h.track == (
        (50.0, 14.0, 100.0), (50.0, 14.005, 150.0),
        (50.0, 14.01, 120.0), (50.0, 14.0, 100.0),
    )


def test_presampled_path_without_points_has_gain_but_no_track():
    h = _hike((((50.0, 14.0), (50.0, 14.01), (50.0, 14.0)),))
    add_elevation(h, [], _NeverCalled(), pre_elevations=[100.0, 150.0, 100.0],
                  pre_points=None, use_presampled=True)
    assert h.gain_m is not None and h.track == ()


def test_presampled_path_degraded_series_keeps_gain_and_track_empty():
    h = _hike((((50.0, 14.0), (50.0, 14.01), (50.0, 14.0)),))
    add_elevation(h, [], _NeverCalled(), pre_elevations=None,
                  pre_points=[(50.0, 14.0)], use_presampled=True)
    assert h.gain_m is None and h.track == ()
