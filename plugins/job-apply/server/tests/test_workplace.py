"""Where a job is done: remote, hybrid or on-site (the design of Oct 18, F3). Made-up postings only."""

import asyncio
import json

import httpx
import pytest

from job_apply import workplace
from job_apply.postings import fetch_posting, finalize, parse_html


@pytest.mark.parametrize("value, said", [
    ("TELECOMMUTE", "remote"), ("Remote", "remote"), ("Fully Remote", "remote"), ("remoteLocal", "remote"),
    ("Global Remote", "remote"), ("ORA_REMOTE", "remote"), ("Hybrid", "hybrid"), ("ORA_HYBRID", "hybrid"),
    ("On-Site", "onsite"), ("Onsite", "onsite"), ("on-site", "onsite"), ("In Office", "onsite"),
    ("ORA_ON_SITE", "onsite"), ("Not Remote", ""),
    # none, or several (an option, never sure: workplace.field_said), or "not remote": not said
    ("", ""), (None, ""), ("Flexible", ""), ("unspecified", ""), ("Remote or On-site", "remote"),
])
def test_a_systems_own_field_says_where_the_job_is_done(value, said):
    assert workplace.from_field(value) == said


@pytest.mark.parametrize("location, said", [
    ("Remote - US", "remote"), ("US-Remote", "remote"), ("Remote (Arizona)", "remote"),
    ("Phoenix, AZ; Remote", "remote"), ("Telework - Arizona", "remote"), ("Phoenix, AZ (Hybrid)", "hybrid"),
    ("Chandler, AZ - Onsite", "onsite"), ("Phoenix, AZ", ""), ("Statewide (Arizona)", ""), ("", ""),
    ("Remotely Hall, Tempe, AZ", ""),
])
def test_a_locations_own_words_say_where_the_job_is_done(location, said):
    assert workplace.in_location(location) == said


@pytest.mark.parametrize("text, said", [
    ("This is a fully remote position.", "remote"), ("A remote role on our support team.", "remote"),
    ("This hybrid role is in the office three days a week.", "hybrid"),
    ("This position is on-site at our Chandler fab.", "onsite"),
    ("This is not a remote position.", "onsite"),
    # an equipment job's words that aren't where it's done
    ("Perform remote diagnostics and remote support of tools.", ""), ("Provide on-site support to customers.", ""),
    ("Hybrid bonding process experience a plus.", ""), ("", ""),
])
def test_a_postings_sentences_say_where_the_job_is_done_but_never_surely(text, said):
    assert workplace.in_text(text) == said
    assert workplace.read(text=text) == ((said, "text") if said else ("", ""))
    assert workplace.keeps(workplace.read(text=text), ["hybrid" if said != "hybrid" else "remote"])


def test_the_best_source_says_it_and_only_a_sure_one_hides_a_job():
    assert workplace.read("Hybrid", "Remote - US", "a fully remote role") == ("hybrid", "structured")
    assert workplace.read(None, "Remote - US", "This hybrid role ...") == ("remote", "location")
    assert workplace.read(None, "Phoenix, AZ", "This hybrid role ...") == ("hybrid", "text")
    assert workplace.better({"workplace": "remote", "workplace_source": "structured"},
                            {"workplace": "onsite", "workplace_source": "location"})
    assert not workplace.better({"workplace": "remote", "workplace_source": "text"},
                                {"workplace": "onsite", "workplace_source": "location"})
    assert workplace.better({"workplace": "remote", "workplace_source": "text"}, {})
    assert not workplace.better({"workplace": "", "workplace_source": ""}, {})
    for said, chosen, kept in ((("onsite", "structured"), ["remote"], False),
                               (("onsite", "location"), ["remote"], False), (("onsite", "text"), ["remote"], True),
                               (("", ""), ["remote"], True), (("remote", "location"), ["remote", "hybrid"], True),
                               (("onsite", "structured"), [], True)):
        assert workplace.keeps(said, chosen) is kept, (said, chosen)
    assert workplace.view("remote", "location") == {"value": "remote", "sure": True, "shown": "Remote",
                                                    "from": "Its location says so"}
    unsure = workplace.view("hybrid", "text")
    assert unsure["shown"] == "likely hybrid" and not unsure["sure"] and "never hides" in unsure["from"]
    assert workplace.view("", "") is None
    assert workplace.parse_choice("remote, Hybrid") == ["remote", "hybrid"]
    assert workplace.parse_choice("On-site|remote") == ["remote", "onsite"]
    assert workplace.parse_choice(None) == [] and workplace.parse_choice("anywhere") is None


