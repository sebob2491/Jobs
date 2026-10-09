"""search_company_jobs against canned responses shaped like each ATS's public API.
The shapes follow what the live smoke test (scripts/live_smoke.py) saw on real sites."""

import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from job_apply.postings import fetch_posting
import job_apply.search as search_module
from job_apply.search import (_workday_location_facets, alternatives, eightfold_page_url, location_matches,
                              location_terms, search_companies, title_matches)

COMPANIES = [
    {"name": "Workday Co", "search": {"workday": "https://wdco.wd1.myworkdayjobs.com/External"}},
    {"name": "Greenhouse Co", "search": {"greenhouse": "ghco"}},
    {"name": "Lever Co", "search": {"lever": "leverco"}},
    {"name": "Eightfold Co", "search": {"eightfold": {"host": "careers.efco.com", "domain": "efco.com"}}},
    {"name": "SR Co", "search": {"smartrecruiters": "SRCO1"}},
    {"name": "Oracle Co", "search": {"oracle": {"host": "abcd.fa.us2.oraclecloud.com", "site": "CX_1"}}},
    {"name": "Browser Co", "careers_url": "https://careers.browserco.com"},
    {"name": "Broken Co", "search": {"greenhouse": "broken"}},
    {"name": "Odd Lever Co", "search": {"lever": "oddco"}},
    {"name": "Workday Site Co", "search": {"workday": "https://wd1.myworkdaysite.com/recruiting/wsco/WS_Careers"}},
    {"name": "AS Co", "search": {"applicantstack": "asco"}},
    {"name": "iCIMS Co", "search": {"icims": "careers-icco"}},
    {"name": "Paycom Co", "search": {"paycom": "PAYCOMKEY"}},
    {"name": "UKG Co", "search": {"ukg": "https://recruiting2.ultipro.com/UKGCO/JobBoard/b0a4d/"}},
]
# SCREEN SPE USA's board: one table of every opening, title and location
AS_BOARD = """<h1>Job Openings</h1><table class="table"><thead><tr><th>Job Title</th><th>Location</th></tr></thead><tbody>
<tr><td><a href="/x/detail/a2ejxq3cpz4b">Field Service Engineer - Chandler</a></td><td>Chandler, AZ</td></tr>
<tr><td><a href="/x/detail/a2ejxq3c5xpg">Field Service Engineer - Austin</a></td><td>Austin, TX</td></tr>
<tr><td><a href="/x/detail/a2ejxq3tau2q">Administrative Coordinator &amp; Translator - Hillsboro</a></td><td>Hillsboro, OR</td></tr>
</tbody></table>"""
# Daifuku's iCIMS portal: the search results inside its frame (in_iframe=1)
ICIMS_ROW = """<div class="row">
 <div class="col-xs-6 header left"> <span class="sr-only field-label">Job Locations</span> <span> {where}</span> </div>
 <div class="col-xs-6 header right"> <span class="sr-only field-label">Posted Date</span>
  <span title="9/24/2026 6:18 PM"> 2 weeks ago<span class="sr-only">(9/24/2026 6:18 PM)</span></span> </div>
 <div class="col-xs-12 title"> <a href="https://careers-icco.icims.com/jobs/{id}/{slug}/job?in_iframe=1"
   class="iCIMS_Anchor" title="{id} - {title}"><span class="sr-only field-label">External Title</span> <h3>{title}</h3></a> </div>
</div>"""
ICIMS_PAGE = ('<div class="iCIMS_JobsTable">'
              + ICIMS_ROW.format(where="US-AZ-Chandler", id=19224, slug="field-service-engineer-1",
                                 title="Field Service Engineer 1")
              + ICIMS_ROW.format(where="US-MI-Novi", id=21882, slug="apus-controls-engineer-iii",
                                 title="Controls Engineer III")
              + '</div><a href="https://careers-icco.icims.com/jobs/intro?in_iframe=1">Welcome page</a>')
seen: list[httpx.Request] = []

WD_FACETS = [{"facetParameter": "locationMainGroup", "descriptor": "Locations", "values": [
    {"facetParameter": "locations", "descriptor": "Locations", "values": [
        {"descriptor": "Chandler, AZ", "id": "loc-chandler", "count": 25},
        {"descriptor": "Hillsboro, OR", "id": "loc-hillsboro", "count": 15}]}]},
    {"facetParameter": "jobFamilyGroup", "descriptor": "Job Category", "values": [
        {"descriptor": "Arizona Operations", "id": "not-a-location", "count": 3}]}]


def workday(body: dict) -> httpx.Response:
    assert body["limit"] <= 20 and "searchText" in body
    offset, facets = body["offset"], body["appliedFacets"]
    if facets:
        assert facets == {"locations": ["loc-chandler"]}
        count, where = 25, "Chandler, AZ"
    else:
        count, where = 40, None
    page = [
        {"title": f"Field Service Engineer {offset + i}", "externalPath": f"/job/X/FSE_R{offset + i}",
         "locationsText": where or ("2 Locations" if i % 5 == 4 else "Chandler, AZ" if i % 2 == 0 else "Hillsboro, OR"),
         "postedOn": "Posted Today",
         "bulletFields": [f"R{offset + i}"]}
        for i in range(min(20, max(0, count - offset)))
    ]
    out = {"jobPostings": page}
    if offset == 0:  # real Workday sends total (and facets) on the first page only
        out["total"] = count
        if not facets:
            out["facets"] = WD_FACETS
    return httpx.Response(200, json=out)


def handler(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    url = str(request.url)
    if url.startswith("https://wdco.wd1.myworkdayjobs.com/wday/cxs/wdco/External/jobs"):
        return workday(json.loads(request.content))
    if url == "https://boards-api.greenhouse.io/v1/boards/ghco/jobs":
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Field Service Engineer II", "absolute_url": "https://www.ghco.com/careers?gh_jid=1",
             "location": {"name": "US > Arizona > Phoenix"}, "updated_at": "2026-10-01T00:00:00Z"},
            {"id": 2, "title": "Accountant", "absolute_url": "https://www.ghco.com/careers?gh_jid=2",
             "location": {"name": "Phoenix, AZ"}},
            {"id": 3, "title": "Equipment Engineering Technician",
             "absolute_url": "https://job-boards.greenhouse.io/ghco/jobs/3", "location": {"name": "Multiple Locations"}},
            {"id": 4, "title": "Field Service Engineer", "absolute_url": "https://job-boards.greenhouse.io/ghco/jobs/4",
             "location": {"name": "Remote - US"}},
        ]})
    if url.startswith("https://api.lever.co/v0/postings/leverco"):
        return httpx.Response(200, json=[
            {"id": "abc", "text": "Field Service Engineer", "hostedUrl": "https://jobs.lever.co/leverco/abc",
             "categories": {"location": "Tempe, AZ"}, "createdAt": 1790000000000},
            {"id": "def", "text": "Field Service Engineer", "hostedUrl": "https://jobs.lever.co/leverco/def",
             "categories": {"location": "Austin, TX"}},
        ])
    if url.startswith("https://api.lever.co/v0/postings/oddco"):
        return httpx.Response(200, json={"ok": False, "error": "Document not found"})
    if url.startswith("https://careers.efco.com/api/pcsx/search"):
        assert request.headers["Referer"] == "https://careers.efco.com/careers"
        start = int(request.url.params["start"])
        positions = [
            {"id": 100 + start + i, "name": "Field Service Engineer 2", "locations": ["Chandler, AZ, United States"],
             "positionUrl": f"/careers/job/{100 + start + i}", "postedTs": 1790000000}
            for i in range(10)
        ] if start < 10 else []
        return httpx.Response(200, json={"status": 200, "data": {"count": 10, "positions": positions}})
    if url.startswith("https://api.smartrecruiters.com/v1/companies/SRCO1/postings"):
        return httpx.Response(200, json={"totalFound": 2, "content": [
            {"id": "744000001", "name": "Regional Lead - Installs", "refNumber": "REF1", "releasedDate": "2026-09-30T10:00:00Z",
             "location": {"city": "Chandler", "region": "AZ", "country": "us"}},
            {"id": "744000002", "name": "Install Engineer", "location": {"city": "Phoenix", "region": "AZ", "country": None}}]})
    if url.startswith("https://abcd.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"):
        # exactly the request the career site makes, or Oracle ignores the keyword
        assert "finder=findReqs;siteNumber=CX_1,facetsList=WORK_LOCATIONS%3B" in url
        assert ("keyword=%22field%20service%22,sortBy=RELEVANCY" in url
                or "keyword=%22equipment%20engineer%22,sortBy=RELEVANCY" in url) and "offset" not in url
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 3, "requisitionList": [
            {"Id": "25011541", "Title": "Equipment Engineer", "PrimaryLocation": "Phoenix, AZ, United States",
             "PostedDate": "2026-09-29", "workLocation": [{"TownOrCity": "Phoenix", "Region2": "AZ", "Country": "US"}]},
            # only a country as the primary location; the site is in the expanded locations
            {"Id": "25011323", "Title": "Field Service Technician", "PrimaryLocation": "United States",
             "secondaryLocations": [{"Name": "Richardson, TX, United States", "CountryCode": "US"}]},
            {"Id": "25011777", "Title": "Field Service Technician", "PrimaryLocation": "Austin, TX, United States",
             "otherWorkLocations": [{"LocationName": "TUC-1", "TownOrCity": "Tucson", "Region2": "AZ", "Country": "US"}]},
        ]}]})
    if url.startswith("https://wd1.myworkdaysite.com/wday/cxs/wsco/WS_Careers/jobs"):
        return httpx.Response(200, json={"total": 1, "jobPostings": [
            {"title": "Field Service Engineer 1", "externalPath": "/job/Phoenix-AZ/Field-Service-Engineer-1_R1",
             "locationsText": "Phoenix, AZ", "postedOn": "Posted Today", "bulletFields": ["R1"]}]})
    if url == "https://asco.applicantstack.com/x/openings":
        return httpx.Response(200, text=AS_BOARD)
    if url == "https://boards-api.greenhouse.io/v1/boards/broken/jobs":
        return httpx.Response(404, json={"status": 404})
    raise AssertionError(f"unexpected request {request.method} {url}")


