"""The Job Desk's local web API, driven the way its page drives it."""

import asyncio
import json
import os
import time

import httpx
import pytest
from conftest import browser_available, fixture_url, launch_options, run

import job_apply.pipeline as pipeline
from job_apply import config
from job_apply.desk import Desk
from job_apply.postings import Posting

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


def test_desk_finds_applies_and_submits(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    posting = fixture_url("site/posting.html")

    async def fake_search(query, companies=None, location=None, limit_per_company=10):
        assert query and location == "AZ"  # from the profile's preferences and home state
        return {"results": [
            {"company": "Example Fab", "title": "Field Service Engineer", "url": posting, "location": "Phoenix, AZ",
             "posted": "Posted 2 Days Ago", "ats": "company_site"},
            {"company": "Example Bank", "title": "Accountant", "url": "https://example.com/jobs/acct",
             "location": "Phoenix, AZ"},
        ], "errors": {"Broken Co": "SearchError: HTTP 500"},
            "browser_only": [{"company": "TSMC Arizona", "careers_url": "https://careers.tsmc.com"}]}

    async def fetch(url):  # only postings that were read are recommended
        if url != posting:
            raise RuntimeError("no network in tests")
        return Posting(url=url, location="Phoenix, AZ", description=(
            "Maintain and repair semiconductor equipment at customer sites across the Phoenix area.\n"
            "Requirements\n- High school diploma or GED.\n- 2+ years of hands-on equipment maintenance experience.\n"
            "- Willingness to travel up to 25% of the time."))

    monkeypatch.setattr(srv, "search_company_jobs", fake_search)
    desk = Desk(srv)
    desk.fetch = fetch

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            await drive()
        finally:
            await desk.stop()

    async def drive():
        h = {"x-desk-token": desk.token}
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False) as c:
            async def state():
                r = await c.get("/api/state", headers=h)
                assert r.status_code == 200
                return r.json()

            async def until(check, timeout=60):
                start = time.monotonic()
                while True:
                    s = await state()
                    if check(s):
                        return s
                    if time.monotonic() - start > timeout:
                        raise AssertionError(f"timed out; last state: {s}")
                    await asyncio.sleep(0.3)

            def run_of(s, job_id):
                return next(r["run"] for r in s["listings"] if r["job_id"] == job_id)

            # locked to this computer and this page
            assert (await c.get("/api/state")).status_code == 403
            assert (await c.get("/api/state", headers={"x-desk-token": "guess"})).status_code == 403
            # a key that isn't plain ASCII is refused, not a server error
            assert (await c.get("/api/state", headers={"x-desk-token": "gu\u00e9ss".encode("latin-1")})).status_code == 403
            assert (await c.get("/api/state", headers={**h, "host": "evil.example"})).status_code == 403
            page = await c.get("/")
            assert page.status_code == 200 and "<title>Job Desk</title>" in page.text

            await c.post("/api/search", headers=h, json={})
            s = await until(lambda s: s["search"]["status"] == "done")
            rows = {r["title"]: r for r in s["listings"]}
            assert rows["Field Service Engineer"]["fit"]["recommended"]
            assert not rows["Accountant"]["fit"]["recommended"]
            assert s["search"]["browser_only"] == [{"company": "TSMC Arizona", "careers_url": "https://careers.tsmc.com"}]
            assert s["search"]["errors"] == {"Broken Co": "SearchError: HTTP 500"}

            # the one button
            job_id = (await c.post("/api/apply", headers=h, json={"urls": [posting]})).json()["queued"][0]
            s = await until(lambda s: (run_of(s, job_id) or {}).get("need") == "sign_in")
            assert run_of(s, job_id)["blocking"]
            assert (await c.post(f"/api/job/{job_id}/show", headers=h, json={})).status_code == 200
            tab = desk.applier.runs[job_id].page
            await tab.fill("#em", "sam.rivera@example.com")
            await tab.fill("#pw", "not-a-real-password")
            await tab.click("button:has-text('Sign In')")

            s = await until(lambda s: (run_of(s, job_id) or {}).get("need") == "questions")
            questions = [q["label"] for q in run_of(s, job_id)["questions"]]
            license_q = next(q for q in questions if "driver" in q)
            terms_q = next(q for q in questions if "terms" in q)
            await c.post("/api/answer", headers=h, json={"job_id": job_id, "answers": [
                {"label": license_q, "value": "Yes", "remember": True},
                {"label": terms_q, "value": "Yes", "remember": False}]})
            s = await until(lambda s: (run_of(s, job_id) or {}).get("status") == "ready")
            assert [a["question"] for a in config.saved_answers()] == [license_q]  # the consent wasn't remembered

            assert (await c.post(f"/api/job/{job_id}/submit", headers=h, json={})).status_code == 200
            s = await until(lambda s: (run_of(s, job_id) or {}).get("status") == "submitted")
            assert s["counts"].get("applied") == 1

            # auto-submit is remembered between sessions
            await c.post("/api/settings", headers=h, json={"auto_submit": True})
        assert Desk(srv).applier.auto_submit

    run(go())


