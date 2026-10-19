"""The live check's test identity (live_smoke.py --test-identity), kept apart from live_smoke.py
so it can be tested without that script's start-up (a home of its own, the server) or a browser.

The owner's call (Oct 10): a clearly fake applicant, "Jobdesk Test", with an inbox of its own, so
the nightly check can make accounts, read their emailed codes and get past sign-in, at a few
employers per job system (test_identity_employers.yaml). It never submits: practice mode and
JOB_APPLY_NEVER_SUBMIT=1 stay on, and job_apply.config lets accounts and notices through in
practice mode only for this identity (config.live_test_identity). Setting it up: TEST_IDENTITY.md.

Its email and passwords come from the environment (the repository's secrets, in the nightly) and
are never printed: every line the run prints, and every file it writes, has them masked.
"""

from __future__ import annotations

import copy
import hashlib
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
RESUME_NOTE = "Automated test resume for the Job Desk live check — not a real applicant"  # (both profiles')
RESUME_NAME = "jobdesk-test-resume.pdf"
# and a test cover letter beside it: a questionnaire that requires one (CSN's and NSU's Workday, run 144) stopped the
# check there, as the profile had none
COVER_LETTER_NOTE = "Automated test cover letter for the Job Desk live check — not a real applicant"
COVER_LETTER_NAME = "jobdesk-test-cover-letter.pdf"
EMPLOYERS_FILE = Path(__file__).with_name("test_identity_employers.yaml")
# The broad check's employers whose sites refuse its example.com address (their Workday, live run 19):
# there it gives the test identity's inbox instead, so the check sees how far it gets. Only the
# address: it still makes no accounts and reads no mail (the owner's call, Oct 11)
INBOX_AT = frozenset({"Onto Innovation", "Robert Half"})
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


def security_answer(question: Mapping[str, Any], seed: str) -> str:
    """With --security-answers (the owner's call, Oct 14): the test identity's answer to an account form's security
    question on the desk's card, as a person would give it there, so the live check goes on into the application
    (Edward Jones' BrassRing). A list's first real question; in an answer box, one made from its site password
    (`seed`) and the box's label: the same every run, so the account made with it keeps it, and never in this public
    repository nor anything the run prints (the desk never logs an answer)."""
    options = [str(o) for o in question.get("options") or []
               if o and not re.match(r"^\s*(?:-+\s*)?(?:select|choose|please|pick)\b", str(o), re.I)]
    if options:
        return options[0]
    return "jd" + hashlib.sha256(f"{seed}|{question.get('label') or ''}".encode("utf-8")).hexdigest()[:12]


def plus_address(email: str, employer: str) -> str:
    """The test inbox's own address for one employer (Gmail delivers "name+tag@" to "name@"): the --plus
    probe's, to see which sites take one."""
    local, _, domain = email.strip().partition("@")
    tag = re.sub(r"[^a-z0-9]+", "-", employer.lower()).strip("-")[:40] or "x"
    return f"{local}+{tag}@{domain}"


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
    """A one-page resume for the live check's applicant (the test identity, or the broad check's fake
    profile), made at run time: who it is, that it's a test, and the profile's jobs and schools."""
    personal = profile.get("personal") or {}
    address = personal.get("address") or {}
    place = ", ".join(str(p) for p in (address.get("city"), address.get("state")) if p)
    lines: list[tuple[int, str]] = [
        (18, f"{personal.get('first_name', FIRST_NAME)} {personal.get('last_name', LAST_NAME)}"),
        (10, " | ".join(str(p) for p in (personal.get("email"), personal.get("phone"), place) if p)),
        (10, ""),
        (12, RESUME_NOTE + "."),
        (10, "Made by the Job Desk plugin's live check (scripts/live_smoke.py). Nothing in it is true."),
        (10, ""),
        (13, "Experience"),
    ]
    for job in profile.get("work_history") or []:
        # (a job still held with no end date, the awkward profile's `current: true`: "to present", never "to None")
        end = job.get("end") or ("present" if job.get("current") is True else "")
        lines.append((10, f"{job.get('title')}, {job.get('company')}, {job.get('location')} "
                          f"({job.get('start')} to {end})"))
    lines += [(10, ""), (13, "Education")]
    for school in profile.get("education_history") or []:
        # (coursework, the awkward profile's `degree: ""`: no degree written, as none was earned)
        said = ", ".join(str(school[k]) for k in ("degree", "major", "school") if school.get(k) not in (None, ""))
        lines.append((10, f"{said} ({school.get('start')} to {school.get('end')})"))
    return tiny_pdf(lines)


def cover_letter_pdf(profile: Mapping[str, Any]) -> bytes:
    """A one-page cover letter for the live check's applicant, made at run time as resume_pdf is: who it is, and that
    it's a test with nothing in it true."""
    personal = profile.get("personal") or {}
    return tiny_pdf([
        (18, f"{personal.get('first_name', FIRST_NAME)} {personal.get('last_name', LAST_NAME)}"),
        (10, " | ".join(str(p) for p in (personal.get("email"), personal.get("phone")) if p)),
        (10, ""),
        (12, COVER_LETTER_NOTE + "."),
        (10, "Made by the Job Desk plugin's live check (scripts/live_smoke.py). Nothing in it is true."),
        (10, ""),
        (10, "It stands in for an applicant's own letter, so the check goes on past a form's \"upload your cover"),
        (10, "letter\". Please disregard it."),
    ])


