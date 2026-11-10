"""Pictures of the Job Desk's pages (the design's "The pictures"), for a pull request and for looking a change over.
Skipped unless JOB_APPLY_SHOTS names a folder, so the ordinary run is no slower. With it, the page is drawn from each
saved state (tests/fixtures/desk/state-<case>.json, made-up values only: the repo is public), for each of the side
menu's seven pages, in the light and the dark look, in a window of 1440 by 900 and one of 1100 by 800, and a PNG of
each is saved there as <case>-<page>-<look>-<width>x<height>.png:

    JOB_APPLY_SHOTS=/somewhere/shots uv run pytest -q -p no:cacheprovider -n 2 tests/test_desk_shots.py

A picture is the window as the person sees it: what's below the fold of a long page isn't in it."""

import os
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
