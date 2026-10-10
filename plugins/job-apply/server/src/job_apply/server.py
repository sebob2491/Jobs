"""MCP server: job intake, application tracking and browser form filling."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlsplit, urlunsplit

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import config
from .ats import ATS_NAMES, detect_ats, greenhouse_form_url
from .autofill import (is_empty_value, is_name_rule, is_school_rule, place_words, plan_autofill, profile_entries,
                       resolve_field)
from .browser import BrowserSession, BrowserUnavailable, SiteDown, SubmitBlocked
from .postings import FetchError, Posting, fetch_posting, finalize, parse_html
from .render import KINDS, render_pdf, to_html
from .search import (ICIMS_FRAME, companies_path, eightfold_page_url, employer_lists,
                     icims_search, icims_state, infor_search, keep_listings, load_companies, location_terms,
                     parse_eightfold, paycom_search, rmk_search, search_companies, sfclassic_search, sitecore_search,
                     ukg_search, wordings_for)
from .tracker import Tracker

INSTRUCTIONS = """\
Job application assistant. Typical flow for one posting:
  ingest_job(url) -> open_application(job_id) -> autofill() -> inspect_form() ->
  fill_form([...]) for remaining answers -> click("Next"/"Continue") -> repeat ->
  on the review page: screenshot() for the user -> submit_application(job_id, user_confirmed=true)
  only after the user says to submit.
Repeated sections (Workday "My Experience"): add_entries("work"/"education"), then autofill.
Tailored documents: render_document(kind, markdown, job_id) before autofill uploads files.
The Job Desk's tailoring switch holds jobs for a tailored resume: tailoring_queue() lists them.
Rules: never invent facts about the applicant; answers must come from the profile or the
user. LinkedIn and Indeed applications are always submitted by the user clicking the
button themselves. Passwords go through fill_secret, never fill_form."""

mcp = MCPServer("job-apply", instructions=INSTRUCTIONS, version="0.3.0")

# Failures Claude should read, not just "Error executing tool": a job id that isn't there,
# a typo in profile.yaml, a browser that won't start.
_ANTICIPATED = (ValueError, KeyError, FileNotFoundError, PermissionError, BrowserUnavailable, SubmitBlocked, FetchError)


def _desk_driving() -> str | None:
    """The job the Job Desk is filling in the browser at this moment, if any."""
    from . import desk

    d = desk._desk
    job_id = d.applier.current if d is not None else None
    if d is None or job_id is None:
        return None
    run = d.applier.runs.get(job_id)
    return f"{run.title} at {run.company}" if run and run.title else "an application"


def _desk_tab_job(tab: Any) -> int | None:
    """The Job Desk job whose application is in this tab and that the desk comes back to
    (paused for the person, waiting on its review page, queued), if any."""
    from . import desk

    d = desk._desk
    if d is None or tab is None:
        return None
    for run in list(d.applier.runs.values()):
        # a failed one too: Resume takes it up again in its tab
        if run.status in ("needs_you", "ready", "queued", "running", "failed") and tab in browser.lineage(run.page):
            return run.job_id
    return None


def tool(drives: Callable[[dict[str, Any]], bool] | bool = False, **options: Any) -> Callable[[Any], Any]:
    """mcp.tool, with anticipated failures handed to Claude in their own words. The function
    itself is returned as written, so the desk calling it gets the same exceptions as before.
    `drives`: the tool acts in the browser's current tab (for these arguments), which the Job
    Desk uses while it fills an application; Claude's call waits its turn rather than
    typing or clicking in the desk's tab mid-step."""
    def message(e: Exception) -> str:
        return str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)

    def check(kwargs: dict[str, Any]) -> None:
        if drives is True or (callable(drives) and drives(kwargs)):
            busy = _desk_driving()
            if busy:
                raise ToolError(f"The Job Desk is filling {busy} in the browser right now. Wait for it to "
                                "pause or finish that job (its page shows when), then try again.")

    def register(fn: Any) -> Any:
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                check(kwargs)
                try:
                    return await fn(*args, **kwargs)
                except _ANTICIPATED as e:
                    raise ToolError(message(e)) from e
        else:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                check(kwargs)
                try:
                    return fn(*args, **kwargs)
                except _ANTICIPATED as e:
                    raise ToolError(message(e)) from e
        mcp.tool(**options)(wrapper)
        return fn
    return register
browser = BrowserSession()
_tracker: Tracker | None = None
_tracker_lock = threading.Lock()


def tracker() -> Tracker:
    """The one tracker, for the tools' worker threads and the desk alike."""
    global _tracker
    with _tracker_lock:
        if _tracker is None:
            config.ensure_home()
            _tracker = Tracker()
        return _tracker


def _job(job_id: int | None) -> dict[str, Any]:
    jid = job_id if job_id is not None else browser.current_job_id
    if jid is None:
        raise ValueError("No job selected: pass job_id (see list_jobs)")
    job = tracker().get(jid)
    if job is None:
        raise ValueError(f"No job with id {jid}")
    return job


def _same_site(job: dict[str, Any], url: str) -> bool:
    """Is this address on the job's own site: its posting's or application's host, or another
    address of the same employer (not another tenant of a job system many employers share)?"""
    from .ats import shared_system
    from .mailbox import site_domain

    host = (urlparse(url or "").hostname or "").lower()
    own = {(urlparse(u).hostname or "").lower() for u in (job.get("url"), job.get("apply_url")) if u}
    if not host or host in own:
        return bool(host)
    return shared_system(url) is None and any(h and site_domain(h) == site_domain(host) for h in own)


