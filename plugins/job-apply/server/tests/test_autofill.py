from job_apply.autofill import (
    choose_option,
    choose_place,
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
    # "Citizenship status" is still the citizenship rule's, from the profile's country
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**data["work_authorization"], "us_citizen": True,
                                                                   "citizenship": "United States"}}))
    assert answer("Citizenship status", "select", ["United States", "Other"]) == "United States"
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**data["work_authorization"], "us_citizen": False}}))
    assert answer("What is your current visa status?", "select", statuses) is None
    assert answer("Do you currently hold an H-1B visa?") is None


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
