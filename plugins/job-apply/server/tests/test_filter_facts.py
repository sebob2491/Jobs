"""The desk's filter chips, read over the second review's strings (the design of Oct 18, F5, F3, F6): as for pay, a
reading that isn't surely the job's never hides it. Each string is made up (or a common wording), with what the chip
should do: True hides, False keeps, None either (a reading that may go either way). The chips' tests here are the
desk page's own (static/desk.html's CHIP_TESTS), in Python."""

import pytest
from bs4 import BeautifulSoup

from job_apply import employment, workplace
from job_apply.postings import QUESTIONS_HEADING, icims_workplace, smartrecruiters_workplace, successfactors_workplace
from job_apply.recommend import posting_facts, requirements, shift_said, title_rank, travel_said

_LEVEL = {"high_school": 1, "associate": 2, "bachelor": 3, "master": 4, "doctorate": 5}


def _hides(facts: dict, mine: str | None) -> dict:
    """What each Requirements chip does to an opening of these facts (desk.html's CHIP_TESTS.requirements), for a
    person whose highest degree is `mine`."""
    f = facts
    return {
        "no_clearance": bool(f.get("clearance_sure") and (f.get("clearance") or f.get("clearance_later"))),
        "degree": bool(f.get("degree_sure") and f.get("degree") and f.get("degree") != "high_school"
                       and not f.get("degree_or_equivalent")
                       and _LEVEL.get(f["degree"], 0) > _LEVEL.get(mine or "", 1)),
        "day_shift": bool(f.get("shift_sure") and f.get("shift") == "other"),
        "travel_25": bool(f.get("travel_sure") and f.get("travel") is not None and f["travel"] > 25),
    }


_WRAP = ("Job Description\nWe build semiconductor equipment.\n\nQualifications:\n{}\n\nBenefits: medical, dental, "
         "401(k). EEO: veterans and disabled encouraged.\n")

