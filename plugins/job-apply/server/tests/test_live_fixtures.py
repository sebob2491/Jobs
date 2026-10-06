"""Regression checks against pages captured from real application sites."""

import json

import pytest
from conftest import FIXTURES, browser_available, check_fixture_extraction, run

LIVE = sorted((FIXTURES / "live").glob("*.expect.json"))

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


@pytest.mark.parametrize("expect_path", LIVE, ids=[p.name.removesuffix(".expect.json") for p in LIVE])
def test_live_fixture_still_extracts(srv, expect_path):
    html = expect_path.with_name(expect_path.name.replace(".expect.json", ".html"))
    problems = run(check_fixture_extraction(srv, html, json.loads(expect_path.read_text())))
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
