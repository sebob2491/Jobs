"""How the page script reads forms: labels, required marks, groups, blocks and ids."""

import pytest
from conftest import browser_available, fixture_url, run

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


def test_a_taleo_question_marked_required_in_its_words_is_required(srv, tmp_path):
    """Kforce's Taleo questionnaire (live, Oct 2026) says a question is required only in its label's
    words ("4. Is any member of your immediate family ...? . Required"): one left empty wasn't asked
    about, and Save and Continue was pressed three times over Taleo's "mandatory question" error. A
    question that merely ends in the word is no such mark."""
    got = fields_of(srv, tmp_path, '<label for="a">1. Are you over the age of 18? . Required</label>'
                                   '<select id="a"><option></option><option>Yes</option><option>No</option></select>'
                                   '<label for="b">Is a driver\'s license required</label>'
                                   '<select id="b"><option></option><option>Yes</option><option>No</option></select>')
    assert [f["required"] for f in got] == [True, False], got


def test_a_hidden_file_box_is_named_by_its_own_upload_button(srv, tmp_path):
    """EMD Electronics' Phenom application (Oct 2026): the resume's file box is hidden beside its
    "Upload Resume/CV" button, after an "or" between it and Apply With LinkedIn, under a header
    that's a plain paragraph. It was read as "or", so the resume never went in."""
    got = fields_of(srv, tmp_path, '<div class="resume-group"><span class="sr-only">Upload options</span>'
                                   '<p>Your resume/CV is required - upload your document here to get started!*</p>'
                                   '<ul><li><button type="button">Apply With LinkedIn</button></li></ul>'
                                   '<div class="apply-or-line"><span class="apply-or-box">or</span></div>'
                                   '<div class="resume-upload-wrapper"><input type="file" style="display: none;">'
                                   '<button type="button">Upload Resume/CV</button></div></div>'
                                   '<div><h4>Cover Letter</h4><div><input type="file" style="display: none;">'
                                   '<button type="button">Upload</button></div></div>')
    assert [(f["kind"], f["label"]) for f in got] == [("file", "Upload Resume/CV"), ("file", "Cover Letter")]
    # only the upload button right after its own hidden box, and only one that says what it takes
    got = fields_of(srv, tmp_path, '<div><h4>Documents</h4><input type="file" aria-label="Upload" hidden>'
                                   '<button type="button">Upload Resume</button><input type="file" aria-label="Upload" hidden>'
                                   '<button type="button">Upload Cover Letter</button></div>'
                                   '<h4>Transcript</h4><div><input type="file" aria-label="Attach" hidden>'
                                   '<button type="button">Clear selection</button><button type="button">Attach</button></div>'
                                   '<form><h3>Portfolio</h3><label for="r">Attach</label><input type="file" id="r">'
                                   '<button type="submit">Submit application</button></form>')
    assert [f["label"] for f in got] == ["Upload Resume", "Upload Cover Letter", "Transcript", "Portfolio"]


def test_a_loading_indicator_on_show_is_said(srv, tmp_path):
    """Workday's loading dots where a step's questions go (KLA's, live, Oct 2026), or a spinner marked
    busy, are said; a hidden one, or a step's progress bar with its value, isn't a page loading."""
    def busy(body: str) -> bool:
        page = tmp_path / "busy.html"
        page.write_text(f"<!doctype html><html><body><h2>Application Questions</h2>{body}</body></html>",
                        encoding="utf-8")
        run(srv.browser.goto(page.resolve().as_uri()))
        return bool(run(srv.browser.inspect(include_dropdown_options=False)).get("busy"))

    dots = '<div data-automation-id="loading" style="width: 80px; height: 20px"><span>.</span></div>'
    assert busy(dots)
    assert busy('<div aria-busy="true" style="height: 20px">Loading</div>')
    assert busy('<div role="progressbar" aria-label="Loading" style="height: 20px"></div>')
    assert not busy(dots.replace('style="', 'style="display: none; '))
    assert not busy('<div role="progressbar" aria-valuenow="3" aria-valuemax="6" style="height: 20px">step 3 of 6</div>')
    assert not busy('<label for="a">First Name</label><input id="a"><button>Next</button>')


def test_a_sign_ups_boxes_are_marked_aside_but_not_an_applications_own(srv, tmp_path):
    """A box to join a talent community or get job alerts beside the application (its email,
    its consent, a choice of interests) is marked aside, as its Submit is; a short form whose
    own button goes on with the application isn't, whatever its words say of job alerts."""
    got = fields_of(srv, tmp_path, '<form id="tc"><h3>Join our Talent Community</h3><label for="e">Email</label>'
                                   '<input id="e" type="email"><label><input type="checkbox" id="ok"> I agree to join the '
                                   'Talent Community</label><button type="submit">Submit</button></form>'
                                   '<footer><p>Get job alerts</p><label><input type="radio" name="i" value="a"> Engineering'
                                   '</label><label><input type="radio" name="i" value="b"> Operations</label></footer>'
                                   '<form><p>Enter your email to start your application. We will also send you job alerts.</p>'
                                   '<label for="s">Email to start application</label><input id="s" type="email">'
                                   '<button type="submit">Apply</button></form>')
    assert [(f["label"], bool(f.get("aside"))) for f in got] == [  # (ticks and choices after the boxes)
        ("Email", True), ("Email to start application", False), ("I agree to join the Talent Community", True),
        ("Get job alerts", True)]


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


