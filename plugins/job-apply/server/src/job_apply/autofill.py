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
    {"united states", "united states of america", "usa", "us", "u s", "u s a", "america"},
    {"united kingdom", "uk", "great britain", "england"},
    {"netherlands", "the netherlands", "holland"},
    {"south korea", "korea republic of", "republic of korea", "korea"},
    {"taiwan", "taiwan province of china", "chinese taipei"},
]
_PLACEHOLDER_VALUES = re.compile(
    r"^(|select|select one|select\.\.\.|-+|choose|choose one|please select|none selected|--\s*select\s*--|mm/dd/yyyy|mm/yyyy)$",
    re.I,
)
_YES = re.compile(r"^(yes|y|true|i am\b(?! not)|i do\b(?! not)|i will\b(?! not)|i have\b(?! not)|i can\b(?! not)|agree)", re.I)
_NO = re.compile(r"^(no|n|false|i am not|i do not|i don'?t|i will not|i won'?t|i have not|i haven'?t|i can ?not|i can'?t)\b", re.I)
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


def degree_key(s: str) -> str | None:
    n = norm(s)
    for key, pattern in _DEGREES:
        if re.search(pattern, n):
            return key
    return None


def choose_option(desired: Any, options: list[str]) -> str | None:
    """Pick the option that best matches `desired`, or None if nothing does."""
    opts = [o for o in options if o and not _PLACEHOLDER_VALUES.match(o.strip())]
    if not opts or desired is None or desired == "":
        return None
    want = norm(desired)
    normed = [(o, norm(o)) for o in opts]

    # 1. exact, including aliases (AZ ~ Arizona, USA ~ United States of America)
    wanted = _aliases(want)
    for o, n in normed:
        if n in wanted or _aliases(n) & wanted:
            return o

    # 2. declines ("Decline to self-identify", "I don't wish to answer", ...)
    if _DECLINE.search(str(desired)):
        hits = [o for o, _ in normed if _DECLINE.search(o)]
        return hits[0] if hits else None

    # 3. yes / no questions
    pol = polarity(desired)
    if pol is not None and all(polarity(o) is not None for o, _ in normed if not _DECLINE.search(o)):
        hits = [o for o, _ in normed if polarity(o) is pol]
        if len(hits) == 1 or (hits and len(want.split()) == 1):
            return hits[0]
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
    contained = [o for o, n in normed if n and n in want]
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


def _yn(path: str) -> Getter:
    def g(prof: Profile, job: dict) -> Any:
        v = prof.get(path)
        if isinstance(v, bool):
            return "Yes" if v else "No"
        return v

    return g


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


def _previously_employed(prof: Profile, job: dict) -> str | None:
    company = norm(job.get("company"))
    if not company:
        return None
    past = [norm(c) for c in prof.get("history.previous_employers", []) or []]
    hit = any(c and (c in company or company in c) for c in past)
    return "Yes" if hit else "No"


def _today(prof: Profile, job: dict) -> str:
    return date.today().strftime("%m/%d/%Y")


def _relocate(prof: Profile, job: dict) -> Any:
    return _yn("preferences.willing_to_relocate")(prof, job)


def _travel(prof: Profile, job: dict) -> Any:
    v = prof.get("preferences.willing_to_travel")
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return v


# (rule name, label regex, getter, max label length or None, allowed kinds or None)
_TEXTY = {"text", "textarea", "select", "listbox", "combobox", "radio_group"}
RULES: list[tuple[str, str, Getter, int | None, set[str] | None]] = [
    ("how_heard", r"how did you (hear|find|learn)|where did you (hear|find|learn)|source of (application|referral)|^source$", _p("preferences.how_did_you_hear"), None, None),
    # contact details: short labels only, so long questions that merely mention
    # "state" or "name" don't match
    ("email", r"^(confirm |re ?enter |verify )?e ?mail( address)?( again)?$|^(your )?email\b", _p("personal.email"), 45, None),
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
    ("postal", r"zip|postal|post code|postcode", _p("personal.address.postal_code"), 45, None),
    ("county", r"^county", lambda p, j: p.get("personal.address.county"), 45, None),
    ("state", r"^state|province|^region|state province", _state, 45, None),
    ("country", r"^country( region)?( of residence)?$|^country", _p("personal.address.country"), 45, None),
    ("linkedin", r"linked ?in", _p("personal.linkedin_url"), 60, {"text", "textarea"}),
    ("github", r"github", _p("personal.github_url"), 45, {"text"}),
    ("website", r"website|portfolio|personal (site|url)|^url$|blog", _p("personal.website"), 45, {"text"}),
    ("current_company", r"(current|most recent|present) (employer|company)", _p("experience.current_company"), 60, None),
    ("current_title", r"(current|most recent|present) (job )?(title|position|role)", _p("experience.current_title"), 60, None),
    ("total_years", r"^(total )?years of (professional |work )?experience$", _p("experience.total_years"), 60, None),
    ("degree", r"highest (level of )?(education|degree)|^degree$|education level", _edu("highest_degree", "degree"), 80, None),
    ("school", r"^(school|university|college|institution)\b", _edu("school", "school"), 45, None),
    ("major", r"^(major|field of study|discipline|area of study)", _edu("major", "major"), 45, None),
    ("gpa", r"^gpa|grade point", _edu("gpa", "gpa"), 45, None),
    ("grad_year", r"graduation (year|date)|year of graduation", _edu("graduation_year", "end"), 60, None),
    ("signature", r"(electronic |e )?signature|sign your (full )?name", _full_name, 80, {"text"}),
    ("signed_date", r"today s date|date signed|signature date", _today, 45, {"text"}),
    # questions (any length)
    ("over_18", r"(18|eighteen) years|at least 18|over the age|age of 18|legal age", _yn("work_authorization.over_18"), None, None),
    ("sponsorship", r"sponsor", _yn("work_authorization.requires_sponsorship"), None, None),
    ("authorized", r"authori[sz]ed to work|eligible to work|legally (able|permitted|allowed) to work|right to work|work authori[sz]ation|employment eligibility", _yn("work_authorization.authorized_to_work"), None, None),
    ("us_person", r"u ?s person|itar|export (control|administration|regulation)|\bear\b", _yn("work_authorization.us_person"), None, None),
    ("us_citizen", r"are you a (u s |united states )?citizen", _yn("work_authorization.us_citizen"), None, None),
    ("citizenship", r"citizenship|country of citizen|are you a (u ?s )?citizen", _p("work_authorization.citizenship"), None, None),
    ("clearance", r"security clearance|active clearance", _p("work_authorization.security_clearance"), None, None),
    ("relocate", r"relocat", _relocate, None, None),
    ("travel", r"travel", _travel, None, None),
    ("shift", r"shift work|rotating shift|nights and weekends|work (nights|weekends)|on ?call", _yn("preferences.flexible_schedule"), None, None),
    ("salary", r"salary|compensation|pay (expectation|requirement)|desired pay|expected pay", _p("preferences.desired_salary"), None, None),
    ("start_date", r"start date|available to start|earliest (date|start)|when can you start|notice period", _p("preferences.earliest_start"), None, None),
    ("previous_employee", r"(previously|ever|formerly) (been )?(employed|worked)|former employee|have you (ever )?worked (for|at)|worked .{0,40} before", _previously_employed, None, None),
    # voluntary self-identification
    ("sexual_orientation", r"sexual orientation", _p("eeo.sexual_orientation"), None, None),
    ("gender", r"\bgender\b|\bsex\b", _p("eeo.gender"), None, None),
    ("hispanic", r"hispanic|latin[oa]", _p("eeo.hispanic_latino"), None, None),
    ("race", r"\brace\b|ethnicity|ethnic", _p("eeo.race"), None, None),
    ("veteran", r"veteran", _p("eeo.veteran"), None, None),
    ("disability", r"disabilit", _p("eeo.disability"), None, None),
    ("pronouns", r"pronoun", _p("personal.pronouns"), None, None),
]


