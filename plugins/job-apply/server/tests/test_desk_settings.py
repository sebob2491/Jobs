"""Settings as switches, the server's half (the design's Version D: D2, D3's routes, D7's tests 1, 2, 7 to 10).

/api/profile/settings writes the profile's settings the Settings page shows, and only those; /api/mail/test signs in to
the inbox and out again, reading nothing; /api/profile/document keeps the usual cover letter as the resume is kept;
/api/open/home opens the desk's own folder and no other. Made-up people and employers only: the repo is public."""

import base64
import io
import json
import logging
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
import yaml
from conftest import run
from test_desk import _client
from test_mailbox import FakeImap

from job_apply import config, mailbox, profile_setup, window
from job_apply import desk as desk_module
from job_apply.desk import PROFILE_SETTINGS, Desk

CANT = "That setting can't be changed here."
NO_PROFILE = "Set up your profile first."
APP_PASSWORD = "abcd efgh ijkl mnop"  # (a made-up app password, as Gmail shows one)
ADDRESS = "sam.rivera@example.com"  # (the tests' profile's)


def _desk(srv) -> Desk:
    desk = Desk(srv)
    desk.applier.start = lambda: None  # (the queue isn't worked)
    return desk


def _calls(desk: Desk, asks: list[tuple], token: bool = True, started=None) -> list[tuple[int, dict]]:
    """Each (method, path, json) asked of the running desk, with the page's key (or none): each answer's status and
    JSON. `started`: what to do once the desk has started (which writes the profile's template where there's none)."""

    async def go():
        await desk.start(port=0, open_browser=False)
        if started is not None:
            started()
        try:
            out = []
            async with _client(desk) as c:
                for method, path, body in asks:
                    h = {"x-desk-token": desk.token} if token else {}
                    if body is None:
                        got = await c.request(method, path, headers=h)
                    elif isinstance(body, str):
                        got = await c.request(method, path, headers=h, content=body)
                    else:
                        got = await c.request(method, path, headers=h, json=body)
                    out.append((got.status_code, got.json()))
            return out
        finally:
            await desk.stop()

    return run(go())


# ------------------------------------------------------------------ D2: /api/profile/settings

# a value of each key in the table, none of them the tests' profile's own
NEW_VALUES = {"submit_mode": "dry_run", "manage_accounts": True, "accept_notices": False, "accept_cookies": True,
              "learn_from_responses": False, "email_codes": True, "email_tracking": True, "follow_up_days": 14,
              "desk_window": "tab", "browser_channel": "msedge", "cover_letter_model": "claude-sonnet-5:beta",
              "phone_alerts_server": "https://ntfy.example.com"}


