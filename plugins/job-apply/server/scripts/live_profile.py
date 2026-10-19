"""The live check's fake applicant: the profile scripts/live_smoke.py applies with (its roles add a background), here
apart from live_smoke.py, which sets up a run's home as it's imported, so the live audit (live_audit.py) reads the same
values without starting a run. Nothing in it is anyone's."""

from __future__ import annotations

import copy
import re
import secrets
from typing import Any, Mapping

# A fresh address each run, on example.com (which takes no mail): Oracle's sites stop sending
# codes to an address after a day of runs ("Too Many Attempts. Try Again Later.").
FAKE_EMAIL = f"testy.mctestface.{secrets.token_hex(3)}@example.com"
FAKE_EMAIL_SHAPE = re.compile(r"\btesty\.mctestface\.[0-9a-f]{6}@example\.com\b", re.I)  # (any run's)
FAKE_PROFILE: dict[str, Any] = {
    "personal": {
        "first_name": "Testy", "last_name": "McTestface", "email": FAKE_EMAIL,
        "phone": "480-555-0199", "phone_country_code": "+1",
        # Chandler's city hall: a real street, so address lookups (Oracle's) can find it
        "address": {"line1": "175 S Arizona Ave", "city": "Chandler", "state": "AZ", "postal_code": "85225",
                    "country": "United States"},
        "linkedin_url": "https://www.linkedin.com/in/example-test-profile", "drivers_license": True,
    },
    "documents": {"resume": None},  # (use_role gives it the run's test resume)
    "work_authorization": {"authorized_to_work": True, "requires_sponsorship": False, "us_person": True,
                           "us_citizen": True, "over_18": True},
    "preferences": {"how_did_you_hear": "Company Website", "willing_to_relocate": True,
                    "willing_to_travel": "Yes", "flexible_schedule": True,
                    "locations": ["Phoenix, AZ", "Chandler, AZ", "Tempe, AZ"]},
    "eeo": {"gender": "Decline to self-identify", "hispanic_latino": "Decline to self-identify",
            "race": "Decline to self-identify", "veteran": "I don't wish to answer",
            "disability": "I do not want to answer"},
    # (with a description: Workday's Role Description is required at some employers, and three stopped there, run 44)
    "work_history": [{"title": "Equipment Technician", "company": "Example Fab", "location": "Chandler, AZ",
                      "start": "2021-03", "end": "present",
                      "description": "Maintained and repaired process equipment; ran preventive maintenance."}],
    "education_history": [{"school": "Arizona State University", "degree": "Bachelor of Science",
                           "major": "Electrical Engineering", "start": 2016, "end": 2020}],
    # (its kind named, as setup asks for it: a list with several kinds of a bachelor's, BA and BS, is the
    # person's to choose from a bare "Bachelor's Degree", and ten employers' stopped there, nightly Oct 11)
    # as setup writes it: the degrees confirmed finished answer "Do you have a Bachelor's degree?"
    "education": {"highest_degree": "Bachelor's Degree",
                  "degrees_earned": [{"level": "high_school"},
                                     {"level": "bachelor", "field": "Electrical Engineering"}]},
    # (accept_cookies, as a person may set it: a banner with no way to decline, Texas Instruments' Oracle one, is
    # accepted, so the check sees past it; one that can be declined still is: the owner asked, Oct 13)
    "settings": {"submit_mode": "dry_run", "browser_channel": "chromium", "headless": True, "accept_cookies": True},
}

# --profile-shape (live_smoke.py): "plain" applies with FAKE_PROFILE (its role's background in), "awkward" with the
# same profile written as a person might write it by hand (awkward below). Sundays' awkward check (live-nightly.yml)
# runs it beside the plain pipeline check, and nightly_summary.py --compare-shapes says where it got less far.
PROFILE_SHAPES = ("plain", "awkward")
AWKWARD_SCHOOL = "Arizona State University"  # (the plain profile's own school: no new real name in the repo)
AWKWARD_PHONE = "602-555-0143"  # (555-01xx: no one's number)


def awkward(profile: Mapping[str, Any] = FAKE_PROFILE) -> dict[str, Any]:
    """The profile in an awkward shape (the design of Oct 18, A7), its jobs' titles, employers and starts and the
    rest as they were:
      - its current job held with no end date at all (`current: true`, which the desk reads as still held), not
        "end: present";
      - two entries at one school, both coursework: `degree: ""` (coursework is never a degree, and the desk must
        never claim one for it), the role's major as the classes;
      - a GED as the one credential finished (`education.highest_degree: GED`, `degrees_earned: [{level: ged}]`): no
        diploma, no bachelor's;
      - a second phone (`personal.alternate_phone`): the profile's own number stays `phone`, the one the desk fills;
        no rule reads the second, so an "Alternate phone" box stays the person's.
    Nothing in it is anyone's."""
    out: dict[str, Any] = copy.deepcopy(dict(profile))
    jobs = [dict(job) for job in out.get("work_history") or []]
    if jobs:
        jobs[0] = {**{k: v for k, v in jobs[0].items() if k != "end"}, "current": True}
    out["work_history"] = jobs
    schools = out.get("education_history") or [{}]
    major = str(schools[0].get("major") or "General Studies")
    out["education_history"] = [  # (most recent first, as the template has them)
        {"school": AWKWARD_SCHOOL, "degree": "", "major": major, "start": 2018, "end": 2019},
        {"school": AWKWARD_SCHOOL, "degree": "", "major": major, "start": 2015, "end": 2016},
    ]
    out["education"] = {"highest_degree": "GED", "degrees_earned": [{"level": "ged"}]}
    out["personal"] = {**(out.get("personal") or {}), "alternate_phone": AWKWARD_PHONE}
    return out


def shaped(profile: Mapping[str, Any], shape: str) -> dict[str, Any]:
    """The profile in one of PROFILE_SHAPES."""
    if shape not in PROFILE_SHAPES:
        raise ValueError(f"no profile shape named {shape!r}; shapes: {', '.join(PROFILE_SHAPES)}")
    return awkward(profile) if shape == "awkward" else copy.deepcopy(dict(profile))
