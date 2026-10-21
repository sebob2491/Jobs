"""The browser's tabs, for BrowserSession (a mixin): the tab the tools act on, the tabs a click opens and the job
each one is for, and a tab closed, crashed, or gone with its browser."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any, Callable

from playwright.async_api import BrowserContext, Error as PlaywrightError, Frame, Page

if TYPE_CHECKING:
    from .browser import _View


class TabsMixin:
    """BrowserSession's tabs."""

    if TYPE_CHECKING:  # (the session's own)
        _ctx: BrowserContext | None
        _openers: dict[Page, Page]
        _frame_ids: dict[Frame, str]
        tab_jobs: dict[Page, int]
        _crashed: set[Page]
        keep_front: Callable[[], Page | None] | None
        _fronting: set[asyncio.Future]
        _page: Page | None
        strict_tabs: bool

        @property
        def _view(self) -> _View: ...

        @property
        def _lock(self) -> asyncio.Lock: ...

        def _views(self) -> list[_View]: ...

        async def _ensure_ctx(self) -> None: ...

    def _refront(self) -> None:
        """Bring the tab the person is at for the desk (keep_front) back in front: a tab a job filled meanwhile
        opened came up over it (a click's new tab, or a job's own)."""
        keep = self.keep_front() if self.keep_front is not None else None
        if keep is None or keep.is_closed():
            return

        async def front() -> None:
            with contextlib.suppress(Exception):
                await keep.bring_to_front()
        task = asyncio.ensure_future(front())
        self._fronting.add(task)
        task.add_done_callback(self._fronting.discard)

    def _watch(self, tab: Page) -> None:
        if not getattr(tab, "_ja_watched", False):
            tab._ja_watched = True  # type: ignore[attr-defined]
            tab.on("popup", lambda popup: self._on_popup(tab, popup))
            tab.on("close", lambda _: self._closed(tab))
            tab.on("crash", lambda _: self._crashed.add(tab))  # (kept once closed: it crashed first)

    def _closed(self, tab: Page) -> None:
        opener = self._openers.pop(tab, None)
        self.tab_jobs.pop(tab, None)
        for view in self._views():
            if tab is view.page and opener is not None and not opener.is_closed():
                view.page = opener  # a popup that closed itself (a sign-in window): back to its tab

    def _on_popup(self, opener: Page, popup: Page) -> None:
        # "Apply" buttons often open the application in a new tab: follow it, but only from
        # the tab being worked on. Tabs the person opens, and background reads, stay put.
        self._watch(popup)
        self._openers[popup] = opener
        # only while a click of ours may still be opening it (asml.com's Apply Now opens its tab
        # a few seconds on): a link the person opens (a privacy policy) doesn't take the tab over
        now, driven = time.monotonic(), False
        for view in self._views():  # (each view whose tab opened it: a job's own, filled at once with others)
            if opener is view.page:
                driven = driven or view.desk
                if now <= view.follow_until:
                    view.page = popup
        if driven:  # opened by a job the desk fills, over the tab the person may be at for another
            self._refront()

    def _forget(self, ctx: BrowserContext) -> None:
        """The person closed the browser window: open a fresh one next time it's needed."""
        if self._ctx is ctx:
            self._ctx = None
            for view in self._views():
                view.page = None
                view.fields.clear()
                view.actions.clear()
            self._openers.clear()
            self._frame_ids.clear()

    async def page(self) -> Page:
        await self._ensure_ctx()
        assert self._ctx is not None
        if self._page is None or self._page.is_closed():  # (a popup that closed itself is already back on its tab)
            if self.strict_tabs:
                raise browser.TabClosed("The tab this application was in has been closed.")
            else:
                live = [p for p in self._ctx.pages if not p.is_closed()]
                self._page = live[-1] if live else await self._ctx.new_page()
        return self._page

    async def new_tab(self) -> Page:
        """Open a tab and make it the one the tools act on (the Job Desk gives each job its own)."""
        async with self._lock:
            await self._ensure_ctx()
            assert self._ctx is not None
            self._page = await self._ctx.new_page()
            if self._view.desk:  # (a job's own tab, over the one the person may be at for another)
                self._refront()
            return self._page

    def use_tab(self, page: Page | None) -> bool:
        """Act on this tab from now on; False if it has been closed (or crashed)."""
        if page is None or self.lost(page) or self._ctx is None:
            return False
        self._page = page
        return True

    def lost(self, tab: Page) -> str:
        """Why nothing more can be done in a tab: "browser" (the browser it was in was closed, or
        crashed, and the tab with it), "closed", or "crashed" (Chrome's "Aw, Snap!": not even a
        reload brings the tab back to the tools). "" while it's there to use."""
        if tab.context is not self._ctx:
            return "browser"
        # a crashed tab that was closed since (by Chrome, or the desk) crashed first: that's what's said
        return "crashed" if tab in self._crashed else "closed" if tab.is_closed() else ""

    def lineage(self, tab: Page | None) -> list[Page]:
        """This tab and the tabs that opened it, nearest first: one job's tabs."""
        out: list[Page] = []
        while tab is not None and tab not in out:
            out.append(tab)
            tab = self._openers.get(tab)
        return out

    def job_tabs(self, job_id: int, tab: Page | None, others: list[Page]) -> list[Page]:
        """One job's tabs still open: its tab and the tabs that opened it (lineage), and every tab one of those opened
        in turn (a sign-in window, a policy page, a second window the site opened). Never a tab of `others` (another
        job's own tab), nor one Claude opened for another job (tab_jobs), nor one opened from either. A tab Claude
        opened for this job counts only as one of these: one it went on to use for something else stays."""
        own = set(self.lineage(tab))
        out: list[Page] = []
        for page in list(self._ctx.pages) if self._ctx is not None else []:
            for t in self.lineage(page):  # (the nearest of its openers that's anyone's says whose it is)
                if t in others or self.tab_jobs.get(t, job_id) != job_id:
                    break
                if t in own:
                    out.append(page)
                    break
        return [t for t in out if not t.is_closed()]

    def job_in(self, tab: Page | None) -> int | None:
        """The job this tab (or the tab that opened it) was opened for, if any."""
        return next((self.tab_jobs[t] for t in self.lineage(tab) if t in self.tab_jobs), None)

    @property
    def current_tab(self) -> Page | None:
        return self._page if self._page is not None and not self._page.is_closed() else None

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
                    title = await browser._bounded(p.title(), 5)  # (a hung tab's title isn't waited on for ever)
                except (PlaywrightError, browser._Hung):
                    title = ""
                out.append({"index": i, "url": p.url, "title": title, "active": p is self._page})
            return {"tabs": out}


# browser.py's own names (its waits, page scripts, helpers and exceptions) are read from it as browser.<name> when a
# method runs: a test's patch of one (monkeypatch.setattr(browser, ...)) reaches the code here as it does browser.py's.
# Imported last: browser.py imports this module for BrowserSession's bases, and either may be imported first.
from . import browser  # noqa: E402
