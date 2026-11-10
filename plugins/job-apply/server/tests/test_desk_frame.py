"""The Job Desk's frame (the design's Version A, A3 to A6): a side menu that leads to seven pages, one top bar for all
of them, each card on its own page, the queue's card and the person in the menu, and the new Settings and Help cards.
Each test opens the real page in a browser. Made-up employers only: the repo is public."""

import asyncio
import copy
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from conftest import browser_available, open_desk, person_browser, run
from test_desk import _fit, _note, _preflight_desk, _stopped_desk, _until
from test_desk_buttons import _POLLS, _idle_desk

from job_apply.desk import Desk

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

DESK_FIXTURES = Path(__file__).parent / "fixtures" / "desk"
# The side menu's pages, in its order, each with its heading in the top bar
PAGES = {"home": "Home", "openings": "Openings", "applications": "Applications", "answers": "Your answers",
         "working": "What's working", "settings": "Settings", "help": "Help"}
# The cards each page shows (each by its heading's id, in the page's order) on a desk with a job stopped for the person
# and nothing sent in yet: Home's three cards (not the idle one), no What's working card
CARDS = {"home": ["needs-h", "done-h", "work-h"], "openings": ["list-h"], "applications": ["apps-h"],
         "answers": ["bank-h"], "working": [],
         "settings": ["profile-h", "safety-card-h", "searching-h", "pw-h", "phone-h", "alerts-h", "program-h"],
         "help": ["how-h", "notes-card-h"]}
# And the top bar's controls it shows while a search runs (Stop search shows only then, on the pages that have it)
SEARCH = ["searched", "stop-search", "find"]
TOP = {"home": ["searched", "mode", "week-btn", "stop-search", "find"],
       "openings": ["area-btn", "searched", "mode", "stop-search", "find"],
       "applications": ["area-btn", "searched", "mode", "stop-search", "find"],
       "answers": ["mode"], "working": ["mode", "week-btn"], "settings": ["mode"], "help": ["mode"]}
QUIT_ASKS = "Quit the Job Desk? Jobs it is working on stop where they are. The queue is kept for next time."

# What a page shows: its name, heading and address, the menu link marked as the current page, the cards in sight, the
# top bar's controls in sight, and the lines that stand in for a hidden card
_SHOWN = """() => {
  const inSight = (el) => el.getClientRects().length > 0;
  return {
    page: document.body.dataset.page, title: document.getElementById('page-title').textContent, hash: location.hash,
    current: [...document.querySelectorAll('.nav a[aria-current=page]')].map((a) => a.getAttribute('href')),
    cards: [...document.querySelectorAll('section.card')].filter(inSight).map((c) => c.getAttribute('aria-labelledby')),
    top: ['area-btn', 'searched', 'mode', 'week-btn', 'stop-search', 'find']
      .filter((id) => inSight(document.getElementById(id))),
    lines: [...document.querySelectorAll('.page-none')].filter(inSight).map((p) => p.textContent.trim()),
  };
}"""


def _state(name: str) -> dict:
    return json.loads((DESK_FIXTURES / name).read_text(encoding="utf-8"))


def _canned(srv, monkeypatch, state: dict) -> Desk:
    """A desk whose page draws `state` and nothing else (Desk.state replaced by it, as the cards' snapshots do)."""
    monkeypatch.setattr(Desk, "state", lambda self: copy.deepcopy(state))
    desk = Desk(srv)
    desk.applier.start = lambda: None  # (nothing is worked: the page shows only the canned state)
    return desk


async def _at(page, name: str) -> dict:
    """The menu's link for the page `name` pressed, and what shows then."""
    await page.click(f'.nav a[href="#{name}"]')
    await page.wait_for_function(f"document.body.dataset.page === '{name}'")
    return await page.evaluate(_SHOWN)


