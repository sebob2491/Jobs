import pytest

from job_apply.autofill import (
    choose_option,
    clean_label,
    employer_aliases,
    choose_place,
    is_decline,
    is_empty_value,
    place_words,
    plan_autofill,
    polarity,
    resolve_field,
    same_country,
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
    assert (choose_option("Decline to self-identify", ["Male", "Female", "I don't wish to answer"])
            == "I don't wish to answer")
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


@pytest.mark.parametrize("want", ["Zebra Unknown Thing", "Bachelor's Degree", "10", "Yes", "Decline to self-identify",
                                  "Arizona", "Phoenix, AZ"])
def test_a_long_list_is_read_a_few_times_a_choice_not_once_a_state(monkeypatch, want):
    """A 241-choice list (a school or a country list) took 100 ms an answer: each choice was put through norm() again
    for each of the 52 states' names (the review of Oct 15). Now a few times a choice, whatever the answer."""
    import random
    from job_apply import autofill
    words = "north south east west river valley mountain state city county technical institute college lake".split()
    rng = random.Random(1)
    options = [" ".join(rng.choice(words).title() for _ in range(3)) + f" {i}" for i in range(241)]
    calls = []
    real = autofill.norm

    def counted(s):
        calls.append(1)
        return real(s)
    monkeypatch.setattr(autofill, "norm", counted)
    choose_option(want, options)
    assert len(calls) < 12 * len(options)


def test_planning_a_long_form_stays_quick(monkeypatch):
    """A timing guard for plan_autofill (the review of Oct 15): a form of 61 questions, six of them 241-entry lists,
    planned from a full profile. It took 135 ms on 0.3.189, nearly all of it norm() over the lists' choices (29 a
    choice); now about one a choice. The count is the guard (a clock is no guard on a slow runner); the clock only
    catches a planning gone quadratic."""
    import random
    import time
    from job_apply import autofill
    from job_apply.autofill import US_STATES, plan_autofill
    rng = random.Random(7)
    words = "north south east west river valley mountain state city county technical institute college lake".split()
    schools = [" ".join(rng.choice(words).title() for _ in range(3)) + f" {i}" for i in range(241)]
    fields: list[dict] = []

    def add(label, kind="text", options=None):
        fields.append({"id": f"f{len(fields)}", "label": label, "kind": kind, "value": "", "required": True,
                       "options": options or []})
    for label in ("First Name", "Last Name", "Email", "Phone", "Address Line 1", "City", "Postal Code", "LinkedIn"):
        add(label)
    add("State", "select", list(US_STATES.values()))
    add("Country", "select", ["United States of America", "Canada", "Mexico"] + [f"Country {i}" for i in range(200)])
    for label in ("School", "Previous school 1", "Previous school 2", "Previous school 3", "Previous school 4",
                  "College or university"):
        add(label, "select", schools)
    for q in ("Are you legally authorized to work in the United States?", "Gender", "Race/Ethnicity", "Veteran Status",
              "Disability Status", "How did you hear about us?", "Are you willing to relocate?",
              "Are you at least 18 years of age?", "Will you now or in the future require sponsorship?",
              "Are you willing to travel?", "Do you have a valid driver's license?", "Can you work shifts?",
              "Have you worked for us before?", "Highest level of education", "Earliest start date"):
        add(q, "select", ["Select One", "Yes", "No", "Decline to self-identify", "I don't wish to answer"]
            + [f"Option {k}" for k in range(20)])
    for i in range(30):
        add(f"Tell us about project {i}", "textarea")
    choices = sum(len(f["options"]) for f in fields)
    calls = []
    real = autofill.norm

    def counted(s):
        calls.append(1)
        return real(s)
    monkeypatch.setattr(autofill, "norm", counted)
    profile = prof()
    started = time.perf_counter()
    plan = plan_autofill(fields, profile)
    took = time.perf_counter() - started
    assert len(fields) == 61 and plan["to_fill"]  # (a real plan: the profile answers many of them)
    assert len(calls) < 4 * choices, (len(calls), choices)
    assert took < 2.0, took


def test_a_place_and_its_aliases_read_the_same_as_before():
    """_place_parts and _aliases with the states' names worked out once: the same parts and aliases as working them
    out for each choice, as they were."""
    import re
    from job_apply.autofill import US_STATES, _aliases, _place_parts, norm
    for s in ["Phoenix, Arizona", "Phoenix, AZ", "US-AZ", "Arizona, United States", "az", "New Mexico", "NM",
              "District of Columbia", "Puerto Rico", "Tempe - Arizona - USA", "Ontario, Canada", "Washington", "wa",
              "Indiana", "in", "Oregon", "or", "Maine", "me", "", "United States of America"]:
        n = norm(s)
        aliases = {n} | ({norm(US_STATES[n.upper()])} if n.upper() in US_STATES else set()) | {
            c.lower() for c, name in US_STATES.items() if n == norm(name)}
        assert (aliases <= _aliases(n)
                and not {a for a in _aliases(n) - aliases if len(a) == 2 and a.upper() in US_STATES})
        parts = [p for p in (norm(x)
                             for x in re.split(r"\s*,\s*|\s+-\s+", re.sub(r"^\s*usa?\s*-\s*", "", s, flags=re.I)))
                 if p]
        was = [next((c.lower() for c, name in US_STATES.items() if p in (c.lower(), norm(name))), p) for p in parts]
        while was and was[-1] in {"united states", "united states of america", "usa", "us", "u s", "u s a"}:
            was.pop()
        assert _place_parts(s) == tuple(was), s


def test_a_choice_saying_the_person_is_not_on_staff_is_answered_from_the_profile():
    """Desert Financial's UKG Pro (run 56): "Have you notified your leader of your application? (internal candidates
    only)" with Yes, No and "I am not a current Desert Financial employee": the last, where the profile says where the
    person works and has never had this employer there, as a Create Account's "new candidate" box is ticked. Only a
    question for the employer's own staff, and only a choice that says no more than that (the review of 0.3.146)."""
    from job_apply.config import Profile

    asked = "Have you notified your leader of your application? (internal candidates only)"
    options = ["Yes", "No", "I am not a current Desert Financial employee"]
    dfcu = {"company": "Desert Financial Credit Union"}

    def answer(p=None, opts=options, job=dfcu, kind="radio_group", label=asked):
        a = resolve_field(f(label, kind, options=opts), p or prof(), job)
        return a and a.value

    assert answer() == "I am not a current Desert Financial employee"
    assert answer(kind="select") == "I am not a current Desert Financial employee"
    assert answer(opts=["Yes", "No", "I am not a current employee of Desert Financial Credit Union"]) \
        == "I am not a current employee of Desert Financial Credit Union"
    assert answer(opts=["Yes", "No", "I am an external candidate"]) == "I am an external candidate"
    assert answer(opts=["Yes", "No"]) is None
    assert answer(opts=["Yes", "No", "I am not a current Intel employee"]) is None  # (another employer's)
    assert answer(opts=["Yes", "No", "I am not a current Intel employee"], job={"company": "Intel Corporation"}) is None
    assert answer(Profile({"personal": {"first_name": "Sam"}})) is None  # (nothing said of their work)
    assert answer(job={}) is None
    # a choice that says more than that is theirs: "former", relatives, the last 12 months
    for more in ("I am not a current or former Desert Financial employee",
                 "I am not a current employee of Desert Financial or a former employee",
                 "I am not a current employee of Desert Financial and I am not related to any employee",
                 "I am not a current employee and have not been employed there in the last 12 months"):
        assert answer(opts=["Yes", "No", more]) is None, more
    # a question that isn't only for staff: whether they applied before, or worked there lately, isn't this
    assert answer(label="Have you applied with us before?",
                  opts=["I am a new applicant", "I am a returning applicant"]) is None
    assert answer(label="Have you worked for Desert Financial in the last year?") == "No"  # (the work history's rule)
    # a profile that has this employer in it at all (a job there with no end, a past one): theirs to say
    there = {"title": "Teller", "company": "Desert Financial Credit Union", "start": "2022-01"}
    data = dict(prof().data)
    assert answer(Profile({**data, "work_history": [there]})) is None
    assert answer(Profile({**data, "work_history": [*data["work_history"], {**there, "end": "2023-05"}]})) is None
    # another employer that only shares a first word isn't this one (the State of Arizona at Arizona State University)
    asu = {"company": "Arizona State University"}
    for other in ("I am not a current employee of the State of Arizona",
                  "I am not a current State of Arizona employee"):
        assert answer(opts=["Yes", "No", other], job=asu) is None, other


def test_resolve_contact_fields():
    p = prof()
    assert resolve_field(f("First Name *"), p).value == "Sam"
    assert resolve_field(f("Legal Last Name"), p).value == "Rivera"
    assert resolve_field(f("Email Address"), p).value == "sam.rivera@example.com"
    assert resolve_field(f("Enter email to start application process"), p).value == "sam.rivera@example.com"  # Qorvo
    assert resolve_field(f("Retype Email Address: *"), p).value == "sam.rivera@example.com"  # SuccessFactors
    assert resolve_field(f("Phone Number"), p).value == "480-555-0123"
    assert resolve_field(f("State", "select", options=["Arizona", "Texas"]), p).value == "Arizona"
    assert resolve_field(f("Country Phone Code", "listbox", options=["Canada (+1)", "United States of America (+1)"]),
                         p).value == "United States of America (+1)"
    assert resolve_field(f("LinkedIn Profile"), p).value.endswith("/samrivera")


def test_resolve_questions():
    p = prof()
    yes_no = ["Yes", "No"]
    assert (resolve_field(f("Are you legally authorized to work in the United States?", "radio_group", options=yes_no),
                          p).value == "Yes")
    assert resolve_field(f("Will you now or in the future require sponsorship for employment visa status (e.g., H-1B)?",
                           "radio_group", options=yes_no), p).value == "No"
    assert resolve_field(f("Are you a U.S. person as defined by the ITAR?", "select", options=yes_no), p).value == "Yes"
    assert resolve_field(f("Are you willing to relocate?", "select", options=yes_no), p).value == "No"
    assert resolve_field(f("Are you at least 18 years of age?", "radio_group", options=yes_no), p).value == "Yes"
    assert (resolve_field(f("Have you ever worked for Lam Research?", "select", options=yes_no), p,
                          {"company": "Lam Research"}).value == "No")
    assert (resolve_field(f("Have you previously been employed by Intel?", "select", options=yes_no), p,
                          {"company": "Intel Corporation"}).value == "Yes")
    assert (resolve_field(f("Are you comfortable working in a cleanroom environment?", "radio_group", options=yes_no),
                          p).value == "Yes")
    # the same facts asked the other way round
    assert resolve_field(f("Are you legally authorized to work in the U.S. without employer sponsorship?",
                           "radio_group", options=yes_no), p).value == "Yes"
    assert resolve_field(f("Can you work for us without requiring visa sponsorship now or in the future?", "select",
                           options=yes_no), p).value == "Yes"
    assert resolve_field(f("Are you under the age of 18?", "radio_group", options=yes_no), p).value == "No"
    # a local applicant isn't relocating; more travel than the profile allows is the user's call
    assert resolve_field(f("Do you currently live in the Phoenix area or are you willing to relocate?", "radio_group",
                           options=yes_no), p) is None
    assert (resolve_field(f("Are you willing to travel up to 50% of the time?", "radio_group", options=yes_no),
                          p).value == "Yes")
    assert resolve_field(f("This role requires up to 100% travel. Are you willing?", "radio_group",
                           options=yes_no), p) is None
    # "How did you hear" must not be answered with the LinkedIn profile URL
    assert resolve_field(f("How did you hear about us? (LinkedIn, Indeed, etc.)"), p).value == "LinkedIn"


def test_resolve_leaves_unknowns_alone():
    p = prof()
    assert resolve_field(f("Why do you want to work here?", "textarea"), p) is None
    # answer bank entry empty
    assert resolve_field(f("Can you lift 50 lbs?", "radio_group", options=["Yes", "No"]), p) is None
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
    # Workday's questionnaire boxes, named by their questions (CSN's and NSU's, live run 144): the cover letter's gets
    # the cover letter; the references' is the person's (the profile has no file for it), not the resume
    workday = {"section": "Application Questions 1 of 2"}
    cover = {**f("Please upload your Letter of Application/Cover Letter.*", "file"), **workday}
    assert resolve_field(cover, p, job, 4).value == str(folder / "cover_letter.pdf")
    refs = {**f("Please upload your Reference Contact Information.*", "file"), **workday}
    assert resolve_field(refs, p, job, 4) is None and resolve_field(refs, p, job, 1) is None


def test_plan_autofill_orders_required_first():
    fields = [
        {"id": "1", "kind": "text", "label": "First Name", "value": ""},
        {"id": "2", "kind": "text", "label": "Last Name", "value": "Already"},
        {"id": "3", "kind": "textarea", "label": "Anything else?", "value": ""},
        {"id": "4", "kind": "textarea", "label": "Why us?", "value": "", "required": True},
        {"id": "5", "kind": "select", "label": "Gender", "value": "Select...",
         "options": ["Select...", "Male", "Female", "Decline"]},
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
    assert choose_option("BS Electrical Engineering",
                         ["Associate's Degree", "Bachelor's Degree", "Master's Degree"]) == "Bachelor's Degree"
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
    assert (entry("Education 1", "Degree", "listbox", options=["Associate's Degree", "Bachelor's Degree"])
            == "Bachelor's Degree")
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
        {"id": "2", "kind": "combobox", "label": "Degree*", "value": "",
         "options": ["Associate's Degree", "Bachelor's Degree"]},
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
          "H-2B, H-4, L-1, L-2, E-1, E-2, J-1, J-2, F-1, etc.)?", "radio_group",
          options=["Yes", "No", "Pending with USCIS"])
    citizen = Profile({"work_authorization": {"us_citizen": True, "requires_sponsorship": False}})
    assert resolve_field(q, citizen).value == "No"
    assert resolve_field(q, Profile({"work_authorization": {"requires_sponsorship": False}})) is None  # not known


def test_attestations_need_known_choices():
    p = prof()
    q = "Are you a U.S. person as defined by U.S. export control regulations (EAR)?"
    assert resolve_field(f(q, "combobox"), p) is None  # choices unknown: leave it for a person
    assert resolve_field(f(q, "combobox", options=["Yes", "No"]), p).value == "Yes"
    assert resolve_field(f(q, "combobox",
                           options=["I am a U.S. citizen", "I am a lawful permanent resident", "Other"]), p) is None


def test_export_questions_that_are_not_about_being_a_us_person():
    p = prof()
    yes_no = ["Yes", "No"]
    # Micron, live: answering this from "U.S. person: yes" said Yes, the opposite of the truth
    micron = ("All Micron sites must observe U.S. export control rules that control information that may be provided "
              "to persons from Cuba, Iran, North Korea, and Syria. Are you a citizen of, or do you hold dual "
              "citizenship with any of these countries?")
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
    for q in ("Current/Last Drawn Monthly Basic Salary:",
              "Expected Monthly Basic Salary (exclude allowance & overtime)",
              "Please describe your current compensation package."):
        assert resolve_field(f(q, "textarea" if q.startswith("Please") else "text"), p) is None, q
    # the user's own answer still applies
    p.data["answers"] = [{"match": "Cuba, Iran", "answer": "No"}]
    assert resolve_field(f(micron, "combobox", options=yes_no), p).value == "No"


def test_choices_seen_on_live_forms():
    asm_prev = ["I have NEVER been employed by ASM", "I was PREVIOUSLY employed by ASM",
                "I am CURRENTLY employed by ASM"]
    assert choose_option("No", asm_prev) == "I have NEVER been employed by ASM"
    asm_auth = ["I require sponsorship to work in this country",
                "I am authorized to work in this country for my current employer",
                "I am authorized to work in this country for any employer",
                "My status to work in this country is unknown"]
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
    lam = ["\U0001F1FA\U0001F1F2 (+1) United States Minor Outlying Islands",
           "\U0001F1FA\U0001F1F8 (+1) United States of America", "\U0001F1E8\U0001F1E6 (+1) Canada"]
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


def test_a_file_kept_from_before_gives_way_only_to_one_tailored_for_the_job(tmp_path):
    """NXP's Workday (the weekly probe, Oct 2026): an account's last resume kept in My Experience's "Resume/CV" upload
    was read as the box's value, so it was left there and the employer got the old resume, not the one tailored for
    the job. A resume or cover letter box showing a file of another name than the job's tailored one is said kept from
    before (for the desk to replace); with no tailored file, none is (a kept file stays, the profile's resume too)."""
    from job_apply import autofill

    folder = tmp_path / "job"
    folder.mkdir()
    job = {"folder": str(folder)}
    box = {"id": "r", "kind": "file", "label": "Upload a file (5MB max)", "section": "Resume/CV",
           "value": "resume-2025.pdf"}
    cover = {"id": "c", "kind": "file", "label": "Cover Letter", "value": "cover-letter-2025.pdf"}
    assert autofill.kept_documents([box, cover], prof(), job) == []
    (folder / "Sam_Rivera_Resume.pdf").write_bytes(b"%PDF-1.4\n")
    resume = str(folder / "Sam_Rivera_Resume.pdf")
    assert autofill.kept_documents([box, cover], prof(), job) == [
        {"id": "r", "kind": "resume", "where": "Resume/CV", "shown": ["resume-2025.pdf"], "tailored": resume,
         "unsure": False}]  # (no cover letter tailored: the kept one stays)
    (folder / "Sam_Rivera_Cover_Letter.pdf").write_bytes(b"%PDF-1.4\n")
    assert [(k["id"], k["kind"], k["tailored"]) for k in autofill.kept_documents([box, cover], prof(), job)] == [
        ("r", "resume", resume), ("c", "cover_letter", str(folder / "Sam_Rivera_Cover_Letter.pdf"))]
    # nothing there: nothing to replace
    for shown in ("", None):
        assert autofill.kept_documents([{**box, "value": shown}], prof(), job) == [], shown
    # a file of the tailored one's name is no sign it's that one (the review of 0.3.209): every tailored resume is named
    # <First>_<Last>_Resume.pdf, so the last application's at the same employer has it too. Whether the desk put it
    # there is the desk's to say (Applier._replace_kept)
    [kept] = autofill.kept_documents([{**box, "value": "Sam_Rivera_Resume.pdf"}], prof(), job)
    assert (kept["shown"], kept["unsure"]) == (["Sam_Rivera_Resume.pdf"], False)
    # a box that shows only that a file is there: replaced; several files, or a box for a resume and a cover letter
    # both: which is the old resume isn't sure
    [kept] = autofill.kept_documents([{**box, "value": "uploaded"}], prof(), job)
    assert (kept["shown"], kept["unsure"]) == ([], False)
    [kept] = autofill.kept_documents([{**box, "value": "resume-2025.pdf, cover-letter-2025.pdf"}], prof(), job)
    assert (kept["shown"], kept["unsure"]) == (["resume-2025.pdf", "cover-letter-2025.pdf"], True)
    [kept] = autofill.kept_documents([{**box, "section": "Resume/CV and Cover Letter"}], prof(), job)
    assert (kept["kind"], kept["unsure"]) == ("resume", True)
    # never a box for another document (a transcript, a certificate), nor a page's one unnamed upload, nor one that
    # reads the resume to fill the form in
    for other in ({"label": "Transcripts", "section": ""}, {"label": "Upload", "section": "Certifications"},
                  {"label": "Resume and transcripts", "section": ""}, {"label": "Attachments", "section": ""}):
        assert autofill.kept_documents([{**box, **other}], prof(), job) == [], other
    assert [k["id"] for k in autofill.kept_documents(
        [{**box, "id": "i", "label": "Import your profile from resume", "section": ""}, box], prof(), job)] == ["r"]


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

    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    data["eeo"]["disability"] = "I don't wish to answer"
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    boxes = ["Yes, I have a disability, or have had one in the past",
             "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
    ans = resolve_field(f("Please check one of the boxes below:*", "checkbox_group", options=boxes), prof())
    assert (ans.rule, ans.value) == ("disability", "I do not want to answer")
    # one choice that mentions it is not enough to tell
    assert resolve_field(f("Please choose one", "radio_group", options=["Yes", "No, no disability"]), prof()) is None
    # a question that says what it asks is never answered from its choices: an accommodation
    # isn't the EEO disability answer, nor is "military member" the protected-veteran one
    data["eeo"]["veteran"] = "I am not a protected veteran"
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    accommodation = ["Yes, I need an accommodation due to a disability",
                     "No, I do not need an accommodation for a disability"]
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
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
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
    p = Profile({"work_authorization": {"authorized_to_work": True, "requires_sponsorship": False,
                                        "citizenship": "Mexico"},
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
    assert answer("Can you perform the essential functions of this job with or without "
                  "accommodation for a disability?") is None
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
    a = resolve_field({"id": "1", "label": label, "kind": kind, "value": "", **kw}, p or _person(),
                      job or {"company": "Acme"})
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
              "Are you legally authorized to work in the United States? Note: this position is not "
              "eligible for visa sponsorship.",
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
    not_allowed = _person(work_authorization={"authorized_to_work": False, "requires_sponsorship": True,
                                              "over_18": True})
    assert _answer("Are you at least 18 years of age and legally authorized to work in the United States?",
                   not_allowed) is None
    assert _answer("Are you 18 years of age or older and able to work in the U.S. without sponsorship?",
                   not_allowed) is None
    # a count of 18 isn't an age (the review of Oct 15: "at least 18 months of experience" was answered Yes from
    # being over 18, "less than 18 months" No)
    for q in ("Do you have at least 18 months of experience in equipment maintenance?",
              "Have you worked less than 18 months in your current role?", "Do you have 18 years of experience?",
              "Can you work at least 18 hours a week?", "Have you completed at least 18 credits?",
              "Do you have under 18 months of experience with Workday?", "Have you been working at least 18 years?",
              "Have you completed at least 18 units?"):
        assert _answer(q, kind="radio_group", options=["Yes", "No"]) is None, q
    # (and the age asked in other words, or with "if under 18" after it, which asks the other way: the review's)
    for q in ("Are you at least eighteen years old?", "Are you 18 or older?", "Are you over 18?", "Age 18 or older?",
              "Are you at least 18 years of age? If under 18, please provide your date of birth"):
        assert _answer(q, kind="radio_group", options=["Yes", "No"]) == "Yes", q


def test_schooling_is_never_understated_nor_overstated():
    """A high school diploma isn't "Some High School"; schooling not finished is never a
    degree, nor gets a graduation date; a school is picked by its name."""
    for ladder in (["Some High School", "High School Graduate", "Some College", "Associate's Degree",
                    "Bachelor's Degree"],
                   ["Less than High School", "Some High School", "High School/GED", "Some College", "Associates",
                    "Bachelors"]):
        assert _answer("Highest level of education", kind="select", options=ladder) in ("High School Graduate",
                                                                                        "High School/GED")
    assert choose_option("Associate's degree (in progress)",
                         ["Some College", "Associate's Degree", "Bachelor's Degree"]) in (None, "Some College")
    assert choose_option("Electrical Engineering coursework toward BSEE", ["High School", "Bachelor's Degree"]) is None
    for written in ("Not completed", "None", "No degree", "Some college (no degree)"):
        p = _person(education={},
                    education_history=[{"school": "Mesa Community College", "degree": written, "end": 2019}])
        assert _answer("Graduation Year", p) is None, written
        assert _answer("Graduation Date", p, section="Education 1") is None, written
    asu = _person(education={"school": "Arizona State University"})
    assert _answer("School", asu, kind="combobox",
                   options=["University of Arizona", "Northern Arizona University"]) is None


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
                                                                           section="Education"
                                                                           if section == "Education" else None))
        assert (filled, asked) == ({"s": "Mesa Community College"}, ["y"]), section
    # on its own, or about another credential, the year is the profile's
    assert plan(box("y", "Graduation year")) == ({"y": 2015}, [])
    assert plan(school, box("y", "High school graduation year"))[0]["y"] == 2015
    # a school the person finished gives its own year, not another credential's
    graduate = _person(education={"highest_degree": "Bachelor's Degree", "graduation_year": 2015},
                       education_history=[{"school": "Arizona State University", "degree": "Bachelor's Degree",
                                           "start": 2016, "end": 2020}])
    assert (plan(school, box("y", "Graduation year"), person=graduate)
            == ({"s": "Arizona State University", "y": "2020"}, []))
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
    assert _answer("Have you ever been employed with ASM before?", kind="radio_group", options=asm,
                   job={"company": "ASM"}) == "I was PREVIOUSLY employed by ASM"
    assert (_answer("Have you ever been employed with Example Fab before?", kind="radio_group",
                    job={"company": "Example Fab"}, options=[o.replace("ASM", "Example Fab") for o in asm])
            == "I am CURRENTLY employed by Example Fab")
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
    # a short question has no core: it is the whole label or nothing ("Are you currently employed?"
    # isn't "... by ASML?")
    assert "core" not in config.save_answer("Are you currently employed?", "Yes", "Acme")
    assert remembered("Are you currently employed?", "Acme") == "Yes"
    assert remembered("Are you currently employed by ASML? Please say.", "Acme") is None
    # a question about the employer is that employer's, read from the sentence that asks, not the notice around it
    mine = ("Our Hiring Process: Acme looks at every application it gets, and its recruiters talk to some of the "
            "people who apply, so you may hear from them by phone or by email in the weeks after you apply. "
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
    assert (answer_to_remember("Have you applied before?", "No, never applied", "Applied Materials")
            == "No, never applied")
    assert answer_to_remember(how, "Applied Materials website", "Applied Materials") == "Company Website"
    # remembered so, it's the employer's own website wherever it's asked: the list's "<employer> website"
    p = _person(preferences={"how_did_you_hear": "LinkedIn"})
    p.data["answers"] = [{"match": "how did you hear about us", "answer": "Company Website", "question": how,
                          "from": "Acme"}]
    lists = ["Advertisement", "Example Fab website", "Job Board"]
    assert _answer("How Did You Hear About Us?*", p, "combobox", {"company": "Example Fab"}, options=lists) == \
        "Example Fab website"
    assert _answer("How Did You Hear About Us?*", p, "combobox", {"company": "Example Fab"},
                   options=["Job Board"]) is None
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
    for shown in ("Please Select...", "Select One...", "Choose...", "Select a State", "Select Country",
                  "Make a Selection"):
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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    def with_phone(phone, code=None):
        personal = {**data["personal"], "phone": phone}
        if code is not None:
            personal["phone_country_code"] = code
        path.write_text(yaml.safe_dump({**data, "personal": personal}), encoding="utf-8")
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
                          {"id": "c", "label": "I agree to join the Talent Community", "kind": "checkbox",
                           "value": False, "required": True, "aside": True},
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
    """A list in the profile written as something else (a number) neither raises nor answers from it: the rest of the
    page is still planned (it asserted nothing before: the review of Oct 15)."""
    for key in ("answers", "education_history", "work_history"):
        p = _person(**{key: 1})
        plan = plan_autofill([{"id": "1", "label": "Have you previously worked for Acme?", "kind": "text", "value": ""},
                              {"id": "2", "label": "Graduation Year", "kind": "text", "value": ""},
                              {"id": "3", "label": "First Name", "kind": "text", "value": ""}], p, {"company": "Acme"})
        planned = {f["id"]: f.get("value") for f in plan["to_fill"]}
        assert planned.get("3") == "Sam", (key, plan)
        assert planned.get("1") != 1 and planned.get("2") != 1, (key, plan)


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
    # "County / State" is the state (Rogers' SuccessFactors lists the states under it, run 56: "Maricopa" wasn't there)
    states = ["No Selection", "Alabama", "Alaska", "Arizona", "Arkansas", "California"]
    assert resolve_field(f("County / State", "combobox", options=states), p).value == "Arizona"

    path = config.profile_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    def with_address(**address):
        path.write_text(yaml.safe_dump({**data, "personal": {**data["personal"],
                                                            "address": {**data["personal"]["address"], **address}}}),
                        encoding="utf-8")
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
        a = resolve_field(f(label, "radio_group", options=yes_no), profile,
                          {"company": "Mayo Clinic", "location": location})
        return a and a.value

    assert answer(mayo, "Phoenix, AZ, United States") == "Yes"
    assert answer(mayo, "Scottsdale, Arizona") == "Yes"
    assert answer(mayo, "US-AZ-Phoenix") == "Yes"
    assert answer(mayo, "Rochester, MN; Phoenix, AZ") == "Yes"
    assert (answer("Do you live within commuting distance of the job, or are you open to relocating?",
                   "Tempe, AZ") == "Yes")
    assert answer(mayo, "Tucson, AZ") is None  # another metro area
    assert answer(mayo, "Glendale, CA") is None  # Arizona's Glendale is near, California's isn't
    assert answer(mayo, "") is None
    assert answer("Do you live in the area, or will you need to relocate?", "Phoenix, AZ") is None
    assert answer("Which campus are you located near, or are you willing to relocate?", "Phoenix, AZ") is None
    assert answer(mayo, "Rochester, MN / Phoenix, AZ") == "Yes"

    path = config.profile_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    def with_(personal=None, preferences=None):
        new = {**data, "preferences": {**data.get("preferences", {}), **(preferences or {})}}
        new["personal"] = {**data["personal"], "address": {**data["personal"]["address"], **(personal or {})}}
        path.write_text(yaml.safe_dump(new), encoding="utf-8")
        return Profile.load()

    assert answer(mayo, "Tucson, AZ", with_(preferences={"willing_to_relocate": True})) == "Yes"
    # Pinal County: still Phoenix's
    assert answer(mayo, "Phoenix, AZ", with_(personal={"city": "San Tan Valley"})) == "Yes"
    assert answer(mayo, "Phoenix, AZ", with_(personal={"city": "Austin", "state": "TX"})) is None


def test_how_did_you_first_hear():
    """Mayo Clinic (live, Oct 2026) asks "How did you first hear about this opportunity?", which
    the how-heard rule missed for the word "first": its "Mayo Clinic Career Site" is the
    profile's "Company Website"."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"],
                                                            "how_did_you_hear": "Company Website"}}), encoding="utf-8")
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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**data,
                                    "preferences": {**data["preferences"], "locations": ["Phoenix, AZ", "Tempe, AZ"]}}),
                    encoding="utf-8")
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
    assert (choose_option("Hispanic or Latino", ["Yes, Hispanic or Latino", "Not Hispanic or Latino"])
            == "Yes, Hispanic or Latino")
    assert choose_option("I am not a protected veteran", ["Protected Veteran", "Decline"]) is None
    # nor a choice that denies more than the answer: a veteran who isn't a protected one is a veteran
    assert choose_option("I am not a protected veteran", ["Protected Veteran", "Not a Veteran", "Decline"]) is None
    assert choose_option("Not a Veteran", ["I am a protected veteran", "I am not a protected veteran"]) == \
        "I am not a protected veteran"
    # a denial about something else doesn't stand in the way
    assert choose_option("Asian", ["Asian (Not Hispanic or Latino)", "White (Not Hispanic or Latino)"]) == \
        "Asian (Not Hispanic or Latino)"
    assert choose_option("No, I do not have a disability",
                         ["Yes, I have a disability", "No, I don't have a disability",
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
    legally = resolve_field(f("Are you authorized to legally work in the United States? If offered employment, you "
                              "will be required to present documentation.*", "radio_group", options=["Yes", "No"]), p)
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


def test_a_degree_level_the_list_lacks_is_never_another_level_nor_a_ged_a_diploma():
    """The review of Oct 15: "Bachelor's Degree" among Master's Degree, Doctorate and Other was "Master's Degree"
    (the words they share), "Master's Degree" among Associate's, Bachelor's and Other "Associate's Degree", a GED
    "High School Diploma" and a diploma "GED". A level the list lacks is the person's to say (an honest "Other",
    or nothing); a GED matches a choice that names an equivalent, a diploma one that says high school or diploma."""
    assert choose_option("Bachelor's Degree", ["Master's Degree", "Doctorate", "Other"]) is None
    assert choose_option("Master's Degree", ["Associate's Degree", "Bachelor's Degree", "Other"]) is None
    assert choose_option("Associate's Degree", ["Some College", "No degree", "Other"]) is None
    assert choose_option("GED", ["High School Diploma", "Bachelor's Degree"]) is None
    # (the bare level: a GED is its equivalent)
    assert choose_option("GED", ["High School", "College"]) == "High School"
    assert choose_option("High School Diploma", ["GED", "Bachelor's Degree"]) is None
    assert choose_option("High School Diploma", ["High School Equivalent (GED)", "Bachelor's"]) is None
    assert choose_option("GED", ["High School Diploma or GED", "Bachelor's Degree"]) == "High School Diploma or GED"
    assert choose_option("GED", ["High School Equivalent (GED)", "Bachelor's"]) == "High School Equivalent (GED)"
    assert choose_option("GED", ["High School or Equivalent", "Bachelor's"]) == "High School or Equivalent"
    assert choose_option("High School Diploma", ["High School Diploma or Equivalent", "Bachelor's"]) == \
        "High School Diploma or Equivalent"
    assert (choose_option("High School Diploma", ["High School or Equivalent", "Bachelor's"])
            == "High School or Equivalent")
    assert choose_option("High School Diploma", ["High School", "College"]) == "High School"
    # the level that is there, as before, in the bare singular too (the review of 0.3.175: "Bachelor" alone was no
    # level, so the list of issue #112, note 16, would have been said to lack a bachelor's); a list with no degree in
    # it, as before
    assert choose_option("Bachelor's Degree", ["Associates", "Bachelors", "Masters"]) == "Bachelors"
    workday = ["Certificate", "Foundation", "Associate", "Bachelor", "Master", "Doctor", "Ph.D.", "Others",
               "Some College", "No degree (Dropped out)"]
    for said, pick in (("Bachelor's Degree", "Bachelor"), ("Bachelor of Science", "Bachelor"), ("BSEE", "Bachelor"),
                       ("Master's Degree", "Master"), ("MBA", "Master"), ("Associate of Applied Science", "Associate"),
                       ("GED", None), ("High School Diploma", None)):
        assert choose_option(said, workday) == pick, (said, pick)
    taleo = ["HS Diploma", "GED", "Associate", "Bachelor", "Master"]
    assert choose_option("Bachelor of Science", taleo) == "Bachelor" and choose_option("GED", taleo) == "GED"
    assert choose_option("High School Diploma", taleo) == "HS Diploma"
    assert choose_option("Doctorate", ["PhD", "Master's"]) == "PhD"
    assert choose_option("Bachelor's Degree", ["Yes", "No"]) is None


def test_a_bare_job_title_beside_a_profile_employers_company_gets_that_jobs_title():
    """Cornerstone's resume reader (Linde's and Matheson's, live check Oct 11) adds an experience row with its
    Company filled in and its Job Title empty, under a heading the form reader doesn't tie to the boxes: the
    title of the profile's job at that company. Never for a company the profile doesn't have."""
    p = Profile({"work_history": [{"title": "Equipment Technician", "company": "Example Fab", "start": "2021-03",
                                   "end": "present"},
                                  {"title": "Field Tech", "company": "Other Semi", "start": "2018-01",
                                   "end": "2021-02"}]})

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
               "section": "Closest Metropolitan City . Required",
               "options": ["Not Specified", "United States", "Canada"]}
    gender = {"id": "g", "label": "Gender", "kind": "select", "value": "Not Specified",
              "options": ["Male", "Female", "Not Specified"]}
    # nor a self-identification list whose first choice is "Not Specified": the person may have chosen it
    taleo_gender = {**gender, "id": "t", "options": ["Not Specified", "Male", "Female"]}
    # its Region is a list of cities: the profile's city goes there, not its state (live run 12)
    region = {"id": "r", "label": "Region", "kind": "select", "value": "Not Specified",
              "section": "Closest Metropolitan City . Required",
              "options": ["Not Specified", "Anthem", "Bisbee", "Chandler"]}
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


def test_a_list_on_its_first_choice_with_no_empty_one_is_the_sites_and_takes_the_profiles_answer():
    """A list with no empty choice to start on opens on its first ("Male", a country code's "Afghanistan +93"): it read
    as answered, and went out as the person's answer whatever the profile said. The profile's answer goes in where it's
    another; where the list has no choice for it, the person is asked; where the profile says nothing, or the same, or
    the list starts on an empty choice (someone picked what it shows), it's left as it is."""
    decliner = Profile({"eeo": {"gender": "Decline to self-identify"},
                        "personal": {"address": {"country": "United States"}}})
    gender = {"id": "g", "label": "Gender", "kind": "select", "value": "Male",
              "options": ["Male", "Female", "I don't wish to answer"]}
    plan = plan_autofill([gender], decliner, {})
    assert [(t["id"], t["value"]) for t in plan["to_fill"]] == [("g", "I don't wish to answer")], plan
    no_decline = {**gender, "id": "n", "options": ["Male", "Female"]}
    assert [q["id"] for q in plan_autofill([no_decline], decliner, {})["needs_input"]] == ["n"]
    same = {"id": "c", "label": "Country", "kind": "select", "value": "United States",
            "options": ["United States", "Canada"]}
    picked = {**gender, "id": "p", "options": ["Select...", "Male", "Female", "I don't wish to answer"]}
    nothing = {"id": "s", "label": "Salutation", "kind": "select", "value": "Mr.", "options": ["Mr.", "Ms.", "Dr."]}
    plan = plan_autofill([same, picked, nothing], decliner, {})
    assert plan["to_fill"] == [] and plan["needs_input"] == [] and plan["already_filled"] == ["c", "p", "s"], plan
    assert plan["judged"] == ["c", "s"]  # (marked on the page: judged once)
    # the review: the same answer word for word ("Bachelor's Degree" isn't the "Associate's Degree" a list opens on)
    graduate = Profile({"education": {"degree": "Bachelor's Degree", "field": "Physics", "school": "Example State"},
                        "education_history": [{"school": "Example State", "degree": "Bachelor's Degree"}]})
    degree = {"id": "d", "label": "Highest degree", "kind": "select", "value": "Associate's Degree",
              "options": ["Associate's Degree", "Bachelor's Degree", "Master's Degree"]}
    assert [(t["id"], t["value"]) for t in plan_autofill([degree], graduate, {})["to_fill"]] == \
        [("d", "Bachelor's Degree")]
    # an optional list with no choice for the profile's answer is asked all the same: what it shows goes out
    asked = plan_autofill([{**no_decline, "required": False}], decliner, {})["needs_input"]
    assert [(q["id"], q.get("required")) for q in asked] == [("n", True)], asked
    # a list of several picks, a list judged once already, or the desk told to fill everything again: as before
    assert plan_autofill([{**gender, "multiple": True}], decliner, {})["already_filled"] == ["g"]
    assert plan_autofill([{**gender, "judged": True}], decliner, {})["already_filled"] == ["g"]
    assert [t["id"] for t in plan_autofill([gender], decliner, {}, overwrite=True)["to_fill"]] == ["g"]


def test_a_country_list_the_site_preset_to_another_country_takes_the_profiles():
    """Onto Innovation's Workday (run 118) opened on Mexico and Mexico (+52) for a profile in the United States: read
    as answered, they stayed, and the phone number was refused. A country list (the address's, the phone's dial code)
    the site preset to another country takes the profile's, once (judged); the same country however it's written
    stays; a profile with no country, a country of citizenship, or a list judged once already: as before."""
    prof = Profile({"personal": {"phone": "480-555-0199", "phone_country_code": "+1",
                                 "address": {"country": "United States", "city": "Chandler"}}})
    country = {"id": "c", "kind": "listbox", "label": "Country*", "value": "Mexico", "required": True}
    code = {"id": "p", "kind": "combobox", "label": "Country Phone Code*", "value": "Mexico (+52)", "required": True}
    plan = plan_autofill([country, code], prof, {})
    assert [(t["id"], t["rule"]) for t in plan["to_fill"]] == [("c", "country"), ("p", "phone_code")], plan
    assert plan["judged"] == ["c", "p"]
    same = [{**country, "value": "United States of America"}, {**code, "value": "USA +1"},
            {**country, "id": "r", "label": "Country/Region", "value": "United States"}]
    plan = plan_autofill(same, prof, {})
    assert plan["to_fill"] == [] and plan["already_filled"] == ["c", "p", "r"] and plan["judged"] == ["c", "p", "r"]
    assert plan_autofill([country, code], Profile({"personal": {"phone": "480-555-0199"}}), {})["to_fill"] == []
    citizenship = {**country, "id": "z", "label": "Country of Citizenship"}
    assert plan_autofill([citizenship], prof, {})["already_filled"] == ["z"]
    assert plan_autofill([{**country, "judged": True}], prof, {})["already_filled"] == ["c"]
    assert not same_country("Canada (+1)", "United States of America (+1)")
    assert not same_country("Mexico (+52)", "United States of America (+1)")
    # (the review of 0.3.196) only the person's own address or phone: never a past job's, a school's or
    # another address's
    for section in ("Work Experience", "Education", "Previous Address", "Mailing Address", "Licenses 1"):
        assert plan_autofill([{**country, "section": section}], prof, {})["to_fill"] == [], section
    for section in ("Address", "Phone", "My Information"):
        assert plan_autofill([{**country, "section": section}], prof, {})["to_fill"], section
    # a select with an empty choice to start on: someone picked what it shows
    picked = {"id": "s", "kind": "select", "label": "Country", "value": "Canada",
              "options": ["Select...", "Canada", "United States"]}
    assert plan_autofill([picked], prof, {})["to_fill"] == []
    # once a job: a list looked at already (by label and section) is the person's since
    plan = plan_autofill([country], prof, {})
    assert plan["presets"] == {"c": "country|"}
    assert plan_autofill([country], prof, {}, presets_done={"country|"})["already_filled"] == ["c"]
    # a dial code's list by its code: +52 for a +52 number stays, as does Canada's +1 for a +1 number; a code alone too
    mexican = Profile({"personal": {"phone": "55 1234 5678", "phone_country_code": "+52",
                                    "address": {"country": "United States", "city": "Chandler"}}})
    assert plan_autofill([code], mexican, {})["to_fill"] == []
    # (the re-check) its choices read too: Mexico (+52) is the profile's code, whatever the address's country picks
    codes = ["Canada (+1)", "Mexico (+52)", "United States of America (+1)"]
    assert plan_autofill([{**code, "options": codes}], mexican, {})["to_fill"] == []
    assert [t["id"] for t in plan_autofill([{**code, "options": codes}], prof, {})["to_fill"]] == ["p"]
    assert same_country("+1", "United States of America (+1)") and same_country("United States (1)", "USA +1")
    # (the re-check) +1 is several countries': a US number under Canada (+1) is refused for its region, so it's taken
    assert [t["id"] for t in plan_autofill([{**code, "value": "Canada (+1)"}], prof, {})["to_fill"]] == ["p"]
    # nor a job's location, a place they'd like to work, or another address
    for section in ("Preferred Location", "Work Location", "Other Address", "Temporary Address"):
        assert plan_autofill([{**country, "section": section}], prof, {})["to_fill"] == [], section


def test_a_list_the_page_marks_invalid_takes_the_profiles_answer_and_a_required_self_id_is_answered():
    """Live run 13 (Oct 2026). Northrop's Eightfold drew its phone "Country code" on Mexico (+52) and marked it
    invalid ("Enter a valid phone number and country code"); the desk left it, as a box with a value. A list
    the page marks invalid takes the profile's answer where it's another. Kforce's required "Birth Assigned
    Gender" sat on its first choice "Not Specified" and was left as a self-identification the person may have
    chosen; a required one is answered."""
    p = Profile({"personal": {"phone": "480-555-0199", "phone_country_code": "+1",
                              "address": {"country": "United States"}}, "eeo": {"gender": "Male"}})
    codes = ["\U0001F1FA\U0001F1F8 (+1) United States", "\U0001F1F2\U0001F1FD (+52) Mexico",
             "\U0001F1E8\U0001F1E6 (+1) Canada"]
    code = {"id": "c", "label": "Country code", "kind": "combobox", "value": codes[1], "invalid": True,
            "required": True, "options": codes}
    birth = {"id": "b", "label": "3. Birth Assigned Gender . Required", "kind": "select", "value": "Not Specified",
             "required": True, "options": ["Not Specified", "Male", "Female"]}
    plan = plan_autofill([code, birth], p, {}, redo={"c"})  # (the review step's, once a job)
    got = {t["id"]: t["value"] for t in plan["to_fill"]}
    assert set(got) == {"c", "b"} and got["b"] == "Male", plan
    # on an ordinary pass too, since run 118 (Onto's Workday, whose phone code on Mexico wasn't marked invalid): a
    # country the site preset to another than the profile's is the site's, taken once (judged: a pick since is the
    # person's, test_a_country_list_the_site_preset_to_another_country_takes_the_profiles)
    assert [t["id"] for t in plan_autofill([code], p, {})["to_fill"]] == ["c"]
    assert plan_autofill([{**code, "judged": True}], p, {})["already_filled"] == ["c"]
    # a decline in the profile never gets the list's own "Not Specified" placeholder back: it's asked
    declined = Profile({"eeo": {"gender": "I choose not to disclose"}})
    plan = plan_autofill([birth], declined, {})
    assert plan["to_fill"] == [] and [q["id"] for q in plan["needs_input"]] == ["b"], plan


def test_a_text_box_the_page_says_is_wrong_takes_the_profiles_answer_only_where_it_differs():
    """EMD's Phenom (live run 41, Oct 12): its resume parse wrote the phone as bare digits where the box asks for
    "+1##########", and called it invalid. Named in `redo`, a text box showing another value takes the
    profile's; one already showing the profile's answer, or one the profile doesn't answer, is left."""
    p = Profile({"personal": {"phone": "480-555-0199", "phone_country_code": "+1"}})
    label = ('Phone Number [must include your location code using the "+", (e.g. '
             '+1##########), with no spaces or dashes]*')
    parsed = {"id": "p", "label": label, "kind": "text", "value": "4805550199", "required": True}
    plan = plan_autofill([parsed], p, {}, redo={"p"})
    assert [(t["id"], t["value"]) for t in plan["to_fill"]] == [("p", "+14805550199")], plan
    assert plan_autofill([parsed], p, {})["already_filled"] == ["p"]  # (never on an ordinary pass)
    same = {**parsed, "value": "+14805550199"}
    assert plan_autofill([same], p, {}, redo={"p"})["already_filled"] == ["p"]
    # never a text area, whose answer is the person's own (the review of 0.3.166: Microchip's Workday marks one it lost
    # invalid, and the desk types it again as it shows)
    area = {**parsed, "kind": "textarea"}
    assert plan_autofill([area], p, {}, redo={"p"})["already_filled"] == ["p"]
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
    declines = ("I don't wish to answer", "I don't want to answer", "I do not want to answer",
                "Decline to self-identify", "I choose not to self-identify", "Prefer not to say", "I'd rather not say",
                "I wish not to answer", "Not specified")
    for said in declines:
        assert choose_option(said, DISABILITY) == "I do not want to answer", said
        assert choose_option(said, VETERAN) == "I don't wish to answer", said
        assert choose_option(said, VETERAN_STATUS) == "I do not wish to self-identify", said
        assert choose_option(said, ["Yes", "No", "Decline to self-identify"]) == "Decline to self-identify", said
        assert choose_option(said, ["Male", "Female", "Not Specified"]) == "Not Specified", said
        assert choose_option(said, ["Man", "Woman", "Non-binary", "I prefer not to answer"]) == "I prefer not to answer"
        assert choose_option(said, ["No Selection", "No, I am not a Protected Veteran",
                                    "Yes, I am a Protected Veteran"]) is None, said
        assert choose_option(said, ["Female", "Male"]) is None, said
    # a "no" about something else than answering is an answer, not a decline
    assert (choose_option("No", ["Yes, I am willing to relocate", "I do not wish to relocate"])
            == "I do not wish to relocate")
    # Pfeiffer Vacuum's SuccessFactors (run 56): "Prefers not to answer", "Not willing to answer"
    assert (choose_option("I don't wish to answer", ["No Selection", "Non-Veteran", "Not a Protected Veteran",
                                                     "Prefers not to answer", "Protected Veteran"])
            == "Prefers not to answer")
    assert choose_option("I do not want to answer", ["No Selection", "No disability", "Not willing to answer",
                                                     "Yes, with a disability (or previously had a disability)"]) \
        == "Not willing to answer"
    # (but not willing to relocate, or to travel, is an answer)
    from job_apply.autofill import is_decline
    assert not is_decline("Not willing to relocate") and not is_decline("Unwilling to travel")
    assert not is_decline("Not willing to share salary history")  # (the review of 0.3.147: an answer)


def test_a_decline_is_found_where_workday_names_each_choice_with_its_country():
    """Probe 48 (Oct 13): Workday names each self-identification choice with the form's country, "Undeclared (United
    States of America)", so its declines weren't found, and Air Products' gender and ASML's race lists were asked
    of the person whose profile declines. A choice's own words are read without that aside; an answer is never a
    decline for it."""
    gender = ["Select One", "Female (United States of America)", "Male (United States of America)",
              "Non-Binary (United States of America)", "Undeclared (United States of America)"]
    race = ["Select One", "American Indian or Alaska Native (United States of America)",
            "Asian (United States of America)", "Hispanic or Latino (United States of America)",
            "Not Declared (United States of America)", "Not Specified (United States of America)",
            "Two or More Races (United States of America)", "White (United States of America)"]
    assert choose_option("Decline to self-identify", gender) == "Undeclared (United States of America)"
    assert choose_option("I don't wish to answer", race) == "Not Declared (United States of America)"
    assert choose_option("Female", gender) == "Female (United States of America)"
    assert not is_decline("Female (United States of America)") and not is_decline("Asian (Not Declared)")
    # only a country is put aside: a bracket that holds an answer is read (the review's: gender X)
    assert not is_decline("Unspecified (X)")
    assert choose_option("Decline to self-identify", ["Male", "Female", "Unspecified (X)", "Decline to answer"]) == \
        "Decline to answer"


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

    data = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    data["preferences"]["flexible_schedule"] = True
    data["eeo"]["disability"] = "No"
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    vevraa = ("U.S. – Protected Veteran Self-Identification This employer is a Government contractor subject to the "
              "Vietnam Era Veterans' Readjustment Assistance Act of 1974, as amended (VEVRAA). For more information, "
              "call the U.S. Department of Labor's Veterans' Employment and Training Service (VETS). If you believe "
              "you belong to any of the categories of protected veterans, please indicate by "
              "selecting the appropriate value below.")
    micron = ["I IDENTIFY AS ONE OR MORE OF THE CLASSIFICATIONS OF PROTECTED VETERANS LISTED BELOW",
              "I DO NOT WISH TO SELF-IDENTIFY", "I AM NOT A VETERAN",
              "I IDENTIFY AS A VETERAN, JUST NOT A PROTECTED VETERAN"]
    ans = resolve_field(f(vevraa, "combobox", options=micron), prof())
    assert (ans.rule, ans.value) == ("veteran", "I DO NOT WISH TO SELF-IDENTIFY")
    yes_no = ["Yes, I am a protected veteran", "No, I am not a protected veteran", "I don't wish to answer"]
    assert resolve_field(f(vevraa, "select", options=yes_no), prof()).value == "I don't wish to answer"
    # its list not read yet: the notice's words alone still don't make it an on-call question
    assert resolve_field(f(vevraa, "combobox"), prof()).rule == "veteran"
    cc305 = ("Voluntary Self-Identification of Disability. Why are you being asked to complete this form? We are a "
             "federal contractor required to provide equal employment opportunity to qualified people with "
             "disabilities. Federal law requires employers to provide reasonable accommodations to qualified "
             "applicants. Please check one of the boxes below")
    ans = resolve_field(f(cc305, "radio_group", options=DISABILITY), prof())
    assert (ans.rule, ans.value) == ("disability", "No, I do not have a disability and have not had one in the past")
    # a question that only mentions a disability is still not the self-identification one
    accommodation = ["Yes, I need an accommodation due to a disability",
                     "No, I do not need an accommodation for a disability"]
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
                   options=["No Selection", "No, I am not a Protected Veteran",
                            "Yes, I am a Protected Veteran"]), "id": "2"}
    country = {**f("Country", "combobox", search=True, options=["Canada", "Mexico"]), "id": "3"}
    plan = plan_autofill([gender, veteran, country], prof())
    assert [(q["label"], q.get("unmatched")) for q in plan["needs_input"]] == [
        ("Gender", "Decline to self-identify"), ("Pre-Offer : Are you a Protected Veteran?", "I don't wish to answer")]
    assert [(x["label"], x["value"]) for x in plan["to_fill"]] == [("Country", "United States")]  # (searched for)


def test_a_short_list_of_places_is_read_with_the_rest_of_the_address():
    """A City list short enough to be read whole ("Chandler, Henderson, TX", "Chandler, Lincoln,
    OK", "Chandler, Maricopa, AZ") gave the first Chandler: the fill picks the one the rest of
    the address names, but the plan had already chosen Texas."""
    cities = ["Chandler, Henderson, TX", "Chandler, Lincoln, OK", "Chandler, Maricopa, AZ",
              "Chandler Heights, Maricopa, AZ"]
    assert resolve_field(f("City *", "select", options=cities), prof()).value == "Chandler, Maricopa, AZ"
    assert (resolve_field(f("County", "select", options=["Maricopa, CA", "Maricopa, AZ"]), prof()).value
            == "Maricopa, AZ")
    assert resolve_field(f("State", "select", options=["Arkansas", "Arizona"]), prof()).value == "Arizona"


def test_some_college_however_a_list_words_it():
    """"Some college coursework" (no degree) found nothing in a list whose choice is "Some
    College, No Degree"; it's never a degree, nor high school."""
    degrees = ["High School Diploma/GED", "Associate Degree (AA/AS)", "Some College, No Degree", "Bachelor Degree"]
    assert choose_option("Some college coursework", degrees) == "Some College, No Degree"
    assert choose_option("Some college coursework", ["Some high school", "College coursework, no degree"]) == \
        "College coursework, no degree"
    assert choose_option("Some college",
                         ["High school diploma or GED", "Associate degree", "Bachelor's degree"]) is None
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


def test_previously_employed_by_a_short_name_its_entry_gives_it():
    """UCT's UKG Pro (run 147): "Have you ever been employed by UCT, or worked at UCT on a Temporary/Contractor
    basis?" at Ultra Clean Holdings (UCT): the desk knew the employer by its first word alone ("ultra"), so the question
    wasn't known for one about working there, and the person was asked. A short name the employer's entry on the lists
    gives it (`aliases`) is the employer's too, answered from the profile as before; never another employer's."""
    yes_no = ["Yes", "No"]
    assert employer_aliases("Ultra Clean Holdings (UCT)") == ["uct"]
    assert employer_aliases("Tokyo Electron (TEL)") == ["tel"]
    assert employer_aliases("CliftonLarsonAllen (CLA)") == ["cla"]
    assert employer_aliases("Mission Support and Test Services (NNSS)") == []  # (NNSS names a site, not the employer)
    uct = f("Have you ever been employed by UCT, or worked at UCT on a Temporary/Contractor basis?", "radio_group",
            options=yes_no)
    assert resolve_field(uct, prof(), {"company": "Ultra Clean Holdings (UCT)"}).value == "No"
    tel = f("Have you previously worked for TEL?", "radio_group", options=yes_no)
    assert resolve_field(tel, prof(), {"company": "Tokyo Electron (TEL)"}).value == "No"
    cla = f("Have you ever been employed by CLA?", "radio_group", options=yes_no)
    assert resolve_field(cla, prof(), {"company": "CliftonLarsonAllen (CLA)"}).value == "No"
    # never another employer's short name
    assert resolve_field(f("Have you ever been employed by UCT?", "radio_group", options=yes_no), prof(),
                         {"company": "Tokyo Electron (TEL)"}) is None


def _worked_at(*companies: str) -> Profile:
    return Profile({"work_history": [{"company": c, "title": "Technician", "start": "2018-01", "end": "2020-01"}
                                     for c in companies]})


@pytest.mark.parametrize("past, company, asked", [
    # a past employer holding the employer's short name or its own name: maybe the one asked about
    ("TEL Technology Center America", "Tokyo Electron (TEL)", "Have you previously worked for TEL?"),
    ("TEL Technology Center America", "Tokyo Electron (TEL)", "Have you previously worked for Tokyo Electron?"),
    ("UCT Thermal Solutions", "Ultra Clean Holdings (UCT)", "Have you ever been employed by UCT?"),
    # a bracket's words are no short name: NNSS is a site, "US" a country
    ("National Security Technologies (NSTec)", "Mission Support and Test Services (NNSS)",
     "Have you ever worked at NNSS?"),
    ("National Security Technologies (NSTec)", "Mission Support and Test Services (NNSS)",
     "Have you ever been employed at the NNSS?"),
    ("Honeywell", "Thermo Fisher Scientific (US)", "Have you ever been employed by a US federal contractor?"),
    # the employer's customers, not working for it
    ("Acme", "Tokyo Electron (TEL)", "Have you ever worked at TEL customer sites, such as Intel?"),
])
def test_worked_there_before_is_the_persons_where_the_profile_cant_say(past, company, asked):
    """The review of run 147's fixes: reading a bracket's words as the employer's short name gave a "No" where the
    profile can't say it (a past job at "TEL Technology Center America" applying to Tokyo Electron (TEL)), or where the
    question asks about something else (NNSS, the site; "US"). Asked of the person."""
    q = f(asked, "radio_group", options=["Yes", "No"])
    assert resolve_field(q, _worked_at(past), {"company": company}) is None, (past, company, asked)


def test_an_employers_short_names_are_read_once_while_its_files_stay_the_same(job_apply_home, monkeypatch):
    """The second review of run 147's fixes: employer_aliases read every list again on each question (175 ms). Read
    once, and again only when a file changes (the person's own companies file too)."""
    import os

    from job_apply import search

    reads = []
    every = search.every_employer
    monkeypatch.setattr(search, "every_employer", lambda: reads.append(1) or every())
    own = job_apply_home / "companies.yaml"
    own.write_text("lists: [phoenix-metro]\ncompanies:\n  - name: Example Fab\n    aliases: [EXF, EX]\n",
                   encoding="utf-8")
    assert employer_aliases("Example Fab") == ["exf"]  # (3 letters or more)
    assert employer_aliases("CliftonLarsonAllen (CLA)") == ["cla"] and employer_aliases("Nobody") == []
    assert len(reads) == 1
    own.write_text("lists: [phoenix-metro]\ncompanies:\n  - name: Example Fab\n    aliases: [EFAB]\n",
                   encoding="utf-8")
    os.utime(own, ns=(own.stat().st_atime_ns, own.stat().st_mtime_ns + 10**9))
    assert employer_aliases("Example Fab") == ["efab"] and len(reads) == 2


def test_us_in_capitals_is_the_country_never_this_employer():
    """The second review of run 147's fixes: "Have you ever worked for US Bank?" applying to Intel, with U.S. Bank in
    the profile, said "No": "us" was read as this employer ("worked for us"). "US" in capitals, or "us" before a name,
    is the country's (another organization: the person's); a plain "us" is still this employer."""
    yes_no = ["Yes", "No"]
    past = _worked_at("U.S. Bank")
    for asked in ("Have you ever worked for US Bank?", "Have you ever been employed by US Airways?",
                  "Have you ever been employed by the US Army?", "Have you ever worked for us Bank?"):
        assert resolve_field(f(asked, "radio_group", options=yes_no), past, {"company": "Intel"}) is None, asked
    for asked in ("Have you ever worked for us before?", "Have you ever worked with us in any capacity?",
                  "Have you ever been employed by us?"):
        assert resolve_field(f(asked, "radio_group", options=yes_no), past, {"company": "Intel"}).value == "No", asked


def test_us_the_country_is_never_us_bank_by_its_first_letter():
    """The third review of run 147's fixes: at U.S. Bank the employer's first word was "u", which "the US Postal
    Service" holds once "US" is read as the country's ("U.S."): "No" where the person is asked. Its leading words up to
    one of 3 letters or more name it ("u s bank"); its own name still does, and "us" still means it."""
    yes_no = ["Yes", "No"]
    job = {"company": "U.S. Bank"}
    for past, asked in (("US Airways", "Have you ever worked for US Airways?"),
                        ("United States Postal Service", "Have you ever worked for the US Postal Service?"),
                        ("US Steel", "Have you ever been employed by US Steel?")):
        assert resolve_field(f(asked, "radio_group", options=yes_no), _worked_at(past), job) is None, asked
    for asked in ("Have you ever worked for U.S. Bank?", "Have you ever worked for us before?"):
        assert resolve_field(f(asked, "radio_group", options=yes_no), _worked_at("Wells Fargo"), job).value == "No"
    assert resolve_field(f("Have you ever worked for U.S. Bank?", "radio_group", options=yes_no),
                         _worked_at("U.S. Bank"), job).value == "Yes"


def test_a_former_employee_question_naming_another_company_is_never_answered_from_this_ones():
    """An employer's form (live, Oct 2026) asked "Are you a current or former employee of Ernst &
    Young?": the desk took it for a question about working for the employer applied to, which
    the profile lists, and answered Yes. A question naming another organization is the person's."""
    yes_no = ["Yes", "No"]
    job = {"company": "Intel Corporation"}  # among the test profile's employers
    for asked in ("Are you a current or former employee of Ernst & Young?",
                  "Have you ever been employed by Example Audit LLP?",
                  "Have you previously worked for a Big Four firm?"):
        assert resolve_field(f(asked, "radio_group", options=yes_no), prof(), job) is None, asked
    for asked in ("Are you a current or former employee?", "Have you ever been employed by this company?",
                  "Have you previously worked for us?",
                  "Are you a former employee of Intel or any of its subsidiaries?"):
        assert resolve_field(f(asked, "radio_group", options=yes_no), prof(), job).value == "Yes", asked


def test_worked_there_before_only_where_the_profile_names_the_employer_itself():
    """The fake-site walks (Oct 2026): the profile's "Example Fab Services" answered Example Fab's "Have you ever been
    employed with Example Fab before?" with Yes, previously, as one name held the other. It's Yes only where a job in
    the profile is at the employer itself, its whole name but for case, punctuation, "The" and its legal words ("Intel
    Corporation" is Intel). A name that only holds the employer's, or is held in it, may be it or not: the question is
    the person's, never a Yes (nor a No). A name that's neither ("Intelligent Fabrication" for Intel) is No."""
    from job_apply.autofill import _same_employer

    same = [("Intel Corporation", "Intel"), ("Microchip Technology Inc.", "Microchip Technology"),
            ("The Example Fab Group, Inc.", "example fab"), ("Example Fab, LLC", "EXAMPLE FAB CO."),
            ("Smith & Sons Holdings", "Smith and Sons"), ("Example Fab Ltd", "Example Fab Corp"),
            ("Wells Fargo & Company", "Wells Fargo")]
    other = [("Example Fab Services", "Example Fab"), ("ASM Pacific Technology", "ASM International"),
             ("Intel", "Intelligent Fabrication"), ("Intel Federal", "Intel"), ("ASM", "ASM Pacific Technology"),
             ("Fab", "Example Fab"), ("", ""), ("Inc", "LLC")]
    for a, b in other:
        assert not _same_employer(a, b) and not _same_employer(b, a), (a, b)
    for a, b in same:
        assert _same_employer(a, b) and _same_employer(b, a), (a, b)

    asked = "Have you ever been employed with Example Fab before?"
    yes_no = ["Yes", "No"]

    def worked(history: list[str], company: str, label: str = asked) -> str | None:
        person = _person(work_history=[{"company": c, "title": "Technician", "start": "2019-01", "end": "2022-05"}
                                       for c in history])
        return _answer(label, person, kind="radio_group", options=yes_no, job={"company": company})

    assert worked(["Example Fab Services"], "Example Fab") is None  # (was Yes)
    assert worked(["Example Fab"], "Example Fab Services") is None
    assert (worked(["Intel"], "Intelligent Fabrication",
                   "Have you ever been employed with Intelligent Fabrication?") == "No")
    assert worked(["ASM Pacific Technology"], "ASM International",
                  "Have you ever worked for ASM International?") is None
    assert worked(["Intel"], "Intel Corporation", "Have you previously been employed by Intel?") == "Yes"
    assert worked(["Microchip Technology Inc."], "Microchip Technology",
                  "Have you previously been employed by Microchip?") == "Yes"
    # a job there now, at a name that only holds the employer's: whether it's this one is the person's
    person = _person(work_history=[{"company": "Example Fab", "title": "Technician", "start": "2019-01",
                                    "end": "2021-05"},
                                   {"company": "Example Fab Services", "title": "Technician", "start": "2021-06",
                                    "end": "present"}])
    asm = ["I am CURRENTLY employed by Example Fab", "I was PREVIOUSLY employed by Example Fab",
           "I have NEVER been employed by Example Fab"]
    assert _answer(asked, person, kind="radio_group", options=asm, job={"company": "Example Fab"}) is None
    # an employee ID kept for one employer goes only on that employer's forms, never on one whose name holds it
    keeps = Profile({"history": {"employee_ids": {"Example Fab": "E1234567"}}})
    assert resolve_field(f("Employee ID (if applicable)"), keeps, {"company": "Example Fab Services"}) is None
    assert resolve_field(f("Employee ID (if applicable)"), keeps, {"company": "Example Fab, Inc."}).value == "E1234567"


@pytest.mark.parametrize("ours,theirs,answer", [
    # may be the same employer: asked, never No (the security review of the framed sign-in)
    ("ASU", "Arizona State University", None), ("TI", "Texas Instruments", None),
    ("APS", "Arizona Public Service", None), ("Global Foundries", "GlobalFoundries", None),
    ("Banner - University Medicine", "Banner Health", None), ("ASM Pacific Technology", "ASM International", None),
    ("Example Fab Services", "Example Fab", None),
    # (and the second review's: short forms with legal words kept, a short name's first word, two first words)
    ("TSMC", "Taiwan Semiconductor Manufacturing Company", None),
    ("TSMC Arizona", "Taiwan Semiconductor Manufacturing Company", None), ("ADI", "Analog Devices, Inc.", None),
    ("TEL", "Tokyo Electron Limited", None), ("Western Alliance Bancorporation", "Western Alliance Bank", None),
    ("State Farm Mutual Automobile Insurance Company", "State Farm Insurance", None),
    ("U of A", "University of Arizona", None), ("UArizona", "University of Arizona", None),
    ("UMC", "University Medical Center of Southern Nevada", None),
    ("UMC Southern Nevada", "University Medical Center of Southern Nevada", None),
    # two employers: No
    ("Intel", "Intelligent Fabrication", "No"), ("Arizona State University", "Arizona Public Service", "No"),
    ("American Express", "American Airlines", "No"), ("United Airlines", "United Health Group", "No"),
    ("ASU", "APS", "No"), ("City of Las Vegas", "City of Henderson", "No"),
    ("Example Fab Services", "Northwind Litho", "No"),
    # (the third review's: never by a state's name, nor a generic second word after a common first one)
    ("Arizona Department of Transportation", "Arizona Department of Education", "No"),
    ("Arizona Board of Regents", "Arizona Board of Nursing", "No"),
    ("Arizona Public Service", "Arizona Public Schools", "No"), ("New York Life", "New York Times", "No"),
    ("New Mexico Gas", "New Mexico State University", "No"), ("North Carolina State", "North Carolina Central", "No"),
    ("United States Postal Service", "United States Air Force", "No"),
    ("Community Health Systems", "Community Health Network", "No"),
    ("National Park Service", "National Park Foundation", "No"),
    # a two-letter first word may be the initials of a name's first words: asked, never No, though most such pairs are
    # two employers (the fourth review: "GD Mission Systems" is General Dynamics Mission Systems', "GE Aerospace"
    # General Electric's, and a wrong No is a false answer where a wrong ask costs a question). Never Yes
    ("TTM Technologies", "TT Electronics", None), ("SK Hynix", "Southwest Key", None),
    ("JB Hunt", "Johnson Brothers", None), ("GE Aerospace", "Global Equities", None),
    ("AB Electronics", "American Bank", None), ("ON Semiconductor", "Oracle Network", None),
    ("ON Semiconductor", "Onsite Nursing", None), ("EE Times", "Electrical Engineering", None),
    ("PF Changs", "Phoenix Fire", None), ("BD Medical", "Banner Desert", None),
    ("ST Microelectronics", "Sound Transit", None), ("JE Dunn", "Jones Electric", None),
    ("GE", "General Electric", None),
    # (the fourth review's: real same employers that round 3 made a firm No)
    ("GD Mission Systems", "General Dynamics Mission Systems", None), ("GE Aerospace", "General Electric", None),
    ("GE Vernova", "General Electric", None), ("GE Healthcare", "General Electric", None),
    ("GE", "General Electric Aviation", None), ("HP Enterprise", "Hewlett Packard Enterprise", None),
    ("TI", "Texas Instruments Tucson", None), ("JB Hunt", "J.B. Hunt Transport", None),
    ("TE Connectivity", "Tyco Electronics", None), ("UA", "University of Arizona Health Sciences", None),
    ("Arizona Public Service", "Arizona Public Svc", None),
    ("Texas Instruments - Sherman", "Texas Instruments Tucson", None),
    ("Arizona State Univ", "Arizona State University", None),
    # the employer itself: Yes
    ("Wells Fargo & Company", "Wells Fargo", "Yes"), ("Texas Instruments Incorporated", "Texas Instruments", "Yes"),
])
def test_an_employer_whose_name_may_be_this_ones_is_asked_never_answered_no(ours, theirs, answer):
    """Worked there before, from a profile job at `ours`, for an employer named `theirs`: No only where neither name may
    be the other's (_may_be_employer: one holding the other, the same but for spaces, one the other's initials, a first
    word of their own in common, never a state's or a common one). "ASU" for Arizona State University
    was answered No."""
    from job_apply.autofill import _may_be_employer, works_there_now

    person = _person(work_history=[{"company": ours, "title": "Technician", "start": "2019-01", "end": "2022-05"}])
    asked = f"Have you ever been employed by {theirs}?"
    assert _answer(asked, person, kind="radio_group", options=["Yes", "No"], job={"company": theirs}) == answer
    assert _may_be_employer(ours, theirs) == _may_be_employer(theirs, ours) == (answer != "No")
    # a "new candidate, not a current employee" box: never for someone whose job now may be there
    now = _person(work_history=[], experience={"current_company": ours})
    assert works_there_now(now, theirs) == (answer != "No")


def test_not_on_the_staff_of_an_employer_the_persons_own_may_be_is_asked():
    """The second review's probe: someone at "GCU" now, applying to Grand Canyon Education, was answered "I am not a
    current Grand Canyon University employee": the choice named an employer that may be the job's, but the person's own
    (GCU) may be the one it names. Asked. And where the person's job may be the one named, past or present."""
    options = ["Yes", "No", "I am not a current Grand Canyon University employee"]
    asked = "Have you notified your leader of your application? (internal candidates only)"
    gcu = _person(work_history=[{"company": "GCU", "title": "Technician", "start": "2020-01", "end": "present"}])
    assert _answer(asked, gcu, kind="radio_group", options=options, job={"company": "Grand Canyon Education"}) is None
    from job_apply.autofill import _not_staff_choice

    field = {"label": asked, "options": options}
    assert _not_staff_choice(field, gcu, {"company": "Grand Canyon Education"}) is None
    intel = _person(work_history=[{"company": "Intel", "title": "Technician", "start": "2020-01", "end": "present"}])
    ti = {"label": "Current employees only: have you told your manager?",
          "options": ["Yes", "No", "I am not a current TI employee"]}
    # (as before)
    assert _not_staff_choice(ti, intel, {"company": "Texas Instruments"}) == "I am not a current TI employee"


def test_a_past_job_at_the_employer_and_one_now_that_may_be_it_is_asked():
    """A past job at Banner Health and one now at Banner Bank, applying to Banner Health: "Yes, previously" may be
    "currently" (Banner Bank may be Banner Health's, by the wider may-be), so it's the person's."""
    person = _person(work_history=[{"company": "Banner Health", "title": "Technician", "start": "2015-01",
                                    "end": "2019-01"},
                                   {"company": "Banner Bank", "title": "Teller", "start": "2019-02", "end": "present"}])
    options = ["I am CURRENTLY employed by Banner Health", "I was PREVIOUSLY employed by Banner Health",
               "I have NEVER been employed by Banner Health"]
    assert _answer("Have you ever been employed with Banner Health before?", person, kind="radio_group",
                   options=options, job={"company": "Banner Health"}) is None


def test_a_current_company_is_an_employer_worked_for():
    """experience.current_company was left out of the employers worked for: someone at Intel now answered No to "Have
    you ever been employed by Intel?" and to "Are you a current or former employee?"."""
    person = _person(work_history=[], experience={"current_company": "Intel"})
    for asked in ("Have you ever been employed by Intel?", "Are you a current or former employee?"):
        assert _answer(asked, person, kind="radio_group", options=["Yes", "No"], job={"company": "Intel Corporation"}) \
            == "Yes", asked
    asm = ["I am CURRENTLY employed by Intel", "I was PREVIOUSLY employed by Intel",
           "I have NEVER been employed by Intel"]
    assert _answer("Have you ever been employed with Intel before?", person, kind="radio_group", options=asm,
                   job={"company": "Intel"}) == "I am CURRENTLY employed by Intel"


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


_WWID = "If you have previously worked for Example Fab in any capacity, please provide your WWID"


def _id_plan(label, person, company="Example Fab", **box):
    """plan_autofill's needs_input for one box asking `label` on `company`'s form: by label, with its flags."""
    plan = plan_autofill([{"id": "w", "kind": "text", "label": label, "value": "", "required": False, **box}], person,
                         {"company": company})
    return {q["label"]: q for q in plan["needs_input"]}, {t["label"]: t["value"] for t in plan["to_fill"]}


def test_an_optional_id_box_at_the_employer_is_the_persons_to_give():
    """Intel's Workday review page (the owner, Oct 21: "If something asks for your WWID I'd like it to ask") left "If
    you have previously worked for Intel Corporation in any capacity, please provide your WWID" empty as optional, the
    profile saying the person had worked there. An optional box for their ID at this employer is theirs to give
    ("employer_id": the desk asks it) where the profile says they worked here, or can't say; never where it says they
    never did. A required one is asked as before; an ordinary optional question isn't asked; and nothing fills one."""
    there = _person(work_history=[{"company": "Example Fab", "title": "Technician", "start": "2019-01",
                                   "end": "2022-05"}])
    never = _person(work_history=[{"company": "Northwind Litho", "title": "Technician", "start": "2019-01",
                                   "end": "2022-05"}])
    unsure = _person(work_history=[{"company": "Example Fab Services", "title": "Technician", "start": "2019-01",
                                    "end": "2022-05"}])
    for person, asked in ((there, True), (unsure, True), (never, False)):
        needs, filled = _id_plan(_WWID, person)
        assert bool(needs[_WWID].get("employer_id")) == asked and filled == {}, (person.data["work_history"], needs)
    needs, filled = _id_plan(_WWID, never, required=True)  # (required: asked, as before)
    assert needs[_WWID]["required"] is True and filled == {}
    needs, _ = _id_plan("Favorite tool color", there)  # (an ordinary optional question: left empty, as before)
    assert not needs["Favorite tool color"].get("employer_id")
    # its other wordings, about this employer or "us"
    for label in ("WWID", "Worldwide ID", "Employee ID (if previously employed)", "Employee Number", "Worker ID",
                  "Badge number", "Personnel number", "Former Employee ID", "Previous employee ID",
                  "If you were previously employed by the company, enter your employee ID",
                  "Have you worked with us before? Employee number:",
                  "If you are a former employee of Example Fab, please enter your employee ID"):
        needs, filled = _id_plan(label, there)
        assert needs[label].get("employer_id") and filled == {}, label
    # never another employer's ID, someone else's, or one in a past job's block: left empty, never filled
    for label, box in (("Employee ID at your previous employer", {}), ("Referring Employee ID", {}),
                       ("Your manager's employee number", {}), ("Employee ID", {"section": "Work Experience 1"})):
        needs, filled = _id_plan(label, there, **box)
        assert not needs[label].get("employer_id") and filled == {}, label
    needs, filled = _id_plan(_WWID, _person(work_history=[{"company": "Other Fab", "title": "Technician",
                                                           "start": "2019-01", "end": "2022-05"}]), "Other Fab")
    assert not needs[_WWID].get("employer_id") and filled == {}  # (Example Fab's, on Other Fab's form)


def test_an_id_box_never_takes_another_rules_answer():
    """"If you were previously employed by the company, enter your employee ID" (a text box) was answered "Yes,
    previously" by the rule for "have you worked here before?", its "company" read as another party's (a Company
    Name's): an ID box takes only the person's ID at the employer, or nothing."""
    there = _person(work_history=[{"company": "Example Fab", "title": "Technician", "start": "2019-01",
                                   "end": "2022-05"}])
    label = "If you were previously employed by the company, enter your employee ID"
    assert _answer(label, there, job={"company": "Example Fab"}) is None
    keeps = Profile({**there.data, "history": {"employee_ids": {"Example Fab": "12345678"}}})
    assert _answer(label, keeps, job={"company": "Example Fab"}) == "12345678"
    # nor the profile's ID at this employer in a box for another's
    assert _answer("Employee ID at your previous employer", keeps, job={"company": "Example Fab"}) is None
    assert _answer("Employee ID", keeps, job={"company": "Example Fab"}, section="Work Experience 1") is None


def test_an_id_remembered_at_one_employer_is_used_only_there(job_apply_home):
    """An employee ID is the person's own only at the employer that gave it: answered on the card, it's kept with that
    employer (answers.yaml's `at`) and fills the box at that employer's next application (its whole name, or a short
    name its list entry gives it), never at another's with the same wording, which asks the person there instead. Kept
    at two employers, each keeps its own. One kept for every employer (written by hand) is never an ID box's; other
    answers are as they were."""
    from job_apply import config

    label = "Employee ID (if previously employed)"
    entry = config.save_answer(label, "12345678", "Example Fab", at="Example Fab")
    assert entry["at"] == "Example Fab"

    def worked(*employers):
        return Profile({**config.Profile.load().data, "work_history": [
            {"company": c, "title": "Technician", "start": "2019-01", "end": "2022-05"} for c in employers]})
    person = worked("Example Fab", "Other Fab")
    needs, filled = _id_plan(label, person, "Example Fab, Inc.")
    assert filled == {label: "12345678"} and needs == {}  # (filled, never asked)
    needs, filled = _id_plan(label, person, "Other Fab")
    assert filled == {} and needs[label].get("employer_id")  # (asked there: they worked at Other Fab too)
    needs, filled = _id_plan(label, worked("Example Fab"), "Other Fab")
    assert filled == {} and not needs[label].get("employer_id")  # (and left empty where they never worked)
    # the same question answered at Other Fab: both kept, each for its own employer
    config.save_answer(label, "87654321", "Other Fab", at="Other Fab")
    person = worked("Example Fab", "Other Fab")
    assert _id_plan(label, person, "Other Fab")[1] == {label: "87654321"}
    assert _id_plan(label, person, "Example Fab")[1] == {label: "12345678"}
    # answered again at Example Fab: its own replaced, Other Fab's kept
    config.save_answer(label, "11112222", "Example Fab", at="Example Fab")
    assert sorted((a["answer"], a["at"]) for a in config.saved_answers()) == [
        ("11112222", "Example Fab"), ("87654321", "Other Fab")]
    # one written by hand for every employer, or kept for another, is never an ID box's; one given at this employer
    # before IDs were kept with theirs (its `from`) still is
    for kept, used in (({"match": "WWID", "answer": "55556666"}, False),
                       ({"match": "wwid", "answer": "55556666", "question": "WWID", "from": "Example Fab"}, True),
                       ({"match": "wwid", "answer": "55556666", "question": "WWID", "from": "Other Fab"}, False)):
        alone = Profile({**worked("Example Fab").data, "answers": [kept]})
        got = resolve_field(f("WWID"), alone, {"company": "Example Fab"})
        assert (got and got.value) == ("55556666" if used else None), kept
    # an answer kept for an employer alone (by hand, to any question) goes only on that employer's forms
    alone = Profile({**worked().data, "answers": [{"match": "tool color", "answer": "Blue", "at": "Example Fab"}]})
    assert resolve_field(f("Favorite tool color"), alone, {"company": "Example Fab"}).value == "Blue"
    assert resolve_field(f("Favorite tool color"), alone, {"company": "Other Fab"}) is None
    # and any other answer is as it was
    alone = Profile({**worked().data, "answers": [{"match": "tool color", "answer": "Blue"}]})
    assert resolve_field(f("Favorite tool color"), alone, {"company": "Other Fab"}).value == "Blue"


def test_an_id_kept_under_an_employers_short_name_is_that_employers(monkeypatch):
    """The employer's short names from its list entry (aliases) name it too: an ID kept "at" UCT is Ultra Clean
    Holdings'."""
    from job_apply import autofill

    monkeypatch.setattr(autofill, "employer_aliases",
                        lambda company: ["uct"] if autofill.norm(company) == "ultra clean holdings" else [])
    assert autofill.same_employer_as("UCT", "Ultra Clean Holdings")
    assert autofill.same_employer_as("Ultra Clean Holdings", "UCT")
    assert not autofill.same_employer_as("UCT", "Example Fab") and not autofill.same_employer_as("", "")
    keeps = Profile({"history": {"employee_ids": {"UCT": "12345678"}}})
    assert _answer("Badge number", keeps, job={"company": "Ultra Clean Holdings"}) == "12345678"


def _at_example_fab():
    """A made-up person who worked at Example Fab and works at Northwind Litho now, with Example Fab's ID in the
    profile, and the same ID remembered from Example Fab's card for "Employee ID" (as Desk.answer keeps it)."""
    return Profile({"work_history": [
        {"company": "Example Fab", "title": "Technician", "start": "2019-01", "end": "2022-05"},
        {"company": "Northwind Litho", "title": "Technician", "start": "2022-06", "end": "present"}],
        "history": {"employee_ids": {"Example Fab": "12345678"}},
        "answers": [{"match": r"^Employee\W+ID$", "question": "Employee ID", "answer": "12345678",
                     "from": "Example Fab", "at": "Example Fab"}]})


@pytest.mark.parametrize("box", [
    {"label": "Employee ID", "section": "References"}, {"label": "Employee ID", "section": "Employment History"},
    {"label": "Employee ID", "section": "Most Recent Employer"},
    {"label": "Employee ID", "section": "Work Experience 1"},
    {"label": "Current Employer Employee ID"}, {"label": "Previous Employer Employee ID"},
    {"label": "Employee ID (previous company)"},
    {"label": "Employee ID", "kind": "radio_group", "options": ["Yes", "No"]},
    {"label": "Employee ID", "kind": "checkbox"}])
def test_an_id_kept_for_this_employer_goes_only_into_its_own_id_box(box):
    """The review of the ID's change (M1, M2): an ID kept for Example Fab (answers.yaml's `at`, or the profile's
    employee_ids) went into every "Employee ID" on its form, under References, Employment History and Most Recent
    Employer (another employer's block) too, read from the label alone; and "Current Employer Employee ID", "Previous
    Employer Employee ID" took it, which main had left empty. The box is told by the whole of it (its section and kind
    too): only the person's own ID box at this employer takes it, and another employer's or someone else's is neither
    filled nor asked as optional. Only a text box is an ID box."""
    from job_apply.autofill import employer_id_box

    person = _at_example_fab()
    field = {"id": "b", "kind": "text", "value": "", "required": False, **box}
    assert employer_id_box(field, {"company": "Example Fab"}) is not True, box
    plan = plan_autofill([field], person, {"company": "Example Fab"})
    assert plan["to_fill"] == [] and not any(q.get("employer_id") for q in plan["needs_input"]), (box, plan)
    for kept in (person, Profile({**person.data, "answers": []}), Profile({**person.data, "history": {}})):
        got = resolve_field(field, kept, {"company": "Example Fab"})
        assert got is None or got.value != "12345678", (box, got)
    # (its own ID box, beside them, takes it)
    own = {"id": "a", "kind": "text", "label": "Employee ID", "value": "", "required": False}
    assert [t["value"] for t in plan_autofill([own, field], person, {"company": "Example Fab"})["to_fill"]] == [
        "12345678"]


@pytest.mark.parametrize("label,company,whose", [
    # (the review's M3: "for", "of" or "at" with no organization named says nothing of another employer)
    ("Employee ID (for former employees)", "Example Fab", True),
    ("Employee ID for former employees", "Example Fab", True),
    ("Employee Number (for rehires only)", "Example Fab", True),
    ("Employee ID for internal applicants", "Example Fab", True),
    ("Employee ID - for current employees only", "Example Fab", True),
    ("Employee ID of former employee", "Example Fab", True), ("Employee ID for a former employee", "Example Fab", True),
    ("EMPLOYEE ID (FOR FORMER EMPLOYEES)", "Example Fab", True),
    ("Employee ID If Previously Employed With Us", "Example Fab", True),
    ("Employee ID at Example Fab", "Example Fab", True),
    ("If you were employed by an Example Fab subsidiary, provide your employee ID", "Example Fab", True),
    # (this employer's short names: its initials, a short name its list entry or its own name gives)
    ("TI Employee ID (if applicable)", "Texas Instruments", True),
    ("Former TI employee? Your TI employee ID", "Texas Instruments", True),
    ("ASU Employee ID (if applicable)", "Arizona State University", True),
    ("AMD employee ID", "Advanced Micro Devices", True), ("SRP employee number", "Salt River Project", True),
    ("AMAT employee number", "Applied Materials", True), ("UCT employee ID", "Ultra Clean Holdings (UCT)", True),
    ("Example Fab's employee ID", "Example Fab", True),
    # (the review's LOW 3: wordings missed)
    ("Employee # (if applicable)", "Example Fab", True), ("Badge #", "Example Fab", True),
    ("Team member ID", "Example Fab", True),
    ("If you have previously worked for Example Fab in any capacity (including through an agency), please provide "
     "your WWID", "Example Fab", True),
    # (the second review's: words in capitals alone name no one; a contraction is no possessive; the applicant's,
    # "their" where the label never says "you", and this employer's own are the person's)
    ("What's your employee ID?", "Example Fab", True), ("What's your WWID?", "Example Fab", True),
    ("Former employees should enter their employee ID", "Example Fab", True),
    ("Applicant's Employee ID (if a current employee)", "Example Fab", True),
    ("Example Fab Corporation's WWID", "Example Fab", True), ("Ex-Employee ID", "Example Fab", True),
    ("Retiree Employee ID", "Example Fab", True), ("Personal Employee Number", "Example Fab", True),
    ("Previously Employed Employee ID", "Example Fab", True), ("Employee ID For Re-Hire", "Example Fab", True),
    ("Employee ID For Previously Employed Applicants", "Example Fab", True),
    ("Security Badge Number", "Example Fab", True), ("Microchip Employee ID", "Example Fab", True),
    ("DOE BADGE NUMBER", "Example Fab", True),
    ("If you are a former company employee, enter your employee ID", "Example Fab", True),
    ("Current company employees: badge number", "Example Fab", True),
    ("Employee ID (if you worked for us at any other company in our group)", "Example Fab", True),
    # another employer's: said so (the review's M2), or an organization named that isn't this one: on the lists (Intel,
    # onsemi, the City of Phoenix), "a" one worked for, or a name with an organization's word
    ("Employee ID At Your Previous Employer", "Example Fab", False),
    ("Current Employer Employee ID", "Example Fab", False), ("Employee ID (previous company)", "Example Fab", False),
    ("If you worked for intel, provide your WWID", "Microchip Technology", False),
    ("former onsemi employee number", "Microchip Technology", False),
    ("If you worked for the City of Phoenix, provide your employee number", "City of Mesa", False),
    ("Employee ID at University of Phoenix", "University of Arizona", False),
    ("Arizona Department of Corrections Employee ID", "Arizona State University", False),
    ("Banner Bank Employee ID", "Banner Health", False),
    ("If you have worked for a government contractor, provide your employee ID", "Example Fab", False),
    # someone else's, or no employee's ID at all (the review's LOW 2)
    ("Friend's employee ID", "Example Fab", None), ("Sponsor's employee ID", "Example Fab", None),
    ("If you were referred, give their WWID", "Example Fab", None),
    ("If an employee told you about this job, enter their employee number", "Example Fab", None),
    ("Employee Identification Number (EIN)", "Example Fab", None),
    # an agency's or a recruiter's, by the words just before the ID (a mutation of the review's got past these)
    ("Agency badge number", "Example Fab", None), ("Recruiter employee ID", "Example Fab", None),
])
def test_whose_id_a_box_asks_is_told_by_its_words(label, company, whose):
    """employer_id_box: the person's own ID at the employer applied to (True) unless its words say otherwise: another
    employer's (False: said so, or an organization named that isn't this one), or no such box (None: someone else's, a
    tax ID). Where its words name this one's own ID (id_names_this_employer), the profile's ID for it fills it; where
    they may name another organization's ("Microchip Employee ID", "DOE BADGE NUMBER": the third review's), it's asked,
    never filled; anywhere else nothing does."""
    from job_apply.autofill import employer_id_box, id_names_this_employer

    assert employer_id_box({"label": label, "kind": "text"}, {"company": company}) is whose, label
    sure = whose is True and label not in ("Microchip Employee ID", "DOE BADGE NUMBER")
    assert not whose or id_names_this_employer({"label": label}, {"company": company}) is sure, label
    keeps = Profile({"history": {"employee_ids": {company: "12345678"}}})
    got = resolve_field(f(label), keeps, {"company": company})
    assert (got and got.value) == ("12345678" if sure else None), label


@pytest.mark.parametrize("company,label,other", [
    ("Intel", "Microchip Employee ID", "Microchip Technology"), ("Intel", "Microchip WWID", "Microchip Technology"),
    ("Intel", "Your employee ID at Microchip", "Microchip Technology"), ("Intel", "Lam employee ID", "Lam Research"),
    ("Intel", "Amkor employee ID", "Amkor Technology"), ("Intel", "AMD Employee ID", "Advanced Micro Devices"),
    ("Intel", "TI badge number", "Texas Instruments"), ("Intel", "Mobileye employee ID", None),
    ("Intel", "Federal employee ID", None),
    ("Intel", "If you are a current or former federal employee, enter your federal employee ID", None),
    ("Intel", "DOE BADGE NUMBER", None), ("Intel", "State employee ID", None),
    ("Intel", "Employee ID at Intel Federal", None), ("Intel", "Employee ID (Intel Federal employees)", None),
    ("Raytheon", "Northrop badge number", "Northrop Grumman"), ("Texas Instruments", "Texas A&M Employee ID", None),
    ("Microchip Technology", "Former Atmel employee number", None)])
def test_this_employers_id_never_goes_into_a_box_that_may_be_another_organizations(job_apply_home, company, label,
                                                                                   other):
    """The third review's MEDIUM: on Intel's form, with `employee_ids: {Intel: ...}`, the ID went into "Microchip
    Employee ID", "Lam employee ID", "TI badge number", "Federal employee ID", "DOE BADGE NUMBER", "Employee ID at Intel
    Federal" (and "Northrop badge number" on Raytheon's, "Texas A&M Employee ID" on Texas Instruments'): a short name,
    an acronym or a government's word may make it another organization's ID. Asking is harmless, filling another's box
    is not: nothing fills it by itself (the profile's employee_ids, an ID kept for this employer, an "N/A" written for
    every employer), and an optional one is asked as before where the person worked here (or the profile can't say);
    its answer is for this application only."""
    history = [{"company": company, "title": "Technician", "start": "2019-01", "end": "2020-01"}]
    if other:
        history.append({"company": other, "title": "Technician", "start": "2020-02", "end": "2022-01"})
    kept = [{"match": "x", "question": label, "answer": "12345678", "from": company, "at": company},
            {"match": "employee id|badge number|wwid", "answer": "N/A"}]
    person = Profile({"work_history": history, "history": {"employee_ids": {company: "12345678"}}, "answers": kept})
    assert resolve_field(f(label), person, {"company": company}) is None, label
    needs, filled = _id_plan(label, person, company)
    assert filled == {} and needs[label].get("employer_id") and needs[label].get("id_unsure"), (label, needs)
    assert not needs[label].get("own_id"), label
    needs, _ = _id_plan(label, person, company, required=True)  # (required: asked, as any required box)
    assert needs[label]["required"] and not needs[label].get("own_id"), label


def test_an_id_box_naming_this_employers_own_id_is_filled_from_the_profile():
    """The third review's: the round-2 wordings that name this employer's own ID, by their plain words, "us", or its own
    name, short name or initials, are filled from the profile's employee_ids by themselves, as before."""
    from job_apply.autofill import id_names_this_employer

    labels = ["Employee ID", "WWID (if applicable)", "Employee ID (for former employees)", "What's your employee ID?",
              "If you are a former company employee, enter your employee ID", "Employee ID (if you worked with us)",
              "Employee ID For Previously Employed Applicants", "Ex-Employee ID", "Retiree Employee ID",
              "Personal Employee Number", "Employee ID For Re-Hire", "Former employees should enter their employee ID",
              "Applicant's Employee ID (if a current employee)", "Previously Employed Employee ID",
              "Intel Corporation's WWID", "Security Badge Number", "Intel employee ID",
              "If you have previously worked for Intel Corporation in any capacity (including through an agency), "
              "please provide your WWID"]
    person = Profile({"work_history": [{"company": "Intel", "title": "T", "start": "2019-01", "end": "2022-05"}],
                      "history": {"employee_ids": {"Intel": "12345678"}}})
    for label in labels:
        assert id_names_this_employer({"label": label}, {"company": "Intel"}), label
        assert resolve_field(f(label), person, {"company": "Intel"}).value == "12345678", label
    for label, company in (("TI badge number", "Texas Instruments"), ("State employee ID", "State of Arizona"),
                           ("AMD Employee ID", "Advanced Micro Devices")):
        assert id_names_this_employer({"label": label}, {"company": company}), label


def test_an_id_box_naming_one_of_the_persons_other_employers_is_that_employers():
    """The second review's (d): an ID box that names one of the person's own other employers (the profile's, compared
    whole: "City of Phoenix" isn't "City of Mesa", "Example Fab Services" isn't Example Fab) is that employer's, not
    this one's: neither filled nor asked. Without it in the profile, words alone ("Example Fab Services") name no
    one."""
    from job_apply.autofill import employer_id_box

    person = Profile({"work_history": [
        {"company": "Example Fab Services", "title": "Technician", "start": "2016-01", "end": "2018-12"},
        {"company": "Northwind Litho", "title": "Technician", "start": "2019-01", "end": "2022-05"},
        {"company": "City of Phoenix", "title": "Technician", "start": "2022-06", "end": "present"}],
        "history": {"employee_ids": {"Example Fab": "12345678", "City of Mesa": "87654321"}}})
    for label, company in (("Employee ID at Example Fab Services", "Example Fab"),
                           ("northwind litho employee number", "Example Fab"),
                           ("If you worked for the City of Phoenix, your employee number", "City of Mesa")):
        assert employer_id_box({"label": label, "kind": "text"}, {"company": company}, person) is False, label
        assert resolve_field(f(label), person, {"company": company}) is None, label
        needs, _ = _id_plan(label, person, company)
        assert not needs[label].get("employer_id"), label
    assert employer_id_box({"label": "Employee ID at Example Fab Services", "kind": "text"},
                           {"company": "Example Fab"}) is True
    # (this employer's own name, the longest, beside another's that it holds: Example Fab Services' form)
    worked = Profile({"work_history": [{"company": "Example Fab", "title": "T", "start": "2016-01", "end": "2018-12"}]})
    assert employer_id_box({"label": "Employee ID at Example Fab Services", "kind": "text"},
                           {"company": "Example Fab Services"}, worked) is True


def test_a_long_notice_that_mentions_an_id_is_no_id_box():
    """A label longer than one box's question (ID_LABEL_MAX) is a notice that mentions an employee number in passing,
    never asked nor filled as one."""
    from job_apply.autofill import ID_LABEL_MAX, employer_id_box, norm

    notice = ("Example Fab keeps the details you give here, and its recruiters may compare them with its records of "
              "former employees by name, employee number and dates of work, as its privacy notice sets out; say "
              "below if you want a copy of that notice.")
    assert len(norm(notice)) > ID_LABEL_MAX
    assert employer_id_box({"label": notice, "kind": "text"}, {"company": "Example Fab"}) is None
    needs, filled = _id_plan(notice, _at_example_fab())
    assert filled == {} and not needs[notice].get("employer_id") and not needs[notice].get("own_id")


def test_an_id_box_whose_question_takes_in_subsidiaries_is_asked_only_of_someone_who_may_have_worked_there():
    """The review's LOW 1: "...Example Fab or any of its subsidiaries, provide your employee ID" made the profile's
    answer "can't say", so it was asked of everyone. A subsidiary clause alone is no reason to ask: left empty where the
    profile has no employer that is or may be this one, asked where it has one."""
    label = "If you have worked for Example Fab or any of its subsidiaries, provide your employee ID"
    stranger = _person(work_history=[{"company": "Northwind Litho", "title": "Technician", "start": "2019-01",
                                      "end": "2022-05"}])
    needs, _ = _id_plan(label, stranger)
    assert needs[label].get("own_id") and not needs[label].get("employer_id")
    for worked in ("Example Fab", "Example Fab Services"):
        person = _person(work_history=[{"company": worked, "title": "Technician", "start": "2019-01",
                                        "end": "2022-05"}])
        assert _id_plan(label, person)[0][label].get("employer_id"), worked


def test_an_id_left_empty_at_an_employer_is_not_asked_there_again(job_apply_home):
    """The review's LOW 1: an optional ID box left empty on the card with Remember ticked is kept so (an answers.yaml
    entry `at` that employer with no answer): never asked at that employer again ("left_here"), as at another."""
    from job_apply import config

    label = "Employee ID (if previously employed)"
    config.save_answer(label, None, "Example Fab", at="Example Fab")

    def worked(*employers):
        return Profile({**config.Profile.load().data, "work_history": [
            {"company": c, "title": "Technician", "start": "2019-01", "end": "2022-05"} for c in employers]})
    person = worked("Example Fab", "Other Fab")
    needs, filled = _id_plan(label, person, "Example Fab, Inc.")
    assert filled == {} and needs[label].get("left_here") and not needs[label].get("employer_id")
    needs, _ = _id_plan(label, person, "Other Fab")
    assert needs[label].get("employer_id") and not needs[label].get("left_here")
    # answered later at Example Fab: the answer takes the empty one's place
    config.save_answer(label, "12345678", "Example Fab", at="Example Fab")
    assert _id_plan(label, worked("Example Fab"), "Example Fab")[1] == {label: "12345678"}
    assert [(a["answer"], a["at"]) for a in config.saved_answers()] == [("12345678", "Example Fab")]


def test_an_answer_written_for_every_employer_fills_an_id_box_only_where_it_says_none():
    """The review's LOW 4: a hand-written `{match: "employee id", answer: "N/A"}` (no `at` nor `from`) filled required
    Employee ID boxes on main, and then never did. It does where it says there's none ("N/A", "None", "Not
    applicable"); a number written for every employer never goes into an ID box."""
    for answer, used in (("N/A", True), ("None", True), ("Not applicable", True), ("12345678", False)):
        person = Profile({"answers": [{"match": "employee id", "answer": answer}]})
        for company in ("Example Fab", "Other Fab"):
            got = resolve_field(f("Employee ID", required=True), person, {"company": company})
            assert (got and got.value) == (answer if used else None), (answer, company)
    # (the second review's LOW 3: the profile's ID for this employer comes before an "N/A" written for every one)
    person = Profile({"answers": [{"match": "employee id", "answer": "N/A"}],
                      "history": {"employee_ids": {"Example Fab": "12345678"}}})
    assert resolve_field(f("Employee ID", required=True), person, {"company": "Example Fab"}).value == "12345678"
    assert resolve_field(f("Employee ID", required=True), person, {"company": "Other Fab"}).value == "N/A"


def test_an_id_kept_for_an_employer_never_goes_into_someone_elses_box_nor_a_reference():
    """The second review's R09 and R10 (mutations its tests let past): an ID kept for Example Fab by a pattern
    (answers.yaml's `at`) never goes into a box for someone else's ID there, and one given on its card before IDs were
    kept with `at` (its `from` alone) never goes into an "Employee ID" under References."""
    kept = Profile({"answers": [{"match": "employee id", "answer": "12345678", "at": "Example Fab"}]})
    for label in ("Referring Employee ID", "Friend's employee ID"):
        assert resolve_field(f(label), kept, {"company": "Example Fab"}) is None, label
    before = Profile({"answers": [{"match": r"^Employee\W+ID$", "question": "Employee ID", "answer": "12345678",
                                   "from": "Example Fab"}]})
    assert resolve_field(f("Employee ID", section="References"), before, {"company": "Example Fab"}) is None
    assert resolve_field(f("Employee ID"), before, {"company": "Example Fab"}).value == "12345678"


def test_a_long_label_asking_an_id_is_read_by_its_question_never_answered_yes_or_no():
    """The second review's MEDIUM 3: a text box's label over ID_LABEL_MAX characters, "If you have previously worked for
    Intel Corporation in any capacity, including as an intern, a contractor or through an agency, please provide your
    WWID so that we can locate your records", got "Yes, previously". Its clause that asks (or its last sentence) asks an
    ID, so it's an ID box whatever its length: the profile's ID for the employer, or asked, never a Yes or No."""
    from job_apply.autofill import ID_LABEL_MAX, employer_id_box, norm

    label = ("If you have previously worked for Example Fab in any capacity, including as an intern, a contractor or "
             "through an agency, please provide your WWID so that we can locate your records")
    assert len(norm(label)) > ID_LABEL_MAX
    worked = {"work_history": [{"company": "Example Fab", "title": "Technician", "start": "2019-01", "end": "2022-05"}]}
    assert employer_id_box({"label": label, "kind": "text"}, {"company": "Example Fab"}, Profile(worked)) is True
    assert resolve_field(f(label), Profile(worked), {"company": "Example Fab"}) is None
    needs, filled = _id_plan(label, Profile(worked))
    assert needs[label].get("employer_id") and filled == {}
    keeps = Profile({**worked, "history": {"employee_ids": {"Example Fab": "12345678"}}})
    assert resolve_field(f(label), keeps, {"company": "Example Fab"}).value == "12345678"
    # (its last sentence, where no clause says "provide" or "enter")
    told = ("Example Fab keeps the records of everyone who has worked for it, in any capacity and at any of its sites, "
            "for as long as the law requires, and its recruiters look at them for each application. Your WWID?")
    assert len(norm(told)) > ID_LABEL_MAX and employer_id_box({"label": told}, {"company": "Example Fab"}) is True


@pytest.mark.parametrize("kind,label,answer", [
    ("radio_group", "Have you worked here before? If yes, enter your employee ID below", "Yes"),
    ("select", "Have you worked here before? If yes, enter your employee ID below", "Yes"),
    ("checkbox", "I am a current employee and will enter my employee ID", True)])
def test_a_list_or_a_tick_box_mentioning_an_id_is_answered_as_main_answers_it(kind, label, answer):
    """The second review's MEDIUM 4: a list, a radio group or a tick box whose words mention an employee ID is no ID
    box: its remembered answer (one given on the desk before, its `from` this employer) fills it, as on main."""
    from job_apply.autofill import employer_id_box

    person = Profile({"answers": [{"match": "q", "question": label, "answer": "Yes", "from": "Example Fab"}]})
    box = {**f(label, kind), **({"options": ["Yes", "No"]} if kind != "checkbox" else {"value": False})}
    assert employer_id_box(box, {"company": "Example Fab"}) is None
    assert resolve_field(box, person, {"company": "Example Fab"}).value == answer


def test_remembered_ids_are_the_answers_that_are_ids():
    """The review's LOW 5: the IDs the person gave the desk live in answers.yaml, so the scrubbers take them out with
    the profile's employee_ids: each kept for an employer (`at`), and each other answer to a question asking an ID (or a
    pattern written by hand, a regular expression too) that is one, with a figure in it: never one that says none, a
    list's choice ("Yes", "Unknown": the second review's LOW 1), nor another answer."""
    from job_apply.autofill import remembered_ids

    person = Profile({"answers": [
        {"match": "x", "question": "Employee ID", "answer": "12345678", "at": "Example Fab"},
        {"match": "wwid", "question": "WWID", "answer": "EF-2468", "from": "Example Fab"},
        {"match": "badge number", "answer": "B-1357"}, {"match": r"employee\s*id", "answer": "HAND-5555"},
        {"match": "employee id", "answer": "N/A"},
        {"match": "y", "question": "Employee ID", "answer": None, "at": "Other Fab"},
        {"match": "z", "question": "Are you a current employee? If yes, enter your employee ID below", "answer": "Yes",
         "from": "Example Fab"},
        {"match": "w", "question": "Do you know your WWID?", "answer": "Unknown", "from": "Example Fab"},
        {"match": "tool color", "answer": "Blue"}]})
    assert remembered_ids(person) == ["12345678", "EF-2468", "B-1357", "HAND-5555"]


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
            "employer (or other company), such as a non-competition, non-disclosure, or non-solicitation agreement, "
            "that impact or interfere with your ability to work for the Company?",
            "Do you own, control, or have an economic interest in any intellectual property rights (patents, "
            "trademarks, or copyrights)?",
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
    for other in ("Home Phone", "Work Phone", "Business Phone", "Office Phone Number", "Phone (Home)",
                  "Phone Number - Work", "Telephone (Work)", "Home Number"):
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
    for label in ("Mobile Phone", "Mobile", "Cell Phone", "Cellular Number", "Mobile Number *", "Phone (Mobile)",
                  _KFORCE_CELL, "Cell Phone Number Please include the area code"):
        assert resolve_field(f(label), prof()).value == number, label
    home = Profile({"personal": {"phone": number, "phone_type": "Home phone"}})
    assert resolve_field(f("Home Phone Number"), home).value == number
    # a long label is a phone box only when it names one, not when it only starts with a word of a type
    for label in ("Mobile devices and cell phone systems you have installed or repaired in the last ten years",
                  "Work history, in your own words, for the last ten years: employer, title and dates"):
        assert resolve_field(f(label), prof()) is None, label


def test_a_long_box_that_asks_for_the_phone_number_takes_the_profiles():
    """Edward Jones' BrassRing (run 86): its personal information step asks, in a long label, "... Please enter the
    phone number you would like to use to receive your code(s) in the field below. Please enter the phone number in the
    following format: ###-###-####. NOTE: Phone number MUST include dashes." No rule took a label that long, so it went
    on the card. It's the profile's phone, written with dashes where the box shows ###-###-####."""
    label = ("As you go through the Selection Process, you may be required to enter an Authentication Code sent to you "
             "by Example Corp in order to sign documents. Please enter the phone number you would like to use to "
             "receive your code(s) in the field below. Please enter the phone number in the following format: "
             "###-###-####. NOTE: Phone number MUST include dashes.")
    for written in ("480-555-0123", "4805550123", "+1 480 555 0123", "(480) 555-0123"):
        p = Profile({"personal": {"phone": written, "phone_country_code": "+1"}})
        assert resolve_field(f(label), p).value == "480-555-0123", written
    # never another's number, another kind's, a code's, an explanation's, or one that agrees to texts
    # or calls (the review)
    for other in ("Please enter your phone number verification code below, as sent to you by text message",
                  "Please enter your mobile number to sign up for job alerts by text message from Example Corp",
                  "Please enter the phone number of your next of kin, in case we cannot reach you directly",
                  "Please enter the phone number of your spouse or domestic partner for our benefits records",
                  "If you are under 18, please enter the phone number of a parent or guardian who can consent",
                  "Do you have any relatives employed by Example Corp? If yes, please enter the name and phone number",
                  "Please enter the name and phone number of a person who can verify your employment history",
                  "Please enter the phone number of the recruiter or agency that referred you to this position",
                  "To help us process your application, please enter your work phone number in the box below",
                  "To help us process your application, please enter your home phone number in the box below",
                  "If you are not able to enter your phone number above, please explain why in a few words",
                  "If you prefer not to enter your phone number, please tell us another way to reach you",
                  "Please enter your mobile number to receive SMS updates about your application status",
                  "Please enter your mobile number so we can contact you on WhatsApp about your application",
                  "Please enter your phone number to receive autodialer or prerecorded messages from Example Corp",
                  "Please enter your mobile number confirmation code, which we sent to you a moment ago",
                  "Please enter your phone number authentication code from the message we sent to you",
                  # (the second review's: sign-ups and another's number in other words, another kind of phone, and a
                  # phone box that shows no format: theirs to answer, on the card)
                  "Would you like to hear about future opportunities? If so, please enter your phone number below",
                  "Please enter your phone number so we can keep you informed of future career opportunities",
                  "To be considered for other positions as well, please enter your best phone number here",
                  "Please enter the phone number your previous boss can be reached at, so we can verify your work",
                  "Please enter the phone number at your current job where we can verify your employment",
                  "Please enter your cell phone number in the box below so we can reach you about your application",
                  "Please enter the phone number you would like us to call you on about your application, below"):
        ans = resolve_field(f(other + " (###-###-####)" if "below so" not in other and "call you on" not in other
                              else other), prof())
        assert ans is None or ans.rule != "phone", (other, ans)
    # the dashes only for a US number, with its dial code where the box asks for it, never under a 1- mask
    with_code = "Please enter your phone number including country code, in the format ###-###-#### please"
    p = Profile({"personal": {"phone": "+1 480 555 0123", "phone_country_code": "+1"}})
    assert resolve_field(f(with_code), p).value == "+1 480-555-0123"
    uk = Profile({"personal": {"phone": "+44 20 7946 0958", "phone_country_code": "+44"}})
    assert resolve_field(f(label), uk).value == "+44 20 7946 0958"
    assert resolve_field(f("Phone Number (1-###-###-####)"), p).value != "480-555-0123"


def test_another_persons_phone_box_never_takes_the_profiles():
    """autofill.py named two lists _SOMEONE_ELSE, and the later one (0.3.110's, for a Job Title beside a reference's
    row) took the place of the contact rules' own: next of kin, spouse, family, relatives, a contact person, a referee
    and supervisors or managers were lost, so "Next of Kin Phone" or "Spouse Phone Number" took the person's own
    phone. One list again, with both lists' words; "Family Name" stays the person's last name."""
    for label in ("Next of Kin Phone", "Spouse Phone Number", "Family Member Phone", "Contact Person Phone",
                  "Supervisors Phone", "Managers Phone", "Relatives Phone", "Referee Phone", "Relationship Phone",
                  "Contact Name"):
        assert resolve_field(f(label), prof()) is None, label
    assert resolve_field(f("Phone", section="Next of Kin"), prof()) is None
    for label in ("Family Name", "Last Name/Family Name", "Family name (surname)", "Family Names"):
        assert resolve_field(f(label), prof()).rule == "last_name", label
    assert resolve_field(f("Phone"), prof()).rule == "phone"


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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    school = {**data["education_history"][0], "end": "present"}
    path.write_text(yaml.safe_dump({**data, "education_history": [school]}), encoding="utf-8")
    assert resolve_field(asked, prof()) is None  # still at school, no year: the person says

    from datetime import date

    today = date.today()
    ended = f"{today.year}-01" if today.month > 1 else f"{today.year - 1}-12"  # earlier this year: done
    path.write_text(yaml.safe_dump({**data, "education_history": [{**school, "end": ended}]}), encoding="utf-8")
    assert resolve_field(asked, prof()).value == "I am currently not attending school"
    path.write_text(yaml.safe_dump({**data, "education_history": [{**school, "end": f"{today.year + 1}-05"}]}),
                    encoding="utf-8")
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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    ended = [{**job, "end": "2024-06"} for job in data["work_history"]]
    path.write_text(yaml.safe_dump({**data, "work_history": ended}), encoding="utf-8")
    assert resolve_field(f("Are you currently employed?", "radio_group", options=yes_no), prof()).value == "No"


def test_a_veteran_question_and_a_family_members_are_answered_as_asked():
    """"I am not a protected veteran" answered "Are you a veteran?" No, though a veteran who
    isn't a protected one is a veteran; and the person's own answers went to "Are you the
    spouse of a veteran?" and "Gender of your spouse"."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**data, "eeo": {**data["eeo"], "veteran": "I am not a protected veteran",
                                                    "gender": "Male"}}), encoding="utf-8")
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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    auth = {k: v for k, v in data["work_authorization"].items() if k != "us_person"}
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**auth, "us_citizen": True},
                                    "eeo": {**data["eeo"], "race": "White"}}), encoding="utf-8")
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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    def with_education(highest, history):
        path.write_text(yaml.safe_dump({**data, "education": {"highest_degree": highest},
                                        "education_history": history}), encoding="utf-8")
        return Profile.load()

    def answer(label, profile, kind="radio_group"):
        a = resolve_field(f(label, kind, options=["Yes", "No"] if kind != "text" else None), profile)
        return a and a.value

    some_college = with_education("Some college", [{"school": "Example Community College", "degree": "", "end": 2019}])
    assert answer("Do you have a Bachelor's degree?", some_college) == "No"
    assert (answer("Do you have a Bachelor's degree or higher (Master\u2019s degree, Ph.D., etc.)? *",
                   some_college) == "No")
    assert answer("Do you have an Associate's degree or higher?", some_college) == "No"
    assert answer("Master's degree preferred", some_college) == "No"
    assert answer("Do you have a high school diploma or GED?", some_college) is None  # not said
    for asked in ("Bachelor's Degree in Accounting or Finance Required", "Do you have a Bachelor of Science degree?",
                  "Do you have a Bachelor's degree from an accredited university?",
                  "Bachelor's degree or equivalent experience"):
        assert answer(asked, some_college) is None, asked
    associate = with_education("Associate's Degree", [])
    assert answer("Do you have a Bachelor's degree?", associate) == "No"
    assert answer("Do you have an Associate's degree?", associate) is None  # never Yes
    # a school listing a degree it can't place, or one at the level asked, stops the No
    for history in ([{"degree": "Bachelor's Degree"}], [{"degree": "BSN", "end": 2019}],
                    [{"degree": "BS", "status": "Graduated (honors)", "end": 2019}]):
        assert answer("Do you have a Bachelor's degree?",
                      with_education("Associate's Degree", history)) is None, history
    # a highest degree written any other way isn't read
    for highest in ("Bachelor's Degree (BS)", "Bachelor's (not finished)", "PhD ABD", "Bachelor's Degree Equivalent"):
        assert answer("Do you have a Master's degree?", with_education(highest, [])) is None, highest
    # a degree's details aren't the question
    assert answer("Bachelor's Degree Major", some_college, "text") is None
    # only a plain "No", with the choices shown: never one that says more than the profile
    for choices in (["Yes", "No, but I have equivalent work experience"],
                    ["Yes", "No, but I am currently enrolled"], []):
        a = resolve_field(f("Do you have a Bachelor's degree?", "combobox", options=choices), some_college)
        assert a is None, choices
    # a school whose entry doesn't say its degree stops the No
    unsaid = with_education("Associate's Degree",
                            [{"school": "Example State University", "major": "Finance", "end": 2020}])
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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    def with_degrees(degrees, highest="Bachelor's Degree", history=None):
        path.write_text(yaml.safe_dump({**data, "education": {"highest_degree": highest, "degrees_earned": degrees},
                                        "education_history": history or []}), encoding="utf-8")
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
    assert (answer("Do you have a Bachelor's degree or higher (Master's degree, Ph.D., etc.)? (Required)",
                   finance) == "Yes")
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
    for asked in ("Do you have a Master's degree or higher (MBA)?",
                  "Do you have a Master's degree or higher (M.B.A.)?"):
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
    # (Desert Financial's UKG Pro, run 56: "or equivalent" is either; "equivalent experience" isn't a diploma's)
    assert answer("Do you have a high school diploma or equivalent?", ged) == "Yes"
    assert answer("Do you have a high school diploma or equivalent experience?", ged) is None
    assert answer("Do you have a Bachelor's degree?", ged) == "No"
    assert answer("Do you have a high school diploma?",
                  with_degrees([{"level": "high_school", "field": "GED"}], "GED")) is None
    diploma = with_degrees([{"level": "high_school"}], "High school diploma",
                           [{"degree": "High school diploma", "end": 2015}])
    assert answer("Do you have a high school diploma?", diploma) == "Yes"
    assert answer("Do you have a high school diploma or equivalent?", diploma) == "Yes"
    assert answer("Do you have a Bachelor's degree?", diploma) == "No"
    # anything it can't read: a level, a status, a field saying it isn't done or is something else
    for degrees in ([{"level": "Bachelor's (in progress)"}], [{"level": "MA"}], [{"level": "BS"}],
                    [{"level": "bachelor", "field": "Finance", "status": "in progress"}],
                    [{"level": "bachelor", "field": "Finance (expected 2027)"}],
                    [{"level": "bachelor", "field": "Finance coursework"}],
                    [{"level": "bachelor", "field": "Finance - ABD"}],
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
    assert answer("Do you have a doctorate?",
                  with_degrees([{"level": "doctorate", "field": "Doctor of Medicine"}])) is None
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
    assert (answer("Do you have a Bachelor's degree?",
                   with_degrees([{"level": "Bachelor's", "field": "Finance"}])) == "Yes")
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
    free_text = with_degrees([], "Bachelor's Degree",
                             [{"degree": "Bachelor's Degree", "major": "Finance", "end": 2020}])
    assert answer("Do you have a Bachelor's degree?", free_text) is None


def test_a_citizens_visa_questions():
    """A U.S. citizen holds no F-1, H-1B or TN visa ("Are you currently on an F-1 visa
    (OPT/CPT)?" was asked), and their visa or citizenship status is the citizen choice. Only
    a citizen's: a past visa ("Have you ever held...") or anyone else's status is theirs to say."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**data["work_authorization"], "us_citizen": True}}),
                    encoding="utf-8")
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
                   "U.S. Lawful Permanent Resident (Greencard holder)",
                   "Person granted Refugee status in the United States", "None of the above"]
    assert answer(ear, "combobox", ear_options) == "Citizen or National of the United States"
    # the same words before an alias (review), and a question about the role isn't the person's status
    assert (choose_option("U.S. Citizen", ["U.S. Citizen or National", "U.S. Citizen", "Non-US Citizen"])
            == "U.S. Citizen")
    assert answer("Is this role subject to export control regulations? Select the option that applies", "select",
                  ["U.S. Citizen", "Not subject"]) is None
    # "Citizenship status" is still the citizenship rule's, from the profile's country
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**data["work_authorization"], "us_citizen": True,
                                                                   "citizenship": "United States"}}), encoding="utf-8")
    assert answer("Citizenship status", "select", ["United States", "Other"]) == "United States"
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**data["work_authorization"], "us_citizen": False}}),
                    encoding="utf-8")
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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**data, "personal": {**data["personal"], "drivers_license": True}}),
                    encoding="utf-8")

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
    path.write_text(yaml.safe_dump({**data, "personal": {**data["personal"], "drivers_license": False}}),
                    encoding="utf-8")
    assert answer("Do you have a valid driver’s license?") == "No"


