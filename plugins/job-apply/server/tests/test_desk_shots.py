"""Pictures of the Job Desk's pages (the design's "The pictures"), for a pull request and for looking a change over.
Skipped unless JOB_APPLY_SHOTS names a folder, so the ordinary run is no slower. With it, the page is drawn from each
saved state (tests/fixtures/desk/state-<case>.json, made-up values only: the repo is public), for each of the side
menu's seven pages, in the light and the dark look, in a window of 1440 by 900 and one of 1100 by 800, and a PNG of
each is saved there as <case>-<page>-<look>-<width>x<height>.png:

    JOB_APPLY_SHOTS=/somewhere/shots uv run pytest -q -p no:cacheprovider -n 2 tests/test_desk_shots.py

A picture is the window as the person sees it: what's below the fold of a long page isn't in it."""

import os
import time
from pathlib import Path

import pytest
from conftest import browser_available, open_desk, open_filter, person_browser, run
from test_desk_frame import DESK_FIXTURES, PAGES, _canned, _state

SHOTS = os.environ.get("JOB_APPLY_SHOTS", "")

pytestmark = [pytest.mark.skipif(not SHOTS, reason="JOB_APPLY_SHOTS names no folder for the pictures"),
              pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")]

CASES = sorted(path.stem.removeprefix("state-") for path in DESK_FIXTURES.glob("state-*.json"))
LOOKS = ("light", "dark")
WINDOWS = ((1440, 900), (1100, 800))


def shot_name(case: str, page: str, look: str, window: tuple[int, int]) -> str:
    return f"{case}-{page}-{look}-{window[0]}x{window[1]}.png"


@pytest.mark.parametrize("window", WINDOWS, ids=lambda w: f"{w[0]}x{w[1]}")
@pytest.mark.parametrize("look", LOOKS)
@pytest.mark.parametrize("case", CASES)
def test_a_picture_of_each_page(srv, job_apply_home, monkeypatch, case, look, window):
    """Each of the seven pages, drawn from one saved state in one look and one window size, saved as a PNG."""
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    desk = _canned(srv, monkeypatch, _state(f"state-{case}.json"))

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": window[0], "height": window[1]}, color_scheme=look)
                written = []
                for name in PAGES:
                    await open_desk(page, desk, name)
                    await page.wait_for_function(f"document.body.dataset.page === '{name}'")
                    await page.wait_for_timeout(150)  # (drawn, and a hover or press settled)
                    path = folder / shot_name(case, name, look, window)
                    await page.screenshot(path=str(path))
                    written.append(path)
                dark = await page.evaluate("matchMedia('(prefers-color-scheme: dark)').matches")
                await browser.close()
                return written, dark
        finally:
            await desk.stop()

    written, dark = run(go())
    assert dark == (look == "dark")
    assert [path.name for path in written] == [shot_name(case, name, look, window) for name in PAGES]
    assert all(path.stat().st_size > 2000 for path in written), [(path.name, path.stat().st_size) for path in written]


SETTINGS_PANELS = ("profile", "search", "applying", "passwords", "email", "phone", "documents", "about")


@pytest.mark.parametrize("window", (*WINDOWS, (900, 700)), ids=lambda w: f"{w[0]}x{w[1]}")
@pytest.mark.parametrize("look", LOOKS)
def test_a_picture_of_each_settings_panel(srv, job_apply_home, monkeypatch, look, window):
    """Each of the Settings page's eight panels (the design's D1), each opened by its address, drawn from the openings
    state, in each look and window size (and a narrow one, where the panels' menu is a row above the panel); saved as
    settings-<panel>-<look>-<w>x<h>.png."""
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    desk = _canned(srv, monkeypatch, _state("state-openings.json"))
    names = [f"settings-{panel}-{look}-{window[0]}x{window[1]}.png" for panel in SETTINGS_PANELS]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": window[0], "height": window[1]}, color_scheme=look)
                for panel, name in zip(SETTINGS_PANELS, names):
                    await open_desk(page, desk, f"settings/{panel}")
                    await page.wait_for_timeout(150)
                    await page.screenshot(path=str(folder / name))
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert all((folder / name).stat().st_size > 2000 for name in names)


@pytest.mark.parametrize("look", LOOKS)
def test_a_picture_of_applying_in_the_look_chosen_in_settings(srv, job_apply_home, monkeypatch, look):
    """Settings' Applying panel (the openings state) in the look chosen by Look (the design's D3), on a computer set to
    the other look, in a window of 1440 by 900; saved as settings-applying-chosen-<look>.png."""
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    desk = _canned(srv, monkeypatch, _state("state-openings.json"))
    path = folder / f"settings-applying-chosen-{look}.png"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                other = "light" if look == "dark" else "dark"
                page = await browser.new_page(viewport={"width": 1440, "height": 900}, color_scheme=other)
                await open_desk(page, desk, "settings/about")
                await page.click(f"[data-look={look}]")
                await open_desk(page, desk, "settings/applying")
                await page.wait_for_timeout(150)
                await page.screenshot(path=str(path))
                theme = await page.evaluate("() => document.documentElement.dataset.theme")
                await browser.close()
                return theme
        finally:
            await desk.stop()

    assert run(go()) == look
    assert path.stat().st_size > 2000


@pytest.mark.parametrize("dropdown", ["pay", "more", "add"])
@pytest.mark.parametrize("look", LOOKS)
def test_a_picture_of_the_openings_with_a_drop_down_open(srv, job_apply_home, monkeypatch, look, dropdown):
    """The Openings page (the openings state) with one of its drop-downs open (the design's B3): Pay, More filters, or
    Add job links, in each look, in a window of 1440 by 900; saved as openings-openings-<drop-down>-<look>.png."""
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    desk = _canned(srv, monkeypatch, _state("state-openings.json"))
    path = folder / f"openings-openings-{dropdown}-{look}.png"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900}, color_scheme=look)
                await open_desk(page, desk, "openings")
                await open_filter(page, dropdown)
                await page.wait_for_timeout(150)
                await page.screenshot(path=str(path))
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert path.stat().st_size > 2000


