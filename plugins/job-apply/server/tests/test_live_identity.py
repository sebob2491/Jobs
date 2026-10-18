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
import time
from collections import Counter
from pathlib import Path

import pytest
import yaml
from test_mailbox import FakeImap, _message  # (the fake inbox the mail tests use)

from job_apply import config, mailbox, search
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


def test_the_broad_checks_fake_profile_gets_a_test_resume_too(tmp_path):
    """The broad check's fake profile had no resume, so about ten employers stopped at "upload your
    resume" (live run 19): it now gets the same kind of test resume, made for the run, in its home."""
    profile = li.with_test_resume(BASE, tmp_path)
    resume = Path(profile["documents"]["resume"])
    assert resume == tmp_path / li.RESUME_NAME and BASE["documents"]["resume"] is None  # (the base is left as it was)
    pdf = resume.read_bytes()
    assert count_pages(pdf) == 1 and li.RESUME_NOTE.encode("cp1252") in pdf
    assert b"(Testy McTestface)" in pdf and b"Equipment Technician, Example Fab" in pdf
    assert {k: v for k, v in profile.items() if k != "documents"} == {k: v for k, v in BASE.items() if k != "documents"}


def test_the_broad_check_gives_the_test_inbox_only_where_example_com_is_refused():
    """Onto Innovation's and Robert Half's Workday refuse the broad check's example.com address (live
    run 19): there, and only there, it gives the test identity's inbox (the owner's call, Oct 11)."""
    assert li.broad_email("Onto Innovation", ENV) == ENV[li.ENV_EMAIL].strip()
    assert li.broad_email("Robert Half", ENV) == ENV[li.ENV_EMAIL].strip()
    assert li.broad_email("KLA", ENV) is None
    assert li.broad_email("Onto Innovation", {}) is None  # a run without it keeps its own address
    assert li.broad_email("Onto Innovation", {li.ENV_EMAIL: "  "}) is None
    # each named as an employer list spells it
    listed = {c["name"] for path in search.employer_lists().values()
              for c in yaml.safe_load(path.read_text(encoding="utf-8"))["companies"]}
    assert li.INBOX_AT <= listed