def test_more_questions_the_profile_answers():
    """Questions from the live runs the profile already answers: where the person lives (Axon),
    sponsorship "to maintain your work authorization" (Grant Thornton), a citizen isn't an
    alien (Axon), when they can start (Micron, GoDaddy), work experience at all (Southwest Gas),
    and a trailing "(Yes/No)"."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**data, "work_authorization": {**data["work_authorization"], "us_citizen": True},
                                    "personal": {**data["personal"], "drivers_license": True},
                                    "preferences": {**data["preferences"], "earliest_start": "2 weeks after offer"}}),
                    encoding="utf-8")

    def answer(label, kind="radio_group"):
        a = resolve_field(f(label, kind, **({"options": ["Yes", "No"]} if kind != "text" else {})), prof())
        return a and a.value

    assert answer("What city do you currently reside in?*", "text") == "Chandler"
    assert answer("Where are you currently located?*", "text") == "Chandler, AZ"
    # a hub list or a question about coming in isn't the address
    assert answer("Where are you currently located?") is None
    assert answer("Where are you currently located? This role is onsite 4 days/week at one of our hubs.",
                  "text") is None
    assert answer("Will you require visa sponsorship, from Grant Thornton or otherwise, now or in the future to "
                  "maintain your work authorization?") == "No"
    # (APS's SuccessFactors, run 56: what the sponsorship is for, "in order to have" it)
    assert answer("* Do you now, or will you in the future, require sponsorship in order to have work authorization in "
                  "the United States?") == "No"
    # ...but a question that also asks whether they can work now stays theirs (the review of 0.3.146)
    for both in ("Are you able to work in the US now, or will you need sponsorship to get work authorization?",
                 "Will you require sponsorship to get work authorization, and are you "
                 "currently able to work in the US?",
                 "Do you currently have, or will you require sponsorship to receive, work authorization?",
                 "Are you able to work in the US, or will you require sponsorship in order to have work authorization?",
                 "Are you able to work in the US now, or will you need sponsorship to obtain work authorization?"):
        assert answer(both) is None, both
    # (and what it's for, "to be able to work", or a visa they have now, is still the one question)
    for one in ("Will you require sponsorship to be able to work in the United States?",
                "Will you require employer sponsorship in order to be permitted to work in the US?",
                "Will you now or in the future require sponsorship to be allowed to work in the U.S.?",
                "Do you currently have a visa that will require sponsorship in the future?"):
        assert answer(one) == "No", one
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
    path.write_text(yaml.safe_dump({**data, "work_history": []}), encoding="utf-8")
    assert answer("Do you have previous work experience?") is None
    assert answer("Are you an alien illegally or unlawfully in the United States?*") is None


def test_questions_from_the_finance_run():
    """GoDaddy's long LinkedIn label and its "(Please note, ...)" aside, Grant Thornton's
    "Preferred Last Name", GoDaddy's LGBTQ self-identification and Axon's I-9 question."""
    import yaml
    from job_apply import config

    path = config.profile_path()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**data, "eeo": {**data["eeo"], "sexual_orientation": "Decline to self-identify"},
                                    "preferences": {**data["preferences"], "desired_salary": "$85,000"}}),
                    encoding="utf-8")

    def answer(label, kind="combobox", options=("Yes", "No")):
        a = resolve_field(f(label, kind, **({"options": list(options)} if kind != "text" else {})), prof())
        return a and a.value

    assert (answer("LinkedIn profile (if you do not have one or if you prefer not to provide one, enter N/A):*", "text")
            == "https://www.linkedin.com/in/samrivera")
    assert answer("Do you currently reside in the United States? (Please note, your answer to this question and the "
                  "related sub-questions, as applicable, will be used to determine your eligibility.)") == "Yes"
    # a note that isn't an aside after the question stays part of it
    assert answer("Desired pay (note: hourly rate)", "text") is None
    assert answer("Preferred Last Name", "text") == "Rivera"
    assert answer("Can you provide verification of both your identity and authorization to work in the United States, "
                  "to the extent required by law?*") == "Yes"
    assert answer("Can you provide verification of authorization to work in a country other "
                  "than the United States?") is None
    # Onto's Workday says only "eligibility", after "If hired"; eligibility for something else isn't it
    assert answer("If hired, can you provide proof of eligibility?*", "listbox", ("Select One", "Yes", "No")) == "Yes"
    assert answer("Upon hire, can you provide documentation of your eligibility?") == "Yes"
    assert answer("Can you provide proof of eligibility once hired?") == "Yes"
    assert answer("Can you provide proof of eligibility?") is None  # (eligibility for what: the person's)
    assert answer("Can you provide proof of eligibility for the veterans' preference?") is None
    assert answer("If hired, can you provide proof of eligibility for rehire?") is None
    assert answer("If hired, can you provide proof of eligibility to work in Canada?") is None
    assert answer("Do you consider yourself a member of the Lesbian, Gay, or Bisexual (LGBTQ) community?*",
                  options=("Yes", "No", "I don't wish to answer")) == "I don't wish to answer"
    # a family member's isn't the person's
    assert answer("Is your spouse a member of the LGBTQ community?",
                  options=("Yes", "No", "I don't wish to answer")) is None


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
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({**data,
                                    "preferences": {**data["preferences"], "earliest_start": "2 weeks after offer"}}),
                    encoding="utf-8")

    def answer(label, options, kind="combobox"):
        a = resolve_field(f(label, kind, options=options), prof())
        return a and a.value

    countries = ["Australia", "Canada", "Netherlands", "United Kingdom", "United States", "Other"]
    assert answer("Where are you currently located?*", countries) == "United States"
    assert answer("Where are you currently located?", ["Phoenix hub", "Seattle hub", "Remote"]) is None
    waits = ["Immediate", "2 Week Notice", "30 Days", "60 Days", "90 Days"]
    assert answer("When are you available to start?", waits) == "2 Week Notice"
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "earliest_start": "1 month"}}),
                    encoding="utf-8")
    assert answer("When are you available to start?", waits) == "30 Days"
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "earliest_start": "Immediately"}}),
                    encoding="utf-8")
    assert answer("When are you available to start?", waits) == "Immediate"
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "earliest_start": "3 weeks"}}),
                    encoding="utf-8")
    assert answer("When are you available to start?", waits) is None  # never a sooner or later start
    # a range isn't one wait: "2-4 Weeks" is no answer for a month
    path.write_text(yaml.safe_dump({**data, "preferences": {**data["preferences"], "earliest_start": "1 month"}}),
                    encoding="utf-8")
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


@pytest.mark.parametrize("disability",
                         ["No", "None", "No disability", "I don't have a disability", "I do not have a disability"])
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
        a = resolve_field({"label": label, "kind": kind, "required": True,
                           **({"options": options} if options else {})}, p)
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


@pytest.mark.parametrize("label, kind", [
    # a past job's pay, either way round, is no answer for the desired salary
    ("What was the salary at your previous job?", "text"), ("Salary at your previous job", "text"),
    ("Salary history", "text"), ("What was your prior salary?", "text"),
    # a city that isn't where the person lives
    ("City of Birth", "text"), ("Hometown", "text"), ("City where your passport was issued", "text"),
    # a move already under way, or at the person's expense, isn't the willingness the profile says
    ("Are you in the process of relocating to the Phoenix area?", "text"),
    ("Are you currently relocating to Arizona?", "text"), ("Are you willing to relocate at your own expense?", "text"),
    # shift work done before is experience, not a willingness
    ("Have you worked shift work before?", "text"), ("Have you ever worked a rotating shift?", "text"),
    # getting to work isn't travelling for it
    ("Do you have reliable transportation to travel to work?", "text"),
    ("Do you have reliable transportation to travel to the job site?", "text"),
    # learning a trade isn't hearing of the job; disability benefits aren't a self-identification
    ("Where did you learn to weld?", "text"), ("Have you received disability benefits?", "text"),
    # another person's or a company's details, and a phone other than the profile's one number
    ("Email of recruiter", "text"), ("Recruiter's phone number", "text"), ("Agency contact email", "text"),
    ("Alternate phone", "text"), ("Secondary phone number", "text"), ("Company website", "text"),
    ("Company LinkedIn page", "text"),
    # a date box for someone not at school: "Not currently attending school" is a list's choice
    ("Expected graduation date", "text"),
])
def test_questions_that_look_like_the_profiles_but_are_not(label, kind):
    """The review of Oct 15's wording slips: each was answered from the profile with something it doesn't say (the
    desired salary for a past job's, the home city for a city of birth, the willingness to relocate for a move under
    way, the travel answer for a commute, the person's own email for a recruiter's...). Each is left for the person."""
    p = _person(preferences={"desired_salary": 65000, "willing_to_relocate": True, "willing_to_travel": True,
                             "flexible_schedule": True, "how_did_you_hear": "LinkedIn"},
                eeo={"disability": "No, I do not have a disability"})
    p.data["personal"].update({"email": "sam@example.com", "website": "https://sam.example.com",
                               "linkedin_url": "https://www.linkedin.com/in/example"})
    assert _answer(label, p, kind=kind) is None, label


def test_the_same_profile_still_answers_its_own_questions():
    """The guards above leave the questions the profile does answer."""
    p = _person(preferences={"desired_salary": 65000, "willing_to_relocate": True, "willing_to_travel": True,
                             "flexible_schedule": True, "how_did_you_hear": "LinkedIn"})
    p.data["personal"].update({"email": "sam@example.com", "website": "https://sam.example.com"})
    assert str(_answer("Desired salary", p)) == "65000"
    assert _answer("City", p) == "Mesa"
    assert _answer("Are you willing to relocate?", p) == "Yes"
    assert _answer("Are you willing to do shift work?", p) == "Yes"
    assert _answer("Are you willing to travel?", p) == "Yes"
    assert _answer("Where did you learn about this position?", p) == "LinkedIn"
    assert _answer("Email", p) == "sam@example.com"
    assert _answer("Phone number", p) == "480-555-0100"
    assert _answer("Website", p) == "https://sam.example.com"
    # (a citizen's status, not "Yes, for any employer")
    assert _answer("Work Authorization Status", p) == "U.S. Citizen"
    assert _answer("Work Authorization Status", _person(work_authorization={"authorized_to_work": True,
                                                                             "requires_sponsorship": False})) is None
    # (the review of 0.3.187: what the guards above must leave answered)
    for label in ("What is your current city?", "Your city of residence", "Residential Town", "Current Town"):
        assert _answer(label, p) == "Mesa", label
    assert _answer("Postal Town", p) == "Mesa"  # (never the ZIP)
    assert _answer("What is your work authorization status?", p) == "U.S. Citizen"
    assert _answer("Work authorization status", p, kind="textarea") == "U.S. Citizen"
    assert _answer("Are you willing to travel to and from customer sites?", p) == "Yes"
    assert _answer("Are you willing to travel to work at customer sites?", p) == "Yes"
    assert _answer("Are you able to work weekends, including shifts that start before 6:00 AM?", p) == "Yes"
    assert _answer("Best phone number (home, cell or other)", p) == "480-555-0100"
    assert _answer("Email (our recruiter will contact you)", p) == "sam@example.com"
    assert _answer("Email", p, section="Agency Information") is None  # (that party's box)
    assert _answer("How did you learn that we were hiring?", p) == "LinkedIn"
    assert str(_answer("What salary would you need to leave your current job?", p)) == "65000"
    p.data["personal"]["linkedin_url"] = "https://www.linkedin.com/in/example"
    assert _answer("Website (other than LinkedIn)", p) == "https://sam.example.com"


def _with_bank(**answers):
    """A profile whose answers came from the desk's Common questions card: the bank's own patterns, by its ids."""
    from job_apply import config

    items = [{"bank": bank_id, "match": "", "answer": answer} for bank_id, answer in answers.items()]
    config._current_bank_patterns(items)
    assert all(item["match"] for item in items), items
    return _person(answers=items)


def test_a_common_questions_answer_goes_where_its_question_is_and_nowhere_else():
    """The review of 0.3.187: a list's "If yes, please explain" is only its hint (its choice is answered); a free answer
    from the card goes into a text area too, a bare yes or no never; and a question a rule leaves to the person on
    purpose (learning a trade, a move at the person's own cost) isn't the common question either."""
    p = _with_bank(terminated="No", heard_about="LinkedIn", desired_pay="$28 per hour", relocate="Yes")
    yes_no = ["Yes", "No"]
    assert _answer("Have you ever been terminated from a job? If yes, please explain.", p, kind="radio_group",
                   options=yes_no) == "No"
    assert _answer("How did you hear about us? (If other, please explain)", p, kind="select",
                   options=["LinkedIn", "Indeed", "Other"]) == "LinkedIn"
    assert _answer("What are your salary expectations?", p, kind="textarea") == "$28 per hour"
    assert _answer("Have you ever been terminated from a job?", p, kind="textarea") is None
    assert _answer("How did you learn to weld?", p) is None
    assert _answer("Are you able to relocate at your own cost?", p) is None
    assert _answer("Are you willing to relocate?", p) == "Yes"


def test_a_common_questions_answer_never_goes_into_an_explanation():
    """The question bank's short answers ("No") go into a list or a short box, never a text area or a box asking for an
    explanation: "If you were ever terminated from a job, please explain" (the review of Oct 15)."""
    p = _person(answers=[{"match": "(?:terminated|fired)\\b.{0,40}\\b(?:job|employer)", "answer": "No", "bank": True}])
    assert _answer("Have you ever been terminated from a job?", p) == "No"
    assert _answer("If you were ever terminated from a job, please explain", p, kind="textarea") is None
    assert _answer("If you were ever terminated from a job, please explain", p) is None
    assert _answer("Have you ever been terminated from a job? If so, provide details", p) is None


@pytest.mark.parametrize("dates", [{"end": 2099}, {"end": "present"}, {"end": "Present"}, {"end": "Current"},
                                   {"to": 2099}, {"dates": "2023 - Present"}, {"end": "May 2099 (expected)"},
                                   {"end_date": "present"}, {"end": "spring of next year"}])
def test_a_degree_still_under_way_is_not_the_highest_education(dates):
    """A Bachelor's whose school ends later, or is under way, isn't the person's highest education yet: "Highest level
    of education completed" got "Bachelor's" (the review of Oct 15; never overstate education). Its expected graduation
    is still the school's year."""
    p = _person(education={},
                education_history=[{"school": "Example State University", "degree": "BS Electrical Engineering",
                                    "major": "EE", "start": 2023, **dates}])
    options = ["High School", "Associate's", "Bachelor's", "Master's"]
    assert _answer("Highest level of education completed", p, kind="select", options=options) is None
    assert _answer("Highest Degree", p) is None
    done = _person(education={},
                   education_history=[{"school": "Example State University", "degree": "BS Electrical Engineering",
                                       "major": "EE", "start": 2016, "end": 2020}])
    assert _answer("Highest level of education completed", done, kind="select", options=options) == "Bachelor's"


def test_the_current_level_of_education_is_the_highest_had():
    """NNSS's Oracle (live run 144): "13. What is your current level of education?" wasn't read as the highest
    education, so the profile's Bachelor's was never given and the person was asked. It is, from the profile alone:
    never a degree under way ("In-process with Master's..." isn't a Master's), and nothing where the profile says
    none (coursework is not a degree)."""
    options = ["High School or equivalent", "Some college", "Associate's degree", "Bachelor's degree",
               "Master's Degree", "Doctorate", "In-process with Master's or Doctorate degree"]
    question = "13. What is your current level of education?"
    for highest, picked in (("Bachelor's Degree", "Bachelor's degree"), ("Master's Degree", "Master's Degree"),
                            ("Some college", "Some college")):
        p = _person(education={"highest_degree": highest})
        assert _answer(question, p, kind="radio_group", options=options) == picked, highest
        assert _answer("What is your level of education?", p, kind="select", options=options) == picked, highest
    under_way = _person(education={}, education_history=[
        {"school": "Example State University", "degree": "MS Electrical Engineering", "start": 2024, "end": 2099}])
    assert _answer(question, under_way, kind="radio_group", options=options) is None
    assert _answer(question, _person(education={}, education_history=[]), kind="radio_group", options=options) is None
    # (not a question about what the job asks for)
    assert _answer("Do you have the level of education required for this position?", _person(
        education={"highest_degree": "Bachelor's Degree"}), kind="radio_group", options=["Yes", "No"]) is None


def test_a_degree_had_is_still_the_highest_when_a_later_one_is_under_way():
    """The review of 0.3.187: an MS under way listed first hid the BS already had."""
    p = _person(education={}, education_history=[
        {"school": "Example State University", "degree": "MS Electrical Engineering", "start": 2024, "end": 2099},
        {"school": "Example State University", "degree": "BS Electrical Engineering", "start": 2012, "end": 2016}])
    assert _answer("Highest level of education completed", p, kind="select",
                   options=["High School", "Associate's", "Bachelor's", "Master's"]) == "Bachelor's"


def test_a_date_box_asking_when_you_can_start_gets_the_day_the_profile_makes():
    """Eightfold's "Preferred Start Date" is a date picker (issue #112, note 41): the profile's "2 weeks after offer"
    typed into it found nothing, and it was skipped. A box that takes a date gets the day the profile's earliest start
    makes (today and its notice; a date as itself; today for "Immediately"), and nothing where it makes none; never a
    day for another date's box; and a box that takes words keeps the profile's words."""
    from datetime import date, timedelta

    today = date.today()
    in_two_weeks = (today + timedelta(days=14)).strftime("%m/%d/%Y")

    def answer(earliest, label="Preferred Start Date", profile=None, **kw):
        field = {"id": "1", "label": label, "kind": "combobox", "value": "", "date": True, **kw}
        a = resolve_field(field, profile or Profile({"preferences": {"earliest_start": earliest}}))
        return a and a.value

    assert answer("2 weeks after offer") == answer("Two weeks' notice") == in_two_weeks
    assert answer("1 month") == (today + timedelta(days=30)).strftime("%m/%d/%Y")
    assert answer("Immediately") == answer("ASAP") == today.strftime("%m/%d/%Y")
    assert answer("2026-11-01") == answer("11/01/2026") == answer("November 1, 2026") == "11/01/2026"
    for unread in ("After graduation", "2 weeks after graduation", "2-4 weeks", "December 2026", "Negotiable"):
        assert answer(unread) is None, unread  # (left, as before: never a day the profile doesn't say)
    for label in ("Available Start Date", "Earliest Start Date*", "Date available to start", "When can you start?"):
        assert answer("2 weeks after offer", label) == in_two_weeks, label
    # a date input takes the ISO form, a date's part its own
    assert answer("2 weeks after offer", kind="text", input_type="date", date=None) == \
        (today + timedelta(days=14)).isoformat()
    assert answer("2026-11-01", kind="text", sublabel="Month", date=None) == "11"
    # the common question's answer (the question bank's, in answers.yaml) too
    bank = Profile({"answers": [{"match": "(?:desired|earliest|preferred) start", "answer": "Two weeks after an offer",
                                 "bank": True}]})
    assert answer(None, profile=bank) == in_two_weeks
    # never another date's box: "Start Date" alone, a start date in a past job's block
    assert answer("2 weeks after offer", "Start Date") is None
    assert answer("2 weeks after offer", section="Work Experience 1") != in_two_weeks
    # a box that takes words keeps the profile's
    assert answer("2 weeks after offer", date=None, kind="text") == "2 weeks after offer"


def test_a_date_box_for_another_day_or_an_unclear_start_gets_no_day():
    """The review of the fix for note 41: "Earliest date available for an interview" took the start date (an answer the
    profile doesn't give), and so did "Anticipated start date of degree"; "Immediately after 2 weeks notice" and
    "ASAP, 2 weeks notice required" made today (sooner than the person said), and a date gone by was given as itself.
    None of them gets a day. And a box hinted day first ("DD/MM/YYYY") gets the day in that order."""
    from datetime import date, timedelta

    def answer(earliest, label="Preferred Start Date", **kw):
        field = {"id": "1", "label": label, "kind": "combobox", "value": "", "date": True, **kw}
        a = resolve_field(field, Profile({"preferences": {"earliest_start": earliest}}))
        return a and a.value

    for label in ("Earliest date available for an interview", "What is the earliest date you could interview?",
                  "Anticipated start date of degree", "Earliest start date for orientation"):
        assert answer("2 weeks after offer", label) is None, label
    for unclear in ("Immediately after 2 weeks notice", "ASAP, 2 weeks notice required",
                    "Available immediately; prefer 2 weeks", "2025-06-01", "2 weeks after background check"):
        assert answer(unclear) is None, unclear
    day = date.today() + timedelta(days=14)
    assert answer("2 weeks after offer", kind="text", date="dd/mm/yyyy") == day.strftime("%d/%m/%Y")
    assert answer("2 weeks after offer", kind="text", date="yyyy-mm-dd") == day.isoformat()
    assert answer("2 weeks after offer", kind="text", date="dd.mm.yy") == day.strftime("%d.%m.%y")


# A mutation pass over _resolve_field (Oct 18): each test below pins a decision no test held, one a changed rule would
# have turned into a different answer on a real form


def test_a_signed_date_box_by_its_other_names_gets_todays_date():
    """"Date Signed" and "Date of Signature" are the day a form is signed, as "Signature Date" is: today, and never
    the person's name, which "Signature" alone takes."""
    from datetime import date

    today = date.today().strftime("%m/%d/%Y")
    for label in ("Date Signed", "Date of Signature"):
        a = resolve_field(f(label), prof())
        assert a is not None and (a.value, a.rule) == (today, "signed_date"), (label, a)


def test_a_bare_date_in_a_block_about_something_else_is_not_the_day_the_form_is_signed():
    """A bare "Date" is the signing date only on a signed form: in a block about a conviction, an accident, military
    service, a license or a past job it asks when that happened, which the profile doesn't say."""
    p = Profile({"personal": {"first_name": "Sam", "last_name": "Rivera"}})
    assert resolve_field(f("Date", section="Voluntary Self-Identification of Disability"), p).rule == "signed_date"
    for section in ("Conviction Details", "Criminal Record", "Offense Information", "Incident Report",
                    "Traffic Violation", "Accident Report", "Military Branch", "Selective Service",
                    "Discharge Information", "Availability", "Available to Start", "Employment", "Education",
                    "Prior Experience", "Residence History", "Place of Birth", "Driver's License", "Certifications"):
        assert resolve_field(f("Date", section=section), p) is None, section


def test_a_pages_one_file_box_for_another_document_never_gets_the_resume():
    """A page's only file box takes the resume whatever it's called ("Attachment"), but not one that asks for another
    document: a degree, a transcript, a license, a passport, a portfolio, references, a photo, a DD-214."""
    p = prof()
    assert resolve_field(f("Attachment", "file"), p).rule == "documents.resume"
    for label in ("Copy of Degree", "Diploma", "Transcripts", "Certificate", "Driver's License", "Passport",
                  "Portfolio", "Writing Sample", "Work Sample", "Letter of Recommendation", "References",
                  "Identification", "Photo", "DD-214"):
        assert resolve_field(f(label, "file"), p) is None, label


def test_a_file_lists_resume_box_is_ticked_however_it_says_the_file_is_the_resume(tmp_path):
    """Kforce's Taleo lists the files on the person's record, each row with a "Select the file" box and a "Document
    is a resume" box: both ticked on the profile's resume's row, whether the box says a resume, the resume or your
    CV."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    p = Profile({"documents": {"resume": str(resume)}})
    for label in ("Select the file: resume.pdf", "Document is a resume", "This document is the resume",
                  "Document is your CV"):
        a = resolve_field(f(label, "checkbox", row_file="resume.pdf", same_file_rows=1), p)
        assert a is not None and a.value is True, label


def test_a_box_for_an_agencys_an_interviewers_or_a_businesss_details_never_takes_the_persons():
    """A box naming another party in its own words is that party's: the person's phone or ZIP never goes in it."""
    p = prof()
    assert _answer("Phone", p) == "480-555-0123"
    for label in ("Agency Phone", "Agent Phone", "Interviewer Phone", "Business Zip Code"):
        assert _answer(label, p) is None, label


def test_a_block_for_a_recruiters_an_agents_or_an_interviewers_details_never_takes_the_persons():
    """A block named for a recruiter's, an agent's or an interviewer's information, details or contact is that
    person's: its Name, Email and Phone are never the applicant's."""
    p = prof()
    for section in ("Recruiter Information", "Agent Details", "Interviewer Details", "Agency Contact"):
        for label in ("Name", "Email", "Phone"):
            assert _answer(label, p, section=section) is None, (section, label)


def test_an_emergency_contacts_block_never_takes_the_persons_own_name_or_address():
    """An emergency contact's names, phone type, dial code and address are theirs, not the person's (as their first
    name, phone and email already were)."""
    p = Profile({"personal": {"first_name": "Sam", "middle_name": "Lee", "last_name": "Rivera",
                              "preferred_name": "Sammy", "phone": "480-555-0100", "phone_country_code": "+1",
                              "phone_type": "Mobile",
                              "address": {"line1": "100 W Main St", "line2": "Apt 2", "city": "Mesa", "state": "AZ",
                                          "county": "Maricopa", "country": "United States"}}})
    assert _answer("Last Name", p) == "Rivera" and _answer("Address Line 1", p) == "100 W Main St"
    for label in ("Last Name", "Middle Name", "Preferred Name", "Preferred Last Name", "Preferred Full Name",
                  "Phone Type", "Country Code", "Address Line 1", "Address Line 2", "County", "State", "Country"):
        assert _answer(label, p, section="Emergency Contact") is None, label


def test_a_citizenship_or_clearance_list_whose_choices_are_unseen_is_left_for_the_person():
    """A citizenship or security clearance attestation is answered only from the choices it shows: a list read
    without them is the person's (as a U.S. person's was)."""
    p = Profile({"work_authorization": {"us_citizen": True, "citizenship": "United States",
                                        "security_clearance": "Secret"}})
    for label in ("Are you a U.S. citizen?", "Citizenship", "Do you hold an active security clearance?"):
        assert resolve_field(f(label, "combobox"), p) is None, label
    assert resolve_field(f("Are you a U.S. citizen?", "combobox", options=["Yes", "No"]), p).value == "Yes"


def test_a_question_about_sanctioned_countries_dual_citizenship_or_export_licenses_is_never_answered():
    """The profile can't honestly answer ties to sanctioned countries, a dual citizenship, a denied party list or an
    export license: a text box or a list asking any of them is the person's (or their own answers bank's), never the
    profile's citizenship, U.S. person or government answer."""
    p = Profile({"work_authorization": {"authorized_to_work": True, "us_person": True, "us_citizen": True,
                                        "citizenship": "United States"},
                 "background": {"government_employee": False}})
    for label in ("Are you a citizen of Cuba?", "Are you a citizen of Iran?", "Are you a citizen or resident of Syria?",
                  "Are you a citizen of the Crimea region of Ukraine?",
                  "Are you a citizen of a country on the OFAC list?", "Do you hold dual citizenship?",
                  "Do you hold dual nationality or citizenship?", "Do you hold citizenship of a sanctioned country?",
                  "Are you a citizen of an embargoed country?", "Would your citizenship require an export license?",
                  "Are you a U.S. person on any denied party list?"):
        assert _answer(label, p) is None, label
    assert _answer("Have you ever been employed by the government of North Korea?", p, kind="radio_group",
                   options=["Yes", "No"]) is None


