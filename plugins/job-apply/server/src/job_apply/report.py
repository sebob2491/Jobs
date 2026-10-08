"""A problem report for one job: what the desk did and the pages it stopped on, with the
person's details taken out, to file on the plugin's GitHub issues so it can be fixed.

Nothing leaves the computer here. The report is written to ~/.job-apply/reports/, its text
is shown to the person, and only they file it: the issue page opens with that text filled
in, and they drag report.zip (the scrubbed pages) into it if they want. Screenshots are
never included: a picture can't be scrubbed reliably.
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
from .fixtures import REDACTED, _without_query, clean_html, personal_strings, redact

PAGES = 3  # the latest saved pages put in a report
ISSUE_BODY = 6000  # characters of the report in the issue's address (browsers and GitHub cap its length)
_SAFE_FILE = re.compile(r"^(page|frame-\d+)\.html$")


def _repository() -> str:
    try:
        repo = json.loads((config.PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["repository"]
    except (OSError, ValueError, KeyError, TypeError):
        repo = ""
    return str(repo).rstrip("/") or "https://github.com/sebob2491/Jobs"


def _snapshots(folder: Path) -> list[Path]:
    debug = folder / "debug"
    found = [p for p in debug.iterdir() if p.is_dir() and (p / "snapshot.json").exists()] if debug.is_dir() else []
    return sorted(found)[-PAGES:]


def _fields(meta: dict[str, Any]) -> list[str]:
    """Each field as its label, kind, whether required and whether filled: never its value."""
    out = []
    for f in meta.get("fields") or []:
        if not isinstance(f, dict) or not f.get("label"):
            continue
        filled = "filled" if f.get("value") not in (None, "", [], False) else "empty"
        out.append(f"- {f['label']} ({f.get('kind', '?')}{', required' if f.get('required') else ''}, {filled})")
    return out


def build(job: dict[str, Any], run: Any = None, profile: config.Profile | None = None) -> dict[str, Any]:
    """Write the report for a job and return what it holds: its folder, report.zip, the text
    the person sees before anything is filed (`preview`), and the address of a new GitHub
    issue with that text (`issue_url`)."""
    prof = profile or config.Profile.load()
    secrets = personal_strings(prof)
    scrub = lambda text: redact(str(text or ""), secrets)  # noqa: E731
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = config.home() / "reports" / f"{int(job['id']):04d}-{stamp}"
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        f"**Plugin version:** {config.plugin_version() or 'unknown'}",
        f"**Job:** {scrub(job.get('title'))} at {scrub(job.get('company'))}",
        f"**Job system:** {job.get('ats') or 'unknown'}",
        f"**Address:** {scrub(_without_query(job.get('apply_url') or job.get('url') or ''))}",
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
        lines += ["", f"### Page saved {snap.name}: {scrub(meta.get('title'))}",
                  f"{scrub(_without_query(meta.get('url') or ''))}"
                  + (f" ({scrub(meta.get('note'))})" if meta.get("note") else ""), *map(scrub, _fields(meta))]
        target = pages_dir / snap.name
        target.mkdir(exist_ok=True)
        for fr in meta.get("frames") or []:
            name = str(fr.get("file") or "")
            src = snap / name
            if _SAFE_FILE.match(name) and src.is_file():
                html = clean_html(src.read_text(encoding="utf-8", errors="replace"), secrets, None,
                                  str(fr.get("url") or meta.get("url") or ""))
                (target / name).write_text(html, encoding="utf-8")
    text = "\n".join(lines) + f"\n\n_Personal details found in the profile are shown as {REDACTED}. " \
                              "No screenshots are included._\n"
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    zip_path = out_dir / "report.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(out_dir / "report.md", "report.md")
        for page in sorted(pages_dir.rglob("*.html")):
            z.write(page, str(page.relative_to(out_dir)))
    title = f"Report: {scrub(job.get('company'))}, {getattr(run, 'need', '') or getattr(run, 'status', '') or 'a problem'}"
    body = text if len(text) <= ISSUE_BODY else text[:ISSUE_BODY] + "\n\n(cut short: the whole report is in report.zip)\n"
    body += "\n\n<!-- To add the saved pages, drag report.zip from the folder the Job Desk named into this box. -->\n"
    issue_url = f"{_repository()}/issues/new?title={quote(title)}&body={quote(body)}&labels=report"
    return {"folder": str(out_dir), "zip": str(zip_path), "preview": text, "issue_url": issue_url,
            "pages": sorted(str(p.relative_to(out_dir)) for p in pages_dir.rglob("*.html"))}
