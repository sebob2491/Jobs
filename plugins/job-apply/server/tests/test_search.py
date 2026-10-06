"""search_company_jobs against canned responses shaped like each ATS's public API.
(tests/test_live_sites.py checks the real endpoints when network access allows.)"""

import asyncio
import json

import httpx

from job_apply.search import alternatives, location_matches, location_terms, search_companies, title_matches

COMPANIES = [
    {"name": "Workday Co", "search": {"workday": "https://wdco.wd1.myworkdayjobs.com/External"}},
    {"name": "Greenhouse Co", "search": {"greenhouse": "ghco"}},
    {"name": "Lever Co", "search": {"lever": "leverco"}},
    {"name": "Eightfold Co", "search": {"eightfold": {"host": "careers.efco.com", "domain": "efco.com"}}},
    {"name": "SR Co", "search": {"smartrecruiters": "SRCO1"}},
    {"name": "Oracle Co", "search": {"oracle": {"host": "abcd.fa.us2.oraclecloud.com", "site": "CX_1"}}},
    {"name": "Browser Co", "careers_url": "https://careers.browserco.com"},
    {"name": "Broken Co", "search": {"greenhouse": "broken"}},
]
seen: list[httpx.Request] = []


def handler(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    url = str(request.url)
    if url.startswith("https://wdco.wd1.myworkdayjobs.com/wday/cxs/wdco/External/jobs"):
        body = json.loads(request.content)
        assert body["limit"] <= 20 and "searchText" in body
        page = [
            {"title": f"Field Service Engineer {body['offset'] + i}", "externalPath": f"/job/Chandler-AZ/FSE_R{body['offset'] + i}",
             "locationsText": "Chandler, AZ" if i % 2 == 0 else "Hillsboro, OR", "postedOn": "Posted Today",
             "bulletFields": [f"R{body['offset'] + i}"]}
            for i in range(20)
        ] if body["offset"] < 40 else []
        return httpx.Response(200, json={"total": 40, "jobPostings": page})
    if url == "https://boards-api.greenhouse.io/v1/boards/ghco/jobs":
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Field Service Engineer II", "absolute_url": "https://job-boards.greenhouse.io/ghco/jobs/1",
             "location": {"name": "Phoenix, Arizona, United States"}, "updated_at": "2026-10-01T00:00:00Z"},
            {"id": 2, "title": "Accountant", "absolute_url": "https://job-boards.greenhouse.io/ghco/jobs/2",
             "location": {"name": "Phoenix, AZ"}},
            {"id": 3, "title": "Equipment Engineering Technician", "absolute_url": "https://job-boards.greenhouse.io/ghco/jobs/3",
             "location": {"name": "Multiple Locations"}},
        ]})
    if url.startswith("https://api.lever.co/v0/postings/leverco"):
        return httpx.Response(200, json=[
            {"id": "abc", "text": "Field Service Engineer", "hostedUrl": "https://jobs.lever.co/leverco/abc",
             "categories": {"location": "Tempe, AZ"}, "createdAt": 1790000000000},
            {"id": "def", "text": "Field Service Engineer", "hostedUrl": "https://jobs.lever.co/leverco/def",
             "categories": {"location": "Austin, TX"}},
        ])
    if url.startswith("https://careers.efco.com/api/apply/v2/jobs"):
        start = int(request.url.params["start"])
        positions = [
            {"id": 100 + start + i, "name": "Field Service Engineer 2", "locations": ["Chandler, AZ, United States"],
             "canonicalPositionUrl": f"https://careers.efco.com/careers/job/{100 + start + i}", "t_create": 1790000000}
            for i in range(10)
        ] if start < 10 else []
        return httpx.Response(200, json={"count": 10, "positions": positions})
    if url.startswith("https://api.smartrecruiters.com/v1/companies/SRCO1/postings"):
        assert request.url.params["q"]
        return httpx.Response(200, json={"totalFound": 1, "content": [
            {"id": "744000001", "name": "Regional Lead - Installs", "refNumber": "REF1", "releasedDate": "2026-09-30T10:00:00Z",
             "location": {"city": "Chandler", "region": "AZ", "country": "us"}}]})
    if url.startswith("https://abcd.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"):
        assert "siteNumber=CX_1" in url and "keyword=%22" in url
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 1, "requisitionList": [
            {"Id": "25011541", "Title": "Equipment Engineer", "PrimaryLocation": "Phoenix, AZ, United States",
             "PostedDate": "2026-09-29"}]}]})
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
    assert location_terms("AZ") == ["az", "arizona"]
    assert location_terms("Phoenix|Chandler") == ["phoenix", "chandler"]
    assert location_terms("arizona") == ["arizona", "az"]
    assert location_matches("US-AZ-Chandler", ["az", "arizona"]) is True
    assert location_matches("Hillsboro, OR", ["az", "arizona"]) is False
    assert location_matches("3 Locations", ["az"]) is None
    assert location_matches("", ["az"]) is None
    assert location_matches("Remote", ["az"]) is None
    assert location_matches("United States", ["az"]) is None


