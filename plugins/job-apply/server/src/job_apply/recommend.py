"""Which openings to apply to first.

Listings from the employer search are scored against the applicant's profile: the
titles they target, their experience against the role's level, how recent the
posting is and, when the posting text can be read, hard requirements such as a
degree, years of experience, a clearance or U.S.-person status. The score only
orders the list; every point comes with a plain-language reason or concern.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from .autofill import degree_key, norm
from .config import Profile
from .postings import QUESTIONS_HEADING, Posting, fetch_posting
from .search import MAX_ALTERNATIVES, US_STATES, location_matches, location_terms, title_matches, title_words

# Semiconductor equipment roles, used when the profile names no target titles.
DEFAULT_TITLES = ["field service", "customer service engineer", "customer engineer", "equipment technician"]
RECOMMEND_AT = 60  # scores from here up are preselected in the Job Desk

_LEVEL = {"high_school": 1, "associate": 2, "bachelor": 3, "master": 4, "doctorate": 5}
_DEGREE_NAME = {"high_school": "a high school diploma", "associate": "an Associate's", "bachelor": "a Bachelor's",
                "master": "a Master's", "doctorate": "a doctorate"}
_SENIOR = re.compile(r"\b(senior|sr|staff|principal|lead|manager|director|head|vp|vice president|chief|supervisor)\b")
_JUNIOR = re.compile(r"\b(early career|entry level|entry|junior|jr|graduate|new grad|apprentice|trainee)\b")
_INTERN = re.compile(r"\b(intern|internship|co op|coop)\b")
# Words in many titles that say nothing of the work: "HR Coordinator" is no reason to suggest an
# "Enrollment Coordinator" or a "Clinical Research Coordinator"
_GENERIC_TITLE_WORDS = {"coordinator", "specialist", "manager", "analyst", "associate", "assistant", "representative",
                        "consultant", "director", "senior", "lead", "officer", "administrator", "clerk", "supervisor",
                        "partner", "principal", "junior", "intern", "staff", "team", "level"}
# Metro areas the employer lists cover: a place in the same one as a person's named places is
# near them (Mesa for Tempe), one outside it isn't (Bagdad's or Morenci's mines for Phoenix).
_METROS = [
    {"phoenix", "chandler", "tempe", "mesa", "scottsdale", "gilbert", "glendale", "peoria", "goodyear",
     "surprise", "avondale", "queen creek", "maricopa", "buckeye", "tolleson", "laveen", "cave creek",
     "fountain hills", "el mirage", "litchfield park", "sun city", "paradise valley", "apache junction",
     "san tan valley", "anthem", "casa grande", "ahwatukee", "sun city west", "sun lakes", "youngtown",
     "waddell", "new river", "gold canyon"},
    {"tucson", "oro valley", "marana", "sahuarita", "vail"},
]
_CITY_STATE = re.compile(r"^\s*([a-z][a-z .'-]*?)\s*[,-]\s*([a-z]{2})\b")
_COUNTRY_STATE_CITY = re.compile(r"^\s*(?:us|usa)\s*-\s*([a-z]{2})\s*-\s*([a-z][a-z .'-]*?)\s*$")  # iCIMS's US-AZ-Bagdad
_STATE_CODES = {code.lower() for code in US_STATES}


def _far_place(location: str, cities: list[str]) -> str | None:
    """The place a listing names when every place it names is a "City, ST" away from the metro
    area of the person's own places ("Bagdad, AZ" for Phoenix); None when any is near, remote,
    or not a city ("Arizona", "2 Locations", "+80 more"), or the person's places aren't in an
    area this knows."""
    near = next((m for m in _METROS if any(c in m for c in cities)), None)
    where = location.lower()
    if near is None or not where.strip() or re.search(r"remote|\bmore\b", where):
        return None
    if any(re.search(rf"\b{re.escape(c)}\b", where) for c in near | set(cities)):
        return None
    places = []
    for part in where.split(";"):
        if m := _COUNTRY_STATE_CITY.match(part):
            state, town = m.group(1), m.group(2)
        elif (m := _CITY_STATE.match(part)) and m.group(1).strip() not in ("us", "usa"):
            town, state = m.group(1), m.group(2)
        else:
            return None
        if state not in _STATE_CODES:
            return None
        places.append(f"{town.strip().title()}, {state.upper()}")
    return places[0] if places else None


# Words whose first letters don't say what the work is: an Account Specialist or Key Accounts
# Manager looks after customers, an Accountant (or Accounts Payable) keeps the books.
_SAME_WORK = {"accountant": "acctg", "accountants": "acctg", "accounting": "acctg", "accountancy": "acctg",
              "account": "account", "accounts": "account"}
_PAYABLES = re.compile(r"\baccounts? (payable|receivable)\b")


def _near_target(title: str, targets: list[str]) -> bool:
    """A title sharing a word that says what the work is with a target title: "field" for field
    service, "recruiting" for Recruiter (a word's first six letters), "HR" for HR Generalist
    ("Human Resources Assistant" says HR too). Not a generic one: "coordinator"."""
    def stems(text: str) -> set[str]:
        words = _PAYABLES.sub(r"accounting \1", title_words(text)).split()
        return {_SAME_WORK.get(w, w[:6]) for w in words if (len(w) > 3 or w == "hr") and w not in _GENERIC_TITLE_WORDS}
    mine = stems(title)
    return any(stems(t) & mine for t in targets)


_TITLE_LEVEL = re.compile(r"\b(?:engineer|technician|tech|specialist|fse|representative|analyst|level)\s+(i{1,3}|iv|v|[1-5])\b")
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# Posting text
_DEGREES = [  # lowest first, so "Associate's or Bachelor's" counts as an Associate's
    ("high_school", r"high school|\bged\b"),
    ("associate", r"associate'?s?\s+(?:degree|of)|associate's|associates? or (?:a )?bachelor|\ba\.?a\.?s\.?\b|\ba\.s\.|(?:2|two)[- ]year (?:technical )?(?:degree|program)|"
                  r"technical degree|(?:technical|trade|vocational) school"),
    ("bachelor", r"bachelor|\bb\.?s\.?(?:e\.?e\.?|m\.?e\.?|c\.?)?\b|\bb\.?a\.?\b|(?:4|four)[- ]year degree|undergraduate degree|"
                 r"(?:^|\b(?:a|an|or)\s+)degree in (?:electrical|electronic|mechanical|engineering|computer|physics|chemi|"
                 r"science|math|a related|related|a technical|technical)"),
    ("master", r"master'?s|\bm\.?s\.?(?:c\.?)?\b(?!\s*(?:office|word|excel|project))|\bmba\b"),
    ("doctorate", r"\bph\.?\s?d\b|doctorate"),
]
_EQUIVALENT = re.compile(r"or equivalent|equivalent (?:combination|experience|work|military)|in lieu of|"
                         r"substitut\w* for (?:a |an |the )?(?:\w+ )?degree|(?:may|can) be substituted|"
                         r"or (?:relevant|related|equivalent) (?:work )?experience|"
                         r"or (?:at least |a minimum of )?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\+? years|"
                         r"(?:ged|high school)[^.\n]{0,40}(?:plus|and|with) \d|"
                         # a list of ways in: "an associate degree, military technical training, field service
                         # experience, or trade certification" (an item of the list, so a comma after it); not
                         # "a bachelor's degree, with hands-on experience troubleshooting ... or ..."
                         r",\s+(?!with\b|and\b|including\b)(?:[a-z-]+\s+){0,2}(?:training|certification|experience)\s*,"
                         r"[^.]*\bor\b|"
                         # "associate degree or technical certification"; not "and Six Sigma or Lean certification"
                         r"\bor\s+(?:an?\s+)?(?:equivalent|technical|trade|vocational|military|relevant|industry|professional)"
                         r"\s+(?:\w+\s+)?(?:training|certification|certificate)\b")
_PREFERRED = re.compile(r"\b(preferred|a plus|desired|desirable|nice to have|ideally|bonus)\b")
# Headings start with these words or end with a colon ("Preferred Qualifications",
# "Nice to have:"); "Bachelor's degree preferred" is a requirement line, not a heading.
_PREF_WORDS = r"(preferred|desired|nice to have|bonus|a plus)"
_REQ_WORDS = r"(minimum|required|basic|must have|requirements|qualifications|what you need|what you'll need|" \
             r"who you are|you have|you bring|key skills)"
_PREF_HEADING = re.compile(rf"^{_PREF_WORDS}\b|\b{_PREF_WORDS}\b.*:$")
# Not "Education" or "Experience" on their own: under "Preferred Qualifications" they're the
# preferred list's own sub-headings, and reading them as the required list back again put a
# preferred Bachelor's among the requirements
_REQ_HEADING = re.compile(rf"^{_REQ_WORDS}\b|\b{_REQ_WORDS}\b.*:$")
_CLEARANCE = re.compile(r"(?:active|current|secret|top secret|ts/sci|security|dod)\s+clearance|clearance (?:is )?required")
_NO_CLEARANCE = re.compile(r"\bno (?:\w+ )?clearance|clearance (?:is )?not required|"
                           r"(?:not require|without) (?:a |an |any )?(?:\w+ )?clearance")
_CLEARANCE_LATER = re.compile(r"(?:ability|able|eligib\w*) to obtain|obtain and maintain|be able to get")
_US_PERSON = re.compile(r"\bu\.?s\.? persons?\b|\bitar\b|export[- ]control|export administration regulations|"
                        r"\bu\.?s\.? citizenship (?:is )?required")
_EAR = re.compile(r"\bEAR\b")  # in capitals only: "ear plugs" is safety gear
_YEARS = re.compile(r"(\d{1,2})(?:\s*(?:-|\u2013|to)\s*\d{1,2})?\s*\+?\s*(?:\+|or more|plus)?\s*(?:years?|yrs?)\b"
                    r"[^.\n]{0,60}?\bexperience")
_TRAVEL = re.compile(r"(\d{2,3})\s*%\s*(?:of\s+(?:the\s+)?time\s+)?(?:domestic\s+|international\s+)?travel|"
                     r"travel[^.\n]{0,40}?(\d{2,3})\s*%")
_SHIFTS = re.compile(r"night shift|nights|weekend|rotating shift|12[- ]hour|on[- ]call")
_DRIVERS = re.compile(r"\b(?:driver'?s?|driving) licen[cs]e|\bcdl\b")
# the license's own "not required", not another requirement's in the same sentence
_NO_DRIVERS = re.compile(r"(?:licen[cs]e|\bcdl)(?: is| are)? not (?:\w+ )?(?:required|needed|necessary)\b|"
                         r"\bno (?:\w+ )?(?:driver|cdl)|\b(?:do|does) not (?:need|require) (?:a |an )?(?:valid )?(?:driver|cdl)")


@dataclass
class Fit:
    score: int = 50
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    blocked: bool = False  # a hard requirement the profile says isn't met
    held: bool = False  # not preselected until checked: its posting unread, or it's outside the area

    @property
    def recommended(self) -> bool:
        return not self.blocked and not self.held and self.score >= RECOMMEND_AT

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "recommended": self.recommended}


# --------------------------------------------------------------------- profile


def target_titles(prof: Profile) -> list[str]:
    titles = [str(t).strip() for t in prof.get("preferences.titles") or [] if str(t).strip()]
    return titles or list(DEFAULT_TITLES)


def target_query(prof: Profile) -> str:
    return " | ".join(target_titles(prof)[:MAX_ALTERNATIVES])


def target_location(prof: Profile) -> str | None:
    """States named in preferences.locations ("Phoenix, AZ"), else the home state."""
    states = []
    names = sorted(US_STATES.items(), key=lambda x: -len(x[1]))  # "West Virginia" before "Virginia"
    for loc in prof.get("preferences.locations") or []:
        m = re.search(r",\s*([A-Za-z]{2})\b", str(loc))
        if m:
            states.append(m.group(1).upper())
            continue
        padded = f" {norm(str(loc))} "  # "Phoenix, Arizona"
        states += [code for code, name in names if f" {norm(name)} " in padded][:1]
    if not states and prof.get("personal.address.state"):
        states.append(str(prof.get("personal.address.state")).strip())
    return "|".join(dict.fromkeys(states)) or None


def _month(value: Any, today: date) -> tuple[int, int] | None:
    t = str(value or "").strip().lower()
    if not t:
        return None
    if t in ("present", "current", "now", "today"):
        return today.year, today.month
    year = re.search(r"\b(19|20)\d{2}\b", t)
    if not year:
        return None
    month = 1
    for name, num in _MONTHS.items():
        if name in t:
            month = num
            break
    else:
        m = re.search(r"\b(\d{1,2})[/-](?:19|20)\d{2}\b|\b(?:19|20)\d{2}[/-](\d{1,2})\b", t)
        if m:
            month = int(m.group(1) or m.group(2))
    return int(year.group()), min(max(month, 1), 12)


def applicant_years(prof: Profile, today: date | None = None) -> float | None:
    """experience.total_years, else the sum of the work_history spans."""
    total = prof.get("experience.total_years")
    if total is not None:
        try:
            return float(total)
        except (TypeError, ValueError):
            pass
    today = today or date.today()
    months = 0
    for job in prof.get("work_history") or []:
        if not isinstance(job, dict):
            continue
        start = _month(job.get("start"), today)
        end = _month(job.get("end") or "present", today)
        if start and end:
            months += max(0, (end[0] - start[0]) * 12 + end[1] - start[1])
    return round(months / 12, 1) if months else None


def applicant_degree(prof: Profile) -> str | None:
    """The person's highest degree. "Some college", or a school not finished (no degree in
    its entry), is no degree yet: counted as a high school diploma, so a posting that
    requires one above it says so. None only when the profile says nothing about schooling."""
    values = [prof.get("education.highest_degree")] + [
        e.get("degree") for e in prof.get("education_history") or [] if isinstance(e, dict)]
    for value in values:
        key = degree_key(str(value or ""))
        if key:
            return key
    education = prof.get("education")
    schooling = values + (list(education.values()) if isinstance(education, dict) else [])
    if any(str(v or "").strip() for v in schooling) or prof.get("education_history"):
        return "high_school"
    return None


def _travel_limit(value: Any) -> int | None:
    """willing_to_travel: True -> 100, False -> 0, "Yes, up to 75%" -> 75."""
    if isinstance(value, bool):
        return 100 if value else 0
    m = re.search(r"(\d{1,3})\s*%", str(value or ""))
    if m:
        return int(m.group(1))
    if re.match(r"\s*(yes|y)\b", str(value or ""), re.I):
        return 100
    if re.match(r"\s*(no|n)\b", str(value or ""), re.I):
        return 0
    return None


# --------------------------------------------------------------------- listings


def days_since(posted: str, today: date | None = None) -> int | None:
    """'Posted Today', 'Posted 3 Days Ago', 'Posted 30+ Days Ago', '2026-09-29', '09/29/2026'."""
    t = (posted or "").strip().lower()
    if not t:
        return None
    today = today or date.today()
    if "today" in t or "just posted" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)(\+?)\s*(day|week|month)s?\s+ago", t)
    if m:  # "30+ Days Ago" is more than 30: none of the "this month" credit
        return int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(3)] + (1 if m.group(2) else 0)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t) or None
    try:
        if m:
            return (today - date(int(m.group(1)), int(m.group(2)), int(m.group(3)))).days
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
        if m:
            return (today - date(int(m.group(3)), int(m.group(1)), int(m.group(2)))).days
    except ValueError:
        return None
    return None


def title_level(title: str) -> int | None:
    m = _TITLE_LEVEL.search(norm(title))
    if not m:
        return None
    v = m.group(1)
    return _ROMAN.get(v) or int(v)


def _split_sections(text: str) -> tuple[str, str]:
    """(required, preferred): lines under a "Preferred ..." heading count as preferred
    until the next heading."""
    req: list[str] = []
    pref: list[str] = []
    mode = "req"
    for line in text.splitlines():
        # Markdown, as Eightfold's postings come: "## Preferred qualifications", "**Requirements:**"
        low = re.sub(r"^#+\s*|^\*\*(.*?)\*\*(:?)$", r"\1\2", line.strip().lower()).strip()
        if low and len(low) <= 70 and not re.match(r"[-*\u2022\u00b7]", low):
            if _PREF_HEADING.search(low):
                mode = "pref"
            elif _REQ_HEADING.search(low):
                mode = "req"
        (pref if mode == "pref" else req).append(line)
    return "\n".join(req), "\n".join(pref)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


_DEGREE_WORD = re.compile(r"degree|diploma|\bged\b|bachelor|master|associate|ph\.?\s?d|doctorate")
_MARKED = re.compile(rf"{_PREFERRED.pattern}|\b(required|requires?|minimum|must|mandatory)\b")


def _without_preferred(sentence: str) -> str:
    """The parts of a sentence that aren't marked preferred: "bachelor's degree required,
    master's preferred" keeps the Bachelor's. A clause without a degree of its own belongs
    to the one before, until that one says required or preferred: "bachelor's degree in
    engineering, physics, or a related field preferred" is all preferred."""
    groups: list[list[str]] = []
    closed = True
    for clause in re.split(r"\s*[;,]\s*|\s+(?:and|but|while|whereas)\s+", sentence):
        if closed or _DEGREE_WORD.search(clause):
            groups.append([])
        groups[-1].append(clause)
        # a clause that says so (required, preferred) or gives a way around the degree ends its group:
        # "relevant experience in lieu of a degree is acceptable" isn't dropped with a later
        # "field service experience preferred"
        closed = bool(_MARKED.search(clause) or _EQUIVALENT.search(clause))
    return " ".join(" ".join(g) for g in groups if not _PREFERRED.search(" ".join(g))).strip()