@pytest.mark.parametrize("window", WINDOWS, ids=lambda w: f"{w[0]}x{w[1]}")
@pytest.mark.parametrize("look", LOOKS)
def test_a_picture_of_the_openings_with_jobs_ticked(srv, job_apply_home, monkeypatch, look, window):
    """The Openings page (the openings state) with the recommended openings ticked, so the apply bar shows over its
    foot (the design's B5), in each look and window size; saved as openings-openings-ticked-<look>-<w>x<h>.png."""
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    desk = _canned(srv, monkeypatch, _state("state-openings.json"))
    path = folder / f"openings-openings-ticked-{look}-{window[0]}x{window[1]}.png"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": window[0], "height": window[1]}, color_scheme=look)
                await open_desk(page, desk, "openings")
                await page.click("[data-act=select-recommended]")
                await page.wait_for_selector(".applybar", state="visible")
                await page.wait_for_timeout(150)
                await page.screenshot(path=str(path))
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert path.stat().st_size > 2000


@pytest.mark.parametrize("window", WINDOWS, ids=lambda w: f"{w[0]}x{w[1]}")
@pytest.mark.parametrize("look", LOOKS)
@pytest.mark.parametrize("cards", ["closed", "open", "pass"])
def test_a_picture_of_home_with_many_jobs(srv, job_apply_home, monkeypatch, cards, look, window):
    """Home (the stops state: more than three jobs in Needs you and in Ready to submit, so their cards start closed, the
    design's C3), as its cards start, with every one opened, or going through them (C4: the header's words, the card
    it's on open and marked now), in each look and window size, each job stopped some minutes before; saved as
    stops-home-<closed, open or pass>-<look>-<w>x<h>.png."""
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    state = _state("state-stops.json")
    now = time.time()
    for row in [*state.get("listings", []), *state.get("others", [])]:
        if (row.get("run") or {}).get("paused_at"):  # (the fixture's 1000 to 1011: 10 to 21 minutes ago)
            row["run"]["paused_at"] = now - 60 * (row["run"]["paused_at"] - 990)
    desk = _canned(srv, monkeypatch, state)
    path = folder / f"stops-home-{cards}-{look}-{window[0]}x{window[1]}.png"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": window[0], "height": window[1]}, color_scheme=look)
                await open_desk(page, desk, "home")
                await page.wait_for_selector("#needs .task [data-open][aria-expanded]")
                if cards == "open":
                    await page.evaluate("() => document.querySelectorAll('[data-open][aria-expanded=false]')"
                                        ".forEach((b) => b.click())")
                if cards == "pass":
                    await page.click("#needs-pass [data-act=pass-start]")
                    await page.wait_for_selector("#needs > .task.now")
                # (the tiles and the lists at the window's top, the page's own top padding above them as when it isn't
                # scrolled: the notices over them fill most of it)
                await page.evaluate("""() => { const content = document.getElementById('content');
                  content.scrollTop += document.getElementById('tiles').getBoundingClientRect().top
                    - content.getBoundingClientRect().top - parseFloat(getComputedStyle(content).paddingTop); }""")
                await page.wait_for_timeout(150)
                await page.screenshot(path=str(path))
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert path.stat().st_size > 2000


@pytest.mark.parametrize("look", LOOKS)
def test_a_picture_of_the_applications_with_change_status_open(srv, job_apply_home, monkeypatch, look):
    """The Applications page (the openings state) with the first row's Change status menu open (the design's C8), in
    each look, in a window of 1440 by 900; saved as openings-applications-status-<look>.png."""
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    desk = _canned(srv, monkeypatch, _state("state-openings.json"))
    path = folder / f"openings-applications-status-{look}.png"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900}, color_scheme=look)
                await open_desk(page, desk, "applications")
                await page.click("#app-rows details.menu > summary >> nth=0")
                await page.wait_for_selector("#app-rows details.menu[open] .menu-panel", state="visible")
                await page.wait_for_timeout(150)
                await page.screenshot(path=str(path))
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert path.stat().st_size > 2000


