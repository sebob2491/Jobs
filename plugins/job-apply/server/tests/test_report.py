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
        "<iframe src='https://acme.wd1.myworkdayjobs.com/frame/questions?sid=XYZ'></iframe></body></html>",
        encoding="utf-8")  # as the browser's snapshot writes it
    (snap / "frame-1.html").write_text("<html><body><h2>Questions</h2><p>Gilbertville</p></body></html>",
                                       encoding="utf-8")
    (snap / "screenshot.jpg").write_bytes(b"\xff\xd8 a picture of the page with Jordan Quill on it")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply?email=jquill77%40example.org",
        "title": "Apply - Jordan Quill", "note": "autofill failures",
        "frames": [{"file": "page.html", "url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply"},
                   {"file": "frame-1.html", "url": "https://acme.wd1.myworkdayjobs.com/frame/questions?sid=XYZ"}],
        "fields": [{"label": "First Name", "kind": "text", "required": True, "value": "Jordan"},
                   {"label": "Phone", "kind": "text", "required": True, "value": ""},
                   {"label": "State", "kind": "select", "required": True, "value": "Select One"},
                   {"label": "Years of experience", "kind": "text", "required": False, "value": 0}]}), encoding="utf-8")


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


def test_a_report_names_none_of_the_profiles_answers_the_desk_said_in_its_own_words(job_apply_home):
    """"(yours: “Protected veteran”)" in a log line is the profile's answer, in a report filed in public: a note took
    it out (notes are filed many together), a report didn't (the review of Oct 15). Both do."""
    job = {"id": 8, "title": "Field Service Engineer", "company": "Acme Semi", "ats": "workday",
           "url": "https://acme.wd1.myworkdayjobs.com/External/job/x"}
    r = Run(8, "Field Service Engineer", "Acme Semi", status="needs_you", need="questions",
            reason="1 question your profile doesn't answer",
            log=["left 1 optional question(s) empty, as none of their choices is your profile's answer: "
                 "“Veteran status” (yours: “Protected veteran”)",
                 "Highest degree: your profile's answer “Doctorate of Wizardry” isn't one of its choices"])
    for text in (report.build(job, r, PERSON)["preview"], report.build(job, r, PERSON, anonymous=True)["preview"]):
        assert "(yours: …)" in text and "your profile's answer …" in text, text
        assert "Protected veteran" not in text and "Wizardry" not in text, text


def test_a_long_report_is_cut_short_in_the_issue_but_whole_in_the_zip(job_apply_home, tmp_path):
    job = {"id": 8, "title": "FSE", "company": "Acme Semi", "url": "https://example.com/j",
           "folder": str(tmp_path / "j")}
    r = Run(8, "FSE", "Acme Semi", status="failed", log=[f"step {i}: " + "x" * 300 for i in range(40)])
    out = report.build(job, r, PERSON)
    body = parse_qs(urlsplit(out["issue_url"]).query)["body"][0]
    assert len(out["preview"]) > report.ISSUE_BODY and "cut short" in body and len(body) < report.ISSUE_BODY + 400
    assert len(out["issue_url"]) <= report.ISSUE_URL


def test_a_report_full_of_quotes_and_line_breaks_still_fits_githubs_address_limit(job_apply_home, tmp_path):
    """The cap was on the text, but encoding makes a curly quote 9 characters and a line
    break 3: a 6000-character report made a 10,000-character address, which GitHub refuses."""
    job = {"id": 9, "title": "FSE", "company": "Acme Semi", "url": "https://example.com/j",
           "folder": str(tmp_path / "j")}
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
    from test_desk import _client

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


def test_an_id_given_on_the_desk_is_taken_out_of_a_report(job_apply_home):
    """The review of the ID's change (LOW 5): an ID the person gave on the desk's card lives in answers.yaml, not the
    profile's employee_ids, and the scrubbers took out only those. A report never shows one either, nor a pattern's
    answer to an ID written by hand; an answer that isn't an ID stays."""
    from job_apply import config

    config.save_answer("Employee ID", "77712345", "Acme Semi", at="Acme Semi")
    person = Profile({**config.Profile.load().data, "personal": {"first_name": "Jordan", "last_name": "Quill"},
                      "answers": [*config.saved_answers(), {"match": "badge number", "answer": "BADGE-4242"},
                                  {"match": "shift", "answer": "Swing"}]})
    job = {"id": 12, "title": "Technician", "company": "Acme Semi", "ats": "workday", "url": "https://acme.example/y"}
    r = Run(12, "Technician", "Acme Semi", status="needs_you", need="stuck",
            log=["filled Employee ID with 77712345", "filled Badge number with BADGE-4242", "filled Shift with Swing"])
    text = report.build(job, r, person)["preview"]
    assert "77712345" not in text and "BADGE-4242" not in text and "filled Employee ID with" in text
    assert "Swing" in text


def test_a_report_or_note_takes_a_value_out_only_as_a_whole_word(job_apply_home):
    """A value was taken out even where it was only part of an everyday word: a first name "Rob"
    left "PREDACTEDlem" for "Problem", garbled and easy to guess. Only whole words go, wherever
    one is: in an email, a file name, run into the next name. In the name of something a short one
    goes only with an initial or two: a town "Ware" left "REDACTED_v2" for the field "software_v2"."""
    person = Profile({"personal": {"first_name": "Rob", "last_name": "Hall", "email": "rob.hall77@example.org",
                                   "address": {"city": "Ware"}}})
    said = ("Problem on the Robotics page: you shall answer the Challenge about software_v2, Rob Hall from Ware "
            "(rob.hall77@example.org, RobHall.pdf, Rob_Hall_Resume.pdf)")
    job = {"id": 15, "title": "Technician", "company": "Example Corp", "ats": "workday", "url": "https://example.com/j"}
    r = Run(15, "Technician", "Example Corp", status="needs_you", need="stuck", reason=said, log=[said])
    whole = ("Problem on the Robotics page: you shall answer the Challenge about software_v2, REDACTED from REDACTED "
             "(REDACTED, REDACTED.pdf, REDACTED_Resume.pdf)")
    assert f"**What the desk said:** {whole}" in report.build(job, r, person)["preview"]
    assert f"**What the desk said:** {whole}" in report.note(job, r, person)