def requirements(text: str) -> dict[str, Any]:
    """What a posting's minimum requirements ask for, read from its text."""
    # Workday's and Oracle's curly apostrophes and non-breaking hyphens ("2\u2011year program")
    text = re.sub(r"[\u2010-\u2015]", "-", (text or "").replace("\u2019", "'").replace("\u2018", "'"))
    text = text.split(f"\n{QUESTIONS_HEADING}\n")[0]  # the application form's questions, not the job's
    required, _ = _split_sections(text)
    out: dict[str, Any] = {"degree": None, "degree_or_equivalent": False, "years": None, "clearance": False,
                           "clearance_later": False, "us_person": False, "travel": None, "shifts": False,
                           "drivers_license": False}
    degree_levels: list[int] = []
    years: list[int] = []
    sentences = _sentences(required.lower())
    for i, sentence in enumerate(sentences):
        if _PREFERRED.search(sentence):
            sentence = _without_preferred(sentence)
            if not sentence:
                continue
        levels = [_LEVEL[key] for key, pattern in _DEGREES if re.search(pattern, sentence)]
        if levels and re.search(r"degree|diploma|\bged\b|\bbs\b|\bb\.s|\ba\.a?\.?s\.|bachelor|master|associate|ph\.?d", sentence):
            degree_levels.append(min(levels))
            following = sentences[i + 1] if i + 1 < len(sentences) else ""
            # the way around it may be the next sentence: "Equivalent combination of education and
            # experience will be considered.", "In lieu of a degree, 4 additional years..."
            if _EQUIVALENT.search(sentence) or _EQUIVALENT.search(following) \
                    or re.match(r"[-\s]*or\b", following) and _EQUIVALENT.search("or " + following):
                out["degree_or_equivalent"] = True
        years += [int(y) for y in _YEARS.findall(sentence) if 0 < int(y) <= 15]
        if _CLEARANCE.search(sentence) and not _NO_CLEARANCE.search(sentence):
            if _CLEARANCE_LATER.search(sentence):
                out["clearance_later"] = True
            else:
                out["clearance"] = True
        if _DRIVERS.search(sentence) and not _NO_DRIVERS.search(sentence):
            out["drivers_license"] = True
    low = (text or "").lower()
    out["us_person"] = bool(_US_PERSON.search(low) or _EAR.search(text or ""))
    travel = [int(a or b) for a, b in _TRAVEL.findall(low)]
    out["travel"] = max(travel) if travel else None
    out["shifts"] = bool(_SHIFTS.search(low))
    if degree_levels:
        level = min(degree_levels)
        out["degree"] = next(k for k, v in _LEVEL.items() if v == level)
    out["years"] = min(years) if years else None
    return out


