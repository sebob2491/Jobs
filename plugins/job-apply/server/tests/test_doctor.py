"""The doctor: each check's pass and fail in plain words, its exit code, and nothing from the
profile in its report but which answers are missing."""

import io
import json
import os
import shutil

import httpx
import pytest
import yaml
from conftest import PROFILE, browser_available, run

from job_apply import config, doctor, updates

REAL_LAUNCHES = doctor.launches
CHROMIUM = os.environ.get("JOB_APPLY_CHROMIUM_PATH")  # (the fixture below takes it away)


@pytest.fixture
def fine(monkeypatch):
    """A computer with everything in place (the conftest profile asks for the bundled Chromium)."""
    monkeypatch.delenv("JOB_APPLY_CHROMIUM_PATH", raising=False)
    monkeypatch.setattr(doctor, "python_version", lambda: (3, 12, 3))
    monkeypatch.setattr(doctor, "uv_version", lambda: "0.11.32")
    monkeypatch.setattr(doctor, "on_windows", lambda: False)
    monkeypatch.setattr(doctor, "chrome_installed", lambda: False)
    monkeypatch.setattr(doctor, "edge_installed", lambda: False)
    monkeypatch.setattr(doctor, "chrome_beta_installed", lambda: False)
    monkeypatch.setattr(doctor, "bundled_chromium_installed", lambda: True)

    async def never(attempt):
        raise AssertionError("the browser is only started with --launch")

    monkeypatch.setattr(doctor, "launches", never)
    monkeypatch.setattr(config, "plugin_version", lambda: "0.3.92")


def report(launch=False):
    return run(doctor.run(launch=launch))


def check(rep, name):
    return next(c for c in rep.checks if c.name == name)


def write_profile(home, **changes):
    profile = {**PROFILE, "documents": {"resume": str(home / "resume.pdf")}, **changes}
    (home / "profile.yaml").write_text(yaml.safe_dump(profile))


def test_everything_in_place_exits_0(fine, loop, capsys):
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "✔ Python 3.12.3" in out and "✔ uv 0.11.32" in out and "✔ job-apply 0.3.92" in out
    assert "✔ Browser: the plugin's own Chromium" in out
    assert "Everything the plugin needs is in place." in out and "✘" not in out


def test_python_too_old(fine, loop, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "python_version", lambda: (3, 9, 18))
    c = check(report(), "python")
    assert c.status == doctor.PROBLEM and "3.9.18 is too old" in c.say and "installer" in c.fix
    assert doctor.main([]) == 1
    assert "1 thing to fix (marked ✘)." in capsys.readouterr().out


def test_uv_missing(fine, loop, monkeypatch):
    monkeypatch.setattr(doctor, "uv_version", lambda: None)
    c = check(report(), "uv")
    assert c.status == doctor.PROBLEM and "uv isn't installed" in c.say and "docs.astral.sh/uv" in c.fix
    assert not report().ok


def test_uv_version_read_from_uv(monkeypatch):
    class Done:
        stdout = "uv 0.11.32 (abc123 2026-09-30)\n"

    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *a, **kw: Done())
    assert doctor.uv_version() == "0.11.32"
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    assert doctor.uv_version() is None


def test_a_newer_version_is_advice_with_how_to_update(fine, loop, monkeypatch, capsys):
    """Not a failure: the plugin works, and the update commands are the desk's own."""
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "0")

    async def latest():
        return "0.3.95"

    monkeypatch.setattr(doctor, "latest_version", latest)
    c = check(report(), "update")
    assert c.status == doctor.ADVICE and "0.3.95" in c.say and "0.3.92" in c.say
    assert all(cmd in c.fix for cmd in updates.UPDATE_COMMANDS)
    assert doctor.main([]) == 0
    assert "! A newer job-apply is out" in capsys.readouterr().out


