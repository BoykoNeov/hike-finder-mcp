"""Pin the surface / tracktype report — what you actually walk on.

The three things worth pinning are the three that would silently produce a
confident-but-wrong answer:

  - **length weighting.** OSM splits a way at every attribute change, so a route is
    routinely ten short asphalt slivers plus one long forest track. Counting ways
    would call that "mostly asphalt". Weighting by metres is the whole design.
  - **coverage.** The shares are fractions of the route's TOTAL length, not of its
    tagged part, and the summary reports how much of the route it actually knows —
    otherwise 200 m of tagged asphalt out of 10 km speaks for the entire walk.
  - **absent vs untagged.** A snapshot written before member-way tags were fetched
    must yield `None` (we never looked), not an empty summary (nobody has tagged it).
    Same distinction `transit_access` draws, and for the same reason.
"""
import json

import pytest

from hike_finder.filters import measure_geometry
from hike_finder.format import format_hike, hike_to_dict
from hike_finder.overpass import AreaData, build_query, parse_area
from hike_finder.snapshot import AreaSnapshot, snapshot_from_json, snapshot_to_json
from hike_finder.surface import (
    summarise_surface,
    summarise_tracktype,
    surface_label,
    tracktype_label,
)


def _leg(metres: float):
    """A straight north-running way of roughly ``metres`` length."""
    return [(50.0, 15.0), (50.0 + metres / 111_195.0, 15.0)]


# ------------------------------------------------------------ length weighting


def test_shares_are_weighted_by_length_not_by_way_count():
    """The core trap. Four short asphalt ways and ONE long forest way: by count that
    is 80 % asphalt, by distance it is overwhelmingly ground — and distance is what
    you walk."""
    members = [(_leg(50), {"surface": "asphalt"}) for _ in range(4)]
    members.append((_leg(4000), {"surface": "ground"}))

    s = summarise_surface(members)
    assert s.dominant.value == "ground"
    assert s.dominant.fraction == pytest.approx(4000 / 4200, abs=0.01)
    assert s.coverage == pytest.approx(1.0, abs=1e-6)


def test_shares_are_sorted_descending_and_cover_every_value():
    members = [
        (_leg(1000), {"surface": "gravel"}),
        (_leg(3000), {"surface": "ground"}),
        (_leg(500), {"surface": "asphalt"}),
    ]
    s = summarise_surface(members)
    assert [sh.value for sh in s.shares] == ["ground", "gravel", "asphalt"]
    assert sum(sh.fraction for sh in s.shares) == pytest.approx(1.0, abs=1e-6)


# --------------------------------------------------------------------- coverage


def test_fractions_are_of_the_whole_route_not_of_the_tagged_part():
    """200 m of tagged asphalt on a 10 km walk is 2 % of the walk — NOT 100 % of it,
    which is what dividing by the tagged length would report."""
    members = [(_leg(200), {"surface": "asphalt"}), (_leg(9800), {})]
    s = summarise_surface(members)
    assert s.dominant.value == "asphalt"
    assert s.dominant.fraction == pytest.approx(0.02, abs=0.005)
    assert s.coverage == pytest.approx(0.02, abs=0.005)


def test_a_route_with_no_tags_at_all_is_empty_but_measured():
    s = summarise_surface([(_leg(1000), {}), (_leg(2000), {})])
    assert s.shares == () and s.coverage == 0.0 and s.dominant is None


def test_zero_length_members_do_not_divide_by_zero():
    s = summarise_surface([([(50.0, 15.0)], {"surface": "asphalt"})])
    assert s.shares == () and s.coverage == 0.0
    assert summarise_surface([]).coverage == 0.0


# ----------------------------------------------------------------------- labels


def test_labels_explain_the_scales_and_pass_unknowns_through():
    assert surface_label("paving_stones") == "paving stones"
    assert tracktype_label("grade3") == "grade3 (even mix)"
    # An unlisted value is passed through rather than bucketed into "other" — a reader
    # who sees "sett" can look it up; a silent re-bucket destroys the information.
    assert surface_label("metal") == "metal"
    assert surface_label(None) is None and tracktype_label(None) is None


