import pytest

from job_apply.autofill import (
    choose_option,
    clean_label,
    choose_place,
    is_decline,
    is_empty_value,
    place_words,
    plan_autofill,
    polarity,
    resolve_field,
)
from job_apply.config import Profile


def prof() -> Profile:
    return Profile.load()


def test_choose_option():
    assert choose_option("AZ", ["Alabama", "Arizona", "Arkansas"]) == "Arizona"
    assert choose_option("Arizona", ["AK", "AZ", "CA"]) == "AZ"
    assert choose_option("United States", ["Canada", "United States of America"]) == "United States of America"
    assert choose_option("USA", ["Select...", "Mexico", "United States"]) == "United States"
    assert choose_option("Yes", ["Yes", "No"]) == "Yes"
    assert choose_option("No", ["Yes, I will require sponsorship", "No, I will not require sponsorship"]) == \
        "No, I will not require sponsorship"
    assert choose_option(True, ["I am authorized", "I am not authorized"]) == "I am authorized"
    assert choose_option("Decline to self-identify", ["Male", "Female", "I don't wish to answer"]) == "I don't wish to answer"
    assert choose_option("united states of america (+1)", ["Canada (+1)", "United States of America (+1)"]) == \
        "United States of America (+1)"
    assert choose_option("Mobile", ["Home", "Mobile", "Work"]) == "Mobile"
    assert choose_option("LinkedIn", ["Company Website", "LinkedIn", "Indeed"]) == "LinkedIn"
    assert choose_option("Yes, up to 75%", ["Yes", "No"]) == "Yes"
    assert choose_option("Purple", ["Red", "Blue"]) is None
    assert choose_option("No", ["None of the above", "Some"]) is None
    # picking from a long menu before typing: only the same entry will do
    assert choose_option("AZ", ["Alabama", "Alaska", "Arizona"], exact_only=True) == "Arizona"
    assert choose_option("Bachelor's Degree", ["Associate's Degree", "Master's Degree"], exact_only=True) is None


def f(label, kind="text", **kw):
    return {"id": "1", "label": label, "kind": kind, "value": "", **kw}


def test_resolve_contact_fields():
    p = prof()
    assert resolve_field(f("First Name *"), p).value == "Sam"
    assert resolve_field(f("Legal Last Name"), p).value == "Rivera"
    assert resolve_field(f("Email Address"), p).value == "sam.rivera@example.com"
    assert resolve_field(f("Enter email to start application process"), p).value == "sam.rivera@example.com"  # Qorvo
    assert resolve_field(f("Retype Email Address: *"), p).value == "sam.rivera@example.com"  # SuccessFactors
    assert resolve_field(f("Phone Number"), p).value == "480-555-0123"
    assert resolve_field(f("State", "select", options=["Arizona", "Texas"]), p).value == "Arizona"
    assert resolve_field(f("Country Phone Code", "listbox", options=["Canada (+1)", "United States of America (+1)"]), p).value \
        == "United States of America (+1)"
    assert resolve_field(f("LinkedIn Profile"), p).value.endswith("/samrivera")


def test_resolve_questions():
    p = prof()
    yes_no = ["Yes", "No"]
    assert resolve_field(f("Are you legally authorized to work in the United States?", "radio_group", options=yes_no), p).value == "Yes"
    assert resolve_field(f("Will you now or in the future require sponsorship for employment visa status (e.g., H-1B)?",
                           "radio_group", options=yes_no), p).value == "No"
    assert resolve_field(f("Are you a U.S. person as defined by the ITAR?", "select", options=yes_no), p).value == "Yes"
    assert resolve_field(f("Are you willing to relocate?", "select", options=yes_no), p).value == "No"
    assert resolve_field(f("Are you at least 18 years of age?", "radio_group", options=yes_no), p).value == "Yes"
    assert resolve_field(f("Have you ever worked for Lam Research?", "select", options=yes_no), p, {"company": "Lam Research"}).value == "No"
    assert resolve_field(f("Have you previously been employed by Intel?", "select", options=yes_no), p, {"company": "Intel Corporation"}).value == "Yes"
    assert resolve_field(f("Are you comfortable working in a cleanroom environment?", "radio_group", options=yes_no), p).value == "Yes"
    # the same facts asked the other way round
    assert resolve_field(f("Are you legally authorized to work in the U.S. without employer sponsorship?", "radio_group",
                           options=yes_no), p).value == "Yes"
    assert resolve_field(f("Can you work for us without requiring visa sponsorship now or in the future?", "select",
                           options=yes_no), p).value == "Yes"
    assert resolve_field(f("Are you under the age of 18?", "radio_group", options=yes_no), p).value == "No"
    # a local applicant isn't relocating; more travel than the profile allows is the user's call
    assert resolve_field(f("Do you currently live in the Phoenix area or are you willing to relocate?", "radio_group",
                           options=yes_no), p) is None
    assert resolve_field(f("Are you willing to travel up to 50% of the time?", "radio_group", options=yes_no), p).value == "Yes"
    assert resolve_field(f("This role requires up to 100% travel. Are you willing?", "radio_group", options=yes_no), p) is None
    # "How did you hear" must not be answered with the LinkedIn profile URL
    assert resolve_field(f("How did you hear about us? (LinkedIn, Indeed, etc.)"), p).value == "LinkedIn"


def test_resolve_leaves_unknowns_alone():
    p = prof()
    assert resolve_field(f("Why do you want to work here?", "textarea"), p) is None
    assert resolve_field(f("Can you lift 50 lbs?", "radio_group", options=["Yes", "No"]), p) is None  # answer bank entry empty
    assert resolve_field(f("Desired salary"), p) is None  # not in profile
    assert resolve_field(f("I certify the information is true", "checkbox"), p) is None
    assert resolve_field(f("Password", "password"), p) is None
    # a long question that merely mentions "state" or "name" is not an address field
    assert resolve_field(f("Please state the name of the most complex tool you have repaired and how"), p) is None


def test_resolve_files(job_apply_home, tmp_path):
    p = prof()
    resume = resolve_field(f("Resume/CV", "file"), p)
    assert resume.value.endswith("resume.pdf")
    assert resolve_field(f("Cover Letter", "file"), p, file_inputs_on_page=2) is None
    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "cover_letter.pdf").write_bytes(b"%PDF")
    (folder / "resume.pdf").write_bytes(b"%PDF")
    job = {"folder": str(folder)}
    assert resolve_field(f("Cover Letter", "file"), p, job, 2).value == str(folder / "cover_letter.pdf")
    assert resolve_field(f("Attach", "file"), p, job, 1).value == str(folder / "resume.pdf")  # tailored resume wins
    # a page's one upload in a section for both (Arizona State University's Workday, the full run of Oct 12): the
    # resume, which the profile has, where it had looked for a cover letter and said the file was missing
    both = {**f("Upload a file (5MB max)*", "file"), "section": "Resume/CV and Cover Letter"}
    assert resolve_field(both, p, job, 1).value == str(folder / "resume.pdf")
    assert resolve_field(both, p, job, 2).value == str(folder / "cover_letter.pdf")  # (two there: as before)
    # the review's: a cover letter's section that mentions the resume keeps its cover letter
    for section in ("Cover Letter (and resume if not uploaded)", "Cover letter. Your resume was already imported"):
        assert resolve_field({**both, "section": section}, p, job, 1).value == str(folder / "cover_letter.pdf"), section


def test_plan_autofill_orders_required_first():
    fields = [
        {"id": "1", "kind": "text", "label": "First Name", "value": ""},
        {"id": "2", "kind": "text", "label": "Last Name", "value": "Already"},
        {"id": "3", "kind": "textarea", "label": "Anything else?", "value": ""},
        {"id": "4", "kind": "textarea", "label": "Why us?", "value": "", "required": True},
        {"id": "5", "kind": "select", "label": "Gender", "value": "Select...", "options": ["Select...", "Male", "Female", "Decline"]},
    ]
    plan = plan_autofill(fields, prof())
    assert [x["id"] for x in plan["to_fill"]] == ["1", "5"]
    assert plan["to_fill"][1]["value"] == "Decline"
    assert plan["already_filled"] == ["2"]
    assert [x["id"] for x in plan["needs_input"]] == ["4", "3"]


def test_dates_and_degrees():
    from job_apply.autofill import degree_key, parse_month_year

    assert parse_month_year("2021-03") == ("03", "2021")
    assert parse_month_year("02/2021") == ("02", "2021")
    assert parse_month_year("Jun 2018") == ("06", "2018")
    assert parse_month_year("September 2019") == ("09", "2019")
    assert parse_month_year(2016) == (None, "2016")
    assert parse_month_year("present") == (None, None)
    assert degree_key("B.S. Electrical Engineering") == "bachelor"
    assert degree_key("Master's Degree") == "master"
    assert degree_key("Ph.D.") == "doctorate"
    assert choose_option("BS Electrical Engineering", ["Associate's Degree", "Bachelor's Degree", "Master's Degree"]) == \
        "Bachelor's Degree"
    assert choose_option("Bachelor of Science", ["Bachelor of Arts", "Bachelor of Science", "Master of Science"]) == \
        "Bachelor of Science"


def test_work_and_education_entries():
    p = prof()

    def entry(section, label, kind="text", **kw):
        a = resolve_field({"id": "1", "kind": kind, "label": label, "section": section, "value": "", **kw}, p)
        return None if a is None else a.value

    assert entry("Work Experience 1", "Job Title*") == "Equipment Technician"
    assert entry("Work Experience 2", "Company") == "Example Fab Services"
    assert entry("Work Experience 1", "I currently work here", "checkbox") is True
    assert entry("Work Experience 2", "I currently work here", "checkbox") is False
    assert entry("Work Experience 1", "From", sublabel="Month") == "03"
    assert entry("Work Experience 1", "From", sublabel="Year") == "2021"
    assert entry("Work Experience 1", "To", sublabel="Year") == "__skip__"  # current job has no end date
    assert entry("Work Experience 2", "To", sublabel="Month") == "02"
    assert entry("Work Experience 2", "Start Date", input_type="month") == "2018-06"
    assert entry("Work Experience 2", "End Date") == "02/2021"
    assert entry("Work Experience 1", "Role Description", "textarea").startswith("Maintained")
    assert entry("Work Experience 3", "Job Title") is None  # no third job in the profile
    assert entry("Work Experience 1", "Supervisor Phone") is None  # not the applicant's own phone
    assert entry("Education 1", "School or University") == "Arizona State University"
    assert entry("Education 1", "Degree", "listbox", options=["Associate's Degree", "Bachelor's Degree"]) == "Bachelor's Degree"
    assert entry("Education 1", "Overall Result (GPA)") == "3.4"
    assert entry("Education 1", "To (Actual or Expected)", sublabel="Year") == "2020"


def test_a_jobs_dates_are_read_however_the_profile_writes_them():
    """A profile written from a resume can hold a job's dates as start_date / end_date or as one
    span ("Mar 2019 - Jun 2021"): Workday's From and To boxes stayed empty for those."""
    from job_apply.autofill import entry_dates

    assert entry_dates({"start": "2021-03", "end": "present"}) == ("2021-03", "present")
    assert entry_dates({"start_date": "Mar 2019", "end_date": "Jun 2021"}) == ("Mar 2019", "Jun 2021")
    assert entry_dates({"dates": "Mar 2019 - Jun 2021"}) == ("Mar 2019", "Jun 2021")
    assert entry_dates({"dates": "03/2019 \u2013 Present"}) == ("03/2019", "Present")
    assert entry_dates({"dates": "2019-2021"}) == ("2019", "2021")
    assert entry_dates({"dates": "2019-03 to 2021-06"}) == ("2019-03", "2021-06")
    assert entry_dates({"dates": "Mar 2019-Jun 2021"}) == ("Mar 2019", "Jun 2021")
    assert entry_dates({"dates": "03/2019-06/2021"}) == ("03/2019", "06/2021")
    assert entry_dates({"dates": "2019-03"}) == (None, None)  # one date, not a span
    assert entry_dates({"start_year": 2019, "start_month": 3, "end_year": 2021, "end_month": "Jun"}) == \
        ("2019-03", "Jun 2021")
    assert entry_dates({"title": "Technician"}) == (None, None)
    p = Profile({"work_history": [{"title": "Technician", "company": "Example Fab", "dates": "Mar 2019 - Present"},
                                  {"title": "Assistant", "company": "Example Lab", "start_date": "2016-08",
                                   "end_date": "Feb 2019"}]})

    def entry(section, label, kind="text", **kw):
        a = resolve_field({"id": "1", "kind": kind, "label": label, "section": section, "value": "", **kw}, p)
        return None if a is None else a.value

    assert entry("Work Experience 1", "From", sublabel="Month") == "03"
    assert entry("Work Experience 1", "I currently work here", "checkbox") is True
    assert entry("Work Experience 2", "From", sublabel="Year") == "2016"
    assert entry("Work Experience 2", "To", sublabel="Month") == "02"


def test_jobs_without_their_months_are_named_as_missing_from_the_profile():
    """Workday asks every job's start and end month. The desk says which jobs in the profile
    lack them, before an application asks, so they're added once."""
    from job_apply.autofill import undated_jobs

    p = Profile({"work_history": [
        {"title": "Equipment Technician", "company": "Example Fab", "start": "2021-03", "end": "present"},
        {"title": "Safety Technician", "company": "Example Steel", "dates": "2018 - 2021"},  # years only
        {"title": "Line Cook", "company": "Example Diner"},
        {"title": "Assistant", "company": "Example Lab", "start": "Jun 2016", "end": "Jan 2018"}]})
    assert undated_jobs(p) == ["Safety Technician at Example Steel", "Line Cook at Example Diner"]
    assert p.profile_gaps()[0] == "work_history dates: Safety Technician at Example Steel; Line Cook at Example Diner"
    # a gap, not a requirement: an old job's months can be forgotten, and most sites never ask
    assert not [m for m in p.missing_required() if m.startswith("work_history")]
    assert not [g for g in prof().profile_gaps() if g.startswith("work_history")]  # the test profile's jobs are dated


def test_the_gaps_name_schools_without_a_degree_and_the_background_left_unanswered():
    """Fewer stops: a school with no degree written stopped Workday's Degree box (classes without
    a degree are "Some college (no degree)"), and each unanswered background question stops the
    desk where it's asked. Setup asks them all at once; the desk's notice lists what's left."""
    from job_apply.config import BACKGROUND_KEYS, Profile

    p = Profile({"education_history": [{"school": "Example Community College", "degree": ""},
                                       {"school": "Example High School", "degree": "High School Diploma"}],
                 "background": {"military": False, "board_member": True}})
    gaps = p.profile_gaps()
    assert "education_history degree: Example Community College" in gaps, gaps
    background = next(g for g in gaps if g.startswith("background: "))
    assert "military" not in background and "board_member" not in background and "relatives_at_employer" in background
    answered = Profile({"background": {k: False for k in BACKGROUND_KEYS}})
    assert not [g for g in answered.profile_gaps() if g.startswith("background")]


def test_a_boxes_in_a_jobs_block_that_the_profile_doesnt_hold():
    """"May we contact this employer?" in a job's block was given the company's name; it and
    "Reason for leaving", "Hours per week" and a date's Day are the person's, not the profile's."""
    from job_apply.autofill import entry_of

    p = prof()

    def field(label, **kw):
        return {"id": "1", "kind": "text", "label": label, "section": "Work Experience 1", "value": "", **kw}

    assert resolve_field(field("May we contact this employer?"), p) is None
    for f in (field("May we contact this employer?"), field("Reason for Leaving"), field("Hours per week"),
              field("From", sublabel="Day")):
        assert entry_of(f, p) is None, f["label"]
    assert entry_of(field("From", sublabel="Month"), p) == ("Equipment Technician at Intel", "work")
    assert entry_of({**field("Degree"), "section": "Education 1"}, p) == ("Arizona State University", "education")


def test_plan_skips_end_date_of_current_job():
    fields = [
        {"id": "1", "kind": "text", "label": "To", "sublabel": "Month", "section": "Work Experience 1", "value": ""},
        {"id": "2", "kind": "text", "label": "To", "sublabel": "Month", "section": "Work Experience 2", "value": ""},
    ]
    plan = plan_autofill(fields, prof())
    assert [f["id"] for f in plan["to_fill"]] == ["2"]
    assert plan["needs_input"] == []


def test_unsectioned_education_dates_follow_school_fields():
    fields = [
        {"id": "1", "kind": "combobox", "label": "School*", "value": ""},
        {"id": "2", "kind": "combobox", "label": "Degree*", "value": "", "options": ["Associate's Degree", "Bachelor's Degree"]},
        {"id": "3", "kind": "text", "label": "Start date year*", "value": ""},
        {"id": "4", "kind": "text", "label": "End date year*", "value": ""},
        {"id": "5", "kind": "text", "label": "Start date", "value": "", "section": "Availability"},
    ]
    plan = {f["id"]: f["value"] for f in plan_autofill(fields, prof())["to_fill"]}
    assert plan["1"] == "Arizona State University"
    assert plan["2"] == "Bachelor's Degree"  # "BS Electrical Engineering" in the profile
    assert (plan["3"], plan["4"]) == ("2016", "2020")
    assert "5" not in plan  # a start date in another section is not an education date


def test_a_start_date_after_other_questions_is_not_the_last_jobs():
    """Unsectioned dates right after a job's boxes are that job's; a "Start Date" further down,
    after other questions (when the person can start), took the job's start date."""
    fields = [{"id": "c", "kind": "text", "label": "Company*", "value": ""},
              {"id": "t", "kind": "text", "label": "Job Title*", "value": ""},
              {"id": "l", "kind": "text", "label": "Location", "value": ""},
              {"id": "s", "kind": "text", "label": "Start date", "value": ""},
              {"id": "q", "kind": "radio_group", "label": "Are you 18 years of age or older?", "value": "",
               "options": ["Yes", "No"]},
              {"id": "a", "kind": "text", "label": "Start Date", "value": ""}]
    from job_apply.autofill import _with_context

    sections = {f["id"]: f.get("section") for f in _with_context(fields)}
    assert sections["s"] == "Work Experience 1" and sections["a"] is None, sections
    plan = {f["id"]: f for f in plan_autofill(fields, prof())["to_fill"]}
    assert plan["s"]["rule"].startswith("work_history[1]"), plan["s"]
    assert "a" not in plan or not plan["a"]["rule"].startswith("work_history"), plan.get("a")


