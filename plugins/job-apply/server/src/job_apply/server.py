"""MCP server: job intake, application tracking and browser form filling."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from playwright.async_api import Error as PlaywrightError

from . import config, pay
from . import employment
from . import places as towns
from . import workplace as wplace
from .ats import ATS_NAMES, detect_ats, greenhouse_form_url, shared_system, workday_parts
from .autofill import common_questions as bank_questions
from .autofill import (is_eeo_rule, is_empty_value, is_how_heard, is_name_rule, is_school_rule, norm, place_words,
                       plan_autofill, profile_entries, resolve_field)
from .browser import BrowserSession, BrowserUnavailable, SiteDown, SubmitBlocked
from .mailbox import MailboxError, site_domain
from .postings import FetchError, Posting, fetch_posting, finalize, parse_html
from .recommend import avoided_title
from .render import KINDS, render_pdf, to_html
from .search import (ICIMS_FRAME, companies_path, eightfold_page_url, employer_lists, every_employer,
                     icims_search, icims_state, infor_search, keep_listings, load_companies, location_terms,
                     parse_eightfold, paycom_search, rmk_search, search_companies, set_search_area, sfclassic_search,
                     sitecore_search, ukg_search, wordings_for)
from .search import search_area as search_area_now
from .tracker import APPLICATION_STATUSES, Tracker, standing

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

# the plugin's own version (plugin.json), what /mcp shows; pyproject's isn't the release's (the review of Oct 18)
mcp = MCPServer("job-apply", instructions=INSTRUCTIONS, version=config.plugin_version() or "0")

# Failures Claude should read, not just "Error executing tool": a job id that isn't there,
# a typo in profile.yaml, a browser that won't start. And, since the review of Oct 18, the
# database's (locked by another session, or not a database any more), the browser's (a tab
# the person closed, a page that timed out), the mailbox's, a site that's down, a file that
# can't be written (OSError, PermissionError and FileNotFoundError among them), the desk
# failing to start: mcp turns anything else into a bare "Error executing tool <name>".
_ANTICIPATED = (ValueError, KeyError, OSError, RuntimeError, sqlite3.Error, PlaywrightError, httpx.HTTPError,
                MailboxError, SiteDown, BrowserUnavailable, SubmitBlocked, FetchError)
# The failures that are the tool's own words to the person. Any other of those above may be a fault in the code too, so
# its traceback goes to the server's log as well (mcp logs none for a ToolError: the review of 0.3.210)
_SAID_AS_MEANT = (ValueError, KeyError, FileNotFoundError, PermissionError, BrowserUnavailable, SubmitBlocked, FetchError)
_log = logging.getLogger(__name__)
# The most of a failure's words Claude is handed: its first line (Playwright adds its call log after it)
_SAID_MAX = 300


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
        said = str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)
        first = said.strip().split("\n", 1)[0].strip() or type(e).__name__
        return first if len(first) <= _SAID_MAX else first[:_SAID_MAX - 1] + "…"

    def said(e: Exception, fn: Any) -> ToolError:
        if not isinstance(e, _SAID_AS_MEANT):
            _log.warning("%s failed", getattr(fn, "__name__", "a tool"), exc_info=e)
        return ToolError(message(e))

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
                    raise said(e, fn) from e
        else:
            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                check(kwargs)
                try:
                    return fn(*args, **kwargs)
                except _ANTICIPATED as e:
                    raise said(e, fn) from e
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


# Saved pages kept in a debug folder (the newest; the pages a job stopped on keep their own count): a page holds every
# frame's HTML and a screenshot, so they're never kept for ever (the review of Oct 15)
DEBUG_KEEP = 20


def _keep_newest(debug: Path) -> None:
    """The newest DEBUG_KEEP saved pages of a debug folder kept, older ones gone (the stops' pages are the desk's own)."""
    with contextlib.suppress(OSError):
        saved = sorted(d for d in debug.iterdir() if d.is_dir() and not d.name.endswith("-stop"))
        for old in saved[:-DEBUG_KEEP]:
            shutil.rmtree(old, ignore_errors=True)


async def _auto_snapshot(note: str, details: Any) -> str | None:
    """Capture the page when something didn't fill, so it can be debugged later."""
    try:
        path = await browser.snapshot(_snapshot_dir(), note=note, details=details)
        _keep_newest(path.parent)
        return str(path)
    except Exception:  # never let debugging break the actual tool call
        return None


def _brief(job: dict[str, Any]) -> dict[str, Any]:
    keys = ["id", "status", "title", "company", "location", "ats", "source", "url", "apply_url", "salary", "folder"]
    out = {k: job.get(k) for k in keys}
    out["ats_name"] = ATS_NAMES.get(job.get("ats", ""), job.get("ats"))
    said = pay.from_fields(job)
    if said is not None:
        out["pay"], out["pay_yearly"] = pay.shown(said), pay.yearly(said)[1] or pay.yearly(said)[0]
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
                     "manage_accounts_chosen": s.manage_accounts_chosen, "accept_notices": s.accept_notices},
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
        # the common questions the desk would stop to ask (common_questions lists them, and saves the answers)
        "common_questions_unanswered": sum(1 for q in bank_questions(prof) if q.get("answer") in (None, "")),
    }


@tool()
async def search_area(places: list[str] | None = None, lists: list[str] | None = None,
                      titles: list[str] | None = None, avoid_titles: list[str] | None = None) -> dict[str, Any]:
    """What and where Find jobs (and search_company_jobs) looks: the job titles the person wants (`titles`; the first
    `searched_titles` are searched, and all of them count toward an opening's fit; none, `default_titles`), the words
    or titles they leave out (`avoid_titles`), the places they want work in (`places`, each a city and state: ["Phoenix, AZ", "Las Vegas, NV"]; their states are searched)
    and the plugin's employer lists to search (`lists`; `choices` names them all, with what each covers and how many
    employers). The Job Desk's What and where to search window changes the same. With no arguments it only says
    what they are now.

    titles: the job titles, replacing the ones there (kept in profile.yaml's preferences.titles), one title each.
    avoid_titles: words or titles to leave out ("Senior", "Call Center"), replacing the ones there (kept in
    profile.yaml's preferences.avoid_titles): an opening whose title has one as whole words is shown on the Job Desk's
    All tab but never recommended.
    places: the places, replacing the ones there (kept in profile.yaml's preferences.locations).
    lists: the lists' names, replacing the ones there (kept in ~/.job-apply/companies.yaml's `lists:`, which is made
    when the person has none; their own employers in it stay). Only what the person asked for."""
    if places is None and lists is None and titles is None and avoid_titles is None:
        return search_area_now()
    area = set_search_area(places=places, lists=lists, titles=titles, avoid_titles=avoid_titles)
    from . import desk

    # (an open desk's openings were found and scored for others, the titles to leave out among them: Find jobs
    # searches again)
    if desk._desk is not None:
        desk._desk._forget_openings()
    return area


