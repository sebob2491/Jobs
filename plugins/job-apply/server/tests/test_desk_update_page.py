"""The page half of the Update button (the design's F2, and F3's test 10): the update card in Settings, under Updates
and about, for each kind of copy and each step of an update; the blue notice and the side menu's line that lead to
it; the question asked before an update closes the desk's browser window on jobs waiting there; the line said once
after an update; and what the page says while the desk is down for its installer. The page draws a canned state, and
the desk's `updates` is a stand-in: no test here reaches the network or starts a program. Made-up values only."""

import asyncio
import copy

import pytest
from conftest import browser_available, open_desk, person_browser, run
from test_desk_frame import _canned, _state

from job_apply import config, desk as desk_module, updates
from job_apply.desk import Desk

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

NOW = 1_800_000_000  # (the page's clock, held: "Checked 2 hours ago" reads the same every run)
COMMANDS = list(updates.UPDATE_COMMANDS)


def _update(**more) -> dict:
    """The state's `update`, with F2's nine keys: version 0.4.3 here, nothing newer found, looked 2 hours ago."""
    return {"current": "0.4.3", "latest": "0.4.3", "newer": False, "kind": "installed", "checked_at": NOW - 7200,
            "progress": None, "error": None, "commands": COMMANDS, "notes": None, **more}


NOTES = "https://github.com/example/desk/releases/tag/job-apply-v0.4.4"
NEWER = {"latest": "0.4.4", "newer": True, "notes": NOTES}


class StandIn:
    """The desk's `updates`, standing in for updates.UpdateCheck: what the routes ask of it, and what they asked."""

    updated_to = None

    def __init__(self, state: dict, why: str | None = None) -> None:
        self.canned, self.why, self.checked, self.started = state, why, 0, 0

    def note_updated(self) -> None:
        pass

    async def refresh(self, standalone=False, now=False) -> None:
        self.checked += now

    def state(self, standalone=False) -> dict:
        return copy.deepcopy(self.canned["update"])

    def why_not(self, standalone, filling):
        return self.why

    def start(self, standalone, **parts) -> None:
        self.started += 1


def _desk(srv, monkeypatch, state: dict, why: str | None = None) -> tuple[Desk, StandIn]:
    desk = _canned(srv, monkeypatch, state)
    desk.updates = stand_in = StandIn(state, why)
    return desk, stand_in


async def _page(browser):
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    await page.clock.set_fixed_time(NOW)
    return page


_CARD = """() => { const $ = (id) => document.getElementById(id), card = $('update-card');
  return {shown: card.getClientRects().length > 0, title: $('update-h').textContent, said: $('update-said').textContent,
          more: [...$('update-more').querySelectorAll('p')].map((p) => p.innerText).join('|'),
          buttons: [...$('update-acts').querySelectorAll('button')].map((b) => [b.textContent, b.disabled]),
          links: [...$('update-acts').querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href'),
                                                                        a.target]),
          markup: $('update-card').querySelectorAll('#bold, img').length + (window.__ran || 0),
          anchors: $('update-card').querySelectorAll('a').length}; }"""
WHATS_NEW = [["What's new", NOTES, "_blank"]]  # (a link like any other: the desk's own window hands it to the browser)

