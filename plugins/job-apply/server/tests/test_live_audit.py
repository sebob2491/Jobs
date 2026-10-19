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
    assert not la.consent_given("ticked “I agree to the Privacy Notice and Terms of Use” "
                                "to make your account on Example")
    assert not la.consent_given("agreed to Example's notice “Use of AI in recruiting” for "
                                "you (settings.accept_notices)")
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
                             f"{(folder / 'pages' / 'form.html').as_posix()}: "
                             "LIVE_TEST_SITE_PASSWORD's value, unmasked"]
    assert not any("Xk9" in f or "jobdesk.test@" in f for f in found)


def test_main_fails_with_error_lines_only_when_something_was_found(tmp_path, capsys, monkeypatch):
    for name in la.SECRETS:
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "live-pipeline"
    folder.mkdir()
    (folder / "run.log").write_text(text(rec("Clean Co", ["filled 9 boxes"])), encoding="utf-8")
    assert la.main(["--log", f"pipeline={folder / 'run.log'}", "--folder", str(folder)]) == 0
    assert "audit: nothing found (1 records, 1 files)" in capsys.readouterr().out
    (folder / "run.log").write_text(text(rec("Example Fab",
                                             ["ticked “Join our talent community” to make your account"])),
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
</body></html>"""  # noqa: E501
PROFILE = {"name": "Jobdesk Test", "levels": ["high_school", "bachelor"],
           "others": ["Example Staffing", "Example Bank"],
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
    for employer, start in (("early-warning-services", "02/2021"), ("tokyo-electron-tel", "03/2021"),
                            ("kla", "02/2021")):
        (folder / "shard-2" / "pipeline" / employer).mkdir(parents=True)
        (folder / "shard-2" / "pipeline" / employer / "page.html").write_text(
            WORKDAY_REVIEW.format(start=start, to="", degree="Bachelor of Science", efrom="2016", more=""),
            encoding="utf-8")
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


def search_rec(company, posting, system="workday"):
    return {"company": company, "search_config": {system: "https://example.com"}, "posting": posting}


def test_a_posting_read_without_its_title_place_or_text_is_said_per_job_system(tmp_path, capsys, monkeypatch):
    """The review's posting-parse audit (run 116): SuccessFactors career sites' postings came back with no place, and
    every iCIMS posting's read failed, with nothing to say so. The search check's postings are read per job system:
    one without its title, its place or its text (a read that failed too) is a warning, never a failure."""
    whole = {"title": "Technician", "location": "Chandler, AZ", "description_chars": 4000, "ats": "workday"}
    records = [search_rec("Whole Co", whole),
               search_rec("Placeless Co", {**whole, "location": "", "ats": "successfactors"}, "successfactors"),
               search_rec("Thin Co", {**whole, "description_chars": 120, "ats": "successfactors"}, "successfactors"),
               search_rec("Refused Co", {"error": "FetchError: HTTP 405 from https://careers-x.icims.com/jobs/1/job"},
                          "icims"),
               {"company": "Unsearched Co", "search_config": {"workday": "https://example.com"}}]
    lines = la.posting_findings("".join("LIVE_RESULT " + json.dumps(r) + "\n" for r in records))
    assert lines == ["postings: icims: 1 of 1 not read whole: Refused Co (FetchError: HTTP 405 from "
                     "https://careers-x.icims.com/jobs/1/job)",
                     "postings: successfactors: 2 of 2 not read whole: Placeless Co (no place), Thin Co (120 chars of "
                     "text)"], lines
    for name in la.SECRETS:
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "live-search"
    folder.mkdir()
    (folder / "run.log").write_text("".join("LIVE_RESULT " + json.dumps(r) + "\n" for r in records), encoding="utf-8")
    assert la.main(["--log", f"search={folder / 'run.log'}", "--folder", str(folder)]) == 0
    out = capsys.readouterr().out
    assert "::warning::postings: successfactors: 2 of 2 not read whole" in out
    assert "postings: 3 of 4 not read whole" in out, out


PRIVACY = ("Example Fab collects your name, contact details and work history to consider your application. We may "
           "share them with our affiliates and service providers, and our site uses cookies. Read our Privacy Notice.")
WAIVER = ("By pressing I Agree you consent to our Terms of Use. Any dispute about your application will be settled by "
          "binding arbitration, and you waive the right to a jury trial.")


def agreed(heading, words, button="I Agree", what="notice", choices=(), windows=()):
    return {"what": what, "heading": heading, "button": button, "words": words, "choices": list(choices),
            "windows": list(windows), "sha": "0" * 40}


def test_the_words_of_an_agreement_are_read_again_and_a_waiver_deep_in_them_fails_the_audit(tmp_path, capsys,
                                                                                         monkeypatch):
    """The audit of Oct 18: the log keeps only an agreement's heading, so a waiver deep in a notice the desk's rules
    missed couldn't be found afterwards. Each round now carries the words the desk agreed to (live_smoke.py's
    agreed_words of pipeline.Run.agreed), and the audit reads them again: an arbitration or waiver paragraph fails it,
    wherever it sits; a plain privacy notice (affiliates, cookies named as its practices) doesn't."""
    plain = rec("Plain Co", ["agreed to Plain Co's notice “Privacy Notice” for you (settings.accept_notices)"])
    # (a terms gate's words are its whole page's: a footer's talent community is no part of what it agrees to)
    footer = PRIVACY + " Join our Talent Community for job alerts."
    plain["rounds"][0]["agreed"] = [agreed("Privacy Notice", PRIVACY), agreed("Welcome", footer, what="terms gate")]
    deep = rec("Deep Co", ["agreed to Deep Co's “I Agree” for you (settings.accept_notices)"], status="needs_you")
    deep["rounds"][0]["agreed"] = [agreed("Candidate Privacy Statement", PRIVACY + " " * 2000 + WAIVER)]
    signup = rec("Signup Co", ["ticked “I agree” for you (settings.accept_notices)"])
    box = "I agree to the Terms of Use and to join the talent community for job alerts"
    signup["rounds"][0]["agreed"] = [agreed(box, box, "ticked", "attestation"),
                                     agreed("Terms of Use", footer + WAIVER, what="terms gate")]
    # after its last round (a code read, then the account's terms ticked): read too ("agreed_after"), and a clause past
    # the first 4,000 characters, in its window
    signup["agreed_after"] = [agreed("Create your account", "x", what="terms gate",
                                     windows=["... and you waive your right to a jury trial ..."])]
    assert list(la.agreement_findings("pipeline", plain)) == []
    assert list(la.agreement_findings("pipeline", deep)) == [
        'pipeline/Deep Co: agreed to "Candidate Privacy Statement" which names arbitration']
    assert list(la.agreement_findings("accounts", signup)) == [
        f'accounts/Signup Co: agreed to "{box}" which signs the applicant up',
        'accounts/Signup Co: agreed to "Terms of Use" which names arbitration',
        'accounts/Signup Co: agreed to "Create your account" which waives a right']
    # each text read apart, with the kinds the desk read it for (the review of c4edd30): a group's label that also
    # asks about marketing emails, with its privacy choice alone picked; a notice's heading and words never run on
    emd = rec("Group Co", ["ticked “Privacy and communications” for you (settings.accept_notices)"])
    label = ("Privacy and communications: please accept our privacy notice, and tell us if you would like to receive "
             "marketing emails")
    emd["rounds"][0]["agreed"] = [
        agreed(label, label, "I accept the privacy notice", "attestation", choices=["I accept the privacy notice"]),
        agreed("Join Example Fab", "Promotions and hiring decisions at Example Fab may use artificial intelligence.")]
    assert list(la.agreement_findings("pipeline", emd)) == []
    emd["rounds"][0]["agreed"][0]["choices"].append("Yes, I would like to receive marketing emails")
    assert list(la.agreement_findings("pipeline", emd)) == [
        f'pipeline/Group Co: agreed to "{label[:79]}…" which signs the applicant up']
    for name in la.SECRETS:
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "live-pipeline"
    folder.mkdir()
    (folder / "run.log").write_text(text(plain), encoding="utf-8")
    assert la.main(["--log", f"pipeline={folder / 'run.log'}"]) == 0
    assert "audit: nothing found (1 records, 0 files); values: as the profile's (0 review pages read); agreements: 2 " \
           "re-read" in capsys.readouterr().out
    (folder / "run.log").write_text(text(plain, deep), encoding="utf-8")
    assert la.main(["--log", f"pipeline={folder / 'run.log'}"]) == 1
    out = capsys.readouterr().out
    assert '::error::pipeline/Deep Co: agreed to "Candidate Privacy Statement" which names arbitration' in out
    assert "agreements: 3 re-read" in out