@pytest.mark.parametrize("page_name", ["home", "openings"])
@pytest.mark.parametrize("look", LOOKS)
def test_a_picture_of_a_desk_without_claude(srv, job_apply_home, monkeypatch, look, page_name):
    """Home and Openings of a desk run on its own (the design's C7: state.standalone), drawn from the openings state,
    on Openings with the recommended openings ticked so the apply bar shows (without Tailor my resume), in each look, in
    a window of 1440 by 900; saved as standalone-<page>-<look>.png."""
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    desk = _canned(srv, monkeypatch, {**_state("state-openings.json"), "standalone": True})
    path = folder / f"standalone-{page_name}-{look}.png"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": 1440, "height": 900}, color_scheme=look)
                await open_desk(page, desk, page_name)
                if page_name == "openings":
                    await page.click("[data-act=select-recommended]")
                    await page.wait_for_selector(".applybar", state="visible")
                await page.wait_for_timeout(150)
                await page.screenshot(path=str(path))
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert path.stat().st_size > 2000


@pytest.mark.parametrize("window", WINDOWS, ids=lambda w: f"{w[0]}x{w[1]}")
@pytest.mark.parametrize("look", LOOKS)
def test_a_picture_of_a_new_persons_home(srv, job_apply_home, monkeypatch, look, window):
    """Home for someone new (the design's C6): Getting started at the top with a few of its lines done (the profile,
    what and where, the inbox's password and one of the common questions answered; never searched, nothing found or
    applied for), in each look and window size; saved as new-home-<look>-<w>x<h>.png."""
    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    state = _state("state-openings.json")
    state.update(listings=[], others=[], applications=[], search={**state["search"], "status": "idle", "at": None},
                 area={**state["area"], "titles": ["Technician"], "places": ["Phoenix, AZ"]})
    state["profile"]["missing"] = []
    desk = _canned(srv, monkeypatch, state)
    path = folder / f"new-home-{look}-{window[0]}x{window[1]}.png"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": window[0], "height": window[1]}, color_scheme=look)
                await open_desk(page, desk, "home")
                await page.wait_for_selector("#start-lines > .item")
                await page.wait_for_timeout(150)
                await page.screenshot(path=str(path))
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert path.stat().st_size > 2000


@pytest.mark.parametrize("window", WINDOWS, ids=lambda w: f"{w[0]}x{w[1]}")
@pytest.mark.parametrize("look", LOOKS)
def test_a_picture_of_your_answers_with_remembered_answers(srv, job_apply_home, monkeypatch, look, window):
    """Your answers (the openings state) with answers the desk remembers (the design's D5): an ID kept for one
    employer (dots), one answered everywhere being changed (its box, Save and Cancel), one left empty at another
    employer, and a second ID; scrolled to the card, in each look and window size; saved as
    answers-remembered-<look>-<w>x<h>.png. Made-up values only."""
    from test_desk_answers import _write

    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    _write(job_apply_home)
    desk = _canned(srv, monkeypatch, _state("state-openings.json"))
    path = folder / f"answers-remembered-{look}-{window[0]}x{window[1]}.png"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": window[0], "height": window[1]}, color_scheme=look)
                await open_desk(page, desk, "answers")
                await page.wait_for_selector("#remembered-card[data-loaded]", state="attached")
                row = page.locator("#remembered > .setrow").nth(1)
                await row.locator("button", has_text="Change").click()
                await row.locator("input[type=text]").fill("Yes, any weekend")
                await page.evaluate("() => document.getElementById('remembered-card').scrollIntoView()")
                await page.wait_for_timeout(150)
                await page.screenshot(path=str(path))
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert path.stat().st_size > 2000


@pytest.mark.parametrize("window", WINDOWS, ids=lambda w: f"{w[0]}x{w[1]}")
@pytest.mark.parametrize("look", LOOKS)
def test_a_picture_of_the_safety_window(srv, job_apply_home, monkeypatch, look, window):
    """Settings' Applying panel with the Safety window open, each "where" that names a panel a link to it (the
    design's D4), in each look and window size; saved as settings-safety-<look>-<w>x<h>.png."""
    from test_desk_buttons import _idle_desk

    folder = Path(SHOTS)
    folder.mkdir(parents=True, exist_ok=True)
    desk = _idle_desk(srv)
    path = folder / f"settings-safety-{look}-{window[0]}x{window[1]}.png"

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page(viewport={"width": window[0], "height": window[1]}, color_scheme=look)
                await open_desk(page, desk, "settings/applying")
                await page.click("#safety-btn")
                await page.wait_for_selector("#safety[open] #safety-body li")
                await page.wait_for_timeout(150)
                await page.screenshot(path=str(path))
                await browser.close()
        finally:
            await desk.stop()

    run(go())
    assert path.stat().st_size > 2000
