"""The Job Desk's local web API, driven the way its page drives it."""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import browser_available, fixture_url, person_browser, run

import job_apply.pipeline as pipeline
from job_apply import autofill, config
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
            # (its address carries the page's key: never sent on to a site a link opens, the review of Oct 15)
            assert '<meta name="referrer" content="no-referrer">' in page.text

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
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("text=Field Service Engineer")
                assert await page.locator("text=Accountant").count() == 0  # not recommended: hidden by default
                where = page.locator(f"span[title='{many}']")
                assert await where.inner_text() == "Phoenix, AZ; Chandler, AZ; Austin, TX and 2 more"
                meta = await page.locator(".meta").first.inner_text()
                assert "2026-08-25" in meta and "T13:11" not in meta  # the date, not the time of day
                await page.click("text=Select recommended")
                said: list[str] = []  # what will stop them, said first (the common questions unanswered here)
                page.once("dialog", lambda d: (said.append(d.message), asyncio.ensure_future(d.accept())))
                await page.click("#apply")
                for _ in range(50):
                    if desk.applier.tasks:
                        break
                    await asyncio.sleep(0.1)
                queued = [jid for _, jid in desk.applier.tasks]
                assert [srv.tracker().get(j)["title"] for j in queued] == ["Field Service Engineer"]
                assert said and said[0].startswith("Before these go:") and "common question" in said[0], said

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
            async with person_browser() as browser:
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
            async with person_browser() as browser:
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


INDEED_JOB = "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b"
INTEL_JOB = "https://intel.wd1.myworkdayjobs.com/External/job/US-Arizona-Chandler/Equipment-Technician_JR0286553"


def test_a_posting_from_indeed_and_the_employers_site_is_one_row_also_on_indeed(srv):
    """The code review's worth building 8: the same job saved from Indeed (by Claude) and found on the employer's site
    (Find jobs) was two rows, and Apply could send two applications for it. It's one row, the employer's posting, which
    says it's also on Indeed (Indeed's posting a link, in a tab of its own); its Apply makes them one job, applied for
    on the employer's site."""
    saved = srv.add_job(url=INDEED_JOB, title="Equipment Technician", company="Intel Corporation",
                        location="Chandler, AZ", source="indeed")["job"]
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    fit = {"score": 80, "reasons": ["title matches"], "concerns": [], "blocked": False, "recommended": True}
    other = "https://careers.example-fab.example/jobs/2"
    desk.listings = [{"url": INTEL_JOB, "title": "Equipment Technician", "company": "Intel",
                      "location": "US-Arizona-Chandler", "ats": "workday", "fit": fit},
                     {"url": other, "title": "Field Service Engineer", "company": "Example Fab",
                      "location": "Phoenix, AZ", "fit": fit}]
    rows = desk.state()["listings"]
    assert [r["url"] for r in rows] == [INTEL_JOB, other]  # (the Indeed job isn't listed again)
    assert rows[0]["job_id"] == saved["id"] and rows[0]["also_on"] == [{"site": "Indeed", "url": INDEED_JOB}]
    assert rows[1]["job_id"] is None and rows[1]["also_on"] == []
    assert desk.apply(urls=[INTEL_JOB], job_ids=[], submit=False) == ([saved["id"]], [])
    job = srv.tracker().get(saved["id"])
    assert (job["url"], job["ats"], len(srv.tracker().list())) == (INTEL_JOB, "workday", 1)
    rows = desk.state()["listings"]
    assert [(r["url"], r["job_id"]) for r in rows] == [(INTEL_JOB, saved["id"]), (other, None)]
    assert rows[0]["also_on"] == [{"site": "Indeed", "url": INDEED_JOB}]
    # applied to (on the employer's site): Indeed's posting, pasted or found again, is never queued
    srv.update_job(saved["id"], status="applied")
    assert desk.apply(urls=[INDEED_JOB], job_ids=[], submit=True) == ([], ["Equipment Technician"])

    async def go():
        srv.update_job(saved["id"], status="saved")
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                row = page.locator("li.row", has_text="Equipment Technician")
                await row.wait_for()
                also = row.locator("a", has_text="Also on Indeed")
                assert await also.get_attribute("href") == INDEED_JOB
                assert await also.get_attribute("target") == "_blank"
                assert await page.locator("li.row").count() == 2
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_a_pasted_indeed_link_for_a_job_saved_from_the_employers_site_makes_no_new_one(srv):
    """Indeed's link to a job the tracker has from the employer's site, pasted on the desk: that job, ready for Apply on
    the employer's site, Indeed's link kept as its alias. Never a second job, which Apply would send in again."""
    own = srv.add_job(url=INTEL_JOB, title="Equipment Technician (JR0286553)", company="Intel",
                      location="Chandler, AZ")["job"]
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def fetched(url):
        return Posting(url=url, title="Equipment Technician", company="Intel Corporation", location="Chandler, Arizona",
                       description="Maintain 300mm tools. " * 20, ats="indeed", source="indeed")

    desk.fetch = fetched
    added = run(desk.add_links([INDEED_JOB + "&from=serp"]))
    assert [(a["job_id"], a["url"]) for a in added] == [(own["id"], INTEL_JOB)]
    jobs = srv.tracker().list()
    assert [(j["id"], j["url"], j["apply_url"]) for j in jobs] == [(own["id"], INTEL_JOB, "")]
    assert srv.tracker().find_by_url(INDEED_JOB)["id"] == own["id"]
    assert [r["job_id"] for r in desk.state()["listings"]] == [own["id"]]


def test_the_desk_searches_by_itself_only_with_its_switch_on_and_a_search_can_be_stopped(srv, monkeypatch):
    """Issue #112, note 44: the page searched by itself whenever the openings were 12 hours old, with no setting, and
    Pause queue didn't stop it. It searches only when Find jobs is pressed, unless "Search by itself every 12 hours"
    is on (off by default), and then not while the queue is paused. A search running is said in the header (by
    itself, or pressed), with Stop search beside it: the search ends, and the openings found before it stay."""
    searched: list[str] = []
    gate: dict[str, asyncio.Event] = {}

    async def fake_search(query, companies=None, location=None, limit_per_company=10):
        searched.append(query)
        await gate["open"].wait()  # (a search taking its time, as the real one does)
        return {"results": [], "errors": {}, "browser_only": []}

    monkeypatch.setattr(srv, "search_company_jobs", fake_search)
    desk = Desk(srv)
    desk.applier.start = lambda: None
    assert desk.auto_search is False  # (off unless the person turns it on)

    async def go():
        gate["open"] = asyncio.Event()
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)  # never searched before: the old page searched on opening
                await page.wait_for_selector("text=Press Find jobs")
                await asyncio.sleep(3.5)  # (two of the page's refreshes)
                assert not searched and desk.search["status"] == "idle"
                assert not await page.is_checked("#auto-search") and await page.is_hidden("#stop-search")
                # turned on while the queue is paused: still nothing
                desk.applier.paused = True
                await page.check("#auto-search")
                await asyncio.sleep(3.5)
                assert desk.auto_search is True and not searched
                # the queue resumed: the page searches by itself, and says so, with Stop search beside it
                desk.applier.paused = False
                await page.wait_for_selector("#stop-search:not([hidden])")
                assert desk.search["auto"] is True
                assert "Searching by itself" in await page.inner_text("#searched")
                assert await page.is_disabled("#find")
                n = len(searched)
                await page.click("#stop-search")
                await page.wait_for_selector("#stop-search", state="hidden")
                assert desk.search["status"] == "idle" and desk.search["stopped"]
                assert "Search stopped" in await page.inner_text("#searched")
                await asyncio.sleep(3.5)
                assert len(searched) == n  # (not started again by itself)
                # Find jobs pressed: a search said as pressed, which finishes
                await page.click("#find")
                await page.wait_for_selector("#stop-search:not([hidden])")
                assert desk.search["auto"] is False
                assert (await page.inner_text("#searched")).startswith("Searching, started")
                gate["open"].set()
                await page.wait_for_selector("#stop-search", state="hidden")
                assert desk.search["status"] == "done"
                assert "Search stopped" not in await page.inner_text("#searched")
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    saved = json.loads((srv.config.home() / "desk.json").read_text(encoding="utf-8"))
    assert saved["auto_search"] is True and Desk(srv).auto_search is True  # (kept for next time)


def test_a_search_asked_by_the_page_itself_needs_the_switch_and_a_running_queue(srv, monkeypatch):
    """The server's side of note 44: a search the page asks for by itself ({"auto": true}) starts only with the
    switch on and the queue not paused; Find jobs (no "auto") always does. Stop search keeps the openings before."""
    gate: dict[str, asyncio.Event] = {}

    async def fake_search(query, companies=None, location=None, limit_per_company=10):
        await gate["open"].wait()
        return {"results": [], "errors": {}, "browser_only": []}

    monkeypatch.setattr(srv, "search_company_jobs", fake_search)
    desk = Desk(srv)
    desk.applier.start = lambda: None
    before = time.time() - 13 * 3600
    desk.search.update(status="done", at=before)
    desk.listings = [{"url": "https://example.com/job/1", "title": "Technician", "company": "Example Fab"}]

    async def go():
        gate["open"] = asyncio.Event()
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False) as c:
                assert (await c.post("/api/search/stop", json={})).status_code == 403
                assert (await c.post("/api/search", headers=h, json={"auto": True})).json() == {"started": False}
                assert (await c.post("/api/search/stop", headers=h, json={})).json() == {"stopped": False}
                assert (await c.post("/api/settings", headers=h, json={"auto_search": True})).json()["auto_search"]
                desk.applier.paused = True
                assert (await c.post("/api/search", headers=h, json={"auto": True})).json() == {"started": False}
                desk.applier.paused = False
                assert (await c.post("/api/search", headers=h, json={"auto": True})).json() == {"started": True}
                await asyncio.sleep(0.2)
                state = (await c.get("/api/state", headers=h)).json()
                assert state["search"]["status"] == "running" and state["search"]["auto"] is True
                assert state["settings"]["auto_search"] is True
                assert (await c.post("/api/search/stop", headers=h, json={})).json() == {"stopped": True}
                state = (await c.get("/api/state", headers=h)).json()
                # the openings found before stay, with when they were found
                assert state["search"]["status"] == "done" and state["search"]["at"] == before
                assert state["search"]["stopped"] and [r["url"] for r in state["listings"]] == ["https://example.com/job/1"]
                # Find jobs needs no switch
                assert (await c.post("/api/settings", headers=h, json={"auto_search": False})).json()["auto_search"] is False
                assert (await c.post("/api/search", headers=h, json={})).json() == {"started": True}
                await asyncio.sleep(0.2)
                assert desk.search["status"] == "running" and desk.search["auto"] is False
                assert desk.search["stopped"] is None
                gate["open"].set()
                for _ in range(50):
                    if desk.search["status"] == "done":
                        break
                    await asyncio.sleep(0.1)
                assert desk.search["status"] == "done" and desk.search["at"] > before
        finally:
            await desk.stop()

    run(go())


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
    path.write_text("# career sites\nsuccessfactors_password: keep-me\nworkday_password: old-one\n\nother: 1\n", encoding="utf-8")
    config.save_site_password("workday_password", "new one")
    config.save_site_password("workday_password", "newer one")  # replaced, not added twice
    text = path.read_text(encoding="utf-8")
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
    config.secrets_path().write_text("workday_password: [unclosed\nnot a line of names\n", encoding="utf-8")
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
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    fit = {"score": 80, "reasons": ["title matches"], "concerns": [], "blocked": False, "recommended": True}
    desk.listings = [{"company": "Example Fab", "title": "Field Service Engineer", "url": "https://example.com/1",
                      "location": "Phoenix, AZ", "fit": fit}]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
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
    path.write_text(broken, encoding="utf-8")
    assert config.saved_answers() == [] and "typo" in config.answers_problem()
    assert config.Profile.load().get("personal.first_name") == "Sam"
    assert "typo" in Desk(srv).state()["answers_problem"]
    with pytest.raises(ValueError, match="typo"):
        config.save_answer("Are you willing to relocate?", "Yes")
    assert path.read_text(encoding="utf-8") == broken  # left as the person wrote it


def test_answers_that_cant_be_remembered_still_go_into_the_application(srv, job_apply_home):
    from job_apply.pipeline import Run, question_key

    (job_apply_home / "answers.yaml").write_text("answers: [\n", encoding="utf-8")
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
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert Desk(srv).state()["settings"]["manage_accounts_chosen"] is True  # the tests' profile turns it off
    profile["settings"].pop("manage_accounts")
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
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


def test_an_answer_naming_the_employer_is_remembered_as_company_website_or_not_at_all(srv, job_apply_home):
    """The card's "Acme website" (issue #112, note 27) is Acme's own, which no other employer's list has: how the
    person heard of the job is remembered as "Company Website", which each list words as its own employer's site;
    another answer naming the employer is used for this application only, and the person is told."""
    from job_apply.pipeline import Run, question_key

    job = srv.add_job(url="https://example.com/a", title="FSE", company="Acme Technologies Inc")["job"]
    desk = Desk(srv)

    def answer(*pairs):
        desk.applier.runs[job["id"]] = Run(job["id"], "FSE", "Acme Technologies Inc", status="needs_you", need="questions")
        return desk.answer(job["id"], [{"label": label, "value": value} for label, value in pairs])

    assert answer(("How Did You Hear About Us?*", "Acme website"), ("Preferred shift", "Nights")) is None
    assert {a["question"]: a["answer"] for a in config.saved_answers()} == {
        "How Did You Hear About Us?*": "Company Website", "Preferred shift": "Nights"}
    # (this application gets the choice as picked: the review of note 27)
    assert desk.applier.runs[job["id"]].once == {"how did you hear about us": "Acme website"}
    note = answer(("Which of our teams would you like to join?", "The Acme field team"))
    assert "Acme field team" in note and "this application only" in note
    assert desk.applier.runs[job["id"]].once == {question_key("Which of our teams would you like to join?"):
                                                 "The Acme field team"}
    assert len(config.saved_answers()) == 2
    # a source that isn't the employer's site stays as it was said
    assert answer(("How Did You Hear About Us?*", "LinkedIn")) is None
    assert config.saved_answers()[0]["answer"] == "LinkedIn"