def _job_here(job_id: int | None) -> dict[str, Any]:
    """The job whose application is in the current tab. A job_id that isn't it is refused:
    filling or submitting another job's page as this one would send that page as this job's
    application (and mark this job applied)."""
    here = browser.current_job_id
    if job_id is None or job_id == here:
        return _job(here if job_id is None else job_id)
    job = _job(job_id)
    tab = browser.current_tab
    if here is None and tab is not None and _desk_tab_job(tab) is None and _same_site(job, tab.url):
        browser.current_job_id = job_id  # a page opened by its address, on this job's own site
        return job
    shown = f"job {here}" if here is not None else "a page that isn't this job's"
    raise ValueError(f"Job {job_id} isn't the one open in the browser's current tab ({shown}). "
                     f"Open it with open_application(job_id={job_id}) first.")


def _snapshot_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    jid = browser.current_job_id
    job = tracker().get(jid) if jid is not None else None
    base = Path(job["folder"]) if job else config.home()
    return base / "debug" / stamp


async def _auto_snapshot(note: str, details: Any) -> str | None:
    """Capture the page when something didn't fill, so it can be debugged later."""
    try:
        return str(await browser.snapshot(_snapshot_dir(), note=note, details=details))
    except Exception:  # never let debugging break the actual tool call
        return None


def _brief(job: dict[str, Any]) -> dict[str, Any]:
    keys = ["id", "status", "title", "company", "location", "ats", "source", "url", "apply_url", "salary", "folder"]
    out = {k: job.get(k) for k in keys}
    out["ats_name"] = ATS_NAMES.get(job.get("ats", ""), job.get("ats"))
    return out


# --------------------------------------------------------------------- setup


def chrome_installed() -> bool:
    """Google Chrome on this computer, wherever its installer put it: on the PATH, in
    Applications (for everyone or this user), or in Program Files or this user's AppData."""
    if shutil.which("google-chrome") or shutil.which("chrome"):
        return True
    places = [Path("/Applications/Google Chrome.app"), Path.home() / "Applications" / "Google Chrome.app"]
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if os.environ.get(var):
            places.append(Path(os.environ[var]) / "Google" / "Chrome" / "Application" / "chrome.exe")
    return any(p.exists() for p in places)


@tool()
def setup_status() -> dict[str, Any]:
    """Check what the plugin needs before it can apply: profile fields, resume file,
    browser. Creates ~/.job-apply and a profile template on first run."""
    home = config.ensure_home()
    prof = config.Profile.load()
    missing = prof.missing_required()
    has_chrome = chrome_installed()
    s = prof.settings
    return {
        "home": str(home),
        "profile_path": str(config.profile_path()),
        "profile_complete": not missing,
        "missing_profile_fields": missing,
        "profile_gaps": prof.profile_gaps(),  # not required, but each one is a stop on some sites
        "settings_warnings": s.warnings,
        "settings": {"submit_mode": s.submit_mode, "auto_submit_ats": s.auto_submit_ats,
                     "browser_channel": s.browser_channel, "headless": s.headless,
                     "email_codes": s.email_codes, "email_tracking": s.email_tracking,
                     "accept_cookies": s.accept_cookies, "manage_accounts": s.manage_accounts,
                     "manage_accounts_chosen": s.manage_accounts_chosen},
        "chrome_detected": has_chrome,
        "browser_note": "The desk uses Google Chrome, or Microsoft Edge without it. If neither starts, install Chrome or run "
                        f"`uv run --project \"{config.PLUGIN_ROOT / 'server'}\" playwright install chromium` "
                        "and set settings.browser_channel: chromium.",
        "plugin_root": str(config.PLUGIN_ROOT),
        "plugin_version": config.plugin_version(),
        "companies_file": str(companies_path()),  # the person's own list, when they have one
        "employer_lists": list(employer_lists()),  # what a person's own file can name under `lists:`
        **({"employer_list_problem": problem} if (problem := _employer_list_problem()) else {}),
        "jobs_by_status": tracker().counts(),
    }


def _employer_list_problem() -> str | None:
    """What's wrong with the person's own companies.yaml, if anything, before a search finds it."""
    try:
        load_companies()
    except ValueError as e:
        return str(e)
    return None


@tool()
def get_profile() -> dict[str, Any]:
    """Return the applicant profile (~/.job-apply/profile.yaml) used to answer forms.
    Edit that file directly to change answers."""
    prof = config.Profile.load()
    return {"path": str(prof.path), "profile": prof.data, "missing_required": prof.missing_required()}


# --------------------------------------------------------------------- jobs