def test_a_check_box_with_no_label_is_named_by_the_words_beside_it(srv, tmp_path):
    """UKG Pro's Create Account (Nikon Precision, live, Oct 2026): its consent box's words sit in a
    span beside it, not in a label, and the box was read with an empty label (and not required, its
    star unseen), so the desk never knew it for the site's terms. The words beside a box are its label,
    within its own wrapper: not words shared with another box."""
    got = fields_of(srv, tmp_path,
                    '<div class="consent"><input id="a" type="checkbox" value=""><span>By checking this box, I have '
                    'read and agree to the <a href="#p">Consent and Privacy Policy</a>*</span></div>'
                    '<div><input id="b" type="checkbox"> <span>Remember this device</span></div>'
                    '<div><div role="checkbox" aria-checked="false" tabindex="0" style="width:12px;height:12px">'
                    '</div><span>Keep me signed in</span></div>'
                    '<div><input id="c" type="checkbox" value=""><input id="d" type="checkbox" value="">'
                    '<span>Words for two boxes</span></div>')
    assert [(f["kind"], f["label"], f["required"]) for f in got] == [
        ("checkbox", "By checking this box, I have read and agree to the Consent and Privacy Policy*", True),
        ("checkbox", "Remember this device", False), ("checkbox", "Keep me signed in", False),
        ("checkbox", "", False), ("checkbox", "", False)]


def test_placeholder_choices_count_as_empty():
    for shown in ("-- Please Select --", "- Select -", "Choose an option", "Please choose", "--"):
        assert is_empty_value(shown), shown
    assert not is_empty_value("Select Engineering")


def page_of(srv, name: str, query: str = "") -> dict:
    run(srv.browser.goto(fixture_url(name) + query))
    return run(srv.browser.inspect(include_dropdown_options=False))


def test_a_resume_the_page_shows_as_uploaded_is_the_boxs_value(srv, tmp_path):
    """Eightfold's resume box is empty again after an upload, beside the file's own buttons
    ("Replace", "Delete file resume.pdf", "Preview file: resume.pdf") under "File upload completed
    successfully" (live, Oct 2026): read as empty, the resume went up again on every pass, and each
    upload brought its notice back."""
    def resume(data):
        return next(f for f in data["fields"] if f["kind"] == "file")
    assert resume(page_of(srv, "site/ai-notice-form.html"))["value"] == ""  # nothing up yet
    assert resume(page_of(srv, "site/ai-notice-form.html", "?uploaded=1"))["value"] == "resume.pdf"
    # without the file's name: its Replace, or the page saying the upload is done
    got = fields_of(srv, tmp_path, '<fieldset><legend>Resume</legend><div role="status">File upload completed successfully'
                                   '</div><div><input type="file"><button type="button">Replace</button></div></fieldset>'
                                   '<label for="fn">First name</label><input id="fn">')
    assert next(f for f in got if f["kind"] == "file")["value"] == "uploaded"


def test_a_dialog_open_over_the_page_is_read_with_its_buttons(srv):
    """Eightfold's notice about its AI screening opens over the whole form as the resume goes up;
    the desk sees it, its heading and all its buttons. A cookie banner is said to be one, and a
    dialog that is the form itself (a sign-in pop-up) or one that's closed isn't a dialog over it."""
    dialogs = page_of(srv, "site/ai-notice-form.html", "?notice=1")["dialogs"]
    assert [d["heading"] for d in dialogs] == ["Notice Related to Example Corp's Use of the Eightfold AI Recruiting Software"]
    assert [b["text"] for b in dialogs[0]["buttons"]] == ["Close", "Cancel", "I Agree"] and not dialogs[0].get("cookie")
    assert "artificial intelligence" in dialogs[0]["text"]
    assert page_of(srv, "site/ai-notice-form.html")["dialogs"] == []
    assert [d.get("cookie") for d in page_of(srv, "site/cookie-form.html")["dialogs"]] == [True]
    assert page_of(srv, "site/signin-header-popup.html", "?start=popup")["dialogs"] == []


def test_what_isnt_a_dialog_over_the_form(srv, tmp_path):
    """Nothing is filled behind a dialog over the form, so only one that waits on an answer counts:
    not a loading overlay (nothing to press), a box's own pop-up (a date picker's calendar, a
    country list), a side panel slid off the window, or a chat widget in a frame of its own."""
    over = 'role="dialog" aria-modal="true" style="position: fixed; inset: 0; background: #fff"'
    form = '<label for="fn">First name</label><input id="fn">'
    chat = ('<iframe style="width: 300px; height: 150px" srcdoc="<div role=&quot;dialog&quot; aria-modal=&quot;true&quot; '
            'style=&quot;position: fixed; inset: 0&quot;><p>Hi! Any questions?</p><button>Chat</button></div>"></iframe>')
    for body in (f"<div {over}><p>Loading your application</p></div>",
                 f'<div {over}><button>Previous month</button><table role="grid"><tr><td role="gridcell">1</td></tr></table></div>',
                 f'<div {over}><ul role="listbox"><li role="option">Canada</li></ul><button>Close</button></div>',
                 '<div role="dialog" aria-modal="true" style="position: fixed; top: 0; left: 100%; width: 400px; '
                 'height: 100%"><p>Job cart</p><button>Close</button></div>',
                 chat):
        page = tmp_path / "dialog.html"
        page.write_text(f"<!doctype html><html><body>{form}{body}</body></html>")
        run(srv.browser.goto(page.resolve().as_uri()))
        assert run(srv.browser.inspect(include_dropdown_options=False))["dialogs"] == [], body
    page.write_text(f'<!doctype html><html><body>{form}<div {over} aria-label="Before you apply"><p>Read this first.</p>'
                    '<button>OK</button></div></body></html>')
    run(srv.browser.goto(page.resolve().as_uri()))
    assert [d["heading"] for d in run(srv.browser.inspect(include_dropdown_options=False))["dialogs"]] == ["Before you apply"]
