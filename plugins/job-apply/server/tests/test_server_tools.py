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


def test_a_greenhouse_posting_opened_by_its_address_opens_greenhouses_own_form(srv, monkeypatch):
    """A Greenhouse board set to send visitors to the employer's site redirects its postings
    there (Carvana's, behind a Cloudflare check, Oct 2026). Opened by job or by address, the
    posting opens on Greenhouse's own form instead, as search results give it."""
    opened = []

    async def goto(url):
        opened.append(url)
        return {"url": url}

    async def human_submit_ats(page=None):
        return None
    monkeypatch.setattr(srv.browser, "goto", goto)
    monkeypatch.setattr(srv.browser, "human_submit_ats", human_submit_ats)
    form = "https://job-boards.greenhouse.io/embed/job_app?for=carvana&token=8080485"
    run(srv.open_application(url="https://job-boards.greenhouse.io/carvana/jobs/8080485"))
    job = srv.add_job(url="https://job-boards.greenhouse.io/carvana/jobs/8080485", title="Coordinator",
                      company="Carvana")["job"]
    run(srv.open_application(job_id=job["id"]))
    run(srv.open_application(url="https://jobs.other.com/b"))
    assert opened == [form, form, "https://jobs.other.com/b"]


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
                           ("plain # my work one", "plain"), ('"abc"  # my note', "abc")):
        path.write_text(f"workday_password: {written}\n")
        assert config.get_secret("workday_password") == meant, written
    path.write_bytes("\ufeffworkday_password: from-notepad\n".encode())  # Notepad's byte-order mark
    assert config.get_secret("workday_password") == "from-notepad"
    # what the desk saves comes back exactly, whatever is in it
    for typed in ("a\u2028b", "x\x85y", "\u00a0lead", 'q"x', "it's", "emoji\U0001F600", "back\\slash", " sp ", "#hash",
                  "0123456", "yes", "*pw", "!Summer2024x", "a: b"):
        config.save_site_password("workday_password", typed)
        assert config.get_secret("workday_password") == typed, repr(typed)
    config.save_site_password("successfactors_password", "kept")
    assert config.get_secret("workday_password") == "a: b" and config.get_secret("successfactors_password") == "kept"


def test_settings_written_by_hand(monkeypatch):
    monkeypatch.delenv("JOB_APPLY_HEADLESS")
    assert config.Settings.from_dict({"browser_channel": "chrome-beta"}).browser_channel == "chrome-beta"
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
    for question in ("Are you a current or former employee?", "Have you previously applied?",
                     "Were you referred by a current employee?", "Does a relative of yours work here?"):
        config.save_answer(question, "Yes", "Intel")
        prof = config.Profile.load()
        assert answer(question, "KLA") != "Yes", question  # Intel's answer isn't KLA's (the profile may answer it)
        assert answer(question, "Intel") == "Yes", question


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
    "https://jobs.example.com/icon-apply": """<!doctype html><html><body><h1>Technician</h1>
<form onsubmit="event.preventDefault()"><label for="n">Full Name</label><input id="n">
<button type="submit" aria-label="Apply"><svg width="16" height="16"><circle cx="8" cy="8" r="6"/></svg></button>
</form></body></html>""",
    "https://jobs.example.com/with-follow": """<!doctype html><html><body><h1>Apply</h1>
<iframe src="https://www.linkedin.com/company/example/follow" style="width:200px;height:40px"></iframe>
<form><label for="fn">First Name</label><input id="fn"><button type="submit">Submit application</button></form>
</body></html>""",
    "https://www.linkedin.com/company/example/follow": "<!doctype html><html><body><button>Follow</button></body></html>",
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