def test_a_citizen_holds_no_nonimmigrant_visa():
    """Texas Instruments (live, Oct 2026) asks whether you hold an H, L, E, J or F visa."""
    q = f("U.S. Immigration Form: Do you currently hold an H, L, E, J, or F nonimmigrant visa (examples: H-1B, "
          "H-2B, H-4, L-1, L-2, E-1, E-2, J-1, J-2, F-1, etc.)?", "radio_group", options=["Yes", "No", "Pending with USCIS"])
    citizen = Profile({"work_authorization": {"us_citizen": True, "requires_sponsorship": False}})
    assert resolve_field(q, citizen).value == "No"
    assert resolve_field(q, Profile({"work_authorization": {"requires_sponsorship": False}})) is None  # not known


def test_attestations_need_known_choices():
    p = prof()
    q = "Are you a U.S. person as defined by U.S. export control regulations (EAR)?"
    assert resolve_field(f(q, "combobox"), p) is None  # choices unknown: leave it for a person
    assert resolve_field(f(q, "combobox", options=["Yes", "No"]), p).value == "Yes"
    assert resolve_field(f(q, "combobox", options=["I am a U.S. citizen", "I am a lawful permanent resident", "Other"]), p) is None


def test_export_questions_that_are_not_about_being_a_us_person():
    p = prof()
    yes_no = ["Yes", "No"]
    # Micron, live: answering this from "U.S. person: yes" said Yes, the opposite of the truth
    micron = ("All Micron sites must observe U.S. export control rules that control information that may be provided "
              "to persons from Cuba, Iran, North Korea, and Syria. Are you a citizen of, or do you hold dual citizenship "
              "with any of these countries?")
    assert resolve_field(f(micron, "combobox", options=yes_no), p) is None
    for q in ("Will you require an export license to access our technology?",
              "This role involves export-controlled technology. Can you comply with these requirements?",
              "Do you hold dual citizenship?"):
        assert resolve_field(f(q, "radio_group", options=yes_no), p) is None, q
    # the U.S. person definition spelled out is still the U.S. person question
    listed = "Are you a U.S. citizen, lawful permanent resident, refugee or asylee?"
    assert resolve_field(f(listed, "radio_group", options=yes_no), p).value == "Yes"
    p.data["work_authorization"]["us_citizen"] = True
    assert resolve_field(f("Are you a U.S. citizen?", "radio_group", options=yes_no), p).value == "Yes"
    assert resolve_field(f("Are you a citizen of the United States?", "radio_group", options=yes_no), p).value == "Yes"
    assert resolve_field(f("Are you a citizen of a country other than the United States?", "radio_group",
                           options=yes_no), p) is None
    # a yearly desired salary doesn't answer current or monthly pay questions (Micron, live)
    p.data.setdefault("preferences", {})["desired_salary"] = "$85,000"
    assert resolve_field(f("Expected salary"), p).value == "$85,000"
    for q in ("Current/Last Drawn Monthly Basic Salary:", "Expected Monthly Basic Salary (exclude allowance & overtime)",
              "Please describe your current compensation package."):
        assert resolve_field(f(q, "textarea" if q.startswith("Please") else "text"), p) is None, q
    # the user's own answer still applies
    p.data["answers"] = [{"match": "Cuba, Iran", "answer": "No"}]
    assert resolve_field(f(micron, "combobox", options=yes_no), p).value == "No"


def test_choices_seen_on_live_forms():
    asm_prev = ["I have NEVER been employed by ASM", "I was PREVIOUSLY employed by ASM", "I am CURRENTLY employed by ASM"]
    assert choose_option("No", asm_prev) == "I have NEVER been employed by ASM"
    asm_auth = ["I require sponsorship to work in this country",
                "I am authorized to work in this country for my current employer",
                "I am authorized to work in this country for any employer", "My status to work in this country is unknown"]
    assert choose_option("Yes", asm_auth) is None  # two "authorized" answers: a bare yes can't pick
    assert choose_option("Yes, for any employer", asm_auth) == asm_auth[2]
    assert choose_option("Yes, for any employer", ["Yes", "No"]) == "Yes"
    assert choose_option("Yes, for any employer", ["Yes", "Yes, with restrictions", "No"]) == "Yes"
    assert choose_option("Yes - 75%", ["Yes - 25%", "Yes - 50%", "Yes - 75%", "No"]) == "Yes - 75%"
    # the profile settles it: authorized and needing no sponsorship is "any employer"
    auth_q = f("Are you legally authorized to work in this country?*", "combobox", options=asm_auth)
    p = prof()
    assert resolve_field(auth_q, p).value == asm_auth[2]
    p.data["work_authorization"] = {**p.data["work_authorization"], "requires_sponsorship": True}
    assert resolve_field(auth_q, p) is None  # current employer only, or not yet: the user says
    lam_eeo = ["Male", "Female", "Choose not to disclose"]
    assert choose_option("Decline to self-identify", lam_eeo) == "Choose not to disclose"


def test_country_lists_with_flags_and_dial_codes():
    lam = ["\U0001F1FA\U0001F1F2 (+1) United States Minor Outlying Islands", "\U0001F1FA\U0001F1F8 (+1) United States of America",
           "\U0001F1E8\U0001F1E6 (+1) Canada"]
    assert choose_option("United States", lam) == lam[1]
    assert choose_option("united states of america (+1)", lam) == lam[1]
    assert choose_option("United States", ["United States Minor Outlying Islands", "United States"]) == "United States"


def test_company_website_is_the_employers_own_site():
    """Onto's Workday lists "ONTO Website", not "Company Website", as where you heard of the job."""
    onto = ["Job Alert", "Job Board", "Networking", "ONTO Website", "Social Media"]
    heard = f("How Did You Hear About Us?*", "combobox", options=onto)
    p = prof()
    p.data["preferences"] = {**p.data.get("preferences", {}), "how_did_you_hear": "Company Website"}
    assert resolve_field(heard, p, {"company": "Onto Innovation"}).value == "ONTO Website"
    two = ["Careers Website", "Other Website", "LinkedIn"]
    assert resolve_field(f("How did you hear about us?", "select", options=two), p, {"company": "Example Fab"}) is None
    assert resolve_field(f("How did you hear about us?", "select", options=["KLA Careers Site", "Indeed"]), p,
                         {"company": "KLA"}).value == "KLA Careers Site"
    # ASML's Workday prompt (nightly live check, Oct 11): no website among its sources, but ASML itself
    asml = ["ASML", "Events", "Job Board", "Professional Org", "Social Media"]
    prompt = {**f("How Did You Hear About Us?*", "combobox", options=asml), "search": True}
    assert resolve_field(prompt, p, {"company": "ASML"}).value == "ASML"
    assert resolve_field(prompt, p, {"company": "Intel"}).value != "ASML"
    # a Workday list read late (issue #112, note 27): "Previous Acme Employee" and "Acme Recruiter" name the
    # employer too, but only "Acme website" is its site
    acme = ["Colleges & Universities", "Contractor Conversion", "Employee Referral", "Job Board / Social Network",
            "Magazine", "Military", "Other", "Previous Acme Employee", "Professional Association", "Acme Recruiter",
            "Acme website"]
    assert resolve_field({**prompt, "options": acme}, p, {"company": "Acme"}).value == "Acme website"
    texas = {**prompt, "options": ["Texas", "Events", "Job Board"]}
    assert resolve_field(texas, p, {"company": "Texas Instruments"}).value != "Texas"  # a word of the name isn't it
    p.data["preferences"]["how_did_you_hear"] = "LinkedIn"
    assert resolve_field(heard, p, {"company": "Onto Innovation"}) is None  # not on the list: the person picks
    # unless the list is a Workday search prompt's top level: then the fill searches for it
    assert resolve_field({**heard, "search": True}, p, {"company": "Onto Innovation"}).value == "LinkedIn"


def test_search_prompts_in_work_and_education_blocks_are_searched():
    """Workday's School prompt lists a few schools on opening; the profile's is searched for."""
    field = {"id": "1", "label": "School or University*", "kind": "combobox", "value": "", "section": "Education 1",
             "options": ["Grand Canyon University", "University of Arizona"]}
    assert resolve_field(field, prof()) is None  # not listed: left for the person
    assert resolve_field({**field, "search": True}, prof()).value == "Arizona State University"
    # listed under a longer name: still found by name
    longer = {**field, "options": ["University of Arizona", "Arizona State University - Tempe"]}
    assert resolve_field(longer, prof()).value == "Arizona State University - Tempe"


def test_names_are_matched_by_name_not_shared_words():
    """Shared words made "Arizona State University" pick "University of Arizona"; a school or
    an employer is matched only by its name."""
    asu = "Arizona State University"
    assert choose_option(asu, ["University of Arizona", "Grand Canyon University"]) == "University of Arizona"  # loose
    assert choose_option(asu, ["University of Arizona", "Grand Canyon University"], names=True) is None
    assert choose_option(asu, ["ASU", "Arizona State University"], names=True) == asu
    assert choose_option("Mesa Community College", ["Scottsdale Community College", "Mesa Community College (AZ)"],
                         names=True) == "Mesa Community College (AZ)"
    assert choose_option("Intel Corporation", ["Intel", "Microchip"], names=True) == "Intel"


def test_no_selection_is_nothing_chosen():
    """SuccessFactors' dropdowns read "No Selection" when nothing is picked: the box is empty,
    and the entry is no "No" answer."""
    assert is_empty_value("No Selection")
    assert choose_option("I do not", ["No Selection", "Yes", "No"]) == "No"


def test_a_paged_list_is_searched_only_when_it_shows_a_full_page():
    """SuccessFactors' dropdowns list 100 entries at a time. Qorvo's countries stop at Iran, so
    the profile's country is searched for. Its veteran list is three entries, all there is:
    an answer that isn't among them is left alone rather than typed in."""
    countries = ["No Selection"] + [f"Country {i}" for i in range(99)]
    country = {"id": "1", "label": "Country", "kind": "combobox", "value": "", "paged": True, "options": countries}
    assert resolve_field(country, prof()).value == "United States"
    assert resolve_field({**country, "options": countries[:50]}, prof()) is None
    veteran = {"id": "2", "label": "Pre-Offer : Are you a Protected Veteran?", "kind": "combobox", "value": "",
               "paged": True, "options": ["No Selection", "I am a protected veteran", "I am not a protected veteran"]}
    assert resolve_field(veteran, prof()) is None  # the profile's "I don't wish to answer" isn't offered


def test_a_place_lookup_picks_the_entry_in_the_rest_of_the_address():
    """Oracle's address lookups, as listed live at Texas Instruments: the first Chandler is in
    Texas, and ZIP 85225 covers Chandler Heights and Chandler. The rest of the address tells
    them apart, by whole parts ("Chandler Heights" is not "Chandler")."""
    near = ["Chandler", "85225", "AZ", "Arizona"]
    cities = ["Chandler Heights, Maricopa, AZ", "Chandler, Henderson, TX", "Chandler, Lincoln, OK",
              "Chandler, Maricopa, AZ", "Chandlerville, Cass, IL"]
    assert choose_option("Chandler", cities) == "Chandler, Henderson, TX"  # the plain pick, before
    assert choose_place("Chandler", cities, near) == "Chandler, Maricopa, AZ"
    zips = ["85225, Chandler Heights, Maricopa, AZ", "85225, Chandler, Maricopa, AZ"]
    assert choose_place("85225", zips, near) == "85225, Chandler, Maricopa, AZ"
    streets = ["1234 E SOME RD, MESA, ARIZONA, 85201", "1234 E SOME RD, CHANDLER, ARIZONA, 85225",
               "1234 E SOME RD, CHANDLER, OKLAHOMA, 74834"]
    # a street matches with its words abbreviated as the lookup lists them
    assert choose_place("1234 East Some Road", streets, near) == "1234 E SOME RD, CHANDLER, ARIZONA, 85225"
    assert choose_place("1234 E. Some Rd.", streets, near) == "1234 E SOME RD, CHANDLER, ARIZONA, 85225"
    assert choose_place("1 Test Way", ["1 TEST RD, RICHMOND, INDIANA"], near) is None  # no entry starts with it
    assert choose_place("Arizona", ["Arizona", "Arkansas"], near) == "Arizona"


def test_a_zip_extension_box_is_not_given_the_zip():
    """onsemi's Oracle form, live: "Zip Code+4" was given the 5-digit ZIP."""
    p = prof()
    assert resolve_field(f("Zip Code *", "combobox"), p).value == p.get("personal.address.postal_code")
    for label in ("Zip Code+4", "ZIP + 4", "Zip plus 4", "Zip Code Extension"):
        assert resolve_field(f(label), p) is None, label


def test_place_words_are_the_rest_of_the_address(job_apply_home):
    p = prof()
    words = place_words("city", p)
    assert "AZ" in words and "Arizona" in words and str(p.get("personal.address.postal_code")) in words
    assert place_words("phone", p) == []  # not an address part


def test_a_box_that_imports_the_resume_is_left_beside_the_one_that_attaches_it():
    """TI's and onsemi's Oracle ask for the resume twice: "Import your profile from resume",
    which reads it to fill the form in, and "Upload Resume". Only the attachment gets it: the
    site's own reading would refill the form while the desk fills it."""
    fields = [{"id": "1", "kind": "file", "label": "Import your profile from resume"},
              {"id": "2", "kind": "file", "label": "Upload Resume", "required": True},
              {"id": "3", "kind": "file", "label": "Upload Cover Letter"}]
    plan = plan_autofill(fields, prof())
    assert [f["id"] for f in plan["to_fill"]] == ["2"]
    assert [f["id"] for f in plan["needs_input"]] == ["3"]  # the profile has no cover letter
    # the only file box on a page gets the resume, whatever it's called
    alone = plan_autofill([{"id": "1", "kind": "file", "label": "Import your profile from resume"}], prof())
    assert [f["id"] for f in alone["to_fill"]] == ["1"]


def test_a_signed_forms_date_is_today_in_the_boxes_it_asks_for(job_apply_home):
    """Workday's disability self-identification (CC-305) is signed with a Name and a Date in
    Month / Day / Year boxes."""
    from datetime import date

    today, p = date.today(), prof()
    assert resolve_field(f("Date*", sublabel="Month", role="spinbutton"), p).value == f"{today.month:02d}"
    assert resolve_field(f("Date*", sublabel="Day", role="spinbutton"), p).value == f"{today.day:02d}"
    assert resolve_field(f("Date*", sublabel="Year", role="spinbutton"), p).value == str(today.year)
    assert resolve_field(f("Date"), p).value == today.strftime("%m/%d/%Y")
    assert resolve_field(f("Today's Date", input_type="date"), p).value == today.isoformat()
    signed = resolve_field(f("Signature Date"), p)
    assert signed.rule == "signed_date" and signed.value == today.strftime("%m/%d/%Y")  # not the name
    assert resolve_field(f("Signature"), p).value == "Sam Rivera"
    assert resolve_field(f("Date of Birth"), p) is None
    assert resolve_field(f("Date available to start"), p) is None


def test_a_question_whose_choices_say_what_it_asks(job_apply_home):
    """Workday's disability form asks "Please check one of the boxes below:"; its choices
    are about a disability."""
    import yaml

    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text())
    data["eeo"]["disability"] = "I don't wish to answer"
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data))
    boxes = ["Yes, I have a disability, or have had one in the past",
             "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
    ans = resolve_field(f("Please check one of the boxes below:*", "checkbox_group", options=boxes), prof())
    assert (ans.rule, ans.value) == ("disability", "I do not want to answer")
    # one choice that mentions it is not enough to tell
    assert resolve_field(f("Please choose one", "radio_group", options=["Yes", "No, no disability"]), prof()) is None
    # a question that says what it asks is never answered from its choices: an accommodation
    # isn't the EEO disability answer, nor is "military member" the protected-veteran one
    data["eeo"]["veteran"] = "I am not a protected veteran"
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data))
    accommodation = ["Yes, I need an accommodation due to a disability", "No, I do not need an accommodation for a disability"]
    assert resolve_field(f("Do you require an accommodation to interview?", "radio_group", options=accommodation),
                         prof()) is None
    military = ["Yes, I am a veteran", "No, I am not a veteran", "I am currently serving"]
    assert resolve_field(f("Are you a current or former member of the U.S. military?", "radio_group",
                           options=military), prof()) is None
    # nor is a box with no question of its own: the page may show it elsewhere
    for label in ("", "Select an option", "Please select"):
        assert resolve_field(f(label, "radio_group", options=accommodation), prof()) is None, label
    # and with no answer in the profile, the person is asked
    del data["eeo"]["disability"]
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data))
    assert resolve_field(f("Please check one of the boxes below:*", "checkbox_group", options=boxes), prof()) is None


def test_choices_by_whole_words_and_by_number():
    """"male" is in "female": a self-identification answer took the wrong one. A number
    among ranges ("10" years, a 3.8 GPA) took a range that doesn't hold it."""
    assert choose_option("Female", ["Male", "Woman", "Non-binary", "Decline to self-identify"]) == "Woman"
    assert choose_option("Woman", ["Man", "Female", "Decline"]) == "Female"
    assert choose_option("No", ["Not applicable", "No", "Yes"]) == "No"
    assert choose_option("10", ["Less than 1 year", "1-3", "More than 3 years"]) == "More than 3 years"
    assert choose_option("2", ["Less than 1 year", "1-3", "More than 3 years"]) == "1-3"
    assert choose_option("3.8", ["1.99 or less", "2.00 - 2.99", "3.00 - 3.49", "3.50 - 4.00 or higher"]) == \
        "3.50 - 4.00 or higher"  # Texas Instruments' GPA question
    assert choose_option("3.8", ["Below 3.0", "3.0 - 3.49", "3.5 and above"]) == "3.5 and above"
    # a bare "Yes" among several kinds of yes: the person says which
    assert choose_option("Yes", ["Yes, for any employer", "Yes, for my current employer only", "No"]) is None


