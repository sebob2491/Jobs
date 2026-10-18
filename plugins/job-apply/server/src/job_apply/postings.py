"""Fetch a job posting and turn it into a normalized record.

Strategy, most reliable first:
  1. The ATS's public JSON endpoint (Workday, Greenhouse, Lever, SmartRecruiters, Oracle, Cornerstone).
  2. schema.org JobPosting JSON-LD, which most career sites embed for Google Jobs.
  3. Page title + main text as a last resort.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .ats import (csod_parts, detect_ats, greenhouse_parts, lever_parts, linkedin_job_id, oracle_parts,
                  smartrecruiters_parts, workday_parts)
from .autofill import US_STATES
from .pay import fields as pay_fields
from .pay import pay_from_ld, pay_from_posting
from .workplace import field_said as workplace_field_said
from .workplace import Fields as WorkplaceFields
from .workplace import fields as workplace_fields
from .workplace import from_field as workplace_field
from .workplace import in_text as workplace_in_text
from .workplace import location_said as workplace_location_said

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
MAX_DESCRIPTION = 20000
# Where a posting's own application questions start in its text (Greenhouse): they're the
# form's, not the job's requirements, so the ranking stops reading there.
QUESTIONS_HEADING = "Application questions:"


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
    # its pay (pay.py): the figures, the period they're for (hour, week, month, year) and where they came from
    # (structured, indeed, text); `salary` stays the words as given. finalize() fills them in
    pay_min: float | None = None
    pay_max: float | None = None
    pay_period: str = ""
    pay_source: str = ""
    pay_sure: bool = False  # (structured, or right after a pay label: only a sure pay fails a "Pay at least")
    # where it's done (workplace.py: remote, hybrid or onsite; "" unknown) and where that came from (structured: the
    # system's own field; location: its location's words; text: its sentences, never sure). finalize() reads the
    # location and the text for one no field said
    workplace: str = ""
    workplace_source: str = ""
    # the whole text, before MAX_DESCRIPTION's cut, and the page's description: read by finalize() for the pay (often
    # the last thing a long posting says), then let go. Never saved
    full_text: str = field(default="", repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        del out["full_text"]
        return out

    @property
    def is_useful(self) -> bool:
        return bool(self.title and len(self.description) > 200)


class FetchError(Exception):
    pass


def html_to_text(raw: str, limit: int | None = MAX_DESCRIPTION) -> str:
    """A posting's HTML as its text, cut at `limit` characters (None: all of it, for its pay)."""
    if not raw:
        return ""
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    # a line of its own, before and after: "<strong>Preferred</strong><p>..." keeps its heading
    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr", "td", "th", "dt", "dd",
                                "section", "article", "header", "footer", "table", "blockquote"]):
        block.insert_before("\n")
        block.insert_after("\n")
    text = soup.get_text()
    text = html.unescape(text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"(^|\n)- *\n+(?!- )", r"\1- ", text)  # "<li>\n  Bachelor's..." keeps its bullet
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


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


_MICRODATA_POSTING = re.compile(r"schema\.org/JobPosting$", re.I)


def _is_posting_type(itemtype: str | None) -> bool:
    return bool(itemtype and _MICRODATA_POSTING.search(itemtype))


def _props(scope: Any, name: str) -> list[Any]:
    """A scope's own properties by name, never those of a scope inside it (an employer's own description)."""
    found = scope.find_all(attrs={"itemprop": lambda v: bool(v) and name in v.split()})
    if not scope.has_attr("itemscope"):
        return found
    return [tag for tag in found if tag.find_parent(attrs={"itemscope": True}) is scope]


def _microdata_value(tag: Any) -> str:
    """A microdata property's value: a meta tag's content, a nested scope's name (none without one: its
    text is an employer's "Visit our website"), or the text."""
    if tag is None:
        return ""
    if tag.has_attr("content"):
        return " ".join(str(tag["content"]).split())
    if tag.has_attr("itemscope"):
        named = _props(tag, "name")
        return _microdata_value(named[0]) if named else ""
    return " ".join(tag.get_text(" ", strip=True).split())