@needs_browser
def test_an_icon_only_final_button_is_a_final_button(srv):
    """Its words are only its aria-label: click() won't press it; the submit path will."""
    browser_routed(srv)
    run(srv.browser.goto("https://jobs.example.com/icon-apply"))
    assert [a["text"] for a in run(srv.browser.find_submit())] == ["Apply"]
    out = run(srv.click("Apply"))
    assert out["clicked"] is False and "final submit" in out["blocked"]


@needs_browser
def test_a_linkedin_follow_widget_doesnt_make_an_employers_form_linkedins(srv):
    browser_routed(srv)
    run(srv.browser.goto("https://jobs.example.com/with-follow"))
    assert run(srv.browser.human_submit_ats()) is None


def test_the_browser_works_one_step_at_a_time_on_each_event_loop():
    """The browser session outlives an event loop (each test has its own). A lock a caller
    waited on in one loop used to fail the next loop's first wait: "bound to a different
    event loop" (CI, Python 3.10)."""
    from job_apply.browser import BrowserSession

    session = BrowserSession()
    order = []

    async def step(name):
        async with session._lock:
            order.append(name)
            await asyncio.sleep(0)

    async def two_at_once():
        await asyncio.gather(step("a"), step("b"))  # the second waits on the first

    for _ in range(2):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(two_at_once())
        finally:
            loop.close()
    assert order == ["a", "b", "a", "b"]


def test_cookie_and_privacy_banners_are_never_accepted():
    """Declined where the banner lets them be, otherwise left to the person: never accepted
    for them, whatever the words ("Accept", "AGREE AND PROCEED", "Accept All Cookies")."""
    from job_apply.browser import _accepts_cookies

    for label, in_banner in (("Accept All Cookies", False), ("Accept Cookies", False), ("Accept", True),
                             ("AGREE AND PROCEED", True), ("Allow all", True), ("I agree", True), ("Got it", True)):
        assert _accepts_cookies(label, label, in_banner), label
    for label, in_banner in (("Reject All", True), ("Accept only necessary cookies", True), ("Use necessary cookies only", True),
                             ("Cookie settings", True), ("Continue without accepting", True), ("Accept", False),
                             ("I agree to the terms", False), ("Next", True),
                             ("I agree to the Terms of Use and Cookie Policy", False)):  # an application's consent
        assert not _accepts_cookies(label, label, in_banner), label


@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_the_click_tool_refuses_a_cookie_banners_accept(srv):
    from conftest import fixture_url

    async def go():
        await srv.browser.goto(fixture_url("site/cookie-form.html"))
        accept = await srv.click("Accept")
        accepted = await srv.browser._page.evaluate("() => window.accepted || 0")
        reject = await srv.click("Reject")
        return accept, accepted, reject

    accept, accepted, reject = run(go())
    assert accept["clicked"] is False and "aren't accepted" in accept["blocked"] and accepted == 0
    assert reject["clicked"] is True



@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_the_click_tool_accepts_a_cookie_banner_only_where_the_person_allows_it(srv, job_apply_home):
    from conftest import fixture_url

    profile = job_apply_home / "profile.yaml"
    profile.write_text(profile.read_text().replace("settings:\n", "settings:\n  accept_cookies: true\n"))

    async def go():
        clicks = []
        # a banner with a way to decline is declined, setting or not ("Deny" in OneTrust's banner,
        # around an accept button that's a box of its own by its id)
        for page, button in (("cookie-form.html", "Accept"), ("cookie-deny.html", "Allow all"),
                             ("cookie-agree-only.html", "AGREE AND PROCEED")):
            await srv.browser.goto(fixture_url(f"site/{page}"))
            clicked = await srv.click(button)
            clicks.append((clicked["clicked"], await srv.browser._page.evaluate("() => window.accepted || 0")))
        return clicks

    assert run(go()) == [(False, 0), (False, 0), (True, 1)]


