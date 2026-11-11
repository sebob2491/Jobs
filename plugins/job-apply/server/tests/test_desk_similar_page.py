"""The Job Desk's page for suggested answers (similar.py): a question on a job's card that the desk found to mean the
same as one the person answered gets one line above it with Use it and No, it's different; Settings, Applying has the
switch "Suggest answers I've given to similar questions"; Your answers shows what an answer was matched to; and the
Safety window has its line. Each test opens the real page in a browser; the routes the card talks to are answered by
the test (the card's job is canned), so what is posted is what is looked at. Made-up values only: the repo is public."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from conftest import browser_available, open_desk, person_browser, run
from test_desk_buttons import _idle_desk
from test_desk_frame import _canned, _state

from job_apply import config

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

LABEL = "Do you have a valid driver's license?"
EARLIER = "Do you hold a current driving licence?"
ANSWER = "Yes, with my own car"
OFFER = {"label": LABEL, "earlier": EARLIER, "answer": ANSWER}
SENTENCE = f"This looks like one you answered: “{EARLIER}” Your answer: {ANSWER}."
SWITCH_SAID = ("When a form asks something in new words, the desk asks a model whether it means the same as a question "
               "you've answered. Only the questions' words are sent, never your answers or anything about you, and "
               "nothing is filled until you say yes.")
NO_MODEL = "Needs your API key or Claude: with neither, nothing is suggested."
SAFETY_NAME = "Suggests answers you gave to similar questions"
SAFETY_SAID = ("Only the questions' words go to the model, never an answer or anything about you; used only after "
               "your yes.")
SAFETY_WHERE = "Settings, Applying: Suggest answers I gave to similar questions"
SHOTS = os.environ.get("JOB_APPLY_SHOTS", "")


def _stops_state(similar: bool = True, options: list[str] | None = None) -> dict:
    """The stops state, whose job 1 asks a licence question (with choices), a salary and some IDs; the licence one
    carries `similar` where an offer is meant (the choices hold the offer's answer, as the server insists)."""
    state = _state("state-stops.json")
    run_ = next(r["run"] for r in state["listings"] if r.get("run") and r["run"]["job_id"] == 1)
    q = run_["questions"][0]
    assert q["label"] == LABEL
    q["options"] = options or ["Select One", ANSWER, "No"]
    if similar:
        q["similar"] = True
    return state


@asynccontextmanager
async def _card(srv, monkeypatch, state, offers=None, *, hold=None, decline=None):
    """The desk drawn from `state`, its card for job 1 open. The page's calls to /api/similar are answered with
    `offers` (after `hold`, an Event, is set), /api/similar/decline with `decline`'s status and body, and /api/answer
    with ok. Yields (page, calls): what was asked, as {"asked": n, "declined": [...], "answered": [...]}."""
    desk = _canned(srv, monkeypatch, state)
    calls: dict = {"asked": 0, "declined": [], "answered": []}

    async def similar(route):
        calls["asked"] += 1
        if hold is not None:
            await hold.wait()
        await route.fulfill(status=200, content_type="application/json", body=json.dumps({"offers": offers or []}))

    async def declined(route):
        calls["declined"].append(json.loads(route.request.post_data or "{}"))
        status, body = decline or (200, {"ok": True})
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    async def answered(route):
        calls["answered"].append(json.loads(route.request.post_data or "{}"))
        await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))

    await desk.start(port=0, open_browser=False)
    try:
        async with person_browser() as browser:
            page = await browser.new_page(viewport={"width": 1280, "height": 1100})
            await page.route(lambda url: "/api/similar?" in url, similar)
            await page.route("**/api/similar/decline", declined)
            await page.route("**/api/answer", answered)
            await open_desk(page, desk, "home")
            await page.wait_for_selector("#needs [data-open]")
            await page.evaluate(
                "() => document.querySelectorAll('[data-open][aria-expanded=false]').forEach((b) => b.click())")
            await page.wait_for_selector('form[data-answer="1"] [data-q]')
            yield page, calls
            await browser.close()
    finally:
        await desk.stop()


