"""Ranking openings against the applicant's profile."""

import asyncio
from datetime import date

import pytest

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
    assert days_since("Posted 30+ Days Ago", TODAY) == 31  # more than 30: none of the "this month" credit
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
    assert requirements("Requirements:\n- Valid driver\u2019s license and a clean driving record.")["drivers_license"]
    assert not requirements("Requirements:\n- 2+ years.\nPreferred:\n- Valid driver's license.")["drivers_license"]
    assert not requirements("Requirements:\n- A driver's license is not required.")["drivers_license"]
    assert requirements("Requirements:\n- Valid driver's license required; prior experience not required.")[
        "drivers_license"]
    assert not requirements("Requirements:\n- This role does not require a driver's license.")["drivers_license"]


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
    driving = LAM_FSE2 + "\n- Valid driver's license required."
    no_license = tech(personal={"address": {"city": "Phoenix", "state": "AZ"}, "drivers_license": False})
    assert "requires a driver's license" in score_listing({"title": "Field Service Engineer"}, no_license, driving,
                                                          TODAY).concerns
    assert "requires a driver's license" not in score_listing({"title": "Field Service Engineer"}, p, driving,
                                                              TODAY).concerns


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


def test_a_posting_the_sites_own_area_filter_found_isnt_held_for_the_place_it_names():
    """Banner's Workday names a posting's place by its facility ("Banner Heart Hosp (6750 E Baywood Ave)"), on the
    posting's page too: found by the site's own Arizona filter (in_area, 0.3.177), it's never held as elsewhere; a
    posting whose page says another state still is."""
    p = tech()
    where = "Banner Heart Hosp (6750 E Baywood Ave)"

    async def search(query, location, limit):
        return {"results": [
            {"company": "Banner Health", "title": "Field Service Engineer 2", "url": "https://x/banner", "location": where,
             "in_area": True},
            {"company": "Moved Co", "title": "Field Service Engineer 2", "url": "https://x/elsewhere", "location": "Peoria, IL"}]}

    async def fetch(url):
        return Posting(url=url, description=LAM_FSE2, location=where if url == "https://x/banner" else "Peoria, IL")

    out = asyncio.run(recommend(p, search, read_postings=5, fetch=fetch, today=TODAY))
    fits = {r["url"]: r["fit"] for r in out["results"]}
    assert not fits["https://x/banner"]["held"] and not any("says it's in" in c for c in fits["https://x/banner"]["concerns"]), fits
    assert fits["https://x/elsewhere"]["held"] and "the posting says it's in Peoria, IL" in fits["https://x/elsewhere"]["concerns"]


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


def test_a_listing_without_a_date_takes_its_postings():
    """EY's SuccessFactors list and iCIMS portals' lists give no date, so their openings got no
    "posted this week" credit beside the others' (a live Find jobs run, Oct 2026). A read
    posting's own date (its datePosted) counts when the list had none, and never over the list's."""
    p = tech()

    async def search(query, location, limit):
        return {"results": [
            {"company": "EY", "title": "Field Service Engineer", "location": "Phoenix, AZ", "url": "https://x/undated"},
            {"company": "KLA", "title": "Field Service Engineer", "location": "Phoenix, AZ", "url": "https://x/dated",
             "posted": "2026-07-01"}]}

    async def fetch(url):
        return Posting(url=url, description=LAM_FSE2, posted_at="2026-10-02T00:00:00Z")

    out = asyncio.run(recommend(p, search, read_postings=2, fetch=fetch, today=TODAY))
    fits = {r["url"]: r["fit"] for r in out["results"]}
    assert "posted this week" in fits["https://x/undated"]["reasons"], fits["https://x/undated"]
    assert "posted over two months ago" in fits["https://x/dated"]["concerns"], fits["https://x/dated"]


def test_a_posting_that_wont_read_is_read_from_the_employers_own_page():
    """A Jibe site's opening links to its iCIMS posting, which turns away plain requests (HTTP
    405); its page on the employer's site describes it too. It's still applied for on iCIMS."""
    p = tech()

    async def search(query, location, limit):
        return {"results": [{"company": "Sprouts", "title": "Field Service Engineer 2", "location": "Phoenix, AZ",
                             "url": "https://x.icims.com/jobs/1/job", "company_url": "https://jobs.x.com/jobs/1"}]}

    read: list[str] = []

    async def fetch(url):
        read.append(url)
        if "icims" in url:
            raise RuntimeError("HTTP 405")
        return Posting(url=url, description=LAM_FSE2, apply_url="https://x.icims.com/jobs/login?loginOnly=1")

    out = asyncio.run(recommend(p, search, read_postings=1, fetch=fetch, today=TODAY))
    top = out["results"][0]
    assert read == ["https://x.icims.com/jobs/1/job", "https://jobs.x.com/jobs/1"]
    assert top["posting"]["description"] == LAM_FSE2 and top["posting"]["apply_url"] == "https://x.icims.com/jobs/1/job"


