"""Snapshots of the Job Desk's cards (the design of Oct 18, T8): the page drawn from a canned state
(tests/fixtures/desk/state-<case>.json, made-up values only: the repo is public), and each card's words compared with
tests/fixtures/desk/cards-<case>.txt. A change to what a card says shows as a diff, card by card. When the change is
meant, write the snapshots again with JOB_APPLY_UPDATE_SNAPSHOTS=1 and look the diff over before committing it."""

import copy
import difflib
import json
import os
import re
import time
from pathlib import Path

import pytest
from conftest import browser_available, open_desk, person_browser, run

from job_apply.desk import Desk

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

DESK_FIXTURES = Path(__file__).parent / "fixtures" / "desk"
UPDATE = os.environ.get("JOB_APPLY_UPDATE_SNAPSHOTS") == "1"

# Each case: the canned state it draws, the snapshot its cards are compared with, what's kept in the browser before the
# page opens (its localStorage), and what's pressed before the cards are read. A card shows only on its own page of the
# side menu, so the cards are read on each page in turn (the menu's links pressed, as a person would), each card from
# its own page; the Openings card from Openings, or from Applications for the "applications" case ("list": its page),
# with the presses made there. Every <details> is opened first (What went in, the passwords' How this works), so
# what's in them is in the snapshot too.
CASES = {
    # a job stopped for each need: questions (with a What went in panel), a sign-in holding the queue, an emailed code,
    # a bot check, a CAPTCHA, stuck, a Submit that didn't go, the person's own Submit, one to check whether it went in,
    # a tailored resume, the first cover letter to check (its text drawn as text), one that may be begun elsewhere, one
    # begun before a restart
    "stops": {"state": "state-stops.json", "golden": "cards-stops.txt"},
    # a ready job with its What went in panel (and a cover letter written for it), one that failed, one sent, one
    # skipped, one being filled (its log's times masked) and two queued
    "ready": {"state": "state-ready.json", "golden": "cards-ready.txt"},
    # nothing under way (the one idle card): openings with pay listed (sure, and read from the text), pay not listed, no
    # date, a new one, one where the desk goes all the way, one also on Indeed, one not recommended, one Not interested;
    # the common questions (one answered) and a saved password
    "openings": {"state": "state-openings.json", "golden": "cards-openings.txt"},
    # the same openings' My applications (the Applications page): one due a follow-up, one at the interview stage
    "applications": {"state": "state-openings.json", "golden": "cards-applications.txt", "list": "applications"},
    # the bar's Stuck group picked: its hint offers the Notes; the queue paused with one waiting
    "notes": {"state": "state-notes.json", "golden": "cards-notes.txt", "storage": {"desk-stop-filter": "stuck"}},
}

# what changes from one run or computer to another: times of day, dates, and "5 min ago"
_MASKS = [(re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "YYYY-MM-DD"), (re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)"), "HH:MM"),
          (re.compile(r"\b(just now|\d+ (min|h|days) ago)\b"), "<ago>")]

_READ_CARDS = """() => [...document.querySelectorAll('section.card')].map((card) => [
  card.getAttribute('aria-labelledby'), card.getClientRects().length ? card.innerText : null])"""


def _fold(text: str | None) -> str:
    """A card's words, a line each, with the whitespace folded and what changes from run to run masked."""
    if text is None:
        return "(hidden)"
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split())
        for pattern, mask in _MASKS:
            line = pattern.sub(mask, line)
        if line:
            lines.append(line)
    return "\n".join(lines)


def _snapshot(cards: list[list]) -> str:
    return "".join(f"## {card}\n{_fold(text)}\n\n" for card, text in cards)


def _state(name: str) -> dict:
    return json.loads((DESK_FIXTURES / name).read_text(encoding="utf-8"))


