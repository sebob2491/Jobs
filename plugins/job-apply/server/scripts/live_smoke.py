"""Live smoke test against real employer career sites. Read-only by design.

    uv run python scripts/live_smoke.py --out live-report [--companies "KLA,ASM"] [--fixtures]

For each company with a `search` config in data/companies.yaml:
  1. search_company_jobs for a broad query, and keep the first result;
  2. read that posting over HTTP (ingest);
  3. open it in headless Chromium, click through "Apply" / "Apply Manually" until a
     form shows, list the form's fields, and run autofill with a FAKE profile;
  4. save a debug snapshot (and, with --fixtures, a test fixture).
Companies without a search API get a lighter check of their careers page.

Safety: JOB_APPLY_NEVER_SUBMIT=1 is forced, so nothing can be submitted. The fake
profile has no resume, so nothing is uploaded. It never clicks sign-in, account
creation, "Autofill with Resume", "Next", or third-party apply buttons (LinkedIn,
Indeed, SEEK), and LinkedIn and Indeed are never contacted.
"""

from __future__ import annotations

import argparse
import asyncio
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

os.environ["JOB_APPLY_NEVER_SUBMIT"] = "1"
os.environ["JOB_APPLY_HEADLESS"] = "1"
_HOME = Path(tempfile.mkdtemp(prefix="job-apply-live-"))
os.environ["JOB_APPLY_HOME"] = str(_HOME)

import yaml  # noqa: E402