@pytest.mark.parametrize("latest, status, words", [("0.3.92", doctor.OK, "newest"),
                                                    ("0.3.90", doctor.OK, "newest"),
                                                    (None, doctor.SKIPPED, "Couldn't look")])
def test_the_same_or_no_answer_is_no_update(fine, loop, monkeypatch, latest, status, words):
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "0")

    async def answer():
        return latest

    monkeypatch.setattr(doctor, "latest_version", answer)
    c = check(report(), "update")
    assert c.status == status and words in c.say


def test_the_update_check_is_the_desks_and_can_be_turned_off(fine, loop, monkeypatch):
    """It reads the published manifest as the desk does, and not at all with JOB_APPLY_NO_UPDATE_CHECK=1."""
    seen = []
    real = httpx.AsyncClient

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"name": "job-apply", "version": "0.3.93"})

    monkeypatch.setattr(updates.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "1")
    c = check(report(), "update")
    assert c.status == doctor.SKIPPED and "JOB_APPLY_NO_UPDATE_CHECK" in c.say and seen == []
    monkeypatch.setenv("JOB_APPLY_NO_UPDATE_CHECK", "0")
    c = check(report(), "update")
    assert c.status == doctor.ADVICE and "0.3.93" in c.say and seen == [updates.manifest_url()]


def test_no_browser(fine, loop, monkeypatch):
    monkeypatch.setattr(doctor, "bundled_chromium_installed", lambda: False)
    c = check(report(), "browser")
    assert c.status == doctor.PROBLEM and "browser_channel: chromium" in c.say
    assert "playwright install chromium" in c.fix and str(config.PLUGIN_ROOT / "server") in c.fix
    assert not report().ok


def test_browser_follows_the_desks_order(fine, loop, monkeypatch, job_apply_home):
    """Chrome, then Edge (every Windows computer has it), then the bundled Chromium."""
    write_profile(job_apply_home, settings={"browser_channel": "chrome"})
    monkeypatch.setattr(doctor, "chrome_installed", lambda: True)
    assert check(report(), "browser").say == "Browser: Google Chrome"
    monkeypatch.setattr(doctor, "chrome_installed", lambda: False)
    monkeypatch.setattr(doctor, "edge_installed", lambda: True)
    assert check(report(), "browser").say == "Browser: Microsoft Edge"
    monkeypatch.setattr(doctor, "edge_installed", lambda: False)
    assert check(report(), "browser").say == "Browser: the plugin's own Chromium"
    monkeypatch.setattr(doctor, "bundled_chromium_installed", lambda: False)
    c = check(report(), "browser")
    assert c.status == doctor.PROBLEM and "Google Chrome, Microsoft Edge" in c.say and "google.com/chrome" in c.fix


def test_a_missing_chromium_path_is_said(fine, loop, monkeypatch, tmp_path):
    monkeypatch.setenv("JOB_APPLY_CHROMIUM_PATH", str(tmp_path / "nothing-here"))
    c = check(report(), "browser")
    assert c.status == doctor.PROBLEM and "JOB_APPLY_CHROMIUM_PATH" in c.say


def test_launch_starts_the_browser_only_when_asked(fine, loop, monkeypatch):
    started = []

    async def launches(attempt):
        started.append(attempt)
        return None

    monkeypatch.setattr(doctor, "launches", launches)
    assert check(report(), "browser").status == doctor.OK and started == []
    c = check(report(launch=True), "browser")
    assert c.status == doctor.OK and "(it starts)" in c.say and started == [{}]

    async def broken(attempt):
        return "Target page, context or browser has been closed: missing libnss3.so"

    monkeypatch.setattr(doctor, "launches", broken)
    c = check(report(launch=True), "browser")
    assert c.status == doctor.PROBLEM and "wouldn't start" in c.say and "libnss3" in c.say


