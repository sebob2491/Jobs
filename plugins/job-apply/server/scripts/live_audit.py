"""The live check's rules audit, over what a check's machine wrote. No network.

    uv run python scripts/live_audit.py --log pipeline=live-pipeline/run.log --folder live-pipeline

Reads the check's LIVE_PIPELINE records (as scripts/live_compare.py does) and the files in its folder, and
fails, one ::error:: line a finding, where the run broke a rule the product keeps (CLAUDE.md):
  - consents: a log line saying the desk agreed to, ticked, pressed, chose or accepted something that names
    a newsletter, job alerts, a talent community, marketing, texts or automated calls, arbitration, at-will
    employment or a waiver, which are never the desk's to give;
  - agreements: each notice, terms gate, attestation or account box the desk agreed to (each round's "agreed" and
    the record's "agreed_after": pipeline.Run.agreed, scrubbed by live_smoke.py) read again for a legal waiver, an
    arbitration agreement or a sign-up in so many words (never._never_for_them, AGREED_KINDS): its heading, its
    choices, the first 4,000 characters of its words and, past them, the 300 characters either side of each such
    word, each apart and with the kinds the desk read it for (_texts_read);
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

It reads every box each employer's saved page holds (pipeline/<employer>/snapshot.json), one ::warning:: line a box
holding one of the applicant's own details (the phone, the email, the zip code, the street, the LinkedIn address or
the full name: live_profile.py's fake profile, never the artifact's) that the desk's own rules
(autofill.resolve_field) wouldn't put there: the profile's phone in an "Employee ID" box. And one ::warning:: line an
employer the check stopped short of its review page without saving the page it stopped on.

And it reads the search check's postings (its LIVE_RESULT records), one ::warning:: line a job system whose
postings were read without their title, their place or their text, or not read at all.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

from job_apply.autofill import degree_key, resolve_field
from job_apply.config import Profile
from job_apply.never import ANYWHERE, _never_for_them
from job_apply.pipeline import _user_name_box

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_compare  # (scripts/live_compare.py, beside this one)
import live_identity  # (scripts/live_identity.py: the test identity's name and its email's mask)
import live_profile  # (scripts/live_profile.py: the fake applicant's profile)

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
    if "ged" in levels:  # (degree_key reads a page's "GED" and "High School Diploma or GED" as high_school's)
        levels.add("high_school")
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
    yield from agreement_findings(check, rec)


# What the words of an agreement the desk made are read for again: what's never agreed to for the person wherever in a
# long notice it sits (never.ANYWHERE: a legal waiver, an arbitration agreement, a sign-up in so many words). Not every
# kind: an employer's privacy notice names its practices (its affiliates, its marketing, its cookies) and the other
# positions it may suggest, which the desk's paths read only where a notice says what it is (its heading, its
# opening), and read anywhere they'd fail every night's audit on a plain privacy notice. A terms gate's words are its
# whole page's (pipeline._terms_gate reads them for a waiver): a careers site's "Join our Talent Community" in its
# footer is no part of what the gate agrees to
#
# Each text of an agreement is read apart, never one run into the next (a sign-up's words are read up to 60 characters
# on), and with the kinds the desk read it for: a list's or a group's own label (an attestation's, "... accept our
# privacy notice, and tell us if you would like to receive marketing emails", whose privacy choice alone was picked)
# for a waiver alone, as the choices picked are read for the rest
AGREED_KINDS = ANYWHERE
_GATE_KINDS = frozenset({"arbitration", "waiver"})
_AGREED_SAYS = {"arbitration": "names arbitration", "waiver": "waives a right", "signs up": "signs the applicant up"}


def agreements(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """What the desk agreed to for the applicant at one employer, in the words its rules read (each round's "agreed",
    and "agreed_after": what it agreed to after the last round was recorded; live_smoke.py's agreed_words of
    pipeline.Run.agreed). Of each: its heading, button, choices, the first 4,000 characters of its words and, past
    them, the 300 characters either side of each of a waiver's or a sign-up's words ("windows")."""
    return [a for rnd in [*(rec.get("rounds") or []), {"agreed": rec.get("agreed_after")}] if isinstance(rnd, dict)
            for a in rnd.get("agreed") or [] if isinstance(a, dict)]


def _texts_read(a: dict[str, Any]) -> list[tuple[str, frozenset[str]]]:
    """An agreement's texts, each with the kinds it's read for (AGREED_KINDS, or a waiver alone)."""
    what, heading, button = a.get("what"), str(a.get("heading") or ""), str(a.get("button") or "")
    words = [str(a.get("words") or ""), *(str(w) for w in a.get("windows") or [])]
    choices = [str(c) for c in a.get("choices") or []]
    if what == "terms gate":  # (its words the whole page's: a footer's talent community is none of the gate's)
        return [(t, _GATE_KINDS) for t in (heading, button, *words)]
    if what == "attestation":  # (a list's or a group's label for a waiver; a box's own label, and each choice, whole)
        return [(heading, _GATE_KINDS if choices else AGREED_KINDS), *((c, AGREED_KINDS) for c in choices)]
    if what == "account box":  # (its label, the choice given and the words above it)
        return [(t, AGREED_KINDS) for t in (heading, *choices, *words)]
    return [(t, AGREED_KINDS) for t in (heading, button, *choices, *words)]  # (a notice)


def agreement_findings(check: str, rec: dict[str, Any]) -> Iterator[str]:
    """Each agreement whose texts hold what's never agreed to for the person (_texts_read), one line each."""
    name = str(rec.get("company") or "?")
    for a in agreements(rec):
        kind = next((k for text, kinds in _texts_read(a) if text and (k := _never_for_them(text, only=kinds))), None)
        if kind:
            said = " ".join(str(a.get("heading") or a.get("button") or "a notice").split())
            said = said if len(said) <= 80 else said[:79] + "…"
            yield f'{check}/{name}: agreed to "{said}" which {_AGREED_SAYS.get(kind, kind)}'


