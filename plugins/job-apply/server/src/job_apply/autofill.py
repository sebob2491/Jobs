"""Decide what to put in an application form field, from the profile.

This module is pure logic (no browser) so it can be unit-tested. The browser
layer extracts fields as dicts shaped like:

    {"id": "7", "kind": "select", "label": "Country", "required": True,
     "options": ["United States of America", "Canada"], "value": ""}

and asks `resolve_field` for an answer. Anything that can't be answered with
confidence is returned as `None` and left for Claude/the user.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from .config import Profile, expand

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico",
}
_COUNTRY_ALIASES = [
    # (not only countries: a self-identification form's words for the same answer too)
    {"female", "woman"}, {"male", "man"},
    {"united states", "united states of america", "usa", "us", "u s", "u s a", "america"},
    {"united kingdom", "uk", "great britain", "england"},
    {"netherlands", "the netherlands", "holland"},
    {"south korea", "korea republic of", "republic of korea", "korea"},
    {"taiwan", "taiwan province of china", "chinese taipei"},
]
_PLACEHOLDER_VALUES = re.compile(  # "-- Please Select --" may have a value of its own ("0")
    r"^(-+\s*)?((please )?(select|choose|pick)( one| an? (option|item|value|answer|response|state|country|year|month))?"
    r"( (state|country|year|month|from (the )?list|below))?( \.\.\.|\.\.\.|…)?|"
    r"|make a selection|none selected|no selection)(\s*-+)?$|"
    r"^(-+|mm/dd/yyyy|mm/yyyy)$",  # "No Selection": SuccessFactors' empty dropdowns
    re.I,
)
# whole words: "Yuma, AZ", "Yearly" and "Yesterday" aren't a yes
_YES = re.compile(r"^((yes|y|true|agreed?|i agree)\b|i am\b(?! not)|i do\b(?! not)|i will\b(?! not)|i have\b(?! not)|i can\b(?! not))", re.I)
_NO = re.compile(r"^(no|n|false|never|i am not|i do not|i don'?t|i will not|i won'?t|i have not|i haven'?t|i have never|"
                 r"i'?ve never|i can ?not|i can'?t)\b", re.I)
_FILLER = {"yes", "no", "y", "n", "i", "am", "a", "an", "the", "to", "for", "of", "in", "my", "and", "or", "is", "be",
           "this", "it", "up"}
PAGED_LIST_PAGE = 100  # entries SuccessFactors' paginated select lists at a time
PLACE_LIST_SLICE = 20  # a list of more places than this shows only some of them (a lookup's first page)
_DECLINE = re.compile(r"decline|not (wish|want) to|prefer not|choose not|do not want|don'?t wish|not to (answer|disclose|self)|rather not", re.I)


def norm(s: Any) -> str:
    s = str(s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9+ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_label(label: str) -> str:
    # the form's marker, not the word in a question ("Sponsorship is not required for you?")
    label = re.sub(r"\(required\)|^\s*required\b[:\s]*|[\s:-]*\brequired\s*[*:]?\s*$", " ", label or "", flags=re.I)
    label = label.replace("*", " ")
    label = re.sub(r"\s+", " ", label).strip(" :?")
    return label


def is_empty_value(value: Any) -> bool:
    if value is None or value is False:
        return True
    if isinstance(value, list):
        return not value
    return bool(_PLACEHOLDER_VALUES.match(str(value).strip()))


def polarity(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    s = str(value or "").strip()
    if _NO.match(s):
        return False
    if _YES.match(s):
        return True
    return None


_NEGATION = {"not", "non", "no", "never", "cannot", "can't", "don't", "doesn't", "didn't", "isn't", "aren't", "wasn't",
             "haven't", "hasn't", "won't"}
_SCOPE_END = {"(", ")", ",", ";", ":", "."}
_FUNCTION_WORDS = {"a", "an", "the", "i", "am", "is", "are", "was", "be", "of", "or", "and", "to", "as", "have", "has",
                   "had", "do", "does", "did", "will", "would", "my", "me", "any", "one", "more", "this", "that", "yes",
                   "for", "in", "on", "with", "at", "by", "currently", "presently", "now"}  # ("not currently" is "currently not")


def _said_and_denied(text: str) -> tuple[set[str], set[str]]:
    """The words a text says, and the ones it says are not so (from a "not" to the next comma
    or bracket): "Asian (Not Hispanic or Latino)" says "asian" and denies "hispanic", "latino"."""
    said: set[str] = set()
    denied: set[str] = set()
    negated = False
    for t in re.findall(r"[a-z0-9]+(?:'[a-z]+)?|[(),;:.]", str(text).lower().replace("\u2019", "'")):
        if t in _SCOPE_END:
            negated = False
        elif t in _NEGATION:
            negated = True
        elif t not in _FUNCTION_WORDS:
            (denied if negated else said).add(t)
    return said, denied


def _contradicts(answer: Any, option: str) -> bool:
    """The option says what the answer denies, or the other way round ("Not Hispanic or Latino"
    and "Hispanic/Latino", "I am not a protected veteran" and "Protected Veteran"), or denies
    more than the answer does: "Not a Veteran" for "I am not a protected veteran"."""
    said_a, denied_a = _said_and_denied(answer)
    said_o, denied_o = _said_and_denied(option)
    return bool(said_a & denied_o or denied_a & said_o or denied_a & denied_o and not denied_a <= denied_o)


_US_NAMES = {"united states", "united states of america", "usa", "us", "u s", "u s a"}


def _place_parts(s: Any) -> tuple[str, ...]:
    """A place's parts, a state as its code and without the country: "Phoenix, Arizona" and
    "Phoenix, AZ" are ('phoenix', 'az'); "Arizona, United States" and "US-AZ" are ('az',)."""
    raw = re.sub(r"^\s*usa?\s*-\s*", "", str(s), flags=re.I)
    out = []
    for part in re.split(r"\s*,\s*|\s+-\s+", raw):
        n = norm(part)
        if n:
            out.append(next((code.lower() for code, name in US_STATES.items() if n in (code.lower(), norm(name))), n))
    while out and out[-1] in _US_NAMES:
        out.pop()
    return tuple(out)


def _aliases(n: str) -> set[str]:
    out = {n}
    for group in _COUNTRY_ALIASES:
        if n in group:
            out |= group
    up = n.upper()
    if up in US_STATES:
        out.add(norm(US_STATES[up]))
    for code, name in US_STATES.items():
        if n == norm(name):
            out.add(code.lower())
    return out


_DEGREES = [
    ("doctorate", r"\b(ph ?d|doctor(ate)?|d ?phil|ed ?d)\b"),
    ("master", r"\b(master s?|masters|ms|m s|ma|m a|msc|meng|m eng|mba|mfa)\b"),
    ("bachelor", r"\b(bachelor s?|bachelors|bs|b s|ba|b a|bsc|beng|b eng|bse|bsee|bsme)\b"),
    ("associate", r"\b(associate s?|associates|aas|a a s)\b"),
    ("high_school", r"\b(high school|ged)\b"),
]


def _containing(want: str, normed: list[tuple[str, str]]) -> str | None:
    """The one option that contains `want` as whole words, or that it contains."""
    contains = [o for o, n in normed if want and re.search(rf"(^| ){re.escape(want)}( |$)", n)]
    if len(contains) == 1:
        return contains[0]
    contained = [o for o, n in normed if n and re.search(rf"(^| ){re.escape(n)}( |$)", want)]
    return contained[0] if len(contained) == 1 and not contains else None


def degree_key(s: str) -> str | None:
    n = norm(s)
    for key, pattern in _DEGREES:
        if re.search(pattern, n):
            return key
    return None


# Schooling begun but not finished: "Some High School", "Some College", "Associate's (in
# progress)", "coursework toward a BSEE"
_PARTIAL_STUDY = re.compile(r"\b(some|less than|incomplete|partial(ly)?|attended|attending|coursework|toward|towards|"
                            r"in progress|enrolled|pursuing|unfinished|did not|not completed|no degree)\b")
_STUDY_WORDS = re.compile(r"\b(high school|college|university|degree|diploma|ged|graduate|coursework)\b")


def _partial_study(n: str) -> bool:
    return bool(_PARTIAL_STUDY.search(n) and (_STUDY_WORDS.search(n) or degree_key(n)))


def _partial_level(n: str) -> str | None:
    """What a partial study is of: "Some college" and "College coursework, no degree" are
    college; "Some high school", high school; "Associate's (in progress)", an associate's."""
    if not _partial_study(n):
        return None
    return degree_key(n) or ("college" if re.search(r"\b(college|university|coursework|credits?)\b", n) else None)


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _in_range(want: str, options: list[str]) -> str | None:
    """For a number, the option whose range holds it ("1-3", "3.0 - 3.49", "Less than 1 year",
    "More than 3 years", "5+", "3.5 and above"), when exactly one does."""
    if not re.fullmatch(r"\d+(?:\.\d+)?", want.strip()):
        return None
    x = float(want)
    holding = []
    for o in options:
        n = o.lower().replace(",", "")
        nums = [float(v) for v in _NUMBER.findall(n)]
        if not nums:
            continue
        # whole words: Oracle's ZIP "01022, Westover AFB, Hampden, MA" isn't "over 1022"
        if re.search(r"\b(less than|under|below|fewer than)\b", n):
            low, high, high_open = float("-inf"), nums[0], True
        elif re.search(r"\b(or less|or fewer|or below)\b", n) and len(nums) == 1:
            low, high, high_open = float("-inf"), nums[0], False
        elif re.search(r"\b(more than|over|greater than|above|or more|or higher|plus)\b|\+", n) and len(nums) == 1:
            low, high, high_open = nums[0], float("inf"), False
            if re.search(r"\b(more than|over|greater than)\b", n):
                low += 1e-9
        elif len(nums) >= 2:
            low, high, high_open = nums[0], nums[1], False
            if re.search(r"or higher|and above|or more", n):
                high = float("inf")
            elif high == int(high) and low == int(low) and "." not in n:
                high += 0.999  # "1-3 years" holds 3.5 years
        else:
            continue
        if low <= x and (x < high if high_open else x <= high):
            holding.append(o)
    return holding[0] if len(holding) == 1 else None


