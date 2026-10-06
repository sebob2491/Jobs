"""Ranking openings against the applicant's profile."""

import asyncio
from datetime import date

from job_apply.config import Profile
from job_apply.postings import Posting
from job_apply.recommend import (applicant_degree, applicant_years, days_since, recommend, requirements, score_listing,
                                 target_location, target_query, title_level)

TODAY = date(2026, 10, 6)


def tech(**over) -> Profile:
    """An equipment technician with an Associate's and about 3 years, like the person this is for."""
    data = {
        "personal": {"address": {"city": "Phoenix", "state": "AZ"}},
        "work_authorization": {"us_person": True},
        "education": {"highest_degree": "Associate of Science"},
        "work_history": [{"title": "Equipment Technician", "company": "Intel", "start": "2023-07", "end": "present"},
                         {"title": "Tech Support", "company": "Freelance", "start": "Jan 2022", "end": "06/2023"}],
        "preferences": {"titles": ["Field Service Engineer", "Equipment Technician"],
                        "locations": ["Phoenix, AZ", "Chandler, AZ"], "willing_to_travel": "Yes, up to 75%"},
    }
    for k, v in over.items():
        data[k] = v
    return Profile(data)


def test_profile_targets():
    p = tech()
    assert target_query(p) == "Field Service Engineer | Equipment Technician"
    assert target_location(p) == "AZ"
    assert target_location(Profile({"personal": {"address": {"state": "Texas"}}})) == "Texas"
    assert "field service" in target_query(Profile({}))  # sensible defaults
    assert applicant_years(p, TODAY) == 4.7  # 39 months at Intel + 17 freelance
    assert applicant_years(Profile({"experience": {"total_years": 2}})) == 2.0
    assert applicant_degree(p) == "associate"


def test_dates_and_levels():
    assert days_since("Posted Today", TODAY) == 0
    assert days_since("Posted 3 Days Ago", TODAY) == 3
    assert days_since("Posted 30+ Days Ago", TODAY) == 30
    assert days_since("2026-09-29", TODAY) == 7
    assert days_since("2026-08-27T13:47:52-04:00", TODAY) == 40
    assert days_since("10/02/2026", TODAY) == 4
    assert days_since("whenever", TODAY) is None
    assert title_level("Field Service Engineer II") == 2
    assert title_level("FSE 3 - Etch") == 3
    assert title_level("Field Service Engineer") is None


LAM_EARLY = """Minimum Qualifications
- Bachelor's degree in Engineering, Physics, or a related field.
- Experience with semiconductor equipment.
Preferred Qualifications
- 2+ years of experience in a fab environment."""

LAM_FSE2 = """What you need
- Bachelor's degree with 2+ years of related experience; or equivalent work experience.
- Willingness to travel up to 50% of the time, domestic and international.
- Must be a U.S. person due to export control regulations."""

INTEL_MTE = """Minimum qualifications:
- Associate's degree in a STEM field with 2+ years of experience in equipment maintenance, OR
- High school diploma or GED with 2.5+ years of experience in equipment maintenance.
Preferred qualifications:
- Bachelor's degree in engineering.
Shift 5: 12-hour days. Wear safety glasses and ear plugs."""

DEFENSE = """Requirements:
- 5-8 years of experience with test equipment.
- Active Secret clearance required.
Bachelor's degree preferred"""


def test_requirements_from_posting_text():
    early = requirements(LAM_EARLY)
    assert early["degree"] == "bachelor" and not early["degree_or_equivalent"]
    assert early["years"] is None  # the 2 years are only preferred
    fse2 = requirements(LAM_FSE2)
    assert fse2["degree"] == "bachelor" and fse2["degree_or_equivalent"]
    assert fse2["years"] == 2 and fse2["travel"] == 50 and fse2["us_person"]
    mte = requirements(INTEL_MTE)
    assert mte["degree"] == "high_school" and mte["years"] == 2  # the lowest path counts
    assert mte["shifts"] and not mte["us_person"]  # ear plugs aren't export rules
    defense = requirements(DEFENSE)
    assert defense["clearance"] and defense["years"] == 5 and defense["degree"] is None
    assert requirements("Ability to obtain a security clearance.")["clearance_later"]


