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