def test_a_report_or_note_takes_a_surname_out_of_a_user_name_or_a_file_name(job_apply_home):
    """Taken out as whole words only, a surname after an initial stayed in a user name, a file name,
    an email or a link ("jquill1987", "JQuill_Resume.pdf"), and so in the public issue. There a
    value goes wherever it is, from a report, an anonymous one (with the employer's name) and a note,
    while "Problem" and "Robotics" stay whole."""
    said = ("Problem on the Robotics page: signed in as jquill1987, JQuill_Resume.pdf, jquill@example.com, "
            "https://example.com/u/jquill, from noreply@mailexamplelitho.com")
    job = {"id": 18, "title": "Technician", "company": "Example Litho", "ats": "workday",
           "url": "https://example.com/j"}
    r = Run(18, "Technician", "Example Litho", status="needs_you", need="stuck", reason=said, log=[said])
    anonymous = report.build(job, r, PERSON, anonymous=True)["preview"]
    for text in (report.build(job, r, PERSON)["preview"], anonymous, report.note(job, r, PERSON)):
        assert "quill" not in text.lower() and "Problem on the Robotics page: signed in as REDACTED1987" in text
    assert "litho" not in anonymous.lower()


def _stop_and_fill_pages(folder: Path, names: list[str]) -> None:
    for name in names:
        snap = folder / "debug" / name
        snap.mkdir(parents=True)
        note = "stopped: questions" if name.endswith("-stop") else "autofill failures"
        (snap / "page.html").write_text(f"<html><body><h1>{note}</h1><label for=a>City</label><input id=a>"
                                        "</body></html>", encoding="utf-8")
        (snap / "snapshot.json").write_text(json.dumps({
            "url": "https://example.com/apply", "title": note, "note": note,
            "frames": [{"file": "page.html", "url": "https://example.com/apply"}],
            "fields": [{"label": "City", "kind": "text", "required": True, "value": ""}]}), encoding="utf-8")


def test_the_page_a_fill_broke_on_stays_in_the_report_after_the_jobs_stops(job_apply_home, tmp_path):
    """Each stop saves its page (debug/<time>-stop), and a report held the newest three pages of any
    kind: after a failed fill and three stops, only stop pages, without the one where the fill broke,
    which a report most needs. It's kept, with the newest stops, and a report still holds three."""
    folder = tmp_path / "0019-example-corp-technician"
    _stop_and_fill_pages(folder, ["20261010-090000-000", "20261010-090100-000-stop", "20261010-090200-000-stop",
                                  "20261010-090300-000-stop"])
    job = {"id": 19, "title": "Technician", "company": "Example Corp", "ats": "company_site",
           "url": "https://example.com/j", "folder": str(folder)}
    out = report.build(job, Run(19, "Technician", "Example Corp", status="needs_you", need="questions"), PERSON)
    assert [p for p in out["pages"] if p.endswith(".html")] == [
        "pages/20261010-090000-000.html", "pages/20261010-090200-000-stop.html", "pages/20261010-090300-000-stop.html"]
    assert "### Page saved 20261010-090000-000: autofill failures" in out["preview"]
    # a failed fill among the newest pages is just one of them
    _stop_and_fill_pages(folder, ["20261010-090400-000", "20261010-090500-000-stop"])
    assert [p.name for p in report._snapshots(folder)] == [
        "20261010-090300-000-stop", "20261010-090400-000", "20261010-090500-000-stop"]


def _saved_job_page(folder: Path) -> None:
    snap = folder / "debug" / "20261010-090000-000-stop"
    snap.mkdir(parents=True)
    posting = "https://acme.wd1.myworkdayjobs.com/External/job/Phoenix-AZ/Field-Service-Engineer_R12345"
    (snap / "page.html").write_text(
        f"<html><head><title>Field Service Engineer - Acme Semi Careers</title></head><body><h1>My Experience</h1>"
        f"<p>Field Service Engineer (R12345) at Acme Semi</p><a href='{posting}'>Back to the posting</a>"
        "<a href='https://careers.acme-semi.example/benefits'>Benefits at careers.acme-semi.example</a>"
        "<img alt='Acme Semi logo'><label for=a>Company</label><input id=a></body></html>", encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": posting + "/apply?sid=SESS123", "title": "Field Service Engineer - Acme Semi Careers",
        "note": "stopped: stuck", "frames": [{"file": "page.html", "url": posting + "/apply"}],
        "fields": [{"label": "Company", "kind": "text", "required": True, "value": ""}]}), encoding="utf-8")