def test_a_work_question_about_another_country_is_left_for_the_person_whatever_it_asks():
    """The profile's work facts are the United States': a question about working without sponsorship, citizenship,
    permanent residence or a visa in another country, or work authorization in "another", "any other" or a "foreign"
    country, is the person's."""
    p = Profile({"work_authorization": {"authorized_to_work": True, "requires_sponsorship": False, "us_person": True,
                                        "us_citizen": True, "citizenship": "United States"}})
    for label in ("Can you work in Canada without sponsorship?", "Are you a citizen or permanent resident of Canada?",
                  "Are you a citizen of Mexico?", "Do you hold a non-immigrant visa for the United Kingdom?",
                  "Are you authorized to work in another country?", "Are you authorized to work in any other country?",
                  "Are you legally authorized to work in a foreign country?"):
        assert _answer(label, p) is None, label


def test_a_list_of_cities_under_a_nearest_city_heading_takes_the_profiles_city():
    """Taleo's "Closest Metropolitan City" Region is a list of cities (Kforce's): so is one under a "Nearest Major
    City", "Closest City" or "Metro Area" heading, or in a box called Area, Metro, Metro Area or Metro City. The
    profile's city goes there, never its state."""
    p = Profile({"personal": {"address": {"country": "United States", "state": "AZ", "city": "Chandler"}}})
    cities = ["Not Specified", "Anthem", "Bisbee", "Chandler"]
    for section in ("Nearest Major City", "Closest City", "Metro Area"):
        a = resolve_field(f("Region", "select", section=section, options=cities), p)
        assert a is not None and a.value == "Chandler", section
    for label in ("Area", "Metro", "Metro Area", "Metro City"):
        a = resolve_field(f(label, "select", section="Closest Metropolitan City", options=cities), p)
        assert a is not None and a.value == "Chandler", label


