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

from . import pay, workplace
from .ats import host_of
from .autofill import _PARTIAL_STUDY, degree_key, entry_dates, norm, school_under_way
from .config import Profile, listed
from .postings import QUESTIONS_HEADING, Posting, fetch_posting
from .search import (DEFAULT_TITLES, MAX_ALTERNATIVES, US_STATES, avoided_title, location_matches, location_terms,
                     title_matches, title_words)

RECOMMEND_AT = 60  # scores from here up are preselected in the Job Desk

_LEVEL = {"high_school": 1, "associate": 2, "bachelor": 3, "master": 4, "doctorate": 5}
_DEGREE_NAME = {"high_school": "a high school diploma", "associate": "an Associate's", "bachelor": "a Bachelor's",
                "master": "a Master's", "doctorate": "a doctorate"}
_SENIOR = re.compile(r"\b(senior|sr|staff|principal|lead|manager|mgr|director|dir|head|vp|vice president|chief|"
                     r"supervisor)\b")
_JUNIOR = re.compile(r"\b(early career|entry level|entry|junior|jr|graduate|new grad|apprentice|trainee)\b")
_INTERN = re.compile(r"\b(intern|internship|co op|coop)\b")
# Words in many titles that say nothing of the work: "HR Coordinator" is no reason to suggest an
# "Enrollment Coordinator" or a "Clinical Research Coordinator"
_GENERIC_TITLE_WORDS = {"coordinator", "specialist", "manager", "analyst", "associate", "assistant", "representative",
                        "consultant", "director", "senior", "lead", "officer", "administrator", "clerk", "supervisor",
                        "partner", "principal", "junior", "intern", "staff", "team", "level",
                        # (and their short forms, as titles write them: "Coord" says no more than "Coordinator")
                        "coord", "spec", "mgr", "assoc", "asst", "rep", "admin", "sr", "jr", "dept"}
# Metro areas the employer lists cover: a place in the same one as a person's named places is
# near them (Mesa for Tempe), one outside it isn't (Bagdad's or Morenci's mines for Phoenix).
_METROS = [
    {"phoenix", "chandler", "tempe", "mesa", "scottsdale", "gilbert", "glendale", "peoria", "goodyear",
     "surprise", "avondale", "queen creek", "maricopa", "buckeye", "tolleson", "laveen", "cave creek",
     "fountain hills", "el mirage", "litchfield park", "sun city", "paradise valley", "apache junction",
     "san tan valley", "anthem", "casa grande", "ahwatukee", "sun city west", "sun lakes", "youngtown",
     "waddell", "new river", "gold canyon"},
    {"tucson", "oro valley", "marana", "sahuarita", "vail"},
    {"las vegas", "north las vegas", "henderson", "paradise", "spring valley", "enterprise", "summerlin",
     "sunrise manor", "whitney", "winchester", "boulder city"},
    {"reno", "sparks", "carson city"},
]
_CITY_STATE = re.compile(r"^\s*([a-z][a-z .'-]*?)\s*[,-]\s*([a-z]{2})\b")
_COUNTRY_STATE_CITY = re.compile(r"^\s*(?:us|usa)\s*-\s*([a-z]{2})\s*-\s*([a-z][a-z .'-]*?)\s*$")  # iCIMS's US-AZ-Bagdad
_STATE_CODES = {code.lower() for code in US_STATES}


def _far_place(location: str, cities: list[str]) -> str | None:
    """The place a listing names when every place it names is a "City, ST" away from the metro
    area of the person's own places ("Bagdad, AZ" for Phoenix); None when any is near, remote,
    or not a city ("Arizona", "2 Locations", "+80 more"), or the person's places aren't in an
    area this knows."""
    near: set[str] = set().union(*(m for m in _METROS if any(c in m for c in cities)))  # (every named place's metro)
    where = location.lower()
    if not near or not where.strip() or re.search(r"remote|\bmore\b", where):
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
                         # (a word "or", not "for": "licensed for 2 years" is no way around a degree)
                         r"\bor (?:at least |a minimum of )?(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\+? years|"
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
# a degree the posting says it doesn't ask for: "A bachelor's degree is not required", "no degree required"
_NO_DEGREE = re.compile(r"\b(degree|diploma)s?\b(?: is| are)? not (?:\w+ )?(?:required|needed|necessary|mandatory)\b|"
                        r"\bno (?:\w+ )?(?:degree|diploma)s? (?:is |are )?(?:required|needed|necessary)\b|"
                        r"\b(?:does|do) not require (?:an? |any )?(?:[\w']+ ){0,2}(?:degree|diploma)")
_CLAUSES = re.compile(r"\s*[;,()]\s*|\s+(?:but|while|whereas)\s+")
_CLEARANCE = re.compile(r"(?:active|current|secret|top secret|ts/sci|security|dod)\s+clearance|clearance (?:is )?required")
_NO_CLEARANCE = re.compile(r"\bno (?:\w+ )?clearance|clearance (?:is )?not required|"
                           # a labelled line that says none ("Security Clearance Level: None", "Clearance: N/A")
                           r"clearance[\w ]{0,40}\??:\s*(?:none|n/?a|no|not required)\b|"
                           r"(?:not require|without) (?:a |an |any )?(?:\w+ )?clearance")
# (and "eligible for a Secret clearance": one to be granted, not held now: the review of Oct 15)
_CLEARANCE_LATER = re.compile(r"(?:ability|able|eligib\w*) to obtain|obtain and maintain|be able to get|"
                              r"eligib\w* (?:for|to (?:hold|receive)) (?:an? )?(?:[\w/]+ ){0,3}clearance")
_US_PERSON = re.compile(r"\bu\.?s\.? persons?\b|\bitar\b|export[- ]control|export administration regulations|"
                        r"\bu\.?s\.? citizenship (?:is )?required")
# in capitals only, and not the safety gear written so ("EAR protection", "EAR plugs": the review of Oct 15)
_EAR = re.compile(r"\bEAR\b(?!\s*(?:-\s*)?(?i:protection|protectors?|plugs?|muffs?|defenders?|wear|buds?|safety))"
                  r"(?!\s*(?:/|&|and)\s*(?i:hearing|eye))")
_YEARS = re.compile(r"(\d{1,2})(?:\s*(?:-|\u2013|to)\s*\d{1,2})?\s*\+?\s*(?:\+|or more|plus)?\s*(?:years?|yrs?)\b"
                    r"[^.\n]{0,60}?\bexperience")
_TRAVEL = re.compile(r"(\d{2,3})\s*%\s*(?:of\s+(?:the\s+)?time\s+)?(?:domestic\s+|international\s+)?travel|"
                     r"travel[^.\n]{0,40}?(\d{2,3})\s*%")
