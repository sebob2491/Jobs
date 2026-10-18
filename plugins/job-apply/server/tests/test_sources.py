"""The server's own sources: what every file of them must keep to."""

import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
ROOTS = ("src", "scripts", "tests")
# owners whose .open() isn't a file's: the browser's and the desk's own, os.open (a file descriptor, binary)
NOT_A_FILE = {"os", "webbrowser", "self", "srv", "browser", "page", "b", "desk", "applier", "server"}


def _text_calls_without_encoding(tree: ast.AST):
    """Path.read_text / write_text, open() and Path.open() in text mode, with no encoding named."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
        if name in ("read_text", "write_text") and isinstance(f, ast.Attribute):
            kind = name
        elif name == "open" and isinstance(f, ast.Name):
            kind = "open"
        elif name == "open" and isinstance(f, ast.Attribute):
            owner = f.value
            oname = owner.attr if isinstance(owner, ast.Attribute) else owner.id if isinstance(owner, ast.Name) else ""
            if oname in NOT_A_FILE:
                continue
            kind = "open"
        else:
            continue
        if any(k.arg == "encoding" for k in node.keywords):
            continue
        if kind == "open":
            mode = None
            position = 1 if isinstance(f, ast.Name) else 0  # open(path, mode) / path.open(mode)
            if len(node.args) > position and isinstance(node.args[position], ast.Constant):
                mode = node.args[position].value
            for k in node.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = k.value.value
            if isinstance(mode, str) and "b" in mode:
                continue
        yield kind, node.lineno


def test_every_text_file_is_read_and_written_as_utf_8():
    """Windows reads and writes text as cp1252 unless told otherwise (the owner's platform, where the desk runs):
    a profile's answer with a character outside it fails to save, and a file written there reads back wrong
    on another computer. Every text read or write in the server's sources, scripts and tests names its
    encoding (the whole-program review of Oct 15: 226 of them didn't, in the scripts and tests)."""
    missing = []
    for root in ROOTS:
        for path in sorted((SERVER / root).rglob("*.py")):
            if "fixtures" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            missing += [f"{path.relative_to(SERVER).as_posix()}:{line} ({kind})"
                        for kind, line in _text_calls_without_encoding(tree)]
    assert not missing, "text read or written with no encoding (add encoding=\"utf-8\"):\n" + "\n".join(missing)


# --- the captured pages: nothing personal, nothing dead ------------------------------------------------------

import re  # noqa: E402
from urllib.parse import urlparse  # noqa: E402

import yaml  # noqa: E402

FIXTURES = SERVER / "tests" / "fixtures"
DATA = SERVER.parent / "data"
_EMAIL = re.compile(r"[a-z0-9._%+-]+@([a-z0-9.-]+\.[a-z]{2,})", re.I)
PUBLIC_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com", "live.com",
               "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "mail.com",
               "gmx.com", "yandex.com", "zoho.com", "cox.net", "comcast.net", "att.net", "verizon.net"}
# the job systems' own addresses (a site's noreply@) are no one's
JOB_SYSTEMS = {"myworkday.com", "myworkdayjobs.com", "workday.com", "icims.com", "successfactors.com", "sap.com",
               "eightfold.ai", "oraclecloud.com", "oracle.com", "brassring.com", "greenhouse.io", "lever.co",
               "smartrecruiters.com", "paycom.com", "ultipro.com", "ukg.com", "infor.com", "csod.com",
               "cornerstoneondemand.com", "phenom.com", "phenompeople.com", "taleo.net", "applicantstack.com",
               "jobvite.com", "avature.net", "adp.com", "paylocity.com", "linkedin.com", "indeed.com"}
# the fake profile's and the test identity's details (scripts/live_smoke.py FAKE_PROFILE, live_identity.py), which
# the scrubber replaces on capture and a page could echo back
PLANTED = ("McTestface", "480-555-0199", "175 S Arizona Ave", "example-test-profile", "Jobdesk Test")
# Anyone's phone, SSN or token the scrubber takes out (fixtures.scrub_others), should a page come in without it: a phone
# with separators or a click-to-call link's digits (the reviews of Oct 15 and 0.3.189)
_PHONE = re.compile(r"(?<![\w-])(?:\+?1[-. ]?)?\(?(\d{3})\)?[-. ](\d{3})[-. ]\d{4}(?![\w-])"
                    r"|\btel:\s*\+?1?[-. (]*(\d{3})\)?[-. ]*(\d{3})[-. ]*\d{4}\b", re.I)
_SSN = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
_JWT = re.compile(r"\beyJ[\w-]{4,}\.[\w-]{4,}")
# toll-free lines, the 555 numbers made up for tests and a form's 000 placeholder; an employer's published HR line
_NOBODYS_AREA = {"800", "888", "877", "866", "855", "844", "833", "000"}
_PUBLIC_LINES = {"510-572-4477"}  # Lam Research's HR support, on its captured pages
# Ten digits with no separators after a word that says they're a phone's (fixtures._PHONE_SAID: the review of Oct 18)
_PHONE_SAID = re.compile(r"\b(?:phone|mobile|cell|telephone|tel|fax|call|text|sms|whatsapp)\b"
                         r"(?:\W{1,3}(?:number|no|us|me|at|on|is)\b)*\W{1,4}?(?:\+?1)?(\d{3})(\d{3})\d{4}(?!\d)", re.I)
# A box with a value on a captured page: the scrubber empties every box but a choice's (fixtures.clean_html), so one
# that's kept its value skipped the scrubber (the review of Oct 18). Only on the captured pages (live/): the mock sites'
# pages are written by hand, some with their boxes filled on purpose, with the tests' fake profile (conftest.PROFILE)
_INPUT = re.compile(r"<input\b[^>]*>", re.I)
_ATTR = re.compile(r"""([\w:-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+)""")
_TYPED_BOX = {"text", "email", "tel", "hidden"}
# Someone greeted by name ("Welcome, Jordan", "Signed in as Jordan Quill"): the scrubber takes out only the profile's
_GREETING = re.compile(r"\b(?:Welcome(?: back)?|Hi|Hello|Signed in as|Logged in as)[,!:]?\s+([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+)?)")
# what a page greets that's no one: the scrubber's stand-in, a page's own words ("Welcome Back" on onsemi's Oracle
# page, "Hello World" in a UI library's strings on Ebara's)
_NOBODY = {"REDACTED", "Back", "World", "To", "There", "Everyone", "Again", "Aboard", "Guest", "Candidate", "Applicant",
           "User", "Friend"}
_FAKE_NAME = {"Sam", "Rivera"}  # the tests' fake profile (conftest.PROFILE), on a mock site's page


def _employer_domains() -> set[str]:
    """The listed employers' own domains: their careers sites' (the registrable part) and their mail_domains. An
    address at one in a captured page is the employer's own (an accommodation inbox), not a person's."""
    domains = set()
    for path in [DATA / "companies.yaml", *sorted((DATA / "lists").glob("*.yaml"))]:
        for company in yaml.safe_load(path.read_text(encoding="utf-8")).get("companies") or []:
            host = (urlparse(company.get("careers_url") or "").hostname or "").lower()
            if host.count(".") >= 1:
                domains.add(".".join(host.split(".")[-2:]))
            domains.update(str(d).lower() for d in company.get("mail_domains") or [])
    return domains


def test_captured_pages_hold_no_personal_data():
    """The repo is public: a page captured from a real site goes through the scrubber before it becomes a fixture
    (python -m job_apply.fixtures), which replaces the profile's details. This is the gate for what the scrubber
    can't know: no address at a public mail provider or at a domain that isn't a listed employer's or a job
    system's (an employer's own accommodation inbox is fine), and none of the fake profile's or the test
    identity's details (the whole-program review of Oct 15 asked for it); and nobody's phone (one with separators or a
    click-to-call link's digits), SSN-shaped number or token (the reviews of Oct 15 and 0.3.189); nor ten bare digits
    after a phone word, anyone greeted by name, or a captured page's box that kept its value (the review of Oct 18)."""
    allowed = _employer_domains() | JOB_SYSTEMS | {"example.com", "example.org", "example.net"}
    problems = []
    for path in sorted(FIXTURES.rglob("*")):
        if not path.is_file() or path.suffix not in (".html", ".json", ".txt", ".md", ".yaml"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        shown = path.relative_to(SERVER).as_posix()
        for m in _EMAIL.finditer(text):
            domain = m.group(1).lower()
            root = ".".join(domain.split(".")[-2:])
            if domain in PUBLIC_MAIL or root in PUBLIC_MAIL:
                problems.append(f"{shown}: an address at {domain}")
            elif domain not in allowed and root not in allowed:
                problems.append(f"{shown}: an address at {domain}, neither a listed employer's nor a job system's")
        problems += [f"{shown}: {value!r}" for value in PLANTED if value.lower() in text.lower()]
        for m in _PHONE.finditer(text):
            area, exchange = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            if exchange != "555" and area not in _NOBODYS_AREA \
                    and re.sub(r"^(?:tel:\s*)?\+?1[-. ]?", "", m.group(0), flags=re.I) not in _PUBLIC_LINES:
                problems.append(f"{shown}: a phone number {m.group(0)!r}")
        problems += [f"{shown}: a phone number {m.group(0)!r}" for m in _PHONE_SAID.finditer(text)
                     if m.group(2) != "555" and m.group(1) not in _NOBODYS_AREA]
        problems += [f"{shown}: an SSN's shape {v!r}" for v in _SSN.findall(text)]
        problems += [f"{shown}: a token {v[:12]!r}…" for v in _JWT.findall(text)]
        for m in _GREETING.finditer(text):
            words = m.group(1).split()
            if words[0] not in _NOBODY and not set(words) <= _FAKE_NAME:
                problems.append(f"{shown}: someone greeted by name {m.group(0)!r}")
        if path.relative_to(FIXTURES).parts[0] == "live":
            for m in _INPUT.finditer(text):
                attrs = {k.lower(): v.strip("\"'") for k, v in _ATTR.findall(m.group(0)[len("<input"):])}
                if attrs.get("type", "text").lower() in _TYPED_BOX and attrs.get("value", "").strip():
                    problems.append(f"{shown}: a box that kept its value {m.group(0)[:120]!r}")
    assert not problems, "personal data in the fixtures:\n" + "\n".join(sorted(set(problems)))


def test_every_fixture_is_used():
    """A captured page or a mock form nobody tests is weight the public repo carries for nothing: each file under
    tests/fixtures is named in a test, is a page test_live_fixtures.py loads by its .expect.json, or is linked
    from another fixture (a frame of a captured page, a page a mock site leads to)."""
    tests_text = "\n".join(p.read_text(encoding="utf-8") for p in sorted((SERVER / "tests").glob("*.py")))
    files = [p for p in sorted(FIXTURES.rglob("*")) if p.is_file()]
    pages = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in files if p.suffix == ".html"}

    def used(path: Path) -> bool:
        if path.name in tests_text or path.stem in tests_text:
            return True
        if path.name.endswith(".expect.json") or path.with_name(path.stem + ".expect.json").exists():
            return True
        return any(path.name in text for name, text in pages.items() if name != path.name)

    dead = [p.relative_to(SERVER).as_posix() for p in files if not used(p)]
    assert not dead, "fixtures no test uses (delete them, or test them):\n" + "\n".join(dead)
