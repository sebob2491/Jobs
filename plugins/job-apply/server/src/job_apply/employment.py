"""A job's employment type: full-time, part-time, contract, temporary, internship, per diem (the design of Oct 18,
F6).

Read only from the fields job systems give for it, as each words it: Workday's timeType ("Full time"), Lever's
commitment ("Full-time", "Intern"), SmartRecruiters' typeOfEmployment ("Full-time"), Oracle's worker type and job
schedule ("Contingent Worker", "Part time"), JSON-LD's employmentType ("FULL_TIME", "PER_DIEM"), Paycom's positionType
("Full Time"), Phoenix Children's schedule ("Full-Time", "PRN"), ASML's job_type and Indeed's job_type. A field is the
system's own word for the job, so a type read from it is sure; never from the posting's sentences. A value that
names none ("Regular", "Employee", "Other", ASML's "Fix") says nothing, and no filter hides that job.

A field may say two things, apart or together: the job's schedule (full or part time) and its arrangement (contract,
temporary, internship, per diem). A filter judges only what the field says: a Workday internship whose timeType says
"Full time" stays when Internship is pressed.
"""

from __future__ import annotations

import re
from typing import Any

TYPES = ("full_time", "part_time", "contract", "temporary", "internship", "per_diem")
WORDS = {"full_time": "Full-time", "part_time": "Part-time", "contract": "Contract", "temporary": "Temporary",
         "internship": "Internship", "per_diem": "Per diem"}
SCHEDULE = ("full_time", "part_time")
ARRANGEMENT = ("contract", "temporary", "internship", "per_diem")
_SAID = {
    "full_time": re.compile(r"\bfull\s*time\b(?!\s+equivalent)|\bfull\s+(?:or\s+|and\s+)?part\s*time\b|\bft\b|"
                            # (to hire, to perm: a full-time job after its contract or temp time)
                            r"\b(?:temp|contract)\s+to\s+(?:hire|perm)\b"),
    "part_time": re.compile(r"\bpart\s*time\b|\bpt\b"),
    "contract": re.compile(r"\bcontract(?:or|ors|ed)?\b|\bcontingent\b|\b1099\b|\bfreelance\b|\bc2h\b"),
    "temporary": re.compile(r"\btemp(?:orary)?\b|\bseasonal\b|\b(?:fixed|limited)\s+term\b"),
    "internship": re.compile(r"\bintern(?:ship|ships)?\b|\bco\s*op\b"),
    "per_diem": re.compile(r"\bper\s*diem\b|\bprn\b|\bas\s+needed\b"),
}
# a type said not to be ("Non-Contingent", "Not Temporary"), and "Full Time Equivalent 0.6" (a share of full time)
_NOT = re.compile(r"\b(?:non|not|no)\s+(?:a\s+)?\w+|\bfull\s+time\s+equivalent\b[\s\d.]*")


def types_of(value: Any) -> list[str]:
    """The employment types a system's field names ("Full-time", "FULL_TIME, TEMPORARY", "Part Time / PRN",
    "Contingent Worker", "Temp to Hire": temporary, and full time after), in TYPES' order; [] when it names none (or
    only says one isn't: "Non-Contingent", "Not Temporary")."""
    words = " ".join(re.sub(r"[_\-/,;|()]+", " ", str(value or "").lower()).split())
    words = _NOT.sub(" ", words)
    return [t for t in TYPES if _SAID[t].search(words)]


def shown(types: list[str]) -> str:
    """The types as the desk's row says them: "Full-time", "Full-time / Temporary"."""
    return " / ".join(WORDS[t] for t in types if t in WORDS)


def parse_choice(value: Any) -> list[str] | None:
    """An employment_type filter as a tool is given it ("full_time", "full-time, contract", "Part time", "co-op",
    "seasonal", "contract-to-hire"): the types named, in TYPES' order; [] for none; None when a word in it isn't one."""
    if value is None:
        return []
    names = [re.sub(r"[\s-]+", "_", w.strip().lower()) for w in re.split(r"[,|/]", str(value))]
    aliases = {"fulltime": "full_time", "parttime": "part_time", "temp": "temporary", "intern": "internship",
               "perdiem": "per_diem", "prn": "per_diem", "contractor": "contract", "coop": "internship",
               "seasonal": "temporary", "contracttohire": "contract", "temptohire": "temporary"}
    names = [aliases.get(n.replace("_", ""), n) for n in names if n]
    if not all(n in TYPES for n in names):
        return None
    return [t for t in TYPES if t in names]


def keeps(types: list[str], chosen: list[str]) -> bool:
    """Whether a job of `types` stays under a filter for `chosen`, judged on each of the two things a field may say
    (SCHEDULE, ARRANGEMENT) that both the job's field and the filter name: there, one of the job's must be chosen. So a
    job whose field doesn't say its type stays, as does a "Full time" internship when Internship is pressed."""
    for axis in (SCHEDULE, ARRANGEMENT):
        mine, asked = [t for t in types if t in axis], [t for t in chosen if t in axis]
        if mine and asked and not set(mine) & set(asked):
            return False
    return True
