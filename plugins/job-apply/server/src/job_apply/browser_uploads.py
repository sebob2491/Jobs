"""Uploads, for BrowserSession (a mixin): a file sent through a site's own upload (its button, or its dialog's
Upload), and a file a page shows (kept, or never kept) read and deleted again."""

from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError, Frame, Locator, Page, TimeoutError as PlaywrightTimeout

from .formjs import (KEPT_DELETE_JS, KEPT_ROWS_JS, KEPT_STATE_JS, UNKEPT_DELETE_JS, UNKEPT_FORGET_JS,
                     UNKEPT_PRESSED_SHOWN_JS, UNKEPT_SHOWN_JS, UPLOAD_NOT_KEPT_JS, UPLOAD_STATE_JS)


class UploadsMixin:
    """BrowserSession's uploads."""

    if TYPE_CHECKING:  # (the session's own, and its tabs': browser_tabs)
        @property
        def _lock(self) -> asyncio.Lock: ...

        async def page(self) -> Page: ...

        def _frame_for(self, page: Page, element_id: str) -> Frame: ...

        def _locator(self, page: Page, element_id: str) -> Locator: ...

        @staticmethod
        def _check_clickable(info: dict[str, Any], url: str = "", agreement: bool = False) -> None: ...

    async def upload_not_kept(self) -> str | None:
        """The name of a file the page shows as uploaded though its own upload fields stayed empty (EMD's Phenom,
        issue #112, note 28: the resume's name and a Delete link, with every hidden field it keeps the upload in
        empty, so its Next did nothing), or None: the page's hidden fields named for the resume, two or more, all
        empty, with a file sent into a file box of the page."""
        async with self._lock:
            page = await self.page()
            for frame in page.frames:
                try:
                    if name := await browser._bounded(frame.evaluate(UPLOAD_NOT_KEPT_JS)):
                        return str(name)
                except (PlaywrightError, browser._Hung):  # a frame that went away meanwhile, or hung
                    continue
            return None

    async def delete_unkept_upload(self, name: str) -> str:
        """Press the Delete beside a file the page shows as uploaded but never kept (upload_not_kept), for it to go
        up again: only the desk's own upload, and only a control that deletes (UNKEPT_DELETE_JS), past the same check
        as every press the desk makes. "deleted" when the page then shows the file's name no more (its upload boxes
        forget they were given it, so the next fill sends it again), "pressed" when it still does with its Delete
        (a site that asks first, say), "gone" when the Delete went but the name is still on the page (a note of it),
        "" when there was no such Delete to press."""
        pressed = None
        async with self._lock:
            page = await self.page()
            for frame in page.frames:
                try:
                    if not await frame.evaluate(UNKEPT_DELETE_JS, name):
                        continue
                    button = frame.locator('[data-ja-unkept="1"]').first
                    self._check_clickable(await button.evaluate(browser.ELEMENT_INFO_JS, timeout=3000), page.url)
                    await button.click(timeout=5000)
                except browser.SubmitBlocked:
                    return ""
                except (PlaywrightError, PlaywrightTimeout):
                    continue
                pressed = frame
                break
        if pressed is None:
            return ""
        for _ in range(12):  # (a site's delete takes a moment: waited on with the browser free for the desk's looks)
            await asyncio.sleep(0.5)
            async with self._lock:
                with contextlib.suppress(PlaywrightError):
                    if not await pressed.evaluate(UNKEPT_SHOWN_JS, name):
                        await pressed.evaluate(UNKEPT_FORGET_JS, name)
                        return "deleted"
        async with self._lock:
            with contextlib.suppress(PlaywrightError):
                if not await pressed.evaluate(UNKEPT_PRESSED_SHOWN_JS):
                    return "gone"
        return "pressed"

    async def kept_upload_state(self, box_id: str) -> str:
        """An upload box showing a file, before the file tailored for this job goes in place of one kept from before
        (Applier._replace_kept): "mine" where the desk sent a file into it itself, "other" where what it shows isn't in
        Workday's list of files (never touched), "" otherwise; "gone" with the box gone (KEPT_STATE_JS). "unread" where
        the box is there but didn't answer in three tries (a slow page, a loaded machine): never taken for a box gone,
        whose kept file would go to the employer unreplaced (the re-review of 0.3.209)."""
        for _ in range(3):
            async with self._lock:
                page = await self.page()
                try:
                    box = self._locator(page, box_id)
                    if not await box.count():
                        return "gone"
                    return str(await box.evaluate(KEPT_STATE_JS, timeout=3000) or "")
                except PlaywrightTimeout:
                    pass
                except (PlaywrightError, KeyError):
                    return "gone"
            await asyncio.sleep(0.5)
        return "unread"

    async def delete_kept_upload(self, box_id: str, name: str) -> str:
        """Press the Delete of a file an upload box shows as kept from before (Workday keeps an account's last resume in
        its "Resume/CV" upload), for the file tailored for this job to go in its place: only that file's own Delete in
        Workday's list (KEPT_DELETE_JS), past the same check as every press the desk makes. `name`: the file's name as
        the box shows it, "" where it shows only that a file is there. "deleted" once the list shows that file in fewer
        items than before (KEPT_ROWS_JS: never by its Delete alone going, as a list drawn again does, nor by its name gone
        from the page), "pressed" when it doesn't (a site that asks first, turned down), "" when there was no such
        Delete to press."""
        async with self._lock:
            page = await self.page()
            try:
                frame = self._frame_for(page, box_id)
                if not await self._locator(page, box_id).evaluate(KEPT_DELETE_JS, name, timeout=3000):
                    return ""
                before = int(await frame.evaluate(KEPT_ROWS_JS, name))
                button = frame.locator('[data-ja-kept-delete="1"]').first
                self._check_clickable(await button.evaluate(browser.ELEMENT_INFO_JS, timeout=3000), page.url)
                await button.click(timeout=5000)
            except (browser.SubmitBlocked, KeyError, PlaywrightError, PlaywrightTimeout):
                return ""
        for _ in range(12):  # (a site's delete takes a moment: waited on with the browser free for the desk's looks)
            await asyncio.sleep(0.5)
            async with self._lock:
                with contextlib.suppress(PlaywrightError):
                    if int(await frame.evaluate(KEPT_ROWS_JS, name)) < before:
                        return "deleted"
        return "pressed"

    async def upload_state(self, name: str) -> str:
        """Whether the page kept the file `name` sent into its resume box (UPLOAD_STATE_JS): "kept", "not kept", or
        "" when no resume box holds it."""
        async with self._lock:
            page = await self.page()
            states = []
            for frame in page.frames:
                with contextlib.suppress(PlaywrightError):
                    states.append(await frame.evaluate(UPLOAD_STATE_JS, name))
            return "kept" if "kept" in states else "not kept" if "not kept" in states else ""

    async def _send_dialog_upload(self, page: Page, box: Locator, path: Path) -> str:
        """A file box inside the site's own upload dialog takes the file only once the dialog's Upload is pressed
        (Amkor's SuccessFactors, the nightly of Oct 12: its "Upload Resume" dialog, open over the profile, kept the
        file in its box while the desk pressed Next, and the page said "Resume: field is required"). Only a dialog
        that's an upload's alone: no other box in it (an application drawn as a dialog is left as it is), its
        Upload or Attach pressed (its OK, Save or Done only where the dialog says it's an upload), through the
        same guard as every press. A dialog still open after is said in the result, not taken for a failure, and
        raises only where the press itself failed (so the next fill presses it again)."""
        dialog = box.locator("xpath=ancestor::*[@role='dialog' or @role='alertdialog' or self::dialog][1]")
        if not await dialog.count():
            return f"uploaded {path.name}"
        facts = await dialog.evaluate("""(d) => {
          const shown = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
          const boxes = [...d.querySelectorAll('input, select, textarea')].filter((el) => el.type !== 'hidden' && shown(el));
          const named = [d.getAttribute('aria-label') || '', ...(d.getAttribute('aria-labelledby') || '').split(/\\s+/)
            .map((id) => (document.getElementById(id) || {}).textContent || ''),
            (d.querySelector('h1, h2, h3, h4, [role="heading"]') || {}).textContent || ''].join(' ');
          return {others: boxes.filter((el) => el.type !== 'file').length, files: boxes.filter((el) => el.type === 'file').length,
                  upload: /\\b(upload|attach|resume|cv|file|document)/i.test(named)};
        }""")
        if facts["others"] or facts["files"] > 1:
            return f"uploaded {path.name}"
        words = r"upload|attach|ok|save|done" if facts["upload"] else r"upload|attach"
        send = dialog.locator('button:visible, [role="button"]:visible').filter(
            has_text=re.compile(rf"^\s*({words})\s*$", re.I))
        if not await send.count():
            return f"uploaded {path.name}"
        self._check_clickable(await send.first.evaluate(browser.ELEMENT_INFO_JS, timeout=3000), page.url)
        pressed = (await send.first.inner_text()).strip()
        await send.first.click(timeout=3000)
        for _ in range(int(browser.UPLOAD_SHOWN_WAIT * 2)):
            with contextlib.suppress(PlaywrightError, PlaywrightTimeout):
                if not await dialog.is_visible(timeout=1000):
                    return f"uploaded {path.name} (its dialog's \u201c{pressed}\u201d pressed)"
            await page.wait_for_timeout(500)
        return f"uploaded {path.name} (its dialog's \u201c{pressed}\u201d pressed; the dialog is still open)"

    async def _upload_through(self, page: Page, button: Locator, path: Path) -> str:
        """A file given through the button that opens the site's own upload (the older SuccessFactors site's
        "Upload a Resume", which opens a dialog: APS's, the live check's plus probe Oct 11). Pressed, the file
        goes to the file chooser it opens, or to the file box its dialog shows (after the dialog's own Browse,
        where it has one), and that dialog's Upload (or OK) is pressed. Only what the press opened is touched:
        the dialogs and file boxes already on the page are marked first (a cookie banner's OK is never pressed,
        another upload's box never given this file). Said when its field doesn't then show the file."""
        dialogs = '[role="dialog"], [role="alertdialog"], dialog[open]'
        # (the dialogs on show, and the file boxes outside any dialog: one drawn hidden until it's opened is the press's)
        await page.evaluate("""(sel) => {
          for (const d of document.querySelectorAll(sel)) if (d.getClientRects().length) d.setAttribute('data-ja-before', '');
          for (const b of document.querySelectorAll('input[type="file"]')) if (!b.closest(sel)) b.setAttribute('data-ja-before', '');
        }""", dialogs)
        try:
            async def chosen(press: Locator) -> bool:  # pressed: did a file chooser open, and take the file?
                try:
                    async with page.expect_file_chooser(timeout=browser.UPLOAD_CHOOSER_WAIT) as chooser:
                        await press.click(timeout=3000)
                    await (await chooser.value).set_files(str(path))
                    return True
                except PlaywrightTimeout:
                    return False

            took = await chosen(button)
            await page.wait_for_timeout(500)
            opened = page.locator(", ".join(f"{d}:not([data-ja-before]):visible" for d in dialogs.split(", "))).last
            dialog = opened if await opened.count() else None
            if not took:
                new_box = (dialog or page).locator('input[type="file"]:not([data-ja-before])')
                browse = dialog.locator('button:visible, [role="button"]:visible, a:visible').filter(
                    has_text=re.compile(r"^\s*(browse|choose (a )?file|select (a )?file|upload from (my )?(device|computer)"
                                        r"|from (my )?(device|computer))\b", re.I)) if dialog else None
                if browse is not None and await browse.count() and await chosen(browse.first):
                    pass
                elif await new_box.count():
                    await new_box.last.set_input_files(str(path))
                else:
                    raise ValueError("pressing it opened no way to choose a file; upload it in "
                                     "the desk's browser window")
                await page.wait_for_timeout(500)
            if dialog is not None and await dialog.count():  # the dialog it opened: its own Upload (or OK, Save, Done)
                confirm = dialog.locator('button:visible, [role="button"]:visible, input[type="submit"]:visible').filter(
                    has_text=re.compile(r"^\s*(upload|attach|ok|save|done)\s*$", re.I))
                if await confirm.count():
                    await confirm.first.click(timeout=3000)
        finally:
            with contextlib.suppress(PlaywrightError):
                await page.evaluate("() => document.querySelectorAll('[data-ja-before]').forEach("
                                    "(el) => el.removeAttribute('data-ja-before'))")
        # shown in its own field: its name, or the upload's tick (not another upload's)
        shown = """(el, k) => {
          const field = el.closest('.RCMFormField, [class*="attachmentField" i]') || el.parentElement.parentElement || el;
          return (field.innerText || '').includes(k)
            || [...field.querySelectorAll('[class*="attachSuccess" i]')].some((x) => x.getClientRects().length > 0);
        }"""
        for _ in range(int(browser.UPLOAD_SHOWN_WAIT * 2)):
            with contextlib.suppress(PlaywrightError, PlaywrightTimeout):
                if await button.evaluate(shown, path.name, timeout=2000):
                    return f"uploaded {path.name}"
            await page.wait_for_timeout(500)
        raise ValueError(f"the page didn't show {path.name} after its upload; check it in the desk's browser window")


# browser.py's own names (its waits, page scripts, helpers and exceptions) are read from it as browser.<name> when a
# method runs: a test's patch of one (monkeypatch.setattr(browser, ...)) reaches the code here as it does browser.py's.
# Imported last: browser.py imports this module for BrowserSession's bases, and either may be imported first.
from . import browser  # noqa: E402
