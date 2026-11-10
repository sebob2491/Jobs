"""Home's three lists and a job's card as a row that opens (the design's Version C: C2 and C3; its C9 tests 1 to 4).
Each test opens the real page in a browser. Made-up employers only: the repo is public."""

import time

import pytest
from conftest import browser_available, open_desk, person_browser, run
from test_desk import _stopped_desk

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

# Each list's cards, in its order, by employer (the card's heading's words after the "·")
_LISTS = """() => Object.fromEntries(['needs', 'done', 'finished'].map((id) => [id,
  [...document.querySelectorAll(`#${id} > .task`)].map((t) => t.querySelector('h3 .small').innerText.replace('·', '').trim())]))"""
# Each card's body: shown or not, and its open button's aria-expanded, by employer
_OPEN = """() => Object.fromEntries([...document.querySelectorAll('#needs > .task, #done > .task, #finished > .task')].map((t) =>
  [t.querySelector('h3 .small').innerText.replace('·', '').trim(),
   [!t.querySelector('[data-body]').hidden, t.querySelector('[data-open]').getAttribute('aria-expanded')]]))"""


def test_each_list_holds_exactly_its_states_and_the_title_counts_the_first_two(srv, job_apply_home):
    """C9's test 1. Needs you: the jobs stopped for the person on anything but a Submit (the one holding the queue first,
    then the longest waiting), then the ones that didn't finish. Ready to submit: the ready ones, then the ones stopped on
    a CAPTCHA, the person's own Submit, a check whether it went in, or a Submit that didn't go. Done this visit: sent and
    skipped. The page's title and the Home menu item count the first two lists together."""
    desk, jobs = _stopped_desk(srv, {
        "Example Failed": dict(status="failed"),
        "Example Questions": dict(status="needs_you", need="questions"),
        "Example Sign In": dict(status="needs_you", need="sign_in"),
        "Example Bot": dict(status="needs_you", need="bot_check"),
        "Example Stuck": dict(status="needs_you", need="stuck"),
        "Example Blocking": dict(status="needs_you", need="email_code", blocking=True),
        "Example Captcha": dict(status="needs_you", need="captcha"),
        "Example Own Submit": dict(status="needs_you", need="your_submit"),
        "Example Ready": dict(status="ready"),
        "Example Check": dict(status="needs_you", need="check_submit"),
        "Example Not Sent": dict(status="needs_you", need="submit_failed"),
        "Example Sent": dict(status="submitted"),
        "Example Skipped": dict(status="skipped"),
        "Example Running": dict(status="running"),
    })
    desk.applier.runs[jobs["Example Stuck"]].paused_at = 500.0  # (the longest waiting of those not holding the queue)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk, "home")
                await page.wait_for_selector("#finished > .task")
                lists = await page.evaluate(_LISTS)
                said = {"title": await page.title(), "menu": await page.inner_text("#nav-home"),
                        "needs": await page.inner_text("#needs-count"), "done": await page.inner_text("#done-count"),
                        "finished-h": await page.inner_text("#finished-h")}
                await browser.close()
                return lists, said
        finally:
            await desk.stop()

    lists, said = run(go())
    assert lists == {
        "needs": ["Example Blocking", "Example Stuck", "Example Questions", "Example Sign In", "Example Bot",
                  "Example Failed"],
        "done": ["Example Ready", "Example Captcha", "Example Own Submit", "Example Check", "Example Not Sent"],
        "finished": ["Example Sent", "Example Skipped"]}, lists
    assert said == {"title": "(11) Needs you · Job Desk", "menu": "11", "needs": "6", "done": "5 to submit",
                    "finished-h": "Done this visit"}, said