HR_POSTING = """Minimum Qualifications
Bachelor's degree in any field.
1+ years of office experience.
"""


def test_a_title_sharing_only_a_generic_word_isnt_preselected():
    """A made-up HR applicant's "HR Coordinator" target made "Associate Clinical Research
    Coordinator" and "Enrollment Coordinator" (fresh, in Phoenix, requirements met) preselected
    at 80 (a live Find jobs run on the Phoenix list, Oct 2026). "Coordinator" says nothing of
    the work; "Field" in "Field Engineer" does, for a field service target, and "HR",
    "Human Resources" or "Recruiting" do for HR targets."""
    hr = tech(preferences={"titles": ["HR Generalist", "Recruiter", "HR Coordinator"], "locations": ["Phoenix, AZ"]},
              education={"highest_degree": "Bachelor's Degree"})
    fresh = {"location": "Phoenix, AZ", "posted": "2026-10-05"}
    off = score_listing({**fresh, "title": "Associate Clinical Research Coordinator"}, hr, HR_POSTING, today=TODAY)
    assert not off.recommended and "not one of your target titles" in off.concerns
    on = score_listing({**fresh, "title": "HR Coordinator II"}, hr, HR_POSTING, today=TODAY)
    assert on.recommended
    near = score_listing({**fresh, "title": "Field Engineer"}, tech(), today=TODAY)
    assert "not one of your target titles" not in near.concerns  # near a field service target
    for title in ("HR Assistant", "Human Resources Assistant", "Recruiting Coordinator"):
        assert "not one of your target titles" not in score_listing({**fresh, "title": title}, hr).concerns, title
    assert "not one of your target titles" in score_listing({**fresh, "title": "Enrollment Coordinator"}, hr).concerns


def test_an_account_specialist_isnt_near_an_accountant():
    """A made-up finance applicant's "Accountant" target made JPMorgan Chase's "Credit Card
    Customer Service Account Specialist I" (fresh, in Tempe) preselected at 75 (a live Find jobs
    run on the Phoenix list, Oct 2026): "Account" was taken for accounting's first letters."""
    fin = tech(preferences={"titles": ["Financial Analyst", "Accountant"], "locations": ["Phoenix, AZ"]})
    fresh = {"location": "Tempe, AZ", "posted": "2026-10-05"}
    off = score_listing({**fresh, "title": "Credit Card Customer Service Account Specialist I"}, fin, today=TODAY)
    assert not off.recommended and "not one of your target titles" in off.concerns
    for title in ("Accounts Payable Specialist", "Accounts Receivable Clerk", "Accounting Manager", "Finance Manager"):
        assert "not one of your target titles" not in score_listing({**fresh, "title": title}, fin).concerns, title
    for title in ("Key Accounts Manager", "National Accounts Representative"):  # a customer's accounts too
        assert "not one of your target titles" in score_listing({**fresh, "title": title}, fin).concerns, title
    sales = tech(preferences={"titles": ["Account Manager"], "locations": ["Phoenix, AZ"]})
    for title in ("Account Executive", "Key Accounts Manager"):
        assert "not one of your target titles" not in score_listing({**fresh, "title": title}, sales).concerns, title
    assert "not one of your target titles" in score_listing({**fresh, "title": "Staff Accountant"}, sales).concerns


def test_a_job_away_from_the_persons_places_says_so():
    """Freeport-McMoRan's Accountant I openings at its Bagdad and Morenci mines, hours from
    Phoenix, were preselected at 85 for a made-up finance applicant who named Phoenix, Tempe and
    Chandler (a live Find jobs run, Oct 2026). Away from the person's metro area, a job says so,
    and isn't preselected for someone who'd rather not relocate. Remote jobs, state-only places
    and lists of many places aren't judged."""
    places = {"titles": ["Accountant"], "locations": ["Phoenix, AZ", "Tempe, AZ", "Chandler, AZ"]}
    stays = tech(preferences={**places, "willing_to_relocate": False})
    moves = tech(preferences={**places, "willing_to_relocate": True})
    fresh = {"title": "Accountant I", "posted": "2026-10-05"}
    mine = {**fresh, "location": "Bagdad, AZ, US, 86321"}
    far = score_listing(mine, stays, today=TODAY)
    assert not far.recommended and any("Bagdad, AZ" in c and "relocate" in c for c in far.concerns), far
    moving = score_listing(mine, moves, today=TODAY)
    assert moving.recommended and any("Bagdad, AZ" in c for c in moving.concerns), moving
    for where in ("Mesa, AZ", "Remote, AZ, United States", "Dallas, TX, US, 75270 (+80 more); Arizona", "2 Locations",
                  "Arizona", "AZ - Scottsdale Corporate", "tucson-az; Phoenix, AZ"):
        fit = score_listing({**fresh, "location": where}, stays, today=TODAY)
        assert fit.recommended and not any("away from" in c for c in fit.concerns), (where, fit)
    assert any("Tucson, AZ" in c for c in score_listing({**fresh, "location": "tucson-az"}, stays, today=TODAY).concerns)
    # iCIMS writes the country and state first
    assert any("Bagdad, AZ" in c for c in score_listing({**fresh, "location": "US-AZ-Bagdad"}, stays, today=TODAY).concerns)
    for where in ("US-AZ-Ahwatukee", "US-AZ-Phoenix | US-TX-Austin"):
        assert score_listing({**fresh, "location": where}, stays, today=TODAY).recommended, where


