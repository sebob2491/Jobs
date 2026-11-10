"""The Openings page's search box, tabs and filter drop-downs (the design's Version B: B1, B2, B3; its B8 tests 1 to
5). Each test opens the real page in a browser. Made-up employers only: the repo is public."""

import pytest
from conftest import browser_available, open_desk, person_browser, run
from test_desk import _fit
from test_desk_buttons import _idle_desk

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

_TITLES = "() => [...document.querySelectorAll('#rows li.row a.title')].map((a) => a.innerText)"
# Each drop-down's summary (its words, the More filters number apart), and whether it's marked as set
_SAID = """() => Object.fromEntries([...document.querySelectorAll('#filters details.f')].map((d) => {
  const words = d.querySelector('summary .f-said');
  const count = d.querySelector('summary .count');
  return [d.dataset.f, [words.innerText.trim(), count && count.getClientRects().length ? count.innerText : null,
                        d.classList.contains('on')]];
}))"""


def _opening(i: int, company: str, title: str, score: int = 80, **more) -> dict:
    return {"company": company, "title": title, "url": f"https://careers.example.com/{i}", "location": "Phoenix, AZ",
            "fit": _fit(score), **more}


async def _until_titles(page, titles: list[str]) -> None:
    try:
        await page.wait_for_function(f"want => JSON.stringify(({_TITLES})()) === JSON.stringify(want)", arg=titles,
                                     timeout=10000)
    except Exception:
        assert await page.evaluate(_TITLES) == titles  # (says which differ)
        raise


def test_the_search_box_hides_rows_by_title_and_company_and_is_empty_after_a_reload(srv, job_apply_home):
    """B8's test 1. The top bar's search box, on Openings only, hides every row whose title and company together don't
    hold every word typed, whatever the case; a reload empties it."""
    desk = _idle_desk(srv)
    desk.listings = [_opening(1, "Example Fab", "Field Service Engineer"),
                     _opening(2, "Example Litho", "Process Technician"),
                     _opening(3, "Example Etch", "Equipment Technician")]
    every = ["Field Service Engineer", "Process Technician", "Equipment Technician"]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()
                await open_desk(page, desk, "openings")
                await _until_titles(page, every)
                seen = {"placeholder": await page.get_attribute("#q", "placeholder"),
                        "type": await page.get_attribute("#q", "type"), "shown": await page.is_visible("#q")}
                said = {}
                for typed in ("technician", "LITHO", "etch technician", "technician fab", "example", "  field   fab "):
                    await page.fill("#q", typed)
                    await page.wait_for_timeout(50)
                    said[typed] = await page.evaluate(_TITLES)
                await page.wait_for_timeout(1700)  # (a refresh: what's typed still holds)
                said["after a refresh"] = await page.evaluate(_TITLES)
                await page.reload()
                await page.wait_for_selector("#plugin-version:not(:empty)", state="attached")
                await _until_titles(page, every)
                seen["after a reload"] = await page.input_value("#q")
                for at in ("home", "applications", "settings"):
                    await page.click(f".nav a[href='#{at}']")
                    await page.wait_for_function("(at) => document.body.dataset.page === at", arg=at)
                    seen[at] = await page.is_visible("#q")
                await browser.close()
                return seen, said
        finally:
            await desk.stop()

    seen, said = run(go())
    assert seen == {"placeholder": "Search a title or company", "type": "search", "shown": True,
                    "after a reload": "", "home": False, "applications": False, "settings": False}, seen
    assert said == {"technician": ["Process Technician", "Equipment Technician"],
                    "LITHO": ["Process Technician"],
                    "etch technician": ["Equipment Technician"],
                    "technician fab": [],  # (every word: none has both)
                    "example": every,
                    "  field   fab ": ["Field Service Engineer"],
                    "after a refresh": ["Field Service Engineer"]}, said


