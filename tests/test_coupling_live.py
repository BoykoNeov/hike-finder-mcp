"""Start-coupling on REAL OSM data — the gap the synthetic tests left open.

`_route_start` aims a route's start marker at the terminus nearest a matched
parking/lift, so the marker lands on the trailhead you actually drive or ride to
(commits 6d4b998 / 8efb05c). The closure fixture (medved_relations.json) carries
NO parking/lift data, so the coupling branch was only ever exercised by synthetic
unit tests — never against a real relation with a real trailhead.

This fixture (tests/fixtures/spindl_area.json) is one live Overpass round-trip for
the Spindleruv Mlyn bbox (50.72,15.58,50.74,15.62), fetched 2026-06-24 — 15 routes,
31 parking lots, 5 ride-up lifts. It pins the coupling end-to-end on that data:

  - the iff guarantee (access verdict <=> matched access points) on every route;
  - the headline point-to-point case (Spindlmanova mise) coupling onto the
    Medvedin chairlift base, ~1.9 km from where the old fallback would have put it;
  - the branched case (Medvedi okruh, 4 termini, stitch drops 12/31 members)
    coupling onto a real lift station at a recovered terminus;
  - the documented limitation: a PURE LOOP has no terminus, so its start is never
    coupled — it stays at the stitched head even with parking matched on the ring.
"""
import json
from pathlib import Path

from hike_finder.access import matched_access_points, route_endpoints
from hike_finder.filters import _route_start, measure_geometry
from hike_finder.geometry import haversine_m, route_termini, stitch_ways
from hike_finder.overpass import parse_area

FIXTURE = Path(__file__).parent / "fixtures" / "spindl_area.json"
CAR_R, LIFT_R = 300.0, 400.0

# Real Krkonose lift base stations the coupling should land a start on (lat, lon),
# read off the live fixture geometry.
MEDVEDIN_LIFT = (50.73392, 15.60660)        # "Spindleruv Mlyn - Medvedin" chair_lift
HORNI_MISECKY_MEDVEDIN = (50.74110, 15.58191)  # "Horni Misecky - Medvedin" chair_lift


def _area():
    return parse_area(json.loads(FIXTURE.read_text(encoding="utf-8"))["elements"])


def _route(area, rid):
    return next(r for r in area.routes if r["id"] == rid)


def _measure(area, rid):
    """Return (hike, line, termini, matched_access_points) for one route."""
    r = _route(area, rid)
    hike, line = measure_geometry(r, area.parking, area.lifts)
    termini = route_termini(r["ways"])
    endpoints = list(dict.fromkeys(termini + route_endpoints(line)))
    aps = matched_access_points(
        endpoints, area.parking, area.lifts, car_radius_m=CAR_R, lift_radius_m=LIFT_R
    )
    return hike, line, termini, aps


def test_fixture_shape():
    area = _area()
    assert (len(area.routes), len(area.parking), len(area.lifts)) == (15, 31, 5)


def test_access_verdict_iff_matched_points_on_every_real_route():
    # The load-bearing invariant: the boolean access verdict and the points the
    # start couples to come from the SAME `<= radius` predicate, so they can never
    # silently disagree. Pinned here on all 15 live routes (synthetic before).
    area = _area()
    for r in area.routes:
        hike, line = measure_geometry(r, area.parking, area.lifts)
        termini = route_termini(r["ways"])
        endpoints = list(dict.fromkeys(termini + route_endpoints(line)))
        aps = matched_access_points(
            endpoints, area.parking, area.lifts, car_radius_m=CAR_R, lift_radius_m=LIFT_R
        )
        assert (hike.car_access or hike.chairlift_access) == bool(aps), hike.name


def test_point_to_point_couples_onto_the_lift_trailhead():
    # Spindlmanova mise: a point-to-point route with car+lift access at ONE end.
    # Coupling must move the start onto the Medvedin chairlift base (the trailhead
    # you ride up), far from the arbitrary stitched head the old rule would pick.
    area = _area()
    hike, line, termini, aps = _measure(area, 6285305)
    assert len(termini) == 2 and aps
    coupled = _route_start(line, termini, aps)
    fallback = _route_start(line, termini, ())  # the no-access rule's pick

    assert haversine_m(coupled, MEDVEDIN_LIFT) < 100      # lands on the lift base (~31 m)
    assert haversine_m(coupled, fallback) > 1500          # genuinely moved (~1.9 km)
    assert hike.start == coupled                          # this is what the pipeline ships


def test_branched_route_couples_onto_a_recovered_terminus_trailhead():
    # Medvedi okruh: branched, 4 genuine termini, stitch_ways weaves ~42% of the
    # length. Coupling must steer the start onto a real lift station at one of the
    # recovered termini — an end the greedy stitch could not even reach.
    area = _area()
    hike, line, termini, aps = _measure(area, 6285306)
    assert len(termini) == 4 and aps
    coupled = _route_start(line, termini, aps)
    fallback = _route_start(line, termini, ())

    assert haversine_m(coupled, HORNI_MISECKY_MEDVEDIN) < 60   # on the lift station (~29 m)
    assert haversine_m(coupled, fallback) > 1000              # moved off the stitch pick
    assert hike.start == coupled


def test_pure_loop_start_is_never_coupled():
    # Documented limitation: a pure loop has no degree-1 vertex, so coupling can't
    # fire even though parking IS matched on its ring. Its start stays at the
    # stitched head (loop start is geometrically arbitrary anyway).
    area = _area()
    hike, line, termini, aps = _measure(area, 6282999)
    assert termini == [] and aps                  # no terminus, yet access matched
    assert hike.circular is True
    assert haversine_m(hike.start, line[0]) <= 1.0  # unchanged from the stitched head


def test_coupling_actually_moves_starts_across_the_area():
    # Guard against a future regression that quietly turns coupling into a no-op:
    # on this real bbox the coupled start differs from the fallback on many routes.
    area = _area()
    moved = 0
    for r in area.routes:
        _, line = measure_geometry(r, area.parking, area.lifts)
        termini = route_termini(r["ways"])
        endpoints = list(dict.fromkeys(termini + route_endpoints(line)))
        aps = matched_access_points(
            endpoints, area.parking, area.lifts, car_radius_m=CAR_R, lift_radius_m=LIFT_R
        )
        if termini and aps:
            if haversine_m(_route_start(line, termini, aps), _route_start(line, termini, ())) > 1.0:
                moved += 1
    assert moved >= 5  # observed 10 of 14 coupled routes on 2026-06-24