def _strip_codes(n: str) -> str:
    """Drop dial codes and bare numbers: '+1 united states of america' -> 'united states of america'."""
    return " ".join(t for t in n.split() if not re.fullmatch(r"\+?\d+", t))


def choose_option(desired: Any, options: list[str], exact_only: bool = False, names: bool = False) -> str | None:
    """Pick the option that best matches `desired`, or None if nothing does. With
    exact_only, only the same text (or an alias: AZ ~ Arizona) counts. With names (a school
    or an employer), one name must contain the other: sharing words isn't enough, since
    "University of Arizona" is not "Arizona State University"."""
    opts = [o for o in options if o and not _PLACEHOLDER_VALUES.match(o.strip())]
    if not opts or desired is None or desired == "":
        return None
    want = norm(desired)
    normed = [(o, norm(o)) for o in opts]

    # 1. exact, including aliases (AZ ~ Arizona, USA ~ United States of America), ignoring
    #    dial codes and flags ("🇺🇸 (+1) United States of America")
    exact = _aliases(want)
    for o, n in normed:
        if n in exact or _aliases(n) & exact:
            return o
    bare = _strip_codes(want)  # (a number alone strips to nothing: no "loose" match then)
    wanted = exact | (_aliases(bare) if bare else set())
    loose = [o for o, n in normed if n in wanted or _aliases(n) & wanted or _strip_codes(n) and _strip_codes(n) in wanted]
    if len(loose) == 1:  # not when only a number told them apart ("Yes - 25%" / "Yes - 75%")
        return loose[0]
    # the same place, its state spelled the other way or its country added: "AZ" is "Arizona,
    # United States" and "US-AZ", "Phoenix, AZ" is "Phoenix, Arizona"
    place = _place_parts(desired)
    same_place = [o for o in opts if place and _place_parts(o) == place]
    if len(same_place) == 1:
        return same_place[0]
    if exact_only:
        return None
    if names:
        return _containing(want, normed)
    # never the opposite of the answer, when its own wording isn't among the choices: "Not
    # Hispanic or Latino" isn't "Hispanic/Latino", nor "I am not a protected veteran" "Protected Veteran"
    # (a decline's "not" is no denial: declines are matched as such below)
    if not _DECLINE.search(str(desired)):
        normed = [(o, n) for o, n in normed if not _contradicts(desired, o)]
        opts = [o for o, _ in normed]
    if degree_key(want) or _partial_study(want):
        # schooling as it is, never more nor less: "High School Diploma" isn't "Some High
        # School", and "Associate's (in progress)" isn't "Associate's Degree"
        partial = _partial_study(want)
        normed = [(o, n) for o, n in normed if not (degree_key(n) or _partial_study(n)) or _partial_study(n) == partial]
        # the one choice that is the same partial study, however worded: "Some college
        # coursework" is "Some College, No Degree"
        level = _partial_level(want)
        alike = [o for o, n in normed if level and _partial_level(n) == level]
        if len(alike) == 1:
            return alike[0]

    # a number among ranges: "10" years -> "More than 3 years", a 3.8 GPA -> "3.50 - 4.00 or higher"
    ranged = _in_range(str(desired), opts)
    if ranged is not None:
        return ranged

    # 2. declines ("Decline to self-identify", "I don't wish to answer", ...)
    if _DECLINE.search(str(desired)):
        hits = [o for o, _ in normed if _DECLINE.search(o)]
        return hits[0] if hits else None

    # 3. yes / no questions
    pol = polarity(desired)
    if pol is not None:
        # the words after the yes or no first, among the answers that don't say the opposite:
        # "Yes, previously" is "I was PREVIOUSLY employed by ASM", not "I am CURRENTLY employed"
        detail = set(want.split()) - _FILLER
        near = sorted(((len(detail & set(n.split())), o) for o, n in normed if polarity(o) is not (not pol)),
                      key=lambda x: -x[0])
        if near and near[0][0] > 0 and (len(near) == 1 or near[0][0] > near[1][0]):
            return near[0][1]
        hits = [o for o, _ in normed if polarity(o) is pol]
        if len(hits) == 1:
            return hits[0]  # e.g. "No" -> "I have NEVER been employed by ASM"
        # several answers say yes: the words after the yes decide ("Yes, for any employer"
        # -> "I am authorized to work in this country for any employer", not "...for my
        # current employer")
        detail = set(want.split()) - _FILLER
        if len(hits) > 1 and detail:
            scores = sorted(((len(detail & set(norm(o).split())), o) for o in hits), key=lambda x: -x[0])
            if scores[0][0] > scores[1][0]:
                return scores[0][1]
        if len(hits) > 1 and len(want.split()) == 1:
            return None  # a bare "Yes" among "Yes, for any employer" / "Yes, for my current employer only": the person says
        # e.g. "No, I will not require sponsorship" vs "No" — fall through to overlap

    # 4. degrees: "BS Electrical Engineering" ~ "Bachelor's Degree"
    dk = degree_key(want)
    if dk:
        same = [(o, n) for o, n in normed if degree_key(n) == dk]
        if len(same) == 1:
            return same[0][0]
        if same:
            normed = same

    # 5. one contains the other
    contains = [o for o, n in normed if want and re.search(rf"(^| ){re.escape(want)}( |$)", n)]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        # "United States" in both "...of America" and "...Minor Outlying Islands": take the
        # one whose extra words the aliases explain, else the one with the fewest extra words
        known = {t for a in wanted for t in a.split()}
        def extra(o: str) -> int:
            return len(set(_strip_codes(norm(o)).split()) - set(want.split()) - known)
        contains.sort(key=extra)
    # whole words only: "male" is in "female", "no" in "not"
    contained = [o for o, n in normed if n and re.search(rf"(^| ){re.escape(n)}( |$)", want)]
    if len(contained) == 1:
        return contained[0]

    # 6. token overlap
    wt = set(want.split())
    best, best_score = None, 0.0
    for o, n in normed:
        ot = set(n.split())
        if not ot or not wt:
            continue
        score = len(wt & ot) / len(wt | ot)
        if pol is not None and polarity(o) is not None and polarity(o) is not pol:
            continue
        if score > best_score:
            best, best_score = o, score
    if best_score >= 0.5:
        return best
    if contains:
        return contains[0]
    return None


@dataclass
class Answer:
    value: Any
    rule: str


Getter = Callable[[Profile, dict], Any]


def _p(path: str) -> Getter:
    return lambda prof, job: prof.get(path)


def _yn(path: str, invert: bool = False) -> Getter:
    def g(prof: Profile, job: dict) -> Any:
        v = prof.get(path)
        if isinstance(v, bool):
            return "Yes" if v != invert else "No"
        return None if invert else v  # free text can't be turned around

    return g


def _authorized(prof: Profile, job: dict) -> Any:
    """Authorized and needing no sponsorship means authorized for any employer, which is
    what forms that also offer "for my current employer" (ASM) are asking."""
    if prof.get("work_authorization.authorized_to_work") is True and \
            prof.get("work_authorization.requires_sponsorship") is False:
        return "Yes, for any employer"
    return _yn("work_authorization.authorized_to_work")(prof, job)


def _edu(summary_key: str, entry_key: str) -> Getter:
    """education.<summary_key>, else the most recent education_history entry."""
    def g(prof: Profile, job: dict) -> Any:
        value = prof.get(f"education.{summary_key}")
        if value is None:
            entries = [e for e in _listed(prof.get("education_history")) if isinstance(e, dict)]
            value = entries[0].get(entry_key) if entries else None
        return value
    return g


