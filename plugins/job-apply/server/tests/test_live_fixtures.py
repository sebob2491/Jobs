"""Regression checks against pages captured from real application sites."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FIXTURES, browser_available, check_fixture_extraction, run

from job_apply import fixtures

LIVE = sorted((FIXTURES / "live").glob("*.expect.json"))
DECIDED = sorted((FIXTURES / "live").glob(f"*{fixtures.DECISION_SUFFIX}"))

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


@pytest.mark.parametrize("expect_path", LIVE, ids=[p.name.removesuffix(".expect.json") for p in LIVE])
def test_live_fixture_still_extracts(srv, expect_path):
    html = expect_path.with_name(expect_path.name.replace(".expect.json", ".html"))
    problems = run(check_fixture_extraction(srv, html, json.loads(expect_path.read_text(encoding="utf-8"))))
    assert not problems, problems


@pytest.mark.parametrize("decision_path", DECIDED, ids=[p.name.removesuffix(fixtures.DECISION_SUFFIX) for p in DECIDED])
def test_live_fixture_decides_the_same(srv, decision_path, tmp_path):
    """Decision replay: what the desk decides on a page a nightly stopped on (the page's kind, the button it would
    press, the CAPTCHA, security-question, account-and-application and job-search reads, each box's answer for the live
    check's applicant), recorded by live_smoke.py --fixtures, is decided the same offline. A change that decides
    otherwise fails, naming the page and the key. An intended one is accepted with nightly_summary.py
    --import-fixtures --replace (a run's new capture of the page), or JOB_APPLY_REDECIDE=1 on this test (the
    decisions written again from the pages here: look the diff over)."""
    name = decision_path.name.removesuffix(fixtures.DECISION_SUFFIX)
    html = decision_path.with_name(f"{name}.html")
    data, text = run(fixtures.read_saved(srv, html))
    now = fixtures.scrubbed_decision(fixtures.decide(data, text, fixtures.replay_profile(tmp_path)))
    was = json.loads(decision_path.read_text(encoding="utf-8"))
    if os.environ.get("JOB_APPLY_REDECIDE") == "1":
        fixtures.write_decision(decision_path, {**now, **({"run": was["run"]} if "run" in was else {})})
        return
    differences = fixtures.decision_differences(name, was, now)
    assert not differences, "\n".join(differences)


def test_the_replay_applicant_is_the_live_checks():
    """The decisions are replayed for the live check's own applicant (its technician, but for the address made fresh
    each run and the resume made for it): a change to one is a change to both, and the decisions are written again."""
    script = ("import json, sys; sys.path.insert(0, 'scripts'); import live_smoke; "
              "print('RESULT ' + json.dumps(live_smoke.FAKE_PROFILE))")
    out = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parent.parent, capture_output=True,
                         text=True, encoding="utf-8", timeout=120)
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stderr[-2000:]
    live = json.loads(line.removeprefix("RESULT "))
    replay = json.loads(json.dumps(fixtures.REPLAY_PROFILE))
    for prof in (live, replay):
        prof["personal"].pop("email")
    assert live == replay


def test_a_chosen_pill_is_the_fields_answer(srv):
    """Infineon's location picker (captured live) shows its choice as a pill beside a
    read-only input; that pill is the answer, so the field isn't empty."""
    run(srv.browser.goto((FIXTURES / "live" / "pipeline-infineon.html").resolve().as_uri()))
    fields = run(srv.browser.inspect(include_dropdown_options=False))["fields"]
    where = next(f for f in fields if f["label"].startswith("Preferred location"))
    assert where["value"] == "Singapore" and where.get("readonly")


