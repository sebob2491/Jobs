"""A problem report for one job: what the desk did and the fields of the pages it stopped on,
with the person's details taken out, to file on the plugin's GitHub issues so it can be fixed.

Nothing leaves the computer here. The report is written to ~/.job-apply/reports/, its text
is shown to the person, and only they file it: the issue page opens with that text filled
in. The saved pages go in report.zip beside it, scrubbed, but they are never put in the
issue: a filled-in form still shows answers no scrubber can know are the person's (the
choices they picked, say). Screenshots are never included: a picture can't be scrubbed.

Notes are the same, made lighter for filing many at once: the desk takes one by itself when
a job stops on something it most likely got wrong (pipeline.NOTED), and the person files
them together in one issue from the desk's Notes. A note holds no answers, and says the
employer by its job system ("a Workday employer"), not its name: only the addresses of the
pages show which site it was.

A report made anonymous says the job that way too, and leaves out its title, its addresses and
its requisition number, from the issue and from the saved pages.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

from . import config
from .ats import ATS_NAMES, detect_ats, shared_system
from .autofill import is_empty_value
from .fixtures import REDACTED, _pattern, _spellings, _without_query, convert, personal_strings, redact
from .mailbox import site_domain

PAGES = 3  # the latest saved pages put in a report
ISSUE_BODY = 6000  # characters of the report in the issue's address at most...
ISSUE_URL = 7000  # ...and of the whole address, once encoded (GitHub refuses one much over 8 KB)
NOTES = 50  # notes kept at most: the oldest go first
NOTE_STEPS = 15  # the desk's last steps in a note
_SEEN = 1000  # the stops already noted that are remembered, so one isn't noted again after a Clear
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
# Quoted text in a fill's error: the answer being filled, what the box showed instead ("Picked 'No'
# but the field shows 'Select One'"). Python quotes it with ' or ", the desk with curly quotes
_QUOTED = re.compile(r"\u201c[^\u201d]*\u201d|(?<!\w)'[^'\n]*'(?!\w)|(?<!\w)\"[^\"\n]*\"(?!\w)")
# The profile's answer named in the desk's own words ("(yours: “No”)", "your profile's answer “X”")
_ANSWER_SAID = re.compile(r"(yours: |your profile's answer )\u201c[^\u201d]*\u201d")


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
    """The newest PAGES pages saved for a job, oldest first. The newest one a failed fill saved (or
    Claude, debug/<time>) is always among them: it's where the fill broke, and the pages of the job's
    stops since (debug/<time>-stop, pipeline.STOP) mustn't push it out."""
    debug = folder / "debug"
    found = [p for p in debug.iterdir() if p.is_dir() and (p / "snapshot.json").exists()] if debug.is_dir() else []
    newest, failed = sorted(found)[-PAGES:], max((p for p in found if not p.name.endswith("-stop")), default=None)
    return newest if failed is None or failed in newest else [failed, *newest[1:]]


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


def _scrubber(secrets: list[str]) -> Callable[[Any], str]:
    """Text with the person's details taken out: addresses lose their queries (session ids,
    tokens) before the redaction."""
    def scrub(text: Any) -> str:
        return redact(_URL.sub(lambda m: _without_query(m.group(0)), str(text or "")), secrets)
    return scrub


def _a(name: str) -> str:
    return f"{'an' if name[:1] in 'AEIOaeio' else 'a'} {name}"


def _address_said(url: str) -> str:
    """An address in a report that doesn't say which job it was: its job system alone."""
    ats = detect_ats(url)
    return f"({_a(ATS_NAMES.get(ats, ats))} address)" if ats and ats != "company_site" else "(an employer's own address)"


