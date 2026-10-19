"""How the page script reads forms: labels, required marks, groups, blocks and ids."""

import pytest
from conftest import browser_available, fixture_url, run

from job_apply.autofill import is_empty_value

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


def fields_of(srv, tmp_path, body: str) -> list[dict]:
    page = tmp_path / "form.html"
    page.write_text(f"<!doctype html><html><body>{body}</body></html>", encoding="utf-8")
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


def test_a_gigya_choice_is_required_by_its_data_required_and_marked_while_its_mark_stands(srv, tmp_path):
    """Edwards Vacuum's Create Account (SAP's Gigya screen set, run 61): "Make my profile visible to:" is required
    only by its radios' data-required (its star a label of its own), and after a refused Submit its first radio is
    marked aria-invalid. The group is required, and marked while nothing's chosen; a mark left on one radio after
    another was picked isn't the group's (the review of 0.3.149)."""
    radios = ('<div role="group" aria-labelledby="v"><span id="v">Make my profile visible to:</span>'
              '<input type="radio" name="vis" id="a" data-required="true" aria-invalid="true"{a}>'
              '<label for="a">Anyone</label><input type="radio" name="vis" id="b" data-required="true"{b}>'
              '<label for="b">Only recruiters</label></div>')
    got = fields_of(srv, tmp_path, radios.format(a="", b=""))
    assert [(f["kind"], f["required"], f.get("invalid")) for f in got] == [("radio_group", True, True)], got
    got = fields_of(srv, tmp_path, radios.format(a="", b=" checked"))
    assert [(f["value"], f.get("invalid")) for f in got] == [("Only recruiters", None)], got
    got = fields_of(srv, tmp_path, radios.format(a=" checked", b=""))
    assert [(f["value"], f.get("invalid")) for f in got] == [("Anyone", True)], got


def test_workdays_whole_step_loading_page_is_busy_while_on_show(srv, tmp_path):
    """Baker Tilly's Workday (run 62): a step still loading is Workday's applyFlowLoadingPage, grey bars where the step
    goes: busy while it's on show, and not once it's gone."""
    page = tmp_path / "form.html"
    for style, busy in (("", True), (' style="display: none"', False)):
        page.write_text('<!doctype html><html><body><h2>Create Account/Sign In</h2><div data-automation-id='
                        f'"applyFlowLoadingPage"{style}><div style="height: 40px; width: 300px; background: #eee">'
                        '</div></div></body></html>', encoding="utf-8")
        run(srv.browser.goto(page.resolve().as_uri()))
        assert bool(run(srv.browser.inspect(include_dropdown_options=False)).get("busy")) is busy, style


def test_workdays_step_body_with_only_its_loading_dots_is_busy(srv, tmp_path):
    """Grand Canyon University's Workday (run 69): its Create Account/Sign In step's body under the progress bar was
    only Workday's three loading dots, boxes with no words and no automation id: busy. Not a body with words, a box to
    fill or a picture, nor one with fewer than three drawn boxes (empty wrappers aren't dots), nor one hidden, nor the
    same outside Workday's apply flow; and never a real step drawn (Onto's and Intel's, captured live)."""
    dot = '<div style="width: 8px; height: 8px; background: #ccd"></div>'
    dots = f'<div style="display: flex; gap: 6px">{dot * 3}</div>'
    flow = ('<div data-automation-id="applyFlowPage"><main><div><ol data-automation-id="progressBar">'
            '<li>current step 1 of 6 Create Account/Sign In</li></ol></div><div{style}>{body}</div></main></div>')
    page = tmp_path / "form.html"
    for html, busy in ((flow.format(style="", body=dots), True),
                       (flow.format(style="", body=dots + '<p>Sign in to apply</p>'), False),
                       (flow.format(style="", body=dots + '<input aria-label="">'), False),
                       (flow.format(style="", body=dots + '<img alt="" src="data:,">'), False),
                       (flow.format(style="", body=f'<div>{dot * 2}</div><div><div><div></div></div></div>'), False),
                       (flow.format(style=' style="display: none"', body=dots), False),
                       ('<main><div><ol><li>step 1</li></ol></div><div>' + dots + '</div></main>', False)):
        page.write_text(f"<!doctype html><html><body>{html}</body></html>", encoding="utf-8")
        run(srv.browser.goto(page.resolve().as_uri()))
        assert bool(run(srv.browser.inspect(include_dropdown_options=False)).get("busy")) is busy, html
    for live in ("live-onto-innovation.html", "live-intel.html"):  # (Workday steps drawn, captured live)
        run(srv.browser.goto(fixture_url(f"live/{live}")))
        assert not run(srv.browser.inspect(include_dropdown_options=False)).get("busy"), live


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
                                   '<button type="button">Upload Resume</button><input type="file" aria-label="Upload" '
                                   'hidden><button type="button">Upload Cover Letter</button></div>'
                                   '<h4>Transcript</h4><div><input type="file" aria-label="Attach" hidden>'
                                   '<button type="button">Clear selection</button><button type="button">Attach</button>'
                                   '</div><form><h3>Portfolio</h3><label for="r">Attach</label>'
                                   '<input type="file" id="r"><button type="submit">Submit application</button></form>')
    assert [f["label"] for f in got] == ["Upload Resume", "Upload Cover Letter", "Transcript", "Portfolio"]


