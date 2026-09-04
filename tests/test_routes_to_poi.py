"""routes_to_poi — drawing a route TO the nearest church / ruin / peak.

The orchestration (fetch → pick candidates → snap all → dijkstra per candidate → rank →
assemble → measure) runs on hand-built coordinate graphs with the network stubbed out, so
the parts that can be *confidently wrong* are pinned offline:

  * "nearest" means nearest ALONG THE TRAILS, not as the crow flies;
  * the three empty-result causes stay distinguishable, because they need different fixes;
  * the certificate fires when the answer might not be the true nearest — including the
    case where it demonstrably ISN'T, which is the whole reason the warning exists.

Coordinates are spaced far above the 1 m weld tolerance, so distinct vertices never fuse.
"""
import json
import logging

import pytest

from hike_finder import search as S
from hike_finder.filters import Criteria
from hike_finder.format import format_hike
from hike_finder.overpass import AreaData


class _FlatProvider:
    """Deterministic elevation: dead flat, no network. Gain is not what these test."""

    def lookup(self, pts):
        return [500.0 for _ in pts]


def _route(ref, ways, rid):
    return {"id": rid, "name": ref, "ref": ref, "osmc_color": None, "tags": {}, "ways": ways}


def _poi(kind, name, coord):
    return {"coord": coord, "kind": kind, "name": name}


@pytest.fixture
def stub_network(monkeypatch):
    """Return a runner that executes routes_to_poi fully offline."""

    def run(start, kinds, routes, pois, *, criteria=None, **kwargs):
        monkeypatch.setattr(
            S,
            "_fetch_area",
            lambda *a, **k: AreaData(routes=routes, parking=[], lifts=[], pois=pois),
        )
        monkeypatch.setattr(S, "_provider", lambda *a, **k: _FlatProvider())
        monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)
        return S.routes_to_poi(start, kinds, criteria or Criteria(), **kwargs)

    return run


# The layout that separates crow-flies from trail distance.
#
#     W •---------------• N          W and N are joined; N hangs off the east end E.
#       |               |
#     START •-----------• E          the only way north is: east to E, then north, then west.
#
# A ruin at W is CLOSER in a straight line than one at E, and much farther on foot.
START = (50.000, 15.000)
E = (50.000, 15.020)      # ~1.43 km east of START
N = (50.010, 15.020)      # ~1.11 km north of E
W = (50.010, 15.002)      # ~1.29 km west of N — and only ~1.11 km from START in a line
_LOOPLESS = [
    _route("east", [[START, E]], 1),
    _route("north", [[E, N]], 2),
    _route("west", [[N, W]], 3),
]
ALONG = _poi("ruins", "along", (50.0002, 15.0195))   # by E: ~1.4 km both ways
ACROSS = _poi("ruins", "across", (50.0098, 15.0025))  # by W: ~1.1 km in a line, ~3.8 km on foot


# ----------------------------------------------------------------- nearest by TRAIL


def test_nearest_means_nearest_along_the_trails(stub_network):
    # ACROSS wins on straight-line distance and loses on foot — the walk is what counts,
    # so the single route returned goes to ALONG.
    hikes = stub_network(START, ("ruins",), _LOOPLESS, [ACROSS, ALONG], n=1)
    assert len(hikes) == 1
    assert hikes[0].destination.name == "along"


def test_more_destinations_come_back_nearest_first(stub_network):
    # ACROSS needs a cap raise to survive at all: 1.1 km in a line but 3.8 km on foot is
    # past the default "3x the straight-line distance" allowance — which is the per-
    # destination cap doing its job, not a bug (a ruin you can see should not license an
    # arbitrarily long walk unless you say so).
    hikes = stub_network(
        START, ("ruins",), _LOOPLESS, [ACROSS, ALONG], n=2,
        criteria=Criteria(max_distance_km=6.0),
    )
    assert [h.destination.name for h in hikes] == ["along", "across"]
    assert hikes[0].distance_km < hikes[1].distance_km


def test_the_per_destination_cap_drops_a_long_way_round(stub_network):
    # The same pair WITHOUT the raised cap: only the one you can actually walk to sensibly.
    hikes = stub_network(START, ("ruins",), _LOOPLESS, [ACROSS, ALONG], n=2)
    assert [h.destination.name for h in hikes] == ["along"]


def test_several_kinds_are_or_ed(stub_network):
    # "the nearest ruin OR castle" — the castle by E wins over the ruin the long way round.
    castle = _poi("castle", "hrad", (50.0002, 15.0195))
    hikes = stub_network(START, ("ruins", "castle"), _LOOPLESS, [ACROSS, castle], n=1)
    assert hikes[0].destination.kind == "castle"


def test_the_route_starts_where_you_picked(stub_network):
    hikes = stub_network(START, ("ruins",), _LOOPLESS, [ALONG], n=1)
    assert hikes[0].start == START


