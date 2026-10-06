"""Which openings to apply to first.

Listings from the employer search are scored against the applicant's profile: the
titles they target, their experience against the role's level, how recent the
posting is and, when the posting text can be read, hard requirements such as a
degree, years of experience, a clearance or U.S.-person status. The score only
orders the list; every point comes with a plain-language reason or concern.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable

from .autofill import degree_key, norm
from .config import Profile
from .postings import Posting, fetch_posting
from .search import MAX_ALTERNATIVES, title_matches

# Semiconductor equipment roles, used when the profile names no target titles.
DEFAULT_TITLES = ["field service", "customer service engineer", "customer engineer", "equipment technician"]
RECOMMEND_AT = 60  # scores from here up are preselected in the Job Desk

_LEVEL = {"high_school": 1, "associate": 2, "bachelor": 3, "master": 4, "doctorate": 5}
_DEGREE_NAME = {"high_school": "a high school diploma", "associate": "an Associate's", "bachelor": "a Bachelor's",
                "master": "a Master's", "doctorate": "a doctorate"}
_SENIOR = re.compile(r"\b(senior|sr|staff|principal|lead|manager|director|head|vp|vice president|chief|supervisor)\b")
_JUNIOR = re.compile(r"\b(early career|entry level|entry|junior|jr|graduate|new grad|apprentice|trainee)\b")
_INTERN = re.compile(r"\b(intern|internship|co op|coop)\b")
_TITLE_LEVEL = re.compile(r"\b(?:engineer|technician|tech|specialist|fse|representative|analyst|level)\s+(i{1,3}|iv|v|[1-5])\b")
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

# Posting text
_DEGREES = [  # lowest first, so "Associate's or Bachelor's" counts as an Associate's
    ("high_school", r"high school|\bged\b"),
    ("associate", r"associate'?s?\s+(?:degree|of)|\ba\.?a\.?s\.?\b|(?:2|two)[- ]year (?:technical )?degree|technical degree"),
    ("bachelor", r"bachelor|\bb\.?s\.?(?:e\.?e\.?|m\.?e\.?|c\.?)?\b|\bb\.?a\.?\b|(?:4|four)[- ]year degree|undergraduate degree"),
    ("master", r"master'?s|\bm\.?s\.?(?:c\.?)?\b(?!\s*(?:office|word|excel|project))|\bmba\b"),
    ("doctorate", r"\bph\.?\s?d\b|doctorate"),
]
_EQUIVALENT = re.compile(r"or equivalent|equivalent (?:combination|experience|work|military)|in lieu of|"
                         r"or (?:relevant|related|equivalent) (?:work )?experience|or \d+\+? years|"
                         r"(?:ged|high school)[^.\n]{0,40}(?:plus|and|with) \d")
_PREFERRED = re.compile(r"\b(preferred|a plus|desired|desirable|nice to have|ideally|bonus)\b")
# Headings start with these words or end with a colon ("Preferred Qualifications",
# "Nice to have:"); "Bachelor's degree preferred" is a requirement line, not a heading.
_PREF_WORDS = r"(preferred|desired|nice to have|bonus|a plus)"
_REQ_WORDS = r"(minimum|required|basic|must have|requirements|qualifications|what you need|what you'll need|" \
             r"who you are|you have|you bring|key skills)"
_PREF_HEADING = re.compile(rf"^{_PREF_WORDS}\b|\b{_PREF_WORDS}\b.*:$")
_REQ_HEADING = re.compile(rf"^{_REQ_WORDS}\b|\b{_REQ_WORDS}\b.*:$")
_CLEARANCE = re.compile(r"(?:active|current|secret|top secret|ts/sci|security|dod)\s+clearance|clearance (?:is )?required")
_CLEARANCE_LATER = re.compile(r"(?:ability|able|eligib\w*) to obtain|obtain and maintain|be able to get")
_US_PERSON = re.compile(r"u\.?s\.? person|\bitar\b|export[- ]control|export administration regulations|"
                        r"u\.?s\.? citizenship (?:is )?required")
_EAR = re.compile(r"\bEAR\b")  # in capitals only: "ear plugs" is safety gear
_YEARS = re.compile(r"(\d{1,2})(?:\s*(?:-|\u2013|to)\s*\d{1,2})?\s*\+?\s*(?:\+|or more|plus)?\s*(?:years?|yrs?)\b"
                    r"[^.\n]{0,60}?\bexperience")
_TRAVEL = re.compile(r"(\d{2,3})\s*%\s*(?:of\s+(?:the\s+)?time\s+)?(?:domestic\s+|international\s+)?travel|"
                     r"travel[^.\n]{0,40}?(\d{2,3})\s*%")
_SHIFTS = re.compile(r"night shift|nights|weekend|rotating shift|12[- ]hour|on[- ]call")


@dataclass
class Fit:
    score: int = 50
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    blocked: bool = False  # a hard requirement the profile says isn't met

    @property
    def recommended(self) -> bool:
        return not self.blocked and self.score >= RECOMMEND_AT

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
    for loc in prof.get("preferences.locations") or []:
        m = re.search(r",\s*([A-Za-z]{2})\b", str(loc))
        if m:
            states.append(m.group(1).upper())
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
    for value in [prof.get("education.highest_degree")] + [
            e.get("degree") for e in prof.get("education_history") or [] if isinstance(e, dict)]:
        key = degree_key(str(value or ""))
        if key:
            return key
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
    m = re.search(r"(\d+)\+?\s*(day|week|month)s?\s+ago", t)
    if m:
        return int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
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
    req, pref, mode = [], [], "req"
    for line in text.splitlines():
        low = line.strip().lower()
        if low and len(low) <= 70 and not low.startswith("-"):
            if _PREF_HEADING.search(low):
                mode = "pref"
            elif _REQ_HEADING.search(low):
                mode = "req"
        (pref if mode == "pref" else req).append(line)
    return "\n".join(req), "\n".join(pref)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def requirements(text: str) -> dict[str, Any]:
    """What a posting's minimum requirements ask for, read from its text."""
    required, _ = _split_sections(text or "")
    out: dict[str, Any] = {"degree": None, "degree_or_equivalent": False, "years": None, "clearance": False,
                           "clearance_later": False, "us_person": False, "travel": None, "shifts": False}
    degree_levels: list[int] = []
    years: list[int] = []
    sentences = _sentences(required.lower())
    for i, sentence in enumerate(sentences):
        if _PREFERRED.search(sentence):
            continue
        levels = [_LEVEL[key] for key, pattern in _DEGREES if re.search(pattern, sentence)]
        if levels and re.search(r"degree|diploma|\bged\b|\bbs\b|\bb\.s|bachelor|master|associate|ph\.?d", sentence):
            degree_levels.append(min(levels))
            following = sentences[i + 1] if i + 1 < len(sentences) else ""
            if _EQUIVALENT.search(sentence) or re.match(r"[-\s]*or\b", following) and _EQUIVALENT.search("or " + following):
                out["degree_or_equivalent"] = True
        years += [int(y) for y in _YEARS.findall(sentence) if 0 < int(y) <= 15]
        if _CLEARANCE.search(sentence):
            if _CLEARANCE_LATER.search(sentence):
                out["clearance_later"] = True
            else:
                out["clearance"] = True
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
    elif any(w in n.split() for t in targets for w in norm(t).split() if len(w) > 3):
        fit.score += 5
    else:
        fit.score -= 15
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
            else:
                fit.score -= 25
                fit.concerns.append(f"requires {asked} (you have {_DEGREE_NAME[mine]})")
        elif mine:
            fit.score += 5
            fit.reasons.append(f"your degree meets the minimum ({asked})")
    if req["years"] is not None and years is not None:
        if req["years"] > years + 0.5:
            fit.score -= 12
            fit.concerns.append(f"asks for {req['years']}+ years (you have about {years:g})")
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


