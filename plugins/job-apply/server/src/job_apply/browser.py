"""A real, visible browser the applicant shares with Claude.

The browser uses a persistent profile in ~/.job-apply/browser, so sign-ins to
LinkedIn, Indeed and company career sites survive between sessions. The person
can watch every step and take over at any time.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Frame,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from . import config
from .autofill import choose_option, is_empty_value, norm, polarity
from .formjs import (CLICK_CHOICE_JS, COVERED_JS, ELEMENT_INFO_JS, ENTRIES_JS, EXTRACT_JS, FIELD_OPTIONS_JS,
                     MARK_OPTIONS_JS, OPEN_MENU_JS, QUIET_JS, SHOWN_VALUE_JS, VISIBLE_TEXT_JS)

SUBMIT_RE = re.compile(r"\bsubmit\b|send (my )?application|finish (my )?application|complete (my )?application", re.I)
# A form's own submit button with one of these labels is the final step too ("Apply", "Send").
FINALISH_RE = re.compile(r"^(apply( now)?|send( now)?|finish|complete( application)?|confirm( and send)?)$", re.I)
# Form buttons that only move between steps; in a dry run every other form submit is refused.
NAVIGATION_RE = re.compile(
    r"^(next|continue|save( and| &)? continue|save( for later| draft)?|review|back|previous|add( another)?|search|"
    r"sign ?in|log ?in|create account|verify|send (me a )?code|ok|accept( all)?( cookies)?|i agree|apply manually|start)\b",
    re.I,
)
# How long a click may wait for its button to become clickable, in ms.
CLICK_TIMEOUT = 8000
CONFIRMATION_RE = re.compile(
    r"thank you for (applying|your application|your interest)|application (has been |was )?(submitted|received|complete)"
    r"|we('ve| have) received your application|successfully (submitted|applied)|your application is (in|on its way)",
    re.I,
)


def _css_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class BrowserUnavailable(Exception):
    pass


UNAVAILABLE_HELP = (
    "Could not start a browser. Install Google Chrome, or run "
    "`uv run --project <plugin>/server playwright install chromium`. Details: "
)


def launch_attempts(settings: config.Settings) -> list[dict[str, Any]]:
    """Browser choices to try in order: an explicit executable, the installed
    Chrome/Edge, then Playwright's bundled Chromium."""
    exe = os.environ.get("JOB_APPLY_CHROMIUM_PATH")
    if exe:
        return [{"executable_path": exe}]
    attempts: list[dict[str, Any]] = []
    if settings.browser_channel in ("chrome", "msedge", "chrome-beta"):
        attempts.append({"channel": settings.browser_channel})
    attempts.append({})
    return attempts


class SubmitBlocked(Exception):
    pass


