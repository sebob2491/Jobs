"""Search a company's openings through its applicant tracking system's public JSON API.

Each company in data/companies.yaml may carry a `search` block naming one of:

    workday:         https://<tenant>.wd<N>.myworkdayjobs.com/<site>
    greenhouse:      <board token>
    lever:           <company slug>
    eightfold:       {host: careers.example.com, domain: example.com}
    smartrecruiters: <company identifier>
    oracle:          {host: xxxx.fa.us2.oraclecloud.com, site: CX_1001}
    applicantstack:  <board name>
    taleo:           {host: myhiring.kforce.com, section: ex, portal: 101430233}  (Taleo career sections)
    talemetry:       <job site address>     (Symplr Talemetry job sites: Valleywise Health)
    phoenixchildrens: <job site address>    (Phoenix Children's own job site)
    jibe:            <Jibe site host>       (iCIMS's Jibe job sites: jobs.sprouts.com)
    jobvite:         <company>              (jobs.jobvite.com/<company>)
    amazon:          {loc_query: "Phoenix, AZ, USA", latitude: .., longitude: .., radius: 50km}  (amazon.jobs)
    randstad:        https://www.randstadusa.com/jobs/internal  (Randstad's own jobs)
    mcloud:          {company: companies/<id>}  (a Google Cloud Talent search at jobsapi-google.m-cloud.io: Edward Jones)
    icims:           <portal name>          (read in the browser)
    paycom:          <career portal key>    (read in the browser)
    ukg:             <job board address>    (UKG Pro / UltiPro; read in the browser)
    rmk:             {url: <search page>}   (SuccessFactors' newer job search; read in the browser)
    successfactors:  <career site address>  (SuccessFactors career sites' search pages)
    sfclassic:       {site: https://career8.successfactors.com, company: <id>}
                                            (SuccessFactors' older career sites; read in the browser)
    infor:           {url: <job board page>} (Infor CloudSuite HCM; read in the browser)
    sitecore:        {url: ..., api: ...}   (ASML; read in the browser)

Companies without one (SuccessFactors sites, custom pages) are searched in the
browser instead. These endpoints are what the careers pages themselves call;
requests are few and sequential per company.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import yaml
from bs4 import BeautifulSoup

from . import config
from .ats import workday_parts
from .autofill import US_STATES, norm
from .postings import USER_AGENT, html_to_text, place_in_text, successfactors_place

WORKDAY_PAGE = 20  # Workday rejects larger pages
MAX_ALTERNATIVES = 4
FETCH_WHEN_FILTERING = 60  # results to scan per search when filtering by location ourselves
EMPLOYERS_AT_ONCE = 8  # employers searched at the same time (each its own site; ~50 s for the Phoenix list, not ~100)
CLIENT_SIDE = {"greenhouse", "lever", "applicantstack", "paycom", "ukg", "sfclassic", "infor", "phoenixchildrens",
               "jobvite", "randstad"}  # whole board at once; titles filtered here
# Searches whose data only comes through the site's own page in the browser (ASML's
# Sitecore Discover widget; iCIMS portals, which turn away plain requests; Paycom, UKG
# Pro and SuccessFactors' newer search, whose APIs want the session their page sets up;
# SuccessFactors' older career sites, whose list the page's script draws; Infor CloudSuite
# boards, whose list call doesn't reliably answer plain requests).
# search_companies lists them under needs_browser and the search_company_jobs tool runs them.
BROWSER_SEARCHES = {"sitecore", "icims", "paycom", "ukg", "rmk", "sfclassic", "infor"}
RETRY_STATUS = {429, 500, 502, 503, 504}  # a passing problem on the site's side
RETRY_DELAY = 1.0  # seconds, doubled on the second retry


@dataclass
class Listing:
    company: str
    title: str
    url: str
    location: str = ""
    posted: str = ""
    external_id: str = ""
    ats: str = ""
    company_url: str = ""  # the employer's own page for the posting, when it differs from url
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if not out["company_url"]:
            del out["company_url"]
        return out


class SearchError(Exception):
    pass


BUILTIN_COMPANIES = "companies.yaml"  # in the plugin's data folder: semiconductor employers in Arizona
BUILTIN_LIST = "semiconductor-az"  # that list's name, for a person's own file to name it
# Other employer lists the plugin carries, by name: data/lists/<name>.yaml (phoenix-metro: large
# Phoenix-area employers in health care, education, finance, utilities and more)
LISTS_DIR = "lists"


def own_companies_path() -> Path:
    """A person's own employer list, in their own folder (~/.job-apply/companies.yaml)."""
    return config.home() / "companies.yaml"


def companies_path() -> Path:
    """The employer list Find jobs searches: the person's own when they have one."""
    own = own_companies_path()
    return own if own.exists() else employer_lists()[BUILTIN_LIST]


def employer_lists() -> dict[str, Path]:
    """The plugin's employer lists, by name."""
    lists = {BUILTIN_LIST: config.PLUGIN_ROOT / "data" / BUILTIN_COMPANIES}
    folder = config.PLUGIN_ROOT / "data" / LISTS_DIR
    if folder.is_dir():
        lists.update({p.stem: p for p in sorted(folder.glob("*.yaml"))})
    return lists


def _companies_in(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"{path} can't be read: {str(e).splitlines()[0] if str(e) else type(e).__name__}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} should be written as `lists:` (the plugin's lists to search) and/or `companies:` "
                         "(employers of your own); see the plugin's templates/companies.example.yaml")
    return [c for c in data.get("companies") or [] if isinstance(c, dict) and c.get("name")], data


def _list_names(data: dict[str, Any], path: Path) -> list[str]:
    """The plugin lists a person's file names: one name, or a list of them."""
    names = data.get("lists")
    if names is None:
        names = []
    elif isinstance(names, str):
        names = [names]
    elif not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError(f"{path}: `lists:` should be a list of the plugin's list names, such as "
                         "[phoenix-metro, semiconductor-az]")
    if data.get("include_builtin") is True and BUILTIN_LIST not in names:
        names = [*names, BUILTIN_LIST]
    return names


def _search_key(company: dict[str, Any]) -> str:
    """What an entry searches: two entries searching the same site are one employer, whatever
    each calls it ("Mayo Clinic", "Mayo Clinic (Arizona)")."""
    return json.dumps(company["search"], sort_keys=True, default=str) if company.get("search") else ""


def load_companies() -> list[dict[str, Any]]:
    """The employers to search: the plugin's semiconductor list, or a person's own file
    (~/.job-apply/companies.yaml) in its place. Their file names the plugin's lists it wants
    (`lists: [phoenix-metro, semiconductor-az]`) and adds employers of its own (`companies:`,
    the same shape, kept as written); `include_builtin: true` is the semiconductor list too.
    A list's employer that one of theirs already covers (the same name, or the same site
    searched) is left out, as is one an earlier list has. Someone looking for HR work in
    Phoenix keeps their own file; nothing of theirs goes into the plugin."""
    lists = employer_lists()
    own_path = own_companies_path()
    if not own_path.exists():
        return _companies_in(lists[BUILTIN_LIST])[0]
    own, data = _companies_in(own_path)
    names = _list_names(data, own_path)
    for name in names:
        if name not in lists:
            raise ValueError(f"{own_path}: the plugin has no employer list named {name!r}; "
                             f"its lists are {', '.join(lists)}")
    out = list(own)
    seen_names = {norm(c["name"]) for c in own}
    seen_sites = {_search_key(c) for c in own} - {""}
    for name in names:
        for company in _companies_in(lists[name])[0]:
            key = _search_key(company)
            if norm(company["name"]) in seen_names or key and key in seen_sites:
                continue
            seen_names.add(norm(company["name"]))
            seen_sites.add(key)
            out.append(company)
    return out


def alternatives(query: str) -> list[str]:
    """'field service | equipment engineer' -> ['field service', 'equipment engineer']"""
    alts = [q.strip() for q in re.split(r"\s*\|\s*|\s+OR\s+", query or "") if q.strip()]
    return alts[:MAX_ALTERNATIVES] or [""]


def title_words(title: str) -> str:
    """A title's words as matched, each way of saying it among them: "HR Business Partner" is
    a human resources job, and "Human Resources Generalist" an HR generalist one."""
    text = re.sub(r"\b(\d+)\s*hrs?\b", r"\1 hours", norm(title))  # a nurse's "12 Hr Nights" isn't HR
    if re.search(r"\bhuman resources?\b", text):
        text += " hr"
    if re.search(r"\bhr\b", text):
        text += " human resources"
    return text


def title_matches(title: str, query: str) -> bool:
    """Every word of at least one alternative appears in the title: as the start of a word
    ("engineer" ~ "engineering"), or the whole word for one of two letters ("HR" isn't "36 Hrs")."""
    text = f" {title_words(title)} "
    for alt in alternatives(query):
        if all((f" {t} " if len(t) <= 2 else f" {t}") in text for t in norm(alt).split()):
            return True
    return False


# Employers often list just the city ("Chandler (Office)"), so a state also matches its
# main metro areas. Only states the plugin's company list covers need entries.
STATE_CITIES = {
    "AZ": ["phoenix", "chandler", "tempe", "mesa", "scottsdale", "gilbert", "glendale", "peoria", "tucson",
           "goodyear", "surprise", "avondale", "casa grande", "queen creek", "maricopa"],
}


_AREA_WORDS = {"greater", "metro", "metropolitan", "area", "region", "valley"}  # "Greater Phoenix", "Phoenix area"
IN_STATE = "in:"  # a term "in:az": the area is in that state (never matched as words: norm drops ":")


# State names, longest first: "west virginia" before "virginia"
_STATE_WORDS = sorted(((code, norm(name).split()) for code, name in US_STATES.items()), key=lambda c: -len(c[1]))


def _trailing_state(words: list[str], leading_ok: bool = False) -> tuple[str | None, list[str]]:
    """A state after the place: 'phoenix az' -> ('AZ', ['phoenix']); 'tempe arizona' -> ('AZ',
    ['tempe']); with leading_ok, before it too: 'arizona phoenix area' -> ('AZ', ['phoenix',
    'area']). Else (None, words): "Kansas City" and "Arizona City" are places of their own,
    and a state named whole ("West Virginia") is read as itself, not as Virginia after "West"."""
    if any(words == n for _, n in _STATE_WORDS):
        return None, words
    if len(words) > 1 and words[-1].upper() in US_STATES:
        return words[-1].upper(), words[:-1]
    for code, n in _STATE_WORDS:
        if len(words) > len(n) and words[-len(n):] == n:
            return code, words[:-len(n)]
    for code, n in _STATE_WORDS if leading_ok else ():
        if len(words) > len(n) and words[:len(n)] == n:
            return code, words[len(n):]
    return None, words