def test_saving_an_answer_keeps_the_rest_of_answers_yaml(job_apply_home):
    import yaml

    path = job_apply_home / "answers.yaml"
    path.write_text("answers:\n  - question: a note to myself\n  - match: lift\n    answer: 'Yes'\nmine: keep\n", encoding="utf-8")
    config.save_answer("Are you willing to relocate?", "No", "Example Fab")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
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
                assert r.json() == {"auto_submit": False, "tailor_resumes": True, "handled_only": False, "troubleshoot": False,
                                    "auto_search": False, "jobs_at_once": 1}  # (the tests' profile: one at a time)
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
    assert json.loads((job_apply_home / "desk.json").read_text(encoding="utf-8"))["tailor_resumes"] is True
    assert Desk(srv).applier.tailor is True  # still on next time the desk opens


def test_the_queue_is_paused_and_resumed_through_the_api(srv):
    """Pause queue and Resume queue (issue #112, note 36): behind the page's key like the rest, an unknown action is
    no action, and the state says whether the queue is paused. Not one of the settings: kept with the queue."""
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                assert (await c.post("/api/queue/pause")).status_code == 403
                assert (await c.post("/api/queue/everything", headers=h)).status_code == 404
                assert not desk.applier.paused and (await c.get("/api/state", headers=h)).json()["paused"] is False
                r = await c.post("/api/queue/pause", headers=h)
                assert r.json() == {"paused": True} and desk.applier.paused
                assert (await c.get("/api/state", headers=h)).json()["paused"] is True
                r = await c.post("/api/queue/resume", headers=h)
                assert r.json() == {"paused": False} and not desk.applier.paused
                assert (await c.get("/api/state", headers=h)).json()["paused"] is False
        finally:
            await desk.stop()

    run(go())
    assert Desk(srv).applier.paused is False  # (resumed: a new desk starts with the queue going)


def test_the_pause_button_pauses_and_resumes_the_queue(srv):
    """The header's Pause queue shows while jobs are queued or running, turns into Resume queue (with how many wait)
    once pressed, and the Working on card says the queue is paused (issue #112, note 36)."""
    from job_apply.pipeline import Run

    job = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    desk = Desk(srv)
    desk.applier.start = lambda: None  # the queue isn't worked in this test
    desk.search.update(status="done", at=time.time())  # recent, so opening the page doesn't search the real sites

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#plugin-version:not(:empty)")  # the page has its state
                assert await page.locator("#pause-btn").is_hidden()  # nothing queued or running
                desk.applier.runs[job["id"]] = Run(job["id"], "Technician", "Example Litho", status="queued")
                desk.applier.tasks.append(("apply", job["id"]))
                await page.wait_for_selector("#pause-btn:visible:text-is('Pause queue')")
                await page.click("#pause-btn")
                await page.wait_for_selector("#pause-btn:text-is('Resume queue (1 waiting)')")
                assert desk.applier.paused
                assert "Paused: 1 waiting" in await page.locator("#work").inner_text()
                await page.click("#pause-btn")
                await page.wait_for_selector("#pause-btn:text-is('Pause queue')")
                assert not desk.applier.paused
                assert "Paused" not in await page.locator("#work").inner_text()
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_jobs_at_once_is_set_on_the_desk_kept_and_given_way_to_a_newer_profile_setting(srv, job_apply_home):
    """The desk's Jobs at once (1 to 3) is profile.yaml's settings.jobs_at_once until it's pressed: then it's kept in
    desk.json as the other switches are, and the queue takes it at once. A later change to profile.yaml is the newer
    choice. Anything but 1, 2 or 3 is turned away and changes nothing."""
    import yaml

    desk = Desk(srv)
    desk.applier.start = lambda: None
    woken = []
    desk.applier.wake = lambda: woken.append(True)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with _client(desk) as c:
                assert (await c.get("/api/state", headers=h)).json()["settings"]["jobs_at_once"] == 1  # the profile's
                for bad in (0, 4, "2", True, None, 2.0):
                    r = await c.post("/api/settings", headers=h, json={"jobs_at_once": bad})
                    assert r.status_code == 400, (bad, r.text)
                assert desk.applier.at_once() == 1 and not woken
                r = await c.post("/api/settings", headers=h, json={"jobs_at_once": 3})
                assert r.json()["jobs_at_once"] == 3 and desk.applier.at_once() == 3 and woken
                assert (await c.get("/api/state", headers=h)).json()["settings"]["jobs_at_once"] == 3
        finally:
            await desk.stop()

    run(go())
    assert json.loads((job_apply_home / "desk.json").read_text(encoding="utf-8"))["jobs_at_once"] == 3
    assert Desk(srv).applier.at_once() == 3  # still so next time the desk opens
    profile = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    profile["settings"]["jobs_at_once"] = 2  # changed in profile.yaml since (by Claude, say)
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    assert Desk(srv).applier.at_once() == 2


def test_jobs_at_once_chosen_on_the_desk_outlasts_a_profile_that_doesnt_read_and_the_codes_default(srv, job_apply_home):
    """The review of jobs at once (LOW): the desk kept profile.yaml's setting as it read then, the code's default where
    the profile didn't say (so a new default would have dropped every desk's choice); a profile.yaml that didn't read
    as the desk opened dropped the choice, and the next switch saved wrote nothing over it. The profile's own words are
    kept with the choice (none where it doesn't say), a profile that doesn't read keeps it, and with no choice the
    queue goes one job at a time until profile.yaml reads again."""
    import yaml

    profile = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    del profile["settings"]["jobs_at_once"]  # (as most profiles have it: the default)
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def settings(d, body):
        await d.start(port=0, open_browser=False)
        try:
            async with _client(d) as c:
                r = await c.post("/api/settings", headers={"x-desk-token": d.token}, json=body)
                assert r.status_code == 200, r.text
        finally:
            await d.stop()

    run(settings(desk, {"jobs_at_once": 3}))
    saved = json.loads((job_apply_home / "desk.json").read_text(encoding="utf-8"))
    assert (saved["jobs_at_once"], saved["jobs_at_once_profile"]) == (3, None), saved
    good = (job_apply_home / "profile.yaml").read_text(encoding="utf-8")
    (job_apply_home / "profile.yaml").write_text("personal:\n  email: sam@example.com\n   phone: [oops\n",
                                                 encoding="utf-8")
    broken = Desk(srv)
    broken.applier.start = lambda: None
    assert broken.applier.at_once() == 3  # (kept: whether it changed can't be told)
    run(settings(broken, {"tailor_resumes": True}))  # another switch saved meanwhile
    saved = json.loads((job_apply_home / "desk.json").read_text(encoding="utf-8"))
    assert (saved["jobs_at_once"], saved["jobs_at_once_profile"]) == (3, None), saved
    from job_apply.pipeline import Applier
    assert Applier(srv).at_once() == 1  # (no desk choice, no profile to read: one at a time)
    (job_apply_home / "profile.yaml").write_text(good, encoding="utf-8")
    assert Desk(srv).applier.at_once() == 3


def test_the_working_on_card_shows_every_job_under_way_and_the_jobs_at_once_buttons_set_it(srv):
    """With jobs filled at once, Working on lists each one under way, and how many more are queued; the Jobs at once
    buttons beside Submit for me show the setting and change it."""
    from job_apply.pipeline import Run

    ids = [srv.add_job(url=f"https://careers.employer{n}.example/job/{n}", title=f"Technician {n}",
                       company=f"Employer {n}")["job"]["id"] for n in range(3)]
    desk = Desk(srv)
    desk.applier.start = lambda: None  # the queue isn't worked in this test
    desk.search.update(status="done", at=time.time())  # recent, so opening the page doesn't search the real sites

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#plugin-version:not(:empty)")  # the page has its state
                await page.wait_for_selector("[data-at-once='1'][aria-pressed='true']")  # (the tests' profile)
                for n, job_id in enumerate(ids):
                    run_ = desk.applier.runs[job_id] = Run(job_id, f"Technician {n}", f"Employer {n}",
                                                           status="running" if n < 2 else "queued")
                    run_.log.append(f"opened https://careers.employer{n}.example/job/{n}")
                desk.applier.tasks.append(("apply", ids[2]))
                await page.wait_for_selector("#work :text('1 more queued.')")
                work = await page.locator("#work").inner_text()
                assert "Technician 0" in work and "Technician 1" in work and "Technician 2" not in work, work
                await page.click("[data-at-once='3']")
                await page.wait_for_selector("[data-at-once='3'][aria-pressed='true']")
                for _ in range(50):
                    if desk.applier.jobs_at_once == 3:
                        break
                    await asyncio.sleep(0.1)
                assert desk.applier.jobs_at_once == 3
                await page.reload()
                await page.wait_for_selector("[data-at-once='3'][aria-pressed='true']")
                assert await page.locator("[data-at-once][aria-pressed='true']").count() == 1
        finally:
            await desk.stop()

    run(go())


def test_only_where_the_desk_goes_all_the_way_shows_those_employers_alone_and_is_kept(srv, job_apply_home):
    """The owner (Oct 12): a switch to show only the openings where the plugin works all the way. The employers
    are the live check's (data/desk_handled.yaml: a test applicant reached the review page there); their openings
    carry a chip, the switch hides the rest (and drops them from what's picked), and it's kept for next time."""
    from job_apply import config

    assert "intel" in config.handled_employers() and "example fab" not in config.handled_employers()
    fit = {"score": 80, "reasons": [], "concerns": [], "blocked": False, "recommended": True}
    desk = Desk(srv)
    desk.listings = [{"company": "Intel", "title": "Module Equipment Technician", "url": "https://example.com/intel",
                      "location": "Phoenix, AZ", "fit": fit},
                     {"company": "Example Fab", "title": "Field Service Engineer", "url": "https://example.com/fab",
                      "location": "Phoenix, AZ", "fit": fit}]
    desk.applier.start = lambda: None  # the queue isn't worked in this test
    desk.search.update(status="done", at=time.time())  # recent, so opening the page doesn't search the real sites

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("text=Field Service Engineer")
                row = page.locator("li.row", has_text="Module Equipment Technician")
                assert await row.locator(".chip.handled").count() == 1
                assert await page.locator("li.row", has_text="Field Service Engineer").locator(".chip.handled").count() == 0
                await page.click("text=Select recommended")
                await page.check("#handled-only")
                await page.wait_for_selector("text=Field Service Engineer", state="detached")
                assert await page.locator("text=Module Equipment Technician").count() == 1
                assert await page.locator("#apply").inner_text() == "Apply to 1 selected"
                # (the review's) a link the person adds shows, whatever its employer, and nothing hidden is applied to
                srv.add_job(url="https://example.com/p", title="Pasted Technician", company="lamresearch")  # (a pasted link)
                desk.listings.append({"company": "Example Fab", "title": "Hidden Tech", "url": "https://example.com/h",
                                      "location": "Phoenix, AZ", "fit": fit})
                await page.wait_for_selector("text=Pasted Technician")
                assert await page.locator("text=Hidden Tech").count() == 0
                await page.click("text=Select recommended")
                picked = await page.evaluate("() => [...selected].filter(selectable)")
                assert "https://example.com/h" not in picked and "https://example.com/intel" in picked, picked
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert json.loads((job_apply_home / "desk.json").read_text(encoding="utf-8"))["handled_only"] is True
    assert Desk(srv).handled_only is True  # still on next time the desk opens


def test_troubleshooting_searches_every_list_shows_where_the_desk_stops_and_sends_nothing(srv, job_apply_home, monkeypatch):
    """The owner (Oct 12), to get the desk going at more employers: a Troubleshooting switch. Find jobs
    searches every list's employers (the person's own file names only one) for the profile's titles and HR ones;
    the page shows only the openings where the desk doesn't go all the way yet, whatever their fit; and nothing is
    sent while it's on (Submit is refused, Submit for me held). It turns "Only where the desk goes all the way" off."""
    from job_apply import desk as desk_module
    from job_apply.pipeline import Run

    (job_apply_home / "companies.yaml").write_text("lists: [semiconductor-az]\ncompanies: []\n", encoding="utf-8")
    searched = []

    async def search_company_jobs(query, location=None, limit_per_company=20, every_list=False):
        searched.append((query, every_list))
        return {"results": [], "errors": {}, "browser_only": []}
    monkeypatch.setattr(srv, "search_company_jobs", search_company_jobs, raising=False)
    low = {"score": 20, "reasons": [], "concerns": [], "blocked": False, "recommended": False}
    desk = Desk(srv)
    desk.listings = [{"company": "Intel", "title": "Module Equipment Technician", "url": "https://example.com/intel",
                      "location": "Phoenix, AZ", "fit": low},
                     {"company": "Example Health", "title": "HR Generalist", "url": "https://example.com/hr",
                      "location": "Phoenix, AZ", "fit": low}]
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    job = srv.add_job(url="https://example.com/ready", title="Recruiter", company="Example Health")["job"]
    desk.applier.runs[job["id"]] = Run(job["id"], "Recruiter", "Example Health", status="ready")

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.check("#handled-only")
                await page.wait_for_selector("text=HR Generalist", state="detached")
                listed = list(desk.listings)
                await page.check("#troubleshoot")
                # the other way's openings are cleared; Find jobs searches again, troubleshooting's way
                await page.click("#find")
                for _ in range(100):
                    if searched and desk.search["status"] == "done":
                        break
                    await page.wait_for_timeout(100)
                assert searched and searched[0][1] is True, searched
                desk.listings = listed  # (as troubleshooting's search would find them)
                await page.wait_for_selector("text=HR Generalist")  # (a low fit, shown all the same)
                assert await page.locator("text=Module Equipment Technician").count() == 0  # (the desk goes all the way)
                assert not await page.is_checked("#handled-only")
                assert "Troubleshooting is on: no application is submitted" in await page.inner_text("#notices")
                await browser.close()
            h = {"x-desk-token": desk.token}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False) as c:
                r = await c.post(f"/api/job/{job['id']}/submit", headers=h)
                assert r.status_code >= 400 and "Troubleshooting is on" in r.text, r.text
                # (the review's) Submit for me isn't carried by Apply, Claude's own Submit is refused too, and a
                # Submit queued before troubleshooting waits on its review page for when it's off
                other = srv.add_job(url="https://example.com/other", title="Recruiter", company="Example Clinic")["job"]
                r = await c.post("/api/apply", headers=h, json={"job_ids": [other["id"]], "submit": True})
                assert other["id"] in r.json()["queued"] and not desk.applier.runs[other["id"]].submit
                monkeypatch.setattr(desk_module, "_desk", desk)
                said = await srv.submit_application(job_id=job["id"], user_confirmed=True)
                assert said["submitted"] is False and "Troubleshooting is on" in said["reason"], said
                held = desk.applier.runs[job["id"]]
                await desk.applier._submit(held)
                assert held.status == "ready" and "turn troubleshooting off" in held.reason, held.reason
            await desk.find_jobs()
        finally:
            await desk.stop()

    run(go())
    assert desk.applier.runs[job["id"]].status == "ready"  # nothing sent
    assert len(searched) == 2 and all(every for _, every in searched), searched  # (pressed on the page, and Claude's)
    assert "human resources" in searched[0][0] and "recruiter" in searched[0][0], searched
    saved = json.loads((job_apply_home / "desk.json").read_text(encoding="utf-8"))
    assert saved["troubleshoot"] is True and saved["handled_only"] is False
    assert Desk(srv).applier.hold_submits is True  # still on next time the desk opens
    assert desk_module.troubleshoot_query("field service | equipment technician | customer engineer") == \
        "field service | equipment technician | human resources | recruiter"