def microdata_jobposting(soup: BeautifulSoup) -> dict[str, Any] | None:
    """A posting a page gives as schema.org microdata rather than JSON-LD, shaped like JSON-LD's
    (SuccessFactors' career sites, EY's, APS', SRP's: a JobPosting scope with its place, date and
    employer as meta tags, then the title and the text). Its text is the whole scope's: a posting's
    shift, travel and qualifications sit beside its description (Rogers'). A place naming only its
    country isn't one (the posting's lines may name the city). None without a scope or a title."""
    scope = soup.find(itemtype=_is_posting_type)
    if scope is None:
        return None
    places: list[Any] = []
    for place in _props(scope, "jobLocation"):
        addr = (_props(place, "address") or [place])[0]
        parts = {k: _microdata_value((_props(addr, k) or [None])[0])
                 for k in ("addressLocality", "addressRegion", "addressCountry")}
        if parts["addressLocality"] or parts["addressRegion"]:
            places.append({"address": parts})
        elif not any(parts.values()) and _microdata_value(place):
            places.append(_microdata_value(place))
    first = {name: (_props(scope, name) or [None])[0] for name in ("title", "hiringOrganization", "employmentType")}
    return {"title": _microdata_value(first["title"]),
            "hiringOrganization": {"name": _microdata_value(first["hiringOrganization"])},
            "jobLocation": places,
            "description": str(scope),
            "employmentType": _microdata_value(first["employmentType"])}


def _unescaped(raw: str) -> str:
    """HTML that was escaped once more ("&lt;p&gt;Requirements..."), as some sites' JSON-LD is."""
    return html.unescape(raw) if "&lt;" in raw and "<" not in raw else raw


# UKG Pro (Nikon) builds its posting page with script: the posting is in the page's
# `new US.Opportunity.CandidateOpportunityDetail({...})` call.
_UKG_DETAIL = "CandidateOpportunityDetail("


def ukg_posting(raw_html: str, url: str) -> Posting | None:
    at = raw_html.find(_UKG_DETAIL)
    if at < 0:
        return None
    try:
        d, _ = json.JSONDecoder().raw_decode(raw_html[at + len(_UKG_DETAIL):])
    except ValueError:
        return None
    if not isinstance(d, dict) or not d.get("Description"):
        return None
    places = []
    for loc in d.get("Locations") or []:
        addr = (loc or {}).get("Address") or {}
        city, state = addr.get("City"), (addr.get("State") or {}).get("Code")
        place = f"{city}, {state}" if city and state else (loc or {}).get("LocalizedDescription") or city or state or ""
        if place and place not in places:
            places.append(place)
    text = html_to_text(str(d["Description"]), limit=None)
    return Posting(url=url, title=str(d.get("Title") or "").strip(), location="; ".join(places),
                   description=text[:MAX_DESCRIPTION], external_id=str(d.get("RequisitionNumber") or ""),
                   posted_at=str(d.get("PostedDate") or ""), parse_method="ukg-page", full_text=text)


_US_COUNTRIES = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}


def _in_us(loc: Any) -> bool:
    """Whether a JSON-LD jobLocation is in the United States: its country says so, or its region is a US state."""
    states = {code.lower() for code in US_STATES} | {name.lower() for name in US_STATES.values()}
    for item in loc if isinstance(loc, list) else [loc]:
        addr = item.get("address", item) if isinstance(item, dict) else None
        if not isinstance(addr, dict):
            continue
        country = _first(addr.get("addressCountry"))
        country = country.get("name") if isinstance(country, dict) else country
        if str(country or "").strip().lower() in _US_COUNTRIES:
            return True
        if not country and str(addr.get("addressRegion") or "").strip().lower() in states:
            return True
    return False