def test_a_decline_box_is_ticked_however_the_site_words_its_decline():
    """A self-identification question's own decline box (Nikon's "I decline to say Gender") is ticked for a profile
    that declines, however the site words it: prefer not to answer, do not wish to disclose, don't wish to say, or
    decline to state, to self-identify or to identify."""
    p = Profile({"eeo": {"gender": "Decline to self-identify", "race": "I don't wish to answer",
                         "veteran": "I don't wish to answer"}})
    for label in ("I prefer not to answer: Veteran Status", "I do not wish to disclose my race",
                  "I don't wish to say Gender", "I decline to state Gender",
                  "I decline to self-identify: Race/Ethnicity", "I decline to identify my gender"):
        a = resolve_field(f(label, "checkbox"), p)
        assert a is not None and a.value is True, label


def test_a_password_box_never_takes_anything_from_the_profile():
    """A password box is never filled from the profile, whatever it's called: not "Email Password" with the email."""
    p = prof()
    for label in ("Email Password", "Email", "City"):
        assert resolve_field(f(label, "password"), p) is None, label


def test_a_lone_checkbox_is_ticked_only_from_the_persons_own_answers():
    """A single box is usually an attestation ("I am at least 18 years of age", "I am legally authorized to work in the
    United States"): it's ticked only where the person's own answers say so, never from the profile's facts."""
    p = prof()
    for label in ("I am at least 18 years of age", "I am legally authorized to work in the United States", "Email"):
        assert resolve_field(f(label, "checkbox"), p) is None, label
    p.data["answers"] = [{"match": "at least 18", "answer": "Yes"}]
    assert resolve_field(f("I am at least 18 years of age", "checkbox"), p).value is True


