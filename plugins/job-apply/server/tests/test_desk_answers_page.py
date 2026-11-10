"""The Your answers page's Answers the desk remembers, the page's half (the design's D5, D7's test 16): each answer
given on a job's card with "remember" ticked, asked of the desk when the page opens and after each change; an ID shown
as dots until Show; Change and Forget, with Forget asking first in D5's words; the empty line in D5's words. A real desk
with a real answers.yaml, the file read after each change. Made-up people, employers and IDs only: the repo is
public."""

import asyncio

import pytest
import yaml
from conftest import browser_available, open_desk, person_browser, press_menu, run
from test_desk_answers import ENTRIES, _id, _kept, _write
from test_desk_buttons import _idle_desk

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

FORGET_ASKS = "Forget this answer? The desk asks you again the next time a site asks it."
NONE_SAID = 'Nothing remembered yet. An answer you give on a job\'s card is kept here when "remember" is ticked.'
DOTS = "\u2022" * 8  # (an ID not shown: dots, "Hidden" to a screen reader)
IDS = ("WW123456", "EX-778899")  # (the made-up IDs of ENTRIES: never in the page until Show)

# Each row of the card as it reads: its question, its lines under it, and its buttons
_ROWS = """() => [...document.querySelectorAll('#remembered > .setrow')].map((r) => ({
  question: r.querySelector('.t').textContent,
  said: [...r.querySelectorAll(':scope > div:first-child > p:not(.t)')].map((p) => p.innerText.trim()),
  box: (r.querySelector('input[type=text]') || {}).value ?? null,
  buttons: [...r.querySelectorAll('button')].map((b) => b.textContent)}))"""
_PAGE = "() => document.documentElement.outerHTML"
_FOCUSED = ("() => { const f = document.activeElement; "
            "return f && (f.dataset.ansBox !== undefined ? 'box' : f.textContent); }")


async def _loaded(page) -> None:
    """The card drawn from the desk's list (asked when the page opened, or again after a change)."""
    await page.wait_for_selector("#remembered-card[data-loaded]", state="attached")


def _row(rows: list, question: str, at: str = "") -> int:
    return next(i for i, r in enumerate(rows) if r["question"] == question and (not at or f"Only at {at}" in r["said"]))