@pytest.mark.skipif(not browser_available(), reason="no browser")
def test_launch_really_starts_one(fine, loop, monkeypatch):
    monkeypatch.setattr(doctor, "launches", REAL_LAUNCHES)
    if CHROMIUM:
        monkeypatch.setenv("JOB_APPLY_CHROMIUM_PATH", CHROMIUM)
    c = check(report(launch=True), "browser")
    assert c.status == doctor.OK and "(it starts)" in c.say, c


def test_folder_and_profile_missing_and_nothing_made(fine, loop, monkeypatch, tmp_path, capsys):
    home = tmp_path / "not-yet"
    monkeypatch.setenv("JOB_APPLY_HOME", str(home))
    rep = report()
    assert check(rep, "folder").status == doctor.PROBLEM and "isn't there yet" in check(rep, "folder").say
    assert check(rep, "profile").say.startswith("No profile yet") and "set up job-apply" in check(rep, "profile").fix
    assert check(rep, "resume").say == "No resume yet"
    assert doctor.main([]) == 1
    assert not home.exists()  # the doctor makes nothing
    assert "3 things to fix" in capsys.readouterr().out


def test_profile_missing_answers(fine, loop, job_apply_home):
    personal = {k: v for k, v in PROFILE["personal"].items() if k not in ("email", "phone")}
    write_profile(job_apply_home, personal=personal)
    c = check(report(), "profile")
    assert c.status == doctor.PROBLEM and c.say.endswith("still needs your email, your phone number")
    assert str(config.profile_path()) in c.say


def test_a_new_profile_isnt_filled_in(fine, loop, job_apply_home):
    shutil.copy(config.TEMPLATE_PROFILE, job_apply_home / "profile.yaml")
    c = check(report(), "profile")
    assert c.status == doctor.PROBLEM and "isn't filled in yet" in c.say


def test_a_profile_with_a_typo(fine, loop, job_apply_home):
    (job_apply_home / "profile.yaml").write_text("personal:\n  first_name: [Sam\n")
    c = check(report(), "profile")
    assert c.status == doctor.PROBLEM and "typo" in c.say and "Sam" not in c.say


def test_profile_gaps_and_settings_are_advice(fine, loop, job_apply_home):
    write_profile(job_apply_home, settings={**PROFILE["settings"], "submit_mode": "sometimes"})
    rep = report()
    assert check(rep, "profile").status == doctor.OK
    gaps = check(rep, "profile_gaps")
    assert gaps.status == doctor.ADVICE and "background questions" in gaps.say
    assert check(rep, "settings").status == doctor.ADVICE and "submit_mode" in check(rep, "settings").say
    assert rep.ok


def test_resume(fine, loop, job_apply_home):
    assert check(report(), "resume").status == doctor.OK
    (job_apply_home / "resume.pdf").unlink()
    c = check(report(), "resume")
    assert c.status == doctor.PROBLEM and "isn't there" in c.say
    (job_apply_home / "resume.docx").write_bytes(b"PK")  # the installer copied a Word resume
    c = check(report(), "resume")
    assert c.status == doctor.PROBLEM and "resume.docx is in your job-apply folder" in c.say
    (job_apply_home / "profile.yaml").unlink()  # before setup: the copied one is what setup uses
    c = check(report(), "resume")
    assert c.status == doctor.OK and "resume.docx" in c.say and "setup will use it" in c.say


@pytest.mark.parametrize("value, status", [(None, doctor.PROBLEM),
                                           (r"C:\Users\pat\AppData\Roaming\uv\python", doctor.PROBLEM),
                                           (r"C:\Users\pat\.uv-python", doctor.OK)])
def test_windows_uv_python_dir(fine, loop, monkeypatch, value, status):
    monkeypatch.setattr(doctor, "on_windows", lambda: True)
    monkeypatch.setattr(doctor, "user_env", lambda name: value if name == "UV_PYTHON_INSTALL_DIR" else None)
    c = check(report(), "uv_python_dir")
    assert c.status == status
    if status == doctor.PROBLEM:
        assert "AppData" in c.say and "UV_PYTHON_INSTALL_DIR" in c.fix