def test_desk_page_buttons_reach_the_api(srv, tmp_path):
    """Click through the real page in a separate browser: pick a job, press Apply, answer a question."""
    from playwright.async_api import async_playwright

    from job_apply.pipeline import Run

    posting = fixture_url("site/posting.html")
    desk = Desk(srv)
    many = "Phoenix, AZ; Chandler, AZ; Austin, TX; Hillsboro, OR; Boise, ID"  # a multi-site Workday posting
    desk.listings = [
        {"company": "Example Fab", "title": "Field Service Engineer", "url": posting, "location": many,
         "posted": "2026-08-25T13:11:48-04:00",  # as Greenhouse's API gives it
         "fit": {"score": 80, "reasons": ["title matches"], "concerns": [], "blocked": False, "recommended": True}},
        {"company": "Example Bank", "title": "Accountant", "url": "https://example.com/acct", "location": "Phoenix, AZ",
         "fit": {"score": 20, "reasons": [], "concerns": ["not one of your target titles"], "blocked": False,
                 "recommended": False}},
    ]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    desk.applier.runs[other["id"]] = Run(other["id"], "Technician", "Example Litho", status="needs_you", need="questions",
                                         reason="2 questions", questions=[{"id": "9", "label": "Do you have a valid driver's license?",
                                                                           "kind": "select", "options": ["Select One", "Yes", "No"],
                                                                           "required": True},
                                                                          # SuccessFactors' list: its first page only
                                                                          {"id": "10", "label": "Country", "kind": "combobox",
                                                                           "options": ["No Selection", "Afghanistan", "Albania"],
                                                                           "required": True}])
    desk.applier.start = lambda: None  # the queue isn't worked in this test
    desk.search.update(status="done", at=time.time())  # recent, so opening the page doesn't search the real sites

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("text=Field Service Engineer")
                assert await page.locator("text=Accountant").count() == 0  # not recommended: hidden by default
                where = page.locator(f"span[title='{many}']")
                assert await where.inner_text() == "Phoenix, AZ; Chandler, AZ; Austin, TX and 2 more"
                meta = await page.locator(".meta").first.inner_text()
                assert "2026-08-25" in meta and "T13:11" not in meta  # the date, not the time of day
                await page.click("text=Select recommended")
                await page.click("#apply")
                for _ in range(50):
                    if desk.applier.tasks:
                        break
                    await asyncio.sleep(0.1)
                queued = [jid for _, jid in desk.applier.tasks]
                assert [srv.tracker().get(j)["title"] for j in queued] == ["Field Service Engineer"]

                await page.select_option("select[data-q]", "Yes")
                # a searchable list: typed in, the entry needn't be among those shown
                country = page.locator("input[list][data-q='Country']")
                assert await page.locator(f"datalist#{await country.get_attribute('list')} option").evaluate_all(
                    "os => os.map((o) => o.value)") == ["Afghanistan", "Albania"]  # no "No Selection"
                await country.fill("United States")
                await asyncio.sleep(3.5)  # the page refreshes every 1.5 s: answers being given stay put
                await page.click(f"button[data-job='{other['id']}'][data-job-act='fill']")
                for _ in range(50):
                    if len(config.saved_answers()) == 2:
                        break
                    await asyncio.sleep(0.1)
                saved = {a["question"]: a["answer"] for a in config.saved_answers()}
                assert saved == {"Do you have a valid driver's license?": "Yes", "Country": "United States"}
                assert ("apply", other["id"]) in desk.applier.tasks

                await page.fill("#add-links", fixture_url("jsonld_posting.html"))
                await page.click("#add-btn")
                # listed, picked for Apply, and the box emptied (waited for: the page's own
                # refresh can draw the row a moment before the add returns)
                await page.wait_for_function("""() => {
                  const row = [...document.querySelectorAll('li.row')].find((r) => r.textContent.includes('EUV'));
                  return row && row.querySelector('input[type=checkbox]').checked
                    && document.getElementById('add-links').value === '';
                }""", timeout=20000)

                await page.fill("#pw", "typed-on-the-page")
                await page.click("#pw-form button[type=submit]")
                await page.wait_for_selector("#pw-state [data-site=workday].good")
                assert config.get_secret("workday_password") == "typed-on-the-page"
                # not left sitting in the page (cleared once the save returns; the page's own
                # refresh can show it saved a moment before that)
                await page.wait_for_function("() => document.getElementById('pw').value === ''", timeout=5000)
                # another system's password (Edwards, Qorvo and Amkor are on SuccessFactors), named
                # with the employers on the person's lists that use it
                assert await page.locator("#pw-state [data-site=successfactors].good").count() == 0
                assert "Edwards" in await page.locator("#pw-site option[value=successfactors]").inner_text()
                await page.select_option("#pw-site", "successfactors")
                await page.fill("#pw", "another-one")
                await page.click("#pw-form button[type=submit]")
                await page.wait_for_selector("#pw-state [data-site=successfactors].good")
                assert config.get_secret("successfactors_password") == "another-one"
                assert config.get_secret("workday_password") == "typed-on-the-page"
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_the_banner_names_the_job_waited_on_and_the_profile_gaps_are_in_words(srv, monkeypatch):
    """The banner names the job the queue is waiting on; what to do is in its Needs you card
    (the banner used to repeat all of it). What the profile still needs is said in words,
    not as profile.yaml's keys."""
    from playwright.async_api import async_playwright

    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    job = srv.add_job(url=fixture_url("site/posting.html"), title="Field Service Engineer", company="Example Fab")["job"]
    desk.applier.runs[job["id"]] = Run(job["id"], "Field Service Engineer", "Example Fab", status="needs_you",
                                       need="sign_in", blocking=True, reason="Sign in on Workday in the browser window.")
    monkeypatch.setattr(config.Profile, "missing_required",
                        lambda self: ["documents.resume", "work_authorization.requires_sponsorship"])

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#notices .waiting")
                banner = await page.inner_text("#notices .waiting")
                assert "Field Service Engineer (Example Fab)" in banner and "Sign in on Workday" not in banner, banner
                assert "Sign in on Workday in the browser window." in await page.inner_text("#needs")
                # Resume beside Later: the card may ask for Resume after a password reset by hand
                buttons = await page.locator("#needs button").all_inner_texts()
                assert "Resume" in buttons and "Later" in buttons, buttons
                notice = await page.inner_text("#notices .notice")
                assert "still needs: your resume file and whether you need visa sponsorship." in notice, notice
                assert "documents.resume" not in notice
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_the_page_alerts_when_a_job_needs_you(srv):
    """With alerts on, a job that comes to need the person (or is ready to submit) brings up
    one desktop notification; what was already waiting when the page opened doesn't."""
    from playwright.async_api import async_playwright

    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    first = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    second = srv.add_job(url=fixture_url("site/posting.html"), title="Field Service Engineer", company="Example Fab")["job"]
    desk.applier.runs[first["id"]] = Run(first["id"], "Technician", "Example Litho", status="needs_you", need="questions",
                                         reason="1 question")
    fake = """
      window.__shown = [];
      class FakeNotification {
        constructor(title, opts) { window.__shown.push({title, body: (opts || {}).body}); }
        close() {}
      }
      FakeNotification.permission = "default";
      FakeNotification.requestPermission = async () => (FakeNotification.permission = "granted");
      window.Notification = FakeNotification;
    """

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                await page.add_init_script(fake)
                await page.goto(desk.url)
                await page.wait_for_selector("#alerts:text('Alert me')")
                await page.click("#alerts")
                await page.wait_for_selector("#alerts:text('Alerts on')")
                await asyncio.sleep(2)  # a refresh or two: the job already waiting isn't announced
                assert await page.evaluate("window.__shown") == []
                desk.applier.runs[second["id"]] = Run(second["id"], "Field Service Engineer", "Example Fab",
                                                      status="needs_you", need="sign_in", reason="Sign in on Workday")
                await page.wait_for_function("window.__shown.length > 0", timeout=10000)
                await asyncio.sleep(2)  # announced once, not on every refresh
                shown = await page.evaluate("window.__shown")
                assert shown == [{"title": "Example Fab needs you", "body": "Field Service Engineer: Sign in on Workday"}]
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_jobs_added_elsewhere_show_up_ranked(srv):
    """Openings Claude saved from Indeed or LinkedIn join the list, so one button covers them too."""
    added = srv.add_job(url="https://www.indeed.com/viewjob?jk=abc123", title="Field Service Engineer",
                        company="TRUMPF")["job"]
    done = srv.add_job(url="https://example.com/old", title="Technician", company="Done Co")["job"]
    srv.update_job(done["id"], status="applied")
    desk = Desk(srv)
    rows = {r["company"]: r for r in desk.state()["listings"]}
    assert rows["TRUMPF"]["job_id"] == added["id"] and rows["TRUMPF"]["added"] and rows["TRUMPF"]["fit"]["score"] > 0
    assert "Done Co" not in rows  # already applied
    assert desk.apply(urls=[added["url"]], job_ids=[], submit=False) == ([added["id"]], [])


def test_jobs_already_applied_to_are_never_queued_again(srv):
    """A pasted link to last week's application, or a stale tick: with "Submit for me" on,
    queueing it again would send a second application."""
    done = srv.add_job(url="https://example.com/old", title="Technician", company="Done Co")["job"]
    fresh = srv.add_job(url="https://example.com/new", title="Field Service Engineer", company="New Co")["job"]
    srv.update_job(done["id"], status="applied")
    desk = Desk(srv)
    queued, refused = desk.apply(urls=[done["url"], fresh["url"]], job_ids=[done["id"]], submit=True)
    assert queued == [fresh["id"]] and refused == ["Technician"]
    assert [jid for _, jid in desk.applier.tasks] == [fresh["id"]]
    with pytest.raises(ValueError, match="already marked applied"):
        desk.applier.enqueue(done["id"])
    # a listing whose link isn't a web address is never opened in the browser
    assert desk.apply(urls=["javascript:alert(1)"], job_ids=[], submit=False) == ([], [])


