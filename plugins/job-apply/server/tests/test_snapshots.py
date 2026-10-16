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

    meta = json.loads((Path(snap) / "snapshot.json").read_text(encoding="utf-8"))
    assert meta["note"] == "fill_form failures"
    assert meta["details"][0]["value"] == "Atlantis"
    assert any(f["label"] == "State" for f in meta["fields"])

    written = convert(Path(snap), "generic-test", tmp_path, Profile.load())
    html = (tmp_path / "generic-test.html").read_text(encoding="utf-8")
    assert "<script" not in html and "data-ja-" not in html
    for secret in ("Rivera", "sam.rivera@example.com", "480-555-0123"):
        assert secret.lower() not in html.lower()
        assert secret.lower() not in (tmp_path / "generic-test.expect.json").read_text(encoding="utf-8").lower()
    expect = json.loads((tmp_path / "generic-test.expect.json").read_text(encoding="utf-8"))
    assert {"label": "First Name *", "kind": "text", "required": True} in expect["fields"]
    assert len(written) == 2
    assert expect["actions"]  # the buttons that move it on are kept, and a lost one is reported
    assert run(check_fixture_extraction(srv, tmp_path / "generic-test.html", expect)) == []
    lost = {**expect, "actions": [*expect["actions"], "Create Account"]}
    assert run(check_fixture_extraction(srv, tmp_path / "generic-test.html", lost)) == ["missing button 'Create Account'"]


def test_a_saved_page_never_keeps_whats_in_a_password_box(srv):
    """The desk now saves the page each job stops on, a sign-in among them. A React site writes
    what's typed into a box's value attribute, so a password typed there stayed in page.html."""
    run(srv.open_application(url=fixture_url("site/signin.html")))
    run(srv.browser.current_tab.evaluate("""() => {
        const pw = document.querySelector('#pw');
        pw.setAttribute('value', 'not-a-real-password"&<>');
        pw.insertAdjacentHTML('afterend', '<input type=hidden title="a > b" value=kept>');
    }"""))
    snap = run(srv.debug_snapshot(note="sign-in"))
    html = (Path(snap["saved_to"]) / "page.html").read_text(encoding="utf-8")
    assert "not-a-real-password" not in html and 'id="pw"' in html and 'value="kept"' in html


def test_iframe_snapshot_keeps_frame(srv, tmp_path):
    run(srv.open_application(url=fixture_url("iframe_host.html")))
    snap = run(srv.debug_snapshot(note="iframe"))
    assert "frame-1.html" in snap["files"]
    convert(Path(snap["saved_to"]), "iframe-test", tmp_path, Profile.load())
    host = (tmp_path / "iframe-test.html").read_text(encoding="utf-8")
    assert 'src="iframe-test.frame-1.html"' in host
    expect = json.loads((tmp_path / "iframe-test.expect.json").read_text(encoding="utf-8"))
    assert run(check_fixture_extraction(srv, tmp_path / "iframe-test.html", expect)) == []


def test_saved_pages_are_capped_and_can_be_forgotten(srv, monkeypatch):
    """The review of Oct 15: the pages saved for debugging (every frame's HTML and a screenshot, the person's details on
    it among them) were kept for ever, with no way to clear them. A job's folder keeps the newest DEBUG_KEEP; the
    pages a job stopped on keep their own count; forget_saved_pages deletes them, one job's or every job's."""
    from job_apply import server

    monkeypatch.setattr(server, "DEBUG_KEEP", 2)
    job = srv.add_job(url=fixture_url("generic_form.html"), title="FSE", company="Example Litho")["job"]
    run(srv.open_application(job_id=job["id"]))
    debug = Path(job["folder"]) / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    (debug / "20260101-000000-000-stop").mkdir()  # (a stop's page: the desk's own count)
    saved = [run(srv.debug_snapshot(note=f"n{i}"))["saved_to"] for i in range(3)]
    kept = sorted(d.name for d in debug.iterdir())
    assert kept == ["20260101-000000-000-stop", *sorted(Path(s).name for s in saved[1:])], kept
    assert run(srv.forget_saved_pages(job_id=job["id"]))["removed"] == 3
    assert not debug.exists() and Path(job["folder"]).exists()


def test_forgetting_saved_pages_takes_every_folders_and_the_reports_and_keeps_the_submit_record(srv, job_apply_home):
    """The review of 0.3.189: forget_saved_pages walked only the tracker's rows (a folder whose row was merged away kept
    its pages), and left the problem reports' pages and report.zip and the screenshot tool's pictures, though it said
    it took them. Every job folder's go; the pictures of a page before and after its Submit stay (the record of what
    was sent), and so does a report's text."""
    from job_apply import config

    apps = config.applications_dir()
    orphan = apps / "0099-merged-away"
    (orphan / "debug" / "20260101-000000-000").mkdir(parents=True)
    (orphan / "screenshot-20260101-000000.jpg").write_bytes(b"jpg")
    (orphan / "before-submit.jpg").write_bytes(b"jpg")
    (orphan / "after-submit.jpg").write_bytes(b"jpg")
    report = job_apply_home / "reports" / "0099-20260101-000000"
    (report / "pages").mkdir(parents=True)
    (report / "pages" / "page-1.html").write_text("<p>page</p>", encoding="utf-8")
    (report / "report.zip").write_bytes(b"zip")
    (report / "report.md").write_text("what happened", encoding="utf-8")
    out = run(srv.forget_saved_pages())
    assert out["removed"] == 3, out
    assert not (orphan / "debug").exists() and not (orphan / "screenshot-20260101-000000.jpg").exists()
    assert (orphan / "before-submit.jpg").exists() and (orphan / "after-submit.jpg").exists()
    assert not (report / "pages").exists() and not (report / "report.zip").exists() and (report / "report.md").exists()