def posting_from_jsonld(ld: dict[str, Any], url: str) -> Posting:
    org = ld.get("hiringOrganization") or {}
    ident = ld.get("identifier") or {}
    text = html_to_text(_unescaped(str(ld.get("description") or "")), limit=None)
    p = Posting(
        url=url,
        title=html.unescape(str(ld.get("title") or "")).strip(),
        company=(org.get("name") if isinstance(org, dict) else str(org)) or "",
        location=_location_from_ld(ld.get("jobLocation")),
        description=text[:MAX_DESCRIPTION],
        full_text=text,
        external_id=str(ident.get("value", "")) if isinstance(ident, dict) else str(ident or ""),
        salary=_salary_from_ld(ld.get("baseSalary")),
        employment_type=", ".join(ld["employmentType"]) if isinstance(ld.get("employmentType"), list) else str(ld.get("employmentType") or ""),
        posted_at=str(ld.get("datePosted") or ""),
        parse_method="json-ld",
        # (its figures, not only the words _salary_from_ld prints; dollars when it names no currency only in the US)
        **pay_fields(pay_from_ld(ld.get("baseSalary"), in_us=_in_us(ld.get("jobLocation")))),
    )
    kinds = ld.get("jobLocationType")
    if "TELECOMMUTE" in (kinds if isinstance(kinds, list) else [kinds]):  # (schema.org's one word for a remote job)
        p.workplace, p.workplace_source = "remote", "structured"
        if "remote" not in p.location.lower():
            p.location = (p.location + "; Remote").strip("; ")
    return p


_CITY_STATE = re.compile(r"^([A-Za-z][A-Za-z .'-]*?),?\s+([A-Z]{2})$")  # "Phoenix AZ"
_SF_WORKPLACE = re.compile(r"(?i)^(?:on-?\s?site|hybrid|remote|fully remote|in[ -]office)$")


def said_workplace(value: Any) -> WorkplaceFields:
    """A job system's own field for where the job is done, as a Posting's or a Listing's workplace fields (source
    "structured", or "option" for one said as an option: "Remote Eligible"): {} when it has none or says none
    (workplace.field_said)."""
    return workplace_fields(*workplace_field_said(value))


def smartrecruiters_workplace(location: Any) -> str:
    """Where a SmartRecruiters job is done, from its location's own flags (remote, hybrid: only a true one says)."""
    loc = location if isinstance(location, dict) else {}
    return "hybrid" if loc.get("hybrid") is True else "remote" if loc.get("remote") is True else ""


def successfactors_workplace(soup: BeautifulSoup) -> str:
    """Where a SuccessFactors posting's job is done, from the unlabelled lines under its title (Edwards: "Service",
    "Phoenix AZ", "United States", "On-Site"): a line that is only that word."""
    for token in soup.select(".joblayouttoken")[:12]:
        words = " ".join(token.get_text(" ", strip=True).split())
        if token.select_one('[itemprop="title"]') is None and _SF_WORKPLACE.match(words):
            return workplace_field(words)
    return ""


_ICIMS_WORKPLACE = re.compile(r"(?i)^(?:location\s+type|workplace(?:\s+type)?|work\s+(?:location\s+)?(?:type|"
                              r"arrangement|model)|remote\s+type)$")
_ICIMS_REMOTE_OK = re.compile(r"(?i)^remote(?:\s+location)?\s+eligible$")


def icims_workplace(node: Any) -> tuple[str, bool]:
    """Where an iCIMS job is done, and whether surely, from its header details (a results row's, or its posting
    page's): "Location Type: Onsite" (workplace.field_said), else "Remote Location Eligible: Yes", remote as an option
    (never sure: eligible isn't done so); ("", False) when they don't say ("Remote Location Eligible: No" alone
    doesn't: it may be hybrid)."""
    eligible = ""
    for tag in node.select(".iCIMS_JobHeaderTag"):
        name, value = tag.select_one("dt"), tag.select_one("dd")
        if name is None or value is None:
            continue
        words = [w.strip() for w in name.get_text(" ", strip=True).split(":") if w.strip()]
        said = value.get_text(" ", strip=True)
        if words and all(_ICIMS_WORKPLACE.match(w) for w in words) and workplace_field(said):
            return workplace_field_said(said)
        if words and all(_ICIMS_REMOTE_OK.match(w) for w in words):
            eligible = said.strip().lower()
    return ("remote", False) if re.match(r"(?:yes|y|true)\b", eligible) else ("", False)


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