def _cards(srv, monkeypatch, state: dict, storage: dict | None = None, press: list[str] = (),
           list_page: str = "openings") -> str:
    """The desk's page drawn from `state` (Desk.state replaced by it), each section.card's words as _snapshot says
    them, each read on its own page (the Openings card on `list_page`, where `press` is pressed)."""
    # A stopped job's row says how long it has waited (the design's C3: from its paused_at, by the page's clock). So
    # that it says the same every run, the page's clock is held at the time the cards are drawn (`now`), and each job
    # stopped is drawn as stopped 12 minutes before it, in the fixture's order (a second apart, as there)
    now = time.time()
    state = copy.deepcopy(state)
    stopped = [row["run"] for row in [*state.get("listings", []), *state.get("others", [])]
               if (row.get("run") or {}).get("paused_at")]
    latest = max((r["paused_at"] for r in stopped), default=0)
    for r in stopped:
        r["paused_at"] = now - 12 * 60 - (latest - r["paused_at"])
    monkeypatch.setattr(Desk, "state", lambda self: copy.deepcopy(state))
    desk = Desk(srv)
    desk.applier.start = lambda: None  # (nothing is worked: the page shows only the canned state)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await page.clock.set_fixed_time(now)
                # The Alerts on this computer card's button says what the browser allows ("Alert me", "Alerts
                # blocked"), and the tests' browsers differ in that (CI's headless shell blocks notifications, a
                # full Chromium hasn't been asked): the cards are read as on a browser not asked yet
                await page.add_init_script("""(() => { try { Object.defineProperty(Notification, "permission",
                    {get: () => "default"}); } catch (e) {} })()""")
                if storage:
                    await page.add_init_script(f"""(() => {{ try {{ for (const [k, v] of Object.entries({json.dumps(storage)}))
                        localStorage.setItem(k, v); }} catch (e) {{}} }})()""")  # noqa: E501
                await open_desk(page, desk)
                await page.wait_for_selector("#plugin-version:not(:empty)")  # the state is drawn
                cards: dict[str, str | None] = {}
                for at in ("home", list_page, "answers", "working", "settings", "help"):
                    if at != "home":
                        await page.click(f".nav a[href='#{at}']")
                        await page.wait_for_function("(at) => document.body.dataset.page === at", arg=at)
                    if at == list_page:
                        for selector in press:
                            await page.click(selector)
                    await page.evaluate("() => document.querySelectorAll('details').forEach((d) => { d.open = true; })")
                    # (and every job's card opened, as a person would, by its chevron: four or more in a list start
                    # closed, the design's C3)
                    await page.evaluate("""() => document.querySelectorAll('[data-open][aria-expanded=false]')
                        .forEach((b) => b.click())""")
                    await page.wait_for_timeout(100)
                    for card, text in await page.evaluate(_READ_CARDS):  # (in the page's order; null when not shown)
                        if cards.get(card) is None:
                            cards[card] = text
                await browser.close()
                return [[card, text] for card, text in cards.items()]
        finally:
            await desk.stop()

    return _snapshot(run(go()))


def _by_card(snapshot: str) -> dict[str, list[str]]:
    """A snapshot's lines by card (its heading's id)."""
    cards: dict[str, list[str]] = {}
    lines: list[str] = []
    for line in snapshot.splitlines(keepends=True):
        if line.startswith("## "):
            lines = cards.setdefault(line[3:].strip(), [])
        else:
            lines.append(line)
    return cards


def _compare(name: str, seen: str) -> None:
    """`seen` against the snapshot `name` (cards-<case>.txt): equal, or an AssertionError with the diff of each card
    that changed (rewritten instead with UPDATE)."""
    golden = DESK_FIXTURES / name
    if UPDATE:
        golden.write_text(seen, encoding="utf-8", newline="\n")
        return
    want = golden.read_text(encoding="utf-8") if golden.exists() else ""
    if seen == want:
        return
    where = golden.relative_to(Path(__file__).parent.parent).as_posix()
    was, now = _by_card(want), _by_card(seen)
    diff = "".join("".join(difflib.unified_diff(was.get(card, []), now.get(card, []), fromfile=f"{where}, card {card}",
                                                tofile=f"the page now, card {card}", n=2))
                   for card in dict.fromkeys([*was, *now]))
    raise AssertionError(f"The desk's cards in {name} changed (if that's meant, run with "
                         f"JOB_APPLY_UPDATE_SNAPSHOTS=1 and look the diff over):\n{diff or 'only in their order'}")


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_desks_cards_say_what_they_said(srv, job_apply_home, monkeypatch, case):
    how = CASES[case]
    seen = _cards(srv, monkeypatch, _state(how["state"]), how.get("storage"), how.get("press", []),
                  how.get("list", "openings"))
    # (every card: Home's four, the Openings card, the Applications card (Version B's B6), Your answers' and What's
    # working's, Settings' seven (Version B's Searching and applying among them), Help's two)
    assert seen.count("\n## ") + 1 == 17, seen
    _compare(how["golden"], seen)


def test_a_changed_card_fails_with_a_diff_naming_the_line(srv, job_apply_home, monkeypatch):
    """The snapshot check itself: a card whose words changed fails, and its message is the diff of that card's lines
    (the old one and the new one, under the card's name), not the two whole pages."""
    if UPDATE:
        pytest.skip("the snapshots are being written")
    state = _state("state-stops.json")
    row = next(r for r in state["listings"] if r["company"] == "Example Check")
    row["run"]["reason"] = "Submit was pressed and a made-up change showed."
    seen = _cards(srv, monkeypatch, state)
    with pytest.raises(AssertionError) as said:
        _compare("cards-stops.txt", seen)
    message = str(said.value)
    assert "--- tests/fixtures/desk/cards-stops.txt, card done-h\n" in message, message
    assert "+++ the page now, card done-h\n" in message and "card list-h" not in message, message
    assert "\n-Submit was pressed and no confirmation showed.\n" in message, message
    assert "\n+Submit was pressed and a made-up change showed.\n" in message, message
    changed = [line for line in message.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert len(changed) == 2, message  # (only the line that changed)