def test_empty_lists_say_so_or_hide(srv, job_apply_home):
    """C2's empty cards: Needs you says "Nothing needs you right now." (the old idle card's id), Ready to submit and Done
    this visit are hidden, and Working on says what to do, with a link to Openings. The old counts are gone."""
    desk, _ = _stopped_desk(srv, {})

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk, "home")
                await page.wait_for_selector("#needs #idle-card")
                seen = {"idle": await page.inner_text("#idle-card"),
                        "idle tag": await page.eval_on_selector("#idle-card", "(p) => [p.tagName, p.className]"),
                        "done shown": await page.is_visible("#done-h"),
                        "finished shown": await page.is_visible("#finished-h"),
                        "work": " ".join((await page.inner_text("#work")).split()),
                        "link": await page.get_attribute("#work a", "href"),
                        "counts": await page.locator("#counts, #idle-counts").count(),
                        "title": await page.title()}
                await page.click("#work a")
                await page.wait_for_function("() => document.body.dataset.page === 'openings'")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen == {"idle": "Nothing needs you right now.", "idle tag": ["P", "empty"], "done shown": False,
                    "finished shown": False,
                    "work": "Nothing is being filled. Tick openings and press Apply. Go to Openings",
                    "link": "#openings", "counts": 0, "title": "Job Desk"}, seen


def test_three_cards_start_open_and_four_closed_but_the_one_holding_the_queue(srv, job_apply_home):
    """C9's test 2. With three or fewer jobs in a list every card in it starts open; with more they start closed, except
    one holding the queue. A card the person opens or closes stays so, refreshes or not. Each row has its icon in a
    tinted square, its heading, one line of its sentence, and one button; the chevron says whether it's open."""
    three, _ = _stopped_desk(srv, {"Example A": dict(status="needs_you", need="questions"),
                                   "Example B": dict(status="needs_you", need="sign_in"),
                                   "Example C": dict(status="needs_you", need="stuck")})
    four, jobs = _stopped_desk(srv, {"Example A": dict(status="needs_you", need="questions"),
                                     "Example B": dict(status="needs_you", need="sign_in", blocking=True),
                                     "Example C": dict(status="needs_you", need="stuck"),
                                     "Example D": dict(status="needs_you", need="tailor")})

    async def look(desk, press: int | None = None):
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk, "home")
                await page.wait_for_selector("#needs > .task [data-open][aria-expanded]")
                seen = {"open": await page.evaluate(_OPEN)}
                seen["rows"] = await page.evaluate("""() => [...document.querySelectorAll('#needs > .task')].map((t) => {
                    const row = t.querySelector('.trow');
                    return [row.querySelector('.itico').className, row.querySelector('.itico use').getAttribute('href'),
                            row.querySelector('.acts button:not([data-open])').innerText,
                            !!row.querySelector('h3 .pill'), row.querySelector('.tline').innerText,
                            row.querySelector('[data-open]').getAttribute('aria-label')];
                })""")
                if press is not None:
                    await page.click(f"#needs [data-open='{press}']")  # opened
                    await page.click(f"#needs [data-open='{jobs['Example B']}']")  # and the open one closed
                    await page.wait_for_timeout(3200)  # (two refreshes)
                    seen["after"] = await page.evaluate(_OPEN)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen3 = run(look(three))
    seen4 = run(look(four, jobs["Example C"]))
    assert seen3["open"] == {"Example A": [True, "true"], "Example B": [True, "true"], "Example C": [True, "true"]}, seen3
    assert seen4["open"] == {"Example A": [False, "false"], "Example B": [True, "true"], "Example C": [False, "false"],
                             "Example D": [False, "false"]}, seen4
    assert seen4["after"] == {"Example A": [False, "false"], "Example B": [False, "false"],
                              "Example C": [True, "true"], "Example D": [False, "false"]}, seen4
    assert seen4["rows"] == [
        ["itico amber", "#i-key", "Show me", True, "Waiting.", "Open or close this card"],  # (holding the queue: first)
        ["itico indigo", "#i-ask", "Answer", True, "Waiting.", "Open or close this card"],
        ["itico red", "#i-alert", "Show me", True, "Waiting.", "Open or close this card"],
        ["itico gray", "#i-file", "Use my usual resume", True, "Waiting.", "Open or close this card"]], seen4


