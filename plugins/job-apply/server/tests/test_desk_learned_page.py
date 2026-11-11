"""The Job Desk's page for what it learns from the person's presses (learned.py): a job's card says when the desk
pressed a button as the person did, with Don't do that again; Settings, Applying has the switch "Learn from what I do
when the desk is stuck" and the list "What the desk has learned" (each with Forget); and the Safety window has a line
for it. Each test opens the real page in a browser. Made-up values only: the repo is public."""

import copy
import json

import pytest
from conftest import browser_available, open_desk, person_browser, run
from test_desk_buttons import _idle_desk
from test_desk_frame import _canned, _state

from job_apply import learned

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

SWITCH_SAID = ("When the desk can't find the button that moves a step on and you press it yourself, it remembers that "
               "button's words for that employer's page and presses it next time. It never learns a Submit, an "
               "agreement, a sign-in or anything you type.")
SAFETY_SAID = ("Only a step button's words, at that employer's page; never a Submit, an agreement, a sign-in or "
               "anything typed.")
SAFETY_WHERE = "Settings, Applying: Learn from what I do when the desk is stuck"
# Two entries the person's presses made (newest day first, as /api/learned lists them): one pressed twice again,
# one never
ONE = ("example.wd5.myworkdayjobs.com|examplefab", "workday", "0123456789abcdef", "Go to next section", "2026-10-03")
TWO = ("careers.example.org|examplelitho", "", "fedcba9876543210", "Continue to questions", "2026-09-21")


def _keep(*entries, worked: dict[int, int] | None = None) -> list[str]:
    """The entries kept in learned.json; their ids as /api/learned gives them (the first's two presses that moved the
    page on, counted where `worked` says: {index: presses})."""
    ids = []
    for i, (place, system, page, button, day) in enumerate(entries):
        assert learned.remember(place, system, page, button, day)
        for _ in range((worked or {}).get(i, 0)):
            learned.worked(place, page)
        ids.append(learned.entry_id(place, page))
    return ids


def _recording(page) -> list[dict]:
    """The bodies of every POST to /api/learned/forget the page makes, in order."""
    posted: list[dict] = []
    page.on("request", lambda r: posted.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/api/learned/forget") else None)
    return posted


_LINE = """() => [...document.querySelectorAll('#learned-list > .setrow')].map((row) => ({
  id: row.dataset.learned, text: row.querySelector('.t').innerText, forget: row.querySelector('button').innerText}))"""


# ------------------------------------------------------------------------------------------- 1: a job's card
def _ready_state(button: str, entry_id: str) -> dict:
    """The ready state's one ready job, having had a press made for it."""
    state = _state("state-ready.json")
    run_ = next(r["run"] for r in state["listings"] if r["run"]["status"] == "ready")
    run_["learned_press"] = {"button": button, "on": "2026-10-03", "id": entry_id}
    return state


async def _open_ready_card(page, desk):
    await open_desk(page, desk, "home")
    await page.wait_for_selector("#done .task [data-open]")
    await page.evaluate("() => document.querySelectorAll('[data-open][aria-expanded=false]').forEach((b) => b.click())")
    await page.wait_for_selector("#done .learned-line")


def test_a_card_says_the_press_the_desk_made_and_dont_do_that_again_forgets_it(srv, job_apply_home, monkeypatch):
    """A run with learned_press shows one line on its card, word for word, and Don't do that again: pressed, it
    posts the entry's id to /api/learned/forget, the entry is gone, and the line reads Forgotten. in its place (the next
    draw of the
    state leaves it so)."""
    [entry_id] = _keep(ONE)
    desk = _canned(srv, monkeypatch, _ready_state("Go to next section", entry_id))

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                posted = _recording(page)
                await _open_ready_card(page, desk)
                seen = {"line": await page.inner_text("#done .learned-line"),
                        "lines": await page.locator(".learned-line").count()}
                await page.click("#done [data-learned-run]")
                await page.wait_for_selector("#done [data-learned-done]")
                await page.evaluate("() => refresh()")  # (the state drawn again, as every 1.5 seconds)
                await page.wait_for_timeout(200)
                seen["after"] = await page.inner_text("#done .learned-line")
                seen["buttons"] = await page.locator("#done [data-learned-run]").count()
                seen["focus"] = await page.evaluate("() => document.activeElement.dataset.learnedDone")
                seen["posted"] = posted
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["line"].replace("\n", " ") == ("The desk pressed “Go to next section” here, as you did on Oct 3. "
                                               "Don't do that again"), seen
    assert seen["lines"] == 1, seen
    assert seen["posted"] == [{"id": entry_id}], seen
    assert (seen["after"], seen["buttons"], seen["focus"]) == ("Forgotten.", 0, entry_id), seen
    assert learned.all() == []


def test_a_buttons_words_with_markup_are_shown_as_words_on_the_card(srv, job_apply_home, monkeypatch):
    """A button's words come from a site: <b> and an ampersand show as typed, never as markup."""
    words = '<b>Go</b> & "next" <img src=x onerror=alert(1)>'
    [entry_id] = _keep((*ONE[:3], words, ONE[4]))
    desk = _canned(srv, monkeypatch, _ready_state(words, entry_id))

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await _open_ready_card(page, desk)
                seen = {"line": await page.inner_text("#done .learned-line"),
                        "markup": await page.locator("#done .learned-line b, #done .learned-line img").count()}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["markup"] == 0, seen
    assert f"The desk pressed “{words}” here, as you did on Oct 3." in seen["line"], seen


def test_a_run_with_no_learned_press_has_no_line(srv, job_apply_home, monkeypatch):
    state = _state("state-ready.json")
    for r in state["listings"]:
        if r.get("run"):
            r["run"]["learned_press"] = None  # (what the desk sends of a run that made no such press)
    desk = _canned(srv, monkeypatch, copy.deepcopy(state))

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "home")
                await page.wait_for_selector("#done .task")
                n = await page.locator(".learned-line").count()
                await browser.close()
                return n
        finally:
            await desk.stop()

    assert run(go()) == 0