_SHIFTS = re.compile(r"night shift|nights|weekend|rotating shift|12[- ]hour|on[- ]call")
# The posting's plain word that it won't sponsor a work visa (the design of Oct 18, F7): "We will not sponsor", "unable
# to sponsor", "no visa sponsorship", "Sponsorship is not available", "not eligible for visa sponsorship", "must be
# authorized to work in the US without sponsorship", "Visa Sponsorship: No". Never a hedge or the other way ("may
# sponsor", "Sponsorship available", "with or without sponsorship", "Sponsorship may not be available", "..., but"), a
# question, or a sponsorship of something else (a security clearance, a license, relocation, a green card, an event).
# Unsure, it says nothing: a missed one only leaves a job unmarked, a wrong one keeps a job from a person who needs a
# visa (the review of F7). For ranking only: never an application's answer
_SPONSOR_NOUN = (r"(?:(?:employment-based\s+)?(?:visa|work\s+visa|employment\s+visa|immigration|employment|employer|"
                 r"company|h-?1-?b|work\s+authori[sz]ation|work\s+permit)\s+)?sponsorship")
_WONT = (r"(?:will\s+not|won't|do\s+not|don't|does\s+not|doesn't|cannot|can\s*not|can't|is\s+not|isn't|are\s+not|"
         r"aren't|unable\s+to|not\s+able\s+to|not\s+in\s+a\s+position\s+to|never)")
_NOW = r"(?:(?:currently|presently|be|able\s+to|at\s+this\s+time|now)\s+)*"
_OTHER_THINGS = r"(?:[\w-]+\s+(?:or|and)\s+)?"  # ("relocation or visa sponsorship")
_WHO_NEEDS = (r"(?:candidates|applicants|individuals|anyone|those)\s+(?:who|that)\s+(?:will\s+)?(?:now\s+or\s+in\s+the\s+"
              rf"future\s+)?(?:require|need)\s+(?:any\s+)?{_SPONSOR_NOUN}")
_NO_SPONSORSHIP = re.compile(
    rf"\b{_WONT}\s+{_NOW}sponsor(?:ing)?\b(?!ship)|"
    rf"\bnot\s+(?:currently\s+)?sponsoring\b|"
    rf"\b{_WONT}\s+{_NOW}(?:offer|offering|provide|providing|support|supporting|consider|considering)\s+"
    rf"(?:any\s+|a\s+|the\s+)?{_OTHER_THINGS}{_SPONSOR_NOUN}|"
    rf"\b{_WONT}\s+{_NOW}consider\s+{_WHO_NEEDS}|\bnot\s+open\s+to\s+{_WHO_NEEDS}|"
    rf"\bno\s+{_SPONSOR_NOUN}\b(?!\s+(?:is\s+|will\s+be\s+)?(?:required|needed|necessary|needs?|requirements?))|"
    rf"{_SPONSOR_NOUN}(?:\s+for\s+(?:work\s+authori[sz]ation|employment|work\s+visas?|visas?))?\s+"
    rf"(?:(?:is|are|will)\s+not\s+(?:be\s+)?|isn't\s+|aren't\s+|not\s+|(?:is|are)\s+un)"
    rf"(?:currently\s+)?(?:available|offered|provided|possible|supported|considered|an\s+option)\b|"
    rf"\b(?:not\s+eligible|ineligible|(?:does|do)\s+not\s+qualify)\s+(?:for|to\s+receive)\s+(?:any\s+)?{_OTHER_THINGS}"
    rf"{_SPONSOR_NOUN}|"
    rf"(?<!with or )(?<!with and )\bwithout\s+(?:(?:the\s+)?need\s+(?:for|of)\s+|needing\s+|requiring\s+|"
    rf"(?:the\s+)?requirement\s+(?:of|for)\s+|any\s+|current\s+or\s+future\s+|now\s+or\s+in\s+the\s+future\s+)*"
    rf"{_SPONSOR_NOUN}(?!\s+from\b)|"
    rf"\bmust\s+not\s+(?:now\s+or\s+in\s+the\s+future\s+|currently\s+or\s+in\s+the\s+future\s+)?(?:require|need)\s+"
    rf"(?:any\s+)?{_SPONSOR_NOUN}|"
    # ("Applicants requiring sponsorship will not be considered", "...are not eligible for this role"; not "...are not
    # eligible for the expedited interview track")
    rf"\b(?:requir(?:e|es|ing)|need(?:s|ing)?)\s+(?:[\w-]+\s+){{0,6}}?{_SPONSOR_NOUN}[^.;]{{0,60}}?\b(?:will\s+not\s+"
    rf"be\s+considered|(?:are|is)\s+(?:not\s+eligible|ineligible)(?!\s+for\s+(?!(?:this|the)\s+(?:position|role|job|"
    rf"opening|opportunity)\b|employment\b|hire\b))|need\s+not\s+apply|should\s+not\s+apply|cannot\s+be\s+"
    rf"considered)")
# a line labelled with it: "Visa Sponsorship: No", "Sponsorship Available: None", "Visa Sponsorship Offered? No"
_SPONSORSHIP_LINE = re.compile(r"(?im)^[ \t*•·-]*(?:visa\s+|immigration\s+|h-?1-?b\s+)?sponsorship(?:\s+(?:available|"
                               r"offered|provided|eligible|eligibility))?[ \t]*(?:\??[ \t]*:|\?)[ \t]*(?:no|none|not\s+"
                               r"(?:available|offered|provided))\b[ \t.]*$")
# a sponsorship of something else ("Security clearance sponsorship is not available", "We will not sponsor relocation",
# "We do not sponsor professional licenses", "...are not eligible for the sign-on bonus"), and what makes it a visa's
_ELSE = (r"clearances?|cdls?|licen[cs]es?|licensure|licensing|tuition|training|certifications?|degrees?|education|"
         r"courses?|exams?|events?|corporate|community|programs?|relocat\w*|housing|travel\w*|memberships?|"
         r"societ(?:y|ies)|bonus(?:es)?|green\s+cards?|perm|conferences?|athletes?|teams?|sports?|clubs?|"
         r"organi[sz]ations?|politic\w*|endorse\w*|charit\w*|booths?|tournaments?|golf|internships?|interns?|"
         r"supervis\w*|managers?|faculty|letters?|famil(?:y|ies)|dependents?|spouses?|relatives?")
