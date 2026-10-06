"""Debug snapshots and turning them into fixtures."""

import json
from pathlib import Path

import pytest
from conftest import browser_available, by_label, check_fixture_extraction, fixture_url, run

from job_apply.config import Profile
from job_apply.fixtures import convert

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


def test_failed_fill_saves_snapshot_and_converts_to_fixture(srv, tmp_path):
    job = srv.add_job(url=fixture_url("generic_form.html"), title="FSE", company="Example Litho")["job"]
    run(srv.open_application(job_id=job["id"]))
    run(srv.autofill())
    state = by_label(run(srv.inspect_form())["fields"], "state")
    out = run(srv.fill_form([{"id": state["id"], "value": "Atlantis"}]))
    assert not out["ok"]
    snap = out["debug_snapshot"]
    assert snap.startswith(job["folder"])

    meta = json.loads((Path(snap) / "snapshot.json").read_text())
    assert meta["note"] == "fill_form failures"
    assert meta["details"][0]["value"] == "Atlantis"
    assert any(f["label"] == "State" for f in meta["fields"])

    written = convert(Path(snap), "generic-test", tmp_path, Profile.load())
    html = (tmp_path / "generic-test.html").read_text()
    assert "<script" not in html and "data-ja-" not in html
    for secret in ("Rivera", "sam.rivera@example.com", "480-555-0123"):
        assert secret.lower() not in html.lower()
        assert secret.lower() not in (tmp_path / "generic-test.expect.json").read_text().lower()
    expect = json.loads((tmp_path / "generic-test.expect.json").read_text())
    assert {"label": "First Name *", "kind": "text", "required": True} in expect["fields"]
    assert len(written) == 2

    assert run(check_fixture_extraction(srv, tmp_path / "generic-test.html", expect)) == []


def test_iframe_snapshot_keeps_frame(srv, tmp_path):
    run(srv.open_application(url=fixture_url("iframe_host.html")))
    snap = run(srv.debug_snapshot(note="iframe"))
    assert "frame-1.html" in snap["files"]
    convert(Path(snap["saved_to"]), "iframe-test", tmp_path, Profile.load())
    host = (tmp_path / "iframe-test.html").read_text()
    assert 'src="iframe-test.frame-1.html"' in host
    expect = json.loads((tmp_path / "iframe-test.expect.json").read_text())
    assert run(check_fixture_extraction(srv, tmp_path / "iframe-test.html", expect)) == []
