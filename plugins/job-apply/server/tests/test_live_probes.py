"""The live check's weekly probes (scripts/live_probes.py, and live_smoke.py's --derived-password and --tailored-resume):
the derived password and its rules, its masking, each reset pass's steps read from a run's record, the tailored
resume and the review page's check, the report and nightly_summary.py --probes, and the workflow that runs them. No
browser and no live site: records shaped like live_smoke.py's, from the desk's own log lines; a made-up identity."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from job_apply import config
from job_apply.autofill import tailored_document

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lp = _load("live_probes")
ns = _load("nightly_summary")
li = lp.live_identity
PIPELINE_SOURCE = Path(__file__).resolve().parents[1] / "src" / "job_apply" / "pipeline.py"
WORKFLOW = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "live-nightly.yml"

# (made up: a Gmail address can't have an underscore, so this one is no one's; the passwords are no one's either)
EMAIL = "jobdesk_test_not_real@gmail.com"
SITE = "Probe-Site-Pass-2026#x"
INBOX = "abcd efgh ijkl mnop"
ENV = {li.ENV_EMAIL: EMAIL, li.ENV_EMAIL_PASSWORD: INBOX, li.ENV_SITE_PASSWORD: SITE}
DERIVED = lp.derived_password(SITE)


# --- the derived password -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("site", [SITE, "Abcdefgh1234567!", "x" * 30 + "A1!", "L0ng!" * 14, "S" * 56 + "aa1" + lp.DERIVED_SUFFIX])
def test_the_derived_password_meets_workdays_rules_and_is_never_the_real_one(site):
    """Pass 1's saved password: the real one with a fixed suffix (a capital, a small letter, a digit and a symbol), the
    same every run, never the real one, within the longest a site takes, and meeting Workday's rules wherever the real
    one does (TEST_IDENTITY.md's: 16 characters or more, every kind, no space, quote or backslash)."""
    derived = lp.derived_password(site)
    assert derived != site and derived == lp.derived_password(f"  {site}\n")  # (as credentials() trims it)
    assert len(derived) <= lp.PASSWORD_MAX and lp.password_problems(derived) == []
    assert derived.endswith((lp.DERIVED_SUFFIX, lp.OTHER_SUFFIX))
    kept = site[:lp.PASSWORD_MAX - len(lp.DERIVED_SUFFIX)]
    assert derived.startswith(kept[:20])  # (derived from the real one: its start kept)
    if len(site) + len(lp.DERIVED_SUFFIX) <= lp.PASSWORD_MAX:
        assert derived == site + lp.DERIVED_SUFFIX


def test_a_site_password_at_the_longest_that_ends_in_the_suffix_gets_the_other_one():
    site = "A" * (lp.PASSWORD_MAX - 8) + "b1" + "xy" + lp.DERIVED_SUFFIX
    assert len(site) == lp.PASSWORD_MAX
    assert lp.derived_password(site).endswith(lp.OTHER_SUFFIX) and lp.derived_password(site) != site


def test_password_rules_say_what_is_wrong_and_never_the_password():
    assert lp.password_problems("Abcdefgh1234567!") == []
    said = lp.password_problems('sh"1 a')
    assert said == ["shorter than 8 characters", "no capital letter",
                    "a space, a quote or a backslash (some job sites refuse those)"]
    assert lp.password_problems("abcdefghijklmnop") == ["no capital letter", "no digit", "no symbol"]
    assert lp.password_problems("A1!" + "b" * 70) == [f"longer than {lp.PASSWORD_MAX} characters"]
    assert not any('sh"1 a' in s for s in said)
    for empty in ("", "   "):
        with pytest.raises(ValueError):
            lp.derived_password(empty)


def test_the_derived_password_is_workdays_alone(monkeypatch):
    """Only Workday's saved password changes in pass 1 (the probe's tenants are Workday's): every other system's stays
    the real one, and the inbox's app password is untouched."""
    env = {**li.secret_env(SITE, INBOX.replace(" ", "")), **lp.derived_env(SITE)}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert config.get_secret("workday_password") == DERIVED
    assert config.get_secret("successfactors_password") == SITE == config.get_secret("icims_password")
    assert config.get_secret("email_password") == INBOX.replace(" ", "")
    assert list(lp.derived_env(SITE)) == ["JOB_APPLY_SECRET_WORKDAY_PASSWORD"]


# --- masking ------------------------------------------------------------------------------------------------------

def test_the_derived_password_is_masked_whole_however_it_shows():
    """The derived password holds the real one: masked whole (never "***" and the suffix after it), in a log line,
    JSON-escaped and in a link, with the real one, the inbox's app password and the address."""
    mask = li.masker(*lp.secrets_of(ENV))
    for shown in (DERIVED, json.dumps(DERIVED)[1:-1], DERIVED.upper()):
        assert mask(f"typed {shown} in") == f"typed {li.PASSWORD_MASK} in", shown
    said = mask(f"{EMAIL} {SITE} {INBOX} {INBOX.replace(' ', '')} https://x.example/?p={DERIVED.replace('#', '%23').replace('!', '%21')}")
    assert lp.DERIVED_SUFFIX not in said and "Rp7" not in said and EMAIL not in said and SITE not in said, said
    assert said.count(li.PASSWORD_MASK) == 4 and li.EMAIL_MASK in said


def test_the_report_and_the_results_are_written_masked(tmp_path, capsys):
    """Whatever a run's record says (a password typed into a reason, say), probes.json, report.md and the LIVE_WEEKLY
    lines hold no secret."""
    leaky = _rec(SIGN_IN + RESET + APPLICATION, reason=f"Filled and waiting ({DERIVED}, {EMAIL}, {SITE})")
    results = {"at": "now", "reset": [lp.reset_result(lp.RESET_TENANTS[0], leaky, leaky, True)], "tailored": []}
    lp.write_report(tmp_path, results, li.masker(*lp.secrets_of(ENV)))
    written = [(tmp_path / "probes.json").read_text(encoding="utf-8"), (tmp_path / "report.md").read_text(encoding="utf-8"),
               capsys.readouterr().out]
    for text in written:
        assert not any(s in text for s in (DERIVED, SITE, EMAIL, json.dumps(DERIVED)[1:-1])), text
    assert li.PASSWORD_MASK in written[0] and li.EMAIL_MASK in written[0]


# --- one reset pass, from its run's record -------------------------------------------------------------------------

def _rec(log, status="ready", need=None, reason="Filled and waiting on the review page. Check it in the browser, "
         "then press Submit.", company="KLA", **extra):
    """A run's record as live_smoke.py's check_pipeline writes it (the last round holds the job's whole log)."""
    rounds = [{"status": "needs_you", "need": "email_code", "reason": "Workday is emailing you a password reset",
               "log": log[:3]}, {"status": status, "need": need, "reason": reason, "log": log}]
    return {"company": company, "rounds": rounds, **extra}


# (the desk's own words: pipeline.py's log lines; test_the_probe_reads_the_desks_own_words checks they still are)
SIGN_IN = ["opened https://kla.wd1.myworkdayjobs.com/Search/job/Phoenix-AZ/Customer-Service-Engineer_1",
           "declined cookies (“Decline”)", "clicked “Apply”", "clicked “Apply Manually”",
           "pressed “Sign In” with your saved password"]
RESET = ["your saved password didn't sign in on Workday, so I asked it to email a password reset",
         "opened the password reset link from your email (sent from otp.workday.com)",
         "set your saved password as Workday's new one", "pressed “Sign In” with your saved password"]
APPLICATION = ["Workday said something went wrong and asked for the page to be refreshed, so I reloaded it",
               "filled 1 field(s) on “My Information”", "clicked “Save and Continue”",
               "ticked “Yes, I have read and consent to the terms and conditions.” for you (settings.accept_notices)",
               "reached the review page"]
EVERY = dict.fromkeys(lp.STEPS, True)


def _steps(rec):
    s = lp.reset_steps(rec)
    return {k: s[k] for k in lp.STEPS}


def test_a_whole_reset_is_every_step():
    steps = lp.reset_steps(_rec(SIGN_IN + RESET + APPLICATION))
    assert {k: steps[k] for k in lp.STEPS} == EVERY
    assert (steps["how_far"], steps["ended"], steps["stopped"], steps["crashed"]) == ("review page", "ready", "", False)
    assert lp.pass_two_needed(steps)


def test_a_reset_that_signs_in_and_stops_on_the_application_reached_it():
    rec = _rec(SIGN_IN + RESET + APPLICATION[:3], status="needs_you", need="questions",
               reason="1 question your profile doesn't answer")
    steps = lp.reset_steps(rec)
    assert {k: steps[k] for k in lp.STEPS} == EVERY and steps["how_far"] == "application"


def test_a_site_that_takes_the_saved_password_refuses_nothing_and_resets_nothing():
    """Pass 1 at a site already on the derived password (an earlier probe's pass 2 didn't finish): signed in, nothing
    reset; pass 2 must run to set the real one back."""
    steps = lp.reset_steps(_rec(SIGN_IN + APPLICATION))
    assert _steps(_rec(SIGN_IN + APPLICATION)) == {**dict.fromkeys(lp.STEPS, False), "signed_in": True,
                                                   "reached_application": True}
    assert lp.pass_two_needed(steps)


def test_a_new_password_the_site_wont_take_is_set_no_further():
    reason = ("I put your saved password in Workday's new-password boxes, and it didn't take it as the new one. It says "
              "“Your password cannot be the same as a previous password.” Set it there")
    steps = lp.reset_steps(_rec(SIGN_IN + RESET[:2], status="needs_you", need="sign_in", reason=reason))
    assert _steps(_rec(SIGN_IN + RESET[:2], status="needs_you", need="sign_in", reason=reason)) == {
        **dict.fromkeys(lp.STEPS, False), "refused": True, "reset_asked": True, "email_read": True}
    assert lp.pass_two_needed(steps) and "previous password" in steps["said"]


def test_a_sign_in_still_refused_after_the_reset_isnt_signed_in():
    rec = _rec(SIGN_IN + RESET, status="needs_you", need="sign_in",
               reason="Your saved password didn't sign in on Workday (it says “Wrong email address or password”).")
    assert _steps(rec) == {**EVERY, "signed_in": False, "reached_application": False}


@pytest.mark.parametrize("log,reason,need,stopped", [
    # the reset's page has a picture code: the desk fills the email and stops (no new account to go back for)
    (SIGN_IN, "I opened Workday's password reset for your saved password, which didn't sign in, and filled in your "
              "email. Solve its picture code and press its button; the desk watches your inbox", "bot_check", "a CAPTCHA"),
    # or goes back to make an account instead, as it does for a first application there
    (SIGN_IN + ["your saved password didn't sign in on Workday, and I opened its password reset, which has a CAPTCHA "
                "(yours to solve), so I went back to make an account there instead"],
     "I filled in Workday's Create Account form with your details and saved password. Its CAPTCHA is yours to solve",
     "sign_in", "a CAPTCHA"),
    (SIGN_IN + RESET[:1], "It also asks for security questions and their answers: those are yours to choose", "sign_in",
     "a security question"),
    (SIGN_IN + RESET, "The site is checking that you're a person (a bot check or CAPTCHA). Pass it in the browser window",
     "bot_check", "a bot check")])
def test_a_captcha_or_a_security_question_on_the_way_is_said(log, reason, need, stopped):
    """The desk never touches one (the person's, by its rules), and the live check never answers a security question
    for a probe (no --security-answers): the pass stops there, and says what stopped it."""
    steps = lp.reset_steps(_rec(log, status="needs_you", need=need, reason=reason))
    assert steps["stopped"] == stopped and steps["refused"] and not steps["signed_in"]


def test_pass_two_is_skipped_only_where_the_sites_password_was_never_changed():
    never = lp.reset_steps(_rec(SIGN_IN, status="needs_you", need="sign_in", reason=(
        "Your saved password didn't sign in on Workday, and I couldn't find its way to a password reset. Sign in")))
    assert never["refused"] and not lp.pass_two_needed(never)
    for rec in (None, {"company": "KLA", "crash": "TimeoutError: still running"}, _rec(SIGN_IN + RESET[:2])):
        assert lp.pass_two_needed(lp.reset_steps(rec)), rec
    no_mail = lp.reset_steps(_rec(SIGN_IN + RESET[:1] + [
        "no password reset email came from Workday in 3 minutes, so I went back to make an account there",
        "pressed “Create Account” to create your account on Workday with your saved password"],
        status="needs_you", need="sign_in", reason="Your saved password didn't sign in on Workday, before or after I "
                                                   "pressed its Create Account."))
    assert no_mail["account_pressed"] and lp.pass_two_needed(no_mail)  # (a Create Account pressed: pass 2, to be sure)
    nothing = lp.reset_steps(_rec(["opened https://kla.wd1.myworkdayjobs.com/x"], status="needs_you", need="bot_check",
                                  reason="The site is checking that you're a person"))
    assert lp.pass_two_needed(nothing)  # (never got to the sign-in: state unknown, pass 2 is the nightly's own run)


def test_a_tenant_passes_with_every_step_of_both_passes():
    whole = _rec(SIGN_IN + RESET + APPLICATION)
    result = lp.reset_result(lp.RESET_TENANTS[0], whole, whole, True)
    assert result["ok"] and result["left_on"] == "real" and not result["warning"] and not result["notes"]
    assert [p["saved"] for p in result["passes"]] == ["derived", "real"]


def test_a_failed_pass_two_says_plainly_where_the_tenant_may_be_left():
    whole = _rec(SIGN_IN + RESET + APPLICATION)
    refused = _rec(SIGN_IN + RESET[:2], status="needs_you", need="sign_in",
                   reason="I put your saved password in Workday's new-password boxes, and it didn't take it as the new one.")
    result = lp.reset_result(lp.RESET_TENANTS[0], whole, refused, True)
    assert not result["ok"] and result["left_on"] == "derived?"
    assert "Pass 2 failed at KLA" in result["warning"] and "may be left on the derived password" in result["warning"]
    assert "The nightly's own reset will repair it" in result["warning"] and "had before" not in result["warning"]
    reused = _rec(SIGN_IN + RESET[:2], status="needs_you", need="sign_in", reason="I put your saved password in "
                  "Workday's new-password boxes, and it didn't take it as the new one. It says “You can't reuse a "
                  "recent password.”")
    said = lp.reset_result(lp.RESET_TENANTS[0], whole, reused, True)["warning"]
    assert "won't take a password it had before" in said and "look at it before the next nightly" in said
    stopped = lp.reset_result(lp.RESET_TENANTS[0], whole, None, False)  # (the probe stopped before pass 2)
    assert stopped["left_on"] == "derived?" and "may be left on the derived password" in stopped["warning"]
    assert "pass 2 didn't run (the probe was stopped before it)" in stopped["notes"]
    crashed = lp.reset_result(lp.RESET_TENANTS[0], whole, {"company": "KLA", "crash": "TimeoutError"}, True)
    assert crashed["left_on"] == "derived?" and crashed["warning"]


def test_a_tenant_whose_password_was_never_changed_is_left_as_it_was_but_fails():
    never = _rec(SIGN_IN, status="needs_you", need="sign_in", reason="Your saved password didn't sign in on Workday, "
                 "and I couldn't find its way to a password reset.")
    result = lp.reset_result(lp.RESET_TENANTS[1], never, None, False)
    assert not result["ok"] and result["left_on"] == "real" and not result["warning"]
    assert any("pass 2 not run" in n for n in result["notes"]) and not result["passes"][1]["ran"]


def test_the_probe_reads_the_desks_own_words():
    """The steps are read from pipeline.py's log lines: each one the probe looks for is still the desk's."""
    source = PIPELINE_SOURCE.read_text(encoding="utf-8")
    for words in ("with your saved password", "your saved password didn't sign in on {site}, so I asked it to email a "
                  "password reset", "opened the password reset link from your email", "set your saved password as "
                  "{site}'s new one", "reached the review page", "to create your account on {site}",
                  "password reset for your saved password, which didn't sign ", "new-password boxes, and it didn't take it ",
                  "Your saved password didn't sign in on", "no password reset email came from"):
        assert words in source, words


# --- the tailored resume ------------------------------------------------------------------------------------------

PROFILE = {"personal": {"first_name": "Jobdesk", "last_name": "Test", "email": EMAIL, "phone": "480-555-0199",
                        "address": {"line1": "175 S Arizona Ave", "city": "Chandler", "state": "AZ"}},
           "work_history": [{"title": "Equipment Technician", "company": "Example Fab", "location": "Chandler, AZ",
                             "start": "2021-03", "end": "present", "description": "Maintained process equipment."}],
           "education_history": [{"school": "Arizona State University", "degree": "Bachelor of Science",
                                  "major": "Electrical Engineering", "start": 2016, "end": 2020}]}


def test_the_tailored_resume_is_marked_and_holds_the_fake_profile_alone():
    md = lp.tailored_markdown(PROFILE, "Field Service Engineer", "NXP Semiconductors")
    assert md.startswith("# Jobdesk Test\n480-555-0199 | Chandler, AZ\n") and lp.TAILORED_NOTE in md
    assert "Tailored for: Field Service Engineer, NXP Semiconductors" in md
    assert "### Equipment Technician — Example Fab, Chandler, AZ *2021-03 – present*" in md
    assert "Arizona State University" in md and "@" not in md and "175 S Arizona" not in md  # (never an address)
    from job_apply.render import to_html
    assert "TAILORED TEST RESUME" in to_html(md, "resume")  # (render_document's layout reads it)


def test_the_desk_takes_the_tailored_file_and_the_check_tells_it_from_the_profiles(tmp_path):
    """Its name is one autofill.tailored_document picks from the job's folder (a stem ending in "resume"), and none a
    site could show for the profile's: the check tells them apart in any case, with "_", "-" or spaces."""
    folder = tmp_path / "0007-nxp-field-service-engineer"
    folder.mkdir()
    (folder / lp.TAILORED_NAME).write_bytes(b"%PDF-1.4 tailored")
    assert tailored_document({"folder": str(folder)}, "resume") == str(folder / lp.TAILORED_NAME)
    tailored, profiles = lp._flat(Path(lp.TAILORED_NAME).stem), lp._flat(Path(li.RESUME_NAME).stem)
    assert tailored not in profiles and profiles not in tailored


@pytest.mark.parametrize("text,verdict,said", [
    ("Resume/CV Jobdesk_Test_Tailored_Probe_Resume.pdf successfully uploaded Jobdesk_Test_Tailored_Probe_Resume.pdf "
     "1.36 KB Websites", "pass", "shows Jobdesk_Test_Tailored_Probe_Resume.pdf"),
    ("Resume/CV * jobdesk test tailored probe resume.PDF Cover Letter Attach", "pass", "shows"),
    # Workday's account keeps the resume of its last application (the nightly's review pages, runs 124 and 135)
    ("Resume/CV jobdesk-test-resume.pdf successfully uploaded jobdesk-test-resume.pdf 1.36 KB", "fail",
     "shows the profile's jobdesk-test-resume.pdf, not the tailored"),
    ("Resume/CV jobdesk-test-resume.pdf Jobdesk_Test_Tailored_Probe_Resume.pdf", "fail", "shows both"),
    ("Resume/CV * Attach Dropbox Accepted file types: pdf, doc, docx, txt, rtf", "fail", "shows neither")])
def test_the_review_page_shows_the_tailored_resume_or_says_what_instead(text, verdict, said):
    check = lp.tailored_check(text)
    assert check["verdict"] == verdict and said in check["detail"], check
    assert "pdf" not in check["shown"]  # (a file type alone is no file)


def test_a_tailored_probe_that_didnt_get_there_fails_with_why():
    page = "Resume/CV Jobdesk_Test_Tailored_Probe_Resume.pdf successfully uploaded"
    made = {"file": lp.TAILORED_NAME, "pages": 1, "picked": lp.TAILORED_NAME}
    employer = lp.TAILORED_EMPLOYERS[1]
    ok = lp.tailored_result(employer, _rec(APPLICATION, company=employer["name"], tailored=made), page)
    assert ok["ok"] and ok["verdict"] == "pass" and ok["shown"] == [lp.TAILORED_NAME]
    short = lp.tailored_result(employer, _rec(SIGN_IN, status="needs_you", need="bot_check", company=employer["name"],
                                              reason="The site is checking that you're a person", tailored=made), page)
    assert not short["ok"] and "didn't reach the review page (ended: needs_you bot_check), stopped on a bot check" == short["detail"]
    broken = lp.tailored_result(employer, _rec(APPLICATION, company=employer["name"], tailored={
        "file": lp.TAILORED_NAME, "error": "BrowserUnavailable: no browser"}), page)
    assert not broken["ok"] and "no tailored resume in the job's folder: BrowserUnavailable" in broken["detail"]
    assert not lp.tailored_result(employer, None, "")["ok"]


def test_live_smoke_drops_the_tailored_resume_into_the_jobs_folder(tmp_path):
    """--tailored-resume, short of a browser (the PDF printer stood in for): the file goes into the job's own folder
    under its own name, from the run's profile, and the desk's own chooser picks it."""
    driver = (
        "import asyncio, json, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import live_smoke\n"
        "from job_apply import render\n"
        "made = {}\n"
        "async def printed(page_html, dest):\n"
        "    made['html'] = page_html\n"
        "    dest.write_bytes(b'%PDF-1.4 stand-in')\n"
        "    return 1\n"
        "render.render_pdf = printed\n"
        "job = live_smoke.server.add_job(url='https://example.com/job/1', title='Field Service Engineer', "
        "company='Example Fab')['job']\n"
        "out = asyncio.run(live_smoke.drop_tailored_resume(job))\n"
        "folder = live_smoke.server.tracker().get(job['id'])['folder']\n"
        "print(json.dumps({'out': out, 'folder': folder, 'note': 'TAILORED TEST RESUME' in made['html'],\n"
        "                  'for': 'Field Service Engineer, Example Fab' in made['html']}))\n")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("LIVE_TEST_", "JOB_APPLY_"))}
    out = subprocess.run([sys.executable, "-c", driver], env={**clean, "TMPDIR": str(tmp_path)}, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    said = json.loads(out.stdout.strip().splitlines()[-1])
    assert said["out"] == {"file": lp.TAILORED_NAME, "pages": 1, "picked": lp.TAILORED_NAME}, said
    assert (Path(said["folder"]) / lp.TAILORED_NAME).read_bytes().startswith(b"%PDF") and said["note"] and said["for"]


# --- live_smoke.py's switches, short of a browser --------------------------------------------------------------------

def _smoke(tmp_path, env, *args):
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("LIVE_TEST_", "JOB_APPLY_SECRET_", "JOB_APPLY_LIVE_TEST_"))}
    out = subprocess.run([sys.executable, str(SCRIPTS / "live_smoke.py"), *args, "--out", str(tmp_path / "out")],
                         env={**clean, **env, "TMPDIR": str(tmp_path)}, capture_output=True, text=True, timeout=120)
    return out.returncode, out.stdout + out.stderr


