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
employer by its job system ("a Workday employer"), not its name, and every address the same
way ("a Workday address"): a job's address shows its employer, its title and its requisition
number, a password reset's its one-time token.

A report made anonymous says the job that way too, and leaves out its title, its addresses, its
requisition number and its place, from the issue and from the saved pages; and the employer by any name a
page gives it besides the job's (its parent group's in a page title), as well as by its own.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from . import config
from .ats import ATS_NAMES, detect_ats, host_of, shared_system
from .autofill import US_STATES, is_empty_value
from .fixtures import _HUMAN_ATTRS, REDACTED, _pattern, _spellings, _without_query, convert, personal_strings, redact
from .history import HIDE_PREFIX
from .mailbox import site_domain

PAGES = 3  # the latest saved pages put in a report
ISSUE_BODY = 6000  # characters of the report in the issue's address at most...
ISSUE_URL = 7000  # ...and of the whole address, once encoded (GitHub refuses one much over 8 KB)
NOTES = 50  # notes kept at most: the oldest go first
NOTE_STEPS = 15  # the desk's last steps in a note
_SEEN = 1000  # the stops already noted that are remembered, so one isn't noted again after a Clear
# An address ends at a space, a quote or a bracket, but a pair of parentheses inside it stays in it
# ("/job/Phoenix-(AZ)/Test-Technician_R123?username=..." in a Workday address: issue #112, note 35)
_URL = re.compile(r"https?://(?:[^\s\"'<>()\]]|\([^\s()]*\))+")
# The last words of an employer's name that aren't its name ("Acme Semiconductor Group, Inc.")
_COMPANY_WORDS = {"inc", "corp", "corporation", "co", "group", "holdings", "llc", "ltd", "technologies",
                  "technology", "semiconductor", "semiconductors", "company", "services", "solutions"}
# The first words of employers' names that are everyday words too: a page uses them for other things
_COMMON_WORDS = {"american", "national", "global", "united", "general", "first", "advanced", "applied",
                 "international", "western", "northern", "southern", "eastern", "central", "pacific", "digital",
                 "state", "city", "county", "public", "home", "health", "university", "college", "early", "blue",
                 "capital", "bank", "texas", "arizona", "phoenix", "valley", "desert", "mountain", "river", "sun"}
# A job system's host labels that are the system's own, not an employer's tenant
_SYSTEM_LABELS = {"www", "jobs", "job-boards", "boards", "careers", "career", "external", "apply", "hire",
                  "recruiting", "talent", "careersite"}
# A name a page gives its employer besides the job's (its parent group's, which says it as well: "Job Details | Example
# Group", "Jobs at Example Group", issue #112, note 42): capitalised words, with "of", "and" or "&" between, six at most
_NAME = r"[A-Z][\w&'.-]*(?: (?:of|and|de|&|[A-Z0-9][\w&'.-]*)){0,5}"
# ... after "Jobs at", "Careers at", "Your Career at", "Life at", "Working at"
_AT_NAME = re.compile(r"(?i:\b(?:jobs?|careers?|opportunit(?:y|ies)|openings?|work(?:ing|ed)?|life|employment)) at "
                      rf"({_NAME})")
# ... in a page title, a part beside the job's or the site's own words ("the job Job Details | Example Group",
# "Careers | Example Group"), or before a careers site's words ("Example Group Careers")
_TITLE_PARTS = re.compile(r"\s+[|\u2013\u2014\u00b7-]\s+|\s*\|\s*")
_SITE_TAIL = re.compile(r"\s+(?:careers?|jobs|career (?:site|portal|center|centre|home)|careers (?:site|home|portal)|"
                        r"job (?:search|board|portal)|talent (?:community|network))$", re.I)
# A site's own words, never a name: its sections and steps, and the job systems'
_PAGE_WORDS = {
    "job", "jobs", "career", "careers", "site", "portal", "details", "detail", "description", "apply", "application",
    "applications", "home", "search", "results", "sign", "in", "up", "on", "login", "log", "account", "create",
    "candidate", "candidates", "profile", "my", "information", "info", "experience", "questions", "question", "review",
    "submit", "opportunities", "opportunity", "openings", "opening", "center", "centre", "page", "external",
    "internal", "talent", "community", "network", "welcome", "current", "employees", "employee", "employer",
    "employment", "position", "positions", "view", "all", "find", "work", "recruiting", "recruitment", "hiring", "now",
    "us", "join", "and", "of", "the", "for", "to", "your", "our", "a", "an", "at", "with", "step", "steps",
    "voluntary", "disclosures", "disclosure", "self", "identify", "identification", "personal", "contact", "resume",
    "cv", "upload", "documents", "document", "attachments", "thank", "thanks", "you", "submitted", "error", "not",
    "found", "loading", "eeo", "equal", "disability", "veteran", "status", "summary", "education", "skills",
    "references", "questionnaire", "assessment", "background", "consent", "privacy", "policy", "terms", "conditions",
    "notice", "agreement", "cookie", "cookies", "settings", "preferences", "location", "locations", "additional",
    "general", "screening", "cover", "letter", "new", "listing", "listings", "postings", "posting", "dashboard", "help",
    "faq", "benefits", "about", "inc", "llc", "workday", "sap", "successfactors", "icims", "oracle", "taleo", "cloud",
    "eightfold", "greenhouse", "lever", "smartrecruiters", "ashby", "avature", "brassring", "jobvite", "phenom",
    "applicantstack", "paycom", "ukg", "pro", "infor", "cornerstone", "linkedin", "indeed", "company", "redacted",
    # (and everyday words a page writes with a capital, in a heading or a title: "Employment at Will", "Engineering
    # Careers", "Remote Jobs", "Life at Work": the review's)
    "will", "remote", "hybrid", "engineering", "engineer", "engineers", "global", "worldwide", "international", "team",
    "teams", "people", "culture", "values", "mission", "life", "risk", "night", "nights", "day", "days", "heights",
    "height", "level", "levels", "every", "main", "office", "offices", "headquarters", "manufacturing", "operations",
    "technology", "technical", "technician", "technicians", "sales", "finance", "marketing", "hr", "it", "students",
    "student", "graduates", "graduate", "internships", "internship", "interns", "veterans", "military", "diversity",
    "inclusion", "perks", "news", "events", "story", "stories", "featured", "latest", "hot", "open", "browse",
    "explore", "discover", "future", "purpose", "impact", "safety", "quality", "acknowledgment", "acknowledgement",
    "hours", "time", "full", "part", "temporary", "contract", "seasonal", "entry", "senior", "staff", "manager",
    "management", "leadership", "executive", "support", "service", "services", "customer", "health", "healthcare",
    "nursing", "retail", "warehouse", "logistics", "driver", "drivers", "corporate", "field", "production",
    "maintenance", "research", "development", "science", "design", "data", "software", "hardware", "security", "legal",
    "admin", "administrative", "accounting", "north", "south", "east", "west", "america", "americas", "europe", "asia",
    "emea", "apac", "local", "regional", "national", "early", "professionals", "professional", "today",
    "acquisition", "apprenticeships", "apprenticeship", "benefit", "rewards", "total"}
