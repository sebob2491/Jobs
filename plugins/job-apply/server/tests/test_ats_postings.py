import asyncio
import json

import httpx
from conftest import FIXTURES

from job_apply.ats import (csod_parts, detect_ats, greenhouse_form_url, greenhouse_parts, host_of, lever_parts,
                           linkedin_job_id, workday_parts)
from job_apply.postings import (FetchError, Posting, csod_context, fetch_posting, finalize, html_to_text, parse_html,
                                place_in_text, posted_date, public_apply_url)


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
        # (a pod with no region, at a page off the candidate path: its sign-in, the review of Oct 15)
        "https://jpmc.fa.oraclecloud.com/fscmUI/faces/AtkHomePageWelcome": "oracle_hcm",
        "https://careers.ti.com/en/sites/CX/job/25018065": "oracle_hcm",  # the company's own address for it
        "https://micron.eightfold.ai/careers": "eightfold",
        "https://www.paycomonline.net/v4/ats/web.php/portal/95CACB007211B4A999FBE2ED52E7762E/jobs/389228": "paycom",
        "https://seus.applicantstack.com/x/detail/a2ejxq3cpz4b": "applicantstack",
        "https://recruiting2.ultipro.com/NIK1001NIKON/JobBoard/f11a0b52/OpportunityDetail?opportunityId=532a7dc9": "ukg",
        "https://css-benchmark-prd.inforcloudsuite.com/hcm/Jobs/navigation/JobPosting%5BJobPostingSet%5D%281%2C12460%2C1%29"
        ".JobPostingDisplayNav": "infor",
        "https://linde.csod.com/ux/ats/careersite/23/home/requisition/33907?c=linde": "csod",
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
    assert csod_parts("https://linde.csod.com/ux/ats/careersite/23/home?c=linde") == {
        "host": "linde.csod.com", "site": "23", "corp": "linde", "requisition": ""}
    assert csod_parts("https://MathesonGas.csod.com/ux/ats/careersite/1/home/requisition/9402?c=mathesongas") == {
        "host": "mathesongas.csod.com", "site": "1", "corp": "mathesongas", "requisition": "9402"}
    assert csod_parts("https://evil.example/linde.csod.com/ux/ats/careersite/23/home") is None


def test_an_addresses_host_is_read_one_way():
    """The one reader of an address's host the modules share: lowercased, without its port or a user's name and
    password, an IPv6 address without its brackets; "" for no address, or one that doesn't parse."""
    cases = {
        "https://Careers.Example.COM/job/1": "careers.example.com",
        "https://careers.example.com:8443/job/1": "careers.example.com",
        "https://someone:secret@careers.example.com/": "careers.example.com",
        "https://[2001:DB8::1]:8443/job/1": "2001:db8::1",
        "https://[::1": "",  # (an unclosed bracket: urlparse raises ValueError)
        "not an address": "",
        "careers.example.com/job/1": "",  # (no scheme: a path)
        "": "",
        None: "",
    }
    for url, host in cases.items():
        assert host_of(url) == host, url