def test_a_posting_asking_for_far_more_experience_isnt_preselected():
    """A "Human Resources Business Partner" posting asked a made-up applicant with about 5 years
    for 10+, and was still preselected at 83 (a live Find jobs run, Oct 2026). Far short, it's
    shown but not preselected; a little short, it only scores lower; years that are the other
    way to a degree the applicant has ("a Bachelor's or 4 years") hold nothing back."""
    hr = tech(preferences={"titles": ["Human Resources"], "locations": ["Phoenix, AZ"]})
    job = {"title": "Human Resources Business Partner", "location": "Phoenix, AZ", "posted": "2026-10-05"}
    far = score_listing(job, hr, "Requirements\n10+ years of HR experience.\n", today=TODAY)
    assert far.blocked and not far.recommended and any("10+ years" in c for c in far.concerns)
    near = score_listing(job, hr, "Requirements\n5+ years of HR experience.\n", today=TODAY)
    assert not near.blocked and near.recommended
    grad = tech(preferences={"titles": ["Human Resources"], "locations": ["Phoenix, AZ"]},
                education={"highest_degree": "Bachelor's Degree"},
                work_history=[{"title": "HR Assistant", "company": "Example", "start": "2026-04", "end": "present"}])
    either = score_listing(job, grad, "Requirements\nBachelor's degree or 4 years of related experience.\n", today=TODAY)
    assert not either.blocked and either.recommended


def test_a_posting_that_turns_away_plain_requests_is_read_the_hard_way_last():
    """Aerotek's and TEKsystems' iCIMS postings answer plain requests with HTTP 405, so their
    entry-level recruiter openings (an HR applicant's best matches, live, Oct 2026) were never
    read, so never preselected. They're read by `fetch_hard` (the desk's browser), after the
    plain read and the employer's own page."""
    p = tech()

    async def search(query, location, limit):
        return {"results": [
            {"company": "Aerotek", "title": "Field Service Engineer 2", "location": "Tempe, AZ",
             "url": "https://careers-x.icims.com/jobs/1/job"},
            {"company": "Sprouts", "title": "Field Service Engineer 2", "location": "Phoenix, AZ",
             "url": "https://y.icims.com/jobs/2/job", "company_url": "https://jobs.y.com/jobs/2"}]}

    order: list[str] = []

    async def fetch(url):
        order.append(url)
        if "icims" in url:
            raise RuntimeError("HTTP 405")
        return Posting(url=url, description=LAM_FSE2)

    async def hard(url):
        order.append("hard " + url)
        return Posting(url=url, description=LAM_FSE2, apply_url=url)

    out = asyncio.run(recommend(p, search, read_postings=2, fetch=fetch, fetch_hard=hard, today=TODAY))
    assert {r["company"]: r["posting"]["description"] == LAM_FSE2 for r in out["results"]} == {"Aerotek": True,
                                                                                               "Sprouts": True}
    assert order.index("https://careers-x.icims.com/jobs/1/job") < order.index("hard https://careers-x.icims.com/jobs/1/job")
    assert "hard https://y.icims.com/jobs/2/job" not in order  # its employer's own page read it first