# A job's place that names no place ("Remote", "Multiple Locations")
_ANYWHERE = {"remote", "hybrid", "on site", "onsite", "virtual", "multiple locations", "various locations",
             "united states", "united states of america", "usa", "us", "anywhere", "nationwide", "work from home"}
_STATES = {s.lower() for pair in US_STATES.items() for s in pair}
# What follows a place said as "the job's location" that was its state or country ("Springfield, IL")
_PLACE_TAIL = re.compile(r"(the job's location)((?:(?:,\s*|\s+)(?:" + "|".join(US_STATES)
                         + r")\b(?:\s+\d{5}(?:-\d{4})?)?"
                         r"|\s+\d{5}(?:-\d{4})?\b|,\s*(?:" + "|".join(sorted(US_STATES.values(), key=len, reverse=True))
                         + r"|United States(?: of America)?|USA|US)\b)+)")
# "the the employer", from "the Example Group" ("Privacy Policy of the the employer": issue #112, note 42)
_THE_THE = re.compile(r"\b([Tt]he) the (employer|job)\b")
# "the employer Group", once an employer's short form has been said ("Apply I Acme Group")
_EMPLOYER_TAIL = re.compile(r"(the employer)(?:[\s,]+(?:" + "|".join(sorted(_COMPANY_WORDS, key=len, reverse=True))
                            + r")\b\.?)+", re.I)
# Quoted text in a fill's error: the answer being filled, what the box showed instead ("Picked 'No'
# but the field shows 'Select One'"). Python quotes it with ' or ", the desk with curly quotes
_QUOTED = re.compile(r"\u201c[^\u201d]*\u201d|(?<!\w)'[^'\n]*'(?!\w)|(?<!\w)\"[^\"\n]*\"(?!\w)")
# The profile's answer named in the desk's own words ("(yours: “No”)", "your profile's answer “X”")
_ANSWER_SAID = re.compile(r"(yours: |your profile's answer )\u201c[^\u201d]*\u201d")
# What a long number may be written with between its digits (fixtures._pattern's): "602.555.0142", "(602) 555-0142"
_PHONE_APART = re.compile(r"[\s.\-()/]+")
# A number's characters: its digits, and what it may be written with between them and before it ("+1 (602) 555-0177")
_NUMBER_CHARS = re.compile(r"[\d\s.\-()/+]")
_URL_HOST = re.compile(r"https?://[^/?#]*", re.I)