# (text, the person's degree, {chip: hides?}): each inside a posting's Qualifications, its benefits after
WRAPPED = [
 # degree
 ("Bachelor's degree preferred.", "high_school", {"degree": False}),
 ("Bachelor's degree or equivalent experience.", "high_school", {"degree": False}),
 ("Bachelor's degree or equivalent experience required.", "high_school", {"degree": False}),
 ("Bachelor's degree in Engineering required.", "high_school", {"degree": True}),
 ("Bachelor's degree in Engineering required.", "bachelor", {"degree": False}),
 ("Master's degree preferred.", "bachelor", {"degree": False}),
 ("Bachelor's degree required, Master's preferred.", "bachelor", {"degree": False}),
 ("Bachelor's degree required; Master's degree preferred", "bachelor", {"degree": False}),
 ("BS required, MS preferred", "bachelor", {"degree": False}),
 ("BS in EE required; MS a plus.", "bachelor", {"degree": False}),
 ("HS diploma required, BS a plus.", "high_school", {"degree": False}),
 ("High school diploma or GED required. Bachelor's degree a plus.", "high_school", {"degree": False}),
 ("High school diploma or GED required; associate's degree preferred.", "high_school", {"degree": False}),
 ("Degree preferred.", "high_school", {"degree": False}),
 ("A degree is preferred but not required.", "high_school", {"degree": False}),
 ("Bachelor's degree preferred but not required.", "high_school", {"degree": False}),
 ("Bachelor's degree (preferred)", "high_school", {"degree": False}),
 ("Bachelor's degree - preferred", "high_school", {"degree": False}),
 ("Bachelors Degree (Preferred)", "high_school", {"degree": False}),
 ("Education: Bachelor's (Preferred)", "high_school", {"degree": False}),
 ("Education: Bachelor's degree (Required)", "high_school", {"degree": True}),
 ("Bachelor's or Master's degree in Physics.", "bachelor", {"degree": False}),
 ("Master's or PhD in Chemistry required.", "bachelor", {"degree": True}),
 ("Master's degree in Engineering or Bachelor's with 5 years of experience.", "bachelor", {"degree": False}),
 ("MS with 3 years of experience or BS with 6 years.", "bachelor", {"degree": False}),
 ("PhD, or MS with 4+ years, or BS with 8+ years of experience.", "bachelor", {"degree": False}),
 ("Associate's degree in electronics or 4 years of military electronics training.", "high_school", {"degree": False}),
 ("Associate's degree in electronics or equivalent military training.", "high_school", {"degree": False}),
 ("Associate's degree in electronics, military technical training, or trade school.", "high_school", {"degree": None}),
 ("Associate degree or technical certification.", "high_school", {"degree": False}),
 ("Bachelor's degree from an accredited university.", "high_school", {"degree": True}),
 ("Bachelor's degree in Engineering with 3+ years of experience.", "bachelor", {"degree": False}),
 ("Bachelor's degree required. Equivalent combination of education and experience will be considered.", "high_school",
  {"degree": False}),
 ("Bachelor's degree required.\nIn lieu of a degree, 4 additional years of related experience.", "high_school",
  {"degree": False}),
 ("Typically requires a Bachelor's degree and 2 years of experience, or a Master's degree.", "bachelor",
  {"degree": False}),
 ("Typically requires a minimum of 3 years of related experience with a Bachelor's degree; "
  "or 1 year and a Master's degree; or a PhD without experience; or equivalent experience.", "high_school",
  {"degree": False}),
 ("Education: Some college preferred.", "high_school", {"degree": False}),
 ("A master's degree is not required.", "bachelor", {"degree": False}),
 ("No degree required.", "high_school", {"degree": False}),
 ("Degree not required.", "high_school", {"degree": False}),
 ("We do not require a college degree.", "high_school", {"degree": False}),
 ("Pursuing a Bachelor's degree in Engineering.", "high_school", {"degree": None}),
 ("Currently enrolled in a Master's program.", "bachelor", {"degree": None}),
 ("Tuition reimbursement for your master's degree.", "bachelor", {"degree": False}),
 ("We offer tuition assistance toward a bachelor's degree.", "high_school", {"degree": False}),
 ("Our team includes engineers with master's degrees and PhDs.", "bachelor", {"degree": False}),
 ("Mentor technicians; work with PhD scientists.", "bachelor", {"degree": False}),
 ("Experience with MS Office required.", "high_school", {"degree": False}),
 ("Proficient in MS Excel and MS Word.", "high_school", {"degree": False}),
 ("Strong MS Excel skills required.", "high_school", {"degree": False}),
 ("Ability to work with BA teams.", "high_school", {"degree": False}),
 ("Experience with MS SQL Server.", "high_school", {"degree": False}),
 ("Experience with MS Dynamics required.", "high_school", {"degree": False}),
 ("Knowledge of MS Teams and SharePoint.", "high_school", {"degree": False}),
 ("Degree in Electrical Engineering.", "high_school", {"degree": True}),
 ("A degree in a related field.", "high_school", {"degree": None}),
 ("Associate's degree required; Bachelor's degree in a technical field strongly preferred.", "associate",
  {"degree": False}),
 ("Associate's degree required. Bachelor's degree preferred.", "associate", {"degree": False}),
 ("Bachelor's degree in Business, HR, or related field preferred; or equivalent experience.", "high_school",
  {"degree": False}),
 ("Bachelor's degree in Human Resources or a related field, or 4 years of HR experience.", "high_school",
  {"degree": False}),
 ("SHRM-CP or PHR certification preferred. Bachelor's degree required.", "high_school", {"degree": True}),
 ("Bachelor's degree from an accredited institution (required). Master's degree (preferred).", "bachelor",
  {"degree": False}),
 ("Master's degree preferred; Bachelor's degree required.", "bachelor", {"degree": False}),
 ("Minimum Qualifications:\nBachelor's degree\nPreferred Qualifications:\nMaster's degree", "bachelor",
  {"degree": False}),
 ("Basic Qualifications:\n- High school diploma\nPreferred Qualifications\n- Bachelor's degree in engineering",
  "high_school", {"degree": False}),
 ("What you'll need:\nHS Diploma\nNice to have:\nBS in Physics", "high_school", {"degree": False}),
 ("Preferred:\nBS in EE\nRequired:\nHigh school diploma", "high_school", {"degree": False}),
 ("Education Preferred: Bachelor's Degree", "high_school", {"degree": False}),
 ("Education:\nBachelor's Degree (Preferred)\nExperience:\n2 years (Required)", "high_school", {"degree": False}),
 ("Bachelor's Degree desired.", "high_school", {"degree": False}),
 ("Ideally a Bachelor's degree in engineering.", "high_school", {"degree": False}),
 ("Bachelor's degree strongly preferred.", "high_school", {"degree": False}),
 ("Bachelor's degree is highly desirable.", "high_school", {"degree": False}),
 ("Bachelor's degree would be an advantage.", "high_school", {"degree": False}),
 ("Bachelor's degree is beneficial.", "high_school", {"degree": False}),
 ("Bachelor's degree is helpful but not necessary.", "high_school", {"degree": False}),
 ("Bachelor's degree or higher is optional.", "high_school", {"degree": False}),
 ("Bachelor's degree, or equivalent combination of education and experience.", "high_school", {"degree": False}),
 ("Bachelor's degree or equivalent work experience", "high_school", {"degree": False}),
 ("Bachelor's degree OR 4 years relevant experience", "high_school", {"degree": False}),
 ("Bachelor's degree / equivalent experience", "high_school", {"degree": False}),
 ("Bachelor's degree (or equivalent experience)", "high_school", {"degree": False}),
 ("Bachelor's degree; or high school diploma and 4 years of experience.", "high_school", {"degree": False}),
 ("Bachelor's degree, or high school diploma plus 4 years of experience.", "high_school", {"degree": False}),
 ("Bachelor's degree preferred, high school diploma required.", "high_school", {"degree": False}),
 ("Bachelor of Science in Engineering, Masters a plus.", "bachelor", {"degree": False}),
 # clearance
 ("Must be able to obtain a Secret clearance.", None, {"no_clearance": True}),
 ("Ability to obtain a security clearance.", None, {"no_clearance": True}),
 ("Active Secret clearance required.", None, {"no_clearance": True}),
 ("Active Secret clearance preferred.", None, {"no_clearance": False}),
 ("Active TS/SCI clearance a plus.", None, {"no_clearance": False}),
 ("No clearance required.", None, {"no_clearance": False}),
 ("A security clearance is not required for this role.", None, {"no_clearance": False}),
 ("This position does not require a security clearance.", None, {"no_clearance": False}),
 ("We hold facility clearances with the DoD; this role does not need one.", None, {"no_clearance": False}),
 ("We hold Top Secret facility clearances.", None, {"no_clearance": False}),
 ("Our company holds a Secret facility clearance.", None, {"no_clearance": False}),
 ("Applicants selected may be subject to a government security investigation and must meet eligibility requirements "
  "for access to classified information.", None, {"no_clearance": None}),
 ("Equal opportunity employer: veterans, disability. Security clearance status is not a factor.", None,
  {"no_clearance": False}),
 ("Medical clearance required (drug screen).", None, {"no_clearance": False}),
 ("Must pass a background check and security clearance check at customer site.", None, {"no_clearance": None}),
 ("Must pass a background check and drug screen.", None, {"no_clearance": False}),
 ("Badge access requires site security clearance.", None, {"no_clearance": None}),
 ("Height clearance of 14 feet required for forklift operation.", None, {"no_clearance": False}),
 ("Maintain safe clearance around electrical panels; clearance required by OSHA.", None, {"no_clearance": False}),
 ("Clearance: None", None, {"no_clearance": False}),
 ("Security Clearance: None", None, {"no_clearance": False}),
 ("Security Clearance Required: No", None, {"no_clearance": False}),
 ("Clearance Level: None/Not Required", None, {"no_clearance": False}),
 ("US citizenship required due to ITAR; no clearance needed.", None, {"no_clearance": False}),
 ("Secret clearance preferred but not required.", None, {"no_clearance": False}),
 ("Candidates with an active clearance preferred.", None, {"no_clearance": False}),
 ("DoD clearance is a plus.", None, {"no_clearance": False}),
 ("Preferred Qualifications:\nActive Secret clearance", None, {"no_clearance": False}),
 ("Inventory clearance and stocking duties.", None, {"no_clearance": False}),
 ("Customs clearance documentation for international shipments required.", None, {"no_clearance": False}),
 ("Security clearance required: None", None, {"no_clearance": False}),
 # travel
 ("Travel up to 10%.", None, {"travel_25": False}),
 ("Up to 50% travel to customer sites.", None, {"travel_25": True}),
 ("Travel reimbursement available. 401(k) with 100% match.", None, {"travel_25": False}),
 ("Travel: 0-25%", None, {"travel_25": False}),
 ("Travel: 25% - 50%", None, {"travel_25": True}),
 ("Willingness to travel up to 75% domestically.", None, {"travel_25": True}),
 ("Travel expenses reimbursed at 100%.", None, {"travel_25": False}),
 ("Travel required: less than 10%", None, {"travel_25": False}),
 ("Travel percentage: 20%", None, {"travel_25": False}),
 ("We pay 100% of travel costs.", None, {"travel_25": False}),
 ("Travel and 100% employer-paid medical.", None, {"travel_25": False}),
 ("Travel insurance; 401k 6% match; tuition 100% covered.", None, {"travel_25": False}),
 ("Business travel accident insurance paid 100% by company.", None, {"travel_25": False}),
 ("We cover 100% travel expenses for relocation.", None, {"travel_25": False}),
 ("Paid travel time at 150% of base rate.", None, {"travel_25": False}),
 ("Travel: No", None, {"travel_25": False}),
 ("Travel: Yes, 50% of the time", None, {"travel_25": True}),
 ("This role requires approximately 30% travel.", None, {"travel_25": True}),
 ("30-40% overnight travel", None, {"travel_25": True}),
 ("Reduce travel costs by 30%.", None, {"travel_25": False}),
 ("Cut travel spend 40% year over year.", None, {"travel_25": False}),
 ("Coordinate travel for 50% of the executive team.", None, {"travel_25": False}),
 ("Travel nurses receive 100% coverage.", None, {"travel_25": False}),
 ("travel and expense audits; ensure 100% compliance.", None, {"travel_25": False}),
 ("Travel is required at 100% to customer sites (field service).", None, {"travel_25": True}),
 ("Travel: Up to 100%", None, {"travel_25": True}),
 ("Ability to travel 100% of the time", None, {"travel_25": True}),
 ("Travel reimbursement of 50% for commuting.", None, {"travel_25": False}),
 ("Travel stipend of 100% of mileage.", None, {"travel_25": False}),
 ("Travel time paid at 100%.", None, {"travel_25": False}),
 ("Travel policy covers 100% of airfare.", None, {"travel_25": False}),
 ("Travel and lodging covered 100%.", None, {"travel_25": False}),
 ("Travel, meals: 100% reimbursed", None, {"travel_25": False}),
 ("Manage a 100% travel budget increase", None, {"travel_25": False}),
 # shift
 ("Shift: Days", None, {"day_shift": False}),
 ("Shift: Nights", None, {"day_shift": True}),
 ("We value insights from all employees.", None, {"day_shift": False}),
 ("This is a 2nd shift position.", None, {"day_shift": None}),
 ("Shift: 2nd Shift (United States of America)", None, {"day_shift": True}),
 ("Shift: 1st Shift (United States of America)", None, {"day_shift": False}),
 ("Schedule: 4x10s, Monday-Thursday", None, {"day_shift": False}),
 ("Schedule: Monday - Friday, 8:00am - 5:00pm", None, {"day_shift": False}),
 ("Schedule: Full-time", None, {"day_shift": False}),
 ("Schedule: Day shift, with occasional overtime on second shift as needed", None, {"day_shift": False}),
 ("Hours: 6:00 PM - 6:30 AM", None, {"day_shift": None}),
 ("Hours: 40 per week; occasional evenings", None, {"day_shift": False}),
 ("Schedule: Days with on-call rotation", None, {"day_shift": False}),
 ("Shift: Day shift, rotating weekends", None, {"day_shift": False}),
 ("Work Schedule: Monday through Friday, first shift; second shift training for two weeks", None, {"day_shift": False}),
 ("Schedule: 12 hour shifts, days and nights", None, {"day_shift": None}),
 ("Schedule: 3 days a week", None, {"day_shift": False}),
 ("Shift: Weekend Days (Fri-Sun)", None, {"day_shift": False}),
 ("Schedule: Second Monday of each month off", None, {"day_shift": False}),
 ("Hours: Flexible, no nights", None, {"day_shift": False}),
 ("Shift: Not nights", None, {"day_shift": False}),
 ("Schedule: Mon-Fri, no weekends or nights", None, {"day_shift": False}),
 ("Hours: 7a-3:30p, must be flexible to cover evenings occasionally", None, {"day_shift": False}),
 ("Shift: Variable", None, {"day_shift": False}),
 ("Schedule: Business hours (no evenings)", None, {"day_shift": False}),
 ("Hours: Day shift; 2nd shift available for those who prefer it", None, {"day_shift": False}),
 ("Schedule: First or second shift", None, {"day_shift": None}),
]