def test_a_reading_with_no_posting_text_falls_through_and_hard_reads_are_few():
    """A page that answers with its menu and no posting (one that builds itself with script) is
    no reading: the next way is tried. Hard reads (the desk's browser, shared with the
    applications) come one at a time, a few, and a portal that turned one away isn't asked
    again."""
    p = tech()
    rows = [{"company": "A", "title": "Field Service Engineer 2", "location": "Tempe, AZ",
             "url": "https://a.icims.com/jobs/1/job", "company_url": "https://jobs.a.com/jobs/1"}]
    rows += [{"company": "B", "title": "Field Service Engineer 2", "location": "Tempe, AZ",
              "url": f"https://b.icims.com/jobs/{n}/job"} for n in range(5)]
    hard_asked: list[str] = []

    async def search(query, location, limit):
        return {"results": rows}

    async def fetch(url):
        if "icims" in url:
            raise RuntimeError("HTTP 405")
        return Posting(url=url, description="Menu | Jobs | Sign in")  # no posting text

    async def hard(url):
        hard_asked.append(url)
        if "b.icims" in url:
            raise RuntimeError("the portal is down")
        return Posting(url=url, description=LAM_FSE2, apply_url=url)

    out = asyncio.run(recommend(p, search, read_postings=10, fetch=fetch, fetch_hard=hard, today=TODAY))
    a = next(r for r in out["results"] if r["company"] == "A")
    assert a["posting"]["description"] == LAM_FSE2  # its own page's menu fell through to the hard read
    assert len([u for u in hard_asked if "b.icims" in u]) == 1  # turned away once: not asked again


def test_the_review_of_oct_15s_reading_slips():
    """A posting that says a degree is NOT required asked for it; "EAR protection" (the safety gear, in capitals) read as
    export control; "eligible for a Secret clearance" read as one needed now; and Phoenixville, PA was "in Phoenix"."""
    for text in ("A bachelor's degree is not required for this role.", "This role does not require a bachelor's degree."):
        assert requirements(text)["degree"] is None, text
    assert requirements("A bachelor's degree is required.")["degree"] == "bachelor"
    # (the review of 0.3.187: only the degree said not required is dropped)
    for text in ("Bachelor's degree required; an advanced degree is not required.",
                 "Master's degree not required; Bachelor's degree required.",
                 "Bachelor's degree in engineering required (a Master's degree is not required)."):
        assert requirements(text)["degree"] == "bachelor", text
    assert requirements("A degree is not required, but a high school diploma or GED is required.")["degree"] == "high_school"
    assert not requirements("EAR/hearing protection required.")["us_person"]
    for text in ("Must be eligible for a Top Secret clearance.", "Eligible to hold a DoD Secret clearance."):
        got = requirements(text)
        assert (got["clearance"], got["clearance_later"]) == (False, True), text
    assert not requirements("Must wear EAR protection and safety glasses at all times.")["us_person"]
    assert not requirements("EAR plugs provided.")["us_person"]
    assert requirements("Access to technology controlled under the EAR.")["us_person"]
    later = requirements("Must be eligible for a Secret clearance.")
    assert (later["clearance"], later["clearance_later"]) == (False, True)
    assert requirements("Requires an active Secret clearance.")["clearance"]
    prof = Profile({"preferences": {"locations": ["Phoenix, AZ"], "titles": ["Technician"]}})
    away = score_listing({"title": "Technician", "company": "Example", "location": "Phoenixville, PA"}, prof)
    assert "in Phoenix" not in away.reasons, away.reasons
    assert "in Phoenix" in score_listing({"title": "Technician", "company": "Example", "location": "Phoenix, AZ"}, prof).reasons


@pytest.mark.parametrize("entry, highest", [
    ({"school": "Example State University", "degree": "BS Electrical Engineering", "start": 2023, "end": 2099}, None),
    ({"school": "Example State University", "degree": "BS Electrical Engineering", "start": 2023, "end": "present"}, None),
    ({"school": "Example State University", "degree": "BS Electrical Engineering", "start": 2016, "end": 2020}, None),
    ({"school": "Example State University", "degree": "", "start": 2016, "end": 2020}, "Bachelor's (in progress)"),
])
def test_a_degree_still_under_way_is_not_counted_as_had(entry, highest):
    """The ranking counted a Bachelor's still under way (a school ending later or "present", or "Bachelor's (in
    progress)") as had, and called the person qualified for postings that require one (the review of Oct 15)."""
    prof = Profile({"education": {"highest_degree": highest} if highest else {}, "education_history": [entry]})
    finished = entry["end"] == 2020 and entry["degree"]
    assert applicant_degree(prof) == ("bachelor" if finished else "high_school")