def test_questions_that_only_look_like_profile_questions():
    """Each of these took a profile answer that said something untrue."""
    p = Profile({"work_authorization": {"authorized_to_work": True, "requires_sponsorship": False, "citizenship": "Mexico"},
                 "personal": {"address": {"country": "United States"}, "first_name": "Sam", "last_name": "Rivera",
                              "phone": "480-555-0100"},
                 "preferences": {"willing_to_relocate": True},
                 "eeo": {"disability": "No, I do not have a disability"},
                 "history": {"previous_employers": []},
                 "work_history": [{"company": "Intel", "title": "Technician", "start": "2020-01", "end": "2022-01"}],
                 "education": {"highest_degree": "High School Diploma"},
                 "education_history": [{"school": "Mesa Community College", "degree": "", "end": 2019}]})

    def answer(label, job=None, **kw):
        a = resolve_field({"id": "1", "label": label, "kind": "text", "value": "", **kw}, p, job or {"company": "Acme"})
        return a and a.value

    # Yes means no sponsorship is needed (it read "sponsor" and answered "requires: no")
    assert answer("Are you legally authorized to work in the US and do not require sponsorship?") == "Yes"
    assert answer("Will you now or in the future require sponsorship?") == "No"
    # about this employer only, from every employer in the profile, by whole names
    assert answer("Have you ever worked in a cleanroom environment?") is None
    assert answer("Have you previously worked for Intel?", {"company": "Intel"}) == "Yes, previously"  # in work_history
    assert answer("Have you ever worked for us before?", {"company": "Intelligent Systems"}) == "No"
    # a school not finished has no graduation year or date (it would say the person graduated)
    assert answer("Graduation Year") is None
    assert answer("Graduation Date", section="Education 1") is None
    # self-identification, not whether the person can do the job
    assert answer("Can you perform the essential functions of this job with or without accommodation for a disability?") is None
    assert answer("Please indicate whether you have a disability") == "No, I do not have a disability"
    # relocation money isn't moving
    assert answer("Will you require relocation assistance?") is None
    assert answer("Are you willing to relocate?") == "Yes"
    # the address's country isn't the citizenship
    assert answer("Country of citizenship") == "Mexico"
    assert answer("Country of Birth") is None
    assert answer("Country/Region") == "United States"
    assert answer("Are you over the age of 21?") is None
    # someone else's details
    assert answer("Phone", section="Emergency Contact") is None
    assert answer("Name", section="References") is None
    assert answer("Employer Phone", section="Work Experience 1") is None


def _person(**extra) -> Profile:
    """Made up: authorized to work in the US, no sponsorship, US citizen, over 18, some
    college classes and no degree, a former ASM technician now at Example Fab."""
    data = {
        "personal": {"first_name": "Sam", "last_name": "Rivera", "phone": "480-555-0100", "phone_country_code": "+1",
                     "address": {"city": "Mesa", "state": "AZ", "postal_code": "85201", "country": "United States"}},
        "work_authorization": {"authorized_to_work": True, "requires_sponsorship": False, "us_citizen": True,
                               "us_person": True, "over_18": True},
        "education": {"highest_degree": "High School Diploma"},
        "education_history": [{"school": "Mesa Community College", "degree": "", "major": "Electronics coursework",
                               "start": 2017, "end": 2019}],
        "work_history": [{"company": "Example Fab", "title": "Technician", "start": "2022-06", "end": "present"},
                         {"company": "ASM", "title": "Field Service Technician", "start": "2019-01", "end": "2022-05"}],
        "preferences": {"willing_to_travel": "Yes, up to 25%"},
    }
    data.update(extra)
    return Profile(data)


def _answer(label, p=None, kind="text", job=None, **kw):
    a = resolve_field({"id": "1", "label": label, "kind": kind, "value": "", **kw}, p or _person(), job or {"company": "Acme"})
    return a and a.value


def test_work_authorization_and_sponsorship_the_right_way_round():
    """Not needing sponsorship, however it's worded, is Yes to the question; an employer's
    note that it won't sponsor isn't the question; a question about another country, or
    with two halves a Yes can't answer both of, is left for the person."""
    for q in ("Do you have unrestricted authorization to work in the U.S. (no sponsorship required)?",
              "Are you authorized to work in the US and not in need of visa sponsorship?",
              "Can you work in the US for any employer? Sponsorship is not required for you?",
              "Are you able to work in the U.S. with no sponsorship needed?",
              "Is your U.S. work authorization free of any sponsorship requirement?",
              "Are you legally authorized to work in the United States? Note: this position is not eligible for visa sponsorship.",
              "Are you authorized to work in the U.S.? (We are unable to sponsor visas for this role.)"):
        assert str(_answer(q)).startswith("Yes"), q  # ("Yes, for any employer" in a text box)
    assert _answer("Please answer yes or no: will you require sponsorship?") == "No"
    assert _answer("Visa sponsorship required") == "No"
    for q in ("We are unable to sponsor employment visas for this position. Do you understand and acknowledge this?",
              "Are you legally authorized to work in Canada?", "Are you eligible to work in the Netherlands?",
              "Will you require sponsorship to work in the United Kingdom?",
              "Are you authorized to work in the US and will you require sponsorship?",
              "Are you a citizen or permanent resident of any country other than the United States?"):
        assert _answer(q) is None, q


def test_age_asked_either_way_and_never_for_another_question():
    assert _answer("Are you less than 18 years of age?") == "No"
    assert _answer("Are you below 18 years of age?") == "No"
    assert _answer("Are you at least 18 years of age?") == "Yes"
    not_allowed = _person(work_authorization={"authorized_to_work": False, "requires_sponsorship": True, "over_18": True})
    assert _answer("Are you at least 18 years of age and legally authorized to work in the United States?", not_allowed) is None
    assert _answer("Are you 18 years of age or older and able to work in the U.S. without sponsorship?", not_allowed) is None


def test_schooling_is_never_understated_nor_overstated():
    """A high school diploma isn't "Some High School"; schooling not finished is never a
    degree, nor gets a graduation date; a school is picked by its name."""
    for ladder in (["Some High School", "High School Graduate", "Some College", "Associate's Degree", "Bachelor's Degree"],
                   ["Less than High School", "Some High School", "High School/GED", "Some College", "Associates", "Bachelors"]):
        assert _answer("Highest level of education", kind="select", options=ladder) in ("High School Graduate", "High School/GED")
    assert choose_option("Associate's degree (in progress)", ["Some College", "Associate's Degree", "Bachelor's Degree"]) \
        in (None, "Some College")
    assert choose_option("Electrical Engineering coursework toward BSEE", ["High School", "Bachelor's Degree"]) is None
    for written in ("Not completed", "None", "No degree", "Some college (no degree)"):
        p = _person(education={}, education_history=[{"school": "Mesa Community College", "degree": written, "end": 2019}])
        assert _answer("Graduation Year", p) is None, written
        assert _answer("Graduation Date", p, section="Education 1") is None, written
    asu = _person(education={"school": "Arizona State University"})
    assert _answer("School", asu, kind="combobox", options=["University of Arizona", "Northern Arizona University"]) is None


def test_a_schools_block_without_numbers_is_one_school():
    """Greenhouse's School / Degree / Discipline / dates with no section: all from the same
    school. A school not finished leaves Degree for the person (it used to get the highest
    degree from elsewhere: "High School", or a Bachelor's at the community college)."""
    fields = [{"id": "s", "label": "School*", "kind": "text", "value": ""},
              {"id": "d", "label": "Degree*", "kind": "select", "value": "",
               "options": ["High School", "Associate's Degree", "Bachelor's Degree"]},
              {"id": "m", "label": "Discipline", "kind": "text", "value": ""},
              {"id": "y", "label": "Start date year", "kind": "text", "value": ""}]
    plan = plan_autofill(fields, _person(education={"highest_degree": "Bachelor's Degree"}), {"company": "Acme"})
    filled = {f["id"]: f["value"] for f in plan["to_fill"]}
    assert filled == {"s": "Mesa Community College", "m": "Electronics coursework", "y": "2017"}, filled
    assert [f["id"] for f in plan["needs_input"]] == ["d"]


def test_a_graduation_year_beside_a_school_is_that_schools():
    """A form asking a School and a Graduation Year got the school from the education history (a
    community college the person didn't finish) and the year from education.graduation_year (their
    high school diploma's): a graduation from the college that never happened. Whatever the
    layout, the year follows the school given: that school's own year when it was finished, else
    the person's to give. On its own, or naming another credential, it's the profile's year."""
    p = _person(education={"highest_degree": "High School Diploma", "graduation_year": 2015})
    acme = {"company": "Acme"}

    def plan(*fields, person=p):
        out = plan_autofill(list(fields), person, acme)
        return {f["id"]: f["value"] for f in out["to_fill"]}, [f["id"] for f in out["needs_input"]]

    def box(id_, label, **kw):
        return {"id": id_, "label": label, "kind": "text", "value": "", **kw}

    school = box("s", "School name")
    for label in ("Graduation year", "Graduation Year (YYYY)*", "Graduation date (mm/yyyy)", "Year of graduation"):
        year = box("y", label)
        for fields in ((school, year), (year, school)):
            assert plan(*fields) == ({"s": "Mesa Community College"}, ["y"]), (label, fields[0]["id"])
    for section in ("Education", "Education 1"):  # an unnumbered heading, and a numbered school with a loose year
        filled, asked = plan(box("s", "School name", section=section), box("y", "Graduation year",
                                                                           section="Education" if section == "Education" else None))
        assert (filled, asked) == ({"s": "Mesa Community College"}, ["y"]), section
    # on its own, or about another credential, the year is the profile's
    assert plan(box("y", "Graduation year")) == ({"y": 2015}, [])
    assert plan(school, box("y", "High school graduation year"))[0]["y"] == 2015
    # a school the person finished gives its own year, not another credential's
    graduate = _person(education={"highest_degree": "Bachelor's Degree", "graduation_year": 2015},
                       education_history=[{"school": "Arizona State University", "degree": "Bachelor's Degree",
                                           "start": 2016, "end": 2020}])
    assert plan(school, box("y", "Graduation year"), person=graduate) == ({"s": "Arizona State University", "y": "2020"}, [])
    # with no school in the profile, the School is the person's and the year stays the profile's
    ged = _person(education={"highest_degree": "GED", "graduation_year": 2017}, education_history=[])
    assert plan(school, box("y", "Graduation year"), person=ged) == ({"y": 2017}, ["s"])
    # an expected graduation date has its own rule, school or not
    expected = box("e", "Expected graduation date")
    assert plan(school, expected)[0].get("e") == plan(expected)[0].get("e")
    # a licence's issuing institution isn't a school
    licence = box("l", "Institution that issued your license")
    assert plan(licence, box("y", "Graduation year")) == ({"y": 2015}, ["l"])


def test_worked_here_before_says_when_and_only_about_working_there():
    asm = ["I am CURRENTLY employed by ASM", "I was PREVIOUSLY employed by ASM", "I have NEVER been employed by ASM"]
    assert _answer("Have you ever been employed with ASM before?", kind="radio_group", options=asm, job={"company": "ASM"}) \
        == "I was PREVIOUSLY employed by ASM"
    assert _answer("Have you ever been employed with Example Fab before?", kind="radio_group", job={"company": "Example Fab"},
                   options=[o.replace("ASM", "Example Fab") for o in asm]) == "I am CURRENTLY employed by Example Fab"
    assert _answer("Have you ever worked on Lam Research etch tools?", job={"company": "Lam Research"}) is None
    assert _answer("Have you previously worked with KLA metrology systems?", job={"company": "KLA"}) is None
    one = _person(history={"previous_employers": "Northwind Semi"})
    assert _answer("Have you previously worked for Northwind Semi?", one, kind="radio_group", options=["Yes", "No"],
                   job={"company": "Northwind Semi"}) == "Yes"


def test_an_answer_given_for_one_employer_isnt_given_to_another(job_apply_home):
    from job_apply import config

    for label, value in (("Cover Letter", "Dear Northwind Semi team, I have wanted to work at Northwind since ..."),
                         ("What excites you about this opportunity?", "The new 300mm fab ..."),
                         ("What do you know about our products?", "Deposition tools ..."),
                         ("Have you interviewed with us before?", "Yes")):
        config.save_answer(label, value, "Northwind Semi")
    p = config.Profile.load()
    for label in ("Cover Letter", "What excites you about this opportunity?", "What do you know about our products?",
                  "Have you interviewed with us before?"):
        assert _answer(label, p, kind="textarea", job={"company": "Contoso Devices"}) is None, label
    assert _answer("What do you know about our products?", p, kind="textarea", job={"company": "Northwind Semi"}) \
        == "Deposition tools ..."


def test_a_question_remembered_from_a_long_notice_is_the_same_question_in_another_employers_wording(job_apply_home):
    """Workday's "Use of Your Profile" notices (issue #112, note 29) are paragraphs worded for each employer, with one
    question in them. An answer remembered from one was filled only where the whole paragraph was the same; the
    question in it is what's remembered, found as a whole sentence in the next employer's paragraph."""
    from job_apply import config
    from job_apply.autofill import _answer_bank

    def remembered(label, company):
        a = _answer_bank(config.Profile.load(), label, {"company": company})
        return a and a.value

    ask = "Do you want your profile kept in the talent community for future openings?"
    acme = (f"Use of Your Profile: Acme keeps the details in your profile so its recruiters can look at them for other "
            f"openings, and you can ask for them to be removed at any time. {ask} Read our Privacy Statement.*")
    example = (f"Talent Community: Example Fab holds the information in your profile for other positions, and its "
               f"recruiters may look at it later; to see how, read our Privacy Statement. {ask} (required)")
    alerts = acme.replace(ask, "Do you want job alerts by email?")
    entry = config.save_answer(acme, "Yes", "Acme")
    assert entry["core"] == ask and entry["question"] == acme
    assert remembered(acme, "Acme") == "Yes"  # the same wording, as before
    # (the review's: a Yes to a talent community, a consent or terms never goes to another employer's by its question)
    assert remembered(example, "Example Fab") is None
    config.save_answer(acme, "No", "Acme")
    assert remembered(example, "Example Fab") == "No"  # the same question, in another's: a No is kept
    assert remembered(alerts, "Example Fab") is None  # another question in the same notice
    # a question that means something only with its notice is never matched by itself
    terms = ("Arbitration: Example Fab and you agree to settle any dispute by binding arbitration, as the agreement "
             "on this page sets out, instead of in court. Do you agree to the terms described above?")
    config.save_answer(terms.replace("Arbitration: Example Fab and you agree to settle any dispute by binding "
                                     "arbitration", "Privacy: Acme keeps your details safe and deletes them on "
                                     "request"), "No", "Acme")
    assert remembered(terms, "Example Fab") is None
    assert remembered(f"{example} Do you want job alerts by email?", "Example Fab") is None  # and the same, with it
    # a short question has no core: it is the whole label or nothing ("Are you currently employed?" isn't "... by ASML?")
    assert "core" not in config.save_answer("Are you currently employed?", "Yes", "Acme")
    assert remembered("Are you currently employed?", "Acme") == "Yes"
    assert remembered("Are you currently employed by ASML? Please say.", "Acme") is None
    # a question about the employer is that employer's, read from the sentence that asks, not the notice around it
    mine = ("Our Hiring Process: Acme looks at every application it gets, and its recruiters talk to some of the people "
            "who apply, so you may hear from them by phone or by email in the weeks after you apply. "
            "Would you take other roles with this company?")
    config.save_answer(mine, "Yes", "Acme")
    assert remembered(mine, "Acme") == "Yes"
    assert remembered(mine.replace("Acme", "Example Fab"), "Example Fab") is None
    # two questions in a notice are told by neither: the whole, or nothing
    entry = config.save_answer(f"{acme} Do you want a copy of it?", "No", "Acme")
    assert "core" not in entry
    assert remembered(f"{acme} Do you want a copy of it?", "Acme") == "No"
    assert remembered(f"{example} Do you want a copy of it?", "Example Fab") is None


def test_question_core_is_the_one_question_a_long_label_asks():
    from job_apply.config import question_core

    ask = "Do you want your profile kept in the talent community for future openings?"
    notice = "Your details are kept so recruiters can look at them for other openings, and you can have them removed."
    assert question_core("Are you willing to relocate?*") == "Are you willing to relocate?"
    assert question_core(f"{notice} {ask} (required)") == ask
    assert question_core(f"{notice} Do you agree?") == f"{notice} Do you agree?"  # too short to say what to agree to
    assert question_core(f"{notice} {ask} {ask}") == f"{notice} {ask} {ask}"  # two sentences ask
    assert question_core(f"{notice} Read it all.") == f"{notice} Read it all."  # none asks
    assert question_core("Please " + "very " * 26 + "kindly say whether you could start soon?") == \
        "Please " + "very " * 26 + "kindly say whether you could start soon?"  # one too long to be told apart


def test_what_names_the_employer_isnt_remembered_as_it_was_said():
    """An answer given to one employer's form ("Acme website") isn't the answer at another's: how the person heard of
    the job is kept as "Company Website" (each form's list words it as its own employer's site), anything else naming
    the employer isn't kept (issue #112, note 27)."""
    from job_apply.autofill import answer_to_remember

    how = "How Did You Hear About Us?*"
    assert answer_to_remember(how, "Acme website", "Acme Technologies Inc") == "Company Website"
    assert answer_to_remember(how, "The Acme careers site", "The Acme Company") == "Company Website"
    assert answer_to_remember(how, "LinkedIn", "Acme") == "LinkedIn"
    assert answer_to_remember(how, "Acme Recruiter", "Acme") is None  # names the employer, and isn't its site
    assert answer_to_remember("Where did you hear about us?", "Example website", "Acme") == "Example website"
    assert answer_to_remember("What would you like to do at Acme?", "Acme website", "Acme") is None
    assert answer_to_remember("Preferred shift", "Nights", "Acme") == "Nights"
    assert answer_to_remember(how, "Acme website", "") == "Acme website"
    # (the review's) an employer whose first word is an everyday or a place's: only its whole name names it
    assert answer_to_remember("Where do you live?", "Phoenix, Arizona", "Phoenix Children's") == "Phoenix, Arizona"
    assert answer_to_remember("Have you applied before?", "No, never applied", "Applied Materials") == "No, never applied"
    assert answer_to_remember(how, "Applied Materials website", "Applied Materials") == "Company Website"
    # remembered so, it's the employer's own website wherever it's asked: the list's "<employer> website"
    p = _person(preferences={"how_did_you_hear": "LinkedIn"})
    p.data["answers"] = [{"match": "how did you hear about us", "answer": "Company Website", "question": how,
                          "from": "Acme"}]
    lists = ["Advertisement", "Example Fab website", "Job Board"]
    assert _answer("How Did You Hear About Us?*", p, "combobox", {"company": "Example Fab"}, options=lists) == \
        "Example Fab website"
    assert _answer("How Did You Hear About Us?*", p, "combobox", {"company": "Example Fab"}, options=["Job Board"]) is None
    # (the review's) a lone site that's another source's is never the employer's own
    for other in ("Indeed Website", "University Career Site", "Other Website"):
        assert _answer("How Did You Hear About Us?*", p, "combobox", {"company": "Example Fab"},
                       options=["Advertisement", other]) is None, other
    assert _answer("How Did You Hear About Us?*", p, "combobox", {"company": "Example Fab"},
                   options=["Advertisement", "Careers Site"]) == "Careers Site"