def test_an_anonymous_report_says_only_the_job_system_and_the_step(job_apply_home, tmp_path):
    """The issue a report drafts named the job: the employer in its title, the job's title and its
    address (with its requisition number) in its text. Anonymous, it says the job system and the
    step only, in the issue and in the saved pages."""
    folder = tmp_path / "0016-acme-semi-field-service-engineer"
    _saved_job_page(folder)
    posting = "https://acme.wd1.myworkdayjobs.com/External/job/Phoenix-AZ/Field-Service-Engineer_R12345"
    job = {"id": 16, "title": "Field Service Engineer", "company": "Acme Semi", "ats": "workday",
           "external_id": "R12345", "url": posting, "apply_url": posting + "/apply", "folder": str(folder)}
    r = Run(16, "Field Service Engineer", "Acme Semi", status="needs_you", need="stuck", code="no_step_button",
            url=posting + "/apply",
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
    assert "**Desk status:** needs_you stuck (`no_step_button`)" in text and "on the employer's site" in text
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


def test_an_anonymous_report_says_an_address_with_parentheses_and_an_employers_short_forms(job_apply_home):
    """Note 35: a Workday address with "(AZ)" in its path was cut at the ")", so the head was said and
    the tail stayed (a doubled ")", the title's slug, the query with an email), and an employer
    named "Acme Semiconductor Group" stayed in a heading ("Acme Career") and a page title
    ("Apply I Acme Group"), which are not its name as given."""
    posting = "https://acme.wd5.myworkdayjobs.com/External/job/Phoenix-(AZ)/Test-Technician-II-Hybrid_R98765"
    address = posting + "/apply/applyManually?username=a%40b.com"
    job = {"id": 21, "title": "Test Technician II (Hybrid)", "company": "Acme Semiconductor Group", "ats": "workday",
           "url": posting}
    r = Run(21, "Test Technician II (Hybrid)", "Acme Semiconductor Group", status="needs_you", need="stuck",
            reason="I couldn't find the button that moves this application on.",
            log=[f"opened {address}", "clicked “Apply” for Test Technician II (Hybrid)"],
            page_info={"title": "Apply I Acme Group", "headings": ["Acme Career", "My Information"],
                       "actions": ["Save and Continue"]})
    text = report.build(job, r, PERSON, anonymous=True)["preview"]
    assert "1. opened (a Workday address)\n" in text and text.count("(a Workday address)") == 1
    assert "Headings: the employer Career | My Information" in text and " (Apply I the employer)\n" in text
    for left in ("))", "Acme", "acme", "Test-Technician", "Technician", "Phoenix", "username", "a%40b", "R98765"):
        assert left not in text, left
    assert "Acme Career" in report.build(job, r, PERSON)["preview"]  # unless asked, the report says which job


def test_an_employers_short_forms_are_its_name_less_its_last_words_its_first_word_and_its_tenant():
    forms = report._employer_forms("Acme Semiconductor Group, Inc.", ["https://acme.wd5.myworkdayjobs.com/x", None])
    assert set(forms) == {"Acme Semiconductor Group, Inc.", "Acme Semiconductor Group", "Acme Semiconductor", "Acme",
                          "acme", "ACME"}  # (its tenant as a host has it, and as a page may)
    # an everyday first word stays (it is a page's too), and so does a short one and a job system's own label
    assert report._employer_forms("American Wafer Co", ["https://boards.greenhouse.io/x"]) == [
        "American Wafer Co", "American Wafer"]
    assert report._employer_forms("TI", ["https://www.linkedin.com/jobs/1"]) == ["TI"]
    assert set(report._employer_forms("Example Fab", ["https://exfab.wd1.myworkdayjobs.com/x"])) == {
        "Example Fab", "Example", "exfab", "EXFAB", "Exfab"}
    # (the review's) an all-capitals short name, and one in parentheses
    assert "NXP" in report._employer_forms("NXP Semiconductors", [])
    assert "TEL" in report._employer_forms("Tokyo Electron (TEL)", [])


def test_an_employers_one_word_forms_are_taken_out_as_written_and_never_inside_an_email():
    """The review of issue #112, note 35: "How early can you start?" kept at Early Warning, an email whose first
    name is the employer's first word left whole (for the scrubber to take out whole), "Careers at NXP" said."""
    say, _ = report._anonymous({"company": "Early Warning Services"}, None, [], [])
    assert (say("How early can you start? Early Warning thanks you.")
            == "How early can you start? the employer thanks you.")
    say, _ = report._anonymous({"company": "Grant Thornton"}, None, [], ["grant.lee@mail.example.org"])
    said = say("Reply to grant.lee@mail.example.org about Grant Thornton")
    assert "lee" not in said and "mail.example" not in said and "Grant" not in said, said  # (the email whole)
    say, _ = report._anonymous({"company": "NXP Semiconductors"}, None, [], [])
    assert "NXP" not in say("Careers at NXP")
    assert report._employer_forms("Example Fab", ["https://careers.example-fab.example/x"]) == [
        "Example Fab", "Example"]  # an employer's own address names no tenant


def test_an_anonymous_report_leaves_out_the_parent_group_the_jobs_place_and_never_says_the_the(job_apply_home,
                                                                                               tmp_path):
    """Issue #112, note 42 (adds to note 35): an anonymous report took the employer's own name out, but its parent
    group's stayed in the page title and a heading ("Job Details | Acme Group", "Jobs at Acme Group": the employer as
    surely, on SuccessFactors); the job's place stayed in a button ("Remove Springfield, IL", Eightfold's location
    chip: an employer and a city are one job); and "the Example Fab" became "the the employer". Each goes, from the
    issue and the saved pages; the page's own words stay."""
    folder = tmp_path / "0031-example-fab-equipment-technician"
    snap = folder / "debug" / "20261016-100000-000-stop"
    snap.mkdir(parents=True)
    posting = "https://career5.successfactors.com/career?company=exfabP&career_job_req_id=4321"
    (snap / "page.html").write_text(
        "<html><head><title>Job Details | Acme Group</title></head><body><h1>Jobs at Acme Group</h1>"
        "<h2>Additional Personal Information</h2><p>Acme Group companies: Example Fab</p>"
        "<button>Remove Springfield, IL</button><label for=a>Country of Residence</label><input id=a></body></html>",
        encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": posting, "title": "Job Details | Acme Group", "note": "stopped: stuck",
        "frames": [{"file": "page.html", "url": posting}],
        "fields": [{"label": "Country of Residence", "kind": "combobox", "required": True, "value": ""}]}),
        encoding="utf-8")
    job = {"id": 31, "title": "Equipment Technician", "company": "Example Fab", "ats": "successfactors",
           "location": "Springfield, IL", "url": posting, "folder": str(folder)}
    r = Run(31, "Equipment Technician", "Example Fab", status="needs_you", need="stuck", url=posting,
            reason="I couldn't find the button that moves this application on.",
            log=["agreed to the employer's notice “Privacy Policy of the Example Fab” for "
                 "you (settings.accept_notices)"],
            page_info={"url": posting, "title": "Equipment Technician Job Details | Acme Group",
                       "headings": ["Jobs at Acme Group", "Additional Personal Information"],
                       "actions": ["Remove Springfield, IL", "Remove Springfield, Illinois", "Next"],
                       "fields": [{"label": "Country of Residence", "kind": "combobox", "required": True,
                                   "empty": True}]})
    out = report.build(job, r, PERSON, anonymous=True)
    text = out["preview"]
    assert "(the job Job Details | the employer)" in text, text
    assert "Headings: Jobs at the employer | Additional Personal Information" in text
    assert "Buttons: Remove the job's location | Remove the job's location | Next" in text
    assert "### Page saved 20261016-100000-000-stop: Job Details | the employer" in text
    assert "notice “Privacy Policy of the employer”" in text and "the the" not in text
    with zipfile.ZipFile(out["zip"]) as z:
        everything = "".join(z.read(n).decode("utf-8") for n in z.namelist())
    assert "Country of Residence" in everything and "Additional Personal Information" in everything
    for named in ("Acme", "Example", "Springfield", "exfab"):
        assert named not in everything and named not in out["issue_url"], named
    plain = report.build(job, r, PERSON)["preview"]
    assert "Acme Group" in plain and "Springfield" in plain  # unless asked, the report says which job
    # a note (filed many together) never says "the the" either
    assert "Privacy Policy of the employer”" in report.note(job, r, PERSON)