def score_listing(listing: dict[str, Any], prof: Profile, description: str = "",
                  today: date | None = None) -> Fit:
    fit = Fit()
    title = listing.get("title") or ""
    n = norm(title)
    targets = target_titles(prof)
    hit = next((t for t in targets if title_matches(title, t)), None)
    if hit:
        fit.score += 25
        fit.reasons.append(f"title matches “{hit}”")
    elif _near_target(title, targets):
        fit.score += 5  # near one ("Field Engineer" for field service, "Recruiting Coordinator" for Recruiter)
    else:  # even fresh, nearby and qualified, not what the person looks for: shown, not preselected
        fit.score -= 25
        fit.concerns.append("not one of your target titles")

    years = applicant_years(prof, today)
    level = title_level(title)
    if _INTERN.search(n):
        fit.score -= 40
        fit.concerns.append("internship")
    elif _SENIOR.search(n):
        if years is None or years < 5:
            fit.score -= 20
            fit.concerns.append("senior-level role" + (f"; you have about {years:g} years" if years is not None else ""))
    elif level and level >= 3:
        if years is None or years < 4:
            fit.score -= 10
            fit.concerns.append(f"level {level} role" + (f"; you have about {years:g} years" if years is not None else ""))
    elif _JUNIOR.search(n) or level == 1:
        if years is None or years < 3:
            fit.score += 8
            fit.reasons.append("entry-level role")

    days = days_since(listing.get("posted") or "", today)
    if days is not None:
        if days <= 7:
            fit.score += 10
            fit.reasons.append("posted this week" if days else "posted today")
        elif days <= 30:
            fit.score += 4
        elif days > 60:
            fit.score -= 5
            fit.concerns.append("posted over two months ago")

    cities = [norm(str(c).split(",")[0]) for c in prof.get("preferences.locations") or []]
    where = norm(listing.get("location") or "")
    city = next((c for c in cities if c and c in where), None)
    if city:
        fit.score += 5
        fit.reasons.append(f"in {city.title()}")
    elif far := _far_place(listing.get("location") or "", cities):
        if prof.get("preferences.willing_to_relocate") is False:  # shown, not preselected
            fit.score -= 25
            fit.blocked = True
            fit.concerns.append(f"in {far}, away from the places you named (and you'd rather not relocate)")
        else:
            fit.score -= 5
            fit.concerns.append(f"in {far}, away from the places you named")
    for note in listing.get("notes") or []:
        fit.concerns.append(note)

    if description:
        _score_requirements(fit, requirements(description), prof, years)
    fit.score = max(0, min(100, fit.score))
    return fit