class BrowserSession:
    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._background = False  # True while a helper tab is open that shouldn't become current
        self._frame_ids: dict[Frame, str] = {}
        self._fields: dict[str, dict] = {}
        self._actions: dict[str, dict] = {}
        self.current_job_id: int | None = None

    # ---------------------------------------------------------------- lifecycle
    @property
    def is_open(self) -> bool:
        return self._ctx is not None

    async def _launch(self) -> None:
        settings = config.Profile.load().settings
        user_dir = config.browser_profile_dir()
        user_dir.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        kwargs: dict[str, Any] = {
            "user_data_dir": str(user_dir),
            "headless": settings.headless,
            "accept_downloads": True,
            "no_viewport": not settings.headless,
        }
        if settings.headless:
            kwargs["viewport"] = {"width": 1280, "height": 900}
        errors = []
        for extra in launch_attempts(settings):
            try:
                self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs, **extra)
                break
            except PlaywrightError as e:
                errors.append(f"{extra or 'bundled chromium'}: {str(e).splitlines()[0]}")
        if self._ctx is None:
            await self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(UNAVAILABLE_HELP + " | ".join(errors))
        self._ctx.set_default_timeout(15000)  # a vanished element fails in 15 s, not 30
        self._ctx.on("page", self._on_new_page)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()

    def _on_new_page(self, page: Page) -> None:
        # "Apply" buttons often open the company site in a new tab; follow it.
        if not self._background:
            self._page = page

    async def page(self) -> Page:
        if self._ctx is None:
            await self._launch()
        assert self._ctx is not None
        if self._page is None or self._page.is_closed():
            live = [p for p in self._ctx.pages if not p.is_closed()]
            self._page = live[-1] if live else await self._ctx.new_page()
        return self._page

    async def new_tab(self) -> Page:
        """Open a tab and make it the one the tools act on (the Job Desk gives each job its own)."""
        async with self._lock:
            await self.page()
            assert self._ctx is not None
            self._page = await self._ctx.new_page()
            return self._page

    def use_tab(self, page: Page | None) -> bool:
        """Act on this tab from now on; False if it has been closed."""
        if page is None or page.is_closed() or self._ctx is None:
            return False
        self._page = page
        return True

    @property
    def current_tab(self) -> Page | None:
        return self._page if self._page is not None and not self._page.is_closed() else None

    async def close(self) -> None:
        if self._ctx is not None:
            try:
                await self._ctx.close()
            except PlaywrightError:
                pass
        if self._pw is not None:
            await self._pw.stop()
        self._ctx = self._pw = self._page = None
        self._fields.clear()
        self._actions.clear()
        self._frame_ids.clear()

    # ---------------------------------------------------------------- navigation
    async def goto(self, url: str) -> dict[str, Any]:
        async with self._lock:
            page = await self.page()
            failure = ""
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PlaywrightTimeout:
                pass
            except PlaywrightError as e:  # DNS failure, blocked host, refused connection...
                failure = str(e).splitlines()[0][:300]
            await self._settle(page)
            summary = await self._summary(page)
            if failure:
                summary["navigation_error"] = failure
            return summary

    async def _settle(self, page: Page, timeout: int = 6000) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeout:
            pass
        try:
            # Oracle and Workday keep drawing the next step after the network goes quiet
            await page.evaluate(QUIET_JS, [350, 2000])
        except PlaywrightError:
            await page.wait_for_timeout(300)  # it navigated meanwhile

    async def tabs(self, switch_to: int | None = None) -> dict[str, Any]:
        async with self._lock:
            await self.page()
            assert self._ctx is not None
            pages = [p for p in self._ctx.pages if not p.is_closed()]
            if switch_to is not None:
                if not 0 <= switch_to < len(pages):
                    raise ValueError(f"No tab {switch_to}; there are {len(pages)}")
                self._page = pages[switch_to]
                await self._page.bring_to_front()
            out = []
            for i, p in enumerate(pages):
                try:
                    title = await p.title()
                except PlaywrightError:
                    title = ""
                out.append({"index": i, "url": p.url, "title": title, "active": p is self._page})
            return {"tabs": out}

    # ---------------------------------------------------------------- reading
    def _frame_prefix(self, frame: Frame, page: Page) -> str:
        if frame is page.main_frame:
            return ""
        if frame not in self._frame_ids:
            self._frame_ids[frame] = f"f{len(self._frame_ids) + 1}-"
        return self._frame_ids[frame]

    def _frame_for(self, page: Page, element_id: str) -> Frame:
        m = re.match(r"(f\d+-)", element_id)
        if not m:
            return page.main_frame
        for frame, prefix in self._frame_ids.items():
            if prefix == m.group(1) and not frame.is_detached():
                return frame
        raise KeyError(f"The frame holding {element_id} is gone; call inspect_form again")

    async def _extract(self, page: Page) -> dict[str, Any]:
        result: dict[str, Any] = {"fields": [], "actions": [], "errors": [], "headings": []}
        for frame in page.frames:
            if frame.is_detached():
                continue
            if frame is not page.main_frame and (not frame.url or frame.url == "about:blank"):
                continue
            try:
                data = await frame.evaluate(EXTRACT_JS, self._frame_prefix(frame, page))
            except PlaywrightError:
                continue  # cross-origin frame that refused, or navigated mid-read
            for key in result:
                result[key].extend(data.get(key, []))
        self._fields = {f["id"]: f for f in result["fields"]}
        self._actions = {a["id"]: a for a in result["actions"]}
        return result

    async def _summary(self, page: Page) -> dict[str, Any]:
        data = await self._extract(page)
        fields = data["fields"]
        return {
            "url": page.url,
            "title": await page.title(),
            "headings": data["headings"],
            "fields": len(fields),
            "empty_required": sum(1 for f in fields if f.get("required") and is_empty_value(f.get("value"))),
            "actions": [a["text"] for a in data["actions"]][:25],
            "errors": data["errors"],
        }

    async def _activate(self, loc: Locator) -> None:
        """Click a dropdown, or focus it when an overlay covers it (react-select puts its
        placeholder on top of the input). Checking first avoids waiting out a click timeout."""
        covered = await loc.evaluate(COVERED_JS)
        if not covered:
            try:
                await loc.click(timeout=2500)
                return
            except (PlaywrightError, PlaywrightTimeout):
                pass
        await loc.focus()
        await loc.press("ArrowDown")

    async def _field_options(self, page: Page, field_id: str, loc: Locator, wait_ms: int) -> list[str]:
        """This field's menu options, polling while the menu renders."""
        self._frame_for(page, field_id)  # fails clearly if the field's frame has gone
        waited = 0
        while True:
            try:
                options = await loc.evaluate(FIELD_OPTIONS_JS)
            except PlaywrightError:
                options = []
            if options or waited >= wait_ms:
                return options
            await page.wait_for_timeout(150)
            waited += 150

    async def _open(self, page: Page, field_id: str, loc: Locator) -> None:
        await self._frame_for(page, field_id).evaluate(MARK_OPTIONS_JS)
        await self._activate(loc)

    async def _read_listbox_options(self, page: Page, field: dict) -> list[str]:
        loc = self._locator(page, field["id"])
        wait = 2000 if field["kind"] == "listbox" else 900  # search pickers often show nothing until typed into
        try:
            await self._open(page, field["id"], loc)
            options = await self._field_options(page, field["id"], loc, wait)
        except (PlaywrightError, PlaywrightTimeout):
            options = []
        finally:
            await page.keyboard.press("Escape")
            await self._close_menus(page)
        return options

    async def _close_menus(self, page: Page) -> None:
        """Close a dropdown menu left open, which would catch the next field's clicks. Focus
        moves off first (some menus, like Eightfold's, ignore Escape); Escape only follows
        if a menu is still open, since it can also close a dialog such as Easy Apply."""
        try:
            if not await page.evaluate(OPEN_MENU_JS):
                return
            await page.evaluate("() => { const a = document.activeElement; if (a && a !== document.body) a.blur(); }")
            await page.wait_for_timeout(150)
            if await page.evaluate(OPEN_MENU_JS):
                await page.keyboard.press("Escape")
        except PlaywrightError:
            pass

    async def inspect(self, include_dropdown_options: bool = True) -> dict[str, Any]:
        async with self._lock:
            page = await self.page()
            data = await self._extract(page)
            for _ in range(5):  # Oracle can draw the form a moment after the rest of the page
                if data["fields"]:
                    break
                await page.wait_for_timeout(500)
                data = await self._extract(page)
            if include_dropdown_options:
                for f in data["fields"]:
                    if f["kind"] in ("listbox", "combobox") and not f.get("options") and not f.get("disabled"):
                        f["options"] = await self._read_listbox_options(page, f)
                        self._fields[f["id"]] = f
            return {"url": page.url, "title": await page.title(), **data}

    async def visible_text(self, max_chars: int = 8000) -> str:
        async with self._lock:
            page = await self.page()
            parts = []
            for frame in page.frames:
                try:
                    t = await frame.evaluate(VISIBLE_TEXT_JS)
                except PlaywrightError:
                    continue
                if t:
                    parts.append(t)
            return "\n\n".join(parts)[:max_chars]

    async def html(self) -> str:
        async with self._lock:
            page = await self.page()
            return await page.content()

    async def capture_json(self, url: str, url_part: str, timeout: int = 25000,
                           want: Callable[[Any], bool] | None = None,
                           rewrite: Callable[[Any], Any] | None = None) -> Any:
        """Open `url` in a background tab and return the JSON of the first response whose URL
        contains `url_part`: the data a careers page loads for itself, when its API refuses
        direct requests. `want` picks among several such calls by their JSON request body;
        `rewrite` may change that body on its way out (e.g. a bigger page size)."""
        def body_of(request: Any) -> Any:
            try:
                return json.loads(request.post_data or "null")
            except (ValueError, TypeError):
                return None

        async with self._lock:
            await self.page()
            assert self._ctx is not None
            self._background = True
            tab = await self._ctx.new_page()
            try:
                if rewrite is not None:
                    async def handle(route: Any) -> None:
                        changed = rewrite(body_of(route.request))
                        if changed is None:
                            await route.continue_()
                        else:
                            await route.continue_(post_data=json.dumps(changed))
                    await tab.route(lambda u: url_part in u, handle)

                def matches(r: Any) -> bool:
                    return url_part in r.url and r.ok and (want is None or bool(want(body_of(r.request))))

                async with tab.expect_response(matches, timeout=timeout) as info:
                    await tab.goto(url, wait_until="domcontentloaded", timeout=45000)
                return await (await info.value).json()
            finally:
                await tab.close()
                self._background = False

    async def background_html(self, url: str) -> str:
        """The HTML of `url`, read in a background tab so the tab an application is in
        stays where it is (the Job Desk adds pasted links while it applies)."""
        async with self._lock:
            await self.page()
            assert self._ctx is not None
            self._background = True
            tab = await self._ctx.new_page()
            try:
                await tab.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    await tab.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeout:
                    pass  # pages that keep polling: what's drawn by now is enough
                return await tab.content()
            finally:
                await tab.close()
                self._background = False

    async def snapshot(self, dest: Path, note: str = "", details: Any = None) -> Path:
        """Save what's needed to debug a page later: HTML of every frame, a screenshot
        and the extracted fields. Stays on the user's machine."""
        async with self._lock:
            page = await self.page()
            dest.mkdir(parents=True, exist_ok=True)
            data = await self._extract(page)
            frames = []
            for i, frame in enumerate(f for f in page.frames if not f.is_detached()):
                name = "page.html" if frame is page.main_frame else f"frame-{i}.html"
                try:
                    (dest / name).write_text(await frame.content(), encoding="utf-8")
                    frames.append({"file": name, "url": frame.url})
                except PlaywrightError:
                    continue
            try:
                (dest / "screenshot.jpg").write_bytes(
                    await page.screenshot(type="jpeg", quality=60, full_page=True)
                )
            except PlaywrightError:
                pass
            meta = {"url": page.url, "title": await page.title(), "note": note, "frames": frames,
                    "details": details, **data}
            (dest / "snapshot.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
            return dest

    async def screenshot(self, full_page: bool = False, save_to: Path | None = None) -> bytes:
        async with self._lock:
            page = await self.page()
            data = await page.screenshot(type="jpeg", quality=60, full_page=full_page)
        if save_to:
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_bytes(data)
        return data

    # ---------------------------------------------------------------- writing
    def _locator(self, page: Page, element_id: str) -> Locator:
        frame = self._frame_for(page, element_id)
        return frame.locator(f'[data-ja-id="{element_id}"]').first

    async def _field(self, page: Page, field_id: str) -> dict:
        if field_id not in self._fields:
            await self._extract(page)
        if field_id not in self._fields:
            raise KeyError(f"No field {field_id!r} on the current page; call inspect_form to refresh ids")
        return self._fields[field_id]

    async def fill(self, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fill fields by id. Each item: {"id": ..., "value": ...}."""
        async with self._lock:
            page = await self.page()
            await self._close_menus(page)
            results = []
            for item in values:
                fid = str(item.get("id", ""))
                try:
                    field = await self._field(page, fid)
                    outcome = await self._fill_one(page, field, item.get("value"))
                    results.append({"id": fid, "label": field.get("label", ""), "ok": True, "result": outcome})
                except Exception as e:  # report and keep going; one odd widget shouldn't stop the rest
                    results.append({"id": fid, "ok": False, "error": f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"})
            return results

    async def fill_secret(self, field_id: str, secret: str) -> None:
        async with self._lock:
            page = await self.page()
            await self._field(page, field_id)
            await self._locator(page, field_id).fill(secret)

    async def _fill_one(self, page: Page, field: dict, value: Any) -> str:
        kind = field["kind"]
        loc = self._locator(page, field["id"])
        if kind == "password":
            raise PermissionError("Passwords are only filled with fill_secret")
        if kind == "file":
            path = config.expand(str(value))
            if not path or not path.exists():
                raise FileNotFoundError(f"No file at {value}")
            await loc.set_input_files(str(path))
            return f"uploaded {path.name}"
        if kind == "checkbox":
            want = value if isinstance(value, bool) else bool(polarity(value))
            await self._set_checked(loc, want)
            return "checked" if want else "unchecked"
        if kind in ("radio_group", "checkbox_group"):
            wanted = value if isinstance(value, list) else [value]
            picked = []
            for w in wanted:
                choice = choose_option(w, field.get("options") or [])
                if choice is None:
                    raise ValueError(f"{w!r} doesn't match any option: {field.get('options')}")
                idx = field["options"].index(choice)
                await self._set_checked(self._locator(page, f"{field['id']}.{idx}"), True)
                picked.append(choice)
            return "selected " + ", ".join(picked)
        if kind == "select":
            choice = choose_option(value, field.get("options") or [])
            if choice is None:
                raise ValueError(f"{value!r} doesn't match any option: {field.get('options')}")
            await loc.select_option(label=choice)
            return f"selected {choice}"
        if kind == "listbox":
            return await self._pick_from_listbox(page, loc, field, value)
        if kind == "combobox":
            if field.get("readonly"):  # pick-only: open the menu and choose, nothing to type into
                return await self._pick_from_listbox(page, loc, field, value)
            return await self._type_and_pick(page, loc, field, value)
        text = "" if value is None else str(value)
        if isinstance(value, bool):
            text = "Yes" if value else "No"
        if field.get("role") == "spinbutton":
            # Date parts (Workday's MM / YYYY) react to keystrokes, not a pasted value.
            await loc.click(timeout=5000)
            await loc.fill("")
            await loc.press_sequentially(text, delay=40)
            await loc.evaluate("el => el.blur()")
            return "typed"
        await loc.fill(text)
        await loc.evaluate("el => el.blur()")
        return "filled"

    async def _set_checked(self, loc: Locator, want: bool) -> None:
        try:
            await loc.set_checked(want, timeout=3000)
            return
        except (PlaywrightError, PlaywrightTimeout):
            pass
        checked = await loc.evaluate("el => el.checked === true || el.getAttribute('aria-checked') === 'true'")
        if checked != want:
            await loc.evaluate(CLICK_CHOICE_JS)

    async def _click_option(self, page: Page, field_id: str, text: str) -> None:
        frame = self._frame_for(page, field_id)
        target = frame.locator(f'[data-ja-opt="{_css_string(text)}"]').first
        if not await target.count():  # the menu re-rendered after it was read
            opt = frame.locator('[role="option"]:visible').filter(has_text=text)
            exact = opt.filter(has_text=re.compile(rf"^\s*{re.escape(text)}\s*$"))
            target = exact.first if await exact.count() else opt.first
        try:
            await target.click(timeout=3000)
        except PlaywrightTimeout:
            # Something sits over the menu; send the events to the option itself.
            await target.evaluate(
                "el => ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach("
                "t => el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true, view: window})))"
            )

    async def _pick_from_listbox(self, page: Page, loc: Locator, field: dict, value: Any) -> str:
        await self._open(page, field["id"], loc)
        options = await self._field_options(page, field["id"], loc, 2500)
        choice = choose_option(value, options)
        if choice is None:
            # Long lists are virtualized; typing jumps to the entry.
            await page.keyboard.type(str(value), delay=40)
            await page.wait_for_timeout(400)
            options = await self._field_options(page, field["id"], loc, 1500)
            choice = choose_option(value, options)
        if choice is None:
            await page.keyboard.press("Escape")
            raise ValueError(f"{value!r} doesn't match any option: {options[:30]}")
        await self._click_option(page, field["id"], choice)
        await self._confirm_choice(page, loc, choice)
        return f"selected {choice}"

    async def _confirm_choice(self, page: Page, loc: Locator, choice: str) -> None:
        """After picking from a menu, the field must show that choice (else the click
        landed in some other menu, or the widget refused it)."""
        await page.wait_for_timeout(150)
        shown = await loc.evaluate(SHOWN_VALUE_JS)
        # Only positive evidence counts: some widgets display the value where we can't see it,
        # and search text left in the box ("C" for "Choose not to disclose") isn't a choice.
        if (shown and norm(choice) not in norm(shown) and not norm(choice).startswith(norm(shown))
                and choose_option(choice, [shown]) is None):
            raise ValueError(f"Picked {choice!r} but the field shows {shown[:80]!r}; set it by hand or with click")

    async def _type_and_pick(self, page: Page, loc: Locator, field: dict, value: Any) -> str:
        text = str(value)
        await self._open(page, field["id"], loc)
        await loc.fill("")
        await loc.press_sequentially(text, delay=30)
        options = await self._field_options(page, field["id"], loc, 2500)
        if not options and not await loc.evaluate("el => !!el.form"):
            # Search-style pickers (Workday) list results after Enter. Inside a <form>,
            # Enter could submit the whole form, so it's never pressed there.
            await loc.press("Enter")
            options = await self._field_options(page, field["id"], loc, 2500)
        choice = choose_option(text, options)
        if choice is None:
            if options:
                await page.keyboard.press("Escape")
                raise ValueError(f"{text!r} doesn't match any suggestion: {options[:30]}")
            return "typed (no suggestions appeared)"
        await self._click_option(page, field["id"], choice)
        await self._confirm_choice(page, loc, choice)
        return f"selected {choice}"

    async def click(self, target: str, allow_submit: bool = False) -> dict[str, Any]:
        """Click an action/field by id, or the first visible button/link with that text."""
        async with self._lock:
            page = await self.page()
            by_id = target in self._actions or target in self._fields or re.fullmatch(r"(f\d+-)?\d+(\.\d+)?", target)
            loc = self._locator(page, target) if by_id else None
            text = self._actions[target]["text"] if target in self._actions else "" if by_id else target
            loc, info = await self._clickable(page, loc, text, allow_submit, target)
            before, note = page.url, None
            try:
                await loc.click(timeout=CLICK_TIMEOUT)
            except PlaywrightTimeout:
                present = await self._present(loc)
                # A page that moved on took the click (a slow navigation can outlast the
                # timeout); clicking again could act on the next page.
                if present or page.url == before:
                    # Knockout/React swapped the button out, or a cookie banner sits on top:
                    # click it directly, after finding and checking it again.
                    loc, info = await self._clickable(
                        page, loc if present else None, info["text"] or text, allow_submit, target)
                    await loc.evaluate("el => el.click()", timeout=5000)
                    note = "clicked directly: the button was covered or replaced"
                else:
                    note = "the click timed out but the page moved on, so it wasn't repeated; check the page"
            await self._settle(page)
            page = await self.page()  # the click may have opened a new tab
            summary = await self._summary(page)
            return {**summary, "note": note} if note else summary

    async def _clickable(self, page: Page, loc: Locator | None, text: str, allow_submit: bool,
                         target: str) -> tuple[Locator, dict[str, Any]]:
        """The element to click and what it is; final submit buttons are refused.

        Oracle's and other Knockout/React pages replace buttons as they re-render, so one
        that's gone (since inspect_form, or while it was being read) is found again by its
        text, and whatever is found is checked again.
        """
        for _ in range(3):
            if (loc is None or not await self._present(loc)) and text:
                loc = await self._find_by_text(page, text)
            if loc is None or not await self._present(loc):
                break
            try:
                info = await loc.evaluate(ELEMENT_INFO_JS, timeout=5000)
            except PlaywrightTimeout:
                loc = None
                continue
            if not allow_submit:
                self._check_clickable(info)
            if await self._present(loc):
                return loc, info
            loc = None
        raise KeyError(f"Nothing clickable matches {target!r}; call inspect_form for ids")

    @staticmethod
    async def _present(loc: Locator) -> bool:
        try:
            return bool(await loc.count())
        except PlaywrightError:
            return False

    @staticmethod
    def _check_clickable(info: dict[str, Any]) -> None:
        label = " ".join((info.get("label") or "").split())
        text = (info.get("text") or "").strip()
        if SUBMIT_RE.search(label) or (info.get("formSubmit") and FINALISH_RE.match(text)):
            raise SubmitBlocked(
                f"{label!r} looks like the final submit button. Use submit_application "
                "(after the user confirms), or let the user click it in the browser."
            )
        if info.get("formSubmit") and not NAVIGATION_RE.match(text) and config.Profile.load().settings.dry_run:
            raise SubmitBlocked(f"Dry run: {label!r} submits a form, and it isn't a recognised step button.")

    async def _find_by_text(self, page: Page, text: str) -> Locator | None:
        """First visible button/link named `text`, then any visible text match, across frames."""
        frames = [f for f in page.frames if not f.is_detached()]
        for exact_role in (True, False):
            for frame in frames:
                if exact_role:
                    loc = frame.get_by_role("button", name=text).or_(frame.get_by_role("link", name=text))
                else:
                    loc = frame.get_by_text(text, exact=False)
                loc = loc.filter(visible=True).first
                try:
                    if await loc.count():
                        return loc
                except PlaywrightError:
                    continue
        return None

    async def add_entries(self, kind_pattern: str, count: int) -> dict[str, Any]:
        """Click a section's Add button until it has `count` numbered entries."""
        async with self._lock:
            page = await self.page()
            frame = page.main_frame
            state = await frame.evaluate(ENTRIES_JS, kind_pattern)
            before = state["entries"]
            clicks = 0
            while state["entries"] < count and clicks < count + 2:
                if not state["buttons"]:
                    break
                await frame.locator(f'[data-ja-id="{state["buttons"][-1]["id"]}"]').click(timeout=5000)
                clicks += 1
                await page.wait_for_timeout(600)
                new = await frame.evaluate(ENTRIES_JS, kind_pattern)
                if new["entries"] <= state["entries"]:
                    await page.wait_for_timeout(1200)  # slow re-render; one more look
                    new = await frame.evaluate(ENTRIES_JS, kind_pattern)
                    if new["entries"] <= state["entries"]:
                        state = new
                        break
                state = new
            return {"before": before, "after": state["entries"], "clicks": clicks,
                    "add_button_found": bool(state["buttons"]) or clicks > 0}

    async def find_submit(self) -> list[dict[str, Any]]:
        async with self._lock:
            page = await self.page()
            data = await self._extract(page)
            return [a for a in data["actions"] if a.get("is_submit") and not a.get("disabled")]

    async def press_submit(self, action_id: str) -> dict[str, Any]:
        if config.Profile.load().settings.dry_run:  # second lock on the door, after submit_application's
            raise SubmitBlocked("Dry run: submitting is disabled")
        async with self._lock:
            page = await self.page()
            await self._locator(page, action_id).click(timeout=8000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                pass
            await page.wait_for_timeout(1500)
            page = await self.page()
            text = ""
            for frame in page.frames:
                try:
                    text += "\n" + await frame.evaluate(VISIBLE_TEXT_JS)
                except PlaywrightError:
                    continue
            data = await self._extract(page)
            return {
                "url": page.url,
                "confirmed": bool(CONFIRMATION_RE.search(text)),
                "errors": data["errors"],
                "text_excerpt": text.strip()[:1500],
            }
