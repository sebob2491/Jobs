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
import secrets
import socket
import time
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from . import config
from .pipeline import Applier, question_key
from .postings import fetch_posting
from .recommend import recommend, score_listing

PAGE = Path(__file__).resolve().parent / "static" / "desk.html"
DEFAULT_PORT = 8765


def _recommendations_path() -> Path:
    return config.home() / "recommendations.json"


def _settings_path() -> Path:
    return config.home() / "desk.json"


class _QuietServer(uvicorn.Server):
    """uvicorn inside the MCP server: no signal handlers of its own (Claude Code owns them)."""

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


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
        self._server: uvicorn.Server | None = None
        self._load()

    # ------------------------------------------------------------- persistence
    def _load(self) -> None:
        try:
            saved = json.loads(_recommendations_path().read_text(encoding="utf-8"))
            self.listings = saved.get("results") or []
            self.search.update({k: saved.get(k) for k in ("query", "location", "errors", "browser_only")
                                if k in saved}, status="done", at=saved.get("at"))
        except (OSError, ValueError):
            pass
        try:
            self.applier.auto_submit = bool(json.loads(_settings_path().read_text(encoding="utf-8")).get("auto_submit"))
        except (OSError, ValueError):
            pass

    def _save_settings(self) -> None:
        config.ensure_home()
        _settings_path().write_text(json.dumps({"auto_submit": self.applier.auto_submit}), encoding="utf-8")

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
            r("/api/settings", self.settings_view, methods=["POST"]),
        ])

    async def start(self, port: int = DEFAULT_PORT, open_browser: bool = True) -> str:
        """Serve the desk in the running event loop (inside the MCP server)."""
        if self._serve_task is None or self._serve_task.done():
            self.port = _free_port(port)
            cfg = uvicorn.Config(self.app(), host="127.0.0.1", port=self.port, log_config=None, access_log=False,
                                 log_level="warning", lifespan="off")
            self._server = _QuietServer(cfg)
            self._serve_task = asyncio.get_running_loop().create_task(self._server.serve())
            for _ in range(200):
                if self._server.started or self._serve_task.done():
                    break
                await asyncio.sleep(0.05)
            if self._serve_task.done():
                raise RuntimeError(f"The Job Desk couldn't start on port {self.port}")
        self.applier.start()
        if open_browser:
            await asyncio.get_running_loop().run_in_executor(None, webbrowser.open, self.url)
        return self.url

    async def stop(self) -> None:
        await self.applier.stop()
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
        return not api or secrets.compare_digest(request.headers.get("x-desk-token", ""), self.token)

    def _forbidden(self) -> Response:
        return JSONResponse({"error": "forbidden"}, status_code=403)

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
        body = await request.json()
        queued = self.apply(urls=body.get("urls") or [], job_ids=body.get("job_ids") or [],
                            submit=bool(body.get("submit")))
        return JSONResponse({"queued": queued})

    async def answer_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        body = await request.json()
        try:
            self.answer(int(body["job_id"]), body.get("answers") or [])
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    async def job_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        job_id, action = request.path_params["job_id"], request.path_params["action"]
        a = self.applier
        try:
            if action == "resume":
                run = a.runs.get(job_id)
                a.enqueue(job_id, submit=bool(run and run.submit), front=True)
            elif action == "later":
                a.later(job_id)
            elif action == "skip":
                await a.skip(job_id)
            elif action == "submit":
                a.submit_now(job_id)
            elif action == "show":
                if not await a.focus(job_id):
                    return JSONResponse({"error": "Its tab is closed. Press Resume to open it again."}, status_code=409)
            elif action == "applied":
                self.srv.tracker().update(job_id, status="applied", note="marked applied in the Job Desk")
                if job_id in a.runs:
                    a.runs[job_id].status, a.runs[job_id].reason = "submitted", "Marked as applied."
            else:
                return JSONResponse({"error": f"unknown action {action!r}"}, status_code=404)
        except (KeyError, ValueError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    async def settings_view(self, request: Request) -> Response:
        if not self._allowed(request, api=True):
            return self._forbidden()
        body = await request.json()
        if "auto_submit" in body:
            self.applier.auto_submit = bool(body["auto_submit"])
            self._save_settings()
        return JSONResponse({"auto_submit": self.applier.auto_submit})

    # ------------------------------------------------------------- actions
    async def find_jobs(self) -> None:
        self.search.update(status="running", error=None, started=time.time())
        try:
            async def run_search(query: str, location: str | None, limit: int) -> dict[str, Any]:
                return await self.srv.search_company_jobs(query, location=location, limit_per_company=limit)

            out = await recommend(config.Profile.load(), run_search, limit_per_company=10, read_postings=40,
                                  fetch=self.fetch)
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

    def apply(self, urls: list[str], job_ids: list[int], submit: bool) -> list[int]:
        t = self.srv.tracker()
        ids = [int(j) for j in job_ids]
        for url in urls:
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
        for job_id in dict.fromkeys(ids):
            self.applier.enqueue(job_id, submit=submit)
        return list(dict.fromkeys(ids))

    def answer(self, job_id: int, answers: list[dict[str, Any]]) -> None:
        run = self.applier.runs[job_id]
        for a in answers:
            label, value = str(a.get("label") or ""), a.get("value")
            if not label or value in (None, ""):
                continue
            if a.get("remember", True):
                config.save_answer(label, value, run.company)
            else:
                run.once[question_key(label)] = value
        self.applier.enqueue(job_id, submit=run.submit, front=True)

    # ------------------------------------------------------------- state
    def state(self) -> dict[str, Any]:
        prof = config.Profile.load()
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
            "profile": {"name": prof.full_name, "missing": prof.missing_required(),
                        "titles": prof.get("preferences.titles") or [], "path": str(config.profile_path())},
            "settings": {"submit_mode": settings.submit_mode, "dry_run": settings.dry_run,
                         "auto_submit": self.applier.auto_submit},
            "search": self.search,
            "listings": rows,
            "others": others,
            "current": self.applier.current,
            "queued": [jid for kind, jid in self.applier.tasks if kind == "apply"],
            "counts": t.counts(),
        }


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