def test_the_page_opens_on_home_and_each_menu_link_shows_its_pages_cards(srv):
    """The design's A8, test 6. With no page in its address the desk opens on Home. Each of the menu's seven links
    shows its page: its heading in the top bar, its cards and no other page's, its top bar controls, its own address,
    and itself marked as the current page. An address that names no page shows Home. The address survives a reload."""
    desk, _ = _stopped_desk(srv, {"Example Fab": dict(status="needs_you", need="stuck")})
    desk.search.update(status="running", started=time.time())  # (said by the top bar; nothing is searched here)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.goto(desk.url)  # (as the desk opens it: no page named)
                await page.wait_for_selector("#plugin-version:not(:empty)")
                seen = {"opened": await page.evaluate(_SHOWN)}
                for name in PAGES:
                    seen[name] = await _at(page, name)
                await _at(page, "settings")
                await page.reload()
                await page.wait_for_selector("#plugin-version:not(:empty)")
                seen["reloaded"] = await page.evaluate(_SHOWN)
                for unknown in ("nowhere", "constructor", "home/"):
                    await page.goto(f"{desk.url}#{unknown}")
                    await page.reload()
                    await page.wait_for_selector("#plugin-version:not(:empty)")
                    seen[unknown] = await page.evaluate(_SHOWN)
                seen["frame"] = await page.evaluate("""() => ({
                    stopped: document.getElementById('stopped').parentElement === document.body,
                    sprite: document.body.firstElementChild.tagName.toLowerCase(),
                    old: document.querySelectorAll('header, footer').length,
                    frame: [...document.querySelector('.app').children].map((el) => el.className),
                    notices: document.getElementById('notices').parentElement.className,
                    pages: [...document.querySelectorAll('.content > .page')].map((p) => p.dataset.pages),
                    headings: [...document.querySelectorAll('.top h1')].map((h) => h.id),
                })""")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())

    def shown(name: str, address: str) -> dict:
        return {"page": name, "title": PAGES[name], "hash": address, "current": [f"#{name}"], "cards": CARDS[name],
                "top": TOP[name],
                "lines": ["Nothing sent yet. Once you have applied, this page says what is working for you."]
                if name == "working" else []}

    assert seen["opened"] == shown("home", ""), seen["opened"]
    for name in PAGES:
        assert seen[name] == shown(name, f"#{name}"), (name, seen[name])
    assert seen["reloaded"] == shown("settings", "#settings"), seen["reloaded"]
    for unknown in ("nowhere", "constructor", "home/"):
        assert seen[unknown] == shown("home", f"#{unknown}"), (unknown, seen[unknown])
    assert seen["frame"] == {"stopped": True, "sprite": "svg", "old": 0, "frame": ["side", "main"],
                             "notices": "content",
                             "pages": ["home", "openings", "applications", "answers", "working", "settings", "help"],
                             "headings": ["page-title"]}, seen["frame"]


def test_a_search_that_isnt_running_shows_no_stop_search_on_any_page(srv):
    """Each top bar control keeps its own show-and-hide beside its page's: Stop search shows on no page while no
    search runs, and Find jobs says so."""
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk)
                tops = {name: (await _at(page, name))["top"] for name in PAGES}
                find = await page.inner_text("#find") if await page.is_visible("#find") else None
                await _at(page, "openings")
                return tops, find, await page.inner_text("#find"), await page.is_enabled("#find")
        finally:
            await desk.stop()

    tops, hidden_find, find, enabled = run(go())
    assert tops == {name: [c for c in TOP[name] if c != "stop-search"] for name in PAGES}, tops
    assert hidden_find is None and find.strip() == "Find jobs" and enabled, (hidden_find, find, enabled)


def test_applications_chooses_my_applications_and_recommended_leads_back_to_openings(srv):
    """The design's A8, test 7, as Version B has it: the Applications page lists the applications in its own list
    (B6's #app-rows) with no Openings tab in sight, and the Openings tabs and the address stay in step: pressing All,
    then Recommended, on Openings stays on #openings; the menu's Applications link (Version B's My applications, no
    longer a tab) leads to #applications; and the menu's Openings link leads back to Recommended (the tab as it was
    left: B2's route() no longer sets it). An opening is ticked on Openings, so the apply bar (B5) shows there."""
    desk = _idle_desk(srv)
    desk.listings = [{"company": "Example Fab", "title": "Field Service Engineer", "location": "Phoenix, AZ",
                      "url": "https://careers.example.com/fab/1", "fit": _fit(80)},
                     {"company": "Example Litho", "title": "Accountant", "location": "Phoenix, AZ",
                      "url": "https://careers.example.com/litho/2", "fit": _fit(30)}]
    sent = srv.add_job(url="https://careers.example.com/etch/3", title="Process Technician",
                       company="Example Etch")["job"]
    srv.update_job(sent["id"], status="applied")
    # (the tabs and the list in sight: the Openings page's #rows, or the Applications page's #app-rows)
    read = """() => ({
        hash: location.hash, title: document.getElementById('page-title').textContent,
        pressed: [...document.querySelectorAll('[data-filter][aria-pressed=true]')]
          .filter((b) => b.getClientRects().length > 0).map((b) => b.dataset.filter),
        rows: [...document.querySelectorAll('#rows .title, #app-rows .title')]
          .filter((t) => t.getClientRects().length > 0).map((t) => t.textContent),
        apply: document.getElementById('apply').getClientRects().length > 0,
    })"""

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk, "applications")
                await page.wait_for_selector("#app-rows li.row.app")
                seen = {"applications": await page.evaluate(read)}
                # (an application's row is .row.app: a row of the list, not the page's frame, which is .app too)
                seen["row"] = await page.evaluate("document.querySelector('#app-rows li.row.app').offsetHeight")
                await page.click('.nav a[href="#openings"]')  # (the Openings tabs are on the Openings page)
                await page.wait_for_selector("#rows [data-pick]")
                await page.check("[data-pick='https://careers.example.com/fab/1']")  # (the apply bar shows)
                await page.click("[data-filter=all]")
                await page.wait_for_selector("#rows li.row >> nth=1")
                seen["all"] = await page.evaluate(read)
                await page.click("[data-filter=recommended]")
                await page.wait_for_function("location.hash === '#openings'")
                await page.wait_for_selector("#rows li.row >> nth=1", state="detached")
                seen["recommended"] = await page.evaluate(read)
                await page.click('.nav a[href="#applications"]')  # (My applications is the Applications page)
                await page.wait_for_function("location.hash === '#applications'")
                seen["mine"] = await page.evaluate(read)
                await page.wait_for_timeout(1700)  # (a refresh: the tab and the address stay as they are)
                seen["later"] = await page.evaluate(read)
                await page.click('.nav a[href="#openings"]')
                await page.wait_for_selector("#rows [data-pick]")
                seen["menu"] = await page.evaluate(read)
                await page.go_back()
                await page.wait_for_selector("#app-rows li.row.app")
                seen["back"] = await page.evaluate(read)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    # (Version B: no My applications tab, so no tab is pressed on Applications)
    mine = {"hash": "#applications", "title": "Applications", "pressed": [], "rows": ["Process Technician"],
            "apply": False}
    recommended = {"hash": "#openings", "title": "Openings", "pressed": ["recommended"],
                   "rows": ["Field Service Engineer"], "apply": True}
    assert seen["applications"] == mine, seen["applications"]
    assert seen["recommended"] == recommended, seen["recommended"]
    assert seen["all"] == {**recommended, "pressed": ["all"], "rows": ["Field Service Engineer", "Accountant"]}, seen
    assert seen["mine"] == mine and seen["later"] == mine and seen["back"] == mine, seen
    assert seen["menu"] == recommended, seen["menu"]
    assert 40 < seen["row"] < 120, seen["row"]