_LINE = """() => { const slot = document.querySelector('form[data-answer="1"] [data-similar-for]');
  const shown = (el) => !!el && !el.hidden && !el.closest('[hidden]');
  const said = slot.querySelector('[data-similar-said]'), using = slot.querySelector('[data-similar-using]');
  return {slot: !slot.hidden, said: shown(said) ? said.innerText : null, using: shown(using) ? using.innerText : null,
    err: slot.querySelector('[data-similar-err]') && shown(slot.querySelector('[data-similar-err]'))
      ? slot.querySelector('[data-similar-err]').innerText : null,
    buttons: [...slot.querySelectorAll('button')].filter(shown).map((b) => b.innerText),
    value: document.querySelector('form[data-answer="1"] [data-q]').value}; }"""
_FOCUS = """() => { const a = document.activeElement; return a.dataset.similarUndo !== undefined ? 'undo'
  : a.dataset.similarUse !== undefined ? 'use' : a.dataset.similarNo !== undefined ? 'no'
  : a.dataset.q === %s ? 'question' : a.tagName + ':' + a.id; }""" % json.dumps(LABEL)


async def _line(page) -> dict:
    return await page.evaluate(_LINE)


async def _drawn(page):
    await page.wait_for_selector('form[data-answer="1"] [data-similar-offer]:not([hidden])')


def _fill_send(page):
    """The card's own button, with a salary typed so the card has something else to send."""
    async def go():
        await page.fill('form[data-answer="1"] [data-q="Desired salary"]', "50000")
        await page.click('[data-job="1"][data-job-act="fill"]')
        await page.wait_for_function(
            "() => !document.querySelector('[data-job=\"1\"][data-job-act=\"fill\"]')?.disabled")
    return go()


# ---------------------------------------------------------------------------------------------- 1: the line
def test_a_question_with_an_offer_shows_the_sentence_once_fetched_and_a_question_without_shows_none(srv, job_apply_home,
                                                                                                 monkeypatch):
    async def go():
        hold = asyncio.Event()
        async with _card(srv, monkeypatch, _stops_state(), [OFFER], hold=hold) as (page, calls):
            await page.wait_for_function("() => true")
            await page.wait_for_timeout(200)
            seen = {"while loading": await page.locator("[data-similar-offer]:not([hidden])").count(),
                    "asked": calls["asked"]}
            hold.set()
            await _drawn(page)
            seen["line"] = await _line(page)
            seen["lines"] = await page.locator("[data-similar-offer]:not([hidden])").count()
            return seen

    seen = run(go())
    assert seen["while loading"] == 0 and seen["asked"] == 1, seen
    assert seen["lines"] == 1 and seen["line"]["said"] == SENTENCE, seen
    assert seen["line"]["buttons"] == ["Use it", "No, it's different"], seen


def test_a_question_without_similar_asks_nothing_and_shows_no_line(srv, job_apply_home, monkeypatch):
    async def go():
        async with _card(srv, monkeypatch, _stops_state(similar=False), [OFFER]) as (page, calls):
            await page.evaluate("() => refresh()")
            await page.wait_for_timeout(300)
            return calls["asked"], await page.locator("[data-similar-offer]").count()

    assert run(go()) == (0, 0)


def test_markup_in_the_earlier_words_or_the_answer_is_shown_escaped(srv, job_apply_home, monkeypatch):
    earlier = '<b>Valid</b> & "licence" <img src=x onerror=alert(1)>'
    answer = "<i>Yes</i> & more"
    offer = {"label": LABEL, "earlier": earlier, "answer": answer}

    async def go():
        async with _card(srv, monkeypatch, _stops_state(options=["Select One", answer, "No"]), [offer]) as (page, _):
            await _drawn(page)
            return (await _line(page), await page.locator("[data-similar-for] b, [data-similar-for] i, "
                                                          "[data-similar-for] img").count())

    line, markup = run(go())
    assert markup == 0, line
    assert line["said"] == f"This looks like one you answered: “{earlier}” Your answer: {answer}.", line