def _score_requirements(fit: Fit, req: dict[str, Any], prof: Profile, years: float | None) -> None:
    mine = applicant_degree(prof)
    if req["degree"] and req["degree"] != "high_school":
        asked = _DEGREE_NAME[req["degree"]]
        if mine and _LEVEL[mine] < _LEVEL[req["degree"]]:
            if req["degree_or_equivalent"]:
                fit.score -= 6
                fit.concerns.append(f"asks for {asked} or equivalent experience (you have {_DEGREE_NAME[mine]})")
            else:  # a requirement not met: shown, not preselected
                fit.score -= 25
                fit.blocked = True
                fit.concerns.append(f"requires {asked} (you have {_DEGREE_NAME[mine]})")
        elif mine:
            fit.score += 5
            fit.reasons.append(f"your degree meets the minimum ({asked})")
    if req["years"] is not None and years is not None:
        if req["years"] > years + 0.5:
            fit.score -= 12
            fit.concerns.append(f"asks for {req['years']}+ years (you have about {years:g})")
            # far short (10+ asked of about 5): shown, not preselected; unless the years are the other
            # way to a degree the applicant has ("a Bachelor's or 4 years")
            degree_instead = req["degree_or_equivalent"] and mine and req["degree"] and \
                _LEVEL[mine] >= _LEVEL[req["degree"]]
            fit.blocked = fit.blocked or (req["years"] >= years + 3 and req["years"] > years * 1.5 and not degree_instead)
        else:
            fit.score += 5
            fit.reasons.append(f"you have the {req['years']}+ years it asks for")
    clearance = str(prof.get("work_authorization.security_clearance") or "").strip().lower()
    if req["clearance"] and clearance in ("", "none", "no", "n/a"):
        fit.score -= 30
        fit.blocked = True
        fit.concerns.append("requires an active security clearance")
    if req["us_person"]:
        us_person = prof.get("work_authorization.us_person")
        if us_person is False:
            fit.score -= 40
            fit.blocked = True
            fit.concerns.append("export-controlled: U.S. persons only")
        elif us_person is None:
            fit.concerns.append("export-controlled role: check that you count as a U.S. person")
    limit = _travel_limit(prof.get("preferences.willing_to_travel"))
    if req["travel"] and limit is not None and req["travel"] > limit:
        fit.score -= 10
        fit.concerns.append(f"up to {req['travel']}% travel (you said {limit}%)")
    if req["shifts"] and prof.get("preferences.flexible_schedule") is False:
        fit.score -= 5
        fit.concerns.append("shift, weekend or on-call work")
    if req["drivers_license"] and prof.get("personal.drivers_license") is False:
        fit.score -= 15
        fit.concerns.append("requires a driver's license")