def test_the_names_a_page_gives_an_employer_are_found_and_its_own_words_are_not():
    """What an anonymous report takes for another name of the employer (note 42): a title's part beside the job's or
    the site's words, one before a careers site's words, and what follows "Jobs at" or "Careers at"; never a page's
    own words, a step's name or a job system's."""
    found = report._names_given(
        ["the job Job Details | Acme Group", "Careers | Northwind", "Contoso Careers", "My Information | Workday",
         "Careers | SAP SuccessFactors", "Voluntary Self-Identification | Equal Opportunity Employer", "Apply"],
        ["Your Career at Fabrikam Holdings", "Life at Tailspin.", "Additional Personal Information",
         "Remove the job's location", "Careers at the employer", "Join our talent community"])
    assert found == {"Acme Group", "Northwind", "Contoso", "Fabrikam Holdings", "Tailspin"}, found
    # its own domain's name too, never a frame's (a CAPTCHA's)
    say, _ = report._anonymous({"company": "Example Fab", "url": "https://jobs.exfabgroup.example/x"}, None,
                               ["https://jobs.exfabgroup.example/x", "https://www.puzzlecheck.example/frame"], [])
    assert say("Welcome to exfabgroup and EXFABGROUP, checked by Puzzlecheck") == \
        "Welcome to the employer and the employer, checked by Puzzlecheck"
    assert set(report._place_forms("Springfield, IL; Shelbyville, Illinois, United States; Remote")) == {
        "Springfield, IL", "Springfield", "Shelbyville, Illinois, United States", "Shelbyville, Illinois",
        "Shelbyville"}


