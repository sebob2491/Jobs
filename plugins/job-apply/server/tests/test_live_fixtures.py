"""Regression checks against pages captured from real application sites."""

import json

import pytest
from conftest import FIXTURES, browser_available, check_fixture_extraction, run

LIVE = sorted((FIXTURES / "live").glob("*.expect.json"))

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


@pytest.mark.parametrize("expect_path", LIVE, ids=[p.name.removesuffix(".expect.json") for p in LIVE])
def test_live_fixture_still_extracts(srv, expect_path):
    html = expect_path.with_name(expect_path.name.replace(".expect.json", ".html"))
    problems = run(check_fixture_extraction(srv, html, json.loads(expect_path.read_text(encoding="utf-8"))))
    assert not problems, problems


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
    assert "REDACTED-resume.pdf" in boxes[1]["row"], boxes[1]
    resume = tmp_path / "REDACTED-resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    prof = config.Profile({"documents": {"resume": str(resume)}})
    assert [getattr(resolve_field(b, prof, {}), "value", None) for b in boxes] == [True, True]
    other = config.Profile({"documents": {"resume": str(tmp_path / "another.pdf")}})
    assert [resolve_field(b, other, {}) for b in boxes] == [None, None]  # (never a file that isn't the profile's)