# --------------------------------------------------------------------- whole list

Search = Callable[[str, str | None, int], Awaitable[dict[str, Any]]]
Fetch = Callable[[str], Awaitable[Posting]]


async def _read(item: dict[str, Any], ways: list[tuple[Fetch, str]]) -> Posting | None:
    """A listing's posting by the first of `ways` (a reader and an address) that reads one with
    its text; a page that answers with a menu and no posting (one that builds itself with
    script) only when none does. Read from another address, it's still applied for where the
    listing links (a Jibe site's page describes an iCIMS posting)."""
    short = None
    for reader, url in ways:
        with contextlib.suppress(Exception):
            posting = await reader(url)
            if url != item["url"]:
                posting.apply_url = item["url"]
            if _has_text(posting):
                return posting
            short = short or posting
    return short


def _has_text(posting: Posting | dict[str, Any] | None) -> bool:
    text = posting.get("description", "") if isinstance(posting, dict) else posting.description if posting else ""
    return len(str(text).strip()) >= MIN_POSTING_TEXT


READ_AT_MOST = 150  # postings read per search: every one that would be preselected, up to this
HARD_READS = 10  # postings read the hard way (the desk's browser, shared with applications) per search
MIN_POSTING_TEXT = 200  # less than this, and the page held no posting (it builds itself with script)