def test_an_anonymous_report_takes_out_only_names_never_everyday_words_and_every_way_a_place_is_written():
    """The review of the fix for note 42: "Employment at Will" made "Will" a name, so "Will you now ... require
    sponsorship?" read "the employer you now ...", and every "will" left the saved pages; "Engineering Careers" and
    "Remote Jobs" took "Engineering" and "Remote" out everywhere, any case. A name found on a page is taken out only as
    written, never an everyday word. And a job's place written as Workday writes it ("US-IL-Springfield"), without its
    comma ("Springfield IL"), with its ZIP code, or one of a list ("Springfield, IL / Shelbyville, IL") goes too."""
    run_ = Run(32, "Equipment Technician", "Example Fab", status="needs_you", need="stuck",
               page_info={"title": "Engineering Careers", "headings": ["Employment at Will", "Life at Northwind Group"],
                          "actions": ["Remote Jobs"]})
    job = {"id": 32, "title": "Equipment Technician", "company": "Example Fab", "ats": "workday",
           "location": "US-IL-Springfield / Shelbyville IL", "url": "https://exfab.wd1.myworkdayjobs.com/x"}
    say, secrets = report._anonymous(job, run_, [job["url"]], [])
    for kept in ("Will you now or in the future require sponsorship?", "Engineering degree required?",
                 "Do you have engineering experience?", "Remote work: will you travel?"):
        assert say(kept) == kept, kept
    assert not {"Will", "will", "Engineering", "Remote"} & set(secrets)
    assert (say("Life at Northwind Group, where Northwind people work")
            == "Life at the employer, where the employer people work")
    assert say("the northwind way") == "the northwind way"  # (as written only)
    for place in ("Remove Springfield, IL 62701", "Remove US-IL-Springfield", "Remove Springfield IL",
                  "Remove Shelbyville, Illinois"):
        assert say(place) == "Remove the job's location", place


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
    r = Run(12, "Field Service Engineer", "Acme Semi", status="needs_you", need="questions", code="questions",
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
    assert "**Job system:** workday \u00b7 **Stop:** `questions` \u00b7 **Noted:** " in text  # (its code: T1)
    assert "1 question(s) your profile doesn't answer." in text
    assert "15. " in text and "16. " not in text and "step 17\n" not in text  # its last 15 steps
    assert "(a Workday address) (Apply to the employer - REDACTED)" in text  # an address says its job system only
    assert url not in text and "wd1" not in text
    assert "- City (text, required, filled)" in text and "- Phone (text, required, empty)" in text
    assert ("- Are you authorized to work in the US? (select, 3 choices) [Application Questions]: "
            "ValueError: Picked … but the field shows …; set it by hand") in text
    assert "- Highest degree (combobox): your profile's answer … isn't one of its choices" in text
    assert "- Country (combobox): ValueError: … doesn't match any suggestion\n" in text
    assert "(yours: …)" in text
    for private in (*PRIVATE, *MORE_PRIVATE, "SESS123", "jsessionid", "Maybe", "Klingon", "Wizardry", "Atlantis",
                    "Albania", "Protected veteran"):
        assert private not in text, private


def test_a_note_says_every_address_by_its_job_system_and_the_employer_by_any_of_its_forms(job_apply_home):
    """Notes 25 and 34: a note kept the job's address (its host, path, title slug and requisition
    number) and a password reset's path with its one-time token, and an employer named "Acme
    Semiconductor Group" stayed in its short forms. A note says an address by its job system only."""
    host = "https://acme.wd5.myworkdayjobs.com/External"
    job = {"id": 22, "title": "Test Technician II (Hybrid)", "company": "Acme Semiconductor Group", "ats": "workday",
           "url": f"{host}/job/Phoenix-(AZ)/Test-Technician-II-Hybrid_R98765?x=SECRET"}
    r = Run(22, "Test Technician II (Hybrid)", "Acme Semiconductor Group", status="needs_you", need="stuck",
            reason=f"asked for a password reset: {host}/passwordreset/TOKEN123/ and {host}/job/Phoenix-(AZ)/Test_R1",
            url=f"{host}/job/Phoenix-(AZ)/Test-Technician-II-Hybrid_R98765/apply?x=SECRET",
            log=[f"opened {host}/job/Phoenix-(AZ)/Test-Technician-II-Hybrid_R98765/apply?x=SECRET",
                 f"followed the reset link {host}/passwordreset/TOKEN123/ on acme"],
            page_info={"url": f"{host}/passwordreset/TOKEN123/", "title": "Apply I Acme Group",
                       "headings": ["Acme Career", "Reset Password"]})
    text = report.note(job, r, PERSON)
    assert "(a Workday address)" in text and text.count("(a Workday address)") == 5
    assert " (Apply I the employer)\n" in text and "Headings: the employer Career | Reset Password" in text
    for left in ("TOKEN123", "SECRET", "passwordreset", "Phoenix", "R98765", "Test-Technician", "))", "Acme", "acme",
                 "myworkdayjobs"):
        assert left not in text, left


def test_a_note_says_which_job_system_the_job_stopped_on(job_apply_home):
    """The page it stopped on names the system (a company's posting that sends the application
    to Workday), else the posting's; a careers site of the employer's own is said so."""
    r = Run(13, "Technician", "Acme Semi", need="stuck", url="https://acme.wd1.myworkdayjobs.com/External/apply")
    assert "at a Workday employer" in report.note({"id": 13, "ats": "company_site"}, r, PERSON)
    r.url = "https://careers.acme-semi.example/apply"
    assert "at an iCIMS employer" in report.note({"id": 13, "ats": "icims"}, r, PERSON)
    assert "at an employer with its own careers site" in report.note({"id": 13, "ats": "company_site"}, r, PERSON)


def _quoting_run(job_id: int) -> Run:
    """A run whose page echoed an answer the person gave on a card ("Nights") in its errors, and whose log has the
    code the inbox gave for it (482913): the desk's history keeps both out, and a report (Applier._report_hidden)."""
    return Run(job_id, "Field Service Engineer", "Example Fab", status="needs_you", need="stuck",
               reason="review page shows errors: Shift: Nights isn't open for this job",
               log=["typed the code 482913 the inbox gave", "the review page shows errors"],
               once={"Shift": "Nights"},
               page_info={"url": "https://example.com/apply/review", "title": "Review",
                          "errors": ["Shift: Nights isn't open for this job"]})


def test_a_report_and_a_note_hide_what_the_desk_history_hides(srv, job_apply_home, monkeypatch):
    """A page's error the desk quotes (its reason, the page's errors) can echo what the person typed on a card, and a
    log line the code the inbox gave: the history keeps them out of its line, and a report and a note (filed in public)
    did not (the review of Oct 18). The desk's Report, Claude's report_problem and the note the desk
    takes all hide them."""
    import time

    import httpx

    from job_apply import desk as desk_module
    from job_apply import pipeline
    from job_apply.pipeline import Applier

    job = srv.add_job(url="https://example.com/jobs/fse", title="Field Service Engineer", company="Example Fab")["job"]
    monkeypatch.setitem(pipeline._MAIL_TAKEN, ("noreply@example.com", "482913", time.time()), job["id"])
    r = _quoting_run(job["id"])
    hide = Applier._report_hidden(r)
    assert {"Nights", "482913"} <= hide

    def hidden(text: str) -> None:
        assert "Nights" not in text and "482913" not in text, text
        assert "review page shows errors" in text, text  # (the desk's own words stay)

    built = report.build(job, r, PERSON, hide=hide)
    hidden(built["preview"])
    hidden(parse_qs(urlsplit(built["issue_url"]).query)["body"][0])
    hidden(report.note(job, r, PERSON, hide=hide))

    # (and so do the callers: the desk's Report, Claude's report_problem, the note the desk takes on a stop)
    d = desk_module.get_desk(srv)
    d.applier.start = lambda: None
    d.applier.runs[job["id"]] = r

    async def go():
        await d.start(port=0, open_browser=False)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{d.port}", timeout=20, trust_env=False) as c:
                said = await c.post(f"/api/job/{job['id']}/report", headers={"x-desk-token": d.token}, json={})
                return said.json()
        finally:
            await d.stop()
    try:
        for out in (run(go()), run(srv.report_problem(job["id"]))):
            hidden(out["preview"])
            hidden(parse_qs(urlsplit(out["issue_url"]).query)["body"][0])
        d.applier._take_note(r)
        [taken] = (job_apply_home / "notes").glob("*.md")
        hidden(taken.read_text(encoding="utf-8"))
    finally:
        desk_module._desk = None


def _reported(srv, job_apply_home, job, r) -> tuple[str, str]:
    """Claude's report_problem on the desk's run `r`, and the note the desk takes on its stop: their texts."""
    from job_apply import desk as desk_module

    d = desk_module.get_desk(srv)
    d.applier.runs[job["id"]] = r
    try:
        out = run(srv.report_problem(job["id"]))
        d.applier._take_note(r)
    finally:
        desk_module._desk = None
    [taken] = (job_apply_home / "notes").glob("*.md")
    return (out["preview"] + "\n" + parse_qs(urlsplit(out["issue_url"]).query)["body"][0],
            taken.read_text(encoding="utf-8"))


def test_a_report_keeps_the_profiles_everyday_words_the_page_says_too(srv, job_apply_home):
    """The review of Oct 18: given every word of the profile to keep out (as the history keeps out of its reason), a
    report took out what the profile shares with the page, the template's own: "REDACTED at Example Fab" for a job
    titled as the profile's wanted titles, "https://www.REDACTED.com/…", "REDACTED Phone", "the REDACTED sign-in page".
    The profile's details are report_strings' to take out; the run's own are what's this run's alone, never a list's
    own choice that went in, nor anything in an address or in the job's own lines."""
    import yaml

    from job_apply.pipeline import WentIn

    path = job_apply_home / "profile.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["personal"]["phone_type"] = "Mobile"
    data["preferences"].update(how_did_you_hear="LinkedIn", titles=["Field Service Engineer"])
    data["work_history"][0]["description"] = "Kept Workday and SAP records."
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    job = srv.add_job(url="https://www.linkedin.com/jobs/view/4012345678", title="Field Service Engineer",
                      company="Example Fab")["job"]
    went = WentIn(answers={"a": {"page": "Apply", "label": "Phone Type", "value": "Mobile Phone", "kind": "choice"},
                           "b": {"page": "Apply", "label": "Source", "value": "LinkedIn", "kind": "choice"}})
    r = Run(job["id"], "Field Service Engineer", "Example Fab", status="needs_you", need="stuck",
            reason="review page shows errors: Mobile Phone is required", went_in=went,
            log=["opened https://www.linkedin.com/jobs/view/4012345678", "on the Workday sign-in page"],
            page_info={"url": "https://example.wd1.myworkdayjobs.com/x", "title": "Apply",
                       "fields": [{"label": "Mobile Phone", "kind": "text", "required": True, "empty": False}]})
    text, taken = _reported(srv, job_apply_home, job, r)
    for kept in ("**Job:** Field Service Engineer at Example Fab", "https://www.linkedin.com/jobs/view/4012345678",
                 "Mobile Phone is required", "- Mobile Phone (text, required, filled)", "the Workday sign-in page"):
        assert kept in text, kept
    assert "## stuck: Field Service Engineer, at" in taken and "Mobile Phone is required" in taken
    assert "the Workday sign-in page" in taken and "REDACTED" not in taken


def test_a_report_hides_the_runs_own_answers_however_a_page_echoes_them(srv, job_apply_home, monkeypatch):
    """The review of Oct 18: a phone typed on a card ("602-555-0177") came back in a page's error written another way,
    a long answer cut short by the page's error, and an account form's 3-letter answer and a 3-letter code (the history
    hides 4 or more): all reached a report and a note. None does now, and the desk's own words stay."""
    import time

    from job_apply import pipeline

    long = "I maintained 300mm etch tools across two fabs and cut unplanned downtime by a third over four years."
    job = srv.add_job(url="https://example.com/jobs/fse", title="Technician", company="Example Fab")["job"]
    monkeypatch.setitem(pipeline._MAIL_TAKEN, ("noreply@example.com", "Q7Z", time.time()), job["id"])
    r = Run(job["id"], "Technician", "Example Fab", status="needs_you", need="stuck",
            reason="review page shows errors: Phone (602) 555-0177 isn't a mobile number",
            once={"Phone": "602-555-0177", "Why this job?": long}, account_answers={"First pet": "Rex"},
            log=["the page said 6025550177 isn't valid", "typed the code Q7Z the inbox gave",
                 "the security answer Rex was refused"],
            page_info={"url": "https://example.com/apply", "title": "Apply",
                       "errors": [f"{long[:50]}… exceeds 100 characters", "602.555.0177 isn't valid"]})
    for text in _reported(srv, job_apply_home, job, r):
        for value in ("555-0177", "5550177", "555.0177", "300mm etch", "across two fabs", "Q7Z", "Rex"):
            assert value not in text, (value, text)
        assert "review page shows errors" in text and "exceeds 100 characters" in text, text


def test_a_report_hides_the_runs_own_in_an_addresss_path_but_never_its_host_nor_a_dates_digits(
        srv, job_apply_home, monkeypatch):
    """The second look at the review's fixes (Oct 18): an address was left whole unless it was all one value, so a code
    in a verify link and an email typed into a form, in a profile page's path, stayed; and an answer with a few
    numbers in it ("Worked 2019-2023 at a fab") was hidden by its digits run together, "2019-2023" anywhere. In an
    address, a value goes as a whole word of its path or query, never from its host; a number's digits only for a
    value that's mostly a number."""
    import time

    from job_apply import pipeline

    job = srv.add_job(url="https://example.com/jobs/fse", title="Technician", company="Example Fab")["job"]
    monkeypatch.setitem(pipeline._MAIL_TAKEN, ("noreply@example.com", "482913", time.time()), job["id"])
    r = Run(job["id"], "Technician", "Example Fab", status="needs_you", need="stuck", reason="stopped on the profile",
            once={"Email": "jq.applies@example.org", "Experience": "Worked 2019-2023 at a fab", "Site": "fabnights"},
            log=["opened https://fabnights.example/verify/482913?code=482913",
                 "opened https://fabnights.example/profile/jq.applies@example.org/edit",
                 "the page shows dates 2019-2023 shown"])
    text = _reported(srv, job_apply_home, job, r)[0]
    for value in ("482913", "jq.applies"):
        assert value not in text, (value, text)
    assert ("https://fabnights.example/verify/REDACTED" in text
            and "https://fabnights.example/profile/REDACTED/edit" in text)
    assert "dates 2019-2023 shown" in text, text


def test_an_anonymous_report_and_a_note_name_no_other_employer_the_desk_knows(srv, job_apply_home, tmp_path):
    """The owner's note 51 (Oct 20): an anonymous report named another employer in its log (a job it waited on for the
    inbox, a tab gone on to another's site). Every employer the desk knows but the job's own is "another employer" in
    an anonymous report, in its saved pages, and in a note: the tracker's jobs', the person's companies file's (and
    their short names), the plugin's lists'. The job's own is "the employer" as before, and a run that isn't the
    job's own is never in its report."""
    (job_apply_home / "companies.yaml").write_text(
        "lists: []\ncompanies:\n  - name: Quarry Ridge Optics\n    careers_url: https://careers.quarry-ridge.example\n"
        "    aliases: [QRO]\n", encoding="utf-8")
    srv.add_job(url="https://careers.tamarisk-fab.example/job/7", title="Technician", company="Tamarisk Fab Inc.")
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/x", title="Technician",
                      company="Acme Semi")["job"]
    folder = Path(job["folder"])
    snap = folder / "debug" / "20261020-090000-000-stop"
    snap.mkdir(parents=True)
    (snap / "page.html").write_text("<html><body><h1>My Information</h1><p>Also hiring: Quarry Ridge Optics and "
                                    "Tamarisk Fab</p><label for=a>Company</label><input id=a></body></html>",
                                    encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply", "title": "Apply - Acme Semi",
        "note": "stopped: stuck", "frames": [{"file": "page.html", "url": "https://acme.wd1.myworkdayjobs.com/x"}],
        "fields": [{"label": "Company", "kind": "text", "required": True, "value": ""}]}), encoding="utf-8")
    r = Run(job["id"], "Technician", "Acme Semi", status="needs_you", need="stuck", code="step_not_saved",
            reason="The page didn't move on.",
            log=["opened https://acme.wd1.myworkdayjobs.com/External/job/x",
                 "waiting: Tamarisk Fab is signing in or waiting on an email; this one carries on after that",
                 "its tab went on to Quarry Ridge Optics's careers site (QRO), so I opened the job again",
                 "filled 3 field(s) on “My Information” at Acme Semi"])
    employers = srv.tracker().companies()
    out = report.build(job, r, PERSON, anonymous=True, employers=employers)
    text = out["preview"]
    with zipfile.ZipFile(out["zip"]) as z:
        everything = text + "".join(z.read(n).decode("utf-8") for n in z.namelist())
    note = report.note(job, r, PERSON, employers=employers)
    for said in (everything, note):
        for named in ("Tamarisk", "Quarry Ridge", "QRO", "Acme"):
            assert named not in said, (named, said)
    assert "waiting: another employer is signing in" in text and "another employer's careers site" in text, text
    assert "on “My Information” at the employer" in text and "on “My Information” at the employer" in note
    # (a report that says which job it was names its employer, and never another's run)
    named = report.build(job, r, PERSON, employers=employers)["preview"]
    assert "Acme Semi" in named
    other = Run(job["id"] + 1, "Technician", "Tamarisk Fab Inc.", status="needs_you", need="stuck",
                log=["filled 2 field(s) at Tamarisk Fab"])
    assert "Tamarisk" not in report.build(job, other, PERSON, employers=employers)["preview"]