# ---------------------------------------------------------------------------------------------- 2: Use it
def test_use_it_posts_nothing_itself_and_the_cards_send_posts_use_similar_with_no_value(srv, job_apply_home,
                                                                                       monkeypatch):
    async def go():
        async with _card(srv, monkeypatch, _stops_state(), [OFFER]) as (page, calls):
            await _drawn(page)
            await page.click("[data-similar-use]")
            seen = {"line": await _line(page), "asked": len(calls["answered"]) + len(calls["declined"])}
            await _fill_send(page)
            seen["answered"] = calls["answered"]
            return seen

    seen = run(go())
    assert seen["asked"] == 0, seen
    assert seen["line"]["using"] == "Using your earlier answer. Undo" and seen["line"]["said"] is None, seen
    assert seen["line"]["value"] == ANSWER and seen["line"]["buttons"] == ["Undo"], seen
    [body] = seen["answered"]
    assert body["job_id"] == 1
    by = {a["label"]: a for a in body["answers"]}
    assert by[LABEL] == {"label": LABEL, "use_similar": True, "remember": True}, by
    assert by["Desired salary"] == {"label": "Desired salary", "value": "50000", "remember": True}, by


def test_use_it_keeps_the_questions_own_remember_tick_as_it_stands(srv, job_apply_home, monkeypatch):
    async def go():
        async with _card(srv, monkeypatch, _stops_state(), [OFFER]) as (page, calls):
            await _drawn(page)
            await page.uncheck(f'form[data-answer="1"] [data-remember="{LABEL}"]')
            await page.click("[data-similar-use]")
            await _fill_send(page)
            return calls["answered"]

    [body] = run(go())
    assert {"label": LABEL, "use_similar": True, "remember": False} in body["answers"], body


def test_after_undo_the_send_posts_no_use_similar_and_the_question_is_unanswered(srv, job_apply_home, monkeypatch):
    async def go():
        async with _card(srv, monkeypatch, _stops_state(), [OFFER]) as (page, calls):
            await _drawn(page)
            await page.click("[data-similar-use]")
            await page.click("[data-similar-undo]")
            seen = {"line": await _line(page)}
            await _fill_send(page)
            seen["answered"] = calls["answered"]
            return seen

    seen = run(go())
    assert seen["line"]["said"] == SENTENCE and seen["line"]["value"] == "", seen
    assert seen["line"]["buttons"] == ["Use it", "No, it's different"], seen
    [body] = seen["answered"]
    assert all("use_similar" not in a for a in body["answers"]), body
    assert LABEL not in [a["label"] for a in body["answers"]], body


def test_after_the_person_picks_another_choice_the_send_posts_their_value(srv, job_apply_home, monkeypatch):
    async def go():
        async with _card(srv, monkeypatch, _stops_state(), [OFFER]) as (page, calls):
            await _drawn(page)
            await page.click("[data-similar-use]")
            await page.select_option(f'form[data-answer="1"] [data-q="{LABEL}"]', "No")
            seen = {"line": await _line(page)}
            await _fill_send(page)
            seen["answered"] = calls["answered"]
            return seen

    seen = run(go())
    assert seen["line"]["said"] == SENTENCE and seen["line"]["using"] is None, seen
    [body] = seen["answered"]
    assert {"label": LABEL, "value": "No", "remember": True} in body["answers"], body
    assert all("use_similar" not in a for a in body["answers"]), body


# ---------------------------------------------------------------------------------------------- 3: No, it's different
def test_no_its_different_posts_the_decline_and_the_line_goes(srv, job_apply_home, monkeypatch):
    async def go():
        async with _card(srv, monkeypatch, _stops_state(), [OFFER]) as (page, calls):
            await _drawn(page)
            await page.click("[data-similar-no]")
            await page.wait_for_function("() => document.querySelector('[data-similar-for]').hidden")
            return calls["declined"], await page.locator("[data-similar-offer]").count()

    declined, lines = run(go())
    assert declined == [{"job_id": 1, "label": LABEL}] and lines == 0


def test_a_decline_the_route_refuses_shows_its_sentence_in_the_lines_place_and_the_buttons_stay(srv, job_apply_home,
                                                                                               monkeypatch):
    sentence = "That question has no suggested answer any more."

    async def go():
        async with _card(srv, monkeypatch, _stops_state(), [OFFER], decline=(400, {"error": sentence})) as (page, _):
            await _drawn(page)
            await page.click("[data-similar-no]")
            await page.wait_for_selector("[data-similar-err]:not([hidden])")
            return await _line(page)

    line = run(go())
    assert line["err"] == sentence and line["said"] is None, line
    assert line["buttons"] == ["Use it", "No, it's different"], line