def test_travel_willingness_answers_only_how_much_travel():
    assert _answer("Are you willing to travel up to 50 percent of the time?") is None
    assert _answer("This role travels 75 percent. Are you willing to travel?") is None
    assert _answer("Are you willing to travel up to 20% of the time?") == "Yes, up to 25%"
    for q in ("Do you have any travel restrictions?", "Do you have a valid U.S. passport for international travel?",
              "Is there anything that would prevent you from traveling?"):
        assert _answer(q) is None, q


def test_placeholder_text_is_no_answer():
    for shown in ("Please Select...", "Select One...", "Choose...", "Select a State", "Select Country", "Make a Selection"):
        assert is_empty_value(shown), shown
    assert not is_empty_value("Selection Committee") and not is_empty_value("Arizona")
    field = {"id": "q", "label": "Will you now or in the future require sponsorship?", "kind": "select",
             "value": "Please Select...", "required": True, "options": ["Please Select...", "Yes", "No"]}
    assert plan_autofill([field], _person())["to_fill"][0]["value"] == "No"


def test_icims_phone_country_code_and_number_are_answered_from_the_profiles_phone():
    """Charles Schwab's iCIMS sign-in (live, Oct 10): its Phone Country Code shows "— Make a
    Selection —" (long dashes), which was read as an answer, so it was never picked; its Number
    says no more than that, and its autocomplete (tel-national) says it's the phone's. Both come
    from the profile's phone, the number without its dial code. With no phone in the profile, the
    Number has no answer; nor has a bare "Number" the site doesn't mark as a phone's."""
    for shown in ("— Make a Selection —", "– Select –", "—"):
        assert is_empty_value(shown), shown
    codes = ["— Make a Selection —", "(+1) Canada", "(+1) United States Minor Outlying Islands", "(+1) United States",
             "(+44) United Kingdom"]
    code = f("Country Code — Make a Selection —", "listbox", value="— Make a Selection —", options=codes, required=True)
    number = f("Number", autocomplete="tel-national")
    filled = {x["label"]: x["value"] for x in plan_autofill([code, {**number, "id": "2"}], _person())["to_fill"]}
    assert filled == {"Country Code — Make a Selection —": "(+1) United States", "Number": "480-555-0100"}, filled
    written_with_code = _person(personal={"first_name": "Sam", "phone": "+1 480-555-0100", "phone_country_code": "+1"})
    assert resolve_field(number, written_with_code).value == "4805550100"
    assert resolve_field(f("Number", autocomplete="tel"), written_with_code).value == "+1 480-555-0100"
    assert resolve_field(number, _person(personal={"first_name": "Sam"})) is None
    assert resolve_field(f("Number"), _person()) is None
    assert resolve_field({**number, "section": "Emergency Contact"}, _person()) is None


def test_a_phone_written_with_its_code_or_a_code_read_as_a_number():
    """A profile's number written with its dial code ("+1 480-555-0100", "1-480-555-0100"), or a
    dial code YAML read as a number (phone_country_code: +1 is 1), still goes in as asked."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())

    def with_phone(phone, code=None):
        personal = {**data["personal"], "phone": phone}
        if code is not None:
            personal["phone_country_code"] = code
        path.write_text(yaml.safe_dump({**data, "personal": personal}))
        return Profile.load()

    emd = 'Phone Number [must include your location code using the "+", (e.g. +1##########), with no spaces or dashes]*'
    assert _answer(emd, with_phone("480-555-0100", 1)) == "+14805550100"
    plus = with_phone("+1 480-555-0100")
    assert _answer("Phone number, digits only, no spaces or dashes", plus) == "4805550100"
    assert _answer(emd, plus) == "+14805550100"
    assert _answer("Phone Number (do not include country code)", plus) == "4805550100"
    one = with_phone("1-480-555-0100")
    assert _answer("Phone Number (including country code)", one) == "+1 480-555-0100"
    assert _answer(emd, one) == "+14805550100"


def test_a_sign_ups_boxes_are_never_filled_nor_asked():
    """Boxes the form reader marks aside (a talent community's email and consent beside the
    application) are neither filled nor put to the person."""
    p = _person()
    plan = plan_autofill([{"id": "e", "label": "Email", "kind": "text", "value": "", "aside": True},
                          {"id": "c", "label": "I agree to join the Talent Community", "kind": "checkbox", "value": False,
                           "required": True, "aside": True},
                          {"id": "f", "label": "First Name", "kind": "text", "value": ""}], p, {"company": "Acme"})
    assert [f["id"] for f in plan["to_fill"]] == ["f"] and plan["needs_input"] == []


def test_boxes_for_someone_elses_details_or_another_date_or_document():
    p = _person()
    for label, section in (("Employer Phone Number", None), ("Company Phone", None), ("School Zip Code", None),
                           ("First Name", "Relative Information"), ("Name", "High School"),
                           ("Phone", "Most Recent Employer"), ("City", "Most Recent Employer")):
        assert _answer(label, p, section=section) is None, (label, section)
    for label in ("State ID Number", "State license number", "Statement of accuracy"):
        assert _answer(label, p) is None, label
    for section in ("Criminal Conviction Details", "Military Service", "Availability"):
        assert _answer("Date", p, section=section) is None, section
    assert _answer("Phone Number (including country code)", p) == "+1 480-555-0100"
    assert _answer("Phone (incl. country code)", p) == "+1 480-555-0100"
    # a long label that says how to write it (EMD Electronics' Phenom form, Oct 2026)
    emd = 'Phone Number [must include your location code using the "+", (e.g. +1##########), with no spaces or dashes]*'
    assert _answer(emd, p) == "+14805550100"
    assert _answer("Phone number, digits only, no spaces or dashes", p) == "4805550100"
    assert _answer("Phone Number (with country code, no spaces)", p) == "+14805550100"
    assert _answer("Phone number including international dialing code, e.g. +44 7700 900123, no spaces", p) == \
        "+14805550100"
    assert _answer("Phone Number (do not include country code)", p) == "480-555-0100"
    assert _answer("Phone number, no spaces or dashes, do not include country code", p) == "4805550100"
    assert _answer("Phone", p) == "480-555-0100"
    for label in ("Upload a copy of your degree or diploma", "Unofficial transcript"):
        assert resolve_field({"id": "1", "label": label, "kind": "file"}, p, {"company": "Acme"}) is None, label
    plan = plan_autofill([{"id": "p", "label": "Position Applied For", "kind": "text", "value": ""},
                          {"id": "d", "label": "Start Date", "kind": "text", "value": ""}], p, {"company": "Acme"})
    assert plan["to_fill"] == [], plan["to_fill"]


def test_a_profile_list_written_another_way_doesnt_stop_the_fill():
    for key in ("answers", "education_history", "work_history"):
        p = _person(**{key: 1})
        plan_autofill([{"id": "1", "label": "Have you previously worked for Acme?", "kind": "text", "value": ""},
                       {"id": "2", "label": "Graduation Year", "kind": "text", "value": ""}], p, {"company": "Acme"})


def test_a_county_comes_from_an_arizona_citys():
    """Oracle's address block (Mayo Clinic's, live, Oct 2026) requires a County, and the desk
    asked for it: the profile names Chandler, AZ, which is in Maricopa County. A county the
    profile names wins; a city the table doesn't know, or another state's, is left to the person."""
    import yaml
    from job_apply import config

    p = prof()
    counties = ["Apache, AZ", "Cochise, AZ", "Maricopa, AZ", "Pima, AZ", "Pinal, AZ"]
    assert resolve_field(f("County *", "combobox", options=counties), p).value == "Maricopa, AZ"
    assert resolve_field(f("County"), p).value == "Maricopa"
    assert "Maricopa" in place_words("city", p)

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())

    def with_address(**address):
        path.write_text(yaml.safe_dump({**data, "personal": {**data["personal"],
                                                            "address": {**data["personal"]["address"], **address}}}))
        return Profile.load()

    assert resolve_field(f("County"), with_address(county="Yavapai")).value == "Yavapai"
    assert resolve_field(f("County"), with_address(city="San Tan Valley")).value == "Pinal"
    assert resolve_field(f("County"), with_address(city="Sedona")) is None
    assert resolve_field(f("County"), with_address(city="Austin", state="TX")) is None


def test_a_long_list_of_places_is_searched_for_the_profiles():
    """Oracle's City on Mayo Clinic's form (live, Oct 2026) opened at "Aaron, Clinton, KY" without
    its search flag, and the desk asked the person for a city the profile names. A long list of
    places shows only a slice: the fill searches it. A short list that lacks the place is asked."""
    p = prof()
    towns = [f"Aaron{n}, Clinton, KY" for n in range(40)]
    ans = resolve_field(f("City *", "combobox", options=towns), p)
    assert ans is not None and ans.value == "Chandler" and ans.rule == "city"
    assert resolve_field(f("City *", "combobox", options=towns[:5]), p) is None


def test_located_near_the_job_or_willing_to_relocate():
    """Mayo Clinic (live, Oct 2026) asks "Are you currently located within 100 miles of a Mayo
    Clinic campus, or willing to relocate?" of a Phoenix job, and the desk left it to a person
    who lives in Chandler. Yes for one living in the job's metro area, or who would move; a
    question that asks which ("or will you need to relocate?") stays the person's."""
    import yaml
    from job_apply import config

    p = prof()  # Chandler, AZ; not willing to relocate
    yes_no = ["Yes", "No"]
    mayo = ("This is a hybrid position and must be located within 100 miles of a Mayo Clinic campus. Are you "
            "currently located within 100 miles of a Mayo Clinic campus, or willing to relocate?")

    def answer(label, location, profile=p):
        a = resolve_field(f(label, "radio_group", options=yes_no), profile, {"company": "Mayo Clinic", "location": location})
        return a and a.value

    assert answer(mayo, "Phoenix, AZ, United States") == "Yes"
    assert answer(mayo, "Scottsdale, Arizona") == "Yes"
    assert answer(mayo, "US-AZ-Phoenix") == "Yes"
    assert answer(mayo, "Rochester, MN; Phoenix, AZ") == "Yes"
    assert answer("Do you live within commuting distance of the job, or are you open to relocating?", "Tempe, AZ") == "Yes"
    assert answer(mayo, "Tucson, AZ") is None  # another metro area
    assert answer(mayo, "Glendale, CA") is None  # Arizona's Glendale is near, California's isn't
    assert answer(mayo, "") is None
    assert answer("Do you live in the area, or will you need to relocate?", "Phoenix, AZ") is None
    assert answer("Which campus are you located near, or are you willing to relocate?", "Phoenix, AZ") is None
    assert answer(mayo, "Rochester, MN / Phoenix, AZ") == "Yes"

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())

    def with_(personal=None, preferences=None):
        new = {**data, "preferences": {**data.get("preferences", {}), **(preferences or {})}}
        new["personal"] = {**data["personal"], "address": {**data["personal"]["address"], **(personal or {})}}
        path.write_text(yaml.safe_dump(new))
        return Profile.load()

    assert answer(mayo, "Tucson, AZ", with_(preferences={"willing_to_relocate": True})) == "Yes"
    assert answer(mayo, "Phoenix, AZ", with_(personal={"city": "San Tan Valley"})) == "Yes"  # Pinal County: still Phoenix's
    assert answer(mayo, "Phoenix, AZ", with_(personal={"city": "Austin", "state": "TX"})) is None


def test_how_did_you_first_hear():
    """Mayo Clinic (live, Oct 2026) asks "How did you first hear about this opportunity?", which
    the how-heard rule missed for the word "first": its "Mayo Clinic Career Site" is the
    profile's "Company Website"."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "how_did_you_hear": "Company Website"}}))
    options = ["I am a current Mayo Clinic Employee (not a trainee)", "Handshake", "Indeed", "LinkedIn",
               "Mayo Clinic Career Site", "Referral - Other", "Other"]
    ans = resolve_field(f("How did you first hear about this opportunity?", "combobox", options=options), prof(),
                        {"company": "Mayo Clinic"})
    assert ans.value == "Mayo Clinic Career Site"
    assert resolve_field(f("Where did you first learn of this job?"), prof()).value == "Company Website"


def test_a_preferred_full_name():
    """American Express's Oracle form (live, Oct 2026) requires a "Preferred Full Name", which
    the desk asked for: the name the person goes by, then their last name."""
    assert resolve_field(f("Preferred Full Name"), prof()).value == "Sam Rivera"
    assert resolve_field(f("Preferred Name"), prof()).value == "Sam"
    assert resolve_field(f("Emergency contact: preferred full name"), prof()) is None


def test_hired_before_by_this_employer():
    """American Express (live, Oct 2026): "Have you been hired at any time in the past for a
    position with American Express Company or any of its subsidiaries or affiliates?" is about
    working there before. The profile can say Yes; it can't say No for the subsidiaries."""
    yes_no = ["Yes", "No"]
    asked = ("Have you been hired at any time in the past for a position with American Express Company or any of its "
             "subsidiaries or affiliates?")
    amex = {"company": "American Express"}
    assert resolve_field(f(asked, "radio_group", options=yes_no), prof(), amex) is None
    assert resolve_field(f("Have you been hired at any time in the past by American Express?", "radio_group",
                           options=yes_no), prof(), amex).value == "No"
    assert resolve_field(f("Have you ever been hired by us before?", "radio_group", options=yes_no), prof(),
                         amex).value == "No"
    assert resolve_field(f("Have you been hired at any time in the past for a position with Intel or its subsidiaries?",
                           "radio_group", options=yes_no), prof(), {"company": "Intel Corporation"}).value == "Yes"


def test_a_preferred_work_location_from_the_profiles_places():
    """American Express (live, Oct 2026) asks "Indicate your highest level of preference by
    work location:" among its offices; the profile's first place answers it."""
    import yaml
    from job_apply import config

    offices = ["Fort Lauderdale, FL", "New York, NY", "Phoenix, AZ", "Palo Alto, CA", "Salt Lake City, UT"]
    asked = f("Indicate your highest level of preference by work location:", "radio_group", options=offices)
    assert resolve_field(asked, prof()) is None  # the profile names no places
    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "locations": ["Phoenix, AZ", "Tempe, AZ"]}}))
    assert resolve_field(asked, prof()).value == "Phoenix, AZ"
    assert resolve_field(f("Preferred Location"), prof()).value == "Phoenix, AZ"


def test_a_zip_is_never_read_as_a_range():
    """Mayo Clinic's Oracle ZIP list (live, Oct 2026) opens at "00501, Holtsville, Suffolk, NY",
    and the desk picked "01022, Westover AFB, Hampden, MA" for 85225: "Westover" read as "over
    1022". That put the address in Massachusetts, and Chandler wasn't found among its cities."""
    zips = ["00501, Holtsville, Suffolk, NY", "00544, Holtsville, Suffolk, NY", "01001, Agawam, Hampden, MA",
            "01002, Amherst, Hampshire, MA", "01022, Westover AFB, Hampden, MA", "01026, Cummington, Hampshire, MA"]
    zips += [f"010{n}, Springfield, Hampden, MA" for n in range(30, 50)]
    ans = resolve_field(f("ZIP Code *", "combobox", options=zips), prof())
    assert ans.value == "85225"  # not on the list's first page: searched for
    assert choose_option("4", ["Under 2 years", "2-3 years", "More than 3 years"]) == "More than 3 years"
    assert choose_option("1", ["Under 2 years", "2-3 years", "More than 3 years"]) == "Under 2 years"
    assert choose_option("7", ["0-2", "3-5", "5+"]) == "5+"
    assert choose_option("12", ["Thunder Bay 10", "Hanover 20"]) is None


def test_a_yes_is_a_whole_word():
    """polarity() read any answer starting with "y" as a yes: "Yuma, AZ" and "Yearly" counted
    among the yeses of a choice list, and a city answer was taken as agreeing to something."""
    for said in ("Yuma, AZ", "Youngtown", "Yearly", "Yesterday", "Agreement", "Trueblue"):
        assert polarity(said) is None, said
    for said in ("Yes", "Y", "yes, I agree", "Yes - 25%", "True", "Agree", "I agree", "Agreed", "I am a veteran"):
        assert polarity(said) is True, said
    assert choose_option("Yes", ["Yearly", "No"]) is None


def test_never_the_opposite_of_the_answer():
    """When a list doesn't have the profile's own wording, the closest-looking choice could be
    its opposite: "Not Hispanic or Latino" went to "Hispanic or Latino" (and to "Hispanic/Latino"
    over "Non-Hispanic/Latino"), "I am not a protected veteran" to "Protected Veteran"."""
    assert choose_option("Not Hispanic or Latino", ["Hispanic or Latino", "Decline to answer"]) is None
    assert choose_option("Not Hispanic or Latino", ["Hispanic/Latino", "Non-Hispanic/Latino"]) != "Hispanic/Latino"
    assert choose_option("Hispanic or Latino", ["Not Hispanic or Latino", "Decline"]) is None
    assert choose_option("Hispanic or Latino", ["Yes, Hispanic or Latino", "Not Hispanic or Latino"]) == "Yes, Hispanic or Latino"
    assert choose_option("I am not a protected veteran", ["Protected Veteran", "Decline"]) is None
    # nor a choice that denies more than the answer: a veteran who isn't a protected one is a veteran
    assert choose_option("I am not a protected veteran", ["Protected Veteran", "Not a Veteran", "Decline"]) is None
    assert choose_option("Not a Veteran", ["I am a protected veteran", "I am not a protected veteran"]) == \
        "I am not a protected veteran"
    # a denial about something else doesn't stand in the way
    assert choose_option("Asian", ["Asian (Not Hispanic or Latino)", "White (Not Hispanic or Latino)"]) == \
        "Asian (Not Hispanic or Latino)"
    assert choose_option("No, I do not have a disability", ["Yes, I have a disability", "No, I don't have a disability",
                                                             "I don't wish to answer"]) == "No, I don't have a disability"
    assert choose_option("No, I will not require sponsorship", ["Yes, I will require sponsorship", "No"]) == "No"
    # a decline's "not" denies nothing
    assert choose_option("I don't wish to answer", ["Yes, I have a disability", "I do not want to answer"]) == \
        "I do not want to answer"


