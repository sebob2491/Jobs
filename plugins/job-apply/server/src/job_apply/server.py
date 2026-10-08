"""MCP server: job intake, application tracking and browser form filling."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import config
from .ats import ATS_NAMES, detect_ats, greenhouse_form_url
from .autofill import is_empty_value, is_name_rule, place_words, plan_autofill, profile_entries
from .browser import BrowserSession, BrowserUnavailable, SiteDown, SubmitBlocked
from .postings import FetchError, Posting, fetch_posting, finalize, parse_html
from .render import KINDS, render_pdf, to_html
from .search import (CLIENT_SIDE, alternatives, eightfold_page_url, icims_search, infor_search, keep_listings,
                     load_companies, location_terms, parse_eightfold, paycom_search, rmk_search, search_companies,
                     sfclassic_search, sitecore_search, ukg_search)
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


@mcp.tool()
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
        "settings_warnings": s.warnings,
        "settings": {"submit_mode": s.submit_mode, "auto_submit_ats": s.auto_submit_ats,
                     "browser_channel": s.browser_channel, "headless": s.headless,
                     "email_codes": s.email_codes, "email_tracking": s.email_tracking},
        "chrome_detected": has_chrome,
        "browser_note": "The desk uses Google Chrome, or Microsoft Edge without it. If neither starts, install Chrome or run "
                        f"`uv run --project \"{config.PLUGIN_ROOT / 'server'}\" playwright install chromium` "
                        "and set settings.browser_channel: chromium.",
        "plugin_root": str(config.PLUGIN_ROOT),
        "plugin_version": config.plugin_version(),
        "companies_file": str(config.PLUGIN_ROOT / "data" / "companies.yaml"),
        "jobs_by_status": tracker().counts(),
    }


@mcp.tool()
def get_profile() -> dict[str, Any]:
    """Return the applicant profile (~/.job-apply/profile.yaml) used to answer forms.
    Edit that file directly to change answers."""
    prof = config.Profile.load()
    return {"path": str(prof.path), "profile": prof.data, "missing_required": prof.missing_required()}


# --------------------------------------------------------------------- jobs


@mcp.tool()
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
        try:
            opened = await browser.goto(url)
            if opened.get("navigation_error"):
                return {"saved": False, "error": f"The browser couldn't open the page: {opened['navigation_error']}"}
            posting = finalize(parse_html(await browser.html(), url))
        except BrowserUnavailable as e:
            return {"saved": False, "error": str(e)}
    job, created = tracker().upsert(posting.to_dict())
    return {"saved": True, "created": created, "job": job, "warnings": posting.warnings}


@mcp.tool()
async def search_company_jobs(
    query: str,
    companies: list[str] | None = None,
    location: str | None = "AZ",
    limit_per_company: int = 20,
) -> dict[str, Any]:
    """Search employers' own careers sites for openings through their applicant tracking
    system's public search: Workday, Greenhouse, Lever, Eightfold, SmartRecruiters, Oracle,
    ApplicantStack. ASML's site, iCIMS portals, Paycom and UKG Pro boards and SuccessFactors'
    newer search (Edwards) and older career sites (Amkor) and Infor CloudSuite boards (Benchmark)
    are read in a background browser tab.

    query: keywords; separate alternatives with "|", e.g. "field service | equipment engineer".
    companies: names from the plugin's companies list (default: all of them).
    location: state code/name or city alternatives ("AZ", "Phoenix|Chandler"); null for anywhere.
    Results already in the tracker carry `tracked`. Companies in `browser_only` have no
    search API; open their careers_url and use the site's search."""
    out = await search_companies(query, companies, location, limit_per_company)
    # Eightfold career sites refuse scripted API calls; let a real page make the call instead.
    by_name = {c["name"]: c for c in load_companies()}
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
        wordings = [query] if item["kind"] in CLIENT_SIDE else alternatives(query)
        for wording in wordings:
            try:
                if item["kind"] == "icims":
                    await icims_search(browser.frames_html, cfg, wording, found)
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


@mcp.tool()
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


@mcp.tool()
def list_jobs(status: str | None = None, company: str | None = None, limit: int = 50) -> dict[str, Any]:
    """List tracked jobs, newest activity first. status is one of:
    saved, in_progress, ready_to_submit, applied, interviewing, offer, rejected, withdrawn, skipped."""
    jobs = tracker().list(status=status, company=company, limit=limit)
    return {"count": len(jobs), "jobs": [_brief(j) for j in jobs], "by_status": tracker().counts()}


