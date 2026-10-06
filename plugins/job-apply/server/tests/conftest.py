import os
from pathlib import Path

import pytest
import yaml

FIXTURES = Path(__file__).parent / "fixtures"

PROFILE = {
    "personal": {
        "first_name": "Sam",
        "last_name": "Rivera",
        "email": "sam.rivera@example.com",
        "phone": "480-555-0123",
        "phone_country_code": "+1",
        "address": {"line1": "100 W Main St", "city": "Chandler", "state": "AZ", "postal_code": "85225",
                    "country": "United States"},
        "linkedin_url": "https://www.linkedin.com/in/samrivera",
    },
    "documents": {"resume": "RESUME_PLACEHOLDER"},
    "work_authorization": {"authorized_to_work": True, "requires_sponsorship": False, "us_person": True,
                           "over_18": True},
    "history": {"previous_employers": ["Intel"]},
    "preferences": {"willing_to_travel": "Yes, up to 75%", "how_did_you_hear": "LinkedIn",
                    "willing_to_relocate": False},
    "eeo": {"gender": "Decline to self-identify", "veteran": "I don't wish to answer"},
    "answers": [{"match": "clean ?room", "answer": "Yes"}, {"match": "lift", "answer": None}],
    "settings": {"submit_mode": "review", "browser_channel": "chromium", "headless": True},
}


@pytest.fixture(autouse=True)
def job_apply_home(tmp_path, monkeypatch):
    home = tmp_path / "job-apply"
    home.mkdir()
    resume = home / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% test resume\n")
    profile = {**PROFILE, "documents": {"resume": str(resume)}}
    (home / "profile.yaml").write_text(yaml.safe_dump(profile))
    monkeypatch.setenv("JOB_APPLY_HOME", str(home))
    monkeypatch.setenv("JOB_APPLY_HEADLESS", "1")
    return home


def fixture_url(name: str) -> str:
    return (FIXTURES / name).resolve().as_uri()


def browser_available() -> bool:
    if os.environ.get("JOB_APPLY_CHROMIUM_PATH"):
        return True
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache" / "ms-playwright"))
    return any(root.glob("chromium-*"))