# whole postings, or their parts, as written
WHOLE = [
 # full-ish postings: (text, mine, expected)
 ("About Us\nGrand Canyon University offers more than 300 bachelor's, master's and doctoral degree programs.\n\n"
  "Responsibilities\nAnswer phones.\n\nQualifications\nHigh school diploma or GED required.", "high_school",
  {"degree": False}),
 ("Arizona State University is a comprehensive public research university that offers bachelor's, master's and "
  "doctoral degrees.\nMinimum Qualifications\nHigh school diploma or GED.", "high_school", {"degree": False}),
 ("Job Summary\nAssist students pursuing their bachelor's degree.\nMinimum Qualifications\nHigh school diploma.",
  "high_school", {"degree": False}),
 ("Responsibilities\n- Advise students on master's degree programs.\nRequirements\n- 2 years of customer service.",
  "high_school", {"degree": False}),
 ("Qualifications\n- 2 years of experience\nBenefits\n"
  "- Tuition reimbursement for associate, bachelor's and master's degrees", "high_school", {"degree": False}),
 ("Benefits:\nTuition-free bachelor's degree through our partnership with ASU.", "high_school", {"degree": False}),
 ("Why join us? Earn your bachelor's degree tuition-free through the Starbucks College Achievement Plan.",
  "high_school", {"degree": False}),
 ("Perks: 100% tuition coverage for a bachelor's degree.", "high_school", {"degree": False}),
 ("Work alongside PhD-level scientists in our R&D lab.\nRequirements:\n"
  "High school diploma; 2 years technician experience.", "high_school", {"degree": False}),
 ("Responsibilities:\nSupport engineers (BS/MS) in the lab.", "high_school", {"degree": False}),
 ("Responsibilities: Process degree audits for graduating bachelor's students.", "high_school", {"degree": False}),
 ("Teach associate degree courses in nursing. Requirements: Master's degree in Nursing.", "bachelor", {"degree": True}),
 # degree in prose, mixed
 ("Requirements:\nBachelor's degree in a STEM field is a plus but not required.", "high_school", {"degree": False}),
 ("Requirements:\nCollege degree a bonus.", "high_school", {"degree": False}),
 ("Requirements:\nAn associate's degree would be nice.", "high_school", {"degree": False}),
 ("Requirements:\nBachelor's degree – or – 4 years of experience in lieu of degree.", "high_school", {"degree": False}),
 ("Requirements:\nBachelor's degree\nOR\n4 years of related experience", "high_school", {"degree": False}),
 ("Requirements:\n• Bachelor's degree\n• 4+ years of experience may substitute for degree", "high_school",
  {"degree": False}),
 ("Requirements:\nBachelor's degree; additional experience may be considered in place of education.", "high_school",
  {"degree": False}),
 ("Requirements:\nBachelor's degree or military experience.", "high_school", {"degree": False}),
 ("Requirements:\nBachelor's degree or comparable experience.", "high_school", {"degree": False}),
 ("Requirements:\nBachelor's degree or commensurate experience.", "high_school", {"degree": False}),
 ("Requirements:\nBachelor's degree or applicable experience.", "high_school", {"degree": False}),
 ("Requirements:\nBachelor's degree, or a combination of education and experience.", "high_school", {"degree": False}),
 ("Requirements:\nBachelor's Degree and 4 years of experience OR Master's Degree and 2 years OR 8 years of experience.",
  "high_school", {"degree": False}),
 ("Requirements:\nBachelor's degree with 2 years experience; Master's with 0 years; PhD.", "bachelor",
  {"degree": False}),
 ("Education/Experience: Master's degree preferred, Bachelor's degree required", "bachelor", {"degree": False}),
 ("Education: Bachelor's Degree Required; Master's Degree Preferred", "bachelor", {"degree": False}),
 ("Education Level: Bachelor's Degree", "bachelor", {"degree": False}),
 ("Required Education: Master's Degree", "bachelor", {"degree": True}),
 ("Minimum Education: Associate's degree; Preferred Education: Bachelor's degree", "associate", {"degree": False}),
 # Clearance label formats
 ("Clearance Type: None", None, {"no_clearance": False}),
 ("Clearance Type: Secret", None, {"no_clearance": True}),
 ("Security Clearance Type: None", None, {"no_clearance": False}),
 ("Security Clearance: None/Not Required", None, {"no_clearance": False}),
 ("Clearance Level Must Currently Possess: None\nClearance Level Must Be Able to Obtain: None", None,
  {"no_clearance": False}),
 ("Security Clearance Level: N/A", None, {"no_clearance": False}),
 ("Security Clearance Required?: No", None, {"no_clearance": False}),
 ("Clearance Level Required: None", None, {"no_clearance": False}),
 ("Security Clearance: This position does not require a security clearance.", None, {"no_clearance": False}),
 ("Security Clearance: There is no security clearance required for this position.", None, {"no_clearance": False}),
 ("Security clearance not needed.", None, {"no_clearance": False}),
 ("No security clearance is required.", None, {"no_clearance": False}),
 ("Not required to hold a security clearance.", None, {"no_clearance": False}),
 ("Required Clearance: None", None, {"no_clearance": False}),
 ("Applicants selected will be subject to a security investigation and may need to meet eligibility requirements for "
  "access to classified information.", None, {"no_clearance": None}),
 ("Arizona IVP Fingerprint Clearance required.", None, {"no_clearance": False}),
 ("Must possess a valid Level 1 IVP Fingerprint Clearance Card.", None, {"no_clearance": False}),
 ("Fingerprint clearance is required prior to start.", None, {"no_clearance": False}),
 ("Must obtain a DPS fingerprint clearance card within 7 days of hire.", None, {"no_clearance": False}),
 ("Medical clearance is required (TB test, immunizations).", None, {"no_clearance": False}),
 ("Employee Health clearance required before start.", None, {"no_clearance": False}),
 ("Must pass background clearance.", None, {"no_clearance": None}),
 ("Background clearance required.", None, {"no_clearance": None}),
 ("Tax clearance required for vendor payments.", None, {"no_clearance": False}),
 ("Airport security clearance (SIDA badge) required.", None, {"no_clearance": None}),
 ("Must be able to obtain an airport SIDA badge.", None, {"no_clearance": False}),
 ("Must be able to obtain and maintain a DOE Q clearance.", None, {"no_clearance": True}),
 ("Responsibilities: Maintain security clearance records for staff; process clearance requests.", None,
  {"no_clearance": False}),
 ("Responsibilities: Coordinate DoD clearance paperwork for new hires (FSO assistant).", None, {"no_clearance": None}),
 ("Northrop Grumman holds a facility security clearance and supports national defense.", None, {"no_clearance": False}),
 ("Our facility maintains a Top Secret clearance.", None, {"no_clearance": False}),
 ("Security clearance: preferred.", None, {"no_clearance": False}),
 ("Secret clearance (preferred)", None, {"no_clearance": False}),
 ("Preferred Qualifications\n- Active Secret clearance\nBasic Qualifications\n- 2 years experience", None,
  {"no_clearance": False}),
 ("Basic Qualifications\n- 2 years experience\nPreferred Qualifications\n- Active Secret clearance", None,
  {"no_clearance": False}),
 ("Basic Qualifications:\n- US citizenship\nPreferred Qualifications:\n- Active DoD clearance\nAbout Us\n"
  "We hold clearances.", None, {"no_clearance": False}),
 # Northrop format: travel and shift lines
 ("Travel: Yes, 10% of the Time\nShift: 1st Shift (United States of America)\nClearance Type: None", None,
  {"travel_25": False, "day_shift": False, "no_clearance": False}),
 ("Travel: Yes, 50% of the Time", None, {"travel_25": True}),
 ("Shift: 9/80", None, {"day_shift": False}),
 ("Schedule: Mon-Fri; on-call rotation", None, {"day_shift": False}),
 ("Hours: Monday - Friday, 8-5 with weekend rotation", None, {"day_shift": False}),
 ("Schedule: Monday-Friday, every third Saturday", None, {"day_shift": False}),
 ("Shift: Day, 7a-7p, every other weekend", None, {"day_shift": False}),
 ("Hours: 8:00 AM - 4:30 PM, with the second half of the week remote", None, {"day_shift": False}),
 ("Schedule: Day shift with occasional evening events", None, {"day_shift": False}),
 ("Schedule: Daytime hours, plus an evening class once a week", None, {"day_shift": False}),
 ("Work Schedule: Monday-Friday; 1st and 2nd shift coverage available", None, {"day_shift": False}),
 ("Hours: Mornings only, 6am - 11am", None, {"day_shift": False}),
 ("Shift: Evening", None, {"day_shift": True}),
 ("Shift: Night", None, {"day_shift": True}),
 ("Shift: Overnight", None, {"day_shift": True}),
 ("Schedule: Swing", None, {"day_shift": True}),
 ("Schedule: Rotating 12s (days/nights)", None, {"day_shift": None}),
 ("Shift: 3rd", None, {"day_shift": True}),
 ("Hours: 40, shift differential paid for nights", None, {"day_shift": None}),
 ("Hours: Full time", None, {"day_shift": False}),
 ("Schedule: Second shift", None, {"day_shift": True}),
 ("Schedule: Days (the first two weeks of training on nights)", None, {"day_shift": False}),
 # travel phrasing
 ("Travel: 25%", None, {"travel_25": False}),
 ("Travel: 26%", None, {"travel_25": True}),
 ("Travel up to 25-50%", None, {"travel_25": True}),
 ("Travel: 10% (overnight stays rare); 401(k) 100% vested", None, {"travel_25": False}),
 ("Up to 10% travel. Employer pays 100% of travel costs.", None, {"travel_25": False}),
 ("Benefits: 100% travel reimbursement.", None, {"travel_25": False}),
 ("Book travel for 100% of company events.", None, {"travel_25": False}),
 ("Responsibilities: process travel claims with 100% accuracy", None, {"travel_25": False}),
 ("Responsibilities: travel coordinator; ensure 99% on-time travel booking", None, {"travel_25": False}),
 ("Travel ≥ 50%", None, {"travel_25": True}),
 ("Reduce business travel by 50%.", None, {"travel_25": False}),
 ("Arrange travel for our 100% remote team.", None, {"travel_25": False}),
 ("Travel nurse: 100% of shifts at partner hospitals", None, {"travel_25": None}),
 ("Travel agent role: 100% commission", None, {"travel_25": False}),
 ("Field engineer: travel 80-100%", None, {"travel_25": True}),
 ("The role requires 10% international travel and 15% domestic travel.", None, {"travel_25": False}),
 ("Responsibilities: Manage travel & expense (T&E) with 100% policy compliance", None, {"travel_25": False}),
 ("Responsibilities: Travel booking; achieve 95% traveler satisfaction", None, {"travel_25": False}),
 ("Travel: minimal; 100% company-paid when needed", None, {"travel_25": False}),
 ("Travel required: 0%", None, {"travel_25": False}),
 ("Business travel is 100% optional", None, {"travel_25": False}),
]


