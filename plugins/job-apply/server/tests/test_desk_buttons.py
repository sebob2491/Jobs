"""The desk's buttons that no test pressed through the page (the design of Oct 18, A9): Carry on with the queue on the
notice of jobs back after a restart, Undo on a skipped job's row, Show me, Clear, the review round's Stop, and the
header's This week, Safety, Notes and What and where. Each is pressed in the real page; the test checks the call it made
to the desk (or that it made none) and what the page shows after."""

import asyncio
import contextlib
import json
import time
from urllib.parse import urlsplit

import pytest
from conftest import browser_available, person_browser, run
from test_desk import _history_line, _note, _stopped_desk

from job_apply import report
from job_apply.desk import Desk

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

# the page's own refresh and its "looked at the openings": every page makes them, whatever is pressed
_POLLS = ("/api/state", "/api/seen")


@contextlib.asynccontextmanager
async def _desk_page(desk: Desk, dialogs: list[str] | None = None):
    """The desk started, its page open in a browser of the person's own; with the list of the calls the page makes to
    the desk's API besides its refresh (method, path with its query, the JSON sent), and the alerts and confirms it
    shows (accepted) in `dialogs`."""
    calls: list[tuple[str, str, object]] = []

    def record(request) -> None:
        url = urlsplit(request.url)
        if not url.path.startswith("/api/") or url.path in _POLLS:
            return
        body = request.post_data
        calls.append((request.method, url.path + (f"?{url.query}" if url.query else ""),
                      json.loads(body) if body else None))

    await desk.start(port=0, open_browser=False)
    try:
        async with person_browser() as browser:
            page = await browser.new_page()
            page.on("request", record)
            page.on("dialog", lambda d: (dialogs.append(d.message) if dialogs is not None else None,
                                         asyncio.ensure_future(d.accept())))
            await page.goto(desk.url)
            await page.wait_for_selector("#plugin-version:not(:empty)")  # the page has its state
            yield page, calls
            await browser.close()
    finally:
        await desk.stop()


async def _until(check, about: object = None, timeout: float = 10) -> None:
    start = time.monotonic()
    while not check():
        if time.monotonic() - start > timeout:
            raise AssertionError(f"timed out: {about() if callable(about) else about}")
        await asyncio.sleep(0.05)


def _idle_desk(srv) -> Desk:
    desk = Desk(srv)
    desk.applier.start = lambda: None  # (the queue isn't worked: what the press did is what's looked at)
    desk.search.update(status="done", at=time.time())  # (recent: opening the page searches nothing)
    return desk


def test_resume_queue_on_the_restored_notice_lets_the_jobs_back_go_on(srv):
    """Jobs queued again after a restart wait, paused, under a notice with Carry on with the queue: pressed, the queue
    goes on, the notice goes and the header's button is Pause queue again."""
    from job_apply.pipeline import Run

    desk = _idle_desk(srv)
    job = srv.add_job(url="https://careers.example.com/fab/1", title="Technician", company="Example Fab")["job"]
    a = desk.applier
    a.runs[job["id"]] = Run(job["id"], "Technician", "Example Fab", status="queued")
    a.tasks.append(("apply", job["id"]))
    a._restored_ids.add(job["id"])
    a.paused = True
    assert "Press Carry on with the queue." in a.restored_notice()

    async def go():
        async with _desk_page(desk) as (page, calls):
            notice = page.locator("#notices .waiting", has_text="back in the queue")
            await notice.wait_for()
            assert await page.inner_text("#pause-btn") == "Carry on with the queue (1 waiting)"
            await notice.locator("[data-act=resume]").click()
            await notice.wait_for(state="detached")
            await page.wait_for_selector("#pause-btn:text-is('Pause queue')")
            assert "Paused" not in await page.inner_text("#work")
            return calls

    calls = run(go())
    assert calls == [("POST", "/api/queue/resume", {})], calls
    assert not a.paused and a.restored_notice() is None and [j for _, j in a.tasks] == [job["id"]]