_SPONSORED_ELSE = re.compile(rf"\b(?:{_ELSE})\b")
# (never a visa of theirs: "sponsorship of family members' visas")
_NOT_THEIRS = re.compile(r"\b(?:famil(?:y|ies)|dependents?|spouses?|relatives?)\b")
# ("Relocation and sponsorship are not available": two things, the second a visa's)
_ALSO_RELOCATION = re.compile(r"\brelocation\s*,?\s+(?:and|or)\s+(?=(?:visa\s+)?sponsorship\b)")
_VISA = re.compile(r"\bvisas?\b|\bh-?1-?b\b|\bimmigration\b|\bwork\s+(?:authori[sz]ation|permit)\b")
# a sponsor of a work visa, said so ("sponsor applicants", "sponsor for this position"), not a bare "we don't sponsor"
_VISA_CONTEXT = re.compile(rf"{_VISA.pattern}|\bsponsorship\b|\bauthori[sz]|\bemployment\b|\bcandidates?\b|"
                           r"\bapplicants?\b|\bindividuals?\b|\bforeign\b|\bcitizenship\b|\bstatus\b|"
                           r"\bfor\s+this\s+(?:position|role|job|opportunity)\b")
# a hedge, a limit, or sponsorship said the other way in the same sentence ("...for interns, but full-time hires may
# be", "...for internships; full-time roles are eligible", "We will sponsor visas, but...")
_SPONSOR_HEDGE = re.compile(r"\b(?:may|might|could|possibly|potentially|case[- ]by[- ]case|unless|except|generally|"
                            r"typically|usually|in\s+most\s+cases|not\s+always|some\s+(?:positions|roles|cases)|"
                            r"certain\s+(?:positions|roles|cases)|preferred|preferably|ideally|a\s+plus|transfers?|"
                            r"but|however|(?:is|are)\s+eligible|will\s+sponsor|we\s+sponsor|"
                            # (a need for one not held against anyone, sponsorship limited to some: "...will not be
                            # considered a negative factor", "...in hiring decisions", "...any less favorably", "...not
                            # available to contractors; employees hired directly receive sponsorship")
                            r"factors?|negative\w*|against|in\s+(?:hiring|employment|our)\s+decisions?|"
                            r"less\s+favou?rabl\w*|any\s+less|contractors?|receives?\s+(?:visa\s+)?sponsorship|"
                            r"(?<!no )(?<!no visa )(?<!no h-1b )(?<!no h1b )(?<!no immigration )sponsorship\s+is\s+"
                            r"available)\b|\?")


def _sponsors_else(sentence: str, m: re.Match[str]) -> bool:
    """Whether a match is of another thing's sponsorship: the word right before its "sponsorship" names one
    ("relocation sponsorship"), or its clause does with no visa in it ("We will not sponsor relocation", "...not
    eligible for the sign-on bonus"), or it's a family's."""
    for at in (m.start() + s.start() for s in re.finditer(r"sponsorship", m.group())):
        words = re.findall(r"[\w-]+", sentence[:at])
        if words and _SPONSORED_ELSE.fullmatch(words[-1]):
            return True
    start = sentence.rfind(";", 0, m.start()) + 1
    stop = sentence.find(";", m.end())
    clause = _ALSO_RELOCATION.sub(" ", sentence[start:stop if stop >= 0 else len(sentence)])
    return bool(_NOT_THEIRS.search(clause) or _SPONSORED_ELSE.search(clause) and not _VISA.search(clause))


def no_sponsorship_said(text: str) -> bool:
    """Whether a posting says plainly that it won't sponsor a work visa (_NO_SPONSORSHIP): in a sentence of its own,
    with no hedge in it, of a visa (not a clearance's, a license's or relocation's sponsorship), or on a line labelled
    so ("Visa Sponsorship: No", "Visa Sponsorship Offered? No"). A question, "may not sponsor", "sponsorship
    available" or a "but" says nothing."""
    text = _prepared(text)
    if _SPONSORSHIP_LINE.search(text):
        return True
    low = re.sub(r"\bu\.\s?s\.(?:\s?a\.)?", "us", text.lower())  # ("in the U.S. without sponsorship": one sentence)
    for sentence in _sentences(low):
        if _SPONSOR_HEDGE.search(sentence):
            continue
        for m in _NO_SPONSORSHIP.finditer(sentence):
            if _sponsors_else(sentence, m):
                continue
            if "sponsorship" not in m.group() and not _VISA_CONTEXT.search(sentence):
                continue  # ("We do not sponsor." alone: of what?)
            return True
    return False


_DRIVERS = re.compile(r"\b(?:driver'?s?|driving) licen[cs]e|\bcdl\b")
# the license's own "not required", not another requirement's in the same sentence
_NO_DRIVERS = re.compile(r"(?:licen[cs]e|\bcdl)(?: is| are)? not (?:\w+ )?(?:required|needed|necessary)\b|"
                         r"\bno (?:\w+ )?(?:driver|cdl)|\b(?:do|does) not (?:need|require) (?:a |an )?(?:valid )?(?:driver|cdl)")


START = 50  # where every opening's score starts, before what's added for it and taken off


@dataclass
class Fit:
    score: int = START
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    blocked: bool = False  # a hard requirement the profile says isn't met
    avoided: str = ""  # the title has one of the person's titles to leave out (this one): never recommended
    held: bool = False  # not preselected until checked: its posting unread, or it's outside the area
    # what the title and the posting say of the job, for the desk's filter row (posting_facts): its level always, its
    # requirements once the posting is read. A filter hides an opening only for a fact said here that fails it
    facts: dict[str, Any] = field(default_factory=dict)
    # how the score adds up, part by part, its start among them ({"why": "posted this week", "points": 10}): they sum to
    # the score (the Job Desk shows them by each opening's score)
    parts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def recommended(self) -> bool:
        return not self.blocked and not self.held and not self.avoided and self.score >= RECOMMEND_AT

    def add(self, points: int, why: str) -> None:
        """`points` added to the score (taken off, below 0), with what for, among its parts."""
        self.score += points
        self.parts.append({"why": why, "points": points})

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "recommended": self.recommended}


# Points off each other opening at an employer the person said they're not interested in (the Job Desk's Not
# interested, for its reason "Company"): enough to take a borderline one out of the recommended, not to hide it
NOT_INTERESTED_COMPANY = 10


def not_interested_in(fit: dict[str, Any], company: str, points: int = NOT_INTERESTED_COMPANY) -> dict[str, Any]:
    """An opening's fit (Fit.to_dict()) at an employer the person said Not interested in, for reason Company: `points`
    off its score (kept within 0 to 100), said among its concerns and its parts ("you said not interested in Acme",
    -10), and recommended no longer once that takes it under RECOMMEND_AT. A fit saved before scores had parts gets
    none."""
    score = int(fit.get("score") or 0)
    lowered = max(0, score - points)
    said = f"you said not interested in {company}"
    out = {**fit, "score": lowered, "concerns": [*(fit.get("concerns") or []), said],
           "recommended": bool(fit.get("recommended")) and lowered >= RECOMMEND_AT}
    if isinstance(fit.get("parts"), list):
        out["parts"] = [*fit["parts"], {"why": said, "points": lowered - score}]
    return out