def test_each_drop_downs_summary_says_its_default_then_what_is_set(srv, job_apply_home):
    """B8's test 2. Each of the six drop-downs' summaries says its default, and once something is chosen in it, what
    is chosen, with the drop-down marked as set (on)."""
    desk = _idle_desk(srv)
    desk.listings = [_opening(1, "Example Fab", "Field Service Engineer", ats="workday"),
                     _opening(2, "Example Litho", "Process Technician", ats="greenhouse")]

    async def choose(page, f: str, selector: str, settings: bool = True) -> None:
        await page.click(f"#filters details[data-f={f}] > summary")
        if settings:
            async with page.expect_response("**/api/settings"):
                await page.click(selector)
        else:
            await page.click(selector)
        await page.keyboard.press("Escape")

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                await open_desk(page, desk, "openings")
                await page.wait_for_selector("#rows a.title")
                seen = {"default": await page.evaluate(_SAID)}
                await choose(page, "sort", "[data-sort=pay]", settings=False)
                seen["best paid"] = (await page.evaluate(_SAID))["sort"]
                await choose(page, "sort", "[data-sort=newest]", settings=False)
                seen["newest"] = (await page.evaluate(_SAID))["sort"]
                await page.click("#filters details[data-f=pay] > summary")
                async with page.expect_response("**/api/settings"):
                    await page.fill("#min-pay", "50000")
                    await page.press("#min-pay", "Enter")
                await page.keyboard.press("Escape")
                await choose(page, "posted", "[data-posted='7']")
                await choose(page, "within", "[data-within='25']")
                await page.click("#filters details[data-f=workplace] > summary")
                for value in ("remote", "hybrid"):
                    async with page.expect_response("**/api/settings"):
                        await page.click(f"[data-chip=workplace][data-value={value}]")
                await page.keyboard.press("Escape")
                await choose(page, "more", "[data-chip=job_types][data-value=full_time]")
                await page.wait_for_timeout(1700)  # (a refresh: each says what the desk keeps)
                seen["set"] = await page.evaluate(_SAID)
                await choose(page, "sort", "[data-sort=fit]", settings=False)
                seen["best fit again"] = (await page.evaluate(_SAID))["sort"]
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["default"] == {"sort": ["Best fit", None, False], "pay": ["Pay: any", None, False],
                               "posted": ["Posted: any time", None, False],
                               "within": ["Within: any distance", None, False], "workplace": ["Workplace", None, False],
                               "more": ["More filters", None, False]}, seen["default"]
    assert seen["best paid"] == ["Best paid", None, True] and seen["newest"] == ["Newest", None, True], seen
    assert seen["set"] == {"sort": ["Newest", None, True], "pay": ["Pay: $50,000+", None, True],
                           "posted": ["Posted: last 7 days", None, True], "within": ["Within 25 miles", None, True],
                           "workplace": ["Workplace: remote, hybrid", None, True],
                           "more": ["More filters", "1", True]}, seen["set"]
    assert seen["best fit again"] == ["Best fit", None, False], seen