def _cases(cases, wrap):
    return [pytest.param(text, mine, expect, wrap, id=f"{i}") for i, (text, mine, expect) in enumerate(cases)]


# Requirements the posting states only in its sentences: shown ("asks a Bachelor's, read from the text", "may need a
# security clearance"), never hidden, as no section of a posting can be told apart surely (the second review of the
# filters: only a line labelled with the requirement and its strength hides a job)
READ_FROM_TEXT = {
    ("Bachelor's degree in Engineering required.", "degree"), ("Master's or PhD in Chemistry required.", "degree"),
    ("Bachelor's degree from an accredited university.", "degree"), ("Degree in Electrical Engineering.", "degree"),
    ("SHRM-CP or PHR certification preferred. Bachelor's degree required.", "degree"),
    ("Teach associate degree courses in nursing. Requirements: Master's degree in Nursing.", "degree"),
    ("Must be able to obtain a Secret clearance.", "no_clearance"),
    ("Ability to obtain a security clearance.", "no_clearance"), ("Active Secret clearance required.", "no_clearance"),
    ("Must be able to obtain and maintain a DOE Q clearance.", "no_clearance"),
}


@pytest.mark.parametrize("text, mine, expect, wrap", _cases(WRAPPED, True) + _cases(WHOLE, False))
def test_a_requirement_chip_hides_only_for_a_sure_reading(text, mine, expect, wrap):
    full = _WRAP.format(text) if wrap else text
    facts = posting_facts("Technician", requirements(full), full)
    got = _hides(facts, mine)
    for chip, should in expect.items():
        if (text, chip) in READ_FROM_TEXT:
            should = False
        if should is not None:
            assert got[chip] is should, (chip, facts)