# --------------------------------------------------------------------- profile


def target_titles(prof: Profile) -> list[str]:
    titles = [str(t).strip() for t in listed(prof.get("preferences.titles")) if str(t).strip()]
    return titles or list(DEFAULT_TITLES)


AVOIDED = "which you leave out"  # (said in each concern and part a title to leave out makes)
AVOID_POINTS = 25


def target_query(prof: Profile) -> str:
    return " | ".join(target_titles(prof)[:MAX_ALTERNATIVES])


def target_location(prof: Profile) -> str | None:
    """States named in preferences.locations ("Phoenix, AZ"), else the home state."""
    states = []
    names = sorted(US_STATES.items(), key=lambda x: -len(x[1]))  # "West Virginia" before "Virginia"
    for loc in listed(prof.get("preferences.locations")):
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
        written = entry_dates(job)
        start = _month(written[0], today)
        end = _month(written[1] or "present", today)
        if start and end:
            months += max(0, (end[0] - start[0]) * 12 + end[1] - start[1])
    return round(months / 12, 1) if months else None


def applicant_degree(prof: Profile) -> str | None:
    """The person's highest degree. "Some college", or a school not finished (no degree in
    its entry), is no degree yet: counted as a high school diploma, so a posting that
    requires one above it says so. None only when the profile says nothing about schooling."""
    values = [prof.get("education.highest_degree")] + [
        e.get("degree") for e in prof.get("education_history") or [] if isinstance(e, dict)]
    # (a degree still under way isn't had yet: "Bachelor's (in progress)", or a school that ends in 2027 or "present":
    # the review of Oct 15)
    done = [prof.get("education.highest_degree")] + [
        e.get("degree") for e in prof.get("education_history") or [] if isinstance(e, dict) and not school_under_way(e)]
    for value in done:
        key = degree_key(str(value or ""))
        if key and not _PARTIAL_STUDY.search(norm(str(value))):
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
                           "drivers_license": False, "no_sponsorship": no_sponsorship_said(text)}
    degree_levels: list[int] = []
    years: list[int] = []
    sentences = _sentences(required.lower())
    for i, sentence in enumerate(sentences):
        if _PREFERRED.search(sentence):
            sentence = _without_preferred(sentence)
            if not sentence:
                continue
        # (a clause saying a degree is NOT required drops that degree only: "Bachelor's degree required; an advanced
        # degree is not required" still asks the Bachelor's, the review of 0.3.187)
        asked = " ".join(c for c in _CLAUSES.split(sentence) if not _NO_DEGREE.search(c))
        levels = [_LEVEL[key] for key, pattern in _DEGREES if re.search(pattern, asked)]
        if levels and re.search(r"degree|diploma|\bged\b|\bbs\b|\bb\.s|\ba\.a?\.?s\.|bachelor|master|associate|ph\.?d", asked):
            degree_levels.append(min(levels))
            following = sentences[i + 1] if i + 1 < len(sentences) else ""
            # the way around it may be the next sentence: "Equivalent combination of education and
            # experience will be considered.", "In lieu of a degree, 4 additional years..."
            if _EQUIVALENT.search(sentence) or _EQUIVALENT.search(following) \
                    or re.match(r"[-\s]*or\b", following) and _EQUIVALENT.search("or " + following):
                out["degree_or_equivalent"] = True
        years += [int(y) for y in _YEARS.findall(sentence) if 0 < int(y) <= 15]
        # (not a heading that names a clearance among other things: Boeing's "Security Clearance and Export Control
        # Requirements: This position must meet U.S. export control ...", the second review of F5)
        cleared = re.sub(r"^[^:.]{0,40}clearance\s+and\s+[^:.]{0,60}:\s*", "", sentence)
        if _CLEARANCE.search(cleared) and not _NO_CLEARANCE.search(cleared):
            if _CLEARANCE_LATER.search(cleared):
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


# --------------------------------------------------------------------- the filter row's facts (the design of Oct 18, F5)
# As for pay (pay.py): only a reading that is surely the job's may hide it. For a degree or a security clearance that
# is a line the posting labels with the requirement and its strength, its value on the same line ("Education:
# Bachelor's (Required)", "Required Education: Master's", "Clearance Type: Secret", "Security Clearance Level:
# TS/SCI"), with no hedge, preference or way around it. Its sections can't be told apart surely ("Preferred Education
# and Experience", "Nice to Haves", "Bonus Points", "Plus:" all read as more requirements), so nothing else hides a job:
# what requirements() reads elsewhere is shown, marked as read from the text (the reviews of F5). A shift or travel
# hides only from its own labelled line too.

# A title's level words, read with care: "staff" only as a staff engineer, scientist or architect; "senior" not of a
# senior center, senior living or a senior companion; not a Head Start teacher, lead abatement or lead generation, an
# assistant to the principal or the CEO; and a trainee is entry level whatever else the title says
_RANK_SENIOR = re.compile(r"\b(?:senior(?!\s+(?:living|care|center|centre|services|citizens?|community|housing|"
                          r"residents?|life|day|companion))|sr|snr|staff(?=\s+(?:engineer|scientist|architect))|"
                          r"principal(?!\s+s\b)|lead(?!\s+(?:generation|abatement|free|based|paint|safe|safety|poisoning))|"
                          r"manager|mgr|director|dir|head(?!\s+start)|vp|vice president|chief|supervisor|supervising)\b")
_BOSS = (r"(?:(?:senior|executive|associate|lead)\s+)?(?:principal|director|vp|vice president|ceo|cfo|coo|cto|cio|"
         r"president|chief\s+\w+|manager|dean|superintendent|pastor|rabbi|head|executive director)(?:\s+of\s+\w+)?")
_TO_THE = re.compile(rf"\bto\s+(?:the\s+)?{_BOSS}(?:\s+(?:and|&)\s+(?:the\s+)?{_BOSS})*\b")
_RANK_TRAINEE = re.compile(r"\b(?:trainee|in training)\b")
_RANK_JUNIOR = re.compile(r"\b(?:early career|entry level|(?<!data )entry|junior|jr|graduate|new grad|apprentice|"
                          r"trainee)\b")
