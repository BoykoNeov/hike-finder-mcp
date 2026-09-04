"""The point modes apply the gain and length filters their tools now advertise.

``test_frontend_parity.py`` checks a filter is DECLARED in each tool's schema; nothing
there checks the mode does anything with it. That is the half that matters here, because
these four modes are not ``find_hikes``: they synthesise routes rather than reporting
mapped relations, and each reaches the shared filter through a different orchestration
(loop composition, Yen's k-shortest, a chained dijkstra, a per-destination route). A
schema promising `min_gain_m` on a mode that quietly ignored it would be this repo's own
recurring failure — a label promising what the selector never checked.

So one test per newly-advertised filter per mode, run offline on hand-built graphs with a
deterministic elevation provider. Where a mode returns several routes the filter is asked
to SELECT (one route survives, one does not), which is stronger than an empty result: an
emptied list is also what a filter that broke the search would produce.

Coordinates are spaced far above the 1 m weld tolerance, so distinct vertices never fuse.
"""
import pytest

from hike_finder import search as S
from hike_finder.filters import Criteria
from hike_finder.overpass import AreaData


class _RisingProvider:
    """Deterministic elevation: rises 100 000 m per degree north — no network.

    So a route reaching 0.01° north of 50.0 has climbed ~1000 m, and one that stays on
    the 50.0 parallel is dead flat. Gain is then a property of the LAYOUT below, which is
    what lets each test say which route the filter must keep.
    """

    def lookup(self, pts):
        return [(lat - 50.0) * 100000.0 for (lat, _lon) in pts]


def _route(ref, ways, rid):
    return {"id": rid, "name": ref, "ref": ref, "osmc_color": None, "tags": {}, "ways": ways}


def _poi(kind, name, coord):
    return {"coord": coord, "kind": kind, "name": name}


@pytest.fixture
def offline(monkeypatch):
    """Run any point mode against hand-built routes, with no network and known gain."""

    def stub(routes, pois=()):
        monkeypatch.setattr(
            S,
            "_fetch_area",
            lambda *a, **k: AreaData(routes=routes, parking=[], lifts=[], pois=list(pois)),
        )
        monkeypatch.setattr(S, "_provider", lambda *a, **k: _RisingProvider())
        monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)

    return stub


# ------------------------------------------------------------------- routes_between
#
# Junctions S and T joined by two distinct trails: FLAT runs straight along the 50.0
# parallel (no climb, and the shorter of the two at ~2.1 km); HILL bows ~1.1 km north and
# back, so it climbs ~1000 m and runs ~3.1 km. Stubs keep S and T degree-3 junctions so the
# two stay distinct segments instead of contracting into one ring.
S_PT = (50.000, 15.000)
T_PT = (50.000, 15.030)
_TWO_WAYS = [
    _route("flat", [[S_PT, (50.000, 15.015), T_PT]], 1),
    _route("hill", [[S_PT, (50.010, 15.015), T_PT]], 2),
    _route("stubS", [[S_PT, (50.000, 14.990)]], 3),
    _route("stubT", [[T_PT, (50.000, 15.040)]], 4),
]


def test_routes_between_finds_both_ways_unfiltered(offline):
    """The baseline the two filter tests select from: one flat short route, one hilly
    long one, and it is the flat one that comes first."""
    offline(_TWO_WAYS)
    hikes = S.routes_between(S_PT, T_PT, Criteria(), k=2)
    assert len(hikes) == 2
    flat, hill = hikes                      # shortest first
    assert flat.gain_m < 50.0 and flat.distance_km < 2.5
    assert hill.gain_m > 900.0 and hill.distance_km > 3.0


def test_routes_between_applies_the_gain_filter(offline):
    offline(_TWO_WAYS)
    climbing = S.routes_between(S_PT, T_PT, Criteria(min_gain_m=500.0), k=2)
    assert [h.gain_m > 900.0 for h in climbing] == [True]   # the flat route is dropped
    gentle = S.routes_between(S_PT, T_PT, Criteria(max_gain_m=500.0), k=2)
    assert [h.gain_m < 50.0 for h in gentle] == [True]      # and the hilly one is


def test_routes_between_applies_the_minimum_length(offline):
    """`min_distance_km` filters the shortest-first alternatives — it does not make the
    search hunt for a longer way round, which is why the tool description says so."""
    offline(_TWO_WAYS)
    long_only = S.routes_between(S_PT, T_PT, Criteria(min_distance_km=2.6), k=2)
    assert [h.distance_km > 3.0 for h in long_only] == [True]
    # Nothing in the network is 20 km long: the filter empties rather than inventing one.
    assert S.routes_between(S_PT, T_PT, Criteria(min_distance_km=20.0), k=2) == []