@tool()
def common_questions(answers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The common questions most applications ask that a profile doesn't answer (how they heard of jobs, desired
    pay, start date, shifts, relocating...), as the Job Desk's "Common questions" card asks them: each with its
    id, the question to ask the person (`ask`), its `kind` ("choice", with its `choices`, or "text") and their
    `answer` so far. Ask the unanswered in setup, the choices with AskUserQuestion.

    answers: the person's answers to keep, [{"id": "shift", "answer": "Nights"}]: each fills every wording of its
    question at every employer (kept in ~/.job-apply/answers.yaml); an empty answer takes one out. Only what the
    person said: never a guess. One that isn't among its question's choices is turned away (`errors`)."""
    saved: list[str] = []
    errors: dict[str, str] = {}
    for item in answers or []:
        bank_id = str((item or {}).get("id") or "")
        try:
            config.save_bank_answer(bank_id, (item or {}).get("answer"))
        except ValueError as e:
            errors[bank_id or "?"] = str(e)
        else:
            saved.append(bank_id)
    asked = bank_questions(config.Profile.load())
    return {"questions": asked, "unanswered": sum(1 for q in asked if q.get("answer") in (None, "")),
            "saved": saved, **({"errors": errors} if errors else {})}


@tool()
async def doctor(launch_browser: bool = False) -> dict[str, Any]:
    """Check this computer's setup for the plugin, as the `job-apply-doctor` command does:
    Python, uv, the plugin's version and whether a newer one is out, the browser, ~/.job-apply,
    the profile and the resume, and on Windows UV_PYTHON_INSTALL_DIR. Each check is ok, a
    problem (with what to do about it), advice, or skipped; `ok` is false when something
    needed is missing. Run it when something seems broken, and pass on its fixes in plain
    words. launch_browser=True also starts the browser, out of sight, to be sure it opens.
    It changes nothing."""
    from . import doctor as checkup

    return (await checkup.run(launch=launch_browser)).as_dict()


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
    if config.own_file(url):  # ~/.job-apply's own files (secrets, answers): its text would be saved where Claude reads it
        return {"saved": False, "error": "That's one of the desk's own files, not a job posting; it wasn't read."}
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
    posting.company = listed_employer(url) or posting.company
    job, created = tracker().upsert(posting.to_dict())
    return {"saved": True, "created": created, "job": job, "warnings": posting.warnings}


def listed_employer(url: str) -> str | None:
    """The name a posting's employer goes by on the plugin's lists (and the person's own), from its career site: a
    Workday site's host and tenant, or a SuccessFactors career site on the employer's own address. Workday's own name
    for it is a legal entity ("100 Intel Corporation", "0001 Applied Materials, Inc", the tailoring test of Oct 12),
    and a career site's its account's ("rogersco" for Rogers Corporation, "APS": run 116), which named the job's
    folder, missed the desk's lists by name, and read "No" to "Have you ever been employed by APS?" for a person who
    worked at Arizona Public Service. SuccessFactors' own hosts carry many employers' sites: a posting there keeps
    its own name. (One employer's site may show a sister company's postings: Pfeiffer Vacuum's shows Busch's, named
    Pfeiffer Vacuum here, as the search names them.)"""
    parts = workday_parts(url)
    host = (urlsplit(url).hostname or "").lower()
    own_site = detect_ats(url) == "successfactors" and shared_system(url) is None
    if not parts and not own_site:
        return None
    try:
        listed = every_employer()
    except Exception:  # (a person's companies.yaml with a mistake: the posting keeps its own name)
        return None
    named = []
    for c in listed:
        if not isinstance(c, dict):
            continue
        search: dict[str, Any] = c["search"] if isinstance(c.get("search"), dict) else {}
        for site in (c.get("careers_url"), search.get("workday")):
            theirs = workday_parts(f"{site}/x") if parts and isinstance(site, str) else None
            if parts and theirs and (theirs["host"], theirs["tenant"]) == (parts["host"], parts["tenant"]):
                return str(c.get("name") or "") or None
        sf = search.get("successfactors")
        if own_site and isinstance(sf, str) and (urlsplit(sf).hostname or "").lower() == host and c.get("name"):
            named.append(str(c["name"]))
    return named[0] if len(set(named)) == 1 else None


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


def _check_min_pay(min_pay: float | None) -> None:
    """A min_pay a tool was given: dollars a year, 0 or more (0 or none: any pay)."""
    if min_pay is not None and not (math.isfinite(min_pay) and 0 <= min_pay <= pay.MIN_PAY_MOST):
        raise ToolError(f"min_pay is dollars a year, 0 to {pay.MIN_PAY_MOST:,} (an hourly rate × 2,080); {min_pay} "
                        "isn't. Leave it out for any pay.")


def _check_workplace(value: str | None) -> list[str]:
    """A workplace a tool was given: the workplaces it names (remote, hybrid, onsite), [] for none."""
    places = wplace.parse_choice(value)
    if places is None:
        raise ToolError(f"workplace is remote, hybrid or onsite, or several of them (\"remote, hybrid\"); {value!r} "
                        "isn't. Leave it out for any.")
    return places


def _home_for(within_miles: float | None) -> towns.Place | None:
    """The person's home town for a within_miles a tool was given (None for none), or a ToolError in plain words: a
    distance that isn't one, or a profile with no home town the plugin knows."""
    if within_miles is None:
        return None
    if not (math.isfinite(within_miles) and 0 < within_miles <= towns.WITHIN_MOST):
        raise ToolError(f"within_miles is a distance in miles, over 0 (10, 25, 50); {within_miles} isn't. Leave it out "
                        "for any distance.")
    try:
        prof = config.Profile.load()
    except Exception as e:
        raise ToolError(f"within_miles measures from the home town in the profile, which doesn't read: {e}") from e
    home, problem = towns.home(prof)
    if home is None:
        raise ToolError(f"{problem} Leave within_miles out to search at any distance.")
    return home


@tool()
async def search_company_jobs(
    query: str,
    companies: list[str] | None = None,
    location: str | None = "AZ",
    limit_per_company: int = 20,
    every_list: bool = False,
    min_pay: float | None = None,
    posted_within_days: int | None = None,
    workplace: str | None = None,
    employment_type: str | None = None,
    exclude: list[str] | None = None,
    within_miles: float | None = None,
) -> dict[str, Any]:
    """Search employers' own careers sites for openings through their applicant tracking
    system's public search: Workday, Greenhouse, Lever, Eightfold, SmartRecruiters, Oracle,
    ApplicantStack, Taleo, Talemetry, iCIMS Jibe, Jobvite, Cornerstone, Phenom, amazon.jobs and Phoenix
    Children's own site. ASML's site, iCIMS portals, Paycom and UKG Pro boards and SuccessFactors'
    newer search (Edwards) and older career sites (Amkor) and Infor CloudSuite boards (Benchmark)
    are read in a background browser tab.

    query: keywords; separate alternatives with "|", e.g. "field service | equipment engineer".
    companies: names from the plugin's companies list (default: all of them).
    location: state code/name or city alternatives ("AZ", "Phoenix|Chandler"); null for anywhere.
    Results already in the tracker carry `tracked`. Companies in `browser_only` have no
    search the plugin can use, and some turn automated browsers away: give the user their
    careers_url to search in their own browser.
    every_list: every plugin list's employers too, not only the person's (the desk's troubleshooting).
    min_pay: dollars a year (hourly pay counts 2,080 hours): leaves out an opening whose pay is listed (by its
    site's list, or its job in the tracker) on its pay line or in its own data, and the most it pays is below this
    (pay only read from a posting's other words never leaves one out). Most lists don't give pay: those openings
    stay, with no `pay` (read the posting, or ingest_job, for it). Results with pay carry `pay`.
    posted_within_days: leaves out openings posted longer ago (7, 14, 30...); one whose site gives no date stays
    (ApplicantStack, Talemetry, Jobvite, Avature and a few others never do). `below_min_pay` counts what min_pay left
    out.
    workplace: "remote", "hybrid" or "onsite", or several ("remote, hybrid"): leaves out an opening its site says
    (its own field, or its location's words: "Remote - US") is done another way. Most don't say: those stay. Results
    that say carry `workplace` and `workplace_source` (structured, location); `other_workplace` counts what it left
    out.
    employment_type: "full_time", "part_time", "contract", "temporary", "internship" or "per_diem", or several
    ("full_time, contract"): leaves out an opening its site (or its job in the tracker) says is only of other types.
    Most lists don't say: those stay. Results that say carry `employment_type` (the site's words) and
    `employment_types` (these names); `other_employment_type` counts what it left out.
    exclude: words or titles to leave out (["Senior", "Call Center"]): an opening whose title has one as whole words,
    any case, is left out ("Senior" leaves out a Senior Technician, never a Seniority Clerk); `excluded_titles` counts
    them (say how many). Pass the profile's preferences.avoid_titles (`search_area`'s `avoid_titles`) by default,
    unless the person asks to see those too; the Job Desk shows them under All, never recommended.
    within_miles: leaves out an opening every place of which is farther than this from the person's home town
    (personal.address.city and state in the profile), in a straight line between town centres; the plugin knows the
    towns of Arizona and the Las Vegas and Reno areas (data/places.yaml), and nothing is looked up online. An opening
    whose place it doesn't know ("2 Locations", a town elsewhere), that names places it can't place, or that is
    remote always stays; a hybrid one is judged by its place. Results whose place it knows carry `miles` (whole
    miles to the nearest town it names, `miles_to`) and `miles_sure` (true only when those are all its places, so
    none can be nearer; otherwise say "N miles to the nearest place it names; another may be nearer");
    `farther_away` counts what it
    left out. Refused, in words to pass on, when the profile has no home town it knows."""
    _check_min_pay(min_pay)
    home = _home_for(within_miles)
    places = _check_workplace(workplace)
    kinds = employment.parse_choice(employment_type)
    if kinds is None:
        raise ToolError(f"employment_type is full_time, part_time, contract, temporary, internship or per_diem, or "
                        f"several of them (\"full_time, contract\"); {employment_type!r} isn't. Leave it out for any.")
    if posted_within_days is not None and posted_within_days < 1:
        raise ToolError(f"posted_within_days is a number of days, 1 or more (7, 14, 30); {posted_within_days} isn't. "
                        "Leave it out for any day.")
    listed = every_employer() if every_list else load_companies()  # once: the person's own file may name several lists
    out = await search_companies(query, companies, location, limit_per_company, companies=listed,
                                 posted_within_days=posted_within_days)
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
        out["results"].extend(keep_listings(name, found, location_terms(location), limit_per_company, query,
                                            posted_within_days))
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
        out["results"].extend(keep_listings(name, found, location_terms(location), limit_per_company, query,
                                            posted_within_days))
    avoid = [w for w in config.avoid_list(exclude or []) if norm(w)]  # ("Senior, Call Center": two)
    t = tracker()
    kept, below, elsewhere, other_types, avoided, farther = [], 0, 0, 0, 0, 0
    for r in out["results"]:
        if avoid and avoided_title(str(r.get("title") or ""), avoid):
            avoided += 1
            continue
        job = t.find_by_url(r["url"])
        if job:
            r["tracked"] = {"id": job["id"], "status": job["status"]}
        said = pay.from_fields(r) or (pay.from_fields(job) if job else None)
        if said is not None:
            r["pay"] = pay.shown(said)
        if pay.meets(said, min_pay) is False:  # (listed and below: one not listed always stays)
            below += 1
            continue
        if job and wplace.better(dict(job), r):  # (where it's done, as its job in the tracker says it better)
            r.update({k: job[k] for k in wplace.FIELDS})
        if not wplace.keeps((r.get("workplace") or "", r.get("workplace_source") or ""), places):
            elsewhere += 1  # (said surely, and not one asked for: one that doesn't say always stays)
            continue
        said_type = r.get("employment_type") or (job["employment_type"] if job else "") or ""
        if employment.types_of(said_type):
            r["employment_type"], r["employment_types"] = said_type, employment.types_of(said_type)
        if not employment.keeps(employment.types_of(said_type), kinds):
            other_types += 1  # (said, and none of the types asked for: one that doesn't say always stays)
            continue
        if home is not None:
            far = towns.away(home, r.get("location"), job.get("location") if job else None)
            if far is not None:
                r["miles"], r["miles_to"], r["miles_sure"] = round(far["miles"]), far["place"], far["sure"]
            if not towns.keeps(far, r.get("workplace") or "", within_miles):
                farther += 1  # (surely farther, and not remote: an unknown place always stays)
                continue
        kept.append(r)
    out["results"] = kept
    if min_pay:
        out["below_min_pay"] = below
    if places:
        out["other_workplace"] = elsewhere
    if kinds:
        out["other_employment_type"] = other_types
    if avoid:
        out["excluded_titles"] = avoided
    if home is not None:
        out["farther_away"] = farther
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
    employment_type: str = "",
) -> dict[str, Any]:
    """Save a job from details you already have (e.g. from the Indeed connector or text the
    user pasted) when ingest_job can't read the page. employment_type: the type as given
    (Indeed's job_type: "Full-time", "Contract"), for the desk's Type filter."""
    p = finalize(Posting(url=url, title=title, company=company, description=description, location=location,
                         apply_url=apply_url, salary=salary, source=source, parse_method="manual",
                         employment_type=employment_type))
    job, created = tracker().upsert(p.to_dict())
    return {"created": created, "job": _brief(job)}