# Self-identification lists as sites word them (the U.S. disability and veteran forms, Workday's,
# SuccessFactors' at Qorvo, Oracle's at onsemi)
DISABILITY = ["Yes, I have a disability, or have had one in the past",
              "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
VETERAN = ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
           "I don't wish to answer"]
VETERAN_STATUS = ["I am not a veteran", "I identify as one or more of the classifications of protected veteran",
                  "I am a veteran but not a protected veteran", "I do not wish to self-identify"]


@pytest.mark.parametrize("military, status, plain", [(False, "I am not a veteran", "I am not a protected veteran"),
                                                     (None, None, "I am not a protected veteran")])
def test_someone_who_never_served_is_not_a_veteran_on_every_list(military, status, plain):
    """Live test 2 (issue #112, note 18, Oct 2026): Workday's "I am not a veteran" list was left empty
    for "I am not a protected veteran", which doesn't say whether the person served. The profile's
    background.military: false does: they're not a veteran, on a list that asks that and on one that
    asks only about protected veterans. Without it, the person is asked, as before."""
    p = Profile({"eeo": {"veteran": "I am not a protected veteran"}, "background": {"military": military}})
    status_list = resolve_field(f("Please indicate your veteran status.", "select", options=VETERAN_STATUS), p)
    assert (status_list and status_list.value and choose_option(status_list.value, VETERAN_STATUS)) == status
    plain_list = resolve_field(f("Veteran Status", "select", options=VETERAN), p)
    assert choose_option(plain_list.value, VETERAN) == plain


def test_sponsorship_and_military_asked_in_other_words():
    """Live test 2 (issue #112, note 17): Workday's "Will you now or in the future require <employer> to
    commence an immigration case in order to employ you?" is the sponsorship question, and "Do you have
    previous military experience?" the military one; both were asked though the profile answers them."""
    p = Profile({"work_authorization": {"authorized_to_work": True, "requires_sponsorship": False},
                 "background": {"military": False}})
    case = resolve_field(f("Will you now or in the future require Example Fab to commence an immigration case in "
                           "order to employ you?", "select", options=["Yes", "No"]), p, {"company": "Example Fab"})
    assert case is not None and (case.rule, choose_option(case.value, ["Yes", "No"])) == ("sponsorship", "No")
    # the same wording negated: someone who needs no sponsorship says yes to it
    negated = resolve_field(f("I will not now or in the future require Example Fab to commence an immigration case in "
                              "order to employ me.", "select", options=["Yes", "No"]), p, {"company": "Example Fab"})
    assert negated is not None and choose_option(negated.value, ["Yes", "No"]) == "Yes", negated
    served = resolve_field(f("Do you have previous military experience?", "select", options=["Yes", "No"]), p)
    assert served is not None and (served.rule, choose_option(served.value, ["Yes", "No"])) == ("military", "No")
    # Linde's (live check, Oct 11): "a visa to permit you to legally work", and "authorized to legally work"
    visa = resolve_field(f("Will you now or in the future require a visa to permit you to legally work in the United "
                           "States?*", "radio_group", options=["Yes", "No"]), p)
    assert visa is not None and (visa.rule, choose_option(visa.value, ["Yes", "No"])) == ("sponsorship", "No")
    no_visa = resolve_field(f("I will not require a visa to work in the United States.", "radio_group",
                              options=["Yes", "No"]), p)
    assert no_visa is not None and choose_option(no_visa.value, ["Yes", "No"]) == "Yes", no_visa
    legally = resolve_field(f("Are you authorized to legally work in the United States? If offered employment, you will "
                              "be required to present documentation.*", "radio_group", options=["Yes", "No"]), p)
    assert legally is not None and (legally.rule, choose_option(legally.value, ["Yes", "No"])) == ("authorized", "Yes")
    # a visa for travel isn't a work visa (review)
    travel = resolve_field(f("Will you require a visa to travel for work?", "radio_group", options=["Yes", "No"]), p)
    assert travel is None or travel.rule != "sponsorship", travel


def test_a_degree_of_one_kind_is_picked_however_the_list_writes_it_and_a_bare_one_never_picks_a_kind():
    """Workday Degree lists (Onto's, nightly live check, Oct 11) list a level's kinds: "Bachelor of Arts" and
    "Bachelor of Science", or "BA" / "BS", "B.S.", "Bachelor's - BS". A profile's "Bachelor of Science" (or
    "BSEE") is the BS however it's written; a bare "Bachelor's Degree" says no kind, so none is picked (it
    was "Bachelor's - BS", a kind the profile never said)."""
    lists = [["Bachelor of Arts", "Bachelor of Science"], ["BA", "BS"], ["B.S.", "B.A."],
             ["Bachelor of Science (BS)", "Bachelor of Arts (BA)"], ["Bachelor's - BS", "Bachelor's - BA"]]
    for options in lists:
        bs = next(o for o in options if "S" in o.replace("Bachelor's", "").replace("Bachelor", "").replace("Arts", ""))
        for said in ("Bachelor of Science", "BSEE", "BS Electrical Engineering"):
            assert choose_option(said, options) == bs, (said, options)
        assert choose_option("Bachelor's Degree", options) is None, options
    # one of the level, or the same words: as before
    assert choose_option("Bachelor's Degree", ["Associates", "Bachelors", "Masters"]) == "Bachelors"
    assert choose_option("Bachelor's Degree", ["Bachelor's Degree", "Master of Science"]) == "Bachelor's Degree"
    # the level written twice, no kind named in either (ASM's Workday, the second nightly at every employer, Oct
    # 12): the kind the profile names isn't there, the level is: the one that says it's a degree
    asm = ["Associates", "Associate's Degree", "Bachelors", "Bachelor's Degree", "Masters", "Master's Degree", "Other"]
    for said in ("Bachelor of Science", "Bachelor's Degree", "BSEE"):
        assert choose_option(said, asm) == "Bachelor's Degree", said


def test_a_bare_job_title_beside_a_profile_employers_company_gets_that_jobs_title():
    """Cornerstone's resume reader (Linde's and Matheson's, live check Oct 11) adds an experience row with its
    Company filled in and its Job Title empty, under a heading the form reader doesn't tie to the boxes: the
    title of the profile's job at that company. Never for a company the profile doesn't have."""
    p = Profile({"work_history": [{"title": "Equipment Technician", "company": "Example Fab", "start": "2021-03",
                                   "end": "present"},
                                  {"title": "Field Tech", "company": "Other Semi", "start": "2018-01", "end": "2021-02"}]})

    def row(company):
        return [{"id": "c", "label": "Company*", "kind": "text", "required": True, "value": company},
                {"id": "t", "label": "Job Title*", "kind": "text", "required": True, "value": ""},
                {"id": "s", "label": "Start Date", "kind": "text", "value": "03/2021"}]

    plan = plan_autofill(row("Example Fab Inc."), p, {})
    assert [(t["id"], t["value"]) for t in plan["to_fill"]] == [("t", "Equipment Technician")], plan
    for company in ("Somewhere Else", ""):
        plan = plan_autofill(row(company), p, {})
        assert "t" not in {t["id"] for t in plan["to_fill"]}, (company, plan)
    # nor a reference's position, who may well work where the person does
    reference = [{"id": "n", "label": "Reference Name", "kind": "text", "value": "Pat Example"}] + row("Example Fab")
    assert "t" not in {t["id"] for t in plan_autofill(reference, p, {})["to_fill"]}


def test_a_list_still_on_its_not_specified_first_choice_is_filled():
    """Kforce's Taleo (live run 10, Oct 2026): its required "Closest Metropolitan City" is three lists, Country,
    State/Province and Region, each on its first choice "Not Specified", which read as answered, so none was
    filled ("The field Closest Metropolitan City is mandatory"). A list on that first choice is filled; a
    "Not Specified" chosen further down a list (a self-identification's decline) is left as it is."""
    p = Profile({"personal": {"address": {"country": "United States", "state": "AZ", "city": "Chandler"}},
                 "eeo": {"gender": "Male"}})
    country = {"id": "c", "label": "Country", "kind": "select", "value": "Not Specified",
               "section": "Closest Metropolitan City . Required", "options": ["Not Specified", "United States", "Canada"]}
    gender = {"id": "g", "label": "Gender", "kind": "select", "value": "Not Specified",
              "options": ["Male", "Female", "Not Specified"]}
    # nor a self-identification list whose first choice is "Not Specified": the person may have chosen it
    taleo_gender = {**gender, "id": "t", "options": ["Not Specified", "Male", "Female"]}
    # its Region is a list of cities: the profile's city goes there, not its state (live run 12)
    region = {"id": "r", "label": "Region", "kind": "select", "value": "Not Specified",
              "section": "Closest Metropolitan City . Required", "options": ["Not Specified", "Anthem", "Bisbee", "Chandler"]}
    assert resolve_field(region, p).value == "Chandler"
    # a required self-identification list on it, for a profile that declines and a list without a decline: asked,
    # without the "Not Specified" that is its empty choice (an answer of it was taken for none, live run 24)
    decliner = Profile({"eeo": {"gender": "Decline to self-identify"}})
    birth = {"id": "b", "label": "Birth Assigned Gender", "kind": "select", "value": "Not Specified", "required": True,
             "options": ["Not Specified", "Male", "Female"]}
    asked = plan_autofill([birth], decliner, {})["needs_input"]
    assert [q["options"] for q in asked] == [["Male", "Female"]], asked
    plan = plan_autofill([country, gender, taleo_gender], p, {})
    assert [(t["id"], t["value"]) for t in plan["to_fill"]] == [("c", "United States")], plan
    assert plan["already_filled"] == ["g", "t"], plan


def test_a_list_the_page_marks_invalid_takes_the_profiles_answer_and_a_required_self_id_is_answered():
    """Live run 13 (Oct 2026). Northrop's Eightfold drew its phone "Country code" on Mexico (+52) and marked it
    invalid ("Enter a valid phone number and country code"); the desk left it, as a box with a value. A list
    the page marks invalid takes the profile's answer where it's another. Kforce's required "Birth Assigned
    Gender" sat on its first choice "Not Specified" and was left as a self-identification the person may have
    chosen; a required one is answered."""
    p = Profile({"personal": {"phone": "480-555-0199", "phone_country_code": "+1",
                              "address": {"country": "United States"}}, "eeo": {"gender": "Male"}})
    codes = ["\U0001F1FA\U0001F1F8 (+1) United States", "\U0001F1F2\U0001F1FD (+52) Mexico", "\U0001F1E8\U0001F1E6 (+1) Canada"]
    code = {"id": "c", "label": "Country code", "kind": "combobox", "value": codes[1], "invalid": True,
            "required": True, "options": codes}
    birth = {"id": "b", "label": "3. Birth Assigned Gender . Required", "kind": "select", "value": "Not Specified",
             "required": True, "options": ["Not Specified", "Male", "Female"]}
    plan = plan_autofill([code, birth], p, {}, redo={"c"})  # (the review step's, once a job)
    got = {t["id"]: t["value"] for t in plan["to_fill"]}
    assert set(got) == {"c", "b"} and got["b"] == "Male", plan
    # never on an ordinary pass (the person may have chosen it), nor for a box not named
    assert plan_autofill([code], p, {})["already_filled"] == ["c"]
    # a decline in the profile never gets the list's own "Not Specified" placeholder back: it's asked
    declined = Profile({"eeo": {"gender": "I choose not to disclose"}})
    plan = plan_autofill([birth], declined, {})
    assert plan["to_fill"] == [] and [q["id"] for q in plan["needs_input"]] == ["b"], plan


def test_a_text_box_the_page_says_is_wrong_takes_the_profiles_answer_only_where_it_differs():
    """EMD's Phenom (live run 41, Oct 12): its resume parse wrote the phone as bare digits where the box asks for
    "+1##########", and called it invalid. Named in `redo`, a text box showing another value takes the
    profile's; one already showing the profile's answer, or one the profile doesn't answer, is left."""
    p = Profile({"personal": {"phone": "480-555-0199", "phone_country_code": "+1"}})
    label = 'Phone Number [must include your location code using the "+", (e.g. +1##########), with no spaces or dashes]*'
    parsed = {"id": "p", "label": label, "kind": "text", "value": "4805550199", "required": True}
    plan = plan_autofill([parsed], p, {}, redo={"p"})
    assert [(t["id"], t["value"]) for t in plan["to_fill"]] == [("p", "+14805550199")], plan
    assert plan_autofill([parsed], p, {})["already_filled"] == ["p"]  # (never on an ordinary pass)
    same = {**parsed, "value": "+14805550199"}
    assert plan_autofill([same], p, {}, redo={"p"})["already_filled"] == ["p"]
    unknown = {"id": "u", "label": "Badge number", "kind": "text", "value": "12", "required": True}
    plan = plan_autofill([unknown], p, {}, redo={"u"})
    assert plan["to_fill"] == [] and plan["already_filled"] == ["u"], plan


def test_a_work_authorization_question_drawn_as_a_text_area_takes_the_profiles_yes_or_no():
    """Nikon's UKG Pro (live run 14, Oct 2026) draws "Are you legally authorized to work in the US for any
    employer?" and "Do you now or in the future require sponsorship...?" as required text areas, and the desk
    left them (text areas are long answers). A yes/no work question drawn so takes the profile's answer; a long
    answer, or an unclear one ("Do you require relocation?"), is still the person's."""
    p = Profile({"work_authorization": {"authorized_to_work": True, "requires_sponsorship": False},
                 "preferences": {"willing_to_relocate": True}})

    def answer(label):
        a = resolve_field(f(label, "textarea", required=True), p, {"company": "Nikon"})
        return a and a.value

    assert polarity(answer("Are you legally authorized to work in the US for any employer?")) is True
    assert answer("Do you now or in the future require sponsorship for employment visa status?") == "No"
    assert answer("Do you require relocation?") is None
    assert answer("Describe your authorization to work in the US.") is None


def test_a_decline_finds_the_lists_own_way_to_decline_and_is_never_a_no():
    """Every site words "I'd rather not say" its own way, and so do people. "I don't want to
    answer" wasn't taken for a decline: its "don't" made it a "No", and it went to "I am not a
    protected veteran", a status the person never gave. A list with no way to decline gets
    nothing (Qorvo's veteran list is only "No, I am not ..." and "Yes, I am ...")."""
    declines = ("I don't wish to answer", "I don't want to answer", "I do not want to answer", "Decline to self-identify",
                "I choose not to self-identify", "Prefer not to say", "I'd rather not say", "I wish not to answer",
                "Not specified")
    for said in declines:
        assert choose_option(said, DISABILITY) == "I do not want to answer", said
        assert choose_option(said, VETERAN) == "I don't wish to answer", said
        assert choose_option(said, VETERAN_STATUS) == "I do not wish to self-identify", said
        assert choose_option(said, ["Yes", "No", "Decline to self-identify"]) == "Decline to self-identify", said
        assert choose_option(said, ["Male", "Female", "Not Specified"]) == "Not Specified", said
        assert choose_option(said, ["Man", "Woman", "Non-binary", "I prefer not to answer"]) == "I prefer not to answer"
        assert choose_option(said, ["No Selection", "No, I am not a Protected Veteran", "Yes, I am a Protected Veteran"]) \
            is None, said
        assert choose_option(said, ["Female", "Male"]) is None, said
    # a "no" about something else than answering is an answer, not a decline
    assert choose_option("No", ["Yes, I am willing to relocate", "I do not wish to relocate"]) == "I do not wish to relocate"


def test_a_decline_is_found_where_workday_names_each_choice_with_its_country():
    """Probe 48 (Oct 13): Workday names each self-identification choice with the form's country, "Undeclared (United
    States of America)", so its declines weren't found, and Air Products' gender and ASML's race lists were asked
    of the person whose profile declines. A choice's own words are read without that aside; an answer is never a
    decline for it."""
    gender = ["Select One", "Female (United States of America)", "Male (United States of America)",
              "Non-Binary (United States of America)", "Undeclared (United States of America)"]
    race = ["Select One", "American Indian or Alaska Native (United States of America)", "Asian (United States of America)",
            "Hispanic or Latino (United States of America)", "Not Declared (United States of America)",
            "Not Specified (United States of America)", "Two or More Races (United States of America)",
            "White (United States of America)"]
    assert choose_option("Decline to self-identify", gender) == "Undeclared (United States of America)"
    assert choose_option("I don't wish to answer", race) == "Not Declared (United States of America)"
    assert choose_option("Female", gender) == "Female (United States of America)"
    assert not is_decline("Female (United States of America)") and not is_decline("Asian (Not Declared)")


def test_an_answer_is_never_a_refusal_to_answer():
    """A list's decline ("I do not want to answer") reads as a "No" by its "I do not": a plain
    "No" couldn't choose between it and "No, I do not have a disability", and "I am not a
    protected veteran" went to Workday's "I do not wish to self-identify". A person who
    answered gets their answer or is asked, never a decline they didn't give."""
    assert choose_option("No", DISABILITY) == "No, I do not have a disability and have not had one in the past"
    assert choose_option("No", VETERAN) == "I am not a protected veteran"
    assert choose_option("Yes", DISABILITY) == "Yes, I have a disability, or have had one in the past"
    # not a veteran at all, or a veteran who isn't a protected one: the profile doesn't say which
    assert choose_option("I am not a protected veteran", VETERAN_STATUS) is None
    assert choose_option("I am not a protected veteran", VETERAN) == "I am not a protected veteran"
    # a choice that declines and answers too (onsemi's) is still that answer, and a decline's
    # second best after a plain decline
    combined = ["I am a Protected Veteran", "I am a Veteran", "I do not wish to Identify or I am not a Veteran"]
    assert choose_option("No", combined) == "I do not wish to Identify or I am not a Veteran"
    assert choose_option("I don't wish to answer", combined) == "I do not wish to Identify or I am not a Veteran"
    assert choose_option("I don't wish to answer", combined + ["Decline to answer"]) == "Decline to answer"


