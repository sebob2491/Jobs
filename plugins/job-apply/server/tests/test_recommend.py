"""Ranking openings against the applicant's profile."""

import asyncio
from datetime import date

from job_apply.config import Profile
from job_apply.postings import Posting
from job_apply.recommend import (applicant_degree, applicant_years, days_since, recommend, requirements, score_listing,
                                 target_location, target_query, title_level)

TODAY = date(2026, 10, 6)


def tech(**over) -> Profile:
    """An equipment technician with an Associate's and about 3 years."""
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


def test_requirements_read_the_way_postings_write_them():
    # curly apostrophes (Workday, Oracle) and "A.S." are degrees too
    assert requirements("Minimum Qualifications\n- Bachelor’s degree in Mechanical Engineering.")["degree"] == "bachelor"
    assert requirements("Requirements\n- A.S. in electronics or 2+ years of military training.")["degree"] == "associate"
    assert requirements("Required: a degree in electrical engineering.")["degree"] == "bachelor"
    # only the preferred part of a sentence is skipped
    both = requirements("Qualifications\nBachelor's degree required, Master's preferred.")
    assert both["degree"] == "bachelor" and not both["degree_or_equivalent"]
    assert requirements("Bachelor's degree in engineering, physics, or a related field preferred.")["degree"] is None
    years = requirements("5+ years of experience required, Bachelor's preferred.")
    assert years["years"] == 5 and years["degree"] is None
    # a line that starts with "Experience" isn't a heading back to the required list
    sections = requirements("Preferred Qualifications\nExperience with vacuum pumps, Bachelor's degree.\n"
                            "Requirements:\nHigh school diploma or GED.")
    assert sections["degree"] == "high_school"
    # saying there's no clearance, or naming staff, isn't a clearance or export rule
    assert not requirements("No security clearance required.")["clearance"]
    assert not requirements("This role does not require a security clearance.")["clearance"]
    assert not requirements("Coordinate with various personnel across the fab.")["us_person"]
    assert requirements("Applicants must be U.S. persons.")["us_person"]


def test_requirements_in_markdown_and_beside_application_questions():
    lam = requirements("## Minimum qualifications\n* High school diploma or GED.\n"
                       "## Preferred qualifications\n* Bachelor's degree in engineering.\n* Active Secret clearance.")
    assert lam["degree"] == "high_school" and not lam["clearance"]
    bold = requirements("**Nice to have:**\nBachelor's degree.\n**Requirements:**\nAssociate's degree.")
    assert bold["degree"] == "associate"
    # Greenhouse's application questions are the form's, not the job's (ASM asks every applicant)
    asm = requirements("Requirements\n- 3+ years of field service experience.\n\nApplication questions:\n"
                       "- Are you a U.S. person under the U.S. Export Administration Regulation (EAR)?\n"
                       "- Do you hold an active security clearance?")
    assert not asm["us_person"] and not asm["clearance"] and asm["years"] == 3
    # Intel: one of several degrees
    assert requirements("Minimum qualifications:\n- Associate's or Bachelor's degree in a STEM field.")["degree"] == "associate"


def test_the_way_around_a_degree_said_another_way_and_lists_that_arent_one():
    """The person this is for has no degree: a false "requires" hides a job they could have;
    a false "or equivalent" preselects one they can't."""
    def read(text):
        r = requirements("Qualifications\n" + text)
        return r["degree"], r["degree_or_equivalent"]
    assert read("- Bachelor's degree in Electronics.\nEquivalent combination of education and experience "
                "will be considered.") == ("bachelor", True)
    assert read("- Bachelor's degree required. In lieu of a degree, 4 additional years of experience.") == ("bachelor", True)
    assert read("- Bachelor's degree; military experience may be substituted for the degree.") == ("bachelor", True)
    assert read("- Associate degree in electronics; relevant experience in lieu of a degree is acceptable, "
                "field service experience preferred.") == ("associate", True)
    assert read("- Bachelor's degree in EE, with hands-on experience troubleshooting electrical or "
                "mechanical systems.") == ("bachelor", False)
    assert read("- Bachelor's degree in engineering and Six Sigma or Lean certification.") == ("bachelor", False)
    # the preferred list's own sub-headings don't make it the required list again
    pref = requirements("Preferred Qualifications\nEducation\nBachelor's degree in engineering.\nExperience\n"
                        "5 years field service.")
    assert pref["degree"] is None