def test_opening_the_page_searches_when_the_last_search_is_stale(srv, monkeypatch):
    from playwright.async_api import async_playwright

    searched = []

    async def fake_search(query, companies=None, location=None, limit_per_company=10):
        searched.append(query)
        return {"results": [], "errors": {}, "browser_only": []}

    monkeypatch.setattr(srv, "search_company_jobs", fake_search)
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                await page.goto(desk.url)
                for _ in range(50):
                    if searched and desk.search["status"] == "done":
                        break
                    await asyncio.sleep(0.1)
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert len(searched) == 1 and desk.search["status"] == "done"


def test_a_site_password_goes_to_secrets_only(srv):
    """Typed into the page, kept in secrets.yaml; the desk only ever says whether one is saved."""
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False) as c:
                h = {"x-desk-token": desk.token}
                body = {"name": "workday_password", "value": "s3cret: #1"}
                assert (await c.post("/api/password", json=body)).status_code == 403
                none = {"workday": False, "successfactors": False, "icims": False, "applicantstack": False,
                        "ukg": False, "infor": False, "taleo": False, "email": False}
                assert (await c.get("/api/state", headers=h)).json()["passwords"] == none
                assert (await c.post("/api/password", headers=h, json=body)).json() == {"saved": True}
                after = await c.get("/api/state", headers=h)
                assert after.json()["passwords"] == {**none, "workday": True} and "s3cret" not in after.text
                # the email app password, for reading sign-up codes, is saved the same way, and a
                # new one clears the page's note about the one turned down
                desk.applier.mail_problem = "The desk couldn't read your email: refused."
                mail = {"name": "email_password", "value": "abcd efgh ijkl mnop"}
                assert (await c.post("/api/password", headers=h, json=mail)).json() == {"saved": True}
                after = await c.get("/api/state", headers=h)
                assert after.json()["passwords"]["email"] is True and "abcd efgh" not in after.text
                assert after.json()["mail_problem"] is None
                for bad in ({"name": "profile", "value": "x"}, {"name": "workday_password", "value": "a\nb"}):
                    assert (await c.post("/api/password", headers=h, json=bad)).status_code == 400
        finally:
            await desk.stop()

    run(go())
    assert config.get_secret("workday_password") == "s3cret: #1"


def test_saving_a_password_leaves_the_rest_of_the_file_alone(job_apply_home):
    path = config.secrets_path()
    path.write_text("# career sites\nsuccessfactors_password: keep-me\nworkday_password: old-one\n\nother: 1\n")
    config.save_site_password("workday_password", "new one")
    config.save_site_password("workday_password", "newer one")  # replaced, not added twice
    text = path.read_text()
    assert text.startswith("# career sites\nsuccessfactors_password: keep-me\n")
    assert "old-one" not in text and text.count("workday_password") == 1 and "other: 1" in text
    assert config.get_secret("workday_password") == "newer one"
    assert config.get_secret("successfactors_password") == "keep-me"
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    for name, value in (("../workday_password", "x"), ("workday_password", " "), ("workday_password", "a\rb")):
        with pytest.raises(ValueError):
            config.save_site_password(name, value)


def test_a_hand_typed_secrets_file_doesnt_break_the_page(srv, job_apply_home):
    """A password typed by hand is read as typed, even where YAML would choke on it."""
    config.secrets_path().write_text("workday_password: [unclosed\nnot a line of names\n")
    assert Desk(srv).state()["passwords"] == {"workday": True, "successfactors": False, "icims": False,
                                              "applicantstack": False, "ukg": False, "infor": False,
                                              "taleo": False, "email": False}
    assert config.get_secret("workday_password") == "[unclosed"


def test_pasted_links_are_read_saved_and_listed(srv):
    """A link from LinkedIn, Indeed or a company site joins the list, picked, ready for Apply.
    Pages plain HTTP can't read are read in a background tab, not the one an application is in."""
    posting = fixture_url("site/posting.html")
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            working = await srv.browser.page()  # the tab an application would be in
            await working.goto(fixture_url("generic_form.html"))
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=60, trust_env=False) as c:
                h = {"x-desk-token": desk.token}
                assert (await c.post("/api/add", json={"text": posting})).status_code == 403
                assert (await c.post("/api/add", headers=h, json={"text": "  "})).status_code == 400
                added = (await c.post("/api/add", headers=h, json={"text": f"{posting}\nnot-a-link"})).json()["added"]
                assert added[0]["job_id"] and added[0]["title"].startswith("Field Service Engineer")
                assert added[1] == {"url": "not-a-link", "error": "not a web address"}
                rows = {r["url"]: r for r in (await c.get("/api/state", headers=h)).json()["listings"]}
                assert rows[added[0]["url"]]["added"] and rows[added[0]["url"]]["fit"]["score"] > 0
            assert (await srv.browser.page()).url.endswith("generic_form.html")  # left where it was
        finally:
            await desk.stop()

    run(go())


def test_openings_new_since_the_last_visit_are_tagged(srv):
    from playwright.async_api import async_playwright

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    fit = {"score": 80, "reasons": ["title matches"], "concerns": [], "blocked": False, "recommended": True}
    desk.listings = [{"company": "Example Fab", "title": "Field Service Engineer", "url": "https://example.com/1",
                      "location": "Phoenix, AZ", "fit": fit}]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("text=Field Service Engineer")
                assert await page.locator(".chip.new").count() == 0  # a first visit tags nothing
                desk.listings.append({"company": "Example Litho", "title": "Equipment Technician",
                                      "url": "https://example.com/2", "location": "Chandler, AZ", "fit": fit})
                await page.reload()
                await page.wait_for_selector("text=Equipment Technician")
                tagged = page.locator("li.row", has=page.locator(".chip.new"))
                assert await tagged.count() == 1 and "Equipment Technician" in await tagged.inner_text()
                assert "1 new" in await page.inner_text("#searched")
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_a_typo_in_answers_yaml_doesnt_take_the_desk_down(srv, job_apply_home):
    """answers.yaml invites hand edits. One that breaks the YAML sets the file aside (and
    says so on the page); it isn't overwritten, and the profile still loads."""
    path = job_apply_home / "answers.yaml"
    broken = "answers:\n  - match: relocate\n    answer: [Yes\n"
    path.write_text(broken)
    assert config.saved_answers() == [] and "typo" in config.answers_problem()
    assert config.Profile.load().get("personal.first_name") == "Sam"
    assert "typo" in Desk(srv).state()["answers_problem"]
    with pytest.raises(ValueError, match="typo"):
        config.save_answer("Are you willing to relocate?", "Yes")
    assert path.read_text() == broken  # left as the person wrote it


def test_answers_that_cant_be_remembered_still_go_into_the_application(srv, job_apply_home):
    from job_apply.pipeline import Run, question_key

    (job_apply_home / "answers.yaml").write_text("answers: [\n")
    job = srv.add_job(url="https://example.com/a", title="FSE", company="Example Fab")["job"]
    desk = Desk(srv)
    desk.applier.runs[job["id"]] = Run(job["id"], "FSE", "Example Fab", status="needs_you", need="questions")
    note = desk.answer(job["id"], [{"label": "Are you willing to relocate?", "value": "No"}])
    assert "this application only" in note
    assert desk.applier.runs[job["id"]].once == {question_key("Are you willing to relocate?"): "No"}