def _numbers_desk(srv) -> tuple[Desk, dict[str, int]]:
    """A desk with something behind each of the menu's numbers, and what each should say: three recommended openings
    not begun (beside one not recommended, two begun and stopped for the person, one sent in, one marked Not
    interested), one application, the common questions the profile leaves unanswered, and two notes."""
    from job_apply.pipeline import Run

    desk = _idle_desk(srv)
    names = ["Example Fab", "Example Litho", "Example Etch", "Example Low", "Example Stuck", "Example Questions",
             "Example Sent", "Example Hidden"]
    urls = {name: f"https://careers.example.com/{i}" for i, name in enumerate(names)}
    desk.listings = [{"company": name, "title": "Technician", "url": urls[name], "location": "Phoenix, AZ",
                      "fit": _fit(30 if name == "Example Low" else 80)} for name in names]
    for i, (name, need) in enumerate((("Example Stuck", "stuck"), ("Example Questions", "questions"))):
        job = srv.add_job(url=urls[name], title="Technician", company=name)["job"]
        r = Run(job["id"], "Technician", name, status="needs_you", need=need, reason="Waiting.")
        r.paused_at = 1000.0 + i
        desk.applier.runs[job["id"]] = r
    sent = srv.add_job(url=urls["Example Sent"], title="Technician", company="Example Sent")["job"]
    srv.update_job(sent["id"], status="applied")
    desk.not_interested(urls["Example Hidden"], "pay")
    for i in range(2):
        _note(i + 1, reason=f"stop {i}")
    unanswered = sum(1 for q in desk.state()["bank"] if q.get("answer") in (None, ""))
    assert unanswered > 0
    return desk, {"home": 2, "openings": 3, "applications": 1, "answers": unanswered, "help": 2}


_NUMBERS = """() => Object.fromEntries([...document.querySelectorAll('.nav a')].map((a) => {
  const n = a.querySelector('.count');
  return [a.getAttribute('href').slice(1), n && n.getClientRects().length ? [n.textContent, n.className] : null];
}))"""


def test_the_menus_numbers_match_the_cards(srv, job_apply_home):
    """The design's A8, test 8. Each number beside a menu item is the one its page's card gives: Home's, in amber, the
    Needs you card's; Openings', the openings the Recommended tab lists as not started, with no filter on (the ones
    begun, sent in or marked Not interested aren't waiting to be looked at); Applications', My applications'; Your
    answers', the common questions not answered yet; Help's, the notes waiting to be filed. What's working and
    Settings have none."""
    desk, want = _numbers_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk)
                await page.wait_for_selector("#nav-help:not([hidden])")
                numbers = await page.evaluate(_NUMBERS)
                cards = {"home": await page.inner_text("#needs-count")}
                await _at(page, "openings")
                await page.click("#more > summary")  # (Status is under More filters)
                cards["openings bar"] = " ".join((await page.inner_text("[data-stop=new]")).split())
                await page.click("[data-stop=new]")
                await page.wait_for_selector("[data-stop=new][aria-pressed=true]")
                cards["openings"] = await page.locator("#rows li.row").count()
                await _at(page, "applications")  # (My applications is the Applications page)
                cards["applications"] = " ".join((await page.inner_text("[data-app-group=all]")).split())
                await _at(page, "answers")
                cards["answers"] = await page.inner_text("#bank-count")
                await _at(page, "help")
                cards["help"] = await page.inner_text("#notes-btn")
                await browser.close()
                return numbers, cards
        finally:
            await desk.stop()

    numbers, cards = run(go())
    assert numbers == {"home": [str(want["home"]), "count warn"], "openings": [str(want["openings"]), "count"],
                       "applications": [str(want["applications"]), "count"],
                       "answers": [str(want["answers"]), "count"], "working": None, "settings": None,
                       "help": [str(want["help"]), "count"]}, numbers
    answered, total = (int(n) for n in cards["answers"].removesuffix(" answered").split(" of "))
    assert cards == {"home": str(want["home"]), "openings bar": f"Not started {want['openings']}",
                     "openings": want["openings"], "applications": f"All {want['applications']}",
                     "answers": f"{total - want['answers']} of {total} answered",
                     "help": f"Notes ({want['help']})"}, cards
    assert answered == total - want["answers"]


