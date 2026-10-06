"""Search a company's openings through its applicant tracking system's public JSON API.

Each company in data/companies.yaml may carry a `search` block naming one of:

    workday:         https://<tenant>.wd<N>.myworkdayjobs.com/<site>
    greenhouse:      <board token>
    lever:           <company slug>
    eightfold:       {host: careers.example.com, domain: example.com}
    smartrecruiters: <company identifier>
    oracle:          {host: xxxx.fa.us2.oraclecloud.com, site: CX_1001}

Companies without one (SuccessFactors sites, custom pages) are searched in the
browser instead. These endpoints are what the careers pages themselves call;
requests are few and sequential per company.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode

import httpx
import yaml

from . import config
from .ats import workday_parts
from .autofill import US_STATES, norm
from .postings import USER_AGENT

WORKDAY_PAGE = 20  # Workday rejects larger pages
MAX_ALTERNATIVES = 4
FETCH_WHEN_FILTERING = 60  # results to scan per search when filtering by location ourselves
CLIENT_SIDE = {"greenhouse", "lever"}  # whole board comes back at once; titles are filtered here
# Searches whose data only comes through the site's own page in the browser (ASML's
# Sitecore Discover widget). search_companies lists them under needs_browser and the
# search_company_jobs tool runs them.
BROWSER_SEARCHES = {"sitecore"}
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


def load_companies() -> list[dict[str, Any]]:
    path = config.PLUGIN_ROOT / "data" / "companies.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("companies", [])


def alternatives(query: str) -> list[str]:
    """'field service | equipment engineer' -> ['field service', 'equipment engineer']"""
    alts = [q.strip() for q in re.split(r"\s*\|\s*|\s+OR\s+", query or "") if q.strip()]
    return alts[:MAX_ALTERNATIVES] or [""]


def title_matches(title: str, query: str) -> bool:
    """Every word of at least one alternative appears in the title."""
    words = norm(title).split()
    text = " " + " ".join(words) + " "
    for alt in alternatives(query):
        terms = norm(alt).split()
        if all(f" {t}" in text for t in terms):  # prefix match: "engineer" ~ "engineering"
            return True
    return False


# Employers often list just the city ("Chandler (Office)"), so a state also matches its
# main metro areas. Only states the plugin's company list covers need entries.
STATE_CITIES = {
    "AZ": ["phoenix", "chandler", "tempe", "mesa", "scottsdale", "gilbert", "glendale", "peoria", "tucson",
           "goodyear", "surprise", "avondale", "casa grande", "queen creek", "maricopa"],
}


def location_terms(location: str | None) -> list[str]:
    """'AZ' -> ['az', 'arizona', 'phoenix', 'chandler', ...]; 'Phoenix|Chandler' -> ['phoenix', 'chandler']."""
    if not location:
        return []
    terms: list[str] = []
    for part in re.split(r"\s*[|,;]\s*", location):
        n = norm(part)
        if not n:
            continue
        terms.append(n)
        up = part.strip().upper()
        if up in US_STATES:
            terms.append(norm(US_STATES[up]))
            terms.extend(STATE_CITIES.get(up, []))
        for code, name in US_STATES.items():
            if n == norm(name):
                terms.append(code.lower())
                terms.extend(STATE_CITIES.get(code, []))
    return list(dict.fromkeys(terms))


_BROAD = re.compile(
    r"((\d+|multiple|various|several) locations?|anywhere|nationwide|united states( of america)?|usa?|u s a?)"
)


def location_matches(text: str, terms: list[str]) -> bool | None:
    """True/False, or None when the listing is too broad to tell ("3 Locations", "Remote - US")."""
    if not terms:
        return True
    n = norm(text)
    padded = f" {n} "
    if any(f" {t} " in padded for t in terms):
        return True
    if not n or _BROAD.fullmatch(n):
        return None
    if re.search(r"\bremote\b", n):
        # "Remote - US" could be done from anywhere; "Remote, Japan" can't
        rest = set(re.sub(r"\bremote\b", " ", n).split()) - {"us", "usa", "u", "s", "a", "united", "states", "of",
                                                                "america", "north", "nationwide", "anywhere", "in"}
        return None if not rest else False
    return False


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
                any_location = True
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
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"

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
            if nowhere_near and location_matches(p.get("locationsText", ""), terms) is None:
                continue
            out.append(Listing(
                company="", title=p.get("title", ""), url=f"https://{host}/{site}{p.get('externalPath') or ''}",
                location=p.get("locationsText", ""), posted=p.get("postedOn", ""),
                external_id=(p.get("bulletFields") or [""])[0], ats="workday",
            ))
        offset += WORKDAY_PAGE
        if not postings or len(out) >= limit or offset >= total:
            break
        data = await page(offset, applied)
    return out[:limit]


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
            location="; ".join(l for l in locs if l),
            external_id=str(p.get("display_job_id") or p.get("displayJobId") or p.get("id") or ""),
            posted=_epoch_date(p.get("t_create") or p.get("postedTs") or p.get("creationTs")), ats="eightfold",
        ))
    return out


# ------------------------------------------------- Sitecore Discover (ASML), through the browser
SITECORE_LIMIT = 100  # the page asks for 25 at a time; one call can cover a company's whole list


def sitecore_page_url(cfg: dict[str, Any], query: str) -> str:
    return str(cfg["url"]).format(query=quote(query))


def _sitecore_searches(body: Any) -> list[dict[str, Any]]:
    items = ((body or {}).get("widget") or {}).get("items") or [] if isinstance(body, dict) else []
    return [i["search"] for i in items if isinstance(i, dict) and isinstance(i.get("search"), dict)]


def sitecore_wants(body: Any) -> bool:
    """The page's own keyword search, not its facet-only first call."""
    return any((s.get("query") or {}).get("keyphrase") for s in _sitecore_searches(body))


