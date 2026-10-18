"""How far a job is from home, for the Job Desk's "Within __ miles" (the design of Oct 18, F8).

Nothing here looks anything up: the plugin's data/places.yaml has the centre of each town its employer lists cover (the
towns of Arizona, and of the Las Vegas and Reno areas), hand-entered, and a job's distance is a straight line
(haversine) from the person's home town (personal.address.city, with personal.address.state) to the nearest of the
towns its location names.

As with every filter on the desk, only a distance known for sure hides a job: one whose location names no town known
here ("Arizona", "2 Locations", "Banner Heart Hosp", a town in another state), or names one beside places it can't
place ("Phoenix, AZ; Statewide", "Tempe, AZ (+3 more)"), always stays, as does a remote job. A hybrid job is judged by
its place, as an on-site one is.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml

from . import config
from .autofill import US_STATES, norm
from .search import _states_named

PLACES_FILE = config.PLUGIN_ROOT / "data" / "places.yaml"
WITHIN_MILES = (10, 25, 50, 100)  # the desk's "Within" choices (none: any distance)
WITHIN_MOST = 3000  # a within_miles a tool is given past this is a typo (the whole country is under it)
EARTH_MILES = 3958.8  # the earth's mean radius


@dataclass(frozen=True)
class Place:
    name: str
    state: str
    lat: float
    lon: float
    needs_state: bool = False  # (read only with its state beside it: places.yaml's needs_state)

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


@lru_cache(maxsize=1)
def known() -> dict[str, tuple[Place, ...]]:
    """The towns of data/places.yaml by their name as norm() writes it (a name two states have: both). None when the
    file can't be read: then no distance is known, and nothing is hidden for one."""
    try:
        data = yaml.safe_load(PLACES_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out: dict[str, list[Place]] = {}
    for state, entry in data.items() if isinstance(data, dict) else ():
        if not isinstance(entry, dict):
            continue
        needs = {norm(n) for n in entry.get("needs_state") or []}
        for name, at in (entry.get("towns") or {}).items():
            key = norm(name)
            out.setdefault(key, []).append(Place(str(name), str(state).upper(), float(at[0]), float(at[1]),
                                                 key in needs))
    return {k: tuple(v) for k, v in out.items()}


@lru_cache(maxsize=1)
def _names() -> re.Pattern[str]:
    """Every town's name, longest first ("north las vegas" before "las vegas", "sun city west" before "sun city"), as
    whole words of norm()'s text."""
    names = sorted(known(), key=len, reverse=True)
    if not names:
        return re.compile(r"(?!)")  # (matches nothing)
    return re.compile(r"(?<![a-z0-9])(" + "|".join(map(re.escape, names)) + r")(?![a-z0-9])")


# a town's name that is a road's or a county's ("W Chandler Blvd, Phoenix", "Maricopa County"), or a school's
_ROAD = re.compile(r" (?:county|rd|road|blvd|boulevard|ave|avenue|st|street|dr|drive|pkwy|parkway|hwy|highway|fwy|"
                   r"freeway|ln|lane|way|loop|cir|circle|ct|court|trl|trail)\b")
_SCHOOL = re.compile(r"\b(?:university|college) of $")
# a location that says the job has places it doesn't name, or none: "+3 more", "2 Locations", "Statewide", "Remote"
_MORE = re.compile(r"(?i)\+\s*\d+\s*more\b|\b\d+\s+locations?\b|\b(?:multiple|various|several|many|all|other)\s+"
                   r"(?:locations?|sites?|places?)\b|\b(?:state|nation|county|valley|metro|country)[\s-]*wide\b|"
                   r"\bremote\b|\banywhere\b|\bwork\s*from\s*home\b|\btelecommute\b")
# what of _MORE says the job has places it doesn't name, besides a count ("2 Locations", which a list says of the places
# its posting names in full): one of a job's locations saying it makes the distance unsure (away)
_OPEN = re.compile(r"(?i)\+\s*\d+\s*more\b|\b(?:multiple|various|several|many|all|other)\s+(?:locations?|sites?|places?)\b|"
                   r"\b(?:state|nation|county|valley|metro|country)[\s-]*wide\b|\bremote\b|\banywhere\b|"
                   r"\bwork\s*from\s*home\b|\btelecommute\b")
# the parts of a location that names several places ("Chandler, AZ; Tempe, AZ", "Phoenix | Mesa", "Mesa or Gilbert")
_PARTS = re.compile(r"(?i)\s*(?:[;|/\n•]|\bor\b|\band\b|&)\s*")
# A state as newspapers abbreviate it ("Calif.", "N.M.", "Ill."), or a code in lower case ("Peoria, il"), after a
# comma: its code, so another state's Las Vegas or Peoria isn't read as Nevada's or Arizona's (only here: the search's
# own state readers are as they were)
_AP_STATES = {"ala": "AL", "ariz": "AZ", "ark": "AR", "calif": "CA", "cal": "CA", "colo": "CO", "conn": "CT",
              "del": "DE", "fla": "FL", "ga": "GA", "ill": "IL", "ind": "IN", "kan": "KS", "kans": "KS", "ky": "KY",
              "la": "LA", "md": "MD", "mass": "MA", "mich": "MI", "minn": "MN", "miss": "MS", "mo": "MO", "mont": "MT",
              "neb": "NE", "nebr": "NE", "nev": "NV", "nh": "NH", "nj": "NJ", "nm": "NM", "ny": "NY", "nc": "NC",
              "nd": "ND", "okla": "OK", "ore": "OR", "oreg": "OR", "pa": "PA", "ri": "RI", "sc": "SC", "sd": "SD",
              "tenn": "TN", "tex": "TX", "vt": "VT", "va": "VA", "wash": "WA", "wva": "WV", "wis": "WI", "wisc": "WI",
              "wyo": "WY"}
_AP = re.compile(r",(\s*)((?:[A-Za-z]\.){2,3}|[A-Za-z]{2,5}\.|[a-z]{2})(?=\s*(?:$|[,;|)\d]))")


def _ap_states(text: str) -> str:
    """`text` with a state written as newspapers abbreviate it, or a code in lower case, after a comma, as its code."""
    def code(m: re.Match[str]) -> str:
        word = m.group(2).replace(".", "").lower()
        said = _AP_STATES.get(word) or (word.upper() if len(word) == 2 and word.upper() in US_STATES else "")
        return f",{m.group(1)}{said}" if said else m.group(0)

    return _AP.sub(code, text)


# what a comma-set piece of a place may hold besides its town: its state, its country, a ZIP code, how it's worked
_ASIDE_WORDS = {"us", "usa", "u", "s", "a", "united", "states", "of", "america", "hybrid", "remote", "onsite", "on",
                "site", "office", "in", "person"}
_STATE_WORDS = {code.lower() for code in US_STATES} | {norm(name) for name in US_STATES.values()}


def _aside(piece: str) -> bool:
    """Whether a comma-set piece of a place ("AZ 85226", "United States of America", "US", a street address: "4500 W
    Chandler Blvd") says no place of its own."""
    said = norm(re.sub(r"\([^)]*\)", " ", piece))
    if re.match(r"\d+ [a-z]", said) and _ROAD.search(f"{said} "[said.find(" "):]):  # (a number on a street)
        return True
    words = [w for w in said.split() if not w.isdigit() and w not in _ASIDE_WORDS]
    return not words or " ".join(words) in _STATE_WORDS


def _in_part(part: str, around: set[str]) -> list[Place]:
    """The towns known here that one part of a location names: in the state it names (or the whole location does);
    with no state named, a town whose name is only one state's and needs none."""
    states = _states_named(part) or around
    text = norm(part)
    out: list[Place] = []
    for m in _names().finditer(text):
        if _ROAD.match(text, m.end()) or _SCHOOL.search(text[:m.start()]):
            continue
        towns = known()[m.group(1)]
        if states:
            out += [t for t in towns if t.state in states]
        elif len(towns) == 1 and not towns[0].needs_state:
            out.append(towns[0])
    return out


@lru_cache(maxsize=4096)
def read(location: str) -> tuple[tuple[Place, ...], bool]:
    """The towns known here that a job's location names, each once in its order, and whether they're all its places:
    every part of it names one, each comma-set piece of a part names one or only its state, country or ZIP code
    ("Tucson, AZ, Fort Huachuca, AZ" names a place it doesn't know), and nothing in it says it has others (or that
    it's remote)."""
    text = _ap_states(str(location or ""))
    if not text.strip():
        return (), False
    around = _states_named(text)
    found: list[Place] = []
    whole = not _MORE.search(text)
    for part in _PARTS.split(text):
        if not norm(part):
            continue
        named = _in_part(part, around)
        whole = whole and bool(named)
        if whole and "," in part:
            states = _states_named(part) or around
            whole = all(_aside(piece) or _in_part(piece, states) for piece in part.split(","))
        found += [t for t in named if t not in found]
    return tuple(found), whole and bool(found)


def miles(a: Place, b: Place) -> float:
    """The straight-line (great-circle) distance between two towns' centres, in miles."""
    la1, lo1, la2, lo2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_MILES * math.asin(math.sqrt(min(1.0, h)))


NO_HOME = ("Within N miles needs your home town, and your profile doesn't say it: add personal.address.city "
           "(and personal.address.state) to your profile.")


def _state_code(said: str) -> str:
    """A state as a profile writes it ("AZ", "az", "Arizona", "Ariz.") as its code; "" for none or one this can't
    read."""
    if said.strip().upper() in US_STATES:
        return said.strip().upper()
    if said.strip().endswith(".") and (code := _AP_STATES.get(said.replace(".", "").strip().lower())):
        return code
    return next((code for code, name in US_STATES.items() if norm(name) == norm(said)), "")


def home(prof: Any) -> tuple[Place | None, str]:
    """The person's home town (personal.address.city, in personal.address.state) as a town known here, or None and
    why, in plain words: the profile doesn't say it, or it isn't a town this knows."""
    city = " ".join(str(prof.get("personal.address.city") or "").split())
    state = " ".join(str(prof.get("personal.address.state") or "").split())
    if not city:
        return None, NO_HOME
    code = _state_code(state)
    towns = list(known().get(norm(city), ())) or [t for m in _names().finditer(norm(city)) for t in known()[m.group(1)]]
    if code:
        towns = [t for t in towns if t.state == code]
    elif not state and towns and (len({t.state for t in towns}) > 1 or towns[0].needs_state):
        # (a name two states have, or another place's or a word too: Paradise, Florence, Miami, Williams)
        return None, (f"Within N miles needs your home state to tell which {city} you mean: add "
                      "personal.address.state (such as AZ) to your profile.")
    elif state:  # (a state it can't read)
        towns = []
    if towns:
        return towns[0], ""
    where = f"{city}, {state}" if state else city
    return None, (f"Within N miles can't tell how far jobs are from {where}: the desk knows where the towns of "
                  "Arizona and the Las Vegas and Reno areas are, and that isn't one of them. Check personal.address.city "
                  "and personal.address.state in your profile.")


def away(home_town: Place, *locations: Any) -> dict[str, Any] | None:
    """How far a job is from `home_town`: the nearest town its locations name (its list's, its posting's once read,
    its job's in the tracker), as {"miles", "place", "sure"}; sure when one of them names only towns known here (so
    no place of it can be nearer) and none says more: one that names a town beside a place it can't place ("Tucson,
    AZ; Statewide", "Tucson, AZ (+2 more)") or says it's remote or in several places ("Multiple Locations") makes it
    unsure, whatever the others say. A bare count ("2 Locations", a list's word for the places its posting names in
    full) doesn't. None when none names a town known here."""
    best: tuple[float, Place] | None = None
    sure = unsure = False
    for location in locations:
        named, whole = read(str(location or ""))
        sure = sure or whole
        unsure = unsure or (not whole and (bool(named) or bool(_OPEN.search(str(location or "")))))
        for town in named:
            d = miles(home_town, town)
            if best is None or d < best[0]:
                best = (d, town)
    if best is None:
        return None
    return {"miles": round(best[0], 1), "place": best[1].label, "sure": sure and not unsure,
            "home": best[1] == home_town}


def keeps(said: dict[str, Any] | None, workplace: str, within: float | None) -> bool:
    """Whether a job stays under "Within `within` miles": always, but for one surely farther (`said`, away's) that
    isn't remote (`workplace`, workplace.py's value: a hybrid job is judged by its place)."""
    return not within or not said or not said.get("sure") or workplace == "remote" or said["miles"] <= within