def test_a_postings_date_is_read_from_every_shape_the_systems_give():
    """The one reader of a posting's date (Paycom's and Cornerstone's m/d/yyyy, SuccessFactors' m/d/yy and written
    dates, Eightfold's and Lever's epochs, EY's microdata), as YYYY-MM-DD; "" for anything else."""
    cases = [
        ("2026-10-01", "2026-10-01"), ("2026-10-01T08:00:00Z", "2026-10-01"), (" 2026-10-01 ", "2026-10-01"),
        (1790000000, "2026-09-21"), (1790000000000, "2026-09-21"), (1790000000.7, "2026-09-21"),  # seconds, ms
        ("10/9/2026", "2026-10-09"), ("09/27/2026", "2026-09-27"), ("10/9/2026 8:00 AM", "2026-10-09"),
        ("7/27/26", "2026-07-27"),
        ("Oct 2, 2026", "2026-10-02"), ("August 5, 2026", "2026-08-05"), ("July  9, 2026", "2026-07-09"),
        ("2 Oct 2026", "2026-10-02"), ("Fri Oct 02 00:00:00 UTC 2026", "2026-10-02"),
        ("Sat Sep 26 07:00:00 EDT 2026", "2026-09-26"), ("2026-7-9", "2026-07-09"),
        # not a date: a place in a date's column, words, nothing, a day that doesn't exist, an epoch that isn't one
        ("Arizona-Tempe", ""), ('["Texas-Dallas"]', ""), ("soon", ""), ("", ""), (None, ""), (" ", ""),
        ("2026-13-45", ""), ("13/45/2026", ""), ("Feb 30, 2026", ""), ("7/27/202", ""), ("2026-10-1234", ""),
        (0, ""), (-5, ""), (True, ""), (10**20, ""),
    ]
    for value, day in cases:
        assert posted_date(value) == day, value


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
    p = finalize(parse_html((FIXTURES / "jsonld_posting.html").read_text(encoding="utf-8"), url))
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
                     ("https://www.jobs.atlascopcogroup.com/job/Chandler-FSE-AZ-85226/4/", "successfactors"),
                     ("https://myhiring.kforce.com/careersection/ex/jobdetail.ftl?job=5", "taleo")):
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
    # a site in another time zone names that one
    assert parse_html(page.replace(" UTC ", " EDT "), "https://e.example/").posted_at == "2026-10-02"


def test_every_listed_own_address_system_is_known():
    """The review of Oct 15: COMPANY_HOSTS held some of the employers whose job system runs on their own address, not
    all, so Rogers', Pfeiffer's, EY's and Freeport's SuccessFactors read as a plain company site (a code from
    SuccessFactors' mail wasn't taken for theirs, and the desk named the site by the employer). Each listed employer
    whose search runs a job system on the employer's own address reads as that system there."""
    from pathlib import Path
    from urllib.parse import urlparse

    import yaml

    data = Path(__file__).resolve().parents[2] / "data"
    missing = []
    for path in [data / "companies.yaml", *sorted((data / "lists").glob("*.yaml"))]:
        for company in yaml.safe_load(path.read_text(encoding="utf-8")).get("companies") or []:
            search = company.get("search") or {}
            own = {"successfactors": search.get("successfactors"),
                   "eightfold": "https://" + search["eightfold"]["host"] if isinstance(search.get("eightfold"), dict)
                   else None}
            for system, url in own.items():
                if isinstance(url, str) and url and detect_ats(url) != system:
                    missing.append(f"{company['name']}: {urlparse(url).hostname} isn't read as {system}")
            if company.get("ats") == "successfactors" and not search \
                    and detect_ats(company.get("careers_url")) != "successfactors":
                missing.append(f"{company['name']}: {company.get('careers_url')} isn't read as successfactors")
    assert not missing, missing


def sf_career_page(scope: str) -> str:
    """A SuccessFactors career site's posting page (EY's, APS', SRP's, Qorvo's: live, Oct 2026), as schema.org
    microdata: a JobPosting scope holding the place, date and employer as meta tags, then the title and the text."""
    return ('<html><head><title>Field Technician Job Details | Example Utility</title></head><body>'
            '<nav><a href="/search/">Search Jobs</a><a href="/talentcommunity/">Join our Talent Community</a></nav>'
            f'<div class="jobDisplayShell" itemscope="itemscope" itemtype="http://schema.org/JobPosting">{scope}</div>'
            '<footer>Equal opportunity employer. Cookie settings.</footer></body></html>')