def test_a_self_identification_question_with_the_law_in_its_label_is_that_question(job_apply_home):
    """Micron's veteran question (its U.S. postings, Oct 2026) carries the whole VEVRAA notice
    in its label. "For more information, call the U.S. Department of Labor" in it read as an
    on-call question, so the profile's shift answer went to it ("Yes" there is "Yes, I am a
    protected veteran"). The disability form's notice speaks of accommodations, which shut out
    the disability rule. A self-identification question is told by its words and its choices."""
    import yaml

    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text())
    data["preferences"]["flexible_schedule"] = True
    data["eeo"]["disability"] = "No"
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data))
    vevraa = ("U.S. – Protected Veteran Self-Identification This employer is a Government contractor subject to the "
              "Vietnam Era Veterans' Readjustment Assistance Act of 1974, as amended (VEVRAA). For more information, call "
              "the U.S. Department of Labor's Veterans' Employment and Training Service (VETS). If you believe you belong "
              "to any of the categories of protected veterans, please indicate by selecting the appropriate value below.")
    micron = ["I IDENTIFY AS ONE OR MORE OF THE CLASSIFICATIONS OF PROTECTED VETERANS LISTED BELOW",
              "I DO NOT WISH TO SELF-IDENTIFY", "I AM NOT A VETERAN", "I IDENTIFY AS A VETERAN, JUST NOT A PROTECTED VETERAN"]
    ans = resolve_field(f(vevraa, "combobox", options=micron), prof())
    assert (ans.rule, ans.value) == ("veteran", "I DO NOT WISH TO SELF-IDENTIFY")
    yes_no = ["Yes, I am a protected veteran", "No, I am not a protected veteran", "I don't wish to answer"]
    assert resolve_field(f(vevraa, "select", options=yes_no), prof()).value == "I don't wish to answer"
    # its list not read yet: the notice's words alone still don't make it an on-call question
    assert resolve_field(f(vevraa, "combobox"), prof()).rule == "veteran"
    cc305 = ("Voluntary Self-Identification of Disability. Why are you being asked to complete this form? We are a federal "
             "contractor required to provide equal employment opportunity to qualified people with disabilities. "
             "Federal law requires employers to provide reasonable accommodations to qualified applicants. Please check "
             "one of the boxes below")
    ans = resolve_field(f(cc305, "radio_group", options=DISABILITY), prof())
    assert (ans.rule, ans.value) == ("disability", "No, I do not have a disability and have not had one in the past")
    # a question that only mentions a disability is still not the self-identification one
    accommodation = ["Yes, I need an accommodation due to a disability", "No, I do not need an accommodation for a disability"]
    assert resolve_field(f("Do you need an accommodation for a disability to interview?", "radio_group",
                           options=accommodation), prof()) is None


def test_a_self_identification_answer_is_picked_from_the_list_or_said_never_searched_for():
    """onsemi's Oracle form (Oct 2026): its Gender lists Female and Male, and since Oracle's
    lists are searched, the profile's "Decline to self-identify" went on to be typed into it,
    found nothing, and was skipped. A self-identification list is all there on opening: its own
    way of declining is picked, and with none, the question is the person's, with the
    profile's answer said (Qorvo's optional veteran list was left empty without a word)."""
    gender = f("Gender", "combobox", search=True, options=["Female", "Male"])
    assert resolve_field(gender, prof()) is None
    assert resolve_field({**gender, "options": ["Female", "Male", "I do not wish to disclose"]}, prof()).value == \
        "I do not wish to disclose"
    veteran = {**f("Pre-Offer : Are you a Protected Veteran?", "combobox", paged=True,
                   options=["No Selection", "No, I am not a Protected Veteran", "Yes, I am a Protected Veteran"]), "id": "2"}
    country = {**f("Country", "combobox", search=True, options=["Canada", "Mexico"]), "id": "3"}
    plan = plan_autofill([gender, veteran, country], prof())
    assert [(q["label"], q.get("unmatched")) for q in plan["needs_input"]] == [
        ("Gender", "Decline to self-identify"), ("Pre-Offer : Are you a Protected Veteran?", "I don't wish to answer")]
    assert [(x["label"], x["value"]) for x in plan["to_fill"]] == [("Country", "United States")]  # (searched for)


def test_a_short_list_of_places_is_read_with_the_rest_of_the_address():
    """A City list short enough to be read whole ("Chandler, Henderson, TX", "Chandler, Lincoln,
    OK", "Chandler, Maricopa, AZ") gave the first Chandler: the fill picks the one the rest of
    the address names, but the plan had already chosen Texas."""
    cities = ["Chandler, Henderson, TX", "Chandler, Lincoln, OK", "Chandler, Maricopa, AZ", "Chandler Heights, Maricopa, AZ"]
    assert resolve_field(f("City *", "select", options=cities), prof()).value == "Chandler, Maricopa, AZ"
    assert resolve_field(f("County", "select", options=["Maricopa, CA", "Maricopa, AZ"]), prof()).value == "Maricopa, AZ"
    assert resolve_field(f("State", "select", options=["Arkansas", "Arizona"]), prof()).value == "Arizona"


def test_some_college_however_a_list_words_it():
    """"Some college coursework" (no degree) found nothing in a list whose choice is "Some
    College, No Degree"; it's never a degree, nor high school."""
    degrees = ["High School Diploma/GED", "Associate Degree (AA/AS)", "Some College, No Degree", "Bachelor Degree"]
    assert choose_option("Some college coursework", degrees) == "Some College, No Degree"
    assert choose_option("Some college coursework", ["Some high school", "College coursework, no degree"]) == \
        "College coursework, no degree"
    assert choose_option("Some college", ["High school diploma or GED", "Associate degree", "Bachelor's degree"]) is None
    assert choose_option("Some high school", ["Some college, no degree", "Some high school, no diploma"]) == \
        "Some high school, no diploma"


def test_previously_employed_by_any_of_the_employers_companies():
    """Micron (live, Oct 2026): "Have you previously been employed by any Micron Company?" was
    asked, the word "any" before the name hiding that it asks about working there before."""
    yes_no = ["Yes", "No"]
    asked = f("Have you previously been employed by any Micron Company?", "radio_group", options=yes_no)
    assert resolve_field(asked, prof(), {"company": "Micron"}).value == "No"
    assert resolve_field(f("Have you ever worked for an Intel company?", "radio_group", options=yes_no), prof(),
                         {"company": "Intel Corporation"}).value == "Yes"
    # onsemi's (live check, Oct 11): worked IN its establishments
    onsemi = f("Have you ever previously worked in any onsemi establishment, anywhere in the world, whether as a "
               "full-time or part-time employee, contractor, temporary staff or intern?", "radio_group", options=yes_no)
    assert resolve_field(onsemi, prof(), {"company": "onsemi"}).value == "No"
    # never a place that starts the employer's name (review): "worked in Arizona" isn't working for ASU
    for asked in ("Have you ever worked in Arizona?", "Have you ever worked in Arizona State University labs?"):
        assert resolve_field(f(asked, "radio_group", options=yes_no), prof(),
                             {"company": "Arizona State University"}) is None, asked


def test_a_former_employee_question_naming_another_company_is_never_answered_from_this_ones():
    """An employer's form (live, Oct 2026) asked "Are you a current or former employee of Ernst &
    Young?": the desk took it for a question about working for the employer applied to, which
    the profile lists, and answered Yes. A question naming another organization is the person's."""
    yes_no = ["Yes", "No"]
    job = {"company": "Intel Corporation"}  # among the test profile's employers
    for asked in ("Are you a current or former employee of Ernst & Young?",
                  "Have you ever been employed by Example Audit LLP?", "Have you previously worked for a Big Four firm?"):
        assert resolve_field(f(asked, "radio_group", options=yes_no), prof(), job) is None, asked
    for asked in ("Are you a current or former employee?", "Have you ever been employed by this company?",
                  "Have you previously worked for us?", "Are you a former employee of Intel or any of its subsidiaries?"):
        assert resolve_field(f(asked, "radio_group", options=yes_no), prof(), job).value == "Yes", asked


def test_an_employee_id_goes_only_on_its_own_employers_forms():
    """A person who has worked for an employer has an ID there, which its forms ask for
    ("Employee ID (if applicable)", "please provide your WWID"). It's never put on another's."""
    from job_apply.config import Profile

    person = Profile({"history": {"employee_ids": {"Example Fab": "E1234567"}}})
    for asked in ("Employee ID (if applicable)", "If you have previously worked for Example Fab in any capacity, "
                  "please provide your WWID", "Employee Number"):
        got = resolve_field(f(asked), person, {"company": "Example Fab Corporation"})
        assert got is not None and got.value == "E1234567", asked
        assert resolve_field(f(asked), person, {"company": "Other Semi"}) is None, asked
    assert resolve_field(f("Employee ID (if applicable)"), prof(), {"company": "Example Fab"}) is None
    for asked in ("Referring Employee ID", "Your manager's employee number"):  # someone else's
        assert resolve_field(f(asked), person, {"company": "Example Fab"}) is None, asked


def test_recurring_background_questions_are_answered_from_a_no_in_the_profile():
    """An employer's Application Questions (live, Oct 2026) asked about government and Defense
    employment, agreements, intellectual property, plans to keep another job or sit on a board,
    and relatives there: each stopped the desk. A "no" in the profile's background answers any
    wording or time span of them; a "yes", or nothing said, leaves them to the person; a question
    about another organization, or another question that shares the words, isn't answered."""
    from job_apply.config import Profile

    no = {"defense_department": False, "government_employee": False, "military": False, "restrictive_agreement": False,
          "intellectual_property": False, "outside_work": False, "board_member": False, "relatives_at_employer": False}
    person, job, yn = Profile({"background": no}), {"company": "Example Fab"}, ["Yes", "No"]

    def answer(label, who=person, **kw):
        got = resolve_field(f(label, kw.pop("kind", "radio_group"), options=kw.pop("options", yn)), who, job)
        return got.value if got else None

    for asked in (
            "Are you a current employee of the US Department of Defense (DOD) or were you an employee of the US "
            "Department of Defense (DOD) on or after January 28, 2008?",
            "Are you a current Federal, State or Local Government employee; including military (other than the DOD) or "
            "have you at any time in the past 5 years been an employee of one of these entities?",
            "To the best of your knowledge and belief, are you aware of a contract or agreement with your current "
            "employer (or other company), such as a non-competition, non-disclosure, or non-solicitation agreement, that "
            "impact or interfere with your ability to work for the Company?",
            "Do you own, control, or have an economic interest in any intellectual property rights (patents, trademarks, "
            "or copyrights)?",
            "Do you have any friends/relatives presently employed by Example Fab?",
            "Are you currently serving on the board of directors of any for-profit company?"):
        assert answer(asked) == "No", asked
    plans = f("If hired, do you intend to (select all that apply):", "checkbox_group", options=[
        "Maintain any secondary non-company employment or engage in a non-company business activity?",
        "Sit on the board of directors or similar governing body of a non-company entity?", "Neither"])
    assert resolve_field(plans, person, job).value == ["Neither"]
    # theirs: another organization, another question with the same words, or a "yes"
    assert answer("Are you an immediate family member (parent, child, sibling, spouse/partner) of a partner at "
                  "Ernst & Young who is based out of the San Jose office?") is None
    assert answer("Are you willing to sign a non-compete agreement?") is None
    for follow_up in ("If yes, list the names of relatives employed here", "Branch of military service"):
        assert answer(follow_up, kind="text", options=None) is None, follow_up
    yes = Profile({"background": {**no, "government_employee": True, "outside_work": True}})
    assert answer("Have you been employed by the U.S. Government in the last two years?", yes) is None
    assert resolve_field(plans, yes, job) is None
    unsaid = Profile({"background": {**no, "military": None}})
    assert answer("Are you a current Federal, State or Local Government employee; including military?", unsaid) is None
    assert answer("Do you own any patents?", Profile({})) is None
    # the export-control question names the U.S. Government and an employee, and is its own
    export = resolve_field(f("Are you a U.S. citizen or national, lawful permanent resident, or have been approved for "
                             "refugee or asylee status by the U.S. Government? (This assists in determining whether we "
                             "must apply for an export license on behalf of an employee.)", "radio_group", options=yn),
                           person, job)
    assert export is None or export.rule != "government_employee", export


def test_a_family_member_at_the_employer_or_on_its_board_is_a_relatives_question():
    """Kforce's Taleo questionnaire (live, Oct 2026): "Is any member of your immediate family currently
    employed at Kforce or a member of the Board of Directors? . Required" was taken for a question
    about the person sitting on a board, and left empty. It's about relatives there: answered No from
    the profile's relatives_at_employer, never from board_member."""
    asked = ("4. Is any member of your immediate family currently employed at Kforce or a member of the Board of "
             "Directors? . Required")
    job, yn = {"company": "Kforce"}, ["Yes", "No"]
    no_relatives = Profile({"background": {"relatives_at_employer": False}})
    got = resolve_field(f(asked, "select", options=yn), no_relatives, job)
    assert got is not None and (got.value, got.rule) == ("No", "relatives"), got
    for other in ("Do you have any family obligations that would prevent you from working weekends or overtime?",
                  "Is your family able to relocate for this work?"):
        got = resolve_field(f(other, "select", options=yn), no_relatives, job)
        assert got is None or got.rule != "relatives", (other, got)
    on_no_board = Profile({"background": {"board_member": False}})
    assert resolve_field(f(asked, "select", options=yn), on_no_board, job) is None
    assert clean_label(asked).endswith("Board of Directors"), clean_label(asked)


def test_a_primary_number_choice_is_the_phones_type():
    """Kforce's Taleo (live check, Oct 2026): "Primary Number . Required", a choice of "Home Phone", "Work
    Phone" or "Cellular Phone", stopped the desk. It's the phone's type, from personal.phone_type ("Mobile"
    unless the profile says otherwise), and a mobile is a cellular phone. A box to type a number in is
    no such choice."""
    options = ["Not Specified", "Home Phone", "Work Phone", "Cellular Phone"]
    asked = f("Primary Number . Required", "select", options=options)
    got = resolve_field(asked, prof(), {"company": "Kforce"})
    assert got is not None and (got.rule, got.value) == ("phone_type", "Cellular Phone"), got
    home = Profile({"personal": {"phone": "480-555-0123", "phone_type": "Home"}})
    assert resolve_field(asked, home, {"company": "Kforce"}).value == "Home Phone"
    typed = resolve_field(f("Primary Number"), prof(), {})
    assert typed is None or typed.rule != "phone_type", typed


_KFORCE_WORK = "Work Phone Number"
_KFORCE_HOME = "Home Phone Number"
_KFORCE_CELL = ("Cellular Number Kforce may send non-marketing, employment-related text messages to any number(s) "
                "you submit. Reply STOP any time to cancel.")


def _numbers_in(labels, p, **kw):
    """The plan for phone boxes with these labels (ids 0, 1, ...): {label: the number it gets}, and the plan."""
    fields = [f(label, id=str(n), **kw) for n, label in enumerate(labels)]
    plan = plan_autofill(fields, p, {"company": "Kforce"})
    return {labels[int(item["id"])]: item["value"] for item in plan["to_fill"]}, plan


def test_a_phone_goes_in_the_box_of_its_own_type_and_no_other():
    """Kforce's Taleo (live check, Oct 2026): "Work Phone Number", "Home Phone Number" and "Cellular Number
    Kforce may send non-marketing, employment-related text messages ..." got the one phone in the first two and
    nothing in the third, and Taleo refused it: "Cellular Phone" was the primary number, so its number was
    required. The profile's one phone is of one type (personal.phone_type, "Mobile" unless it says otherwise):
    it goes in that type's box, however long the box's label, and the other types' boxes are left empty."""
    labels = [_KFORCE_WORK, _KFORCE_HOME, _KFORCE_CELL]
    number = "480-555-0123"
    got, plan = _numbers_in(labels, prof())
    assert got == {_KFORCE_CELL: number}, got
    assert [q["label"] for q in plan["needs_input"]] == [_KFORCE_WORK, _KFORCE_HOME]
    for phone_type, box in (("Home", _KFORCE_HOME), ("Work", _KFORCE_WORK), ("Cell", _KFORCE_CELL),
                            ("Cellular Phone", _KFORCE_CELL), ("Landline", _KFORCE_HOME), ("Mobile", _KFORCE_CELL)):
        typed = Profile({"personal": {"phone": number, "phone_type": phone_type}})
        assert _numbers_in(labels, typed)[0] == {box: number}, phone_type
    # a type that is none of the three says no box's kind: not the person's number for any of them
    other = Profile({"personal": {"phone": number, "phone_type": "Pager"}})
    assert _numbers_in(labels, other)[0] == {}


def test_a_phone_box_of_another_type_is_the_persons_even_when_required():
    """A required Work box beside the mobile's own box isn't filled with a number that isn't a work phone:
    it's the person's question, put to them as a required one. (The same for the other ways a form names a
    kind.) A form's one phone box is filled whatever its kind: test_a_forms_one_phone_box_gets_the_number."""
    got, plan = _numbers_in([_KFORCE_WORK, "Cell Phone"], prof(), required=True)
    assert got == {"Cell Phone": "480-555-0123"}, got
    assert [(q["label"], q.get("required")) for q in plan["needs_input"]] == [(_KFORCE_WORK, True)]
    for other in ("Home Phone", "Work Phone", "Business Phone", "Office Phone Number", "Phone (Home)", "Phone Number - Work",
                  "Telephone (Work)", "Home Number"):
        assert other not in _numbers_in([other, "Cell Phone"], prof())[0], other
    home = Profile({"personal": {"phone": "480-555-0123", "phone_type": "Home"}})
    for other in ("Mobile Phone", "Cell Phone Number", "Mobile", "Cellular", "Phone (Mobile)", "Work Phone Number"):
        assert other not in _numbers_in([other, "Home Phone"], home)[0], other


def test_a_phone_box_that_names_no_type_or_the_profiles_is_filled():
    """"Phone", "Phone Number" and "Primary Phone" say no kind: filled as always; so is a box of the
    profile's own type, long or short, whatever its other words."""
    number = "480-555-0123"
    for label in ("Phone", "Phone Number", "Primary Phone", "Telephone", "Phone Number (including country code)"):
        assert resolve_field(f(label), prof()).value in (number, f"+1 {number}"), label
    for label in ("Mobile Phone", "Mobile", "Cell Phone", "Cellular Number", "Mobile Number *", "Phone (Mobile)", _KFORCE_CELL,
                  "Cell Phone Number Please include the area code"):
        assert resolve_field(f(label), prof()).value == number, label
    home = Profile({"personal": {"phone": number, "phone_type": "Home phone"}})
    assert resolve_field(f("Home Phone Number"), home).value == number
    # a long label is a phone box only when it names one, not when it only starts with a word of a type
    for label in ("Mobile devices and cell phone systems you have installed or repaired in the last ten years",
                  "Work history, in your own words, for the last ten years: employer, title and dates"):
        assert resolve_field(f(label), prof()) is None, label