# The applicant's own details a box may hold (live_profile.FAKE_PROFILE's, and the test identity's name and its email's
# mask, as a run writes them: never read from the artifact), each told by its shape (personal_value)
_PERSONAL = live_profile.FAKE_PROFILE["personal"]
_PHONE_DIGITS = re.sub(r"\D", "", _PERSONAL["phone"])
_FULL_NAMES = {f"{_PERSONAL['first_name']} {_PERSONAL['last_name']}".casefold(),
               f"{live_identity.FIRST_NAME} {live_identity.LAST_NAME}".casefold()}
_STREET = str(_PERSONAL["address"]["line1"]).casefold()
_ZIP = re.compile(re.escape(str(_PERSONAL["address"]["postal_code"])) + r"(?:-\d{4})?")
_LINKEDIN = re.sub(r"^https?://(?:www\.)?", "", str(_PERSONAL["linkedin_url"])).casefold()
_NOT_A_BOX = {"password", "file", "checkbox", "radio", "checkbox_group", "radio_group"}
# A label that asks for the detail a box holds, though resolve_field gives it no rule ("Home Phone", "Contact Number",
# "Sign-in Email", "City, State or Zip", "Applicant Name", "Sign", "Website", "Social Media Profile"): no finding; but
# one whose words are about someone else, or an employee's number, is (a reference's name, "Employee ID Number")
_NAMES_IT = {what: re.compile(words, re.I) for what, words in {
    "phone": r"phone|mobile|\bcell|\btel\b|telephone|contact (?:number|no)",
    "email": r"e-?mail|user ?(?:name|id)|log ?in|sign[- ]?in",
    "zip code": r"\bzip|postal|post ?code|location|\bcity\b",
    "street": r"address|street",
    "LinkedIn address": r"linked ?in|website|\burl\b|social|profile|portfolio|\blinks?\b",
    "full name": r"\bname\b|\bsign",
}.items()}
_SOMEONE_ELSES = re.compile(r"\breferen|emergency|manager|supervisor|\breferr|relative|spouse|contact person|recruiter|"
                            r"employee", re.I)