def test_every_employer_is_the_persons_and_every_list(job_apply_home):
    from job_apply.search import employer_lists, every_employer, load_companies

    (job_apply_home / "companies.yaml").write_text("lists: [semiconductor-az]\ncompanies: []\n", encoding="utf-8")
    names = {c["name"] for c in every_employer()}
    assert {c["name"] for c in load_companies()} < names
    from job_apply.search import _companies_in
    for path in employer_lists().values():
        assert {c["name"] for c in _companies_in(path)[0]} <= names


def test_tailoring_queue_hands_claude_the_jobs_and_the_real_resume(srv, job_apply_home):
    from job_apply import desk as desk_module
    from job_apply.pipeline import Run

    job = srv.add_job(url="https://example.com/jobs/fse", title="Field Service Engineer", company="Example Fab",
                      description="Install and service EUV tools at customer fabs.")["job"]
    (job_apply_home / "Sam_Rivera_Resume.md").write_text("# Sam Rivera\n\n## Experience\n", encoding="utf-8")
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
    # (the tailoring test of Oct 12: the posting's words leaked in, scope widened, the older job put first)
    assert any("only where the resume says the same thing" in rule for rule in out["rules"])
    assert any("Never widen" in rule for rule in out["rules"]) and any("newest first" in r for r in out["rules"])



def test_the_page_shows_the_plugin_version(srv):
    """So the person can tell an update arrived (the desk's footer)."""
    import json

    manifest = json.loads((config.PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
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


def test_a_pasted_link_on_a_listed_employers_site_takes_the_lists_name(srv):
    """A link pasted on the desk kept the posting's own name for its employer, where Claude's ingest_job takes the
    list's (Workday's legal entity, "100 Intel Corporation", named the job's folder and missed the lists by name)."""
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def fetched(url):
        return Posting(url=url, title="Module Equipment Technician", company="100 Intel Corporation",
                       description="Maintain tools. " * 20, parse_method="workday-api")

    desk.fetch = fetched
    intel = "https://intel.wd1.myworkdayjobs.com/External/job/US-Arizona-Phoenix/Module-Equipment-Technician_JR0286553"
    other = "https://acme.wd1.myworkdayjobs.com/External/job/Phoenix-AZ/Technician_R1"
    assert [o["company"] for o in run(desk.add_links([intel, other]))] == ["Intel", "100 Intel Corporation"]


def test_apply_says_first_what_will_stop_the_ticked_jobs(srv, job_apply_home):
    """The review of Oct 15 (worth building, 1): a batch of 30 ticked jobs went off, and each one on a system with no
    saved password stopped at its sign-in, each whose site mailed a code waited without the inbox, and each common
    question unanswered stopped the first job asking it. Before Apply, the desk says what will stop them (the job
    systems with no password, with how many of the ticked jobs; the inbox; the common questions), and nothing
    when nothing will."""
    desk = Desk(srv)
    desk.listings = [{"url": "https://acme.wd1.myworkdayjobs.com/External/job/1", "ats": "workday", "title": "Tech"},
                     {"url": "https://beta.wd5.myworkdayjobs.com/External/job/2", "ats": "workday", "title": "Tech"},
                     {"url": "https://careers.example.com/job/3/", "ats": "successfactors", "title": "Tech"},
                     {"url": "https://boards.greenhouse.io/x/jobs/4", "ats": "greenhouse", "title": "Tech"}]
    urls = [r["url"] for r in desk.listings]
    lines = desk.preflight(urls)
    assert lines[0].startswith("Workday: no password saved, so 2 of these jobs stop at the sign-in"), lines
    assert lines[1].startswith("SuccessFactors: no password saved, so 1 of these jobs stops"), lines
    assert any("email app password" in x for x in lines) and any("common question" in x for x in lines), lines
    assert not any("Greenhouse" in x for x in lines)
    config.secrets_path().write_text("workday_password: a\nsuccessfactors_password: b\nemail_password: c\n",
                                     encoding="utf-8")
    lines = desk.preflight(urls)
    assert len(lines) == 1 and "common question" in lines[0], lines
    for q in autofill.common_questions(config.Profile.load()):
        config.save_bank_answer(q["id"], (q.get("choices") or ["Yes"])[0] if q.get("kind") == "choice" else "Ask me")
    assert desk.preflight(urls) == []
    assert desk.preflight([urls[3]]) == []  # (a Greenhouse job: no account, no code)


def test_a_secrets_file_that_isnt_a_list_of_names_doesnt_break_the_page(srv, job_apply_home):
    """A hand edit that leaves secrets.yaml a bare line (no "name: value") made every refresh fail."""
    config.secrets_path().write_text("just a line I typed\n", encoding="utf-8")
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
    config.secrets_path().write_text("workday_password: hunter2\n", encoding="utf-8")
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
            async with person_browser() as browser:
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
    (job_apply_home / "profile.yaml").write_text("personal:\n  first_name: Sam\n   last_name: [oops\n", encoding="utf-8")
    (job_apply_home / "recommendations.json").write_text(json.dumps({"results": [{"title": "no address"}, "x"]}), encoding="utf-8")
    state = Desk(srv).state()
    assert "typo" in state["profile_problem"] and state["listings"] == []


def test_the_desks_own_files_arent_queued_either(srv, job_apply_home):
    config.secrets_path().write_text("workday_password: hunter2\n", encoding="utf-8")
    queued, _ = Desk(srv).apply(urls=[config.secrets_path().resolve().as_uri()], job_ids=[], submit=False)
    assert queued == [] and srv.list_jobs()["jobs"] == []


def test_report_a_problem_shows_the_report_with_a_link_to_file_it(srv, job_apply_home):
    """The report was shown in a confirm() and GitHub opened with window.open after the wait,
    which browsers block as a pop-up; a submitted job had no Report button at all, and a
    double click wrote two reports."""
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
            async with person_browser() as browser:
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
            async with person_browser() as browser:
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
            async with person_browser() as browser:
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
            async with person_browser() as browser:
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
                   "  - {name: Example Credit Union, careers_url: 'https://e.example', ats: ukg}\n", encoding="utf-8")
    systems = {s["value"]: s["label"] for s in desk_module.password_systems()}
    assert systems["workday"].startswith("Workday: Banner Health, HonorHealth, Arizona State University and "), systems
    assert systems["ukg"] == "UKG Pro: Example Credit Union, Desert Financial Credit Union"
    assert systems["applicantstack"] == "ApplicantStack"  # no employer on these lists uses it
    assert systems["taleo"] == "Taleo: Kforce"  # (its own address, where the saved Taleo password goes too)
    own.write_text("lists: [phoenix-metro]\ncompanies: []\n", encoding="utf-8")
    os.utime(own, (time.time() + 5, time.time() + 5))  # a later change than the first write
    assert {s["value"]: s["label"] for s in desk_module.password_systems()}["ukg"] == \
        "UKG Pro: Desert Financial Credit Union"
    own.write_text("lists: [phoenix-metro\n", encoding="utf-8")  # a typo: the systems without names, the page still up
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


    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
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

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
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
            async with person_browser() as browser:
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


def test_the_page_lists_my_applications_folds_an_idle_side_and_closes_a_tip(srv, monkeypatch):
    """My applications lists each job sent in with where it stands; with nothing needing the person, being
    worked on or done, one short card stands for the three; a tip closed stays closed; and an opening
    without a location doesn't say so."""

    for i, (company, status) in enumerate([("Intel", "applied"), ("ASML", "rejected"), ("KLA", "saved")]):
        job = srv.add_job(url=f"https://example.com/jobs/{i}", title="Technician", company=company)["job"]
        srv.update_job(job["id"], status=status)
    monkeypatch.setattr(config.Profile, "profile_gaps", lambda self: ["background: government"])
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    assert sorted((a["company"], a["status"]) for a in desk.state()["applications"]) == [("ASML", "rejected"),
                                                                                          ("Intel", "applied")]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#idle-card:not([hidden])")
                assert await page.locator("#needs").is_hidden() and await page.locator("#work").is_hidden()
                assert "location not given" not in await page.inner_text("#rows")
                assert "(2)" in await page.inner_text("[data-filter=mine]")
                await page.click("[data-filter=mine]")
                rows = await page.inner_text("#rows")
                assert "Intel" in rows and "Applied" in rows and "Rejected" in rows and "KLA" not in rows, rows
                assert await page.locator("#apply").is_hidden()
                assert "Fewer stops" in await page.inner_text("#notices")
                await page.click("[data-act=dismiss]")
                await page.reload()
                await page.wait_for_selector("#idle-card:not([hidden])")
                await page.wait_for_timeout(500)
                assert "Fewer stops" not in await page.inner_text("#notices")
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_the_common_questions_are_asked_once_on_the_page_and_kept(srv, job_apply_home):
    """The question bank's card: each unanswered question asked; a choice made is saved to answers.yaml with the
    bank's pattern at once, and the card counts it; a choice that isn't one of its own is turned away."""

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    bank = desk.state()["bank"]
    assert bank and all("match" not in q for q in bank)
    assert "heard_about" not in [q["id"] for q in bank]  # (the tests' profile says how it heard of jobs)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                r = await c.post("/api/bank", json={"id": "relocate", "answer": "Maybe"},
                                 headers={"x-desk-token": desk.token})
                assert r.status_code == 400
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#bank-card:not([hidden]) select[data-bank=shift]")
                await page.select_option("select[data-bank=shift]", "Nights")
                await page.wait_for_function("document.getElementById('bank-count').textContent.startsWith('1 of')")
                assert await page.eval_on_selector("#bank-given select", "s => s.value") == "Nights"
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert {q["id"]: q["answer"] for q in config.question_bank()}["shift"] == "Nights"


