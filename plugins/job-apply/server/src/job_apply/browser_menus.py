"""The lists a form's boxes open, for BrowserSession (a mixin): their choices read, one picked by a click or typed and
picked (inside a group it opens, too), one ticked in a multiselect, and the menus and pop-ups closed again."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from playwright.async_api import Error as PlaywrightError, Frame, Locator, Page, TimeoutError as PlaywrightTimeout

from .autofill import choose_option, is_empty_value, norm
from .formjs import (FIELD_OPTIONS_JS, MARK_OPTIONS_JS, OPEN_MENU_JS, OPEN_PICKERS_JS, OUTSIDE_CLICK_JS, SHOWN_VALUE_JS,
                     WORKDAY_CHOSEN_JS)


class MenusMixin:
    """BrowserSession's lists and menus."""

    if TYPE_CHECKING:  # (the session's own)
        _groups: dict[tuple[str, str], dict[str, str]]

        def _frame_for(self, page: Page, element_id: str) -> Frame: ...

        def _locator(self, page: Page, element_id: str) -> Locator: ...

    async def _activate(self, loc: Locator) -> None:
        """Click a dropdown, or focus it when an overlay covers it (react-select puts its
        placeholder on top of the input). Checking first avoids waiting out a click timeout."""
        covered = await browser._covered(loc)
        if not covered:
            try:
                await loc.click(timeout=2500)
                return
            except (PlaywrightError, PlaywrightTimeout):
                pass
        await loc.focus(timeout=browser.FOCUS_TIMEOUT)
        await loc.press("ArrowDown", timeout=browser.FOCUS_TIMEOUT)

    async def _field_options(self, page: Page, field_id: str, loc: Locator, wait_ms: int,
                             placeholder_ms: int = 0) -> list[str]:
        """This field's menu options, polling while the menu renders (and while it shows only its placeholder:
        BCBS of Arizona's and KLA's Workday Degree list, run 53, opened on "Select One" alone, its choices a moment
        after). `placeholder_ms`: how long a menu showing its placeholder alone is waited on, where that's longer
        (Boeing's veteran list, run 56: still "Select One" after two seconds); one showing nothing, `wait_ms`."""
        self._frame_for(page, field_id)  # fails clearly if the field's frame has gone
        waited = 0
        while True:
            try:
                options = await loc.evaluate(FIELD_OPTIONS_JS)
            except PlaywrightError:
                options = []
            if any(not is_empty_value(o) for o in options) or waited >= max(wait_ms, placeholder_ms if options else 0):
                return options
            await page.wait_for_timeout(150)
            waited += 150

    async def _results_for(self, page: Page, field_id: str, loc: Locator, query: str, wait_ms: int) -> list[str]:
        """The menu once it lists something with the words searched for, or [] after `wait_ms`."""
        waited = 0
        while waited < wait_ms:
            await page.wait_for_timeout(200)
            waited += 200
            options = await self._field_options(page, field_id, loc, 0)
            if any(norm(query) in norm(o) for o in options):
                return options
        return []

    async def _new_options(self, page: Page, field_id: str, loc: Locator, stale: list[str], wait_ms: int) -> list[str]:
        """The menu once it differs from `stale` (the list showing before a search), or
        whatever it shows after `wait_ms`."""
        waited, options = 0, stale
        while waited < wait_ms:
            await page.wait_for_timeout(150)
            waited += 150
            options = await self._field_options(page, field_id, loc, 0)
            if options and options != stale:
                break
        return options

    async def _open(self, page: Page, field_id: str, loc: Locator) -> None:
        await self._frame_for(page, field_id).evaluate(MARK_OPTIONS_JS)
        await self._activate(loc)

    async def _read_listbox_options(self, page: Page, field: dict) -> list[str]:
        loc = self._locator(page, field["id"])
        # (search pickers often show nothing until typed into)
        wait = browser.LIST_OPEN_WAIT if field["kind"] == "listbox" else browser.COMBO_OPEN_WAIT
        try:
            await self._open(page, field["id"], loc)
            # (a list's choices can take seconds after it opens on its placeholder alone: Boeing's Workday veteran list,
            # run 56, still "Select One" after two; _field_options stops waiting as soon as they come)
            options = await self._field_options(page, field["id"], loc, wait,
                                                 browser.LIST_PLACEHOLDER_WAIT if field["kind"] == "listbox" else 0)
        except (PlaywrightError, PlaywrightTimeout):
            options = []
        finally:
            # focus away first, Escape only if that didn't close it: on some widgets
            # Escape also clears the field
            await self._close_menus(page)
        return options

    async def _close_menus(self, page: Page) -> None:
        """Close a dropdown menu left open. An open menu catches the next field's clicks,
        covers the buttons under it, and on Eightfold keeps its field looking empty (the
        box is cleared for searching while the menu shows). Focus moves off first; Escape
        follows only if a menu is still open, since it can also close a dialog such as
        Easy Apply; a menu that ignores both (Eightfold's country lists) closes on a click
        on the page itself. A framed form's too (iCIMS's Phone Country Code, whose open list
        shows its search box, which reads as another field). A picker's dialog first (_close_pickers)."""
        await self._close_pickers(page)
        for frame in self._menu_frames(page):
            try:
                if not await frame.evaluate(OPEN_MENU_JS):
                    continue
                await frame.evaluate("() => { const a = document.activeElement; "
                                     "if (a && a !== document.body) a.blur(); }")
                await page.wait_for_timeout(150)
                if not await frame.evaluate(OPEN_MENU_JS):
                    continue
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(150)
                if await frame.evaluate(OPEN_MENU_JS):
                    await frame.evaluate(OUTSIDE_CLICK_JS)
            except PlaywrightError:
                continue

    def _own_frames(self, page: Page, element_id: str) -> list[Frame]:
        """The frame a box is in, and the page itself (where a framed form's pop-ups may be drawn)."""
        try:
            frame = self._frame_for(page, element_id)
        except Exception:  # (its frame gone: the page's alone)
            return [page.main_frame]
        return [frame] if frame is page.main_frame else [frame, page.main_frame]

    @staticmethod
    def _menu_frames(page: Page) -> list[Frame]:
        """The page and its frames a form may be in: never a blank or detached one, nor a CAPTCHA's own."""
        return [frame for frame in page.frames
                if frame is page.main_frame or not (frame.is_detached() or not frame.url or frame.url == "about:blank"
                                                    or browser._CAPTCHA_FRAME.match(frame.url))]

    async def _close_pickers(self, page: Page, frames: list[Frame] | None = None, field_id: str = "") -> None:
        """Close a box's own pop-up left open as a dialog over the page (OPEN_PICKERS_JS; `field_id`: the box being
        filled, which may have opened it): Eightfold's date picker opens its calendar as a modal dialog that keeps the
        focus, and swallows the next box's click and keys, so its Country of Residence took nothing typed and timed
        out (issue #112, note 41). As a menu is: the focus taken off first (on some boxes an Escape clears them too);
        then Escape, with the focus moved into it; then on the box that opened it; each time a look that it's gone;
        and a click beside it last. Nothing while a notice is open over the page. Never raises: a box that can't be
        filled after says so itself."""
        for frame in frames or self._menu_frames(page):
            try:
                if not await frame.evaluate(OPEN_PICKERS_JS, ["", field_id]):
                    continue
                for attempt in ("blur", "inside", "opener", "beside"):
                    # (each only while it's still open and no notice is: looked at again first)
                    if not await frame.evaluate(OPEN_PICKERS_JS,
                                                ["" if attempt == "beside" else attempt, field_id]):
                        break
                    if attempt == "beside":
                        await frame.evaluate(OUTSIDE_CLICK_JS)
                    elif attempt != "blur":
                        await page.keyboard.press("Escape")
                    gone = False
                    for _ in range(2 if attempt == "blur" else 4):
                        await page.wait_for_timeout(100)
                        if gone := not await frame.evaluate(OPEN_PICKERS_JS, ["", field_id]):
                            break
                    if gone:
                        break
            except (PlaywrightError, PlaywrightTimeout):
                continue

    async def _click_option(self, page: Page, field_id: str, text: str) -> None:
        frame = self._frame_for(page, field_id)
        target = frame.locator(f'[data-ja-opt="{browser._css_string(text)}"]').first
        if not await target.count():  # the menu re-rendered after it was read
            opt = frame.locator('[role="option"]:visible, [role="grid"] '
                                '[role="gridcell"]:visible').filter(has_text=text)
            exact = opt.filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$"))
            target = exact.first if await exact.count() else opt.first
        try:
            lifted = await target.evaluate(browser._THROUGH_BARS_JS)
        except PlaywrightError:
            lifted = 0
        try:
            await target.click(timeout=3000)
        except PlaywrightTimeout:
            # Something sits over the menu; send the events to the option itself.
            await target.evaluate(
                "el => ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach("
                "t => el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})))"
            )
        finally:
            if lifted:
                try:
                    await frame.evaluate(browser._BARS_BACK_JS)
                except PlaywrightError:
                    pass

    async def _pick_from_listbox(self, page: Page, loc: Locator, field: dict, value: Any) -> str:
        names = bool(field.get("names"))
        if await loc.evaluate(browser.TWIN_TICKED_JS) is not None:
            return await self._tick_in_multiselect(page, loc, field, value)
        for attempt in range(2):
            await self._open(page, field["id"], loc)
            options = await self._field_options(page, field["id"], loc, browser.PICK_OPEN_WAIT)
            if not options:  # its menu didn't open (Onto's Phone Device Type, once): close up and try again
                await self._close_menus(page)
                await self._open(page, field["id"], loc)
                options = await self._field_options(page, field["id"], loc, browser.LIST_REOPEN_WAIT)
            choice = choose_option(value, options, names=names)
            if choice is None and not field.get("pick_only"):
                # Long lists are virtualized; typing jumps to the entry.
                await page.keyboard.type(str(value), delay=40)
                await page.wait_for_timeout(400)
                options = await self._field_options(page, field["id"], loc, browser.LIST_JUMP_WAIT)
                choice = choose_option(value, options, names=names)
            if choice is None:
                await page.keyboard.press("Escape")
                raise ValueError(f"{value!r} doesn't match any option: {options[:30]}")
            await self._click_option(page, field["id"], choice)
            try:
                await self._confirm_choice(page, loc, choice)
            except ValueError:
                # a choice that opened a list of its own (Workday's "How Did You Hear About Us?": "<employer>
                # website ›" > "<employer> website", live test 2, issue #112, note 15): picked inside it
                inner = await self._new_options(page, field["id"], loc, options, browser.LIST_NEW_WAIT)
                entry = browser._inner_choice(value, choice, inner, names) if inner and inner != options else None
                if entry is not None:
                    await self._click_option(page, field["id"], entry)
                    await self._confirm_choice(page, loc, entry)
                    return f"selected {browser._bare_choice(choice)} > {entry}"
                if attempt:
                    raise
                await self._close_menus(page)  # a pick that didn't land: once more, from a menu opened afresh
                continue
            return f"selected {choice}"
        raise AssertionError("unreachable")

    async def _tick_in_multiselect(self, page: Page, loc: Locator, field: dict, value: Any) -> str:
        """A jQuery UI multiselect (BrassRing's "Please check each applicable box", Edward Jones' run 87): its button
        reads "Choose..." whatever is ticked, and a click on a choice ticks it or unticks it. The choice is clicked
        once, only where it isn't ticked already (by what the widget lists as ticked, or its own row's tick), and
        taken where it then shows ticked; never clicked again, which would untick it. BrassRing's search box for
        several choices (run 88) works alike: its list opens on the Down key and shows a page of 15, its choices known
        only once it's open; an answer that page doesn't show, or shows only a near neighbour of ("South Asian" for
        "Asian": the review's), is searched for, and the exact one taken. Oracle's cx-multi-select too (MSTS' question
        7, run 144): its choices ticked show as pills under its box, and it sets its answer only as its list closes
        (closed here after the tick, the focus taken off it)."""
        names = bool(field.get("names"))
        options = field.get("options") or []
        choice = choose_option(value, options, names=names) if options else None
        if options and choice is None:
            raise ValueError(f"{value!r} doesn't match any option: {options[:30]}")
        if choice is not None and browser._among(choice, await loc.evaluate(browser.TWIN_TICKED_JS) or []):
            return f"selected {choice} (ticked already)"
        search = await loc.evaluate("el => el.tagName === 'INPUT'")
        want = str(choice if choice is not None else value)
        try:
            shown = await self._open_ticks(page, field["id"], loc, search)
            pick = choose_option(want, shown, names=names)
            if search and (pick is None or (norm(pick) != norm(want) and len(shown) >= browser._SEARCH_PAGE)):
                await loc.fill("")
                await loc.press_sequentially(browser._search_words(want), delay=30)
                # (a filter that waits a moment)
                found = await self._new_options(page, field["id"], loc, shown, browser.LIST_FILTER_WAIT)
                # the narrowed list judges better than the first page: its exact one, else its nearest
                better = next((o for o in found if norm(o) == norm(want)), None) \
                    or (choose_option(want, found, names=names) if found else None)
                if better is not None:
                    pick = better
                elif pick is not None:  # nothing nearer: its first page's, from the list opened afresh
                    await loc.fill("")
                    shown = await self._open_ticks(page, field["id"], loc, search)
                    if pick not in shown:
                        raise ValueError(f"{pick!r} left its list when it opened again; tick it in "
                                         "the desk's browser window")
            if pick is None:
                if search:
                    await self._clear_search(page, loc)  # (its typed words aren't an answer)
                raise ValueError(f"{value!r} doesn't match any option: {shown[:30]}")
            # (its row's tick alone isn't enough before a click: only where the widget's own list has it too)
            ticked = await loc.evaluate(browser.TWIN_TICKED_JS) or []
            if browser._among(pick, ticked) or (await self._row_ticked(page, field["id"], pick)
                                        and any(norm(t) in norm(pick) or norm(pick) in norm(t) for t in ticked
                                                if norm(t))):
                return f"selected {pick} (ticked already)"
            await self._click_option(page, field["id"], pick)
            for _ in range(8):
                await page.wait_for_timeout(150)
                if (browser._among(pick, await loc.evaluate(browser.TWIN_TICKED_JS) or [])
                        or await self._row_ticked(page, field["id"], pick)):
                    return f"selected {pick}"
        finally:
            await self._close_menus(page)  # (its menu stays open after a tick)
        raise ValueError(f"Ticked {pick!r} but the list doesn't have it ticked; tick it in the desk's browser window")

    async def _open_ticks(self, page: Page, field_id: str, loc: Locator, search: bool) -> list[str]:
        """Open a multiselect's list (a search box's on the Down key: a click only puts the cursor in it) and read
        its choices; once more from closed if it showed none."""
        await self._open(page, field_id, loc)
        shown = await self._field_options(page, field_id, loc, browser.PICK_OPEN_WAIT)
        if not shown and search and not await loc.input_value():
            await loc.press("ArrowDown")
            shown = await self._field_options(page, field_id, loc, browser.LIST_REOPEN_WAIT)
        if not shown:
            # its menu didn't open (the click closed one already open): closed, and opened once more
            await self._close_menus(page)
            await self._open(page, field_id, loc)
            if search:
                await loc.press("ArrowDown")
            shown = await self._field_options(page, field_id, loc, browser.LIST_REOPEN_WAIT)
            if not shown:
                raise ValueError("its list of choices didn't open; tick it in the desk's browser window")
        return shown

    async def _row_ticked(self, page: Page, field_id: str, text: str) -> bool:
        """Does this choice's own row in the open list show it ticked (BrassRing's pseudo-checkbox)?"""
        row = self._frame_for(page, field_id).locator(f'[data-ja-opt="{browser._css_string(text)}"]').first
        try:
            return bool(await row.count()) and bool(await row.evaluate(browser._ROW_TICKED_JS,
                                                                       timeout=browser.FOCUS_TIMEOUT))
        except (PlaywrightError, PlaywrightTimeout):
            return False

    async def _confirm_choice(self, page: Page, loc: Locator, choice: str) -> None:
        """After picking from a menu, the field must show that choice (else the click
        landed in some other menu, or the widget refused it)."""
        await page.wait_for_timeout(150)
        pills = await loc.evaluate(WORKDAY_CHOSEN_JS)
        if pills is not None:
            # A Workday prompt shows what it took as a pill, so a missing one is a miss for sure
            # (the entry was a group that opened its own list, or the pick hadn't landed yet).
            for _ in range(8):
                if any(norm(choice) in norm(p) or choose_option(choice, [p]) for p in pills):
                    return
                await page.wait_for_timeout(200)
                pills = await loc.evaluate(WORKDAY_CHOSEN_JS) or []
            raise ValueError(f"Picked {choice!r} but the field didn't take it"
                             + (f" (it shows {', '.join(pills)[:80]!r})" if pills else ""))
        shown = await loc.evaluate(SHOWN_VALUE_JS)
        # Only positive evidence counts: some widgets display the value where we can't see it,
        # and search text left in the box ("C" for "Choose not to disclose") isn't a choice.
        if (shown and norm(choice) not in norm(shown) and not norm(choice).startswith(norm(shown))
                and choose_option(choice, [shown]) is None):
            raise ValueError(f"Picked {choice!r} but the field shows {shown[:80]!r}; set it by hand or with click")

    async def _list_loading(self, loc: Locator) -> bool:
        """Is this box's list open with nothing in it yet (its menu named by aria-owns or aria-controls, shown and
        without a choice; or the box saying it's expanded)?"""
        try:
            return bool(await loc.evaluate(browser._LIST_LOADING_JS, timeout=browser.FOCUS_TIMEOUT))
        except (PlaywrightError, PlaywrightTimeout):
            return False

    async def _reopen(self, page: Page, field_id: str, loc: Locator) -> bool:
        """Open a pick-only list once more, by a click alone: never an ArrowDown, which on some lists changes the value
        while closed (the review's). False when it won't take a click (not opened again)."""
        try:
            await self._frame_for(page, field_id).evaluate(MARK_OPTIONS_JS)
            await loc.click(timeout=2500)
            return True
        except (PlaywrightError, PlaywrightTimeout):
            return False

    async def _list_open(self, loc: Locator) -> bool:
        try:
            return bool(await loc.evaluate(browser._LIST_OPEN_JS, timeout=browser.FOCUS_TIMEOUT))
        except (PlaywrightError, PlaywrightTimeout):
            return True  # (can't tell: not opened again, which could close it)

    async def _clear_search(self, page: Page, loc: Locator) -> None:
        """Empty a picker's box after a search that found nothing to pick. SuccessFactors keeps
        the typed words, and a box with words in it reads as answered: the desk went on to the
        submit step while the site still said the question was required."""
        try:
            if await loc.input_value():
                await loc.fill("")
                await loc.evaluate("el => el.blur()")
        except (PlaywrightError, PlaywrightTimeout):
            pass
        await self._close_menus(page)  # after: emptying the box can bring its menu back

    async def _type_and_pick(self, page: Page, loc: Locator, field: dict, value: Any) -> str:
        text, names = str(value), bool(field.get("names"))
        await self._open(page, field["id"], loc)
        # Pick straight from the menu when the answer is in it: a short list (Yes / No), or
        # the exact entry in a long one. Typing is only for search pickers and long lists,
        # and typed keys can land in another field (Micron's ended up with "ona", the end
        # of "Arizona", in the question below the State).
        pick_only = bool(field.get("pick_only"))
        # (a self-identification list shows every choice on opening, a moment later on some sites)
        options = await self._field_options(page, field["id"], loc,
                                             browser.PICK_OPEN_WAIT if pick_only else browser.COMBO_OPEN_WAIT)

        def no_choice(listed: list[str]) -> bool:  # (nothing, or its placeholder alone: "No Selection")
            return not any(not is_empty_value(o) for o in listed)

        async def choices_come(listed: list[str]) -> list[str]:
            # SuccessFactors' paginated selects fetch their entries when first opened, a few seconds on some servers
            # (Freeport-McMoRan's Ethnicity, Edwards Vacuum's Protected Veteran, Rogers' Gender and Ethnicity: the
            # desk read no choices and asked the person, 20 runs): a list open and still empty, or on its placeholder
            # alone, is waited on
            waited = browser.PICK_OPEN_WAIT  # (waited already, as the list was read)
            while no_choice(listed) and waited < browser.PICK_LIST_WAIT and (listed or await self._list_loading(loc)):
                await page.wait_for_timeout(500)
                waited += 500
                listed = await self._field_options(page, field["id"], loc, 0)
            return listed

        if pick_only and no_choice(options):
            if not await self._list_open(loc) and await self._reopen(page, field["id"], loc):
                # It opened and closed again by itself before its choices were read (Edwards Vacuum's Protected
                # Veteran, run 114: its five choices shown half a second after the click, the list gone again by two;
                # opened a second time, they stay; its box owns the list, so its absence read as still loading and was
                # waited on in vain): opened once more, and read as the first time. (A site still fetching, its list
                # not drawn yet, takes the second click as the first: run 111's, the fixture's &nolist)
                options = await self._field_options(page, field["id"], loc, browser.PICK_OPEN_WAIT)
            options = await choices_come(options)
        choice = (browser._choose(text, options, field, exact_only=len(options) > browser.SHORT_MENU and not pick_only)
                  if options else None)
        groups_key = browser._groups_key(page.url, field)
        group = self._groups.get(groups_key, {}).get(norm(text)) if choice is None and options and groups_key else None
        top = choose_option(group, options, exact_only=True) if group is not None else None
        if top is not None:
            # An entry inside a group, the person's pick from the entries the group opened before (Hitachi's
            # "Career Website" > "Hitachi Digital Career Site (…)", run 112: a search for the entry found nothing on
            # the site, and the person's answer never went in): the group picked, then the entry
            await self._click_option(page, field["id"], top)
            inner = await self._new_options(page, field["id"], loc, options, browser.LIST_NEW_WAIT)
            if inner and inner != options:
                entry = choose_option(text, inner, exact_only=True)
                if entry is not None:
                    await self._click_option(page, field["id"], entry)
                    await self._confirm_choice(page, loc, entry)
                    return f"selected {top} > {entry}"
                await self._close_menus(page)
                raise browser.PickedAGroup(top, inner)  # (its entries now, to ask about)
            # (the group no longer opens a list of its own: whatever it did is said, never taken for the answer)
            await self._close_menus(page)
            raise ValueError(f"{top!r} no longer opens its entries, so {text!r} wasn't picked; check this list")
        query = browser._search_words(text)
        if choice is None and pick_only:
            # Its answer in the site's words or not at all: the profile's own words typed in
            # ("Decline to self-identify") find nothing in "Female" / "Male", nor in a list whose
            # decline is worded another way, and the box is left with them or empty
            await self._close_menus(page)
            raise ValueError(f"{text!r} doesn't match any option: {options[:30]}" if not no_choice(options)
                             else "its list showed no choices")
        if choice is None:
            opened = options
            await loc.fill("")
            await loc.press_sequentially(query, delay=30)
            options = await self._field_options(page, field["id"], loc, browser.LIST_FILTER_WAIT)
            if options and options == opened and not await loc.evaluate(browser.WORKDAY_PROMPT_JS):
                # still the list from before the search (Oracle's ZIP lists "00000, …" on opening):
                # its results come a moment later
                options = await self._results_for(page, field["id"], loc, query, browser.LIST_SEARCH_WAIT) or options
            # Search-style pickers (Workday) list results after Enter: nothing listed yet, or
            # (Workday's 2026 prompts) the categories it opened with, untouched by typing.
            # Inside a <form>, Enter could submit the whole form, so it's never pressed there.
            unsearched = not options or (options == opened and await loc.evaluate(browser.WORKDAY_PROMPT_JS))
            if unsearched and not await loc.evaluate("el => !!el.form"):
                await loc.press("Enter")
                options = await self._new_options(page, field["id"], loc, options, browser.LIST_FILTER_WAIT)
            choice = browser._choose(text, options, field)
        if choice is None:
            if options:
                await self._clear_search(page, loc)
                raise browser.NoMatch(text, options)
            # Nothing was listed. A pick-list (one that names its menu) needs a pick: Eightfold's
            # Country code dropped the typed text once its menu closed, and the field stayed
            # empty while counting as filled. So does a Workday prompt, whose value is only ever
            # a pill (a school its list doesn't have read as filled with the typed name). Only a
            # free-text box may keep what was typed.
            # Unless the search chose it: Workday chooses the one entry its search finds at once, and closes the
            # list (live probe 45, Oct 13), its pill the answer itself, word for word ("C" isn't "C#": the review's)
            said = " ".join(text.split()).casefold()
            took = next((p for p in await loc.evaluate(WORKDAY_CHOSEN_JS) or []
                         if " ".join(p.split()).casefold() == said), None)
            if took is not None:
                await self._clear_search(page, loc)  # (nothing typed left in its box)
                return f"selected {took}"
            picks = (await loc.evaluate("el => !!(el.getAttribute('aria-controls') || el.getAttribute('aria-owns'))")
                     or await loc.evaluate(browser.WORKDAY_PROMPT_JS)
                     # (or one whose list said it was empty, "No results found": a list to pick from, the review's)
                     or await loc.evaluate("el => el.hasAttribute('data-ja-empty')"))
            await loc.evaluate("el => el.blur()")
            await page.wait_for_timeout(200)
            if picks or norm(await loc.input_value()) != norm(query):
                await self._clear_search(page, loc)
                raise ValueError(f"nothing in its list matched {query!r}")
            return "typed (no suggestions appeared)"
        await self._click_option(page, field["id"], choice)
        try:
            await self._confirm_choice(page, loc, choice)
        except ValueError:
            inner = await self._new_options(page, field["id"], loc, options, browser.LIST_NEW_WAIT)
            entry = browser._inner_choice(text, choice, inner, names) if inner and inner != options else None
            if entry is not None:  # the answer is in the group it opened (Onto's "ONTO Website" > "ONTO Website")
                await self._click_option(page, field["id"], entry)
                await self._confirm_choice(page, loc, entry)
                return f"selected {choice} > {entry}"
            await self._close_menus(page)
            if inner and inner != options:
                if groups_key:
                    known = self._groups.setdefault(groups_key, {})
                    if len(known) + len(inner) <= 500:
                        known.update({norm(e): choice for e in inner})
                raise browser.PickedAGroup(choice, inner) from None
            raise
        return f"selected {choice}"


# browser.py's own names (its waits, page scripts, helpers and exceptions) are read from it as browser.<name> when a
# method runs: a test's patch of one (monkeypatch.setattr(browser, ...)) reaches the code here as it does browser.py's.
# Imported last: browser.py imports this module for BrowserSession's bases, and either may be imported first.
from . import browser  # noqa: E402