# ---------------------------------------------------------------------------------------------- 4: the refresh
def test_the_line_and_a_pressed_use_it_survive_a_state_refresh_and_a_card_drawn_anew(srv, job_apply_home, monkeypatch):
    state = _stops_state()

    async def go():
        async with _card(srv, monkeypatch, state, [OFFER]) as (page, calls):
            await _drawn(page)
            seen = {}
            await page.evaluate("() => refresh()")
            await page.wait_for_timeout(300)
            seen["offer"] = await _line(page)
            await page.click("[data-similar-use]")
            await page.evaluate("() => refresh()")
            await page.wait_for_timeout(300)
            seen["used"] = await _line(page)
            seen["asked"] = calls["asked"]
            # (the card's words change, so it's drawn anew: the offer is asked again, and the choice is put back)
            state["listings"][0]["run"]["questions"][0]["options"].append("Maybe")
            await page.evaluate("() => refresh()")
            await page.wait_for_function(
                "() => document.querySelector('select[data-q] option:last-child').text === 'Maybe'")
            await page.wait_for_selector("[data-similar-using]:not([hidden])")
            seen["again"] = await _line(page)
            seen["asked again"] = calls["asked"]
            await _fill_send(page)
            seen["answered"] = calls["answered"]
            return seen

    seen = run(go())
    assert seen["offer"]["said"] == SENTENCE and seen["asked"] == 1, seen
    assert seen["used"]["using"] == "Using your earlier answer. Undo" and seen["used"]["value"] == ANSWER, seen
    assert seen["again"]["using"] == "Using your earlier answer. Undo" and seen["again"]["value"] == ANSWER, seen
    assert seen["asked again"] == 2, seen
    assert {"label": LABEL, "use_similar": True, "remember": True} in seen["answered"][0]["answers"], seen


# ---------------------------------------------------------------------------------------------- 5: the answer
def test_the_answer_is_in_no_attribute_no_storage_and_not_the_state_object(srv, job_apply_home, monkeypatch):
    probe = """(answer) => {
      const attrs = [];
      for (const el of document.querySelectorAll('*')) {
        for (const a of el.attributes) {
          if (a.value.includes(answer) || a.name.includes(answer)) attrs.push(el.tagName + ' ' + a.name);
        }
      }
      const stored = [localStorage, sessionStorage].map((s) => JSON.stringify(Object.entries(s))).join('');
      return {attrs, stored: stored.includes(answer), state: JSON.stringify(state).includes(answer),
        window: Object.keys(window).filter((k) => {
          try { return typeof window[k] === 'string' && window[k].includes(answer); }
          catch (e) { return false; } })}; }"""

    # (the offer's answer is one of the site's choices apart from case and spacing: said otherwise here, so what the
    # site's own choices hold, in the state and in the card's markup, isn't taken for the answer)
    said = "YES,  with my own car"
    offer = {**OFFER, "answer": said}

    async def go():
        async with _card(srv, monkeypatch, _stops_state(), [offer]) as (page, _):
            await _drawn(page)
            seen = {"offer": await page.evaluate(probe, said)}
            await page.click("[data-similar-use]")
            seen["chosen"] = await page.evaluate("() => document.querySelector('select[data-q]').value")
            await page.evaluate("() => refresh()")
            await page.wait_for_timeout(300)
            seen["used"] = await page.evaluate(probe, said)
            seen["earlier"] = await page.evaluate(probe, EARLIER)
            return seen

    seen = run(go())
    assert seen.pop("chosen") == ANSWER, seen
    for what, found in seen.items():
        assert found == {"attrs": [], "stored": False, "state": False, "window": []}, (what, seen)


# ---------------------------------------------------------------------------------------------- 6: the switch
_SWITCH = """() => { const s = document.getElementById('similar-me'), ok = document.getElementById('similar-me-ok'),
  err = s.closest('.setrow').querySelector('.set-err');
  return {checked: s.checked, said: document.getElementById('similar-me-said').textContent,
    by: document.getElementById('similar-me-by').textContent,
    name: document.getElementById('similar-me-name').textContent,
    ok: ok.hidden ? null : ok.textContent, err: err && !err.hidden ? err.textContent : null}; }"""