def test_the_desk_page_says_account_handling_is_on_until_the_profile_says(srv, job_apply_home):
    """On by default since 0.3.67: the desk page tells a person whose profile doesn't say (an
    install from before), until their profile says either way."""
    from pathlib import Path

    import yaml

    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text())
    assert Desk(srv).state()["settings"]["manage_accounts_chosen"] is True  # the tests' profile turns it off
    profile["settings"].pop("manage_accounts")
    path.write_text(yaml.safe_dump(profile))
    settings = Desk(srv).state()["settings"]
    assert (settings["manage_accounts"], settings["manage_accounts_chosen"]) == (True, False)
    page = (Path(srv.__file__).parent / "static" / "desk.html").read_text(encoding="utf-8")
    assert "s.manage_accounts && !s.manage_accounts_chosen" in page and "manage_accounts: false" in page


def test_two_boxes_with_one_label_keep_their_own_answers(srv, job_apply_home):
    """Workday's Month and Year boxes are both "Date": the card sends each box's own answer
    ("section | label | sub-label"), which is used for this application only, never remembered
    (by its label alone, one answer would go in both boxes, and in every "Date" after)."""
    from job_apply.pipeline import Run, placed_key

    job = srv.add_job(url="https://example.com/a", title="FSE", company="Example Fab")["job"]
    desk = Desk(srv)
    desk.applier.runs[job["id"]] = Run(job["id"], "FSE", "Example Fab", status="needs_you", need="questions")
    assert desk.answer(job["id"], [{"label": "Signature | Date* | Month", "value": "03", "remember": True},
                                   {"label": "Signature | Date* | Year", "value": "2026", "remember": True}]) is None
    month = {"label": "Date*", "section": "Signature", "sublabel": "Month"}
    year = {"label": "Date*", "section": "Signature", "sublabel": "Year"}
    assert desk.applier.runs[job["id"]].once == {placed_key(month): "03", placed_key(year): "2026"}
    assert config.saved_answers() == []


def test_a_remembered_answer_replaces_one_given_for_this_application_only(srv, job_apply_home):
    """The page turned down an answer given for this application only, and the person answers
    again, remembering it: the earlier answer isn't tried again ahead of it."""
    from job_apply.pipeline import Run, question_key

    job = srv.add_job(url="https://example.com/a", title="FSE", company="Example Fab")["job"]
    desk = Desk(srv)
    desk.applier.runs[job["id"]] = Run(job["id"], "FSE", "Example Fab", status="needs_you", need="questions",
                                       once={question_key("Preferred Locale/Language"): "Klingon"})
    assert desk.answer(job["id"], [{"label": "Preferred Locale/Language", "value": "English"}]) is None
    assert desk.applier.runs[job["id"]].once == {}
    assert config.saved_answers()[0]["answer"] == "English"

def test_saving_an_answer_keeps_the_rest_of_answers_yaml(job_apply_home):
    import yaml

    path = job_apply_home / "answers.yaml"
    path.write_text("answers:\n  - question: a note to myself\n  - match: lift\n    answer: 'Yes'\nmine: keep\n")
    config.save_answer("Are you willing to relocate?", "No", "Example Fab")
    data = yaml.safe_load(path.read_text())
    assert data["mine"] == "keep" and {"question": "a note to myself"} in data["answers"]
    assert any(a.get("match") == "lift" for a in data["answers"]) and data["answers"][0]["answer"] == "No"
    assert not path.with_name("answers.yaml.tmp").exists()


def test_the_tailoring_switch_is_kept_and_its_waiting_jobs_listed(srv, job_apply_home):
    """The desk remembers "Tailor my resume for each job", says how many jobs wait for a
    tailored resume, and "Use my usual resume" sends one on without it."""
    from job_apply.pipeline import Run

    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    desk = Desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False) as c:
                r = await c.post("/api/settings", headers=h, json={"tailor_resumes": True})
                assert r.json() == {"auto_submit": False, "tailor_resumes": True}
                await desk.applier.stop()  # hold the queue still while the waiting job is set up
                desk.applier.runs[job["id"]] = Run(job["id"], "FSE", "Example Fab", status="needs_you", need="tailor")
                state = (await c.get("/api/state", headers=h)).json()
                assert state["settings"]["tailor_resumes"] is True and state["tailoring"] == 1
                r = await c.post(f"/api/job/{job['id']}/usual_resume", headers=h)
                assert r.json() == {"ok": True}
                assert desk.applier.runs[job["id"]].usual_resume and desk.applier.runs[job["id"]].status == "queued"
        finally:
            await desk.stop()

    run(go())
    assert json.loads((job_apply_home / "desk.json").read_text())["tailor_resumes"] is True
    assert Desk(srv).applier.tailor is True  # still on next time the desk opens


def test_tailoring_queue_hands_claude_the_jobs_and_the_real_resume(srv, job_apply_home):
    from job_apply import desk as desk_module
    from job_apply.pipeline import Run

    job = srv.add_job(url="https://example.com/jobs/fse", title="Field Service Engineer", company="Example Fab",
                      description="Install and service EUV tools at customer fabs.")["job"]
    (job_apply_home / "Sam_Rivera_Resume.md").write_text("# Sam Rivera\n\n## Experience\n")
    d = desk_module.get_desk(srv)
    d.applier.runs[job["id"]] = Run(job["id"], status="needs_you", need="tailor")
    try:
        out = run(srv.tailoring_queue())
    finally:
        desk_module._desk = None
    assert [j["job_id"] for j in out["jobs"]] == [job["id"]]
    assert "EUV tools" in out["jobs"][0]["description"]
    assert out["base_resume"].startswith("# Sam Rivera") and out["resume_file"].endswith("resume.pdf")
    assert any("Coursework is not a degree" in rule for rule in out["rules"])



def test_the_page_shows_the_plugin_version(srv):
    """So the person can tell an update arrived (the desk's footer)."""
    import json

    manifest = json.loads((config.PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert Desk(srv).state()["version"] == manifest["version"] != ""


def test_a_pasted_posting_that_builds_itself_with_script_is_read_in_the_browser(srv):
    """Plain HTTP got the page but no posting in it (Nikon's UKG pages, TI's): the browser reads it."""
    posting = fixture_url("site/posting.html")
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def thin(url):
        return Posting(url=url, title="", description="Loading...")

    desk.fetch = thin
    out = run(desk.add_links([posting]))
    assert out[0]["job_id"] and out[0]["title"].startswith("Field Service Engineer")
    assert "Maintain and repair semiconductor equipment" in srv.tracker().get(out[0]["job_id"])["description"]


def test_a_secrets_file_that_isnt_a_list_of_names_doesnt_break_the_page(srv, job_apply_home):
    """A hand edit that leaves secrets.yaml a bare line (no "name: value") made every refresh fail."""
    config.secrets_path().write_text("just a line I typed\n")
    assert Desk(srv).state()["passwords"]["workday"] is False
    assert config.get_secret("workday_password") is None


def test_links_past_the_first_twenty_are_handed_back(srv):
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False) as c:
                text = " ".join(f"not-a-link-{i}" for i in range(23))
                out = (await c.post("/api/add", headers={"x-desk-token": desk.token}, json={"text": text})).json()
                assert len(out["added"]) == 20 and out["left"] == ["not-a-link-20", "not-a-link-21", "not-a-link-22"]
        finally:
            await desk.stop()

    run(go())


def test_the_desks_own_files_arent_read_as_postings(srv, job_apply_home):
    """A file:// link into ~/.job-apply (secrets, answers) would be saved as a posting's text,
    where Claude could read it."""
    config.secrets_path().write_text("workday_password: hunter2\n")
    desk = Desk(srv)
    out = run(desk.add_links([config.secrets_path().resolve().as_uri()]))
    assert out[0]["error"] == "that's one of the desk's own files, not a job posting"
    assert "hunter2" not in json.dumps(srv.list_jobs())


def test_a_desk_that_cant_bind_its_port_doesnt_end_the_mcp_server():
    """uvicorn ends a failed start with sys.exit(1): only the desk's task may end."""
    from job_apply.desk import _serve

    class Fails:
        async def serve(self):
            raise SystemExit(1)

    with pytest.raises(RuntimeError, match="stopped"):
        asyncio.run(_serve(Fails()))


@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_the_page_keeps_its_lists_steady(srv):
    """Two listings leading to one job drew its card twice, and the copy grew by one each
    poll; the "Not searched automatically" list closed itself every refresh; a job already
    filled on its review page could be ticked and started over; and a page without the key
    said Claude Code might not be running."""
    from playwright.async_api import async_playwright

    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    job = srv.add_job(url=fixture_url("site/posting.html"), title="Field Service Engineer", company="Example Fab")["job"]
    desk.listings = [{"url": job["url"], "title": "Field Service Engineer", "company": "Example Fab"},
                     {"url": job["url"] + "?utm_source=x", "title": "Field Service Engineer", "company": "Example Fab"}]
    desk.applier.runs[job["id"]] = Run(job["id"], "Field Service Engineer", "Example Fab", status="ready",
                                       reason="Filled and waiting on the review page.")
    desk.search.update(status="done", at=time.time(), browser_only=[{"company": "Example Litho",
                                                                      "careers_url": "https://example.com/jobs"}])

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#skipped-sites summary")
                await page.click("#skipped-sites summary")
                await page.wait_for_timeout(4000)  # two refreshes or more
                assert await page.evaluate("document.querySelector('#skipped-sites details').open")
                assert await page.locator("#done .task").count() == 1  # one card, not one more each poll
                boxes = page.locator("input[data-pick]")
                assert all([await boxes.nth(i).is_disabled() for i in range(await boxes.count())])
                keyless = await browser.new_page()
                await keyless.goto(f"http://127.0.0.1:{desk.port}/")
                await keyless.wait_for_selector("#notices .notice")
                assert "key is missing or out of date" in await keyless.inner_text("#notices")
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def _client(desk):
    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False)