# --------------------------------------------------------- honesty about arriving


def test_destination_names_the_route_and_reports_the_gap_it_leaves(stub_network):
    # The route ends at the nearest point ON THE TRAIL, not at the ruin. That gap is
    # measured, carried on the Hike, and rendered as "ends N m from" — never "arrives at".
    hikes = stub_network(START, ("ruins",), _LOOPLESS, [ALONG], n=1)
    h = hikes[0]
    assert h.name == "Route to ruin “along”"
    assert 0.0 < h.destination.distance_m < 100.0   # ALONG sits ~25 m off the trail
    line = format_hike(h)
    assert "ends " in line and " m from the ruin]" in line
    assert "arrives" not in line


def test_an_unnamed_destination_still_names_the_route(stub_network):
    hikes = stub_network(
        START, ("ruins",), _LOOPLESS, [_poi("ruins", None, (50.0002, 15.0195))], n=1
    )
    assert hikes[0].name == "Route to ruin"


def test_a_route_drawn_to_a_poi_is_not_a_loop(stub_network):
    hikes = stub_network(START, ("ruins",), _LOOPLESS, [ALONG], n=1)
    assert hikes[0].circular is False


def test_a_stray_circular_filter_cannot_empty_the_result(stub_network):
    # A drawn route is synthesised, never a mapped circular relation — a `circular` filter
    # left on from another search must not silently drop every result (as in routes_between).
    hikes = stub_network(
        START, ("ruins",), _LOOPLESS, [ALONG], n=1, criteria=Criteria(circular=True)
    )
    assert len(hikes) == 1


# ------------------------------------------------- the three empty-result causes


def test_nothing_of_that_kind_mapped_is_said_out_loud(stub_network, caplog):
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network(
            START, ("ruins",), _LOOPLESS, [_poi("church", "kostel", (50.0002, 15.0195))]
        )
    assert hikes == []
    assert "is mapped within" in caplog.text
    # The lever, and the honesty note that a miss is about the MAP, not the world.
    assert "search radius" in caplog.text and "*mapped*" in caplog.text


def test_an_off_network_destination_is_reported_as_such(stub_network, caplog):
    # A ruin 3.3 km from the nearest trail (the northern W-N leg) is beyond the 2 km snap
    # limit: it exists, it is simply not walkable to on marked trails. A different failure
    # from "none mapped", and it needs a different fix.
    far = _poi("ruins", "middle of nowhere", (50.040, 15.010))
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network(START, ("ruins",), _LOOPLESS, [far], search_radius_m=6000)
    assert hikes == []
    assert "1 off-network" in caplog.text


def test_a_disconnected_destination_is_reported_as_such(stub_network, caplog):
    # A ruin sitting on a trail that shares no junction with the start's network.
    island = _route("island", [[(50.005, 15.010), (50.005, 15.014)]], 9)
    on_island = _poi("ruins", "unreachable", (50.0051, 15.012))
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network(START, ("ruins",), [*_LOOPLESS, island], [on_island])
    assert hikes == []
    assert "not connected to your start" in caplog.text


def test_a_destination_past_the_length_cap_is_reported_as_such(stub_network, caplog):
    # ACROSS is ~3.8 km on foot; a 2 km cap puts it out of reach without pretending it
    # isn't there.
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network(
            START, ("ruins",), _LOOPLESS, [ACROSS], criteria=Criteria(max_distance_km=2.0)
        )
    assert hikes == []
    assert "longer than the cap" in caplog.text


def test_a_start_off_the_network_bails_before_routing(stub_network, caplog):
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network(
            (50.030, 15.010), ("ruins",), _LOOPLESS, [ALONG], search_radius_m=6000
        )
    assert hikes == []
    assert "your start point is" in caplog.text


# ------------------------------------------------------------------ the certificate


def test_a_provable_answer_claims_nothing_extra(stub_network, caplog):
    # The route returned (~1.4 km on foot) is shorter than the 3 km search radius, so no
    # object outside that radius can beat it — the answer IS the nearest, and says nothing.
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        stub_network(START, ("ruins",), _LOOPLESS, [ACROSS, ALONG], n=1)
    assert "not provably the nearest" not in caplog.text


def test_a_route_longer_than_the_search_radius_admits_it_may_not_be_nearest(
    stub_network, caplog
):
    # Only ACROSS is in scope, and reaching it takes ~3.8 km on foot — past the 1.5 km
    # radius that was actually looked at. Something nearer could be sitting just outside.
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network(
            START, ("ruins",), _LOOPLESS, [ACROSS], n=1,
            search_radius_m=1500, criteria=Criteria(max_distance_km=6.0),
        )
    assert len(hikes) == 1
    assert "not provably the nearest" in caplog.text
    assert "search radius" in caplog.text