def run_search(**kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await search_companies(client=client, companies=COMPANIES, **kw)
    return asyncio.run(go())


def test_helpers():
    assert alternatives("field service | equipment engineer") == ["field service", "equipment engineer"]
    assert alternatives("a OR b") == ["a", "b"]
    assert title_matches("Field Service Engineer II", "field service")
    assert title_matches("Equipment Engineering Technician", "field service | equipment engineer")
    assert not title_matches("Accountant", "field service | equipment engineer")

    az = location_terms("AZ")
    assert az[:2] == ["az", "arizona"] and {"phoenix", "chandler", "tempe"} <= set(az)  # metro cities count too
    assert location_terms("Phoenix|Chandler") == ["phoenix", "chandler", "in:az"]  # another state's Phoenix isn't it
    assert location_terms("arizona")[:2] == ["arizona", "az"]
    assert location_matches("Chandler (Office)", az) is True  # NXP lists only the city
    assert location_matches("AZ - Chandler", az) is True
    assert location_matches("US-AZ-Chandler", az) is True
    assert location_matches("US > Arizona > Phoenix", az) is True
    assert location_matches("Hillsboro, OR", az) is False
    for broad in ("3 Locations", "", "Remote", "Remote - US", "United States (Remote)", "United States", "USA"):
        assert location_matches(broad, az) is None, broad
    assert location_matches("Remote - Arizona", az) is True
    assert location_matches("Remote, Japan", az) is False
    assert location_matches("Remote - Staffordshire, United Kingdom", az) is False
    # a metro city's name in another state isn't the area
    for elsewhere in ("Peoria, IL", "Glendale, California", "Chandler, TX", "Mesa, CO", "Surprise, NE"):
        assert location_matches(elsewhere, az) is False, elsewhere
    for here in ("Peoria, AZ", "Glendale, Arizona", "Austin, TX; Chandler, AZ", "PHOENIX AZ", "Chandler (AZ)"):
        assert location_matches(here, az) is True, here
    assert eightfold_page_url({"host": "careers.x.com", "domain": "x.com"}, "field service | equipment", "AZ") == \
        "https://careers.x.com/careers?query=field+service+equipment&domain=x.com&location=Arizona"



def test_hr_and_human_resources_are_the_same_in_a_title():
    """HR searches on the Phoenix list (Oct 2026): "HR Business Partner" is a human resources
    job, and "Human Resources Generalist" an HR generalist's. "HR" is a whole word: a nurse's
    "36 Hrs" isn't HR."""
    assert title_matches("HR Business Partner", "human resources | recruiter")
    assert title_matches("Human Resources Generalist", "hr generalist")
    assert title_matches("Sr. Human Resource Coordinator", "hr coordinator")
    assert title_matches("HR/Payroll Specialist", "human resources")
    assert title_matches("Human Resources Manager", "resources")  # each word still counts alone
    assert title_matches("Human Resource Planner", "resource planner")
    assert not title_matches("Housekeeping Associate", "human resources | hr generalist")
    assert not title_matches("Hospital Unit Clerk", "hr")
    assert not title_matches("Registered Nurse - 36 Hrs Nights", "human resources | hr")
    assert not title_matches("Pharmacy Tech 32 Hrs/Wk", "hr | recruiter")
    assert not title_matches("Registered Nurse - ICU - 12 Hr Nights", "human resources | hr generalist")
    assert not title_matches("Security Officer 24 Hr Shift", "hr")

def test_search_all_backends():
    seen.clear()
    out = run_search(query="field service | equipment engineer", location="AZ", limit=20)
    by_company: dict[str, list] = {}
    for r in out["results"]:
        by_company.setdefault(r["company"], []).append(r)

    wd = by_company["Workday Co"]
    assert len(wd) == 20 and all(r["location"] == "Chandler, AZ" for r in wd)  # Workday's own AZ filter applied
    wd_calls = [json.loads(r.content) for r in seen if "myworkdayjobs" in str(r.url)]
    # per alternative: one unfiltered page to learn the location filters, then the filtered pages (25 jobs = 2 pages)
    assert [c["appliedFacets"] != {} for c in wd_calls] == [False, True, True, False, True, True]
    assert [c["offset"] for c in wd_calls] == [0, 0, 20, 0, 0, 20]

    gh = {r["title"]: r for r in by_company["Greenhouse Co"]}
    assert set(gh) == {"Field Service Engineer II", "Equipment Engineering Technician", "Field Service Engineer"}
    assert gh["Field Service Engineer II"]["url"] == "https://job-boards.greenhouse.io/ghco/jobs/1"
    assert gh["Field Service Engineer II"]["company_url"] == "https://www.ghco.com/careers?gh_jid=1"
    assert "company_url" not in gh["Equipment Engineering Technician"]
    assert gh["Equipment Engineering Technician"]["notes"] == ["location given as 'Multiple Locations'; check the posting"]
    assert gh["Field Service Engineer"]["notes"] == ["location given as 'Remote - US'; check the posting"]
    assert sum("greenhouse" in str(r.url) and "ghco" in str(r.url) for r in seen) == 1  # whole board, fetched once

    assert [r["url"] for r in by_company["Lever Co"]] == ["https://jobs.lever.co/leverco/abc"]
    assert by_company["Lever Co"][0]["posted"] == "2026-09-21"
    ef = by_company["Eightfold Co"]
    assert len(ef) == 10 and ef[0]["url"].startswith("https://careers.efco.com/careers/job/1")
    sr = {r["url"]: r for r in by_company["SR Co"]}
    assert sr["https://jobs.smartrecruiters.com/SRCO1/744000001"]["location"] == "Chandler, AZ, US"
    assert sr["https://jobs.smartrecruiters.com/SRCO1/744000002"]["location"] == "Phoenix, AZ"  # country: null
    orc = by_company["Oracle Co"]
    assert orc[0]["url"] == "https://abcd.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/25011541"
    # "United States" in Richardson, TX is dropped; a Texas job that's also in Tucson is kept
    assert [r["location"] for r in orc] == ["Phoenix, AZ, United States", "Austin, TX, United States; Tucson, AZ, US"]

    # myworkdaysite.com postings keep the tenant in their address (Onto Innovation)
    assert by_company["Workday Site Co"][0]["url"] == \
        "https://wd1.myworkdaysite.com/recruiting/wsco/WS_Careers/job/Phoenix-AZ/Field-Service-Engineer-1_R1"
    assert [(r["title"], r["location"], r["url"]) for r in by_company["AS Co"]] == [
        ("Field Service Engineer - Chandler", "Chandler, AZ", "https://asco.applicantstack.com/x/detail/a2ejxq3cpz4b")]
    assert sum("applicantstack" in str(r.url) for r in seen) == 1  # whole board, fetched once
    # iCIMS answers plain requests with HTTP 405: it's left for the browser
    assert "iCIMS Co" not in by_company and not any("icims" in str(r.url) for r in seen)
    assert {"company": "iCIMS Co", "kind": "icims", "config": "careers-icco"} in out["needs_browser"]
    assert {"company": "Paycom Co", "kind": "paycom", "config": "PAYCOMKEY"} in out["needs_browser"]
    assert {"company": "UKG Co", "kind": "ukg", "config": "https://recruiting2.ultipro.com/UKGCO/JobBoard/b0a4d/"} \
        in out["needs_browser"]
    assert not any("paycom" in str(r.url) for r in seen)

    assert out["browser_only"] == [{"company": "Browser Co", "careers_url": "https://careers.browserco.com"}]
    assert set(out["errors"]) == {"Broken Co", "Odd Lever Co"}  # each fails alone; the rest still return
    assert out["errors"]["Broken Co"] == "SearchError: HTTP 404 from https://boards-api.greenhouse.io/v1/boards/broken/jobs"


def test_workday_area_without_matches_returns_nothing():
    # the site's location filter has nothing in Texas, so its "2 Locations" jobs aren't there either
    out = run_search(query="field service", names=["workday"], location="TX")
    assert out["results"] == [] and out["errors"] == {}


def test_workday_location_filter_outcomes():
    tx, az = location_terms("TX"), location_terms("AZ")
    assert _workday_location_facets(WD_FACETS, az) == {"locations": ["loc-chandler"]}
    assert _workday_location_facets(WD_FACETS, tx) == {}  # has places, none in the area
    no_places = [{"facetParameter": "jobFamilyGroup", "descriptor": "Job Category",
                  "values": [{"descriptor": "Engineering", "id": "eng", "count": 9}]}]
    assert _workday_location_facets(no_places, tx) is None  # can't tell, so "N Locations" jobs stay


def test_company_filter_and_anywhere():
    out = run_search(query="field service", names=["lever co"], location=None)
    assert {r["company"] for r in out["results"]} == {"Lever Co"}
    assert len(out["results"]) == 2  # no location filter


def test_posting_apis():
    def posting_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://api.smartrecruiters.com/v1/companies/ASML1/postings/744000001":
            return httpx.Response(200, json={
                "name": "Regional Lead - Installs", "company": {"name": "ASML"}, "refNumber": "REF1",
                "location": {"city": "Chandler", "region": "AZ", "country": "us"}, "releasedDate": "2026-09-30T10:00:00Z",
                "applyUrl": "https://jobs.smartrecruiters.com/oneclick-ui/company/ASML1/publication/x",
                "typeOfEmployment": {"label": "Full-time"},
                "jobAd": {"sections": {"jobDescription": {"title": "Job Description", "text": "<p>Lead EUV installs.</p>"},
                                       "qualifications": {"title": "Qualifications", "text": "<ul><li>5 years</li></ul>"}}}})
        if url.startswith("https://hctz.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"):
            assert "finder=ById;Id=%222506217%22,siteNumber=CX_1001" in url
            return httpx.Response(200, json={"items": [{
                "Id": "2506217", "Title": "Equipment Technician", "PrimaryLocation": "Phoenix, AZ, United States",
                "ExternalDescriptionStr": "<p>Maintain probers.</p>", "ExternalQualificationsStr": "<ul><li>AAS</li></ul>",
                "ExternalPostedStartDate": "2026-09-30"}]})
        if url == "https://careers.example-semi.com/en/sites/CX_1001/job/2506217":  # the company's own address
            return httpx.Response(200, text='<html><head><link rel="preconnect" href="https://hctz.fa.us2.oraclecloud.com">'
                                            '</head><body><div id="app"></div></body></html>')
        if url.startswith("https://api.lever.co/v0/postings/leverco/0b1c2d3e-0000-1111-2222-333344445555"):
            return httpx.Response(200, json={"text": "Field Service Engineer", "categories": {"location": "Phoenix, AZ"},
                                              "descriptionPlain": "Service our tools in the field.",
                                              "lists": [{"text": "Requirements", "content": "<li>3 years</li>"}],
                                              "additionalPlain": "This role requires U.S. citizenship (ITAR)."})
        if url.startswith("https://boards-api.greenhouse.io/v1/boards/asm/jobs/4885531101"):
            return httpx.Response(200, json={"title": "Digital Solutions Engineer", "company_name": "ASM",
                                              "absolute_url": "https://www.asm.com/open-vacancies/?gh_jid=4885531101",
                                              "location": {"name": "US > Arizona > Phoenix"}, "content": "&lt;p&gt;Hi&lt;/p&gt;"})
        raise AssertionError(url)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(posting_handler)) as client:
            sr = await fetch_posting("https://jobs.smartrecruiters.com/ASML1/744000001-regional-lead", client=client)
            gh = await fetch_posting("https://job-boards.greenhouse.io/asm/jobs/4885531101", client=client)
            orc = await fetch_posting(
                "https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/2506217", client=client)
            # a company's own address for its Oracle site: the API call goes to where it points
            own = await fetch_posting("https://careers.example-semi.com/en/sites/CX_1001/job/2506217", client=client)
            lever = await fetch_posting("https://jobs.lever.co/leverco/0b1c2d3e-0000-1111-2222-333344445555", client=client)
            return sr, gh, orc, own, lever

    sr, gh, orc, own, lever = asyncio.run(go())
    assert own.parse_method == "oracle-api" and own.title == "Equipment Technician" and own.ats == "oracle_hcm"
    assert "U.S. citizenship (ITAR)" in lever.description and "- 3 years" in lever.description
    assert orc.parse_method == "oracle-api" and orc.title == "Equipment Technician"
    assert "Maintain probers." in orc.description and "- AAS" in orc.description and orc.ats == "oracle_hcm"
    assert sr.parse_method == "smartrecruiters-api" and sr.company == "ASML" and sr.location == "Chandler, AZ, US"
    assert "Lead EUV installs." in sr.description and "- 5 years" in sr.description
    assert sr.ats == "smartrecruiters"
    # the hosted Greenhouse page always has the form, unlike the employer's embedding page
    assert gh.apply_url == "https://job-boards.greenhouse.io/asm/jobs/4885531101"


def test_tool_marks_tracked_jobs(srv, monkeypatch):
    import job_apply.server as server

    async def fake(query, names, location, limit, **kw):
        return {"results": [{"company": "Lever Co", "title": "FSE", "url": "https://jobs.lever.co/leverco/abc"}],
                "errors": {}, "browser_only": []}

    monkeypatch.setattr(server, "search_companies", fake)
    srv.add_job(url="https://jobs.lever.co/leverco/abc", title="FSE", company="Lever Co")
    out = asyncio.run(server.search_company_jobs("field service"))
    assert out["count"] == 1 and out["results"][0]["tracked"]["status"] == "saved"


def test_tool_falls_back_to_browser_for_eightfold(srv, monkeypatch):
    import job_apply.server as server

    async def fake(query, names, location, limit, **kw):
        return {"results": [], "errors": {"Lam Research": "SearchError: HTTP 403 from https://careers.lamresearch.com/api"},
                "browser_only": []}

    pages = []

    async def capture(url, part):
        pages.append((url, part))
        return {"count": 2, "positions": [
            {"id": 1, "name": "Field Service Engineer 2", "locations": ["Chandler, AZ, US"],
             "canonicalPositionUrl": "https://careers.lamresearch.com/careers/job/1"},
            {"id": 2, "name": "Payroll Specialist", "locations": ["Chandler, AZ, US"]}]}

    monkeypatch.setattr(server, "search_companies", fake)
    monkeypatch.setattr(server.browser, "capture_json", capture)
    out = asyncio.run(server.search_company_jobs("field service", location="AZ"))
    assert out["errors"] == {}
    # ranked, not filtered: Eightfold's own relevance already chose these
    assert [(r["title"], r["title_match"]) for r in out["results"]] == \
        [("Field Service Engineer 2", True), ("Payroll Specialist", False)]
    assert pages == [("https://careers.lamresearch.com/careers?query=field+service&domain=lamresearch.com&location=Arizona",
                      "/api/pcsx/search")]


def test_parse_eightfold_shapes():
    from job_apply.search import parse_eightfold

    v2 = {"count": 1, "positions": [{"id": 7, "name": "FSE", "locations": ["Chandler, AZ"],
                                     "canonicalPositionUrl": "https://c.x.com/careers/job/7"}]}
    pcsx = {"data": {"count": 1, "positions": [{"id": 8, "name": "FSE 2", "locations": [{"name": "Phoenix, AZ"}],
                                                "positionUrl": "/careers/job/8"}]}}
    assert [(x.title, x.url, x.location) for x in parse_eightfold(v2, "c.x.com")] == \
        [("FSE", "https://c.x.com/careers/job/7", "Chandler, AZ")]
    assert [(x.title, x.url, x.location) for x in parse_eightfold(pcsx, "c.x.com")] == \
        [("FSE 2", "https://c.x.com/careers/job/8", "Phoenix, AZ")]
    assert parse_eightfold({"unexpected": True}, "c.x.com") == []


def test_passing_server_errors_are_retried_and_one_failure_keeps_the_rest(monkeypatch):
    monkeypatch.setattr(search_module, "RETRY_DELAY", 0)
    tries: list[str] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        tries.append(body["searchText"])
        if body["searchText"] == "customer engineer":
            return httpx.Response(502)  # down for this wording the whole time
        if tries.count("field service") == 1:
            return httpx.Response(503)  # a hiccup on the very first request
        return workday(body)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
            return await search_companies("field service | customer engineer", location="AZ", client=client,
                                          companies=[COMPANIES[0]])
    out = asyncio.run(go())
    assert len(out["results"]) == 20 and all(r["location"] == "Chandler, AZ" for r in out["results"])
    assert tries.count("customer engineer") == 3
    assert out["errors"]["Workday Co"].startswith("SearchError: HTTP 502")
    assert "1 of 2 searches failed" in out["errors"]["Workday Co"]


# The shape of asml.com's Sitecore Discover answer (live probe, Oct 2026; descriptions trimmed).
ASML_ANSWER = {"widgets": [{"rfk_id": "asml_job_search", "type": "content_grid", "content": [
    {"description": "<p>Install and repair EUV systems.</p>", "id": "J-00327683", "job_city": "Hefei",
     "job_country": "China", "job_date_posted": "2025-09-25T00:00:00", "job_degrees": ["Bachelor", "Master"],
     "job_id": "J-00327683", "job_location": "Hefei, China", "job_state": "AH", "job_teams": ["Customer Support"],
     "job_type": "Fix", "name": "Field Service Engineer", "type": "job_detail_page",
     "url": "https://www.asml.com/en/careers/find-your-job/field-service-engineer-j00327683"},
    {"id": "J-00329630", "job_city": "Phoenix", "job_country": "US", "job_date_posted": "2026-09-14T00:00:00",
     "job_id": "J-00329630", "job_location": "Phoenix, AZ, US", "job_state": "AZ", "job_type": "Fix",
     "name": "Field Service Engineer - EUV", "type": "job_detail_page",
     "url": "https://www.asml.com/en/careers/find-your-job/field-service-engineer-euv-j00329630"},
    {"name": "Life at ASML", "type": "article", "url": "https://www.asml.com/en/careers/life"},
]}]}


def test_asml_sitecore_answer_and_request():
    from job_apply.search import parse_sitecore, sitecore_page_url, sitecore_rewrite, sitecore_wants

    jobs = parse_sitecore(ASML_ANSWER)
    assert [(j.title, j.location, j.posted, j.external_id) for j in jobs] == [
        ("Field Service Engineer", "Hefei, China", "2025-09-25", "J-00327683"),
        ("Field Service Engineer - EUV", "Phoenix, AZ, US", "2026-09-14", "J-00329630"),
    ]  # the article isn't a job
    facets_only = {"widget": {"items": [{"rfk_id": "asml_job_search", "search": {"limit": 25, "facet": {"all": True}}}]}}
    keyword = {"widget": {"items": [{"rfk_id": "asml_job_search", "search": {
        "limit": 25, "offset": 0, "query": {"keyphrase": "field service", "operator": "and"}}}]}}
    assert not sitecore_wants(facets_only) and sitecore_wants(keyword)
    assert sitecore_rewrite(facets_only) is None  # left alone
    assert sitecore_rewrite(keyword)["widget"]["items"][0]["search"]["limit"] == 100
    assert sitecore_page_url({"url": "https://www.asml.com/en/careers/find-your-job?query={query}"}, "field service") == \
        "https://www.asml.com/en/careers/find-your-job?query=field%20service"

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await search_companies("field service", client=client, location="AZ", companies=[
                {"name": "ASML", "search": {"sitecore": {"url": "https://www.asml.com/f?query={query}"}}}])
    out = asyncio.run(go())
    assert out["results"] == [] and out["needs_browser"] == [
        {"company": "ASML", "kind": "sitecore", "config": {"url": "https://www.asml.com/f?query={query}"}}]


