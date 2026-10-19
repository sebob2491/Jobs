"""Where a job is done: remote, hybrid or on-site (the design of Oct 18, F3).

Read, best first, from a job system's own field for it (JSON-LD's jobLocationType TELECOMMUTE, Paycom's remoteType,
iCIMS's "Location Type", SuccessFactors' "On-Site" line under the title, and the fields Oracle, Workday, Lever and
SmartRecruiters give when they give them), then the words of the job's location ("Remote - US", "Phoenix, AZ
(Hybrid)"), and only last from the posting's sentences ("this is a fully remote role", "hybrid schedule").

As with pay (pay.py), only a sure reading can hide a job: a field's or the location's that says one way and only that.
One that says it as an option, or beside a place ("Remote eligible", the State of Arizona's "REMOTE OPTIONS", iCIMS's
"Remote Location Eligible: Yes", "Phoenix, AZ; Remote", "Remote or Phoenix", "Remote/Hybrid"), and one read from the
posting's sentences ("remote diagnostics", "no remote work during training") are shown and never hide the job. The
word must stand as the workplace, not as part of a name ("Arizona Virtual Academy", "Remote Sensing Lab", a mine's
"Remote Site"). A job that doesn't say is "unknown" ("" here), and no filter hides it.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict

from .autofill import US_STATES

WORKPLACES = ("remote", "hybrid", "onsite")
WORDS = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}
# where a reading came from, best first: a better one replaces a worse (Tracker, recommend's take). "option": a field
# or a location that says it as an option or beside a place: shown, never sure
SOURCES = {"structured": 4, "location": 3, "option": 2, "text": 1}
FIELDS = ("workplace", "workplace_source")

# "Not Remote", "Remote Work Not Available", "Not Eligible for Remote", "Remote: No", "Work from home: No": not remote,
# which says nothing sure of hybrid or on-site
_NEGATED = re.compile(r"(?i)\b(?:no|not|non)[\s-]*(?:fully\s+)?(?:remote|tele-?work|work\s*from\s*home)\b|"
                      r"\bremote(?:\s+work)?\s+(?:is\s+)?not\s+(?:available|an\s+option|possible|eligible)|"
                      r"\bnot\s+eligible\s+for\s+remote|"
                      r"\b(?:remote(?:\s+location)?(?:\s+eligible)?|remote\s+work|tele-?work(?:\s+eligible)?|"
                      r"telecommute|work\s*from\s*home|wfh)\s*\??\s*:\s*(?:no|not)\b")
_OPTION = re.compile(r"(?i)\b(?:eligible|eligibility|optional|options?|friendly|possible|available|considered|"
                     r"temporar(?:y|ily)|or)\b")
# the words of one part of a location or field, normalized: hyphens and underscores as spaces, "on site" as one word
_JOIN = [(re.compile(r"(?i)\bon[\s_-]*site\b"), "onsite"),
         (re.compile(r"(?i)\bin[\s_-]*(?:the\s+)?office\b"), "inoffice"),
         (re.compile(r"(?i)\bin[\s_-]*person\b"), "inperson"),
         (re.compile(r"(?i)\bwork[\s_-]*from[\s_-]*home\b"), "wfh"),
         (re.compile(r"(?i)\bhome[\s_-]*based\b"), "homebased"), (re.compile(r"(?i)\btele[\s-]*(?:work|commute)\b"),
                                                                      "telework")]
_REMOTE_WORD = r"(?:remote|telework|telecommute|wfh|homebased|virtual)"
_ONSITE_WORD = r"(?:onsite|inoffice|inperson|office)"
_MODS = (r"(?:only|eligible|optional|options?|friendly|first|work|position|role|job|based|required|"
         r"us|usa|anywhere|local|global)")
# a part of a location or a field that is the workplace and nothing else ("Remote", "Remote eligible", "100% Onsite",
# "Hybrid Remote", "Partially Remote")
_PART = {
    "hybrid": re.compile(rf"(?i)^(?:hybrid(?:\s+(?:{_REMOTE_WORD}|{_ONSITE_WORD}|{_MODS}|schedule|model))*|"
                         rf"(?:{_ONSITE_WORD})\s+hybrid|(?:partially|partial|occasional|occasionally|part\s+time)"
                         rf"\s+remote|{_ONSITE_WORD}\s+with\s+(?:occasional|some|partial)\s+remote(?:\s+work)?)$"),
    "remote": re.compile(r"(?i)^(?:(?:fully|100%|completely|entirely|100\s+percent|temporarily|global|"
                         rf"us)\s+)?{_REMOTE_WORD}(?:\s+(?:{_MODS}|with\s+travel))*$"),
    "onsite": re.compile(r"(?i)^(?:(?:fully|100%|completely|entirely)\s+)?(?:onsite|inoffice|"
                         rf"inperson)(?:\s+{_MODS})*$|^(?:office|office\s+based|onsite\s+required)$"),
}
_ANY_WORD = re.compile(rf"(?i)\b(?:{_REMOTE_WORD}|{_ONSITE_WORD}|hybrid)\b")
_REGIONS = ({code.lower() for code in US_STATES} | {name.lower() for name in US_STATES.values()}
            | {"us", "u s", "usa", "united states", "united states of america", "nationwide", "anywhere",
               "north america", "americas", "global", "worldwide", "only", ""})


def _parts(text: str) -> list[str]:
    words = str(text or "")
    for pattern, joined in _JOIN:
        words = pattern.sub(joined, words)
    return [" ".join(p.split()).lower() for p in re.split(r"(?i)[;,|/()\[\]:–-]|\bor\b|\band\b", words)]


def _said(text: str, places: bool) -> tuple[str, bool]:
    """(the workplace `text` says, whether surely) for a field (`places` False) or a location (True: a remote one
    beside a place is an option, not sure)."""
    text = str(text or "")
    if not text.strip() or _NEGATED.search(text):
        return "", False
    kinds: list[str] = []
    named = place = False
    no_parens = re.sub(r"\([^)]*\)", " ", text)
    for part in _parts(text):
        kind = next((k for k, pattern in _PART.items() if pattern.match(part)), "")
        if kind:
            kinds.append(kind)
        elif _ANY_WORD.search(part):
            named = True  # (the word inside a name: "Remote Sensing Lab", "Arizona Virtual Academy", "Remote Site")
    for part in _parts(no_parens):
        if part and not any(p.match(part) for p in _PART.values()) and not _ANY_WORD.search(part) \
                and part not in _REGIONS:
            place = True  # (a place besides regions: "Phoenix", "Morenci"; not "US", "Arizona")
    found = set(kinds)
    if not found:
        return "", False
    value = "hybrid" if "hybrid" in found else "remote" if "remote" in found else "onsite"
    several = len(found) > 1  # (two ways named: "Hybrid / On-Site", "Remote/Hybrid": never sure)
    option = bool(_OPTION.search(text)) and value == "remote"
    sure = not (several or option or named or (places and value == "remote" and place))
    return value, sure


def field_said(value: Any) -> tuple[str, bool]:
    """A job system's own field for where the job is done ("TELECOMMUTE", "Remote", "Hybrid", "On-Site", "Onsite",
    "In Office", Oracle's "ORA_HYBRID", "Partially Remote"), and whether surely: not when it says it as an option
    ("Remote Eligible", "Remote Optional", "Temporarily Remote"). ("", False) for one that says none or several
    ("Flexible", "Remote or On-site", "unspecified", "Not Remote", "Remote: No")."""
    words = re.sub(r"(?i)^ora_|_", " ", re.sub(r"([a-z])([A-Z])", r"\1 \2", str(value or ""))).strip()
    if re.fullmatch(r"(?i)telecommute|remote\s*:\s*yes", words):
        return "remote", True
    return _said(words, places=False)


def from_field(value: Any) -> str:
    """field_said's workplace alone."""
    return field_said(value)[0]