def snapshot(folder, employer, fields, page=True):
    where = folder / "pipeline" / employer
    where.mkdir(parents=True)
    (where / "snapshot.json").write_text(json.dumps({"url": "https://example.com", "fields": fields}), encoding="utf-8")
    if page:
        (where / "page.html").write_text("<p>a page</p>", encoding="utf-8")


def test_a_box_holding_the_applicants_own_detail_where_the_rules_put_none_is_said(tmp_path, capsys, monkeypatch):
    """The audit of Oct 18: the values check read dates and degrees only, so the profile's phone in the wrong box
    passed. Each saved page's boxes are read: one that holds the fake applicant's phone, email, zip, street, LinkedIn
    or full name (live_profile.py's, never the artifact's) where autofill.resolve_field wouldn't put it is a warning;
    the same detail in its own box (in any format the site writes it) says nothing. And a stop whose page wasn't saved
    is a warning, with "pages: N of M stops saved" on the last line."""
    for name in la.SECRETS:
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "live-hr"
    snapshot(folder, "wrong-co", [{"id": "a", "kind": "text", "label": "Employee ID", "value": "(480) 555-0199"},
                                  {"id": "b", "kind": "text", "label": "Mobile Phone", "value": "480-555-0199"},
                                  {"id": "c", "kind": "text", "label": "Reference Name", "value": "Testy McTestface"}])
    # (labels that ask for the detail though no rule of the desk's names them: no finding, the review of c4edd30)
    snapshot(folder, "asked-co",
             [{"id": str(i), "kind": "text", "label": label, "value": value} for i, (label, value) in
              enumerate([("Home Phone", "480-555-0199"), ("Contact Number", "480-555-0199"),
                         ("Sign-in Email", "testy.mctestface.0a1b2c@example.com"),
                         ("City, State or Zip", "85225"), ("Applicant Name", "Testy McTestface"),
                         ("Preferred Name", "Testy McTestface"), ("Sign", "Jobdesk Test"),
                         ("Website", "https://www.linkedin.com/in/example-test-profile"),
                         ("Social Media Profile", "linkedin.com/in/example-test-profile")])])
    snapshot(folder, "right-co", [
        {"id": "a", "kind": "text", "label": "Phone Number*", "value": "(480) 555-0199"},
        {"id": "b", "kind": "text", "label": "Email Address", "value": "testy.mctestface.0a1b2c@example.com"},
        {"id": "c", "kind": "text", "label": "User Name", "value": "<test-email>"},
        {"id": "d", "kind": "text", "label": "Postal Code", "value": "85225"},
        {"id": "e", "kind": "text", "label": "Address Line 1", "value": "175 S Arizona Ave"},
        {"id": "f", "kind": "text", "label": "LinkedIn Profile",
         "value": "https://www.linkedin.com/in/example-test-profile"},
        {"id": "g", "kind": "text", "label": "Electronic Signature", "value": "Jobdesk Test"},
        {"id": "h", "kind": "text", "label": "Employee ID", "value": "N/A"},
        {"id": "i", "kind": "password", "label": "Password", "value": "(set)"}])
    snapshot(folder, "unsaved-co", [], page=False)
    snapshot(folder, "fixture-co", [])
    records = [rec("Wrong Co", ["filled 9 boxes"]), rec("Right Co", ["filled 9 boxes"]), rec("Asked Co", ["filled"]),
               rec("Unsaved Co", ["filled 9 boxes"], status="needs_you"),
               {**rec("Broken Co", ["filled 9 boxes"], status="needs_you"),
                "snapshot_error": "TimeoutError: page closed"},
               # (its page saved, and the fixture made from it failed: said as that, the review of c4edd30)
               {**rec("Fixture Co", ["filled 9 boxes"], status="needs_you"), "fixture_error": "OSError: disk full"}]
    assert la.stop_pages("hr", {"Odd Co": {"company": "Odd Co", "rounds": ["not a round"]}}, [folder]) == (0, 0, [])
    assert la.boxes_findings("hr", la.live_compare.read_log(text(*records), "LIVE_PIPELINE "), [folder]) == [
        "hr/Wrong Co: box \"Employee ID\" holds the profile's phone",
        "hr/Wrong Co: box \"Reference Name\" holds the profile's full name"]
    (folder / "run.log").write_text(text(*records), encoding="utf-8")
    assert la.main(["--log", f"hr={folder / 'run.log'}", "--folder", str(folder)]) == 0  # (warnings: never a failure)
    out = capsys.readouterr().out
    assert "::warning::hr/Wrong Co: box \"Employee ID\" holds the profile's phone" in out
    assert "::warning::hr/Unsaved Co: the page it stopped on wasn't saved" in out
    assert "::warning::hr/Broken Co: the page it stopped on wasn't saved (TimeoutError: page closed)" in out
    assert ("::warning::hr/Fixture Co: the page it stopped on was saved, but its fixture "
            "failed (OSError: disk full)" in out)
    assert "Right Co" not in out and "Asked Co" not in out and "::error::" not in out
    assert "; pages: 1 of 3 stops saved" in out


