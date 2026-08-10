"""Pin cabled-climbing detection — the asymmetric one.

Everything here guards a miss in ONE direction. A ferrata reported where there is none
costs a user a route they could have walked; a ferrata missed costs them a harness they
did not bring. So the cases pinned are the ones where a reasonable implementation
quietly says "not cabled":

  - **either key, not one with the other as a bonus.** 25 of Cortina's 70 graded ways
    are `highway=path` + `via_ferrata_scale`. Keying on the highway value alone misses
    a third of them.
  - **`via_ferrata_scale=0` is a grade, not a falsy value.** Membership, not truthiness.
  - **`route=via_ferrata` with no grade at all** — `rel/19063986` in the Cortina box.
    A grade-only test loses the one object that exists solely as a ferrata relation.
  - **presence, never dominance.** 300 m of cable on a 12 km walk must fire. This is
    where copying `surface`'s 40 %/50 % gates would pass every test and ship a lie.
  - **absent vs clean.** `None` (no member tags to read) is not `present=False`.

The tag shapes below are the ones dumped live from the Cortina and Ehrwald boxes, not
invented: `1+`, `2+`, `3.5`, `highway=path` carrying a grade, and a scale-less
`route=via_ferrata` relation.
"""
import pytest

from hike_finder.ferrata import (
    FerrataSummary,
    relation_is_ferrata,
    scale_of,
    summarise_ferrata,
    summary_label,
    way_is_ferrata,
)


def _leg(metres: float):
    """A straight north-running way of roughly ``metres`` length."""
    return [(46.5, 12.1), (46.5 + metres / 111_195.0, 12.1)]


# ------------------------------------------------------------------ the predicate


def test_highway_via_ferrata_is_cabled():
    assert way_is_ferrata({"highway": "via_ferrata"})


def test_a_path_carrying_a_grade_is_cabled_too():
    """The measured trap: 25 of Cortina's 70 graded ways are `highway=path`. A test
    that keys on the highway value alone calls these ordinary paths."""
    assert way_is_ferrata({"highway": "path", "via_ferrata_scale": "2+"})


def test_grade_zero_is_a_grade_not_a_falsy_value():
    """`via_ferrata_scale=0` appears twice in the Cortina box. A truthiness test reads
    it as untagged and lets the gentlest cabled route through an avoidance filter."""
    assert way_is_ferrata({"highway": "path", "via_ferrata_scale": "0"})
    assert scale_of({"via_ferrata_scale": "0"}) == "0"


def test_an_ordinary_path_is_not_cabled():
    assert not way_is_ferrata({"highway": "path", "surface": "rock", "sac_scale": "T4"})
    assert not way_is_ferrata({})
    assert not way_is_ferrata(None)


def test_relation_predicate_reads_both_signals():
    assert relation_is_ferrata({"route": "via_ferrata"})
    assert relation_is_ferrata({"route": "hiking", "via_ferrata_scale": "3"})
    assert not relation_is_ferrata({"route": "hiking", "name": "Ferrata Lamon"})


def test_name_alone_never_makes_a_route_ferrata():
    """A name is free text, not a tag read. ~22 relations in the Cortina box have
    "ferrata" in the name; folding that into a tag-derived flag would let the label
    promise what the selector never checked."""
    assert not relation_is_ferrata({"route": "hiking", "name": "Via Ferrata Lipella"})
    assert not way_is_ferrata({"highway": "path", "name": "Klettersteig"})


def test_scale_is_passed_through_raw():
    """Mixed schemes — `1+`, `3.5`, and A–F elsewhere in OSM — have no safe common
    ordering, so values are never normalised or bucketed."""
    assert scale_of({"via_ferrata_scale": "3.5"}) == "3.5"
    assert scale_of({"via_ferrata_scale": "1+"}) == "1+"
    assert scale_of({"via_ferrata_scale": "C"}) == "C"
    assert scale_of({"via_ferrata_scale": "  2+  "}) == "2+"
    assert scale_of({"via_ferrata_scale": ""}) is None
    assert scale_of({}) is None


# ------------------------------------------------------- presence, not dominance


def test_a_short_cabled_section_on_a_long_walk_fires():
    """THE case. 300 m of cable on 12 km is 2.5 % — under every gate `surface` applies,
    and exactly what must not be averaged away. If this ever fails because the summary
    was routed through a share-based helper, the tests around it will still pass."""
    members = [(_leg(11_700), {"highway": "path"}), (_leg(300), {"highway": "via_ferrata"})]
    s = summarise_ferrata(members, {"route": "hiking"})
    assert s.present
    assert s.length_m == pytest.approx(300, rel=0.02)
    assert s.fraction == pytest.approx(300 / 12_000, rel=0.05)