def _jsonld(**ld):
    data = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Field Service Engineer",
            "description": "<p>Service tools.</p>" * 30, "hiringOrganization": {"name": "Example Fab"},
            "jobLocation": {"address": {"addressLocality": "Phoenix", "addressRegion": "AZ"}}, **ld}
    return f'<html><script type="application/ld+json">{json.dumps(data)}</script><body></body></html>'


def test_a_posting_says_where_its_done_from_its_data_its_location_or_its_text():
    remote = finalize(parse_html(_jsonld(jobLocationType="TELECOMMUTE"), "https://careers.example.com/job/1"))
    assert (remote.workplace, remote.workplace_source) == ("remote", "structured") and "Remote" in remote.location
    placed = finalize(parse_html(_jsonld(jobLocation={"address": {"addressLocality": "Remote - US"}}),
                                 "https://careers.example.com/job/2"))
    assert (placed.workplace, placed.workplace_source) == ("remote", "location")
    said = finalize(parse_html(_jsonld(description="<p>This hybrid role works from our Tempe office.</p>" * 10),
                               "https://careers.example.com/job/3"))
    assert (said.workplace, said.workplace_source) == ("hybrid", "text")
    plain = finalize(parse_html(_jsonld(), "https://careers.example.com/job/4"))
    assert (plain.workplace, plain.workplace_source) == ("", "")
    assert {"workplace", "workplace_source"} <= set(plain.to_dict())
    # SuccessFactors' line under the title, and iCIMS's header details
    sf = ('<html><body><div class="joblayouttoken"><span itemprop="title">Field Service Engineer</span></div>'
          '<div class="joblayouttoken"><span>Service</span></div><div class="joblayouttoken"><span>Phoenix AZ</span>'
          '</div><div class="joblayouttoken"><span>On-Site</span></div><main>' + "<p>Service tools.</p>" * 30
          + "</main></body></html>")
    page = finalize(parse_html(sf, "https://jobs.example.com/job/Phoenix-FSE/171942-en_US/"))
    assert (page.location, page.workplace, page.workplace_source) == ("Phoenix, AZ", "onsite", "structured")
    icims = ('<html><body><main><dl><div class="iCIMS_JobHeaderTag"><dt>Location Type</dt><dd>Hybrid</dd></div>'
             '<div class="iCIMS_JobHeaderTag"><dt>Remote Location Eligible</dt><dd>No</dd></div></dl>'
             + "<p>Recruit people.</p>" * 30 + "</main></body></html>")
    page = finalize(parse_html(icims, "https://careers-x.icims.com/jobs/1/recruiter/job"))
    assert (page.workplace, page.workplace_source) == ("hybrid", "structured")
    eligible = icims.replace("<dt>Location Type</dt><dd>Hybrid</dd>", "").replace("<dd>No</dd>", "<dd>Yes</dd>")
    assert finalize(parse_html(eligible, "https://careers-x.icims.com/jobs/1/recruiter/job")).workplace == "remote"


