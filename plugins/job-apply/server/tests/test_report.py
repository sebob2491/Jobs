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
                               "phone": "602-555-0142", "address": {"line1": "88 W Example Rd", "postal_code": "85201",
                                                                    "city": "Gilbertville"}},
                  "education": {"school": "Saguaro Valley College"},
                  "history": {"previous_employers": ["Blue Mesa Fab"]},
                  "work_history": [{"company": "Copperline Tools", "title": "Technician"}]})
PRIVATE = ("Jordan", "Quill", "jquill77", "555-0142", "6025550142", "Example Rd", "85201")
# kept in a test fixture, but not in a public issue
MORE_PRIVATE = ("Gilbertville", "Saguaro Valley", "Blue Mesa Fab", "Copperline")


def _saved_page(folder: Path) -> None:
    snap = folder / "debug" / "20261008-120000-000"
    snap.mkdir(parents=True)
    (snap / "page.html").write_text(
        "<html><body><h1>My Information</h1><label for=a>First Name</label><input id=a value='Jordan'>"
        "<p>Signed in as jquill77@example.org · 602-555-0142 · 88 W Example Rd, Gilbertville AZ 85201</p>"
        "<p>Saguaro Valley College · Blue Mesa Fab · Copperline Tools</p>"
        "<iframe src='https://acme.wd1.myworkdayjobs.com/frame/questions?sid=XYZ'></iframe></body></html>")
    (snap / "frame-1.html").write_text("<html><body><h2>Questions</h2><p>Gilbertville</p></body></html>")
    (snap / "screenshot.jpg").write_bytes(b"\xff\xd8 a picture of the page with Jordan Quill on it")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply?email=jquill77%40example.org",
        "title": "Apply - Jordan Quill", "note": "autofill failures",
        "frames": [{"file": "page.html", "url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply"},
                   {"file": "frame-1.html", "url": "https://acme.wd1.myworkdayjobs.com/frame/questions?sid=XYZ"}],
        "fields": [{"label": "First Name", "kind": "text", "required": True, "value": "Jordan"},
                   {"label": "Phone", "kind": "text", "required": True, "value": ""},
                   {"label": "State", "kind": "select", "required": True, "value": "Select One"},
                   {"label": "Years of experience", "kind": "text", "required": False, "value": 0}]}))


def test_a_report_holds_what_the_desk_did_and_the_pages_with_the_persons_details_out(job_apply_home, tmp_path):
    folder = tmp_path / "0007-acme-fse"
    _saved_page(folder)
    job = {"id": 7, "title": "Field Service Engineer", "company": "Acme Semi", "ats": "workday",
           "url": "https://acme.wd1.myworkdayjobs.com/External/job/x?source=jquill77", "folder": str(folder)}
    r = Run(7, "Field Service Engineer", "Acme Semi", status="needs_you", need="questions",
            reason="1 question your profile doesn't answer",
            log=["opened https://x.taleo.net/careersection/jobapply.ftl;jsessionid=SESS123?_csrf=TOK456&job=1",
                 "filled 12 field(s) for Jordan Quill", "answered Blue Mesa Fab: Yes, previously"])
    out = report.build(job, r, PERSON)
    text = out["preview"]
    assert "Field Service Engineer at Acme Semi" in text and "workday" in text and "filled 12 field(s)" in text
    assert "First Name (text, required, filled)" in text and "Phone (text, required, empty)" in text
    # a dropdown still on "Select One" is empty (it's why a run gets stuck); a box with 0 is filled
    assert "State (select, required, empty)" in text and "Years of experience (text, filled)" in text
    assert "https://x.taleo.net/careersection/jobapply.ftl" in text  # an address keeps its page, not its session
    assert not any(token in text + out["issue_url"] for token in ("SESS123", "TOK456", "jsessionid"))
    with zipfile.ZipFile(out["zip"]) as z:
        names = z.namelist()
        everything = "".join(z.read(n).decode("utf-8") for n in names)
        page = z.read("pages/20261008-120000-000.html").decode("utf-8")
    assert "report.md" in names and "pages/20261008-120000-000.frame-1.html" in names
    assert not any(n.endswith(".jpg") for n in names)  # pictures can't be scrubbed
    assert "My Information" in everything and "Questions" in everything
    assert 'src="20261008-120000-000.frame-1.html"' in page  # the saved page still shows its frame
    for private in PRIVATE + MORE_PRIVATE:
        assert private not in everything and private not in text, private
    query = parse_qs(urlsplit(out["issue_url"]).query)
    assert "drag" not in query["body"][0]  # the saved pages aren't for the public issue
    assert out["issue_url"].startswith("https://github.com/sebob2491/Jobs/issues/new?")
    assert query["title"] == ["Report: Acme Semi, questions"] and query["body"][0].startswith(text[:200])
    assert not any(p in out["issue_url"] for p in PRIVATE)


def test_a_long_report_is_cut_short_in_the_issue_but_whole_in_the_zip(job_apply_home, tmp_path):
    job = {"id": 8, "title": "FSE", "company": "Acme Semi", "url": "https://example.com/j", "folder": str(tmp_path / "j")}
    r = Run(8, "FSE", "Acme Semi", status="failed", log=[f"step {i}: " + "x" * 300 for i in range(40)])
    out = report.build(job, r, PERSON)
    body = parse_qs(urlsplit(out["issue_url"]).query)["body"][0]
    assert len(out["preview"]) > report.ISSUE_BODY and "cut short" in body and len(body) < report.ISSUE_BODY + 400
    assert len(out["issue_url"]) <= report.ISSUE_URL


def test_a_report_full_of_quotes_and_line_breaks_still_fits_githubs_address_limit(job_apply_home, tmp_path):
    """The cap was on the text, but encoding makes a curly quote 9 characters and a line
    break 3: a 6000-character report made a 10,000-character address, which GitHub refuses."""
    job = {"id": 9, "title": "FSE", "company": "Acme Semi", "url": "https://example.com/j", "folder": str(tmp_path / "j")}
    r = Run(9, "FSE", "Acme Semi", status="failed", log=[f"clicked \u201cNext\u201d on page {i}" for i in range(40)])
    out = report.build(job, r, PERSON)
    assert len(out["issue_url"]) <= report.ISSUE_URL
    assert "clicked \u201cNext\u201d on page 0" in parse_qs(urlsplit(out["issue_url"]).query)["body"][0]


def test_two_reports_in_the_same_second_get_folders_of_their_own(job_apply_home):
    """A double click (or the desk and Claude at once) wrote two reports into one folder,
    racing on report.zip."""
    first, second = report._folder(7), report._folder(7)
    assert first != second and first.is_dir() and second.is_dir()


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