def _full_name(prof: Profile, job: dict) -> str | None:
    return prof.full_name or None


def _preferred_full_name(prof: Profile, job: dict) -> str | None:
    first = prof.get("personal.preferred_name") or prof.get("personal.first_name")
    return " ".join(str(x).strip() for x in (first, prof.get("personal.last_name")) if x) or None


def _state(prof: Profile, job: dict) -> str | None:
    s = prof.get("personal.address.state")
    if s and str(s).upper() in US_STATES:
        return US_STATES[str(s).upper()]
    return s


def canonical_country(country: Any) -> str:
    n = norm(country)
    for group in _COUNTRY_ALIASES:
        if n in group:
            return max(group, key=len)
    return n


def _phone_code(prof: Profile, job: dict) -> str | None:
    # Workday-style pickers list "United States of America (+1)", "Canada (+1)", ...
    code = prof.get("personal.phone_country_code", "+1")
    country = prof.get("personal.address.country", "")
    return f"{canonical_country(country)} ({code})" if country else code


def _same_employer(a: str, b: str) -> bool:
    """One name holds the other as whole words: "Intel" ~ "Intel Corporation", not
    "Intelligent Systems"."""
    return bool(a and b and (re.search(rf"(^| ){re.escape(a)}( |$)", b) or re.search(rf"(^| ){re.escape(b)}( |$)", a)))


def _listed(value: Any) -> list[Any]:
    """A profile list as written: one name written on its own is a list of one, and anything
    else that isn't a list (a number typed by mistake) is none."""
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, str) and value.strip() else []


def _previously_employed(prof: Profile, job: dict, label: str = "") -> str | None:
    """Has the person worked for this employer before? Only for a question about working
    for this employer: "Have you ever worked in a cleanroom?" and "Have you worked on Lam
    etch tools?" aren't. The answer comes from the employers in the profile, past and present
    (history.previous_employers, work_history), and says which: "Yes, previously" picks ASM's
    "I was PREVIOUSLY employed by ASM", not "I am CURRENTLY employed"."""
    company = norm(job.get("company"))
    if not company:
        return None
    asked = norm(label)
    first = re.escape(company.split()[0])
    names_it = re.search(rf"\b(employ\w*|work\w*|intern\w*|contract\w*)( \w+){{0,3}}? (by|for|at|with|of) (the |any |an? )?{first}\b"
                         rf"|\b{first}( \w+)? (employee|employment|intern|contractor)s?\b"
                         rf"|\bhired\b.{{0,60}}? (by|with|at) (the )?{first}\b", asked)
    if not (names_it or re.search(r"(employed|worked|hired) (by|for|at|with) (us|this|our|the company)\b|former employee", asked)):
        return None
    if re.search(rf"\b{first}( \w+){{0,2}} (tools?|systems?|equipment|products?|software|technolog\w*|machines?|platforms?|"
                 r"scanners?|metrology|etch|deposition|parts)\b", asked):
        return None  # "worked with KLA metrology systems": the employer's products, not working for it
    past = [norm(c) for c in _listed(prof.get("history.previous_employers"))]
    entries = [e for e in _listed(prof.get("work_history")) if isinstance(e, dict)]
    past += [norm(e.get("company")) for e in entries]
    if not any(_same_employer(c, company) for c in past if c):
        # "...or any of its subsidiaries or affiliates": the profile can't say it's none of those
        return None if re.search(r"\b(subsidiar|affiliat)", asked) else "No"
    now = [norm(e.get("company")) for e in entries if is_present(e.get("end")) or e.get("current") is True]
    now.append(norm(prof.get("experience.current_company")))
    return "Yes, currently" if any(_same_employer(c, company) for c in now if c) else "Yes, previously"


_UNFINISHED = re.compile(r"^(none|n ?a|no|not applicable)$|\b(no degree|not (completed|finished)|incomplete|"
                         r"unfinished|did not (complete|finish|graduate)|in progress|some|coursework|attended)\b")


def finished_degree(value: Any) -> bool:
    """A degree the entry says was earned: not empty, nor "Not completed", "None", "No degree"
    or "Some college (no degree)", which an unfinished school is written as."""
    n = norm(value)
    return bool(n) and not _UNFINISHED.search(n)


def _graduated(prof: Profile, job: dict) -> Any:
    """The year the person graduated: never the last year at a school they didn't finish
    (an education_history entry with no degree), which would say they graduated."""
    year = prof.get("education.graduation_year")
    if year:
        return year
    finished = [e for e in _listed(prof.get("education_history")) if isinstance(e, dict) and finished_degree(e.get("degree"))]
    return finished[0].get("end") if finished else None


def _expected_graduation(prof: Profile, job: dict) -> Any:
    """When the person expects to graduate: the year of a school still under way, or, when
    every school the profile lists has ended, that they aren't attending (a list's "I am
    currently not attending school"). Never a past year, as if still to come."""
    entries = [e for e in _listed(prof.get("education_history")) if isinstance(e, dict)]
    if not entries:
        return None
    today = date.today()
    ongoing: list[str | None] = []
    for e in entries:
        end = e.get("end")
        month, year = parse_month_year(end)
        if is_present(end) or end in (None, "") or not year:
            ongoing.append(None)  # under way, or not said when it ends
        elif int(year) > today.year or int(year) == today.year and month and int(month) >= today.month:
            ongoing.append(year)
        elif int(year) == today.year and not month:
            ongoing.append(None)  # this year, month not given: maybe still to come
    if ongoing:
        return next((y for y in ongoing if y), None)  # a school under way: its year, if the profile has one
    return "Not currently attending school"


_AUTHORIZED = r"authori[sz]ed to work|eligible to work|legally (able|permitted|allowed) to work|right to work|" \
              r"work authori[sz]ation|employment eligibility|unrestricted authori[sz]ation"


def _no_sponsorship(prof: Profile, job: dict, label: str = "") -> Any:
    """Yes means no sponsorship is needed ("…without sponsorship?", "…and do not require
    sponsorship?"). Asked together with being authorized, Yes needs both."""
    needs = prof.get("work_authorization.requires_sponsorship")
    if not isinstance(needs, bool):
        return None
    if re.search(r"authori[sz](ed|ation)|eligible|legally|able to work|for any employer", norm(label)):
        allowed = prof.get("work_authorization.authorized_to_work")
        if allowed is False or needs:
            return "No"
        return "Yes" if allowed is True else None
    return "No" if needs else "Yes"


def _sponsorship(prof: Profile, job: dict, label: str = "") -> Any:
    """Will the person need sponsorship? Not answered when the same question also asks whether
    they're authorized ("Are you authorized … and will you require sponsorship?"), which a
    single Yes or No can't answer both halves of."""
    if re.search(_AUTHORIZED, norm(label)):
        return None
    return _yn("work_authorization.requires_sponsorship")(prof, job)


def _age(invert: bool) -> Getter:
    """Over 18 (or, inverted, under). Not answered from age alone when the question also asks
    about working or sponsorship ("at least 18 and legally authorized to work…?")."""
    def g(prof: Profile, job: dict, label: str = "") -> Any:
        if re.search(r"authori[sz]|sponsor|eligib|legally|permitted to work|able to work|right to work", norm(label)):
            return None
        return _yn("work_authorization.over_18", invert=invert)(prof, job)
    return g


_UNDER_18, _OVER_18 = _age(True), _age(False)


# The date a form is signed on: "Date" alone is that on a signed form (Workday's disability
# self-identification, CC-305); "Signature Date" is not a signature
_SIGNED_DATE = re.compile(r"^date$|today s date|date signed|signature date|date of signature")


def _today_for(field: dict) -> str:
    """Today as the date box takes it: one part (Workday's Month / Day / Year boxes), or whole."""
    today = date.today()
    sub = norm(field.get("sublabel"))
    if sub in ("month", "day", "year"):
        return {"month": f"{today.month:02d}", "day": f"{today.day:02d}", "year": str(today.year)}[sub]
    if field.get("input_type") == "date":
        return today.isoformat()
    return today.strftime("%m/%d/%Y")


def _relocate(prof: Profile, job: dict) -> Any:
    return _yn("preferences.willing_to_relocate")(prof, job)


def _veteran(prof: Profile, job: dict, label: str = "") -> Any:
    """The EEO veteran answer. "I am not a protected veteran" doesn't say whether the person is
    a veteran at all: "Are you a veteran?" is then theirs to answer."""
    v = prof.get("eeo.veteran")
    if not v:
        return None
    asked = norm(label)
    if re.search(r"\bare you an? (u s |us |military )?veteran\b", asked) and "protected" not in asked \
            and polarity(v) is False and re.search(r"\bprotected\b", norm(v)):
        return None
    return v