def _follow_up_days() -> int:
    """settings.follow_up_days, or its default while the profile doesn't read."""
    try:
        return config.Profile.load().settings.follow_up_days
    except Exception:
        return config.FOLLOW_UP_DAYS_DEFAULT


@tool()
def list_jobs(status: str | None = None, company: str | None = None, limit: int = 50,
              min_pay: float | None = None) -> dict[str, Any]:
    """List tracked jobs, newest activity first. status is one of:
    saved, in_progress, ready_to_submit, applied, interviewing, offer, rejected, withdrawn, skipped.

    A job sent in (ready_to_submit and after) also has `last_heard`, the newest employer
    email logged for it with log_email (category, day, one-line summary; null for none
    yet), and `follow_up_due`: the day a follow-up became due, for one applied to
    settings.follow_up_days days ago (10 unless the profile says) or more with no email
    logged, not even a confirmation; null otherwise. The Job Desk's My applications
    marks those with the day it became due and a Follow up button (a request to draft one, for Claude).

    A job the user marked Not interested on the Job Desk has `not_interested: true` and
    `not_interested_reason` (title, company, location, pay, other, or "" for none): ask
    before applying to it.

    A job whose pay is listed has `pay` ("$26–$28/hr (≈$54k–$58k/yr)") and `pay_yearly` (the most it pays a year;
    for "from $X", X a year). min_pay (dollars a year; hourly pay counts 2,080 hours) leaves out a job whose pay is
    listed on its pay line (or in its own data) and below it; a job that doesn't list its pay, or whose pay is only
    read from its words, stays. Up to `limit` jobs either way."""
    _check_min_pay(min_pay)
    t = tracker()
    if min_pay:
        jobs = [j for j in t.list(status=status, company=company, limit=100000)
                if pay.meets(pay.from_fields(j), min_pay) is not False][:limit]
    else:
        jobs = t.list(status=status, company=company, limit=limit)
    sent = any(j["status"] in APPLICATION_STATUSES for j in jobs)  # (none sent: neither the emails nor the profile read)
    heard, days = (t.last_heard(), _follow_up_days()) if sent else ({}, config.FOLLOW_UP_DAYS_DEFAULT)
    marks = t.not_interested_marks(_listed())
    also = t.aliases() if marks else {}
    out = []
    for j in jobs:
        brief = _brief(j)
        if marks and (mark := marks.mark(job=j, aliases=also.get(j["id"], []))) is not None:
            brief.update(not_interested=True, not_interested_reason=mark["reason"])
        if j["status"] in APPLICATION_STATUSES:
            stands = standing(j, heard.get(j["id"]), days)
            last = stands["last_heard"]
            brief["last_heard"] = {k: last[k] for k in ("category", "day", "summary")} if last else None
            brief["follow_up_due"] = stands["follow_up_due"]
        out.append(brief)
    return {"count": len(jobs), "jobs": out, "by_status": t.counts()}