def test_a_menu_number_is_hidden_at_zero(srv, monkeypatch):
    """A number beside a menu item shows only when there's something to count: on a desk with no opening, no job, no
    application, every common question answered and no note, none shows."""
    state = _state("state-openings.json")
    state.update(listings=[], others=[], applications=[], notes=0)
    for q in state["bank"]:
        q["answer"] = q.get("answer") or "Yes"
    desk = _canned(srv, monkeypatch, state)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk)
                return await page.evaluate(_NUMBERS), await page.locator(".nav a .count").count()
        finally:
            await desk.stop()

    numbers, counts = run(go())
    assert numbers == dict.fromkeys(PAGES), numbers
    assert counts == 5  # (the five that can show are there, each hidden)


def test_quit_the_job_desk_shows_only_on_a_desk_run_on_its_own(srv):
    """The design's A8, test 10. Settings' This program card says the version. On a desk inside Claude it says the desk
    stops with Claude, and has no button. On a desk run on its own (job-apply-desk) it says closing the window doesn't
    stop the desk, and has Quit the Job Desk: pressed, it asks first, the desk is told, and the page shows only that
    the desk has stopped."""
    desk = _idle_desk(srv)
    version = desk.state()["version"]
    said: list[str] = []
    calls: list[tuple[str, str]] = []
    card = "section[aria-labelledby=program-h]"

    def record(request) -> None:
        path = urlsplit(request.url).path
        if path.startswith("/api/") and path not in _POLLS:
            calls.append((request.method, path))

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                page.on("request", record)
                page.on("dialog", lambda d: (said.append(d.message), asyncio.ensure_future(d.accept())))
                await open_desk(page, desk, "settings")
                await page.wait_for_selector(f"{card} :text('The desk runs inside Claude here.')")
                inside = (" ".join((await page.inner_text(card)).split()),
                          await page.locator(f"{card} [data-act=quit]").count(),
                          await page.is_visible(f"{card} [data-act=quit]"))
                await _at(page, "home")
                elsewhere = await page.is_visible("[data-act=quit]")
                desk.standalone = True  # (as job-apply-desk sets it)
                await _at(page, "settings")
                await page.wait_for_selector(f"{card} [data-act=quit]:visible")
                own = " ".join((await page.inner_text(card)).split())
                # (the window's own close noted: a page that closed itself would be gone before it's read)
                await page.evaluate("() => { window.close = () => { window.closedItself = true; }; }")
                await page.click(f"{card} [data-act=quit]")
                await page.wait_for_selector("#stopped", state="visible")
                shown = [line.strip() for line in (await page.inner_text("body")).splitlines() if line.strip()]
                return inside, elsewhere, own, shown, await page.evaluate("window.closedItself")
        finally:
            await desk.stop()

    inside, elsewhere, own, shown, closed = run(go())
    assert inside == (f"This program Version {version}. The desk runs inside Claude here. It stops when you close "
                      "Claude.", 1, False), inside
    assert not elsewhere
    assert own == (f"This program Version {version}. Closing the window doesn't stop the desk. Jobs it is working on "
                   "carry on, and opening Job Desk again shows the window. Quit the Job Desk"), own
    assert said == [QUIT_ASKS] and calls == [("POST", "/api/quit")] and desk.quitting.is_set(), (said, calls)
    assert shown == ["The Job Desk has stopped", "You can close this window. Open Job Desk again to carry on."], shown
    assert closed is True


def test_the_notices_show_on_every_page(srv):
    """The design's A8, test 11. What the desk has to say at the top (a wait for the person in yellow, a problem in
    red) shows above every page's cards, not only Home's, and a press on one works from any page."""
    desk, jobs = _stopped_desk(srv, {"Example Fab": dict(status="needs_you", need="sign_in", blocking=True)})
    desk.search.update(status="failed", error="SearchError: HTTP 500")
    shown: list[int] = []

    async def focus(job_id):
        shown.append(job_id)
        return True
    desk.applier.focus = focus
    read = """() => [...document.querySelectorAll('#notices > *')].filter((n) => n.getClientRects().length)
        .map((n) => [n.className, n.innerText.split(/[:.]/)[0]])"""
    waits, problem = ["waiting", "Waiting for you in the desk's browser window"], ["notice bad", "Problem"]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk)
                await page.wait_for_selector("#notices .notice.bad")
                seen = {}
                for name in PAGES:
                    await _at(page, name)
                    seen[name] = await page.evaluate(read)
                above = await page.evaluate("""() => { const n = document.getElementById('notices');
                    return [...document.querySelectorAll('.content > .page')]
                        .every((p) => n.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING); }""")
                await _at(page, "help")
                await page.click("#notices .waiting [data-job-act=show]")
                await _until(lambda: shown, about=lambda: shown)
                await browser.close()
                return seen, above
        finally:
            await desk.stop()

    seen, above = run(go())
    assert waits in seen["home"] and problem in seen["home"], seen["home"]
    assert seen == dict.fromkeys(PAGES, seen["home"]), seen
    assert above and shown == [jobs["Example Fab"]]


