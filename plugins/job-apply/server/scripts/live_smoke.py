"""Live smoke test against real employer career sites. Read-only by design.

    uv run python scripts/live_smoke.py --out live-report [--companies "KLA,ASM"] [--fixtures] [--parallel 4] [--role hr]

For each company with a `search` config in data/companies.yaml:
  1. search_company_jobs for a broad query, and keep the first result;
  2. read that posting over HTTP (ingest);
  3. open it in headless Chromium, click through "Apply" / "Apply Manually" until a
     form shows, list the form's fields, and run autofill with a FAKE profile;
  4. save a debug snapshot (and, with --fixtures, a test fixture).
Companies without a search API get a lighter check of their careers page.

With --pipeline it instead runs the Job Desk's one-button pipeline on one posting per
company: through Apply, sign-in pages (where it stops), every form step and the review
page. Questions the fake profile can't answer get throwaway answers for that run only.
With --fixtures too, a page it stopped on with questions or a problem becomes a test
fixture (tests/fixtures/live/pipeline-<company>).

--parallel N runs N groups of employers at once, each in a process (and browser) of its
own; every site is still visited once. --shard I/N runs only the I-th of N groups.

Safety: JOB_APPLY_NEVER_SUBMIT=1 is forced, so nothing can be submitted. The fake
profile has no resume, so nothing is uploaded. It never clicks sign-in, account
creation, "Autofill with Resume", "Next", or third-party apply buttons (LinkedIn,
Indeed, SEEK), and LinkedIn and Indeed are never contacted.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

os.environ["JOB_APPLY_NEVER_SUBMIT"] = "1"
os.environ["JOB_APPLY_HEADLESS"] = "1"
_HOME = Path(tempfile.mkdtemp(prefix="job-apply-live-"))
os.environ["JOB_APPLY_HOME"] = str(_HOME)

import secrets  # noqa: E402

import yaml  # noqa: E402

# A fresh address each run, on example.com (which takes no mail): Oracle's sites stop sending
# codes to an address after a day of runs ("Too Many Attempts. Try Again Later.").
FAKE_EMAIL = f"testy.mctestface.{secrets.token_hex(3)}@example.com"
FAKE_PROFILE = {
    "personal": {
        "first_name": "Testy", "last_name": "McTestface", "email": FAKE_EMAIL,
        "phone": "480-555-0199", "phone_country_code": "+1",
        # Chandler's city hall: a real street, so address lookups (Oracle's) can find it
        "address": {"line1": "175 S Arizona Ave", "city": "Chandler", "state": "AZ", "postal_code": "85225",
                    "country": "United States"},
        "linkedin_url": "https://www.linkedin.com/in/example-test-profile",
    },
    "documents": {"resume": None},
    "work_authorization": {"authorized_to_work": True, "requires_sponsorship": False, "us_person": True,
                           "us_citizen": True, "over_18": True},
    "preferences": {"how_did_you_hear": "Company Website", "willing_to_relocate": True,
                    "willing_to_travel": "Yes", "flexible_schedule": True},
    "eeo": {"gender": "Decline to self-identify", "hispanic_latino": "Decline to self-identify",
            "race": "Decline to self-identify", "veteran": "I don't wish to answer",
            "disability": "I do not want to answer"},
    "work_history": [{"title": "Equipment Technician", "company": "Example Fab", "location": "Chandler, AZ",
                      "start": "2021-03", "end": "present"}],
    "education_history": [{"school": "Arizona State University", "degree": "Bachelor's Degree",
                           "major": "Electrical Engineering", "start": 2016, "end": 2020}],
    "settings": {"submit_mode": "dry_run", "browser_channel": "chromium", "headless": True},
}
(_HOME / "profile.yaml").write_text(yaml.safe_dump(FAKE_PROFILE))

from job_apply import server  # noqa: E402
from job_apply.fixtures import convert  # noqa: E402
from job_apply.postings import fetch_posting  # noqa: E402
from job_apply.autofill import is_empty_value, polarity  # noqa: E402
from job_apply.search import load_companies, sitecore_search  # noqa: E402

QUERY_AZ = "field service | customer service engineer | customer engineer | equipment technician"  # in Arizona
QUERY_ANY = "engineer | technician"  # fallback so every company still gets a browser check
# --role: the kind of job a run looks for, and the fake applicant's background to match
ROLES: dict[str, dict[str, Any]] = {
    "technician": {"query_az": QUERY_AZ, "query_any": QUERY_ANY},
    "hr": {
        "query_az": "human resources | recruiter | talent acquisition | hr generalist",  # (four: the most searched)
        "query_any": "human resources | recruiter",
        "work_history": [{"title": "HR Coordinator", "company": "Example Staffing", "location": "Tempe, AZ",
                          "start": "2021-03", "end": "present"}],
        "education_history": [{"school": "Arizona State University", "degree": "Bachelor's Degree",
                               "major": "Business Administration", "start": 2016, "end": 2020}],
    },
}


def use_role(name: str) -> None:
    """Search for this role's jobs, as an applicant with its background (this run's home only)."""
    global QUERY_AZ, QUERY_ANY
    role = ROLES[name]
    QUERY_AZ, QUERY_ANY = role["query_az"], role["query_any"]
    profile = {**FAKE_PROFILE, **{k: role[k] for k in ("work_history", "education_history") if k in role}}
    (_HOME / "profile.yaml").write_text(yaml.safe_dump(profile))
APPLY = re.compile(r"^(apply( now| for (this|the) (job|position|role)( online)?)?|quick apply|apply to (this )?job|i'?m interested|"
                   r"start (your |my )?application|apply manually)$", re.I)
NEVER = re.compile(r"autofill|resume|last application|submit|sign ?in|log ?in|create account|register|upload|"
                   r"linked ?in|indeed|seek|google|facebook|next|continue|save", re.I)
COMPANY_TIMEOUT = 150
# Cookie banners: the test always takes the privacy-preserving choice.
REJECT_COOKIES = re.compile(r"^(reject( all)?( cookies)?|decline( all)?|only (strictly )?necessary|necessary only|"
                            r"use necessary cookies only|accept (only )?necessary( cookies)?)$", re.I)


async def dismiss_cookies() -> str | None:
    form = await server.inspect_form(include_dropdown_options=False)
    for a in form["actions"]:
        if REJECT_COOKIES.match(a["text"].strip()):
            await server.click(a["id"])
            return a["text"]
    return None


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def pick_action(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [a for a in actions if not a.get("disabled") and not a.get("is_submit") and APPLY.match(a["text"].strip())]
    usable = [a for a in usable if not NEVER.search(a["text"]) or re.search(r"apply manually", a["text"], re.I)]
    manual = [a for a in usable if re.search(r"manually", a["text"], re.I)]
    return (manual or usable or [None])[0]


def field_brief(f: dict[str, Any]) -> dict[str, Any]:
    out = {k: f[k] for k in ("kind", "label", "required", "section", "sublabel") if f.get(k) not in (None, "", False)}
    if f.get("options"):
        out["options"] = f["options"][:6] + (["…"] if len(f["options"]) > 6 else [])
    return out


def search_brief(found: dict[str, Any], name: str) -> dict[str, Any]:
    return {
        "count": len(found["results"]),
        "error": found["errors"].get(name),
        "sample": [{k: r.get(k) for k in ("title", "location", "url", "posted", "notes")} for r in found["results"][:4]],
    }


async def check_company(company: dict[str, Any], out: Path, fixtures: bool, rec: dict[str, Any]) -> None:
    """Fills in rec as it goes, so whatever was found survives a crash."""
    rec["search_config"] = company.get("search")
    # The MCP tool, so the Eightfold browser fallback and tracker marking run too.
    az = await server.search_company_jobs(QUERY_AZ, companies=[company["name"]], location="AZ", limit_per_company=5)
    rec["search_az"] = search_brief(az, company["name"])
    found = az
    if not az["results"]:
        # The tool again, so boards read in the browser (iCIMS, Paycom) get a posting to check too.
        found = await server.search_company_jobs(QUERY_ANY, companies=[company["name"]], location=None,
                                                 limit_per_company=3)
        rec["search_any"] = search_brief(found, company["name"])
    if not found["results"]:
        return
    first = found["results"][0]

    posting = None
    try:
        posting = await fetch_posting(first["url"])
        rec["posting"] = {
            "parse_method": posting.parse_method, "title": posting.title, "company": posting.company,
            "location": posting.location, "description_chars": len(posting.description), "ats": posting.ats,
            "apply_url": posting.apply_url, "external_id": posting.external_id, "warnings": posting.warnings,
        }
    except Exception as e:  # noqa: BLE001 - report everything
        rec["posting"] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}

    job = server.add_job(url=first["url"], title=first["title"], company=company["name"],
                         apply_url=(posting.apply_url if posting else "") or "")["job"]
    steps: list[dict[str, Any]] = []
    rec["steps"] = steps
    opened = await server.open_application(job_id=job["id"])
    if "error" in opened:
        rec["browser"] = {"error": opened["error"]}
        return
    steps.append({"step": "open", "url": opened["url"], "title": opened["title"], "headings": opened["headings"][:4],
                  "fields": opened["fields"], "actions": opened["actions"][:15],
                  "navigation_error": opened.get("navigation_error")})
    if dismissed := await dismiss_cookies():
        steps.append({"step": f"cookies: {dismissed}"})
    for _ in range(3):
        form = await server.inspect_form(include_dropdown_options=False)
        if len([f for f in form["fields"] if f["kind"] != "file"]) >= 3:
            break
        action = pick_action(form["actions"])
        if action is None:
            break
        step: dict[str, Any] = {"step": f"click {action['text']!r}"}
        steps.append(step)  # before clicking, so a crash shows which button it was
        clicked = await server.click(action["id"])
        step.update(clicked=clicked.get("clicked"), url=clicked.get("url"),
                    headings=(clicked.get("headings") or [])[:4], fields=clicked.get("fields"),
                    blocked=clicked.get("blocked"), note=clicked.get("note"))
        if dismissed := await dismiss_cookies():
            steps.append({"step": f"cookies: {dismissed}"})
    form = await server.inspect_form(include_dropdown_options=True)
    rec["form"] = {
        "url": form["url"], "ats": form["ats"], "headings": form["headings"][:6], "errors": form["errors"][:5],
        "fields": [field_brief(f) for f in form["fields"]],
        "actions": [a["text"] for a in form["actions"]][:30],
    }
    if form["fields"]:
        filled = await server.autofill(job_id=job["id"])
        rec["autofill"] = {
            "filled": [{"label": f["label"], "value": f["value"], "from": f["from"]} for f in filled["filled"]],
            "failed": filled["failed"],
            "needs_input": [field_brief(f) for f in filled["needs_input"]],
        }
    snap = await server.debug_snapshot(note=f"live smoke: {company['name']}")
    dest = out / "snapshots" / slug(company["name"])
    shutil.copytree(snap["saved_to"], dest, dirs_exist_ok=True)
    rec["snapshot_kb"] = sum(p.stat().st_size for p in dest.iterdir()) // 1024
    if fixtures and form["fields"]:
        fixture_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "live"
        convert(dest, f"live-{slug(company['name'])}", fixture_dir)
        rec["fixture"] = f"live-{slug(company['name'])}"


# Career pages whose job data comes from a call we haven't pinned down yet: record what
# the page itself requests, so the search can use it.
PROBES = {
    "Lam Research": "https://careers.lamresearch.com/careers?query=field%20service&location=Arizona",
    "Micron": "https://micron.eightfold.ai/careers?query=field%20service&domain=micron.com",
    "Infineon": "https://jobs.infineon.com/careers?query=field%20service&domain=infineon.com",
    "ASML": "https://www.asml.com/en/careers/find-your-job?query=field%20service",
    "Texas Instruments": "https://careers.ti.com/en/sites/CX/jobs?keyword=technician",
    "onsemi": "https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs?keyword=field+service",
    # Employers not in companies.yaml go here while their job board is worked out: what it's
    # built on, and how a listing looks. (SUSS MicroTec, Oct 2026: an EQS board of 80
    # openings, none in Arizona; its US ones are in Williston, VT.)
    # Employers in companies.yaml with no search yet (SuccessFactors and unknown sites):
    # their search pages, to see whether a list of openings can be read off them.
    # (Oct 2026: TSMC Arizona shows Cloudflare's check; Qorvo's search pages are SuccessFactors
    # HTML; Amkor's older SuccessFactors list is read in the browser now, and its posting page
    # wraps the posting in a form whose submit is "Apply". Canon USA and MKS block automated
    # browsers outright. Benchmark's Infor CloudSuite board is searched through its own page now.)
    # Equipment makers with field service engineers at Arizona fabs, not in the list yet.
    # Nikon's posting page: how its Apply button is drawn (the form reader doesn't see it)
    "Nikon Precision": "https://recruiting2.ultipro.com/NIK1001NIKON/JobBoard/f11a0b52-5153-4c12-ad2c-b7f3b0a74112/"
                       "OpportunityDetail?opportunityId=532a7dc9-8394-4cbc-8184-f43e88e906bf",
}
# Pages read over plain HTTP, as a search would read them: the markup of the parts a
# reader needs. (Oct 2026: an Edwards posting names its place in the unlabelled lines under
# its title; Qorvo's search pages are HTML tables, 25 rows a page; Amkor's career site has a
# search form and no list.)
HTTP_PROBES: dict[str, tuple[str, str]] = {}


# Search forms whose results the page only draws once the form is sent: send it in the
# browser, then record the requests that brought the list, the list, and the form, to write a
# reader from. (Oct 2026: Amkor's SuccessFactors site draws its list over DWR calls, 10 to a
# page or 50 when asked, and a posting opens on its own; its search reads it that way now.)
FORM_PROBES: dict[str, tuple[str, str]] = {}
FORMS_JS = r"""() => [...document.forms].slice(0, 6).map((f) => ({
  id: f.id, name: f.getAttribute('name'), action: f.getAttribute('action'), method: f.getAttribute('method'),
  fields: [...f.elements].slice(0, 40).map((e) => ({tag: e.tagName, type: e.type, name: e.name, id: e.id,
    value: String(e.value || '').slice(0, 80), shown: !!(e.offsetWidth || e.offsetHeight),
    label: ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.innerText || '')
      .replace(/\s+/g, ' ').trim().slice(0, 60)}))}))"""
REQ_LINKS_JS = r"""() => {
  const links = [...document.querySelectorAll('a[href]')]
    .filter((a) => /career_job_req_id|job_listing&|jobReqId|requisition/i.test(a.getAttribute('href') || ''));
  const first = links[0];
  const box = first && (first.closest('table') || first.parentElement);
  return {count: links.length,
          links: links.slice(0, 12).map((a) => {
            const row = a.closest('tr') || a.parentElement;
            return {href: a.href.slice(0, 400), text: (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120),
                    row: (row.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 300)};
          }),
          markup: box ? box.outerHTML.replace(/\s+/g, ' ').slice(0, 6000) : null};
}"""


async def probe_form(name: str, url: str, press: str) -> dict[str, Any]:
    await server.browser.page()
    ctx = server.browser._ctx  # noqa: SLF001 - test script reaching into the session on purpose
    tab = await ctx.new_page()
    sent: list[dict[str, Any]] = []

    def on_request(r: Any) -> None:
        if r.resource_type in ("document", "xhr", "fetch"):
            sent.append({"method": r.method, "type": r.resource_type, "url": r.url[:700],
                         "post": (r.post_data or "")[:2500]})

    replies: list[Any] = []

    async def _reply(r: Any) -> dict[str, Any]:
        try:
            body = await r.text()
        except Exception as e:  # noqa: BLE001
            body = f"unreadable: {e}"
        return {"url": r.url[:300], "type": r.headers.get("content-type", "")[:60], "chars": len(body),
                "body": body[:6000]}

    def on_response(r: Any) -> None:
        if ".dwr" in r.url:  # SuccessFactors' older pages talk to their server over DWR
            replies.append(asyncio.ensure_future(_reply(r)))

    tab.on("request", on_request)
    tab.on("response", on_response)
    rec: dict[str, Any] = {"form_probe": name, "url": url}
    try:
        await tab.goto(url, wait_until="domcontentloaded", timeout=45000)
        await tab.wait_for_timeout(8000)
        decline = tab.get_by_role("button", name=re.compile(r"^(reject|decline)( all)?$|necessary only", re.I))
        if await decline.count():
            await decline.first.click(timeout=5000)
            rec["cookies"] = "declined"
        rec["title"] = await tab.title()
        rec["forms"] = await tab.evaluate(FORMS_JS)
        rec["frames"] = [f.url[:300] for f in tab.frames if f is not tab.main_frame][:5]
        rec["before"] = await tab.evaluate(REQ_LINKS_JS)
        rec["requests_before"] = sent[:20]
        n = len(sent)
        button = tab.get_by_role("button", name=re.compile(press, re.I)).or_(
            tab.locator(f"input[type=submit][value*='{press}' i], input[type=button][value*='{press}' i]")).first
        rec["button"] = await button.evaluate("(e) => e.outerHTML.replace(/\\s+/g, ' ').slice(0, 800)")
        await button.click(timeout=10000)
        await tab.wait_for_timeout(9000)
        rec["url_after"] = tab.url
        rec["requests_after"] = sent[n:n + 20]
        rec["after"] = await tab.evaluate(REQ_LINKS_JS)
        rec["text_after"] = re.sub(r"\s+", " ", await tab.inner_text("body"))[:2000]
        # more to a page: what's sent, and how many show
        per_page = tab.locator("li.per_page select")
        if await per_page.count():
            n = len(sent)
            await per_page.first.select_option("50")
            await tab.wait_for_timeout(6000)
            rec["requests_per_page"] = sent[n:n + 10]
            rec["after_per_page"] = (await tab.evaluate(REQ_LINKS_JS))["count"]
        rec["replies"] = [await r for r in replies[:6]]
        # a posting, opened on its own (without the session's _s.crb), in a fresh tab and over HTTP
        links = (rec["after"] or {}).get("links") or []
        if links:
            posting = re.sub(r"&_s\.crb=[^&]*", "", links[0]["href"])
            rec["posting_url"] = posting
            page = await ctx.new_page()
            try:
                await page.goto(posting, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(6000)
                rec["posting_title"] = await page.title()
                rec["posting_url_after"] = page.url
                rec["posting_text"] = re.sub(r"\s+", " ", await page.inner_text("body"))[:3000]
            finally:
                await page.close()
            import httpx

            from job_apply.postings import USER_AGENT
            async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30) as client:
                r = await client.get(posting)
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))
            i = text.find(links[0]["text"][:20])
            rec["posting_http"] = {"status": r.status_code, "final_url": str(r.url)[:300], "chars": len(r.text),
                                   "has_title": i >= 0, "around_title": text[max(0, i - 200):i + 1500] if i >= 0
                                   else text[:1500]}
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        await tab.close()
    return rec


# JSON a job board's page calls for, asked for directly: cold, and again after loading the
# board's page in the same client (for its cookies), then through the page itself with a
# bigger page size: {name: (call's address, board page)}. (Oct 2026: Benchmark's Infor
# CloudSuite list timed out two times in three when asked directly; its page's own call,
# asked for 200, brought all 197 postings.)
JSON_PROBES: dict[str, tuple[str, str]] = {}


async def probe_json(name: str, url: str, warm: str | None = None) -> dict[str, Any]:
    import httpx

    from job_apply.postings import USER_AGENT

    rec: dict[str, Any] = {"json_probe": name, "url": url}

    def summary(data: Any) -> dict[str, Any]:
        view = data.get("dataViewSet") if isinstance(data, dict) else None
        if not isinstance(view, dict):
            return {"keys": sorted(data)[:30] if isinstance(data, dict) else type(data).__name__}
        items = view.get("data") or []
        field = lambda it, k: ((it.get("fields") or {}).get(k) or {}).get("value")  # noqa: E731
        return {"paging": view.get("pagingInfo"), "count": len(items), "ids": [it.get("resourceId") for it in items[:5]],
                "titles": [field(it, "Description") for it in items],
                "places": [field(it, "LocationOfJobDescriptionForSort") for it in items],
                "first": [{k: str(v.get("value") if isinstance(v, dict) else v)[:600]
                           for k, v in (it.get("fields") or {}).items()} for it in items[:2]],
                "item_keys": sorted(items[0]) if items else []}

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"},
                                 follow_redirects=True, timeout=45) as client:
        for label in ("warm", "cold") if warm else ("cold",):
            try:
                if label == "warm":
                    w = await client.get(warm)
                    rec["warm_page"] = {"status": w.status_code, "cookies": sorted(client.cookies.keys())}
                r = await client.get(url)
                out: dict[str, Any] = {"status": r.status_code, "type": r.headers.get("content-type", "")[:60],
                                       "chars": len(r.text)}
                try:
                    out.update(summary(r.json()))
                except ValueError:
                    out["text"] = r.text[:1500]
            except Exception as e:  # noqa: BLE001 - each way of asking on its own
                out = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            rec[label] = out
            client.cookies.clear()
    if warm:  # the board's page makes the call itself, asked for 200 at a time
        part = urlsplit(url).path.rsplit("/", 1)[-1]
        try:
            data = await server.browser.capture_json(
                warm, part, timeout=40000, rewrite_url=lambda u: re.sub(r"pagesize=\d+", "pagesize=200", u))
            rec["browser"] = summary(data)
        except Exception as e:  # noqa: BLE001
            rec["browser"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return rec


# Buttons that open a menu drawn by the page's script: press one in the browser and record
# the menu that appears around a text it shows. (Qorvo's "Apply now ▾" opens a menu of ways
# to apply that isn't in the page's HTML.)
MENU_PROBES = {
    "Qorvo": ("https://careers.qorvo.com/job/Chandler-Analog-Design-Intern-AZ-85226/1421977600/", "Apply now",
              "Apply with LinkedIn"),
}
MENU_JS = r"""(needle) => {
  const hits = [...document.querySelectorAll('body *')].filter((e) => e.children.length < 3
    && (e.textContent || '').includes(needle));
  return hits.slice(0, 3).map((hit) => {
    let box = hit;
    for (let i = 0; i < 4 && box.parentElement && box.parentElement !== document.body; i++) box = box.parentElement;
    return {
      html: box.outerHTML.replace(/\s+/g, ' ').slice(0, 5000),
      parts: [...box.querySelectorAll('a, button, [role], li')].slice(0, 20).map((e) => ({
        tag: e.tagName, role: e.getAttribute('role'), cls: e.className && String(e.className).slice(0, 80),
        href: e.getAttribute('href'), title: e.getAttribute('title'), aria: e.getAttribute('aria-label'),
        text: (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60),
        shown: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)})),
    };
  });
}"""


async def probe_menu(name: str, url: str, press: str, needle: str) -> dict[str, Any]:
    await server.browser.page()
    ctx = server.browser._ctx  # noqa: SLF001 - test script reaching into the session on purpose
    tab = await ctx.new_page()
    rec: dict[str, Any] = {"menu_probe": name, "url": url}
    try:
        await tab.goto(url, wait_until="domcontentloaded", timeout=45000)
        await tab.wait_for_timeout(6000)
        rec["before"] = await tab.evaluate(MENU_JS, needle)
        button = tab.get_by_role("button", name=press).or_(tab.get_by_role("link", name=press)).first
        rec["button"] = await button.evaluate("(e) => e.outerHTML.replace(/\\s+/g, ' ').slice(0, 1500)")
        await button.click(timeout=10000)
        await tab.wait_for_timeout(2500)
        rec["after"] = await tab.evaluate(MENU_JS, needle)
        # the menu beside the button, and everything on the page labelled "apply", by text or attribute
        rec["menu_html"] = await button.evaluate("""(b) => {
          const g = b.closest('.btn-group, .dropdown, .applylink') || b.parentElement;
          const m = g && g.querySelector('.dropdown-menu, [role="menu"], ul');
          return (m || g).outerHTML.replace(/\\s+/g, ' ').slice(0, 4000);
        }""")
        rec["apply_bits"] = await tab.evaluate("""() => [...document.querySelectorAll('a, button, [role], li')]
          .filter((e) => /apply/i.test((e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || '')
            + ' ' + (e.children.length < 3 ? e.textContent : '')))
          .slice(0, 20).map((e) => ({tag: e.tagName, role: e.getAttribute('role'), aria: e.getAttribute('aria-label'),
            title: e.getAttribute('title'), cls: String(e.className || '').slice(0, 80), href: e.getAttribute('href'),
            text: (e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 60),
            shown: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length),
            html: e.outerHTML.replace(/\\s+/g, ' ').slice(0, 300)}))""")
        rec["url_after"] = tab.url
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        await tab.close()
    return rec


async def probe_http(name: str, url: str, selector: str) -> dict[str, Any]:
    import httpx
    from bs4 import BeautifulSoup

    from job_apply.postings import USER_AGENT

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                                 follow_redirects=True, timeout=30) as client:
        r = await client.get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    if selector.startswith("text:"):  # the markup around each place the page says this
        found = []
        for hit in soup.find_all(string=re.compile(re.escape(selector[5:])))[:4]:
            box = hit.parent
            for _ in range(4):
                box = box.parent if box.parent is not None and box.parent.name != "body" else box
            found.append(box)
    else:
        found = soup.select(selector)[:8]
    return {"http_probe": name, "url": url, "status": r.status_code, "final_url": str(r.url), "chars": len(r.text),
            "parts": [re.sub(r"\s+", " ", str(e))[:4000] for e in found]}


# Job links as a page (or one of its frames) draws them, with the text of the card around
# each and a little of its markup, to write a reader for a new job board from.
JOB_LINKS_JS = r"""() => {
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!/\/job|find-your-job\/.+|jobid|requisition|\/detail\/|\/opening|\/position/i.test(href)) continue;
    let card = a;
    for (let i = 0; i < 4 && card.parentElement && (card.innerText || '').length < 80; i++) card = card.parentElement;
    out.push({href, text: (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120),
              card: (card.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 300),
              html: card.outerHTML.replace(/\s+/g, ' ').slice(0, 700)});
    if (out.length >= 12) break;
  }
  return out;
}"""

# Hosts of the job systems the search knows, or might learn: a careers page's links to one
# of these say where its openings really live.
ATS_HOST = re.compile(r"myworkdayjobs|myworkdaysite|myworkday\.com|icims\.com|applicantstack|greenhouse\.io|lever\.co|"
                      r"eightfold\.ai|successfactors|sapsf|oraclecloud|smartrecruiters|phenompeople|paylocity|ultipro|ukg|"
                      r"adp\.com|bamboohr|jobvite|taleo|workable|recruitee|ashbyhq|breezy|applytojob|dayforce|hrmos|softgarden", re.I)


_PLACE_KEY = re.compile(r"locat|city|country|state|region|address|place|site", re.I)


def _places(value: Any, key: str = "", depth: int = 0) -> list[str]:
    """Location-like values in a job record: 'jobOpening.locations[0].city=Corona'."""
    if depth > 5:
        return []
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _places(v, f"{key}.{k}" if key else str(k), depth + 1)]
    if isinstance(value, list):
        return [p for i, v in enumerate(value[:5]) for p in _places(v, f"{key}[{i}]", depth + 1)]
    if isinstance(value, (str, int, float)) and str(value) and _PLACE_KEY.search(key):
        return [f"{key}={str(value)[:60]}"]
    return []


async def probe_page(name: str, url: str) -> dict[str, Any]:
    await server.browser.page()
    ctx = server.browser._ctx  # noqa: SLF001 - test script reaching into the session on purpose
    assert ctx is not None
    tab = await ctx.new_page()  # a tab of its own, not a popup, so it never becomes the current one
    seen: list[dict[str, Any]] = []

    samples: list[Any] = []

    def on_response(r: Any) -> None:
        ctype = r.headers.get("content-type", "")
        infor = "inforcloudsuite.com" in r.url and r.request.resource_type in ("xhr", "fetch")
        if "json" in ctype or "/api/" in r.url or infor:
            keep = 1500 if ("recruitingCEJobRequisitions" in r.url or "pcsx/search" in r.url) else 300
            seen.append({"method": r.request.method, "status": r.status, "url": r.url[:keep], "type": ctype[:40]})
            if "/discover/v2/" in r.url:  # ASML's job search (Sitecore Discover): keep the request and an answer
                samples.append(asyncio.ensure_future(_sample(r)))
            elif infor or any(part in r.url for part in ("jobPublication/list.json", "job-posting-previews/search",
                                                           "LoadSearchResults", "/services/recruiting/v1/jobs",
                                                           "/recruiting/career/v1/jobs")):
                # SUSS's job list; Paycom's (Ebara); UKG Pro's (Nikon); SuccessFactors' newer one
                # (Edwards), and the call its posting pages make; Infor's (Benchmark)
                samples.append(asyncio.ensure_future(_sample(r, 4000)))

    async def _sample(r: Any, keep: int = 1500) -> dict[str, Any]:
        try:
            body = await r.text()
        except Exception as e:  # noqa: BLE001
            body = f"unreadable: {e}"
        out: dict[str, Any] = {"url": r.url[:600], "request": (r.request.post_data or "")[:3000], "response": body[:keep]}
        try:  # a list of openings (SUSS; Paycom's under a key): the fields one has, and a whole one
            items = json.loads(body)
            if isinstance(items, dict):
                out["keys"] = sorted(items)[:20]
                items = next((v for v in items.values() if isinstance(v, list) and v and isinstance(v[0], dict)), items)
            if isinstance(items, list) and items and isinstance(items[0], dict):
                out["count"] = len(items)
                out["item_keys"] = sorted(items[0])
                out["first_item"] = json.dumps({k: v for k, v in items[0].items()
                                                if not isinstance(v, str) or len(v) < 300}, default=str)[:3000]
                # where each one is: any location-like values, wherever they sit in the item
                out["places"] = [{"title": str(i.get("position") or i.get("jobTitle") or i.get("title") or "")[:80],
                                  "lang": i.get("language"), "where": _places(i)[:6]} for i in items[:100]]
        except Exception:  # noqa: BLE001
            pass
        try:  # the answer's shape: totals, filter names and values, where the openings are
            out["widgets"] = [{
                "keys": sorted(k for k in w if k not in ("content", "facet")),
                "total_item": w.get("total_item"),
                "facets": {f.get("name"): [v.get("text") for v in f.get("value") or []][:20] for f in w.get("facet") or []},
                "locations": [c.get("job_location") for c in w.get("content") or []],
            } for w in json.loads(body).get("widgets") or []]
        except Exception:  # noqa: BLE001
            pass
        return out

    tab.on("response", on_response)
    rec: dict[str, Any] = {"probe": name, "url": url}
    try:
        resp = await tab.goto(url, wait_until="domcontentloaded", timeout=45000)
        rec["document_status"] = resp.status if resp else None
        await tab.wait_for_timeout(12000)
        rec["title"] = await tab.title()
        rec["json_calls"] = seen[:30]
        text = re.sub(r"\s+", " ", await tab.inner_text("body"))
        rec["text_sample"], rec["text_more"] = text[:500], text[500:3500]
        if samples:
            rec["api_samples"] = [await s for s in samples[-6:]]
        # job links as the page draws them (and its frames: iCIMS lists jobs in one), with the
        # text of the card around each and the card's markup
        job_links: list[Any] = []
        for frame in tab.frames:
            try:
                job_links += await frame.evaluate(JOB_LINKS_JS)
            except Exception:  # noqa: BLE001 - a frame that went away
                continue
        rec["job_links"] = job_links[:12]
        # Phenom career sites (Thermo Fisher) draw their search results from data in the page
        rec["phenom"] = await tab.evaluate("""() => {
          const d = window.phApp && window.phApp.ddo;
          if (!d) return null;
          const s = d.eagerLoadRefineSearch || d.refineSearch;
          return {keys: Object.keys(d).slice(0, 30), search: s ? JSON.stringify(s).slice(0, 3000) : null};
        }""")
        # where the page says the job is: elements named for a location, and JSON-LD
        rec["location_bits"] = await tab.evaluate("""() => {
          const bits = [...document.querySelectorAll('[class*="location" i], [id*="location" i], [class*="geo" i], '
            + '[itemprop*="address" i], [data-careersite-propertyid*="city" i], [data-careersite-propertyid*="state" i]')]
            .slice(0, 8).map((e) => e.outerHTML.slice(0, 300));
          const ld = [...document.querySelectorAll('script[type="application/ld+json"]')].map((s) => s.textContent.slice(0, 600));
          const text = document.body.innerText;
          const i = text.search(/location/i);
          return {bits, ld, around: i >= 0 ? text.slice(Math.max(0, i - 100), i + 300) : ''};
        }""")
        # the markup of anything that reads like an apply or sign-in button, whatever it's drawn with
        rec["apply_buttons"] = await tab.evaluate("""() => [...document.querySelectorAll('body *')]
          .filter((e) => /^\\s*(apply( now)?|quick apply|sign in)\\s*$/i.test(e.textContent || '') && e.children.length < 4)
          .slice(0, 8).map((e) => ({tag: e.tagName, type: e.type || null, html: e.outerHTML.slice(0, 500),
                                   form: e.form ? (e.form.id || e.form.getAttribute('name') || 'unnamed') : null,
                                   form_fields: e.form ? [...e.form.elements].filter((f) => /^(INPUT|SELECT|TEXTAREA)$/.test(f.tagName)
                                     && !/^(hidden|submit|button|image|reset)$/i.test(f.type || '')
                                     && !!(f.offsetWidth || f.offsetHeight)).length : null,
                                   parent: (e.parentElement ? e.parentElement.outerHTML : '').slice(0, 300)}))""")
        hrefs = await tab.evaluate("() => [...document.querySelectorAll('a[href], iframe[src]')].map(e => e.href || e.src)")
        rec["ats_links"] = sorted({h for h in hrefs if ATS_HOST.search(h)})[:10]
        rec["frames"] = [f.url[:200] for f in tab.frames if f is not tab.main_frame][:5]
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        await tab.close()
    return rec


async def check_sitecore(company: dict[str, Any]) -> dict[str, Any]:
    """Every page of one wording, before any location filter: is the user's area in there?"""
    found: list[Any] = []
    rec: dict[str, Any] = {"sitecore_check": company["name"]}
    try:
        await sitecore_search(server.browser.capture_json, company["search"]["sitecore"], "field service", found)
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    rec["count"] = len(found)
    rec["us_locations"] = sorted({f.location for f in found if re.search(r"\bUS\b|United States", f.location)})
    rec["arizona"] = [{"title": f.title, "location": f.location, "url": f.url} for f in found
                      if re.search(r"\bAZ\b|Arizona|Phoenix|Chandler", f.location)]
    return rec


