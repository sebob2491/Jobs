"""Fetch a job posting and turn it into a normalized record.

Strategy, most reliable first:
  1. The ATS's public JSON endpoint (Workday, Greenhouse, Lever).
  2. schema.org JobPosting JSON-LD, which most career sites embed for Google Jobs.
  3. Page title + main text as a last resort.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .ats import (detect_ats, greenhouse_parts, lever_parts, linkedin_job_id, oracle_parts, smartrecruiters_parts,
                  workday_parts)
from .autofill import US_STATES

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
MAX_DESCRIPTION = 20000


@dataclass
class Posting:
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    apply_url: str = ""
    ats: str = "company_site"
    external_id: str = ""
    salary: str = ""
    employment_type: str = ""
    posted_at: str = ""
    source: str = ""
    parse_method: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_useful(self) -> bool:
        return bool(self.title and len(self.description) > 200)


class FetchError(Exception):
    pass


def html_to_text(raw: str) -> str:
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "ul", "ol", "tr", "section"]):
        block.insert_after("\n")
    text = soup.get_text()
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:MAX_DESCRIPTION]


def _first(v: Any) -> Any:
    return v[0] if isinstance(v, list) and v else v


def _location_from_ld(loc: Any) -> str:
    out = []
    for item in loc if isinstance(loc, list) else [loc]:
        if not isinstance(item, dict):
            if item:
                out.append(str(item))
            continue
        addr = item.get("address", item)
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion"), _first(addr.get("addressCountry"))]
            parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
            out.append(", ".join(str(p) for p in parts if p))
        elif addr:
            out.append(str(addr))
    return "; ".join(o for o in out if o)


def _salary_from_ld(sal: Any) -> str:
    if not isinstance(sal, dict):
        return str(sal) if sal else ""
    cur = sal.get("currency", "")
    val = sal.get("value", {})
    if isinstance(val, dict):
        lo, hi, unit = val.get("minValue"), val.get("maxValue"), val.get("unitText", "")
        amount = f"{lo}-{hi}" if lo and hi else str(val.get("value") or lo or hi or "")
        return " ".join(x for x in [cur, amount, unit and f"per {unit.lower()}"] if x)
    return f"{cur} {val}".strip()


def find_jsonld_jobposting(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            # Some sites emit trailing commas or raw newlines inside strings.
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.strip()), strict=False)
            except json.JSONDecodeError:
                continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = node.get("@type")
                types = t if isinstance(t, list) else [t]
                if "JobPosting" in types:
                    return node
                if "@graph" in node:
                    stack.append(node["@graph"])
    return None


def posting_from_jsonld(ld: dict[str, Any], url: str) -> Posting:
    org = ld.get("hiringOrganization") or {}
    ident = ld.get("identifier") or {}
    p = Posting(
        url=url,
        title=html.unescape(str(ld.get("title") or "")).strip(),
        company=(org.get("name") if isinstance(org, dict) else str(org)) or "",
        location=_location_from_ld(ld.get("jobLocation")),
        description=html_to_text(str(ld.get("description") or "")),
        external_id=str(ident.get("value", "")) if isinstance(ident, dict) else str(ident or ""),
        salary=_salary_from_ld(ld.get("baseSalary")),
        employment_type=", ".join(ld["employmentType"]) if isinstance(ld.get("employmentType"), list) else str(ld.get("employmentType") or ""),
        posted_at=str(ld.get("datePosted") or ""),
        parse_method="json-ld",
    )
    if ld.get("jobLocationType") == "TELECOMMUTE" and "remote" not in p.location.lower():
        p.location = (p.location + "; Remote").strip("; ")
    return p


_CITY_STATE = re.compile(r"^([A-Za-z][A-Za-z .'-]*?),?\s+([A-Z]{2})$")  # "Phoenix AZ"


def successfactors_place(soup: BeautifulSoup) -> str:
    """Where a SuccessFactors posting is, from the unlabelled lines under its title (Edwards:
    "Service", "Phoenix AZ", "United States", "On-Site"): 'Phoenix, AZ'."""
    for token in soup.select(".joblayouttoken")[:12]:
        if token.select_one('[itemprop="title"]') is not None:
            continue
        m = _CITY_STATE.match(" ".join(token.get_text(" ", strip=True).split()))
        if m and m.group(2) in US_STATES:
            return f"{m.group(1)}, {m.group(2)}"
    return ""


# A city and a US state in running text: "based at our headquarters in Tempe, AZ", "Peoria, Arizona"
_TEXT_PLACE = re.compile(r"\b([A-Z][a-z]+(?:[ .'-]+[A-Z][a-z]+){0,2}),\s*([A-Z]{2}|[A-Z][a-z]+(?: [A-Z][a-z]+)?)\b")
_STATE_CODES = {name.lower(): code for code, name in US_STATES.items()}


def place_in_text(text: str) -> str:
    """The first city and US state a posting's text names: 'Tempe, AZ'. SuccessFactors' older
    career sites (Amkor) give no place but the one the description mentions."""
    for m in _TEXT_PLACE.finditer(text or ""):
        state = m.group(2)
        code = state if state in US_STATES else _STATE_CODES.get(state.lower())
        if code:
            return f"{m.group(1)}, {code}"
    return ""


# SuccessFactors' older career sites title a posting "Career Opportunities: <title> (<req id>)"
_SF_CLASSIC_TITLE = re.compile(r"^Career Opportunities:\s*(.+?)\s*\(\d+\)$")


# Apply links that only work when pressed on the posting page itself: SuccessFactors career
# sites send a visit straight to one to their home page. The posting is opened instead.
_PAGE_BOUND_APPLY = re.compile(r"/talentcommunity/apply/", re.I)


def _find_apply_link(soup: BeautifulSoup, base_url: str) -> str:
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ").split()).lower()
        label = (a.get("aria-label") or "").lower()
        if re.search(r"\bapply\b", text + " " + label) and not a["href"].startswith(("#", "javascript:", "mailto:")):
            return "" if _PAGE_BOUND_APPLY.search(a["href"]) else urljoin(base_url, a["href"])
    return ""


def parse_html(raw_html: str, url: str) -> Posting:
    soup = BeautifulSoup(raw_html, "html.parser")
    ld = find_jsonld_jobposting(soup)
    if ld:
        p = posting_from_jsonld(ld, url)
    else:
        title = ""
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"]
        elif soup.title:
            title = soup.title.get_text()
        main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
        p = Posting(url=url, title=" ".join(title.split()), description=html_to_text(str(main)), parse_method="page-text")
        m = _SF_CLASSIC_TITLE.match(p.title)
        if m:
            p.title = m.group(1)
        site = soup.find("meta", property="og:site_name")
        if site and site.get("content"):
            p.company = site["content"]
        p.warnings.append("No structured JobPosting data; title/company may need correcting.")
    p.apply_url = p.apply_url or _find_apply_link(soup, url)
    p.location = p.location or successfactors_place(soup)
    if not p.location and "career_job_req_id=" in url:  # SuccessFactors' older sites: only the text says
        p.location = place_in_text(p.description)
    return p


async def _get(client: httpx.AsyncClient, url: str, **kw: Any) -> httpx.Response:
    try:
        r = await client.get(url, **kw)
    except httpx.HTTPError as e:
        raise FetchError(f"{type(e).__name__}: {e}") from e
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code} from {url}")
    return r


async def _fetch_workday(client: httpx.AsyncClient, url: str) -> Posting | None:
    parts = workday_parts(url)
    if not parts or not parts["job_path"]:
        return None
    api = f"https://{parts['host']}/wday/cxs/{parts['tenant']}/{parts['site']}{parts['job_path']}"
    r = await _get(client, api, headers={"Accept": "application/json"})
    info = r.json().get("jobPostingInfo") or {}
    if not info:
        return None
    org = (r.json().get("hiringOrganization") or {}).get("name", "")
    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
    return Posting(
        url=url,
        title=info.get("title", ""),
        company=org or parts["tenant"],
        location="; ".join(l for l in locs if l),
        description=html_to_text(info.get("jobDescription", "")),
        apply_url=info.get("externalUrl") or url,
        external_id=info.get("jobReqId", ""),
        employment_type=info.get("timeType", ""),
        posted_at=info.get("startDate", ""),
        parse_method="workday-api",
    )


async def _fetch_greenhouse(client: httpx.AsyncClient, url: str) -> Posting | None:
    parts = greenhouse_parts(url)
    if not parts:
        return None
    api = f"https://boards-api.greenhouse.io/v1/boards/{parts['board']}/jobs/{parts['job_id']}?questions=true"
    data = (await _get(client, api)).json()
    # The Greenhouse-hosted page always carries the form; employer pages that embed it
    # in an iframe often don't load it until cookies are accepted.
    hosted = f"https://job-boards.greenhouse.io/{parts['board']}/jobs/{parts['job_id']}"
    p = Posting(
        url=url,
        title=data.get("title", ""),
        company=data.get("company_name") or parts["board"],
        location=(data.get("location") or {}).get("name", ""),
        description=html_to_text(html.unescape(data.get("content", ""))),
        apply_url=hosted,
        external_id=str(data.get("requisition_id") or data.get("id") or ""),
        posted_at=data.get("updated_at", ""),
        parse_method="greenhouse-api",
    )
    questions = [q.get("label") for q in data.get("questions") or [] if q.get("label")]
    if questions:
        p.description += "\n\nApplication questions:\n" + "\n".join(f"- {q}" for q in questions)
    return p


async def _fetch_lever(client: httpx.AsyncClient, url: str) -> Posting | None:
    parts = lever_parts(url)
    if not parts:
        return None
    api = f"https://api.lever.co/v0/postings/{parts['company']}/{parts['posting_id']}"
    data = (await _get(client, api)).json()
    cats = data.get("categories") or {}
    body = data.get("descriptionPlain") or html_to_text(data.get("description", ""))
    for section in data.get("lists") or []:
        body += f"\n\n{section.get('text', '')}\n" + html_to_text(section.get("content", ""))
    return Posting(
        url=url,
        title=data.get("text", ""),
        company=parts["company"],
        location=cats.get("location", ""),
        description=body.strip()[:MAX_DESCRIPTION],
        apply_url=data.get("applyUrl") or url,
        external_id=parts["posting_id"],
        employment_type=cats.get("commitment", ""),
        parse_method="lever-api",
    )


async def _fetch_smartrecruiters(client: httpx.AsyncClient, url: str) -> Posting | None:
    parts = smartrecruiters_parts(url)
    if not parts:
        return None
    api = f"https://api.smartrecruiters.com/v1/companies/{parts['company']}/postings/{parts['posting_id']}"
    data = (await _get(client, api)).json()
    sections = ((data.get("jobAd") or {}).get("sections") or {})
    body = []
    for key in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"):
        sec = sections.get(key) or {}
        if sec.get("text"):
            body.append(f"{sec.get('title') or ''}\n{html_to_text(sec['text'])}".strip())
    loc = data.get("location") or {}
    return Posting(
        url=url,
        title=data.get("name", ""),
        company=(data.get("company") or {}).get("name") or parts["company"],
        location=", ".join(x for x in [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()] if x),
        description="\n\n".join(body)[:MAX_DESCRIPTION],
        apply_url=data.get("applyUrl") or url,
        external_id=data.get("refNumber") or parts["posting_id"],
        employment_type=(data.get("typeOfEmployment") or {}).get("label", ""),
        posted_at=(data.get("releasedDate") or "")[:10],
        parse_method="smartrecruiters-api",
    )


async def _fetch_oracle(client: httpx.AsyncClient, url: str) -> Posting | None:
    parts = oracle_parts(url)
    if not parts:
        return None
    api = (f"https://{parts['host']}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
           f"?expand=all&onlyData=true&finder=ById;Id=%22{parts['job_id']}%22,siteNumber={parts['site']}")
    items = (await _get(client, api, headers={"Accept": "application/json"})).json().get("items") or []
    if not items:
        return None
    d = items[0]
    body = "\n\n".join(html_to_text(d.get(k) or "") for k in
                       ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr") if d.get(k))
    return Posting(
        url=url,
        title=d.get("Title") or "",
        location=d.get("PrimaryLocation") or "",
        description=body[:MAX_DESCRIPTION],
        apply_url=url,
        external_id=str(d.get("Id") or parts["job_id"]),
        employment_type=d.get("WorkerType") or d.get("JobSchedule") or "",
        posted_at=d.get("ExternalPostedStartDate") or "",
        parse_method="oracle-api",
    )


async def fetch_posting(url: str, timeout: float = 20.0, client: httpx.AsyncClient | None = None) -> Posting:
    """Fetch and parse a posting over plain HTTP. Raises FetchError when the
    site refuses (LinkedIn and Indeed usually do); callers can then read the
    page through the browser instead."""
    url = url.strip()
    if client is not None:
        return await _fetch_with(client, url)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout) as own:
        return await _fetch_with(own, url)


async def _fetch_with(client: httpx.AsyncClient, url: str) -> Posting:
    ats = detect_ats(url)
    posting: Posting | None = None
    api_error = ""
    fetcher = {"workday": _fetch_workday, "greenhouse": _fetch_greenhouse, "lever": _fetch_lever,
               "smartrecruiters": _fetch_smartrecruiters, "oracle_hcm": _fetch_oracle}.get(ats)
    if fetcher:
        try:
            posting = await fetcher(client, url)
        except (FetchError, ValueError, KeyError, TypeError, AttributeError) as e:
            api_error = f"{type(e).__name__}: {e}"
    if posting is None:
        r = await _get(client, url, headers={"Accept": "text/html,application/xhtml+xml"})
        final_url = str(r.url)
        if ats == "linkedin" and ("authwall" in final_url or "/login" in final_url):
            raise FetchError("LinkedIn requires sign-in for this posting")
        posting = parse_html(r.text, url)
        if api_error:
            posting.warnings.append(f"ATS API lookup failed ({api_error}); parsed the page instead.")
    return finalize(posting)


def finalize(posting: Posting) -> Posting:
    posting.ats = detect_ats(posting.url)
    if posting.apply_url and detect_ats(posting.apply_url) != "company_site":
        # e.g. an asml.com posting whose Apply button goes to an ATS
        posting.ats = detect_ats(posting.apply_url)
    if posting.ats == "linkedin" and not posting.external_id:
        posting.external_id = linkedin_job_id(posting.url) or ""
    posting.source = posting.source or detect_ats(posting.url)
    posting.title = posting.title.strip()
    # Many sites title pages "Job Title | Company" or "Job Title - Careers at X".
    if posting.parse_method == "page-text" and " | " in posting.title:
        title, _, rest = posting.title.partition(" | ")
        posting.title = title.strip()
        posting.company = posting.company or rest.strip()
    if not posting.is_useful:
        posting.warnings.append("Posting text looks incomplete; confirm details with the user or read it in the browser.")
    return posting