def test_degrees_with_other_ways_in():
    """Postings that take training, a certification or years of work instead of the degree
    (the wording of Applied Materials', ASM's and Onto's postings)."""
    for text in ("- Completion of an Associate degree, military technical training, field service experience, "
                 "or trade certification",
                 "- Technical school diploma or associate degree from a two\u2011year program in Electronics, or at least "
                 "three years of progressively responsible, hands\u2011on experience",
                 "- Alternatively: Associate degree or technical certification with 5+ years of field service experience."):
        req = requirements("Qualifications\n" + text)
        assert req["degree"] == "associate" and req["degree_or_equivalent"], text
    ladder = requirements("Qualifications\n- Technical school diploma (2\u2011year program), OR BS with 1\u20133 years of "
                          "experience, OR 7+ years of relevant technical experience.")
    assert ladder["degree"] == "associate" and ladder["degree_or_equivalent"]
    hard = requirements("Qualifications\n- Completion of an Associate degree or Bachelors Degree")
    assert hard["degree"] == "associate" and not hard["degree_or_equivalent"]
    with_years = requirements("- Bachelor's degree and 3+ years of hands-on technical experience in electronic systems.")
    assert with_years["degree"] == "bachelor" and not with_years["degree_or_equivalent"]


def test_a_degree_you_dont_have_is_not_preselected():
    """The person this is for has some college classes and no degree: a posting that
    requires a degree without "or equivalent" is shown, not preselected."""
    some_college = tech(education={"highest_degree": "Some college"})
    assert applicant_degree(some_college) == "high_school"
    unfinished = [{"school": "Example Community College", "major": "Electronics"}]
    assert applicant_degree(tech(education={}, education_history=unfinished)) == "high_school"
    assert applicant_degree(Profile({})) is None  # says nothing about schooling: nothing checked
    assert applicant_degree(Profile({"education": {"highest_degree": None, "school": None}})) is None  # the template
    as_req = "Requirements\n- Associate's degree in electronics.\n- 2+ years of experience."
    fit = score_listing({"title": "Equipment Technician", "location": "Chandler, AZ"}, some_college, as_req, TODAY)
    assert fit.blocked and not fit.recommended
    assert "requires an Associate's (you have a high school diploma)" in fit.concerns
    equivalent = "Requirements\n- Associate's degree or equivalent experience.\n- 2+ years of experience."
    ok = score_listing({"title": "Equipment Technician", "location": "Chandler, AZ"}, some_college, equivalent, TODAY)
    assert not ok.blocked and ok.recommended


def test_the_area_by_state_names_and_codes():
    assert target_location(Profile({"preferences": {"locations": ["Phoenix, Arizona", "Tempe, AZ 85281"]}})) == "AZ"
    assert target_location(Profile({"preferences": {"locations": ["Charleston, West Virginia"]}})) == "WV"


def test_recommend_holds_postings_it_could_not_check():
    """Only postings whose requirements were read are preselected; one whose posting says
    it's in another state isn't either."""
    p = tech()

    async def search(query, location, limit):
        return {"results": [
            {"company": "Lam Research", "title": "Field Service Engineer 2", "url": "https://x/fse2", "location": "Chandler, AZ"},
            {"company": "Acme", "title": "Field Service Engineer", "url": "https://x/unread", "location": "Phoenix, AZ"},
            {"company": "Moved Co", "title": "Field Service Engineer", "url": "https://x/elsewhere", "location": "3 Locations"},
            # a page that builds itself with script: read, but no posting in it
            {"company": "Script Co", "title": "Field Service Engineer", "url": "https://x/blank", "location": "Tempe, AZ"},
        ]}

    async def fetch(url):
        if url == "https://x/unread":
            raise RuntimeError("unreadable")
        where = "Peoria, IL" if url == "https://x/elsewhere" else "Chandler, AZ"
        return Posting(url=url, description="" if url == "https://x/blank" else LAM_FSE2, location=where)

    # only one posting is read by rank, but every one that would be preselected is read too
    out = asyncio.run(recommend(p, search, read_postings=1, fetch=fetch, today=TODAY))
    fits = {r["url"]: r["fit"] for r in out["results"]}
    assert fits["https://x/fse2"]["recommended"]
    assert not fits["https://x/unread"]["recommended"] and fits["https://x/unread"]["held"]
    assert "posting not read yet: its requirements are unchecked" in fits["https://x/unread"]["concerns"]
    assert not fits["https://x/blank"]["recommended"] and fits["https://x/blank"]["held"]
    assert "the posting's text couldn't be read: its requirements are unchecked" in fits["https://x/blank"]["concerns"]
    assert not fits["https://x/elsewhere"]["recommended"]
    assert "the posting says it's in Peoria, IL" in fits["https://x/elsewhere"]["concerns"]
