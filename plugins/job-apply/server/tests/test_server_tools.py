"""The tools Claude calls: what they refuse, which job they act for, what Claude reads back."""

import asyncio
from pathlib import Path

import pytest
from conftest import browser_available, run

from job_apply import config
from job_apply.autofill import resolve_field

FORM = """<!doctype html><html><body><h1>{title}</h1>
<form action="{action}" method="get">
 <label for="em">Email</label><input id="em" name="em" type="text">
 <label for="pw">Password</label><input id="pw" name="pw" type="password">
 <label for="rs">Resume</label><input id="rs" name="rs" type="file">
 <button type="submit">Submit application</button>
</form></body></html>"""
DONE = "<!doctype html><html><body><p>Thank you for applying!</p></body></html>"
INDEED_HOST = """<!doctype html><html><body><h1>Careers at Example</h1>
<iframe src="https://apply.indeed.com/form" style="width:900px;height:500px"></iframe></body></html>"""
INDEED_FORM = """<!doctype html><html><body><form action="https://apply.indeed.com/done" method="get">
<label for="q">Phone</label><input id="q" name="q" type="text" value="480-555-0123">
<button type="submit">Submit your application</button></form></body></html>"""
PAGES = {
    "https://careers.example.com/a": FORM.format(title="Job A", action="https://careers.example.com/a-done"),
    "https://jobs.other.com/b": FORM.format(title="Job B", action="https://jobs.other.com/b-done"),
    "https://careers.example.com/indeed-host": INDEED_HOST,
    "https://apply.indeed.com/form": INDEED_FORM,
    "https://acme.wd1.myworkdayjobs.com/en-US/External/login": FORM.format(title="Sign in", action="x"),
}


def routed(srv) -> list[str]:
    """The browser with every request answered by the pages above; nothing leaves the machine."""
    sent: list[str] = []

    async def route(r):
        url = r.request.url.split("?")[0]
        if url.endswith("done"):
            sent.append(r.request.url)
            return await r.fulfill(status=200, content_type="text/html", body=DONE)
        if url in PAGES:
            return await r.fulfill(status=200, content_type="text/html", body=PAGES[url])
        return await r.abort()

    async def go():
        await srv.browser._launch()
        await srv.browser._ctx.route("**/*", route)
    run(go())
    return sent


needs_browser = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


@needs_browser
def test_indeeds_form_inside_an_employers_page_is_the_persons_to_submit(srv):
    sent = routed(srv)
    job = srv.add_job(url="https://careers.example.com/indeed-host", title="Tech", company="Example")["job"]
    opened = run(srv.open_application(job_id=job["id"]))
    assert opened["submit_policy"] == "user_clicks_submit"
    out = run(srv.submit_application(user_confirmed=True))
    assert out["submitted"] is False and "Indeed" in out["reason"]
    # and the browser itself won't press it, whoever asks
    buttons = run(srv.browser.find_submit())
    with pytest.raises(srv.SubmitBlocked):
        run(srv.browser.press_submit(buttons[-1]["id"]))
    assert sent == []


@needs_browser
def test_a_page_opened_by_its_address_isnt_the_job_opened_before(srv):
    sent = routed(srv)
    a = srv.add_job(url="https://careers.example.com/a", title="Tech", company="Intel")["job"]
    Path(a["folder"], "Sam_Rivera_Resume.pdf").write_bytes(b"%PDF-1.4 tailored for Intel")
    run(srv.open_application(job_id=a["id"]))
    run(srv.open_application(url="https://jobs.other.com/b"))
    assert srv.browser.current_job_id is None
    with pytest.raises(ValueError, match="No job selected"):
        run(srv.submit_application(user_confirmed=True))
    # a job saved at that address becomes the current one
    b = srv.add_job(url="https://jobs.other.com/b", title="FSE", company="ASML")["job"]
    run(srv.open_application(url="https://jobs.other.com/b"))
    assert srv.browser.current_job_id == b["id"]
    assert sent == [] and srv.get_job(a["id"])["job"]["status"] != "applied"