def test_remembered_answers_show_ids_as_dots_until_show_change_and_forget_them(srv, job_apply_home):
    """D7's test 16: a private answer (an ID) shows dots until Show, and its value isn't in the page until then, nor
    after the page is left; Change turns the answer into a box with Save and Cancel, whose words and focus last through
    the page's refreshes, and Save writes that entry alone; an empty answer is refused in the desk's words; Forget asks
    first in D5's words (No keeps it) and takes out that entry alone."""
    _write(job_apply_home)
    before = _kept(job_apply_home)
    desk = _idle_desk(srv)
    asked: list[str] = []
    answer = {"forget": False}

    async def on_dialog(d):
        asked.append(d.message)
        await (d.accept() if answer["forget"] else d.dismiss())

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("dialog", lambda d: asyncio.ensure_future(on_dialog(d)))
                seen: dict = {}
                await open_desk(page, desk, "answers")
                await _loaded(page)
                seen["rows"] = await page.evaluate(_ROWS)
                seen["page"] = await page.evaluate(_PAGE)
                # Show: the Example Fab ID's value, and only it
                fab = _row(seen["rows"], "Employee ID", "Example Fab")
                await page.locator("#remembered > .setrow").nth(fab).locator("button", has_text="Show").click()
                # (the value asked of the desk on the press: drawn once it comes)
                await page.locator("#remembered > .setrow").nth(fab).locator("button", has_text="Hide").wait_for()
                seen["shown"] = await page.evaluate(_ROWS)
                seen["shown_page"] = await page.evaluate(_PAGE)
                # Change on the weekends answer: a box with its words, kept through a refresh (1.5 seconds)
                weekends = _row(seen["rows"], "Are you willing to work weekends?")
                row = page.locator("#remembered > .setrow").nth(weekends)
                await row.locator("button", has_text="Change").click()
                seen["changing"] = (await page.evaluate(_ROWS))[weekends]
                box = row.locator("input[type=text]")
                await box.fill("Yes, any weekend")
                await page.wait_for_timeout(2000)
                seen["kept"] = ((await page.evaluate(_ROWS))[weekends]["box"], await page.evaluate(_FOCUSED))
                await page.keyboard.press("Enter")  # (Save, from the box)
                await page.wait_for_function("() => !document.querySelector('#remembered input')")
                seen["saved"] = (_kept(job_apply_home), (await page.evaluate(_ROWS))[weekends],
                                 await page.evaluate(_FOCUSED))
                # an empty answer: refused in the desk's words, the box left for another go, the file as it was
                await row.locator("button", has_text="Change").click()
                await row.locator("input[type=text]").fill("   ")
                await row.locator("button", has_text="Save").click()
                await row.locator(".set-err").wait_for()
                seen["refused"] = (await row.locator(".set-err").inner_text(), _kept(job_apply_home),
                                   (await page.evaluate(_ROWS))[weekends]["box"])
                await row.locator("button", has_text="Cancel").click()
                seen["cancelled"] = ((await page.evaluate(_ROWS))[weekends], await page.evaluate(_FOCUSED))
                # Forget on the Example Litho ID: asked first; No keeps it, Yes takes that entry out
                litho = _row(seen["rows"], "Employee ID", "Example Litho")
                await page.locator("#remembered > .setrow").nth(litho).locator("button", has_text="Forget").click()
                await page.wait_for_timeout(300)
                seen["kept_on_no"] = (_kept(job_apply_home), len(await page.evaluate(_ROWS)))
                answer["forget"] = True
                await page.locator("#remembered > .setrow").nth(litho).locator("button", has_text="Forget").click()
                await page.wait_for_function("() => document.querySelectorAll('#remembered > .setrow').length === 3")
                seen["forgotten"] = (_kept(job_apply_home), await page.evaluate(_ROWS))
                # the page left and opened again: the ID shown is dots again, its value out of the page
                await press_menu(page, "home")
                seen["left_page"] = await page.evaluate(_PAGE)
                await press_menu(page, "answers")
                await _loaded(page)
                seen["back"] = (await page.evaluate(_ROWS), await page.evaluate(_PAGE))
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    rows = seen["rows"]
    # (newest first, the common question's answer left out: it has its own card above)
    assert [(r["question"], r["said"]) for r in rows] == [
        ("Employee ID", ["Only at Example Fab", DOTS]),
        ("Are you willing to work weekends?", ["Yes, every other weekend"]),
        ("Middle name", ["Only at Example Litho", "Left empty"]),
        ("Employee ID", ["Only at Example Litho", DOTS]),
    ], rows
    assert [r["buttons"] for r in rows] == [["Show", "Change", "Forget"], ["Change", "Forget"], ["Change", "Forget"],
                                            ["Show", "Change", "Forget"]], rows
    assert "•" * 8 in seen["page"]
    assert not [i for i in IDS if i in seen["page"]], "an ID is in the page before Show"
    assert not [r for r in rows if "Some college" in str(r)]  # (the common question's answer isn't in this card)
    shown = seen["shown"]
    assert shown[0]["said"] == ["Only at Example Fab", "WW123456"] and shown[0]["buttons"][0] == "Hide", shown
    assert shown[3]["said"] == ["Only at Example Litho", DOTS], shown
    assert "EX-778899" not in seen["shown_page"]
    # Change: the answer as a box with Save and Cancel
    assert seen["changing"]["box"] == "Yes, every other weekend" and seen["changing"]["buttons"] == ["Save", "Cancel"]
    assert seen["kept"] == ("Yes, any weekend", "box"), seen["kept"]
    kept, row, focused = seen["saved"]
    assert kept == [before[0], {**before[1], "answer": "Yes, any weekend"}, *before[2:]], kept
    assert row["said"] == ["Yes, any weekend"] and row["buttons"] == ["Change", "Forget"] and focused == "Change"
    said, kept, box = seen["refused"]
    assert said == "Type an answer, or press Forget." and box == "   ", seen["refused"]
    assert kept[1]["answer"] == "Yes, any weekend" and len(kept) == len(before)
    assert seen["cancelled"][0]["said"] == ["Yes, any weekend"] and seen["cancelled"][1] == "Change", seen["cancelled"]
    # Forget: asked first in D5's words, twice (No, then Yes); that entry alone taken out
    assert asked == [FORGET_ASKS, FORGET_ASKS], asked
    assert seen["kept_on_no"] == (kept, 4), seen["kept_on_no"]
    kept, rows = seen["forgotten"]
    assert kept == [before[0], {**before[1], "answer": "Yes, any weekend"}, before[2], before[3]], kept
    assert [(r["question"], r["said"][0]) for r in rows] == [("Employee ID", "Only at Example Fab"),
                                                            ("Are you willing to work weekends?", "Yes, any weekend"),
                                                            ("Middle name", "Only at Example Litho")], rows
    # left: dots again
    assert "WW123456" not in seen["left_page"]
    rows, page = seen["back"]
    assert rows[0]["said"] == ["Only at Example Fab", DOTS] and "WW123456" not in page, rows
    assert yaml.safe_load((job_apply_home / "answers.yaml").read_text(encoding="utf-8"))["answers"] == kept


