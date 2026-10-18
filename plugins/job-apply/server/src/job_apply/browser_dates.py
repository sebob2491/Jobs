"""Dates, for BrowserSession (a mixin): a date's parts typed (Workday's MM / DD / YYYY), and a day picked in a date
picker's calendar."""

from __future__ import annotations

import contextlib
import os
import re
import sys
from datetime import date
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError, Frame, Locator, Page, TimeoutError as PlaywrightTimeout

from .autofill import date_form
from .formjs import DAY_CELL_JS


class DatesMixin:
    """BrowserSession's dates."""

    if TYPE_CHECKING:  # (the session's own, and its menus': browser_menus)
        _date_typed: set[str]

        def _frame_for(self, page: Page, element_id: str) -> Frame: ...

        async def _activate(self, loc: Locator) -> None: ...

        def _own_frames(self, page: Page, element_id: str) -> list[Frame]: ...

        async def _close_pickers(self, page: Page, frames: list[Frame] | None = None, field_id: str = "") -> None: ...

        async def _type_and_pick(self, page: Page, loc: Locator, field: dict, value: Any) -> str: ...

    async def _type_date_part(self, page: Page, loc: Locator, text: str, given: dict[str, Any] | None = None,
                              clear: bool = False) -> str:
        """A date's part (Workday's MM / DD / YYYY), which reacts to keystrokes, not a pasted value.

        Some Workday dates send their digits to the part the date counts as current, which starts at the
        first part, moves on by itself once a part is full, and goes back to the first whenever focus leaves
        the date: typed part by part, with the date left in between, every year went into the month (issue
        #112, note 13, live, Oct 2026). Others (Intel's and Entegris', traced live Oct 12) send them to the
        part with focus, whatever part they mark current, and their arrows only move focus. So the date's
        parts are typed in one run from its first part, each padded so it's full (the parts before this one
        as they read now); the first one's display pressed where it's a later part or the date marks another
        current (which makes it both current and focused), each focused where the page didn't move focus on
        to it, and no arrows; then each is read back, and a part that doesn't read as typed is said, not left
        wrong. Those dates also read the whole date again as focus leaves it, from its parts with a number, a lone
        one as the year (Intel's and U.S. Bank's month, typed with the year still empty, came back as the year
        2003, traced live Oct 12): the later parts given in the same fill (`given`, by id) are typed in the same
        run, so the date is never left half typed. A date that doesn't read back as typed is typed once more, the
        same parts with the same digits, a little slower: HonorHealth's school year now and then lost the keys typed as
        it took focus, and read 1900 (run 49, Oct 13)."""
        count = await loc.evaluate(browser.DATE_PARTS_JS,
                                   timeout=browser.FOCUS_TIMEOUT)  # marks the date's parts in order
        # (found from this box, so in its own frame: the nearest box around it holding them all)
        parts = loc if count == 1 else loc.locator(
            f"xpath=ancestor::*[count(.//*[@data-ja-date-part]) = {count}][1]//*[@data-ja-date-part]")
        try:
            # (a part with no aria-valuemax is sized by its name: Month and Day two digits, Year four)
            infos = [await parts.nth(i).evaluate(
                "el => ({id: el.getAttribute('data-ja-id'), mine: el.hasAttribute('data-ja-date-mine'), max: Number(el.getAttribute('aria-valuemax'))"
                " || (/year|yyyy/i.test(el.getAttribute('aria-label') || el.placeholder || '') ? 9999"
                " : /day|dd/i.test(el.getAttribute('aria-label') || el.placeholder || '') ? 31"
                " : /month|mm/i.test(el.getAttribute('aria-label') || el.placeholder || '') ? 12 : 0),"
                " now: el.getAttribute('aria-valuenow') || (el.value || '').replace(/\\D/g, '')})") for i in range(count)]
            mine = next(i for i, info in enumerate(infos) if info["mine"])
            digits = re.sub(r"\D", "", text)
            if not digits:
                raise ValueError(f"{text!r} isn't a number for a date's part")

            def full(value: str, top: int) -> str:
                return value.zfill(2) if 0 < top <= 31 else value.zfill(4) if top >= 1000 and len(value) <= 4 else value

            trace = os.environ.get("JOB_APPLY_DATE_TRACE") == "1"  # (the live check's: each step of a date, said)

            async def said(step: str) -> None:
                if trace:
                    with contextlib.suppress(PlaywrightError, PlaywrightTimeout):
                        now = await parts.evaluate_all(
                            "ps => ps.map((p) => (p.getAttribute('aria-valuenow') || p.value || '-') + "
                            "(p.parentElement && p.parentElement.getAttribute('tabindex') === '0' ? '*' : '') + "
                            "(p === p.ownerDocument.activeElement ? '^' : ''))")
                        print(f"DATETRACE {step}: {' / '.join(now)}", file=sys.stderr, flush=True)

            want = [full(digits, infos[i]["max"]) if i == mine else full(str(infos[i]["now"]), infos[i]["max"])
                    if str(infos[i]["now"]).isdigit() and int(infos[i]["now"]) > 0 else "" for i in range(count)]
            # the date's other parts given in this fill, typed in the same run as this one
            others = {i: re.sub(r"\D", "", str(given[infos[i]["id"]] or "")) for i in range(count)
                      if i != mine and given and infos[i]["id"] and infos[i]["id"] in given}
            filled = [full(others[i], infos[i]["max"]) if others.get(i) else want[i] for i in range(count)]
            # from the first part where every part before this one has a number; else from this part
            start = 0 if all(filled[:mine]) else mine
            # on to the last later part given, while none between is left empty
            last = max([mine] + [i for i, d in others.items() if d and i > mine])
            end = mine
            while end < last and filled[end + 1]:
                end += 1
            for i in range(start, end + 1):  # (only the parts typed now take what was given)
                want[i] = filled[i]
            await said(f"typing {digits} into part {mine} (want {want}, from part {start} to {end})")
            read_js = "el => el.getAttribute('aria-valuenow') || (el.value || '').replace(/\\D/g, '')"
            was = [str(info["now"]) for info in infos]  # (the date as it read before it was typed)

            async def type_run(slow: bool) -> list[Any]:
                for i in range(start, end + 1):
                    part = parts.nth(i)
                    display = part.locator("xpath=preceding-sibling::*[@aria-hidden='true'][1]")
                    if i == start and await display.count() and (
                            start > 0 or await part.evaluate(browser._ANOTHER_PART_CURRENT_JS)):
                        # a later part with an earlier one empty, or a date that marks another of its parts current
                        # (Intel's keeps it where it was when focus left the date, nightly live check Oct 11: the month
                        # typed after the year went into the year): Workday's display over it is pressed, which makes it
                        # the date's current part (focus alone leaves the current part as it is)
                        with contextlib.suppress(PlaywrightError, PlaywrightTimeout):
                            await display.click(timeout=2000)
                        await said(f"part {i}: its display pressed")
                    if not await part.evaluate("el => el === el.ownerDocument.activeElement"):
                        # Where a hint is drawn over the boxes (Workday's "MM/DD/YYYY" or each part's "MM"), a click
                        # never reaches them: focus takes the keys just the same. Checked first, so no click timeout
                        covered = await browser._covered(part)
                        if not covered:
                            try:
                                await part.click(timeout=2000)
                            except PlaywrightTimeout:
                                covered = True
                        if covered:
                            await part.focus(timeout=browser.FOCUS_TIMEOUT)
                    # Workday types into the part with focus, whichever its date marks current (a live trace of
                    # Intel's and Entegris', Oct 12: its arrows only moved focus away, wrapping to the year, so the month
                    # went into the year): no arrows, only this part focused before its digits
                    await said(f"part {i}: focused")
                    if slow:  # (the second time: a moment for the part to take its focus)
                        await page.wait_for_timeout(250)
                    if i == start:  # (the part as it was: its digits replaced, as Workday selects a part it's given)
                        await part.evaluate("el => { try { el.select(); } catch (e) {} }")
                    await page.keyboard.type(want[i], delay=80 if slow else 40)
                    await said(f"part {i}: typed {want[i]}")
                await loc.evaluate("el => el.ownerDocument.activeElement && el.ownerDocument.activeElement.blur()")
                await page.wait_for_timeout(100)
                await said("left the date")
                return [await parts.nth(i).evaluate(read_js) for i in range(count)]

            def off(read: list[Any]) -> list[int]:  # every part with an answer, not only those typed now
                return [i for i in range(count) if want[i] and not (str(read[i]).isdigit() and int(read[i]) == int(want[i]))]

            def number(x: Any) -> str:  # (as a number: "05" and "5" alike, "0" and "" alike)
                return str(int(x)) if str(x).isdigit() and int(x) else ""

            def strays(read: list[Any]) -> list[int]:  # parts not typed that don't read as they did before
                return [i for i in range(count) if not start <= i <= end and number(read[i]) != number(was[i])]

            if clear:
                # A date the page lost (`clear`: Tokyo Electron's Workday Self Identify, run 55: "current value is
                # 10/13/2026" under it, and "The field Date is required" all the same): typed again as it was, it's the
                # same date, and Workday passes a date on as focus leaves it only where it changed. Its parts typed
                # now are emptied first, key by key, and the date left; then typed
                for i in range(start, end + 1):
                    part = parts.nth(i)
                    with contextlib.suppress(PlaywrightError, PlaywrightTimeout):
                        await part.focus(timeout=browser.FOCUS_TIMEOUT)
                        for _ in range(5):  # (a part's digits, one at a time: never on into the part before it)
                            if not number(await part.evaluate(read_js)) or not await part.evaluate(
                                    "el => el === el.ownerDocument.activeElement"):
                                break
                            await page.keyboard.press("Backspace")
                            await page.wait_for_timeout(50)  # (the part's number read again once it's drawn)
                await loc.evaluate("el => el.ownerDocument.activeElement && el.ownerDocument.activeElement.blur()")
                await page.wait_for_timeout(150)
                await said("cleared and left")
            read = await type_run(False)
            self._date_typed |= {str(infos[i]["id"]) for i in range(start, end + 1) if infos[i]["id"]}
            wrong = off(read)
            if wrong and not strays(read):
                # once more, the same parts with the same digits, a little slower (HonorHealth's school year lost
                # the keys typed as it took focus, and read 1900: run 49); never where a part not typed has changed,
                # and taken only where none has after (never a year the first run left from a month alone: the
                # reviews')
                await said("not as typed: once more")
                read = await type_run(True)
                wrong = off(read) + strays(read)
        finally:
            with contextlib.suppress(PlaywrightError):
                await loc.evaluate("el => el.ownerDocument.querySelectorAll('[data-ja-date-part]').forEach((p) => {"
                                   " p.removeAttribute('data-ja-date-part'); p.removeAttribute('data-ja-date-mine'); })")
        if wrong:
            shown = "/".join(str(r) or "?" for r in read)
            raise ValueError(f"typed {digits}, but the date reads {shown}; check it in the browser")
        return "typed"

    async def _pick_date(self, page: Page, loc: Locator, field: dict, day: date) -> str:
        """A day in a date picker that opens a calendar (Eightfold's "Preferred Start Date", a combobox hinted "Select
        date" whose calendar is a modal dialog: the profile's words typed into it found nothing, and it was skipped,
        issue #112, note 41): the calendar opened, moved on a month at a time to the day's month (DATE_PICKER_MONTHS
        at most), and the day pressed, as a person picks one. The box must then show that day. A box that opens no
        calendar has the day typed, as any other search box has its answer."""
        frame = self._frame_for(page, field["id"])
        await frame.evaluate(DAY_CELL_JS,
                             [field["id"], "", True])  # (a dialog open already is no calendar it opens)
        await self._activate(loc)
        found = "none"
        for _ in range(browser.DATE_PICKER_MONTHS + 1):
            found = "none"
            for _ in range(10):  # (a calendar drawn a moment after the click)
                found = await frame.evaluate(DAY_CELL_JS, [field["id"], day.isoformat()])
                if found != "none":
                    break
                await page.wait_for_timeout(150)
            if found not in ("next", "back"):
                break
            await frame.locator("[data-ja-day-nav]").first.click(timeout=3000)
            await page.wait_for_timeout(150)
        if found == "none":
            return await self._type_and_pick(page, loc, field, day.strftime(date_form(field)))
        if found != "day":
            await self._close_pickers(page, self._own_frames(page, field["id"]), field["id"])
            raise ValueError(f"its calendar didn't offer {day:%m/%d/%Y} to pick"
                             + (" (its days have no dates to read)" if found == "unread" else ""))
        await frame.locator("[data-ja-day]").first.click(timeout=3000)
        shown = ""
        for _ in range(10):  # (the box showing the day a moment after the press)
            await page.wait_for_timeout(150)
            shown = (await loc.input_value(timeout=browser.FOCUS_TIMEOUT)).strip()
            if browser._shows_day(shown, day):
                return f"picked {shown}"
        raise ValueError(f"picked {day:%m/%d/%Y} in its calendar, but the box shows {shown[:40]!r}")


# browser.py's own names (its waits, page scripts, helpers and exceptions) are read from it as browser.<name> when a
# method runs: a test's patch of one (monkeypatch.setattr(browser, ...)) reaches the code here as it does browser.py's.
# Imported last: browser.py imports this module for BrowserSession's bases, and either may be imported first.
from . import browser  # noqa: E402
