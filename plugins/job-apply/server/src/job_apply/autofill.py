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

from .config import Profile, expand, question_core

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
    # a phone's type: Kforce's Taleo lists "Cellular Phone" for a profile's "Mobile"
    {"mobile", "cell", "cellular", "mobile phone", "cell phone", "cellular phone"},
    {"home", "home phone", "landline"}, {"work", "work phone", "business phone", "office phone"},
    {"united states", "united states of america", "usa", "us", "u s", "u s a", "america"},
    {"united kingdom", "uk", "great britain", "england"},
    {"netherlands", "the netherlands", "holland"},
    {"south korea", "korea republic of", "republic of korea", "korea"},
    {"taiwan", "taiwan province of china", "chinese taipei"},
    # a U.S. citizen, as export-control lists word it (ASM's "Citizen or National of the United States")
    {"u s citizen", "us citizen", "united states citizen", "citizen of the united states",
     "citizen or national of the united states", "u s citizen or national", "us citizen or national",
     "united states citizen or national"},
]
# "-- Please Select --" may have a value of its own ("0"); iCIMS's dashes are long ("— Make a Selection —")
_PLACEHOLDER_VALUES = re.compile(
    r"^([-\u2013\u2014]+\s*)?((please )?(select|choose|pick)( one| an? (option|item|value|answer|response|state|country|"
    r"year|month))?( (state|country|year|month|from (the )?list|below))?( \.\.\.|\.\.\.|…)?|"
    r"|make a selection|none selected|no selection)(\s*[-\u2013\u2014]+)?$|"
    r"^([-\u2013\u2014]+|mm/dd/yyyy|mm/yyyy)$",  # "No Selection": SuccessFactors' empty dropdowns
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
# Declining to answer, as each site and person words it ("Decline to self-identify", "I don't
# wish to answer", "I don't want to answer", "I choose not to disclose", "Prefer not to say",
# "Not specified"), on norm()'s text. Only a refusal to answer: "I do not wish to relocate" is
# an answer.
_ANSWERING = (r"(answer|respond|reply|disclose|say|state|specify|share|declare|indicate|identify|self identify|"
              r"provide (this|that|it|my|an answer|a response|th[ie]se?)\b)")
_DECLINE = re.compile(
    r"\bdeclin(e|ed|es|ing)\b"
    rf"|\b(not|don t|dont) (wish|want|care|like) to (be )?{_ANSWERING}"
    rf"|\b(prefers?|preferred|chooses?|chose|elect|opt|wish|rather) not (to )?{_ANSWERING}"
    # (SuccessFactors' lists at Pfeiffer Vacuum, run 56: "Prefers not to answer", "Not willing to answer")
    rf"|\b(not|un)( )?willing to {_ANSWERING}$"  # (and no more: "Not willing to share salary history" is an answer)
    r"|^(not specified|unspecified|not disclosed|undisclosed|not declared|undeclared|no answer|no response)$")


def norm(s: Any) -> str:
    s = str(s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9+ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _without_aside(text: Any) -> str:
    """A choice's own words, without the country after them: Workday names each self-identification choice with the
    form's country, "Undeclared (United States of America)" (probe 48, Oct 13). Only a country: "Unspecified (X)" is
    an answer (the review's)."""
    said = str(text or "")
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", said, re.S)
    if m and (norm(m[2]) in _US_NAMES or _OTHER_COUNTRIES.fullmatch(norm(m[2]))):
        return m[1]
    return said


def is_decline(text: Any) -> bool:
    """A refusal to answer: a self-identification form's "I don't wish to answer", worded
    another way on every site."""
    return bool(_DECLINE.search(norm(text)) or _DECLINE.search(norm(_without_aside(text))))


def _only_declines(option: str) -> bool:
    """A choice that does nothing but decline: not onsemi's "I do not wish to Identify or I am
    not a Veteran", which is also the answer of someone who isn't a veteran."""
    return all(is_decline(part) for part in re.split(r"\bor\b", norm(_without_aside(option))) if part.strip())


def clean_label(label: str) -> str:
    # the form's marker, not the word in a question ("Sponsorship is not required for you?")
    label = re.sub(r"\(required\)|^\s*required\b[:\s]*|[\s:.-]*\brequired\s*[*:]?\s*$", " ", label or "", flags=re.I)
    label = label.replace("*", " ")
    label = re.sub(r"\(\s*(yes\s*/\s*no|y\s*/\s*n)\s*\)", " ", label, flags=re.I)  # "...license? (Yes/No)"
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
    ("doctorate", r"\b(ph ?d|doctor(ate|al)?|d ?phil|ed ?d)\b"),
    # (each level in the bare singular too, "Bachelor", as Workday lists write it: the review of 0.3.175)
    ("master", r"\b(master(?: s|s)?|ms|m s|ma|m a|msc|meng|m eng|mba|m b a|mfa)\b"),
    ("bachelor", r"\b(bachelor(?: s|s)?|bs|b s|ba|b a|bsc|beng|b eng|bse|bsee|bsme)\b"),
    ("associate", r"\b(associate(?: s|s)?|aas|a a s)\b"),
    ("high_school", r"\b(high school|hs|h s|ged)\b"),
]


def _containing(want: str, normed: list[tuple[str, str]]) -> str | None:
    """The one option that contains `want` as whole words, or that it contains."""
    contains = [o for o, n in normed if want and re.search(rf"(^| ){re.escape(want)}( |$)", n)]
    if len(contains) == 1:
        return contains[0]
    contained = [o for o, n in normed if n and re.search(rf"(^| ){re.escape(n)}( |$)", want)]
    return contained[0] if len(contained) == 1 and not contains else None


# A degree's kind within its level, each as written in full or abbreviated (on norm()'s text: "B.S." is "b s")
_DEGREE_KINDS = [
    ("bfa", r"bachelor of fine arts|\bb ?fa\b"), ("bba", r"bachelor of business administration|\bb ?ba\b"),
    ("mba", r"master of business administration|\bm ?ba\b"),
    ("aas", r"associate (degree )?of applied science|\ba ?a ?s\b"),
    ("bs", r"bachelor (degree )?of science|\bb ?sc?\b|\bbs[a-z]{1,3}\b"), ("ba", r"bachelor (degree )?of arts|\bb ?a\b"),
    ("ms", r"master (degree )?of science|\bm ?sc?\b|\bms[a-z]{1,3}\b"), ("ma", r"master (degree )?of arts|\bm ?a\b"),
    ("as", r"associate (degree )?of science|\ba ?s\b"), ("aa", r"associate (degree )?of arts|\ba ?a\b"),
]


def degree_kind(s: str) -> str | None:
    """A degree's kind (bs, ba, ms, mba ...) when its words name one; a bare "Bachelor's Degree" names none."""
    n = norm(s)
    return next((kind for kind, pattern in _DEGREE_KINDS if re.search(pattern, n)), None)


def degree_key(s: str) -> str | None:
    n = norm(s)
    for key, pattern in _DEGREES:
        if re.search(pattern, n):
            return key
    return None


# A diploma's equivalents (on norm()'s text)
_GED = re.compile(r"\b(ged|equivalen\w*|hiset|tasc|hsed)\b")


def _same_schooling(want: str, option: str) -> bool:
    """Within the high-school level, a GED (or another equivalent) and a diploma are different schooling: a GED
    matches a choice that names one ("GED", "High School Diploma or Equivalent") or the bare level ("High School",
    the level most lists mean, which a GED is the equivalent of), never a diploma; a diploma one that says high
    school or a diploma, not an equivalent alone ("High School Equivalent (GED)"). Both on norm()'s text."""
    if degree_key(want) != "high_school":
        return True
    if _GED.search(want):
        return bool(_GED.search(option) or re.search(r"\bhigh school\b(?! diploma)", option))
    return bool(re.search(r"\bdiploma\b|\bhigh school\b(?! equivalen)", option))


# Schooling begun but not finished: "Some High School", "Some College", "Associate's (in
# progress)", "coursework toward a BSEE"
_PARTIAL_STUDY = re.compile(r"\b(some|less than|incomplete|partial(ly)?|attended|attending|coursework|toward|towards|"
                            r"in progress|enrolled|pursuing|unfinished|did not|not completed|no degree|"
                            # (a degree still to come: "BS (expected 2027)", the safety audit of Oct 12)
                            r"expected|anticipated|graduating|candidate|pending)\b")
_STUDY_WORDS = re.compile(r"\b(high school|college|university|degree|diploma|ged|graduate|coursework)\b")


def _partial_study(n: str) -> bool:
    return bool(_PARTIAL_STUDY.search(n) and (_STUDY_WORDS.search(n) or degree_key(n)))


def _partial_level(n: str) -> str | None:
    """What a partial study is of: "Some college" and "College coursework, no degree" are
    college; "Some high school", high school; "Associate's (in progress)", an associate's."""
    if not _partial_study(n):
        return None
    # ("PS-Post Secondary-No Degree": Knight-Swift's)
    return degree_key(n) or ("college" if re.search(r"\b(college|university|coursework|credits?|post ?secondary)\b", n)
                             else None)


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
    for o, n in normed:  # the same words first, then an alias ("U.S. Citizen" before "U.S. Citizen or National")
        if n == want:
            return o
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
    # (a decline's "not" is no denial: declines are matched as such below). Nor a refusal to
    # answer for someone who answered: "I do not want to answer" reads as a "no", and took
    # a plain "No" away from "No, I do not have a disability"
    declining = is_decline(desired)
    if not declining:
        normed = [(o, n) for o, n in normed if not _contradicts(desired, o) and not _only_declines(o)]
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

    # 2. declines ("Decline to self-identify", "I don't wish to answer", ...): only ever another
    # decline, never a "No" ("I don't want to answer" isn't "I am not a protected veteran")
    if declining:
        hits = sorted((o for o, _ in normed if is_decline(o)), key=lambda o: not _only_declines(o))
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
        same = [(o, n) for o, n in normed if degree_key(n) == dk and _same_schooling(want, n)]
        if not same and any(degree_key(n) for _, n in normed):
            # a level the list lacks is never another level, nor a GED a diploma ("Bachelor's Degree" among Master's,
            # Doctorate and Other was Master's, by the words they share: the review of Oct 15): the person's to say
            return None
        if len(same) == 1:
            return same[0][0]
        # several of that level ("Bachelor of Arts", "Bachelor of Science"): the kind the answer names, however
        # each writes it ("Bachelor of Science" is "BS", "B.S." or "Bachelor's - BS"); none named, none picked
        kind = degree_kind(want)
        if kind and len(same) > 1:
            of_kind = [o for o, n in same if degree_kind(n) == kind]
            if len(of_kind) == 1:
                return of_kind[0]
        if not kind and len({degree_kind(n) for _, n in same} - {None}) > 1:
            return None  # a bare "Bachelor's Degree" among "Bachelor's - BS" and "Bachelor's - BA": the person's to say
        if len(same) > 1 and all(degree_kind(n) is None for _, n in same):
            # the level written twice, no kind named in either (ASM's "Bachelors" and "Bachelor's Degree", the second
            # nightly at every employer, Oct 12): the same answer, so the one that says it's a degree, else the first
            return next((o for o, n in same if "degree" in n.split()), same[0][0])
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


def _eeo_gender(prof: Profile, job: dict) -> Any:
    """The profile's gender, its one-letter forms spelled out ("M" is "Male")."""
    value = prof.get("eeo.gender")
    return {"m": "Male", "f": "Female"}.get(norm(value), value) if isinstance(value, str) else value


def _eeo_disability(prof: Profile, job: dict) -> Any:
    """The profile's disability answer, "None" read as no disability."""
    value = prof.get("eeo.disability")
    return "No" if isinstance(value, str) and norm(value) in ("none", "no disabilities") else value


def _city_state(prof: Profile, job: dict) -> Any:
    """"Where are you currently located?": the address's city and state ("Chandler, AZ")."""
    city, state = prof.get("personal.address.city"), prof.get("personal.address.state")
    return f"{city}, {state}" if city and state else None


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
                         # onsemi's "worked in any onsemi establishment" (never "worked in Arizona": a place)
                         rf"|\bwork\w* in (any |an? |the )?{first}( \w+)? (establishments?|facilit\w+|sites?|locations?|"
                         r"compan\w+|offices?|plants?|subsidiar\w+|affiliates?)\b"
                         rf"|\b{first}( \w+)? (employee|employment|intern|contractor)s?\b"
                         rf"|\bhired\b.{{0,60}}? (by|with|at) (the )?{first}\b", asked)
    if not (names_it or re.search(r"(employed|worked|hired) (by|for|at|with) (us|this|our|the company)\b|former employee", asked)):
        return None
    # A question naming another organization ("Are you a current or former employee of <an accounting
    # firm>?" on an employer's own form) isn't about working for this one: it's the person's to answer
    other = re.search(r"\b(employee|employed|employment|worked|work|intern|contractor)s? (\w+ )?(of|by|for|at|with) "
                      r"(?!(us|this|our|the (company|organization|firm|business)|any|an?|one)\b)\w", asked)
    if not names_it and other:
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
    return "Yes, currently" if works_there_now(prof, company) else "Yes, previously"


def works_there_now(prof: Profile, company: str) -> bool:
    """Does the profile have the person working for this employer now: a work_history entry that
    runs to the present, or experience.current_company?"""
    company = norm(company)
    entries = [e for e in _listed(prof.get("work_history")) if isinstance(e, dict)]
    now = [norm(e.get("company")) for e in entries if is_present(entry_dates(e)[1]) or e.get("current") is True]
    now.append(norm(prof.get("experience.current_company")))
    return bool(company) and any(_same_employer(c, company) for c in now if c)