# F2's card, row by row: (the state's update, a job being filled or not) and what the card says
ROWS = {
    "nothing newer": (_update(), None, {
        "title": "You have the newest version", "said": "Version 0.4.3. Checked 2 hours ago.", "more": "",
        "links": [], "buttons": [["Check now", False]]}),
    "never looked": (_update(latest=None, checked_at=0), None, {
        "title": "Version 0.4.3", "said": "The desk couldn't look for a newer one. It needs the internet for that.",
        "more": "", "links": [], "buttons": [["Check now", False]]}),
    "newer, installed": (_update(**NEWER), None, {
        "title": "Version 0.4.4 is ready", "said": "You have 0.4.3. Updating takes about a minute. The desk closes, "
        "installs, and opens again. Your queue is kept.", "more": "", "links": WHATS_NEW,
        "buttons": [["Update now", False]]}),
    "newer, plugin": (_update(**NEWER, kind="plugin"), None, {
        "title": "Version 0.4.4 is ready", "said": "You have 0.4.3. The desk updates the plugin. Then restart Claude "
        "to use it.", "more": "", "links": WHATS_NEW, "buttons": [["Update now", False]]}),
    "newer, manual": (_update(**NEWER, kind="manual"), None, {
        "title": "Version 0.4.4 is out", "said": "You have 0.4.3. This copy isn't one the desk can update by itself.",
        "more": "", "links": WHATS_NEW, "buttons": []}),
    "newer, installed, a job being filled": (_update(**NEWER), "current", {
        "title": "Version 0.4.4 is ready", "said": "You have 0.4.3. Updating takes about a minute. The desk closes, "
        "installs, and opens again. Your queue is kept. Wait for the jobs being filled to finish, or pause the queue.",
        "more": "", "links": WHATS_NEW, "buttons": [["Update now", True]]}),
    "newer, plugin, a job being filled": (_update(**NEWER, kind="plugin"), "running", {
        "title": "Version 0.4.4 is ready", "said": "You have 0.4.3. The desk updates the plugin. Then restart Claude "
        "to use it. Wait for the jobs being filled to finish, or pause the queue.", "more": "",
        "links": WHATS_NEW, "buttons": [["Update now", True]]}),
    "downloading": (_update(**NEWER, progress="downloading"), None, {
        "title": "Downloading version 0.4.4", "said": "This takes a moment.", "more": "", "links": [], "buttons": []}),
    "installing": (_update(**NEWER, progress="installing"), None, {
        "title": "Installing version 0.4.4", "said": "This window closes, and the Job Desk opens again by itself.",
        "more": "", "links": [], "buttons": []}),
    "updating": (_update(**NEWER, kind="plugin", progress="updating"), None, {
        "title": "Updating the plugin", "said": "This takes a moment.", "more": "", "links": [], "buttons": []}),
    "done": (_update(**NEWER, kind="plugin", progress="done"), None, {
        "title": "Updated to 0.4.4", "said": "Restart Claude to use it.", "more": "", "links": [], "buttons": []}),
    "failed, installed": (_update(**NEWER, progress="failed", error=updates.NOT_WHOLE), None, {
        "title": "The update didn't go through", "said": "The download didn't arrive whole. Nothing was changed.",
        "more": "", "links": WHATS_NEW, "buttons": [["Update now", False]]}),
    "failed, wouldn't start outside the desk's job": (
        _update(**NEWER, progress="failed", error=updates.NO_BREAKAWAY), None, {
            "title": "The update didn't go through", "said": "This computer wouldn't let the desk start the installer "
            "by itself. Press What's new, download the installer there, and run it. Nothing was changed.",
            "more": "", "links": WHATS_NEW, "buttons": [["Update now", False]]}),
    "failed, didn't finish": (_update(**NEWER, progress="failed", error=updates.NOT_FINISHED.format("0.4.4")), None, {
        "title": "The update didn't go through", "said": "The update to version 0.4.4 didn't finish. Press Update now "
        "to try again. Nothing was changed.", "more": "", "links": WHATS_NEW, "buttons": [["Update now", False]]}),
    "failed, plugin": (_update(**NEWER, kind="plugin", progress="failed", error=updates.CLAUDE_FAILED), None, {
        "title": "The update didn't go through", "said": "Claude couldn't update the plugin. Nothing was changed.",
        "more": "You can also run these in a terminal:|claude plugin marketplace update sebob-jobs\n"
                "claude plugin update job-apply@sebob-jobs", "links": WHATS_NEW, "buttons": [["Update now", False]]}),
    # (the server's sentence is drawn as words, never as markup)
    "failed, markup in the sentence": (
        _update(**NEWER, progress="failed", error='<b id="bold">Bold</b><img src=x onerror="window.__ran=1">'), None, {
            "title": "The update didn't go through",
            "said": '<b id="bold">Bold</b><img src=x onerror="window.__ran=1"> Nothing was changed.',
            "more": "", "links": WHATS_NEW, "buttons": [["Update now", False]], "markup": 0}),
    # ("What's new" only for a web address: a page's notes that are anything else draw none)
    "newer, notes not a web address": (_update(**{**NEWER, "notes": "javascript:alert(1)"}), None, {
        "title": "Version 0.4.4 is ready", "said": "You have 0.4.3. Updating takes about a minute. The desk closes, "
        "installs, and opens again. Your queue is kept.", "more": "", "links": [],
        "buttons": [["Update now", False]], "anchors": 0}),
}