# ------------------------------------------------------------------ the side menu: the queue's card, the person

_QUEUE = """() => { const card = document.getElementById('queue-card'), bar = document.getElementById('queue-bar');
  if (!card.getClientRects().length) return null;
  const text = (id) => document.getElementById(id).getClientRects().length
    ? document.getElementById(id).innerText.trim() : null;
  return {said: text('queue-said'), more: text('queue-more'), filling: document.getElementById('queue-dot').className,
          bar: bar.querySelector('i').style.width,
          of: [bar.getAttribute('aria-valuenow'), bar.getAttribute('aria-valuemax')],
          pause: text('pause-btn'), clear: text('clear-queue-btn')}; }"""


def _queue(state: dict, statuses: list[str], **more) -> dict:
    """`state` with its jobs' runs set to `statuses` (the first ones; the rest are taken off the desk's hands)."""
    state = copy.deepcopy(state)
    rows = [r for r in [*state["listings"], *state["others"]] if r.get("run")]
    for row, status in zip(rows, statuses):
        row["run"]["status"] = status
    for row in rows[len(statuses):]:
        row["run"] = None
    state.update(more)
    return state


def test_the_queue_card_says_how_the_queue_is_doing(srv, monkeypatch):
    """The side menu's queue card, in place of the header's two queue buttons: what the queue is doing in two lines
    (paused; being filled, with how many more wait and how many at a time; waiting to start), a dot that is green
    while jobs are being filled, a bar for the share of the jobs taken as far as the desk can, and under it Pause
    queue or Carry on with the queue, and Clear the queue. With neither button to show, the card isn't there."""
    ready, notes = _state("state-ready.json"), _state("state-notes.json")
    two = copy.deepcopy(ready)
    two["settings"]["jobs_at_once"] = 2
    cases = {
        # (the saved state: one being filled, two waiting, four the desk is done with for now)
        "filling": ready,
        "filling, none waiting": _queue(two, ["running", "running", "ready"]),
        # (the saved state: the queue paused, one waiting, two the desk is done with for now)
        "paused": notes,
        "paused, none waiting": _queue(notes, ["needs_you", "failed", "ready"], paused=True),
        "waiting": _queue(ready, ["queued", "queued", "ready", "ready"], clearable=2),
        "waiting, the queue kept elsewhere": _queue(ready, ["queued"], queue_note="Another Job Desk keeps the queue."),
        "only jobs held before they begin": _queue(ready, ["needs_you", "ready"], clearable=1),
        "nothing": _state("state-openings.json"),
    }
    state: dict = {}
    desk = _canned(srv, monkeypatch, state)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                seen = {}
                for name, case in cases.items():
                    state.clear()
                    state.update(copy.deepcopy(case))
                    await page.goto("about:blank")
                    await open_desk(page, desk, "settings")  # (the menu is the same on every page)
                    seen[name] = await page.evaluate(_QUEUE)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["filling"] == {"said": "Working on 1 job", "more": "2 more waiting · 1 at a time", "filling": "dot on",
                               "bar": "57%", "of": ["4", "7"], "pause": "Pause queue", "clear": None}, seen["filling"]
    assert seen["filling, none waiting"] == {"said": "Working on 2 jobs", "more": "2 at a time", "filling": "dot on",
                                             "bar": "33%", "of": ["1", "3"], "pause": "Pause queue", "clear": None}
    assert seen["paused"] == {"said": "Paused", "more": "1 waiting", "filling": "dot", "bar": "67%", "of": ["2", "3"],
                              "pause": "Carry on with the queue (1 waiting)", "clear": None}, seen["paused"]
    assert seen["paused, none waiting"] == {"said": "Paused", "more": "Nothing waiting", "filling": "dot",
                                            "bar": "100%", "of": ["3", "3"], "pause": "Carry on with the queue",
                                            "clear": None}, seen["paused, none waiting"]
    assert seen["waiting"] == {"said": "2 waiting", "more": "Starting soon", "filling": "dot", "bar": "50%",
                               "of": ["2", "4"], "pause": "Pause queue", "clear": "Clear the queue"}, seen["waiting"]
    kept = seen["waiting, the queue kept elsewhere"]
    assert (kept["said"], kept["more"]) == ("1 waiting", "Another Job Desk keeps the queue."), kept
    assert seen["only jobs held before they begin"] == {
        "said": "Waiting for you", "more": "1 job not started yet", "filling": "dot", "bar": "100%", "of": ["2", "2"],
        "pause": None, "clear": "Clear the queue"}, seen["only jobs held before they begin"]
    assert seen["nothing"] is None


