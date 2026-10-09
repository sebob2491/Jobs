import json

from conftest import FIXTURES

from job_apply.ats import detect_ats, greenhouse_form_url, greenhouse_parts, lever_parts, linkedin_job_id, workday_parts
from job_apply.postings import finalize, html_to_text, parse_html, place_in_text, public_apply_url


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
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210000001": "oracle_hcm",
        "https://careers.ti.com/en/sites/CX/job/25018065": "oracle_hcm",  # the company's own address for it
        "https://micron.eightfold.ai/careers": "eightfold",
        "https://www.paycomonline.net/v4/ats/web.php/portal/95CACB007211B4A999FBE2ED52E7762E/jobs/389228": "paycom",
        "https://seus.applicantstack.com/x/detail/a2ejxq3cpz4b": "applicantstack",
        "https://recruiting2.ultipro.com/NIK1001NIKON/JobBoard/f11a0b52/OpportunityDetail?opportunityId=532a7dc9": "ukg",
        "https://css-benchmark-prd.inforcloudsuite.com/hcm/Jobs/navigation/JobPosting%5BJobPostingSet%5D%281%2C12460%2C1%29"
        ".JobPostingDisplayNav": "infor",
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


def test_greenhouse_postings_open_at_greenhouses_own_form():
    """asm.com's posting page holds the form in a frame behind its cookie banner."""
    form = "https://job-boards.greenhouse.io/embed/job_app?for=asm&token=4889175101"
    assert greenhouse_form_url("https://job-boards.greenhouse.io/asm/jobs/4889175101") == form
    assert greenhouse_form_url("https://boards.greenhouse.io/asm/jobs/4889175101?gh_src=abc") == form
    assert greenhouse_form_url(form) == form
    assert greenhouse_form_url("https://www.asm.com/open-vacancies/engineer-4889175101?gh_jid=4889175101") is None
    assert greenhouse_form_url("https://evil.example/greenhouse.io/asm/jobs/1") is None


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


def test_an_apply_link_that_only_works_from_the_posting_is_left_out():
    """Edwards' Apply now (SuccessFactors) sends a cold visit to the site's home page, so the
    posting itself is where an application starts."""
    raw = """<html><head><title>Field Service Engineer</title></head><body><main><h1>Field Service Engineer</h1>
    <a class="unify-apply-now dialogApplyBtn" href="/talentcommunity/apply/171942/?locale=en_US">Apply now</a>
    <p>Support customers in Arizona.</p></main></body></html>"""
    p = finalize(parse_html(raw, "https://www.jobs.atlascopcogroup.com/job/Field-Service-Engineer/171942-en_US"))
    assert p.apply_url == ""


def test_a_successfactors_posting_says_where_it_is_under_its_title():
    """Edwards' postings carry no structured data; the lines under the title name the place."""
    token = ('<div class="joblayouttoken displayDTM"><div class="inner"><span class="rtltextaligneligible"{}>{}</span>'
             '</div></div>')
    raw = ("<html><head><title>Onsite Service Engineer AZ</title></head><body><main>"
           + token.format(' itemprop="title"', "Onsite Service Engineer AZ")
           + "".join(token.format("", line) for line in ("Service", " ", "Phoenix AZ", "United States", "On-Site"))
           + "<p>Support customers in Arizona.</p></main></body></html>")
    p = finalize(parse_html(raw, "https://www.jobs.atlascopcogroup.com/job/Onsite-Service-Engineer-AZ/172120-en_US"))
    assert p.location == "Phoenix, AZ"


def test_html_to_text_lists_and_entities():
    text = html_to_text("<p>Hello&nbsp;there</p><ul><li>One</li><li>Two &amp; three</li></ul>")
    assert "Hello there" in text
    assert "- One" in text and "- Two & three" in text


def test_html_to_text_keeps_headings_and_bullets_on_their_own_lines():
    """The ranking reads requirements line by line, and what's under a "Preferred" heading
    differently."""
    # whitespace inside a list item: the bullet stays with its text
    assert html_to_text("<ul><li>\n   Bachelor's degree required\n </li></ul>") == "- Bachelor's degree required"
    # an inline heading before a block is a line of its own
    def lines(raw):
        return [line for line in html_to_text(raw).splitlines() if line]
    assert lines("<div><strong>Preferred Qualifications</strong><p>Experience with vacuum pumps.</p></div>") == [
        "Preferred Qualifications", "Experience with vacuum pumps."]
    # table cells and smaller headings too
    assert lines("<table><tr><td>Location</td><td>Phoenix, AZ</td></tr></table>") == ["Location", "Phoenix, AZ"]
    assert lines("<h5>Requirements</h5>High school diploma") == ["Requirements", "High school diploma"]


