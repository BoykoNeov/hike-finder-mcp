"""Pin public-transport access — the third access signal.

``access.py`` already answers "can I drive here?" and "can I ride a lift up?".
This adds "can I get here without a car?", which around KČT country is often the
question that actually decides a walk: a Czech trailhead is very frequently a
railway *halt*.

Three things are worth pinning, and they are the three that can silently rot:

  - the **registry round-trip** (query selectors vs classifier), the same anti-drift
    pin ``test_poi.py`` applies to ``POI_KINDS``. A stop that is fetchable but
    unclassifiable fails as a silently-empty result, not an error;
  - the **split radius** — rail generous, road tight — which is the only thing
    keeping the filter from matching essentially everything, since
    ``highway=bus_stop`` is mapped along nearly every rural road;
  - the **not-recorded tri-state**. This is the one with teeth: transit postdates
    existing snapshots, so ``None`` ("never measured") has to survive from the
    snapshot loader through ``Criteria`` without ever collapsing into ``False``
    ("nothing mapped nearby"). Collapsing them would answer
    ``--transit-access false`` with a full list of routes labelled by a measurement
    that never happened.
"""
import json

from hike_finder.access import (
    TRANSIT_KINDS,
    classify_transit,
    nearest_transit_m,
    transit_access,
    transit_label,
    transit_selectors_by_key,
)
from hike_finder.filters import Criteria, Hike
from hike_finder.overpass import AreaData, build_query, parse_area
from hike_finder.snapshot import AreaSnapshot, snapshot_to_json, snapshot_from_json

# One endpoint, and stops placed north of it at known distances. 1 deg latitude is
# ~111.2 km, so 0.001 deg is ~111 m — enough to place a stop precisely either side
# of a radius without depending on the exact earth model.
START = (50.0, 15.0)


def _north(metres: float):
    return (50.0 + metres / 111_195.0, 15.0)


def _stop(kind: str, metres: float, name: str | None = None):
    return {"coord": _north(metres), "kind": kind, "name": name}


# --------------------------------------------------------------- the registry


def test_every_registered_transit_kind_round_trips():
    """Everything the query fetches classifies back, and vice versa — the anti-drift
    pin, mirroring test_poi's. Written independently the query and the classifier
    drift, and the failure is an empty result rather than an exception."""
    selectors = transit_selectors_by_key()
    for kind, spec in TRANSIT_KINDS.items():
        assert spec.values, f"{kind} registers no tag values"
        for value in spec.values:
            assert classify_transit({spec.key: value}) == kind
            assert value in selectors[spec.key], f"{kind}/{value} is not fetched"
        assert transit_label(kind) == spec.label


def test_build_query_asks_for_the_transit_stops():
    q = build_query(50.0, 15.0, 50.1, 15.1)
    assert '"railway"' in q and "station" in q and "halt" in q
    assert '"highway"' in q and "bus_stop" in q


def test_stop_position_is_not_served():
    """Deliberate exclusion, pinned so it isn't 'fixed' later: these nodes sit on the
    rails rather than where you get off, and duplicate the station node."""
    assert classify_transit({"public_transport": "stop_position"}) is None


def test_unrelated_tags_classify_as_nothing():
    assert classify_transit({"amenity": "parking"}) is None
    assert classify_transit({}) is None


# ------------------------------------------------------------- the split radius


def test_rail_reaches_further_than_a_bus_stop():
    """The asymmetry IS the feature. A station at 800 m is a real way to arrive; a bus
    stop at 800 m is not much of a fact, because bus stops are mapped along nearly
    every rural road — one generous radius for both would make the filter match
    everything and stop discriminating."""
    ok, kind = transit_access([START], [_stop("station", 800)])
    assert ok and kind == "station"

    ok, kind = transit_access([START], [_stop("bus_stop", 800)])
    assert not ok and kind is None

    ok, kind = transit_access([START], [_stop("bus_stop", 300)])
    assert ok and kind == "bus_stop"


def test_a_station_just_past_its_radius_does_not_qualify():
    assert transit_access([START], [_stop("station", 1200)]) == (False, None)


def test_rail_wins_over_road_even_when_the_bus_stop_is_nearer():
    """Nearest-wins is right for chairlifts (one family of thing); here it is not.
    'Can I get here without a car' is answered by the train, so a station at 500 m
    beats a bus stop at 50 m — the more useful fact, not the closer one."""
    stops = [_stop("bus_stop", 50), _stop("station", 500)]
    ok, kind = transit_access([START], stops)
    assert ok and kind == "station"