def test_a_file_lists_box_on_another_files_row_is_never_ticked(tmp_path):
    """A file list's boxes are ticked only on the row of the profile's own resume, and only where no other row has its
    name (one tailored for another job may share it); with no resume in the profile, on none."""
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    p = Profile({"documents": {"resume": str(resume)}})
    for label in ("Select the file: cover-letter.pdf", "Document is a resume"):
        assert resolve_field(f(label, "checkbox", row_file="cover-letter.pdf", same_file_rows=1), p) is None, label
    for label in ("Select the file: resume.pdf", "Document is a resume"):
        assert resolve_field(f(label, "checkbox", row_file="resume.pdf", same_file_rows=2), p) is None, label
        box = f(label, "checkbox", row_file="resume.pdf", same_file_rows=1)
        assert resolve_field(box, Profile({"personal": {"first_name": "Sam"}})) is None, label


def test_a_second_file_box_for_other_documents_never_gets_the_resume():
    """Beside the resume's box, another file box ("Additional Documents") is for something else: only the resume's
    box gets the resume."""
    fields = [{"id": "1", "kind": "file", "label": "Upload Resume"},
              {"id": "2", "kind": "file", "label": "Additional Documents"}]
    assert [t["id"] for t in plan_autofill(fields, prof())["to_fill"]] == ["1"]


def test_a_phone_box_that_says_only_no_takes_the_phone():
    """iCIMS's "Number" beside its Phone Country Code is a phone's by its autocomplete: so is one that says only
    "No."."""
    a = resolve_field(f("No.", autocomplete="tel-national"), _person())
    assert a is not None and (a.value, a.rule) == ("480-555-0100", "phone")


def test_a_box_marked_as_a_phones_that_names_another_number_is_not_the_persons_phone():
    """A box whose autocomplete says it's a phone's takes the profile's phone only where its label says no more than
    "Number": a Fax, a Pager or an "Other number" marked so is the person's to fill."""
    for label in ("Fax", "Pager", "Other number"):
        assert resolve_field(f(label, autocomplete="tel"), _person()) is None, label


def test_a_yes_or_no_the_profile_writes_as_true_or_false_goes_in_a_text_box_as_yes_or_no():
    """A profile that writes "security_clearance: no" (YAML's false) answers "Do you hold an active security
    clearance?" in a text box with No, never "False"."""
    for held, said in ((False, "No"), (True, "Yes")):
        p = Profile({"work_authorization": {"security_clearance": held}})
        assert _answer("Do you hold an active security clearance?", p) == said, held


def test_a_jobs_currently_employed_question_in_a_box_is_yes_or_no():
    """"Currently employed here?" in a job's block, drawn as a text box or a list whose choices aren't read yet: Yes
    for the job with no end, No for the one that ended, never True or False."""
    p = _person()
    assert _answer("Currently employed here?", p, section="Work Experience 1") == "Yes"
    assert _answer("Currently employed here?", p, kind="combobox", section="Work Experience 2") == "No"


def test_a_citizens_visa_status_is_us_citizen_in_a_box_or_the_choice_naming_the_united_states():
    """"What is your visa status?" for a citizen: "U.S. Citizen" in a text box, and among choices the one that says
    U.S. citizen however it's written ("American Citizen", "Citizen - USA"), never another country's."""
    p = Profile({"work_authorization": {"us_citizen": True}})
    assert _answer("What is your visa status?", p) == "U.S. Citizen"
    assert _answer("What is your visa status?", p, kind="select",
                   options=["American Citizen", "Green Card Holder", "H-1B"]) == "American Citizen"
    assert _answer("Visa Status", p, kind="select", options=["Citizen - USA", "Citizen - Other Country"]) == \
        "Citizen - USA"
    assert _answer("Visa Status", p, kind="select", options=["H-1B", "F-1", "Green Card"]) is None  # (none is theirs)
    for citizen in ("US Citizen", "United States Citizen", "Citizen of the U.S.", "Citizen - US"):
        assert _answer("Visa Status", p, kind="select", options=[citizen, "Green Card", "H-1B"]) == citizen, citizen
    assert _answer("Visa Status", p, kind="select", options=["Not a U.S. citizen", "U.S. citizen"]) == "U.S. citizen"


def test_a_long_question_that_mentions_a_date_signed_gets_no_date():
    """"If you have previously signed an agreement with us, enter the date signed" asks for a day gone by: only a short
    label ("Date Signed") is the day the form is signed."""
    assert resolve_field(f("If you have previously signed an agreement with us, enter the date signed"), prof()) is None


def test_the_persons_own_answer_for_a_closest_city_list_stands_over_the_profiles_city():
    """Kforce's "Closest Metropolitan City" Region takes the profile's city, unless the person answered it themselves
    (Phoenix, their nearest big city): their answer stands."""
    p = Profile({"personal": {"address": {"city": "Chandler", "state": "AZ"}},
                 "answers": [{"match": "region", "answer": "Phoenix", "question": "Region"}]})
    region = f("Region", "select", section="Closest Metropolitan City",
               options=["Not Specified", "Chandler", "Phoenix"])
    assert resolve_field(region, p).value == "Phoenix"


def test_a_preferred_metro_area_is_the_persons_choice_not_their_city():
    """A list of cities under a heading that asks where the person would like to work ("Preferred Metropolitan Area")
    is their choice, not where they live."""
    p = Profile({"personal": {"address": {"city": "Chandler", "state": "AZ"}}})
    for section in ("Preferred Metropolitan Area", "Desired Metro Area", "Metro area you would like to work in",
                    "Metro area you are willing to work in", "Metro area you would relocate to"):
        field = f("Region", "select", section=section, options=["Not Specified", "Chandler", "Phoenix"])
        assert resolve_field(field, p) is None, section


