from conftest import FIXTURES

from job_apply.ats import detect_ats, greenhouse_parts, lever_parts, linkedin_job_id, workday_parts
from job_apply.postings import finalize, html_to_text, parse_html


def test_detect_ats():
    cases = {
        "https://www.linkedin.com/jobs/view/3900000001/": "linkedin",
        "https://www.indeed.com/viewjob?jk=abc123": "indeed",
        "https://smartapply.indeed.com/beta/indeedapply/form/questions/1": "indeed",
        "https://amat.wd1.myworkdayjobs.com/en-US/External/job/AGS-SAM_R2618358": "workday",
        "https://job-boards.greenhouse.io/asm/jobs/4932681101": "greenhouse",
        "https://jobs.lever.co/acme/0b1c2d3e-0000-1111-2222-333344445555": "lever",
        "https://career8.successfactors.com/career?company=amkor": "successfactors",
        "https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs": "oracle_hcm",
        "https://micron.eightfold.ai/careers": "eightfold",
        "https://www.example-semi.com/careers/find-your-job/field-service-engineer-j00012345": "company_site",
        None: "company_site",
    }
    for url, ats in cases.items():
        assert detect_ats(url) == ats, url


def test_url_parts():
    wd = workday_parts("https://kla.wd1.myworkdayjobs.com/en-US/Search/job/phoenix-az/customer-support-engineer_2530675/apply")
    assert wd == {"host": "kla.wd1.myworkdayjobs.com", "tenant": "kla", "site": "Search",
                  "job_path": "/job/phoenix-az/customer-support-engineer_2530675"}
    assert workday_parts("https://example.com/job/1") is None
    assert greenhouse_parts("https://job-boards.greenhouse.io/asm/jobs/4932681101") == {"board": "asm", "job_id": "4932681101"}
    assert lever_parts("https://jobs.lever.co/acme/0b1c2d3e-0000-1111-2222-333344445555/apply")["company"] == "acme"
    assert linkedin_job_id("https://www.linkedin.com/jobs/view/3900000001/?trk=abc") == "3900000001"
    assert linkedin_job_id("https://www.linkedin.com/jobs/search/?currentJobId=3900000002&f_TPR=r86400") == "3900000002"


def test_parse_jsonld_posting():
    url = "https://careers.example.com/jobs/fse-euv"
    p = finalize(parse_html((FIXTURES / "jsonld_posting.html").read_text(), url))
    assert p.parse_method == "json-ld"
    assert p.title == "Field Service Engineer – EUV"
    assert p.company == "Example Litho"
    assert p.location == "Phoenix, AZ, US"
    assert p.external_id == "J-00012345"
    assert p.salary == "USD 80000-110000 per year"
    assert "- Bachelor's degree" in p.description
    # the Apply button leads to Workday, so that's where the form gets filled
    assert p.apply_url.startswith("https://examplelitho.wd3.myworkdayjobs.com/")
    assert p.ats == "workday"
    assert p.source == "company_site"


def test_parse_without_structured_data():
    raw = """<html><head><title>Equipment Technician | Example Fab</title></head>
    <body><nav>Home</nav><main><h1>Equipment Technician</h1><p>Maintain etch tools.</p></main></body></html>"""
    p = finalize(parse_html(raw, "https://jobs.example.com/123"))
    assert p.title == "Equipment Technician"
    assert p.company == "Example Fab"
    assert "Maintain etch tools." in p.description
    assert "Home" not in p.description
    assert any("incomplete" in w for w in p.warnings)


def test_html_to_text_lists_and_entities():
    text = html_to_text("<p>Hello&nbsp;there</p><ul><li>One</li><li>Two &amp; three</li></ul>")
    assert "Hello there" in text
    assert "- One" in text and "- Two & three" in text


def test_fetch_posting_over_http():
    import asyncio
    import functools
    import http.server
    import threading

    from job_apply.postings import fetch_posting

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/jsonld_posting.html"
        p = asyncio.run(fetch_posting(url))
    finally:
        httpd.shutdown()
    assert p.title == "Field Service Engineer – EUV"
    assert p.ats == "workday" and p.source == "company_site"