def test_a_press_that_cannot_be_forgotten_stays_on_the_card_with_the_routes_sentence(srv, job_apply_home, monkeypatch):
    [entry_id] = _keep(ONE)
    desk = _canned(srv, monkeypatch, _ready_state("Go to next section", entry_id))
    sentence = "The desk couldn't write what it learned just now."

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await page.route("**/api/learned/forget", lambda route: route.fulfill(
                    status=400, content_type="application/json", body=json.dumps({"error": sentence})))
                await _open_ready_card(page, desk)
                await page.click("#done [data-learned-run]")
                await page.wait_for_selector("#done .learned-line + .set-err")
                seen = {"err": await page.inner_text("#done .learned-line + .set-err"),
                        "button": await page.locator("#done [data-learned-run]").count()}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen == {"err": sentence, "button": 1}, seen
    assert len(learned.all()) == 1


# -------------------------------------------------------------------------------------------- 2: the switch
_SWITCH = """() => { const s = document.getElementById('learn-me'), ok = document.getElementById('learn-me-ok'),
  err = s.closest('.setrow').querySelector('.set-err');
  return {checked: s.checked, said: document.getElementById('learn-me-said').textContent,
    name: document.getElementById('learn-me-name').textContent,
    ok: ok.hidden ? null : ok.textContent, err: err && !err.hidden ? err.textContent : null}; }"""


def test_the_switch_shows_the_state_saves_through_the_settings_route_and_says_saved(srv, job_apply_home):
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                posted = []
                page.on("request", lambda r: posted.append(json.loads(r.post_data or "{}"))
                        if r.method == "POST" and r.url.endswith("/api/settings") else None)
                await open_desk(page, desk, "settings/applying")
                seen = {"first": await page.evaluate(_SWITCH)}
                await page.click("#learn-me")
                await page.wait_for_selector("#learn-me-ok:not([hidden])")
                seen["off"] = await page.evaluate(_SWITCH)
                seen["kept"] = desk.applier.learn_from_me
                await page.click("#learn-me")
                await page.wait_for_function("() => !document.querySelector('[data-saving]')")
                await page.wait_for_selector("#learn-me-ok:not([hidden])")
                seen["on"] = await page.evaluate(_SWITCH)
                seen["posted"] = posted
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    name = "Learn from what I do when the desk is stuck"
    assert seen["first"] == {"checked": True, "said": SWITCH_SAID, "name": name, "ok": None, "err": None}, seen
    assert seen["off"]["checked"] is False and seen["off"]["ok"] == "Saved" and seen["off"]["err"] is None, seen
    assert seen["kept"] is False and seen["on"]["checked"] is True and seen["on"]["ok"] == "Saved", seen
    assert seen["posted"] == [{"learn_from_me": False}, {"learn_from_me": True}], seen
    assert desk.applier.learn_from_me is True