def test_the_person_in_the_menu_is_their_initials_name_and_the_version(srv, monkeypatch):
    """The foot of the side menu is one button for the person's profile: a circle with their initials (the first
    letters of the first and last words of their name; a person's icon while the profile has no name), the name (or
    Set up your profile), and the version. Pressed, it opens the profile's window."""
    state = _state("state-openings.json")
    desk = _canned(srv, monkeypatch, state)
    read = """() => { const me = document.querySelector('.side button.me');
        return [me.dataset.act, me.title, document.getElementById('me-avatar').textContent.trim(),
                document.querySelectorAll('#me-avatar use[href="#i-user"]').length,
                document.getElementById('who').textContent, document.getElementById('plugin-version').textContent]; }"""

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                seen = {}
                for name in ("Sam Rivera", "  mary ann  o'neil ", "Cher", "<b>Ada</b> Ng", ""):
                    state["profile"]["name"] = name
                    await page.goto("about:blank")
                    await open_desk(page, desk, "help")
                    seen[name] = await page.evaluate(read)
                await page.click(".side button.me")
                await page.wait_for_selector("#setup[open]")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["Sam Rivera"] == ["setup", "Your profile", "SR", 0, "Sam Rivera", "Version 0.0.0"], seen
    assert seen["  mary ann  o'neil "][2:5] == ["MO", 0, "mary ann  o'neil"], seen
    assert seen["Cher"][2:5] == ["C", 0, "Cher"], seen
    assert seen["<b>Ada</b> Ng"][2:5] == ["<N", 0, "<b>Ada</b> Ng"], seen  # (drawn as text, never as markup)
    assert seen[""] == ["setup", "Your profile", "", 1, "Set up your profile", "Version 0.0.0"], seen


def test_a_page_whose_card_is_hidden_says_so(srv, monkeypatch):
    """Your answers and What's working each hold one card, hidden while it has nothing to show, and Help's notes
    button shows only with notes to file: each then says so in a line of its own, which goes once there's something."""
    full, empty = _state("state-notes.json"), _state("state-openings.json")
    full["outcomes"], empty["outcomes"] = empty["outcomes"], None
    empty.update(bank=[], notes=0)
    state: dict = {}
    desk = _canned(srv, monkeypatch, state)
    read = """() => [...document.querySelectorAll('#bank-none, #working-none, #notes-none')]
        .filter((p) => p.getClientRects().length).map((p) => p.textContent.trim())"""

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                seen = {}
                for name, case in (("empty", empty), ("full", full)):
                    state.clear()
                    state.update(copy.deepcopy(case))
                    await page.goto("about:blank")
                    await open_desk(page, desk)
                    seen[name] = {}
                    for at in ("answers", "working", "help"):
                        await _at(page, at)
                        seen[name][at] = await page.evaluate(read)
                    seen[name]["notes"] = await page.is_visible("#notes-btn")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["empty"] == {"answers": ["No common questions to show yet."],
                             "working": ["Nothing sent yet. Once you have applied, this page says what is working for "
                                         "you."],
                             "help": ["No notes right now."], "notes": False}, seen["empty"]
    assert seen["full"] == {"answers": [], "working": [], "help": [], "notes": True}, seen["full"]


def test_the_new_settings_and_help_cards_say_the_designs_words(srv):
    """Settings' four new cards and Help's two, each with the design's words (A5) and its control: Edit your profile
    and See your safety settings open their windows; Alerts on this computer holds the alerts button; Help says how
    the desk works in four numbered steps, then where answers are kept."""
    desk = _idle_desk(srv)

    def card(heading: str) -> str:
        return f"section.card[aria-labelledby={heading}]"

    async def words(page, heading: str) -> str:
        return " ".join((await page.inner_text(card(heading))).split())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk, "settings")
                seen = {h: await words(page, h) for h in ("profile-h", "safety-card-h", "alerts-h")}
                seen["alerts"] = await page.locator(f"{card('alerts-h')} #alerts[data-act=alerts]").count()
                await page.click(f"{card('safety-card-h')} #safety-btn")
                await page.wait_for_selector("#safety[open] #safety-body li")
                await page.click("[data-act=safety-close]")
                await page.click(f"{card('profile-h')} #setup-btn")
                await page.wait_for_selector("#setup[open]")
                await page.click("[data-act=setup-close]")
                await _at(page, "help")
                seen["how"] = await page.locator(f"{card('how-h')} ol > li").all_inner_texts()
                seen["how-h"] = await words(page, "how-h")
                seen["notes-card-h"] = await words(page, "notes-card-h")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["profile-h"] == ("Your profile Your details, work history and education, and the answers that go on "
                                 "every application. Edit your profile"), seen["profile-h"]
    assert seen["safety-card-h"] == ("Safety What the desk may do for you and what it never does, as it is set now. "
                                     "See your safety settings"), seen["safety-card-h"]
    assert seen["alerts-h"].startswith("Alerts on this computer A note on your screen when a job needs you."), seen
    assert seen["alerts"] == 1
    assert [" ".join(line.split()) for line in seen["how"]] == [
        "Find jobs searches the career sites of the employers on your lists, and ranks what it finds against your "
        "profile.",
        "You pick openings and press Apply to selected. The desk fills each application in the desk's browser window, "
        "up to its review page.",
        "When a site needs you (a question, a sign-in, a bot check), the job waits under Needs you on the Home page.",
        'Nothing is submitted until you press Submit, unless you turned on "Submit for me". LinkedIn and Indeed are '
        "always submitted by you."], seen["how"]
    assert seen["how-h"].startswith("How the desk works Find jobs searches") and seen["how-h"].endswith(
        'Answers you give here are saved in ~/.job-apply/answers.yaml unless you untick "remember".'), seen["how-h"]
    assert seen["notes-card-h"] == ("Notes on the desk's stops Each time a job stops on something the desk most likely "
                                    "got wrong, it takes a note. Filing the notes is how the next version gets better. "
                                    "No notes right now."), seen["notes-card-h"]