def test_the_summary_says_on_how_many_postings_each_system_lists_pay_and_gives_no_date(tmp_path, capsys, monkeypatch):
    """The design of Oct 18, F1 and F2: how far the desk's pay listing and its Posted filter reach, per job system, on
    the audit's summary line. A record from before the live check kept dates isn't counted either way."""
    whole = {"title": "Technician", "location": "Chandler, AZ", "description_chars": 4000}
    records = [search_rec("Paid Co", {**whole, "ats": "paycom", "pay": "$26–$28/hr (≈$54k–$58k/yr)",
                                      "posted": "2026-10-01"}, "paycom"),
               search_rec("Onto Co", {**whole, "ats": "workday", "pay": "$71k–$107k/yr", "posted": "Posted Today"}),
               search_rec("Lam Co", {**whole, "ats": "workday", "pay": "", "posted": "2026-10-02"}),
               search_rec("Old Co", {**whole, "ats": "workday"}),
               search_rec("Dateless Co", {**whole, "ats": "applicantstack", "pay": "", "posted": ""}, "applicantstack"),
               search_rec("Refused Co", {"error": "FetchError: HTTP 405"}, "icims")]
    facts = la.posting_facts("".join("LIVE_RESULT " + json.dumps(r) + "\n" for r in records))
    none = {"placed": 0, "workplace": 0}  # (records from before the workplace was kept: counted neither way)
    assert facts == {"paycom": {"read": 1, "pay": 1, "dated": 1, "no_date": 0, **none},
                     "workday": {"read": 3, "pay": 1, "dated": 2, "no_date": 0, **none},
                     "applicantstack": {"read": 1, "pay": 0, "dated": 1, "no_date": 1, **none}}
    assert la.facts_said(facts) == ("pay listed on 2 of 5 (applicantstack 0 of 1, paycom 1 of 1, workday 1 of 3); "
                                    "no date on 1 of 4 (applicantstack 1 of 1)")
    for name in la.SECRETS:
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "live-search"
    folder.mkdir()
    (folder / "run.log").write_text("".join("LIVE_RESULT " + json.dumps(r) + "\n" for r in records), encoding="utf-8")
    la.main(["--log", f"search={folder / 'run.log'}", "--folder", str(folder)])
    assert "; pay listed on 2 of 5 (applicantstack 0 of 1, paycom 1 of 1, workday 1 of 3); no date on 1 of 4" \
        in capsys.readouterr().out