# A menu's way to the job list, not this posting's application: Phoenix Children's "Browse &
# Apply" (its whole job list), "Search and apply", "Find jobs & apply", "View all jobs". Read
# from the link's start: "Apply for Executive Search Consultant" is a posting's own
_LIST_LINK = re.compile(r"^(?:browse|search|find)\b|\b(?:view all|all (?:jobs|positions|openings)|"
                        r"other (?:jobs|positions))\b", re.I)


def _find_apply_link(soup: BeautifulSoup, base_url: str) -> str:
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ").split()).lower()
        label = str(a.get("aria-label") or "").lower()
        href = str(a["href"])
        if (re.search(r"\bapply\b", text + " " + label) and not href.startswith(("#", "javascript:", "mailto:"))
                and not _LIST_LINK.search(text) and not _LIST_LINK.search(label)):
            return "" if _PAGE_BOUND_APPLY.search(href) else urljoin(base_url, href)
    return ""


# A posting's date written out: "Oct 2, 2026", "October 2, 2026" (a search's own "July  9, 2026" too: a space in a
# format matches any run of them), "2 Oct 2026", "Fri Oct 02 00:00:00 2026" (its zone dropped first), and the
# looser "7/9/2026" and "2026-7-9" the numbered shapes don't take
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%m/%d/%Y", "%Y-%m-%d", "%a %b %d %H:%M:%S %Y")
_MS_EPOCH = 10**11  # (seconds past it are the year 5138: a number this big is milliseconds)


def _day(year: int, month: int, day: int) -> str:
    try:
        return date(year, month, day).isoformat()
    except ValueError:  # (no such day: "13/45/2026")
        return ""