def test_live_smokes_probe_switches_go_only_with_what_they_need(tmp_path):
    code, said = _smoke(tmp_path, ENV, "--pipeline", "--derived-password")
    assert code == 2 and "--derived-password goes with --test-identity" in said, said
    code, said = _smoke(tmp_path, ENV, "--tailored-resume")
    assert code == 2 and "--tailored-resume goes with --pipeline" in said, said


def test_live_smoke_with_the_derived_password_says_so_and_never_says_it(tmp_path):
    """Everything up to the first employer (--companies matches none, so no browser starts), with --derived-password:
    the run says Workday's saved password is the derived one, and neither it nor the real one shows anywhere."""
    code, said = _smoke(tmp_path, ENV, "--pipeline", "--test-identity", "--everywhere", "--derived-password",
                        "--companies", "no-such-employer")
    assert code == 0, said
    assert "Workday's saved password this run is the one derived from its site password" in said
    assert not any(s in said for s in (DERIVED, SITE, EMAIL, INBOX.replace(" ", ""))), said
    for path in tmp_path.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert not any(s.encode() in data for s in (DERIVED, SITE)), path


def test_live_smoke_sets_the_derived_password_for_workday_alone(tmp_path):
    """In the run itself: Workday's saved password is the derived one, every other system's the real one, and the
    derived one printed comes out masked whole."""
    driver = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import live_smoke\n"
        "from job_apply import config\n"
        "live_smoke.start_test_identity(announce=False, derived=True)\n"
        "site = live_smoke.live_identity.credentials(os.environ)[2]\n"
        "derived = live_smoke.live_probes.derived_password(site)\n"
        "print('workday', config.get_secret('workday_password') == derived)\n"
        "print('others', config.get_secret('successfactors_password') == site == config.get_secret('taleo_password'))\n"
        "print('shown', derived, site)\n")
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("LIVE_TEST_", "JOB_APPLY_"))}
    out = subprocess.run([sys.executable, "-c", driver], env={**clean, **ENV, "TMPDIR": str(tmp_path)},
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["workday True", "others True", f"shown {li.PASSWORD_MASK} {li.PASSWORD_MASK}"]


# --- the probes' employers and runs ---------------------------------------------------------------------------------

def test_each_probe_employer_is_one_employer_on_the_lists():
    """live_smoke.py --companies matches any part of a name: each probe's employer matches exactly one employer on the
    plugin's lists (--everywhere's), the reset tenants a Workday one each."""
    employers = li.everywhere_employers()
    for e in [*lp.RESET_TENANTS, *lp.TAILORED_EMPLOYERS]:
        matched = [x for x in employers if e["name"].lower() in x["name"].lower()]
        assert [x["name"] for x in matched] == [e["name"]], (e["name"], matched)
        if e in lp.RESET_TENANTS:
            assert matched[0]["system"] == "workday"
    assert len(lp.RESET_TENANTS) == 2 and {e["name"] for e in lp.RESET_TENANTS}.isdisjoint(
        e["name"] for e in lp.TAILORED_EMPLOYERS)  # (a tailored run never meets a tenant a reset left on the derived one)


def test_each_run_is_the_test_identity_at_one_employer_and_never_answers_a_security_question(tmp_path):
    first = lp.run_args("KLA", tmp_path / "a", derived=True)
    second = lp.run_args("KLA", tmp_path / "b")
    tailored = lp.run_args("NXP Semiconductors", tmp_path / "c", tailored=True)
    for args in (first, second, tailored):
        assert args[:4] == ["--pipeline", "--test-identity", "--everywhere", "--companies"]
        assert "--security-answers" not in args and "--parallel" not in args and "--fake-passwords" not in args
    assert "--derived-password" in first and "--derived-password" not in second + tailored
    assert "--tailored-resume" in tailored and "--tailored-resume" not in first + second
    assert lp.folder_of("reset", "Hitachi High-Tech America", 2) == "reset-hitachi-high-tech-america-2"
    assert lp.folder_of("tailored", "ASM (ASM America)") == "tailored-asm-asm-america"
    assert [e["name"] for e in lp.chosen(lp.RESET_TENANTS, "hitachi")] == ["Hitachi High-Tech America"]


def test_the_probes_run_one_at_a_time_and_pass_two_only_where_it_may_be_needed(tmp_path, monkeypatch, capsys,
                                                                               probes_out):
    """The whole of a probes run with its live_smoke.py runs stood in for (each leaves what a real one left, from
    probes_out): one run at a time, each tenant's pass 1 (the derived password) then its pass 2, Hitachi's pass 2 left
    out as its pass 1 never changed the site's password, then the tailored runs; the report from what they left."""
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "stdout", sys.stdout)  # (main masks them: put back after the test)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    out = tmp_path / "out"
    calls, running = [], []

    async def stand_in(args, folder, minutes=lp.PASS_MINUTES):
        assert not running  # (one at a time: one inbox)
        running.append(1)
        calls.append((folder.name, "--derived-password" in args, "--tailored-resume" in args))
        shutil.copytree(probes_out / folder.name, folder, dirs_exist_ok=True)
        running.pop()
        return {"exit": 0}

    monkeypatch.setattr(lp, "run_one", stand_in)
    assert lp.main(["--out", str(out)]) == 1
    assert calls == [("reset-kla-1", True, False), ("reset-kla-2", False, False),
                     ("reset-hitachi-high-tech-america-1", True, False), ("tailored-asm-asm-america", False, True),
                     ("tailored-nxp-semiconductors", False, True)]
    printed = capsys.readouterr().out
    assert "pass 2 not needed (the site's password was never changed)" in printed
    assert "Weekly probes (reset, tailored) as the test identity (<test-email>)" in printed
    assert json.loads((out / "probes.json").read_text(encoding="utf-8"))["reset"][0]["ok"] is True
    calls.clear()
    assert lp.main(["--out", str(tmp_path / "again"), "--probes", "tailored", "--companies", "asm"]) == 0
    assert calls == [("tailored-asm-asm-america", False, True)]