def test_the_summary_says_on_how_many_postings_each_system_says_where_the_job_is_done(tmp_path, capsys, monkeypatch):
    """The design of Oct 18, F3: how far the desk's Workplace filter reaches, per job system, on the audit's summary
    line, as the pay listing's does. A record from before the live check kept the workplace isn't counted."""
    whole = {"title": "Technician", "location": "Chandler, AZ", "description_chars": 4000, "pay": "", "posted": "x"}
    records = [search_rec("Paid Co", {**whole, "ats": "paycom", "workplace": "hybrid",
                                      "workplace_source": "structured"}, "paycom"),
               search_rec("Remote Co", {**whole, "ats": "workday", "workplace": "remote",
                                        "workplace_source": "location"}),
               search_rec("Plain Co", {**whole, "ats": "workday", "workplace": "", "workplace_source": ""}),
               search_rec("Old Co", {**whole, "ats": "workday"})]
    facts = la.posting_facts("".join("LIVE_RESULT " + json.dumps(r) + "\n" for r in records))
    assert (facts["paycom"]["placed"], facts["paycom"]["workplace"]) == (1, 1)
    assert (facts["workday"]["read"], facts["workday"]["placed"], facts["workday"]["workplace"]) == (3, 2, 1)
    assert la.facts_said(facts).endswith("; workplace read on 2 of 3 (paycom 1 of 1, workday 1 of 2)")
    for name in la.SECRETS:
        monkeypatch.delenv(name, raising=False)
    folder = tmp_path / "live-search"
    folder.mkdir()
    (folder / "run.log").write_text("".join("LIVE_RESULT " + json.dumps(r) + "\n" for r in records), encoding="utf-8")
    la.main(["--log", f"search={folder / 'run.log'}", "--folder", str(folder)])
    assert "; workplace read on 2 of 3 (paycom 1 of 1, workday 1 of 2)" in capsys.readouterr().out
