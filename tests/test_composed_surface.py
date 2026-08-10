"""Surface / tracktype on SYNTHESISED routes — the four modes that build their own geometry.

``--compose-loops``, ``--around``, ``--from``/``--to``, ``--via`` and ``--to-poi`` do not
report an OSM relation; they stitch a route out of contracted trail-graph segments. Their
synthetic route dict is one assembled polyline (``"ways": [route.coords]``), so there is no
member list for ``way_tags`` to be parallel to and ``measure_geometry`` can only leave the
surface summary at ``None``. The tags ride on the graph instead — per *step* of each
:class:`compose.Segment` — and ``compose.assemble_tag_runs`` turns a route's traversal back
into the ``(sub-polyline, tags)`` pairs ``surface.summarise_*`` already consumes.

What that leaves worth pinning is what could go confidently wrong:

  - **length weighting survives the round trip.** Same rule as a relation route: OSM splits
    a way at every attribute change, so way *count* is not the answer.
  - **step alignment, not point alignment.** ``assemble_loop_series`` drops the junction
    value shared between consecutive segments; a step list has none to drop, and reusing it
    would silently lose one step per segment.
  - **an exact split, not a prorated one.** ``snap_points`` cuts a segment mid-surface; the
    piece must keep the surfaces actually under it, not the parent's mix scaled by length.
  - **per occurrence.** A ``--via-loop`` that falls back to a retrace walks a segment twice
    and must be weighted twice — exactly as its length is counted twice.
  - **absent vs untagged.** Area data with no member-way tags at all yields ``None`` ("we
    never looked"), never an empty summary ("nobody tagged it").
"""
import pytest

from hike_finder import search as S
from hike_finder.compose import (
    _assemble,
    _dijkstra,
    assemble_tag_runs,
    build_trail_graph,
    snap_points,
)
from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Criteria
from hike_finder.geometry import polyline_length_m
from hike_finder.overpass import AreaData
from hike_finder.surface import summarise_surface, summarise_tracktype

# A square whose southern side is mapped as FOUR short asphalt slivers while each of the
# other three sides is one long ground way. By way count asphalt is 4 of 7 — a majority; by
# length it is about a tenth of the walk. That gap is the whole point of the measurement.
_S, _N = 50.00, 50.02
_W, _E = 15.000, 15.008
_SLIVERS = [
    [(_S, _W + 0.002 * i), (_S, _W + 0.002 * (i + 1))] for i in range(4)
]
_SQUARE_WAYS = _SLIVERS + [
    [(_S, _E), (_N, _E)],
    [(_N, _E), (_N, _W)],
    [(_N, _W), (_S, _W)],
]
_SQUARE_TAGS = [
    {"surface": "asphalt"}, {"surface": "asphalt"},
    {"surface": "asphalt"}, {"surface": "asphalt"},
    {"surface": "ground", "tracktype": "grade3"},
    {"surface": "ground"},
    {"surface": "ground", "tracktype": "grade3"},
]


def _route(ref, ways, tags=None, rid=1):
    r = {"id": rid, "name": ref, "ref": ref, "osmc_color": None, "tags": {}, "ways": ways}
    if tags is not None:
        r["way_tags"] = tags
    return r


def _square(with_tags=True):
    return [_route("sq", _SQUARE_WAYS, _SQUARE_TAGS if with_tags else None)]


def _only_loop(routes):
    """The single closed component of ``routes``, assembled as a composed route."""
    g = build_trail_graph(routes)
    assert len(g.segments) == 1 and g.segments[0].a == g.segments[0].b
    return g, _assemble(g, g.segments[0].a, [0])


def _run_length(runs):
    return sum(polyline_length_m(c) for c, _t in runs)


# --------------------------------------------------------------- the graph carries the tags


def test_step_tags_are_parallel_to_the_steps_not_the_points():
    # The invariant everything else rests on: one tag per STEP, so len(coords) - 1. An
    # off-by-one here is exactly what reusing assemble_loop_series would introduce.
    g = build_trail_graph(_square())
    assert g.has_way_tags
    for s in g.segments:
        assert len(s.step_tags) == len(s.coords) - 1


def test_area_data_without_member_tags_says_so_rather_than_reporting_bare_ground():
    # A snapshot predating the member-tag fetch. `has_way_tags` is presence detection, so
    # the graph knows it was never told — and the route reports nothing at all instead of
    # an empty summary, which would read as "nobody has tagged any of this".
    g, route = _only_loop(_square(with_tags=False))
    assert g.has_way_tags is False
    assert assemble_tag_runs(g, route) is None


def test_fetched_but_untagged_is_an_empty_summary_not_absence():
    # The complement, and the reason the flag is presence and not "is anything tagged":
    # tags were fetched, nobody filled them in. That is a real answer with 0 % coverage.
    g, route = _only_loop([_route("sq", _SQUARE_WAYS, [{} for _ in _SQUARE_WAYS])])
    assert g.has_way_tags is True
    runs = assemble_tag_runs(g, route)
    assert runs is not None
    assert summarise_surface(runs).coverage == 0.0


# ------------------------------------------------------------------------ length weighting


def test_a_composed_route_weights_surface_by_length_not_by_way_count():
    g, route = _only_loop(_square())
    summary = summarise_surface(assemble_tag_runs(g, route))
    shares = {s.value: s.fraction for s in summary.shares}
    # 4 of the 7 member ways are asphalt; about a tenth of the metres are.
    assert summary.dominant.value == "ground"
    assert shares["asphalt"] == pytest.approx(0.102, abs=0.01)
    assert shares["ground"] == pytest.approx(0.898, abs=0.01)
    assert summary.coverage == pytest.approx(1.0)