def test_a_question_of_its_own_is_never_read_from_its_choices():
    """Workday's "Please check one of the boxes below:" is read from its disability choices. A label that asks its own
    question ("Do you require an accommodation? Check the box below", "...that applies to your spouse") is never
    answered from them, though they mention a disability."""
    options = ["Yes, I have a disability, or have had one in the past",
               "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
    p = Profile({"eeo": {"disability": "No"}})
    assert resolve_field(f("Please check one of the boxes below:", "checkbox_group", options=options), p).value == \
        options[1]
    for label in ("Do you require an accommodation? Check the box below",
                  "Check the box below that applies to your spouse"):
        assert resolve_field(f(label, "checkbox_group", options=options), p) is None, label


def test_a_box_that_only_says_to_choose_is_never_read_from_its_choices():
    """A list with no question of its own ("Select one", "Please select"), whose question the page may show
    elsewhere, isn't answered from its choices, though they are the disability ones."""
    options = ["Yes, I have a disability, or have had one in the past",
               "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
    p = Profile({"eeo": {"disability": "No"}})
    for label in ("Select one", "Please select"):
        assert resolve_field(f(label, "radio_group", options=options), p) is None, label


def test_a_text_area_about_work_authorization_that_isnt_a_yes_or_no_question_is_the_persons():
    """Nikon's "Are you legally authorized to work in the US...?" text area takes the profile's Yes or No; a text area
    that asks for an explanation ("Please explain your work authorization", "Visa sponsorship needed (explain)") is
    the person's to write."""
    p = Profile({"work_authorization": {"authorized_to_work": True, "requires_sponsorship": False}})
    assert polarity(_answer("Are you legally authorized to work in the US?", p, kind="textarea")) is True
    for label in ("Please explain your work authorization", "Visa sponsorship needed (explain)"):
        assert _answer(label, p, kind="textarea") is None, label


def _job_with_letters(tmp_path):
    folder = tmp_path / "job"
    folder.mkdir()
    (folder / "cover_letter.pdf").write_bytes(b"%PDF")
    (folder / "resume.pdf").write_bytes(b"%PDF")
    return {"folder": str(folder)}, folder


def test_a_box_or_section_for_the_cover_letter_alone_gets_the_cover_letter(tmp_path):
    """A page's one upload in a section for both ("Resume and Cover Letter") gets the resume; a box there named for the
    cover letter, or a page's one box in a cover letter's own section, gets the cover letter."""
    job, folder = _job_with_letters(tmp_path)
    p = prof()
    assert resolve_field(f("Upload", "file", section="Resume and Cover Letter"), p, job, 1).value == \
        str(folder / "resume.pdf")
    assert resolve_field(f("Cover Letter", "file", section="Resume and Cover Letter"), p, job, 1).value == \
        str(folder / "cover_letter.pdf")
    assert resolve_field(f("Upload", "file", section="Cover Letter"), p, job, 1).value == \
        str(folder / "cover_letter.pdf")


def test_a_section_for_both_that_says_the_resume_is_there_gets_the_cover_letter(tmp_path):
    """A section for both that says the box is only for when the resume isn't there, or that the resume already is,
    keeps the page's one upload for the cover letter."""
    job, folder = _job_with_letters(tmp_path)
    for section in ("Resume or Cover Letter (only if not attached above)",
                    "Your resume was already imported - Resume and Cover Letter"):
        a = resolve_field(f("Upload", "file", section=section), prof(), job, 1)
        assert a is not None and a.value == str(folder / "cover_letter.pdf"), section


def test_a_current_jobs_end_date_drawn_as_a_list_is_left_empty_not_asked():
    """The end of a job the person still has is left empty, as a text box's is, when the form draws it as a list: it's
    neither filled nor asked."""
    fields = [{"id": "1", "kind": "select", "label": "End Year", "section": "Work Experience 1", "value": "",
               "options": ["2024", "2025", "2026"]}]
    plan = plan_autofill(fields, prof())
    assert plan["to_fill"] == [] and plan["needs_input"] == [], plan


def test_a_degree_question_where_equivalent_experience_counts_is_the_persons():
    """"Do you have a bachelor's degree?" under "Bachelor's degree or equivalent experience": a profile without one
    would answer No where the employer counts experience instead, so it's left for the person."""
    p = _person()
    assert _answer("Do you have a bachelor's degree?", p, kind="radio_group", options=["Yes", "No"]) == "No"
    for section in ("Minimum Qualifications: Bachelor's degree or equivalent experience", "Degree or equivalent"):
        assert _answer("Do you have a bachelor's degree?", p, kind="radio_group", options=["Yes", "No"],
                       section=section) is None, section


def test_how_you_heard_found_among_the_choices_is_picked_as_it_is():
    """The profile's "LinkedIn" among a list's sources is picked as it is; only "Company Website" is looked for as the
    employer's own site."""
    p = prof()  # (how_did_you_hear: LinkedIn)
    for options in (["Company Website", "Acme Careers Site", "LinkedIn", "Indeed"],
                    ["LinkedIn", "Indeed", "Acme Website"]):
        a = resolve_field(f("How did you hear about us?", "select", options=options), p, {"company": "Acme"})
        assert a is not None and a.value == "LinkedIn", options


def test_a_search_prompt_in_a_schools_block_that_shows_the_profiles_choice_picks_it():
    """Workday's Degree and Field of Study in an Education block are search prompts: one that already shows the
    profile's choice ("Bachelor's Degree" for its BS, "Engineering, Electrical") picks it, rather than searching for
    the profile's own words, which the prompt may not find."""
    p = Profile({"education_history": [{"school": "Arizona State University", "degree": "BS Electrical Engineering",
                                        "major": "Electrical Engineering", "start": 2016, "end": 2020}]})
    degree = f("Degree", "combobox", section="Education 1", search=True,
               options=["Associate's Degree", "Bachelor's Degree", "Master's Degree"])
    assert resolve_field(degree, p).value == "Bachelor's Degree"
    major = f("Field of Study", "combobox", section="Education 1", search=True,
              options=["Engineering, Electrical", "Engineering, Mechanical"])
    assert resolve_field(major, p).value == "Engineering, Electrical"


def test_a_list_in_a_schools_block_picks_its_own_choice_whatever_its_kind():
    """A school's Degree drawn as a list, radio buttons or check boxes takes the list's own choice for the profile's
    degree ("Bachelor's Degree" for its BS), never the profile's words, which none of them has."""
    p = Profile({"education_history": [{"school": "Arizona State University", "degree": "BS Electrical Engineering",
                                        "start": 2016, "end": 2020}]})
    for kind in ("select", "radio_group", "checkbox_group"):
        degree = f("Degree", kind, section="Education 1", options=["Associate's Degree", "Bachelor's Degree"])
        assert resolve_field(degree, p).value == "Bachelor's Degree", kind


def test_a_cv_box_beside_another_file_box_gets_the_resume():
    """Among a page's file boxes, the one named for a CV or a Curriculum Vitae is the resume's."""
    for label in ("Upload CV", "Curriculum Vitae"):
        fields = [{"id": "1", "kind": "file", "label": label},
                  {"id": "2", "kind": "file", "label": "Other Attachments"}]
        assert [t["id"] for t in plan_autofill(fields, prof())["to_fill"]] == ["1"], label


def test_a_resume_box_for_a_profile_without_a_resume_is_asked():
    """With no resume in the profile, the resume's box is asked about, never filled with nothing."""
    plan = plan_autofill([{"id": "1", "kind": "file", "label": "Upload Resume", "required": True}],
                         Profile({"personal": {"first_name": "Sam"}}))
    assert plan["to_fill"] == [] and [q["id"] for q in plan["needs_input"]] == ["1"], plan


def test_a_resume_import_box_beside_the_upload_is_left_however_it_says_so():
    """TI's "Import your profile from resume" beside "Upload Resume" (two file boxes): the import is left however it
    says it reads the resume (autofill, auto fill, parse, populate); only the upload gets it."""
    for label in ("Import your profile from resume", "Autofill with resume", "Auto fill from resume",
                  "Parse your resume", "Populate from resume"):
        fields = [{"id": "1", "kind": "file", "label": label}, {"id": "2", "kind": "file", "label": "Upload Resume"}]
        assert [t["id"] for t in plan_autofill(fields, prof())["to_fill"]] == ["2"], label


def test_a_pages_one_upload_in_a_section_for_both_gets_the_resume_however_it_is_worded(tmp_path):
    """A page's one upload in a section for both the resume and the cover letter gets the resume, however the section
    puts it: "Upload your Resume and Cover Letter", "Attach your CV or Cover Letter", "Resume and Cover Letter (5MB
    max)", "CV or Cover Letter - 5MB max"."""
    job, folder = _job_with_letters(tmp_path)
    for section in ("Upload your Resume and Cover Letter", "Attach your CV or Cover Letter",
                    "Resume and Cover Letter (5MB max)", "CV or Cover Letter - 5MB max"):
        a = resolve_field(f("Upload", "file", section=section), prof(), job, 1)
        assert a is not None and a.value == str(folder / "resume.pdf"), section


def test_a_signed_forms_date_in_a_certification_block_is_today():
    """A bare "Date" in a license's or a certification's block asks when that was; a "Date Signed" there (an
    "Applicant Certification" signed at the end of a form) is still today."""
    from datetime import date

    a = resolve_field(f("Date Signed", section="Applicant Certification"), prof())
    assert a is not None and (a.value, a.rule) == (date.today().strftime("%m/%d/%Y"), "signed_date")


def test_a_spouse_at_the_employer_is_answered_as_a_relatives_question():
    """"Is your spouse or partner employed by Acme?" is the relatives question, answered from the profile's no (only a
    self-identification question about a family member is left: it isn't the person's own)."""
    p = Profile({"background": {"relatives_at_employer": False}})
    assert _answer("Is your spouse or partner employed by Acme?", p, kind="radio_group", options=["Yes", "No"]) == "No"


def test_a_without_sponsorship_question_drawn_as_a_text_area_takes_the_profiles_yes():
    """As Nikon's authorization and sponsorship text areas do, one asking whether the person can work without
    sponsorship takes the profile's plain Yes."""
    p = Profile({"work_authorization": {"authorized_to_work": True, "requires_sponsorship": False}})
    assert _answer("Can you work in the U.S. without sponsorship?", p, kind="textarea") == "Yes"


def test_a_work_question_drawn_as_a_text_area_takes_the_yes_or_no_however_it_starts():
    """A yes/no work question in a text area takes the profile's Yes or No whether it asks "Are you", "Do you", "Will
    you", "Would you" or "Have you"."""
    p = Profile({"work_authorization": {"authorized_to_work": True, "requires_sponsorship": False}})
    assert _answer("Will you require visa sponsorship?", p, kind="textarea") == "No"
    assert _answer("Would you require visa sponsorship?", p, kind="textarea") == "No"
    assert polarity(_answer("Have you been authorized to work in the U.S.?", p, kind="textarea")) is True


def test_where_the_person_lives_asked_in_a_text_area_is_never_an_essay():
    """GCU's "In what city do you live?" text area takes the profile's city, as "Which city do you live in?" does;
    one that goes on to ask for more (and..., why, explain, describe, tell us) is an essay, the person's."""
    p = Profile({"personal": {"address": {"city": "Chandler", "state": "Arizona"}}})
    assert _answer("Which city do you live in?", p, kind="textarea") == "Chandler"
    for label in ("In what state do you live, and how far is it from our site?",
                  "What state do you live in? Why did you move there?",
                  "Which state do you live in? Explain any plans to move",
                  "What state do you live in? Describe your commute",
                  "What state do you live in? Tell us about your commute"):
        assert _answer(label, p, kind="textarea") is None, label


def test_where_you_are_located_among_countries_is_never_the_united_states_for_someone_elsewhere():
    """Axon's "Where are you currently located?" among countries is the United States for a profile there, and never
    for a profile whose address is in another country."""
    countries = ["Canada", "Mexico", "United States"]
    here = Profile({"personal": {"address": {"city": "Chandler", "state": "AZ", "country": "United States"}}})
    assert _answer("Where are you currently located?", here, kind="select", options=countries) == "United States"
    there = Profile({"personal": {"address": {"city": "Toronto", "state": "ON", "country": "Canada"}}})
    assert _answer("Where are you currently located?", there, kind="select", options=countries) != "United States"


def test_a_degree_question_whose_choices_arent_read_is_left_for_the_person():
    """"Do you have a bachelor's degree?" drawn as a list whose choices weren't read is the person's: its plain Yes or
    No is picked only among choices shown."""
    assert _answer("Do you have a bachelor's degree?", _person(), kind="combobox") is None


def test_a_label_that_only_says_how_to_answer_is_read_from_its_choices_however_its_worded():
    """Workday's "Please check one of the boxes below:" is read from its disability choices: so is any label that only
    says how to answer and points at the choices, whatever its verb (select, choose, tick, mark, pick) and words."""
    options = ["Yes, I have a disability, or have had one in the past",
               "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
    p = Profile({"eeo": {"disability": "No"}})
    for label in ("Select one of the following", "Choose one below", "Tick the box", "Mark the appropriate box",
                  "Pick one below", "Check any that apply below", "Check all that apply below",
                  "Select only one of the boxes", "Tick a box below", "Select an option below",
                  "Select one of the options below", "Select an answer below", "Choose one of the answers below",
                  "Choose a response below", "Select which applies below"):
        a = resolve_field(f(label, "radio_group", options=options), p)
        assert a is not None and a.value == options[1], label


def test_a_list_with_no_question_of_its_own_isnt_a_self_identification_by_its_choices():
    """A veteran list with no label of its own (the page shows its question elsewhere) isn't answered, though its
    choices are the veteran ones and offer a decline."""
    options = ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
               "I don't wish to answer"]
    p = Profile({"eeo": {"veteran": "I am not a protected veteran"}})
    assert resolve_field(f("Are you a protected veteran?", "radio_group", options=options), p).value == options[0]
    assert resolve_field(f("", "radio_group", options=options), p) is None


def test_an_accommodation_question_is_never_answered_from_the_disability_status():
    """"Do you need an accommodation for a disability?" mentions a disability but isn't the self-identification: its
    Yes/No/decline, or choices that say whether the person has a disability, are the person's to answer."""
    p = Profile({"eeo": {"disability": "No"}})
    q = "Do you need an accommodation for a disability?"
    assert _answer(q, p, kind="radio_group", options=["Yes", "No", "I don't wish to answer"]) is None
    disability_said = ["Yes, I have a disability", "No, I don't have a disability"]
    assert _answer(q, p, kind="radio_group", options=disability_said) is None


def test_a_text_area_for_linkedin_salary_or_start_takes_the_profiles():
    """A text area is a long answer the person writes, except for the LinkedIn profile, the desired salary and the
    start date, which the profile's own words answer."""
    p = Profile({"personal": {"linkedin_url": "https://www.linkedin.com/in/example"},
                 "preferences": {"desired_salary": "$60,000", "earliest_start": "2 weeks after offer"}})
    assert _answer("LinkedIn Profile", p, kind="textarea") == "https://www.linkedin.com/in/example"
    assert _answer("Desired salary", p, kind="textarea") == "$60,000"
    assert _answer("When can you start?", p, kind="textarea") == "2 weeks after offer"


def test_a_state_box_that_asks_for_the_abbreviation_gets_it():
    """"State (abbreviation)" takes the state's two letters, as "In what state do you live? (Please use the state
    abbreviation format)" does."""
    p = Profile({"personal": {"address": {"state": "Arizona"}}})
    assert _answer("State (abbreviation)", p) == "AZ"


def test_a_long_list_that_is_all_there_is_isnt_searched_for_an_answer_it_lacks():
    """A list of 150 countries that is neither a search prompt nor a paged list shows all it has: without the
    profile's, it's the person's, never a search for it."""
    countries = [f"Country {i}" for i in range(150)]
    assert _answer("Country", prof(), kind="select", options=countries) is None
    assert _answer("Country", prof(), kind="select", options=countries, paged=True) == "United States"


def test_a_persons_no_for_a_lone_box_leaves_it_unticked():
    """A single box the person's own answers name is ticked for their Yes and left unticked for their No."""
    for said, ticked in (("Yes", True), ("No", False)):
        p = Profile({"answers": [{"match": "reliable transportation", "answer": said}]})
        a = resolve_field(f("I have reliable transportation", "checkbox"), p)
        assert a is not None and a.value is ticked, said


# The wrong answers the mutation pass found (Oct 18)


def test_a_lone_box_is_ticked_only_by_a_yes_never_by_a_decline_or_a_maybe():
    """A single box the person's own answer names was ticked for any answer but a no: "Decline to self-identify"
    ticked "I identify as a protected veteran", and "Maybe" ticked "I am willing to relocate". Only an answer that
    reads as a yes ticks it; a decline, "Prefer not to say", "Maybe", "Only if required" or anything else that reads
    as neither yes nor no leaves it."""
    for match, label in (("veteran", "I identify as a protected veteran"), ("disabilit", "I have a disability"),
                         ("relocat", "I am willing to relocate")):
        for said in ("Decline to self-identify", "Prefer not to say", "Maybe", "Only if required", "Not applicable"):
            p = Profile({"answers": [{"match": match, "answer": said}]})
            a = resolve_field(f(label, "checkbox"), p)
            assert a is None or a.value is False, (label, said, a)
            assert not plan_autofill([f(label, "checkbox")], p)["to_fill"], (label, said)
        p = Profile({"answers": [{"match": match, "answer": "Yes"}]})
        assert resolve_field(f(label, "checkbox"), p).value is True, label


def test_a_long_signature_date_label_gets_todays_date_never_the_name():
    """"Signature Date (please enter the date in MM/DD/YYYY format)" is the signing date as "Signature Date" is:
    today. Over 45 characters it went to the signature rule, and the person's name went into the date box. A label
    that names a date is never the signature box; a long question that mentions a date signed further on still gets
    no date, and "Signature" alone is still the name."""
    from datetime import date

    today, p = date.today().strftime("%m/%d/%Y"), prof()
    for label in ("Signature Date (please enter the date in MM/DD/YYYY format)",
                  "Signature date - enter today's date as MM/DD/YYYY",
                  "Signature Date: Please enter the date you are signing this form"):
        a = resolve_field(f(label), p)
        assert a is not None and (a.value, a.rule) == (today, "signed_date"), (label, a)
    assert resolve_field(f("Electronic Signature Date"), p).value == today  # (a short one, wherever it says so)
    named = resolve_field(f("Enter the date beside your electronic signature (MM/DD/YYYY)"), p)
    assert named is None or named.rule != "signature", named
    assert resolve_field(f("If you have previously signed an agreement with us, enter the date signed"), p) is None
    for label in ("Signature", "Please sign your full name"):
        assert resolve_field(f(label), p).value == "Sam Rivera", label


def test_a_us_citizen_question_written_without_dots_is_the_us_citizen_question():
    """"Are you a US Citizen?" is the U.S. citizen question as "Are you a U.S. citizen?" is: the profile's Yes in a
    text box or among Yes and No. It was read as the citizenship question ("United States" in a box, nothing among
    Yes and No). "Us" the pronoun is never the country."""
    p = Profile({"work_authorization": {"us_citizen": True, "citizenship": "United States"}})
    for label in ("Are you a US Citizen?", "Are you a USA citizen?", "Are you a citizen of the US?",
                  "Are you a United States citizen?"):
        assert _answer(label, p) == "Yes", label
        assert _answer(label, p, kind="radio_group", options=["Yes", "No"]) == "Yes", label
    for label in ("Are you willing to join us?", "Tell us about your citizenship"):
        a = resolve_field(f(label), p, {"company": "Acme"})
        assert a is None or a.rule != "us_citizen", (label, a)


def test_a_closest_city_box_with_no_city_in_the_profile_gets_nothing_not_the_state():
    """Kforce's "Closest Metropolitan City" Region takes the profile's city; a profile without one leaves it, never
    its state ("Arizona" went into the box)."""
    p = Profile({"personal": {"address": {"state": "AZ", "country": "United States"}}})
    assert resolve_field(f("Region", section="Closest Metropolitan City"), p) is None
    region = f("Region", "select", section="Closest Metropolitan City", options=["Not Specified", "Anthem", "Arizona"])
    assert resolve_field(region, p) is None


def test_is_sponsorship_required_drawn_as_a_text_area_takes_the_profiles_no():
    """Nikon's sponsorship text area takes the profile's plain No however the question starts: "Is sponsorship
    required for you to work in the US?" was left (only "are/do/will/... you" questions were read as yes or no)."""
    p = Profile({"work_authorization": {"authorized_to_work": True, "requires_sponsorship": False}})
    assert _answer("Is sponsorship required for you to work in the US?", p, kind="textarea") == "No"
    assert _answer("Is visa sponsorship needed for you?", p, kind="textarea") == "No"
    assert _answer("Is employment sponsorship required for you?", p, kind="textarea") == "No"
    assert _answer("Visa sponsorship needed (explain)", p, kind="textarea") is None


# A review of those fixes (Oct 18)


def _work(**said):
    return Profile({"work_authorization": said})


def test_a_us_citizen_question_that_goes_on_with_or_or_by_is_never_answered_as_citizenship_alone():
    """"Are you a US citizen or do you have authorization to work in the US?" is true for a permanent resident or a
    visa holder allowed to work, but the citizen rule read only its start and answered No for them (with "U.S." too).
    "...by birth?" asks what the profile never says. Both are left to the other rules or the person, never a No nor
    the citizenship's country; the question alone, "(Yes/No)" and a note after it are still the citizen question, and
    "US citizen or permanent resident" still the U.S. person's."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, us_person=True,
               citizenship="Mexico")
    visa = _work(authorized_to_work=True, requires_sponsorship=True, us_citizen=False, us_person=False,
                 citizenship="India")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for us in ("US", "U.S."):
        for rest in ("or do you have authorization to work in the US?", "or do you hold a valid work visa?",
                     "or do you have an EAD?", "or otherwise able to work in the US without restriction?",
                     "or can you provide proof of work eligibility?"):
            label = f"Are you a {us} citizen {rest}"
            for p in (pr, visa):
                for said in (_answer(label, p, **yes_no), _answer(label, p)):
                    assert said not in ("No", "Mexico", "India"), (label, said)
        for p in (citizen, pr):
            assert _answer(f"Are you a {us} citizen by birth?", p, **yes_no) is None, us
            assert _answer(f"Are you a {us} citizen by birth?", p) is None, us
        assert _answer(f"Are you a {us} citizen?", citizen, **yes_no) == "Yes"
        assert _answer(f"Are you a {us} citizen? (Yes/No)", pr) == "No"
        assert _answer(f"Are you a {us} citizen? This position requires US citizenship.", citizen) == "Yes"
        a = resolve_field(f(f"Are you a {us} citizen or permanent resident?", **yes_no), pr, {"company": "Acme"})
        assert a is not None and (a.value, a.rule) == ("Yes", "us_person"), a


def test_a_us_citizen_question_that_asks_more_than_citizenship_is_left():
    """The U.S. citizen question is answered only where nothing after it asks more: the label ends there, or goes on
    with a "?", a note in brackets ("(Yes/No)") or a sentence that only says something ("This position requires US
    citizenship."). "...and able to obtain a security clearance", "...eligible for a security clearance", "...with an
    active clearance" or "...who can work on-site" ask what citizenship alone doesn't say (a citizen's Yes claimed them
    all): left, in any spelling."""
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for us in ("US", "U.S.", "USA"):
        for rest in ("and able to obtain a security clearance?", "eligible for a security clearance?",
                     "with an active clearance?", "who can work on-site?"):
            label = f"Are you a {us} citizen {rest}"
            assert _answer(label, citizen, **yes_no) is None, label
            assert _answer(label, citizen) is None, label
        for label in (f"Are you a {us} citizen?", f"Are you a {us} citizen? (Yes/No)",
                      f"Are you a {us} citizen? This position requires US citizenship.",
                      f"Are you a citizen of the {us}?"):
            assert _answer(label, citizen) == "Yes", label
            assert _answer(label, citizen, **yes_no) == "Yes", label


def test_a_signature_box_that_says_the_date_is_recorded_gets_the_name_and_one_that_asks_for_a_date_doesnt():
    """A signature box that says the date is added for the person ("Signature (type your name; the date is added
    automatically)", "Sign your full name (no dates)") takes the name, as before. One that asks for a date to be
    entered ("Enter the date beside your electronic signature"), or for a signature and a date in one box, never
    does."""
    p = prof()
    for label in ("Signature (type your name; the date is added automatically)",
                  "Signature (type your full name; today's date will be recorded)",
                  "Signature of Applicant (type full name). Date will be captured upon submission.",
                  "Signature: Type your name. Dates are recorded automatically.", "Sign your full name (no dates)"):
        a = resolve_field(f(label), p)
        assert a is not None and (a.value, a.rule) == ("Sam Rivera", "signature"), (label, a)
    for label in ("Enter the date beside your electronic signature (MM/DD/YYYY)", "Signature and Date",
                  "Applicant Signature & Date", "Signature/Date"):
        a = resolve_field(f(label), p)
        assert a is None or a.rule != "signature", (label, a)


def test_a_long_date_signed_label_that_names_another_day_gets_no_date():
    """A long label that starts with "Date signed" or "Signature date" but asks when something else was signed or
    happened (a non-compete, an offer letter, a military discharge, the last day employed) gets no date, nor the name;
    one about this form's own signing still gets today."""
    from datetime import date

    p = prof()
    for label in ("Date signed (if you signed a non-compete with your current employer)",
                  "Date signed - Please enter the date you signed the attached offer letter",
                  "Signature date: enter the date of your military discharge",
                  "Signature date of your current non-compete agreement, if any",
                  "Date Signed (enter the date you signed this application, or the date you were last employed)"):
        assert resolve_field(f(label), p) is None, label
    a = resolve_field(f("Signature Date (date you signed the self-identification form)"), p)
    assert a is not None and a.value == date.today().strftime("%m/%d/%Y"), a


def test_a_sponsorship_question_about_someone_else_or_the_employers_policy_is_left():
    """"Is sponsorship available for candidates who need it?" asks the employer's policy, and "...required for your
    spouse or dependents?" or "...for any family member?" someone else's need: never the person's own No, in a text
    area, a text box or a list."""
    p = _work(authorized_to_work=True, requires_sponsorship=False)
    for label in ("Is sponsorship available for candidates who need it?",
                  "Is employment sponsorship required for any family member?",
                  "Is sponsorship required for your spouse or dependents?",
                  "Will you require sponsorship for your spouse?"):
        for kind in ("textarea", "text"):
            assert _answer(label, p, kind=kind) is None, (label, kind)
        assert _answer(label, p, kind="radio_group", options=["Yes", "No"]) is None, label
    assert _answer("Will you require sponsorship?", p, kind="radio_group", options=["Yes", "No"]) == "No"


def test_a_profile_whose_citizenship_is_the_united_states_answers_the_us_citizen_question():
    """A profile that says its citizenship is the United States, but not us_citizen, answers "Are you a US Citizen?"
    Yes (it gave the country, "United States", in a text box and nothing among Yes and No). Another country's
    citizenship says nothing of a second, U.S. one: left."""
    p = _work(citizenship="United States")
    assert _answer("Are you a US Citizen?", p) == "Yes"
    assert _answer("Are you a U.S. citizen?", p, kind="radio_group", options=["Yes", "No"]) == "Yes"
    assert _answer("Are you a US Citizen?", _work(citizenship="Mexico")) is None
    assert _answer("Are you a US Citizen?", _work(us_citizen=False, citizenship="United States")) == "No"


# A re-review of those checks (Oct 18): each had reached too far


def test_a_us_citizen_question_followed_by_an_if_not_question_is_left():
    """"Are you a US citizen? If not, do you have authorization to work in the US?" asks a second question the
    citizenship alone doesn't answer (a permanent resident's No would deny the authorization): left, as are "If no,
    do you have an EAD?", the same on a new line, and "If not, please list your citizenship" / "If not, which country?"
    (a text box there takes the citizenship's country, as before). A sentence that only says something, "or not" and
    "or (US) national" are still the citizen question; and a label asking for the country of citizenship still gets
    it, whatever it asks in brackets."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, us_person=True,
               citizenship="Mexico")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Are you a US citizen? If not, do you have authorization to work in the US?",
                  "Are you a U.S. citizen? If not, do you have authorization to work in the U.S.?",
                  "Are you a US citizen? If not, do you hold a valid work visa?",
                  "Are you a US citizen? If no, do you have an EAD?",
                  "Are you a US citizen?\nIf not, do you have authorization to work in the US?",
                  "Are you a US citizen? If not, please list your citizenship",
                  "Are you a citizen of the US? If not, which country?"):
        for p in (pr, citizen):
            assert _answer(label, p, **yes_no) is None, label
            assert _answer(label, p) not in ("Yes", "No"), label
    for label in ("Are you a US citizen? This position requires US citizenship.",
                  "Are you a US citizen? Note: we cannot sponsor visas.", "Are you a U.S. citizen or not?",
                  "Are you a US citizen or not?", "Are you a U.S. citizen or national?",
                  "Are you a US citizen (or a US national)?"):
        assert (_answer(label, citizen, **yes_no), _answer(label, pr, **yes_no)) == ("Yes", "No"), label
    assert _answer("Country of citizenship (are you a citizen of more than one country?)", pr) == "Mexico"


def test_a_us_citizen_question_with_another_question_after_it_is_left_however_that_one_is_put():
    """A second question after "Are you a US citizen?", however it opens ("If you are not a US citizen, do you...",
    "Otherwise, ...", "Non-citizens: ...", "Do you hold an active security clearance?", "If yes, ..."), or a request
    ("If not, list your visa type."): a Yes or No there would answer it too, a permanent resident's No denying the work
    authorization they have, a citizen's Yes claiming a clearance the profile doesn't hold. Left; a sentence that only
    says something is still the citizen question (the third review of Oct 18)."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, us_person=True,
               citizenship="Mexico")
    visa = _work(authorized_to_work=True, requires_sponsorship=True, us_citizen=False, citizenship="India")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Are you a US citizen? If you are not a US citizen, do you have authorization to work in the US?",
                  "Are you a US citizen? If you are not, do you hold a valid work visa?",
                  "Are you a US citizen? If not a US citizen, do you hold a valid work visa?",
                  "Are you a US citizen? If not - do you hold a valid work visa?",
                  "Are you a US citizen? If not: do you have an EAD?",
                  "Are you a US citizen? If not, then do you hold a valid work visa?",
                  "Are you a US citizen? Otherwise, do you hold a valid work visa?",
                  "Are you a US citizen? Non-citizens: do you hold a valid work visa?",
                  "Are you a US citizen? If no, is your work visa valid for this role?",
                  "Are you a U.S. citizen? If not, does your visa allow you to work for us?",
                  "Are you a US citizen? Do you hold an active security clearance?",
                  "Are you a US citizen? If yes, do you hold an active clearance?",
                  "Are you a U.S. citizen? Have you ever held a security clearance?",
                  "Are you a US citizen? Are you willing to obtain a clearance?",
                  "Are you a US citizen? Can you work on-site in Chandler?",
                  "Are you a US citizen? If not, list your visa type.",
                  "Are you a US citizen? If no, indicate your current visa status."):
        for p in (pr, visa, citizen):
            assert _answer(label, p, **yes_no) is None, (label, p)
            assert _answer(label, p) not in ("Yes", "No"), (label, p)
    for label in ("Are you a US citizen? This position requires US citizenship.",
                  "Are you a US citizen? Note: we cannot sponsor visas."):
        assert (_answer(label, citizen, **yes_no), _answer(label, pr, **yes_no)) == ("Yes", "No"), label


def test_a_us_citizen_question_beside_another_question_anywhere_in_the_label_is_left():
    """The whole label is read: a second question after a statement ("This role requires it. If not, do you hold a
    valid work visa?"), one before the citizen question ("Do you hold a valid work visa? Are you a US citizen?") or in
    its own sentence ("Can you work on-site in Chandler, and are you a US citizen?") would be answered by its Yes or
    No too: left. Notes and words on how to answer around it ask nothing: still the citizen question (the fourth
    review of Oct 18)."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, us_person=True,
               citizenship="Mexico")
    visa = _work(authorized_to_work=True, requires_sponsorship=True, us_citizen=False, citizenship="India")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Are you a US citizen? Yes or No. If not, do you have authorization to work in the US?",
                  "Are you a US citizen? This role requires it. If not, do you hold a valid work visa?",
                  "Are you a US citizen? Note: ITAR applies. Do you hold an active security clearance?",
                  "Are you a US citizen? This position requires citizenship. Can you obtain a clearance?",
                  "Do you hold an active security clearance? Are you a US citizen?",
                  "Do you hold a valid work visa? Are you a US citizen?",
                  "Are you willing to relocate? Are you a US citizen?",
                  "Can you work on-site in Chandler, and are you a US citizen?",
                  "Have you ever held a clearance, and are you a U.S. citizen?",
                  "Are you a US citizen? Answer Yes or No. If no, which visa do you hold?"):
        for p in (pr, visa, citizen):
            assert _answer(label, p, **yes_no) is None, (label, p)
            assert _answer(label, p) not in ("Yes", "No"), (label, p)
    # (only what's on the short list beside it: still the citizen question)
    for label in ("Are you a US citizen? Please select Yes or No.", "Are you a US citizen? Choose Yes or No.",
                  "Are you a US citizen? Select one.", "Citizenship: Are you a US citizen?",
                  "U.S. Citizenship - Are you a U.S. citizen?",
                  "Are you a US citizen? Proof of citizenship will be required.",
                  "Are you a U.S. citizen? U.S. citizenship is required for this position.",
                  "Are you a US citizen? This position requires US citizenship."):
        assert (_answer(label, citizen, **yes_no), _answer(label, pr, **yes_no)) == ("Yes", "No"), label


def test_a_us_citizen_question_beside_anything_off_the_short_list_is_left_for_the_person():
    """Beside the citizen question only a numbering or a heading of its own, the answers it takes and a few sentences
    that say no more than it keep it answered. Anything else, however harmless it looks ("Please note that ...",
    "Indicate below.", "If hired, proof of citizenship will be required."), is left for the person: a list of what may
    change a Yes's meaning never ends ("Select Yes if currently employed by Acme.", "Must hold an active Secret.",
    "Willing to relocate (Y/N)": the sixth review of Oct 18)."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, us_person=True,
               citizenship="Mexico")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Are you a US citizen? Select Yes if currently employed by Acme.",
                  "Are you a US citizen? Answer Yes if eligible for a Secret.",
                  "Are you a US citizen? Answer Yes when holding an active TS/SCI.",
                  "Are you a US citizen? Select Yes only for current Secret holders.",
                  "Are you a US citizen? Willing to relocate (Y/N)", "Willing to relocate: Are you a US citizen?",
                  "Are you a US citizen? This role requires an active TS/SCI with polygraph.",
                  "Are you a US citizen? Must hold an active Secret.",
                  "Are you a US citizen? Applicants must be US citizens with an active Secret.",
                  "Are you a US citizen? Relocation to Phoenix is required.",
                  "Are you a US citizen? A bachelor's degree is required for this role.",
                  "TS/SCI required: Are you a US citizen?",
                  "Are you a U.S. citizen? Please note that this position requires U.S. citizenship.",
                  "Are you a US citizen? Indicate below.",
                  "Are you a US citizen? State law requires us to verify eligibility.",
                  "Are you a US citizen? If hired, proof of citizenship will be required.",
                  "Are you a US citizen? Will be verified upon hire.", "Are you a US citizen (as required by ITAR)?",
                  "Are you a US citizen? (e.g. born or naturalized)"):
        for p in (pr, citizen):
            assert _answer(label, p, **yes_no) is None, (label, p)
    for label in ("3) Are you a US citizen?", "Q3: Are you a US citizen?", "Required: Are you a US citizen?",
                  "Are you a US citizen? Y/N", "Are you a US citizen? [Yes/No]", "Are you a US citizen (yes/no)?",
                  "Are you a U.S. citizen or not?", "Are you a US citizen (or a US national)?"):
        assert (_answer(label, citizen, **yes_no), _answer(label, pr, **yes_no)) == ("Yes", "No"), label


def test_a_us_citizen_question_with_anything_around_it_that_speaks_to_the_person_is_left():
    """Around the citizen question only what speaks to no one keeps it answered: a request ("Confirm that you hold an
    active security clearance."), a question in brackets, a condition on the Yes ("Select Yes only if you also hold a
    clearance."), another requirement ("This role also requires an active Secret clearance."), a question before it
    ended by a dash or a colon: left, a Yes there claiming what the profile doesn't say (the fifth review of Oct 18)."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, us_person=True,
               citizenship="Mexico")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Are you a US citizen? Confirm that you hold an active security clearance.",
                  "Are you a US citizen? In addition, please confirm you hold an active clearance.",
                  "Are you a US citizen? Additionally, confirm you can work on-site in Chandler.",
                  "Are you a US citizen? Also confirm you are willing to relocate.",
                  "Are you a US citizen? Tell us if you hold a work visa.",
                  "Are you a US citizen? Also do you hold a clearance",
                  "Are you a US citizen? Yes/No - and do you hold a clearance",
                  "Are you a US citizen (and do you hold a clearance)?",
                  "Are you a US citizen? (This position requires U.S. citizenship. Do you hold an active clearance?)",
                  "Are you a US citizen? Please indicate below whether you hold an active security clearance.",
                  "Are you a US citizen? Select Yes only if you can also obtain a Secret clearance.",
                  "Are you a US citizen? Do not answer yes unless you also hold a clearance.",
                  "Are you a US citizen? If selected, can you start within two weeks",
                  "Are you a US citizen? This role also requires an active Secret clearance.",
                  "Are you a US citizen? You must also be able to obtain a clearance.",
                  "Clearance holders only. Are you a US citizen?",
                  "Do you hold an active clearance - Are you a US citizen?",
                  "Do you hold an active clearance: Are you a US citizen?",
                  "Do you hold an active clearance. Are you a US citizen?",
                  "Are you a 'US' citizen? Do you hold an active clearance?"):
        for p in (pr, citizen):
            assert _answer(label, p, **yes_no) is None, (label, p)
            assert _answer(label, p) not in ("Yes", "No"), (label, p)
    for label in ("3) Are you a US citizen?", "Q3: Are you a US citizen?", "Are you a US citizen? Y/N",
                  "Are you a US citizen? [Yes/No]"):
        assert (_answer(label, citizen, **yes_no), _answer(label, pr, **yes_no)) == ("Yes", "No"), label


def test_the_smaller_sponsorship_and_signature_wordings_of_the_third_review():
    """"E-Signature (enter the current date)" asks for the date: never the name. "...for any of your dependents" and
    "...for your domestic partner" are another person's sponsorship; "Will Acme be required to provide sponsorship for
    you?" is the person's own need; and an "Is sponsorship required?" text area that asks which visa is left."""
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True)
    assert _answer("E-Signature (enter the current date)") != "Sam Rivera"  # (_person's name)
    assert _answer("E-Signature") == "Sam Rivera"
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Is sponsorship required for any of your dependents to work in the US?",
                  "Will you require sponsorship for your domestic partner?"):
        assert _answer(label, citizen, **yes_no) is None, label
        assert _answer(label, citizen, kind="textarea") is None, label
    for label in ("Will Acme be required to provide sponsorship for you?",
                  "Would the company need to provide visa sponsorship for you to work here?"):
        assert _answer(label, citizen, **yes_no) == "No", label
    for label in ("Is sponsorship required? If yes, please specify the visa type.",
                  "Is sponsorship required? If yes, which visa?"):
        assert _answer(label, citizen, kind="textarea") is None, label


def test_a_us_citizen_question_with_its_yes_or_no_written_after_it_is_still_answered():
    """"Are you a US citizen: Yes/No" and "..., yes or no?" name the answers the question takes, nothing more: the
    citizen question, in every spelling, as "U.S." was before the reviews of Oct 18. One that goes on past them still
    asks more."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, citizenship="Mexico")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Are you a US citizen: Yes/No", "Are you a U.S. citizen: Yes/No", "Are you a US citizen, yes or no?",
                  "Are you a U.S. citizen - Yes / No", "Are you a USA citizen (yes/no)?"):
        assert (_answer(label, citizen, **yes_no), _answer(label, pr, **yes_no)) == ("Yes", "No"), label
    assert _answer("Are you a US citizen, yes or no, and able to obtain a security clearance?", citizen,
                   **yes_no) is None


def test_a_us_citizen_question_is_read_as_the_form_gives_it_and_by_the_citizen_rules_alone():
    """The seventh review of Oct 18: the check read the label with the employer's notes taken out, so an instruction in
    a note ("(Note: green card holders should select Yes.)") went unseen; and the other work rules read a citizen
    question's label before the citizen rule ("Are you a US citizen? Will you require visa sponsorship?" took a
    citizen's No, "Work Authorization: Are you a US citizen?" a permanent resident's Yes, "...(do not include permanent
    residents)?" the U.S. person's Yes). Now the check reads the whole label and only the citizen rules read a citizen
    question: anything beside it is left. The employer's plain word that it won't sponsor still says no more, a "Work
    Authorization" heading is its own, and "...citizen or permanent resident?" is still the U.S. person's question."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, us_person=True,
               citizenship="Mexico")
    visa = _work(authorized_to_work=True, requires_sponsorship=True, us_citizen=False, us_person=False,
                 citizenship="India")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Are you a US citizen? (Note: green card holders should select Yes.)",
                  "Are you a US citizen? (Please note: if you hold a green card, select Yes.)",
                  "Are you a US citizen? We are unable to sponsor visas, so select Yes if you hold a green card.",
                  "Are you a US citizen? (This position is not eligible for visa sponsorship; select No if you will "
                  "need it.)",
                  "Are you a US citizen? Will you require visa sponsorship now or in the future?",
                  "Are you a U.S. citizen? Do you now or will you in the future require sponsorship?",
                  "Are you a US citizen and authorized to work in the US?",
                  "Are you a US citizen without need of sponsorship?",
                  "Are you a US citizen? If not, sponsorship may be required.",
                  "Are you a US citizen? If not, valid work authorization is required.",
                  "Are you a US citizen (do not include permanent residents)?",
                  "Are you a US citizen by birth or naturalization (not a permanent resident)?",
                  "Are you a US citizen or do you hold an H-1B visa?"):
        for p in (citizen, pr, visa):
            assert _answer(label, p, **yes_no) is None, (label, p.get("work_authorization.citizenship"))
    for label in ("Work Authorization: Are you a US citizen?", "Work Authorization Status - Are you a U.S. citizen?",
                  "Are you a US citizen? Note: this position is not eligible for visa sponsorship.",
                  "Are you a US citizen? (We are unable to sponsor visas for this role.)",
                  "Are you a U.S. citizen? No visa sponsorship is available for this position.",
                  "Are you a US citizen? We do not offer sponsorship.",
                  "Are you a US citizen? (This position requires U.S. citizenship.)"):
        assert [_answer(label, p, **yes_no) for p in (citizen, pr, visa)] == ["Yes", "No", "No"], label
    for label in ("Are you a U.S. citizen or permanent resident?", "Are you a U.S. citizen or green card holder?",
                  "Are you a US citizen, national, lawful permanent resident, refugee or asylee?"):
        a = resolve_field(f(label, **yes_no), pr, {"company": "Acme"})
        assert a is not None and (a.value, a.rule) == ("Yes", "us_person"), (label, a)
        assert _answer(label, visa, **yes_no) == "No", label
    # (nor the U.S. person's rule, where a label leaves the others out)
    assert (_answer("Do you hold U.S. citizenship (not lawful permanent residence or a green card)?", pr,
                    **yes_no) != "Yes")