def _employed_now(prof: Profile, job: dict) -> Any:
    """Whether the person has a job now: a work history entry that hasn't ended. None when the
    profile lists no work at all."""
    entries = [e for e in _listed(prof.get("work_history")) if isinstance(e, dict)]
    if not entries:
        return None
    return "Yes" if any(is_present(e.get("end")) or e.get("current") is True for e in entries) else "No"


def _lives_in(prof: Profile, job: dict, label: str = "") -> Any:
    """"Do you currently live in Arizona?", "...in the Phoenix area?": from the profile's
    address. A state is the address's state; a city the address's city or, asked about its
    area, a town in its metro area (Arizona's). A distance is the person's to judge."""
    asked = norm(label)
    m = re.search(r"\b(?:live|living|reside|residing|located|based) in (?:the )?(.+)$", asked)
    if not m or re.search(r"\d|\bmiles?\b|\bwithin\b|commut|distance|relocat|\bor\b", asked):
        return None
    place = m.group(1).strip()
    area = bool(re.search(r"\b(area|metro\w*|region|valley)$", place))
    place = re.sub(r"\s*\b(metro(politan)? area|metro|area|region|valley)$", "", place).strip()
    state = str(prof.get("personal.address.state") or "").strip()
    home_code = state.upper() if state.upper() in US_STATES else next(
        (code for code, name in US_STATES.items() if norm(name) == norm(state)), None)
    if place in _US_NAMES:
        country = norm(prof.get("personal.address.country"))
        return ("Yes" if country in _US_NAMES else "No") if country else None
    asked_code = place.upper() if len(place) == 2 and place.upper() in US_STATES else next(
        (code for code, name in US_STATES.items() if norm(name) == place), None)
    if asked_code:
        return ("Yes" if asked_code == home_code else "No") if home_code else None
    city = norm(prof.get("personal.address.city"))
    if not city or not home_code:
        return None
    if place == city:
        return "Yes"
    if home_code == "AZ" and area and _az_metro(place) and _az_metro(city):
        return "Yes" if _az_metro(place) == _az_metro(city) else "No"
    return None


def _local_or_relocate(prof: Profile, job: dict, label: str = "") -> Any:
    """"Are you located within 100 miles of a campus, or willing to relocate?": Yes when the
    person would move, or lives in the metro area of a place the job names (Tempe for a
    Phoenix job). Else the person's to answer, as is "do you live nearby, or will you need to
    relocate?", which asks which, not whether."""
    asked = next((part for part in re.split(r"[.?!]\s+", label) if re.search(r"relocat", part, re.I)), "")
    if not (re.match(r"\s*(are|do|would|will|can)\s+you\b", asked, re.I)
            and re.search(r"\bor (are you |would you be )?(willing|able|open) to relocat", norm(asked))):
        return None  # not a yes-or-no question: "Which campus are you near, or are you willing to relocate?"
    if prof.get("preferences.willing_to_relocate") is True or lives_near(prof, str(job.get("location") or "")):
        return "Yes"
    return None


def _travel(prof: Profile, job: dict, label: str = "") -> Any:
    """Willing to travel: not whether anything stops the person travelling, nor a passport, nor
    travel abroad, which a willingness to travel (some of the time) doesn't say."""
    if re.search(r"restrict|passport|prevent|limitation|visa|unable to|not able to|anything that|international|overseas|"
                 r"abroad|outside (of )?(the )?(u s|us|united states|country)", norm(label)):
        return None
    v = prof.get("preferences.willing_to_travel")
    percent = r"(\d+) ?(?:%|percent|per cent)"
    asked, mine = re.findall(percent, label, re.I), re.findall(percent, str(v or ""), re.I)
    if asked and max(map(int, asked)) > (max(map(int, mine)) if mine else 100):
        return None  # more travel than the profile agrees to: the user decides
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return v


def _phone(prof: Profile, job: dict, label: str = "") -> Any:
    """The phone number; with its dial code where the box asks for it ("including country code")."""
    phone = prof.get("personal.phone")
    if phone and re.search(r"(including|incl|with) (the |your )?country code", norm(label)) \
            and not str(phone).strip().startswith("+"):
        return f"{prof.get('personal.phone_country_code', '+1')} {phone}"
    return phone


_SOMEONE_ELSE = re.compile(r"\b(referen\w*|referee\w*|emergency|next of kin|supervisor\w*|manager\w*|referr\w*|contact person|"
                           r"relatives?|family|spouse)\b")
# A box for another party's details, by its own words ("Employer Phone", "School Zip Code") or
# its section's ("Most Recent Employer", "High School")
_OTHER_PARTY_LABEL = re.compile(r"\b(employer\w*|company|business|school)\b")
_OTHER_PARTY_SECTION = re.compile(r"\b(employer\w*|school)\b")
_EEO_RULES = {"gender", "hispanic", "race", "veteran", "disability"}
# A question about someone in the person's family ("Gender of your spouse", "Are you the spouse of a veteran?"),
# not a family word in passing ("we partner with veterans", "family and medical leave")
_FAMILY = re.compile(r"\b(your|their) (spouse|husband|wife|partner|parents?|child(ren)?|dependents?|relatives?|family"
                     r"( members?)?|next of kin)\b|\b(spouse|husband|wife|widow\w*|partner|parent|child|dependent|relative|"
                     r"family member) of (a|an|the|any)\b")
_CONTACT_RULES = {"email", "first_name", "middle_name", "last_name", "preferred_name", "preferred_full_name", "full_name",
                  "phone_type",
                  "phone_code", "phone_ext", "phone", "address1", "address2", "city", "postal_ext", "postal",
                  "county", "state", "country"}

# Getters that read the question itself, not only the profile
_READS_QUESTION = {_travel, _previously_employed, _no_sponsorship, _sponsorship, _UNDER_18, _OVER_18, _phone,
                   _local_or_relocate, _lives_in, _veteran}

