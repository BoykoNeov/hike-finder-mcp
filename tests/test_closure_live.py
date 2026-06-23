"""Closure regression on REAL OSM geometry — the gap the synthetic tests missed.

The 'Medvěd*' route relations were fetched live from Overpass (2026-06-23) and
trimmed into tests/fixtures/medved_relations.json. A previous closure version
clustered way ENDPOINTS within 30 m, which over-merged piled-up endpoints in
dense relations and INVENTED cycles — flipping six linear/branched routes,
'Medvědí okruh' among them, to circular=True. The vertex-graph circuit rank
(geometry.route_cycle_count) fixes that. These assertions pin the vertex-level
ground truth so the regression can't silently return.
"""
import json
from pathlib import Path

from hike_finder.access import endpoints_closed, is_circular
from hike_finder.geometry import stitch_ways
from hike_finder.overpass import parse_area

FIXTURE = Path(__file__).parent / "fixtures" / "medved_relations.json"

# Ground truth from the exact-coordinate vertex graph (which captures T-junctions
# via shared nodes). True = the member ways structurally enclose a loop.
EXPECTED_CLOSED = {
    3215491: True,    # [M] Medvědí stěna (okruh)        — genuine loop
    3992873: True,    # Medvědí stezky - červený okruh   — genuine loop
    6643167: True,    # Medvědí stezky - modrý okruh     — genuine loop
    6285306: False,   # Medvědí okruh — branched linear, NOT a loop (the headline)
    254733: False,    # [M] Medvědí bouda - Špindlerova bouda — point-to-point
    1631097: False,   # Medvědí stezka                   — linear
    3215492: False,   # [M] odbočka na Medvědí horu      — branch
    20442995: False,  # Medvědí naučná stezka            — linear
}


def _routes():
    elements = json.loads(FIXTURE.read_text(encoding="utf-8"))["elements"]
    return {r["id"]: r for r in parse_area(elements).routes}


def test_real_relations_closure_ground_truth():
    routes = _routes()
    assert set(routes) == set(EXPECTED_CLOSED)  # fixture and expectations in sync
    got = {rid: endpoints_closed(r["ways"]) for rid, r in routes.items()}
    assert got == EXPECTED_CLOSED


def test_real_medvedi_okruh_is_not_circular_end_to_end():
    # The reported symptom was 'Medvědí okruh' reading circular=false. Live data
    # shows it genuinely is NOT a loop (branched, ends ~2.4 km apart), so the
    # honest verdict is non-circular. The endpoint-cluster fix had flipped it to a
    # false positive; the vertex graph restores the correct answer end-to-end.
    r = _routes()[6285306]
    assert not r["tags"].get("roundtrip")  # geometry decides, no tag override
    line = stitch_ways(r["ways"])
    assert is_circular(r["ways"], line, r["tags"]) is False


def test_real_genuine_okruh_is_circular_end_to_end():
    # A real KČT loop (closes at exact shared vertices) stays circular.
    r = _routes()[3992873]
    line = stitch_ways(r["ways"])
    assert is_circular(r["ways"], line, r["tags"]) is True