def test_the_bot_checks_waiting_are_solved_in_one_round(srv):
    """Bot checks wait in their tabs while the queue goes on (the owner's call, Oct 11). The desk says how many wait;
    "Solve them" brings up the oldest one's tab, and the next one's as soon as it's past its check. The person
    solves each: the desk only shows them."""
    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    jobs = [srv.add_job(url=f"https://example.com/job/{i}", title="Technician", company=c)["job"]
            for i, c in enumerate(["Example Fab", "Example Litho"])]
    runs = []
    for i, job in enumerate(jobs):
        r = Run(job["id"], "Technician", job["company"], status="needs_you", need="bot_check", left=True,
                reason="The site is checking that you're a person.")
        r.paused_at = 1000.0 + i
        desk.applier.runs[job["id"]] = r
        runs.append(r)
    shown = []

    async def focus(job_id):
        shown.append(job_id)
        return True
    desk.applier.focus = focus
    rounds = []
    desk.applier.start_round = rounds.append  # (the worker holds while the person is at one)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#notices [data-act=bot-round]")
                assert "2 bot checks to solve" in await page.inner_text("#notices")
                await page.click("#notices [data-act=bot-round]")
                await page.wait_for_function("document.title !== ''")
                for _ in range(40):
                    if shown:
                        break
                    await page.wait_for_timeout(100)
                assert shown == [jobs[0]["id"]], shown
                runs[0].status, runs[0].need = "running", ""  # the person solved it: the desk carries on with it
                for _ in range(60):
                    if len(shown) > 1:
                        break
                    await page.wait_for_timeout(100)
                assert shown == [jobs[0]["id"], jobs[1]["id"]] and rounds == shown, (shown, rounds)
                ended = []
                desk.applier.end_round = lambda: ended.append(True)
                runs[1].status, runs[1].need = "running", ""  # none left: the round ends, and the queue goes on
                for _ in range(60):
                    if ended:
                        break
                    await page.wait_for_timeout(100)
                assert ended
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_the_applications_ready_for_the_persons_submit_are_reviewed_in_one_round(srv):
    """For a batch (the owner's mega apply, Oct 12): "Review them" brings each application filled to the person's
    Submit up in turn; "Submit this one" sends the one shown (their press), "I submitted it" says they pressed the
    site's own last button, and the next comes up once it's gone. Nothing is sent without the person's press."""
    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    jobs = [srv.add_job(url=f"https://example.com/job/{i}", title="Technician", company=c)["job"]
            for i, c in enumerate(["Example Fab", "Example Litho"])]
    first = Run(jobs[0]["id"], "Technician", "Example Fab", status="ready", reason="Filled and waiting on the review page.")
    second = Run(jobs[1]["id"], "Technician", "Example Litho", status="needs_you", need="your_submit",
                 reason="Its send button reads Apply.")
    first.paused_at, second.paused_at = 1000.0, 1001.0
    desk.applier.runs.update({jobs[0]["id"]: first, jobs[1]["id"]: second})
    shown, sent, said = [], [], []

    async def focus(job_id):
        shown.append(job_id)
        return True
    desk.applier.focus = focus
    desk.applier.start_round = lambda job_id: None

    def submit_now(job_id):
        sent.append(job_id)
        first.status = "queued"
        return first
    desk.applier.submit_now = submit_now
    desk.applier.mark_applied = lambda job_id: (said.append(job_id), setattr(second, "status", "submitted"))

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#notices [data-act=review-round]")
                assert "2 applications ready for your Submit" in await page.inner_text("#notices")
                assert not sent and not said  # (nothing sent before the person's press)
                await page.click("#notices [data-act=review-round]")
                await page.wait_for_selector("#notices [data-act=review-submit]")
                for _ in range(40):
                    if shown:
                        break
                    await page.wait_for_timeout(100)
                assert shown == [jobs[0]["id"]], shown
                await page.click("#notices [data-act=review-submit]")
                # (its own "I submitted it", the site's last button pressed: the round is on the second)
                await page.wait_for_selector("#notices :text('Reviewing Technician (Example Litho)')")
                for _ in range(40):  # (the desk's focus is awaited after the words show: the review of Oct 15, under load)
                    if len(shown) >= 2:
                        break
                    await page.wait_for_timeout(100)
                assert sent == [jobs[0]["id"]] and shown == [jobs[0]["id"], jobs[1]["id"]], (sent, shown)
                await page.click("#notices [data-act=review-applied]")
                for _ in range(40):
                    if said:
                        break
                    await page.wait_for_timeout(100)
                assert said == [jobs[1]["id"]]
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_jobs_stopped_on_a_bot_check_or_captcha_are_marked_and_a_forms_captcha_is_reviewed_in_the_round(srv):
    """The owner (Oct 12): which jobs stopped on a bot check or CAPTCHA? Each says so on its card, and the
    filled form with a CAPTCHA before its Submit comes up in the "Review them" round with the others ready
    to send: the person ticks it and presses "Submit this one" (their press; the desk never solves one)."""
    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    names = ["Example Fab", "Example Litho", "Example Etch", "Example Optics"]
    jobs = [srv.add_job(url=f"https://example.com/job/{i}", title="Technician", company=c)["job"]
            for i, c in enumerate(names)]
    shapes = [dict(need="bot_check", left=True), dict(need="sign_in", captcha=True),
              dict(need="captcha", captcha=True), dict(need="questions")]
    for i, (job, name, shape) in enumerate(zip(jobs, names, shapes)):
        r = Run(job["id"], "Technician", name, status="needs_you", reason="Waiting.", **shape)
        r.paused_at = 1000.0 + i
        desk.applier.runs[job["id"]] = r
    shown, sent = [], []

    async def focus(job_id):
        shown.append(job_id)
        return True
    desk.applier.focus = focus
    desk.applier.start_round = lambda job_id: None
    desk.applier.submit_now = lambda job_id: (sent.append(job_id), desk.applier.runs[job_id])[1]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#needs .task h3 .pill")
                marks = await page.eval_on_selector_all("#needs .task h3", "hs => hs.map((h) => h.innerText)")
                marked = {n: m for m in marks for n in names if n in m}
                assert "Bot check" in marked["Example Fab"] and "CAPTCHA" in marked["Example Litho"], marks
                assert "CAPTCHA" in marked["Example Etch"], marks
                assert "CAPTCHA" not in marked["Example Optics"] and "Bot check" not in marked["Example Optics"], marks
                assert "1 application ready for your Submit (1 with a CAPTCHA to tick first)" in await page.inner_text("#notices")
                assert not sent
                await page.click("#notices [data-act=review-round]")
                await page.wait_for_selector("#notices [data-act=review-submit]")
                assert shown == [jobs[2]["id"]], shown
                assert "Tick its CAPTCHA" in await page.inner_text("#notices")
                # (or ticked, and the site's own Submit pressed: theirs to say, on its card and in the round)
                assert await page.locator("#notices [data-act=review-applied]").count() == 1
                assert await page.locator(f"#needs [data-job='{jobs[2]['id']}'][data-job-act=applied]").count() == 1
                await page.click("#notices [data-act=review-submit]")
                for _ in range(40):
                    if sent:
                        break
                    await page.wait_for_timeout(50)
                assert sent == [jobs[2]["id"]], sent
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_a_question_asked_for_want_of_the_profiles_choice_isnt_remembered_by_default(srv):
    """A self-identification list without the profile's answer among its choices (a gender list with no decline) is
    asked about, and what the person picks there is for that site: remembered, it would go in at every later site,
    where the profile's decline is a choice (the review of 0.3.138). Its "Remember" box starts unticked; another
    question's starts ticked."""
    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    job = srv.add_job(url="https://example.com/job/1", title="Technician", company="Example Fab")["job"]
    desk.applier.runs[job["id"]] = Run(job["id"], "Technician", "Example Fab", status="needs_you", need="questions",
                                       reason="Waiting.", questions=[
        {"id": "g", "kind": "select", "label": "Gender", "required": True, "options": ["Male", "Female"],
         "unmatched": "Decline to self-identify", "error": "your profile's answer isn't one of its choices"},
        {"id": "c", "kind": "select", "label": "Do you have a clean room certification?", "required": True,
         "options": ["Yes", "No"]}])

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("[data-remember]")
                ticked = {label: await page.locator(f'[data-remember="{label}"]').is_checked()
                          for label in ("Gender", "Do you have a clean room certification?")}
                await browser.close()
                return ticked
        finally:
            await desk.stop()

    assert run(go()) == {"Gender": False, "Do you have a clean room certification?": True}


def test_a_box_whose_words_say_little_shows_the_words_above_it_and_isnt_remembered_by_default(srv):
    """Taylor Morrison's Workday "Acknowledgment*" (runs 71 and 73): what it acknowledges and certifies is the site's
    words above it, now on its card. Its "Remember" box starts unticked: remembered, the answer would go in at every
    later "Acknowledgment", whatever that one says (and "Acknowledgment" was missed as a consent, spelled without the
    e)."""
    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    job = srv.add_job(url="https://example.com/job/1", title="Coordinator", company="Example Homes")["job"]
    said = "By checking the box below, I acknowledge the Privacy Notice <and> certify I shared it."
    desk.applier.runs[job["id"]] = Run(job["id"], "Coordinator", "Example Homes", status="needs_you", need="questions",
                                       reason="Waiting.", questions=[
        {"id": "a", "kind": "checkbox", "label": "Acknowledgment*", "required": True, "section": "Terms and Conditions",
         "about": said},
        {"id": "y", "kind": "checkbox", "label": "Yes", "required": True, "about": "I have a forklift license."},
        {"id": "c", "kind": "checkbox", "label": "Are you 18 or older?", "required": True},
        # (and a date's Day asked alone, its Month and Year typed in the browser: answered for its own box, never
        # remembered by the date's label, which would put the day in every part: the review of 0.3.159)
        {"id": "d", "kind": "text", "label": "Start date*", "sublabel": "Day", "section": "Questions", "required": True,
         "role": "spinbutton"},
        {"id": "g", "kind": "text", "label": "Graduation", "sublabel": "Year", "required": True}])  # (a box, not a date's)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("[data-remember]")
                ticked = {label: await page.locator(f'[data-remember="{label}"]').is_checked()
                          for label in ("Acknowledgment*", "Yes", "Are you 18 or older?")}
                card = await page.locator("#needs").inner_text()
                day = (await page.locator('[data-q="Questions | Start date* | Day"]').count(),
                       await page.locator('[data-remember="Start date*"]').count(),
                       await page.locator('[data-remember="Graduation"]').count())
                await browser.close()
                return ticked, card, day
        finally:
            await desk.stop()

    ticked, card, day = run(go())
    assert ticked == {"Acknowledgment*": False, "Yes": False, "Are you 18 or older?": True}, ticked
    assert f"“{said}”" in card and "“I have a forklift license.”" in card, card
    assert day == (1, 0, 1), day


def test_an_account_forms_security_questions_on_the_card_go_into_that_form_alone(srv, job_apply_home):
    """BrassRing's Create Account (Edward Jones, runs 67 to 77) asks for a security question and its answer, the
    person's to choose. On the card they say they're for that account only, with no "Remember" box; answered, they're
    kept apart for that form (Run.account_answers): never remembered, never among the application's answers (which go
    into every box with its label), never on the desk page. The application's own questions beside them are as ever."""
    from job_apply.pipeline import Run, placed_key, question_key

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    job = srv.add_job(url="https://example.com/job/1", title="Analyst", company="Example Corp")["job"]
    chooser = {"id": "q", "kind": "listbox", "label": "* Question 1", "required": True, "section": "Security questions",
               "options": ["Select question", "What was the name of your first pet?", "In what city were you born?"],
               "account": True}
    answer = {"id": "a", "kind": "text", "label": "Question 1 Answer", "section": "Security questions", "account": True}
    desk.applier.runs[job["id"]] = Run(job["id"], "Analyst", "Example Corp", status="needs_you", need="sign_in",
                                       reason="Waiting.", questions=[chooser, answer], blocking=True)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector('[data-q="Security questions | Question 1 Answer | "]')
                seen = (await page.locator("#needs").inner_text(), await page.locator("[data-remember]").count(),
                        await page.locator('[data-q="Security questions | * Question 1 | "] option').all_inner_texts(),
                        await page.locator('[data-job-act="later"]').count())
                await browser.close()
                return seen
        finally:
            await desk.stop()

    card, remember, choices, later = run(go())
    assert card.count("For this account only: never remembered.") == 2 and remember == 0, (card, remember)
    assert choices == ["", "What was the name of your first pet?", "In what city were you born?"], choices
    assert later == 1  # (the sign-in holds the queue: Later lets it go on, as on any sign-in card)
    secret = "Flagstaff Ridge 1987"
    # all of them, or none: one answer alone is turned down, and nothing is kept
    with pytest.raises(ValueError, match="each of its security questions"):
        desk.answer(job["id"], [{"label": "Security questions | Question 1 Answer | ", "value": secret}])
    assert desk.applier.runs[job["id"]].account_answers == {} and config.saved_answers() == []
    # (a page from before, sending a box's label alone: still that form's alone)
    assert desk.answer(job["id"], [{"label": "* Question 1", "value": "In what city were you born?"},
                                   {"label": "Question 1 Answer", "value": secret, "remember": True}]) is None
    assert desk.applier.runs[job["id"]].account_answers == {
        placed_key(chooser): "In what city were you born?", placed_key(answer): secret}
    assert desk.applier.runs[job["id"]].once == {} and config.saved_answers() == []
    desk.applier.runs[job["id"]] = Run(job["id"], "Analyst", "Example Corp", status="needs_you", need="sign_in",
                                       reason="Waiting.", questions=[chooser, answer], blocking=True)
    assert desk.answer(job["id"], [
        {"label": "Security questions | * Question 1 | ", "value": "In what city were you born?", "remember": True},
        {"label": "Security questions | Question 1 Answer | ", "value": secret, "remember": True},
        {"label": "Are you willing to relocate?", "value": "No", "remember": False}]) is None
    r = desk.applier.runs[job["id"]]
    assert r.account_answers == {placed_key(chooser): "In what city were you born?", placed_key(answer): secret}
    assert r.once == {question_key("Are you willing to relocate?"): "No"} and config.saved_answers() == []
    assert secret not in json.dumps(r.public()) and secret not in json.dumps(desk.state())


def test_the_desk_page_still_loads_after_a_look_in_the_inbox(srv):
    """A job's Run keeps the employer's mail domains (a frozenset) and the other employers' mail check (a function)
    once the desk has looked in the inbox for its code; both went into the page's state, which JSON can't write, so
    every /state call failed from then on and the page froze (the review of Oct 15)."""
    from job_apply.pipeline import Run

    desk = Desk(srv)
    job = srv.add_job(url="https://careers.example.com/a", title="Tech", company="Example")["job"]
    r = Run(job["id"], "Tech", "Example", status="needs_you", need="email_code", reason="Waiting for the code.")
    r.employer_mail, r.others_mail = frozenset({"example.com"}), (lambda domain, subject: False)
    desk.applier.runs[job["id"]] = r
    assert json.dumps(desk.state())  # as the page's /state route writes it: no default=str
    assert not {"employer_mail", "others_mail"} & r.public().keys()


@pytest.mark.parametrize("width", [1280, 390])
def test_a_cards_what_went_in_shows_the_answers_as_text_and_nothing_secret(srv, job_apply_home, width):
    """The whole-program review's worth-building list, item 3: a ready job's card (and a stopped one's) has a panel,
    closed until opened, with what went into the application: its answers by page, the files put up, what was agreed
    to for the person, the account made and the optional questions left empty, so the person checks them at a glance.
    What's in it is the page's and the profile's words, shown as text, never as HTML. Never a password, a security
    answer or an emailed code; and none of it in a problem report or a note. It fits a phone's width."""
    from job_apply import report
    from job_apply.pipeline import Run, _went_in, _went_left

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    jobs = [srv.add_job(url=f"https://example.com/job/{i}", title="Technician", company="Example Fab")["job"]
            for i in range(2)]
    marked = "<b>Bold</b><img src=x onerror=\"document.title='pwned'\">"
    secret = "Flagstaff Ridge 1987"
    page = {"title": "Apply", "headings": ["My Information"], "fields": [
        {"id": "1", "kind": "text", "label": "First Name *"}, {"id": "2", "kind": "text", "label": "Referral note"},
        {"id": "3", "kind": "file", "label": "Resume/CV"}, {"id": "4", "kind": "password", "label": "Password"},
        {"id": "5", "kind": "text", "label": "Answer", "section": "Security questions"},
        {"id": "6", "kind": "text", "label": "Middle Name"}, {"id": "7", "kind": "text", "label": "Portfolio"}]}
    ready = Run(jobs[0]["id"], "Technician", "Example Fab", status="ready",
                reason="Filled and waiting on the review page.")
    _went_in(ready, [{"id": "1", "value": "Sam"}, {"id": "2", "value": marked},
                     {"id": "3", "value": str(job_apply_home / "resume.pdf"), "from": "documents.resume"},
                     {"id": "4", "value": "not-a-real-password"}, {"id": "5", "value": secret},
                     {"id": "7", "value": "https://example.com/" + "a" * 200}], page)
    _went_left(ready, {"needs_input": [{"id": "6", "kind": "text", "label": "Middle Name", "required": False}]}, page)
    ready.went_in.agree("agreed to Example Fab's notice “AI in hiring”")
    ready.went_in.account("created your account on Example Fab's site with your saved password and agreed to its "
                          "terms (manage_accounts: false in profile.yaml leaves this to you)")
    ready.went_in.change("Last Name: Rivers \u2192 Rivera")  # (a saved draft's answer the desk changed: note 38's review)
    stopped = Run(jobs[1]["id"], "Technician", "Example Fab", status="needs_you", need="questions",
                  reason="1 question your profile doesn't answer.",
                  questions=[{"id": "9", "kind": "text", "label": "Desired salary", "required": True}])
    _went_in(stopped, [{"id": "1", "value": "Sam"}], page)
    desk.applier.runs.update({jobs[0]["id"]: ready, jobs[1]["id"]: stopped})
    state = desk.state()
    assert json.dumps(state)  # (as the page's /state route writes it: no default=str)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                tab = await browser.new_page(viewport={"width": width, "height": 900})
                await tab.goto(desk.url)
                panel = tab.locator(f'#done details[data-went="{jobs[0]["id"]}"]')
                await panel.wait_for(state="attached")
                closed = await panel.evaluate("(el) => el.open")
                summary = await panel.locator("summary").inner_text()
                await panel.locator("summary").click()
                seen = await panel.inner_text()
                tags = await panel.evaluate("(el) => el.querySelectorAll('b, img').length")
                await tab.wait_for_timeout(1200)  # (a refresh or two: the panel stays open, as drawn)
                still = await panel.evaluate("(el) => el.open")
                # its card drawn again (its reason changed): the panel the person opened is open still
                ready.reason = "Filled and waiting on the review page. Check it before you press Submit."
                await tab.wait_for_function("() => document.querySelector('#done').innerText.includes('Check it before')")
                still = still and await panel.evaluate("(el) => el.open")
                other = await tab.locator(f'#needs details[data-went="{jobs[1]["id"]}"] summary').inner_text()
                html, title = await tab.content(), await tab.title()
                sizes = await tab.evaluate("() => [document.documentElement.scrollWidth, "
                                           "document.documentElement.clientWidth]")
                return closed, summary, seen, tags, still, other, html, title, sizes
        finally:
            await desk.stop()

    closed, summary, seen, tags, still, other, html, title, (scroll, client) = run(go())
    assert closed is False and still is True
    assert summary == ("What went in (3 answers, 1 file, 1 agreement, 1 account made, 1 draft answer changed, "
                       "1 left empty)"), summary
    for words in ("My Information", "First Name", "Sam", marked, "Resume: resume.pdf",
                  "agreed to Example Fab's notice “AI in hiring”",
                  "created your account on Example Fab's site with your saved password and agreed to its terms",
                  "Middle Name (your profile doesn't answer it)", "Changed from the saved draft",
                  "Last Name: Rivers \u2192 Rivera"):
        assert words in seen, (words, seen)
    assert tags == 0 and "pwned" not in title  # (the marked-up answer is its words, not the page's)
    assert "manage_accounts: false" not in seen and str(job_apply_home) not in seen
    assert other == "What went in (1 answer)", other  # (the stopped job's too)
    assert scroll <= client, (scroll, client)  # (a long answer wraps: no sideways scroll, on a phone either)
    for kept_out in ("not-a-real-password", secret):
        assert kept_out not in html and kept_out not in json.dumps(state), kept_out
    # the panel is the person's own page alone: nothing of it in a problem report or a note on the stop
    said = report.build(jobs[0], ready)["preview"] + report.note(jobs[0], ready)
    assert "Referral note" not in said and "Bold" not in said and "Rivers" not in said, said


