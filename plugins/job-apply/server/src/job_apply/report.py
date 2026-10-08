"""A problem report for one job: what the desk did and the fields of the pages it stopped on,
with the person's details taken out, to file on the plugin's GitHub issues so it can be fixed.

Nothing leaves the computer here. The report is written to ~/.job-apply/reports/, its text
is shown to the person, and only they file it: the issue page opens with that text filled
in. The saved pages go in report.zip beside it, scrubbed, but they are never put in the
issue: a filled-in form still shows answers no scrubber can know are the person's (the
choices they picked, say). Screenshots are never included: a picture can't be scrubbed.
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import config
from .autofill import is_empty_value
from .fixtures import REDACTED, _without_query, convert, personal_strings, redact

PAGES = 3  # the latest saved pages put in a report
ISSUE_BODY = 6000  # characters of the report in the issue's address at most...
ISSUE_URL = 7000  # ...and of the whole address, once encoded (GitHub refuses one much over 8 KB)
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")


def _repository() -> str:
    try:
        repo = json.loads((config.PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["repository"]
    except (OSError, ValueError, KeyError, TypeError):
        repo = ""
    return str(repo).rstrip("/") or "https://github.com/sebob2491/Jobs"


def report_strings(prof: config.Profile) -> list[str]:
    """What a report takes out: the personal details fixtures take out, plus the person's
    city, schools and employers, which a test fixture keeps but a public issue mustn't."""
    more: list[Any] = [prof.get("personal.address.city"), prof.get("education.school"),
                       prof.get("experience.current_company"), *(prof.get("history.previous_employers") or [])]
    for key, field in (("work_history", "company"), ("education_history", "school")):
        more += [e.get(field) for e in prof.get(key) or [] if isinstance(e, dict)]
    extra = {str(v).strip() for v in more if v and not isinstance(v, (dict, list)) and len(str(v).strip()) >= 3}
    return sorted(set(personal_strings(prof)) | extra, key=len, reverse=True)


def _snapshots(folder: Path) -> list[Path]:
    debug = folder / "debug"
    found = [p for p in debug.iterdir() if p.is_dir() and (p / "snapshot.json").exists()] if debug.is_dir() else []
    return sorted(found)[-PAGES:]


def _fields(meta: dict[str, Any]) -> list[str]:
    """Each field as its label, kind, whether required and whether filled: never its value.
    A dropdown still on "Select One" is empty, and a box holding 0 is filled."""
    out = []
    for f in meta.get("fields") or []:
        if not isinstance(f, dict) or not f.get("label"):
            continue
        value = f.get("value")
        number = isinstance(value, (int, float)) and not isinstance(value, bool)
        filled = "filled" if number or not (value is None or value == "" or is_empty_value(value)) else "empty"
        out.append(f"- {f['label']} ({f.get('kind', '?')}{', required' if f.get('required') else ''}, {filled})")
    return out


def _folder(job_id: int) -> Path:
    """A new folder for this report: two reports in the same second (a double click, or the
    desk and Claude at once) each get their own."""
    base = config.home() / "reports" / f"{job_id:04d}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    base.parent.mkdir(parents=True, exist_ok=True)
    for n in range(1, 100):
        folder = base if n == 1 else base.with_name(f"{base.name}-{n}")
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            continue
    raise RuntimeError(f"couldn't make a folder for the report beside {base}")


def _issue_url(title: str, text: str) -> str:
    """The new-issue address with as much of the report as fits: the cap is on the encoded
    address, since encoding a quote or a line break makes it several characters."""
    repo, limit = _repository(), ISSUE_BODY
    while True:
        body = text if len(text) <= limit else text[:limit] + "\n\n(cut short: the whole report is in report.md)\n"
        url = f"{repo}/issues/new?title={quote(title)}&body={quote(body)}&labels=report"
        if len(url) <= ISSUE_URL or limit <= 200:
            return url
        limit = min(limit - 100, int(limit * ISSUE_URL / len(url)))


def build(job: dict[str, Any], run: Any = None, profile: config.Profile | None = None) -> dict[str, Any]:
    """Write the report for a job and return what it holds: its folder, report.zip (the text
    and the scrubbed pages, kept on this computer), the text the person sees before anything
    is filed (`preview`), and the address of a new GitHub issue with that text (`issue_url`)."""
    prof = profile or config.Profile.load()
    secrets = report_strings(prof)

    def scrub(text: Any) -> str:  # addresses lose their queries (session ids, tokens) before the redaction
        return redact(_URL.sub(lambda m: _without_query(m.group(0)), str(text or "")), secrets)

    out_dir = _folder(int(job["id"]))
    pages_dir = out_dir / "pages"
    pages_dir.mkdir()

    lines = [
        f"**Plugin version:** {config.plugin_version() or 'unknown'}",
        f"**Job:** {scrub(job.get('title'))} at {scrub(job.get('company'))}",
        f"**Job system:** {job.get('ats') or 'unknown'}",
        f"**Address:** {scrub(job.get('apply_url') or job.get('url') or '')}",
    ]
    if run is not None:
        lines += [f"**Desk status:** {getattr(run, 'status', '')} {getattr(run, 'need', '')}".rstrip(),
                  f"**What the desk said:** {scrub(getattr(run, 'reason', ''))}"]
        steps = [scrub(s) for s in (getattr(run, "log", None) or [])][-40:]
        if steps:
            lines += ["", "### What the desk did", *[f"{i}. {s}" for i, s in enumerate(steps, 1)]]
    for snap in _snapshots(Path(job["folder"])) if job.get("folder") else []:
        try:
            meta = json.loads((snap / "snapshot.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        try:
            convert(snap, snap.name, pages_dir, secrets=secrets)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # its fields are still listed
        lines += ["", f"### Page saved {snap.name}: {scrub(meta.get('title'))}",
                  scrub(meta.get("url") or "") + (f" ({scrub(meta.get('note'))})" if meta.get("note") else ""),
                  *map(scrub, _fields(meta))]
    text = "\n".join(lines) + f"\n\n_Personal details found in the profile are shown as {REDACTED}. " \
                              "No screenshots or saved pages are included._\n"
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    pages = sorted(str(p.relative_to(out_dir)) for p in pages_dir.iterdir() if p.is_file())
    zip_path = out_dir / "report.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(out_dir / "report.md", "report.md")
        for page in pages:
            z.write(out_dir / page, page)
    title = f"Report: {scrub(job.get('company'))}, {getattr(run, 'need', '') or getattr(run, 'status', '') or 'a problem'}"
    return {"folder": str(out_dir), "zip": str(zip_path), "preview": text, "issue_url": _issue_url(title, text),
            "pages": pages}