def test_postings_written_into_the_page_differently():
    # JSON-LD whose description was escaped twice
    ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Technician",
          "description": "&lt;p&gt;Requirements&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Associate degree&lt;/li&gt;&lt;/ul&gt;"}
    raw = f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body></body></html>'
    p = parse_html(raw, "https://jobs.example.com/1")
    assert [line for line in p.description.splitlines() if line] == ["Requirements", "- Associate degree"]
    # UKG Pro (Nikon) puts the posting in a script call
    detail = {"Title": "Field Service Engineer", "RequisitionNumber": "FIELD001451", "PostedDate": "2026-07-08T21:04:44Z",
              "Description": "<p><strong>Requirements</strong></p><ul><li>Must be a U.S. person (ITAR).</li></ul>",
              "Locations": [{"LocalizedDescription": "Arizona", "Address": {"City": "Chandler", "State": {"Code": "AZ"}}},
                            {"LocalizedDescription": "Phoenix, AZ", "Address": {"City": None, "State": {"Code": "AZ"}}}]}
    raw = ("<html><body><div id='app'></div><script>var opportunity = new US.Opportunity.CandidateOpportunityDetail("
           + json.dumps(detail) + ");</script></body></html>")
    p = finalize(parse_html(raw, "https://recruiting2.ultipro.com/NIK1001NIKON/JobBoard/f11a/OpportunityDetail?opportunityId=1"))
    assert p.title == "Field Service Engineer" and p.location == "Chandler, AZ; Phoenix, AZ" and p.parse_method == "ukg-page"
    assert "- Must be a U.S. person (ITAR)." in p.description and p.ats == "ukg"


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


def test_an_older_successfactors_posting_names_its_place_in_its_text():
    """Amkor's postings (SuccessFactors' older career site) have no place field; the description
    says where the job is. The page's title carries the requisition number."""
    page = ("<html><head><title>Career Opportunities: Equipment Technician (ATA) (29111)</title></head><body>"
            "<div>Requisition ID 29111 - Posted 10/01/2026 - Engineering</div><div>Job Description</div>"
            "<p>Amkor Technology, Inc. leads in packaging. This position is based at our Peoria, Arizona factory.</p>"
            "</body></html>")
    url = "https://career8.successfactors.com/career?career_ns=job_listing&company=amkor&career_job_req_id=29111"
    p = parse_html(page, url)
    assert (p.title, p.location) == ("Equipment Technician (ATA)", "Peoria, AZ")
    assert parse_html(page, "https://www.example.com/news/1").location == ""  # other pages' text isn't guessed at


def test_a_place_is_a_city_and_a_us_state():
    assert place_in_text("based at our corporate headquarters in Tempe, AZ.") == "Tempe, AZ"
    assert place_in_text("Work from New York, NY or Salt Lake City, UT") == "New York, NY"
    assert place_in_text("Amkor Technology, Inc. (Nasdaq: AMKR) builds packages") == ""
    assert place_in_text("Openings in Arizona, USA") == ""


def test_a_job_system_is_known_by_its_host_not_by_words_elsewhere_in_the_address():
    """A lookalike address isn't a job system: its sign-up mail, auto-submit setting and
    shared-site rules don't apply to it. An employer's own address for one is known."""
    for lookalike in ("https://evil.example/myworkdayjobs.com/x", "https://myworkdayjobs.com.evil.example/x",
                      "https://evilgreenhouse.io/a", "https://notsuccessfactors.com/x", "https://example.com/?ref=linkedin.com"):
        assert detect_ats(lookalike) == "company_site", lookalike
    assert detect_ats("https://AMAT.WD1.MYWORKDAYJOBS.COM:443/External") == "workday"
    assert detect_ats("https://lnkd.in/abc") == "linkedin" and detect_ats("https://uk.indeed.com/viewjob?jk=1") == "indeed"
    assert detect_ats("https://www.indeed.co.uk/viewjob?jk=1") == "indeed"
    for own, ats in (("https://careers.lamresearch.com/careers/job/1", "eightfold"),
                     ("https://jobs.infineon.com/careers/job/2", "eightfold"),
                     ("https://careers.qorvo.com/job/Greensboro/x/3/", "successfactors"),
                     ("https://www.jobs.atlascopcogroup.com/job/Chandler-FSE-AZ-85226/4/", "successfactors")):
        assert detect_ats(own) == ats, own
    assert detect_ats("https://careers.ti.com/en/sites/CX/job/123") == "oracle_hcm"  # its Oracle site, by its path


def test_url_helpers_take_any_case_and_a_link_from_part_way_through():
    assert workday_parts("https://AMAT.wd1.myworkdayjobs.com/en-US/External/job/Phoenix/FSE_R1/apply/applyManually") == {
        "host": "amat.wd1.myworkdayjobs.com", "tenant": "amat", "site": "External", "job_path": "/job/Phoenix/FSE_R1"}
    assert workday_parts("https://evil.example/myworkdayjobs.com/External/job/x") is None
    assert greenhouse_form_url("https://BOARDS.GREENHOUSE.IO/asm/jobs/123") == \
        "https://job-boards.greenhouse.io/embed/job_app?for=asm&token=123"
    assert linkedin_job_id("https://WWW.LINKEDIN.COM/JOBS/VIEW/field-service-4012345678") == "4012345678"