SF_SCOPE = ('<span itemprop="jobLocation" itemscope="" itemtype="http://schema.org/Place"><span itemprop="address" '
            'itemscope="" itemtype="http://schema.org/PostalAddress"><meta itemprop="addressLocality" content="Gilbert">'
            '<meta itemprop="addressRegion" content="AZ"><meta itemprop="postalCode" content="85233">'
            '<meta itemprop="addressCountry" content="US"></span></span>'
            '<meta itemprop="datePosted" content="Sat Sep 26 07:00:00 UTC 2026">'
            '<meta itemprop="hiringOrganization" content="Example Utility">'
            '<div class="jobDisplay"><h1><span itemprop="title">Field Technician     </span></h1>'
            '<span itemprop="description"><span class="jobdescription"><p>Maintain substation equipment.</p>'
            '<p>Requires two years of electrical experience.</p>'
            + '<p>Inspects, tests and repairs breakers, relays and transformers on a rotating schedule.</p>' * 2
            + '</span></span></div>')


def test_a_postings_microdata_gives_its_place_employer_and_title():
    """Run 116's posting reads: SuccessFactors career sites (EY, APS, SRP, Freeport, Qorvo, Rogers, Pfeiffer) give
    their posting as microdata, not JSON-LD, and the posting came back with no place and no employer (a pasted link
    was saved without them, and Find jobs couldn't check where it is), its title from the page's own title. Read,
    they're the posting's like JSON-LD's, its text the posting's own (not the menu's or the footer's)."""
    url = "https://careers.example.com/job/Gilbert-Field-Technician-AZ-85233/1424499100/"
    p = finalize(parse_html(sf_career_page(SF_SCOPE), url))
    assert (p.title, p.company, p.location, p.posted_at) == \
        ("Field Technician", "Example Utility", "Gilbert, AZ, US", "2026-09-26"), p
    assert p.parse_method == "microdata" and not p.warnings, p.warnings
    assert "substation" in p.description and "Talent Community" not in p.description and "Cookie" not in p.description


def test_a_postings_microdata_in_other_shapes():
    url = "https://careers.example.com/job/1/"
    # an employer given as its own scope, two places, and no text of its own: the page's is kept
    scope = (SF_SCOPE.replace('<meta itemprop="hiringOrganization" content="Example Utility">',
                              '<div itemprop="hiringOrganization" itemscope itemtype="http://schema.org/Organization">'
                              '<span itemprop="name">Example Utility</span></div>')
             .replace('<span itemprop="jobLocation"', '<span itemprop="jobLocation" itemscope><span itemprop="address" '
                      'itemscope><meta itemprop="addressLocality" content="Tempe"><meta itemprop="addressRegion" '
                      'content="AZ"></span></span><span itemprop="jobLocation"', 1)
             .replace('<span itemprop="description">', '<span>'))
    p = parse_html(sf_career_page(scope), url)
    assert (p.company, p.location) == ("Example Utility", "Tempe, AZ; Gilbert, AZ, US"), p
    assert "substation" in p.description
    # one without a title isn't read as the posting (the page's text is), but its place still is
    bare = parse_html(sf_career_page(SF_SCOPE.replace('itemprop="title"', "")), url)
    assert (bare.parse_method, bare.location) == ("page-text", "Gilbert, AZ, US")
    # JSON-LD, where a page has both, is read first
    ld = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "Line Worker", '
          '"hiringOrganization": {"name": "Example Utility"}, "description": "Climb poles."}</script>')
    assert parse_html(sf_career_page(SF_SCOPE).replace("</head>", ld + "</head>"), url).title == "Line Worker"