async def check_careers_page(company: dict[str, Any], rec: dict[str, Any]) -> None:
    """Companies without a search API: can the browser read their careers page at all?"""
    rec.update(careers_url=company.get("careers_url"), ats=company.get("ats"))
    opened = await server.open_application(url=company["careers_url"])
    if "error" in opened:
        rec["browser"] = {"error": opened["error"]}
        return
    form = await server.inspect_form(include_dropdown_options=False)
    text = await server.page_text(4000)
    rec["page"] = {
        "url": form["url"], "title": opened["title"], "headings": form["headings"][:6], "ats_detected": form["ats"],
        "navigation_error": opened.get("navigation_error"),
        "search_fields": [field_brief(f) for f in form["fields"] if f["kind"] in ("text", "combobox")][:5],
        "actions": [a["text"] for a in form["actions"]][:20],
        "text_sample": re.sub(r"\s+", " ", text)[:600],
    }


async def page_state() -> dict[str, Any]:
    """Where the browser was when something failed."""
    try:
        form = await asyncio.wait_for(server.inspect_form(include_dropdown_options=False), 20)
        return {"url": form["url"], "headings": form["headings"][:6], "fields": len(form["fields"]),
                "actions": [a["text"] for a in form["actions"]][:20]}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("live-report"))
    ap.add_argument("--companies", default="", help="comma-separated names (default: all)")
    ap.add_argument("--fixtures", action="store_true", help="also write tests/fixtures/live/ fixtures")
    ap.add_argument("--pipeline", action="store_true", help="run the one-button apply pipeline instead")
    ap.add_argument("--fake-passwords", action="store_true",
                    help="with --pipeline: a throwaway saved password for each job system the desk takes one for, "
                         "so a run tries the sign-in once and fills in Create Account (it never creates one)")
    ap.add_argument("--parallel", type=int, default=1, help="run this many groups of employers at once")
    ap.add_argument("--shard", default="", help="I/N: only the I-th of N groups (what --parallel runs)")
    ap.add_argument("--lists", default="", help="the plugin's employer lists to check, comma-separated "
                                                "(default: semiconductor-az; e.g. phoenix-metro,semiconductor-az)")
    ap.add_argument("--role", choices=sorted(ROLES), default="technician",
                    help="the kind of job to look for, with a fake applicant to match (default: technician)")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    use_role(args.role)
    if args.lists:  # as a person's own companies.yaml names them, in this run's own home
        (_HOME / "companies.yaml").write_text(yaml.safe_dump({"lists": [n.strip() for n in args.lists.split(",")
                                                                        if n.strip()]}))
    if args.parallel > 1 and not args.shard:
        return await parallel_main(args)
    wanted = [n.strip().lower() for n in args.companies.split(",") if n.strip()]
    companies = [c for c in load_companies() if not wanted or any(w in c["name"].lower() for w in wanted)]
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        companies = companies[i::n]
    if args.pipeline:
        if args.fake_passwords:
            from job_apply.pipeline import DESK_PASSWORDS
            for name in DESK_PASSWORDS:  # this run's environment only; never written anywhere
                os.environ[f"JOB_APPLY_SECRET_{name.upper()}"] = f"Throwaway-{secrets.token_urlsafe(12)}-1!"
        return await pipeline_main(companies, args.out, args.fixtures)

    records = []
    for company in companies:
        started = time.time()
        rec: dict[str, Any] = {"company": company["name"]}
        try:
            if company.get("search"):
                await asyncio.wait_for(check_company(company, args.out, args.fixtures, rec), COMPANY_TIMEOUT)
            else:
                await asyncio.wait_for(check_careers_page(company, rec), 60)
        except Exception as e:  # noqa: BLE001
            rec["crash"] = f"{type(e).__name__}: {str(e)[:300]}"
            rec["trace"] = traceback.format_exc()[-1500:]
            rec["page_at_crash"] = await page_state()
            await server.close_browser()  # start the next company with a fresh browser
        rec["seconds"] = round(time.time() - started, 1)
        records.append(rec)
        print("LIVE_RESULT " + json.dumps(rec, default=str), flush=True)

    for company in companies:
        if "sitecore" in (company.get("search") or {}):
            check = await asyncio.wait_for(check_sitecore(company), 120)
            print("LIVE_SITECORE " + json.dumps(check, default=str), flush=True)
    for name, url in PROBES.items():
        if wanted and not any(w in name.lower() for w in wanted) or args.shard and not args.shard.startswith("0/"):
            continue  # (one group checks these, not every one)
        try:
            probe = await asyncio.wait_for(probe_page(name, url), 90)
        except Exception as e:  # noqa: BLE001
            probe = {"probe": name, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        print("LIVE_PROBE " + json.dumps(probe, default=str), flush=True)
    for name, (url, press, needle) in MENU_PROBES.items():
        if wanted and not any(w in name.lower() for w in wanted):
            continue
        try:
            probe = await asyncio.wait_for(probe_menu(name, url, press, needle), 90)
        except Exception as e:  # noqa: BLE001
            probe = {"menu_probe": name, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        print("LIVE_MENU " + json.dumps(probe, default=str), flush=True)
    for name, (url, press) in FORM_PROBES.items():
        if wanted and not any(w in name.lower() for w in wanted):
            continue
        try:
            probe = await asyncio.wait_for(probe_form(name, url, press), 150)
        except Exception as e:  # noqa: BLE001
            probe = {"form_probe": name, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        print("LIVE_FORM " + json.dumps(probe, default=str), flush=True)
    for name, (url, warm) in JSON_PROBES.items():
        if wanted and not any(w in name.lower() for w in wanted):
            continue
        try:
            probe = await asyncio.wait_for(probe_json(name, url, warm), 90)
        except Exception as e:  # noqa: BLE001
            probe = {"json_probe": name, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        print("LIVE_JSON " + json.dumps(probe, default=str), flush=True)
    for name, (url, selector) in HTTP_PROBES.items():
        if wanted and not any(w in name.lower() for w in wanted):
            continue
        try:
            probe = await asyncio.wait_for(probe_http(name, url, selector), 60)
        except Exception as e:  # noqa: BLE001
            probe = {"http_probe": name, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        print("LIVE_HTTP " + json.dumps(probe, default=str), flush=True)

    await server.close_browser()
    (args.out / "report.json").write_text(json.dumps(records, indent=2, default=str))
    lines = ["| Company | AZ matches | Posting | Form fields | Autofilled | Failed | Notes |", "|---|---|---|---|---|---|---|"]
    for r in records:
        s = r.get("search_az") or {}
        p = r.get("posting") or {}
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(*map(_cell, (
            r["company"],
            s.get("error") or s.get("count", "–"),
            p.get("error", "")[:40] or p.get("parse_method", "–"),
            len((r.get("form") or {}).get("fields", [])) if "form" in r else "–",
            len((r.get("autofill") or {}).get("filled", [])) if "autofill" in r else "–",
            len((r.get("autofill") or {}).get("failed", [])) if "autofill" in r else "–",
            (r.get("crash") or (r.get("browser") or {}).get("error") or (r.get("page") or {}).get("title", ""))[:60],
        ))))
    (args.out / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


PIPELINE_WAIT = 180


def _cell(value: Any) -> str:
    """A markdown table cell: a "|" in a job title would split it."""
    return str(value).replace("|", "\\|")


async def print_shot(name: str, page: Any) -> None:
    """A small screenshot in the log, where it can be read without the run's artifacts.
    The page only ever holds the fake applicant's details."""
    import base64

    if page is None or page.is_closed():
        return
    try:
        data = await page.screenshot(type="jpeg", quality=35, full_page=False, scale="css")
        print(f"LIVE_SHOT {json.dumps(name)} {base64.b64encode(data).decode()}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"LIVE_SHOT {json.dumps(name)} failed: {e}", flush=True)


def fake_answer(q: dict[str, Any]) -> Any:
    """A throwaway answer for a question the fake profile can't answer (this run only)."""
    options = [o for o in q.get("options") or []
               if o and not is_empty_value(o) and not re.match(r"^(select|choose|--|please)", o, re.I)]
    if q.get("kind") == "checkbox":
        return "Yes"
    if options:
        # "No" where there is one, as most applicants answer "applied before?", "relatives
        # here?" or "terminated?"; a "Yes" brings follow-up questions the run then has to answer
        return next((o for o in options if polarity(o) is False), options[0])
    if re.search(r"year|salary|number|how many|zip|postal|\bgpa\b", q.get("label") or "", re.I):
        return "0"
    return "Test answer"


async def _question_markup(run: Any) -> list[str]:
    if run.page is None or run.page.is_closed():
        return []
    out = []
    for q in run.questions[:12]:
        try:
            out.append(await run.page.evaluate(
                """(id) => { const e = document.querySelector(`[data-ja-id="${id}"]`);
                  return e ? e.outerHTML.replace(/\\s+/g, ' ').slice(0, 700) : 'not found'; }""", str(q.get("id"))))
        except Exception as e:  # noqa: BLE001
            out.append(f"{type(e).__name__}: {str(e)[:100]}")
    return out


# An element's markup and the elements around it, with only the attributes that say what
# it is and whether it shows (the page holds only the fake applicant's details)
_MARKUP_JS = r"""
(id) => {
  const el = document.querySelector(`[data-ja-id="${id}"]`);
  if (!el) return null;
  const KEEP = /^(data-automation-id|role|aria-label|aria-hidden|aria-expanded|aria-disabled|aria-modal|type|tabindex|hidden|href|inert|disabled)$/;
  const short = (node) => {
    const c = node.cloneNode(true);
    for (const n of [c, ...c.querySelectorAll('*')]) {
      if (/^(SCRIPT|STYLE|SVG|PATH)$/i.test(n.tagName)) { n.remove(); continue; }
      for (const a of [...n.attributes]) if (!KEEP.test(a.name)) n.removeAttribute(a.name);
    }
    return c.outerHTML.replace(/\s+/g, ' ');
  };
  const chain = [];
  for (let n = el.parentElement, d = 0; n && d < 8; n = n.parentElement, d++) {
    const s = getComputedStyle(n);
    chain.push([n.tagName.toLowerCase(), n.getAttribute('data-automation-id') || '', n.getAttribute('role') || '',
                n.getAttribute('aria-hidden') || '', s.display, s.visibility, s.opacity,
                Math.round(n.getBoundingClientRect().width) + 'x' + Math.round(n.getBoundingClientRect().height)].join('|'));
  }
  const r = el.getBoundingClientRect();
  const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  return { markup: short(el).slice(0, 600), parent: short(el.parentElement).slice(0, 1200), chain,
           box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
           on_top: hit === el || el.contains(hit) ? 'itself' : (hit ? short(hit).slice(0, 300) : null) };
}
"""


async def _account_page(press: str | None) -> dict[str, Any]:
    form = await server.inspect_form(include_dropdown_options=False)
    return {"pressed": press, "url": form["url"], "title": form.get("title"),
            "headings": form["headings"][:10], "actions": [a["text"] for a in form["actions"]][:24],
            "fields": [{k: f.get(k) for k in ("label", "kind", "required", "sublabel", "options")}
                       for f in form["fields"]][:30]}


async def account_form() -> dict[str, Any]:
    """At a sign-in the run stopped on: the form its way to a new account opens ("Create an
    account", "Register", "Sign up"), which the desk fills when a saved password doesn't sign
    in. Opened and read only, with the queue stopped, so nothing is filled in or sent. Only
    from a sign-in page (one password box): on a Create Account form, its own "Create
    Account" button would create the account.

    Each way in is recorded with its markup; when one doesn't open a form with a second
    password box, the next one is tried (up to three)."""
    from job_apply.pipeline import _CREATE_ACCOUNT

    def ways(form: dict[str, Any]) -> list[dict[str, Any]]:
        return [a for a in form["actions"] if _CREATE_ACCOUNT.match(a["text"].strip()) and not a.get("disabled")
                and not a.get("is_submit") and not a.get("form_submit") and not a.get("account_form")]

    try:
        form = await server.inspect_form(include_dropdown_options=False)
        email_step = next((a for a in form["actions"] if re.match(r"^sign in with email$", a["text"].strip(), re.I)), None)
        if not form["fields"] and email_step is not None:
            # Workday's "Sign in with Apple / Google / email" step: this only shows the form
            await server.click(email_step["id"])
            await asyncio.sleep(4)
            form = await server.inspect_form(include_dropdown_options=False)
        if sum(f["kind"] == "password" for f in form["fields"]) != 1 or any(
                f.get("value") for f in form["fields"] if f["kind"] in ("text", "email", "password")):
            # an account form, or one the desk filled in (a --fake-passwords run): left as it is
            return {"skipped": "not an untouched sign-in page", "fields": [f.get("label") for f in form["fields"]][:20]}
        found = ways(form)
        if not found:
            return {"none": [a["text"] for a in form["actions"]][:20]}
        page = await server.browser.page()
        signin_url = page.url
        await print_shot("sign-in page", page)
        rec: dict[str, Any] = {"ways": [await page.evaluate(_MARKUP_JS, str(a["id"])) for a in found[:4]], "tries": []}
        for i in range(min(3, len(found))):
            if i:  # back to the sign-in page as it was
                await page.goto(signin_url)
                await asyncio.sleep(6)
                again = await server.inspect_form(include_dropdown_options=False)
                step = next((a for a in again["actions"] if re.match(r"^sign in with email$", a["text"].strip(), re.I)), None)
                if not again["fields"] and step is not None:
                    await server.click(step["id"])
                    await asyncio.sleep(4)
                    again = await server.inspect_form(include_dropdown_options=False)
                found = ways(again)
                if i >= len(found):
                    break
            await server.click(found[i]["id"])
            await asyncio.sleep(6)
            after = await _account_page(f"{found[i]['text']} (way {i + 1} of {len(found)})")
            rec["tries"].append(after)
            await print_shot(f"account form, way {i + 1}", page)
            if sum(f["kind"] == "password" for f in after["fields"]) != 1:
                break
        return rec
    except Exception as e:  # noqa: BLE001 - a probe; the run's record stands without it
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


_NO_MATCH = re.compile(r"nothing in its list matched '(.+)'")
# A search box the desk couldn't pick from: the list it shows once a value is typed. Its
# markup with only the attributes that say what each part is (the page holds only the fake
# applicant's details), and how many of each role are showing.
_LIST_JS = r"""
(id) => {
  const el = document.querySelector(`[data-ja-id="${id}"]`);
  if (!el) return null;
  const KEEP = /^(data-automation-id|role|aria-label|aria-selected|aria-hidden|aria-expanded|aria-activedescendant|id|class|tabindex|type|name)$/;
  const short = (node) => {
    const c = node.cloneNode(true);
    for (const n of [c, ...c.querySelectorAll('*')]) {
      if (/^(SCRIPT|STYLE|SVG|PATH)$/i.test(n.tagName)) { n.remove(); continue; }
      for (const a of [...n.attributes]) {
        if (!KEEP.test(a.name)) n.removeAttribute(a.name);
        else if (a.name === 'class') n.setAttribute('class', a.value.split(/\s+/).slice(0, 3).join(' '));
      }
    }
    return c.outerHTML.replace(/\s+/g, ' ');
  };
  const shown = (n) => n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
  const ref = el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '';
  const box = ref ? document.getElementById(ref.split(/\s+/)[0]) : null;
  const roles = {};
  for (const n of (box || document).querySelectorAll('[role]')) {
    if (shown(n)) roles[n.getAttribute('role')] = (roles[n.getAttribute('role')] || 0) + 1;
  }
  return { controls: ref, box: box ? short(box).slice(0, 3000) : null, box_shown: box ? shown(box) : null,
           roles, value: el.value, expanded: el.getAttribute('aria-expanded'),
           active: el.getAttribute('aria-activedescendant') };
}
"""


async def list_probe(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For up to three search boxes whose list the desk couldn't pick from: type the value
    again and record the list that shows (the run is over; nothing is sent)."""
    out: list[dict[str, Any]] = []
    try:
        page = await server.browser.page()
        for q in questions:
            m = _NO_MATCH.search(q.get("error") or "")
            if not m or len(out) >= 3:
                continue
            loc = page.locator(f'[data-ja-id="{q["id"]}"]')
            try:
                # focus and type without a pointer click: a cookie banner may cover the page
                # (TI's has no Reject, so it stays), and typing goes in under it
                await loc.scroll_into_view_if_needed(timeout=5000)
                await loc.evaluate("el => el.focus()")
                await loc.fill("")
                await loc.press_sequentially(m.group(1), delay=80)
                await asyncio.sleep(3)
                info = await page.evaluate(_LIST_JS, str(q["id"])) or {}
                await print_shot(f"list for {q.get('label')}", page)
                await page.keyboard.press("Escape")
            except Exception as e:  # noqa: BLE001
                info = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
            out.append({"label": q.get("label"), "typed": m.group(1), **info})
    except Exception as e:  # noqa: BLE001 - a probe; the run's record stands without it
        out.append({"error": f"{type(e).__name__}: {str(e)[:200]}"})
    return out


async def check_pipeline(company: dict[str, Any], out: Path, rec: dict[str, Any], fixtures: bool = False) -> None:
    from job_apply.pipeline import Applier, question_key

    found = await server.search_company_jobs(QUERY_AZ, companies=[company["name"]], location="AZ", limit_per_company=5)
    if not found["results"]:
        found = await server.search_company_jobs(QUERY_ANY, companies=[company["name"]], location=None,
                                                 limit_per_company=3)
    if not found["results"]:
        rec["note"] = "no postings found"
        return
    first = found["results"][0]
    rec["posting"] = {k: first.get(k) for k in ("title", "location", "url")}
    apply_url = ""
    try:  # as the desk does: the posting's own apply link, when it has one
        apply_url = (await fetch_posting(first["url"])).apply_url
    except Exception as e:  # noqa: BLE001
        rec["posting"]["read_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    rec["posting"]["apply_url"] = apply_url
    job = server.add_job(url=first["url"], title=first["title"], company=company["name"], apply_url=apply_url)["job"]
    applier = Applier(server)
    applier.start()
    rec["rounds"] = []
    run = None
    try:
        run = applier.enqueue(job["id"])
        for _ in range(3):
            start = time.monotonic()
            while run.status in ("queued", "running"):
                if time.monotonic() - start > PIPELINE_WAIT:
                    raise TimeoutError(f"still {run.status} after {PIPELINE_WAIT}s; log: {run.log[-3:]}")
                await asyncio.sleep(0.5)
            rec["rounds"].append({
                "status": run.status, "need": run.need, "reason": run.reason, "url": run.url, "log": list(run.log),
                "questions": [{k: q.get(k) for k in ("label", "kind", "required", "options", "error")}
                              for q in run.questions],
                # each asked-about field's markup, to see what kind of control it is
                "markup": await _question_markup(run),
                "page": run.page_info,
            })
            if run.need == "stuck":
                await print_shot(company["name"], run.page)
            if run.status == "needs_you" and run.need == "questions":
                for q in run.questions:
                    run.once[question_key(q.get("label") or "")] = fake_answer(q)
                applier.enqueue(job["id"], front=True)
                continue
            break
    finally:
        await applier.stop()
        if (rec.get("rounds") or [{}])[-1].get("need") == "sign_in":
            rec["account_form"] = await account_form()
        if run is not None and any(_NO_MATCH.search(q.get("error") or "") for q in run.questions):
            rec["lists"] = await list_probe(run.questions)
        try:
            snap = await server.debug_snapshot(note=f"live pipeline: {company['name']}")
            dest = out / "pipeline" / slug(company["name"])
            shutil.copytree(snap["saved_to"], dest, dirs_exist_ok=True)
            if fixtures and (rec.get("rounds") or [{}])[-1].get("need") in ("questions", "stuck"):
                # the page it stopped on, as a test fixture (the fake applicant's details only)
                fixture_dir = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "live"
                convert(dest, f"pipeline-{slug(company['name'])}", fixture_dir)
                rec["fixture"] = f"pipeline-{slug(company['name'])}"
        except Exception as e:  # noqa: BLE001 - the record matters more than the snapshot
            rec["snapshot_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        await server.close_browser()


async def pipeline_main(companies: list[dict[str, Any]], out: Path, fixtures: bool = False) -> int:
    records = []
    for company in [c for c in companies if c.get("search")]:
        started = time.time()
        rec: dict[str, Any] = {"company": company["name"]}
        try:
            await asyncio.wait_for(check_pipeline(company, out, rec, fixtures), 3 * PIPELINE_WAIT + 60)
        except Exception as e:  # noqa: BLE001
            rec["crash"] = f"{type(e).__name__}: {str(e)[:300]}"
            rec["trace"] = traceback.format_exc()[-1500:]
            await server.close_browser()
        rec["seconds"] = round(time.time() - started, 1)
        records.append(rec)
        print("LIVE_PIPELINE " + json.dumps(rec, default=str), flush=True)
    (out / "pipeline.json").write_text(json.dumps(records, indent=2, default=str))
    _pipeline_report(records, out)
    return 0


def _pipeline_report(records: list[dict[str, Any]], out: Path) -> None:
    lines = ["| Company | Posting | Ended | Waiting on | Steps | Questions answered |", "|---|---|---|---|---|---|"]
    for r in records:
        rounds = r.get("rounds") or []
        last = rounds[-1] if rounds else {}
        lines.append("| {} | {} | {} | {} | {} | {} |".format(*map(_cell, (
            r["company"], ((r.get("posting") or {}).get("title") or r.get("note") or "")[:40],
            r.get("crash", "")[:50] or last.get("status", "–"), last.get("need", ""),
            len(last.get("log") or []), sum(len(x.get("questions") or []) for x in rounds[:-1]),
        ))))
    (out / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


async def parallel_main(args: argparse.Namespace) -> int:
    """Run the employers in args.parallel groups at once, each in its own process (a home, a
    browser and a fake email of its own), then print their results in order as one run."""
    n = args.parallel
    passed = [*(["--companies", args.companies] if args.companies else []), *(["--fixtures"] if args.fixtures else []),
              *(["--pipeline"] if args.pipeline else []), *(["--fake-passwords"] if args.fake_passwords else []),
              *(["--lists", args.lists] if args.lists else []), "--role", args.role]
    children = [await asyncio.create_subprocess_exec(
        sys.executable, __file__, *passed, "--shard", f"{i}/{n}", "--out", str(args.out / f"shard-{i}"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT) for i in range(n)]
    outputs = await asyncio.gather(*(c.communicate() for c in children))
    for stdout, _ in outputs:
        sys.stdout.write(stdout.decode(errors="replace"))
    if args.pipeline:
        records = []
        for i in range(n):
            with contextlib.suppress(OSError, ValueError):
                records += json.loads((args.out / f"shard-{i}" / "pipeline.json").read_text())
        (args.out / "pipeline.json").write_text(json.dumps(records, indent=2, default=str))
        _pipeline_report(records, args.out)
    return max((c.returncode or 0) for c in children)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
