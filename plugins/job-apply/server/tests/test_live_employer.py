"""The live check applies at the employer it's checking (scripts/live_smoke.py's employers_postings). Run in a process
of its own: live_smoke sets the environment it runs in. The employers here are the lists' own; nothing is anyone's."""

import json
import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).parent.parent

RUN = """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, "scripts")
import live_smoke
from job_apply import server
from job_apply.postings import FetchError

ROLE = live_smoke.QUERY_AZ.split("|")[0].strip().title()
DISTRICT = {"company": "Clark County School District", "title": ROLE, "location": "Las Vegas, NV",
            "url": "https://ccsd.example.com/jobs/1"}
COUNTY = {"company": "Clark County", "title": "Road Crew Lead", "location": "Las Vegas, NV",
          "url": "https://county.example.com/jobs/2"}
ONLY_THE_OTHER = False
asked = []

async def search(query, companies=None, location=None, limit_per_company=20):
    asked.append(companies)
    # as the tool answers a search for "Clark County": the school district's too, its posting the better match
    return {"results": [dict(DISTRICT)] if ONLY_THE_OTHER else [dict(DISTRICT), dict(COUNTY)],
            "errors": {"Clark County School District": "HTTP 500 from its board"}}

async def unread(url):
    raise FetchError("not read here")

class Reached(Exception):
    pass

def add_job(**job):
    print("RESULT " + json.dumps({"added": job}, sort_keys=True))
    raise Reached

server.search_company_jobs = search
server.add_job = add_job
live_smoke.fetch_posting = unread
live_smoke.read_posting = unread

async def main():
    global ONLY_THE_OTHER
    for check in ("pipeline", "company"):
        for ONLY_THE_OTHER in (False, True):
            rec = {}
            try:
                if check == "pipeline":
                    await live_smoke.check_pipeline({"name": "Clark County"}, Path("out"), rec)
                else:
                    await live_smoke.check_company({"name": "Clark County"}, Path("out"), False, rec)
            except Reached:
                pass
            print("RESULT " + json.dumps({"check": check, "only_the_other": ONLY_THE_OTHER, "note": rec.get("note"),
                                          "search_az": rec.get("search_az"), "search_any": rec.get("search_any")},
                                         sort_keys=True))
    print("RESULT " + json.dumps({"asked": asked}))

asyncio.run(main())
"""


def test_the_check_applies_only_to_a_posting_of_the_employer_it_is_checking():
    """The NEOGOV probe (run 198), and every nightly before it: the search tool finds employers by part of a name, as
    a person names them, so a search for "Clark County" answered with Clark County School District's postings too,
    and the check applied to the best of them all under Clark County's name. Its row was the school district's Taleo
    application; the county's own NEOGOV one was never tried. Only the employer's own postings count: the other's are
    left out, with the other's search error, and an employer with none of its own has no postings found."""
    out = subprocess.run([sys.executable, "-c", RUN], cwd=SERVER, capture_output=True, text=True,
                         encoding="utf-8", timeout=180)
    lines = [json.loads(x[len("RESULT "):]) for x in out.stdout.splitlines() if x.startswith("RESULT ")]
    assert lines, out.stdout[-2000:] + out.stderr[-3000:]
    added = [x["added"] for x in lines if "added" in x]
    # the pipeline's check and the search's each add one job, the county's own (never the district's better match),
    # and none when the search found only the other employer's
    assert [(a["company"], a["url"], a["title"]) for a in added] == [
        ("Clark County", "https://county.example.com/jobs/2", "Road Crew Lead")] * 2, lines
    recs = {(x["check"], x["only_the_other"]): x for x in lines if "check" in x}
    assert recs[("pipeline", True)]["note"] == "no postings found"  # (the district's search error isn't the county's)
    assert recs[("pipeline", False)]["note"] is None
    brief = recs[("company", False)]["search_az"]
    assert brief["count"] == 1 and brief["error"] is None
    assert [s["title"] for s in brief["sample"]] == ["Road Crew Lead"]
    none = recs[("company", True)]
    assert none["search_az"]["count"] == 0 and none["search_any"]["count"] == 0
    # asked for by its name each time, as before (the tool's own fallbacks and tracker marking still run)
    assert lines[-1]["asked"] and all(a == ["Clark County"] for a in lines[-1]["asked"])