def test_text_messages_notice_is_no_sign_up_but_an_opt_in_or_marketing_is():
    """Kforce's Cellular box says it "may send non-marketing, employment-related text messages": that is no
    sign-up, the box is the application's number. A checkbox that agrees to texts is never ticked, and a box
    that signs the person up for marketing, job alerts or a newsletter, or asks their consent, stays theirs."""
    assert resolve_field(f(_KFORCE_CELL), prof()).value == "480-555-0123"
    optin = f("I agree to receive text messages about jobs", "checkbox")
    assert resolve_field(optin, prof()) is None
    assert resolve_field({**optin, "value": "", "required": True}, prof()) is None
    plan = plan_autofill([optin, f(_KFORCE_CELL, id="2")], prof(), {"company": "Kforce"})
    assert [item["id"] for item in plan["to_fill"]] == ["2"], plan
    for sign_up in ("Mobile Number Sign up for job alerts by text message",
                    "Cell Phone Number Send me text messages about similar jobs and other openings at the company",
                    "Cellular Number By providing it you agree to receive recurring marketing text messages from us",
                    "Mobile Number Opt in to our newsletter and promotions by text message",
                    "Cell Number I consent to receive automated text messages (SMS) from the employer"):
        assert resolve_field(f(sign_up), prof()) is None, sign_up


def test_a_forms_one_phone_box_gets_the_number_whatever_kind_it_names():
    """A form whose only phone box is of another kind than the profile's ("Home Phone *" alone, for a
    mobile) wants the number there: it's filled, as before boxes had kinds. With a box of the profile's
    kind or a plain one beside it, the other kinds stay the person's."""
    alone = plan_autofill([f("Home Phone *", "text", required=True)], prof(), {})
    assert [(x["label"], x["rule"]) for x in alone["to_fill"]] == [("Home Phone *", "phone")], alone
    both = plan_autofill([f("Home Phone *", "text", required=True), {**f("Cell Phone", "text"), "id": "2"}], prof(), {})
    assert [x["label"] for x in both["to_fill"]] == ["Cell Phone"], both
    # of two boxes of other kinds, the one the form requires
    two = plan_autofill([f("Home Phone", "text", required=True), {**f("Work Phone", "text"), "id": "2"}], prof(), {})
    assert [x["label"] for x in two["to_fill"]] == ["Home Phone"], two
    # never someone else's box, nor one that signs the person up for texts
    for label in ("Emergency Contact Home Phone", "Home Phone Number - text me job alerts and similar jobs"):
        assert plan_autofill([f(label, "text", required=True)], prof(), {})["to_fill"] == [], label


def test_an_expected_graduation_date_is_never_a_past_year():
    """American Express (live, Oct 2026) asks "What is your expected graduation date:" with
    "I am currently not attending school" among its choices, and the desk typed 2020, the
    year the profile's school ended, as if it were still to come."""
    import yaml
    from job_apply import config

    choices = ["April - June 2026", "April - June 2027", "I am currently not attending school", "January - March 2027"]
    asked = f("What is your expected graduation date:", "combobox", options=choices)
    assert resolve_field(asked, prof()).value == "I am currently not attending school"
    assert resolve_field(f("Graduation Date"), prof()).value == 2020  # (a finished school's own date)

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    school = {**data["education_history"][0], "end": "present"}
    path.write_text(yaml.safe_dump({**data, "education_history": [school]}))
    assert resolve_field(asked, prof()) is None  # still at school, no year: the person says

    from datetime import date

    today = date.today()
    ended = f"{today.year}-01" if today.month > 1 else f"{today.year - 1}-12"  # earlier this year: done
    path.write_text(yaml.safe_dump({**data, "education_history": [{**school, "end": ended}]}))
    assert resolve_field(asked, prof()).value == "I am currently not attending school"
    path.write_text(yaml.safe_dump({**data, "education_history": [{**school, "end": f"{today.year + 1}-05"}]}))
    assert resolve_field(f("Expected graduation date"), prof()).value == str(today.year + 1)


def test_the_same_place_spelled_another_way():
    """A state or place list that spells the state the other way, or adds the country, found
    nothing: "AZ" wasn't "Arizona, United States" nor "US-AZ", "Phoenix, AZ" wasn't "Phoenix,
    Arizona". Never another place: "Arizona City" isn't Arizona."""
    assert choose_option("AZ", ["Arizona, United States", "Arkansas, United States"]) == "Arizona, United States"
    assert choose_option("Arizona", ["US-AK", "US-AZ", "US-AR"]) == "US-AZ"
    assert choose_option("Phoenix, AZ", ["Phoenix, Arizona", "Tempe, Arizona"]) == "Phoenix, Arizona"
    assert choose_option("AZ", ["Arizona City", "Arizona"]) == "Arizona"


def test_where_the_person_lives_from_the_address():
    """"Do you currently live in Arizona?" was asked though the profile's address says so. A
    state is the address's; a city the address's city or, asked about its area, any town in
    its metro area. A distance, or a city that isn't theirs, is the person's to say."""
    yes_no = ["Yes", "No"]

    def answer(label, profile=None):
        a = resolve_field(f(label, "radio_group", options=yes_no), profile or prof())  # Chandler, AZ
        return a and a.value

    assert answer("Do you currently live in Arizona?") == "Yes"
    assert answer("Do you live in AZ?") == "Yes"
    assert answer("Do you live in Texas?") == "No"
    assert answer("Are you located in the Phoenix metro area?") == "Yes"
    assert answer("Do you live in the Tucson area?") == "No"
    assert answer("Do you reside in Chandler?") == "Yes"
    assert answer("Do you live in the United States?") == "Yes"
    assert answer("Do you live in Phoenix?") is None  # Chandler isn't Phoenix, though it's in its area
    assert answer("Do you live within 30 miles of Tempe?") is None


def test_willing_to_travel_isnt_willing_to_travel_abroad():
    """A profile willing to travel "up to 75%" said Yes to "Are you willing to travel
    internationally?", which it doesn't say."""
    yes_no = ["Yes", "No"]
    assert resolve_field(f("Are you willing to travel internationally?", "radio_group", options=yes_no), prof()) is None
    assert resolve_field(f("Are you willing to travel outside the United States?", "radio_group", options=yes_no),
                         prof()) is None
    assert resolve_field(f("Are you willing to travel?", "radio_group", options=yes_no), prof()).value == "Yes"


def test_currently_employed_from_the_work_history():
    """"Are you currently employed?" was asked at every employer (an answer given at one is
    about that one): the work history says, by an entry that hasn't ended. "...by Intel?"
    asks about one employer, and is answered as such."""
    import yaml
    from job_apply import config

    yes_no = ["Yes", "No"]
    assert resolve_field(f("Are you currently employed?", "radio_group", options=yes_no), prof()).value == "Yes"
    assert resolve_field(f("Currently employed?", "select", options=yes_no), prof()).value == "Yes"
    by_acme = resolve_field(f("Are you currently employed by Acme?", "radio_group", options=yes_no), prof(),
                            {"company": "Acme"})
    assert by_acme is None or by_acme.rule != "employed_now"
    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    ended = [{**job, "end": "2024-06"} for job in data["work_history"]]
    path.write_text(yaml.safe_dump({**data, "work_history": ended}))
    assert resolve_field(f("Are you currently employed?", "radio_group", options=yes_no), prof()).value == "No"


def test_a_veteran_question_and_a_family_members_are_answered_as_asked():
    """"I am not a protected veteran" answered "Are you a veteran?" No, though a veteran who
    isn't a protected one is a veteran; and the person's own answers went to "Are you the
    spouse of a veteran?" and "Gender of your spouse"."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**data, "eeo": {**data["eeo"], "veteran": "I am not a protected veteran",
                                                    "gender": "Male"}}))
    yes_no = ["Yes", "No"]
    assert resolve_field(f("Are you a veteran?", "radio_group", options=yes_no), prof()) is None
    assert resolve_field(f("Are you the spouse of a veteran?", "radio_group", options=yes_no), prof()) is None
    assert resolve_field(f("Gender of your spouse", "select", options=["Male", "Female"]), prof()) is None
    # the questions as asked of the person still get their answers
    status = ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
              "I don't wish to answer"]
    assert resolve_field(f("Veteran Status", "select", options=status), prof()).value == "I am not a protected veteran"
    assert resolve_field(f("Are you a protected veteran?", "radio_group", options=yes_no), prof()).value == "No"
    assert resolve_field(f("Gender", "select", options=["Male", "Female"]), prof()).value == "Male"
    # a family word in passing doesn't make it someone else's question
    assert resolve_field(f("We are an equal opportunity employer and partner with veterans. Veteran Status", "select",
                           options=status), prof()).value == "I am not a protected veteran"


def test_a_citizen_is_a_us_person_and_eligibility_is_authorization():
    """"Are you a U.S. citizen or permanent resident?" waited on a us_person answer that a
    citizen's profile needn't give; "Can you provide proof of eligibility to work in the US?"
    and "...legally eligible for employment..." weren't read as work authorization; and
    "Race of your household members" got the person's own race."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    auth = {k: v for k, v in data["work_authorization"].items() if k != "us_person"}
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**auth, "us_citizen": True},
                                    "eeo": {**data["eeo"], "race": "White"}}))
    yes_no = ["Yes", "No"]

    def answer(label, options=yes_no):
        a = resolve_field(f(label, "radio_group", options=options), prof())
        return a and a.value

    assert answer("Are you a U.S. citizen or permanent resident?") == "Yes"
    assert answer("Are you a US citizen, permanent resident, refugee or asylee?") == "Yes"
    assert answer("Can you provide proof of eligibility to work in the US upon hire?") == "Yes"
    assert answer("Are you legally eligible for employment in the United States?") == "Yes"
    assert answer("Race of your household members", ["White", "Black", "Asian"]) is None


def test_a_degree_the_person_lacks_is_answered_no():
    """"Do you have a Bachelor's degree?" was always asked, though a profile whose highest
    education is "Some college" says the answer. It's No when the highest education stated is
    below the level asked and no school lists a degree that isn't. Never Yes: which degrees a
    person holds is theirs to say (a degree under way or written another way can't be told
    from one earned), and a field, a kind of degree or a condition is theirs too."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())

    def with_education(highest, history):
        path.write_text(yaml.safe_dump({**data, "education": {"highest_degree": highest}, "education_history": history}))
        return Profile.load()

    def answer(label, profile, kind="radio_group"):
        a = resolve_field(f(label, kind, options=["Yes", "No"] if kind != "text" else None), profile)
        return a and a.value

    some_college = with_education("Some college", [{"school": "Example Community College", "degree": "", "end": 2019}])
    assert answer("Do you have a Bachelor's degree?", some_college) == "No"
    assert answer("Do you have a Bachelor's degree or higher (Master\u2019s degree, Ph.D., etc.)? *", some_college) == "No"
    assert answer("Do you have an Associate's degree or higher?", some_college) == "No"
    assert answer("Master's degree preferred", some_college) == "No"
    assert answer("Do you have a high school diploma or GED?", some_college) is None  # not said
    for asked in ("Bachelor's Degree in Accounting or Finance Required", "Do you have a Bachelor of Science degree?",
                  "Do you have a Bachelor's degree from an accredited university?", "Bachelor's degree or equivalent experience"):
        assert answer(asked, some_college) is None, asked
    associate = with_education("Associate's Degree", [])
    assert answer("Do you have a Bachelor's degree?", associate) == "No"
    assert answer("Do you have an Associate's degree?", associate) is None  # never Yes
    # a school listing a degree it can't place, or one at the level asked, stops the No
    for history in ([{"degree": "Bachelor's Degree"}], [{"degree": "BSN", "end": 2019}],
                    [{"degree": "BS", "status": "Graduated (honors)", "end": 2019}]):
        assert answer("Do you have a Bachelor's degree?", with_education("Associate's Degree", history)) is None, history
    # a highest degree written any other way isn't read
    for highest in ("Bachelor's Degree (BS)", "Bachelor's (not finished)", "PhD ABD", "Bachelor's Degree Equivalent"):
        assert answer("Do you have a Master's degree?", with_education(highest, [])) is None, highest
    # a degree's details aren't the question
    assert answer("Bachelor's Degree Major", some_college, "text") is None
    # only a plain "No", with the choices shown: never one that says more than the profile
    for choices in (["Yes", "No, but I have equivalent work experience"], ["Yes", "No, but I am currently enrolled"], []):
        a = resolve_field(f("Do you have a Bachelor's degree?", "combobox", options=choices), some_college)
        assert a is None, choices
    # a school whose entry doesn't say its degree stops the No
    unsaid = with_education("Associate's Degree", [{"school": "Example State University", "major": "Finance", "end": 2020}])
    assert answer("Do you have a Bachelor's degree?", unsaid) is None
    assert answer("Do you have a Bachelor's degree?", with_education("College coursework, no degree", [])) == "No"
    assert answer("Do you have a high school diploma?", with_education("Some high school", [])) == "No"
    for highest in ("Graduate coursework", "Attended university", "Currently enrolled in university"):
        assert answer("Do you have a Bachelor's degree?", with_education(highest, [])) is None, highest
    # a section about education and experience doesn't make it an equivalence
    a = resolve_field(f("Do you have a Bachelor's degree?", "radio_group", options=["Yes", "No"],
                        section="Education and Experience"), some_college)
    assert a.value == "No"


def test_a_confirmed_degree_answers_yes():
    """Setup records each degree the person confirmed they finished in education.degrees_earned
    ({level, field}); only those answer "Do you have a Bachelor's degree?" Yes: one at that
    level, or above it when the question says "or higher". A field the question names, a
    kind of degree or a condition stays the person's, as does any entry it can't read; a
    GED answers for a GED only. Free-text education never answers Yes."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())

    def with_degrees(degrees, highest="Bachelor's Degree", history=None):
        path.write_text(yaml.safe_dump({**data, "education": {"highest_degree": highest, "degrees_earned": degrees},
                                        "education_history": history or []}))
        return Profile.load()

    def answer(label, profile, options=("Yes", "No")):
        a = resolve_field(f(label, "radio_group", options=list(options)), profile)
        return a and a.value

    finance = with_degrees([{"level": "bachelor", "field": "Finance"}])
    assert answer("Do you have a Bachelor's degree?", finance) == "Yes"
    assert answer("Bachelor's degree required", finance) == "Yes"
    assert answer("Do you have an Associate's degree or higher?", finance) == "Yes"
    assert answer("Do you have an Associate's degree?", finance) is None  # not "or higher"
    # "or higher" with examples of what's higher (Carvana, Oct 2026) is the same question
    assert answer("Do you have a Bachelor's degree or higher (Master\u2019s degree, Ph.D., etc.)? *", finance) == "Yes"
    assert answer("Do you have a Bachelor's degree or higher (Master's degree, Ph.D., etc.)? (Required)", finance) == "Yes"
    assert answer("Do you have an Associate's degree or higher (e.g., Bachelor's, Master's)?", finance) == "Yes"
    assert answer("Do you have an Associate's degree or higher (i.e. Bachelor degree or M.B.A.)?", finance) == "Yes"
    assert answer("Do you have a Master's degree or higher (Ph.D., Ed.D., Doctoral degree, etc.)?", finance) == "No"
    # ...but not when what's named isn't a degree above the one asked: then it asks something else
    for asked in ("Do you have a Bachelor's degree or higher (in Finance or Accounting)?",
                  "Do you have an Associate's degree or higher (Bachelor's preferred)?",
                  "Do you have a Master's degree or higher (Bachelor's)?",
                  "Do you have a Master's degree or higher (Bachelor's and Master's)?",
                  "Do you have a Master's degree or higher (MD, JD)?"):
        assert answer(asked, finance) is None, asked
    engineer = with_degrees([{"level": "master", "field": "Engineering"}], "Master's Degree")
    for asked in ("Do you have a Master's degree or higher (MBA)?", "Do you have a Master's degree or higher (M.B.A.)?"):
        assert answer(asked, engineer) is None, asked
    assert answer("Do you have a Bachelor's degree or higher (MBA, Ph.D.)?", engineer) == "Yes"
    assert answer("Do you have a Master's degree?", finance) == "No"  # the most finished is a bachelor's
    for asked in ("Bachelor's Degree in Accounting or Finance Required", "Bachelor's degree in Finance",
                  "Bachelor's degree in Nursing, CCRN, or CNOR", "Do you have a Bachelor's degree with honors?",
                  "Do you have a Bachelor of Science degree?", "Bachelor's degree or equivalent experience",
                  "Do you have a Bachelor's degree from an accredited university?"):
        assert answer(asked, finance) is None, asked
    # only a plain "Yes"
    assert answer("Do you have a Bachelor's degree?", finance, ("Yes, in a related field", "No")) is None
    # a GED answers for a GED, not a diploma; a diploma confirmed gives the No above it
    ged = with_degrees([{"level": "ged"}], "GED")
    assert answer("Do you have a high school diploma?", ged) is None
    assert answer("Do you have a GED?", ged) == "Yes"
    assert answer("Do you have a high school diploma or GED?", ged) == "Yes"
    assert answer("Do you have a Bachelor's degree?", ged) == "No"
    assert answer("Do you have a high school diploma?", with_degrees([{"level": "high_school", "field": "GED"}], "GED")) is None
    diploma = with_degrees([{"level": "high_school"}], "High school diploma", [{"degree": "High school diploma", "end": 2015}])
    assert answer("Do you have a high school diploma?", diploma) == "Yes"
    assert answer("Do you have a Bachelor's degree?", diploma) == "No"
    # anything it can't read: a level, a status, a field saying it isn't done or is something else
    for degrees in ([{"level": "Bachelor's (in progress)"}], [{"level": "MA"}], [{"level": "BS"}],
                    [{"level": "bachelor", "field": "Finance", "status": "in progress"}],
                    [{"level": "bachelor", "field": "Finance (expected 2027)"}],
                    [{"level": "bachelor", "field": "Finance coursework"}], [{"level": "bachelor", "field": "Finance - ABD"}],
                    [{"level": "bachelor", "field": "Finance - withdrew"}]):
        assert answer("Do you have a Bachelor's degree?", with_degrees(degrees, "Some college")) is None, degrees
    assert answer("Do you have a doctorate?", with_degrees([{"level": "doctorate", "field": "Juris Doctor"}])) is None
    assert answer("Do you have an Associate's degree?",
                  with_degrees([{"level": "associate", "field": "Welding Certificate"}], "Some college")) is None
    for equivalent in ("General Educational Development", "HiSET"):
        assert answer("Do you have a high school diploma?",
                      with_degrees([{"level": "high_school", "field": equivalent}], "Some college")) is None, equivalent
    assert answer("Do you have a high school diploma?", with_degrees([{"level": "high_school"}], "GED")) is None
    assert answer("Do you have a GED?", with_degrees([{"level": "ged", "field": "GED"}], "GED")) == "Yes"
    assert answer("Do you have a doctorate?", with_degrees([{"level": "doctorate", "field": "Doctor of Medicine"}])) is None
    assert answer("Do you have a Bachelor's degree?",
                  with_degrees([{"level": "bachelor", "field": "Finance (Spring '27)"}], "Some college")) is None
    assert answer("Do you have a high school diploma?", with_degrees([{"level": "high_school"}], "HiSET")) is None
    # ...but a past year, a combined highest, or a field that only sounds like one is read
    diploma_or_ged = with_degrees([{"level": "high_school"}], "High School Diploma or GED")
    assert answer("Do you have a high school diploma?", diploma_or_ged) == "Yes"
    assert answer("Do you have a Bachelor's degree?",
                  with_degrees([{"level": "bachelor", "field": "Computer Science, Class of 2015"}])) == "Yes"
    assert answer("Do you have an Associate's degree?",
                  with_degrees([{"level": "associate", "field": "General Education"}], "Associate's Degree")) == "Yes"
    assert answer("Do you have a Master's degree?",
                  with_degrees([{"level": "master", "field": "Teaching Certification"}], "Master's Degree")) == "Yes"
    # what is read: a degree's name as the level, a field as a diploma names it, a lone entry
    assert answer("Do you have a Bachelor's degree?", with_degrees([{"level": "Bachelor's", "field": "Finance"}])) == "Yes"
    assert answer("Do you have a Bachelor's degree?",
                  with_degrees([{"level": "bachelor", "field": "Business Administration (Finance)"}])) == "Yes"
    assert answer("Do you have a Bachelor's degree?", with_degrees({"level": "bachelor", "field": "Finance"})) == "Yes"
    # one it can't read doesn't stop a Yes from one it can, but stops a No
    mixed = with_degrees([{"level": "bachelor", "field": "Finance"}, {"level": "certificate"}])
    assert answer("Do you have a Bachelor's degree?", mixed) == "Yes"
    assert answer("Do you have a Master's degree?", mixed) is None
    # no highest education stated, or one it can't read: no No
    assert answer("Do you have a Bachelor's degree?", with_degrees([{"level": "high_school"}], "")) is None
    assert answer("Do you have a Master's degree?", with_degrees([{"level": "bachelor"}], "MBA")) is None
    # without confirmed degrees, a free-text one never answers Yes
    free_text = with_degrees([], "Bachelor's Degree", [{"degree": "Bachelor's Degree", "major": "Finance", "end": 2020}])
    assert answer("Do you have a Bachelor's degree?", free_text) is None


