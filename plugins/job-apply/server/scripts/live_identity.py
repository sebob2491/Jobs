"""The live check's test identity (live_smoke.py --test-identity), kept apart from live_smoke.py
so it can be tested without that script's start-up (a home of its own, the server) or a browser.

The owner's call (Oct 10): a clearly fake applicant, "Jobdesk Test", with an inbox of its own, so
the nightly check can make accounts, read their emailed codes and get past sign-in, at one or two
employers per job system (test_identity_employers.yaml). It never submits: practice mode and
JOB_APPLY_NEVER_SUBMIT=1 stay on, and job_apply.config lets accounts and notices through in
practice mode only for this identity (config.live_test_identity). Setting it up: TEST_IDENTITY.md.

Its email and passwords come from the environment (the repository's secrets, in the nightly) and
are never printed: every line the run prints, and every file it writes, has them masked.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

ENV_EMAIL = "LIVE_TEST_EMAIL"  # the test identity's own inbox (a Gmail address)
ENV_EMAIL_PASSWORD = "LIVE_TEST_EMAIL_PASSWORD"  # that inbox's app password: the desk reads codes with it
ENV_SITE_PASSWORD = "LIVE_TEST_SITE_PASSWORD"  # one password for its accounts on every job system
ENV_VARS = (ENV_EMAIL, ENV_EMAIL_PASSWORD, ENV_SITE_PASSWORD)
IDENTITY_SWITCH = "JOB_APPLY_LIVE_TEST_IDENTITY"  # (config.LIVE_TEST_IDENTITY_ENV)

FIRST_NAME, LAST_NAME = "Jobdesk", "Test"
PHONE = "480-555-0199"  # a 555 number, as the fake profile's
RESUME_NOTE = "Automated test resume for the Job Desk live check — not a real applicant"
RESUME_NAME = "jobdesk-test-resume.pdf"
EMPLOYERS_FILE = Path(__file__).with_name("test_identity_employers.yaml")
EMAIL_MASK, PASSWORD_MASK = "<test-email>", "***"


def env_problem(env: Mapping[str, str]) -> str | None:
    """Why the test identity can't run with this environment, or None. Never says a value."""
    missing = [name for name in ENV_VARS if not (env.get(name) or "").strip()]
    if missing:
        return (f"--test-identity needs {', '.join(missing)} set (the test identity's inbox, its app password and "
                "the password for its job-site accounts; see scripts/TEST_IDENTITY.md). Nothing was run.")
    from job_apply import mailbox

    if mailbox.imap_host(env[ENV_EMAIL].strip()) is None:
        return (f"--test-identity needs {ENV_EMAIL} to be an inbox the desk can read sign-up codes from (a Gmail "
                "address; see scripts/TEST_IDENTITY.md). Nothing was run.")
    return None


def credentials(env: Mapping[str, str]) -> tuple[str, str, str]:
    """The test identity's email, inbox app password and site password, as they're used: the email
    trimmed, the app password with no whitespace in it (Google shows it in four groups of four, and
    it may be pasted that way), and the site password without whitespace pasted around it."""
    return env[ENV_EMAIL].strip(), "".join(env[ENV_EMAIL_PASSWORD].split()), env[ENV_SITE_PASSWORD].strip()


def secret_env(site_password: str, email_password: str) -> dict[str, str]:
    """The desk's saved passwords for this run, as config.get_secret reads them from the environment
    (JOB_APPLY_SECRET_<NAME>, ahead of secrets.yaml): the one site password for every job system the
    desk signs in to, and the inbox's app password as email_password, which pipeline.mail_login
    pairs with the profile's email. In the environment, as --fake-passwords puts its own, so no
    password is ever written to a file."""
    from job_apply.pipeline import PASSWORD_SITES

    names = [f"{ats}_password" for ats in PASSWORD_SITES]
    out = {_env_key(name): site_password for name in names}
    out[_env_key("email_password")] = email_password
    return out


def _env_key(name: str) -> str:
    return "JOB_APPLY_SECRET_" + re.sub(r"[^A-Z0-9]", "_", name.upper())  # (as config.get_secret spells it)


def identity_profile(base: Mapping[str, Any], email: str, resume: Path | str) -> dict[str, Any]:
    """The fake profile as the test identity: its name and inbox, the fake 555 phone and the fake
    profile's address, the test resume, and accounts and notices on (practice mode stays)."""
    profile = copy.deepcopy(dict(base))
    personal = dict(profile.get("personal") or {})
    personal.update(first_name=FIRST_NAME, last_name=LAST_NAME, email=email.strip(), phone=PHONE)
    profile["personal"] = personal
    profile["documents"] = {**(profile.get("documents") or {}), "resume": str(resume)}
    profile["settings"] = {**(profile.get("settings") or {}), "submit_mode": "dry_run",
                           "manage_accounts": True, "accept_notices": True}
    return profile