def _said_no(prof: Profile, key: str) -> bool:
    v = prof.get(f"background.{key}")
    return v is False or isinstance(v, str) and norm(v) in ("no", "false", "none", "never")


# Another organization named as where something happened ("a partner at <a firm>", "employed by
# <a firm>"): not "us", "this company", any government or the Department of Defense
_ELSEWHERE = re.compile(r"\b(at|by|of|with|for) (?!(us|this|our|the (company|organization|firm|business|employer)|any|an?|"
                        r"one|these|those|the (u s|us|federal|state|local)|federal|state|local|government|department|dod|"
                        r"military|armed)\b)[a-z]")


def _background(key: str, *, unless: str | None = None) -> Getter:
    """A recurring yes/no question about the person's past or plans (background.<key>, which
    setup asks once). A "no" answers it however it's worded and whatever time it covers ("in
    the past 5 years", "on or after January 28, 2008"); a "yes" needs details only the person
    has, so the question is theirs. `unless`: words that make it another question."""
    def g(prof: Profile, job: dict, label: str = "") -> Any:
        asked = norm(label)
        if unless and re.search(unless, asked):
            return None
        return "No" if _said_no(prof, key) else None

    g.reads_question = True  # type: ignore[attr-defined]
    return g


def _relatives(prof: Profile, job: dict, label: str = "") -> Any:
    """"Do you have any friends or relatives currently employed by <the employer>?" Never one
    about another organization ("an immediate family member of a partner at <a firm>") or a
    government official."""
    asked = norm(label)
    company = norm(job.get("company"))
    first = company.split()[0] if company else ""
    if re.search(r"government|official|public office|politic", asked):
        return None
    named = _ELSEWHERE.search(asked)
    if named and not (first and re.search(rf"\b(at|by|of|with|for) (the )?{re.escape(first)}\b", asked)):
        return None
    return "No" if _said_no(prof, "relatives_at_employer") else None


def _government(prof: Profile, job: dict, label: str = "") -> Any:
    """A government employee now or lately (federal, state or local; the military counted in
    some): "No" only where the profile says neither."""
    asked = norm(label)
    if re.search(r"\b(military|armed forces|uniformed)\b", asked) and not _said_no(prof, "military"):
        return None
    return "No" if _said_no(prof, "government_employee") else None


def _plans_none(field: dict, prof: Profile) -> Any:
    """"If hired, do you intend to (select all that apply): keep another job or business / sit on
    a board / Neither": its "Neither" where the profile says no to both."""
    options = field.get("options") or []
    plans = [o for o in options if re.search(r"secondary|outside|second job|other (employment|business)|business activit|"
                                             r"moonlight|board of (directors|advisors)|governing body", norm(o))]
    neither = [o for o in options if re.match(r"^(neither|none)( of (the|these)( above)?)?\b", norm(o))]
    if not plans or len(neither) != 1 or len(plans) + 1 != len(options):
        return None
    return [neither[0]] if _said_no(prof, "outside_work") and _said_no(prof, "board_member") else None


# A choice that says the person isn't one of the employer's staff, and nothing more: "I am not a current Desert
# Financial employee" beside the Yes and No of "Have you notified your leader of your application? (internal candidates
# only)" (Desert Financial's UKG Pro, run 56), "I am an external candidate". Never one that says more ("...or former",
# "...and not related to any employee"), nor a new applicant's (whether they applied before isn't the profile's)
_NOT_STAFF = re.compile(r"^(?:(?:n a|not applicable|no) )?(?:i am |i m )?(?:not (?:a |an )?current (?P<who>(?:\w+ ){0,4}?)"
                        r"(?:employee|associate|team member|staff member)(?: (?:of|at|with) (?:the )?(?P<of>(?:\w+ ){0,5}\w+))?|"
                        r"(?:an )?external (?:candidate|applicant))$")
_SAYS_MORE = re.compile(r"\b(?:or|and|nor|former\w*|previous\w*|past|prior|ever|been|relat\w*|famil\w*|last|within)\b")
# a question only for the employer's own staff ("(internal candidates only)", "Current employees only: ...")
_STAFF_ONLY = re.compile(r"\binternal (?:candidates?|applicants?|employees?|only)\b|\bcurrent (?:employees?|associates?|"
                         r"team members?) only\b|\b(?:if|for) (?:you are )?(?:an? )?(?:current|internal) (?:employee|associate|"
                         r"team member|candidate|applicant)s?\b")


def _not_staff_choice(field: dict, prof: Profile, job: dict) -> str | None:
    """A question only for the employer's own staff (_STAFF_ONLY), answered with its one choice that says the person
    isn't on that staff and nothing more (_NOT_STAFF), naming no employer or this one alone, as a Create Account's
    "new candidate, not a current employee" box is ticked (pipeline._new_candidate_boxes): where the profile says
    where the person works and has never had this employer there (a job there with no end may be one they have now,
    and "former" is theirs to say)."""
    company = norm(job.get("company"))
    if not company or not _STAFF_ONLY.search(norm(field.get("label"))):
        return None
    entries = [e for e in _listed(prof.get("work_history")) if isinstance(e, dict)]
    if not (entries or prof.get("experience.current_company")):
        return None  # (a profile that says nothing of their work says nothing of this either)
    known = [norm(e.get("company")) for e in entries] + [norm(c) for c in _listed(prof.get("history.previous_employers"))]
    known.append(norm(prof.get("experience.current_company")))
    if any(_same_employer(c, company) for c in known if c):
        return None
    fits = []
    for o in field.get("options") or []:
        said = norm(o)
        m = _NOT_STAFF.fullmatch(said)
        if not m or _SAYS_MORE.search(said):
            continue
        named = [n.strip() for n in (m.group("who"), m.group("of")) if n and n.strip()]
        if all(_same_employer(n, company) for n in named):
            fits.append(o)
    return fits[0] if len(fits) == 1 else None


def _employee_id(prof: Profile, job: dict) -> str | None:
    """The person's ID at the employer applied to (history.employee_ids, by employer), for its
    "Employee ID (if applicable)" or "please provide your WWID": never another employer's."""
    company = norm(job.get("company"))
    ids = prof.get("history.employee_ids")
    if not company or not isinstance(ids, dict):
        return None
    return next((str(value).strip() for name, value in ids.items()
                 if value not in (None, "") and _same_employer(norm(str(name)), company)), None)


_UNFINISHED = re.compile(r"^(none|n ?a|no|not applicable)$|\b(no degree|not (completed|finished)|incomplete|"
                         r"unfinished|did not (complete|finish|graduate)|in progress|some|coursework|attended|"
                         r"expected|anticipated|graduating|candidate|pending)\b")


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


_AUTHORIZED = r"authori[sz]ed to ((legally|lawfully) )?work|^(do|does) you (currently )?(have|hold) (the )?(legal )?authori[sz]ation to work|eligible to ((legally|lawfully) )?work|eligibility to work|eligible for employment|legally (able|permitted|allowed) to work|right to work|" \
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


# what the sponsorship would be for, not a second question: "...require visa sponsorship...
# to maintain your work authorization?" (Grant Thornton), "...require sponsorship in order to have work authorization in the
# United States?" (APS's SuccessFactors, run 56)
_FOR_AUTHORIZATION = re.compile(r"\b((in order )?to (obtain|maintain|keep|retain|continue|extend)|in order to (have|get|gain|"
                                r"secure|receive))( your| my| the)?( current)?"
                                r"( work| employment)? (authori[sz]ation|eligibility)\b|\bfor (work |employment )?authori[sz]ation\b")
# and the other half of a question that also asks whether they can work now ("Are you able to work in the US now, or
# will you need sponsorship to obtain work authorization?"): a single Yes or No can't answer both (the review of 0.3.146)
# (never what the sponsorship is for: "...require sponsorship to be able to work in the United States?"; nor a visa they
# have now: "Do you currently have a visa that will require sponsorship?")
_ABLE_NOW = re.compile(r"(?<!\bto be )\b(able|permitted|allowed|eligible) to (legally |lawfully )?work\b|\b(currently|already) "
                       r"(have|hold|possess)( it| (the right to work|work authori[sz]ation|authori[sz]ation))?( or| and|$)")


def _sponsorship(prof: Profile, job: dict, label: str = "") -> Any:
    """Will the person need sponsorship? Not answered when the same question also asks whether
    they're authorized ("Are you authorized … and will you require sponsorship?"), which a
    single Yes or No can't answer both halves of."""
    rest = _FOR_AUTHORIZATION.sub(" ", norm(label))
    if re.search(_AUTHORIZED, rest) or _ABLE_NOW.search(rest):
        return None
    return _yn("work_authorization.requires_sponsorship")(prof, job)


def _age(invert: bool) -> Getter:
    """Over 18 (or, inverted, under). Not answered from age alone when the question also asks
    about working or sponsorship ("at least 18 and legally authorized to work…?")."""
    def g(prof: Profile, job: dict, label: str = "") -> Any:
        # (nor from a count of 18 months, hours, credits or units, or 18 years of experience or work: the review of Oct 15)
        if re.search(r"authori[sz]|sponsor|eligib|legally|permitted to work|able to work|right to work|"
                     r"\b(months?|hours?|credits?|units?|experience|work(ed|ing)?)\b", norm(label)):
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
    a veteran at all: "Are you a veteran?" is then theirs to answer, and a list's "I am not a veteran"
    isn't it. Unless the profile says they never served (background.military: false): then they're
    not a veteran, which every list's not-a-veteran or not-a-protected-veteran choice is (live test 2,
    issue #112, note 18: Workday's "I am not a veteran" list was left empty)."""
    v = prof.get("eeo.veteran")
    if not v:
        return None
    if (polarity(v) is False and re.search(r"\bprotected\b", norm(v)) and not is_decline(v)
            and prof.get("background.military") is False):
        return "I am not a veteran"
    asked = norm(label)
    if re.search(r"\bare you an? (u s |us |military )?veteran\b", asked) and "protected" not in asked \
            and polarity(v) is False and re.search(r"\bprotected\b", norm(v)):
        return None
    return v


_LEVELS = ["high_school", "associate", "bachelor", "master", "doctorate"]
# Education written the way setup writes it ("Bachelor's Degree", "Some college") or by a
# degree's full name: nothing an abbreviation could also be ("MA" is a Medical Assistant
# certificate too), nothing more ("Equivalent", "(expected 2027)")
_DEGREE_NAMES = [
    ("high_school", r"high school( diploma)?|ged|high school diploma or ged|ged or high school diploma"),
    ("associate", r"associate( s|s)?( degree)?|associate of (applied )?(science|arts)"),
    ("bachelor", r"bachelor( s|s)?( degree)?|bachelor of (science|arts|fine arts|business administration)"),
    ("master", r"master( s|s)?( degree)?|master of (science|arts|fine arts|business administration)"),
    ("doctorate", r"doctorate|ph d|doctor of philosophy"),
]


def _named_level(text: Any) -> int | None:
    """The rank in _LEVELS of a degree written as setup writes it, or by its full name."""
    n = norm(text)
    return next((_LEVELS.index(key) for key, pattern in _DEGREE_NAMES if re.fullmatch(pattern, n)), None)


# Partial study written the way setup writes it, and the most it lets the person have finished:
# only these, word for word ("Graduate coursework" or "Attended university" say other things)
_SOME_STUDY = [(-1, r"some high school"),
               (0, r"some college( no degree| coursework)?|college coursework( no degree)?")]


def _studied_level(text: Any) -> int | None:
    n = norm(text)
    return next((rank for rank, pattern in _SOME_STUDY if re.fullmatch(pattern, n)), None)


# A question asking for nothing but a level of education, maybe "or higher" (with degrees named
# after it: "(Master's degree, Ph.D., etc.)", Carvana's, Oct 2026, which _higher_examples reads):
# a field ("in Finance"), a kind of degree ("Bachelor of Science") or anything more ("from an
# accredited university") isn't one the profile can settle
_DEGREE_WORDS = "|".join(pattern[len(r"\b("):-len(r")\b")] for _, pattern in _DEGREES)
_DEGREE_NAME = rf"(?:an? )?(?:{_DEGREE_WORDS})(?: ?degrees?)?"  # ("master s?" takes a trailing space)
_DEGREE_ONLY = (r"^(?:do you (?:have|hold|possess) |have you (?:earned|completed|received) )?(?:an? |the )?"
                r"(?P<deg>high school diploma(?: or (?:a |an |its )?(?:ged|equivalent))?|ged|(?:associate|bachelor|master)(?: s|s)? degree|"
                r"doctorate(?: degree)?)(?P<higher> or (?:higher|above|greater)"
                rf"(?: (?:{_DEGREE_NAME}|e g|eg|i e|such as|including|etc|or|and))*)?(?: (?:required|preferred))*$")
_EXAMPLES_SPLIT = re.compile(r"[,/;()]|\b(?:or|and|e\.?\s?g|i\.?\s?e|such as|including|etc)\b\.?", re.I)


def _higher_examples(label: str, rank: int) -> bool:
    """The degrees a question names after "or higher" ("(Master's degree, Ph.D., etc.)") are
    examples of what's higher, so the question is the same without them, only when each is a
    degree above the level asked: "Master's or higher (MBA)" or "(Bachelor's)" asks something
    else, and so does one that isn't a degree at all ("(Bachelor's preferred)")."""
    m = re.search(r"\bor\s+(?:higher|above|greater)\b(.*)$", label, re.I | re.S)
    tail = m.group(1) if m else ""
    marker = re.search(r"\s*\b(?:required|preferred)\W*$", tail, re.I)
    if marker and tail[:marker.start()].count("(") == tail[:marker.start()].count(")"):
        tail = tail[:marker.start()]  # the question's own "required", not one inside the brackets
    for item in _EXAMPLES_SPLIT.split(tail):
        named = norm(item)
        if not named:
            continue
        key = degree_key(named)
        if not re.fullmatch(_DEGREE_NAME, named) or key is None or _LEVELS.index(key) <= rank:
            return False
    return True
