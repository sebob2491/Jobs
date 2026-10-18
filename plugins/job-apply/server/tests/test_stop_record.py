"""The desk's last action and the stop's record (the review of Oct 18, T10): what the desk last pressed, typed, picked
or uploaded on a job's pages (Run.last_action, by the button's words or the box's label alone), kept in the desk's state,
the saved stop page's details and a problem report; and stop.json beside the page a job stopped on, with its code,
need, reason, that last action, the page's errors and the words around the first, scrubbed as a report is."""

import ast
import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import browser_available, run

import job_apply.pipeline as pipeline
from job_apply import report
from job_apply.pipeline import Applier, Run

PIPELINE = Path(pipeline.__file__)

# A step whose Next doesn't move on (its error says the resume is wanted), on a page that also shows the person's own
# email and phone (as a signed-in site does) beside the error, and echoes what was typed into the form
STUCK_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Apply</title></head>
<body>
<h1>Apply</h1>
<p>Signed in as sam.rivera@example.com, phone 480-555-0123.</p>
<h2>Additional Attachments</h2>
<form id="f" novalidate>
  <label for="fn">First Name</label><input id="fn" required>
  <div id="err" role="alert"></div>
  <p id="echo"></p>
  <button type="button" id="next">Next</button>
</form>
<script>
  window.presses = 0;
  document.getElementById('next').addEventListener('click', () => {
    window.presses += 1;
    document.getElementById('err').textContent = '* Resume/CV is required before this step is saved';
    document.getElementById('echo').textContent = 'You wrote: ' + document.getElementById('fn').value + ' Riverbend';
  });
</script>
</body></html>
"""


async def until(condition, timeout=60, about=None):
    start = time.monotonic()
    while not condition():
        if time.monotonic() - start > timeout:
            raise AssertionError("timed out waiting" + (f": {about()!r}" if about else ""))
        await asyncio.sleep(0.2)


@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_a_step_not_saved_stop_writes_its_record_with_the_button_pressed_and_the_error(srv, monkeypatch, tmp_path):
    """A planted step that won't save: the desk presses Next until it stops ("The page didn't move on",
    step_not_saved), and beside the page it saved, stop.json names the code, the button it pressed ("Next") and the
    page's error with the words around it; the person's email and phone the page shows, and the name typed into the
    form that it echoes, are taken out. The desk's state has the last action; so has the saved page's details."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    page = tmp_path / "stuck.html"
    page.write_text(STUCK_PAGE, encoding="utf-8")
    job = srv.add_job(url=page.as_uri(), title="Technician", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running") and applier.current is None,
                        about=lambda: (r.status, r.reason, r.log[-5:]))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need, r.code) == ("needs_you", "stuck", "step_not_saved"), (r.reason, r.log)
    assert r.last_action["what"] == "pressed" and r.last_action["target"] == "Next", r.last_action
    assert r.public()["last_action"] == r.last_action
    [stop] = (Path(job["folder"]) / "debug").glob(f"*-{pipeline.STOP}")
    record = json.loads((stop / report.STOP_RECORD).read_text(encoding="utf-8"))
    assert (record["code"], record["need"]) == ("step_not_saved", "stuck")
    assert record["last_action"]["what"] == "pressed" and record["last_action"]["target"] == "Next"
    assert record["errors"] and "Resume/CV is required" in record["errors"][0]
    assert "Resume/CV is required before this step is saved" in record["text"] and len(record["text"]) <= report.STOP_TEXT
    assert record["reason"].startswith("The page didn't move on")
    said = json.dumps(record)
    for private in ("sam.rivera", "480-555-0123", "4805550123", "Sam Riverbend"):
        assert private not in said, (private, record)
    assert "REDACTED" in record["text"]  # (the page's own words kept, the person's taken out)
    details = json.loads((stop / "snapshot.json").read_text(encoding="utf-8"))["details"]
    assert details["last_action"] == record["last_action"]