# a number at the end of a title's first part that is its level ("Accountant II", "Clerk 2"), not a shift, grade,
# building, store, route or the like ("Production Worker - Shift 2", "Teacher, Grade 5", "RN - PCU Level 2")
_LEVEL_AT_END = re.compile(r"\b(?<!shift )(?<!grade )(?<!grades )(?<!building )(?<!bldg )(?<!store )(?<!route )"
                           r"(?<!region )(?<!district )(?<!site )(?<!location )(?<!clinic )(?<!unit )(?<!room )"
                           r"(?<!post )(?<!fab )(?<!module )(?<!phase )(?<!cohort )(?<!team )(?<!area )(?<!zone )"
                           r"(?<!line )(?<!floor )(?<!station )(?<!title )(?<!pcu )(?<!icu )(?<!nicu )"
                           r"(i{1,3}|iv|v|[1-5])$")


def title_rank(title: str) -> str:
    """A job's level as its title says it: "intern", "senior" (senior, lead, staff engineer, principal, manager,
    supervisor, director..., or level III and up), "entry" (entry level, junior, graduate, apprentice, a trainee
    whatever else it says, or level I), "mid" (level II), or "" when the title doesn't say. A level is read in the
    title's first part only (before " - ", ",", "(" or "/"): its number after the work's word (title_level:
    "Technician II") or its last word ("Accountant II", "Office Assistant III"), never a shift's, a grade's or a
    building's ("Production Worker - Shift 2", "Teacher, Grade 5")."""
    n = norm(title)
    first = norm(re.split(r"\s[-–]\s|[,(/]", title, maxsplit=1)[0])
    level = title_level(first)
    if level is None and (m := _LEVEL_AT_END.search(first)):
        level = _ROMAN.get(m.group(1)) or int(m.group(1))
    if _INTERN.search(n):
        return "intern"
    if _RANK_TRAINEE.search(n):
        return "entry"
    if _RANK_SENIOR.search(_TO_THE.sub(" ", n)) or (level is not None and level >= 3):
        return "senior"
    if _RANK_JUNIOR.search(n) or level == 1:
        return "entry"
    return "mid" if level == 2 else ""


# A line labelled with a requirement: "Required Education: Master's Degree", "Clearance Type: Secret"
_LABELLED = re.compile(r"^[ \t*•·-]*([A-Za-z][A-Za-z /&'()-]{1,60}?)[ \t]*\??[ \t]*:[ \t]*(\S.*)$")
_EDUCATION_LABEL = re.compile(r"(?i)^(?:(?:required|minimum)\s+(?:education(?:al)?|degree)(?:\s+(?:level|required))?|"
                              r"(?:education(?:al)?|degree)\s+(?:\(?required\)?|requirements?)|"
                              r"(?:education(?:al)?|degree)(?:\s+level)?)$")
_STRENGTH = re.compile(r"(?i)\brequired\b|\bminimum\b|\brequirements?\b")
_CLEARANCE_LABEL = re.compile(r"(?i)^(?:required\s+(?:security\s+)?clearance|(?:security\s+)?clearance\s+(?:type|level|"
                              r"required|requirements?|level\s+required|level\s+must\s+currently\s+possess|"
                              r"level\s+must\s+be\s+able\s+to\s+obtain|status))$")
# a hedge, a preference or a way around it: never sure
_HEDGE = re.compile(r"(?i)\b(?:may|might|could|some|certain|depending|depends|future|eventually|tbd|to be determined|"
                    r"within\s+\d+\s+(?:years?|months?|days?)|after\s+hire|of\s+hire|not\s+required|not\s+mandatory|"
                    r"preferred|preferably|preference|a plus|plus|desired|desirable|nice|ideal(?:ly)?|bonus|advantage\w*|"
                    r"beneficial|helpful|optional|asset|welcome|favou?rabl\w*|great|recommended|valued|considered|"
                    r"strongly)\b")
_EQUIVALENT_WIDE = re.compile(r"(?i)\bor\b|\bequivalen\w*|\bin lieu\b|\bin place of\b|\bsubstitut\w*|\binstead\b|"
                              r"\bcombination\b|\bwithout a degree\b|\babsence of\b")
_NOT_SECURITY = re.compile(r"(?i)\b(?:card|fingerprint|dps|dcs|ivp|board|medical|health|tax|customs|osha|fbi|"
                           r"background|drug)\b")
_CLEARANCE_LEVEL = re.compile(r"(?i)\b(?:top secret|secret|ts|ts/sci|sci|confidential|public trust|q|l|"
                              r"yankee white)\b")
_CLEARANCE_NONE = re.compile(r"(?i)^(?:none|n/?a|no|nothing|not\b)|\b(?:not|no|none|without)\b")


def _labelled_lines(text: str) -> list[tuple[str, str, str]]:
    """A posting's labelled lines: (label, value, the next line), the label lowercased."""
    lines = [line.strip() for line in _prepared(text).splitlines() if line.strip()]
    out = []
    for i, line in enumerate(lines):
        if m := _LABELLED.match(line):
            out.append((m.group(1).strip().lower(), m.group(2).strip(), lines[i + 1] if i + 1 < len(lines) else ""))
    return out


def degree_said(text: str) -> tuple[str | None, bool]:
    """The degree a posting surely asks, and whether experience is taken instead (sure only without one: a way around
    it on the line, or the next, makes it unsure); (None, False) when nothing says it surely. Sure only from a line
    labelled with the education asked and its strength, its value on that line: "Education: Bachelor's degree
    (Required)", "Required Education: Master's Degree", "Minimum Education: Bachelor's Degree"; never with a hedge or a
    preference ("within 2 years of hire", "preferred", "a plus") or a way around it ("or equivalent experience")."""
    found: list[int] = []
    for label, value, following in _labelled_lines(text):
        if not _EDUCATION_LABEL.match(label) or not (_STRENGTH.search(label) or re.search(r"(?i)\brequired\b", value)):
            continue
        if _HEDGE.search(value) or _HEDGE.search(label) or _EQUIVALENT_WIDE.search(value) \
                or _EQUIVALENT_WIDE.search(following) and not _LABELLED.match(following):
            continue
        level = _degree_level(value.lower())
        if level is not None:
            found.append(level)
    if not found:
        return None, False
    level = min(found)
    return next(k for k, v in _LEVEL.items() if v == level), False


def _degree_level(value: str) -> int | None:
    """The lowest degree a requirement names, as requirements() reads it (a clause saying one is not required left
    out); None for none."""
    asked = " ".join(c for c in _CLAUSES.split(value) if not _NO_DEGREE.search(c))
    levels = [_LEVEL[key] for key, pattern in _DEGREES if re.search(pattern, asked)]
    if levels and re.search(r"degree|diploma|\bged\b|\bbs\b|\bb\.s|\ba\.a?\.?s\.|bachelor|master|associate|ph\.?d", asked):
        return min(levels)
    return None