def test_change_on_an_id_not_shown_starts_empty_and_keeps_its_value_out_of_the_page(srv, job_apply_home):
    """An ID's Change, before Show: the box starts empty (its value is never put in the page for it); Cancel leaves
    the dots, and a new value saved is written to that entry alone and shown as dots again."""
    _write(job_apply_home)
    before = _kept(job_apply_home)
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                await open_desk(page, desk, "answers")
                await _loaded(page)
                row = page.locator("#remembered > .setrow").nth(0)
                await row.locator("button", has_text="Change").click()
                seen = {"box": await row.locator("input[type=text]").input_value(), "page": await page.evaluate(_PAGE)}
                await row.locator("input[type=text]").fill("WW000111")
                await row.locator("button", has_text="Save").click()
                await page.wait_for_function("() => !document.querySelector('#remembered input')")
                await _loaded(page)
                seen["after"] = ((await page.evaluate(_ROWS))[0], _kept(job_apply_home))
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["box"] == "" and "WW123456" not in seen["page"], seen["box"]
    row, kept = seen["after"]
    assert row["said"] == ["Only at Example Fab", DOTS], row
    assert kept == [{**before[0], "answer": "WW000111"}, *before[1:]], kept


# Everything the page holds that could carry an ID's value: its HTML, its text, every box's value, and the card's own
# variables (the list from the desk, the values shown, a change under way, the errors), as one string
_HELD = """() => [document.documentElement.outerHTML, document.body.innerText,
  [...document.querySelectorAll('input, textarea')].map((i) => i.value).join('|'),
  JSON.stringify([remembered, [...rememberedShown], rememberedEdit, [...rememberedErr]])].join('\\n')"""


