"""The Job Desk: a page on this computer that lists recommended openings and applies to
the ones picked with one button.

It is served by the plugin on 127.0.0.1 and drives the same browser session as Claude's
tools. Every API call needs the random token in the page's address and a local Host
header, so other websites open in the same browser can't reach it. The token is new at
each start; the queue outlasts a restart of the desk (pipeline.QUEUE_FILE).

    uv run --project <plugin>/server job-apply-desk      # on its own, without Claude
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import json
import math
import os
import re
import secrets
import socket
import sqlite3
import time
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import autofill, config, employment, history, letters, pay, places, report, workplace
from .browser import _cookie_setting
from .pipeline import (DESK_PASSWORDS, MAIL_WATCH_MAX, QUEUE_NO_LOCK, Applier, _may_accept_notices,
                       _may_manage_accounts, _test_identity, placed_key, question_key)
from .ats import ATS_NAMES, JOB_BOARDS, detect_ats, job_board
from .postings import FetchError, Posting, fetch_posting, finalize, parse_html
from .recommend import applicant_degree, days_since, not_interested_in, recommend, score_listing
from .search import (MAX_ALTERNATIVES, alternatives, every_employer, load_companies, own_companies_path, search_area,
                     set_search_area)
from .tracker import APPLICATION_STATUSES, NotInterested, company_key, damaged, normalize_url, standing
from .updates import UpdateCheck

PAGE = Path(__file__).resolve().parent / "static" / "desk.html"
DEFAULT_PORT = 8765
HISTORY_DAYS_MAX = 366  # the most days This week (/api/history) looks back over
HISTORY_RECENT_MAX = 200  # and the most of its newest lines it lists
VISIT_GAP = 30 * 60  # a look at the openings this long (seconds) after the one before begins a new visit
SEEN_WRITE = 60  # the time of the last look is written to desk.json at most this often (the page looks again and again)
FOUND_KEEP = 5000  # the openings whose first-found time is kept (openings_found.json): the newest
WHATS_NEW_MOST = 50  # the openings whats_new lists (it counts them all)
# a job's statuses once it's sent in, or waiting on its review page (APPLICATION_STATUSES): no opening to turn down
SENT = frozenset(APPLICATION_STATUSES)
# and once it's done with on the desk (the page's DONE): sent in, closed, or skipped. An opening in one is no news
DONE = frozenset({"applied", "interviewing", "offer", "rejected", "withdrawn", "skipped"})


def _recommendations_path() -> Path:
    return config.home() / "recommendations.json"


def _settings_path() -> Path:
    return config.home() / "desk.json"


def watch_path(port: int) -> Path:
    """Where a running desk keeps its port and key for Claude's watch of its mail requests (job-apply-wait-mail): the
    person's alone to read, and gone as the desk stops. Never on a command line, where other programs see it."""
    return config.home() / f"desk_watch_{port}.json"


def _found_path() -> Path:
    """When each opening was first found by a search, by its address (seconds): what's New is what came after the
    person's last visit, and whats_new lists what came after its last call. Kept apart from recommendations.json, which
    a change of what and where to search clears."""
    return config.home() / "openings_found.json"


def _whats_new_path() -> Path:
    """When Claude's whats_new was last called: the next call without `since` lists what came after it."""
    return config.home() / "whats_new.json"


def _seconds(value: Any) -> float | None:
    """A time kept as seconds, or None for anything else (a file changed by hand)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _iso_seconds(said: Any) -> float | None:
    """The tracker's time ("2026-10-17T08:00:00+00:00") as seconds, or None for one that doesn't read."""
    try:
        t = datetime.fromisoformat(str(said or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.astimezone()).timestamp()


def read_found() -> dict[str, float]:
    try:
        data = json.loads(_found_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found = data.get("found") if isinstance(data, dict) else None
    if not isinstance(found, dict):
        return {}
    return {u: t for u, at in found.items() if isinstance(u, str) and (t := _seconds(at)) is not None}


def _count_param(request: Request, name: str, default: int, most: int) -> int:
    """A whole number from 1 to `most` in the request's address (?days=7), or `default` without one."""
    said = request.query_params.get(name)
    if said is None:
        return default
    if not re.fullmatch(r"[0-9]{1,6}", said.strip()) or not 1 <= int(said) <= most:
        raise ValueError(f"{name} should be a whole number from 1 to {most}")
    return int(said)


class _QuietServer(uvicorn.Server):
    """uvicorn inside the MCP server: no signal handlers of its own (Claude Code owns them)."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def _serve(server: uvicorn.Server) -> None:
    """uvicorn ends a start that fails (the port taken meanwhile) with sys.exit(1); inside the
    MCP server that would end it too. Here it only ends the desk's own task."""
    try:
        await server.serve()
    except SystemExit as e:
        raise RuntimeError(f"the Job Desk's server stopped (exit {e.code})") from None


def _free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise OSError("No free local port")