def test_the_update_card_says_each_row_of_f2s_table(srv, monkeypatch):
    """F3's test 10: every row of F2's card (each kind of copy; nothing newer, a check that never worked, a newer
    version; each step of an update under way; a failure, with the server's sentence and, for a plugin copy, the two
    commands), in Settings under Updates and about. Update now is disabled while a job is being filled (the applier's
    job under way, or a job's run running), and says why."""
    state = _state("state-openings.json")
    desk, _ = _desk(srv, monkeypatch, state)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await _page(browser)
                seen = {}
                for name, (update, filling, _) in ROWS.items():
                    state["update"] = update
                    state["current"] = 7 if filling == "current" else None
                    state["others"] = [{"job_id": 7, "title": "Example Technician", "company": "Example Fab",
                                        "run": {"status": "running", "log": [], "log_at": []}}] \
                        if filling == "running" else []
                    await page.goto("about:blank")
                    await open_desk(page, desk, "settings/about")
                    seen[name] = await page.evaluate(_CARD)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    for name, (_, _, want) in ROWS.items():
        got = {k: v for k, v in seen[name].items() if k in want or k not in ("markup", "anchors")}
        assert got == {"shown": True, **want}, (name, seen[name])


def _waiting(k: int) -> dict:
    """The stops state with `k` jobs waiting on the person in the desk's browser window (the rest just openings)."""
    state = _state("state-stops.json")
    kept = 0
    for row in [*state.get("listings", []), *state.get("others", [])]:
        if row.get("run"):
            kept += 1
            if kept > k:
                del row["run"]
    state["update"] = _update(**NEWER)
    return state


@pytest.mark.parametrize("waiting", [0, 1, 3])
def test_update_now_asks_first_when_jobs_wait_in_the_desks_browser_window(srv, monkeypatch, waiting):
    """The brief's item 5: an installed copy's update closes the desk's browser window, so with jobs waiting on the
    person there (needs_you or ready), Update now asks first, and a No starts nothing; a Yes starts it. With none, it
    starts at once."""
    state = _waiting(waiting) if waiting else {**_state("state-openings.json"), "update": _update(**NEWER)}
    desk, stand_in = _desk(srv, monkeypatch, state)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await _page(browser)
                asked, answers = [], [False, True]
                page.on("dialog", lambda d: asked.append(d.message) or asyncio.ensure_future(
                    d.accept() if answers.pop(0) else d.dismiss()))
                await open_desk(page, desk, "settings/about")
                await page.click("[data-act=update-start]")
                await page.wait_for_timeout(500)
                declined = stand_in.started
                if waiting:
                    await page.click("[data-act=update-start]")
                    await page.wait_for_function(SAVED.format("update-start"))
                    await page.wait_for_timeout(300)
                await browser.close()
                return asked, declined, stand_in.started
        finally:
            await desk.stop()

    asked, declined, started = run(go())
    if not waiting:
        assert (asked, declined, started) == ([], 1, 1)
        return
    said = ("1 job is waiting on you there, and what you have typed into its page will be lost." if waiting == 1 else
            f"{waiting} jobs are waiting on you there, and what you have typed into their pages will be lost.")
    assert asked == [f"Updating closes the desk's browser window. {said} Update now?"] * 2
    assert (declined, started) == (0, 1)


@pytest.mark.parametrize("why", [updates.NOT_NEWER, updates.MANUAL, updates.ALREADY, updates.FILLING])
def test_a_refused_update_says_the_servers_sentence_under_the_card(srv, monkeypatch, why):
    """Each of /api/update/start's four refusals (F2), said under the card in red, as any refused save is (D1); the
    button is there again afterwards."""
    state = {**_state("state-openings.json"), "update": _update(**NEWER)}
    desk, stand_in = _desk(srv, monkeypatch, state, why=why)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await _page(browser)
                await open_desk(page, desk, "settings/about")
                await page.click("[data-act=update-start]")
                err = await page.wait_for_selector("#update-row > .set-err:not([hidden])")
                said = await err.text_content()
                enabled = await page.is_enabled("[data-act=update-start]")
                await browser.close()
                return said, enabled
        finally:
            await desk.stop()

    assert run(go()) == (why, True)
    assert stand_in.started == 0


def test_check_now_asks_the_desk_to_look(srv, monkeypatch):
    """Check now posts /api/update/check (the server keeps its own pace: once a minute at most)."""
    state = {**_state("state-openings.json"), "update": _update()}
    desk, stand_in = _desk(srv, monkeypatch, state)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await _page(browser)
                await open_desk(page, desk, "settings/about")
                await page.click("[data-act=update-check]")
                await page.wait_for_function(SAVED.format("update-check"))
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert stand_in.checked == 1