def test_other_employers_are_named_every_way_and_the_saved_pages_markup_stays_whole(srv, job_apply_home):
    """The review of note 51's fix: another employer is also taken out by its name less a part in brackets ("Quarry
    Labs (Henderson data center)": "Quarry Labs"), a bracket's own name ("Saguaro Gaming (Red Mesa Resorts)"), in
    capitals ("TAMARISK SYSTEMS"), and by a short name of the curated few ("Northrop" of "Northrop Grumman"); never
    the job's place ("Pinal County", an employer too). And a short one-word name ("TEL", "Switch") never breaks the
    saved pages' markup (type="tel", role="switch")."""
    for name in ("Quarry Labs (Henderson data center)", "Saguaro Gaming (Red Mesa Resorts)", "Tamarisk Systems",
                 "Northrop Grumman", "Pinal County", "TEL", "Switch"):
        srv.add_job(url=f"https://careers.example/{len(name)}/{name[:3]}", title="Technician", company=name)
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/x", title="Technician", company="Acme Semi",
                      location="Pinal County, AZ")["job"]
    snap = Path(job["folder"]) / "debug" / "20261021-090000-000-stop"
    snap.mkdir(parents=True)
    (snap / "page.html").write_text(
        '<html><body><h1>My Information</h1><p>Also hiring at Quarry Labs and TAMARISK SYSTEMS</p>'
        '<label for=a>Phone</label><input id=a type="tel" autocomplete="tel-national">'
        '<div role="switch" aria-checked="false">Text me</div></body></html>', encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply", "title": "Apply - Acme Semi",
        "note": "stopped: stuck", "frames": [{"file": "page.html", "url": "https://acme.wd1.myworkdayjobs.com/x"}],
        "fields": [{"label": "Phone", "kind": "text", "required": True, "value": ""}]}), encoding="utf-8")
    r = Run(job["id"], "Technician", "Acme Semi", status="needs_you", need="stuck", reason="The page didn't move on.",
            log=["waited for Quarry Labs", "its tab went on to Red Mesa Resorts' site", "TAMARISK SYSTEMS said no",
                 "Northrop called", "the job is in Pinal County", "TEL and Switch are hiring"])
    employers = srv.tracker().companies()
    out = report.build(job, r, PERSON, anonymous=True, employers=employers)
    with zipfile.ZipFile(out["zip"]) as z:
        page = z.read("pages/20261021-090000-000-stop.html").decode("utf-8")
    for named in ("Quarry", "Red Mesa", "TAMARISK", "Northrop", "TEL ", "Switch"):
        assert named not in out["preview"], (named, out["preview"])
    assert "Pinal County" not in out["preview"] and "the job's location" in out["preview"], out["preview"]
    assert "Quarry Labs" not in page and "TAMARISK" not in page, page
    assert 'type="tel"' in page and 'autocomplete="tel-national"' in page and 'role="switch"' in page, page