def test_a_citizens_visa_questions():
    """A U.S. citizen holds no F-1, H-1B or TN visa ("Are you currently on an F-1 visa
    (OPT/CPT)?" was asked), and their visa or citizenship status is the citizen choice. Only
    a citizen's: a past visa ("Have you ever held...") or anyone else's status is theirs to say."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**data["work_authorization"], "us_citizen": True}}))
    yes_no = ["Yes", "No"]

    def answer(label, kind="radio_group", options=yes_no):
        a = resolve_field(f(label, kind, options=options), prof())
        return a and a.value

    assert answer("Are you currently on an F-1 visa (OPT/CPT)?") == "No"
    assert answer("Do you currently hold an H-1B visa?") == "No"
    assert answer("Are you currently in the U.S. on a TN visa?") == "No"
    assert answer("Have you ever held an H-1B visa?") is None
    statuses = ["U.S. Citizen", "Permanent Resident", "H-1B", "F-1 OPT", "Other"]
    assert answer("What is your current visa status?", "select", statuses) == "U.S. Citizen"
    assert answer("Immigration status", "select", ["Citizen of the United States", "Non-US Citizen", "Other"]) == \
        "Citizen of the United States"
    # ASM's export-control question (live check, Oct 11): which of these applies to you
    ear = ("ASM may work with technologies subject to U.S. export control regulations. The following questions are "
           "being asked to comply with the regulation. You may read more about this here: U.S. Export Administration "
           "Regulation (EAR) 15 C.F.R. \u00a7 730 -774. Please, select which option applies to you.*")
    ear_options = ["Alien Authorized to Work", "Citizen or National of the United States",
                   "U.S. Lawful Permanent Resident (Greencard holder)", "Person granted Refugee status in the United States",
                   "None of the above"]
    assert answer(ear, "combobox", ear_options) == "Citizen or National of the United States"
    # the same words before an alias (review), and a question about the role isn't the person's status
    assert choose_option("U.S. Citizen", ["U.S. Citizen or National", "U.S. Citizen", "Non-US Citizen"]) == "U.S. Citizen"
    assert answer("Is this role subject to export control regulations? Select the option that applies", "select",
                  ["U.S. Citizen", "Not subject"]) is None
    # "Citizenship status" is still the citizenship rule's, from the profile's country
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**data["work_authorization"], "us_citizen": True,
                                                                   "citizenship": "United States"}}))
    assert answer("Citizenship status", "select", ["United States", "Other"]) == "United States"
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**data["work_authorization"], "us_citizen": False}}))
    assert answer("What is your current visa status?", "select", statuses) is None
    assert answer("Do you currently hold an H-1B visa?") is None
    assert answer(ear, "combobox", ear_options) is None


def test_a_drivers_license_question():
    """"Do you have a valid driver's license?" (Southwest Gas asks it, and field jobs do) is
    the profile's yes or no. Its record, a commercial license, or a license asked together
    with something else stays the person's."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**data, "personal": {**data["personal"], "drivers_license": True}}))

    def answer(label, kind="radio_group"):
        a = resolve_field(f(label, kind, options=["Yes", "No"]), prof())
        return a and a.value

    assert answer("Do you have a valid driver’s license?*") == "Yes"
    assert answer("Do you possess a current, valid drivers license?") == "Yes"
    assert answer("Do you have a valid driver's license issued in the United States?", "select") == "Yes"
    assert answer("Has your driver’s license been revoked or suspended in the past two years?") is None
    assert answer("Do you have a valid commercial driver's license (CDL)?") is None
    assert answer("Do you have a valid driver's license and reliable transportation?") is None
    assert answer("Driver's License Number", "text") is None
    path.write_text(yaml.safe_dump({**data, "personal": {**data["personal"], "drivers_license": False}}))
    assert answer("Do you have a valid driver’s license?") == "No"


def test_more_questions_the_profile_answers():
    """Questions from the live runs the profile already answers: where the person lives (Axon),
    sponsorship "to maintain your work authorization" (Grant Thornton), a citizen isn't an
    alien (Axon), when they can start (Micron, GoDaddy), work experience at all (Southwest Gas),
    and a trailing "(Yes/No)"."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**data["work_authorization"], "us_citizen": True},
                                    "personal": {**data["personal"], "drivers_license": True},
                                    "preferences": {**data["preferences"], "earliest_start": "2 weeks after offer"}}))

    def answer(label, kind="radio_group"):
        a = resolve_field(f(label, kind, **({"options": ["Yes", "No"]} if kind != "text" else {})), prof())
        return a and a.value

    assert answer("What city do you currently reside in?*", "text") == "Chandler"
    assert answer("Where are you currently located?*", "text") == "Chandler, AZ"
    # a hub list or a question about coming in isn't the address
    assert answer("Where are you currently located?") is None
    assert answer("Where are you currently located? This role is onsite 4 days/week at one of our hubs.", "text") is None
    assert answer("Will you require visa sponsorship, from Grant Thornton or otherwise, now or in the future to maintain "
                  "your work authorization?") == "No"
    # both halves in one question stay the person's
    assert answer("Are you authorized to work in the U.S., or will you require sponsorship to obtain work "
                  "authorization?") is None
    assert answer("Are you an alien illegally or unlawfully in the United States?*") == "No"
    assert answer("Are you an alien who has been admitted to the United States under a nonimmigrant visa? "
                  "(i.e. H-1B, TN, F-1)") == "No"
    assert answer("When would you be available if an offer was accepted?", "text") == "2 weeks after offer"
    assert answer("What is the soonest you would be able to start?*", "text") == "2 weeks after offer"
    assert answer("When would you be available for an interview?", "text") is None
    assert answer("What is the soonest you would be available for a phone screen?", "text") is None
    assert answer("Do you have previous work experience?") == "Yes"
    assert answer("Do you have a valid driver's license? (Yes/No)") == "Yes"
    assert answer("Do you have a valid driver's license? (Y/N)*") == "Yes"
    path.write_text(yaml.safe_dump({**data, "work_history": []}))
    assert answer("Do you have previous work experience?") is None
    assert answer("Are you an alien illegally or unlawfully in the United States?*") is None


def test_questions_from_the_finance_run():
    """GoDaddy's long LinkedIn label and its "(Please note, ...)" aside, Grant Thornton's
    "Preferred Last Name", GoDaddy's LGBTQ self-identification and Axon's I-9 question."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**data, "eeo": {**data["eeo"], "sexual_orientation": "Decline to self-identify"},
                                    "preferences": {**data["preferences"], "desired_salary": "$85,000"}}))

    def answer(label, kind="combobox", options=("Yes", "No")):
        a = resolve_field(f(label, kind, **({"options": list(options)} if kind != "text" else {})), prof())
        return a and a.value

    assert answer("LinkedIn profile (if you do not have one or if you prefer not to provide one, enter N/A):*", "text") == \
        "https://www.linkedin.com/in/samrivera"
    assert answer("Do you currently reside in the United States? (Please note, your answer to this question and the "
                  "related sub-questions, as applicable, will be used to determine your eligibility.)") == "Yes"
    # a note that isn't an aside after the question stays part of it
    assert answer("Desired pay (note: hourly rate)", "text") is None
    assert answer("Preferred Last Name", "text") == "Rivera"
    assert answer("Can you provide verification of both your identity and authorization to work in the United States, "
                  "to the extent required by law?*") == "Yes"
    assert answer("Can you provide verification of authorization to work in a country other than the United States?") is None
    assert answer("Do you consider yourself a member of the Lesbian, Gay, or Bisexual (LGBTQ) community?*",
                  options=("Yes", "No", "I don't wish to answer")) == "I don't wish to answer"
    # a family member's isn't the person's
    assert answer("Is your spouse a member of the LGBTQ community?", options=("Yes", "No", "I don't wish to answer")) is None


def test_some_college_is_post_secondary_with_no_degree():
    """Knight-Swift's Education list (live, Oct 2026) says "PS-POST SECONDARY-NO DEGREE" for some
    college; "NA-NO POST SECONDARY EDU" and the degrees are never it."""
    options = ["Select an option...", "AA-AA DEGREE", "BA-BACHELOR OF ARTS", "BS-BACHELOR OF SCIENCE",
               "MA-MASTER OF ARTS", "PS-POST SECONDARY-NO DEGREE", "TRADE-TRADE SCHOOL GRADUATE",
               "NA-NO POST SECONDARY EDU"]
    assert choose_option("Some college", options) == "PS-POST SECONDARY-NO DEGREE"
    assert choose_option("Some college", [o for o in options if not o.startswith("PS")]) is None


def test_where_you_are_and_when_you_can_start_among_choices():
    """Axon's "Where are you currently located?" lists countries; Insight's "When are you
    available to start?" lists waits ("Immediate", "2 Week Notice", "30 Days")."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "earliest_start": "2 weeks after offer"}}))

    def answer(label, options, kind="combobox"):
        a = resolve_field(f(label, kind, options=options), prof())
        return a and a.value

    countries = ["Australia", "Canada", "Netherlands", "United Kingdom", "United States", "Other"]
    assert answer("Where are you currently located?*", countries) == "United States"
    assert answer("Where are you currently located?", ["Phoenix hub", "Seattle hub", "Remote"]) is None
    waits = ["Immediate", "2 Week Notice", "30 Days", "60 Days", "90 Days"]
    assert answer("When are you available to start?", waits) == "2 Week Notice"
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "earliest_start": "1 month"}}))
    assert answer("When are you available to start?", waits) == "30 Days"
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "earliest_start": "Immediately"}}))
    assert answer("When are you available to start?", waits) == "Immediate"
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "earliest_start": "3 weeks"}}))
    assert answer("When are you available to start?", waits) is None  # never a sooner or later start
    # a range isn't one wait: "2-4 Weeks" is no answer for a month
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "earliest_start": "1 month"}}))
    assert answer("When are you available to start?", ["Immediate", "2-4 Weeks", "60 Days"]) is None
    assert answer("When are you available to start?", ["Immediate", "1 to 2 months", "30 Days"]) == "30 Days"


def test_a_questions_own_decline_box_is_ticked_for_a_profile_that_declines():
    """Nikon's UKG Pro (live run 17, Oct 11): "Gender" (Choose..., Male, Female), required, with an "I decline to
    say Gender" box beneath it, and the same for Ethnic Origin and protected veteran. The desk asked all three,
    though the profile declines them. The box is ticked, and its question isn't asked; a profile with an answer
    leaves the box alone."""
    fields = [
        {"id": "g", "kind": "select", "label": "Gender", "required": True, "value": "",
         "options": ["Choose...", "Male", "Female"]},
        {"id": "gd", "kind": "checkbox", "label": "I decline to say Gender", "value": False},
        {"id": "v", "kind": "select", "label": "Are you a protected veteran?", "required": True, "value": "",
         "options": ["Choose...", "Yes", "No"]},
        {"id": "vd", "kind": "checkbox", "label": "I decline to say Are you a protected veteran?", "value": False},
    ]
    declines = Profile({"eeo": {"gender": "Decline to self-identify", "veteran": "I don't wish to answer"}})
    plan = plan_autofill(fields, declines)
    assert {t["id"]: t["value"] for t in plan["to_fill"]} == {"gd": True, "vd": True}
    assert not [q for q in plan["needs_input"] if q["id"] in ("g", "v")], plan["needs_input"]
    answers = Profile({"eeo": {"gender": "Female", "veteran": "I am not a protected veteran"}})
    plan = plan_autofill(fields, answers)
    filled = {t["id"]: t["value"] for t in plan["to_fill"]}
    assert "gd" not in filled and "vd" not in filled and filled.get("g") == "Female" and filled.get("v") == "No"
    # nor is any box ticked for a profile with no self-identification answers
    assert not plan_autofill(fields, Profile({}))["to_fill"]
    # a decline box for something that isn't a self-identification question is left alone
    other = [{"id": "p", "kind": "checkbox", "label": "I decline to say my salary expectations", "value": False}]
    assert not plan_autofill(other, declines)["to_fill"]


@pytest.mark.parametrize("disability", ["No", "None", "No disability", "I don't have a disability", "I do not have a disability"])
def test_a_profile_without_a_disability_picks_the_no_choice(disability):
    """Note 18 (live test 2, issue #112): a profile without a disability got Workday's "I do not want to answer".
    However the profile words it ("None" was read as no answer), the form's "No" choice is picked, never its
    decline."""
    options = ["Yes, I have a disability, or have had one in the past",
               "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
    field = {"kind": "checkbox_group", "label": "Please check one of the boxes below:*",
             "section": "Voluntary Self-Identification of Disability", "options": options}
    ans = resolve_field(field, Profile({"eeo": {"disability": disability}}))
    assert ans is not None and ans.value == options[1], ans


@pytest.mark.parametrize("gender, expected", [("M", "Male"), ("F", "Female"), ("Man", "Male"), ("Woman", "Female")])
def test_a_profiles_gender_however_short_picks_its_choice(gender, expected):
    """Note 18: gender left empty on one site. A profile's "M" or "F" is the list's Male or Female."""
    field = {"kind": "listbox", "label": "Please select your gender.",
             "options": ["Male", "Female", "I do not wish to self-identify"]}
    ans = resolve_field(field, Profile({"eeo": {"gender": gender}}))
    assert ans is not None and ans.value == expected, ans


def test_where_the_person_lives_and_their_work_authorization_asked_as_questions():
    """Grand Canyon University's Workday (the full run of Oct 12) asked, and the desk passed to the person: "Do you
    have authorization to work in the U.S.?", and in text areas "In what city do you live?" and "In what state do
    you live? (Please use the state abbreviation format, i.e. AZ)". The profile answers all three; an essay box that
    only mentions a town isn't one of them."""
    p = Profile({"personal": {"address": {"city": "Chandler", "state": "Arizona"}},
                 "work_authorization": {"authorized_to_work": True, "requires_sponsorship": False}})
    def ask(label, kind="textarea", options=None):
        a = resolve_field({"label": label, "kind": kind, "required": True, **({"options": options} if options else {})}, p)
        return a and a.value
    assert ask("Do you have authorization to work in the U.S.?*", "listbox", ["Select One", "Yes", "No"]) == "Yes"
    assert ask("Will you now or in the future require sponsorship to obtain authorization to work in the U.S.?",
               "listbox", ["Yes", "No"]) == "No"
    assert ask("In what city do you live?*") == "Chandler"
    assert ask("In what state do you live? (Please use the state abbreviation format, i.e. AZ)*") == "AZ"
    assert ask("Describe a town you would like to visit") is None
    assert ask("Which state do you live in, and why do you want to relocate?") is None  # (an essay: the review's)


def test_a_degree_still_to_come_is_never_filled_as_one():
    """The safety audit (Oct 12): a profile's "BS Electrical Engineering (expected 2027)" picked "Bachelor's Degree" for
    the highest level of education, as did "graduating", "anticipated" and "candidate". A degree not yet earned is
    the person's to say; one earned is filled as before."""
    options = ["High School", "Associate's Degree", "Bachelor's Degree", "Master's Degree"]
    for degree in ("BS Electrical Engineering (expected 2027)", "Bachelor of Science, graduating 2027",
                   "BS (anticipated 2026)", "BS candidate", "Bachelor's degree pending"):
        assert choose_option(degree, options) is None, degree
        p = Profile({"education": {"highest_degree": degree}})
        assert resolve_field({"label": "Highest level of education", "kind": "select", "options": options,
                              "required": True}, p) is None, degree
    assert choose_option("Bachelor of Science", options) == "Bachelor's Degree"
