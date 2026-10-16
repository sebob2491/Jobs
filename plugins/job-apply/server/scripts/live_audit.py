"""The live check's rules audit, over what a check's machine wrote. No network.

    uv run python scripts/live_audit.py --log pipeline=live-pipeline/run.log --folder live-pipeline

Reads the check's LIVE_PIPELINE records (as scripts/live_compare.py does) and the files in its folder, and
fails, one ::error:: line a finding, where the run broke a rule the product keeps (CLAUDE.md):
  - consents: a log line saying the desk agreed to, ticked, pressed, chose or accepted something that names
    a newsletter, job alerts, a talent community, marketing, texts or automated calls, arbitration, at-will
    employment or a waiver, which are never the desk's to give;
  - submitting: a round whose status is "submitted" (the live check never submits: JOB_APPLY_NEVER_SUBMIT);
  - secrets: the test identity's email and passwords (LIVE_TEST_EMAIL, LIVE_TEST_EMAIL_PASSWORD and
    LIVE_TEST_SITE_PASSWORD, read from the environment as the check's machine has them) in any file the
    check wrote, unmasked; an emailed code (six digits beside "code") or a security answer the test identity
    made (live_identity.security_answer's shape) in a log line, a reason or a file.
The nightly workflow runs it on each check's machine after the check; a finding fails that job.

It also reads each review page the check reached (pipeline/<employer>/page.html) against the profile the check
applied with (the record's "profile", live_smoke.py's profile_values), one ::warning:: line a value that isn't
the profile's (never a failure: the test identity's entries saved at a tenant by an earlier run show here until
they're corrected): a job or school from or to a date the profile doesn't give, a degree beside the profile's
school of a level the profile doesn't have (the "never overstate" rule), or another role's employer.

And it reads the search check's postings (its LIVE_RESULT records), one ::warning:: line a job system whose
postings were read without their title, their place or their text, or not read at all.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

from job_apply.autofill import degree_key

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_compare  # noqa: E402  (scripts/live_compare.py, beside this one)

SECRETS = ("LIVE_TEST_EMAIL", "LIVE_TEST_EMAIL_PASSWORD", "LIVE_TEST_SITE_PASSWORD")
MAX_FILE = 50 * 1024 * 1024
POSTING_TEXT = 200  # a posting's text this short or shorter isn't its whole (Posting.is_useful's)

# the desk's own log lines open with what it did; a consent it gave reads "agreed to …", "ticked …", "pressed …"
_GAVE = re.compile(r"^(?:agreed to|ticked|pressed|chose|accepted|picked)\b", re.I)
_NEVER = re.compile(r"\b(?:newsletters?|job alerts?|talent (?:community|network|pool)|marketing|text messages?|"
                    r"texts|sms|automated (?:calls?|messages?)|arbitration|at[- ]will|waiv(?:e|er|ers)|"
                    r"release of claims)\b", re.I)
_NOT = re.compile(r"\b(?:never|not|didn't|unticked|left|refused|declined|instead of)\b", re.I)
_CODE = re.compile(r"\bcode\b.{0,40}?\b\d{6}\b|\b\d{6}\b.{0,40}?\bcode\b", re.I | re.S)
_ANSWER = re.compile(r"\bjd[0-9a-f]{12}\b")  # live_identity.security_answer: "jd" + 12 hex digits

# The review page's values against the profile's (the record's "profile": live_smoke.py's profile_values).
# Workday's review page is a summary ("Work Experience 1 Job Title … From 03/2021 … Education 1 School or University
# … Degree Bachelor of Science … From 2016 To (Actual or Expected) 2020"); elsewhere it's the form itself.
_NEXT = (r"(?=\b(?:{}Certifications|Licen[cs]es|Languages|Resume|R\u00e9sum\u00e9|Websites|Skills|Social Network|"
         r"Application Questions|Voluntary|Self Identify|Review)\b|$)")
_AFTER_WORK, _AFTER_EDUCATION = _NEXT.format("Education|"), _NEXT.format("")
_DATE = r"(\d{1,2}/\d{1,2}/\d{4}|\d{1,2}/\d{4}|\d{4}-\d{2}(?:-\d{2})?|[A-Z][a-z]+\.? \d{4}|\d{4})"
_FROM = re.compile(r"\bFrom(?: \(Actual or Expected\))?:? " + _DATE + r"\b")
_TO = re.compile(r"\bTo(?: \(Actual or Expected\))?:? " + _DATE + r"\b")
_DEGREE = re.compile(r"\bDegree(?: Level| Type)?\b\s*[:*]*\s*(.{0,60})")
_STILL_HELD = re.compile(r"^\s*(?:present|current(?:ly)?|now|ongoing|to date)?\s*$", re.I)
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
           "nov": 11, "dec": 12}
SCHOOL_WINDOW = 300  # the chars after a school's name where its degree is read


def slug(name: str) -> str:  # (live_smoke.py's: an employer's folder in a check's)
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def _input_value(m: re.Match[str]) -> str:
    tag = m.group(0)
    if re.search(r"""\btype\s*=\s*["']?(?:hidden|password|submit|button|checkbox|radio|file)\b""", tag, re.I):
        return " "
    v = re.search(r"""\bvalue\s*=\s*(?:"([^"]*)"|'([^']*)')""", tag)
    return f" {v.group(1) if v.group(1) is not None else v.group(2) or ''} " if v else " "


