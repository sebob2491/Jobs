"""The desk's filter chips over the second review's 564 strings (the design of Oct 18, F5, F3, F6): a reading that
isn't surely the job's never hides it. Only a line labelled with the requirement and its strength hides a job for a
degree or a clearance ("Minimum Education: Bachelor's Degree", "Clearance Type: Secret"); what a posting's sentences
say, under whatever heading, is shown and never hides it (the coordinator's decision of Oct 18).

tests/fixtures/filters/review2.json holds each string with its intended result: where the reviewer said the job must
stay, it stays; where the reviewer would hide it, or allowed either, the result is the decided rule's (most are
"shown, not hidden": read from the text). Made-up postings only, as the reviewer wrote them."""

import json
from pathlib import Path

import pytest

from job_apply import employment, workplace
from job_apply.config import Profile
from job_apply.recommend import (Fit, _score_requirements, clearance_said, degree_said, posting_facts, requirements,
                                 title_rank)

DATA = json.loads((Path(__file__).parent / "fixtures" / "filters" / "review2.json").read_text(encoding="utf-8"))
_LEVEL = {"high_school": 1, "associate": 2, "bachelor": 3, "master": 4, "doctorate": 5}


def _hides(text: str, chip: str, mine: str | None) -> bool:
    """What a Requirements chip does to a posting of `text` (desk.html's CHIP_TESTS.requirements, in Python), for a
    person whose highest degree is `mine`."""
    f = posting_facts("Technician", requirements(text), text)
    if chip == "no_clearance":
        return bool(f["clearance_sure"] and (f["clearance"] or f["clearance_later"]))
    if chip == "degree":
        return bool(f["degree_sure"] and f["degree"] and f["degree"] != "high_school" and not f["degree_or_equivalent"]
                    and _LEVEL.get(f["degree"], 0) > _LEVEL.get(mine or "", 1))
    if chip == "day_shift":
        return bool(f["shift_sure"] and f["shift"] == "other")
    return bool(f["travel_sure"] and f["travel"] is not None and f["travel"] > 25)


def _params(key):
    return [pytest.param(text, mine, hides, id=f"{key}{i}") for i, (text, mine, hides) in enumerate(DATA[key])]


@pytest.mark.parametrize("chip, key", [("degree", "degree"), ("no_clearance", "clearance"), ("day_shift", "shift"),
                                       ("travel_25", "travel")])
def test_the_data_has_every_string(chip, key):
    assert len(DATA[key]) == {"degree": 141, "clearance": 80, "shift": 60, "travel": 41}[key]


@pytest.mark.parametrize("text, mine, hides", _params("degree"))
def test_the_degree_chip_hides_only_for_a_labelled_education_line(text, mine, hides):
    assert _hides(text, "degree", mine) is hides


@pytest.mark.parametrize("text, mine, hides", _params("clearance"))
def test_the_clearance_chip_hides_only_for_a_labelled_clearance_line(text, mine, hides):
    assert _hides(text, "no_clearance", mine) is hides


@pytest.mark.parametrize("text, mine, hides", _params("shift"))
def test_the_day_shift_chip_hides_only_for_another_shift_on_its_line(text, mine, hides):
    assert _hides(text, "day_shift", mine) is hides


@pytest.mark.parametrize("text, mine, hides", _params("travel"))
def test_the_travel_chip_hides_only_for_travel_said_as_the_jobs(text, mine, hides):
    assert _hides(text, "travel_25", mine) is hides


@pytest.mark.parametrize("heading", DATA["headings"])
def test_a_preferred_heading_never_makes_a_degree_or_a_clearance_sure(heading):
    text = DATA["heading_base"].format(h=heading)
    assert degree_said(text) == (None, False) and clearance_said(text) == (False, False, False)


@pytest.mark.parametrize("label", DATA["labels"])
def test_a_preference_label_never_makes_a_degree_or_a_clearance_sure(label):
    text = DATA["label_base"].format(l=label)
    assert degree_said(text) == (None, False) and clearance_said(text) == (False, False, False)


@pytest.mark.parametrize("text, line", [
    ("Minimum Education: Bachelor's Degree\nMinimum Experience: 2 years", "bachelor"),
    ("Education: Bachelor's degree (Required)", "bachelor"),
    ("Required Education: Master's Degree", "master"),
    ("Education: Bachelor's (Preferred)", None),
    ("Required Education: Bachelor's degree or equivalent experience", None),
    ("Required Education: Bachelor's degree\nOR 4 years of related experience", None),
    ("Required Education: Bachelor's degree within 2 years of hire", None),
    ("Education Level: Bachelor's Degree", None),
])
def test_a_labelled_education_line_is_sure_only_when_it_says_required_with_no_way_around(text, line):
    assert degree_said(text) == (line, False)


@pytest.mark.parametrize("text, said", [
    ("Clearance Type: Secret", (True, False, True)), ("Security Clearance Level: TS/SCI", (True, False, True)),
    ("Clearance Required: Secret", (True, False, True)),
    ("Clearance Level Must Currently Possess: Secret", (True, False, True)),
    ("Clearance Level Must Be Able to Obtain: Secret", (False, True, True)),
    ("Clearance Type: None", (False, False, False)), ("Clearance Level: Secret (preferred, not required)",
                                                       (False, False, False)),
    ("Clearance Type: Secret for some assignments", (False, False, False)),
    ("Clearance: Valid AZ DPS Level 1 Fingerprint Clearance Card", (False, False, False)),
    ("Security Clearance: Secret", (False, False, False)),  # (the label doesn't say it's required, nor its level)
])
def test_a_labelled_clearance_line_is_sure_only_with_a_level_and_no_hedge(text, said):
    assert clearance_said(text) == said


@pytest.mark.parametrize("title, ok", [pytest.param(t, set(ok), id=t) for t, ok in DATA["titles"]])
def test_a_titles_level_is_never_a_shift_grade_or_buildings_number(title, ok):
    assert title_rank(title) in ok


@pytest.mark.parametrize("text, kind, value, sure", DATA["places"])
def test_a_workplace_reading_is_never_sure_and_wrong(text, kind, value, sure):
    assert (workplace.field_said(text) if kind == "field" else workplace.location_said(text)) == (value, sure)


@pytest.mark.parametrize("value, types", DATA["types"])
def test_a_type_field_names_no_type_it_doesnt_say(value, types):
    assert employment.types_of(value) == [t for t in employment.TYPES if t in types]


@pytest.mark.parametrize("text, mine, blocked", DATA["score"])
def test_the_score_blocks_every_required_degree_main_blocks(text, mine, blocked):
    """The score keeps main's own reading of a degree (its preferred words, its ways around one): the widened words
    the chips use un-blocked real requirements ("Master's degree in Counseling and bilingual Spanish helpful")."""
    prof = Profile({"education": {"highest_degree": "Bachelor's" if mine == "bachelor" else "Master's"}})
    fit = Fit()
    _score_requirements(fit, requirements(DATA["score_head"] + text), prof, None)
    assert fit.blocked is blocked


def test_licensed_for_two_years_is_no_way_around_a_degree():
    """main's "or N years" matched inside "for 2 years": a word "or" only."""
    req = requirements("Qualifications\n- Master's degree in Counseling and licensed for 2 years")
    assert (req["degree"], req["degree_or_equivalent"]) == ("master", False)
    assert requirements("Qualifications\n- Master's degree or 2 years of experience")["degree_or_equivalent"]
