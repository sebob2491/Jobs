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


def test_a_report_shows_the_page_the_job_stopped_on_and_where_its_questions_sit(job_apply_home, tmp_path):
    """No page saved in the job's folder (a stop at a sign-in, say): the report still shows the
    page the desk stopped on, as it read it, and each question with the block it sits in, so a
    box asked one by one ("From", "Month") can be told apart. Never a value."""
    job = {"id": 9, "title": "Equipment Technician", "company": "Acme Semi", "ats": "workday",
           "url": "https://acme.wd1.myworkdayjobs.com/External/job/x"}
    r = Run(9, "Equipment Technician", "Acme Semi", status="needs_you", need="stuck", reason="Workday asks...",
            page_info={"url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply?sid=SESS123",
                       "title": "My Experience - Jordan Quill", "headings": ["My Experience", "Work Experience 1"],
                       "actions": ["Add", "Save and Continue"], "errors": ["Enter a valid date"],
                       "fields": [{"label": "From", "kind": "text", "required": True, "section": "Work Experience 1",
                                   "sublabel": "Month", "empty": True},
                                  {"label": "Company", "kind": "text", "required": True, "empty": False}]},
            questions=[{"id": "a", "label": "From", "sublabel": "Year", "section": "Work Experience", "kind": "text"},
                       {"id": "b", "label": "Degree", "section": "Education 1", "kind": "listbox",
                        "options": ["High School Diploma", "Some College"]}])
    text = report.build(job, r, PERSON)["preview"]
    assert "### The page it stopped on" in text and "Headings: My Experience | Work Experience 1" in text
    assert "Buttons: Add | Save and Continue" in text and "Errors: Enter a valid date" in text
    assert "- From (text, required, empty) [Work Experience 1 / Month]" in text
    assert "- Company (text, required, filled)" in text
    assert "- From (text) [Work Experience / Year]" in text and "- Degree (listbox, 2 choices) [Education 1]" in text
    assert "SESS123" not in text and "Jordan" not in text and "Quill" not in text


def test_an_employee_id_is_taken_out_of_a_report(job_apply_home):
    """The person's ID at an employer (history.employee_ids) is theirs: a report never shows it."""
    person = Profile({"personal": {"first_name": "Jordan", "last_name": "Quill"},
                      "history": {"employee_ids": {"Acme Semi": "88812345"}}})
    job = {"id": 11, "title": "Technician", "company": "Acme Semi", "ats": "workday", "url": "https://acme.example/x"}
    r = Run(11, "Technician", "Acme Semi", status="needs_you", need="stuck", log=["filled Employee ID with 88812345"])
    text = report.build(job, r, person)["preview"]
    assert "88812345" not in text and "filled Employee ID with" in text


def test_a_report_or_note_takes_a_value_out_only_as_a_whole_word(job_apply_home):
    """A value was taken out even where it was only part of an everyday word: a first name "Rob"
    left "PREDACTEDlem" for "Problem", garbled and easy to guess. Only whole words go, wherever
    one is: in an email, a file name, run into the next name."""
    person = Profile({"personal": {"first_name": "Rob", "last_name": "Hall", "email": "rob.hall77@example.org",
                                   "address": {"city": "Ware"}}})
    said = ("Problem on the Robotics page: you shall answer the Challenge about software, Rob Hall from Ware "
            "(rob.hall77@example.org, RobHall.pdf, Rob_Hall_Resume.pdf)")
    job = {"id": 15, "title": "Technician", "company": "Example Corp", "ats": "workday", "url": "https://example.com/j"}
    r = Run(15, "Technician", "Example Corp", status="needs_you", need="stuck", reason=said, log=[said])
    whole = ("Problem on the Robotics page: you shall answer the Challenge about software, REDACTED from REDACTED "
             "(REDACTED, REDACTED.pdf, REDACTED_Resume.pdf)")
    assert f"**What the desk said:** {whole}" in report.build(job, r, person)["preview"]
    assert f"**What the desk said:** {whole}" in report.note(job, r, person)