def test_a_title_written_short_is_near_its_whole_word_and_only_that():
    """The review of 0.3.189: reading a title's short words ("Tech", "Acct") as whole ones also gave every target its
    short words, so any title with "Tech" in it was near an Equipment Technician target ("Tech Sales Representative"
    preselected at 70) and "Key Acct Exec" near an Accountant one (90). A title's short word counts as its whole word
    only, a target never takes in short ones, "Tech" is a technician's only where a technician's title puts it, and
    "Acct" is an Account's."""
    fresh = {"location": "Phoenix, AZ", "posted": "2026-10-05"}
    p = tech()
    for title in ("Tech Sales Representative", "Tech Lead, Platform", "Tech Program Manager", "Ed Tech Coordinator",
                  "Recruiter - Tech", "Tech/Data Recruiter", "Account Executive - Ed Tech", "Adjunct Faculty - ENG"):
        listing = score_listing({**fresh, "title": title}, p, today=TODAY)
        assert not listing.recommended and "not one of your target titles" in listing.concerns, title
    for title in ("Equipment Tech II", "Field Service Engr"):
        assert "not one of your target titles" not in score_listing({**fresh, "title": title}, p).concerns, title
    fin = tech(preferences={"titles": ["Accountant", "Financial Analyst"], "locations": ["Phoenix, AZ"]})
    for title in ("Key Acct Exec", "Sr Acct Mgr"):
        listing = score_listing({**fresh, "title": title}, fin, today=TODAY)
        assert not listing.recommended and "not one of your target titles" in listing.concerns, title
    # (a manager's title written short is a senior one too: "Engr Mgr" as "Engineering Manager", the review's)
    assert score_listing({**fresh, "title": "Engr Mgr"}, p).score == \
        score_listing({**fresh, "title": "Engineering Manager"}, p).score


NEEDS_MUCH = """Requirements
- Bachelor's degree in Electrical Engineering required.
- 8+ years of experience with semiconductor equipment.
- Active Secret clearance required.
- U.S. persons only (ITAR).
- Up to 90% travel.
- Rotating 12-hour night shifts and weekends.
- A valid driver's license is required."""


@pytest.mark.parametrize("title, place, posted, text, who", [
    ("Field Service Engineer", "Phoenix, AZ", "Posted Today", LAM_EARLY, {}),  # a title match, near, fresh
    ("Field Engineer", "Chandler, AZ", "Posted 20 Days Ago", "", {}),  # near a target title, posted this month
    ("Accountant", "Austin, TX", "2026-06-01", "", {}),  # not a target, away, old
    ("Senior Field Service Engineer III", "Phoenix, AZ", "", "", {}),  # senior
    ("Equipment Technician 3", "Bagdad, AZ", "", "", {}),  # a level 3 role, far from the places named
    ("Equipment Technician Intern", "Bagdad, AZ", "2026-01-02", NEEDS_MUCH,  # down to 0: kept within 0 to 100
     {"preferences": {"titles": ["Equipment Technician"], "locations": ["Phoenix, AZ"], "willing_to_relocate": False,
                      "willing_to_travel": False, "flexible_schedule": False},
      "work_authorization": {"us_person": False}, "personal": {"drivers_license": False}}),
    ("Junior Equipment Technician", "Phoenix, AZ", "Posted Today", "Requirements\n- Bachelor's degree required.\n"
     "- 1+ years of experience in a fab.", {"experience": {"total_years": 1}, "education": {"highest_degree": "BS"}}),
])
def test_a_scores_parts_add_up_to_it(title, place, posted, text, who):
    """Code review, worth building 9: the desk shows how each opening's score adds up ("title matches +25, in Phoenix
    +5, ..."), from score_listing's own parts, never worked out again in the page: the start, each point added or taken
    off with what for, and, where the score had to be kept within 0 to 100, that too. They sum to the score. The
    desk's Not interested in an employer takes 10 off its other openings, said as a part as well."""
    from job_apply.recommend import NOT_INTERESTED_COMPANY, START, not_interested_in

    fit = score_listing({"title": title, "location": place, "posted": posted}, tech(**who), text, today=TODAY)
    assert fit.parts[0] == {"why": "start", "points": START}
    assert sum(p["points"] for p in fit.parts) == fit.score, fit.parts
    assert len(fit.parts) > 2 and all(p["points"] for p in fit.parts[1:]), fit.parts
    said = fit.to_dict()
    assert said["parts"] == fit.parts
    lower = not_interested_in(said, "Example Fab")
    assert lower["parts"][-1] == {"why": "you said not interested in Example Fab",
                                  "points": max(0, fit.score - NOT_INTERESTED_COMPANY) - fit.score}
    assert sum(p["points"] for p in lower["parts"]) == lower["score"] == max(0, fit.score - NOT_INTERESTED_COMPANY)
    assert lower["recommended"] == (fit.recommended and lower["score"] >= 60)
    assert said["score"] == fit.score  # (the fit it was given is as it was)