@needs_browser
def test_a_saved_password_goes_only_into_a_password_box_on_its_own_site(srv, job_apply_home):
    routed(srv)
    config.secrets_path().write_text("workday_password: S3cret-Workday!\n")
    run(srv.browser.goto("https://careers.example.com/a"))
    fields = {f["label"]: f["id"] for f in run(srv.inspect_form(False))["fields"]}
    out = run(srv.fill_secret(fields["Email"], "workday_password"))
    assert out["ok"] is False and "password box" in out["error"]
    out = run(srv.fill_secret(fields["Password"], "workday_password"))  # not a Workday site
    assert out["ok"] is False and "another site" in out["error"]
    assert "S3cret" not in str(run(srv.inspect_form(False)))
    run(srv.browser.goto("https://acme.wd1.myworkdayjobs.com/en-US/External/login"))
    fields = {f["label"]: f["id"] for f in run(srv.inspect_form(False))["fields"]}
    assert run(srv.fill_secret(fields["Password"], "workday_password")) == {"ok": True}


@needs_browser
def test_the_desks_own_files_arent_uploaded(srv, job_apply_home):
    routed(srv)
    config.secrets_path().write_text("workday_password: S3cret-Workday!\n")
    run(srv.browser.goto("https://careers.example.com/a"))
    fields = {f["label"]: f["id"] for f in run(srv.inspect_form(False))["fields"]}
    out = run(srv.fill_form([{"id": fields["Resume"], "value": str(config.secrets_path())}]))
    assert not out["results"][0]["ok"] and "desk's own files" in out["results"][0]["error"]


def test_tool_errors_reach_claude_in_their_own_words(srv):
    from mcp.server.mcpserver.exceptions import ToolError

    async def call(name, args):
        with pytest.raises(ToolError) as e:
            await srv.mcp.call_tool(name, args)
        return str(e.value)
    assert "No job with id 999" in asyncio.get_event_loop().run_until_complete(call("get_job", {"job_id": 999}))
    job = srv.add_job(url="https://example.com/jobs/1", title="Tech", company="Example")["job"]
    assert "Unknown status 'aplied'" in run(call("update_job", {"job_id": job["id"], "status": "aplied"}))


def test_a_profile_with_a_typo_says_where(job_apply_home):
    config.profile_path().write_text('documents:\n  resume: "C:\\Users\\Sam\\resume.pdf"\n')
    with pytest.raises(ValueError, match=r"typo near line 2.*backslashes"):
        config.Profile.load()
    config.profile_path().write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="sections like"):
        config.Profile.load()


def test_passwords_are_read_as_written(job_apply_home):
    path = config.secrets_path()
    for written, meant in (("0123456", "0123456"), ("yes", "yes"), ("12:30:45", "12:30:45"),
                           ("!Summer2024x", "!Summer2024x"), ("*pw", "*pw"), ("'it''s'", "it's"), ('"q\\"x"', 'q"x'),
                           ("plain # my work one", "plain")):
        path.write_text(f"workday_password: {written}\n")
        assert config.get_secret("workday_password") == meant, written


def test_settings_written_by_hand(monkeypatch):
    monkeypatch.delenv("JOB_APPLY_HEADLESS")
    s = config.Settings.from_dict({"browser_channel": "edge", "headless": "false", "auto_submit_ats": "workday, greenhouse"})
    assert s.browser_channel == "msedge" and s.headless is False and s.auto_submit_ats == ["workday", "greenhouse"]
    s = config.Settings.from_dict({"browser_channel": None, "headless": "yes"})
    assert s.browser_channel == "chrome" and s.headless is True and not s.warnings
    s = config.Settings.from_dict({"browser_channel": "firefox"})
    assert s.browser_channel == "chrome" and "browser_channel" in s.warnings[0]


def test_saved_answers_fill_only_that_question_and_not_another_employers(job_apply_home):
    config.save_answer("Why do you want to work here?*", "Intel's fabs are where I learned the trade.", "Intel")
    config.save_answer("Are you currently employed?", "Yes", "Intel")
    config.save_answer("I agree", True, "Intel")
    config.save_answer("Do you have a valid driver's license?", "Yes", "Intel")
    prof = config.Profile.load()

    def answer(label, company, kind="text"):
        a = resolve_field({"id": "1", "kind": kind, "label": label, "value": ""}, prof, {"company": company})
        return a.value if a else None
    assert answer("Why do you want to work here?", "Intel") == "Intel's fabs are where I learned the trade."
    assert answer("Why do you want to work here?", "ASML") is None  # about Intel
    assert answer("Are you currently employed by ASML or any of its subsidiaries?", "ASML") is None
    assert answer("I agree to resolve any dispute by binding individual arbitration", "ASML", "checkbox") is None
    assert answer("Do you have a valid driver's license? *", "ASML") == "Yes"  # the same question anywhere