# --------------------------------------------------------------------- whole list

Search = Callable[[str, str | None, int], Awaitable[dict[str, Any]]]
Fetch = Callable[[str], Awaitable[Posting]]


async def recommend(prof: Profile, search: Search, limit_per_company: int = 10, read_postings: int = 30,
                    fetch: Fetch = fetch_posting, today: date | None = None) -> dict[str, Any]:
    """Search every employer for the profile's target titles and area, score each listing,
    then read the top `read_postings` postings to check their requirements."""
    query, location = target_query(prof), target_location(prof)
    found = await search(query, location, limit_per_company)
    items: list[dict[str, Any]] = []
    for r in found.get("results", []):
        items.append({**r, "fit": score_listing(r, prof, today=today)})
    items.sort(key=lambda x: -x["fit"].score)
    sem = asyncio.Semaphore(6)

    async def read(item: dict[str, Any]) -> None:
        async with sem:
            try:
                posting = await fetch(item["url"])
            except Exception:  # an unreadable posting keeps its title-based score
                return
        item["posting"] = {k: getattr(posting, k) for k in
                           ("title", "location", "description", "apply_url", "salary", "employment_type",
                            "posted_at", "external_id", "ats")}
        item["fit"] = score_listing(item, prof, posting.description, today)

    await asyncio.gather(*(read(i) for i in items[:read_postings]))
    items.sort(key=lambda x: (-x["fit"].score, x.get("company", ""), x.get("title", "")))
    for item in items:
        item["fit"] = item["fit"].to_dict()
    return {"query": query, "location": location, "results": items,
            "errors": found.get("errors", {}), "browser_only": found.get("browser_only", [])}