# A field that says the degree isn't done ("Finance coursework", "Finance (expected 2027)", "ABD"), or
# isn't that degree ("Welding Certificate", "Juris Doctor", a diploma's equivalent: GED, HiSET, TASC, HSED)
_NOT_DONE_FIELD = re.compile(r"\b(expected|anticipated|pending|progress|ongoing|current\w*|pursuing|enrolled|incomplete|"
                             r"unfinished|not|some|coursework|withdr\w*|abd|candidate|graduating|toward\w*|partial\w*|"
                             r"hold|left|remaining|spring|summer|fall|winter)\b|'\d{2}\b")
# a diploma's equivalents, and degrees a field may show to be something other than the level written
_EQUIVALENT = r"ged|hiset|tasc|hsed|equivalen\w*|general educational development|general equivalency"
_NOT_THAT_DEGREE = re.compile(rf"\b(certificates?|juris|jd|md|pharm ?d|{_EQUIVALENT}|doctor of (medicine|pharmacy|dental|"
                              r"nursing|jurisprudence|osteopathic|veterinary|physical therapy|optometry|law))\b")


def _future_year(text: str) -> bool:
    """A year in a field still to come ("Finance 2027"): the degree isn't done."""
    return any(int(y) > date.today().year for y in re.findall(r"\b((?:19|20)\d{2})\b", text))


def _degrees_earned(prof: Profile) -> tuple[list[tuple[int, str]], bool]:
    """The degrees the person confirmed they finished (education.degrees_earned, which setup
    writes after asking about each), as (level rank, kind: "ged" for a GED, else ""), and
    whether anything there couldn't be read: a level not one of _LEVELS, "ged" or a degree as
    setup writes it; anything besides a level and a field (a status); a field saying the
    degree isn't done, or is something else (a certificate, a JD, a diploma's equivalent)."""
    raw = prof.get("education.degrees_earned")
    entries = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    out: list[tuple[int, str]] = []
    unread = raw is not None and not isinstance(raw, (list, dict))
    for d in entries:
        if not isinstance(d, dict) or set(d) - {"level", "field"}:
            unread = True
            continue
        written = str(d.get("field") or "").lower()
        field = norm(written)
        level = norm(d.get("level")).replace(" ", "_")
        if _NOT_DONE_FIELD.search(written) or _future_year(field) or level != "ged" and _NOT_THAT_DEGREE.search(field):
            unread = True
            continue
        if level == "ged":
            out.append((0, "ged"))
            continue
        rank = _LEVELS.index(level) if level in _LEVELS else _named_level(d.get("level"))
        if rank is None or level != "high_school" and "ged" in level:
            unread = True
            continue
        out.append((rank, ""))
    return out, unread


def _has_degree(prof: Profile, job: dict, label: str = "") -> Any:
    """"Do you have a Bachelor's degree?". Yes only from the degrees the person confirmed they
    finished (education.degrees_earned): one at that level (or above it, when the question says
    "or higher"); a GED answers for a GED, never for a diploma. No when the most the person has
    finished, by the highest education the profile states as setup writes it ("Some college",
    "Associate's Degree") and those degrees, is below the level asked, and no school it lists
    names a degree that isn't. Else the person's to say: free-text education can't tell a
    degree earned from one under way, and a field or condition is theirs to judge."""
    asked = clean_label(label)  # as the rule read it: without "(Required)" or "(Yes/No)"
    shape = re.match(_DEGREE_ONLY, norm(asked))
    level = degree_key(shape.group("deg")) if shape else None
    if not shape or level is None:
        return None
    rank = _LEVELS.index(level)
    if shape.group("higher") and not _higher_examples(asked, rank):
        return None
    earned, unread = _degrees_earned(prof)
    higher = bool(shape.group("higher"))
    # a high school diploma isn't a GED, nor a GED a diploma; "a high school diploma or GED" is either, and so is "a high
    # school diploma or equivalent" (Desert Financial's UKG Pro, run 56), but not "...or equivalent experience"
    deg = shape.group("deg")
    kinds = ({"", "ged"} if rank > 0 or "diploma" in deg and re.search(r"\b(ged|equivalent)$", deg)
             else {"ged"} if deg == "ged" else {""})
    if kinds == {""} and rank == 0 and re.fullmatch(rf"(a )?({_EQUIVALENT})( diploma| certificate)?",
                                                    norm(prof.get("education.highest_degree"))):
        return None  # a diploma asked of someone whose highest is a GED (or HiSET...): theirs to say
    if any((r == rank and kind in kinds) or r > rank and higher for r, kind in earned):
        return "Yes"
    if unread:
        return None  # a degree it couldn't read may be the one asked about
    history = prof.get("education_history")
    if history is not None and not isinstance(history, list):
        return None  # written some other way: not read
    highest = prof.get("education.highest_degree")
    stated = _named_level(highest)
    if stated is None:
        stated = _studied_level(highest)
    if stated is None:
        return None  # no highest education stated, or one it can't read ("MBA"): the confirmed degrees are only the least
    if max([stated, *(r for r, _ in earned)]) >= rank:
        return None
    for e in history or []:  # every school's degree is none (written so: setup's "" for one not finished), or below
        if not isinstance(e, dict) or "degree" not in e:
            return None
        degree = str(e.get("degree") or "").strip()
        listed = _named_level(degree)
        if degree and (listed is None or listed >= rank):
            return None
    return "No"


def _us_person(prof: Profile, job: dict) -> Any:
    """A U.S. person (a citizen, permanent resident, refugee or asylee): the profile's answer,
    or Yes for a U.S. citizen, who is one."""
    v = prof.get("work_authorization.us_person")
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return "Yes" if prof.get("work_authorization.us_citizen") is True else None


def _employed_now(prof: Profile, job: dict) -> Any:
    """Whether the person has a job now: a work history entry that hasn't ended. None when the
    profile lists no work at all."""
    entries = [e for e in _listed(prof.get("work_history")) if isinstance(e, dict)]
    if not entries:
        return None
    return "Yes" if any(is_present(entry_dates(e)[1]) or e.get("current") is True for e in entries) else "No"


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


_CODE_WORDS = r"(the |your )?((country|location|international|area) )?(dial(ing)?|calling|country|location) code"
# a box's label that names no more than a number (iCIMS's "Number"): its autocomplete says whose
_BARE_NUMBER = re.compile(r"(number|no)?")


def _phone(prof: Profile, job: dict, label: str = "") -> Any:
    """The phone number; with its dial code where the box asks for it ("including country code",
    "must include your location code"), not where it says not to, and as bare digits where it
    says so ("no spaces or dashes": EMD Electronics' "+1##########", Oct 2026)."""
    phone = prof.get("personal.phone")
    if not phone:
        return phone
    asked = norm(label)
    code = "+" + re.sub(r"\D", "", str(prof.get("personal.phone_country_code", "+1")))  # (YAML reads +1 as 1)
    if code == "+":
        code = "+1"
    without = re.search(rf"\b(do not|don t|dont|without|no|exclude|excluding) (include |including |incl |add |the )?"
                        rf"{_CODE_WORDS}", asked)
    with_code = not without and re.search(rf"\b(including|incl|include|with|plus) {_CODE_WORDS}", asked)
    written = str(phone).strip()
    digits = re.sub(r"\D", "", written)
    # the number without its dial code, when it's written with it ("+1 480-555-0100", "1-480-555-0100")
    national = digits[len(code) - 1:] if (written.startswith("+") and digits.startswith(code[1:])
                                          or code == "+1" and len(digits) == 11 and digits.startswith("1")) else digits
    if re.search(r"\bno (spaces|dashes)|without (spaces|dashes)|digits only|numbers only", asked):
        return f"{code}{national}" if with_code else national
    if code == "+1" and len(national) == 10 and re.search(r"(?:^|[^\d+#\s-])\s*#{3}-#{3}-#{4}", label):
        # a US number written as the box shows it (Edward Jones' "###-###-####. NOTE: Phone number MUST include dashes.",
        # run 86), with its dial code where the box asks for it (never under a "1-###-..." or "+1 ###-..." mask: the
        # review's)
        dashed = f"{national[:3]}-{national[3:6]}-{national[6:]}"
        return f"{code} {dashed}" if with_code else dashed
    if with_code and not written.startswith("+"):
        bare = re.sub(r"^\s*1[\s.-]*", "", written) if national != digits else written
        return f"{code} {bare}"
    if without and national != digits:
        return national
    return phone


def _masked_phone(prof: Profile, job: dict, label: str = "") -> Any:
    """The phone number for a long box that asks for it and shows the format it's written in (Edward Jones'
    "###-###-####", run 86): only where the label shows that mask; else the box is the person's to answer."""
    return _phone(prof, job, label) if re.search(r"#{3}-#{3}-#{4}", label or "") else None


# The kinds of phone a form's boxes name (the alias groups of a phone's type, in _COUNTRY_ALIASES)
_PHONE_KINDS = {"mobile": r"mobile|cell(?:ular)?", "home": r"home|landline", "work": r"work|business|office"}
_ANY_PHONE_KIND = "|".join(_PHONE_KINDS.values())
# A box that names its kind: Taleo's "Work Phone Number", "Cellular Number ..." (Kforce's goes on with a
# notice about its text messages), "Phone (Home)", "Mobile"; not "Phone Number", which names none
_PHONE_BOX_KIND = re.compile(rf"^(?:(?P<lead>{_ANY_PHONE_KIND})(?: (?:tele)?phone| number| no)\b|"
                             rf"(?:tele)?phone(?: number| no)? (?P<trail>{_ANY_PHONE_KIND})$|(?P<bare>mobile|cell(?:ular)?)$)")
# A label that goes on after its box's name for as long as the employer likes, so no matter how long: only
# a box named by kind (not "Mobile apps you have built"), and never one that asks for consent or signs the
# person up for marketing, alerts or a newsletter (a notice that its texts are "non-marketing" is no sign-up)
_PHONE_SIGN_UP = (r"(?<!non )\bmarketing|\bjob alerts?|\bnewsletters?|\bpromotion\w*|\bsubscri\w*|\bopt(?:ing)? in|"
                  r"\bconsent|\bagree|\bsend me|\bsimilar jobs|\btalent (?:community|network)")
_LONG_PHONE_BOX = rf"^(?!.*(?:{_PHONE_SIGN_UP}))(?:{_ANY_PHONE_KIND})(?: (?:tele)?phone| number)\b"
# What a long label that asks to "enter the phone number" may say that makes it not the person's own number to give:
# someone else's, an explanation, texts or automated calls (the review of 0.3.167)
_NOT_OWN_PHONE = (r"\b(?:next of kin|spouse|partner|family|relatives?|parents?|guardian|referee|referen\w*|supervisor\w*|"
                  r"manager\w*|recruiter|agency|emergency|explain|why|reason|tell us|not able|unable|cannot|prefer not|"
                  r"another way|sms|text\w*|updates|whatsapp|autodial\w*|automated|pre ?recorded|"
                  # (a sign-up under other words, another's number under other words: the second review's)
                  r"notif\w*|alerts?|inform\w*|future|other (?:positions|roles|opportunities)|new (?:roles|openings|jobs)|"
                  r"similar roles|stay (?:connected|in touch)|hear about|boss|colleague\w*|co ?workers?|employ(?:er|ment)\w*|"
                  r"current job|previous|former|prior)\b")


def _phone_kind_of(word: str) -> str | None:
    return next((kind for kind, words in _PHONE_KINDS.items() if re.fullmatch(words, word)), None)


def _phone_box_kind(label: str) -> str | None:
    """The kind of phone a box names ("mobile", "home" or "work"), or None for a plain one."""
    m = _PHONE_BOX_KIND.match(label)
    return _phone_kind_of(m.group("lead") or m.group("trail") or m.group("bare")) if m else None


def _other_kind_of_phone(label: str, prof: Profile) -> bool:
    """A box for a kind of phone other than the profile's ("Work Phone Number" for a mobile): the
    profile's one number isn't of that kind, so it's the person's. The profile's phone_type is
    "Mobile" unless it says otherwise; one that is none of the three fits no box named by kind."""
    box = _phone_box_kind(label)
    return box is not None and box != _phone_kind_of(re.sub(r" (?:tele)?phone$", "", norm(prof.get("personal.phone_type", "Mobile"))))


# A box for another person's details: a reference's, a supervisor's, an emergency contact's, a relative's
# ("Family Name" is the person's own last name)
_SOMEONE_ELSE = re.compile(r"\b(referen\w*|referee\w*|emergency|next of kin|supervisor\w*|manager\w*|referr\w*|"
                           r"contact (?:person|name)|relationship|relatives?|family(?! (?:or )?(?:last name|surname|name)s?\b)|spouse)\b")
# A box for another party's details, by its own words ("Employer Phone", "School Zip Code") or
# its section's ("Most Recent Employer", "High School")
_OTHER_PARTY_LABEL = re.compile(r"\b(employer\w*|company|business|school)\b")
_OTHER_PARTY_SECTION = re.compile(r"\b(employer\w*|school)\b")
_EEO_RULES = {"gender", "hispanic", "race", "veteran", "disability", "sexual_orientation"}
# A question about someone in the person's family ("Gender of your spouse", "Are you the spouse of a veteran?"),
# not a family word in passing ("we partner with veterans", "family and medical leave")
_FAMILY = re.compile(r"\b(your|their) (spouse|husband|wife|partner|parents?|child(ren)?|dependents?|relatives?|family"
                     r"( members?)?|household( members?)?|next of kin)\b|\bhousehold members?\b|\b(spouse|husband|wife|widow\w*|partner|parent|child|dependent|relative|"
                     r"family member) of (a|an|the|any)\b")
