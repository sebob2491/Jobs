"""The live check's rules audit (scripts/live_audit.py): a consent the desk never gives, a submit, a secret, a code
or a security answer in what a check's machine wrote. No network: the logs are made here."""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("live_audit", SCRIPTS / "live_audit.py")
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)


def rec(company, log, status="ready", reason=""):
    return {"company": company, "posting": {"title": "Technician"},
            "rounds": [{"status": status, "need": "", "reason": reason, "log": log, "questions": []}]}


def text(*records):
    return "".join("LIVE_PIPELINE " + json.dumps(r) + "\n" for r in records)


def test_a_consent_the_desk_never_gives_is_a_finding_and_the_ones_it_does_are_not():
    assert la.consent_given("ticked “Sign me up for job alerts” to make your account on Example's site")
    assert la.consent_given("agreed to Example's “Newsletter and marketing emails” for you (settings.accept_notices)")
    assert la.consent_given("pressed “I agree to arbitration” to finish creating your account on Example")
    assert la.consent_given("ticked “I acknowledge my employment is at-will” to make your account")
    assert not la.consent_given("ticked “I agree to the Privacy Notice and Terms of Use” to make your account on Example")
    assert not la.consent_given("agreed to Example's notice “Use of AI in recruiting” for you (settings.accept_notices)")
    assert not la.consent_given("left “Send me job alerts” unticked: never a newsletter")
    assert not la.consent_given("clicked “Apply” (a newsletter box sits in the footer)")  # not a consent's verb
    assert not la.consent_given("filled 9 boxes; the talent community box stays yours")


def test_findings_name_the_employer_and_the_rule():
    log = text(rec("Example Fab", ["clicked “Apply”", "ticked “Sign me up for job alerts” to make your account"]),
               rec("Sent Co", ["pressed “Submit”"], status="submitted"),
               rec("Code Co", ["entered the code 123456 from your email (example.com)"]),
               rec("Secret Co", ["typed jd0123456789ab into “Answer”"]),
               rec("Clean Co", ["entered the code from your email (example.com)", "filled 9 boxes"]))
    found = la.log_findings("accounts", log)
    assert [f.split(":")[0] for f in found] == ["accounts/Example Fab", "accounts/Sent Co", "accounts/Code Co",
                                                 "accounts/Secret Co"]
    assert "a consent the desk never gives" in found[0] and "job alerts" in found[0]
    assert "was submitted" in found[1] and "an emailed code" in found[2]
    assert "a security answer" in found[3] and "jd0123456789ab" not in found[3]