FAKE_PROFILE = {
    "personal": {
        "first_name": "Testy", "last_name": "McTestface", "email": "testy.mctestface@example.com",
        "phone": "480-555-0199", "phone_country_code": "+1",
        "address": {"line1": "1 Test Way", "city": "Chandler", "state": "AZ", "postal_code": "85225",
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
from job_apply.search import load_companies, search_companies  # noqa: E402

QUERY_AZ = "field service | customer service engineer | customer engineer | equipment technician"  # in Arizona
QUERY_ANY = "engineer | technician"  # fallback so every company still gets a browser check
APPLY = re.compile(r"^(apply( now| for (this|the) (job|position|role))?|apply to (this )?job|i'?m interested|"
                   r"start (your |my )?application|apply manually)$", re.I)
NEVER = re.compile(r"autofill|resume|last application|submit|sign ?in|log ?in|create account|register|upload|"
                   r"linked ?in|indeed|seek|google|facebook|next|continue|save", re.I)
COMPANY_TIMEOUT = 150


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


async def check_company(company: dict[str, Any], out: Path, fixtures: bool) -> dict[str, Any]:
    rec: dict[str, Any] = {"company": company["name"], "search_config": company.get("search")}
    # The MCP tool, so the Eightfold browser fallback and tracker marking run too.
    az = await server.search_company_jobs(QUERY_AZ, companies=[company["name"]], location="AZ", limit_per_company=5)
    rec["search_az"] = search_brief(az, company["name"])
    found = az
    if not az["results"]:
        found = await search_companies(QUERY_ANY, location=None, limit=3, companies=[company])
        rec["search_any"] = search_brief(found, company["name"])
    if not found["results"]:
        return rec
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
    opened = await server.open_application(job_id=job["id"])
    if "error" in opened:
        rec["browser"] = {"error": opened["error"]}
        return rec
    steps.append({"step": "open", "url": opened["url"], "title": opened["title"], "headings": opened["headings"][:4],
                  "fields": opened["fields"], "actions": opened["actions"][:15],
                  "navigation_error": opened.get("navigation_error")})
    for _ in range(3):
        form = await server.inspect_form(include_dropdown_options=False)
        if len([f for f in form["fields"] if f["kind"] != "file"]) >= 3:
            break
        action = pick_action(form["actions"])
        if action is None:
            break
        clicked = await server.click(action["id"])
        steps.append({"step": f"click {action['text']!r}", "clicked": clicked.get("clicked"),
                      "url": clicked.get("url"), "headings": (clicked.get("headings") or [])[:4],
                      "fields": clicked.get("fields"), "blocked": clicked.get("blocked")})
    form = await server.inspect_form(include_dropdown_options=True)
    rec["form"] = {
        "url": form["url"], "ats": form["ats"], "headings": form["headings"][:6], "errors": form["errors"][:5],
        "fields": [field_brief(f) for f in form["fields"]],
        "actions": [a["text"] for a in form["actions"]][:30],
    }
    rec["steps"] = steps
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
    return rec


# Career pages whose job data comes from a call we haven't pinned down yet: record what
# the page itself requests, so the search can use it.
PROBES = {
    "Lam Research": "https://careers.lamresearch.com/careers?query=field%20service&location=Arizona",
    "Micron": "https://micron.eightfold.ai/careers?query=field%20service&domain=micron.com",
    "Infineon": "https://jobs.infineon.com/careers?query=field%20service&domain=infineon.com",
    "ASML": "https://www.asml.com/en/careers/find-your-job?query=field%20service",
    "Texas Instruments": "https://careers.ti.com/en/sites/CX/jobs?keyword=technician",
    "onsemi": "https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs?keyword=field+service",
}


async def probe_page(name: str, url: str) -> dict[str, Any]:
    await server.browser.page()
    ctx = server.browser._ctx  # noqa: SLF001 - test script reaching into the session on purpose
    assert ctx is not None
    server.browser._background = True  # noqa: SLF001
    tab = await ctx.new_page()
    seen: list[dict[str, Any]] = []

    def on_response(r: Any) -> None:
        ctype = r.headers.get("content-type", "")
        if "json" in ctype or "/api/" in r.url:
            keep = 1500 if ("recruitingCEJobRequisitions" in r.url or "pcsx/search" in r.url) else 300
            seen.append({"method": r.request.method, "status": r.status, "url": r.url[:keep], "type": ctype[:40]})

    tab.on("response", on_response)
    rec: dict[str, Any] = {"probe": name, "url": url}
    try:
        resp = await tab.goto(url, wait_until="domcontentloaded", timeout=45000)
        rec["document_status"] = resp.status if resp else None
        await tab.wait_for_timeout(12000)
        rec["title"] = await tab.title()
        rec["json_calls"] = seen[:30]
        rec["text_sample"] = re.sub(r"\s+", " ", await tab.inner_text("body"))[:500]
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    finally:
        await tab.close()
        server.browser._background = False  # noqa: SLF001
    return rec


async def check_careers_page(company: dict[str, Any]) -> dict[str, Any]:
    """Companies without a search API: can the browser read their careers page at all?"""
    rec: dict[str, Any] = {"company": company["name"], "careers_url": company.get("careers_url"), "ats": company.get("ats")}
    opened = await server.open_application(url=company["careers_url"])
    if "error" in opened:
        rec["browser"] = {"error": opened["error"]}
        return rec
    form = await server.inspect_form(include_dropdown_options=False)
    text = await server.page_text(4000)
    rec["page"] = {
        "url": form["url"], "title": opened["title"], "headings": form["headings"][:6], "ats_detected": form["ats"],
        "navigation_error": opened.get("navigation_error"),
        "search_fields": [field_brief(f) for f in form["fields"] if f["kind"] in ("text", "combobox")][:5],
        "actions": [a["text"] for a in form["actions"]][:20],
        "text_sample": re.sub(r"\s+", " ", text)[:600],
    }
    return rec


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("live-report"))
    ap.add_argument("--companies", default="", help="comma-separated names (default: all)")
    ap.add_argument("--fixtures", action="store_true", help="also write tests/fixtures/live/ fixtures")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    wanted = [n.strip().lower() for n in args.companies.split(",") if n.strip()]
    companies = [c for c in load_companies() if not wanted or any(w in c["name"].lower() for w in wanted)]

    records = []
    for company in companies:
        started = time.time()
        check = check_company if company.get("search") else None
        try:
            if check:
                rec = await asyncio.wait_for(check_company(company, args.out, args.fixtures), COMPANY_TIMEOUT)
            else:
                rec = await asyncio.wait_for(check_careers_page(company), 60)
        except Exception as e:  # noqa: BLE001
            rec = {"company": company["name"], "crash": f"{type(e).__name__}: {str(e)[:300]}",
                   "trace": traceback.format_exc()[-1500:]}
            await server.close_browser()  # start the next company with a fresh browser
        rec["seconds"] = round(time.time() - started, 1)
        records.append(rec)
        print("LIVE_RESULT " + json.dumps(rec, default=str), flush=True)

    for name, url in PROBES.items():
        if wanted and not any(w in name.lower() for w in wanted):
            continue
        try:
            probe = await asyncio.wait_for(probe_page(name, url), 90)
        except Exception as e:  # noqa: BLE001
            probe = {"probe": name, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        print("LIVE_PROBE " + json.dumps(probe, default=str), flush=True)

    await server.close_browser()
    (args.out / "report.json").write_text(json.dumps(records, indent=2, default=str))
    lines = ["| Company | AZ matches | Posting | Form fields | Autofilled | Failed | Notes |", "|---|---|---|---|---|---|---|"]
    for r in records:
        s = r.get("search_az") or {}
        p = r.get("posting") or {}
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            r["company"],
            s.get("error") or s.get("count", "–"),
            p.get("error", "")[:40] or p.get("parse_method", "–"),
            len((r.get("form") or {}).get("fields", [])) if "form" in r else "–",
            len((r.get("autofill") or {}).get("filled", [])) if "autofill" in r else "–",
            len((r.get("autofill") or {}).get("failed", [])) if "autofill" in r else "–",
            (r.get("crash") or (r.get("browser") or {}).get("error") or (r.get("page") or {}).get("title", ""))[:60],
        ))
    (args.out / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