def resume_pdf(profile: Mapping[str, Any]) -> bytes:
    """A one-page resume for the test identity, made at run time: who it is, that it's a test, and
    the fake profile's jobs and schools."""
    personal = profile.get("personal") or {}
    address = personal.get("address") or {}
    place = ", ".join(str(p) for p in (address.get("city"), address.get("state")) if p)
    lines: list[tuple[int, str]] = [
        (18, f"{personal.get('first_name', FIRST_NAME)} {personal.get('last_name', LAST_NAME)}"),
        (10, " | ".join(str(p) for p in (personal.get("email"), personal.get("phone"), place) if p)),
        (10, ""),
        (12, RESUME_NOTE + "."),
        (10, "Made by the Job Desk plugin's nightly check (scripts/live_smoke.py --test-identity). Nothing in it is true."),
        (10, ""),
        (13, "Experience"),
    ]
    for job in profile.get("work_history") or []:
        lines.append((10, f"{job.get('title')}, {job.get('company')}, {job.get('location')} "
                          f"({job.get('start')} to {job.get('end')})"))
    lines += [(10, ""), (13, "Education")]
    for school in profile.get("education_history") or []:
        lines.append((10, f"{school.get('degree')}, {school.get('major')}, {school.get('school')} "
                          f"({school.get('start')} to {school.get('end')})"))
    return tiny_pdf(lines)


def tiny_pdf(lines: Iterable[tuple[int, str]]) -> bytes:
    """A one-page Letter PDF of these (font size, text) lines in Helvetica, written by hand: no
    browser, nothing to install, and its text stays readable by a job site's resume reader."""
    def text(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    ops = ["BT", "72 728 Td"]
    for size, line in lines:
        ops += [f"/F1 {size} Tf", f"{round(size * 1.45)} TL", f"({text(line)}) Tj", "T*"]
    ops.append("ET")
    stream = "\n".join(ops).encode("cp1252", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
        b"/Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Title (Automated test resume) /Producer (Job Desk live check) >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 6 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _spellings(value: str) -> set[str]:
    """How a value may come out: as is, JSON-escaped (in a LIVE_PIPELINE line), URL-encoded (in a
    link), and an app password without the spaces Google shows it with."""
    out = {value, json.dumps(value)[1:-1], quote(value, safe=""), value.replace(" ", "")}
    return {v for v in out if len(v) >= 4}  # (shorter, and the masking would garble what it's in)


def masker(email: str, passwords: Iterable[str]) -> Callable[[str], str]:
    """A function that masks the test identity's email and passwords in a text, in any letter case."""
    masks: dict[str, str] = {}
    for password in passwords:
        if password:
            masks.update({v.lower(): PASSWORD_MASK for v in _spellings(password)})
    if email:
        masks.update({v.lower(): EMAIL_MASK for v in _spellings(email.strip())})
    if not masks:
        return lambda text: text
    found = re.compile("|".join(re.escape(v) for v in sorted(masks, key=len, reverse=True)), re.I)
    return lambda text: found.sub(lambda m: masks[m.group().lower()], text)


class MaskedStream:
    """sys.stdout or sys.stderr, with every line's secrets masked (print writes a line whole)."""

    def __init__(self, stream: Any, mask: Callable[[str], str]):
        self._stream, self._mask = stream, mask

    def write(self, text: str) -> int:
        self._stream.write(self._mask(text))
        return len(text)

    def __getattr__(self, name: str) -> Any:  # flush, fileno, encoding, ...
        return getattr(self._stream, name)


def mask_files(folder: Path, mask: Callable[[str], str]) -> None:
    """The text files a run saved (a page's HTML, its fields), with the secrets masked."""
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() in (".html", ".json", ".md", ".txt", ".log"):
            text = path.read_text(encoding="utf-8", errors="surrogateescape")
            masked = mask(text)
            if masked != text:
                path.write_text(masked, encoding="utf-8", errors="surrogateescape")


def load_employers(path: Path = EMPLOYERS_FILE) -> list[dict[str, str]]:
    """The employers the test identity applies at: [{name, list, role, system, why}], in the file's order."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for entry in data.get("employers") or []:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) and entry[k].strip()
                                                  for k in ("name", "list", "role", "system", "why")):
            raise ValueError(f"{path.name}: each employer needs a name, list, role, system and why: {entry!r}")
        out.append({k: entry[k].strip() for k in ("name", "list", "role", "system", "why")})
    return out