def test_the_last_action_is_a_label_never_a_value():
    """Run.did keeps a button's words or a box's label, never what went in (a fill's entries carry their values, which
    it doesn't read), says what was done by the box's kind where the call leaves it to the box, and does nothing for no
    target, an unknown kind of action, or a target it can't read."""
    r = Run(1)
    r.did("pressed", {"id": "a1", "text": "Save and Continue ›", "label": "save"})
    assert (r.last_action["what"], r.last_action["target"]) == ("pressed", "Save and Continue")
    page = {"fields": [{"id": "f1", "label": "Mobile Phone", "kind": "text"},
                       {"id": "f2", "label": "State", "kind": "select"},
                       {"id": "f3", "label": "Resume", "kind": "file"}]}
    for entry, said in (({"id": "f1", "value": "480-555-0123"}, ("typed", "Mobile Phone")),
                        ({"id": "f2", "label": "State", "value": "Arizona"}, ("picked", "State")),
                        ({"id": "f3", "label": "Resume", "value": "/home/x/resume.pdf", "uploaded": True},
                         ("uploaded", "Resume"))):
        r.did("", [{"id": "f0", "label": "Earlier box", "value": "x"}, entry], page)
        assert (r.last_action["what"], r.last_action["target"]) == said
        assert entry["value"] not in json.dumps(r.last_action)
    before = dict(r.last_action)
    for what, target in (("pressed", None), ("pressed", []), ("pressed", {"id": "x"}), ("hovered", "Next"),
                         ("typed", 42), ("", object())):
        r.did(what, target)
        assert r.last_action == before, (what, target)
    assert set(r.public()) >= {"last_action"} and r.public()["last_action"] == before


def test_every_recording_of_the_last_action_is_a_statement_of_its_own():
    """The last action is a record only (T10): each Run.did call in the driver is a statement of its own (nothing reads
    what it gives back), and its arguments wait for nothing, assign nothing and call nothing but reads (a dict's get):
    so taking each such line away leaves the driver as it decides. Run.did itself never raises."""
    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    statements = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "did"]
    assert len(calls) >= 40  # (where the desk clicks and fills)
    for call in calls:
        assert id(call) in statements, ast.unparse(call)
        assert isinstance(call.func, ast.Attribute) and ast.unparse(call.func.value) == "run", ast.unparse(call)
        for arg in call.args:
            for n in ast.walk(arg):
                assert not isinstance(n, (ast.Await, ast.NamedExpr, ast.Lambda)), ast.unparse(call)
                if isinstance(n, ast.Call):
                    assert getattr(n.func, "attr", "") == "get" or getattr(n.func, "id", "") == "len", ast.unparse(call)
    did = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name == "did")
    assert isinstance(did.body[-1], ast.Try) and ast.unparse(did.body[-1].handlers[0].type) == "Exception"


def test_a_report_says_the_last_action_by_its_label_with_the_runs_answers_taken_out(srv, monkeypatch):
    """A problem report's "Last action" line: what the desk last did, by the button's words or the box's label, through
    the report's scrubber (a label that echoes one of the run's own answers loses it, as the report's other lines do)."""
    job = srv.add_job(url="https://jobs.example.com/apply/1", title="Technician", company="Example Fab")["job"]
    r = Run(job["id"], "Technician", "Example Fab", status="needs_you", need="stuck", code="step_not_saved",
            reason="The page didn't move on.")
    r.last_action = {"at": time.time(), "what": "typed", "target": "Why Riverbend Tools? (your answer)"}
    out = report.build(srv.tracker().get(job["id"]), r, hide={"Riverbend Tools"})
    line = next(x for x in out["preview"].splitlines() if x.startswith("**Last action:**"))
    assert line.startswith("**Last action:** typed “Why REDACTED? (your answer)” at "), line
    r.last_action = None
    assert "Last action" not in report.build(srv.tracker().get(job["id"]), r)["preview"]


def test_a_stop_record_that_cant_be_written_is_not_the_stops_trouble(tmp_path):
    """No page saved (the folder empty, or gone): no stop.json, and nothing raised."""
    assert report.write_stop(tmp_path / "gone", Run(1)) is None
    assert report.write_stop(tmp_path, Run(1)) is None
    assert not (tmp_path / report.STOP_RECORD).exists()
