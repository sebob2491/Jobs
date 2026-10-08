"""The tools Claude calls: what they refuse, which job they act for, what Claude reads back."""

import asyncio
from pathlib import Path

import pytest
from conftest import browser_available, run

from job_apply import config
from job_apply.autofill import resolve_field

FORM = """<!doctype html><html><body><h1>{title}</h1>
<form action="{action}" method="get">
 <label for="em">Email</label><input id="em" name="em" type="text">
 <label for="pw">Password</label><input id="pw" name="pw" type="password">
 <label for="rs">Resume</label><input id="rs" name="rs" type="file">
 <button type="submit">Submit application</button>
</form></body></html>"""
DONE = "<!doctype html><html><body><p>Thank you for applying!</p></body></html>"
INDEED_HOST = """<!doctype html><html><body><h1>Careers at Example</h1>
<iframe src="https://apply.indeed.com/form" style="width:900px;height:500px"></iframe></body></html>"""
INDEED_FORM = """<!doctype html><html><body><form action="https://apply.indeed.com/done" method="get">
<label for="q">Phone</label><input id="q" name="q" type="text" value="480-555-0123">
<button type="submit">Submit your application</button></form></body></html>"""
PAGES = {
    "https://careers.example.com/a": FORM.format(title="Job A", action="https://careers.example.com/a-done"),
    "https://jobs.other.com/b": FORM.format(title="Job B", action="https://jobs.other.com/b-done"),
    "https://careers.example.com/indeed-host": INDEED_HOST,
    "https://apply.indeed.com/form": INDEED_FORM,
    "https://acme.wd1.myworkdayjobs.com/en-US/External/login": FORM.format(title="Sign in", action="x"),
}


def routed(srv) -> list[str]:
    """The browser with every request answered by the pages above; nothing leaves the machine."""
    sent: list[str] = []

    async def route(r):
        url = r.request.url.split("?")[0]
        if url.endswith("done"):
            sent.append(r.request.url)
            return await r.fulfill(status=200, content_type="text/html", body=DONE)
        if url in PAGES:
            return await r.fulfill(status=200, content_type="text/html", body=PAGES[url])
        return await r.abort()

    async def go():
        await srv.browser._launch()
        await srv.browser._ctx.route("**/*", route)
    run(go())
    return sent


needs_browser = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


@needs_browser
def test_indeeds_form_inside_an_employers_page_is_the_persons_to_submit(srv):
    sent = routed(srv)
    job = srv.add_job(url="https://careers.example.com/indeed-host", title="Tech", company="Example")["job"]
    opened = run(srv.open_application(job_id=job["id"]))
    assert opened["submit_policy"] == "user_clicks_submit"
    out = run(srv.submit_application(user_confirmed=True))
    assert out["submitted"] is False and "Indeed" in out["reason"]
    # and the browser itself won't press it, whoever asks
    buttons = run(srv.browser.find_submit())
    with pytest.raises(srv.SubmitBlocked):
        run(srv.browser.press_submit(buttons[-1]["id"]))
    assert sent == []


@needs_browser
def test_a_page_opened_by_its_address_isnt_the_job_opened_before(srv):
    sent = routed(srv)
    a = srv.add_job(url="https://careers.example.com/a", title="Tech", company="Intel")["job"]
    Path(a["folder"], "Sam_Rivera_Resume.pdf").write_bytes(b"%PDF-1.4 tailored for Intel")
    run(srv.open_application(job_id=a["id"]))
    run(srv.open_application(url="https://jobs.other.com/b"))
    assert srv.browser.current_job_id is None
    with pytest.raises(ValueError, match="No job selected"):
        run(srv.submit_application(user_confirmed=True))
    # a job saved at that address becomes the current one
    b = srv.add_job(url="https://jobs.other.com/b", title="FSE", company="ASML")["job"]
    run(srv.open_application(url="https://jobs.other.com/b"))
    assert srv.browser.current_job_id == b["id"]
    assert sent == [] and srv.get_job(a["id"])["job"]["status"] != "applied"