def test_a_run_is_live_smoke_itself_and_one_past_its_time_is_stopped(tmp_path, monkeypatch, capsys):
    """run_one starts live_smoke.py with the probe's switches (here at no employer: no browser starts), passes its lines
    on, and writes how it ended; a run past its time is stopped, its process group with it."""
    import asyncio

    clean = {k: v for k, v in os.environ.items() if k.startswith(("LIVE_TEST_", "JOB_APPLY_"))}
    for key in clean:
        monkeypatch.delenv(key)
    for key, value in {**ENV, "TMPDIR": str(tmp_path)}.items():
        monkeypatch.setenv(key, value)
    folder = tmp_path / "reset-nobody-1"
    outcome = asyncio.run(lp.run_one(lp.run_args("no-such-employer", folder, derived=True), folder))
    assert outcome["exit"] == 0 and json.loads((folder / "outcome.json").read_text(encoding="utf-8"))["exit"] == 0
    printed = capsys.readouterr().out
    assert "Workday's saved password this run is the one derived from its site password" in printed
    assert not any(s in printed for s in (DERIVED, SITE, EMAIL)), printed
    assert lp.read_record(folder, "Nobody") == {"company": "Nobody", "crash": "the run ended (exit 0) with no record "
                                                                              "of Nobody"}
    slow = tmp_path / "slow.py"
    slow.write_text("import time\nprint('started', flush=True)\ntime.sleep(60)\n", encoding="utf-8")
    monkeypatch.setattr(lp, "SMOKE", slow)
    stopped = asyncio.run(lp.run_one([], tmp_path / "slow", minutes=0.05))
    assert stopped["exit"] is None and "still running after 0.05 minutes" in stopped["stopped"]
    assert stopped["seconds"] < 30 and "started" in capsys.readouterr().out