def test_a_score_kept_within_0_to_100_says_so_among_its_parts():
    capped = score_listing({"title": "Junior Equipment Technician", "location": "Phoenix, AZ", "posted": "Posted Today"},
                           tech(experience={"total_years": 1}, education={"highest_degree": "BS"}),
                           "Requirements\n- Bachelor's degree required.\n- 1+ years of experience in a fab.", today=TODAY)
    assert capped.score == 100 and capped.parts[-1]["why"] == "kept within 0 to 100" and capped.parts[-1]["points"] < 0
    floored = score_listing({"title": "Equipment Technician Intern", "location": "Bagdad, AZ", "posted": "2026-01-02"},
                            tech(work_authorization={"us_person": False}), NEEDS_MUCH, today=TODAY)
    assert floored.score == 0 and floored.parts[-1] == {"why": "kept within 0 to 100",
                                                         "points": -sum(p["points"] for p in floored.parts[:-1])}


def test_pay_that_meets_the_desired_salary_adds_five_and_pay_not_listed_takes_nothing():
    """The design of Oct 18, F1: +5 "pay meets your desired $X" only when preferences.desired_salary is set and the
    listed pay reaches it; never a minus, for pay below it or not listed."""
    listing = {"title": "Equipment Technician", "location": "Phoenix, AZ", "posted": "2026-10-01"}
    paid = {**listing, "pay_min": 26, "pay_max": 28, "pay_period": "hour", "pay_source": "text",  # (≈$58,240 at most)
            "pay_sure": True}
    wants = tech(preferences={**tech().get("preferences"), "desired_salary": "$55,000"})
    plain = score_listing(listing, wants, today=TODAY)
    met = score_listing(paid, wants, today=TODAY)
    assert met.score == plain.score + 5 and "pay meets your desired $55,000" in met.reasons
    assert {"why": "pay meets your desired $55,000", "points": 5} in met.parts
    # below what's wanted, or with nothing wanted: no plus and no minus
    high = tech(preferences={**tech().get("preferences"), "desired_salary": "90k"})
    assert score_listing(paid, high, today=TODAY).score == score_listing(listing, high, today=TODAY).score
    assert score_listing(paid, tech(), today=TODAY).score == score_listing(listing, tech(), today=TODAY).score
    assert not any("pay" in p["why"] for p in score_listing(listing, wants, today=TODAY).parts)
    # (the second review) only a sure pay earns it: one read from the words may be wrong
    unsure = {**paid, "pay_sure": False}
    assert score_listing(unsure, wants, today=TODAY).score == plain.score


def test_recommend_takes_a_read_postings_pay_onto_the_opening():
    """A read posting's pay goes on its opening (shown on the desk) and in its posting's fields, and is scored."""
    p = tech(preferences={**tech().get("preferences"), "desired_salary": 50000})

    async def search(query, location, limit):
        return {"results": [{"company": "Example Fab", "title": "Equipment Technician", "url": "https://x/paid",
                             "location": "Phoenix, AZ"}]}

    async def fetch(url):
        return Posting(url=url, description=LAM_FSE2, location="Phoenix, AZ", salary="$26 - $28 an hour",
                       pay_min=26, pay_max=28, pay_period="hour", pay_source="text", pay_sure=True)

    item = asyncio.run(recommend(p, search, read_postings=1, fetch=fetch, today=TODAY))["results"][0]
    assert (item["pay_min"], item["pay_max"], item["pay_period"], item["pay_source"]) == (26, 28, "hour", "text")
    assert item["posting"]["pay_period"] == "hour" and item["posting"]["salary"] == "$26 - $28 an hour"
    assert "pay meets your desired $50,000" in item["fit"]["reasons"]


def test_recommend_takes_where_a_read_posting_says_its_done_when_it_says_it_better():
    """The design of Oct 18, F3: the opening keeps where the list said it's done, unless the posting read says it from
    a better source (its own field over the location's words over its sentences)."""
    async def search(query, location, limit):
        return {"results": [
            {"company": "Example Fab", "title": "Equipment Technician", "url": "https://x/field",
             "location": "Remote - US", "workplace": "remote", "workplace_source": "location"},
            {"company": "Example Fab", "title": "Equipment Technician", "url": "https://x/text",
             "location": "Remote - US", "workplace": "remote", "workplace_source": "location"}]}

    async def fetch(url):
        said = ("hybrid", "structured") if url.endswith("field") else ("onsite", "text")
        return Posting(url=url, description=LAM_FSE2, location="Phoenix, AZ", workplace=said[0],
                       workplace_source=said[1])

    items = {i["url"]: i for i in asyncio.run(recommend(tech(), search, read_postings=2, fetch=fetch,
                                                        today=TODAY))["results"]}
    assert (items["https://x/field"]["workplace"], items["https://x/field"]["workplace_source"]) == \
        ("hybrid", "structured")
    assert (items["https://x/text"]["workplace"], items["https://x/text"]["workplace_source"]) == ("remote", "location")
    assert items["https://x/text"]["posting"]["workplace"] == "onsite"  # (the posting's own, kept with it)