def test_search_tool_runs_asml_in_the_browser(srv, monkeypatch):
    calls = []

    async def fake_capture(url, url_part, timeout=25000, want=None, rewrite=None):
        calls.append((url, url_part, want is not None, rewrite is not None))
        return ASML_ANSWER

    monkeypatch.setattr(srv.browser, "capture_json", fake_capture)
    monkeypatch.setattr(srv, "load_companies", lambda: [
        {"name": "ASML", "search": {"sitecore": {"url": "https://www.asml.com/en/careers/find-your-job?query={query}"}}}])
    out = asyncio.run(srv.search_company_jobs("field service | customer engineer", companies=["ASML"], location="AZ"))
    assert [r["title"] for r in out["results"]] == ["Field Service Engineer - EUV"]  # Hefei is filtered out
    assert out["results"][0]["company"] == "ASML" and "needs_browser" not in out
    assert [c[0] for c in calls] == ["https://www.asml.com/en/careers/find-your-job?query=field%20service",
                                     "https://www.asml.com/en/careers/find-your-job?query=customer%20engineer"]
    assert all(c[1] == "/discover/v2/" and c[2] and c[3] for c in calls)


def test_sitecore_search_pages_through_results():
    from job_apply.search import Listing, sitecore_search, sitecore_total

    page_body = {"widget": {"items": [{"rfk_id": "asml_job_search", "search": {
        "limit": 25, "offset": 0, "query": {"keyphrase": "field service"}}}]}}
    offsets = []

    def answer(start, count, total):
        return {"widgets": [{"rfk_id": "asml_job_search", "total_item": total, "content": [
            {"name": f"Field Service Engineer {i}", "type": "job_detail_page", "job_location": "Phoenix, AZ, US",
             "url": f"https://www.asml.com/en/careers/find-your-job/fse-{i}"} for i in range(start, start + count)]}]}

    async def capture(url, url_part, timeout=25000, want=None, rewrite=None):
        sent = rewrite(json.loads(json.dumps(page_body)))  # what the page's own request becomes
        offsets.append(sent["widget"]["items"][0]["search"]["offset"])
        assert sent["widget"]["items"][0]["search"]["limit"] == 100
        start = offsets[-1]
        if start >= 200:
            raise AssertionError("asked past the end")
        return answer(start, min(100, 150 - start), 150)

    found: list[Listing] = []
    asyncio.run(sitecore_search(capture, {"url": "https://www.asml.com/f?query={query}"}, "field service", found))
    assert offsets == [0, 100] and len(found) == 150 and sitecore_total(answer(0, 1, 150)) == 150

    async def broken_second_page(url, url_part, timeout=25000, want=None, rewrite=None):
        offset = rewrite(json.loads(json.dumps(page_body)))["widget"]["items"][0]["search"]["offset"]
        if offset:
            raise TimeoutError("page 2 never answered")
        return answer(0, 100, 150)

    kept: list[Listing] = []
    with pytest.raises(TimeoutError):
        asyncio.run(sitecore_search(broken_second_page, {"url": "https://www.asml.com/f?query={query}"}, "x", kept))
    assert len(kept) == 100  # the first page survives the failure

    no_total = {"widgets": [{"content": answer(0, 3, 3)["widgets"][0]["content"]}]}
    calls = []

    async def short(url, url_part, timeout=25000, want=None, rewrite=None):
        calls.append(url)
        return no_total
    asyncio.run(sitecore_search(short, {"url": "https://www.asml.com/f?query={query}"}, "x", []))
    assert len(calls) == 1  # fewer than a full page: that was all of them


def test_icims_board_is_read_in_the_browser(srv, monkeypatch):
    """Daifuku's portal turns away plain requests; its search page is read in a background
    tab, where the openings sit inside the portal's frame."""
    from job_apply.search import icims_page_url

    pages = []

    async def fake_frames_html(url, inner=None):
        pages.append((url, inner))
        return ['<html><body><iframe id="icims_content_iframe"></iframe></body></html>', ICIMS_PAGE]

    monkeypatch.setattr(srv.browser, "frames_html", fake_frames_html)
    out = asyncio.run(srv.search_company_jobs("field service", companies=["Daifuku"], location="AZ"))
    assert [(r["title"], r["location"], r["posted"], r["url"]) for r in out["results"]] == [
        ("Field Service Engineer 1", "US-AZ-Chandler", "2026-09-24",
         "https://careers-icco.icims.com/jobs/19224/field-service-engineer-1/job")]  # Novi, MI left out
    # the portal's own Arizona filter, and read once its openings' frame is in
    assert pages == [(icims_page_url("careers-daifuku-america", "field service", state="AZ"), "#icims_content_iframe")]
    assert pages[0][0] == ("https://careers-daifuku-america.icims.com/jobs/search?ss=1&searchKeyword=field+service"
                           "&in_iframe=1&searchLocation=-12784-")
    assert not out["errors"]
    pages.clear()
    asyncio.run(srv.search_company_jobs("field service", companies=["Daifuku"], location=None))
    assert pages == [("https://careers-daifuku-america.icims.com/jobs/search?ss=1&searchKeyword=field+service"
                      "&in_iframe=1", "#icims_content_iframe")]  # anywhere: no filter


def test_an_icims_search_is_filtered_to_one_state_only():
    """A national portal's Arizona openings are a page or two with the portal's own state filter
    (Aerotek's recruiter openings: 3 on one page, not 6 pages of 20, live). Places in several
    states, or a place that could be remote, aren't filtered: the filter would drop some."""
    from job_apply.search import icims_page_url, icims_state, location_terms

    assert icims_state(location_terms("AZ")) == icims_state(location_terms("Arizona")) == "AZ"
    assert icims_state(location_terms("Phoenix|Chandler")) == "AZ"  # Arizona's cities
    assert icims_state(location_terms("Tempe, AZ 85281")) == "AZ"
    assert icims_state(location_terms("Austin, TX")) == "TX"
    assert icims_state(location_terms("IN")) == "IN"  # "in" and "or" are broad words too, but here states
    assert icims_state(location_terms("Portland, OR")) == "OR"
    for anywhere in (None, "", "AZ|TX", "Phoenix|Remote", "Remote", "United States", "Phoenix|Austin"):
        assert icims_state(location_terms(anywhere)) is None, anywhere
    assert "searchLocation=-12827-" in icims_page_url("careers-x", "recruiter", 2, "TX")
    assert "searchLocation" not in icims_page_url("careers-x", "recruiter", 0, None)


def test_a_board_that_is_down_is_said_to_be_down(srv, monkeypatch):
    """Daifuku's iCIMS board answered HTTP 521 (live, Oct 2026): no openings would be wrong."""
    from job_apply.browser import SiteDown

    async def down(url, inner=None):
        raise SiteDown("careers-daifuku-america.icims.com is down right now (HTTP 521); try again later")

    monkeypatch.setattr(srv.browser, "frames_html", down)
    out = asyncio.run(srv.search_company_jobs("field service", companies=["Daifuku"], location="AZ"))
    assert out["results"] == []
    assert out["errors"] == {"Daifuku America": "careers-daifuku-america.icims.com is down right now (HTTP 521); "
                                                "try again later"}


# Ebara's Paycom board: the page's own search call (10 at a time) and its answer
PAYCOM_PAGE_BODY = {"skip": 0, "take": 10, "filtersForQuery": {
    "distanceFrom": 0, "workEnvironments": [], "positionTypes": [], "educationLevels": [], "categories": [],
    "travelTypes": [], "shiftTypes": [], "otherFilters": [], "keywordSearchText": "", "location": "", "sortOption": ""}}


def paycom_answer(previews, total=None):
    return {"jobPostingPreviews": previews, "jobPostingPreviewsCount": len(previews) if total is None else total}


def paycom_job(job_id, title, where, remote=""):
    return {"jobId": job_id, "jobTitle": title, "positionType": "Full Time", "remoteType": remote,
            "locations": where, "description": "POSITION SUMMARY\r\nUnder direct supervision, ...",
            "postedOn": "", "isHotJob": False}


def test_paycom_board_is_read_in_the_browser(srv, monkeypatch):
    """Ebara's Paycom API wants the session its career page sets up, so the page makes the
    call, asked for the whole board, and titles are matched here."""
    calls = []

    async def fake_capture(url, url_part, timeout=25000, want=None, rewrite=None):
        sent = rewrite(json.loads(json.dumps(PAYCOM_PAGE_BODY)))
        calls.append((url, url_part, sent["skip"], sent["take"]))
        assert sent["filtersForQuery"] == PAYCOM_PAGE_BODY["filtersForQuery"]  # the rest of the page's call kept
        return paycom_answer([
            paycom_job(389200, "Desktop System Specialist  (32906)", "Phoenix, AZ - Phoenix, AZ"),
            paycom_job(401000, "Field Service Associate II  (Semiconductor)  (33300)", "Chandler, AZ - Chandler, AZ 85226"),
            paycom_job(389228, "Field Service Associate II  (Semiconductor)  (32907)", "Sherman, TX - Sherman, TX 75092"),
            paycom_job(401001, "Field Service Technician I-III (33301)", "Phoenix, AZ - Phoenix, AZ", remote="Hybrid"),
        ])

    monkeypatch.setattr(srv.browser, "capture_json", fake_capture)
    out = asyncio.run(srv.search_company_jobs("field service | customer engineer", companies=["Ebara"], location="AZ"))
    base = "https://www.paycomonline.net/v4/ats/web.php/portal/95CACB007211B4A999FBE2ED52E7762E"
    assert [(r["title"], r["location"], r["url"], r["external_id"], r["ats"]) for r in out["results"]] == [
        ("Field Service Associate II (Semiconductor)", "Chandler, AZ 85226", f"{base}/jobs/401000", "33300", "paycom"),
        ("Field Service Technician I-III", "Phoenix, AZ", f"{base}/jobs/401001", "33301", "paycom"),
    ]  # the Texas opening and the desktop job are left out
    assert out["results"][1]["notes"] == ["Paycom lists it as Hybrid"]
    assert calls == [(f"{base}/career-page", "/job-posting-previews/search", 0, 100)]  # whole board, one tab
    assert not out["errors"] and out["results"][0]["company"] == "Ebara Technologies"


def test_paycom_search_pages_through_a_big_board():
    from job_apply.search import Listing, paycom_rewrite, paycom_search

    skips = []

    async def capture(url, url_part, timeout=25000, want=None, rewrite=None):
        sent = rewrite(json.loads(json.dumps(PAYCOM_PAGE_BODY)))
        skips.append(sent["skip"])
        start = sent["skip"]
        return paycom_answer([paycom_job(i, f"Field Service Engineer ({i})", "Chandler, AZ - Chandler, AZ")
                              for i in range(start + 1, min(start + 100, 150) + 1)], total=150)

    found: list[Listing] = []
    asyncio.run(paycom_search(capture, "KEY", "field service", found))
    assert skips == [0, 100] and len(found) == 150
    assert paycom_rewrite({"widgets": []}) is None and paycom_rewrite(None) is None  # some other call: left alone

    calls = []

    async def odd(url, url_part, timeout=25000, want=None, rewrite=None):
        calls.append(url)
        return {"unexpected": True}
    asyncio.run(paycom_search(odd, "KEY", "field service", []))
    assert calls == ["https://www.paycomonline.net/v4/ats/web.php/portal/KEY/career-page"]  # nothing more to ask for


def test_workday_entries_that_arent_postings_are_left_out():
    """Analog Devices' answer had an entry with no title or address, which became a listing
    for the board itself and sent the desk paging through its job list."""
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total": 2, "jobPostings": [
            {"title": "", "locationsText": "", "postedOn": "", "bulletFields": []},
            {"title": "Field Service Engineer", "externalPath": "/job/Chandler-AZ/FSE_R1", "locationsText": "Chandler, AZ",
             "postedOn": "Posted Today", "bulletFields": ["R1"]}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_module._workday(client, "https://adco.wd1.myworkdayjobs.com/External", "field service",
                                                20, [])
    found = asyncio.run(go())
    assert [(x.title, x.url) for x in found] == [
        ("Field Service Engineer", "https://adco.wd1.myworkdayjobs.com/External/job/Chandler-AZ/FSE_R1")]


def workday_multi_site(filters: bool):
    """A Workday site whose postings include multi-site ones, listed as "7 Locations"; each
    posting's own call names its places. With filters, the site's location filter has an
    Arizona value (and the search uses it); without, the site has no location filter."""
    places = {
        "FSE_R1": ["Austin, TX", "Phoenix, AZ", "Hillsboro, OR", "Phoenix, AZ"],
        "FSE_R2": ["Austin, TX", "Hillsboro, OR"],
        "FSS_R5": ["Tempe, AZ"],
    }
    postings = [("Field Service Engineer", "/job/Austin-TX/FSE_R1", "7 Locations"),
                ("Field Service Engineer II", "/job/Austin-TX/FSE_R2", "2 Locations"),
                ("Field Service Technician", "/job/Chandler-AZ/FST_R3", "Chandler, AZ"),
                ("Field Service Engineer III", "/job/Phoenix-AZ/FSE_R4", "3 Locations"),  # its call fails
                ("Field Service Specialist", "/job/FSS_R5", "")]
    calls: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        if url == "https://mco.wd1.myworkdayjobs.com/wday/cxs/mco/External/jobs":
            body = json.loads(request.content)
            out = {"total": len(postings), "jobPostings": [
                {"title": t, "externalPath": path, "locationsText": where, "postedOn": "Posted Today",
                 "bulletFields": [path.rsplit("_", 1)[-1]]} for t, path, where in postings]}
            if filters and not body["appliedFacets"]:
                out["facets"] = [{"facetParameter": "locations", "descriptor": "Locations", "values": [
                    {"descriptor": "Phoenix, AZ", "id": "loc-phx", "count": 4},
                    {"descriptor": "Austin, TX", "id": "loc-aus", "count": 2}]}]
            return httpx.Response(200, json=out)
        job = url.rsplit("/", 1)[-1]
        if url.startswith("https://mco.wd1.myworkdayjobs.com/wday/cxs/mco/External/job/") and job in places:
            first, *more = places[job]
            return httpx.Response(200, json={"jobPostingInfo": {"title": "x", "location": first,
                                                                "additionalLocations": more}})
        return httpx.Response(500)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("field service", location="AZ", client=client, companies=[
                {"name": "Multi Co", "search": {"workday": "https://mco.wd1.myworkdayjobs.com/External"}}])
    found = asyncio.run(go())
    assert found["errors"] == {}
    return {r["title"]: (r["location"], r["notes"]) for r in found["results"]}, calls


