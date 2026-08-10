"""Gain/loss is refused on a loop whose stitched line does not close.

``Hike.circular`` comes off the member ways' vertex graph (circuit rank); gain rides on
the STITCHED line, and ``stitch_ways`` greedily drops members it can't chain. When the
two disagree the elevation series belongs to some other path than the route, and the
numbers are not merely imprecise but impossible — live over Krkonoše, routes labelled
``[loop]`` reported ``gain=240 loss=0`` and ``gain=0 loss=117``.

The fixtures below are built from that run's measured partition: every loop whose line
closed scored a start-to-end gap under 2 % of its own length and read gain≈loss; every
one that didn't scored over 68 % and read an impossible asymmetry.
"""
import pytest

from hike_finder.access import is_circular
from hike_finder.elevation.base import ElevationProvider
from hike_finder.filters import Hike, add_elevation

M_PER_DEG_LAT = 111320.0


class _LatRamp(ElevationProvider):
    """Deterministic elevation: 20 km per degree of latitude (0 m at lat 50.0)."""

    SCALE = 20000.0

    def lookup(self, points):
        return [(lat - 50.0) * self.SCALE for lat, _ in points]


class _NeverCalled(ElevationProvider):
    def lookup(self, points):  # pragma: no cover - asserts it is never reached
        raise AssertionError("provider must not be called on the presampled path")


def _hike(line, *, circular, distance_km):
    return Hike(
        osm_id=1, name="T", distance_km=distance_km, circular=circular,
        car_access=False, chairlift_access=False, start=line[0],
        ways=(tuple(line),),
    )


def _gap_line(gap_m, *, rise_deg=0.02):
    """A line that climbs ``rise_deg`` of latitude and returns to ``gap_m`` of its start.

    A rectangle, so the climbing and descending legs are the same length and carry the
    same number of resampled points: a closing version then reads gain == loss exactly,
    and the only variable under test is the end gap. (An asymmetric shape smooths the
    two legs differently and the invariant blurs for reasons that have nothing to do
    with closure.)
    """
    return [
        (50.0, 14.0),                              # start
        (50.0 + rise_deg, 14.0),                   # climb
        (50.0 + rise_deg, 14.01),                  # across the top, flat
        (50.0, 14.01),                             # descend, mirroring the climb
        (50.0 + gap_m / M_PER_DEG_LAT, 14.0),      # back to within `gap_m` of the start
    ]


# --- the gate fires -----------------------------------------------------------


def test_loop_whose_line_does_not_close_reports_no_gain():
    # The `route/8464045` shape: labelled a loop, ends ~85 % of its length from its
    # start. Before the gate this reported a physically impossible gain/loss pair.
    line = _gap_line(1900.0)
    h = _hike(line, circular=True, distance_km=2.2)
    add_elevation(h, line, _LatRamp(), sample_interval_m=25.0)

    assert h.gain_m is None
    assert h.loss_m is None
    # The track would have been built from the same misordered line, so it goes too.
    assert h.track == ()


def test_the_cap_binds_on_a_long_loop_the_fraction_would_forgive():
    # 300 m of gap is only 1.5 % of a 20 km route, which the fraction alone would wave
    # through — but a loop that ends 300 m from where it started did not close.
    line = _gap_line(300.0)
    h = _hike(line, circular=True, distance_km=20.0)
    add_elevation(h, line, _LatRamp(), sample_interval_m=25.0)

    assert h.gain_m is None


# --- the tolerance really is two-sided ----------------------------------------


@pytest.mark.parametrize(
    "distance_km, expect_gain",
    [
        (0.1, False),   # `[M] Labský vodopád`: 69 m on a 0.1 km route is not a loop
        (10.0, True),   # the same 69 m on a 10 km route is a digitization seam
    ],
)
def test_the_same_gap_is_read_by_the_route_s_own_length(distance_km, expect_gain):
    """Absolute metres cannot separate these two, which is why the bound is a fraction.

    Both routes end 69 m from their start — under ``is_circular``'s 150 m tolerance, so
    both are labelled loops. Only one of them is one.
    """
    line = _gap_line(69.0)
    h = _hike(line, circular=True, distance_km=distance_km)
    add_elevation(h, line, _LatRamp(), sample_interval_m=25.0)

    assert (h.gain_m is not None) is expect_gain