def _repository() -> str:
    try:
        repo = json.loads((config.PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
                          .read_text(encoding="utf-8"))["repository"]
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
    # (and the cities of the places they'd like to work, which the desk puts in a form's "Preferred location")
    more += [_city(place) for place in config.listed(prof.get("preferences.locations"))]
    extra = {str(v).strip() for v in more if v and not isinstance(v, (dict, list)) and len(str(v).strip()) >= 3}
    return sorted(set(personal_strings(prof)) | extra, key=len, reverse=True)


def _city(place: Any) -> str:
    """A place's city: its first part that's neither a state nor a country nor no place at all ("Springfield" in
    "Springfield, IL", "AZ, Springfield", "US-IL-Springfield" as Workday writes one, "Springfield IL
    62701"), else ""."""
    if not isinstance(place, str):
        return ""
    place = re.sub(r"\([^)]*\)", " ", place)
    if m := re.fullmatch(r"\s*[A-Z]{2,3}\s*-\s*[A-Z]{2}\s*-\s*(.+?)\s*", place):  # (country-state-city)
        place = m.group(1)
    for part in place.split(","):
        part = re.sub(r"\s+\d{5}(?:-\d{4})?$", "", part.strip())  # (its ZIP code)
        if (words := part.split()) and len(words) > 1 and words[-1] in US_STATES:
            part = " ".join(words[:-1])  # ("Springfield IL")
        if part and part.lower() not in _ANYWHERE | _STATES and not part.isdigit():
            return part
    return ""


def _place_forms(location: Any) -> list[str]:
    """How a page may name a job's place ("Springfield, IL; Shelbyville, IL", "Springfield, IL / Shelbyville, IL"):
    each place as given, its city and state, and its city alone ("Remove Springfield, Illinois" on Eightfold's
    location chip: issue #112, note 42)."""
    forms: set[str] = set()
    for place in re.split(r"\s*[;|]\s*|\s+/\s+", str(location or "")):
        parts = [p.strip() for p in place.split(",") if p.strip()]
        forms |= {", ".join(parts), ", ".join(parts[:2]), _city(", ".join(parts))} if parts else set()
    return [f for f in forms if len(f) >= 3 and f.lower() not in _ANYWHERE | _STATES]


def _named(words: str) -> str:
    """Words that are a name ("Example Group"), not a site's own ("Job Details"), an everyday word ("Will") or a
    place's ("Arizona"), nor a stand-in: else ""."""
    words = re.sub(r"'s$|[.,:;!?]+$", "", words.strip())
    if len(words) < 3 or not re.fullmatch(_NAME, words) or REDACTED in words:
        return ""
    own = [w for w in re.findall(r"[a-z0-9&']+", words.lower()) if w not in _PAGE_WORDS]
    if len(words.split()) == 1 and words.lower() in _COMMON_WORDS | _COMPANY_WORDS | _STATES | _ANYWHERE:
        return ""
    return words if own else ""


def _names_given(titles: list[str], texts: list[str]) -> set[str]:
    """The names pages give an employer besides those the job has (_NAME), from text the job's own names are said in
    already: after "Jobs at" or "Careers at" anywhere; in a page title, a first or last part beside the job's or the
    site's own words, and one before a careers site's words."""
    found = {_named(m.group(1)) for text in [*texts, *titles] for m in _AT_NAME.finditer(text)}
    for title in titles:
        parts = [p.strip() for p in _TITLE_PARTS.split(title) if p.strip()]
        for i, part in enumerate(parts):
            if (bare := _SITE_TAIL.sub("", part)) != part:
                found.add(_named(bare))
            elif len(parts) > 1 and i in (0, len(parts) - 1) and all(
                    "the job" in other or "the employer" in other
                    or not [w for w in re.findall(r"[a-z0-9&']+", other.lower()) if w not in _PAGE_WORDS]
                    for other in parts[:i] + parts[i + 1:]):
                found.add(_named(part))
    return found - {""}


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


def _hiding(scrub: Callable[[Any], str], hide: Iterable[Any]) -> Callable[[Any], str]:
    """`scrub`, after taking out what's this run's alone (`hide`: pipeline's Applier._report_hidden: the answers the
    person gave for this application and what was typed into the form, an account form's answers, the codes the inbox
    gave, the desk's secrets), each as a whole word, of 3 characters or more, however a page may write it: a page's
    error the desk quotes ("review page shows errors: …") can echo what was typed. A number of 7 digits or more goes
    with any separators where it's mostly digits ("602-555-0177" as "(602) 555-0177" or "6025550177"; never "Worked
    2019-2023 at a fab" as "20192023"), and a long answer by its start too, as far as the text goes on with it (a page's
    error that cut it short: "I maintained 300mm etch tools across… exceeds 100 characters"). In an address, only as a
    whole word of its path or query (a code in a verify link, an email in a profile's path), never its host; or the
    whole address, when it's one of them (a reset link the inbox gave)."""
    values = {v for v in (" ".join(str(h or "").split()) for h in hide) if len(v) >= 3}
    hidden = {s for v in values for s in _spellings(v) if len(s) >= 3}
    hidden |= {digits for v in values if len(digits := re.sub(r"\D", "", v)) >= 7
               and len(_NUMBER_CHARS.findall(v)) >= 0.8 * len(v)}
    if not hidden:
        return scrub
    ordered = sorted(hidden, key=len, reverse=True)
    links = {_without_query(v) for v in values if _URL.fullmatch(v)}
    long = sorted((v for v in values if len(v) > HIDE_PREFIX), key=len, reverse=True)

    def words(said: str) -> str:
        said = _cut_echoes(said, long)
        # (only the values a text may hold, looked for as plain words first: a pattern for each of many values, every
        # line, took half a minute a report)
        low, digits = said.casefold(), _PHONE_APART.sub("", said)
        near = [s for s in ordered if (s[-10:] in digits if s.isdigit() and len(s) >= 7
                                       else all(w.rstrip(".,").casefold() in low for w in s.split()))]
        return redact(said, near) if near else said

    def address(url: str) -> str:
        if _without_query(url) in links:
            return REDACTED
        host = _URL_HOST.match(url)
        site, rest = (host.group(0), url[host.end():]) if host else ("", url)
        low = rest.casefold()
        for s in ordered:  # (as whole words alone: never "nightshift" in a path for "Nights")
            if all(w.rstrip(".,").casefold() in low for w in s.split()):
                rest = re.sub(_pattern(s), REDACTED, rest, flags=re.I)
        return site + rest

    def first(text: Any) -> str:
        said, out, at = str(text or ""), [], 0
        for m in _URL.finditer(said):
            out += [words(said[at:m.start()]), address(m.group(0))]
            at = m.end()
        return "".join([*out, words(said[at:])])
    return lambda text: scrub(first(text))


def _cut_echoes(text: str, values: list[str]) -> str:
    """`text` without each of `values` (a long answer) said from its start (its first HIDE_PREFIX characters, a whole
    word's start) as far as the text goes on with it, cut mid-word or not."""
    for value in values:
        start = re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, value[:HIDE_PREFIX].split(" "))), re.I)
        at = 0
        while m := start.search(text, at):
            i, k = m.end(), HIDE_PREFIX
            while i < len(text) and k < len(value):
                if value[k] == " " and text[i].isspace():
                    while i < len(text) and text[i].isspace():
                        i += 1
                elif text[i].lower() == value[k].lower():
                    i += 1
                else:
                    break
                k += 1
            text = text[:m.start()] + REDACTED + text[i:]
            at = m.start() + len(REDACTED)
    return text


def _scrubber(secrets: list[str]) -> Callable[[Any], str]:
    """Text with the person's details taken out: addresses lose their queries (session ids,
    tokens) before the redaction, and the profile's answers the desk names in its own words
    ("(yours: “Protected veteran”)" in a log line) go too: a report is filed in public."""
    def scrub(text: Any) -> str:
        said = _ANSWER_SAID.sub("\\1\u2026", str(text or ""))
        return redact(_URL.sub(lambda m: _without_query(m.group(0)), said), secrets)
    return scrub


def _a(name: str) -> str:
    return f"{'an' if name[:1] in 'AEIOaeio' else 'a'} {name}"


def _address_said(url: str) -> str:
    """An address in a report that doesn't say which job it was: its job system alone."""
    ats = detect_ats(url)
    return (f"({_a(ATS_NAMES.get(ats, ats))} address)" if ats and ats != "company_site"
            else "(an employer's own address)")