def test_a_workday_posting_in_several_places_is_listed_with_them(monkeypatch):
    """Found through the site's own Arizona filter: the places come from each posting, the
    ones in Arizona first. Where they don't show Arizona, or the posting can't be read, it
    stays "N Locations" to check."""
    monkeypatch.setattr(search_module, "RETRY_DELAY", 0)
    listed, calls = workday_multi_site(filters=True)
    assert listed == {
        "Field Service Engineer": ("Phoenix, AZ; Austin, TX; Hillsboro, OR", []),
        "Field Service Engineer II": ("2 Locations", ["location given as '2 Locations'; check the posting"]),
        "Field Service Technician": ("Chandler, AZ", []),
        "Field Service Engineer III": ("3 Locations", ["location given as '3 Locations'; check the posting"]),
        "Field Service Specialist": ("Tempe, AZ", []),
    }
    # only the postings without a single place are read, each once (a failing one up to 3 times)
    read = [c.rsplit("/", 1)[-1] for c in calls if "/External/job/" in c]
    assert sorted(set(read)) == ["FSE_R1", "FSE_R2", "FSE_R4", "FSS_R5"] and read.count("FSE_R1") == 1


def test_a_workday_site_without_a_place_filter_drops_multi_site_jobs_elsewhere(monkeypatch):
    monkeypatch.setattr(search_module, "RETRY_DELAY", 0)
    listed, _ = workday_multi_site(filters=False)
    assert listed == {
        "Field Service Engineer": ("Phoenix, AZ; Austin, TX; Hillsboro, OR", []),
        # Austin and Hillsboro only: not in Arizona
        "Field Service Technician": ("Chandler, AZ", []),
        "Field Service Engineer III": ("3 Locations", ["location given as '3 Locations'; check the posting"]),
        "Field Service Specialist": ("Tempe, AZ", []),
    }