def test_an_ids_value_comes_only_on_show_and_is_let_go_on_hide_on_leaving_and_on_a_new_list(srv, job_apply_home):
    """An ID's value isn't in the list the desk sends (D5, the second look): before Show it is nowhere in the page,
    not in its HTML, its text, a box or the card's variables. Show asks the desk for it (one /api/answers/show with
    the row's id) and draws it, with Hide; Hide, leaving the page, and the list asked again (after a change) each drop
    it from the variables too. A Show the desk refuses keeps the dots and says why under the row."""
    _write(job_apply_home)
    desk = _idle_desk(srv)
    shows: list = []

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("request", lambda r: shows.append(r.post_data_json) if r.url.endswith("/api/answers/show")
                        else None)
                fab = page.locator("#remembered > .setrow").nth(0)

                async def show() -> None:
                    await fab.locator("button", has_text="Show").click()
                    await fab.locator("button", has_text="Hide").wait_for()

                seen: dict = {}
                await open_desk(page, desk, "answers")
                await _loaded(page)
                seen["before"] = await page.evaluate(_HELD)
                await show()
                seen["shown"] = ((await page.evaluate(_ROWS))[0], len(shows), await page.evaluate(_HELD))
                await fab.locator("button", has_text="Hide").click()
                await fab.locator("button", has_text="Show").wait_for()
                seen["hidden"] = ((await page.evaluate(_ROWS))[0], await page.evaluate(_HELD))
                # shown, then Change: the box holds the value
                await show()
                await fab.locator("button", has_text="Change").click()
                seen["change"] = await fab.locator("input[type=text]").input_value()
                await fab.locator("button", has_text="Cancel").click()
                # shown, then the page left
                await press_menu(page, "home")
                seen["left"] = await page.evaluate(_HELD)
                await press_menu(page, "answers")
                await _loaded(page)
                # shown, then the list asked again (another row's answer changed)
                await show()
                weekends = page.locator("#remembered > .setrow").nth(1)
                await weekends.locator("button", has_text="Change").click()
                await weekends.locator("input[type=text]").fill("Yes, any weekend")
                await weekends.locator("button", has_text="Save").click()
                await page.wait_for_function("() => !document.querySelector('#remembered input')")
                await fab.locator("button", has_text="Show").wait_for()
                seen["new list"] = await page.evaluate(_HELD)
                # a Show refused: the Example Litho ID forgotten behind the page's back
                _write(job_apply_home, [e for e in ENTRIES if e.get("at") != "Example Litho"
                                        or e["question"] != "Employee ID"])
                litho = page.locator("#remembered > .setrow").nth(3)
                await litho.locator("button", has_text="Show").click()
                await litho.locator(".set-err").wait_for()
                seen["refused"] = ((await page.evaluate(_ROWS))[3], await litho.locator(".set-err").inner_text())
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert not [i for i in IDS if i in seen["before"]], "an ID's value is in the page before Show"
    row, asked, held = seen["shown"]
    assert row["said"] == ["Only at Example Fab", "WW123456"] and row["buttons"][0] == "Hide", row
    assert asked == 1 and shows[0] == {"id": _id(ENTRIES[0])}, shows
    assert "WW123456" in held and "EX-778899" not in held
    row, held = seen["hidden"]
    assert row["said"] == ["Only at Example Fab", DOTS] and "WW123456" not in held, row
    assert seen["change"] == "WW123456"
    assert "WW123456" not in seen["left"], "kept after the page was left"
    assert "WW123456" not in seen["new list"], "kept after the list was asked again"
    assert len(shows) == 4, shows
    row, said = seen["refused"]
    assert row["said"] == ["Only at Example Litho", DOTS] and row["buttons"][0] == "Show", row
    assert said == "That answer isn't remembered any more.", said


def test_with_nothing_remembered_the_card_says_so_in_d5s_words(srv, job_apply_home):
    """D7's test 16, its last part: with nothing remembered (no answers.yaml, or only a common question's answer),
    the card holds the line D5 gives, and no row; one remembered, the line goes."""
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                seen = []
                for entries in ([], [ENTRIES[3]], ENTRIES[1:2]):
                    if entries:
                        _write(job_apply_home, entries)
                    await page.goto("about:blank")
                    await open_desk(page, desk, "answers")
                    await _loaded(page)
                    seen.append((await page.evaluate("""() => { const p = document.getElementById('remembered-none');
                        return p.getClientRects().length ? p.textContent : null; }"""),
                                 len(await page.evaluate(_ROWS)),
                                 await page.is_visible("#remembered-h")))
                await browser.close()
                return seen
        finally:
            await desk.stop()

    assert run(go()) == [(NONE_SAID, 0, True), (NONE_SAID, 0, True), (None, 1, True)]


def test_the_remembered_answers_are_asked_when_the_page_opens_and_never_from_the_state(srv, job_apply_home):
    """The card is filled by /api/answers, asked as Your answers opens (again each time it's opened), not by the
    state the page refreshes: an answer added to the file while the page is open shows only once the page is opened
    again."""
    _write(job_apply_home, ENTRIES[1:2])
    desk = _idle_desk(srv)
    asked: list[str] = []

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("request", lambda r: asked.append(r.url) if "/api/answers" in r.url else None)
                await open_desk(page, desk, "home")
                await page.wait_for_timeout(300)
                seen = [len(asked)]
                await press_menu(page, "answers")
                await _loaded(page)
                seen.append(len(await page.evaluate(_ROWS)))
                _write(job_apply_home, ENTRIES[:2])
                await page.wait_for_timeout(2000)  # (a refresh or more)
                seen.append(len(await page.evaluate(_ROWS)))
                await press_menu(page, "home")
                await press_menu(page, "answers")
                await page.wait_for_function("() => document.querySelectorAll('#remembered > .setrow').length === 2")
                seen.append(len(asked))
                await browser.close()
                return seen
        finally:
            await desk.stop()

    assert run(go()) == [0, 1, 1, 2]
    assert all(_id(ENTRIES[1]) not in url for url in asked)
