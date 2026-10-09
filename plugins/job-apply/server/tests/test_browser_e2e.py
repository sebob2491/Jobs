"""Drive the MCP tools against local mock application forms in headless Chromium."""

import pytest
from conftest import browser_available, by_label, fixture_url, run

from job_apply import browser as browser_module

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


def test_inspect_waits_for_a_form_drawn_late(srv):
    """Oracle's email step draws its form a moment after the page itself."""
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


def test_a_menu_left_over_the_next_field_isnt_its_options(srv):
    """Micron: the availability question's menu stayed open over the next question and was
    drawn afresh when focus moved, so its options were read as the next question's."""
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
          ul.style.top = (r.bottom + scrollY) + 'px';
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
        let availMenu = null;
        avail.addEventListener('click', () => { if (!availMenu) availMenu = menu(avail, AVAIL); });
        // ignores Escape and blur, and draws itself again when focus moves on
        friends.addEventListener('focus', () => { if (availMenu) { availMenu.remove(); availMenu = menu(avail, AVAIL); } });
        const later = () => setTimeout(() => menu(friends, ['Yes', 'No']), 400);
        friends.addEventListener('click', later);
        friends.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown') later(); });
      </script>"""))
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
        assert time.monotonic() - start < 5
        assert any("Recruiter" in p for p in pages)
    finally:
        site.shutdown()


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
        await page.wait_for_timeout(500)
        shown = await srv.inspect_form(include_dropdown_options=False)
        await page.evaluate("() => { document.querySelector('iframe').style.visibility = 'hidden'; }")
        hidden = await srv.inspect_form(include_dropdown_options=False)
        await page.evaluate("""() => {  // an invisible reCAPTCHA's badge, on show but asking nothing
            const f = document.querySelector('iframe');
            f.src = 'https://www.google.com/recaptcha/api2/anchor?k=x&size=invisible';
            f.style.cssText = 'width:256px;height:60px';
        }""")
        await page.wait_for_timeout(500)
        badge = await srv.inspect_form(include_dropdown_options=False)
        return shown, hidden, badge

    shown, hidden, badge = run(go())
    assert "captcha" not in badge
    assert "CAPTCHA" in shown.get("captcha", "")
    assert "Verify" not in [a["text"] for a in shown["actions"]]
    assert not any("robot" in (f.get("label") or "") for f in shown["fields"])
    assert "captcha" not in hidden


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