# Nikon Precision's UKG Pro board: the page's own search call (50 at a time) and its answer
UKG_PAGE_BODY = {"opportunitySearch": {"Top": 50, "Skip": 0, "QueryString": "", "OrderBy": [
    {"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}],
    "Filters": [{"t": "TermsSearchFilterDto", "fieldName": 4, "extra": None, "values": []}]},
    "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [], "Skills": [],
                      "hasNoLicenses": False, "SkippedSkills": []}}


def ukg_place(city, code, description):
    return {"LocalizedDescription": description, "Address": {"City": city, "State": {"Code": code, "Name": "Arizona"}}}


UKG_ANSWER = {"totalCount": 3, "opportunities": [
    {"Id": "a0a2a8f5", "Title": "NRCA Optical Scientist Graduate-Level Intern (Summer 2027)", "RequisitionNumber": "NRCAO001457",
     "Locations": [ukg_place("Oro Valley", "AZ", "Tucson, Arizona")], "PostedDate": "2026-09-29T22:59:48.553Z"},
    {"Id": "532a7dc9", "Title": "Field Service Engineer", "RequisitionNumber": "FIELD001451",
     "Locations": [ukg_place("Chandler", "AZ", "Arizona"), ukg_place(None, "AZ", "Phoenix, AZ")],
     "PostedDate": "2026-07-10T18:41:41.306Z"},
    {"Id": "77aa", "Title": "Field Service Engineer", "RequisitionNumber": "FIELD001460",
     "Locations": [{"LocalizedDescription": "Hillsboro, OR", "Address": {"City": "Hillsboro", "State": {"Code": "OR"}}}],
     "PostedDate": "2026-08-01T00:00:00Z"}]}


def test_ukg_board_is_read_in_the_browser(srv, monkeypatch):
    """Nikon Precision's UKG Pro board loads its openings from its own API; the page makes
    the call, asked for the whole board, and titles are matched here."""
    calls = []

    async def fake_capture(url, url_part, timeout=25000, want=None, rewrite=None):
        sent = rewrite(json.loads(json.dumps(UKG_PAGE_BODY)))
        calls.append((url, url_part, sent["opportunitySearch"]["Skip"], sent["opportunitySearch"]["Top"]))
        assert sent["matchCriteria"] == UKG_PAGE_BODY["matchCriteria"]  # the rest of the page's call kept
        return UKG_ANSWER

    monkeypatch.setattr(srv.browser, "capture_json", fake_capture)
    out = asyncio.run(srv.search_company_jobs("field service | customer engineer", companies=["Nikon"], location="AZ"))
    board = "https://recruiting2.ultipro.com/NIK1001NIKON/JobBoard/f11a0b52-5153-4c12-ad2c-b7f3b0a74112/"
    assert [(r["title"], r["location"], r["url"], r["external_id"], r["posted"], r["ats"]) for r in out["results"]] == [
        ("Field Service Engineer", "Chandler, AZ; Phoenix, AZ", f"{board}OpportunityDetail?opportunityId=532a7dc9",
         "FIELD001451", "2026-07-10", "ukg")]  # the intern and the Oregon opening are left out
    assert calls == [(f"{board}?q=&o=postedDateDesc", "/JobBoardView/LoadSearchResults", 0, 200)]
    assert not out["errors"] and out["results"][0]["company"] == "Nikon Precision"


def test_ukg_rewrite_leaves_other_calls_alone():
    from job_apply.search import ukg_board_url, ukg_rewrite
    assert ukg_rewrite({"filters": []}) is None and ukg_rewrite(None) is None
    assert ukg_board_url("https://recruiting2.ultipro.com/T/JobBoard/B?q=&o=postedDateDesc") == \
        "https://recruiting2.ultipro.com/T/JobBoard/B/"


# Edwards on SuccessFactors' newer search: the page's own search call and its answer
RMK_PAGE_BODY = {"locale": "en_US", "pageNumber": 0, "sortBy": "", "keywords": "", "location": "",
                 "facetFilters": {"filter1": ["Edwards"], "mfield3": ["United States"]}, "brand": "", "skills": [],
                 "categoryId": 0, "alertId": "", "rcmCandidateId": ""}
RMK_FACETS_BODY = {"facetingOnly": True, "categoryId": 0, "locale": "en_US", "keywords": "", "location": "",
                   "facetFields": ["filter1", "mfield3"], "facetFilters": {"filter1": ["Edwards"]}}


def rmk_job(job_id, title, slug, start="7/27/26"):
    return {"response": {"supportedLocales": ["en_US"], "filter1": ["Edwards"], "unifiedUrlTitle": slug,
                         "unifiedStandardStart": start, "filter2": ["Service"], "id": job_id,
                         "unifiedStandardTitle": title, "urlTitle": slug}}


RMK_ANSWER = {"totalJobs": 4, "jobSearchResult": [
    rmk_job("160986", "Workshop Technician - CA", "Workshop-Technician-CA", "5/27/26"),
    rmk_job("172120", "Onsite Service Engineer - AZ", "Onsite-Service-Engineer-AZ"),
    rmk_job("172852", "Assembly &amp; Test Technician (Edwards Vacuum)", "Assembly-&amp;-Test-Technician-%28Edwards-Vacuum%29",
            "8/7/26"),
    {"response": {"unifiedStandardTitle": "no id"}}]}


def edwards_posting(title, *lines):
    """An Edwards posting's header as SuccessFactors draws it: unlabelled lines under the title."""
    token = ('<div class="joblayouttoken displayDTM marginTopNone"><div class="inner"><div class="row">'
             '<div class="col-xs-12 fontalign-left"><span class="rtltextaligneligible"{attr} lang="en-US">{text} </span>'
             '</div></div></div></div>')
    return ('<html><body><div class="jobDisplay">' + token.format(attr=' itemprop="title"', text=title)
            + "".join(token.format(attr="", text=line) for line in lines) + "<p>Job Description</p></div></body></html>")


def test_successfactors_search_is_read_in_the_browser(srv, monkeypatch):
    """Edwards' search answers only its page; the page's own call gets each wording. The state
    Edwards puts at the end of some titles is their location; the others' postings say it."""
    calls, reads = [], []

    async def fake_read(url):
        reads.append(url)
        return edwards_posting("Assembly &amp; Test Technician (Edwards Vacuum)", "Manufacturing", "", "Chandler AZ",
                               "United States", "On-Site")

    monkeypatch.setattr(search_module, "read_page", fake_read)
    edwards = ("https://www.jobs.atlascopcogroup.com/search/?q=&facetFilters=%7B%22filter1%22%3A%5B%22Edwards%22%5D"
               "%2C%22mfield3%22%3A%5B%22United+States%22%5D%7D")

    async def fake_capture(url, url_part, timeout=25000, want=None, rewrite=None):
        assert not want(RMK_FACETS_BODY) and want(RMK_PAGE_BODY)  # the facet-only call is skipped
        sent = rewrite(json.loads(json.dumps(RMK_PAGE_BODY)))
        calls.append((url, url_part, sent["keywords"], sent["pageNumber"]))
        assert sent["facetFilters"] == RMK_PAGE_BODY["facetFilters"]  # still Edwards, still the US
        return RMK_ANSWER

    monkeypatch.setattr(srv.browser, "capture_json", fake_capture)
    out = asyncio.run(srv.search_company_jobs("field service", companies=["Edwards"], location="AZ"))
    got = {r["title"]: r for r in out["results"]}
    assert set(got) == {"Onsite Service Engineer - AZ", "Assembly & Test Technician (Edwards Vacuum)"}  # CA left out
    onsite = got["Onsite Service Engineer - AZ"]
    assert (onsite["location"], onsite["posted"], onsite["url"]) == (
        "AZ", "2026-07-27", "https://www.jobs.atlascopcogroup.com/job/Onsite-Service-Engineer-AZ/172120-en_US")
    assembly = got["Assembly & Test Technician (Edwards Vacuum)"]
    assert (assembly["location"], assembly["notes"]) == ("Chandler, AZ", [])  # read off its posting
    # the CA and AZ titles say where they are; only the third posting is read
    assert [url.rsplit("/", 1)[-1] for url in reads] == ["172852-en_US"]
    assert calls == [(edwards, "/services/recruiting/v1/jobs", "field service", 0)]  # 4 openings: one page
    assert not out["errors"]


def test_successfactors_search_pages_through_results():
    from job_apply.search import Listing, rmk_search

    pages = []

    async def capture(url, url_part, timeout=25000, want=None, rewrite=None):
        page = rewrite(json.loads(json.dumps(RMK_PAGE_BODY)))["pageNumber"]
        pages.append(page)
        return {"totalJobs": 23, "jobSearchResult": [rmk_job(f"{page}{i}", f"Service Engineer {page}{i} - AZ", "x")
                                                      for i in range(10 if page < 2 else 3)]}

    async def read(url):
        raise AssertionError(f"read {url}, though its title says where it is")

    found: list[Listing] = []
    asyncio.run(rmk_search(capture, {"url": "https://jobs.example.com/search/?q="}, "service", found, read=read))
    assert pages == [0, 1, 2] and len(found) == 23
    assert found[0].url == "https://jobs.example.com/job/x/00-en_US" and found[0].location == "AZ"


def test_successfactors_search_without_a_full_address_is_searched_at_https():
    """A person's own employer list with the search page's address missing its https:// is
    searched at https://, as the classic SuccessFactors search already does, not failed with
    "'NoneType' object has no attribute 'group'"."""
    from job_apply.search import Listing, rmk_search

    opened = []

    async def capture(url, url_part, timeout=25000, want=None, rewrite=None):
        opened.append(url)
        return {"totalJobs": 1, "jobSearchResult": [{"response": {
            "id": "171942", "unifiedStandardTitle": "Field Service Engineer - AZ", "unifiedUrlTitle": "FSE"}}]}

    async def read(url):
        raise AssertionError("the title names the state")

    found: list[Listing] = []
    asyncio.run(rmk_search(capture, {"url": "jobs.example.com/search/?q="}, "service", found, read))
    assert opened == ["https://jobs.example.com/search/?q="]
    assert [x.url for x in found] == ["https://jobs.example.com/job/FSE/171942-en_US"]


def test_edwards_postings_without_a_state_are_read_for_their_place():
    """Most Edwards titles carry no state (live, Oct 2026: Field Service Engineer in Phoenix
    and in San Jose); each posting's header names its city and state."""
    from job_apply.search import Listing, rmk_search

    reads = []
    places = {"171942": ("Service", "", "Phoenix AZ", "United States", "On-Site"),
              "170917": ("Service", "", "San Jose CA", "United States", "On-Site"),
              "155395": ("Service", "", "United States", "On-Site")}  # no city: stays unknown

    async def read(url):
        reads.append(url)
        job_id = url.rsplit("/", 1)[-1].split("-")[0]
        if job_id == "160001":
            raise httpx.ConnectError("unreachable")
        return edwards_posting("Field Service Engineer", *places[job_id])

    async def capture(url, url_part, timeout=25000, want=None, rewrite=None):
        rewrite(json.loads(json.dumps(RMK_PAGE_BODY)))
        return {"totalJobs": 5, "jobSearchResult": [
            rmk_job("171942", "Field Service Engineer", "Field-Service-Engineer"),
            rmk_job("170917", "Field Service Engineer", "Field-Service-Engineer"),
            rmk_job("155395", "Field Service Engineer - US EG", "Field-Service-Engineer-US-EG"),
            rmk_job("160001", "Service Engineer", "Service-Engineer"),
            rmk_job("172120", "Onsite Service Engineer - AZ", "Onsite-Service-Engineer-AZ")]}

    found: list[Listing] = []
    site = {"url": "https://jobs.example.com/search/?q="}
    asyncio.run(rmk_search(capture, site, "field service", found, read=read))
    assert {x.external_id: x.location for x in found} == {
        "171942": "Phoenix, AZ", "170917": "San Jose, CA", "155395": "", "160001": "", "172120": "AZ"}
    assert len(reads) == 4  # not the one whose title ends with its state
    asyncio.run(rmk_search(capture, site, "service engineer", found, read=read))  # the next wording
    assert len(reads) == 6  # only the two still unknown are tried again


# Qorvo's search pages (SuccessFactors), as the live site drew them in Oct 2026
SF_ROW = """<tr class="data-row"> <td class="colTitle" headers="hdrTitle"> <span class="jobTitle hidden-phone">
 <a class="jobTitle-link" href="{href}">{title}</a> </span> <div class="jobdetail-phone visible-phone">
 <span class="jobTitle visible-phone"> <a class="jobTitle-link" href="{href}">{title}</a> </span>
 <span class="jobLocation visible-phone"> <span class="jobLocation"> {where} </span></span> </div> </td>
 <td class="colLocation hidden-phone" headers="hdrLocation"> <span class="jobLocation"> {where} </span> </td> </tr>"""
SF_MORE = '<small class="nobr">+{} more…</small>'


def sf_row(job_id, title, where, more=0):
    slug = re.sub(r"\W+", "-", f"{where.split(',')[0]}-{title}")
    return SF_ROW.format(href=f"/job/{slug}/{job_id}/", title=title,
                         where=where + (" " + SF_MORE.format(more) if more else ""))


def sf_page(rows, first, total):
    last = first + len(rows) - 1
    return (f'<html><body><span aria-label="Results {first} – {last}" class="paginationLabel">Results <b>{first} – '
            f'{last}</b> of <b>{total}</b></span><table id="searchresults"><tbody>{"".join(rows)}</tbody></table>'
            "</body></html>")


QORVO_AZ = [sf_row("1421977600", "Analog Design Intern", "Chandler, AZ, US, 85226"),
            sf_row("1420082600", "Test Engineering Intern", "Chandler, AZ, US, 85226"),
            sf_row("1424233200", "Sr Principal Design Engineer", "Greensboro, NC, US, 27409", more=3),
            sf_row("1434607400", "Sr. RFIC Design Engineer", "Greensboro, NC, US, 27409", more=8)]


def run_sf(handler, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await search_companies(client=client, companies=[
                {"name": "Qorvo", "search": {"successfactors": "https://careers.qorvo.com"}}], **kw)
    return asyncio.run(go())


def test_successfactors_search_pages_use_the_sites_own_state_search():
    """Given a state, Qorvo's own location search narrows the openings; an opening whose
    first place is elsewhere is only listed because one of its other places is there."""
    asked = []

    def handler(request):
        assert str(request.url).startswith("https://careers.qorvo.com/search/?")
        asked.append(dict(request.url.params))
        return httpx.Response(200, text=sf_page(QORVO_AZ, 1, 4))

    out = run_sf(handler, query="engineer", location="AZ")
    assert asked == [{"q": "engineer", "startrow": "0", "locationsearch": "Arizona"}]
    got = {r["title"]: r for r in out["results"]}
    assert got["Analog Design Intern"]["url"] == "https://careers.qorvo.com/job/Chandler-Analog-Design-Intern/1421977600/"
    assert got["Analog Design Intern"]["location"] == "Chandler, AZ, US, 85226"
    assert got["Sr Principal Design Engineer"]["location"] == "Greensboro, NC, US, 27409 (+3 more); Arizona"
    assert all(not r["notes"] for r in out["results"]) and len(got) == 4
    assert got["Analog Design Intern"]["external_id"] == "1421977600" and not out["errors"]


def test_successfactors_search_pages_through_results_and_keeps_unclear_places():
    """25 openings a page until the count the page gives. Without a state to search, an
    opening shown elsewhere "+3 more" may still be in the user's city: it's kept, flagged."""
    starts = []

    def handler(request):
        start = int(request.url.params["startrow"])
        starts.append(start)
        assert "locationsearch" not in request.url.params
        if start == 0:
            rows = [sf_row("1", "Test Engineer", "Chandler, AZ, US, 85226"),
                    sf_row("2", "Design Engineer", "Greensboro, NC, US, 27409", more=3)]
            rows += [sf_row(str(100 + i), "Process Engineer", "Richardson, TX, US, 75080") for i in range(23)]
        else:
            rows = [sf_row(str(200 + start + i), "Process Engineer", "Richardson, TX, US, 75080")
                    for i in range(25 if start == 25 else 3)]
        return httpx.Response(200, text=sf_page(rows, start + 1, 53))

    out = run_sf(handler, query="engineer", location="Chandler|Phoenix", limit=60)
    assert starts == [0, 25, 50]
    assert [(r["title"], r["notes"]) for r in out["results"]] == [
        ("Design Engineer", ["location given as 'Greensboro, NC, US, 27409 (+3 more)'; check the posting"]),
        ("Test Engineer", [])]




# Amkor's career site (SuccessFactors' older pages), as its script drew the list in Oct 2026
SFC_ROW = ('<tr class="jobResultItem"><td><div role="heading" aria-level="3"><a class="jobTitle" '
           'href="/career?career%5fns=job%5flisting&amp;company=amkor&amp;navBarLevel=JOB%5fSEARCH&amp;'
           'rcm%5fsite%5flocale=en%5fUS&amp;career_job_req_id={req}&amp;selected_lang=en_US&amp;'
           '_s.crb=6LEX%2ba4zKBJnftf%3d">{title}</a></div><div class="noteSection" role="note"><div>Requisition ID: '
           '<span class="jobContentEM">{req}</span> - <span class="jobContentEM">Posted on {posted}</span> - '
           '<span class="jobContentEM">Engineering</span>&nbsp;&nbsp;-&nbsp;&nbsp;<span class="jobContentEM">Regular '
           'Non-Exempt Full-Time Employee</span></div></div></td></tr>')


def sfc_page(rows):
    return f'<html><body><span class="jobCount">{len(rows)} Jobs</span><table><tbody>{"".join(rows)}</tbody></table></body></html>'


def test_amkor_list_is_read_in_the_browser_and_its_postings_say_where(srv, monkeypatch):
    """Amkor's list is drawn by its page's script, 50 to a page, and its rows name no place: titles
    are matched here, and each match's posting says where the job is."""
    rows = [SFC_ROW.format(req="29111", title="Equipment Technician (ATA)", posted="10/01/2026"),
            SFC_ROW.format(req="29112", title="Field Service Engineer", posted="09/30/2026"),
            SFC_ROW.format(req="29113", title="Logistics Analyst", posted="09/29/2026"),
            SFC_ROW.format(req="29114", title="Equipment Technician", posted="09/28/2026"),
            SFC_ROW.format(req="29115", title="Field Service Engineer II", posted="09/27/2026")]
    places = {"29111": "This position is based at our Peoria, Arizona factory.", "29112": "Based in Austin, TX.",
              "29114": "Join us at our headquarters in Tempe, AZ."}
    asked, reads = [], []

    async def fake_pages(url, row_selector, per_page=None, next_button=None, max_pages=5):
        asked.append((url, row_selector, per_page, next_button))
        return [sfc_page(rows[:3]), sfc_page(rows[3:])]

    async def fake_read(url):
        reads.append(url)
        req = re.search(r"career_job_req_id=(\d+)", url).group(1)
        if req not in places:
            raise httpx.ConnectError("unreachable")
        return f"<html><body><div>Job Description</div><p>{places[req]}</p></body></html>"

    monkeypatch.setattr(srv.browser, "listing_pages", fake_pages)
    monkeypatch.setattr(search_module, "read_page", fake_read)
    out = asyncio.run(srv.search_company_jobs("field service | equipment technician", companies=["Amkor"], location="AZ"))
    got = {r["title"]: (r["location"], r["posted"], r["notes"]) for r in out["results"]}
    assert got == {  # Austin is left out, the analyst wasn't asked for, an unreadable posting is flagged
        "Equipment Technician (ATA)": ("Peoria, AZ", "2026-10-01", []),
        "Equipment Technician": ("Tempe, AZ", "2026-09-28", []),
        "Field Service Engineer II": ("", "2026-09-27", ["no location given; check the posting"])}
    assert len(reads) == 4  # the matches only
    page = "https://career8.successfactors.com/career?company=amkor&career_ns=job_listing_summary&navBarLevel=JOB_SEARCH"
    assert asked == [(page, "tr.jobResultItem", ("li.per_page select", "50"), "li.paginationArrowContainer.next > a")]
    tech = next(r for r in out["results"] if r["title"] == "Equipment Technician (ATA)")
    assert tech["url"] == ("https://career8.successfactors.com/career?career_ns=job_listing&company=amkor"
                           "&navBarLevel=JOB_SEARCH&rcm_site_locale=en_US&career_job_req_id=29111&selected_lang=en_US")
    assert not out["errors"]



# Benchmark's Infor CloudSuite board: the page's list call and a posting card, as seen in Oct 2026
INFOR_CALL = ("https://css-benchmark-prd.inforcloudsuite.com/hcm/Jobs/list/JobPosting.JobSearchCardViewList?pageop=load"
              "&pagesize=10&dependentList=true&relation=JobBoard%281%2CEXTERNAL%29.Postings&csk.JobBoard=EXTERNAL")


def infor_card(job_id, title, where, begin, link=True):
    href = (f"https://CSS-BENCHMARK-PRD.INFORCLOUDSUITE.COM:443/hcm/Jobs/navigation/JobPosting%5BJobPostingSet%5D"
            f"%281%2C{job_id}%2C1%29.JobPostingDisplayNav?csk.HROrganization&#61;1&amp;csk.JobBoard&#61;EXTERNAL"
            "&amp;web10x&#61;true")
    fields = {"Description": {"value": title, "size": 100}, "LocationOfJob": {"value": where},
              "PostingDateRange_prd_Begin": {"value": begin}, "JobId": {"value": job_id},
              "CategoryDescriptionForSort": {"value": "Manufacturing / Production"}}
    if link:
        fields["_op_JobPostingCardViewLabelLinkBack_spc_translation_cp_"] = {
            "value": f'<a href="{href}">{title} - {job_id}</a>'}
    return {"resourceId": f"JobPosting[JobPostingSet](1,{job_id},1)", "fields": fields}


def test_benchmark_board_is_read_in_the_browser(srv, monkeypatch):
    """Benchmark's Infor board answers its own page, 10 postings at a time: that call is asked
    for 200, and titles and places are matched here."""
    cfg_url = next(c for c in search_module.load_companies() if c["name"] == "Benchmark Electronics")["search"]["infor"]["url"]
    calls = []

    async def fake_capture(url, url_part, timeout=25000, want=None, rewrite=None, rewrite_url=None):
        calls.append((url, url_part, rewrite_url(INFOR_CALL)))
        return {"dataViewSet": {"pagingInfo": {"pageSize": 200, "hasNext": False}, "data": [
            infor_card("12460", "Manufacturing Technician I", "Arizona:Mesa", "20260930"),
            infor_card("12529", "Precision Inspector II", "Arizona:Tempe", "20261006"),
            infor_card("12554", "Production Inspector I", "Minnesota:Rochester", "20261006"),
            infor_card("12555", "Test Technician", "Arizona:Tempe", "20261001", link=False),  # no way to it
            infor_card("12497", "PCB Assembler II", "ROU:BV:Ghimbav", "20261006")]}}

    monkeypatch.setattr(srv.browser, "capture_json", fake_capture)
    out = asyncio.run(srv.search_company_jobs("technician | inspector", companies=["Benchmark"], location="AZ"))
    got = {r["title"]: (r["location"], r["posted"], r["url"]) for r in out["results"]}
    assert got == {  # Rochester left out; the assembler wasn't asked for
        "Manufacturing Technician I": ("Mesa, AZ", "2026-09-30", (
            "https://css-benchmark-prd.inforcloudsuite.com/hcm/Jobs/navigation/JobPosting%5BJobPostingSet%5D%281%2C12460"
            "%2C1%29.JobPostingDisplayNav?csk.HROrganization=1&csk.JobBoard=EXTERNAL&web10x=true")),
        "Precision Inspector II": ("Tempe, AZ", "2026-10-06", (
            "https://css-benchmark-prd.inforcloudsuite.com/hcm/Jobs/navigation/JobPosting%5BJobPostingSet%5D%281%2C12529"
            "%2C1%29.JobPostingDisplayNav?csk.HROrganization=1&csk.JobBoard=EXTERNAL&web10x=true"))}
    assert calls == [(cfg_url, "JobPosting.JobSearchCardViewList", INFOR_CALL.replace("pagesize=10", "pagesize=200"))]
    assert not out["errors"]


def test_infor_places():
    from job_apply.search import _infor_place

    assert _infor_place("Arizona:Tempe") == "Tempe, AZ"
    assert _infor_place("MX:BC:Tijuana") == "Tijuana, BC, MX"
    assert _infor_place("Jalisco: El Salto") == "El Salto, Jalisco"
    assert _infor_place("Remote") == "Remote"


def test_oracle_search_pages_past_the_first_25():
    """Oracle answers 25 at a time: onsemi's "technician" search has 200+, and Arizona's
    can be on any page."""
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        asked.append(url)
        m = re.search(r"offset=(\d+),", url)
        start = int(m.group(1)) if m else 0
        reqs = [{"Id": str(1000 + n), "Title": f"Equipment Technician {n}",
                 "PrimaryLocation": "Phoenix, AZ, United States" if n in (30, 140) else "Austin, TX, United States"}
                for n in range(start, min(start + 25, 150))]
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 150, "requisitionList": reqs}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("equipment technician", location="AZ", client=client, companies=[
                {"name": "Oracle Co", "search": {"oracle": {"host": "abcd.fa.us2.oraclecloud.com", "site": "CX_1"}}}])
    found = asyncio.run(go())
    assert found["errors"] == {}
    assert [r["title"] for r in found["results"]] == ["Equipment Technician 140", "Equipment Technician 30"]
    assert len(asked) == 6 and "offset" not in asked[0] and "limit=25,offset=25," in asked[1]  # stops at the last


def test_broad_us_listings_are_flagged_not_dropped():
    """Field-service and travel jobs are often posted with only the country and a word or two:
    the person could take them from Arizona, so they're kept and marked "check the posting"."""
    az = location_terms("AZ")
    for broad in ("US - Multiple Locations", "Multiple Locations, US", "Field Based - US", "Remote - US (Field Based)",
                  "Remote - United States (Travel)", "Home Based - USA", "Anywhere in the US", "US Nationwide",
                  "North America", "Work From Home, US"):
        assert location_matches(broad, az) is None, broad
    # another state alone, its code also an English word (Edwards lists Oregon jobs as "OR", live)
    for elsewhere in ("Remote - Canada", "Remote, Japan", "US - Texas", "Remote - TX", "OR", "Onsite - OR"):
        assert location_matches(elsewhere, az) is False, elsewhere


def test_an_area_city_listed_beside_another_state_is_in_the_area():
    az = location_terms("AZ")
    for here in ("Austin, TX; Chandler", "Chandler (Office); Austin, TX", "Hillsboro, OR or Chandler", "Phoenix / LA",
                 "Chandler - WORK IN OFFICE"):
        assert location_matches(here, az) is True, here
    for elsewhere in ("Indianapolis, IN", "Portland OR 97201", "US-IN-Indianapolis", "Peoria, IL; Austin, TX"):
        assert location_matches(elsewhere, az) is False, elsewhere


def test_a_location_as_a_person_types_it():
    for typed in ("Phoenix AZ", "Phoenix Arizona", "Phoenix, AZ 85001", "Arizona (Phoenix area)"):
        terms = location_terms(typed)
        assert {"phoenix", "az", "arizona", "chandler"} <= set(terms), (typed, terms)
        assert location_matches("Tempe, Arizona", terms) and location_matches("Phoenix, OR", terms) is False
    for metro in ("Greater Phoenix", "Phoenix metro", "Phoenix area"):
        assert location_matches("Phoenix, AZ", location_terms(metro)) is True, metro
    # cities alone: the state they're in, not another state's city of the same name
    cities = location_terms("Phoenix|Chandler")
    assert location_matches("Chandler, AZ", cities) is True
    assert location_matches("Phoenix, OR", cities) is False and location_matches("Chandler, TX", cities) is False


def test_a_workday_remote_us_job_is_kept_when_the_site_lists_no_area_place():
    """The site's place filter has no Arizona value: its "3 Locations" jobs aren't in the area,
    but a "Remote - United States" one could be done from it. A posting whose place is null
    doesn't fail the company's search."""
    facets = [{"facetParameter": "locations", "values": [
        {"id": "tx", "descriptor": "Austin, TX", "count": 2}, {"id": "us", "descriptor": "Remote - United States", "count": 1}]}]
    postings = [
        {"title": "Field Service Engineer", "externalPath": "/job/x/FSE_R1", "locationsText": "Remote - United States",
         "postedOn": "Posted Today", "bulletFields": ["R1"]},
        {"title": "Field Service Engineer II", "externalPath": "/job/x/FSE_R2", "locationsText": "3 Locations",
         "postedOn": "Posted Today", "bulletFields": ["R2"]},
        {"title": "Field Service Technician", "externalPath": "/job/x/FST_R3", "locationsText": None,
         "postedOn": None, "bulletFields": ["R3"]},
    ]

    def answer(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":  # a posting's own page: nowhere to say
            return httpx.Response(200, json={"jobPostingInfo": {}})
        return httpx.Response(200, json={"total": 3, "facets": facets, "jobPostings": postings})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_module._workday(client, "https://adco.wd1.myworkdayjobs.com/External", "field service",
                                                20, location_terms("AZ"))
    found = asyncio.run(go())
    assert [x.title for x in found] == ["Field Service Engineer", "Field Service Technician"]


def _paged(total: int, area_at: set[int]):
    def place(n: int) -> str:
        return "Chandler, AZ" if n in area_at else "Boise, ID"
    return place


def test_eightfold_and_smartrecruiters_read_on_for_the_area():
    """Neither search takes the area (Micron's "Arizona" finds nothing), so with the area
    filtered here they read past the first 60: Arizona's openings can be far down."""
    place = _paged(150, {120, 130})
    asked = {"eightfold": [], "smartrecruiters": []}

    def answer(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        if "eightfold" in request.url.host or "efco" in request.url.host:
            start = int(q.get("start") or 0)
            asked["eightfold"].append(start)
            positions = [{"id": n, "name": f"Field Service Engineer {n}", "locations": [place(n)],
                          "canonicalPositionUrl": f"https://careers.efco.com/careers/job/{n}"} for n in range(start, min(start + 10, 150))]
            return httpx.Response(200, json={"data": {"positions": positions, "count": 150}})
        offset = int(q.get("offset") or 0)
        asked["smartrecruiters"].append(offset)
        content = [{"id": str(n), "name": f"Field Service Engineer {n}", "location": {"city": "Chandler" if n in (120, 130) else "Boise",
                    "region": "AZ" if n in (120, 130) else "ID", "country": "us"}, "releasedDate": "2026-10-01T00:00:00Z"}
                   for n in range(offset, min(offset + 100, 150))]
        return httpx.Response(200, json={"content": content, "totalFound": 150})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("field service", location="AZ", client=client, companies=[
                {"name": "Eightfold Co", "search": {"eightfold": {"host": "careers.efco.com", "domain": "efco.com"}}},
                {"name": "SR Co", "search": {"smartrecruiters": "srco"}}])
    found = asyncio.run(go())
    assert found["errors"] == {}
    by = {}
    for r in found["results"]:
        by.setdefault(r["company"], []).append(r["title"])
    assert sorted(by.get("Eightfold Co", [])) == ["Field Service Engineer 120", "Field Service Engineer 130"], found
    assert sorted(by.get("SR Co", [])) == ["Field Service Engineer 120", "Field Service Engineer 130"], found
    assert asked["smartrecruiters"] == [0, 100]


def test_oracle_keeps_two_places_of_one_city_name():
    merged = search_module._merge_places(["Peoria, IL, United States", "Peoria, AZ, US", "Scottsdale, AZ, United States",
                                          "Scottsdale, AZ, US"])
    assert merged == "Peoria, IL, United States; Peoria, AZ, US; Scottsdale, AZ, United States"


def test_companies_are_picked_by_whole_words():
    companies = [{"name": n} for n in ["ASML", "ASM (ASM America)", "Intel", "Tokyo Electron (TEL)", "Lam Research"]]
    pick = search_module._pick
    assert [c["name"] for c in pick(companies, ["TEL"])] == ["Tokyo Electron (TEL)"]
    assert [c["name"] for c in pick(companies, ["ASM"])] == ["ASM (ASM America)"]
    assert [c["name"] for c in pick(companies, ["Lam Research", "intel"])] == ["Intel", "Lam Research"]


def test_a_state_named_whole_or_a_city_named_for_one():
    """"West Virginia" is West Virginia, not Virginia after "West"; "Kansas City, MO" and
    "Arizona City" are places, not a state with "City" after it."""
    wv = location_terms("West Virginia")
    assert location_matches("Charleston, WV", wv) is True and location_matches("Richmond, VA", wv) is False
    assert location_matches("Morgantown, West Virginia", wv) is True
    assert "ks" not in location_terms("Kansas City, MO") and "mo" in location_terms("Kansas City, MO")
    assert location_terms("Arizona City") == ["arizona city"]
    assert "az" in location_terms("Arizona - Phoenix")  # set apart: a state first


def test_a_city_named_alone_beside_its_fuller_self_is_one_place():
    merged = search_module._merge_places(["Phoenix", "Phoenix, AZ, United States", "Casa Grande",
                                          "Casa Grande, AZ, US", "Tucson"])
    assert merged == "Phoenix, AZ, United States; Casa Grande, AZ, US; Tucson"


def test_a_company_is_picked_by_the_start_of_its_name():
    companies = [{"name": n} for n in ["Applied Materials", "ASM (ASM America)", "ASML", "Microchip Technology", "Micron"]]
    pick = search_module._pick
    assert [c["name"] for c in pick(companies, ["Applied Material"])] == ["Applied Materials"]
    assert [c["name"] for c in pick(companies, ["Micro"])] == ["Microchip Technology", "Micron"]
    assert [c["name"] for c in pick(companies, ["ASM"])] == ["ASM (ASM America)"]  # a short code: whole words only


def test_a_persons_own_employer_list_is_searched_instead(job_apply_home):
    """Someone looking for other work (HR in Phoenix) keeps their own list in their own
    folder: it's searched in place of the plugin's, or beside it with include_builtin, their
    entry winning for an employer both name. A broken file says which file."""
    builtin = search_module.load_companies()
    assert any(c["name"] == "Intel" for c in builtin)
    own = job_apply_home / "companies.yaml"
    own.write_text("companies:\n  - name: Example Health\n    careers_url: https://example.wd1.myworkdayjobs.com/Careers\n"
                   "    search: {workday: https://example.wd1.myworkdayjobs.com/Careers}\n"
                   "  - name: Intel\n    careers_url: https://jobs.example.com/intel\n")
    assert [c["name"] for c in search_module.load_companies()] == ["Example Health", "Intel"]
    assert search_module.companies_path() == own
    own.write_text(own.read_text() + "include_builtin: true\n")
    both = search_module.load_companies()
    assert len(both) == len(builtin) + 1 and [c["careers_url"] for c in both if c["name"] == "Intel"] == \
        ["https://jobs.example.com/intel"]
    own.write_text("companies:\n  - name: [oops\n")
    with pytest.raises(ValueError, match="companies.yaml can't be read"):
        search_module.load_companies()


def test_a_persons_own_file_can_name_the_plugins_lists(job_apply_home, monkeypatch, tmp_path):
    """`lists: [phoenix-metro, semiconductor-az]` searches both (an employer in two lists
    once), with the person's own entries first; a list the plugin doesn't have says which it has."""
    from job_apply import config

    root = tmp_path / "plugin"
    (root / "data" / "lists").mkdir(parents=True)
    (root / "data" / "companies.yaml").write_text("companies:\n  - {name: Intel, careers_url: https://a.example}\n"
                                                  "  - {name: Microchip, careers_url: https://b.example}\n")
    (root / "data" / "lists" / "phoenix-metro.yaml").write_text(
        "companies:\n  - {name: Example Health, careers_url: https://c.example}\n  - {name: Intel, careers_url: https://d.example}\n")
    monkeypatch.setattr(config, "PLUGIN_ROOT", root)
    assert list(search_module.employer_lists()) == ["semiconductor-az", "phoenix-metro"]
    own = job_apply_home / "companies.yaml"
    own.write_text("lists: [phoenix-metro, semiconductor-az]\ncompanies:\n  - {name: Example Bank, careers_url: https://e.example}\n")
    got = search_module.load_companies()
    assert [(c["name"], c["careers_url"]) for c in got] == [
        ("Example Bank", "https://e.example"), ("Example Health", "https://c.example"), ("Intel", "https://d.example"),
        ("Microchip", "https://b.example")]
    own.write_text("lists: [phoenix]\n")
    with pytest.raises(ValueError, match="no employer list named 'phoenix'.*phoenix-metro"):
        search_module.load_companies()
    own.write_text("lists: ../../etc/passwd\n")
    with pytest.raises(ValueError, match="no employer list named"):
        search_module.load_companies()


def test_a_persons_own_entries_stay_as_written_and_cover_the_lists_same_sites(job_apply_home, monkeypatch, tmp_path):
    """Two entries of their own under one name (two sites of one employer) are both searched;
    a list's entry for a site one of theirs already searches, under another name ("Mayo
    Clinic (Arizona)"), is left out; `lists:` written as anything but names is said to be wrong."""
    from job_apply import config

    root = tmp_path / "plugin"
    (root / "data" / "lists").mkdir(parents=True)
    (root / "data" / "companies.yaml").write_text("companies: []\n")
    (root / "data" / "lists" / "phoenix-metro.yaml").write_text(
        "companies:\n  - name: Mayo Clinic (Arizona)\n    careers_url: https://a.example\n"
        "    search: {oracle: {site: CX_1, host: fa.example.oraclecloud.com}}\n"
        "  - {name: Example Bank, careers_url: https://b.example}\n")
    monkeypatch.setattr(config, "PLUGIN_ROOT", root)
    own = job_apply_home / "companies.yaml"
    own.write_text("lists: [phoenix-metro]\ncompanies:\n"
                   "  - {name: Robert Half, careers_url: https://c.example, search: {workday: 'https://rh.wd1.myworkdayjobs.com/A'}}\n"
                   "  - {name: Robert Half, careers_url: https://d.example, search: {workday: 'https://rh.wd1.myworkdayjobs.com/B'}}\n"
                   "  - name: Mayo Clinic\n    careers_url: https://e.example\n"
                   "    search: {oracle: {host: fa.example.oraclecloud.com, site: CX_1}}\n")
    assert [c["careers_url"] for c in search_module.load_companies()] == \
        ["https://c.example", "https://d.example", "https://e.example", "https://b.example"]
    own.write_text("lists:\n  phoenix-metro: true\n")
    with pytest.raises(ValueError, match="should be a list of the plugin's list names"):
        search_module.load_companies()
    own.write_text("- phoenix-metro\n")
    with pytest.raises(ValueError, match="`lists:`"):
        search_module.load_companies()


def test_a_company_is_picked_by_the_name_asked_for_not_one_inside_it():
    companies = [{"name": n} for n in ["Maricopa County", "Maricopa Community Colleges", "Intel"]]
    pick = search_module._pick
    assert [c["name"] for c in pick(companies, ["Maricopa County Community College District"])] == []
    assert [c["name"] for c in pick(companies, ["Intel Corporation"])] == ["Intel"]
    assert [c["name"] for c in pick(companies, ["Maricopa County"])] == ["Maricopa County"]


def _icims_results(rows: list[tuple[str, str]], page: int, last: int) -> str:
    """An iCIMS results page as Aerotek's draws it: the place among each row's details, and
    links to the other pages (pr=0 is the first)."""
    out = []
    for n, (title, place) in enumerate(rows):
        out.append(f'<div class="row"><div class="col-xs-6 header left"></div>'
                   f'<div class="col-xs-12 title"><a class="iCIMS_Anchor" href="https://careers-x.icims.com/jobs/{page}{n}/'
                   f'job-{page}-{n}/job?in_iframe=1"><h3>{title}</h3></a></div>'
                   '<div class="col-xs-12 additionalFields"><dl class="iCIMS_JobHeaderGroup">'
                   '<div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Category</dt>'
                   '<dd class="iCIMS_JobHeaderData"><span>Recruiting</span></dd></div>'
                   '<div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField"><span class="sr-only field-label">'
                   f'Location : Location</span></dt><dd class="iCIMS_JobHeaderData"><span>{place}</span></dd></div>'
                   '</dl></div></div>')
    links = "".join(f'<a href="https://careers-x.icims.com/jobs/search?pr={p}&amp;in_iframe=1">{p + 1}</a>'
                    for p in range(last + 1))
    return f'<html><body>{"".join(out)}<div class="iCIMS_Paging">{links}</div></body></html>'


def test_an_icims_rows_place_is_read_from_its_details_and_later_pages_are_read():
    """Aerotek's portal: each row's place sits under "Location" in its details (results used to
    come back unplaced, so nothing was left out as elsewhere), and Arizona openings are on
    page 3 of a national search (only page 1 was read)."""
    from job_apply.search import ICIMS_PAGES, icims_search, parse_icims

    rows = parse_icims(_icims_results([("On Premise Recruiter", "US-WI-Stoughton")], 0, 0), "https://careers-x.icims.com")
    assert [(r.title, r.location) for r in rows] == [("On Premise Recruiter", "US-WI-Stoughton")]

    asked = []
    pages = {0: [("Recruiter", "US-WI-Stoughton")], 1: [("Recruiter", "US-TX-Dallas")],
             2: [("Recruiter", "US-AZ-Tempe")], 3: [("Recruiter", "US-AZ-Phoenix")], 4: [("Recruiter", "US-AZ-Mesa")]}

    async def frames_html(url):
        page = int(parse_qs(urlsplit(url).query).get("pr", ["0"])[0])
        asked.append(page)
        return [_icims_results(pages[page], page, 4)]

    found = []
    asyncio.run(icims_search(frames_html, "careers-x", "recruiter", found))
    assert asked == list(range(ICIMS_PAGES)) == [0, 1, 2, 3]  # a few pages, not every one
    assert [r.location for r in found if r.location.startswith("US-AZ")] == ["US-AZ-Tempe", "US-AZ-Phoenix"]
    asked.clear()

    async def one_page(url):
        asked.append(url)
        return [_icims_results(pages[0], 0, 0)]

    asyncio.run(icims_search(one_page, "careers-x", "recruiter", []))
    assert len(asked) == 1  # a search with one page of results is read once


def test_an_icims_rows_location_type_is_not_its_place():
    """Details named "Location Type" or "Remote Location Eligible" ahead of the row's
    "Location" aren't its place: "Onsite" isn't in Arizona, so the opening was dropped."""
    from job_apply.search import parse_icims

    page = _icims_results([("Recruiter", "US-AZ-Phoenix")], 0, 0).replace(
        '<div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Category</dt>',
        '<div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Location Type</dt>'
        '<dd class="iCIMS_JobHeaderData"><span>Onsite</span></dd></div>'
        '<div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Remote Location Eligible</dt>'
        '<dd class="iCIMS_JobHeaderData"><span>No</span></dd></div>'
        '<div class="iCIMS_JobHeaderTag"><dt class="iCIMS_JobHeaderField">Category</dt>')
    assert [r.location for r in parse_icims(page, "https://careers-x.icims.com")] == ["US-AZ-Phoenix"]


def test_an_icims_later_page_that_wont_load_keeps_what_was_found():
    """Page 2 of 4 timing out ended the whole wording's search with an error, throwing away
    the openings already read; a first page that won't load is still an error."""
    from job_apply.search import icims_search

    async def frames_html(url):
        page = int(parse_qs(urlsplit(url).query).get("pr", ["0"])[0])
        if page == 2:
            raise TimeoutError("page 3 didn't load")
        return [_icims_results([("Recruiter", f"US-AZ-Page{page}")], page, 3)]

    found = []
    asyncio.run(icims_search(frames_html, "careers-x", "recruiter", found))
    assert [r.location for r in found] == ["US-AZ-Page0", "US-AZ-Page1"]

    async def down(url):
        raise TimeoutError("the portal is down")

    with pytest.raises(TimeoutError):
        asyncio.run(icims_search(down, "careers-x", "recruiter", []))


def test_a_taleo_career_section_is_searched_with_its_own_pages_request():
    """Kforce's internal jobs (a Taleo career section, Oct 2026): the page's own JSON search,
    which answers HTTP 500 without its time zone headers. Each row's columns are the title, its
    places ("Arizona-Phoenix", as a JSON list in a string) and the date posted. Its total can be
    more than it lists, so a short page ends the reading."""
    asked = []

    def row(i, place, when="Oct 7, 2026"):
        return {"jobId": str(9000 + i), "contestNo": str(26000 + i), "linkedColumn": 0, "locationsColumns": [1],
                "column": [f"Recruiter {i}", json.dumps([place]), when]}

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.headers.get("tz") and request.headers.get("tzname")
        assert str(request.url) == "https://myhiring.kco.com/careersection/rest/jobboard/searchjobs?lang=en&portal=101"
        body = json.loads(request.content)
        asked.append((body["fieldData"]["fields"]["KEYWORD"], body["pageNo"]))
        rows = [row(i, "Texas-Dallas") for i in range(25)] if body["pageNo"] == 1 else [
            row(30, "Arizona-Phoenix"), row(31, "Florida-EE Specific City - WFH", "Sep 1, 2026")]
        return httpx.Response(200, json={"requisitionList": rows,
                                         "pagingData": {"currentPageNo": body["pageNo"], "pageSize": 25, "totalCount": 90}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("recruiter", location="AZ", client=client, companies=[
                {"name": "K Co", "search": {"taleo": {"host": "myhiring.kco.com", "section": "ex", "portal": "101"}}}])
    out = asyncio.run(go())
    assert asked == [("recruiter", 1), ("recruiter", 2)]
    assert [(r["title"], r["location"], r["posted"], r["url"]) for r in out["results"]] == [
        ("Recruiter 30", "Phoenix, Arizona", "2026-10-07",
         "https://myhiring.kco.com/careersection/ex/jobdetail.ftl?job=26030&lang=en")]
    assert not out["errors"]


VALLEYWISE_PAGE = """<div class="jobs-section__item p-3"><div class="row"><div class="col-12">
<h4><a href="https://jobs.vwco.org/jobs/2231639-team-specialist">Team Specialist</a></h4>
<a class="btn" href="#" onclick="return pmApplyURL('/jobs/2231639-team-specialist/record_apply_start_return_url');">Apply Now</a>
<div class="row"><div class="col-xs-12 col-sm-6 jobcardtext"><i class="fas fa-map-marker" title="Location"></i>
 Mesa, AZ, United States </div><div class="col-xs-12 col-sm-6 jobcardtext"><i class="fas fa-sitemap" title="Department"></i>
 BH Specialty Clinic - Mesa </div></div></div></div></div>
<div class="jobs-section__item p-3"><h4><a href="/jobs/2230959-hr-specialist">HR Specialist</a></h4>
<div class="jobcardtext"><i class="fas fa-map-marker"></i> Phoenix, AZ, United States</div></div>"""


def test_a_talemetry_job_site_is_read_page_by_page():
    """Valleywise Health's Symplr (Talemetry) job site: results drawn on the server, 25 a page,
    each with its place beside a map marker."""
    from job_apply.search import parse_talemetry

    rows = parse_talemetry(VALLEYWISE_PAGE, "https://jobs.vwco.org")
    assert [(r.title, r.location, r.url, r.external_id) for r in rows] == [
        ("Team Specialist", "Mesa, AZ, United States", "https://jobs.vwco.org/jobs/2231639-team-specialist", "2231639"),
        ("HR Specialist", "Phoenix, AZ, United States", "https://jobs.vwco.org/jobs/2230959-hr-specialist", "2230959")]
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, text=VALLEYWISE_PAGE)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("specialist", location="AZ", client=client, companies=[
                {"name": "VW Co", "search": {"talemetry": "https://jobs.vwco.org"}}])
    out = asyncio.run(go())
    assert asked == ["https://jobs.vwco.org/jobs/search?q=specialist&page=1"]  # a short page: no more to read
    assert [r["title"] for r in out["results"]] == ["HR Specialist", "Team Specialist"]


PCH_PAGE = """<div class="blog-item"><div class="row position"><div class="col-12 col-lg-9 blog-content">
<h2><a href="/Positions/Posting/1064100">Talent Acquisition Coordinator</a></h2>
<div class="d-block d-lg-none mb-3">Recruitment | Full-Time | Phoenix<br/></div>
<article><strong>Posting Note:</strong> Join our recruiting team.</article></div></div></div>
<div class="blog-item"><h2><a href="/Positions/Posting/982403">Allergist Immunologist</a></h2>
<div class="d-block">Allergy and Immunology | Full-Time | Glendale</div></div>
<div class="blog-item"><h2><a href="/Positions/Posting/990001">Recruiter</a></h2>
<div class="d-block">Recruitment | Full-Time | Remote</div></div>"""


def test_phoenix_childrens_board_is_read_whole_and_filtered_by_title():
    """Phoenix Children's own job site lists every opening (about 300) on one page, each with
    "department | schedule | place"; its places are Phoenix-area towns."""
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, text=PCH_PAGE)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("talent acquisition | recruiter", location="AZ", client=client, companies=[
                {"name": "PCH Co", "search": {"phoenixchildrens": "https://careers.pchco.org"}}])
    out = asyncio.run(go())
    assert asked == ["https://careers.pchco.org/Positions/"]  # the whole board, once
    assert [(r["title"], r["location"], r["url"]) for r in out["results"]] == [
        ("Recruiter", "Remote", "https://careers.pchco.org/Positions/Posting/990001"),  # (a remote one: no town)
        ("Talent Acquisition Coordinator", "Phoenix, AZ", "https://careers.pchco.org/Positions/Posting/1064100")]


def test_a_taleo_rows_odd_columns():
    """Another Taleo site's rows: a place as plain text, not a JSON list; a row without its
    title column named."""
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"requisitionList": [
            {"contestNo": "1", "linkedColumn": None, "locationsColumns": [1],
             "column": ["Recruiter", "Arizona-Tempe", "Sep 30, 2026"]},
            {"contestNo": "2", "linkedColumn": 0, "locationsColumns": [1, 7], "column": ["HR Generalist", '"Texas-Dallas"']}],
            "pagingData": {"pageSize": 25}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_module._taleo(client, {"host": "jobs.tco.com", "portal": "1"}, "", 20, [])
    found = asyncio.run(go())
    assert [(x.title, x.location, x.posted) for x in found] == [
        ("Recruiter", "Tempe, Arizona", "2026-09-30"), ("HR Generalist", "Dallas, Texas", "")]


def test_a_jibe_site_is_searched_in_one_state_and_links_to_its_icims_postings():
    """iCIMS's Jibe job sites (Sprouts', PetSmart's, State Farm's; Oct 2026) answer their own
    page's JSON search, filtered to a state by name. An opening applied for on an iCIMS portal
    links to its posting there (where the desk applies); another (PetSmart's store jobs, on
    Cadient) to its page on the Jibe site."""
    asked = []

    def job(slug, title, where, apply_url, posted="2026-09-02T17:49:00+0000"):
        return {"data": {"slug": slug, "req_id": slug, "title": title, "full_location": where, "posted_date": posted,
                         "apply_url": apply_url}}

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        return httpx.Response(200, json={"totalCount": 2, "jobs": [
            job("374999", "Manager, Human Resources", "Phoenix, Arizona",
                "https://storesupport-sprouts.icims.com/jobs/374999/login"),
            job("6016-1213", "Retail Store Manager", "Casa Grande, Arizona",
                "https://cta.cadienttalent.com/index.jsp?POSTING_ID=6016")]})

    async def go(location):
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("human resources", location=location, client=client, companies=[
                {"name": "J Co", "search": {"jibe": "jobs.jco.com"}}])
    out = asyncio.run(go("AZ"))
    assert asked == [{"keywords": "human resources", "page": "1", "limit": "100", "location": "Arizona"}]
    assert [(r["title"], r["location"], r["posted"], r["url"], r.get("company_url")) for r in out["results"]] == [
        ("Manager, Human Resources", "Phoenix, Arizona", "2026-09-02",
         "https://storesupport-sprouts.icims.com/jobs/374999/job", "https://jobs.jco.com/jobs/374999?lang=en-us"),
        ("Retail Store Manager", "Casa Grande, Arizona", "2026-09-02",
         "https://jobs.jco.com/jobs/6016-1213?lang=en-us", None)]
    asked.clear()
    asyncio.run(go(None))
    assert "location" not in asked[0]  # anywhere


JOBVITE_PAGE = """<table><tbody>
<div class="jv-job-item" onclick="window.location.href='/kco/job/oWzEAfwt'">
<div class="jv-job-list-name"><a href="/kco/job/oWzEAfwt">Transportation Recruiter</a></div>
<div class="jv-job-list-location">
            Phoenix,
            Arizona
            </div></div>
<div class="jv-job-item"><div class="jv-job-list-name"><a href="/kco/job/o2pNAfwy">Senior Financial Planning Analyst</a></div>
<div class="jv-job-list-location"> Phoenix, Arizona </div></div>
</tbody></table>"""


def test_a_jobvite_board_is_read_whole_and_filtered_by_title():
    """Knight-Swift's Jobvite board lists every office opening on one page, each with its place."""
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, text=JOBVITE_PAGE)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("recruiter | talent acquisition", location="AZ", client=client, companies=[
                {"name": "K Co", "search": {"jobvite": "kco"}}])
    out = asyncio.run(go())
    assert asked == ["https://jobs.jobvite.com/kco/jobs"]
    assert [(r["title"], r["location"], r["url"]) for r in out["results"]] == [
        ("Transportation Recruiter", "Phoenix, Arizona", "https://jobs.jobvite.com/kco/job/oWzEAfwt")]


def test_amazon_jobs_is_searched_around_a_place():
    """amazon.jobs answers its search page's JSON search around a place (its address, latitude,
    longitude and radius); without them it searches the world (Tokyo's recruiters first). Each
    opening names every place it's offered in: more than half of its Phoenix-area managers'
    openings were in Seattle or Bellevue first, and in Tempe too (live, Oct 2026)."""
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        def place(town, state):
            return json.dumps({"city": town, "normalizedStateName": state, "normalizedCountryName": "United States"})
        return httpx.Response(200, json={"hits": 3, "jobs": [
            {"title": "HR Business Partner", "job_path": "/en/jobs/10493265/hr-business-partner",
             "normalized_location": "Phoenix, Arizona, USA", "posted_date": "August  5, 2026", "id_icims": "10493265",
             "locations": [place("Phoenix", "Arizona"), place("PHOENIX", "Arizona")]},  # (one place)
            {"title": "HR Manager", "job_path": "/en/jobs/2/hr-manager", "normalized_location": "Seattle, Washington, USA",
             "posted_date": "Sep 24, 2026", "locations": [place("Seattle", "Washington"), place("Tempe", "Arizona")]},
            {"title": "Recruiter", "job_path": "/en/jobs/1/recruiter", "normalized_location": "Seattle, Washington, USA",
             "posted_date": "Sep 24, 2026", "locations": [place("Seattle", "Washington")]}]})

    async def go(location):
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("human resources | hr business partner", location=location, client=client,
                                          companies=[{"name": "A Co", "search": {"amazon": {
                                              "loc_query": "Phoenix, AZ, USA", "latitude": 33.44825,
                                              "longitude": -112.0758, "radius": "50km"}}}])
    out = asyncio.run(go("AZ"))
    assert asked[0]["loc_query"] == "Phoenix, AZ, USA" and asked[0]["radius"] == "50km"
    assert asked[0]["latitude"] == "33.44825" and asked[0]["base_query"] == "human resources"
    assert [(r["title"], r["location"], r["posted"], r["url"]) for r in out["results"]] == [
        ("HR Business Partner", "Phoenix, Arizona", "2026-08-05",
         "https://www.amazon.jobs/en/jobs/10493265/hr-business-partner"),
        ("HR Manager", "Seattle, Washington; Tempe, Arizona", "2026-09-24",  # a Seattle job offered in Tempe too
         "https://www.amazon.jobs/en/jobs/2/hr-manager")]  # Seattle's alone left out
    asked.clear()
    asyncio.run(go(None))
    assert "loc_query" not in asked[0] and "latitude" not in asked[0]  # anywhere: the world


def test_a_jobvite_boards_longer_categories_are_read_past_their_first_twenty():
    """Knight-Swift's board lists 20 openings a category; its Operations and Shop categories go
    on, through "Show More", on search pages of their own (31 of 97 openings, Oct 2026)."""
    board = JOBVITE_PAGE.replace("</tbody></table>", '<a href="/kco/search?c=Shop&amp;p=0">Show More</a></tbody></table>')
    shop = """<table class="jv-job-list jv-search-list"><tr><td class="jv-job-list-name">
    <a href="/kco/job/oShop1">Shop Recruiter</a></td><td class="jv-job-list-location"> Laredo, Texas </td></tr>
    <tr><td class="jv-job-list-name"><a href="/kco/job/oShop2">Diesel Technician</a></td>
    <td class="jv-job-list-location"> Phoenix, Arizona </td></tr></table>"""
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, text=shop if "search" in str(request.url) else board)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("recruiter", location=None, client=client, companies=[
                {"name": "K Co", "search": {"jobvite": "kco"}}])
    out = asyncio.run(go())
    assert asked == ["https://jobs.jobvite.com/kco/jobs", "https://jobs.jobvite.com/kco/search?c=Shop&p=0"]
    assert [(r["title"], r["location"]) for r in out["results"]] == [
        ("Shop Recruiter", "Laredo, Texas"), ("Transportation Recruiter", "Phoenix, Arizona")]


def test_a_jibe_search_not_in_one_state_reads_further_and_takes_an_address():
    """Not one state ("Phoenix | Remote"): PetSmart's national list has its few Arizona openings
    anywhere in its 1900, so more of it is read. A full address in the employer list works too."""
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.host, dict(request.url.params)))
        jobs = [{"data": {"slug": f"{len(asked)}-{i}", "title": "Recruiter", "full_location": "Dallas, Texas"}}
                for i in range(100)]
        return httpx.Response(200, json={"jobs": jobs})

    async def go(location):
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("recruiter", location=location, client=client, companies=[
                {"name": "J Co", "search": {"jibe": "https://careers.jco.com/"}}])
    asyncio.run(go("Phoenix | Remote"))
    assert [h for h, _ in asked] == ["careers.jco.com", "careers.jco.com"] and "location" not in asked[0][1]
    asked.clear()
    asyncio.run(go("AZ"))
    assert len(asked) == 1 and asked[0][1]["location"] == "Arizona"  # the site's own filter: one page