# ------------------------------------------------------------------ the filter row's facts (the design of Oct 18, F5)


@pytest.mark.parametrize("title, level", [
    ("Field Service Engineer Intern", "intern"), ("Co-op, Equipment Engineering", "intern"),
    ("Senior Equipment Technician", "senior"), ("Sr. Field Service Engineer", "senior"),
    ("Lead Process Technician", "senior"), ("Maintenance Supervisor", "senior"), ("Technician III", "senior"),
    ("Technician II", "mid"), ("Technician I", "entry"), ("Field Service Technician I-III", "entry"),
    ("Entry Level Equipment Technician", "entry"), ("Junior Analyst", "entry"), ("Equipment Technician", ""),
])
def test_a_titles_level_is_read_by_the_scores_own_rules(title, level):
    from job_apply.recommend import title_rank

    assert title_rank(title) == level
    assert score_listing({"title": title}, tech(), today=TODAY).to_dict()["facts"] == {"level": level}


@pytest.mark.parametrize("text, travel", [
    ("Must be willing to travel up to 75% of the time.", 75), ("Travel: 0-25%", 25), ("Up to 10% travel.", 10),
    ("Travel required: up to 50%", 50), ("Domestic and international travel up to 40% expected.", 40),
    ("20% domestic travel", 20), ("Travel Percentage: 25%", 25), ("Travel - Up to 30% of the time", 30),
    # not the job's travel: its benefits
    ("We pay travel reimbursement; 401(k) with 100% match.", None),
    ("This role is eligible for 100% travel reimbursement.", None),
    ("We cover 100% of travel costs.", None), ("Some travel may be required.", None),
])
def test_travel_is_said_as_the_jobs_share_of_its_time(text, travel):
    from job_apply.recommend import travel_said

    assert travel_said(text) == travel


@pytest.mark.parametrize("text, said", [
    ("Shift: 1st Shift (United States of America)", ("day", True)),
    ("Shift: 3rd Shift (United States of America)", ("other", True)),
    ("Shift: Nights", ("other", True)), ("Schedule: Days/Nights", ("mixed", True)),
    ("Hours: 6pm - 6am, nights", ("", False)), ("Schedule: Full Time", ("", False)),  # (not "Hours:": the review)
    ("Work Schedule: Monday - Friday", ("", False)),
    # its sentences: shown, never sure (it may be a "no")
    ("This is a day shift role.", ("day", False)), ("We have no night shifts.", ("", False)),
    ("Equipment Technician. Grow your insights.", ("", False)),
])
def test_a_shift_is_sure_only_from_the_postings_own_shift_line(text, said):
    from job_apply.recommend import shift_said

    assert shift_said(text) == said


def test_a_read_postings_requirements_are_its_facts_and_an_unread_one_says_none():
    """The design of Oct 18, F5: the fit carries what the desk's filter chips hide by, the posting's requirements once
    read (the same reading that blocks a job's recommendation) and its title's level always."""
    text = ("Equipment Technician\nMinimum Qualifications:\n- Bachelor's degree in Electrical Engineering required.\n"
            "- Active Secret clearance required.\n- Up to 50% travel.\nShift: Nights\n"
            "Preferred Qualifications:\n- Master's degree preferred.")
    facts = score_listing({"title": "Senior Equipment Technician"}, tech(), text, TODAY).to_dict()["facts"]
    # (a degree and a clearance in its requirements' sentences: shown, never sure, the second review of the filters;
    # only a line labelled with them is)
    assert facts == {"level": "senior", "read": True, "degree": "bachelor", "degree_or_equivalent": False,
                     "degree_sure": False, "clearance": True, "clearance_later": False, "clearance_sure": False,
                     "travel": 50, "travel_sure": True, "shift": "other", "shift_sure": True,
                     "no_sponsorship": False}
    labelled = text + "\nRequired Education: Bachelor's Degree\nClearance Type: Secret"
    facts = score_listing({"title": "Equipment Technician"}, tech(), labelled, TODAY).to_dict()["facts"]
    assert (facts["degree"], facts["degree_sure"], facts["clearance"], facts["clearance_sure"]) == \
        ("bachelor", True, True, True)
    later = ("Requirements:\n- Associate's degree or equivalent experience.\n- Ability to obtain a Secret clearance.\n"
             "Travel to customer sites; travel reimbursement and a 401(k) with 100% match.")
    facts = score_listing({"title": "Equipment Technician"}, tech(), later, TODAY).to_dict()["facts"]
    assert (facts["degree"], facts["degree_or_equivalent"], facts["clearance"], facts["clearance_later"]) == \
        ("associate", True, False, True)
    assert (facts["travel"], facts["travel_sure"]) == (100, False)  # (requirements' wider reading: never hides)
    assert (facts["shift"], facts["shift_sure"]) == ("", False)
    assert score_listing({"title": "Equipment Technician"}, tech(), today=TODAY).to_dict()["facts"] == {"level": ""}