_CONTACT_RULES = {"email", "first_name", "middle_name", "last_name", "preferred_last_name", "preferred_name",
                  "preferred_full_name", "full_name",
                  "phone_type",
                  "phone_code", "phone_ext", "phone", "address1", "address2", "city", "postal_ext", "postal",
                  "county", "state", "home_state", "country"}

# Getters that read the question itself, not only the profile
# A background question's answer is a choice: a text box with its words is a follow-up ("If yes,
# list the names of relatives employed here", "Branch of military service"), never "No"
_YES_NO_KINDS = {"select", "listbox", "combobox", "radio_group"}


def _reads_question(getter: Getter) -> bool:
    return getter in _READS_QUESTION or bool(getattr(getter, "reads_question", False))


_READS_QUESTION = {_relatives, _government, _travel, _previously_employed, _no_sponsorship, _sponsorship, _UNDER_18, _OVER_18, _phone,
                   _masked_phone,
                   _local_or_relocate, _lives_in, _veteran, _has_degree}

HOW_HEARD = r"(how|where) did you (first )?(hear|find|learn)|source of (application|referral)|^source$"
# (rule name, label regex, getter, max label length or None, allowed kinds or None)
_TEXTY = {"text", "textarea", "select", "listbox", "combobox", "radio_group"}
RULES: list[tuple[str, str, Getter, int | None, set[str] | None]] = [
    ("how_heard", HOW_HEARD, _p("preferences.how_did_you_hear"), None, None),
    # contact details: short labels only, so long questions that merely mention
    # "state" or "name" don't match
    ("email", r"^(confirm |re ?enter |re ?type |verify )?e ?mail( address)?( again)?$|^(your )?email\b|^enter (your )?e ?mail\b",
     _p("personal.email"), 45, None),  # "Enter email to start application process" (Qorvo)
    ("first_name", r"^(legal )?(first|given)( name)?$|^(legal )?first name|^given name|^forename", _p("personal.first_name"), 45, None),
    ("middle_name", r"^middle (name|initial)", _p("personal.middle_name"), 45, None),
    ("last_name", r"^(legal )?(last|family|sur)( ?name)?$|^(legal )?(last|family) name|^surname", _p("personal.last_name"), 45, None),
    # American Express's Oracle form: "Preferred Full Name", the name the person goes by and their last name
    ("preferred_full_name", r"^preferred full name", _preferred_full_name, 45, None),
    ("preferred_last_name", r"^preferred (last|family|sur) ?name", _p("personal.last_name"), 45, None),
    ("preferred_name", r"^preferred (first )?name|^nick ?name", lambda p, j: p.get("personal.preferred_name") or p.get("personal.first_name"), 45, None),
    ("full_name", r"^(full |legal |your |candidate )?(full )?name$|^full (legal )?name|^legal name", _full_name, 45, None),
    ("phone_type", r"phone (device )?type|type of phone", lambda p, j: p.get("personal.phone_type", "Mobile"), 45, None),
    # Kforce's Taleo: "Primary Number", a choice of Home, Work or Cellular Phone (live, Oct 2026)
    ("phone_type", r"^primary (phone )?number$", lambda p, j: p.get("personal.phone_type", "Mobile"), 45, _YES_NO_KINDS),
    # (not "Phone Number (including country code)": that's the number, written with its code)
    ("phone_code", r"^(?!.*\b(number|no|incl|including|with)\b).*\b(country|phone|dial(ing)?) (phone )?code\b", _phone_code, 45, None),
    ("phone_ext", r"extension", lambda p, j: None, 45, None),
    ("phone", r"phone|mobile|cell|telephone", _phone, 45, {"text", "combobox"}),
    # a longer label that goes on to say how to write it ("Phone Number [must include your location code ...]")
    ("phone", r"^((mobile|cell|home|primary) )?(tele)?phone( number)? (must|should|including|incl|with|format|in the format|"
              r"e g|digits|no spaces)\b", _phone, None, {"text"}),
    # Kforce's Taleo (live, Oct 2026): "Cellular Number Kforce may send non-marketing, employment-related text
    # messages to any number(s) you submit. Reply STOP any time to cancel." is the application's number box
    ("phone", _LONG_PHONE_BOX, _phone, None, {"text"}),
    # Edward Jones' BrassRing (run 86): "... Please enter the phone number you would like to use to receive your code(s)
    # in the field below ... ###-###-####": a box that asks for the person's own phone number in the format it shows,
    # however long its words (only with that format shown: _masked_phone).
    # Not another's ("... of your next of kin"), another kind's ("your work phone number"), a code's the number names,
    # an explanation's, nor one that signs the person up or agrees to texts or automated calls (the review of 0.3.167)
    ("phone", rf"^(?!.*(?:{_PHONE_SIGN_UP}|{_NOT_OWN_PHONE}))"
              r".*\benter (?:the|your|a) (?:(?:best|preferred|primary|contact|daytime) ){0,2}"
              r"(?:tele)?phone number\b(?! (?:of|for)\b)"
              # (a code's or an extension's box the number names, not a number "including country code")
              r"(?! (?:(?!including\b|incl\b|with\b|plus\b)\w+ ){0,2}(?:code|otp|passcode|pin|ext\w*)\b)",
     _masked_phone, None, {"text"}),
    ("address2", r"address line 2|^address 2|apartment|suite|^apt|^unit", _p("personal.address.line2"), 45, None),
    ("address1", r"address line 1|^address 1|^street|^(home |mailing |street )?address$", _p("personal.address.line1"), 45, None),
    ("city", r"^city|town|location city|current city|city of residence|"
             r"^(in )?(what|which) city do you (currently |presently )?(live|reside)( in)?$", _p("personal.address.city"), 45, None),
    ("current_location", r"^where are you (currently |presently )?(located|based|living)$|^(your )?current location$|"
                         r"^where do you (currently |presently )?(live|reside)$", _city_state, 45, None),
    # Oracle's "Zip Code+4" wants the 4-digit extension, not the ZIP: left for the site to fill
    ("postal_ext", r"zip( code)? ?(\+|plus) ?4|^zip ?4$|zip (code )?extension", lambda p, j: None, 45, None),
    ("postal", r"zip|postal|post code|postcode", _p("personal.address.postal_code"), 45, None),
    # (but "County / State" is a state's: Rogers' SuccessFactors lists the states under it, run 56)
    ("county", r"^county(?! (or |and )?state\b)", lambda p, j: county_of(p), 45, None),
    # (not "State your desired salary", "State ID Number" or "Statement of accuracy")
    ("state", r"^state\b(?! (your|id|identification|licen[cs]e|the|any|why|how|what|whether|if|briefly)\b)|province|^region|"
     r"^county (or |and )?state$",
     _state, 45, None),
    # asked as a question, with a note on how to write it (GCU's "In what state do you live? (Please use the state
    # abbreviation format, i.e. AZ)", the full run of Oct 12)
    ("home_state", r"^(in )?(what|which) state do you (currently |presently )?(live|reside)( in)?\b", _state, 120, None),
    # the address's country: not "Country of citizenship / birth" (those are other questions)
    ("country", r"^country\b(?!.*\b(citizen|birth|born|nationalit|passport|origin)\w*)", _p("personal.address.country"), 45, None),
    # "LinkedIn profile (if you do not have one or if you prefer not to provide one, enter N/A)" (GoDaddy)
    ("linkedin", r"^(your )?linked ?in (profile|url|profile url|address)\b", _p("personal.linkedin_url"), 200,
     {"text", "textarea"}),
    ("linkedin", r"linked ?in", _p("personal.linkedin_url"), 60, {"text", "textarea"}),
    ("github", r"github", _p("personal.github_url"), 45, {"text"}),
    ("website", r"website|portfolio|personal (site|url)|^url$|blog", _p("personal.website"), 45, {"text"}),
    # "Are you currently employed?" (only that: "...by Intel?" asks about one employer): from the work history
    ("employed_now", r"^(are you )?(currently|presently) employed$", _employed_now, 40, None),
    ("current_company", r"(current|most recent|present) (employer|company)", _p("experience.current_company"), 60, None),
    ("current_title", r"(current|most recent|present) (job )?(title|position|role)", _p("experience.current_title"), 60, None),
    ("total_years", r"^(total )?years of (professional |work )?experience$", _p("experience.total_years"), 60, None),
    ("any_experience", r"^do you have (any )?(previous|prior|past) (work|employment) experience$",
     lambda p, j: "Yes" if p.get("work_history") else None, 60, None),
    # "Do you have a Bachelor's degree?": see _has_degree
    ("has_degree", _DEGREE_ONLY, _has_degree, 200, {"select", "listbox", "combobox", "radio_group"}),
    ("degree", r"highest (level of )?(education|degree)|^degree$|education level", _edu("highest_degree", "degree"), 80, None),
    ("school", r"^(school|university|college|institution)\b(?!.*\b(licen[cs]\w*|certif\w*|major|degree|gpa|city|state|location|"
     r"country|year|date|address|"
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
    # (years of age: never 18 months, hours, credits or years of experience, the review of Oct 15; nor "if under 18,
    # give your date of birth" after the age question, which asks the other way)
    ("under_18", r"(?<!\bif )(under|less than|below|younger than) (the age of )?(18|eighteen)\b(?! ?(months?|hours?|days?|weeks?|"
                 r"credits?|units?|yrs?|years? (of )?(experience|work|service)))", _UNDER_18, None, None),
    ("over_18", r"(18|eighteen) years(?! (of )?(experience|work|service))|at least (18|eighteen)\b(?! ?(months?|hours?|days?|"
                r"weeks?|credits?|units?|yrs?|years? (of )?(experience|work|service)))|over (the age of )?(18|eighteen)\b|"
                r"\b(18|eighteen) or (older|over)\b|age of 18|legal age", _OVER_18, None, None),
    # Yes means no sponsorship: "…without sponsorship?", "…and do not require sponsorship?"
    # (only filler words between the "no" and the sponsorship: "Please answer yes or no: will
    # you require sponsorship?" asks the other way)
    ("no_sponsorship", r"\b(no|not|never|without|free of)( (any|visa|employment|immigration|employer|the|a|in|need|needs|of|"
     r"for|requiring|requirement|h ?1 ?b|work))* sponsor|(do not|don t|dont|will not|won t|not|never) (now or in the future |"
     r"currently |ever )?(require|need)\w* .{0,40}sponsor|sponsor\w* (is |are |would be |will be )?(not|never) (be )?"
     r"(required|needed|necessary)|"
     # (the immigration-case wording, negated: "I will not require the employer to commence an immigration case")
     r"\b(no|not|never|without|don t|dont|won t)( now or in the future)? requir\w* .{0,60}\b(commence|start|begin|file|"
     r"initiate)\w* an? (immigration|visa) (case|petition|process)|"
     # (and a visa, negated: "I will not require a visa to work in the United States")
     r"\b(do not|don t|dont|will not|won t|not|never)( now or in the future| currently)? (require|need)\w* an? (work )?visa\b",
     _no_sponsorship, None, None),
    # (and in other words: "require <employer> to commence an immigration case in order to employ you",
    # live test 2, issue #112, note 17)
    ("sponsorship", r"(require|need|seek)\w* .{0,40}sponsor|sponsor\w* .{0,40}(require|need)|"
     r"^(visa |employment |immigration )?sponsorship( status| required| needed)?$|"
     r"\brequire\w* .{0,60}\b(commence|start|begin|file|initiate)\w* an? (immigration|visa) (case|petition|process)|"
     # Linde: "Will you now or in the future require a visa to permit you to legally work in the United States?"
     r"\b(require|need)\w* an? (work )?visa (to|in order to) .{0,30}\b(legally work|work (legally |lawfully )?in the "
     r"(u s|us|united states))\b",
     _sponsorship, None, None),
    # the I-9: "Can you provide verification of both your identity and authorization to work in the
    # United States, to the extent required by law?" (Axon)
    # Onto's Workday: "If hired, can you provide proof of eligibility?" (eligibility alone, if hired, is the I-9's)
    ("work_proof", r"^((if|once|when) (you are )?hired |upon hire )?can you (provide|furnish|submit|present) "
                   r"(proof|verification|documentation|documents?) (of|for|showing) "
                   r".{0,40}(authori[sz]ation to work|eligibility to work|employment eligibility|work authori[sz]ation)|"
                   r"^((if|once|when) (you are )?hired |upon hire )can you (provide|furnish|submit|present) "
                   r"(proof|verification|documentation|documents?) (of|for|showing) (your )?eligibility$|"
                   r"^can you (provide|furnish|submit|present) (proof|verification|documentation|documents?) "
                   r"(of|for|showing) (your )?eligibility (upon|if|once|when) (you are )?hired$",
     _yn("work_authorization.authorized_to_work"), 200, {"select", "listbox", "combobox", "radio_group"}),
    ("authorized", _AUTHORIZED, _authorized, None, None),
    # Only questions that ask whether you are a U.S. person: export-control wording
    # also comes with other questions, e.g. Micron's "are you a citizen of Cuba, Iran ...?"
    ("us_person", r"^(?!.*\b(other than|another country|any (other )?country|foreign)\b).*"
     r"(\bu ?s person\b|citizen.{0,80}(permanent resident|green card|refugee|asyl|protected individual))",
     _us_person, None, None),
    ("us_citizen", r"are you a (u s|united states) citizen\b|are you a citizen of the (u s|united states)( of america)?$",
     _yn("work_authorization.us_citizen"), None, None),
    # "Are you an alien illegally in the United States?", "...admitted under a nonimmigrant visa?" (Axon)
    ("alien", r"^are you an? (illegal )?alien\b", lambda p, j: "No" if p.get("work_authorization.us_citizen") is True else None,
     200, {"select", "listbox", "combobox", "radio_group"}),
    # TI: "Do you currently hold an H, L, E, J, or F nonimmigrant visa?" A citizen holds none
    ("visa_holder", r"\b(hold|have) an? .{0,40}non ?immigrant visa|"
     # "Are you currently on an F-1 visa (OPT/CPT)?", "Do you currently hold an H-1B visa?": a citizen holds none
     r"\b(hold|have|holding|on) an? (f ?1|h ?1 ?b|j ?1|l ?1|tn|o ?1|e ?3|h ?4)\b( visa| status)?",
     lambda p, j: "No" if p.get("work_authorization.us_citizen") is True else None, None, None),
    # "What is your current visa status?" among "U.S. Citizen", "H-1B", ...: a citizen's is the citizen one
    # ("Citizenship status" keeps the citizenship rule, from the profile's country)
    ("citizen_status", r"^(what is )?(your )?(current )?(visa|immigration) status$",
     lambda p, j: "U.S. Citizen" if p.get("work_authorization.us_citizen") is True else None, 80, None),
    # ASM's export-control question: "... U.S. Export Administration Regulation (EAR) ... Please, select
    # which option applies to you." among "Citizen or National of the United States", "Alien Authorized
    # to Work", ...: a citizen's is the citizen one
    ("export_status", r"\bexport (control|administration)\b.{0,600}\b(select|choose|indicate|which)\b.{0,40}"
     r"\b(applies|describes|option)\b.{0,20}\b(you|yours?)\b",
     lambda p, j: "U.S. Citizen" if p.get("work_authorization.us_citizen") is True else None, None,
     {"select", "listbox", "combobox", "radio_group"}),
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
    # (whole words: "for more information, call ..." in a veteran notice isn't "on call")
    ("shift", r"shift work|rotating shift|nights and weekends|work (nights|weekends)|\bon ?call\b", _yn("preferences.flexible_schedule"), None, None),
    # "Do you have a valid driver's license?" alone: not its record ("...been revoked or suspended"),
    # a commercial one, or one asked with something else ("...and reliable transportation")
    ("drivers_license", r"^(do you (currently )?(have|hold|possess)|are you in possession of) (a |an )?"
                        r"((current|valid|active|unrestricted)( and| or)? )*(u s |us |state |arizona )?"
                        r"(driver( s|s)?|driving) licen[cs]e( (issued )?in (the u s|the united states|arizona|your state))?$",
     _yn("personal.drivers_license"), 120, None),
    # desired_salary is one yearly figure: not an answer to "current salary" or a monthly/hourly rate
    ("other_salary", r"(current|last|previous|present|most recent|drawn) .{0,25}(salary|compensation|pay\b)"
     r"|(monthly|per month|hourly|per hour|weekly|per week) .{0,25}(salary|compensation|pay\b|rate)", lambda p, j: None, None, None),
    # American Express: "Indicate your highest level of preference by work location:" among its offices
    ("preferred_location", r"^(preferred|desired) (work )?(location|site|city)s?\b|location preference|"
     r"preference (by|for) (work )?location|(which|what) (work )?location (would|do) you prefer",
     lambda p, j: next(iter(_listed(p.get("preferences.locations"))), None), 120, None),
    ("salary", r"salary|compensation|pay (expectation|requirement)|desired pay|expected pay", _p("preferences.desired_salary"), None, None),
    ("start_date", r"start date|available to start|earliest (date|start)|when can you start|notice period|"
                   # to start work, not for an interview
                   r"when (would|could|will) you be available( to (start|begin)| for (work|employment)| if .{0,10}offer\b.*)?$|"
                   r"soonest .{0,30}(start|begin)\b", _p("preferences.earliest_start"), None, None),
    # background: recurring yes/no questions setup asks once (a "no" answers any wording of them)
    ("dod_employee", r"\b(department of defense|dod)\b.{0,80}\b(employee|employed|employment)|\b(employee|employed) "
     r"(of|by|with) (the )?(u s |us )?(department of defense|dod)\b", _background("defense_department"), None, _YES_NO_KINDS),
    ("government_employee", r"\b(are|were|have) you\b.{0,80}\b(government|federal|state or local|public sector)\b.{0,60}"
     r"\b(employee|employed|official|worker)|\b(employ\w*|work\w*) (by|for) (a |the |any )?(u s |us |federal |state |local |"
     r"city |county )?(government|public agency)\b", _government, None, _YES_NO_KINDS),
    ("relatives", r"\b(relatives?|related|(immediate )?family members?|members? of your (immediate )?family|immediate family|"
     r"friends?|spouse|in laws?)\b.{0,80}\b(employ\w*|work\w*)\b|"
     r"\b(employ\w*|work\w*)\b.{0,60}\b(relatives?|family members?)\b", _relatives, None, _YES_NO_KINDS),
    ("restrictive_agreement", r"\bnon ?(compet\w*|solicit\w*)\b|restrictive covenant|\bagreements?\b.{0,160}\b(impact|interfere|"
     r"restrict|limit|prevent|prohibit)\w*", _background("restrictive_agreement", unless=r"\b(willing|sign|agree to)\b"), None,
     _YES_NO_KINDS),
    ("intellectual_property", r"\b(own|control|hold)\b.{0,60}\b(intellectual property|patents?|trademarks?|copyrights?)|"
     r"\b(intellectual property|patents?)\b.{0,60}\b(own|interest)", _background("intellectual_property"), None, _YES_NO_KINDS),
    ("outside_work", r"\b(secondary|outside|second|additional) (employment|job|business)|\bmoonlight", _background("outside_work"),
     None, _YES_NO_KINDS),
    ("board_member", r"\bboard of (directors|advisors)\b|\bboard member\b|governing body",
     _background("board_member", unless=r"\b(family|relatives?|spouse|friends?)\b"), None,
     _YES_NO_KINDS),
    ("military", r"\b(served|serve|serving|service) in (the |any )?(u s |us )?(military|armed forces)|\bmilitary service\b|"
     r"\b(previous|prior|any) (u s |us )?(military|armed forces) (experience|service|background)\b",
     _background("military", unless=r"\b(spouse|family|parent|relative)\b"), None, _YES_NO_KINDS),
    ("employee_id", r"\b(employee|worker|associate|staff|badge) ?(id|number|no\b|#)|\bwwid\b", _employee_id, 160, {"text"}),
    ("previous_employee", r"(previously|ever|formerly) (been )?(employed|worked)|former employee|have you (ever )?worked (for|at)|worked .{0,40} before|"
     r"have you (ever )?been hired\b",
     _previously_employed, None, None),  # (only about this employer: see _previously_employed)
    # voluntary self-identification
    ("sexual_orientation", r"sexual orientation|\blgbt", _p("eeo.sexual_orientation"), None, None),
    ("gender", r"\bgender\b|\bsex\b", _eeo_gender, None, None),
    ("hispanic", r"hispanic|latin[oa]", _p("eeo.hispanic_latino"), None, None),
    ("race", r"\brace\b|ethnicity|ethnic", _p("eeo.race"), None, None),
    ("veteran", r"veteran", _veteran, None, None),
    # self-identification only: "Can you perform the essential functions … with or without
    # accommodation?" and "Do you need an accommodation?" are other questions
    ("disability", r"^(?!.*\b(essential function|accommodat|perform|able to)).*disabilit", _eeo_disability, None, None),
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


# A question that means something only with the text around it, and the notices whose remembered Yes never goes to
# another employer's by their question alone (issue #112, note 29's review)
_POINTS_AWAY = re.compile(r"\b(?:above|below|following|these|those|this|that|it|them|such|said)\b", re.I)
_NOTICE_OF_CONSENT = re.compile(r"talent (?:community|network|pool)|job alerts?|newsletter|subscribe|marketing|mailing "
                                r"list|text messages?|\bsms\b|promotion|arbitration|terms|privacy|consent|agree|"
                                r"authori[sz]|acknowledg|certif|attest|signature", re.I)


def _answer_bank(prof: Profile, label: str, job: dict | None = None, common: bool = False) -> Answer | None:
    """The person's own answers (answers.yaml, profile.yaml's answers:) to this question; with `common`, only the
    common questions' (the question bank's), which resolve_field tries last."""
    company = norm((job or {}).get("company"))
    for item in _listed(prof.get("answers")):
        if not isinstance(item, dict) or not item.get("match") or item.get("answer") in (None, ""):
            continue
        if bool(item.get("bank")) != common:
            continue
        if item.get("question"):
            # answered in the Job Desk: that question, worded the same. "I agree" isn't "I agree
            # to binding arbitration", and "Are you currently employed?" isn't "... by ASML?"
            asked = label
            if _bare_question(str(item["question"])) != _bare_question(label):
                # or the question a long label was remembered by (its "core"), when this long label asks the same
                # one: a notice is worded for each employer (issue #112, note 29). Whether it is about the
                # employer is read from that question, not the notice around it ("our Privacy Statement"); a
                # label that asks two things has no one question, and isn't answered from this
                asked = question_core(label)
                if not item.get("core") or _bare_question(asked) != _bare_question(str(item["core"])):
                    continue
                # never a question that only means something with the notice around it ("Do you agree to the
                # terms described above?"), nor a Yes to a notice of consent, terms or sign-ups: another
                # employer's may be an arbitration agreement or a talent community's. A No to one is kept
                if _POINTS_AWAY.search(asked) or (_NOTICE_OF_CONSENT.search(f"{label} {item['question']}")
                                                  and not _NO.match(str(item.get("answer")).strip())):
                    continue
            source = norm(str(item.get("from") or ""))
            if source and source != company and (_EMPLOYER_SPECIFIC.search(asked) or source in norm(label)
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
    (r"phone|e ?mail|fax|address|zip|postal|supervisor|manager|reference|salary|wage|pay\b|reason|contact", "skip"),
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


# Other ways a job's dates get written: start_date / end_date, from / to, or one span
_START_KEYS = ("start", "start_date", "from", "started")
_END_KEYS = ("end", "end_date", "to", "ended")
_SPAN = re.compile(r"\s+(?:-|to|until)\s+|\s*[\u2013\u2014]\s*", re.I)


def _split_span(span: str) -> list[str]:
    """"Mar 2019 - Jun 2021" or "Mar 2019-Jun 2021" in two; ISO "2019-03" stays one date."""
    parts = _SPAN.split(span, maxsplit=1)
    if len(parts) == 2:
        return [p.strip() for p in parts]
    for m in re.finditer(r"-", span):
        left, right = span[:m.start()].strip(), span[m.end():].strip()
        if parse_month_year(left)[1] and (parse_month_year(right)[1] or is_present(right)):
            return [left, right]
    return [span]


def _from_parts(entry: dict, which: str) -> Any:
    """start_year with start_month (03, "Mar"), as one date."""
    year, month = entry.get(f"{which}_year"), entry.get(f"{which}_month")
    if year in (None, ""):
        return None
    if month in (None, ""):
        return str(year)
    return f"{year}-{int(month):02d}" if str(month).strip().isdigit() else f"{month} {year}"


def entry_dates(entry: dict) -> tuple[Any, Any]:
    """A job's (or school's) start and end as written: `start` and `end`, else start_date /
    end_date or from / to, else a `dates` span ("Mar 2019 - Jun 2021", "2019 \u2013 Present")."""
    start = next((entry[k] for k in _START_KEYS if entry.get(k) not in (None, "")), None)
    end = next((entry[k] for k in _END_KEYS if entry.get(k) not in (None, "")), None)
    start = start if start is not None else _from_parts(entry, "start")
    end = end if end is not None else _from_parts(entry, "end")
    span = next((entry[k] for k in ("dates", "date", "period", "years") if isinstance(entry.get(k), str)), "").strip()
    if span and (start is None or end is None):
        parts = _split_span(span)
        if len(parts) == 2:
            start = start if start is not None else parts[0]
            end = end if end is not None else parts[1]
    return start, end


def _entry_name(entry: dict) -> str:
    title, company = str(entry.get("title") or "").strip(), str(entry.get("company") or "").strip()
    return f"{title} at {company}" if title and company else title or company or "a job with no title"


def undated_jobs(prof: Profile) -> list[str]:
    """The jobs in work_history without a start month and year, or (unless they're still held)
    an end month and year: Workday asks for both, for every job."""
    out = []
    for entry in profile_entries(prof, "work_history"):
        start, end = entry_dates(entry)
        current = is_present(end) or entry.get("current") is True
        if not all(parse_month_year(start)) or not current and not all(parse_month_year(end)):
            out.append(_entry_name(entry))
    return out


def profile_entries(prof: Profile, key: str) -> list[dict]:
    entries = [e for e in _listed(prof.get(key)) if isinstance(e, dict)]
    if not entries and key == "education_history" and prof.get("education.school"):
        entries = [{
            "school": prof.get("education.school"), "degree": prof.get("education.highest_degree"),
            "major": prof.get("education.major"), "gpa": prof.get("education.gpa"),
            "end": prof.get("education.graduation_year"),
        }]
    return entries


def _entry_slot(field: dict, prof: Profile) -> tuple[str, int, list[dict], str | None] | None:
    """For a field inside a numbered block like "Work Experience 2": the profile list it's from
    (work_history, education_history), the block's number, that list's entries, and the entry's
    attribute it asks for (None when it's none of them: "Hours per week")."""
    section = norm(field.get("section"))
    m = re.search(r"(\d+)$", section)
    if not m:
        return None
    for key, pattern in _ENTRY_SECTIONS:
        if re.search(pattern, section):
            break
    else:
        return None
    label = norm(clean_label(field.get("label") or ""))
    attr = next((a for pattern, a in (_WORK_FIELDS if key == "work_history" else _EDUCATION_FIELDS)
                 if re.search(pattern, label)), None)
    return key, int(m.group(1)), profile_entries(prof, key), attr


def entry_of(field: dict, prof: Profile) -> tuple[str, str] | None:
    """For a box in a job's or school's block that the profile is where its answer lives (a job's
    title or dates, a school's degree): that entry in words ("Safety Technician at Example Fab")
    and which list it's from ("work", "education"; "extra" for a block past the profile's
    entries). None for any other box, a block's "Reason for leaving" or "Day" among them."""
    slot = _entry_slot(field, prof)
    if slot is None or slot[3] in (None, "skip"):
        return None
    key, n, entries, attr = slot
    if attr in ("start", "end") and norm(field.get("sublabel")) == "day":
        return None
    if not 0 < n <= len(entries):
        thing = "job" if key == "work_history" else "school"
        return f"{field.get('section')} (your profile has no {thing} {n})", "extra"
    if key == "work_history":
        return _entry_name(entries[n - 1]), "work"
    return str(entries[n - 1].get("school") or "").strip() or str(field.get("section")), "education"


# How a school attended without finishing a degree is written in the profile (setup writes it so)
NO_DEGREE = "Some college (no degree)"


def no_choice_for_no_degree(field: dict, prof: Profile) -> bool:
    """A school's Degree list with nothing for the entry's classes without a degree (a Workday
    site's lists only High School, GED, Associates and up, live, Oct 2026): writing "Some college
    (no degree)" in the profile can't place it there, and none of the others is ever picked for
    it (coursework is not a degree). The pick there is the person's."""
    slot = _entry_slot(field, prof)
    if slot is None or slot[0] != "education_history" or slot[3] != "degree" or not field.get("options") \
            or list_unloaded(field):  # (a list with nothing at all has nothing for anything: not loaded)
        return False
    _, n, entries, _ = slot
    if not 0 < n <= len(entries) or finished_degree(entries[n - 1].get("degree")):
        return False
    said = str(entries[n - 1].get("degree") or "").strip()  # ("Not completed", say)
    return all(choose_option(words, field["options"]) is None for words in {said, NO_DEGREE} if words)


# A school's degree that says no degree was finished there: classes taken, not a degree missing from the profile (an
# empty one), nor one under way ("BS in progress", "expected 2027")
_CLASSES_ONLY = re.compile(r"\bno degree\b|\bsome college\b|\bcourse ?work\b|\bclasses\b|\bnot (?:completed|finished)\b|"
                           r"\bdid(?: not|n't) (?:finish|complete)\b|\bnon-?degree\b|\battended\b", re.I)


def classes_only(field: dict, prof: Profile) -> bool:
    """A box in a school's block whose profile entry says no degree was finished there ("Some college (no degree)")."""
    slot = _entry_slot(field, prof)
    if slot is None or slot[0] != "education_history":
        return False
    _, n, entries, _ = slot
    return 0 < n <= len(entries) and bool(_CLASSES_ONLY.search(str(entries[n - 1].get("degree") or "")))


def list_unloaded(field: dict) -> bool:
    """A list read with its placeholder alone ("Select One") and no choice at all: one the page didn't finish loading
    (Grand Canyon University's Workday Degree list, run 61; in runs 57 to 60 it had its degrees). Not a search prompt,
    whose choices come as it's typed in."""
    options = field.get("options") or []
    return field.get("kind") in ("select", "listbox", "combobox") and not field.get("search") and bool(options) \
        and all(is_empty_value(o) for o in options)


def entry_list_unloaded(field: dict, prof: Profile) -> bool:
    """A school's Degree or major list read with its placeholder alone (list_unloaded), in a block the profile has
    that answer for. Only those: a list that fills once another box is chosen (a State after its Country) shows its
    placeholder alone until then, and a block past the profile's entries, or an answer the profile lacks, is said for
    what it is."""
    slot = _entry_slot(field, prof)
    if slot is None or slot[0] != "education_history" or slot[3] not in ("degree", "major") or not list_unloaded(field):
        return False
    _, n, entries, attr = slot
    return 0 < n <= len(entries) and bool(str(entries[n - 1].get(attr) or "").strip())


def degree_not_listed(field: dict, prof: Profile) -> str | None:
    """The profile's degree for a school whose Degree list has no choice for it (a Workday site's list was
    Certificate, Foundation, Associate, Bachelor ... Some College, No degree, with no high school or GED:
    live test 2, issue #112, note 16), which isn't the profile missing it. That degree, else None."""
    slot = _entry_slot(field, prof)
    if slot is None or slot[0] != "education_history" or slot[3] != "degree" or not field.get("options") \
            or list_unloaded(field):  # (nothing at all: not loaded, not without the profile's degree)
        return None
    _, n, entries, _ = slot
    if not 0 < n <= len(entries):
        return None
    said = str(entries[n - 1].get("degree") or "").strip()
    if not said or no_choice_for_no_degree(field, prof):
        return None
    return said if choose_option(said, field["options"]) is None else None


def degree_kinds_listed(field: dict, degree: str) -> list[str]:
    """A Degree list's choices of the profile's degree level, when it has several kinds of it and the
    profile's degree names none ("Bachelor of Arts", "Bachelor of Science" for "Bachelor's Degree":
    Onto's Workday, nightly live check Oct 11). Empty otherwise."""
    level = degree_key(degree)
    if not level or degree_kind(degree):
        return []
    same = [o for o in field.get("options") or [] if isinstance(o, str) and degree_key(o) == level]
    return same if len({degree_kind(o) for o in same} - {None}) > 1 else []


def _resolve_entry(field: dict, prof: Profile) -> tuple[bool, Answer | None]:
    """(handled, answer) for fields inside a numbered block like "Work Experience 2"."""
    slot = _entry_slot(field, prof)
    if slot is None:
        return False, None
    key, n, entries, attr = slot
    if not 0 < n <= len(entries):
        return True, None
    entry = entries[n - 1]
    if attr is None:
        return True, None  # e.g. "Hours per week": not something the profile holds
    rule = f"{key}[{n}].{attr}"
    dates = dict(zip(("start", "end"), entry_dates(entry)))
    current = is_present(dates["end"]) or entry.get("current") is True
    if attr == "skip":
        return True, None  # e.g. "Employer Phone": not something the profile holds
    if attr == "current":
        return True, Answer(current, rule)
    if (attr == "end" and key == "education_history" and "graduat" in norm(clean_label(field.get("label") or ""))
            and not finished_degree(entry.get("degree"))):
        return True, None  # a school not finished: no graduation date to give
    if attr == "end" and current:
        return True, Answer(SKIP, rule)
    if attr in ("start", "end"):
        value = _date_value(field, dates[attr])
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
_WORK_RULES = {"no_sponsorship", "sponsorship", "authorized", "work_proof", "us_person", "us_citizen", "citizenship",
               "visa_holder"}
_OTHER_THAN = re.compile(r"\b(other than|another country|any (other )?country|foreign)\b")
# Documents other than a resume that a lone file box may ask for
# a file list's boxes (Taleo's): the file chosen for this application, and the one that's the resume (its row says which)
_FILE_PICKED = re.compile(r"^\s*select the file:\s*(.+?)\s*$", re.I)
_FILE_IS_RESUME = re.compile(r"^\s*(?:this )?document is (?:a|the|your) (?:resume|cv)\s*$", re.I)
_OTHER_DOCUMENT = re.compile(r"\b(degree|diploma|transcripts?|certificat\w*|licen[cs]e|passport|portfolio|writing sample|"
                             r"work sample|recommendation|references?|identification|photo\w*|dd ?214)\b")
# A date box asking when something else happened, not the date a form is signed
_OTHER_EVENT = re.compile(r"\b(conviction|criminal|offen[cs]e|incident|violation|accident|military|service|discharge|"
                          r"availability|available|employment|education|experience|history|birth|licen[cs]e|certif\w*)\b")


def _without_notes(label: str) -> str:
    """The question without the employer's notes in it, which say what the employer offers, or
    an aside ("Do you currently reside in the United States? (Please note, your answer ...)")."""
    label = re.sub(r"(?<=\?)\s*\(\s*(please )?note\b[^)]*\)?", " ", label or "", flags=re.I).strip() or label
    parts = re.split(r"(?<=[.?!])\s+|[()]", label or "")
    kept = [p for p in parts if p.strip() and not _EMPLOYER_NOTE.search(p)]
    return " ".join(kept) if len(kept) < len([p for p in parts if p.strip()]) else label


_METRO_SECTION = re.compile(r"\b(metropolitan|metro area|(closest|nearest) (major )?city)\b")
_METRO_PART = re.compile(r"region|area|city|metro(politan)?( area| city)?")


# A box that declines to answer the question it names ("I decline to say Gender")
_DECLINE_BOX = re.compile(r"^\s*i (?:decline|prefer not|do not wish|don'?t wish) to (?:say|answer|disclose|state|"
                          r"self-identify|identify)\b[\s:,-]*(\S.{1,150}?)\s*$", re.I)


def resolve_field(field: dict, prof: Profile, job: dict | None = None, file_inputs_on_page: int = 1,
                  unmatched: list[Answer] | None = None) -> Answer | None:
    """The answer for a field, or None. A self-identification answer the profile gives but the
    field's choices don't have is added to `unmatched`, so it can be said rather than skipped.
    The common questions' answers (the question bank's, in answers.yaml) come last: only where nothing
    else answers, and never in a block about a past job or a school."""
    ans = _resolve_field(field, prof, job, file_inputs_on_page, unmatched)
    if ans is None and field.get("kind", "text") not in ("checkbox", "file", "password") \
            and not _PAST_BLOCK.search(norm(field.get("section"))):
        ans = _answer_bank(prof, _without_notes(field.get("label") or field.get("name") or ""), job, common=True)
    return ans


# A form's block about one past job or school, whose questions are that job's or school's, never the person's
# answers in general ("Were you terminated?", "Which shift did you work?" in Work Experience 2)
_PAST_BLOCK = re.compile(r"experience|employment|employer|work history|job \d|position \d|education|school|"
                         r"college|universit|training|licen[cs]e|certification", re.I)


def _resolve_field(field: dict, prof: Profile, job: dict | None = None, file_inputs_on_page: int = 1,
                   unmatched: list[Answer] | None = None) -> Answer | None:
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
        # (a page's one upload, in a section for both, "Resume/CV and Cover Letter": the resume goes in it, as a
        # person would put it there: Arizona State University's Workday, the full run of Oct 12)
        # (only a section named for both alike, never one for a cover letter that mentions the resume: "Cover Letter
        # (and resume if not uploaded)", "Your resume was already imported", the review's)
        section = norm(field.get("section"))
        both = (file_inputs_on_page == 1 and not re.search(r"cover", label)
                and re.search(r"\b(resume|cv)\b (and|or)? ?(cover letter)$|^(resume|cv)( cv)? (and|or) cover letter\b", section)
                and not re.search(r"\b(if|optional|already|instead|only)\b", section))
        if re.search(r"cover", where) and not both:
            path = _document(prof, job, "cover_letter")
            return Answer(path, "documents.cover_letter") if path else None
        named = re.search(r"resume|cv|curriculum", where)
        if named or file_inputs_on_page == 1 and not _OTHER_DOCUMENT.search(where):
            path = _document(prof, job, "resume")
            return Answer(path, "documents.resume") if path else None
        return None

    if kind == "checkbox" and (_FILE_PICKED.match(raw_label) or _FILE_IS_RESUME.match(raw_label)):
        # a list of the files on the person's record (Kforce's Taleo, the nightly of Oct 12: "You must select at least
        # one relevant file"): the profile's resume is the one for this application, and is the resume; no other file
        # (its row's file exactly the profile's resume, and the only row with that name: never a file tailored for
        # another job that shares its name, nor one whose name only holds it)
        path = _document(prof, job, "resume")
        name = Path(path).name.strip().lower() if path else ""
        row_file = str(field.get("row_file") or "").strip().lower()
        if name and row_file == name and field.get("same_file_rows") == 1:
            return Answer(True, "documents.resume")
        return None
    if kind == "checkbox" and (declined := _DECLINE_BOX.match(clean_label(raw_label))):
        # a self-identification question's own "I decline to say" box (Nikon's UKG Pro, live run 17: "I decline to
        # say Gender" beside its Gender list): ticked when the profile's answer to that question is a decline,
        # which the list itself may not have
        ans = resolve_field({"kind": "select", "label": declined.group(1), "section": field.get("section")}, prof, job)
        if ans is not None and ans.rule in _EEO_RULES and is_decline(ans.value):
            return Answer(True, ans.rule)
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
    if ans is None and _METRO_SECTION.search(section) and _METRO_PART.fullmatch(label) and not someone_else \
            and not re.search(r"\b(prefer|desired|willing|like to work|relocat)", f"{section} {label}"):
        # Taleo's "Closest Metropolitan City": its Region list is cities (Kforce's, live run 12, Oct 2026: Anthem,
        # Bisbee, ... Chandler): the profile's city, not its state, which the list doesn't have
        city = prof.get("personal.address.city")
        if city:
            ans = Answer(city, "personal.address.city")
    if ans is None and kind == "checkbox_group" and (none := _plans_none(field, prof)):
        return Answer(none, "background.plans")  # (one of its own choices, as it reads)
    if ans is None:
        # a self-identification question is its own topic's, whatever else its notice's words match
        own = _self_identification(label, field)
        for name, pattern, getter, max_len, kinds in ([own] if own else RULES):
            if someone_else and (name in _CONTACT_RULES or name == "employee_id"):
                continue  # a reference's or an emergency contact's name, phone or email; a referrer's ID
            if name in _EEO_RULES and _FAMILY.search(label):
                continue  # "Are you the spouse of a veteran?", "Gender of your spouse": not the person's own
            if max_len is not None and len(label) > max_len:
                continue
            if kinds is not None and kind not in kinds:
                continue
            if own or re.search(pattern, label):
                if name in _WORK_RULES and (_OTHER_COUNTRIES.search(label) or _OTHER_THAN.search(label)):
                    return None  # another country's question: the profile's facts are the United States'
                if name == "phone" and _other_kind_of_phone(label, prof):
                    return None  # a Work Phone for a mobile: the profile's number isn't that kind's
                value = getter(prof, job, raw_label) if _reads_question(getter) else getter(prof, job)  # type: ignore[call-arg]  # these take the question too
                if value is None or value == "":
                    return None  # recognised but the profile has no answer
                ans = Answer(value, name)
                break
    if ans is None and kind in _CHOICE_KINDS - {"checkbox", "checkbox_group"} and not someone_else \
            and (outside := _not_staff_choice(field, prof, job)):
        return Answer(outside, "work_history")  # (one of its own choices, as it reads; only where no rule took the question)
    if ans is None and kind in _CHOICE_KINDS and field.get("options") and _INSTRUCTION_ONLY.match(label) \
            and _POINTS_AT_CHOICES.search(label):
        ans = _topic_from_options(field["options"], prof, job)
    if ans is None and kind == "text" and field.get("autocomplete") and not someone_else and _BARE_NUMBER.fullmatch(label):
        # a box the site marks as a phone's that says no more than "Number" (iCIMS's, beside its Phone
        # Country Code): the profile's phone, without its dial code where the site asks for the national number
        national = field["autocomplete"] != "tel"
        ans = Answer(_phone(prof, job, "phone number without country code" if national else "phone number"), "phone")
    if ans is None or ans.value is None or ans.value == "":
        return None

    # (a yes/no question drawn as a text area, as Nikon's UKG Pro draws its work-authorization questions, live run
    # 14, Oct 2026: the profile's plain Yes or No)
    yes_no_box = ans.rule in {"authorized", "sponsorship", "no_sponsorship"} and polarity(ans.value) is not None \
        and bool(re.match(r"(are|do|will|would|can|have|is) you\b", label))
    # (and where a person lives, asked as a question in a text area: GCU's "In what city do you live?", the full run
    # of Oct 12)
    home_box = ans.rule in {"city", "home_state"} and bool(re.match(
        r"(in )?(what|which) (city|state) do you(?!.*\b(and|why|explain|describe|tell)\b)", label))
    if kind == "textarea" and not ans.rule.startswith("answers[") and ans.rule not in {"linkedin", "salary", "start_date"} \
            and not yes_no_box and not home_box:
        return None
    if isinstance(ans.value, bool):
        ans.value = "Yes" if ans.value else "No"
    if ans.rule in {"state", "home_state"} and re.search(r"\babbreviat", label) and not field.get("options"):
        # ("Please use the state abbreviation format, i.e. AZ")
        ans.value = next((k for k, v in US_STATES.items() if norm(v) == norm(ans.value)), ans.value)

    options = field.get("options")
    if ans.rule == "citizen_status" and options:
        # the one choice that says U.S. citizen ("US Citizen", "Citizen of the United States"), not "Non-US Citizen"
        citizen = [o for o in options if re.search(r"\b(u s|us|united states|american)\b.*\bcitizen|\bcitizen\b.*\b(u s|us|"
                                                   r"united states|usa)\b", norm(o)) and not re.search(r"\bnon\b|\bnot\b", norm(o))]
        return Answer(citizen[0], ans.rule) if len(citizen) == 1 else None
    if ans.rule == "has_degree":
        # a plain "Yes" or "No" among the choices shown: never "No, but I have equivalent experience" or
        # "No, but I'm enrolled", which say more than the profile does; nor where the section allows an equivalent
        plain = next((o for o in options or [] if norm(o) == norm(ans.value)), None)
        if plain is None or "equivalen" in section:
            return None
        return Answer(plain, ans.rule)
    if kind in {"select", "radio_group", "listbox", "checkbox_group", "combobox"} and options:
        # a place lookup's entries: the one in the rest of the profile's address ("Chandler,
        # Maricopa, AZ", not "Chandler, Henderson, TX" listed before it)
        if ans.rule == "current_location":
            # among countries (Axon's "Where are you currently located?"), the profile's own; a list
            # of offices or regions is the person's to choose from
            us = [o for o in options if _US.fullmatch(norm(o))]
            home = _US.fullmatch(norm(prof.get("personal.address.country") or ""))
            return Answer(us[0], ans.rule) if home and len(us) == 1 else None
        chosen = (ans.rule in _PLACE_RULES and choose_place(ans.value, options, place_words(ans.rule, prof))
                  or choose_option(ans.value, options, names=is_name_rule(ans.rule)))
        if chosen is None and is_how_heard(ans.rule, label):
            chosen = _own_website(ans.value, options, job)
        if chosen is None and ans.rule == "start_date":
            chosen = _same_wait(ans.value, options)
        # a search prompt lists only its top level, and a full page of a paged list (Qorvo's
        # countries stop at Iran) only its start: the fill searches them for the answer. A
        # shorter paged list is all there is, so a search can't find anything else in it.
        # A long list of places (Oracle's City on Mayo Clinic's form opened at "Aaron, Clinton, KY",
        # its search flag unset) is a slice of them too: the fill searches it
        searched = (field.get("search") or field.get("paged") and len(options) >= PAGED_LIST_PAGE
                    or ans.rule in _PLACE_RULES and len(options) > PLACE_LIST_SLICE)
        # but a self-identification list is short and all there on opening, in the site's own
        # words: typing the profile's ("Decline to self-identify" into onsemi's Gender, whose
        # Oracle list is Female and Male) finds nothing and fails
        if chosen is None and searched and ans.rule not in _EEO_RULES and not isinstance(ans.value, (list, dict)):
            return ans
        if chosen is None:
            if ans.rule in _EEO_RULES and unmatched is not None:
                unmatched.append(ans)  # the profile answers it, but none of the choices is its answer
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


_TOPIC_WORDS = {"veteran": re.compile(r"\bveteran"), "disability": re.compile(r"\bdisabilit")}
_SELF_ID = re.compile(r"\bself ?identif")


def _self_identification(label: str, field: dict) -> tuple[str, str, Getter, int | None, set[str] | None] | None:
    """The rule of a veteran or disability self-identification question, told by its words and
    its choices together. Micron's veteran question carries the whole VEVRAA notice in its
    label, where "for more information, call ..." read as an on-call question ahead of the
    veteran one. A question about something else that mentions the topic ("Do you need an
    accommodation for a disability?") has no self-identification wording and no way to decline."""
    options = field.get("options") or []
    if field.get("kind") not in _CHOICE_KINDS or not options or _FAMILY.search(label):
        return None
    for rule in RULES:
        name, pattern = rule[0], rule[1]
        if name in _OPTION_TOPICS and _TOPIC_WORDS[name].search(label) \
                and sum(bool(re.search(pattern, norm(o))) for o in options) >= 2 \
                and (_SELF_ID.search(label) or any(is_decline(o) for o in options)):
            return rule
    return None


def _topic_from_options(options: list[str], prof: Profile, job: dict) -> Answer | None:
    """"Please check one of the boxes below:" on Workday's disability form: its choices ("Yes,
    I have a disability…", "No, I do not have a disability…") say what it asks."""
    for name, pattern, getter, _, _ in RULES:
        if name in _OPTION_TOPICS and sum(bool(re.search(pattern, norm(o))) for o in options) >= 2:
            return Answer(getter(prof, job), name)
    return None


_PLACE_RULES = {"address1", "postal", "city", "state", "home_state", "county"}

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


def is_eeo_rule(rule: str) -> bool:
    """Self-identification answers (gender, race, veteran, disability, ...): one of the list's
    own choices, picked from it, never typed in to search for it."""
    return rule in _EEO_RULES


_WEBSITE = re.compile(r"\b(web ?site|careers? (site|page|portal)|company site)\b", re.I)


_US = re.compile(r"(the )?(united states( of america)?|usa?)")
_WAIT = re.compile(r"\b(\d+|one|two|three|four|six|eight)[ -]?(day|week|month)s?\b")
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "six": 6, "eight": 8}


def _days(text: Any) -> int | None:
    """How long a start takes, in days: "Immediately" 0, "2 weeks after offer" 14, "30 Days" 30."""
    n = norm(text)
    if re.search(r"\b(immediate(ly)?|asap|right away)\b", n):
        return 0
    if len(re.findall(r"\d+", n)) > 1 or re.search(r"\b(to|or|between)\b", n):
        return None  # a range ("2-4 Weeks", "1 to 3 months"): not one wait
    m = _WAIT.search(n)
    if not m:
        return None
    count = int(m.group(1)) if m.group(1).isdigit() else _WORD_NUMBERS[m.group(1)]
    return count * {"day": 1, "week": 7, "month": 30}[m.group(2)]


def _same_wait(value: Any, options: list[str]) -> str | None:
    """The choice for the same start as the profile's earliest one, however each is worded
    ("2 weeks after offer" is Insight's "2 Week Notice"; a month is "30 Days"). Never a sooner
    or later one."""
    want = _days(value)
    if want is None:
        return None
    same = [o for o in options if (d := _days(o)) is not None and abs(d - want) <= 3]
    return same[0] if len(same) == 1 else None


def _own_website(value: Any, options: list[str], job: dict) -> str | None:
    """"Company Website" (how the person heard of the job) is the employer's own site, which
    a form may call "ONTO Website" or "Careers Site"."""
    if not re.search(r"\b(company|employer|career|careers|corporate)\b.*\b(web ?site|site|page)\b", str(value), re.I):
        return None
    sites = [o for o in options if _WEBSITE.search(o)]
    employer = set(norm(job.get("company") or "").split()) - {"inc", "corp", "corporation", "co", "ltd", "llc"}
    if len(sites) > 1:  # "ONTO Website" over "Other Website": the one naming the employer
        sites = [o for o in sites if employer & set(norm(o).split())] or sites
    if not sites:  # no website among them, but the employer itself (ASML's "ASML", nightly live check Oct 11)
        # (its whole name, never a word of it: "Texas" isn't Texas Instruments)
        sites = [o for o in options if norm(o) and set(norm(o).split()) == employer]
    if len(sites) != 1:
        return None
    # one site: the employer's, or any but another source's ("Indeed Website", "University Career Site": the review
    # of issue #112, note 27). Robert Half's "Website / Job Board Posting" is a group holding "Company Career Site"
    named = bool(employer & set(norm(sites[0]).split()))
    other = re.search(r"\b(indeed|linkedin|glassdoor|monster|ziprecruiter|handshake|university|college|school|"
                      r"other|agency|recruiter|referral)\b", sites[0], re.I)
    return sites[0] if named or not other else None


# Employers' first words that are everyday or places' words too: an answer that has one doesn't name the employer
# ("No, I've never applied" at Applied Materials; "Phoenix, Arizona" at Phoenix Children's)
_EVERYDAY_NAMES = {"applied", "phoenix", "arizona", "texas", "american", "national", "global", "united", "general",
                   "first", "advanced", "early", "blue", "capital", "bank", "state", "city", "county", "university",
                   "college", "health", "banner", "western", "southern", "northern", "eastern", "central", "pacific",
                   "digital", "public", "home", "valley", "desert", "mountain", "sun", "salt", "river"}

# What doesn't make an employer's name its own ("The Boeing Company", "Acme Technologies Inc")
_NAME_FILLER = {"the", "inc", "incorporated", "corp", "corporation", "co", "company", "group", "llc", "ltd", "plc",
                "technologies", "technology", "holdings", "international", "and"}


def is_how_heard(rule: str, label: str) -> bool:
    """The question of how the person heard of the job, answered from the profile or from an answer they
    remembered for it."""
    return rule == "how_heard" or rule.startswith("answers[") and bool(re.search(HOW_HEARD, norm(label)))


def answer_to_remember(label: str, answer: Any, company: str) -> Any | None:
    """What of a person's answer to keep for the questions of later applications, or None for nothing. An answer
    that names the employer it was given to ("Acme website", to how they heard of the job) isn't another's: for
    that question it's kept as "Company Website", which each form's list words as its own employer's site
    (_own_website); any other is for this application only (issue #112, note 27)."""
    words = [w for w in norm(company).split() if w not in _NAME_FILLER]
    if not words:
        return answer
    # its first word, or (where that's an everyday or a place's word: "Applied", "Phoenix") its whole name
    said = norm(answer).split()
    if words[0] in _EVERYDAY_NAMES:
        joined = " ".join(said)
        if not re.search(rf"\b{re.escape(' '.join(words))}\b", joined):
            return answer
    elif words[0] not in said:
        return answer
    if re.search(HOW_HEARD, norm(label)) and _WEBSITE.search(str(answer)):
        return "Company Website"
    return None


# not "Institution that issued your license": a licence's or certificate's, not a school's
_EDU_START = re.compile(r"^(school|university|college|institution)\b(?!.*\b(licen[cs]\w*|certif\w*))")
_EDU_FIELD = re.compile(r"^(school|university|college|institution|degree|discipline|major|field of study)\b"
                        r"(?!.*\b(licen[cs]\w*|certif\w*))")
# (not "Position Applied For", the job being applied to)
_JOB_FIELD = re.compile(r"^(company|employer|job title|title|position)\b(?! (applied|you are applying|of interest|desired|sought))")
_DATE_PART = re.compile(r"^(start|end|from|to)( date)?( (year|month))?$")
# The other boxes of a job's or school's block, which come between its name and its dates
_IN_BLOCK = re.compile(r"^(location|city|state|country|description|role description|responsibilit\w*|summary|"
                       r"gpa|grade|overall result|i currently|current(ly)?)\b")


def _with_context(fields: list[dict]) -> list[dict]:
    """Greenhouse-style forms put School, Degree, Discipline and "Start date year" together
    with no section heading: they're one school's, the first in the profile's education
    history, so its school is never given another school's degree. Unsectioned dates right
    after a job's boxes are that job's: a box of a block of its own, or another question
    between (an availability "Start Date" further down), ends the guess."""
    out, block, school = [], None, False
    for f in fields:
        label = norm(clean_label(f.get("label") or ""))
        if f.get("section") or not (_EDU_START.match(label) or _EDU_FIELD.match(label) or _JOB_FIELD.match(label)
                                    or _DATE_PART.match(label) or _IN_BLOCK.match(label)):
            block, school = None, False
        else:
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


def unset_choice(f: dict) -> bool:
    """A list still on its own first choice, where that says nothing is chosen: Taleo's "Not Specified" (Kforce's
    Closest Metropolitan City: Country, State/Province and Region, live run 10, Oct 2026), read as answered. A
    "Not Specified" picked further down a list (a self-identification's decline) is an answer."""
    options = f.get("options") or []
    value = norm(f.get("value"))
    return f.get("kind") == "select" and bool(options) and value in ("not specified", "unspecified") \
        and norm(options[0]) == value


def site_default(f: dict) -> bool:
    """A list on its first choice that has no empty one to start on (a gender list opening on "Male", a country code
    on "Afghanistan +93"): the site's own default, which no one chose. Read as answered, it went out as the person's
    answer, whatever the profile said. Once only (`judged`: the desk marks the list): a pick of it after that is the
    person's (the review)."""
    options = [str(o) for o in f.get("options") or []]
    return f.get("kind") == "select" and not f.get("multiple") and not f.get("judged") and len(options) > 1 \
        and norm(f.get("value")) == norm(options[0]) and not any(is_empty_value(o) for o in options)


def plan_autofill(fields: list[dict], prof: Profile, job: dict | None = None, overwrite: bool = False,
                  redo: set[str] | None = None) -> dict[str, Any]:
    """Split fields into ones we can fill and ones that need a decision. Not a sign-up's boxes
    beside the application (a talent community's email and consent: the form reader's aside)."""
    fields = _with_context([f for f in fields if not f.get("aside")])
    file_inputs = sum(1 for f in fields if f.get("kind") == "file")
    to_fill: list[dict] = []
    needs_input: list[dict] = []
    already: list[str] = []
    judged: list[str] = []  # lists on the site's first choice, filled or left as they are (site_default)
    for f in fields:
        # a read-only text box can't be typed in, but a read-only dropdown is a pick-only
        # menu (Infineon's "Preferred location"): it still takes a choice
        if f.get("disabled") or f.get("readonly") and f.get("kind") not in _CHOICE_KINDS:
            continue
        unset = unset_choice(f)
        # `redo`: lists the page marks invalid on a choice it made itself, which the desk may answer again (Northrop's
        # Eightfold phone "Country code" on Mexico, live run 13, Oct 2026: the review step's, once a job), and text
        # boxes it says are wrong showing what it wrote itself (EMD's resume parse's phone, live run 41, Oct 12)
        rejected = f["id"] in (redo or set()) and f.get("kind") in ("select", "listbox", "combobox", "radio_group", "text") \
            and not is_empty_value(f.get("value"))
        has_value = not is_empty_value(f.get("value")) and not unset and not rejected
        defaulted = has_value and site_default(f)
        if has_value and not overwrite and not defaulted:
            already.append(f["id"])
            continue
        missed: list[Answer] = []
        # (a list on its own "Not Specified" first choice: answered from the others, never that placeholder again)
        ans = resolve_field({**f, "options": (f.get("options") or [])[1:]} if unset else f, prof, job, file_inputs,
                            unmatched=missed)
        if defaulted and not overwrite and (ans is None and not missed or ans is not None and (
                ans.value == SKIP or choose_option(str(ans.value), [str(f.get("value"))], exact_only=True) is not None)):
            # (the site's first choice, where the profile says nothing else, or says the same: left as it is; the same
            # word for word, as "Bachelor's Degree" isn't an "Associate's Degree" a list opens on: the review)
            already.append(f["id"])
            judged.append(f["id"])
            continue
        if unset and not overwrite and ans is not None and is_eeo_rule(ans.rule) and not f.get("required"):
            # an optional self-identification list on its "Not Specified" stays as it is (the person may have
            # chosen it); a required one is answered (Kforce's "Birth Assigned Gender", live run 13)
            already.append(f["id"])
            continue
        # (a date's part showing the answer is typed again all the same: the page lost what was typed, and keeps only
        # what's shown: Entegris' Workday Self Identify Date, run 54)
        if rejected and not overwrite and (ans is None or ans.value == SKIP or is_eeo_rule(ans.rule)
                                           or (str(ans.value).strip() == str(f.get("value")).strip()
                                               and f.get("role") != "spinbutton"
                                               if f.get("kind") == "text"
                                               else choose_option(str(ans.value), [str(f.get("value"))]) is not None)):
            already.append(f["id"])  # (nothing else to give it, or the same answer: left to the person)
            continue
        if ans is not None and ans.value == SKIP:
            continue
        if ans is not None:
            to_fill.append({"id": f["id"], "label": f.get("label", ""), "value": ans.value, "rule": ans.rule})
            if defaulted:
                judged.append(f["id"])
        elif f.get("kind") != "password":
            # with the profile's answer the choices don't have (Qorvo's veteran list has no "I
            # don't wish to answer"), so the desk can say why it's empty
            # (a list on its "Not Specified" first choice is asked about without it: that choice is the empty one,
            # and an answer of it was taken for none and asked again, Kforce's Birth Assigned Gender, live run 24)
            asked = _asked({**f, "options": (f.get("options") or [])[1:]} if unset else f)
            # (a list on the site's first choice is never left empty: what it shows goes out, so it's asked as
            # required, optional or not: the review)
            needs_input.append({**asked, **({"unmatched": str(missed[0].value)} if missed else {}),
                                **({"required": True} if defaulted else {})})
    _graduation_of_the_school_given(fields, to_fill, needs_input, prof, job, file_inputs)
    _title_beside_its_company(fields, to_fill, needs_input, prof)
    # A form with no phone box of the profile's kind and no plain one, whose boxes name other kinds ("Home
    # Phone" alone, for a mobile): where only one of them is there, or only one is required, that's where
    # the form wants the number, so it's filled, as before boxes had kinds. Only a box the profile would
    # fill but for its kind (never a reference's, nor one that signs the person up for texts)
    if not any(t["rule"] == "phone" for t in to_fill) and prof.get("personal.phone"):
        def kind_only(f: dict) -> bool:
            kind = _phone_box_kind(norm(clean_label(f.get("label") or "")))
            if f.get("kind") != "text" or kind is None:
                return False
            as_kind = Profile({**prof.data, "personal": {**(prof.get("personal") or {}), "phone_type": kind}})
            got = resolve_field(f, as_kind, job or {}, file_inputs)
            return got is not None and got.rule == "phone"
        waiting = {q.get("id") for q in needs_input}
        boxes = [f for f in fields if f.get("id") in waiting and kind_only(f)]
        required = [f for f in boxes if f.get("required")]
        chosen = boxes[0] if len(boxes) == 1 else required[0] if len(required) == 1 else None
        if chosen is not None:
            needs_input[:] = [q for q in needs_input if q.get("id") != chosen["id"]]
            to_fill.append({"id": chosen["id"], "label": chosen.get("label", ""),
                            "value": _phone(prof, job or {}, norm(clean_label(chosen.get("label") or ""))), "rule": "phone"})
    # a question whose own "I decline to say" box is ticked (or being ticked) for a profile that declines it is
    # answered by that box (Nikon's UKG Pro: "Gender" with "I decline to say Gender" beneath it)
    def declined_by(f: dict) -> str | None:
        got = _DECLINE_BOX.match(clean_label(f.get("label") or ""))
        return norm(clean_label(got.group(1))) if got else None
    ticking = {f["id"] for f in fields if f.get("kind") == "checkbox" and f.get("value") is True} \
        | {t["id"] for t in to_fill if t["value"] is True and t["rule"] in _EEO_RULES}
    declined = {d for f in fields if f.get("id") in ticking and (d := declined_by(f))}
    if declined:
        needs_input[:] = [q for q in needs_input if norm(clean_label(q.get("label") or "")) not in declined]
    needs_input.sort(key=lambda f: not f.get("required"))
    return {"to_fill": to_fill, "needs_input": needs_input, "already_filled": already,
            **({"judged": judged} if judged else {})}


def _asked(f: dict) -> dict:
    # (and a date's part's role: the desk's card asks one for its own box, "spinbutton")
    return {k: f[k] for k in ("id", "kind", "label", "section", "sublabel", "required", "options", "role")
            if k in f and f[k] not in (None, [])}


_SCHOOL_FROM_ENTRY = re.compile(r"education_history\[(\d+)\]\.school")


def is_school_rule(rule: str) -> bool:
    """A School box filled from the profile: an education entry's, or the one school's."""
    return bool(_SCHOOL_FROM_ENTRY.fullmatch(rule or "")) or rule == "school"
# a box about another credential than the school given: its own year, education.graduation_year
_OTHER_CREDENTIAL = re.compile(r"\b(high school|ged|diploma|secondary)\b")


_BARE_TITLE = re.compile(r"(job |position |work )?title|position|role")
_COMPANY_BOX = re.compile(r"company( name)?|employer( name)?|organi[sz]ation( name)?")


def _title_beside_its_company(fields: list[dict], to_fill: list[dict], needs_input: list[dict],
                              prof: Profile) -> None:
    """A bare "Job Title" with no numbered block around it, beside a Company box that holds one of
    the profile's employers: that job's title. Cornerstone's resume reader (Linde's, Matheson's,
    live check Oct 11) adds an experience row with the company filled in and its title empty, under
    a heading the form reader doesn't tie to the boxes. Never when the company is none of the
    profile's, or two of its jobs were there."""
    asked = {q.get("id") for q in needs_input}
    planned = {t["id"]: t["value"] for t in to_fill}
    entries = [e for e in _listed(prof.get("work_history")) if isinstance(e, dict)]
    for i, f in enumerate(fields):
        if f.get("id") not in asked or f.get("kind") != "text":
            continue
        if not _BARE_TITLE.fullmatch(norm(clean_label(f.get("label") or ""))):
            continue
        around = fields[max(0, i - 4):i + 5]
        if any(_SOMEONE_ELSE.search(norm(f"{g.get('section') or ''} {clean_label(g.get('label') or '')}"))
               for g in around):
            continue  # a reference's or a supervisor's row: their position, not the person's
        near = [g for g in fields[max(0, i - 2):i] + fields[i + 1:i + 3]
                if _COMPANY_BOX.fullmatch(norm(clean_label(g.get("label") or "")))]
        if len(near) != 1:
            continue
        company = norm(planned.get(near[0]["id"], near[0].get("value")))
        jobs = [e for e in entries if company and _same_employer(norm(e.get("company")), company)]
        title = str(jobs[0].get("title") or "").strip() if len(jobs) == 1 else ""
        if title:
            needs_input[:] = [q for q in needs_input if q.get("id") != f["id"]]
            to_fill.append({"id": f["id"], "label": f.get("label", ""), "value": title, "rule": "work_title"})


def _graduation_of_the_school_given(fields: list[dict], to_fill: list[dict], needs_input: list[dict], prof: Profile,
                                    job: dict | None, file_inputs: int) -> None:
    """A form's School and Graduation Year came from two places: the school from an education
    entry (or, with no education.school, the first one), the year from education.graduation_year,
    which may be a diploma's or a GED's. Beside a college the person didn't finish they said
    the person graduated from it. Whatever the layout, the year follows the school the form
    was given: that school's own year when it was finished, else the person's to give."""
    entry = None
    for item in to_fill:
        if m := _SCHOOL_FROM_ENTRY.fullmatch(item["rule"]):
            entry = int(m.group(1))
        elif item["rule"] == "school" and not prof.get("education.school"):
            entry = 1  # _edu("school") falls back to the first entry
        if entry:
            break
    if not entry:
        return
    by_id = {f["id"]: f for f in fields}
    for item in [i for i in to_fill if i["rule"] == "grad_year"]:
        f = by_id[item["id"]]
        if _OTHER_CREDENTIAL.search(norm(clean_label(f.get("label") or ""))):
            continue
        ans = resolve_field({**f, "section": f"Education {entry}"}, prof, job, file_inputs)
        if ans is None or ans.value == SKIP:
            to_fill.remove(item)
            needs_input.append(_asked(f))
        else:
            item["value"], item["rule"] = ans.value, ans.rule