# (rule name, label regex, getter, max label length or None, allowed kinds or None)
_TEXTY = {"text", "textarea", "select", "listbox", "combobox", "radio_group"}
RULES: list[tuple[str, str, Getter, int | None, set[str] | None]] = [
    ("how_heard", r"(how|where) did you (first )?(hear|find|learn)|source of (application|referral)|^source$", _p("preferences.how_did_you_hear"), None, None),
    # contact details: short labels only, so long questions that merely mention
    # "state" or "name" don't match
    ("email", r"^(confirm |re ?enter |re ?type |verify )?e ?mail( address)?( again)?$|^(your )?email\b|^enter (your )?e ?mail\b",
     _p("personal.email"), 45, None),  # "Enter email to start application process" (Qorvo)
    ("first_name", r"^(legal )?(first|given)( name)?$|^(legal )?first name|^given name|^forename", _p("personal.first_name"), 45, None),
    ("middle_name", r"^middle (name|initial)", _p("personal.middle_name"), 45, None),
    ("last_name", r"^(legal )?(last|family|sur)( ?name)?$|^(legal )?(last|family) name|^surname", _p("personal.last_name"), 45, None),
    # American Express's Oracle form: "Preferred Full Name", the name the person goes by and their last name
    ("preferred_full_name", r"^preferred full name", _preferred_full_name, 45, None),
    ("preferred_name", r"^preferred (first )?name|^nick ?name", lambda p, j: p.get("personal.preferred_name") or p.get("personal.first_name"), 45, None),
    ("full_name", r"^(full |legal |your |candidate )?(full )?name$|^full (legal )?name|^legal name", _full_name, 45, None),
    ("phone_type", r"phone (device )?type|type of phone", lambda p, j: p.get("personal.phone_type", "Mobile"), 45, None),
    # (not "Phone Number (including country code)": that's the number, written with its code)
    ("phone_code", r"^(?!.*\b(number|no|incl|including|with)\b).*\b(country|phone|dial(ing)?) (phone )?code\b", _phone_code, 45, None),
    ("phone_ext", r"extension", lambda p, j: None, 45, None),
    ("phone", r"phone|mobile|cell|telephone", _phone, 45, {"text", "combobox"}),
    ("address2", r"address line 2|^address 2|apartment|suite|^apt|^unit", _p("personal.address.line2"), 45, None),
    ("address1", r"address line 1|^address 1|^street|^(home |mailing |street )?address$", _p("personal.address.line1"), 45, None),
    ("city", r"^city|town|location city|current city|city of residence", _p("personal.address.city"), 45, None),
    # Oracle's "Zip Code+4" wants the 4-digit extension, not the ZIP: left for the site to fill
    ("postal_ext", r"zip( code)? ?(\+|plus) ?4|^zip ?4$|zip (code )?extension", lambda p, j: None, 45, None),
    ("postal", r"zip|postal|post code|postcode", _p("personal.address.postal_code"), 45, None),
    ("county", r"^county", lambda p, j: county_of(p), 45, None),
    # (not "State your desired salary", "State ID Number" or "Statement of accuracy")
    ("state", r"^state\b(?! (your|id|identification|licen[cs]e|the|any|why|how|what|whether|if|briefly)\b)|province|^region",
     _state, 45, None),
    # the address's country: not "Country of citizenship / birth" (those are other questions)
    ("country", r"^country\b(?!.*\b(citizen|birth|born|nationalit|passport|origin)\w*)", _p("personal.address.country"), 45, None),
    ("linkedin", r"linked ?in", _p("personal.linkedin_url"), 60, {"text", "textarea"}),
    ("github", r"github", _p("personal.github_url"), 45, {"text"}),
    ("website", r"website|portfolio|personal (site|url)|^url$|blog", _p("personal.website"), 45, {"text"}),
    # "Are you currently employed?" (only that: "...by Intel?" asks about one employer): from the work history
    ("employed_now", r"^(are you )?(currently|presently) employed$", _employed_now, 40, None),
    ("current_company", r"(current|most recent|present) (employer|company)", _p("experience.current_company"), 60, None),
    ("current_title", r"(current|most recent|present) (job )?(title|position|role)", _p("experience.current_title"), 60, None),
    ("total_years", r"^(total )?years of (professional |work )?experience$", _p("experience.total_years"), 60, None),
    ("degree", r"highest (level of )?(education|degree)|^degree$|education level", _edu("highest_degree", "degree"), 80, None),
    ("school", r"^(school|university|college|institution)\b(?!.*\b(major|degree|gpa|city|state|location|country|year|date|address|"
     r"zip|postal|phone|e ?mail|fax|code)\b)",
     _edu("school", "school"), 45, None),
    ("major", r"^(major|field of study|discipline|area of study)", _edu("major", "major"), 45, None),
    ("gpa", r"^gpa|grade point", _edu("gpa", "gpa"), 45, None),
    # "What is your expected graduation date:" (American Express): one done with school isn't attending
    ("expected_grad", r"(expected|anticipated|projected) (graduation|completion)|graduation date.{0,20}(expected|anticipated)",
     _expected_graduation, 80, None),
    ("grad_year", r"graduation (year|date)|year of graduation", _graduated, 60, None),
    ("signature", r"(electronic |e )?signature|sign your (full )?name", _full_name, 80, {"text"}),
    # questions (any length)
    # the same facts asked the other way round come first
    ("under_18", r"(under|less than|below|younger than) (the age of )?(18|eighteen)", _UNDER_18, None, None),
    ("over_18", r"(18|eighteen) years|at least 18|over the age of (18|eighteen)\b|age of 18|legal age", _OVER_18, None, None),
    # Yes means no sponsorship: "…without sponsorship?", "…and do not require sponsorship?"
    # (only filler words between the "no" and the sponsorship: "Please answer yes or no: will
    # you require sponsorship?" asks the other way)
    ("no_sponsorship", r"\b(no|not|never|without|free of)( (any|visa|employment|immigration|employer|the|a|in|need|needs|of|"
     r"for|requiring|requirement|h ?1 ?b|work))* sponsor|(do not|don t|dont|will not|won t|not|never) (now or in the future |"
     r"currently |ever )?(require|need)\w* .{0,40}sponsor|sponsor\w* (is |are |would be |will be )?(not|never) (be )?"
     r"(required|needed|necessary)", _no_sponsorship, None, None),
    ("sponsorship", r"(require|need|seek)\w* .{0,40}sponsor|sponsor\w* .{0,40}(require|need)|"
     r"^(visa |employment |immigration )?sponsorship( status| required| needed)?$", _sponsorship, None, None),
    ("authorized", _AUTHORIZED, _authorized, None, None),
    # Only questions that ask whether you are a U.S. person: export-control wording
    # also comes with other questions, e.g. Micron's "are you a citizen of Cuba, Iran ...?"
    ("us_person", r"^(?!.*\b(other than|another country|any (other )?country|foreign)\b).*"
     r"(\bu ?s person\b|citizen.{0,80}(permanent resident|green card|refugee|asyl|protected individual))",
     _yn("work_authorization.us_person"), None, None),
    ("us_citizen", r"are you a (u s|united states) citizen\b|are you a citizen of the (u s|united states)( of america)?$",
     _yn("work_authorization.us_citizen"), None, None),
    # TI: "Do you currently hold an H, L, E, J, or F nonimmigrant visa?" A citizen holds none
    ("visa_holder", r"\b(hold|have) an? .{0,40}non ?immigrant visa",
     lambda p, j: "No" if p.get("work_authorization.us_citizen") is True else None, None, None),
    ("citizenship", r"citizenship|country of citizen|are you a (u ?s )?citizen", _p("work_authorization.citizenship"), None, None),
    ("clearance", r"security clearance|active clearance", _p("work_authorization.security_clearance"), None, None),
    # "do you live nearby or are you willing to relocate?": a local applicant isn't relocating
    ("local_or_relocate", r"(located|live|living|reside|residing|based|commut).{0,60}relocat|relocat.{0,60}(located|live|living|reside|residing|commut)",
     _local_or_relocate, None, None),
    # relocation money, or a role with none: not whether the person would move
    ("relocation_help", r"relocat\w* (assistance|package|benefit|reimburse|support|expense|allowance)|no relocat|"
     r"(assistance|help) .{0,20}relocat", lambda p, j: None, None, None),
    ("relocate", r"relocat", _relocate, None, None),
    # "Do you currently live in Arizona?" / "...in the Phoenix area?": the profile's address
    ("lives_in", r"^(do|are) you (currently |presently )?(live|living|reside|residing|located|based) in ", _lives_in, 100, None),
    ("travel", r"travel", _travel, None, None),
    ("shift", r"shift work|rotating shift|nights and weekends|work (nights|weekends)|on ?call", _yn("preferences.flexible_schedule"), None, None),
    # desired_salary is one yearly figure: not an answer to "current salary" or a monthly/hourly rate
    ("other_salary", r"(current|last|previous|present|most recent|drawn) .{0,25}(salary|compensation|pay\b)"
     r"|(monthly|per month|hourly|per hour|weekly|per week) .{0,25}(salary|compensation|pay\b|rate)", lambda p, j: None, None, None),
    # American Express: "Indicate your highest level of preference by work location:" among its offices
    ("preferred_location", r"^(preferred|desired) (work )?(location|site|city)s?\b|location preference|"
     r"preference (by|for) (work )?location|(which|what) (work )?location (would|do) you prefer",
     lambda p, j: next(iter(_listed(p.get("preferences.locations"))), None), 120, None),
    ("salary", r"salary|compensation|pay (expectation|requirement)|desired pay|expected pay", _p("preferences.desired_salary"), None, None),
    ("start_date", r"start date|available to start|earliest (date|start)|when can you start|notice period", _p("preferences.earliest_start"), None, None),
    ("previous_employee", r"(previously|ever|formerly) (been )?(employed|worked)|former employee|have you (ever )?worked (for|at)|worked .{0,40} before|"
     r"have you (ever )?been hired\b",
     _previously_employed, None, None),  # (only about this employer: see _previously_employed)
    # voluntary self-identification
    ("sexual_orientation", r"sexual orientation", _p("eeo.sexual_orientation"), None, None),
    ("gender", r"\bgender\b|\bsex\b", _p("eeo.gender"), None, None),
    ("hispanic", r"hispanic|latin[oa]", _p("eeo.hispanic_latino"), None, None),
    ("race", r"\brace\b|ethnicity|ethnic", _p("eeo.race"), None, None),
    ("veteran", r"veteran", _veteran, None, None),
    # self-identification only: "Can you perform the essential functions … with or without
    # accommodation?" and "Do you need an accommodation?" are other questions
    ("disability", r"^(?!.*\b(essential function|accommodat|perform|able to)).*disabilit", _p("eeo.disability"), None, None),
    ("pronouns", r"pronoun", _p("personal.pronouns"), None, None),
]


# Questions about the employer asking them: an answer given to one company's isn't another's
_EMPLOYER_SPECIFIC = re.compile(
    r"\b(this|our|the) (company|organi[sz]ation|employer|firm)\b|\bwork(ing)? (here|for us|with us)\b|"
    r"\bjoin(ing)? (us|our)\b|\b(employed|worked|work) (by|for|at|with)\b|\bpreviously (been )?(employed|worked)|"
    r"\bcurrently employed\b|\bwhy (do|would|are) you\b|\binterest(ed)? in (this|our|the)\b|\brelatives?\b|"
    r"\bfamily members?\b|\bsubsidiar|\baffiliate|\bemployees?\b|\b(previously|ever|already) applied\b|"
    r"\bworks? here\b|\breferr(ed|al)\b|\bformer(ly)?\b|\balumni\b|\bcontractor (for|with|at)\b|"
    r"\bthis (opportunity|role|position|job)\b|\bcover letter\b|(?-i:\b(us|our|Our)\b)", re.I)


