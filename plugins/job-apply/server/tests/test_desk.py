"""The Job Desk's local web API, driven the way its page drives it."""

import asyncio
import os
import time

import httpx
import pytest
from conftest import browser_available, fixture_url, run

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
    desk.listings = [
        {"company": "Example Fab", "title": "Field Service Engineer", "url": posting, "location": "Phoenix, AZ",
         "fit": {"score": 80, "reasons": ["title matches"], "concerns": [], "blocked": False, "recommended": True}},
        {"company": "Example Bank", "title": "Accountant", "url": "https://example.com/acct", "location": "Phoenix, AZ",
         "fit": {"score": 20, "reasons": [], "concerns": ["not one of your target titles"], "blocked": False,
                 "recommended": False}},
    ]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    desk.applier.runs[other["id"]] = Run(other["id"], "Technician", "Example Litho", status="needs_you", need="questions",
                                         reason="1 question", questions=[{"id": "9", "label": "Do you have a valid driver's license?",
                                                                          "kind": "select", "options": ["Select One", "Yes", "No"],
                                                                          "required": True}])
    desk.applier.start = lambda: None  # the queue isn't worked in this test
    desk.search.update(status="done", at=time.time())  # recent, so opening the page doesn't search the real sites

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                page = await browser.new_page()
                await page.goto(desk.url)
                await page.wait_for_selector("text=Field Service Engineer")
                assert await page.locator("text=Accountant").count() == 0  # not recommended: hidden by default
                await page.click("text=Select recommended")
                await page.click("#apply")
                for _ in range(50):
                    if desk.applier.tasks:
                        break
                    await asyncio.sleep(0.1)
                queued = [jid for _, jid in desk.applier.tasks]
                assert [srv.tracker().get(j)["title"] for j in queued] == ["Field Service Engineer"]

                await page.select_option("select[data-q]", "Yes")
                await page.click(f"button[data-job='{other['id']}'][data-job-act='fill']")
                for _ in range(50):
                    if config.saved_answers():
                        break
                    await asyncio.sleep(0.1)
                assert config.saved_answers()[0]["answer"] == "Yes"
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
                await page.wait_for_selector("#pw-state:text('Saved')")
                assert config.get_secret("workday_password") == "typed-on-the-page"
                # not left sitting in the page (cleared once the save returns; the page's own
                # refresh can show "Saved" a moment before that)
                await page.wait_for_function("() => document.getElementById('pw').value === ''", timeout=5000)
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
    assert desk.apply(urls=[added["url"]], job_ids=[], submit=False) == [added["id"]]


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
                browser = await pw.chromium.launch()
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
                assert (await c.get("/api/state", headers=h)).json()["passwords"] == {"workday": False}
                assert (await c.post("/api/password", headers=h, json=body)).json() == {"saved": True}
                after = await c.get("/api/state", headers=h)
                assert after.json()["passwords"] == {"workday": True} and "s3cret" not in after.text
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
    assert Desk(srv).state()["passwords"] == {"workday": False}


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
                browser = await pw.chromium.launch()
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