@needs_browser
def test_a_school_the_sites_list_doesnt_have_is_its_other(srv, job_apply_home):
    """Workday's School or University list is the employer's own, and small colleges are often
    left out: the typed name read as filled while the box stayed empty. The list's "Other" is
    the true answer there."""
    import yaml
    from conftest import fixture_url

    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["education_history"][0]["school"] = "Example Valley Community College"
    path.write_text(yaml.safe_dump(profile))

    async def go():
        await srv.browser.goto(fixture_url("site/workday-school-not-listed.html"))
        result = await srv.autofill()
        pills = await srv.browser._page.evaluate(
            "() => [...document.querySelectorAll('[data-automation-id=selectedItem]')].map((p) => p.textContent)")
        return result, pills

    result, pills = run(go())
    assert pills == ["Other"], (pills, result["failed"])
    assert not result["failed"], result["failed"]
    # only for a school the list lacks: not a slow list, or a pick the box didn't take
    assert srv._NOT_IN_LIST.search("nothing in its list matched 'Example Valley Community College'")
    assert not srv._NOT_IN_LIST.search("Picked 'Arizona State University' but the field didn't take it")
    assert not srv._NOT_IN_LIST.search("Timeout 5000ms exceeded")


@needs_browser
def test_a_failed_desk_jobs_tab_is_still_the_desks(srv):
    """Resume takes a failed job up again in its tab: Claude's open_application doesn't load
    another posting there."""
    from job_apply import desk as desk_module
    from job_apply.pipeline import Run

    job = srv.add_job(url="https://careers.example.com/a", title="Tech", company="Example")["job"]
    d = desk_module.get_desk(srv)

    async def go():
        tab = await srv.browser.new_tab()
        d.applier.runs[job["id"]] = Run(job["id"], "Tech", "Example", status="failed", page=tab)
        return srv._desk_tab_job(tab)
    try:
        assert run(go()) == job["id"]
    finally:
        desk_module._desk = None


@needs_browser
def test_a_submit_whose_record_cant_be_written_is_still_in_the_tracker(srv):
    """A full disk (here: a folder where the record goes) mustn't lose a confirmed submit."""
    sent = routed(srv)
    job = srv.add_job(url="https://careers.example.com/a", title="Tech", company="Example")["job"]
    (Path(job["folder"]) / "submission.json").mkdir()
    run(srv.open_application(job_id=job["id"]))
    out = run(srv.submit_application(job_id=job["id"], user_confirmed=True))
    assert out["submitted"] and out["confirmed"] and len(sent) == 1, out
    assert srv.get_job(job["id"])["job"]["status"] == "applied"


@needs_browser
@pytest.mark.parametrize("how", ["broke", "blocked"])
def test_a_press_that_doesnt_finish_is_remembered_and_one_never_made_isnt(srv, monkeypatch, how):
    """The press is recorded before it's made: a tab that dies mid-press may still have sent
    the application, so the Job Desk never presses that job's Submit again by itself. A press
    the browser refused leaves no record."""
    from job_apply.pipeline import _pressed_before

    routed(srv)
    job = srv.add_job(url="https://careers.example.com/a", title="Tech", company="Example")["job"]
    run(srv.open_application(job_id=job["id"]))

    async def press(button_id):
        if how == "blocked":
            raise srv.SubmitBlocked("Not this one")
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(srv.browser, "press_submit", press)
    if how == "broke":
        with pytest.raises(RuntimeError):
            run(srv.submit_application(job_id=job["id"], user_confirmed=True))
    else:
        assert run(srv.submit_application(job_id=job["id"], user_confirmed=True))["submitted"] is False
    job = srv.tracker().get(job["id"])
    notes = [e["note"] for e in srv.tracker().events(job["id"])]
    if how == "broke":
        assert _pressed_before(job) and any(str(n).startswith("pressed Submit;") for n in notes), notes
    else:
        assert not _pressed_before(job) and not (Path(job["folder"]) / "submission.json").exists()