def personal_value(value: Any) -> str | None:
    """Which of the applicant's own details a box's value is: "phone", "email", "zip code", "street", "LinkedIn
    address" or "full name"; None for anything else."""
    s = " ".join(str(value or "").split())
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if re.fullmatch(r"[\d\s()+.-]+", s) and digits in (_PHONE_DIGITS, "1" + _PHONE_DIGITS):
        return "phone"
    if live_profile.FAKE_EMAIL_SHAPE.fullmatch(s) or s == live_identity.EMAIL_MASK:
        return "email"
    if _ZIP.fullmatch(s):
        return "zip code"
    if s.casefold().rstrip(".") == _STREET:
        return "street"
    if _LINKEDIN in s.casefold():
        return "LinkedIn address"
    if s.casefold() in _FULL_NAMES:
        return "full name"
    return None


def box_findings(where: str, fields: list[Any], profile: Profile) -> Iterator[str]:
    """Each box holding one of the applicant's own details (personal_value) where the desk's own rules
    (autofill.resolve_field, by the box's label and kind) wouldn't put that detail: the profile's phone in an
    "Employee ID" box, its name in a reference's. One line each."""
    for f in fields:
        if not isinstance(f, dict) or f.get("kind") in _NOT_A_BOX or not isinstance(f.get("value"), str):
            continue
        what = personal_value(f["value"])
        if what is None:
            continue
        label = " ".join(str(f.get("label") or "").split())
        # (by its label and kind, and what else the form reader saw of it: its section, its autocomplete, as the desk
        # read it: Schwab's "Number" box is a phone's by its autocomplete, tel-national; never its value)
        seen = {k: v for k, v in f.items() if k not in ("id", "value")}
        try:
            answer = resolve_field({**seen, "label": label, "kind": f.get("kind") or "text"}, profile)
        except Exception:  # (a rule that can't read a box so bare: not the desk's answer either)
            answer = None
        if what == "email" and _user_name_box({"label": label}):
            continue  # (an account's user name: the desk puts the email in it, pipeline._sign_in)
        if answer is not None and personal_value(answer.value) == what:
            continue
        if _NAMES_IT[what].search(label) and not _SOMEONE_ELSES.search(label):
            continue  # (a box whose label asks for that detail, as a site may: "Home Phone", "Sign-in Email")
        yield f'{where}: box "{label[:80]}" holds the profile\'s {what}'


def _saved_pages(folders: list[Path], name: str) -> dict[str, Path]:
    """Each employer's saved file `name` (pipeline/<employer>/<name>, in a shard's folder too), by its folder's name."""
    return {p.parent.name: p for folder in folders for p in folder.rglob(name)
            if p.is_file() and p.parent.parent.name == "pipeline"}


def boxes_findings(check: str, records: dict[str, dict[str, Any]], folders: list[Path]) -> list[str]:
    """box_findings over every employer's saved page (its snapshot.json's boxes), against the fake profile."""
    snapshots = _saved_pages(folders, "snapshot.json")
    profile = Profile(copy.deepcopy(live_profile.FAKE_PROFILE))
    out: list[str] = []
    for name in records:
        path = snapshots.get(slug(name))
        if path is None:
            continue
        try:
            fields = json.loads(path.read_text(encoding="utf-8")).get("fields") or []
        except (OSError, ValueError, AttributeError):
            continue
        out += box_findings(f"{check}/{name}", fields, profile)
    return out


def stop_pages(check: str, records: dict[str, dict[str, Any]], folders: list[Path]) -> tuple[int, int, list[str]]:
    """The employers the check stopped short of their review page (their last round not "ready"), how many of them had
    the page they stopped on saved (pipeline/<employer>/page.html), and a line for each that hadn't, or whose saving
    or fixture said an error all the same (live_smoke.py's snapshot_error, fixture_error): (saved, stops, lines)."""
    pages = _saved_pages(folders, "page.html")
    saved, stops, lines = 0, 0, []
    for name, rec in records.items():
        rounds = [r for r in rec.get("rounds") or [] if isinstance(r, dict)]
        if not rounds or rounds[-1].get("status") == "ready":
            continue
        stops += 1
        error = str(rec.get("snapshot_error") or "")[:120]
        if slug(name) not in pages:
            lines.append(f"{check}/{name}: the page it stopped on wasn't saved" + (f" ({error})" if error else ""))
            continue
        saved += 1
        if error:
            lines.append(f"{check}/{name}: the page it stopped on was saved, but saving it failed after ({error})")
        elif rec.get("fixture_error"):
            lines.append(f"{check}/{name}: the page it stopped on was saved, but its fixture failed "
                         f"({str(rec['fixture_error'])[:120]})")
    return saved, stops, lines


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