def test_typing_in_a_card_survives_closing_opening_and_refreshes(srv, job_apply_home):
    """C9's test 3. What's typed into a card's box is still there after the card is closed and opened again, and after
    two refreshes. Its row's Answer opens a closed card and puts the cursor in its first box."""
    box = {"id": "y", "kind": "text", "label": "Years in a cleanroom", "required": True}
    desk, jobs = _stopped_desk(srv, {f"Example {c}": dict(status="needs_you", need="questions", questions=[box])
                                     for c in "ABCD"})
    a = jobs["Example A"]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk, "home")
                card = page.locator(f"#needs > .task[data-task='{a}']")
                await card.locator("[data-open][aria-expanded=false]").wait_for()
                await card.locator(".trow button", has_text="Answer").click()  # (four: closed; Answer opens it)
                focused = await page.evaluate("() => document.activeElement.dataset.q || ''")
                await page.keyboard.type("3 years")
                await card.locator("[data-open]").click()  # closed
                hidden = await card.locator("[data-body]").is_hidden()
                await card.locator("[data-open]").click()  # and opened again
                await page.wait_for_timeout(3200)  # (two refreshes)
                kept = await card.locator('input[data-q="Years in a cleanroom"]').input_value()
                still_open = await card.locator("[data-body]").is_visible()
                await browser.close()
                return focused, hidden, kept, still_open
        finally:
            await desk.stop()

    focused, hidden, kept, still_open = run(go())
    assert (focused, hidden, kept, still_open) == ("Years in a cleanroom", True, "3 years", True)


def test_the_wait_is_written_by_the_clock_and_the_card_stays_the_same(srv, job_apply_home):
    """C9's test 4. A row's wait says "just now" under a minute, then "12 min", then "1 h 5 min", from when the job
    stopped (paused_at); and a card's HTML string holds no clock, so it is the same a minute apart (the card isn't
    drawn again) while its wait moves on."""
    desk, jobs = _stopped_desk(srv, {"Example Now": dict(status="needs_you", need="sign_in"),
                                     "Example Twelve": dict(status="needs_you", need="stuck"),
                                     "Example Hour": dict(status="needs_you", need="questions")})
    now = time.time()
    for name, ago in (("Example Now", 10), ("Example Twelve", 12 * 60 + 5), ("Example Hour", 65 * 60 + 5)):
        desk.applier.runs[jobs[name]].paused_at = now - ago
    waits = """() => Object.fromEntries([...document.querySelectorAll('#needs > .task')].map((t) =>
      [t.querySelector('h3 .small').innerText.replace('·', '').trim(), t.querySelector('.wait[data-since]').innerText]))"""
    marks = """() => [...document.querySelectorAll('#needs > .task')].map((t) => {
      t.__seen = t.__seen || Math.random(); return [t.__seen, t.dataset.html]; })"""

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.clock.set_fixed_time(now)
                await open_desk(page, desk, "home")
                await page.wait_for_function("() => document.querySelectorAll('#needs .wait[data-since]').length === 3")
                await page.wait_for_function("() => [...document.querySelectorAll('#needs .wait')].every((w) => w.innerText)")
                first = await page.evaluate(waits)
                before = await page.evaluate(marks)
                await page.clock.set_fixed_time(now + 60)
                await page.wait_for_timeout(3200)  # (two refreshes)
                later = await page.evaluate(waits)
                after = await page.evaluate(marks)
                await browser.close()
                return first, later, before, after
        finally:
            await desk.stop()

    first, later, before, after = run(go())
    assert first == {"Example Hour": "1 h 5 min", "Example Twelve": "12 min", "Example Now": "just now"}, first
    assert later == {"Example Hour": "1 h 6 min", "Example Twelve": "13 min", "Example Now": "1 min"}, later
    assert before == after  # (the same cards, their HTML strings unchanged)