def _anonymous(job: dict[str, Any], run: Any, urls: list[Any],
               secrets: list[str]) -> tuple[Callable[[Any], str], list[str]]:
    """For a report that doesn't say which job it was: a scrubber for its text that also says every
    address by its job system alone, the employer as "the employer" and the job's title as "the
    job", and takes out its requisition number and its sites; and what its saved pages lose
    besides the person's details: those names, and the sites of the job's addresses (each host,
    and the employer's own domain beside it, never a job system's)."""
    said: dict[str, str] = {}
    for value, stand_in in ((job.get("external_id"), REDACTED), (job.get("title"), "the job"),
                            (getattr(run, "title", ""), "the job"), (job.get("company"), "the employer"),
                            (getattr(run, "company", ""), "the employer")):
        if len(name := str(value or "").strip()) >= 2:
            said.setdefault(name, stand_in)
    sites = set()
    for url in urls:
        host = (urlparse(str(url or "")).hostname or "").lower().removeprefix("www.")
        if host:
            own = shared_system(f"https://{host}/") is None and re.search(r"[a-z]", host)
            sites |= {host, site_domain(host)} if own else {host}
    secrets = sorted({*secrets, *(s for name in [*said, *sites] for s in _spellings(name) if len(s) >= 2)},
                     key=len, reverse=True)
    scrub, names = _scrubber(secrets), sorted(said, key=len, reverse=True)

    def say(text: Any) -> str:
        text = _URL.sub(lambda m: _address_said(m.group(0)), str(text or ""))
        for name in names:
            text = re.sub(_pattern(name), said[name], text, flags=re.I if len(name) >= 3 else 0)
        return scrub(text)
    return say, secrets


def _without_answers(error: Any) -> str:
    """What went wrong with a fill, without what was being filled: the error quotes the answer,
    what the box showed, and the list's choices after a colon."""
    text = re.sub(r":\s*\[.*$", "", str(error or "").strip().split("\n")[0])
    return _QUOTED.sub("\u2026", text)[:200]


def _where_in_page(f: dict[str, Any]) -> str:
    """The block and sub-box a field sits in ("Work Experience 2", "Month"), when it says."""
    where = " / ".join(str(f.get(k)) for k in ("section", "sublabel") if f.get(k))
    return f" [{where}]" if where else ""


def _paused_page(info: dict[str, Any], questions: list[dict[str, Any]], errors: bool = False) -> list[str]:
    """The page the job stopped on, as the desk read it (headings, buttons, boxes: never what's
    in them), and the questions it asked there with where each sits on the page. `errors`:
    with why a fill didn't go in, less the answer it quotes."""
    if not info and not questions:
        return []
    out = ["", "### The page it stopped on"]
    if info:
        out.append(f"{info.get('url') or ''} ({info.get('title') or ''})")
        if info.get("headings"):
            out.append("Headings: " + " | ".join(map(str, info["headings"])))
        if info.get("actions"):
            out.append("Buttons: " + " | ".join(map(str, info["actions"])))
        if info.get("errors"):
            out.append("Errors: " + " | ".join(map(str, info["errors"])))
        for f in info.get("fields") or []:
            if isinstance(f, dict) and f.get("label"):
                out.append(f"- {f['label']} ({f.get('kind', '?')}{', required' if f.get('required') else ''}, "
                           f"{'empty' if f.get('empty') else 'filled'}){_where_in_page(f)}")
    if questions:
        out += ["", "### Questions it asked"]
        for q in questions[:40]:
            if isinstance(q, dict):
                options = q.get("options") or []
                out.append(f"- {q.get('label') or '?'} ({q.get('kind', '?')}"
                           + (f", {len(options)} choices" if options else "") + f"){_where_in_page(q)}"
                           + (f": {_without_answers(q['error'])}" if errors and q.get("error") else ""))
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


def _issue_url(title: str, text: str, rest: str = "the whole report is in report.md") -> str:
    """The new-issue address with as much of the report as fits: the cap is on the encoded
    address, since encoding a quote or a line break makes it several characters. `rest`: where
    the whole text is, said where it's cut."""
    repo, limit = _repository(), ISSUE_BODY
    while True:
        body = text if len(text) <= limit else text[:limit] + f"\n\n(cut short: {rest})\n"
        url = f"{repo}/issues/new?title={quote(title)}&body={quote(body)}&labels=report"
        if len(url) <= ISSUE_URL or limit <= 200:
            return url
        limit = min(limit - 100, int(limit * ISSUE_URL / len(url)))