def test_search_all_backends():
    seen.clear()
    out = run_search(query="field service | equipment engineer", location="AZ", limit=20)
    by_company: dict[str, list] = {}
    for r in out["results"]:
        by_company.setdefault(r["company"], []).append(r)

    wd = by_company["Workday Co"]
    assert all(r["location"] == "Chandler, AZ" for r in wd)  # Oregon ones filtered out
    assert wd[0]["url"].startswith("https://wdco.wd1.myworkdayjobs.com/External/job/Chandler-AZ/FSE_R")
    assert len(wd) == 20  # both pages scanned, half in AZ, deduplicated across alternatives

    gh = {r["title"]: r for r in by_company["Greenhouse Co"]}
    assert set(gh) == {"Field Service Engineer II", "Equipment Engineering Technician"}  # accountant dropped
    assert gh["Equipment Engineering Technician"]["notes"] == ["location given as 'Multiple Locations'; check the posting"]

    assert [r["url"] for r in by_company["Lever Co"]] == ["https://jobs.lever.co/leverco/abc"]
    assert by_company["Lever Co"][0]["posted"] == "2026-09-21"
    assert len(by_company["Eightfold Co"]) == 10
    assert by_company["SR Co"][0]["url"] == "https://jobs.smartrecruiters.com/SRCO1/744000001"
    assert by_company["SR Co"][0]["location"] == "Chandler, AZ, US"
    assert by_company["Oracle Co"][0]["url"] == \
        "https://abcd.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/25011541"

    assert out["browser_only"] == [{"company": "Browser Co", "careers_url": "https://careers.browserco.com"}]
    assert out["errors"] == {"Broken Co": "SearchError: HTTP 404 from https://boards-api.greenhouse.io/v1/boards/broken/jobs"}
    # Workday pages until `total` is reached, once per alternative
    wd_calls = [r for r in seen if "myworkdayjobs" in str(r.url)]
    assert len(wd_calls) == 4


def test_company_filter_and_anywhere():
    out = run_search(query="field service", names=["lever"], location=None)
    assert {r["company"] for r in out["results"]} == {"Lever Co"}
    assert len(out["results"]) == 2  # no location filter


def test_tool_marks_tracked_jobs(srv, monkeypatch):
    import job_apply.server as server
    from job_apply import search

    async def fake(query, names, location, limit):
        return {"results": [{"company": "Lever Co", "title": "FSE", "url": "https://jobs.lever.co/leverco/abc"}],
                "errors": {}, "browser_only": []}

    monkeypatch.setattr(server, "search_companies", fake)
    srv.add_job(url="https://jobs.lever.co/leverco/abc", title="FSE", company="Lever Co")
    out = asyncio.run(server.search_company_jobs("field service"))
    assert out["count"] == 1 and out["results"][0]["tracked"]["status"] == "saved"
    assert search.load_companies()  # the shipped list loads