def test_extent_is_reported_alongside_the_flag_never_instead_of_it():
    members = [(_leg(1000), {"highway": "path"}), (_leg(1000), {"via_ferrata_scale": "2"})]
    s = summarise_ferrata(members, {"route": "hiking"})
    assert s.present and s.fraction == pytest.approx(0.5, rel=0.02)


def test_a_fully_walkable_route_is_clean_not_unknown():
    members = [(_leg(1000), {"highway": "path"}), (_leg(2000), {"highway": "track"})]
    s = summarise_ferrata(members, {"route": "hiking"})
    assert s is not None
    assert s.present is False
    assert s.grades == ()


# ------------------------------------------------------------ relation-level claims


def test_route_via_ferrata_without_any_grade_is_still_ferrata():
    """`rel/19063986` (*Via Ferrata Lucio Dalaiti*) carries `route=via_ferrata` and no
    scale. It is the single object that exists ONLY as a ferrata relation — the whole
    marginal value of fetching them — so a grade-only test would drop it."""
    s = summarise_ferrata([(_leg(800), {"highway": "path"})], {"route": "via_ferrata"})
    assert s.present
    assert s.relation_tagged
    assert s.grades == ()


def test_a_hiking_relation_graded_on_the_relation_is_ferrata():
    """13 of Cortina's 212 hiking relations are mapped this way — the grade sits on the
    relation and none of the member ways carries a thing."""
    members = [(_leg(1500), {"highway": "path"})]
    s = summarise_ferrata(members, {"route": "hiking", "via_ferrata_scale": "3+"})
    assert s.present
    assert s.relation_tagged
    assert s.grades == ("3+",)
    # No member way is tagged, so the EXTENT is genuinely unmeasured — and must read as
    # unmeasured rather than as "almost none".
    assert s.length_m == 0.0


def test_grades_merge_the_relation_and_its_ways_without_ordering_meaning():
    members = [
        (_leg(400), {"highway": "via_ferrata", "via_ferrata_scale": "2"}),
        (_leg(300), {"highway": "via_ferrata", "via_ferrata_scale": "1+"}),
        (_leg(2000), {"highway": "path"}),
    ]
    s = summarise_ferrata(members, {"route": "hiking", "via_ferrata_scale": "3"})
    assert set(s.grades) == {"1+", "2", "3"}
    assert s.length_m == pytest.approx(700, rel=0.02)


# ------------------------------------------------------------- absent vs clean


def test_no_member_tags_and_a_silent_relation_is_unknown_not_clean():
    """A snapshot predating the member-tag fetch cannot say. Collapsing that into
    `present=False` reports an unexamined route as safe — the one direction this must
    never fail in."""
    assert summarise_ferrata(None, {"route": "hiking"}) is None
    assert summarise_ferrata([], {"route": "hiking"}) is None


def test_no_member_tags_but_a_ferrata_relation_still_answers():
    """The relation's own tags survive on every snapshot, so a relation-level claim is
    answerable even when the member tags are not."""
    s = summarise_ferrata(None, {"route": "via_ferrata"})
    assert s is not None and s.present and s.relation_tagged
    s = summarise_ferrata([], {"route": "hiking", "via_ferrata_scale": "1"})
    assert s is not None and s.present and s.grades == ("1",)


def test_zero_length_members_do_not_divide_by_zero():
    s = summarise_ferrata([([(46.5, 12.1)], {"highway": "via_ferrata"})], {"route": "hiking"})
    assert s is not None and s.present is False and s.fraction == 0.0


# --------------------------------------------------------------------- rendering


def test_label_is_silent_on_both_unknown_and_clean():
    """Absence of the flag is NOT a safety claim — it covers both "we could not look"
    and "we looked, it is clean", which is why the two stay separable in the data."""
    assert summary_label(None) is None
    assert summary_label(FerrataSummary(present=False)) is None


def test_label_names_the_grades_and_the_measured_extent():
    s = FerrataSummary(present=True, length_m=700.0, fraction=0.25, grades=("1+", "2"))
    assert summary_label(s) == "ferrata 1+/2 0.7 km"


def test_label_omits_an_unmeasured_extent_rather_than_printing_zero():
    """"0.0 km" would read as "barely any" — the opposite of "we never measured it"."""
    s = FerrataSummary(present=True, grades=("3+",), relation_tagged=True)
    assert summary_label(s) == "ferrata 3+"
    assert summary_label(FerrataSummary(present=True, relation_tagged=True)) == "ferrata"