def location_terms(location: str | None) -> list[str]:
    """'AZ' -> ['az', 'arizona', 'phoenix', 'chandler', ...]; 'Phoenix|Chandler' -> ['phoenix',
    'chandler', 'in:az'] (cities of one state: another state's Phoenix isn't the area).
    'Phoenix AZ', 'Phoenix, AZ 85001' and 'Greater Phoenix' are read as a person means them."""
    if not location:
        return []
    terms: list[str] = []
    states: set[str] = set()
    for part in re.split(r"\s*[|,;]\s*", location):
        words = [w for w in norm(part).split() if not w.isdigit()]  # "AZ 85001": a ZIP code is no place
        # a state first only when set apart: "Arizona (Phoenix area)", "Arizona - Phoenix"
        state, words = _trailing_state(words, leading_ok=bool(re.match(r"^\s*[A-Za-z ]+\s*[(\-\u2013:]", part)))
        words = [w for w in words if w not in _AREA_WORDS] or words
        n = " ".join(words)
        if n:
            terms.append(n)
            if n.upper() in US_STATES:
                state = state or n.upper()
            for code, name in US_STATES.items():
                if n == norm(name):
                    state = state or code
        if state:
            states.add(state)
            terms += [state.lower(), norm(US_STATES[state]), *STATE_CITIES.get(state, [])]
    if not states:
        # only cities: the state they're all in, when the plugin knows it
        cities = {t for t in terms}
        home = {code for code, names in STATE_CITIES.items() if cities and cities <= set(names)}
        terms += [IN_STATE + code.lower() for code in home]
    return list(dict.fromkeys(terms))


# Words that say where a job is only as "the US", "anywhere" or "several places": a listing
# with nothing else ("US - Multiple Locations", "Remote - US (Field Based)") could be done
# from the area, and is flagged rather than dropped
_BROAD_WORDS = {"us", "usa", "u", "s", "a", "united", "states", "of", "america", "north", "nationwide", "anywhere",
                "in", "the", "multiple", "various", "several", "locations", "location", "field", "based", "home",
                "remote", "travel", "traveling", "travelling", "virtual", "telecommute", "work", "from", "and", "or",
                "any", "all", "hybrid", "mobile", "more"}


_WORDLIKE_CODES = {"IN", "OR", "ME", "OK", "HI", "OH", "DE", "PA", "MA", "AL", "CO", "LA", "ID", "MI", "MO", "MS",
                   "GA", "NE", "AR", "MT"}


def _states_named(text: str) -> set[str]:
    """US states a place names: a capitalised code ("Peoria, IL", "US-AZ-Chandler") or the
    state's name ("Peoria, Illinois")."""
    text = text or ""
    found = set()
    for m in re.finditer(r"(?<![^\s,;/|(\-])([A-Z]{2})(?![^\s,;/|).\-])", text):
        code = m.group(1)
        if code not in US_STATES:
            continue
        if code in _WORDLIKE_CODES:
            # in capitals, "IN" and "OR" are words too ("CHANDLER - WORK IN OFFICE"): a state only
            # after a comma or dash ("Indianapolis, IN", "US-IN-"), or ending the place ("Portland OR 97201")
            before, after = text[:m.start()].rstrip(), text[m.end():].lstrip()
            if not (before.endswith((",", "-", "(", "/", "|", ";")) or not after
                    or after[:1] in ",;)|/" or after[:1].isdigit()):
                continue
        found.add(code)
    padded = f" {norm(text)} "
    found |= {code for code, name in US_STATES.items() if f" {norm(name)} " in padded}
    return found


def location_matches(text: str, terms: list[str]) -> bool | None:
    """True/False, or None when the listing is too broad to tell ("3 Locations", "Remote - US")."""
    if not terms:
        return True
    wanted = {t.upper() for t in terms if t.upper() in US_STATES}
    wanted |= {t[len(IN_STATE):].upper() for t in terms if t.startswith(IN_STATE)}
    # Each place of a listing that names several on its own: "Austin, TX; Chandler" is in the
    # area by its Chandler, though Texas is named and Arizona isn't
    for place in re.split(r"\s*(?:[;|/]|\bor\b|\band\b)\s*", text or ""):
        named = _states_named(place)
        if wanted and named and not named & wanted:
            continue  # another state's Peoria or Glendale: the city name alone isn't the area
        padded = f" {norm(place)} "
        if any(f" {t} " in padded for t in terms):
            return True
    n = norm(text)
    if not n or re.search(r"\+\d+ more\b", n):  # "Greensboro, NC (+3 more)": the others could be here
        return None
    named = _states_named(text)
    if wanted and named and not named & wanted:
        return False  # "OR", "Austin, TX": another state, and none of the area's places
    # "Remote - US" could be done from anywhere; "Remote, Japan" can't
    return None if all(w in _BROAD_WORDS or w.isdigit() for w in n.split()) else False


# --------------------------------------------------------------------- per-ATS
# Each searcher takes (client, config value, query, how many to fetch, location terms).


def _workday_location_facets(facets: Any, terms: list[str]) -> dict[str, list[str]] | None:
    """Pick the location facet values (Workday's own location filter) that match the terms.
    Returns {facetParameter: [ids]} for the one parameter covering the most postings, {} when
    the site has location values but none in the area, and None when it has none at all."""
    found: dict[str, list[tuple[str, int]]] = {}
    any_location = False

    def walk(items: Any, param: str | None) -> None:
        nonlocal any_location
        for item in items or []:
            if not isinstance(item, dict):
                continue
            p = item.get("facetParameter") or param
            if p and "id" in item and "descriptor" in item and re.search(r"location|country|state|city|region", p, re.I):
                # countries alone say nothing of which city a "2 Locations" job is in
                any_location = any_location or not re.search(r"country", p, re.I)
                if location_matches(str(item["descriptor"]), terms) is True:
                    found.setdefault(p, []).append((str(item["id"]), int(item.get("count") or 0)))
            if item.get("values"):
                walk(item["values"], p)

    walk(facets, None)
    if not found:
        return {} if any_location else None
    best = max(found, key=lambda p: sum(c for _, c in found[p]))
    return {best: [i for i, _ in found[best]]}


