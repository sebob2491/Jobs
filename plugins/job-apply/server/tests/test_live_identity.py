"""The live check's test identity (scripts/live_identity.py, and live_smoke.py --test-identity): a
clearly fake applicant with an inbox of its own, which the nightly "accounts" check applies as at one
or two employers per job system. Its email and passwords come from the environment, and are never
printed or written anywhere."""

import importlib.util
import io
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

from job_apply import config, search
from job_apply.render import count_pages

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_spec = importlib.util.spec_from_file_location("live_identity", SCRIPTS / "live_identity.py")
li = importlib.util.module_from_spec(_spec)
sys.modules["live_identity"] = li
_spec.loader.exec_module(li)

# (made up: a Gmail address can't have an underscore, so this one is no one's)
EMAIL = "jobdesk_test_not_real@gmail.com"
SITE_PASSWORD = 'Site"Pass\\word(1)!'  # quotes, a backslash and brackets: JSON- and PDF-escaped where they show
INBOX_PASSWORD = "abcd efgh ijkl mnop"  # as Google shows an app password
ENV = {li.ENV_EMAIL: EMAIL, li.ENV_EMAIL_PASSWORD: INBOX_PASSWORD, li.ENV_SITE_PASSWORD: SITE_PASSWORD}
SECRETS = (SITE_PASSWORD, INBOX_PASSWORD, INBOX_PASSWORD.replace(" ", ""), json.dumps(SITE_PASSWORD)[1:-1])

BASE = {  # the shape of live_smoke.py's fake profile
    "personal": {"first_name": "Testy", "last_name": "McTestface", "email": "testy@example.com", "phone": "480-555-0199",
                 "address": {"line1": "175 S Arizona Ave", "city": "Chandler", "state": "AZ", "postal_code": "85225",
                             "country": "United States"}},
    "documents": {"resume": None},
    "work_history": [{"title": "Equipment Technician", "company": "Example Fab", "location": "Chandler, AZ",
                      "start": "2021-03", "end": "present"}],
    "education_history": [{"school": "Arizona State University", "degree": "Bachelor's Degree",
                           "major": "Electrical Engineering", "start": 2016, "end": 2020}],
    "settings": {"submit_mode": "dry_run", "browser_channel": "chromium", "headless": True},
}


# --- the environment ---------------------------------------------------------------

def test_it_needs_all_three_values_and_never_says_them():
    for name in li.ENV_VARS:
        problem = li.env_problem({**ENV, name: "  "})
        assert problem and name in problem and "TEST_IDENTITY.md" in problem
        assert not any(value in problem for value in ENV.values())
    assert li.env_problem({}) is not None
    assert li.env_problem(ENV) is None


def test_its_inbox_must_be_one_the_desk_can_read():
    """The desk reads sign-up codes over IMAP from the inboxes it knows (Gmail's among them)."""
    problem = li.env_problem({**ENV, li.ENV_EMAIL: "jobdesk.test@example.com"})
    assert problem and "Gmail" in problem and "example.com" not in problem


def test_the_app_password_is_used_without_its_spaces_and_the_email_trimmed(monkeypatch):
    """Google shows an app password in four groups of four, and it may be pasted that way (or with a
    line break after it): the desk reads the inbox with the letters alone. The email is trimmed, and
    the site password loses only whitespace pasted around it."""
    env = {li.ENV_EMAIL: f"  {EMAIL}\n", li.ENV_EMAIL_PASSWORD: " abcd efgh\tijkl mnop\n",
           li.ENV_SITE_PASSWORD: f" {SITE_PASSWORD}\r\n"}
    email, inbox, site = li.credentials(env)
    assert (email, inbox, site) == (EMAIL, "abcdefghijklmnop", SITE_PASSWORD)
    for key, value in li.secret_env(site, inbox).items():
        monkeypatch.setenv(key, value)
    assert config.get_secret("email_password") == "abcdefghijklmnop"
    assert config.get_secret("workday_password") == SITE_PASSWORD
    mask = li.masker(email, [site, inbox, *(env[k] for k in li.ENV_VARS[1:])])  # (as live_smoke.py masks)
    assert mask(f"{env[li.ENV_EMAIL_PASSWORD]} abcdefghijklmnop") == f"{li.PASSWORD_MASK} {li.PASSWORD_MASK}"


def test_its_passwords_are_the_desks_saved_ones_for_every_job_system(monkeypatch):
    """One site password for every job system the desk signs in to, and the inbox's app password
    as email_password, as config.get_secret reads them (from the environment: no file is written)."""
    from job_apply.pipeline import PASSWORD_SITES

    env = li.secret_env(SITE_PASSWORD, INBOX_PASSWORD)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert len(env) == len(PASSWORD_SITES) + 1
    for ats in PASSWORD_SITES:
        assert config.get_secret(f"{ats}_password") == SITE_PASSWORD, ats
    assert config.get_secret("email_password") == INBOX_PASSWORD


# --- its profile and resume ---------------------------------------------------------