def test_a_job_marked_applied_while_queued_is_never_filled_or_sent(srv):
    """Marked applied while it waits in the queue (on the desk, or by Claude): it leaves the
    queue, and is never opened, filled or, with Submit for me on, sent a second time."""
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.applier.auto_submit = True
    on_desk = srv.add_job(url=fixture_url("site/step1.html"), title="FSE", company="Example Fab")["job"]
    by_claude = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                h = {"x-desk-token": desk.token}
                ids = [on_desk["id"], by_claude["id"]]
                assert (await c.post("/api/apply", headers=h, json={"job_ids": ids, "submit": True})).json()["queued"] == ids
                assert (await c.post(f"/api/job/{on_desk['id']}/applied", headers=h)).json() == {"ok": True}
            srv.update_job(by_claude["id"], status="applied")  # Claude logged it, say from a confirmation email
            for _ in range(2):
                if desk.applier.tasks:
                    await desk.applier._tick()
        finally:
            await desk.stop()

    run(go())
    runs = desk.applier.runs
    assert runs[on_desk["id"]].status == "submitted" and runs[by_claude["id"]].status == "submitted"
    assert "isn't applied to again" in runs[by_claude["id"]].reason
    assert srv.browser._ctx is None  # nothing was opened


def test_ill_typed_requests_are_turned_away_and_change_nothing(srv, job_apply_home):
    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    job = srv.add_job(url="https://example.com/a", title="FSE", company="Example Fab")["job"]
    desk.applier.runs[job["id"]] = Run(job["id"], "FSE", "Example Fab", status="ready")  # not asking anything

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                h = {"x-desk-token": desk.token}
                for path, body in (("/api/apply", b"not json"), ("/api/apply", b"[1, 2]"),
                                   ("/api/apply", b'{"job_ids": "13", "submit": true}'),
                                   ("/api/apply", b'{"job_ids": ["abc"]}'), ("/api/answer", b'{"job_id": null}'),
                                   ("/api/settings", b'"x"'), ("/api/password", b"null")):
                    r = await c.post(path, headers={**h, "content-type": "application/json"}, content=body)
                    assert r.status_code == 400, (path, body, r.status_code, r.text)
                answer = {"job_id": job["id"], "answers": [{"label": "Willing to relocate?", "value": "No"}]}
                assert (await c.post("/api/answer", headers=h, json=answer)).status_code == 400
                assert (await c.post("/api/job/777/skip", headers=h)).status_code == 400
                assert (await c.post("/api/job/99999999999999999999/resume", headers=h)).status_code == 404
                return (await c.get("/api/state", headers=h)).json()
        finally:
            await desk.stop()

    state = run(go())
    assert not desk.applier.tasks  # "13" isn't jobs 1 and 3
    assert 777 not in desk.applier.runs and all(o["job_id"] != 777 for o in state["others"])
    assert config.saved_answers() == []  # nothing remembered for a job that wasn't asking


def test_resume_on_the_desk_tries_a_refused_password_once_more(srv, job_apply_home):
    """The desk's Resume is the person's: a job that stopped pressing a refused saved password gets
    one more press with it (they may have reset the password to it by hand). The queue carrying on
    with the job by itself gets none."""
    from job_apply.pipeline import SIGN_IN_TRIES, Run

    desk = Desk(srv)
    desk.applier.start = lambda: None  # (nothing is driven: only what Resume sets up is looked at)
    job = srv.add_job(url="https://example.com/a", title="FSE", company="Example Corp")["job"]
    paused = Run(job["id"], "FSE", "Example Corp", status="needs_you", need="sign_in", sign_in_tries=SIGN_IN_TRIES + 1)
    desk.applier.runs[job["id"]] = paused

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                r = await c.post(f"/api/job/{job['id']}/resume", headers={"x-desk-token": desk.token})
                assert r.json() == {"ok": True}
        finally:
            await desk.stop()

    run(go())
    assert (paused.status, paused.sign_in_tries) == ("queued", SIGN_IN_TRIES - 1)


def test_a_password_saved_on_the_desk_is_the_one_used(srv, monkeypatch):
    """One set in the environment wins over the file, so saving there is refused, saying why;
    the same email app password saved again after a refusal is tried again."""
    monkeypatch.setenv("JOB_APPLY_SECRET_WORKDAY_PASSWORD", "an-old-one")
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.applier._mail_refused = "abcd efgh ijkl mnop"  # a mail-service hiccup was taken for a refusal

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                h = {"x-desk-token": desk.token}
                env = await c.post("/api/password", headers=h, json={"name": "workday_password", "value": "new"})
                mail = await c.post("/api/password", headers=h, json={"name": "email_password",
                                                                      "value": "abcd efgh ijkl mnop"})
                return env, mail
        finally:
            await desk.stop()

    env, mail = run(go())
    assert env.status_code == 400 and "JOB_APPLY_SECRET_WORKDAY_PASSWORD" in env.json()["error"]
    assert mail.json() == {"saved": True} and desk.applier._mail_refused is None


def test_a_broken_profile_or_saved_search_doesnt_take_the_page_down(srv, job_apply_home):
    (job_apply_home / "profile.yaml").write_text("personal:\n  first_name: Sam\n   last_name: [oops\n")
    (job_apply_home / "recommendations.json").write_text(json.dumps({"results": [{"title": "no address"}, "x"]}))
    state = Desk(srv).state()
    assert "typo" in state["profile_problem"] and state["listings"] == []