def _employer_forms(company: str, urls: list[Any]) -> list[str]:
    """How a page may say an employer, longest first: the name it was given, and (each at least 4
    letters) the name with its last words dropped one at a time ("Acme Semiconductor Group" ->
    "Acme Semiconductor", "Acme": a heading says "Acme Career", a title "Apply I Acme Group"), its
    first word unless an everyday one, and the tenant label of a job system's host
    (acme.wd5.myworkdayjobs.com -> "acme")."""
    forms: set[str] = set(re.findall(r"\(([A-Z][A-Za-z]{1,5})\)", company))  # "Tokyo Electron (TEL)"
    words = re.sub(r"\([^)]*\)", " ", company).split()
    for last in range(len(words) - 1, 0, -1):
        if words[last].strip(".,").lower() not in _COMPANY_WORDS:
            break
        forms.add(" ".join(words[:last]).rstrip(".,"))
    if words and words[0].strip(".,").lower() not in _COMMON_WORDS | _COMPANY_WORDS:
        forms.add(words[0].strip(".,"))
    for url in urls:
        tenant = host_of(str(url)).split(".")[0] if url and shared_system(str(url)) else ""
        if tenant and tenant not in _SYSTEM_LABELS and tenant not in _COMMON_WORDS:
            # (a one-word form is matched as written: _as_written)
            forms |= {tenant, tenant.upper(), tenant.capitalize()}
    # (a short form of 4 letters or more, or an all-capitals one of 3: "NXP", "ASM", "TEL")
    kept = {f for f in forms if (len(f) >= 4 or len(f) >= 3 and f.isupper()) and f.lower() not in _SYSTEM_LABELS}
    return sorted({*kept, *([company.strip()] if len(company.strip()) >= 2 else [])}, key=len, reverse=True)


def _as_written(form: str, company: str) -> bool:
    """A one-word short form of an employer's name is taken out only as it's written ("Early", "SCREEN"), not
    where a page uses the word ("How early can you start?"): the review of issue #112, note 35."""
    return " " not in form.strip() and form.strip().lower() != company.strip().lower()


# Never inside an email address: one at a domain an employer's word is, or a person's whose first name is one
# ("grant.lee@…" at Grant Thornton), is taken out whole after
_NOT_IN_EMAIL = r"(?<![\w@])(?:{})(?!\w)(?![\w.+-]*@)"
# What another employer than the job's is said as, in a report that doesn't say which job it was and in a note
ANOTHER = "another employer"
# An employer's short name a page says it by, where that's a brand of its own and never an everyday word or a person's
# name (the review of fix-ready-check, round 2: a name's first word by rule took "Salt", "Light", "Grant", "Switch",
# "Tokyo" out of a report's sentences): for a name that starts with the key. And the lists' aliases ("TEL", "AMAT")
_SHORT_NAMES = {"northrop grumman": "Northrop", "honeywell": "Honeywell", "wynn": "Wynn", "caesars": "Caesars",
                "charles schwab": "Schwab", "thermo fisher": "Thermo Fisher", "lam research": "Lam",
                "jpmorgan": "JPMorgan", "mgm resorts": "MGM", "hitachi": "Hitachi", "booz allen": "Booz Allen",
                "knight-swift": "Knight-Swift", "daifuku": "Daifuku", "nikon": "Nikon"}
_THE_ANOTHER = re.compile(r"\b(?:[Tt]he|[Aa]n?) another employer\b")  # ("the Intel site": "another employer site")


def _other_employers(job: dict[str, Any], run: Any, known: Iterable[Any] = ()) -> list[str]:
    """Every employer the desk knows but the job's own, longest first: the person's companies file's and the plugin's
    lists' (search.every_employer: each name and its short names) and the tracker's jobs' (`known`, from the caller).
    What a report that doesn't say which job it was, and a note, never name (the owner's note 51, Oct 20: an anonymous
    report's log named another employer, one waited on for the inbox meanwhile). Never one that's the job's own
    employer said another way ("Example Fab" for "Example Fab, Inc.", "Example" for "Example Group"), nor a name under
    three letters."""
    names: set[str] = {str(k or "").strip() for k in known}
    try:
        from .search import every_employer  # (search reads this module's neighbours: imported when wanted)

        for c in every_employer():
            aliases = c.get("aliases")
            names |= {str(n).strip() for n in [c.get("name"), *(aliases if isinstance(aliases, list) else [aliases])]
                      if isinstance(n, str)}
    except Exception:  # (a companies file written oddly: the tracker's names alone)
        pass
    own = [str(c or "") for c in (job.get("company"), getattr(run, "company", "")) if str(c or "").strip()]
    mine = {_norm_name(f) for c in own for f in [c, *_employer_forms(c, [])]}

    def ours(name: str) -> bool:
        said = _norm_name(name)
        return said in mine or any(said.startswith(f"{m} ") or m.startswith(f"{said} ") for m in mine if m)

    places = {_norm_name(f) for f in _place_forms(job.get("location"))}  # (never the job's place: "Maricopa County")

    def forms(name: str) -> set[str]:
        # the name, without a part in brackets ("Google (Henderson data center)": "Google"), a bracket's own name
        # where it's one ("Station Casinos (Red Rock Resorts)": "Red Rock Resorts", "Tokyo Electron (TEL)": "TEL"),
        # each less its last words that aren't its name ("Tamarisk Fab Inc."), and a short name of the curated few
        # (_SHORT_NAMES: "Northrop"). Never a first word by rule ("Salt" of "Salt River Project", "Banner" of "Banner
        # Health": the review of fix-ready-check, round 2)
        out: set[str] = set()
        inner = [b.strip() for b in re.findall(r"\(([^)]*)\)", name)
                 if b.strip() and all(w[:1].isupper() or not w[:1].isalpha() for w in b.split())]
        for whole in [name, " ".join(re.sub(r"\([^)]*\)", " ", name).split()), *inner]:
            words = whole.split()
            if not words:
                continue
            out.add(whole)
            low = _norm_name(whole)
            out |= {short for key, short in _SHORT_NAMES.items() if low == key or low.startswith(f"{key} ")}
            while len(words) > 1 and words[-1].strip(".,").lower() in _COMPANY_WORDS:
                words.pop()
                out.add(" ".join(words).rstrip(".,"))
        return {f for f in out if _norm_name(f) not in places}
    return sorted({f for n in names if not ours(n) for f in forms(n) if len(f) >= 3 and not ours(f)}, key=len,
                  reverse=True)