def test_a_closing_loop_still_reads_gain_equal_to_loss():
    line = _gap_line(0.0)
    h = _hike(line, circular=True, distance_km=7.3)
    add_elevation(h, line, _LatRamp(), sample_interval_m=25.0)

    assert h.gain_m is not None
    # The invariant the broken routes violated: a closed walk returns to its altitude.
    assert h.gain_m == pytest.approx(h.loss_m, abs=1.0)


# --- the gate is deliberately narrow ------------------------------------------


def test_a_linear_route_is_untouched_by_the_gate():
    """A one-way route's line SHOULD end far from its start — that is not a defect.

    Closure is the only cheap contradiction the geometry offers, and a linear route
    offers none, so its gain is exactly as unverified after the gate as before it.
    """
    line = _gap_line(1900.0)
    h = _hike(line, circular=False, distance_km=2.2)
    add_elevation(h, line, _LatRamp(), sample_interval_m=25.0)

    assert h.gain_m is not None


def test_a_composed_loop_is_never_gated_on_its_stitched_line():
    """The presampled path carries a single synthesised ring, closed by construction.

    Its series is pre-assembled from per-segment samples (see search.compose_loops), so
    the `line` argument is not what the elevations were taken along. Gating on it would
    silently null the gain of every `--compose-loops` result.
    """
    line = _gap_line(1900.0)  # deliberately does NOT close
    h = _hike(line, circular=True, distance_km=2.2)
    # Symmetric up-and-back-down, so the smoothing window treats both legs alike and
    # gain == loss falls out of the ring being closed rather than out of the fixture.
    series = [100.0, 150.0, 220.0, 150.0, 100.0]
    points = [(50.0, 14.0), (50.01, 14.0), (50.02, 14.0), (50.01, 14.01), (50.0, 14.0)]
    add_elevation(
        h, line, _NeverCalled(),
        pre_elevations=series, pre_points=points, use_presampled=True,
    )

    assert h.gain_m is not None
    assert h.gain_m == pytest.approx(h.loss_m, abs=1.0)


# --- the label and this gate no longer disagree about one geometry ------------


def test_the_label_and_the_gain_gate_read_the_same_bound():
    """A route the LABEL calls a loop off its end gap must clear this gate too.

    They were written with the same 150 m constant and drifted: this gate learned to
    scale by route length, ``access.is_circular`` did not. The visible result was
    "loop, gain n/a" on a 0.1 km route — a hike labelled circular by one rule and
    refused its gain by the other, with the wrong one holding the label. Both read
    ``access.closure_limit_m`` now, so a fallback-labelled loop always has a gain.

    Exact agreement holds up to ``Hike.distance_km``'s 2-decimal rounding, which can
    move the limit by ≤ 0.25 m at the defaults — far below the margin either rule
    was built to resolve, so the cases are chosen clear of it.
    """
    # The first case is the whole point: 69 m is under the old flat 150 m, so the
    # label said loop, while the gate — already scaling — refused the gain. Any pair
    # where the two bounds differ has to live in that window; the rest are the
    # agreeing cases either side of it, so a bound broken in the OTHER direction
    # (both accepting everything) is caught too.
    for gap_m, distance_km in ((69.0, 0.2), (69.0, 10.0), (300.0, 20.0), (2.0, 0.2)):
        line = _gap_line(gap_m)
        ways = [[line[0], line[-1]]]
        labelled = is_circular(ways, line, {}, distance_km=distance_km)

        h = _hike(line, circular=True, distance_km=distance_km)
        add_elevation(h, line, _LatRamp(), sample_interval_m=25.0)
        has_gain = h.gain_m is not None

        assert labelled is has_gain, f"gap {gap_m} m on {distance_km} km"
