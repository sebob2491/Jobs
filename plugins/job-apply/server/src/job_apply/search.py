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
from urllib.parse import quote

import httpx
import yaml

from . import config
from .ats import workday_parts
from .autofill import US_STATES, norm
from .postings import USER_AGENT

WORKDAY_PAGE = 20  # Workday rejects larger pages
MAX_ALTERNATIVES = 4
FETCH_WHEN_FILTERING = 60  # results to scan per search when filtering by location ourselves


@dataclass
class Listing:
    company: str
    title: str
    url: str
    location: str = ""
    posted: str = ""
    external_id: str = ""
    ats: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def location_terms(location: str | None) -> list[str]:
    """'AZ' -> ['az', 'arizona']; 'Phoenix|Chandler' -> ['phoenix', 'chandler']."""
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
        for code, name in US_STATES.items():
            if n == norm(name):
                terms.append(code.lower())
    return list(dict.fromkeys(terms))


_BROAD = re.compile(r"(\d+|multiple|various|several) locations?|remote|anywhere|united states( of america)?|usa?|us remote")


def location_matches(text: str, terms: list[str]) -> bool | None:
    """True/False, or None when the listing is too broad to tell ("3 Locations", "Remote")."""
    if not terms:
        return True
    n = norm(text)
    if not n or _BROAD.fullmatch(n):
        return None
    padded = f" {n} "
    return any(f" {t} " in padded for t in terms)


# --------------------------------------------------------------------- per-ATS