def _in_words(name: str, words: str) -> bool:
    """Is another employer's name one a saved page shows, as words of their own (`words`: _saved_words), and one that
    may be taken out of the page's whole HTML without breaking it: more than one word. A one-word name ("TEL",
    "Switch", "Intel") would be taken out of its attributes too (type="tel", role="switch"): the pages lose it from
    their words alone (_page_names, fixtures' `words`)."""
    return " " in name.strip() and bool(
        re.search(r"(?<!\w)" + r"\s+".join(map(re.escape, name.lower().split())) + r"(?!\w)", words.lower()))


def _as_others(name: str) -> list[str]:
    """How another employer's name is said: as written, and a one-word name in capitals too ("Intel", "INTEL"),
    never in another case ("intel" of a sentence, "tel" of type="tel": the review of fix-ready-check, round 2)."""
    name = name.strip()
    return [name, *([name.upper()] if " " not in name and name.upper() != name else [])]


def _page_names(others: Iterable[str], words: str) -> list[str]:
    """The one-word names of other employers (_as_others) a saved page shows as words of their own, case and all: what
    the pages lose from their words and their words for a person (fixtures' `words`), never from their markup."""
    return sorted({form for o in others if " " not in o.strip() for form in _as_others(o)
                   if re.search(r"(?<!\w)" + re.escape(form) + r"(?!\w)", words)}, key=len, reverse=True)


def _outside(keep: Iterable[str]) -> Callable[[re.Pattern[str], str, str], str]:
    """A re.sub that leaves alone what's inside any of `keep` where the text says it (the job's own title and place,
    and a heading that says them: "Light Industrial Work Station Technician", "Tokyo, Japan")."""
    spots = [re.compile(r"(?<!\w)" + r"\s+".join(map(re.escape, k.split())) + r"(?!\w)", re.I)
             for k in {k.strip() for k in keep} if len(k) >= 3]

    def sub(pattern: re.Pattern[str], repl: str, text: str) -> str:
        kept = [m.span() for spot in spots for m in spot.finditer(text)]
        return pattern.sub(lambda m: m.group(0) if any(a < m.end() and m.start() < b for a, b in kept) else repl, text)
    return sub


def _saved_words(snaps: Iterable[Path]) -> str:
    """The saved pages' words as a person sees them (each frame's too: _page_words, never their tags and attributes),
    their titles and their words for a person in attributes (fixtures' _HUMAN_ATTRS: alt, aria-label...): where another
    employer is looked for to take it out of them (one in none of them is never looked for: a page is megabytes). The
    review of fix-ready-check, round 3: a name in a page's <title> or an image's alt alone stayed in it."""
    import html as html_lib

    out = []
    for snap in snaps:
        for page in sorted(snap.glob("*.html")):
            try:
                raw = page.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            out.append(_page_words(raw))
            out += [" ".join(html_lib.unescape(_TAG.sub(" ", t)).split()) for t in _TITLE_TEXT.findall(raw)]
            out += [" ".join(html_lib.unescape(m.group(2) or m.group(3) or "").split())
                    for m in _HUMAN_VALUE.finditer(raw)]
    return "\n".join(out)


_TITLE_TEXT = re.compile(r"(?is)<title\b[^>]*>(.*?)</title\s*>")
_HUMAN_VALUE = re.compile(r"(?i)(?<![\w-])(" + "|".join(sorted(map(re.escape, _HUMAN_ATTRS), key=len, reverse=True))
                          + r")\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")


def _norm_name(name: str) -> str:
    """An employer's name as two spellings of it compare: lower case, words alone, its company words dropped from its
    end ("Example Fab, Inc." and "example fab" are one)."""
    words = re.sub(r"[^\w&]+", " ", name.lower()).split()
    while len(words) > 1 and words[-1] in _COMPANY_WORDS | {"the"}:
        words.pop()
    return " ".join(words)


