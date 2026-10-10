"""The Job Desk: a page on this computer that lists recommended openings and applies to
the ones picked with one button.

It is served by the plugin on 127.0.0.1 and drives the same browser session as Claude's
tools. Every API call needs the random token in the page's address and a local Host
header, so other websites open in the same browser can't reach it.

    uv run --project <plugin>/server job-apply-desk      # on its own, without Claude
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import secrets
import socket
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import config, report
from .pipeline import DESK_PASSWORDS, Applier, question_key
from .ats import detect_ats
from .postings import FetchError, Posting, fetch_posting, finalize, parse_html
from .recommend import recommend, score_listing
from .search import load_companies, own_companies_path
from .updates import UpdateCheck

PAGE = Path(__file__).resolve().parent / "static" / "desk.html"
DEFAULT_PORT = 8765


def _recommendations_path() -> Path:
    return config.home() / "recommendations.json"


def _settings_path() -> Path:
    return config.home() / "desk.json"


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
        self.applier = Applier(srv)
        self.fetch = fetch_posting  # reads each recommended posting's requirements
        self.token = secrets.token_urlsafe(18)
        self.port: int | None = None
        self.listings: list[dict[str, Any]] = []
        self.search: dict[str, Any] = {"status": "idle", "at": None, "error": None, "query": "", "location": None,
                                       "errors": {}, "browser_only": []}
        self._search_task: asyncio.Task | None = None
        self._serve_task: asyncio.Task | None = None
        self.updates = UpdateCheck()  # a newer published version, said on the page
        self._update_task: asyncio.Task | None = None
        self._server: uvicorn.Server | None = None
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
        except (OSError, ValueError, AttributeError):
            pass
        try:
            saved = json.loads(_settings_path().read_text(encoding="utf-8"))
            self.applier.auto_submit = saved.get("auto_submit") is True
            self.applier.tailor = saved.get("tailor_resumes") is True
        except (OSError, ValueError, AttributeError):
            pass

    def _save_settings(self) -> None:
        config.ensure_home()
        _settings_path().write_text(json.dumps({"auto_submit": self.applier.auto_submit,
                                                "tailor_resumes": self.applier.tailor}), encoding="utf-8")

    # ------------------------------------------------------------- serving
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def app(self) -> Starlette:
        r = Route
        return Starlette(routes=[
            r("/", self.page_view),
            r("/api/state", self.state_view),
            r("/api/search", self.search_view, methods=["POST"]),
            r("/api/apply", self.apply_view, methods=["POST"]),
            r("/api/answer", self.answer_view, methods=["POST"]),
            r("/api/job/{job_id:int}/{action}", self.job_view, methods=["POST"]),
            r("/api/notes/{action}", self.notes_view, methods=["POST"]),
            r("/api/settings", self.settings_view, methods=["POST"]),
            r("/api/password", self.password_view, methods=["POST"]),
            r("/api/add", self.add_view, methods=["POST"]),
        ])

    async def start(self, port: int = DEFAULT_PORT, open_browser: bool = True) -> str:
        """Serve the desk in the running event loop (inside the MCP server)."""
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

    async def stop(self) -> None:
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
        return JSONResponse({"error": str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)}, status_code=400)

    async def page_view(self, request: Request) -> Response:
        if not self._allowed(request, api=False):
            return self._forbidden()
        return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    async def state_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        return JSONResponse(self.state(), headers={"Cache-Control": "no-store"})

    async def search_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        if self._search_task is None or self._search_task.done():
            self._search_task = asyncio.get_running_loop().create_task(self.find_jobs())
        return JSONResponse({"started": True})

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
            if not isinstance(job_ids, list) or not all(isinstance(j, int) and not isinstance(j, bool) for j in job_ids):
                raise ValueError("job_ids should be a list of job numbers")
        except ValueError as e:
            return self._bad(e)
        queued, refused = self.apply(urls=urls, job_ids=job_ids, submit=body.get("submit") is True)
        return JSONResponse({"queued": queued, "already_applied": refused})

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
            elif action == "submit":
                a.submit_now(job_id)
            elif action == "show":
                if not await a.focus(job_id):
                    return JSONResponse({"error": "Its tab is closed. Press Resume to open it again."}, status_code=409)
            elif action == "usual_resume":
                a.use_usual_resume(job_id)
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
                return JSONResponse(await asyncio.to_thread(report.build, job, a.runs.get(job_id), anonymous=anonymous))
            else:
                return JSONResponse({"error": f"unknown action {action!r}"}, status_code=404)
        except (KeyError, ValueError) as e:
            return self._bad(e)
        return JSONResponse({"ok": True})

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
        if "auto_submit" in body:
            self.applier.auto_submit = body["auto_submit"] is True  # not "false", which bool() calls true
        if "tailor_resumes" in body:
            self.applier.set_tailor(body["tailor_resumes"] is True)
        if "auto_submit" in body or "tailor_resumes" in body:
            self._save_settings()
        return JSONResponse({"auto_submit": self.applier.auto_submit, "tailor_resumes": self.applier.tailor})

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

    async def find_jobs(self) -> None:
        self.search.update(status="running", error=None, started=time.time())
        try:
            async def run_search(query: str, location: str | None, limit: int) -> dict[str, Any]:
                return await self.srv.search_company_jobs(query, location=location, limit_per_company=limit)

            out = await recommend(config.Profile.load(), run_search, limit_per_company=10, read_postings=40,
                                  fetch=self.fetch, fetch_hard=self.read_in_browser)
            self.listings = out["results"]
            at = time.time()
            self.search.update(status="done", at=at, query=out["query"], location=out["location"],
                               errors=out["errors"], browser_only=out["browser_only"])
            config.ensure_home()
            _recommendations_path().write_text(json.dumps({**out, "at": at}, default=str), encoding="utf-8")
        except Exception as e:
            self.search.update(status="failed", error=f"{type(e).__name__}: {str(e)[:300]}")

    def _listing(self, url: str) -> dict[str, Any] | None:
        return next((item for item in self.listings if item.get("url") == url), None)

    def apply(self, urls: list[str], job_ids: list[int], submit: bool) -> tuple[list[int], list[str]]:
        """Queue these jobs. Returns the ids queued, and the titles of any already applied to,
        which are never queued again (with "Submit for me" on, that would apply twice)."""
        t = self.srv.tracker()
        ids = [int(j) for j in job_ids]
        for url in urls:
            if not re.match(r"(?i)^(https?|file)://", str(url)) or _own_file(str(url)):
                continue  # a listing whose link isn't a web address ("javascript:…"), or is one of the desk's own files
            tracked = t.find_by_url(url)
            if tracked is not None and self._listing(url) is None:
                ids.append(tracked["id"])  # added by Claude or by hand
                continue
            item = self._listing(url) or {"url": url}
            posting = item.get("posting") or {}
            job, _ = t.upsert({
                "url": url, "apply_url": posting.get("apply_url") or "", "title": item.get("title") or posting.get("title"),
                "company": item.get("company"), "location": item.get("location") or posting.get("location"),
                "ats": item.get("ats") or posting.get("ats"), "source": "job desk",
                "external_id": item.get("external_id") or posting.get("external_id"),
                "salary": posting.get("salary"), "employment_type": posting.get("employment_type"),
                "posted_at": posting.get("posted_at") or item.get("posted"), "description": posting.get("description"),
            })
            ids.append(job["id"])
        queued, refused = [], []
        for job_id in dict.fromkeys(ids):
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
            if _own_file(url):  # its text would be saved as a posting, where Claude could read it
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
            job, _ = self.srv.tracker().upsert(posting.to_dict())
            out.append({"url": job["url"], "job_id": job["id"], "title": job.get("title"), "company": job.get("company"),
                        "status": job.get("status"), "warnings": posting.warnings})
        return out

    def answer(self, job_id: int, answers: list[dict[str, Any]]) -> str | None:
        """Fill the person's answers in. Returns a note if some couldn't be remembered (they
        still go into this application)."""
        run = self.applier.runs.get(job_id)
        if run is None or run.status != "needs_you":
            # nothing is remembered for a job that isn't asking (a stale page)
            raise ValueError("That job isn't waiting for answers any more.")
        problem = None
        for a in answers:
            label, value = str(a.get("label") or ""), a.get("value")
            if not label or value in (None, ""):
                continue
            if a.get("remember", True) and " | " not in label:  # (one box's own answer is never remembered)
                try:
                    config.save_answer(label, value, run.company)
                    run.once.pop(question_key(label), None)  # an earlier answer for this application only
                    continue
                except (ValueError, OSError) as e:
                    problem = f"Couldn't remember your answers ({e}); they're used for this application only."
            run.once[question_key(label)] = value
        self.applier.enqueue(job_id, submit=run.submit, front=True)
        return problem

    # ------------------------------------------------------------- state
    def state(self) -> dict[str, Any]:
        try:
            prof, profile_problem = config.Profile.load(), None
        except Exception as e:  # a typo in profile.yaml: said on the page, which keeps working
            prof, profile_problem = config.Profile({}), _first_line(e)
        settings = prof.settings
        t = self.srv.tracker()
        runs = {jid: r.public() for jid, r in self.applier.runs.items()}
        rows, seen = [], set()
        for item in self.listings:
            job = t.find_by_url(item["url"])
            jid = job["id"] if job else None
            seen.add(jid)
            rows.append({
                "url": item["url"], "title": item.get("title"), "company": item.get("company"),
                "location": item.get("location"), "posted": item.get("posted"), "fit": item.get("fit"),
                "notes": item.get("notes") or [], "job_id": jid, "status": job["status"] if job else None,
                "run": runs.get(jid) if jid is not None else None,
            })
        # jobs added another way (Claude from Indeed or LinkedIn, or by hand) and not finished yet
        for brief in t.list(limit=200):
            if brief["id"] in seen or brief["status"] not in ("saved", "in_progress", "ready_to_submit"):
                continue
            job = t.get(brief["id"]) or brief
            seen.add(job["id"])
            listing = {"title": job.get("title"), "location": job.get("location"), "posted": job.get("posted_at")}
            rows.append({
                "url": job["url"], "title": job.get("title"), "company": job.get("company"),
                "location": job.get("location"), "posted": job.get("posted_at"),
                "fit": score_listing(listing, prof, job.get("description") or "").to_dict(), "notes": [],
                "job_id": job["id"], "status": job["status"], "run": runs.get(job["id"]), "added": True,
            })
        others = []
        for jid, run in runs.items():
            if jid not in seen:
                job = t.get(jid, with_description=False) or {}
                others.append({"job_id": jid, "title": job.get("title") or run["title"], "company": job.get("company"),
                               "url": job.get("url"), "status": job.get("status"), "run": run})
        return {
            "profile": {"name": prof.full_name, "missing": prof.missing_required(), "gaps": prof.profile_gaps(),
                        "titles": prof.get("preferences.titles") or [], "path": str(config.profile_path())},
            "settings": {"submit_mode": settings.submit_mode, "dry_run": settings.dry_run,
                         "manage_accounts": settings.may_manage_accounts,
                         "manage_accounts_chosen": settings.manage_accounts_chosen,
                         "auto_submit": self.applier.auto_submit, "tailor_resumes": self.applier.tailor},
            "tailoring": len(self.applier.tailoring()),
            # saved or not, never the value
            "passwords": {name.removesuffix("_password"): saved for name, saved in
                          _saved([f"{ats}_password" for ats in SITE_PASSWORDS] + ["email_password"]).items()},
            "password_systems": password_systems(),
            "mail_problem": self.applier.mail_problem,
            "profile_problem": profile_problem,
            "version": config.plugin_version(),
            "update": self.updates.notice(),
            "answers_problem": config.answers_problem(),
            "search": self.search,
            "listings": rows,
            "others": others,
            "current": self.applier.current,
            "queued": [jid for kind, jid in self.applier.tasks if kind == "apply"],
            "counts": t.counts(),
            "notes": report.notes_count(),  # notes on the desk's stops, waiting to be filed
        }


def _first_line(e: BaseException) -> str:
    return ((str(e).strip().splitlines() or [type(e).__name__])[0])[:150]


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


def _own_file(url: str) -> bool:
    """A file:// link into the desk's own folder (~/.job-apply: secrets, answers, the tracker)."""
    if not url.lower().startswith("file://"):
        return False
    try:
        path = Path(url2pathname(urlparse(url).path)).resolve()
        return path == config.home().resolve() or config.home().resolve() in path.parents
    except (OSError, ValueError):
        return True


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

_desk: Desk | None = None


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