@mcp.tool()
def get_job(job_id: int) -> dict[str, Any]:
    """Full record for one job, including description and status history."""
    job = _job(job_id)
    return {"job": job, "history": tracker().events(job_id)}


@mcp.tool()
def update_job(job_id: int, status: str | None = None, notes: str | None = None,
               apply_url: str | None = None, event_note: str = "") -> dict[str, Any]:
    """Change a job's status / notes / apply URL. Use status="applied" after the user
    submits an application themselves. Valid statuses: saved, in_progress,
    ready_to_submit, applied, interviewing, offer, rejected, withdrawn, skipped."""
    job = tracker().update(job_id, status=status, notes=notes, apply_url=apply_url, note=event_note)
    return {"job": _brief(job)}


@mcp.tool()
def log_email(job_id: int, thread_id: str, category: str, summary: str = "", received_at: str = "") -> dict[str, Any]:
    """Record an employer's email about an application and update its status.

    category: confirmation, assessment, interview, offer, rejection or other.
    thread_id is the Gmail thread id, and received_at the date of the message: the same
    message is never counted twice (already_logged=true), while a later one in the same
    thread (a rejection under the confirmation) is. Status only moves forward
    (applied -> interviewing -> offer); a rejection sets rejected unless there's already
    an offer."""
    return tracker().log_email(job_id, thread_id, category, summary, received_at)


@mcp.tool()
def logged_emails(since_days: int | None = 90) -> dict[str, Any]:
    """Gmail threads already recorded with log_email, with the date of the message logged
    (received_at), so a status check can skip them unless a newer message has arrived."""
    rows = tracker().logged_threads(since_days)
    return {"count": len(rows), "threads": rows}


@mcp.tool()
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
    folder = config.ensure_home() if default else Path(_job(job_id)["folder"])
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


@mcp.tool()
def tailoring_queue() -> dict[str, Any]:
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


@mcp.tool()
def export_jobs_csv(path: str | None = None) -> dict[str, Any]:
    """Write every tracked job to a CSV (default ~/.job-apply/applications.csv) for a spreadsheet."""
    out = config.expand(path) if path else config.home() / "applications.csv"
    assert out is not None
    n = tracker().export_csv(out)
    return {"path": str(out), "rows": n}


# --------------------------------------------------------------------- browser


@mcp.tool()
async def open_application(job_id: int | None = None, url: str | None = None) -> dict[str, Any]:
    """Open a job's application (or its posting page, when there is no separate apply URL)
    in the visible browser and make it the current job. Returns a summary of the page:
    headings, number of fields, buttons and errors.

    Next steps are usually click("Apply") / click("Easy Apply"), then autofill()."""
    if job_id is None and not url:
        raise ValueError("Pass job_id or url")
    target = url
    if job_id is not None:
        job = _job(job_id)
        target = url or job.get("apply_url") or job["url"]
        target = greenhouse_form_url(target) or target
        browser.current_job_id = job_id
        if job["status"] == "saved":
            tracker().update(job_id, status="in_progress", note="opened application")
    try:
        summary = await browser.goto(target)  # type: ignore[arg-type]
    except BrowserUnavailable as e:
        return {"error": str(e)}
    summary["ats"] = detect_ats(summary["url"])
    summary["submit_policy"] = _submit_policy(summary["ats"])
    return summary


@mcp.tool()
async def inspect_form(include_dropdown_options: bool = True) -> dict[str, Any]:
    """List the fields and buttons on the current page (all frames). Each field has an id,
    kind (text, textarea, select, listbox, combobox, radio_group, checkbox_group, checkbox,
    file, password), label, required flag, options and current value. Buttons are listed
    under `actions`; is_submit marks the final submit button. Ids stay valid until the
    page changes; call this again after navigating."""
    data = await browser.inspect(include_dropdown_options)
    data["ats"] = detect_ats(data["url"])
    return data