def clearance_said(text: str) -> tuple[bool, bool, bool]:
    """(asks a security clearance held now, one to be got, sure). Sure only from a line labelled with the clearance
    asked, its level on that line: "Clearance Type: Secret", "Clearance Required: Secret", "Security Clearance Level:
    TS/SCI", "Clearance Level Must Currently Possess: Secret" ("Must Be Able to Obtain": one to be got); never with a
    hedge or a preference ("may", "some assignments", "preferred"), a None, N/A or No, nor a clearance card,
    fingerprint, DPS, DCS, IVP or Board one. (False, False, False) when nothing says it surely: requirements()'s reading
    is shown, never filtered by."""
    now = later = False
    for label, value, _ in _labelled_lines(text):
        if not _CLEARANCE_LABEL.match(label) or _NOT_SECURITY.search(f"{label} {value}"):
            continue
        if _CLEARANCE_NONE.search(value) or _HEDGE.search(value) or not _CLEARANCE_LEVEL.search(value):
            continue
        if re.search(r"(?i)\bobtain|\beligib|\bable to\b", f"{label} {value}"):
            later = True
        else:
            now = True
    return now, later, now or later


# A posting's own label for its shift ("Shift: 1st Shift (United States of America)", "Shift: Nights", "Schedule:
# Days/Nights"): a line of its own, the label first. Not "Hours:" ("Hours: 40 per week; occasional evenings")
_SHIFT_LINE = re.compile(r"(?im)^[ \t*•-]*(?:work\s+)?(?:shift|shifts|shift\s+(?:schedule|type|pattern|time|details)|"
                         r"schedule|work\s+schedule)[ \t]*:[ \t]*(.{1,160})$")
_DAY_WORDS = re.compile(r"(?i)\b(?:days?|daytime|mornings?|1st|first)\b")
# a value's part that is another shift, whole ("Nights", "Swing", "3rd", "Night Job") or as a shift ("2nd shift",
# "Rotating shifts"); not "every third Saturday", "on-call rotation", "occasional evenings"
_OTHER_PART = re.compile(r"(?i)^(?:the\s+)?(?:\d+\s*x\s*\d+s?\s+)?(?:(?:nights?|overnights?|evenings?|swing|"
                         r"grave(?:yard)?|noc|mids|2nd|3rd|second|third)(?:\s+(?:shifts?|jobs?)\b.*)?|rotating|"
                         r"rotating\s+shifts?\b.*|shift\s+rotation)$")
# a shift said not to be ("no nights", "not nights", "no weekends or nights", "no night shifts")
_NOT_SHIFT = re.compile(r"(?i)\b(?:no|not|non|never|without)\s+(?:(?:\w+\s+)?(?:or|and)\s+)?(?:night|evening|overnight|"
                        r"weekend|swing|graveyard|rotating)\w*(?:\s+shifts?)?(?:\s+(?:or|and)\s+\w+)?")
# pay for a shift is no shift ("differentials paid for evenings and nights", "Evening shift differential: $2.00/hr")
_SHIFT_PAY = re.compile(r"(?i)differential|premium|\bpay\b|\bpaid\b|bonus|\$")
_ROTATING = re.compile(r"(?i)\brotat\w*\b")
# a shift in the posting's sentences: "this is a night shift position", "day shift"
_DAY_SHIFT = re.compile(r"(?i)\b(?:day|1st|first)[ -]shifts?\b")
_OTHER_SHIFT = re.compile(r"(?i)\b(?:night|overnight|evening|swing|graveyard|2nd|3rd|second|third|rotating)[ -]shifts?\b")


def _shift_value(value: str) -> str:
    """A shift line's value as day, other (nights, evenings, swing, a 2nd or 3rd or rotating shift), mixed (both, or a
    rotation of days and nights) or "" (neither, or any: "Any", "Varies", "All shifts")."""
    value = _NOT_SHIFT.sub(" ", re.sub(r"\([^)]*\)", " ", value))
    # (a clause about a shift's pay is no shift: "differentials paid for evenings and nights")
    value = " ; ".join(c for c in re.split(r"[;,]", value) if not _SHIFT_PAY.search(c))
    if re.match(r"(?i)\s*(?:any|all|varies|variable|various|open|flexible|n/?a|not applicable)\b", value):
        return ""
    parts = [" ".join(p.split()) for p in re.split(r"(?i)[,;/|&]|\s[-\u2013]\s|\b(?:and|or|with|plus)\b", value)]
    parts = [p for p in parts if p]
    other = any(_OTHER_PART.match(p) for p in parts)
    day = bool(_DAY_WORDS.search(value))
    if _ROTATING.search(value) and re.search(r"(?i)\bdays?\b", value) and re.search(r"(?i)\bnights?\b", value):
        return "mixed"  # ("Rotating Days/Nights", "Day/Night rotation")
    return "mixed" if day and other else "day" if day else "other" if other else ""


def shift_said(text: str) -> tuple[str, bool]:
    """A posting's shift: ("day", sure), ("other", sure) for nights, evenings, swing, rotating or a 2nd or 3rd shift,
    ("mixed", sure) when it names both, or ("", False) when it doesn't say. `sure` only from a line the posting labels
    its shift or schedule on, read with care (a "no nights" is none, "every third Saturday", "on-call rotation" and a
    shift's pay aren't shifts); one read from its sentences is shown, and never hides a job."""
    text = _prepared(text)
    for line in _SHIFT_LINE.findall(text):
        said = _shift_value(line)
        if said:
            return said, True
    text = _NOT_SHIFT.sub(" ", text)
    d, o = bool(_DAY_SHIFT.search(text)), bool(_OTHER_SHIFT.search(text))
    return ("mixed" if d and o else "day" if d else "other" if o else ""), False


# travel said as the job's, a share of its time: "up to 25% travel", "Travel: 0-25%", "Travel: Yes, 50% of the Time",
# "30-40% overnight travel"; on one line. Not "travel reimbursement", "travel budget", "Travel perks: 30% off", "401(k)
# with 100% match", "100% employer-paid", "100% free standby flights"
_NOT_TRAVEL = (r"(?![ \t]*(?:reimburs|expenses?|allowance|stipends?|pay\b|paid\b|time\b|bonus|policy|insurance|"
               r"benefits?|budget|spend|costs?|claims?|booking|agents?|nurses?|coordinat|perks?|discounts?|"
               r"privileges|arrangements|management|desk|accident|and\s+expense|&))")