def test_a_microdata_postings_text_is_its_whole_scope_and_its_own_properties():
    """The review of 0.3.192: its text was only the description property, and Rogers' (live) keeps its shift, its
    travel and its qualifications beside that (4,137 chars of text read as 1,031); a short description with the
    requirements in another property read as "couldn't be read" and was held. An employer given as a scope of its
    own, with a description of its own, gave its description as the job's. A place that names only the country
    hid the one the posting's lines name."""
    url = "https://careers.example.com/job/1/"
    scope = (SF_SCOPE.replace('<meta itemprop="hiringOrganization" content="Example Utility">',
                              '<div itemprop="hiringOrganization" itemscope itemtype="http://schema.org/Organization">'
                              '<span itemprop="description">We keep the lights on.</span>'
                              '<span itemprop="name">Example Utility</span></div>')
             .replace("</div>", '<p>Approximate Travel: 10%</p><span itemprop="qualifications">A bachelor\'s degree in '
                      'electrical engineering is required; ITAR rules apply.</span></div>', 1))
    p = parse_html(sf_career_page(scope), url)
    assert p.company == "Example Utility" and "substation" in p.description, p
    assert "Travel: 10%" in p.description and "ITAR" in p.description, p.description
    assert "Talent Community" not in p.description and "Cookie" not in p.description
    nameless = scope.replace('<span itemprop="name">Example Utility</span>', "Visit our website")
    assert parse_html(sf_career_page(nameless), url).company == ""
    country = (SF_SCOPE.replace('<meta itemprop="addressLocality" content="Gilbert">', "")
               .replace('<meta itemprop="addressRegion" content="AZ">', ""))
    page = sf_career_page(country).replace("<footer>", '<div class="joblayouttoken">Phoenix AZ</div><footer>')
    assert parse_html(page, url).location == "Phoenix, AZ"


# The posting readers' answers (tests/fixtures/postings/<system>.json): each job system's posting API answer, in its
# own shape, with what the desk should read from it ("expect"). The ones here are made up; scripts/live_smoke.py
# --save-postings <dir> records each system's first answer in a search check, scrubbed, in the same form, to replace
# one with once it's looked over (the repo is public). The review of Oct 18 (T5): Workday's reader serves 54 of the 149
# listed employers and had no offline test.
POSTINGS = FIXTURES / "postings"
# each job system's reader (postings._fetch_with's), and the parse_method of a posting it read
READERS = {"workday": ("_fetch_workday", "workday-api"), "greenhouse": ("_fetch_greenhouse", "greenhouse-api"),
           "lever": ("_fetch_lever", "lever-api"), "smartrecruiters": ("_fetch_smartrecruiters", "smartrecruiters-api"),
           "oracle_hcm": ("_fetch_oracle", "oracle-api"), "csod": ("_fetch_csod", "csod-api")}


def replay_posting(fixture: dict) -> Posting:
    """The posting fetch_posting reads at fixture["posting_url"], every request answered from fixture["answers"] (a
    JSON answer, or a page's HTML): a request the fixture has no answer for fails the test."""
    answers = fixture["answers"]

    def answer(request: httpx.Request) -> httpx.Response:
        body = answers.get(str(request.url))
        if body is None:
            raise AssertionError(f"no answer in the fixture for {request.url}")
        return httpx.Response(200, html=body) if isinstance(body, str) else httpx.Response(200, json=body)

    async def go() -> Posting:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            return await fetch_posting(fixture["posting_url"], client=client)

    return asyncio.run(go())


def check_posting(posting: Posting, fixture: dict) -> None:
    """The posting read is the fixture's expect: its title, place, text (each phrase in it) and apply link at least."""
    expect = fixture["expect"]
    assert {"title", "location", "text", "apply_url"} <= set(expect), sorted(expect)
    assert (posting.parse_method, posting.ats) == (READERS[fixture["system"]][1], fixture["system"]), posting
    for key, want in expect.items():
        if key == "text":
            missing = [phrase for phrase in want if phrase not in posting.description]
            assert not missing, (missing, posting.description)
        else:
            assert getattr(posting, key) == want, (key, getattr(posting, key))


def posting_fixture(system: str) -> dict:
    return json.loads((POSTINGS / f"{system}.json").read_text(encoding="utf-8"))