def _with_comments(home: Path) -> str:
    """The tests' profile as a person keeps it: a comment at its top, and comments on settings' own lines."""
    text = yaml.safe_dump(yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8")), sort_keys=False)
    text = "# Sam's profile, edited by hand\n" + text
    text = text.replace("  submit_mode: review\n", "  submit_mode: review  # practice first\n")
    text = text.replace("  headless: true\n", "  headless: true  # keep the window hidden\n")
    assert "# practice first" in text and "# keep the window hidden" in text
    (home / "profile.yaml").write_text(text, encoding="utf-8")
    return text


def test_profile_settings_writes_each_key_of_its_table_and_keeps_the_rest(srv, job_apply_home):
    """D7 test 1: each key in D2's table is written to profile.yaml's settings with the value the page sends; every
    other line and comment stays as written; the answer is the settings as they now stand; and the state the page
    draws from has the eight keys D2 adds, and the documents' file names (never their paths)."""
    before_text = _with_comments(job_apply_home)
    before = yaml.safe_load(before_text)
    assert set(NEW_VALUES) == set(PROFILE_SETTINGS)
    desk = _desk(srv)
    asks = [("POST", "/api/profile/settings", {"key": k, "value": v}) for k, v in NEW_VALUES.items()]
    asks += [("POST", "/api/profile/settings", {"key": "cover_letter_model", "value": ""}),
             ("POST", "/api/profile/settings", {"key": "cover_letter_model",
                                                "value": NEW_VALUES["cover_letter_model"]}),
             ("GET", "/api/state", None)]
    said = _calls(desk, asks)
    for (status, answer), (key, value) in zip(said, NEW_VALUES.items()):
        assert status == 200, (key, answer)
        assert answer[key] == value and set(answer) == set(PROFILE_SETTINGS), (key, answer)
    assert said[len(NEW_VALUES)][1]["cover_letter_model"] == ""
    assert said[-2][1] == NEW_VALUES
    after_text = (job_apply_home / "profile.yaml").read_text(encoding="utf-8")
    after = yaml.safe_load(after_text)
    assert after["settings"] == {**before["settings"], **NEW_VALUES}
    assert {k: v for k, v in after.items() if k != "settings"} == {k: v for k, v in before.items() if k != "settings"}
    assert after_text.startswith("# Sam's profile, edited by hand\n")
    assert "  submit_mode: \"dry_run\"  # practice first\n" in after_text
    assert "  headless: true  # keep the window hidden\n" in after_text
    # (and the desk reads them so: the state's settings, with the keys D2 adds)
    status, state = said[-1]
    assert status == 200
    shown = state["settings"]
    for key in ("accept_notices", "accept_cookies", "email_codes", "email_tracking", "desk_window", "browser_channel",
                "cover_letter_model", "phone_alerts_server", "follow_up_days", "learn_from_responses"):
        assert shown[key] == NEW_VALUES[key], key
    assert shown["submit_mode"] == "dry_run"
    assert state["profile"]["documents"] == {"resume": "resume.pdf", "cover_letter": None}
    assert str(job_apply_home) not in json.dumps(state["profile"]["documents"])


@pytest.mark.parametrize("key, value, said", [
    ("colour", "blue", CANT),
    ("auto_submit_ats", ["workday"], CANT),
    ("headless", False, CANT),
    ("jobs_at_once", 3, CANT),
    ("phone_summary_at", "07:00", CANT),
    ("phone_alert_details", True, CANT),
    (None, True, CANT),
    ("submit_mode", "auto", "Practice mode is either on or off."),
    ("follow_up_days", 0, "Follow-up days is a whole number from 1 to 90."),
    ("follow_up_days", 91, "Follow-up days is a whole number from 1 to 90."),
    ("follow_up_days", "ten", "Follow-up days is a whole number from 1 to 90."),
    ("follow_up_days", True, "Follow-up days is a whole number from 1 to 90."),
    ("follow_up_days", 10.5, "Follow-up days is a whole number from 1 to 90."),
    ("desk_window", "popup", "The desk opens in its own window or in a browser tab."),
    ("browser_channel", "firefox", "The desk fills forms in Chrome, Edge or its own browser."),
    ("manage_accounts", "true", "That switch is either on or off."),
    ("accept_cookies", 1, "That switch is either on or off."),
    ("cover_letter_model", "claude sonnet", None),
    ("cover_letter_model", "x" * 81, None),
    ("cover_letter_model", "-p", None),
    ("cover_letter_model", None, None),
    ("phone_alerts_server", "http://ntfy.example.com", None),
    ("phone_alerts_server", "https://sam:secret@ntfy.example.com", None),
])
def test_profile_settings_refuses_what_its_table_doesnt_take_and_changes_nothing(srv, job_apply_home, key, value,
                                                                                 said):
    """D7 test 2: an unknown key, one the route never writes (auto_submit_ats, headless, jobs_at_once, the phone alerts'
    two), submit_mode "auto", follow_up_days of 0, 91 and "ten", a desk_window of "popup", a model with a space in it:
    each a 400 with one plain sentence, and profile.yaml as it was."""
    before = (job_apply_home / "profile.yaml").read_bytes()
    [(status, answer)] = _calls(_desk(srv), [("POST", "/api/profile/settings", {"key": key, "value": value})])
    assert status == 400, answer
    if said is not None:
        assert answer == {"error": said}
    else:
        assert answer["error"].endswith(("for the usual one.", "to use ntfy.sh.")), answer
        assert "secret" not in answer["error"]
    assert (job_apply_home / "profile.yaml").read_bytes() == before


def test_profile_settings_needs_the_key_and_a_profile(srv, job_apply_home):
    """D7 test 2: without the page's key, 403; with no profile yet, 400 "Set up your profile first.", and none is
    made. A body that isn't an object is a 400."""
    before = (job_apply_home / "profile.yaml").read_bytes()
    desk = _desk(srv)
    ask = ("POST", "/api/profile/settings", {"key": "follow_up_days", "value": 14})
    assert [s for s, _ in _calls(desk, [ask], token=False)] == [403]
    assert (job_apply_home / "profile.yaml").read_bytes() == before
    assert _calls(_desk(srv), [("POST", "/api/profile/settings", "[1, 2]")])[0][0] == 400
    assert _calls(_desk(srv), [ask], started=(job_apply_home / "profile.yaml").unlink) == [(400, {"error": NO_PROFILE})]
    assert not (job_apply_home / "profile.yaml").exists()


# ------------------------------------------------------------------ D3: /api/mail/test

@pytest.fixture
def imap(monkeypatch):
    FakeImap.reset()
    monkeypatch.setattr(mailbox.imaplib, "IMAP4_SSL", FakeImap)
    monkeypatch.setitem(mailbox.IMAP_HOSTS, "example.com", "imap.example.com")
    config.save_site_password("email_password", APP_PASSWORD)
    return FakeImap


def _mail_test(srv) -> tuple[int, dict]:
    return _calls(_desk(srv), [("POST", "/api/mail/test", {})])[0]


def _only_sign_in_and_out(imap) -> None:
    """The fake server saw a sign-in and a sign-out, and no command that opens a mailbox or reads a message."""
    [box] = imap.instances
    assert box.calls == [("login", ADDRESS), ("logout",)], box.calls


def test_mail_test_signs_in_and_out_and_reads_nothing(srv, imap):
    """D7 test 7: a working sign-in answers ok; the fake mail server sees a sign-in and a sign-out, nothing else."""
    imap.messages = [(time.time(), b"From: x@example.com\r\nSubject: Your code is 123456\r\n\r\nhi\r\n")]
    assert _mail_test(srv) == (200, {"ok": True})
    _only_sign_in_and_out(imap)


def test_mail_test_says_a_refused_password_in_its_sentence(srv, imap):
    """D7 test 7: a refused password: "your email provider refused the password"; signed out, nothing read."""
    imap.accept = False
    assert _mail_test(srv) == (200, {"ok": False, "why": "your email provider refused the password"})
    _only_sign_in_and_out(imap)


def test_mail_test_says_an_address_the_desk_cant_read(srv, imap, monkeypatch):
    """D7 test 7: an address whose mail the desk can't read: "the desk can't read that kind of address", and no
    server is reached."""
    monkeypatch.delitem(mailbox.IMAP_HOSTS, "example.com")
    assert _mail_test(srv) == (200, {"ok": False, "why": "the desk can't read that kind of address"})
    assert imap.instances == []


def test_mail_test_says_a_provider_that_doesnt_answer_and_gives_up_in_time(srv, imap, monkeypatch):
    """D7 test 7: a provider that can't be reached, and one that takes the connection and never answers the sign-in:
    "your email provider didn't answer", the second within the test's wait (10 seconds in the desk; shortened here),
    never a mailbox opened."""
    class Unreachable(FakeImap):
        def __init__(self, host, timeout=None, ssl_context=None):
            raise TimeoutError("timed out")

    monkeypatch.setattr(mailbox.imaplib, "IMAP4_SSL", Unreachable)
    assert _mail_test(srv) == (200, {"ok": False, "why": "your email provider didn't answer"})

    released = threading.Event()

    class Silent(FakeImap):
        def login(self, user, password):
            self.calls.append(("login", user))
            released.wait(5)  # (a server that never answers: let go once the test has its answer)

    assert mailbox.SIGN_IN_TEST_WAIT == 10
    monkeypatch.setattr(mailbox, "SIGN_IN_TEST_WAIT", 0.5)
    monkeypatch.setattr(mailbox.imaplib, "IMAP4_SSL", Silent)
    began = time.monotonic()
    try:
        assert _mail_test(srv) == (200, {"ok": False, "why": "your email provider didn't answer"})
        assert time.monotonic() - began < 4
    finally:
        released.set()
    assert all(call[0] in ("login", "logout") for box in imap.instances for call in box.calls)


def test_mail_test_never_lets_the_password_or_the_address_out(srv, imap, caplog, monkeypatch):
    """D7 test 8: whatever happens, the password and the address are in no answer and no log line; and with no app
    password saved, the test asks for one."""
    caplog.set_level(logging.DEBUG)
    answers = []
    for accept in (True, False):
        imap.accept = accept
        answers.append(_mail_test(srv))
    imap.accept, imap.login_error = True, OSError(f"connection reset while signing in {ADDRESS} with {APP_PASSWORD}")
    answers.append(_mail_test(srv))
    monkeypatch.delitem(mailbox.IMAP_HOSTS, "example.com")
    answers.append(_mail_test(srv))
    assert [a[1]["ok"] for a in answers] == [True, False, False, False]
    said = json.dumps(answers) + "\n".join(r.getMessage() for r in caplog.records)
    for secret in (APP_PASSWORD, ADDRESS, "sam.rivera"):
        assert secret not in said, secret
    monkeypatch.setattr(desk_module, "_secret", lambda name: None)  # (none saved)
    assert _mail_test(srv) == (400, {"error": "Save your email's app password first."})


def test_mail_test_needs_the_key(srv, imap):
    assert [s for s, _ in _calls(_desk(srv), [("POST", "/api/mail/test", {})], token=False)] == [403]
    assert imap.instances == []


# ------------------------------------------------------------------ D3: /api/profile/document

PDF = b"%PDF-1.4\n% a made-up cover letter\n"


def _make_docx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("word/document.xml", (2026, 1, 1, 0, 0, 0)), "<w:document/>")
    return out.getvalue()