_NOT_TRAVEL_AFTER = (r"(?![ \t]*(?:off\b|discounts?|match|paid|covered|reimburs\w*|free\b|employer|employee|vested|"
                     r"commission|coverage|compliance|accuracy|satisfaction|optional|higher|of[ \t]+(?:mileage|airfare|"
                     r"costs?|expenses?|the[ \t]+irs)))")
_TRAVEL_SAID = re.compile(
    rf"(?i)(?:(\d{{1,3}})[ \t]*%?[ \t]*(?:-|to)[ \t]*)?(\d{{1,3}})[ \t]*%[ \t]*(?:of[ \t]+(?:the[ \t]+|your[ \t]+)?"
    rf"time[ \t]+)?(?:(?:domestic|international|overnight|business|regional|local|and|or|/)[ \t]*)*"
    rf"travel(?:ing|ling)?\b{_NOT_TRAVEL}|"
    rf"\btravel(?:ing|ling)?(?:[ \t]+(?:required|requirements?|expectations?|percentage(?:[ \t]+required)?|percent|"
    rf"amount|is[ \t]+required(?:[ \t]+at)?))?[ \t]*(?::|-|–|is|of|requires?|at)?[ \t]*(?:yes[ \t]*,?[ \t]*)?"
    rf"(?P<more>>=?|≥|more[ \t]+than|over|at[ \t]+least)?"
    rf"(?:up[ \t]+to|approximately|approx\.?|about|around|roughly|less[ \t]+than|under|<|~)?"
    rf"[ \t]*(?:(\d{{1,3}})[ \t]*%?[ \t]*(?:-|to)[ \t]*)?(\d{{1,3}})[ \t]*%{_NOT_TRAVEL_AFTER}")


def _prepared(text: str) -> str:
    """A posting's text as requirements() reads it: plain apostrophes and hyphens, without the application's questions."""
    text = re.sub(r"[‐-―]", "-", (text or "").replace("’", "'").replace("‘", "'"))
    return text.split(f"\n{QUESTIONS_HEADING}\n")[0]


def travel_said(text: str) -> int | None:
    """The most travel a posting says the job asks, as a share of its time ("up to 25% travel", "Travel: 0-25%": 25;
    "Travel: >25%": 26), or None when it doesn't say it so."""
    found = [int(m.group(2) or m.group(5)) + (1 if m.group("more") else 0)
             for m in _TRAVEL_SAID.finditer(_prepared(text))]
    found = [n for n in found if 0 <= n <= 101]
    return min(max(found), 100) if found else None


def posting_facts(title: str, req: dict[str, Any] | None = None, text: str = "") -> dict[str, Any]:
    """What an opening says of itself, for the desk's filter row (the design of Oct 18, F5): its `level` from its title
    (title_rank) always; once its posting is read (`req`, requirements() of its `text`), `read`, its degree (`degree`,
    None when none is asked; `degree_or_equivalent` when experience is taken instead; `degree_sure` when degree_said
    reads it surely, else requirements()'s reading, shown only), its security clearance (`clearance` held now,
    `clearance_later` to be got; `clearance_sure` from clearance_said, else requirements()'s reading, shown only), its
    travel (`travel`, a share of its time; `travel_sure` when said as the job's), its shift (`shift`: day, other,
    mixed or ""; `shift_sure` from its own shift line) and whether it says it won't sponsor a work visa
    (`no_sponsorship`). A filter hides an opening only for a fact said and sure that
    fails it; an unread posting says none of these, and stays."""
    out: dict[str, Any] = {"level": title_rank(title)}
    if req is None:
        return out
    travel = travel_said(text)
    shift, shift_sure = shift_said(text)
    degree, instead = degree_said(text)
    now, later, clearance_sure = clearance_said(text)
    out.update({"read": True,
                "degree": degree or req["degree"],
                "degree_or_equivalent": instead if degree else req["degree_or_equivalent"],
                "degree_sure": degree is not None,
                "clearance": now or (not clearance_sure and req["clearance"]),
                "clearance_later": later or (not clearance_sure and req["clearance_later"]),
                "clearance_sure": clearance_sure,
                "travel": travel if travel is not None else req["travel"], "travel_sure": travel is not None,
                "shift": shift, "shift_sure": shift_sure,
                # (said plainly, or not at all: no_sponsorship_said)
                "no_sponsorship": bool(req.get("no_sponsorship"))})
    return out


def score_listing(listing: dict[str, Any], prof: Profile, description: str = "",
                  today: date | None = None) -> Fit:
    fit = Fit()
    fit.facts = posting_facts(listing.get("title") or "")
    fit.parts.append({"why": "start", "points": fit.score})
    title = listing.get("title") or ""
    n = norm(title)
    targets = target_titles(prof)
    hit = next((t for t in targets if title_matches(title, t)), None)
    if hit:
        fit.add(25, f"title matches “{hit}”")
        fit.reasons.append(f"title matches “{hit}”")
    elif _near_target(title, targets):
        # near one ("Field Engineer" for field service, "Recruiting Coordinator" for Recruiter)
        fit.add(5, "title near one of yours")
    else:  # even fresh, nearby and qualified, not what the person looks for: shown, not preselected
        fit.add(-25, "not one of your target titles")
        fit.concerns.append("not one of your target titles")
    # a title the person leaves out (preferences.avoid_titles, the What and where window): shown under All, never
    # recommended (the design of Oct 18, F4)
    if avoided := avoided_title(title, prof.avoid_titles):
        fit.add(-AVOID_POINTS, f"title has “{avoided}”, {AVOIDED}")
        fit.avoided = avoided
        fit.concerns.append(f"title has “{avoided}”, {AVOIDED}")

    years = applicant_years(prof, today)
    level = title_level(title)
    if _INTERN.search(n):
        fit.add(-40, "internship")
        fit.concerns.append("internship")
    elif _SENIOR.search(n):
        if years is None or years < 5:
            fit.add(-20, "senior-level role")
            fit.concerns.append("senior-level role" + (f"; you have about {years:g} years" if years is not None else ""))
    elif level and level >= 3:
        if years is None or years < 4:
            fit.add(-10, f"level {level} role")
            fit.concerns.append(f"level {level} role" + (f"; you have about {years:g} years" if years is not None else ""))
    elif _JUNIOR.search(n) or level == 1:
        if years is None or years < 3:
            fit.add(8, "entry-level role")
            fit.reasons.append("entry-level role")

    days = days_since(listing.get("posted") or "", today)
    if days is not None:
        if days <= 7:
            fit.add(10, "posted this week" if days else "posted today")
            fit.reasons.append("posted this week" if days else "posted today")
        elif days <= 30:
            fit.add(4, "posted this month")
        elif days > 60:
            fit.add(-5, "posted over two months ago")
            fit.concerns.append("posted over two months ago")

    cities = [norm(str(c).split(",")[0]) for c in prof.get("preferences.locations") or []]
    where = norm(listing.get("location") or "")
    # (whole words: "Phoenixville, PA" isn't in Phoenix: the review of Oct 15)
    city = next((c for c in cities if c and re.search(rf"\b{re.escape(c)}\b", where)), None)
    if city:
        fit.add(5, f"in {city.title()}")
        fit.reasons.append(f"in {city.title()}")
    elif far := _far_place(listing.get("location") or "", cities):
        if prof.get("preferences.willing_to_relocate") is False:  # shown, not preselected
            fit.add(-25, f"in {far}, away from the places you named")
            fit.blocked = True
            fit.concerns.append(f"in {far}, away from the places you named (and you'd rather not relocate)")
        else:
            fit.add(-5, f"in {far}, away from the places you named")
            fit.concerns.append(f"in {far}, away from the places you named")
    for note in listing.get("notes") or []:
        fit.concerns.append(note)
    # pay: a plus where it reaches what the person wants; never a minus, and nothing for pay not listed
    desired = pay.desired_yearly(prof.get("preferences.desired_salary"))
    listed = pay.from_fields(listing)  # (only a sure pay: one read from the words may be wrong)
    if desired and listed is not None and listed.sure and pay.meets(listed, desired):
        said = f"pay meets your desired ${desired:,.0f}"
        fit.add(5, said)
        fit.reasons.append(said)

    if description:
        req = requirements(description)
        _score_requirements(fit, req, prof, years)
        fit.facts = posting_facts(title, req, description)
    kept = max(0, min(100, fit.score))
    if kept != fit.score:  # (said among the parts too: they sum to the score)
        fit.add(kept - fit.score, "kept within 0 to 100")
    return fit