def test_a_saved_pages_pictures_are_dropped(tmp_path):
    tmp_path = tmp_path / "snapshot"  # (beside the run's home)
    """Masking reads text, and a screenshot can show the test inbox's address in a filled box (review,
    Oct 11): the saved page keeps its text, masked, and loses its pictures."""
    (tmp_path / "frames").mkdir(parents=True)
    for name in ("screenshot.jpg", "frames/shot.PNG", "page.html", "snapshot.json"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert li.drop_images(tmp_path) == 2
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == \
        ["page.html", "snapshot.json"]


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
    (snap / "page.html").write_text(f'<input value="{EMAIL}"><p>{INBOX_PASSWORD}</p>', encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({"value": SITE_PASSWORD}), encoding="utf-8")
    (snap / "screenshot.jpg").write_bytes(b"\xff\xd8" + EMAIL.encode())  # (an image is left as it is)
    li.mask_files(tmp_path, mask)
    assert (snap / "page.html").read_text(encoding="utf-8") == f'<input value="{li.EMAIL_MASK}"><p>{li.PASSWORD_MASK}</p>'
    assert json.loads((snap / "snapshot.json").read_text(encoding="utf-8")) == {"value": li.PASSWORD_MASK}
    assert (snap / "screenshot.jpg").read_bytes().endswith(EMAIL.encode())


# --- its employers ------------------------------------------------------------------

SYSTEMS = {"Workday", "SuccessFactors", "iCIMS", "Oracle", "Eightfold", "UKG Pro", "Taleo", "BrassRing",
           "Cornerstone", "Paycom", "ApplicantStack", "Infor"}


def test_its_employers_are_a_few_per_job_system_from_the_plugins_lists(job_apply_home):
    employers = li.load_employers()
    names = [e["name"] for e in employers]
    assert len(names) == len(set(names))
    per_system = Counter(e["system"] for e in employers)
    assert set(per_system) == SYSTEMS and max(per_system.values()) <= 6, per_system
    assert {e["role"] for e in employers} <= {"technician", "hr", "finance"}  # (live_smoke.py's ROLES)
    lists = search.employer_lists()
    for e in employers:
        listed = yaml.safe_load(lists[e["list"]].read_text(encoding="utf-8"))["companies"]
        assert any(c["name"] == e["name"] and c.get("search") for c in listed), e  # (the pipeline runs on a search)
    # as live_smoke.py reads them: its employers' lists, as a person's companies.yaml names them
    (job_apply_home / "companies.yaml").write_text(yaml.safe_dump({"lists": sorted({e["list"] for e in employers})}), encoding="utf-8")
    assert set(names) <= {c["name"] for c in search.load_companies()}
    # with --parallel 2, two on the same system are in the same group (one inbox, one sender at a time)
    for system in [s for s, n in per_system.items() if n > 1]:
        assert len({i % 2 for i, e in enumerate(employers) if e["system"] == system}) == 1, system


def test_everywhere_is_every_employer_on_the_lists_with_its_role_and_system():
    """--everywhere (the owner's call, Oct 11: "lets have practice runs make one"): the test identity at every
    employer with a search on the lists in LIST_ROLES (the semiconductor list for technician jobs, the Phoenix list
    for HR jobs, the Las Vegas list for IT jobs: the design of Oct 18, A3), once each, with its search's system."""
    employers = li.everywhere_employers()
    names = [e["name"] for e in employers]
    assert len(names) == len(set(names)) and len(names) > 120, len(names)
    assert li.LIST_ROLES == {"semiconductor-az": "technician", "phoenix-metro": "hr", "las-vegas": "it"}
    assert set(li.LIST_ROLES) == {e["list"] for e in employers} == set(search.employer_lists())  # (every list)
    by = {e["name"]: e for e in employers}
    assert (by["KLA"]["role"], by["KLA"]["system"]) == ("technician", "workday")
    assert by["Axon"]["role"] == "hr" and by["Lam Research"]["system"] == "eightfold"
    assert (by["MGM Resorts"]["list"], by["MGM Resorts"]["role"], by["MGM Resorts"]["system"]) == \
        ("las-vegas", "it", "workday")
    assert by["Switch"]["role"] == "it" and by["Allegiant Air"]["system"] == "lever"
    assert by["Southwest Gas"]["list"] == "phoenix-metro"  # (on two lists: once, as the first list's)
    assert "TSMC Arizona" not in by  # (no search: it's searched by hand)
    assert all(e["why"] and e["system"] for e in employers)
    # every employer on the lists is applied at, or said with why it isn't: none left out without a word
    skipped = {e["name"]: e for e in li.skipped_employers()}
    listed = {c["name"] for name in li.LIST_ROLES
              for c in yaml.safe_load(search.employer_lists()[name].read_text(encoding="utf-8"))["companies"]}
    assert set(by) | set(skipped) == listed and not set(by) & set(skipped)
    assert {"TSMC Arizona", "Maricopa Community Colleges", "City of Phoenix", "ADP", "American Airlines",
            "Clark County", "Zappos"} <= set(skipped)
    assert all(e["why"] != li.NO_REASON for e in skipped.values()), skipped  # (each list entry says why)
    assert skipped["TSMC Arizona"]["list"] == "semiconductor-az" and "Cloudflare" in skipped["TSMC Arizona"]["why"]


def test_an_employer_with_no_search_is_said_with_its_why_or_that_it_gives_none(tmp_path):
    """skipped_employers: an entry with no search block is reported with its list and its `no_search:` why, or
    NO_REASON when it gives none; never one another list searches, nor one twice."""
    lists = {}
    for name, companies in (("semiconductor-az", [{"name": "Example Fab", "search": {"workday": "https://e.example"}},
                                                  {"name": "Example Gate", "no_search": "  a bot\n check  "}]),
                            ("phoenix-metro", [{"name": "Example Fab"}, {"name": "Example Quiet"},
                                               {"name": "Example Gate", "no_search": "again"}]),
                            ("las-vegas", [{"name": "Example Casino", "search": {}}])):
        lists[name] = tmp_path / f"{name}.yaml"
        lists[name].write_text(yaml.safe_dump({"companies": companies}), encoding="utf-8")
    assert li.skipped_employers(lists) == [
        {"name": "Example Gate", "list": "semiconductor-az", "why": "a bot check"},
        {"name": "Example Quiet", "list": "phoenix-metro", "why": li.NO_REASON},
        {"name": "Example Casino", "list": "las-vegas", "why": li.NO_REASON},  # (an empty search is none)
    ]
    assert [e["name"] for e in li.everywhere_employers(lists)] == ["Example Fab"]


def test_its_groups_keep_a_code_sending_systems_employers_together():
    """With --parallel n each group runs at once, all reading one inbox: two of one system's emailed codes at once
    could be taken for each other, so a system's employers wait one after another in one group. Workday's emailed
    links name the employer's own site, so its employers spread out. Every employer is in exactly one group."""
    employers = li.everywhere_employers()
    groups = li.arrange(employers, 4)
    assert sorted(n for g in groups for n in g) == sorted(e["name"] for e in employers)
    where = {n: i for i, g in enumerate(groups) for n in g}
    for system in {e["system"] for e in employers} - li.LINKED_SYSTEMS:
        assert len({where[e["name"]] for e in employers if e["system"] == system}) == 1, system
    workday = {where[e["name"]] for e in employers if e["system"] == "workday"}
    assert len(workday) == 4, workday  # spread over every group
    sizes = sorted(len(g) for g in groups)
    assert sizes[-1] - sizes[0] <= max(len([e for e in employers if e["system"] == s]) for s in
                                       {e["system"] for e in employers} - li.LINKED_SYSTEMS), sizes
    assert sorted(li.arrange(employers[:3], 1)[0]) == sorted(e["name"] for e in employers[:3])
    assert by_name(employers)["ASML"] == "workday"  # (asml.com's own search; its Apply opens Workday)


WORKFLOW = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "live-nightly.yml"


def _matrix():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["check"]["strategy"]["matrix"]["include"]


def test_the_nightlys_machines_run_every_group_once_with_one_inbox_a_system():
    """The nightly's accounts check runs on N machines at once, P groups each (--machine M/N, --parallel P): machine
    M's group i is arrange's group M + N*i (live_smoke.py's --shard), so every employer on the lists runs once, on
    one machine, and a code-sending system's employers stay in one group there (one inbox). Four machines since the
    Las Vegas list (the design of Oct 18, A3); the matrix stays under GitHub's 20 jobs at once."""
    rows = [r for r in _matrix() if r["check"] == "accounts"]
    machines, parallel = set(), set()
    for row in rows:
        args = row["args"].split()
        assert {"--pipeline", "--test-identity", "--everywhere"} <= set(args), args
        machines.add(args[args.index("--machine") + 1])
        parallel.add(int(args[args.index("--parallel") + 1]))
    n = len(rows)
    assert n == 4 and machines == {f"{m}/{n}" for m in range(n)} and len(parallel) == 1, (machines, parallel)
    assert len({r["slot"] for r in rows}) == n
    assert len(_matrix()) <= 20
    p = parallel.pop()
    employers = li.everywhere_employers()
    groups = li.arrange(employers, p * n)
    ran = [name for m in range(n) for i in range(p) for name in groups[m + n * i]]
    assert sorted(ran) == sorted(e["name"] for e in employers)  # (each once)
    where = {name: i for i, g in enumerate(groups) for name in g}
    for system in {e["system"] for e in employers} - li.LINKED_SYSTEMS:
        assert len({where[e["name"]] for e in employers if e["system"] == system}) == 1, system
    # no group longer than its biggest code-sending system, or a little over an even share
    biggest = max(Counter(e["system"] for e in employers if e["system"] not in li.LINKED_SYSTEMS).values())
    assert max(len(g) for g in groups) <= max(biggest, len(employers) // (p * n) + 2), [len(g) for g in groups]


def test_the_search_check_covers_the_lists_the_accounts_check_does():
    """The search check's --lists are LIST_ROLES' lists; no check looks for finance jobs (paused: the owner's call,
    Oct 10), and none applies at the Las Vegas list as anything but IT."""
    rows = {r.get("slot") or r["check"]: r for r in _matrix()}
    args = rows["search"]["args"].split()
    assert set(args[args.index("--lists") + 1].split(",")) == set(li.LIST_ROLES)
    assert "finance" not in rows and "finance" not in li.LIST_ROLES.values()
    for row in rows.values():
        said = row["args"].split()
        if "--role" in said:
            assert said[said.index("--role") + 1] != "finance", row
        if "--pipeline" in said and "--lists" in said and "las-vegas" in said[said.index("--lists") + 1]:
            assert said[said.index("--role") + 1] == "it", row


def by_name(employers):
    return {e["name"]: e["system"] for e in employers}


def test_an_employer_without_its_reasons_is_refused(tmp_path):
    path = tmp_path / "employers.yaml"
    path.write_text(yaml.safe_dump({"employers": [{"name": "KLA", "list": "semiconductor-az", "role": "technician"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="system and why"):
        li.load_employers(path)


# --- live_smoke.py --test-identity, short of a browser (conftest's smoke_script) ------------------------------

def test_live_smoke_refuses_to_start_without_the_test_identitys_values(tmp_path, smoke_script):
    code, said = smoke_script({li.ENV_EMAIL: EMAIL, li.ENV_SITE_PASSWORD: SITE_PASSWORD},
                              "--pipeline", "--test-identity")
    assert code == 2 and li.ENV_EMAIL_PASSWORD in said and "Nothing was run" in said, said
    assert EMAIL not in said and SITE_PASSWORD not in said
    assert not (tmp_path / "out").exists()
    code, said = smoke_script(ENV, "--test-identity")
    assert code == 2 and "goes with --pipeline" in said, said


def test_live_smoke_sets_up_the_test_identity_and_never_says_its_secrets(tmp_path, monkeypatch, smoke_script):
    """Everything up to the first employer (--companies matches none, so no browser starts): its
    profile and resume, its employers' lists, and its passwords kept out of every file."""
    code, said = smoke_script({**ENV, li.ENV_EMAIL: f" {EMAIL}\n"}, "--pipeline", "--test-identity",
                              "--fake-passwords", "--companies", "no-such-employer")
    assert code == 0, said
    assert f"Test identity: Jobdesk Test ({li.EMAIL_MASK}), at KLA (Workday)" in said
    assert not any(s.lower() in said.lower() for s in (*SECRETS, EMAIL)), said
    homes = [h for h in tmp_path.glob("job-apply-live-*") if (h / "companies.yaml").exists()]
    assert len(homes) == 1
    home = homes[0]
    profile = yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8"))
    assert (profile["personal"]["first_name"], profile["personal"]["last_name"]) == ("Jobdesk", "Test")
    assert profile["personal"]["email"] == EMAIL
    resume = Path(profile["documents"]["resume"])
    assert resume.parent == home and resume.read_bytes().startswith(b"%PDF")
    assert yaml.safe_load((home / "companies.yaml").read_text(encoding="utf-8")) == {"lists": ["phoenix-metro", "semiconductor-az"]}
    assert not (home / "secrets.yaml").exists()  # (its passwords are in the run's environment only)
    for path in [*home.rglob("*"), *(tmp_path / "out").rglob("*")]:
        if path.is_file():
            data = path.read_bytes()
            assert not any(s.encode() in data for s in SECRETS), path
    # the profile it wrote is the test identity's, as the desk reads it in that run
    for key, value in {**ENV, "JOB_APPLY_NEVER_SUBMIT": "1", li.IDENTITY_SWITCH: "1"}.items():
        monkeypatch.setenv(key, value)
    assert config.Profile.load(home / "profile.yaml").settings.may_manage_accounts


def test_live_smoke_everywhere_says_each_employer_it_cant_apply_at(tmp_path, smoke_script):
    """--everywhere on its first machine says each employer on its lists with no search, with its list and why, on a
    LIVE_SKIPPED line (once a run: never on another machine, nor in each group), and its home's lists are all three."""
    code, said = smoke_script(ENV, "--pipeline", "--test-identity", "--everywhere", "--machine", "0/4",
                              "--companies", "no-such-employer")
    assert code == 0, said
    lines = [json.loads(x[len("LIVE_SKIPPED "):]) for x in said.splitlines() if x.startswith("LIVE_SKIPPED ")]
    assert lines == [{"company": e["name"], "list": e["list"], "why": e["why"]} for e in li.skipped_employers()]
    by = {x["company"]: x for x in lines}
    assert {"TSMC Arizona", "City of Phoenix", "ADP", "Clark County"} <= set(by)
    assert by["Clark County"]["list"] == "las-vegas" and "NEOGOV" in by["Clark County"]["why"]
    home = next(h for h in tmp_path.glob("job-apply-live-*") if (h / "companies.yaml").exists())
    assert yaml.safe_load((home / "companies.yaml").read_text(encoding="utf-8")) == {"lists": sorted(li.LIST_ROLES)}
    code, said = smoke_script(ENV, "--pipeline", "--test-identity", "--everywhere", "--machine", "1/4",
                              "--companies", "no-such-employer")
    assert code == 0 and "LIVE_SKIPPED" not in said, said


# --- the inbox report: why the desk missed an emailed code -----------------------------

@pytest.fixture
def fake_inbox(monkeypatch):
    FakeImap.reset()
    monkeypatch.setattr(mailbox.imaplib, "IMAP4_SSL", FakeImap)
    return FakeImap


def test_the_inbox_report_says_who_mailed_and_when_but_never_the_address_or_the_text(fake_inbox):
    """A run that ends still waiting on an emailed code (Edward Jones' BrassRing, Oct 2026) can't be
    explained from outside: the report lists the last 15 minutes of the test inbox, its Spam folder
    too, as the sender's domain, the subject (with the address and long numbers hidden), the arrival
    time, whether the desk's sender check lets it through and whether it reads a code from it."""
    now = float(int(time.time()))
    fake_inbox.messages = [
        (now - 3000, _message("Old <old@elsewhere.example>", "Long ago", "Nothing")),  # outside the 15 minutes
        (now - 120, _message("Edward Jones <noreply@brassring.com>", f"Your Passcode for {EMAIL}",
                             "Your passcode is 482913. It expires in 10 minutes.")),
        (now - 60, _message("Deals <deals@shop.example>", "Job 2617841 is open", "Apply now")),
    ]
    fake_inbox.spam = [(now - 30, _message("BrassRing <donotreply@kenexa.com>", "Your Passcode",
                                     "Hello, your passcode is 111222"))]
    report = li.inbox_report(ENV, {"brassring.com", "edwardjones.com"}, paused_at=now - 125, now=now)
    said = json.dumps(report)
    for private in (EMAIL, EMAIL.split("@")[0], INBOX_PASSWORD, INBOX_PASSWORD.replace(" ", ""), "482913", "111222",
                    "noreply", "donotreply", "deals@", "2617841", "Apply now", "elsewhere"):
        assert private not in said, private
    assert [(m["folder"], m["from"], m["allowed"], m["code"]) for m in report["messages"]] == [
        ("inbox", "brassring.com", True, True), ("inbox", "shop.example", False, False),
        ("spam", "kenexa.com", False, True)]
    first, second, third = report["messages"]
    assert (first["subject"], second["subject"], third["subject"]) == ("Your Passcode for <email>",
                                                                       "Job <digits> is open", "Your Passcode")
    assert [m["after_pause_s"] for m in report["messages"]] == [5, 65, 95]
    assert first["arrived"] == time.strftime("%H:%M:%S", time.gmtime(now - 120))
    assert report["senders_allowed"] == ["brassring.com", "edwardjones.com"] and report["spam_folder"] is True
    assert "error" not in report
    calls = fake_inbox.instances[0].calls
    assert ("select", "INBOX", True) in calls and ("select", '"[Gmail]/Spam"', True) in calls  # read-only
    assert all("BODY.PEEK[" in c[2] for c in calls if c[0] == "fetch") and calls[-1] == ("logout",)


def test_the_inbox_report_says_a_fault_without_the_password(fake_inbox):
    fake_inbox.accept = False
    report = li.inbox_report(ENV, {"brassring.com"}, paused_at=None)
    assert "app password" in report["error"] and report["messages"] == []
    assert not any(s in json.dumps(report) for s in (EMAIL, *SECRETS))


def test_live_smoke_prints_the_inbox_report_as_one_line(tmp_path):
    """What live_smoke.py prints for an employer whose run ended waiting on an emailed code: a LIVE_INBOX
    line of JSON, with the job's allowed senders and what the desk said of reading the inbox."""
    driver = (
        "import asyncio, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import live_smoke\n"
        "class Applier:\n"
        "    mail_problem = 'it said something'\n"
        "    def _mail_senders(self, run): return {'brassring.com'}\n"
        "class Run:\n"
        "    paused_at = 1000.0\n"
        "    mail_looks, mail_last = 3, 'looked for a code: none (6 code box(es) on the page)'\n"
        "live_smoke.live_identity.inbox_report = lambda env, senders, paused_at: {\n"
        "    'senders_allowed': sorted(senders), 'paused': paused_at, 'messages': []}\n"
        "asyncio.run(live_smoke.report_inbox('Edward Jones', Applier(), Run()))\n")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("LIVE_TEST_", "JOB_APPLY_"))}
    out = subprocess.run([sys.executable, "-c", driver], env={**clean, "TMPDIR": str(tmp_path)}, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    lines = [line for line in out.stdout.splitlines() if line.startswith("LIVE_INBOX ")]
    assert len(lines) == 1, out.stdout
    assert json.loads(lines[0].removeprefix("LIVE_INBOX ")) == {
        "company": "Edward Jones", "senders_allowed": ["brassring.com"], "paused": 1000.0, "messages": [],
        "mail_problem": "it said something",
        "desk": {"looks": 3, "last": "looked for a code: none (6 code box(es) on the page)"}}


def test_the_plus_probe_applies_with_the_inboxs_own_plus_address_and_nothing_else(monkeypatch):
    """--plus (which sites take a "+tag" address?): the test identity applies as its inbox's own
    "name+employer@", which Gmail delivers to the one inbox. Only that inbox's plus addresses count as the
    test identity; the inbox is signed in to without the tag; and the plus address is masked like the rest."""
    plus = li.plus_address(EMAIL, "Ultra Clean Holdings (UCT)")
    assert plus == "jobdesk_test_not_real+ultra-clean-holdings-uct@gmail.com"
    for key, value in {**ENV, "JOB_APPLY_NEVER_SUBMIT": "1", li.IDENTITY_SWITCH: "1"}.items():
        monkeypatch.setenv(key, value)
    assert config.live_test_identity(plus) and config.live_test_identity(EMAIL.upper())
    for other in ("someone+jobdesk_test_not_real@gmail.com", "jobdesk_test_not_real+x@yahoo.com",
                  "jobdesk_test_not_realx+a@gmail.com", "jobdesk_test_not_real+a+b@gmail.com", "x" + EMAIL):
        assert not config.live_test_identity(other), other
    monkeypatch.delenv("JOB_APPLY_NEVER_SUBMIT")
    assert not config.live_test_identity(plus)  # (never without the hard switch)
    assert mailbox.login_address(plus) == EMAIL and mailbox.login_address("a+b@example.com") == "a+b@example.com"
    mask = li.masker(EMAIL, [])
    assert "jobdesk_test_not_real" not in mask(f"signed up as {plus} and {plus.upper()}").lower()
    short = li.masker("ab@gmail.com", [])  # (an inbox with a short name: its plus addresses too)
    assert short("as ab+kla@gmail.com, then AB+X@GMAIL.COM") == f"as {li.EMAIL_MASK}, then {li.EMAIL_MASK}"


def test_the_test_identitys_security_answers_are_a_listed_question_and_one_of_its_own(smoke_script):
    """--security-answers (the owner's call, Oct 14): the live check answers an account form's security questions on
    the desk's card for the test identity (Edward Jones' BrassRing), so it goes on into the application. The list's
    first real question, and an answer made from its site password: the same each run (the account keeps it), its own
    for each box, and never the password itself. Without --test-identity the switch is refused."""
    chooser = {"label": "Question 1", "kind": "listbox", "account": True,
               "options": ["Select question", "What is the name of your first school?", "Where is your favorite "
                           "vacation spot?"]}
    box = {"label": "Question 1 Answer", "kind": "text", "account": True}
    assert li.security_answer(chooser, SITE_PASSWORD) == "What is the name of your first school?"
    answer = li.security_answer(box, SITE_PASSWORD)
    assert answer == li.security_answer(box, SITE_PASSWORD) and answer != li.security_answer(box, "another password")
    assert answer != li.security_answer({**box, "label": "Question 2 Answer"}, SITE_PASSWORD)
    assert SITE_PASSWORD not in answer and len(answer) >= 12
    code, said = smoke_script(ENV, "--pipeline", "--security-answers")
    assert code == 2 and "--security-answers goes with --test-identity" in said, said
