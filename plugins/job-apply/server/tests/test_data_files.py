"""The YAML shipped with the plugin must load and use known values."""

import yaml

from job_apply import config
from job_apply.ats import ATS_NAMES


def test_profile_template_loads_with_safe_defaults(tmp_path):
    data = yaml.safe_load(config.TEMPLATE_PROFILE.read_text())
    prof = config.Profile(data)
    s = prof.settings
    assert (s.submit_mode, s.auto_submit_ats, s.email_codes, s.email_tracking) == ("review", [], False, False)
    assert "personal.first_name" in prof.missing_required()
    assert data["work_history"] == [] and data["education_history"] == []
    assert all("match" in a for a in data["answers"])


def test_accepting_cookies_is_the_persons_choice():
    """Off unless set to true, as written: "yes" or a missing setting leaves banners to the person."""
    assert config.Settings.from_dict({}).accept_cookies is False
    assert config.Settings.from_dict({"accept_cookies": "yes"}).accept_cookies is False
    assert config.Settings.from_dict({"accept_cookies": True}).accept_cookies is True


def test_managing_accounts_is_the_persons_choice_and_never_in_practice_mode():
    """Off unless set to true, as written; and practice mode, which sends nothing, never makes an
    account or resets a password."""
    assert config.Settings.from_dict({}).may_manage_accounts is False
    assert config.Settings.from_dict({"manage_accounts": "yes"}).may_manage_accounts is False
    assert config.Settings.from_dict({"manage_accounts": True}).may_manage_accounts is True
    assert config.Settings.from_dict({"manage_accounts": True, "submit_mode": "dry_run"}).may_manage_accounts is False


def test_submit_mode_spellings():
    from job_apply.config import Settings

    for raw in ("dry_run", "dry-run", "Dry Run", "dryrun", "practice"):
        assert Settings.from_dict({"submit_mode": raw}).submit_mode == "dry_run", raw
    assert Settings.from_dict({"submit_mode": "AUTO"}).submit_mode == "auto"
    odd = Settings.from_dict({"submit_mode": "yolo"})
    assert odd.submit_mode == "review" and "yolo" in odd.warnings[0]
    assert Settings.from_dict({}).warnings == []


def test_click_guard_rules(monkeypatch):
    import pytest

    from job_apply.browser import BrowserSession, SubmitBlocked

    check = BrowserSession._check_clickable
    with pytest.raises(SubmitBlocked):
        check({"label": "Submit Application", "text": "Submit Application", "formSubmit": False})
    with pytest.raises(SubmitBlocked):
        check({"label": "Apply", "text": "Apply", "formSubmit": True})
    check({"label": "Apply", "text": "Apply", "formSubmit": False})  # Workday's start button isn't in a form
    check({"label": "Send it", "text": "Send it", "formSubmit": True})
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    check({"label": "Save and Continue", "text": "Save and Continue", "formSubmit": True})
    check({"label": "Next", "text": "Next", "formSubmit": True})
    with pytest.raises(SubmitBlocked):
        check({"label": "Send it", "text": "Send it", "formSubmit": True})


def test_every_employer_list_loads_with_known_values():
    """The plugin's employer lists (data/companies.yaml and data/lists/*.yaml, which a
    person's companies.yaml names) and the template that names them. A misspelt search kind
    would leave that employer out of every search, silently."""
    from job_apply.search import BROWSER_SEARCHES, SEARCHERS, employer_lists

    from job_apply.autofill import norm

    lists = employer_lists()
    assert len(yaml.safe_load(lists["semiconductor-az"].read_text())["companies"]) >= 20
    for name, path in lists.items():
        data = yaml.safe_load(path.read_text())
        names = [norm(c["name"]) for c in data["companies"]]  # as load_companies tells them apart
        assert names and len(names) == len(set(names)), name
        for c in data["companies"]:
            assert c["careers_url"].startswith("https://"), (name, c)
            assert c["ats"] in set(ATS_NAMES) | {"custom", "unknown"}, (name, c)
            assert c["confidence"] in {"high", "medium", "low"}, (name, c)
            for kind in c.get("search") or {}:
                assert kind in set(SEARCHERS) | BROWSER_SEARCHES, (name, c["name"], kind)
    template = yaml.safe_load((config.PLUGIN_ROOT / "templates" / "companies.example.yaml").read_text())
    assert set(template["lists"]) <= set(lists) and template["companies"] == []


