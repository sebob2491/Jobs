"""Drive the MCP tools against local mock application forms in headless Chromium."""


from pathlib import Path

import pytest
from conftest import browser_available, by_label, fixture_url, run

from job_apply import browser as browser_module
from job_apply import config

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


def test_generic_form_autofill_and_submit_guard(srv):
    job = srv.add_job(url=fixture_url("generic_form.html"), title="Field Service Engineer", company="Example Litho")["job"]
    summary = run(srv.open_application(job_id=job["id"]))
    assert summary["fields"] >= 15
    assert summary["submit_policy"] == "after_user_confirms"

    form = run(srv.inspect_form())
    fields = form["fields"]
    auth = by_label(fields, "legally authorized")
    assert auth["kind"] == "radio_group" and auth["options"] == ["Yes", "No"] and auth["required"]
    shifts = by_label(fields, "which shifts")
    assert shifts["kind"] == "checkbox_group" and shifts["options"] == ["Days", "Nights", "Weekends"]
    assert by_label(fields, "phone")["label"] == "Phone number"  # from placeholder
    assert by_label(fields, "city")["label"] == "City"  # from preceding text
    assert by_label(fields, "resume")["kind"] == "file"  # hidden input still found
    assert by_label(fields, "create a password")["kind"] == "password"
    assert any(a.get("is_submit") for a in form["actions"])

    result = run(srv.autofill())
    filled = {f["label"]: f["value"] for f in result["filled"]}
    assert filled["First Name *"] == "Sam"
    assert filled["State"] == "Arizona"
    assert filled["Country"] == "United States of America"
    assert filled["Gender"] == "I decline to self-identify"
    assert filled["Veteran Status"] == "I don't wish to answer"
    assert filled["How did you hear about this position?"] == "LinkedIn"
    assert filled["Are you willing to travel up to 75% of the time?"] == "Yes"
    assert any(v.endswith("resume.pdf") for v in filled.values())
    assert not result["failed"], result["failed"]
    pending = [f["label"] for f in result["needs_input"]]
    assert pending[0].startswith("Why do you want")  # required first
    assert any("certify" in p for p in pending)  # consent left for the person
    assert not any("password" in p.lower() for p in pending)

    after = {f["label"]: f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    assert after["Last Name *"]["value"] == "Rivera"
    assert after["Will you now or in the future require sponsorship for employment visa status (e.g., H-1B)?"]["value"] == \
        "No, I will not require sponsorship"  # visually hidden radio
    assert after["Resume/CV *"]["value"] == "resume.pdf"

    why = by_label(fields, "why do you want")
    certify = by_label(fields, "certify")
    out = run(srv.fill_form([{"id": why["id"], "value": "I have maintained litho tools for 4 years."},
                             {"id": certify["id"], "value": True},
                             {"id": shifts["id"], "value": ["Days", "Weekends"]}]))
    assert out["ok"], out

    blocked = run(srv.click("Submit Application"))
    assert blocked["clicked"] is False and "submit_application" in blocked["blocked"]

    refused = run(srv.submit_application(job_id=job["id"]))
    assert refused["submitted"] is False

    done = run(srv.submit_application(job_id=job["id"], user_confirmed=True))
    assert done["submitted"] and done["confirmed"], done
    assert srv.get_job(job["id"])["job"]["status"] == "applied"


def test_workday_style_widgets(srv):
    job = srv.add_job(url=fixture_url("workday_like.html"), title="Field Service Engineer", company="Example Litho")["job"]
    run(srv.open_application(job_id=job["id"]))
    form = run(srv.inspect_form())
    code = by_label(form["fields"], "country phone code")
    assert code["kind"] == "listbox" and code["required"]
    assert "United States of America (+1)" in code["options"]  # read by opening the dropdown
    assert by_label(form["fields"], "how did you hear")["kind"] == "combobox"

    result = run(srv.autofill())
    assert not result["failed"], result["failed"]
    after = {f["label"]: f["value"] for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    assert after["Country Phone Code*"] == "United States of America (+1)"
    assert after["Phone Device Type*"] == "Mobile"
    assert after["Phone Number*"] == "480-555-0123"
    assert after["How Did You Hear About Us?*"] == "LinkedIn"
    assert after["Address Line 1"] == "100 W Main St"

    nxt = run(srv.click("Save and Continue"))
    assert nxt["clicked"] and "Review" in nxt["headings"]
    assert run(srv.click("Submit"))["clicked"] is False


def test_a_dropdown_just_above_a_sticky_footer_is_picked_with_a_real_click(srv):
    """A Workday "Application Questions" page (live, Oct 2026): ten Yes/No dropdowns took their
    answers, but the last, a long sponsorship question with a note, sat just above the sticky
    "Save and Continue" bar. Its menu opened under the bar, the click on "No" landed on the bar,
    the script's click that followed was ignored, and it failed with "Picked 'No' but the field
    shows 'Select One'" (the person's own answer from the desk too). A dropdown is brought to
    the middle of the window before its menu is opened."""
    run(srv.browser.goto(fixture_url("site/workday-footer-listbox.html")))
    result = run(srv.autofill())
    assert not result["failed"], result["failed"]
    page = run(srv.browser.page())
    assert run(page.evaluate("() => ['q1', 'q2'].map((id) => document.getElementById(id).dataset.picked)")) == \
        ["Yes", "No"]
    assert {f["label"]: f["value"] for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]
            if f["kind"] == "listbox"} == {
        "Are you legally authorized to work in the United States?*": "Yes",
        "Will you now or in the future require sponsorship for employment visa status (e.g., H-1B visa status)?*": "No"}
    # the bar takes clicks again
    assert run(page.evaluate("() => [getComputedStyle(document.querySelector('footer')).pointerEvents, "
                             "document.querySelectorAll('[data-ja-through]').length]")) == ["auto", 0]
    # the person's own answer, from the desk, goes in the same way
    run(srv.browser.goto(fixture_url("site/workday-footer-listbox.html")))
    sponsorship = by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "sponsorship")
    assert run(srv.fill_form([{"id": sponsorship["id"], "value": "Yes"}]))["ok"]
    assert run(page.evaluate("() => document.getElementById('q2').dataset.picked")) == "Yes"


def test_workday_2026_search_prompt_is_picked_not_typed(srv):
    """Onto's Workday (Oct 2026): the prompt is a plain search input; typed text that isn't
    picked from its list vanishes, and the pick shows as a pill beside the input."""
    run(srv.browser.goto(fixture_url("workday_prompt_2026.html")))
    fields = run(srv.inspect_form(include_dropdown_options=False))["fields"]
    heard = by_label(fields, "how did you hear")
    assert heard["kind"] == "combobox" and heard["value"] == ""
    assert by_label(fields, "country phone code")["value"] == "United States of America (+1)"
    assert heard.get("search")  # its list on opening is only the top level
    # a group in the top level opens its own list: its entries are what to choose between
    out = run(srv.fill_form([{"id": heard["id"], "value": "Job Board"}]))
    assert not out["results"][0]["ok"] and out["results"][0]["options"] == ["Indeed", "LinkedIn"], out
    assert by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "how did you hear")["value"] == ""
    # a group that holds an entry of its own name: that entry is the answer
    out = run(srv.fill_form([{"id": heard["id"], "value": "Company Website"}]))
    assert out["results"][0]["ok"], out
    assert by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "how did you hear")["value"] == \
        "Company Website"
    run(srv.browser.goto(fixture_url("workday_prompt_2026.html")))
    heard = by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "how did you hear")
    # an entry inside a group is found by searching
    out = run(srv.fill_form([{"id": heard["id"], "value": "LinkedIn"}]))
    assert out["results"][0]["ok"], out
    after = run(srv.inspect_form(include_dropdown_options=False))["fields"]
    assert by_label(after, "how did you hear")["value"] == "LinkedIn"
    assert by_label(after, "country phone code")["value"] == "United States of America (+1)"



def test_a_workday_groups_own_list_drawn_over_the_lists_below_is_read_as_its_own(srv):
    """Air Products' and Wells Fargo's Workday (run 44; live probe 47, Oct 13): "How Did You Hear About Us?" sits
    above the Country and Prefix lists. A group's own list, drawn under a header (its back button and name), covers
    them, nearer the Country list's edge than its own box, so it was taken for the Country list's menu and nothing
    was read ("Picked 'Website' but the field didn't take it", every time). A menu drawn over another list isn't
    that list's: the group's entries are read, its entry for the answer picked, or the entries asked about."""
    run(srv.browser.goto(fixture_url("site/workday-prompt-over-lists.html")))
    heard = by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "how did you hear")
    out = run(srv.fill_form([{"id": heard["id"], "value": "Company Website"}]))
    assert out["results"][0]["ok"], out
    assert by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "how did you hear")["value"] == \
        "Company Website"
    run(srv.browser.goto(fixture_url("site/workday-prompt-over-lists.html")))
    heard = by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "how did you hear")
    out = run(srv.fill_form([{"id": heard["id"], "value": "Advert on Job Board"}]))
    assert not out["results"][0]["ok"] and out["results"][0]["options"] == ["Other"], out
    # an entry inside a group, searched for: Workday chooses the one it finds at once, and closes the list (live
    # probe 45): that's the answer taken, not "nothing in its list matched"
    out = run(srv.fill_form([{"id": heard["id"], "value": "Newspaper"}]))
    assert out["results"][0]["ok"], out
    assert by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "how did you hear")["value"] == \
        "Newspaper"


def test_self_identification_dropdowns_are_picked_from_their_lists_never_typed_into(srv, job_apply_home):
    """Reported in use (Oct 2026): the desk typed self-identification answers into dropdowns,
    which didn't work. In the live check, onsemi's Oracle Gender lists only Female and Male, and
    because Oracle's lists are searched, "Decline to self-identify" was typed in, found nothing,
    and the box stayed empty. Qorvo's disability list's "I do not want to answer" read as a
    "No", so a plain "No" picked nothing. Micron's veteran question carries the VEVRAA notice in
    its label, whose "for more information, call" read as an on-call question. Each answer is
    now the list's own entry for it, clicked; an answer a list doesn't have is said, with the
    profile's answer, and nothing is typed."""
    import yaml

    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text())
    data["eeo"]["disability"] = "No"  # (gender "Decline to self-identify", veteran "I don't wish to answer")
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data))
    run(srv.browser.goto(fixture_url("site/eeo-dropdowns.html")))
    result = run(srv.autofill())
    filled = {f["label"]: f["value"] for f in result["filled"]}
    assert filled["Pre-Offer : Disability"] == "No, I do not have a disability and have not had one in the past"
    # (its list shows only once fetched: the profile's answer went to the fill, which picked the list's own)
    assert by_label(result["filled"], "VEVRAA")["from"] == "veteran"
    assert not result["failed"], result["failed"]
    unoffered = {f["label"]: f.get("unmatched") for f in result["needs_input"]}
    assert unoffered == {"Gender": "Decline to self-identify",
                         "Pre-Offer : Are you a Protected Veteran?": "I don't wish to answer"}

    page = run(srv.browser.page())
    boxes = run(page.evaluate("() => [...document.querySelectorAll('input[role=combobox]')].map((b) => "
                              "[b.value, b.dataset.picked || null, b.dataset.typed || null])"))
    assert boxes == [["", None, None],
                     ["No, I do not have a disability and have not had one in the past",
                      "No, I do not have a disability and have not had one in the past", None],
                     ["", None, None],
                     ["I DO NOT WISH TO SELF-IDENTIFY", "I DO NOT WISH TO SELF-IDENTIFY", None]]


