"""Where a job is done: remote, hybrid or on-site (the design of Oct 18, F3).

Read, best first, from a job system's own field for it (JSON-LD's jobLocationType TELECOMMUTE, Paycom's remoteType,
iCIMS's "Location Type" and "Remote Location Eligible", SuccessFactors' "On-Site" line under the title, and the
fields Oracle, Workday, Lever and SmartRecruiters give when they give them), then the words of the job's location
("Remote - US", "Phoenix, AZ (Hybrid)", the State of Arizona's "REMOTE OPTIONS"), and only last from the posting's
sentences ("this is a fully remote role", "hybrid schedule").

As with pay (pay.py), only a sure reading can hide a job: a field's or the location's. One read from the posting's
sentences may be about something else ("remote diagnostics", "no remote work during training"), so it is shown,
marked as read from the text, and the Workplace filter never hides the job for it. A job that doesn't say is
"unknown" ("" here), and no filter hides it.
"""

from __future__ import annotations

import re
from typing import Any

WORKPLACES = ("remote", "hybrid", "onsite")
WORDS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}
# where a reading came from, best first: a better one replaces a worse (Tracker, recommend's take)
SOURCES = {"structured": 3, "location": 2, "text": 1}
FIELDS = ("workplace", "workplace_source")

_REMOTE = re.compile(r"(?i)remote|tele-?commut|tele-?work|work\s*from\s*home|\bwfh\b|virtual|anywhere")
_HYBRID = re.compile(r"(?i)hybrid|flex(?:ible)?\s+(?:work|remote|location)")
_ONSITE = re.compile(r"(?i)on[\s_-]*site|in[\s_-]*(?:the\s+)?office|in[\s_-]*person|on[\s_-]*premises?|\boffice\b|"
                     r"not\s+remote|\bno\s+remote")
_NOT_REMOTE = re.compile(r"(?i)\b(?:not|non)[\s-]+remote|\bno\s+remote")


def from_field(value: Any) -> str:
    """A job system's own field for where the job is done ("TELECOMMUTE", "Remote", "Hybrid", "On-Site", "Onsite",
    "In Office", Oracle's "ORA_HYBRID", iCIMS's "remoteLocal") as remote, hybrid or onsite; "" for a value that says
    none or several ("Flexible", "Remote or On-site", "unspecified")."""
    words = " ".join(str(value or "").replace("_", " ").split())
    if not words:
        return ""
    if _HYBRID.search(words):
        return "hybrid"
    remote = bool(_REMOTE.search(words)) and not _NOT_REMOTE.search(words)
    onsite = bool(_ONSITE.search(words))
    return "" if remote == onsite else "remote" if remote else "onsite"


_PLACE_REMOTE = re.compile(r"(?i)\bremote\b|\btele-?(?:work|commute)\b|\bwork\s+from\s+home\b|\bwfh\b|\bvirtual\b")
_PLACE_HYBRID = re.compile(r"(?i)\bhybrid\b")
_PLACE_ONSITE = re.compile(r"(?i)\bon-?\s?site\b|\bin[\s-]office\b")


def in_location(location: str) -> str:
    """Where a job is done as its location says it: "Remote - US", "US-Remote", "Remote (Arizona)" are remote (a job
    with a place and a remote one, "Phoenix, AZ; Remote", can be done remotely too); "Phoenix, AZ (Hybrid)" hybrid;
    "Chandler, AZ - Onsite" on-site; "" for a place alone, which says nothing of it."""
    found = set()
    for place in str(location or "").split(";"):
        if _PLACE_HYBRID.search(place):
            found.add("hybrid")
        elif _PLACE_REMOTE.search(place) and not _NOT_REMOTE.search(place):
            found.add("remote")
        elif _PLACE_ONSITE.search(place):
            found.add("onsite")
    return next((w for w in WORKPLACES if w in found), "")


# The posting's own sentences, for a job that says it nowhere else: unsure (never hides a job)
_TEXT_HYBRID = re.compile(r"(?i)\bhybrid[\s-]+(?:position|role|job|opportunity|schedule|work(?:ing)?|model|"
                          r"arrangement|environment|setting)\b|\bthis\s+is\s+a\s+hybrid\b")