def test_the_probes_refuse_to_start_without_the_test_identity(tmp_path, monkeypatch, capsys):
    for name in li.ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert lp.main(["--out", str(tmp_path / "out")]) == 2
    assert "The weekly probes need LIVE_TEST_EMAIL" in capsys.readouterr().err and not (tmp_path / "out").exists()
    for key, value in {**ENV, li.ENV_SITE_PASSWORD: "has a space1A!"}.items():
        monkeypatch.setenv(key, value)
    assert lp.main(["--out", str(tmp_path / "out")]) == 2
    err = capsys.readouterr().err
    assert "wouldn't meet Workday's rules (a space, a quote or a backslash" in err and "has a space" not in err
    assert lp.main(["--probes", "reset,other"]) == 2


# --- the report, from runs already there (the dry path), and nightly_summary.py --probes -----------------------------

def _run(out: Path, folder: str, rec, page: str = "") -> None:
    (out / folder).mkdir(parents=True)
    (out / folder / "pipeline.json").write_text(json.dumps([rec]), encoding="utf-8")
    (out / folder / "outcome.json").write_text(json.dumps({"exit": 0, "seconds": 80}), encoding="utf-8")
    if page:
        shot = out / folder / "pipeline" / lp.slug(rec["company"])
        shot.mkdir(parents=True)
        (shot / "page.html").write_text(f"<html><body><h2>Review</h2><p>{page}</p><script>x='jobdesk-test-resume.pdf'"
                                        "</script></body></html>", encoding="utf-8")


