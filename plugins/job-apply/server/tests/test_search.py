"""search_company_jobs against canned responses shaped like each ATS's public API.
The shapes follow what the live smoke test (scripts/live_smoke.py) saw on real sites."""

import asyncio
import json

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
    if url.startswith("https://careers-icco.icims.com/jobs/search"):
        assert request.url.params["in_iframe"] == "1" and request.url.params["searchKeyword"]
        return httpx.Response(200, text=ICIMS_PAGE)
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
    assert location_terms("Phoenix|Chandler") == ["phoenix", "chandler"]
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
    assert eightfold_page_url({"host": "careers.x.com", "domain": "x.com"}, "field service | equipment", "AZ") == \
        "https://careers.x.com/careers?query=field+service+equipment&domain=x.com&location=Arizona"


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
    assert [(r["title"], r["location"], r["posted"], r["url"]) for r in by_company["iCIMS Co"]] == [
        ("Field Service Engineer 1", "US-AZ-Chandler", "2026-09-24",
         "https://careers-icco.icims.com/jobs/19224/field-service-engineer-1/job")]

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
            return sr, gh, orc

    sr, gh, orc = asyncio.run(go())
    assert orc.parse_method == "oracle-api" and orc.title == "Equipment Technician"
    assert "Maintain probers." in orc.description and "- AAS" in orc.description and orc.ats == "oracle_hcm"
    assert sr.parse_method == "smartrecruiters-api" and sr.company == "ASML" and sr.location == "Chandler, AZ, US"
    assert "Lead EUV installs." in sr.description and "- 5 years" in sr.description
    assert sr.ats == "smartrecruiters"
    # the hosted Greenhouse page always has the form, unlike the employer's embedding page
    assert gh.apply_url == "https://job-boards.greenhouse.io/asm/jobs/4885531101"


def test_tool_marks_tracked_jobs(srv, monkeypatch):
    import job_apply.server as server

    async def fake(query, names, location, limit):
        return {"results": [{"company": "Lever Co", "title": "FSE", "url": "https://jobs.lever.co/leverco/abc"}],
                "errors": {}, "browser_only": []}

    monkeypatch.setattr(server, "search_companies", fake)
    srv.add_job(url="https://jobs.lever.co/leverco/abc", title="FSE", company="Lever Co")
    out = asyncio.run(server.search_company_jobs("field service"))
    assert out["count"] == 1 and out["results"][0]["tracked"]["status"] == "saved"


def test_tool_falls_back_to_browser_for_eightfold(srv, monkeypatch):
    import job_apply.server as server

    async def fake(query, names, location, limit):
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
