"""A problem report for a job: what the desk did and the pages it saved, the person's
details taken out, nothing filed without them."""

import json
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from conftest import run

from job_apply import report
from job_apply.config import Profile
from job_apply.pipeline import Run

PERSON = Profile({"personal": {"first_name": "Jordan", "last_name": "Quill", "email": "jquill77@example.org",
                               "phone": "602-555-0142", "address": {"line1": "88 W Example Rd", "postal_code": "85201"}}})
PRIVATE = ("Jordan", "Quill", "jquill77", "555-0142", "6025550142", "Example Rd", "85201")


def _saved_page(folder: Path) -> None:
    snap = folder / "debug" / "20261008-120000-000"
    snap.mkdir(parents=True)
    (snap / "page.html").write_text(
        "<html><body><h1>My Information</h1><label for=a>First Name</label><input id=a value='Jordan'>"
        "<p>Signed in as jquill77@example.org · 602-555-0142 · 88 W Example Rd, Mesa AZ 85201</p></body></html>")
    (snap / "screenshot.jpg").write_bytes(b"\xff\xd8 a picture of the page with Jordan Quill on it")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply?email=jquill77%40example.org",
        "title": "Apply - Jordan Quill", "note": "autofill failures",
        "frames": [{"file": "page.html", "url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply"}],
        "fields": [{"label": "First Name", "kind": "text", "required": True, "value": "Jordan"},
                   {"label": "Phone", "kind": "text", "required": True, "value": ""}]}))


def test_a_report_holds_what_the_desk_did_and_the_pages_with_the_persons_details_out(job_apply_home, tmp_path):
    folder = tmp_path / "0007-acme-fse"
    _saved_page(folder)
    job = {"id": 7, "title": "Field Service Engineer", "company": "Acme Semi", "ats": "workday",
           "url": "https://acme.wd1.myworkdayjobs.com/External/job/x?source=jquill77", "folder": str(folder)}
    r = Run(7, "Field Service Engineer", "Acme Semi", status="needs_you", need="questions",
            reason="1 question your profile doesn't answer", log=["opened the posting", "filled 12 field(s) for Jordan Quill"])
    out = report.build(job, r, PERSON)
    text = out["preview"]
    assert "Field Service Engineer at Acme Semi" in text and "workday" in text and "filled 12 field(s)" in text
    assert "First Name (text, required, filled)" in text and "Phone (text, required, empty)" in text
    with zipfile.ZipFile(out["zip"]) as z:
        names = z.namelist()
        everything = "".join(z.read(n).decode("utf-8") for n in names)
    assert "report.md" in names and any(n.endswith("page.html") for n in names)
    assert not any(n.endswith(".jpg") for n in names)  # pictures can't be scrubbed
    assert "My Information" in everything
    for private in PRIVATE:
        assert private not in everything and private not in text, private
    query = parse_qs(urlsplit(out["issue_url"]).query)
    assert out["issue_url"].startswith("https://github.com/sebob2491/Jobs/issues/new?")
    assert query["title"] == ["Report: Acme Semi, questions"] and query["body"][0].startswith(text[:200])
    assert not any(p in out["issue_url"] for p in PRIVATE)


def test_a_long_report_is_cut_short_in_the_issue_but_whole_in_the_zip(job_apply_home, tmp_path):
    job = {"id": 8, "title": "FSE", "company": "Acme Semi", "url": "https://example.com/j", "folder": str(tmp_path / "j")}
    r = Run(8, "FSE", "Acme Semi", status="failed", log=[f"step {i}: " + "x" * 300 for i in range(40)])
    out = report.build(job, r, PERSON)
    body = parse_qs(urlsplit(out["issue_url"]).query)["body"][0]
    assert len(out["preview"]) > report.ISSUE_BODY and "cut short" in body and len(body) < report.ISSUE_BODY + 400


def test_the_desk_builds_a_report_for_a_job_and_only_for_one_it_has(srv, job_apply_home):
    from test_desk import _client  # noqa: PLC0415

    from job_apply.desk import Desk

    desk = Desk(srv)
    desk.applier.start = lambda: None
    job = srv.add_job(url="https://example.com/a", title="FSE", company="Example Fab")["job"]

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with _client(desk) as c:
                h = {"x-desk-token": desk.token}
                ok = (await c.post(f"/api/job/{job['id']}/report", headers=h)).json()
                missing = await c.post("/api/job/777/report", headers=h)
                return ok, missing.status_code
        finally:
            await desk.stop()

    ok, missing = run(go())
    assert "FSE at Example Fab" in ok["preview"] and Path(ok["zip"]).is_file() and missing == 400
    assert Path(ok["folder"]).parent == job_apply_home / "reports"
