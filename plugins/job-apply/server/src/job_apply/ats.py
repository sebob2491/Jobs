"""Identify which applicant tracking system (ATS) a URL belongs to."""

from __future__ import annotations

import re
from urllib.parse import urlparse

# (ats id, regex on host+path). First match wins.
_PATTERNS: list[tuple[str, str]] = [
    ("linkedin", r"(^|\.)linkedin\.com"),
    ("indeed", r"(^|\.)indeed\.com|smartapply\.indeed"),
    ("workday", r"myworkdayjobs\.com|myworkdaysite\.com|\.myworkday\.com"),
    ("greenhouse", r"greenhouse\.io"),
    ("lever", r"(^|\.)lever\.co"),
    ("icims", r"icims\.com"),
    ("successfactors", r"successfactors\.(com|eu)|sapsf\.(com|eu)|jobs\.sap\.com"),
    ("taleo", r"taleo\.net"),
    ("smartrecruiters", r"smartrecruiters\.com"),
    ("ashby", r"ashbyhq\.com"),
    ("eightfold", r"eightfold\.ai"),
    ("avature", r"avature\.net"),
    ("oracle_hcm", r"oraclecloud\.com/hcmUI|\.fa\.[a-z0-9]+\.oraclecloud\.com"),
    ("brassring", r"brassring\.com"),
    ("jobvite", r"jobvite\.com"),
    ("phenom", r"phenompeople\.com"),
]

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
    "company_site": "Company careers site",
}


def detect_ats(url: str | None) -> str:
    if not url:
        return "company_site"
    parsed = urlparse(url if "://" in url else "https://" + url)
    target = (parsed.netloc + parsed.path).lower()
    for ats, pattern in _PATTERNS:
        if re.search(pattern, target):
            return ats
    return "company_site"


def workday_parts(url: str) -> dict[str, str] | None:
    """Split a Workday posting URL into host/tenant/site/job path.

    https://acme.wd1.myworkdayjobs.com/en-US/External/job/Phoenix-AZ/Engineer_R123
      -> {host: acme.wd1.myworkdayjobs.com, tenant: acme, site: External,
          job_path: /job/Phoenix-AZ/Engineer_R123}
    """
    parsed = urlparse(url)
    host = parsed.netloc
    if "myworkdayjobs.com" not in host and "myworkdaysite.com" not in host:
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
    if rest and rest[-1] in ("apply", "applyManually", "autofillWithResume"):
        rest = rest[:-1]
    job_path = "/" + "/".join(rest) if rest else ""
    return {"host": host, "tenant": tenant, "site": site, "job_path": job_path}


def greenhouse_parts(url: str) -> dict[str, str] | None:
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url)
    if m:
        return {"board": m.group(1), "job_id": m.group(2)}
    m = re.search(r"greenhouse\.io/embed/job_app\?.*?for=([\w-]+).*?token=(\d+)", url)
    if m:
        return {"board": m.group(1), "job_id": m.group(2)}
    return None


def lever_parts(url: str) -> dict[str, str] | None:
    m = re.search(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})", url)
    if m:
        return {"company": m.group(1), "posting_id": m.group(2)}
    return None


def linkedin_job_id(url: str) -> str | None:
    m = re.search(r"linkedin\.com/jobs/view/(?:[\w-]*?-)?(\d{6,})", url) or re.search(
        r"currentJobId=(\d{6,})", url
    )
    return m.group(1) if m else None