def test_a_refused_save_says_the_routes_sentence_and_the_switch_goes_back(srv, job_apply_home):
    desk = _idle_desk(srv)
    sentence = "The desk couldn't save that just now."

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/applying")
                await page.route("**/api/settings", lambda route: route.fulfill(
                    status=400, content_type="application/json", body=json.dumps({"error": sentence}))
                    if route.request.method == "POST" else route.continue_())
                await page.click("#learn-me")
                await page.wait_for_function("() => !document.querySelector('[data-saving]')")
                await page.wait_for_selector("#learn-me ~ .set-err:not([hidden]), .setrow > .set-err:not([hidden])")
                seen = await page.evaluate(_SWITCH)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert (seen["checked"], seen["ok"], seen["err"]) == (True, None, sentence), seen
    assert desk.applier.learn_from_me is True


# --------------------------------------------------------------------------------------------- 3: the list
def test_the_list_draws_every_part_of_each_entry_and_leaves_worked_out_at_none(srv, job_apply_home):
    ids = _keep(ONE, TWO, worked={0: 2})
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/applying")
                await page.wait_for_selector("#learned[data-loaded]", state="attached")
                seen = {"rows": await page.evaluate(_LINE), "none": await page.locator("#learned-none").is_visible(),
                        "heading": await page.inner_text("#learned-h")}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["heading"] == "What the desk has learned" and seen["none"] is False, seen
    assert seen["rows"] == [
        {"id": ids[0], "text": "example.wd5.myworkdayjobs.com · Workday · “Go to next section” · Oct 3 "
                               "· worked 2 times", "forget": "Forget"},
        {"id": ids[1], "text": "careers.example.org · “Continue to questions” · Sep 21",
         "forget": "Forget"}], seen


def test_an_empty_list_says_nothing_yet_and_shows_even_with_the_switch_off(srv, job_apply_home):
    desk = _idle_desk(srv)
    desk.applier.set_learn_from_me(False)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/applying")
                await page.wait_for_selector("#learned[data-loaded]", state="attached")
                seen = {"none": await page.inner_text("#learned-none"), "rows": await page.evaluate(_LINE),
                        "switch": await page.evaluate(_SWITCH)}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["none"] == "Nothing yet." and seen["rows"] == [] and seen["switch"]["checked"] is False, seen


def test_forget_posts_the_id_and_the_line_goes(srv, job_apply_home):
    ids = _keep(ONE, TWO)
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                posted = _recording(page)
                await open_desk(page, desk, "settings/applying")
                await page.wait_for_selector("#learned[data-loaded]", state="attached")
                await page.click(f"[data-learned-forget='{ids[0]}']")
                await page.wait_for_function("() => document.querySelectorAll('#learned-list > .setrow').length === 1")
                seen = {"rows": await page.evaluate(_LINE), "posted": list(posted)}
                await page.click(f"[data-learned-forget='{ids[1]}']")
                await page.wait_for_selector("#learned-none:not([hidden])")
                seen["last"] = await page.inner_text("#learned-none")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert [r["id"] for r in seen["rows"]] == [ids[1]] and seen["posted"] == [{"id": ids[0]}], seen
    assert seen["last"] == "Nothing yet." and learned.all() == []