def test_a_successfactors_list_is_searched_and_a_search_that_picks_nothing_is_emptied(srv, job_apply_home):
    """Qorvo's SuccessFactors (Oct 2026): its dropdowns list their first 100 entries (the
    countries stop at Iran) and find the rest as you type. Words typed that pick nothing stay
    in the box, where they read as an answer: the desk went on to the submit step while the
    site still said "Preferred Locale/Language is required"."""
    run(srv.browser.goto(fixture_url("site/sf-select-form.html")))
    fields = run(srv.inspect_form())["fields"]
    country, locale = by_label(fields, "country"), by_label(fields, "locale")
    assert country["kind"] == "combobox" and country["paged"] and len(country["options"]) == 100
    assert "United States" not in country["options"]
    assert by_label(fields, "veteran")["options"] == ["No Selection", "I am a protected veteran",
                                                       "I am not a protected veteran"]
    result = run(srv.autofill())
    assert {f["label"]: f["value"] for f in result["filled"]}["Country"] == "United States", result
    assert not result["failed"], result["failed"]  # the veteran list is all there is: nothing typed into it
    assert [f["label"] for f in result["needs_input"] if f.get("required")] == ["Preferred Locale/Language"]

    out = run(srv.fill_form([{"id": locale["id"], "value": "Klingon"}]))
    assert not out["ok"] and "Klingon" in out["results"][0]["error"], out
    after = run(srv.inspect_form(include_dropdown_options=False))["fields"]
    assert by_label(after, "locale")["value"] == ""  # not "Klingon"
    assert by_label(after, "country")["value"] == "United States"

    # a country the list doesn't have: the field comes back with what to ask instead
    profile = job_apply_home / "profile.yaml"
    profile.write_text(profile.read_text().replace("country: United States", "country: Atlantis"))
    run(srv.browser.goto(fixture_url("site/sf-select-form.html")))
    failed = run(srv.autofill())["failed"]
    assert [(f["label"], f["kind"], f["required"], len(f["options"])) for f in failed] == [("Country", "combobox", True, 100)]
    assert by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "country")["value"] == ""


def test_an_answer_that_picks_a_group_is_chosen_again_among_its_entries(srv, job_apply_home):
    """Robert Half's Workday (live, Oct 2026): "Company Website" matched only the top level's
    "Website / Job Board Posting", a group whose entries hold "Company Career Site". The desk
    asked the person; the answer is chosen again among the group's entries and filled."""
    profile = job_apply_home / "profile.yaml"
    profile.write_text(profile.read_text().replace("how_did_you_hear: LinkedIn", "how_did_you_hear: Company Website"))
    run(srv.browser.goto(fixture_url("workday_prompt_2026.html") + "?groups=staffing"))
    result = run(srv.autofill())
    assert not [f for f in result["failed"] if "hear" in (f["label"] or "").lower()], result["failed"]
    heard = by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "how did you hear")
    assert heard["value"] == "Company Career Site"


def test_how_did_you_hear_is_chosen_again_from_a_list_that_drew_late(srv, job_apply_home):
    """Workday's "How Did You Hear About Us?" (live, issue #112, note 27): the prompt listed nothing when the
    page was read, so "Company Website" went to the fill as it was and found no match in the list that came
    after ("doesn't match any suggestion: [..., 'Acme website']"). The failed field comes back with that
    list, the answer is chosen again among its entries ("Acme website") and filled, inside the group it opens."""
    profile = job_apply_home / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8").replace("how_did_you_hear: LinkedIn", "how_did_you_hear: Company Website"),
                       encoding="utf-8")
    job = srv.add_job(url=fixture_url("workday_prompt_2026.html") + "?groups=employer&slow=1500",
                      title="Technician", company="Acme")["job"]
    run(srv.open_application(job_id=job["id"]))
    result = run(srv.autofill(job_id=job["id"]))
    assert not [f for f in result["failed"] if "hear" in (f["label"] or "").lower()], result["failed"]
    heard = by_label(run(srv.inspect_form(include_dropdown_options=False))["fields"], "how did you hear")
    assert heard["value"] == "Acme website"
    # an answer the list has no match for (and isn't the employer's site) is left to the person, with the list
    profile.write_text(profile.read_text(encoding="utf-8").replace("how_did_you_hear: Company Website", "how_did_you_hear: Radio"),
                       encoding="utf-8")
    run(srv.browser.goto(fixture_url("workday_prompt_2026.html") + "?groups=employer&slow=1500"))
    failed = [f for f in run(srv.autofill(job_id=job["id"]))["failed"] if "hear" in (f["label"] or "").lower()]
    assert [f["options"] for f in failed] == [["Advertisement", "Acme website", "Job Board"]], failed


def test_a_list_the_page_draws_is_read_page_by_page(srv):
    """Amkor's career site (SuccessFactors' older pages) draws its job list with a script,
    10 to a page: a bigger page is chosen, then the next-page arrow pressed until it's gone."""
    from job_apply.search import SFCLASSIC_NEXT, SFCLASSIC_ROWS, parse_sfclassic

    pages = run(srv.browser.listing_pages(fixture_url("site/sf-classic-list.html"), SFCLASSIC_ROWS,
                                          per_page=("li.per_page select", "20"), next_button=SFCLASSIC_NEXT))
    cfg = {"site": "https://career8.successfactors.com", "company": "example"}
    listings = [x for page in pages for x in parse_sfclassic(page, cfg)]
    assert [len(parse_sfclassic(page, cfg)) for page in pages] == [20, 3]
    assert len({x.external_id for x in listings}) == 23
    tech = next(x for x in listings if x.title == "Equipment Technician (ATA)")
    assert (tech.external_id, tech.posted, tech.url) == ("29004", "2026-09-24", (
        "https://career8.successfactors.com/career?career_ns=job_listing&company=example&navBarLevel=JOB_SEARCH"
        "&rcm_site_locale=en_US&career_job_req_id=29004&selected_lang=en_US"))  # no session in it


SF_POSTING_QUERY = "?career_ns=job_listing&company=example&career_job_req_id=29107"


def test_an_older_successfactors_posting_is_entered_through_its_apply(srv, monkeypatch):
    """Amkor's postings (SuccessFactors' older sites) sit in a form whose submit is "Apply". With
    nothing in the form to fill, it opens the application, so it's pressed like any way in,
    even in a dry run. An "Apply" whose form has something to fill still sends it."""
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    posting = fixture_url("site/sf-classic-posting.html") + SF_POSTING_QUERY
    run(srv.browser.goto(posting))
    applies = [a for a in run(srv.inspect_form(include_dropdown_options=False))["actions"] if a["text"] == "Apply"]
    assert len(applies) == 2 and not any(a.get("is_submit") for a in applies)
    out = run(srv.click(applies[0]["id"]))
    assert out["clicked"] and out["headings"] == ["Sign In"], out

    # the same page with a box to fill in its form: Apply would send that
    run(srv.browser.goto(posting))
    page = run(srv.browser.page())
    run(page.evaluate("() => { const box = document.createElement('input'); box.name = 'name';"
                      " document.getElementById('careerform').prepend(box); }"))
    applies = [a for a in run(srv.inspect_form(include_dropdown_options=False))["actions"] if a["text"] == "Apply"]
    assert applies and all(a.get("is_submit") for a in applies)
    assert run(srv.click(applies[0]["id"]))["clicked"] is False
    # and a page that isn't a posting: an "Apply" submit is the final one, as before
    run(srv.browser.goto(fixture_url("site/sf-classic-posting.html")))
    assert run(srv.click("Apply"))["clicked"] is False

def test_iframe_form_and_linkedin_policy(srv, monkeypatch):
    job = srv.add_job(url=fixture_url("iframe_host.html"), title="Process Technician", company="Example Fab")["job"]
    run(srv.open_application(job_id=job["id"]))
    result = run(srv.autofill())
    labels = {f["label"]: f["value"] for f in result["filled"]}
    assert labels["Email *"] == "sam.rivera@example.com"
    assert labels["Resume/CV *"].endswith("resume.pdf")
    assert all(f["id"].startswith("f") for f in result["filled"])  # ids carry the frame prefix
    assert [f["label"] for f in result["needs_input"]] == ["Cover Letter"]

    # text lookup reaches into the iframe, and the submit guard still applies there
    blocked = run(srv.click("Submit Application"))
    assert blocked["clicked"] is False and "blocked" in blocked

    # Pretend this page is LinkedIn: the tool must hand the final click to the person.
    monkeypatch.setattr(srv, "detect_ats", lambda url: "linkedin")
    out = run(srv.submit_application(job_id=job["id"], user_confirmed=True))
    assert out["submitted"] is False and "click Submit themselves" in out["reason"]
    assert srv.get_job(job["id"])["job"]["status"] == "ready_to_submit"


def test_icims_privacy_gate_is_read_with_its_frames_headings_and_its_phone(srv):
    """Charles Schwab's iCIMS (live, Oct 10): its sign-in gate sits in a frame inside the careers page,
    which has a talent network sign-up of its own. Each frame's headings are said apart (by the
    prefix of its ids), the Phone Country Code's "— Make a Selection —" is no answer, so its
    countries are read, and its Number says it's the phone's (autocomplete). Autofill answers all
    three from the profile."""
    job = srv.add_job(url=fixture_url("site/icims-privacy-gate.html"), title="Analyst", company="Example Financial")["job"]
    run(srv.open_application(job_id=job["id"]))
    form = run(srv.inspect_form())
    gate = next(a for a in form["actions"] if a["text"] == "I Acknowledge the Privacy Notice")
    frame = gate["id"].split("-")[0] + "-"
    assert form["frame_headings"][frame] == ["Privacy Notice and Sign-In"], form["frame_headings"]
    assert "Join our talent network" in form["frame_headings"][""], form["frame_headings"]
    code = by_label(form["fields"], "country code")
    assert code["kind"] == "listbox" and "(+1) United States" in code["options"], code
    assert by_label(form["fields"], "number")["autocomplete"] == "tel-national"
    result = run(srv.autofill())
    filled = {f["label"]: f["value"] for f in result["filled"]}
    assert filled == {"Email": "sam.rivera@example.com", "Country Code — Make a Selection —": "(+1) United States",
                      "Number": "480-555-0123"}, (filled, result["failed"])


def test_auto_mode_refuses_incomplete_form(srv, job_apply_home):
    import yaml

    prof_path = job_apply_home / "profile.yaml"
    prof = yaml.safe_load(prof_path.read_text())
    prof["settings"].update({"submit_mode": "auto", "auto_submit_ats": ["company_site"]})
    prof_path.write_text(yaml.safe_dump(prof))

    job = srv.add_job(url=fixture_url("generic_form.html"), title="Field Service Engineer", company="Example Litho")["job"]
    assert run(srv.open_application(job_id=job["id"]))["submit_policy"] == "auto"
    run(srv.autofill())
    out = run(srv.submit_application(job_id=job["id"]))
    assert out["submitted"] is False
    assert any(label.startswith("Why do you want") for label in out["empty_required"])
    assert srv.get_job(job["id"])["job"]["status"] == "in_progress"