# ------------------------------------------------------------------ narrow windows, and what leads across pages

_SIDE = """() => { const inSight = (el) => el.getClientRects().length > 0, side = document.querySelector('.side');
  const who = document.getElementById('who').getBoundingClientRect(), menu = side.getBoundingClientRect();
  const there = document.elementFromPoint(who.left + who.width / 2, who.top + who.height / 2);
  return {width: Math.round(menu.width), height: Math.round(menu.height), window: innerHeight,
    labels: [...side.querySelectorAll('.nav a .label')].filter(inSight).map((l) => l.textContent),
    names: [...side.querySelectorAll('.nav a')].map((a) => [a.title, a.getAttribute('aria-label')]),
    icons: [...side.querySelectorAll('.nav a')].filter((a) => inSight(a.querySelector('svg.ico'))).length,
    queue: inSight(document.getElementById('queue-card')), brand: inSight(side.querySelector('.brand b')),
    name: !!there && there.id === 'who'}; }"""


def test_a_narrow_window_shows_the_menus_icons_only_and_every_page_fits(srv, monkeypatch):
    """The design's A3: 1,024 px wide or more, the side menu is 244 px wide, with each page's name, the queue's card
    and the person's name. Under that it is 64 px of icons, each link's name kept as its tooltip and its label for a
    screen reader, the queue's card only its Pause queue icon (that test's below), without the name. The menu fills
    the window's height, and no page scrolls sideways, down to a phone's width."""
    desk = _canned(srv, monkeypatch, _state("state-ready.json"))  # (jobs under way: the queue's card has words)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                seen = {}
                for width in (1280, 1024, 1023, 390):
                    page = await browser.new_page(viewport={"width": width, "height": 800})
                    await open_desk(page, desk)
                    seen[width] = await page.evaluate(_SIDE)
                    seen[width]["sideways"] = {}
                    for name in PAGES:
                        await _at(page, name)
                        seen[width]["sideways"][name] = await page.evaluate(
                            "document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    seen[width]["current"] = await page.get_attribute(".nav a[aria-current=page]", "href")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    names = [[name, name] for name in PAGES.values()]
    for width in (1280, 1024):
        assert seen[width] == {"width": 244, "height": 800, "window": 800, "labels": list(PAGES.values()),
                               "names": names, "icons": 7, "queue": True, "brand": True, "name": True,
                               "sideways": dict.fromkeys(PAGES, 0), "current": "#help"}, (width, seen[width])
    for width in (1023, 390):
        assert seen[width] == {"width": 64, "height": 800, "window": 800, "labels": [], "names": names, "icons": 7,
                               "queue": True, "brand": False, "name": False, "sideways": dict.fromkeys(PAGES, 0),
                               "current": "#help"}, (width, seen[width])


@pytest.mark.parametrize("ats, url, page_name, heading", [
    ("workday", "https://acme.wd1.myworkdayjobs.com/External/job/Phoenix-AZ/Technician_R1", "settings", "pw-h"),
    ("greenhouse", "https://boards.greenhouse.io/example/jobs/1", "answers", "bank-h"),
], ids=["a site password", "the common questions"])
def test_see_to_these_first_leads_to_the_page_of_the_card_it_names(srv, job_apply_home, ats, url, page_name, heading):
    """Before Apply sends jobs off it says what will stop them, and See to these first leads to the card for the first
    thing said. That card is on another page than the openings now (Site passwords on Settings, Common questions on
    Your answers): the desk goes to that page, the address with it, and the card is in sight."""
    desk = _preflight_desk(srv, url, ats)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1200, "height": 700})
                await open_desk(page, desk, "openings")
                await page.click("[data-act=select-recommended]")
                await page.click("#apply")
                await page.wait_for_selector("#preflight[open]")
                await page.keyboard.press("Enter")  # (See to these first)
                await page.wait_for_selector("#preflight", state="hidden")
                await page.wait_for_function(f"document.body.dataset.page === '{page_name}'")
                await page.wait_for_timeout(300)  # (the address's own change heard: the card stays in sight)
                seen = await page.evaluate(f"""() => {{ const h = document.getElementById('{heading}');
                    const r = h.getBoundingClientRect();
                    return {{hash: location.hash, title: document.getElementById('page-title').textContent,
                             shown: h.getClientRects().length > 0, top: r.top, bottom: r.bottom, window: innerHeight,
                             focused: document.activeElement.id}}; }}""")
                await browser.close()
                return seen, list(desk.applier.tasks)
        finally:
            await desk.stop()

    seen, tasks = run(go())
    assert (seen["hash"], seen["title"], seen["shown"]) == (f"#{page_name}", PAGES[page_name], True), seen
    assert 64 <= seen["top"] and seen["bottom"] <= seen["window"], seen  # (under the top bar, in the window)
    assert seen["focused"] == ("pw" if page_name == "settings" else ""), seen
    assert tasks == []  # (nothing was applied to)