def test_a_failing_route_shows_its_sentence_in_the_list_and_when_forgetting(srv, job_apply_home):
    ids = _keep(ONE)
    desk = _idle_desk(srv)
    asking, refusing = "The desk couldn't read what it learned.", "The desk couldn't forget that just now."

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                broken = {"on": True}

                def fail(route):
                    if broken["on"]:
                        return route.fulfill(status=500, content_type="application/json",
                                             body=json.dumps({"error": asking}))
                    return route.continue_()

                await page.route("**/api/learned", fail)
                await open_desk(page, desk, "settings/applying")
                await page.wait_for_selector("#learned[data-loaded]", state="attached")
                seen = {"asking": await page.inner_text("#learned-err"),
                        "none": await page.locator("#learned-none").is_visible()}
                broken["on"] = False
                await page.unroute("**/api/learned")
                await page.goto("about:blank")
                await open_desk(page, desk, "settings/applying")
                await page.wait_for_selector("#learned[data-loaded]", state="attached")
                await page.route("**/api/learned/forget", lambda route: route.fulfill(
                    status=400, content_type="application/json", body=json.dumps({"error": refusing})))
                await page.click(f"[data-learned-forget='{ids[0]}']")
                await page.wait_for_selector("#learned-err:not([hidden])")
                seen["refusing"] = await page.inner_text("#learned-err")
                seen["rows"] = len(await page.evaluate(_LINE))
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen == {"asking": asking, "none": False, "refusing": refusing, "rows": 1}, seen
    assert len(learned.all()) == 1


# ---------------------------------------------------------------------------------------------- 4: Safety
@pytest.mark.parametrize("on", [True, False])
def test_the_safety_window_says_whether_the_desk_learns_from_you(srv, job_apply_home, on):
    desk = _idle_desk(srv)
    desk.applier.set_learn_from_me(on)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/applying")
                await page.click("#safety-btn")
                await page.wait_for_selector("#safety[open] li[data-safety=learn_from_me]")
                seen = await page.evaluate("""() => {
                  const li = document.querySelector('#safety li[data-safety=learn_from_me]');
                  const a = li.querySelector('a'); return {text: li.innerText.replace(/\\s+/g, ' ').trim(),
                    href: a && a.getAttribute('href')}; }""")
                await page.click("#safety li[data-safety=learn_from_me] a")
                seen["panel"] = await page.evaluate("() => document.body.dataset.panel")
                seen["open"] = await page.evaluate("() => document.getElementById('safety').open")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["text"] == f"Learns from you: {'on' if on else 'off'}. {SAFETY_SAID} Where: {SAFETY_WHERE}.", seen
    assert (seen["href"], seen["panel"], seen["open"]) == ("#settings/applying", "applying", False), seen


# ---------------------------------------------------------------------------------------------- 5: the keyboard
def test_tab_reaches_each_forget_and_focus_stays_in_the_list_after_one(srv, job_apply_home):
    ids = _keep(ONE, TWO)
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/applying")
                await page.wait_for_selector("#learned[data-loaded]", state="attached")
                here = "() => document.activeElement.dataset.learnedForget || document.activeElement.id"
                await page.focus("#similar-me")  # (the row just above the list)
                order = []
                for _ in range(3):
                    await page.keyboard.press("Tab")
                    order.append(await page.evaluate(here))
                # (the ring shows on a control the keyboard reached)
                await page.focus("#similar-me")  # (the row just above the list)
                await page.keyboard.press("Tab")
                ring = await page.evaluate("""() => { const s = getComputedStyle(document.activeElement);
                  return [s.outlineStyle, parseFloat(s.outlineWidth)]; }""")
                await page.keyboard.press("Enter")  # (the first line's Forget)
                await page.wait_for_function("() => document.querySelectorAll('#learned-list > .setrow').length === 1")
                after_first = await page.evaluate(here)
                await page.keyboard.press("Enter")  # (the last line's)
                await page.wait_for_selector("#learned-none:not([hidden])")
                after_last = await page.evaluate("() => document.activeElement.id")
                await browser.close()
                return order, ring, after_first, after_last
        finally:
            await desk.stop()

    order, ring, after_first, after_last = run(go())
    assert order == [ids[0], ids[1], "safety-btn"], order
    assert ring[0] != "none" and ring[1] >= 2, ring
    assert (after_first, after_last) == (ids[1], "learned-h")