def test_workday_experience_entries(srv):
    job = srv.add_job(url=fixture_url("workday_experience.html"), title="FSE", company="Example Litho")["job"]
    run(srv.open_application(job_id=job["id"]))

    work = run(srv.add_entries("work"))
    assert (work["before"], work["after"], work["wanted"]) == (0, 2, 2)
    edu = run(srv.add_entries("education"))
    assert (edu["after"], edu["wanted"]) == (1, 1)
    assert run(srv.add_entries("work"))["clicks"] == 0  # already enough blocks

    result = run(srv.autofill())
    assert not result["failed"], result["failed"]
    assert not [f for f in result["needs_input"] if f.get("section")], result["needs_input"]

    fields = run(srv.inspect_form(include_dropdown_options=False))["fields"]
    got = {(f.get("section"), f["label"].rstrip("*"), f.get("sublabel")): f["value"] for f in fields}
    assert got[("Work Experience 1", "Job Title", None)] == "Equipment Technician"
    assert got[("Work Experience 1", "I currently work here", None)] is True
    assert got[("Work Experience 1", "From", "Month")] == "03"
    assert got[("Work Experience 1", "From", "Year")] == "2021"
    assert ("Work Experience 1", "To", "Year") not in got  # hidden once "currently work here" is checked
    assert got[("Work Experience 2", "Company", None)] == "Example Fab Services"
    assert got[("Work Experience 2", "To", "Month")] == "02"
    assert got[("Work Experience 2", "Role Description", None)].startswith("PMs and troubleshooting")
    assert got[("Education 1", "School or University", None)] == "Arizona State University"
    assert got[("Education 1", "Degree", None)] == "Bachelor's Degree"
    assert got[("Education 1", "Field of Study", None)] == "Electrical Engineering"
    assert got[("Education 1", "Overall Result (GPA)", None)] == "3.4"
    assert got[("Education 1", "To (Actual or Expected)", "Year")] == "2020"


def test_dry_run_never_submits(srv, monkeypatch):
    from job_apply.browser import SubmitBlocked

    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    job = srv.add_job(url=fixture_url("generic_form.html"), title="FSE", company="Example Litho")["job"]
    assert run(srv.open_application(job_id=job["id"]))["submit_policy"] == "dry_run"
    run(srv.autofill())
    out = run(srv.submit_application(job_id=job["id"], user_confirmed=True))
    assert out["submitted"] is False and "Dry run" in out["reason"]
    assert srv.get_job(job["id"])["job"]["status"] == "ready_to_submit"
    # even the low-level click refuses, and the page is untouched
    submit_id = run(srv.browser.find_submit())[0]["id"]
    with pytest.raises(SubmitBlocked):
        run(srv.browser.press_submit(submit_id))
    assert "Thank you" not in run(srv.page_text())


def test_a_sign_in_forms_submit_may_be_clicked_and_no_other(srv, monkeypatch):
    """SuccessFactors' sign-in button reads "Submit": in a form holding only the email and
    password it sends the sign-in, so it may be clicked, practice mode included. A "Submit"
    with any other box beside the password stays refused."""
    from job_apply.browser import SubmitBlocked

    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    run(srv.browser.goto(fixture_url("site/signin-submit.html") + "?extra"))
    form = run(srv.inspect_form())
    submit = next(a for a in form["actions"] if a["text"] == "Submit")
    assert not submit.get("sign_in_form")
    with pytest.raises(SubmitBlocked):
        run(srv.browser.click(submit["id"]))
    assert run(run(srv.browser.page()).evaluate("() => window.signInTries")) == 0
    run(srv.browser.goto(fixture_url("site/signin-submit.html")))
    form = run(srv.inspect_form())
    submit = next(a for a in form["actions"] if a["text"] == "Submit")
    assert submit.get("sign_in_form") and submit.get("is_submit")
    run(srv.browser.click(submit["id"]))
    assert run(run(srv.browser.page()).evaluate("() => window.signInTries")) == 1


def test_a_date_whose_hint_covers_its_boxes_is_typed_in(srv):
    """Workday's Self Identify draws its date's "MM/DD/YYYY" hint over the Month, Day and Year
    boxes: a click on them timed out (live, Oct 2026), and the date was asked of the person."""
    run(srv.browser.goto(fixture_url("site/workday-date-hint.html")))
    boxes = {f["sublabel"]: f for f in run(srv.inspect_form())["fields"]}
    out = run(srv.fill_form([{"id": boxes["Month"]["id"], "value": "10"}, {"id": boxes["Day"]["id"], "value": "09"},
                             {"id": boxes["Year"]["id"], "value": "2026"}]))
    assert out["ok"] and not out.get("failed"), out
    got = {f["sublabel"]: f["value"] for f in run(srv.inspect_form())["fields"]}
    assert got == {"Month": "10", "Day": "09", "Year": "2026"}, got


def test_each_jobs_currently_work_here_box_is_its_own(srv):
    """Workday names every job's "I currently work here" box alike: the form reader took them for
    one group across the blocks (a report listed "Work Experience 1 (checkbox_group)"), and the
    current job's box was never ticked, which Workday won't save without (live, Oct 2026)."""
    run(srv.browser.goto(fixture_url("site/workday-my-experience.html")))
    run(srv.add_entries("work"))
    boxes = [f for f in run(srv.inspect_form())["fields"] if "currently work" in (f.get("label") or "").lower()]
    assert [(f["kind"], f.get("section")) for f in boxes] == [("checkbox", "Work Experience 1"),
                                                              ("checkbox", "Work Experience 2")], boxes
    run(srv.autofill())
    page = run(srv.browser.page())
    assert run(page.evaluate("() => [document.getElementById('cw-1').checked, document.getElementById('cw-2').checked]")) \
        == [True, False]


def test_a_covered_add_button_is_left_and_said(srv):
    """Nikon's UKG Pro (live, Oct 2026): after "Add Experience", the press of "Add Education" timed out
    (something drawn over it) and the error ended the whole application ("Something went wrong:
    TimeoutError"). A covered Add button is left as it is, and said; the other section's still works."""
    run(srv.browser.goto(fixture_url("site/workday-my-experience.html") + "?covered"))
    education = run(srv.add_entries("education"))
    assert (education["clicks"], education.get("add_button_covered")) == (0, True), education
    assert "covered" in education["note"], education
    work = run(srv.add_entries("work"))
    assert work["clicks"] == 1 and not work.get("add_button_covered"), work


def test_an_add_button_a_toast_covers_for_a_moment_is_pressed_once_it_clears(srv):
    """Workday's My Experience (issue #112, note 32): "added 1 work block(s)", then the work section's Add button
    was "covered" and its other blocks were left off (the page reached the form with 2 of 5 jobs). A cover that
    goes away on its own, a toast or a re-render, is waited on: the button is pressed again a little later. (The
    note here goes after 6 s; the one press that found it there gave up after 3.)"""
    run(srv.browser.goto(fixture_url("site/workday-my-experience.html") + "?toast"))
    education = run(srv.add_entries("education"))
    assert (education["clicks"], education["after"]) == (1, 1), education
    assert not education.get("add_button_covered") and "note" not in education, education


def test_workday_date_parts_read_back_as_typed_whatever_the_order(srv):
    """Workday's date boxes (issue #112, note 13, live test 2, Oct 2026): every job's month came out 02
    (its year right) and the self-identify "Date" 02/02/2006, on two employers' sites, and the desk said
    nothing. A date's digits go to the part the date counts as current, which goes back to its first part
    whenever focus leaves the date: the year typed into a date left between its parts went into the month
    ("2019": month 02, year 019). Each date's parts are typed in one run from its first part, and read back."""
    run(srv.browser.goto(fixture_url("site/workday-date-sections.html")))
    by = {(f.get("section") or f["label"], f.get("sublabel")): f["id"]
          for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    values = [(("From", "Month"), "10"), (("To", "Month"), "3"), (("Job Title", None), "Technician"),
              (("From", "Year"), "2019"), (("To", "Year"), "2021"),
              (("Date", "Month"), "10"), (("Date", "Day"), "9"), (("Name", None), "Sam Rivera"), (("Date", "Year"), "2026")]
    out = run(srv.fill_form([{"id": by[key], "value": value} for key, value in values]))
    assert all(r.get("ok") for r in out["results"]), out["results"]
    page = run(srv.browser.page())
    shown = run(page.evaluate("() => [...document.querySelectorAll('[role=group]')].map((g) => "
                              "[...g.querySelectorAll('[aria-hidden=true]')].map((d) => d.textContent).join('/'))"))
    assert shown == ["10/2019", "03/2021", "10/09/2026"], shown


def test_a_workday_year_that_reads_back_wrong_is_typed_once_more(srv):
    """HonorHealth's Workday (run 49, Oct 13; once in run 44 elsewhere): a school's year now and then lost the keys
    typed as it took focus, and read back 1900 ("typed 2016: 1900"); the desk said so and the job stopped. A date
    that doesn't read back as typed is typed once more, a little slower, before it's said."""
    run(srv.browser.goto(fixture_url("site/workday-date-sections.html") + "?drops"))
    year = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]
                if f.get("section") == "First Year Attended" or f["label"] == "First Year Attended")
    out = run(srv.fill_form([{"id": year["id"], "value": "2016"}]))
    assert out["results"][0]["ok"], out["results"]
    page = run(srv.browser.page())
    assert run(page.evaluate("() => document.getElementById('education-1--firstYearAttended-dateSectionYear-input')"
                             ".getAttribute('aria-valuenow')")) == "2016"