def page_text(markup: str) -> str:
    """A saved page's words: its text, each box's value and each list's chosen entry; never its scripts and styles,
    a list's other entries, or a password's or hidden box's value."""
    t = re.sub(r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    t = re.sub(r"<option\b(?![^>]*\bselected\b)[^>]*>.*?</option>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<input\b[^>]*>", _input_value, t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


def month_year(value: Any) -> tuple[int | None, int | None]:
    """A date as the profile or a page writes it: "2021-03", "03/2021", "3/1/2021", "March 2021", "Mar. 2021" and
    2016 read (3, 2021) and (None, 2016); anything else (None, None)."""
    s = str(value or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", s)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = re.fullmatch(r"(\d{1,2})/(?:\d{1,2}/)?(\d{4})", s)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.fullmatch(r"([A-Za-z]{3,})\.? (\d{4})", s)
    if m:
        return _MONTHS.get(m.group(1)[:3].lower()), int(m.group(2))
    return (None, int(s)) if re.fullmatch(r"\d{4}", s) else (None, None)


def same_date(shown: Any, given: Any) -> bool:
    """The page's date is the profile's: the same year, and the same month where both give one."""
    sm, sy = month_year(shown)
    gm, gy = month_year(given)
    return sy is not None and sy == gy and (sm is None or gm is None or sm == gm)


def page_findings(where: str, text: str, profile: dict[str, Any]) -> Iterator[str]:
    """What the review page shows that isn't the profile's, one line each: a job or school from or to a date the
    profile doesn't give (Workday's "From 02/2021" for a job the profile starts 2021-03: the test identity's entry
    saved at Early Warning Services, KLA and Hitachi by an earlier run, run 105; a school "From 1900" at Western
    Alliance Bank), a degree beside the profile's school of a level the profile doesn't have, or another role's
    employer. A page with no "From"/"To" dates or no degree beside the school says nothing."""
    for other in profile.get("others") or []:
        if other and other in text:
            yield f"{where}: the review page names another role's employer, {other}"
    jobs = [e for e in profile.get("work") or [] if isinstance(e, dict)]
    work = re.search(r"\bWork Experience\b(.*?)" + _AFTER_WORK, text, re.S)
    if work and jobs:
        starts = [str(e["start"]) for e in jobs if e.get("start")]
        ends = [str(e["end"]) for e in jobs if e.get("end") and not _STILL_HELD.match(str(e["end"]))]
        for shown in _FROM.findall(work.group(1)):
            if starts and not any(same_date(shown, s) for s in starts):
                yield f"{where}: the review page shows a job from {shown}; the profile's start {', '.join(starts)}"
        for shown in _TO.findall(work.group(1)):
            if not any(same_date(shown, e) for e in ends):
                yield (f"{where}: the review page shows a job to {shown}; the profile's "
                       + (f"end {', '.join(ends)}" if ends else "are still held"))
    schools = [e for e in profile.get("education") or [] if isinstance(e, dict)]
    education = re.search(r"\bEducation\b(.*?)" + _AFTER_EDUCATION, text[work.end() if work else 0:], re.S)
    if education and schools:
        starts = [str(e["start"]) for e in schools if e.get("start")]
        ends = [str(e["end"]) for e in schools if e.get("end")]
        for shown in _FROM.findall(education.group(1)):
            if starts and not any(same_date(shown, s) for s in starts):
                yield f"{where}: the review page shows a school from {shown}; the profile's start {', '.join(starts)}"
        for shown in _TO.findall(education.group(1)):
            if ends and not any(same_date(shown, e) for e in ends):
                yield f"{where}: the review page shows a school to {shown}; the profile's end {', '.join(ends)}"
    levels = {str(lv) for lv in profile.get("levels") or []}
    levels |= {k for e in schools if e.get("degree") for k in [degree_key(str(e["degree"]))] if k}
    for school in sorted({str(e["school"]) for e in schools if e.get("school")}):
        for at in re.finditer(re.escape(school), text):
            label = _DEGREE.search(text[at.end():at.end() + SCHOOL_WINDOW])  # (the first Degree after it is its own)
            level = degree_key(label.group(1)) if label else None
            if label and level and level not in levels:
                yield (f"{where}: beside {school} the review page shows a {level.replace('_', ' ')} degree "
                       f"(\u201c{' '.join(label.group(1).split()[:3])}\u201d); the profile's levels: {', '.join(sorted(levels)) or 'none'}")


def review_pages(records: dict[str, dict[str, Any]], folder: Path) -> Iterator[tuple[str, dict[str, Any], Path]]:
    """Each employer whose last round reached the review page with its profile noted (live_smoke.py's
    profile_values), and the page its machine saved there (pipeline/<employer>/page.html, in a shard's folder too)."""
    for name, rec in records.items():
        if not isinstance(rec.get("profile"), dict) or (rec.get("rounds") or [{}])[-1].get("status") != "ready":
            continue
        page = next((p for p in folder.rglob(f"pipeline/{slug(name)}/page.html") if p.is_file()), None)
        if page is not None:
            yield name, rec, page


def values_findings(check: str, records: dict[str, dict[str, Any]], folder: Path) -> list[str]:
    """The review pages' values that aren't the profile's (page_findings), one line each."""
    out: list[str] = []
    for name, rec, page in review_pages(records, folder):
        text = page_text(page.read_text(encoding="utf-8", errors="replace"))
        out += page_findings(f"{check}/{name}", text, rec["profile"])
    return out


def consent_given(line: str) -> bool:
    """A log line where the desk gave a consent that is never its to give."""
    return bool(_GAVE.match(line.strip()) and _NEVER.search(line) and not _NOT.search(line))


def record_findings(check: str, rec: dict[str, Any]) -> Iterator[str]:
    """What one employer's record broke, each as one line."""
    name = str(rec.get("company") or "?")
    for i, rnd in enumerate(rec.get("rounds") or [], 1):
        if rnd.get("status") == "submitted":
            yield f"{check}/{name}: round {i} was submitted (the live check never submits)"
        texts = [str(line) for line in rnd.get("log") or []] + [str(rnd.get("reason") or "")]
        for text in texts:
            if consent_given(text):
                yield f"{check}/{name}: a consent the desk never gives: {text[:200]}"
            if _CODE.search(text):
                yield f"{check}/{name}: an emailed code in the log: {text[:200]}"
            if _ANSWER.search(text):
                yield f"{check}/{name}: a security answer in the log"


def log_findings(check: str, text: str) -> list[str]:
    return [f for rec in live_compare.read_log(text, "LIVE_PIPELINE ").values() for f in record_findings(check, rec)]


def posting_reads(text: str) -> tuple[dict[str, int], dict[str, list[str]]]:
    """The search check's postings (its LIVE_RESULT records) read, and those read without their title, their place
    or their text (a read that failed too), by job system: a parser that no longer reads a system's postings, or
    never did (run 116: SuccessFactors career sites' had no place, and every iCIMS posting's read failed)."""
    read: dict[str, int] = {}
    short: dict[str, list[str]] = {}
    for rec in live_compare.read_log(text, "LIVE_RESULT ").values():
        p = rec.get("posting")
        if not isinstance(p, dict):
            continue  # (its search found nothing to read)
        system = str(p.get("ats") or next(iter(rec.get("search_config") or {}), "") or "?")
        read[system] = read.get(system, 0) + 1
        missing = ([str(p["error"])[:120]] if p.get("error") else
                   [what for what, key in (("no title", "title"), ("no place", "location")) if not p.get(key)]
                   + ([f"{p.get('description_chars') or 0} chars of text"]
                      if (p.get("description_chars") or 0) <= POSTING_TEXT else []))
        if missing:
            short.setdefault(system, []).append(f"{rec.get('company') or '?'} ({', '.join(missing)})")
    return read, short


def posting_findings(text: str) -> list[str]:
    """posting_reads' short ones, one line a job system."""
    read, short = posting_reads(text)
    return [f"postings: {system}: {len(names)} of {read[system]} not read whole: {', '.join(names)}"
            for system, names in sorted(short.items())]


def _texts(folder: Path) -> Iterator[tuple[Path, str]]:
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        if path.stat().st_size > MAX_FILE:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:  # a screenshot, a zip
            continue
        yield path, data.decode("utf-8", errors="replace")


def folder_findings(folder: Path, secrets: dict[str, str]) -> list[str]:
    """Secrets (by name, never by value), codes and security answers in the files a check wrote."""
    found = []
    for path, text in _texts(folder):
        shown = path.as_posix()
        for name, value in secrets.items():
            if value and len(value) >= 6 and value in text:
                found.append(f"{shown}: {name}'s value, unmasked")
        if _ANSWER.search(text):
            found.append(f"{shown}: a security answer the test identity made")
        if path.name == "run.log":
            continue  # (its log lines are read as records, with the code rule; a page's six digits are its own)
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", action="append", default=[], metavar="CHECK=PATH", help="a check's run.log; repeatable")
    ap.add_argument("--folder", action="append", default=[], type=Path, help="a check's output folder; repeatable")
    args = ap.parse_args(argv)
    findings: list[str] = []
    read: dict[str, dict[str, Any]] = {}
    checks: list[str] = []
    postings: list[str] = []
    searched = short_reads = 0
    for spec in args.log:
        check, sep, path = spec.partition("=")
        if not sep:
            ap.error(f"--log {spec!r}: expected CHECK=PATH")
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"::warning::{check}: can't read {path} ({e})")
            continue
        read.update(live_compare.read_log(text, "LIVE_PIPELINE "))
        checks.append(check)
        findings += log_findings(check, text)
        postings += posting_findings(text)
        got, short = posting_reads(text)
        searched += sum(got.values())
        short_reads += sum(len(names) for names in short.values())
    secrets = {name: os.environ.get(name, "") for name in SECRETS}
    files = pages = 0
    values: list[str] = []
    for folder in args.folder:
        if not folder.is_dir():
            print(f"::warning::{folder.as_posix()}: no such folder")
            continue
        files += sum(1 for _ in _texts(folder))
        findings += folder_findings(folder, secrets)
        pages += sum(1 for _ in review_pages(read, folder))
        values += values_findings(checks[0] if len(checks) == 1 else "values", read, folder)
    for finding in findings:
        print(f"::error::{finding}")
    for finding in values + postings:
        print(f"::warning::{finding}")
    said = (f"values: {len(values)} not the profile's" if values else "values: as the profile's") + \
        f" ({pages} review page{'' if pages == 1 else 's'} read)"
    if searched:
        said += (f"; postings: {short_reads} of {searched} not read whole" if short_reads
                 else f"; postings: all {searched} read whole")
    if findings:
        print(f"audit: {len(findings)} finding{'' if len(findings) == 1 else 's'} ({len(read)} records, {files} files); {said}")
        return 1
    print(f"audit: nothing found ({len(read)} records, {files} files); {said}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