def test_every_posting_reader_has_a_fixture():
    """A job system's reader added to postings.py gets a fixture (and a test) of its own."""
    import job_apply.postings as postings

    fetchers = {name for name in vars(postings) if name.startswith("_fetch_") and name != "_fetch_with"}
    assert fetchers == {fetcher for fetcher, _ in READERS.values()}, fetchers
    assert sorted(p.stem for p in POSTINGS.glob("*.json")) == sorted(READERS)
    for system in READERS:
        assert posting_fixture(system)["system"] == system


def test_a_workday_posting_is_read_from_its_json_api():
    """Workday's reader (its cxs API): the title, both places, the text as lines and the posting's external address.
    Without the employer's name or an external address in the answer, the tenant's name and the posting's own address."""
    fixture = posting_fixture("workday")
    posting = replay_posting(fixture)
    check_posting(posting, fixture)
    assert not posting.warnings, posting.warnings
    unnamed = json.loads(json.dumps(fixture))
    for answer in unnamed["answers"].values():
        del answer["hiringOrganization"]
        del answer["jobPostingInfo"]["externalUrl"]
    posting = replay_posting(unnamed)
    assert (posting.company, posting.apply_url) == ("examplefab", fixture["posting_url"]), posting


def test_a_greenhouse_posting_is_read_from_its_board_api():
    """Greenhouse's reader: the text (escaped once more in the answer) and the application's questions under it, and
    Greenhouse's own page for applying, not the employer's embedding one."""
    fixture = posting_fixture("greenhouse")
    posting = replay_posting(fixture)
    check_posting(posting, fixture)
    assert posting.description.count("\n- ") == 3, posting.description  # (the degree, two questions: no empty label)


def test_a_lever_posting_is_read_from_its_postings_api():
    fixture = posting_fixture("lever")
    check_posting(replay_posting(fixture), fixture)


def test_a_smartrecruiters_posting_is_read_from_its_api():
    fixture = posting_fixture("smartrecruiters")
    posting = replay_posting(fixture)
    check_posting(posting, fixture)
    assert "Additional Information" not in posting.description  # (a section without text)


def test_an_oracle_posting_is_read_from_its_requisition_api():
    fixture = posting_fixture("oracle_hcm")
    check_posting(replay_posting(fixture), fixture)


def test_a_cornerstone_posting_is_read_with_its_pages_token():
    """Cornerstone's reader: the page's anonymous token, then the requisition service; a place named twice, once; and
    a posting that takes no applications says so."""
    fixture = posting_fixture("csod")
    posting = replay_posting(fixture)
    check_posting(posting, fixture)
    assert posting.warnings == ["Cornerstone says this posting takes no applications now."], posting.warnings


def page_posting(title: str) -> str:
    ld = {"@type": "JobPosting", "title": title, "description": "<p>Made-up text.</p>",
          "jobLocation": {"address": {"addressLocality": "Tempe", "addressRegion": "AZ"}}}
    return f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body></body></html>'