def test_the_profile_is_the_test_identity_in_practice_mode(monkeypatch, tmp_path):
    resume = tmp_path / li.RESUME_NAME
    before = json.dumps(BASE, sort_keys=True)
    profile = li.identity_profile(BASE, f" {EMAIL} ", resume)
    assert json.dumps(BASE, sort_keys=True) == before  # the fake profile itself is left as it was
    personal = profile["personal"]
    assert (personal["first_name"], personal["last_name"], personal["email"]) == ("Jobdesk", "Test", EMAIL)
    assert "555" in personal["phone"] and personal["address"] == BASE["personal"]["address"]
    assert profile["documents"]["resume"] == str(resume)
    assert profile["work_history"] == BASE["work_history"]
    assert profile["settings"] == {**BASE["settings"], "submit_mode": "dry_run", "manage_accounts": True,
                                   "accept_notices": True}
    # as the live check runs it: accounts and notices on, in practice mode, and nothing submitted
    for key, value in {**ENV, "JOB_APPLY_NEVER_SUBMIT": "1", li.IDENTITY_SWITCH: "1"}.items():
        monkeypatch.setenv(key, value)
    s = config.Profile(profile).settings
    assert (s.test_identity, s.may_manage_accounts, s.may_accept_notices, s.dry_run) == (True, True, True, True)
    assert not s.may_auto_submit("workday")


def test_the_test_resume_is_one_page_that_says_what_it_is(tmp_path):
    profile = li.identity_profile(BASE, EMAIL, tmp_path / li.RESUME_NAME)
    pdf = li.resume_pdf(profile)
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF") and count_pages(pdf) == 1
    assert li.RESUME_NOTE.encode("cp1252") in pdf  # (its dash too, in the PDF's own encoding)
    assert b"(Jobdesk Test)" in pdf and b"Equipment Technician, Example Fab" in pdf
    # every object where the cross-reference table says it is
    xref = int(pdf.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    offsets = [int(line[:10]) for line in pdf[xref:].split(b"\n")[3:9]]
    assert [pdf[o:].split(b"\n", 1)[0] for o in offsets] == [b"%d 0 obj" % n for n in range(1, 7)]
    assert li.tiny_pdf([(10, "a (bracketed) back\\slash")]).count(b"(a \\(bracketed\\) back\\\\slash) Tj") == 1


# --- masking ------------------------------------------------------------------------

def test_the_email_and_passwords_are_masked_however_they_come_out():
    mask = li.masker(EMAIL, [SITE_PASSWORD, INBOX_PASSWORD])
    record = json.dumps({"email": EMAIL.upper(), "typed": SITE_PASSWORD, "inbox": INBOX_PASSWORD,
                         "link": f"https://example.com/reset?email={EMAIL.replace('@', '%40')}",
                         "app": INBOX_PASSWORD.replace(" ", "")})
    masked = mask("LIVE_PIPELINE " + record)
    assert not any(s.lower() in masked.lower() for s in (*SECRETS, EMAIL, EMAIL.replace("@", "%40")))
    assert json.loads(masked.split(" ", 1)[1]) == {"email": li.EMAIL_MASK, "typed": li.PASSWORD_MASK,
                                                    "inbox": li.PASSWORD_MASK, "app": li.PASSWORD_MASK,
                                                    "link": f"https://example.com/reset?email={li.EMAIL_MASK}"}
    assert mask("nothing secret here") == "nothing secret here"


def test_what_the_run_prints_and_saves_is_masked(tmp_path):
    mask = li.masker(EMAIL, [SITE_PASSWORD, INBOX_PASSWORD])
    out = io.StringIO()
    stream = li.MaskedStream(out, mask)
    print(f"signed in as {EMAIL} with {SITE_PASSWORD}", file=stream, flush=True)
    assert out.getvalue() == f"signed in as {li.EMAIL_MASK} with {li.PASSWORD_MASK}\n"
    snap = tmp_path / "pipeline" / "kla"
    snap.mkdir(parents=True)
    (snap / "page.html").write_text(f'<input value="{EMAIL}"><p>{INBOX_PASSWORD}</p>')
    (snap / "snapshot.json").write_text(json.dumps({"value": SITE_PASSWORD}))
    (snap / "screenshot.jpg").write_bytes(b"\xff\xd8" + EMAIL.encode())  # (an image is left as it is)
    li.mask_files(tmp_path, mask)
    assert (snap / "page.html").read_text() == f'<input value="{li.EMAIL_MASK}"><p>{li.PASSWORD_MASK}</p>'
    assert json.loads((snap / "snapshot.json").read_text()) == {"value": li.PASSWORD_MASK}
    assert (snap / "screenshot.jpg").read_bytes().endswith(EMAIL.encode())


# --- its employers ------------------------------------------------------------------

SYSTEMS = {"Workday", "SuccessFactors", "iCIMS", "Oracle", "Eightfold", "UKG Pro", "Taleo", "BrassRing",
           "Cornerstone", "Paycom", "ApplicantStack", "Infor"}


def test_its_employers_are_one_or_two_per_job_system_from_the_plugins_lists(job_apply_home):
    employers = li.load_employers()
    names = [e["name"] for e in employers]
    assert len(names) == len(set(names))
    per_system = Counter(e["system"] for e in employers)
    assert set(per_system) == SYSTEMS and max(per_system.values()) <= 2, per_system
    assert {e["role"] for e in employers} <= {"technician", "hr", "finance"}  # (live_smoke.py's ROLES)
    lists = search.employer_lists()
    for e in employers:
        listed = yaml.safe_load(lists[e["list"]].read_text())["companies"]
        assert any(c["name"] == e["name"] and c.get("search") for c in listed), e  # (the pipeline runs on a search)
    # as live_smoke.py reads them: its employers' lists, as a person's companies.yaml names them
    (job_apply_home / "companies.yaml").write_text(yaml.safe_dump({"lists": sorted({e["list"] for e in employers})}))
    assert set(names) <= {c["name"] for c in search.load_companies()}
    # with --parallel 2, two on the same system are in the same group (one inbox, one sender at a time)
    for system in [s for s, n in per_system.items() if n > 1]:
        assert len({i % 2 for i, e in enumerate(employers) if e["system"] == system}) == 1, system


def test_an_employer_without_its_reasons_is_refused(tmp_path):
    path = tmp_path / "employers.yaml"
    path.write_text(yaml.safe_dump({"employers": [{"name": "KLA", "list": "semiconductor-az", "role": "technician"}]}))
    with pytest.raises(ValueError, match="system and why"):
        li.load_employers(path)


# --- live_smoke.py --test-identity, short of a browser ------------------------------

def _smoke(tmp_path, env, *args):
    """live_smoke.py in a process of its own, with only `env` of the test identity's values (its
    home goes in tmp_path)."""
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("LIVE_TEST_", "JOB_APPLY_SECRET_", "JOB_APPLY_LIVE_TEST_"))}
    out = subprocess.run([sys.executable, str(SCRIPTS / "live_smoke.py"), *args, "--out", str(tmp_path / "out")],
                         env={**clean, **env, "TMPDIR": str(tmp_path)}, capture_output=True, text=True, timeout=120)
    return out.returncode, out.stdout + out.stderr


