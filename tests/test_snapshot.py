"""Area snapshots: persistence round-trip + faithful offline search.

The load-bearing claim is that an offline search of a snapshot returns *the same*
numbers a live search would — because the unchanged ``find_hikes`` drives a provider
that replays the recorded elevations, and the offline run re-derives the identical
sample points (same saved geometry, same locked ``sample_interval_m``). These tests
prove that without any network: record with a deterministic fake provider, save, load,
search, and compare.
"""
import pytest

from hike_finder.elevation.base import ElevationError, ElevationProvider
from hike_finder.filters import Criteria, find_hikes
from hike_finder.overpass import AreaData
from hike_finder.search import search_snapshot
from hike_finder.snapshot import (
    AreaSnapshot,
    RecordingElevationProvider,
    SnapshotElevationProvider,
    load_snapshot,
    save_snapshot,
)


class _LatRamp(ElevationProvider):
    SCALE = 20000.0

    def lookup(self, points):
        return [(lat - 50.0) * self.SCALE for lat, _ in points]


def _area():
    return AreaData(
        routes=[
            {"id": 1, "name": "North A", "ways": [[(50.0, 14.0), (50.05, 14.0)]], "tags": {}},
            {"id": 2, "name": "North B", "ways": [[(50.0, 14.1), (50.04, 14.1)]], "tags": {}},
        ],
        parking=[{"coord": (50.0, 14.0009), "name": "P"}],
        lifts=[{"stations": [(50.05, 14.0005)], "kind": "gondola", "name": "G"}],
    )


def _record_snapshot(bbox=(49.9, 13.9, 50.2, 14.2)):
    """Mimic download_area's core offline: record every elevation find_hikes resolves."""
    area = _area()
    recorder = RecordingElevationProvider(_LatRamp())
    find_hikes(area, recorder, Criteria(), bbox=bbox)
    return AreaSnapshot(bbox=bbox, area=area, elevations=recorder.samples, sample_interval_m=25.0)


# --------------------------------------------------------------------------- providers


def test_recording_provider_passes_through_and_records():
    rec = RecordingElevationProvider(_LatRamp())
    pts = [(50.0, 14.0), (50.01, 14.0)]
    out = rec.lookup(pts)
    assert out == pytest.approx([0.0, 200.0])
    assert rec.samples[(50.01, 14.0)] == pytest.approx(200.0)


def test_snapshot_provider_answers_and_raises_on_miss():
    prov = SnapshotElevationProvider({(50.0, 14.0): 100.0, (50.01, 14.0): 300.0})
    assert prov.lookup([(50.0, 14.0), (50.01, 14.0)]) == [100.0, 300.0]
    with pytest.raises(ElevationError):
        prov.lookup([(50.0, 14.0), (51.0, 14.0)])  # second point unknown


# --------------------------------------------------------------------------- round-trip


def test_save_load_round_trip_restores_tuples(tmp_path):
    snap = _record_snapshot()
    path = tmp_path / "area.json"
    save_snapshot(snap, path)
    loaded = load_snapshot(path)

    assert loaded.bbox == snap.bbox
    assert loaded.sample_interval_m == 25.0
    assert loaded.route_count == 2
    # Coords must come back as tuples — geometry de-dup/graph code needs them hashable.
    way0 = loaded.area.routes[0]["ways"][0]
    assert isinstance(way0[0], tuple)
    assert isinstance(loaded.area.parking[0]["coord"], tuple)
    assert isinstance(loaded.area.lifts[0]["stations"][0], tuple)
    # Elevation map survives intact (same number of samples, same values).
    assert loaded.sample_count == snap.sample_count


def test_offline_search_matches_online_gain(tmp_path):
    # Online: search the live area with the real (fake) provider.
    area = _area()
    online = find_hikes(area, _LatRamp(), Criteria(), bbox=(49.9, 13.9, 50.2, 14.2))
    online_gain = {h.osm_id: h.gain_m for h in online}

    # Snapshot the same area, save+load (exercises the JSON float-key round-trip),
    # then search offline.
    snap = _record_snapshot()
    path = tmp_path / "area.json"
    save_snapshot(snap, path)
    loaded = load_snapshot(path)
    offline = find_hikes(
        loaded.area,
        SnapshotElevationProvider(loaded.elevations),
        Criteria(),
        bbox=loaded.bbox,
        sample_interval_m=loaded.sample_interval_m,
    )
    offline_gain = {h.osm_id: h.gain_m for h in offline}

    assert offline_gain == online_gain
    # And the gains are real numbers, not degraded-to-None (proves the keys matched).
    assert all(g is not None for g in offline_gain.values())


def test_search_snapshot_applies_filters_offline(tmp_path):
    snap = _record_snapshot()
    path = tmp_path / "area.json"
    save_snapshot(snap, path)
    loaded = load_snapshot(path)

    # Read both routes' gains, then filter to keep only the higher one — all offline.
    all_hikes = search_snapshot(loaded, Criteria())
    assert len(all_hikes) == 2
    gains = sorted(h.gain_m for h in all_hikes)
    cut = (gains[0] + gains[1]) / 2
    kept = search_snapshot(loaded, Criteria(min_gain_m=cut))
    assert len(kept) == 1 and kept[0].gain_m == gains[1]


def test_download_area_warms_and_prunes_over_length(monkeypatch):
    # Mock the two network boundaries; download_area should sample every plausible
    # route AND prune the through-route the over-length guard rejects, so the snapshot
    # has no unsampled dead routes.
    from hike_finder import search as search_mod

    bbox = (50.0, 14.0, 50.01, 14.01)  # ~1.3 km diagonal; guard cut ~5.2 km (x4)
    area = AreaData(
        routes=[
            {"id": 1, "name": "Local", "ways": [[(50.0, 14.0), (50.005, 14.0)]], "tags": {}},
            {"id": 2, "name": "Through", "ways": [[(50.0, 14.0), (50.2, 14.0)]], "tags": {}},  # ~22 km
        ]
    )
    monkeypatch.setattr(search_mod, "fetch_area", lambda *a, **k: area)
    monkeypatch.setattr(search_mod, "get_provider", lambda *a, **k: _LatRamp())

    snap = search_mod.download_area(bbox)
    assert snap.route_count == 1                       # through-route pruned
    assert snap.area.routes[0]["id"] == 1
    assert snap.sample_count > 0                       # the local route was sampled
    assert snap.sample_interval_m == 25.0


def test_search_snapshot_near_miss_offline(tmp_path):
    snap = _record_snapshot()
    loaded_path = tmp_path / "area.json"
    save_snapshot(snap, loaded_path)
    loaded = load_snapshot(loaded_path)

    top = max(h.gain_m for h in search_snapshot(loaded, Criteria()))
    # Demand just above the best route -> zero strict matches -> 'auto' shows near-misses.
    out = search_snapshot(loaded, Criteria(min_gain_m=top + 30), near_miss="auto")
    assert out and all(h.near_miss for h in out)