def test_undo_on_a_skipped_jobs_row_puts_it_back_in_the_queue(srv):
    """Undo on the row of a job skipped in this desk: the call names that job, the job is queued again, and its row
    says so (no Undo, its box not ticked: it's under way)."""
    from job_apply.pipeline import Run

    desk, jobs = _stopped_desk(srv, {"Example Fab": dict(status="skipped")})
    job_id = jobs["Example Fab"]
    srv.update_job(job_id, status="skipped")

    async def go():
        async with _desk_page(desk) as (page, calls):
            row = page.locator("#rows li.row", has_text="Example Fab")
            await row.locator("button[data-job-act=unskip]").click()
            await row.locator(".pill:text-is('Queued')").wait_for()
            undo_left = await row.locator("button[data-job-act=unskip]").count()
            box = row.locator("input[type=checkbox]")
            return calls, undo_left, await box.is_checked(), await box.is_disabled()

    calls, undo_left, checked, disabled = run(go())
    assert calls == [("POST", f"/api/job/{job_id}/unskip", {})], calls
    assert undo_left == 0 and not checked and disabled
    assert isinstance(desk.applier.runs[job_id], Run) and desk.applier.runs[job_id].status == "queued"
    assert [j for _, j in desk.applier.tasks] == [job_id]


def test_show_me_brings_the_jobs_tab_up_or_says_its_closed(srv):
    """Show me on the banner of the job holding the queue brings its tab to the front; on a job whose tab is closed
    the desk answers 409 and the page says so, pointing to Carry on."""
    desk, jobs = _stopped_desk(srv, {"Example Fab": dict(status="needs_you", need="sign_in", blocking=True),
                                     "Example Litho": dict(status="needs_you", need="stuck")})
    fab, litho = jobs["Example Fab"], jobs["Example Litho"]
    shown: list[int] = []

    async def focus(job_id):  # (the desk's own: run.page.bring_to_front, or False with no tab)
        shown.append(job_id)
        return job_id == fab
    desk.applier.focus = focus
    said: list[str] = []

    async def go():
        async with _desk_page(desk, said) as (page, calls):
            await page.click(f"#notices .waiting [data-job='{fab}'][data-job-act=show]")
            await _until(lambda: shown, "Show me reached the desk")
            await page.click(f"#needs [data-job='{litho}'][data-job-act=show]")
            await _until(lambda: said, "the page said the tab is closed")
            return calls

    calls = run(go())
    assert calls == [("POST", f"/api/job/{fab}/show", {}), ("POST", f"/api/job/{litho}/show", {})], calls
    assert shown == [fab, litho]
    assert said == ["Its tab is closed. Press Carry on to open it again."], said
    assert desk.applier.runs[litho].status == "needs_you"  # (nothing else done to it)


def test_clear_unticks_every_opening_and_calls_nothing(srv):
    """Clear, beside Select recommended: every ticked opening unticked, Apply disabled again, and nothing sent to the
    desk (ticking is the page's own)."""
    desk = _idle_desk(srv)
    fit = {"score": 80, "reasons": ["title matches"], "concerns": [], "blocked": False, "recommended": True}
    desk.listings = [{"company": f"Example {name}", "title": "Technician", "url": f"https://careers.example.com/{i}",
                      "location": "Phoenix, AZ", "fit": fit} for i, name in enumerate(("Fab", "Litho", "Etch"))]

    async def go():
        async with _desk_page(desk) as (page, calls):
            await page.click("[data-act=select-recommended]")
            await page.wait_for_selector("#apply:text-is('Apply to 3 selected')")
            ticked = await page.locator("[data-pick]:checked").count()
            await page.click("[data-act=select-none]")
            await page.wait_for_selector("#apply:text-is('Apply to selected')")
            await page.wait_for_timeout(1700)  # (a refresh: nothing comes back ticked)
            return (calls, ticked, await page.locator("[data-pick]:checked").count(), await page.is_disabled("#apply"),
                    await page.inner_text("#apply-hint"))

    calls, ticked, left, disabled, hint = run(go())
    assert ticked == 3 and left == 0 and disabled, (ticked, left, disabled)
    assert hint == "Tick the jobs to apply to.", hint
    assert calls == [], calls
    assert not desk.applier.tasks