@needs_browser
def test_a_saved_password_goes_only_into_a_password_box_on_its_own_site(srv, job_apply_home):
    routed(srv)
    config.secrets_path().write_text("workday_password: S3cret-Workday!\n")
    run(srv.browser.goto("https://careers.example.com/a"))
    fields = {f["label"]: f["id"] for f in run(srv.inspect_form(False))["fields"]}
    out = run(srv.fill_secret(fields["Email"], "workday_password"))
    assert out["ok"] is False and "password box" in out["error"]
    out = run(srv.fill_secret(fields["Password"], "workday_password"))  # not a Workday site
    assert out["ok"] is False and "another site" in out["error"]
    assert "S3cret" not in str(run(srv.inspect_form(False)))
    run(srv.browser.goto("https://acme.wd1.myworkdayjobs.com/en-US/External/login"))
    fields = {f["label"]: f["id"] for f in run(srv.inspect_form(False))["fields"]}
    assert run(srv.fill_secret(fields["Password"], "workday_password")) == {"ok": True}


@needs_browser
def test_the_desks_own_files_arent_uploaded(srv, job_apply_home):
    routed(srv)
    config.secrets_path().write_text("workday_password: S3cret-Workday!\n")
    run(srv.browser.goto("https://careers.example.com/a"))
    fields = {f["label"]: f["id"] for f in run(srv.inspect_form(False))["fields"]}
    out = run(srv.fill_form([{"id": fields["Resume"], "value": str(config.secrets_path())}]))
    assert not out["results"][0]["ok"] and "desk's own files" in out["results"][0]["error"]


def test_tool_errors_reach_claude_in_their_own_words(srv):
    from mcp.server.mcpserver.exceptions import ToolError

    async def call(name, args):
        with pytest.raises(ToolError) as e:
            await srv.mcp.call_tool(name, args)
        return str(e.value)
    assert "No job with id 999" in asyncio.get_event_loop().run_until_complete(call("get_job", {"job_id": 999}))
    job = srv.add_job(url="https://example.com/jobs/1", title="Tech", company="Example")["job"]
    assert "Unknown status 'aplied'" in run(call("update_job", {"job_id": job["id"], "status": "aplied"}))


def test_a_profile_with_a_typo_says_where(job_apply_home):
    config.profile_path().write_text('documents:\n  resume: "C:\\Users\\Sam\\resume.pdf"\n')
    with pytest.raises(ValueError, match=r"typo near line 2.*backslashes"):
        config.Profile.load()
    config.profile_path().write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="sections like"):
        config.Profile.load()


def test_passwords_are_read_as_written(job_apply_home):
    path = config.secrets_path()
    for written, meant in (("0123456", "0123456"), ("yes", "yes"), ("12:30:45", "12:30:45"),
                           ("!Summer2024x", "!Summer2024x"), ("*pw", "*pw"), ("'it''s'", "it's"), ('"q\\"x"', 'q"x'),
                           ("plain # my work one", "plain")):
        path.write_text(f"workday_password: {written}\n")
        assert config.get_secret("workday_password") == meant, written


def test_settings_written_by_hand(monkeypatch):
    monkeypatch.delenv("JOB_APPLY_HEADLESS")
    s = config.Settings.from_dict({"browser_channel": "edge", "headless": "false", "auto_submit_ats": "workday, greenhouse"})
    assert s.browser_channel == "msedge" and s.headless is False and s.auto_submit_ats == ["workday", "greenhouse"]
    s = config.Settings.from_dict({"browser_channel": None, "headless": "yes"})
    assert s.browser_channel == "chrome" and s.headless is True and not s.warnings
    s = config.Settings.from_dict({"browser_channel": "firefox"})
    assert s.browser_channel == "chrome" and "browser_channel" in s.warnings[0]


def test_saved_answers_fill_only_that_question_and_not_another_employers(job_apply_home):
    config.save_answer("Why do you want to work here?*", "Intel's fabs are where I learned the trade.", "Intel")
    config.save_answer("Are you currently employed?", "Yes", "Intel")
    config.save_answer("I agree", True, "Intel")
    config.save_answer("Do you have a valid driver's license?", "Yes", "Intel")
    prof = config.Profile.load()

    def answer(label, company, kind="text"):
        a = resolve_field({"id": "1", "kind": kind, "label": label, "value": ""}, prof, {"company": company})
        return a.value if a else None
    assert answer("Why do you want to work here?", "Intel") == "Intel's fabs are where I learned the trade."
    assert answer("Why do you want to work here?", "ASML") is None  # about Intel
    assert answer("Are you currently employed by ASML or any of its subsidiaries?", "ASML") is None
    assert answer("I agree to resolve any dispute by binding individual arbitration", "ASML", "checkbox") is None
    assert answer("Do you have a valid driver's license? *", "ASML") == "Yes"  # the same question anywhere