SF_TILES = """<div id="tile-search-results-label">Showing 1 to 2 of 2 Jobs</div><ul id="job-tile-list">
<li class="job-tile job-id-1424491000 job-row-index-1" data-url="/job/Tempe-Compensation-Analyst-AZ-85280/1424491000/">
 <div class="sub-section sub-section-desktop"><span class="section-title title" role="heading">
  <a class="jobTitle-link" href="/job/Tempe-Compensation-Analyst-AZ-85280/1424491000/"> Compensation  Analyst </a></span>
  <div class="section-field location" id="job-1424491000-desktop-section-location"><span class="section-label">Location</span>
   <div id="job-1424491000-desktop-section-location-value">Tempe, AZ, US </div></div>
  <div class="section-field date" id="job-1424491000-desktop-section-date"><span class="section-label">Date</span>
   <div id="job-1424491000-desktop-section-date-value">Oct 2, 2026 </div></div></div>
 <div class="sub-section sub-section-tablet">
  <a class="jobTitle-link" href="/job/Tempe-Compensation-Analyst-AZ-85280/1424491000/"> Compensation Analyst </a></div>
</li>
<li class="job-tile job-id-1 job-row-index-2" data-url="/job/Austin-Recruiter-TX/1/">
 <a class="jobTitle-link" href="/job/Austin-Recruiter-TX/1/">Recruiter</a>
 <div class="section-field location"><div id="job-1-desktop-section-location-value">Austin, TX, US</div></div></li>
</ul>"""