@tool(drives=lambda a: bool(a.get("use_browser")))
async def ingest_job(url: str, use_browser: bool = False) -> dict[str, Any]:
    """Fetch a job posting (LinkedIn, Indeed, Workday, Greenhouse, Lever, any careers page),
    parse title/company/location/description/ATS, and save it to the tracker.

    Plain HTTP is tried first. LinkedIn and Indeed usually refuse that; pass
    use_browser=true to read the page through the user's signed-in browser instead.
    Returns the job record including its full description."""
    posting: Posting | None = None
    note = ""
    if not use_browser:
        try:
            posting = await fetch_posting(url)
        except FetchError as e:
            note = str(e)
    if posting is None or (use_browser is False and not posting.is_useful and detect_ats(url) in ("linkedin", "indeed")):
        if not use_browser:
            return {
                "saved": False,
                "error": f"Couldn't read the posting over HTTP ({note or 'incomplete page'}).",
                "next": "Call ingest_job again with use_browser=true, or add_job with details the user provides "
                        "(for Indeed, the Indeed connector's get_job_details also works).",
            }
        if detect_ats(url) == "icims":  # drawn inside a frame, which the page read below misses
            with contextlib.suppress(FetchError, BrowserUnavailable):
                posting = await read_icims_posting(url)
                job, created = tracker().upsert(posting.to_dict())
                return {"saved": True, "created": created, "job": job, "warnings": posting.warnings}
        try:
            if _desk_tab_job(browser.current_tab) is not None:
                await browser.new_tab()  # the Job Desk comes back to that tab: the posting gets its own
            opened = await browser.goto(url)
            if opened.get("navigation_error"):
                return {"saved": False, "error": f"The browser couldn't open the page: {opened['navigation_error']}"}
            posting = finalize(parse_html(await browser.html(), url))
        except BrowserUnavailable as e:
            return {"saved": False, "error": str(e)}
    job, created = tracker().upsert(posting.to_dict())
    return {"saved": True, "created": created, "job": job, "warnings": posting.warnings}


async def read_icims_posting(url: str) -> Posting:
    """An iCIMS posting, read in a background tab: iCIMS turns away plain requests (HTTP 405)
    and draws the posting inside a frame (in_iframe=1), which a read of the page itself misses.
    It's applied for from the posting: its own Apply goes to a framed sign-in page."""
    parts = urlsplit(url)
    framed = urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(q for q in (parts.query, "in_iframe=1") if q), ""))
    drawn = [html for html in await browser.frames_html(framed, inner=ICIMS_FRAME) if "iCIMS_JobContent" in html]
    if not drawn:
        raise FetchError(f"The posting wasn't drawn in its frame: {url}")
    posting = finalize(parse_html(drawn[0], url))
    if not posting.is_useful:
        raise FetchError(f"No posting text in {url}")
    posting.apply_url = url
    return posting


@tool()
async def search_company_jobs(
    query: str,
    companies: list[str] | None = None,
    location: str | None = "AZ",
    limit_per_company: int = 20,
) -> dict[str, Any]:
    """Search employers' own careers sites for openings through their applicant tracking
    system's public search: Workday, Greenhouse, Lever, Eightfold, SmartRecruiters, Oracle,
    ApplicantStack, Taleo, Talemetry, iCIMS Jibe, Jobvite, amazon.jobs and Phoenix Children's own
    site. ASML's site, iCIMS portals, Paycom and UKG Pro boards and SuccessFactors'
    newer search (Edwards) and older career sites (Amkor) and Infor CloudSuite boards (Benchmark)
    are read in a background browser tab.

    query: keywords; separate alternatives with "|", e.g. "field service | equipment engineer".
    companies: names from the plugin's companies list (default: all of them).
    location: state code/name or city alternatives ("AZ", "Phoenix|Chandler"); null for anywhere.
    Results already in the tracker carry `tracked`. Companies in `browser_only` have no
    search the plugin can use, and some turn automated browsers away: give the user their
    careers_url to search in their own browser."""
    listed = load_companies()  # once: the person's own file may name several lists
    out = await search_companies(query, companies, location, limit_per_company, companies=listed)
    # Eightfold career sites refuse scripted API calls; let a real page make the call instead.
    by_name = {c["name"]: c for c in listed}
    for name, err in list(out["errors"].items()):
        cfg = (by_name.get(name, {}).get("search") or {}).get("eightfold")
        if not cfg or "403" not in err:
            continue
        try:
            data = await browser.capture_json(eightfold_page_url(cfg, query, location), "/api/pcsx/search")
        except Exception as e:  # keep the original error, add why the fallback failed too
            out["errors"][name] = f"{err}; browser fallback: {type(e).__name__}: {str(e).splitlines()[0][:150]}"
            continue
        found = parse_eightfold(data, cfg["host"])
        out["results"].extend(keep_listings(name, found, location_terms(location), limit_per_company, query))
        del out["errors"][name]
    # Sites whose search only answers in the browser (ASML, iCIMS, Paycom, Amkor, Benchmark): one background tab
    # per wording, or one for the whole board when titles are matched here.
    for item in out.pop("needs_browser", []):
        name, cfg = item["company"], item["config"]
        found, failures = [], []
        wordings = wordings_for(item["kind"], query)
        for wording in wordings:
            try:
                if item["kind"] == "icims":
                    await icims_search(functools.partial(browser.frames_html, inner=ICIMS_FRAME), cfg, wording,
                                       found, icims_state(location_terms(location)))
                elif item["kind"] == "paycom":
                    await paycom_search(browser.capture_json, cfg, wording, found)
                elif item["kind"] == "ukg":
                    await ukg_search(browser.capture_json, cfg, wording, found)
                elif item["kind"] == "rmk":
                    await rmk_search(browser.capture_json, cfg, wording, found)
                elif item["kind"] == "sfclassic":
                    await sfclassic_search(browser.listing_pages, cfg, wording, found)
                elif item["kind"] == "infor":
                    await infor_search(browser.capture_json, cfg, wording, found)
                else:
                    await sitecore_search(browser.capture_json, cfg, wording, found)
            except SiteDown as e:  # the board itself is down: said plainly
                failures.append(str(e))
            except Exception as e:  # one wording failing keeps the others' results
                failures.append(f"browser search: {type(e).__name__}: {str(e).splitlines()[0][:150] if str(e) else ''}")
        if failures:
            out["errors"][name] = failures[0] + (
                f" ({len(failures)} of {len(wordings)} searches failed)" if found else "")
        out["results"].extend(keep_listings(name, found, location_terms(location), limit_per_company, query))
    t = tracker()
    for r in out["results"]:
        job = t.find_by_url(r["url"])
        if job:
            r["tracked"] = {"id": job["id"], "status": job["status"]}
    out["count"] = len(out["results"])
    return out


