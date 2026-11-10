"""Home's three lists and a job's card as a row that opens (the design's Version C: C2 and C3; its C9 tests 1 to 4).
Each test opens the real page in a browser. Made-up employers only: the repo is public."""

import copy
import time

import pytest
from conftest import browser_available, open_desk, person_browser, run
from test_desk import _stopped_desk

from job_apply.desk import Desk

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

# A card's employer (its heading's words after the "·")
_WHO = "(t) => t.querySelector('h3 .small').innerText.replace('·', '').trim()"
# Each list's cards, in its order, by employer
_LISTS = f"""() => Object.fromEntries(['needs', 'done', 'finished'].map((id) => [id,
  [...document.querySelectorAll(`#${{id}} > .task`)].map({_WHO})]))"""
# Each card's body: shown or not, and its open button's aria-expanded, by employer
_OPEN = f"""() => Object.fromEntries([...document.querySelectorAll('#needs > .task, #done > .task, #finished > .task')]
  .map((t) => [({_WHO})(t), [!t.querySelector('[data-body]').hidden,
                              t.querySelector('[data-open]').getAttribute('aria-expanded')]]))"""


def test_each_list_holds_exactly_its_states_and_the_title_counts_the_first_two(srv, job_apply_home):
    """C9's test 1. Needs you: the jobs stopped for the person on anything but a Submit (the one holding the queue
    first, then the longest waiting), then the ones that didn't finish. Ready to submit: the ready ones, then the ones
    stopped on a CAPTCHA, the person's own Submit, a check whether it went in, or a Submit that didn't go. Done this
    visit: sent and skipped. The page's title and the Home menu item count the first two lists together."""
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
    """C2's empty cards: Needs you says "Nothing needs you right now." (the old idle card's id), Ready to submit and
    Done this visit are hidden, and Working on says what to do, with a link to Openings. The old counts are gone."""
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
                seen["rows"] = await page.evaluate("""() => [...document.querySelectorAll('#needs > .task')]
                  .map((t) => {
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
    assert seen3["open"] == {"Example A": [True, "true"], "Example B": [True, "true"],
                             "Example C": [True, "true"]}, seen3
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
    waits = f"""() => Object.fromEntries([...document.querySelectorAll('#needs > .task')].map((t) =>
      [({_WHO})(t), t.querySelector('.wait[data-since]').innerText]))"""
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
                await page.wait_for_function(
                    "() => [...document.querySelectorAll('#needs .wait')].every((w) => w.innerText)")
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


# The pass's header (its words, or its button's), the card it's on, and each Needs you card's open state, by employer
_PASS = f"""() => ({{header: document.querySelector('#needs-pass').innerText.replace(/\\s+/g, ' ').trim(),
  now: [...document.querySelectorAll('#needs > .task.now')].map({_WHO}),
  open: Object.fromEntries([...document.querySelectorAll('#needs > .task')].map((t) =>
    [({_WHO})(t), !t.querySelector('[data-body]').hidden]))}})"""
# Whether the card the pass is on is the given employer's
_NOW_IS = "(who) => document.querySelector('#needs > .task.now h3 .small')?.innerText.includes(who)"


def test_going_through_them_one_job_at_a_time(srv, job_apply_home):
    """C9's test 5. "Go through them" starts on the first job in Needs you that isn't a bot check: its card open and
    marked now, the others closed, the cursor in its first box. Stop ends the pass. Answering moves on by itself (the
    desk carries on with the job: it leaves the list); a sign-in brings its tab up once; Next leaves one for later; and
    with only bot checks left the pass hands over to the existing bot round."""
    box = {"id": "y", "kind": "text", "label": "Years in a cleanroom", "required": True}
    desk, jobs = _stopped_desk(srv, {"Example Bot": dict(status="needs_you", need="bot_check"),
                                     "Example Questions": dict(status="needs_you", need="questions", questions=[box]),
                                     "Example Sign In": dict(status="needs_you", need="sign_in"),
                                     "Example Stuck": dict(status="needs_you", need="stuck")})
    shown: list[int] = []

    async def focus(job_id):
        shown.append(job_id)
        return True
    desk.applier.focus = focus
    rounds: list[int] = []
    desk.applier.start_round = rounds.append

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk, "home")
                start = page.locator("#needs-pass [data-act=pass-start]")
                await start.wait_for()
                seen = {"button": [await start.inner_text(), await start.get_attribute("class"),
                                   await start.locator("use").get_attribute("href")]}
                await start.click()
                await page.wait_for_function("() => document.querySelector('#needs > .task.now')")
                seen["started"] = await page.evaluate(_PASS)
                seen["cursor"] = await page.evaluate("() => document.activeElement.dataset.q || ''")
                seen["next tip"] = await page.get_attribute("#needs-pass [data-act=pass-next]", "title")
                await page.click("#needs-pass [data-act=pass-stop]")
                await page.locator("#needs-pass [data-act=pass-start]").wait_for()
                seen["stopped"] = (await page.evaluate(_PASS))["now"]
                await page.click("#needs-pass [data-act=pass-start]")
                await page.wait_for_function("() => document.querySelector('#needs > .task.now')")
                seen["again"] = (await page.evaluate(_PASS))["now"]
                run_q = desk.applier.runs[jobs["Example Questions"]]
                run_q.status, run_q.need = "running", ""  # answered: the desk carries on with it
                await page.wait_for_function(_NOW_IS, arg="Sign In")
                await page.wait_for_timeout(3200)  # (two refreshes: still one show)
                seen["sign in"] = await page.evaluate(_PASS)
                seen["shown"] = list(shown)
                await page.click("#needs-pass [data-act=pass-next]")
                await page.wait_for_function(_NOW_IS, arg="Stuck")
                seen["stuck"] = await page.evaluate(_PASS)
                await page.click("#needs-pass [data-act=pass-next]")  # (only the bot check left: the bot round)
                await page.wait_for_selector("#notices [data-act=bot-next]")
                for _ in range(40):
                    if rounds:
                        break
                    await page.wait_for_timeout(100)
                seen["handed"] = await page.evaluate(_PASS)
                seen["rounds"], seen["shown at end"] = list(rounds), list(shown)
                seen["toast"] = await page.inner_text("#toast")
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    bot, signin = jobs["Example Bot"], jobs["Example Sign In"]
    assert seen["button"] == ["Go through them", "btn primary sm", "#i-arrow"], seen
    assert seen["started"] == {"header": "Going through them · 4 left Next Stop", "now": ["Example Questions"],
                               "open": {"Example Bot": False, "Example Questions": True, "Example Sign In": False,
                                        "Example Stuck": False}}, seen
    assert seen["cursor"] == "Years in a cleanroom", seen
    assert seen["next tip"] == "Leave this one for later", seen
    assert seen["stopped"] == [] and seen["again"] == ["Example Questions"], seen
    assert seen["sign in"] == {"header": "Going through them · 3 left Next Stop", "now": ["Example Sign In"],
                               "open": {"Example Bot": False, "Example Sign In": True, "Example Stuck": False}}, seen
    assert seen["shown"] == [signin], seen
    assert seen["stuck"] == {"header": "Going through them · 2 left Next Stop", "now": ["Example Stuck"],
                             "open": {"Example Bot": False, "Example Sign In": False, "Example Stuck": True}}, seen
    assert seen["handed"]["now"] == [] and seen["handed"]["header"] == "Go through them", seen
    assert seen["rounds"] == [bot] and seen["shown at end"] == [signin, bot], seen
    assert seen["toast"] == "", seen  # (no closing word: the bot round goes on)


def test_the_pass_ends_with_a_word_on_what_is_left(srv, job_apply_home):
    """C9's test 6. With nothing left the pass says "That is everything that needed you."; with jobs passed over with
    Next, "Done for now. 2 left for later." """
    shapes = {"Example A": dict(status="needs_you", need="stuck"), "Example B": dict(status="needs_you", need="tailor")}
    every, every_jobs = _stopped_desk(srv, shapes)
    later, _ = _stopped_desk(srv, shapes)

    async def go(desk, jobs=None):
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk, "home")
                await page.click("#needs-pass [data-act=pass-start]")
                await page.wait_for_function("() => document.querySelector('#needs > .task.now')")
                if jobs:  # the desk carries on with both
                    for job_id in jobs.values():
                        desk.applier.runs[job_id].status, desk.applier.runs[job_id].need = "running", ""
                else:  # both left for later
                    await page.click("#needs-pass [data-act=pass-next]")
                    await page.wait_for_function(_NOW_IS, arg="Example B")
                    await page.click("#needs-pass [data-act=pass-next]")
                await page.wait_for_function("() => document.querySelector('#toast').innerText")
                said = [await page.inner_text("#toast"), await page.inner_text("#needs-pass")]
                await browser.close()
                return said
        finally:
            await desk.stop()

    assert run(go(every, every_jobs)) == ["That is everything that needed you.", ""]
    assert run(go(later)) == ["Done for now. 2 left for later.", "Go through them"]


# Each running job in Working on: its employer, its steps (each bar's class), its step labels, the label marked on, how
# long it has run, how many log lines, and its Report a problem
_STEPS = """() => [...document.querySelectorAll('#work > .task[data-run]')].map((t) => [
  t.querySelector('.head span').innerText,
  [...t.querySelectorAll('.steps i')].map((i) => i.className),
  [...t.querySelectorAll('.steplabels span')].map((s) => s.innerText),
  [...t.querySelectorAll('.steplabels .on')].map((s) => s.innerText),
  t.querySelector('.head [data-since]')?.innerText || '',
  t.querySelectorAll('.log li').length, t.querySelector('[data-job-act=report]')?.innerText || ''])"""


def test_each_running_job_draws_the_step_it_has_reached(srv, job_apply_home):
    """C9's test 7. Working on: each running job with its title, its company, how long it has run (from its first
    log_at), four steps (Opened, Account, Form, Review), those before the one reached done and that one now, by C5's
    table: Review when its need is one of the four Submits, Form when it has seen the form, Account when it has the
    inbox or waits on a sign-in or an emailed code, else Opened. Its last four log lines and Report a problem. Under
    them, the jobs waiting their turn in a <details> that lists each by its title and company."""
    desk, jobs = _stopped_desk(srv, {
        "Example Opened": dict(status="running"),
        "Example Inbox": dict(status="running", inbox=True),
        "Example Code": dict(status="running", need="email_code"),
        "Example Form": dict(status="running", seen_form=True, inbox=True),
        "Example Review": dict(status="running", seen_form=True, need="check_submit"),
        "Example Next": dict(status="queued"),
        "Example Last": dict(status="queued"),
    })
    now = time.time()
    for job_id in jobs.values():
        r = desk.applier.runs[job_id]
        if r.status == "running":  # (five lines, the last four shown; the first logged without a time, then 2 min ago)
            r.log = [f"line {n}" for n in range(5)]
            r.log_at = [0.0, now - 125, now - 100, now - 60, now - 30]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await page.clock.set_fixed_time(now)
                await open_desk(page, desk, "home")
                await page.wait_for_function("""() => [...document.querySelectorAll('#work [data-since]')]
                    .filter((s) => s.innerText).length === 5""")
                seen = {"steps": await page.evaluate(_STEPS)}
                waiting = page.locator("#work details")
                seen["summary"] = await waiting.locator("summary").inner_text()
                seen["closed"] = await page.inner_text("#work")
                await waiting.locator("summary").click()
                seen["waiting"] = await waiting.locator("li").all_inner_texts()
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    labels = ["Opened", "Account", "Form", "Review"]

    def drawn(company, reached):
        return [company, ["done"] * reached + ["now"] + [""] * (3 - reached), labels, [labels[reached]], "2 min", 4,
                "Report a problem"]
    assert seen["steps"] == [drawn("Example Opened", 0), drawn("Example Inbox", 1), drawn("Example Code", 1),
                             drawn("Example Form", 2), drawn("Example Review", 3)], seen
    assert seen["summary"] == "2 more waiting their turn", seen
    assert "Example Next" not in seen["closed"], seen  # (listed only once it's opened)
    assert seen["waiting"] == ["Technician · Example Next", "Technician · Example Last"], seen


# The tiles, each as [its label, its number, the line under it, whether it asks for attention]; This week's bars, each
# as [the day, how many, its bar's class]; its three numbers with their labels; its header's link; Home's cards in order
_TILES = """() => ({
  tiles: [...document.querySelectorAll('#tiles .tile')].map((t) => [t.querySelector('.label').innerText,
    t.querySelector('.num').innerText, t.querySelector('.sub').innerText, t.classList.contains('attn')]),
  bars: [...document.querySelectorAll('#week-bars > div')].map((d) => [d.querySelector('span').innerText,
    Number(d.dataset.n), d.querySelector('i').className]),
  stats: [...document.querySelectorAll('#week-stats > div')].map((d) => d.innerText.replace(/\\s+/g, ' ')),
  link: [document.querySelector('#thisweek-link').innerText,
    document.querySelector('#thisweek-link').getAttribute('href')],
  order: [...document.querySelectorAll('[data-pages=home] .card')]
    .map((c) => c.id || c.getAttribute('aria-labelledby'))})"""


def _app(n: int, applied_at: str, group: str = "waiting", heard: str = "") -> dict:
    return {"id": 900 + n, "title": "Technician", "company": f"Example Sent {n}", "url": f"https://example.com/s{n}",
            "status": "interviewing" if group == "interviews" else "applied", "applied_at": applied_at,
            "updated_at": applied_at, "group": group, "emails": 1 if heard else 0, "follow_up_due": None,
            "last_heard": {"category": "interview", "received_at": heard, "day": "", "summary": ""} if heard else None}


# Seven applications: four applied for in the 7 days ending Friday the 13th in Phoenix (Saturday the 7th, Tuesday, late
# Thursday there, which is Friday in UTC, and Friday); one the day before those 7 that heard back in them (an email's
# Date header) and one that heard back in them (ISO); one heard back before them; one long before. Two at the
# interview stage
_APPS = [_app(1, "2026-11-13T17:00:00+00:00"), _app(2, "2026-11-13T02:00:00+00:00"),
         _app(3, "2026-11-10T16:00:00+00:00", heard="2026-11-12T18:00:00+00:00"), _app(4, "2026-11-07T20:00:00+00:00"),
         _app(5, "2026-11-06T20:00:00+00:00", "interviews", heard="Wed, 11 Nov 2026 09:30:00 -0700"),
         _app(6, "2026-10-20T16:00:00+00:00", "interviews", heard="2026-11-01T16:00:00+00:00"),
         _app(7, "2026-10-01T16:00:00+00:00")]


def test_the_tiles_and_this_week_from_fixed_applications_and_a_fixed_clock(srv, job_apply_home, monkeypatch):
    """C9's test 8. The four tiles: Needs you (up to three kinds from its list, in its order; attn above 0), Working on
    (the running jobs, and how many more wait, or Paused), Ready to submit (and what becomes of them), Sent this week
    (the applications applied for in the last 7 days, and how many in all). This week: seven bars, a day each, ending
    today, today's marked; under them sent, answers and interviews; and the link to What's working in its header. All
    from a fixed set of applications, on a fixed clock (Friday the 13th of November 2026, 3 pm in Phoenix)."""
    from datetime import datetime, timedelta, timezone

    # (Phoenix keeps one time all year, 7 hours behind UTC: said so here, since Windows has no list of time zones for
    # Python to look "America/Phoenix" up in; the browser below has its own)
    friday = datetime(2026, 11, 13, 15, 0, tzinfo=timezone(timedelta(hours=-7))).timestamp()
    state = Desk.state
    monkeypatch.setattr(Desk, "state", lambda self: {**state(self), "applications": copy.deepcopy(_APPS)})
    busy, _ = _stopped_desk(srv, {
        "Example Questions": dict(status="needs_you", need="questions"),
        "Example Sign In": dict(status="needs_you", need="sign_in"),
        "Example Questions Too": dict(status="needs_you", need="questions"),
        "Example Bot": dict(status="needs_you", need="bot_check"),
        "Example Stuck": dict(status="needs_you", need="stuck"),
        "Example Running": dict(status="running"), "Example Running Too": dict(status="running"),
        "Example Queued": dict(status="queued"), "Example Queued Too": dict(status="queued"),
        "Example Queued Three": dict(status="queued"),
        "Example Ready": dict(status="ready"), "Example Captcha": dict(status="needs_you", need="captcha")})
    quiet, _ = _stopped_desk(srv, {})
    paused, _ = _stopped_desk(srv, {
        "Example Tailor": dict(status="needs_you", need="tailor"),
        "Example Code": dict(status="needs_you", need="email_code"),
        "Example Failed": dict(status="failed"),
        "Example Waiting": dict(status="queued"), "Example Ready": dict(status="ready")})
    paused.applier.paused = True
    paused.applier.auto_submit = True

    async def look(desk):
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(timezone_id="America/Phoenix")
                await page.clock.set_fixed_time(friday)
                await open_desk(page, desk, "home")
                await page.wait_for_selector("#plugin-version:not(:empty)")
                await page.wait_for_selector("#week-bars > div")
                seen = await page.evaluate(_TILES)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    week = {"bars": [["Sat", 1, ""], ["Sun", 0, "none"], ["Mon", 0, "none"], ["Tue", 1, ""], ["Wed", 0, "none"],
                     ["Thu", 1, ""], ["Fri", 1, "today"]],
            "stats": ["4 sent", "2 answers", "2 interviews"], "link": ["What's working", "#working"]}
    quiet_seen = run(look(quiet))  # (first: a desk stopped keeps its queue, and the next one started picks it up)
    busy_seen = run(look(busy))
    assert busy_seen == {"tiles": [["Needs you", "5", "Questions, a sign-in, a bot check", True],
                                   ["Working on", "2", "3 more waiting", False],
                                   ["Ready to submit", "2", "Filled in, waiting for your OK", False],
                                   ["Sent this week", "4", "7 sent in all", False]],
                         **week, "order": ["start", "tile-needs", "tile-work", "tile-ready", "tile-sent", "needs-h",
                                           "done-h", "finished-h", "work-h", "thisweek-h"]}, busy_seen
    assert quiet_seen["tiles"] == [["Needs you", "0", "Nothing right now", False],
                                   ["Working on", "0", "Nothing waiting", False],
                                   ["Ready to submit", "0", "Nothing yet", False],
                                   ["Sent this week", "4", "7 sent in all", False]], quiet_seen
    assert {k: quiet_seen[k] for k in week} == week, quiet_seen
    paused_seen = run(look(paused))
    assert paused_seen["tiles"] == [["Needs you", "3", "Something to decide, an emailed code, something stuck", True],
                                    ["Working on", "0", "Paused", False],
                                    ["Ready to submit", "1", "The desk submits these for you", False],
                                    ["Sent this week", "4", "7 sent in all", False]], paused_seen


# Getting started (C6): its count, and each line's name, sentence, whether it's done (ticked and dimmed) and its button
_START = """() => {
  const card = document.getElementById('start');
  return {shown: !!card && card.getClientRects().length > 0,
    count: card ? document.getElementById('start-count').innerText : null,
    lines: card ? [...card.querySelectorAll('#start-lines > .item')].map((l) => [l.querySelector('.t').innerText,
      l.querySelector('.s').innerText, l.classList.contains('done'),
      l.querySelector('.itico use').getAttribute('href'),
      [...l.querySelectorAll('.acts .btn')].map((b) => [b.innerText, b.dataset.act || b.getAttribute('href')])]) : []};
}"""
_NAMES = ["Set up your profile", "Choose what to look for, and where", "Save your site passwords",
          "Let the desk read emailed codes", "Answer the common questions", "Find jobs", "Apply to your first job"]
_SAID = ["Your details, your work history and your resume.", "The job titles and the places Find jobs searches.",
         "The desk signs in with them and makes your accounts. {pw}.",
         "Optional. Many sites email a code when you sign in. With this, the desk enters it for you.",
         "{bank}. Each answer is one stop fewer.", "Search the employers on your lists.",
         "Tick an opening and press Apply. Nothing is sent until you press Submit."]
# (the two Open Settings to the panel each password is on: the job systems' on Accounts and passwords, the inbox's
# app password on Email codes: the design's D1 and D3)
_BUTTONS = [["Set up", "setup"], ["Choose", "area"], ["Open Settings", "#settings/passwords"],
            ["Open Settings", "#settings/email"],
            ["Answer them", "#answers"], ["Find jobs", "find"], ["Go to Openings", "#openings"]]


def _lines(done: list[bool], pw: str, bank: str) -> list:
    return [[name, said.format(pw=pw, bank=bank), d, "#i-checkc" if d else "#i-ring", [] if d else [button]]
            for name, said, d, button in zip(_NAMES, _SAID, done, _BUTTONS)]


def _question(n: int, answer: str | None) -> dict:
    return {"id": f"q{n}", "ask": f"Made-up question {n}?", "kind": "choice", "choices": ["Yes", "No"],
            "answer": answer, "seen": 0}


def test_getting_started_ticks_each_line_by_its_rule_and_goes_for_good(srv, job_apply_home, monkeypatch):
    """C9's test 9. Getting started, at the top of Home: seven lines, each ticked, dimmed and without its button exactly
    when its rule says, and "n of 7 done" beside the title. Its numbers are the Your answers page's, and the job
    systems' passwords saved (the line is done with one of them; the inbox's password and the API key aren't counted).
    The setup window still opens by itself while the profile is incomplete. The card goes for good once lines 1, 2, 6
    and 7 are done (even if one of them comes undone after), and "Hide this" puts it away across a reload."""
    state = Desk.state
    over: dict = {}

    def drawn(self):
        s = state(self)
        out = {**s, **{k: v for k, v in over.items() if k != "profile"}}
        out["profile"] = {**s["profile"], **over.get("profile", {})}
        return out

    monkeypatch.setattr(Desk, "state", drawn)
    # One done: the profile lacks a phone, no job titles, only the inbox's password saved (no job system's), one
    # common question of three answered, never searched, no openings, no job begun, nothing applied for
    nothing = {"profile": {"missing": ["personal.phone"]}, "area": {"titles": [], "places": ["Phoenix, AZ"]},
               "passwords": {"workday": False, "icims": False, "email": True, "anthropic": False},
               "bank": [_question(1, "Yes"), _question(2, None), _question(3, "")],
               "search": {"status": "idle", "at": None}, "listings": [], "others": [], "applications": []}
    # Five done: the profile, what and where, a job system's password, every common question, and an opening found
    # (the desk's own, below; never searched here); not the inbox's password, and no job begun
    some = {"profile": {"missing": []}, "area": {"titles": ["Technician"], "places": ["Phoenix, AZ"]},
            "passwords": {"workday": True, "icims": False, "email": False, "anthropic": True},
            "bank": [_question(1, "Yes"), _question(2, "No")], "search": {"status": "idle", "at": None},
            "others": [], "applications": []}
    # Lines 1, 2, 6 and 7 done: searched, no openings left, one application
    four = {**some, "search": {"status": "done", "at": 1000.0}, "listings": [],
            "applications": [_app(1, "2026-11-02T16:00:00+00:00")]}

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            seen = {}
            async with person_browser() as browser:
                page = await browser.new_page()
                over.clear()
                over.update(nothing)
                await open_desk(page, desk, "home")
                await page.wait_for_selector("#setup[open]")  # (by itself, once a visit: the profile lacks a phone)
                await page.click("[data-act=setup-close]")
                await page.wait_for_selector("#start-lines > .item")
                seen["nothing"] = await page.evaluate(_START)
                seen["first"] = await page.evaluate("""() => [...document.querySelectorAll('[data-pages=home] .card')]
                    .map((c) => c.id || c.getAttribute('aria-labelledby')).slice(0, 2)""")
                await page.click("#start [data-act=dismiss][data-tip=checklist]")  # Hide this
                await page.wait_for_selector("#start", state="hidden")
                await page.reload()
                await page.wait_for_selector("#plugin-version:not(:empty)", state="attached")
                await page.wait_for_timeout(500)
                seen["hidden"] = await page.evaluate(_START)
                await browser.close()
            async with person_browser() as browser:
                page = await browser.new_page()
                over.clear()
                over.update(some)
                await open_desk(page, desk, "home")
                await page.wait_for_selector("#start-lines > .item")
                seen["some"] = await page.evaluate(_START)
                over.update(four)  # (the card goes at the next refresh)
                await page.wait_for_selector("#start", state="hidden")
                seen["four"] = await page.evaluate(_START)
                over.update({"applications": [], "search": {"status": "idle", "at": None}})  # (lines 6 and 7 undone)
                await page.reload()
                await page.wait_for_selector("#plugin-version:not(:empty)", state="attached")
                await page.wait_for_timeout(2000)  # (a refresh after the first draw)
                seen["for good"] = await page.evaluate(_START)
                await browser.close()
            return seen
        finally:
            await desk.stop()

    desk, _ = _stopped_desk(srv, {})
    desk.listings = [{"company": "Example Fab", "title": "Technician", "url": "https://example.com/1",
                      "location": "Phoenix, AZ"}]
    seen = run(go())
    assert seen["nothing"] == {"shown": True, "count": "1 of 7 done",
                               "lines": _lines([False, False, False, True, False, False, False], "0 of 2 saved",
                                               "1 of 3 answered")}, seen["nothing"]
    assert seen["first"] == ["start", "tile-needs"], seen["first"]
    assert seen["hidden"]["shown"] is False, seen["hidden"]
    assert seen["some"] == {"shown": True, "count": "5 of 7 done",
                            "lines": _lines([True, True, True, False, True, True, False], "1 of 2 saved",
                                            "2 of 2 answered")}, seen["some"]
    assert seen["four"]["shown"] is False, seen["four"]
    assert seen["for good"]["shown"] is False, seen["for good"]


# ------------------------------------------------------------------ C7 and "Changed in Version C": the page's words
_ASK_JOBS = ': ask Claude to "add the start and end months of my jobs from my resume to my profile"'
_ASK_BACKGROUND = ': ask Claude to "ask me the Job Desk\'s background questions"'


@pytest.mark.parametrize("standalone", [True, False])
def test_the_profile_notices_and_the_tailoring_banner_name_claude_only_inside_claude(srv, job_apply_home, monkeypatch,
                                                                                    standalone):
    """"Changed in Version C" on the page. The red notice for an unfinished profile says only what's missing, with
    Finish your profile (data-act setup), on either desk. Its three places that say what to ask Claude for (the months
    of the jobs, the background questions, and the U.S. service questions) say it inside Claude, and end before the
    colon without it. The yellow tailoring banner says "In your chat with Claude, say “tailor my resumes”." inside
    Claude, and is never shown without it."""
    from job_apply import config

    state = Desk.state
    monkeypatch.setattr(Desk, "state", lambda self: {**state(self), "tailoring": 2})  # (two jobs waiting for a resume)
    monkeypatch.setattr(config.Profile, "missing_required",
                        lambda self: ["work_history dates: Example Fab", "background: patents"])
    monkeypatch.setattr(config.Profile, "profile_gaps", lambda self: ["history: us_government_employee, us_military"])
    desk, _ = _stopped_desk(srv, {})
    desk.standalone = standalone

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk, "home")
                await page.wait_for_selector("#notices .notice.bad")
                if await page.is_visible("#setup"):
                    await page.click("[data-act=setup-close]")  # (it opens by itself: the profile lacks something)
                seen = {"problem": " ".join((await page.inner_text("#notices .notice.bad")).split()),
                        "button": await page.eval_on_selector_all(
                            "#notices .notice.bad button", "(bs) => bs.map((b) => [b.innerText, b.dataset.act])"),
                        "gaps": " ".join((await page.inner_text("#notices .notice.info > span")).split()),
                        "banners": [" ".join(t.split())
                                    for t in await page.locator("#notices .waiting").all_inner_texts()]}
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    jobs = "the start and end months of your jobs (Example Fab)"
    background = ("your answers to the yes/no questions most employers ask (government, military or Defense work, "
                  "relatives at the company, a non-compete, patents, another job or a board seat)")
    service = "whether you've ever worked for the U.S. federal government or served in the U.S. military"
    ask_jobs, ask_background = ("", "") if standalone else (_ASK_JOBS, _ASK_BACKGROUND)
    assert seen["problem"] == (f"Problem: your profile still needs: {jobs}{ask_jobs} and {background}{ask_background}. "
                               "Finish your profile"), seen
    assert seen["button"] == [["Finish your profile", "setup"]], seen
    assert seen["gaps"] == f"Fewer stops: your profile could also hold {service}{ask_background}.", seen
    tailoring = ("2 jobs waiting for a tailored resume. In your chat with Claude, say “tailor my resumes”. Each job "
                 "carries on as soon as its resume is ready.")
    assert seen["banners"] == ([] if standalone else [tailoring]), seen