def test_a_reader_that_cant_read_the_posting_leaves_it_to_the_page():
    """Each reader gives way to the page (its JSON-LD here) when the address isn't a posting's, its API has no posting,
    or its page lacks what the API needs; an API that fails says so in the posting's warnings. LinkedIn's sign-in
    wall is an error, not a posting."""
    wd = "https://examplefab.wd1.myworkdayjobs.com/en-US/External/job/Phoenix-AZ/Technician_R1"
    wd_api = "https://examplefab.wd1.myworkdayjobs.com/wday/cxs/examplefab/External/job/Phoenix-AZ/Technician_R"
    oracle = "https://examplefab.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/7"
    csod = "https://examplefab.csod.com/ux/ats/careersite/1/home/requisition/{}?c=examplefab"
    csod_api = "https://examplefab.csod.com/services/x/job-requisition/v2/requisitions/{}/jobDetails?cultureId=1"
    token = '<script>csod.context={"token": "anon-token-1", "cultureID": 1};</script>'
    cases = {  # the posting's address: its answers besides the page, and what the posting read says
        "https://examplefab.wd1.myworkdayjobs.com/en-US/External": ({}, ""),  # the site, not a posting
        wd: ({wd_api + "1": {"jobPostingInfo": {}}}, ""),  # no posting in the answer
        wd.replace("_R1", "_R2"): ({wd_api + "2": 500}, "ATS API lookup failed (FetchError: HTTP 500 from"),
        "https://job-boards.greenhouse.io/examplefab": ({}, ""),
        "https://jobs.lever.co/examplefab": ({}, ""),
        "https://jobs.smartrecruiters.com/ExampleFab": ({}, ""),
        "https://examplefab.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/jobs": ({}, ""),
        oracle: ({"https://examplefab.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                  "?expand=all&onlyData=true&finder=ById;Id=%227%22,siteNumber=CX_1": {"items": []}}, ""),
        "https://careers.example.com/en/sites/CX_1/job/8": ({}, ""),  # its own address, its page naming no Oracle host
        "https://examplefab.csod.com/ux/ats/careersite/1/home?c=examplefab": ({}, ""),  # the site, not a requisition
        csod.format(2): ({}, ""),  # a page without its token
        csod.format(3): ({csod_api.format(3): {"data": {}}}, ""),  # no posting in the answer
    }
    pages = {csod.format(3): token}

    def answer(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://www.linkedin.com/jobs/view/4000000001/":
            return httpx.Response(302, headers={"Location": "https://www.linkedin.com/authwall?trk=x"})
        if url.startswith("https://www.linkedin.com/authwall"):
            return httpx.Response(200, html="<html><body>Sign in</body></html>")
        for posting_url, (answers, _) in cases.items():
            if url == posting_url:
                return httpx.Response(200, html=pages.get(url, "") + page_posting("From the page"))
            if url in answers:
                body = answers[url]
                return httpx.Response(body) if isinstance(body, int) else httpx.Response(200, json=body)
        raise AssertionError(url)

    async def go() -> list:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer), follow_redirects=True) as client:
            read = [await fetch_posting(url, client=client) for url in cases]
            try:
                await fetch_posting("https://www.linkedin.com/jobs/view/4000000001/", client=client)
            except FetchError as e:
                read.append(str(e))
            return read

    *read, linkedin = asyncio.run(go())
    for (url, (_, warned)), posting in zip(cases.items(), read):
        assert (posting.parse_method, posting.title, posting.location) == ("json-ld", "From the page", "Tempe, AZ"), url
        api_warnings = [w for w in posting.warnings if w.startswith("ATS API")]
        assert (api_warnings[0].startswith(warned) if warned else not api_warnings), (url, posting.warnings)
    assert linkedin == "LinkedIn requires sign-in for this posting"


def test_a_cornerstone_pages_context_is_the_one_with_its_token():
    page = ('csod.context={"token": ' 'csod.context={}; csod.context={"cultureID": 1}; '
            'csod.context={"token": "anon-token-1", "cultureID": 2};')
    assert csod_context(page) == {"token": "anon-token-1", "cultureID": 2}
    assert csod_context('csod.context={"cultureID": 1};') == {"cultureID": 1}  # (a placeholder, without one)
    assert csod_context("<html></html>") == {}


def test_json_ld_in_the_shapes_sites_write_it():
    """A script left empty, trailing commas, one that's no JSON at all, places written as words, a salary as one
    value, a remote posting, and markup the text leaves out."""
    def page(*scripts: str) -> str:
        return ("<html><head>" + "".join(f'<script type="application/ld+json">{s}</script>' for s in scripts)
                + "</head><body></body></html>")

    ld = ('{"@type": "JobPosting", "title": "Remote Technician", "jobLocation": ["Tempe, AZ", {"address": "Mesa, AZ"},],'
          ' "baseSalary": {"currency": "USD", "value": 30}, "jobLocationType": "TELECOMMUTE",'
          ' "description": "<p>One<br>Two</p><script>track()<\\/script>",}')  # (its "/" escaped, as in a page)
    p = parse_html(page("", "{not json", ld), "https://careers.example.com/job/1")
    assert (p.title, p.location, p.salary, p.parse_method) == ("Remote Technician", "Tempe, AZ; Mesa, AZ; Remote",
                                                               "USD 30", "json-ld"), p
    assert p.description == "One\nTwo", p.description
    assert html_to_text("") == ""


SAVE_RUN = """
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, "scripts")
import httpx
import live_smoke

answers = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

def answer(request):
    body = answers.get(str(request.url))
    return httpx.Response(404) if body is None else httpx.Response(200, json=body)

real = httpx.AsyncClient
httpx.AsyncClient = lambda **kw: real(**{"transport": httpx.MockTransport(answer), **kw})
live_smoke.SAVE_POSTINGS = Path(sys.argv[1])

async def main():
    for url in sys.argv[3:]:
        print("RESULT", (await live_smoke.read_posting(url)).title)

asyncio.run(main())
"""


def test_the_live_check_saves_each_systems_first_posting_answer_scrubbed(tmp_path):
    """live_smoke.py --save-postings: the search check's first posting of each job system read from its API, saved in
    the fixtures' form (scrubbed, keys naming a person left out), reads offline as the desk read it live; a second
    posting of the system leaves it be. Run in a process of its own: live_smoke sets the environment it runs in."""
    import subprocess
    import sys
    from pathlib import Path

    workday, smart = posting_fixture("workday"), posting_fixture("smartrecruiters")
    (first_api, first), = workday["answers"].items()
    second = json.loads(json.dumps(first).replace("Equipment Technician II", "Etch Technician"))
    (sr_api, sr), = smart["answers"].items()
    said = ("<p>Questions? Ask someone@gmail.com or call 602-431" "-0188, or Testy McTestface on the night crew.</p>"
            + "<p>Keep the made-up chillers cold.</p>" * 8)
    sr = {**sr, "creator": {"name": "Pat Example"},
          "jobAd": {"sections": {"jobDescription": {"title": "Job Description", "text": said}}}}
    stand_in = {first_api: first, first_api.replace("_R0001234", "_R0005678"): second, sr_api: sr}
    (tmp_path / "answers.json").write_text(json.dumps(stand_in), encoding="utf-8")
    urls = [workday["posting_url"], workday["posting_url"].replace("_R0001234", "_R0005678"), smart["posting_url"]]
    out = subprocess.run([sys.executable, "-c", SAVE_RUN, str(tmp_path / "saved"), str(tmp_path / "answers.json"), *urls],
                         cwd=Path(__file__).parent.parent, capture_output=True, text=True, encoding="utf-8", timeout=120)
    lines = [x[len("RESULT "):] for x in out.stdout.splitlines() if x.startswith("RESULT ")]
    assert lines == ["Equipment Technician II", "Etch Technician", "Facilities Technician"], \
        out.stdout[-2000:] + out.stderr[-3000:]
    assert sorted(p.name for p in (tmp_path / "saved").iterdir()) == ["smartrecruiters.json", "workday.json"]
    saved = json.loads((tmp_path / "saved" / "workday.json").read_text(encoding="utf-8"))
    assert saved["posting_url"] == workday["posting_url"] and saved["expect"]["title"] == "Equipment Technician II"
    check_posting(replay_posting(saved), saved)
    saved_sr = json.loads((tmp_path / "saved" / "smartrecruiters.json").read_text(encoding="utf-8"))
    check_posting(replay_posting(saved_sr), saved_sr)
    text = json.dumps(saved_sr)
    for gone in ("creator", "Pat Example", "someone@gmail.com", "602-431", "McTestface"):
        assert gone not in text, gone
    assert "someone@example.com" in text and "REDACTED" in text