def sitecore_rewrite(body: Any) -> Any:
    """Ask for SITECORE_LIMIT results in one go instead of the page's first 25."""
    searches = [s for s in _sitecore_searches(body) if (s.get("query") or {}).get("keyphrase")]
    for s in searches:
        s["limit"], s["offset"] = SITECORE_LIMIT, 0
    return body if searches else None


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


async def _eightfold(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    host, domain = cfg["host"], cfg["domain"]
    api = f"https://{host}/api/pcsx/search"
    headers = {"Accept": "application/json", "Referer": f"https://{host}/careers"}
    out: list[Listing] = []
    start = 0
    while len(out) < limit:
        params = {"domain": domain, "query": query, "location": "", "start": start}
        r = await _send(client, "GET", api, params=params, headers=headers)
        _raise_for(r, api)
        data = r.json()
        batch = parse_eightfold(data, host)
        out.extend(batch)
        start += len(batch)
        total = (data.get("data") or {}).get("count") if isinstance(data.get("data"), dict) else data.get("count")
        if not batch or start >= int(total or 0):
            break
    return out[:limit]


async def _smartrecruiters(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    api = f"https://api.smartrecruiters.com/v1/companies/{cfg}/postings"
    r = await _send(client, "GET", api, params={"q": query, "limit": min(limit, 100)})
    _raise_for(r, api)
    out = []
    for p in r.json().get("content") or []:
        loc = p.get("location") or {}
        where = ", ".join(x for x in [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()] if x)
        out.append(Listing(
            company="", title=p.get("name") or "", url=f"https://jobs.smartrecruiters.com/{cfg}/{p.get('id')}",
            location=where, posted=(p.get("releasedDate") or "")[:10],
            external_id=p.get("refNumber") or p.get("id") or "", ats="smartrecruiters",
        ))
    return out[:limit]


async def _oracle(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    # The same request the career site's own search makes (copied from a live probe):
    # one relevance-ranked page; the keyword is ignored if the finder differs.
    host, site = cfg["host"], cfg["site"]
    keyword = query.replace('"', "").strip()
    facets = "%3B".join(["WORK_LOCATIONS", "WORKPLACE_TYPES", "TITLES", "CATEGORIES", "ORGANIZATIONS",
                         "POSTING_DATES", "FLEX_FIELDS", "LOCATIONS"])
    finder = (f"findReqs;siteNumber={site},facetsList={facets},limit={min(limit, 25)},"
              f"keyword={quote(chr(34) + keyword + chr(34), safe='')},sortBy={'RELEVANCY' if keyword else 'POSTING_DATES_DESC'}")
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
           "&expand=requisitionList.workLocation,requisitionList.otherWorkLocations,requisitionList.secondaryLocations,"
           f"flexFieldsFacet.values,requisitionList.requisitionFlexFields&finder={finder}")
    r = await _send(client, "GET", api, headers={"Accept": "application/json"})
    _raise_for(r, api)
    out = []
    for item in r.json().get("items") or []:
        for req in item.get("requisitionList") or []:
            places = [req.get("PrimaryLocation") or ""] + _oracle_places(req)
            out.append(Listing(
                company="", title=req.get("Title") or "",
                url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req.get('Id')}",
                location=_merge_places(places), posted=req.get("PostedDate") or "",
                external_id=str(req.get("Id") or ""), ats="oracle_hcm",
            ))
    return out[:limit]


def _merge_places(places: list[str]) -> str:
    """"Scottsdale, AZ, United States" and "Scottsdale, AZ, US" are one place."""
    kept: list[str] = []
    cities: set[str] = set()
    for place in places:
        city = norm(place.split(",")[0])
        if city and city not in cities:
            cities.add(city)
            kept.append(place)
    return "; ".join(kept)


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


def _pick(companies: list[dict[str, Any]], names: list[str] | None) -> list[dict[str, Any]]:
    if not names:
        return companies
    wanted = [norm(n) for n in names]
    return [c for c in companies if any(w and (w in norm(c["name"]) or norm(c["name"]) in w) for w in wanted)]


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
            listing.notes.append(f"location given as {listing.location or 'nothing'!r}; check the posting")
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
    sem = asyncio.Semaphore(4)

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