@pytest.mark.parametrize("text, travel", [
    ("Benefits\n- Paid Travel\n100% employer-paid medical", None),
    ("Perks:\nTravel\n50% off hotel stays (team member rate)", None),
    ("Team member travel discounts\n- 50% off rooms", None), ("Travel Requirements\n50%", None),
    ("Travel: Less than 50%", 50), ("Travel 50% of the time during the first 6 months of training, then 10%", 50),
    ("Travel perks: 30% off flights", None), ("Travel discounts of up to 40% at our resorts", None),
    ("Travel - up to 30% employee discount on cruises", None), ("Travel benefits: 90% off standby flights", None),
    ("Travel privileges\n90% off airfare", None), ("Travel: Up to 10%; overtime at 150%", 10),
    ("Business travel: 5%\nTuition: 100% covered", 5), ("Travel: 0%\n401(k): 100% match up to 6%", 0),
    ("Travel is required to all 3 sites. 100% of time on the floor.", None),
    ("Travel: occasional.\n50% of the role is customer-facing.", None),
    ("Willing to travel? Yes\n75% of applicants", None), ("Travel Amount: 25%", 25),
    ("Travel Percentage Required: 0 - 25%", 25), ("Travel: 25-50% (domestic)", 50),
])
def test_travel_is_read_on_its_own_line_and_never_from_a_discount_or_a_benefit(text, travel):
    assert travel_said(text) == travel


@pytest.mark.parametrize("text, hides", [
    ("Shift: Days", False), ("Hours: 40 per week; some evenings and weekends", False),
    ("Schedule: Monday through Friday; must be available for the second shift once a quarter", False),
    ("Shift: Graveyard", True), ("Shift: Varies", False), ("Shift: Day/Swing/Grave", False),
    ("Hours: Varies, including evenings", False), ("Schedule: 9/80 (every second Friday off)", False),
    ("Shift: 1st", False), ("Schedule: Days; first 3 weeks of orientation on nights", False),
    ("Schedule: No night shifts", False), ("Shift: Days, no nights or weekends", False),
    ("Shift: Evening (3pm-11:30pm)", True), ("Schedule: rotating weekends, day shift", False),
    ("Work Hours: Day shift with 1-in-3 weekend rotation", False),
    ("Hours: Business hours; after-hours on-call rotation", False),
])
def test_a_shift_line_hides_only_for_another_shift_said_whole(text, hides):
    shift, sure = shift_said(text)
    assert (sure and shift == "other") is hides, (shift, sure)


@pytest.mark.parametrize("text, blocks", [
    # the score's own clearance reading (requirements()): Boeing's heading names one, its words don't ask one
    ("Security Clearance and Export Control Requirements: This position must meet U.S. export control compliance "
     "requirements. To meet U.S. export control compliance requirements, a 'U.S. Person' as defined by 22 C.F.R. "
     "§120.62 is required.", False),
    ("Security Clearance: This position does not require a Security Clearance.", False),
    ("Security Clearance: This position does not require a security clearance.\nVisa Sponsorship: Employer will not "
     "sponsor applicants for employment visa status.", False),
    ("Clearance Level: None", False), ("Security Clearance Level: None", False),
    ("Security Clearance Level: Secret", True), ("Clearance: None", False), ("Security clearance: N/A", False),
    ("Applicants must be U.S. citizens; no security clearance is needed for this role.", False),
    ("No active clearance needed.", False), ("Clearance not required, but must be a US person.", False),
    ("Must be able to pass a background check; security clearance not required.", False),
])
def test_the_score_reads_no_clearance_from_a_heading_or_a_line_that_says_none(text, blocks):
    req = requirements(text)
    assert (req["clearance"] or req["clearance_later"]) is blocks


@pytest.mark.parametrize("title, level", [
    ("Senior Technician", "senior"), ("Sr. Equipment Technician", "senior"), ("Technician II", "mid"),
    ("Technician 2", "mid"), ("Technician III", "senior"), ("Intern", "intern"),
    ("Engineering Intern - Summer 2027", "intern"), ("Lead Technician", "senior"), ("Associate Engineer", ""),
    ("Staff Accountant", ""), ("Staff Nurse - ICU", ""), ("Staff Assistant", ""), ("Staff Engineer", "senior"),
    ("Lead Generation Specialist", ""), ("Lead Abatement Technician", ""), ("Head Start Teacher Assistant", ""),
    ("Head Start Family Advocate", ""), ("Administrative Assistant to the Principal", ""),
    ("Principal's Secretary", ""), ("Executive Assistant to the VP", ""),
    ("Administrative Assistant to the Director", ""), ("Director of HR", "senior"), ("Case Manager", "senior"),
    ("Office Manager", "senior"), ("Assistant Manager", "senior"), ("Manager in Training", "entry"),
    ("Caregiver - Senior Living", ""), ("Senior Center Assistant", ""), ("Senior Services Aide", ""),
    ("Recreation Leader - Senior Center", ""), ("Internal Auditor", ""), ("International Trade Specialist", ""),
    ("Data Entry Clerk", ""), ("Entry Level Technician", "entry"), ("Junior Accountant", "entry"),
    ("Apprentice Electrician", "entry"), ("Graduate Nurse", "entry"), ("Chief Engineer (hotel)", "senior"),
    ("Dir of Sales", "senior"), ("Accountant II", "mid"), ("Accountant I", "entry"), ("HR Generalist II", "mid"),
    ("Level 3 Support Analyst", "senior"), ("Help Desk Analyst Tier 2", "mid"),
    ("Field Service Engineer III", "senior"), ("FSE II", "mid"),
    ("Process Technician IV", "senior"), ("Equipment Tech 1", "entry"), ("Payroll Specialist I/II", "entry"),
    ("Technician I-III", "entry"), ("Lead Teller", "senior"), ("Head Cashier", "senior"), ("Team Leader", ""),
    ("Supervisor, Production", "senior"), ("Clean Room Technician - Swing Shift", ""),
    ("Leadership Development Program Associate", ""), ("Mgr Trainee", "entry"), ("Senior Accountant", "senior"),
    ("Sr Financial Analyst", "senior"), ("Customer Service Representative 2", "mid"), ("Lead Line Cook", "senior"),
    ("Head Custodian", "senior"), ("Principal Engineer", "senior"), ("Cooperative Education (Co-op) Student", "intern"),
    ("Co-Op Engineer", "intern"), ("Student Worker", ""), ("Security Officer - Lead", "senior"),
    ("Bus Driver Trainee", "entry"), ("Executive Chef", ""), ("Staffing Coordinator", ""),
    ("Lead-Free Solder Technician", ""), ("Teller 2", "mid"), ("Clerk II", "mid"), ("Office Assistant III", "senior"),
    ("Custodian II", "mid"), ("Senior Living Community Concierge", ""), ("Nursing Assistant - Senior Care", ""),
])
def test_a_titles_level_is_read_with_care(title, level):
    assert title_rank(title) == level


# ------------------------------------------------------------------ where the job is done (F3)