def test_the_switch_shows_the_state_saves_through_the_settings_route_and_says_saved(srv, job_apply_home, monkeypatch):
    monkeypatch.setattr("job_apply.desk._similar_by", lambda: "Claude")
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
                await page.click("#similar-me")
                await page.wait_for_selector("#similar-me-ok:not([hidden])")
                seen["off"] = await page.evaluate(_SWITCH)
                seen["kept"] = desk.applier.suggest_similar
                await page.click("#similar-me")
                await page.wait_for_function("() => !document.querySelector('[data-saving]')")
                await page.wait_for_selector("#similar-me-ok:not([hidden])")
                seen["on"] = await page.evaluate(_SWITCH)
                seen["posted"] = posted
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    name = "Suggest answers I've given to similar questions"
    assert seen["first"] == {"checked": True, "said": SWITCH_SAID, "by": "Uses Claude.", "name": name, "ok": None,
                             "err": None}, seen
    assert seen["off"]["checked"] is False and seen["off"]["ok"] == "Saved" and seen["off"]["err"] is None, seen
    assert seen["kept"] is False and seen["on"]["checked"] is True and seen["on"]["ok"] == "Saved", seen
    assert seen["posted"] == [{"suggest_similar": False}, {"suggest_similar": True}], seen
    assert desk.applier.suggest_similar is True


def test_the_last_line_says_what_is_used_or_that_nothing_is_suggested_without_a_model(srv, job_apply_home, monkeypatch):
    desk = _idle_desk(srv)
    by = {"words": "your Anthropic key"}
    monkeypatch.setattr("job_apply.desk._similar_by", lambda: by["words"])

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/applying")
                first = (await page.evaluate(_SWITCH))["by"]
                by["words"] = None
                desk._similar_by_said = None  # (the desk keeps its look for a minute: looked at afresh here)
                await page.evaluate("() => refresh()")
                await page.wait_for_function(
                    "(w) => document.getElementById('similar-me-by').textContent === w", arg=NO_MODEL)
                await browser.close()
                return first
        finally:
            await desk.stop()

    assert run(go()) == "Uses your Anthropic key."


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
                await page.click("#similar-me")
                await page.wait_for_function("() => !document.querySelector('[data-saving]')")
                await page.wait_for_selector("#similar-me ~ .set-err:not([hidden]), .setrow > .set-err:not([hidden])")
                seen = await page.evaluate(_SWITCH)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert (seen["checked"], seen["ok"], seen["err"]) == (True, None, sentence), seen
    assert desk.applier.suggest_similar is True


@pytest.mark.parametrize("on", [True, False])
def test_the_safety_window_draws_the_suggestion_line_like_the_others(srv, job_apply_home, monkeypatch, on):
    monkeypatch.setattr("job_apply.desk._similar_by", lambda: "Claude")
    desk = _idle_desk(srv)
    desk.applier.set_suggest_similar(on)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "settings/applying")
                await page.click("#safety-btn")
                await page.wait_for_selector("#safety[open] li[data-safety=suggest_similar]")
                seen = await page.evaluate("""() => {
                  const li = document.querySelector('#safety li[data-safety=suggest_similar]');
                  const a = li.querySelector('a'); return {text: li.innerText.replace(/\\s+/g, ' ').trim(),
                    href: a && a.getAttribute('href'), strong: li.querySelector('strong').innerText}; }""")
                await page.click("#safety li[data-safety=suggest_similar] a")
                seen["panel"] = await page.evaluate("() => document.body.dataset.panel")
                seen["open"] = await page.evaluate("() => document.getElementById('safety').open")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    value = "on" if on else "off"
    assert seen["text"] == f"{SAFETY_NAME}: {value}. {SAFETY_SAID} Where: {SAFETY_WHERE}.", seen
    assert seen["strong"] == f"{SAFETY_NAME}: {value}.", seen
    assert (seen["href"], seen["panel"], seen["open"]) == ("#settings/applying", "applying", False), seen


# ---------------------------------------------------------------------------------------------- 7: Your answers
def test_your_answers_draws_matched_to_under_a_question_with_matched_and_not_without(srv, job_apply_home):
    config.save_answer(LABEL, "Yes", matched=EARLIER)
    config.save_answer("Are you willing to work weekends?", "Yes")
    config.save_answer("Hours", "Part time", matched='<b>Hrs</b> & "more"')
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "answers")
                await page.wait_for_selector("#remembered-card[data-loaded]", state="attached")
                seen = await page.evaluate("""() => Object.fromEntries(
                  [...document.querySelectorAll('#remembered > .setrow')].map((row) => [
                    row.querySelector('.t').innerText, [...row.querySelectorAll('.s')].map((s) => s.innerText)]))""")
                seen["markup"] = await page.locator("#remembered .s b").count()
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen.pop("markup") == 0
    assert seen == {LABEL: [f"Matched to: “{EARLIER}”"], "Are you willing to work weekends?": [],
                    "Hours": ['Matched to: “<b>Hrs</b> & "more"”']}, seen