def test_tracktype_reads_its_own_key():
    members = [(_leg(1000), {"surface": "ground", "tracktype": "grade4"})]
    assert summarise_tracktype(members).dominant.value == "grade4"
    assert summarise_surface(members).dominant.value == "ground"


# ------------------------------------------------------ fetching and the join


def test_build_query_asks_for_the_member_way_tags():
    """A route relation carries no surface tag; `out body geom` returns member geometry
    WITHOUT member tags. This second statement is the only way to see them."""
    q = build_query(50.0, 15.0, 50.1, 15.1)
    assert "way(r);" in q and "out tags;" in q


def test_parse_area_joins_member_tags_by_way_id():
    elements = [
        {"type": "relation", "id": 1, "tags": {"route": "hiking", "name": "R"},
         "members": [
             {"type": "way", "ref": 100, "role": "",
              "geometry": [{"lat": 50.0, "lon": 15.0}, {"lat": 50.01, "lon": 15.0}]},
             {"type": "way", "ref": 200, "role": "",
              "geometry": [{"lat": 50.01, "lon": 15.0}, {"lat": 50.02, "lon": 15.0}]},
         ]},
        # the tag-only records from `way(r); out tags;` — no geometry of any kind
        {"type": "way", "id": 100, "tags": {"highway": "path", "surface": "gravel"}},
        {"type": "way", "id": 200, "tags": {"highway": "path", "surface": "ground"}},
    ]
    area = parse_area(elements)
    route = area.routes[0]
    assert route["way_tags"] == [
        {"highway": "path", "surface": "gravel"},
        {"highway": "path", "surface": "ground"},
    ]
    # parallel to "ways", index for index
    assert len(route["way_tags"]) == len(route["ways"])


def test_tag_only_ways_are_not_mistaken_for_features():
    """They have no coordinate, so filing one as a POI would put a pin nowhere. The
    parking/lift/transit/POI branches must not see them at all."""
    elements = [
        {"type": "way", "id": 100, "tags": {"tourism": "viewpoint", "surface": "rock"}},
        {"type": "way", "id": 101, "tags": {"amenity": "parking"}},
    ]
    area = parse_area(elements)
    assert area.pois == [] and area.parking == [] and area.transit == []


def test_a_member_way_appearing_twice_keeps_both_positions():
    """An out-and-back leg includes the same way twice; a dict keyed by way id would
    collapse them and under-weight that surface."""
    geom = [{"lat": 50.0, "lon": 15.0}, {"lat": 50.01, "lon": 15.0}]
    elements = [
        {"type": "relation", "id": 1, "tags": {"route": "hiking"},
         "members": [{"type": "way", "ref": 100, "role": "", "geometry": geom},
                     {"type": "way", "ref": 100, "role": "", "geometry": geom}]},
        {"type": "way", "id": 100, "tags": {"surface": "gravel"}},
    ]
    route = parse_area(elements).routes[0]
    assert route["way_tags"] == [{"surface": "gravel"}, {"surface": "gravel"}]


# ------------------------------------------------- absent vs untagged, end to end


def _route(way_tags):
    r = {"id": 1, "name": "R", "ref": None, "tags": {},
         "ways": [_leg(1000), _leg(3000)]}
    if way_tags is not None:
        r["way_tags"] = way_tags
    return r


def test_measure_geometry_distinguishes_never_looked_from_nobody_tagged():
    hike, _ = measure_geometry(_route(None), [], [])
    assert hike.surface is None and hike.tracktype is None  # never fetched

    hike, _ = measure_geometry(_route([{}, {}]), [], [])
    assert hike.surface is not None and hike.surface.coverage == 0.0  # fetched, untagged

    hike, _ = measure_geometry(
        _route([{"surface": "asphalt"}, {"surface": "ground"}]), [], []
    )
    assert hike.surface.dominant.value == "ground"