def posting_facts(text: str) -> dict[str, dict[str, int]]:
    """By job system, the search check's postings read (`read`), those whose pay was found (`pay`: pay.py, from the
    posting's data or its words), those whose date wasn't (`no_date`: neither the list nor the posting gave one) and how
    many records said either way (`dated`: a run before the live check recorded dates has none): how far the desk's pay
    listing and its Posted filter reach (the design of Oct 18, F1 and F2)."""
    out: dict[str, dict[str, int]] = {}
    for rec in live_compare.read_log(text, "LIVE_RESULT ").values():
        p = rec.get("posting")
        if not isinstance(p, dict) or p.get("error"):
            continue
        system = str(p.get("ats") or next(iter(rec.get("search_config") or {}), "") or "?")
        facts = out.setdefault(system, {"read": 0, "pay": 0, "dated": 0, "no_date": 0})
        facts["read"] += 1
        facts["pay"] += bool(p.get("pay"))
        if "posted" in p:
            facts["dated"] += 1
            facts["no_date"] += not p.get("posted")
    return out


def facts_said(facts: dict[str, dict[str, int]]) -> str:
    """posting_facts in the summary line: "pay listed on 3 of 9 (paycom 1 of 1, workday 2 of 8); no date on 2 of 9
    (applicantstack 2 of 2)"."""
    if not facts:
        return ""
    read = sum(f["read"] for f in facts.values())
    said = f"pay listed on {sum(f['pay'] for f in facts.values())} of {read} (" + ", ".join(
        f"{system} {f['pay']} of {f['read']}" for system, f in sorted(facts.items())) + ")"
    dated = {system: f for system, f in facts.items() if f["dated"]}
    if dated:
        undated = {system: f for system, f in dated.items() if f["no_date"]}
        said += f"; no date on {sum(f['no_date'] for f in dated.values())} of {sum(f['dated'] for f in dated.values())}"
        if undated:
            said += " (" + ", ".join(f"{system} {f['no_date']} of {f['dated']}"
                                     for system, f in sorted(undated.items())) + ")"
    return said


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
    facts: dict[str, dict[str, int]] = {}
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
        for system, f in posting_facts(text).items():
            mine = facts.setdefault(system, dict.fromkeys(f, 0))
            for k, n in f.items():
                mine[k] += n
    secrets = {name: os.environ.get(name, "") for name in SECRETS}
    files = pages = 0
    values: list[str] = []
    folders: list[Path] = []
    for folder in args.folder:
        if not folder.is_dir():
            print(f"::warning::{folder.as_posix()}: no such folder")
            continue
        folders.append(folder)
        files += sum(1 for _ in _texts(folder))
        findings += folder_findings(folder, secrets)
        pages += sum(1 for _ in review_pages(read, folder))
        values += values_findings(checks[0] if len(checks) == 1 else "values", read, folder)
    boxes: list[str] = []
    saved_stops = stops = 0
    if folders:
        boxes = boxes_findings(checks[0] if len(checks) == 1 else "boxes", read, folders)
        saved_stops, stops, unsaved = stop_pages(checks[0] if len(checks) == 1 else "pages", read, folders)
        boxes += unsaved
    for finding in findings:
        print(f"::error::{finding}")
    for finding in values + boxes + postings:
        print(f"::warning::{finding}")
    said = (f"values: {len(values)} not the profile's" if values else "values: as the profile's") + \
        f" ({pages} review page{'' if pages == 1 else 's'} read)"
    said += f"; agreements: {sum(len(agreements(rec)) for rec in read.values())} re-read"
    if folders:
        said += f"; pages: {saved_stops} of {stops} stops saved"
    if searched:
        said += (f"; postings: {short_reads} of {searched} not read whole" if short_reads
                 else f"; postings: all {searched} read whole")
    if facts:
        said += f"; {facts_said(facts)}"
    if findings:
        print(f"audit: {len(findings)} finding{'' if len(findings) == 1 else 's'} ({len(read)} records, {files} files); {said}")
        return 1
    print(f"audit: nothing found ({len(read)} records, {files} files); {said}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