@needs_browser
def test_a_job_id_that_isnt_the_open_tabs_is_refused(srv):
    """Job A opened, then job B in the same tab: autofill(job_id=A) used to put A's answers
    and resume into B's form, and submit_application(job_id=A) pressed B's Submit and marked
    A applied. Switching tabs makes the tab's own job the current one."""
    sent = routed(srv)
    a = srv.add_job(url="https://careers.example.com/a", title="Tech", company="Example")["job"]
    b = srv.add_job(url="https://jobs.other.com/b", title="FSE", company="Other")["job"]
    run(srv.open_application(job_id=a["id"]))
    run(srv.browser.new_tab())
    run(srv.open_application(job_id=b["id"]))
    for call in (srv.autofill(job_id=a["id"]), srv.submit_application(job_id=a["id"], user_confirmed=True)):
        with pytest.raises(ValueError, match="isn't the one open"):
            run(call)
    assert sent == [] and srv.get_job(a["id"])["job"]["status"] != "applied"
    run(srv.tabs(switch_to=0))
    assert srv.browser.current_job_id == a["id"]
    run(srv.autofill(job_id=a["id"]))  # its own tab: fine


@needs_browser
def test_a_saved_password_goes_only_onto_the_site_its_named_for(srv):
    routed(srv)
    import os
    os.environ["JOB_APPLY_SECRET_LINKEDIN_PASSWORD"] = "not-a-real-password"
    os.environ["JOB_APPLY_SECRET_OTHER_PASSWORD"] = "not-a-real-password-either"
    try:
        run(srv.browser.goto("https://jobs.other.com/b"))
        box = next(f["id"] for f in run(srv.inspect_form(False))["fields"] if f["kind"] == "password")
        assert run(srv.fill_secret(box, "linkedin_password"))["ok"] is False
        assert run(srv.fill_secret(box, "other_password"))["ok"] is True  # named for this site
        out = run(srv.fill_secret(box, "emaıl_password"))  # a dotless i is still the inbox's password
        assert out["ok"] is False and "inbox" in out["error"]
    finally:
        del os.environ["JOB_APPLY_SECRET_LINKEDIN_PASSWORD"], os.environ["JOB_APPLY_SECRET_OTHER_PASSWORD"]


SCRIPTED_SEND = """<!doctype html><html><body><h1>Review your application</h1>
<button type="button" onclick="fetch('/api/send-application', {method: 'POST'})">Finish</button>
<button type="button" onclick="fetch('/api/send-application', {method: 'POST'})">Send</button></body></html>"""


@needs_browser
def test_practice_mode_doesnt_press_a_plain_button_that_sends(srv, monkeypatch):
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    sent: list[str] = []

    async def route(r):
        if r.request.method == "POST":
            sent.append(r.request.url)
        return await r.fulfill(status=200, content_type="text/html", body=SCRIPTED_SEND)

    async def go():
        await srv.browser._launch()
        await srv.browser._ctx.route("**/*", route)
        await srv.browser.goto("https://jobs.contoso.example/apply")
        return [await srv.click(name) for name in ("Finish", "Send")]

    for out in run(go()):
        assert out["clicked"] is False and "Dry run" in out["blocked"], out
    assert sent == []


@needs_browser
def test_reading_a_posting_leaves_the_desks_paused_tab_alone(srv):
    from job_apply import desk as desk_module
    from job_apply.pipeline import Run

    routed(srv)
    paused = srv.add_job(url="https://careers.example.com/a", title="Tech", company="Example")["job"]
    run(srv.open_application(job_id=paused["id"]))
    d = desk_module.get_desk(srv)
    try:
        d.applier.runs[paused["id"]] = Run(paused["id"], "Tech", "Example", status="needs_you",
                                          page=srv.browser.current_tab)
        tab = srv.browser.current_tab
        run(srv.ingest_job("https://jobs.other.com/b", use_browser=True))
        assert tab.url == "https://careers.example.com/a"
    finally:
        desk_module._desk = None