class Desk:
    def __init__(self, srv: Any):
        self.srv = srv
        # (its queue kept as it changes, and picked up again as the desk starts after a restart: pipeline.QUEUE_FILE)
        self.applier = Applier(srv, keep_queue=True)
        self.applier.listed = lambda: self.listings  # (the openings listed here: for its Not interested check)
        self.fetch = fetch_posting  # reads each recommended posting's requirements
        self.token = secrets.token_urlsafe(18)
        self.port: int | None = None
        self.listings: list[dict[str, Any]] = []
        self.handled_only = False  # the page shows only openings where the desk goes all the way (desk_handled.yaml)
        # the page's filters (the design of Oct 18, Part 7): "Pay at least $__/yr" hides an opening whose pay is listed
        # and below it, and "Posted" one posted more days ago than it; neither hides one that doesn't say (None: off)
        self.min_pay: int | None = None
        self.posted_within: int | None = None
        # "Within __ miles" of home (places.py): hides an opening surely farther from the person's home town (None: off)
        self.within_miles: int | None = None
        # and its chips (CHIP_FILTERS: the requirements an opening may not ask), each hiding an opening only for a fact
        # it says and that fails it ([]: none pressed)
        self.chips: dict[str, list[str]] = {name: [] for name in CHIP_FILTERS}
        # Troubleshooting (the owner, Oct 12, to get the desk going at more employers): Find jobs searches every
        # list's employers, for the profile's titles and HR ones, and the page shows only the openings where the desk
        # doesn't go all the way yet, whatever their fit. Nothing is sent meanwhile (Applier.hold_submits)
        self.troubleshoot = False
        # profile.yaml's settings.jobs_at_once as written when "Jobs at once" was chosen on the desk (_load)
        self._at_once_profile: Any = None
        # Find jobs searches when the person presses it. With this on (off unless they turn it on: issue #112, note
        # 44), the page also searches by itself once the openings are 12 hours old, but not while the queue is paused
        self.auto_search = False
        self.search: dict[str, Any] = {"status": "idle", "at": None, "error": None, "query": "", "location": None,
                                       "errors": {}, "browser_only": [], "auto": False, "stopped": None}
        self._search_task: asyncio.Task | None = None
        self._searches = 0  # (counts the openings cleared: a search started before keeps none of what it finds)
        self._left_searches: set[asyncio.Task] = set()  # (still running after a clear: held until they end)
        self._serve_task: asyncio.Task | None = None
        self._watch_file_atexit = False
        self.updates = UpdateCheck()  # a newer published version, said on the page
        self._update_task: asyncio.Task | None = None
        self._server: uvicorn.Server | None = None
        # when the person last looked at the openings (the page loaded, or came to the front: saw), and the last look
        # before this visit began: an opening first saved after that one is New, all visit long (both in desk.json)
        self.last_seen: float | None = None
        self.seen_since: float | None = None
        self._seen_written = 0.0
        self.found: dict[str, float] = read_found()  # when each opening was first found (openings_found.json)
        self._load()

    # ------------------------------------------------------------- persistence
    def _load(self) -> None:
        try:
            saved = json.loads(_recommendations_path().read_text(encoding="utf-8"))
            results = saved.get("results")
            # a hand-edited file: only listings with an address (the page and state() need one)
            self.listings = [r for r in results if isinstance(r, dict) and isinstance(r.get("url"), str)] \
                if isinstance(results, list) else []
            self.search.update({k: saved.get(k) for k in ("query", "location", "errors", "browser_only")
                                if k in saved}, status="done", at=saved.get("at"))
            # (openings found before their times were kept: found by the search that saved them, none of them news)
            if (at := _seconds(saved.get("at"))) is not None:
                for item in self.listings:
                    self.found.setdefault(item["url"], at)
        except (OSError, ValueError, AttributeError):
            pass
        try:
            saved = json.loads(_settings_path().read_text(encoding="utf-8"))
            self.last_seen, self.seen_since = _seconds(saved.get("last_seen")), _seconds(saved.get("seen_since"))
            self.applier.auto_submit = saved.get("auto_submit") is True
            self.applier.tailor = saved.get("tailor_resumes") is True
            self.applier.letters = saved.get("cover_letters") is True
            self.applier.letters_approved = saved.get("cover_letters_approved") is True
            self.handled_only = saved.get("handled_only") is True
            self.min_pay = _min_pay(saved.get("min_pay"))
            self.posted_within = saved.get("posted_within") if saved.get("posted_within") in POSTED_WITHIN else None
            self.within_miles = saved.get("within_miles") if saved.get("within_miles") in WITHIN_MILES else None
            self.chips = {name: _chips(name, saved.get(name)) or [] for name in CHIP_FILTERS}
            self.auto_search = saved.get("auto_search") is True
            self.troubleshoot = saved.get("troubleshoot") is True and not self.handled_only
            self.applier.hold_submits = self.troubleshoot
            # "Jobs at once" as chosen on the desk, while profile.yaml's settings.jobs_at_once is still as it was
            # written then (changed there since, that's the newer choice; a profile that doesn't read just now keeps it)
            chosen, written = saved.get("jobs_at_once"), _profile_jobs_at_once()
            if type(chosen) is int and 1 <= chosen <= config.JOBS_AT_ONCE_MAX and (
                    written is _UNREAD or saved.get("jobs_at_once_profile") == written):
                self.applier.jobs_at_once = chosen
                self._at_once_profile = saved.get("jobs_at_once_profile")
        except (OSError, ValueError, AttributeError):
            pass

    def _save_settings(self) -> None:
        """desk.json, all or nothing (it's written at each look at the page too): a write cut short (a full disk, the
        program ending) leaves it as it was, never torn, which would turn every switch back to its default,
        Troubleshooting's hold on sending among them."""
        config.ensure_home()
        config.write_text_atomic(_settings_path(), json.dumps({"auto_submit": self.applier.auto_submit,
                                                "tailor_resumes": self.applier.tailor,
                                                "cover_letters": self.applier.letters,
                                                "cover_letters_approved": self.applier.letters_approved,
                                                "handled_only": self.handled_only,
                                                "min_pay": self.min_pay, "posted_within": self.posted_within,
                                                "within_miles": self.within_miles, **self.chips,
                                                "troubleshoot": self.troubleshoot,
                                                "auto_search": self.auto_search,
                                                "jobs_at_once": self.applier.jobs_at_once,
                                                "jobs_at_once_profile": self._at_once_profile,
                                                "last_seen": self.last_seen, "seen_since": self.seen_since}))

    def saw(self, now: float | None = None) -> None:
        """The person looked at the openings (the page loaded, or came to the front). A look VISIT_GAP or more after the
        last one begins a new visit: the openings first saved after that last look are New, and stay so all visit long
        (a reload, or the page coming to the front again, doesn't take their mark away). Written to desk.json at most
        every SEEN_WRITE seconds, and at each new visit."""
        now = time.time() if now is None else now
        last = self.last_seen
        visit = last is None or now - last >= VISIT_GAP
        if visit:
            self.seen_since = last  # (None at the very first look: nothing is news to someone who hasn't looked before)
        self.last_seen = now
        if visit or now - self._seen_written >= SEEN_WRITE:
            self._seen_written = now
            with contextlib.suppress(OSError):
                self._save_settings()

    def _save_found(self) -> None:
        """The openings' first-found times, the newest FOUND_KEEP."""
        if len(self.found) > FOUND_KEEP:
            self.found = dict(sorted(self.found.items(), key=lambda kv: kv[1])[-FOUND_KEEP:])
        config.ensure_home()
        config.write_text_atomic(_found_path(), json.dumps({"found": self.found}))

    # ------------------------------------------------------------- serving
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def app(self) -> Starlette:
        r = Route
        return Starlette(routes=[
            r("/", self.page_view),
            r("/api/state", self.state_view),
            r("/api/history", self.history_view),
            r("/api/safety", self.safety_view),
            r("/api/seen", self.seen_view, methods=["POST"]),
            r("/api/not_interested/{action}", self.not_interested_view, methods=["POST"]),
            r("/api/search", self.search_view, methods=["POST"]),
            r("/api/search/stop", self.search_stop_view, methods=["POST"]),
            r("/api/apply", self.apply_view, methods=["POST"]),
            r("/api/preflight", self.preflight_view, methods=["POST"]),
            r("/api/answer", self.answer_view, methods=["POST"]),
            r("/api/job/{job_id:int}/letter", self.letter_view, methods=["GET"]),
            r("/api/job/{job_id:int}/{action}", self.job_view, methods=["POST"]),
            r("/api/notes/{action}", self.notes_view, methods=["POST"]),
            r("/api/queue/{action}", self.queue_view, methods=["POST"]),
            r("/api/settings", self.settings_view, methods=["POST"]),
            r("/api/password", self.password_view, methods=["POST"]),
            r("/api/add", self.add_view, methods=["POST"]),
            r("/api/bank", self.bank_view, methods=["POST"]),
            r("/api/area", self.area_view, methods=["POST"]),
            r("/api/mail/wait", self.mail_wait_view),
        ])

    async def start(self, port: int = DEFAULT_PORT, open_browser: bool = True) -> str:
        """Serve the desk in the running event loop (inside the MCP server)."""
        # the queue kept before a restart, back before the page is served: a call from it never meets (nor writes over)
        # the queue before that
        self.applier.restore()
        if self._serve_task is None or self._serve_task.done():
            self.port = _free_port(port)
            cfg = uvicorn.Config(self.app(), host="127.0.0.1", port=self.port, log_config=None, access_log=False,
                                 log_level="warning", lifespan="off")
            self._server = _QuietServer(cfg)
            self._serve_task = asyncio.get_running_loop().create_task(_serve(self._server))
            for _ in range(200):
                if self._server.started or self._serve_task.done():
                    break
                await asyncio.sleep(0.05)
            if self._serve_task.done():
                raise RuntimeError(f"The Job Desk couldn't start on port {self.port}")
        self.applier.start()
        with contextlib.suppress(OSError):  # (without it, Claude's watch for mail requests says the desk isn't open)
            self._write_watch_file()
            if not self._watch_file_atexit:  # (the MCP server ends with Claude Code, never stopping the desk)
                self._watch_file_atexit = True
                atexit.register(self._remove_watch_file)
        if self._update_task is None or self._update_task.done():
            self._update_task = asyncio.get_running_loop().create_task(self._watch_updates())
        if open_browser:
            await asyncio.get_running_loop().run_in_executor(None, webbrowser.open, self.url)
        return self.url

    async def _watch_updates(self) -> None:
        """Look for a newer published version now and then (UpdateCheck keeps to its own pace)."""
        while True:
            with contextlib.suppress(Exception):
                await self.updates.refresh()
            await asyncio.sleep(600)

    def _write_watch_file(self) -> None:
        """The port and key Claude's watch (job-apply-wait-mail) reaches this desk with: written anew, readable and
        writable by the person alone (on Windows, a file in their own folder already is)."""
        if self.port is None:
            return
        config.ensure_home()
        path = watch_path(self.port)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()  # (made anew: an older file's wider permissions aren't kept)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"port": self.port, "token": self.token, "pid": os.getpid()}, f)

    def _remove_watch_file(self) -> None:
        """This desk's watch file, gone as it stops (one another desk has written since is left)."""
        if self.port is None:
            return
        path = watch_path(self.port)
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("token") == self.token:
                path.unlink()
        except (OSError, ValueError, AttributeError):
            pass

    def watch_command(self) -> str:
        """The command Claude runs in the background to wait for this desk's mail requests: the same in Git Bash,
        PowerShell and a Unix shell, and with no key in it (the watch reads this desk's watch file)."""
        server = (config.PLUGIN_ROOT / "server").as_posix()
        file = watch_path(self.port or DEFAULT_PORT).as_posix()
        return f'uv run --quiet --frozen --project "{server}" job-apply-wait-mail --desk "{file}"'

    def mail_watch(self) -> dict[str, Any]:
        """For Claude (open_job_desk, mail_requests): whether it should watch for the desk's mail requests (`watch`:
        settings.email_codes on, and no email app password the desk can use), and the command that waits for one."""
        a = self.applier
        codes = a._email_codes_on()
        before = a.mail_problem  # (a look at the login, said nowhere from here)
        try:
            working = a.mail_login() is not None
        except Exception:
            working = False
        finally:
            a.mail_problem = before
        saved = _saved(["email_password"])["email_password"]
        return {"email_codes": codes,
                "app_password": "saved" if working else "saved, but the desk can't use it" if saved else "not saved",
                "watch": codes and not working, "watch_command": self.watch_command()}

    async def stop(self) -> None:
        self._remove_watch_file()
        await self.applier.stop()
        if self._update_task is not None and not self._update_task.done():
            self._update_task.cancel()
            with contextlib.suppress(BaseException):
                await self._update_task
        if self._search_task is not None and not self._search_task.done():
            self._search_task.cancel()
            with contextlib.suppress(BaseException):
                await self._search_task
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._serve_task, 5)
        self._serve_task = self._server = None

    # ------------------------------------------------------------- views
    def _allowed(self, request: Request, api: bool) -> bool:
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False  # DNS rebinding: someone else's name pointing here
        # bytes: compare_digest refuses non-ASCII text, and a stray header must be a 403, not a 500
        return not api or secrets.compare_digest(request.headers.get("x-desk-token", "").encode(), self.token.encode())

    def _forbidden(self) -> Response:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    @staticmethod
    async def _body(request: Request) -> dict[str, Any]:
        """The request's JSON object; a ValueError for anything else (the page only sends objects)."""
        try:
            body = await request.json()
        except ValueError:
            raise ValueError("The request isn't JSON.") from None
        if not isinstance(body, dict):
            raise ValueError("The request should be a JSON object.")
        return body

    @staticmethod
    def _bad(e: Exception) -> Response:
        return JSONResponse({"error": str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)},
                            status_code=400)

    async def page_view(self, request: Request) -> Response:
        if not self._allowed(request, api=False):
            return self._forbidden()
        return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    async def state_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        return JSONResponse(self.state(), headers={"Cache-Control": "no-store"})

    async def history_view(self, request: Request) -> Response:
        """The page's This week: what the last `days` days of the desk's history (history.HISTORY_FILE) come to, with
        its newest `recent` lines."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            days = _count_param(request, "days", 7, HISTORY_DAYS_MAX)
            recent = _count_param(request, "recent", 20, HISTORY_RECENT_MAX)
        except ValueError as e:
            return self._bad(e)
        return JSONResponse(await asyncio.to_thread(history.summary, days, recent),
                            headers={"Cache-Control": "no-store"})

    async def mail_wait_view(self, request: Request) -> Response:
        """Claude's watch for the desk's mail requests (settings.email_codes, no working email app password:
        job-apply-wait-mail): answers as soon as a job's emailed code or link is asked of Claude (`due`), or after
        `wait` seconds (MAIL_WATCH_MAX at most) with none. `watch` says whether there's anything left to wait for, and
        `why` why not. Only whether there's something to do, never what: mail_requests() says that. Claude counts as
        watching while a wait is open, and a few minutes after."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            wait = _count_param(request, "wait", MAIL_WATCH_MAX, MAIL_WATCH_MAX)
        except ValueError as e:
            return self._bad(e)
        a = self.applier
        why = a.mail_watch_unwanted(queue=False)  # (the queue may be empty for a while yet: only at the end)
        if why is None:
            if await a.wait_for_mail_ask(wait, request.is_disconnected):
                return JSONResponse({"due": True, "watch": True}, headers={"Cache-Control": "no-store"})
            why = a.mail_watch_unwanted()
        return JSONResponse({"due": False, "watch": why is None, "why": why}, headers={"Cache-Control": "no-store"})

    async def safety_view(self, request: Request) -> Response:
        """The page's Safety window: each setting that decides what the desk does in the person's name, as it is now."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        return JSONResponse(self.safety(), headers={"Cache-Control": "no-store"})

    async def seen_view(self, request: Request) -> Response:
        """The page loaded, or came to the front: the person looked at the openings (saw)."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        self.saw()
        return JSONResponse({"since": self.seen_since, "last": self.last_seen})

    async def not_interested_view(self, request: Request) -> Response:
        """Not interested on an opening (with its reason, or none: tracker.NOT_INTERESTED_REASONS), and its Undo."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        action = request.path_params["action"]
        if action not in ("mark", "undo"):
            return JSONResponse({"error": f"unknown action {action!r}"}, status_code=404)
        try:
            body = await self._body(request)
            url, reason = body.get("url"), body.get("reason") or ""
            if not isinstance(url, str) or not url.strip():
                raise ValueError("url should be an opening's address")
            if not isinstance(reason, str):
                raise ValueError("reason should be a word")
            if action == "undo":
                if not self.interested_again(url):  # (said, never silent: the page shows it)
                    return JSONResponse({"undone": False, "error": "There was nothing to undo: this opening isn't "
                                         "marked Not interested (any more). The list shows it as it is now."},
                                        status_code=409)
                return JSONResponse({"undone": True})
            self.not_interested(url, reason.strip().lower())
        except (KeyError, ValueError) as e:
            return self._bad(e)
        return JSONResponse({"ok": True})

    async def search_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            auto = (await self._body(request)).get("auto") is True
        except ValueError as e:
            return self._bad(e)
        if auto and (not self.auto_search or self.applier.paused):
            # (a page that asks by itself: only with the switch on, and never while the queue is paused)
            return JSONResponse({"started": False})
        if self._search_task is None or self._search_task.done():
            self._search_task = asyncio.get_running_loop().create_task(self.find_jobs(auto=auto))
        return JSONResponse({"started": True})

    async def search_stop_view(self, request: Request) -> Response:
        """Stop search: the search under way ends, and the openings found before it stay."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        return JSONResponse({"stopped": await self.stop_search()})

    async def stop_search(self) -> bool:
        """The search under way, if any, stopped (find_jobs says so in self.search). Whether one was."""
        task = self._search_task
        if task is None or task.done():
            return False
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        return True

    async def apply_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            body = await self._body(request)
            urls, job_ids = body.get("urls") or [], body.get("job_ids") or []
            # lists of addresses and of whole numbers: "13" is no list of jobs (read letter by
            # letter it was jobs 1 and 3), nor is true a job's number
            if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
                raise ValueError("urls should be a list of web addresses")
            if (not isinstance(job_ids, list)
                    or not all(isinstance(j, int) and not isinstance(j, bool) for j in job_ids)):
                raise ValueError("job_ids should be a list of job numbers")
        except ValueError as e:
            return self._bad(e)
        # (never Submit for me while troubleshooting: a job queued now could go once it's off)
        notes: dict[str, Any] = {}
        queued, refused = self.apply(urls=urls, job_ids=job_ids,
                                     submit=body.get("submit") is True and not self.troubleshoot,
                                     notes=notes)
        return JSONResponse({"queued": queued, "already_applied": refused,
                             **({"not_interested": notes["not_interested"]} if notes.get("not_interested") else {}),
                             **({"ordered": True} if notes.get("ordered") else {})})

    async def preflight_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            body = await self._body(request)
            urls = body.get("urls") or []
            if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
                raise ValueError("urls should be a list of web addresses")
        except ValueError as e:
            return self._bad(e)
        return JSONResponse({"lines": self.preflight(urls)})

    async def answer_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            body = await self._body(request)
            job_id, answers = body.get("job_id"), body.get("answers") or []
            if not isinstance(job_id, int) or isinstance(job_id, bool):
                raise ValueError("job_id should be a job number")
            if not isinstance(answers, list) or not all(isinstance(a, dict) for a in answers):
                raise ValueError("answers should be a list of questions and answers")
            note = self.answer(job_id, answers)
        except (KeyError, ValueError) as e:
            return self._bad(e)
        return JSONResponse({"ok": True, "note": note})

    async def job_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        job_id, action = request.path_params["job_id"], request.path_params["action"]
        a = self.applier
        if job_id >= 2 ** 63:  # past what the tracker can hold: no such job
            return JSONResponse({"error": f"No job with id {job_id}"}, status_code=404)
        try:
            if action == "resume":
                a.resume(job_id)
            elif action == "later":
                a.later(job_id)
            elif action == "skip":
                await a.skip(job_id)
            elif action == "unskip":  # Undo on a skipped job
                await a.unskip(job_id)
            elif action == "submit":
                a.submit_now(job_id)
            elif action == "show":
                if not await a.focus(job_id):
                    return JSONResponse({"error": "Its tab is closed. Press Carry on to open it again."},
                                        status_code=409)
            elif action == "round":  # the desk's Bot checks round: this check's tab up, nothing started over it
                if not await a.focus(job_id):
                    return JSONResponse({"error": "Its tab is closed. Press Carry on to open it again."},
                                        status_code=409)
                a.start_round(job_id)
            elif action == "round_end":
                a.end_round()
            elif action == "usual_resume":
                a.use_usual_resume(job_id)
            elif action in ("letter_ok", "letters_off"):
                # Use it, on the first cover letter's card (the rest go in without asking), or Turn cover letters off:
                # kept in desk.json whatever comes after (the job queued again may be refused: marked Not interested)
                try:
                    (a.approve_letter if action == "letter_ok" else a.letters_off_for)(job_id)
                finally:
                    self._save_settings()
            elif action == "apply_anyway":  # a job that may be one begun or applied to on another site: it isn't
                a.apply_anyway(job_id)
            elif action == "applied":
                self.srv.tracker().update(job_id, status="applied", note="marked applied in the Job Desk")
                a.mark_applied(job_id)
            elif action == "report":  # a scrubbed report to file on GitHub: shown first, filed by the person
                job = self.srv.tracker().get(job_id)
                if job is None:
                    raise KeyError(f"No job with id {job_id}")
                anonymous = False  # (the page's box for a report that doesn't say which job it was)
                with contextlib.suppress(ValueError):
                    anonymous = (await self._body(request)).get("anonymous") is True
                run = a.runs.get(job_id)  # (and what the desk's history keeps out of a line on it, the report too)
                hide = await asyncio.to_thread(Applier._report_hidden, run) if run is not None else set()
                return JSONResponse(await asyncio.to_thread(report.build, job, run, anonymous=anonymous, hide=hide))
            else:
                return JSONResponse({"error": f"unknown action {action!r}"}, status_code=404)
        except (KeyError, ValueError) as e:
            return self._bad(e)
        return JSONResponse({"ok": True})

    async def letter_view(self, request: Request) -> Response:
        """The cover letter the desk wrote for a job (its card's Cover letter), from the job's folder: a PDF there
        alone, behind the page's key like the rest."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        job_id = request.path_params["job_id"]
        job = self.srv.tracker().get(job_id, with_description=False) if job_id < 2 ** 63 else None
        found = autofill.tailored_document(job, "cover_letter") if job else None
        if (job is None or found is None or Path(found).suffix.lower() != ".pdf"
                or Path(found).parent != Path(job["folder"])):
            return JSONResponse({"error": "No cover letter is in this job's folder."}, status_code=404)
        return FileResponse(found, media_type="application/pdf", headers={"Cache-Control": "no-store"})

    async def queue_view(self, request: Request) -> Response:
        """Pause queue / Resume queue: the job under way finishes its pass, and no other starts while paused."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        action = request.path_params["action"]
        if action not in ("pause", "resume"):
            return JSONResponse({"error": f"unknown action {action!r}"}, status_code=404)
        (self.applier.pause_queue if action == "pause" else self.applier.resume_queue)()
        return JSONResponse({"paused": self.applier.paused})

    async def notes_view(self, request: Request) -> Response:
        """The notes the desk took on its stops (report.take_note): shown as the one issue they'd
        make, filed on GitHub by the person, then cleared."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        action = request.path_params["action"]
        if action == "show":
            return JSONResponse(await asyncio.to_thread(report.notes))
        if action != "clear":
            return JSONResponse({"error": f"unknown action {action!r}"}, status_code=404)
        try:
            ids = (await self._body(request)).get("ids")
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
                raise ValueError("ids should be a list of the notes shown")
        except ValueError as e:
            return self._bad(e)
        return JSONResponse({"cleared": report.clear_notes(ids)})

    async def settings_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            body = await self._body(request)
        except ValueError as e:
            return self._bad(e)
        at_once = body.get("jobs_at_once")
        if "jobs_at_once" in body and not (type(at_once) is int and 1 <= at_once <= config.JOBS_AT_ONCE_MAX):
            return self._bad(ValueError(f"jobs_at_once should be a number from 1 to {config.JOBS_AT_ONCE_MAX}"))
        if "min_pay" in body and body["min_pay"] is not None and _min_pay(body["min_pay"]) is None:
            return self._bad(ValueError("min_pay should be a yearly figure in dollars, or null for any pay"))
        if "posted_within" in body and body["posted_within"] not in (None, *POSTED_WITHIN):
            return self._bad(ValueError("posted_within should be 7, 14, 30 or null for any day"))
        if "within_miles" in body and body["within_miles"] not in (None, *WITHIN_MILES):
            return self._bad(ValueError(f"within_miles should be {', '.join(map(str, WITHIN_MILES))} or null for any "
                                        "distance"))
        for name, choices in CHIP_FILTERS.items():
            if name in body and body[name] is not None and _chips(name, body[name]) is None:
                return self._bad(ValueError(f"{name} should be a list of some of {', '.join(choices)} "
                                            "(an empty list, or null, for none)" if choices else
                                            f"{name} should be a list of job systems' keys, such as workday or "
                                            "oracle_hcm (an empty list, or null, for none)"))
        for name in CHIP_FILTERS:
            if name in body:
                self.chips[name] = _chips(name, body[name]) or []
        if "min_pay" in body:
            self.min_pay = _min_pay(body["min_pay"])
        if "posted_within" in body:
            self.posted_within = body["posted_within"]
        if "within_miles" in body:
            self.within_miles = body["within_miles"]
        if "jobs_at_once" in body:
            self.applier.jobs_at_once = at_once
            written = _profile_jobs_at_once()  # (as profile.yaml has it now: a later change there is the newer choice)
            self._at_once_profile = self._at_once_profile if written is _UNREAD else written
            self.applier.wake()  # (more at once: the next job begins now)
        if "auto_submit" in body:
            self.applier.auto_submit = body["auto_submit"] is True  # not "false", which bool() calls true
        if "tailor_resumes" in body:
            self.applier.set_tailor(body["tailor_resumes"] is True)
        if "cover_letters" in body:
            self.applier.set_letters(body["cover_letters"] is True)
        # the two employer switches are one or the other: either turned on turns the other off
        if "handled_only" in body:
            self.handled_only = body["handled_only"] is True
            self.troubleshoot = self.troubleshoot and not self.handled_only
        if "auto_search" in body:
            self.auto_search = body["auto_search"] is True
        if "troubleshoot" in body:
            was, self.troubleshoot = self.troubleshoot, body["troubleshoot"] is True
            self.handled_only = self.handled_only and not self.troubleshoot
            if was != self.troubleshoot:
                # the openings found one way aren't the other's (troubleshooting's: every list's employers, HR jobs):
                # cleared, for Find jobs to search again, so none is applied to by mistake
                self._forget_openings()
        self.applier.hold_submits = self.troubleshoot
        if any(k in body for k in ("auto_submit", "tailor_resumes", "cover_letters", "handled_only", "troubleshoot",
                                   "auto_search", "jobs_at_once", "min_pay", "posted_within", "within_miles",
                                   *CHIP_FILTERS)):
            self._save_settings()
        return JSONResponse({"auto_submit": self.applier.auto_submit, "tailor_resumes": self.applier.tailor,
                             "cover_letters": self.applier.letters,
                             "handled_only": self.handled_only, "troubleshoot": self.troubleshoot,
                             "min_pay": self.min_pay, "posted_within": self.posted_within,
                             "within_miles": self.within_miles, **self.chips,
                             "auto_search": self.auto_search, "jobs_at_once": self.applier.at_once()})

    def _forget_openings(self) -> None:
        """The openings found so far cleared, for Find jobs to search again (where or how it searches changed). A
        search under way keeps none of what it finds, and the next Find jobs starts a new one."""
        self._searches += 1
        if self._search_task is not None and not self._search_task.done():
            self._left_searches.add(self._search_task)
            self._search_task.add_done_callback(self._left_searches.discard)
        self._search_task = None
        self.listings = []
        self.search.update(status="idle", at=None, auto=False, stopped=None)
        with contextlib.suppress(OSError):
            _recommendations_path().unlink()

    async def area_view(self, request: Request) -> Response:
        """What and where Find jobs searches (the What and where to search window): the job titles, the titles to
        leave out, the places, and the plugin's employer lists. The openings found for others are cleared, for Find
        jobs to search again."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            body = await self._body(request)
            places, lists, titles = body.get("places"), body.get("lists"), body.get("titles")
            avoid = body.get("avoid_titles")
            for value, what in ((places, "places"), (lists, "lists"), (titles, "titles"), (avoid, "avoid_titles")):
                if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
                    raise ValueError(f"{what} should be a list of words")
            area = set_search_area(places=places, lists=lists, titles=titles, avoid_titles=avoid)
        except ValueError as e:
            return self._bad(e)
        self._forget_openings()
        return JSONResponse(area)

    async def bank_view(self, request: Request) -> Response:
        """An answer to one of the common questions (data/question_bank.yaml), kept in answers.yaml for
        every application that asks it; an empty one takes it out."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            body = await self._body(request)
            answer = body.get("answer")
            if answer is not None and not isinstance(answer, str):
                raise ValueError("An answer is a few words")
            config.save_bank_answer(str(body.get("id") or ""), answer)
        except (ValueError, OSError) as e:
            return self._bad(e)
        return JSONResponse({"ok": True})

    async def password_view(self, request: Request) -> Response:
        """A career-site password typed into the page goes straight to secrets.yaml. It is
        never sent back, logged, or shown to Claude."""
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            body = await self._body(request)
            name = str(body.get("name") or "")
            env = "JOB_APPLY_SECRET_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
            if name and os.environ.get(env):
                # the environment's wins (config.get_secret): saved here, it would never be used
                raise ValueError(f"This password is set in your environment ({env}), which comes before one saved "
                                 "here. Change it there, or remove it there and save it here.")
            config.save_site_password(name, str(body.get("value") or ""))
        except ValueError as e:
            return self._bad(e)
        if name == "email_password":
            # the new one is tried at the next emailed code, even if it's the one turned down before
            # (a mail service's hiccup could be taken for a refusal)
            self.applier.mail_problem = self.applier._mail_refused = None
        return JSONResponse({"saved": True})

    async def add_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        try:
            body = await self._body(request)
        except ValueError as e:
            return self._bad(e)
        links = [u for u in re.split(r"\s+", str(body.get("text") or "")) if u]
        if not links:
            return JSONResponse({"error": "Paste a job link first."}, status_code=400)
        # the rest stay in the page's box, for another Add, rather than vanishing unread
        return JSONResponse({"added": await self.add_links(links[:MAX_LINKS]), "left": links[MAX_LINKS:]})

    # ------------------------------------------------------------- actions
    async def read_in_browser(self, url: str) -> Posting:
        """A posting that turns away plain requests, read in a background tab: iCIMS's (HTTP
        405), from the frame its posting is drawn in. Others aren't read this way: Find jobs
        reads dozens, and the browser is shared with the applications."""
        if detect_ats(url) != "icims":
            raise FetchError(f"not read in the browser: {url}")
        return await self.srv.read_icims_posting(url)

    async def find_jobs(self, auto: bool = False) -> None:
        """Every employer's career site searched for the profile's titles and places (`auto`: the page's own search
        of openings 12 hours old, said as such while it runs). Stopped (Stop search), the openings before it stay."""
        self.search.update(status="running", error=None, started=time.time(), auto=auto, stopped=None)
        searching = self._searches  # (a search started before the openings were cleared keeps none it finds)
        try:
            every = self.troubleshoot  # (as it was when Find jobs was pressed)

            async def run_search(query: str, location: str | None, limit: int) -> dict[str, Any]:
                if every:
                    return await self.srv.search_company_jobs(troubleshoot_query(query), location=location,
                                                              limit_per_company=limit, every_list=True)
                return await self.srv.search_company_jobs(query, location=location, limit_per_company=limit)

            out = await recommend(config.Profile.load(), run_search, limit_per_company=10, read_postings=40,
                                  fetch=self.fetch, fetch_hard=self.read_in_browser)
            if searching != self._searches:
                return
            self.listings = out["results"]
            at = time.time()
            for item in self.listings:  # (first found now, unless an earlier search found it)
                self.found.setdefault(item["url"], at)
            with contextlib.suppress(OSError):
                self._save_found()
            self.search.update(status="done", at=at, query=out["query"], location=out["location"],
                               errors=out["errors"], browser_only=out["browser_only"])
            config.ensure_home()
            _recommendations_path().write_text(json.dumps({**out, "at": at}, default=str), encoding="utf-8")
        except asyncio.CancelledError:
            if searching == self._searches:
                self.search.update(status="done" if self.search.get("at") else "idle", stopped=time.time())
            raise
        except Exception as e:
            if searching == self._searches:
                self.search.update(status="failed", error=f"{type(e).__name__}: {str(e)[:300]}")

    def preflight(self, urls: list[str]) -> list[str]:
        """What will stop these jobs, said before Apply sends them off (the review of Oct 15: a batch of 30 found it
        out one stop at a time): a job system with no password saved (its jobs stop at the sign-in), no email app
        password while one of them is on a system that mails a sign-up code or link, and the common questions not
        answered yet. None when nothing will."""
        t = self.srv.tracker()
        by_system: dict[str, int] = {}
        mailing = False
        for url in urls:
            item = self._listing(url) or t.find_by_url(url) or {}
            ats = str(item.get("ats") or detect_ats(str(item.get("apply_url") or url)) or "")
            if f"{ats}_password" in DESK_PASSWORDS:
                by_system[f"{ats}_password"] = by_system.get(f"{ats}_password", 0) + 1
            mailing = mailing or f"{ats}_password" in DESK_PASSWORDS or ats in MAILS_CODES
        saved = _saved([*by_system, "email_password"])
        lines = []
        for name, n in sorted(by_system.items(), key=lambda kv: (-kv[1], kv[0])):
            if not saved[name]:
                lines.append(f"{DESK_PASSWORDS[name]}: no password saved, so {n} of these jobs "
                             f"{'stop' if n != 1 else 'stops'} at the sign-in for you. Save one under Site passwords "
                             "and the desk signs in itself.")
        if mailing and not saved["email_password"]:
            lines.append("No email app password saved: a site that emails a sign-up code or link waits for you to "
                         "enter it. Save one under Site passwords and the desk reads it from your inbox.")
        try:
            left = sum(1 for q in autofill.common_questions(config.Profile.load()) if q.get("answer") in (None, ""))
        except ValueError:  # a profile that doesn't read: the queue says so itself
            left = 0
        if left:
            lines.append(f"{left} common question{'s' if left != 1 else ''} not answered yet (the Common questions "
                         "card): each stops the first job that asks it.")
        return lines

    def _listing(self, url: str) -> dict[str, Any] | None:
        return next((item for item in self.listings if item.get("url") == url), None)

    def not_interested(self, url: str, reason: str = "") -> None:
        """The person's Not interested on an opening, with its reason or none (tracker.NOT_INTERESTED_REASONS): hidden
        from the openings, but for their Not interested group, and never queued until their Undo (kept in the tracker).
        Not one the desk has a card for (begun, or skipped from its card, whose Undo would queue it again: Skip job is
        for those), nor one sent in already."""
        t = self.srv.tracker()
        item, job = self._listing(url), self._job_of(url)
        if item is None and job is None:
            raise KeyError("That opening isn't in the list any more.")
        if job is not None and job["id"] in self.applier.runs:
            raise ValueError("The desk has a card for this job (under Needs you, Working on, or Ready and done): "
                             "Skip job there takes it out.")
        if job is not None and job.get("status") in SENT:
            raise ValueError("This job is already sent in, or waiting on its review page.")
        said = item or job or {}
        posting = (item or {}).get("posting") or {}
        # (with the requisition numbers it gives, for a job saved since that is surely this posting: never another's)
        t.set_not_interested(url, company=str(said.get("company") or ""), title=str(said.get("title") or ""),
                             reason=reason, job_id=job["id"] if job is not None else None,
                             location=str(said.get("location") or ""),
                             external_id=str((item or {}).get("external_id") or posting.get("external_id") or ""),
                             description=str(posting.get("description") or ""))

    def interested_again(self, url: str) -> bool:
        """Undo on an opening's Not interested: the opening, and the job its row is (_job_of: the job saved from a job
        board that an employer's posting not saved yet is), shown again. Whether anything was marked."""
        job = self._job_of(url)
        return self.srv.tracker().undo_not_interested(url, job_id=job["id"] if job is not None else None,
                                                      listed=self.listings)

    def unwanted(self, url: str, rows: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """The person's Not interested mark on an opening, if any: on its address, or on the job its row is (_job_of),
        by that job's number or any of its addresses, or a mark made before that job was saved that is surely its
        posting (Tracker.marked_not_interested, with the openings listed here: JR1 marked is never JR2's)."""
        t = self.srv.tracker()
        mark = t.marked_not_interested(url=url, listed=self.listings)
        if mark is None and (job := self._job_of(url, rows)) is not None:
            mark = t.marked_not_interested(job_id=job["id"], listed=self.listings)
        return mark

    def _row_jobs(self) -> dict[str, Any]:
        """The job each opening's row is, by its address, as state() lists them: the one at its address (or with it
        among its other addresses), or, for an opening on the employer's site not saved yet, the job saved from a job
        board that it surely is, unless another opening is alike that job too (Tracker.same_posting, all at once)."""
        t = self.srv.tracker()
        tracked = [t.find_by_url(item["url"]) for item in self.listings]
        copies = iter(t.same_posting([item for item, job in zip(self.listings, tracked) if job is None]))
        return {item["url"]: job if job is not None else next(copies) for item, job in zip(self.listings, tracked)}

    def _job_of(self, url: str, rows: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """The job an opening's row is (_row_jobs: `rows`, worked out once for many), or the job at an address not
        listed."""
        if self._listing(url) is not None:
            return (rows if rows is not None else self._row_jobs()).get(url)
        return self.srv.tracker().find_by_url(url)

    def apply(self, urls: list[str], job_ids: list[int], submit: bool,
              notes: dict[str, Any] | None = None) -> tuple[list[int], list[str]]:
        """Queue these jobs. Returns the ids queued, and the titles of any already applied to,
        which are never queued again (with "Submit for me" on, that would apply twice), or
        skipped (queued once the person's Undo puts them back). Never one the person said Not interested in (until its
        Undo): their titles go in `notes["not_interested"]`. Two or more queued together go the likeliest to go all the
        way first (likely_order; `notes["ordered"]` when that changed their order); one alone keeps its place, after
        those queued before it."""
        t = self.srv.tracker()
        left_out: list[str] = []
        ids = [int(j) for j in job_ids]
        rows = self._row_jobs() if any(self._listing(str(url)) is not None for url in urls) else {}
        for url in urls:
            if not re.match(r"(?i)^(https?|file)://", str(url)) or config.own_file(str(url)):
                continue  # a listing whose link isn't a web address ("javascript:…"), or is one of the desk's own files
            if self.unwanted(url, rows):  # (never saved for Apply either: by its address, or the job its row is)
                left_out.append(str((self._listing(url) or self._job_of(url, rows) or {}).get("title") or "a job"))
                continue
            tracked = t.find_by_url(url)
            if tracked is not None and self._listing(url) is None:
                ids.append(tracked["id"])  # added by Claude or by hand
                continue
            item = self._listing(url) or {"url": url}
            posting = item.get("posting") or {}
            job, _ = t.upsert({
                "url": url, "apply_url": posting.get("apply_url") or "",
                "title": item.get("title") or posting.get("title"), "company": item.get("company"),
                "location": item.get("location") or posting.get("location"),
                "ats": item.get("ats") or posting.get("ats"), "source": "job desk",
                "external_id": item.get("external_id") or posting.get("external_id"),
                "salary": posting.get("salary"),
                "employment_type": posting.get("employment_type") or item.get("employment_type"),
                "posted_at": posting.get("posted_at") or item.get("posted"), "description": posting.get("description"),
                **{k: (item if item.get("pay_period") else posting).get(k) for k in PAY_KEYS},
                **{k: (posting if workplace.better(posting, item) else item).get(k) for k in workplace.FIELDS},
            })
            ids.append(job["id"])
        # and by the job each is (by its number, or any of its addresses: one made one with a job marked on another
        # site is that job)
        kept = []
        for j in dict.fromkeys(ids):
            if t.marked_not_interested(job_id=j, listed=self.listings) is None:
                kept.append(j)
            else:
                left_out.append(str((t.get(j, with_description=False) or {}).get("title") or f"job {j}"))
        ids = kept
        if len(ids) > 1:  # (queued together: the likeliest to go all the way first, each tier in the person's order)
            jobs = {j: t.get(j, with_description=False) or {"id": j} for j in ids}
            mine, ids = ids, [job["id"] for job in likely_order([jobs[j] for j in ids])]
            if notes is not None and ids != mine:
                notes["ordered"] = True
        if notes is not None and left_out:
            notes["not_interested"] = left_out
        queued, refused = [], []
        for job_id in ids:
            begun = self.applier.runs.get(job_id)
            if begun is not None and begun.started and begun.status not in ("queued", "running"):
                begun.in_their_hands = True  # (Apply on a job begun before: its tab may hold what they did in it)
            try:
                self.applier.enqueue(job_id, submit=submit)
                queued.append(job_id)
            except KeyError:  # no such job (a stale page)
                continue
            except ValueError:
                job = t.get(job_id, with_description=False) or {}
                refused.append(job.get("title") or f"job {job_id}")
        return queued, refused

    async def add_links(self, links: list[str]) -> list[dict[str, Any]]:
        """Pasted job links (LinkedIn, Indeed, a company site): read each posting, save it to
        the tracker, and so to the list, ready for Apply. LinkedIn and Indeed usually turn
        away plain requests, so those are read in a background tab of the browser, where the
        person may be signed in."""
        out: list[dict[str, Any]] = []
        for url in links:
            if not re.match(r"(?i)^(https?|file)://", url):
                out.append({"url": url, "error": "not a web address"})
                continue
            if config.own_file(url):  # its text would be saved as a posting, where Claude could read it
                out.append({"url": url, "error": "that's one of the desk's own files, not a job posting"})
                continue
            posting = None
            try:
                posting = await self.fetch(url)
                if not posting.is_useful and detect_ats(url) in ("linkedin", "indeed"):
                    posting = None  # their sign-in page, not the posting
            except Exception:  # FetchError, a timeout, an odd page: the browser gets a go
                posting = None
            if posting is None or not posting.is_useful:  # or a page that builds itself with script
                try:
                    seen = (await self.srv.read_icims_posting(url) if detect_ats(url) == "icims" else
                            finalize(parse_html(await self.srv.browser.background_html(url), url)))
                    if posting is None or len(seen.description) > len(posting.description):
                        posting = seen
                except Exception as e:  # report it on the page; the other links still go in
                    if posting is None:
                        out.append({"url": url, "error": f"couldn't read the posting: {_first_line(e)}"})
                        continue
            posting.company = self.srv.listed_employer(url) or posting.company
            job, _ = self.srv.tracker().upsert(posting.to_dict())
            out.append({"url": job["url"], "job_id": job["id"], "title": job.get("title"),
                        "company": job.get("company"), "status": job.get("status"), "warnings": posting.warnings})
        return out

    def answer(self, job_id: int, answers: list[dict[str, Any]]) -> str | None:
        """Fill the person's answers in. Returns a note if some couldn't be remembered (they
        still go into this application)."""
        run = self.applier.runs.get(job_id)
        if run is None or run.status != "needs_you":
            # nothing is remembered for a job that isn't asking (a stale page)
            raise ValueError("That job isn't waiting for answers any more.")
        problem = None
        # an account form's security questions, the person's for that form alone: kept apart (Run.account_answers),
        # never remembered, never among the application's answers, never shown again. By each box's own key, as the
        # card sends it, or by its label alone where that's one box's (a page from before)
        own = [placed_key(q) for q in run.questions if q.get("account")]
        bare = [question_key(q.get("label") or "") for q in run.questions if q.get("account")]
        account = {**{b: k for b, k in zip(bare, own) if bare.count(b) == 1}, **{k: k for k in own}}
        given = {account[question_key(str(a.get("label") or ""))]: a.get("value") for a in answers
                 if question_key(str(a.get("label") or "")) in account and a.get("value") not in (None, "")}
        if given and len(given) < len(own):  # (all of them, or none: the form takes them together)
            raise ValueError("Answer each of its security questions here, or none of them (and fill them in in the "
                             "desk's browser window).")
        run.account_answers = given
        for a in answers:
            label, value = str(a.get("label") or ""), a.get("value")
            if not label or value in (None, "") or question_key(label) in account:
                continue
            # (answered again: a box the site had emptied of it, left empty while optional, is filled again)
            for box, asked in list(run.once_left.items()):
                if question_key(label) in (box, asked):
                    del run.once_left[box]
            if a.get("remember", True) and " | " not in label:  # (one box's own answer is never remembered)
                # an answer naming this employer ("Acme website") is remembered as "Company Website", or not at
                # all: it isn't the answer at another employer's
                kept = autofill.answer_to_remember(label, value, run.company)
                if kept is None:
                    said = f'"{value}" names {run.company}, so it\'s used for this application only, not remembered.'
                    problem = f"{problem} {said}" if problem else said
                    run.once[question_key(label)] = value
                    continue
                try:
                    config.save_answer(label, kept, run.company)
                    if kept == value:
                        run.once.pop(question_key(label), None)  # an earlier answer for this application only
                        continue
                    # (remembered in its general form: this application gets the choice as picked, which a
                    # list with two of the employer's own sites can't be told from "Company Website")
                except (ValueError, OSError) as e:
                    problem = f"Couldn't remember your answers ({e}); they're used for this application only."
            run.once[question_key(label)] = value
        if run.started:
            # (its tab may hold what they did while its card was up: a file kept there is theirs)
            run.in_their_hands = True
        self.applier.go_on_for(self.applier.enqueue(job_id, submit=run.submit, front=True))
        self.applier.pressed.add(job_id)  # (answered on its card: it goes even while the queue is paused)
        return problem

    # ------------------------------------------------------------- state
    def state(self) -> dict[str, Any]:
        try:
            prof, profile_problem = config.Profile.load(), None
        except Exception as e:  # a typo in profile.yaml: said on the page, which keeps working
            prof, profile_problem = config.Profile({}), _first_line(e)
        try:
            return self._state(prof, profile_problem, self.srv.tracker(), None)
        except sqlite3.Error as e:
            # a tracker.db that isn't a database any more (or one another program holds past the wait): said on the
            # page, which keeps working without the jobs it holds, instead of failing every poll and staying blank
            # (the review of Oct 18)
            return self._state(prof, profile_problem, _NoJobs(), _first_line(e))

    def _state(self, prof: config.Profile, profile_problem: str | None, t: Any,
               tracker_problem: str | None) -> dict[str, Any]:
        settings = prof.settings
        runs = {jid: r.public() for jid, r in self.applier.runs.items()}
        for waiting, shown in runs.items():  # (a job whose emailed code Claude is looking for in the person's Gmail)
            shown["gmail"] = self.applier.gmail_looking(waiting)
            shown["gmail_note"] = self.applier.gmail_note(waiting)  # (and why Claude no longer does, when it doesn't)
        rows, seen = [], set()
        handled = config.handled_employers()  # (where the desk goes all the way, in the live check)
        tracked_at = [t.find_by_url(item["url"]) for item in self.listings]
        # one row per posting: an opening on the employer's site that Claude saved from Indeed already is that job's
        # row (Apply makes them one job, applied for on the employer's site: Tracker.upsert)
        copies = iter(t.same_posting([item for item, job in zip(self.listings, tracked_at) if job is None]))
        also = t.aliases()
        # the openings the person said Not interested in (shown only in that group of the bar), and the employers they
        # said it of for reason Company, whose other openings score lower
        marks = t.not_interested_marks(self.listings)  # (by the job each row is too: NotInterested)
        employers = unwanted_employers(marks)
        since = self.seen_since
        # the person's home town, for each opening's distance ("Within __ miles"), or why there's none
        home, home_problem = places.home(prof)
        if profile_problem:  # (a profile.yaml that doesn't read: not one without a home town)
            home_problem = ("Within N miles can't find your home town just now: your profile can't be read "
                            f"({profile_problem}).")

        def news(url: str, job: dict[str, Any] | None, run: Any) -> tuple[float | None, bool]:
            """When an opening was first saved (found by a search, or saved to the tracker), and whether it's New: first
            saved since the visit before this one, and not taken up (begun, sent or skipped) since."""
            at = [s for s in (self.found.get(url), _iso_seconds(job.get("created_at")) if job else None)
                  if s is not None]
            first = min(at) if at else None
            return first, (since is not None and first is not None and first > since and run is None
                           and (job is None or job.get("status") not in DONE))

        for item, job in zip(self.listings, tracked_at):
            copy = next(copies) if job is None else None
            job = job or copy
            jid = job["id"] if job else None
            seen.add(jid)
            run = runs.get(jid) if jid is not None else None
            said = marks.mark(item["url"], job, also.get(jid, []) if jid is not None else ()) if marks else None
            first, new = news(item["url"], job, run)
            rows.append({
                "url": item["url"], "title": item.get("title"), "company": item.get("company"),
                "location": item.get("location"), "posted": item.get("posted"),
                "posted_days": days_since(str(item.get("posted") or "")),
                "pay": pay_of(item, job), "ats": item.get("ats") or (job or {}).get("ats") or "",
                "system": system_of(item, job), "workplace": workplace_of(item, job),
                "job_types": job_types_of(item, job),
                "miles": places.away(home, item.get("location"), (item.get("posting") or {}).get("location"),
                                     (job or {}).get("location")) if home else None,
                "fit": item.get("fit") if said else fit_for(item.get("fit"), item.get("company"), employers),
                "notes": item.get("notes") or [], "job_id": jid, "status": job["status"] if job else None,
                "run": run,
                "handled": str(item.get("company") or "").strip().casefold() in handled,
                "also_on": _also_on(([copy["url"]] if copy else []) + (also.get(jid, []) if jid is not None else [])),
                "saved_at": first, "new": new and not said,
                "not_interested": {"reason": said["reason"], "at": said["at"]} if said else None,
            })
        tracked = t.list(limit=300)
        # what the person applied to, and where each stands (the page's My applications): the newest email logged for
        # it, how many there are, whether it's time to follow up, and its group
        heard = t.last_heard()
        applications = [{**{k: j.get(k)
                            for k in ("id", "title", "company", "url", "status", "applied_at", "updated_at")},
                         **standing(j, heard.get(j["id"]), settings.follow_up_days)}
                        for j in tracked if j["status"] in APPLICATION_STATUSES]
        # jobs added another way (Claude from Indeed or LinkedIn, or by hand) and not finished yet
        for brief in tracked[:200]:
            if brief["id"] in seen or brief["status"] not in ("saved", "in_progress", "ready_to_submit"):
                continue
            job = t.get(brief["id"]) or brief
            seen.add(job["id"])
            listing = {"title": job.get("title"), "location": job.get("location"), "posted": job.get("posted_at"),
                       **{k: job.get(k) for k in PAY_KEYS}}
            said = marks.mark(job["url"], job, also.get(job["id"], [])) if marks else None
            fit = score_listing(listing, prof, job.get("description") or "").to_dict()
            first, new = news(job["url"], job, runs.get(job["id"]))
            rows.append({
                "url": job["url"], "title": job.get("title"), "company": job.get("company"),
                "location": job.get("location"), "posted": job.get("posted_at"),
                "posted_days": days_since(str(job.get("posted_at") or "")),
                "pay": pay_of(None, job), "ats": job.get("ats") or "", "system": system_of(None, job),
                "workplace": workplace_of(None, job), "job_types": job_types_of(None, job),
                "miles": places.away(home, job.get("location")) if home else None,
                "fit": fit if said else fit_for(fit, job.get("company"), employers), "notes": [],
                "job_id": job["id"], "status": job["status"], "run": runs.get(job["id"]), "added": True,
                "handled": str(job.get("company") or "").strip().casefold() in handled,
                "also_on": _also_on(also.get(job["id"], [])),
                "saved_at": first, "new": new and not said,
                "not_interested": {"reason": said["reason"], "at": said["at"]} if said else None,
            })
        others = []
        for jid, run in runs.items():
            if jid not in seen:
                job = t.get(jid, with_description=False) or {}
                others.append({"job_id": jid, "title": job.get("title") or run["title"], "company": job.get("company"),
                               "url": job.get("url"), "status": job.get("status"), "run": run})
        return {
            "profile": {"name": prof.full_name, "missing": prof.missing_required(), "gaps": prof.profile_gaps(),
                        "titles": prof.get("preferences.titles") or [], "path": str(config.profile_path()),
                        # the highest degree it says the person has (for the filter row's "My degree is enough")
                        "degree": applicant_degree(prof),
                        # whether it says they need a work visa sponsored (for a card's "won't sponsor a work visa")
                        "requires_sponsorship": prof.get("work_authorization.requires_sponsorship") is True,
                        # the home town each opening's distance is from ("Within __ miles"), or why there's none
                        "home": home.label if home else None, "home_problem": home_problem},
            "settings": {"submit_mode": settings.submit_mode, "dry_run": settings.dry_run,
                         "manage_accounts": settings.may_manage_accounts,
                         "manage_accounts_chosen": settings.manage_accounts_chosen,
                         "auto_submit": self.applier.auto_submit, "tailor_resumes": self.applier.tailor,
                         "cover_letters": self.applier.letters,
                         "cover_letters_approved": self.applier.letters_approved,
                         "handled_only": self.handled_only, "troubleshoot": self.troubleshoot,
                         "min_pay": self.min_pay, "posted_within": self.posted_within,
                         "within_miles": self.within_miles, **self.chips,
                         "auto_search": self.auto_search, "jobs_at_once": self.applier.at_once(),
                         "follow_up_days": settings.follow_up_days},
            "tailoring": len(self.applier.tailoring()),
            # why no cover letter is being written (the switch on), said once at the top of the page
            "letters_problem": self.applier.letters_problem if self.applier.letters else None,
            # saved or not, never the value
            "passwords": {name.removesuffix("_password"): saved for name, saved in
                          _saved([f"{ats}_password" for ats in SITE_PASSWORDS] + ["email_password"]).items()},
            "password_systems": password_systems(),
            "mail_problem": self.applier.mail_problem,
            "profile_problem": profile_problem,
            # a settings line that does nothing (a typo, a value it can't read), said where the person looks
            "settings_warnings": list(settings.warnings),
            "tracker_problem": tracker_problem,
            "tracker_damaged": damaged(tracker_problem or ""),
            "tracker_path": str(config.db_path()),
            "version": config.plugin_version(),
            "update": self.updates.notice(),
            "answers_problem": config.answers_problem(),
            "search": self.search,
            # the person's last look at the openings, and the one before this visit (New is what came after that)
            "seen": {"since": self.seen_since, "last": self.last_seen},
            "listings": rows,
            "others": others,
            "applications": applications,
            "bank": autofill.common_questions(prof),  # the common questions, with the person's answers
            "area": search_area(),  # where Find jobs searches
            "current": self.applier.current,
            "queued": [jid for kind, jid in self.applier.tasks if kind == "apply"],
            "paused": self.applier.paused,  # (Pause queue: nothing new starts until Resume queue)
            # the jobs back in the queue after a restart, waiting for Resume queue; and why the queue isn't kept here,
            # if it isn't (another desk keeps it, or the folder allows no lock)
            "restored": self.applier.restored_notice(),
            "queue_note": self.applier.queue_note(),
            # (a queue this folder can't keep is a problem, said in red; one another desk keeps is only so)
            "queue_note_problem": self.applier.queue_note() == QUEUE_NO_LOCK,
            "counts": t.counts(),
            "notes": report.notes_count(),  # notes on the desk's stops, waiting to be filed
        }

    def safety(self) -> dict[str, Any]:
        """The Safety window: each setting that decides what the desk does in the person's name, with its value now,
        what that means for them in plain words, and where to change it; and the rules no setting changes. Read-only.
        Each value is the one the desk acts on, from its own functions (the pipeline's _may_accept_notices,
        _may_manage_accounts and _test_identity, the browser's _cookie_setting, the Applier's Submit for me, hold on
        sending, Jobs at once and inbox), so the window can't say otherwise: a profile.yaml that can't be read turns
        what it allows off until it's fixed, as the desk does. One of those that fails to answer (a file another program
        has locked) makes its own line "can't tell right now", never the window fail."""
        try:
            prof, problem = config.Profile.load(), None
        except Exception as e:  # said at the top: the desk can't fill an application until it's fixed
            prof, problem = config.Profile({}), _first_line(e)
        s, a = prof.settings, self.applier
        unread = problem is not None
        fix = "off until profile.yaml is fixed"
        unknown = "can't tell right now"
        busy = ("The desk couldn't read this setting just now (is profile.yaml open in another program?). Open Safety "
                "again in a moment.")
        profile = "profile.yaml, under settings:"

        def tell(ask: Any) -> bool | None:
            """What one of the desk's own functions says, or None when it fails to answer."""
            try:
                return bool(ask())
            except Exception:
                return None

        # practice mode, as submit_application's policy has it (the environment's hard switch whatever the profile says)
        dry = config.never_submit() or (not unread and s.dry_run)
        identity = tell(_test_identity)  # (the live check's test identity: accounts and notices in practice mode too)
        notices, accounts, cookies = tell(_may_accept_notices), tell(_may_manage_accounts), tell(_cookie_setting)
        lines: list[dict[str, str]] = []

        def line(key: str, name: str, value: str, said: str, where: str) -> None:
            lines.append({"key": key, "name": name, "value": value, "said": said, "where": where})

        if dry:
            # what practice mode still does for the person, by the same functions as the lines below
            if notices is None or accounts is None or cookies is None:
                does = " Whether it makes accounts or agrees to anything for you can't be told right now."
            else:
                agrees = "agrees to notices for you" if notices else "agrees to nothing for you"
                if cookies:
                    agrees += (", and accepts" if notices else " but") + " a cookie banner with no way to decline " \
                                                                         "(Accept cookies is on)"
                does = f" It {'makes accounts' if accounts else 'makes no accounts'} and {agrees}."
            line("practice", "Practice mode", "on",
                 "Nothing is sent: the desk fills each application up to its review page and never submits it." + does
                 + (" (The live check's test identity: Submit stays refused.)" if identity else ""),
                 "JOB_APPLY_NEVER_SUBMIT=1 in the environment, which comes before profile.yaml" if config.never_submit()
                 else f"{profile} submit_mode (dry_run is practice mode; review applies for real)")
        elif unread:
            line("practice", "Practice mode", "can't tell until profile.yaml is fixed",
                 "Your profile can't be read, so the desk fills and sends nothing until it's fixed.",
                 f"{profile} submit_mode")
        else:
            auto = ", ".join(s.auto_submit_ats) or "none listed under auto_submit_ats, so it asks you everywhere"
            line("practice", "Practice mode", "off",
                 "Applications can be sent: when you press Submit on the desk, by Submit for me when it's on, or when "
                 "you tell Claude to submit one in the chat."
                 + (" submit_mode is auto: when Claude fills an application with you in the chat, it submits one "
                    f"that's complete without asking on these job systems: {auto}. Never LinkedIn or Indeed, and the "
                    "desk itself still waits for your Submit or Submit for me." if s.submit_mode == "auto" else ""),
                 f"{profile} submit_mode (dry_run is practice mode: nothing is sent)")
        submit_box = "The Submit for me box under Openings (kept in desk.json)"
        if dry:
            line("auto_submit", "Submit for me", "off in practice mode",
                 "Practice mode sends nothing: the desk stops at each review page.", submit_box)
        elif unread:
            line("auto_submit", "Submit for me", fix, "Nothing is sent while your profile can't be read.", submit_box)
        elif a.hold_submits:
            line("auto_submit", "Submit for me", "off while Troubleshooting is on",
                 "Troubleshooting is on, so nothing is submitted: not by your Submit, nor for you.", submit_box)
        elif a.auto_submit:
            line("auto_submit", "Submit for me", "on",
                 "A job you apply to while it's on is submitted for you once the desk has filled it to its review page "
                 "and nothing needs you. One that still needs something (a CAPTCHA, errors on its review page, answers "
                 "it changed from a saved draft) waits there for your Submit. LinkedIn and Indeed are always yours.",
                 submit_box)
        else:
            line("auto_submit", "Submit for me", "off",
                 "The desk stops at each review page; you press Submit.", submit_box)
        line("boards", "LinkedIn and Indeed", "always yours",
             "The desk fills LinkedIn and Indeed applications but never presses their Submit, whatever the settings: "
             "you do, in the desk's browser window.", "Not a setting: always so")
        where = f"{profile} accept_notices (on unless set to false)"
        if notices is None:
            line("accept_notices", "Agree to notices", unknown, busy, where)
        elif notices:
            line("accept_notices", "Agree to notices", "on",
                 "The desk agrees for you to an employer's notice about AI screening, its privacy notice and its terms "
                 "of use, and picks an application's attestation that its information is true (or consent to the "
                 "background check that comes with applying), and logs each. Never a newsletter, job alerts, a talent "
                 "community or marketing; cookie banners have their own setting. Any other dialog over the form stops "
                 "the desk for you.", where)
        elif unread:
            line("accept_notices", "Agree to notices", fix,
                 "Notices, terms and attestations wait for you until it's fixed.", where)
        elif s.accept_notices:  # (on, but not in practice mode)
            line("accept_notices", "Agree to notices", "off in practice mode",
                 "Practice mode agrees to nothing: notices, terms and attestations wait for you.", where)
        else:
            line("accept_notices", "Agree to notices", "off",
                 "Notices, terms and attestations are yours: the desk stops for you at each one.", where)
        where = f"{profile} manage_accounts (on unless set to false)"
        if accounts is None:
            line("manage_accounts", "Make accounts", unknown, busy, where)
        elif accounts:
            line("manage_accounts", "Make accounts", "on" if s.manage_accounts_chosen else
                 "on (your profile doesn't say, so it's on by default)",
                 "Where an employer's site needs an account, the desk creates yours there: with the job system's "
                 "password saved under Site passwords, or with the code the site emails where it asks for no password "
                 "(Eightfold's), ticking only the site's terms (never a newsletter). It resets a saved password a site "
                 "refuses through the site's own \"Forgot password\" email. A consent to be considered for other "
                 "positions is given only where the site won't make the account without it, never when it also signs "
                 "you up for job alerts, a newsletter, a talent community or marketing. A CAPTCHA or security question "
                 "on the way stays yours.", where)
        elif unread:
            line("manage_accounts", "Make accounts", fix,
                 "The desk makes no account and resets no password until it's fixed.", where)
        elif s.manage_accounts:  # (on, but not in practice mode)
            line("manage_accounts", "Make accounts", "off in practice mode",
                 "Practice mode makes no accounts: Create Account is left to you.", where)
        else:
            line("manage_accounts", "Make accounts", "off",
                 "The desk never creates an account or resets a password: it fills in Create Account and leaves the "
                 "rest to you.", where)
        line("accept_cookies", "Accept cookies",
             unknown if cookies is None else "on" if cookies else fix if unread else "off",
             busy if cookies is None else
             "Cookie banners are declined; one with no way to decline is accepted for you." if cookies else
             "Cookie banners are declined, or left to you when there's no way to decline: never accepted.",
             f"{profile} accept_cookies (off unless set to true)")
        where = f"{profile} email_codes (off unless set to true)"
        if s.email_codes and not unread:
            line("email_codes", "Emailed codes (Claude)", "on",
                 "Claude may read a sign-up or verification code, or a verification link, that a career site emails "
                 "you, through the Gmail connector: when you apply with Claude in the chat, and for the desk while its "
                 "chat is open and no email app password of yours works here (the desk asks Claude for a job's own "
                 "mail since its wait began, checks it as it does your inbox's, and puts the code in itself). Only the "
                 "newest message from that job's site or its job system, a link only when it goes back to that site, "
                 "and no other mail. Never a password reset's email: that's the email app password's alone.", where)
        else:
            line("email_codes", "Emailed codes (Claude)", fix if unread else "off",
                 "When you apply with Claude in the chat, Claude asks you for a code or link a career site emails you; "
                 "on the desk you enter it yourself, unless its email app password reads it.",
                 where)
        saved = _saved(["email_password"])["email_password"]
        before = a.mail_problem
        # (the desk's own way into its inbox; what it would say of one it can't use isn't put on the page from here)
        try:
            usable, why = a.mail_login() is not None, a.mail_problem
        except Exception:  # (a file it reads, locked just now)
            usable, why = False, "The desk couldn't look just now (a file it reads may be open in another program)."
        finally:
            a.mail_problem = before
        where = "The Site passwords card (Your email)"
        if saved and usable:
            line("email_password", "Email app password (the desk)", "saved",
                 "The desk reads only the sign-up codes and links a job's own site emails you"
                 + (", and the password-reset email it just asked that site for" if accounts else "")
                 + ", opened read-only. Nothing else in your inbox, and Claude never sees the password.", where)
        elif saved:
            line("email_password", "Email app password (the desk)", "saved, but the desk can't use it now",
                 (why or "Your profile has no email address it can read mail for, or your mail service turned the "
                  "password down.")
                 + " Until then, a site that emails a sign-up code or link waits for you to enter it.",
                 where)
        else:
            line("email_password", "Email app password (the desk)", "not saved",
                 "A site that emails a sign-up code or link waits for you to enter it.", where)
        line("email_tracking", "Replies to your applications",
             "on" if s.email_tracking and not unread else fix if unread else "off",
             "Claude may read your email through the Gmail connector for employers' replies to your applications, "
             "and logs them under My applications. Never with the desk's email app password; a follow-up is only "
             "ever a draft for you to send." if s.email_tracking and not unread else
             "Claude doesn't read your email for replies.", f"{profile} email_tracking (off unless set to true)")
        n = a.at_once()
        line("jobs_at_once", "Jobs at once", str(n),
             ("The desk fills one application at a time." if n == 1 else
              f"The desk fills up to {n} applications at the same time, each in its own tab.")
             + " Never two at one employer's site, and only one at a time signs in, makes an account or waits on an "
               "emailed code.",
             "The Jobs at once buttons under Openings"
             + (" (your choice there is in force)" if a.jobs_at_once is not None
                else f", or {profile} jobs_at_once"))
        if a.hold_submits:
            said = ("Nothing is submitted, by your Submit or for you. Find jobs searches every list's employers and "
                    "shows only the openings where the desk doesn't go all the way yet; "
                    + ("nothing is filled until profile.yaml is fixed." if unread else "the desk still fills each step"
                       + (" and makes accounts" if accounts else "") + ", which the sites keep."))
        else:
            said = ("Practice mode sends nothing either way." if dry else
                    "Nothing is sent while your profile can't be read." if unread else "The desk applies for real.")
        line("troubleshoot", "Troubleshooting", "on" if a.hold_submits else "off", said,
             "The Troubleshooting box under Openings, More")
        letters_box = "The Write a cover letter for each job box under Openings (kept in desk.json)"
        if a.letters:
            line("cover_letters", "Cover letters", "on" if a.letters_approved else "on (the first waits for your OK)",
                 "Claude writes a cover letter for each job you apply to, through your own Claude Code sign-in (your "
                 "Claude plan; no key needed), from your resume, your profile and the job's posting, using only what "
                 "they say: never a degree, certification, clearance, years or skill they don't state. It runs with no "
                 "tools and none of your MCP servers, and the posting is only its data. The letter goes into the job's "
                 "folder and into a cover letter box"
                 + (". " if a.letters_approved else
                    "; the first one waits on its job's card for your Use it, and the rest go in without asking. ")
                 + "Where one can't be written, the job goes on with your usual cover letter, if you have one.",
                 letters_box + f", and {profile} cover_letter_model for the model")
        else:
            line("cover_letters", "Cover letters", "off",
                 "No cover letter is written for you: your usual one (documents.cover_letter in profile.yaml), if you "
                 "have one, goes with every application that asks.", letters_box)
        line("bot_checks", "Bot checks and CAPTCHAs", "always yours",
             "The desk never gets around a bot check and never solves a CAPTCHA: it pauses the job and brings its tab "
             "up for you.", "Not a setting: always so")
        line("passwords", "Passwords", "always kept to their own sites",
             "A password saved on the desk is typed only into its own job system's sites, stays on this computer, and "
             "Claude never sees it. Never type one into the chat.", "Not a setting: always so")
        return {"lines": lines, "problem": problem}


# job systems with no saved password of their own that email a sign-up code or link (Oracle Recruiting's codes), beside
# the ones the desk keeps a password for, whose accounts are made with an emailed link or code
MAILS_CODES = {"oracle_hcm"}


_UNREAD = object()  # (a profile.yaml that doesn't read just now)


def _profile_jobs_at_once() -> Any:
    """profile.yaml's settings.jobs_at_once as written there (None where it doesn't say), or _UNREAD while the profile
    doesn't read: what the desk's own choice was made over, so a later change there is seen (never the code's default,
    which may change)."""
    try:
        settings = config.Profile.load().get("settings")
    except Exception:
        return _UNREAD
    return settings.get("jobs_at_once") if isinstance(settings, dict) else None


def _first_line(e: BaseException) -> str:
    return ((str(e).strip().splitlines() or [type(e).__name__])[0])[:150]


def _also_on(urls: list[str]) -> list[dict[str, str]]:
    """A job's other addresses, for its row's "Also on Indeed": each with the job board it's on."""
    return [{"site": JOB_BOARDS.get(job_board(u) or "", "another site"), "url": u} for u in dict.fromkeys(urls)]


class _NoJobs:
    """The tracker's stand-in while tracker.db can't be read (Desk.state): no jobs, no marks."""

    def find_by_url(self, url: str) -> None:
        return None

    def same_posting(self, items: list[dict[str, Any]]) -> list[None]:
        return [None] * len(items)

    def aliases(self) -> dict[int, list[str]]:
        return {}

    def not_interested_marks(self, listings: Any) -> NotInterested:
        return NotInterested([])

    def list(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def last_heard(self) -> dict[int, Any]:
        return {}

    def get(self, *args: Any, **kwargs: Any) -> None:
        return None

    def counts(self) -> dict[str, int]:
        return {}


def unwanted_employers(marks: NotInterested) -> dict[str, str]:
    """The employers the person said Not interested in for reason Company (Tracker.not_interested_marks), by
    company_key, each with its name as the opening gave it."""
    return {company_key(said["company"]): said["company"] for said in marks.by_url.values()
            if said.get("reason") == "company" and company_key(said.get("company") or "")}


def fit_for(fit: Any, company: Any, employers: dict[str, str]) -> Any:
    """An opening's fit, lower at an employer the person said Not interested in (recommend.not_interested_in)."""
    name = employers.get(company_key(str(company or "")))
    return not_interested_in(fit, name) if name and isinstance(fit, dict) else fit


# job systems no employer is listed as using: not one the desk can be said to go through
_NO_SYSTEM = frozenset({"", "unknown", "custom", "company_site"})


def usual_systems() -> set[str]:
    """The job systems the desk usually goes all the way on: more than half of their employers on the plugin's lists
    (two or more of them) are ones where the live check took a test applicant to the review page (desk_handled.yaml)."""
    handled = config.handled_employers()
    try:
        employers = every_employer()
    except (OSError, ValueError):  # (a companies.yaml with a mistake: no system said to be usual)
        return set()
    total: Counter[str] = Counter()
    through: Counter[str] = Counter()
    for c in employers:
        ats = str(c.get("ats") or "")
        if ats in _NO_SYSTEM:
            continue
        total[ats] += 1
        through[ats] += str(c.get("name") or "").strip().casefold() in handled
    return {ats for ats, n in total.items() if n >= 2 and through[ats] * 2 > n}


def bot_check_employers() -> set[str]:
    """The employers (in lower case) where, in the desk's history (runs.jsonl), passes stopped at a bot check more often
    than they reached the review page: the person's to get through there, most likely."""
    stops: Counter[str] = Counter()
    through: Counter[str] = Counter()
    for e in history.read():
        who = str(e.get("company") or "").strip().casefold()
        if who and e.get("outcome") == "needs_you" and e.get("need") == "bot_check":
            stops[who] += 1
        elif who and e.get("outcome") in ("ready", "submitted"):
            through[who] += 1
    return {who for who, n in stops.items() if n > through[who]}


def likely_order(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Jobs queued together (Apply to selected), the ones most likely to go all the way first: at employers where the
    live check went all the way (desk_handled.yaml); then on job systems the desk usually goes all the way on
    (usual_systems); then the rest; and last, the ones that need the person anyway: LinkedIn's and Indeed's (whose
    Submit is always theirs), and employers where the desk's own history met bot checks more often than not
    (bot_check_employers). Each tier keeps the person's order."""
    handled, systems, bots = config.handled_employers(), usual_systems(), bot_check_employers()

    def tier(job: dict[str, Any]) -> int:
        who = str(job.get("company") or "").strip().casefold()
        urls = [str(job.get(k) or "") for k in ("url", "apply_url") if job.get(k)]
        systems_of = {str(job.get("ats") or "")} | {detect_ats(u) for u in urls}
        if systems_of & config.HUMAN_SUBMIT_ONLY or who in bots:
            return 3
        if who in handled:
            return 0
        return 1 if systems_of & systems else 2

    return sorted(jobs, key=tier)  # (sorted keeps the order of the ones alike)


PAY_KEYS = pay.FIELDS
POSTED_WITHIN = (7, 14, 30)  # the page's "Posted" choices, in days (none: any day)
MIN_PAY_MOST = pay.MIN_PAY_MOST  # a "Pay at least" over this is a typo
WITHIN_MILES = places.WITHIN_MILES  # the page's "Within" choices, in miles from home (none: any distance)
# The filter row's chips, by the setting each is kept as (desk.json, /api/settings), with what each may hold: the
# requirements an opening may not ask (posting_facts: no clearance, no degree above the person's, a day shift, travel
# of 25% or less, an entry-level or not a senior title)
CHIP_FILTERS: dict[str, tuple[str, ...]] = {
    "requirements": ("no_clearance", "degree", "day_shift", "travel_25", "entry_level", "not_senior"),
    # where the job is done (workplace.py): an opening that says another, surely, is hidden
    "workplace": workplace.WORKPLACES,
    # its employment type (employment.py): an opening that says only others is hidden
    "job_types": employment.TYPES,
    # the job system it's applied for on (system_of): an opening on another is hidden. Any system's key: the chips are
    # the systems of the openings listed
    "systems": (),
}
_SYSTEM_KEY = re.compile(r"[a-z0-9_]{1,40}")
# the System chips' names: the job systems' own (ats.ATS_NAMES), short, and the few the search names that it hasn't
SYSTEM_NAMES = {**ATS_NAMES, "successfactors": "SuccessFactors", "taleo": "Taleo", "oracle_hcm": "Oracle",
                "company_site": "Employer's own site", "jibe": "iCIMS Jibe", "talemetry": "Talemetry",
                "amazon": "amazon.jobs"}
# what the search calls an employer's own site: one System chip
_OWN_SITE = {"", "custom", "unknown", "company_site"}


def _chips(name: str, value: Any) -> list[str] | None:
    """A chip setting as saved or sent: a list of its choices (CHIP_FILTERS), each once, in their order (for systems,
    any system's key, in the order sent); [] for none (None, or an empty list); None when it isn't one."""
    if value is None:
        return []
    choices = CHIP_FILTERS[name]
    if not isinstance(value, list) or not all(
            isinstance(v, str) and (v in choices if choices else bool(_SYSTEM_KEY.fullmatch(v))) for v in value):
        return None
    return [c for c in choices if c in value] if choices else list(dict.fromkeys(value))


def system_of(item: dict[str, Any] | None, job: dict[str, Any] | None) -> dict[str, str]:
    """The job system an opening is applied for on, for its row's System chip: the one its search or the tracker
    named (Listing.ats), else the one its address is on (ats.detect_ats, as the queue's order reads it); an employer's
    own site (a custom search, or an address no system is known by) is one, "company_site". Its key and name."""
    said = str((item or {}).get("ats") or (job or {}).get("ats") or "").strip().lower()
    if said in _OWN_SITE:
        urls = [str(x.get(k) or "") for x in (item, job) if x for k in ("apply_url", "url") if x.get(k)]
        said = next((s for s in map(detect_ats, urls) if s not in _OWN_SITE), "company_site")
    key = said if _SYSTEM_KEY.fullmatch(said) else "company_site"
    return {"key": key, "name": SYSTEM_NAMES.get(key) or key.replace("_", " ").title()}


def _min_pay(value: Any) -> int | None:
    """A "Pay at least $__/yr" as saved or sent: a whole number of dollars a year over 0, else None (any pay)."""
    if type(value) is float and value.is_integer():
        value = int(value)
    return value if type(value) is int and 0 < value <= MIN_PAY_MOST else None


def job_types_of(item: dict[str, Any] | None, job: dict[str, Any] | None) -> dict[str, Any] | None:
    """An opening's employment types as its row says them, or None when nothing says: its list's words, else its
    posting's (once read), else the tracker's job's; each a field the job system gives for it."""
    for said in ((item or {}).get("employment_type"), ((item or {}).get("posting") or {}).get("employment_type"),
                 (job or {}).get("employment_type")):
        types = employment.types_of(said)
        if types:
            return {"types": types, "shown": employment.shown(types), "from": f"The job system says “{said}”"}
    return None


def workplace_of(item: dict[str, Any] | None, job: dict[str, Any] | None) -> dict[str, Any] | None:
    """Where an opening is done as its row says it (workplace.view), or None when it doesn't say: the better said of
    the opening's own (its list's, or its posting's once read) and the tracker's job's."""
    best: dict[str, Any] = {}
    for said in (item, job):
        if said and workplace.better(said, best):
            best = {k: said.get(k) for k in workplace.FIELDS}
    return workplace.view(best.get("workplace"), best.get("workplace_source")) if best else None


def pay_of(item: dict[str, Any] | None, job: dict[str, Any] | None) -> dict[str, Any] | None:
    """An opening's pay as its row says it (pay.view), or None when it isn't listed: the opening's own (its list's, or
    its posting's once read: recommend's take), else the tracker's job's."""
    for said in (item, job):
        if said and said.get("pay_period"):
            return pay.view(said, (said.get("posting") or {}).get("salary") or said.get("salary") or "")
    return None


def whats_new(tracker: Any, since: float) -> list[dict[str, Any]]:
    """The openings first saved at `since` (seconds) or after, best score first: the desk's (its last search's,
    recommendations.json, each by when a search first found it: openings_found.json) and the jobs saved to the tracker
    another way (by Claude, from Indeed or a link), none of them begun, sent or skipped since, and none the person said
    Not interested in. Each with its title, company, location, score (lower at an employer they said Not interested in),
    whether it's recommended, its address, its job number once saved, when it was first saved, its pay in a few words
    ("" when it isn't listed) with the most it pays a year (for "from $X", X a year), and its applicant tracking
    system."""
    try:
        saved = json.loads(_recommendations_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    results = saved.get("results") if isinstance(saved, dict) else None
    listings = [r for r in results if isinstance(r, dict) and isinstance(r.get("url"), str)] \
        if isinstance(results, list) else []
    searched, found = _seconds(saved.get("at")) if isinstance(saved, dict) else None, read_found()
    marks = tracker.not_interested_marks(listings)  # (by the job each is too: NotInterested)
    employers = unwanted_employers(marks)
    also = tracker.aliases()
    out: list[dict[str, Any]] = []
    taken: set[Any] = set()

    def add(url: str, job: dict[str, Any] | None, title: Any, company: Any, location: Any, fit: Any,
            first: float | None, item: dict[str, Any] | None = None) -> None:
        key = normalize_url(url)
        if key in taken or (job is not None and job["id"] in taken):
            return
        taken.update({key, *([job["id"]] if job is not None else [])})
        if job is not None:  # (and its other addresses: one posting, one line)
            taken.update(normalize_url(u) for u in also.get(job["id"], []))
        unwanted = marks.mark(url, job, also.get(job["id"], []) if job is not None else ()) if marks else None
        if first is None or first < since or unwanted or (job is not None and job.get("status") != "saved"):
            return
        fit = fit_for(fit if isinstance(fit, dict) else {}, company, employers)
        shown = pay_of(item, job)
        out.append({"title": title or "", "company": company or "", "location": location or "",
                    "score": fit.get("score"), "recommended": bool(fit.get("recommended")), "url": url,
                    "job_id": job["id"] if job is not None else None,
                    "saved_at": history.stamp(first),
                    # its pay in a few words ("" for not listed) and a year's most, and its applicant tracking system
                    "pay": shown["shown"] if shown else "", "pay_yearly": shown["sort"] if shown else None,
                    "ats": (item or {}).get("ats") or (job or {}).get("ats") or ""})

    tracked = [tracker.find_by_url(item["url"]) for item in listings]
    # (an opening on the employer's site not saved yet is the job saved from a job board that it surely is, as the desk
    # lists them: Tracker.same_posting)
    copies = iter(tracker.same_posting([item for item, job in zip(listings, tracked) if job is None]))
    for item, job in zip(listings, tracked):
        job = job or next(copies)
        at = [s for s in (found.get(item["url"], searched), _iso_seconds(job.get("created_at")) if job else None)
              if s is not None]
        add(item["url"], job, item.get("title"), item.get("company"), item.get("location"), item.get("fit"),
            min(at) if at else None, item)
    try:
        prof = config.Profile.load()
    except Exception:  # (a profile with a typo: scored as for no one, as the desk's page does)
        prof = config.Profile({})
    for brief in tracker.list(status="saved", limit=500):
        first = _iso_seconds(brief.get("created_at"))
        if first is None or first < since or brief["id"] in taken or normalize_url(brief["url"]) in taken:
            continue
        job = tracker.get(brief["id"]) or brief
        listing = {"title": job.get("title"), "location": job.get("location"), "posted": job.get("posted_at"),
                   **{k: job.get(k) for k in PAY_KEYS}}
        add(job["url"], job, job.get("title"), job.get("company"), job.get("location"),
            score_listing(listing, prof, job.get("description") or "").to_dict(), first)
    out.sort(key=lambda o: -(o["score"] or 0))  # (sorted keeps the search's order among equals)
    return out


def _saved(names: list[str]) -> dict[str, bool]:
    """Which of these secrets are saved, from one reading of secrets.yaml (the page asks
    every 1.5 s): never their values."""
    try:
        path = config.secrets_path()
        data = config.read_secrets(path) if path.exists() else {}
    except Exception:  # a hand-edited secrets.yaml with a typo mustn't break the page
        data = {}
    env = {k for k, v in os.environ.items() if k.startswith("JOB_APPLY_SECRET_") and v}
    return {n: f"JOB_APPLY_SECRET_{re.sub(r'[^A-Z0-9]', '_', n.upper())}" in env or data.get(n) is not None
            for n in names}


TROUBLESHOOT_TITLES = ["human resources", "recruiter"]  # troubleshooting's HR jobs, beside the profile's titles
MAX_LINKS = 20  # pasted at once; a person's own picks, not a crawl
# The job systems whose password the page lets the person save (the employers on the list
# use these): the desk signs in with each on that system's own sites only (PASSWORD_SITES).
SITE_PASSWORDS = tuple(name.removesuffix("_password") for name in DESK_PASSWORDS)
_systems: tuple[float | None, list[dict[str, str]]] | None = None


def password_systems() -> list[dict[str, str]]:
    """The job systems a password can be saved for, each named with the employers on the
    person's own lists that use it ("Workday: Banner Health, HonorHealth, Arizona State
    University and 21 more"): the Phoenix list's for someone who searches it, not only the
    semiconductor list's. Read again when their companies.yaml changes."""
    global _systems
    own = own_companies_path()
    try:
        key = own.stat().st_mtime if own.exists() else None
    except OSError:
        key = None
    if _systems is not None and _systems[0] == key:
        return _systems[1]
    try:
        companies = load_companies()
    except (OSError, ValueError):  # a companies.yaml with a mistake: the systems without names
        companies = []
    out = []
    for name, system in DESK_PASSWORDS.items():
        ats = name.removesuffix("_password")
        users = [str(c.get("name")) for c in companies if c.get("ats") == ats and c.get("name")]
        more = f" and {len(users) - 3} more" if len(users) > 3 else ""
        out.append({"value": ats, "label": f"{system}: {', '.join(users[:3])}{more}" if users else system})
    _systems = (key, out)
    return out

def troubleshoot_query(query: str) -> str:
    """What troubleshooting searches for: the profile's first two titles, and HR jobs (the Phoenix list's
    employers mostly hire for those), so every list's employers have openings to try the desk on."""
    titles = [t for t in alternatives(query) if t][:2]
    extra = [t for t in TROUBLESHOOT_TITLES if t not in titles]
    return " | ".join([*titles, *extra][:MAX_ALTERNATIVES])


_desk: Desk | None = None


def first_letter_waits(job: dict[str, Any]) -> bool:
    """Does this job's first cover letter wait for the person's say on its card (its draft in its folder, their OK not
    given: the running desk's, or desk.json's while none runs)? Then render_document puts a letter for it in the draft,
    never in the folder as one to upload."""
    if _desk is not None:
        return _desk.applier.letter_waits(job)
    draft = Path(job["folder"]) / letters.DRAFT if job.get("folder") else None
    if draft is None or not draft.is_file():
        return False
    try:
        return json.loads(_settings_path().read_text(encoding="utf-8")).get("cover_letters_approved") is not True
    except (OSError, ValueError, AttributeError):
        return True


def listed_openings() -> list[dict[str, Any]]:
    """The openings the desk lists: the running desk's, or its last search's (recommendations.json): what makes a Not
    interested mark made before its job was saved ambiguous (Tracker.not_interested_marks)."""
    if _desk is not None:
        return list(getattr(_desk, "listings", None) or [])  # (a desk still starting has none yet)
    try:
        saved = json.loads(_recommendations_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    results = saved.get("results") if isinstance(saved, dict) else None
    return [r for r in results if isinstance(r, dict) and isinstance(r.get("url"), str)] \
        if isinstance(results, list) else []


def get_desk(srv: Any) -> Desk:
    global _desk
    if _desk is None:
        _desk = Desk(srv)
    return _desk


def main(argv: list[str] | None = None) -> int:
    """Run the Job Desk on its own (without Claude Code)."""
    ap = argparse.ArgumentParser(description="Open the Job Desk")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="don't open the page in your browser")
    args = ap.parse_args(argv)
    from . import server

    desk = get_desk(server)

    async def run() -> None:
        url = await desk.start(port=args.port, open_browser=not args.no_browser)
        print(f"Job Desk: {url}\nLeave this window open while you use it; Ctrl+C stops it.", flush=True)
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await desk.stop()
            await server.browser.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