def test_the_templates_hospital_question_is_only_the_exclusion_question():
    """The profile template's answer for hospitals' exclusion question (Mayo Clinic's, live)
    must not answer a question that only mentions Medicare: its "No" would say the person has
    no Medicare billing experience."""
    import re

    data = yaml.safe_load(config.TEMPLATE_PROFILE.read_text())
    pattern = next(a["match"] for a in data["answers"] if "medicare" in a["match"])
    for asked in ("Have you received notice from any government agency that you are or were unable to participate in "
                  "Medicare, Medicaid, or any other government program?",
                  "Have you ever been excluded from participation in any federal health care program?"):
        assert re.search(pattern, asked, re.I), asked
    assert not re.search(pattern, "Do you have experience with Medicare/Medicaid billing?", re.I)


def test_the_templates_answers_answer_only_the_question_they_are_for():
    """A pattern is searched for anywhere in a question: the template's "background check|drug
    (test|screen)", given "Yes" for "Are you willing to take a drug test?", answered "Have you
    ever failed a drug test?" with Yes, and a felony question that mentioned a background check
    too. Each pattern answers its own question, through the answer bank forms are filled from;
    a look-alike asking the opposite is the person's."""
    import re

    from job_apply.autofill import _answer_bank

    data = yaml.safe_load(config.TEMPLATE_PROFILE.read_text())
    patterns = [a["match"] for a in data["answers"]]
    prof = config.Profile({"answers": [{"match": p, "answer": f"answer {i}"} for i, p in enumerate(patterns)]})

    def answered(asked: str) -> bool:
        return _answer_bank(prof, asked) is not None

    for asked in ("Are you able to work in a cleanroom environment?",
                  "Are you willing to work in a clean room wearing a full gown?",
                  "Can you lift up to 50 lbs?",
                  "Can you lift 25-50 lbs?",
                  "Are you willing to submit to a background check and drug screen?",
                  "Are you willing to submit to a criminal background check?",
                  "This position requires a pre-employment drug test. Do you consent?",
                  "Would you be able to pass a background check and drug screen?",
                  "Do you have any relatives currently employed by this company?",
                  "Do any family members work here?",
                  "Do you have any relatives at this company?",
                  "Are you currently bound by a non-compete or non-solicitation agreement?",
                  "Have you signed a non-compete with your current employer?",
                  "Are you bound by a non-compete that would prevent you from working here?",
                  "Do you consent to a credit check?",
                  "Have you been employed by any government agency in the last two years?"):
        assert answered(asked), asked
        assert len([p for p in patterns if re.search(p, asked, re.I)]) == 1, asked  # not two patterns' answers
    for asked in ("Have you ever failed a drug test?",
                  "Have you ever refused a drug screen?",
                  "Have you ever tested positive on a drug test?",
                  "Have you ever been convicted of a felony? A conviction will not automatically disqualify you; "
                  "a background check will be conducted.",
                  "Do you have any criminal convictions? A background check will be performed.",
                  "Do you have a criminal record? All offers are contingent on a background check.",
                  "Is there anything in your background that would prevent you from passing a background check or "
                  "drug screen?",
                  "Do you have any pending charges? A background check is required.",
                  "How many years of cleanroom experience do you have?",
                  "Describe your cleanroom experience.",
                  "Can you lift 150 lbs?",
                  "Are you able to lift 250 pounds?",
                  "Relative's name",
                  "Relative's work phone",
                  "Are you willing to sign a non-compete agreement as a condition of employment?",
                  "Would you accept a non-compete agreement?",
                  "Would you be able to comply with a non-compete clause?",
                  "Have you ever declared bankruptcy or had a negative item on your credit report?",
                  "Is there anything in your credit history we should know about?",
                  "Please explain any issues in your credit history.",
                  "Have you worked for a government contractor?"):
        assert not answered(asked), asked


def test_a_profile_from_an_older_template_answers_with_todays_patterns(job_apply_home):
    """Profiles made before 0.3.61 kept the old patterns, which answered "Have you ever failed a
    drug test?" with the Yes given for "willing to take one?". Read now, such a profile uses the
    template's pattern; one the person wrote themselves is theirs, and the file isn't touched."""
    from job_apply.autofill import _answer_bank

    template = [a["match"] for a in yaml.safe_load(config.TEMPLATE_PROFILE.read_text())["answers"]]
    assert set(config.RETIRED_ANSWER_PATTERNS.values()) <= set(template)
    old = ("answers:\n"
           "  - match: \"background check|drug (test|screen)\"\n    answer: \"Yes\"\n"
           "  - match: \"drug-free\"\n    answer: \"Yes\"\n")
    (job_apply_home / "profile.yaml").write_text(old)
    prof = config.Profile.load()
    assert _answer_bank(prof, "Have you ever failed a drug test?") is None
    assert _answer_bank(prof, "Are you willing to take a drug test?").value == "Yes"
    assert [a["match"] for a in prof.get("answers")][-1] == "drug-free"
    assert (job_apply_home / "profile.yaml").read_text() == old