# ------------------------------------------------------------------ titles to leave out (the design of Oct 18, F4)


def test_a_title_to_leave_out_is_matched_as_whole_words_and_never_recommended():
    """preferences.avoid_titles: an opening whose title has one of them as whole words, any case, is blocked with a
    concern (shown under All, never recommended); "Senior" is never found inside "Seniority", nor "Lead" in "Leader"."""
    from job_apply.recommend import avoided_title

    avoid = ["Senior", "call center", "Lead"]
    assert avoided_title("Senior Equipment Technician", avoid) == "Senior"
    assert avoided_title("SENIOR FIELD SERVICE ENGINEER", avoid) == "Senior"
    assert avoided_title("Sr. Technician, Senior-Level", avoid) == "Senior"
    assert avoided_title("Call Center Agent - Equipment Support", avoid) == "call center"
    assert avoided_title("Equipment Technician (Lead)", avoid) == "Lead"
    for kept in ("Seniority Records Clerk", "Equipment Technician", "Team Leader, Equipment", "Callcenter Analyst",
                 "Call Centre Agent", "Center for Call Quality"):
        assert avoided_title(kept, avoid) is None, kept
    assert avoided_title("Senior Technician", []) is None

    wants = tech(preferences={**tech().get("preferences"), "avoid_titles": ["Senior", " senior ", "Call  Center", ""]})
    assert wants.avoid_titles == ["Senior", "Call Center"]  # (each once, as written)
    assert tech(preferences={"avoid_titles": "Senior"}).avoid_titles == ["Senior"]  # (one written bare)
    listing = {"title": "Senior Equipment Technician", "location": "Phoenix, AZ", "posted": "2026-10-01"}
    plain = score_listing(listing, tech(), today=TODAY)
    left = score_listing(listing, wants, today=TODAY)
    assert left.blocked and not left.recommended and left.score == plain.score - 25
    assert "title has “Senior”, which you leave out" in left.concerns
    assert {"why": "title has “Senior”, which you leave out", "points": -25} in left.parts
    fine = score_listing({**listing, "title": "Seniority Equipment Technician"}, wants, today=TODAY)
    assert not fine.blocked and not any("leave out" in c for c in fine.concerns)


# ------------------------------------------------------------------ sponsorship offered (the design of Oct 18, F7)

# a made-up posting (no fixture says it yet), its second requirement the employer's word on sponsorship
WONT_SPONSOR = ("Equipment Technician\nMinimum Qualifications:\n- Associate's degree in electronics.\n"
                "- Must be authorized to work in the U.S. without sponsorship, now or in the future.\n"
                "Benefits: medical, dental, 401(k).")


def test_a_posting_that_wont_sponsor_blocks_only_a_person_who_needs_sponsorship():
    """requirements()["no_sponsorship"], read from the posting's plain words: -30 and blocked (shown, never
    recommended) only for a profile whose work_authorization.requires_sponsorship is true; for anyone else it's
    information only (the fit's facts), with no point taken."""
    assert requirements(WONT_SPONSOR)["no_sponsorship"] is True
    open_text = WONT_SPONSOR.replace("without sponsorship", "with or without sponsorship")
    assert requirements(open_text)["no_sponsorship"] is False
    listing = {"title": "Equipment Technician", "location": "Phoenix, AZ", "posted": "2026-10-01"}
    needs = tech(work_authorization={"us_person": True, "requires_sponsorship": True})
    blocked = score_listing(listing, needs, WONT_SPONSOR, TODAY)
    open_to = score_listing(listing, needs, open_text, TODAY)
    assert blocked.blocked and not blocked.recommended and blocked.score == open_to.score - 30
    assert "says it won't sponsor a work visa (your profile says you need sponsorship)" in blocked.concerns
    assert {"why": "says it won't sponsor a work visa", "points": -30} in blocked.parts
    assert blocked.facts["no_sponsorship"] is True and open_to.facts["no_sponsorship"] is False
    for who in (tech(), tech(work_authorization={"us_person": True, "requires_sponsorship": False})):
        fit = score_listing(listing, who, WONT_SPONSOR, TODAY)
        assert not fit.blocked and fit.recommended and fit.facts["no_sponsorship"] is True
        assert not any("sponsor" in p["why"] for p in fit.parts) and not any("sponsor" in c for c in fit.concerns)