WORKDAY_UPLOAD = ('<div data-automation-id="formField-" data-fkit-id="primaryQuestionnaire--a{n}">'
                  '<div id="rich-label{n}"><div data-automation-id="richText"><p><b>{question}</b>{star}</p></div>'
                  '</div><div><div style="width: 100%"><div aria-labelledby="rich-label{n}" '
                  'data-automation-id="a{n}-FileUpload"><div><div data-automation-id="file-upload-drop-zone">'
                  '<div>Drop files here</div><div><div>or</div>'
                  '<button type="button" data-automation-id="select-files"><span>Select files</span></button>'
                  '</div></div><input data-automation-id="file-upload-input-ref" multiple type="file"></div>'
                  '</div></div></div></div>')


def test_a_workday_questionnaire_file_box_is_named_by_its_question(srv, tmp_path):
    """CSN's, NSU's and UNLV's Workday (live run 144): a questionnaire's file boxes were read as "Drop files here or
    Select files", the words of the drop zone before the input, so the cover letter's box wasn't known for one and the
    pause named nothing the person could find. The box's "...-FileUpload" wrapper is labelled (aria-labelledby) by the
    question's own rich text."""
    star = '<abbr class="requiredAsterisk" title="required">*</abbr>'
    got = fields_of(srv, tmp_path, '<div aria-labelledby="sec" role="group"><h3 id="sec">Application Questions</h3>'
                    + WORKDAY_UPLOAD.format(n=155, question="Please upload your Letter of Application/Cover Letter.",
                                            star=star)
                    + WORKDAY_UPLOAD.format(n=156, question="Please upload your Reference Contact Information.",
                                            star="") + '</div>')
    assert [(f["kind"], f["label"], f["required"]) for f in got] == [
        ("file", "Please upload your Letter of Application/Cover Letter.*", True),
        ("file", "Please upload your Reference Contact Information.", False)]


def test_a_drop_zones_words_dont_name_its_file_box(srv, tmp_path):
    """A drop zone's own words, "Drop files here or Select files" (Workday's, live run 144), say nothing about what the
    box takes: they're as generic as "Attach", and the heading before the box names it."""
    got = fields_of(srv, tmp_path, '<div><h4>Cover Letter</h4><div><div><div>Drop files here</div><div>or</div>'
                                   '<button type="button">Select files</button></div><input type="file"></div></div>'
                                   '<div><h4>Transcript</h4><div><div>Drag and drop files here or browse files</div>'
                                   '<input type="file"></div></div>')
    assert [f["label"] for f in got] == ["Cover Letter", "Transcript"]


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
    assert not busy('<div role="progressbar" aria-valuenow="3" aria-valuemax="6" '
                    'style="height: 20px">step 3 of 6</div>')
    assert not busy('<label for="a">First Name</label><input id="a"><button>Next</button>')


def test_a_sign_ups_boxes_are_marked_aside_but_not_an_applications_own(srv, tmp_path):
    """A box to join a talent community or get job alerts beside the application (its email,
    its consent, a choice of interests) is marked aside, as its Submit is; a short form whose
    own button goes on with the application isn't, whatever its words say of job alerts."""
    got = fields_of(srv, tmp_path, '<form id="tc"><h3>Join our Talent Community</h3><label for="e">Email</label>'
                                   '<input id="e" type="email"><label><input type="checkbox" id="ok"> I agree to join '
                                   'the Talent Community</label><button type="submit">Submit</button></form>'
                                   '<footer><p>Get job alerts</p><label><input type="radio" name="i" value="a"> '
                                   'Engineering</label><label><input type="radio" name="i" value="b"> '
                                   'Operations</label></footer><form><p>Enter your email to start your application. We '
                                   'will also send you job alerts.</p><label for="s">Email to start application</label>'
                                   '<input id="s" type="email"><button type="submit">Apply</button></form>')
    assert [(f["label"], bool(f.get("aside"))) for f in got] == [  # (ticks and choices after the boxes)
        ("Email", True), ("Email to start application", False), ("I agree to join the Talent Community", True),
        ("Get job alerts", True)]


def test_blocks_with_their_headings_beside_them(srv, tmp_path):
    got = fields_of(srv, tmp_path, '<div><h3>Work Experience 1</h3><div><label for="t1">Job Title</label>'
                                   '<input id="t1"></div><h3>Work Experience 2</h3><div><label for="t2">Job '
                                   'Title</label><input id="t2"></div></div>')
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
    got = fields_of(srv, tmp_path, '<fieldset><legend>Resume</legend><div role="status">File upload completed '
                                   'successfully</div><div><input type="file"><button type="button">Replace</button>'
                                   '</div></fieldset><label for="fn">First name</label><input id="fn">')
    assert next(f for f in got if f["kind"] == "file")["value"] == "uploaded"