def test_the_desks_own_files_arent_queued_either(srv, job_apply_home):
    config.secrets_path().write_text("workday_password: hunter2\n")
    queued, _ = Desk(srv).apply(urls=[config.secrets_path().resolve().as_uri()], job_ids=[], submit=False)
    assert queued == [] and srv.list_jobs()["jobs"] == []


def test_report_a_problem_shows_the_report_with_a_link_to_file_it(srv, job_apply_home):
    """The report was shown in a confirm() and GitHub opened with window.open after the wait,
    which browsers block as a pop-up; a submitted job had no Report button at all, and a
    double click wrote two reports."""
    from playwright.async_api import async_playwright

    from job_apply.pipeline import Run

    desk = Desk(srv)
    job = srv.add_job(url="https://example.com/a", title="Technician", company="Example Litho")["job"]
    desk.applier.runs[job["id"]] = Run(job["id"], "Technician", "Example Litho", status="submitted",
                                       reason="submitted", log=["opened the posting", "pressed “Submit”"])
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                await page.goto(desk.url)
                button = page.locator(f"button[data-job='{job['id']}'][data-job-act='report']")
                await button.dblclick()
                await page.wait_for_selector("#report[open]")
                assert "Technician at Example Litho" in await page.locator("#report-text").inner_text()
                href = await page.locator("#report-open").get_attribute("href")
                assert href.startswith("https://github.com/sebob2491/Jobs/issues/new?")
                await page.click("[data-act=report-close]")
                assert await page.locator("#report[open]").count() == 0
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert len(list((job_apply_home / "reports").iterdir())) == 1


def test_report_a_problem_can_leave_out_which_job_it_was(srv, job_apply_home):
    """The report named the job: the employer in the issue's title, the job and its address in its
    text. The dialog's box makes it again with the job system alone, and clearing it names it again."""
    from urllib.parse import parse_qs, urlsplit

    from playwright.async_api import async_playwright

    from job_apply.pipeline import Run

    desk = Desk(srv)
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/x", title="Technician",
                      company="Example Litho")["job"]
    desk.applier.runs[job["id"]] = Run(job["id"], "Technician", "Example Litho", status="failed",
                                       reason="Something went wrong on Example Litho's site.")
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.click(f"button[data-job='{job['id']}'][data-job-act='report']")
                await page.wait_for_selector("#report[open]")
                named = await page.locator("#report-text").inner_text()
                await page.check("#report-anonymous")
                await page.wait_for_function("() => !document.querySelector('#report-text').textContent.includes('Litho')")
                anonymous = await page.locator("#report-text").inner_text()
                title = parse_qs(urlsplit(await page.locator("#report-open").get_attribute("href")).query)["title"]
                await page.uncheck("#report-anonymous")
                await page.wait_for_function("() => document.querySelector('#report-text').textContent.includes('Litho')")
                await browser.close()
                return named, anonymous, title
        finally:
            await desk.stop()

    named, anonymous, title = run(go())
    assert "Technician at Example Litho" in named and "acme.wd1" in named
    assert "**Job:** a Workday employer" in anonymous and title == ["Report: a Workday employer, failed"]
    assert "Something went wrong on the employer's site." in anonymous
    assert not any(word in anonymous for word in ("Technician", "Example Litho", "acme"))


def test_the_report_box_goes_back_when_the_report_cant_be_made_again(srv, job_apply_home, monkeypatch):
    """Ticking "Don't say which job it was" makes the report again. When that failed, the box stayed
    ticked over the report that names the job, with its link to file it: the person would file a
    public issue naming the job, thinking it didn't. The box goes back to say what the report shown
    does, with that report's link, whether it was ticked or cleared. While one is being made, there's
    no link."""
    import threading

    from playwright.async_api import async_playwright

    from job_apply import report
    from job_apply.pipeline import Run

    build, failing, looked = report.build, set(), threading.Event()
    looked.set()

    def flaky(job, run=None, profile=None, anonymous=False):
        looked.wait(10)  # (until the page has been looked at while the report is made)
        if anonymous in failing:
            raise OSError("No space left on device")
        return build(job, run, profile, anonymous)

    monkeypatch.setattr(report, "build", flaky)
    desk = Desk(srv)
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/x", title="Technician",
                      company="Example Litho")["job"]
    desk.applier.runs[job["id"]] = Run(job["id"], "Technician", "Example Litho", status="failed",
                                       reason="Something went wrong on Example Litho's site.")
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                alerts, links = [], []

                async def dismiss(dialog):
                    alerts.append(dialog.message)
                    await dialog.dismiss()
                page.on("dialog", dismiss)
                await page.goto(desk.url)
                await page.click(f"button[data-job='{job['id']}'][data-job-act='report']")
                await page.wait_for_selector("#report[open]")

                async def toggle():  # tick or clear the box, and wait for the report it asks for
                    looked.clear()
                    async with page.expect_response(lambda r: r.url.endswith(f"/api/job/{job['id']}/report")):
                        await page.click("#report-anonymous")
                        links.append(await page.locator("#report-open").get_attribute("href"))
                        looked.set()
                    await page.wait_for_function("() => !document.querySelector('#report-anonymous').disabled")
                    return (await page.is_checked("#report-anonymous"),
                            await page.locator("#report-open").get_attribute("href"),
                            await page.locator("#report-text").inner_text())

                named = await page.locator("#report-open").get_attribute("href")
                failing.add(True)
                seen = [await toggle()]  # can't leave the job out
                failing.clear()
                seen.append(await toggle())  # leaves it out
                failing.add(False)
                seen.append(await toggle())  # can't name it again
                await browser.close()
                return named, seen, alerts, links
        finally:
            await desk.stop()

    named, seen, alerts, links = run(go())
    (ticked, href, text), (anonymous, left_out, _), (still, href_after, text_after) = seen
    assert not ticked and href == named and "Example Litho" in text  # the box says the report names the job
    assert anonymous and "Litho" not in left_out
    assert still and href_after == left_out and "Litho" not in href_after + text_after
    assert len(alerts) == 2 and links == [None, None, None]


def test_a_report_that_cant_be_made_doesnt_bring_back_another_jobs(srv, job_apply_home, monkeypatch):
    """The dialog went back to the report it showed last when one couldn't be made, but that was
    kept for the page, not the job: when job B's report failed, the dialog took back job A's, with
    its box and its link, and Open would file a public issue about A. A job's report starts with
    nothing of another's; one that can't be made leaves no link, the box unticked and no text."""
    from playwright.async_api import async_playwright

    from job_apply import report
    from job_apply.pipeline import Run

    build = report.build
    desk = Desk(srv)
    a = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/a", title="Technician",
                    company="Example Litho")["job"]
    b = srv.add_job(url="https://example.com/jobs/b", title="Operator", company="Example Corp")["job"]

    def failing(job, run=None, profile=None, anonymous=False):
        if job["id"] == b["id"]:
            raise OSError("No space left on device")
        return build(job, run, profile, anonymous)

    monkeypatch.setattr(report, "build", failing)
    for job in (a, b):
        desk.applier.runs[job["id"]] = Run(job["id"], job["title"], job["company"], status="failed",
                                           reason="Something went wrong.")
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                alerts = []

                async def dismiss(dialog):
                    alerts.append(dialog.message)
                    await dialog.dismiss()
                page.on("dialog", dismiss)
                await page.goto(desk.url)
                ready = "() => !document.querySelector('#report-anonymous').disabled"
                await page.click(f"button[data-job='{a['id']}'][data-job-act='report']")
                await page.wait_for_selector("#report[open]")
                async with page.expect_response(lambda r: r.url.endswith(f"/api/job/{a['id']}/report")):
                    await page.check("#report-anonymous")  # A's report, leaving the job out
                await page.wait_for_function(ready)
                a_link = await page.locator("#report-open").get_attribute("href")
                await page.click("[data-act=report-close]")
                async with page.expect_response(lambda r: r.url.endswith(f"/api/job/{b['id']}/report")):
                    await page.click(f"button[data-job='{b['id']}'][data-job-act='report']")
                await page.wait_for_function(ready)
                after = (await page.locator("#report[open]").count(), await page.is_checked("#report-anonymous"),
                         await page.locator("#report-open").get_attribute("href"),
                         await page.locator("#report-text").text_content(),
                         await page.locator("#report-where").text_content())
                await browser.close()
                return a_link, after, alerts
        finally:
            await desk.stop()

    a_link, after, alerts = run(go())
    assert a_link and a_link.startswith("https://github.com/")
    assert len(alerts) == 1  # (that it couldn't be made)
    assert after == (0, False, None, "", "")  # nothing of A's, nor a link to file it


