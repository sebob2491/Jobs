"""Offline replay (the review of Oct 18, T10): `job-apply-doctor --replay` opens a saved page in the bundled Chromium
with no network and says what the Job Desk reads and decides there. Every page in tests/fixtures/live replays offline
and says the same as its golden in tests/fixtures/replay (an intended change is written again with
JOB_APPLY_UPDATE_REPLAY=1: look the diff over)."""

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import yaml
from conftest import FIXTURES, browser_available, run, worker_browser

from job_apply import doctor, replay

LIVE = sorted((FIXTURES / "live").glob("*.expect.json"))
GOLDEN = FIXTURES / "replay"

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


async def _replayed(path: Path, profile: Path | None = None) -> dict:
    return await replay.replay(path, profile, browser=await worker_browser())


@pytest.mark.parametrize("expect_path", LIVE, ids=[p.name.removesuffix(".expect.json") for p in LIVE])
def test_every_live_page_replays_offline_as_its_golden_says(loop, expect_path):
    name = expect_path.name.removesuffix(".expect.json")
    result = run(_replayed(expect_path.with_name(f"{name}.html")))
    assert result["refused"] == 0, result["refused"]  # (a fixture is kept whole: nothing of it is on the network)
    golden = GOLDEN / f"{name}.json"
    if os.environ.get("JOB_APPLY_UPDATE_REPLAY") == "1":
        GOLDEN.mkdir(exist_ok=True)
        golden.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return
    assert golden.is_file(), f"no golden for {name}: write it with JOB_APPLY_UPDATE_REPLAY=1 and look it over"
    assert result == json.loads(golden.read_text(encoding="utf-8"))


def test_every_golden_has_its_page():
    assert sorted(p.stem for p in GOLDEN.glob("*.json")) == [p.name.removesuffix(".expect.json") for p in LIVE]


class _Listener:
    """A port on this computer standing in for the site: every connection made to it counted (a raw TCP accept, before
    anything is asked: a frame's address is connected to before a request is routed), and closed."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(50)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # (and a STUN request a script's WebRTC sends)
        self.udp.bind(("127.0.0.1", self.port))
        self.udp.settimeout(0.2)
        self.accepts = self.datagrams = 0
        self.done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()
        threading.Thread(target=self._hear, daemon=True).start()

    def _hear(self) -> None:
        while not self.done.is_set():
            try:
                self.udp.recvfrom(2000)
            except OSError:
                continue
            self.datagrams += 1

    def _serve(self) -> None:
        while not self.done.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.accepts += 1
            conn.close()

    def close(self) -> None:
        self.done.set()
        self.sock.close()
        self.udp.close()


REMOTE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Apply</title>
<link rel="stylesheet" href="{site}/style.css"></head><body><h1>Apply</h1>
<img src="{site}/logo.png" alt=""><iframe src="{site}/frame.html"></iframe>
<label for="e">Email</label><input id="e" required><button type="button">Next</button>
<script>fetch("{site}/beacon").catch(() => {{}}); try {{ new WebSocket("{ws}/ws"); }} catch (e) {{}}
try {{ const pc = new RTCPeerConnection({{iceServers: [{{urls: "stun:127.0.0.1:{port}"}}]}});
pc.createDataChannel("x"); pc.createOffer().then(o => pc.setLocalDescription(o)); }} catch (e) {{}}</script>
</body></html>"""


@pytest.mark.parametrize("fixture", [False, True], ids=["a page", "a fixture's page"])
def test_a_replay_connects_to_nothing_on_the_network(loop, tmp_path, fixture):
    """A saved page that names things on the network (a style sheet, an image, a frame, a script's fetch, web socket
    and WebRTC): nothing connects to the site (a port here standing in for it accepts no connection at all, and hears
    no datagram). A page is
    cleaned of them before it's opened; a fixture's page (an expect.json beside it, so cleaned when it was made) is
    opened as it is, with no script run and its requests refused, and the replay says how many it refused."""
    server = _Listener()
    site = f"http://127.0.0.1:{server.port}"
    page = tmp_path / "remote.html"
    page.write_text(REMOTE_PAGE.format(site=site, ws=site.replace("http", "ws"), port=server.port), encoding="utf-8")
    if fixture:
        (tmp_path / "remote.expect.json").write_text(json.dumps({"source_url": f"{site}/apply", "fields": []}),
                                                     encoding="utf-8")
    try:
        result = run(_replayed(page))
        time.sleep(0.5)  # (a connection late to come)
    finally:
        server.close()
    assert (server.accepts, server.datagrams) == (0, 0)
    assert (result["refused"] >= 2) if fixture else (result["refused"] == 0), result["refused"]
    assert [(b["label"], b["plan"], b.get("value")) for b in result["boxes"]] == [("Email", "fill", "<email>")]
    assert result["next"] == "Next" and result["address"] == (f"{site}/apply" if fixture else "")


STOP_PAGE = """<!doctype html><html><head><title>Apply</title></head><body>
<h1>My Information</h1><p>Welcome back, Sam Rivera (sam.rivera@example.com), or is it Alex Quill?</p>
<label for="p">Phone Number</label><input id="p" value="480-555-0123" required>
<label for="c">City</label><input id="c" value="Chandler">
<div role="alert">Phone Number 480-555-0123 is not valid</div>
<button type="button">I Accept the Privacy Notice</button></body></html>"""
# (another applicant's profile, named for the replay: made up, as every value here is)
OTHER_PROFILE = {"personal": {"first_name": "Alex", "last_name": "Quill", "email": "alex.quill@example.org",
                              "phone": "602-555-0144", "address": {"city": "Mesa", "state": "AZ"}}}


