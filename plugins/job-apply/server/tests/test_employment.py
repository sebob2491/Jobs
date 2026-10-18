"""A job's employment type, as the job systems' own fields say it (the design of Oct 18, F6). Made-up values only."""

import asyncio
from pathlib import Path

import httpx
import pytest

from job_apply import employment
from job_apply.postings import fetch_posting, finalize, parse_html


@pytest.mark.parametrize("value, types", [
    # as the systems word it: Workday's timeType, Lever's commitment, SmartRecruiters' label, JSON-LD, Paycom,
    # Phoenix Children's schedule, Oracle's worker type and schedule, Indeed's job_type
    ("Full time", ["full_time"]), ("Full-time", ["full_time"]), ("FULL_TIME", ["full_time"]),
    ("Full Time", ["full_time"]), ("Full-Time", ["full_time"]), ("Part time", ["part_time"]),
    ("PART_TIME", ["part_time"]), ("Contract", ["contract"]), ("CONTRACTOR", ["contract"]),
    ("Contingent Worker, Full time", ["full_time", "contract"]), ("Contract to Hire", ["full_time", "contract"]),
    ("TEMPORARY", ["temporary"]), ("Seasonal", ["temporary"]), ("Temp to Hire", ["full_time", "temporary"]),
    ("Intern", ["internship"]), ("Internship", ["internship"]), ("INTERN", ["internship"]), ("Co-op", ["internship"]),
    ("PER_DIEM", ["per_diem"]), ("Per Diem", ["per_diem"]), ("PRN", ["per_diem"]),
    ("FULL_TIME, TEMPORARY", ["full_time", "temporary"]), ("Part Time / PRN", ["part_time", "per_diem"]),
    ("Full or Part Time", ["full_time", "part_time"]), ("Full/Part Time", ["full_time", "part_time"]),
    # words that name none: nothing said
    ("Regular", []), ("Employee", []), ("Permanent", []), ("Other", []), ("VOLUNTEER", []), ("Fix", []), ("", []),
    (None, []), ("Internal Audit", []), ("Temperature Controls", []),
])
def test_a_systems_field_names_the_jobs_employment_types(value, types):
    assert employment.types_of(value) == types


def test_a_filter_keeps_every_job_that_doesnt_say_its_type():
    assert employment.keeps(["full_time"], ["full_time", "contract"])
    assert employment.keeps(["full_time", "temporary"], ["temporary"])  # (one of its types is asked for)
    assert not employment.keeps(["part_time"], ["full_time"])
    assert employment.keeps([], ["full_time"]) and employment.keeps(["part_time"], [])
    assert employment.parse_choice("full_time, Contract") == ["full_time", "contract"]
    assert employment.parse_choice("Full-time|part time|PRN|temp|intern") == [
        "full_time", "part_time", "temporary", "internship", "per_diem"]
    assert employment.parse_choice(None) == [] and employment.parse_choice("forever") is None
    assert employment.shown(["full_time", "temporary"]) == "Full-time / Temporary"


def test_a_postings_employment_type_comes_from_its_own_data():
    fixture = Path(__file__).parent / "fixtures" / "jsonld_posting.html"
    posting = finalize(parse_html(fixture.read_text(encoding="utf-8"), "https://careers.example.com/job/1"))
    assert employment.types_of(posting.employment_type) == ["full_time"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"Id": "1", "Title": "Technician", "WorkerType": "Employee",
                                                    "JobSchedule": "Part time",
                                                    "ExternalDescriptionStr": "<p>Tools.</p>"}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_posting("https://hctz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/"
                                       "job/1", client=client)

    oracle = asyncio.run(go())  # (its schedule too, not only its worker type, which names none)
    assert oracle.employment_type == "Employee, Part time" and employment.types_of(oracle.employment_type) == [
        "part_time"]