SAVED = "() => !document.querySelector('[data-act={}][data-saving]')"  # (a press saveRow has finished with)
NOTICE_SAYS = "() => document.querySelector('#notices').textContent.includes('{}')"
_NOTICES = "() => [...document.querySelectorAll('#notices > p')].map((p) => p.textContent.trim())"


def test_the_notice_names_no_command_and_its_link_opens_the_card(srv, monkeypatch):
    """F2 and Plain wording, Version F: the blue notice says "Version 0.4.4 is ready (you have 0.4.3)." and links to
    #settings/about ("Update"), naming no command; the link opens the update card. Not said with nothing newer, nor
    while an update is under way or done (the card says those)."""
    state = {**_state("state-openings.json"), "update": _update(**NEWER)}
    desk, _ = _desk(srv, monkeypatch, state)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await _page(browser)
                await open_desk(page, desk, "home")
                notices = await page.evaluate(_NOTICES)
                links = await page.eval_on_selector_all("#notices a", "(a) => a.map((l) => l.getAttribute('href'))")
                await page.click("#notices a[href='#settings/about']")
                await page.wait_for_function("() => document.body.dataset.page === 'settings'"
                                             " && document.body.dataset.panel === 'about'")
                card = await page.evaluate(_CARD)
                others = {}
                for name, update in (("nothing newer", _update()),
                                     ("downloading", _update(**NEWER, progress="downloading")),
                                     ("done", _update(**NEWER, kind="plugin", progress="done")),
                                     ("failed", _update(**NEWER, progress="failed", error=updates.NOT_WHOLE))):
                    state["update"] = update
                    await page.goto("about:blank")
                    await open_desk(page, desk, "home")
                    others[name] = [n for n in await page.evaluate(_NOTICES) if "0.4.4" in n or "0.4.3" in n]
                await browser.close()
                return notices, links, card, others
        finally:
            await desk.stop()

    notices, links, card, others = run(go())
    said = [n for n in notices if "0.4.4" in n]
    assert said == ["Version 0.4.4 is ready (you have 0.4.3). Update"], notices
    assert links == ["#settings/about"]
    assert not any("claude plugin" in n or "terminal" in n.lower() or "PowerShell" in n for n in notices), notices
    assert card["shown"] and card["title"] == "Version 0.4.4 is ready", card
    assert others == {"nothing newer": [], "downloading": [], "done": [],
                      "failed": ["Version 0.4.4 is ready (you have 0.4.3). Update"]}, others


def test_the_side_menu_says_update_ready_as_a_link_to_the_card(srv, monkeypatch):
    """F2: under the person's name, "Version 0.4.3 · update ready" as a link to #settings/about, which opens the card;
    with nothing newer, "Version 0.4.3" and no link, and pressing there opens the profile as the rest of the button
    does."""
    state = {**_state("state-openings.json"), "version": "0.4.3", "update": _update(**NEWER)}
    desk, _ = _desk(srv, monkeypatch, state)
    read = """() => { const v = document.getElementById('plugin-version');
        return [v.textContent, [...v.querySelectorAll('a')].map((a) => a.getAttribute('href'))]; }"""

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await _page(browser)
                await open_desk(page, desk, "help")
                ready = await page.evaluate(read)
                await page.click("#plugin-version a")
                await page.wait_for_function("() => document.body.dataset.page === 'settings'"
                                             " && document.body.dataset.panel === 'about'")
                card = await page.is_visible("#update-card")
                state["update"] = _update()
                await page.goto("about:blank")
                await open_desk(page, desk, "help")
                plain = await page.evaluate(read)
                box = await page.locator("#plugin-version").bounding_box()
                await page.mouse.click(box["x"] + 10, box["y"] + box["height"] / 2)
                await page.wait_for_selector("#setup[open]")
                await browser.close()
                return ready, card, plain
        finally:
            await desk.stop()

    ready, card, plain = run(go())
    assert ready == ["Version 0.4.3 · update ready", ["#settings/about"]], ready
    assert card
    assert plain == ["Version 0.4.3", []], plain