def test_every_card_says_i_submitted_it_already(srv):
    """Issue #112, note 31 (the owner's ask): an application sent on the site itself, whatever the desk's card
    waited on, is marked applied from that card: "I submitted it already" on the questions, sign-in, stuck,
    ready and failed cards alike. It counts as applied, and the job is never queued again."""
    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    shapes = {"Example Questions": dict(status="needs_you", need="questions"),
              "Example Sign In": dict(status="needs_you", need="sign_in"),
              "Example Stuck": dict(status="needs_you", need="stuck"),
              "Example Ready": dict(status="ready"),
              "Example Failed": dict(status="failed")}
    jobs = {}
    for i, (name, shape) in enumerate(shapes.items()):
        job = srv.add_job(url=f"https://example.com/job/{i}", title="Technician", company=name)["job"]
        desk.applier.runs[job["id"]] = Run(job["id"], "Technician", name, reason="Waiting.", **shape)
        jobs[name] = job["id"]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#done .task")
                for name, job_id in jobs.items():
                    button = page.locator(f"[data-job='{job_id}'][data-job-act=applied]")
                    assert await button.count() == 1, name
                    assert await button.inner_text() == "I submitted it already", name
                await page.click(f"[data-job='{jobs['Example Ready']}'][data-job-act=applied]")
                for _ in range(40):
                    if desk.applier.runs[jobs["Example Ready"]].status == "submitted":
                        break
                    await page.wait_for_timeout(50)
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert desk.applier.runs[jobs["Example Ready"]].status == "submitted"
    assert srv.tracker().get(jobs["Example Ready"])["status"] == "applied"
    with pytest.raises(ValueError):
        desk.applier.enqueue(jobs["Example Ready"])


def test_a_review_rounds_submit_sends_only_the_application_its_banner_names(srv):
    """The review of the round: a review page whose tab was closed is passed over for the next one, and the banner
    went on naming the closed one for up to a poll while its Submit would have sent the next, which the person never
    chose. The banner names the one brought up at once, and its Submit sends only the one it names."""
    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    names = ["Example Fab", "Example Closed", "Example Litho"]
    jobs = [srv.add_job(url=f"https://example.com/job/{i}", title="Technician", company=c)["job"]
            for i, c in enumerate(names)]
    for i, (job, name) in enumerate(zip(jobs, names)):
        r = Run(job["id"], "Technician", name, status="ready", reason="Filled and waiting on the review page.")
        r.paused_at = 1000.0 + i
        desk.applier.runs[job["id"]] = r
    shown, sent = [], []

    async def focus(job_id):
        shown.append(job_id)
        return job_id != jobs[1]["id"]  # (its tab closed)
    desk.applier.focus = focus
    desk.applier.start_round = lambda job_id: None
    desk.applier.submit_now = lambda job_id: (sent.append(job_id), desk.applier.runs[job_id])[1]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.click("#notices [data-act=review-round]")
                await page.wait_for_selector("#notices [data-act=review-submit]")
                await page.click("#notices [data-act=review-next]")
                for _ in range(40):
                    if jobs[2]["id"] in shown:
                        break
                    await page.wait_for_timeout(25)
                await page.wait_for_timeout(100)
                assert "Reviewing Technician (Example Litho)" in await page.inner_text("#notices")
                await page.click("#notices [data-act=review-submit]")
                for _ in range(40):
                    if sent:
                        break
                    await page.wait_for_timeout(50)
                assert sent == [jobs[2]["id"]], sent
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_the_openings_bar_shows_where_each_job_stands_and_troubleshooting_ticks_one_per_employer(srv, job_apply_home):
    """The owner (Oct 16), troubleshooting a queue of 76: a bar over the openings shows them by where they stand
    (bot checks to solve, the ones the desk is stuck on for Claude, ready...), each with its count, and Bot checks
    offers the round that solves them; a group picked hides the others, which are never applied to; and in
    troubleshooting, Select recommended ticks one opening per employer (its others most likely stop the same way)."""
    from job_apply.pipeline import Run

    low = {"score": 20, "reasons": [], "concerns": [], "blocked": False, "recommended": False}
    rows = [("bot", "Example Fab A", 30), ("stuck", "Example Fab B", 30), ("ask", "Example Fab C", 30),
            ("ready", "Example Fab D", 30), ("new1", "Example Fab E", 50), ("new2", "Example Fab E", 70),
            ("new3", "Example Fab F", 40)]
    desk = Desk(srv)
    desk.troubleshoot = True
    desk.listings = [{"company": company, "title": f"Technician {key}", "url": f"https://example.com/{key}",
                      "location": "Phoenix, AZ", "fit": {**low, "score": score}} for key, company, score in rows]
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    for key, company, _ in rows[:4]:
        job = srv.add_job(url=f"https://example.com/{key}", title=f"Technician {key}", company=company)["job"]
        status, need = {"bot": ("needs_you", "bot_check"), "stuck": ("needs_you", "stuck"),
                        "ask": ("needs_you", "questions"), "ready": ("ready", "")}[key]
        desk.applier.runs[job["id"]] = Run(job["id"], f"Technician {key}", company, status=status, need=need)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#stops:not([hidden]) [data-stop='bot']")
                bar = {await b.get_attribute("data-stop"): " ".join((await b.inner_text()).split())
                       for b in await page.locator("#stops button").all()}
                assert bar == {"any": "Any 7", "new": "Not started 3", "bot": "Bot checks 1", "you": "Needs you 1",
                               "stuck": "Stuck 1", "ready": "Ready 1"}, bar
                await page.click("[data-stop='bot']")
                await page.wait_for_selector("text=Technician new1", state="detached")
                assert await page.locator("#rows .row").count() == 1 and await page.is_visible("text=Technician bot")
                assert await page.is_visible("#stop-hint [data-act='bot-round']")
                await page.click("[data-stop='stuck']")
                await page.wait_for_selector("text=Technician stuck")
                assert await page.locator("#rows .row").count() == 1
                assert "Report a problem" in await page.inner_text("#stop-hint")
                # a group picked: Select recommended ticks nothing it hides (none of the stuck one's is new)
                await page.click("[data-act='select-recommended']")
                assert await page.is_disabled("#apply")
                await page.click("[data-stop='new']")
                await page.wait_for_selector("text=Technician new1")
                await page.click("[data-act='select-recommended']")
                ticked = await page.eval_on_selector_all("[data-pick]:checked", "bs => bs.map((b) => b.dataset.pick)")
                # one per employer, the best fit of Example Fab E's two
                assert sorted(ticked) == ["https://example.com/new2", "https://example.com/new3"], ticked
                # one ticked by hand counts as its employer's (the review's): Example Fab E's other isn't added
                await page.click("[data-act='select-none']")
                await page.check("[data-pick='https://example.com/new1']")
                await page.click("[data-act='select-recommended']")
                ticked = await page.eval_on_selector_all("[data-pick]:checked", "bs => bs.map((b) => b.dataset.pick)")
                assert sorted(ticked) == ["https://example.com/new1", "https://example.com/new3"], ticked
                # the page's refresh leaves the bar as it is: the focus stays on it
                await page.focus("[data-stop='new']")
                await asyncio.sleep(2)
                assert await page.evaluate("document.activeElement.dataset.stop") == "new"
                await page.click("[data-stop='any']")
        finally:
            await desk.stop()

    run(go())


def test_select_recommended_where_the_desk_goes_all_the_way_ticks_only_those_employers(srv, job_apply_home):
    """The review of 0.3.195: with "Only where the desk goes all the way" on, Select recommended ticked a job of the
    person's own (tracked, saved) at another employer too. It ticks the recommended openings at those employers only,
    as before; the person's own still show, and they tick them themselves."""
    from job_apply import config as cfg

    handled = sorted(cfg.handled_employers())[0]
    good = {"score": 80, "reasons": [], "concerns": [], "blocked": False, "recommended": True}
    desk = Desk(srv)
    desk.handled_only = True
    desk.listings = [{"company": handled, "title": "Technician", "url": "https://example.com/handled", "fit": good},
                     {"company": "Example Fab", "title": "Technician", "url": "https://example.com/saved", "fit": good}]
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    srv.add_job(url="https://example.com/saved", title="Technician", company="Example Fab")

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("[data-pick='https://example.com/saved']")
                await page.click("[data-act='select-recommended']")
                ticked = await page.eval_on_selector_all("[data-pick]:checked", "bs => bs.map((b) => b.dataset.pick)")
                assert ticked == ["https://example.com/handled"], ticked
        finally:
            await desk.stop()

    run(go())


# ---------------------------------------------------------------- the desk survives a restart
# (the review of Oct 15, worth building 4: the queue lived in memory, so a restart of the desk (a Claude app restart, a
# crash, a plugin update) lost every queued job)


async def _until(check, timeout=30, about=None):
    start = time.monotonic()
    while not check():
        if time.monotonic() - start > timeout:
            raise AssertionError("timed out waiting" + (f": {about()!r}" if about else ""))
        await asyncio.sleep(0.1)


def _jobs(srv, n: int) -> list[int]:
    """n jobs at as many employers (each its own site: none waits for another's)."""
    return [srv.add_job(url=f"https://careers.employer{i}.example/apply/{i}", title=f"Technician {i}",
                        company=f"Employer {i}")["job"]["id"] for i in range(n)]


def _passes_reach_review(monkeypatch) -> list[int]:
    """Each pass (by job) as it begins; a pass ends on its review page at once."""
    passes: list[int] = []

    async def drive(self, r):
        r.status, r.started = "running", True  # (its application begun, as a real pass's is once its tab opens)
        passes.append(r.job_id)
        r.status, r.reason = "ready", "Filled and waiting on the review page."

    monkeypatch.setattr(pipeline.Applier, "_drive", drive)
    return passes


def _kept_queue() -> dict:
    return json.loads((config.home() / "desk-queue.json").read_text(encoding="utf-8"))


def _keep_queue(job_ids: list[int], submit: bool = False, paused: bool = False) -> None:
    """The queue as a desk before a restart kept it: these jobs, never begun, in this order."""
    jobs = [{"id": j, "state": "queued", **({"submit": True} if submit else {})} for j in job_ids]
    (config.home() / "desk-queue.json").write_text(json.dumps({"paused": paused, "jobs": jobs}), encoding="utf-8")