def test_a_dialog_open_over_the_page_is_read_with_its_buttons(srv):
    """Eightfold's notice about its AI screening opens over the whole form as the resume goes up;
    the desk sees it, its heading and all its buttons. A cookie banner is said to be one, and a
    dialog that is the form itself (a sign-in pop-up) or one that's closed isn't a dialog over it."""
    dialogs = page_of(srv, "site/ai-notice-form.html", "?notice=1")["dialogs"]
    assert ([d["heading"] for d in dialogs]
            == ["Notice Related to Example Corp's Use of the Eightfold AI Recruiting Software"])
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
    chat = ('<iframe style="width: 300px; height: 150px" srcdoc="<div role=&quot;dialog&quot; '
            'aria-modal=&quot;true&quot; style=&quot;position: fixed; inset: 0&quot;><p>Hi! Any questions?</p>'
            '<button>Chat</button></div>"></iframe>')
    for body in (f"<div {over}><p>Loading your application</p></div>",
                 f'<div {over}><button>Previous month</button><table role="grid"><tr>'
                 '<td role="gridcell">1</td></tr></table></div>',
                 f'<div {over}><ul role="listbox"><li role="option">Canada</li></ul><button>Close</button></div>',
                 '<div role="dialog" aria-modal="true" style="position: fixed; top: 0; left: 100%; width: 400px; '
                 'height: 100%"><p>Job cart</p><button>Close</button></div>',
                 chat):
        page = tmp_path / "dialog.html"
        page.write_text(f"<!doctype html><html><body>{form}{body}</body></html>", encoding="utf-8")
        run(srv.browser.goto(page.resolve().as_uri()))
        assert run(srv.browser.inspect(include_dropdown_options=False))["dialogs"] == [], body
    page.write_text(f'<!doctype html><html><body>{form}<div {over} aria-label="Before you apply">'
                    '<p>Read this first.</p><button>OK</button></div></body></html>', encoding="utf-8")
    run(srv.browser.goto(page.resolve().as_uri()))
    assert ([d["heading"] for d in run(srv.browser.inspect(include_dropdown_options=False))["dialogs"]]
            == ["Before you apply"])


def test_a_jquery_ui_selectmenu_is_read_as_its_hidden_select(srv, tmp_path):
    """BrassRing's security question (runs 67 to 77): a jQuery UI selectmenu, a span button ("<id>-button") standing in
    for a hidden select, whose choices and whether it's required are the select's. Its placeholder chosen is no
    choice, whatever its value ("", or "-1"). Another element acting as a list's button with no select behind it (a
    header's language picker) isn't the form's; an input with that role still is."""
    def menu(sid: str, label: str, placeholder: str, picked: str = "") -> str:
        return (f'<label for="{sid}-button">{label}</label><select id="{sid}" required style="display:none">'
                f'<option value="{placeholder}">Select question</option><option value="0"'
                f'{" selected" if picked else ""}>In what city were you born?</option></select>'
                f'<span id="{sid}-button" role="button" tabindex="0" aria-haspopup="listbox" aria-owns="{sid}-menu">'
                f'<span class="ui-selectmenu-text">{picked or "Select question"}</span></span>'
                f'<ul id="{sid}-menu" role="listbox"></ul>')

    fields = fields_of(srv, tmp_path, menu("q1", "Question 1", "") + menu("q2", "Question 2", "-1")
                       + menu("q3", "Question 3", "", "In what city were you born?")
                       + '<span role="button" aria-haspopup="listbox" id="lang">English</span>'
                       + '<input role="button" aria-haspopup="listbox" id="pick" aria-label="Pick a site">')
    got = {f["label"]: (f["kind"], f.get("value"), f.get("required")) for f in fields}
    assert got == {"Question 1": ("listbox", "", True), "Question 2": ("listbox", "", True),
                   "Question 3": ("listbox", "In what city were you born?", True),
                   "Pick a site": ("text", "", False)}, got