async def _workday(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    parts = workday_parts(str(cfg).rstrip("/") + "/")
    if not parts:
        raise SearchError(f"Not a Workday site URL: {cfg}")
    host, tenant, site = parts["host"], parts["tenant"], parts["site"]
    cxs = f"https://{host}/wday/cxs/{tenant}/{site}"
    api = f"{cxs}/jobs"
    # myworkdaysite.com addresses carry the tenant: /recruiting/<tenant>/<site>/job/...
    base = f"https://{host}/recruiting/{tenant}/{site}" if "myworkdaysite.com" in host else f"https://{host}/{site}"

    async def page(offset: int, facets: dict[str, list[str]]) -> dict[str, Any]:
        body = {"appliedFacets": facets, "limit": WORKDAY_PAGE, "offset": offset, "searchText": query}
        r = await _send(client, "POST", api, json=body, headers={"Accept": "application/json"})
        _raise_for(r, api)
        return r.json()

    first = await page(0, {})
    applied: dict[str, list[str]] = {}
    nowhere_near = False
    if terms and first.get("facets"):
        # Use the site's own location filter when one of its values names the area;
        # otherwise scan unfiltered results and filter them afterwards.
        match = _workday_location_facets(first["facets"], terms)
        if match:
            applied = match
            first = await page(0, applied)
        # The filter lists every place these results are in. If none is in the area,
        # neither is any "3 Locations" job.
        nowhere_near = match == {}
    total = int(first.get("total") or 0)  # only the first page carries the total
    out: list[Listing] = []
    data, offset = first, 0
    while True:
        postings = data.get("jobPostings") or []
        for p in postings:
            if not p.get("externalPath") or not p.get("title"):
                continue  # not a posting (Analog Devices' answer had one with neither)
            where = str(p.get("locationsText") or "")
            if nowhere_near and _SITE_COUNT.fullmatch(where.strip()):
                continue  # "3 Locations", none of them in the area (a "Remote - US" job could be done from it)
            out.append(Listing(
                company="", title=p.get("title", ""), url=f"{base}{p.get('externalPath') or ''}",
                location=where, posted=str(p.get("postedOn") or ""),
                external_id=(p.get("bulletFields") or [""])[0], ats="workday",
            ))
        offset += WORKDAY_PAGE
        if not postings or len(out) >= limit or offset >= total:
            break
        data = await page(offset, applied)
    out = out[:limit]
    await _workday_places(client, cxs, base, out, terms, in_area=bool(applied))
    return out


WORKDAY_PLACE_PAGES = 20  # multi-site postings read for their places, per search
_SITE_COUNT = re.compile(r"\d+ locations?", re.I)  # how Workday lists a multi-site posting


async def _workday_places(client: httpx.AsyncClient, cxs: str, base: str, listings: list[Listing],
                          terms: list[str], in_area: bool) -> None:
    """Workday lists a posting with several places as "7 Locations", and the odd one with
    none. Read those postings' places (the ones in the area first), so the location filter
    and the ranking see where they are. A posting the site's own location filter found is
    in the area: its places only replace "7 Locations" when they show that too."""
    sem = asyncio.Semaphore(4)

    async def one(listing: Listing) -> None:
        url = cxs + listing.url[len(base):]  # the posting's own call: /wday/cxs/<tenant>/<site>/job/...
        async with sem:
            try:
                r = await _send(client, "GET", url, headers={"Accept": "application/json"})
                _raise_for(r, url)
                info = r.json().get("jobPostingInfo") or {}
            except Exception:  # an unreadable posting stays "check the posting"
                return
        places = [str(x).strip() for x in [info.get("location"), *(info.get("additionalLocations") or [])] if x]
        places = list(dict.fromkeys(x for x in places if x))
        places.sort(key=lambda x: location_matches(x, terms) is not True)
        where = "; ".join(places)
        if where and not (in_area and location_matches(where, terms) is not True):
            listing.location = where

    todo = [x for x in listings
            if x.url.startswith(base + "/") and (not x.location.strip() or _SITE_COUNT.fullmatch(x.location.strip()))]
    await asyncio.gather(*(one(x) for x in todo[:WORKDAY_PLACE_PAGES]))


async def _greenhouse(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    api = f"https://boards-api.greenhouse.io/v1/boards/{cfg}/jobs"
    r = await _send(client, "GET", api)
    _raise_for(r, api)
    out = []
    for j in r.json().get("jobs", []):
        if not title_matches(j.get("title", ""), query):
            continue
        hosted = f"https://job-boards.greenhouse.io/{cfg}/jobs/{j.get('id')}"
        own = j.get("absolute_url") or ""
        out.append(Listing(
            company="", title=j.get("title", ""), url=hosted, company_url=own if own != hosted else "",
            location=(j.get("location") or {}).get("name", ""), posted=j.get("updated_at", ""),
            external_id=str(j.get("requisition_id") or j.get("id") or ""), ats="greenhouse",
        ))
    return out


async def _lever(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    api = f"https://api.lever.co/v0/postings/{cfg}?mode=json"
    r = await _send(client, "GET", api)
    _raise_for(r, api)
    data = r.json()
    if not isinstance(data, list):
        raise SearchError(f"Unexpected Lever response from {api}")
    out = []
    for j in data:
        if not title_matches(j.get("text", ""), query):
            continue
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        out.append(Listing(
            company="", title=j.get("text", ""), url=j.get("hostedUrl", ""), location=cats.get("location") or "",
            posted=datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat() if created else "",
            external_id=j.get("id", ""), ats="lever",
        ))
    return out


def _eightfold_positions(data: Any) -> list[dict[str, Any]]:
    """Positions from either Eightfold API shape (v2 `positions`, or pcsx `data.positions`)."""
    if isinstance(data, dict):
        for key in ("positions", "results", "jobs"):
            if isinstance(data.get(key), list):
                return [p for p in data[key] if isinstance(p, dict)]
        if isinstance(data.get("data"), (dict, list)):
            return _eightfold_positions(data["data"])
    return []


def _place(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("location") or value.get("displayName") or "")
    return str(value or "")


def parse_eightfold(data: Any, host: str) -> list[Listing]:
    out = []
    for p in _eightfold_positions(data):
        locs = [_place(x) for x in (p.get("locations") or p.get("standardizedLocations") or [p.get("location")])]
        link = p.get("canonicalPositionUrl") or p.get("positionUrl") or p.get("url") or ""
        if link.startswith("/"):
            link = f"https://{host}{link}"
        out.append(Listing(
            company="", title=p.get("name") or p.get("title") or "",
            url=link or f"https://{host}/careers/job/{p.get('id')}",
            location="; ".join(loc for loc in locs if loc),
            external_id=str(p.get("display_job_id") or p.get("displayJobId") or p.get("id") or ""),
            posted=_epoch_date(p.get("t_create") or p.get("postedTs") or p.get("creationTs")), ats="eightfold",
        ))
    return out


# ------------------------------------------------- Sitecore Discover (ASML), through the browser
SITECORE_LIMIT = 100  # the page asks for 25 at a time; Sitecore answers up to 100 per call
SITECORE_PAGES = 3  # 300 openings per wording; ASML's broadest search here finds about 150


def sitecore_page_url(cfg: dict[str, Any], query: str) -> str:
    return str(cfg["url"]).format(query=quote(query))


def _sitecore_searches(body: Any) -> list[dict[str, Any]]:
    items = ((body or {}).get("widget") or {}).get("items") or [] if isinstance(body, dict) else []
    return [i["search"] for i in items if isinstance(i, dict) and isinstance(i.get("search"), dict)]


def sitecore_wants(body: Any) -> bool:
    """The page's own keyword search, not its facet-only first call."""
    return any((s.get("query") or {}).get("keyphrase") for s in _sitecore_searches(body))


def sitecore_rewrite(body: Any, offset: int = 0) -> Any:
    """Ask for SITECORE_LIMIT results from `offset` instead of the page's first 25."""
    searches = [s for s in _sitecore_searches(body) if (s.get("query") or {}).get("keyphrase")]
    for s in searches:
        s["limit"], s["offset"] = SITECORE_LIMIT, offset
    return body if searches else None


def sitecore_total(data: Any) -> int | None:
    """How many openings the search found in all, when the answer says."""
    widgets = data.get("widgets") or [] if isinstance(data, dict) else []
    totals = [w.get("total_item") for w in widgets if isinstance(w, dict)]
    return next((t for t in totals if isinstance(t, int)), None)


async def sitecore_search(capture: Callable[..., Awaitable[Any]], cfg: dict[str, Any], query: str,
                          found: list[Listing]) -> None:
    """One wording through the site's own search page, SITECORE_LIMIT openings at a time.
    `capture` is BrowserSession.capture_json. Openings go into `found` as each page
    arrives, so a failure keeps the pages before it."""
    for page in range(SITECORE_PAGES):
        offset = page * SITECORE_LIMIT
        data = await capture(sitecore_page_url(cfg, query), cfg.get("api", "/discover/v2/"), want=sitecore_wants,
                             rewrite=lambda body, offset=offset: sitecore_rewrite(body, offset))
        batch = parse_sitecore(data)
        found.extend(batch)
        total = sitecore_total(data)
        if offset + SITECORE_LIMIT >= total if total is not None else len(batch) < SITECORE_LIMIT:
            return


def parse_sitecore(data: Any) -> list[Listing]:
    out = []
    for widget in (data or {}).get("widgets") or []:
        for c in widget.get("content") or []:
            if not isinstance(c, dict) or not c.get("url") or c.get("type", "job_detail_page") != "job_detail_page":
                continue
            place = c.get("job_location") or ", ".join(
                str(x) for x in (c.get("job_city"), c.get("job_state"), c.get("job_country")) if x)
            out.append(Listing(
                company="", title=c.get("name") or "", url=c["url"], location=place,
                posted=str(c.get("job_date_posted") or "")[:10], external_id=str(c.get("job_id") or c.get("id") or ""),
                ats="company_site",
            ))
    return out


def eightfold_page_url(cfg: dict[str, Any], query: str, location: str | None) -> str:
    """The careers page whose own search call the browser fallback listens for."""
    params = {"query": " ".join(alternatives(query)), "domain": cfg["domain"]}
    if location:
        first = location.split("|")[0].strip()
        params["location"] = US_STATES.get(first.upper(), first)
    return f"https://{cfg['host']}/careers?{urlencode(params)}"


async def _board_page(client: httpx.AsyncClient, url: str) -> str:
    """One page of a board that lists its openings whole (titles are matched here)."""
    r = await _send(client, "GET", url)
    _raise_for(r, url)
    return r.text


def _titled(listings: list[Listing], query: str) -> list[Listing]:
    return [listing for listing in listings if title_matches(listing.title, query)]


async def _applicantstack(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """ApplicantStack boards (SCREEN SPE USA) list every opening on one page, each with
    its location."""
    base = f"https://{cfg}.applicantstack.com"
    return _titled(parse_applicantstack(await _board_page(client, f"{base}/x/openings"), base), query)


def parse_applicantstack(html: str, base: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    seen: set[str] = set()
    for a in soup.select('a[href*="/x/detail/"]'):
        url = urljoin(base + "/", str(a["href"]))
        title = a.get_text(" ", strip=True)
        if not title or url in seen:
            continue
        seen.add(url)
        row = a.find_parent("tr")  # a table: Job Title, Location
        cells = [td.get_text(" ", strip=True) for td in row.find_all("td") if td.find("a") is None] if row else []
        out.append(Listing(company="", title=title, url=url, location=next((c for c in cells if c), ""),
                           external_id=url.rstrip("/").rsplit("/", 1)[-1], ats="applicantstack"))
    return out


ICIMS_PAGES = 4  # result pages read per search (20 openings each): a national portal's Arizona ones are often past the first
# iCIMS portals' location filter names a state by iCIMS's own number, the same on every portal
# (Aerotek's, TEKsystems', Allegis Group's, Schwab's, GDMS's and Daifuku's, live, Oct 2026)
ICIMS_STATES = {
    "AL": 12782, "AK": 12783, "AZ": 12784, "AR": 12785, "CA": 12789, "CO": 12790, "CT": 12791, "DE": 12792,
    "FL": 12793, "GA": 12794, "HI": 12795, "ID": 12796, "IL": 12797, "IN": 12798, "IA": 12799, "KS": 12800,
    "KY": 12801, "LA": 12802, "ME": 12803, "MD": 12804, "MA": 12805, "MI": 12806, "MN": 12807, "MS": 12808,
    "MO": 12809, "MT": 12810, "NE": 12811, "NV": 12812, "NH": 12813, "NJ": 12814, "NM": 12815, "NY": 12816,
    "NC": 12817, "ND": 12818, "OH": 12819, "OK": 12820, "OR": 12821, "PA": 12822, "RI": 12823, "SC": 12824,
    "SD": 12825, "TN": 12826, "TX": 12827, "UT": 12828, "VT": 12829, "VA": 12830, "WA": 12831, "DC": 12832,
    "WV": 12833, "WI": 12834, "WY": 12835,
}
ICIMS_FRAME = "#icims_content_iframe"  # where a portal's search page draws its openings


def icims_page_url(cfg: Any, query: str, page: int = 0, state: str | None = None) -> str:
    where = {"searchLocation": f"-{ICIMS_STATES[state]}-"} if state in ICIMS_STATES else {}
    return f"https://{cfg}.icims.com/jobs/search?" + urlencode(
        {"ss": "1", "searchKeyword": query, "in_iframe": "1", **where, **({"pr": str(page)} if page else {})})


def icims_state(terms: list[str]) -> str | None:
    """The one state a search's place is in, for an iCIMS portal's own location filter (a
    national portal's Arizona openings are then a page or two, not six): None for anywhere,
    places in several states, or a place that could be remote ("Phoenix | Remote")."""
    if any(all(w in _BROAD_WORDS for w in t.split()) for t in terms
           if not t.startswith(IN_STATE) and t.upper() not in US_STATES):  # (Indiana's "in", Oregon's "or")
        return None
    states = {t.upper() for t in terms if t.upper() in US_STATES}
    states |= {t[len(IN_STATE):].upper() for t in terms if t.startswith(IN_STATE)}
    return states.pop() if len(states) == 1 else None


def _icims_last_page(html: str) -> int:
    """The last result page a search's page links go to (pr=0 is the first)."""
    return max((int(n) for n in re.findall(r"[?&](?:amp;)?pr=(\d+)", html)), default=0)


async def icims_search(frames_html: Callable[[str], Awaitable[list[str]]], cfg: Any, query: str,
                       found: list[Listing], state: str | None = None) -> None:
    """iCIMS portals (Daifuku America) answer plain requests with HTTP 405, so the search
    page is read in the browser. The openings are drawn inside the portal's frame, each
    with its location and posting date, 20 to a page: the first ICIMS_PAGES pages are read,
    of the openings in `state` when one is given."""
    base = f"https://{cfg}.icims.com"
    last = 0
    for page in range(ICIMS_PAGES):
        if page > last:
            break
        try:
            docs = await frames_html(icims_page_url(cfg, query, page, state))
        except Exception:
            if not page:
                raise
            break  # a later page that won't load ends the reading; what's found stands
        for doc in docs:
            found.extend(parse_icims(doc, base))
            last = max(last, _icims_last_page(doc))


_ICIMS_PLACE = re.compile(r"^(job )?locations?$", re.I)  # not "Location Type", "Remote Location Eligible"


def _icims_detail_place(row: Any) -> str:
    """The place among a row's details: the one named "Location" (read out to screen readers
    as "Location : Location")."""
    for tag in row.select(".iCIMS_JobHeaderTag"):
        name, value = tag.select_one("dt"), tag.select_one("dd")
        words = [w.strip() for w in name.get_text(" ", strip=True).split(":")] if name is not None else []
        if value is not None and words and all(_ICIMS_PLACE.match(w) for w in words if w):
            return value.get_text(" ", strip=True)
    return ""


def parse_icims(html: str, base: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    seen: set[str] = set()
    for a in soup.select('a[href*="/jobs/"]'):
        m = re.search(r"/jobs/(\d+)/[^/?#]+/job", str(a.get("href") or ""))
        if not m:
            continue
        url = urljoin(base + "/", str(a["href"]).split("?")[0])  # the full page, not the frame's copy
        if url in seen:
            continue
        seen.add(url)
        heading = a.find(["h2", "h3", "h4"])
        for label in a.select(".sr-only, .field-label"):  # "External Title", read out to screen readers
            label.decompose()
        title = (heading or a).get_text(" ", strip=True) or re.sub(r"^\d+\s*-\s*", "", str(a.get("title") or ""))
        row = a.find_parent(class_="row")
        location = posted = ""
        if row is not None:
            left = row.select_one(".header.left")  # Job Locations: US-AZ-Chandler
            if left is not None:
                location = " ".join(s.get_text(" ", strip=True) for s in left.find_all("span", recursive=False)
                                    if "sr-only" not in (s.get("class") or []))
            if not location.strip():  # or among the row's details (Aerotek's): Location: US-WI-Stoughton
                location = _icims_detail_place(row)
            when = row.select_one(".header.right span[title]")  # Posted Date: 9/24/2026 6:18 PM
            if when is not None:
                d = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(when["title"]))
                posted = f"{d.group(3)}-{int(d.group(1)):02d}-{int(d.group(2)):02d}" if d else ""
        out.append(Listing(company="", title=title, url=url, location=location.strip(), posted=posted,
                           external_id=m.group(1), ats="icims"))
    return out



_REMOTEISH = re.compile(r"\b(remote|telework|virtual|statewide|various|multiple)\b", re.I)


# ----------------------------------------------------------------- Taleo career sections (Kforce)
TALEO_PAGES = 4  # pages per wording; 25 openings a page


def _taleo_body(query: str, page: int) -> dict[str, Any]:
    """The search the career section's own page sends: a keyword, no filters, newest first."""
    filters = ["POSTING_DATE", "LOCATION", "JOB_FIELD", "JOB_TYPE", "JOB_SCHEDULE", "JOB_LEVEL"]
    advanced = ["ORGANIZATION", "LOCATION", "JOB_FIELD", "JOB_NUMBER", "URGENT_JOB", "EMPLOYEE_STATUS", "STUDY_LEVEL",
                "WILL_TRAVEL", "JOB_SHIFT"]
    return {"multilineEnabled": False, "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
            "fieldData": {"fields": {"KEYWORD": query, "LOCATION": ""}, "valid": True},
            "filterSelectionParam": {"searchFilterSelections": [{"id": f, "selectedValues": []} for f in filters]},
            "advancedSearchFiltersSelectionParam": {
                "searchFilterSelections": [{"id": f, "selectedValues": []} for f in advanced]},
            "pageNo": page}


async def _taleo(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """Taleo career sections answer their own page's search with JSON, 25 openings a page.
    It needs the time zone headers the page sends (without them: HTTP 500)."""
    host, section, portal = cfg["host"], cfg.get("section", "ex"), cfg["portal"]
    api = f"https://{host}/careersection/rest/jobboard/searchjobs?" + urlencode({"lang": "en", "portal": portal})
    headers = {"Accept": "application/json", "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest",
               "tz": "GMT-07:00", "tzname": "America/Phoenix"}
    out: list[Listing] = []
    for page in range(1, TALEO_PAGES + 1):
        r = await _send(client, "POST", api, json=_taleo_body(query, page), headers=headers)
        _raise_for(r, api)
        data = r.json()
        rows = data.get("requisitionList") or []
        for req in rows:
            columns = [str(c) for c in req.get("column") or []]
            linked = req.get("linkedColumn")
            title = (columns[linked] if isinstance(linked, int) and 0 <= linked < len(columns) else
                     columns[0] if columns else "").strip()
            job = str(req.get("contestNo") or req.get("jobId") or "")
            if not title or not job:
                continue
            places = [p for i in req.get("locationsColumns") or [] if isinstance(i, int) and 0 <= i < len(columns)
                      for p in _taleo_places(columns[i])]
            when = next((d for d in map(_sf_date, columns[1:]) if d), "")
            out.append(Listing(company="", title=title, ats="taleo", external_id=job,
                               url=f"https://{host}/careersection/{section}/jobdetail.ftl?" + urlencode({"job": job,
                                                                                                        "lang": "en"}),
                               location="; ".join(_taleo_place(x) for x in places), posted=when))
        size = int((data.get("pagingData") or {}).get("pageSize") or 25)
        if len(rows) < size or len(out) >= limit:  # (its total can be more than it lists: a short page ends it)
            break
    return out


def _taleo_places(column: str) -> list[str]:
    """A row's places: a JSON list in a string (Kforce's), or the place as it's written."""
    try:
        value = json.loads(column)
    except ValueError:
        value = column
    return [str(x).strip() for x in (value if isinstance(value, list) else [value]) if str(x).strip()]


def _taleo_place(place: str) -> str:
    """Taleo's "Arizona-Phoenix" (state, then town) as "Phoenix, Arizona"."""
    state, _, town = place.partition("-")
    return f"{town.strip()}, {state.strip()}" if town.strip() and state.strip() else place.strip()


# ----------------------------------------------------------------- Talemetry job sites (Valleywise Health)
TALEMETRY_PAGES = 4  # pages per wording; 25 openings a page
TALEMETRY_PAGE = 25


async def _talemetry(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """Symplr's Talemetry job sites (jobs.valleywisehealth.org) draw their search results on
    the server, 25 a page, each with its place."""
    base = str(cfg).rstrip("/")
    out: list[Listing] = []
    for page in range(1, TALEMETRY_PAGES + 1):
        url = f"{base}/jobs/search?" + urlencode({"q": query, "page": page})
        r = await _send(client, "GET", url)
        _raise_for(r, url)
        rows = parse_talemetry(r.text, base)
        out.extend(rows)
        if len(rows) < TALEMETRY_PAGE or len(out) >= limit:
            break
    return out


def parse_talemetry(html: str, base: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    for item in soup.select("div.jobs-section__item"):
        link = item.select_one("h4 a[href]")
        if link is None or not link.get_text(strip=True):
            continue
        url = urljoin(base + "/", str(link["href"]))
        marker = item.select_one("i.fa-map-marker")
        cell = marker.find_parent(class_="jobcardtext") if marker is not None else None
        m = re.search(r"/jobs/(\d+)", url)
        out.append(Listing(company="", title=link.get_text(" ", strip=True), url=url, ats="talemetry",
                           location=cell.get_text(" ", strip=True) if cell is not None else "",
                           external_id=m.group(1) if m else ""))
    return out


# ----------------------------------------------------------------- Phoenix Children's own job site
async def _phoenixchildrens(client: httpx.AsyncClient, cfg: Any, query: str, limit: int,
                            terms: list[str]) -> list[Listing]:
    """Phoenix Children's own job site lists every opening on one page (about 300), each
    with its department, schedule and place ("Recruitment | Full-Time | Phoenix")."""
    base = str(cfg).rstrip("/")
    return _titled(parse_phoenixchildrens(await _board_page(client, f"{base}/Positions/"), base), query)


def parse_phoenixchildrens(html: str, base: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    seen: set[str] = set()
    for item in soup.select("div.blog-item"):
        link = item.select_one('h2 a[href*="/Positions/Posting/"]')
        if link is None or not link.get_text(strip=True):
            continue
        url = urljoin(base + "/", str(link["href"]))
        if url in seen:
            continue
        seen.add(url)
        line = item.select_one("div.d-block")
        parts = [x.strip() for x in (line.get_text(" ", strip=True) if line is not None else "").split("|")]
        place = parts[-1] if len(parts) >= 3 else ""
        out.append(Listing(company="", title=link.get_text(" ", strip=True), url=url, ats="custom",
                           location=f"{place}, AZ" if place and not _REMOTEISH.search(place) else place,
                           external_id=url.rstrip("/").rsplit("/", 1)[-1]))
    return out


# ----------------------------------------------------------------- iCIMS Jibe job sites (PetSmart, Sprouts, State Farm)
JIBE_PAGES = 3  # pages per wording at most; 100 openings a page
JIBE_PAGE = 100
_ICIMS_APPLY = re.compile(r"^(https://[\w.-]+\.icims\.com)/jobs/(\d+)/login", re.I)


async def _jibe(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """iCIMS's Jibe job sites (jobs.sprouts.com, careers.petsmart.com) answer their own page's
    search with JSON, 100 openings a page, filtered to a state when the search is in one.
    Each opening links to its iCIMS posting, where the desk applies, when it has one."""
    host = urlsplit(str(cfg) if "//" in str(cfg) else f"https://{cfg}").hostname or str(cfg)
    state = icims_state(terms)
    # Not one state ("Phoenix | Remote", a town the plugin doesn't place): the national list is
    # read further, as its few local openings are anywhere in it (PetSmart's: 1900 of them)
    want = max(limit, AREA_SCAN) if terms and not state else limit
    out: list[Listing] = []
    for page in range(1, JIBE_PAGES + 1):
        params = {"keywords": query, "page": page, "limit": JIBE_PAGE, **({"location": US_STATES[state]} if state else {})}
        url = f"https://{host}/api/jobs?" + urlencode(params)
        r = await _send(client, "GET", url, headers={"Accept": "application/json"})
        _raise_for(r, url)
        jobs = r.json().get("jobs") or []
        for item in jobs:
            data = item.get("data") or {}
            title, slug = str(data.get("title") or "").strip(), str(data.get("slug") or data.get("req_id") or "")
            if not title or not slug:
                continue
            page_url = f"https://{host}/jobs/{quote(slug)}?lang=en-us"
            m = _ICIMS_APPLY.match(str(data.get("apply_url") or ""))
            where = data.get("full_location") or ", ".join(str(x) for x in (data.get("city"), data.get("state")) if x)
            out.append(Listing(company="", title=title, url=f"{m.group(1)}/jobs/{m.group(2)}/job" if m else page_url,
                               company_url=page_url if m else "", location=str(where or ""),
                               posted=str(data.get("posted_date") or "")[:10], external_id=str(data.get("req_id") or slug),
                               ats="icims" if m else "jibe"))
        if len(jobs) < JIBE_PAGE or len(out) >= want:
            break
    return out


# ----------------------------------------------------------------- Jobvite job boards (Knight-Swift)
JOBVITE_CATEGORIES = 10  # categories read past their first 20 openings ("Show More"), per search


async def _jobvite(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """Jobvite boards (jobs.jobvite.com/<company>/jobs) list their openings by category, the
    first 20 of each with its place; a longer category's "Show More" lists the rest on a
    search page of its own (Knight-Swift's Operations and Shop, Oct 2026)."""
    base = f"https://jobs.jobvite.com/{str(cfg).strip('/')}"
    html = await _board_page(client, f"{base}/jobs")
    found = parse_jobvite(html, base)
    more = dict.fromkeys(urljoin(base + "/", str(a["href"])) for a in BeautifulSoup(html, "html.parser").select(
        'a[href*="search?c="]'))
    for url in list(more)[:JOBVITE_CATEGORIES]:
        found += parse_jobvite(await _board_page(client, url), base)
    return _titled(list({x.url: x for x in found}.values()), query)


def parse_jobvite(html: str, base: str) -> list[Listing]:
    """A board's openings, as its category lists (div.jv-job-item) or a search page's table
    rows show them: each name's link and, beside it, its place."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[Listing] = []
    seen: set[str] = set()
    for name in soup.select(".jv-job-list-name"):
        link = name.select_one('a[href*="/job/"]')
        if link is None or not link.get_text(strip=True):
            continue
        url = urljoin(base + "/", str(link["href"]))
        if url in seen:
            continue
        seen.add(url)
        row = name.find_parent(class_="jv-job-item") or name.find_parent("tr") or name.parent
        where = row.select_one(".jv-job-list-location") if row is not None else None
        out.append(Listing(company="", title=link.get_text(" ", strip=True), url=url, ats="jobvite",
                           location=" ".join(where.get_text(" ", strip=True).split()) if where is not None else "",
                           external_id=url.rstrip("/").rsplit("/", 1)[-1]))
    return out


# ----------------------------------------------------------------- Randstad's own jobs (randstadusa.com)
RANDSTAD_PAGES = 4  # pages read nationwide; 30 openings a page


async def _randstad(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """Randstad's internal jobs (randstadusa.com/jobs/internal/) are listed in the page's own
    data (window.__ROUTE_DATA__), 30 a page: one state's on a page of its own
    (/jobs/internal/arizona/), the whole country's over several."""
    base = str(cfg).rstrip("/")
    state = icims_state(terms)
    urls = ([f"{base}/{US_STATES[state].lower().replace(' ', '-')}/"] if state else
            [f"{base}/"] + [f"{base}/page-{n}/" for n in range(2, RANDSTAD_PAGES + 1)])
    found: list[Listing] = []
    for url in urls:
        batch = parse_randstad(await _board_page(client, url))
        found += batch
        if not batch:
            break
    return _titled(list({x.url: x for x in found}.values()), query)


def parse_randstad(html: str) -> list[Listing]:
    at = html.find("window.__ROUTE_DATA__")
    start = html.find("{", at) if at >= 0 else -1
    if start < 0:
        return []
    try:
        data, _ = json.JSONDecoder().raw_decode(html, start)
    except ValueError:
        return []
    out: list[Listing] = []
    for hit in ((data.get("searchResults") or {}).get("hits") or []) if isinstance(data, dict) else []:
        title, url = str(hit.get("title") or "").strip(), str(hit.get("detailsUrl") or hit.get("applyUrl") or "")
        if not title or not url:
            continue
        where = hit.get("jobLocation") or {}
        out.append(Listing(company="", title=title, url=url.split("?")[0], ats="custom",
                           location=", ".join(str(x) for x in (where.get("city"), where.get("stateAbbreviation") or
                                                               where.get("state")) if x),
                           posted=_epoch_date(int(hit["createdDate"]) // 1000) if str(hit.get("createdDate") or "").isdigit()
                           else "", external_id=str(hit.get("atsReference") or "")))
    return out


# ----------------------------------------------------------------- m-cloud (a Google Cloud Talent job search)
MCLOUD_API = "https://jobsapi-google.m-cloud.io/api/job/search"
MCLOUD_PAGE = 100  # openings a request answers


async def _mcloud(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """Career sites whose search is a Google Cloud Talent search at jobsapi-google.m-cloud.io
    (Edward Jones'): the words go in, openings from anywhere come out, 100 a page. It takes no
    place, so a search in an area reads further, and keep_listings keeps the area's."""
    company = str(cfg["company"] if isinstance(cfg, dict) else cfg)  # a block without one fails loudly
    if terms:
        limit = max(limit, AREA_SCAN)
    out: dict[str, Listing] = {}
    offset = 0
    while len(out) < limit:
        r = await _send(client, "GET", MCLOUD_API, params={"companyName": company, "query": query,
                                                           "pageSize": MCLOUD_PAGE, "offset": offset})
        _raise_for(r, MCLOUD_API)
        data = r.json()
        hits = data.get("searchResults") or []
        for listing in parse_mcloud(data):  # an opening listed twice (two ids) counts once, by its number
            out.setdefault(listing.external_id or listing.url, listing)
        offset += len(hits)
        if not hits or offset >= int(data.get("totalHits") or 0):
            break
    return list(out.values())[:limit]


def parse_mcloud(data: dict[str, Any]) -> list[Listing]:
    found: list[Listing] = []
    for hit in data.get("searchResults") or []:
        job = hit.get("job") or {}
        title, url = str(job.get("title") or "").strip(), str(job.get("url") or "")
        if not title or not url:
            continue
        places = [", ".join(str(x) for x in (job.get("primary_city"), job.get("primary_state")) if x)]
        places += [", ".join(str(x) for x in (p.get("addtnl_city"), p.get("addtnl_state")) if x)
                   for p in job.get("addtnl_locations") or [] if isinstance(p, dict)]
        found.append(Listing(company="", title=title, url=url, ats="custom",
                             location="; ".join(dict.fromkeys(p for p in places if p)),
                             posted=str(job.get("open_date") or "")[:10], external_id=str(job.get("ref") or "")))
    return found


# ----------------------------------------------------------------- amazon.jobs
AMAZON_PAGE = 100  # openings a search reads: its first page, nearest the place first


async def _amazon(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """amazon.jobs answers its search page's own JSON search around a place: the employer
    list's address, latitude, longitude and radius (`loc_query`, `latitude`, `longitude`,
    `radius`). A search for anywhere searches the world. Each opening names every place it's
    offered in (a Seattle job is often in Tempe too)."""
    where = {k: cfg[k] for k in ("loc_query", "latitude", "longitude", "radius") if cfg.get(k) is not None} if terms else {}
    params = {"base_query": query, "result_limit": AMAZON_PAGE, "offset": 0, "sort": "relevant", **where}
    url = "https://www.amazon.jobs/en/search.json?" + urlencode(params)
    r = await _send(client, "GET", url, headers={"Accept": "application/json"})
    _raise_for(r, url)
    out: list[Listing] = []
    for job in r.json().get("jobs") or []:
        title, path = str(job.get("title") or "").strip(), str(job.get("job_path") or "")
        if not title or not path:
            continue
        places = _amazon_places(job.get("locations") or [])
        out.append(Listing(company="", title=title, url=urljoin("https://www.amazon.jobs/", path), ats="amazon",
                           location="; ".join(places) or str(job.get("normalized_location") or job.get("location") or ""),
                           posted=_sf_date(" ".join(str(job.get("posted_date") or "").split())),  # "July  9, 2026"
                           external_id=str(job.get("id_icims") or job.get("id") or "")))
    return out


def _amazon_places(locations: list[Any]) -> list[str]:
    """An opening's places, each a JSON object in a string: "Tempe, Arizona"."""
    places: list[str] = []
    for item in locations:
        try:
            place = json.loads(item) if isinstance(item, str) else item
        except ValueError:
            continue
        if isinstance(place, dict):
            town, state = str(place.get("city") or "").strip(), str(place.get("normalizedStateName") or "").strip()
            town = town.title() if town.isupper() else town  # "MESA" and "Mesa" are one place
            text = ", ".join(x for x in (town, state) if x) or str(place.get("normalizedCountryName") or "")
            if text and text.lower() not in (x.lower() for x in places):
                places.append(text)
    return places


# ----------------------------------------------------------------- Paycom (Ebara), through the browser
PAYCOM_TAKE = 100  # the page asks for 10 at a time
PAYCOM_PAGES = 3


def paycom_page_url(cfg: Any) -> str:
    return f"https://www.paycomonline.net/v4/ats/web.php/portal/{cfg}/career-page"


def paycom_rewrite(body: Any, skip: int = 0) -> Any:
    """Ask for PAYCOM_TAKE openings from `skip` instead of the page's first 10."""
    if not isinstance(body, dict) or "take" not in body:
        return None
    return {**body, "skip": skip, "take": PAYCOM_TAKE}


async def paycom_search(capture: Callable[..., Awaitable[Any]], cfg: Any, query: str,
                        found: list[Listing]) -> None:
    """Paycom career pages (Ebara) load their openings from Paycom's API under a session the
    page sets up, so the board is read in the browser, PAYCOM_TAKE openings per call, and
    titles are matched here. `capture` is BrowserSession.capture_json."""
    for page in range(PAYCOM_PAGES):
        skip = page * PAYCOM_TAKE
        data = await capture(paycom_page_url(cfg), "/job-posting-previews/search",
                             rewrite=lambda body, skip=skip: paycom_rewrite(body, skip))
        batch = parse_paycom(data, cfg)
        found.extend(listing for listing in batch if title_matches(listing.title, query))
        total = data.get("jobPostingPreviewsCount") if isinstance(data, dict) else None
        if skip + PAYCOM_TAKE >= total if isinstance(total, int) else len(batch) < PAYCOM_TAKE:
            return


def _paycom_place(text: str) -> str:
    """'Sacramento, CA - Sacramento, CA 95838' (the site, then its address) -> 'Sacramento, CA 95838'."""
    site, sep, address = text.partition(" - ")
    return address if sep and address.startswith(site) else text


def parse_paycom(data: Any, cfg: Any) -> list[Listing]:
    previews = data.get("jobPostingPreviews") or [] if isinstance(data, dict) else []
    out = []
    for p in previews:
        if not isinstance(p, dict) or not p.get("jobId"):
            continue
        title = re.sub(r"\s+", " ", str(p.get("jobTitle") or "")).strip()
        req = re.search(r"\s*\((\d+)\)$", title)  # "Field Service Technician II (33195)": Ebara's requisition number
        if req:
            title = title[:req.start()]
        listing = Listing(
            company="", title=title, url=f"https://www.paycomonline.net/v4/ats/web.php/portal/{cfg}/jobs/{p['jobId']}",
            location=_paycom_place(re.sub(r"\s+", " ", str(p.get("locations") or "")).strip()),
            posted=_paycom_date(p.get("postedOn")), external_id=req.group(1) if req else str(p["jobId"]), ats="paycom",
        )
        if p.get("remoteType"):
            listing.notes.append(f"Paycom lists it as {p['remoteType']}")
        out.append(listing)
    return out


def _paycom_date(value: Any) -> str:
    """'10/01/2026' or '2026-10-01T...' -> '2026-10-01'; Ebara's board leaves it empty."""
    text = str(value or "")
    us = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if us:
        return f"{us.group(3)}-{int(us.group(1)):02d}-{int(us.group(2)):02d}"
    return text[:10] if re.match(r"\d{4}-\d{2}-\d{2}", text) else ""



# ------------------------------------------------------- UKG Pro / UltiPro (Nikon), through the browser
UKG_TOP = 200  # the page asks for 50 at a time
UKG_PAGES = 3


def ukg_board_url(cfg: Any) -> str:
    """'https://recruiting2.ultipro.com/<TENANT>/JobBoard/<board id>/' (the board's own page)."""
    return str(cfg).split("?")[0].rstrip("/") + "/"


def ukg_rewrite(body: Any, skip: int = 0) -> Any:
    """Ask for UKG_TOP openings from `skip` instead of the page's first 50."""
    search = body.get("opportunitySearch") if isinstance(body, dict) else None
    if not isinstance(search, dict) or "Top" not in search:
        return None
    return {**body, "opportunitySearch": {**search, "Top": UKG_TOP, "Skip": skip}}


async def ukg_search(capture: Callable[..., Awaitable[Any]], cfg: Any, query: str, found: list[Listing]) -> None:
    """UKG Pro job boards (Nikon Precision) load their openings from the board's own API
    under the session its page sets up, so the board is read in the browser, UKG_TOP
    openings per call, and titles are matched here. `capture` is BrowserSession.capture_json."""
    board = ukg_board_url(cfg)
    for page in range(UKG_PAGES):
        skip = page * UKG_TOP
        data = await capture(board + "?q=&o=postedDateDesc", "/JobBoardView/LoadSearchResults",
                             rewrite=lambda body, skip=skip: ukg_rewrite(body, skip))
        batch = parse_ukg(data, board)
        found.extend(listing for listing in batch if title_matches(listing.title, query))
        total = data.get("totalCount") if isinstance(data, dict) else None
        if skip + UKG_TOP >= total if isinstance(total, int) else len(batch) < UKG_TOP:
            return


def _ukg_place(loc: Any) -> str:
    """'Chandler, AZ' from the address, else the board's own words for it ('Phoenix, AZ')."""
    if not isinstance(loc, dict):
        return ""
    address = loc.get("Address") or {}
    state = (address.get("State") or {}).get("Code") or (address.get("State") or {}).get("Name") or ""
    if address.get("City"):
        return ", ".join(x for x in (address["City"], state) if x)
    return loc.get("LocalizedDescription") or loc.get("LocalizedName") or state


def parse_ukg(data: Any, board: str) -> list[Listing]:
    opportunities = data.get("opportunities") or [] if isinstance(data, dict) else []
    out = []
    for o in opportunities:
        if not isinstance(o, dict) or not o.get("Id") or not o.get("Title"):
            continue
        places = list(dict.fromkeys(p for p in (_ukg_place(loc) for loc in o.get("Locations") or []) if p))
        out.append(Listing(
            company="", title=str(o["Title"]).strip(), url=f"{board}OpportunityDetail?opportunityId={o['Id']}",
            location="; ".join(places), posted=str(o.get("PostedDate") or "")[:10],
            external_id=str(o.get("RequisitionNumber") or o["Id"]), ats="ukg",
        ))
    return out


# ---------------------------------- SuccessFactors' newer job search (Edwards), through the browser
RMK_PAGE = 10  # the API answers 10 at a time
RMK_PAGES = 5
_STATE_SUFFIX = re.compile(r"(?:\s[-–]\s|\s\(|,\s)([A-Z]{2})\)?\s*$")  # "Onsite Service Engineer - AZ"


def rmk_wants(body: Any) -> bool:
    """The page's search call, not its facet-only first one."""
    return isinstance(body, dict) and "pageNumber" in body and not body.get("facetingOnly")


def rmk_rewrite(body: Any, query: str, page: int) -> Any:
    if not rmk_wants(body):
        return None
    return {**body, "keywords": query, "pageNumber": page}


async def rmk_search(capture: Callable[..., Awaitable[Any]], cfg: Any, query: str, found: list[Listing],
                     read: Callable[[str], Awaitable[str]] | None = None) -> None:
    """SuccessFactors' newer career sites (Edwards on jobs.atlascopcogroup.com) search through
    /services/recruiting/v1/jobs, which answers only the page's own session, so the search page
    (filtered to the employer and country by its address) is read in the browser, its own search
    call given the wording and a page number. The answer carries no locations, and the site's
    own location search finds nothing (Oct 2026): some titles end with the state ("Onsite
    Service Engineer - AZ"); the rest are read off each posting."""
    url = str(cfg["url"] if isinstance(cfg, dict) else cfg).strip()
    if not re.match(r"https?://", url, re.I):  # a person's own list may leave it off, as SuccessFactors' may
        url = "https://" + url.lstrip("/")
    site = re.match(r"https?://[^/]+", url, re.I)
    if site is None:
        raise ValueError(f"rmk: {url!r} isn't a search page's address")
    origin = site.group(0)
    mine: list[Listing] = []
    for page in range(RMK_PAGES):
        data = await capture(url, "/services/recruiting/v1/jobs", want=rmk_wants,
                             rewrite=lambda body, page=page: rmk_rewrite(body, query, page))
        batch = parse_rmk(data, origin)
        mine.extend(batch)
        total = data.get("totalJobs") if isinstance(data, dict) else None
        if (page + 1) * RMK_PAGE >= total if isinstance(total, int) else len(batch) < RMK_PAGE:
            break
    known = {x.url: x.location for x in found if x.location}  # read for an earlier wording
    for listing in mine:
        listing.location = listing.location or known.get(listing.url, "")
    await rmk_places(mine, read)
    found.extend(mine)


RMK_PLACE_PAGES = 30  # postings read for their place, per search


async def read_page(url: str) -> str:
    """A page over plain HTTP, as the searches read them."""
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                                 follow_redirects=True, timeout=20) as client:
        r = await _send(client, "GET", url)
        _raise_for(r, url)
        return r.text


async def rmk_places(listings: list[Listing], read: Callable[[str], Awaitable[str]] | None = None) -> None:
    """Fill in the place of each listing that has none from its posting, whose header names
    the city and state ("Field Service Engineer · Service · Phoenix AZ · United States")."""
    read = read or read_page
    sem = asyncio.Semaphore(4)

    async def one(listing: Listing) -> None:
        async with sem:
            try:
                page = await read(listing.url)
            except Exception:  # an unreadable posting stays "check the posting"
                return
        listing.location = successfactors_place(BeautifulSoup(page, "html.parser")) or listing.location

    await asyncio.gather(*(one(x) for x in [x for x in listings if not x.location][:RMK_PLACE_PAGES]))


def _rmk_date(value: Any) -> str:
    """'7/27/26' -> '2026-07-27'."""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})$", str(value or "").strip())
    if not m:
        return ""
    year = int(m.group(3))
    return f"{year + 2000 if year < 100 else year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"


def parse_rmk(data: Any, origin: str) -> list[Listing]:
    results = data.get("jobSearchResult") or [] if isinstance(data, dict) else []
    out = []
    for item in results:
        job = item.get("response") if isinstance(item, dict) else None
        if not isinstance(job, dict) or not job.get("id"):
            continue
        title = html.unescape(str(job.get("unifiedStandardTitle") or job.get("title") or "")).strip()
        slug = job.get("unifiedUrlTitle") or job.get("urlTitle") or re.sub(r"\W+", "-", title)
        locale = (job.get("supportedLocales") or ["en_US"])[0]
        state = _STATE_SUFFIX.search(title)
        out.append(Listing(
            company="", title=title, url=f"{origin}/job/{slug}/{job['id']}-{locale}",
            location=state.group(1) if state and state.group(1) in US_STATES else "",
            posted=_rmk_date(job.get("unifiedStandardStart")), external_id=str(job["id"]), ats="successfactors",
        ))
    return out


# ------------------------------------------------- SuccessFactors career sites' search pages (Qorvo, SRP)
SF_PAGES = 4  # pages per wording; a page holds 25 rows (Qorvo's table) or up to 100 tiles (SRP's)
_SF_MORE = re.compile(r"\+\s*(\d+)\s*more\W*$", re.I)  # "Greensboro, NC, US, 27409 +3 more…"
_SF_MORE_SHOWN = re.compile(r"\(\+\d+ more\)")
_SF_TOTAL = re.compile(r"of\s+([\d,]+)")  # "Results 1 – 25 of 120"


def _sf_state(terms: list[str]) -> str:
    """The state to hand the site's own location search ('Arizona' for 'AZ'), so openings
    elsewhere don't crowd the user's area off the pages read."""
    return next((name for name in US_STATES.values() if norm(name) in terms), "")


async def _successfactors(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """SuccessFactors career sites (Qorvo, Salt River Project) draw their search results as an
    HTML table or as tiles, a page at a time. Given a state, the site's own location search narrows them; it also
    finds openings whose first place is elsewhere ("Greensboro, NC +3 more")."""
    site = str(cfg["url"] if isinstance(cfg, dict) else cfg).rstrip("/")
    if "://" not in site:
        site = f"https://{site}"
    state = _sf_state(terms)
    url = f"{site}/search/"
    out: list[Listing] = []
    start = 0  # the next page's first row, by the rows the site drew (not the ones kept)
    for _ in range(SF_PAGES):
        params: dict[str, Any] = {"q": query, "startrow": start}
        if state:
            params["locationsearch"] = state
        r = await _send(client, "GET", url, params=params)
        _raise_for(r, url)
        batch, total, drawn = _parse_sf(r.text, site)
        start += drawn
        for listing in batch:
            if state and _SF_MORE_SHOWN.search(listing.location) and location_matches(listing.location, terms) is not True:
                # one of its other places is in the state, or the site wouldn't have listed it
                listing.location = f"{listing.location}; {state}"
        out.extend(batch)
        if not drawn or start >= (total if total is not None else limit) or len(out) >= limit:
            break
    return out


def _sf_date(text: str) -> str:
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_successfactors(page: str, site: str) -> tuple[list[Listing], int | None]:
    """The openings on one page of a SuccessFactors search, and how many the search found."""
    listings, total, _ = _parse_sf(page, site)
    return listings, total


def _parse_sf(page: str, site: str) -> tuple[list[Listing], int | None, int]:
    """A SuccessFactors search page's openings, drawn as table rows (Qorvo's) or as tiles
    (Salt River Project's), each with its place and date; how many the search found; and how
    many rows the page drew (where the next page starts)."""
    soup = BeautifulSoup(page, "html.parser")
    out: list[Listing] = []
    seen: set[str] = set()
    rows = soup.select("tr.data-row") or soup.select("li.job-tile")  # (a table's page with a few tiles: its rows)
    for row in rows:
        link = row.select_one("a.jobTitle-link[href]")
        if link is None:
            continue
        url = urljoin(site + "/", str(link["href"]))
        title = " ".join(link.get_text(" ", strip=True).split())
        if not title or url in seen:
            continue
        seen.add(url)
        cell = (row.select_one(".colLocation") or row.select_one(".jobLocation")
                or row.select_one('.section-field.location [id$="location-value"]'))
        if cell is not None:
            where = re.sub(r"\s+", " ", cell.get_text(" ", strip=True))
        else:  # a tile site that names its place by town and state fields
            where = ", ".join(" ".join(c.get_text(" ", strip=True).split()) for c in
                              row.select('.section-field.city [id$="-value"], .section-field.state [id$="-value"]'))
        more = _SF_MORE.search(where)
        if more:  # "Greensboro, NC, US, 27409 (+3 more)"
            where = f"{where[:more.start()].strip()} (+{more.group(1)} more)"
        when = (row.select_one(".colDate .jobDate") or row.select_one(".jobDate")
                or row.select_one('.section-field.date [id$="date-value"]'))
        job_id = re.search(r"/(\d+)/?(?:\?|$)", str(link["href"]))
        out.append(Listing(company="", title=title, url=url, location=where,
                           posted=_sf_date(when.get_text(" ", strip=True)) if when is not None else "",
                           external_id=job_id.group(1) if job_id else "", ats="successfactors"))
    label = soup.select_one(".paginationLabel, #tile-search-results-label")  # "Showing 1 to 16 of 16 Jobs"
    total = _SF_TOTAL.search(label.get_text(" ", strip=True)) if label is not None else None
    return out, int(total.group(1).replace(",", "")) if total else None, len(rows)


# SuccessFactors' older career sites (career8.successfactors.com, Amkor): the page's script
# draws the job list (10 to a page, or 50 when asked), and its rows name no place.
SFCLASSIC_ROWS = "tr.jobResultItem"
SFCLASSIC_PER_PAGE = ("li.per_page select", "50")
SFCLASSIC_NEXT = "li.paginationArrowContainer.next > a"
SFCLASSIC_PLACE_PAGES = 30  # postings read for their place, per search


def sfclassic_page_url(cfg: Any) -> str:
    return f"{cfg['site'].rstrip('/')}/career?company={cfg['company']}&career_ns=job_listing_summary&navBarLevel=JOB_SEARCH"


def sfclassic_posting_url(cfg: Any, req_id: str) -> str:
    """A posting's address without the session the list was drawn in (it opens on its own)."""
    return (f"{cfg['site'].rstrip('/')}/career?career_ns=job_listing&company={cfg['company']}&navBarLevel=JOB_SEARCH"
            f"&rcm_site_locale=en_US&career_job_req_id={req_id}&selected_lang=en_US")


def parse_sfclassic(page: str, cfg: Any) -> list[Listing]:
    """The openings on one page of an older SuccessFactors career site's job list."""
    out: list[Listing] = []
    for row in BeautifulSoup(page, "html.parser").select(SFCLASSIC_ROWS):
        link = row.select_one("a.jobTitle[href]")
        req = re.search(r"career_job_req_id=(\d+)", str(link["href"])) if link is not None else None
        if link is None or req is None:
            continue
        note = row.select_one(".noteSection")
        posted = re.search(r"Posted on (\d{1,2}/\d{1,2}/\d{4})", note.get_text(" ", strip=True)) if note else None
        out.append(Listing(company="", title=link.get_text(" ", strip=True), url=sfclassic_posting_url(cfg, req.group(1)),
                           posted=_sf_date(posted.group(1)) if posted else "", external_id=req.group(1),
                           ats="successfactors"))
    return out


async def sfclassic_search(pages_of: Callable[..., Awaitable[list[str]]], cfg: Any, query: str, found: list[Listing],
                           read: Callable[[str], Awaitable[str]] | None = None) -> None:
    """Search an older SuccessFactors career site (Amkor's): its whole list is read in the
    browser and titles are matched here. `pages_of` is BrowserSession.listing_pages. The rows
    say nothing of where a job is, so each match's posting is read for the place its text
    names ("based at our headquarters in Tempe, AZ")."""
    pages = await pages_of(sfclassic_page_url(cfg), SFCLASSIC_ROWS, per_page=SFCLASSIC_PER_PAGE,
                           next_button=SFCLASSIC_NEXT)
    known = {x.url for x in found}
    mine: list[Listing] = []
    for page in pages:
        for listing in parse_sfclassic(page, cfg):
            if listing.url not in known and title_matches(listing.title, query):
                known.add(listing.url)
                mine.append(listing)
    read = read or read_page
    sem = asyncio.Semaphore(4)

    async def place(listing: Listing) -> None:
        async with sem:
            try:
                listing.location = place_in_text(html_to_text(await read(listing.url)))
            except Exception:  # an unreadable posting stays "check the posting"
                return

    await asyncio.gather(*(place(x) for x in mine[:SFCLASSIC_PLACE_PAGES]))
    found.extend(mine)


# Infor CloudSuite HCM job boards (Benchmark): the board's page asks for its postings 10 at a
# time, newest first, in its call's address ("pagesize=10").
INFOR_LIST = "JobPosting.JobSearchCardViewList"
INFOR_PAGE = 200
_STATE_BY_NAME = {name.lower(): code for code, name in US_STATES.items()}


def infor_rewrite(url: str) -> str:
    return re.sub(r"([?&]pagesize=)\d+", rf"\g<1>{INFOR_PAGE}", url)


async def infor_search(capture: Callable[..., Awaitable[Any]], cfg: Any, query: str, found: list[Listing]) -> None:
    """Search an Infor CloudSuite job board: the page's own call is asked for 200 postings
    (the newest; a board with more lists its oldest beyond those), and titles are matched
    here. `capture` is BrowserSession.capture_json."""
    data = await capture(cfg["url"], INFOR_LIST, rewrite_url=infor_rewrite)
    known = {x.url for x in found}
    found.extend(x for x in parse_infor(data) if x.url not in known and title_matches(x.title, query))


def _infor_place(value: str) -> str:
    """'Arizona:Tempe' -> 'Tempe, AZ'; 'MX:BC:Tijuana' -> 'Tijuana, BC, MX'."""
    parts = [p.strip() for p in value.split(":") if p.strip()]
    if len(parts) >= 2 and parts[0].lower() in _STATE_BY_NAME:
        return f"{parts[-1]}, {_STATE_BY_NAME[parts[0].lower()]}"
    return ", ".join(reversed(parts))


def parse_infor(data: Any) -> list[Listing]:
    view = data.get("dataViewSet") if isinstance(data, dict) else None
    out: list[Listing] = []
    for item in (view or {}).get("data") or []:
        fields = item.get("fields") if isinstance(item, dict) else None
        if not isinstance(fields, dict):
            continue

        def value(key: str) -> str:  # (called only in this pass of the loop)
            v = fields.get(key)  # noqa: B023
            return str(v.get("value") or "") if isinstance(v, dict) else ""

        title = value("Description").strip()
        # the card's link to the posting: <a href="https://CSS-...COM:443/hcm/Jobs/navigation/JobPosting...">
        link = re.search(r'href="([^"]+)"', value("_op_JobPostingCardViewLabelLinkBack_spc_translation_cp_"))
        if not title or not link:
            continue
        parts = urlsplit(html.unescape(link.group(1)))
        begin = value("PostingDateRange_prd_Begin")
        out.append(Listing(company="", title=title, url=urlunsplit(("https", parts.hostname or "", parts.path, parts.query, "")),
                           location=_infor_place(value("LocationOfJob")),
                           posted=f"{begin[:4]}-{begin[4:6]}-{begin[6:8]}" if re.fullmatch(r"\d{8}", begin) else "",
                           external_id=value("JobId") or value("JobRequisition"), ats="infor"))
    return out


async def _eightfold(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    host, domain = cfg["host"], cfg["domain"]
    api = f"https://{host}/api/pcsx/search"
    headers = {"Accept": "application/json", "Referer": f"https://{host}/careers"}
    out: list[Listing] = []
    start = 0
    if terms:  # the search takes no place ("Arizona" finds nothing): Arizona's can be far down the list
        limit = max(limit, AREA_SCAN)
    while len(out) < limit:
        params = {"domain": domain, "query": query, "location": "", "start": start}
        r = await _send(client, "GET", api, params=params, headers=headers)
        if r.status_code == 403 and "not enabled" in r.text:  # Insight's site: "PCSX is not enabled for this user"
            return await _eightfold_v2(client, host, domain, query, limit, terms)
        _raise_for(r, api)
        data = r.json()
        batch = parse_eightfold(data, host)
        out.extend(batch)
        start += len(batch)
        total = (data.get("data") or {}).get("count") if isinstance(data.get("data"), dict) else data.get("count")
        if not batch or start >= int(total or 0):
            break
    return out[:limit]


async def _eightfold_v2(client: httpx.AsyncClient, host: str, domain: str, query: str, limit: int,
                        terms: list[str]) -> list[Listing]:
    """Eightfold's older search (/api/apply/v2/jobs), for a site whose newer one is switched off
    (Insight Enterprises', Oct 2026): 10 openings a page, filtered to a state when the search
    is in one."""
    api = f"https://{host}/api/apply/v2/jobs"
    state = icims_state(terms)
    if terms and not state:
        limit = max(limit, AREA_SCAN)
    out: list[Listing] = []
    start = 0
    while len(out) < limit:
        params = {"domain": domain, "query": query, "start": start, "num": 10,
                  **({"location": US_STATES[state]} if state else {})}
        r = await _send(client, "GET", api, params=params, headers={"Accept": "application/json"})
        _raise_for(r, api)
        data = r.json()
        positions = data.get("positions") or []
        start += len(positions)
        batch = []
        for p in positions:
            if not p.get("name") or not p.get("id"):
                continue
            places = [", ".join(x.strip() for x in str(place).split(",")) for place in p.get("locations") or []]
            batch.append(Listing(company="", title=str(p["name"]).strip(), ats="eightfold",
                                 url=p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p['id']}",
                                 location="; ".join(places) or str(p.get("location") or ""),
                                 posted=_epoch_date(p.get("t_create")),
                                 external_id=str(p.get("display_job_id") or p.get("ats_job_id") or p["id"])))
        out.extend(batch)
        if not positions or start >= int(data.get("count") or 0):
            break
    return out[:limit]


SMARTRECRUITERS_PAGE = 100  # the most its API answers at once


async def _smartrecruiters(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    api = f"https://api.smartrecruiters.com/v1/companies/{cfg}/postings"
    found: list[dict[str, Any]] = []
    if terms:  # the area is filtered here, after the search: its openings can be far down the list
        limit = max(limit, AREA_SCAN)
    while len(found) < limit:  # a page at a time: the area's openings can be past the first
        r = await _send(client, "GET", api, params={"q": query, "limit": SMARTRECRUITERS_PAGE, "offset": len(found)})
        _raise_for(r, api)
        data = r.json()
        page = data.get("content") or []
        found += page
        if not page or len(found) >= int(data.get("totalFound") or 0):
            break
    out = []
    for p in found:
        loc = p.get("location") or {}
        where = ", ".join(x for x in [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()] if x)
        out.append(Listing(
            company="", title=p.get("name") or "", url=f"https://jobs.smartrecruiters.com/{cfg}/{p.get('id')}",
            location=where, posted=(p.get("releasedDate") or "")[:10],
            external_id=p.get("refNumber") or p.get("id") or "", ats="smartrecruiters",
        ))
    return out[:limit]


async def _oracle(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    # The same request the career site's own search makes (copied from a live probe),
    # relevance-ranked; the keyword is ignored if the finder differs. It has no place
    # filter here, so when the area is filtered afterwards more pages are read.
    host, site = cfg["host"], cfg["site"]
    keyword = query.replace('"', "").strip()
    facets = "%3B".join(["WORK_LOCATIONS", "WORKPLACE_TYPES", "TITLES", "CATEGORIES", "ORGANIZATIONS",
                         "POSTING_DATES", "FLEX_FIELDS", "LOCATIONS"])
    out: list[Listing] = []
    if terms:
        limit = max(limit, AREA_SCAN)
    for offset in range(0, max(limit, 1), ORACLE_PAGE):  # a page at a time: Arizona's openings can be past the first
        at = f"offset={offset}," if offset else ""  # the first page as the career site asks for it
        finder = (f"findReqs;siteNumber={site},facetsList={facets},limit={ORACLE_PAGE},{at}"
                  f"keyword={quote(chr(34) + keyword + chr(34), safe='')},sortBy={'RELEVANCY' if keyword else 'POSTING_DATES_DESC'}")
        api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
               "&expand=requisitionList.workLocation,requisitionList.otherWorkLocations,requisitionList.secondaryLocations,"
               f"flexFieldsFacet.values,requisitionList.requisitionFlexFields&finder={finder}")
        r = await _send(client, "GET", api, headers={"Accept": "application/json"})
        _raise_for(r, api)
        page = _oracle_listings(r.json(), host, site)
        out.extend(page)
        total = next((i.get("TotalJobsCount") for i in r.json().get("items") or [] if i.get("TotalJobsCount")), None)
        if len(page) < ORACLE_PAGE or total is not None and offset + ORACLE_PAGE >= int(total):
            break
    return out[:limit]


ORACLE_PAGE = 25  # the most Oracle's search answers at once
# Openings read per search when the site's own search can't take the area, so only some of
# what it finds are in it (onsemi lists 200+ technicians, Micron 250+)
AREA_SCAN = 200


def _oracle_listings(data: dict[str, Any], host: str, site: str) -> list[Listing]:
    out = []
    for item in data.get("items") or []:
        for req in item.get("requisitionList") or []:
            places = [req.get("PrimaryLocation") or ""] + _oracle_places(req)
            out.append(Listing(
                company="", title=req.get("Title") or "",
                url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req.get('Id')}",
                location=_merge_places(places), posted=req.get("PostedDate") or "",
                external_id=str(req.get("Id") or ""), ats="oracle_hcm",
            ))
    return out


def _merge_places(places: list[str]) -> str:
    """"Scottsdale, AZ, United States" and "Scottsdale, AZ, US" are one place; "Peoria, IL"
    and "Peoria, AZ" are two."""
    kept: dict[str, str] = {}
    cities: set[str] = set()  # cities named with their state
    for place in places:
        parts = [norm(x) for x in place.split(",")]
        key = " ".join(parts[:2]) if len(parts) > 1 else parts[0]  # its city and its state
        if len(parts) > 1:
            cities.add(parts[0])
        if key and key not in kept:
            kept[key] = place
    # "Phoenix" beside "Phoenix, AZ, US" is the same place, said with less
    return "; ".join(p for k, p in kept.items() if "," in p or k not in cities)


def _oracle_places(req: dict[str, Any]) -> list[str]:
    """A requisition's other places (the search asks for them). The primary location is
    sometimes just "United States", and a job can also be at sites other than the primary."""
    places = []
    for key in ("secondaryLocations", "workLocation", "otherWorkLocations"):
        for loc in req.get(key) or []:
            if not isinstance(loc, dict):
                continue
            parts = [str(loc[k]) for k in ("TownOrCity", "Region2", "Country") if loc.get(k)]
            name = loc.get("Name") or ", ".join(parts) or loc.get("LocationName")
            if name:
                places.append(str(name))
    return places


SEARCHERS: dict[str, Callable[[httpx.AsyncClient, Any, str, int, list[str]], Awaitable[list[Listing]]]] = {
    "workday": _workday,
    "greenhouse": _greenhouse,
    "lever": _lever,
    "eightfold": _eightfold,
    "smartrecruiters": _smartrecruiters,
    "oracle": _oracle,
    "applicantstack": _applicantstack,
    "successfactors": _successfactors,
    "taleo": _taleo,
    "talemetry": _talemetry,
    "phoenixchildrens": _phoenixchildrens,
    "jibe": _jibe,
    "jobvite": _jobvite,
    "amazon": _amazon,
    "randstad": _randstad,
    "mcloud": _mcloud,
}


def _epoch_date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


async def _send(client: httpx.AsyncClient, method: str, url: str, **kw: Any) -> httpx.Response:
    """The request, tried up to three times while the site reports a passing problem
    (Workday answers the odd search with a 502)."""
    for attempt in range(3):
        try:
            r = await client.request(method, url, **kw)
        except httpx.TransportError:
            if attempt == 2:
                raise
        else:
            if r.status_code not in RETRY_STATUS or attempt == 2:
                return r
        await asyncio.sleep(RETRY_DELAY * 2 ** attempt)
    raise AssertionError("unreachable")


def _raise_for(r: httpx.Response, url: str) -> None:
    if r.status_code >= 400:
        raise SearchError(f"HTTP {r.status_code} from {url.split('?')[0]}")


# --------------------------------------------------------------------- entry point


_COMPANY_WORDS = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "llc", "plc", "the", "group",
                  "holdings", "technologies", "technology", "international", "usa", "us", "america", "americas",
                  "north", "arizona", "az", "careers", "jobs"}


def _pick(companies: list[dict[str, Any]], names: list[str] | None) -> list[dict[str, Any]]:
    if not names:
        return companies
    wanted = [norm(n) for n in names]

    def words_in(part: str, whole: str) -> bool:  # whole words: "TEL" isn't in "Intel", nor "ASM" in "ASML"
        return bool(part) and f" {part} " in f" {whole} "

    def named(w: str, name: str) -> bool:
        # or the start of the name, when that's more than a short code: "Applied Material", "Micro";
        # or the name inside what was asked for when the rest only says what kind of company it
        # is ("Intel Corporation"), not when it names another ("Maricopa County Community
        # College District" isn't Maricopa County)
        if words_in(w, name) or len(w) >= 5 and name.startswith(w):
            return True
        return words_in(name, w) and set(w.split()) - set(name.split()) <= _COMPANY_WORDS

    return [c for c in companies if any(named(w, norm(c["name"])) for w in wanted)]


def keep_listings(company: str, listings: list[Listing], terms: list[str], limit: int,
                  query: str = "") -> list[dict[str, Any]]:
    """Deduplicate, apply the location filter, note broad locations, put listings whose
    title matches the query first (others matched on description), cap at `limit`."""
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    if query:
        listings = sorted(listings, key=lambda x: not title_matches(x.title, query))
    for listing in listings:
        if listing.url in seen:
            continue
        seen.add(listing.url)
        listing.company = company
        where = location_matches(listing.location, terms)
        if where is False:
            continue
        if where is None:
            listing.notes.append(f"location given as {listing.location!r}; check the posting" if listing.location
                                 else "no location given; check the posting")
        row = listing.to_dict()
        if query:
            row["title_match"] = title_matches(listing.title, query)
        kept.append(row)
        if len(kept) >= limit:
            break
    return kept


async def search_companies(
    query: str,
    names: list[str] | None = None,
    location: str | None = None,
    limit: int = 20,
    client: httpx.AsyncClient | None = None,
    companies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    companies = _pick(companies if companies is not None else load_companies(), names)
    terms = location_terms(location)
    own = client is None
    client = client or httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}, follow_redirects=True, timeout=20
    )
    results: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    browser_only: list[dict[str, str]] = []
    needs_browser: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(EMPLOYERS_AT_ONCE)

    async def one(company: dict[str, Any]) -> None:
        search = company.get("search") or {}
        kind = next((k for k in SEARCHERS if k in search), None)
        if kind is None:
            browser_kind = next((k for k in BROWSER_SEARCHES if k in search), None)
            if browser_kind:
                needs_browser.append({"company": company["name"], "kind": browser_kind, "config": search[browser_kind]})
            else:
                browser_only.append({"company": company["name"], "careers_url": company.get("careers_url", "")})
            return
        fetch = max(limit, FETCH_WHEN_FILTERING) if terms else limit
        found: list[Listing] = []
        wordings = [query] if kind in CLIENT_SIDE else alternatives(query)
        failed: list[str] = []
        async with sem:
            for wording in wordings:
                try:
                    found.extend(await SEARCHERS[kind](client, search[kind], wording, fetch, terms))
                except Exception as e:  # an odd response must not sink the other searches
                    failed.append(f"{type(e).__name__}: {str(e)[:200]}")
        if failed:
            partial = len(failed) < len(wordings)
            errors[company["name"]] = failed[0] + (
                f" ({len(failed)} of {len(wordings)} searches failed; the others' results are listed)" if partial else "")
        results.extend(keep_listings(company["name"], found, terms, limit, query))

    try:
        await asyncio.gather(*(one(c) for c in companies))
    finally:
        if own:
            await client.aclose()
    results.sort(key=lambda r: (r["company"], not r.get("title_match", True), r["title"]))
    return {"results": results, "errors": errors, "browser_only": browser_only, "needs_browser": needs_browser}