@pytest.fixture
def probes_out(tmp_path):
    """A probes run's folder: KLA's two passes whole; Hitachi's pass 1 refused with no way to a reset (no pass 2); the
    tailored resume on ASM's review page; NXP's showing the resume its account kept; a run stopped at its time."""
    out = tmp_path / "live-probes"
    whole = SIGN_IN + RESET + APPLICATION
    _run(out, "reset-kla-1", _rec(whole))
    _run(out, "reset-kla-2", _rec(whole))
    _run(out, "reset-hitachi-high-tech-america-1", _rec(SIGN_IN, status="needs_you", need="sign_in",
         company="Hitachi High-Tech America", reason="Your saved password didn't sign in on Workday, and I couldn't "
                                                     "find its way to a password reset."))
    made = {"file": lp.TAILORED_NAME, "pages": 1, "picked": lp.TAILORED_NAME}
    _run(out, "tailored-asm-asm-america", _rec(APPLICATION, company="ASM (ASM America)", tailored=made),
         page=f"Resume/CV * {lp.TAILORED_NAME} Cover Letter Attach")
    _run(out, "tailored-nxp-semiconductors", _rec(APPLICATION, company="NXP Semiconductors", tailored=made),
         page="Resume/CV jobdesk-test-resume.pdf successfully uploaded jobdesk-test-resume.pdf 1.36 KB")
    return out