def test_another_employers_first_word_alone_and_its_words_in_a_sentence_are_never_taken_out(srv, job_apply_home):
    """The review of fix-ready-check, round 2: a name's first word by rule ("Salt" of "Salt River Project", "Light" of
    "Light & Wonder", "Grant" of "Grant Thornton", "Tokyo" of "Tokyo Electron", "Station" of "Station Casinos") and a
    one-word name in any case ("the switch" for "Switch") took everyday words out of a report and a note, the job's own
    title too. A one-word name goes as written and in capitals, and never inside the job's own title or place."""
    for name in ("Salt River Project", "Light & Wonder", "Grant Thornton", "Switch", "Tokyo Electron (TEL)",
                 "Station Casinos (Red Rock Resorts)"):
        srv.add_job(url=f"https://careers.example/{len(name)}/{name[:3]}", title="Technician", company=name)
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/x",
                      title="Light Industrial Work Station Technician", company="Acme Semi",
                      location="Tokyo, Japan")["job"]
    r = Run(job["id"], "Light Industrial Work Station Technician", "Acme Semi", status="needs_you", need="stuck",
            reason="The page didn't move on.",
            log=["the page says: Relocation to Salt Lake City is offered", "Light industrial experience wanted",
                 "Grant permission to text you", "turned on the switch for texts", "Switch and SWITCH are hiring",
                 "TEL said no", "waited for Grant Thornton", "Station Casinos called"])
    employers = srv.tracker().companies()
    text = report.build(job, r, PERSON, anonymous=True, employers=employers)["preview"]
    note = report.note(job, r, PERSON, employers=employers)
    for said in (text, note):
        for kept in ("Salt Lake City", "Light industrial experience", "Grant permission", "the switch for texts"):
            assert kept in said, (kept, said)
        assert "another employer and another employer are hiring" in said, said
        for named in ("TEL", "Grant Thornton", "Station Casinos", "Switch"):
            assert named not in said, (named, said)
    # (the note names the job: its title whole, its place too)
    assert "Light Industrial Work Station Technician" in note, note
    switch = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/y", title="Network Switch Technician",
                         company="Acme Semi")["job"]
    on = Run(switch["id"], "Network Switch Technician", "Acme Semi", status="needs_you", need="stuck",
             log=["filled 2 field(s) on Network Switch Technician", "Switch is hiring"])
    said = report.note(switch, on, PERSON, employers=employers)
    assert "Network Switch Technician" in said and "another employer is hiring" in said, said