def test_a_dates_second_try_never_takes_a_part_the_first_left_wrong(srv):
    """The review of 0.3.136: a month typed with its year empty, on a date that reads a lone number as its year as
    focus leaves it (Intel's and U.S. Bank's), came back as the year 2010. A second try that took the date as it
    then read typed the month alone and kept that year, which the profile never gave. The second try types the same
    parts with the same digits, and a part it didn't type must read as it did before: here it's still said."""
    run(srv.browser.goto(fixture_url("site/workday-date-sections.html") + "?sticky&arrows"))
    by = {(f.get("section") or f["label"], f.get("sublabel")): f["id"]
          for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    out = run(srv.fill_form([{"id": by[("From", "Month")], "value": "10"}]))
    assert not out["results"][0].get("ok"), out["results"]
    assert "reads" in out["results"][0]["error"], out["results"]


def test_a_workday_date_whose_current_part_stays_put_gets_its_month(srv):
    """Intel's Workday (nightly live check, Oct 11): the date's current part stays where it was when focus
    leaves the date, and is marked so; with the year typed first, the month's digits went into the year and the
    month read MM ("Workday didn't take your profile's answer for ... (From)"). The part typed first is made
    current with a press of its display whenever the date marks another part current."""
    run(srv.browser.goto(fixture_url("site/workday-date-sections.html") + "?sticky"))
    by = {(f.get("section") or f["label"], f.get("sublabel")): f["id"]
          for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    values = [(("From", "Year"), "2021"), (("From", "Month"), "3"), (("To", "Year"), "2023"), (("To", "Month"), "11")]
    out = run(srv.fill_form([{"id": by[key], "value": value} for key, value in values]))
    assert all(r.get("ok") for r in out["results"]), out["results"]
    page = run(srv.browser.page())
    shown = run(page.evaluate("() => [...document.querySelectorAll('[role=group]')].map((g) => "
                              "[...g.querySelectorAll('[aria-hidden=true]')].map((d) => d.textContent).join('/'))"))
    assert shown[:2] == ["03/2021", "11/2023"], shown


@pytest.mark.parametrize("order", ["month first", "year first", "backwards"])
def test_a_workday_date_takes_each_part_in_the_one_with_focus(srv, order):
    """Intel's and Entegris' Workday, traced live (Oct 12): digits go to the part with focus, whatever part the date
    marks current, and its arrows only move focus, wrapping round. The desk's arrow presses (0.3.116) took focus
    from the month to the year, so the month went into the year ("2003"), and the year began in the month ("02");
    seventeen employers stopped on their work history's From. Each part is focused, and typed, with no arrows.
    And as focus leaves such a date it's read again, a lone number as the year: the month typed with the year still
    empty came back as the year 2003 (Intel's and U.S. Bank's, traced live Oct 12). A date's parts given together
    are typed in one run."""
    run(srv.browser.goto(fixture_url("site/workday-date-sections.html") + "?sticky&arrows"))
    by = {(f.get("section") or f["label"], f.get("sublabel")): f["id"]
          for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    values = [(("From", "Month"), "3"), (("From", "Year"), "2021"), (("To", "Month"), "11"), (("To", "Year"), "2023"),
              (("Date", "Month"), "10"), (("Date", "Day"), "9"), (("Date", "Year"), "2026")]
    if order == "year first":
        values = [values[1], values[0], values[3], values[2], values[6], values[4], values[5]]
    if order == "backwards":
        values = values[::-1]
    out = run(srv.fill_form([{"id": by[key], "value": value} for key, value in values]))
    assert all(r.get("ok") for r in out["results"]), out["results"]
    page = run(srv.browser.page())
    shown = run(page.evaluate("() => [...document.querySelectorAll('[role=group]')].map((g) => "
                              "[...g.querySelectorAll('[aria-hidden=true]')].map((d) => d.textContent).join('/'))"))
    assert shown == ["03/2021", "11/2023", "10/09/2026"], shown


def test_a_workday_dates_part_left_empty_isnt_checked_against_one_never_typed(srv):
    """The review of 0.3.121: a date's Month and Year given with its Day not, the Year wasn't typed with the Month
    (the empty Day between), yet the Month's fill said the date was wrong for the Year it hadn't typed."""
    run(srv.browser.goto(fixture_url("site/workday-date-sections.html") + "?sticky"))
    by = {(f.get("section") or f["label"], f.get("sublabel")): f["id"]
          for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    out = run(srv.fill_form([{"id": by[("Date", "Month")], "value": "10"}, {"id": by[("Date", "Year")], "value": "2026"}]))
    assert out["results"][0].get("ok"), out["results"]


def test_a_file_put_in_the_sites_open_upload_dialog_is_sent_with_its_upload(srv, tmp_path):
    """Amkor's SuccessFactors (the nightly of Oct 12): its "Upload Resume" dialog is open over the profile, its file
    box inside. The desk put the resume in the box, pressed Next, and the site said "Resume: field is required": the
    file counts only once the dialog's own Upload is pressed. It's pressed, and the dialog waited on to close."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    run(srv.browser.goto(fixture_url("site/successfactors-open-upload-dialog.html")))
    box = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"] if f["kind"] == "file")
    out = run(srv.fill_form([{"id": box["id"], "value": str(resume)}]))
    assert out["results"][0].get("ok"), out["results"]
    page = run(srv.browser.page())
    assert run(page.inner_text("#resume")) == "resume.pdf is on file.", run(page.inner_text("body"))


@pytest.mark.parametrize("page,said", [("phenom-resume-not-kept.html", "resume.pdf"), ("phenom-resume-not-kept.html?kept", None),
                                       ("upload-cover-letter.html", None)])
def test_a_resume_the_site_didnt_keep_is_told_from_any_other_upload(srv, tmp_path, page, said):
    """Issue #112, note 28 and its review: the resume's own file box, its hidden fields beside it all empty, is a
    resume the site didn't keep; a cover letter's box, beside "resume" fields of a save-for-later elsewhere, isn't."""
    doc = tmp_path / "resume.pdf"
    doc.write_bytes(b"%PDF-1.4 test")
    name, _, query = page.partition("?")
    run(srv.browser.goto(fixture_url(f"site/{name}") + (f"?{query}" if query else "")))
    box = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"] if f["kind"] == "file")
    run(srv.fill_form([{"id": box["id"], "value": str(doc)}]))
    assert run(srv.browser.upload_not_kept()) == said


def test_a_dialog_that_isnt_an_uploads_alone_is_never_pressed(srv, tmp_path):
    """The review of the open upload dialog's press: an application drawn as a dialog, its resume box beside a name
    box and a "Save" that sends it, would have been sent (around the guard every press goes through). Only a dialog
    holding nothing but its file box is pressed."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    run(srv.browser.goto(fixture_url("site/successfactors-open-upload-dialog.html") + "?form"))
    box = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"] if f["kind"] == "file")
    out = run(srv.fill_form([{"id": box["id"], "value": str(resume)}]))
    assert out["results"][0].get("ok"), out["results"]
    assert run(run(srv.browser.page()).evaluate("() => window.sent")) is False


def test_a_press_allowed_to_send_is_never_found_again_by_its_words(srv, tmp_path):
    """The safety audit (Oct 12): a press the desk makes with allow_submit (a Create Account form's own button) whose
    button was redrawn was found again by its words, anywhere on the page, and pressed with no check: another form's
    "Submit". It's refused instead; the desk stops there."""
    page_file = tmp_path / "redrawn.html"
    page_file.write_text('<!doctype html><form id="acct" onsubmit="window.sent.push(\'acct\'); return false;">'
                         '<label for="e">Email</label><input id="e" type="email"><button type="submit">Submit</button></form>'
                         '<form id="app" onsubmit="window.sent.push(\'app\'); return false;"><label for="n">Name</label>'
                         '<input id="n"></form><script>window.sent = [];</script>', encoding="utf-8")
    run(srv.browser.goto(page_file.as_uri()))
    button = next(a for a in run(srv.inspect_form(include_dropdown_options=False))["actions"] if a["text"] == "Submit")
    page = run(srv.browser.page())
    # the account's form redrawn without its button, and the application's own Submit drawn
    run(page.evaluate("""() => { document.querySelector('#acct button').remove();
      const b = document.createElement('button'); b.type = 'submit'; b.textContent = 'Submit';
      document.getElementById('app').append(b); }"""))
    with pytest.raises(KeyError):
        run(srv.browser.click(button["id"], allow_submit=True))
    assert run(page.evaluate("() => window.sent")) == []


@pytest.mark.parametrize("how", ["dialog", "box", "chooser"])
def test_a_resume_uploaded_through_the_sites_own_dialog(srv, tmp_path, how):
    """APS's SuccessFactors (the live check's plus probe, Oct 11): its resume is a "+" button that opens a dialog
    ("Upload a Resume Opens a dialog"), with no file box on the page, so the desk never saw it and the site said
    "Resume is required". It's read as the resume's box, and the file goes in through what the button opens: the
    browser's file chooser, or the dialog's own file box (after its Browse), then the dialog's Upload."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    query = "" if how == "dialog" else f"?{how}"
    run(srv.browser.goto(fixture_url("site/successfactors-upload-dialog.html") + query))
    box = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"] if f["label"] == "Resume")
    assert (box["label"], box["required"], box["value"], box.get("opens_dialog")) == ("Resume", True, "", True), box
    if how == "dialog":  # as the desk fills it: the profile's resume (the tests' profile has one)
        result = run(srv.autofill())
        assert not result["failed"], result["failed"]
        name = Path(config.Profile.load().get("documents.resume")).name
    else:
        out = run(srv.fill_form([{"id": box["id"], "value": str(resume)}]))
        assert out["results"][0].get("ok"), out["results"]
        name = "resume.pdf"
    again = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"] if f["label"] == "Resume")
    assert again["value"] == name, again


@pytest.mark.parametrize("how", ["dialog", "box"])
def test_an_upload_through_a_dialog_touches_only_what_it_opened(srv, tmp_path, how):
    """With a cookie banner on show, another upload's file box on the page and the site's "Browse jobs" link: the
    resume goes through the dialog its button opened, and nothing else is pressed or given the file (a cookie
    banner is never accepted for the person)."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    run(srv.browser.goto(fixture_url("site/successfactors-upload-dialog.html") + f"?others{'&box' if how == 'box' else ''}"))
    box = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"] if f["label"] == "Resume")
    out = run(srv.fill_form([{"id": box["id"], "value": str(resume)}]))
    assert out["results"][0].get("ok"), out["results"]
    page = run(srv.browser.page())
    assert run(page.evaluate("() => [!!window.cookieAccepted, document.getElementById('cover').files.length, "
                             "location.hash]")) == [False, 0, ""]


def test_each_jobs_dates_go_in_its_own_block(srv):
    """A Workday site's dates are each a fieldset of their own, with each part's "MM" or "YYYY"
    drawn over its box: the desk filled every job's title, company and description and left all
    its From and To boxes empty, asked bare with no job named (live, Oct 2026)."""
    run(srv.browser.goto(fixture_url("site/workday-experience-blocks.html")))
    run(srv.add_entries("work"))
    fields = run(srv.inspect_form(include_dropdown_options=False))["fields"]
    dates = [f for f in fields if f.get("role") == "spinbutton"]
    assert [f.get("section") for f in dates] == ["Work Experience 1"] * 4 + ["Work Experience 2"] * 4, dates

    result = run(srv.autofill())
    assert not result["failed"], result["failed"]
    assert not [f for f in result["needs_input"] if f.get("section", "").startswith("Work")], result["needs_input"]
    got = {(f.get("section"), f["label"].rstrip("*"), f.get("sublabel")): f["value"]
           for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    assert (got[("Work Experience 1", "From", "Month")], got[("Work Experience 1", "From", "Year")]) == ("03", "2021")
    assert ("Work Experience 1", "To", "Month") not in got  # it's the job held now
    assert [got[("Work Experience 2", d, p)] for d in ("From", "To") for p in ("Month", "Year")] == \
        ["06", "2018", "02", "2021"]


def test_blocks_drawn_after_their_heading_are_read_and_filled(srv, job_apply_home):
    """A Workday site draws a new Education block's heading at once and its boxes a moment later:
    the desk added two blocks and read the page before their boxes were there, so the second was
    never filled (live, Oct 2026). Its Degree list has nothing for classes without a degree, so
    that box stays the person's: never High School, GED or Associates for coursework."""
    import yaml

    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text())
    school = profile["education_history"][0]["school"]
    profile["education_history"].append({"school": school, "degree": "", "major": "Physics", "start": 2021,
                                         "end": 2022})
    path.write_text(yaml.safe_dump(profile))
    run(srv.browser.goto(fixture_url("site/workday-experience-blocks.html")))
    assert run(srv.add_entries("education"))["after"] == 2

    result = run(srv.autofill())
    assert not result["failed"], result["failed"]
    got = {(f.get("section"), f["label"].rstrip("*"), f.get("sublabel")): f["value"]
           for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    assert got[("Education 1", "Degree", None)] == "Bachelors"
    boxes = (("School or University", None), ("Field of Study", None), ("From", "Year"),
             ("To (Actual or Expected)", "Year"))
    assert [got[("Education 2", label, sub)] for label, sub in boxes] == [school, "Physics", "2021", "2022"]
    assert got[("Education 2", "Degree", None)] == "Select One"
    degree = next(f for f in result["needs_input"] if f["label"] == "Degree*")
    assert degree["section"] == "Education 2" and "Associates" in degree["options"], degree


def test_final_apply_button_honeypot_and_enter(srv, monkeypatch):
    job = srv.add_job(url=fixture_url("apply_button_form.html"), title="Tech", company="Example Fab")["job"]
    run(srv.open_application(job_id=job["id"]))
    form = run(srv.inspect_form())
    assert not any("blank" in f["label"].lower() for f in form["fields"])  # bot trap never reported
    actions = {a["text"]: a for a in form["actions"]}
    assert actions["Apply"].get("is_submit") and actions["Apply"].get("form_submit")
    assert actions["Search"].get("form_submit") and not actions["Search"].get("is_submit")

    deg = next(f for f in form["fields"] if f["label"] == "Degree")
    assert deg["kind"] == "combobox" and "Bachelor's Degree" in deg["options"]  # read through the click-blocking layer

    result = run(srv.autofill())
    assert not result["failed"], result["failed"]
    assert {f["label"] for f in result["filled"]} >= {"First Name", "Last Name", "Email", "City", "Degree"}
    assert "Thank you" not in run(srv.page_text())  # typing into the in-form combobox pressed no Enter
    # the export-control question offers no plain yes/no, so it's left for the person
    assert any(n["label"].startswith("Are you a U.S. person") for n in result["needs_input"])
    after = {f["label"]: f["value"] for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    assert after["Degree"] == "Bachelor's Degree"
    assert after["Gender"] == "Decline to self-identify"
    # menus stay open on this page, so when inspect opened them one after another each field
    # still had to list only its own choices
    menus = {f["label"]: f.get("options") for f in form["fields"] if f["kind"] == "combobox"}
    assert menus["Gender"] == ["Male", "Female", "Decline to self-identify"]
    assert "Male" not in menus["Degree"] and "Other" in menus["Are you a U.S. person as defined by export control regulations (EAR)?"]

    blocked = run(srv.click("Apply"))  # a form's own "Apply" button is the final submit
    assert blocked["clicked"] is False

    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    assert run(srv.click("Search"))["clicked"]  # step-like form buttons still work in a dry run
    assert "searched" in run(srv.page_text())
    assert run(srv.click("Send it"))["clicked"] is False  # any other form submit is refused in a dry run
    monkeypatch.delenv("JOB_APPLY_NEVER_SUBMIT")
    assert run(srv.click("Send it"))["clicked"]  # outside a dry run it's an ordinary button

    done = run(srv.submit_application(job_id=job["id"], user_confirmed=True))
    assert done["submitted"] and done["confirmed"]


def test_a_choice_that_opens_a_list_of_its_own_is_picked_inside_it(srv):
    """Workday's "How Did You Hear About Us?" (live test 2, issue #112, note 15): every choice opens a
    second list, here with one choice of the same name ("<employer> website" > "<employer> website"). The
    desk picked the first level and stopped ("Picked ... but the field didn't take it"). Inside the list
    it opens, the choice of the same name (or the only one) is picked."""
    run(srv.browser.goto(fixture_url("site/workday-two-level-list.html")))
    field = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]
                 if f["label"].startswith("How Did You Hear"))
    out = run(srv.fill_form([{"id": field["id"], "value": "Example Fab website"}]))
    assert out["results"][0].get("ok"), out["results"]
    page = run(srv.browser.page())
    assert run(page.evaluate("() => document.getElementById('source').textContent")) == "Example Fab website"


def test_inside_a_list_a_choice_opens_only_the_answer_or_its_namesake_is_picked():
    """The list a choice opens: the answer, or the entry named like the choice ("<employer> website ›" opens
    "<employer> website"); never a lone entry that's something else ("Job Board ›" opening only "Indeed" isn't
    the profile's "Job Board")."""
    from job_apply.browser import _inner_choice

    assert _inner_choice("Company Website", "Example Fab website \u203a", ["Example Fab website"]) == "Example Fab website"
    assert _inner_choice("Job Board", "Job Board / Social Network \u203a", ["Indeed"]) is None
    assert _inner_choice("LinkedIn", "Job Board / Social Network \u203a", ["Indeed", "LinkedIn"]) == "LinkedIn"


def test_a_confirmation_drawn_a_few_seconds_after_submit_is_seen(srv):
    """Workday (live test 2, issue #112, note 19): both applications the person had the desk submit went
    in, and Workday showed "Application Submitted / Congratulations! Thank you for submitting your
    application!" in a dialog over its candidate home a few seconds later. The desk had already looked,
    and said "no confirmation showed". It watches for one a while longer."""
    job = srv.add_job(url=fixture_url("site/workday-review.html"), title="FSE", company="Example Fab")["job"]
    run(srv.open_application(job_id=job["id"]))
    done = run(srv.submit_application(job_id=job["id"], user_confirmed=True))
    assert done["submitted"] and done["confirmed"], done
    assert srv.get_job(job["id"])["job"]["status"] == "applied"


def test_click_finds_a_replaced_button_and_checks_it_again(srv, monkeypatch):
    """Oracle's pages swap a button out between reading it and clicking it."""
    import job_apply.browser as browser_module

    run(srv.open_application(url=fixture_url("apply_button_form.html")))
    page = run(srv.browser.page())
    run(page.evaluate("() => { const a = document.createElement('a'); a.href = '#top'; a.textContent = 'Apply'; "
                      "document.body.prepend(a); }"))
    form = run(srv.inspect_form(include_dropdown_options=False))
    search = next(a for a in form["actions"] if a["text"] == "Search")
    link = next(a for a in form["actions"] if a["text"] == "Apply" and not a.get("form_submit"))
    # The first look at an element replaces it: with a copy (Search), or with nothing (the
    # link), so finding it again by its text lands on the form's own final Apply button.
    monkeypatch.setattr(browser_module, "ELEMENT_INFO_JS", (
        "(el) => { const info = (" + browser_module.ELEMENT_INFO_JS.strip() + ")(el);"
        " if (!window.swapped) { window.swapped = true;"
        "   if (el.tagName === 'A') el.remove();"
        "   else { const copy = el.cloneNode(true); copy.removeAttribute('data-ja-id'); el.replaceWith(copy); } }"
        " return info; }"))
    assert run(srv.click(search["id"]))["clicked"]
    assert "searched" in run(srv.page_text())
    run(page.evaluate("() => { window.swapped = false; }"))
    assert run(srv.click(link["id"]))["clicked"] is False
    assert "Thank you" not in run(srv.page_text())


def test_click_is_not_repeated_on_a_page_that_moved_on(srv, monkeypatch):
    """When a click times out because the page already went to the next step, the
    same-named button there must not be clicked as well."""
    import job_apply.browser as browser_module

    monkeypatch.setattr(browser_module, "CLICK_TIMEOUT", 1500)
    run(srv.open_application(url=fixture_url("apply_button_form.html")))
    page = run(srv.browser.page())
    run(page.evaluate("""() => {
      window.clicks = 0;
      const next = document.createElement('button');
      next.type = 'button';
      next.textContent = 'Continue';
      next.addEventListener('mouseover', () => {
        history.pushState({}, '', '#step-2');  // the next step, with its own Continue
        const again = next.cloneNode(true);
        again.removeAttribute('data-ja-id');
        again.addEventListener('click', () => { window.clicks += 1; });
        next.replaceWith(again);
      }, {once: true});
      document.body.prepend(next);
    }"""))
    form = run(srv.inspect_form(include_dropdown_options=False))
    cont = next(a for a in form["actions"] if a["text"] == "Continue")
    out = run(srv.click(cont["id"]))
    assert out["clicked"] and out["url"].endswith("#step-2") and "moved on" in out["note"]
    assert run(page.evaluate("() => window.clicks")) == 0


def test_inspect_waits_for_a_form_drawn_late(srv, monkeypatch):
    """Oracle's email step draws its form a moment after the page itself (with the desk's own wait: the tests'
    is shorter)."""
    monkeypatch.setattr(browser_module, "FORM_DRAW_WAIT", 500)
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.evaluate("""() => setTimeout(() => {
      const f = document.createElement('form');
      f.innerHTML = '<label for=em>Email Address</label><input id=em type=email required>';
      document.body.append(f);
    }, 1200)"""))
    fields = run(srv.inspect_form(include_dropdown_options=False))["fields"]
    assert [f["label"] for f in fields] == ["Email Address"]


def test_errors_are_read_without_icon_glyphs(srv):
    """Amkor's sign-in shows its error after an icon-font glyph, and an alert that's only a glyph."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
      '<div role="alert">\ue0b1 Invalid email address or password.</div><div role="alert">\ue1f7</div>')"""))
    assert run(srv.browser.inspect(False))["errors"] == ["Invalid email address or password."]


@pytest.mark.parametrize("mode", ["", "?slow"])
def test_oracle_address_lookups_are_picked_from_their_grid_in_the_right_state(srv, mode):
    """Texas Instruments' and onsemi's Oracle address boxes are lookups whose entries are a
    grid's cells, and live, every one failed ("nothing in its list matched"). The first
    entries for a city or a street can be in another state ("Chandler, Henderson, TX"), and a
    ZIP can cover two places: the pick is the one that matches the rest of the address.

    ?slow: onsemi's City, State and County, drawn afresh from the ZIP picked, are gone for a
    moment first; live, State was then asked as a question though the site had filled it."""
    run(srv.open_application(url=fixture_url("site/oracle-address.html") + mode))
    result = run(srv.autofill())
    assert not result["failed"], result["failed"]
    page = run(srv.browser.page())
    assert run(page.input_value("#postalCode4")) == ""  # the ZIP's extension, not the ZIP (onsemi, live)
    assert run(page.evaluate("() => window.picked")) == {
        "addressLine1": "100 W MAIN ST, CHANDLER, ARIZONA, 85225",
        "postalCode": "85225, Chandler, Maricopa, AZ",  # not Chandler Heights
        "city": "Chandler, Maricopa, AZ",  # not Chandler, Henderson, TX
        "region2": "Arizona",
        "region1": "Maricopa",  # filled in by the site from the ZIP: the profile has no county
    }


def test_sign_in_and_create_account_buttons_say_where_they_are(srv):
    """Workday's sign-in pop-up open over its Create Account form: the header's "Sign In" comes
    before the pop-up's password box, the pop-up's own buttons after it, and the form behind it
    (whose boxes aren't read) has its own "Create Account", which creates the account."""
    run(srv.open_application(url=fixture_url("site/signin-header-popup.html") + "?start=popup"))
    form = run(srv.browser.inspect(False))
    assert [f["label"] for f in form["fields"]] == ["Email Address", "Password"]  # the pop-up's
    flags = [(a["text"], bool(a.get("after_password")), bool(a.get("account_form"))) for a in form["actions"]]
    assert flags[0] == ("Sign In", False, False)  # the header's
    assert ("Create Account", False, True) in flags  # the form's own button (a div), behind the pop-up
    assert flags[-4:] == [("Sign In", True, False), ("Create Account", True, False),
                          ("Forgot your password?", True, False), ("Close", True, False)]  # the pop-up's


def test_a_job_board_that_is_down_says_so(srv):
    """Daifuku's iCIMS board, live (Oct 2026), answered HTTP 521 (Cloudflare: the site's own
    server down). Read as a page, that was a board with no openings."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from job_apply.browser import SiteDown

    class Down(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html><head><title>Error : careers-example.icims.com</title></head><body>Web server is down</body></html>"
            if self.path.startswith("/challenge"):  # Cloudflare's check: a 503 that clears by itself
                body = b"<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
                self.send_response(503)
            else:
                self.send_response(521)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    site = ThreadingHTTPServer(("127.0.0.1", 0), Down)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{site.server_address[1]}/jobs/search?ss=1"
        with pytest.raises(SiteDown, match=r"127\.0\.0\.1 is down right now \(HTTP 521\)"):
            run(srv.browser.frames_html(url))
        with pytest.raises(SiteDown):
            run(srv.browser.listing_pages(url, "tr.jobResultItem"))
        # a bot check isn't the site being down: the page is read (and the check waited out)
        pages = run(srv.browser.frames_html(f"http://127.0.0.1:{site.server_address[1]}/challenge"))
        assert "Checking your browser" in pages[0]
    finally:
        site.shutdown()


def test_search_text_left_in_a_picker_is_no_evidence(srv):
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.evaluate("() => { const form = document.createElement('form'); "
                      "form.innerHTML = '<div><input id=eth value=C></div><input id=other>'; document.body.prepend(form); }"))
    loc = page.locator("#eth")
    run(srv.browser._confirm_choice(page, loc, "Choose not to disclose"))  # "C" is what was typed, not a choice
    run(page.evaluate("() => { document.getElementById('eth').value = 'Asian'; }"))
    with pytest.raises(ValueError):
        run(srv.browser._confirm_choice(page, loc, "Choose not to disclose"))


def test_dry_run_keeps_later_statuses(srv, monkeypatch):
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    job = srv.add_job(url=fixture_url("generic_form.html"), title="FSE", company="Example Litho")["job"]
    srv.update_job(job["id"], status="interviewing")
    run(srv.open_application(job_id=job["id"]))
    run(srv.submit_application(job_id=job["id"], user_confirmed=True))
    assert srv.get_job(job["id"])["job"]["status"] == "interviewing"


def test_ingest_reports_navigation_failure(srv):
    run(srv.open_application(url=fixture_url("generic_form.html")))
    out = run(srv.ingest_job("http://127.0.0.1:9/no-such-posting", use_browser=True))
    assert out["saved"] is False and "couldn't open" in out["error"]
    assert srv.list_jobs()["count"] == 0  # nothing saved from the page that was already open


def test_capture_json_takes_the_keyword_search_and_enlarges_it(srv):
    """ASML's page calls its search API twice (facets, then the keyword search); take the
    keyword one, with its page size raised before it leaves the browser."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from job_apply.search import sitecore_rewrite, sitecore_wants

    page = b"""<!doctype html><title>Find your job</title><script>
      const send = search => fetch('/discover/v2/1', {method: 'POST', headers: {'content-type': 'application/json'},
                                                      body: JSON.stringify({widget: {items: [{rfk_id: 'jobs', search}]}})});
      const keyphrase = new URLSearchParams(location.search).get('query');
      send({limit: 25, facet: {all: true}}).then(() => send({limit: 25, offset: 50, query: {keyphrase}}));
    </script>"""

    class Site(BaseHTTPRequestHandler):
        def reply(self, kind, body):
            self.send_response(200)
            self.send_header("content-type", kind)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply("text/html", page)

        def do_POST(self):
            received = json.loads(self.rfile.read(int(self.headers["content-length"])))
            self.reply("application/json", json.dumps({"received": received}).encode())

        def log_message(self, *args):
            pass

    site = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{site.server_port}/careers?query=field%20service"
        got = run(srv.browser.capture_json(url, "/discover/v2/", want=sitecore_wants, rewrite=sitecore_rewrite))
    finally:
        site.shutdown()
    search = got["received"]["widget"]["items"][0]["search"]
    assert search == {"limit": 100, "offset": 0, "query": {"keyphrase": "field service"}}
    assert run(srv.browser.page()).url == "about:blank"  # the search tab didn't take over


def test_a_workday_cover_is_the_submit_found_not_its_hidden_button(srv):
    """Workday draws a form's own button hidden (aria-hidden) under a div of its own that takes the
    click (data-automation-id="click_filter"). Over a final Submit, the cover is the button found to
    submit with, and reads as that button; the hidden one, which does nothing, isn't."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content("""
      <form><label for="n">Name</label><input id="n">
        <div style="position: relative; width: 200px; height: 40px">
          <div role="button" tabindex="0" data-automation-id="click_filter" style="position: absolute; inset: 0"></div>
          <button type="submit" aria-hidden="true" tabindex="-2" style="width: 100%; height: 100%">Submit</button>
        </div></form>"""))
    found = run(srv.browser.find_submit())
    assert [(a["text"], bool(a.get("covered"))) for a in found] == [("Submit", False)], found
    info = run(srv.browser.element_info(found[0]["id"]))
    assert info is not None and info["label"] == "Submit", info


def test_a_resume_goes_into_a_page_once_though_its_box_empties(srv, job_apply_home):
    """Nikon's UKG Pro (live run 13, Oct 2026) empties its upload box once it has the file, so each pass read it
    as never uploaded and uploaded the resume again; each upload set its resume reading going, which emptied
    Address 1 and Zip after they were filled. A file goes into a page once; a reloaded page gets it again."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    resume = job_apply_home / "resume-once.pdf"
    resume.write_bytes(b"%PDF-1.4 test")

    async def upload_page():
        await page.set_content("""<label for="cv">Upload Resume</label><input id="cv" type="file">
          <script>window.uploads = 0; document.getElementById('cv').addEventListener('change', (e) => {
            window.uploads += 1; e.target.value = ''; });</script>""")

    run(upload_page())
    for _ in range(2):
        box = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"] if f["kind"] == "file")
        assert run(srv.fill_form([{"id": box["id"], "value": str(resume)}]))["results"][0]["ok"]
    assert run(page.evaluate("() => window.uploads")) == 1
    # its box drawn afresh after the upload (UKG's, live run 15), with the file named on the page: not uploaded again
    run(page.goto(fixture_url("jsonld_posting.html")))
    run(page.set_content("""<label for="cv">Upload Resume</label><div id="w"><input id="cv" type="file"></div><p id="n"></p>
      <script>window.uploads = 0; function arm() { document.getElementById('cv').addEventListener('change', (e) => {
        window.uploads += 1; document.getElementById('n').textContent = e.target.files[0].name
          + ' has been used to pre-fill part of your application.';
        document.getElementById('w').innerHTML = '<input id="cv" type="file">'; arm(); }); } arm();</script>"""))
    for _ in range(2):
        box = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"] if f["kind"] == "file")
        out = run(srv.fill_form([{"id": box["id"], "value": str(resume)}]))["results"][0]
        assert out["ok"], out
    assert run(page.evaluate("() => window.uploads")) == 1
    run(page.goto(fixture_url("jsonld_posting.html")))  # a page loaded afresh: uploaded again
    run(upload_page())
    box = next(f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"] if f["kind"] == "file")
    run(srv.fill_form([{"id": box["id"], "value": str(resume)}]))
    assert run(page.evaluate("() => window.uploads")) == 1  # (its own count: one upload on this page)


def test_a_maps_own_controls_are_not_the_pages_buttons(srv):
    """Benchmark's Infor posting (live run 10, Oct 11) holds a Google map whose controls ("Keyboard shortcuts",
    "Map camera controls", "Show satellite imagery") are drawn afresh all the while, so the page never looked
    the same twice, and the desk didn't see its Apply go nowhere. A map's own controls aren't read as the
    page's buttons."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content("""<h1>Field Service Technician</h1><button type="button">Apply</button>
      <div class="gm-style"><button type="button">Keyboard shortcuts</button>
        <button type="button">Map camera controls</button><button type="button">Show satellite imagery</button></div>"""))
    texts = [a["text"] for a in run(srv.inspect_form(include_dropdown_options=False))["actions"]]
    assert texts == ["Apply"], texts


@pytest.mark.parametrize("overlap", [0, 1, 3, 8])
def test_a_menu_left_over_the_next_field_isnt_its_options(srv, overlap):
    """Micron: the availability question's menu stayed open over the next question and was
    drawn afresh when focus moved, so its options were read as the next question's. Also where
    the menu is drawn a little over its own box (a joined border, a rounding, or more: the review
    of 0.3.134's), which never makes it the next question's."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content("""
      <style>.f { margin-bottom: 8px } label { display: block } input { width: 300px }
             [role=listbox] { position: absolute; background: #fff; border: 1px solid #ccc; margin: 0; padding: 0;
                              width: 300px; list-style: none; z-index: 10 } [role=option] { padding: 6px }</style>
      <form>
        <div class="f"><label for="avail">When would you be available if an offer was accepted?</label>
          <input id="avail" role="combobox" autocomplete="off"></div>
        <div class="f"><label for="friends">Do you have any friends/relatives presently employed by Micron?</label>
          <input id="friends" role="combobox" autocomplete="off"></div>
      </form>
      <script>
        function menu(input, options) {
          const ul = document.createElement('ul');
          ul.setAttribute('role', 'listbox');
          const r = input.getBoundingClientRect();
          ul.style.left = (r.left + scrollX) + 'px';
          ul.style.top = (r.bottom + scrollY - OVERLAP) + 'px';
          for (const t of options) {
            const li = document.createElement('li');
            li.setAttribute('role', 'option');
            li.textContent = t;
            li.onclick = () => { input.value = t; ul.remove(); };
            ul.append(li);
          }
          document.body.append(ul);
          return ul;
        }
        const AVAIL = ['Immediately after offer acceptance', 'Within 1 month after offer acceptance',
                       'Within 2 months after offer acceptance'];
        const OVERLAP = %d;
        let availMenu = null;
        avail.addEventListener('click', () => { if (!availMenu) availMenu = menu(avail, AVAIL); });
        // ignores Escape and blur, and draws itself again when focus moves on
        friends.addEventListener('focus', () => { if (availMenu) { availMenu.remove(); availMenu = menu(avail, AVAIL); } });
        const later = () => setTimeout(() => menu(friends, ['Yes', 'No']), 400);
        friends.addEventListener('click', later);
        friends.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown') later(); });
      </script>""" % overlap))
    fields = run(srv.inspect_form(include_dropdown_options=True))["fields"]
    assert by_label(fields, "available")["options"][0] == "Immediately after offer acceptance"
    assert by_label(fields, "friends")["options"] == ["Yes", "No"]


def test_a_pick_only_dropdown_is_asked_about_and_filled(srv):
    """Infineon: a required read-only dropdown ("Preferred location") was neither filled
    nor asked about, so the application stalled with nothing to answer."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content("""
      <form>
        <label for="loc">Preferred location for Engineer / Senior Engineer Product Engineering *</label>
        <input id="loc" role="combobox" readonly required aria-controls="loc-list" aria-expanded="false">
        <ul id="loc-list" role="listbox" hidden>
          <li role="option">Singapore</li><li role="option">Kulim</li><li role="option">Melaka</li>
        </ul>
      </form>
      <script>
        const box = document.getElementById('loc'), list = document.getElementById('loc-list');
        const open = () => { list.hidden = false; box.setAttribute('aria-expanded', 'true'); };
        box.addEventListener('click', open);
        box.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown') open(); });
        list.addEventListener('click', (e) => { box.value = e.target.textContent; list.hidden = true; });
      </script>"""))
    asked = run(srv.autofill())["needs_input"]
    assert [(f["label"], f["options"]) for f in asked] == [
        ("Preferred location for Engineer / Senior Engineer Product Engineering *", ["Singapore", "Kulim", "Melaka"])]
    out = run(srv.fill_form([{"id": asked[0]["id"], "value": "Kulim"}]))
    assert out["ok"], out
    assert run(page.input_value("#loc")) == "Kulim"


def test_reading_a_form_leaves_answered_dropdowns_alone(srv):
    """Micron: reading every dropdown's options opened answered ones too, and the Escape
    that closes the menu cleared the answer, so the same question came back each round."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content("""
      <form>
        <label for="rel">Do you have any friends/relatives presently employed by Micron? *</label>
        <input id="rel" role="combobox" required aria-controls="rel-list" autocomplete="off">
        <ul id="rel-list" role="listbox" hidden><li role="option">Yes</li><li role="option">No</li></ul>
      </form>
      <script>
        const box = document.getElementById('rel'), list = document.getElementById('rel-list');
        box.addEventListener('click', () => { list.hidden = false; });
        box.addEventListener('keydown', (e) => {
          if (e.key === 'ArrowDown') list.hidden = false;
          if (e.key === 'Escape') { box.value = ''; list.hidden = true; }  // like Micron's: Escape clears it
        });
        list.addEventListener('click', (e) => { box.value = e.target.textContent; list.hidden = true; });
      </script>"""))
    asked = run(srv.autofill())["needs_input"]
    assert [f["options"] for f in asked] == [["Yes", "No"]]
    assert run(srv.fill_form([{"id": asked[0]["id"], "value": "No"}]))["ok"]
    assert run(srv.autofill())["needs_input"] == []
    assert run(page.input_value("#rel")) == "No"  # read again without being opened, so still answered


def test_a_short_dropdown_is_picked_without_typing(srv):
    """Eightfold's Yes/No dropdowns need no typing, and on Micron typed keys ended up in
    another question ("ona", the end of "Arizona"). A visible answer is clicked instead."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content("""
      <form>
        <label for="q">Have you applied on any previous occasions for employment with Micron? *</label>
        <input id="q" role="combobox" required aria-controls="q-list" autocomplete="off">
        <ul id="q-list" role="listbox" hidden><li role="option">Yes</li><li role="option">No</li></ul>
      </form>
      <script>
        const box = document.getElementById('q'), list = document.getElementById('q-list');
        box.addEventListener('click', () => { list.hidden = false; });
        box.addEventListener('input', () => { box.dataset.typed = box.value; });
        list.addEventListener('click', (e) => { box.value = e.target.textContent; box.dataset.picked = e.target.textContent; list.hidden = true; });
      </script>"""))
    field = run(srv.inspect_form(include_dropdown_options=False))["fields"][0]
    assert run(srv.fill_form([{"id": field["id"], "value": "No"}]))["ok"]
    assert run(page.evaluate("() => [document.getElementById('q').dataset.picked, document.getElementById('q').dataset.typed]")) \
        == ["No", None]


def test_an_open_menus_highlighted_option_isnt_the_answer(srv):
    """Pills count as a field's answer, but the selected-looking option in an open menu doesn't."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content("""
      <form>
        <div class="field"><label for="lang">Language of Application *</label>
          <div class="wrap"><input id="lang" role="combobox" required readonly>
            <ul role="listbox"><li role="option" class="selected-option"><span class="item-label">German</span></li>
              <li role="option"><span class="item-label">English</span></li></ul></div></div>
        <div class="field"><label for="loc">Preferred location *</label>
          <div class="pills"><div class="tag-pill"><span class="pill-label">Singapore</span><button type="button">Remove Singapore</button></div></div>
          <div class="wrap"><input id="loc" role="combobox" required readonly></div></div>
      </form>"""))
    fields = {f["label"]: f for f in run(srv.inspect_form(include_dropdown_options=False))["fields"]}
    assert fields["Language of Application *"]["value"] == ""
    assert fields["Preferred location *"]["value"] == "Singapore"


EIGHTFOLD_COUNTRY_CODE = """
  <form>
    <label for="cc">Country code *</label>
    <div class="wrap"><input id="cc" role="combobox" required aria-controls="cc-list" autocomplete="off"></div>
    <ul id="cc-list" role="listbox" hidden></ul>
    <button type="submit">Submit application</button>
  </form>
  <script>
    const NAMES = ['Afghanistan', 'Albania', 'Algeria', 'Andorra', 'Angola', 'Argentina', 'Armenia', 'Australia',
                   'Austria', 'Bahamas', 'Belgium', 'Brazil', 'Canada', 'France', 'Germany', 'Mexico'];
    const ALL = NAMES.map((n, i) => ({flag: '🏳', code: i + 20, name: n})).concat([
      {flag: '🇺🇸', code: 1, name: 'United States of America'}, {flag: '🇺🇲', code: 1, name: 'United States Minor Outlying Islands'}]);
    const shown = (o) => `${o.flag} (+${o.code}) ${o.name}`;
    const box = document.getElementById('cc'), list = document.getElementById('cc-list');
    let chosen = '';
    function show(q) {  // the search matches the country's name, not the flag and code it shows
      list.innerHTML = '';
      for (const o of ALL.filter((o) => o.name.toLowerCase().includes(q.toLowerCase()))) {
        const li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.textContent = shown(o);
        list.append(li);
      }
      list.hidden = false;
    }
    const close = () => { list.hidden = true; box.value = chosen; };
    box.addEventListener('click', () => { box.value = ''; show(''); });  // the box clears for searching while open
    box.addEventListener('input', () => show(box.value));
    list.addEventListener('click', (e) => { chosen = e.target.textContent; close(); });
    box.addEventListener('blur', () => { if (list.hidden) box.value = chosen; });  // typed text that picked nothing is dropped
    // ignores Escape and focus leaving; closes on a click beside it
    document.addEventListener('mousedown', (e) => { if (!list.contains(e.target) && e.target !== box) close(); });
  </script>"""


def test_a_country_code_picker_like_eightfolds(srv):
    """Eightfold's Country code stayed empty on every live run, its menu left open over the
    Submit button: "United States of America (+1)" typed whole matched nothing in a list
    showing "🇺🇸 (+1) United States of America", the miss counted as filled, and the menu
    ignored Escape and focus leaving."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content(EIGHTFOLD_COUNTRY_CODE))
    filled = run(srv.autofill())["filled"]
    assert [f["value"] for f in filled if f["label"].startswith("Country code")] == ["🇺🇸 (+1) United States of America"]
    assert run(page.input_value("#cc")) == "🇺🇸 (+1) United States of America"
    assert run(page.evaluate("() => document.getElementById('cc-list').hidden"))  # not left open over the button


def test_a_pick_list_with_no_match_is_reported(srv):
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content(EIGHTFOLD_COUNTRY_CODE))
    field = run(srv.inspect_form(include_dropdown_options=False))["fields"][0]
    out = run(srv.fill_form([{"id": field["id"], "value": "Atlantis (+999)"}]))
    assert not out["ok"] and "nothing in its list matched 'Atlantis'" in out["results"][0]["error"]


def test_a_picker_is_searched_by_name_not_by_flag_and_code(srv):
    """When the entry only shows up once searched for, the search has to be words the list
    matches: the country's name, not "🇺🇸 (+1) ..." or "... (+1)"."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content(EIGHTFOLD_COUNTRY_CODE.replace(
        "box.addEventListener('click', () => { box.value = ''; show(''); });",
        "box.addEventListener('click', () => { box.value = ''; show(''); [...list.children].slice(8).forEach((li) => li.remove()); });")))
    field = run(srv.inspect_form(include_dropdown_options=False))["fields"][0]
    for value in ("United States of America (+1)", "🇺🇸 (+1) United States of America"):
        out = run(srv.fill_form([{"id": field["id"], "value": value}]))
        assert out["ok"], out
        assert run(page.input_value("#cc")) == "🇺🇸 (+1) United States of America"


def test_a_long_open_menu_doesnt_hide_the_submit_button(srv):
    """Infineon: an open list of referral sources, drawn as buttons, filled the 60 actions
    read from the page before "Submit application" was reached, so the desk couldn't find
    it. Menu entries aren't page actions, and a submit button always makes the list."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    sources = "".join(f'<button type="button" role="option">Source {i}</button>' for i in range(80))
    clears = "".join(f'<button type="button">Clear field {i}</button>' for i in range(70))
    run(page.set_content(f"""<form>
      <label for="h">How did you hear about us?</label><input id="h" role="combobox" aria-controls="h-list">
      <div id="h-list" role="listbox">{sources}</div>
      {clears}
      <button type="submit">Submit application</button></form>"""))
    actions = run(srv.inspect_form(include_dropdown_options=False))["actions"]
    assert not any(a["text"].startswith("Source") for a in actions)
    assert [a["text"] for a in run(srv.browser.find_submit())] == ["Submit application"]


def test_the_browser_comes_back_after_its_window_is_closed(srv):
    """The person closes the automation browser's window: the next job opens a fresh one,
    instead of every job failing until the desk is restarted."""
    async def go():
        await srv.browser.page()
        await srv.browser._ctx.close()  # the window is closed
        tab = await srv.browser.new_tab()
        await tab.goto(fixture_url("site/step1.html"))
        return (await srv.inspect_form())["fields"]

    fields = run(go())
    assert any(f["label"].startswith("First Name") for f in fields)


def test_workdays_disability_form_is_signed_and_answered(srv, job_apply_home):
    """Workday's "Self Identify" step: Name, today's Date in its Month / Day / Year boxes, and
    the profile's answer among checkboxes whose label doesn't say what they ask."""
    import datetime

    import yaml

    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text())
    data["eeo"]["disability"] = "Decline to self-identify"
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data))
    run(srv.open_application(url=fixture_url("workday_self_identify.html")))
    result = run(srv.autofill())
    assert not result["failed"], result["failed"]
    assert [f["label"] for f in result["needs_input"]] == ["Employee ID"]  # optional, and not the applicant's
    after = run(srv.inspect_form(include_dropdown_options=False))["fields"]
    today = datetime.date.today()
    assert {f.get("sublabel"): f["value"] for f in after if f["label"].startswith("Date")} == \
        {"Month": f"{today.month:02d}", "Day": f"{today.day:02d}", "Year": str(today.year)}
    assert by_label(after, "Name")["value"] == "Sam Rivera"
    assert by_label(after, "check one of the boxes")["value"] == ["I do not want to answer"]


