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
