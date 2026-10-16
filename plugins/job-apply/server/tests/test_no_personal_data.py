"""The repo is public: nothing personal in it (CLAUDE.md). Every email address and phone number in its tracked text is
one of the ones below (the job systems' senders, made-up test addresses, an example's) or on a reserved test domain or
a 555 or toll-free number; a new one fails until someone has looked at it and listed it here (the review of Oct 15's
no-personal-data gate). The captured fixtures have their own gate (test_sources.py)."""

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
_EMAIL = re.compile(r"\b[\w.%+-]+@(?!(?:[\w-]+\.)*example(?:\.(?:com|org|net))?\b)[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b", re.I)
_PHONE = re.compile(r"(?<![\w-])(?:\+?1[-. ]?)?\(?(\d{3})\)?[-. ](\d{3})[-. ]\d{4}(?![\w-])"
                    r"|\btel:\s*\+?1?[-. (]*(\d{3})\)?[-. ]*(\d{3})[-. ]*\d{4}\b", re.I)  # (a click-to-call link's too)
# Looked at, each one: a job system's or an employer's sender (no-reply), the live check's made-up inbox and its plus
# addresses (jobdesk_test_not_real), a placeholder in a docstring (name@gmail.com), a made-up sender in a test
KNOWN = {
    "ab+kla@gmail.com", "ab+x@gmail.com", "ab@gmail.com", "acme@otp.workday.com", "confirm@eightfold.ai",
    "donotreply@kenexa.com", "jobdesk_test_not_real+a+b@gmail.com", "jobdesk_test_not_real+ultra-clean-holdings-uct@gmail.com",
    "jobdesk_test_not_real+x@yahoo.com", "jobdesk_test_not_real@gmail.com", "jobdesk_test_not_realx+a@gmail.com",
    "name+jobs@gmail.com", "name@gmail.com", "no-reply@administrativesystem.mayoclinic.org", "no-reply@another-employer.com",
    "no-reply@careers.acme.com", "no-reply@careers.bdo.com", "no-reply@careers.ti.com", "no-reply@mail.brassring.com",
    "no-reply@recruitment.americanexpress.com", "no-reply@us.greenhouse-mail.io", "no-reply@us.gt.com",
    "no-reply@workflow.email.us-phoenix-1.ocs.oraclecloud.com", "noreply@brassring.com", "noreply@kenexa.com",
    "noreply@mailexamplelitho.com", "noreply@not-kenexa.com", "noreply@oraclecloud.com", "noreply@workday.intel.com",
    "noreply@workday.kla.com", "offers@not-ti.com", "offers@shop.com", "other@otp.workday.com",
    "send-only.example@otp.workday.com", "someone+jobdesk_test_not_real@gmail.com", "someone@gmail.com", "someone@kla.com",
    "someone@outlook.com", "x.fa.sender@workflow.email.us-phoenix-1.ocs.oraclecloud.com",
}
# Phone numbers looked at: an employer's published HR line its captured pages carry (Lam Research's)
KNOWN_PHONES = {"510-572-4477"}
_TEXT = (".md", ".py", ".yaml", ".yml", ".json", ".html", ".txt", ".toml", ".sh", ".ps1", ".js", ".cfg", ".ini")


def _tracked() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [REPO / f for f in out.splitlines()
            if f.endswith(_TEXT) and "/tests/fixtures/" not in f and (REPO / f).is_file()]


def test_no_new_email_address_or_phone_number_in_the_repo():
    emails, phones = set(), set()
    for path in _tracked():
        text = path.read_text(encoding="utf-8", errors="replace")
        emails |= {m.group(0).lower() for m in _EMAIL.finditer(text)} - KNOWN
        for m in _PHONE.finditer(text):
            area, exchange = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            if exchange != "555" and area not in ("800", "888", "877", "866", "855", "844", "833", "000") \
                    and m.group(0) not in KNOWN_PHONES:
                phones.add(f"{path.relative_to(REPO).as_posix()}: {m.group(0)}")
    assert not emails, f"addresses not looked at yet (a person's? list them in KNOWN once checked): {sorted(emails)}"
    assert not phones, sorted(phones)