def test_a_successfactors_site_that_draws_its_results_as_tiles():
    """Salt River Project's SuccessFactors site draws its search results as tiles, not the table
    rows Qorvo's has: the search found none (Oct 2026). Each tile has its title (twice: desktop
    and tablet), place and date."""
    from job_apply.search import parse_successfactors

    rows, total = parse_successfactors(SF_TILES, "https://careers.srpnet.com")
    assert [(r.title, r.location, r.posted, r.url, r.external_id) for r in rows] == [
        ("Compensation Analyst", "Tempe, AZ, US", "2026-10-02",
         "https://careers.srpnet.com/job/Tempe-Compensation-Analyst-AZ-85280/1424491000/", "1424491000"),
        ("Recruiter", "Austin, TX, US", "", "https://careers.srpnet.com/job/Austin-Recruiter-TX/1/", "1")]
    assert total == 2  # its own label: "Showing 1 to 2 of 2 Jobs"
    town = SF_TILES.replace('<div class="section-field location"><div id="job-1-desktop-section-location-value">Austin, TX, US</div></div>',
                            '<div class="section-field city"><div id="job-1-desktop-section-city-value">Austin</div></div>'
                            '<div class="section-field state"><div id="job-1-desktop-section-state-value">TX</div></div>')
    assert parse_successfactors(town, "https://careers.srpnet.com")[0][1].location == "Austin, TX"  # town and state fields

    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        return httpx.Response(200, text=SF_TILES)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("compensation | recruiter", location="AZ", client=client, companies=[
                {"name": "SRP Co", "search": {"successfactors": "https://careers.srpnet.com"}}])
    out = asyncio.run(go())
    assert [r["title"] for r in out["results"]] == ["Compensation Analyst"]  # Austin's left out
    assert [a["startrow"] for a in asked] == ["0", "0"]  # a page per wording: all 2 of 2 were on it