@pytest.mark.parametrize("location, said", [
    ("Remote - US", ("remote", True)), ("US-Remote", ("remote", True)), ("Phoenix, AZ; Remote", ("remote", False)),
    ("Hybrid", ("hybrid", True)), ("On-site", ("onsite", True)), ("Remote (must live in AZ)", ("remote", True)),
    ("Not remote", ("", False)), ("remote work not available", ("", False)),
    ("Remote Location Eligible: No", ("", False)), ("Work from home: No", ("", False)),
    ("100% on-site", ("onsite", True)), ("Remote (Arizona)", ("remote", True)), ("Remote Options", ("remote", False)),
    ("Remote options (Arizona)", ("remote", False)), ("Phoenix, AZ (Hybrid)", ("hybrid", True)),
    ("Chandler, AZ - Onsite", ("onsite", True)), ("Phoenix, AZ", ("", False)), ("Remote, Phoenix", ("remote", False)),
    ("Non-Remote - Phoenix, AZ", ("", False)), ("No Remote - Tempe", ("", False)), ("Virtual", ("remote", True)),
    ("Virtual Office", ("", False)), ("Virtual Academy - Las Vegas, NV", ("", False)),
    ("Arizona Virtual Academy", ("", False)), ("Las Vegas, NV - Remote eligible", ("remote", False)),
    ("Tempe, AZ (Remote Optional)", ("remote", False)), ("Phoenix, AZ - Hybrid Remote", ("hybrid", True)),
    ("Remote/Hybrid - Phoenix", ("hybrid", False)), ("Phoenix (On-site or Remote)", ("remote", False)),
    ("Remote or Phoenix, AZ", ("remote", False)), ("Remotely Operated Vehicle Facility, Tucson", ("", False)),
    ("Remote Sensing Lab - Tempe", ("", False)), ("Teleworks Bldg, Phoenix", ("", False)),
    ("Onsite - Remote Training", ("onsite", False)), ("Remote Phoenix Office (onsite)", ("onsite", False)),
    ("On-Site Hybrid", ("hybrid", True)), ("Office - Mesa, AZ", ("onsite", True)),
    ("In-Office, Scottsdale", ("onsite", True)), ("Remote; Phoenix, AZ; Hybrid", ("hybrid", False)),
    ("Phoenix, AZ | Remote", ("remote", False)), ("Phoenix, AZ / Remote", ("remote", False)),
    ("Remote, US (excluding CA)", ("remote", True)), ("USA - Remote - AZ", ("remote", True)),
    ("Telecommute", ("remote", True)), ("Home Based - Arizona", ("remote", True)), ("Work from Home", ("remote", True)),
    ("Field-based, Arizona", ("", False)), ("Mobile - Phoenix Metro", ("", False)),
    ("Remote Area Medical - Mesa", ("", False)), ("Arizona - Remote Site (Mine)", ("", False)),
    ("Remote Site, Bagdad AZ", ("", False)), ("Remote Location - Morenci, AZ", ("", False)),
    ("Morenci (Remote)", ("remote", False)), ("Grand Canyon (remote site)", ("", False)),
    ("Flagstaff, AZ (remote area)", ("", False)),
])
def test_a_location_says_where_the_job_is_done_surely_only_alone(location, said):
    assert workplace.location_said(location) == said


@pytest.mark.parametrize("value, said", [
    ("Remote", ("remote", True)), ("Hybrid", ("hybrid", True)), ("On-site", ("onsite", True)),
    ("Onsite", ("onsite", True)), ("On Site", ("onsite", True)), ("In Office", ("onsite", True)),
    ("ORA_ON_SITE", ("onsite", True)), ("ORA_HYBRID", ("hybrid", True)), ("ORA_REMOTE", ("remote", True)),
    ("TELECOMMUTE", ("remote", True)), ("remoteLocal", ("remote", True)), ("Global Remote", ("remote", True)),
    ("Fully Remote", ("remote", True)), ("Remote or On-site", ("remote", False)), ("Flexible", ("", False)),
    ("unspecified", ("", False)), ("Not Remote", ("", False)), ("No", ("", False)), ("Yes", ("", False)),
    ("Remote Eligible", ("remote", False)), ("Remote Optional", ("remote", False)), ("Hybrid Remote", ("hybrid", True)),
    ("Partially Remote", ("hybrid", True)), ("Occasional Remote", ("hybrid", True)),
    ("Remote - Temporary", ("remote", False)), ("Temporarily Remote", ("remote", False)),
    ("Remote with travel", ("remote", True)), ("On-site with occasional remote", ("hybrid", True)),
    ("Field", ("", False)), ("Field Based", ("", False)), ("Mobile", ("", False)), ("Office", ("onsite", True)),
    ("Office Based", ("onsite", True)), ("Virtual", ("remote", True)), ("Work From Home", ("remote", True)),
    ("Not Applicable", ("", False)), ("N/A", ("", False)), ("Remote First", ("remote", True)),
    ("Office/Remote Flexible", ("onsite", False)), ("Onsite Required", ("onsite", True)),
    ("Remote (Arizona Only)", ("remote", True)), ("On-site - Remote training", ("onsite", False)),
    ("Remote-Friendly", ("remote", False)), ("Remote Work Not Available", ("", False)),
    ("Not Eligible for Remote", ("", False)), ("Remote: No", ("", False)),
    ("Remote: Yes", ("remote", True)), ("Flexible Work", ("", False)), ("Flexible Location", ("", False)),
    ("Flex", ("", False)),
])
def test_a_systems_field_says_where_the_job_is_done_and_whether_surely(value, said):
    assert workplace.field_said(value) == said


def _icims(pairs):
    html = ("<div>"
            + "".join(f'<div class="iCIMS_JobHeaderTag"><dt>{a}</dt><dd>{b}</dd></div>' for a, b in pairs) + "</div>")
    return icims_workplace(BeautifulSoup(html, "html.parser"))


@pytest.mark.parametrize("pairs, said", [
    ([("Remote Location Eligible", "No")], ("", False)), ([("Remote Location Eligible", "Yes")], ("remote", False)),
    ([("Location Type", "Onsite")], ("onsite", True)), ([("Location Type", "Remote")], ("remote", True)),
    ([("Location Type", "Onsite"), ("Remote Location Eligible", "Yes")], ("onsite", True)),
    ([("Remote Location Eligible", "Yes"), ("Location Type", "Hybrid")], ("hybrid", True)),
    ([("Remote Location Eligible", "Yes - Arizona only")], ("remote", False)),
    ([("Remote Location Eligible", "Yes, but must report to office 3 days")], ("remote", False)),
    ([("Work Arrangement", "Remote or Onsite")], ("remote", False)),
    ([("Location Type", "Remote Local")], ("remote", True)), ([("Location Type", "Remote/Onsite")], ("remote", False)),
    ([("Workplace Type", "Office")], ("onsite", True)), ([("Location Type", "Field")], ("", False)),
    ([("Remote Type", "Remote Local")], ("remote", True)),
])
def test_icims_details_say_where_the_job_is_done_and_eligible_is_never_sure(pairs, said):
    assert _icims(pairs) == said


def test_smartrecruiters_and_successfactors_say_it_only_as_their_own_words():
    assert [smartrecruiters_workplace(loc) for loc in ({"remote": True}, {"hybrid": True, "remote": True},
                                                       {"remote": False}, {"fullLocation": "Remote"})] == \
        ["remote", "hybrid", "", ""]

    def sf(lines):
        html = "".join(f'<span class="joblayouttoken">{line}</span>' for line in lines)
        return successfactors_workplace(BeautifulSoup(html, "html.parser"))

    assert [sf(lines) for lines in (["Service", "Phoenix AZ", "United States", "On-Site"], ["Remote"], ["Hybrid"],
                                    ["Remote Support Engineer"], ["<span itemprop='title'>Remote</span>"])] == \
        ["onsite", "remote", "hybrid", "", ""]