def _saved_job_page(folder: Path) -> None:
    snap = folder / "debug" / "20261010-090000-000-stop"
    snap.mkdir(parents=True)
    posting = "https://acme.wd1.myworkdayjobs.com/External/job/Phoenix-AZ/Field-Service-Engineer_R12345"
    (snap / "page.html").write_text(
        f"<html><head><title>Field Service Engineer - Acme Semi Careers</title></head><body><h1>My Experience</h1>"
        f"<p>Field Service Engineer (R12345) at Acme Semi</p><a href='{posting}'>Back to the posting</a>"
        "<a href='https://careers.acme-semi.example/benefits'>Benefits at careers.acme-semi.example</a>"
        "<img alt='Acme Semi logo'><label for=a>Company</label><input id=a></body></html>")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": posting + "/apply?sid=SESS123", "title": "Field Service Engineer - Acme Semi Careers",
        "note": "stopped: stuck", "frames": [{"file": "page.html", "url": posting + "/apply"}],
        "fields": [{"label": "Company", "kind": "text", "required": True, "value": ""}]}))


def test_an_anonymous_report_says_only_the_job_system_and_the_step(job_apply_home, tmp_path):
    """The issue a report drafts named the job: the employer in its title, the job's title and its
    address (with its requisition number) in its text. Anonymous, it says the job system and the
    step only, in the issue and in the saved pages."""
    folder = tmp_path / "0016-acme-semi-field-service-engineer"
    _saved_job_page(folder)
    posting = "https://acme.wd1.myworkdayjobs.com/External/job/Phoenix-AZ/Field-Service-Engineer_R12345"
    job = {"id": 16, "title": "Field Service Engineer", "company": "Acme Semi", "ats": "workday",
           "external_id": "R12345", "url": posting, "apply_url": posting + "/apply", "folder": str(folder)}
    r = Run(16, "Field Service Engineer", "Acme Semi", status="needs_you", need="stuck", url=posting + "/apply",
            reason="I couldn't find the button that moves this application on, on Acme Semi's site.",
            log=[f"opened {posting}/apply?source=LinkedIn", "clicked “Apply” for Field Service Engineer (R12345)",
                 "careers.acme-semi.example took too long to respond"],
            page_info={"url": posting + "/apply", "title": "Field Service Engineer - Acme Semi Careers",
                       "headings": ["My Experience", "Field-Service-Engineer"], "actions": ["Save and Continue"],
                       "fields": [{"label": "Why Acme Semi?", "kind": "textarea", "required": True, "empty": True}]})
    out = report.build(job, r, PERSON, anonymous=True)
    text = out["preview"]
    assert parse_qs(urlsplit(out["issue_url"]).query)["title"] == ["Report: a Workday employer, stuck"]
    assert "**Job:** a Workday employer" in text and "**Job system:** workday" in text and "**Address:**" not in text
    assert "**Desk status:** needs_you stuck" in text and "on the employer's site" in text
    assert "1. opened (a Workday address)" in text and "2. clicked “Apply” for the job (REDACTED)" in text
    assert "(a Workday address) (the job - the employer Careers)" in text and "- Why the employer? (textarea" in text
    assert "### Page saved 20261010-090000-000-stop: the job - the employer Careers" in text
    assert "employer, the job's title, its addresses and its requisition number are left out" in text
    with zipfile.ZipFile(out["zip"]) as z:
        page = z.read("pages/20261010-090000-000-stop.html").decode("utf-8")
        everything = "".join(z.read(n).decode("utf-8") for n in z.namelist())
    assert "My Experience" in page and "Phoenix-AZ" in page
    for named in ("Acme", "acme", "Field Service Engineer", "Field-Service-Engineer", "R12345", "SESS123"):
        assert named not in everything and named not in out["issue_url"], named
    assert "Acme Semi" in report.build(job, r, PERSON)["preview"]  # unless asked, the report says which job


def test_claude_can_make_the_report_anonymous_too(srv):
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/x", title="Technician",
                      company="Example Litho")["job"]
    out = run(srv.report_problem(job_id=job["id"], anonymous=True))
    assert "**Job:** a Workday employer" in out["preview"]
    assert not any(named in out["issue_url"] for named in ("Litho", "Technician", "acme"))


