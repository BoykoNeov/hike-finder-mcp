"""Offline tests for the CLI's pure parts and the shared formatter.

No network: we only exercise argument parsing, the args -> Criteria mapping
(especially the tri-state booleans, which are easy to get wrong), and the
one-line / dict rendering shared by every frontend.
"""
from hike_finder.cli import build_criteria, build_parser
from hike_finder.filters import Hike
from hike_finder.format import format_hike, hike_to_dict


def _parse(*argv):
    return build_parser().parse_args(list(argv))


def test_bbox_parsed_in_order():
    args = _parse("--bbox", "50.72", "15.58", "50.74", "15.62")
    assert args.bbox == [50.72, 15.58, 50.74, 15.62]


def test_boolean_filters_are_tristate():
    # omitted -> None (don't care)
    a = _parse("--bbox", "1", "2", "3", "4")
    assert a.circular is None and a.car_access is None and a.chairlift_access is None

    # present -> True (require)
    b = _parse("--bbox", "1", "2", "3", "4", "--circular", "--car-access", "--chairlift-access")
    assert b.circular is True and b.car_access is True and b.chairlift_access is True

    # negated -> False (exclude)
    c = _parse("--bbox", "1", "2", "3", "4", "--no-circular", "--no-car-access", "--no-chairlift-access")
    assert c.circular is False and c.car_access is False and c.chairlift_access is False


def test_build_criteria_maps_all_fields():
    args = _parse(
        "--bbox", "1", "2", "3", "4",
        "--min-gain", "100", "--max-gain", "800",
        "--min-distance", "5", "--max-distance", "20",
        "--circular", "--no-car-access",
    )
    crit = build_criteria(args)
    assert crit.min_gain_m == 100 and crit.max_gain_m == 800
    assert crit.min_distance_km == 5 and crit.max_distance_km == 20
    assert crit.circular is True
    assert crit.car_access is False
    assert crit.chairlift_access is None  # untouched -> don't care


def _sample_hike(**over):
    base = dict(
        osm_id=42, name="Test loop", distance_km=8.3, circular=True,
        car_access=True, chairlift_access=True, start=(50.7312, 15.6044),
        gain_m=540, loss_m=535, lift_type="chair_lift", ref="0001",
    )
    base.update(over)
    return Hike(**base)


def test_format_hike_full():
    line = format_hike(_sample_hike())
    assert line.startswith("Test loop — 8.3 km, +540 m / -535 m")
    assert "[loop, car, lift:chair_lift]" in line
    assert "start 50.7312,15.6044" in line
    assert "OSM relation 42" in line


def test_format_hike_oneway_no_access_no_gain():
    line = format_hike(_sample_hike(
        circular=False, car_access=False, chairlift_access=False,
        gain_m=None, loss_m=None, lift_type=None,
    ))
    assert "[one-way]" in line
    assert "gain n/a" in line
    assert "car" not in line and "lift:" not in line


def test_hike_to_dict_shape():
    d = hike_to_dict(_sample_hike())
    assert d["osm_id"] == 42 and d["name"] == "Test loop"
    assert d["start"] == {"lat": 50.7312, "lon": 15.6044}
    assert d["lift_type"] == "chair_lift"
    assert set(d) == {
        "osm_id", "name", "ref", "distance_km", "gain_m", "loss_m",
        "circular", "car_access", "chairlift_access", "lift_type", "start",
    }