DOCX = _make_docx()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _profile(home: Path) -> dict:
    return yaml.safe_load((home / "profile.yaml").read_text(encoding="utf-8"))


def test_the_usual_cover_letter_is_kept_as_the_resume_is(srv, job_apply_home):
    """D7 test 9: a PDF and a Word file, each saved as the usual cover letter in the desk's own folder (never under the
    name or the folder the page sends) with documents.cover_letter set to it; remove empties that line and leaves the
    file; the answers and the state name the files, never their paths."""
    home = job_apply_home
    said = _calls(_desk(srv), [
        ("POST", "/api/profile/document", {"kind": "cover_letter", "name": "../../My Letter.PDF", "data": _b64(PDF)}),
        ("GET", "/api/state", None)])
    assert said[0][0] == 200 and said[0][1]["documents"] == {"resume": "resume.pdf", "cover_letter": "cover_letter.pdf"}
    assert said[1][1]["profile"]["documents"]["cover_letter"] == "cover_letter.pdf"
    assert (home / "cover_letter.pdf").read_bytes() == PDF
    assert config.expand(_profile(home)["documents"]["cover_letter"]) == home / "cover_letter.pdf"
    assert not (home.parent / "My Letter.PDF").exists() and not (home.parent.parent / "My Letter.PDF").exists()
    assert str(home) not in json.dumps(said[0][1])

    said = _calls(_desk(srv), [("POST", "/api/profile/document",
                                {"kind": "cover_letter", "name": "letter.docx", "data": _b64(DOCX)})])
    assert said[0][0] == 200 and said[0][1]["documents"]["cover_letter"] == "cover_letter.docx"
    assert config.expand(_profile(home)["documents"]["cover_letter"]) == home / "cover_letter.docx"

    said = _calls(_desk(srv), [("POST", "/api/profile/document", {"kind": "cover_letter", "remove": True})])
    assert said == [(200, {"documents": {"resume": "resume.pdf", "cover_letter": None},
                           "version": profile_setup.profile_version()})]
    assert _profile(home)["documents"]["cover_letter"] == ""
    assert (home / "cover_letter.docx").read_bytes() == DOCX and (home / "cover_letter.pdf").exists()
    assert _profile(home)["documents"]["resume"]  # (the resume's line stays)


