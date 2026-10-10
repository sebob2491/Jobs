"""Identify which applicant tracking system (ATS) a URL belongs to."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# Each job system's own domains: an address is the system's when its host is one of them
# or under one ("amat.wd1.myworkdayjobs.com"), never because one appears elsewhere in it
# ("evil.example/myworkdayjobs.com/", "myworkdayjobs.com.evil.example"). First match wins.
_DOMAINS: list[tuple[str, tuple[str, ...]]] = [
    ("linkedin", ("linkedin.com", "lnkd.in")),
    ("indeed", ("indeed.com", "indeed.co.uk", "indeed.ca")),
    ("workday", ("myworkdayjobs.com", "myworkdaysite.com", "myworkday.com")),
    ("greenhouse", ("greenhouse.io",)),
    ("lever", ("lever.co",)),
    ("icims", ("icims.com",)),
    ("successfactors", ("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu", "jobs.sap.com")),
    ("taleo", ("taleo.net",)),
    ("smartrecruiters", ("smartrecruiters.com",)),
    ("ashby", ("ashbyhq.com",)),
    ("eightfold", ("eightfold.ai",)),
    ("avature", ("avature.net",)),
    ("brassring", ("brassring.com",)),
    ("jobvite", ("jobvite.com",)),
    ("phenom", ("phenompeople.com",)),
    ("applicantstack", ("applicantstack.com",)),
    ("paycom", ("paycomonline.net", "paycomonline.com")),
    ("ukg", ("ultipro.com", "ukg.net")),
    ("infor", ("inforcloudsuite.com",)),
    ("csod", ("csod.com",)),  # Cornerstone OnDemand: <company>.csod.com
]
# Oracle's recruiting sites: a pod's own host, or a company's own address for its Oracle
# site (careers.ti.com), known by its path
_ORACLE_HOST = re.compile(r"(^|\.)fa\.[a-z0-9-]+\.oraclecloud\.com$")
_ORACLE_PATH = re.compile(r"^(?:/hcmui/candidateexperience)?/[a-z]{2}(?:-[a-z]{2})?/sites/[\w-]+/(?:job|requisitions/preview)/\d+")
# Employers whose career site runs a job system on their own address (companies.yaml)
COMPANY_HOSTS = {
    "careers.lamresearch.com": "eightfold", "jobs.infineon.com": "eightfold", "careers.micron.com": "eightfold",
    "careers.qorvo.com": "successfactors", "jobs.atlascopcogroup.com": "successfactors",
    # data/lists/phoenix-metro.yaml
    "careers.aps.com": "successfactors", "careers.srpnet.com": "successfactors",
    "jobs.northropgrumman.com": "eightfold", "careers.insight.com": "eightfold",
    "myhiring.kforce.com": "taleo",
}


def _host_and_path(url: str) -> tuple[str, str]:
    parsed = urlparse(url if "://" in url else "https://" + url)
    return (parsed.hostname or "").lower().rstrip("."), parsed.path or ""


def shared_system(url: str | None) -> str | None:
    """The job system whose own domain the address is on: one many employers share (a
    Workday tenant, Oracle's pods, LinkedIn). None for an employer's own address."""
    host, path = _host_and_path(url or "")
    for ats, domains in _DOMAINS:
        if any(host == d or host.endswith("." + d) for d in domains):
            return ats
    if _ORACLE_HOST.search(host) or (host == "oraclecloud.com" or host.endswith(".oraclecloud.com")) \
            and path.lower().startswith("/hcmui"):
        return "oracle_hcm"
    return None


ATS_NAMES = {
    "linkedin": "LinkedIn",
    "indeed": "Indeed",
    "workday": "Workday",
    "greenhouse": "Greenhouse",
    "lever": "Lever",
    "icims": "iCIMS",
    "successfactors": "SAP SuccessFactors",
    "taleo": "Oracle Taleo",
    "smartrecruiters": "SmartRecruiters",
    "ashby": "Ashby",
    "eightfold": "Eightfold",
    "avature": "Avature",
    "oracle_hcm": "Oracle Recruiting Cloud",
    "brassring": "BrassRing",
    "jobvite": "Jobvite",
    "phenom": "Phenom",
    "applicantstack": "ApplicantStack",
    "paycom": "Paycom",
    "ukg": "UKG Pro",
    "infor": "Infor",
    "csod": "Cornerstone",
    "company_site": "Company careers site",
}


def detect_ats(url: str | None) -> str:
    if not url:
        return "company_site"
    shared = shared_system(url)
    if shared:
        return shared
    host, path = _host_and_path(url)
    if host in COMPANY_HOSTS or host.removeprefix("www.") in COMPANY_HOSTS:
        return COMPANY_HOSTS.get(host) or COMPANY_HOSTS[host.removeprefix("www.")]
    if _ORACLE_PATH.match(path.lower()):
        return "oracle_hcm"
    return "company_site"


def workday_parts(url: str) -> dict[str, str] | None:
    """Split a Workday posting URL into host/tenant/site/job path.

    https://acme.wd1.myworkdayjobs.com/en-US/External/job/Phoenix-AZ/Engineer_R123
      -> {host: acme.wd1.myworkdayjobs.com, tenant: acme, site: External,
          job_path: /job/Phoenix-AZ/Engineer_R123}
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if shared_system(url) != "workday" or "myworkday.com" in host and "myworkdayjobs" not in host:
        return None
    tenant = host.split(".")[0]
    parts = [p for p in parsed.path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    # myworkdaysite.com uses /recruiting/<tenant>/<site>/...
    if parts and parts[0] == "recruiting" and len(parts) >= 3:
        tenant, parts = parts[1], parts[2:]
    if not parts:
        return None
    site = parts[0]
    rest = parts[1:]
    while rest and rest[-1] in ("apply", "applyManually", "autofillWithResume", "useMyLastApplication"):
        rest = rest[:-1]  # a copied address from part-way through applying: ".../apply/applyManually"
    job_path = "/" + "/".join(rest) if rest else ""
    return {"host": host, "tenant": tenant, "site": site, "job_path": job_path}


def greenhouse_parts(url: str) -> dict[str, str] | None:
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url, re.I)
    if m:
        return {"board": m.group(1), "job_id": m.group(2)}
    m = re.search(r"greenhouse\.io/embed/job_app\?.*?for=([\w-]+).*?token=(\d+)", url, re.I)
    if m:
        return {"board": m.group(1), "job_id": m.group(2)}
    return None


def greenhouse_form_url(url: str) -> str | None:
    """Greenhouse's own application form for one of its job-board postings. A board set to
    send visitors to the employer's careers page redirects the posting there, where the form
    sits in a frame that the site's cookie banner can hold back (asm.com); the form itself
    has no banner."""
    if shared_system(url) != "greenhouse":
        return None
    gh = greenhouse_parts(url)
    return f"https://job-boards.greenhouse.io/embed/job_app?for={gh['board']}&token={gh['job_id']}" if gh else None


def lever_parts(url: str) -> dict[str, str] | None:
    m = re.search(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})", url)
    if m:
        return {"company": m.group(1), "posting_id": m.group(2)}
    return None