def test_an_oracle_site_on_a_companys_own_address_under_its_hcmui_path():
    assert detect_ats("https://careers.ti.com/hcmUI/CandidateExperience/en/sites/CX/job/25018065") == "oracle_hcm"
    assert detect_ats("https://careers.ti.com/hcmUI/CandidateExperience/en/sites/CX/job/25018065/apply/email") \
        == "oracle_hcm"


def test_job_systems_on_the_phoenix_employers_own_addresses():
    """So their emailed codes count: the system's mail, while the page is on the employer's host."""
    for url, ats in (("https://careers.aps.com/job/Phoenix-HR-Generalist/1/", "successfactors"),
                     ("https://careers.srpnet.com/job/Tempe-Recruiter/2/", "successfactors"),
                     ("https://jobs.northropgrumman.com/careers/job/3", "eightfold"),
                     ("https://careers.insight.com/careers/job/4", "eightfold")):
        assert detect_ats(url) == ats, url


def test_a_menus_link_to_the_job_list_isnt_the_postings_apply_link():
    """Phoenix Children's postings (live, Oct 2026): the menu's "Browse & Apply" goes to the whole
    job list, and the posting's own "Apply!" to its form on the same page. Taken for the apply
    link, the list sent the desk to the first posting on it: another job."""
    page = ("<html><head><title>Talent Acquisition Coordinator</title></head><body>"
            '<nav><a href="/Positions/">Browse &amp; Apply</a><a href="/Positions/Search">Search and apply</a></nav>'
            '<main><h2>Talent Acquisition Coordinator</h2><a href="#apply">Apply!</a><p>Coordinate interviews.</p>'
            '<form id="apply" action="/Position/Apply" method="post"><input name="FirstName"></form></main></body></html>')
    p = parse_html(page, "https://careers.pchco.org/Positions/Posting/1064100")
    assert p.apply_url == ""  # applied on the posting's own page
    page = page.replace('<a href="#apply">Apply!</a>', '<a href="https://apply.example.com/job/1064100" '
                        'aria-label="Apply for Executive Search Consultant">Apply</a>')  # "search" in its title
    assert parse_html(page, "https://careers.pchco.org/Positions/Posting/1064100").apply_url == \
        "https://apply.example.com/job/1064100"


def test_an_apply_link_to_a_staff_only_site_goes_to_the_public_one():
    """Edward Jones' home-office postings (live, Oct 2026) link Apply to its staff Brassring site
    (siteid 5377), whose sign-in ends on a host the public can't reach; the same job is on its
    public site (5374). Its branch postings already link there, and other Brassring sites are
    left alone."""
    page = ('<html><head><title>Senior Financial Analyst III</title></head><body><h1>Senior Financial Analyst III</h1>'
            '<p>Tempe, AZ</p><a href="https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?PageType=JobDetails'
            '&amp;partnerid=26235&amp;siteid=5377&amp;jobid=1427502&amp;gqid=0&amp;al=1">Apply</a></body></html>')
    posting = finalize(parse_html(page, "https://careers.example.com/job/1/senior-financial-analyst-iii/"))
    assert "partnerid=26235&siteid=5374&jobid=1427502" in posting.apply_url, posting.apply_url
    other = "https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=99999&siteid=5377&jobid=1"
    assert public_apply_url(other) == other


def test_a_postings_microdata_date_is_read():
    """EY's SuccessFactors postings give their date as microdata, not JSON-LD
    (itemprop="datePosted" content="Fri Oct 02 00:00:00 UTC 2026"), and Find jobs had none for
    them; read, it counts toward "posted this week" like the others' dates."""
    page = ('<html><head><title>Indirect Tax Analyst</title><meta itemprop="datePosted" '
            'content="Fri Oct 02 00:00:00 UTC 2026"></head><body><h1>Indirect Tax Analyst</h1>'
            '<p>Phoenix, AZ</p></body></html>')
    assert finalize(parse_html(page, "https://careers.example.com/job/1/")).posted_at == "2026-10-02"
    shown = page.replace('<meta itemprop="datePosted" content="Fri Oct 02 00:00:00 UTC 2026">', "").replace(
        "<p>Phoenix", '<span itemprop="datePosted">Oct 2, 2026</span><p>Phoenix')
    assert parse_html(shown, "https://careers.example.com/job/1/").posted_at == "2026-10-02"
    assert parse_html(page.replace("Fri Oct 02 00:00:00 UTC 2026", "soon"), "https://e.example/").posted_at == ""
