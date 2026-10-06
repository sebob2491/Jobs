"""Search a company's openings through its applicant tracking system's public JSON API.

Each company in data/companies.yaml may carry a `search` block naming one of:

    workday:         https://<tenant>.wd<N>.myworkdayjobs.com/<site>
    greenhouse:      <board token>
    lever:           <company slug>
    eightfold:       {host: careers.example.com, domain: example.com}
    smartrecruiters: <company identifier>
    oracle:          {host: xxxx.fa.us2.oraclecloud.com, site: CX_1001}
    applicantstack:  <board name>
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
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
CLIENT_SIDE = {"greenhouse", "lever", "applicantstack", "paycom", "ukg", "sfclassic", "infor"}  # whole board at once; titles filtered here
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
    if not n or _BROAD.fullmatch(n) or re.search(r"\+\d+ more\b", n):  # "Greensboro, NC (+3 more)"
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
            if nowhere_near and location_matches(p.get("locationsText", ""), terms) is None:
                continue
            out.append(Listing(
                company="", title=p.get("title", ""), url=f"{base}{p.get('externalPath') or ''}",
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


async def _applicantstack(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """ApplicantStack boards (SCREEN SPE USA) list every opening on one page, each with
    its location."""
    base = f"https://{cfg}.applicantstack.com"
    url = f"{base}/x/openings"
    r = await _send(client, "GET", url)
    _raise_for(r, url)
    return [listing for listing in parse_applicantstack(r.text, base) if title_matches(listing.title, query)]


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


def icims_page_url(cfg: Any, query: str) -> str:
    return f"https://{cfg}.icims.com/jobs/search?" + urlencode({"ss": "1", "searchKeyword": query, "in_iframe": "1"})


async def icims_search(frames_html: Callable[[str], Awaitable[list[str]]], cfg: Any, query: str,
                       found: list[Listing]) -> None:
    """iCIMS portals (Daifuku America) answer plain requests with HTTP 405, so the search
    page is read in the browser. The openings are drawn inside the portal's frame, each
    with its location and posting date."""
    base = f"https://{cfg}.icims.com"
    for html in await frames_html(icims_page_url(cfg, query)):
        found.extend(parse_icims(html, base))


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
            when = row.select_one(".header.right span[title]")  # Posted Date: 9/24/2026 6:18 PM
            if when is not None:
                d = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(when["title"]))
                posted = f"{d.group(3)}-{int(d.group(1)):02d}-{int(d.group(2)):02d}" if d else ""
        out.append(Listing(company="", title=title, url=url, location=location.strip(), posted=posted,
                           external_id=m.group(1), ats="icims"))
    return out



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
    url = str(cfg["url"] if isinstance(cfg, dict) else cfg)
    origin = re.match(r"https?://[^/]+", url).group(0)
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


# ------------------------------------------------- SuccessFactors career sites' search pages (Qorvo)
SF_PAGES = 4  # pages per wording; the site draws 25 rows a page
_SF_MORE = re.compile(r"\+\s*(\d+)\s*more\W*$", re.I)  # "Greensboro, NC, US, 27409 +3 more…"
_SF_MORE_SHOWN = re.compile(r"\(\+\d+ more\)")
_SF_TOTAL = re.compile(r"of\s+([\d,]+)")  # "Results 1 – 25 of 120"


def _sf_state(terms: list[str]) -> str:
    """The state to hand the site's own location search ('Arizona' for 'AZ'), so openings
    elsewhere don't crowd the user's area off the pages read."""
    return next((name for name in US_STATES.values() if norm(name) in terms), "")


async def _successfactors(client: httpx.AsyncClient, cfg: Any, query: str, limit: int, terms: list[str]) -> list[Listing]:
    """SuccessFactors career sites (Qorvo) draw their search results as an HTML table, 25
    openings a page. Given a state, the site's own location search narrows them; it also
    finds openings whose first place is elsewhere ("Greensboro, NC +3 more")."""
    site = str(cfg["url"] if isinstance(cfg, dict) else cfg).rstrip("/")
    if "://" not in site:
        site = f"https://{site}"
    state = _sf_state(terms)
    url = f"{site}/search/"
    out: list[Listing] = []
    for _ in range(SF_PAGES):
        params: dict[str, Any] = {"q": query, "startrow": len(out)}
        if state:
            params["locationsearch"] = state
        r = await _send(client, "GET", url, params=params)
        _raise_for(r, url)
        batch, total = parse_successfactors(r.text, site)
        for listing in batch:
            if state and _SF_MORE_SHOWN.search(listing.location) and location_matches(listing.location, terms) is not True:
                # one of its other places is in the state, or the site wouldn't have listed it
                listing.location = f"{listing.location}; {state}"
        out.extend(batch)
        if not batch or len(out) >= (total if total is not None else limit) or len(out) >= limit:
            break
    return out


def _sf_date(text: str) -> str:
    for fmt in ("%b %d, %Y", "%d %b %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def parse_successfactors(page: str, site: str) -> tuple[list[Listing], int | None]:
    """The openings on one page of a SuccessFactors search, and how many the search found."""
    soup = BeautifulSoup(page, "html.parser")
    out: list[Listing] = []
    seen: set[str] = set()
    for row in soup.select("tr.data-row"):
        link = row.select_one("a.jobTitle-link[href]")
        if link is None:
            continue
        url = urljoin(site + "/", str(link["href"]))
        title = link.get_text(" ", strip=True)
        if not title or url in seen:
            continue
        seen.add(url)
        cell = row.select_one(".colLocation") or row.select_one(".jobLocation")
        where = re.sub(r"\s+", " ", cell.get_text(" ", strip=True)) if cell is not None else ""
        more = _SF_MORE.search(where)
        if more:  # "Greensboro, NC, US, 27409 (+3 more)"
            where = f"{where[:more.start()].strip()} (+{more.group(1)} more)"
        when = row.select_one(".colDate .jobDate") or row.select_one(".jobDate")
        job_id = re.search(r"/(\d+)/?(?:\?|$)", str(link["href"]))
        out.append(Listing(company="", title=title, url=url, location=where,
                           posted=_sf_date(when.get_text(" ", strip=True)) if when is not None else "",
                           external_id=job_id.group(1) if job_id else "", ats="successfactors"))
    label = soup.select_one(".paginationLabel")
    total = _SF_TOTAL.search(label.get_text(" ", strip=True)) if label is not None else None
    return out, int(total.group(1).replace(",", "")) if total else None


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
        if req is None:
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

        def value(key: str) -> str:
            v = fields.get(key)
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
    "applicantstack": _applicantstack,
    "successfactors": _successfactors,
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