def test_nearest_within_a_class_still_wins():
    stops = [_stop("station", 900), _stop("halt", 200)]
    ok, kind = transit_access([START], stops)
    assert ok and kind == "halt"


def test_radii_are_overridable():
    stops = [_stop("station", 800)]
    assert transit_access([START], stops, rail_radius_m=500.0)[0] is False


def test_no_endpoints_or_no_stops_is_false_not_an_error():
    assert transit_access([], [_stop("station", 10)]) == (False, None)
    assert transit_access([START], []) == (False, None)


def test_nearest_transit_measures_past_the_strict_radius():
    """The near-miss sibling applies ONE relaxed cap to every kind and reports the
    plain nearest — it is answering "how close did it come?", where the per-kind
    radius has already been missed."""
    d, kind = nearest_transit_m([START], [_stop("bus_stop", 600)], 1500.0)
    assert kind == "bus_stop" and 590 < d < 610
    assert nearest_transit_m([START], [_stop("bus_stop", 600)], 100.0) == (None, None)


# ------------------------------------------------------- parsing and the tri-state


def test_parse_area_files_stops_under_transit_not_pois():
    elements = [
        {"type": "node", "id": 1, "lat": 50.0, "lon": 15.0,
         "tags": {"railway": "halt", "name": "Zastávka"}},
        {"type": "node", "id": 2, "lat": 50.1, "lon": 15.1,
         "tags": {"highway": "bus_stop"}},
        {"type": "node", "id": 3, "lat": 50.2, "lon": 15.2,
         "tags": {"historic": "ruins"}},
    ]
    area = parse_area(elements)
    assert [t["kind"] for t in area.transit] == ["halt", "bus_stop"]
    assert area.transit[0]["name"] == "Zastávka"
    # the ruin still lands in pois, untouched by the new branch
    assert [p["kind"] for p in area.pois] == ["ruins"]


def test_a_live_parse_with_no_stops_records_an_empty_list_not_unknown():
    """'None mapped here' is a real answer and must be distinguishable from 'never
    looked' — which is the whole basis of the tri-state below."""
    area = parse_area([{"type": "node", "id": 1, "lat": 50.0, "lon": 15.0,
                        "tags": {"historic": "ruins"}}])
    assert area.transit == []


def test_a_stop_emitted_twice_is_recorded_once():
    """Overpass emits an element once per matching statement."""
    el = {"type": "node", "id": 1, "lat": 50.0, "lon": 15.0, "tags": {"railway": "halt"}}
    assert len(parse_area([el, dict(el)]).transit) == 1


def test_criteria_refuses_to_answer_when_transit_was_never_recorded():
    """The core honesty rule. A Hike from a pre-transit snapshot has transit_access
    None, and an ACTIVE filter must drop it either way — including `false`, where the
    tempting-but-wrong behaviour is to treat unknown as "no transit" and return the
    route. Same rule accepts_gain already applies to unmeasured gain."""
    unknown = Hike(osm_id=1, name="x", distance_km=5.0, circular=True,
                   car_access=False, chairlift_access=False, start=(50.0, 15.0))
    assert unknown.transit_access is None

    assert Criteria().accepts_geometry(unknown)  # no filter -> unaffected
    assert not Criteria(transit_access=True).accepts_geometry(unknown)
    assert not Criteria(transit_access=False).accepts_geometry(unknown)


def test_criteria_filters_normally_once_transit_is_known():
    known = Hike(osm_id=1, name="x", distance_km=5.0, circular=True,
                 car_access=False, chairlift_access=False, start=(50.0, 15.0),
                 transit_access=False)
    assert Criteria(transit_access=False).accepts_geometry(known)
    assert not Criteria(transit_access=True).accepts_geometry(known)


# ------------------------------------------------------------ snapshot round-trip


def _snapshot(area: AreaData) -> AreaSnapshot:
    return AreaSnapshot(bbox=(50.0, 15.0, 50.1, 15.1), area=area, elevations={},
                        sample_interval_m=25.0)


