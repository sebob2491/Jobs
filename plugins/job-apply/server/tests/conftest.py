import asyncio
import os
import zlib
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
    "work_history": [
        {"title": "Equipment Technician", "company": "Intel", "location": "Chandler, AZ", "start": "2021-03",
         "end": "present", "description": "Maintained 300mm etch and deposition tools."},
        {"title": "Maintenance Technician", "company": "Example Fab Services", "location": "Tempe, AZ",
         "start": "Jun 2018", "end": "02/2021", "description": "PMs and troubleshooting on vacuum pumps."},
    ],
    "education_history": [
        {"school": "Arizona State University", "degree": "BS Electrical Engineering", "major": "Electrical Engineering",
         "gpa": 3.4, "start": 2016, "end": 2020},
    ],
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


def launch_options() -> dict:
    """For tests that start a browser of their own: the same Chromium the app is told to use."""
    exe = os.environ.get("JOB_APPLY_CHROMIUM_PATH")
    return {"executable_path": exe} if exe else {}


def browser_available() -> bool:
    if os.environ.get("JOB_APPLY_CHROMIUM_PATH"):
        return True
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache" / "ms-playwright"))
    return any(root.glob("chromium-*"))


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    asyncio.set_event_loop(lp)
    yield lp
    lp.close()


@pytest.fixture
def srv(job_apply_home, loop):
    """The MCP server module with a fresh tracker; closes its browser afterwards."""
    from job_apply import server

    server._tracker = None
    server.browser.current_job_id = None
    yield server
    loop.run_until_complete(server.browser.close())
    if server._tracker:
        server._tracker.close()
        server._tracker = None


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def by_label(fields, text):
    return next(f for f in fields if text.lower() in f["label"].lower())


async def check_fixture_extraction(server, html: Path, expect: dict) -> list[str]:
    """Open a saved page and report expected fields that are no longer extracted."""
    await server.browser.goto(html.resolve().as_uri())
    got = (await server.browser.inspect(include_dropdown_options=False))["fields"]
    problems = []
    for want in expect["fields"]:
        same = [f for f in got if f.get("label") == want["label"] and f.get("kind") == want["kind"]]
        if not same:
            problems.append(f"missing {want['kind']} field {want['label']!r}")
        elif "required" in want and not any(bool(f.get("required")) == want["required"] for f in same):
            problems.append(f"required flag changed for {want['label']!r}")
    return problems


def pytest_collection_modifyitems(config, items):
    """JOB_APPLY_TEST_SHARD=1/2 runs only that part of the suite (CI runs the parts on separate
    machines). A test's part comes from its id, so every machine and xdist worker agrees."""
    shard = os.environ.get("JOB_APPLY_TEST_SHARD")
    if not shard:
        return
    part, parts = (int(x) for x in shard.split("/"))
    mine = [zlib.crc32(item.nodeid.encode()) % parts == part - 1 for item in items]
    config.hook.pytest_deselected(items=[item for item, keep in zip(items, mine) if not keep])
    items[:] = [item for item, keep in zip(items, mine) if keep]