# ------------------------------------------------------------------- circular_routes
#
# Two separate rings, both passing within 500 m of the picked point between them: LOW is
# a shallow 0.002° band (~220 m of climb), HIGH a 0.010° one (~1000 m). Both sit inside
# the derived box, so the only thing that can tell them apart is the gain filter.
AROUND = (50.000, 15.015)
_LOW_RING = [(50.000, 15.000), (50.000, 15.010), (50.002, 15.010), (50.002, 15.000),
             (50.000, 15.000)]
_HIGH_RING = [(50.000, 15.020), (50.000, 15.030), (50.010, 15.030), (50.010, 15.020),
              (50.000, 15.020)]
_TWO_RINGS = [_route("low", [_LOW_RING], 1), _route("high", [_HIGH_RING], 2)]
# The rings are ~1.9 km and ~3.7 km round, so the default 3-15 km target band would drop
# the low one before any gain filter saw it. Every circular_routes case below carries this.
_BAND = {"min_distance_km": 1.0, "max_distance_km": 6.0}


def test_circular_routes_finds_both_rings_unfiltered(offline):
    offline(_TWO_RINGS)
    hikes = S.compose_loops_around(AROUND, Criteria(**_BAND), radius_m=500.0)
    assert sorted(round(h.gain_m / 100.0) for h in hikes) == [2, 10]


def test_circular_routes_applies_the_gain_filter(offline):
    offline(_TWO_RINGS)
    climbing = S.compose_loops_around(
        AROUND, Criteria(min_gain_m=500.0, **_BAND), radius_m=500.0
    )
    assert [h.gain_m > 900.0 for h in climbing] == [True]    # only the high ring
    gentle = S.compose_loops_around(
        AROUND, Criteria(max_gain_m=500.0, **_BAND), radius_m=500.0
    )
    assert [h.gain_m < 300.0 for h in gentle] == [True]      # only the low one


# ------------------------------------------------------------------------- route_via
#
# One route is drawn through the given points, so the filters DISCARD it rather than
# choosing between alternatives — the tool description says that too. Linking S to the
# top of the northern bow and on to T forces the climbing route.
_VIA = [(50.000, 15.000), (50.010, 15.015), (50.000, 15.030)]


def test_route_via_draws_the_climbing_route_unfiltered(offline):
    offline(_TWO_WAYS)
    (hike,) = S.route_via(_VIA, Criteria())
    assert hike.gain_m > 900.0


def test_route_via_applies_the_gain_filter(offline):
    offline(_TWO_WAYS)
    assert S.route_via(_VIA, Criteria(min_gain_m=2000.0)) == []   # asks for more climb
    assert S.route_via(_VIA, Criteria(max_gain_m=100.0)) == []    # asks for less
    assert len(S.route_via(_VIA, Criteria(min_gain_m=500.0, max_gain_m=2000.0))) == 1


# ---------------------------------------------------------------------- routes_to_poi
#
# The layout from test_routes_to_poi: the only way north is east to E, then north, then
# west — so a ruin by W is nearer in a straight line and much farther on foot. Two ruins,
# one ~1.4 km along the trails and one ~3.8 km, which `min_distance_km` can select from.
POI_START = (50.000, 15.000)
_E = (50.000, 15.020)
_N = (50.010, 15.020)
_W = (50.010, 15.002)
_LOOPLESS = [
    _route("east", [[POI_START, _E]], 1),
    _route("north", [[_E, _N]], 2),
    _route("west", [[_N, _W]], 3),
]
_NEAR_RUIN = _poi("ruins", "along", (50.0002, 15.0195))
_FAR_RUIN = _poi("ruins", "across", (50.0098, 15.0025))


def test_routes_to_poi_reaches_both_ruins_unfiltered(offline):
    offline(_LOOPLESS, pois=[_NEAR_RUIN, _FAR_RUIN])
    hikes = S.routes_to_poi(
        POI_START, ["ruins"], Criteria(max_distance_km=6.0), n=2, search_radius_m=5000.0
    )
    assert [h.destination.name for h in hikes] == ["along", "across"]   # nearest first


def test_routes_to_poi_applies_the_minimum_length(offline):
    """It drops the near destination — it does not go looking for a farther one, and it
    does not widen the fetched area the way `max_distance_km` does."""
    offline(_LOOPLESS, pois=[_NEAR_RUIN, _FAR_RUIN])
    hikes = S.routes_to_poi(
        POI_START,
        ["ruins"],
        Criteria(min_distance_km=2.0, max_distance_km=6.0),
        n=2,
        search_radius_m=5000.0,
    )
    assert [h.destination.name for h in hikes] == ["across"]