def test_the_page_says_once_that_the_desk_was_updated(srv, monkeypatch):
    """F2: after an installed copy updated and opened again, the notice about the queue (back paused) starts with "The
    Job Desk was updated to 0.4.4.", once: the desk that opens after it doesn't say it."""
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(config, "plugin_version", lambda: "0.4.4")
    (config.home() / "updates").mkdir(parents=True, exist_ok=True)
    (config.home() / "updates" / updates.UPDATED_TO).write_text("0.4.4", encoding="utf-8")

    async def notices_of(desk):
        desk.applier.start = lambda: None
        monkeypatch.setattr(desk.applier, "restored_notice", lambda: "2 jobs from before the desk restarted are back.")
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await _page(browser)
                await open_desk(page, desk, "home")
                await page.wait_for_selector("[data-act=resume]")
                said = await page.evaluate(_NOTICES)
                await browser.close()
                return [n for n in said if "restarted" in n]
        finally:
            await desk.stop()

    first = run(notices_of(Desk(srv)))
    assert len(first) == 1 and first[0].startswith(
        "The Job Desk was updated to 0.4.4. 2 jobs from before the desk restarted are back."), first
    again = run(notices_of(Desk(srv)))
    assert len(again) == 1 and again[0].startswith("2 jobs from before the desk restarted are back."), again


def test_while_the_desk_is_down_for_its_installer_the_page_says_so_and_comes_back(srv, monkeypatch):
    """The brief's item 6, with F2's installing: the desk stops for its installer (its state said installing), so the
    page closes its window where the browser lets it, and says "The Job Desk is updating. This window will show it
    again when it is back." in place of "the desk isn't answering"; three minutes on with no desk, it says to open
    the Job Desk again. The desk the installer opens has a key and a window of its own: when it answers (refusing
    this window's key), the page says the desk opened again and that this window can be closed, never that its key is
    out of date, and stops asking. Without an update under way, a desk gone is said as before."""
    state = {**_state("state-openings.json"), "update": _update(**NEWER, progress="installing")}
    desk, _ = _desk(srv, monkeypatch, state)

    async def go():
        await desk.start(port=0, open_browser=False)
        port = desk.port
        again = None
        try:
            async with person_browser() as browser:
                page = await _page(browser)
                await page.add_init_script("window.close = () => { window.__closed = (window.__closed || 0) + 1; };")
                await open_desk(page, desk, "settings/about")
                card = await page.text_content("#update-h")
                await desk.stop()
                await page.wait_for_function(NOTICE_SAYS.format("updating"))
                await page.wait_for_timeout(3500)  # (two more refreshes: still said, and the window closed once)
                down = await page.evaluate(_NOTICES), await page.evaluate("window.__closed")
                await page.clock.set_fixed_time(NOW + 200)  # (past three minutes with no desk)
                await page.wait_for_function(NOTICE_SAYS.format("come back"))
                long = await page.evaluate(_NOTICES)
                again = Desk(srv)
                again.applier.start = lambda: None
                # (the same port again, as the desk the installer opens takes its usual one: here the port the test's
                # desk had, which the system may hold a little while after it closed)
                monkeypatch.setattr(desk_module, "_free_port", lambda preferred: port)
                asked = []
                page.on("request", lambda r: asked.append(r.url) if "/api/" in r.url else None)
                await again.start(port=port, open_browser=False)
                await page.wait_for_function(NOTICE_SAYS.format("opened again"), timeout=20000)
                del asked[:]
                await page.wait_for_timeout(3500)  # (two refreshes' time: the page has stopped asking)
                back = await page.evaluate(_NOTICES), await page.evaluate("window.__closed"), list(asked)
                # (and a desk gone with no update under way: said as before)
                state["update"] = _update()
                await page.goto("about:blank")
                await open_desk(page, again, "home")
                await again.stop()
                await page.wait_for_function(NOTICE_SAYS.format("answering"))
                gone = await page.evaluate(_NOTICES)
                await browser.close()
                return card, down, long, back, gone
        finally:
            for running in (desk, again):  # (stopping one stopped already does nothing)
                if running is not None:
                    await running.stop()

    card, (down, closed), long, (back, closed_again, asked), gone = run(go())
    assert card == "Installing version 0.4.4"
    assert down == ["The Job Desk is updating. This window will show it again when it is back."], down
    assert closed == 1
    assert long == ["Problem: the Job Desk hasn't come back from its update yet. Open the Job Desk again."], long
    # (never "was updated": the desk opened may be the version before, after an update that didn't finish)
    assert back == ["The Job Desk opened again in a window of its own. You can close this one."]
    assert closed_again == 2 and asked == [], (closed_again, asked)
    assert len(gone) == 1 and gone[0].startswith("Problem: the desk isn't answering ("), gone