def _bare_question(text: str) -> str:
    return " ".join(re.sub(r"\(required\)|\*", " ", text or "", flags=re.I).strip(" :?.").split()).lower()


def _answer_bank(prof: Profile, label: str, job: dict | None = None) -> Answer | None:
    company = norm((job or {}).get("company"))
    for item in _listed(prof.get("answers")):
        if not isinstance(item, dict) or not item.get("match") or item.get("answer") in (None, ""):
            continue
        if item.get("question"):
            # answered in the Job Desk: that question, worded the same. "I agree" isn't "I agree
            # to binding arbitration", and "Are you currently employed?" isn't "... by ASML?"
            if _bare_question(str(item["question"])) != _bare_question(label):
                continue
            source = norm(str(item.get("from") or ""))
            if source and source != company and (_EMPLOYER_SPECIFIC.search(label) or source in norm(label)
                                                 or source.split()[0] in norm(item.get("answer")).split()):
                continue  # about the company it was given for, or naming it
            return Answer(item.get("answer"), f"answers[{item['match']}]")
        try:
            if re.search(str(item["match"]), label, re.I):
                return Answer(item.get("answer"), f"answers[{item['match']}]")
        except re.error:
            if str(item["match"]).lower() in label.lower():
                return Answer(item.get("answer"), f"answers[{item['match']}]")
    return None


def tailored_document(job: dict, kind: str) -> str | None:
    """A resume or cover letter made for this job, in its folder."""
    folder = Path(job["folder"]) if job.get("folder") else None
    stem = "cover_letter" if kind == "cover_letter" else "resume"
    if folder and folder.exists():
        for ext in (".pdf", ".docx", ".doc"):
            # resume.pdf, Sam_Rivera_Resume.pdf, Sam_Rivera_Cover_Letter.pdf ...
            # one render_document found too long (its .too-long marker) isn't sent: the default is
            hits = sorted(p for p in folder.glob(f"*{ext}") if p.stem.lower().endswith(stem)
                          and not p.with_suffix(".too-long").exists())
            if hits:
                return str(hits[0])
    return None


def _document(prof: Profile, job: dict, kind: str) -> str | None:
    """Prefer a tailored file in the job's folder, then the profile default."""
    tailored = tailored_document(job, kind)
    if tailored:
        return tailored
    p = expand(prof.get(f"documents.{kind}"))
    return str(p) if p and p.exists() else None


_CHOICE_KINDS = {"select", "listbox", "combobox", "radio_group", "checkbox_group", "checkbox"}
SKIP = "__skip__"  # deliberately left empty, e.g. the end date of a current job
# Legal attestations answered only when the exact choices are known.
_NEEDS_OPTIONS = {"us_person", "citizenship", "us_citizen", "clearance"}
# Questions the profile can't answer honestly (ties to sanctioned countries, dual
# citizenship, export licences). Only the user's own answers bank may answer them.
_NEVER_GUESS = re.compile(r"\b(cuba|iran|north korea|syria|crimea|ofac)\b|\bsanction|\bembargo|denied part"
                          r"|dual (citizen|national)|export licen[cs]")

_ENTRY_SECTIONS = [
    ("education_history", r"education|school|degree"),
    ("work_history", r"experience|employment|work history|position|job"),
]
_WORK_FIELDS = [
    (r"phone|e ?mail|fax|address|zip|postal|supervisor|manager|reference|salary|wage|pay\b|reason", "skip"),
    (r"currently work|current(ly)? (employed|job|position|role)|i work here|present (job|position|employer)", "current"),
    (r"title|^position|^role$", "title"),
    (r"company|employer|organi[sz]ation", "company"),
    (r"location|city", "location"),
    (r"description|responsibilit|duties|summary|achievements", "description"),
    (r"^from|start", "start"),
    (r"^to$|^to |end|until", "end"),
]
_EDUCATION_FIELDS = [
    (r"school|university|college|institution", "school"),
    (r"degree|qualification", "degree"),
    (r"field of study|major|discipline|area of study|concentration", "major"),
    (r"gpa|overall result|grade", "gpa"),
    (r"^from|start", "start"),
    (r"^to$|^to |end|graduat|actual or expected", "end"),
]
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def is_present(value: Any) -> bool:
    return str(value or "").strip().lower() in {"present", "current", "now", "ongoing", "today"}


def parse_month_year(value: Any) -> tuple[str | None, str | None]:
    """'2021-03', '03/2021', 'Mar 2021', 2021 -> ('03', '2021'); month may be None."""
    s = str(value or "").strip().lower()
    if m := re.match(r"^(\d{4})[-/.](\d{1,2})\b", s):
        return f"{int(m.group(2)):02d}", m.group(1)
    if m := re.match(r"^(\d{1,2})[-/.](\d{4})$", s):
        return f"{int(m.group(1)):02d}", m.group(2)
    if m := re.match(r"^([a-z]{3})[a-z]*\.? (\d{4})$", s):
        month = _MONTHS.get(m.group(1))
        return (f"{month:02d}" if month else None), m.group(2)
    if m := re.match(r"^(\d{4})$", s):
        return None, m.group(1)
    return None, None


def _date_value(field: dict, value: Any) -> str | None:
    month, year = parse_month_year(value)
    if not year:
        return None
    sub = norm(field.get("sublabel"))
    if not sub:  # "Start date year", "End date month"
        label = norm(clean_label(field.get("label") or ""))
        sub = "year" if re.search(r"\byear\b", label) else "month" if re.search(r"\bmonth\b", label) else ""
    if sub == "month":
        return month
    if sub == "year":
        return year
    if sub == "day":
        return None
    if field.get("input_type") == "month":
        return f"{year}-{month or '01'}"
    if field.get("input_type") == "date":
        return f"{year}-{month or '01'}-01"
    return f"{month}/{year}" if month else year


def profile_entries(prof: Profile, key: str) -> list[dict]:
    entries = [e for e in _listed(prof.get(key)) if isinstance(e, dict)]
    if not entries and key == "education_history" and prof.get("education.school"):
        entries = [{
            "school": prof.get("education.school"), "degree": prof.get("education.highest_degree"),
            "major": prof.get("education.major"), "gpa": prof.get("education.gpa"),
            "end": prof.get("education.graduation_year"),
        }]
    return entries


def _resolve_entry(field: dict, prof: Profile) -> tuple[bool, Answer | None]:
    """(handled, answer) for fields inside a numbered block like "Work Experience 2"."""
    section = norm(field.get("section"))
    m = re.search(r"(\d+)$", section)
    if not m:
        return False, None
    for key, pattern in _ENTRY_SECTIONS:
        if re.search(pattern, section):
            break
    else:
        return False, None
    n = int(m.group(1))
    entries = profile_entries(prof, key)
    if not 0 < n <= len(entries):
        return True, None
    entry = entries[n - 1]
    label = norm(clean_label(field.get("label") or ""))
    for pattern, attr in _WORK_FIELDS if key == "work_history" else _EDUCATION_FIELDS:
        if re.search(pattern, label):
            break
    else:
        return True, None  # e.g. "Supervisor phone": not the applicant's own details
    rule = f"{key}[{n}].{attr}"
    current = is_present(entry.get("end")) or entry.get("current") is True
    if attr == "skip":
        return True, None  # e.g. "Employer Phone": not something the profile holds
    if attr == "current":
        return True, Answer(current, rule)
    if attr == "end" and key == "education_history" and "graduat" in label and not finished_degree(entry.get("degree")):
        return True, None  # a school not finished: no graduation date to give
    if attr == "end" and current:
        return True, Answer(SKIP, rule)
    if attr in ("start", "end"):
        value = _date_value(field, entry.get(attr))
        return True, (Answer(value, rule) if value else None)
    value = entry.get(attr)
    return True, (Answer(str(value).strip(), rule) if value not in (None, "") else None)


# A note from the employer inside a question ("Note: this position is not eligible for visa
# sponsorship.", "(We are unable to sponsor visas for this role.)"): not part of what's asked
_EMPLOYER_NOTE = re.compile(
    r"\b(we|this (position|role|job|opportunity)|the (company|employer|position|role))\b.{0,60}"
    r"\b(not|unable|cannot|can t|can't|won t|won't|no)\b.{0,40}sponsor|not eligible for .{0,30}sponsor|unable to sponsor|"
    r"\bno (visa |employment )?sponsorship (is )?(available|offered|provided)", re.I)
# Work questions about other countries: the profile's work facts are about the United States
_OTHER_COUNTRIES = re.compile(
    r"\b(canada|mexico|united kingdom|uk|england|britain|ireland|germany|netherlands|france|belgium|italy|spain|"
    r"switzerland|austria|sweden|denmark|norway|finland|poland|czech|israel|india|china|japan|korea|taiwan|singapore|"
    r"malaysia|philippines|vietnam|thailand|australia|new zealand|brazil|costa rica|european union|eu)\b")