# The queue's card as it shows: each of its parts in sight or not, the Pause queue button's words (and whether they're
# drawn), title, label and icon, and its width beside the menu's links
_NARROW_QUEUE = """() => { const inSight = (id) => document.getElementById(id).getClientRects().length > 0;
  const btn = document.getElementById('pause-btn'), use = btn.querySelector('svg.ico use');
  const drawn = document.createRange();
  drawn.selectNodeContents(btn.lastChild);
  return {card: inSight('queue-card'), said: inSight('queue-said'), more: inSight('queue-more'),
          bar: inSight('queue-bar'), clear: inSight('clear-queue-btn'), pause: inSight('pause-btn'),
          words: btn.textContent.trim(), drawn: Math.round(drawn.getBoundingClientRect().width) > 0, title: btn.title,
          label: btn.getAttribute('aria-label'),
          icon: use && use.getClientRects().length ? use.getAttribute('href') : null,
          width: Math.round(btn.getBoundingClientRect().width),
          link: Math.round(document.querySelector('.nav a').getBoundingClientRect().width)}; }"""
_PAUSE_TITLE = ("The job under way finishes what it is doing; no other job starts until you press Carry on with the "
                "queue")


def test_a_narrow_window_keeps_pause_queue_as_an_icon_in_the_menu(srv):
    """The design's A3: under 1,024 px the queue's card shrinks to its Pause queue button alone, an icon as wide as the
    menu's (pause while the queue runs, play while it is paused), its usual words its title and its label: a window
    snapped to half of a 1920 px screen (960 px) can still pause the queue. Pressed there, it pauses it. At 1,280 px the
    card is as it was: its two lines, its bar, the button's words, and Clear the queue."""
    from job_apply.pipeline import Run

    desk = _idle_desk(srv)
    a = desk.applier
    for i in range(4):
        job_id = srv.add_job(url=f"https://careers.example.com/fab/{i}", title=f"Technician {i}",
                             company="Example Fab")["job"]["id"]
        a.runs[job_id] = Run(job_id, "Technician", "Example Fab", status="queued")
        a.tasks.append(("apply", job_id))

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 960, "height": 800})
                await open_desk(page, desk)
                await page.wait_for_selector("#pause-btn", state="visible")
                narrow = await page.evaluate(_NARROW_QUEUE)
                await page.click("#pause-btn")
                await _until(lambda: a.paused, "Pause queue reached the desk")
                await page.wait_for_selector("#pause-btn[aria-label='Carry on with the queue (4 waiting)']")
                paused = await page.evaluate(_NARROW_QUEUE)
                await page.set_viewport_size({"width": 1280, "height": 800})
                await page.wait_for_selector("#queue-said", state="visible")
                # (the styles change with the width at once; the page's own redraw for the width comes a moment after)
                await page.wait_for_selector("#pause-btn:not([aria-label])")
                wide = await page.evaluate(_NARROW_QUEUE)
                await browser.close()
                return narrow, paused, wide
        finally:
            await desk.stop()

    narrow, paused, wide = run(go())
    assert narrow == {"card": True, "said": False, "more": False, "bar": False, "clear": False, "pause": True,
                      "words": "Pause queue", "drawn": False, "title": "Pause queue", "label": "Pause queue",
                      "icon": "#i-pause", "width": narrow["link"], "link": narrow["link"]}, narrow
    assert paused == {**narrow, "words": "Carry on with the queue (4 waiting)",
                      "title": "Carry on with the queue (4 waiting)",
                      "label": "Carry on with the queue (4 waiting)", "icon": "#i-play"}, paused
    assert wide == {"card": True, "said": True, "more": True, "bar": True, "clear": True, "pause": True,
                    "words": "Carry on with the queue (4 waiting)", "drawn": True, "title": _PAUSE_TITLE,
                    "label": None, "icon": None, "width": wide["width"], "link": wide["link"]}, wide
    assert wide["width"] > 150  # (full width in the card, as it was)


def test_a_narrow_window_hides_a_queue_card_with_no_pause_queue_to_show(srv, monkeypatch):
    """Under 1,024 px the queue's card is only its Pause queue icon, so with none to show (only Clear the queue would:
    jobs held before they begin) the card is hidden, not left as an empty box. At 1,280 px it is as it was."""
    desk = _canned(srv, monkeypatch, _queue(_state("state-ready.json"), ["needs_you", "ready"], clearable=1))

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                seen = {}
                for width in (960, 1280):
                    page = await browser.new_page(viewport={"width": width, "height": 800})
                    await open_desk(page, desk)
                    await page.wait_for_function("document.getElementById('clear-queue-btn').hidden === false")
                    seen[width] = await page.evaluate("""() => {
                      const inSight = (id) => document.getElementById(id).getClientRects().length > 0;
                      return [inSight('queue-card'), inSight('pause-btn'), inSight('clear-queue-btn')]; }""")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen == {960: [False, False, False], 1280: [True, False, True]}, seen