async def _workday(client: httpx.AsyncClient, cfg: Any, query: str, limit: int) -> list[Listing]:
    parts = workday_parts(str(cfg).rstrip("/") + "/")
    if not parts:
        raise SearchError(f"Not a Workday site URL: {cfg}")
    host, tenant, site = parts["host"], parts["tenant"], parts["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    out: list[Listing] = []
    offset = 0
    while len(out) < limit:
        body = {"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset, "searchText": query}
        r = await client.post(api, json=body, headers={"Accept": "application/json"})
        _raise_for(r, api)
        data = r.json()
        postings = data.get("jobPostings") or []
        for p in postings:
            path = p.get("externalPath") or ""
            out.append(Listing(
                company="", title=p.get("title", ""), url=f"https://{host}/{site}{path}",
                location=p.get("locationsText", ""), posted=p.get("postedOn", ""),
                external_id=(p.get("bulletFields") or [""])[0], ats="workday",
            ))
        offset += WORKDAY_PAGE
        if not postings or offset >= int(data.get("total") or 0):
            break
    return out[:limit]


async def _greenhouse(client: httpx.AsyncClient, cfg: Any, query: str, limit: int) -> list[Listing]:
    api = f"https://boards-api.greenhouse.io/v1/boards/{cfg}/jobs"
    r = await client.get(api)
    _raise_for(r, api)
    out = [
        Listing(company="", title=j.get("title", ""), url=j.get("absolute_url", ""),
                location=(j.get("location") or {}).get("name", ""), posted=j.get("updated_at", ""),
                external_id=str(j.get("requisition_id") or j.get("id") or ""), ats="greenhouse")
        for j in r.json().get("jobs", [])
    ]
    return [x for x in out if title_matches(x.title, query)][:limit]


async def _lever(client: httpx.AsyncClient, cfg: Any, query: str, limit: int) -> list[Listing]:
    api = f"https://api.lever.co/v0/postings/{cfg}?mode=json"
    r = await client.get(api)
    _raise_for(r, api)
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        created = j.get("createdAt")
        out.append(Listing(
            company="", title=j.get("text", ""), url=j.get("hostedUrl", ""), location=cats.get("location", ""),
            posted=datetime.fromtimestamp(created / 1000, timezone.utc).date().isoformat() if created else "",
            external_id=j.get("id", ""), ats="lever",
        ))
    return [x for x in out if title_matches(x.title, query)][:limit]


async def _eightfold(client: httpx.AsyncClient, cfg: Any, query: str, limit: int) -> list[Listing]:
    host, domain = cfg["host"], cfg["domain"]
    api = f"https://{host}/api/apply/v2/jobs"
    out: list[Listing] = []
    start = 0
    while len(out) < limit:
        params = {"domain": domain, "start": start, "num": 10, "query": query, "sort_by": "relevance"}
        r = await client.get(api, params=params, headers={"Accept": "application/json"})
        _raise_for(r, api)
        data = r.json()
        positions = data.get("positions") or []
        for p in positions:
            locs = p.get("locations") or [p.get("location", "")]
            out.append(Listing(
                company="", title=p.get("name", ""),
                url=p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p.get('id')}",
                location="; ".join(l for l in locs if l), external_id=str(p.get("display_job_id") or p.get("id") or ""),
                posted=_epoch_date(p.get("t_create")), ats="eightfold",
            ))
        start += 10
        if not positions or start >= int(data.get("count") or 0):
            break
    return out[:limit]


async def _smartrecruiters(client: httpx.AsyncClient, cfg: Any, query: str, limit: int) -> list[Listing]:
    api = f"https://api.smartrecruiters.com/v1/companies/{cfg}/postings"
    r = await client.get(api, params={"q": query, "limit": min(limit, 100)})
    _raise_for(r, api)
    out = []
    for p in r.json().get("content", []):
        loc = p.get("location") or {}
        where = ", ".join(x for x in [loc.get("city"), loc.get("region"), loc.get("country", "").upper()] if x)
        out.append(Listing(
            company="", title=p.get("name", ""), url=f"https://jobs.smartrecruiters.com/{cfg}/{p.get('id')}",
            location=where, posted=(p.get("releasedDate") or "")[:10], external_id=p.get("refNumber") or p.get("id", ""),
            ats="smartrecruiters",
        ))
    return out[:limit]


async def _oracle(client: httpx.AsyncClient, cfg: Any, query: str, limit: int) -> list[Listing]:
    host, site = cfg["host"], cfg["site"]
    keyword = query.replace('"', "")
    finder = f'findReqs;siteNumber={site},limit={min(limit, 25)},keyword="{keyword}",sortBy=POSTING_DATES_DESC'
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList.secondaryLocations&finder={quote(finder, safe='=;,')}")
    r = await client.get(api, headers={"Accept": "application/json"})
    _raise_for(r, api)
    out = []
    for item in r.json().get("items", []):
        for req in item.get("requisitionList") or []:
            out.append(Listing(
                company="", title=req.get("Title", ""),
                url=f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{req.get('Id')}",
                location=req.get("PrimaryLocation", ""), posted=req.get("PostedDate", ""),
                external_id=str(req.get("Id", "")), ats="oracle_hcm",
            ))
    return out[:limit]


SEARCHERS: dict[str, Callable[[httpx.AsyncClient, Any, str, int], Awaitable[list[Listing]]]] = {
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
        found: dict[str, Listing] = {}
        fetch = max(limit, FETCH_WHEN_FILTERING) if terms else limit
        try:
            async with sem:
                for alt in alternatives(query):
                    for listing in await SEARCHERS[kind](client, search[kind], alt, fetch):
                        found.setdefault(listing.url, listing)
        except (httpx.HTTPError, SearchError, KeyError, ValueError, TypeError) as e:
            errors[company["name"]] = f"{type(e).__name__}: {str(e)[:200]}"
            return
        kept = 0
        for listing in found.values():
            listing.company = company["name"]
            where = location_matches(listing.location, terms)
            if where is False:
                continue
            if where is None:
                listing.notes.append(f"location given as {listing.location or 'nothing'!r}; check the posting")
            results.append(listing.to_dict())
            kept += 1
            if kept >= limit:
                break

    try:
        await asyncio.gather(*(one(c) for c in companies))
    finally:
        if own:
            await client.aclose()
    results.sort(key=lambda r: (r["company"], r["title"]))
    return {"results": results, "errors": errors, "browser_only": browser_only}