BROWSER_PAGES = {
    "https://jobs.example.com/one-page": """<!doctype html><html><body><h1>Field Service Engineer</h1>
<p>Thank you for your interest in Example Semi.</p>
<form onsubmit="event.preventDefault()"><label for="n">Full Name</label><input id="n" name="n">
<button type="submit">Apply for this job</button></form></body></html>""",
    "https://jobs.example.com/step1": """<!doctype html><html><body><h1>Step 1</h1><form>
<label for="fn">First Name</label><input id="fn"><label for="ln">Last Name</label><input id="ln">
<button type="submit">Submit application</button></form></body></html>""",
    "https://jobs.example.com/step2": """<!doctype html><html><body><h1>Step 2</h1><form>
<label for="ref">Referred by</label><input id="ref"><label for="notes">Notes</label><textarea id="notes"></textarea>
<button type="button">Next</button></form></body></html>""",
    "https://jobs.example.com/with-policy": """<!doctype html><html><body><h1>Apply</h1>
<p>Read our <a href="https://jobs.example.com/policy" target="_blank">privacy policy</a>.</p>
<form><label for="fn">First Name</label><input id="fn"></form></body></html>""",
    "https://jobs.example.com/policy": "<!doctype html><html><body><h1>Privacy Policy</h1></body></html>",
    "https://jobs.example.com/widgets": """<!doctype html><html><body><form>
<label for="terms">I accept the terms</label><input type="checkbox" id="terms" disabled>
<label for="country">Country</label><select id="country" onchange="
  document.getElementById('state').innerHTML = this.value ? '<option></option><option>Arizona</option><option>Texas</option>' : '<option>Select a country first</option>'">
  <option value=""></option><option>United States</option></select>
<label for="state">State</label><select id="state"><option>Select a country first</option></select>
</form></body></html>""",
}


def browser_routed(srv) -> None:
    async def route(r):
        url = r.request.url.split("?")[0]
        if url in BROWSER_PAGES:
            return await r.fulfill(status=200, content_type="text/html", body=BROWSER_PAGES[url])
        return await r.abort()

    async def go():
        await srv.browser._launch()
        await srv.browser._ctx.route("**/*", route)
    run(go())


@needs_browser
def test_a_forms_own_apply_for_this_job_is_its_final_button(srv):
    """Not pressed by click() (nor by the desk, which skips final buttons), in review mode too."""
    browser_routed(srv)
    run(srv.browser.goto("https://jobs.example.com/one-page"))
    actions = run(srv.inspect_form(False))["actions"]
    assert [a.get("is_submit") for a in actions if a["text"] == "Apply for this job"] == [True]
    out = run(srv.click("Apply for this job"))
    assert out["clicked"] is False and "final submit" in out["blocked"]
    from job_apply.browser import final_text
    assert final_text("Apply Now ›") == "Apply Now" and final_text("Apply arrow_forward") == "Apply"


@needs_browser
def test_ids_from_another_page_name_nothing_on_this_one(srv, monkeypatch):
    """Claude read step 1; the person pressed on to step 2 in the window."""
    monkeypatch.delenv("JOB_APPLY_NEVER_SUBMIT", raising=False)
    browser_routed(srv)
    run(srv.browser.goto("https://jobs.example.com/step1"))
    step1 = {f["label"]: f["id"] for f in run(srv.inspect_form(False))["fields"]}
    submit = run(srv.browser.find_submit())[0]["id"]
    run(srv.browser.goto("https://jobs.example.com/step2"))
    run(srv.inspect_form(False))
    out = run(srv.fill_form([{"id": step1["Last Name"], "value": "Rivera"}]))["results"]
    assert out[0]["ok"] is False
    with pytest.raises(srv.SubmitBlocked):
        run(srv.browser.press_submit(submit))
    values = {f["label"]: f["value"] for f in run(srv.inspect_form(False))["fields"]}
    assert values == {"Referred by": "", "Notes": ""}


