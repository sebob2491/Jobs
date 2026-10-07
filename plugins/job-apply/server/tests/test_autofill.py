from job_apply.autofill import (
    choose_option,
    choose_place,
    is_empty_value,
    place_words,
    plan_autofill,
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


def test_place_words_are_the_rest_of_the_address(job_apply_home):
    p = prof()
    words = place_words("city", p)
    assert "AZ" in words and "Arizona" in words and str(p.get("personal.address.postal_code")) in words
    assert place_words("phone", p) == []  # not an address part