def test_a_note_holds_the_stop_but_not_the_person_their_answers_or_the_employer(job_apply_home):
    """Notes are filed many at once in one public issue: each is scrubbed as a report is, holds
    no field's value and none of the person's answers (a fill's error quotes them), and names
    the employer by its job system only, or the issue would list every employer applied to."""
    url = "https://acme.wd1.myworkdayjobs.com/External/job/x/apply"
    job = {"id": 12, "title": "Field Service Engineer", "company": "Acme Semi", "ats": "workday",
           "url": "https://acme.wd1.myworkdayjobs.com/External/job/x?source=jquill77"}
    r = Run(12, "Field Service Engineer", "Acme Semi", status="needs_you", need="questions",
            reason="1 question(s) your profile doesn't answer.", url=url + "?sid=SESS123",
            log=[f"step {i}" for i in range(30)] + [
                f"opened {url};jsessionid=SESS123?email=jquill77%40example.org",
                "filled 12 field(s) for Jordan Quill on Acme Semi's site, from Blue Mesa Fab and Copperline Tools",
                "left 1 optional question(s) empty, as none of their choices is your profile's answer: "
                "“Veteran status” (yours: “Protected veteran”)"],
            page_info={"url": url + "?sid=SESS123", "title": "Apply to Acme Semi - Jordan Quill",
                       "headings": ["My Information"], "actions": ["Save and Continue"], "errors": [],
                       "fields": [{"label": "City", "kind": "text", "required": True, "empty": False,
                                   "value": "Gilbertville"},
                                  {"label": "Phone", "kind": "text", "required": True, "empty": True}]},
            questions=[{"id": "a", "label": "Are you authorized to work in the US?", "kind": "select",
                        "options": ["Select One", "Yes", "No"], "section": "Application Questions", "value": "Maybe",
                        "error": "ValueError: Picked 'Klingon' but the field shows 'Select One'; set it by hand"},
                       {"id": "b", "label": "Highest degree", "kind": "combobox",
                        "error": "your profile's answer “Doctorate of Wizardry” isn't one of its choices"},
                       {"id": "c", "label": "Country", "kind": "combobox",
                        "error": "ValueError: 'Atlantis' doesn't match any suggestion: ['Albania', 'Algeria']"}])
    text = report.note(job, r, PERSON)
    assert text.startswith("## questions: Field Service Engineer, at a Workday employer\n")
    assert "**Job system:** workday" in text and "Acme Semi" not in text and "the employer's site" in text
    assert "1 question(s) your profile doesn't answer." in text
    assert "15. " in text and "16. " not in text and "step 17\n" not in text  # its last 15 steps
    assert f"{url} (Apply to the employer - REDACTED)" in text  # an address keeps its host and page only
    assert "- City (text, required, filled)" in text and "- Phone (text, required, empty)" in text
    assert ("- Are you authorized to work in the US? (select, 3 choices) [Application Questions]: "
            "ValueError: Picked … but the field shows …; set it by hand") in text
    assert "- Highest degree (combobox): your profile's answer … isn't one of its choices" in text
    assert "- Country (combobox): ValueError: … doesn't match any suggestion\n" in text
    assert "(yours: …)" in text
    for private in (*PRIVATE, *MORE_PRIVATE, "SESS123", "jsessionid", "Maybe", "Klingon", "Wizardry", "Atlantis",
                    "Albania", "Protected veteran"):
        assert private not in text, private


def test_a_note_says_which_job_system_the_job_stopped_on(job_apply_home):
    """The page it stopped on names the system (a company's posting that sends the application
    to Workday), else the posting's; a careers site of the employer's own is said so."""
    r = Run(13, "Technician", "Acme Semi", need="stuck", url="https://acme.wd1.myworkdayjobs.com/External/apply")
    assert "at a Workday employer" in report.note({"id": 13, "ats": "company_site"}, r, PERSON)
    r.url = "https://careers.acme-semi.example/apply"
    assert "at an iCIMS employer" in report.note({"id": 13, "ats": "icims"}, r, PERSON)
    assert "at an employer with its own careers site" in report.note({"id": 13, "ats": "company_site"}, r, PERSON)
