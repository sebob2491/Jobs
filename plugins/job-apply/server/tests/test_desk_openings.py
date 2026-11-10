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
