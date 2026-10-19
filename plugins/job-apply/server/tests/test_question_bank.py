"""The common questions (data/question_bank.yaml): asked once on the Job Desk, each answer kept in answers.yaml with
the bank's pattern fills that question however an employer words it. Wordings from the nightly live check, Oct 11."""

import re

import pytest
import yaml

from job_apply import autofill, config

BANK = {q["id"]: q for q in config.question_bank()}


@pytest.mark.parametrize("bank_id, label", [
    ("heard_about", "How Did You Hear About Us?*"),
    ("heard_about", "How did you hear about this job posting? (please make only 1 selection)"),
    ("heard_about", "How did you happen to apply here? Please specify source."),
    ("heard_about", "Source Type . Required"),
    ("desired_pay", "What are your salary expectations for this role?"),
    ("desired_pay", "What is your desired annual salary?"),
    ("desired_pay", "This is an hourly position. What is your desired compensation for this position?"),
    ("desired_pay", "What is your expected fixed annual salary?"),
    ("desired_pay", "Desired Salary"),
    ("start_date", "What is your desired start date?"),
    ("start_date", "When would you be available if an offer was accepted?"),
    ("relocate", "Would you consider relocating, either now or in the future?"),
    ("background_check", "Would you be willing to submit to a background check if a job is offered?"),
    ("adult", "Are you age 18 or over?"),
    ("adult", "Are you 18 years of age or older?"),
    ("essential_functions",
     "Are you able to perform the essential functions of the job, with or without reasonable accommodations?"),
    ("terminated",
     "Have you ever been terminated or asked to resign by any former employer for the following reasons?"),
    ("shift", "What is your shift preference?"),
    ("schedule_type", "What is your desired schedule type?"),
    ("language", "Language of Application"),
    ("language", "Preferred Locale/Language"),
    ("community_college",
     "Have you ever attended a community college or taken classes/certifications at a community college?"),
    ("military_spouse", "Are you a current or former military spouse or domestic partner?"),
    ("proof_of_age", "If hired, can you furnish proof of age?"),
    # run 88's (Oct 14): the HR applications at BDO, Knight-Swift and Robert Half, and EMD, ASM and Matheson
    ("non_compete", "Are you currently subject to a non-compete or employment agreement with another employer?"),
    ("non_compete", "Do you have a Non-Solicitation or Non-Compete Agreement?"),
    ("non_compete", "Do you have any agreement with your current employer or any other employer that you had in the "
                    "past 2 years (such as any non-solicit, non-competition, or non-disclosure agreement)?"),
    ("non_compete", "Have you entered into any restrictive covenant, non-compete agreement, or nondisclosure agreement "
                    "with your current or any former employer that would restrict your employment?"),
    ("non_compete", "Have you ever entered in to a non-disclosure or non-compete agreement or any other agreement to "
                    "restrict your employment?"),
    ("non_compete", "Are you a party to any agreement(s) that would: (a) prohibit you from working for Example Corp; "
                    "(b) restrict your ability to solicit clients?"),
])
def test_each_common_question_covers_the_wordings_employers_asked(bank_id, label):
    hits = [q["id"] for q in BANK.values() if re.search(q["match"], label, re.I)]
    assert hits == [bank_id], (label, hits)


@pytest.mark.parametrize("label", [
    "Start Date",  # a past job's
    "Have you received notice from any government agency in the last 18 months?",
    # the review of Oct 15: another question about a background check, not consent to one; a count of 18, not an age
    "Have you ever refused to consent to a background check?",
    "Have you ever declined to agree to a background check?",
    "Do you have 18 years of experience?",
    "Have you completed at least 18 credits?",
    "Over the past 18 months, have you worked for a competitor?",
    "If under age 18, can you furnish a work permit?",
    "Employment Type",  # a past job's
    "Current/Last drawn monthly basic salary",
    "Do you require relocation assistance?",
    "Were you referred by a current Carvana or ADESA employee?",
    "Have you ever been employed by APS?",
    "Do you have any relatives employed by this organization?",
    "SMS Consent - I consent to Swift Transportation contacting me via text message",
    "Use of your profile: please select Yes if you wish your profile to be kept in our global talent community",
    # the review's (0.3.118): another question that shares words with one of the bank's
    "Were you ever discharged from the military?",
    "Have you ever been dismissed from school?",
    "Are you able to work at least 16 hours?",
    "Are you at least 16 years old or older?",  # (16 isn't 18: not the same question)
    "Which shift did you work?",
    "Referral source name",
    "How did you hear about us? (If employee referral, please provide their name)",
    "Willing to relocate with relocation assistance?",
    # another employer's own (a competitor of this one), or whether they'd sign one: not whether they're bound by one
    "Do you currently work for a competitor of Example Corp and/or are you subject to any post-employment "
    "restrictions, such as a noncompete?",
    "Are you willing to sign a non-compete agreement?",
    "If hired, will you agree to sign a non-disclosure and non-solicitation agreement?",
    "Do you agree to the terms of our confidentiality agreement?",
    # a consent to an agreement of the employer's (the review's): the person's each time, never their non-compete answer
    "Do you agree to the arbitration agreement, which prohibits class action lawsuits?",
    "I have read and agree to the confidentiality agreement that restricts use of company data.",
    "Do you acknowledge the employment agreement restricts outside employment?",
    "I acknowledge that I may be asked to sign a non-compete agreement.",
])
def test_no_common_question_takes_one_that_isnt_the_persons_alone(label):
    """A past job's dates and type, the employer's own (worked there, relatives, a referral), a consent: never the
    bank's (each employer's or job's own, or the person's every time)."""
    assert not [q["id"] for q in BANK.values() if re.search(q["match"], label, re.I)], label