def posted_date(value: Any) -> str:
    """A posting's date as YYYY-MM-DD, from any of the shapes the job systems give it, tried in this order: an ISO
    date or timestamp ("2026-10-01T08:00:00Z"); an epoch, as a number, in seconds or milliseconds (Eightfold's
    t_create, Lever's createdAt); month/day/year, the year in two digits or four ("7/27/26", Paycom's "10/9/2026
    8:00 AM"); a date written out ("Oct 2, 2026", EY's microdata "Fri Oct 02 00:00:00 UTC 2026"). "" for anything
    else (a place in a date's column, "soon", nothing), or for a day that doesn't exist."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        seconds = value // 1000 if value >= _MS_EPOCH else value
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).date().isoformat() if seconds > 0 else ""
        except (OverflowError, OSError, ValueError):
            return ""
    text = str(value).strip()
    if iso := re.match(r"(\d{4})-(\d{2})-(\d{2})(?!\d)", text):
        return _day(int(iso[1]), int(iso[2]), int(iso[3]))
    if us := re.match(r"(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?!\d)", text):
        year = int(us[3])
        return _day(year + 2000 if year < 100 else year, int(us[1]), int(us[2]))
    text = re.sub(r"(\d{2}:\d{2}:\d{2}) [A-Za-z]{2,5} (\d{4})$", r"\1 \2", text)  # its zone (UTC, EDT): the day is enough
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def microdata_date(soup: BeautifulSoup) -> str:
    """The date a page gives as microdata rather than JSON-LD (SuccessFactors career sites,
    EY's: itemprop="datePosted" content="Fri Oct 02 00:00:00 UTC 2026"), as YYYY-MM-DD."""
    tag = soup.select_one("[itemprop=datePosted]")
    return posted_date(str((tag.get("content") or tag.get_text()) if tag else ""))


def _description_meta(soup: BeautifulSoup) -> str:
    """The longest description a page's meta tags give (og:description, description, twitter:description)."""
    found = [" ".join(html.unescape(str(tag.get("content") or "")).split()) for tag in soup.find_all("meta")
             if str(tag.get("property") or tag.get("name") or "").lower() in
             ("og:description", "description", "twitter:description")]
    return max(found, key=len, default="")


def parse_html(raw_html: str, url: str) -> Posting:
    soup = BeautifulSoup(raw_html, "html.parser")
    ld = find_jsonld_jobposting(soup)
    micro = None if ld else microdata_jobposting(soup)
    ukg = None if ld or (micro and micro["title"]) else ukg_posting(raw_html, url)
    if ld:
        p = posting_from_jsonld(ld, url)
    elif micro and micro["title"]:
        p = posting_from_jsonld(micro, url)
        p.parse_method = "microdata"
        if len(p.description) <= 200:  # (Posting.is_useful's) a scope around little but its meta tags: the page's
            main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup  # type: ignore[call-overload]  # bs4 stubs want a name too
            p.full_text = html_to_text(str(main), limit=None)
            p.description = p.full_text[:MAX_DESCRIPTION]
    elif ukg:
        p = ukg
    else:
        title = ""
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = str(og["content"])
        elif soup.title:
            title = soup.title.get_text()
        main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup  # type: ignore[call-overload]  # bs4 stubs want a name too
        text = html_to_text(str(main), limit=None)
        p = Posting(url=url, title=" ".join(title.split()), description=text[:MAX_DESCRIPTION],
                    parse_method="page-text", full_text=text)
        m = _SF_CLASSIC_TITLE.match(p.title)
        if m:
            p.title = m.group(1)
        site = soup.find("meta", property="og:site_name")
        if site and site.get("content"):
            p.company = str(site["content"])
        p.warnings.append("No structured JobPosting data; title/company may need correcting.")
    p.apply_url = p.apply_url or _find_apply_link(soup, url)
    p.posted_at = p.posted_at or microdata_date(soup)
    # the page's description too, for the pay: a Workday page read in the browser holds its posting there (its text
    # is drawn by script: Onto's "Base Salary Range", Intel's "Annual Salary Range")
    said = _description_meta(soup)
    if said and said not in (p.full_text or p.description):
        p.full_text = f"{p.full_text or p.description}\n\n{said}"
    p.location = p.location or (_location_from_ld(micro["jobLocation"]) if micro else "") or successfactors_place(soup)
    if not p.location and "career_job_req_id=" in url:  # SuccessFactors' older sites: only the text says
        p.location = place_in_text(p.description)
    if not p.workplace:  # (SuccessFactors' "On-Site" line under the title, iCIMS's "Location Type" detail)
        line = successfactors_workplace(soup)
        for k, v in workplace_fields(*((line, True) if line else icims_workplace(soup))).items():
            setattr(p, k, v)
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
    text = html_to_text(info.get("jobDescription", ""), limit=None)
    return Posting(
        url=url,
        title=info.get("title", ""),
        company=org or parts["tenant"],
        location="; ".join(loc for loc in locs if loc),
        description=text[:MAX_DESCRIPTION],
        full_text=text,
        apply_url=info.get("externalUrl") or url,
        external_id=info.get("jobReqId", ""),
        employment_type=info.get("timeType", ""),
        posted_at=info.get("startDate", ""),
        parse_method="workday-api",
        **said_workplace(info.get("remoteType")),  # ("Hybrid", "Remote", "On Site": a tenant's field, where it has it)
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
    text = html_to_text(html.unescape(data.get("content", "")), limit=None)
    p = Posting(
        url=url,
        title=data.get("title", ""),
        company=data.get("company_name") or parts["board"],
        location=(data.get("location") or {}).get("name", ""),
        description=text[:MAX_DESCRIPTION],
        full_text=text,
        apply_url=hosted,
        external_id=str(data.get("requisition_id") or data.get("id") or ""),
        posted_at=data.get("updated_at", ""),
        parse_method="greenhouse-api",
    )
    questions = [q.get("label") for q in data.get("questions") or [] if q.get("label")]
    if questions:
        p.description += f"\n\n{QUESTIONS_HEADING}\n" + "\n".join(f"- {q}" for q in questions)
    return p


async def _fetch_lever(client: httpx.AsyncClient, url: str) -> Posting | None:
    parts = lever_parts(url)
    if not parts:
        return None
    api = f"https://api.lever.co/v0/postings/{parts['company']}/{parts['posting_id']}"
    data = (await _get(client, api)).json()
    cats = data.get("categories") or {}
    body = data.get("descriptionPlain") or html_to_text(data.get("description", ""), limit=None)
    for section in data.get("lists") or []:
        body += f"\n\n{section.get('text', '')}\n" + html_to_text(section.get("content", ""), limit=None)
    # the closing section, where citizenship, clearance and travel terms often are
    body += "\n\n" + (data.get("additionalPlain") or html_to_text(data.get("additional") or "", limit=None))
    return Posting(
        url=url,
        title=data.get("text", ""),
        company=parts["company"],
        location=cats.get("location", ""),
        description=body.strip()[:MAX_DESCRIPTION],
        full_text=body.strip(),
        apply_url=data.get("applyUrl") or url,
        external_id=parts["posting_id"],
        employment_type=cats.get("commitment", ""),
        parse_method="lever-api",
        **said_workplace(data.get("workplaceType")),  # (remote, hybrid, on-site; "unspecified" says none)
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
            body.append(f"{sec.get('title') or ''}\n{html_to_text(sec['text'], limit=None)}".strip())
    loc = data.get("location") or {}
    return Posting(
        url=url,
        title=data.get("name", ""),
        company=(data.get("company") or {}).get("name") or parts["company"],
        location=", ".join(x for x in [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()] if x),
        description="\n\n".join(body)[:MAX_DESCRIPTION],
        full_text="\n\n".join(body),
        apply_url=data.get("applyUrl") or url,
        external_id=data.get("refNumber") or parts["posting_id"],
        employment_type=(data.get("typeOfEmployment") or {}).get("label", ""),
        posted_at=(data.get("releasedDate") or "")[:10],
        parse_method="smartrecruiters-api",
        **said_workplace(smartrecruiters_workplace(loc)),
    )


_ORACLE_HOST = re.compile(r"\b[a-z0-9-]+\.fa(?:\.[a-z0-9-]+)?\.oraclecloud\.com\b")


async def _fetch_oracle(client: httpx.AsyncClient, url: str) -> Posting | None:
    parts = oracle_parts(url)
    if not parts:
        return None
    host = parts["host"]
    path = ("/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f"?expand=all&onlyData=true&finder=ById;Id=%22{parts['job_id']}%22,siteNumber={parts['site']}")
    if not host.endswith(".oraclecloud.com"):
        # a company's own address for its Oracle site (careers.ti.com) answers only the page,
        # which names the Oracle host its API is on
        page = (await _get(client, url, headers={"Accept": "text/html"})).text
        m = _ORACLE_HOST.search(page)
        if not m:
            return None
        host = m.group(0)
    items = (await _get(client, f"https://{host}{path}", headers={"Accept": "application/json"})).json().get("items") or []
    if not items:
        return None
    d = items[0]
    body = "\n\n".join(html_to_text(d.get(k) or "", limit=None) for k in
                       ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr") if d.get(k))
    return Posting(
        url=url,
        title=d.get("Title") or "",
        location=d.get("PrimaryLocation") or "",
        description=body[:MAX_DESCRIPTION],
        full_text=body,
        apply_url=url,
        external_id=str(d.get("Id") or parts["job_id"]),
        # (both: "Contingent Worker" says contract, "Part time" the schedule; "Employee" alone says neither)
        employment_type=", ".join(str(d[k]) for k in ("WorkerType", "JobSchedule") if d.get(k)),
        posted_at=d.get("ExternalPostedStartDate") or "",
        parse_method="oracle-api",
        **said_workplace(d.get("WorkplaceTypeCode") or d.get("WorkplaceType")),  # (ORA_HYBRID, "On-site", where given)
    )


_CSOD_CONTEXT = re.compile(r"csod\.context\s*=\s*(?=\{)")


def csod_context(page: str) -> dict[str, Any]:
    """What a Cornerstone OnDemand career page sets up for its script (`csod.context={...}`):
    the anonymous token its search and posting calls carry, and its region's API address
    (endpoints.cloud). {} for a page without one."""
    found: dict[str, Any] = {}
    for m in _CSOD_CONTEXT.finditer(page or ""):  # the one with a token: not a placeholder set up before it
        try:
            ctx, _ = json.JSONDecoder().raw_decode(page, m.end())
        except ValueError:
            continue
        if isinstance(ctx, dict) and ctx.get("token"):
            return ctx
        found = found or (ctx if isinstance(ctx, dict) else {})
    return found


def csod_place(loc: Any) -> str:
    """A Cornerstone place ({"city": "Phoenix", "state": "AZ", "country": "US"}) as "Phoenix, AZ";
    not its country, as Canada's "CA" would read as California."""
    if not isinstance(loc, dict):
        return ""
    return ", ".join(str(loc[k]).strip() for k in ("city", "state") if str(loc.get(k) or "").strip())


async def _fetch_csod(client: httpx.AsyncClient, url: str) -> Posting | None:
    """A Cornerstone posting's page is drawn by its script: the page itself holds only the title
    and the start of the text (og:description, empty on Matheson's, Oct 2026). The script reads
    the posting from the site's job-requisition service, with the page's anonymous token."""
    parts = csod_parts(url)
    if not parts or not parts["requisition"]:
        return None
    ctx = csod_context((await _get(client, url, headers={"Accept": "text/html"})).text)
    if not ctx.get("token"):
        return None
    api = (f"https://{parts['host']}/services/x/job-requisition/v2/requisitions/{parts['requisition']}"
           f"/jobDetails?cultureId={ctx.get('cultureID') or 1}")
    r = await _get(client, api, headers={"Accept": "application/json", "Authorization": f"Bearer {ctx['token']}"})
    data = r.json().get("data") or {}
    if not data.get("displayTitle"):
        return None
    places = [csod_place(p) for p in [data.get("primaryLocation"), *(data.get("additionalLocations") or [])]]
    text = html_to_text(str(data.get("externalDescription") or ""), limit=None)
    posting = Posting(
        url=url,
        title=str(data["displayTitle"]),
        company=parts["corp"],  # (the site's own name for the employer: "linde"; its pages give no other)
        location="; ".join(dict.fromkeys(p for p in places if p)),
        description=text[:MAX_DESCRIPTION],
        full_text=text,
        apply_url=url,
        external_id=parts["requisition"],
        posted_at=str(data.get("openDate") or "")[:10],
        parse_method="csod-api",
    )
    if str(data.get("allowApply")).lower() == "false":
        posting.warnings.append("Cornerstone says this posting takes no applications now.")
    return posting


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
               "smartrecruiters": _fetch_smartrecruiters, "oracle_hcm": _fetch_oracle, "csod": _fetch_csod}.get(ats)
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


# Apply links that go somewhere the public can't apply, and where the same job is open to them.
# Edward Jones' home-office postings (live, Oct 2026) link Apply to its staff Brassring site
# (siteid 5377), behind a staff sign-in that ends on a host the public can't reach; the same
# job is on its public site (5374), with an "Apply to job" button.
_PUBLIC_APPLY = [(re.compile(r"(sjobs\.brassring\.com/.*[?&]partnerid=26235&(?:amp;)?siteid=)5377\b", re.I), r"\g<1>5374")]


def public_apply_url(url: str) -> str:
    if not url:
        return url
    for staff, public in _PUBLIC_APPLY:
        url = staff.sub(public, url)
    return url


def finalize(posting: Posting) -> Posting:
    posting.apply_url = public_apply_url(posting.apply_url)
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
    pay = pay_from_posting(posting)
    if pay is not None and pay.listed:
        for k, v in pay_fields(pay).items():
            setattr(posting, k, v)
    if not posting.workplace:  # no field said where it's done: its location's words, else its sentences (unsure)
        if (place := workplace_location_said(posting.location))[0]:  # (beside a place, or an option: "option")
            for k, v in workplace_fields(*place, source="location").items():
                setattr(posting, k, v)
        elif said := workplace_in_text(posting.full_text or posting.description):
            posting.workplace, posting.workplace_source = said, "text"
    posting.full_text = ""  # (read: let go)
    return posting
