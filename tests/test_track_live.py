"""The per-point elevation track gate, validated on REAL multi-member geometry.

`add_elevation` records `Hike.track` (the export's `<ele>` source) only when the
stitched walking line *faithfully* covers all member ways — otherwise a branched /
gap-split relation would export a track silently missing the legs `stitch_ways` drops.
The unit tests (test_track.py) prove both branches on synthetic geometry; this pins
them on the live `spindl_area.json` fixture (one real Overpass round-trip, 15 routes
incl. the two fragmented relations the HANDOFF flagged: the route named "4207" / id
237097, and "Medvědí okruh" / id 6285306).

The gate is geometry-only — it doesn't depend on elevation *values* — so a deterministic
ramp provider makes the whole check offline and reproducible.
"""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from hike_finder.elevation.base import ElevationProvider
from hike_finder.export import hikes_to_geojson, hikes_to_gpx
from hike_finder.filters import add_elevation, measure_geometry
from hike_finder.geometry import polyline_length_m, total_way_length_m
from hike_finder.overpass import parse_area

FIXTURE = Path(__file__).parent / "fixtures" / "spindl_area.json"
GPX_NS = {"g": "http://www.topografix.com/GPX/1/1"}

# The two relations whose members can't be chained into one line (HANDOFF: 36/70 and
# 19/31 members dropped) — the routes the gate must reject.
FRAGMENTED_IDS = {237097, 6285306}


class _LatRamp(ElevationProvider):
    def lookup(self, points):
        return [(lat - 50.0) * 20000.0 for lat, _ in points]


def _measured_hikes():
    area = parse_area(json.loads(FIXTURE.read_text(encoding="utf-8"))["elements"])
    prov = _LatRamp()
    out = []
    for r in area.routes:
        measured = measure_geometry(r, area.parking, area.lifts)
        assert measured is not None, r["id"]
        hike, line = measured
        add_elevation(hike, line, prov, sample_interval_m=25.0)
        summed = total_way_length_m([list(w) for w in hike.ways])
        faithful = summed > 0 and polyline_length_m(line) >= summed * 0.98
        out.append((r["id"], hike, faithful))
    return out


def test_track_recorded_iff_stitch_is_faithful_on_every_real_route():
    # The load-bearing invariant on real geometry: a per-point track exists exactly
    # when the stitch covered all member ways. No clean route loses its profile; no
    # fragmented route exports a track missing legs.
    for rid, hike, faithful in _measured_hikes():
        assert bool(hike.track) == faithful, f"{rid} {hike.name}: track={bool(hike.track)} faithful={faithful}"


def test_gate_fires_on_exactly_the_two_fragmented_relations():
    fragmented = {rid for rid, _h, faithful in _measured_hikes() if not faithful}
    assert fragmented == FRAGMENTED_IDS
    # And the 13 faithful routes really did get a track (not all-empty by accident).
    assert sum(1 for _rid, h, _f in _measured_hikes() if h.track) == 13


def test_fragmented_route_keeps_full_geometry_without_elevation():
    # The fallback must be the FULL raw-ways geometry (every leg), not the partial
    # stitched line — and carry no <ele>. Distance integrity is the whole reason.
    measured = {rid: h for rid, h, _f in _measured_hikes()}
    okruh = measured[6285306]
    assert okruh.track == ()
    assert okruh.gain_m is not None  # gain is still computed (from the partial line)
    feat = json.loads(hikes_to_geojson([okruh]))["features"][0]
    # raw-ways MultiLineString: one segment per member way, plain 2D [lon, lat].
    coords = feat["geometry"]["coordinates"]
    assert len(coords) >= 2 and len(coords[0][0]) == 2


def test_gpx_and_geojson_output_match_the_gate_per_route():
    hikes = [h for _rid, h, _f in _measured_hikes()]
    root = ET.fromstring(hikes_to_gpx(hikes))
    by_name = {t.find("g:name", GPX_NS).text: t for t in root.findall("g:trk", GPX_NS)}
    gj = json.loads(hikes_to_geojson(hikes))

    for hike, feat in zip(hikes, gj["features"]):
        name = hike.place_name or hike.name or "hike"
        trk = by_name["~ " + name] if ("~ " + name) in by_name else by_name.get(name)
        assert trk is not None, name
        segs = trk.findall("g:trkseg", GPX_NS)
        eles = trk.findall(".//g:ele", GPX_NS)
        first_coord = feat["geometry"]["coordinates"][0][0]
        if hike.track:
            assert len(segs) == 1                       # one clean walking track
            assert len(eles) == len(hike.track) > 0     # <ele> on every point
            assert len(first_coord) == 3                # GeoJSON 3D [lon, lat, ele]
        else:
            assert eles == []                           # fallback carries no elevation
            assert len(first_coord) == 2                # GeoJSON 2D [lon, lat]