@pytest.mark.parametrize("how", ["stopped", "ended"])
def test_after_a_restart_the_jobs_never_begun_are_queued_again_paused_and_the_begun_ones_are_cards(srv, monkeypatch,
                                                                                                  how):
    """The queue is kept in the person's folder as it changes. The desk starting again queues the jobs it never began
    again, in their order, with Submit for me where they had it and it's still on; and the queue comes back paused, said
    at the top of the page, so none starts without the person's Resume queue. A job begun before the restart isn't
    queued again (the review of the restart, HIGH: one stopped on its review page's errors, the person may have fixed
    them and pressed the site's own Submit; a fresh pass with Submit for me would send a second application): it's a
    card, which says to check where it is, and its Resume fills it again up to its review page, never sending it, as
    does one the browser's closing (or the desk's stopping) cut short. A job applied to or skipped since is left out.
    Whether the desk was stopped, or the program ended with its tasks cancelled (and the queue written again after, as
    another job's end would)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    a, b, c, d, e, f = _jobs(srv, 6)
    passes: list[int] = []
    restarted = False

    async def drive(self, r):
        r.status, r.started = "running", True  # (its application begun, as a real pass's is once its tab opens)
        passes.append(r.job_id)
        if not restarted and r.job_id == a:
            return self._pause(r, "stuck", "The review page shows errors: Phone is invalid. Fix them in the browser, "
                               "then press Submit.")
        if not restarted and r.job_id == b:
            await asyncio.Event().wait()  # under way as the desk restarts
        r.status, r.reason = "ready", "Filled and waiting on the review page."

    monkeypatch.setattr(pipeline.Applier, "_drive", drive)

    async def go():
        nonlocal restarted
        before = Desk(srv)
        before.applier.auto_submit = True  # Submit for me on (desk.json), as the jobs are queued
        before._save_settings()
        assert before.apply(urls=[], job_ids=[a, b, c, d, e, f], submit=True)[0] == [a, b, c, d, e, f]
        before.applier.start()
        await _until(lambda: b in before.applier._slots and before.applier.runs[b].status == "running",
                     about=lambda: passes)
        assert before.applier.runs[a].need == "stuck"
        if how == "stopped":
            await before.applier.stop()
        else:
            tasks = [before.applier._worker_task, *before.applier._slots.values()]
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)
            before.applier._save_queue()
        assert before.applier.runs[b].status == "failed"  # (cut short)
        # meanwhile one was marked applied (Claude's update_job, or a confirmation email), and one skipped
        srv.update_job(d, status="applied")
        srv.update_job(e, status="skipped")
        restarted = True

        after = Desk(srv)
        after.search.update(status="done", at=time.time())  # (no search of the real sites)
        await after.start(port=0, open_browser=False)
        try:
            q = after.applier
            assert [j for _, j in q.tasks] == [c, f] and q.runs[c].submit and q.runs[f].submit
            assert q.paused and after.state()["queued"] == [c, f]
            assert q.runs[c].log[-1] == "picked up again after the desk restarted"
            for job_id in (a, b):  # begun: cards, never queued by themselves, Submit for me off
                r = q.runs[job_id]
                assert (r.status, r.need, r.left_at_restart, r.submit) == ("needs_you", "restarted", True, False)
                # (its tab is gone with the restart: nothing to look at there, and the case it's for, said)
                assert r.reason == ("The desk restarted before this application was finished. If you sent it yourself "
                                    "on the site, press I submitted it already. Otherwise Resume fills it again up to "
                                    "its review page (nothing is sent), or Skip job."), r.reason
            assert after.state()["restored"] == (
                "2 jobs from before the desk restarted are back in the queue, paused. Press Resume queue to go on. 2 of "
                "them go in by Submit for me once they're filled; turn Submit for me off first if you'd rather check each.")
            assert d not in q.runs and e not in q.runs
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(after.url)
                card = page.locator("#needs .task", has_text="Technician 0")
                await card.wait_for()
                said = await card.inner_text()  # (its tab is gone with the restart: nothing to look at there)
                assert ("The desk restarted before this application was finished. If you sent it yourself on the site, "
                        "press I submitted it already. Otherwise Resume fills it again up to its review page (nothing "
                        "is sent), or Skip job.") in said, said
                buttons = await card.locator("button").all_inner_texts()
                assert buttons == ["Resume", "Skip job", "I submitted it already", "Report a problem"], buttons
                await asyncio.sleep(1)
                assert passes == [a, b]  # (paused: nothing restored has started)
                notice = page.locator("#notices", has_text="back in the queue, paused")
                await notice.locator("button", has_text="Resume queue").click()  # the person's Resume queue
                await _until(lambda: q.runs[c].status == q.runs[f].status == "ready", about=lambda: passes)
                await notice.wait_for(state="detached")
                assert passes[2:] == [c, f] and q.runs[a].status == q.runs[b].status == "needs_you"
                await card.locator("button", has_text="Resume").click()  # filled again, never sent
                await _until(lambda: q.runs[a].status == "ready")
            assert passes[2:] == [c, f, a] and not q.runs[a].submit
        finally:
            await after.stop()

    run(go())


def test_a_job_whose_submit_may_have_gone_in_isnt_queued_again_after_a_restart(srv, monkeypatch):
    """A job with a Submit pressed for it and no confirmation (its folder's submission.json, written as Submit is
    pressed, or the tracker's note) may have gone in: after a restart it isn't queued again at all, even with Submit
    for me on. Its card says to check, with I submitted it, and Resume, which fills it again up to its review page and
    never sends it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    passes = _passes_reach_review(monkeypatch)
    recorded, noted, fine = _jobs(srv, 3)
    before = Desk(srv)
    before.applier.auto_submit = True
    before.applier.restore()  # (the desk keeps its queue from its start)
    before.apply(urls=[], job_ids=[recorded, noted, fine], submit=True)
    folder = Path(srv.tracker().get(recorded)["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "submission.json").write_text(json.dumps({"confirmed": False, "state": "pressing"}), encoding="utf-8")
    srv.tracker().update(noted, note="pressed Submit; no confirmation showed")

    async def go():
        after = Desk(srv)
        after.applier.auto_submit = True
        after.search.update(status="done", at=time.time())  # (no search of the real sites)
        await after.start(port=0, open_browser=False)
        try:
            q = after.applier
            assert [j for _, j in q.tasks] == [fine] and q.paused
            for job_id in (recorded, noted):
                r = q.runs[job_id]
                assert (r.status, r.need, r.left_at_restart) == ("needs_you", "check_submit", True)
                assert r.reason == pipeline.RESTART_CHECK and "may have gone in" in r.reason
            q.resume_queue()
            await _until(lambda: q.runs[fine].status == "ready")
            await asyncio.sleep(0.5)
            assert passes == [fine]
            async with person_browser() as browser:  # its card on the page: no tab to show, Resume to fill it again
                page = await browser.new_page()
                await page.goto(after.url)
                card = page.locator("#needs .task", has_text="Technician 0")
                await card.wait_for()
                assert "may have gone in" in await card.inner_text()
                buttons = await card.locator("button").all_inner_texts()
                assert buttons == ["I submitted it", "Resume", "Skip job", "Report a problem"], buttons
                await card.locator("button", has_text="Resume").click()  # filled again, never sent
                await _until(lambda: q.runs[recorded].status == "ready")
            assert not q.runs[recorded].submit and q.runs[recorded].pressed_before
            assert passes == [fine, recorded]
        finally:
            await after.stop()

    run(go())


def test_submit_for_me_goes_on_after_a_restart_only_while_it_is_still_on(srv):
    """Submit for me applies to a job never begun, queued again after a restart, only while it's still on (desk.json),
    as it does to any job queued now; a job queued before it was turned on stays without it. The notice that they're
    back says how many Submit for me sends once they're filled (the re-check: Resume queue sends those too)."""
    with_it, without = _jobs(srv, 2)
    before = Desk(srv)
    before.applier.restore()
    before.applier.auto_submit = True
    before._save_settings()
    before.apply(urls=[], job_ids=[with_it], submit=True)
    before.apply(urls=[], job_ids=[without], submit=False)  # (queued before it was turned on)

    async def go():
        again = Desk(srv)
        again.applier.start()
        assert again.applier.auto_submit and again.applier.paused  # (back paused: nothing is filled here)
        assert again.applier.runs[with_it].submit and not again.applier.runs[without].submit
        assert again.state()["restored"] == (
            "2 jobs from before the desk restarted are back in the queue, paused. Press Resume queue to go on. 1 of "
            "them goes in by Submit for me once it's filled; turn Submit for me off first if you'd rather check each.")
        again.applier.auto_submit = False  # turned off, then another restart
        again._save_settings()
        await again.applier.stop()
        third = Desk(srv)
        third.applier.start()
        assert not third.applier.auto_submit
        assert not third.applier.runs[with_it].submit and not third.applier.runs[without].submit
        await third.applier.stop()

    run(go())


def test_a_kept_queue_naming_a_job_made_one_with_another_goes_on_with_the_one_kept(srv):
    """One posting saved twice before (from Indeed and from the employer's site) is one job once the tracker opens
    after an update (Tracker._merge_copies), and the queue the desk kept may name the one gone. It never crashes: the
    queue goes on with the job kept, once, and never with one applied to on Indeed; Submit for me only for one never
    begun (begun on Indeed, it may have gone in there)."""
    t = srv.tracker()

    def older(url, status, company, title, source=""):  # (as a version before saved it)
        with t._lock:
            cur = t.conn.execute("INSERT INTO jobs (url, status, company, title, location, source, created_at, "
                                 "updated_at) VALUES (?, ?, ?, ?, 'Chandler, AZ', ?, 'x', 'x')",
                                 (url, status, company, title, source))
            t.conn.commit()
        return cur.lastrowid

    intel_indeed = older(INDEED_JOB, "saved", "Intel Corporation", "Equipment Technician", "indeed")
    intel = older(INTEL_JOB, "saved", "Intel", "Equipment Technician")
    lam_indeed = older("https://www.indeed.com/viewjob?jk=1111111111111111", "applied", "Lam Research",
                       "Field Service Engineer", "indeed")
    lam = older("https://careers.lamresearch.com/careers/job/1", "saved", "Lam Research", "Field Service Engineer")
    kla_indeed = older("https://www.indeed.com/viewjob?jk=2222222222222222", "in_progress", "KLA", "Technician",
                       "indeed")
    kla = older("https://kla.wd1.myworkdayjobs.com/Search/job/Chandler-AZ/Technician_1", "saved", "KLA", "Technician")
    (other,) = _jobs(srv, 1)
    _keep_queue([lam, intel, other, intel_indeed, kla], submit=True)
    t.close()
    srv._tracker = None  # (the update: the tracker opens again)

    desk = Desk(srv)
    desk.applier.auto_submit = True
    desk.applier.restore()
    q = desk.applier
    try:
        assert [j for _, j in q.tasks] == [intel_indeed, other, kla_indeed]
        assert q.runs[intel_indeed].submit and q.runs[other].submit and not q.runs[kla_indeed].submit
        assert not {intel, lam, lam_indeed, kla} & set(q.runs)
        assert [e["id"] for e in _kept_queue()["jobs"]] == [intel_indeed, other, kla_indeed]
        assert srv.tracker().get(intel) is None and srv.tracker().get(intel_indeed)["url"] == INTEL_JOB
    finally:
        run(q.stop())


def test_a_queue_kept_long_ago_comes_back_paused_and_says_how_old_it_is(srv, monkeypatch):
    """The review of the restart (MEDIUM): a kept queue had no age, and opening the desk weeks later (setup's
    open_job_desk, for the passwords card) started filling at once, Submit for me sending. One over a week old comes
    back as any does, paused, said with its age, and never starts by itself. Its age outlasts the restarts (the
    re-check: it was the file's, new as the queue was written again)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    passes = _passes_reach_review(monkeypatch)
    (job,) = _jobs(srv, 1)
    _keep_queue([job])
    path = config.home() / "desk-queue.json"
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))

    async def go():
        for _ in range(2):  # (and again after another restart)
            later = Desk(srv)
            later.applier.start()
            try:
                assert later.applier.paused and [j for _, j in later.applier.tasks] == [job]
                assert later.state()["restored"] == ("1 job from before the desk restarted (kept 30 days ago) is back "
                                                     "in the queue, paused. Press Resume queue to go on.")
                await asyncio.sleep(1)
                assert passes == []
            finally:
                await later.applier.stop()

    run(go())
    assert _kept_queue()["since"] < time.time() - 29 * 86400


def test_a_call_before_the_desk_has_its_queue_back_doesnt_write_over_it(srv):
    """The review of the restart (L3): the desk served its page before it read its queue back, and a call from a page
    left open in that moment (Pause queue, say) wrote the empty queue over the kept one. Nothing is written before the
    queue is back, and the desk reads it back before it serves the page."""
    from job_apply import desk as desk_module

    ids = _jobs(srv, 3)
    _keep_queue(ids)  # (kept before the restart)
    after = Desk(srv)
    after.applier.pause_queue()  # (a page's POST /api/queue/pause, before the desk has its queue back)
    assert [e["id"] for e in _kept_queue()["jobs"]] == ids
    seen: list[list[int]] = []

    class Server(desk_module._QuietServer):  # (what the desk has back as it begins serving its page)
        async def serve(self, *args, **kwargs):
            seen.append([j for _, j in after.applier.tasks])
            return await super().serve(*args, **kwargs)

    async def go():
        await after.start(port=0, open_browser=False)
        await after.stop()

    real = desk_module._QuietServer
    desk_module._QuietServer = Server
    try:
        run(go())
    finally:
        desk_module._QuietServer = real
    assert seen == [ids] and [j for _, j in after.applier.tasks] == ids


def test_one_desk_keeps_the_queue_and_another_sessions_desk_leaves_it_alone(srv, job_apply_home):
    """The review of the restart (L2): two Claude sessions share the person's folder, and the second's desk took up the
    first's queue (every job it was working on) and wrote its own over it. One desk keeps the queue at a time (a lock
    the operating system lets go of with its process); another says so on its page, picks nothing up and keeps nothing.
    Once the first has ended, the next desk picks its queue up."""
    import subprocess
    import sys

    ids = _jobs(srv, 2)
    _keep_queue(ids)  # (the first desk's, kept before a restart)
    # the first desk, started again in another Claude session (a process of its own): its queue paused, not worked here
    other = ("import asyncio, sys\n"
             "from job_apply import server\n"
             "from job_apply.desk import Desk\n"
             "async def main():\n"
             "    desk = Desk(server)\n"
             "    desk.applier.paused = True\n"
             "    desk.applier.start()\n"
             "    print('started', len(desk.applier.tasks), flush=True)\n"
             "    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.read)\n"
             "    await desk.applier.stop()\n"
             "asyncio.run(main())\n")
    proc = subprocess.Popen([sys.executable, "-c", other], env={**os.environ, "JOB_APPLY_HOME": str(job_apply_home)},
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")

    async def go():
        try:
            assert proc.stdout.readline().split() == ["started", "2"]
            kept = _kept_queue()
            second = Desk(srv)  # (this session's)
            second.applier.paused = True  # (nothing is worked here)
            second.applier.start()
            try:
                assert not second.applier.tasks and not second.applier.runs  # nothing picked up
                assert second.state()["queue_note"] == pipeline.QUEUE_ELSEWHERE
                mine = srv.add_job(url="https://careers.mine.example/apply/9", title="Technician 9",
                                   company="Mine")["job"]["id"]
                second.apply(urls=[], job_ids=[mine], submit=False)
                second.applier.pause_queue()
                assert _kept_queue() == kept  # (the first's, as it kept it)
            finally:
                await second.applier.stop()
        finally:
            proc.stdin.close()
            proc.wait(timeout=60)
        third = Desk(srv)  # (the first has ended: the next desk picks its queue up)
        third.applier.paused = True
        third.applier.start()
        try:
            assert [j for _, j in third.applier.tasks] == ids and not third.applier.queue_elsewhere
        finally:
            await third.applier.stop()

    run(go())


def test_the_kept_queue_holds_job_numbers_and_where_they_were_and_nothing_else(srv, job_apply_home):
    """The queue kept for a restart holds each job's number, where it was in the queue, and its Submit flags: never an
    answer, a security answer, an emailed code, a password, a question, a page's address or words, or the page's key."""
    password = "Hunter2-Example!"
    config.save_site_password("workday_password", password)
    asked, answered = _jobs(srv, 2)
    desk = Desk(srv)
    desk.applier.restore()
    desk.apply(urls=[], job_ids=[asked, answered], submit=True)
    r = desk.applier.runs[asked]
    r.status, r.need, r.reason = "needs_you", "questions", "1 question: what clearance do you hold?"
    r.questions = [{"label": "What clearance do you hold?", "value": "Clearance-Example-Answer"}]
    r.once = {"why do you want to work here": "Because-Example-Reason"}
    r.account_answers = {"mothers maiden name": "Maiden-Example-Answer"}
    r.url = "https://careers.employer0.example/apply/0?session=Session-Example-Token"
    r.desk_code = 314159
    r.log.append("typed the emailed code 271828 in")
    desk.applier.runs[answered].once = {"salary": f"{password} 90000"}
    desk.applier.pause_queue()  # (written as the queue changes)
    text = (job_apply_home / "desk-queue.json").read_text(encoding="utf-8")
    kept = json.loads(text)
    assert set(kept) == {"paused", "jobs", "since"} and kept["paused"] is True
    assert [e["id"] for e in kept["jobs"]] == [asked, answered]
    assert all(set(e) <= {"id", "state", "submit", "pressed", "may_have_gone"} for e in kept["jobs"]), kept
    for said in (password, "Clearance", "clearance", "Because-Example", "Maiden", "Session-Example", "employer0",
                 "314159", "271828", "Technician", "Employer", "question", desk.token):
        assert said not in text, said


def test_the_page_key_is_new_at_each_start_and_kept_nowhere(srv, job_apply_home):
    """The review of the restart (MEDIUM 2): a page key kept across restarts was sent by a desk page left open to
    127.0.0.1:8765 while the desk was down, where another account on the computer could listen for it and then use the
    restarted desk's API (press Submit, turn on Submit for me, save passwords). The key is new at each start, as before,
    and kept nowhere: a page left open says its key is out of date, and the person opens the desk again."""
    first, second = Desk(srv), Desk(srv)
    assert first.token != second.token
    assert not any("key" in p.name for p in job_apply_home.iterdir()), list(job_apply_home.iterdir())


def test_the_desks_port_is_never_one_another_program_holds():
    """The review of the restart (MEDIUM 2, L1): the desk's port check bound with SO_REUSEADDR, so a port another
    desk (or uvicorn) had bound that way and not yet listened on read as free. It never picks a port another socket
    holds, listening or not."""
    import socket

    from job_apply.desk import _free_port

    held = [("127.0.0.1", False, True), ("127.0.0.1", True, True), ("127.0.0.1", True, False), ("127.0.0.1", False, False)]
    if os.name != "nt":  # (Windows lets a specific address share a port with the wildcard's: not the desk's to judge)
        held += [("0.0.0.0", False, True), ("0.0.0.0", True, True)]
    for address, reuse, listening in held:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if reuse:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((address, 0))
            if listening:
                s.listen()
            port = s.getsockname()[1]
            assert _free_port(port) != port, (address, reuse, listening)


def test_a_folder_that_allows_no_lock_keeps_no_queue_and_says_so(srv, monkeypatch, job_apply_home):
    """The re-check of the restart: any error taking the queue's lock was read as another desk keeping the queue. Only
    a lock held elsewhere is that; one the folder doesn't allow (ENOLCK, ENOTSUP: a network drive, say) means no queue
    is kept here, and the page says so."""
    import errno
    import sys

    def no_locks(*args):
        raise OSError(errno.ENOLCK, "No locks available")

    if sys.platform == "win32":
        import msvcrt
        monkeypatch.setattr(msvcrt, "locking", no_locks)
    else:
        import fcntl
        monkeypatch.setattr(fcntl, "flock", no_locks)
    (job,) = _jobs(srv, 1)
    desk = Desk(srv)
    desk.applier.restore()
    assert not desk.applier.queue_elsewhere
    assert desk.state().get("queue_note") == pipeline.QUEUE_NO_LOCK
    desk.apply(urls=[], job_ids=[job], submit=False)
    assert not (job_apply_home / "desk-queue.json").exists()


def test_a_desk_takes_the_queue_up_once_the_other_has_ended_and_holds_only_what_it_adds(srv, monkeypatch,
                                                                                       job_apply_home):
    """The re-check of the restart: a desk that found another keeping the queue only tried again as it was opened, and
    then paused its whole queue for what it picked up. It looks again about every minute while the other runs; once
    that one has ended, the note goes, and the jobs it had kept are back, held for the person's Resume queue (said so),
    while this desk's own queue goes on."""
    import subprocess
    import sys

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "QUEUE_LOCK_RETRY", 0.5, raising=False)
    passes = _passes_reach_review(monkeypatch)
    theirs, mine, later = _jobs(srv, 3)
    _keep_queue([theirs])  # (the other desk's)
    holder = ("import sys\n"
              "from job_apply import pipeline\n"
              "print('held' if pipeline._take_queue_lock() in (True, 'mine') else 'not', flush=True)\n"
              "sys.stdin.read()\n")
    proc = subprocess.Popen([sys.executable, "-c", holder], env={**os.environ, "JOB_APPLY_HOME": str(job_apply_home)},
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")

    async def go():
        desk = Desk(srv)
        try:
            assert proc.stdout.readline().strip() == "held"
            desk.applier.start()
            assert desk.applier.queue_elsewhere and theirs not in desk.applier.runs
            desk.apply(urls=[], job_ids=[mine], submit=False)  # (this desk's own, going on)
            await _until(lambda: desk.applier.runs[mine].status == "ready")
        finally:
            proc.stdin.close()
            proc.wait(timeout=60)
        try:
            await _until(lambda: theirs in desk.applier.runs, timeout=15)  # (looked at again by itself)
            q = desk.applier
            assert not q.queue_elsewhere and desk.state()["queue_note"] is None and not q.paused
            assert [j for _, j in q.tasks] == [theirs]
            assert desk.state()["restored"] == ("1 job the Job Desk before this one kept (it has ended) is back in the "
                                                "queue, held. Press Resume queue to go on.")
            desk.apply(urls=[], job_ids=[later], submit=False)
            await _until(lambda: q.runs[later].status == "ready")
            await asyncio.sleep(1)
            assert passes == [mine, later]  # (theirs held, this desk's own gone on)
            q.resume_queue()
            await _until(lambda: q.runs[theirs].status == "ready")
            assert passes == [mine, later, theirs] and desk.state()["restored"] is None
        finally:
            await desk.applier.stop()

    run(go())


def test_a_job_waiting_for_its_tailored_resume_comes_back_waiting_for_it(srv, monkeypatch):
    """The re-check of the restart: a job waiting for a tailored resume (its tab never opened) came back as a card for
    a job begun. It was never begun: it waits for its resume again, listed for Claude to write it, with Submit for me
    under the usual rule."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    (job,) = _jobs(srv, 1)
    before = Desk(srv)
    before.applier.auto_submit = before.applier.tailor = True  # (Submit for me on, and resumes tailored: desk.json)
    before._save_settings()

    async def go():
        before.applier.start()
        before.apply(urls=[], job_ids=[job], submit=True)
        await _until(lambda: before.applier.runs[job].need == "tailor")
        await before.applier.stop()
        after = Desk(srv)
        after.applier.start()
        try:
            r = after.applier.runs[job]
            assert (r.status, r.need, r.submit, r.left_at_restart) == ("needs_you", "tailor", True, False), r.reason
            assert after.applier.tailoring() == [r]
        finally:
            await after.applier.stop()

    run(go())


# ------------------------------------------------------------- the desk's history: This week
def _history_line(now: float, days_ago: float, minutes: float | None = None, **entry) -> dict:
    """A line of runs.jsonl as the desk writes it, its pass ended `days_ago` days before `now`."""
    from job_apply import history

    ended = now - days_ago * 86400
    line = {**entry, "ended": history.stamp(ended)}
    if minutes is not None:
        line.update(started=history.stamp(ended - minutes * 60), minutes=minutes)
    return line


def test_this_week_sums_up_the_desks_history(srv, job_apply_home):
    """The review of Oct 15 (worth building 5): /api/history (the page's This week) sums up the last 7 days of the
    desk's history: the jobs filled up to their review page (sent ones too) and sent, its stops for the person by kind,
    failed, skipped and cut short, the minutes a job took (in all, and the median), the employers it stopped at most,
    and its newest lines. A line older than the days asked for, or that won't read, is left out. Locked to this computer
    and this page, as every call is."""
    now = time.time()
    lines = [
        _history_line(now, 10, 5.0, job_id=1, company="Old Co", title="Tech", outcome="needs_you", need="sign_in"),
        _history_line(now, 6, 4.0, job_id=2, company="Acme Fab", title="FSE", outcome="needs_you", need="sign_in"),
        _history_line(now, 5.9, 2.0, job_id=2, company="Acme Fab", title="FSE", outcome="ready"),
        _history_line(now, 5, None, job_id=2, company="Acme Fab", title="FSE", outcome="marked_applied"),
        _history_line(now, 4, 6.0, job_id=3, company="Acme Fab", title="Tech", outcome="needs_you", need="questions"),
        _history_line(now, 3.8, 1.0, job_id=3, company="Acme Fab", title="Tech", outcome="needs_you", need="sign_in"),
        _history_line(now, 3.5, 3.0, job_id=4, company="Other Litho", title="Tech", outcome="needs_you",
                      need="bot_check"),
        _history_line(now, 3, 1.0, job_id=4, company="Other Litho", title="Tech", outcome="failed"),
        _history_line(now, 2, 9.0, job_id=5, company="Third Tool", title="FSE", outcome="submitted"),
        _history_line(now, 1, None, job_id=6, company="Fourth Fab", title="FSE", outcome="skipped"),
        _history_line(now, 0.5, 0.5, job_id=7, company="Acme Fab", title="Operator", outcome="stopped"),
    ]
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                h = {"x-desk-token": desk.token}
                empty = (await c.get("/api/history", headers=h)).json()
                (job_apply_home / "runs.jsonl").write_text(
                    "".join(json.dumps(e) + "\n" for e in lines) + '{"job_id": 8, "outco\n', encoding="utf-8")
                refused = [(await c.get("/api/history")).status_code,
                           (await c.get("/api/history", headers={"x-desk-token": "guess"})).status_code,
                           (await c.get("/api/history", headers={**h, "host": "evil.example"})).status_code]
                bad = [(await c.get(f"/api/history?days={d}", headers=h)).status_code for d in ("0", "abc", "400", "-1")]
                week = (await c.get("/api/history?recent=3", headers=h)).json()
                month = (await c.get("/api/history?days=30", headers=h)).json()
                return empty, refused, bad, week, month
        finally:
            await desk.stop()

    empty, refused, bad, week, month = run(go())
    assert (empty["any"], empty["lines"], empty["recent"], empty["stops"]) == (False, 0, [], []), empty
    assert refused == [403, 403, 403] and bad == [400, 400, 400, 400], (refused, bad)
    # (marked as applied by the person: neither filled up to its review page by the desk, nor sent by it)
    assert {k: week[k] for k in ("days", "any", "lines", "jobs", "reviewed", "submitted", "marked", "failed", "skipped",
                                 "stopped", "pauses", "minutes", "median_minutes", "timed_jobs")} == {
        "days": 7, "any": True, "lines": 10, "jobs": 6, "reviewed": 2, "submitted": 1, "marked": 1, "failed": 1,
        "skipped": 1, "stopped": 1, "pauses": {"sign_in": 2, "questions": 1, "bot_check": 1}, "minutes": 26.5,
        "median_minutes": 6.0, "timed_jobs": 5}, week
    assert [(s["company"], s["times"], s["kind"]) for s in week["stops"]] == [
        ("Acme Fab", 3, "sign_in"), ("Other Litho", 2, "bot_check")], week["stops"]
    assert [(e["job_id"], e["outcome"]) for e in week["recent"]] == [(7, "stopped"), (6, "skipped"), (5, "submitted")]
    assert week["recent"][0]["minutes"] == 0.5 and week["recent"][0]["company"] == "Acme Fab", week["recent"]
    assert (month["lines"], month["jobs"], month["pauses"]["sign_in"]) == (11, 7, 3), month
    # Claude's desk_history tool says the same, the desk open or not
    told = run(srv.desk_history(recent=3))
    assert {k: v for k, v in told.items() if k not in ("file", "since")} == {
        k: v for k, v in week.items() if k != "since"} and told["file"].endswith("runs.jsonl"), told
    with pytest.raises(ValueError):
        run(srv.desk_history(days=0))


def test_this_week_takes_lines_changed_by_hand(srv, job_apply_home):
    """The review of 0.3.204: a line changed by hand could break This week: "minutes": Infinity (which json reads) made
    the window's JSON one that can't be sent, and a time before 1970 without its offset can't be told on some systems.
    Such lines are passed over (their numbers that aren't, and the lines whose time can't be told), and nothing that
    isn't a finite number or plain words is listed."""
    from job_apply import history

    now = history.stamp(time.time() - 60)
    lines = [
        f'{{"job_id": NaN, "company": "Acme Fab", "outcome": "ready", "minutes": Infinity, "rounds": Infinity, '
        f'"pages": -Infinity, "ended": "{now}"}}',
        f'{{"job_id": 5, "company": "Acme Fab", "outcome": "ready", "minutes": 2.0, "resumes": NaN, "reason": ["a"], '
        f'"ended": "{now}"}}',
        '{"job_id": 6, "company": "Acme Fab", "outcome": "failed", "ended": "1900-01-01T00:00:00"}',
        '{"job_id": 7, "company": "Acme Fab", "outcome": "failed", "ended": "0001-01-01T00:00:00"}',
        '{"job_id": 8, "company": "Acme Fab", "outcome": "failed", "ended": "9999-12-31T23:59:59-12:00"}',
    ]
    (job_apply_home / "runs.jsonl").write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    desk = Desk(srv)
    desk.applier.start = lambda: None

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                return await c.get("/api/history?days=366", headers={"x-desk-token": desk.token})
        finally:
            await desk.stop()

    answer = run(go())
    assert answer.status_code == 200, answer.text
    week = answer.json()
    assert (week["minutes"], week["timed_jobs"], week["reviewed"]) == (2.0, 1, 2), week
    assert all(isinstance(v, str) or v == v and abs(v) != float("inf") for e in week["recent"] for v in e.values()), \
        week["recent"]
    unnumbered = next(e for e in week["recent"] if e["outcome"] == "ready" and "minutes" not in e)
    timed = next(e for e in week["recent"] if e.get("minutes") == 2.0)
    assert {"job_id", "rounds", "pages"}.isdisjoint(unnumbered) and {"resumes", "reason"}.isdisjoint(timed), \
        week["recent"]


def test_the_desk_page_shows_each_log_lines_time_and_this_week(srv, job_apply_home):
    """Each line of a job's log on the desk shows the time it was logged (HH:MM, this computer's clock), and the desk's
    JSON carries the times beside the lines. The header's This week opens a window that sums up the desk's history,
    and says so plainly when there's none yet."""
    from job_apply.pipeline import Run

    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    job = srv.add_job(url=fixture_url("site/posting.html"), title="Field Service Engineer", company="Example Fab")["job"]
    r = desk.applier.runs[job["id"]] = Run(job["id"], "Field Service Engineer", "Example Fab", status="running")
    for line in ("opened the posting", "clicked “Apply”", "filled 4 field(s) on “Your Details”"):
        desk.applier._log(r, line)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                shown = (await c.get("/api/state", headers={"x-desk-token": desk.token})).json()
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("#work .log .when")
                times = await page.locator("#work .log .when").all_inner_texts()
                lines = await page.locator("#work .log li").all_inner_texts()
                await page.click("#week-btn")
                await page.wait_for_selector("#week[open] #week-body p")
                none_yet = await page.inner_text("#week-body")
                await page.click("#week button[data-act=week-close]")
                (job_apply_home / "runs.jsonl").write_text(json.dumps(_history_line(
                    time.time(), 0.1, 3.5, job_id=job["id"], company="Example Fab", title="Field Service Engineer",
                    host="careers.example.com", outcome="ready", reason="Filled and waiting on the review page.")) + "\n",
                    encoding="utf-8")
                await page.click("#week-btn")
                await page.wait_for_selector("#week[open] #week-recent li")
                week = await page.inner_text("#week-body")
                await browser.close()
                return shown, times, lines, none_yet, week
        finally:
            await desk.stop()

    shown, times, lines, none_yet, week = run(go())
    kept = next(o["run"] for o in shown["listings"] + shown["others"] if o["job_id"] == job["id"] and o["run"])
    assert len(kept["log_at"]) == len(kept["log"]) == 3 and all(t > 0 for t in kept["log_at"]), kept
    assert len(times) == 3 and all(len(t) == 5 and t[2] == ":" and t.replace(":", "").isdigit() for t in times), times
    assert lines[0].startswith(times[0]) and lines[0].endswith("opened the posting"), lines
    assert "No history yet" in none_yet, none_yet
    assert "Filled up to their review page: 1 job. Sent by the desk: 0 jobs." in week and "Example Fab" in week, week
    assert "Time with the desk (waits included; jobs at once overlap)" in week, week
    assert "at its review page (3.5 min)" in week and "a job took 3.5 minutes" in week, week


# ------------------------------------------------------------- after submit: My applications
AFTER_NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
AFTER_GROUPS = {"Quiet Fab": "waiting", "Fresh Fab": "waiting", "Ready Fab": "waiting", "Ack Fab": "heard",
                "Quiz Fab": "heard", "Talk Fab": "interviews", "Test Fab": "interviews", "Offer Fab": "interviews",
                "No Fab": "closed", "Gone Fab": "closed"}


def _after_submit_jobs(srv, monkeypatch) -> dict[str, int]:
    """An application at each stage after it was sent in (and a job only saved), with the tracker's clock at AFTER_NOW:
    Quiet Fab applied 16 days before with no reply (a follow-up due since Oct 11), Fresh Fab 5 days before, Ready Fab
    not sent yet; Ack Fab confirmed; Quiz Fab sent an assessment, and the person put it back to applied; Talk Fab and
    Test Fab are interviewing (Test Fab's newest email an assessment), Offer Fab has an offer; No Fab was turned down and
    Gone Fab withdrawn."""
    from job_apply import tracker

    monkeypatch.setattr(tracker, "_clock", lambda: AFTER_NOW)
    t = srv.tracker()
    ids: dict[str, int] = {}

    def job(company, status="applied", applied_at="2026-09-20T15:00:00+00:00"):
        jid = srv.add_job(url=f"https://example.com/jobs/{company.split()[0].lower()}", title="Technician",
                          company=company)["job"]["id"]
        srv.update_job(jid, status=status)
        t.conn.execute("UPDATE jobs SET applied_at = ? WHERE id = ?", (applied_at, jid))
        t.conn.commit()
        ids[company] = jid
        return jid

    job("Quiet Fab", applied_at="2026-10-01T15:00:00+00:00")
    job("Fresh Fab", applied_at="2026-10-12T09:00:00+00:00")
    job("Ready Fab", status="ready_to_submit", applied_at="")
    srv.log_email(job("Ack Fab"), "ack-1", "confirmation", "We received your application", "2026-09-21")
    srv.log_email(job("Quiz Fab"), "quiz-1", "assessment", "Online skills test, due in 5 days", "2026-10-02")
    srv.update_job(ids["Quiz Fab"], status="applied", event_note="not an interview yet")
    srv.log_email(job("Talk Fab"), "talk-1", "confirmation", "Received", "2026-09-21")
    srv.log_email(ids["Talk Fab"], "talk-2", "interview", "Phone screen request, reply with availability", "2026-10-06")
    srv.log_email(job("Test Fab"), "test-1", "assessment", "HireVue video interview by Friday", "2026-10-09")
    job("Offer Fab", status="offer")
    srv.log_email(job("No Fab"), "no-1", "rejection", "Not moving forward", "2026-10-04")
    job("Gone Fab", status="withdrawn")
    job("Saved Fab", status="saved", applied_at="")
    return ids


def test_my_applications_say_when_each_last_heard_whether_to_follow_up_and_its_group(srv, job_apply_home, monkeypatch):
    """The code review's worth building 7 (after submit): each application in the desk's state carries its newest
    email logged (category, when it came, that day and its one-line summary) and how many are logged; the day a
    follow-up became due, for one applied to settings.follow_up_days days ago (10 by default) with no email at all; and
    its group (waiting, heard, interviews, closed). Claude's list_jobs says the same, small, and only for a job sent in."""
    ids = _after_submit_jobs(srv, monkeypatch)
    desk = Desk(srv)
    desk.applier.start = lambda: None
    state = desk.state()
    apps = {a["company"]: a for a in state["applications"]}
    assert {c: a["group"] for c, a in apps.items()} == AFTER_GROUPS, apps
    assert {c: a["follow_up_due"] for c, a in apps.items() if a["follow_up_due"]} == {"Quiet Fab": "2026-10-11"}
    assert state["settings"]["follow_up_days"] == 10
    assert (apps["Quiet Fab"]["last_heard"], apps["Quiet Fab"]["emails"]) == (None, 0)
    assert apps["Talk Fab"]["last_heard"] == {"category": "interview", "received_at": "2026-10-06", "day": "2026-10-06",
                                              "summary": "Phone screen request, reply with availability"}
    assert apps["Talk Fab"]["emails"] == 2 and apps["Ack Fab"]["emails"] == 1
    assert (apps["Quiz Fab"]["last_heard"]["category"], apps["Test Fab"]["last_heard"]["category"]) == ("assessment",) * 2
    listed = {j["company"]: j for j in srv.list_jobs()["jobs"]}
    assert (listed["Quiet Fab"]["last_heard"], listed["Quiet Fab"]["follow_up_due"]) == (None, "2026-10-11")
    assert listed["Talk Fab"]["last_heard"] == {"category": "interview", "day": "2026-10-06",
                                                "summary": "Phone screen request, reply with availability"}
    assert listed["Talk Fab"]["follow_up_due"] is None and listed["Talk Fab"]["id"] == ids["Talk Fab"]
    assert {"last_heard", "follow_up_due"}.isdisjoint(listed["Saved Fab"]), listed["Saved Fab"]
    # the person's own number of days
    profile = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    profile["settings"]["follow_up_days"] = 4
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(profile), encoding="utf-8")
    due = {a["company"]: a["follow_up_due"] for a in desk.state()["applications"] if a["follow_up_due"]}
    assert due == {"Quiet Fab": "2026-10-05", "Fresh Fab": "2026-10-16"}, due
    assert {j["company"]: j.get("follow_up_due") for j in srv.list_jobs()["jobs"] if j.get("follow_up_due")} == due


def test_my_applications_on_the_page_are_grouped_marked_for_follow_up_and_copy_a_prep_request(srv, monkeypatch):
    """My applications on the desk's page: a bar groups the applications (Waiting, with how many are due a follow-up;
    Heard back; Interviews; Closed), with counts, and the group picked is kept in the browser. Each row says when the
    employer last wrote ("Heard <day>: <category>", the summary as its tooltip) or "No reply yet", and one due a
    follow-up says "Follow up?" with its day. An interview's row, and one whose newest email is an assessment, has
    Prep me: the page can't reach Claude, so it copies a request to paste to Claude and says so. Nothing is sent."""
    ids = _after_submit_jobs(srv, monkeypatch)
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(permissions=["clipboard-read", "clipboard-write"])
                sent: list[str] = []
                page.on("request", lambda r: sent.append(r.url) if not r.url.startswith(desk.url.split("?")[0]) else None)
                await page.goto(desk.url)
                await page.wait_for_function("document.getElementById('mine-count').textContent === '(10)'")
                seen = {"hidden_on_openings": await page.locator("#app-groups").is_hidden()}
                await page.click("[data-filter=mine]")
                await page.wait_for_selector("#app-groups:not([hidden]) button")
                seen["bar"] = " ".join((await page.inner_text("#app-groups")).split())
                await page.click("[data-app-group=interviews]")
                await page.wait_for_selector("[data-app-group=interviews][aria-pressed=true]")
                seen["interviews"] = await page.inner_text("#rows")
                seen["interview_preps"] = await page.locator("#rows [data-act=prep]").count()
                talk = page.locator("#rows li", has_text="Talk Fab")
                seen["heard"] = " ".join((await talk.locator(".heard").inner_text()).split())
                seen["heard_tip"] = await talk.locator(".heard").get_attribute("title")
                await talk.locator("[data-act=prep]").click()
                await page.wait_for_function("document.getElementById('toast').textContent.includes('Copied')")
                seen["toast"] = await page.inner_text("#toast")
                seen["copied"] = await page.evaluate("navigator.clipboard.readText()")
                await page.locator("#rows li", has_text="Test Fab").locator("[data-act=prep]").click()
                await page.wait_for_function("async () => (await navigator.clipboard.readText()).includes('Test Fab')")
                seen["copied_test"] = await page.evaluate("navigator.clipboard.readText()")
                await page.click("[data-app-group=heard]")
                await page.wait_for_selector("[data-app-group=heard][aria-pressed=true]")
                seen["heard_rows"] = await page.inner_text("#rows")
                seen["heard_preps"] = [await page.locator("#rows li", has_text=c).locator("[data-act=prep]").count()
                                       for c in ("Quiz Fab", "Ack Fab")]
                await page.click("[data-app-group=waiting]")
                await page.wait_for_selector("[data-app-group=waiting][aria-pressed=true]")
                seen["waiting"] = await page.inner_text("#rows")
                seen["quiet"] = " ".join((await page.inner_text("#rows li:has-text('Quiet Fab')")).split())
                seen["waiting_preps"] = await page.locator("#rows [data-act=prep]").count()
                await page.reload()
                await page.wait_for_function("document.getElementById('mine-count').textContent === '(10)'")
                await page.click("[data-filter=mine]")
                await page.wait_for_selector("#app-groups:not([hidden]) button")
                seen["kept"] = await page.get_attribute("[data-app-group=waiting]", "aria-pressed")
                seen["kept_rows"] = await page.inner_text("#rows")
                await page.set_viewport_size({"width": 390, "height": 900})
                seen["sizes"] = await page.evaluate("() => [document.documentElement.scrollWidth, "
                                                    "document.documentElement.clientWidth]")
                await page.click("[data-filter=recommended]")
                seen["hidden_again"] = await page.locator("#app-groups").is_hidden()
                seen["sent"] = sent
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["hidden_on_openings"] and seen["hidden_again"], seen
    assert seen["bar"] == "All 10 Waiting 3, 1 to follow up Heard back 2 Interviews 3 Closed 2", seen["bar"]
    interviews = seen["interviews"]
    assert all(c in interviews for c in ("Talk Fab", "Test Fab", "Offer Fab")) and "Quiet Fab" not in interviews, interviews
    assert seen["interview_preps"] == 3, seen
    assert seen["heard"] == "Heard 2026-10-06: interview", seen["heard"]
    assert seen["heard_tip"] == "Phone screen request, reply with availability (2 emails logged)", seen["heard_tip"]
    assert seen["toast"] == "Copied: paste it to Claude", seen["toast"]
    assert seen["copied"] == (f"Prep me for my interview: job {ids['Talk Fab']}, Technician at Talk Fab "
                              "(use interview-prep)"), seen["copied"]
    assert seen["copied_test"] == (f"Prep me for my assessment: job {ids['Test Fab']}, Technician at Test Fab "
                                   "(use interview-prep)"), seen["copied_test"]
    assert "Quiz Fab" in seen["heard_rows"] and "Heard 2026-09-21: confirmation" in seen["heard_rows"], seen["heard_rows"]
    assert seen["heard_preps"] == [1, 0], seen
    waiting = seen["waiting"]
    assert all(c in waiting for c in ("Quiet Fab", "Fresh Fab", "Ready Fab")) and "Ack Fab" not in waiting, waiting
    assert "Follow up? Due 2026-10-11" in seen["quiet"] and "No reply yet" in seen["quiet"], seen["quiet"]
    assert waiting.count("Follow up?") == 1 and seen["waiting_preps"] == 0, waiting
    assert seen["kept"] == "true" and "Quiet Fab" in seen["kept_rows"] and "Talk Fab" not in seen["kept_rows"], seen
    assert seen["sizes"][0] <= seen["sizes"][1], seen["sizes"]
    assert seen["sent"] == [], seen["sent"]  # (nothing went anywhere but the desk)