@mcp.tool()
async def autofill(job_id: int | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Fill every field on the current page that the profile answers with confidence
    (contact details, address, work authorization, sponsorship, EEO choices, resume upload,
    saved answers). Returns what was filled and the fields still needing a decision —
    draft those from the profile/resume and confirm anything subjective with the user."""
    job = _job(job_id) if (job_id is not None or browser.current_job_id is not None) else {}
    data = await browser.inspect(include_dropdown_options=True)
    prof = config.Profile.load()
    plan = plan_autofill(data["fields"], prof, job, overwrite=overwrite)
    results = await browser.fill([{"id": f["id"], "value": f["value"], "names": is_name_rule(f.get("rule") or ""),
                                   "near": place_words(f.get("rule") or "", prof)}
                                  for f in plan["to_fill"]]) if plan["to_fill"] else []
    by_id = {f["id"]: f for f in plan["to_fill"]}
    fields = {f["id"]: f for f in data["fields"]}
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
                           "required": bool(field.get("required", True)), **({"options": options} if options else {})})
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def debug_snapshot(note: str = "") -> dict[str, Any]:
    """Save the current page (HTML of every frame, full screenshot, extracted fields) to the
    job's debug folder. Use it when a page behaves unexpectedly — a field that won't fill,
    a button that does nothing, a missed label — so the case can become a regression test
    (see the README's "Turning a failure into a test")."""
    path = await browser.snapshot(_snapshot_dir(), note=note)
    return {"saved_to": str(path), "files": sorted(p.name for p in path.iterdir())}


@mcp.tool()
async def fill_secret(field_id: str, secret_name: str) -> dict[str, Any]:
    """Type a stored secret (e.g. a career-site password) into a field without the value
    passing through the conversation. Secrets come from env JOB_APPLY_SECRET_<NAME> or
    ~/.job-apply/secrets.yaml."""
    if re.sub(r"[^a-z0-9]", "_", secret_name.strip().lower()) == "email_password":
        # the key to the person's inbox: the desk reads sign-up codes with it, nothing types it anywhere
        return {"ok": False, "error": "The email app password is only for reading sign-up codes from the inbox; "
                                      "it is never typed into a page."}
    secret = config.get_secret(secret_name)
    if secret is None:
        return {"ok": False, "error": f"No secret named {secret_name!r}. Ask the user to add it to "
                f"{config.secrets_path()} or set JOB_APPLY_SECRET_{secret_name.upper()}, or to type it in the browser."}
    await browser.fill_secret(field_id, secret)
    return {"ok": True}


@mcp.tool()
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


@mcp.tool(structured_output=False)
async def screenshot(full_page: bool = False, job_id: int | None = None) -> list[Any]:
    """Screenshot of the current browser tab (JPEG). Use it to check tricky widgets and to
    show the user the review page before submitting. Saved into the job's folder too."""
    save = None
    jid = job_id if job_id is not None else browser.current_job_id
    if jid is not None and (job := tracker().get(jid)):
        save = Path(job["folder"]) / f"screenshot-{datetime.now():%Y%m%d-%H%M%S}.jpg"
    data = await browser.screenshot(full_page=full_page, save_to=save)
    return [Image(data=data, format="jpeg"), f"saved: {save}" if save else "not saved (no current job)"]


@mcp.tool()
async def page_text(max_chars: int = 8000) -> str:
    """Visible text of the current tab, for reading instructions, errors or a confirmation page."""
    return await browser.visible_text(max_chars)


@mcp.tool()
async def tabs(switch_to: int | None = None) -> dict[str, Any]:
    """List open browser tabs, or switch to tab number `switch_to`."""
    return await browser.tabs(switch_to)


@mcp.tool()
async def close_browser() -> dict[str, Any]:
    """Close the automation browser (sign-ins are kept in the profile for next time)."""
    await browser.close()
    return {"closed": True}


@mcp.tool()
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


def _submit_policy(ats: str) -> str:
    s = config.Profile.load().settings
    if s.dry_run:
        return "dry_run"
    if ats in config.HUMAN_SUBMIT_ONLY:
        return "user_clicks_submit"
    return "auto" if s.may_auto_submit(ats) else "after_user_confirms"


@mcp.tool()
async def submit_application(job_id: int | None = None, user_confirmed: bool = False) -> dict[str, Any]:
    """Click the final Submit button on the current page and record the result.

    Only call after showing the user the filled review page and they said to submit
    (user_confirmed=true), or when settings.submit_mode is "auto" for this ATS.
    On LinkedIn and Indeed this never clicks: it marks the job ready_to_submit and
    the user clicks Submit in the browser; then call update_job(status="applied")."""
    job = _job(job_id)
    page = await browser.inspect(include_dropdown_options=False)
    ats = detect_ats(page["url"])
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
    result = await browser.press_submit(buttons[-1]["id"])
    await browser.screenshot(full_page=False, save_to=folder / "after-submit.jpg")
    if result["confirmed"]:
        tracker().update(job["id"], status="applied", note=f"submitted via {ATS_NAMES.get(ats, ats)}")
    (folder / "submission.json").write_text(json.dumps({**result, "at": datetime.now().isoformat()}, indent=2))
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