def test_an_anonymous_reports_saved_pages_lose_a_one_word_employer_from_their_words_alone(srv, job_apply_home):
    """The review of fix-ready-check, round 2: a saved page kept another employer's one-word name ("Intel", "TEL"),
    which comes out of the pages' words (their title too) and their words for a person (alt, aria-label) as written,
    never out of their markup: type="tel", role="switch", autocomplete="tel-national", an id stay."""
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/x", title="Technician",
                      company="Acme Semi")["job"]
    snap = Path(job["folder"]) / "debug" / "20261021-100000-000-stop"
    snap.mkdir(parents=True)
    (snap / "page.html").write_text(
        '<html><head><title>Intel Careers</title></head><body><h1>My Information</h1>'
        '<p>Have you worked for TEL or Intel before?</p><img src="x.png" alt="Intel logo">'
        '<label for=a>Phone</label><input id="Intel-box" type="tel" autocomplete="tel-national" '
        'aria-label="Intel phone">'
        '<div role="switch" class="Switch" aria-checked="false">Text me</div></body></html>', encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply", "title": "Apply - Acme Semi",
        "note": "stopped: stuck", "frames": [{"file": "page.html", "url": "https://acme.wd1.myworkdayjobs.com/x"}],
        "fields": [{"label": "Have you worked for TEL or Intel before?", "kind": "radio", "required": True,
                    "value": ""}]}), encoding="utf-8")
    r = Run(job["id"], "Technician", "Acme Semi", status="needs_you", need="stuck", reason="The page didn't move on.")
    out = report.build(job, r, PERSON, anonymous=True, employers=srv.tracker().companies())
    with zipfile.ZipFile(out["zip"]) as z:
        page = z.read("pages/20261021-100000-000-stop.html").decode("utf-8")
        expect = z.read("pages/20261021-100000-000-stop.expect.json").decode("utf-8")
    for said in (page, expect):
        assert "Intel " not in said and "TEL " not in said, said
    assert ("<title>REDACTED Careers</title>" in page and 'alt="REDACTED logo"' in page
            and 'aria-label="REDACTED phone"' in page), page
    assert ('type="tel"' in page and 'autocomplete="tel-national"' in page and 'role="switch"' in page
            and 'id="Intel-box"' in page and 'class="Switch"' in page), page


def test_an_anonymous_reports_saved_pages_lose_another_employer_named_only_in_their_title_or_alt(srv, job_apply_home):
    """The review of fix-ready-check, round 3: another employer named only in a page's <title> or an image's alt
    ("Intel Careers | Lam Research", alt="Intel logo") was never looked for there (the pages' words left their head and
    attributes out), and stayed in the saved page."""
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/x", title="Technician",
                      company="Acme Semi")["job"]
    snap = Path(job["folder"]) / "debug" / "20261021-110000-000-stop"
    snap.mkdir(parents=True)
    (snap / "page.html").write_text(
        '<html><head><title>Intel Careers | Lam Research</title></head><body><h1>My Information</h1>'
        '<img src="x.png" alt="Intel logo"><label for=a>Phone</label><input id=a type="tel"></body></html>',
        encoding="utf-8")
    (snap / "snapshot.json").write_text(json.dumps({
        "url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply", "title": "Apply - Acme Semi",
        "note": "stopped: stuck", "frames": [{"file": "page.html", "url": "https://acme.wd1.myworkdayjobs.com/x"}],
        "fields": [{"label": "Phone", "kind": "text", "required": True, "value": ""}]}), encoding="utf-8")
    r = Run(job["id"], "Technician", "Acme Semi", status="needs_you", need="stuck", reason="The page didn't move on.")
    out = report.build(job, r, PERSON, anonymous=True, employers=srv.tracker().companies())
    with zipfile.ZipFile(out["zip"]) as z:
        page = z.read("pages/20261021-110000-000-stop.html").decode("utf-8")
    assert "Intel" not in page and "Lam Research" not in page, page
    assert 'type="tel"' in page, page
