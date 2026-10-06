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
    if not n or _BROAD.fullmatch(n) or re.search(r"\bremote\b", n):
        return None
    return False


# --------------------------------------------------------------------- per-ATS
# Each searcher takes (client, config value, query, how many to fetch, location terms).


def _workday_location_facets(facets: Any, terms: list[str]) -> dict[str, list[str]]:
    """Pick the location facet values (Workday's own location filter) that match the terms.
    Returns {facetParameter: [ids]} for the one parameter covering the most postings."""
    found: dict[str, list[tuple[str, int]]] = {}

    def walk(items: Any, param: str | None) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            p = item.get("facetParameter") or param
            if p and "id" in item and "descriptor" in item and re.search(r"location|country|state|city|region", p, re.I):
                if location_matches(str(item["descriptor"]), terms) is True:
                    found.setdefault(p, []).append((str(item["id"]), int(item.get("count") or 0)))
            if item.get("values"):
                walk(item["values"], p)

    walk(facets, None)
    if not found:
        return {}
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
        r = await client.post(api, json=body, headers={"Accept": "application/json"})
        _raise_for(r, api)
        return r.json()

    first = await page(0, {})
    applied: dict[str, list[str]] = {}
    if terms and first.get("facets"):
        # Use the site's own location filter when one of its values names the area;
        # otherwise scan unfiltered results and filter them afterwards.
        applied = _workday_location_facets(first["facets"], terms)
        if applied:
            first = await page(0, applied)
    total = int(first.get("total") or 0)  # only the first page carries the total
    out: list[Listing] = []
    data, offset = first, 0
    while True:
        postings = data.get("jobPostings") or []
        for p in postings:
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
    r = await client.get(api)
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
    r = await client.get(api)
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


def parse_eightfold(data: dict[str, Any], host: str) -> list[Listing]:
    out = []
    for p in data.get("positions") or []:
        locs = p.get("locations") or [p.get("location") or ""]
        out.append(Listing(
            company="", title=p.get("name", ""),
            url=p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p.get('id')}",
            location="; ".join(l for l in locs if l), external_id=str(p.get("display_job_id") or p.get("id") or ""),
            posted=_epoch_date(p.get("t_create")), ats="eightfold",
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
    api = f"https://{host}/api/apply/v2/jobs"
    headers = {"Accept": "application/json", "Referer": f"https://{host}/careers"}
    out: list[Listing] = []
    start = 0
    while len(out) < limit:
        params = {"domain": domain, "start": start, "num": 10, "query": query, "sort_by": "relevance"}
        r = await client.get(api, params=params, headers=headers)
        _raise_for(r, api)
        data = r.json()
        batch = parse_eightfold(data, host)
        out.extend(batch)
        start += 10
        if not batch or start >= int(data.get("count") or 0):
            break
    return out[:limit]


async def _smartrecruiters(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    api = f"https://api.smartrecruiters.com/v1/companies/{cfg}/postings"
    r = await client.get(api, params={"q": query, "limit": min(limit, 100)})
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
    host, site = cfg["host"], cfg["site"]
    keyword = query.replace('"', "")
    out: list[Listing] = []
    offset = 0
    while len(out) < limit:
        page_size = min(25, limit - len(out))
        sort = "RELEVANCY" if keyword.strip() else "POSTING_DATES_DESC"
        finder = (f'findReqs;siteNumber={site},limit={page_size},offset={offset},keyword="{keyword}",'
                  f"sortBy={sort}")
        api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
               f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={quote(finder, safe='=;,')}")
        r = await client.get(api, headers={"Accept": "application/json"})
        _raise_for(r, api)
        batch = []
        for item in r.json().get("items") or []:
            for req in item.get("requisitionList") or []:
                batch.append(Listing(
                    company="", title=req.get("Title") or "",
                    url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req.get('Id')}",
                    location=req.get("PrimaryLocation") or "", posted=req.get("PostedDate") or "",
                    external_id=str(req.get("Id") or ""), ats="oracle_hcm",
                ))
        out.extend(batch)
        offset += page_size
        if len(batch) < page_size:
            break
    return out[:limit]


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
    sem = asyncio.Semaphore(4)

    async def one(company: dict[str, Any]) -> None:
        search = company.get("search") or {}
        kind = next((k for k in SEARCHERS if k in search), None)
        if kind is None:
            browser_only.append({"company": company["name"], "careers_url": company.get("careers_url", "")})
            return
        fetch = max(limit, FETCH_WHEN_FILTERING) if terms else limit
        found: list[Listing] = []
        try:
            async with sem:
                if kind in CLIENT_SIDE:
                    found = await SEARCHERS[kind](client, search[kind], query, fetch, terms)
                else:
                    for alt in alternatives(query):
                        found.extend(await SEARCHERS[kind](client, search[kind], alt, fetch, terms))
        except Exception as e:  # one company's odd response must not sink the others
            errors[company["name"]] = f"{type(e).__name__}: {str(e)[:200]}"
            return
        results.extend(keep_listings(company["name"], found, terms, limit, query))

    try:
        await asyncio.gather(*(one(c) for c in companies))
    finally:
        if own:
            await client.aclose()
    results.sort(key=lambda r: (r["company"], not r.get("title_match", True), r["title"]))
    return {"results": results, "errors": errors, "browser_only": browser_only}