def test_the_desk_reads_an_icims_posting_in_its_browser(srv, monkeypatch):
    """iCIMS postings turn away plain requests: Find jobs reads one in a background tab, from
    the frame its posting is drawn in (in_iframe=1). Other sites' postings aren't read that way."""
    from job_apply.postings import FetchError

    asked = []

    async def frames_html(url, inner=None):
        asked.append((url, inner))
        outer = ("<html><head><title>Careers at Example</title></head><body><nav>" + "Jobs | Locations | Benefits | " * 20
                 + "</nav><iframe id='icims_content_iframe'></iframe></body></html>")  # long, but no posting
        return [outer, "<html><head><title>Entry Level Recruiter</title></head><body><div class='iCIMS_JobContent'>"
                "<h1>Entry Level Recruiter</h1>"
                + "<p>Recruit and place contract talent for clients across the Phoenix area every day.</p>" * 6
                + "<a href='/jobs/13516/login?in_iframe=1'>Apply for this job online</a></div></body></html>"]

    monkeypatch.setattr(srv.browser, "frames_html", frames_html)
    desk = Desk(srv)
    p = run(desk.read_in_browser("https://careers-aco.icims.com/jobs/13516/entry-level-recruiter/job#top"))
    assert "Recruit and place contract talent" in p.description and "Benefits" not in p.description
    assert p.apply_url == "https://careers-aco.icims.com/jobs/13516/entry-level-recruiter/job#top"  # not its framed sign-in
    assert asked == [("https://careers-aco.icims.com/jobs/13516/entry-level-recruiter/job?in_iframe=1",
                      "#icims_content_iframe")]  # (in its query, not after its #)

    async def outer_only(url, inner=None):
        return ["<html><head><title>Careers</title></head><body>" + "Jobs | Benefits | " * 40 + "</body></html>"]

    monkeypatch.setattr(srv.browser, "frames_html", outer_only)
    with pytest.raises(FetchError):  # the frame never drew its posting: no reading, not the page around it
        run(desk.read_in_browser("https://careers-aco.icims.com/jobs/1/x/job"))
    with pytest.raises(FetchError):
        run(desk.read_in_browser("https://boards.greenhouse.io/aco/jobs/1"))


def test_the_password_list_names_the_persons_own_employers(job_apply_home):
    """The desk page named only the semiconductor list's employers by each job system ("Workday:
    Intel, Applied, KLA and 10 more"), whatever lists a person searches. Someone on the Phoenix
    list sees its employers, their own first, and it's read again when their file changes."""
    from job_apply import desk as desk_module

    desk_module._systems = None
    own = job_apply_home / "companies.yaml"
    own.write_text("lists: [phoenix-metro]\ncompanies:\n"
                   "  - {name: Example Credit Union, careers_url: 'https://e.example', ats: ukg}\n")
    systems = {s["value"]: s["label"] for s in desk_module.password_systems()}
    assert systems["workday"].startswith("Workday: Banner Health, HonorHealth, Arizona State University and "), systems
    assert systems["ukg"] == "UKG Pro: Example Credit Union, Desert Financial Credit Union"
    assert systems["applicantstack"] == "ApplicantStack"  # no employer on these lists uses it
    assert systems["taleo"] == "Taleo: Kforce"  # (its own address, where the saved Taleo password goes too)
    own.write_text("lists: [phoenix-metro]\ncompanies: []\n")
    os.utime(own, (time.time() + 5, time.time() + 5))  # a later change than the first write
    assert {s["value"]: s["label"] for s in desk_module.password_systems()}["ukg"] == \
        "UKG Pro: Desert Financial Credit Union"
    own.write_text("lists: [phoenix-metro\n")  # a typo: the systems without names, the page still up
    os.utime(own, (time.time() + 10, time.time() + 10))
    assert {s["value"]: s["label"] for s in desk_module.password_systems()}["workday"] == "Workday"


def _note(job_id, reason="I couldn't find the button that moves this application on.", log=()):
    """A note the desk took on a job's stop (as Applier._pause takes them)."""
    from job_apply import report
    from job_apply.pipeline import Run

    job = {"id": job_id, "title": "Technician", "company": "Example Litho", "ats": "workday"}
    return report.take_note(job, Run(job_id, "Technician", "Example Litho", status="needs_you", need="stuck",
                                     reason=reason, log=list(log)))


def test_the_notes_are_counted_in_the_header_shown_as_one_issue_and_cleared(srv, job_apply_home):
    """Notes the desk took on its stops: a Notes (N) link in the header once there are any, a
    panel with all of them as the one issue they make, and Clear once they're filed."""
    from urllib.parse import parse_qs, urlsplit

    from playwright.async_api import async_playwright

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page()
                page.on("dialog", lambda d: asyncio.ensure_future(d.accept()))  # Clear's "are you sure"
                await page.goto(desk.url)
                await page.wait_for_selector("#plugin-version:not(:empty)")  # the page has its state
                assert await page.locator("#notes-btn").is_hidden()  # none yet
                for i in range(3):
                    _note(i + 1, reason=f"stop {i}")
                await page.wait_for_selector("#notes-btn:text('Notes (3)')")
                await page.click("#notes-btn")
                await page.wait_for_selector("#notes[open]")
                text = await page.locator("#notes-text").inner_text()
                assert text.count("## stuck: Technician, at a Workday employer") == 3 and "Example Litho" not in text
                href = await page.locator("#notes-open").get_attribute("href")
                assert href.startswith("https://github.com/sebob2491/Jobs/issues/new?")
                query = parse_qs(urlsplit(href).query)
                assert query["title"] == [f"Live-run notes: 3 stops (job-apply {config.plugin_version()})"]
                assert query["body"][0] == text.strip() + "\n" and await page.locator("#notes-cut").is_hidden()
                await page.click("[data-act=notes-clear]")
                await page.wait_for_selector("#notes-btn", state="hidden")
                assert await page.locator("#notes[open]").count() == 0
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert list((job_apply_home / "notes").glob("*.md")) == []


