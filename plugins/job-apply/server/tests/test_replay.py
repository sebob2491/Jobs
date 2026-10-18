"""Offline replay (the review of Oct 18, T10): `job-apply-doctor --replay` opens a saved page in the bundled Chromium with
no network and says what the Job Desk reads and decides there. Every page in tests/fixtures/live replays offline and
says the same as its golden in tests/fixtures/replay (an intended change is written again with
JOB_APPLY_UPDATE_REPLAY=1: look the diff over)."""

import http.server
import json
import os
import threading
from pathlib import Path

import pytest
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


class _Counting(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # (http.server's name for it)
        _Counting.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<p>from the network</p>")

    def log_message(self, *args):
        pass


def test_a_replay_reaches_nothing_on_the_network(loop, tmp_path):
    """A saved page that names things on the network (an image, a style sheet, a frame, a script's fetch and web
    socket): none of them is fetched (a server on this computer, standing in for the site, is asked for nothing), and
    the replay says how many it refused."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Counting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = f"http://127.0.0.1:{server.server_address[1]}"
    _Counting.hits = []
    page = tmp_path / "remote.html"
    page.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>Apply</title>
<link rel="stylesheet" href="{site}/style.css"></head><body><h1>Apply</h1>
<img src="{site}/logo.png" alt=""><iframe src="{site}/frame.html"></iframe>
<label for="e">Email</label><input id="e" required><button type="button">Next</button>
<script>fetch("{site}/beacon").catch(() => {{}}); try {{ new WebSocket("{site.replace('http', 'ws')}/ws"); }} catch (e) {{}}</script>
</body></html>""", encoding="utf-8")
    try:
        result = run(_replayed(page))
    finally:
        server.shutdown()
    assert _Counting.hits == []
    assert result["refused"] >= 4, result["refused"]
    assert [(b["label"], b["plan"], b.get("value")) for b in result["boxes"]] == [("Email", "fill", "<email>")]
    assert result["next"] == "Next" and result["address"] == ""


def test_a_debug_folder_is_scrubbed_and_replayed_for_the_profile_named(loop, tmp_path, job_apply_home):
    """A job's debug folder (the page a stop saved, with what the person typed and their details on it) is scrubbed
    as a fixture is before it's opened, and answered for the profile named: each box by its rule, never a value; and
    nothing of the person's is in what's said."""
    folder = tmp_path / "20261018-101500-000-stop"
    folder.mkdir()
    (folder / "page.html").write_text("""<!doctype html><html><head><title>Apply</title></head><body>
<h1>My Information</h1><p>Welcome back, Sam Rivera (sam.rivera@example.com)</p>
<label for="p">Phone Number</label><input id="p" value="480-555-0123" required>
<label for="c">City</label><input id="c" value="Chandler">
<div role="alert">Phone Number 480-555-0123 is not valid</div>
<button type="button">I Accept the Privacy Notice</button></body></html>""", encoding="utf-8")
    (folder / "snapshot.json").write_text(json.dumps({
        "url": "https://jobs.example.com/apply/123?token=abc", "title": "Apply", "note": "stopped: stuck",
        "frames": [{"file": "page.html", "url": "https://jobs.example.com/apply/123?token=abc"}],
        "fields": []}), encoding="utf-8")
    result = run(_replayed(folder, job_apply_home / "profile.yaml"))
    said = json.dumps(result)
    for private in ("Sam", "Rivera", "sam.rivera", "480-555-0123", "token=abc"):
        assert private not in said, private
    assert result["applicant"] == "the profile named" and result["address"] == "https://jobs.example.com/apply/123"
    boxes = {b["label"]: b for b in result["boxes"]}
    assert boxes["Phone Number"]["plan"] == "fill" and boxes["Phone Number"]["value"] == "<phone>"
    assert boxes["City"]["value"] == "<city>"
    assert result["agreement"]["button"] == "I Accept the Privacy Notice" and result["agreement"]["terms_gate"]
    assert result["agreement"]["never"] is None
    assert result["errors"] and "REDACTED" in result["errors"][0]


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