def test_an_eightfold_site_without_its_newer_search_uses_the_older_one():
    """Insight Enterprises' Eightfold site answers /api/pcsx/search with 403 "PCSX is not enabled
    for this user" (the page never makes that call either); its older /api/apply/v2/jobs answers,
    10 a page, filtered to a state (Oct 2026)."""
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/api/pcsx/search":
            return httpx.Response(403, json={"message": "PCSX is not enabled for this user."})
        start = int(request.url.params["start"])
        positions = [{"id": str(1000 + start + i), "name": f"Sales Manager {start + i}", "locations": ["Chandler,United States"],
                      "t_create": 1791210658, "display_job_id": str(start + i),
                      "canonicalPositionUrl": f"https://ins.eightfold.ai/careers/job/{1000 + start + i}"}
                     for i in range(10 if start == 0 else 2)]
        return httpx.Response(200, json={"count": 12, "positions": positions})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("sales manager", location="AZ", client=client, companies=[
                {"name": "Ins Co", "search": {"eightfold": {"host": "careers.ins.com", "domain": "ins.com"}}}])
    out = asyncio.run(go())
    assert [p for p, _ in asked] == ["/api/pcsx/search", "/api/apply/v2/jobs", "/api/apply/v2/jobs"]
    assert asked[1][1]["location"] == "Arizona" and asked[2][1]["start"] == "10"
    first = out["results"][0]
    assert (first["location"], first["posted"], first["url"]) == (
        "Chandler, United States", "2026-10-05", "https://ins.eightfold.ai/careers/job/1000")
    assert len(out["results"]) == 12 and not out["errors"]


RANDSTAD_PAGE = """<html><body><script>
window.__ROUTE_DATA__ = {"regionCitiesAggregation":[],"searchResults":{"totalSize":2,"hits":[
 {"title":"Industrial Client Development Manager","createdDate":"1791453711239","atsReference":"52520",
  "jobLocation":{"city":"Phoenix","state":"Arizona","stateAbbreviation":"AZ"},
  "detailsUrl":"https://randco.workgr8.com/jobs/52520/industrial-client-development-manager?sid=11117"},
 {"title":"Internal Recruiter","createdDate":"1791453717257","atsReference":"53302",
  "jobLocation":{"city":"Tucson","state":"Arizona","stateAbbreviation":"AZ"},
  "detailsUrl":"https://randco.workgr8.com/jobs/53302/internal-recruiter?sid=11117"}]}};
                  window.__SEO_DATA__ = {"title": "Jobs"};
</script></body></html>"""


def test_randstads_own_jobs_are_read_from_the_pages_data():
    """Randstad lists its internal jobs in the page's own data (window.__ROUTE_DATA__, followed by
    more script), one state's on a page of its own (/jobs/internal/arizona/)."""
    asked = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, text=RANDSTAD_PAGE if "page-2" not in str(request.url) else "<html></html>")

    async def go(location):
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("recruiter", location=location, client=client, companies=[
                {"name": "R Co", "search": {"randstad": "https://www.randco.com/jobs/internal"}}])
    out = asyncio.run(go("AZ"))
    assert asked == ["https://www.randco.com/jobs/internal/arizona/"]
    assert [(r["title"], r["location"], r["posted"], r["url"]) for r in out["results"]] == [
        ("Internal Recruiter", "Tucson, AZ", "2026-10-08", "https://randco.workgr8.com/jobs/53302/internal-recruiter")]
    asked.clear()
    asyncio.run(go(None))
    assert asked == ["https://www.randco.com/jobs/internal/", "https://www.randco.com/jobs/internal/page-2/"]


def test_an_m_cloud_search_is_read_for_its_places():
    """Edward Jones' career site searches through a Google Cloud Talent search at
    jobsapi-google.m-cloud.io: words in, openings from anywhere out (no place filter), 100 a page,
    some listed twice under two ids with one job number (Oct 2026)."""
    asked = []

    def job(n: int, title: str, city: str, state: str, ref: str) -> dict:
        return {"job": {"title": title, "url": f"https://careers.example.com/job/{n}/", "ref": ref,
                        "open_date": "2026-09-11T00:00:00", "primary_city": city, "primary_state": state,
                        "addtnl_locations": [{"addtnl_city": "Saint Louis", "addtnl_state": "MO"}]}}

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(dict(request.url.params))
        if request.url.params["offset"] == "0":
            hits = [job(1, "Senior Financial Analyst III", "Tempe", "AZ", "118370BR"),
                    job(2, "Senior Financial Analyst III", "Tempe", "AZ", "118370BR"),
                    job(3, "Financial Analyst II", "Saint Louis", "MO", "118371BR")]
        else:
            hits = [job(4, "Financial Analyst I", "Tempe", "AZ", "118372BR")]
        return httpx.Response(200, json={"totalHits": 4, "searchResults": hits})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("financial analyst", location="AZ", client=client, companies=[
                {"name": "Example Investments", "search": {"mcloud": {"company": "companies/abc"}}}])
    out = asyncio.run(go())
    assert asked[0]["companyName"] == "companies/abc" and asked[0]["query"] == "financial analyst"
    assert [a["offset"] for a in asked] == ["0", "3"]
    assert [(r["title"], r["location"], r["posted"]) for r in out["results"]] == [
        ("Financial Analyst I", "Tempe, AZ; Saint Louis, MO", "2026-09-11"),
        ("Senior Financial Analyst III", "Tempe, AZ; Saint Louis, MO", "2026-09-11")]
    assert not out["errors"]


def test_employers_are_searched_eight_at_a_time():
    """Find jobs on the Phoenix list (about 50 employers with a search) took about 100 s,
    searching four employers at a time; each is its own site, so eight at once halve it."""
    busy, most = 0, 0

    async def answer(request: httpx.Request) -> httpx.Response:
        nonlocal busy, most
        busy += 1
        most = max(most, busy)
        await asyncio.sleep(0.05)
        busy -= 1
        return httpx.Response(200, json={"jobs": []})

    companies = [{"name": f"Employer {n}", "search": {"greenhouse": f"board{n}"}} for n in range(12)]

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await search_companies("accountant", client=client, companies=companies)
    out = asyncio.run(go())
    assert not out["errors"] and most == 8, (most, out["errors"])


def test_an_employer_with_two_workday_sites_searches_both():
    """PwC lists experienced openings and entry-level ones on two Workday sites: both are searched."""
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        site = "Exp" if "/Exp/" in str(request.url) else "Entry"
        return httpx.Response(200, json={"total": 1, "jobPostings": [{
            "title": f"Audit Associate ({site})", "externalPath": f"/job/AZ-Phoenix/Audit_{site}", "locationsText": "AZ-Phoenix",
            "postedOn": "Posted Today", "bulletFields": [f"{site}1"]}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await search_module._workday(client, ["https://pwc.wd3.myworkdayjobs.com/Exp",
                                                         "https://pwc.wd3.myworkdayjobs.com/Entry"], "audit", 10, [])

    found = asyncio.run(go())
    assert sorted(x.url for x in found) == ["https://pwc.wd3.myworkdayjobs.com/Entry/job/AZ-Phoenix/Audit_Entry",
                                            "https://pwc.wd3.myworkdayjobs.com/Exp/job/AZ-Phoenix/Audit_Exp"]