def _anonymous(job: dict[str, Any], run: Any, urls: list[Any], secrets: list[str],
               pages: list[dict[str, Any]] | None = None, others: list[str] | None = None,
               page_words: str = "") -> tuple[Callable[[Any], str], list[str]]:
    """For a report that doesn't say which job it was: a scrubber for its text that also says every
    address by its job system alone, the employer as "the employer" and the job's title as "the
    job", and takes out its requisition number and its sites; and what its saved pages lose
    besides the person's details: those names, and the sites of the job's addresses (each host,
    and the employer's own domain beside it, never a job system's). The employer by its own domain's name
    too, and by any other name the run's page and the saved `pages` give it (_names_given: its parent group's,
    issue #112, note 42); and the job's place as "the job's location". And every other employer the desk knows
    (`others`: _other_employers) as "another employer", in the text and in the saved pages that name it
    (`page_words`: theirs)."""
    said: dict[str, str] = {}
    for value, stand_in in ((job.get("external_id"), REDACTED), (job.get("title"), "the job"),
                            (getattr(run, "title", ""), "the job"), (job.get("company"), "the employer"),
                            (getattr(run, "company", ""), "the employer")):
        if len(name := str(value or "").strip()) >= 2:
            said.setdefault(name, stand_in)
    exact = set()
    for company in (job.get("company"), getattr(run, "company", "")):
        for form in _employer_forms(str(company or ""), urls):
            said.setdefault(form, "the employer")
            if _as_written(form, str(company or "")):
                exact.add(form)
    sites = set()
    for url in urls:
        host = host_of(str(url or "")).removeprefix("www.")
        if host:
            own = shared_system(f"https://{host}/") is None and re.search(r"[a-z]", host)
            sites |= {host, site_domain(host)} if own else {host}
    info = getattr(run, "page_info", None) or {}
    for url in (job.get("url"), job.get("apply_url"), getattr(run, "url", ""), info.get("url")):
        # the employer's own domain's name, as a page may write it ("examplegroup", "EXAMPLEGROUP"): the job's own
        # addresses only, never a frame's (a CAPTCHA's, a video's)
        host = host_of(str(url or ""))
        label = site_domain(host).split(".")[0] if host and not shared_system(f"https://{host}/") else ""
        if (len(label) >= 4 and re.search(r"[a-z]", label)
                and label not in _SYSTEM_LABELS | _COMMON_WORDS | _COMPANY_WORDS):
            for form in {label, label.upper(), label.capitalize(),
                         *([label.replace("-", " ")] if "-" in label else [])}:
                said.setdefault(form, "the employer")
                exact |= {form} if " " not in form else set()
    # every other employer the desk knows, after the job's own (a job waited on meanwhile, a tab gone on to another's
    # site): as written where it's one word ("Intel", "SCREEN"), never its word in a sentence
    for other in others or []:
        for form in _as_others(other):
            if form not in said:
                said[form] = ANOTHER
                exact |= {form} if " " not in form.strip() else set()  # ("Switch" as written: never "the switch")
    outside = _outside([str(t or "") for t in (job.get("title"), getattr(run, "title", ""))]
                       + _place_forms(job.get("location")))

    def unnamed(text: Any) -> str:
        text = _URL.sub(lambda m: _address_said(m.group(0)), str(text or ""))
        low = text.lower()
        for name in sorted(said, key=len, reverse=True):
            if said[name] == ANOTHER and name.split()[0].lower() not in low:
                continue  # (most of the desk's employers aren't in a line at all: looked for only where they may be)
            if said[name] == ANOTHER:  # (never inside the job's own title or place: "Light Industrial Technician")
                text = outside(re.compile(_NOT_IN_EMAIL.format(_pattern(name)), 0 if name in exact else re.I), ANOTHER,
                               text)
            elif said[name] == "the employer":
                text = re.sub(_NOT_IN_EMAIL.format(_pattern(name)), said[name], text,
                              flags=0 if name in exact or len(name) < 3 else re.I)
            # (as written, and never a longer word's start: "IL" of "Illinois")
            elif said[name] == "the job's location":
                text = re.sub(_pattern(name) + r"(?![^\W\d_])", said[name], text)
            else:
                text = re.sub(_pattern(name), said[name], text, flags=re.I if len(name) >= 3 else 0)
        text = _PLACE_TAIL.sub(r"\1", text)
        return _THE_THE.sub(r"\1 \2", _EMPLOYER_TAIL.sub(r"\1", _THE_ANOTHER.sub(ANOTHER, text)))

    titles = [str(t) for t in [info.get("title"), *(p.get("title") for p in pages or [])] if t]
    texts = [str(t) for t in [*(info.get("headings") or []), *(info.get("actions") or []), getattr(run, "reason", ""),
                              *(getattr(run, "log", None) or [])] if t]
    found = set()
    for name in _names_given([unnamed(t) for t in titles], [unnamed(t) for t in texts]):
        words = name.split()  # (and the name less its last words that aren't its name: "Example Group" -> "Example")
        while words:
            form = " ".join(words)
            if (len(form) >= 4 or len(form) >= 3 and form.isupper()) and (len(words) > 1 or _named(form)) \
                    and form not in said:
                said[form] = "the employer"
                exact.add(form)  # (only as written: a name found on a page, never its words in a sentence)
                found.add(form)
            if len(words) < 2 or words[-1].strip(".,").lower() not in _COMPANY_WORDS:
                break
            words = words[:-1]
    for form in _place_forms(job.get("location")):
        said.setdefault(form, "the job's location")
    # (a saved page loses a place's city alone, wherever it's written: "Springfield, IL" would leave "linois" of
    # "Springfield, Illinois")
    kept = [name for name in [*said, *sites] if (said.get(name) != "the job's location" or "," not in name)
            and (said.get(name) != ANOTHER or _in_words(name, page_words))]  # (another only where it is)
    secrets = sorted({*secrets, *(s for name in kept for s in _spellings(name) if len(s) >= 2)}, key=len, reverse=True)
    # (the text says a name found on a page only as written, so its scrubber never takes it in another case; the saved
    # pages lose it in any)
    written = {s for name in found for s in _spellings(name)} - {s for name in kept if name not in found
                                                                 for s in _spellings(name)}
    scrub = _scrubber([s for s in secrets if s not in written])
    return (lambda text: scrub(unnamed(text))), secrets


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
          anonymous: bool = False, hide: Iterable[Any] = (), employers: Iterable[Any] = ()) -> dict[str, Any]:
    """Write the report for a job and return what it holds: its folder, report.zip (the text
    and the scrubbed pages, kept on this computer), the text the person sees before anything
    is filed (`preview`), and the address of a new GitHub issue with that text (`issue_url`).
    `anonymous`: the job said by its job system alone ("a Workday employer"), as a note says it,
    with no employer, job title, address or requisition number in the issue or the saved pages, nor any other
    employer the desk knows (_other_employers: its lists', the person's companies file's, and `employers`, the
    tracker's jobs'). `hide`: what else its text takes out, the run's own (_hiding): never from the job's title,
    employer or address. A run that isn't this job's is never its report's (it carries only its own job's log)."""
    if run is not None and getattr(run, "job_id", job.get("id")) != job.get("id"):
        run = None
    prof = profile or config.Profile.load()
    secrets = report_strings(prof)
    scrub = _scrubber(secrets)
    saved = []
    for snap in _snapshots(Path(job["folder"])) if job.get("folder") else []:
        try:
            saved.append((snap, json.loads((snap / "snapshot.json").read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    words: list[str] = []  # (what the saved pages lose from their words alone: another employer's one-word name)
    if anonymous:
        urls = [job.get("url"), job.get("apply_url"), getattr(run, "url", ""),
                (getattr(run, "page_info", None) or {}).get("url")]
        for _, meta in saved:
            urls += [meta.get("url"), *(f.get("url") for f in meta.get("frames") or [] if isinstance(f, dict))]
        others, page_words = _other_employers(job, run, employers), _saved_words(snap for snap, _ in saved)
        scrub, secrets = _anonymous(job, run, urls, secrets, [meta for _, meta in saved], others, page_words)
        words = _page_names(others, page_words)
    said = _hiding(scrub, hide)  # (the text's but the job's own lines: the saved pages stay on this computer)
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
        lines += [f"**Desk status:** {getattr(run, 'status', '')} {getattr(run, 'need', '')}".rstrip() + _code(run),
                  f"**What the desk said:** {said(getattr(run, 'reason', ''))}"]
        if last := last_action_said(getattr(run, "last_action", None), said):
            lines.append(f"**Last action:** {last}")
        steps = [said(s) for s in (getattr(run, "log", None) or [])][-40:]
        if steps:
            lines += ["", "### What the desk did", *[f"{i}. {s}" for i, s in enumerate(steps, 1)]]
        lines += map(said, _paused_page(getattr(run, "page_info", None) or {}, getattr(run, "questions", None) or []))
    for snap, meta in saved:
        try:
            convert(snap, snap.name, pages_dir, secrets=secrets, words=words)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # its fields are still listed
        lines += ["", f"### Page saved {snap.name}: {said(meta.get('title'))}",
                  said(meta.get("url") or "") + (f" ({said(meta.get('note'))})" if meta.get("note") else ""),
                  *map(said, _fields(meta))]
    left_out = ("The employer, the job's title, its addresses and its requisition number are left out, and so are the "
                "job's place and any other name a page gives the employer. ")
    text = "\n".join(lines) + f"\n\n_Personal details found in the profile are shown as {REDACTED}. " \
                              f"{left_out if anonymous else ''}No screenshots or saved pages are included._\n"
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    # named as in the zip ("pages/x.html"), on Windows too
    pages = sorted(p.relative_to(out_dir).as_posix() for p in pages_dir.iterdir() if p.is_file())
    zip_path = out_dir / "report.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(out_dir / "report.md", "report.md")
        for page in pages:
            z.write(out_dir / page, page)
    title = f"Report: {employer if anonymous else scrub(job.get('company'))}, " \
            f"{getattr(run, 'need', '') or getattr(run, 'status', '') or 'a problem'}"
    return {"folder": str(out_dir), "zip": str(zip_path), "preview": text, "issue_url": _issue_url(title, text),
            "pages": pages}


LAST_ACTIONS = ("pressed", "typed", "picked", "uploaded")
LAST_ACTION_SAID = 120  # characters of a last action's target a report or a stop's record says (after it's scrubbed)
STOP_TEXT = 300  # characters of the page's words around its first error, in a stop's record (stop.json)
STOP_WINDOW = 1500  # characters either side of the first error read (and scrubbed) to cut that from
STOP_RECORD = "stop.json"


def last_action_said(last: Any, said: Callable[[Any], str]) -> str:
    """The desk's last action on a job's pages (pipeline's Run.last_action) as a report says it: 'pressed “Next” at
    14:02:11', its target (a button's words, a box's label: never what was typed) through the report's scrubber
    `said`, then cut short (a value across the cut is taken out whole first). "" for none."""
    if not isinstance(last, dict) or last.get("what") not in LAST_ACTIONS or not str(last.get("target") or "").strip():
        return ""
    at = last.get("at")
    when = datetime.fromtimestamp(at).strftime(" at %H:%M:%S") if isinstance(at, (int, float)) else ""
    return f"{last['what']} “{said(str(last['target']))[:LAST_ACTION_SAID]}”{when}"


_UNSEEN = re.compile(r"(?is)<(script|style|noscript|template|head|svg)\b.*?</\1\s*>|<!--.*?-->")
_TAG = re.compile(r"(?s)<[^>]*>")


def _page_words(html_text: str) -> str:
    """A saved page's words, read quickly (a stop's page can be megabytes: a parser took seconds): its tags taken out,
    and what's never shown (scripts, styles, its head)."""
    import html as html_lib

    return " ".join(html_lib.unescape(_TAG.sub(" ", _UNSEEN.sub(" ", html_text))).split())


def stop_facts(run: Any) -> dict[str, Any]:
    """What a stop's record says of the run (its code, need, reason and last action), copied as the stop is saved."""
    last = getattr(run, "last_action", None)
    return {"code": getattr(run, "code", "") or "", "need": getattr(run, "need", "") or "",
            "reason": str(getattr(run, "reason", "") or ""),
            "last_action": dict(last) if isinstance(last, dict) else None}


def write_stop(folder: Path, facts: dict[str, Any], hide: Iterable[Any] = (),
               profile: config.Profile | None = None) -> Path | None:
    """A stop's record, stop.json, beside the page it stopped on (`folder`: the snapshot pipeline's Applier._save_stop
    saved; `facts`: stop_facts' of the run): its code, need and reason, the desk's last action (what it pressed, typed,
    picked or uploaded, by its label), the page's errors, and about STOP_TEXT characters of the page's words around
    the first of them (none where its words aren't found: never the page's first words instead), all scrubbed as a
    report's text is: the profile's details (report_strings) and what's the run's alone (`hide`,
    Applier._report_hidden: the answers given for it, what was typed into its form). Only the STOP_WINDOW characters
    either side of the error are scrubbed, never the whole page (megabytes: seconds), and the cut is made after. The
    last action goes into the snapshot's details too. A record only: None where there's no page saved, and nothing
    that goes wrong here is the stop's trouble. Run off the desk's event loop (pipeline: asyncio.to_thread)."""
    try:
        meta_path = folder / "snapshot.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        page = folder / "page.html"
        words = _page_words(page.read_text(encoding="utf-8")) if page.is_file() else ""
        said = _hiding(_scrubber(report_strings(profile or config.Profile.load())), hide)
        raw = [" ".join(str(e).split()) for e in meta.get("errors") or [] if str(e or "").strip()][:5]
        errors = [said(e)[:STOP_TEXT] for e in raw]
        text = ""
        for error, shown in zip(raw[:1], errors[:1]):  # (the first error, as written, else its first words)
            for probe in (error, " ".join(error.split()[:4])):
                if probe and (at := words.casefold().find(probe.casefold())) >= 0:
                    window = said(words[max(0, at - STOP_WINDOW):at + len(probe) + STOP_WINDOW])
                    # (where it is once scrubbed: as said, else its first words, else about where it was)
                    found = window.casefold().find(shown.casefold()) if shown else -1
                    if found < 0 and shown.split():
                        found = window.casefold().find(" ".join(shown.split()[:4]).casefold())
                    start = max(0, (found if found >= 0 else min(at, STOP_WINDOW)) - STOP_TEXT // 3)
                    text = window[start:start + STOP_TEXT]
                    break
        last = facts.get("last_action")
        last = {**last, "target": said(str(last.get("target") or ""))[:LAST_ACTION_SAID]} if isinstance(last, dict) \
            else None
        record = {"code": facts.get("code") or "", "need": facts.get("need") or "", "reason": said(facts.get("reason")),
                  "last_action": last, "errors": errors, "text": text}
        out = folder / STOP_RECORD
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        details = meta.get("details") if isinstance(meta.get("details"), dict) else {}
        meta["details"] = {**details, "last_action": last}
        meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
        return out
    except Exception:  # (a record: never the stop's trouble)
        return None


def _code(run: Any, bare: bool = False) -> str:
    """A stopped run's stop code (pipeline.STOP_CODES), as a report's status line ends with it: " (`step_not_saved`)";
    `bare`, the code alone. Nothing for a run that isn't stopped, or has none."""
    code = getattr(run, "code", "") if getattr(run, "status", "") == "needs_you" else ""
    if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,40}", code):
        return ""
    return code if bare else f" (`{code}`)"


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


def _note_scrubber(job: dict[str, Any], run: Any, prof: config.Profile,
                   hide: Iterable[Any] = (), employers: Iterable[Any] = ()) -> Callable[[Any], str]:
    """`_scrubber`, and also: every address said by its job system alone ("(a Workday address)":
    its path names the job and a password reset's holds its token), the employer applied to,
    by its name or a short form of it, called "the employer", and every other employer the desk knows
    (_other_employers, with `employers`: the tracker's jobs') "another employer"."""
    scrub = _hiding(_scrubber(report_strings(prof)), hide)
    urls = [job.get("url"), job.get("apply_url"), getattr(run, "url", ""),
            (getattr(run, "page_info", None) or {}).get("url")]
    forms = {(f, _as_written(f, str(n or ""))) for n in (job.get("company"), getattr(run, "company", ""))
             for f in _employer_forms(str(n or ""), urls)}
    exact = {f for f, as_written in forms if as_written}
    others = _other_employers(job, run, employers)

    def pattern(names: set[str], flags: int) -> re.Pattern[str] | None:
        spaced = [r"[\s.,]+".join(map(re.escape, f.split())) for f in sorted(names, key=len, reverse=True)]
        return re.compile(_NOT_IN_EMAIL.format("|".join(spaced)), flags) if spaced else None
    employer = [p for p in (pattern({f for f, _ in forms} - exact, re.I), pattern(exact, 0)) if p is not None]
    # (another employer by any of its names: in any case where it's more than one word, as written (and in capitals)
    # where it's one, "TEL", "Switch", never "the switch"; and never inside the job's own title or place)
    another = [p for p in (pattern({o for o in others if " " in o.strip()}, re.I),
                           pattern({f for o in others if " " not in o.strip() for f in _as_others(o)}, 0))
               if p is not None]
    outside = _outside([str(t or "") for t in (job.get("title"), getattr(run, "title", ""))]
                       + _place_forms(job.get("location")))

    def unnamed(text: str) -> str:
        text = _URL.sub(lambda m: _address_said(m.group(0)), text)
        for p in employer:
            text = p.sub("the employer", text)
        for p in another:
            text = outside(p, ANOTHER, text)
        return _THE_THE.sub(r"\1 \2", _EMPLOYER_TAIL.sub(r"\1", _THE_ANOTHER.sub(ANOTHER, text)))

    return lambda text: scrub(unnamed(str(text or "")))


def note(job: dict[str, Any], run: Any, profile: config.Profile | None = None, hide: Iterable[Any] = (),
         employers: Iterable[Any] = ()) -> str:
    """A note on one job's stop: what the desk said, its last steps, the page it stopped on and
    its questions, with why any answer didn't go in. Notes are filed together in one public
    issue, so on top of what `build` takes out, a note holds none of the person's answers, and
    says the employer and every address by its job system only, and no other employer the desk knows (`employers`:
    the tracker's jobs', _other_employers): the issue could otherwise list every employer they applied to. `hide`:
    what else it takes out, as `build` (never from the job's title)."""
    prof = profile or config.Profile.load()
    say = _note_scrubber(job, run, prof, hide, employers)
    ats, employer = _job_system(job, run)
    stop, title = getattr(run, "need", "") or getattr(run, "status", ""), job.get("title") or getattr(run, "title", "")
    lines = [
        f"## {stop}: {_note_scrubber(job, run, prof)(title)}, at {employer}",
        f"**Plugin version:** {config.plugin_version() or 'unknown'} \u00b7 **Job system:** {ats} \u00b7 "
        + (f"**Stop:** `{code}` \u00b7 " if (code := _code(run, bare=True)) else "")
        + f"**Noted:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"**What the desk said:** {say(getattr(run, 'reason', ''))}",
    ]
    steps = [say(s) for s in (getattr(run, "log", None) or [])][-NOTE_STEPS:]
    if steps:
        lines += ["", "### Its last steps", *[f"{i}. {s}" for i, s in enumerate(steps, 1)]]
    lines += map(say, _paused_page(getattr(run, "page_info", None) or {}, getattr(run, "questions", None) or [],
                                   errors=True))
    return "\n".join(lines) + "\n"


def take_note(job: dict[str, Any], run: Any, profile: config.Profile | None = None,
              hide: Iterable[Any] = (), employers: Iterable[Any] = ()) -> Path | None:
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
    path.write_text(note(job, run, profile, hide, employers), encoding="utf-8")
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
    end = (f"_Personal details found in the profile are shown as {REDACTED}. Employers and their addresses are "
           "said by their job system, not named. No answers, field values or screenshots are included._")
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