def test_the_report_is_written_from_the_runs_there(probes_out, capsys):
    assert lp.main(["--out", str(probes_out), "--report-only"]) == 1  # (Hitachi's and NXP's didn't pass)
    printed = capsys.readouterr().out
    results = json.loads((probes_out / "probes.json").read_text(encoding="utf-8"))
    assert [(r["employer"], r["ok"], r["left_on"]) for r in results["reset"]] == [
        ("KLA", True, "real"), ("Hitachi High-Tech America", False, "real")]
    assert [(r["employer"], r["verdict"]) for r in results["tailored"]] == [("ASM (ASM America)", "pass"),
                                                                           ("NXP Semiconductors", "fail")]
    assert results["tailored"][1]["shown"] == [li.RESUME_NAME]  # (not the script's own words: the page's)
    report = (probes_out / "report.md").read_text(encoding="utf-8")
    assert "| KLA | 1 | derived | yes | yes | yes | yes | yes | yes (review page) | ready |" in report
    assert "| KLA | 2 | real | yes | yes | yes | yes | yes | yes (review page) | ready |" in report
    assert "| Hitachi High-Tech America | 1 | derived | yes | no | no | no | no | no | needs_you sign_in |" in report
    assert "| Hitachi High-Tech America | 2 | real | – | – | – | – | – | – | not run |" in report
    assert f"| ASM (ASM America) | {lp.TAILORED_NAME} | pass |" in report
    assert "| NXP Semiconductors | jobdesk-test-resume.pdf | FAIL | the review page shows the profile's" in report
    assert sum(line.startswith("LIVE_WEEKLY ") for line in printed.splitlines()) == 4
    assert "reset KLA: pass (site left on the real password)" in printed