def test_no_windows_check_elsewhere(fine, loop):
    assert not [c for c in report().checks if c.name == "uv_python_dir"]


def test_appdata_paths():
    assert doctor.in_appdata(r"C:\Users\pat\AppData\Local\uv\python")
    assert doctor.in_appdata("C:/Users/pat/appdata/roaming/uv")
    assert not doctor.in_appdata(r"C:\Users\pat\.uv-python") and not doctor.in_appdata("/home/pat/.uv-python")


def test_nothing_personal_is_printed(fine, loop, job_apply_home, capsys):
    """The report may be pasted where others read it: of the profile, only its path and which
    answers are missing. Not a name, an employer, a school or the resume's own file name."""
    resume = job_apply_home / "Sam Rivera resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    personal = {k: v for k, v in PROFILE["personal"].items() if k != "phone"}
    work = [{**PROFILE["work_history"][0], "start": "2021"}]  # a job without its months
    education = [{"school": "Arizona State University"}]  # a school without a degree
    write_profile(job_apply_home, personal=personal, work_history=work, education_history=education,
                  documents={"resume": str(resume)})
    for args in ([], ["--json"]):
        doctor.main(args)
        out = capsys.readouterr().out
        # (JSON writes a Windows path's backslashes doubled: read it back to compare)
        shown = json.loads(out)["report"] if args else out
        for personal_text in ("Sam", "Rivera", "sam.rivera@example.com", "480-555", "100 W Main", "Chandler",
                              "85225", "Intel", "Equipment Technician", "Arizona State", "samrivera"):
            assert personal_text not in out and personal_text not in shown, personal_text
        assert str(config.profile_path()) in shown
    write_profile(job_apply_home, work_history=work, education_history=education)
    doctor.main([])
    out = capsys.readouterr().out
    assert "start and end months for some jobs" in out and "what you finished at some schools" in out
    assert "Intel" not in out and "Arizona State" not in out


def test_json_report(fine, loop, capsys):
    assert doctor.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and {c["name"] for c in data["checks"]} >= {"python", "uv", "plugin", "browser",
                                                                          "folder", "profile", "resume"}
    assert data["report"].startswith("job-apply doctor")


def test_marks_fall_back_to_plain_text(monkeypatch):
    """By the stream's own encoding, whatever the computer running the test (Windows' older
    console has a test of its own below)."""
    monkeypatch.setattr(doctor, "on_windows", lambda: False)
    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    cp1252 = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    assert doctor.marks_for(utf8) == doctor.MARKS
    assert doctor.marks_for(cp1252) == doctor.ASCII_MARKS
    text = doctor.Report([doctor.Check("x", doctor.PROBLEM, "Broken", "Fix it.")]).text(doctor.ASCII_MARKS)
    assert "[X] Broken\n    To fix: Fix it." in text and "1 thing to fix (marked [X])." in text


def test_windows_older_console_gets_plain_marks(monkeypatch):
    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    monkeypatch.setattr(doctor, "on_windows", lambda: True)
    monkeypatch.delenv("WT_SESSION", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    assert doctor.marks_for(utf8) == doctor.ASCII_MARKS
    monkeypatch.setenv("WT_SESSION", "1")  # Windows Terminal shows them
    assert doctor.marks_for(utf8) == doctor.MARKS


def test_claude_can_run_it_as_a_tool(fine, srv, monkeypatch):
    out = run(srv.doctor())
    assert out["ok"] is True and out["report"].startswith("job-apply doctor")
    monkeypatch.setattr(doctor, "uv_version", lambda: None)
    out = run(srv.doctor())
    assert out["ok"] is False and any(c["name"] == "uv" and c["status"] == "problem" for c in out["checks"])


def test_the_console_script_is_declared():
    text = (config.PACKAGE_DIR.parents[1] / "pyproject.toml").read_text()
    assert 'job-apply-doctor = "job_apply.doctor:main"' in text