def smartrecruiters_parts(url: str) -> dict[str, str] | None:
    m = re.search(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)/(\d{6,})", url)
    if m:
        return {"company": m.group(1), "posting_id": m.group(2)}
    return None


def oracle_parts(url: str) -> dict[str, str] | None:
    m = re.search(r"https?://([^/]+)(?:/hcmUI/CandidateExperience)?/[\w-]+/sites/([\w-]+)/(?:requisitions/preview|job)/(\d+)",
                  url, re.I)
    if m:
        return {"host": m.group(1), "site": m.group(2), "job_id": m.group(3)}
    return None


def csod_parts(url: str) -> dict[str, str] | None:
    """A Cornerstone OnDemand career site's parts, from its address or a posting's:
    https://linde.csod.com/ux/ats/careersite/23/home/requisition/33794?c=linde
      -> {host: linde.csod.com, site: 23, corp: linde, requisition: 33794}
    (requisition is "" for the site's own address)."""
    if shared_system(url) != "csod":
        return None
    parsed = urlparse(url)
    m = re.search(r"/careersite/(\d+)(?:/home)?(?:/requisition/(\d+))?", parsed.path, re.I)
    if not m:
        return None
    host = (parsed.hostname or "").lower()
    corp = (parse_qs(parsed.query).get("c") or [""])[0] or host.split(".")[0]
    return {"host": host, "site": m.group(1), "corp": corp, "requisition": m.group(2) or ""}


def linkedin_job_id(url: str) -> str | None:
    m = re.search(r"linkedin\.com/jobs/view/(?:[\w-]*?-)?(\d{6,})", url, re.I) or re.search(
        r"currentJobId=(\d{6,})", url
    )
    return m.group(1) if m else None