def test_stop_ends_the_review_round_and_the_banner_offers_it_again(srv):
    """Stop in the review round: the desk is told the round is over (round_end), the banner goes back to "Review them",
    and nothing is sent or marked."""
    desk, jobs = _stopped_desk(srv, {"Example Fab": dict(status="ready"),
                                     "Example Litho": dict(status="needs_you", need="your_submit")})
    a = desk.applier
    shown, ended = [], []

    async def focus(job_id):
        shown.append(job_id)
        return True
    a.focus = focus
    end_round = a.end_round
    a.end_round = lambda: (ended.append(True), end_round())
    a.submit_now = a.mark_applied = lambda job_id: pytest.fail("nothing is sent in this test")

    async def go():
        async with _desk_page(desk) as (page, calls):
            await page.click("#notices [data-act=review-round]")
            await page.wait_for_selector("#notices [data-act=review-stop]")
            await _until(lambda: shown, "the round brought the first one up")
            await page.click("#notices [data-act=review-stop]")
            await page.wait_for_selector("#notices [data-act=review-round]")
            await _until(lambda: ended, "the desk was told the round ended")
            await page.wait_for_timeout(1700)  # (a refresh: the round doesn't start again by itself)
            banner = await page.inner_text("#notices .waiting")
            return calls, banner, await page.locator("#notices [data-act=review-stop]").count()

    calls, banner, stops = run(go())
    first = jobs["Example Fab"]  # (the one stopped first)
    assert calls == [("POST", f"/api/job/{first}/round", {}), ("POST", "/api/job/0/round_end", {})], calls
    assert shown == [first] and ended == [True]
    assert "Reviewing" not in banner and "2 applications ready for your Submit" in banner and stops == 0, banner
    assert a.round_job is None  # (the desk's own start_round and end_round: no round held)


def test_this_week_opens_with_the_desks_last_7_days(srv, job_apply_home):
    """This week asks the desk for the last 7 days (/api/history?days=7) and shows what it says in its window; Close
    shuts it."""
    now = time.time()
    (job_apply_home / "runs.jsonl").write_text(json.dumps(_history_line(
        now, 0.5, 4.0, job_id=1, company="Example Fab", title="Technician", host="careers.example.com",
        outcome="ready", reason="Filled and waiting on the review page.")) + "\n", encoding="utf-8")
    desk = _idle_desk(srv)

    async def go():
        async with _desk_page(desk) as (page, calls):
            await page.click("#week-btn")
            await page.wait_for_selector("#week[open] #week-recent li")
            body = await page.inner_text("#week-body")
            await page.click("#week [data-act=week-close]")
            await page.wait_for_selector("#week[open]", state="detached")
            return calls, body

    calls, body = run(go())
    assert calls == [("GET", "/api/history?days=7", None)], calls
    assert "Filled up to their review page: 1 job. Sent by the desk: 0 jobs." in body, body
    assert "Example Fab · Technician" in body and "at its review page (4 min)" in body, body


def test_safety_opens_with_each_setting_as_the_desk_acts_on_it(srv, job_apply_home):
    """Safety asks the desk for its settings as they are now (/api/safety) and lists each one, by its key, in its
    window."""
    desk = _idle_desk(srv)
    lines = desk.safety()["lines"]

    async def go():
        async with _desk_page(desk) as (page, calls):
            await page.click("#safety-btn")
            await page.wait_for_selector("#safety[open] #safety-body li")
            keys = await page.eval_on_selector_all("#safety-body li", "ls => ls.map((l) => l.dataset.safety)")
            first = await page.inner_text("#safety-body li >> nth=0")
            await page.click("[data-act=safety-close]")
            await page.wait_for_selector("#safety[open]", state="detached")
            return calls, keys, first

    calls, keys, first = run(go())
    assert calls == [("GET", "/api/safety", None)], calls
    assert keys == [line["key"] for line in lines] and len(keys) > 3, keys
    assert (first.startswith(f"{lines[0]['name']}: {lines[0]['value']}.")
            and f"Where: {lines[0]['where']}." in first), first


def test_notes_opens_the_notes_as_the_one_issue_they_make(srv, job_apply_home):
    """Notes (N) asks the desk for its notes (/api/notes/show) and shows them as the issue they make, its GitHub link
    beside them; nothing is cleared by opening it."""
    for i in range(2):
        _note(i + 1, reason=f"stop {i}")
    desk = _idle_desk(srv)
    shown = report.notes()

    async def go():
        async with _desk_page(desk) as (page, calls):
            await page.click("#notes-btn:text-is('Notes (2)')")
            await page.wait_for_selector("#notes[open]")
            text = await page.inner_text("#notes-text")
            href = await page.get_attribute("#notes-open", "href")
            await page.click("[data-act=notes-close]")
            await page.wait_for_selector("#notes[open]", state="detached")
            return calls, text, href

    calls, text, href = run(go())
    assert calls == [("POST", "/api/notes/show", {})], calls
    assert text.strip() == shown["text"].strip() and href == shown["issue_url"], (text, href)
    assert report.notes_count() == 2