@pytest.mark.parametrize("text, said", [
    ("This is a fully remote position.", "remote"), ("This is not a remote role.", "onsite"),
    ("No remote work during the 90-day training, then hybrid schedule.", "hybrid"),
    ("Remote diagnostics of tools; this position is on-site.", "onsite"),
    ("Must be on-site. Not a hybrid role.", "onsite"), ("We are not fully remote.", "onsite"),
    ("This role is 100% remote.", "remote"), ("Hybrid work model available after 6 months.", "hybrid"),
    ("Remote work is not available for this position.", "onsite"),
    ("Our hybrid cloud environment", ""), ("hybrid vehicle maintenance experience", ""),
])
def test_a_postings_sentences_say_where_its_done_but_never_surely(text, said):
    assert workplace.in_text(text) == said


# ------------------------------------------------------------------ employment type (F6)

@pytest.mark.parametrize("value, types", [
    ("Full-time / Part-time", ["full_time", "part_time"]), ("Full time", ["full_time"]), ("Part time", ["part_time"]),
    ("Temp to Hire", ["full_time", "temporary"]), ("Temp-to-Hire", ["full_time", "temporary"]),
    ("Contract-to-hire", ["full_time", "contract"]), ("Contract to Hire", ["full_time", "contract"]),
    ("Regular Full Time", ["full_time"]), ("Regular", []), ("PRN", ["per_diem"]), ("Per Diem", ["per_diem"]),
    ("Seasonal", ["temporary"]), ("Intern", ["internship"]), ("Internship", ["internship"]), ("Co-op", ["internship"]),
    ("Co-Op", ["internship"]), ("Fixed term", ["temporary"]), ("Fixed-Term", ["temporary"]),
    ("Limited Term", ["temporary"]), ("Contingent Worker", ["contract"]), ("Employee", []),
    ("Employee, Part time", ["part_time"]), ("FULL_TIME", ["full_time"]), ("PART_TIME", ["part_time"]),
    ("CONTRACTOR", ["contract"]), ("TEMPORARY", ["temporary"]), ("INTERN", ["internship"]), ("VOLUNTEER", []),
    ("PER_DIEM", ["per_diem"]), ("OTHER", []), ("FULL_TIME, TEMPORARY", ["full_time", "temporary"]),
    ("Part Time / PRN", ["part_time", "per_diem"]), ("Full or Part Time", ["full_time", "part_time"]),
    ("Full and Part Time", ["full_time", "part_time"]), ("Fix", []), ("Variable", []), ("Casual", []), ("On Call", []),
    ("On-Call", []), ("As Needed", ["per_diem"]), ("Relief", []), ("Pool", []), ("Float Pool", []),
    ("Full-Time Temporary", ["full_time", "temporary"]), ("Part-Time Seasonal", ["part_time", "temporary"]),
    ("Temporary (Fixed Term)", ["temporary"]), ("Hourly", []), ("Salaried", []), ("Exempt", []), ("Non-Exempt", []),
    ("1099", ["contract"]), ("Freelance", ["contract"]), ("Gig", []), ("Regular Part Time", ["part_time"]),
    ("Part-time (20 hrs)", ["part_time"]), ("Full-Time (FT)", ["full_time"]), ("FT", ["full_time"]),
    ("PT", ["part_time"]), ("Full-Time, Benefits Eligible", ["full_time"]), ("PRN/Per Diem", ["per_diem"]),
    ("Contractor", ["contract"]), ("Contract", ["contract"]), ("Contracted", ["contract"]), ("Temp", ["temporary"]),
    ("Temporary Full-Time", ["full_time", "temporary"]), ("Full Time Fixed Term", ["full_time", "temporary"]),
    ("Permanent", []), ("Permanent Full-Time", ["full_time"]), ("Student", []), ("Student Worker", []),
    ("Work Study", []), ("Apprenticeship", []), ("Graduate Program", []), ("Contract W2", ["contract"]),
    ("W2 Contract", ["contract"]), ("Contract Corp-to-Corp", ["contract"]), ("Direct Hire", []),
    ("Full Time - Direct Hire", ["full_time"]), ("Perm", []), ("Contract-to-Perm", ["full_time", "contract"]),
    ("Interim", []), ("Locum Tenens", []), ("Full-time; Weekend", ["full_time"]), ("PT Weekends", ["part_time"]),
    ("Part-time Benefited", ["part_time"]), ("Full Time (Benefited)", ["full_time"]),
    ("Days, Full-Time", ["full_time"]), ("PRN - As Needed", ["per_diem"]), ("Ft/Pt", ["full_time", "part_time"]),
    ("Internal", []), ("International", []), ("Seasonal/Temporary", ["temporary"]), ("Contingent", ["contract"]),
    ("Non-Contingent", []), ("Not Temporary", []), ("Short-term", []), ("Long term contract", ["contract"]),
    ("Fixed", []), ("Termination-free", []), ("Intermittent", []), ("Part time 0.5 FTE", ["part_time"]),
    ("1.0 FTE", []), ("0.9 FTE", []), ("Full Time Equivalent 0.6", []),
])
def test_a_systems_field_names_its_employment_types(value, types):
    assert employment.types_of(value) == types


@pytest.mark.parametrize("choice, names", [
    ("full_time", ["full_time"]), ("Full-time", ["full_time"]), ("full time", ["full_time"]),
    ("part-time, contract", ["part_time", "contract"]), ("prn", ["per_diem"]), ("temp", ["temporary"]),
    ("fulltime", ["full_time"]), ("per diem", ["per_diem"]), ("intern", ["internship"]), ("co-op", ["internship"]),
    ("seasonal", ["temporary"]), ("contract-to-hire", ["contract"]), ("", []), (" ", []), ("remote", None),
])
def test_the_tools_employment_type_takes_the_types_and_their_common_names(choice, names):
    assert employment.parse_choice(choice) == names


def test_a_type_chip_judges_only_what_the_jobs_field_says():
    """A field says a schedule (full or part time), an arrangement (contract, temporary, internship, per diem), or
    both; a chip pressed judges only what the field says. A Workday internship whose timeType says "Full time" stays
    when Internship is pressed."""
    keeps = employment.keeps
    assert keeps(employment.types_of("Full time"), ["internship"])
    assert keeps(employment.types_of("Full time"), ["temporary"])
    assert keeps(employment.types_of("Employee, Full time"), ["contract"])
    assert not keeps(employment.types_of("Part time"), ["full_time"])
    assert not keeps(employment.types_of("Contract"), ["internship"])
    assert keeps(employment.types_of("Temp to Hire"), ["full_time"]) and keeps(employment.types_of("Temp to Hire"),
                                                                              ["temporary"])
    assert not keeps(employment.types_of("Full-Time Temporary"), ["full_time", "contract"])
    assert keeps(employment.types_of("Regular"), ["full_time"]) and keeps([], ["internship"])


# ------------------------------------------------------------------ sponsorship offered (the design of Oct 18, F7)

