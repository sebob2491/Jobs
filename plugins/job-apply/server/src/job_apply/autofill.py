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
    r"^(-+\s*)?(|select|select one|select an option|select\.\.\.|choose|choose one|choose an option|please select|"
    r"please select one|please choose|none selected|no selection)(\s*-+)?$|"
    r"^(-+|mm/dd/yyyy|mm/yyyy)$",  # "No Selection": SuccessFactors' empty dropdowns
    re.I,
)
_YES = re.compile(r"^(yes|y|true|i am\b(?! not)|i do\b(?! not)|i will\b(?! not)|i have\b(?! not)|i can\b(?! not)|agree)", re.I)
_NO = re.compile(r"^(no|n|false|never|i am not|i do not|i don'?t|i will not|i won'?t|i have not|i haven'?t|i have never|"
                 r"i'?ve never|i can ?not|i can'?t)\b", re.I)
_FILLER = {"yes", "no", "y", "n", "i", "am", "a", "an", "the", "to", "for", "of", "in", "my", "and", "or", "is", "be",
           "this", "it", "up"}
PAGED_LIST_PAGE = 100  # entries SuccessFactors' paginated select lists at a time
_DECLINE = re.compile(r"decline|not (wish|want) to|prefer not|choose not|do not want|don'?t wish|not to (answer|disclose|self)|rather not", re.I)


