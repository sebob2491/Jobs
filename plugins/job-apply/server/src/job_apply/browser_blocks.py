"""A section's numbered blocks (a job, a school), for BrowserSession (a mixin): its Add button pressed until it has
enough, in whichever frame holds it, and the blocks the desk's own Add drew."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError, Frame, Page, TimeoutError as PlaywrightTimeout

from .formjs import ADDED_BLOCKS_JS, ADD_UNWATCH_JS, ADD_WATCH_JS, ENTRIES_JS


class BlocksMixin:
    """BrowserSession's blocks."""

    if TYPE_CHECKING:  # (the session's own, and its tabs': browser_tabs)
        @property
        def _lock(self) -> asyncio.Lock: ...

        async def page(self) -> Page: ...

        def _frame_prefix(self, frame: Frame, page: Page) -> str: ...

        async def _settle(self, page: Page, timeout: int = 6000) -> None: ...

    def _form_frames(self, page: Page) -> list[Frame]:
        """The page and the frames in it that may hold the application: never a bot check's, a player's or a blank
        one (the frames _extract reads)."""
        return [f for f in page.frames if not f.is_detached() and (f is page.main_frame or (
            bool(f.url) and f.url != "about:blank" and not browser._CAPTCHA_FRAME.match(f.url)
            and not browser._MEDIA_FRAME.match(f.url)))]

    async def _entries_frame(self, page: Page, kind_pattern: str) -> tuple[Frame, str, dict[str, Any]]:
        """The frame holding the section's Add button, its id prefix, and its entries as read: the page itself when it's
        there, else the application drawn inside a frame on the careers page (iCIMS's #icims_content_iframe, eleven
        employers on the lists: the desk looked for the button on the page around the frame, found none, and went on
        with one job and one school; the review of Oct 15). Among frames with an Add button, the one with the most
        numbered blocks of the kind, then the most boxes (the form's frame over a careers page whose own "Add" sits
        under a heading of the kind: the review of 0.3.186), then the page itself first. With no Add button anywhere,
        the same order over every frame."""
        read: list[tuple[Frame, str, dict[str, Any]]] = []
        for frame in self._form_frames(page):
            prefix = self._frame_prefix(frame, page)
            try:
                read.append((frame, prefix, await frame.evaluate(ENTRIES_JS, [kind_pattern, prefix])))
            except PlaywrightError:  # (a frame that refused, or navigated mid-read: not the form's)
                if frame is page.main_frame:
                    raise
        with_button = [r for r in read if r[2]["buttons"]]
        return max(with_button or read, key=lambda r: (r[2]["entries"], r[2].get("boxes", 0)))

    async def add_entries(self, kind_pattern: str, count: int) -> dict[str, Any]:
        """Click a section's Add button until it has `count` numbered entries, in the frame that holds it. A frame that
        goes away meanwhile (the careers page drew it again) ends the adding with what was done, as a page with no Add
        button would: never the job's failure (the review of 0.3.186)."""
        async with self._lock:
            page = await self.page()
            frame, prefix, state = await self._entries_frame(page, kind_pattern)
            framed = frame is not page.main_frame
            before, clicks, covered = state["entries"], 0, False

            async def entries() -> dict[str, Any]:
                return await frame.evaluate(ENTRIES_JS, [kind_pattern, prefix])

            # the block a press draws is marked as the desk's own (only such a block is ever deleted again: the review
            # of 0.3.173): watched from just before each press, and no longer after one that drew none
            async def watch(on: bool) -> None:
                with contextlib.suppress(PlaywrightError):
                    await (frame.evaluate(ADD_WATCH_JS, kind_pattern) if on
                           else frame.evaluate(ADD_UNWATCH_JS))

            try:
                # a page opened again draws the blocks saved earlier a moment after itself (Baker Tilly's and U.S.
                # Bank's Workday My Experience, run 52, Oct 13: none counted, one added, and the saved one came beside
                # it): looked for again a moment before any is added
                waited = 0
                while state["entries"] < count and state["buttons"] and waited < browser.SAVED_DRAW_WAIT:
                    await page.wait_for_timeout(500)
                    waited += 500
                    state = await entries()
                before = state["entries"]
                while state["entries"] < count and clicks < count + 2:
                    if not state["buttons"]:
                        break
                    pressed = False
                    for attempt in range(1, browser.ADD_TRIES + 1):
                        button = frame.locator(browser._by_ja_id(state["buttons"][-1]["id"]))
                        try:
                            await watch(True)
                            await button.click(timeout=browser.ADD_PRESS_TIMEOUT)
                            pressed = True
                            break
                        except PlaywrightTimeout:
                            await watch(False)
                            # something drawn over it (Nikon's UKG Pro, live, Oct 2026: the error ended the whole
                            # application). A toast or a re-render clears in a moment (Workday's My Experience, note 32:
                            # "added 1 work block(s)", then the rest left off): the button is scrolled to the middle of
                            # the page and looked at again, the page given longer each time, then pressed again. A cover
                            # that stays is left as it is, as a press beneath a note or a dialog may not be its: never
                            # pressed through
                            covered = attempt == browser.ADD_TRIES
                            if covered:
                                break
                            if await browser._covered(button):
                                await page.wait_for_timeout(browser.ADD_COVER_WAIT * attempt)
                            state = await entries()
                            if state["entries"] >= count or not state["buttons"]:
                                break  # (the blocks came by themselves, or the button is gone: the loop's to say)
                    if covered:
                        break
                    if not pressed:
                        continue
                    clicks += 1
                    await page.wait_for_timeout(600)
                    new = await entries()
                    if new["entries"] <= state["entries"]:
                        await page.wait_for_timeout(1200)  # slow re-render; one more look
                        new = await entries()
                        if new["entries"] <= state["entries"]:
                            await watch(False)  # (it drew none: a block drawn from here on isn't its)
                            state = new
                            break
                    state = new
                # Workday draws a new block's heading first and its boxes a moment later: the page is read
                # next (autofill) once they're there, or the new block isn't filled
                waited = 0
                while clicks and state.get("empty") and waited < browser.BLOCK_DRAW_WAIT:
                    await page.wait_for_timeout(250)
                    waited += 250
                    state = await entries()
                if waited:
                    await self._settle(page, timeout=2000)  # the rest of their boxes
                    if framed:  # (the frame's own drawing: _settle watches the page around it)
                        with contextlib.suppress(PlaywrightError, browser._Hung):
                            await browser._bounded(frame.evaluate(browser.QUIET_JS, [350, 2000]), 5)
            except PlaywrightError:
                if not framed or not frame.is_detached():
                    raise
                pass  # (the frame is gone: what was done stands)
            finally:
                await watch(False)
            return {"before": before, "after": state["entries"], "clicks": clicks,
                    "add_button_found": bool(state["buttons"]) or clicks > 0,
                    **({"add_button_covered": True} if covered else {})}

    async def added_blocks(self) -> list[str]:
        """The headings of the blocks the desk's own Add drew (add_entries marks them), as they read now, in the page
        or any frame of it: a block numbered again keeps its mark."""
        async with self._lock:
            page = await self.page()
            marks: list[str] = []
            for frame in self._form_frames(page):
                with contextlib.suppress(PlaywrightError):
                    marks.extend(await frame.evaluate(ADDED_BLOCKS_JS))
            return marks


# browser.py's own names (its waits, page scripts, helpers and exceptions) are read from it as browser.<name> when a
# method runs: a test's patch of one (monkeypatch.setattr(browser, ...)) reaches the code here as it does browser.py's.
# Imported last: browser.py imports this module for BrowserSession's bases, and either may be imported first.
from . import browser  # noqa: E402