def test_the_us_person_question_is_held_to_the_same_short_list():
    """The eighth review of Oct 18: the U.S. person's rule answered its whole label, whatever was around the question,
    on main too: a citizen-only question that names permanent residents to rule them out took a permanent resident's
    Yes, and the question with a clearance, a move or on-site work beside it a Yes for them all. Now the U.S. person's
    question is answered only as "Are you a U.S. person" or the citizen question with the others named after it, with
    nothing beside it but what the citizen question allows; the rest are left."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, us_person=True,
               citizenship="Mexico")
    visa = _work(authorized_to_work=True, requires_sponsorship=True, us_citizen=False, us_person=False,
                 citizenship="India")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Are you a U.S. citizen (permanent residents do not qualify)?",
                  "Are you a US citizen (green card holders are not eligible)?",
                  "Are you a US citizen (permanent residents should answer No)?",
                  "Are you a US citizen (permanent residents: answer No)?",
                  "Are you a US citizen (as opposed to a permanent resident)?",
                  "Are you a US citizen (rather than a green card holder)?",
                  "Are you a US citizen, since permanent residents cannot obtain this clearance?",
                  "Are you a US citizen, permanent residents excluded?",
                  "Are you a US citizen (refugees and asylees do not qualify)?",
                  "Are you a U.S. citizen or permanent resident and able to obtain a security clearance?",
                  "Are you a US citizen or permanent resident, and do you hold an active clearance?",
                  "Are you a US citizen or permanent resident? Do you hold an active clearance?",
                  "Are you a US citizen or permanent resident? Select Yes only if you also hold a TS/SCI.",
                  "Are you a US citizen or permanent resident? (Note: answer Yes only if you also hold a clearance)",
                  "Are you a US citizen or green card holder willing to relocate to Phoenix?",
                  "Are you a US citizen or permanent resident who can work on-site in Chandler?",
                  "Do you hold an active clearance? Are you a US citizen or permanent resident?",
                  "Are you legally authorized to work in the US (are you a US citizen or permanent resident)?",
                  "Are you a U.S. person? Do you hold an active clearance?"):
        for p in (citizen, pr, visa):
            assert _answer(label, p, **yes_no) is None, (label, p.get("work_authorization.citizenship"))
    for label in ("Are you a U.S. citizen or permanent resident?", "Are you a U.S. citizen or green card holder?",
                  "Are you a US citizen, national, lawful permanent resident, refugee or asylee?",
                  "Are you a U.S. citizen or lawful permanent resident (green card holder)?",
                  "Are you a U.S. citizen, U.S. national, or lawful permanent resident? (Yes/No)",
                  "Export Control: Are you a U.S. citizen or permanent resident?",
                  "Are you a U.S. person?", "Are you a U.S. person as defined by the International Traffic in Arms "
                  "Regulations (ITAR)?",
                  "Are you a U.S. Person (U.S. citizen, U.S. permanent resident, refugee, asylee, or protected "
                  "individual as defined by 8 U.S.C. 1324b(a)(3))?",
                  "Are you a US citizen, permanent resident, or protected individual as defined by 8 U.S.C. "
                  "1324b(a)(3)? Note: this position is not eligible for visa sponsorship."):
        a = resolve_field(f(label, **yes_no), pr, {"company": "Acme"})
        assert a is not None and (a.value, a.rule) == ("Yes", "us_person"), (label, a)
        assert [_answer(label, p, **yes_no) for p in (citizen, visa)] == ["Yes", "No"], label


def test_a_us_person_wording_the_check_doesnt_know_is_left_and_a_list_without_residents_is_no_residents_yes():
    """The ninth review of Oct 18: the U.S. person's rule still answered, with nothing checked around it, any wording
    the check didn't know ("Are you currently a ...", a bare label, "U.S. person" in quotes, "Is the candidate ..."),
    and a list of the persons that leaves out the permanent residents took a permanent resident's Yes from their
    us_person. Now the rule answers only a wording the check knows, held to its short list; a list without the
    permanent residents gets a citizen's Yes and a non-U.S. person's No, and leaves the permanent resident's. The common
    wordings it knows now ("currently", bare, quoted, "(Green Card)", "of the United States", "under ITAR/EAR", a
    definition with its citation or its list) keep their answer."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, us_person=True,
               citizenship="Mexico")
    visa = _work(authorized_to_work=True, requires_sponsorship=True, us_citizen=False, us_person=False,
                 citizenship="India")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Are you currently a US citizen or permanent resident and able to obtain a security clearance?",
                  "Are you currently a U.S. citizen or permanent resident? Do you hold an active clearance?",
                  "U.S. citizen or permanent resident? Do you hold an active clearance?",
                  "US citizen or green card holder willing to relocate to Phoenix",
                  "Do you qualify as a U.S. person under ITAR, and can you obtain a security clearance?",
                  "Are you a 'U.S. person'? Do you hold an active clearance?",
                  'Are you a "U.S. Person" and able to work on-site in Chandler?',
                  "Is the candidate a U.S. citizen or permanent resident with an active Secret clearance?",
                  "Are you presently a US citizen (green card holders are not eligible)?",
                  "Citizen or permanent resident of the US (permanent residents do not qualify for this role)?"):
        for p in (citizen, pr, visa):
            assert _answer(label, p, **yes_no) is None, (label, p.get("work_authorization.citizenship"))
    for label in ("Are you a U.S. citizen or refugee/asylee?", "Are you a US citizen, national, refugee or asylee?",
                  "Are you a US citizen or person granted asylum?"):
        assert [_answer(label, p, **yes_no) for p in (citizen, pr, visa)] == ["Yes", None, "No"], label
    for label in ("Are you a US Citizen or Permanent Resident (Green Card)?",
                  "Are you a US citizen or a lawful permanent resident of the United States?",
                  "Are you currently a U.S. citizen or permanent resident of the US?",
                  "Are you a US citizen or permanent resident alien?",
                  "Are you a U.S. Person under ITAR/EAR?",
                  "Are you a U.S. Person as defined by ITAR (22 CFR 120.15)?",
                  "Are you a U.S. person as defined by ITAR (citizen, permanent resident, protected individual)?",
                  "Are you a U.S. Person as defined by U.S. export control laws?",
                  "Are you a U.S. person, as defined by the Export Administration Regulations (EAR) and ITAR?",
                  "Are you a 'U.S. person'?", "U.S. Citizen or Permanent Resident",
                  "3) US citizen / permanent resident", "Export Control: U.S. Person"):
        a = resolve_field(f(label, **yes_no), pr, {"company": "Acme"})
        assert a is not None and (a.value, a.rule) == ("Yes", "us_person"), (label, a)
        assert [_answer(label, p, **yes_no) for p in (citizen, visa)] == ["Yes", "No"], label
    assert [_answer("Are you currently a U.S. citizen?", p, **yes_no) for p in (citizen, pr)] == ["Yes", "No"]


def test_a_bare_citizen_question_and_a_us_person_heading_are_read_as_the_question_they_head():
    """The tenth review of Oct 18 (on main too): a bare "U.S. Citizen?" after "Work Authorization:" took the work
    authorization rule's Yes, claiming citizenship for a permanent resident or a visa holder; and a "U.S. Person"
    heading made a list without the permanent residents look like one that names them, so they got Yes. Now the bare
    citizen question is the citizen rules' (left where they can't read it), and the persons named are read from the
    question on. Its persons or its definition after its "?", and "This position requires access to export-controlled
    information.", say no more."""
    pr = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=False, us_person=True,
               citizenship="Mexico")
    visa = _work(authorized_to_work=True, requires_sponsorship=True, us_citizen=False, us_person=False,
                 citizenship="India")
    citizen = _work(authorized_to_work=True, requires_sponsorship=False, us_citizen=True, us_person=True,
                    citizenship="United States")
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Work Authorization: U.S. Citizen?", "Work Authorization: US Citizen (Y/N)",
                  "Work Authorization Status: U.S. Citizen?", "Employment Eligibility - US Citizen?",
                  "Authorized to work in the US: U.S. citizen?", "Legally authorized to work? U.S. citizen?"):
        for p in (pr, visa):
            assert _answer(label, p, **yes_no) != "Yes", label
    for label in ("U.S. Person: Are you a U.S. citizen, refugee or asylee?",
                  "U.S. Person Status - Are you a US citizen or refugee?"):
        assert [_answer(label, p, **yes_no) for p in (citizen, pr, visa)] == ["Yes", None, "No"], label
    for label in ("Are you a U.S. Person? (U.S. citizen, lawful permanent resident, refugee or asylee)",
                  "Are you a U.S. citizen or permanent resident? (Green card holder)",
                  "Are you a U.S. person? This position requires access to export-controlled technology.",
                  "Are you a U.S. person as that term is defined in the EAR?"):
        a = resolve_field(f(label, **yes_no), pr, {"company": "Acme"})
        assert a is not None and (a.value, a.rule) == ("Yes", "us_person"), (label, a)
        assert [_answer(label, p, **yes_no) for p in (citizen, visa)] == ["Yes", "No"], label
    assert _answer("Are you a US citizen? This position requires access to export-controlled information.", pr,
                   **yes_no) == "No"
    assert _answer("Are you a US citizen? (Green card holders: answer Yes)", pr, **yes_no) is None
    # (the eleventh review: "Authorized to work in the US" is a question, no heading: a visa holder's No would deny it)
    for label in ("Legally authorized to work in the US: Are you a US citizen?",
                  "Legally authorized to work in the US - U.S. citizen or permanent resident"):
        assert _answer(label, visa, **yes_no) != "No", label


def test_a_sponsorship_question_about_a_partner_company_is_still_the_persons():
    """"...for a partner company" names a company, not the person's partner: the person's own need, the profile's
    answer. Their partner's need stays left."""
    p = _work(authorized_to_work=True, requires_sponsorship=False)
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    assert _answer("Will you require visa sponsorship to work for a partner company?", p, **yes_no) == "No"
    assert _answer("Will you require visa sponsorship to work for a partner organization of Acme?", p, **yes_no) == "No"
    assert _answer("Will you require visa sponsorship for your partner?", p, **yes_no) is None


def test_a_signature_box_is_refused_only_for_a_signing_date_or_a_date_to_enter():
    """A box named for the signing date ("Employment Application Signature Date", "Applicant Signature Date (please use
    MM/DD/YYYY format)") gets today, never the name; one that asks for a date to be entered ("Enter the date beside your
    electronic signature"), or the signature and the date in one ("Signature and Date"), is left. One that only
    mentions a date keeps the name: "...type your full name, the date is recorded automatically", "...enter your full
    name, not the date", "...no date needed", "...type your name to sign and date this application"."""
    from datetime import date

    today, p = date.today().strftime("%m/%d/%Y"), prof()
    for label in ("Employment Application Signature Date", "Certification Signature Date",
                  "E-Signature Date of employment agreement", "Applicant Signature Date (please use MM/DD/YYYY format)",
                  "Candidate Signature Date (please enter as MM/DD/YYYY)",
                  "Employee Signature Date - format MM/DD/YYYY, e.g. 10/18/2026",
                  "Electronic Signature Date (Employment Application)"):
        a = resolve_field(f(label), p)
        assert a is not None and (a.value, a.rule) == (today, "signed_date"), (label, a)
    for label in ("Electronic Signature (type your full name, the date is recorded automatically)",
                  "Signature (enter your full name, not the date)",
                  "Electronic Signature (enter the full name you use, no date needed)",
                  "Electronic Signature - type your name to sign and date this application"):
        a = resolve_field(f(label), p)
        assert a is not None and (a.value, a.rule) == ("Sam Rivera", "signature"), (label, a)
    for label in ("Signature and Date", "Applicant Signature & Date",
                  "Enter the date beside your electronic signature (MM/DD/YYYY)"):
        assert resolve_field(f(label), p) is None, label


def test_a_short_signing_date_label_that_names_a_form_still_gets_today():
    """Only a long label (over 45 characters) is read for another day signed: a short one that names the form it signs
    ("Applicant Certification - Date Signed", "Date Signed (Applicant Agreement)", "Signature Date (Employment
    Application)", "Today's Date (Employment History)") gets today, as before."""
    from datetime import date

    today, p = date.today().strftime("%m/%d/%Y"), prof()
    for label in ("Applicant Certification - Date Signed", "Date Signed (Applicant Agreement)",
                  "Signature Date (Employment Application)", "Today's Date (Employment History)"):
        a = resolve_field(f(label), p)
        assert a is not None and (a.value, a.rule) == (today, "signed_date"), (label, a)


def test_a_sponsorship_question_the_person_requires_is_theirs_whatever_the_employer_provides():
    """"Will you require the company to provide visa sponsorship?" asks the person's need, though it names the
    employer providing it, as does "Will you require sponsorship? Sponsorship is available for this role.": the
    profile's answer. Only a question about the employer's policy with no need of the person's in it ("Is sponsorship
    available for candidates who need it?", "Does Acme offer sponsorship?") is left."""
    p = _work(authorized_to_work=True, requires_sponsorship=False)
    yes_no = {"kind": "radio_group", "options": ["Yes", "No"]}
    for label in ("Will you now or in the future require Acme to provide sponsorship for an employment visa?",
                  "Will you require the company to provide visa sponsorship?",
                  "Will you now or in the future require us to provide immigration sponsorship?",
                  "Will you need the employer to offer visa sponsorship now or in the future?",
                  "Will you need sponsorship provided by the company now or in the future?",
                  "Will you require sponsorship? Sponsorship is available for this role.",
                  "Will you require sponsorship? We offer sponsorship for qualified candidates."):
        assert _answer(label, p, **yes_no) == "No", label
    for label in ("Is sponsorship available for candidates who need it?", "Does Acme offer sponsorship?"):
        assert _answer(label, p, **yes_no) is None, label
        assert _answer(label, p) is None, label


def test_an_is_sponsorship_text_area_that_asks_for_an_explanation_is_left():
    """"Is sponsorship required for you to work in the US?" in a text area takes the profile's No; one that asks for an
    explanation or a description ("If yes, please explain.", "Please describe your current immigration status.",
    "Explain.") is the person's to write."""
    p = _work(authorized_to_work=True, requires_sponsorship=False)
    assert _answer("Is sponsorship required for you to work in the US?", p, kind="textarea") == "No"
    for label in ("Is sponsorship required? If yes, please explain.",
                  "Is sponsorship required? Please describe your current immigration status.",
                  "Is sponsorship required? Explain."):
        assert _answer(label, p, kind="textarea") is None, label


# The weekly mutation pass's first sample (scripts/mutation_pass.py, Oct 18): each test below holds a decision a changed
# rule got past every other test


def test_a_degree_finds_its_field_in_a_list_that_names_no_degree_level():
    """A degree among choices that name no level at all (a list of fields of study) is still matched by its words:
    the degree step narrows to the same level only where some choice names one."""
    assert choose_option("BS Electrical Engineering", ["Electrical Engineering", "Mechanical Engineering"]) == \
        "Electrical Engineering"


def test_the_persons_answers_skip_a_stray_line_an_empty_answer_and_a_missing_pattern():
    """answers.yaml's entries that say nothing (a line that isn't an entry, an answer left empty or None, an answer
    with no pattern) are passed over, and the entry after them that answers is taken."""
    from job_apply.autofill import _answer_bank

    p = Profile({"answers": ["a stray line", {"match": "widget", "answer": None}, {"match": "widget", "answer": ""},
                            {"answer": "No"}, {"match": "", "answer": "No"}, {"match": "widget", "answer": "Yes"}]})
    a = _answer_bank(p, "Have you worked with widgets?")
    assert a is not None and (a.value, a.rule) == ("Yes", "answers[widget]")


def test_an_answers_pattern_that_is_no_regex_is_matched_as_plain_words():
    """A pattern the person wrote that isn't a valid regex ("C++") is read as the words themselves, in any case: it
    answers a label that holds them, and only such a label."""
    from job_apply.autofill import _answer_bank

    p = Profile({"answers": [{"match": "C++", "answer": "Yes"}]})
    a = _answer_bank(p, "Do you know c++ well?")
    assert a is not None and (a.value, a.rule) == ("Yes", "answers[C++]")
    assert _answer_bank(p, "Do you know Java?") is None


# --- NNSS's Oracle questions (live check, run 148): what the profile says is answered, the rest is the person's -----

NNSS = {"company": "Mission Support and Test Services (NNSS)"}
YES_NO = ["Yes", "No"]
NNSS_PROOF = ("2. Proof of U.S. Citizenship is a requirement for this position. Can you provide proof of U.S. "
              "Citizenship?")
NNSS_WORKED_FOR = ("7. Have you ever worked for the NNSS, NNSA, Mission Support and Test Services, LLC or its parent "
                   "companies or predecessors (e.g. Honeywell International Inc., Jacobs Engineering Group Inc., "
                   "Amentum, HII Nuclear, Stoller Newport News Nuclear Inc., SOC, or National Security Technologies, "
                   "LLC, or any of their affiliates or subsidiaries)?")
NNSS_ORGS = ["Amentum", "HII Nuclear", "Honeywell International, Inc.", "Jacobs Engineering Group Inc.", "MSTS",
             "National Security Technologies, LLC", "NNSA", "NNSS", "SOC", "Stoller Newport News Nuclear Inc.", "N/A"]
NNSS_SERVICE = ("4. Have you ever worked for the U.S. Government executive, judicial, or legislative "
                "agencies/branches or served in the U.S. Military in any capacity (including as Special Government "
                "Employee, an Intergovernmental Personnel Act employ, or similar capacity)?")
NNSS_CLEARANCE = "9. Do you currently have a Security Clearance granted by the U.S. Government?"


def _nnss(label, data, kind="radio_group", options=YES_NO, job=NNSS, **kw):
    a = resolve_field(f(label, kind, options=options, **kw), Profile(data), job)
    return a and a.value


@pytest.mark.parametrize("label", [
    NNSS_PROOF,
    "Can you provide proof of U.S. citizenship?",
    "If hired, can you furnish documentation of United States citizenship?",
    "Are you able to provide proof of US citizenship upon hire?",
    "Citizenship: Can you present evidence of U.S. citizenship? (Yes/No)",
    "U.S. citizenship is required. Can you provide proof of U.S. citizenship?*",
])
def test_proof_of_us_citizenship_is_the_profiles_citizenship(label):
    """NNSS's question 2 (run 148) was asked of the person, though the profile says whether they're a U.S. citizen:
    a citizen can give proof of it (Yes), one who isn't can't (No); where the profile doesn't say, the person's (the
    owner's call, Oct 21: what the profile supports is answered)."""
    assert _nnss(label, {"work_authorization": {"us_citizen": True}}) == "Yes"
    assert _nnss(label, {"work_authorization": {"citizenship": "United States"}}) == "Yes"
    assert _nnss(label, {"work_authorization": {"us_citizen": False}}) == "No"
    assert _nnss(label, {"work_authorization": {"citizenship": "Mexico"}}) is None  # (a U.S. one besides: unsaid)
    assert _nnss(label, {}) is None


def test_proof_of_citizenship_is_answered_only_as_asked():
    """A citizenship the question doesn't name is a citizen's Yes only (another country's can be proved too); a
    question that asks more beside it, or about another country, is the person's; the I-9's question with
    citizenship in it is still the I-9's; a list whose choices are unseen is the person's."""
    citizen = {"work_authorization": {"us_citizen": True, "authorized_to_work": True}}
    other = {"work_authorization": {"us_citizen": False, "authorized_to_work": True}}
    assert _nnss("Can you provide proof of citizenship?", citizen) == "Yes"
    assert _nnss("Can you provide proof of citizenship?", other) is None
    for label in ("Can you provide proof of U.S. citizenship? If not, please explain why.",
                  "Do you hold a clearance? Can you provide proof of U.S. citizenship?",
                  "Can you provide proof of U.S. citizenship and pass a background check?",
                  "Select Yes only if you also hold a passport. Can you provide proof of U.S. citizenship?",
                  "Can you provide proof of Canadian citizenship?"):
        assert _nnss(label, citizen) is None, label
        assert _nnss(label, other) is None, label
    i9 = "Can you provide proof of U.S. citizenship or eligibility to work in the United States?"
    assert _nnss(i9, other) == "Yes"  # (work_proof's, as before)
    assert _nnss(NNSS_PROOF, citizen, kind="combobox", options=None) is None


@pytest.mark.parametrize("held, said", [
    ("Secret (active)", "Yes"), ("TS/SCI", "Yes"), ("Top Secret", "Yes"), ("DOE Q (active)", "Yes"), (True, "Yes"),
    ("None", "No"), ("No", "No"), ("N/A", "No"), (False, "No"), ("None, but eligible to obtain one", "No"),
    (None, None), ("Secret (expired 2021)", None), ("Secret, inactive", None), ("Public Trust", None),
    ("Eligible for a Secret clearance", None), ("Interim Secret", None),
])
def test_a_clearance_held_now_is_the_profiles(held, said):
    """NNSS's question 9 (run 148): "Do you currently have a Security Clearance granted by the U.S. Government?" from
    the profile's security_clearance: Yes for a clearance held now, No for none, the person's where it may not be held
    now or isn't a clearance."""
    data = {"work_authorization": {"security_clearance": held}}
    assert _nnss(NNSS_CLEARANCE, data) == said
    assert _nnss("Do you have a current U.S. government security clearance?", data) == said
    assert _nnss(NNSS_CLEARANCE, data, kind="combobox", options=None) is None  # (its choices unseen)


def test_an_active_clearance_is_one_the_profile_says_is_active():
    """"Do you hold an active security clearance?": a level alone may be one held but inactive, so the person's."""
    label = "Do you hold an active security clearance?"
    assert _nnss(label, {"work_authorization": {"security_clearance": "Secret (active)"}}) == "Yes"
    assert _nnss(label, {"work_authorization": {"security_clearance": "Secret"}}) is None
    assert _nnss(label, {"work_authorization": {"security_clearance": "None"}}) == "No"


LEVELS_ASKED = ("Do you hold a Top Secret clearance?", "Do you have an active Secret clearance or higher?",
                "Do you currently hold an active TS/SCI clearance?", "Do you have a DOE Q clearance?",
                "Do you hold an active Secret security clearance?", "Do you have an active DOE L or Q clearance?",
                "Do you hold a security clearance at the Secret level or higher?")


@pytest.mark.parametrize("held, said", [
    # (what each of LEVELS_ASKED is answered, in its order)
    (True, (None,) * 7),
    ("Yes", (None,) * 7),
    (False, ("No",) * 7),
    ("None", ("No",) * 7),
    ("Top Secret (active)", ("Yes", "Yes", None, None, "Yes", None, "Yes")),
    ("TS/SCI (active)", ("Yes", "Yes", "Yes", None, "Yes", None, "Yes")),
    ("Secret (active)", (None, "Yes", None, None, "Yes", None, "Yes")),
    ("Confidential (active)", (None,) * 7),
    ("DOE Q (active)", (None, None, None, "Yes", None, "Yes", None)),
    ("DOE L (active)", (None, None, None, None, None, "Yes", None)),
    ("Top Secret", ("Yes", None, None, None, None, None, "Yes")),  # (an "active" one asked: not said)
    ("Top Secret (expired)", (None,) * 7),
])
def test_a_clearance_question_naming_a_level_is_answered_only_where_the_profile_meets_it(held, said):
    """"Do you hold a Top Secret clearance?", "...an active Secret clearance or higher?", "...TS/SCI?": a bare true
    doesn't say which, so it's the person's (it answered "Do you hold an active Secret security clearance?" Yes). Yes
    only for a level the profile names that meets it (Top Secret meets Secret, which meets Confidential; Q meets L;
    TS/SCI needs SCI named; never across DoD and DOE), held now; No where the profile says none."""
    data = {"work_authorization": {"security_clearance": held}}
    assert tuple(_nnss(label, data) for label in LEVELS_ASKED) == said


def test_a_level_named_in_a_box_is_never_a_bare_trues_yes():
    """The same in a text box, which the clearance's own words go in: a bare true's "Yes" never, a none's "No" and a
    level written out as written."""
    label = "Do you hold an active Top Secret security clearance?"
    assert _nnss(label, {"work_authorization": {"security_clearance": True}}, kind="text", options=None) is None
    assert _nnss(label, {"work_authorization": {"security_clearance": False}}, kind="text", options=None) == "No"
    assert _nnss(label, {"work_authorization": {"security_clearance": "Top Secret (active)"}}, kind="text",
                 options=None) == "Top Secret (active)"
    levels = ["None", "Confidential", "Secret", "Top Secret"]
    assert _nnss("Security clearance level", {"work_authorization": {"security_clearance": "Secret (active)"}},
                 kind="select", options=levels) == "Secret"


@pytest.mark.parametrize("label", [
    "10. Have you ever been denied a Security Clearance?",
    "11. Are you able to obtain and maintain a security clearance?",
    "Have you ever held a security clearance?",
    "Are you willing to obtain a security clearance?",
    "Has your security clearance ever been revoked or suspended?",
    "Are you eligible for a security clearance?",
])
@pytest.mark.parametrize("held", [True, False, "No", "None", "Secret (active)"])
def test_other_clearance_questions_are_the_persons(label, held):
    """NNSS's questions 10 and 11 (run 148), and others about a clearance that the one held now doesn't answer: a
    profile's true answered "Have you ever been denied a Security Clearance?" Yes, and its false "Are you able to
    obtain and maintain a security clearance?" No. The person's."""
    assert _nnss(label, {"work_authorization": {"security_clearance": held}}) is None


@pytest.mark.parametrize("label", [
    "Have you ever worked for any of the following companies?",
    "Have you ever been employed by any of these organizations?",
    "Have you ever worked for Honeywell International, Jacobs Engineering Group or Bechtel National?",
])
def test_worked_for_one_of_the_organizations_listed_is_the_profiles(label):
    """"Have you ever worked for any of these?" among them and one choice that says none (NNSS's question 7, run 148,
    in other words): the one an employer of theirs surely is; the person's where one of theirs may be one ("Honeywell
    Aerospace" for Honeywell International, Inc.), where several are, where the profile names no employer, and where
    none of theirs is one: the none is never the profile's (the fourth review of 0.3.227). In a list that takes several,
    another employer of theirs makes it the person's too (it may be one of them by another name)."""
    options = ["Honeywell International, Inc.", "Jacobs Engineering Group Inc.", "Bechtel National", "N/A"]

    def worked(*companies, kind="combobox", **kw):
        data = {"work_history": [{"company": c, "start": "2018-01", "end": "2020-01"} for c in companies]}
        return _nnss(label, data, kind=kind, options=options, **kw)

    assert worked("Jacobs Engineering Group") == "Jacobs Engineering Group Inc."
    assert worked("Jacobs Engineering Group", "Intel") == "Jacobs Engineering Group Inc."
    assert worked("Honeywell International Inc.") == "Honeywell International, Inc."
    assert worked("Jacobs Engineering Group", kind="checkbox_group") == ["Jacobs Engineering Group Inc."]
    assert worked("Jacobs Engineering Group", "Intel", kind="checkbox_group") is None
    assert worked("Jacobs Engineering Group", "Intel", multiple=True) is None
    assert worked("Intel", "Example Fab Services") is None
    assert worked("Intel", kind="radio_group") is None
    assert worked("Honeywell Aerospace") is None
    assert worked("Honeywell") is None
    assert worked("Jacobs Engineering Group", "Honeywell International") is None
    assert worked("Mission Support and Test Services") is None  # (may be the employer applied to)
    assert worked() is None
    assert _nnss(label, {"history": {"previous_employers": ["Jacobs Engineering Group"]}}, options=options) == \
        "Jacobs Engineering Group Inc."
    assert _nnss(label, {"experience": {"current_company": "Honeywell Aerospace"}}, options=options) is None


@pytest.mark.parametrize("employer, label, options", [
    # (a city's department, a state's, a university, by a name that isn't the list's)
    ("Phoenix Convention Center", "Have you ever worked for any of the following agencies?",
     ["City of Phoenix", "State of Arizona", "Maricopa County", "None of the above"]),
    ("ADOT", "Have you ever worked for any of the following agencies?",
     ["City of Phoenix", "State of Arizona", "Maricopa County", "None of the above"]),
    ("Phoenix Police Department", "Have you ever worked for any of the following organizations?",
     ["City of Phoenix", "State of Arizona", "Maricopa County", "None of the above"]),
    ("Phoenix VA Health Care System", "Have you ever worked for any of the following agencies?",
     ["Department of Veterans Affairs", "Department of Defense", "None of the above"]),
    # (a DOE site and the company that runs it)
    ("Los Alamos National Laboratory", "Have you ever worked for any of the following companies?",
     ["Triad National Security", "Consolidated Nuclear Security", "None"]),
    ("Sandia National Laboratories", "Have you ever worked for any of the following companies?",
     ["National Technology and Engineering Solutions of Sandia", "Mission Support and Test Services", "None"]),
    # (a short name, a renamed company)
    ("BWX Technologies", "Have you ever worked for any of the following companies?", ["BWXT Pantex", "Acme", "N/A"]),
    ("National Security Technologies", "Have you ever worked for any of the following companies?",
     ["NSTec LLC", "Bechtel Corporation", "N/A"]),
    ("AECOM", "Have you ever worked for any of the following companies?", ["Amentum", "Acme Widgets", "N/A"]),
    ("Intel", "Have you ever worked for any of the following companies?",
     ["Honeywell International", "Jacobs Engineering Group", "N/A"]),
])
def test_the_none_of_a_list_of_organizations_is_never_the_profiles(employer, label, options):
    """The fourth review of 0.3.227: a profile can't prove the person never worked for any of a list of organizations:
    its employer may be one of them by another name (a city's convention center or water service, a VA hospital, a
    national laboratory and the company that runs it, a short name, a renamed company). The none is the person's, in
    any list, and remembered once they give it; one the profile's employer surely is is still picked."""
    for kind in ("combobox", "radio_group", "checkbox_group"):
        assert _nnss(label, {"work_history": [{"company": employer, "start": "2018-01", "end": "2020-01"}]},
                     kind=kind, options=options, job={"company": "Example Fab"}) is None, (employer, kind)
    assert _nnss("Have you ever worked for any of the following companies?", {"work_history": [
        {"company": "BWXT", "start": "2018-01", "end": "2020-01"}]}, options=["BWXT", "Acme Widgets", "N/A"],
        job={"company": "Example Fab"}) == "BWXT"


def test_worked_for_an_organization_its_family_or_examples_is_never_a_none():
    """NNSS's question 7 as the page has it (run 148), a list that takes several, reaches past the names its choices
    give: "its parent companies or predecessors (e.g. ...)", "or any of their affiliates or subsidiaries". Its N/A is
    never the profile's, and another employer of theirs may be one of those by another name (Bechtel Nevada ran the
    site before them; AECOM's government arm is now Amentum): the person's, unless every employer of theirs is one of
    its choices."""
    def worked(label, *companies):
        data = {"work_history": [{"company": c, "start": "2018-01", "end": "2020-01"} for c in companies]}
        return _nnss(label, data, kind="combobox", options=NNSS_ORGS, multiple=True)

    asked = ("7. Have you ever worked for the NNSS, NNSA, Mission Support and Test Services, LLC or its parent "
             "companies or predecessors (e.g. Honeywell International Inc., Jacobs Engineering Group Inc., National "
             "Security Technologies, LLC, etc.)?")
    for label in (NNSS_WORKED_FOR, asked,
                  "Have you ever worked for any of the following companies or their subsidiaries?",
                  "Have you ever worked for any of the following companies, such as Amentum or SOC?"):
        assert worked(label, "Intel") is None, label
        assert worked(label, "Honeywell Aerospace") is None, label
        assert worked(label, "Jacobs Engineering Group", "Intel") is None, label
        assert worked(label, "Jacobs Engineering Group") == "Jacobs Engineering Group Inc.", label
        assert worked(label, "National Security Technologies (NSTec)") is None, label


def test_a_question_about_working_somewhere_that_lists_no_organizations_is_not_answered_as_one():
    """Only a question whose choices are the organizations it names, and a none: shifts, departments or a list
    with Yes and No aren't, and its "N/A" is never given for them."""
    intel = {"work_history": [{"company": "Intel", "start": "2018-01", "end": "2020-01"}]}
    assert _nnss("Have you ever worked any of these shifts?", intel, options=["Day", "Night", "N/A"]) is None
    assert _nnss("Have you ever worked for any of the following departments?", intel,
                 options=["Currently employed in Engineering", "Formerly employed in Operations", "N/A"]) is None
    assert _nnss("Have you ever worked for NNSS?", intel, options=["Yes", "No", "N/A"]) != "N/A"