def _score_requirements(fit: Fit, req: dict[str, Any], prof: Profile, years: float | None) -> None:
    mine = applicant_degree(prof)
    if req["degree"] and req["degree"] != "high_school":
        asked = _DEGREE_NAME[req["degree"]]
        if mine and _LEVEL[mine] < _LEVEL[req["degree"]]:
            if req["degree_or_equivalent"]:
                fit.add(-6, f"asks for {asked} or equivalent experience")
                fit.concerns.append(f"asks for {asked} or equivalent experience (you have {_DEGREE_NAME[mine]})")
            else:  # a requirement not met: shown, not preselected
                fit.add(-25, f"requires {asked}")
                fit.blocked = True
                fit.concerns.append(f"requires {asked} (you have {_DEGREE_NAME[mine]})")
        elif mine:
            fit.add(5, f"your degree meets the minimum ({asked})")
            fit.reasons.append(f"your degree meets the minimum ({asked})")
    if req["years"] is not None and years is not None:
        if req["years"] > years + 0.5:
            fit.add(-12, f"asks for {req['years']}+ years")
            fit.concerns.append(f"asks for {req['years']}+ years (you have about {years:g})")
            # far short (10+ asked of about 5): shown, not preselected; unless the years are the other
            # way to a degree the applicant has ("a Bachelor's or 4 years")
            degree_instead = req["degree_or_equivalent"] and mine and req["degree"] and \
                _LEVEL[mine] >= _LEVEL[req["degree"]]
            fit.blocked = fit.blocked or (req["years"] >= years + 3 and req["years"] > years * 1.5 and not degree_instead)
        else:
            fit.add(5, f"you have the {req['years']}+ years it asks for")
            fit.reasons.append(f"you have the {req['years']}+ years it asks for")
    clearance = str(prof.get("work_authorization.security_clearance") or "").strip().lower()
    if req["clearance"] and clearance in ("", "none", "no", "n/a"):
        fit.add(-30, "requires an active security clearance")
        fit.blocked = True
        fit.concerns.append("requires an active security clearance")
    # a posting that says plainly it won't sponsor a work visa, for a person who needs one: shown, not preselected (the
    # design of Oct 18, F7). Ranking only: no application's question is answered from it
    if req.get("no_sponsorship") and prof.get("work_authorization.requires_sponsorship") is True:
        fit.add(-30, "says it won't sponsor a work visa")
        fit.blocked = True
        fit.concerns.append("says it won't sponsor a work visa (your profile says you need sponsorship)")
    if req["us_person"]:
        us_person = prof.get("work_authorization.us_person")
        if us_person is False:
            fit.add(-40, "export-controlled: U.S. persons only")
            fit.blocked = True
            fit.concerns.append("export-controlled: U.S. persons only")
        elif us_person is None:
            fit.concerns.append("export-controlled role: check that you count as a U.S. person")
    limit = _travel_limit(prof.get("preferences.willing_to_travel"))
    if req["travel"] and limit is not None and req["travel"] > limit:
        fit.add(-10, f"up to {req['travel']}% travel")
        fit.concerns.append(f"up to {req['travel']}% travel (you said {limit}%)")
    if req["shifts"] and prof.get("preferences.flexible_schedule") is False:
        fit.add(-5, "shift, weekend or on-call work")
        fit.concerns.append("shift, weekend or on-call work")
    if req["drivers_license"] and prof.get("personal.drivers_license") is False:
        fit.add(-15, "requires a driver's license")
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
                            "posted_at", "external_id", "ats", *pay.FIELDS, *workplace.FIELDS)}
        if posting.pay_period and not item.get("pay_period"):  # (the opening's pay: shown on the desk, and scored)
            item.update({k: getattr(posting, k) for k in pay.FIELDS})
        if workplace.better(vars(posting), item):  # (where it's done, as the posting says it better than the list)
            item.update({k: getattr(posting, k) for k in workplace.FIELDS})
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
            host = host_of(item["url"])
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
            if fit.score >= RECOMMEND_AT and not fit.blocked and not fit.avoided:
                fit.held = True
                fit.concerns.append("posting not read yet: its requirements are unchecked" if "posting" not in item
                                    else "the posting's text couldn't be read: its requirements are unchecked")
            continue
        where = item["posting"].get("location") or ""
        # (never for a posting the site's own area filter found, whose place it names by the facility: Banner's)
        if where and not item.get("in_area") and location_matches(where, terms) is False \
                and location_matches(item.get("location") or "", terms) is not True:
            fit.held = True
            fit.concerns.append(f"the posting says it's in {where}")
    items.sort(key=lambda x: (-x["fit"].score, x.get("company", ""), x.get("title", "")))
    for item in items:
        item["fit"] = item["fit"].to_dict()
    return {"query": query, "location": location, "results": items,
            "errors": found.get("errors", {}), "browser_only": found.get("browser_only", [])}