@pytest.mark.parametrize("body, said", [
    ({"kind": "cover_letter", "name": "letter.txt", "data": _b64(b"Dear hiring manager")},
     "Pick your cover letter as a PDF or a Word (.docx) file"),
    ({"kind": "cover_letter", "name": "letter.exe", "data": _b64(PDF)},
     "Pick your cover letter as a PDF or a Word (.docx) file"),
    ({"kind": "cover_letter", "name": "letter.pdf", "data": _b64(b"MZ not a pdf")},
     "That file isn't a PDF inside: save your cover letter as a PDF and pick that"),
    ({"kind": "cover_letter", "name": "letter.docx", "data": _b64(b"PK not a zip")},
     "That file isn't a Word document inside: save your cover letter as .docx or PDF and pick that"),
    ({"kind": "cover_letter", "name": "letter.pdf", "data": ""}, "That file is empty"),
    ({"kind": "cover_letter", "name": "letter.pdf", "data": "%%% not base64"},
     "The file didn't come through whole: pick it again"),
    ({"kind": "cover_letter", "name": "letter.pdf", "data": _b64(PDF + b"x" * 64)},
     "That file is bigger than 0 MB: pick a smaller copy"),
    ({"kind": "resume", "name": "resume.pdf", "data": _b64(PDF)}, "Only the usual cover letter is saved here."),
    ({"kind": "transcripts", "remove": True}, "Only the usual cover letter is saved here."),
    ({"name": "letter.pdf", "data": _b64(PDF)}, "Only the usual cover letter is saved here."),
])
def test_the_cover_letter_route_refuses_what_save_resume_refuses_and_any_other_kind(srv, job_apply_home, monkeypatch,
                                                                                  body, said):
    """D7 test 9: what save_resume refuses (a file that isn't a PDF or a Word file by its name or inside, an empty one,
    one too big, one that didn't come through whole), and any kind but the cover letter: a 400 with its sentence, and
    nothing written."""
    monkeypatch.setattr(profile_setup, "RESUME_MOST", 64)  # (a small limit for the test: the same check)
    before = (job_apply_home / "profile.yaml").read_bytes()
    assert _calls(_desk(srv), [("POST", "/api/profile/document", body)]) == [(400, {"error": said})]
    assert (job_apply_home / "profile.yaml").read_bytes() == before
    assert not list(job_apply_home.parent.rglob("cover_letter*")) and not list(job_apply_home.parent.rglob("letter*"))