def test_partial_tracktype_coverage_is_reported_not_extrapolated():
    # Two of the three ground sides carry a tracktype; the shares are fractions of the
    # WHOLE route, so they must not add up to 1 and coverage must say why.
    g, route = _only_loop(_square())
    summary = summarise_tracktype(assemble_tag_runs(g, route))
    assert summary.dominant.value == "grade3"
    assert summary.coverage == pytest.approx(0.795, abs=0.01)
    assert summary.dominant.fraction == pytest.approx(summary.coverage)


def test_the_runs_measure_the_same_length_the_route_does():
    # The coverage denominator IS the route's length. If the assembled runs measured some
    # other whole, every fraction would be quietly wrong against the reported distance.
    g, route = _only_loop(_square())
    assert _run_length(assemble_tag_runs(g, route)) == pytest.approx(
        polyline_length_m(route.coords)
    )


# ------------------------------------------------------------------- a mid-segment split

# A meridian line mapped as two equal ways: south half asphalt, north half ground. The
# midpoint M is degree 2, so both weld into ONE segment carrying both surfaces — which is
# what makes a snap through it a real test of where the surfaces sit.
_A, _M, _B = (50.000, 15.0), (50.010, 15.0), (50.020, 15.0)
_SPLIT_ROUTE = [
    _route("line", [[_A, _M], [_M, _B]], [{"surface": "asphalt"}, {"surface": "ground"}])
]


def test_a_snapped_split_keeps_the_surfaces_actually_under_each_piece():
    # Snap a quarter of the way along the ASPHALT half, then route from there to the north
    # end: 0.75 of the asphalt plus all of the ground, so asphalt is 3/7 of what is left.
    # Prorating the parent segment's mix by piece length would say 1/2 — the number this
    # test exists to rule out.
    g = build_trail_graph(_SPLIT_ROUTE)
    assert len(g.segments) == 1
    g2, snapped = snap_points(g, [(50.0025, 15.0), _B])
    (src, src_d), (dst, _dd) = snapped
    assert src_d < 1.0  # the point sits on the line
    segs, _nodes, _len = _dijkstra(g2, src, dst)
    route = _assemble(g2, src, segs)
    shares = {s.value: s.fraction for s in summarise_surface(assemble_tag_runs(g2, route)).shares}
    assert shares["asphalt"] == pytest.approx(3 / 7, abs=0.005)
    assert shares["ground"] == pytest.approx(4 / 7, abs=0.005)


def test_the_split_graph_remembers_that_tags_were_fetched():
    # snap_points rebuilds the graph; losing the flag there would mute surface for every
    # mode that snaps a point — which is all four point-based ones.
    g2, _snapped = snap_points(build_trail_graph(_SPLIT_ROUTE), [(50.0025, 15.0)])
    assert g2.has_way_tags is True


# --------------------------------------------------------------------------- a retrace


def test_a_retraced_segment_is_weighted_twice_like_its_length_is():
    # The --via-loop fallback: no disjoint return exists, so the leg is walked back. The
    # traversal is per OCCURRENCE, so the out-and-back measures twice the line, and the
    # asphalt half stays half of it.
    g = build_trail_graph(_SPLIT_ROUTE)
    out_and_back = _assemble(g, g.segments[0].a, [0, 0])
    runs = assemble_tag_runs(g, out_and_back)
    line_m = polyline_length_m(g.segments[0].coords)
    assert _run_length(runs) == pytest.approx(2 * line_m)
    shares = {s.value: s.fraction for s in summarise_surface(runs).shares}
    assert shares["asphalt"] == pytest.approx(0.5, abs=0.01)


# -------------------------------------------------------------- end to end through search


class _RampProvider(ElevationProvider):
    """Deterministic offline elevation: height rises with latitude."""

    def lookup(self, points):
        return [(lat - 50.0) * 5000.0 for lat, _ in points]


def _stub(monkeypatch, routes):
    area = AreaData(routes=routes, parking=[], lifts=[], transit=[], pois=[])
    monkeypatch.setattr(S, "_fetch_area", lambda *a, **k: area)
    monkeypatch.setattr(S, "_provider", lambda *a, **k: _RampProvider())
    monkeypatch.setattr(S._cache, "from_config", lambda cfg: None)


_BBOX = (49.99, 14.99, 50.03, 15.02)


def test_a_composed_loop_reaches_the_renderer_with_its_surface(monkeypatch):
    _stub(monkeypatch, _square())
    hikes = S.compose_loops(_BBOX, Criteria(min_distance_km=1.0, max_distance_km=20.0))
    assert hikes and hikes[0].composed
    h = hikes[0]
    assert h.surface is not None and h.surface.dominant.value == "ground"
    assert h.tracktype is not None and h.tracktype.dominant.value == "grade3"
    # The coverage fraction is measured against the SAME whole the distance is: a
    # denominator that drifted from `distance_km` would make every share quietly wrong.
    assert h.surface.coverage == pytest.approx(1.0, abs=0.001)


def test_a_composed_loop_from_untagged_data_reports_no_surface(monkeypatch):
    _stub(monkeypatch, _square(with_tags=False))
    hikes = S.compose_loops(_BBOX, Criteria(min_distance_km=1.0, max_distance_km=20.0))
    assert hikes and hikes[0].composed
    assert hikes[0].surface is None
    assert hikes[0].tracktype is None