# Made-up sentences as postings write them (no fixture says it yet). True: the posting says plainly it won't sponsor
# a work visa; False: it doesn't, says it may, says it does, hedges, asks, or means another sponsorship
SPONSORSHIP = [
 ("We will not sponsor applicants for work visas.", True),
 ("Visa Sponsorship: Employer will not sponsor applicants for employment visa status.", True),
 ("The company is unable to sponsor employment visas for this position.", True),
 ("We are unable to sponsor H-1B visas at this time.", True),
 ("No visa sponsorship is available for this position.", True),
 ("No visa sponsorship.", True),
 ("No sponsorship is available.", True),
 ("Visa sponsorship is not available for this role.", True),
 ("Sponsorship is not available for this position.", True),
 ("Relocation and sponsorship are not available.", True),
 ("This position is not eligible for visa sponsorship.", True),
 ("This position is not eligible for relocation or visa sponsorship.", True),
 ("Candidates must be authorized to work in the U.S. without sponsorship.", True),
 ("Applicants must be legally authorized to work in the United States without the need for employer sponsorship, now "
  "or in the future.", True),
 ("Must be eligible to work in the US without current or future visa sponsorship.", True),
 ("Applicants must not now or in the future require visa sponsorship.", True),
 ("Candidates who now or in the future require sponsorship will not be considered.", True),
 ("Applicants who require visa sponsorship are not eligible for this role.", True),
 ("We do not offer visa sponsorship.", True),
 ("Example Fab does not provide immigration sponsorship for this role.", True),
 ("We are not currently sponsoring visas.", True),
 ("Our company will not be sponsoring H-1B candidates for this opening.", True),
 ("We cannot sponsor candidates for this position.", True),
 ("Note: This role is not eligible for immigration sponsorship.", True),
 ("Please note: we can’t sponsor work visas.", True),
 ("This employer is not in a position to offer sponsorship.", True),
 ("Visa Sponsorship: No", True),
 ("Sponsorship Available: None", True),
 # (the review of F7: its probes' plain ones, and the four it found missed)
 ("Visa Sponsorship Offered? No", True),
 ("This position does not qualify for visa sponsorship.", True),
 ("This opportunity is not open to candidates who require visa sponsorship.", True),
 ("We will not consider candidates who require sponsorship.", True),
 ("Sponsorship for work authorization is not available.", True),
 ("This position is not eligible for visa sponsorship, relocation, or remote work.", True),
 ("This position is not eligible for employment-based immigration sponsorship.", True),
 ("Applicants requiring sponsorship will not be considered.", True),
 ("Must be a U.S. citizen or permanent resident; we do not provide visa sponsorship.", True),
 ("Applicants must be currently authorized to work in the US on a full-time basis; the company will not sponsor "
  "applicants for work visas.", True),
 ("Please note this role is not eligible for sponsorship.", True),
 ("Visa sponsorship is unavailable for this position.", True),
 # (the second review of F7: "requiring", and who need not or should not apply)
 ("Candidates requiring visa sponsorship are not eligible.", True),
 ("Candidates requiring visa sponsorship now or in the future are not eligible for this role.", True),
 ("Applicants requiring sponsorship should not apply.", True),
 ("Candidates who require visa sponsorship need not apply.", True),
 ("We may sponsor qualified candidates.", False),
 ("Visa sponsorship available.", False),
 ("Sponsorship available for the right candidate.", False),
 ("Visa sponsorship is available for this position.", False),
 ("We sponsor H-1B visas.", False),
 ("We will sponsor visas for qualified engineers.", False),
 ("Sponsorship may not be available for all roles.", False),
 ("We may not be able to sponsor visas.", False),
 ("Candidates with or without sponsorship are welcome to apply.", False),
 ("Candidates requiring sponsorship are welcome to apply.", False),
 ("Are you legally authorized to work in the US without sponsorship?", False),
 ("Will you now or in the future require sponsorship?", False),
 ("No, I will not require sponsorship", False),
 ("Sponsorship is considered on a case-by-case basis.", False),
 ("We do not sponsor visas, except for PhD candidates.", False),
 ("Typically we do not sponsor visas.", False),
 ("We do not sponsor new H-1B petitions; transfers will be considered.", False),
 ("We are open to sponsoring H-1B transfers.", False),
 ("Must be authorized to work in the United States.", False),
 ("No sponsorship required.", False),
 ("We do not sponsor.", False),
 ("Visa Sponsorship: Yes", False),
 ("Sponsorship: N/A", False),
 ("Security clearance sponsorship is not available.", False),
 ("We will not sponsor a security clearance for candidates.", False),
 ("No CDL sponsorship; a valid CDL is required.", False),
 ("Tuition sponsorship is not available for part-time staff.", False),
 ("Sponsorship opportunities with local events are not available to interns.", False),
 ("Our sponsorship of community events is not available to employees.", False),
 ("The company does not discriminate and will consider candidates who need sponsorship.", False),
 # (the review of F7: another thing's sponsorship, said in the plural or of relocation; one scoped to other work; a
 # green card, travel, a candidate for office, a bonus, a supervisor's, athletes, clubs, memberships)
 ("Relocation sponsorship is not available.", False),
 ("We will not sponsor relocation for this position.", False),
 ("We cannot sponsor relocation; local candidates only.", False),
 ("We do not sponsor relocation or housing for candidates.", False),
 ("We will not sponsor applicants' relocation costs.", False),
 ("We do not sponsor clearances for candidates.", False),
 ("We are unable to sponsor candidates for clearances at this time.", False),
 ("Sponsorship for security clearances is not available.", False),
 ("We do not sponsor professional licenses for applicants.", False),
 ("We do not sponsor certifications for candidates.", False),
 ("We will not sponsor candidates for licensure.", False),
 ("We do not sponsor individuals for the PE exam.", False),
 ("We do not sponsor applicants for continuing education courses.", False),
 ("We will not sponsor applicants for a commercial driver's license.", False),
 ("Sponsorship is not available for internships; full-time roles are eligible.", False),
 ("We do not sponsor visas for interns, but full-time hires may be sponsored.", False),
 ("We will sponsor visas, but we do not sponsor green cards.", False),
 ("We do not sponsor green cards.", False),
 ("We cannot sponsor candidates' travel to the interview.", False),
 ("We do not sponsor or endorse any political candidates.", False),
 ("Applicants who require sponsorship are not eligible for the sign-on bonus.", False),
 ("Work without sponsorship from a supervisor is not allowed in the cleanroom.", False),
 ("Employees without sponsorship from their manager cannot enroll in tuition reimbursement.", False),
 ("We do not sponsor individual athletes.", False),
 ("We are unable to sponsor student organizations or candidates' clubs.", False),
 ("We do not sponsor professional memberships.", False),
 ("We will not sponsor applicants for membership in professional societies.", False),
 ("We are not currently sponsoring any events.", False),
 ("The employer does not offer sponsorship of family members' visas.", False),
 ("Relocation assistance is not available; visa sponsorship is available.", False),
 ("Clearance Sponsorship: No", False),
 ("Relocation Sponsorship: No", False),
 # (the second review of F7: a need for sponsorship not held against anyone, sponsorship limited to some, a
 # "however", and a track that isn't the job)
 ("Your need for sponsorship will not be considered in hiring decisions.", False),
 ("A candidate's need for visa sponsorship will not be considered a negative factor.", False),
 ("Visa sponsorship is not available to contractors; employees hired directly receive sponsorship.", False),
 ("We cannot sponsor new visas; however, we will consider candidates on existing visas.", False),
 ("We will not consider candidates who require sponsorship any less favorably.", False),
 ("Applicants who need sponsorship are not eligible for the expedited interview track.", False),
 ("Interns are not eligible for sponsorship; full-time employees receive visa sponsorship.", False),
 ("Applicants who will need sponsorship are welcome and will be considered.", False),
]


@pytest.mark.parametrize("text, says", SPONSORSHIP, ids=[str(i) for i in range(len(SPONSORSHIP))])
def test_a_posting_says_it_wont_sponsor_a_visa_only_in_plain_words(text, says):
    full = _WRAP.format(text)
    assert requirements(full)["no_sponsorship"] is says
    assert posting_facts("Technician", requirements(full), full)["no_sponsorship"] is says
    # (and its application's own questions, after the posting, never say it for the posting)
    asked = f"Technician\nWe build tools.\n\n{QUESTIONS_HEADING}\n{text}\n"
    assert requirements(asked)["no_sponsorship"] is False
