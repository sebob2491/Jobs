"""The live check's fake applicant: the profile scripts/live_smoke.py applies with (its roles add a background), here
apart from live_smoke.py, which sets up a run's home as it's imported, so the live audit (live_audit.py) reads the same
values without starting a run. Nothing in it is anyone's."""

from __future__ import annotations

import re
import secrets
from typing import Any

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
                  "degrees_earned": [{"level": "high_school"}, {"level": "bachelor", "field": "Electrical Engineering"}]},
    # (accept_cookies, as a person may set it: a banner with no way to decline, Texas Instruments' Oracle one, is
    # accepted, so the check sees past it; one that can be declined still is: the owner asked, Oct 13)
    "settings": {"submit_mode": "dry_run", "browser_channel": "chromium", "headless": True, "accept_cookies": True},
}