def build(job: dict[str, Any], run: Any = None, profile: config.Profile | None = None,
          anonymous: bool = False) -> dict[str, Any]:
    """Write the report for a job and return what it holds: its folder, report.zip (the text
    and the scrubbed pages, kept on this computer), the text the person sees before anything
    is filed (`preview`), and the address of a new GitHub issue with that text (`issue_url`).
    `anonymous`: the job said by its job system alone ("a Workday employer"), as a note says it,
    with no employer, job title, address or requisition number in the issue or the saved pages."""
    prof = profile or config.Profile.load()
    secrets = report_strings(prof)
    scrub = _scrubber(secrets)
    saved = []
    for snap in _snapshots(Path(job["folder"])) if job.get("folder") else []:
        try:
            saved.append((snap, json.loads((snap / "snapshot.json").read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    if anonymous:
        urls = [job.get("url"), job.get("apply_url"), getattr(run, "url", ""),
                (getattr(run, "page_info", None) or {}).get("url")]
        for _, meta in saved:
            urls += [meta.get("url"), *(f.get("url") for f in meta.get("frames") or [] if isinstance(f, dict))]
        scrub, secrets = _anonymous(job, run, urls, secrets)
    out_dir = _folder(int(job["id"]))
    pages_dir = out_dir / "pages"
    pages_dir.mkdir()

    employer = _job_system(job, run)[1]
    lines = [
        f"**Plugin version:** {config.plugin_version() or 'unknown'}",
        f"**Job:** {employer}" if anonymous else f"**Job:** {scrub(job.get('title'))} at {scrub(job.get('company'))}",
        f"**Job system:** {job.get('ats') or 'unknown'}",
        *([] if anonymous else [f"**Address:** {scrub(job.get('apply_url') or job.get('url') or '')}"]),
    ]
    if run is not None:
        lines += [f"**Desk status:** {getattr(run, 'status', '')} {getattr(run, 'need', '')}".rstrip(),
                  f"**What the desk said:** {scrub(getattr(run, 'reason', ''))}"]
        steps = [scrub(s) for s in (getattr(run, "log", None) or [])][-40:]
        if steps:
            lines += ["", "### What the desk did", *[f"{i}. {s}" for i, s in enumerate(steps, 1)]]
        lines += map(scrub, _paused_page(getattr(run, "page_info", None) or {}, getattr(run, "questions", None) or []))
    for snap, meta in saved:
        try:
            convert(snap, snap.name, pages_dir, secrets=secrets)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # its fields are still listed
        lines += ["", f"### Page saved {snap.name}: {scrub(meta.get('title'))}",
                  scrub(meta.get("url") or "") + (f" ({scrub(meta.get('note'))})" if meta.get("note") else ""),
                  *map(scrub, _fields(meta))]
    left_out = "The employer, the job's title, its addresses and its requisition number are left out. "
    text = "\n".join(lines) + f"\n\n_Personal details found in the profile are shown as {REDACTED}. " \
                              f"{left_out if anonymous else ''}No screenshots or saved pages are included._\n"
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    pages = sorted(str(p.relative_to(out_dir)) for p in pages_dir.iterdir() if p.is_file())
    zip_path = out_dir / "report.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(out_dir / "report.md", "report.md")
        for page in pages:
            z.write(out_dir / page, page)
    title = f"Report: {employer if anonymous else scrub(job.get('company'))}, " \
            f"{getattr(run, 'need', '') or getattr(run, 'status', '') or 'a problem'}"
    return {"folder": str(out_dir), "zip": str(zip_path), "preview": text, "issue_url": _issue_url(title, text),
            "pages": pages}


def notes_dir() -> Path:
    return config.home() / "notes"


def _note_files() -> list[Path]:
    folder = notes_dir()
    return sorted(folder.glob("*.md")) if folder.is_dir() else []  # oldest first: each name starts with its time


def _job_system(job: dict[str, Any], run: Any) -> tuple[str, str]:
    """The job system a job stopped on (the page's, else the posting's), and the employer said
    by it alone: "a Workday employer"."""
    found = [detect_ats(url) for url in (getattr(run, "url", ""), job.get("apply_url"), job.get("url")) if url]
    ats = next((a for a in [*found[:1], str(job.get("ats") or ""), *found[1:]] if a and a != "company_site"), "")
    if not ats:
        return "company_site", "an employer with its own careers site"
    return ats, f"{_a(ATS_NAMES.get(ats, ats))} employer"


def _note_scrubber(job: dict[str, Any], run: Any, prof: config.Profile) -> Callable[[Any], str]:
    """`_scrubber`, and also: the profile's answers the desk names in its own words taken out,
    and the employer applied to called "the employer" outside web addresses."""
    scrub = _scrubber(report_strings(prof))
    names = {str(n).strip() for n in (job.get("company"), getattr(run, "company", ""))
             if n and len(str(n).strip()) >= 2}
    spaced = [r"[\s.,]+".join(map(re.escape, n.split())) for n in sorted(names, key=len, reverse=True)]
    employer = re.compile(r"(?<!\w)(?:" + "|".join(spaced) + r")(?!\w)", re.I) if spaced else None

    def unnamed(text: str) -> str:
        if employer is None:
            return text
        out, at = [], 0
        for m in _URL.finditer(text):
            out += [employer.sub("the employer", text[at:m.start()]), m.group(0)]
            at = m.end()
        return "".join(out) + employer.sub("the employer", text[at:])

    return lambda text: scrub(unnamed(_ANSWER_SAID.sub("\\1\u2026", str(text or ""))))


def note(job: dict[str, Any], run: Any, profile: config.Profile | None = None) -> str:
    """A note on one job's stop: what the desk said, its last steps, the page it stopped on and
    its questions, with why any answer didn't go in. Notes are filed together in one public
    issue, so on top of what `build` takes out, a note holds none of the person's answers, and
    says the employer by its job system only: the issue could otherwise list every employer
    they applied to."""
    say = _note_scrubber(job, run, profile or config.Profile.load())
    ats, employer = _job_system(job, run)
    stop, title = getattr(run, "need", "") or getattr(run, "status", ""), job.get("title") or getattr(run, "title", "")
    lines = [
        f"## {stop}: {say(title)}, at {employer}",
        f"**Plugin version:** {config.plugin_version() or 'unknown'} \u00b7 **Job system:** {ats} \u00b7 "
        f"**Noted:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**What the desk said:** {say(getattr(run, 'reason', ''))}",
    ]
    steps = [say(s) for s in (getattr(run, "log", None) or [])][-NOTE_STEPS:]
    if steps:
        lines += ["", "### Its last steps", *[f"{i}. {s}" for i, s in enumerate(steps, 1)]]
    lines += map(say, _paused_page(getattr(run, "page_info", None) or {}, getattr(run, "questions", None) or [],
                                   errors=True))
    return "\n".join(lines) + "\n"


def take_note(job: dict[str, Any], run: Any, profile: config.Profile | None = None) -> Path | None:
    """Keep a note on a job's stop in ~/.job-apply/notes/, once for each job, stop and reason: a
    job that stops the same way again isn't noted twice, even after the notes are cleared. The
    newest NOTES are kept. Returns the note's file, or None when this stop was noted before."""
    folder = notes_dir()
    folder.mkdir(parents=True, exist_ok=True)
    which = f"{job['id']}\n{getattr(run, 'need', '')}\n{getattr(run, 'reason', '')}"
    key = hashlib.sha256(which.encode()).hexdigest()[:16]
    seen_file = folder / "seen.json"
    try:
        seen = json.loads(seen_file.read_text(encoding="utf-8"))
        seen = [k for k in seen if isinstance(k, str)] if isinstance(seen, list) else []
    except (OSError, ValueError):
        seen = []
    if key in seen:
        return None
    path = folder / f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{int(job['id']):04d}-{key}.md"
    path.write_text(note(job, run, profile), encoding="utf-8")
    seen_file.write_text(json.dumps([*seen, key][-_SEEN:]), encoding="utf-8")
    for old in _note_files()[:-NOTES]:
        old.unlink(missing_ok=True)
    return path


def notes_count() -> int:
    return len(_note_files())


def notes() -> dict[str, Any]:
    """The notes kept, oldest first, as one issue: its title, the whole text (for the desk's Copy
    all), the new-issue address with as much of it as fits, and whether that's all of it."""
    kept = []
    for path in _note_files():
        try:
            kept.append({"id": path.stem, "text": path.read_text(encoding="utf-8").strip()})
        except OSError:  # cleared meanwhile
            continue
    stops = f"{len(kept)} stop{'' if len(kept) == 1 else 's'}"
    title = f"Live-run notes: {stops} (job-apply {config.plugin_version() or 'unknown'})"
    end = (f"_Personal details found in the profile are shown as {REDACTED}. Employers are said by their job "
           "system, not named. No answers, field values or screenshots are included._")
    text = "\n\n".join([*(n["text"] for n in kept), end]) + "\n"
    url = _issue_url(title, text, rest="the rest is in the text the Job Desk's Copy all copies")
    return {"notes": kept, "title": title, "text": text, "issue_url": url, "cut": quote(text) not in url}


def clear_notes(ids: list[str]) -> int:
    """Remove these notes (the ones the person was shown): one taken since stays. Returns how many went."""
    gone = 0
    for path in _note_files():
        if path.stem in ids:
            path.unlink(missing_ok=True)
            gone += 1
    return gone