def test_the_more_filters_number_counts_what_is_set_inside_it(srv, job_apply_home):
    """B8's test 3. More filters' number counts one for a Status other than Any, and one for each pressed button or
    chosen company in its Type, Requirements, Job system and Company groups; at none it's hidden."""
    from job_apply.pipeline import Run

    desk = _idle_desk(srv)
    desk.listings = [_opening(1, "Example Fab", "Field Service Engineer", ats="workday"),
                     _opening(2, "Example Litho", "Process Technician", ats="greenhouse"),
                     _opening(3, "Example Etch", "Equipment Technician", ats="workday")]
    job = srv.add_job(url="https://careers.example.com/3", title="Equipment Technician", company="Example Etch")["job"]
    desk.applier.runs[job["id"]] = Run(job["id"], "Equipment Technician", "Example Etch", status="needs_you",
                                       need="stuck", reason="Waiting.")

    async def count(page) -> str | None:
        await page.wait_for_timeout(50)
        return (await page.evaluate(_SAID))["more"][1]

    async def press(page, selector: str) -> None:
        async with page.expect_response("**/api/settings"):
            await page.click(selector)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                await open_desk(page, desk, "openings")
                await page.wait_for_selector("#rows a.title")
                await page.click("#more > summary")
                heads = await page.locator("#more .f-panel h3").all_inner_texts()
                seen = [await count(page)]
                await page.click("[data-stop=stuck]")
                seen.append(await count(page))
                await press(page, "[data-chip=job_types][data-value=full_time]")
                seen.append(await count(page))
                await press(page, "[data-chip=requirements][data-value=day_shift]")
                await press(page, "[data-chip=requirements][data-value=not_senior]")
                seen.append(await count(page))
                await press(page, "[data-chip=systems][data-value=workday]")
                seen.append(await count(page))
                async with page.expect_response("**/api/settings"):
                    await page.fill("#company-add", "Example Fab")
                    await page.press("#company-add", "Enter")
                seen.append(await count(page))
                await page.wait_for_timeout(1700)  # (a refresh: the same)
                seen.append(await count(page))
                await page.click("[data-stop=any]")
                seen.append(await count(page))
                await press(page, "[data-chip=requirements][data-value=day_shift]")
                seen.append(await count(page))
                await browser.close()
                return heads, seen
        finally:
            await desk.stop()

    heads, seen = run(go())
    assert heads == ["Status", "Type", "Requirements", "Job system", "Company"], heads
    assert seen == [None, "1", "2", "4", "5", "6", "6", "5", "4"], seen


def test_a_drop_down_closes_on_a_click_outside_and_on_escape_and_stays_open_through_refreshes(srv, job_apply_home):
    """B8's test 4. A drop-down opens on its summary and stays open through two refreshes (its panel isn't redrawn);
    a click outside closes it; Escape closes it and puts the focus back on its summary; a choice in Sort, Posted or
    Within closes it, while Workplace stays open for another press."""
    desk = _idle_desk(srv)
    desk.listings = [_opening(1, "Example Fab", "Field Service Engineer")]
    opened = "() => [...document.querySelectorAll('details.f[open]')].map((d) => d.dataset.f || d.id)"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                await open_desk(page, desk, "openings")
                await page.wait_for_selector("#rows a.title")
                seen = {}
                await page.click("#filters details[data-f=workplace] > summary")
                seen["opened"] = await page.evaluate(opened)
                await page.wait_for_timeout(3200)  # (two refreshes)
                seen["after two refreshes"] = await page.evaluate(opened)
                seen["visible"] = await page.is_visible("[data-chip=workplace][data-value=remote]")
                async with page.expect_response("**/api/settings"):
                    await page.click("[data-chip=workplace][data-value=remote]")
                seen["after a workplace press"] = await page.evaluate(opened)
                await page.mouse.click(700, 860)  # (outside it: the page below the list)
                seen["after a click outside"] = await page.evaluate(opened)
                await page.click("#filters details[data-f=pay] > summary")
                await page.focus("#min-pay")
                await page.keyboard.press("Escape")
                seen["after escape"] = await page.evaluate(opened)
                seen["focus"] = await page.evaluate(
                    "() => document.activeElement.tagName + ' ' + document.activeElement.parentElement.dataset.f")
                await page.click("#filters details[data-f=pay] > summary")
                await page.click("#filters details[data-f=sort] > summary")  # (another opened: the first closes)
                seen["another"] = await page.evaluate(opened)
                await page.click("[data-sort=newest]")
                seen["after a sort"] = await page.evaluate(opened)
                await page.click("#filters details[data-f=posted] > summary")
                async with page.expect_response("**/api/settings"):
                    await page.click("[data-posted='14']")
                seen["after posted"] = await page.evaluate(opened)
                await page.click("#filters details[data-f=within] > summary")
                async with page.expect_response("**/api/settings"):
                    await page.click("[data-within='50']")
                seen["after within"] = await page.evaluate(opened)
                await page.click("#more > summary")
                await page.click("#more > summary")  # (its summary again: closed)
                seen["summary again"] = await page.evaluate(opened)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen == {"opened": ["workplace"], "after two refreshes": ["workplace"], "visible": True,
                    "after a workplace press": ["workplace"], "after a click outside": [], "after escape": [],
                    "focus": "SUMMARY pay", "another": ["sort"], "after a sort": [], "after posted": [],
                    "after within": [], "summary again": []}, seen