@pytest.mark.parametrize("query", ["", "?modelonly"])
def test_a_jquery_ui_multiselect_takes_its_choice_once_and_reads_it_back(srv, query):
    """BrassRing's EEO step (Edward Jones, run 87): "Please check each applicable box" is a multiselect over a hidden
    <select multiple>, its button always "Choose...", what's ticked listed under it. The desk ticked a choice, read the
    button as not showing it, and clicked the choice again, which unticked it: "Required". A multiselect's choice is
    confirmed by what it has ticked, never clicked twice, and read back as the box's value; the step then goes on.
    ?modelonly (the review's): a widget that keeps its ticks in its own list alone, the hidden select untouched: read
    from that list, so the same answer given again (a second pass, the person's card) never unticks it."""
    page = page_of(srv, "site/brassring-multiselect.html", query)
    eth = next(f for f in page["fields"] if f["label"].startswith("How would you like to identify your ethnicity"))
    assert (eth["kind"], eth.get("value"), eth.get("required")) == ("listbox", "", True), eth
    done = run(srv.browser.fill([{"id": eth["id"], "value": "Prefer Not to Disclose"}]))
    assert all(r["ok"] for r in done), done
    tab = run(srv.browser.page())
    ticked = run(tab.evaluate("() => Array.from(document.querySelectorAll('#eth-button_selection-list li'))"
                              ".map((li) => li.textContent.trim())"))
    assert ticked == ["Prefer Not to Disclose"], ticked
    after = next(f for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"] if f["id"] == eth["id"])
    assert after.get("value") == "Prefer Not to Disclose", after
    # given again, it stays ticked; another answer ticks that one too, the menu opened afresh
    again = run(srv.browser.fill([{"id": eth["id"], "value": "Prefer Not to Disclose"},
                                  {"id": eth["id"], "value": "Asian"}]))
    assert all(r["ok"] for r in again), again
    ticked = run(tab.evaluate("() => Array.from(document.querySelectorAll('#eth-button_selection-list li'))"
                              ".map((li) => li.textContent.trim())"))
    assert ticked == ["Asian", "Prefer Not to Disclose"], ticked
    run(tab.click("#save"))
    run(tab.wait_for_url("**/step1.html", timeout=5000))
    assert run(tab.evaluate("() => sessionStorage.getItem('multi.saved')")) == '["Asian","Prefer Not to Disclose"]'


def test_a_brassring_search_box_for_several_choices_is_read_ticked_once_and_read_back(srv):
    """BrassRing's second race question (Edward Jones, run 88): "Please check each applicable box" drawn as a search box
    (type=search, role=combobox) standing for a hidden <select multiple> that holds only "Choose...". The form reader
    passed it over as a site's search box, so it was never answered and its "Required" stopped the step. It's read as
    the question, with what's ticked (its pills) as its value; its list opens on the Down key, a choice past its first
    page of 15 is found by typing, and each is clicked once: given again, it stays ticked."""
    page = page_of(srv, "site/brassring-autocomplete-multi.html")
    race = next((f for f in page["fields"] if f["label"].startswith("How would you like to identify your race")), None)
    assert race is not None, page["fields"]
    assert (race["kind"], race.get("value"), race.get("required"), race.get("multiple")) == ("combobox", "", True, True)
    done = run(srv.browser.fill([{"id": race["id"], "value": "Decline to self-identify", "pick_only": True}]))
    assert all(r["ok"] for r in done), done
    tab = run(srv.browser.page())
    pills = "() => Array.from(document.querySelectorAll('.selectionList li')).map((li) => li.firstChild.textContent)"
    assert run(tab.evaluate(pills)) == ["Prefer Not to Disclose"]
    after = next(f for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"] if f["id"] == race["id"])
    assert after.get("value") == "Prefer Not to Disclose", after
    again = run(srv.browser.fill([{"id": race["id"], "value": "Prefer Not to Disclose", "pick_only": True},
                                  {"id": race["id"], "value": "White", "pick_only": True}]))
    assert all(r["ok"] for r in again), again
    assert run(tab.evaluate(pills)) == ["Prefer Not to Disclose", "White"]
    assert not run(tab.evaluate("() => !!document.getElementById('race-input_listbox')"))  # (its list closed after)
    run(tab.click("#save"))
    run(tab.wait_for_url("**/step1.html", timeout=5000))
    assert run(tab.evaluate("() => sessionStorage.getItem('race.saved')")) == '["Prefer Not to Disclose","White"]'


@pytest.mark.parametrize("query, given, ticked", [
    ("?reversed", ["Asian"], ["Asian"]),  # never its first page's near neighbour ("South Asian")
    ("?options", ["White"], ["White"]),  # past its first page though the select lists it: searched for
    # a row's words besides its pill's: given again, never unticked
    ("?hidden", ["Asian", "Asian", "Asian"], ["Asian"]),
    ("?reversed&hidden", ["Asian", "Asian"], ["Asian"]),  # both: the narrowed list's nearest, not the first page's
])
def test_a_brassring_search_box_ticks_the_answer_itself_once(srv, query, given, ticked):
    """The review of 0.3.169: on BrassRing's search box for several choices, the answer is the one ticked (searched for
    where its first page shows only a near one, or none), and a choice already ticked is never clicked again, whatever
    else its row says besides its words."""
    page = page_of(srv, "site/brassring-autocomplete-multi.html", query)
    race = next(f for f in page["fields"] if f["label"].startswith("How would you like to identify your race"))
    for value in given:
        done = run(srv.browser.fill([{"id": race["id"], "value": value, "pick_only": True}]))
        assert all(r["ok"] for r in done), done
    tab = run(srv.browser.page())
    pills = "() => Array.from(document.querySelectorAll('.selectionList li')).map((li) => li.firstChild.textContent)"
    assert run(tab.evaluate(pills)) == ticked


def test_an_oracle_multi_select_is_read_from_its_pills_and_ticked_once(srv):
    """Oracle's cx-multi-select (Mission Support and Test Services' question 7, the nightly's run 144): a box whose
    choices, once ticked, show as pills under it. Its pills weren't read, so a pick that went in read as empty: the
    desk's second try at the answer clicked the choice again, which unticked it, and the same question was asked seven
    rounds. Its value is its pills; a choice is clicked once, only where it isn't ticked, and taken where its pill then
    shows; the list is closed after (the widget sets the answer only then)."""
    page = run(srv.browser.goto(fixture_url("site/oracle-multiselect.html")))
    assert page["fields"] == 2, page
    form = run(srv.browser.inspect(include_dropdown_options=True))
    q7 = next(f for f in form["fields"] if f["label"].startswith("7. Have you ever worked for Example Labs"))
    assert (q7["kind"], q7.get("value"), q7.get("required")) == ("combobox", "", True), q7
    assert "N/A" in q7.get("options", []), q7
    tab = run(srv.browser.page())
    pills = ("() => Array.from(document.querySelectorAll('.cx-multi-select-pill__value-text'))"
             ".map((p) => p.textContent.trim())")
    for _ in range(2):  # (given again, a second try or a second pass: it stays ticked)
        done = run(srv.browser.fill([{"id": q7["id"], "value": "N/A"}]))
        assert all(r["ok"] for r in done), done
        assert run(tab.evaluate(pills)) == ["N/A"]
        assert run(tab.evaluate("() => window.committed")) == ["N/A"]  # (its list closed: the answer set)
        after = next(f for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"]
                     if f["id"] == q7["id"])
        assert after.get("value") == "N/A", after


@pytest.mark.parametrize("query", ["?refuse", "?rowonly"])
def test_an_oracle_multi_select_that_wont_take_a_pick_says_so(srv, query):
    """A pick the box doesn't take (no pill after the click) is said, for the person to tick it, never taken for an
    answer: read as filled, the same question would come back each round with the desk sure it had answered it.
    ?rowonly (the review's): its row marked selected, as one the pointer is over, is no pick without its pill."""
    page_of(srv, "site/oracle-multiselect.html", query)
    form = run(srv.browser.inspect(include_dropdown_options=True))
    q7 = next(f for f in form["fields"] if f["label"].startswith("7. Have you ever worked for Example Labs"))
    done = run(srv.browser.fill([{"id": q7["id"], "value": "N/A"}]))
    assert not done[0]["ok"] and "desk's browser window" in done[0]["error"], done
    after = next(f for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"] if f["id"] == q7["id"])
    assert after.get("value") == "", after


def test_a_security_answer_put_in_is_never_read_back_nor_saved(srv, tmp_path):
    """An account's security answer from the desk's card (the review of the card): its box is marked before it's filled,
    so it reads back "(set)", as a password, even where the box held it already ("already set"), shows as dots, and
    a saved page (a stop's, a problem report's) holds neither it nor a picture of it."""
    fields = fields_of(srv, tmp_path, '<label for="a">Question 1 Answer</label><input id="a" value="Flagstaff Ridge '
                                      '1987"><label for="b">Nickname</label><input id="b">')
    ids = {f["label"]: f["id"] for f in fields}
    done = run(srv.browser.fill([{"id": ids["Question 1 Answer"], "value": "Flagstaff Ridge 1987", "secret": True},
                                 {"id": ids["Nickname"], "value": "Sam"}]))
    assert all(r["ok"] for r in done), done
    after = {f["label"]: f["value"] for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"]}
    assert after == {"Question 1 Answer": "(set)", "Nickname": "Sam"}, after
    page = run(srv.browser.page())
    assert run(page.evaluate("() => getComputedStyle(document.getElementById('a')).webkitTextSecurity")) == "disc"
    dest = run(srv.browser.snapshot(tmp_path / "saved"))
    saved = "".join(f.read_text(encoding="utf-8") for f in dest.iterdir() if f.suffix in (".html", ".json"))
    assert "Flagstaff" not in saved and "Sam" in saved and not (dest / "screenshot.jpg").exists()


def test_a_security_box_reads_as_answered_only_with_its_answer_seen_going_in(srv, tmp_path):
    """The second review of the card: a list for a security question picked but not taken (the choice isn't one it
    has) stays on its placeholder, whose value can be "-1", or a menu button showing "Select question": marked as the
    person's, it read "(set)" and the account was pressed. A list or menu on its placeholder reads empty, and anything
    in a box the desk marked but didn't see its answer go in reads "(unconfirmed)", never its value; so does a list
    put back on another choice since (the third review: a placeholder "-- Please choose --" the pattern misses)."""
    fields = fields_of(srv, tmp_path, '<label for="q">Question 1</label><select id="q"><option value="-1">Select '
                                      'question</option><option value="0">In what city were you born?</option></select>'
                                      '<label for="m">Question 2</label><button type="button" id="m" '
                                      'aria-haspopup="listbox">Select question</button>'
                                      '<label for="a">Question 1 Answer</label><input id="a" value="Flagstaff">'
                                      '<label for="c">Question 3</label><select id="c"><option value="0">-- Please '
                                      'choose --</option><option value="1">In what city were you born?</option>'
                                      '</select>')
    ids = {f["label"]: f["id"] for f in fields}
    done = run(srv.browser.fill([{"id": ids["Question 1"], "value": "What was your first car?", "secret": True},
                                 {"id": ids["Question 3"], "value": "In what city were you born?", "secret": True}]))
    assert [r["ok"] for r in done] == [False, True], done
    assert {f["label"]: f["value"] for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"]}[
        "Question 3"] == "(set)"
    page = run(srv.browser.page())
    run(page.evaluate("ids => ids.forEach((id) => document.querySelector(`[data-ja-id='${id}']`)"
                      ".setAttribute('data-ja-secret', 'pending'))", [ids["Question 2"], ids["Question 1 Answer"]]))
    run(page.evaluate("() => { document.getElementById('c').selectedIndex = 0; }"))  # (put back by the person)
    after = {f["label"]: f["value"] for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"]}
    assert after == {"Question 1": "", "Question 2": "", "Question 1 Answer": "(unconfirmed)",
                     "Question 3": "(unconfirmed)"}, after


def test_a_button_marked_aria_live_is_never_read_as_the_pages_error(srv, tmp_path):
    """BrassRing's application page (probe 81) marks its "Let's get started" button and its "Save and finish later"
    link aria-live, so the form reader read them as the page's errors. A button or a link is never one; the page's
    own alert still is."""
    page = tmp_path / "apply.html"
    page.write_text('<!doctype html><html><body><h1>Senior Analyst</h1><div role="navigation">'
                    '<button type="button" aria-live="assertive">Let\'s get started</button> '
                    '<a href="#" aria-live="assertive">Save and finish later</a></div>'
                    '<div role="alert">Your session will expire in 5 minutes</div></body></html>', encoding="utf-8")
    run(srv.browser.goto(page.resolve().as_uri()))
    assert (run(srv.browser.inspect(include_dropdown_options=False))["errors"]
            == ["Your session will expire in 5 minutes"])


def test_a_job_alert_sign_up_with_three_boxes_is_beside_the_application(srv, tmp_path):
    """CommonSpirit's careers search (Dignity Health, run 82): its job alert sign-up, "Sign up for Job Alerts", has five
    boxes (an email, a job category, a location, Remote and Job Type) and a spam trap hidden from the eye, more than a
    footer's one or two, so it was read as the page's form: the desk filled the person's email into it and asked for its
    category and location. A form whose own heading says it's a sign-up for job alerts is beside the application, and
    so is its Submit."""
    run(srv.browser.goto(fixture_url("site/careers-search-job-alert.html")))
    page = run(srv.browser.inspect(include_dropdown_options=False))
    aside = {f["label"].rstrip("*"): bool(f.get("aside")) for f in page["fields"]}
    assert (set(aside) >= {"Email Address", "Job Category", "Location", "Remote", "Job Type"}
            and all(aside.values())), aside
    assert [bool(a.get("aside")) for a in page["actions"] if a["text"].strip() == "Submit"] == [True], page["actions"]


_APPLICATION = ('<label for="fn">First name*</label><input id="fn" required><label for="ln">Last name*</label><input '
                'id="ln" required><label for="em">Email*</label><input id="em" type="email" required>')


@pytest.mark.parametrize("body", [
    # a last step headed about job alerts, whose own button sends the application
    '<form><h2>Job Alerts and Communication Preferences</h2><p>Would you like to receive text messages about your '
    'application?</p><label><input type="radio" name="sms" value="y" required> Yes</label><label><input type="radio" '
    'name="sms" value="n"> No</label><label><input type="checkbox" name="certify" required> I certify that the '
    'information I provided is true</label><label for="sig">Electronic signature</label><input id="sig" name="sig" '
    'required><button type="button">Back</button><button type="submit">Submit Application</button></form>',
    # a form around the whole page: a newsletter's heading hidden in it, a sign-in in its main
    '<form id="aspnetForm"><div style="display:none"><h2>Subscribe to our newsletter</h2></div><header><a href="#">Home'
    '</a></header><main><h1>Candidate Login</h1><label for="u">Email</label><input id="u" type="email"><label for="p">'
    'Password</label><input id="p" type="password"><button type="submit">Sign In</button></main></form>',
    # and one with a list of similar jobs beside the application
    '<form id="form1"><aside><h3>Similar Jobs</h3><a href="#">Technician II</a></aside><main><h1>Apply: Technician</h1>'
    '<label for="n">Full name</label><input id="n" required><label for="e">Email</label><input id="e" type="email" '
    'required><label for="ph">Phone</label><input id="ph" type="tel" required><button type="submit">Submit</button>'
    '</main></form>',
    # (the second review's) an evergreen posting for a talent pool, its title the form's heading
    '<form><h2>Manufacturing Technician - Talent Pool</h2>' + _APPLICATION
    + '<button type="submit">Submit</button></form>',
    # a way on read by its words, not its value alone ("Submit Application", value "Submit"), and Taleo's input buttons
    '<form><h2>Job Alerts and Communication Preferences</h2><p>Would you like texts?</p><label><input type="radio" '
    'name="sms" value="y" required> Yes</label><label><input type="radio" name="sms" value="n"> No</label><label>'
    '<input type="checkbox" name="certify" required> I certify</label><label for="sig">Electronic signature</label>'
    '<input id="sig" required><button type="submit" name="act" value="Submit">Submit Application</button></form>',
    '<form><h2>Newsletter</h2>' + _APPLICATION + '<input type="button" value="Save and Continue"></form>',
    '<form><h2>Get Job Alerts</h2><label for="ph">Mobile phone</label><input id="ph" type="tel"><a class="btn" '
    'href="javascript:void(0)">Next</a></form>',
    # and a sign-up's heading far above the application's boxes in a form around the page
    '<form id="aspnetForm"><div class="top"><h3>Get Job Alerts</h3><a href="/alerts">Sign up</a></div><div><div>'
    'Technician</div><p>' + 'We are hiring. ' * 30 + '</p>' + _APPLICATION
    + '<button type="submit">Submit</button></div></form>',
], ids=["last step", "whole page", "similar jobs", "talent pool", "button value", "input button", "link", "far above"])
def test_a_form_whose_heading_isnt_its_own_or_that_applies_isnt_a_job_alert_sign_up(srv, tmp_path, body):
    """The reviews of 0.3.164: the job alert rule reads a form's own heading, the nearest one shown before its boxes and
    close above them, that names a sign-up, in a form that isn't the page's frame, and never one whose own way on goes
    on with an application, by any of its words."""
    assert not any(f.get("aside") for f in fields_of(srv, tmp_path, body))


def test_a_form_whose_kind_is_a_job_alert_is_beside_the_application(srv, tmp_path):
    """A form the site itself names a job alert's (data-form-type "JobAlert", however it's spelled), heading or not."""
    fields = fields_of(srv, tmp_path, '<form data-form-type="JobAlert"><label for="e">Email</label><input id="e" '
                       'type="email"><label for="c">Category</label><select id="c"><option>A</option></select><label '
                       'for="l">Location</label><input id="l"><button type="submit">Submit</button></form>')
    assert fields and all(f.get("aside") for f in fields), fields


def test_a_groups_question_written_inside_it_as_a_paragraph_is_its_label(srv, tmp_path):
    """The review of Oct 15: a radio group whose question is written inside its fieldset as a plain paragraph (no
    legend) read as label "", so nothing could answer it. Its first words before the first choice are its question; a
    choice's own label never is, and a legend still comes first."""
    got = fields_of(srv, tmp_path, '<fieldset><input type="radio" name="x0" value="a" id="x0a">'
                                   '<label for="x0a">A</label><input type="radio" name="x0" value="b" id="x0b"><label '
                                   'for="x0b">B</label></fieldset><fieldset><p>Are you at least 18 years of age?</p>'
                                   '<div><input type="radio" name="q1" value="yes" id="q1y">'
                                   '<label for="q1y">Yes</label><input type="radio" name="q1" value="no" id="q1n">'
                                   '<label for="q1n">No</label></div></fieldset>'
                                   '<fieldset><label for="q2y">Yes</label><input type="radio" name="q2" id="q2y">'
                                   '<label for="q2n">No</label><input type="radio" name="q2" id="q2n"></fieldset>'
                                   '<fieldset><legend>Do you need sponsorship?</legend><p>Choose one.</p>'
                                   '<input type="radio" name="q3" id="q3y"><label for="q3y">Yes</label>'
                                   '<input type="radio" name="q3" id="q3n"><label for="q3n">No</label></fieldset>')
    labels = [f["label"] for f in got if f["kind"] == "radio_group"]
    assert labels[1:] == ["Are you at least 18 years of age?", "", "Do you need sponsorship?"], labels


def test_a_box_in_a_wrapper_folded_shut_isnt_read_as_shown(srv, tmp_path):
    """The review of Oct 15: a box inside a wrapper folded shut (an accordion closed: height 0, its overflow hidden)
    keeps its own size, and was read as shown: the desk filled, or asked about, a box the person can't see. An open
    wrapper's box is read as before."""
    got = fields_of(srv, tmp_path, '<div style="height: 0; overflow: hidden"><label for="a">Folded Box</label>'
                                   '<input id="a"></div>'
                                   '<div style="overflow: hidden"><label for="b">Open Box</label><input id="b"></div>'
                                   '<label for="c">Plain Box</label><input id="c">')
    assert [f["label"] for f in got] == ["Open Box", "Plain Box"]
    # (the review of 0.3.188: a fixed dialog, a modal dialog and a box placed against a wrapper outside the folded one
    # are drawn wherever the folded wrapper is: read)
    got = fields_of(srv, tmp_path, '<div style="overflow: hidden"><div role="dialog" aria-modal="true" '
                                   'style="position: fixed; inset: 10%; background: #fff">'
                                   '<label for="fn">Fixed Box</label><input id="fn"></div></div>'
                                   '<div style="overflow: hidden"><dialog id="d"><label for="u">Modal Box</label>'
                                   '<input id="u"></dialog></div><script>document.getElementById("d").showModal()</scri'
                                   'pt>')
    assert [f["label"] for f in got] == ["Fixed Box", "Modal Box"], got
    got = fields_of(srv, tmp_path, '<div style="position: relative"><div style="overflow: hidden">'
                                   '<div style="position: absolute; top: 40px"><label for="a">Placed Box</label>'
                                   '<input id="a"></div></div></div>')
    assert [f["label"] for f in got] == ["Placed Box"], got


def test_a_progress_bar_drawn_fully_transparent_is_no_sign_of_loading(srv, tmp_path):
    """The review of Oct 15: a progress bar left on the page at opacity 0 (itself or a wrapper) kept the page "busy",
    and the desk waited for nothing. One that can be seen still is."""
    page = tmp_path / "form.html"
    bar = '<div role="progressbar" style="width: 200px; height: 8px; background: #08c"></div>'
    for wrap, busy in (("{}", True), ('<div style="opacity: 0">{}</div>', False),
                       (bar.replace('background', 'opacity: 0; background'), False)):
        page.write_text('<!doctype html><html><body><label for="n">Name</label><input id="n">'
                        + (wrap.format(bar) if "{}" in wrap else wrap) + '</body></html>', encoding="utf-8")
        run(srv.browser.goto(page.resolve().as_uri()))
        assert bool(run(srv.browser.inspect(include_dropdown_options=False)).get("busy")) is busy, wrap


def test_an_id_a_page_set_itself_is_never_made_into_a_selector(srv, tmp_path):
    """The review of Oct 15: the form reader kept a data-ja-id a page had set itself, and the desk's selectors were
    made from it: one with quotes in it could steer a fill to another box. Only an id of the reader's own shape is kept;
    a selector is never made from any other."""
    from job_apply.browser import _by_ja_id

    got = fields_of(srv, tmp_path, '<label for="a">Name</label><input id="a" data-ja-id=\'x"], [id="b\'>'
                                   '<label for="b">Other</label><input id="b">')
    assert all('"' not in f["id"] and "]" not in f["id"] for f in got), got
    # (a group's id too: the review of 0.3.188)
    got = fields_of(srv, tmp_path, '<fieldset data-ja-gid=\'x"], [id="b\'><legend>Pick one</legend>'
                                   '<input type="radio" name="r" id="r1" data-ja-gid-member=\'x"], [id="b\'>'
                                   '<label for="r1">A</label><input type="radio" name="r" id="r2">'
                                   '<label for="r2">B</label></fieldset><input id="b">')
    assert all('"' not in f["id"] and "]" not in f["id"] for f in got), got
    assert _by_ja_id("f2-abc17.0") == '[data-ja-id="f2-abc17.0"]'
    for bad in ('x"], [id="b', "a b", ""):
        with pytest.raises(KeyError):
            _by_ja_id(bad)


def test_each_question_in_one_fieldset_is_its_own_and_a_hint_inside_a_group_isnt_one(srv, tmp_path):
    """The review of 0.3.188: a fieldset holding several questions gave each group the first question's words (the
    desk then ticked "Yes" to needing sponsorship from the age answer); a hint inside a group ("Select one") beat the
    question written before it; a hidden "This field is required" became a label."""
    got = fields_of(srv, tmp_path, '<h1>Application Questions</h1><fieldset><p>Are you at least 18 years of age?</p>'
                                   '<div><input type="radio" name="age" id="a1"><label for="a1">Yes</label>'
                                   '<input type="radio" name="age" id="a2"><label for="a2">No</label></div>'
                                   '<p>Will you now or in the future require visa sponsorship?</p>'
                                   '<div><input type="radio" name="spon" id="s1"><label for="s1">Yes</label>'
                                   '<input type="radio" name="spon" id="s2"><label for="s2">No</label></div></fieldset>'
                                   '<div><span>Do you have a valid driver\'s license?</span><fieldset>'
                                   '<span class="hint">Select one</span><input type="radio" name="dl" id="d1">'
                                   '<label for="d1">Yes</label><input type="radio" name="dl" id="d2"><label for="d2">No'
                                   '</label></fieldset></div>'
                                   '<p>Are you legally authorized to work in the United States?</p>'
                                   '<div role="radiogroup"><div style="display:none">This field is required</div>'
                                   '<input type="radio" name="w" id="w1"><label for="w1">Yes</label>'
                                   '<input type="radio" name="w" id="w2"><label for="w2">No</label></div>')
    assert [f["label"] for f in got if f["kind"] == "radio_group"] == [
        "Are you at least 18 years of age?", "Will you now or in the future require visa sponsorship?",
        "Do you have a valid driver's license?", "Are you legally authorized to work in the United States?"]
