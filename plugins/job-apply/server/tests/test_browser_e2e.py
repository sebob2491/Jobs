"""Drive the MCP tools against local mock application forms in headless Chromium."""

import pytest
from conftest import browser_available, by_label, fixture_url, run

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