def test_live_smoke_refuses_to_start_without_the_test_identitys_values(tmp_path):
    code, said = _smoke(tmp_path, {li.ENV_EMAIL: EMAIL, li.ENV_SITE_PASSWORD: SITE_PASSWORD},
                        "--pipeline", "--test-identity")
    assert code == 2 and li.ENV_EMAIL_PASSWORD in said and "Nothing was run" in said, said
    assert EMAIL not in said and SITE_PASSWORD not in said
    assert not (tmp_path / "out").exists()
    code, said = _smoke(tmp_path, ENV, "--test-identity")
    assert code == 2 and "goes with --pipeline" in said, said


def test_live_smoke_sets_up_the_test_identity_and_never_says_its_secrets(tmp_path, monkeypatch):
    """Everything up to the first employer (--companies matches none, so no browser starts): its
    profile and resume, its employers' lists, and its passwords kept out of every file."""
    code, said = _smoke(tmp_path, {**ENV, li.ENV_EMAIL: f" {EMAIL}\n"}, "--pipeline", "--test-identity",
                        "--fake-passwords", "--companies", "no-such-employer")
    assert code == 0, said
    assert f"Test identity: Jobdesk Test ({li.EMAIL_MASK}), at KLA (Workday)" in said
    assert not any(s.lower() in said.lower() for s in (*SECRETS, EMAIL)), said
    homes = [h for h in tmp_path.glob("job-apply-live-*") if (h / "companies.yaml").exists()]
    assert len(homes) == 1
    home = homes[0]
    profile = yaml.safe_load((home / "profile.yaml").read_text())
    assert (profile["personal"]["first_name"], profile["personal"]["last_name"]) == ("Jobdesk", "Test")
    assert profile["personal"]["email"] == EMAIL
    resume = Path(profile["documents"]["resume"])
    assert resume.parent == home and resume.read_bytes().startswith(b"%PDF")
    assert yaml.safe_load((home / "companies.yaml").read_text()) == {"lists": ["phoenix-metro", "semiconductor-az"]}
    assert not (home / "secrets.yaml").exists()  # (its passwords are in the run's environment only)
    for path in [*home.rglob("*"), *(tmp_path / "out").rglob("*")]:
        if path.is_file():
            data = path.read_bytes()
            assert not any(s.encode() in data for s in SECRETS), path
    # the profile it wrote is the test identity's, as the desk reads it in that run
    for key, value in {**ENV, "JOB_APPLY_NEVER_SUBMIT": "1", li.IDENTITY_SWITCH: "1"}.items():
        monkeypatch.setenv(key, value)
    assert config.Profile.load(home / "profile.yaml").settings.may_manage_accounts