def test_groups_with_the_same_label_are_each_filled(srv):
    """Two education blocks each ask "Did you graduate?": a group's id is on its choices,
    so it's found by that, not by its label (which two groups share)."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content("""<form>
      <h3>Education 1</h3><fieldset><legend>Did you graduate?</legend>
        <label><input type="radio" name="g1">Yes</label><label><input type="radio" name="g1">No</label></fieldset>
      <h3>Education 2</h3><fieldset><legend>Did you graduate?</legend>
        <label><input type="radio" name="g2">Yes</label><label><input type="radio" name="g2">No</label></fieldset>
      <fieldset><legend>Which shifts?</legend>
        <label><input type="checkbox" name="s">Days</label><label><input type="checkbox" name="s">Nights</label></fieldset>
      </form>"""))
    fields = run(srv.inspect_form(include_dropdown_options=False))["fields"]
    first, second = [f for f in fields if f["label"] == "Did you graduate?"]
    shifts = by_label(fields, "Which shifts")
    out = run(srv.fill_form([{"id": first["id"], "value": "Yes"}, {"id": second["id"], "value": "No"},
                             {"id": shifts["id"], "value": ["Nights"]}]))
    assert out["ok"], out
    after = run(srv.inspect_form(include_dropdown_options=False))["fields"]
    assert [f["value"] for f in after if f["label"] == "Did you graduate?"] == ["Yes", "No"]
    assert by_label(after, "Which shifts")["value"] == ["Nights"]
    # a group that lost a choice since it was read is still found by the others
    first = [f for f in after if f["label"] == "Did you graduate?"][0]
    run(page.evaluate("() => document.querySelector('input[name=g1]').parentElement.remove()"))
    out = run(srv.fill_form([{"id": first["id"], "value": "No"}]))
    assert out["ok"], out


def test_a_boards_frame_is_read_once_its_page_is_in(srv):
    """iCIMS portals draw their openings in a frame, then go on loading trackers for seconds
    (Allegis Group's: a survey and an ad pixel). Each page of results waited for the network to
    go quiet, up to 8 seconds, holding the browser meanwhile; it's read once the frame is in."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Board(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/poll"):  # a tracker that never lets the network go quiet
                time.sleep(0.5)
                body = b"{}"
            elif self.path.startswith("/inner"):
                body = b"<html><body><a href='/jobs/1/recruiter/job'><h3>Recruiter</h3></a></body></html>"
            else:
                body = (b"<html><body><script>setInterval(() => fetch('/poll?' + Math.random()), 200);"
                        b"setTimeout(() => { const f = document.createElement('iframe'); f.id = 'icims_content_iframe';"
                        b"f.src = '/inner'; document.body.append(f); }, 300);</script></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    site = ThreadingHTTPServer(("127.0.0.1", 0), Board)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{site.server_address[1]}/jobs/search?ss=1"
        run(srv.browser.page())  # the browser is up: only reading the board is timed
        start = time.monotonic()
        pages = run(srv.browser.frames_html(url, inner="#icims_content_iframe"))
        # not the whole SETTLE_WAIT the polling page would hold it for (5 s was too close on Windows CI, Oct 13)
        assert time.monotonic() - start < browser_module.SETTLE_WAIT - 1
        assert any("Recruiter" in p for p in pages)
    finally:
        site.shutdown()


async def framed(page, part, timeout=15):
    """Wait for the page's frame whose address has `part` in it to have loaded."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = next((f for f in page.frames if part in f.url), None)
        if frame is not None:
            await frame.wait_for_load_state("load")
            return frame
        await page.wait_for_timeout(100)
    raise AssertionError(f"no frame at {part} in {timeout} s: {[f.url for f in page.frames]}")


def test_a_captcha_on_show_is_said_but_its_frame_isnt_read(srv):
    """A CAPTCHA's own frame isn't read as part of the form (iCIMS's hidden hCaptcha has
    "Verify" and "Refresh Challenge."), but one on show is said, for the user to solve."""
    async def go():
        await srv.browser.page()
        await srv.browser._ctx.route("https://www.google.com/recaptcha/**", lambda route: route.fulfill(
            status=200, content_type="text/html",
            body="<html><body><label><input type=checkbox id=anchor> I'm not a robot</label>"
                 "<button>Verify</button></body></html>"))
        await srv.browser.goto(fixture_url("generic_form.html"))
        page = await srv.browser.page()
        await page.evaluate("""() => {
            const f = document.createElement('iframe');
            f.src = 'https://www.google.com/recaptcha/api2/anchor?k=x';
            f.style.cssText = 'width:304px;height:78px';
            document.body.append(f);
        }""")
        await framed(page, "anchor?k=x")
        shown = await srv.inspect_form(include_dropdown_options=False)
        await page.evaluate("() => { document.querySelector('iframe').style.visibility = 'hidden'; }")
        hidden = await srv.inspect_form(include_dropdown_options=False)
        await page.evaluate("""() => {  // an invisible reCAPTCHA's badge, on show but asking nothing
            const f = document.querySelector('iframe');
            f.src = 'https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible';
            f.style.cssText = 'width:256px;height:60px';
        }""")
        await framed(page, "size=invisible")  # (half a second wasn't always enough on Windows CI: the old frame was read)
        badge = await srv.inspect_form(include_dropdown_options=False)
        return shown, hidden, badge

    shown, hidden, badge = run(go())
    assert "captcha" not in badge
    assert "CAPTCHA" in shown.get("captcha", "")
    assert "Verify" not in [a["text"] for a in shown["actions"]]
    assert not any("robot" in (f.get("label") or "") for f in shown["fields"])
    assert "captcha" not in hidden


def test_a_video_players_frame_isnt_read_as_part_of_the_page(srv):
    """Oracle's job pages (live, issue #112, note 33) embed a YouTube player whose private video says
    "This video is private ... Sign in." in an error box: its words and its "Sign in" link are the
    player's, not the page's. Players are known by their hosts; an employer's own frame is read."""
    for url in ("https://www.youtube.com/embed/abc123", "https://youtube-nocookie.com/embed/abc123?rel=0",
                "https://player.vimeo.com/video/1", "https://fast.wistia.net/embed/iframe/x", "https://youtu.be/x",
                "https://www.dailymotion.com/embed/video/x", "https://www.loom.com/embed/x", "HTTPS://YOUTUBE.COM"):
        assert browser_module._MEDIA_FRAME.match(url), url
    for url in ("https://careers-x.icims.com/jobs/1/login?in_iframe=1", "https://boards.greenhouse.io/embed/job_app",
                "https://notyoutube.com/embed/x", "https://youtube.com.example.com/x", "about:blank", ""):
        assert not browser_module._MEDIA_FRAME.match(url), url

    video = (Path(__file__).parent / "fixtures" / "site" / "video-frame.html").read_text(encoding="utf-8")

    async def go():
        await srv.browser.page()
        await srv.browser._ctx.route("https://www.youtube.com/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body=video))
        await srv.browser.goto(fixture_url("generic_form.html"))
        page = await srv.browser.page()
        await page.evaluate("""() => {
            const f = document.createElement('iframe');
            f.src = 'https://www.youtube.com/embed/abc123';
            f.style.cssText = 'width:400px;height:200px';
            document.body.append(f);
        }""")
        await framed(page, "embed/abc123")
        return await srv.inspect_form(include_dropdown_options=False), await srv.browser.visible_text()

    form, text = run(go())
    assert form["fields"] and "Sign in" not in [a["text"] for a in form["actions"]], form["actions"]
    assert not form["errors"] and "private" not in text, (form["errors"], text)


def test_answers_a_page_loses_in_a_quick_run_of_fills_are_put_in_again(srv):
    """Insight Enterprises' Eightfold form (live, Oct 2026) lost some of a quick run of fills from
    its own record and said "Email cannot be left blank" beside the address shown. The boxes it
    marks invalid while they still show the answer are filled again, one at a time."""
    job = srv.add_job(url=fixture_url("lossy_form.html"), title="HRBP", company="Example Fab")["job"]
    run(srv.open_application(job_id=job["id"]))
    run(srv.inspect_form())
    result = run(srv.autofill())
    assert {f["label"] for f in result["filled"]} >= {"First Name", "Last Name", "Email", "City", "Postal Code"}

    async def record():
        page = await srv.browser.page()
        await page.wait_for_timeout(300)
        return await page.evaluate("() => window.record")

    profile_answers = {"First Name": "Sam", "Last Name": "Rivera", "Email": "sam.rivera@example.com",
                       "City": "Chandler", "Postal Code": "85225"}
    assert run(record()) == profile_answers
    assert not [e for e in run(srv.inspect_form())["errors"] if "blank" in e]

    # a later fill (the person's answer) that loses the earlier ones: those are put in again too
    middle = by_label(run(srv.inspect_form())["fields"], "middle name")
    run(srv.fill_form([{"id": middle["id"], "value": "J"}]))
    assert run(record()) == {**profile_answers, "Middle Name": "J"}
    assert not [e for e in run(srv.inspect_form())["errors"] if "blank" in e]

    async def cleared():
        return await (await srv.browser.page()).evaluate("() => window.cleared")

    # a phone number the site never takes is typed again a few times, not on every fill; a note
    # the desk didn't write is never touched
    times = run(cleared())
    assert 1 <= times.get("Phone", 0) <= browser_module.LOST_FILL_TRIES and "Notes" not in times, times
    run(srv.fill_form([{"id": middle["id"], "value": "K"}]))
    assert run(cleared())["Phone"] == times["Phone"]


def test_nothing_is_filled_behind_a_dialog_open_over_the_page(srv):
    """Eightfold's notice about its AI screening sat open over the form while the boxes behind it
    were filled: its Country of Residence timed out and came back as a question (live, Oct 2026).
    Nothing is filled behind an open dialog; once it's answered, everything is, and the resume the
    page already holds isn't sent again."""
    run(srv.open_application(url=fixture_url("site/ai-notice-form.html") + "?notice=1"))
    page = run(srv.browser.page())
    out = run(srv.autofill())
    assert not out["filled"] and {f["label"] for f in out["failed"]} >= {"First name", "Country of Residence"}, out
    assert all(f["error"].startswith("DialogOpen: “Notice Related to Example Corp's Use of the Eightfold AI")
               for f in out["failed"]), out["failed"]
    assert run(page.input_value("#first")) == "" and run(page.input_value("#input-13")) == ""
    run(page.click("#cancelUploadResume"))  # the person answers it
    out = run(srv.autofill())
    assert not out["failed"], out["failed"]
    filled = {f["label"]: f["value"] for f in out["filled"]}
    assert filled["First name"] == "Sam" and filled["Country of Residence"] == "United States", filled
    assert run(page.evaluate("() => window.uploads")) == 0


def test_a_long_requirement_row_is_its_questions_label_not_its_number(srv):
    """Phoenix Children's qualifications (live, Oct 2026) are a table of rows "5. | <the
    requirement> | Yes / No". A requirement too long to read as a label left its question
    called "5.", which tells the person nothing."""
    run(srv.open_application(url=fixture_url("site/qualification-table.html")))
    labels = [f["label"] for f in run(srv.browser.inspect(False))["fields"] if f["kind"] != "file"]
    assert labels[0] == "Three (3+) or more years of experience in accounting. Required"
    assert labels[1].startswith("Experience in system integrated Enterprise Resource Planning (ERP)")
    assert labels[1].endswith("…") and len(labels[1]) <= 300
    assert labels[2] == "I Agree"


def test_a_sign_in_window_that_closes_itself_hands_back_to_the_tab_that_opened_it(srv):
    """A job's tab opens a sign-in window, which the desk follows, and the window closes itself
    once the person is in. With the desk's one tab per job, the tools go back to the tab that
    opened it: the tab's close forgot its opener before that was looked up, so the job failed
    with "The tab this application was in has been closed"."""
    async def go():
        tab = await srv.browser.new_tab()
        await tab.goto(fixture_url("site/step1.html"))
        srv.browser.strict_tabs = True
        try:
            async with tab.expect_popup() as opened:
                await tab.evaluate("u => { window.open(u) }", fixture_url("site/signin.html"))
            popup = await opened.value
            assert srv.browser.use_tab(popup)  # (followed, as a click of the desk's would be)
            async with popup.expect_event("close"):  # (listened for first: the close can beat a wait begun after it)
                await popup.evaluate("() => { setTimeout(() => window.close(), 50) }")
            return tab, await srv.browser.page()
        finally:
            srv.browser.strict_tabs = False

    tab, now = run(go())
    assert now is tab


def test_a_jobs_own_tab_closed_is_still_said_closed(srv):
    """The tab a job was opened in, closed (not a window it opened): nothing to go back to."""
    async def go():
        tab = await srv.browser.new_tab()
        await tab.goto(fixture_url("site/step1.html"))
        srv.browser.strict_tabs = True
        try:
            await tab.close()
            with pytest.raises(browser_module.TabClosed):
                await srv.browser.page()
        finally:
            srv.browser.strict_tabs = False

    run(go())


def test_a_box_being_drawn_again_isnt_waited_on_before_typing(srv):
    """A date part Workday is drawing again (after a menu pick) matches nothing for a moment: the
    check for an overlay waited Playwright's default 30 s per part before falling back to focus.
    It asks for 2 s at most, as the click it stands in for would."""
    import time

    async def go():
        page = await srv.browser.page()
        await page.goto(fixture_url("site/step1.html"))
        began = time.monotonic()
        covered = await browser_module._covered(page.locator("#not-drawn-yet"))
        return covered, time.monotonic() - began

    covered, took = run(go())
    assert covered and took < 5, took


def test_a_box_that_isnt_there_to_type_in_is_given_up_on_in_seconds(srv):
    """After the 2 s overlay check, the box was focused with Playwright's default 30 s wait: a box
    being drawn again still cost 30 s, only later. Its focus waits FOCUS_TIMEOUT at most."""
    import time

    from playwright.async_api import TimeoutError as PlaywrightTimeout

    async def go():
        page = await srv.browser.page()
        await page.goto(fixture_url("site/step1.html"))
        began = time.monotonic()
        with pytest.raises(PlaywrightTimeout):
            await srv.browser._activate(page.locator("#not-drawn-yet"))
        return time.monotonic() - began

    took = run(go())
    assert took < 2 + browser_module.FOCUS_TIMEOUT / 1000 + 3, took


def test_a_resume_box_whose_group_says_it_is_required_is_required(srv):
    """EMD Electronics' own application (live run 19, Oct 11): its resume box isn't marked required, but the group
    around it says "Your resume/CV is required"; the desk pressed Next twice and said only "The page didn't move
    on". The box counts as required (so a missing resume is said as such); the attachments box beside it doesn't."""
    run(srv.open_application(url=fixture_url("site/resume-required-header.html")))
    fields = run(srv.inspect_form(include_dropdown_options=False))["fields"]
    assert by_label(fields, "Upload Resume/CV")["required"]
    assert not by_label(fields, "Upload Additional Attachments")["required"]
    assert not by_label(fields, "Second resume (optional)")["required"]  # (nor one that says it's optional)
