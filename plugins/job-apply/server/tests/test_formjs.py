"""How the page script reads forms: labels, required marks, groups, blocks and ids."""

import pytest
from conftest import browser_available, run

from job_apply.autofill import is_empty_value

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


def fields_of(srv, tmp_path, body: str) -> list[dict]:
    page = tmp_path / "form.html"
    page.write_text(f"<!doctype html><html><body>{body}</body></html>")
    run(srv.browser.goto(page.resolve().as_uri()))
    return run(srv.browser.inspect(include_dropdown_options=False))["fields"]


def test_a_form_posting_to_a_research_institute_isnt_a_search_box(srv, tmp_path):
    got = fields_of(srv, tmp_path, '<form action="/embed/job_app?for=toyotaresearchinstitute&token=1">'
                                   '<label for="fn">First Name</label><input id="fn"></form>'
                                   '<form class="jobSearchForm"><label for="q">Keywords</label><input id="q"></form>')
    assert [f["label"] for f in got] == ["First Name"]


def test_an_asterisk_anywhere_in_the_label_means_required(srv, tmp_path):
    got = fields_of(srv, tmp_path, '<p>* indicates a required field</p>'
                                   '<label for="a">* First Name</label><input id="a">'
                                   '<label for="b">Email * (work)</label><input id="b">'
                                   '<label for="c">Middle Name</label><input id="c">')
    assert {f["label"]: f["required"] for f in got} == {"* First Name": True, "Email * (work)": True,
                                                         "Middle Name": False}


def test_blocks_with_their_headings_beside_them(srv, tmp_path):
    got = fields_of(srv, tmp_path, '<div><h3>Work Experience 1</h3><div><label for="t1">Job Title</label><input id="t1"></div>'
                                   '<h3>Work Experience 2</h3><div><label for="t2">Job Title</label><input id="t2"></div></div>')
    assert [f.get("section") for f in got] == ["Work Experience 1", "Work Experience 2"]


# A job's Location and From date as a Workday site draws them (live, Oct 2026): the date is a
# fieldset of its own, its legend the date's label. `deeper` puts the date further down its block.
def workday_job(deeper: int = 0) -> str:
    return ('<div role="group" aria-labelledby="p1"><div><h3 id="p1">Work Experience 1</h3></div>'
            '<div data-automation-id="formField-location" data-fkit-id="workExperience-16--location">'
            '<label for="workExperience-16--location">Location</label>'
            '<div><div><input type="text" id="workExperience-16--location" name="location"></div></div></div>'
            '<div data-automation-id="formField-startDate" data-fkit-id="workExperience-16--startDate">'
            + '<div>' * deeper + '<fieldset><legend><label id="label121"><span>From<abbr aria-hidden="true">*</abbr>'
            '</span></label></legend><div id="workExperience-16--startDate" role="group" '
            'data-automation-id="dateInputWrapper" aria-labelledby="hiddenDateValueId-workExperience-16--startDate">'
            '<div id="workExperience-16--startDate-dateSectionMonth"><input role="spinbutton" aria-label="Month" '
            'id="workExperience-16--startDate-dateSectionMonth-input" data-automation-id="dateSectionMonth-input">'
            '</div><div id="workExperience-16--startDate-dateSectionYear"><input role="spinbutton" aria-label="Year" '
            'id="workExperience-16--startDate-dateSectionYear-input" data-automation-id="dateSectionYear-input"></div>'
            '</div></fieldset>' + '</div>' * deeper + '</div></div>')


def test_a_date_in_a_fieldset_of_its_own_is_in_its_jobs_block(srv, tmp_path):
    """The legend of a date's own fieldset ("From") was taken for its block, and dropped as the
    date's own label: a Workday site's 16 From and To boxes were asked bare, with no job named,
    and left empty (live, Oct 2026)."""
    got = fields_of(srv, tmp_path, workday_job())
    assert [(f["label"], f.get("sublabel"), f.get("section")) for f in got] == [
        ("Location", None, "Work Experience 1"), ("From*", "Month", "Work Experience 1"),
        ("From*", "Year", "Work Experience 1")]


def test_a_box_whose_block_isnt_found_takes_its_neighbours_by_their_ids(srv, tmp_path):
    """Where a box's block can't be found above it, Workday's ids still say it: "workExperience-16--"
    begins the ids of every box in that job's block."""
    got = fields_of(srv, tmp_path, workday_job(deeper=12))
    assert [f.get("section") for f in got] == ["Work Experience 1"] * 3, got


def test_radios_with_no_name_or_group_are_one_question(srv, tmp_path):
    got = fields_of(srv, tmp_path, '<p>Are you 18 or older?</p><div>'
                                   '<div role="radio" aria-checked="false" tabindex="0">Yes</div>'
                                   '<div role="radio" aria-checked="false" tabindex="0">No</div></div>')
    groups = [f for f in got if f["kind"] == "radio_group"]
    assert len(groups) == 1 and groups[0]["options"] == ["Yes", "No"]


def test_a_copied_block_gets_ids_of_its_own(srv, tmp_path):
    """A site that adds a block by copying one copies our ids with it."""
    block = '<div class="b"><label>School <input name="s"></label><input type="checkbox"> Current</div>'
    fields_of(srv, tmp_path, block)  # tags the block
    page = run(srv.browser.page())
    run(page.evaluate("() => { const b = document.querySelector('.b'); b.after(b.cloneNode(true)); }"))
    got = run(srv.browser.inspect(include_dropdown_options=False))["fields"]
    ids = [f["id"] for f in got]
    assert len(ids) == 4 and len(set(ids)) == 4


def test_an_unlabelled_box_doesnt_take_the_label_before_it(srv, tmp_path):
    got = fields_of(srv, tmp_path, '<div><span>Phone Number *</span><input name="phone"></div>'
                                   '<div><input name="ext"></div>')
    assert {f["label"]: f["required"] for f in got} == {"Phone Number *": True, "ext": False}


def test_placeholder_choices_count_as_empty():
    for shown in ("-- Please Select --", "- Select -", "Choose an option", "Please choose", "--"):
        assert is_empty_value(shown), shown
    assert not is_empty_value("Select Engineering")