@pytest.mark.parametrize("named", ["home", "other"])
@pytest.mark.parametrize("given", ["folder", "page.html"])
def test_a_debug_folder_is_scrubbed_and_replayed_for_the_profile_named(loop, tmp_path, job_apply_home, named, given):
    """A job's debug folder (the page a stop saved, with what the person typed and their details on it; or its
    page.html named) is scrubbed as a fixture is before it's opened, and answered for the profile named: each box by
    its rule, never a value. Nothing of the person's (the desk's own profile's) is in what's said, whichever profile is
    named, and nothing of the named one's either (the review of T10: one named scrubbed only its own)."""
    folder = tmp_path / "20261018-101500-000-stop"
    folder.mkdir()
    (folder / "page.html").write_text(STOP_PAGE, encoding="utf-8")
    (folder / "snapshot.json").write_text(json.dumps({
        "url": "https://jobs.example.com/apply/123?token=abc", "title": "Apply", "note": "stopped: stuck",
        "frames": [{"file": "page.html", "url": "https://jobs.example.com/apply/123?token=abc"}],
        "fields": []}), encoding="utf-8")
    profile = job_apply_home / "profile.yaml"
    if named == "other":
        profile = tmp_path / "other.yaml"
        profile.write_text(yaml.safe_dump(OTHER_PROFILE), encoding="utf-8")
    result = run(_replayed(folder if given == "folder" else folder / "page.html", profile))
    said = json.dumps(result)
    for private in ("Sam", "Rivera", "sam.rivera", "480-555-0123", "token=abc",
                    *(("Quill", "alex.quill") if named == "other" else ())):
        assert private not in said, private
    assert result["applicant"] == "the profile named" and result["address"] == "https://jobs.example.com/apply/123"
    boxes = {b["label"]: b for b in result["boxes"]}
    assert boxes["Phone Number"]["plan"] == "fill" and boxes["Phone Number"]["value"] == "<phone>"
    assert boxes["City"]["value"] == "<city>"
    assert result["agreement"]["button"] == "I Accept the Privacy Notice" and result["agreement"]["terms_gate"]
    assert result["agreement"]["never"] is None
    assert result["errors"] and "REDACTED" in result["errors"][0]


def test_a_page_that_isnt_a_fixture_is_cleaned_before_its_opened(loop, tmp_path):
    """A .html with no expect.json beside it (a page saved some other way) is cleaned before it's opened: the person's
    details are taken out of its words, as they are of a debug folder's."""
    page = tmp_path / "saved.html"
    page.write_text(STOP_PAGE, encoding="utf-8")
    said = json.dumps(run(_replayed(page)))
    for private in ("Sam", "Rivera", "sam.rivera", "480-555-0123"):
        assert private not in said, private


def test_the_agreement_verdict_names_what_is_never_agreed_to(loop, tmp_path):
    """A terms gate whose page also waives a right (arbitration) is the person's, and the replay says which kind."""
    page = tmp_path / "gate.html"
    page.write_text("""<!doctype html><html><head><title>Privacy Agreement</title></head><body>
<h1>Privacy Agreement</h1><p>By continuing you agree to binding arbitration of any dispute and waive a jury trial.</p>
<button type="button">I Accept</button></body></html>""", encoding="utf-8")
    result = run(_replayed(page))
    assert result["agreement"]["terms_gate"] is False and result["agreement"]["verdict"] == "the person's"
    assert result["agreement"]["never"] == "arbitration", result["agreement"]
    assert "it holds what's never agreed to (arbitration)" in replay.text(result)


def test_the_doctor_replays_a_page_and_says_so_in_words_or_json(capsys):
    """`job-apply-doctor --replay <page>` (and --json): its own browser, started and stopped for it."""
    page = FIXTURES / "live" / "pipeline-kforce.html"
    assert doctor.main(["--replay", str(page), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["file"] == "pipeline-kforce.html" and result["refused"] == 0
    assert result == json.loads((GOLDEN / "pipeline-kforce.json").read_text(encoding="utf-8"))
    assert doctor.main(["--replay", str(page)]) == 0
    words = capsys.readouterr().out
    assert words.startswith("Replay of pipeline-kforce.html (offline: 0 request(s) to the network refused)")
    assert "Answered for: the live check's test applicant" in words and "Boxes (" in words
    assert doctor.main(["--replay", str(FIXTURES / "live" / "README.md")]) == 1
    assert "isn't a saved page" in capsys.readouterr().err
    # (a profile named that isn't there: said plainly, before anything opens, and the home folder said as ~)
    missing = Path.home() / "no-such-profile.yaml"
    assert doctor.main(["--replay", str(page), "--profile", str(missing)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("There's no profile file at ~") and str(Path.home()) not in err, err


def test_the_doctor_says_what_to_run_when_its_chromium_isnt_there(capsys, monkeypatch):
    from playwright.async_api import Error as PlaywrightError

    async def missing(*args, **kw):
        raise PlaywrightError(f"BrowserType.launch: Executable doesn't exist at {Path.home()}/.cache/ms-playwright/x")

    monkeypatch.setattr(replay, "replay", missing)
    assert doctor.main(["--replay", str(FIXTURES / "live" / "pipeline-kforce.html")]) == 1
    err = capsys.readouterr().err
    assert "the plugin's own Chromium, and it isn't installed: run `uv run --project" in err, err
    assert "playwright install chromium" in err and str(Path.home()) + "/" not in err, err