def broad_email(company: str, env: Mapping[str, str]) -> str | None:
    """The broad check's email at this employer: the test identity's inbox (LIVE_TEST_EMAIL) where
    the site refuses example.com, when the run has it; else None, for its own example.com one."""
    email = (env.get(ENV_EMAIL) or "").strip()
    return email if email and company in INBOX_AT else None


def with_test_resume(profile: Mapping[str, Any], folder: Path) -> dict[str, Any]:
    """The profile with a test resume and a test cover letter of its own: resume_pdf and cover_letter_pdf written into
    `folder` (the run's home) and the profile's documents pointing to them, so the live check reaches the steps past a
    form's "upload your resume" (and a required "upload your cover letter": CSN's and NSU's Workday, run 144)."""
    resume, letter = folder / RESUME_NAME, folder / COVER_LETTER_NAME
    own: dict[str, Any] = copy.deepcopy(dict(profile))
    own["documents"] = {**(own.get("documents") or {}), "resume": str(resume), "cover_letter": str(letter)}
    resume.write_bytes(resume_pdf(own))
    letter.write_bytes(cover_letter_pdf(own))
    return own


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
    plus = None
    if email and "@" in email:  # and the inbox's own "+tag" addresses (--plus), however short its name
        local, _, domain = email.strip().partition("@")
        plus = re.compile(rf"{re.escape(local)}\+[a-z0-9-]+@{re.escape(domain)}", re.I)

    def mask(text: str) -> str:
        if plus is not None:
            text = plus.sub(EMAIL_MASK, text)
        return found.sub(lambda m: masks[m.group().lower()], text)
    return mask


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


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def drop_images(folder: Path) -> int:
    """A saved page's pictures removed: masking reads text, and a screenshot can show the test
    inbox's address in a filled box. The number removed."""
    gone = 0
    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            path.unlink()
            gone += 1
    return gone


# --everywhere: each list's employers with the role their jobs are looked for in (finance is paused; the Las Vegas
# list is IT jobs only). The nightly's accounts and search checks cover these lists, and no others
LIST_ROLES = {"semiconductor-az": "technician", "phoenix-metro": "hr", "las-vegas": "it"}
# Job systems whose emailed link names the employer's own site, so the desk tells one employer's mail from
# another's (pipeline.own_link): their employers may wait on mail at the same time. Every other system's
# employers wait one after another (one inbox; a code from the same sender is anyone's)
LINKED_SYSTEMS = {"workday"}
# A search's name for the job system its applications run on, where they differ (SuccessFactors' older sites,
# its Recruiting Marketing pages; asml.com's own search, whose Apply opens Workday)
SYSTEM_OF_SEARCH = {"sfclassic": "successfactors", "rmk": "successfactors", "sitecore": "workday"}


def everywhere_employers(lists: Mapping[str, Path] | None = None) -> list[dict[str, str]]:
    """--everywhere (the owner's call, Oct 11: "lets have practice runs make one"): the test identity at
    every employer with a search on LIST_ROLES' lists, each with its list's role and its search's system
    (the first key of its search: workday, eightfold, oracle ...), in the lists' order, each employer once."""
    from job_apply import search

    paths = lists if lists is not None else search.employer_lists()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, role in LIST_ROLES.items():
        data = yaml.safe_load(paths[name].read_text(encoding="utf-8")) or {}
        for c in data.get("companies") or []:
            if not isinstance(c, dict) or not isinstance(c.get("search"), dict) or not c["search"] or c["name"] in seen:
                continue
            seen.add(c["name"])
            out.append({"name": c["name"], "list": name, "role": role,
                        "system": SYSTEM_OF_SEARCH.get(str(next(iter(c["search"]))), str(next(iter(c["search"])))),
                        "why": "every employer on the lists (the owner's call, Oct 11)"})
    return out


NO_REASON = "no search block, and its entry says no why (no_search:)"


def skipped_employers(lists: Mapping[str, Path] | None = None) -> list[dict[str, str]]:
    """The employers on LIST_ROLES' lists that --everywhere can't apply at: those with no search block (the pipeline
    starts from a posting its search found), each with its list and why (its entry's `no_search:`, or NO_REASON when
    it gives none), in the lists' order, each once and never one another list searches. The live check says each of
    them (live_smoke.py's LIVE_SKIPPED lines), so none is left out without a word."""
    from job_apply import search

    paths = lists if lists is not None else search.employer_lists()
    searched = {e["name"] for e in everywhere_employers(paths)}
    out: list[dict[str, str]] = []
    for name in LIST_ROLES:
        data = yaml.safe_load(paths[name].read_text(encoding="utf-8")) or {}
        for c in data.get("companies") or []:
            if not isinstance(c, dict) or not c.get("name") or isinstance(c.get("search"), dict) and c["search"]:
                continue
            if c["name"] in searched or any(s["name"] == c["name"] for s in out):
                continue
            why = " ".join(str(c.get("no_search") or "").split())
            out.append({"name": c["name"], "list": name, "why": why or NO_REASON})
    return out