@needs_browser
def test_a_thank_you_already_on_the_form_isnt_a_confirmation(srv, monkeypatch):
    monkeypatch.delenv("JOB_APPLY_NEVER_SUBMIT", raising=False)
    browser_routed(srv)
    run(srv.browser.goto("https://jobs.example.com/one-page"))
    submit = run(srv.browser.find_submit())[0]["id"]
    assert run(srv.browser.press_submit(submit))["confirmed"] is False


@needs_browser
def test_a_tick_or_a_choice_the_page_doesnt_take_isnt_reported_done(srv):
    browser_routed(srv)
    run(srv.browser.goto("https://jobs.example.com/widgets"))
    ids = {f["label"]: f["id"] for f in run(srv.inspect_form(False))["fields"]}
    out = run(srv.fill_form([{"id": ids["I accept the terms"], "value": True},
                             {"id": ids["Country"], "value": "United States"},
                             {"id": ids["State"], "value": "Arizona"}]))["results"]
    assert [r["ok"] for r in out] == [False, True, True], out
    values = {f["label"]: f["value"] for f in run(srv.inspect_form(False))["fields"]}
    assert values["State"] == "Arizona" and values["I accept the terms"] is False


def test_a_browser_that_is_there_but_wont_start_isnt_swapped_for_another(monkeypatch, job_apply_home):
    """Chrome's profile held by another window: Edge, without the sign-ins, isn't the answer."""
    from playwright.async_api import Error as PlaywrightError

    from job_apply import browser as browser_module

    tried: list[str] = []

    class Chromium:
        async def launch_persistent_context(self, **kw):
            tried.append(kw.get("channel") or "bundled")
            raise PlaywrightError("BrowserType.launch_persistent_context: Target page, context or browser has been "
                                  "closed\nThe profile appears to be in use by another Chromium process")

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            pass

    class Start:
        async def start(self):
            return Playwright()

    monkeypatch.delenv("JOB_APPLY_CHROMIUM_PATH", raising=False)
    monkeypatch.setattr(browser_module, "async_playwright", lambda: Start())
    config.profile_path().write_text("settings:\n  browser_channel: chrome\n")
    session = browser_module.BrowserSession()
    with pytest.raises(browser_module.BrowserUnavailable, match="in use"):
        asyncio.new_event_loop().run_until_complete(session._launch())
    assert tried == ["chrome"]


@needs_browser
def test_a_tab_the_person_opens_doesnt_take_over(srv):
    """They opened the privacy policy from the application: the desk stays on the application."""
    browser_routed(srv)
    run(srv.browser.goto("https://jobs.example.com/with-policy"))
    page = run(srv.browser.page())

    async def person_clicks():
        async with page.context.expect_page() as opened:
            await page.click("text=privacy policy")
        await (await opened.value).wait_for_load_state()
    run(person_clicks())
    assert run(srv.inspect_form(False))["headings"] == ["Apply"]


def test_claudes_browser_tools_wait_while_the_desk_fills_an_application(srv):
    """The desk and Claude share the browser's tab: Claude's typing or clicking mid-step would
    land in the desk's application. Reading, and the desk's own calls, go ahead."""
    from mcp.server.mcpserver.exceptions import ToolError

    from job_apply import desk as desk_module
    from job_apply.pipeline import Run

    job = srv.add_job(url="https://example.com/jobs/fse", title="Field Service Engineer", company="Example Fab")["job"]
    d = desk_module.get_desk(srv)
    d.applier.runs[job["id"]] = Run(job["id"], "Field Service Engineer", "Example Fab", status="running")
    d.applier.current = job["id"]

    async def call(name, args):
        return await srv.mcp.call_tool(name, args)
    try:
        for name, args in (("click", {"target": "Next"}), ("fill_form", {"values": []}),
                           ("submit_application", {"user_confirmed": True}), ("tabs", {"switch_to": 0}),
                           ("ingest_job", {"url": "https://example.com/jobs/2", "use_browser": True})):
            with pytest.raises(ToolError, match="Job Desk is filling Field Service Engineer at Example Fab"):
                run(call(name, args))
        run(call("list_jobs", {}))  # not the browser: fine
        d.applier.current = None
        with pytest.raises(ToolError) as e:  # the desk is between jobs: the tool runs (and fails on its own terms)
            run(call("submit_application", {"user_confirmed": True}))
        assert "Job Desk" not in str(e.value)
    finally:
        desk_module._desk = None
