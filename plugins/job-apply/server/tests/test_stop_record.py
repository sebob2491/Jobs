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
    assert report.write_stop(tmp_path / "gone", report.stop_facts(Run(1))) is None
    assert report.write_stop(tmp_path, report.stop_facts(Run(1))) is None
    assert not (tmp_path / report.STOP_RECORD).exists()


def _saved_stop(folder: Path, html: str, errors: list[str]) -> Path:
    folder.mkdir()
    (folder / "page.html").write_text(html, encoding="utf-8")
    (folder / "snapshot.json").write_text(json.dumps({"url": "https://jobs.example.com/apply", "errors": errors,
                                                      "fields": [], "frames": [{"file": "page.html"}]}),
                                          encoding="utf-8")
    return folder


def test_a_big_pages_stop_record_scrubs_only_the_words_around_its_error(tmp_path, monkeypatch):
    """The review of T10: the record was made by parsing and scrubbing the whole page on the desk's event loop (7.5 s
    on a 2.6 MB page). The page's words are read quickly, only the STOP_WINDOW characters either side of the first
    error are scrubbed, and the profile's details there are taken out all the same (the cut made after)."""
    filler = "".join(f"<div class='row'><span>Requisition {n}</span> <a href='#'>Details</a></div>\n"
                     for n in range(30000))
    error = "Phone Number is not valid"
    html = (f"<html><head><style>.row{{color:red}}</style></head><body>{filler}<p>Call sam.rivera@example.com at "
            f"480-555-0123 about it.</p><div role='alert'>{error}</div>{filler}</body></html>")
    assert len(html) > 2_500_000
    folder = _saved_stop(tmp_path / "stop", html, [error])
    lengths: list[int] = []
    hiding = report._hiding

    def measured(scrub, hide):
        said = hiding(scrub, hide)
        return lambda text: lengths.append(len(str(text or ""))) or said(text)

    monkeypatch.setattr(report, "_hiding", measured)
    began = time.perf_counter()
    out = report.write_stop(folder, {"code": "step_not_saved", "need": "stuck", "reason": "The page didn't move on.",
                                     "last_action": {"at": 1.0, "what": "pressed", "target": "Next"}})
    took = time.perf_counter() - began
    record = json.loads(out.read_text(encoding="utf-8"))
    assert error in record["text"] and len(record["text"]) <= report.STOP_TEXT, record["text"]
    assert "sam.rivera" not in record["text"] and "480-555-0123" not in record["text"] and "REDACTED" in record["text"]
    assert max(lengths) <= 2 * report.STOP_WINDOW + len(error), max(lengths)  # (never the page whole)
    assert took < 1.0, took


def test_a_stop_records_text_is_empty_when_its_error_isnt_in_the_pages_words(tmp_path):
    """An error the page's words don't hold (read from an attribute, say): no text, never the page's first words."""
    folder = _saved_stop(tmp_path / "stop", "<html><body><p>Welcome to our careers site.</p></body></html>",
                         ["Something the page doesn't say"])
    record = json.loads(report.write_stop(folder, report.stop_facts(Run(1))).read_text(encoding="utf-8"))
    assert record["text"] == "" and record["errors"] == ["Something the page doesn't say"]


def test_a_long_target_is_scrubbed_before_its_cut(tmp_path):
    """The review of T10: a target cut to 120 characters before it was scrubbed left a fragment of a profile value
    across the cut ("sam.rivera@exa") that no scrubbing could match. It's kept longer, scrubbed, then cut."""
    r = Run(1)
    r.did("typed", {"id": "f1", "label": "x" * 110 + " sam.rivera@example.com is the email we have on file"})
    said = report.last_action_said(r.last_action, report._scrubber(report.report_strings(pipeline.config.Profile.load())))
    assert "rive" not in said and "@exa" not in said and "REDACTED" in said, said  # (no fragment of it either)
    folder = _saved_stop(tmp_path / "stop", "<html><body><p>Hello</p></body></html>", [])
    record = json.loads(report.write_stop(folder, report.stop_facts(r)).read_text(encoding="utf-8"))
    target = record["last_action"]["target"]
    assert "rive" not in target and "@exa" not in target and len(target) <= report.LAST_ACTION_SAID, target


def test_a_security_questions_box_is_never_named_as_the_last_action():
    """The review of T10: a security question's label says which question the person chose, and a password's or an
    emailed code's box is kept out of the "What went in" panel; the last action names only what kind of box it was."""
    page = {"headings": ["Security questions"], "fields": [
        {"id": "q1", "label": "Security question 1: What was the name of your first pet?", "kind": "text"},
        {"id": "q2", "label": "Question 1 Answer", "kind": "text"},
        {"id": "pw", "label": "Create Password", "kind": "password"}]}
    r = Run(1)
    for box, said in (({"id": "q1"}, "a box whose words are kept out of view"),
                      ({"id": "q2"}, "a box whose words are kept out of view"),
                      ({"id": "pw"}, "a password box")):
        r.did("typed", [box], page)
        assert r.last_action["target"] == said, (box, r.last_action)
    r.did("typed", {"id": "e", "label": "Email Address", "kind": "text"}, {"fields": []})
    assert r.last_action["target"] == "Email Address"


def test_a_new_pass_starts_with_no_last_action(srv, monkeypatch):
    """The review of T10: a stop's record named an action from an earlier pass (before a Resume) as this stop's. Each
    pass starts with none."""
    job = srv.add_job(url="https://jobs.example.com/apply/1", title="Technician", company="Example Fab")["job"]
    applier = Applier(srv)
    r = Run(job["id"], "Technician", "Example Fab", status="running")
    r.last_action = {"at": 1.0, "what": "pressed", "target": "Next"}
    seen = []

    async def submit(run_):
        seen.append(run_.last_action)
        run_.status = "ready"

    monkeypatch.setattr(applier, "_submit", submit)
    run(applier._run_task("submit", r))
    assert seen == [None]