def test_workday_search_prompts_are_pickers_with_their_choice(srv):
    """Workday's 2026 prompts (Onto, captured live) are search inputs with no ARIA combobox
    role; what's chosen is a pill beside the input's box. Typing into one as plain text
    is cleared when it loses focus, so it must be picked from its list."""
    run(srv.browser.goto((FIXTURES / "live" / "live-onto-innovation.html").resolve().as_uri()))
    fields = {f["label"]: f for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"]}
    heard, code = fields["How Did You Hear About Us?*"], fields["Country Phone Code*"]
    assert heard["kind"] == code["kind"] == "combobox"
    assert heard["value"] == "" and code["value"] == "United States of America (+1)"


def test_a_dialogs_own_fields_count_even_inside_aria_hidden(srv):
    """Paycom's Quick Apply (Ebara, captured live) wraps its contact fields in aria-hidden
    inside its open dialog; the page behind the dialog stays out."""
    run(srv.browser.goto((FIXTURES / "live" / "live-ebara-technologies.html").resolve().as_uri()))
    fields = run(srv.browser.inspect(include_dropdown_options=False))["fields"]
    labels = [f["label"] for f in fields]
    for want in ("Legal First Name *", "Legal Last Name *", "Email Address *", "Confirm Email *"):
        assert any(label.startswith(want.rstrip(" *")) for label in labels), (want, labels)
    assert any(f["kind"] == "file" for f in fields)



def test_a_file_lists_boxes_are_read_and_the_resume_is_chosen_in_it(srv, tmp_path):
    """Kforce's Taleo (the nightly of Oct 12, captured live): its attachments step lists the files on the person's
    record, each with a box to choose it for this application and one to say it's the resume, labelled only by their
    titles; read as "on", nothing was ticked, and the site said "You must select at least one relevant file". They're
    read by their titles (with the row they're in), and the profile's resume is chosen, and said to be the resume."""
    from job_apply import config
    from job_apply.autofill import resolve_field

    run(srv.browser.goto((FIXTURES / "live" / "pipeline-kforce.html").resolve().as_uri()))
    boxes = [f for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"] if f["kind"] == "checkbox"]
    assert [b["label"] for b in boxes] == ["Select the file: REDACTED-resume.pdf", "Document is a resume"], boxes
    assert all((b["row_file"], b["same_file_rows"]) == ("REDACTED-resume.pdf", 1) for b in boxes), boxes
    resume = tmp_path / "REDACTED-resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    prof = config.Profile({"documents": {"resume": str(resume)}})
    assert [getattr(resolve_field(b, prof, {}), "value", None) for b in boxes] == [True, True]
    other = config.Profile({"documents": {"resume": str(tmp_path / "another.pdf")}})
    assert [resolve_field(b, other, {}) for b in boxes] == [None, None]  # (never a file that isn't the profile's)
    # the review's: a resume of the same name on another row (one tailored for another job), or a name that only
    # holds the resume's: neither ticked
    twice = [{**b, "same_file_rows": 2} for b in boxes]
    assert [resolve_field(b, prof, {}) for b in twice] == [None, None]
    longer = [{**b, "row_file": "old-REDACTED-resume.pdf"} for b in boxes]
    assert [resolve_field(b, prof, {}) for b in longer] == [None, None]


def test_a_boxs_title_is_its_label_only_in_a_file_list(srv, tmp_path):
    """The review's: a row's job alerts with an "I agree" box titled so, and no other words, isn't read as "I agree"
    (which the desk's terms rules could tick): only a file list's boxes are read by their titles."""
    page = tmp_path / "row.html"
    page.write_text('<!doctype html><table><tr><td>Send me job alerts and marketing emails</td>'
                    '<td><input type="checkbox" title="I agree"></td><td><input type="checkbox" title="I do not agree">'
                    '</td></tr></table>', encoding="utf-8")
    run(srv.browser.goto(page.as_uri()))
    boxes = [f for f in run(srv.browser.inspect(include_dropdown_options=False))["fields"] if f["kind"] == "checkbox"]
    assert boxes and not any(b["label"] in ("I agree", "I do not agree") for b in boxes), boxes


def test_a_careers_search_home_is_read_as_a_job_search_not_an_application(srv):
    """Edwards Vacuum's SuccessFactors careers home (the nightly of Oct 12, captured live), where its sign-in opened
    again led: its keywords, requisition ID and filters read as an application's boxes, and the desk stopped. It's a
    job search, with no sign-in on it."""
    from job_apply import pipeline

    run(srv.browser.goto((FIXTURES / "live" / "pipeline-edwards-vacuum.html").resolve().as_uri()))
    data = run(srv.browser.inspect(include_dropdown_options=False))
    assert pipeline._job_search(data), (data["fields"], data["actions"])


RUN_DECIDE = """
import asyncio, json, shutil, sys
from pathlib import Path
sys.path.insert(0, "scripts")
import live_smoke
from job_apply import server

async def main(where, page):
    live_smoke.FIXTURE_DIR = Path(where) / "live"
    live_smoke.FIXTURE_DIR.mkdir()
    shutil.copy2(page, live_smoke.FIXTURE_DIR / "pipeline-example.html")
    await live_smoke.decide_fixture("pipeline-example", Path(where) / "out",
                                    {"status": "needs_you", "need": "stuck", "reason": "Stopped: " + "x" * 100})
    await server.close_browser()
    stops = [live_smoke.stopped_short({"rounds": rounds}) for rounds in (
        [], [{"status": "ready"}], [{"status": "needs_you", "need": "questions"}],
        [{"status": "needs_you", "need": "sign_in"}], [{"status": "needs_you", "need": "bot_check"}],
        [{"status": "needs_you", "need": "questions"}, {"status": "failed", "need": None}])]
    print("RESULT " + json.dumps(stops))

asyncio.run(main(*sys.argv[1:]))
"""


def test_the_live_check_records_what_the_desk_decides_on_every_page_it_stopped_on(tmp_path):
    """live_smoke.py --fixtures captures the page wherever an employer's last round didn't reach the review page (a
    sign-in's, a bot check's too, not only questions and problems), and writes beside it what the desk decides there,
    as test_live_fixture_decides_the_same decides it again, with the run's own need and the start of its reason."""
    page = FIXTURES / "live" / "pipeline-kforce.html"
    out = subprocess.run([sys.executable, "-c", RUN_DECIDE, str(tmp_path), str(page)], cwd=Path(__file__).parent.parent,
                         capture_output=True, text=True, encoding="utf-8", timeout=180)
    line = next((x for x in out.stdout.splitlines() if x.startswith("RESULT ")), None)
    assert line, out.stdout[-2000:] + out.stderr[-3000:]
    assert json.loads(line.removeprefix("RESULT ")) == [False, False, True, True, True, True]
    written = tmp_path / "live" / f"pipeline-example{fixtures.DECISION_SUFFIX}"
    copy = tmp_path / "out" / "fixtures" / written.name
    assert copy.read_text(encoding="utf-8") == written.read_text(encoding="utf-8")
    decision = json.loads(written.read_text(encoding="utf-8"))
    assert decision.pop("run") == {"need": "stuck", "reason": ("Stopped: " + "x" * 100)[:80]}
    assert decision == json.loads((FIXTURES / "live" / f"pipeline-kforce{fixtures.DECISION_SUFFIX}").read_text(
        encoding="utf-8"))