def test_the_not_interested_tab_lists_only_those_with_undo_and_is_hidden_at_zero(srv, job_apply_home):
    """B8's test 5. The Not interested tab lists every opening marked Not interested and nothing else, each with its
    Undo; Recommended and All leave them out; each tab's number counts its rows; the tab is hidden while there are
    none."""
    desk = _idle_desk(srv)
    desk.listings = [_opening(1, "Example Fab", "Field Service Engineer"),
                     _opening(2, "Example Litho", "Process Technician"),
                     _opening(3, "Example Etch", "Equipment Technician"),
                     _opening(4, "Example Low", "Accountant", score=30)]
    tabs = """() => [...document.querySelectorAll('[data-filter]')].map((b) => [b.dataset.filter,
      b.getClientRects().length ? b.innerText.replace(/\\s+/g, ' ').trim() : null])"""

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                await open_desk(page, desk, "openings")
                await _until_titles(page, ["Field Service Engineer", "Process Technician", "Equipment Technician"])
                seen = {"none": await page.evaluate(tabs)}
                desk.not_interested("https://careers.example.com/2", "pay")
                desk.not_interested("https://careers.example.com/4", "title")
                await _until_titles(page, ["Field Service Engineer", "Equipment Technician"])
                await page.wait_for_selector("[data-filter=unwanted]:visible")
                seen["two"] = await page.evaluate(tabs)
                await page.click("[data-filter=all]")
                await _until_titles(page, ["Field Service Engineer", "Equipment Technician"])
                await page.click("[data-filter=unwanted]")
                await _until_titles(page, ["Process Technician", "Accountant"])
                seen["hash"] = await page.evaluate("location.hash")
                seen["undo"] = await page.locator("#rows li.row [data-ni-undo]").all_inner_texts()
                seen["ticks"] = await page.locator("#rows li.row [data-pick]:enabled").count()
                async with page.expect_response("**/api/not_interested/undo"):
                    await page.locator("#rows li.row [data-ni-undo]").first.click()
                await _until_titles(page, ["Accountant"])
                seen["one left"] = await page.evaluate(tabs)
                async with page.expect_response("**/api/not_interested/undo"):
                    await page.locator("#rows li.row [data-ni-undo]").first.click()
                await page.wait_for_selector("[data-filter=unwanted]", state="hidden")
                seen["after undo"] = await page.evaluate(tabs)
                await page.wait_for_selector("#rows a.title")
                seen["rows after"] = await page.evaluate(_TITLES)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen["none"] == [["recommended", "Recommended 3"], ["all", "All 4"], ["unwanted", None]], seen
    assert seen["two"] == [["recommended", "Recommended 2"], ["all", "All 2"], ["unwanted", "Not interested 2"]], seen
    assert seen["hash"] == "#openings" and seen["undo"] == ["Undo", "Undo"] and seen["ticks"] == 0, seen
    assert seen["one left"] == [["recommended", "Recommended 3"], ["all", "All 3"],
                                ["unwanted", "Not interested 1"]], seen
    assert seen["after undo"] == [["recommended", "Recommended 3"], ["all", "All 4"], ["unwanted", None]], seen
    assert seen["rows after"] == ["Field Service Engineer", "Process Technician", "Equipment Technician"], seen


_PICK_ALL = "() => { const b = document.getElementById('pick-all'); return [b.checked, b.indeterminate]; }"
_TICKED = """() => [...document.querySelectorAll('#rows li.row')].filter((r) => r.querySelector('[data-pick]').checked)
  .map((r) => [r.querySelector('a.title').innerText, r.classList.contains('sel')])"""