def test_an_answer_given_once_fills_every_wording_and_can_be_changed_or_taken_out(job_apply_home):
    config.save_answer("Are you willing to relocate?", "No")  # one given on a job's card stays as it was
    config.save_bank_answer("heard_about", "Company website")
    config.save_bank_answer("desired_pay", " $28 per hour ")
    bank = {q["id"]: q for q in config.question_bank()}
    assert (bank["heard_about"]["answer"], bank["desired_pay"]["answer"], bank["shift"]["answer"]) == \
        ("Company website", "$28 per hour", None)
    config.save_bank_answer("shift", "Days")
    prof = config.Profile.load()
    for label, company in (("What is your shift preference?*", "KLA"), ("Preferred Shift", "Linde")):
        got = autofill.resolve_field({"kind": "select", "label": label, "options": ["Select One", "Days", "Nights"]},
                                     prof, {"company": company})
        assert got is not None and got.value == "Days", (label, got)
    # the tests' profile says how it heard of jobs: that comes first, and the card doesn't ask it
    heard = autofill.resolve_field({"kind": "text", "label": "How did you hear about us?"}, prof, {"company": "KLA"})
    assert heard is not None and heard.rule == "how_heard", heard
    pay = autofill.resolve_field({"kind": "text", "label": "What is your desired annual salary?"}, prof,
                                 {"company": "X"})
    assert pay is not None and pay.value == "$28 per hour", pay
    with pytest.raises(ValueError):
        config.save_bank_answer("relocate", "Maybe")  # (a choice question's answer is one of its choices)
    with pytest.raises(ValueError):
        config.save_bank_answer("no_such_question", "Yes")
    config.save_bank_answer("heard_about", "")
    kept = yaml.safe_load(config.answers_path().read_text(encoding="utf-8"))["answers"]
    assert [a.get("bank") for a in kept] == ["shift", "desired_pay", None], kept
    assert kept[2]["question"] == "Are you willing to relocate?"


def test_a_common_answer_comes_last_and_never_in_a_past_jobs_block(job_apply_home):
    """The review of 0.3.118: the profile's own rules (desired salary) and an answer the person gave for that exact
    question come before a common one; a block about one past job or school never takes one."""
    import yaml as _yaml
    path = job_apply_home / "profile.yaml"
    profile = _yaml.safe_load(path.read_text(encoding="utf-8"))
    profile.setdefault("preferences", {})["desired_salary"] = "65000"
    path.write_text(_yaml.safe_dump(profile), encoding="utf-8")
    config.save_bank_answer("desired_pay", "$28 per hour")
    config.save_bank_answer("shift", "Days")
    prof = config.Profile.load()
    pay = autofill.resolve_field({"kind": "text", "label": "Desired Salary"}, prof, {"company": "X"})
    assert pay is not None and pay.rule != "answers" and "28" not in str(pay.value), pay
    config.save_answer("What is your shift preference?", "Nights", "X")
    prof = config.Profile.load()
    own = autofill.resolve_field({"kind": "text", "label": "What is your shift preference?"}, prof, {"company": "X"})
    assert own is not None and own.value == "Nights", own
    common = autofill.resolve_field({"kind": "text", "label": "Preferred shift"}, prof, {"company": "X"})
    assert common is not None and common.value == "Days", common
    past = autofill.resolve_field({"kind": "text", "label": "Preferred shift", "section": "Work Experience 2"},
                                  prof, {"company": "X"})
    assert past is None or past.value != "Days", past


def test_an_account_forms_security_questions_are_never_listed_for_the_bank():
    """scripts/question_bank.py lists the questions a nightly asked that no common question covers. An account form's
    security questions (BrassRing's, on the desk's card) are that account's alone, never a common question."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "question_bank.py"
    spec = importlib.util.spec_from_file_location("question_bank", path)
    qb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qb)
    rounds = [{"need": "sign_in", "questions": [
        {"label": "* Question 1", "kind": "listbox", "options": ["In what city were you born?"], "account": True},
        {"label": "Question 1 Answer", "kind": "text", "account": True}]},
        {"need": "questions", "questions": [{"label": "Do you have a forklift license?", "kind": "select"}]}]
    found = qb.uncovered([{"company": "Example Corp", "rounds": rounds}], [])
    assert [label for label, _, _ in found] == ["Do you have a forklift license"], found


def test_an_answer_saved_with_an_older_bank_pattern_takes_the_banks_current_one(job_apply_home):
    """An answer saved on the desk keeps the bank's pattern of that day in answers.yaml, so a pattern corrected since
    (background_check's, 0.3.175: "have you ever refused to consent to a background check?" matched it) never
    reached anyone who had answered before (the review's). The bank's pattern of today is used; the file is the
    person's and stays as it is."""
    old = ("(?:willing|agree|consent)\\b.{0,20}\\b(?:submit to|undergo|complete|to)?\\s*(?:a )?"
           "background (?:check|screening|investigation)")
    (job_apply_home / "answers.yaml").write_text(
        yaml.safe_dump({"answers": [{"match": old, "answer": "Yes", "bank": "background_check"}]}), encoding="utf-8")
    prof = config.Profile.load()

    def answer(label):
        a = autofill.resolve_field({"id": "q", "label": label, "kind": "radio_group", "options": ["Yes", "No"],
                                    "value": ""},
                                   prof, {})
        return a and a.value

    assert answer("Are you willing to submit to a background check?") == "Yes"
    assert answer("Have you ever refused to consent to a background check?") is None
    kept = yaml.safe_load((job_apply_home / "answers.yaml").read_text(encoding="utf-8"))
    assert kept["answers"][0]["match"] == old  # (the file is the person's: as they saved it)