def location_said(location: str) -> tuple[str, bool]:
    """Where a job is done as its location says it, and whether surely: "Remote - US", "US-Remote", "Remote (must live
    in AZ)" are surely remote; "Phoenix, AZ (Hybrid)" hybrid and "Chandler, AZ - Onsite" on-site. Not sure: a remote
    one beside a place ("Phoenix, AZ; Remote", "Morenci (Remote)"), or as an option ("Remote eligible", "Remote
    Options", "Remote or Phoenix"), or with another ("Remote/Hybrid"), or a word inside a name. ("", False) for a place
    alone, a name ("Remote Sensing Lab"), or a "not remote"."""
    return _said(location, places=True)


def in_location(location: str) -> str:
    """location_said's workplace alone."""
    return location_said(location)[0]


class Fields(TypedDict, total=False):
    workplace: str
    workplace_source: str


def fields(value: str, sure: bool, source: str = "structured") -> Fields:
    """A reading as a Listing's or a Posting's workplace fields: `source` when sure, else "option"; {} for none."""
    if value not in WORKPLACES:
        return {}
    return {"workplace": value, "workplace_source": source if sure else "option"}


# The posting's own sentences, for a job that says it nowhere else: unsure (never hides a job)
_TEXT_HYBRID = re.compile(r"(?i)(?<!not a )(?<!not )\bhybrid[\s-]+(?:position|role|job|opportunity|schedule|"
                          r"work(?:ing)?|model|arrangement|environment|setting)\b|\bthis\s+is\s+a\s+hybrid\b")