def test_the_header_box_is_unticked_half_ticked_and_ticked_and_selects_and_clears(srv, job_apply_home):
    """B8's test 6. The table's header box (#pick-all) is unticked with nothing ticked, ticked when every recommended
    opening shown that can be applied to is, and half-ticked otherwise; pressed unticked it does what Select recommended
    does, pressed otherwise what Clear does. A ticked row is marked (sel), and a box ticked from the keyboard keeps the
    focus, ticked, through the refresh."""
    desk = _idle_desk(srv)
    desk.listings = [_opening(1, "Example Fab", "Field Service Engineer"),
                     _opening(2, "Example Litho", "Process Technician"),
                     _opening(3, "Example Etch", "Equipment Technician"),
                     _opening(4, "Example Low", "Accountant", score=30)]
    three = ["Field Service Engineer", "Process Technician", "Equipment Technician"]

    async def state(page) -> list:
        return [await page.evaluate(_PICK_ALL), await page.evaluate(_TICKED)]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                await open_desk(page, desk, "openings")
                await _until_titles(page, three)
                seen = {"label": await page.get_attribute("#pick-all", "aria-label"), "none": await state(page)}
                await page.focus("[data-pick='https://careers.example.com/2']")
                await page.keyboard.press("Space")
                await page.wait_for_selector("#rows li.row.sel")
                await page.wait_for_timeout(1700)  # (a refresh)
                seen["focus"] = await page.evaluate("document.activeElement.dataset.pick || ''")
                seen["one"] = await state(page)
                await page.click("#pick-all")  # (half-ticked: Clear)
                seen["cleared"] = await state(page)
                await page.click("#pick-all")  # (unticked: Select recommended)
                seen["all"] = await state(page)
                await page.click("[data-filter=all]")
                await _until_titles(page, [*three, "Accountant"])
                await page.check("[data-pick='https://careers.example.com/4']")
                seen["and one more"] = await page.evaluate(_PICK_ALL)
                await page.uncheck("[data-pick='https://careers.example.com/1']")
                seen["one off"] = await page.evaluate(_PICK_ALL)
                await page.click("#pick-all")  # (half-ticked: Clear)
                seen["cleared again"] = await state(page)
                await page.click("[data-act=select-recommended]")
                seen["select recommended"] = await page.evaluate(_PICK_ALL)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    assert seen == {"label": "Select the recommended openings shown",
                    "none": [[False, False], []],
                    "focus": "https://careers.example.com/2",
                    "one": [[False, True], [["Process Technician", True]]],
                    "cleared": [[False, False], []],
                    "all": [[True, False], [[t, True] for t in three]],
                    "and one more": [True, False],
                    "one off": [False, True],
                    "cleared again": [[False, False], []],
                    "select recommended": [True, False]}, seen


def test_a_row_where_the_desk_goes_all_the_way_says_so_in_its_applies_on_cell(srv, job_apply_home):
    """B8's test 8. The Applies on cell says the job system; under it, only where the desk goes all the way (the live
    check's employers: data/desk_handled.yaml), "Goes all the way" with its tooltip. A row without it has no second
    line there."""
    from job_apply import config

    assert "intel" in config.handled_employers() and "example fab" not in config.handled_employers()
    desk = _idle_desk(srv)
    desk.listings = [_opening(1, "Intel", "Module Equipment Technician"),
                     _opening(2, "Example Fab", "Field Service Engineer")]
    systems = {r["title"]: r["system"]["name"] for r in desk.state()["listings"]}
    cells = """() => Object.fromEntries([...document.querySelectorAll('#rows li.row')].map((r) => {
      const cell = r.querySelector('.c-sys'), handled = cell.querySelector('.handled');
      return [r.querySelector('a.title').innerText, [cell.getAttribute('role'), cell.innerText.split('\\n'),
              handled ? handled.title : null, r.querySelectorAll('.handled').length]];
    }))"""

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                await open_desk(page, desk, "openings")
                await _until_titles(page, ["Module Equipment Technician", "Field Service Engineer"])
                seen = await page.evaluate(cells)
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    tip = ("In the plugin's live check, the desk took a test applicant at this employer all the way to the review "
           "page.")
    assert all(systems.values()), systems
    assert seen == {"Module Equipment Technician": ["cell", [systems["Module Equipment Technician"], "Goes all the way"],
                                                    tip, 1],
                    "Field Service Engineer": ["cell", [systems["Field Service Engineer"]], None, 0]}, seen