@tool()
def add_job(
    url: str,
    title: str,
    company: str,
    description: str = "",
    location: str = "",
    apply_url: str = "",
    salary: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Save a job from details you already have (e.g. from the Indeed connector or text the
    user pasted) when ingest_job can't read the page."""
    p = finalize(Posting(url=url, title=title, company=company, description=description, location=location,
                         apply_url=apply_url, salary=salary, source=source, parse_method="manual"))
    job, created = tracker().upsert(p.to_dict())
    return {"created": created, "job": _brief(job)}


@tool()
def list_jobs(status: str | None = None, company: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List tracked jobs, newest activity first. status is one of:
    saved, in_progress, ready_to_submit, applied, interviewing, offer, rejected, withdrawn, skipped."""
    jobs = tracker().list(status=status, company=company, limit=limit)
    return {"count": len(jobs), "jobs": [_brief(j) for j in jobs], "by_status": tracker().counts()}


@tool()
def get_job(job_id: int) -> dict[str, Any]:
    """Full record for one job, including description and status history."""
    job = _job(job_id)
    return {"job": job, "history": tracker().events(job_id)}


@tool()
def update_job(job_id: int, status: str | None = None, notes: str | None = None,
               apply_url: str | None = None, event_note: str = "") -> dict[str, Any]:
    """Change a job's status / notes / apply URL. Use status="applied" after the user
    submits an application themselves. Valid statuses: saved, in_progress,
    ready_to_submit, applied, interviewing, offer, rejected, withdrawn, skipped."""
    job = tracker().update(job_id, status=status, notes=notes, apply_url=apply_url, note=event_note)
    return {"job": _brief(job)}


@tool()
def log_email(job_id: int, thread_id: str, category: str, summary: str = "", received_at: str = "") -> dict[str, Any]:
    """Record an employer's email about an application and update its status.

    category: confirmation, assessment, interview, offer, rejection or other.
    thread_id is the Gmail thread id, and received_at the date of the message: the same
    message is never counted twice (already_logged=true), while a later one in the same
    thread (a rejection under the confirmation) is. Status only moves forward
    (applied -> interviewing -> offer); a rejection sets rejected unless there's already
    an offer."""
    return tracker().log_email(job_id, thread_id, category, summary, received_at)


@tool()
def logged_emails(since_days: int | None = 90) -> dict[str, Any]:
    """Gmail threads already recorded with log_email, with the date of the message logged
    (received_at), so a status check can skip them unless a newer message has arrived."""
    rows = tracker().logged_threads(since_days)
    return {"count": len(rows), "threads": rows}


@tool()
async def render_document(kind: str, markdown: str, job_id: int | None = None, default: bool = False) -> dict[str, Any]:
    """Turn a tailored resume or cover letter written in Markdown into a PDF in the job's
    folder. autofill uploads it there ahead of the profile's default documents.
    With default=true (no job), it writes the user's default document into ~/.job-apply
    instead, for setup; then point documents.resume / documents.cover_letter at it.

    kind: "resume" or "cover_letter".
    Resume layout: `# Full Name`, a contact line, `## Section` headings,
    `### Job Title — Company, City *Mar 2021 – Present*` (the *italic* dates sit on the
    right) and bullet lists. Cover letter: `# Full Name`, a contact line, then paragraphs.
    Only reorder, trim and rephrase what the user's real resume says."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    job = None if default else _job(job_id)  # (the current job, when none is named)
    job_id = job["id"] if job else None
    folder = config.ensure_home() if job is None else Path(job["folder"])
    prof = config.Profile.load()
    stem = "_".join(p for p in [prof.get("personal.first_name"), prof.get("personal.last_name")] if p)
    name = f"{stem}_{'Resume' if kind == 'resume' else 'Cover_Letter'}" if stem else kind
    source = folder / f"{name}.md"
    folder.mkdir(parents=True, exist_ok=True)
    source.write_text(markdown, encoding="utf-8")
    try:
        pages = await render_pdf(to_html(markdown, kind, title=name.replace("_", " ")), folder / f"{name}.pdf")
    except BrowserUnavailable as e:
        return {"error": str(e), "source": str(source)}
    limit = 2 if kind == "resume" else 1
    out: dict[str, Any] = {"path": str(folder / f"{name}.pdf"), "pages": pages, "source": str(source)}
    too_long = folder / f"{name}.too-long"  # a job the desk holds for this resume waits for the next render
    if pages > limit:
        out["warning"] = f"{pages} pages; aim for at most {limit}. Tighten the text and render again."
        if job_id is not None:
            too_long.touch()
    else:
        too_long.unlink(missing_ok=True)
        from . import desk

        if job_id is not None and desk._desk is not None:  # the job it was written for carries on now
            desk._desk.applier.wake()
    return out


TAILOR_RULES = [
    "Use only what the user's real resume and profile say: choose, order and reword. Never add a tool, "
    "certification, number, duty, employer, date or degree they didn't list.",
    "Lead with the experience and skills the posting asks for, in the posting's own words where they're true.",
    "Keep every title, employer and date exactly as the profile has them. Coursework is not a degree.",
    "At most 2 pages. Same layout as render_document's: # Name, a contact line, ## sections, "
    "### Title — Company, City *dates*, bullets.",
    "The first time, show the user one tailored resume before rendering the rest; after they approve the "
    "style, carry on without asking.",
]


@tool()
async def tailoring_queue() -> dict[str, Any]:  # async: it reads the desk's runs on the desk's own loop
    """Jobs the Job Desk is holding until Claude writes a resume tailored to each one (its
    "Tailor my resume for each job" switch is on). For each job, write the resume in
    Markdown from the user's real resume (base_resume, or resume_file and the profile's
    work and education history), following `rules`, then call
    render_document("resume", markdown, job_id). The desk carries on with that job as soon
    as its PDF is saved."""
    from . import desk

    d = desk._desk
    if d is None:
        return {"jobs": [], "note": "The Job Desk isn't open, so no job is waiting. Open it with open_job_desk()."}
    prof = config.Profile.load()
    jobs = []
    for run in d.applier.tailoring():
        job = tracker().get(run.job_id) or {}
        jobs.append({"job_id": run.job_id, "title": job.get("title") or run.title,
                     "company": job.get("company") or run.company, "url": job.get("url"),
                     "location": job.get("location"),
                     "description": (job.get("description") or "")[:12000]
                     or "Not read yet: read it with ingest_job(url) first."})
    base = sorted(config.home().glob("*_Resume.md"))
    resume = config.expand(prof.get("documents.resume"))
    return {
        "jobs": jobs,
        "base_resume": base[0].read_text(encoding="utf-8") if base else None,
        "resume_file": str(resume) if resume and resume.exists() else None,
        "work_history": prof.get("work_history") or [],
        "education_history": prof.get("education_history") or [],
        "education": prof.get("education") or {},
        "rules": TAILOR_RULES,
    }


@tool()
async def report_problem(job_id: int) -> dict[str, Any]:
    """Write a problem report for a job that went wrong: what the Job Desk did and the fields
    of the pages it saved, with the person's details taken out (no screenshots). Show the user
    `preview`, all of it, before anything else, and say the issue will be public and shows
    which job they applied for. Only with their OK, give them `issue_url` to open (a new issue
    on the plugin's public GitHub repository with that text). Never file it without their OK.
    `zip` also holds the saved pages, scrubbed but possibly still showing their answers: it
    stays on their computer, and never goes on the public issue."""
    from . import desk, report

    job = _job(job_id)
    run = desk._desk.applier.runs.get(job["id"]) if desk._desk is not None else None
    return await asyncio.to_thread(report.build, job, run)


@tool()
def export_jobs_csv(path: str | None = None) -> dict[str, Any]:
    """Write every tracked job to a CSV (default ~/.job-apply/applications.csv) for a spreadsheet."""
    out = config.expand(path) if path else config.home() / "applications.csv"
    assert out is not None
    n = tracker().export_csv(out)
    return {"path": str(out), "rows": n}


# --------------------------------------------------------------------- browser


@tool(drives=True)
async def open_application(job_id: int | None = None, url: str | None = None) -> dict[str, Any]:
    """Open a job's application (or its posting page, when there is no separate apply URL)
    in the visible browser and make it the current job. Returns a summary of the page:
    headings, number of fields, buttons and errors.

    Next steps are usually click("Apply") / click("Easy Apply"), then autofill()."""
    if job_id is None and not url:
        raise ValueError("Pass job_id or url")
    target = url
    if job_id is None:
        # the job saved at this address, if any: never the one opened before, whose documents
        # would go into this form and whose status a submit here would change
        known = tracker().find_by_url(url)
        job_id = known["id"] if known else None
        browser.current_job_id = job_id
    else:
        job = _job(job_id)
        target = url or job.get("apply_url") or job["url"]
        target = greenhouse_form_url(target) or target
        browser.current_job_id = job_id
        if job["status"] == "saved":
            tracker().update(job_id, status="in_progress", note="opened application")
    owner = _desk_tab_job(browser.current_tab)
    if owner is not None and owner != job_id:
        # the tab holds another job the Job Desk has paused or filled: it's left as it is, and
        # this one opens in a tab of its own (the desk would otherwise fill this page as that job)
        await browser.new_tab()
    try:
        summary = await browser.goto(target)  # type: ignore[arg-type]
    except BrowserUnavailable as e:
        return {"error": str(e)}
    if browser.current_tab is not None:  # which job this tab is for, when Claude switches back to it
        if job_id is not None:
            browser.tab_jobs[browser.current_tab] = job_id
        else:
            browser.tab_jobs.pop(browser.current_tab, None)
    summary["ats"] = detect_ats(summary["url"])
    summary["submit_policy"] = _submit_policy(await browser.human_submit_ats() or summary["ats"])
    return summary


@tool()
async def inspect_form(include_dropdown_options: bool = True) -> dict[str, Any]:
    """List the fields and buttons on the current page (all frames). Each field has an id,
    kind (text, textarea, select, listbox, combobox, radio_group, checkbox_group, checkbox,
    file, password), label, required flag, options and current value. Buttons are listed
    under `actions`; is_submit marks the final submit button. A CAPTCHA on show is said
    under `captcha` (its own frame isn't listed): it's the user's to solve. Ids stay valid
    until the page changes; call this again after navigating."""
    data = await browser.inspect(include_dropdown_options)
    data["ats"] = detect_ats(data["url"])
    return data


# What a site's list calls a school it doesn't have, and the fill's words for "not in its list"
SCHOOL_NOT_LISTED = ("Other", "Not Listed")
_NOT_IN_LIST = re.compile(r"doesn't match any (suggestion|option)|nothing in its list matched")


@tool(drives=True)
async def autofill(job_id: int | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Fill every field on the current page that the profile answers with confidence
    (contact details, address, work authorization, sponsorship, EEO choices, resume upload,
    saved answers). Returns what was filled and the fields still needing a decision —
    draft those from the profile/resume and confirm anything subjective with the user."""
    job = _job_here(job_id) if (job_id is not None or browser.current_job_id is not None) else {}
    data = await browser.inspect(include_dropdown_options=True)
    prof = config.Profile.load()
    plan = plan_autofill(data["fields"], prof, job, overwrite=overwrite)
    results = await browser.fill([{"id": f["id"], "value": f["value"], "names": is_name_rule(f.get("rule") or ""),
                                   "near": place_words(f.get("rule") or "", prof)}
                                  for f in plan["to_fill"]]) if plan["to_fill"] else []
    by_id = {f["id"]: f for f in plan["to_fill"]}
    fields = {f["id"]: f for f in data["fields"]}
    # an answer that matched only a group (Robert Half's "Website / Job Board Posting", holding
    # "Company Career Site"): chosen again among the group's entries, and filled with that one
    regrouped = {}
    for r in results:
        if not r["ok"] and r.get("options") and r["id"] in fields:
            again = resolve_field({**fields[r["id"]], "options": r["options"], "search": False}, prof, job)
            if again is not None and again.value in r["options"]:
                regrouped[r["id"]] = again.value
    if regrouped:
        retried = {x["id"]: x for x in await browser.fill([{"id": fid, "value": v} for fid, v in regrouped.items()])}
        results = [retried.get(r["id"], r) for r in results]
        by_id.update({fid: {**by_id[fid], "value": v} for fid, v in regrouped.items() if retried.get(fid, {}).get("ok")})
    # a school a site's list doesn't have (a small community college): its "Other" entry, the
    # true answer there (a box for the school's name, where one then shows, is filled after)
    for i, r in enumerate(results):
        if (not r["ok"] and is_school_rule(by_id.get(r["id"], {}).get("rule") or "")
                and _NOT_IN_LIST.search(str(r.get("error") or ""))):  # not a slow list or a missed click
            for other in SCHOOL_NOT_LISTED:
                tried = (await browser.fill([{"id": r["id"], "value": other}]))[0]
                if tried.get("ok"):
                    results[i] = tried
                    by_id[r["id"]] = {**by_id[r["id"]], "value": other}
                    break
    filled, failed = [], []
    for r in results:
        src = by_id.get(r["id"], {})
        entry = {"id": r["id"], "label": src.get("label"), "value": src.get("value"), "from": src.get("rule")}
        if r["ok"]:
            filled.append(entry)
        else:
            # what to ask instead: the field's kind, whether it's required, and its choices
            # (those of the group picked, when the answer was a group of entries)
            field = fields.get(r["id"], {})
            options = r.get("options") or field.get("options")
            failed.append({**entry, "error": r["error"], "kind": field.get("kind"),
                           "required": bool(field.get("required", True)), **({"options": options} if options else {}),
                           **{k: field[k] for k in ("section", "sublabel") if field.get(k)}})
    after = await browser.inspect(include_dropdown_options=False)
    snapshot = await _auto_snapshot("autofill failures", failed) if failed else None
    return {
        "page": {"url": after["url"], "headings": after["headings"]},
        "filled": filled,
        "failed": failed,
        **({"debug_snapshot": snapshot} if snapshot else {}),
        "needs_input": plan["needs_input"],
        "already_filled": len(plan["already_filled"]),
        "errors": after["errors"],
        "actions": [a for a in after["actions"] if not a.get("disabled")][:25],
    }


@tool(drives=True)
async def add_entries(section: str, count: int | None = None) -> dict[str, Any]:
    """Create the repeated blocks for work history or education before filling them.

    section is "work" or "education". Clicks the section's Add / Add Another button until
    there is one numbered block ("Work Experience 1", "Education 1"…) per entry in the
    profile's work_history / education_history (or `count`). Then call autofill, which
    fills each block from the matching profile entry."""
    key, pattern = {
        "work": ("work_history", r"work experience|employment|work history|experience"),
        "education": ("education_history", r"education"),
    }[section.lower()]
    want = count if count is not None else len(profile_entries(config.Profile.load(), key))
    if want == 0:
        return {"added": 0, "note": f"The profile has no {key} entries; add them to profile.yaml first."}
    result = await browser.add_entries(pattern, want)
    result["wanted"] = want
    if not result["add_button_found"]:
        result["note"] = "No Add button found for this section on the current page."
    return result


@tool(drives=True)
async def fill_form(values: list[dict[str, Any]]) -> dict[str, Any]:
    """Fill specific fields: values = [{"id": "12", "value": "..."}]. Ids come from
    inspect_form/autofill. Options are matched loosely ("Yes", "AZ" -> "Arizona"). For
    checkbox_group pass a list. For file fields pass a local file path."""
    results = await browser.fill(values)
    ok = all(r["ok"] for r in results)
    out: dict[str, Any] = {"results": results, "ok": ok}
    if not ok:
        failed = [{**r, "value": v.get("value")} for r, v in zip(results, values) if not r["ok"]]
        if snapshot := await _auto_snapshot("fill_form failures", failed):
            out["debug_snapshot"] = snapshot
    return out


@tool()
async def debug_snapshot(note: str = "") -> dict[str, Any]:
    """Save the current page (HTML of every frame, full screenshot, extracted fields) to the
    job's debug folder. Use it when a page behaves unexpectedly — a field that won't fill,
    a button that does nothing, a missed label — so the case can become a regression test
    (see the README's "Turning a failure into a test")."""
    path = await browser.snapshot(_snapshot_dir(), note=note)
    return {"saved_to": str(path), "files": sorted(p.name for p in path.iterdir())}


@tool(drives=True)
async def fill_secret(field_id: str, secret_name: str) -> dict[str, Any]:
    """Type a stored secret (e.g. a career-site password) into a field without the value
    passing through the conversation. Secrets come from env JOB_APPLY_SECRET_<NAME> or
    ~/.job-apply/secrets.yaml."""
    if re.sub(r"[^A-Z0-9]", "_", secret_name.strip().upper()) == "EMAIL_PASSWORD":  # (read as get_secret reads it)
        # the key to the person's inbox: the desk reads sign-up codes with it, nothing types it anywhere
        return {"ok": False, "error": "The email app password is only for reading sign-up codes from the inbox; "
                                      "it is never typed into a page."}
    secret = config.get_secret(secret_name)
    if secret is None:
        return {"ok": False, "error": f"No secret named {secret_name!r}. Ask the user to add it to "
                f"{config.secrets_path()} or set JOB_APPLY_SECRET_{secret_name.upper()}, or to type it in the browser."}
    from .pipeline import PASSWORD_SITES, password_for

    name = re.sub(r"[^a-z0-9]", "_", secret_name.strip().lower())
    site = name.removesuffix("_password")

    def site_ok(url: str) -> bool:
        # a password saved for one system (workday_password) goes only onto that system's sites;
        # any other only onto the site it's named for (linkedin_password: linkedin.com)
        if password_for(url) == name:
            return True
        if site in PASSWORD_SITES:
            return False
        parsed = urlparse(url or "")
        host = re.sub(r"[^a-z0-9.]", "", (parsed.hostname or "").lower())
        return parsed.scheme == "https" and len(site) >= 3 and site.replace("_", "") in host

    try:
        await browser.fill_secret(field_id, secret, site_ok)
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


@tool(drives=True)
async def click(target: str) -> dict[str, Any]:
    """Click a button/link by its id from inspect_form, or by visible text ("Next",
    "Save and Continue", "Apply", "Easy Apply", "Add"). Refuses final submit buttons —
    those go through submit_application. Returns a summary of the resulting page."""
    try:
        summary = await browser.click(target)
    except SubmitBlocked as e:
        return {"clicked": False, "blocked": str(e)}
    summary["ats"] = detect_ats(summary["url"])
    return {"clicked": True, **summary}


@tool(structured_output=False)
async def screenshot(full_page: bool = False, job_id: int | None = None) -> list[Any]:
    """Screenshot of the current browser tab (JPEG). Use it to check tricky widgets and to
    show the user the review page before submitting. Saved into the job's folder too."""
    save = None
    jid = job_id if job_id is not None else browser.current_job_id
    if jid is not None and (job := tracker().get(jid)):
        save = Path(job["folder"]) / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.jpg"
    data = await browser.screenshot(full_page=full_page, save_to=save)
    return [Image(data=data, format="jpeg"), f"saved: {save}" if save else "not saved (no current job)"]


@tool()
async def page_text(max_chars: int = 8000) -> str:
    """Visible text of the current tab, for reading instructions, errors or a confirmation page."""
    return await browser.visible_text(max_chars)


@tool(drives=lambda a: a.get("switch_to") is not None)
async def tabs(switch_to: int | None = None) -> dict[str, Any]:
    """List open browser tabs, or switch to tab number `switch_to`."""
    out = await browser.tabs(switch_to)
    if switch_to is not None:  # the job is the tab's now: the Job Desk's, or the one it was opened for
        tab = browser.current_tab
        owner = _desk_tab_job(tab)
        browser.current_job_id = owner if owner is not None else browser.job_in(tab)
    return out


@tool(drives=True)
async def close_browser() -> dict[str, Any]:
    """Close the automation browser (sign-ins are kept in the profile for next time)."""
    await browser.close()
    return {"closed": True}


@tool()
async def open_job_desk(open_browser: bool = True) -> dict[str, Any]:
    """Open the Job Desk: a page on this computer that lists recommended openings from
    every employer, ranked against the profile, and applies to the ones the user picks
    with one button. It fills each application in the automation browser up to its
    review page, pauses for anything that needs the user (questions it can't answer,
    sign-ins, bot checks, emailed codes), and submits only when they press Submit or turn
    on "Submit for me". Returns the page's address; it stays up while Claude Code runs."""
    import sys

    from .desk import get_desk

    desk = get_desk(sys.modules[__name__])
    url = await desk.start(open_browser=open_browser)
    return {"url": url, "opened_in_browser": open_browser,
            "note": "The address carries a private key; share it with no one. Press Find jobs on the page to search."}


def _mark_ready(job: dict[str, Any], note: str) -> None:
    """ready_to_submit only for jobs not yet past that point (a practice run on an
    application that's already in must not move it backwards)."""
    if job["status"] in ("saved", "in_progress"):
        tracker().update(job["id"], status="ready_to_submit", note=note)
    else:
        tracker().update(job["id"], note=note)


def _write_record(path: Path, record: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):  # the tracker keeps the note either way
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def _submit_policy(ats: str) -> str:
    s = config.Profile.load().settings
    if s.dry_run:
        return "dry_run"
    if ats in config.HUMAN_SUBMIT_ONLY:
        return "user_clicks_submit"
    return "auto" if s.may_auto_submit(ats) else "after_user_confirms"


@tool(drives=True)
async def submit_application(job_id: int | None = None, user_confirmed: bool = False) -> dict[str, Any]:
    """Click the final Submit button on the current page and record the result.

    Only call after showing the user the filled review page and they said to submit
    (user_confirmed=true), or when settings.submit_mode is "auto" for this ATS.
    On LinkedIn and Indeed this never clicks: it marks the job ready_to_submit and
    the user clicks Submit in the browser; then call update_job(status="applied")."""
    job = _job_here(job_id)
    page = await browser.inspect(include_dropdown_options=False)
    ats = await browser.human_submit_ats() or detect_ats(page["url"])  # Indeed's form inside an employer's page
    policy = _submit_policy(ats)
    if policy == "dry_run":
        _mark_ready(job, "dry run: filled, not submitted")
        return {"submitted": False, "reason": "Dry run (settings.submit_mode: dry_run): the form is filled and "
                                              "left unsubmitted. Nothing was sent."}
    if policy == "user_clicks_submit":
        _mark_ready(job, f"filled on {ATS_NAMES.get(ats, ats)}")
        return {
            "submitted": False,
            "reason": f"{ATS_NAMES.get(ats, ats)} prohibits automated submission. Ask the user to review the "
                      "browser window and click Submit themselves, then call update_job(status='applied').",
        }
    if policy == "after_user_confirms" and not user_confirmed:
        return {"submitted": False, "reason": "Show the user the review page and get an explicit go-ahead first."}
    empty_required = [f["label"] for f in page["fields"]
                      if f.get("required") and is_empty_value(f.get("value")) and f["kind"] != "password"]
    if empty_required and not user_confirmed:
        return {"submitted": False, "reason": "Required fields are still empty; fill them or ask the user.",
                "empty_required": empty_required}
    buttons = await browser.find_submit()
    if not buttons:
        return {"submitted": False, "reason": "No submit button on this page. Continue through the steps first.",
                "empty_required": empty_required, "actions": [a["text"] for a in page["actions"]]}
    folder = Path(job["folder"])
    await browser.screenshot(full_page=True, save_to=folder / "before-submit.jpg")
    # A record of the press, written before it: with no confirmation (or a page that closes itself
    # as it sends) the application may still have gone, and the Job Desk reads this, after a
    # restart too, so as never to press Submit again by itself
    record = folder / "submission.json"
    try:
        earlier: str | None = record.read_text(encoding="utf-8")
    except OSError:
        earlier = None
    _write_record(record, {"confirmed": False, "state": "pressing", "at": datetime.now().isoformat()})
    try:
        result = await browser.press_submit(buttons[-1]["id"])
    except SubmitBlocked as e:  # nothing was pressed: the record is as it was
        with contextlib.suppress(OSError):
            record.write_text(earlier, encoding="utf-8") if earlier is not None else record.unlink(missing_ok=True)
        return {"submitted": False, "reason": str(e)}
    except Exception:
        tracker().update(job["id"], note="pressed Submit; it didn't finish, so it may or may not have gone")
        raise
    # the tracker first: a full disk mustn't lose a confirmed submit
    if result["confirmed"]:
        tracker().update(job["id"], status="applied", note=f"submitted via {ATS_NAMES.get(ats, ats)}")
    else:
        tracker().update(job["id"], note="pressed Submit; no confirmation showed")
    _write_record(record, {**result, "at": datetime.now().isoformat()})
    with contextlib.suppress(Exception):  # a tab the confirmation closed
        await browser.screenshot(full_page=False, save_to=folder / "after-submit.jpg")
    return {
        "submitted": True,
        "confirmed": result["confirmed"],
        "empty_required_at_submit": empty_required,
        "errors": result["errors"],
        "url": result["url"],
        "text_excerpt": result["text_excerpt"][:800],
        "next": None if result["confirmed"] else
        "No confirmation text detected. Check page_text/screenshot; if it went through, update_job(status='applied').",
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