@tool()
def get_job(job_id: int) -> dict[str, Any]:
    """Full record for one job, including description and status history. One the user
    marked Not interested on the Job Desk has `not_interested: true` and
    `not_interested_reason`: ask before applying to it."""
    job = _job(job_id)
    if (mark := _not_interested(job["id"])) is not None:
        job = {**job, "not_interested": True, "not_interested_reason": mark["reason"]}
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


def _forget_old_name(folder: Path, prof: config.Profile, kind: str, name: str) -> None:
    """A job's document under the name 0.3.188 and before gave it ("Mary Ann_Rivera_Resume.pdf": the names as written),
    gone once it's written under today's: else it sorted first and was uploaded, or its too-long marker held the job for
    ever (the review of 0.3.189). Only in a job's own folder, and only that one name."""
    old_stem = "_".join(str(p) for p in [prof.get("personal.first_name"), prof.get("personal.last_name")] if p)
    old = f"{old_stem}_{'Resume' if kind == 'resume' else 'Cover_Letter'}" if old_stem else kind
    if old == name:
        return
    for ext in (".pdf", ".md", ".too-long"):
        path = folder / f"{old}{ext}"
        with contextlib.suppress(OSError, ValueError):
            if path.parent.resolve() == folder.resolve() and path.is_file():  # (a name with "/" in it: never elsewhere)
                path.unlink()


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
    # (a name as a file's name: words joined by "_", never a character a file system refuses or reads as a folder,
    # "Ana/Luisa" or "O'Brien: Jr", the review of Oct 15; accented letters kept)
    stem = "_".join(part for p in [prof.get("personal.first_name"), prof.get("personal.last_name")] if p
                    for part in [re.sub(r"[^\w-]+", "_", str(p)).strip("_")] if part)
    name = f"{stem}_{'Resume' if kind == 'resume' else 'Cover_Letter'}" if stem else kind
    source = folder / f"{name}.md"
    folder.mkdir(parents=True, exist_ok=True)
    if job_id is not None:
        _forget_old_name(folder, prof, kind, name)
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
    "Lead with the experience and skills the posting asks for, in the posting's own words only where the resume "
    "says the same thing (\"customer sites\" isn't \"accounts in a territory\"; \"kept a maintenance log\" isn't "
    "\"accurate part-replacement records\").",
    "Never widen how much, how many or how often: \"maintained 18 machines\" and \"diagnosed faults\" don't make "
    "\"diagnosed faults on 18 machines\"; \"helped start up\" isn't \"led start-ups\". Keep past jobs in the past tense.",
    "Keep every title, employer and date exactly as the profile has them, jobs newest first. Coursework is not a degree.",
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
async def report_problem(job_id: int, anonymous: bool = False) -> dict[str, Any]:
    """Write a problem report for a job that went wrong: what the Job Desk did and the fields
    of the pages it saved (the page it stopped on among them), with the person's details taken
    out (no screenshots). Show the user `preview`, all of it, before anything else, and say the
    issue will be public and shows which job they applied for, unless it's anonymous. Only with
    their OK, give them `issue_url` to open (a new issue on the plugin's public GitHub repository
    with that text). Never file it without their OK. `zip` also holds the saved pages, scrubbed
    but possibly still showing their answers: it stays on their computer, and never goes on the
    public issue.

    anonymous=True says the job by its job system alone ("a Workday employer") and the step it
    stopped at: the employer, the job's title, its addresses and its requisition number are left
    out of the issue's title, its text and the saved pages. Offer it when the user would rather
    not show which job they applied for."""
    from . import desk, report
    from .pipeline import Applier

    job = _job(job_id)
    run = desk._desk.applier.runs.get(job["id"]) if desk._desk is not None else None
    # (and what the desk's history keeps out of a line on it, the report too)
    hide = await asyncio.to_thread(Applier._report_hidden, run) if run is not None else set()
    return await asyncio.to_thread(report.build, job, run, anonymous=anonymous, hide=hide)


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

    Next steps are usually click("Apply") / click("Easy Apply"), then autofill().
    `maybe_same`, when present: this job may be one already started or applied to on
    another site (an Indeed job and the employer's posting kept apart). Ask the person
    before applying. `not_interested`, when present: the person marked this job Not
    interested on the Job Desk. Ask before applying."""
    if job_id is None and not url:
        raise ValueError("Pass job_id or url")
    target, job = url or "", None
    if job_id is None:
        # the job saved at this address, if any: never the one opened before, whose documents
        # would go into this form and whose status a submit here would change
        known = tracker().find_by_url(url)
        job_id = known["id"] if known else None
    else:
        job = _job(job_id)
        target = url or job.get("apply_url") or job["url"]
    if config.own_file(target):  # ~/.job-apply's own files (its settings, saved pages): never opened as a page
        return {"error": "That's one of the desk's own files, not a job posting; it wasn't opened."}
    browser.current_job_id = job_id
    if job is not None and job["status"] == "saved":
        tracker().update(job["id"], status="in_progress", note="opened application")
    # a Greenhouse board that sends visitors to the employer's site (Carvana's is behind a
    # Cloudflare check): Greenhouse's own form, however the posting was named
    target = greenhouse_form_url(target) or target
    owner = _desk_tab_job(browser.current_tab)
    if owner is not None and owner != job_id:
        # the tab holds another job the Job Desk has paused or filled: it's left as it is, and
        # this one opens in a tab of its own (the desk would otherwise fill this page as that job)
        await browser.new_tab()
    try:
        summary = await browser.goto(target)
    except BrowserUnavailable as e:
        return {"error": str(e)}
    if browser.current_tab is not None:  # which job this tab is for, when Claude switches back to it
        if job_id is not None:
            browser.tab_jobs[browser.current_tab] = job_id
        else:
            browser.tab_jobs.pop(browser.current_tab, None)
    summary["ats"] = detect_ats(summary["url"])
    summary["submit_policy"] = _submit_policy(await browser.human_submit_ats() or summary["ats"])
    if maybe := _maybe_same(job_id):
        summary["maybe_same"] = {"jobs": maybe, "warning": MAYBE_SAME_SAYS}
    if (mark := _not_interested(job_id, url)) is not None:
        summary["not_interested"] = {"reason": mark["reason"], "warning": NOT_INTERESTED_SAYS}
    return summary


@tool()
async def inspect_form(include_dropdown_options: bool = True) -> dict[str, Any]:
    """List the fields and buttons on the current page (all frames). Each field has an id,
    kind (text, textarea, select, listbox, combobox, radio_group, checkbox_group, checkbox,
    file, password), label, required flag, options and current value. Buttons are listed
    under `actions`; is_submit marks the final submit button. A CAPTCHA on show is said
    under `captcha` (its own frame isn't listed): it's the user's to solve. A dialog open over
    the page (a notice to answer first) is listed under `dialogs` with its buttons; nothing is
    filled behind one. `busy` says a loading indicator is on show (the step isn't drawn yet: look
    again in a moment). Ids stay valid until the page changes; call this again after navigating."""
    data = await browser.inspect(include_dropdown_options)
    data["ats"] = detect_ats(data["url"])
    return data


# What a site's list calls a school it doesn't have, and the fill's words for "not in its list"
SCHOOL_NOT_LISTED = ("Other", "Not Listed")
_NOT_IN_LIST = re.compile(r"doesn't match any (suggestion|option)|nothing in its list matched")


# The country lists each job's pages showed on a country the site preset (autofill.preset_country), looked at once a job:
# a pick since is the person's, however the page draws the list again (a mark on the element goes with it: the review of
# 0.3.196). By job (or page, filled with none), by autofill.preset_key.
_PRESETS_SEEN: dict[Any, set[str]] = {}


def forget_presets(job_id: int) -> None:
    """The job's application opened again in a new tab: a fresh form, whose preset country lists are the site's again
    (the review of 0.3.196: closed unsaved and opened again, Workday showed Mexico again, and it stayed)."""
    _PRESETS_SEEN.pop(job_id, None)


@tool(drives=True)
async def autofill(job_id: int | None = None, overwrite: bool = False, redo: list[str] | None = None,
                   refresh: bool = False, keep: list[str] | None = None) -> dict[str, Any]:
    """Fill every field on the current page that the profile answers with confidence
    (contact details, address, work authorization, sponsorship, EEO choices, resume upload,
    saved answers). Returns what was filled and the fields still needing a decision —
    draft those from the profile/resume and confirm anything subjective with the user.
    `refresh`: on an application saved earlier (a draft), also put the profile's answer into
    boxes that show a different one (never a box the profile doesn't answer, nor one holding
    the user's own saved answer, nor the ids in `keep`); those are marked "refreshed"."""
    job = _job_here(job_id) if (job_id is not None or browser.current_job_id is not None) else {}
    data = await browser.inspect(include_dropdown_options=True)
    prof = config.Profile.load()
    seen = _PRESETS_SEEN.setdefault(job.get("id") or data.get("url"), set())
    plan = plan_autofill(data["fields"], prof, job, overwrite=overwrite, redo=set(redo or []), presets_done=seen,
                         refresh=refresh, keep=set(keep or []))
    results = await browser.fill([{"id": f["id"], "value": f["value"], "names": is_name_rule(f.get("rule") or ""),
                                   "near": place_words(f.get("rule") or "", prof),
                                   "pick_only": is_eeo_rule(f.get("rule") or ""),
                                   # (answered again, where the page lost it, even showing the answer: a date's parts;
                                   # and a draft's date part that shows another than the profile's)
                                   "again": f["id"] in (redo or []) or bool(f.get("again"))}
                                  for f in plan["to_fill"]]) if plan["to_fill"] else []
    if plan.get("judged"):  # (one whose fill didn't go in isn't judged yet: it's tried again, or asked)
        went, filling = {r["id"] for r in results if r.get("ok")}, {f["id"] for f in plan["to_fill"]}
        await browser.mark_judged([i for i in plan["judged"] if i not in filling],
                                  [i for i in plan["judged"] if i in filling and i in went])
    # a preset country left as it was (the profile's already) is looked at; one corrected, once its fill went in
    presets = plan.get("presets") or {}
    corrected = [i for i in presets if i in {f["id"] for f in plan["to_fill"]} and i in {r["id"] for r in results if r.get("ok")}]
    seen.update(key for i, key in presets.items() if i in corrected or i not in {f["id"] for f in plan["to_fill"]})
    by_id = {f["id"]: f for f in plan["to_fill"]}
    fields = {f["id"]: f for f in data["fields"]}
    # an answer that matched only a group (Robert Half's "Website / Job Board Posting", holding
    # "Company Career Site"): chosen again among the group's entries, and filled with that one.
    # So is a how-did-you-hear answer that matched nothing in a list the plan hadn't read (a Workday prompt
    # that draws its choices late: "Company Website" is its "<employer> website", issue #112, note 27)
    regrouped = {}
    for r in results:
        if r["ok"] or not r.get("options") or r["id"] not in fields:
            continue
        # (any other answer a list had no match for stays that: the fill looked at the same entries)
        if str(r.get("error") or "").startswith("NoMatch") and not is_how_heard(by_id.get(r["id"], {}).get("rule") or "",
                                                                   fields[r["id"]].get("label") or ""):
            continue
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
        # (a file the fill sent itself, "uploaded <name>": never one it found the page showing already)
        entry = {"id": r["id"], "label": src.get("label"), "value": src.get("value"), "from": src.get("rule"),
                 **({"refreshed": True} if src.get("refreshed") else {}),
                 **({"uploaded": True} if r["ok"] and str(r.get("result") or "").startswith("uploaded ") else {})}
        if r["ok"]:
            filled.append(entry)
        else:
            # what to ask instead: the field's kind, whether it's required, and its choices
            # (those of the group picked, when the answer was a group of entries)
            field = fields.get(r["id"], {})
            options = r.get("options") or field.get("options")
            failed.append({**entry, "error": r["error"], "kind": field.get("kind"),
                           "required": bool(field.get("required", True)), **({"options": options} if options else {}),
                           **{k: field[k] for k in ("section", "sublabel", "role") if field.get(k)}})
    after = await browser.inspect(include_dropdown_options=False)
    snapshot = await _auto_snapshot("autofill failures", failed) if failed else None
    return {
        "page": {"url": after["url"], "headings": after["headings"]},
        "filled": filled,
        "failed": failed,
        **({"debug_snapshot": snapshot} if snapshot else {}),
        "needs_input": plan["needs_input"],
        **({"countries_corrected": corrected} if corrected else {}),  # (the page may now draw that country's boxes)
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
    elif result.get("add_button_covered"):
        result["note"] = ("The section's Add button is covered by something on the page (a note or a dialog), so it "
                          + (f"was pressed {result['clicks']} time(s) and then " if result["clicks"] else "")
                          + "wasn't pressed (again); close what covers it, or add the blocks in the browser window.")
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
    _keep_newest(path.parent)
    return {"saved_to": str(path), "files": sorted(p.name for p in path.iterdir())}


@tool()
async def forget_saved_pages(job_id: int | None = None) -> dict[str, Any]:
    """Delete the pages saved of the person's applications: each job folder's debug/ (the pages saved to look into a
    stop) and its screenshot-*.jpg (the screenshot tool's), and the problem reports' pages and report.zip; one job's
    with job_id, else every job's and the home's own debug/. They hold each frame's HTML or a picture of the page as
    it was, the person's details on it among them. What stays: the pictures of a page before and after its Submit (the
    record of what was sent), a report's text, and everything else in the folders."""
    reports = config.home() / "reports"
    if job_id is not None:
        job = _job(job_id)
        folders = [Path(job["folder"])] if job.get("folder") else []
        report_dirs = sorted(reports.glob(f"{int(job['id']):04d}-*"))
        debugs = [f / "debug" for f in folders]
    else:
        # (every job folder there is, the tracker's or not: one whose row was merged into another's keeps its own)
        folders = sorted(p for p in config.applications_dir().glob("*") if p.is_dir())
        report_dirs = sorted(reports.glob("*"))
        debugs = [f / "debug" for f in folders] + [config.home() / "debug"]
    removed = 0
    for debug in debugs:
        if debug.is_dir():
            removed += sum(1 for d in debug.iterdir() if d.is_dir())
            shutil.rmtree(debug, ignore_errors=True)
    for folder in folders:
        for shot in folder.glob("screenshot-*.jpg"):
            with contextlib.suppress(OSError):
                shot.unlink()
                removed += 1
    for report in report_dirs:
        pages = report / "pages"
        if pages.is_dir():
            removed += sum(1 for p in pages.iterdir() if p.is_file())
            shutil.rmtree(pages, ignore_errors=True)
        with contextlib.suppress(OSError):
            (report / "report.zip").unlink(missing_ok=True)
    return {"removed": removed, "note": f"Deleted {removed} saved page(s) and screenshot(s)."}


@tool(drives=True)
async def fill_secret(field_id: str, secret_name: str) -> dict[str, Any]:
    """Type a stored secret (e.g. a career-site password) into a field without the value
    passing through the conversation. Secrets come from env JOB_APPLY_SECRET_<NAME> or
    ~/.job-apply/secrets.yaml. A system's password (workday_password) goes only onto that
    system's sites; one named for a site (linkedin_password, or the safer linkedin_com_password)
    only onto that site's own domain, or its tenant on a shared job system (honeywell_password
    on honeywell.wd5.myworkdayjobs.com)."""
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
        # a password saved for one system (workday_password) goes only onto that system's sites; any other only
        # onto the site it's named for, by the site's own domain (linkedin_password: linkedin.com, or linkedin.<any
        # top-level domain>; linkedin_com_password and www_linkedin_com_password name it exactly) or its tenant on
        # a shared job system (honeywell_password: honeywell.wd5.myworkdayjobs.com). A host that merely has the
        # name in it (linkedin.evil.example, notlinkedin-careers.example) is somebody else's
        if password_for(url) == name:
            return True
        if site in PASSWORD_SITES:
            return False
        parsed = urlparse(url or "")
        host = re.sub(r"[^a-z0-9.]", "", (parsed.hostname or "").lower())
        domain, named = site_domain(host), site.replace("_", "")
        return bool(parsed.scheme == "https" and site and (
            named in (domain.split(".")[0], domain.replace(".", ""), host.replace(".", ""))
            or named == host.split(".")[0] and shared_system(url) is not None))

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


@tool()
async def desk_history(days: int = 7, recent: int = 20) -> dict[str, Any]:
    """How the Job Desk's last `days` days went (its "This week" window), from the line it
    writes in ~/.job-apply/runs.jsonl each time it's done with a job for now (the desk
    needn't be open): jobs filled up to their review page (`reviewed`, ones the desk sent
    included), sent by the desk (`submitted`), marked as applied by the user or found
    already applied (`marked`), `failed`, `skipped`, passes cut short by the desk stopping
    (`stopped`), its stops for the user by kind (`pauses`: questions, sign_in, bot_check,
    email_code, ...), the time with the desk (`minutes` in all and `median_minutes` a job:
    waits for sites and the inbox included, and jobs filled at once overlap, so it's more
    than the work itself), the employers it stopped at most (`stops`, with the kind of
    stop), and its newest `recent` lines, newest first (each with the job, when it began and
    ended, its minutes, how it ended and why, the steps it went through, the desk's rounds
    that took the user to it and their Resumes). `any` is false when there's no history
    yet. Use it for "how did this week go?"."""
    from . import history

    if not 1 <= days <= 366 or not 1 <= recent <= 200:
        raise ValueError("days should be from 1 to 366, and recent from 1 to 200")
    return {**await asyncio.to_thread(history.summary, days, recent), "file": str(history.history_path())}


WHATS_NEW_FIRST = 24 * 3600  # what the first call of whats_new looks back over, with no call before it to go from


def _since_time(said: str, now: float) -> float:
    """whats_new's `since`: a date or a time ("2026-10-17", "2026-10-17T08:00", this computer's time unless it says its
    offset), or how long ago ("12h", "3d")."""
    text = said.strip()
    ago = re.fullmatch(r"(\d{1,4})\s*([hd])", text.lower())
    if ago:
        return now - int(ago.group(1)) * (3600 if ago.group(2) == "h" else 86400)
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return (t if t.tzinfo else t.astimezone()).timestamp()
    except (ValueError, OverflowError, OSError):  # (OSError: a time this computer's clock can't say, year 1 on Windows)
        raise ValueError(f"since should be a date or time like 2026-10-17 or 2026-10-17T08:00, or 12h or 3d, "
                         f"not {said!r}") from None


@tool()
def whats_new(since: str | None = None) -> dict[str, Any]:
    """A morning check: the openings first saved since `since` (a date or time like "2026-10-17" or
    "2026-10-17T08:00", or "12h", "3d"), or since this tool's last call (kept in ~/.job-apply;
    the last 24 hours at the first call). From the Job Desk's openings (each by when a search
    first found it) and the jobs saved to the tracker another way (ingest_job, add_job), none
    begun, sent or skipped, and none the user said Not interested in on the desk; best score
    first, each with its title, company, location, score (out of 100; `recommended` from 60),
    address and job number once saved, its pay ("$26–$28/hr (≈$54k–$58k/yr)", "" when its posting
    doesn't list it or isn't read yet), `pay_yearly` (the most it pays a year; for "from $X",
    X a year) and `ats`. `summary` lists them in a few lines, and
    `searched_at` says when the desk last searched. It never searches by itself: for fresh
    openings, search first (the desk's Find jobs, or search_company_jobs with the find-jobs
    skill), then call this. Each morning: search, then whats_new."""
    from . import desk as job_desk
    from .history import stamp

    now = time.time()
    path = job_desk._whats_new_path()
    if since is not None:
        start, first = _since_time(since, now), False
    else:
        try:
            kept = json.loads(path.read_text(encoding="utf-8")).get("at")
        except (OSError, ValueError, AttributeError):
            kept = None
        last = job_desk._seconds(kept)
        start, first = (now - WHATS_NEW_FIRST, True) if last is None else (last, False)
    found = job_desk.whats_new(tracker(), start)
    config.ensure_home()
    # (the next call without `since` starts here: to the second, as the tracker keeps its times, so a job saved in the
    # second of this call is in the next one too, never in neither)
    config.write_text_atomic(path, json.dumps({"at": int(now)}))
    try:
        searched = json.loads(job_desk._recommendations_path().read_text(encoding="utf-8")).get("at")
    except (OSError, ValueError, AttributeError):
        searched = None
    searched_at = stamp(s) if (s := job_desk._seconds(searched)) is not None else None
    shown = found[:job_desk.WHATS_NEW_MOST]
    when = stamp(start)[:16].replace("T", " ")
    if found:
        summary = "\n".join([f"{len(found)} new opening{'s' if len(found) != 1 else ''} since {when}, best first:"]
                            + [f"- {o['score']} · {o['title']} · {o['company']}" + (f" · {o['location']}" if o['location']
                                                                                     else "") for o in shown[:10]]
                            + ([f"and {len(found) - 10} more"] if len(found) > 10 else []))
    else:
        summary = f"Nothing new since {when}."
    if searched_at is None:
        summary += " The Job Desk hasn't searched yet: press Find jobs there, or search with the find-jobs skill."
    return {"since": stamp(start), "until": stamp(now), "first_call": first, "count": len(found), "openings": shown,
            "summary": summary, "searched_at": searched_at}


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


# Tracker statuses that mean the application is no longer the desk's to send (pipeline.FINISHED, and a Skip)
_SUBMIT_GONE = frozenset({"applied", "interviewing", "offer", "rejected", "withdrawn", "skipped"})
# A job the tracker kept apart from one it may be the same posting as (an Indeed job and the employer's): what Claude's
# tools say while that one is begun or applied to, as the Job Desk's card asks (the review of 0.3.206)
MAYBE_SAME_SAYS = "may be the job already started or applied to: ask the person before applying"
_BEGUN_OR_DONE = frozenset({"in_progress", "ready_to_submit", "applied", "interviewing", "offer", "rejected", "withdrawn"})


# A job the person marked Not interested on the Job Desk: what Claude's tools say of it, and submit_application presses
# nothing for it without user_confirmed (the review of 0.3.207)
NOT_INTERESTED_SAYS = "the person marked this job Not interested: ask before applying"


def _not_interested(job_id: int | None, url: str | None = None) -> dict[str, Any] | None:
    """The person's Not interested mark on this job (by its number or any of its addresses), or on an opening at this
    address not saved as a job; None for none."""
    if job_id is None and not url:
        return None
    return tracker().marked_not_interested(url=url if job_id is None else None, job_id=job_id, listed=_listed())


def _listed() -> list[dict[str, Any]]:
    """The openings the Job Desk lists (a mark made before its job was saved is ambiguous among them)."""
    from . import desk as job_desk  # (the desk's own module imports this one)

    return job_desk.listed_openings()


def _maybe_same(job_id: int | None) -> list[dict[str, Any]]:
    """The jobs begun or applied to that this one may be the same posting as (Tracker.maybe_same: until the person
    said Apply anyway on the desk), each by its id, title, company and status."""
    if job_id is None:
        return []
    return [{"job_id": o["id"], "title": o.get("title") or "", "company": o.get("company") or "", "status": o["status"]}
            for o in tracker().maybe_same(job_id) if o.get("status") in _BEGUN_OR_DONE]


@tool(drives=True)
async def submit_application(job_id: int | None = None, user_confirmed: bool = False) -> dict[str, Any]:
    """Click the final Submit button on the current page and record the result.

    Only call after showing the user the filled review page and they said to submit
    (user_confirmed=true), or when settings.submit_mode is "auto" for this ATS.
    On LinkedIn and Indeed this never clicks: it marks the job ready_to_submit and
    the user clicks Submit in the browser; then call update_job(status="applied").
    A job that may be one already started or applied to on another site (`maybe_same`
    from open_application), or one the user marked Not interested on the Job Desk
    (`not_interested`), is never pressed without user_confirmed=true."""
    from . import desk as job_desk  # (the desk's own module imports this one)
    if job_desk._desk is not None and job_desk._desk.applier.hold_submits:
        return {"submitted": False, "reason": "The Job Desk's Troubleshooting is on, so nothing is submitted. Turn it "
                "off on the desk first."}
    job = _job_here(job_id)
    was = job.get("status")  # (what the tracker said as this began: see before the press)
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
    if (unwanted := _not_interested(job["id"])) is not None and not user_confirmed:
        return {"submitted": False, "reason": f"Not pressed: {NOT_INTERESTED_SAYS}.",
                "not_interested": {"reason": unwanted["reason"]}}
    if (maybe := _maybe_same(job["id"])) and not user_confirmed:
        said = "; ".join(f"job {o['job_id']} ({o['title']} at {o['company']}, {o['status'].replace('_', ' ')})"
                         for o in maybe)
        return {"submitted": False, "reason": f"Not pressed: this {MAYBE_SAME_SAYS} ({said}).", "maybe_same": maybe}
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
                "no_submit_button": True, "empty_required": empty_required,
                "actions": [a["text"] for a in page["actions"]]}
    folder = Path(job["folder"])
    # a keepsake of the page as sent, when Chrome can take one (CI, Oct 12: "Unable to capture screenshot"
    # stopped a Submit the person had pressed)
    with contextlib.suppress(Exception):
        await browser.screenshot(full_page=True, save_to=folder / "before-submit.jpg")
    # A record of the press, written before it: with no confirmation (or a page that closes itself
    # as it sends) the application may still have gone, and the Job Desk reads this, after a
    # restart too, so as never to press Submit again by itself
    record = folder / "submission.json"
    # Marked applied (the desk's "I submitted it") or skipped while the page was looked over just now: the application
    # is the person's, so nothing is pressed (a second one can't be taken back). Only a change since this began: a call
    # on a job the tracker said was applied already goes on as before
    now = (tracker().get(job["id"], with_description=False) or {}).get("status")
    if now != was and now in _SUBMIT_GONE:
        return {"submitted": False, "already": True,
                "reason": "Not pressed: the job was marked as applied (or skipped) while its page was checked."}
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