def test_the_cover_letter_route_needs_the_key_and_a_profile(srv, job_apply_home):
    ask = ("POST", "/api/profile/document", {"kind": "cover_letter", "name": "l.pdf", "data": _b64(PDF)})
    assert [s for s, _ in _calls(_desk(srv), [ask], token=False)] == [403]
    assert not (job_apply_home / "cover_letter.pdf").exists()
    for body in (ask[2], {"kind": "cover_letter", "remove": True}):
        assert _calls(_desk(srv), [("POST", "/api/profile/document", body)],
                      started=lambda: (job_apply_home / "profile.yaml").unlink(missing_ok=True)) == [
            (400, {"error": NO_PROFILE})]
    assert not (job_apply_home / "cover_letter.pdf").exists() and not (job_apply_home / "profile.yaml").exists()


# ------------------------------------------------------------------ D3: /api/open/home

def test_open_home_opens_the_desks_own_folder_whatever_the_request_holds(srv, job_apply_home, monkeypatch, tmp_path):
    """D7 test 10: Open the folder opens the desk's own folder, and a body with a path in it (or any other) changes
    nothing about what opens. Without the page's key, nothing opens."""
    opened: list[Path] = []
    monkeypatch.setattr(window, "open_folder", lambda folder: opened.append(folder) or True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    asks = [("POST", "/api/open/home", None), ("POST", "/api/open/home", {}),
            ("POST", "/api/open/home", {"path": str(elsewhere)}),
            ("POST", "/api/open/home", {"folder": str(elsewhere), "url": elsewhere.as_uri()}),
            ("POST", "/api/open/home", "not json")]
    said = _calls(_desk(srv), asks)
    assert said == [(200, {"opened": True})] * len(asks)
    assert opened == [config.home()] * len(asks) and config.home() == job_apply_home
    assert [s for s, _ in _calls(_desk(srv), asks[:1], token=False)] == [403]
    assert len(opened) == len(asks)
    monkeypatch.setattr(window, "open_folder", lambda folder: False)
    assert _calls(_desk(srv), asks[:1]) == [(502, {"error": "The folder couldn't be opened."})]


def test_open_folder_starts_the_systems_file_manager_on_that_folder_and_no_shell(monkeypatch, tmp_path):
    """window.open_folder: the file manager's own program with the folder's whole path as its one argument (macOS'
    open, Linux's xdg-open; Windows' os.startfile), never through a shell; a folder that isn't there opens nothing."""
    started: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(window.subprocess, "Popen", lambda args, **kw: started.append((args, kw)))
    if sys.platform == "win32":
        monkeypatch.setattr(window.os, "startfile", lambda path: started.append(([path], {})))
        assert window.open_folder(tmp_path) is True
        assert started == [([str(tmp_path.resolve())], {})]
    else:
        for system, program in (("linux", "xdg-open"), ("macos", "open")):
            monkeypatch.setattr(window, "_system", lambda system=system: system)
            started.clear()
            assert window.open_folder(tmp_path) is True
            [(args, kw)] = started
            assert args == [program, str(tmp_path.resolve())] and not kw.get("shell"), (args, kw)
    started.clear()
    assert window.open_folder(tmp_path / "not-there") is False and started == []
