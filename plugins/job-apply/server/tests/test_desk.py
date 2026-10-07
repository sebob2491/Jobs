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

    async def offline(url):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(srv, "search_company_jobs", fake_search)
    desk = Desk(srv)
    desk.fetch = offline

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
                # another system's password (Edwards, Qorvo and Amkor are on SuccessFactors)
                assert await page.locator("#pw-state [data-site=successfactors].good").count() == 0
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
                        "ukg": False, "infor": False}
                assert (await c.get("/api/state", headers=h)).json()["passwords"] == none
                assert (await c.post("/api/password", headers=h, json=body)).json() == {"saved": True}
                after = await c.get("/api/state", headers=h)
                assert after.json()["passwords"] == {**none, "workday": True} and "s3cret" not in after.text
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


def test_a_broken_secrets_file_doesnt_break_the_page(srv, job_apply_home):
    config.secrets_path().write_text("workday_password: [unclosed\n")
    assert Desk(srv).state()["passwords"] == {"workday": False, "successfactors": False, "icims": False,
                                              "applicantstack": False, "ukg": False, "infor": False}


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
        out = srv.tailoring_queue()
    finally:
        desk_module._desk = None
    assert [j["job_id"] for j in out["jobs"]] == [job["id"]]
    assert "EUV tools" in out["jobs"][0]["description"]
    assert out["base_resume"].startswith("# Sam Rivera") and out["resume_file"].endswith("resume.pdf")
    assert any("Coursework is not a degree" in rule for rule in out["rules"])

