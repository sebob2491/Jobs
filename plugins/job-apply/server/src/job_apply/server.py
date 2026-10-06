"""MCP server: job intake, application tracking and browser form filling."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import config
from .ats import ATS_NAMES, detect_ats
from .autofill import is_empty_value, plan_autofill
from .browser import BrowserSession, BrowserUnavailable, SubmitBlocked
from .postings import FetchError, Posting, fetch_posting, finalize, parse_html
from .tracker import Tracker

INSTRUCTIONS = """\
Job application assistant. Typical flow for one posting:
  ingest_job(url) -> open_application(job_id) -> autofill() -> inspect_form() ->
  fill_form([...]) for remaining answers -> click("Next"/"Continue") -> repeat ->
  on the review page: screenshot() for the user -> submit_application(job_id, user_confirmed=true)
  only after the user says to submit.
Rules: never invent facts about the applicant; answers must come from the profile or the
user. LinkedIn and Indeed applications are always submitted by the user clicking the
button themselves. Passwords go through fill_secret, never fill_form."""

mcp = MCPServer("job-apply", instructions=INSTRUCTIONS, version="0.1.0")
browser = BrowserSession()
_tracker: Tracker | None = None


def tracker() -> Tracker:
    global _tracker
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


def _brief(job: dict[str, Any]) -> dict[str, Any]:
    keys = ["id", "status", "title", "company", "location", "ats", "source", "url", "apply_url", "salary", "folder"]
    out = {k: job.get(k) for k in keys}
    out["ats_name"] = ATS_NAMES.get(job.get("ats", ""), job.get("ats"))
    return out


# --------------------------------------------------------------------- setup


@mcp.tool()
def setup_status() -> dict[str, Any]:
    """Check what the plugin needs before it can apply: profile fields, resume file,
    browser. Creates ~/.job-apply and a profile template on first run."""
    home = config.ensure_home()
    prof = config.Profile.load()
    missing = prof.missing_required()
    has_chrome = bool(shutil.which("google-chrome") or shutil.which("chrome") or Path(
        "/Applications/Google Chrome.app").exists() or Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe").exists())
    s = prof.settings
    return {
        "home": str(home),
        "profile_path": str(config.profile_path()),
        "profile_complete": not missing,
        "missing_profile_fields": missing,
        "settings": {"submit_mode": s.submit_mode, "auto_submit_ats": s.auto_submit_ats,
                     "browser_channel": s.browser_channel, "headless": s.headless},
        "chrome_detected": has_chrome,
        "browser_note": "If the browser fails to start, install Chrome or run "
                        f"`uv run --project \"{config.PLUGIN_ROOT / 'server'}\" playwright install chromium` "
                        "and set settings.browser_channel: chromium.",
        "plugin_root": str(config.PLUGIN_ROOT),
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
            await browser.goto(url)
            posting = finalize(parse_html(await browser.html(), url))
        except BrowserUnavailable as e:
            return {"saved": False, "error": str(e)}
    job, created = tracker().upsert(posting.to_dict())
    return {"saved": True, "created": created, "job": job, "warnings": posting.warnings}


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
    plan = plan_autofill(data["fields"], config.Profile.load(), job, overwrite=overwrite)
    results = await browser.fill([{"id": f["id"], "value": f["value"]} for f in plan["to_fill"]]) if plan["to_fill"] else []
    by_id = {f["id"]: f for f in plan["to_fill"]}
    filled, failed = [], []
    for r in results:
        src = by_id.get(r["id"], {})
        entry = {"id": r["id"], "label": src.get("label"), "value": src.get("value"), "from": src.get("rule")}
        if r["ok"]:
            filled.append(entry)
        else:
            failed.append({**entry, "error": r["error"]})
    after = await browser.inspect(include_dropdown_options=False)
    return {
        "page": {"url": after["url"], "headings": after["headings"]},
        "filled": filled,
        "failed": failed,
        "needs_input": plan["needs_input"],
        "already_filled": len(plan["already_filled"]),
        "errors": after["errors"],
        "actions": [a for a in after["actions"] if not a.get("disabled")][:25],
    }


@mcp.tool()
async def fill_form(values: list[dict[str, Any]]) -> dict[str, Any]:
    """Fill specific fields: values = [{"id": "12", "value": "..."}]. Ids come from
    inspect_form/autofill. Options are matched loosely ("Yes", "AZ" -> "Arizona"). For
    checkbox_group pass a list. For file fields pass a local file path."""
    results = await browser.fill(values)
    return {"results": results, "ok": all(r["ok"] for r in results)}


@mcp.tool()
async def fill_secret(field_id: str, secret_name: str) -> dict[str, Any]:
    """Type a stored secret (e.g. a career-site password) into a field without the value
    passing through the conversation. Secrets come from env JOB_APPLY_SECRET_<NAME> or
    ~/.job-apply/secrets.yaml."""
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


def _submit_policy(ats: str) -> str:
    s = config.Profile.load().settings
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
    if policy == "user_clicks_submit":
        tracker().update(job["id"], status="ready_to_submit", note=f"filled on {ATS_NAMES.get(ats, ats)}")
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