_WORK_RULES = {"no_sponsorship", "sponsorship", "authorized", "us_person", "us_citizen", "citizenship", "visa_holder"}
_OTHER_THAN = re.compile(r"\b(other than|another country|any (other )?country|foreign)\b")
# Documents other than a resume that a lone file box may ask for
_OTHER_DOCUMENT = re.compile(r"\b(degree|diploma|transcripts?|certificat\w*|licen[cs]e|passport|portfolio|writing sample|"
                             r"work sample|recommendation|references?|identification|photo\w*|dd ?214)\b")
# A date box asking when something else happened, not the date a form is signed
_OTHER_EVENT = re.compile(r"\b(conviction|criminal|offen[cs]e|incident|violation|accident|military|service|discharge|"
                          r"availability|available|employment|education|experience|history|birth|licen[cs]e|certif\w*)\b")


def _without_notes(label: str) -> str:
    """The question without the employer's notes in it, which say what the employer offers."""
    parts = re.split(r"(?<=[.?!])\s+|[()]", label or "")
    kept = [p for p in parts if p.strip() and not _EMPLOYER_NOTE.search(p)]
    return " ".join(kept) if len(kept) < len([p for p in parts if p.strip()]) else label


def resolve_field(field: dict, prof: Profile, job: dict | None = None, file_inputs_on_page: int = 1) -> Answer | None:
    job = job or {}
    kind = field.get("kind", "text")
    raw_label = _without_notes(field.get("label") or field.get("name") or "")
    label = norm(clean_label(raw_label))
    if kind == "password":
        return None

    handled, ans = _resolve_entry(field, prof)
    if handled:
        if ans is None or ans.value == SKIP or kind == "checkbox":
            return ans
        if isinstance(ans.value, bool):
            ans.value = "Yes" if ans.value else "No"
        if kind in {"select", "radio_group", "listbox", "checkbox_group", "combobox"} and field.get("options"):
            chosen = choose_option(ans.value, field["options"], names=is_name_rule(ans.rule))
            if chosen is None and field.get("search"):
                return ans  # a search prompt (Workday's School) lists only some: the fill searches it
            return Answer(chosen, ans.rule) if chosen else None
        return ans

    if kind == "file":
        where = f"{label} {norm(field.get('section'))}"
        # A box that reads the resume to fill the form in ("Import your profile from resume" on
        # TI's and onsemi's Oracle), beside the one that attaches it: the site would refill the
        # form from its own reading while the desk fills it, so only the attachment gets it
        if file_inputs_on_page > 1 and re.search(r"\b(import|autofill|auto fill|parse|populate)\b", where):
            return Answer(SKIP, "documents.resume")
        if re.search(r"cover", where):
            path = _document(prof, job, "cover_letter")
            return Answer(path, "documents.cover_letter") if path else None
        named = re.search(r"resume|cv|curriculum", where)
        if named or file_inputs_on_page == 1 and not _OTHER_DOCUMENT.search(where):
            path = _document(prof, job, "resume")
            return Answer(path, "documents.resume") if path else None
        return None

    if kind == "checkbox":
        # Single checkboxes are usually consents/attestations: leave for a person,
        # except ones the user pre-approved in the answer bank.
        ans = _answer_bank(prof, raw_label, job)
        if ans is not None:
            pol = polarity(ans.value)
            return Answer(pol if pol is not None else bool(ans.value), ans.rule)
        return None

    ans = _answer_bank(prof, raw_label, job)
    if ans is None and _NEVER_GUESS.search(label):
        return None
    section = norm(field.get("section"))
    if ans is None and kind == "text" and len(label) <= 45 and _SIGNED_DATE.search(label) \
            and not (label == "date" and _OTHER_EVENT.search(section)):
        return Answer(_today_for(field), "signed_date")
    someone_else = _SOMEONE_ELSE.search(f"{label} {section}") or _OTHER_PARTY_LABEL.search(label) \
        or _OTHER_PARTY_SECTION.search(section)
    if ans is None:
        for name, pattern, getter, max_len, kinds in RULES:
            if someone_else and name in _CONTACT_RULES:
                continue  # a reference's or an emergency contact's name, phone or email
            if name in _EEO_RULES and _FAMILY.search(label):
                continue  # "Are you the spouse of a veteran?", "Gender of your spouse": not the person's own
            if max_len is not None and len(label) > max_len:
                continue
            if kinds is not None and kind not in kinds:
                continue
            if re.search(pattern, label):
                if name in _WORK_RULES and (_OTHER_COUNTRIES.search(label) or _OTHER_THAN.search(label)):
                    return None  # another country's question: the profile's facts are the United States'
                value = getter(prof, job, raw_label) if getter in _READS_QUESTION else getter(prof, job)  # type: ignore[call-arg]  # these take the question too
                if value is None or value == "":
                    return None  # recognised but the profile has no answer
                ans = Answer(value, name)
                break
    if ans is None and kind in _CHOICE_KINDS and field.get("options") and _INSTRUCTION_ONLY.match(label) \
            and _POINTS_AT_CHOICES.search(label):
        ans = _topic_from_options(field["options"], prof, job)
    if ans is None or ans.value is None or ans.value == "":
        return None

    if kind == "textarea" and not ans.rule.startswith("answers[") and ans.rule not in {"linkedin", "salary", "start_date"}:
        return None
    if isinstance(ans.value, bool):
        ans.value = "Yes" if ans.value else "No"

    options = field.get("options")
    if kind in {"select", "radio_group", "listbox", "checkbox_group", "combobox"} and options:
        # a place lookup's entries: the one in the rest of the profile's address ("Chandler,
        # Maricopa, AZ", not "Chandler, Henderson, TX" listed before it)
        chosen = (ans.rule in _PLACE_RULES and choose_place(ans.value, options, place_words(ans.rule, prof))
                  or choose_option(ans.value, options, names=is_name_rule(ans.rule)))
        if chosen is None and ans.rule == "how_heard":
            chosen = _own_website(ans.value, options, job)
        # a search prompt lists only its top level, and a full page of a paged list (Qorvo's
        # countries stop at Iran) only its start: the fill searches them for the answer. A
        # shorter paged list is all there is, so a search can't find anything else in it.
        # A long list of places (Oracle's City on Mayo Clinic's form opened at "Aaron, Clinton, KY",
        # its search flag unset) is a slice of them too: the fill searches it
        searched = (field.get("search") or field.get("paged") and len(options) >= PAGED_LIST_PAGE
                    or ans.rule in _PLACE_RULES and len(options) > PLACE_LIST_SLICE)
        if chosen is None and searched and not isinstance(ans.value, (list, dict)):
            return ans
        if chosen is None:
            return None
        return Answer(chosen, ans.rule)
    if kind == "combobox" and ans.rule in _NEEDS_OPTIONS:
        return None  # an attestation we won't answer without seeing the exact choices
    return ans


# Questions whose label may not say what they ask, though their choices do
_OPTION_TOPICS = ("veteran", "disability")
# A label that only says how to answer, pointing at the choices ("Please check one of the
# boxes below:"): a question that names its subject ("Do you require an accommodation?") is
# never read from its choices, even when they mention a disability, nor is a box with no
# question of its own ("", "Select an option"), whose question the page may show elsewhere
_INSTRUCTION_ONLY = re.compile(
    r"^(?:please )?(?:check|select|choose|tick|mark|pick)(?: (?:one|any|all|only|of|the|a|an|following|box|"
    r"boxes|option|options|answer|answers|response|below|that|which|apply|applies|appropriate))*$")
_POINTS_AT_CHOICES = re.compile(r"\b(?:box|boxes|below|following)\b")


def _topic_from_options(options: list[str], prof: Profile, job: dict) -> Answer | None:
    """"Please check one of the boxes below:" on Workday's disability form: its choices ("Yes,
    I have a disability…", "No, I do not have a disability…") say what it asks."""
    for name, pattern, getter, _, _ in RULES:
        if name in _OPTION_TOPICS and sum(bool(re.search(pattern, norm(o))) for o in options) >= 2:
            return Answer(getter(prof, job), name)
    return None


_PLACE_RULES = {"address1", "postal", "city", "state", "county"}