def test_the_one_liner_stays_quiet_below_half_coverage():
    """A dominant value read off 2 % of the route would print as a fact about the walk.
    The full breakdown is still in hike_to_dict for anyone who wants it."""
    thin, _ = measure_geometry(_route([{"surface": "asphalt"}, {}]), [], [])
    assert thin.surface.coverage < 0.5
    assert "surface:" not in format_hike(thin)
    assert hike_to_dict(thin)["surface"]["shares"][0]["value"] == "asphalt"

    thick, _ = measure_geometry(
        _route([{"surface": "gravel"}, {"surface": "gravel"}]), [], []
    )
    assert "surface:gravel 100%" in format_hike(thick)


def test_a_plurality_is_reported_as_mixed_not_as_the_answer():
    """Seen live: a well-tagged route whose commonest surface was only 21 % printed
    `surface:grass 21%`, which reads as "a grass walk" when four fifths of it is not.
    With no majority the flag names nothing and says how much is known."""
    r = {"id": 1, "name": "R", "ref": None, "tags": {},
         "ways": [_leg(1000), _leg(1000), _leg(1000), _leg(1000)],
         "way_tags": [{"surface": "grass"}, {"surface": "gravel"},
                      {"surface": "ground"}, {"surface": "asphalt"}]}
    hike, _ = measure_geometry(r, [], [])
    assert hike.surface.coverage == pytest.approx(1.0, abs=1e-6)
    assert hike.surface.dominant.fraction == pytest.approx(0.25, abs=0.01)

    line = format_hike(hike)
    assert "surface:mixed (100% known)" in line
    assert "grass" not in line  # the plurality is NOT promoted to the answer


def test_snapshot_round_trips_member_tags_and_absence_stays_absence():
    area = AreaData(routes=[{
        "id": 1, "name": "R", "ref": None, "unnamed": False, "osmc_color": None,
        "tags": {}, "ways": [_leg(1000)], "way_tags": [{"surface": "gravel"}],
    }])
    snap = AreaSnapshot(bbox=(50.0, 15.0, 50.1, 15.1), area=area, elevations={},
                        sample_interval_m=25.0)
    back = snapshot_from_json(json.loads(json.dumps(snapshot_to_json(snap))))
    assert back.area.routes[0]["way_tags"] == [{"surface": "gravel"}]

    # A file written before member tags existed has no key, and must stay "unknown"
    # rather than loading as a route nobody tagged.
    d = snapshot_to_json(snap)
    del d["area"]["routes"][0]["way_tags"]
    reloaded = snapshot_from_json(d)
    assert reloaded.area.routes[0]["way_tags"] == []
    hike, _ = measure_geometry(reloaded.area.routes[0], [], [])
    assert hike.surface is None


def test_clipping_keeps_way_tags_parallel_to_ways():
    """`way_tags` is defined as parallel to `ways`, and clipping changes how many ways
    there are — a member can split into several runs or vanish. A `{**r}` that carried
    the old list through would mis-attribute every surface after the first split."""
    from hike_finder.compose import clip_routes_to_bbox

    # way 0 straddles the west edge and survives as ONE run; way 1 is fully inside.
    r = {"id": 1, "name": "R", "ways": [
            [(50.0, 14.90), (50.0, 14.99), (50.0, 15.05), (50.0, 15.06)],
            [(50.02, 15.02), (50.02, 15.03)],
         ],
         "way_tags": [{"surface": "asphalt"}, {"surface": "ground"}]}
    out = clip_routes_to_bbox([r], (49.9, 15.0, 50.1, 15.1))[0]

    assert len(out["way_tags"]) == len(out["ways"])
    # every surviving run still carries ITS OWN way's surface
    for way, tags in zip(out["ways"], out["way_tags"], strict=True):
        expected = "asphalt" if way[0][0] == 50.0 else "ground"
        assert tags["surface"] == expected


def test_clipping_a_route_that_never_had_tags_stays_untagged():
    """Absence must survive clipping too — inventing [] parallel entries would turn
    "never fetched" into "fetched, nothing tagged"."""
    from hike_finder.compose import clip_routes_to_bbox

    r = {"id": 1, "name": "R", "ways": [[(50.02, 15.02), (50.02, 15.03)]]}
    out = clip_routes_to_bbox([r], (49.9, 15.0, 50.1, 15.1))[0]
    assert out["way_tags"] == []
    hike, _ = measure_geometry(out, [], [])
    assert hike.surface is None