@pytest.mark.parametrize("label, options", [
    ("Have you worked with any of the following equipment?", ["Lathe", "Mill", "CNC Router", "None of the above"]),
    ("Have you ever worked with any of the following software?", ["SAP", "Oracle", "Workday", "None"]),
    ("Have you worked with any of these chemicals?", ["Hydrofluoric acid", "Arsine", "Silane", "N/A"]),
    ("Have you worked with any of these programming languages?", ["Python", "C++", "Java", "None"]),
    ("Have you worked with any of the following? (select all that apply)",
     ["Lam etch tools", "KLA metrology", "Applied Materials CVD", "None of the above"]),
    ("Have you worked at any of the following types of facilities?", ["Semiconductor fab", "Cleanroom", "None"]),
    ("Have you ever worked at any of the following locations?", ["Hospital", "Clinic", "Laboratory", "None"]),
    ("Have you ever worked at one of these sites?", ["Chandler", "Hillsboro", "Rio Rancho", "None"]),
    ("Have you ever worked at Chandler, Hillsboro or Rio Rancho?", ["Chandler", "Hillsboro", "Rio Rancho", "None"]),
    ("Have you ever worked with any of the following companies?", ["Boeing", "Raytheon", "None"]),
    ("Have you ever worked with any of the following customers?", ["Boeing", "Raytheon", "None"]),
])
def test_a_list_of_tools_places_or_customers_is_never_answered_none(label, options):
    """The review of 0.3.227: the list rule took "Have you worked with any of the following equipment?" for a list of
    employers and answered "None of the above" (and "None" for chemicals, software, sites, a supplier's customers).
    Only who the person worked for: organizations by a word that says so ("...the following companies?"), or named
    where it asks who they worked for; never "worked with", nor places "worked at"."""
    for kind in ("radio_group", "checkbox_group"):
        a = resolve_field(f(label, kind, options=options), _worked_at("Intel"), {"company": "Example Fab"})
        assert a is None or a.rule != "work_history", (label, kind, a)


SERVICE_BOTH = (NNSS_SERVICE, NNSS_SERVICE + " If yes, you must complete the sub-questions below",
                "Have you ever served in the U.S. military or worked for the federal government?",
                "Did you serve in the U.S. military or work for a federal agency?")


@pytest.mark.parametrize("label", SERVICE_BOTH)
def test_us_federal_or_military_service_is_the_profiles_both_ways(label):
    """NNSS's question 4 (run 148; the owner's call, Oct 21: setup asks it): Yes where the profile says the person
    worked for the U.S. federal government or served in the U.S. military, No where it says neither, else the
    person's. The background's "government employee now or in the last 5 years" doesn't say "ever", and a veteran's
    voluntary self-identification is never a screening answer (the coordinator's call on the review of 0.3.227)."""
    def asked(**data):
        return _nnss(label, data)

    assert asked(history={"us_government_employee": False, "us_military": False}) == "No"
    assert asked(history={"us_government_employee": True, "us_military": False}) == "Yes"
    assert asked(history={"us_government_employee": False, "us_military": True}) == "Yes"
    assert asked(history={"us_government_employee": True}) == "Yes"
    assert asked(history={"us_government_employee": False}) is None
    assert asked() is None
    assert asked(background={"government_employee": False, "military": False}) is None
    assert asked(history={"us_government_employee": False}, background={"military": False}) == "No"
    vet = "I identify as one or more of the classifications of protected veteran"
    assert asked(history={"us_government_employee": False}, eeo={"veteran": vet}) is None
    assert asked(history={"us_government_employee": False}, eeo={"veteran": "Veteran"}) is None
    assert asked(history={"us_government_employee": False}, eeo={"veteran": "I am not a protected veteran"}) is None


def test_federal_or_military_service_asked_alone_is_the_profiles_where_it_says_so():
    """"Have you ever been employed by the federal government?", "Are you a current or former federal employee?",
    "Have you served in the U.S. Armed Forces?": from the profile's history, both ways. Federal employment is No only
    where military service was none too (a service member is paid by it), unless the question says civilian; "the
    military" unnamed is No only where the profile says they never served in any armed forces."""
    neither = {"history": {"us_government_employee": False, "us_military": False}}
    federal = {"history": {"us_government_employee": True, "us_military": False}}
    served = {"history": {"us_government_employee": False, "us_military": True}}
    for label in ("Have you ever been employed by the federal government?",
                  "Are you a current or former federal employee?", "Have you ever worked for a federal agency?"):
        assert (_nnss(label, neither), _nnss(label, federal), _nnss(label, served)) == ("No", "Yes", None), label
    civilian = "Have you ever been employed by the federal government as a civilian?"
    assert (_nnss(civilian, neither), _nnss(civilian, federal), _nnss(civilian, served)) == ("No", "Yes", "No")
    for label in ("Have you served in the U.S. Armed Forces?", "Have you ever served in the US military?",
                  "Are you a current or former member of the U.S. armed forces?"):
        assert (_nnss(label, neither), _nnss(label, federal), _nnss(label, served)) == ("No", "No", "Yes"), label
    unnamed = "Have you ever served in the military?"
    assert (_nnss(unnamed, neither), _nnss(unnamed, served)) == (None, "Yes")  # (another country's: unsaid)
    assert _nnss(unnamed, {**neither, "background": {"military": False}}) == "No"


@pytest.mark.parametrize("label", [
    "Have you ever worked for a federal, state or local government agency?",
    "Have you ever worked for any governmental entity?",
    "Have you worked for the federal government in the last 5 years?",
    "Are you currently a federal employee?",
    "Has your spouse ever worked for the federal government?",
    "Have you ever worked for a federal contractor?",
    "Have you ever served in the U.S. military? If yes, what was your discharge status?",
    "Have you ever served in the U.S. military? If you served only in a state guard, select No.",
])
def test_other_government_or_military_questions_are_never_answered_from_us_service(label):
    """A question beside the U.S. service the profile's history says, or about more than it: a state or local
    government, a time it covers, someone else, a contractor, a detail of the service. Never answered from it (a
    federal Yes is no state one, a never is no "not lately"): the background's rules, or the person's."""
    for data in ({"history": {"us_government_employee": True, "us_military": True}},
                 {"history": {"us_government_employee": False, "us_military": False}}):
        a = resolve_field(f(label, "radio_group", options=YES_NO), Profile(data), NNSS)
        assert a is None or a.rule != "us_service", (label, data, a)
    assert _nnss("If yes, please list the agency and dates", {"history": {"us_government_employee": True}},
                 kind="text", options=None) is None


def test_the_gaps_name_the_us_service_left_unanswered():
    """Setup asks the U.S. service (history.us_government_employee, history.us_military) with the background
    questions: the gaps list them until answered, and the doctor says them in words."""
    from job_apply import doctor

    assert "history: us_government_employee, us_military" in Profile({}).profile_gaps()
    assert "history: us_military" in Profile({"history": {"us_government_employee": False}}).profile_gaps()
    answered = Profile({"history": {"us_government_employee": False, "us_military": True}})
    assert not [g for g in answered.profile_gaps() if g.startswith("history")]
    assert "U.S. military" in doctor.GAP_WORDS["history"]


def test_nnss_answers_the_person_gives_are_used_again_at_its_next_job(job_apply_home):
    """The questions the profile can't say (NNSS's 4 to 14 but 9, run 148) are the person's once: the desk remembers
    each answer, and the next NNSS job's same question takes it; one about working for this employer is never given
    to another's."""
    from job_apply import config

    given = {
        NNSS_SERVICE: ("radio_group", YES_NO, "No"),
        "5. Have you ever been suspended or debarred (or proposed to be suspended or debarred) from doing business "
        "with any governmental entity, including a state, federal, or international entity, or have you ever been "
        "listed on any other Restricted Parties list issued by United States Federal Government? If yes, please "
        "explain.":
            ("textarea", None, "No"),
        "6. Have you ever been found liable for a violation of the Federal Civil False Claims Act (31 U.S.C. Sections "
        "3729-3733) or for the violation of another federal or state civil or criminal statute involving fraud?":
            ("radio_group", YES_NO, "No"),
        NNSS_WORKED_FOR: ("combobox", NNSS_ORGS, "N/A"),
        "8. Do you have any relatives or individuals with whom you have a close personal relationship (such as a "
        "romantic partner) working for MSTS?": ("radio_group", YES_NO, "No"),
        "10. Have you ever been denied a Security Clearance?": ("radio_group", YES_NO, "No"),
        "11. Are you able to obtain and maintain a security clearance?": ("radio_group", YES_NO, "Yes"),
        "11a. If no, please provide an explanation. If not applicable, please write N/A.": ("textarea", None, "N/A"),
        "12. If offered a position, what is your desired Annual or Hourly expected range?":
            ("textarea", None, "$25 to $30 an hour"),
        "14. Can you perform the essential functions of this position as described with or without an "
        "accommodation?": ("radio_group", YES_NO, "Yes"),
    }
    for label, (kind, options, value) in given.items():
        assert _nnss(label, {}, kind=kind, options=options) is None, label  # (the person's, until answered)
        config.save_answer(label, value, NNSS["company"])
    p = config.Profile.load()
    for label, (kind, options, value) in given.items():
        a = resolve_field(f(label, kind, options=options), p, NNSS)
        assert a is not None and a.value == value, (label, a)
    for label in (NNSS_SERVICE, NNSS_WORKED_FOR):
        kind, options, _ = given[label]
        assert resolve_field(f(label, kind, options=options), p, {"company": "Example Fab"}) is None, label


# --- the review of 0.3.227: false answers about the person ---------------------------------------------------------

@pytest.mark.parametrize("held, dod, doe", [
    ("Secret (active), DoD", "Yes", None),
    ("DOE Q (active)", None, "Yes"),
    ("Secret (active)", "Yes", None),
    ("Secret (active) - DHS", None, None),
    (True, None, None),
    (False, "No", "No"),
    ("None", "No", "No"),
    ("Top Secret / Q (active)", None, "Yes"),  # (a Secret is the DoD's only where no agency is named)
    ("Q (active), DoD Secret (active)", "Yes", "Yes"),
    # (the second review of 0.3.227: the DOE named beside a Secret is no DoD clearance)
    ("Secret (active), DOE", None, "Yes"),
    ("DOE Secret (active)", None, "Yes"),
    ("Secret/L (active)", None, "Yes"),
    ("Secret (active), NNSA", None, "Yes"),
    ("Secret (active) - DOE L", None, "Yes"),
    ("L (active) / Secret", None, "Yes"),
    ("Secret (active), USCG", None, None),
    ("Secret (active) - Coast Guard", None, None),
])
def test_a_clearance_question_naming_an_agency_is_answered_only_for_that_agencys(held, dod, doe):
    """A question naming the agency ("...an active DoD clearance?", "...granted by the Department of Energy?"): a bare
    true doesn't say whose, and another agency's clearance is none of its (a Q is the DOE's; a Secret named with no
    agency the DoD's, never the DOE's). The review of 0.3.227: "Secret (active), DoD" was Yes to "active DOE
    clearance?", "DOE Q (active)" Yes to "DoD security clearance?"."""
    data = {"work_authorization": {"security_clearance": held}}
    for label in ("Do you currently hold an active DoD clearance?", "Do you hold a DoD security clearance?",
                  "Do you currently hold a security clearance issued by DoD?"):
        assert _nnss(label, data) == dod, (label, held)
    for label in ("Do you currently hold an active DOE clearance?", "Do you hold a DOE security clearance?",
                  "Do you currently have a security clearance granted by the Department of Energy?"):
        assert _nnss(label, data) == doe, (label, held)


@pytest.mark.parametrize("held", [
    "Secret (2012-2016)", "Secret 2016", "Secret (DoD), last investigated 2015", "Canadian Secret (active)",
    "NATO Secret (active)", "Secret (UK)", "Secret (active) - foreign",
])
def test_a_clearance_with_dates_only_or_another_countrys_is_no_us_clearance_held_now(held):
    """The review of 0.3.227: a clearance written with its dates and nothing saying it's active or current may be one
    held then, and another country's or NATO's is no U.S. Government one: never Yes to "Do you currently have a
    Security Clearance granted by the U.S. Government?", nor a level held in a list of levels."""
    data = {"work_authorization": {"security_clearance": held}}
    assert _nnss(NNSS_CLEARANCE, data) is None
    assert _nnss("What level of security clearance do you hold?", data, kind="select",
                 options=["None", "Confidential", "Secret", "Top Secret"]) is None
    assert _nnss(NNSS_CLEARANCE, {"work_authorization": {"security_clearance": "Secret (active since 2016)"}}) == "Yes"


def test_two_levels_asked_together_are_both_needed():
    """"Do you hold an active Secret and Q clearance?": both, never the Secret alone (the review of 0.3.227)."""
    label = "Do you hold an active Secret and Q clearance?"
    assert _nnss(label, {"work_authorization": {"security_clearance": "Secret (active)"}}) is None
    assert _nnss(label, {"work_authorization": {"security_clearance": "Top Secret / Q (active)"}}) == "Yes"
    assert _nnss("Do you hold an active Secret or Q clearance?",
                 {"work_authorization": {"security_clearance": "Secret (active)"}}) == "Yes"


@pytest.mark.parametrize("held", ["Secret (expired 2021)", "Top Secret (inactive)", "Eligible for Secret",
                                  "Interim Secret"])
def test_a_clearance_not_held_now_is_no_level_held(held):
    """The review of 0.3.227: a clearance the profile says isn't held now was given as the level held ("Secret" for
    "What level of security clearance do you hold?", "Active security clearance"): the person's."""
    data = {"work_authorization": {"security_clearance": held}}
    levels = ["None", "Confidential", "Secret", "Top Secret", "TS/SCI"]
    for label in ("What level of security clearance do you hold?", "Active security clearance",
                  "Current security clearance"):
        assert _nnss(label, data, kind="select", options=levels) is None, label
    assert _nnss("What level of security clearance do you hold?",
                 {"work_authorization": {"security_clearance": "Secret (active)"}}, kind="select",
                 options=levels) == "Secret"


@pytest.mark.parametrize("label", [
    "Do you currently hold a security clearance that has been active within the last 2 years?",
    "Do you hold a security clearance that has been adjudicated within the last 5 years?",
    "Do you hold a security clearance with an investigation completed in the past 5 years?",
])
def test_a_clearance_question_about_its_time_is_the_persons(label):
    """A clearance held now says nothing of when it was investigated or active: the person's."""
    for held in (True, False, "Secret (active)"):
        assert _nnss(label, {"work_authorization": {"security_clearance": held}}) is None, (label, held)


def test_proof_of_citizenship_and_work_authorization_is_a_citizens_yes_only():
    """The review of 0.3.227: the I-9's rule took "Can you provide proof of U.S. citizenship and authorization to
    work?" and answered a permanent resident's authorization: Yes. With citizenship asked beside it, a citizen's Yes
    only; "...citizenship or eligibility to work" stays the authorization's."""
    resident = {"work_authorization": {"us_citizen": False, "us_person": True, "authorized_to_work": True}}
    citizen = {"work_authorization": {"us_citizen": True, "authorized_to_work": True}}
    for label in ("Can you provide proof of U.S. citizenship and authorization to work?",
                  "Can you provide proof of U.S. citizenship and work authorization?"):
        assert _nnss(label, resident) is None, label
        assert _nnss(label, citizen) == "Yes", label
    assert _nnss("Can you provide proof of U.S. citizenship or eligibility to work in the United States?",
                 resident) == "Yes"


def test_worked_for_the_military_is_not_served_in_it():
    """The review of 0.3.227: "worked for the U.S. military" was read as served in it, so an Army civilian was No and a
    service member Yes to "...as a civilian?"; "worked with" a federal agency (a contractor) as employed by it. Worked
    for the military: Yes where they served, No where they neither served nor worked for the federal government, else
    the person's; as a civilian: No only where they never worked for the federal government. "Worked with" is the
    person's."""
    civilian = {"history": {"us_government_employee": True, "us_military": False}}
    neither = {"history": {"us_government_employee": False, "us_military": False}}
    served = {"history": {"us_government_employee": False, "us_military": True}}
    label = "Have you ever worked for the U.S. military?"
    assert (_nnss(label, civilian), _nnss(label, neither), _nnss(label, served)) == (None, "No", "Yes")
    label = "Have you ever worked for the U.S. military as a civilian?"
    assert (_nnss(label, civilian), _nnss(label, neither), _nnss(label, served)) == (None, "No", "No")
    for label in ("Have you ever worked with a federal agency?", "Have you ever worked with the federal government?",
                  "Have you ever worked with the U.S. military?"):
        for data in (civilian, neither, served):
            assert _nnss(label, data) is None, (label, data)


@pytest.mark.parametrize("label", [
    "Have you ever worked for the federal government, including any position funded by a federal grant?",
    "Have you ever worked for the U.S. Government in any capacity, including at a national laboratory or FFRDC?",
    "Have you ever worked for the U.S. Government including as a volunteer?",
    "Have you ever served in the U.S. military, including ROTC or a service academy?",
    "Have you ever worked for the federal government or served in the U.S. military, including the Public Health "
    "Service?",
    "Have you ever served in the U.S. military? If yes, explain unless you served only in the Reserves",
])
def test_us_service_including_what_the_profile_doesnt_count_is_the_persons(label):
    """The review of 0.3.227: "including ..." took anything, so a national laboratory, a grant, a volunteer, ROTC or
    the Public Health Service was a No from a profile that never counted them; and a condition after the question
    ("If yes, explain unless you served only in the Reserves") was taken for a request for details."""
    for data in ({"history": {"us_government_employee": False, "us_military": False}},
                 {"history": {"us_government_employee": True, "us_military": True}}):
        a = resolve_field(f(label, "radio_group", options=YES_NO), Profile(data), NNSS)
        assert a is None or a.rule != "us_service", (label, data, a)
    asked = "Have you ever served in the U.S. military? If yes, please list branch and dates of service."
    assert _nnss(asked, {"history": {"us_military": True}}) == "Yes"


def test_the_background_never_contradicts_the_us_service_the_history_says():
    """The review of 0.3.227: a profile whose history says the person worked for the federal government (or served),
    and whose background says they weren't a government employee in the last 5 years (or never served), got the
    background's No to "Were you ever employed by the federal government?" or "Do you have prior military service?".
    The history answers what it says ("ever"), and the background's No is never given against it."""
    data = {"history": {"us_government_employee": True, "us_military": True},
            "background": {"government_employee": False, "military": False}}
    assert _nnss("Were you ever employed by the federal government?", data) == "Yes"
    assert _nnss("Have you ever worked for the U.S. Government (excluding military service)?", data) == "Yes"
    assert _nnss("Do you have prior military service?", data) == "Yes"
    for label in ("Are you a former federal employee?", "Have you ever been a federal government employee or official?",
                  "Have you ever been employed by any government agency?",
                  "Do you have any prior U.S. military experience?",
                  "Have you ever served on active duty in the U.S. military?"):
        assert _nnss(label, data) is None, label
    # (the background's No still answers where the history doesn't say otherwise)
    never = {"history": {"us_government_employee": False, "us_military": False},
             "background": {"government_employee": False, "military": False}}
    assert _nnss("Are you a former federal employee?", never) == "No"
    assert _nnss("Do you have any prior U.S. military experience?", never) == "No"


# --- the second review of 0.3.227 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("label", [
    "Have you worked for any of the following companies in the past 2 years?",
    "Have you been employed by any of the following companies within the last 12 months?",
    "Have you previously worked for any of the following companies since 2020?",
    "Have you ever worked for any of the following companies as a contractor?",
    "Have you ever worked for any of the following companies on a government contract?",
    "In the last 5 years, have you worked for any of the following companies?",
    "Have you worked for Honeywell International or Jacobs Engineering Group in the last 5 years?",
    "Have you ever been an intern at any of the following companies?",
    "Have you ever been a contractor at any of the following companies?",
])
def test_a_list_question_with_a_time_or_a_role_is_the_persons(label):
    """The second review of 0.3.227: the list rule answered a question with a time or a role in it as if it asked only
    who the person ever worked for: a job at Jacobs in 2010-2012 was picked for "...in the past 2 years?", and a
    staffing agency's contractor was "None of the above" to "...a contractor at any of the following companies?". The
    profile's employers say neither: the person's."""
    options = ["Jacobs Engineering Group Inc.", "Honeywell International, Inc.", "None of the above"]
    for profile in (_worked_at("Jacobs Engineering Group"), _worked_at("Kelly Services")):
        for kind in ("combobox", "checkbox_group"):
            a = resolve_field(f(label, kind, options=options), profile, {"company": "Example Fab"})
            assert a is None, (label, kind, a)
    # (and with nothing but the list around it, as before)
    a = resolve_field(f("Have you ever worked for any of the following companies? (select all that apply)",
                        "checkbox_group", options=options), _worked_at("Jacobs Engineering Group"),
                      {"company": "Example Fab"})
    assert a is not None and a.value == ["Jacobs Engineering Group Inc."]


@pytest.mark.parametrize("label", [
    "Are you a current or former Department of Defense employee (military or civilian)?",
    "Have you ever been employed by the Department of Defense, including military service?",
    "Are you a former DoD employee, including uniformed service members?",
])
def test_a_dod_question_counting_military_service_is_never_a_veterans_no(label):
    """The second review of 0.3.227: the background's "never employed by the DoD" answered No for a veteran where the
    question counts military service in it. The person's, unless the profile says they never served."""
    veteran = {"history": {"us_government_employee": False, "us_military": True},
               "background": {"defense_department": False, "military": True, "government_employee": False}}
    assert _nnss(label, veteran) is None
    never = {"history": {"us_government_employee": False, "us_military": False},
             "background": {"defense_department": False, "military": False}}
    assert _nnss(label, never) == "No"
    # (nor now: one who served may be on active duty, the third review's)
    assert _nnss("Are you currently employed by the Department of Defense?", veteran) is None
    assert _nnss("Are you currently employed by the Department of Defense?", never) == "No"


@pytest.mark.parametrize("label", [
    "Have you ever been employed by the Department of Defense?",
    "Are you a former DoD employee?",
    "Have you previously been employed by the U.S. Department of Defense?",
])
def test_a_veteran_was_employed_by_the_dod(label):
    """The coordinator's follow-up to the second review of 0.3.227: a service member was the DoD's, so a veteran's
    background "never employed by the DoD" is no No to "Have you ever been employed by the Department of Defense?":
    the person's. Someone the profile says never served keeps the background's No."""
    veteran = {"history": {"us_government_employee": False, "us_military": True},
               "background": {"defense_department": False, "military": True, "government_employee": False}}
    never = {"history": {"us_government_employee": False, "us_military": False},
             "background": {"defense_department": False, "military": False}}
    assert (_nnss(label, veteran), _nnss(label, never)) == (None, "No")


def test_were_you_ever_employed_by_the_military_is_the_served_question():
    """"Were you ever employed by the U.S. military?": Yes where the profile says the person served, No where it says
    they neither served nor worked for the federal government, else the person's."""
    label = "Were you ever employed by the U.S. military?"
    assert _nnss(label, {"history": {"us_government_employee": False, "us_military": True}}) == "Yes"
    assert _nnss(label, {"history": {"us_government_employee": False, "us_military": False}}) == "No"
    assert _nnss(label, {"history": {"us_government_employee": True, "us_military": False}}) is None
    assert _nnss(label, {}) is None


@pytest.mark.parametrize("held", ["Secret (active 2012-2016)", "Top Secret, active 2015-2019",
                                  "Secret (active) 2010 to 2014"])
def test_a_clearance_with_the_years_it_ran_is_not_held_now_even_active(held):
    """The second review of 0.3.227: "active" beside the years a clearance ran skipped the date check."""
    data = {"work_authorization": {"security_clearance": held}}
    assert _nnss(NNSS_CLEARANCE, data) is None
    assert _nnss("Do you hold an active DoD security clearance?", data) is None


def test_proof_of_citizenship_or_residency_and_authorization_is_never_the_authorizations():
    """The second review of 0.3.227: "...proof of citizenship or permanent residency and authorization to work?" took
    the "or" for the I-9's, and an H-1B holder's authorization answered Yes. Only where the other choice is the
    authorization itself."""
    h1b = {"work_authorization": {"us_citizen": False, "authorized_to_work": True, "requires_sponsorship": True}}
    resident = {"work_authorization": {"us_citizen": False, "us_person": True, "authorized_to_work": True}}
    citizen = {"work_authorization": {"us_citizen": True, "authorized_to_work": True}}
    label = "Can you provide proof of citizenship or permanent residency and authorization to work?"
    assert (_nnss(label, h1b), _nnss(label, resident), _nnss(label, citizen)) == (None, None, "Yes")
    label = "Can you provide proof of U.S. citizenship or lawful permanent residence and authorization to work?"
    assert (_nnss(label, h1b), _nnss(label, resident)) == (None, None)
    label = "Can you provide proof of U.S. citizenship or eligibility to work in the United States upon hire?"
    assert (_nnss(label, h1b), _nnss(label, resident)) == ("Yes", "Yes")


def test_the_background_still_answers_now_or_lately_beside_federal_history():
    """The second review of 0.3.227: once the history said the person worked for the federal government, the
    background's government rule stood aside for every question, and "Are you currently a state or local government
    employee?" lost its No. It stands aside only for "ever" and "former"."""
    data = {"history": {"us_government_employee": True}, "background": {"government_employee": False}}
    assert _nnss("Are you currently a state or local government employee?", data) == "No"
    assert _nnss("Have you been a government employee in the last 5 years?", data) == "No"
    assert _nnss("Are you a former federal employee?", data) is None


# --- the third review of 0.3.227 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("label", [
    "Are you now or have you ever been employed by the federal government?",
    "Are you currently, or have you ever been, a federal government employee?",
    "Are you currently or have you ever been employed by a government agency?",
    "Are you a former or current government employee?",
    "Have you ever worked for the federal government, and if so, within the past 5 years?",
])
def test_a_now_or_ever_government_question_is_never_the_backgrounds_no_beside_federal_history(label):
    """The third review of 0.3.227: "now or ever" read as "now", and someone the history says worked for the federal
    government long ago got the background's 5-year No. A question that asks about any time stands aside; one about
    now alone keeps the background's answer."""
    data = {"history": {"us_government_employee": True}, "background": {"government_employee": False}}
    a = resolve_field(f(label, "radio_group", options=YES_NO), Profile(data), NNSS)
    assert a is None or a.value != "No", (label, a)


@pytest.mark.parametrize("label", [
    "Are you a former or current DoD employee?",
    "Are you currently or have you ever been employed by the Department of Defense?",
    "Are you now or have you ever been a DoD employee?",
])
def test_a_now_or_ever_dod_question_is_never_a_veterans_no(label):
    """The third review of 0.3.227: a veteran got the background's DoD No where the question asks "now or ever"."""
    veteran = {"history": {"us_government_employee": False, "us_military": True},
               "background": {"defense_department": False, "military": True}}
    never = {"history": {"us_government_employee": False, "us_military": False},
             "background": {"defense_department": False, "military": False}}
    assert (_nnss(label, veteran), _nnss(label, never)) == (None, "No")


def test_a_dod_question_about_a_contractor_too_is_the_persons():
    """"Are you a current DoD employee or contractor?": the background says nothing of a contractor (the third
    review's)."""
    never = {"history": {"us_government_employee": False, "us_military": False},
             "background": {"defense_department": False, "military": False}}
    assert _nnss("Are you a current DoD employee or contractor?", never) is None


# --- the fourth review of 0.3.227 ----------------------------------------------------------------------------------

@pytest.mark.parametrize("label", [
    "Are you currently or have you been employed by the federal government?",
    "Are you currently, or have you in the past been, employed by the federal government?",
    "Do you currently work for, or have you worked for, the federal government?",
    "Are you currently a federal employee or have you been one in the past?",
    "Are you currently or were you once a government employee?",
    "Are you currently a government employee, or have you been one at some point?",
    "Have you been a government employee in the last 5 years or before that?",
    "Are you currently employed by a government agency, or were you in the last 5 years?",
    "Is this your first time working for a government agency, or are you currently a government employee?",
    "Have you been a government employee in the last 10 years?",
    "Have you been a government employee in the past?",
])
def test_beside_federal_history_the_background_answers_only_now_or_five_years(label):
    """The fourth review of 0.3.227: a list of what makes a question about another time kept missing wordings. Beside a
    history that says the person worked for the federal government, the background's "No" (now or in the last 5
    years) answers only a question unmistakably about now or a window of 5 years or less; anything else is the
    person's."""
    data = {"history": {"us_government_employee": True}, "background": {"government_employee": False}}
    a = resolve_field(f(label, "radio_group", options=YES_NO), Profile(data), NNSS)
    assert a is None or a.value != "No", (label, a)
    for label in ("Are you currently a federal employee?", "Have you been a government employee in the last 5 years?",
                  "Are you currently a state or local government employee?",
                  "Have you been a government employee within the past three years?"):
        assert _nnss(label, data) == "No", label


# --- the fifth review of 0.3.227 -----------------------------------------------------------------------------------

@pytest.mark.parametrize("label", [
    "Are you currently a federal employee or a retired federal employee?",
    "Are you currently a federal employee or annuitant?",
    "Are you currently a federal government employee or retiree?",
    "Are you currently a government employee or will you be one?",
])
def test_beside_federal_history_now_or_retired_or_later_is_the_persons(label):
    """The fifth review of 0.3.227: "currently ... or a retired federal employee / annuitant / retiree", "...or will you
    be one?" ask about another time too: the profile doesn't say whether that federal job ended in retirement, nor
    what's to come. The person's, beside a history that says the person worked for the federal government."""
    data = {"history": {"us_government_employee": True}, "background": {"government_employee": False}}
    assert _nnss(label, data) is None
    assert _nnss("Are you currently a federal employee?", data) == "No"


@pytest.mark.parametrize("history", [{"us_government_employee": True}, {"us_government_employee": False}, {}])
def test_a_government_question_about_a_contractor_too_is_the_persons(history):
    """The fifth review of 0.3.227: "Are you currently a government employee or contractor?" got the background's
    No, which says nothing of contract work: the person's, whatever the history says."""
    data = {"history": history, "background": {"government_employee": False}}
    assert _nnss("Are you currently a government employee or contractor?", data) is None
    assert _nnss("Have you ever been a government employee or government contractor?", data) is None


@pytest.mark.parametrize("label, kind, extra", [
    ("Have you ever worked for any of the following companies? (select all that apply)", "combobox", {}),
    ("Have you ever worked for any of the following companies? (select all that apply)", "combobox",
     {"search": True}),
    ("Have you ever worked for any of the following companies? (select all that apply)", "listbox", {}),
    ("Have you ever worked for any of the following companies? Check all that apply.", "combobox", {}),
    ("Have you ever worked for one or more of the following companies?", "combobox", {}),
    ("Have you ever worked for any of the following companies?", "combobox", {"search": True}),
    ("Have you ever worked for any of the following companies?", "listbox", {}),
])
def test_a_list_that_takes_several_is_never_one_pick_beside_another_employer(label, kind, extra):
    """The fifth review of 0.3.227: a multi-select read without its flag (Workday's search prompt, a list box, a
    label that says "all that apply") was taken for a single choice, and Jacobs was picked alone for someone also at
    Los Alamos National Laboratory, whose operator (Triad) is among the choices. A list that takes several, by any of
    these, is the person's where another employer of theirs may be one by another name."""
    options = ["Jacobs Engineering Group Inc.", "Honeywell International, Inc.", "Triad National Security, LLC",
               "None of the above"]
    both = {"work_history": [{"company": c, "start": "2018-01", "end": "2020-01"}
                             for c in ("Jacobs Engineering Group", "Los Alamos National Laboratory")]}
    assert _nnss(label, both, kind=kind, options=options, job={"company": "Example Fab"}, **extra) is None
    # (the single choice still picks it)
    assert _nnss("Have you ever worked for any of the following companies?", both, kind="combobox", options=options,
                 job={"company": "Example Fab"}) == "Jacobs Engineering Group Inc."


@pytest.mark.parametrize("label", [
    "Have you formerly worked for any of the following companies?",
    "Have you previously worked for any of the following companies?",
])
def test_formerly_worked_for_is_never_the_employer_they_are_at_now(label):
    """The fifth review of 0.3.227: "Have you formerly worked for any of the following companies?" picked Jacobs for
    someone at Jacobs now. The person's; one they left is still picked."""
    options = ["Jacobs Engineering Group Inc.", "Honeywell International, Inc.", "None of the above"]
    now = {"work_history": [{"company": "Jacobs Engineering Group", "start": "2021-01", "end": "present"}]}
    left = {"work_history": [{"company": "Jacobs Engineering Group", "start": "2018-01", "end": "2020-01"}]}
    assert _nnss(label, now, kind="combobox", options=options, job={"company": "Example Fab"}) is None
    assert _nnss(label, left, kind="combobox", options=options, job={"company": "Example Fab"}) == \
        "Jacobs Engineering Group Inc."