def test_many_notes_make_one_issue_whose_address_github_takes(srv, job_apply_home):
    """Fifty notes with long logs: the issue's address stays under GitHub's limit, cut with a
    line saying the rest is in Copy all's text, which holds every note. Only the newest 50 are
    kept; the notes need the page's key, and Clear removes only the ones shown."""
    from urllib.parse import parse_qs, urlsplit

    from job_apply import report

    for i in range(55):
        _note(i + 1, reason=f"stop {i}", log=[f"clicked “Next” on page {n}: " + "x" * 200 for n in range(20)])
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                h = {"x-desk-token": desk.token}
                assert (await c.post("/api/notes/show")).status_code == 403
                assert (await c.post("/api/notes/clear", json={"ids": []})).status_code == 403
                assert (await c.post("/api/notes/clear", headers=h, json={"ids": "all"})).status_code == 400
                assert (await c.post("/api/notes/everything", headers=h)).status_code == 404
                shown = (await c.post("/api/notes/show", headers=h)).json()
                _note(99, reason="taken after they were shown")
                ids = [n["id"] for n in shown["notes"]]
                cleared = (await c.post("/api/notes/clear", headers=h, json={"ids": ids})).json()
                return shown, cleared, (await c.get("/api/state", headers=h)).json()["notes"]
        finally:
            await desk.stop()

    shown, cleared, left = run(go())
    assert len(shown["notes"]) == report.NOTES == 50 and shown["text"].count("## stuck: Technician") == 50
    assert "said:** stop 4\n" not in shown["text"] and "said:** stop 5\n" in shown["text"]  # the oldest went
    assert len(shown["issue_url"]) <= report.ISSUE_URL and shown["cut"]
    body = parse_qs(urlsplit(shown["issue_url"]).query)["body"][0]
    assert body.endswith("(cut short: the rest is in the text the Job Desk's Copy all copies)\n")
    # the note taken since stays (the oldest shown had made way for it: 50 at most)
    assert cleared == {"cleared": 49} and left == 1


@pytest.mark.skipif(not browser_available(), reason="no browser")
@pytest.mark.parametrize("width", [1280, 390])
def test_the_desk_page_fits_its_window(srv, width):
    """The Site passwords card's job-system list was as wide as its longest choice ("SuccessFactors:
    TSMC Arizona, ..."), and nothing let it shrink: the page ran past a 1280-wide window, and a
    phone's, and scrolled sideways."""
    from playwright.async_api import async_playwright

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page(viewport={"width": width, "height": 900})
                await page.goto(desk.url)
                await page.wait_for_selector("#pw-site option", state="attached")
                sizes = await page.evaluate("() => [document.documentElement.scrollWidth, "
                                            "document.documentElement.clientWidth]")
                await browser.close()
                return sizes
        finally:
            await desk.stop()

    scroll, client = run(go())
    assert scroll <= client, (scroll, client)


@pytest.mark.parametrize("width", [1280, 390])
def test_undo_on_the_page_puts_a_skipped_job_back(srv, width):
    """Skip couldn't be undone. After Skip, the job's card (and its row in the list) offers Undo:
    a job the desk had begun goes back in the queue, with the tracker's status from before; one
    skipped before the desk restarted goes back to the list, unticked. The page still fits a phone."""
    from playwright.async_api import async_playwright

    from job_apply.pipeline import Run

    fit = {"score": 80, "reasons": ["title matches"], "concerns": [], "blocked": False, "recommended": True}
    begun = srv.add_job(url=fixture_url("site/posting.html"), title="Field Service Engineer", company="Example Fab")["job"]
    srv.update_job(begun["id"], status="in_progress")
    earlier = srv.add_job(url="https://example.com/jobs/earlier", title="Equipment Technician", company="Example Litho")["job"]
    srv.update_job(earlier["id"], status="skipped")  # skipped before the desk was restarted: no run
    desk = Desk(srv)
    desk.applier.start = lambda: None  # (the queue isn't worked: where Undo puts the job is what's looked at)
    desk.search.update(status="done", at=time.time())
    desk.listings = [{"url": j["url"], "title": j["title"], "company": j["company"], "fit": fit} for j in (begun, earlier)]
    desk.applier.runs[begun["id"]] = Run(begun["id"], "Field Service Engineer", "Example Fab", status="needs_you",
                                         need="sign_in", reason="Sign in on Example Fab's site.")
    undo = "button[data-job-act=unskip]"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(**launch_options())
                page = await browser.new_page(viewport={"width": width, "height": 900})
                await page.goto(desk.url)
                await page.click(f"#needs button[data-job='{begun['id']}'][data-job-act=skip]")
                await page.wait_for_selector(f"#done {undo}[data-job='{begun['id']}']")
                assert desk.applier.runs[begun["id"]].status == "skipped"
                assert srv.get_job(begun["id"])["job"]["status"] == "skipped"
                assert await page.locator(f"#rows {undo}").count() == 2  # both skipped rows offer it too
                sizes = await page.evaluate("() => [document.documentElement.scrollWidth, "
                                            "document.documentElement.clientWidth]")
                assert sizes[0] <= sizes[1], sizes

                await page.click(f"#done {undo}[data-job='{begun['id']}']")
                await page.wait_for_selector(f"#done {undo}", state="detached")
                row = page.locator("li.row", has_text="Field Service Engineer")
                await row.locator(".pill:text('Queued')").wait_for()
                assert desk.applier.runs[begun["id"]].status == "queued"
                assert [j for _, j in desk.applier.tasks] == [begun["id"]]
                assert srv.get_job(begun["id"])["job"]["status"] == "in_progress"

                other = page.locator("li.row", has_text="Equipment Technician")
                await other.locator(undo).click()
                await other.locator(f"{undo}").wait_for(state="detached")
                box = other.locator("input[type=checkbox]")
                assert await box.is_enabled() and not await box.is_checked()
                assert srv.get_job(earlier["id"])["job"]["status"] == "saved" and earlier["id"] not in desk.applier.runs
                assert [j for _, j in desk.applier.tasks] == [begun["id"]]
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_a_skipped_job_isnt_applied_to_until_undone_and_undo_elsewhere_changes_nothing(srv):
    """Through the desk's API: Apply and Resume leave a skipped job out (a stale page, another
    window); Undo on a job that isn't skipped, or on none, changes nothing; one marked applied and then
    skipped goes back to applied and is never queued again."""
    desk = Desk(srv)
    desk.applier.start = lambda: None
    ids = [srv.add_job(url=f"https://example.com/jobs/{n}", title=f"Job {n}", company="Example Co")["job"]["id"]
           for n in range(3)]
    a, b, sent = ids

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                h = {"x-desk-token": desk.token}

                async def job(job_id, action):
                    return await c.post(f"/api/job/{job_id}/{action}", headers=h)

                assert (await c.post("/api/apply", headers=h, json={"job_ids": ids})).json()["queued"] == ids
                assert (await job(sent, "applied")).json() == {"ok": True}
                assert (await job(b, "skip")).json() == {"ok": True}
                out = (await c.post("/api/apply", headers=h, json={"job_ids": [b], "submit": True})).json()
                assert out == {"queued": [], "already_applied": ["Job 1"]}
                refused = await job(b, "resume")
                assert refused.status_code == 400 and "skipped" in refused.json()["error"]
                assert [j for _, j in desk.applier.tasks] == [a]

                history = srv.get_job(a)["history"]
                assert (await job(a, "unskip")).json() == {"ok": True}  # not skipped: nothing changes
                assert [j for _, j in desk.applier.tasks] == [a] and desk.applier.runs[a].status == "queued"
                assert srv.get_job(a)["history"] == history
                assert (await job(777, "unskip")).status_code == 400

                assert (await job(sent, "skip")).json() == {"ok": True}
                assert (await job(sent, "unskip")).json() == {"ok": True}
                assert (await job(b, "unskip")).json() == {"ok": True}
        finally:
            await desk.stop()

    run(go())
    assert [j for _, j in desk.applier.tasks] == [a, b]
    assert desk.applier.runs[b].status == "queued" and srv.get_job(b)["job"]["status"] == "saved"
    assert desk.applier.runs[sent].status == "submitted" and srv.get_job(sent)["job"]["status"] == "applied"