def test_what_and_where_opens_on_the_profiles_titles_and_places_and_cancel_saves_nothing(srv, job_apply_home):
    """What and where opens its window on what Find jobs searches now (the page's state: no call), and Cancel closes
    it with nothing saved or searched."""
    from job_apply import search

    search.set_search_area(places=["Mesa, AZ"], titles=["Equipment Technician", "Field Service Engineer"])
    desk = _idle_desk(srv)
    area = desk.state()["area"]
    assert area["titles"] == ["Equipment Technician", "Field Service Engineer"] and area["places"] == ["Mesa, AZ"], area
    searched = desk.search["at"]

    async def go():
        async with _desk_page(desk) as (page, calls):
            await page.wait_for_function("document.getElementById('area-btn').textContent.includes('Mesa, AZ')")
            await page.click("#area-btn")
            await page.wait_for_selector("#area[open]")
            titles = await page.locator("#area-titles .chip").all_inner_texts()
            places = await page.locator("#area-places .chip").all_inner_texts()
            lists = await page.eval_on_selector_all("[data-area-list]",
                                                    "bs => bs.map((b) => [b.dataset.areaList, b.checked])")
            await page.fill("#area-title-new", "Help Desk Technician")  # (typed, then Cancel: not kept)
            await page.click("[data-act=area-close]")
            await page.wait_for_selector("#area[open]", state="detached")
            await page.wait_for_timeout(500)
            return calls, titles, places, lists

    calls, titles, places, lists = run(go())
    assert calls == [], calls
    assert [t.replace("×", "").strip() for t in titles] == area["titles"], titles
    assert [p.replace("×", "").strip() for p in places] == area["places"], places
    assert [name for name, on in lists if on] == area["lists"] and len(lists) == len(area["choices"]), lists
    assert desk.state()["area"]["titles"] == area["titles"] and desk.search["at"] == searched


def test_an_optional_id_left_empty_on_its_card_goes_with_its_remember(srv, job_apply_home):
    """The review of the ID's change (LOW 1): an optional box for the person's ID at this employer, left empty on its
    questions card, is sent with its Remember with the card's other answers on Fill these in (ticked: left empty at
    this employer from then on). Its Remember starts unticked (the third review's LOW 2: leaving it empty for good is
    the person's own choice), and typing an ID ticks it. Carry on sends nothing of it (the second review's MEDIUM 1):
    left empty for that job alone."""
    from job_apply import config

    wwid = "WWID (if previously employed)"
    optional = {"id": "w", "kind": "text", "label": wwid, "required": False, "own_id": True, "employer_id": True}
    needed = {"id": "y", "kind": "text", "label": "Years in a cleanroom", "required": True}
    desk, jobs = _stopped_desk(srv, {"Example Fab": dict(status="needs_you", need="questions",
                                                         questions=[optional, needed]),
                                     "Example Litho": dict(status="needs_you", need="questions",
                                                           questions=[optional]),
                                     "Example Ops": dict(status="needs_you", need="questions",
                                                         questions=[optional])})
    fab, litho, ops = jobs["Example Fab"], jobs["Example Litho"], jobs["Example Ops"]
    said: list[str] = []

    async def go():
        async with _desk_page(desk, said) as (page, calls):
            card = page.locator(f'form[data-answer="{fab}"]')
            keep = card.locator(f'[data-remember="{wwid}"]')
            unticked = not await keep.is_checked()
            await keep.check()  # (the person's own choice: left empty at Example Fab)
            await card.locator('input[data-q="Years in a cleanroom"]').fill("3")
            await page.click(f"#needs [data-job='{fab}'][data-job-act=fill]")
            await _until(lambda: len(calls) == 1, lambda: calls)
            await page.locator(f'form[data-answer="{litho}"] input[data-q="{wwid}"]').fill("12345678")
            await page.click(f"#needs [data-job='{litho}'][data-job-act=fill]")
            await _until(lambda: len(calls) == 2, lambda: calls)
            await page.click(f"#needs [data-job='{ops}'][data-job-act=resume]")
            await _until(lambda: len(calls) == 3, lambda: calls)
            return calls, unticked

    calls, unticked = run(go())
    assert unticked
    assert calls == [("POST", "/api/answer", {"job_id": fab, "answers": [
                         {"label": wwid, "value": "", "remember": True},
                         {"label": "Years in a cleanroom", "value": "3", "remember": True}]}),
                     ("POST", "/api/answer", {"job_id": litho, "answers": [
                         {"label": wwid, "value": "12345678", "remember": True}]}),
                     ("POST", f"/api/job/{ops}/resume", {})], calls
    assert said == []
    assert [(a["question"], a["answer"], a["at"]) for a in config.saved_answers() if a["question"] == wwid] == [
        (wwid, "12345678", "Example Litho"), (wwid, None, "Example Fab")]
    assert desk.applier.runs[ops].asked_once  # (Carry on: for that job alone)