def test_a_run_stopped_at_its_time_is_said(tmp_path):
    out = tmp_path / "live-probes"
    (out / "reset-kla-1").mkdir(parents=True)
    (out / "reset-kla-1" / "outcome.json").write_text(json.dumps({"exit": None, "stopped": "still running after 20 "
                                                                  "minutes, so it was stopped"}), encoding="utf-8")
    results = lp.collect(out, ["reset"], "KLA")
    first = results["reset"][0]["passes"][0]
    assert first["ended"] == "crash" and "still running after 20 minutes" in first["said"]
    assert results["reset"][0]["left_on"] == "derived?"  # (pass 2 never ran: said, plainly)


def test_nightly_summary_reads_the_probes_from_a_folder_or_a_zip(probes_out, tmp_path, capsys):
    lp.main(["--out", str(probes_out), "--report-only"])
    capsys.readouterr()
    assert ns.main(["--probes", str(probes_out)]) == 0
    from_folder = capsys.readouterr().out
    assert from_folder.splitlines()[0] == "reset KLA: pass (site left on the real password)"
    assert ("  pass 1 (derived password): refused yes, reset asked for yes, email read yes, new password set yes, "
            "signed in yes, reached the application yes (review page); ended ready") in from_folder
    assert "tailored NXP Semiconductors: FAIL: the review page shows the profile's jobdesk-test-resume.pdf" in from_folder
    zipped = tmp_path / "live-probes.zip"
    with zipfile.ZipFile(zipped, "w") as z:  # (as GitHub zips an artifact: its folder's contents at the top)
        for path in probes_out.rglob("*"):
            if path.is_file():
                z.write(path, path.relative_to(probes_out).as_posix())
    assert ns.main(["--probes", str(zipped), "--companies", "KLA"]) == 0
    from_zip = capsys.readouterr().out
    assert from_zip.startswith(from_folder)
    assert "-- reset-kla-1\n== KLA" in from_zip and "-- reset-kla-2\n== KLA" in from_zip and "-- tailored" not in from_zip
    assert ns.main(["--probes", str(tmp_path / "nothing-here")]) == 1