def test_the_systems_apis_say_where_their_jobs_are_done_when_they_give_it():
    """Fields these systems' APIs give for it (not seen in this repo's fixtures yet): read only when there, and only a
    value that says one."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://api.lever.co/v0/postings/leverco/"):
            kind = "hybrid" if url.endswith("1111") else "unspecified"
            return httpx.Response(200,
                                  json={"text": "Field Service Engineer", "categories": {"location": "Phoenix, AZ"},
                                        "descriptionPlain": "Service tools.", "workplaceType": kind})
        if url.startswith("https://api.smartrecruiters.com/v1/companies/EXAMPLE1/postings/744000001"):
            return httpx.Response(200, json={"name": "Technician", "location": {"city": "Chandler", "region": "AZ",
                                                                                "remote": True},
                                             "jobAd": {"sections": {}}})
        if "recruitingCEJobRequisitionDetails" in url:
            return httpx.Response(200, json={"items": [{"Id": "1", "Title": "Technician", "WorkplaceTypeCode":
                                                        "ORA_ON_SITE", "ExternalDescriptionStr": "<p>Tools.</p>"}]})
        raise AssertionError(url)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return [await fetch_posting(u, client=client) for u in (
                "https://jobs.lever.co/leverco/0b1c2d3e-0000-1111-2222-333344441111",
                "https://jobs.lever.co/leverco/0b1c2d3e-0000-1111-2222-333344442222",
                "https://jobs.smartrecruiters.com/EXAMPLE1/744000001-technician",
                "https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/1")]

    said = [(p.workplace, p.workplace_source) for p in asyncio.run(go())]
    assert said == [("hybrid", "structured"), ("", ""), ("remote", "structured"), ("onsite", "structured")]


def test_an_older_tracker_gains_the_workplace_columns_and_a_better_source_replaces_a_worse(job_apply_home):
    """A tracker.db from before F3 gains the two columns as it opens (with nothing said for its jobs); a job's workplace
    is kept whole, filled in where it has none and replaced only by a better source's (structured > location > text)."""
    import sqlite3

    from job_apply import config
    from job_apply.tracker import Tracker

    path = config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, title TEXT, "
                "company TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    old.execute("INSERT INTO jobs (url, title, company, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("https://careers.example.com/job/1", "Technician", "Example Fab", "2026-10-01T00:00:00+00:00",
                 "2026-10-01T00:00:00+00:00"))
    old.commit()
    old.close()
    t = Tracker()
    assert {"workplace", "workplace_source"} <= {r[1] for r in t.conn.execute("PRAGMA table_info(jobs)")}
    assert (t.get(1)["workplace"], t.get(1)["workplace_source"]) == ("", "")
    url = "https://careers.example.com/job/2"

    def said(**fields):
        job, _ = t.upsert({"url": url, "title": "Technician", "company": "Example Fab", **fields})
        return job["workplace"], job["workplace_source"]

    assert said() == ("", "")
    assert said(workplace="hybrid", workplace_source="text") == ("hybrid", "text")
    assert said(workplace="remote", workplace_source="location") == ("remote", "location")
    assert said(workplace="onsite", workplace_source="text") == ("remote", "location")  # (a worse source: kept)
    assert said(workplace="", workplace_source="") == ("remote", "location")
    assert said(workplace="hybrid", workplace_source="structured") == ("hybrid", "structured")
    t.close()


def test_the_same_posting_read_again_replaces_its_field_and_clears_a_text_reading(job_apply_home):
    """The second review of F3 (as for pay): the job's own posting read again replaces a reading of the same rank (the
    site changed its field), and clears one read from its words that the new read no longer finds; another posting of
    the job (a job board's copy) never does."""
    from job_apply.tracker import Tracker

    t = Tracker()
    url = "https://careers.example.com/job/3"

    def said(**fields):
        job, _ = t.upsert({"url": url, "title": "Technician", "company": "Example Fab", **fields})
        return job["workplace"], job["workplace_source"]

    assert said(workplace="onsite", workplace_source="structured") == ("onsite", "structured")
    assert said(workplace="hybrid", workplace_source="structured") == ("hybrid", "structured")
    other = "https://careers.example.com/job/4"
    t.upsert({"url": other, "title": "Technician", "company": "Example Fab", "description": "This hybrid role ...",
              "workplace": "hybrid", "workplace_source": "text"})
    job, _ = t.upsert({"url": other, "description": "Service our tools.", "workplace": "", "workplace_source": ""})
    assert (job["workplace"], job["workplace_source"]) == ("", "")
    t.close()


def test_a_json_ld_list_of_location_types_and_a_paycom_value_it_cant_read():
    """The second review of F3: JSON-LD's jobLocationType may be a list; a Paycom remoteType that names none is kept
    as Paycom says it, a note."""
    from job_apply.search import parse_paycom

    page = finalize(parse_html(_jsonld(jobLocationType=["TELECOMMUTE"]), "https://careers.example.com/job/5"))
    assert (page.workplace, page.workplace_source) == ("remote", "structured")
    listing = parse_paycom({"jobPostingPreviews": [{"jobId": 1, "jobTitle": "Tech (1)", "locations": "Phoenix, AZ",
                                                    "remoteType": "Flexible"}]}, "X")[0]
    assert listing.workplace == "" and listing.notes == ["Paycom lists it as Flexible"]