def test_the_cheap_pass_admits_when_it_may_have_dropped_a_nearer_one(stub_network, caplog):
    # Eleven candidates: ten clustered by W (near in a line, far on foot) and ALONG, which
    # is farther in a line and so ranks eleventh — outside the cheap pass's window. The
    # answer returned is genuinely NOT the nearest, and the certificate is what says so
    # rather than leaving the superlative standing.
    crowd = [
        _poi("ruins", f"across-{i}", (50.0098 + i * 0.00002, 15.0025)) for i in range(10)
    ]
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network(
            START, ("ruins",), _LOOPLESS, [*crowd, ALONG], n=1,
            criteria=Criteria(max_distance_km=6.0),
        )
    assert len(hikes) == 1
    assert hikes[0].destination.name.startswith("across")   # the truly nearest was dropped
    assert "not provably the nearest" in caplog.text
    assert "candidate not examined" in caplog.text


# ------------------------------------------------------------------------ validation


def test_an_unknown_kind_raises_rather_than_matching_nothing(stub_network):
    with pytest.raises(ValueError, match="unknown point-of-interest kind"):
        stub_network(START, ("dragon",), _LOOPLESS, [ALONG])


def test_no_kind_at_all_raises(stub_network):
    with pytest.raises(ValueError, match="at least one destination kind"):
        stub_network(START, (), _LOOPLESS, [ALONG])


# --------------------------------------------------------------- the bbox argument


def test_the_fetched_box_is_padded_by_the_length_cap(monkeypatch):
    # The completeness argument, pinned: a shortest path of length L has every vertex
    # within L of its start, so padding the box by the route length cap makes any route
    # within that cap unclippable. --max-distance therefore sizes the FETCH, not just the
    # results — the tight-pad precedent of compose_loops_around, not the accepted-clipping
    # one of routes_between.
    seen = {}

    def _capture(bbox, *a, **k):
        seen["bbox"] = bbox
        return AreaData(routes=_LOOPLESS, parking=[], lifts=[], pois=[ALONG])

    monkeypatch.setattr(S, "_fetch_area", _capture)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _FlatProvider())
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)
    S.routes_to_poi(START, ("ruins",), Criteria(max_distance_km=5.0))

    south, west, north, east = seen["bbox"]
    from hike_finder.geometry import haversine_m

    # 5 km cap -> at least 5 km of pad in every direction (never less, or a qualifying
    # route could be clipped and the "nearest" claim would quietly break).
    assert haversine_m(START, (north, START[1])) >= 5000.0
    assert haversine_m(START, (south, START[1])) >= 5000.0
    assert haversine_m(START, (START[0], east)) >= 5000.0
    assert haversine_m(START, (START[0], west)) >= 5000.0


# ------------------------------------------------------- a short answer is not a silent one


def test_a_partial_answer_says_what_was_skipped(stub_network, caplog):
    # Two ruins asked for, one drawn: the counters that explain an EMPTY result explain a
    # short one too, so the skipped candidate is named rather than quietly dropped.
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        hikes = stub_network(START, ("ruins",), _LOOPLESS, [ACROSS, ALONG], n=2)
    assert len(hikes) == 1
    assert "1 only via a route past the length cap" in caplog.text
    assert "not the 2 you asked for" in caplog.text


def test_a_complete_answer_stays_quiet(stub_network, caplog):
    with caplog.at_level(logging.WARNING, logger="hike_finder.search"):
        stub_network(START, ("ruins",), _LOOPLESS, [ALONG], n=1)
    assert "you asked for" not in caplog.text
    assert "skipped along the way" not in caplog.text


# ------------------------------------------------------------------ export (GPX/GeoJSON)


def test_a_route_to_a_poi_exports_with_its_non_ascii_name(stub_network):
    """The last seam: a drawn route goes out to GPX/GeoJSON like any other.

    The destination-derived name carries the object's own (usually non-ASCII) name, and
    GPX is XML — so this pins that the name survives serialisation rather than assuming a
    generic export path handles it.
    """
    import xml.etree.ElementTree as ET

    from hike_finder.export import hikes_to_geojson, hikes_to_gpx

    hikes = stub_network(
        START, ("ruins",), _LOOPLESS, [_poi("ruins", "Zřícenina Rotštejn", (50.0002, 15.0195))],
        n=1,
    )
    gpx = hikes_to_gpx(hikes)
    assert ET.fromstring(gpx).tag.endswith("gpx")
    assert "Zřícenina Rotštejn" in gpx
    obj = json.loads(hikes_to_geojson(hikes))
    assert obj["type"] == "FeatureCollection" and len(obj["features"]) == 1
    assert "Zřícenina Rotštejn" in obj["features"][0]["properties"]["name"]