def test_scores_read_like_the_brief():
    p = tech()
    fse2 = score_listing({"title": "Field Service Engineer 2", "location": "US-AZ-Chandler (1002)", "posted": "2026-10-01"},
                         p, LAM_FSE2, TODAY)
    early = score_listing({"title": "Field Service Engineer, Early Career Opportunity", "location": "US-AZ-Chandler",
                           "posted": "2026-07-23"}, p, LAM_EARLY, TODAY)
    mte = score_listing({"title": "Manufacturing Equipment Technician (MTE)", "location": "US, Arizona, Phoenix",
                         "posted": "Posted 8 Days Ago"}, p, INTEL_MTE, TODAY)
    senior = score_listing({"title": "Senior Field Service Engineer", "location": "Phoenix, AZ"}, p, today=TODAY)
    accountant = score_listing({"title": "Accountant", "location": "Phoenix, AZ"}, p, today=TODAY)
    assert fse2.recommended and mte.recommended
    assert "asks for a Bachelor's or equivalent experience (you have an Associate's)" in fse2.concerns
    assert "requires a Bachelor's (you have an Associate's)" in early.concerns
    assert early.score < fse2.score and early.score < mte.score
    assert "title matches “Field Service Engineer”" in fse2.reasons and "in Chandler" in fse2.reasons
    assert any("senior-level" in c for c in senior.concerns)
    assert not accountant.recommended and "not one of your target titles" in accountant.concerns
    cleared = score_listing({"title": "Field Service Engineer"}, p, DEFENSE, TODAY)
    assert cleared.blocked and not cleared.recommended
    not_us = score_listing({"title": "Field Service Engineer"}, tech(work_authorization={"us_person": False}), LAM_FSE2, TODAY)
    assert not_us.blocked and "export-controlled: U.S. persons only" in not_us.concerns
    far = score_listing({"title": "Field Service Engineer"}, tech(preferences={"willing_to_travel": "up to 25%"}),
                        LAM_FSE2, TODAY)
    assert "up to 50% travel (you said 25%)" in far.concerns


def test_recommend_reads_the_top_postings():
    p = tech()
    asked = {}

    async def search(query, location, limit):
        asked.update(query=query, location=location, limit=limit)
        return {"results": [
            {"company": "Lam Research", "title": "Field Service Engineer 2", "url": "https://x/fse2", "location": "Chandler, AZ",
             "posted": "2026-10-01"},
            {"company": "Lam Research", "title": "Field Service Engineer, Early Career", "url": "https://x/early",
             "location": "Chandler, AZ", "posted": "2026-07-23"},
            {"company": "Acme", "title": "Accountant", "url": "https://x/acct", "location": "Phoenix, AZ"},
        ], "errors": {"Broken Co": "SearchError: HTTP 500"}, "browser_only": [{"company": "TSMC Arizona"}]}

    texts = {"https://x/fse2": LAM_FSE2, "https://x/early": LAM_EARLY}
    read: list[str] = []

    async def fetch(url):
        read.append(url)
        if url not in texts:
            raise RuntimeError("unreadable")
        return Posting(url=url, description=texts[url], apply_url=url + "/apply")

    out = asyncio.run(recommend(p, search, limit_per_company=7, read_postings=2, fetch=fetch, today=TODAY))
    assert asked == {"query": "Field Service Engineer | Equipment Technician", "location": "AZ", "limit": 7}
    assert [r["title"] for r in out["results"]][0] == "Field Service Engineer 2"
    assert sorted(read) == ["https://x/early", "https://x/fse2"]  # only the top two
    top = out["results"][0]
    assert top["fit"]["recommended"] and top["posting"]["apply_url"] == "https://x/fse2/apply"
    assert out["results"][-1]["title"] == "Accountant" and not out["results"][-1]["fit"]["recommended"]
    assert out["errors"] == {"Broken Co": "SearchError: HTTP 500"} and out["browser_only"] == [{"company": "TSMC Arizona"}]