def arrange(employers: list[dict[str, str]], n: int) -> list[list[str]]:
    """The employers' names in n groups that run at once (--parallel n), as even as can be: a system's
    employers all in one group (one inbox: two of its codes at once could be taken for each other), placed
    first, biggest first; then LINKED_SYSTEMS', one by one into the shortest groups."""
    groups: list[list[str]] = [[] for _ in range(max(n, 1))]
    by_system: dict[str, list[str]] = {}
    for e in employers:
        by_system.setdefault(e["system"].lower(), []).append(e["name"])
    ordered = sorted(by_system.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for system, names in ordered:  # a code-sending system's employers first, whole, biggest first
        if system not in LINKED_SYSTEMS:
            min(groups, key=len).extend(names)
    for system, names in ordered:  # then the others one by one into the shortest groups
        if system in LINKED_SYSTEMS:
            for name in names:
                min(groups, key=len).append(name)
    return groups


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


INBOX_WINDOW = 15 * 60  # seconds back inbox_report looks
_EMAIL_ADDRESS = re.compile(r"[\w.+%'-]+@[\w-]+(?:\.[\w-]+)+")
_LONG_NUMBER = re.compile(r"\S*\d\S*")  # (any word with a digit: a code, lettered or short, or a job's number)


def inbox_report(env: Mapping[str, str], senders: Iterable[str] = (), paused_at: float | None = None,
                 now: float | None = None) -> dict[str, Any]:
    """What the test identity's inbox holds from the last INBOX_WINDOW seconds, its Spam folder too, for
    the live check to print when a run ends still waiting on an emailed code (live_smoke.py's LIVE_INBOX
    line): the sender's domain only, the subject (with any email address as "<email>" and any long number
    as "<digits>"), when it arrived (UTC, and in seconds after `paused_at`, when the run began waiting),
    whether the desk's sender check lets it through (`senders`: the domains this job's code may come
    from) and whether the desk reads a code from it (yes or no: never the code). Never the address, the
    password or a message's text. A fault in reading it is said in "error", not raised."""
    import email
    import email.policy
    import email.utils
    import imaplib
    import time

    from job_apply import mailbox

    now = time.time() if now is None else now
    allowed = {d.lower() for d in senders}
    report: dict[str, Any] = {"within_minutes": INBOX_WINDOW // 60, "senders_allowed": sorted(allowed), "messages": []}
    address, password, _ = credentials(env)
    try:
        box, _ = mailbox.connect(address, password)
    except mailbox.MailboxError as e:
        return {**report, "error": str(e)}
    seen: list[tuple[float, dict[str, Any]]] = []
    try:
        junk = mailbox.junk_folder(box)
        report["spam_folder"] = junk is not None
        day = time.strftime("%d-%b-%Y", time.gmtime(now - INBOX_WINDOW - 86400))
        for label, folder in (("inbox", "INBOX"), ("spam", mailbox.quoted(junk) if junk else None)):
            if folder is None:
                continue
            box.select(folder, readonly=True)
            ids = (box.search(None, "SINCE", day)[1][0] or b"").split()[-mailbox.NEWEST:]
            if not ids:
                continue
            for received, mid, head in mailbox.headers(box, ids, "FROM SUBJECT"):
                if received < now - INBOX_WINDOW:
                    continue
                sender = str(head.get("From") or "")
                shown = email.utils.parseaddr(sender)[1]
                subject = " ".join(str(head.get("Subject") or "").split())
                try:  # whether the desk reads a code from it
                    parts = box.fetch(mid, "(BODY.PEEK[])")[1]  # (bytes, as mailbox.py sends them: imaplib takes them)
                    raw = next((p for p in parts if isinstance(p, tuple)), None)
                    code = raw is not None and bool(mailbox.message_code(
                        email.message_from_bytes(raw[1], policy=email.policy.default)))
                except Exception:  # an odd message: said as one the desk can't read a code from
                    code = False
                seen.append((received, {
                    "folder": label, "from": shown.rsplit("@", 1)[1].lower() if "@" in shown else "",
                    "subject": _LONG_NUMBER.sub("<digits>", _EMAIL_ADDRESS.sub("<email>", subject))[:120],
                    "arrived": time.strftime("%H:%M:%S", time.gmtime(received)),
                    **({"after_pause_s": round(received - paused_at)} if paused_at else {}),
                    "allowed": mailbox.sender_allowed(sender, allowed), "code": code,
                }))
    except (imaplib.IMAP4.error, OSError) as e:
        report["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        mailbox.logout(box)
    report["messages"] = [m for _, m in sorted(seen, key=lambda s: s[0])]
    return report