# --- where and when they run -------------------------------------------------------------------------------------

def test_the_probes_run_weekly_in_the_nightlys_workflow_after_its_checks():
    """In the live-nightly workflow (its concurrency group: never beside another run on the test inbox), after the
    checks of the same run (never beside the accounts check at the same accounts), on a weekly schedule of their own
    (which runs nothing else) and by hand; never submitting, never answering a security question."""
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert flow["concurrency"] == {"group": "live-nightly", "cancel-in-progress": False}
    on = flow.get("on", flow.get(True))
    crons = [s["cron"] for s in on["schedule"]]
    fresh = flow["jobs"]["fresh"]
    weekly = fresh["steps"][0]["env"]["WEEKLY"]
    assert weekly in crons and len(crons) == 2 and weekly.endswith(" * * 0")  # (Sundays)
    assert fresh["outputs"]["probes"] == "${{ steps.last.outputs.probes }}"
    assert "probes" in on["workflow_dispatch"]["inputs"]["checks"]["description"]
    probes = flow["jobs"]["probes"]
    assert probes["needs"] == ["fresh", "check"] and probes["if"] == "always() && needs.fresh.outputs.probes == 'true'"
    assert "concurrency" not in probes and probes["env"]["JOB_APPLY_NEVER_SUBMIT"] == "1"
    steps = {step.get("name", ""): step for step in probes["steps"]}
    run = steps["Run the probes"]
    assert "scripts/live_probes.py --out live-probes" in run["run"] and run["continue-on-error"] is True
    assert "--security-answers" not in json.dumps(probes)
    assert "scripts/live_audit.py --log \"probes=live-probes/run.log\" --folder live-probes" in steps["Audit what it wrote"]["run"]
    assert "live-probes/report.md" in steps["The probes' results"]["run"]
    assert steps["Fail if a probe didn't pass"]["if"] == "steps.run.outcome == 'failure'"
    compare = flow["jobs"]["compare"]["if"]
    assert "needs.fresh.outputs.go == 'true'" in compare  # (a weekly run is no nightly: never compared)


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None,
                    reason="the workflow's steps run in bash on Linux (Windows' may be another's)")
@pytest.mark.parametrize("event,schedule,checks,go,probes", [
    ("schedule", "43 13 * * 0", "", "false", "true"),  # the weekly schedule: the probes alone
    ("workflow_dispatch", "", "", "true", "false"),  # a full run by hand (after a release): no probes
    ("workflow_dispatch", "", "probes", "false", "true"),  # the probes alone, by hand
    ("workflow_dispatch", "", " probes ", "false", "true"),
    ("workflow_dispatch", "", "accounts,probes", "true", "true"),  # both: the probes after the checks
    ("workflow_dispatch", "", "accounts", "true", "false")])
def test_what_a_run_does_is_decided_by_its_fresh_job(tmp_path, event, schedule, checks, go, probes):
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = flow["jobs"]["fresh"]["steps"][0]
    output, summary = tmp_path / "output", tmp_path / "summary"
    env = {**{k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")},
           "EVENT": event, "SCHEDULE": schedule, "CHECKS": checks, "WEEKLY": step["env"]["WEEKLY"], "REPO": "o/r",
           "GH_TOKEN": "", "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}
    out = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    said = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").split())
    assert said == {"go": go, "probes": probes}, said