# Arizona's cities by county, for a form's County (Oracle's address block requires one) when the
# profile names its city but not its county. A city split between counties counts where most of
# it is (Peoria and Queen Creek in Maricopa, Apache Junction in Pinal); one split evenly (Sedona)
# isn't listed, and the person is asked.
_AZ_COUNTIES = {
    "Maricopa": {"phoenix", "chandler", "tempe", "mesa", "scottsdale", "gilbert", "glendale", "peoria", "goodyear",
                 "surprise", "avondale", "buckeye", "tolleson", "laveen", "cave creek", "carefree", "fountain hills",
                 "el mirage", "litchfield park", "sun city", "sun city west", "paradise valley", "anthem",
                 "ahwatukee", "youngtown", "waddell", "new river", "queen creek", "sun lakes", "wickenburg"},
    "Pinal": {"san tan valley", "casa grande", "maricopa", "apache junction", "gold canyon", "florence", "coolidge",
              "eloy"},
    "Pima": {"tucson", "oro valley", "marana", "sahuarita", "vail", "green valley"},
    "Yavapai": {"prescott", "prescott valley", "cottonwood", "chino valley", "camp verde"},
    "Coconino": {"flagstaff", "page", "williams"},
    "Yuma": {"yuma", "san luis", "somerton"},
    "Mohave": {"lake havasu city", "kingman", "bullhead city"},
    "Cochise": {"sierra vista", "douglas", "bisbee", "benson"},
}


def county_of(prof: Profile) -> str | None:
    """The profile's county, or for an Arizona address the county its city is in."""
    named = str(prof.get("personal.address.county") or "").strip()
    if named:
        return named
    if norm(prof.get("personal.address.state")) not in ("az", "arizona"):
        return None
    return _az_county(norm(prof.get("personal.address.city")))


# Metro areas by county: Phoenix's takes in Pinal County's towns (San Tan Valley, Maricopa)
_SAME_METRO = {"Pinal": "Maricopa"}
_ICIMS_PLACE = re.compile(r"^\s*usa?\s*-\s*([a-z]{2})\s*-\s*(.+)$", re.I)  # "US-AZ-Chandler"


def _az_county(city: str) -> str | None:
    return next((county for county, cities in _AZ_COUNTIES.items() if city in cities), None)


def _az_metro(city: str) -> str | None:
    county = _az_county(city)
    return _SAME_METRO.get(county, county) if county else None


def lives_near(prof: Profile, location: str) -> bool:
    """Whether an Arizona profile's city is in the metro area of a place the job names:
    "Tempe, AZ" or "Phoenix, Arizona, United States" for Chandler, not "Tucson, AZ"."""
    if norm(prof.get("personal.address.state")) not in ("az", "arizona"):
        return False
    home = _az_metro(norm(prof.get("personal.address.city")))
    if home is None:
        return False
    for place in re.split(r"[;|/\n]| or ", location):
        if m := _ICIMS_PLACE.match(place):
            state, town = m.group(1), m.group(2)
        else:
            town, _, rest = place.partition(",")
            state = (norm(rest).split() or [""])[0]
        if norm(state) in ("az", "arizona") and _az_metro(norm(town)) == home:
            return True
    return False


def place_words(rule: str, prof: Profile) -> list[str]:
    """For an address field: the rest of the profile's address, which tells apart the entries
    of a place lookup (Oracle's City lists "Chandler, Henderson, TX" before "Chandler,
    Maricopa, AZ"). Empty for any other field."""
    if rule not in _PLACE_RULES:
        return []
    words = [prof.get("personal.address.city"), prof.get("personal.address.postal_code"), county_of(prof)]
    state = str(prof.get("personal.address.state") or "").strip()
    if state.upper() in US_STATES:
        words += [state.upper(), US_STATES[state.upper()]]
    elif state:
        words += [state, *(code for code, name in US_STATES.items() if norm(name) == norm(state))]
    return [str(w) for w in words if w]


# A street as address lookups list it: "1234 East Some Road" is their "1234 E SOME RD"
_STREET_WORDS = {"north": "n", "south": "s", "east": "e", "west": "w", "northeast": "ne", "northwest": "nw",
                 "southeast": "se", "southwest": "sw", "street": "st", "road": "rd", "avenue": "ave", "drive": "dr",
                 "lane": "ln", "boulevard": "blvd", "court": "ct", "place": "pl", "parkway": "pkwy", "circle": "cir",
                 "highway": "hwy", "terrace": "ter", "trail": "trl", "apartment": "apt", "suite": "ste"}


def _place_part(s: Any) -> str:
    return " ".join(_STREET_WORDS.get(w, w) for w in norm(s).split())


def choose_place(value: Any, options: list[str], near: list[str]) -> str | None:
    """A place lookup's entry ("Chandler, Maricopa, AZ"; "85225, Chandler, Maricopa, AZ"): of
    those whose first part is the value (not "Chandler Heights, …"; a street matches with
    its words abbreviated, "1234 East Some Road" ~ "1234 E SOME RD"), the one naming most of
    the rest of the address. None when no entry starts with the value."""
    v = _place_part(value)
    first = [o for o in options if v and _place_part(o.split(",")[0]) == v]
    if not first:
        return None
    words = {norm(w) for w in near} - {"", v}
    # whole parts only: "Chandler Heights" is not "Chandler"
    return max(first, key=lambda o: len({norm(part) for part in o.split(",")} & words))  # the first, on a tie


def is_name_rule(rule: str) -> bool:
    """Answers that are names (a school, an employer), matched by name only."""
    return bool(re.search(r"\.(school|company|employer)$|^(school|current_company)$", rule or ""))


_WEBSITE = re.compile(r"\b(web ?site|careers? (site|page|portal)|company site)\b", re.I)


def _own_website(value: Any, options: list[str], job: dict) -> str | None:
    """"Company Website" (how the person heard of the job) is the employer's own site, which
    a form may call "ONTO Website" or "Careers Site"."""
    if not re.search(r"\b(company|employer|career|careers|corporate)\b.*\b(web ?site|site|page)\b", str(value), re.I):
        return None
    sites = [o for o in options if _WEBSITE.search(o)]
    if len(sites) > 1:  # "ONTO Website" over "Other Website": the one naming the employer
        employer = set(norm(job.get("company") or "").split()) - {"inc", "corp", "corporation", "co", "ltd", "llc"}
        sites = [o for o in sites if employer & set(norm(o).split())] or sites
    return sites[0] if len(sites) == 1 else None


_EDU_START = re.compile(r"^(school|university|college|institution)\b")
_EDU_FIELD = re.compile(r"^(school|university|college|institution|degree|discipline|major|field of study)\b")
# (not "Position Applied For", the job being applied to)
_JOB_FIELD = re.compile(r"^(company|employer|job title|title|position)\b(?! (applied|you are applying|of interest|desired|sought))")
_DATE_PART = re.compile(r"^(start|end|from|to)( date)?( (year|month))?$")


def _with_context(fields: list[dict]) -> list[dict]:
    """Greenhouse-style forms put School, Degree, Discipline and "Start date year" together
    with no section heading: they're one school's, the first in the profile's education
    history, so its school is never given another school's degree. Unsectioned dates after
    a job's boxes are that job's."""
    out, block, school = [], None, False
    for f in fields:
        label = norm(clean_label(f.get("label") or ""))
        if not f.get("section"):
            if _EDU_START.match(label):
                block, school = "Education 1", True
                f = {**f, "section": block}
            elif _EDU_FIELD.match(label):
                if block == "Education 1" and school:
                    f = {**f, "section": block}
                else:  # a Degree with no School before it is the person's highest; its dates a school's
                    block, school = "Education 1", False
            elif _JOB_FIELD.match(label):
                school = False
                block = "Work Experience 1"
            elif block and _DATE_PART.match(label):
                f = {**f, "section": block}
        out.append(f)
    return out


def plan_autofill(fields: list[dict], prof: Profile, job: dict | None = None, overwrite: bool = False) -> dict[str, Any]:
    """Split fields into ones we can fill and ones that need a decision."""
    fields = _with_context(fields)
    file_inputs = sum(1 for f in fields if f.get("kind") == "file")
    to_fill: list[dict] = []
    needs_input: list[dict] = []
    already: list[str] = []
    for f in fields:
        # a read-only text box can't be typed in, but a read-only dropdown is a pick-only
        # menu (Infineon's "Preferred location"): it still takes a choice
        if f.get("disabled") or f.get("readonly") and f.get("kind") not in _CHOICE_KINDS:
            continue
        has_value = not is_empty_value(f.get("value"))
        if has_value and not overwrite:
            already.append(f["id"])
            continue
        ans = resolve_field(f, prof, job, file_inputs)
        if ans is not None and ans.value == SKIP:
            continue
        if ans is not None:
            to_fill.append({"id": f["id"], "label": f.get("label", ""), "value": ans.value, "rule": ans.rule})
        elif f.get("kind") != "password":
            needs_input.append(
                {k: f[k] for k in ("id", "kind", "label", "section", "sublabel", "required", "options")
                 if k in f and f[k] not in (None, [])}
            )
    needs_input.sort(key=lambda f: not f.get("required"))
    return {"to_fill": to_fill, "needs_input": needs_input, "already_filled": already}