_TEXT_NOT_REMOTE = re.compile(r"(?i)\b(?:is\s+not|isn't|not)\s+(?:a\s+)?(?:fully\s+)?remote\b|\bno\s+remote\s+work\b|"
                              r"\bnon-remote\b|\bremote\s+work\s+is\s+not\s+(?:available|an\s+option|possible)\b")
_TEXT_REMOTE = re.compile(r"(?i)\b(?:fully|100%|completely|entirely|100\s+percent)\s+remote\b|"
                          r"\bremote[\s-]+(?:position|role|job|opportunity)\b|\bthis\s+is\s+a\s+remote\b|"
                          r"\bwork\s+from\s+home\s+(?:position|role|job)\b")
_TEXT_ONSITE = re.compile(r"(?i)\b(?:fully|100%|completely|entirely)\s+on-?\s?site\b|"
                          r"\bon-?\s?site\s+(?:position|role|job)\b|"
                          r"\b(?:position|role|job)\s+is\s+(?:100%\s+|fully\s+)?on-?\s?site\b|"
                          r"\bmust\s+be\s+on-?\s?site\b")


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
    its posting's `text`; ("", "") when none says. A field's or a location's said as an option is "option"."""
    said = field_said(field)
    if said[0]:
        return said[0], "structured" if said[1] else "option"
    said = location_said(location)
    if said[0]:
        return said[0], "location" if said[1] else "option"
    said_text = in_text(text)
    return (said_text, "text") if said_text else ("", "")


def sure(source: str) -> bool:
    """Whether a reading from `source` may hide a job: a field's or the location's said surely, never an option's or
    the text's."""
    return source in ("structured", "location")


def rank(said: Any) -> int:
    """How good a reading (its workplace and workplace_source) is: SOURCES', 0 for none."""
    try:
        value, source = said["workplace"], said["workplace_source"]
    except (KeyError, IndexError, TypeError):
        return 0
    return SOURCES.get(str(source or ""), 0) if value in WORKPLACES else 0


def better(new: dict[str, Any], old: Any) -> bool:
    """Whether `new`'s reading (its workplace and workplace_source) should replace `old`'s: it says one and `old` says
    none, or says it from a better source (structured > location > option > text)."""
    return rank(new) > rank(old)


_FROM = {"structured": "The job system's own field says so", "location": "Its location says so",
         "option": "Said as an option, or beside a place: the Workplace filter never hides this job for it",
         "text": "Read from the posting's text: the Workplace filter never hides this job for it"}


def view(value: Any, source: Any) -> dict[str, Any] | None:
    """What the desk's row says of where a job is done, or None when it doesn't say: the value, whether it's sure
    (only a sure one is hidden by the Workplace filter), its words ("Remote"; "remote option" for an option, "likely
    remote" when read from the text) and where it came from."""
    if value not in WORKPLACES:
        return None
    source = str(source or "")
    is_sure = sure(source)
    word = WORDS[value]
    words = word if is_sure else f"{word.lower()} option" if source == "option" else f"likely {word.lower()}"
    return {"value": value, "sure": is_sure, "shown": words, "from": _FROM.get(source, "")}


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