# Where each line's cells sit: the six rows' and the header's (the box, Fit, Job, Pay, Posted, Applies on, Status)
_PLACES = """() => {
  const box = (el) => el.getBoundingClientRect();
  const lines = [...document.querySelectorAll('#rows li.row'), document.querySelector('.rows-head')].map((row) => {
    const cells = [...row.children], at = row.getBoundingClientRect();
    const [pick, fit, job, pay, posted, sys, status] = cells.map(box);
    return {cells: cells.length, roles: [...new Set(cells.map((c) => c.getAttribute('role')))],
            twoLines: [pay, posted, sys].every((c) => c.top >= job.bottom - 1),
            firstLine: [pick, fit, status].every((c) => c.top < job.bottom && c.bottom > job.top),
            inOrder: pay.left >= job.left - 1 && pay.right <= posted.left + 1 && posted.right <= sys.left + 1,
            inRow: cells.map(box).every((c) => c.left >= at.left - 1 && c.right <= at.right + 1),
            cut: cells.filter((c) => c.scrollWidth > c.clientWidth + 1).map((c) => c.className || c.innerText)};
  });
  const first = document.querySelector('#rows li.row'), content = document.getElementById('content');
  return {lines, sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth,
          content: content.scrollWidth > content.clientWidth,
          title: first.innerText.split('Senior Field Service Engineer').length - 1,
          pay: first.querySelectorAll('.pay').length, when: first.querySelectorAll('.when').length};
}"""


def test_at_1000_pixels_pay_posted_and_applies_on_sit_under_the_job_cell(srv, job_apply_home):
    """B8's test 9. At 1000px wide each row is two lines: the box, Fit, Job and Status on the first, Pay, Posted and
    Applies on under the Job cell, in that order; the header the same. Nothing is cut off (no cell wider than it shows,
    no sideways scroll) and nothing is doubled (each cell once). At 1440px they're one line, beside the Job cell."""
    desk = _idle_desk(srv)
    long = "Example Semiconductor Equipment and Materials Holdings"
    desk.listings = [_opening(1, long, "Senior Field Service Engineer, Lithography and Metrology Systems",
                              ats="workday", posted="2026-10-12"),
                     _opening(2, "Example Litho", "Process Technician")]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                seen = {}
                for width in (1000, 1440):
                    page = await browser.new_page(viewport={"width": width, "height": 900})
                    await open_desk(page, desk, "openings")
                    await page.wait_for_selector("#rows li.row .c-sys")
                    seen[width] = await page.evaluate(_PLACES)
                    await page.close()
                await browser.close()
                return seen
        finally:
            await desk.stop()

    seen = run(go())
    narrow, wide = seen[1000], seen[1440]
    assert [line["roles"] for line in narrow["lines"]] == [["cell"], ["cell"], ["columnheader"]], narrow
    for line in narrow["lines"]:
        assert {**line, "roles": None} == {"cells": 7, "roles": None, "twoLines": True, "firstLine": True,
                                           "inOrder": True, "inRow": True, "cut": []}, narrow
    assert not narrow["sideways"] and not narrow["content"], narrow
    assert (narrow["title"], narrow["pay"], narrow["when"]) == (1, 1, 1), narrow
    for line in wide["lines"]:
        assert not line["twoLines"] and line["inOrder"] and line["inRow"] and line["cut"] == [], wide
    assert not wide["sideways"] and not wide["content"], wide