# ---------------------------------------------------------------------------------------------- 8: the keyboard
def test_focus_goes_to_undo_after_use_it_the_question_after_no_and_use_it_after_undo(srv, job_apply_home, monkeypatch):
    async def go():
        async with _card(srv, monkeypatch, _stops_state(), [OFFER]) as (page, _):
            await _drawn(page)
            seen = {}
            # (Tab: Use it, then No, then the question's own control; the ring shows on what the keyboard reached)
            await page.focus("[data-similar-use]")
            order = [await page.evaluate(_FOCUS)]
            ring = await page.evaluate("""() => { const s = getComputedStyle(document.activeElement);
              return [s.outlineStyle, parseFloat(s.outlineWidth)]; }""")
            for _ in range(2):
                await page.keyboard.press("Tab")
                order.append(await page.evaluate(_FOCUS))
            seen["order"], seen["ring"] = order, ring
            await page.focus("[data-similar-use]")
            await page.keyboard.press("Enter")
            seen["after use"] = await page.evaluate(_FOCUS)
            await page.keyboard.press("Enter")  # (Undo)
            seen["after undo"] = await page.evaluate(_FOCUS)
            await page.keyboard.press("Tab")
            await page.keyboard.press("Enter")  # (No, it's different)
            await page.wait_for_function("() => document.querySelector('[data-similar-for]').hidden")
            seen["after no"] = await page.evaluate(_FOCUS)
            return seen

    seen = run(go())
    assert seen["order"] == ["use", "no", "question"], seen
    assert seen["ring"][0] != "none" and seen["ring"][1] >= 2, seen
    assert (seen["after use"], seen["after undo"], seen["after no"]) == ("undo", "use", "question"), seen


# ---------------------------------------------------------------------------------------------- the pictures
@pytest.mark.skipif(not SHOTS, reason="JOB_APPLY_SHOTS names no folder for the pictures")
@pytest.mark.parametrize("window", [(1280, 1000), (420, 900)], ids=lambda w: f"{w[0]}x{w[1]}")
@pytest.mark.parametrize("look", ["light", "dark"])
def test_pictures_of_a_card_with_an_offer_after_use_it_and_the_applying_panel(srv, job_apply_home, monkeypatch, look,
                                                                              window):
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    size = f"{look}-{window[0]}x{window[1]}"
    monkeypatch.setattr("job_apply.desk._similar_by", lambda: "Claude")

    async def applying():
        async with _card(srv, monkeypatch, _stops_state(), [OFFER]) as (page, _):
            await page.set_viewport_size({"width": window[0], "height": window[1]})
            await page.emulate_media(color_scheme=look)
            await page.goto(page.url.split("#")[0] + "#settings/applying")
            await page.wait_for_selector("#similar-me")
            await page.evaluate("() => document.getElementById('similar-me').scrollIntoView({block: 'center'})")
            await page.wait_for_timeout(150)
            await page.screenshot(path=str(folder / f"similar-applying-{size}.png"))

    async def card_only():
        async with _card(srv, monkeypatch, _stops_state(), [OFFER]) as (page, _):
            await page.set_viewport_size({"width": window[0], "height": window[1]})
            await page.emulate_media(color_scheme=look)
            await _drawn(page)
            await page.evaluate("() => document.querySelector('[data-similar-for]').scrollIntoView({block: 'center'})")
            await page.wait_for_timeout(150)
            await page.screenshot(path=str(folder / f"similar-offer-{size}.png"))
            await page.click("[data-similar-use]")
            await page.wait_for_timeout(150)
            await page.screenshot(path=str(folder / f"similar-used-{size}.png"))

    run(card_only())
    run(applying())
    assert all((folder / f"similar-{n}-{size}.png").stat().st_size > 2000 for n in ("offer", "used", "applying"))