_TEXT_NOT_REMOTE = re.compile(r"(?i)\b(?:is\s+not|isn't|not)\s+(?:a\s+)?(?:fully\s+)?remote\b|\bno\s+remote\s+work\b|"
                              r"\bnon-remote\b")
_TEXT_REMOTE = re.compile(r"(?i)\b(?:fully|100%|completely|entirely|100\s+percent)\s+remote\b|"
                          r"\bremote[\s-]+(?:position|role|job|opportunity)\b|\bthis\s+is\s+a\s+remote\b|"
                          r"\bwork\s+from\s+home\s+(?:position|role|job)\b")
_TEXT_ONSITE = re.compile(r"(?i)\b(?:fully|100%|completely|entirely)\s+on-?\s?site\b|"
                          r"\bon-?\s?site\s+(?:position|role|job)\b|"
                          r"\b(?:position|role|job)\s+is\s+(?:100%\s+|fully\s+)?on-?\s?site\b")


def in_text(text: str) -> str:
    """Where a job is done as its posting's sentences say it ("this hybrid role", "a fully remote position", "this
    position is on-site", "this is not a remote role"); "" when they don't. Never sure: shown, never filtered by."""
    text = str(text or "")
    if _TEXT_HYBRID.search(text):
        return "hybrid"
    if _TEXT_NOT_REMOTE.search(text):
        return "onsite"
    if _TEXT_REMOTE.search(text):
        return "remote"
    return "onsite" if _TEXT_ONSITE.search(text) else ""


def read(field: Any = None, location: str = "", text: str = "") -> tuple[str, str]:
    """(where the job is done, where that came from): a system's own `field` first, then its `location`'s words, then
    its posting's `text`; ("", "") when none says."""
    for said, source in ((from_field(field), "structured"), (in_location(location), "location"),
                         (in_text(text), "text")):
        if said:
            return said, source
    return "", ""


def sure(source: str) -> bool:
    """Whether a reading from `source` may hide a job: a field's or the location's, never the text's."""
    return source in ("structured", "location")


def better(new: dict[str, Any], old: Any) -> bool:
    """Whether `new`'s reading (its workplace and workplace_source) should replace `old`'s: it says one and `old` says
    none, or says it from a better source (structured > location > text)."""
    def rank(said: Any) -> int:
        try:
            value, source = said["workplace"], said["workplace_source"]
        except (KeyError, IndexError, TypeError):
            return 0
        return SOURCES.get(str(source or ""), 0) if value in WORKPLACES else 0
    return rank(new) > rank(old)


_FROM = {"structured": "The job system's own field says so", "location": "Its location says so",
         "text": "Read from the posting's text: the Workplace filter never hides this job for it"}


def view(value: Any, source: Any) -> dict[str, Any] | None:
    """What the desk's row says of where a job is done, or None when it doesn't say: the value, whether it's sure
    (only a sure one is hidden by the Workplace filter), its words ("Remote"; "likely remote" when read from the text)
    and where it came from."""
    if value not in WORKPLACES:
        return None
    is_sure = sure(str(source or ""))
    words = WORDS[value] if is_sure else f"likely {WORDS[value].lower()}"
    return {"value": value, "sure": is_sure, "shown": words, "from": _FROM.get(str(source or ""), "")}


def parse_choice(value: Any) -> list[str] | None:
    """A workplace filter as a tool is given it ("remote", "remote, hybrid", "on-site"): the workplaces named, in their
    order; [] for none; None when a word in it isn't one."""
    if value is None:
        return []
    names = [w.strip().lower().replace("-", "").replace(" ", "") for w in re.split(r"[,|/]", str(value))]
    if not all(n in WORKPLACES for n in names if n):
        return None
    return [w for w in WORKPLACES if w in names]


def keeps(said: tuple[str, str], chosen: list[str]) -> bool:
    """Whether a job whose workplace reading is `said` (value, source) stays under a filter for `chosen`: always, but
    for a sure reading of one not chosen."""
    value, source = said
    return not chosen or value not in WORKPLACES or not sure(source) or value in chosen
