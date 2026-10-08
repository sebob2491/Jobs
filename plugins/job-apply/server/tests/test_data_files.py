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
