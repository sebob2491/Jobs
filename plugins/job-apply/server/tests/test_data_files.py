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