async def recommend(prof: Profile, search: Search, limit_per_company: int = 10, read_postings: int = 30,
                    fetch: Fetch = fetch_posting, today: date | None = None,
                    fetch_hard: Fetch | None = None) -> dict[str, Any]:
    """Search every employer for the profile's target titles and area, score each listing,
    then read the postings (the top `read_postings`, and every one that would be preselected)
    to check their requirements: each by its own address, else its page on the employer's own
    site, else `fetch_hard` (the desk's browser, for postings that turn away plain requests). One that wasn't read, or turns out to be elsewhere, isn't
    preselected: its requirements (a degree, a clearance) are unchecked."""
    query, location = target_query(prof), target_location(prof)
    found = await search(query, location, limit_per_company)
    items: list[dict[str, Any]] = []
    for r in found.get("results", []):
        items.append({**r, "fit": score_listing(r, prof, today=today)})
    items.sort(key=lambda x: -x["fit"].score)
    sem = asyncio.Semaphore(6)

    def take(item: dict[str, Any], posting: Posting | None) -> None:
        if posting is None:  # an unreadable posting keeps its title-based score
            return
        item["posting"] = {k: getattr(posting, k) for k in
                           ("title", "location", "description", "apply_url", "salary", "employment_type",
                            "posted_at", "external_id", "ats")}
        if not str(item.get("posted") or "").strip() and posting.posted_at:
            item["posted"] = posting.posted_at  # the list didn't say when (EY's, iCIMS portals'); the posting does
        item["fit"] = score_listing(item, prof, posting.description, today)

    async def read(item: dict[str, Any]) -> None:
        async with sem:
            take(item, await _read(item, [(fetch, item["url"]), *([(fetch, item["company_url"])]
                                                                  if item.get("company_url") else [])]))

    to_read = list({id(i): i for i in items[:read_postings] + [i for i in items if i["fit"].recommended]}.values())
    to_read = to_read[:READ_AT_MOST]
    await asyncio.gather(*(read(i) for i in to_read))
    if fetch_hard is not None:
        # the rest the hard way, one at a time, best first, a few: the browser is shared with the
        # applications, and a portal that turned one away (or is down) isn't asked again
        failed: set[str] = set()
        hard = 0
        for item in sorted((i for i in to_read if not _has_text(i.get("posting"))), key=lambda i: -i["fit"].score):
            host = urlparse(item["url"]).hostname or ""
            if hard >= HARD_READS or host in failed:
                continue
            hard += 1
            posting = await _read(item, [(fetch_hard, item["url"])])
            if not _has_text(posting):
                failed.add(host)
                continue
            take(item, posting)
    terms = location_terms(location)
    for item in items:
        fit = item["fit"]
        if "posting" not in item or not _has_text(item["posting"]):
            if fit.score >= RECOMMEND_AT and not fit.blocked:
                fit.held = True
                fit.concerns.append("posting not read yet: its requirements are unchecked" if "posting" not in item
                                    else "the posting's text couldn't be read: its requirements are unchecked")
            continue
        where = item["posting"].get("location") or ""
        if where and location_matches(where, terms) is False and location_matches(item.get("location") or "", terms) is not True:
            fit.held = True
            fit.concerns.append(f"the posting says it's in {where}")
    items.sort(key=lambda x: (-x["fit"].score, x.get("company", ""), x.get("title", "")))
    for item in items:
        item["fit"] = item["fit"].to_dict()
    return {"query": query, "location": location, "results": items,
            "errors": found.get("errors", {}), "browser_only": found.get("browser_only", [])}