def test_snapshot_round_trips_stops_and_keeps_empty_distinct_from_absent():
    area = AreaData(transit=[{"coord": (50.0, 15.0), "kind": "halt", "name": "Z"}])
    back = snapshot_from_json(json.loads(json.dumps(snapshot_to_json(_snapshot(area)))))
    assert back.area.transit == [{"coord": (50.0, 15.0), "kind": "halt", "name": "Z"}]

    # An area with no stops saves an EMPTY LIST and loads back as one — a recorded
    # "none here", not a shrug.
    empty = snapshot_from_json(json.loads(json.dumps(snapshot_to_json(_snapshot(AreaData(transit=[]))))))
    assert empty.area.transit == []


def test_measure_geometry_carries_transit_onto_the_hike():
    """The wiring, not the predicate: everything above tests `transit_access` in
    isolation, which would keep passing even if `find_hikes` never handed the stops to
    it. A ~1 km way with a halt beside its start.
    """
    from hike_finder.filters import measure_geometry

    way = [(50.0, 15.0), (50.005, 15.0), (50.009, 15.0)]
    route = {"id": 1, "name": "Test", "ref": None, "tags": {}, "ways": [way]}

    hike, _ = measure_geometry(route, [], [], transit=[_stop("halt", 200)])
    assert hike.transit_access is True and hike.transit_type == "halt"

    # An area that recorded no stops answers False...
    hike, _ = measure_geometry(route, [], [], transit=[])
    assert hike.transit_access is False and hike.transit_type is None

    # ...and one that never recorded any answers "unknown", which is the default,
    # so every pre-existing caller that omits `transit` stays honest for free.
    hike, _ = measure_geometry(route, [], [])
    assert hike.transit_access is None


def test_a_snapshot_written_before_transit_existed_loads_as_unknown():
    """The load-bearing compatibility case: no `transit` key means the file cannot
    answer the question, and must NOT decay into "no transit here"."""
    d = snapshot_to_json(_snapshot(AreaData(transit=[])))
    del d["area"]["transit"]
    assert snapshot_from_json(d).area.transit is None


def test_a_station_just_past_the_radius_is_a_near_miss_not_a_silent_drop():
    """transit_distance_m exists so a station 1.1 km out reads like a parking lot
    380 m out — annotated, not dropped. A computed-but-unread field would be worse
    than no field: it looks complete."""
    hike = Hike(osm_id=1, name="x", distance_km=5.0, circular=True,
                car_access=False, chairlift_access=False, start=(50.0, 15.0),
                transit_access=False, transit_distance_m=1100.0)
    crit = Criteria(transit_access=True)

    assert not crit.accepts_geometry(hike)  # strict: still not a match
    assert crit.accepts_geometry_relaxed(
        hike, dist_km_margin=2.0, radius_frac=0.5,
        car_radius_m=300.0, lift_radius_m=400.0,
    )
    notes = crit.near_miss_notes(hike, gain_frac=0.2, car_radius_m=300.0,
                                 lift_radius_m=400.0)
    assert notes and any("public transport" in n and "1100 m" in n for n in notes)


def test_the_near_miss_pool_still_refuses_an_unmeasured_route():
    """Relaxing the RADIUS must not relax the tri-state: a route from a snapshot that
    never recorded transit has no distance either, so it stays out of the pool rather
    than sneaking in through the widened gate."""
    unknown = Hike(osm_id=1, name="x", distance_km=5.0, circular=True,
                   car_access=False, chairlift_access=False, start=(50.0, 15.0))
    assert unknown.transit_access is None and unknown.transit_distance_m is None
    assert not Criteria(transit_access=True).accepts_geometry_relaxed(
        unknown, dist_km_margin=2.0, radius_frac=0.5,
        car_radius_m=300.0, lift_radius_m=400.0,
    )


def test_an_unanswerable_offline_transit_search_says_so(caplog):
    """Returning nothing is correct but not sufficient — silence would read as
    "nowhere here is reachable by public transport", which for a Czech valley with a
    railway halt in it is a lie of omission. The filter must announce that the DATA is
    missing, not the transport."""
    import logging

    from hike_finder.search import search_snapshot

    way = [(50.0, 15.0), (50.005, 15.0), (50.009, 15.0)]
    area = AreaData(routes=[{"id": 1, "name": "Test", "ref": None, "unnamed": False,
                             "osmc_color": None, "tags": {}, "ways": [way]}])
    assert area.transit is None  # as a pre-transit snapshot loads

    with caplog.at_level(logging.WARNING):
        hikes = search_snapshot(_snapshot(area), Criteria(transit_access=True))

    assert hikes == []
    assert "transit" in caplog.text.lower()
    assert "predates" in caplog.text.lower() or "re-download" in caplog.text.lower()