def norm(s: Any) -> str:
    s = str(s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9+ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_label(label: str) -> str:
    label = re.sub(r"\(required\)|\brequired\b", " ", label or "", flags=re.I)
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
        if re.search(r"less than|under|below|fewer than", n):
            low, high, high_open = float("-inf"), nums[0], True
        elif re.search(r"or less|or fewer|or below", n) and len(nums) == 1:
            low, high, high_open = float("-inf"), nums[0], False
        elif re.search(r"more than|over|greater than|above|or more|or higher|\+|plus", n) and len(nums) == 1:
            low, high, high_open = nums[0], float("inf"), False
            if re.search(r"more than|over|greater than", n):
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
    if exact_only:
        return None
    if names:
        return _containing(want, normed)

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
            entries = [e for e in prof.get("education_history", []) or [] if isinstance(e, dict)]
            value = entries[0].get(entry_key) if entries else None
        return value
    return g


def _full_name(prof: Profile, job: dict) -> str | None:
    return prof.full_name or None


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


def _previously_employed(prof: Profile, job: dict, label: str = "") -> str | None:
    """Has the person worked for this employer before? Only for a question about this
    employer: "Have you ever worked in a cleanroom?" isn't one. The answer comes from the
    employers in the profile, past and present (history.previous_employers, work_history)."""
    company = norm(job.get("company"))
    if not company:
        return None
    asked = norm(label)
    names_it = company.split()[0] in asked.split()
    if not (names_it or re.search(r"(employed|worked) (by|for|at|with) (us|this|our|the company)\b|former employee", asked)):
        return None
    past = [norm(c) for c in prof.get("history.previous_employers", []) or []]
    past += [norm(e.get("company")) for e in prof.get("work_history", []) or [] if isinstance(e, dict)]
    return "Yes" if any(_same_employer(c, company) for c in past) else "No"


def _graduated(prof: Profile, job: dict) -> Any:
    """The year the person graduated: never the last year at a school they didn't finish
    (an education_history entry with no degree), which would say they graduated."""
    year = prof.get("education.graduation_year")
    if year:
        return year
    finished = [e for e in prof.get("education_history", []) or [] if isinstance(e, dict) and e.get("degree")]
    return finished[0].get("end") if finished else None


def _no_sponsorship(prof: Profile, job: dict, label: str = "") -> Any:
    """Yes means no sponsorship is needed ("…without sponsorship?", "…and do not require
    sponsorship?"). Asked together with being authorized, Yes needs both."""
    needs = prof.get("work_authorization.requires_sponsorship")
    if not isinstance(needs, bool):
        return None
    if re.search(r"authori[sz]ed|eligible|legally", norm(label)):
        allowed = prof.get("work_authorization.authorized_to_work")
        if allowed is False or needs:
            return "No"
        return "Yes" if allowed is True else None
    return "No" if needs else "Yes"


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


def _travel(prof: Profile, job: dict, label: str = "") -> Any:
    v = prof.get("preferences.willing_to_travel")
    if isinstance(v, bool):
        return "Yes" if v else "No"
    asked, mine = re.findall(r"(\d+) ?%", label), re.findall(r"(\d+) ?%", str(v or ""))
    if asked and mine and max(map(int, asked)) > max(map(int, mine)):
        return None  # more travel than the profile agrees to: the user decides
    return v


_SOMEONE_ELSE = re.compile(r"\b(referen\w*|referee\w*|emergency|next of kin|supervisor\w*|manager\w*|referr\w*|contact person)\b")
_CONTACT_RULES = {"email", "first_name", "middle_name", "last_name", "preferred_name", "full_name", "phone_type",
                  "phone_code", "phone_ext", "phone", "address1", "address2", "city", "postal_ext", "postal",
                  "county", "state", "country"}

# Getters that read the question itself, not only the profile
_READS_QUESTION = {_travel, _previously_employed, _no_sponsorship}

# (rule name, label regex, getter, max label length or None, allowed kinds or None)
_TEXTY = {"text", "textarea", "select", "listbox", "combobox", "radio_group"}
RULES: list[tuple[str, str, Getter, int | None, set[str] | None]] = [
    ("how_heard", r"how did you (hear|find|learn)|where did you (hear|find|learn)|source of (application|referral)|^source$", _p("preferences.how_did_you_hear"), None, None),
    # contact details: short labels only, so long questions that merely mention
    # "state" or "name" don't match
    ("email", r"^(confirm |re ?enter |re ?type |verify )?e ?mail( address)?( again)?$|^(your )?email\b|^enter (your )?e ?mail\b",
     _p("personal.email"), 45, None),  # "Enter email to start application process" (Qorvo)
    ("first_name", r"^(legal )?(first|given)( name)?$|^(legal )?first name|^given name|^forename", _p("personal.first_name"), 45, None),
    ("middle_name", r"^middle (name|initial)", _p("personal.middle_name"), 45, None),
    ("last_name", r"^(legal )?(last|family|sur)( ?name)?$|^(legal )?(last|family) name|^surname", _p("personal.last_name"), 45, None),
    ("preferred_name", r"^preferred (first )?name|^nick ?name", lambda p, j: p.get("personal.preferred_name") or p.get("personal.first_name"), 45, None),
    ("full_name", r"^(full |legal |your |candidate )?(full )?name$|^full (legal )?name|^legal name", _full_name, 45, None),
    ("phone_type", r"phone (device )?type|type of phone", lambda p, j: p.get("personal.phone_type", "Mobile"), 45, None),
    ("phone_code", r"(country|phone) (phone )?code|^country code", _phone_code, 45, None),
    ("phone_ext", r"extension", lambda p, j: None, 45, None),
    ("phone", r"phone|mobile|cell|telephone", _p("personal.phone"), 45, {"text", "combobox"}),
    ("address2", r"address line 2|^address 2|apartment|suite|^apt|^unit", _p("personal.address.line2"), 45, None),
    ("address1", r"address line 1|^address 1|^street|^(home |mailing |street )?address$", _p("personal.address.line1"), 45, None),
    ("city", r"^city|town|location city|current city|city of residence", _p("personal.address.city"), 45, None),
    # Oracle's "Zip Code+4" wants the 4-digit extension, not the ZIP: left for the site to fill
    ("postal_ext", r"zip( code)? ?(\+|plus) ?4|^zip ?4$|zip (code )?extension", lambda p, j: None, 45, None),
    ("postal", r"zip|postal|post code|postcode", _p("personal.address.postal_code"), 45, None),
    ("county", r"^county", lambda p, j: p.get("personal.address.county"), 45, None),
    ("state", r"^state|province|^region|state province", _state, 45, None),
    # the address's country: not "Country of citizenship / birth" (those are other questions)
    ("country", r"^country\b(?!.*\b(citizen|birth|born|nationalit|passport|origin)\w*)", _p("personal.address.country"), 45, None),
    ("linkedin", r"linked ?in", _p("personal.linkedin_url"), 60, {"text", "textarea"}),
    ("github", r"github", _p("personal.github_url"), 45, {"text"}),
    ("website", r"website|portfolio|personal (site|url)|^url$|blog", _p("personal.website"), 45, {"text"}),
    ("current_company", r"(current|most recent|present) (employer|company)", _p("experience.current_company"), 60, None),
    ("current_title", r"(current|most recent|present) (job )?(title|position|role)", _p("experience.current_title"), 60, None),
    ("total_years", r"^(total )?years of (professional |work )?experience$", _p("experience.total_years"), 60, None),
    ("degree", r"highest (level of )?(education|degree)|^degree$|education level", _edu("highest_degree", "degree"), 80, None),
    ("school", r"^(school|university|college|institution)\b(?!.*\b(major|degree|gpa|city|state|location|country|year|date|address)\b)",
     _edu("school", "school"), 45, None),
    ("major", r"^(major|field of study|discipline|area of study)", _edu("major", "major"), 45, None),
    ("gpa", r"^gpa|grade point", _edu("gpa", "gpa"), 45, None),
    ("grad_year", r"graduation (year|date)|year of graduation", _graduated, 60, None),
    ("signature", r"(electronic |e )?signature|sign your (full )?name", _full_name, 80, {"text"}),
    # questions (any length)
    # the same facts asked the other way round come first
    ("under_18", r"under (the age of )?(18|eighteen)|younger than (18|eighteen)", _yn("work_authorization.over_18", invert=True), None, None),
    ("over_18", r"(18|eighteen) years|at least 18|over the age of (18|eighteen)\b|age of 18|legal age", _yn("work_authorization.over_18"), None, None),
    # Yes means no sponsorship: "…without sponsorship?", "…and do not require sponsorship?"
    ("no_sponsorship", r"without .{0,40}sponsor|(do not|don t|dont|will not|won t|not|never) (now or in the future |currently |ever )?"
     r"(require|need)\w* .{0,40}sponsor|no (need|requirement) for .{0,30}sponsor", _no_sponsorship, None, None),
    ("sponsorship", r"sponsor", _yn("work_authorization.requires_sponsorship"), None, None),
    ("authorized", r"authori[sz]ed to work|eligible to work|legally (able|permitted|allowed) to work|right to work|work authori[sz]ation|employment eligibility", _authorized, None, None),
    # Only questions that ask whether you are a U.S. person: export-control wording
    # also comes with other questions, e.g. Micron's "are you a citizen of Cuba, Iran ...?"
    ("us_person", r"\bu ?s person\b|citizen.{0,80}(permanent resident|green card|refugee|asyl|protected individual)",
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
     lambda p, j: None, None, None),
    # relocation money, or a role with none: not whether the person would move
    ("relocation_help", r"relocat\w* (assistance|package|benefit|reimburse|support|expense|allowance)|no relocat|"
     r"(assistance|help) .{0,20}relocat", lambda p, j: None, None, None),
    ("relocate", r"relocat", _relocate, None, None),
    ("travel", r"travel", _travel, None, None),
    ("shift", r"shift work|rotating shift|nights and weekends|work (nights|weekends)|on ?call", _yn("preferences.flexible_schedule"), None, None),
    # desired_salary is one yearly figure: not an answer to "current salary" or a monthly/hourly rate
    ("other_salary", r"(current|last|previous|present|most recent|drawn) .{0,25}(salary|compensation|pay\b)"
     r"|(monthly|per month|hourly|per hour|weekly|per week) .{0,25}(salary|compensation|pay\b|rate)", lambda p, j: None, None, None),
    ("salary", r"salary|compensation|pay (expectation|requirement)|desired pay|expected pay", _p("preferences.desired_salary"), None, None),
    ("start_date", r"start date|available to start|earliest (date|start)|when can you start|notice period", _p("preferences.earliest_start"), None, None),
    ("previous_employee", r"(previously|ever|formerly) (been )?(employed|worked)|former employee|have you (ever )?worked (for|at)|worked .{0,40} before",
     _previously_employed, None, None),  # (only about this employer: see _previously_employed)
    # voluntary self-identification
    ("sexual_orientation", r"sexual orientation", _p("eeo.sexual_orientation"), None, None),
    ("gender", r"\bgender\b|\bsex\b", _p("eeo.gender"), None, None),
    ("hispanic", r"hispanic|latin[oa]", _p("eeo.hispanic_latino"), None, None),
    ("race", r"\brace\b|ethnicity|ethnic", _p("eeo.race"), None, None),
    ("veteran", r"veteran", _p("eeo.veteran"), None, None),
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
    r"\bfamily members?\b|\bsubsidiar|\baffiliate", re.I)


def _bare_question(text: str) -> str:
    return " ".join(re.sub(r"\(required\)|\*", " ", text or "", flags=re.I).strip(" :?.").split()).lower()


def _answer_bank(prof: Profile, label: str, job: dict | None = None) -> Answer | None:
    company = norm((job or {}).get("company"))
    for item in prof.get("answers", []) or []:
        if not isinstance(item, dict) or not item.get("match") or item.get("answer") in (None, ""):
            continue
        if item.get("question"):
            # answered in the Job Desk: that question, worded the same. "I agree" isn't "I agree
            # to binding arbitration", and "Are you currently employed?" isn't "... by ASML?"
            if _bare_question(str(item["question"])) != _bare_question(label):
                continue
            source = norm(str(item.get("from") or ""))
            if source and source != company and (_EMPLOYER_SPECIFIC.search(label) or source in norm(label)):
                continue  # about the company it was given for
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
    entries = [e for e in prof.get(key, []) or [] if isinstance(e, dict)]
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
    if attr == "end" and key == "education_history" and "graduat" in label and not entry.get("degree"):
        return True, None  # a school not finished: no graduation date to give
    if attr == "end" and current:
        return True, Answer(SKIP, rule)
    if attr in ("start", "end"):
        value = _date_value(field, entry.get(attr))
        return True, (Answer(value, rule) if value else None)
    value = entry.get(attr)
    return True, (Answer(str(value).strip(), rule) if value not in (None, "") else None)


def resolve_field(field: dict, prof: Profile, job: dict | None = None, file_inputs_on_page: int = 1) -> Answer | None:
    job = job or {}
    kind = field.get("kind", "text")
    raw_label = field.get("label") or field.get("name") or ""
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
        if re.search(r"resume|cv|curriculum", where) or file_inputs_on_page == 1:
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
    if ans is None and kind == "text" and len(label) <= 45 and _SIGNED_DATE.search(label):
        return Answer(_today_for(field), "signed_date")
    someone_else = _SOMEONE_ELSE.search(f"{label} {norm(field.get('section'))}")
    if ans is None:
        for name, pattern, getter, max_len, kinds in RULES:
            if someone_else and name in _CONTACT_RULES:
                continue  # a reference's or an emergency contact's name, phone or email
            if max_len is not None and len(label) > max_len:
                continue
            if kinds is not None and kind not in kinds:
                continue
            if re.search(pattern, label):
                value = getter(prof, job, raw_label) if getter in _READS_QUESTION else getter(prof, job)
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
        chosen = choose_option(ans.value, options)
        if chosen is None and ans.rule == "how_heard":
            chosen = _own_website(ans.value, options, job)
        # a search prompt lists only its top level, and a full page of a paged list (Qorvo's
        # countries stop at Iran) only its start: the fill searches them for the answer. A
        # shorter paged list is all there is, so a search can't find anything else in it.
        searched = field.get("search") or field.get("paged") and len(options) >= PAGED_LIST_PAGE
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


def place_words(rule: str, prof: Profile) -> list[str]:
    """For an address field: the rest of the profile's address, which tells apart the entries
    of a place lookup (Oracle's City lists "Chandler, Henderson, TX" before "Chandler,
    Maricopa, AZ"). Empty for any other field."""
    if rule not in _PLACE_RULES:
        return []
    words = [prof.get("personal.address.city"), prof.get("personal.address.postal_code"),
             prof.get("personal.address.county")]
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
    return bool(re.search(r"\.(school|company|employer)$", rule or ""))


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


_EDU_FIELD = re.compile(r"^(school|university|college|institution|degree|discipline|major|field of study)\b")
_JOB_FIELD = re.compile(r"^(company|employer|job title|title|position)\b")
_DATE_PART = re.compile(r"^(start|end|from|to)( date)?( (year|month))?$")


def _with_context(fields: list[dict]) -> list[dict]:
    """Greenhouse-style forms put "Start date year" right after School/Degree with no
    section heading; treat such unsectioned date fields as belonging to that block."""
    out, block = [], None
    for f in fields:
        label = norm(clean_label(f.get("label") or ""))
        if not f.get("section"):
            if _EDU_FIELD.match(label):
                block = "Education 1"
            elif _JOB_FIELD.match(label):
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