def test_a_secret_in_a_file_is_said_by_name_never_by_value(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TEST_EMAIL", "jobdesk.test@example.com")
    monkeypatch.setenv("LIVE_TEST_SITE_PASSWORD", "Xk9-not-a-real-password")
    monkeypatch.setenv("LIVE_TEST_EMAIL_PASSWORD", "")
    folder = tmp_path / "live-accounts-0"
    (folder / "pages").mkdir(parents=True)
    (folder / "run.log").write_text(text(rec("Example Fab", ["filled 9 boxes"])), encoding="utf-8")
    (folder / "pages" / "example.html").write_text("<p>Signed in as jobdesk.test@example.com</p>", encoding="utf-8")
    (folder / "pages" / "form.html").write_text('<input value="Xk9-not-a-real-password">', encoding="utf-8")
    (folder / "pages" / "clean.html").write_text("<p>Signed in as [email]</p>", encoding="utf-8")
    (folder / "shot.png").write_bytes(b"\x89PNG\0\0jobdesk.test@example.com")  # binary: not read
    found = la.folder_findings(folder, {n: __import__("os").environ.get(n, "") for n in la.SECRETS})
    assert sorted(found) == [f"{(folder / 'pages' / 'example.html').as_posix()}: LIVE_TEST_EMAIL's value, unmasked",
                             f"{(folder / 'pages' / 'form.html').as_posix()}: LIVE_TEST_SITE_PASSWORD's value, unmasked"]
    assert not any("Xk9" in f or "jobdesk.test@" in f for f in found)


def test_main_fails_with_error_lines_only_when_something_was_found(tmp_path, capsys, monkeypatch):
    for name in la.SECRETS:
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "live-pipeline"
    folder.mkdir()
    (folder / "run.log").write_text(text(rec("Clean Co", ["filled 9 boxes"])), encoding="utf-8")
    assert la.main(["--log", f"pipeline={folder / 'run.log'}", "--folder", str(folder)]) == 0
    assert "audit: nothing found (1 records, 1 files)" in capsys.readouterr().out
    (folder / "run.log").write_text(text(rec("Example Fab", ["ticked “Join our talent community” to make your account"])),
                                    encoding="utf-8")
    assert la.main(["--log", f"pipeline={folder / 'run.log'}", "--folder", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "::error::pipeline/Example Fab: a consent the desk never gives: ticked “Join our talent community”" in out
    assert "audit: 1 finding (1 records, 1 files)" in out


WORKDAY_REVIEW = """<html><body><h2>Review</h2><p>Legal Name Jobdesk Test</p><p>Email Address <input type="email" value="x@example.com"></p>
<h3>My Experience</h3><h4>Work Experience</h4><p>Work Experience 1</p><p>Job Title Equipment Technician</p>
<p>Company Example Fab</p><p>Location Chandler, AZ</p><p>I currently work here Yes</p><p>From {start}</p>{to}
<h4>Education</h4><p>Education 1</p><p>School or University Arizona State University</p><p>Degree {degree}</p>
<p>Field of Study Electrical Engineering</p><p>From {efrom}</p><p>To (Actual or Expected) 2020</p>
<h4>Certifications</h4><p>No Response</p>{more}
<script>var hint = "From 01/1999 Degree Doctorate";</script><style>.x::before {{ content: "Example Bank"; }}</style>
<select><option>Choose...</option><option selected>Bachelor's</option><option>Master's Degree</option></select>
</body></html>"""
PROFILE = {"name": "Jobdesk Test", "levels": ["high_school", "bachelor"], "others": ["Example Staffing", "Example Bank"],
           "work": [{"title": "Equipment Technician", "company": "Example Fab", "start": "2021-03", "end": "present"}],
           "education": [{"school": "Arizona State University", "degree": "Bachelor of Science", "start": "2016",
                          "end": "2020"}]}


def review(start="03/2021", to="", degree="Bachelor of Science", efrom="2016", more=""):
    return la.page_text(WORKDAY_REVIEW.format(start=start, to=to, degree=degree, efrom=efrom, more=more))


def test_a_review_page_as_the_profile_has_it_says_nothing():
    """Tokyo Electron's Workday review page (run 105): the job from 03/2021, still held; the school 2016 to 2020, a
    Bachelor of Science. A script's and a style's words, a list's other entries and a box's value aren't the page's."""
    assert list(la.page_findings("accounts/TEL", review(), PROFILE)) == []
    assert list(la.page_findings("accounts/TEL", review(start="March 2021", efrom="08/2016"), PROFILE)) == []
    text = review()
    assert "Example Bank" not in text and "01/1999" not in text and "Master's Degree" not in text
    assert "Bachelor's" in text and "x@example.com" in text  # (a list's chosen entry, a box's value)


def test_a_date_a_degree_or_another_roles_employer_not_the_profiles_is_said():
    """Early Warning Services, KLA and Hitachi (run 105): the test identity's job saved at the tenant by an earlier run
    starts 02/2021 where the profile says 2021-03; Western Alliance Bank: its school from 1900. Said one line each,
    with the profile's own value; a job to a date where the profile's is still held, a master's beside a school
    the profile holds a bachelor's from, and another role's employer too."""
    found = list(la.page_findings("accounts/Early Warning Services",
                                  review(start="02/2021", to="<p>To 05/2024</p>", degree="Master of Science",
                                         efrom="1900", more="<p>Previously at Example Bank</p>"), PROFILE))
    assert found == [
        "accounts/Early Warning Services: the review page names another role's employer, Example Bank",
        "accounts/Early Warning Services: the review page shows a job from 02/2021; the profile's start 2021-03",
        "accounts/Early Warning Services: the review page shows a job to 05/2024; the profile's are still held",
        "accounts/Early Warning Services: the review page shows a school from 1900; the profile's start 2016",
        "accounts/Early Warning Services: beside Arizona State University the review page shows a master degree "
        "(“Master of Science”); the profile's levels: bachelor, high_school",
    ]


def test_the_postings_own_degree_words_are_not_the_applicants():
    """American Express' Oracle page (run 105) says "Degree Level : Master's Degree" among the posting's details, far
    from the applicant's school; a Degree read beside the school is the applicant's, one elsewhere isn't."""
    posting = "<p>Posting Details: Degree Level : Master's Degree Job Schedule : Full time</p>"
    assert list(la.page_findings("hr/AmEx", la.page_text(posting + WORKDAY_REVIEW.format(
        start="03/2021", to="", degree="BS", efrom="2016", more="")), PROFILE)) == []
    # (and a profile with no degree: a bachelor's beside its school is the "never overstate" rule broken)
    none = {**PROFILE, "levels": [], "education": [{"school": "Arizona State University", "start": "2016"}]}
    found = list(la.page_findings("hr/AmEx", review(degree="Bachelor of Science"), none))
    assert len(found) == 1 and "shows a bachelor degree" in found[0] and "the profile's levels: none" in found[0]


def test_a_date_is_read_as_the_profile_or_a_page_writes_it():
    assert la.month_year("2021-03") == (3, 2021) and la.month_year("03/2021") == (3, 2021)
    assert la.month_year("3/1/2021") == (3, 2021) and la.month_year("March 2021") == (3, 2021)
    assert la.month_year("Mar. 2021") == (3, 2021) and la.month_year(2016) == (None, 2016)
    assert la.month_year("present") == (None, None) and la.month_year("") == (None, None)
    assert la.same_date("02/2021", "2021-03") is False and la.same_date("2021", "2021-03") is True
    assert la.same_date("08/2016", 2016) is True and la.same_date("1900", "2016") is False


def test_the_values_check_reads_each_review_page_its_machine_saved(tmp_path, capsys, monkeypatch):
    """A record that reached the review page with its profile noted is read against the page in pipeline/<employer>/
    (a shard's folder too); one that stopped earlier, or has no profile (an older run's), isn't. A value that isn't
    the profile's is a ::warning:: line and never fails the audit."""
    for name in la.SECRETS:
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "live-accounts-1"
    for employer, start in (("early-warning-services", "02/2021"), ("tokyo-electron-tel", "03/2021"), ("kla", "02/2021")):
        (folder / "shard-2" / "pipeline" / employer).mkdir(parents=True)
        (folder / "shard-2" / "pipeline" / employer / "page.html").write_text(
            WORKDAY_REVIEW.format(start=start, to="", degree="Bachelor of Science", efrom="2016", more=""), encoding="utf-8")
    ready = {**rec("Early Warning Services", ["reached the review page"]), "profile": PROFILE}
    fine = {**rec("Tokyo Electron (TEL)", ["reached the review page"]), "profile": PROFILE}
    old = rec("KLA", ["reached the review page"])  # (no profile noted: an older run's)
    stopped = {**rec("KLA", ["filled 9 boxes"], status="needs_you"), "profile": PROFILE}
    (folder / "run.log").write_text(text(ready, fine, old), encoding="utf-8")
    assert la.values_findings("accounts", la.live_compare.read_log(text(ready, fine, old, stopped), "LIVE_PIPELINE "),
                              folder) == ["accounts/Early Warning Services: the review page shows a job from 02/2021; "
                                          "the profile's start 2021-03"]
    assert la.main(["--log", f"accounts={folder / 'run.log'}", "--folder", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "::warning::accounts/Early Warning Services: the review page shows a job from 02/2021" in out
    assert "::error::" not in out
    assert "audit: nothing found (3 records, 4 files); values: 1 not the profile's (2 review pages read)" in out