def _answer_bank(prof: Profile, label: str) -> Answer | None:
    for item in prof.get("answers", []) or []:
        if not isinstance(item, dict) or not item.get("match") or item.get("answer") in (None, ""):
            continue
        try:
            if re.search(str(item["match"]), label, re.I):
                return Answer(item.get("answer"), f"answers[{item['match']}]")
        except re.error:
            if str(item["match"]).lower() in label.lower():
                return Answer(item.get("answer"), f"answers[{item['match']}]")
    return None


def _document(prof: Profile, job: dict, kind: str) -> str | None:
    """Prefer a tailored file in the job's folder, then the profile default."""
    folder = Path(job["folder"]) if job.get("folder") else None
    stem = "cover_letter" if kind == "cover_letter" else "resume"
    if folder and folder.exists():
        for ext in (".pdf", ".docx", ".doc"):
            # resume.pdf, Sam_Rivera_Resume.pdf, Sam_Rivera_Cover_Letter.pdf ...
            hits = sorted(p for p in folder.glob(f"*{ext}") if p.stem.lower().endswith(stem))
            if hits:
                return str(hits[0])
    p = expand(prof.get(f"documents.{kind}"))
    return str(p) if p and p.exists() else None


SKIP = "__skip__"  # deliberately left empty, e.g. the end date of a current job
# Legal attestations answered only when the exact choices are known.
_NEEDS_OPTIONS = {"us_person", "citizenship", "us_citizen", "clearance"}

_ENTRY_SECTIONS = [
    ("education_history", r"education|school|degree"),
    ("work_history", r"experience|employment|work history|position|job"),
]
_WORK_FIELDS = [
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
    if attr == "current":
        return True, Answer(current, rule)
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
            chosen = choose_option(ans.value, field["options"])
            return Answer(chosen, ans.rule) if chosen else None
        return ans

    if kind == "file":
        where = f"{label} {norm(field.get('section'))}"
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
        ans = _answer_bank(prof, raw_label)
        if ans is not None:
            pol = polarity(ans.value)
            return Answer(pol if pol is not None else bool(ans.value), ans.rule)
        return None

    ans = _answer_bank(prof, raw_label)
    if ans is None:
        for name, pattern, getter, max_len, kinds in RULES:
            if max_len is not None and len(label) > max_len:
                continue
            if kinds is not None and kind not in kinds:
                continue
            if re.search(pattern, label):
                value = getter(prof, job)
                if value is None or value == "":
                    return None  # recognised but the profile has no answer
                ans = Answer(value, name)
                break
    if ans is None or ans.value is None or ans.value == "":
        return None

    if kind == "textarea" and not ans.rule.startswith("answers[") and ans.rule not in {"linkedin", "salary", "start_date"}:
        return None
    if isinstance(ans.value, bool):
        ans.value = "Yes" if ans.value else "No"

    options = field.get("options")
    if kind in {"select", "radio_group", "listbox", "checkbox_group", "combobox"} and options:
        chosen = choose_option(ans.value, options)
        if chosen is None:
            return None
        return Answer(chosen, ans.rule)
    if kind == "combobox" and ans.rule in _NEEDS_OPTIONS:
        return None  # an attestation we won't answer without seeing the exact choices
    return ans


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
        if f.get("disabled") or f.get("readonly"):
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
