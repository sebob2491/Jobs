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


def test_companies_file():
    data = yaml.safe_load((config.PLUGIN_ROOT / "data" / "companies.yaml").read_text())
    names = [c["name"] for c in data["companies"]]
    assert len(names) == len(set(names)) >= 20
    for c in data["companies"]:
        assert c["careers_url"].startswith("https://"), c
        assert c["ats"] in set(ATS_NAMES) | {"custom", "unknown"}, c
        assert c["confidence"] in {"high", "medium", "low"}, c


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
