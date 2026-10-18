"""The one-button apply pipeline, against a fake multi-step application site
(tests/fixtures/site: posting -> Apply -> Apply Manually -> sign-in -> two form steps ->
review -> submit)."""

import asyncio
import contextlib
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest
import yaml
from conftest import browser_available, fixture_url, run

import job_apply.browser as browser_module
import job_apply.pipeline as pipeline
from job_apply import config, formjs, mailbox
from job_apply.pipeline import Applier, Run, classify, pick_next, question_key

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")


def saved_password(monkeypatch):
    """A saved password, and the fake application site (local files) treated as its system's."""
    monkeypatch.setenv("JOB_APPLY_SECRET_TEST_SITE_PASSWORD", "not-a-real-password")
    monkeypatch.setattr(pipeline, "password_for", lambda url: "test_site_password")


async def until(condition, timeout=60, about=None):
    """Wait for condition(); on a timeout, say what about() shows (a run's state and log)."""
    start = time.monotonic()
    while not condition():
        if time.monotonic() - start > timeout:
            raise AssertionError("timed out waiting" + (f": {about()!r}" if about else ""))
        await asyncio.sleep(0.2)


def state(r):
    """A run's state and the end of its log, for a test that times out waiting on it, with
    where its tab is now (the job's own address doesn't say whether the tab moved)."""
    def tab() -> str | None:
        try:
            return r.page.url if r.page is not None and not r.page.is_closed() else None
        except Exception:
            return None
    return lambda: (r.status, r.need, r.reason, r.url, tab(), r.log[-8:])


def test_pick_next_and_classify():
    def acts(*texts, **kw):
        return [{"id": str(i), "text": t, **kw} for i, t in enumerate(texts)]
    assert pick_next(acts("Back to Job Posting", "Autofill with Resume", "Apply Manually"), False)["text"] == "Apply Manually"
    assert pick_next(acts("Apply with LinkedIn", "Apply"), False)["text"] == "Apply"
    assert pick_next(acts("Save", "Apply on company site"), False)["text"] == "Apply on company site"  # Indeed
    assert pick_next(acts("Save for Later", "Save and Continue", "Apply"), True)["text"] == "Save and Continue"
    assert pick_next(acts("Submit"), True) is None  # never the final button
    assert pick_next(acts("Search", "Sign In"), False) is None
    assert pick_next(acts("Log back in!", "Apply for this job online"), False)["text"] == "Apply for this job online"  # iCIMS
    assert pick_next(acts("Sign In", "Create Account", "Quick Apply", "Accept Cookies"), False)["text"] == "Quick Apply"  # Paycom
    assert classify({"title": "Just a moment...", "fields": [], "actions": []}, "") == "bot_check"
    assert classify({"title": "Jobs", "fields": [], "actions": acts("Sign in with email", "Sign in with Google")}, "") == "sign_in"
    code = [{"id": "1", "kind": "text", "label": "Enter the verification code we sent to your email"}]
    assert classify({"title": "Apply", "fields": code, "actions": []}, "") == "email_code"
    # a reCAPTCHA box on a real form is part of the form, not a page-wide check
    form = [{"id": "1", "kind": "text", "label": "First Name"}]
    assert classify({"title": "Apply", "fields": form, "actions": []}, "I'm not a robot") == "form"


def test_a_phone_box_that_speaks_of_codes_isnt_an_emailed_codes():
    """Edward Jones' BrassRing (run 85): its personal information step asks for a phone number "to receive your
    code(s)", in words that speak of "an Authentication Code sent to you", and the whole step was read as an emailed
    code's: nothing on it was filled, and the desk said the site had emailed a code. A box that asks for a phone
    number (or shows its ###-###-#### format) is never a code's; a box for a code sent to a phone still is."""
    phone = ("As you go through the Selection Process, you may be required to enter an Authentication Code sent to you "
             "by Example Corp in order to sign documents. Please enter the phone number you would like to use to "
             "receive your code(s) in the field below. Please enter the phone number in the following format: "
             "###-###-####. NOTE: Phone number MUST include dashes.")
    fields = [{"id": "1", "kind": "text", "label": "Legal First Name", "required": True},
              {"id": "2", "kind": "text", "label": "Primary Phone (ie. ###-###-####)", "required": True},
              {"id": "3", "kind": "text", "label": phone, "required": True}]
    assert not pipeline._code_box(fields[2])
    assert classify({"title": "Apply", "fields": fields, "actions": []}, "") == "form"
    for code in ("Enter the code sent to your phone number", "Enter the verification code we sent to your email",
                 "Passcode", "Enter the 6-digit code sent to your mobile phone", "Security code",
                 # (a code the number names, and a Re-enter beside one: the review's)
                 "Enter your phone number verification code", "Enter the mobile number OTP",
                 "Enter your mobile phone number's verification code",
                 "Verification code. Didn't get it? Re-enter your phone number"):
        assert pipeline._code_box({"id": "c", "kind": "text", "label": code}), code


@pytest.fixture(autouse=True)
def _no_mail_taken():
    """Each test starts with no code from the inbox put in yet (the desk keeps them for its whole process)."""
    pipeline._MAIL_TAKEN.clear()
    yield
    pipeline._MAIL_TAKEN.clear()


def test_what_a_page_flags_is_named():
    """Onto's Workday lists what's wrong as links ("Error-Email") in an "Errors Found" box and
    marks the field invalid; a stalled page says so in words."""
    page = {"errors": ["Please try again."],
            "actions": ["Errors Found", "Error-Email", {"text": "Error - How Did You Hear About Us?*"}, "Next"],
            "fields": [{"label": "Email*", "invalid": True}, {"label": "City*"}]}
    assert pipeline._flagged(page) == ["Please try again.", "“Email” needs fixing",
                                       "“How Did You Hear About Us” needs fixing", "“Email” is marked invalid"]
    assert pipeline._flagged({"errors": ["Field Service Engineer page is loaded"], "actions": ["Next"]}) == []
    # a click's summary: the actions' texts and how many fields there are (Onto's run crashed on it)
    assert pipeline._flagged({"fields": 16, "actions": ["Error-Email", "Next"], "errors": []}) == ["“Email” needs fixing"]
    # a question whose label runs on is named in a few words, and what's wrong with it still said (Maricopa County's
    # Workday, run 49: "The page didn't move on: “Salary offers are based on … was made.” is mar.")
    salary = ("Salary offers are based on the candidate's equivalent experience and internal equity with other Maricopa "
              "County employees within the same job classification. To ensure we are able to meet your needs, please "
              "specify the minimum wage/salary you would accept if an offer of employment was made.*")
    said = pipeline._flagged({"fields": [{"label": salary, "invalid": True}], "actions": ["Next"]})
    assert said == ["“Salary offers are based on the candidate's equivalent experience and internal…” is marked invalid"], \
        said


def test_what_a_create_account_form_wants_is_named():
    """A Create Account form that didn't go through: what the site said and marked on it, or else
    the boxes still empty that it needs (never a "keep me informed" one), or nothing at all."""
    left = {"errors": ["1 error found", "Your password must contain\n at least one symbol."],
            "actions": [{"text": "Error - Yes, I confirm that I have read the privacy notice."}, {"text": "Create Account"}]}
    assert pipeline._account_wants(left, {}) == ("it says “Your password must contain at least one symbol”; "
                                                 "“Yes, I confirm that I have read the privacy notice.” needs fixing.")
    filled = {"fields": [{"kind": "checkbox", "label": "I am at least 18 years of age*", "required": True, "value": False},
                         {"kind": "checkbox", "label": "Keep me informed, as the privacy notice describes", "value": False},
                         {"kind": "text", "label": "Phone*", "required": True, "value": ""},
                         {"kind": "password", "label": "Password*", "required": True, "value": ""}]}
    assert pipeline._account_wants({"errors": []}, filled) == ("“I am at least 18 years of age” isn't ticked; "
                                                               "“Phone” is still empty.")
    assert pipeline._account_wants({}, {"fields": []}) == ""
    # Edwards Vacuum's (SAP's Gigya screen set, run 61): a required choice of who sees the profile, the person's
    seen = ["Any recruiter within the Atlas Copco Group worldwide", "Any recruiter within the Atlas Copco Group in this "
            "country", "Only recruiters managing jobs I apply to"]
    left = {"errors": ["This field is required"], "fields": [
        {"kind": "radio_group", "label": "Make my profile visible to:", "required": True, "invalid": True, "value": "",
         "options": seen}]}
    assert pipeline._account_wants(left, {}) == (
        "it says “This field is required”; “Make my profile visible to” is marked invalid. Its choices for “Make my "
        "profile visible to”: “Any recruiter within the Atlas Copco Group worldwide”, “Any recruiter within the Atlas "
        "Copco Group in this country”, “Only recruiters managing jobs I apply to”; that choice is yours.")
    left["fields"][0]["options"] = ["Select One", *"ABCDEF"]
    assert pipeline._account_wants(left, {}).endswith("“A”, “B”, “C”, “D” and 2 more; that choice is yours.")


def test_a_posting_with_a_talent_community_box_goes_in_through_apply(srv, monkeypatch):
    """EMD Electronics' posting (a Phenom site, live, Oct 2026) has a box to join its Talent
    Community (an email and a consent tick, with a Submit of its own), a chatbot's box and two
    "Save Job" ticks beside the job. Five boxes made it look like the application: the desk filled
    the email, asked about the consent, and left "Apply Now" to the person as the button that sends
    it. Those boxes aren't the application's: the desk goes in through Apply Now and joins nothing."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/talent-community-posting.html"), title="Equipment Technician/Engineer",
                      company="Example Materials")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status in ("needs_you", "ready"))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.log[1] == "clicked \u201cApply Now\u201d", r.log
    assert not any("Talent Community" in (q.get("label") or "") for q in r.questions)
    assert not any(line.startswith("filled") and "Communication" in line for line in r.log)


def test_one_button_apply_walks_the_whole_flow(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="Field Service Engineer", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        try:
            return await walk()
        finally:
            await applier.stop()

    async def walk():
        applier.start()
        r = applier.enqueue(job["id"])
        await until(lambda: r.status == "needs_you")
        assert r.need == "sign_in" and r.blocking, r.reason
        assert r.log[1:3] == ["clicked “Apply”", "clicked “Apply Manually”"]  # not the job-alerts box
        assert "on Example Fab's site" in r.reason
        assert "subscribed" not in await r.page.evaluate("() => document.body.innerText")

        # the person signs in themselves; the desk notices and carries on
        await r.page.fill("#em", "sam.rivera@example.com")
        await r.page.fill("#pw", "not-a-real-password")
        await r.page.click("button:has-text('Sign In')")
        await until(lambda: r.status == "needs_you" and r.need == "questions")
        labels = {q["label"]: q for q in r.questions}
        license_q = next(q for q in labels if "driver" in q)
        terms_q = next(q for q in labels if "agree to the terms" in q)
        assert labels[license_q]["options"] == ["Select One", "Yes", "No"] or "Yes" in labels[license_q]["options"]
        assert not any("authorized" in q for q in labels)  # the profile answers that one
        assert not r.blocking  # questions wait in the desk; the queue isn't held

        # one answer remembered for later applications, one for this application only
        config.save_answer(license_q, "Yes", "Example Fab")
        r.once[question_key(terms_q)] = "Yes"
        applier.enqueue(job["id"], front=True)
        await until(lambda: r.status in ("ready", "failed") or r.status == "needs_you" and r.need != "questions")
        assert r.status == "ready", r.reason
        assert srv.tracker().get(job["id"])["status"] == "ready_to_submit"
        assert any(a.get("question", "").startswith("Do you have a valid driver") for a in config.saved_answers())
        assert not any("terms" in a.get("question", "") for a in config.saved_answers())

        applier.submit_now(job["id"])
        await until(lambda: r.status == "submitted")
        return r

    r = run(go())
    assert r.reason == "Submitted."
    assert srv.tracker().get(job["id"])["status"] == "applied"
    assert "Thank you for applying" in run(srv.page_text())


@pytest.mark.parametrize("at_once", [1, 2])
def test_a_bot_check_waits_in_its_tab_while_the_other_jobs_go_ahead(srv, monkeypatch, at_once):
    """A bot check never holds the queue (the owner's call, Oct 11): its job waits in its tab, watched, and the
    other jobs go ahead at once. When the person passes the check (the desk's Bot checks round takes them to
    it), the desk carries on with it by itself. The desk never solves one. So too with jobs filled at once."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    checked = srv.add_job(url=fixture_url("site/botcheck.html"), title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)
    applier.jobs_at_once = at_once

    async def go():
        applier.start()
        try:
            first = applier.enqueue(checked["id"])
            second = applier.enqueue(other["id"])
            await until(lambda: first.status == "needs_you")
            assert first.need == "bot_check" and not first.blocking and first.left
            await until(lambda: second.status not in ("queued", "running"))  # went ahead, no wait
            assert first.status == "needs_you" and first.need == "bot_check"
            assert "botcheck.html" in first.page.url  # (still on the check: the desk never solves one)
            await first.page.click("#human")  # the person passes it
            await until(lambda: first.need == "sign_in")  # carried on by itself, up to the sign-in
        finally:
            await applier.stop()

    run(go())


@pytest.mark.parametrize("at_once", [1, 2])
def test_in_the_desks_bot_check_round_no_other_job_starts_and_a_solved_one_goes_on_at_once(srv, monkeypatch, at_once):
    """The review of 0.3.119: while the person is at a bot check in the desk's round, starting another job would
    open its tab over the one they're solving; and a solved one waited for the job being worked on. In a round the
    queue holds, and the solved check is noticed straight away; once it's past, the other jobs go on (here once
    its sign-in, which holds the queue as ever, is left a moment). So too with jobs filled at once."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 1)
    checked = srv.add_job(url=fixture_url("site/botcheck.html"), title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)
    applier.jobs_at_once = at_once

    async def go():
        applier.start()
        try:
            first = applier.enqueue(checked["id"])
            await until(lambda: first.status == "needs_you" and first.need == "bot_check")
            applier.start_round(checked["id"])  # (the desk's "Solve them" brought its tab up)
            second = applier.enqueue(other["id"])
            await asyncio.sleep(1.5)
            assert second.status == "queued"  # nothing started over the tab the person is at
            await first.page.click("#human")
            await until(lambda: first.need == "sign_in", timeout=20, about=state(first))  # noticed at once
            await until(lambda: second.status not in ("queued", "running"), timeout=30, about=state(second))
            assert applier.round_job is None
        finally:
            await applier.stop()

    run(go())


def test_a_bot_check_is_past_only_once_the_page_stops_asking():
    """A form showing "Please verify you are not a robot" as an error still asks; a reCAPTCHA badge's "protected
    by reCAPTCHA" in a footer doesn't."""
    asks = ["Please verify you are not a robot.", "Verify you are human by completing the action below.",
            "The CAPTCHA is incorrect. Please try again.", "Complete the security check to continue"]
    assert all(pipeline._STILL_CHECKING.search(t) for t in asks)
    fine = ["This site is protected by reCAPTCHA and the Google Privacy Policy and Terms of Service apply.",
            "Application received. Thank you!"]
    assert not any(pipeline._STILL_CHECKING.search(t) for t in fine)


def test_a_job_left_waiting_is_watched_without_taking_over_the_tools(srv, monkeypatch):
    """The desk looks in on a job left waiting for the person while Claude may be using the
    tools: their tab stays Claude's, and what Claude opens there is never driven as that job."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 1)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("site/step1.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            first = applier.enqueue(job["id"])
            await until(lambda: first.status == "needs_you" and first.need == "sign_in")
            await until(lambda: first.left)  # nobody at the browser: left, and watched
            claude_tab = await srv.browser.new_tab()  # Claude, helping with another job
            await asyncio.sleep(1.5)  # the idle queue looks in on the left tab meanwhile
            mine = srv.browser.current_tab is claude_tab
            await srv.open_application(job_id=other["id"])
            await asyncio.sleep(2)
            return first, mine, claude_tab
        finally:
            await applier.stop()

    first, mine, claude_tab = run(go())
    assert mine  # the tools' tab is still Claude's
    assert claude_tab.url.endswith("step1.html") and first.page.url.endswith("signin.html")
    assert first.status == "needs_you" and first.need == "sign_in", (first.reason, first.log)


def test_a_left_tab_taken_to_another_posting_isnt_driven_as_its_job(srv, monkeypatch):
    """The person uses a left job's tab to look at another posting: that isn't this job's
    application moving on, so the desk leaves it be."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 1)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            first = applier.enqueue(job["id"])
            await until(lambda: first.status == "needs_you" and first.need == "sign_in")
            await until(lambda: first.left)
            first.paused_host = "careers.example.com"  # as if it paused on that employer's own host
            await first.page.goto(fixture_url("site/step1.html"))  # another posting's form
            await asyncio.sleep(2)
            return first
        finally:
            await applier.stop()

    first = run(go())
    assert first.status == "needs_you" and first.need == "sign_in" and first.left, (first.reason, first.log)


def test_someone_at_work_in_the_paused_tab_holds_the_queue(srv, monkeypatch):
    """Typing in the paused tab (signing in) is someone there: the queue keeps waiting."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 2)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            first = applier.enqueue(job["id"])
            second = applier.enqueue(other["id"])
            await until(lambda: first.status == "needs_you")
            assert first.need == "sign_in" and first.blocking, first.reason
            for ch in "sam.rivera@example.com"[:10]:  # about 4 seconds of typing
                await first.page.type("#em", ch)
                await asyncio.sleep(0.4)
            held = (first.blocking, second.status)
            await until(lambda: second.status not in ("queued", "running"))  # stopped: it goes ahead
            return held
        finally:
            await applier.stop()

    blocking, second_status = run(go())
    assert blocking and second_status == "queued"


def test_a_resume_on_another_job_goes_while_a_sign_in_holds_the_queue(srv, monkeypatch):
    """The review of Oct 15: a Resume the person pressed on another job waited behind a job holding the queue for its
    sign-in, though a Submit they press goes at once. A Resume on another job goes too: the one holding the queue is
    left to be watched between jobs (as its Later does), and carries on by itself once its tab is past the sign-in."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 600)
    turned_away = srv.add_job(url=fixture_url("site/forbidden.html"), title="Analyst", company="Example Health")["job"]
    signing_in = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            other = applier.enqueue(turned_away["id"])
            await until(lambda: other.status == "needs_you", about=state(other))
            holder = applier.enqueue(signing_in["id"])
            await until(lambda: holder.status == "needs_you", about=state(holder))
            assert holder.need == "sign_in" and holder.blocking, holder.reason
            # (someone at work in its tab a moment ago: the queue stays held, and the other job says why: the
            # review of 0.3.189, keys typed there mustn't land in a tab another job brings forward)
            applier.resume(other.job_id)
            await asyncio.sleep(1.5)
            waiting = (other.status, other.reason, holder.blocking)
            holder.active_at -= pipeline.RELEASE_IDLE + pipeline.SLOW_POLL_SECONDS + 5  # (nothing done in its tab for a while)
            said = len(other.log)
            applier.resume(other.job_id)
            await until(lambda: len(other.log) > said and other.status == "needs_you", timeout=20, about=state(other))
            return holder, waiting
        finally:
            await applier.stop()

    holder, waiting = run(go())
    assert waiting[0] == "queued" and waiting[1].startswith("Waiting: Example Fab holds the queue") and waiting[2], waiting
    assert holder.status == "needs_you" and holder.left and not holder.blocking, (holder.status, holder.left)


def test_a_captcha_challenge_after_a_click_waits_for_the_person(srv, monkeypatch):
    """Daifuku's iCIMS answers Next on its email step with hCaptcha's pictures and "Please try
    again.": the person solves it, then the desk carries on. A challenge frame kept hidden
    (Paycom's) is no check at all."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/captcha-step.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            first = (r.need, r.reason, list(r.log), r.blocking)
            await r.page.evaluate("() => window.solved()")
            await until(lambda: r.need == "sign_in")  # carried on by itself, up to the next step
            return first
        finally:
            await applier.stop()

    need, reason, log, blocking = run(go())
    assert need == "bot_check" and not blocking, (reason, log)
    assert "clicked “Next”" in log and "CAPTCHA" in reason


def test_saved_password_signs_in_without_the_person(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "questions", r.reason  # straight past the sign-in to the questions
    assert "pressed “Sign In” with your saved password" in r.log


def test_what_went_in_is_kept_for_the_jobs_card_and_never_its_password(srv, monkeypatch):
    """The whole-program review's worth-building list, item 3: with 30 jobs ready, the person read 30 review pages again
    to check them. Each job keeps what went into it, for the "What went in" panel on its desk card (a stopped job's
    too): the profile's answers and the person's own, by page and box, a box's last answer only. Never the saved
    password the desk signed in with, nor the sign-in's boxes (not the application's), and none of it in the log;
    public() stays plain JSON (a frozenset once broke the desk's whole state)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you", about=state(r))
            assert r.need == "questions", r.reason
            stopped = json.loads(json.dumps(r.public()))["went_in"]
            for q in r.questions:  # (the person's answers, for this application only)
                r.once[question_key(q["label"])] = "Yes"
            applier.enqueue(job["id"], front=True)
            await until(lambda: r.status in ("ready", "failed") or r.status == "needs_you" and r.need != "questions",
                        about=state(r))
            return r, stopped
        finally:
            await applier.stop()

    r, stopped = run(go())
    assert r.status == "ready", (r.status, r.reason, r.log)
    went = json.loads(json.dumps(r.public()))["went_in"]  # (plain json.dumps, as the page's state is written)
    said = [(a["page"], a["label"], a["value"]) for a in went["answers"]]
    assert said == [
        ("My Information", "First Name", "Sam"), ("My Information", "Last Name", "Rivera"),
        ("My Information", "Email", "sam.rivera@example.com"), ("My Information", "Phone Number", "480-555-0123"),
        ("My Information", "City", "Chandler"), ("My Information", "State", "Arizona"),
        ("Application Questions", "Are you legally authorized to work in the United States?", "Yes"),
        ("Application Questions", "Do you have a valid driver's license?", "Yes"),
        ("Application Questions", "I agree to the terms of this application", "Yes")], said
    # on the card it stopped on, what had gone in by then (the questions asked were the card's)
    assert [(a["page"], a["label"]) for a in stopped["answers"]] == [(p, label) for p, label, _ in said[:7]], stopped
    assert went["files"] == [] and went["agreed"] == [] and went["accounts"] == [] and went["left"] == []
    shown = json.dumps(r.public())
    assert "not-a-real-password" not in shown and "Sign In" not in {a["page"] for a in went["answers"]}
    assert not any("Arizona" in line or "Chandler" in line for line in r.log), r.log  # (nothing of it logged)


def test_what_went_in_names_the_files_cuts_a_long_answer_short_and_says_what_was_left_empty(srv, monkeypatch,
                                                                                         job_apply_home):
    """The panel names the files put up by their names (never the folder they're in), shows a long answer's start
    (a text area's), and lists the optional questions left empty: a cover letter the profile has none of, and a
    question it doesn't answer."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    story = "Maintained etch and deposition tools on a 300mm line. " * 12  # (over 600 characters)
    profile["answers"].append({"match": "equipment experience", "answer": story})
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    job = srv.add_job(url=fixture_url("site/what-went-in.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready", (r.status, r.reason, r.log)
    went = json.loads(json.dumps(r.public()))["went_in"]
    assert went["files"] == [{"label": "Resume", "name": "resume.pdf"}], went["files"]
    answers = {a["label"]: a["value"] for a in went["answers"]}
    assert answers["First Name"] == "Sam" and answers["Email"] == "sam.rivera@example.com", answers
    long = answers["Tell us about your equipment experience"]
    assert long == " ".join(story.split())[:pipeline.WENT_IN_CHARS - 1].rstrip() + "…", long
    assert [(q["label"], q["why"]) for q in went["left"]] == [
        ("Cover Letter", "your profile doesn't answer it"),
        ("What do you like to do outside of work?", "your profile doesn't answer it")], went["left"]
    assert str(job_apply_home) not in json.dumps(went)  # (the file's name alone, not where it is)


def test_what_went_in_keeps_no_password_code_or_security_answer_and_stays_small(monkeypatch):
    """Never on the panel, nor anywhere in the desk page's state: anything typed into a password box, an emailed code
    (a bare "Code" box too), a security question's answer (named by its box, or only by the form's heading, as
    BrassRing's "Security questions" over "Question 1 Answer"), "Create a secret", a challenge answer, or a value
    that is one of the desk's saved secrets or holds one that's no ordinary word. A password "Arizona" doesn't take
    every answer naming the state off it, and a ZIP code isn't a code's. A box's last answer stands in its first place;
    boxes that share a label keep their own (Workday's Month and Year are each a "Date"). Bounded: WENT_IN_ANSWERS
    answers a job, WENT_IN_CHARS of each, WENT_IN_LINES of a list, and nothing of it logged. A job queued or under way
    sends none (no card shows it there)."""
    monkeypatch.setenv("JOB_APPLY_SECRET_WORKDAY_PASSWORD", "Hunter-22-secret")
    monkeypatch.setenv("JOB_APPLY_SECRET_ICIMS_PASSWORD", "Arizona")
    r = Run(1, "Technician", "Example Fab", status="ready")
    page = {"url": "https://example.com/apply", "title": "Apply", "headings": ["My Information"], "fields": [
        {"id": "abc1", "kind": "text", "label": "First Name"},
        {"id": "abc2", "kind": "password", "label": "Create a password"},
        {"id": "abc3", "kind": "text", "label": "Enter the verification code we sent to your email"},
        {"id": "abc4", "kind": "text", "label": "Question 1 Answer", "section": "Security questions"},
        {"id": "abc5", "kind": "text", "label": "Code"},
        {"id": "abc6", "kind": "text", "label": "Referral note"},
        {"id": "abc7", "kind": "textarea", "label": "Cover note"},
        {"id": "abc8", "kind": "text", "label": "Date", "role": "spinbutton"},
        {"id": "abc9", "kind": "text", "label": "Date", "role": "spinbutton"},
        {"id": "abc10", "kind": "text", "label": "Middle Name"},
        {"id": "abc11", "kind": "text", "label": "Create a secret"},
        {"id": "abc12", "kind": "text", "label": "Challenge answer"},
        {"id": "abc13", "kind": "text", "label": "State"},
        {"id": "abc14", "kind": "text", "label": "Zip Code"},
        {"id": "abc15", "kind": "text", "label": "Where do you live?"}]}
    pipeline._went_in(r, [{"id": "abc1", "value": "Sam"}, {"id": "abc2", "value": "typed-in-a-password-box"},
                          {"id": "abc3", "value": "481516"}, {"id": "abc4", "value": "Flagstaff Ridge 1987"},
                          {"id": "abc5", "value": "662607"}, {"id": "abc6", "value": "see Hunter-22-secret"},
                          {"id": "abc7", "value": "word " * 200}, {"id": "abc8", "value": "10"},
                          {"id": "abc9", "value": "2026"}, {"id": "abc11", "value": "Rex the dog"},
                          {"id": "abc12", "value": "Blue Heron"}, {"id": "abc13", "value": "Arizona"},
                          {"id": "abc14", "value": "85225"}, {"id": "abc15", "value": "Chandler, Arizona"}], page)
    pipeline._went_in(r, [{"id": "abc1", "value": "Samuel"}], page)  # (a later round's answer to the same box)
    pipeline._went_left(r, {"needs_input": [{"id": "abc10", "kind": "text", "label": "Middle Name", "required": False},
                                            {"id": "abc2", "kind": "password", "label": "Password", "required": False}],
                            "failed": []}, page)
    # a form whose security questions only its heading names (BrassRing's), never the boxes themselves
    brassring = {"url": "https://example.com/register", "title": "Register", "headings": ["Security questions"],
                 "fields": [{"id": "def1", "kind": "text", "label": "Question 1"},
                            {"id": "def2", "kind": "text", "label": "Question 1 Answer"}]}
    pipeline._went_in(r, [{"id": "def1", "value": "Name of your first pet"}, {"id": "def2", "value": "Biscuit"}],
                      brassring)
    # a security clearance's questions (Northrop's, Boeing's) are no account's security questions: they stay on it
    clearance = {"url": "https://example.com/questions", "title": "Apply", "headings": ["Security Clearance Questions"],
                 "fields": [{"id": "jkl1", "kind": "radio_group", "label": "Answer: do you hold an active clearance?"},
                            {"id": "jkl2", "kind": "select", "label": "Clearance level",
                             "section": "Security Clearance Questions"}]}
    pipeline._went_in(r, [{"id": "jkl1", "value": "Yes"}, {"id": "jkl2", "value": "None"}], clearance)
    shown = json.dumps(r.public())  # (plain json.dumps: the page's state is written so)
    for secret in ("typed-in-a-password-box", "481516", "Flagstaff Ridge 1987", "662607", "Hunter-22-secret",
                   "Rex the dog", "Blue Heron", "first pet", "Biscuit"):
        assert secret not in shown, secret
    went = r.public()["went_in"]
    assert [(a["label"], a["value"]) for a in went["answers"]] == [
        ("First Name", "Samuel"), ("Cover note", ("word " * 60)[:pipeline.WENT_IN_CHARS - 1].rstrip() + "…"),
        ("Date", "10"), ("Date", "2026"), ("Zip Code", "85225"), ("Where do you live?", "Chandler, Arizona"),
        ("Answer: do you hold an active clearance?", "Yes"), ("Clearance level", "None")], \
        went["answers"]  # (a value that is the secret "Arizona" alone stays off)
    assert [q["label"] for q in went["left"]] == ["Middle Name"] and r.log == []
    for status in ("queued", "running"):
        r.status = status
        assert r.public()["went_in"] is None, status

    many = Run(2, "Technician", "Example Fab", status="ready")
    boxes = {"title": "Apply", "headings": ["Questions"],
             "fields": [{"id": f"ghi{i}", "kind": "text", "label": f"Question {i}"} for i in range(250)]}
    pipeline._went_in(many, [{"id": f"ghi{i}", "value": "x" * 1000} for i in range(250)], boxes)
    for line in [f"agreed to notice {i}" for i in range(100)]:
        many.went_in.agree(line)
    went = many.public()["went_in"]
    assert len(went["answers"]) == pipeline.WENT_IN_ANSWERS and went["full"] is True
    assert {len(a["value"]) for a in went["answers"]} == {pipeline.WENT_IN_CHARS}
    assert len(went["agreed"]) == pipeline.WENT_IN_LINES
    assert len(json.dumps(many.public())) < 60_000  # (30 jobs of it, at most, on the page's state)
    assert Run(3, status="ready").public()["went_in"] is None  # (nothing yet: no panel)


def test_a_box_keeps_its_place_on_the_panel_while_the_page_around_it_changes():
    """The review of the panel: answers were kept by the page's name, its last heading, which moves when the page
    shows an error ("There is a problem with your answers") or draws a block ("Education 2"). The same box filled
    before and after was listed twice (its old answer kept), an optional box left empty and then answered was listed as
    both, and a deleted block's answers stayed. A box keeps its first place, by its id; a page is named without its
    error headings or a heading its boxes are under (a one-page form's "Education"). Two boxes alike on one page load
    (steps drawn in place at one address) keep their own; the same box on the page loaded again is one."""
    from job_apply.pipeline import WentIn, _went_forget, _went_in, _went_left

    r = Run(1, "Technician", "Example Fab", status="ready")
    url = "https://example.com/apply"
    before = {"url": url, "title": "Apply", "headings": ["Apply for Technician"],
              "fields": [{"id": "abc1", "kind": "text", "label": "Phone"},
                         {"id": "abc2", "kind": "text", "label": "Middle Name"}]}
    wrong = {**before, "headings": ["Apply for Technician", "There is a problem with your answers"]}
    _went_in(r, [{"id": "abc1", "value": "480-555-0000"}], before)
    _went_left(r, {"needs_input": [{"id": "abc2", "kind": "text", "label": "Middle Name", "required": False}]}, before)
    _went_in(r, [{"id": "abc1", "value": "480-555-0123"}, {"id": "abc2", "value": "J"}], wrong)
    went = r.public()["went_in"]
    assert [(a["page"], a["label"], a["value"]) for a in went["answers"]] == [
        ("Apply for Technician", "Phone", "480-555-0123"), ("Apply for Technician", "Middle Name", "J")], went
    assert went["left"] == [], went["left"]

    # a block drawn since renames the page by its last heading: the block's boxes still go when it's deleted
    school = {"url": url, "title": "Apply", "headings": ["My Experience", "Education 2"],
              "fields": [{"id": "abc3", "kind": "text", "label": "School", "section": "Education 2"},
                         {"id": "abc4", "kind": "text", "label": "Degree", "section": "Education 2"}]}
    _went_in(r, [{"id": "abc3", "value": "Mesa Community College"}], school)
    _went_left(r, {"needs_input": [{"id": "abc4", "kind": "text", "label": "Degree", "section": "Education 2",
                                    "required": False}]}, school)
    _went_forget(r, {**school, "headings": ["My Experience", "Education 2", "Work Experience 1"]}, "Education 2")
    went = r.public()["went_in"]
    assert "Mesa Community College" not in json.dumps(went) and went["left"] == [], went

    # a one-page form's section heading isn't its page's name
    one = Run(2, status="ready")
    form = {"url": "https://example.com/one", "title": "Job Application for Technician at Example Fab",
            "headings": ["Technician", "Apply for this Job", "Education"],
            "fields": [{"id": "abc5", "kind": "text", "label": "First Name"},
                       {"id": "abc6", "kind": "text", "label": "School", "section": "Education"}]}
    _went_in(one, [{"id": "abc5", "value": "Sam"}, {"id": "abc6", "value": "ASU"}], form)
    assert [(a["page"], a["section"], a["label"]) for a in one.public()["went_in"]["answers"]] == [
        ("Apply for this Job", "", "First Name"), ("Apply for this Job", "Education", "School")]

    # other steps at one address (drawn in place, or loaded afresh: Taleo's flow.jsf): boxes of their own. The same
    # step's boxes drawn again with new ids (a Workday address block redrawn after its Country changed, the step gone
    # back to), or the page loaded again: the same boxes, a box left empty and answered since no longer "left empty"
    spa = Run(3, status="ready")
    explain = {"kind": "text", "label": "If yes, please explain"}
    middle = {"kind": "text", "label": "Middle Name"}
    step1 = {"url": url, "title": "Apply", "headings": ["Questions"],
             "fields": [{"id": "xyz1", **explain}, {"id": "xyz2", **middle}]}
    step2 = {**step1, "headings": ["Disclosures"], "fields": [{"id": "xyz9", **explain}]}
    fresh = {**step1, "headings": ["Self Identify"], "fields": [{"id": "qrs1", **explain}]}
    redrawn = {**step1, "fields": [{"id": "xyz20", **explain}, {"id": "xyz21", **middle}]}
    again = {**step1, "fields": [{"id": "tuv1", **explain}, {"id": "tuv2", **middle}]}
    _went_in(spa, [{"id": "xyz1", "value": "A cousin"}], step1)
    _went_left(spa, {"needs_input": [{"id": "xyz2", **middle, "required": False}]}, step1)
    _went_in(spa, [{"id": "xyz9", "value": "A patent"}], step2)
    _went_in(spa, [{"id": "qrs1", "value": "A board seat"}], fresh)
    _went_in(spa, [{"id": "xyz20", "value": "A cousin in Tempe"}, {"id": "xyz21", "value": "J"}], redrawn)
    _went_in(spa, [{"id": "tuv1", "value": "A cousin in Mesa"}], again)
    # (and an id a load elsewhere repeats, its three letters and its count, is another box)
    _went_in(spa, [{"id": "xyz1", "value": "480-555-0123"}],
             {"url": "https://example.com/contact", "title": "Apply", "headings": ["Contact"],
              "fields": [{"id": "xyz1", "kind": "text", "label": "Phone"}]})
    went = spa.public()["went_in"]
    assert [(a["page"], a["label"], a["value"]) for a in went["answers"]] == [
        ("Questions", "If yes, please explain", "A cousin in Mesa"), ("Disclosures", "If yes, please explain", "A patent"),
        ("Self Identify", "If yes, please explain", "A board seat"), ("Questions", "Middle Name", "J"),
        ("Contact", "Phone", "480-555-0123")], went["answers"]
    assert went["left"] == [], went["left"]

    # the page as filled: the choice a list made of the profile's words, never a secret's "(set)" or an emptied box's
    shown = Run(4, status="ready")
    page = {"url": url, "title": "Apply", "headings": ["My Information"],
            "fields": [{"id": "abc7", "kind": "select", "label": "State"}, {"id": "abc8", "kind": "text", "label": "City"},
                       {"id": "abc9", "kind": "text", "label": "Phone"}]}
    after = {**page, "fields": [{"id": "abc7", "kind": "select", "label": "State", "value": "Arizona"},
                                {"id": "abc8", "kind": "text", "label": "City", "value": ""},
                                {"id": "abc9", "kind": "text", "label": "Phone", "value": "(set)"}]}
    _went_in(shown, [{"id": "abc7", "value": "AZ"}, {"id": "abc8", "value": "Chandler"},
                     {"id": "abc9", "value": "480-555-0123"}], page, after, shown=after)
    assert [a["value"] for a in shown.public()["went_in"]["answers"]] == ["Arizona", "Chandler", "480-555-0123"]
    assert WentIn().public() is None


def test_keeping_what_went_in_never_stops_a_job(monkeypatch):
    """Whatever goes wrong keeping the panel's record (a secrets file it can't read, a page read it doesn't expect),
    the job goes on as it would have: nothing raised, nothing logged."""
    r = Run(1, "Technician", "Example Fab", status="ready")

    def broken() -> set[str]:
        raise OSError("unreadable")

    monkeypatch.setattr(pipeline, "_secret_values", broken)
    page = {"title": "Apply", "headings": ["My Information"], "fields": [{"id": "abc1", "kind": "text", "label": "City"}]}
    pipeline._went_in(r, [{"id": "abc1", "value": "Chandler"}], page)
    pipeline._went_left(r, {"needs_input": [{"id": "abc1", "kind": "text", "label": "City", "required": False}]}, page)
    pipeline._went_forget(r, {"fields": None}, "Education 2")
    r.went_in.agree(None)
    assert r.log == [] and r.public()["went_in"] is None


def test_the_secrets_file_is_read_again_only_once_it_changes(job_apply_home, monkeypatch):
    """The panel's check for saved secrets reads secrets.yaml once, and again only once the file has changed (it ran
    on every fill)."""
    path = job_apply_home / "secrets.yaml"
    path.write_text("workday_password: 'First-secret-1'\n", encoding="utf-8")
    assert "First-secret-1" in pipeline._secret_values()
    path.write_text("workday_password: 'A-longer-second-secret-2'\n", encoding="utf-8")
    assert pipeline._secret_values() >= {"A-longer-second-secret-2"} and "First-secret-1" not in pipeline._secret_values()
    read, real = [], config.read_secrets
    monkeypatch.setattr(config, "read_secrets", lambda p: read.append(p) or real(p))
    pipeline._secret_values()
    pipeline._secret_values()
    assert read == []  # (unchanged: not read again)


def test_a_job_opened_again_in_a_new_tab_starts_its_panel_over(srv):
    """The review of the panel: a job opened again in a new tab (its tab closed or crashed, Skip and Undo) starts its
    application over, and its panel listed the answers of the form that was gone. It starts over too; an account made
    there is still there, and stays listed."""
    job = srv.add_job(url=fixture_url("site/step1.html"), title="Technician", company="Example Fab")["job"]
    applier = Applier(srv)
    r = Run(job["id"], "Technician", "Example Fab", status="running")
    page = {"url": "https://example.com/apply", "title": "Apply", "headings": ["My Information"],
            "fields": [{"id": "abc1", "kind": "text", "label": "First Name"}]}
    pipeline._went_in(r, [{"id": "abc1", "value": "Sam"}], page)
    r.went_in.agree("agreed to Example Fab's notice “AI in hiring”")
    r.went_in.account("created your account on Example Fab's site with your saved password")

    async def go():
        try:
            return await applier._open(r)
        finally:
            await applier.stop()

    assert run(go()) and r.opened_fresh
    r.status = "ready"
    assert r.public()["went_in"] == {"answers": [], "files": [], "agreed": [], "left": [], "full": False,
                                     "accounts": ["created your account on Example Fab's site with your saved password"]}


def test_workday_style_dialog_is_followed(srv, monkeypatch):
    """Workday's Apply opens a dialog on the same page (a fifth heading), and announces
    "… page is loaded" to screen readers; neither may read as "the page didn't move on"."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/workday-posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in", (r.reason, r.log)
    assert r.log[1:3] == ["clicked “Apply”", "clicked “Apply Manually”"]
    assert r.page_info["url"].endswith("signin.html") and "Sign In" in r.page_info["actions"]


def test_sign_in_buttons_drawn_late_are_a_sign_in(srv, monkeypatch):
    """KLA's Workday draws "Sign in with email" a few seconds after its sign-in step loads:
    that's the person's sign-in, not a page without a way on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/late-signin.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in", (r.reason, r.log)


def test_a_sign_in_step_that_never_draws_its_buttons_is_a_sign_in(srv, monkeypatch):
    """Applied's Workday sometimes leaves its sign-in step loading: the person signs in."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "SIGN_IN_STEP_WAIT", 2)
    job = srv.add_job(url=fixture_url("site/signin-step-loading.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in" and r.blocking, (r.reason, r.log)


def test_a_sign_in_step_with_no_buttons_holds_still_until_the_person_moves_it(srv, monkeypatch):
    """That page reads as an ordinary page, so it looked "past the sign-in" on every look: the
    job was queued again, waited, paused again, and held the queue in a loop for good."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "SIGN_IN_STEP_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/signin-step-loading.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            seen = len(r.log)
            await asyncio.sleep(4)  # many looks
            assert r.status == "needs_you" and r.need == "sign_in" and len(r.log) == seen, r.log
            await r.page.goto(fixture_url("site/step1.html"))  # the person signs in: the application
            await until(lambda: r.need == "questions" or r.status == "ready", about=state(r))
            return r
        finally:
            await applier.stop()

    run(go())


def test_only_the_jobs_own_places_are_its_application(srv):
    """A paused tab on another employer's tenant of the same job system isn't this job: the
    desk would drive (and could submit) that application as this one."""
    from job_apply.pipeline import Run

    applier = Applier(srv)
    run_ = Run(1, "FSE", "Acme", status="needs_you", need="sign_in")
    run_.url = "https://acme.wd1.myworkdayjobs.com/External/job/Phoenix/FSE_R1/apply"
    run_.paused_host = "acme.wd1.myworkdayjobs.com"
    own = {"acme.wd1.myworkdayjobs.com"}
    assert applier._own_place(run_, "https://acme.wd1.myworkdayjobs.com/External/job/Phoenix/FSE_R1/apply/step2", own)
    assert not applier._own_place(run_, "https://other.wd5.myworkdayjobs.com/Careers/job/X_R9/apply", own)
    assert not applier._own_place(run_, "https://mail.google.com/mail/u/0/", own)
    # on from the employer's own site into its job system (TI's careers site into Oracle)
    run_.paused_host = "careers.ti.com"
    assert applier._own_place(run_, "https://edbz.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1",
                              {"careers.ti.com"})


def test_a_tab_that_cant_be_read_still_lets_the_queue_go_on(srv, monkeypatch):
    """A paused tab that keeps failing to be read held the queue for the whole 45 minutes.
    One look at the queue (a tick) is driven here, not the worker: a pause set on a running
    worker raced it (it went on to a 30 s sleep meanwhile, unwoken: CI, Oct 13)."""
    from job_apply.pipeline import Run

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0)
    applier = Applier(srv)

    async def broken(run):
        raise RuntimeError("the tab crashed")

    applier._moved_on = broken
    r = applier.runs[1] = Run(1, "FSE", "Example Fab", status="running")
    # a pause that holds the queue
    applier._pause(r, "sign_in", "Sign in there, then press Carry on.", code="account_theirs")
    assert r.blocking and not r.left
    r.active_at -= pipeline.HANDS_ON_IDLE + 1  # nothing happened in its tab since
    run(applier._tick())
    assert r.left and not r.blocking and "nothing happened in its tab" in r.reason


def test_a_pause_on_a_bot_check_or_captcha_is_marked_as_one(srv):
    """The desk marks a job stopped on a bot check or CAPTCHA (the owner, Oct 12), a sign-in's or Create
    Account's own picture check among them (Edwards', live), and no other stop."""
    from job_apply.pipeline import Run

    applier = Applier(srv)
    marks = {}
    for need, seen in [("bot_check", None), ("captcha", None), ("sign_in", {"captcha": True}),
                       ("sign_in", {"challenge": True}), ("sign_in", {}), ("questions", None)]:
        r = Run(1, "FSE", "Example Fab", status="running")
        applier._pause(r, need, "Waiting.", seen=seen, code="questions")
        marks[(need, str(seen))] = r.public()["captcha"]
    assert marks == {("bot_check", "None"): True, ("captcha", "None"): True, ("sign_in", "{'captcha': True}"): True,
                     ("sign_in", "{'challenge': True}"): True, ("sign_in", "{}"): False, ("questions", "None"): False}
    # Submit pressed on a CAPTCHA's stop whose tab was closed meanwhile: a stop for Resume, no CAPTCHA's any more
    job = srv.add_job(url="https://example.com/job/1", title="FSE", company="Example Fab")["job"]
    r = applier.runs[job["id"]] = Run(job["id"], "FSE", "Example Fab", status="running")
    applier._pause(r, "captcha", "Filled. The form has a CAPTCHA.", code="captcha")
    run(applier._submit(r))
    assert (r.status, r.need, r.captcha) == ("needs_you", "stuck", False), (r.status, r.need, r.reason)


def test_a_tab_still_for_a_minute_is_looked_at_less_often(srv):
    """A tab waiting on the person is read whole (every frame) each look: every few seconds while it changes or has just
    paused, every SLOW_POLL_SECONDS once it's been still a minute (the review of Oct 15). A change brings it back."""
    applier = Applier(srv)
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    async def not_moved(run):
        return False
    applier._sleep = sleep
    applier._moved_on = not_moved
    job = srv.add_job(url="https://example.com/job/1", title="FSE", company="Example Fab")["job"]
    r = applier.runs[job["id"]] = Run(job["id"], "FSE", "Example Fab", status="needs_you")
    r.need, r.blocking = "sign_in", True  # holding the queue
    for still in (10, 120, 30):
        r.paused_at = time.time() - 120
        r.active_at = time.time() - still
        run(applier._tick())
    assert slept == [pipeline.POLL_SECONDS, pipeline.SLOW_POLL_SECONDS, pipeline.POLL_SECONDS]
    slept.clear()
    r.blocking, r.left = False, True  # the queue went on without it: watched still
    for still in (10, 120):
        r.paused_at = r.active_at = time.time() - still
        run(applier._tick())
    assert slept == [pipeline.POLL_SECONDS, pipeline.SLOW_POLL_SECONDS]


def test_work_in_a_tab_the_queue_went_on_without_is_noticed_too(srv):
    """A tab left waiting while the other jobs went ahead is watched for the person at work in it as the one holding
    the queue is: a change in its boxes brings it back to the quick look (the review of 0.3.190)."""
    fields = [{"id": "1", "label": "Email", "kind": "email", "value": ""}]

    class Browser:
        async def peek(self, tab):
            return {"url": "https://x.example/signin", "fields": [dict(f) for f in fields]}, ""

    class Stub:  # (the server, its browser looked through)
        browser = Browser()

        def __getattr__(self, name):
            return getattr(srv, name)

    applier = Applier(srv)
    applier.srv = Stub()
    r = Run(1, "FSE", "Example Fab", status="needs_you")
    r.page, r.left = object(), True
    r.paused_at = r.active_at = time.time() - 120
    run(applier._past_pause(r))
    assert time.time() - r.active_at > 100  # (its first look: nothing to compare with)
    fields[0]["value"] = "s"  # the person types
    run(applier._past_pause(r))
    assert time.time() - r.active_at < 5 and applier._poll_for([r]) == pipeline.POLL_SECONDS


def test_a_wait_for_the_page_to_change_doesnt_read_its_words(srv):
    """The desk's waits look every half second, at the form: the page's words, a read of their own, aren't wanted
    there and aren't read (the review of Oct 15)."""
    reads = {"form": 0, "text": 0}
    one = {"url": "https://x.example/apply", "headings": ["One"], "fields": []}
    two = {"url": "https://x.example/apply", "headings": ["Two"], "fields": []}

    class Browser:
        async def challenge_showing(self):
            return False

    class Stub:
        browser = Browser()

        async def inspect_form(self, include_dropdown_options=True):
            reads["form"] += 1
            return dict(one if reads["form"] < 3 else two)

        async def page_text(self, max_chars=8000):
            reads["text"] += 1
            return "One"

    applier = Applier(srv)
    applier.srv = Stub()
    assert run(applier._wait_for_change(one, 5))
    assert reads == {"form": 3, "text": 0}
    assert run(applier._look()) == ({**two, "challenge": False}, "One")  # (a look that wants the words reads them)


def test_a_this_application_answer_stays_on_its_page(srv):
    """"If yes, please explain" answered about relatives on one page went into a later page's
    "If yes, please explain" about something else."""
    from job_apply.pipeline import Run

    filled = []

    class Stub:
        async def fill_form(self, values):
            filled.extend(values)
            return {"ok": True, "results": [{"id": v["id"], "ok": True} for v in values]}

    applier = Applier(srv)
    applier.srv = Stub()

    async def look(**kw):
        return page_two, ""

    applier._look = look
    monkey = pipeline.ONCE_SETTLE
    pipeline.ONCE_SETTLE = 0
    try:
        r = Run(1, "FSE", "Acme")
        r.once[question_key("If yes, please explain")] = "My brother works in Fab 3"
        page_one = {"url": "https://x.example/apply", "headings": ["Relatives"],
                    "fields": [{"id": "5", "label": "If yes, please explain", "kind": "text", "value": ""}]}
        page_two = {"url": "https://x.example/apply", "headings": ["Background"],
                    "fields": [{"id": "9", "label": "If yes, please explain", "kind": "text", "value": ""}]}
        run(applier._fill_once(r, page_one))
        assert [v["id"] for v in filled] == ["5"]
        filled.clear()
        run(applier._fill_once(r, page_two))
        assert filled == []  # a later page's box of the same name gets nothing
    finally:
        pipeline.ONCE_SETTLE = monkey


def test_a_job_skipped_while_submit_for_me_checks_it_isnt_submitted(srv):
    from job_apply.pipeline import Run

    applier = Applier(srv)
    r = Run(1, "FSE", "Acme", status="ready")
    calls = []

    class Stub:
        class browser:
            current_job_id = None

            @staticmethod
            def use_tab(page):
                return True

        async def inspect_form(self, include_dropdown_options=False):
            r.status = "skipped"  # Skip pressed meanwhile
            return {"fields": []}

        async def submit_application(self, **kw):
            calls.append(kw)
            return {"submitted": True, "confirmed": True}

        @staticmethod
        def tracker():
            return srv.tracker()

    applier.srv = Stub()
    run(applier._submit(r, by_person=False))
    assert calls == [] and r.status == "skipped"


def test_a_submit_beside_a_next_button_isnt_the_review_page(srv, monkeypatch):
    """Step 1 of 2 with Next, and a job-alerts Submit in the footer: taken for the review page,
    "Submit for me" would have pressed a Submit before the application was filled."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/next-and-submit.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status in ("ready", "needs_you", "failed"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and r.url.endswith("review.html"), state(r)()
    assert "clicked \u201cNext\u201d" in r.log


@pytest.mark.parametrize("query", ["?ms=8000", "?never"])
def test_a_page_that_stays_blank_a_while_is_waited_for(srv, monkeypatch, query):
    """Infineon's application page (2 MB, a reCAPTCHA) is now and then blank for longer than
    a page's buttons take to be drawn."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "BLANK_PAGE_WAIT", 15)
    job = srv.add_job(url=fixture_url("site/blank-then-form.html") + query, title="Tech", company="Example Semi")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status in ("ready", "needs_you"), timeout=30)
            return r
        finally:
            await applier.stop()

    r = run(go())
    if query == "?never":
        assert r.need == "stuck" and "stayed blank" in r.reason, (r.reason, r.log)
    else:
        assert r.status == "ready", (r.reason, r.log)


def test_a_list_of_jobs_isnt_paged_through(srv, monkeypatch):
    """A link to a search page (Analog Devices' board) has "next" for its next page of jobs:
    pressing it again and again never opens an application."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/job-list.html"), title="Careers", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "stuck" and "three times" in r.reason, (r.reason, r.log)
    assert sum(entry == "clicked “next”" for entry in r.log) == 2


def test_a_note_over_the_posting_is_dismissed(srv, monkeypatch):
    """Nikon's UKG board lays an accessibility note over the posting and hides the posting's
    buttons until it's dismissed."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/note-posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in", (r.reason, r.log)
    assert r.log[1:3] == ["dismissed a note (“Dismiss Note”)", "clicked “Apply now”"]


def test_a_button_drawn_as_a_web_component_is_pressed(srv, monkeypatch):
    """UKG's Apply now is a <ukg-button> whose real button is in its shadow root."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/webcomponent-posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in" and "clicked “Apply now”" in r.log, (r.reason, r.log)


def test_an_apply_menu_is_followed_through_to_the_application(srv, monkeypatch):
    """Qorvo's Apply now opens a menu (Apply Now, Start apply with LinkedIn); its Apply Now
    asks for an email and a Start before the application. The toggle itself is pressed
    once, not over and over, and the LinkedIn route is left alone."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/apply-menu-posting.html"), title="Intern", company="Example Semi")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in", (r.reason, r.log)
    clicks = [line for line in r.log if line.startswith("clicked")]
    assert clicks == ["clicked “Apply now”", "clicked “Apply Now”", "clicked “Start”"], r.log
    assert r.url.endswith("/signin.html")


@pytest.mark.parametrize("page, button", [("cookie-form.html", "Reject"), ("cookie-words.html", "Decline"),
                                          ("cookie-unnamed-c1-privacy-dialog.html", "Reject All"),
                                          ("cookie-unnamed-c2-long-fixed.html", "Decline"),
                                          ("cookie-unnamed-c3-heading-first.html", "Reject"),
                                          ("cookie-unnamed-c5-ketch-like.html", "Reject All")],
                         ids=["dialog", "words-only", "unnamed-dialog", "long-bar", "heading-first", "ketch-like"])
def test_cookie_dialog_is_declined_never_accepted(srv, monkeypatch, page, button):
    """Infineon's cookie dialog covers the whole form; the desk presses Reject, never Accept. A banner known by its
    words alone (0.3.176: a fixed bar named nothing) is declined the same way, and so are the ones the form reader
    doesn't know by their names (a privacy dialog about cookies, a long bar, cookies said after a heading: the review
    of 0.3.188 found 0.3.188's first guard left them over the form)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url(f"site/{page}"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"))
            return r, await r.page.evaluate("() => window.accepted || 0")
        finally:
            await applier.stop()

    r, accepted = run(go())
    assert r.status == "ready", (r.reason, r.log)
    assert f"declined cookies (“{button}”)" in r.log and accepted == 0, r.log


def test_a_bare_decline_on_a_dialog_that_isnt_a_cookie_banner_is_never_pressed(srv, monkeypatch):
    """The review of Oct 15: on a page whose text speaks of cookies anywhere (a footer's "This site uses cookies"), any
    bare "Decline" or "Reject" was pressed as a cookie banner's, though here it declines the application's data-privacy
    statement (SuccessFactors draws one over its form), and the application went nowhere. A bare decline is pressed
    only inside a cookie banner; one that names cookies ("Decline cookies", "Necessary only") as before."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/privacy-dialog-decline.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => window.declined || 0")
        finally:
            await applier.stop()

    r, declined = run(go())
    assert declined == 0 and not any(line.startswith("declined cookies") for line in r.log), (r.reason, r.log)


def test_a_cookie_banner_with_no_way_to_decline_is_the_persons_unless_they_allow_it(srv, monkeypatch, job_apply_home):
    """Texas Instruments' consent prompt (live, Oct 2026) offers only "Manage Preferences" and
    "Agree and Proceed", over the whole application: the desk sat behind it. It says it's the
    person's to answer, and accepts it only where they set settings.accept_cookies."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)

    def apply_once():
        job = srv.add_job(url=fixture_url("site/cookie-agree-only.html"), title="FSE", company="Example Fab")["job"]
        applier = Applier(srv)

        async def go():
            applier.start()
            try:
                r = applier.enqueue(job["id"])
                await until(lambda: r.status not in ("queued", "running"), about=state(r))
                return r, await r.page.evaluate("() => [window.accepted || 0, window.prefs || 0]")
            finally:
                await applier.stop()

        return run(go())

    r, (accepted, prefs) = apply_once()
    assert (r.status, r.need) == ("needs_you", "stuck") and "can't decline" in r.reason, (r.reason, r.log)
    assert "accept_cookies: true" in r.reason and (accepted, prefs) == (0, 0)
    profile = job_apply_home / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8").replace("settings:\n", "settings:\n  accept_cookies: true\n"), encoding="utf-8")
    r, (accepted, prefs) = apply_once()
    assert accepted == 1 and prefs == 0, (r.reason, r.log)
    assert any(line.startswith("accepted cookies (\u201cAGREE AND PROCEED\u201d)") for line in r.log), r.log
    assert r.status == "ready", (r.status, r.reason, r.log)


def test_a_banner_that_can_be_declined_is_declined_even_where_accepting_is_allowed(srv, monkeypatch, job_apply_home):
    """settings.accept_cookies lets the desk accept only a banner with no way to decline: one
    whose way is "Deny" is denied."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    profile = job_apply_home / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8").replace("settings:\n", "settings:\n  accept_cookies: true\n"), encoding="utf-8")
    job = srv.add_job(url=fixture_url("site/cookie-deny.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => [window.accepted || 0, window.denied || 0]")
        finally:
            await applier.stop()

    r, (accepted, denied) = run(go())
    assert (accepted, denied) == (0, 1) and "declined cookies (\u201cDeny\u201d)" in r.log, (r.reason, r.log)
    assert r.status == "ready", (r.status, r.reason, r.log)


def test_a_cookie_notice_along_the_edge_is_left_be(srv, monkeypatch):
    """A notice along the bottom with only "Got it" doesn't cover the form: the desk leaves it
    there and goes on, neither accepting it nor stopping for it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/cookie-bar.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => window.accepted || 0")
        finally:
            await applier.stop()

    r, accepted = run(go())
    assert r.status == "ready" and accepted == 0, (r.status, r.reason, r.log)


def test_a_cookie_banner_after_a_long_posting_is_still_declined(srv, monkeypatch):
    """Qorvo's cookie banner comes after a long posting, past the page text the desk reads.
    Its buttons sit in the banner's own box, so it's still turned down, and never accepted."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/long-posting-cookies.html"), title="ET", company="Example Semi")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in", (r.reason, r.log)
    assert r.log[1:3] == ["declined cookies (“Reject All”)", "clicked “Apply now”"], r.log


NOTICE = "Notice Related to Example Corp's Use of the Eightfold AI Recruiting Software"


def notice_settings(job_apply_home, **settings):
    import yaml

    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile["settings"].update(settings)
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")


async def notice_page(r):
    """What the Eightfold-like form shows: its uploads and answers to the notice, and its boxes."""
    return await r.page.evaluate("() => ({uploads: window.uploads, agreed: window.agreed, "
                                 "first: document.getElementById('first').value, "
                                 "country: document.getElementById('input-13').value, "
                                 "terms: document.getElementById('input-65').value})")


@pytest.mark.parametrize("settings", [{"accept_notices": False}, {"submit_mode": "dry_run"}], ids=["off", "practice"])
def test_a_notice_over_the_form_is_the_persons_and_the_form_is_filled_after_it(srv, monkeypatch, job_apply_home, settings):
    """Eightfold's notice about its AI screening was open over the one-page form (live, Oct 2026).
    The desk left it alone but never said so, and went on filling the boxes behind it: Country of
    Residence timed out and came back as a question the profile doesn't answer. Without
    settings.accept_notices (and always in practice mode) the notice is the person's: the desk
    stops, naming it. Once they've answered it, the form is filled behind it, the resume the page
    already holds isn't sent again (each upload brings the notice back), and the attestation is
    asked."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    notice_settings(job_apply_home, **settings)
    job = srv.add_job(url=fixture_url("site/ai-notice-form.html") + "?notice=1", title="Equipment Technician",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            first = (r.status, r.need, r.reason, await notice_page(r))
            await r.page.click("#cancelUploadResume")  # the person answers it
            stopped = r.paused_at
            applier.enqueue(job["id"])  # and presses Resume
            await until(lambda: r.paused_at > stopped and r.status not in ("queued", "running"), about=state(r))
            return r, first, await notice_page(r)
        finally:
            await applier.stop()

    r, (status, need, reason, before), after = run(go())
    assert (status, need) == ("needs_you", "stuck") and f"“{NOTICE}”" in reason, reason
    assert "press its button in the desk's browser window, then press Carry on" in reason and "accept_notices: true" in reason
    assert before == {"uploads": 0, "agreed": 0, "first": "", "country": "", "terms": ""}  # nothing done behind it
    assert (r.status, r.need) == ("needs_you", "questions"), (r.status, r.reason, r.log)
    assert [q["label"][:20] for q in r.questions] == ["Terms and Conditions"], r.questions
    assert after == {"uploads": 0, "agreed": 0, "first": "Sam", "country": "United States", "terms": ""}


def test_with_accept_notices_the_desk_agrees_to_an_ai_notice_and_an_attestation(srv, monkeypatch):
    """The owner's choice: settings.accept_notices (on unless turned off) agrees for the person to
    an employer's notice about AI screening (Eightfold's, which opens as the resume goes up), and
    picks an application's attestation that its information is true (its Terms and Conditions
    dropdown, with one choice), saying each in the log. The resume goes up once, the boxes behind
    the notice are filled once it's answered, and the form is ready to submit."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/ai-notice-form.html"), title="Equipment Technician",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await notice_page(r)
        finally:
            await applier.stop()

    r, shown = run(go())
    assert r.status == "ready", (r.status, r.reason, r.log)
    assert shown == {"uploads": 1, "agreed": 1, "first": "Sam", "country": "United States", "terms": "I Agree"}
    assert f"agreed to Example Corp's notice “{NOTICE}” for you (settings.accept_notices)" in r.log, r.log
    assert any(line.startswith("picked “I Agree” for “Terms and Conditions")
               and line.endswith("for you (settings.accept_notices)") for line in r.log), r.log
    # and on the job's "What went in" panel: each agreed to, the resume by its name, the profile's answers
    went = json.loads(json.dumps(r.public()))["went_in"]
    assert went["agreed"][0] == f"agreed to Example Corp's notice “{NOTICE}”", went["agreed"]
    assert len(went["agreed"]) == 2 and went["agreed"][1].startswith("picked “I Agree” for “Terms and Conditions"), \
        went["agreed"]
    assert went["files"] == [{"label": "Resume", "name": "resume.pdf"}], went["files"]
    assert {(a["label"], a["value"]) for a in went["answers"]} >= {("First name", "Sam"),
                                                                   ("Country of Residence", "United States")}, went
    # and each kept in the words its rules read (Run.agreed, for the live check's audit: T2), never shown
    assert [(a["what"], a["heading"], a["button"]) for a in r.agreed] == [
        ("notice", NOTICE, "I Agree"), ("attestation", r.agreed[1]["heading"], "I Agree")]
    assert "artificial intelligence" in r.agreed[0]["words"] and r.agreed[1]["heading"].startswith("Terms and Conditions")
    assert "I certify that the information" in r.agreed[1]["words"] and r.agreed[1]["choices"] == ["I Agree"]
    assert r.agreed[0]["choices"] == [] and r.agreed[0]["windows"] == []
    assert all(len(a["sha"]) == 40 and len(a["words"]) <= pipeline.AGREED_WORDS for a in r.agreed)
    assert "agreed" not in r.public() and "artificial intelligence" not in json.dumps(r.public())


def test_an_account_forms_boxes_are_kept_each_in_its_own_words_and_a_long_texts_waiver_in_a_window():
    """Run.agreed (T2, the review of c4edd30): an account box's label, the choice given and the words above it are
    kept apart, as the desk read each (never one run into the next); past the first AGREED_WORDS characters, the
    words either side of a waiver's are kept, so the audit sees a clause deep in a long page."""
    from job_apply.pipeline import Run

    r = Run(job_id=1)
    Applier._keep_ticked(r, [
        {"id": "a", "kind": "checkbox", "label": "I agree", "words_above": "By creating an account you accept Acme's "
                                                                          "Terms of Use."},
        {"id": "b", "kind": "radio_group", "label": "Contact Consent",
         "options": ["I understand that I may be considered for other open positions"]}], "Create Account")
    assert [(a["what"], a["heading"], a["button"], a["choices"], a["words"]) for a in r.agreed] == [
        ("account box", "I agree", "Create Account", [], "By creating an account you accept Acme's Terms of Use."),
        ("account box", "Contact Consent", "Create Account",
         ["I understand that I may be considered for other open positions"], "")]
    page = "Terms of Use. " * 400 + "Any dispute goes to binding arbitration under these terms. " + "More. " * 200
    r.keep_agreed("terms gate", "Welcome", "I Accept", page)
    kept = r.agreed[-1]
    assert len(kept["words"]) == pipeline.AGREED_WORDS and "arbitration" not in kept["words"]
    assert len(kept["windows"]) == 1 and "binding arbitration under these terms" in kept["windows"][0]
    assert len(kept["windows"][0]) <= 2 * pipeline.AGREED_WINDOW + len("arbitrat")


@pytest.mark.parametrize("notice, settings", [("privacy", {}), ("other", {}), ("privacy", {"accept_notices": False}),
                                              ("waiver", {}), ("signup", {})],
                         ids=["privacy", "other", "privacy-off", "waiver", "signup"])
def test_a_privacy_dialog_over_the_form_is_agreed_and_another_is_the_persons(srv, monkeypatch, job_apply_home, notice,
                                                                              settings):
    """settings.accept_notices agrees to the employer's privacy notice too, the owner's call (Oct
    10): a privacy policy over the form, with Cancel and Ok, is taken in with its Ok. Any other
    dialog over the form (about neither AI screening nor privacy), or any with the setting off,
    stops the desk for the person. So does the AI notice itself when its words also bind the person to
    arbitration (waiver) or join them to a talent community with job alerts (signup): the review of Oct 15
    found the AI notice's path without the guards the privacy notice's has."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    notice_settings(job_apply_home, **settings)
    job = srv.add_job(url=fixture_url("site/ai-notice-form.html") + f"?notice={notice}", title="Equipment Technician",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await notice_page(r)
        finally:
            await applier.stop()

    r, shown = run(go())
    if notice == "privacy" and not settings:
        assert "agreed to Example Corp's notice “Privacy Policy of Example Corp” for you (settings.accept_notices)" \
            in r.log, r.log
        assert shown["agreed"] == 1 and shown["first"] == "Sam", shown
        return
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert shown["agreed"] == 0 and shown["first"] == "", shown
    if notice == "other":
        assert "“Before you continue”" in r.reason and "accept_notices" not in r.reason, r.reason
    elif notice in ("waiver", "signup"):  # (not the setting's to give: no hint that it would)
        assert "Use of the Eightfold AI" in r.reason and "accept_notices" not in r.reason, r.reason
    else:
        assert "“Privacy Policy of Example Corp”" in r.reason and "accept_notices: true" in r.reason, r.reason


PROFILE_START = "site/profile-start.html"
PROFILE_DIALOG = "“Profile Bootstrapping” dialog"


def profile_run(srv, query="", tailored=None):
    """A job on the Eightfold-like page whose dialog offers how to start the profile (`tailored`: the name
    of a resume made for this job, in its folder); the run, and what the page recorded: how the profile was
    started, the file put in and how many times, the dialog's buttons pressed, and its form's boxes."""
    job = srv.add_job(url=fixture_url(PROFILE_START) + query, title="Equipment Technician",
                      company="Example Corp")["job"]
    if tailored:
        folder = Path(srv.tracker().get(job["id"])["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / tailored).write_bytes(b"%PDF-1.4\n")
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate(
                "() => ({started: sessionStorage.getItem('started'), file: sessionStorage.getItem('file'), "
                "uploads: Number(sessionStorage.getItem('uploads') || 0), pressed: sessionStorage.getItem('pressed') || '', "
                "first: document.getElementById('first').value, email: document.getElementById('email').value})")
        finally:
            await applier.stop()

    return run(go())


@pytest.mark.parametrize("have", ["resume", "none"])
def test_a_dialog_offering_to_start_the_profile_is_answered_by_entering_the_information(srv, monkeypatch,
                                                                                       job_apply_home, have):
    """Eightfold's "Profile Bootstrapping" dialog ("Create your profile with existing information",
    with "Upload your resume" and "Enter information") opened over Northrop Grumman's application
    (live check, Oct 2026), and the desk stopped for the person as for a notice only they can answer.
    It's no agreement but a choice of how to start the profile: "Enter information" is pressed and the
    form filled from the profile, with a resume in the profile or not. Never the resume upload: the
    site's reading of it would fill the boxes with its guesses. Nothing else is pressed. Said in the log."""
    import yaml

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    if have == "none":
        path = job_apply_home / "profile.yaml"
        prof = yaml.safe_load(path.read_text(encoding="utf-8"))
        prof["documents"] = {"resume": None}
        path.write_text(yaml.safe_dump(prof), encoding="utf-8")
    r, shown = profile_run(srv)
    assert r.status == "ready", (r.status, r.reason, r.log)
    assert shown["first"] == "Sam" and shown["email"] == "sam.rivera@example.com", shown  # the form, filled as usual
    assert (shown["started"], shown["file"], shown["uploads"], shown["pressed"]) == ("enter", None, 0, "enter"), shown
    assert (f"pressed “Enter information” in Example Corp's {PROFILE_DIALOG}, to fill the application from your "
            "profile (your resume goes in where it asks for one)") in r.log, r.log


def test_a_profile_dialog_that_stays_open_is_the_persons(srv, monkeypatch):
    """The dialog stayed open after "Enter information" (the site didn't take it): the desk says what it
    did and leaves it to the person, without pressing it again."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    r, shown = profile_run(srv, "?stays")
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert r.reason.startswith(f"I pressed “Enter information” in the {PROFILE_DIALOG} on Example Corp's site, and it "
                               "stayed open. Look at it in the desk's browser window, then press Carry on."), r.reason
    assert (shown["started"], shown["uploads"], shown["pressed"], shown["first"]) == ("enter", 0, "enter", ""), shown


def test_a_profile_dialog_is_known_by_its_offer_not_its_name():
    """What makes a dialog the one that offers how to start a profile: a button to upload the resume and
    one to enter the information by hand, whatever the site calls it. Not one with only one of them,
    nor any other dialog's buttons."""
    def dialog(*texts, **kw):
        return {"heading": "Anything", "buttons": [{"id": str(i), "text": t, **kw} for i, t in enumerate(texts)]}

    start = pipeline._profile_start
    assert start(dialog("Upload your resume", "Enter information"))["text"] == "Enter information"
    assert start(dialog("Upload Resume/CV", "Enter manually"))["text"] == "Enter manually"
    assert start(dialog("Attach your CV", "Fill in my details"))["text"] == "Fill in my details"
    assert start(dialog("Upload your resume")) is None
    assert start(dialog("Enter information")) is None
    assert start(dialog("Upload your resume", "Cancel")) is None
    assert start(dialog("Cancel", "I Agree")) is None
    assert start(dialog("Upload your resume", "Enter information", disabled=True)) is None


@pytest.mark.parametrize("settings, query", [({}, ""), ({"accept_notices": False}, ""), ({}, "?waiver=1")],
                         ids=["on", "off", "waiver"])
def test_a_privacy_agreement_before_the_application_is_accepted_for_the_person(srv, monkeypatch, job_apply_home,
                                                                                settings, query):
    """Kforce's Taleo career section (live, Oct 2026) puts a privacy agreement before the
    application, its only ways on "I Accept" and "I Decline". With settings.accept_notices (the
    owner's call, Oct 10) the desk accepts it for the person, says so, and goes on to fill the
    application; with it off the agreement is theirs to press. So is one whose words also bind them
    to arbitration (0.3.176: the page's words are read whole for a waiver, the review's)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    notice_settings(job_apply_home, **settings)
    job = srv.add_job(url=fixture_url("site/privacy-agreement.html") + query, title="Recruiter",
                      company="Example Staffing")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    if settings or query:
        assert (r.status, r.need) == ("needs_you", "stuck") and "“I Accept”" in r.reason, (r.reason, r.log)
        assert r.url.endswith("privacy-agreement.html" + query), r.url
        assert not any(line.startswith("agreed") for line in r.log), r.log
        return
    assert "agreed to Example Staffing's “I Accept” for you (settings.accept_notices)" in r.log, r.log
    assert r.url.endswith("generic_form.html") and any(line.startswith("filled") for line in r.log), r.log
    # and the page's words kept with it (Run.agreed, for the live check's audit), never shown
    gate = [a for a in r.agreed if a["what"] == "terms gate"]
    assert [(a["heading"], a["button"]) for a in gate] == [("Privacy Agreement", "I Accept")], r.agreed
    assert "collects the personal information you give" in gate[0]["words"] and "agreed" not in r.public()


def test_a_box_that_wouldnt_fill_is_said_as_that_not_asked(srv, monkeypatch):
    """A fill that failed with an error is no question the profile doesn't answer: Eightfold's
    Country of Residence came back as one, "TimeoutError: Locator.evaluate: Timeout 15000ms
    exceeded." on the card (live, Oct 2026). It's said as a fill that failed."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    fill_one = srv.browser._fill_one

    async def times_out(page, field, value, *given):
        if field.get("label") == "Country of Residence":
            raise PlaywrightTimeout("Locator.evaluate: Timeout 15000ms exceeded.")
        return await fill_one(page, field, value, *given)

    monkeypatch.setattr(srv.browser, "_fill_one", times_out)
    job = srv.add_job(url=fixture_url("site/ai-notice-form.html") + "?uploaded=1", title="Equipment Technician",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need, r.questions) == ("needs_you", "stuck", []), (r.status, r.reason, r.questions)
    assert r.reason.startswith("I couldn't fill “Country of Residence” (the page didn't respond in time). "
                               "Fill it in the desk's browser window, then press Carry on"), r.reason
    assert "TimeoutError" not in r.reason and "question" not in r.reason


def test_what_attests_to_an_application_and_what_doesnt():
    """An application's attestation (its information true and complete, or consent to the background
    check that comes with applying) and, the owner's call (Oct 10), its acceptance of the employer's
    privacy notice or terms of use are picked where they're required: a check box, the one choice
    that agrees, or the one Yes. A newsletter, job alerts, a talent community, cookies or a fact
    about the person isn't one."""
    certify = ("Terms and Conditions Please read carefully. I certify that the information contained in the "
               "application is correct and complete.")
    attestation = pipeline._attestation
    assert attestation({"kind": "combobox", "label": certify, "required": True, "options": ["I Agree"]}) == "I Agree"
    assert attestation({"kind": "select", "label": certify, "required": True, "options": ["Select...", "Yes"]}) == "Yes"
    assert attestation({"kind": "select", "label": certify, "required": True, "options": ["Yes", "No"]}) == "Yes"
    assert attestation({"kind": "checkbox", "label": "I authorize Example Corp to conduct a background check as part "
                                                     "of my application *", "required": True}) is True
    # the employer's privacy notice and terms (ASM's box, EMD's tick in a group of one, a yes/no)
    assert attestation({"kind": "checkbox", "label": "I have read and agree to the Privacy notice and Terms of use.*",
                        "required": True}) is True
    assert attestation({"kind": "checkbox", "label": "I agree to the terms of use", "required": True}) is True
    # EMD's required pair (live, Oct 2026): its terms ticked, its marketing email left
    emd = ("Terms of Use & Data Privacy Statement: Please accept our Terms of Use and Data Privacy Statement. For "
           "Chinese Residents: In addition to the above statement, you also agree to the international transfer of "
           "your personal data.")
    mail = ("Email Communications: By checking this box, you wish to receive email campaigns, which may include event "
            "invites. You may withdraw your consent at any time by clicking the unsubscribe link. Detailed information "
            "is available in our Privacy Statement.")
    assert attestation({"kind": "checkbox_group", "label": "You are applying for", "required": True,
                        "options": [emd, mail]}) == [emd]
    assert attestation({"kind": "checkbox_group", "label": "Consents", "required": True,
                        "options": ["I accept the privacy notice", "Send me job alerts"]}) == ["I accept the privacy notice"]
    # an attestation is one whatever else it says of its use
    assert attestation({"kind": "checkbox", "label": "I certify that the information in this application is true and "
                        "complete, and understand false statements may disqualify me from future opportunities",
                        "required": True}) is True
    # (the review's: a certification naming affiliates or offers isn't a sign-up; nor emails about the application)
    assert attestation({"kind": "checkbox", "label": "I certify the information is true and complete and authorize Acme "
                        "and its affiliates to conduct a background check", "required": True}) is True
    assert attestation({"kind": "checkbox", "label": "I agree to the privacy policy and to receive emails about my "
                        "application", "required": True}) is True
    assert attestation({"kind": "radio_group", "label": "Do you accept our applicant privacy notice?", "required": True,
                        "options": ["Yes", "No"]}) is None  # (a radio group of two isn't one it picks in)
    # a lone radio is, as a tick box (BrassRing's consents, Edward Jones' run 83), but only one that accepts the notice
    consent = "By clicking the 'I agree' button below, you are {}."
    assert attestation({"kind": "radio_group", "label": consent.format(
        "acknowledging you have received and had the opportunity to review the above Privacy Notice"),
        "required": True, "options": ["I agree"]}) == "I agree"
    for said in ("consenting to receive electronic communications from Example Corp",
                 "consenting to Texting or automated calling with Example Corp",
                 # (the review's: a privacy notice in the same words doesn't make texts or calls one)
                 "consenting to Texting or automated calling with Example Corp, as described in our Privacy Notice",
                 "acknowledging the Privacy Notice and agree that Example Corp may call or text you",
                 "acknowledging the Privacy Notice and consenting to WhatsApp messages from Example Corp",
                 # nor anything about the person, and never a lone "I disagree"
                 "certifying the information is true and complete and that I am authorized to work in the United States"):
        assert attestation({"kind": "radio_group", "label": consent.format(said), "required": True,
                            "options": ["I agree"]}) is None, said
    # (the review of Oct 15: a box whose words also say something about the person, or ask another question, is
    # theirs, as a lone choice's are)
    for said in ("I certify that the information provided is true and complete, and that I am legally authorized to "
                 "work in the United States",
                 "I certify that the information provided is true and complete and I have never been convicted of a felony",
                 "I certify the information is true and that I am at least 18 years of age",
                 "I certify that the information provided is true and complete and that I am willing to undergo a "
                 "background check",
                 "Have you ever been convicted of a crime? I certify that the information provided is true"):
        assert attestation({"kind": "checkbox", "label": said, "required": True}) is None, said
    # but not for its consequence clauses, which say nothing about the person (the second review's)
    for said in ("I certify that my answers are true and complete to the best of my knowledge. I understand that if I am "
                 "employed, false statements on this application shall be considered sufficient cause for dismissal.",
                 "I certify that the information provided is true and complete. I understand that I am subject to a "
                 "background check as a condition of employment.",
                 "I certify that the information provided is true and complete and understand that this employer is a "
                 "drug-free workplace",
                 "I certify that the information is true and understand that eligibility for employment is contingent "
                 "on a background check",
                 "I certify that the information provided is true and complete and that I may be required to present a "
                 "valid driver's license if hired",
                 "I acknowledge that I am applying with Example Corp and that the information provided is true and complete"):
        assert attestation({"kind": "checkbox", "label": said, "required": True}) is True, said
    privacy = consent.format("acknowledging you have received and had the opportunity to review the above Privacy Notice")
    for no in ("I disagree", "Decline", "Do not agree"):
        assert attestation({"kind": "radio_group", "label": privacy, "required": True, "options": [no]}) is None, no
    # nor a lone choice whose own words say more (the second review's), a radio's or a list's
    for kind in ("radio_group", "select"):
        for more in ("I agree, and I consent to receive text messages and job alerts",
                     "Yes, and sign me up for the talent community", "I agree to binding arbitration of any dispute",
                     "I am authorized to work in the US without sponsorship"):
            assert attestation({"kind": kind, "label": privacy, "required": True, "options": [more]}) is None, more
            assert attestation({"kind": kind, "label": certify, "required": True, "options": [more]}) is None, more
    assert attestation({"kind": "checkbox", "label": "I acknowledge the Privacy Notice and agree that Example Corp may "
                        "call or text you", "required": True}) is None
    # nor ever a legal waiver, whatever else it attests (the review's)
    assert attestation({"kind": "checkbox", "label": "I certify the information is true and complete, and I agree to "
                        "resolve any dispute through binding arbitration", "required": True}) is None
    assert attestation({"kind": "select", "label": "Do you accept our applicant privacy notice?", "required": True,
                        "options": ["Select...", "Yes", "No"]}) == "Yes"
    for q in ({"kind": "select", "label": certify, "required": True, "options": ["I do not agree"]},
              {"kind": "combobox", "label": certify, "required": False, "options": ["I Agree"]},
              {"kind": "combobox", "label": certify, "required": True, "options": []},  # choices unknown
              {"kind": "select", "label": certify, "required": True, "options": ["Yes, I agree", "Yes"]},  # which?
              {"kind": "checkbox", "label": "I certify that I am at least 18 years old", "required": True},
              {"kind": "checkbox", "label": "I certify my information is true, and send me job alerts", "required": True},
              {"kind": "checkbox", "label": "I agree (i) to join the global Talent Community to be considered for current "
                                            "and future job opportunities as explained in our privacy statement",
               "required": True},
              {"kind": "select", "label": "Use of Your Profile: Please select Yes if you wish your profile to be kept in "
                                          "our global talent community (see our privacy statement)", "required": True,
               "options": ["Please Select", "Yes", "No"]},
              {"kind": "checkbox", "label": "I agree to receive text messages about my application (privacy policy)",
               "required": True},
              {"kind": "checkbox", "label": "I accept the cookie and privacy policy", "required": True},
              # the safety audit's (Oct 12): an agreement or attestation that also signs the person up
              {"kind": "checkbox", "label": "I have read and agree to the Privacy Policy and consent to receive email "
                                            "communications from Acme", "required": True},
              {"kind": "checkbox", "label": "I agree to the terms of use and would like to receive updates "
                                            "about new jobs",
               "required": True},
              {"kind": "checkbox", "label": "I certify the information is true and complete and agree to receive emails",
               "required": True},
              {"kind": "checkbox_group", "label": "Consents", "required": True,
               "options": ["I accept the privacy notice and agree to receive news and offers"]},
              # the final review's: more than the application, in the same breath
              {"kind": "checkbox", "label": "I certify the information is true and complete and consent to being "
                                            "contacted about similar jobs", "required": True},
              {"kind": "checkbox", "label": "I certify the information is true and send me special offers",
               "required": True},
              {"kind": "checkbox", "label": "I agree to the privacy policy and to receive emails about my application "
                                            "and other opportunities", "required": True},
              {"kind": "checkbox_group", "label": "Consents", "required": True,
               "options": ["Send me job alerts", "Keep my profile for future opportunities (privacy notice)"]},
              # a box that also says something of the person, or a question beside the agreement
              {"kind": "checkbox", "label": "I have read the Privacy Notice and confirm I am legally authorized to work "
                                            "in the US without sponsorship", "required": True},
              {"kind": "checkbox", "label": "I agree to the terms of use and confirm I am 18 or older", "required": True},
              {"kind": "select", "label": "Have you ever been convicted of a felony? I certify that my answers are true "
                                          "and complete.", "required": True, "options": ["Select...", "Yes", "No"]},
              {"kind": "select", "label": "Have you read our privacy notice, and do you hold a CDL?", "required": True,
               "options": ["Yes", "No"]},
              {"kind": "checkbox", "label": "I consent to Example Corp retaining my personal data to consider me for other "
                                            "positions with its affiliates", "required": True},
              {"kind": "checkbox", "label": "I agree to the Arbitration Agreement and the privacy notice", "required": True},
              {"kind": "text", "label": "Type your name to certify that your answers are true", "required": True}):
        assert attestation(q) is None, q
    # gates: by their own button, headings or title; never one that also sends, or about what's never agreed to
    gate = pipeline._terms_gate
    assert gate({"text": "I Accept"}, {"headings": ["Privacy Agreement"]})
    assert gate({"text": "I Acknowledge the Privacy Notice"}, {"headings": ["Login"]})
    assert gate({"text": "I Agree"}, {"headings": [], "title": "Candidate Privacy Notice"})
    assert not gate({"text": "I Agree"}, {"headings": ["Arbitration Agreement"]})  # a privacy link in its footer
    assert not gate({"text": "I Accept"}, {"headings": ["Before you continue"]})  # its words: a talent community
    assert not gate({"text": "I Accept and Apply"}, {"headings": ["Privacy Agreement"]})
    assert not gate({"text": "I Accept"}, {"headings": ["Join our Talent Community", "Privacy"]})
    # a framed gate by its own frame's headings: not the careers page's around it (Schwab's iCIMS, Oct 10)
    schwab = {"headings": ["Join our talent network", "Returning candidate?", "Privacy Notice and Sign-In"],
              "frame_headings": {"": ["Join our talent network", "Returning candidate?"],
                                 "f2-": ["Privacy Notice and Sign-In"]}, "title": "Login"}
    assert gate({"id": "f2-qxi4", "text": "I Acknowledge the Privacy Notice"}, schwab)
    assert not gate({"id": "qxi4", "text": "I Acknowledge the Privacy Notice"}, schwab)  # one beside the sign-up
    assert not gate({"id": "f2-qxi4", "text": "I Acknowledge the Privacy Notice"},
                    {**schwab, "frame_headings": {"f2-": ["Join our Talent Community"]}})
    notice = pipeline._terms_notice
    assert notice("Privacy Policy of Example Corp", "Example Corp protects the personal data you give it.") == (True, True)
    assert notice("Before you apply", "Please read and accept our privacy notice.") == (True, False)
    for heading, text in (("Error", "Your personal information could not be saved."),
                          ("", "We found an existing profile with your personal information. Continue to overwrite it?"),
                          ("Session", "For your privacy you'll be signed out."),
                          ("Privacy preferences", "We use cookies. Accept them?"),
                          ("Before you continue", "Example Corp will call you about the next steps.")):
        assert notice(heading, text) == (False, False), (heading, text)
    # notices about AI screening, as the desk tells them from others
    assert pipeline._AI_NOTICE.search("Example Corp uses an artificial intelligence (\"AI\") recruiting software")
    assert pipeline._AI_NOTICE.search("We use automated employment decision tools to screen applications")
    assert not pipeline._AI_NOTICE.search("Example Corp protects the personal data you give it. Said in Spain.")


def test_create_account_is_filled_but_left_for_the_person(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/create-account.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "sign_in" and "Create Account form" in r.reason, r.reason
            page = r.page
            filled = await page.evaluate("() => [em.value, pw.value === pw2.value && pw.value.length > 0, terms.checked]")
            assert filled == ["sam.rivera@example.com", True, False]  # terms untouched
            assert await page.evaluate("() => window.created") == 0  # and the button not pressed
            await page.check("#terms")  # the person agrees and creates the account
            await page.click("button:has-text('Create Account')")
            await until(lambda: r.need == "questions" or r.status == "ready")  # the desk carried on by itself
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "filled the Create Account form with your details and saved password" in r.log


def manage_accounts(job_apply_home, submit_mode="review"):
    import yaml

    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile["settings"].update(manage_accounts=True, submit_mode=submit_mode)
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")


@pytest.mark.parametrize("at_once", [1, 2])
def test_with_manage_accounts_the_desk_creates_the_account(srv, monkeypatch, job_apply_home, at_once):
    """The person turned on settings.manage_accounts: the desk ticks the Create Account form's
    terms box and presses its button itself, with the saved password, and carries on. With two
    at once, another job is filled alongside meanwhile, and makes no account."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/create-account.html"), title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)
    applier.jobs_at_once = at_once

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            o = applier.enqueue(other["id"]) if at_once > 1 else None
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            if o is not None:
                await until(lambda: o.status not in ("queued", "running"), about=state(o))
                assert not any("account" in line for line in o.log), o.log
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert any(line.startswith("created your account on") and "agreed to its terms" in line for line in r.log), r.log
    assert r.need != "sign_in" and "Create Account" not in r.reason, (r.status, r.reason, r.log)
    # on the job's "What went in" panel: the account made, never its password, nor the account form's boxes
    went = json.loads(json.dumps(r.public()))["went_in"]
    assert len(went["accounts"]) == 1 and went["accounts"][0].startswith("created your account on") \
        and went["accounts"][0].endswith("and agreed to its terms"), went["accounts"]
    assert "not-a-real-password" not in json.dumps(r.public())
    assert not any("assword" in a["label"] for a in went["answers"]), went
    # the terms box ticked, kept in its own words with the button that made the account (Run.agreed)
    assert [(a["what"], a["heading"], a["button"], a["choices"]) for a in r.agreed] == [
        ("account box", "Yes, I have read and consent to the terms and conditions", "Create Account", [])], r.agreed


@pytest.mark.parametrize("chosen", [True, False])
def test_the_log_says_where_to_turn_manage_accounts_off_only_until_the_profile_has_chosen_it(
        srv, monkeypatch, job_apply_home, chosen):
    """Notes 23 and 37: "(manage_accounts: false in profile.yaml leaves this to you)" ended the line about
    the account made even for someone whose profile says manage_accounts: true."""
    import yaml

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    if not chosen:  # the profile doesn't say: the setting is on, as it is by default
        path = job_apply_home / "profile.yaml"
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
        del profile["settings"]["manage_accounts"]
        path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    assert config.Profile.load().settings.may_manage_accounts
    job = srv.add_job(url=fixture_url("site/create-account.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert any(line.startswith("created your account on") for line in r.log), r.log
    hinted = [line for line in r.log if "manage_accounts: false" in line]
    if chosen:
        assert not hinted, r.log
    else:
        assert len(hinted) == 1 and hinted[0].startswith("created your account on") \
            and hinted[0].endswith("(manage_accounts: false in profile.yaml leaves this to you)"), r.log


@pytest.mark.parametrize("headings, title, said", [
    # Workday's progress bar names the step, and a checkbox's legend or a footer's heading doesn't
    (["current step 4 of 6 Voluntary Disclosures", "I agree that I am 18 years of age or older and", "follow us"], "",
     "Voluntary Disclosures"),
    (["Errors Found", "My Information", "follow us"], "", "My Information"),
    (["current step 2 of 6", "Menu", "Skip to main content", "Contact Us"], "My Experience | Acme Careers",
     "My Experience"),
    (["3 errors", "Share"], "", "this page"),
    ([], "", "this page"),
    (["Example Financial", "Join our talent network", "Privacy Notice and Sign-In"], "Login | Example Financial",
     "Privacy Notice and Sign-In"),
])
def test_a_page_is_named_by_its_step_or_its_last_heading_that_names_a_page(headings, title, said):
    assert pipeline._where({"headings": headings, "title": title}) == (
        "this page" if said == "this page" else f"“{said}”")


def test_with_manage_accounts_a_create_account_forms_link_is_pressed(srv, monkeypatch, job_apply_home):
    """SCREEN's ApplicantStack (live, Oct 2026): its Create an Account form's own "Submit" is a link
    whose script sends the form, not a submit button, and it reads neither account nor sign up. The
    desk filled the form and then left it to the person, as if it had a picture code."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/create-account-username.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert any(line.startswith("pressed “Submit” to create your account") for line in r.log), r.log
    assert any(line.startswith("created your account on") for line in r.log), r.log
    assert r.need != "sign_in" and "Create Account" not in r.reason, (r.status, r.reason, r.log)
    assert any("“My Information”" in line for line in r.log), r.log  # on to the application


SF_ACCOUNT = "site/sf-create-account.html"
STATEMENT = "Data Privacy Consent Statement"


async def statement_page(r):
    """What the SuccessFactors-like Create Account kept for the tab: its sf.* keys (sessionStorage)."""
    return await r.page.evaluate("() => Object.fromEntries(Object.keys(sessionStorage).filter(k => k.startsWith('sf.'))"
                                 ".map(k => [k.slice(3), JSON.parse(sessionStorage.getItem(k))]))")


def create_account_on_sf(srv, query="", watch=0):
    """Apply at the SuccessFactors-like Create Account until the desk stops: the run, what the page kept, and
    what its boxes hold (None once it has gone on to the application). `watch`: seconds more it's left to
    watch the paused tab, which carries on by itself where the person is past the pause."""
    job = srv.add_job(url=fixture_url(SF_ACCOUNT) + query, title="Financial Analyst", company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            await asyncio.sleep(watch)
            form = await r.page.evaluate("() => document.getElementById('fbclc_userName') && [fbclc_userName.value, "
                                         "fbclc_fName.value, fbclc_country.value, fbclc_pwd.value.length > 0]")
            return r, await statement_page(r), form
        finally:
            await applier.stop()

    return run(go())


@pytest.mark.parametrize("how", ["on", "slow", "once", "test-identity", "long", "gigya"])
def test_with_manage_accounts_a_data_privacy_statement_is_opened_and_accepted_for_the_account(
        srv, monkeypatch, job_apply_home, how):
    """SuccessFactors' Create Account (APS's, live, Oct 10, 2026) makes no account until its data privacy
    statement has been read and accepted: a link, "Read and accept the data privacy statement.", opens it in
    a dialog (Accept, Decline). The desk pressed Create Account without it, and the page stayed, with only
    its password hints to say ("Password accepted; Password matches"). With settings.accept_notices the desk
    opens the statement, accepts it for the person (as any employer notice), says so in the log and presses
    Create Account; never Decline, and never the box for hearing more about career opportunities (marketing).
    The statement comes a moment after its link is pressed (slow). The live check's test identity does the
    same in practice mode. Amkor's link reads "Please read and accept the data privacy statement to proceed.",
    longer than the form reader took a button's words to be, so it was never seen (long, live check Oct 11).
    Edwards Vacuum's (gigya, run 57) has it inside the label of a required box greyed out until it's accepted,
    "Read and acknowledge the data privacy statement.", and a "Submit": the link was never seen as one to press."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    if how == "test-identity":
        as_test_identity(monkeypatch, job_apply_home)
    r, kept, _ = create_account_on_sf(srv, f"?{how}" if how in ("slow", "once", "long", "gigya") else "")
    log = "\n".join(r.log)
    agreed = f"agreed to Example Corp's notice “{STATEMENT}” for you (settings.accept_notices)"
    assert agreed in r.log, log
    assert any(line.startswith("created your account on") and "accepted its data privacy statement" in line
               for line in r.log), log
    made = "pressed “Submit” to create" if how == "gigya" else "pressed “Create Account”"
    assert r.log.index(agreed) < next(i for i, line in enumerate(r.log) if line.startswith(made)), log
    opened = 2 if how == "once" else 1  # (once: its first press opened nothing, and it was pressed again)
    assert kept == {"opened": opened, "statement": "accepted", "presses": 1, "created": 1, "campaign": False}, kept
    assert r.need != "sign_in" and "Create Account" not in r.reason, (r.status, r.reason, log)
    assert any("“My Information”" in line for line in r.log), log  # on to the application


def test_a_create_account_choice_the_profile_doesnt_make_is_named_with_its_choices_and_left_to_the_person(
        srv, monkeypatch, job_apply_home):
    """Edwards Vacuum's Create Account (SAP's Gigya screen set, run 61): with its statement accepted and Submit
    pressed, it said "This field is required" over "Make my profile visible to:" (which recruiters see the profile),
    a choice it marks required only by a data-required and a star of its own, and the card said only that. The
    question is read as required and marked, and the card names it with its choices, as the person's: the desk never
    picks one."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    r, kept, _ = create_account_on_sf(srv, "?gigya&visibility")
    assert r.need == "sign_in" and "This field is required”" in r.reason, (r.status, r.reason, r.log)
    assert "“Make my profile visible to” is marked invalid" in r.reason, r.reason
    assert ("Its choices for “Make my profile visible to”: “Any recruiter within the Example Group worldwide”, “Any "
            "recruiter within the Example Group in this country”, “Only recruiters managing jobs I apply to”; that "
            "choice is yours.") in r.reason, r.reason
    assert kept["statement"] == "accepted" and kept["visibility"] is None and "created" not in kept, kept
    seen = next(f for f in r.page_info["fields"] if f.get("label", "").startswith("Make my profile visible to"))
    assert seen["required"] and seen["kind"] == "radio_group", seen  # (Gigya's data-required)


def test_an_emailed_passcode_in_a_password_box_takes_the_inbox_code_never_the_saved_password(
        srv, monkeypatch, job_apply_home):
    """APS's SuccessFactors (live run 9, Oct 11): once the account was made it asked for the code it emailed,
    "*Passcode:", in a password box, and the desk put the saved password there ("pressed Continue with your
    saved password"; "The passcode entered is incorrect"). A password box named for a code is an emailed
    code's: the inbox's code goes in, and the account goes on to the application."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    inbox_with_code(monkeypatch)
    r, kept, _ = create_account_on_sf(srv, "?passcode", watch=8)
    log = "\n".join(r.log)
    assert kept.get("passcode") == "123456", (kept, log)
    assert not any("Continue” with your saved password" in line for line in r.log), log
    assert "entered the code from your email" in log and any("“My Information”" in line for line in r.log), log
    # the job's "What went in" panel names the account made and the application's answers: never the emailed code,
    # nor the password
    went, shown = r.public()["went_in"], json.dumps(r.public())
    assert went["accounts"] and went["accounts"][0].startswith("created your account on"), went
    assert ("First Name", "Sam") in {(a["label"], a["value"]) for a in went["answers"]}, went
    assert "123456" not in shown and "not-a-real-password" not in shown


@pytest.mark.parametrize("how", ["notices-off", "practice"])
def test_a_data_privacy_statement_is_the_persons_without_accept_notices(srv, monkeypatch, job_apply_home, how):
    """Without settings.accept_notices (or in practice mode, which never makes an account) the desk fills
    SuccessFactors' Create Account form and leaves it: the data privacy statement agrees to something in
    the person's name, so it's not opened, nor Create Account pressed (the site would refuse it). The
    pause says plainly that the form asks them to read and accept it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    if how == "practice":
        manage_accounts(job_apply_home, submit_mode="dry_run")
    else:
        manage_accounts(job_apply_home)
        notice_settings(job_apply_home, accept_notices=False)
    r, kept, filled = create_account_on_sf(srv)
    assert (r.status, r.need) == ("needs_you", "sign_in"), (r.status, r.reason, r.log)
    assert "asks you to read and accept its data privacy statement" in r.reason, r.reason
    assert "accept_notices: true" in r.reason and "I tried" not in r.reason, r.reason
    assert kept == {}, kept  # the statement never opened, nothing pressed
    assert filled == ["sam.rivera@example.com", "Sam", "United States", True], filled  # the form is filled in
    assert not any("agreed to" in line for line in r.log), r.log


def test_a_data_privacy_statement_that_also_signs_up_for_marketing_is_the_persons(srv, monkeypatch, job_apply_home):
    """Never a newsletter, job alerts or marketing: a statement whose opening says that accepting it
    also signs the person up for marketing emails and job alerts is opened but not accepted, left open
    for them to read, and Create Account is not pressed."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    r, kept, _ = create_account_on_sf(srv, "?marketing", watch=3)  # (it stays paused, the statement open)
    assert (r.status, r.need) == ("needs_you", "sign_in"), (r.status, r.reason, r.log)
    assert "asks you to read and accept its data privacy statement" in r.reason, r.reason
    assert "I tried to open it and accept it for you, and couldn't." in r.reason, r.reason
    # opened (once, or again after its wait on a slow machine: Windows' runner), and nothing else: not accepted, declined
    # or closed (the review of Oct 15)
    assert set(kept) == {"opened"} and kept["opened"] >= 1, kept
    assert not any("agreed to" in line or "created your account" in line for line in r.log), r.log


def test_a_data_privacy_statements_link_and_its_long_text_are_told_from_the_rest():
    """A Create Account form's link to the statement is told by its words (read and accept, a privacy
    notice or terms, nothing about a newsletter, job alerts or a talent community) and its place after
    the password boxes, in the form. Its dialog, named so in its heading, is read for what's never agreed to
    only at its opening: its long text speaks of affiliates, marketing and deleting data as practices."""
    def link(text, **kw):
        return {"id": "a1", "text": text, "in_account_form": True, **kw}

    found = pipeline._privacy_statement({"actions": [link("Show"), link("Read and accept the data privacy statement.")]})
    assert found is not None and found["text"].startswith("Read and accept")
    for text in ("Data privacy policy", "Read our privacy statement", "I accept the privacy statement",
                 "Read and accept the talent community privacy statement",
                 "Read and agree to receive job alerts, and the privacy statement"):
        assert pipeline._privacy_statement({"actions": [link(text)]}) is None, text
    footer = {"id": "a2", "text": "Read and accept the data privacy statement."}  # not in the account form
    assert pipeline._privacy_statement({"actions": [footer]}) is None
    assert pipeline._privacy_statement({"actions": [link(footer["text"], disabled=True)]}) is None
    notice = pipeline._terms_notice
    statement = ("Contact the HR Service Team at 100 Example Street, Example City. " * 8
                 + "Example Corp and its affiliated companies protect your Personal Information. It is not used for "
                 "marketing. You may ask us to delete it, withdraw your consent, or stop cookies.")
    assert statement.index("affiliated") > pipeline.NOTICE_OPENING  # (past its opening)
    assert notice("Data Privacy Consent Statement", statement) == (True, True)
    assert notice("Before you apply", statement) == (False, False)  # not named in its heading: all of it is read
    assert notice("Data Privacy Consent Statement",
                  "By accepting you also agree to receive marketing emails. " + statement) == (False, False)
    assert notice("Talent Community Privacy Statement", statement) == (False, False)
    # a legal waiver is never agreed to for the person, however deep in the statement it sits
    for waiver in ("Any dispute will be resolved by binding arbitration.", "You waive any right to a jury trial.",
                   "By clicking Accept you also join our Talent Community and receive job alerts."):
        assert notice("Data Privacy Consent Statement", statement + " " + waiver) == (False, False), waiver


def test_a_create_account_drawn_a_moment_after_its_link_is_waited_for(srv, monkeypatch, job_apply_home):
    """Edwards Vacuum's SuccessFactors (run 60): after its saved password was refused, the desk pressed "Don't have an
    account yet?", read the sign-in page again at once (the Create Account form comes a few seconds later), pressed the
    link again (which starts it over) and stopped: "Sign in (or create your account)". The page is given a moment to
    change after Create Account is pressed, and the form that comes is filled."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/signin-slow-create.html"), title="Field Service Engineer",
                      company="Example Vacuum")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => [sessionStorage.getItem('slow.create'), sessionStorage.getItem('slow.made')]")
        finally:
            await applier.stop()

    r, (presses, made) = run(go())
    assert presses == "1", (presses, r.log)
    assert any(line.startswith("pressed “Submit” to create your account") for line in r.log), (r.reason, r.log)
    assert made == "1", (r.status, r.reason, r.log)


def as_test_identity(monkeypatch, job_apply_home, email="sam.rivera@example.com"):
    """The nightly live check's switches on (live_smoke.py --test-identity), with `email` as the test
    identity's: the conftest profile's own makes it the test identity, in practice mode."""
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    monkeypatch.setenv("JOB_APPLY_LIVE_TEST_IDENTITY", "1")
    monkeypatch.setenv("LIVE_TEST_EMAIL", email)
    manage_accounts(job_apply_home, submit_mode="dry_run")


@pytest.mark.parametrize("identity", [None, "jobdesk.test@example.com"], ids=["practice", "not-the-test-identity"])
def test_in_practice_mode_the_desk_never_creates_an_account(srv, monkeypatch, job_apply_home, identity):
    """Practice mode sends nothing, an account's details included, whatever manage_accounts says.
    So too with the live check's switches on, for a profile that isn't its test identity's."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home, submit_mode="dry_run")
    if identity:
        as_test_identity(monkeypatch, job_apply_home, identity)
    job = srv.add_job(url=fixture_url("site/create-account.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => window.created")
        finally:
            await applier.stop()

    r, created = run(go())
    assert (r.need, created) == ("sign_in", 0) and "Create Account form" in r.reason, (r.reason, r.log)


def test_the_live_checks_test_identity_creates_the_account_in_practice_mode(srv, monkeypatch, job_apply_home):
    """The nightly live check's test identity (a clearly fake applicant with its own inbox; the
    owner's call, Oct 10) makes the account in practice mode, so the check goes on past sign-in."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    as_test_identity(monkeypatch, job_apply_home)
    job = srv.add_job(url=fixture_url("site/create-account.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert any(line.startswith("created your account on") for line in r.log), (r.reason, r.log)
    assert r.need != "sign_in" and "Create Account" not in r.reason, (r.status, r.reason, r.log)


def test_the_live_checks_test_identity_never_submits(srv, monkeypatch, job_apply_home):
    """The test identity makes accounts and agrees to notices in practice mode, and that's all: with
    "Submit for me" on, and with Submit pressed on the desk, nothing is sent, and the last lock on the
    button (press_submit) still refuses it. Loosening any of them for the test identity fails this."""
    from job_apply.browser import SubmitBlocked

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    as_test_identity(monkeypatch, job_apply_home)
    assert config.Profile.load().settings.may_manage_accounts  # (it is the test identity)
    url = "https://careers.acme-fab.example/apply/1"
    posts: list[str] = []
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, {url: _form("Acme Fab", action="/application")}, posts)
        applier.start()
        try:
            r = applier.enqueue(job["id"], submit=True)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            first = r.status
            applier.submit_now(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            assert srv.browser.use_tab(r.page)  # (the job's own tab)
            [button] = await srv.browser.find_submit()
            with pytest.raises(SubmitBlocked):
                await srv.browser.press_submit(button["id"])
            return r, first
        finally:
            await applier.stop()

    r, first = run(go())
    assert first == "ready" and posts == [], (first, posts, r.reason, r.log)
    assert r.status != "submitted" and srv.tracker().get(job["id"])["status"] == "ready_to_submit", (r.status, r.reason)


@pytest.mark.parametrize("page, query", [("privacy-agreement.html", "?form=1"), ("ai-notice-form.html", "?notice=1&ack=1")],
                         ids=["gate", "dialog"])
@pytest.mark.parametrize("identity", [True, False], ids=["test-identity", "practice"])
def test_the_test_identity_agrees_with_a_forms_button_in_practice_mode(srv, monkeypatch, job_apply_home, page, query,
                                                                       identity):
    """Charles Schwab's iCIMS posting (live, Oct 10) puts its privacy notice before the application,
    whose way on is a form's own button, "I Acknowledge the Privacy Notice": practice mode's lock on a
    form's buttons that aren't step buttons refused it, and the live check's test identity stopped there.
    The test identity agrees to notices in practice mode, so it presses that, as it does a notice's
    agree that is a form's button ("I Acknowledge"), and says so. Any other practice-mode profile leaves
    both to the person, as before."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    if identity:
        as_test_identity(monkeypatch, job_apply_home)
    else:
        manage_accounts(job_apply_home, submit_mode="dry_run")
    gate = page.startswith("privacy")
    job = srv.add_job(url=fixture_url(f"site/{page}") + query, title="Recruiter", company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, None if gate else await notice_page(r)
        finally:
            await applier.stop()

    r, shown = run(go())
    name = "I Acknowledge the Privacy Notice" if gate else NOTICE
    if not identity:
        assert (r.status, r.need) == ("needs_you", "stuck") and f"“{name}”" in r.reason, (r.status, r.reason, r.log)
        assert not any(line.startswith("agreed") for line in r.log), r.log
        if gate:
            assert r.url.endswith("privacy-agreement.html?form=1"), r.url
        else:
            assert shown["agreed"] == 0, shown
        return
    said = f"“{name}”" if gate else f"notice “{name}”"
    assert f"agreed to Example Corp's {said} for you (settings.accept_notices)" in r.log, (r.reason, r.log)
    if gate:
        assert r.url.endswith("generic_form.html") and any(line.startswith("filled") for line in r.log), r.log
    else:
        assert shown["agreed"] == 1 and shown["first"] == "Sam", shown
    assert r.status != "submitted", (r.status, r.reason)


@pytest.mark.parametrize("who", ["on", "test-identity", "off", "practice"])
def test_icims_privacy_gate_in_its_frame_takes_the_email_and_phone_and_is_acknowledged(srv, monkeypatch, job_apply_home,
                                                                                         who):
    """Charles Schwab's iCIMS (live, Oct 10): "Privacy Notice and Sign-In" in iCIMS's frame takes an
    email, a required Phone Country Code ("— Make a Selection —", read as an answer) and a Number,
    and its way on is "I Acknowledge the Privacy Notice", which sends them. The careers page around
    the frame has a "Join our talent network" sign-up of its own, whose heading made the gate look
    like one: the desk filled the email and left the press to the person. With accept_notices (and
    as the live check's test identity, in practice mode) it fills the email, the country code and
    the number from the profile's phone, presses it, and carries on to the sign-in step; with it
    off, or in any other profile's practice mode, the press stays the person's."""
    from urllib.parse import parse_qs, urlparse

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    if who == "test-identity":
        as_test_identity(monkeypatch, job_apply_home)
    elif who == "practice":
        notice_settings(job_apply_home, submit_mode="dry_run")
    elif who == "off":
        notice_settings(job_apply_home, accept_notices=False)
    job = srv.add_job(url=fixture_url("site/icims-privacy-gate.html"), title="Analyst", company="Example Financial")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            [frame] = [f for f in r.page.frames if "in_iframe=1" in f.url]
            return r, frame.url
        finally:
            await applier.stop()

    r, frame_url = run(go())
    sent = {k: v[0] for k, v in parse_qs(urlparse(frame_url).query).items()}
    if who in ("off", "practice"):
        assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
        assert "“I Acknowledge the Privacy Notice”" in r.reason, r.reason
        assert "step" not in sent and not any(line.startswith("agreed") for line in r.log), (sent, r.log)
        return
    assert r.log[1:3] == ["filled 3 field(s) on “Privacy Notice and Sign-In”", "agreed to Example Financial's “I "
                          "Acknowledge the Privacy Notice” for you (settings.accept_notices)"], (r.reason, r.log)
    assert sent.get("step") == "email", (frame_url, r.reason, r.log)
    assert (sent.get("css_loginName"), sent.get("countryCodeSelect"), sent.get("css_phoneNumber")) == (
        "sam.rivera@example.com", "US", "480-555-0123"), sent
    assert (r.status, r.need) == ("needs_you", "sign_in"), (r.status, r.reason, r.log)


@pytest.mark.parametrize("check", ["hCaptcha", "reCAPTCHA"])
def test_an_agreement_a_bot_check_holds_is_said_to_be_the_persons(srv, monkeypatch, job_apply_home, check):
    """Charles Schwab's iCIMS in the live check (Oct 2026): "I Acknowledge the Privacy Notice" was pressed
    for the person, then went blank and greyed out behind the page's hCaptcha, and the desk said it
    couldn't find the button. It says what it pressed, and that the bot check is theirs."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/icims-privacy-gate.html") + ("?bot=recaptcha" if check == "reCAPTCHA" else "?bot"),
                      title="Analyst", company="Example Financial")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert any(line.startswith("agreed to Example Financial's “I Acknowledge the Privacy Notice”") for line in r.log), r.log
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert "I pressed “I Acknowledge the Privacy Notice” for you" in r.reason and f"({check})" in r.reason, r.reason
    assert "couldn't find the button" not in r.reason


def test_after_a_resume_an_agreement_a_bot_check_holds_is_still_said_to_be_the_persons(srv, monkeypatch, job_apply_home):
    """Charles Schwab's iCIMS in the live check (run 57): after the person's Resume, the greyed "I Acknowledge the Privacy
    Notice" behind the page's hCaptcha (pressed on the pass before) read as a page with no way on: "I couldn't find the
    button". It's the bot check's, on any pass."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/icims-privacy-gate.html") + "?bot&keep", title="Analyst",
                      company="Example Financial")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            first = r.reason
            r = applier.resume(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return first, r
        finally:
            await applier.stop()

    first, r = run(go())
    assert "I pressed “I Acknowledge the Privacy Notice” for you" in first, first
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert "is greyed out, and the page is protected by a bot check (hCaptcha)" in r.reason, r.reason
    assert "couldn't find the button" not in r.reason


_TWO = ('<label for="p">{}</label><input id="p" type="password">'
        '<label for="p2">{}</label><input id="p2" type="password">')


@pytest.mark.parametrize("head,boxes,want", [
    ("Sign In", '<label for="u">User ID</label><input id="u" type="text"><label for="p">Password</label>'
                '<input id="p" type="password">', None),
    ("Sign In", '<p>Signing in as sam.rivera@example.com</p><label for="p">New Password</label>'
                '<input id="p" type="password">', None),
    ("Sign In", '<p>Signing in as sam.rivera@example.com</p><label for="p">Temporary Password</label>'
                '<input id="p" type="password">', None),
    ("Sign In", '<label for="p">Password</label><input id="p" type="password">', None),
    # a Create Account that shows the email it's for (BrassRing's, run 67): both boxes, and only that
    ("Create new account", '<p>Email address</p><p>sam.rivera@example.com</p>'
                           + _TWO.format("Password", "Re-enter password"), "prefilled"),
    ("Create new account", '<p>Email address</p><p>mysam.rivera@example.com</p>'
                           + _TWO.format("Password", "Re-enter password"), None),  # (another address)
    ("Reset your account password", '<p>sam.rivera@example.com</p>'
                                    + _TWO.format("New Password", "Confirm New Password"), None),
    ("My Account", '<p>Signed in as sam.rivera@example.com</p>' + _TWO.format("Password", "Confirm password"), None),
    ("Create new account", '<p>sam.rivera@example.com</p>' + _TWO.format("Current password", "Password"), None),
    ("Create new account", '<p>sam.rivera@example.com</p>' + _TWO.format("New password", "Confirm password"), None),
    # (a single-page app's title stays "Create new account" over its other steps: its reset's step isn't one)
    ("Create new account|Reset password", '<p>sam.rivera@example.com</p>'
                                          + _TWO.format("Password", "Confirm password"), None)])
def test_the_password_alone_goes_only_where_the_sign_in_shows_whos_signing_in(srv, monkeypatch, head, boxes, want):
    """A sign-in with no box for the email gets the saved password alone only where it shows the email
    ("Signing in as ...", Workday's after its own reset). Not beside a box the desk can't fill (a "User
    ID", which would be sent empty), nor in a new, temporary or unexplained password box. A Create Account that
    shows the email it's for, whole (BrassRing's after its passcode, run 67), gets it in both its boxes; never a
    password reset's or change's two boxes, an account page's, nor a pair with an old or a new password's
    (the review of 0.3.154)."""
    saved_password(monkeypatch)
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    title, _, heading = head.partition("|")
    run(page.set_content(f"<title>{title}</title><h2>{heading or title}</h2><form onsubmit='return false'>{boxes}"
                         "<button>Continue</button></form>"))
    data = run(srv.inspect_form(include_dropdown_options=False))
    applier = Applier(srv)
    r = Run(1, "FSE", "Example Fab")
    assert run(applier._sign_in(r, data, {})) == want, boxes
    filled = run(page.evaluate("() => [...document.querySelectorAll('input[type=password]')].map((b) => !!b.value)"))
    assert filled == ([True, True] if want else [False] * len(filled)), (boxes, filled)


def test_security_questions_are_read_by_a_box_or_by_the_forms_heading():
    """BrassRing's "Security questions" heading over "Question 1" (a list the form reader may not see) and its
    "Question 1 Answer" (run 67): the person's, by the heading too. And the email shown, only whole."""
    answer = {"id": "a", "label": "Question 1 Answer", "kind": "text"}
    assert pipeline._security_boxes([answer]) == []
    assert pipeline._security_boxes([answer], ["Account Information", "Security questions"]) == [answer]
    assert pipeline._security_asked({"fields": [], "headings": ["Security questions"]})
    assert pipeline._security_asked({"fields": [{"id": "q", "label": "Security question", "kind": "select"}]})
    assert not pipeline._security_asked({"fields": [answer], "headings": ["Account Information"]})
    assert pipeline._shows_address("Signing in as sam.rivera@example.com.", "sam.rivera@example.com")
    assert not pipeline._shows_address("mysam.rivera@example.com", "sam.rivera@example.com")
    assert not pipeline._shows_address("sam.rivera@example.com.au", "sam.rivera@example.com")


@pytest.mark.parametrize("then,slow,late", [("", 0, False), ("done", 0, False), ("alone", 0, False), ("", 3000, False),
                                            ("", 0, True)],
                         ids=["plain", "done", "alone", "slow", "slowforgot"])
def test_with_manage_accounts_a_refused_saved_password_is_reset(srv, monkeypatch, job_apply_home, then, slow, late):
    """The email has an account there, and the saved password isn't its password. With the
    inbox watched, the desk asks for a password reset before trying a new account (the owner's
    choice), opens the emailed link in the job's tab, sets the saved password as the new one,
    and signs in. Live (issue #112, note 14), the desk stopped after the reset with "Sign in ... in the
    browser window": the sign-in after it may say "Password has been reset" with only a "Sign In" button
    (done), or ask for the password alone (alone). It signs in with the new password there too.
    Again live (note 34) the site took a moment over the new password, its reset form still on show with no
    error (slow): that read as a taken password, then as a sign-in with no email box, and the desk asked the
    person to sign in. It waits for the sign-in; and the reset form, two password boxes like a Create Account
    form, doesn't spend the wait kept for one. A "Forgot your password?" whose script goes to the reset a moment
    after the click (slowforgot): the sign-in still on show was read as a reset the desk can't fill in, and it made a
    new account instead, never asking for the reset."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    wanted = []

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        wanted.append(want)
        link = (fixture_url("site/reset-password.html") + "?token=abc" + (f"&then={then}" if then else "")
                + (f"&slow={slow}" if slow else ""))
        return pipeline.mailbox.Found(want, link, "careers.example.com", time.time()) if want == "reset" else None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)
    waits = []
    wait_for_application = Applier._wait_for_application

    async def counted(self):
        waits.append(1)
        await wait_for_application(self)

    monkeypatch.setattr(Applier, "_wait_for_application", counted)
    job = srv.add_job(url=fixture_url("site/signin-forgot.html") + ("?slowforgot" if late else ""), title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you" and r.need != "email_code" or r.status == "ready",
                        timeout=90, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert "asked it to email a password reset" in log, log
    assert "opened the password reset link from your email (sent from careers.example.com)" in log, r.log
    assert "set your saved password as" in log, r.log
    set_at = next(i for i, line in enumerate(r.log) if line.startswith("set your saved password as"))
    assert "pressed “Sign In” with your saved password" in r.log[set_at:], r.log  # signed in after the reset
    assert r.seen_form and not any(w in r.url for w in ("signin", "reset", "forgot")), (r.url, r.reason, r.log)
    assert "Sign in (or create your account)" not in (r.reason or ""), (r.reason, r.log)
    assert "reset" in wanted
    assert "opened Create Account" not in log and "created your account" not in log, log
    assert not waits  # (the reset form has two password boxes, but it isn't a Create Account form drawing its application)
    # (signed in: what the reset left on the job is spent, and a refusal after this is the ordinary one: the review of
    # 0.3.209)
    assert not (r.reset_set or getattr(r, "reset_opened", True)), (r.reset_set, getattr(r, "reset_opened", None))


def test_a_new_password_the_site_never_takes_is_left_to_the_person(srv, monkeypatch, job_apply_home):
    """The reset form stays on show after the desk's button press, with no error, past RESET_SETTLE: the
    saved password wasn't taken as the new one, and the person is told so (not asked to sign in)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setattr(pipeline, "RESET_SETTLE", 2)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        link = fixture_url("site/reset-password.html") + "?token=abc&slow=600000"
        return pipeline.mailbox.Found(want, link, "careers.example.com", time.time()) if want == "reset" else None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)
    job = srv.add_job(url=fixture_url("site/signin-forgot.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you" and r.need != "email_code" or r.status == "ready",
                        timeout=90, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in" and "didn't take it as the new one" in r.reason, (r.need, r.reason, r.log)
    assert "set your saved password as" not in "\n".join(r.log), r.log


def _reset_through(monkeypatch, job_apply_home, srv, link: str, resumes: int = 0) -> Run:
    """A job whose saved password the site refuses, with the inbox watched: the reset asked for, its email's link
    (`link`) opened, and the job run until it stops for the person or is ready; then `resumes` times, the person's
    Resume and the same again (the log says what each stop said: "stopped: ...")."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        return pipeline.mailbox.Found(want, link, "careers.example.com", time.time()) if want == "reset" else None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)
    job = srv.add_job(url=fixture_url("site/signin-forgot.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you" and r.need != "email_code" or r.status == "ready",
                        timeout=90, about=state(r))
            for _ in range(resumes):
                r.log.append(f"stopped: {r.reason}")
                applier.resume(job["id"])
                await until(lambda: r.status not in ("queued", "running"), timeout=90, about=state(r))
                await until(lambda: r.status == "needs_you" and r.need != "email_code" or r.status == "ready",
                            timeout=90, about=state(r))
            return r
        finally:
            await applier.stop()

    return run(go())


def test_a_sign_in_refused_right_after_the_desks_own_reset_is_said_and_makes_no_account(srv, monkeypatch,
                                                                                         job_apply_home):
    """KLA's Workday, the reset probe's second pass (run 136): the reset's link was spent (the pass before's email), the
    site set nothing and said nothing of it, its form went, and the desk said it had set the saved password. The sign-in
    after it was refused, and the desk opened Create Account and pressed it, for an account that's there. A sign-in
    refused right after the desk's own reset is said for what it is, on a card, and no Create Account is pressed."""
    r = _reset_through(monkeypatch, job_apply_home, srv, fixture_url("site/reset-password.html") + "?token=abc&spent")
    log = "\n".join(r.log)
    assert "set your saved password as" in log, r.log  # (as far as the desk could tell)
    set_at = next(i for i, line in enumerate(r.log) if line.startswith("set your saved password as"))
    assert "pressed “Sign In” with your saved password" in r.log[set_at:], r.log
    assert r.need == "sign_in" and r.reason.startswith("I set your saved password through"), (r.need, r.reason, r.log)
    assert "it still doesn't sign in there (it says “Wrong email address or password" in r.reason, r.reason
    assert "a reset link works once" in r.reason and "then press Carry on" in r.reason, r.reason
    assert "opened Create Account" not in log and "create your account" not in log, r.log
    assert "create-account" not in r.page.url, r.page.url


def test_a_reset_page_that_says_its_link_has_expired_is_said_not_taken_for_a_set_password(srv, monkeypatch,
                                                                                          job_apply_home):
    """The reset's form went once its button was pressed, and the page says the link has expired: the saved password
    wasn't set. The desk said it was set (only the form going was looked at); it stops and says so in the site's
    words."""
    r = _reset_through(monkeypatch, job_apply_home, srv, fixture_url("site/reset-password.html") + "?token=abc&expired")
    assert r.need == "sign_in" and "it says “This link has expired" in r.reason, (r.need, r.reason, r.log)
    assert "didn't set it as the new one" in r.reason and "Forgot password" in r.reason, r.reason
    assert "set your saved password as" not in "\n".join(r.log), r.log


def test_a_reset_link_that_says_its_spent_as_it_opens_is_said_and_makes_no_account(srv, monkeypatch, job_apply_home):
    """The review of 0.3.209: a spent reset link whose page says so as it opens (no new-password boxes, "This link has
    expired") was read only after a press there never was: the job stopped on "couldn't find the button", or went on to
    the sign-in and Create Account. Its words are read as it opens, and said."""
    r = _reset_through(monkeypatch, job_apply_home, srv, fixture_url("site/reset-password.html") + "?token=abc&gone")
    log = "\n".join(r.log)
    assert r.need == "sign_in" and r.reason.startswith("The password reset link from your email opened on "), \
        (r.need, r.reason, r.log)
    assert "saying “This link has expired" in r.reason and "couldn't find the button" not in r.reason, r.reason
    assert "set your saved password as" not in log and "opened Create Account" not in log, r.log
    assert "create your account" not in log, r.log


def test_resume_on_a_spent_reset_links_card_goes_back_to_the_sign_in(srv, monkeypatch, job_apply_home):
    """The re-review of 0.3.209: the card for a spent reset link asks the person to set the password through a new
    email's link, which opens in their own browser; the desk's tab stayed on the spent page, so every Resume read it
    again and said the same card. Resume goes back to the sign-in and tries the saved password once; refused there,
    it's said, and never a Create Account."""
    r = _reset_through(monkeypatch, job_apply_home, srv, fixture_url("site/reset-password.html") + "?token=abc&gone",
                       resumes=1)
    log = "\n".join(r.log)
    first = next(i for i, line in enumerate(r.log) if line.startswith("stopped: The password reset link from your email"))
    assert "pressed “Sign In” with your saved password" in r.log[first:], r.log
    assert r.need == "sign_in" and r.reason.startswith("Your saved password still doesn't sign in on"), \
        (r.need, r.reason, r.log)
    assert "opened Create Account" not in log and "create your account" not in log, r.log


def test_a_sign_in_refused_after_the_reset_link_opened_makes_no_account(srv, monkeypatch, job_apply_home):
    """The review of 0.3.209: a reset link that goes on to the sign-in at once, saying nothing (spent, or the site's way
    with one), and the saved password still refused there: the account is there (the site emailed its reset), so no
    Create Account; a card says what happened."""
    r = _reset_through(monkeypatch, job_apply_home, srv, fixture_url("site/reset-password.html") + "?token=abc&straight")
    log = "\n".join(r.log)
    assert "opened the password reset link from your email" in log and "set your saved password as" not in log, r.log
    assert r.need == "sign_in" and r.reason.startswith("Your saved password still doesn't sign in on"), \
        (r.need, r.reason, r.log)
    assert "after its password reset (it says “Wrong email address or password" in r.reason, r.reason
    assert "a reset link works once" in r.reason and "then press Carry on" in r.reason, r.reason
    assert "opened Create Account" not in log and "create your account" not in log, r.log


def test_a_session_expired_note_after_a_reset_that_went_in_is_no_refusal(srv, monkeypatch, job_apply_home):
    """The review of 0.3.209: a site whose password change ends every session goes on to its sign-in saying "Your
    session has expired. Please sign in again.": read as the reset's refusal, the desk stopped. The password is set, and
    the desk signs in with it."""
    r = _reset_through(monkeypatch, job_apply_home, srv,
                       fixture_url("site/reset-password.html") + "?token=abc&then=session")
    log = "\n".join(r.log)
    assert "set your saved password as" in log and "didn't set it" not in (r.reason or ""), (r.reason, r.log)
    assert r.seen_form and not any(w in r.url for w in ("signin", "reset", "forgot")), (r.url, r.reason, r.log)


def test_a_reset_email_whose_link_was_opened_already_is_said_never_taken_for_no_email(srv, monkeypatch,
                                                                                      job_apply_home):
    """The review of 0.3.209: a site that sends the same reset link again (or an earlier reset's email, its link opened
    already) had it passed over until the wait ran out, which then read as no email at all: "no password reset email
    came ... so I went back to make an account there". An email came, so the account is there: a card says so."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "RESET_MAIL_WAIT", 4)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    link = fixture_url("site/reset-password.html") + "?token=sent-again"
    watched_inbox(monkeypatch, reset_link=link)
    pipeline._take_code(("careers.example.com", link, time.time()), 999)  # (opened already: another job's reset)
    job = srv.add_job(url=fixture_url("site/signin-forgot.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you" and r.need != "email_code" or r.status == "ready",
                        timeout=60, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert r.need == "sign_in" and "but its link is one the desk opened already" in r.reason, (r.need, r.reason, r.log)
    assert "no password reset email came" not in log and "opened the password reset link" not in log, r.log
    assert "opened Create Account" not in log and "create your account" not in log, r.log


def test_a_password_saved_since_the_reset_is_refused_without_its_card_and_makes_no_account(srv, monkeypatch):
    """The review of 0.3.209: what a reset leaves on the job's card (the desk set the saved password there, or opened
    its link) holds for that password only: the person saves another, and the site refuses it, said the ordinary way.
    The re-review: but the account is there all the same (Run.account_known), so never a Create Account for the job,
    as the cards' "or another, and save that one on the desk" led to. A job with no reset still gets one."""
    import hashlib

    saved_password(monkeypatch)
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.goto(fixture_url("site/signin-forgot.html")))
    data = run(srv.inspect_form(include_dropdown_options=False))
    applier = Applier(srv)
    same = Run(1, "FSE", "Example Fab", sign_in_tries=2,
               sign_in_key=hashlib.sha256(b"not-a-real-password").hexdigest())  # (the saved one: the reset's)
    same.reset_set = same.reset_opened = True
    assert run(applier._sign_in(same, data, {"submitted": 1, "settled": 1})) == "refused"
    since = Run(2, "FSE", "Example Fab", sign_in_tries=2,
                sign_in_key=hashlib.sha256(b"the-password-the-reset-set").hexdigest())  # (another saved since)
    since.reset_set = since.reset_opened = since.account_known = True
    assert run(applier._sign_in(since, data, {"submitted": 1})) == "refused"
    assert not (since.reset_set or since.reset_opened) and since.account_known
    fresh = Run(3, "FSE", "Example Fab", sign_in_tries=2,
                sign_in_key=hashlib.sha256(b"the-password-the-reset-set").hexdigest())
    assert run(applier._sign_in(fresh, data, {"submitted": 1})) == "create_account"


def test_a_new_account_is_signed_in_with_and_only_its_own_form_is_sent(srv, monkeypatch, job_apply_home):
    """After the desk makes the account, the site asks to sign in: the new account's sign-in is
    tried, not taken for the refused one. The header's talent-community "Sign Up" isn't the
    account form's button, and an optional "contact me" box is left unticked."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/signin-new-account.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    seen = {}

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])

            async def watch():
                while r.status in ("queued", "running"):
                    if r.page is not None and "create-account-signin" in r.page.url:
                        try:
                            got = await r.page.evaluate("() => ({talent: window.talent, contact: window.contact})")
                        except Exception:
                            got = {}
                        # (a look while the page is being left finds neither set: only what it did set counts)
                        if got.get("talent") is not None:
                            seen["talent"] = max(seen.get("talent", 0), got["talent"])
                        if got.get("contact") is not None:
                            seen["contact"] = got["contact"]
                    await asyncio.sleep(0.05)

            await asyncio.gather(watch(), until(lambda: r.status not in ("queued", "running"), about=state(r)))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.seen_form and "reset" not in " ".join(r.log), (r.reason, r.log)
    assert sum(line.startswith("pressed \u201cSign In\u201d") for line in r.log) == 2, r.log
    assert seen.get("talent") == 0 and seen.get("contact") is not True, seen  # (the page was seen: talent is 0 there)


def test_a_new_account_the_site_turns_down_is_not_taken_for_an_existing_one(srv, monkeypatch, job_apply_home):
    """"Already have an account? Sign In" is on most Create Account pages: a password the site
    turns down there isn't the email having an account, so no reset is asked for."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/create-account-signin.html") + "?policy", title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in" and "wants something more" in r.reason, (r.reason, r.log)
    assert not any("password reset" in line for line in r.log), r.log


def test_without_the_inbox_resume_after_a_reset_signs_in_again(srv, monkeypatch, job_apply_home):
    """With no email app password on the desk the person opens the reset email and sets the saved
    password themselves, then presses Resume: the desk goes back to the sign-in page and signs in
    with it, instead of pausing on "we've sent you a link" again."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/signin-forgot.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you", about=state(r))
            assert r.need == "email_code" and "Open the link in the email" in r.reason, (r.reason, r.log)
            applier.enqueue(job["id"])  # Resume, after the person set the password
            await until(lambda: r.status == "needs_you" and r.need != "email_code", about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert sum(line.startswith("pressed \u201cSign In\u201d") for line in r.log) == 2, r.log
    assert r.log.count(next(line for line in r.log if "is emailing you a password reset" in line)) == 1, r.log


def test_a_sign_in_forms_submit_is_pressed(srv, monkeypatch):
    """SuccessFactors' sign-in (SAP's Gigya) calls its button "Submit": in a form that holds
    only the email and password, it's pressed like a "Sign In", and the application opens."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-submit.html") + "?pw=not-a-real-password", title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.log.count("pressed \u201cSubmit\u201d with your saved password") == 1, r.log
    assert r.seen_form and "signin" not in r.url, (r.url, r.reason, r.log)


def test_a_submit_beside_more_than_the_sign_in_is_left_to_the_person(srv, monkeypatch):
    """A "Submit" whose form asks for more than the email and password (a phone number) may
    send more than a sign-in: it's never pressed. The boxes are filled in for the person."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-submit.html") + "?extra&pw=not-a-real-password", title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => window.signInTries")
        finally:
            await applier.stop()

    r, tries = run(go())
    assert (r.need, tries) == ("sign_in", 0) and "Press its sign-in button" in r.reason, (r.reason, r.log)


@pytest.mark.parametrize("query", ["", "?questions"])
def test_an_apply_that_would_send_the_application_is_never_pressed(srv, monkeypatch, query):
    """A SuccessFactors application for someone signed in (live, Oct 2026): the profile shows greyed
    out, the job's questions below, and the button that sends it all is a plain "Apply". The desk
    pressed it three times, taking it for the way in. "Apply" after the desk has filled the page in,
    or on a page that holds an application, is the person's to press."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/sf-application-signed-in.html") + query, title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => window.applied")
        finally:
            await applier.stop()

    r, applied = run(go())
    assert (r.need, applied) == ("your_submit", 0), (r.status, r.need, r.reason, r.log)
    assert "\u201cApply\u201d, so it's yours to press" in r.reason


def test_an_application_the_person_submitted_on_the_site_is_marked_applied(srv, monkeypatch):
    """The desk filled a Workday application; the person pressed the site's own Submit, landed on
    its Candidate Home ("Application Submitted") and pressed Resume. The desk said the site had
    emailed a link to confirm the email (the page tells how to change it, with "Send Link"). A job
    the desk has filled that comes back on a page saying it went in is marked applied."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/workday-candidate-home.html"), title="Equipment Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        r.seen_form = True  # (filled before: the person pressed Resume)
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "submitted" and "application submitted" in r.reason, (r.status, r.need, r.reason, r.log)
    assert srv.tracker().get(job["id"])["status"] == "applied"


def test_a_confirmation_with_job_alerts_and_a_survey_is_marked_applied(srv, monkeypatch):
    """The person pressed the site's own Submit and then Resume, on a page that thanks them for applying
    and offers job alerts by email (its Email box required, its own Subscribe) and a voluntary survey
    (Continue). The desk took the required box and the Continue for a step of the form, and the job was
    never marked applied. Neither is the application's."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/applied-job-alerts.html"), title="Equipment Technician",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        r.seen_form = True  # (filled before: the person pressed Resume)
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "submitted" and "thank you for applying" in r.reason, (r.status, r.need, r.reason, r.log)
    assert srv.tracker().get(job["id"])["status"] == "applied"


def test_a_form_page_that_thanks_the_person_isnt_taken_for_a_confirmation(srv, monkeypatch):
    """A Workday site's Application Questions step (step 3 of 6) opens with "Thank you for your
    application. Please complete the below questions." The desk, back on the job after the person
    moved on in the browser, marked it Submitted (live, Oct 2026). A page with required boxes still
    empty, a button on to the next step, or a progress bar short of its last step is still the form."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/workday-questions-thank-you.html"), title="Equipment Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        r.seen_form = True  # (filled earlier steps: the person pressed Resume)
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status != "submitted" and r.need == "questions", (r.status, r.need, r.reason, r.log)
    assert [q["label"] for q in r.questions] == ["2) Have you ever held a security clearance with a foreign government?*"], \
        r.questions
    assert srv.tracker().get(job["id"])["status"] != "applied"


@pytest.mark.parametrize("query", ["", "?wipe", "?silent", "?textarea"])
def test_boxes_the_pages_errors_say_are_empty_though_filled_are_typed_again(srv, monkeypatch, query):
    """Nikon's UKG Pro (live run 10, Oct 11): its resume parse overwrote the page's own record of Address 1 and
    Zip a moment after the desk typed them, so Save and continue said "Address 1 Please enter an address."
    beside the filled boxes, and the desk stopped. Boxes the page's errors name, though they show a value, are
    typed again, once, and the step is pressed again. ?silent: EMD's Phenom (live check, Oct 11) lost boxes the
    same way and said nothing at all; its required boxes are typed again, once, all the same. ?textarea: a text area
    too (Microchip's Workday, run 84: its "Please specify your preferred work location." showed the answer, and the
    page said it was required and must have a value; only text boxes were typed again)."""
    from job_apply.desk import Desk

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 1)
    # (the page's resume parse lands 0.4 s after Address 1 is typed: the production wait after filling, 0.5 s, sees it
    # land before Save and continue is pressed, as UKG's did)
    monkeypatch.setattr(browser_module, "LOST_FILL_WAIT", 0.5)
    job = srv.add_job(url=fixture_url("site/ukg-prefill.html") + query, title="Field Service Engineer",
                      company="Example Fab")["job"]
    desk = Desk(srv)
    applier = desk.applier

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            if query == "?textarea" and r.need == "questions":  # (the person's answer, as at Microchip)
                desk.answer(job["id"], [{"label": q["label"], "value": "100 Main St", "remember": False}
                                        for q in r.questions])
                await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "review.html" in r.url, (r.url, r.reason, r.log)
    if query == "?silent":
        assert any(line.startswith("typed ") and "required box(es) again" in line for line in r.log), r.log
    elif query == "?wipe":  # (wiped: filled from the profile again, as in live run 12)
        assert any(line.startswith("filled again") for line in r.log), r.log
    else:
        assert any(line.startswith("typed again") and "Address 1" in line for line in r.log), r.log


@pytest.mark.parametrize("query", ["", "?kept", "?again", "?outside&again", "?confirm&again", "?toast&again"])
def test_a_resume_the_page_shows_but_never_kept_is_said_not_typed_around(srv, monkeypatch, query):
    """EMD's Phenom (issue #112, note 28): the page showed the resume as uploaded (its name, Download and Delete
    links), but the hidden fields it keeps the upload in stayed empty, so Next did nothing and said nothing. The desk
    pressed Next three times, typed the required boxes again, and stopped with "The page didn't move on". It deletes the
    file with its own Delete and uploads it again, once a job (?again: then it's kept, and Next goes on: EMD's, runs
    53 to 64, and ?outside: its page as it is). Still not kept, it says so (the upload, for the person to delete and
    do again) and doesn't type the boxes again; a Delete that asks first (?confirm: turned down, the file stays) is
    said as it stands, never as deleted, and one that goes leaving a note naming the file (?toast) is said as
    pressed (the reviews of 0.3.153). ?kept: the upload is kept, and Next goes on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 1)
    monkeypatch.setattr(pipeline, "NEW_TAB_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/phenom-resume-not-kept.html") + query, title="Equipment Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => sessionStorage.getItem('deletes')")
        finally:
            await applier.stop()

    r, deletes = run(go())
    again = [line for line in r.log if "hadn't kept it, so I deleted it and uploaded it again" in line]
    if query in ("?kept", "?again", "?outside&again"):
        assert "review.html" in r.url, (r.url, r.reason, r.log)
        assert len(again) == (query != "?kept"), r.log
        assert deletes == (None if query == "?kept" else "1"), deletes
        return
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    if query == "?toast&again":  # (the Delete went, but a note still names the file: nothing goes up over it)
        assert "I pressed its Delete to upload it again, but the page still shows the file's name" in r.reason \
            and deletes == "1" and not again, (r.reason, deletes, r.log)
        return
    assert "resume.pdf" in r.reason and "Delete link" in r.reason, r.reason
    if query:  # (the Delete turned down: nothing was deleted, and nothing is said to have been)
        assert "the resume didn't upload" in r.reason and "I deleted" not in r.reason and deletes is None, \
            (r.reason, deletes)
    else:
        assert "I deleted it and uploaded it again, and it still didn't upload" in r.reason and deletes == "1", \
            (r.reason, deletes)
    assert "page didn't move on" not in r.reason and not again, (r.reason, r.log)
    assert not any("required box(es) again" in line for line in r.log), r.log


def kept_resume_run(srv, query="", tailored="Sam_Rivera_Resume.pdf", letter=None, before=None, resume=False,
                    auto_submit=False, between=None):
    """A job on the Workday-like My Experience page whose "Resume/CV" upload keeps the account's last resume
    (2024-resume.pdf), with a resume tailored for it in its folder (`tailored`: its name, or None) and a cover letter
    (`letter`). `before`: the job's run as it was (Run's fields), and `resume`: gone on with by the person's Resume
    rather than queued; `between`: what the person does once it stops (a coroutine on the run), before it's looked at
    again. The run, and what the page counted (KEPT_COUNTED)."""
    job = srv.add_job(url=fixture_url("site/workday-kept-resume.html") + query, title="Equipment Technician",
                      company="Example Fab")["job"]
    folder = Path(srv.tracker().get(job["id"])["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    for name in (tailored, letter):
        if name:
            (folder / name).write_bytes(b"%PDF-1.4\n")
    applier = Applier(srv)
    applier.auto_submit = auto_submit
    if before is not None:
        applier.runs[job["id"]] = Run(job["id"], "Equipment Technician", "Example Fab", **before)

    async def go():
        applier.start()
        try:
            r = applier.resume(job["id"]) if resume else applier.enqueue(job["id"], submit=auto_submit)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            if between is not None:
                await between(applier, r)
                await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate(KEPT_COUNTED)
        finally:
            await applier.stop()

    return run(go())


# What the page counted: the kept file's Deletes pressed and gone, the files sent into the resume's box, and the same of
# the cover letter's; and the Deletes of what's never to be pressed
KEPT_COUNTED = ("() => Object.fromEntries(['presses', 'deletes', 'uploads', 'coverDeletes', 'plainDeletes', "
                "'letterPresses', 'letterDeletes', 'letterUploads']"
                ".map((k) => [k, JSON.parse(sessionStorage.getItem(k) || 'null')]))")
REPLACED = "replaced the resume kept from before with the one tailored for this job"


@pytest.mark.parametrize("query,tailored", [("", "Sam_Rivera_Resume.pdf"), ("?redraw", "Sam_Rivera_Resume.pdf"),
                                            ("?unnamed", "Sam_Rivera_Resume.pdf"), ("", "resume.pdf"),
                                            ("?kept=Sam_Rivera_Resume.pdf", "Sam_Rivera_Resume.pdf"),
                                            ("?toast", "Sam_Rivera_Resume.pdf")])
def test_a_resume_kept_from_before_gives_way_to_the_one_tailored_for_the_job(srv, monkeypatch, query, tailored):
    """NXP's Workday (the weekly probe, Oct 2026): an account that applied there before finds its last resume kept in
    My Experience's "Resume/CV" upload. The desk read the box as filled and left it, so the employer got the old resume,
    not the one tailored for the job. The kept file is deleted with its own Delete and the tailored one uploaded, said
    in the log; once a job, so a page that draws the old name again (?redraw: saved and drawn again) isn't deleted
    from again. A row that shows no name (?unnamed) can't say what it holds: replaced all the same, once. A tailored
    resume.pdf is no file the page shows already for being inside the kept file's name (2024-resume.pdf). The review of
    0.3.209: a kept file of the tailored one's own name (every tailored resume is <First>_<Last>_Resume.pdf, so the last
    application's at the same employer has it) is replaced as any other; and a Delete that leaves a note naming the
    file (?toast) has deleted it, its item gone."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    r, counted = kept_resume_run(srv, query, tailored)
    assert "review.html" in r.url, (r.url, r.reason, r.log)
    assert (counted["deletes"], counted["uploads"]) == (1, [tailored]), (counted, r.log)
    assert r.log.count(REPLACED) == 1, r.log
    # (?redraw: the page, drawn again with the old name, was gone through again)
    assert r.log.count("clicked “Save and Continue”") == (2 if query == "?redraw" else 1), r.log


@pytest.mark.parametrize("query,tailored", [("", None), ("?plain", "Sam_Rivera_Resume.pdf")])
def test_a_resume_kept_from_before_stays_with_none_tailored_for_the_job(srv, monkeypatch, query, tailored):
    """With no resume tailored for the job, a resume the site kept from before stays as it is (as the profile's
    would): nothing deleted, nothing uploaded beside it. So does one outside Workday's list of files (?plain, the review
    of 0.3.209: another site's list of stored files may be the person's own file library, where a Delete takes the file
    off their account), tailored resume or not, and nothing is said of it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    r, counted = kept_resume_run(srv, query, tailored)
    assert "review.html" in r.url, (r.url, r.reason, r.log)
    assert (counted["presses"], counted["deletes"], counted["plainDeletes"], counted["uploads"]) == (None,) * 4, \
        (counted, r.log)
    assert not any("kept from before" in line for line in r.log), r.log


@pytest.mark.parametrize("query", ["?stuck", "?cover", "?submit", "?rerender"])
def test_a_resume_kept_from_before_that_wont_go_is_the_persons(srv, monkeypatch, query):
    """The kept resume's Delete asks first (?stuck: turned down), or the box keeps a cover letter beside it (?cover:
    which is the old resume isn't sure), or its Delete would send the form (?submit), or draws the list again with the
    file still in it (?rerender, the review of 0.3.209: its Delete gone isn't the file gone): the tailored resume is
    never uploaded beside the old one (the employer would get two). The person's, on a card saying what to do; nothing
    deleted, nothing sent."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    r, counted = kept_resume_run(srv, query)
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert (counted["deletes"], counted["uploads"], counted["coverDeletes"]) == (None,) * 3, (counted, r.log)
    assert counted["presses"] == (1 if query in ("?stuck", "?rerender") else None), counted
    assert run(r.page.evaluate("() => window.sent")) is False
    assert "Sam_Rivera_Resume.pdf" in r.reason and "then press Carry on" in r.reason, r.reason
    if query == "?cover":
        assert "files kept from before (2024-resume.pdf, 2024-cover-letter.pdf)" in r.reason \
            and "which is your old resume" in r.reason, r.reason
    else:
        assert "still has the resume kept from before (2024-resume.pdf), and I couldn't delete it" in r.reason \
            and "two resumes" in r.reason, r.reason
    assert REPLACED not in r.log, r.log


def test_a_tailored_resume_the_fill_didnt_send_is_never_said_replaced(srv, monkeypatch):
    """The review of 0.3.209: the kept file deleted, the fill found the tailored file's name on the page (a note naming
    the deleted Old_Sam_Rivera_Resume.pdf holds Sam_Rivera_Resume.pdf) and sent nothing, yet the log said "replaced".
    Only a file the fill sent itself is: here, the box is the person's to put it in, on a card."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    r, counted = kept_resume_run(srv, "?toast&kept=Old_Sam_Rivera_Resume.pdf")
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert (counted["deletes"], counted["uploads"]) == (1, None), (counted, r.log)
    assert "I deleted the resume Example Fab's site kept from before (Old_Sam_Rivera_Resume.pdf) to put in the one " \
        "tailored for this job, and it didn't go in" in r.reason, r.reason
    assert REPLACED not in r.log, r.log


@pytest.mark.parametrize("how", ["resume", "restart", "pressed before", "carried on"])
def test_a_file_in_an_application_the_person_has_had_is_theirs(srv, monkeypatch, how):
    """The review of 0.3.209: once the person has had the application in their hands (their Resume after a stop, or
    after a restart of the desk, whose job goes on only by it; or a Submit pressed before), a file in its upload may be
    one they put there themselves (My_Own_Pick_final.pdf). It's never deleted, nor anything uploaded beside it: left as
    it is, and said. So is one the desk went on with by itself after they may have worked in its tab (Run.in_their_hands,
    the re-review: Submit for me sent the tailored file in place of theirs)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    before = {"resume": {"status": "needs_you", "need": "stuck"},
              "restart": {"status": "needs_you", "need": "restarted", "left_at_restart": True, "started": True},
              "pressed before": {"pressed_before": True},
              "carried on": {"status": "needs_you", "need": "bot_check", "left": True, "started": True,
                             "in_their_hands": True}}[how]
    r, counted = kept_resume_run(srv, "?kept=My_Own_Pick_final.pdf", before=before,
                                 resume=how in ("resume", "restart"), auto_submit=how == "carried on")
    assert "review.html" in r.url, (r.url, r.reason, r.log)
    assert (counted["presses"], counted["deletes"], counted["uploads"]) == (None,) * 3, (counted, r.log)
    assert r.log.count("left the resume in its “Resume/CV” upload as it is (My_Own_Pick_final.pdf), not the one "
                       "tailored for this job: you've had this application in your hands, so what's in it is yours") \
        == 1, r.log
    assert REPLACED not in r.log, r.log


def test_the_desk_going_on_with_a_job_the_person_may_have_worked_in_says_so(srv, monkeypatch):
    """The re-review of 0.3.209: a job the desk goes on with, begun, without the person's Resume, where they may have
    worked in its tab meanwhile, is marked so (Run.in_their_hands), and a file in its upload stays theirs: the queue
    picking it up by itself past its pause, their Undo of its Skip, Apply on it again. A job never begun, or queued
    afresh, isn't."""
    from types import SimpleNamespace

    from job_apply.desk import Desk

    jobs = [srv.add_job(url=f"https://{n}.example/job", title="FSE", company=n.title())["job"]["id"] for n in "abcde"]
    applier = Applier(srv)
    monkeypatch.setattr(srv.browser, "lost", lambda tab: "")

    async def past(r):
        return True

    applier._past_pause = past
    tab = SimpleNamespace(is_closed=lambda: False, url="https://a.example/apply")
    left = applier.runs[jobs[0]] = Run(jobs[0], "FSE", "A", status="needs_you", need="bot_check", left=True, page=tab,
                                       started=True)
    run(applier._pick_up_left())
    assert left.status == "queued" and left.in_their_hands, (left.status, left.log)

    for job_id in jobs[1:3]:
        applier.enqueue(job_id)
    applier.runs[jobs[1]].started = True  # (begun: its tab was theirs before the Skip)
    for job_id in jobs[1:3]:
        run(applier.skip(job_id))
        run(applier.unskip(job_id))
    assert [applier.runs[j].in_their_hands for j in jobs[1:3]] == [True, False]

    desk = Desk(srv)
    desk.applier = applier
    applier.runs[jobs[3]] = Run(jobs[3], "FSE", "D", status="needs_you", need="stuck", started=True)
    assert desk.apply(urls=[], job_ids=[jobs[3], jobs[4]], submit=False)[0] == [jobs[3], jobs[4]]
    assert [applier.runs[j].in_their_hands for j in jobs[3:]] == [True, False]

    # (the re-review: a questions card answered on the desk, its tab the person's while the card was up)
    asked = srv.add_job(url="https://f.example/job", title="FSE", company="F")["job"]["id"]
    q = applier.runs[asked] = Run(asked, "FSE", "F", status="needs_you", need="questions", started=True,
                                  questions=[{"label": "Shift preference?", "kind": "text", "id": "q1"}])
    desk.answer(asked, [{"label": "Shift preference?", "value": "Days", "remember": False}])
    assert q.in_their_hands and q.status == "queued", (q.status, q.log)


@pytest.mark.parametrize("slow", [1, 3])
def test_a_kept_files_box_that_reads_slowly_is_never_taken_for_one_gone(srv, monkeypatch, slow):
    """The re-review of 0.3.209: the box's read timing out on a loaded machine was taken for the box gone, so the kept
    resume stayed, unsettled, and Submit for me sent it (the likely flake of the draft test). A read that answers on a
    later try replaces it as ever; one that never answers is the person's, on a card: nothing deleted, nothing sent."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    real, tries = srv.browser._locator, []

    def locator(page, box_id):
        loc = real(page, box_id)

        class Slow:
            def __getattr__(self, name):
                return getattr(loc, name)

            async def evaluate(self, script, *args, **kwargs):
                if script == formjs.KEPT_STATE_JS and len(tries) < slow:
                    tries.append(1)
                    raise PlaywrightTimeout("Timeout 3000ms exceeded.")
                return await loc.evaluate(script, *args, **kwargs)

        return Slow()

    monkeypatch.setattr(srv.browser, "_locator", locator)
    r, counted = kept_resume_run(srv, auto_submit=True)
    if slow < 3:
        assert (r.status, counted["deletes"], counted["uploads"]) == ("submitted", 1, ["Sam_Rivera_Resume.pdf"]), \
            (r.status, r.reason, counted, r.log)
        assert REPLACED in r.log, r.log
    else:
        assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
        assert (counted["presses"], counted["deletes"], counted["uploads"]) == (None,) * 3, (counted, r.log)
        assert "the page was too slow for me to read it" in r.reason and "Sam_Rivera_Resume.pdf" in r.reason, r.reason
        assert run(r.page.evaluate("() => window.sent")) is False


@pytest.mark.parametrize("checked", [True, False])
def test_a_drafts_resume_replaced_holds_submit_for_me(srv, monkeypatch, checked):
    """The review of 0.3.209: a saved draft's resume replaced with the tailored one is a change to its answers, whether
    they were being re-checked (`checked`) or not (a stop where the person may have changed its boxes, the queue gone on
    by itself): Submit for me leaves it on its review page for the person to check, and the job's "What went in" says
    what changed. The re-check says nothing of the box replaced ("they match your profile" of a page whose only answer
    the desk replaced)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    r, counted = kept_resume_run(srv, before={"draft": True, "draft_off": not checked}, auto_submit=True)
    assert (r.status, counted["deletes"], counted["uploads"]) == ("ready", 1, ["Sam_Rivera_Resume.pdf"]), \
        (r.status, r.reason, counted, r.log)
    assert REPLACED in r.log and r.draft_changed, r.log
    assert "Submit for me left it for you" in r.reason, r.reason
    assert any("2024-resume.pdf" in line for line in r.public()["went_in"]["changed"]), r.public()["went_in"]
    assert not any("they match your profile" in line for line in r.log), r.log


def test_a_cover_letter_kept_from_before_gives_way_to_the_one_tailored_for_the_job(srv, monkeypatch):
    """The review of 0.3.209, end to end: the job's folder holds a resume and a cover letter tailored for it, and
    My Experience keeps a file from before in each upload: each is deleted with its own Delete and the tailored one
    sent into its own box."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    r, counted = kept_resume_run(srv, "?letter", letter="Sam_Rivera_Cover_Letter.pdf")
    assert "review.html" in r.url, (r.url, r.reason, r.log)
    assert ({k: counted[k] for k in ("deletes", "uploads", "letterDeletes", "letterUploads")}
            == {"deletes": 1, "uploads": ["Sam_Rivera_Resume.pdf"], "letterDeletes": 1,
                "letterUploads": ["Sam_Rivera_Cover_Letter.pdf"]}), (counted, r.log)
    assert REPLACED in r.log, r.log
    assert "replaced the cover letter kept from before with the one tailored for this job" in r.log, r.log


def test_a_second_kept_file_that_wont_go_is_said_once_the_first_is_past(srv, monkeypatch):
    """The review of 0.3.209: two kept files whose Deletes won't go (a resume's and a cover letter's, each asking
    first). The card says the resume's; the cover letter's Delete is neither pressed nor its box settled, so the next
    pass says it: after the person's Resume, it's theirs (left as it is, and said)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    seen = {}

    async def on_resume(applier, r):
        seen.update(need=r.need, reason=r.reason, settled=set(r.kept_replaced),
                    letter=await r.page.evaluate("() => sessionStorage.getItem('letterPresses')"))
        applier.resume(r.job_id)

    r, counted = kept_resume_run(srv, "?letter&stuck&letterstuck", letter="Sam_Rivera_Cover_Letter.pdf",
                                 between=on_resume)
    assert seen["need"] == "stuck" and "still has the resume kept from before" in seen["reason"], seen
    assert (seen["letter"], seen["settled"]) == (None, {"resume"}), seen
    assert "review.html" in r.url, (r.url, r.reason, r.log)
    assert "left the cover letter in its “Cover Letter” upload as it is (2024-cover-letter.pdf), not the one tailored " \
        "for this job: you've had this application in your hands, so what's in it is yours" in r.log, r.log
    assert (counted["letterPresses"], counted["letterUploads"], counted["uploads"]) == (None,) * 3, counted


def test_a_box_the_page_says_is_wrong_showing_what_it_wrote_itself_takes_the_profiles_answer(srv, monkeypatch):
    """EMD Electronics' Phenom (live run 41, Oct 12): its resume parse wrote the phone as "4805550199" in a box
    asking for "+1##########" with no spaces or dashes, and Next called it invalid. The desk had left the box (it
    showed a value), then typed the page's own digits again, and stopped. A text box the errors name, showing
    something other than the profile's answer, takes the profile's answer, once a page."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/phenom-parsed-phone.html"), title="Equipment Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "review.html" in r.url, (r.url, r.reason, r.log)
    assert any(line.startswith("filled from your profile what the page said was wrong: Phone Number")
               for line in r.log), r.log

    # (the review's) a box an error names while asking for something else, that the page doesn't mark invalid,
    # keeps what it shows
    job = srv.add_job(url=fixture_url("site/phenom-parsed-phone.html") + "?valid", title="Equipment Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def held():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => document.getElementById('phoneNumber').value")
        finally:
            await applier.stop()

    r, shown = run(held())
    assert r.status == "needs_you", (r.status, r.reason, r.log)
    assert not any(line.startswith("filled from your profile") for line in r.log), r.log
    assert shown == "+14805550000", r.log


def test_a_step_waiting_on_a_tax_credit_screening_says_so_and_leaves_it_to_the_person(srv, monkeypatch):
    """Kforce's Taleo (live run 41, Oct 12): Save and Continue wouldn't go on until the applicant completed its tax
    credit screening questionnaire ("Complete Tax Credit Screening Questionnaire"), and the desk stopped with the page's
    raw error list. The screening is questions about the person: the desk says so, names its link, and leaves it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/taleo-tax-credit.html"), title="Client Specialist",
                      company="Example Staffing")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "needs_you" and r.need == "stuck", (r.status, r.reason, r.log)
    assert "tax credit screening" in r.reason and "Complete Tax Credit Screening Questionnaire" in r.reason, r.reason
    # (the review's) what else the page says is said too
    assert "the page also says: Please enter a valid start date for your last job" in r.reason, r.reason


def test_a_resume_parse_notice_is_not_an_error(srv, monkeypatch):
    """Nikon's UKG Pro (live run 16, Oct 11): its Skills step took a moment to save, with the resume parse's
    notice ("... has been used to pre-fill part of your application. Please verify that all of your information
    is correct.") showing, and the desk stopped at once on it ("The page didn't move on"). The notice asks
    nothing to be fixed, so the step is looked at again."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/ukg-prefill.html") + "?slow", title="Field Service Engineer",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "review.html" in r.url, (r.url, r.reason, r.log)
    assert not pipeline._errorish("x.pdf has been used to pre-fill part of your application. Please verify that "
                                  "all of your information is correct.")
    assert pipeline._errorish("Address 1 Please enter an address.")
    # (a real error beside the notice, in the same message, still counts)
    assert pipeline._errorish("x.pdf has been used to pre-fill part of your application. Please enter a valid "
                              "phone number.")


@pytest.mark.parametrize("query", ["", "?quiet"])
@pytest.mark.real_waits  # (the step saves for longer than the tests' shortened STEP_SAVE_WAIT: the wait itself is the point)
def test_a_step_still_saving_after_three_presses_is_waited_on(srv, monkeypatch, query):
    """KLA's Workday (live run 11, Oct 11): every box of Application Questions 2 of 2 answered, its Save and
    Continue took a while to go on, and the desk stopped after its presses ("The page didn't move on"); the next
    step was there a little later. A step still saving, with nothing said wrong, is waited on once. ?quiet: the
    page shows nothing while it saves, so two presses in a row seem to do nothing (live run 16)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/workday-slow-save.html") + query, title="Customer Service Engineer",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        r.seen_form = True
        r.once["are you willing to travel"] = "Yes"
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), timeout=90, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready", (r.status, r.need, r.reason, r.log)


@pytest.mark.parametrize("query", ["", "?bare", "?bare&late"])
def test_a_step_still_loading_its_questions_is_waited_for(srv, monkeypatch, query):
    """KLA's Workday (live, Oct 2026): past My Experience, its Application Questions step showed its
    loading dots where the questions go and a greyed-out Save and Continue the form reader found no
    button for, beside the previous step's resume box, and the desk stopped at once: "I couldn't find
    the button that moves this application on." A step still loading is waited for. In live run 9 (bare)
    it showed only the resume box, no dots and no button at all: a form with no button yet is waited on too."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/workday-questions-loading.html") + query, title="Customer Service Engineer",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        r.seen_form = True  # (filled My Information and My Experience)
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready", (r.status, r.need, r.reason, r.log)
    assert any("clicked “Save and Continue”" in line for line in r.log), r.log


@pytest.mark.parametrize("query", ["", "?heal", "?late=1500", "?dots", "?dots&heal"])
def test_a_sign_in_step_still_loading_is_reloaded_once_then_said_not_taken_for_one_to_sign_in_at(srv, monkeypatch, query):
    """Baker Tilly's Workday (run 62; run 61 went to its review page): Apply Manually opened its "Create Account/Sign
    In" step with Workday's whole-step loading skeleton (applyFlowLoadingPage) and only the header's Sign In, and the
    desk said "Sign in (or create your account) on Workday in the browser window". A step still loading is waited on,
    reloaded once (heal: then it draws and goes on), and otherwise said not to have finished loading, as the sign-in
    step it is (the person may sign in there, and the desk carries on by itself). One that draws a moment later (late)
    is waited on, never reloaded. Grand Canyon University's (run 69, "dots"): the step's body only Workday's loading
    dots, with no automation id, read as a step still loading too."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "SIGN_IN_STEP_WAIT", 1)
    monkeypatch.setattr(pipeline, "STEP_LOAD_WAIT", 2)
    job = srv.add_job(url=fixture_url("site/workday-signin-loading.html") + query, title="HR Business Partner",
                      company="Example Advisors")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    reloaded = any("was still loading" in line and "reloaded it" in line for line in r.log)
    assert reloaded == (query != "?late=1500"), (r.reason, r.log)
    if "heal" in query or "late" in query:
        assert r.status == "ready", (r.status, r.need, r.reason, r.log)
    else:
        assert (r.status, r.need) == ("needs_you", "sign_in") and "hasn't finished loading" in r.reason, (r.reason, r.log)
        assert "its Sign In at the top" in r.reason, r.reason


def test_a_resume_from_a_stop_is_never_followed_by_a_reload_of_the_persons_page(srv):
    """The review of 0.3.147: a page still loading after the person's own Resume from a stop (what they typed there by
    hand) is said, never reloaded away. A Resume from a questions card is the person's hands on the page too (the review
    of Oct 15): its run is marked so, and test_a_loading_step_after_a_questions_resume_is_said_not_reloaded drives it."""
    applier = Applier(srv)
    job = srv.add_job(url=fixture_url("site/workday-sections-loading.html"), title="Clerk", company="Example Bank")["job"]
    r = applier.enqueue(job["id"])
    r.status, r.need = "needs_you", "stuck"
    assert applier.resume(job["id"]).load_reloaded
    other = srv.add_job(url=fixture_url("site/workday-sections-loading.html") + "?b", title="Clerk 2",
                        company="Example Bank")["job"]
    q = applier.enqueue(other["id"])
    q.status, q.need = "needs_you", "questions"
    assert applier.resume(other["id"]).hands_on


def test_a_loading_step_after_a_questions_resume_is_said_not_reloaded(srv, monkeypatch):
    """The review of Oct 15: the person answered a questions card by typing into the page itself (the card's Resume is
    for that too), and the desk, landing on a step still loading, reloaded it, and what they typed was gone. After any
    Resume of the person's, a page still loading is said, as after a stop's."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "STEP_LOAD_WAIT", 2)
    job = srv.add_job(url=fixture_url("site/workday-sections-loading.html"), title="Branch Coordinator",
                      company="Example Bank")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        r.status, r.need = "needs_you", "questions"
        applier.resume(job["id"])
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert not any("reloaded it" in line for line in r.log), r.log
    assert (r.status, r.need) == ("needs_you", "stuck") and "didn't finish loading" in r.reason, (r.reason, r.log)
    assert "after a wait," in r.reason and "a reload" not in r.reason.split("so there's")[0], r.reason  # (none happened)


@pytest.mark.parametrize("query", ["", "?heal"])
def test_a_step_whose_sections_never_finish_loading_is_reloaded_once_then_said(srv, monkeypatch, query):
    """Wells Fargo's and Hitachi's Workday (run 57, Oct 13): My Information right after the account's sign-in drew its
    Country and names, but its Address, Email Address and Phone stayed Workday's loading placeholders and its Save and
    Continue greyed out, and the desk said "I couldn't find the button that moves this application on". A step still
    loading is waited on, reloaded once (heal: then it draws the rest and goes on), and otherwise said for what it is."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "STEP_LOAD_WAIT", 2)
    job = srv.add_job(url=fixture_url("site/workday-sections-loading.html") + query, title="Branch Coordinator",
                      company="Example Bank")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert any("still loading" in line and "reloaded" in line for line in r.log), r.log
    if query:
        assert r.status == "ready", (r.status, r.need, r.reason, r.log)
    else:
        assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.need, r.reason, r.log)
        assert "didn't finish loading" in r.reason and "couldn't find the button" not in r.reason, r.reason


@pytest.mark.parametrize("query", ["", "?heal", "?wrappers", "?wrappers&heal"])
def test_a_workday_application_frame_left_empty_is_reloaded_once_then_said(srv, monkeypatch, query):
    """Grand Canyon University's Workday (run 91): right after the account's sign-in, its application frame (#mainContent,
    data-mfe-id="applyFlow") stayed empty between the site's header and footer, no step, no loading dots, and the desk
    said "I couldn't find the button that moves this application on". An application frame with nothing in it is a
    step not drawn yet: waited on, reloaded once (heal: then it draws and goes on), and otherwise said as the page
    that didn't finish loading. Baker Tilly's after Apply Manually (run 117) held three empty boxes, no words and
    nothing to press (wrappers): the same, and the desk said the same "couldn't find the button"."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "STEP_LOAD_WAIT", 2)
    job = srv.add_job(url=fixture_url("site/workday-apply-unmounted.html") + query, title="Senior HR Generalist",
                      company="Example University")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert any("still loading" in line and "reloaded" in line for line in r.log), (r.reason, r.log)
    if "heal" in query:
        assert r.status == "ready", (r.status, r.need, r.reason, r.log)
    else:
        assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.need, r.reason, r.log)
        assert "didn't finish loading" in r.reason and "never came up between the site's header and footer" in r.reason \
            and "couldn't find the button" not in r.reason, r.reason


def test_an_answer_the_site_lost_is_put_back_on_its_own_step_with_workdays_errors_found(srv, monkeypatch):
    """Robert Half's Workday (run 59): the person's answers to its compensation and non-compete lists went in, and Next
    came back to the same step with "Errors Found" over it and the compensation list on "Select One" again. Each answer
    is kept to the page it first went in on, and the heading made the step another page: the answer was never put
    back, and the question was asked round after round. The step is the same step with its errors shown."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/workday-answers-lost.html"), title="Talent Manager",
                      company="Example Staffing")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        r.seen_form = True  # (past My Information and My Experience)
        r.once[pipeline.question_key("What is your desired Compensation?*")] = "Negotiable"
        r.once[pipeline.question_key("Have you ever entered in to a non-compete agreement or any other agreement to "
                                     "restrict your employment?*")] = "No"
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready", (r.status, r.need, r.reason, [q.get("label") for q in r.questions], r.log)


def test_a_first_step_that_thanks_the_person_isnt_taken_for_a_confirmation(srv, monkeypatch):
    """A careers site's first step thanks the person ("Thanks for applying to Acme!") over its required
    First Name and Email and their Next, and its footer asks them to join its talent community. Back on
    the job before pressing anything, the desk took the two boxes for the talent community's and marked
    the job applied: an application never sent, and never filled in again. A Next beside them is the
    form's, and so are they."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme.example/apply/1"
    first = ('<html><head><title>Acme application</title></head><body><h1>Thanks for applying to Acme!</h1>'
             '<p>Tell us who you are to get started.</p><form method="post" action="/apply/2">'
             '<label for="fn">First Name *</label><input id="fn" name="fn" required>'
             '<label for="em">Email *</label><input id="em" name="em" type="email" required>'
             '<button type="submit">Next</button></form><footer><h2>Join our talent community</h2>'
             '<button type="button">Subscribe</button></footer></body></html>')
    pages = {url: first,
             ("POST", "https://careers.acme.example/apply/2"): _form("Acme", "Do you hold an active TS/SCI clearance?",
                                                                    button="Next")}
    posts: list[str] = []
    job = srv.add_job(url=url, title="Field Service Engineer", company="Acme")["job"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, posts)
        r = applier.enqueue(job["id"])
        r.seen_form = True  # (filled before: the person pressed Resume)
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status != "submitted" and r.need == "questions", (r.status, r.need, r.reason, r.log)
    assert posts == ["https://careers.acme.example/apply/2"]  # its first step filled in and sent on
    assert srv.tracker().get(job["id"])["status"] != "applied"


_FIRST_STEP = [{"kind": "text", "label": "First name*", "required": True, "value": ""},
               {"kind": "text", "label": "Email*", "required": True, "value": "", "input_type": "email"}]


@pytest.mark.parametrize("data, text, mid", [
    ({"fields": [{"kind": "select", "label": "Q", "required": True, "value": ""}], "actions": []}, "", True),
    ({"fields": [], "actions": [{"text": "Save and Continue"}]}, "", True),
    ({"fields": [], "actions": [], "headings": ["current step 3 of 6 Application Questions"]}, "", True),
    ({"fields": [], "actions": [], "headings": ["current step 6 of 6 Review"]}, "", False),
    ({"fields": [], "actions": [{"text": "Search for More Jobs"}, {"text": "Return to Home"}],
      "headings": ["Application Submitted"]}, "", False),
    # after the site's own Submit: job alerts (a required Email, its Subscribe), or on to a voluntary survey
    ({"fields": [{"kind": "text", "label": "Email*", "required": True, "value": ""}], "actions": [{"text": "Subscribe"}],
      "headings": ["Thank you for applying!", "Get job alerts"]}, "", False),
    ({"fields": [], "actions": [{"text": "Continue"}], "headings": ["Thank you for applying!"]}, "", False),
    ({"fields": [{"kind": "checkbox", "label": "Join our talent community", "required": True, "value": False},
                {"kind": "text", "label": "Email Address", "required": True, "value": ""}],
      "actions": [{"text": "Continue"}, {"text": "Submit", "is_submit": True, "aside": True}]}, "", False),
    # both (applied-job-alerts.html as the form reader reads it): the survey's Continue is in no form, and
    # the alerts' Subscribe is its own form's
    ({"fields": [{"kind": "text", "label": "Email*", "required": True, "value": "", "input_type": "email"}],
      "actions": [{"text": "Continue"}, {"text": "Subscribe", "form_submit": True}],
      "headings": ["Thank you for applying!", "Get job alerts"]},
     "Thank you for applying! We have your application for Equipment Technician at Example Corp. Our recruiters "
     "will be in touch. Help us improve: tell us how applying went in a two-minute voluntary survey. Continue",
     False),
    # but an Email nothing says is the alerts', and a step button beside the application's boxes, are the form's
    ({"fields": [{"kind": "text", "label": "Email*", "required": True, "value": ""}], "actions": [{"text": "Continue"}],
      "headings": ["Thank you for applying!"]}, "", True),
    ({"fields": [{"kind": "text", "label": "First Name", "required": True, "value": "Sam"}], "actions": [{"text": "Next"}],
      "headings": ["Thank you for applying!"]}, "", True),
    # a first step that thanks the person, its name and email beside a Next, with a talent community or job
    # alerts sign-up in the footer: a Next isn't a sign-up's (its button is a Subscribe of its own), and
    # once the sign-up's box is told by its words, the others are the application's
    ({"fields": _FIRST_STEP, "actions": [{"text": "Next"}, {"text": "Subscribe"}],
      "headings": ["Thanks for applying to Acme!", "Join our talent community"]}, "", True),
    ({"fields": _FIRST_STEP, "actions": [{"text": "Next", "disabled": True}],  # (greyed out until they're filled in)
      "headings": ["Thanks for applying to Acme!", "Join our talent community"]}, "", True),
    ({"fields": _FIRST_STEP, "actions": [{"text": "Next", "form_submit": True},
                                         {"text": "Submit", "is_submit": True, "form_submit": True, "aside": True}],
      "headings": ["Thanks for applying to Acme!"]}, "", True),
    ({"fields": [*_FIRST_STEP, {"kind": "text", "label": "Get job alerts by email", "required": False, "value": ""}],
      "actions": [{"text": "Next"}, {"text": "Submit", "is_submit": True, "form_submit": True, "aside": True}],
      "headings": ["Thanks for applying to Acme!"]}, "", True),
    # a step that thanks the person and asks them to go on, its questions on the next step (or in a frame)
    ({"fields": [], "actions": [{"text": "Continue"}], "headings": ["Thank you for your application"]},
     "Thank you for your application. Please continue to the questions. Continue", True),
    ({"fields": [], "actions": [], "headings": ["Application Questions"]},
     "Application Questions\n\nThank you for your application. Please complete the below questions.", True),
    # but going on to more jobs is what a page says once the application went
    ({"fields": [], "actions": [], "headings": ["Thank you"]},
     "Thank you for applying! Please continue to browse our open positions.", False),
    ({"fields": [], "actions": [], "headings": ["Thank you"]},
     "Your application has been submitted. Please continue to explore other opportunities with us.", False),
    # and an application's own voluntary or optional steps are still the application
    ({"fields": [], "actions": [], "headings": ["Voluntary Disclosures"]},
     "Thank you for your application. Please complete the voluntary self-identification questions below.", True),
    ({"fields": [], "actions": [], "headings": ["Additional Questions"]},
     "Thank you for your application. Please complete the optional questions below.", True),
])
def test_what_says_a_page_is_partway_through_the_form(data, text, mid):
    assert pipeline._mid_application(data, text) is mid


def watched_inbox(monkeypatch, reset_link=None):
    """An email app password on the desk, and an inbox that holds `reset_link` (or nothing)."""
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        if want == "reset" and reset_link:
            return pipeline.mailbox.Found(want, reset_link, "careers.example.com", time.time())
        return None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)


@pytest.mark.slow
def test_a_reset_the_site_has_no_account_for_makes_one_instead(srv, monkeypatch, job_apply_home):
    """The password reset says there's no account for the email: back on the sign-in page, the
    desk takes its way to a new account ("Don't have an account yet?", SuccessFactors') and
    makes one with the saved password."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-submit.html") + "?unknown", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert "asked it to email a password reset" not in log and "has no account for your email" in log, log
    assert "created your account" in log and r.seen_form, (r.reason, r.log)


@pytest.mark.slow
@pytest.mark.parametrize("at_once", [1, 2])
def test_no_reset_email_in_a_few_minutes_makes_an_account_and_the_queue_goes_on(srv, monkeypatch, job_apply_home,
                                                                               at_once):
    """The site says it emailed a reset (most say so whether or not the email has an account),
    and nothing comes: the other jobs go ahead meanwhile, and after RESET_MAIL_WAIT the desk goes
    back to the sign-in page and makes an account. With two at once, the other job signs in with
    the saved password alongside the wait."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "RESET_MAIL_WAIT", 4)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-submit.html"), title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html" if at_once == 1 else "site/posting.html"), title="Tech",
                        company="Example Litho")["job"]
    applier = Applier(srv)
    applier.jobs_at_once = at_once

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you", about=state(r))
            assert r.need == "email_code" and "makes one instead" in r.reason and not r.blocking, (r.reason, r.log)
            r2 = applier.enqueue(other["id"])
            await until(lambda: r2.status not in ("queued", "running"), about=state(r2))  # not held by the wait
            await until(lambda: r.status not in ("queued", "running") and r.need != "email_code", timeout=60,
                        about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert "asked it to email a password reset" in log and "no password reset email came" in log, log
    assert "created your account" in log and r.seen_form, (r.reason, r.log)
    assert sum(line.startswith("pressed \u201cSubmit\u201d") for line in r.log) == 1, r.log  # not tried again


def test_a_reset_the_desk_cant_fill_in_makes_an_account_instead(srv, monkeypatch, job_apply_home):
    """The saved password didn't sign in, and the site's password reset isn't one the desk can fill
    in (it asks a security question: the person's). UKG Pro's, SuccessFactors' and Infor's stopped the
    test identity, which had no account at any of them (live, Oct 2026). With no reset to be had, the
    desk goes back to the sign-in page and makes an account (a site that has one for the email says
    so, and that's the person's)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-submit.html") + "?reset=odd", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert "asked it to email a password reset" not in log, log
    assert "isn't one I can fill in, so I went back to make an account" in log, log
    assert "created your account" in log and r.seen_form, (r.reason, r.log)


async def brassring_did(page):
    """What the BrassRing-like page (site/brassring-signin.html) has seen in this tab."""
    return await page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage).filter(([k]) => "
                               "k.startsWith('br.')).map(([k, v]) => [k.slice(3), JSON.parse(v)]))")


def test_an_email_first_account_step_that_stays_is_pressed_once(srv, monkeypatch, job_apply_home):
    """BrassRing's Create Account asks for the email first. Where its Continue refuses the email and the
    step stays, the desk said it had gone on (the step has no view of an Eightfold site's), came back to
    it and pressed Continue again on each look, each one emailing a passcode. It's pressed once, and
    what the site said is the person's."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "ACCOUNT_STEP_WAIT", 2)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=fixture_url("site/brassring-signin.html") + "?refuse", title="Senior Analyst",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await brassring_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    assert did.get("accountPresses") == 1, (did, r.log)
    assert r.need == "sign_in" and "Please enter a valid email address" in r.reason, (r.reason, r.log)


@pytest.mark.parametrize("who", ["on", "captcha", "off", "practice"])
def test_a_reset_with_a_captcha_makes_the_account_instead(srv, monkeypatch, job_apply_home, who):
    """Edward Jones' BrassRing (live, Oct 2026): the saved password didn't sign in, and its password
    reset asks for a picture code ("Enter Captcha"). The desk filled in the email, pressed the reset's
    Continue without the code, and stopped on "One or more fields require your attention. Captcha text
    Required field". A CAPTCHA is the person's, so that's a reset the desk can't do: with
    manage_accounts it goes back to the sign-in page (drawn in place at the posting's own address, so
    the page is loaded again: going to that address left the reset on show) and makes the account
    through "Don't have an account yet?", whose first step takes the email (its Continue emails a
    passcode there). The reset's CAPTCHA is never touched, nor its Continue pressed. A CAPTCHA on
    Create Account too ("captcha") is the person's, and the pause says so. Without manage_accounts,
    or in practice mode, no reset is asked for, and Create Account's Continue, which begins the
    account, is the person's (the desk had pressed it, taking it for an application's step)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    if who in ("on", "captcha"):
        manage_accounts(job_apply_home)
    elif who == "practice":
        manage_accounts(job_apply_home, submit_mode="dry_run")
    watched_inbox(monkeypatch)
    url = fixture_url("site/brassring-signin.html") + ("?captcha" if who == "captcha" else "")
    job = srv.add_job(url=url, title="Senior Analyst", company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await brassring_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    log = "\n".join(r.log)
    assert "captcha" not in did and "resetPresses" not in did, (did, r.log)  # no CAPTCHA typed in, nor reset sent
    assert did.get("signIns") == 1, (did, r.log)  # the saved password, once
    if who in ("off", "practice"):
        assert "password reset" not in log and "accountEmail" not in did, (did, r.log)
        assert r.need == "sign_in" and "Making the account is yours" in r.reason, (r.reason, r.log)
        return
    assert "asked it to email a password reset" not in log, log
    assert ("I opened its password reset, which has a CAPTCHA (yours to solve), so I went back to make an account "
            "there instead") in log, log
    if who == "captcha":
        assert "accountEmail" not in did, (did, r.log)
        assert r.need == "sign_in" and "with a CAPTCHA that's yours to solve" in r.reason, (r.reason, r.log)
        return
    assert did.get("accountEmail") == "sam.rivera@example.com", (did, r.log)
    assert "gave Example Corp's site your email for the new account and pressed “Continue”" in log, log
    assert r.need == "email_code", (r.reason, r.log)  # its passcode: the inbox's, or the person's


@pytest.mark.parametrize("query", ["", "?stay", "?keys"])
def test_brassrings_six_box_passcode_is_entered_from_the_inbox_and_verified(srv, monkeypatch, job_apply_home, query):
    """Edward Jones' BrassRing (the live check's test identity, Oct 2026): its Create Account emails a
    passcode and asks for it in six boxes ("Enter 1st digit of your passcode" ... "Enter 6th digit of
    your passcode") and a "Verify Passcode" button. The desk paused for it and watched the inbox, but
    "Verify Passcode" isn't one of the buttons it presses after a code (Verify, Confirm, Continue, Next,
    each with "code", "email" or "account"), so the digits went in and the person had to press it. The
    code goes in a digit per box and Verify Passcode is pressed (and the log says the mail was in Spam, where
    BrassRing's page says to look, when it was). Live (?stay), the passcode step is drawn below the email
    step, whose Continue stays: the desk pressed that, which sent another passcode and emptied the boxes.
    A button naming the code ("Verify Passcode") goes before a bare "Continue"."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    as_test_identity(monkeypatch, job_apply_home)
    asked: list[str] = []
    inbox_with_code(monkeypatch, asked, spam=True)
    job = srv.add_job(url=fixture_url("site/brassring-signin.html") + query, title="Senior Analyst",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "email_code", about=state(r))  # its passcode: the inbox's
            await until(lambda: "pressed \u201cVerify Passcode\u201d" in r.log, about=state(r))
            return r, await brassring_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    assert did.get("accountEmail") == "sam.rivera@example.com", (did, r.log)
    assert (did.get("passcode"), did.get("verifies"), did.get("accountPresses")) == ("123456", 1, 1), (did, r.log)
    assert "code" in asked and "link" not in asked, asked
    assert "entered the code from your email (sent from careers.example.com, in your Spam folder)" in r.log, r.log


@pytest.mark.parametrize("query", ["", "?other"])
def test_brassrings_account_after_its_passcode_gets_the_saved_password_and_leaves_its_security_question(
        srv, monkeypatch, job_apply_home, query):
    """Edward Jones' BrassRing (run 67): after its passcode, its Create Account shows the email (no box for it), a
    password twice, and "Security questions": "Question 1" to choose and its answer. With no email box the desk took
    it for no form of its own and said "Sign in (or create your account) on BrassRing". It's the account for the email
    it shows: the saved password goes in both boxes, and the security question and its answer are the person's (named
    so by the heading), so Continue isn't pressed and the card says so. A form that shows another email (?other)
    isn't the account for the profile's: the saved password never goes on it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    as_test_identity(monkeypatch, job_apply_home)
    inbox_with_code(monkeypatch)
    job = srv.add_job(url=fixture_url("site/brassring-signin.html") + query, title="Senior Analyst",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: "pressed \u201cVerify Passcode\u201d" in r.log, about=state(r))
            await until(lambda: r.need not in ("", "email_code") and r.status not in ("queued", "running"),
                        about=state(r))  # (the desk goes on by itself after the code: _enter_code)
            return r, await brassring_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    form = did.get("accountForm") or {}
    if query:
        assert not form.get("passwords") and "accountMade" not in did, (did, r.log)
        assert r.need == "sign_in" and "saved password" not in r.reason.split(".")[0], (r.reason, r.log)
        return
    assert form.get("passwords") == 2 and not form.get("question") and not form.get("answer"), (did, r.reason, r.log)
    assert not form.get("twofa") and "accountMade" not in did, (did, r.log)
    assert r.need == "sign_in" and "Create Account form with your details and saved password" in r.reason \
        and "security questions and their answers: those are yours" in r.reason, (r.reason, r.log)


@pytest.mark.slow
def test_brassrings_security_question_is_answered_on_the_card_for_that_form_alone(srv, monkeypatch, job_apply_home):
    """Edward Jones' BrassRing (runs 67 to 77): its Create Account asks for a security question, chosen from a jQuery UI
    selectmenu (a span standing in for a hidden select, its choices in a menu drawn when it's clicked), and its answer.
    The desk never read the chooser, and could only leave both to the person in the browser window. With
    manage_accounts they're put to the person on the card, the chooser with its choices. Answered there, they go into
    that form alone (never remembered, never among the application's answers, never in its log or on the desk page)
    and the desk creates the account."""
    from job_apply.desk import Desk

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    as_test_identity(monkeypatch, job_apply_home)
    inbox_with_code(monkeypatch)
    job = srv.add_job(url=fixture_url("site/brassring-signin.html") + "?selectmenu", title="Senior Analyst",
                      company="Example Corp")["job"]
    desk = Desk(srv)
    applier = desk.applier
    secret = "Flagstaff Ridge 1987"

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: "pressed “Verify Passcode”" in r.log, about=state(r))
            await until(lambda: r.need not in ("", "email_code") and r.status not in ("queued", "running"),
                        about=state(r))
            asked, reason = [dict(q) for q in r.questions or []], r.reason
            keys = [f"{q.get('section') or ''} | {q['label']} | {q.get('sublabel') or ''}" for q in asked]
            chooser = next((k for k, q in zip(keys, asked) if q.get("options")), None)
            if chooser is None:
                return r, asked, reason, {}, ""
            desk.answer(job["id"], [{"label": chooser, "value": "In what city were you born?", "remember": True},
                                    *({"label": k, "value": secret} for k in keys if k != chooser)])
            shown = json.dumps(r.public())
            await until(lambda: any(line.startswith("filled in your answers") for line in r.log), about=state(r))
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            # (the form as read now, and as a saved page keeps it: Claude's tools, a stop's page, a problem report)
            srv.browser.use_tab(r.page)
            read = json.dumps(await srv.inspect_form(include_dropdown_options=False))
            dest = await srv.browser.snapshot(job_apply_home / "saved-page")
            saved = "".join(f.read_text(encoding="utf-8") for f in dest.iterdir() if f.suffix in (".html", ".json"))
            shown += read + saved + ("screenshot" if (dest / "screenshot.jpg").exists() else "")
            return r, asked, reason, await brassring_did(r.page), shown
        finally:
            await applier.stop()

    r, asked, reason, did, shown = run(go())
    assert [q["kind"] for q in asked] == ["listbox", "text"] and all(q.get("account") for q in asked), asked
    assert "What was the name of your first pet?" in asked[0]["options"] and asked[0]["required"], asked
    assert r.need == "sign_in" and "Answer them here" in reason, (reason, r.log)
    form = did.get("accountForm") or {}
    assert (form.get("question"), form.get("answer"), did.get("accountMade")) == (
        "In what city were you born?", secret, 1), (did, r.log)
    assert [line for line in r.log if "your answers" in line] == [
        "filled in your answers to the security questions on Example Corp's site"], r.log
    # that form's alone: never remembered, never the application's, said nowhere
    assert config.saved_answers() == [] and r.once == {}, (config.saved_answers(), r.once)
    assert secret not in shown and secret not in json.dumps(r.public()) and secret not in "\n".join(r.log)
    assert '"(set)"' in shown and "screenshot" not in shown and r.account_answers == {}, shown[-3000:]
    # nor on the job's "What went in" panel: neither the question chosen nor its answer
    went = json.dumps(r.public()["went_in"])
    assert "In what city were you born?" not in went and secret not in went, went


@pytest.mark.slow
def test_brassrings_application_is_begun_with_lets_get_started_once_the_account_is_made(srv, monkeypatch,
                                                                                         job_apply_home):
    """Edward Jones' BrassRing (probe 81): its security questions answered on the card and the account made, its
    application's first page offers "Let's get started" and "Save and finish later" (both marked aria-live). "Let's
    get started" wasn't a step to the desk, so it said "I couldn't find the button". It begins the application."""
    from job_apply.desk import Desk

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    as_test_identity(monkeypatch, job_apply_home)
    inbox_with_code(monkeypatch)
    job = srv.add_job(url=fixture_url("site/brassring-signin.html") + "?selectmenu&apply", title="Senior Analyst",
                      company="Example Corp")["job"]
    desk = Desk(srv)
    applier = desk.applier

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "sign_in" and r.status not in ("queued", "running") and r.questions,
                        timeout=90, about=state(r))
            desk.answer(job["id"], [
                {"label": f"{q.get('section') or ''} | {q['label']} | {q.get('sublabel') or ''}",
                 "value": "In what city were you born?" if q.get("options") else "Flagstaff Ridge 1987"}
                for q in r.questions])
            await until(lambda: r.seen_form or (r.status not in ("queued", "running") and not r.questions),
                        timeout=90, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert any(line.startswith("clicked") and "get started" in line for line in r.log), r.log
    assert r.seen_form and "couldn't find the button" not in r.reason, (r.reason, r.log)


def test_a_job_alert_sign_up_on_the_page_is_never_filled_asked_or_sent(srv, monkeypatch, job_apply_home):
    """Dignity Health (run 82): the posting's link led to CommonSpirit's careers search, whose "Sign up for Job Alerts"
    form (an email, a job category and a location) the desk took for the application: it typed the person's email
    into it and asked for the rest, and answered, its Submit would have signed them up. Job alerts are never the
    person's to be signed up for by the desk: nothing in it is filled, asked or sent."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/careers-search-job-alert.html"), title="HR Coordinator",
                      company="Example Health")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => [document.getElementById('em').value, "
                                            "sessionStorage.getItem('alerts.submits')]")
        finally:
            await applier.stop()

    r, (email, submits) = run(go())
    assert email == "" and submits is None, (email, submits, r.log)
    assert not any(q.get("label", "").startswith(("Job Category", "Location", "Email")) for q in r.questions or []), \
        (r.questions, r.reason)
    assert not any(line.startswith("filled") for line in r.log), r.log


@pytest.mark.slow
@pytest.mark.parametrize("case", ["redrawn", "failed", "unpicked", "captcha"])
def test_an_account_form_is_never_pressed_with_its_security_questions_unanswered(srv, monkeypatch, job_apply_home,
                                                                                   case):
    """The review of the security questions' card. "redrawn": the person's answers went in, the button wasn't pressed
    (held back here once), and the form was drawn again, empty, before Resume: the desk had kept "answered" for the job
    and pressed it with the questions empty. Told from the form as it is now, it isn't pressed, and the card asks again
    (and the pause before says the answers are in). "failed": they wouldn't go in: said so, and left to the browser
    window, never pressed. "unpicked" (the second review): the question picked on the card isn't one its list has, so
    its list stays on its placeholder, whose value is "-1": the answer box went in, and the list, marked as the
    person's, read as answered, so a Resume pressed it. A list on its placeholder is unanswered, and a box reads as
    answered only once the desk saw its answer go in. "captcha": a picture code on the form too: the account is the
    person's to make, so no card (answered, it would only come back), and the CAPTCHA is said."""
    from job_apply.desk import Desk

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    as_test_identity(monkeypatch, job_apply_home)
    inbox_with_code(monkeypatch)
    query = "?minusone" if case == "unpicked" else "?selectmenu" + ("&formcaptcha" if case == "captcha" else "")
    job = srv.add_job(url=fixture_url("site/brassring-signin.html") + query, title="Senior Analyst",
                      company="Example Corp")["job"]
    desk = Desk(srv)
    applier = desk.applier
    if case == "failed":
        real_fill = srv.browser.fill

        async def fill(values):
            if any(v.get("secret") for v in values):
                return [{"id": v["id"], "ok": False, "error": "TimeoutError: it wouldn't take it"} for v in values]
            return await real_fill(values)

        monkeypatch.setattr(srv.browser, "fill", fill)
    held: list[int] = []
    real_make = Applier._make_account

    async def make_account(self, run_, data):
        if case == "redrawn" and not held and any(line.startswith("filled in your answers") for line in run_.log):
            held.append(1)  # (its button not found, say: nothing pressed)
            return False
        return await real_make(self, run_, data)

    monkeypatch.setattr(Applier, "_make_account", make_account)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: "pressed \u201cVerify Passcode\u201d" in r.log, about=state(r))
            await until(lambda: r.need not in ("", "email_code") and r.status not in ("queued", "running"),
                        about=state(r))
            pauses = [(r.reason, [dict(q) for q in r.questions or []])]
            if case != "captcha":
                desk.answer(job["id"], [
                    {"label": f"{q.get('section') or ''} | {q['label']} | {q.get('sublabel') or ''}",
                     "value": ("What was your first car?" if case == "unpicked" else "In what city were you born?")
                     if q.get("options") else "Flagstaff Ridge 1987"}
                    for q in pauses[0][1]])
                await until(lambda: r.status not in ("queued", "running") and any(
                    "your answers to the security questions" in line for line in r.log), about=state(r))
                pauses.append((r.reason, [dict(q) for q in r.questions or []]))
            if case in ("redrawn", "unpicked"):  # (redrawn: its boxes new, and empty; unpicked: as the desk left it)
                await r.page.evaluate("""(redraw) => { if (!redraw) return;
                    document.querySelectorAll('[data-ja-secret]').forEach((e) => {
                      e.removeAttribute('data-ja-secret'); e.style.removeProperty('-webkit-text-security'); });
                    document.getElementById('sa').value = ''; document.getElementById('sq').value = '';
                    document.getElementById('sq-button_text').textContent = 'Select question'; }""", case == "redrawn")
                told = len(r.log)
                applier.resume(job["id"])
                await until(lambda: r.status not in ("queued", "running") and len(r.log) > told, about=state(r))
                pauses.append((r.reason, [dict(q) for q in r.questions or []]))
            return r, pauses, await brassring_did(r.page)
        finally:
            await applier.stop()

    r, pauses, did = run(go())
    assert "accountMade" not in did, (did, r.log)  # (never pressed with them unanswered)
    if case == "captcha":
        (reason, card), = pauses
        assert card == [] and "solve its CAPTCHA (that's yours)" in reason, (reason, card)
        return
    assert pauses[0][1] and all(q.get("account") for q in pauses[0][1]), pauses[0]
    reason, card = pauses[1]
    if case == "unpicked":
        assert card == [] and "couldn't put your answers to its security questions into it" in reason, (reason, r.log)
        reason, card = pauses[2]
        assert card and "Answer them here" in reason, (reason, card, r.log)  # (on Resume: asked again, never pressed)
        return
    if case == "failed":
        assert card == [] and "couldn't put your answers to its security questions into it" in reason, (reason, r.log)
        assert "couldn't put your answers to the security questions into the form on Example Corp's site" in r.log
        return
    assert card == [] and "Your answers to its security questions are in" in reason, (reason, r.log)
    reason, card = pauses[2]
    assert card and all(q.get("account") for q in card) and "Answer them here" in reason, (reason, card, r.log)


def test_an_account_forms_security_questions_go_on_the_card_only_told_apart():
    """Two security questions under one heading, each "Question" with its "Answer" (the review of the card): their
    boxes' keys on the card are the same, so one answer would go into both. No card then: they stay the person's in
    the browser window."""
    boxes = [{"id": i, "kind": "select" if n % 2 == 0 else "text", "label": "Question" if n % 2 == 0 else "Answer",
              "options": ["In what city were you born?"] if n % 2 == 0 else None} for n, i in enumerate("abcd")]
    assert pipeline._account_card_boxes(boxes, ["Security questions"]) == []
    assert len(pipeline._account_card_boxes(boxes[:2], ["Security questions"])) == 2
    assert not pipeline._security_answered({"fields": boxes[:2], "headings": ["Security questions"]})
    assert pipeline._security_answered({"fields": [{**f, "value": "(set)"} for f in boxes[:2]],
                                        "headings": ["Security questions"]})


def test_a_code_the_page_wouldnt_take_is_said_in_the_log_and_tried_again(srv, monkeypatch, job_apply_home):
    """The inbox's code was found but didn't go into the page (a dialog open over it, a box that wouldn't
    take it): the desk looked again at the next poll and said nothing, so a run that ended waiting on its
    code couldn't say whether it was found. The log says so, once, and the next look types it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    as_test_identity(monkeypatch, job_apply_home)
    inbox_with_code(monkeypatch)
    real, refused = srv.browser.type_code, []

    async def type_code(ids, code):
        if len(ids) == 6 and len(refused) < 2:  # (the code's six boxes: refused twice)
            refused.append(ids)
            raise browser_module.DialogOpen("“Notice” is open over the page")
        return await real(ids, code)

    monkeypatch.setattr(srv.browser, "type_code", type_code)
    job = srv.add_job(url=fixture_url("site/brassring-signin.html"), title="Senior Analyst",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: "pressed “Verify Passcode”" in r.log, about=state(r))
            return r, await brassring_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    said = [line for line in r.log if line.startswith("found the code in your email but couldn't type it")]
    assert said == ["found the code in your email but couldn't type it into the page (DialogOpen: “Notice” "
                    "is open over the page); I'll try again"], r.log
    assert len(refused) == 2 and (did.get("passcode"), did.get("verifies")) == ("123456", 1), (did, r.log)


def test_what_the_codes_boxes_read_instead_is_never_logged(srv, monkeypatch, job_apply_home):
    """A code typed into its boxes and read back wrong was logged as read ("its boxes read 12345"): most of the
    code, in a log the desk shows and a report files (the review of Oct 15). The log says how much they read."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    as_test_identity(monkeypatch, job_apply_home)
    inbox_with_code(monkeypatch)
    real, misread = srv.browser.type_code, []

    async def type_code(ids, code):
        if len(ids) == 6 and not misread:  # (the code's six boxes: read back one short, once)
            misread.append(ids)
            return list(code[:-1]) + [""]
        return await real(ids, code)

    monkeypatch.setattr(srv.browser, "type_code", type_code)
    job = srv.add_job(url=fixture_url("site/brassring-signin.html"), title="Senior Analyst",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: "pressed “Verify Passcode”" in r.log, about=state(r))
            return r, await brassring_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    said = [line for line in r.log if line.startswith("found the code in your email but couldn't type it")]
    assert said == ["found the code in your email but couldn't type it into the page (its boxes read 5 character(s), "
                    "not the code); I'll try again"], r.log
    assert "12345" not in "\n".join(r.log) and (did.get("passcode"), did.get("verifies")) == ("123456", 1), (did, r.log)


def test_whose_mail_a_brassring_job_reads_a_code_from(srv):
    """Edward Jones' posting is on its own careers site and its application on BrassRing: the codes may
    come from either's domains, and from the job system's own (Kenexa's, too)."""
    job = srv.add_job(url="https://careers.edwardjones.com/job/12345/financial-analyst", title="Financial Analyst",
                      company="Edward Jones",
                      apply_url="https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=26235"
                                "&siteid=5374#jobDetails=1427502_5374")["job"]
    applier = Applier(srv)
    r = Run(job["id"], "Financial Analyst", "Edward Jones",
            url="https://sjobs.brassring.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=26235&siteid=5374")
    assert applier._mail_senders(r) == {"edwardjones.com", "brassring.com", "kenexa.com"}


def test_whose_mail_a_workday_job_reads_a_reset_from(srv):
    """KLA's Workday (kla.wd1.myworkdayjobs.com, live, Oct 2026) sent its password reset from its own
    Workday mail domain, workday.kla.com: the desk never read it ("no password reset email came from
    Workday in 3 minutes") and made a new account instead. A tenant's own workday.<tenant>.com counts
    as the site's; no other address at kla.com does, nor another tenant's."""
    url = "https://kla.wd1.myworkdayjobs.com/Search/job/Phoenix-AZ/Customer-Service-Engineer_2637545"
    job = srv.add_job(url=url, title="Customer Service Engineer", company="KLA")["job"]
    r = Run(job["id"], "Customer Service Engineer", "KLA", url=url)
    senders = Applier(srv)._mail_senders(r)
    assert senders == {"myworkdayjobs.com", "myworkday.com", "workday.com", "workday.kla.com"}, senders
    assert mailbox.sender_allowed("Workday <noreply@workday.kla.com>", senders)
    assert not mailbox.sender_allowed("someone@kla.com", senders)
    assert not mailbox.sender_allowed("noreply@workday.intel.com", senders)


@pytest.mark.parametrize("company, url, sender", [
    ("BDO", "https://ebqb.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/BDOExperiencedCareers/job/14240",
     "Careers <no-reply@careers.bdo.com>"),
    ("American Express", "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/26013283",
     "no-reply@recruitment.americanexpress.com"),
    ("Grant Thornton", "https://ehzq.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/115912",
     "no-reply@us.gt.com"),  # (not its careers site's grantthornton.com: its entry names gt.com)
    ("Mayo Clinic", "https://fa-euwp-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/377528",
     "no-reply@administrativesystem.mayoclinic.org"),
    # (named otherwise, as a posting may name it: known by the site its entry searches)
    ("BDO USA, P.C.", "https://ebqb.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/BDOExperiencedCareers/job/1",
     "no-reply@careers.bdo.com"),
])
def test_whose_mail_an_oracle_job_reads_its_code_from(srv, job_apply_home, company, url, sender):
    """Oracle Recruiting mails its code from the employer's own domain, not Oracle's (run 49, Oct 13: American
    Express, BDO, Grant Thornton and Mayo Clinic each waited out a code their inbox had). An employer on the lists
    mails from its careers site's domain and any its entry names; an employer not on them only from Oracle's."""
    job = srv.add_job(url=url, title="Analyst", company=company)["job"]
    senders = Applier(srv)._mail_senders(Run(job["id"], "Analyst", company, url=url))
    assert mailbox.sender_allowed(sender, senders), senders
    assert not mailbox.sender_allowed("no-reply@another-employer.com", senders)
    other = "https://abcd.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/1"
    job = srv.add_job(url=other, title="Analyst", company="Unlisted Example")["job"]
    assert Applier(srv)._mail_senders(Run(job["id"], "Analyst", "Unlisted Example", url=other)) == {"oracle.com",
                                                                                                  "oraclecloud.com"}


def test_a_code_from_a_shared_sender_naming_another_employer_is_never_this_jobs(job_apply_home):
    """Run 54: Oracle mailed onsemi's code from its one shared address ("onsemi Confirm Your Identity") after onsemi's
    wait had ended, and American Express, waiting a minute later on a code from that address too, put it in ("The
    code isn't valid"). Such mail is another job's; the employer's own domain's, or a subject naming this employer
    (or none on the lists), stays this job's. Employers whose names begin with the same two words aren't told apart
    by it."""
    (job_apply_home / "companies.yaml").write_text(yaml.safe_dump({"lists": ["phoenix-metro", "semiconductor-az"]}), encoding="utf-8")
    oracle = "workflow.email.us-phoenix-1.ocs.oraclecloud.com"
    others = pipeline._another_employers_mail("American Express", frozenset({"americanexpress.com"}))
    assert others(oracle, "onsemi Confirm Your Identity")
    assert others(oracle, "Thank you for your interest in Tokyo Electron (TEL)")
    assert others(oracle, "American Airlines Confirm Your Identity")  # (one word alike isn't enough: the review)
    assert not others(oracle, "American Express Confirm Your Identity")
    assert not others(oracle, "Confirm Your Identity")
    assert not others("recruitment.americanexpress.com", "onsemi Confirm Your Identity")  # (the employer's own)
    assert not others(oracle, "Your onsemiconductor order")  # (a word of its own)
    gcu = pipeline._another_employers_mail("Grand Canyon University", frozenset())
    assert gcu is not None and not gcu("myworkday.com", "Grand Canyon Education: verify your email")
    assert pipeline._another_employers_mail("", frozenset()) is None


def test_the_test_identity_agrees_to_the_notices_a_persons_desk_leaves_to_them():
    """Run 56: Freeport-McMoRan's data privacy statement on its SuccessFactors Create Account (a waiver of rights in it,
    and its heading not a privacy notice's) is the person's on their own desk. The live check's test identity agrees to
    it (the owner's call, Oct 13); never a dialog about an error or an existing profile, nor anything but a notice."""
    heading = "Freeport-McMoRan Consent Statement"
    text = ("Freeport Data Privacy Statement ... If you are from a jurisdiction outside of the United States, please note "
            "in particular the waiver of rights provision (bolded) below")
    assert pipeline._terms_notice(heading, text) == (False, False)  # (the person's own desk: theirs)
    said = f"{heading} {text}"
    assert not pipeline._test_identity_agrees(heading, said, False)
    assert pipeline._test_identity_agrees(heading, said, True)
    assert not pipeline._test_identity_agrees("Something went wrong", "Something went wrong. Privacy notice failed", True)
    assert not pipeline._test_identity_agrees("Overwrite your existing profile?",
                                              "Overwrite your existing profile? Your profile and its terms", True)
    assert not pipeline._test_identity_agrees("Upload your resume", "Choose a file to upload", True)
    # (the review: by its opening, not its heading alone; never a sign-up, nor one that sends)
    assert not pipeline._test_identity_agrees("Notice", "Notice Your existing profile will be overwritten. I agree", True)
    assert not pipeline._test_identity_agrees("Privacy", "Privacy Join our talent community and receive job alerts", True)
    assert not pipeline._test_identity_agrees("Terms", "Terms By continuing you submit your application and agree", True)


def test_an_employers_mail_domains_are_only_its_own(job_apply_home):
    """The review of 0.3.137: a site several employers on the lists search (Nikon's, UCT's and Desert Financial's UKG
    Pro, recruiting2.ultipro.com) names none of them, so another employer's job there never reads a credit union's mail;
    a mail service's domain is never an employer's, nor the person's own address's; and an entry written oddly in the
    person's own list leaves the others as they are."""
    assert pipeline._employer_mail_domains("Acme Unlisted", {"recruiting2.ultipro.com"}) == set()
    assert pipeline._employer_mail_domains("Desert Financial Credit Union", {"recruiting2.ultipro.com"}) == \
        {"desertfinancial.com"}  # (by its name)
    (job_apply_home / "companies.yaml").write_text(yaml.safe_dump({"lists": ["phoenix-metro"], "companies": [
        {"name": "Odd One", "search": {"query": "Test [x"}, "mail_domains": 5},
        {"name": "Example Mailer", "careers_url": "https://mail.google.com/careers",
         "mail_domains": ["gmail.com", "careers.outlook.com", "examplemailer.com", "acme-person.example",
                          "mail.acme-person.example"]}]}),
        encoding="utf-8")
    assert pipeline._employer_mail_domains("Example Mailer", set(), own="acme-person.example") == {"examplemailer.com"}
    assert pipeline._employer_mail_domains("BDO", set()) == {"bdo.com"}
    # (an employer's own mail domain its entry names: Vanguard's Workday verifies its accounts from vanguardhr.com,
    # the live check's run 102, where the link the desk had asked for again went unread as another's)
    assert pipeline._employer_mail_domains("Vanguard", set()) >= {"vanguardjobs.com", "vanguardhr.com"}
    assert pipeline._employer_mail_domains("Odd One", {"test"}) == set()


def test_a_job_search_reached_mid_sign_in_opens_the_job_again_once(srv, monkeypatch, job_apply_home):
    """Edwards Vacuum's SuccessFactors (the probe of Oct 12): after the wait on a reset email, the site had already
    left its sign-in for its careers search (its session gone), and the desk, not going back through the sign-in,
    stopped there ("couldn't find the button"). Mid sign-in, a job search with no way on has the job opened again,
    once a job."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/sf-careers-home.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"))
            assert "opened the job again" not in "\n".join(r.log)  # (not mid sign-in: as before)
            r.reset_from = "https://career5.successfactors.eu/careers?company=example&login_ns=signin"
            applier.enqueue(job["id"], front=True)
            await until(lambda: r.status not in ("queued", "running"))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert log.count("left its sign-in for its job search, so I opened the job again") == 1, log
    assert r.need == "stuck", (r.need, r.reason)  # (once a job: then said as before)


@pytest.mark.slow
@pytest.mark.parametrize("gone", ["error", "home", "menu"])
def test_a_sign_in_page_good_for_one_visit_is_reached_again_through_the_posting(srv, monkeypatch, job_apply_home,
                                                                                 gone):
    """SuccessFactors' sign-in page (career?_s.crb=…) is good for one visit: going back to it after a
    reset the desk couldn't do showed "An error occurred while processing your request" (Arizona
    Public Service, the test identity, live Oct 2026), or led to the company's careers search home, with
    no sign-in on it (Edwards Vacuum's, after three minutes waiting on a reset email, the plus probe Oct 11).
    The desk opens the job's posting again, whose Apply leads to a fresh sign-in page, and makes the account
    there."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    # (menu: its "Apply now" opens a menu whose "Apply Now" goes on, EY's: pressed again once the job's opened again,
    # it isn't a third press)
    posting = fixture_url("site/sf-posting.html") + {"error": "", "home": "?home", "menu": "?menu"}[gone]
    job = srv.add_job(url=posting, title="Energy Analyst", company="Example Utility")["job"]
    applier = Applier(srv)
    # What each tab loaded, and what sessionStorage held as each page started: on CI, now and then, the sign-in opened
    # again drew its form, not its error (the 0.3.154 run). Under load the page didn't see the token its first visit
    # stored: the reset page after it already found sf.used empty, in the same tab (a lost write, the browser's)
    seen: list[str] = []
    tabs: list = []

    def watch(tab):
        name = f"tab {len(tabs)}"
        tabs.append(tab)
        tab.on("framenavigated", lambda f: f == tab.main_frame and seen.append(f"{name}: {f.url.rsplit('/', 1)[-1]}"))
        tab.on("console", lambda m: m.text.startswith("sf.used") and seen.append(f"{name}: {m.text}"))

    back = Applier._back_to_sign_in

    async def back_to_sign_in(self, run_):  # (which tab the tools read when the desk went back: the job's?)
        seen.append(f"going back: the tools' tab is the job's: {srv.browser._page is run_.page}")
        await back(self, run_)

    monkeypatch.setattr(Applier, "_back_to_sign_in", back_to_sign_in)

    async def go():
        await srv.browser.page()
        for tab in srv.browser._ctx.pages:
            watch(tab)
        srv.browser._ctx.on("page", watch)
        await srv.browser._ctx.add_init_script("if (location.protocol === 'file:' && sessionStorage) "
                                               "console.log('sf.used at the start: ' + sessionStorage.getItem('sf.used'))")
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            with contextlib.suppress(Exception):
                seen.append("at the end: " + await r.page.evaluate("() => location.href.split('/').pop() + "
                                                                   "', sf.used ' + sessionStorage.getItem('sf.used')"))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert "so I went back to make an account there instead" in log, log
    said = ", opened again, led to the site's job search, not its sign-in" if gone == "home" else " couldn't be opened again"
    assert f"sign-in page{said}, so I opened the job again" in log, log + "\n--- the browser:\n" + "\n".join(seen)
    assert "created your account" in log and r.seen_form, (r.reason, r.log)


# Benchmark's Infor CloudSuite board (data/companies.yaml): a posting, and Infor's sign-in
INFOR = "https://css-benchmark-prd.inforcloudsuite.com"
INFOR_POSTING = INFOR + "/hcm/Jobs/form/JobPosting.JobPostingDisplay?csk.JobBoard=EXTERNAL&csk.HROrganization=1"


async def _infor_site(srv, posting: str = "infor-posting.html", refused: bool = False) -> None:
    """Benchmark's Infor board from the hand-written pages (site/infor-*.html, and its New Account
    Registration: register-picture-code.html) at its own address, so its saved password is Infor's as
    it is live; routed: nothing reaches the real site. Infor's sign-in and its reset are both at
    /sso/SSOServlet. `posting`: which of its postings (infor-posting-refused.html's Apply goes nowhere
    once a sign-in was refused in the tab); `refused`: one was already, as every tab opens."""
    site = Path(__file__).parent / "fixtures" / "site"
    pages = {"/hcm/Jobs/form/JobPosting.JobPostingDisplay": posting, "/sso/SSOServlet": "infor-signin.html",
             "/hcm/Jobs/form/Candidate.SelfRegistrationForm": "register-picture-code.html"}

    async def handler(route):
        url = urlparse(route.request.url)
        name = "infor-reset.html" if "_action=PWDRESET" in url.query else pages.get(url.path)
        await route.fulfill(status=200 if name else 404, content_type="text/html",
                            body=(site / name).read_text(encoding="utf-8") if name else "<html><body>not found</body></html>")

    await srv.browser.page()
    await srv.browser._ctx.route(INFOR + "/**", handler)
    if refused:
        await srv.browser._ctx.add_init_script(f"if (location.origin === '{INFOR}' && !sessionStorage.getItem("
                                               "'infor.logins')) sessionStorage.setItem('infor.logins', '1');")


def test_infors_sign_in_opened_again_by_its_address_is_reached_through_the_posting(srv, monkeypatch, job_apply_home):
    """Benchmark's Infor board (live, Oct 2026): the saved password didn't sign in, the password
    reset's button stayed greyed out with the email filled in, and the desk went back to the sign-in
    page's address to make an account there. But its form had been sent to /sso/SSOServlet, and that
    address opened again is Infor's own sign-in, with no "Register" (nor "Forgot password?"): the desk
    stopped there. It opens the posting again, whose Apply leads to a fresh sign-in with its "Register",
    and fills in the board's New Account Registration; its picture code is the person's, and so is
    its Submit."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setenv("JOB_APPLY_SECRET_INFOR_PASSWORD", "not-a-real-password")
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=INFOR_POSTING, title="Field Service Technician", company="Benchmark Electronics")["job"]
    applier = Applier(srv)

    async def go():
        await _infor_site(srv)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => [window.result ? window.result() : location.href, window.created, "
                                            "sessionStorage.getItem('infor.logins')]")
        finally:
            await applier.stop()

    r, (filled, created, logins) = run(go())
    assert filled == {"first": "Sam", "last": "Rivera", "email": "sam.rivera@example.com", "same": True, "code": "",
                      "upload": "", "files": 0, "nores": False}, (filled, r.reason, r.log)
    assert (created, logins) == (0, "1"), (created, logins, r.log)  # (one sign-in, never a second)
    assert r.need == "sign_in" and "Create Account form" in r.reason and "CAPTCHA is yours to solve" in r.reason, r.reason
    log = "\n".join(r.log)
    assert "couldn't press its button, so I went back to make an account there instead" in log, log
    assert "Infor's sign-in page, opened again, had no way to a new account or a password reset, so I opened the " \
           "job again" in log, log
    assert "your saved password didn't sign in, so I opened Create Account" in log, log
    assert not any(line.startswith(("pressed “Submit”", "created your account")) for line in r.log), log


def test_infors_register_is_the_way_to_an_account_when_the_postings_apply_goes_nowhere(srv, monkeypatch, job_apply_home):
    """Benchmark's Infor board again (live, Oct 10 2026): the desk had gone back for an account, found Infor's
    sign-in opened by its address bare, and opened the job again, but this time the posting's "Apply" went
    nowhere (once a sign-in was refused in the tab, Infor's session keeps Apply from opening the sign-in
    again), and the desk stopped on "The page didn't move on". The posting's header has "Register", the
    board's New Account Registration: the desk presses that instead (it was on its way to a new account),
    fills it in from the profile, and leaves its picture code and its Submit to the person."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setenv("JOB_APPLY_SECRET_INFOR_PASSWORD", "not-a-real-password")
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=INFOR_POSTING, title="Field Service Technician", company="Benchmark Electronics")["job"]
    applier = Applier(srv)

    async def go():
        await _infor_site(srv, posting="infor-posting-refused.html")
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            # (about 25 s on Linux, waiting out the refused sign-in and a posting whose Apply goes nowhere: more than a
            # minute on Windows CI, the run of 0.3.164's)
            await until(lambda: r.status not in ("queued", "running"), timeout=150, about=state(r))
            return r, await r.page.evaluate("() => [window.result ? window.result() : location.href, window.created, "
                                            "sessionStorage.getItem('infor.logins')]")
        finally:
            await applier.stop()

    r, (filled, created, logins) = run(go())
    assert filled == {"first": "Sam", "last": "Rivera", "email": "sam.rivera@example.com", "same": True, "code": "",
                      "upload": "", "files": 0, "nores": False}, (filled, r.reason, r.log)
    assert (created, logins) == (0, "1"), (created, logins, r.log)  # (one sign-in, never a second; nothing sent)
    assert r.need == "sign_in" and "Create Account form" in r.reason and "CAPTCHA is yours to solve" in r.reason, r.reason
    log = "\n".join(r.log)
    assert "Infor's sign-in page, opened again, had no way to a new account or a password reset, so I opened the " \
           "job again" in log, log
    assert "“Apply” on Infor's posting went nowhere, so I pressed its “Register” to make an account there instead" \
           in log, log
    assert not any(line.startswith(("pressed “Submit”", "created your account")) for line in r.log), log


@pytest.mark.parametrize("settings", ["on", "off", "practice"])
def test_a_postings_register_is_not_pressed_where_the_desk_is_not_making_an_account(srv, monkeypatch, job_apply_home,
                                                                                    settings):
    """A posting whose "Apply" goes nowhere, on a first look at it (no sign-in refused, no account being made
    for: the desk only goes in through Apply), is stopped on as before, whatever the settings: its header's
    "Register" is never pressed, with settings.manage_accounts off or in practice mode (which makes no
    account) or on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 1)  # (the stop on the posting is the point, not its save wait)
    monkeypatch.setenv("JOB_APPLY_SECRET_INFOR_PASSWORD", "not-a-real-password")
    if settings != "off":
        manage_accounts(job_apply_home, submit_mode="dry_run" if settings == "practice" else "review")
    watched_inbox(monkeypatch)
    job = srv.add_job(url=INFOR_POSTING, title="Field Service Technician", company="Benchmark Electronics")["job"]
    applier = Applier(srv)

    async def go():
        await _infor_site(srv, posting="infor-posting-refused.html", refused=True)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            # (it takes about 40 s on Linux, waiting out a posting whose Apply goes nowhere: more on Windows CI)
            await until(lambda: r.status not in ("queued", "running"), timeout=150, about=state(r))
            return r, r.page.url
        finally:
            await applier.stop()

    r, url = run(go())
    assert r.need == "stuck" and "The page didn't move on" in r.reason, (r.need, r.reason, r.log)
    assert urlparse(url).path == "/hcm/Jobs/form/JobPosting.JobPostingDisplay", (url, r.log)  # (still on the posting)
    assert not any("Register" in line or "make an account" in line for line in r.log), r.log


# Kforce's Taleo career section, on Kforce's own address (data/lists/phoenix-metro.yaml)
TALEO_LOGIN = "https://myhiring.kforce.com/careersection/iam/accessmanagement/login.jsf"


async def _taleo_site(srv) -> None:
    """Kforce's Taleo career section from the hand-written pages (site/taleo-*.html) at its own
    address, so its saved password is Taleo's as it is live; routed: nothing reaches the real site.
    Its application is a plain form."""
    site = Path(__file__).parent / "fixtures" / "site"
    pages = {"/careersection/iam/accessmanagement/login.jsf": (site / "taleo-login.html").read_text(encoding="utf-8"),
             "/careersection/iam/accessmanagement/register.jsf": (site / "taleo-register.html").read_text(encoding="utf-8"),
             "/careersection/application.jss": _form("Kforce")}

    async def handler(route):
        page = pages.get(urlparse(route.request.url).path)
        await route.fulfill(status=200 if page else 404, content_type="text/html",
                            body=page or "<html><body>not found</body></html>")

    await srv.browser.page()
    await srv.browser._ctx.route("https://myhiring.kforce.com/**", handler)


def test_taleos_saved_password_goes_on_its_career_sections_only():
    """Kforce's Taleo career section is on Kforce's own address: the saved Taleo password goes there
    (as on taleo.net), and on nothing that only looks like it."""
    assert pipeline.password_for(TALEO_LOGIN + "?lang=en") == "taleo_password"
    assert pipeline.password_for("https://acme.taleo.net/careersection/2/jobapply.ftl") == "taleo_password"
    for url in ("http://myhiring.kforce.com/careersection/iam/accessmanagement/login.jsf",
                "https://myhiring.kforce.com.evil.example/careersection/", "https://evil.example/myhiring.kforce.com/",
                "https://www.kforce.com/careers/"):
        assert pipeline.password_for(url) is None, url
    assert pipeline._CREATE_ACCOUNT.match("New User")  # Taleo's way to a new account
    assert "Save a Taleo password" in pipeline._password_tip(TALEO_LOGIN)  # (none saved in the test's home)


def test_taleo_signs_in_with_its_saved_password_and_the_email_as_user_name(srv, monkeypatch, job_apply_home):
    """Kforce's Taleo Login (live, Oct 10) asks for a "User Name", and is on Kforce's own address: the
    desk didn't take it for Taleo's and stopped at once ("Sign in (or create your account)"), the saved
    Taleo password untried. It signs in there with that password, the profile email as the user name
    (the one the desk makes an account with), and goes on to the application."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setenv("JOB_APPLY_SECRET_TALEO_PASSWORD", "not-a-real-password")
    job = srv.add_job(url=TALEO_LOGIN + "?user=sam.rivera%40example.com&pw=not-a-real-password", title="Recruiter",
                      company="Kforce")["job"]
    applier = Applier(srv)

    async def go():
        await _taleo_site(srv)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => [sessionStorage.getItem('signedIn'), sessionStorage.getItem('logins')]")
        finally:
            await applier.stop()

    r, (signed_in, logins) = run(go())
    assert (signed_in, logins) == ("sam.rivera@example.com", "1"), (signed_in, logins, r.reason, r.log)
    assert "pressed “Login” with your saved password and your email as the user name" in r.log, r.log
    assert r.seen_form and not any("account" in line for line in r.log), (r.status, r.reason, r.log)


def test_with_manage_accounts_taleo_makes_the_account_through_new_user(srv, monkeypatch, job_apply_home):
    """The live check's test identity has no account at Kforce's Taleo. The saved Taleo password
    doesn't sign in, and Taleo's reset ("Need a Password?") asks for the user name and emails an
    access code, which the desk can't do, so even with the inbox watched it makes the account: Taleo's
    "New User", the profile email as the user name (said in the log) and the saved password twice,
    then "Register"."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setenv("JOB_APPLY_SECRET_TALEO_PASSWORD", "not-a-real-password")
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=TALEO_LOGIN + "?lang=en", title="Recruiter", company="Kforce")["job"]
    applier = Applier(srv)

    async def go():
        await _taleo_site(srv)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate(
                "() => [sessionStorage.getItem('registered'), sessionStorage.getItem('logins'),"
                " sessionStorage.getItem('registers')]")
        finally:
            await applier.stop()

    r, (registered, logins, registers) = run(go())
    assert (registered, logins, registers) == ("sam.rivera@example.com", "1", "1"), (registered, logins, r.reason, r.log)
    log = "\n".join(r.log)
    assert "your saved password didn't sign in, so I opened Create Account" in log, log
    assert "filled the Create Account form with your details and saved password (your email as its user name)" in log
    assert "pressed “Register” to create your account on Oracle Taleo with your email as its user name" in log
    assert any(line.startswith("created your account on Oracle Taleo with your email as its user name")
               for line in r.log), log
    assert "password reset" not in log and r.seen_form, (r.status, r.reason, log)


def test_taleos_security_questions_are_left_to_the_person(srv, monkeypatch, job_apply_home):
    """A Taleo New User form that asks for a security question and its answer: the desk fills in the
    user name (the email) and the password, never makes up the questions' answers, and so leaves the
    form to the person (Register unpressed) with what it wants."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setenv("JOB_APPLY_SECRET_TALEO_PASSWORD", "not-a-real-password")
    manage_accounts(job_apply_home)
    job = srv.add_job(url=TALEO_LOGIN + "?questions=1", title="Recruiter", company="Kforce")["job"]
    applier = Applier(srv)

    async def go():
        await _taleo_site(srv)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate(
                "() => [document.getElementById('dialogTemplate-dialogForm-userName').value,"
                " document.getElementById('dialogTemplate-dialogForm-question1').value,"
                " document.getElementById('dialogTemplate-dialogForm-answer1').value,"
                " sessionStorage.getItem('registers')]")
        finally:
            await applier.stop()

    r, shown = run(go())
    assert shown == ["sam.rivera@example.com", "", "", None], (shown, r.reason, r.log)
    assert r.need == "sign_in" and "security questions" in r.reason and "never makes up answers" in r.reason, r.reason
    assert "your email as its user name" in r.reason, r.reason
    assert not any(line.startswith(("pressed “Register", "created your account")) for line in r.log), r.log


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["workday", "workday-late"])
def test_a_workday_reset_that_says_if_an_account_exists_waits_then_makes_one(srv, monkeypatch, job_apply_home, mode):
    """Workday's password reset stays on its form and says "You will receive an email with
    instructions to reset your password if an account exists for this email address." (live, Oct
    2026, the test identity's first run). Said at once (Banner Health's), the desk took it for a
    refusal and stopped; said a moment later (KLA's), it waited for the email, and when none came
    it signed in on the reset's own form and stopped. Either way: wait for the email, then, with
    none, back to the sign-in page to make an account."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "RESET_MAIL_WAIT", 4)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-submit.html") + f"?reset={mode}", title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you", about=state(r))
            assert r.need == "email_code" and "makes one instead" in r.reason, (r.reason, r.log)
            await until(lambda: r.status not in ("queued", "running") and r.need != "email_code", timeout=60,
                        about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert "asked it to email a password reset" in log and "no password reset email came" in log, log
    assert "created your account" in log and r.seen_form, (r.reason, r.log)


def test_a_new_account_the_site_takes_a_moment_over_is_signed_in_to(srv, monkeypatch, job_apply_home):
    """A Workday site goes on to its Sign In page a few seconds after Create Account: the desk
    waits for that, rather than filling the same form in again and stopping, then signs in."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/create-account.html") + "?slow", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "wants something more" not in r.reason, (r.reason, r.log)
    assert r.log.count("filled the Create Account form with your details and saved password") == 1, r.log
    assert any(line.startswith("pressed \u201cSign In\u201d") for line in r.log) and r.seen_form, (r.reason, r.log)


async def workday_did(page):
    """What the Workday-like account page (site/workday-account.html) has seen in this tab."""
    return await page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage).filter(([k]) => "
                               "k.startsWith('wd.')).map(([k, v]) => [k.slice(3), JSON.parse(v)]))")


def test_a_workday_account_is_said_made_only_once_its_sign_in_gets_in(srv, monkeypatch, job_apply_home):
    """A Workday site's Create Account wants its "I confirm that I have read the privacy notice" box
    ticked (never a "keep me informed" one), and goes on to its Sign In whether or not it made the
    account (live, Oct 2026). The desk said it had made the account as the button was pressed: it's
    said only once the new account's sign-in gets in."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/workday-account.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await workday_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    assert (did["privacy"], did["informed"], did["presses"], did["created"], did["signIns"]) == (True, False, 1, 1, 2), did
    signed_in = max(i for i, line in enumerate(r.log) if line.startswith("pressed \u201cSign In\u201d"))
    made = [i for i, line in enumerate(r.log) if line.startswith("created your account on")]
    assert len(made) == 1 and signed_in < made[0], r.log
    pressed = [line for line in r.log[:signed_in] if line.startswith("pressed \u201cCreate Account\u201d")]
    assert len(pressed) == 1 and "after ticking \u201cYes, I confirm that I have read the privacy notice.\u201d" in pressed[0]
    assert r.seen_form, (r.reason, r.log)


def test_a_create_account_shown_again_after_the_account_was_made_goes_to_its_sign_in(srv, monkeypatch, job_apply_home):
    """Applied Materials' Workday (run 60): the account was made and signed in, then Workday's "something went wrong"
    refresh signed the applicant out and showed Create Account again; the desk filled it and stopped ("it wants
    something more"), as a Create Account is never pressed twice for a job. The account is made: its Sign In is the
    way back in."""
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    run(srv.open_application(url=fixture_url("site/workday-account.html") + "?create"))
    data = run(srv.inspect_form(include_dropdown_options=False))
    applier = Applier(srv)
    r = Run(1, "FSE", "Example Fab")
    r.accounts_tried, r.account_in = 1, True  # (made, and signed in to: the desk said so)
    tried: dict = {}
    assert run(applier._sign_in(r, data, tried)) == "email_step", r.log
    assert "your account there is made already, so I went to its Sign In" in r.log
    now = run(srv.inspect_form(include_dropdown_options=False))
    assert sum(f.get("kind") == "password" for f in now["fields"]) == 1, now["fields"]  # (Sign In's one password box)
    # (and an account not made for this job, or only pressed for and not yet signed in to: its Create Account is
    # filled, as before, and what the site wants of it is said: the review of 0.3.148)
    for made in ("", "created your account on Workday with your saved password"):
        run(srv.open_application(url=fixture_url("site/workday-account.html") + "?create"))
        data = run(srv.inspect_form(include_dropdown_options=False))
        other = Run(2, "FSE", "Example Fab")
        other.account_made = made
        assert run(applier._sign_in(other, data, {})) == "prefilled", made


def test_an_assessment_step_is_said_to_be_the_applicants(srv, monkeypatch, job_apply_home):
    """Circle K's Workday (the nightly of Oct 12): a step that won't go on until the applicant takes the employer's
    assessment. The desk pressed Save and Continue three times and said no application opened. It says the
    assessment is the person's to take, and never starts it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/workday-assessment.html"), title="CSR", company="Example Stores")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => window.pressed")
        finally:
            await applier.stop()

    r, pressed = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason)
    assert "take its assessment" in r.reason and "\u201cTake Assessment\u201d" in r.reason, r.reason
    assert "assessment" not in pressed, pressed


@pytest.mark.parametrize("late", [False, True])
def test_a_page_that_asks_to_be_refreshed_is_reloaded_once(srv, monkeypatch, job_apply_home, late):
    """KLA's Workday, right after the desk signed in (live, Oct 2026): "Something went wrong. Please
    refresh the page and then try again." The desk stopped there ("couldn't find the button that moves
    this application on"). It reloads the page, once, says so, and goes on with the application. And
    the reloaded page, a new one, is waited on for its late buttons, whatever was waited on before it
    (the nightly of Oct 12: KLA's drew its application late after the reload, and the desk stopped)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    page = fixture_url("site/workday-went-wrong.html") + ("?late" if late else "")
    job = srv.add_job(url=page, title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    said = [line for line in r.log if "asked for the page to be refreshed, so I reloaded it" in line]
    assert len(said) == 1 and r.seen_form and "went-wrong" not in r.url, (r.url, r.reason, r.log)


@pytest.mark.parametrize("query", ["?again", "?empty", "?empty&bare", "?later"])
def test_a_page_that_asks_to_be_refreshed_again_after_its_reload_is_said_for_what_it_is(srv, monkeypatch, job_apply_home,
                                                                                       query):
    """Circle K's Workday (run 63): its Take Assessment step said "Something went wrong. Please refresh the page and
    then try again." The desk reloaded it once, went through the steps again, and the step said the same; the desk
    then said "I couldn't find the button that moves this application on". Asked again after its one reload, it says
    the site went wrong again, and that it's the site's. Northern Trust's (run 71, "empty"): reloaded, the page came
    back only the site's header and footer, and the desk said the same "couldn't find the button": it's said to have
    come back without the application, the site's (with no heading at all too: "bare"), and never reloaded again for
    its empty application frame (the review of 0.3.172). Only the page the reload brought back: a step after it that
    went on to such a page ("later") isn't the site's (the review of 0.3.158), but a step still loading, as Grand Canyon
    University's (run 91): reloaded once, then said."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "STEP_LOAD_WAIT", 2)
    job = srv.add_job(url=fixture_url("site/workday-went-wrong.html") + query, title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    if query == "?later":
        assert sum("so I reloaded it" in line for line in r.log) == 2, r.log
        assert any("still loading" in line and "reloaded" in line for line in r.log), r.log
        assert (r.status, r.need) == ("needs_you", "stuck") and "didn't finish loading" in r.reason \
            and "came back without the application" not in r.reason, (r.reason, r.log)
        return
    assert sum("so I reloaded it" in line for line in r.log) == 1, r.log
    said = "said \u201cSomething went wrong\u201d again" if query == "?again" else "came back without the application"
    assert (r.status, r.need) == ("needs_you", "stuck") and said in r.reason, (r.reason, r.log)
    assert "then press Carry on" in r.reason and "couldn't find the button" not in r.reason, r.reason


@pytest.mark.parametrize("query", ["?signin", "?signin&heal"])
def test_an_empty_frame_after_signing_in_past_a_refresh_is_a_step_still_loading(srv, monkeypatch, job_apply_home, query):
    """The review of 0.3.172: Workday said "Something went wrong" (KLA's, after its Apply Manually), the reload brought
    back its sign-in, and the desk signed in with the saved password to an empty application frame (Grand Canyon
    University's and Blue Cross Blue Shield's after their sign-in). A sign-in isn't a press, so that page was taken for
    the one the refresh brought back: never reloaded, and said to have come back without the application. It's a step
    still loading: reloaded once (heal: then it draws and goes on), then said."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "STEP_LOAD_WAIT", 2)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/workday-went-wrong.html") + query, title="HR Generalist",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert any("still loading" in line and "reloaded" in line for line in r.log), (r.reason, r.log)
    if "heal" in query:
        assert r.status == "ready", (r.status, r.need, r.reason, r.log)
    else:
        assert (r.status, r.need) == ("needs_you", "stuck") and "never came up" in r.reason \
            and "came back without the application" not in r.reason, (r.reason, r.log)


@pytest.mark.parametrize("moved", [False, True])
def test_a_page_still_empty_after_resume_is_still_said_to_be_the_sites(srv, monkeypatch, job_apply_home, moved):
    """Northern Trust's and Axcelis' Workday (runs 88 and 90): reloaded for its "Something went wrong", the page came
    back with only the site's header and footer, said as the site's trouble; pressed Resume on, it was still so, and the
    desk said "I couldn't find the button that moves this application on", as if the way on were the desk's to find.
    It's said again as the site's, and nothing is reloaded or pressed for it. The review of 0.3.172 ("moved"): the person
    went back to the posting before Resume, and its Apply, pressed by the desk, led to an empty application frame again:
    that one's said as a page that didn't finish loading (never reloaded after a Resume), not as no way on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "STEP_LOAD_WAIT", 2)
    job = srv.add_job(url=fixture_url("site/workday-went-wrong.html") + "?empty", title="HR Analyst",
                      company="Example Bank")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            first = r.reason
            if moved:  # (the person, in the browser)
                await r.page.goto(fixture_url("site/workday-apply-unmounted.html"))
            applier.resume(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, first
        finally:
            await applier.stop()

    r, first = run(go())
    assert "came back without the application" in first, (first, r.log)
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.reason, r.log)
    assert "couldn't find the button" not in r.reason, (r.reason, r.log)
    if moved:
        assert any("clicked \u201cApply\u201d" in line for line in r.log), r.log
        assert "didn't finish loading" in r.reason and "never came up" in r.reason, (r.reason, r.log)
    else:
        assert "still shows only its header and footer" in r.reason, (r.reason, r.log)
    assert sum("so I reloaded it" in line for line in r.log) == 1, r.log


@pytest.mark.parametrize("query", ["", "?heal"])
def test_a_posting_page_with_no_way_on_is_reloaded_once_then_said(srv, monkeypatch, job_apply_home, query):
    """The State of Arizona's posting page (runs 61 and 63; in run 62 it drew): the block with its APPLY came back
    "Content missing", and the desk said "I couldn't find the button that moves this application on". A posting page
    with no way on, before anything was pressed, is reloaded once (heal: then it draws its Apply and goes on), and a
    part that says "Content missing" is said for what it is."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/posting-content-missing.html") + query, title="HR Analyst",
                      company="Example State")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert sum("showed no way on to the application, so I reloaded it" in line for line in r.log) == 1, r.log
    if query:
        assert r.seen_form and "content-missing" not in r.url, (r.url, r.reason, r.log)
    else:
        assert (r.status, r.need) == ("needs_you", "stuck") and "\u201cContent missing\u201d, even after I reloaded it" \
            in r.reason, (r.reason, r.log)
        assert "couldn't find the button" not in r.reason, r.reason


@pytest.mark.parametrize("query", ["", "?heal"])
def test_an_empty_posting_page_is_reloaded_once_then_said(srv, monkeypatch, job_apply_home, query):
    """Linde's Cornerstone requisition page (run 75; in runs 62 and 77 it drew): nothing of the job on it but the site's
    bar with its language list, unlabelled and empty. The list made the page a form to the desk, so it was never reloaded
    as a posting page with no way on is, and the desk said "I couldn't find the button". A page's language list isn't
    the application's: the page is reloaded once (heal: then it draws its Apply and goes on), and still empty, it's
    said for what it is."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/posting-empty.html") + query, title="Engineering Manager",
                      company="Example Gas")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert sum("showed no way on to the application, so I reloaded it" in line for line in r.log) == 1, r.log
    if query:
        assert r.seen_form and "posting-empty" not in r.url, (r.url, r.reason, r.log)
    else:
        assert (r.status, r.need) == ("needs_you", "stuck") and "came up empty, even after I reloaded it" in r.reason, \
            (r.reason, r.log)


def test_a_pages_own_language_picker_never_makes_it_a_form():
    """What kind of page it is (classify, on the desk's own looks and on its peeks at a paused tab alike) leaves out the
    page's language picker, while the page as read keeps it (the review of 0.3.161: left out of the desk's looks alone,
    a paused tab's peek, which has it, read as moved on, and a press's summary never matched the look before it). A
    list the page requires, or a box of another kind with that label, is a question."""
    lang = {"id": "l", "kind": "select", "label": "", "options": []}
    picker = {"id": "p", "kind": "combobox", "label": "Language", "value": "English (US)"}
    verify = "Verify your email. We sent a link to your email address: open it to carry on."
    assert classify({"fields": [lang, picker], "actions": []}, "Powered by Example Talent Cloud.") == "page"
    assert classify({"fields": [lang], "actions": []}, verify) == "email_code"
    assert classify({"fields": [{**picker, "required": True}]}, "") == "form"
    assert classify({"fields": [{**picker, "invalid": True}]}, "") == "form"
    assert classify({"fields": [{"id": "t", "kind": "text", "label": "Language"}]}, "") == "form"
    assert classify({"fields": [{**lang, "label": "Country", "options": ["United States"]}]}, "") == "form"


def test_a_page_a_sign_in_led_to_is_never_reloaded_for_having_no_way_on(srv, monkeypatch):
    """The review of 0.3.152: a page with no way on is reloaded only where it's the posting's own, opened on this pass
    with nothing done on it since. A page the desk's sign-in led to (its form's POST answered with a candidate home)
    is never reloaded: a reload may send the sign-in again."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    saved_password(monkeypatch)
    login = "https://careers.example-advisors.example/login"
    pages = {login: '<html><body><h1>Sign In</h1><form method="post" action="/login"><label for="em">Email</label>'
                    '<input id="em" name="em" type="email"><label for="pw">Password</label><input id="pw" name="pw" '
                    'type="password"><button type="submit">Sign In</button></form></body></html>',
             ("POST", login): '<html><body><h1>Welcome back to your candidate home</h1><p>Your applications and saved '
                              'jobs are listed here.</p><a href="#help">Help</a></body></html>'}
    posts: list[str] = []
    job = srv.add_job(url=login, title="HR Business Partner", company="Example Advisors")["job"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, posts)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert posts == [login], (posts, r.log)  # (the sign-in sent once)
    assert not any("showed no way on to the application" in line for line in r.log), r.log
    # (and it got as far as the page with no way on, where the reload would have been)
    assert (r.status, r.need) == ("needs_you", "stuck") and "couldn't find the button" in r.reason, (r.reason, r.log)


# An application page that came up blank: the site's app shell, with no headings, boxes or buttons (Eightfold's apply
# page after its Apply Now, issue #112, note 40), and words that say nothing (more than a blank page's few)
_BLANK_SHELL = ('<html><head><title>Careers</title></head><body><div id="root"></div><div class="legal">Example Fab '
                'careers site. Copyright 2026 Example Fab. All rights reserved.</div></body></html>')
_BLANK_FORM = ('<html><head><title>Careers</title></head><body><h2>Apply</h2><form id="f" novalidate>'
               '<label for="fn">First Name</label><input id="fn" required><label for="em">Email</label>'
               '<input id="em" type="email" required><button type="submit">Save and Continue</button></form>'
               "<script>document.getElementById('f').addEventListener('submit', (e) => { e.preventDefault(); "
               "location.href = '/review'; });</script></body></html>")
_BLANK_REVIEW = ('<html><head><title>Review</title></head><body><h2>Review</h2><p>Check your application.</p><form '
                 'onsubmit="event.preventDefault()"><button type="submit">Submit</button></form></body></html>')


async def _serve_counted(srv, pages: dict, gets: dict, posts: list) -> None:
    """Serve `pages` by address (a ("POST", address) key for a POST's answer), counting each address's GETs: a page
    given as a list is its answers to each GET in turn, its last one for every GET after."""
    async def handler(route):
        req = route.request
        url = req.url.split("#")[0]
        if req.method == "POST":
            posts.append(url)
            body = pages.get(("POST", url), "<html><body>not found</body></html>")
        else:
            gets[url] = gets.get(url, 0) + 1
            body = pages.get(url, "<html><body>not found</body></html>")
            if isinstance(body, list):
                body = body[min(gets[url], len(body)) - 1]
        await route.fulfill(status=200, content_type="text/html", body=body)

    await srv.browser.page()
    await srv.browser._ctx.route("https://**.example/**", handler)


@pytest.mark.parametrize("blanks", [1, 2, 9])
def test_a_page_a_press_led_to_that_came_up_blank_is_loaded_again_before_asking(srv, monkeypatch, blanks):
    """Issue #112, note 40: after Eightfold's Apply Now, its apply page (careers/apply?pid=...) stayed blank white: the
    app shell, no headings, boxes or buttons. The desk said "I couldn't find the button that moves this application
    on"; the person reloaded it, the form came, and they pressed Resume. A page a press led to with nothing drawn on
    it didn't load: after the wait it's loaded again (up to twice), and goes on once it draws; still blank, the card
    says it loaded blank and to reload it. The person's Resume on it is never loaded again by the desk."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "BLANK_PAGE_WAIT", 2)
    base = "https://careers.example-fab.example"
    posting, apply = f"{base}/careers/job/1", f"{base}/careers/apply?pid=1"
    pages = {posting: '<html><head><title>Careers</title></head><body><h1>Equipment Technician</h1><p>Chandler, AZ.'
                      '</p><a href="/careers/apply?pid=1">Apply Now</a></body></html>',
             apply: [_BLANK_SHELL] * blanks + [_BLANK_FORM], f"{base}/review": _BLANK_REVIEW}
    gets: dict[str, int] = {}
    posts: list[str] = []
    job = srv.add_job(url=posting, title="Equipment Technician", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        await _serve_counted(srv, pages, gets, posts)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            first = (r.status, r.need, r.reason, gets.get(apply))
            if r.status == "needs_you":
                applier.resume(job["id"])  # (Resume pressed without a reload: the desk doesn't load it again either)
                await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, first
        finally:
            await applier.stop()

    r, (status, need, reason, loads) = run(go())
    again = [line for line in r.log if "came up blank after “Apply Now”" in line and "so I loaded it again"
             in line]
    if blanks <= 2:
        assert status == "ready" and r.seen_form, (status, reason, r.log)
        assert len(again) == blanks and loads == blanks + 1, (again, loads, r.log)
    else:
        assert (status, need) == ("needs_you", "stuck") and "couldn't find the button" not in reason, (reason, r.log)
        assert reason.startswith("Example Fab's site's page loaded blank after “Apply Now”") \
            and "even after I loaded it again twice" in reason and "reload the page" in reason, reason
        assert len(again) == 2 and loads == 3, (again, loads, r.log)
        # the person's Resume, the page still blank: said again, never loaded again by the desk
        assert (r.status, r.need) == ("needs_you", "stuck") and "loaded blank" in r.reason, (r.reason, r.log)
        assert "even after" not in r.reason and gets.get(apply) == 3, (r.reason, gets)
    assert posts == []


def test_a_blank_page_a_forms_post_led_to_is_loaded_again_without_sending_the_form_again(srv, monkeypatch):
    """A blank page a step's POST answered (issue #112, note 40's reload, after a press): loaded again by its address,
    so the step isn't sent a second time, as a reload of a POST's answer would."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "BLANK_PAGE_WAIT", 2)
    base = "https://careers.example-fab.example"
    step, sent = f"{base}/apply/step1", f"{base}/apply/step2"
    pages = {step: '<html><head><title>Apply</title></head><body><h2>Your Details</h2><form method="post" '
                   'action="/apply/step2"><label for="fn">First Name</label><input id="fn" name="fn" required>'
                   '<button type="submit">Next</button></form></body></html>',
             ("POST", sent): _BLANK_SHELL, sent: _BLANK_REVIEW}
    gets: dict[str, int] = {}
    posts: list[str] = []
    job = srv.add_job(url=step, title="Equipment Technician", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        await _serve_counted(srv, pages, gets, posts)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and r.url == sent, (r.status, r.reason, r.log)
    assert posts == [sent] and gets.get(sent) == 1, (posts, gets, r.log)  # (the step sent once; loaded again by a GET)
    assert sum("came up blank after “Next”" in line for line in r.log) == 1, r.log


def test_a_blank_shell_a_press_led_to_gets_the_blank_pages_wait_before_its_loaded_again(srv, monkeypatch):
    """The review of note 40's reload: an app shell with words enough not to be a blank page (its footer) had only the
    wait for a page's late buttons before it was loaded again. It gets the blank page's wait: one that draws its form
    within it is never loaded again."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "BLANK_PAGE_WAIT", 10)
    base = "https://careers.example-fab.example"
    posting, apply = f"{base}/careers/job/1", f"{base}/careers/apply?pid=1"
    late = _BLANK_SHELL.replace("</body>", "<script>setTimeout(() => { document.open(); document.write("
                                + json.dumps(_BLANK_FORM).replace("</", "<\\/") + "); document.close(); }, 3000);</script></body>")
    pages = {posting: '<html><head><title>Careers</title></head><body><h1>Equipment Technician</h1>'
                      '<a href="/careers/apply?pid=1">Apply Now</a></body></html>', apply: late, f"{base}/review": _BLANK_REVIEW}
    gets: dict[str, int] = {}
    job = srv.add_job(url=posting, title="Equipment Technician", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        await _serve_counted(srv, pages, gets, [])
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and gets.get(apply) == 1, (r.status, r.reason, gets, r.log)
    assert not any("so I loaded it again" in line for line in r.log), r.log


def test_a_blank_shell_that_turns_busy_is_waited_out_then_loaded_again(srv, monkeypatch):
    """The second review of note 40's reload: a blank page that showed a loading indicator only on the second look was
    stopped on at once ("reload it"). Busy, it's waited out as a blank page is, then loaded again as any other."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "BLANK_PAGE_WAIT", 3)
    base = "https://careers.example-fab.example"
    posting, apply = f"{base}/careers/job/1", f"{base}/careers/apply?pid=1"
    busy = _BLANK_SHELL.replace("</body>", "<script>setTimeout(() => document.body.insertAdjacentHTML('beforeend', "
                                "'<div aria-busy=\"true\" style=\"width:20px;height:20px\"></div>'), 1500);</script></body>")
    pages = {posting: '<html><head><title>Careers</title></head><body><h1>Equipment Technician</h1>'
                      '<a href="/careers/apply?pid=1">Apply Now</a></body></html>',
             apply: [busy, _BLANK_FORM], f"{base}/review": _BLANK_REVIEW}
    gets: dict[str, int] = {}
    job = srv.add_job(url=posting, title="Equipment Technician", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        await _serve_counted(srv, pages, gets, [])
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and gets.get(apply) == 2, (r.status, r.reason, gets, r.log)
    assert sum("so I loaded it again" in line for line in r.log) == 1, r.log


def test_another_jobs_draft_is_never_gone_into_as_this_one(srv, monkeypatch):
    """The review of note 38: a page with one Continue Application that nothing says is this job's (the job's posting
    sent on to a candidate home listing another job's draft) is never pressed: another job's application would be
    filled, and its review page shown, as this job's. The desk stops and says so."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    base = "https://careers.example-fab.example"
    posting, home, other = (f"{base}/job/Equipment-Technician_R100", f"{base}/userHome",
                            f"{base}/job/Process-Engineer_R200/apply")
    pages = {posting: "<html><body><script>location.replace('/userHome')</script></body></html>",
             home: '<html><head><title>Candidate Home</title></head><body><h2>My Applications</h2><p>Process Engineer: '
                   'not submitted</p><a role="button" href="/job/Process-Engineer_R200/apply">Continue Application</a>'
                   '</body></html>',
             other: "<html><body><h2>My Information</h2><label for='n'>First Name</label><input id='n'></body></html>"}
    gets: dict[str, int] = {}
    job = srv.add_job(url=posting, title="Equipment Technician", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        await _serve_counted(srv, pages, gets, [])
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck") and "nothing on the page says it's this job's" in r.reason, \
        (r.reason, r.log)
    assert other not in gets and not r.draft, (gets, r.log)


def test_a_page_with_only_a_bot_check_or_a_message_is_never_blank():
    """What _nothing_drawn takes for a page that didn't load: nothing drawn, and no words of its own that say why."""
    shell = {"url": "https://careers.example-fab.example/apply", "fields": [], "actions": [], "headings": []}
    words = "Example Fab careers site. Copyright 2026 Example Fab. All rights reserved."
    assert pipeline._nothing_drawn(shell, words) and pipeline._nothing_drawn(shell, "")
    assert not pipeline._nothing_drawn({**shell, "captcha": "reCAPTCHA"}, "")
    assert not pipeline._nothing_drawn({**shell, "challenge": True}, "")
    assert not pipeline._nothing_drawn(shell, "Protected by reCAPTCHA. " + words)
    assert not pipeline._nothing_drawn(shell, "Checking your browser before accessing the site.")
    assert not pipeline._nothing_drawn(shell, "Sorry, this position has been filled.")
    assert not pipeline._nothing_drawn(shell, "Oops, something went wrong. Technical Details: 500")
    assert not pipeline._nothing_drawn({**shell, "headings": ["Apply"]}, words)
    assert not pipeline._nothing_drawn({**shell, "actions": [{"id": "1", "text": "Apply"}]}, words)
    assert not pipeline._nothing_drawn({**shell, "errors": ["Please enter your email address"]}, words)


def test_a_review_page_whose_submit_is_greyed_while_it_draws_is_waited_on(srv, monkeypatch, job_apply_home):
    """Wells Fargo's Workday (run 49, Oct 13): its review page drew Submit greyed out for a moment, and the desk
    stopped with "“Submit” is greyed out, so the site still wants something" though the page wanted nothing. A
    button greyed out while a page draws is waited on, and the page as it is after the wait is the one read."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 6)
    job = srv.add_job(url=fixture_url("site/late-submit-step.html"), title="Teller", company="Example Bank")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and r.url.endswith("late-submit-review.html"), (r.status, r.reason, r.log)


def test_only_a_greyed_submit_or_step_that_comes_on_ends_the_wait():
    """The wait on a page drawing late ends when a Submit or step button that was greyed out comes on (Wells Fargo,
    run 49). Not when another button does, nor one aside from the form, nor a greyed twin of a button already on
    (Workday draws a covered copy of its Submit)."""
    def page(*actions):
        return {"actions": [dict(a) for a in actions]}
    submit, grey = {"text": "Submit", "is_submit": True}, {"text": "Submit", "is_submit": True, "disabled": True}
    assert pipeline._came_on(page(grey), page(submit))
    assert pipeline._came_on(page({"text": "Next", "disabled": True}), page({"text": "Next"}))
    assert not pipeline._came_on(page(grey), page(grey))
    assert not pipeline._came_on(page(grey, submit), page(submit))  # (its twin was on already)
    assert not pipeline._came_on(page({"text": "Download", "disabled": True}), page({"text": "Download"}))
    aside = {"text": "Submit", "is_submit": True, "aside": True}
    assert not pipeline._came_on(page(dict(aside, disabled=True)), page(aside))
    assert not pipeline._came_on(page(), page(submit))  # (a button that wasn't there isn't one that came on)


@pytest.mark.parametrize("save", [6000, 60000])
def test_a_step_that_goes_on_while_its_next_is_greyed_is_read_again(srv, monkeypatch, job_apply_home, save):
    """EMD's Phenom (run 51's practice check, Oct 13): Next, pressed, was greyed out while the step was saved, and the
    site went on to its next step at the same address but its query, with fewer boxes and Submit, no Next. The wait
    for the step's button ended only on that button, more boxes or a loading sign, so it ran out, and the desk stopped
    on the first step as it had read it: "“Next” is greyed out, so the site still wants something". A page that goes
    on while the desk waits is read again; one whose Next stays greyed past the wait is still said to want something
    (the review)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 2)  # (EMD's save took longer than this wait)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 12)
    job = srv.add_job(url=fixture_url("site/greyed-next-then-step.html") + f"?save={save}", title="Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    if save > 12000:
        assert r.need == "stuck" and "“Next” is greyed out, so the site still wants something" in r.reason, r.reason
        return
    assert r.status == "ready" and "step=2" in r.url, (r.status, r.reason, r.log)


def test_an_optional_question_the_step_says_it_wants_at_once_is_asked(srv, monkeypatch, job_apply_home):
    """Baker Tilly's Workday (run 53, Oct 13): a question nothing marked required, whose choices lack the profile's
    answer, was left empty; Save and Continue said at once that the page wants it ("Errors Found", "Error-Please
    indicate if you are Hispanic or Latino?"), and the desk stopped: "The page didn't move on ... Fix it in the
    browser". Said later (run 51), the question was asked. It's asked either way."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/optional-flagged-at-once.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "questions" and [q["label"] for q in r.questions] == ["Gender"], (r.need, r.reason, r.log)


@pytest.mark.slow
def test_an_application_of_many_steps_reaches_its_review_page(srv, monkeypatch, job_apply_home):
    """Avnet's Workday (run 53, Oct 13): its account, then seven steps, some passes over a page waiting on it; the
    desk's 15 passes ran out as the review page came ("This application has more steps than I expected")."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/many-steps.html") + "?n=18", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=180, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and r.url.endswith("review.html"), (r.status, r.reason, r.log[-3:])


@pytest.mark.parametrize("save", ["", "?save=3000"], ids=["at-once", "slow-save"])
def test_a_questionnaire_asking_one_question_at_a_time_at_one_address_goes_on(srv, monkeypatch, job_apply_home, save):
    """The review of Oct 15: a questionnaire drawing its next question in place (one address, one heading, one box, a
    Next) looked like one step pressed again and again, and the desk stopped ("The page didn't move on") with
    questions still to go. Which questions a step shows is part of it: each question is a step of its own. One that
    takes a few seconds to save each answer (?save) is waited on until its next question is drawn (the review of
    0.3.189: the wait looked only at the address and headings)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/questions-one-address.html") + save, title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and r.url.endswith("review.html"), (r.status, r.reason, r.log[-4:])
    assert sum(line == "clicked “Next”" for line in r.log) >= 4, r.log


@pytest.mark.slow
def test_a_stuck_step_under_a_ticking_label_is_still_seen_stuck(srv, monkeypatch, job_apply_home):
    """The review of 0.3.189: a box label with a counter ticking in it ("Reference (expires in 600s)") changed on every
    look, so a step whose Next did nothing never looked stuck, and Next was pressed thirty times. A label's numbers
    don't make it another question."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/ticking-label.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r, await r.page.evaluate("() => window.presses || 0")
        finally:
            await applier.stop()

    r, presses = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck") and "didn't move on" in r.reason, (r.reason, r.log[-3:])
    assert presses <= 4, presses


def test_a_pass_over_a_job_that_runs_too_long_is_said_and_the_queue_goes_on(srv, monkeypatch, job_apply_home):
    """The review of Oct 15: a pass over a job's pages had a count of steps (MAX_STEPS) and no time limit, and its waits
    (a page's buttons, a step saving, a list's choices) can add up to minutes a step: one job could hold the queue for
    an hour. A pass that runs past PASS_SECONDS stops where it is and says so."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "PASS_SECONDS", 2)
    job = srv.add_job(url=fixture_url("site/many-steps.html") + "?n=18", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck") and "taken longer than" in r.reason, (r.status, r.reason)
    assert "press Carry on" in r.reason and not r.url.endswith("review.html"), r.reason


def test_a_last_press_that_reaches_the_review_page_after_the_time_limit_goes_to_it(srv, monkeypatch, job_apply_home):
    """The review of 0.3.189: the pass's time limit was looked at before reading the page, so a last press that
    reached the review page as the time ran out was said to be stuck, and Submit for me didn't go. It's looked at
    only before a press."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 2)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 12)
    monkeypatch.setattr(pipeline, "PASS_SECONDS", 4)
    job = srv.add_job(url=fixture_url("site/greyed-next-then-step.html") + "?save=6000", title="Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and "step=2" in r.url, (r.status, r.reason, r.log[-3:])


def test_a_workday_date_the_page_lost_is_typed_in_again_as_a_date(srv, monkeypatch, job_apply_home):
    """Entegris' and Analog Devices' Workday Self Identify (runs 53 and 54, Oct 13): the signature Date showed
    10/13/2026, typed in, but Save and Continue said "The field Date is required and must have a value"; its parts
    went to the boxes typed again one by one, which takes no date's part, and the desk stopped. A date's parts the
    errors name are filled again from the profile, as a date is."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/workday-date-lost.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and r.url.endswith("review.html"), (r.status, r.reason, r.log[-4:])


def test_a_workday_date_the_person_gave_that_the_page_lost_is_typed_in_again(srv, monkeypatch, job_apply_home):
    """Robert Half's Workday (run 72): "Anticipated eligibility time for employment?", a date the profile doesn't
    answer, was given on the desk as 10/14/2026, went in and showed so, and Next said "The field ... is required and
    must have a value". Only a profile's date was typed again; the person's stayed as it was, and the desk stopped
    with "needs fixing". Theirs is typed again as a date is, once a page."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/workday-date-lost.html") + "?asked", title="Talent Manager",
                      company="Example Staffing")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            assert (r.status, r.need) == ("needs_you", "questions"), (r.status, r.reason, r.log[-3:])
            assert [q.get("sublabel") for q in r.questions] == ["Month", "Day", "Year"], r.questions
            assert all(q.get("role") == "spinbutton" for q in r.questions), r.questions  # (the card's: a date's parts)
            for q in r.questions:  # (as the desk's card keys a date's parts: each by its own box)
                r.once[pipeline.placed_key(q)] = {"Month": "10", "Day": "14", "Year": "2026"}[q["sublabel"]]
            applier.enqueue(job["id"], front=True)
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and r.url.endswith("review.html"), (r.status, r.reason, r.log[-4:])
    assert ("typed your answer again where the page said it was missing: Anticipated eligibility time for "
            "employment") in r.log, r.log


def test_a_date_the_person_gave_is_typed_again_only_where_the_page_still_shows_it():
    """The review of 0.3.159: the person's date is typed again only in the parts that still show their answer. Never
    over a part they have changed in the browser since, nor with an answer from another page, nor a part asked alone
    (answered by the date's label) put into the date's other parts."""
    data = {"url": "https://example.com/apply", "headings": ["Application Questions", "Errors Found"]}

    def part(sub: str, value: str) -> dict:
        return {"id": sub[0].lower(), "label": "Start date*", "sublabel": sub, "role": "spinbutton", "kind": "text",
                "value": value}

    month, day, year = part("Month", "10"), part("Day", "14"), part("Year", "2026")
    r = Run(1, "Technician", "Example Fab")
    for f, given in ((month, "10"), (day, "14"), (year, "2026")):
        r.once[pipeline.placed_key(f)] = given
    assert pipeline._lost_answers(r, data, [month, day, year]) == {"m": "10", "d": "14", "y": "2026"}
    assert pipeline._lost_answers(r, data, [{**month, "value": "11"}, day, year]) == {"d": "14", "y": "2026"}
    r.once_page[pipeline.placed_key(month)] = ("https://example.com/other", ("Other Questions",))
    assert pipeline._lost_answers(r, data, [month, day, year]) == {"d": "14", "y": "2026"}
    alone = Run(2, "Technician", "Example Fab")
    alone.once[question_key("Start date*")] = "14"  # (its Day asked alone, its Month and Year typed in the browser)
    assert pipeline._lost_answers(alone, data, [month, day, year]) == {"d": "14"}


def test_a_box_whose_own_words_say_little_is_asked_with_the_words_above_it(srv, monkeypatch, job_apply_home):
    """Taylor Morrison's Workday (runs 71 and 73): a required box labelled only "Acknowledgment*", whose paragraph above
    acknowledges the employer's privacy notice and certifies that the applicant shared it with their emergency
    contacts. A fact about the person: never ticked for them. The card asked "Terms and Conditions: Acknowledgment"
    with nothing to say what it acknowledges; it now shows the site's words above the box."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/workday-acknowledgment.html"), title="Benefits Coordinator",
                      company="Example Homes")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            ticked = await r.page.evaluate(
                "document.getElementById('termsAndConditions--acceptTermsAndAgreements').checked")
            return r, ticked
        finally:
            await applier.stop()

    r, ticked = run(go())
    assert (r.status, r.need) == ("needs_you", "questions") and not ticked, (r.status, r.reason, ticked, r.log)
    [q] = r.questions
    assert q["label"].startswith("Acknowledgment"), q
    assert q.get("about", "").endswith("I also certify that I have shared a copy of it with my emergency contacts "
                                       "before giving their details."), q
    assert "Acknowledgment" not in q["about"], q  # (its own label isn't the words above it)


def test_a_workday_step_whose_questions_come_late_isnt_a_list_of_jobs(srv, monkeypatch, job_apply_home):
    """Wells Fargo's Workday (run 55, Oct 13): every step is at the same address, and Application Questions drew its
    questions a moment after its heading. With no boxes there yet, the two steps' Save and Continue before it read as
    "next" on a list of jobs pressed again and again, and the desk stopped. Only presses on pages with no form count."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/workday-late-questions.html"), title="Coordinator", company="Example Bank")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and r.url.endswith("review.html"), (r.status, r.reason, r.log[-3:])


def test_a_workday_step_that_says_what_it_saved_already_exists_is_reloaded(srv, monkeypatch, job_apply_home):
    """PayPal's Workday (run 56) and Grand Canyon Education's (run 55): My Information's first Save and Continue saved
    the phone number (or the address) but the step stayed; pressed again, "A phone number already exists for this
    application. Update or delete the phone number.", and the desk stopped. The page is reloaded, once a page, and
    shows what it saved; the step goes on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/workday-saved-twice.html"), title="Engineer", company="Example Pay")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and r.url.endswith("review.html"), (r.status, r.reason, r.log[-3:])
    assert any("already there" in line for line in r.log), r.log


def test_each_page_that_draws_its_buttons_late_is_waited_on(srv, monkeypatch, job_apply_home):
    """Baker Tilly's Workday (run 44, Oct 13): the page after its sign-in drew its questions and Save and Continue
    late, and the desk, having waited on an earlier page's late buttons on that pass, stopped at once: "I couldn't
    find the button that moves this application on". Each page is waited on once."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 4)
    job = srv.add_job(url=fixture_url("site/late-twice.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and r.url.endswith("review.html"), (r.status, r.reason, r.log)


def test_a_refresh_the_site_asked_for_midway_is_no_circle(srv, monkeypatch, job_apply_home):
    """Circle K's Workday (run 44, Oct 13): Save and Continue on Voluntary Disclosures said "Something went wrong.
    Please refresh the page", and the page reloaded went back to the first step. The desk, going through again, took
    that for going round in a circle and stopped. A refresh the site asked for starts a new lap (once a job), and the
    steps it takes again aren't counted against the application's (the review's)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAX_STEPS", 5)  # (3 steps, the refresh, then the same 3 and the review page)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/workday-went-wrong-midway.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "round in a circle" not in r.reason, (r.status, r.reason, r.log)
    assert r.status == "ready" and r.url.endswith("review.html"), (r.status, r.reason, r.log)
    assert sum("asked for the page to be refreshed" in line for line in r.log) == 1, r.log


def test_a_workday_sign_in_presses_the_cover_over_its_hidden_button(srv, monkeypatch, job_apply_home):
    """KLA's Workday (live, Oct 2026) draws its Sign In as a hidden "Sign In" button (aria-hidden) under a
    div of its own named "Submit" (data-automation-id="click_filter") that takes the click. The desk
    pressed the hidden button, which does nothing and says nothing, and went on as if the password had
    been refused: a reset, then a Create Account, every time, though the account signed in fine in the
    owner's own browser. The cover is read as the button it covers, and pressed."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/workday-account.html") + "?cover", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await workday_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    assert (did.get("created"), did.get("signIns")) == (1, 2), (did, r.log)
    assert any(line.startswith("pressed \u201cSign In\u201d") for line in r.log) and r.seen_form, (r.reason, r.log)


@pytest.mark.parametrize("company, ticked", [("Example Fab", True), ("Intel", False)])
def test_a_workday_create_account_says_a_new_candidate_from_the_profile(srv, monkeypatch, job_apply_home, company, ticked):
    """Banner Health's Workday (live, Oct 2026) made no account, and said nothing, until its "Yes, I am a
    new candidate and not a current employee" box was ticked (not marked required). It's ticked when
    the profile has no current job at the employer (an old one there doesn't count: Example Fab
    Services ended in 2021); someone who works there now (Intel, in the test profile) is left to it,
    and the card names the box."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "ACCOUNT_WAIT", 2)  # (a form that says nothing is waited on that long)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/workday-account.html") + "?candidate", title="FSE", company=company)["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await workday_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    assert (did["candidate"], did["privacy"], did.get("created")) == (ticked, True, 1 if ticked else None), (did, r.log)
    pressed = next(line for line in r.log if line.startswith("pressed \u201cCreate Account\u201d"))
    if ticked:
        assert "\u201cYes, I am a new candidate and not a current employee\u201d" in pressed, r.log
        assert any(line.startswith("created your account on") for line in r.log) and r.seen_form, (r.reason, r.log)
    else:
        assert "new candidate" not in pressed, r.log
        assert r.need == "sign_in" and "\u201cYes, I am a new candidate and not a current employee\u201d isn't ticked" in r.reason, (
            r.reason, r.log)


def test_a_workday_create_account_that_doesnt_sign_in_goes_to_a_reset_not_round_again(srv, monkeypatch, job_apply_home):
    """A Workday site, without the inbox watched (live, Oct 2026): the saved password didn't sign in,
    Create Account went back to Sign In, the sign-in failed again, Create Account again, round and
    round across a Resume, until the site said the account might be locked. One refused sign-in and
    one Create Account that didn't take go to the password reset (the person follows its emailed
    link); the counts hold as the queue comes back to the job, and the refused password is pressed at
    most twice (once more after the reset)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/workday-account.html") + "?exists=another-password", title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)
    seen = []

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            while True:  # the reset's email asked for, then back to the job twice
                await until(lambda: r.status not in ("queued", "running"), about=state(r))
                seen.append((r.need, r.reason, await workday_did(r.page)))
                if len(seen) == 3:
                    return r
                applier.enqueue(job["id"], front=True)
        finally:
            await applier.stop()

    r = run(go())
    (need, reason, did), *after = seen
    assert need == "email_code" and "Open the link in the email" in reason, (reason, r.log)
    assert (did["signIns"], did["presses"], did.get("created", 0), did["resets"]) == (2, 1, 0, 1), did
    for need, reason, did in after:  # once more after the reset, then never again
        assert need == "sign_in" and "won't try it again for this job" in reason, (reason, r.log)
        assert "before or after I pressed its Create Account" in reason and "might be locked" in reason, reason
        assert (did["signIns"], did["presses"], did["resets"]) == (3, 1, 1), did
    assert not any("created your account" in line for line in r.log), r.log


@pytest.mark.slow
def test_with_the_inbox_watched_a_workday_create_account_that_doesnt_sign_in_stops(srv, monkeypatch, job_apply_home):
    """With the inbox watched, the reset comes first; no email comes, so the desk makes an account,
    which a Workday site doesn't sign in to either. It stops there, never pressing Create Account or
    the refused password again as the queue comes back to it; a new password saved on the desk is tried."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "RESET_MAIL_WAIT", 4)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=fixture_url("site/workday-account.html") + "?exists=another-password", title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)
    seen = []

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you" and r.need != "email_code", timeout=60, about=state(r))
            seen.append((r.need, r.reason, await workday_did(r.page)))
            applier.enqueue(job["id"], front=True)  # (the queue back to it: a Resume tries once more)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            seen.append((r.need, r.reason, await workday_did(r.page)))
            monkeypatch.setenv("JOB_APPLY_SECRET_TEST_SITE_PASSWORD", "another-password")  # the account's, saved
            applier.enqueue(job["id"], front=True)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    for need, reason, did in seen:
        assert need == "sign_in" and "won't try it again for this job" in reason, (reason, r.log)
        assert (did["signIns"], did["presses"], did["resets"]) == (2, 1, 1), did
    assert not any("created your account" in line for line in r.log), r.log
    assert r.seen_form and r.need != "sign_in", (r.reason, r.log)


def test_a_resume_after_the_password_is_reset_by_hand_signs_in(srv, monkeypatch, job_apply_home):
    """A Workday site refused the saved password until the desk stopped trying, so the account isn't
    locked; its card said to reset the password to the saved one through "Forgot password". The person
    did and pressed Resume, and the desk said the same again without signing in. Each Resume the person
    presses tries the saved password once more (once, refused or not); the queue coming back to the job
    by itself doesn't."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/workday-account.html") + "?exists=another-password", title="FSE",
                      company="Example Corp")["job"]
    applier = Applier(srv)
    seen = []

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            for then in ("resume", "queue", "resume", "reset by hand", None):
                await until(lambda: r.status not in ("queued", "running"), about=state(r))
                seen.append((r.need, r.reason, (await workday_did(r.page))["signIns"]))
                if then == "reset by hand":  # to the saved password, through the site's own Forgot Password
                    await r.page.evaluate("() => sessionStorage.setItem('wd.account', "
                                          "JSON.stringify('not-a-real-password'))")
                if then == "queue":
                    applier.enqueue(job["id"], front=True)
                elif then:
                    applier.resume(job["id"])
            return r
        finally:
            await applier.stop()

    r = run(go())
    (need, reason, tries), *refused, (_, _, signed_in) = seen
    assert need == "email_code" and tries == 2, (reason, r.log)  # the reset the desk asked for
    for need, reason, tries in refused:
        assert need == "sign_in" and "won't try it again for this job unless you press Carry on" in reason, (reason, r.log)
    assert [tries for _, _, tries in refused] == [3, 3, 4], seen  # the queue's pass pressed nothing
    assert signed_in == 5 and r.seen_form and r.need != "sign_in", (r.reason, r.log)


@pytest.mark.parametrize("need, tries", [("sign_in", 1), ("questions", 2), ("stuck", 2)])
def test_only_a_resume_on_a_sign_in_card_presses_a_refused_password_again(srv, need, tries):
    """The sign-in card asks the person to reset the password by hand and press Carry on, and that
    Resume gets one more press of the saved password. A Resume on a question's card or a stuck page's,
    of a job whose saved password was refused twice, gave it one all the same: a third refused sign-in,
    which the guard against a locked account is there to stop."""
    job = srv.add_job(url="https://example.com/a", title="FSE", company="Example Corp")["job"]
    applier = Applier(srv)
    paused = Run(job["id"], "FSE", "Example Corp", status="needs_you", need=need, sign_in_tries=2)
    applier.runs[job["id"]] = paused
    applier.resume(job["id"])
    assert (paused.status, paused.sign_in_tries) == ("queued", tries)


def test_an_account_the_desk_made_is_signed_in_to_after_the_tries_ran_out(srv, monkeypatch, job_apply_home):
    """The saved password was refused twice at this job on earlier passes, and the desk then made the
    account there with it. Its sign-in was never pressed (the job's two were used), so the new account
    went to a password reset instead. An account the desk has just made gets one sign-in."""
    import hashlib

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/workday-account.html"), title="FSE", company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        r = applier.enqueue(job["id"])
        r.sign_in_tries = pipeline.SIGN_IN_TRIES  # (refused on earlier passes)
        r.sign_in_key = hashlib.sha256(b"not-a-real-password").hexdigest()
        applier.start()
        try:
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await workday_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    assert (did["presses"], did.get("created"), did.get("signIns"), did.get("resets")) == (1, 1, 1, None), (did, r.log)
    assert any(line.startswith("created your account on") for line in r.log) and r.seen_form, (r.reason, r.log)


@contextlib.contextmanager
def served_fixtures():
    """The fixtures served over http, so that a link opened in another tab reaches this one through its
    browser's localStorage (a file:// page has none). The address of their folder."""
    import functools
    import http.server
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(Path(__file__).parent / "fixtures"))
    site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{site.server_address[1]}/"
    finally:
        site.shutdown()
        site.server_close()


@pytest.mark.slow
@pytest.mark.parametrize("query", ["?verify", "?verify=late"])
def test_with_the_inbox_a_new_account_to_verify_is_signed_in_to_once_its_link_is_opened(srv, monkeypatch, job_apply_home,
                                                                                       query):
    """Banner Health's Workday (live, Oct 2026, the test identity): the saved password didn't sign in, no
    reset email came, and Create Account went back to Sign In saying "An email has been sent to you.
    Please verify your account". The desk pressed Sign In at once; the unverified account refused it, and
    the job stopped on "your saved password didn't sign in". Right after the desk made the account, such a
    page waits for that email: with the inbox watched, its link (the site's own) is opened in this browser,
    and then the saved password is pressed, once. &late: the page says so only once Sign In is pressed, so
    the job's two sign-ins are used up when the link is opened (a refusal for want of the link isn't a
    wrong password: it doesn't stop the sign-in after the link)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "RESET_MAIL_WAIT", 4)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    asked = []

    with served_fixtures() as site:
        link = f"{site}site/workday-verify.html"

        def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
            asked.append(want)
            assert allowed_link(link) and not allowed_link("https://elsewhere.example.net/verify?t=1")
            return pipeline.mailbox.Found(want, link, "myworkday.com", time.time()) if want == "link" else None

        monkeypatch.setattr(pipeline.mailbox, "search", inbox)
        job = srv.add_job(url=f"{site}site/workday-account.html{query}", title="FSE", company="Example Fab")["job"]
        applier = Applier(srv)

        async def go():
            applier.start()
            try:
                r = applier.enqueue(job["id"])
                await until(lambda: r.seen_form and r.status not in ("queued", "running"), about=state(r))
                return r, await workday_did(r.page)
            finally:
                await applier.stop()

        r, did = run(go())
    log = r.log
    assert "reset" in asked and "link" in asked, asked
    # (one refused before the account was made, one for want of the link if the page didn't say so, one after it)
    assert (did["created"], did["presses"], did["signIns"]) == (1, 1, 2 + query.endswith("late")), (did, log)
    made = next(i for i, line in enumerate(log) if line.startswith("pressed \u201cCreate Account\u201d"))
    waited = next(i for i, line in enumerate(log) if "emailed you a link to verify your new account" in line)
    opened = log.index("opened the confirmation link from your email (sent from myworkday.com)")
    after = [i for i, line in enumerate(log) if line.startswith("pressed \u201cSign In\u201d") and i > waited]
    assert made < waited < opened and len(after) == 1 and opened < after[0], log  # (once the wait began: once, after the link)
    assert sum(line.startswith("created your account on") for line in log) == 1, log
    assert r.need not in ("sign_in", "email_code"), (r.reason, log)
    # (the email it says it just sent is waited for: its "Resend Account Verification" isn't pressed)
    assert did.get("resends", 0) == 0 and not any("Resend" in line for line in log), (did, log)


def test_without_the_inbox_a_new_account_to_verify_waits_for_the_person_and_their_resume(srv, monkeypatch, job_apply_home):
    """The same Workday Sign In, with no inbox to read: the card says to verify the account from the
    email and press Carry on, never that the saved password didn't sign in (it wasn't pressed: the account
    isn't verified yet, and a refusal then counts against the account's locking). The Resume signs in,
    once."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    monkeypatch.setattr(pipeline.mailbox, "search", lambda *a, **k: pytest.fail("the inbox was read"))
    job = srv.add_job(url=fixture_url("site/workday-account.html") + "?create&verify", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            await asyncio.sleep(1.5)  # (the queue's polls: none of them presses Sign In)
            paused = (r.status, r.need, r.reason, await workday_did(r.page))
            await r.page.evaluate("() => sessionStorage.setItem('wd.verified', 'true')")  # the link, opened elsewhere
            applier.resume(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, paused, await workday_did(r.page)
        finally:
            await applier.stop()

    r, (status, need, reason, before), did = run(go())
    assert (status, need) == ("needs_you", "sign_in"), (reason, r.log)
    assert "verify your new account" in reason and "press Carry on" in reason, reason
    assert "didn't sign in" not in reason and "locked" not in reason, reason
    assert (before["created"], before.get("signIns", 0)) == (1, 0), before
    assert did["signIns"] == 1 and r.seen_form and r.need != "sign_in", (did, r.reason, r.log)
    assert sum(line.startswith("created your account on") for line in r.log) == 1, r.log


def test_a_resume_before_the_new_accounts_link_tries_the_password_once_and_waits_again(srv, monkeypatch, job_apply_home):
    """The same new account with no inbox, and a Resume pressed before the link was opened: the saved password
    is tried once (as the card promised), the site refuses it with "An email has been sent to you. Please verify
    your account" again, and the desk waits again, with the same card; never a password reset (the site sends
    no email for an unverified account) or a second Create Account for an email that has one (the review of
    0.3.178). The Resume after the link signs in."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    monkeypatch.setattr(pipeline.mailbox, "search", lambda *a, **k: pytest.fail("the inbox was read"))
    job = srv.add_job(url=fixture_url("site/workday-account.html") + "?create&verify", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            applier.resume(job["id"])  # (too soon: the link isn't opened)
            await until(lambda: r.status not in ("queued", "running") and r.sign_in_tries > 0, about=state(r))
            early = (r.status, r.need, r.reason, await workday_did(r.page))
            await r.page.evaluate("() => sessionStorage.setItem('wd.verified', 'true')")  # the link, opened elsewhere
            applier.resume(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, early, await workday_did(r.page)
        finally:
            await applier.stop()

    r, (status, need, reason, early), did = run(go())
    assert (status, need) == ("needs_you", "sign_in") and "verify your new account" in reason, (reason, r.log)
    assert "didn't sign in" not in reason and "reset" not in reason, reason
    assert (early["created"], early["signIns"], early.get("resets", 0), early.get("presses")) == (1, 1, 0, 1), early
    assert did["signIns"] == 2 and r.seen_form and r.need != "sign_in", (did, r.reason, r.log)
    assert not any("password reset" in line for line in r.log), r.log


@pytest.mark.parametrize("said, refused", [
    ("Verify your account before you sign in or request a verification email.", True),  # Workday's (Vanguard)
    ("You need to verify your account before you can sign in. Request a verification email.", True),
    ("Please verify your email address before logging in.", True),
    ("Your account has not been verified. Check your email for the verification link.", True),
    ("An email has been sent to you. Please verify your account", False),  # a new account's (Banner Health's)
    ("Sign in to this account or enter an email address that isn't already in use.", False),
    ("You may have entered the wrong email address or password or your account might be locked.", False),
])
def test_the_words_of_a_sign_in_refused_for_want_of_the_accounts_verification(said, refused):
    """A sign-in refused because the account was never verified (the site's own alert), as against a new
    account's word that the email is on its way, or any other refusal."""
    assert bool(pipeline._VERIFY_FIRST.search(said)) is refused, said


def test_an_account_there_from_before_that_still_needs_verifying_gets_its_email_sent_again(srv, monkeypatch,
                                                                                            job_apply_home):
    """Vanguard's Workday (live, run 98, the test identity): the account made in an earlier run was never
    verified (its link went unopened), so Sign In was refused with "Verify your account before you sign in or
    request a verification email." and the page offered "Resend Account Verification". The desk waited for an
    email sent days before (and in runs 96 and 97 asked for a password reset instead, which such an account
    gets no email for, then stopped on the sign-in). With the inbox watched it presses that button, once, waits
    for the new email, opens its link and signs in with the saved password, once; no reset is asked for and
    no account made."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    asked = []

    with served_fixtures() as site:
        link = f"{site}site/workday-verify.html"

        def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
            asked.append(want)
            return pipeline.mailbox.Found(want, link, "myworkday.com", time.time()) if want == "link" else None

        monkeypatch.setattr(pipeline.mailbox, "search", inbox)
        job = srv.add_job(url=f"{site}site/workday-account.html?exists=not-a-real-password&verify=stale", title="FSE",
                          company="Example Fab")["job"]
        applier = Applier(srv)

        async def go():
            applier.start()
            try:
                r = applier.enqueue(job["id"])
                await until(lambda: r.seen_form and r.status not in ("queued", "running"), about=state(r))
                return r, await workday_did(r.page)
            finally:
                await applier.stop()

        r, did = run(go())
    log = r.log
    assert "link" in asked and "reset" not in asked, asked
    assert (did.get("resends", 0), did.get("resets", 0), did.get("presses", 0), did["signIns"]) == (1, 0, 0, 2), (did, log)
    resent = next(i for i, line in enumerate(log) if line.startswith("pressed \u201cResend Account Verification\u201d"))
    waited = next(i for i, line in enumerate(log) if "verified from the link it emailed" in line)
    opened = log.index("opened the confirmation link from your email (sent from myworkday.com)")
    sign_ins = [i for i, line in enumerate(log) if line.startswith("pressed \u201cSign In\u201d")]
    assert len(sign_ins) == 2 and sign_ins[0] < resent < waited < opened < sign_ins[1], log
    assert "pressed \u201cResend Account Verification\u201d and is watching your inbox" in log[waited], log[waited]
    assert "didn't sign in" not in "\n".join(log) and r.need not in ("sign_in", "email_code"), (r.reason, log)


def test_without_the_inbox_an_account_that_still_needs_verifying_is_told_with_the_sites_resend_button(srv, monkeypatch,
                                                                                                      job_apply_home):
    """The same refusal with no inbox to read: nothing is pressed for the person (the email is theirs to find,
    or to ask for again), no reset is asked for and no account made; the card says the account there still
    needs verifying and names the site's "Resend Account Verification". Their Resume after the link signs in
    once."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    monkeypatch.setattr(pipeline.mailbox, "search", lambda *a, **k: pytest.fail("the inbox was read"))
    job = srv.add_job(url=fixture_url("site/workday-account.html") + "?exists=not-a-real-password&verify=stale", title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            await asyncio.sleep(1.5)  # (the queue's polls: none of them presses anything)
            paused = (r.status, r.need, r.reason, await workday_did(r.page))
            await r.page.evaluate("() => sessionStorage.setItem('wd.verified', 'true')")  # the link, opened elsewhere
            applier.resume(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, paused, await workday_did(r.page)
        finally:
            await applier.stop()

    r, (status, need, reason, before), did = run(go())
    assert (status, need) == ("needs_you", "sign_in"), (reason, r.log)
    assert "verified from the link it emailed" in reason and "press Carry on" in reason, reason
    assert "(its \u201cResend Account Verification\u201d sends a new one)" in reason, reason
    assert "didn't sign in" not in reason and "locked" not in reason, reason
    assert (before.get("resends", 0), before.get("resets", 0), before.get("presses", 0), before["signIns"]) == (0, 0, 0, 1), before
    assert did["signIns"] == 2 and r.seen_form and r.need != "sign_in", (did, r.reason, r.log)
    assert not any(line.startswith("pressed \u201cResend") for line in r.log), r.log  # (named on the card, never pressed)


@pytest.mark.parametrize("page, query, says", [
    ("create-account-signin.html", "?policy", "it says \u201cYour password must contain at least one symbol\u201d."),
    ("workday-account.html", "?create&adult", "\u201cI am at least 18 years of age\u201d isn't ticked."),
])
def test_a_create_account_that_doesnt_go_through_says_what_the_page_wants(srv, monkeypatch, job_apply_home, page, query,
                                                                          says):
    """Create Account pressed, and the form is still there: the card says what the page says (its
    error), or names the boxes it requires that are still empty, not a guess at a picture code."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "ACCOUNT_WAIT", 2)  # (a form that says nothing is waited on that long)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url(f"site/{page}") + query, title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in" and f"it wants something more: {says} Finish it there" in r.reason, (r.reason, r.log)
    assert "picture code" not in r.reason


@pytest.mark.parametrize("page", ["signin-no-account.html", "signin-no-account-link.html",
                                  "signin-no-account-lost-click.html"])
def test_a_saved_password_that_doesnt_sign_in_opens_create_account(srv, monkeypatch, page):
    """A first application at a Workday employer: there's no account there yet, so the
    saved password can't get in. It's tried once, then Create Account is filled in. The way
    there can be a button, or a link ("Create an account" on Amkor's SuccessFactors page),
    and a click on it made while the failed sign-in reloads the page can be lost (Amkor's)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url(f"site/{page}"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "sign_in" and "Create Account form" in r.reason, (r.reason, r.log)
            assert "first application" in r.reason
            assert r.page.url.endswith("create-account.html")
            filled = await r.page.evaluate("() => [em.value, pw.value === pw2.value && pw.value.length > 0, terms.checked]")
            assert filled == ["sam.rivera@example.com", True, False]  # terms and the button stay with the person
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.log.count("pressed “Sign In” with your saved password") == 1  # not tried again
    assert "your saved password didn't sign in, so I opened Create Account" in r.log


@pytest.mark.parametrize("page, form", [("signin-signup-link.html", "signup.html"),
                                        ("signin-signup-submit.html", "signup-submit.html")])
def test_a_sign_up_form_with_one_password_box_is_filled_in(srv, monkeypatch, page, form):
    """UKG Pro (Nikon Precision): after the saved password doesn't sign in, "Sign up" opens a
    "Create your account" form with one password box. It's filled in; its button (Continue,
    or a "Create Account" that sends the form) is the person's."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url(f"site/{page}"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "sign_in" and "Create Account form" in r.reason, (r.reason, r.log)
            assert "first application" in r.reason
            assert r.page.url.endswith(form)
            filled = await r.page.evaluate("() => [em.value, pw.value.length > 0, window.created]")
            assert filled == ["sam.rivera@example.com", True, 0]  # filled in, its button not pressed
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.log.count("pressed “Sign in” with your saved password") == 1  # not tried again
    assert "filled the Create Account form with your details and saved password" in r.log


async def ukg_did(page):
    """What the UKG-like Register page (site/ukg-register.html) has seen in this tab."""
    return await page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage).filter(([k]) => "
                               "k.startsWith('ukg.')).map(([k, v]) => [k.slice(4), JSON.parse(v)]))")


@pytest.mark.parametrize("query", ["?register", "?register&component"])
def test_with_manage_accounts_ukg_pros_second_account_step_is_finished(srv, monkeypatch, job_apply_home, query):
    """UKG Pro (Nikon Precision, live, Oct 2026, the test identity): after the sign-up's email,
    password and Continue, its Register page asks for a name and phone, a consent box ("By checking
    this box, I have read and agree to the Consent and Privacy Policy*", its words beside it, not in a
    label) and "Create account", greyed out until the box is ticked. The desk said the account was made
    as that page came up, filled it as an application, and stopped: it couldn't find the button. It
    finishes the account now (the consent ticked, never the text messages box), and says it's made
    only once the site goes on to the application. Live, "Create account" is a web component whose own
    "form" property isn't a form, and the click threw ("component")."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/signin-signup-link.html") + query, title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await ukg_did(r.page)
        finally:
            await applier.stop()

    r, did = run(go())
    assert did == {"presses": 1, "texts": False, "created": True}, (did, r.log)
    first = next(i for i, line in enumerate(r.log) if line.startswith("pressed “Continue” to create your account"))
    second = [i for i, line in enumerate(r.log) if line.startswith("pressed “Create account” to finish creating your "
                                                                    "account")]
    made = [i for i, line in enumerate(r.log) if line.startswith("created your account on")]
    assert len(second) == 1 and "after ticking “By checking this box, I have read and agree to the Consent and " \
        "Privacy Policy”" in r.log[second[0]], r.log
    assert len(made) == 1 and first < second[0] < made[0] and "agreed to its terms" in r.log[made[0]], r.log
    assert r.seen_form and r.need != "sign_in" and "couldn't find the button" not in r.reason, (r.reason, r.log)
    assert any("“My Information”" in line for line in r.log), r.log  # on to the application


@pytest.mark.parametrize("query", ["?register&security", "?register&captcha"])
def test_an_account_step_with_security_questions_or_a_picture_code_is_the_persons(srv, monkeypatch, job_apply_home,
                                                                                   query):
    """UKG Pro's Register page, asking security questions too (under its "Security questions" heading: their answers
    the person's, never made up) or a picture code to type (a CAPTCHA, the person's): the reviews of 0.3.154 and
    0.3.155. The desk ticked its terms and pressed "Create account" all the same. Its name and phone are filled; its
    security boxes, and a "City of birth" beside them (which would have taken the profile's city), are left empty;
    its consent isn't ticked nor its button pressed, and the card says what's the person's."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/signin-signup-link.html") + query, title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            values = await r.page.evaluate("() => Object.fromEntries(['fn', 'ph', 'sq', 'sa', 'cb', 'pc'].map((id) => "
                                           "[id, document.getElementById(id) ? document.getElementById(id).value : null]))")
            ticked = await r.page.evaluate("() => document.getElementById('consent').checked")
            return r, await ukg_did(r.page), values, ticked
        finally:
            await applier.stop()

    r, did, values, ticked = run(go())
    assert "presses" not in did and not ticked, (did, r.log)
    assert values["fn"] and values["ph"], values  # (the account's own details, from the profile)
    assert any(line.startswith("filled the rest of") for line in r.log), r.log
    assert r.need == "sign_in" and "“Create account”" in r.reason, (r.reason, r.log)
    if "security" in query:
        assert (values["sq"], values["sa"], values["cb"]) == ("", "", ""), values
        assert "; its security questions and their answers are yours to choose" in r.reason, r.reason
    else:
        assert values["pc"] == "" and "solve its CAPTCHA (that's yours)" in r.reason, (values, r.reason)


def test_without_manage_accounts_ukg_pros_second_account_step_is_the_persons(srv, monkeypatch):
    """The same Register page, with settings.manage_accounts off: the person pressed Continue on the
    sign-up the desk filled in, and the desk, carrying on, filled the next page as an application and
    stopped, saying it couldn't find the button. It fills in the name and phone and leaves the consent
    and "Create account" to the person, then carries on once they've pressed it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-signup-link.html") + "?register", title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you", about=state(r))
            assert r.need == "sign_in" and "Create Account form" in r.reason, (r.reason, r.log)
            await r.page.click("button:has-text('Continue')")  # the person creates the account
            await until(lambda: "few more details" in r.reason, about=state(r))
            assert r.need == "sign_in" and "“Create account”" in r.reason, (r.reason, r.log)
            page = r.page
            filled = await page.evaluate("() => [fn.value, ln.value, ph.value.length > 0, consent.checked, texts.checked]")
            assert filled == ["Sam", "Rivera", True, False, False], filled  # the boxes, never the consent
            assert (await ukg_did(page)).get("presses") is None  # nor its button
            await page.check("#consent")  # the person agrees and creates the account
            await page.click("button:has-text('Create account')")
            await until(lambda: r.need == "questions" or r.status == "ready", about=state(r))  # carried on by itself
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert not any("created your account" in line or "pressed “Create account”" in line for line in r.log), r.log
    assert r.seen_form, (r.reason, r.log)


@pytest.mark.parametrize("mode", ["", "?slow=3500"])
def test_a_question_the_site_answers_itself_is_not_asked(srv, monkeypatch, mode):
    """Oracle fills County from the ZIP picked. The profile has no county, so County was among
    the questions the page left open before filling; once the site has filled it, it isn't
    put to the person.

    ?slow: onsemi, live, drew City, State and County afresh a while after the ZIP was picked.
    A box that wasn't back in time to be filled, but was by the time the page was looked at
    again, filled by the site, isn't asked about either."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/oracle-address.html") + mode, title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status in ("needs_you", "ready"))
            return r, await r.page.evaluate("() => window.picked")
        finally:
            await applier.stop()

    r, picked = run(go())
    assert not any("County" in (q.get("label") or "") for q in r.questions), (r.reason, r.questions)
    assert "clicked “Next”" in r.log, r.log
    assert picked["region1"] == "Maricopa" and picked["city"] == "Chandler, Maricopa, AZ"


def test_a_site_that_says_try_again_later_is_left_until_the_person_resumes(srv, monkeypatch):
    """Texas Instruments' and onsemi's Oracle sites, after many sign-up emails in a day: NEXT
    answers "Too Many Attempts. Try Again Later.", whose CONTINUE goes back to the posting.
    Live, the desk pressed on round that circle until it noticed. It stops on that page now
    and says to leave the job a while; when the person presses Resume, it carries on from
    there."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/try-later.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "stuck" and not r.blocking, (r.reason, r.log)
            assert "says “Too Many Attempts. Try Again Later.” Leave this job for a while" in r.reason, r.reason
            assert r.log.count("clicked “Apply Now”") == 1  # not round again
            await r.page.evaluate("() => sessionStorage.setItem('waited', 'yes')")
            applier.enqueue(job["id"], front=True)  # the person presses Resume
            await until(lambda: r.need == "questions" or r.status == "ready")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "clicked “CONTINUE”" in r.log and r.log.count("clicked “Apply Now”") == 2, r.log


def test_a_try_again_later_page_reached_while_the_person_has_the_tab_still_stops(srv, monkeypatch):
    """The desk waits for an emailed code; the person's tries there end on "Too Many Attempts.
    Try Again Later." The queue carries on by itself once the page has moved on, but nobody
    pressed Resume on that page, so the desk stops there instead of pressing on round it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "email_code" and r.blocking, (r.reason, r.log)
            await r.page.goto(fixture_url("site/try-later.html") + "?step=wait")
            await until(lambda: r.status == "needs_you" and r.need != "email_code")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "stuck" and "Try Again Later" in r.reason, (r.reason, r.log)
    assert "clicked “CONTINUE”" not in r.log, r.log


@pytest.mark.parametrize("query", ["", "?code"])
def test_an_emailed_link_or_code_is_waited_for(srv, monkeypatch, query):
    """After Create Account, a site emails a link to confirm the address, or a code. The link
    opens in the person's own browser, which leaves the desk's tab where it was: the desk says
    to reload that tab, and carries on once it has been. A code is typed into the tab."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/verify-email.html") + query, title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "email_code", (r.reason, r.log)
            if query:
                assert "emailed you a code" in r.reason and "reload" not in r.reason, r.reason
                await r.page.fill("#code", "123456")
                await r.page.click("button:has-text('Verify')")
            else:
                assert "emailed you a link" in r.reason and "reload this job's tab" in r.reason, r.reason
                await asyncio.sleep(1.5)
                assert r.need == "email_code"  # the tab doesn't change by itself
                await r.page.evaluate("() => sessionStorage.setItem('verified', 'yes')")  # the link, opened elsewhere
                await r.page.reload()
            await until(lambda: r.need == "questions" or r.status == "ready", about=state(r))  # carried on by itself
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert any(line.startswith("filled") for line in r.log), r.log


@pytest.mark.parametrize("query", ["?code", "?code&split", ""])
def test_an_emailed_code_or_link_is_read_from_the_inbox(srv, monkeypatch, query):
    """With an email app password saved, a job waiting on an emailed code gets it from the
    inbox, typed in and Verify pressed; one waiting on a link has it opened in this browser
    and its tab reloaded. Only mail from that job's site, since the wait began, is asked for.
    &split: Oracle's "Confirm Your Identity" (onsemi, TI, live) has a box per digit."""
    import functools
    import http.server
    import threading

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    # served over http, so the link opened in another tab confirms the address for this one
    # (a file:// page has no storage shared between tabs)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(Path(__file__).parent / "fixtures"))
    site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    page = f"http://127.0.0.1:{site.server_address[1]}/site/verify-email.html"
    job = srv.add_job(url=page + query, title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    asked = []

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        asked.append((address, password, want))
        assert since > time.time() - 120  # since the wait began
        if want == "code":
            return pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time())
        assert allowed_link(page + "?confirm") and not allowed_link("https://elsewhere.example.net/verify?t=1")
        return pipeline.mailbox.Found("link", page + "?confirm", "careers.example.com", time.time())

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "questions" or r.status == "ready", about=state(r))  # nobody touched it
            return r
        finally:
            await applier.stop()

    try:
        r = run(go())
    finally:
        site.shutdown()
    assert asked and asked[0] == ("sam.rivera@example.com", "an-app-password", "code" if query else "link")
    if query:
        assert "entered the code from your email (sent from careers.example.com)" in r.log, r.log
        assert "pressed “Verify”" in r.log, r.log
    else:
        assert "opened the confirmation link from your email (sent from careers.example.com)" in r.log, r.log
    assert any("watching your inbox" in line for line in r.log), r.log


def test_a_job_past_its_emailed_code_carries_on_with_the_queue_paused(srv, monkeypatch):
    """Issue #112, note 43 (Oracle, 0.3.194): the desk entered the emailed code and pressed Verify, and the job dropped
    back to "queued" for good, its tab left half-filled on the application's first section, no card for it. The job
    held the queue for its code; put back in it once past the code, it waited as any new start does (Pause queue on).
    The job under way carries on after its code, Pause queue or not: only new jobs wait on Resume queue."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    job = srv.add_job(url=fixture_url("site/verify-email.html") + "?code&split", title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("site/late-form.html"), title="ET", company="Example Semi")["job"]
    applier = Applier(srv)
    arrived = []  # the code's in the inbox once the queue is paused

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        return pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time()) if arrived else None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            applier.enqueue(other["id"])
            await until(lambda: r.need == "email_code", about=state(r))
            applier.pause_queue()
            arrived.append(True)
            await until(lambda: r.need == "questions" or r.status == "ready", about=state(r))
            return r, applier.runs[other["id"]]
        finally:
            await applier.stop()

    r, waiting = run(go())
    assert "pressed “Verify”" in r.log, r.log
    assert any(line.startswith("filled") for line in r.log[r.log.index("pressed “Verify”"):]), r.log  # (the page after)
    assert waiting.status == "queued"  # (a new job still waits for Resume queue)


def test_a_code_that_comes_after_the_queue_went_on_is_still_used(srv, monkeypatch):
    """No one at the browser: after a while the other jobs go ahead without the one waiting on
    its emailed code. A code that arrives after that is still read from the inbox, between
    jobs, and the job carries on by itself."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 1)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    waiting = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)
    sent = {"yet": False}

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        return pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time()) if sent["yet"] else None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)

    async def go():
        applier.start()
        try:
            first = applier.enqueue(waiting["id"])
            second = applier.enqueue(other["id"])
            await until(lambda: first.left, about=state(first))  # the queue went on without it
            await until(lambda: second.status not in ("queued", "running"), about=state(second))
            sent["yet"] = True  # the email arrives now
            await until(lambda: first.need == "questions" or first.status == "ready", about=state(first))
            return first
        finally:
            await applier.stop()

    r = run(go())
    assert "entered the code from your email (sent from careers.example.com)" in r.log, r.log


def test_a_job_the_queue_went_on_without_carries_on_past_its_code_with_the_queue_paused(srv, monkeypatch):
    """The re-check of 0.3.196 (issue #112, note 43's other way in): a job the queue went on without, its code put in
    between jobs while Pause queue is on, waited as "queued" with no card. It was under way: it carries on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 1)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    waiting = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)
    sent = {"yet": False}

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        return pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time()) if sent["yet"] else None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)

    async def go():
        applier.start()
        try:
            first = applier.enqueue(waiting["id"])
            second = applier.enqueue(other["id"])
            await until(lambda: first.left, about=state(first))  # the queue went on without it
            await until(lambda: second.status not in ("queued", "running"), about=state(second))
            applier.pause_queue()
            sent["yet"] = True  # the email arrives now
            await until(lambda: first.need == "questions" or first.status == "ready", about=state(first))
            return first
        finally:
            await applier.stop()

    r = run(go())
    assert "entered the code from your email (sent from careers.example.com)" in r.log, r.log


def test_a_refused_email_app_password_is_said_once_and_not_tried_again(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "a-wrong-one")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    job = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    tries = []

    def refused(*args, **kw):
        tries.append(args)
        raise pipeline.mailbox.MailboxError("imap.gmail.com didn't accept the email app password")

    monkeypatch.setattr(pipeline.mailbox, "search", refused)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "email_code")
            await asyncio.sleep(2)  # several polls
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert len(tries) == 1
    assert applier.mail_problem and "didn't accept the email app password" in applier.mail_problem
    assert applier.mail_login() is None  # until a new one is saved
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "a-new-one")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    assert applier.mail_login() == ("sam.rivera@example.com", "a-new-one")
    assert r.need == "email_code"  # still waiting for the person


def test_a_certificate_that_doesnt_check_out_isnt_a_refused_app_password(srv, monkeypatch):
    """The checked connection's own error (0.3.174: something answering for the mail service, or a computer with
    no root certificates) is said and tried again shortly, never read as a refused app password, which would retire
    the saved one until a new one is saved (the review of 0.3.174)."""
    import ssl

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    made = []

    class Unchecked:
        def __init__(self, host, timeout=None, ssl_context=None):
            made.append(host)
            raise ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                                                  "self-signed certificate in certificate chain (_ssl.c:1000)")

    monkeypatch.setattr(pipeline.mailbox.imaplib, "IMAP4_SSL", Unchecked)
    job = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "email_code" and len(made) >= 2)  # looked again: nothing retired
            return r
        finally:
            await applier.stop()

    run(go())
    assert "imap.example.com's certificate didn't check out" in applier.mail_problem, applier.mail_problem
    assert "tries again shortly" in applier.mail_problem and "app password" not in applier.mail_problem
    assert applier.mail_login() == ("sam.rivera@example.com", "an-app-password")  # the saved one still stands


def test_an_inbox_the_desk_cant_read_is_said_and_not_watched(srv, monkeypatch):
    """An address whose mail service takes no app passwords (a work domain, Outlook.com): the
    pause doesn't claim the inbox is watched, and the page says why."""
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    applier = Applier(srv)
    assert applier.mail_login() is None  # the profile's sam.rivera@example.com
    assert "can't read mail for example.com addresses" in applier.mail_problem


def test_a_code_that_didnt_go_in_is_tried_again(srv, monkeypatch):
    """The code box was being drawn again when the code came: it's looked for again on the next
    check, not given up on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    job = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    monkeypatch.setattr(pipeline.mailbox, "search",
                        lambda *a, **k: pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time()))
    real, tries = applier._enter_code, []

    async def enter(run, found):
        tries.append(1)
        return False if len(tries) == 1 else await real(run, found)

    applier._enter_code = enter

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "questions" or r.status == "ready", about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert len(tries) >= 2 and "entered the code from your email (sent from careers.example.com)" in r.log


def test_an_odd_answer_from_the_mail_service_doesnt_stop_the_queue(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    job = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    def odd(*a, **k):
        raise IndexError("an unexpected answer")

    monkeypatch.setattr(pipeline.mailbox, "search", odd)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "email_code")
            await until(lambda: applier.mail_problem is not None)
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "couldn't read your email (IndexError)" in applier.mail_problem and r.need == "email_code"


def test_without_an_email_app_password_the_inbox_is_never_read(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    job = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    monkeypatch.setattr(pipeline.mailbox, "search", lambda *a, **k: pytest.fail("the inbox was read"))

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "email_code")
            await asyncio.sleep(1)
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "watching your inbox" not in r.reason


def test_the_sign_in_forms_own_button_is_pressed_not_the_headers(srv, monkeypatch):
    """KLA's, NXP's, ASML's and Hitachi's Workday: the page's header has a "Sign In" of its own,
    ahead of the sign-in form, which opens a sign-in pop-up and sends nothing. The form's own
    button is pressed; when the saved password doesn't sign in, Create Account is opened and
    filled in, and its button (a div on Workday) is left to the person."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-header-popup.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "sign_in" and "Create Account form" in r.reason, (r.reason, r.log)
            assert "first application" in r.reason
            state = await r.page.evaluate("""() => [window.popupOpened, window.signInTries, window.created,
                em.value, pw.value === pw2.value && pw.value.length > 0, terms.checked]""")
            assert state == [False, 1, False, "sam.rivera@example.com", True, False]
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.log.count("pressed “Sign In” with your saved password") == 1
    assert "your saved password didn't sign in, so I opened Create Account" in r.log


def test_a_headers_sign_in_isnt_pressed_for_a_form_whose_button_reads_otherwise(srv, monkeypatch):
    """Nothing after the password box reads "Sign In" (the form's button says Continue): the
    header's "Sign In" isn't pressed in its place. The form holds only the sign-in, so its own
    Continue is pressed; that doesn't get in (no account there), so Create Account is opened."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-header-popup.html") + "?button=Continue", title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "sign_in" and "Create Account form" in r.reason, (r.reason, r.log)
            state = await r.page.evaluate("() => [window.popupOpened, window.signInTries, window.created]")
            assert state == [False, 1, False]
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.log.count("pressed \u201cContinue\u201d with your saved password") == 1, r.log


def test_a_create_account_forms_own_button_is_never_the_way_to_it(srv, monkeypatch):
    """Workday's sign-in pop-up, open over its Create Account form (which pressing the header's
    "Sign In" used to leave): the form behind it isn't read, but its "Create Account" button (a
    div, not a form's submit) is on the page, ahead of the pop-up's own "Create Account". That
    button creates the account; the pop-up's link is the way to the form."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-header-popup.html") + "?start=popup", title="FSE",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            state = await r.page.evaluate("""() => [window.created, window.signInTries,
                em.value, pw.value === pw2.value && pw.value.length > 0]""")
            assert state == [False, 1, "sam.rivera@example.com", True], (state, r.reason, r.log)
            assert r.need == "sign_in" and "Create Account form" in r.reason, (r.reason, r.log)
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "your saved password didn't sign in, so I opened Create Account" in r.log


@pytest.mark.parametrize("page, expected", [
    # Amkor's SuccessFactors: the email twice, names and country; the newsletter box is left alone
    ("create-account-details.html", {"email": "sam.rivera@example.com", "email2": "sam.rivera@example.com", "same": True,
                                     "first": "Sam", "last": "Rivera", "country": "United States", "news": False}),
    # SCREEN's ApplicantStack: the user name is the email, then the name and the email again (its box
    # for robots, hidden, stays empty)
    ("create-account-username.html", {"user": "sam.rivera@example.com", "same": True, "name": "Sam Rivera",
                                      "email": "sam.rivera@example.com", "robots": ""}),
    # Benchmark's Infor: the picture code, the resume upload and the "no resume" box are the person's
    ("register-picture-code.html", {"first": "Sam", "last": "Rivera", "email": "sam.rivera@example.com", "same": True,
                                    "code": "", "upload": "", "files": 0, "nores": False}),
])
def test_a_create_account_form_is_filled_from_the_profile(srv, monkeypatch, page, expected):
    """A new account's form asks for more than the email and password. The rest comes from
    the profile; the form isn't sent, and the job isn't taken for a filled-in application."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url(f"site/{page}"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "sign_in" and "Create Account form" in r.reason, (r.reason, r.log)
            assert await r.page.evaluate("() => window.result()") == expected
            assert await r.page.evaluate("() => window.created") == 0
            # the desk's own record of the page shows it filled in (whether, never what)
            assert {f["kind"]: f["empty"] for f in r.page_info["fields"] if f["kind"] == "password"} == {"password": False}
            return r
        finally:
            await applier.stop()

    r = run(go())
    # (SCREEN's asks for a user name too, and is told it's the email)
    user = " (your email as its user name)" if "username" in page else ""
    assert "filled the Create Account form with your details and saved password" + user in r.log, r.log
    assert srv.tracker().get(job["id"])["status"] != "ready"


def test_with_manage_accounts_a_create_account_with_a_picture_code_is_left_to_the_person(srv, monkeypatch,
                                                                                          job_apply_home):
    """Benchmark's Infor registration asks for a picture code ("Enter the text in image above"), a
    CAPTCHA the site draws itself. The desk took only a check's own frame for a CAPTCHA, so with
    manage_accounts it pressed the form's Submit with the code empty. A CAPTCHA is the person's: the
    form is filled in and left to them, and the pause says the CAPTCHA is theirs."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/register-picture-code.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => [window.created, window.result().code]")
        finally:
            await applier.stop()

    r, (created, code) = run(go())
    assert (created, code) == (0, ""), (created, code, r.log)
    assert r.need == "sign_in" and "Its CAPTCHA is yours to solve" in r.reason, (r.reason, r.log)
    assert not any(line.startswith("pressed") for line in r.log), r.log


def test_follows_an_application_that_opens_in_a_new_tab_late(srv, monkeypatch):
    """asml.com: Apply Now opens the application system in a new tab a few seconds after the click."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/popup-posting.html"), title="Sr. Field Application Engineering",
                      company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=90)
            assert r.need == "sign_in", (r.status, r.reason, r.log)
            assert "followed the application into a new tab" in r.log
            assert r.page.url.endswith("signin.html")
        finally:
            await applier.stop()

    run(go())


def test_a_missing_file_brings_the_questions_along(srv, job_apply_home, monkeypatch):
    """ASM: the form wants a resume the profile doesn't have and asks questions it can't
    answer; the questions come with the pause, so they can be answered meanwhile."""
    import yaml

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    prof = yaml.safe_load((job_apply_home / "profile.yaml").read_text(encoding="utf-8"))
    prof["documents"] = {"resume": None}
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(prof), encoding="utf-8")
    job = srv.add_job(url=fixture_url("site/upload-form.html"), title="Engineer", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"))
            assert r.need == "stuck" and "Resume/CV" in r.reason and "1 question your profile" in r.reason, r.reason
            assert len(r.questions) == 1 and "non-compete" in r.questions[0]["label"]
        finally:
            await applier.stop()

    run(go())


def _job_b_waiting_on_review(srv, applier):
    """Job B: filled earlier, waiting on its review page for the person's own Submit."""
    async def setup():
        b_job = srv.add_job(url=fixture_url("site/review.html"), title="Job B", company="B Co")["job"]
        tab = await srv.browser.new_tab()
        await tab.goto(fixture_url("site/review.html"))
        applier.runs[b_job["id"]] = pipeline.Run(b_job["id"], "Job B", "B Co", status="ready", seen_form=True, page=tab)
        return b_job, tab
    return setup()


@pytest.mark.parametrize("how", ["skip", "close"])
def test_a_job_whose_tab_goes_never_carries_on_in_another_tab(srv, monkeypatch, how):
    """Skip pressed, or the tab closed, while a job runs: it stops there. Moving on to the
    last open tab instead (job B's, on its review page) could press B's Submit."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    a_job = srv.add_job(url=fixture_url("site/step1.html"), title="Job A", company="A Co")["job"]
    applier = Applier(srv)
    applier.auto_submit = True
    real_click = srv.click

    async def go():
        b_job, b_tab = await _job_b_waiting_on_review(srv, applier)

        async def click_then_lose_the_tab(target):
            out = await real_click(target)
            a = applier.runs[a_job["id"]]
            if a.status == "running" and a.page is not None and not a.page.is_closed():
                if how == "skip":
                    await applier.skip(a_job["id"])
                else:
                    await a.page.close()
            return out

        monkeypatch.setattr(srv, "click", click_then_lose_the_tab)
        applier.start()
        try:
            a = applier.enqueue(a_job["id"], submit=True)
            # until the worker is done with A (the old code carried on into B's tab after a skip)
            await until(lambda: applier.current is None and a.status not in ("queued", "running"))
            await asyncio.sleep(0.5)
            return a, b_job, await b_tab.inner_text("body")
        finally:
            await applier.stop()

    a, b_job, b_text = run(go())
    if how == "skip":
        assert a.status == "skipped", (a.status, a.reason, a.log)
    else:
        assert a.status == "failed" and "tab was closed" in a.reason, (a.status, a.reason, a.log)
    assert "Thank you" not in b_text and "Check your application" in b_text  # B untouched
    assert srv.tracker().get(b_job["id"])["status"] == "saved"
    assert srv.tracker().get(a_job["id"])["status"] != "applied"


def test_a_tab_the_person_opens_is_not_taken_over(srv, monkeypatch):
    """Only tabs the application opens are followed. One the person opens (to check their
    email, say) while a job runs is theirs: nothing is typed or clicked there."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/step1.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    real_click = srv.click
    theirs = []

    async def click_then_open_a_tab(target):
        out = await real_click(target)
        if not theirs:
            tab = await srv.browser._ctx.new_page()  # like pressing Ctrl+T
            await tab.goto(fixture_url("site/step1.html"))
            theirs.append(tab)
        return out

    monkeypatch.setattr(srv, "click", click_then_open_a_tab)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"))
            values = await theirs[0].evaluate("() => [fn.value, ln.value, em.value]")
            return r, values, theirs[0].url
        finally:
            await applier.stop()

    r, values, their_url = run(go())
    assert r.need == "questions" and r.page.url.endswith("step2.html"), (r.status, r.reason, r.log)
    assert values == ["", "", ""] and their_url.endswith("step1.html")  # left alone


def test_a_sign_in_hold_waits_quietly_while_other_jobs_queue(srv, monkeypatch):
    """The queue holds for a person's sign-in, with another job waiting: the paused tab is
    looked at once a poll, not in a tight loop over the page they're typing in."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    first = srv.add_job(url=fixture_url("site/signin.html"), title="FSE", company="Example Fab")["job"]
    second = srv.add_job(url=fixture_url("site/step1.html"), title="FSE 2", company="Example Fab")["job"]
    applier = Applier(srv)
    looks = []
    real_moved_on = applier._moved_on

    async def counted(run_):
        looks.append(time.monotonic())
        return await real_moved_on(run_)

    monkeypatch.setattr(applier, "_moved_on", counted)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(first["id"])
            await until(lambda: r.status == "needs_you")
            applier.enqueue(second["id"])
            looks.clear()
            await asyncio.sleep(2)
            return r, len(looks)
        finally:
            await applier.stop()

    r, count = run(go())
    assert r.need == "sign_in" and r.blocking
    assert count <= 2 / 0.3 + 2, count  # one look a poll (hundreds before)


def test_a_paused_tab_taken_to_webmail_is_not_the_application_moving_on(srv, monkeypatch):
    """Paused at a sign-in, the person uses that tab to read their email: nothing is filled
    or clicked there. Back on the application's site, the desk carries on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/signin.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            await r.page.route("https://mail.example.com/**", lambda route: route.fulfill(
                content_type="text/html", body="<h1>Inbox</h1><form><label for=q>Search mail</label><input id=q>"
                "<label for=to>To</label><input id=to><button>Next</button></form>"))
            await r.page.goto("https://mail.example.com/inbox")
            await asyncio.sleep(1.5)
            stayed = (r.status, r.blocking, await r.page.evaluate("() => to.value"))
            await r.page.goto(fixture_url("site/step1.html"))  # signed in elsewhere, back on the application
            await until(lambda: r.status == "needs_you" and r.need == "questions")
            return stayed
        finally:
            await applier.stop()

    status, blocking, typed = run(go())
    assert (status, blocking, typed) == ("needs_you", True, "")


def test_submit_for_me_leaves_an_application_with_an_empty_required_field(srv, monkeypatch):
    job = srv.add_job(url=fixture_url("site/submit-empty.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        tab = await srv.browser.new_tab()
        await tab.goto(fixture_url("site/submit-empty.html"))
        r = pipeline.Run(job["id"], "FSE", "Example Fab", status="running", seen_form=True, page=tab)
        applier.runs[job["id"]] = r
        await applier._strict(applier._submit(r, by_person=False))
        return r, await tab.evaluate("() => window.sent")

    r, sent = run(go())
    assert r.need == "stuck" and "Why do you want to work here?" in r.reason, (r.status, r.reason)
    assert sent == 0


def test_a_submit_the_site_turns_down_is_not_called_submitted(srv, monkeypatch):
    """The person pressed Submit; the site showed an error and kept the form. That's not
    "Submitted": the job stays open, with Submit and "I submitted it" to hand."""
    job = srv.add_job(url=fixture_url("site/submit-empty.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        tab = await srv.browser.new_tab()
        await tab.goto(fixture_url("site/submit-empty.html"))
        r = pipeline.Run(job["id"], "FSE", "Example Fab", status="queued", seen_form=True, page=tab)
        applier.runs[job["id"]] = r
        await applier._strict(applier._submit(r))
        return r

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "submit_failed"), (r.status, r.reason)
    assert "please answer every required question" in r.reason
    assert srv.tracker().get(job["id"])["status"] != "applied"


@pytest.mark.parametrize("page,sent", [("workday-candidate-home.html", True), ("applied-before-posting.html", True),
                                       ("workday-questions-thank-you.html", False), ("note-posting.html", False)])
def test_a_submit_on_a_page_already_past_its_submit_takes_the_sites_word(srv, monkeypatch, page, sent):
    """Issue #112, note 31: the person pressed Submit on the site, then on the desk, which found no Submit button
    ("No submit button on this page. Continue through the steps first."); the job ended up skipped and the
    application never counted. A page that says it went (Workday's "Application Submitted", a job page's "You
    applied for this job on") marks it applied; any other is the person's to say, with "I submitted it myself"
    on its card. Nothing is pressed either way."""
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    srv.tracker().update(job["id"], status="ready_to_submit")
    applier = Applier(srv)

    async def go():
        tab = await srv.browser.new_tab()
        await tab.goto(fixture_url(f"site/{page}"))
        r = pipeline.Run(job["id"], "FSE", "Example Fab", status="queued", seen_form=True, page=tab,
                         url=fixture_url("site/review.html"))  # (the review page it waited on)
        applier.runs[job["id"]] = r
        await applier._strict(applier._submit(r))
        return r

    r = run(go())
    status = srv.tracker().get(job["id"])["status"]
    if sent:
        assert r.status == "submitted" and r.reason.startswith("Submitted:"), (r.status, r.reason)
        assert status == "applied"
    else:
        assert (r.status, r.need) == ("needs_you", "stuck") and "I submitted it myself" in r.reason, (r.status, r.reason)
        assert status != "applied"


def test_a_confirmation_is_taken_only_on_the_persons_submit_and_a_page_the_tab_went_on_to(srv):
    """The review of note 31: a confirmation's words on the review page itself ("this application submitted by me is
    true"), or on the posting a session went back to ("Thank you for applying"), are no word that it went; nor
    is any page for Submit for me, whose second look for a Submit button may just have missed it."""
    applier = Applier(srv)

    async def go(page, by_person, url):
        job = srv.add_job(url=fixture_url("site/workday-candidate-home.html") + f"?{page}", title="FSE",
                          company="Example Fab")["job"]
        tab = await srv.browser.new_tab()
        await tab.goto(fixture_url(f"site/{page}"))
        r = pipeline.Run(job["id"], "FSE", "Example Fab", status="queued", seen_form=True, page=tab, url=url)
        applier.runs[job["id"]] = r
        await applier._strict(applier._submit(r, by_person=by_person))
        return r, srv.tracker().get(job["id"])["status"]

    home = fixture_url("site/workday-candidate-home.html")
    for by_person, url in ((False, fixture_url("site/review.html")), (True, home)):
        r, status = run(go("workday-candidate-home.html", by_person, url))
        assert r.status != "submitted" and status != "applied", (by_person, r.status, r.reason)


def test_a_submit_with_no_confirmation_asks_the_person_to_check(srv, monkeypatch):
    job = srv.add_job(url=fixture_url("site/submit-quiet.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        tab = await srv.browser.new_tab()
        await tab.goto(fixture_url("site/submit-quiet.html"))
        r = pipeline.Run(job["id"], "FSE", "Example Fab", status="queued", seen_form=True, page=tab)
        applier.runs[job["id"]] = r
        await applier._strict(applier._submit(r))
        return r

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "check_submit"), (r.status, r.reason)


def test_questions_that_appear_after_filling_are_filled_too(srv, monkeypatch):
    """State shows only once Country is picked: it's filled in the same pass, not left empty
    on a page that then counts as ready."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/reveal.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"))
            return r, await r.page.evaluate("() => [country.value, st.value]")
        finally:
            await applier.stop()

    r, values = run(go())
    assert r.status == "ready", (r.status, r.reason, r.log)
    assert values == ["United States", "Arizona"]


def test_the_saved_password_only_goes_to_its_own_system():
    from job_apply.pipeline import password_for
    assert password_for("https://asml.wd3.myworkdayjobs.com/en-US/ASMLExternal/login") == "workday_password"
    assert password_for("https://wd5.myworkday.com/acme/login.htmld") == "workday_password"
    assert password_for("https://career4.successfactors.com/career?company=acme") == "successfactors_password"
    # SCREEN's ApplicantStack, UKG Pro's sign-in (Nikon Precision), Benchmark's Infor board
    assert password_for("https://seus.applicantstack.com/x/login") == "applicantstack_password"
    assert password_for("https://signin-us.ultipro.com/u/login?state=abc") == "ukg_password"
    assert password_for("https://css-benchmark-prd.inforcloudsuite.com/sso/SSOServlet") == "infor_password"
    # lookalikes that only mention Workday in their address
    assert password_for("https://evil.example/myworkdayjobs.com/login") is None
    assert password_for("https://acme.myworkdayjobs.com.evil.example/login") is None
    assert password_for("https://notmyworkdayjobs.com/login") is None
    assert password_for("http://acme.wd1.myworkdayjobs.com/login") is None  # not over https
    assert password_for("https://careers.example.com/signin") is None


def test_a_password_box_inside_an_embedded_frame_is_left_alone(srv, monkeypatch):
    """A frame can come from anywhere; the saved password only goes into the page's own form."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    job = srv.add_job(url=fixture_url("site/framed-signin.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            frame = next(f for f in r.page.frames if f.url.endswith("/signin.html"))
            return r, await frame.evaluate("() => [em.value, pw.value]")
        finally:
            await applier.stop()

    r, values = run(go())
    assert r.need == "sign_in", (r.status, r.reason, r.log)
    assert values == ["", ""]
    assert not any("saved password" in line for line in r.log)


def test_skip_closes_the_jobs_tabs_both_the_posting_and_the_application(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/popup-posting.html"), title="Sr. Field Application Engineering",
                      company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you", timeout=90)
            tabs = srv.browser.lineage(r.page)
            await applier.skip(job["id"])
            return r, tabs
        finally:
            await applier.stop()

    r, tabs = run(go())
    assert len(tabs) == 2 and tabs[0].url.endswith("signin.html") and tabs[1].url.endswith("popup-posting.html")
    assert all(t.is_closed() for t in tabs) and r.status == "skipped"


def test_a_job_skipped_while_its_paused_tab_is_looked_at_isnt_started_again(srv, monkeypatch):
    """Skip pressed while the desk was checking a job paused for a sign-in: the tab Skip closed
    read as "they got past it", and the skipped job was queued again. The test above failed
    now and then on this."""
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    paused = Run(job["id"], "FSE", "Example Fab", status="needs_you", need="sign_in", blocking=True)
    paused.paused_at = time.time()
    applier.runs[job["id"]] = paused

    async def moved_on(r):
        await applier.skip(job["id"])  # pressed meanwhile; its tab is gone, which reads as moved on
        return True

    monkeypatch.setattr(applier, "_moved_on", moved_on)
    run(applier._tick())
    assert paused.status == "skipped" and not applier.tasks


def test_undo_puts_a_skipped_job_back_in_its_place_and_until_then_it_isnt_applied_to(srv, monkeypatch):
    """Skip had no undo. Undo now gives the tracker back the job's status from before, and puts the
    job back in the queue where it was, with its log; until then neither Apply, Resume nor the
    queue going on starts it. It goes back with Submit for me off for it: Undo sends nothing."""
    a, b, c = (srv.add_job(url=f"https://example.com/jobs/{n}", title=f"Job {n}", company="Example Co")["job"]["id"]
               for n in "abc")
    srv.update_job(b, status="in_progress", notes="asked about the night shift")
    applier = Applier(srv)
    driven = []

    async def drive(r):  # (no browser: what the queue starts is what matters here)
        driven.append(r.job_id)
        r.status, r.reason = "ready", "Filled and waiting on the review page."

    monkeypatch.setattr(applier, "_drive", drive)
    for job_id in (a, b, c):
        applier.enqueue(job_id, submit=True)
    applier.runs[b].log.append("an earlier try")

    run(applier.skip(b))
    assert [j for _, j in applier.tasks] == [a, c] and srv.get_job(b)["job"]["status"] == "skipped"
    for start in (lambda: applier.enqueue(b), lambda: applier.resume(b)):
        with pytest.raises(ValueError, match="skipped"):
            start()
    assert [j for _, j in applier.tasks] == [a, c]

    events = len(srv.get_job(a)["history"])
    assert run(applier.unskip(a)) is applier.runs[a]  # not skipped: left as it is
    assert applier.runs[a].status == "queued" and len(srv.get_job(a)["history"]) == events
    with pytest.raises(KeyError):
        run(applier.unskip(777))

    r = run(applier.unskip(b))
    assert [j for _, j in applier.tasks] == [a, b, c]  # its place
    assert r.status == "queued" and not r.submit
    assert r.log[0] == "an earlier try" and r.log[-1] == "Skip undone: back in the queue"
    job = srv.get_job(b)
    assert job["job"]["status"] == "in_progress" and job["job"]["notes"] == "asked about the night shift"
    assert [e["status"] for e in job["history"]][-2:] == ["skipped", "in_progress"]

    # skipped again: the queue goes on without it, and Undo puts it back to be applied to
    run(applier.skip(b))
    while applier.tasks:
        run(applier._tick())
    assert driven == [a, c] and applier.runs[b].status == "skipped"
    run(applier.unskip(b))
    run(applier._tick())
    assert driven == [a, c, b] and applier.runs[b].status == "ready"


def test_undo_waits_for_the_step_the_job_was_skipped_in(srv, monkeypatch):
    """Skip pressed while the worker was still on the job (saving the page it paused on, say): an
    Undo straight after it put the job back while that step still ran, which could carry on in the
    tab Skip closed. Undo waits for the step to end, and says to press it again if it doesn't."""
    job = srv.add_job(url="https://example.com/jobs/1", title="Job", company="Example Co")["job"]["id"]
    applier = Applier(srv)
    applier.runs[job] = Run(job, "Job", "Example Co", status="needs_you", need="sign_in")
    applier.current = job  # the worker, still on it

    async def go():
        await applier.skip(job)
        undo = asyncio.ensure_future(applier.unskip(job))
        await asyncio.sleep(0.3)
        assert not undo.done() and not applier.tasks and srv.get_job(job)["job"]["status"] == "skipped"
        applier.current = None  # its step ends
        return await undo

    assert run(go()).status == "queued" and [j for _, j in applier.tasks] == [job]
    monkeypatch.setattr(pipeline, "UNSKIP_WAIT", 0.2)
    run(applier.skip(job))
    applier.current = job
    with pytest.raises(ValueError, match="still stopping"):
        run(applier.unskip(job))
    assert applier.runs[job].status == "skipped" and srv.get_job(job)["job"]["status"] == "skipped"


def test_undo_of_a_skip_never_sends_or_queues_a_job_that_went_in(srv):
    """Skipped after it went in (marked applied): Undo gives the tracker back "applied", and the job
    is never queued again (with Submit for me on, that would apply twice). One skipped before the desk
    began it goes back to the list."""
    sent = srv.add_job(url="https://example.com/jobs/sent", title="Sent", company="Example Co")["job"]["id"]
    fresh = srv.add_job(url="https://example.com/jobs/fresh", title="Fresh", company="Example Co")["job"]["id"]
    applier = Applier(srv)
    applier.auto_submit = True
    applier.enqueue(sent, submit=True)
    srv.update_job(sent, status="applied")
    applier.mark_applied(sent)
    run(applier.skip(sent))
    run(applier.skip(fresh))
    r = run(applier.unskip(sent))
    assert r.status == "submitted" and not applier.tasks and srv.get_job(sent)["job"]["status"] == "applied"
    assert run(applier.unskip(fresh)) is None and fresh not in applier.runs and not applier.tasks
    assert srv.get_job(fresh)["job"]["status"] == "saved"


def test_a_job_put_back_by_undo_opens_again_and_says_why_it_starts_over(srv, monkeypatch):
    """Skip closed the job's tab: put back by Undo, it opens again in a new tab, its log saying why
    its application starts over, and stops where it stopped before."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "sign_in", about=state(r))
            first = r.page
            await applier.skip(job["id"])
            assert first.is_closed() and srv.get_job(job["id"])["job"]["status"] == "skipped"
            await applier.unskip(job["id"])  # (the worker may still be saving the paused page)
            await until(lambda: r.need == "sign_in" and r.page is not first, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "needs_you" and not r.page.is_closed()
    assert any(line.startswith("its tab was closed, so I opened the job again in a new tab") for line in r.log), r.log
    assert srv.get_job(job["id"])["job"]["status"] == "in_progress"


@pytest.mark.parametrize("query", ["", "?tokens"], ids=["plain", "fresh-tokens"])
def test_a_flow_that_goes_round_in_a_circle_stops_after_one_lap(srv, monkeypatch, query):
    """Oracle's sites, refusing a code for an address: Next leads to a Continue that goes back
    to the posting. Going round again would only repeat it (and might email another code). A site whose addresses
    carry a fresh token on each visit (?tokens: "&csrf=…") goes round the same circle (the review of Oct 15: each
    press looked new, and the circle was never seen)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/cycle-posting.html") + query, title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "stuck" and "keeps sending me back to" in r.reason, (r.status, r.reason, r.log)
    assert "“Continue” on “Confirm Your Identity” (“We couldn't send a verification code" in r.reason
    assert [line for line in r.log if line.startswith("clicked")] == ["clicked “Apply Now”", "clicked “Next”",
                                                                       "clicked “Continue”"]


def test_tailoring_holds_a_job_until_its_resume_is_written(srv, monkeypatch):
    """With "Tailor my resume" on, a job waits before its tab opens and carries on once a
    resume made for it is in its folder; a draft that came out too long doesn't count."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    folder = Path(srv.tracker().get(job["id"])["folder"])
    applier = Applier(srv)
    applier.tailor = True

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            waited = (r.need, r.page, [x.job_id for x in applier.tailoring()])
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "Sam_Rivera_Resume.pdf").write_bytes(b"%PDF-1.4\n")
            (folder / "Sam_Rivera_Resume.too-long").touch()  # render_document asked for a shorter one
            applier.wake()
            await asyncio.sleep(1)
            held = r.need
            (folder / "Sam_Rivera_Resume.too-long").unlink()
            applier.wake()
            await until(lambda: r.status == "needs_you" and r.need != "tailor")
            return waited, held, r
        finally:
            await applier.stop()

    waited, held, r = run(go())
    assert waited == ("tailor", None, [job["id"]])  # no tab opened while it waits
    assert held == "tailor"
    assert r.need == "sign_in" and "clicked “Apply Manually”" in r.log, (r.reason, r.log)


def test_the_usual_resume_can_go_instead_of_a_tailored_one(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    applier.tailor = True

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            applier.use_usual_resume(job["id"])
            await until(lambda: r.status == "needs_you" and r.need != "tailor")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in", (r.reason, r.log)


def test_turning_tailoring_off_lets_waiting_jobs_go(srv):
    applier = Applier(srv)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    run_ = pipeline.Run(job["id"], status="needs_you", need="tailor")
    applier.runs[job["id"]] = run_

    async def go():
        applier.set_tailor(False)

    run(go())
    assert run_.status == "queued" and ("apply", job["id"]) in applier.tasks


def account_apply_run(srv, monkeypatch, saved=None, query=""):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    if saved:  # a password saved for this site (the fixture page stands in for SuccessFactors)
        monkeypatch.setattr(pipeline, "password_for", lambda url: "successfactors_password")
        monkeypatch.setenv("JOB_APPLY_SECRET_SUCCESSFACTORS_PASSWORD", saved)
    job = srv.add_job(url=fixture_url("site/account-apply.html") + query, title="ET", company="Example Semi")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            values = await r.page.evaluate("() => Object.fromEntries([...document.querySelectorAll('input')].map((i) => [i.id, i.value]))")
            return r, values
        finally:
            await applier.stop()

    r, values = run(go())
    return job, r, values


def test_a_page_that_creates_the_account_as_it_applies_is_filled_around_the_password(srv, monkeypatch):
    """Qorvo's SuccessFactors application is also its Create Account form, and its Apply
    sends both. The desk fills the application, leaves the password and that button to
    the person, and says so."""
    job, r, values = account_apply_run(srv, monkeypatch)
    assert r.need == "your_submit", (r.reason, r.log)
    assert "Choose a password" in r.reason and "press its Apply button yourself" in r.reason
    assert values["em"] == values["em2"] == "sam.rivera@example.com"
    assert (values["fn"], values["ln"], values["city"], values["zip"]) == ("Sam", "Rivera", "Chandler", "85225")
    assert values["pw"] == values["pw2"] == ""  # the person's to choose
    # what's recorded of the page is the page as filled: only the password boxes are left
    assert [f["label"] for f in r.page_info["fields"] if f["empty"]] == ["Choose Password: *", "Retype Password: *"]
    assert srv.tracker().get(job["id"])["status"] == "ready_to_submit"
    assert not any("clicked" in line for line in r.log)  # Apply sends it: never pressed


def test_a_saved_password_goes_into_both_boxes_of_such_a_page(srv, monkeypatch):
    job, r, values = account_apply_run(srv, monkeypatch, saved="Fake-Pass-123")
    assert r.need == "your_submit" and "Your saved password is in its password boxes" in r.reason, (r.reason, r.log)
    assert values["pw"] == values["pw2"] == "Fake-Pass-123"
    # and the desk's record of the page says so (it was read before they were filled)
    assert [f["empty"] for f in r.page_info["fields"] if f["kind"] == "password"] == [False, False]


@pytest.mark.parametrize("saved, late", [(None, "1500"), ("Fake-Pass-123", "1500"), ("Fake-Pass-123", "input")])
def test_such_a_page_is_known_when_its_application_draws_late(srv, monkeypatch, saved, late):
    """Qorvo's draws its application a moment after the account boxes above it. Live, with a
    saved password, the desk acted on the first look: it took the page for a plain Create
    Account form, filled in only the account part and asked the person to create the account.
    It waits a moment now, and looks again after filling the account part."""
    monkeypatch.setattr(pipeline, "ACCOUNT_DRAW_WAIT", 3)
    job, r, values = account_apply_run(srv, monkeypatch, saved=saved, query=f"?late={late}")
    assert r.need == "your_submit" and "press its Apply button yourself" in r.reason, (r.reason, r.log)
    assert (values["fn"], values["ln"], values["city"], values["zip"]) == ("Sam", "Rivera", "Chandler", "85225")
    assert values["pw"] == values["pw2"] == (saved or "")
    assert srv.tracker().get(job["id"])["status"] == "ready_to_submit"



def test_an_answer_the_page_turns_down_is_asked_again(srv, monkeypatch):
    """Qorvo, live: an answer that matched nothing in a SuccessFactors dropdown left its words
    in the box, the desk took the box for answered, and went on to the submit step while the
    site still said "Preferred Locale/Language is required"."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/sf-select-form.html"), title="ET", company="Example Semi")["job"]
    applier = Applier(srv)
    locale = question_key("Preferred Locale/Language")

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "questions", (r.reason, r.log)
            assert [q["label"] for q in r.questions] == ["Preferred Locale/Language"]  # Country was searched for

            r.once[locale] = "Klingon"
            applier.enqueue(job["id"], front=True)
            await until(lambda: any("didn't go in" in line for line in r.log) and r.status not in ("queued", "running"))
            assert r.need == "questions", (r.status, r.reason, r.log)
            assert [q["label"] for q in r.questions] == ["Preferred Locale/Language"]
            assert "Klingon" in r.questions[0]["error"]
            assert await r.page.input_value("[aria-label='Preferred Locale/Language']") == ""

            r.once[locale] = "English"
            applier.enqueue(job["id"], front=True)
            await until(lambda: r.status in ("ready", "failed") or r.status == "needs_you" and r.need != "questions")
            assert r.status == "ready", (r.reason, r.log)
            return await r.page.evaluate("() => [...document.querySelectorAll('input')].map((i) => i.value)")
        finally:
            await applier.stop()

    assert run(go()) == ["Sam", "United States", "English", ""]  # the optional veteran question is left


def test_a_persons_answer_goes_into_a_list_still_on_not_specified(srv, monkeypatch):
    """Kforce's Taleo (the probe of Oct 12): a required list that starts on "Not Specified", nothing chosen yet, was
    asked about (its "Not Specified" isn't an answer), but the person's answer ("Female") went only into an empty
    box, so it never went in, and the question came back again and again. It goes into a list still on its own
    "Not Specified"."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/select-no-placeholder.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    gender = question_key("3. Birth Assigned Gender . Required")

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"))
            assert r.need == "questions" and [q["label"] for q in r.questions] == ["3. Birth Assigned Gender . Required"], \
                (r.status, r.need, r.reason, r.log)
            r.once[gender] = "Female"
            applier.enqueue(job["id"], front=True)
            await until(lambda: r.status not in ("queued", "running"))
            return r, await r.page.input_value("#g")
        finally:
            await applier.stop()

    r, value = run(go())
    assert value == "Female" and r.status == "ready", (value, r.status, r.reason)


@pytest.mark.parametrize("answer", ["Female", "Male"])
def test_a_persons_answer_goes_into_a_list_on_the_sites_first_choice(srv, monkeypatch, answer):
    """A required list with no empty choice opens on "Male", which no one chose; the profile declines and the list has
    no decline, so the person is asked. Their answer for this application only goes in over the site's first choice,
    or, where it is that choice, is theirs now, rather than the question coming back again and again (the reviews of
    0.3.138)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/select-site-default.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"))
            assert r.need == "questions" and [q["label"] for q in r.questions] == ["Gender"], \
                (r.status, r.need, r.reason, r.log)
            r.once[question_key("Gender")] = answer
            applier.enqueue(job["id"], front=True)
            await until(lambda: r.status not in ("queued", "running"))
            return r, await r.page.input_value("#g")
        finally:
            await applier.stop()

    r, value = run(go())
    assert value == answer and r.status == "ready", (value, r.status, r.reason)


def test_an_answer_that_doesnt_stick_the_first_time_is_filled_again_not_asked_again(srv, monkeypatch):
    """Lam's and Micron's consent questions (Eightfold), live: an answer the person gave didn't
    stick the first time, and the desk asked for it again. It's filled in a second time first."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/drop-first-answer.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    badge = question_key("Which badge color do you prefer?")

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "questions" and [q["label"] for q in r.questions] == ["Which badge color do you prefer?"]
            r.once[badge] = "Green"
            applier.enqueue(job["id"], front=True)
            await until(lambda: r.status in ("ready", "failed") or r.status == "needs_you" and r.log[-1] != r.reason[:0])
            await until(lambda: r.status not in ("queued", "running"))
            assert r.status == "ready", (r.status, r.reason, r.log)
            return r, await r.page.input_value("#badge")
        finally:
            await applier.stop()

    r, value = run(go())
    assert value == "Green"
    assert sum("your profile doesn't answer" in line for line in r.log) == 1  # asked once


def test_a_turned_down_answer_is_asked_again_whatever_the_box_shows():
    """A widget that keeps the words after a failed pick still gets its question asked again,
    unless the profile's own answer went in after."""
    turned_down = {question_key("Preferred Locale/Language"): {
        "id": "28", "label": "Preferred Locale/Language", "kind": "combobox", "required": True,
        "error": "nothing in its list matched 'Klingon'"}}
    nothing_left = {"needs_input": [], "failed": [], "filled": []}
    pending, _ = pipeline._pending(nothing_left, turned_down)
    assert [(q["label"], q["error"]) for q in pending] == [("Preferred Locale/Language", "nothing in its list matched 'Klingon'")]
    profile_filled = {**nothing_left, "filled": [{"id": "28", "label": "Preferred Locale/Language", "value": "English"}]}
    assert pipeline._pending(profile_filled, turned_down)[0] == []


def test_the_persons_turned_down_answer_is_said_over_the_profiles():
    """HonorHealth's and Microchip's Workday (the probe of Oct 12): "How Did You Hear About Us?" was asked, the
    answer given opened a list of its own ("Employee Referral" › its entries), and the profile's answer, tried after
    it, was refused too. The question came back with the profile's refusal and the same first list, again and again.
    The person's own answer's refusal is what's said, with the entries it opened."""
    label = "How Did You Hear About Us?*"
    theirs = {question_key(label): {"id": "h", "label": label, "kind": "combobox", "required": True,
                                    "error": "PickedAGroup: 'Employee Referral' is a group; pick one of its entries",
                                    "options": ["Current Employee", "Former Employee"]}}
    result = {"needs_input": [], "filled": [],
              "failed": [{"id": "h", "label": label, "kind": "combobox", "required": True,
                          "error": "ValueError: 'Company Website' doesn't match any suggestion",
                          "options": ["Employee Referral", "Job Board", "Other"]}]}
    pending, _ = pipeline._pending(result, theirs)
    assert len(pending) == 1 and pending[0]["options"] == ["Current Employee", "Former Employee"], pending
    assert pending[0]["error"].startswith("PickedAGroup"), pending


def test_a_profile_answer_that_doesnt_go_in_is_asked_only_where_required():
    """A required field is asked about with its own choices. An optional one is skipped: Qorvo's
    optional veteran question (no "don't wish to answer") was asked over and over."""
    result = {"needs_input": [], "filled": [], "failed": [
        {"id": "17", "label": "Country", "value": "Atlantis", "error": "nothing in its list matched 'Atlantis'",
         "kind": "combobox", "required": True, "options": ["No Selection", "Afghanistan"]},
        {"id": "40", "label": "Pre-Offer : Are you a Protected Veteran?", "value": "I don't wish to answer",
         "error": "nothing in its list matched", "kind": "combobox", "required": False, "options": ["Yes", "No"]}]}
    pending, _ = pipeline._pending(result, {})
    assert [(q["label"], q["kind"], q["options"]) for q in pending] == [("Country", "combobox", ["No Selection", "Afghanistan"])]


def test_a_question_the_pages_errors_name_is_asked_though_not_marked_required():
    """Workday's veteran question (live test 2, issue #112, note 18) wasn't marked required, so the desk
    left it empty as optional, pressed Save and Continue three times, and only then stopped. The page's
    "Errors Found Error-Please indicate your veteran status." says it is: it's asked."""
    result = {"needs_input": [{"id": "v", "label": "Please indicate your veteran status.", "kind": "listbox",
                               "unmatched": "I am not a protected veteran"},
                              {"id": "g", "label": "Please select your gender.", "kind": "listbox"}],
              "failed": [], "filled": []}
    pipeline._required_by_errors(result, {"errors": ["Errors Found", "Error-Please indicate your veteran status."]})
    pending, _ = pipeline._pending(result, {})
    assert [f["id"] for f in pending] == ["v"], pending
    assert "isn't one of its choices" in pending[0]["error"], pending

    # or the section the page's errors name (Kforce's "The field Closest Metropolitan City is mandatory.")
    region = {"needs_input": [{"id": "r", "label": "Region", "kind": "select",
                               "section": "Closest Metropolitan City . Required"}], "failed": [], "filled": []}
    pipeline._required_by_errors(region, {"errors": ['The field "Closest Metropolitan City" is mandatory.']})
    assert [f["id"] for f in pipeline._pending(region, {})[0]] == ["r"]
    # not wherever a section's name comes up ("Please correct the errors in: Personal Information")
    middle = {"needs_input": [{"id": "m", "label": "Middle Name", "kind": "text", "section": "Personal Information"}],
              "failed": [], "filled": []}
    pipeline._required_by_errors(middle, {"errors": ["Please correct the errors in: Personal Information"]})
    assert pipeline._pending(middle, {})[0] == []

    # Baker Tilly's Workday (the probe of Oct 12): its errors read as none, but its error link names the question,
    # or the page marks the box itself invalid
    hispanic = "Please indicate if you are Hispanic or Latino?*"
    for data in ({"errors": [], "actions": [{"text": "Errors Found"}, {"text": "Error-" + hispanic.rstrip("*")}],
                  "fields": [{"id": "h", "label": hispanic}]},
                 {"errors": [], "actions": [], "fields": [{"id": "h", "label": hispanic, "invalid": True}]}):
        asked = {"needs_input": [{"id": "h", "label": hispanic, "kind": "listbox", "unmatched": "Decline to self-identify"}],
                 "failed": [], "filled": []}
        pipeline._required_by_errors(asked, data)
        assert [f["id"] for f in pipeline._pending(asked, {})[0]] == ["h"], data


def test_a_self_identification_answer_a_list_doesnt_have_is_said_not_left_silently():
    """Qorvo's optional veteran list has no "I don't wish to answer": the desk left it empty and
    said nothing, as if it had filled it. Left empty (no other choice is the person's), and the
    log says so, once; a required one is asked with the reason."""
    vevraa = "U.S. Protected Veteran Self-Identification. " + "This employer is a Government contractor. " * 10
    result = {"filled": [], "failed": [], "needs_input": [
        {"id": "31", "label": "Pre-Offer : Are you a Protected Veteran?", "kind": "combobox", "required": False,
         "options": ["No Selection", "No, I am not a Protected Veteran", "Yes, I am a Protected Veteran"],
         "unmatched": "I don't wish to answer"},
        {"id": "32", "label": vevraa, "kind": "combobox", "required": False, "options": ["Yes", "No"],
         "unmatched": "I don't wish to answer"},
        {"id": "29", "label": "Gender", "kind": "combobox", "required": True, "options": ["Female", "Male"],
         "unmatched": "Decline to self-identify"},
        {"id": "40", "label": "Preferred Locale/Language", "kind": "combobox", "required": False, "options": ["English"]}]}
    pending, _ = pipeline._pending(result, {})
    assert [(q["label"], q["error"]) for q in pending] == [
        ("Gender", "your profile's answer “Decline to self-identify” isn't one of its choices")]
    r = Run(1)
    applier = Applier(None)
    applier._note_skipped(r, result)
    applier._note_skipped(r, result)  # the same page filled again
    assert r.log == ["left 2 optional question(s) empty, as none of their choices is your profile's answer: "
                     "“Pre-Offer : Are you a Protected Veteran” (yours: “I don't wish to answer”), "
                     "“U.S. Protected Veteran Self-Identification. This employer is a Government…” "
                     "(yours: “I don't wish to answer”)"], r.log



def test_an_older_successfactors_posting_is_applied_to_through_its_apply(srv, monkeypatch):
    """Amkor, live (Oct 2026): its posting's "Apply" is a form's submit button, which the desk
    took for the final one, and it stopped ("couldn't find the button that moves this
    application on")."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = fixture_url("site/sf-classic-posting.html") + "?career_ns=job_listing&company=example&career_job_req_id=29107"
    job = srv.add_job(url=url, title="Equipment Technician (ATA)", company="Example Semi")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in", (r.reason, r.log)
    assert "clicked \u201cApply\u201d" in r.log


def test_a_sign_in_by_hand_mentions_the_password_the_desk_could_save(monkeypatch):
    """The tip at a sign-in names the system whose password the desk page saves; none for a
    system it doesn't, or once that password is saved."""
    monkeypatch.setattr(pipeline, "_secret", lambda name: None)
    assert "Save a SuccessFactors password" in pipeline._password_tip("https://career8.successfactors.com/career?x=1")
    assert "Save a Workday password" in pipeline._password_tip("https://intel.wd1.myworkdayjobs.com/External/login")
    assert "Save an Infor password" in pipeline._password_tip("https://css-benchmark-prd.inforcloudsuite.com/sso/SSOServlet")
    assert "Save an ApplicantStack password" in pipeline._password_tip("https://seus.applicantstack.com/x/login")
    assert "Save a UKG Pro password" in pipeline._password_tip("https://signin-us.ultipro.com/u/login")
    assert "Save a Taleo password" in pipeline._password_tip("https://acme.taleo.net/careersection/iam/login.jsf")
    assert pipeline._password_tip("https://sjobs.brassring.com/TGnewUI/Home") == ""  # not offered on the page
    assert pipeline._password_tip("https://example.com/careers/login") == ""
    monkeypatch.setattr(pipeline, "_secret", lambda name: "saved")
    assert pipeline._password_tip("https://career8.successfactors.com/career?x=1") == ""


def test_past_a_pause_takes_looks_a_poll_apart(monkeypatch):
    """Two looks a moment apart (a page between two states) don't make a pause passed; looks
    a poll apart, with none between them saying otherwise, do."""
    clock = [1000.0]
    monkeypatch.setattr(pipeline.time, "time", lambda: clock[0])
    r = Run(1)
    assert not Applier._twice(r, True)
    clock[0] += 0.2
    assert not Applier._twice(r, True)  # too soon
    clock[0] += pipeline.POLL_SECONDS
    assert Applier._twice(r, True)
    assert not Applier._twice(r, False)  # a look that says otherwise starts it over
    clock[0] += pipeline.POLL_SECONDS
    assert not Applier._twice(r, True)


# Fake employer sites at https://<name>.example, served through the browser (no network). A
# form's POST is recorded, and answered with PAGES[("POST", address)] or a thank-you page.
def _form(company: str, *questions: str, action: str = "/posted", button: str = "Submit Application",
          footer: bool = False) -> str:
    asked = "".join(f'<label for="q{i}">{q} *</label><select id="q{i}" name="q{i}" required><option value="">'
                    'Select One</option><option>Yes</option><option>No</option></select>' for i, q in enumerate(questions))
    alerts = ('<footer><form id="alerts" method="post" action="/job-alerts"><label for="ae">Get job alerts by email'
              '</label><input id="ae" name="ae"><button type="submit">Submit</button></form></footer>') if footer else ""
    return (f'<html><head><title>{company} application</title></head><body><h1>Apply: Field Service Engineer</h1>'
            f'<form method="post" action="{action}"><label for="fn">First Name *</label><input id="fn" name="fn" required>'
            f'<label for="ln">Last Name *</label><input id="ln" name="ln" required>{asked}'
            f'<button type="submit">{button}</button></form>{alerts}</body></html>')


async def _serve(srv, pages: dict, posts: list) -> None:
    async def handler(route):
        req = route.request
        url = req.url.split("?")[0]
        if req.method == "POST":
            posts.append(url)
            body = pages.get(("POST", url), "<html><body><h1>Thank you for applying</h1></body></html>")
        else:
            body = pages.get(url, "<html><body>not found</body></html>")
        await route.fulfill(status=200, content_type="text/html", body=body)

    await srv.browser.page()
    await srv.browser._ctx.route("https://**.example/**", handler)


@pytest.mark.parametrize("how", ["claude", "elsewhere"])
def test_a_paused_jobs_tab_taken_to_another_posting_isnt_filled_as_that_job(srv, monkeypatch, how):
    """Job A waits on a question. Meanwhile its tab goes to job B: Claude opens B with
    open_application (the tools were left on A's tab), or the person takes the tab there.
    Answered, A carries on in a tab of its own: B's form is never filled with A's details,
    nor, with Submit for me on, sent as A's application."""
    from types import SimpleNamespace

    from job_apply import desk as desk_module

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    a_url, b_url = "https://careers.acme-fab.example/apply/1", "https://careers.other-litho.example/apply/2"
    pages = {a_url: _form("Acme Fab", "Do you hold an active TS/SCI clearance?"), b_url: _form("Other Litho")}
    posts: list[str] = []
    a = srv.add_job(url=a_url, title="Field Service Engineer", company="Acme Fab")["job"]
    b = srv.add_job(url=b_url, title="Litho Technician", company="Other Litho")["job"]
    applier = Applier(srv)
    applier.auto_submit = True
    monkeypatch.setattr(desk_module, "_desk", SimpleNamespace(applier=applier))

    async def go():
        await _serve(srv, pages, posts)
        applier.start()
        try:
            r = applier.enqueue(a["id"], submit=True)
            await until(lambda: r.status == "needs_you", about=state(r))
            assert r.need == "questions", r.reason
            if how == "claude":
                await srv.open_application(job_id=b["id"])
                assert r.page.url == a_url  # opened in a tab of its own
            else:
                await r.page.goto(b_url)
            r.once[question_key(r.questions[0]["label"])] = "No"
            applier.enqueue(a["id"], submit=True, front=True)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert not any("other-litho" in p for p in posts), posts
    assert r.status == "submitted" and posts == ["https://careers.acme-fab.example/posted"], (r.status, r.reason, posts)
    assert srv.tracker().get(b["id"])["status"] != "applied"


EXAMPLE_CORP = "https://careers.example-corp.example"


def _example_corp(srv, monkeypatch, loads: list[str]) -> str:
    """Example Corp's careers site, its application all on one page as Eightfold's is, served
    through the browser (and through the browser opened again, after it closed). Each address
    loaded goes into `loads`. Returns the job's posting."""
    site = Path(__file__).parent / "fixtures" / "site"
    pages = {f"{EXAMPLE_CORP}{path}": (site / name).read_text(encoding="utf-8") for path, name in (
        ("/careers/job/1", "one-page-posting.html"), ("/careers/apply", "one-page-apply.html"),
        ("/careers", "one-page-home.html"))}

    async def handler(route):
        if route.request.method == "GET":
            loads.append(route.request.url)
        body = pages.get(route.request.url.split("?")[0], "<html><body><h1>Thank you for applying</h1></body></html>")
        await route.fulfill(status=200, content_type="text/html", body=body)

    real_launch = srv.browser._launch

    async def launch():
        await real_launch()
        await srv.browser._ctx.route("https://**.example/**", handler)

    monkeypatch.setattr(srv.browser, "_launch", launch)
    if srv.browser._ctx is not None:  # (open already: served from now on)
        run(srv.browser._ctx.route("https://**.example/**", handler))
    return f"{EXAMPLE_CORP}/careers/job/1"


async def _crash(tab):
    """Crash a tab's page, as Chrome's "Aw, Snap!" is: by the browser's own Page.crash (on CI, under load, a navigation
    to chrome://crash now and then never crashed it, #215), and that navigation only if it didn't."""
    crashed = []
    tab.once("crash", lambda _: crashed.append(True))
    with contextlib.suppress(Exception):
        cdp = await tab.context.new_cdp_session(tab)
        await asyncio.wait_for(cdp.send("Page.crash"), 5)  # (it never answers: its page is gone)
    for _ in range(50):
        if crashed:
            break
        await asyncio.sleep(0.2)
    if not crashed:
        with contextlib.suppress(Exception):
            await tab.goto("chrome://crash", timeout=5000)
    await until(lambda: crashed)


@pytest.mark.parametrize("how", ["answered", "resumed", pytest.param("crashed", marks=pytest.mark.own_browser),
                                 "closed", "browser", "home"])
def test_a_job_picked_up_again_carries_on_with_the_form_in_its_tab(srv, monkeypatch, how):
    """Eightfold, live (Oct 2026): a job waiting on a question on its one-page form was picked
    up again by opening its posting and pressing Apply Now, onto a fresh form without the resume
    uploaded and the boxes filled in the old one. Answered on the desk, or in the browser and
    Resume pressed, it carries on with the form in its tab. Only a tab that's gone (closed,
    crashed, or the browser with it) or that has left the application (back to the careers home,
    whose Apply is another job's) has the job opened again, and the desk says why."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    loads: list[str] = []
    posting = _example_corp(srv, monkeypatch, loads)
    job = srv.add_job(url=posting, title="Field Service Engineer", company="Example Corp")["job"]
    applier = Applier(srv)

    def form(tab):  # (a page loaded again has another loadedAt)
        return tab.evaluate("() => [window.loadedAt, cv.files.length, fn.value, sp.value, ge.value, ts.value]")

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you", about=state(r))
            assert r.need == "questions" and len(r.questions) == 1 and "TS/SCI" in r.questions[0]["label"], r.reason
            tab, before = r.page, await form(r.page)
            if how == "resumed":
                await tab.select_option("#ts", "No")  # answered in the browser instead
            else:
                r.once[question_key(r.questions[0]["label"])] = "No"
            if how == "crashed":
                await _crash(tab)
            elif how == "closed":
                await tab.close()
            elif how == "browser":
                await srv.browser.close()
            elif how == "home":
                await tab.goto(f"{EXAMPLE_CORP}/careers")  # the person, looking at the site's other jobs
            applier.enqueue(job["id"], front=True)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, tab, before, await form(r.page) if r.status == "ready" else None
        finally:
            await applier.stop()

    r, tab, before, after = run(go())
    assert before[1:] == [1, "Sam", "No", "Decline to self-identify", ""]  # its resume and boxes, filled
    assert r.status == "ready" and r.page.url == f"{EXAMPLE_CORP}/careers/apply?pid=1", (r.status, r.reason, r.log)
    assert after[1:] == [1, "Sam", "No", "Decline to self-identify", "No"]
    assert not any("pid=2" in u for u in loads), loads  # the careers home's other job is never applied to
    since = r.log[next(i for i, line in enumerate(r.log) if "your profile doesn't answer" in line):]  # from the pause on
    if how in ("answered", "resumed"):
        # the same tab, its page never loaded again: what was uploaded and filled there is kept
        assert r.page is tab and after[0] == before[0], r.log
        assert loads.count(posting) == 1 and not any(line.startswith(("opened", "its tab")) for line in since), r.log
    else:
        said: tuple[str, ...] = ({"crashed": "its tab crashed", "closed": "its tab was closed",
                                  "browser": "the browser had closed",
                                  "home": "its tab had gone back to the site's careers home"}[how],)
        if how == "crashed" and tab.context is not srv.browser._ctx:
            # Chromium sometimes goes down whole with the crashed tab (Windows CI, and a loaded machine, Oct 13):
            # then the desk may have seen the browser closed, and says so. (A browser started again since, to open
            # the job, is no sign of that: the desk said "its tab crashed" then, on Linux CI, Oct 13)
            said += ("the browser had closed",)
        assert r.page is not tab and loads.count(posting) == 2, (loads, r.log)
        # (in full, as a string: a list is cut short in pytest's report, and on Windows CI this has failed now and
        # then with the line that says why cut off, Oct 11)
        assert any(line.startswith(said) for line in since), "\n".join(since)
        if how == "home":  # the person's to look at still
            assert not tab.is_closed() and tab.url == f"{EXAMPLE_CORP}/careers"
        else:
            assert tab.is_closed()  # (a crashed tab's "Aw, Snap!" is closed for the new one)


def test_a_step_that_redraws_but_doesnt_move_on_stops_after_three_presses(srv, monkeypatch):
    """Matheson's Cornerstone application, in a live run under load (Oct 10): its Next didn't move
    on (the step wanted a resume), but the page redrew a little after each press, so no press looked
    like a stall and the desk pressed Next fifteen times, then said the application had more steps
    than expected. The same button on the same step three times is a page that isn't moving on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/next-redraws.html"), title="Technician", company="Example Gases")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => window.presses")
        finally:
            await applier.stop()

    r, presses = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert r.reason.startswith("The page didn't move on: * Resume/CV is required."), r.reason
    assert presses == 3, (presses, r.log)


@pytest.mark.own_browser  # (it crashes a tab: now and then Chromium goes down whole with it)
def test_a_crashed_tab_closed_since_is_still_said_to_have_crashed(srv):
    """A job's tab that crashed and was closed after it (by Chrome, or the desk) crashed: a picked-up
    job said "its tab was closed" for one (a full test run under load, Oct 10)."""
    async def go():
        tab = await srv.browser.page()
        await tab.goto(fixture_url("generic_form.html"))
        await _crash(tab)
        crashed = srv.browser.lost(tab)
        await tab.close()
        return crashed, srv.browser.lost(tab)

    assert run(go()) == ("crashed", "crashed")


@pytest.mark.own_browser  # (it crashes a tab: now and then Chromium goes down whole with it)
def test_a_tab_that_crashes_while_its_job_runs_is_opened_again_on_resume(srv, monkeypatch):
    """A tab whose page crashes while the desk fills it is no use to the desk again, even
    reloaded: the job says so, and Resume opens it in a new tab. (Each Resume failed again in
    the crashed tab: "Something went wrong: TargetClosedError: ... Page crashed".)"""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    posting = _example_corp(srv, monkeypatch, [])
    job = srv.add_job(url=posting, title="Field Service Engineer", company="Example Corp")["job"]
    applier = Applier(srv)
    real_click = srv.click
    crashed = []

    async def click_then_crash(target):
        out = await real_click(target)
        if not crashed:
            crashed.append(srv.browser.current_tab)
            await _crash(crashed[0])
        return out

    monkeypatch.setattr(srv, "click", click_then_crash)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            said = r.reason
            applier.enqueue(job["id"], front=True)  # Resume
            await until(lambda: r.status == "needs_you", about=state(r))
            return r, said
        finally:
            await applier.stop()

    r, said = run(go())
    assert said == "Its tab crashed. Press Carry on to start this application again.", said
    assert r.need == "questions" and r.page is not crashed[0] and crashed[0].is_closed(), (r.reason, r.log)
    assert any(line.startswith("its tab crashed, so I opened the job again") for line in r.log), r.log


def test_a_browser_closed_while_its_job_runs_is_said_so_and_opened_again_on_resume(srv, monkeypatch):
    """The automation browser closed under a job in progress (issue #112, note 36: the person quit to stop the queue)
    said "Something went wrong: TargetClosedError: Target page, context or browser has been closed". It says that
    the browser was closed, and Resume opens the application again in a new one."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    posting = _example_corp(srv, monkeypatch, [])
    job = srv.add_job(url=posting, title="Field Service Engineer", company="Example Corp")["job"]
    applier = Applier(srv)
    real_click = srv.click
    closed = []

    async def click_then_close(target):
        out = await real_click(target)
        if not closed:
            closed.append(srv.browser.current_tab)
            await srv.browser.close()
            await closed[0].title()  # (raises as Playwright does for a page of a browser that's gone)
        return out

    monkeypatch.setattr(srv, "click", click_then_close)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            said = r.reason
            applier.enqueue(job["id"], front=True)  # Resume
            await until(lambda: r.status == "needs_you", about=state(r))
            return r, said
        finally:
            await applier.stop()

    r, said = run(go())
    assert said == "The desk's browser window was closed. Press Carry on to start this application again.", said
    assert r.need == "questions" and r.page is not closed[0] and closed[0].is_closed(), (r.reason, r.log)


def test_a_paused_queue_starts_no_job_until_it_is_resumed(srv, monkeypatch):
    """A queue of about 90 jobs had no way to stop but quitting Claude Code, which broke the job in progress
    (issue #112, note 36). With the queue paused nothing new starts; Resume queue carries on with all of it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    a_url, b_url = "https://careers.acme-fab.example/apply/1", "https://careers.other-litho.example/apply/2"
    pages = {a_url: _form("Acme Fab"), b_url: _form("Other Litho")}
    a = srv.add_job(url=a_url, title="Field Service Engineer", company="Acme Fab")["job"]
    b = srv.add_job(url=b_url, title="Litho Technician", company="Other Litho")["job"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, [])
        applier.start()
        try:
            applier.pause_queue()
            first, second = applier.enqueue(a["id"]), applier.enqueue(b["id"])
            await asyncio.sleep(1.5)  # (five polls)
            held = (first.status, second.status, list(applier.tasks), applier.current)
            applier.resume_queue()
            await until(lambda: first.status == second.status == "ready", about=state(second))
            return held, applier.paused
        finally:
            await applier.stop()

    held, paused = run(go())
    assert held == ("queued", "queued", [("apply", a["id"]), ("apply", b["id"])], None), held
    assert not paused


def test_a_resume_the_person_pressed_goes_while_the_queue_is_paused(srv, monkeypatch):
    """The review of note 36: a job's own Resume (or answers on its card) while the queue is paused only queued it,
    and its card said "queued" with nothing happening. The person's own press goes; the rest stay paused."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    a_url, b_url = "https://careers.acme-fab.example/apply/1", "https://careers.other-litho.example/apply/2"
    pages = {a_url: _form("Acme Fab"), b_url: _form("Other Litho")}
    a = srv.add_job(url=a_url, title="Field Service Engineer", company="Acme Fab")["job"]
    b = srv.add_job(url=b_url, title="Litho Technician", company="Other Litho")["job"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, [])
        applier.start()
        try:
            applier.pause_queue()
            first, second = applier.enqueue(a["id"]), applier.enqueue(b["id"])
            applier.resume(b["id"])
            await until(lambda: second.status == "ready", about=state(second))
            return first.status
        finally:
            await applier.stop()

    assert run(go()) == "queued"


def test_a_submit_the_person_pressed_still_goes_while_the_queue_is_paused(srv, monkeypatch):
    """Pause queue holds the jobs that haven't started, not the person's own press of Submit on one that waits on
    its review page (issue #112, note 36)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    a_url, b_url = "https://careers.acme-fab.example/apply/1", "https://careers.other-litho.example/apply/2"
    pages = {a_url: _form("Acme Fab"), b_url: _form("Other Litho")}
    posts: list[str] = []
    a = srv.add_job(url=a_url, title="Field Service Engineer", company="Acme Fab")["job"]
    b = srv.add_job(url=b_url, title="Litho Technician", company="Other Litho")["job"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, posts)
        applier.start()
        try:
            first = applier.enqueue(a["id"])
            await until(lambda: first.status == "ready", about=state(first))
            applier.pause_queue()
            second = applier.enqueue(b["id"])
            applier.submit_now(a["id"])
            await until(lambda: first.status == "submitted", about=state(first))
            await asyncio.sleep(1)
            held = second.status
            applier.resume_queue()
            await until(lambda: second.status == "ready", about=state(second))
            return held
        finally:
            await applier.stop()

    held = run(go())
    assert held == "queued" and posts == ["https://careers.acme-fab.example/posted"], (held, posts)


def test_submit_presses_the_applications_button_not_a_footer_alerts_one(srv, monkeypatch):
    """A one-page application, and the site's footer job-alerts box with a "Submit" of its own
    (whose "Thank you for your interest!" reads like a confirmation). The person's Submit
    sends the application."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme-fab.example/apply/1"
    pages = {url: _form("Acme Fab", action="/application", footer=True),
             ("POST", "https://careers.acme-fab.example/job-alerts"): "<p>Thank you for your interest! New jobs by email.</p>"}
    posts: list[str] = []
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, posts)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            assert r.status == "ready", r.reason
            applier.submit_now(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert posts == ["https://careers.acme-fab.example/application"], posts
    assert r.status == "submitted" and srv.tracker().get(job["id"])["status"] == "applied"


def test_a_footer_alerts_submit_is_marked_aside(srv):
    run(srv.browser.goto(fixture_url("site/next-and-submit.html")))
    actions = {a["text"]: a for a in run(srv.inspect_form(include_dropdown_options=False))["actions"]}
    assert actions["Submit"].get("aside") and actions["Submit"].get("is_submit")
    assert not actions["Next"].get("aside")
    assert run(srv.browser.find_submit()) == []


def test_a_submit_with_no_confirmation_isnt_pressed_again_by_submit_for_me(srv, monkeypatch):
    """Submit for me pressed Submit and the site's thank-you used words the desk doesn't know.
    After a restart (a new desk), the job picked again is filled up to its review page and
    left for the person: it may already have gone."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme-fab.example/apply/1"
    pages = {url: _form("Acme Fab"),
             ("POST", "https://careers.acme-fab.example/posted"): "<h1>All set!</h1><p>Our team will be in touch.</p>"}
    posts: list[str] = []
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]

    async def session():
        applier = Applier(srv)
        applier.auto_submit = True
        applier.start()
        try:
            r = applier.enqueue(job["id"], submit=True)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    async def go():
        await _serve(srv, pages, posts)
        return await session(), await session()

    first, second = run(go())
    assert (first.status, first.need) == ("needs_you", "check_submit"), first.reason
    assert second.status == "ready" and "pressed for this job before" in second.reason, (second.status, second.reason)
    assert len(posts) == 1, posts
    assert any("no confirmation" in e["note"] for e in srv.tracker().events(job["id"]))


def _desks_applier(srv) -> Applier:
    """An applier that keeps its queue for a restart, as the Job Desk's does."""
    applier = Applier(srv)
    applier.keep_queue = True
    return applier


def test_a_submit_pressed_before_a_restart_is_never_carried_over(srv, monkeypatch):
    """The desk's queue, kept across a restart (the review of Oct 15, worth building 4), never carries a Submit over.
    The person pressed Submit for one job while another's was being pressed (its site slow to answer), and the desk
    restarted before either was done. The one whose Submit waited its turn comes back on a card that says that Submit
    wasn't sent: its Resume fills it again up to its review page, even with Submit for me on, and pressed again there,
    it goes. The one whose Submit was being pressed may have gone in: its card says to check. A job never begun, queued
    with Submit for me on, goes in after the restart (once the person resumes the queue), as it's still on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    sites = ("acme-fab", "other-litho", "third-tool")
    pages = {f"https://careers.{s}.example/apply/1": _form(s.replace("-", " ").title()) for s in sites}
    posts: list[str] = []
    pressed, pressing, for_me = (srv.add_job(url=f"https://careers.{s}.example/apply/1", title="FSE",
                                             company=s.replace("-", " ").title())["job"]["id"] for s in sites)
    real_submit = srv.submit_application
    pressing_now: list[int] = []

    async def slow_submit(**kwargs):  # (the site slow to answer: its press under way as the desk restarts)
        pressing_now.append(kwargs["job_id"])
        await asyncio.Event().wait()

    async def go():
        await _serve(srv, pages, posts)
        before = _desks_applier(srv)
        before.start()
        one, two = before.enqueue(pressed), before.enqueue(pressing)
        await until(lambda: one.status == two.status == "ready", about=lambda: (state(one)(), state(two)()))
        before.pause_queue()
        before.auto_submit = True
        before.enqueue(for_me, submit=True)  # (Submit for me on for it: waiting its turn, the queue paused)
        monkeypatch.setattr(srv, "submit_application", slow_submit)
        before.submit_now(pressing)
        await until(lambda: pressing_now == [pressing], about=state(two))
        before.submit_now(pressed)  # (waits its turn: one job at a time)
        await asyncio.sleep(1)
        assert one.status == "queued" and ("submit", pressed) in before.tasks
        await before.stop()  # the restart
        monkeypatch.setattr(srv, "submit_application", real_submit)

        after = _desks_applier(srv)
        after.auto_submit = True  # (still on)
        after.start()
        try:
            one, two, mine = after.runs[pressed], after.runs[pressing], after.runs[for_me]
            assert after.paused and [j for _, j in after.tasks] == [for_me] and mine.submit
            assert (one.status, one.need, one.left_at_restart, one.submit) == ("needs_you", "restarted", True, False)
            assert one.reason == pipeline.RESTART_BEGUN + " Your Submit from before the restart wasn't sent.", one.reason
            assert (two.status, two.need, two.left_at_restart, two.reason) == ("needs_you", "check_submit", True,
                                                                               pipeline.RESTART_CHECK)
            after.resume_queue()
            await until(lambda: mine.status == "submitted", about=state(mine))
            assert posts == ["https://careers.third-tool.example/posted"], posts
            after.resume(pressed)  # its card's Resume: filled again, Submit for me on, and not sent
            await until(lambda: one.status not in ("queued", "running"), about=state(one))
            assert one.status == "ready" and one.reason.endswith(pipeline.SUBMIT_UNSENT), (one.status, one.reason)
            await asyncio.sleep(1)
            assert posts == ["https://careers.third-tool.example/posted"], posts
            after.submit_now(pressed)  # pressed again: it goes
            await until(lambda: one.status not in ("queued", "running"), about=state(one))
            assert one.status == "submitted" and not one.submit_unsent, (one.status, one.reason)
            assert two.status == "needs_you" and pressing not in [j for _, j in after.tasks]
        finally:
            await after.stop()

    run(go())
    assert posts == ["https://careers.third-tool.example/posted", "https://careers.acme-fab.example/posted"], posts


def test_a_submit_the_desk_cant_record_as_being_sent_isnt_pressed(srv, monkeypatch):
    """The review of the restart (L6): a Submit is kept as being pressed while it is, so a restart meanwhile never
    queues it again. Where that can't be written (a full disk, where the job's own record of the press may not be
    written either), a restart mid-press couldn't tell it may have gone: it isn't pressed, and the card asks the person
    to press the site's Submit themselves."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme-fab.example/apply/1"
    posts: list[str] = []
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]["id"]

    async def go():
        await _serve(srv, {url: _form("Acme Fab")}, posts)
        applier = _desks_applier(srv)
        applier.start()
        try:
            r = applier.enqueue(job)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            assert r.status == "ready", r.reason
            kept = pipeline.queue_path()
            kept.unlink()
            kept.mkdir()  # (nothing can be written there now, as on a full disk)
            applier.submit_now(job)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "your_submit") and "couldn't record" in r.reason, (r.status, r.reason)
    assert posts == [] and not r.sending


def test_a_draft_the_person_took_over_isnt_rechecked_after_a_restart(srv, monkeypatch):
    """The review of the restart (L4): that a saved draft is the person's now (their Resume, or a stop where they may
    have changed its boxes: Run.draft_off) lived in memory alone, so after a restart the job, picked again from the
    list, had its draft re-checked against the profile over what they had changed. It's kept in the job's folder
    (beside the draft's changes), and the draft is left as it is."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/workday-continue-posting.html"), title="Equipment Technician",
                      company="Example Fab")["job"]
    # before the restart: the draft went on with after the person's Resume
    Applier(srv)._draft_off(Run(job["id"], "Equipment Technician", "Example Fab", draft=True))
    applier = Applier(srv)  # (the desk, restarted)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])  # (ticked again in the list: no Resume of its own)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and "review.html" in r.url, (r.status, r.reason, r.log)
    saved = dict(parse_qsl(urlparse(r.url).query))
    assert (saved["ln"], saved["rd-1"]) == ("Rivers", "Ran the wet benches on nights."), (saved, r.log)
    assert r.draft and r.draft_off and not r.draft_changed, r.log
    assert not any(line.startswith("re-checked the saved draft's answers") for line in r.log), r.log


def test_the_step_before_the_review_page_isnt_taken_for_it(srv, monkeypatch):
    """Step 3 of 4 goes on with "Review"; its footer has a job-alerts box with a "Submit".
    Submit for me presses Review, then the review page's own Submit."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    assert pipeline._FORWARD.match("Review") and pipeline._FORWARD.match("Review Application")
    url = "https://careers.acme-fab.example/apply/step3"
    review = ('<html><body><div>current step 4 of 4</div><h2>Review</h2><p>First Name: Sam</p>'
              '<form method="post" action="/posted"><button type="submit">Submit Application</button></form></body></html>')
    pages = {url: _form("Acme Fab", action="/review-page", button="Review", footer=True),
             ("POST", "https://careers.acme-fab.example/review-page"): review,
             ("POST", "https://careers.acme-fab.example/job-alerts"): "<p>You are now subscribed to job alerts.</p>"}
    posts: list[str] = []
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]
    applier = Applier(srv)
    applier.auto_submit = True

    async def go():
        await _serve(srv, pages, posts)
        applier.start()
        try:
            r = applier.enqueue(job["id"], submit=True)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert posts == ["https://careers.acme-fab.example/review-page", "https://careers.acme-fab.example/posted"], \
        (posts, r.status, r.reason, r.log)
    assert r.status == "submitted", r.reason


def test_practice_mode_doesnt_call_a_posting_the_review_page(srv, monkeypatch):
    """In practice mode a posting whose "Quick Apply" sends a form isn't pressed. That stops
    the job, said as such: nothing was filled, so it's no review page."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    url = "https://careers.acme-fab.example/jobs/9"
    pages = {url: '<html><body><h1>Field Service Engineer</h1><p>Service and repair of equipment in the field.</p>'
                  '<form method="post" action="/quick-apply"><button type="submit">Quick Apply</button></form></body></html>'}
    posts: list[str] = []
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, posts)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck") and "Practice mode" in r.reason, (r.status, r.reason)
    assert srv.tracker().get(job["id"])["status"] != "ready_to_submit" and posts == []


@pytest.mark.parametrize("need", ["captcha", "submit_failed"])
def test_a_closed_tab_at_submit_waits_on_resume(srv, need):
    """Submit pressed on the desk for a job whose tab was closed: it says to press Carry on, and
    waits as a job the desk page offers Resume for (not on a CAPTCHA, which has no Resume)."""
    job = srv.add_job(url=fixture_url("site/review.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        tab = await srv.browser.new_tab()
        await tab.close()
        r = Run(job["id"], "FSE", "Example Fab", status="needs_you", need=need, page=tab)
        applier.runs[job["id"]] = r
        applier.submit_now(job["id"])
        await applier._strict(applier._submit(r))
        return r

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck") and "Press Carry on" in r.reason, (r.status, r.need, r.reason)


def test_an_emailed_code_put_in_between_jobs_gives_the_tools_back(srv, monkeypatch):
    """A job left waiting on its emailed code; Claude at work on another job in a tab of its
    own. The code goes in with the tools held (Claude's calls wait), and they're given back on
    Claude's tab and job: its next fill doesn't land in the waiting job's page."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 1)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    waiting = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)
    sent = {"yet": False}
    held_by = []
    real_type = srv.browser.type_code

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        return pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time()) if sent["yet"] else None

    async def type_code(ids, code):
        if code == "123456":
            held_by.append(applier.current)
        return await real_type(ids, code)

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)
    monkeypatch.setattr(srv.browser, "type_code", type_code)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(waiting["id"])
            await until(lambda: r.left, about=state(r))  # no one at the browser: the queue went on
            claude_tab = await srv.browser.new_tab()  # Claude's own
            await srv.open_application(job_id=other["id"])
            sent["yet"] = True
            # (the step ends once the page it stopped on is saved: then the tools are given back)
            await until(lambda: (r.need == "questions" or r.status == "ready") and applier.current is None,
                        about=state(r))
            return r, srv.browser.current_tab is claude_tab, srv.browser.current_job_id
        finally:
            await applier.stop()

    r, on_claudes_tab, job_id = run(go())
    assert "entered the code from your email (sent from careers.example.com)" in r.log, r.log
    assert held_by == [waiting["id"]]
    assert on_claudes_tab and job_id == other["id"]


def test_a_profile_typo_doesnt_stop_the_inbox_watch_from_saying_so(srv, monkeypatch, job_apply_home):
    """profile.yaml broken while a job waits on an emailed code: the inbox isn't read, the
    desk says why, and the queue's loop carries on (it used to fail every tick)."""
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    (job_apply_home / "profile.yaml").write_text("personal:\n  email: sam@example.com\n   phone: [oops\n", encoding="utf-8")
    applier = Applier(srv)
    assert applier.mail_login() is None
    assert "profile.yaml" in (applier.mail_problem or "")


def test_submit_on_the_desk_isnt_pressed_in_a_tab_taken_to_another_posting(srv, monkeypatch):
    """Job A waits on its review page; its tab has since gone to job B's application. The
    desk's Submit for A doesn't press B's button (it would send B, recorded as A)."""
    a_url, b_url = "https://careers.acme-fab.example/apply/1", "https://careers.other-litho.example/apply/2"
    pages = {a_url: _form("Acme Fab"), b_url: _form("Other Litho")}
    posts: list[str] = []
    a = srv.add_job(url=a_url, title="FSE", company="Acme Fab")["job"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, posts)
        tab = await srv.browser.new_tab()
        await tab.goto(b_url)
        r = Run(a["id"], "FSE", "Acme Fab", status="ready", seen_form=True, page=tab, url=a_url)
        applier.runs[a["id"]] = r
        applier.submit_now(a["id"])
        await applier._strict(applier._submit(r))
        return r

    r = run(go())
    assert posts == [] and (r.status, r.need) == ("needs_you", "stuck") and "Carry on" in r.reason, (r.status, r.reason)
    assert srv.tracker().get(a["id"])["status"] != "applied"


def test_an_emailed_code_is_read_only_from_its_own_mail_and_its_own_site(srv, monkeypatch):
    """Two jobs on one site wait on emailed codes. The later one doesn't look back past its own
    wait (mail from before it is the earlier job's), the earlier one stops at the later one's
    wait, and a link only counts when its address is the job's own site: not another address
    that merely names a job system in its path, nor another employer's Workday."""
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    verify = "https://careers.acme-fab.example/verify"
    pages = {verify: '<html><body><h1>Confirm your email</h1><label for="code">Enter the verification code we sent to '
                     'your email</label><input id="code" type="text"><button type="button">Verify</button></body></html>'}
    asked = {}

    def search(address, password, since, senders, want, allowed_link, before=None, look_back=pipeline.mailbox.LOOK_BACK,
               others=None):
        asked[since] = (before, look_back, allowed_link)
        return None

    monkeypatch.setattr(pipeline.mailbox, "search", search)
    applier = Applier(srv)
    now = time.time()

    async def go():
        await _serve(srv, pages, [])
        runs = []
        for i, paused in enumerate((now - 300, now)):
            job = srv.add_job(url=f"{verify}?job={i}", title=f"FSE {i}", company="Acme Fab")["job"]
            tab = await srv.browser.new_tab()
            await tab.goto(verify)
            r = Run(job["id"], f"FSE {i}", "Acme Fab", status="needs_you", need="email_code", page=tab, url=verify,
                    paused_at=paused, paused_host="careers.acme-fab.example")
            applier.runs[job["id"]] = r
            runs.append(r)
        for r in runs:
            await applier._check_mail(r)
        return runs

    earlier, later = run(go())
    assert asked[later.paused_at][:2] == (None, pipeline.SHARED_LOOK_BACK)  # a few seconds, not two minutes
    assert asked[earlier.paused_at][:2] == (later.paused_at, pipeline.mailbox.LOOK_BACK)
    own_link = asked[later.paused_at][2]
    assert own_link("https://careers.acme-fab.example/activate?t=1")
    assert own_link("https://jobs.acme-fab.example/confirm?t=1")  # the employer's own site
    for url in ("https://evil.example/myworkdayjobs.com/verify", "https://evil.example/greenhouse.io/confirm?t=1",
                "https://other.wd5.myworkdayjobs.com/activate/x", "javascript:alert(1)"):
        assert not own_link(url), url


@pytest.mark.parametrize("kind", ["code", "link"])
def test_a_code_put_in_for_one_job_is_never_put_in_for_another(srv, monkeypatch, kind):
    """onsemi's and Mayo Clinic's Oracle sites (run 49, Oct 13) mail their codes from the same Oracle address. Mayo's
    wait began a minute after onsemi's code came, was given that code too, and stopped on it. A code the inbox gave
    one job is never another's: the other waits for its own. A link is the account's, and two jobs on one site share
    it: each opens it (the review)."""
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    verify = "https://careers.acme-fab.example/verify"
    pages = {verify: '<html><body><h1>Confirm your email</h1><label for="code">Enter the verification code we sent to '
                     'your email</label><input id="code" type="text"><button type="button">Verify</button></body></html>',
             verify + "/confirmed": "<html><body><h1>Your email is confirmed</h1></body></html>"}
    now = time.time()
    first = "482913" if kind == "code" else verify + "/confirmed?t=1"
    inbox = [pipeline.mailbox.Found(kind, first, "oraclecloud.com", now - 60)]  # the first job's
    monkeypatch.setattr(pipeline.mailbox, "search", lambda *args, **kwargs: inbox[-1])
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, [])
        runs = []
        for i, paused in enumerate((now - 90, now)):
            job = srv.add_job(url=f"{verify}?job={i}", title=f"FSE {i}", company="Acme Fab")["job"]
            tab = await srv.browser.new_tab()
            await tab.goto(verify)
            r = Run(job["id"], f"FSE {i}", "Acme Fab", status="needs_you", need="email_code", page=tab, url=verify,
                    paused_at=paused, paused_host="careers.acme-fab.example")
            applier.runs[job["id"]] = r
            runs.append(r)
        first, second = runs
        await applier._check_mail(first)
        await applier._check_mail(second)
        given = (second.mail_done, second.mail_last, await second.page.input_value("#code"))
        inbox.append(pipeline.mailbox.Found("code", "551234", "careers.acme-fab.example", now + 5))  # its own
        await applier._check_mail(second)
        return first, second, given

    first, second, given = run(go())
    if kind == "link":
        assert given[0] and "opened the confirmation link from your email (sent from oraclecloud.com)" in second.log
        return
    assert "entered the code from your email (sent from oraclecloud.com)" in first.log, first.log
    assert not given[0] and "another job's" in given[1] and given[2] == "", given
    assert second.mail_done and "entered the code from your email (sent from careers.acme-fab.example)" in second.log
    assert run(second.page.input_value("#code")) == "551234"


@pytest.mark.parametrize("at_once", [1, 2])
def test_two_jobs_whose_codes_come_from_one_sender_each_get_their_own(srv, monkeypatch, at_once):
    """Two jobs whose sites email their codes from one address (two Oracle sites), filled one at a time or two at once:
    each code from the inbox goes into its own job's form, never the other's. Filled at once, the second job's code
    step waits until the first's code is in."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 600)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    monkeypatch.setattr(pipeline, "_another_employers_mail", lambda *a, **k: None)
    inbox: list = []
    sent: dict = {}
    real_pause = Applier._pause

    def pause(self, r, need, reason, questions=None, seen=None, *, code):
        if need == "email_code" and r.job_id not in sent:  # (the site emails its code as the step is pressed)
            sent[r.job_id] = time.time() - 1
            inbox.append(pipeline.mailbox.Found("code", "123456", "oraclecloud.com", sent[r.job_id]))
        return real_pause(self, r, need, reason, questions, seen, code=code)

    def search(address, password, since, senders, want, allowed_link, before=None, look_back=120, others=None):
        got = [m for m in inbox if m.received >= since - look_back and (before is None or m.received < before)]
        return max(got, key=lambda m: m.received) if got else None

    took: dict = {}
    real_take = pipeline._take_code

    def take_code(mail, job_id):
        took[job_id] = time.time()
        real_take(mail, job_id)

    monkeypatch.setattr(Applier, "_pause", pause)
    monkeypatch.setattr(pipeline.mailbox, "search", search)
    monkeypatch.setattr(pipeline, "_take_code", take_code)
    jobs = [srv.add_job(url=fixture_url("site/verify-email.html") + f"?code&job={n}", title="FSE",
                        company=company)["job"]["id"] for n, company in enumerate(("Example Fab", "Example Litho"))]
    applier = Applier(srv)
    applier.jobs_at_once = at_once

    async def go():
        applier.start()
        try:
            runs = [applier.enqueue(j) for j in jobs]
            for r in runs:
                await until(lambda r=r: r.job_id in took, timeout=60, about=state(r))  # (its code put in, and noted)
                assert "entered the code from your email (sent from oraclecloud.com)" in r.log, r.log
            return runs
        finally:
            await applier.stop()

    runs = run(go())
    for r in runs:
        assert pipeline._MAIL_TAKEN.get(("oraclecloud.com", "123456", sent[r.job_id])) == r.job_id, \
            (pipeline._MAIL_TAKEN, sent)
    first, second = sorted(runs, key=lambda r: sent[r.job_id])
    assert sent[second.job_id] + 1 >= took[first.job_id], (sent, took)  # (its code step once the first's code was in)


def test_a_job_that_never_paused_goes_on_only_at_its_own_addresses(srv):
    """Only from where a job paused on its employer's own site does it go on into a job system
    many employers share; not a job that never paused (any Workday page would count as its
    own), and never into another employer's careers site that runs a job system itself."""
    applier = Applier(srv)
    own = {"careers.acme-fab.example"}
    other_workday = "https://other.wd5.myworkdayjobs.com/en-US/External/job/Phoenix/FSE_R1/apply"
    never = Run(1, "FSE", "Acme Fab")
    assert not applier._own_place(never, other_workday, own=own)
    assert applier._own_place(never, "https://careers.acme-fab.example/apply/1", own=own)
    paused = Run(2, "FSE", "Acme Fab", paused_host="careers.acme-fab.example")
    assert applier._own_place(paused, "https://acme.wd1.myworkdayjobs.com/en-US/External/apply", own=own)
    assert not applier._own_place(paused, "https://careers.micron.com/careers/job/1", own=own)


def test_a_job_marked_applied_while_it_runs_isnt_paused_after(srv):
    job = srv.add_job(url="https://careers.acme-fab.example/apply/1", title="FSE", company="Acme Fab")["job"]
    applier = Applier(srv)
    r = Run(job["id"], "FSE", "Acme Fab", status="running")
    applier.runs[job["id"]] = r
    applier.mark_applied(job["id"])
    applier._pause(r, "questions", "Answer these", code="questions")
    assert (r.status, r.need) == ("submitted", "")


def test_a_press_the_tracker_remembers_isnt_made_again_without_its_record(srv):
    """The record in the job's folder couldn't be written (a full disk): the tracker's note of
    the press still keeps Submit for me from pressing again."""
    job = srv.add_job(url="https://careers.acme-fab.example/apply/1", title="FSE", company="Acme Fab")["job"]
    srv.tracker().update(job["id"], note="pressed Submit; no confirmation showed")
    applier = Applier(srv)
    applier.auto_submit = True
    r = applier.enqueue(job["id"], submit=True)
    assert r.pressed_before and not r.submit


def test_a_button_with_an_arrow_after_its_words_is_still_the_way_in():
    """APS's SuccessFactors posting (live, Oct 2026): "Apply now »" wasn't read as Apply, so the
    desk said it couldn't find the button."""
    def acts(*texts):
        return [{"id": str(i), "text": t} for i, t in enumerate(texts)]

    assert pick_next(acts("Search Jobs", "Create Alert", "Apply now »", "Apply now »"), False)["text"] == "Apply now »"
    assert pick_next(acts("Back", "Next ›"), True)["text"] == "Next ›"


def test_reject_non_essential_cookies_is_a_way_to_decline():
    """Aerotek's iCIMS banner (live, Oct 2026) offers "Reject Non-Essential Cookies"."""
    from job_apply.browser import declines_cookies

    assert declines_cookies("Reject Non-Essential Cookies", False)
    assert declines_cookies("Decline nonessential", False)
    assert not declines_cookies("Accept Non-Essential Cookies", True)


def test_a_banners_other_ways_to_decline_count_too():
    """Banners decline in more words than "Reject": "Deny", "Continue without accepting",
    "Essential cookies only". Looser ones ("No thanks") count only inside a cookie box."""
    from job_apply.browser import declines_cookies

    for words in ("Deny", "Deny all", "Refuse all", "Continue without accepting", "Essential cookies only",
                  "Accept essential cookies only", "Only required cookies", "Opt out", "No thanks",
                  "Reject optional cookies", "Do not accept"):
        assert declines_cookies(words, True), words
    for words, in_banner in (("No thanks", False), ("Deny", False), ("Accept all", True), ("Got it", True),
                             ("Manage preferences", True), ("Cookie settings", True)):
        assert not declines_cookies(words, in_banner), words


def test_a_country_the_site_preset_is_the_profiles_and_its_boxes_are_read_again(srv, monkeypatch):
    """Onto Innovation's Workday (the live check's run 118, Oct 16) opened My Information on Country = Mexico and its phone
    code on Mexico (+52): the desk left them as answered, asked the person Mexico's "Father's Family Name", and the
    profile's Arizona number was refused as not valid for that region. The profile's country goes in, and the page,
    whose boxes Workday relabels for it, is read and filled again: Last Name from the profile, no question left."""
    from job_apply import server

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    server._PRESETS_SEEN.clear()
    job = srv.add_job(url=fixture_url("site/workday-country-preset.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready", (r.need, r.reason, r.questions, r.log)
    assert r.url.endswith("review.html") and not any("Family" in str(q.get("label")) for q in r.questions), r.log
    # opened again in a new tab (its first closed unsaved), the site's preset is the site's again (the review's)
    server._PRESETS_SEEN[job["id"]] = {"country|"}
    server.forget_presets(job["id"])
    assert job["id"] not in server._PRESETS_SEEN


def test_a_form_still_being_drawn_is_waited_for(srv, monkeypatch):
    """Oracle's Personal Info step (Southwest Gas's, American Express's, live): upload boxes and a
    greyed-out Next first, the name and email boxes a moment later. The desk said it couldn't
    find the button; it waits for the form, fills it and goes on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/late-form.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready", (r.need, r.reason, r.log)
    assert "clicked “Next”" in r.log and r.url.endswith("review.html"), r.log


@pytest.mark.parametrize("player", ["beside", "youtube"])
def test_a_video_players_error_isnt_the_pages_while_the_email_page_is_drawn(srv, monkeypatch, player):
    """Oracle's job page (issue #112, note 33) has a YouTube player in its media slider, which said "This
    video is private ... Sign in." Apply Now draws the email page a moment after the click, and the desk
    took the player's text for the page's error and stopped with "The page didn't move on". The player's
    frame isn't read (youtube), and nor are the errors of a frame with nothing to fill (beside); the desk
    fills the email, ticks the terms box and presses Next."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    video = (Path(__file__).parent / "fixtures" / "site" / "video-frame.html").read_text(encoding="utf-8")
    job = srv.add_job(url=fixture_url("site/oracle-video-posting.html") + ("?youtube" if player == "youtube" else ""),
                      title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        await srv.browser.page()
        await srv.browser._ctx.route("https://www.youtube.com/**", lambda route: route.fulfill(
            status=200, content_type="text/html", body=video))
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert "didn't move on" not in log and "didn't move on" not in r.reason, (r.reason, r.log)
    assert any(line.startswith("filled 1 field(s) on") for line in r.log), r.log  # (its email, on its start page)
    assert "ticked “I agree with the terms and conditions” for you (settings.accept_notices)" in r.log, r.log
    assert "clicked “Next”" in r.log, r.log


@pytest.mark.parametrize("settings", [{"accept_notices": False}, {}], ids=["off", "on"])
def test_an_agreement_is_left_to_the_person_and_a_captchas_buttons_arent_the_pages(srv, monkeypatch, job_apply_home,
                                                                                     settings):
    """Schwab's iCIMS sign-in (live, Oct 2026): the way on is "I Acknowledge the Privacy Notice",
    and a hidden hCaptcha frame's "Verify" and "Refresh Challenge." were read as the page's own
    buttons. The CAPTCHA's frame isn't read. Without settings.accept_notices the desk names the
    button that agrees to something rather than pressing it or saying it couldn't find one; with
    it (the owner's call, Oct 10) it acknowledges the notice for the person, once: a page that
    stays after that is theirs."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    notice_settings(job_apply_home, **settings)
    job = srv.add_job(url=fixture_url("site/privacy-signin.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        await srv.browser.page()
        await srv.browser._ctx.route("https://newassets.hcaptcha.com/**", lambda route: route.fulfill(
            status=200, content_type="text/html",
            body="<html><body><button>Verify</button><button>Refresh Challenge.</button></body></html>"))
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you", about=state(r))
            return r, await r.page.evaluate("() => window.acknowledged || false")
        finally:
            await applier.stop()

    r, acknowledged = run(go())
    assert r.need == "stuck" and "“I Acknowledge the Privacy Notice”" in r.reason, (r.reason, r.log)
    assert acknowledged is not bool(settings)
    assert ("agreed to Example Fab's “I Acknowledge the Privacy Notice” for you (settings.accept_notices)"
            in r.log) is not bool(settings), r.log
    assert not {"Verify", "Refresh Challenge."} & set(r.page_info.get("actions") or []), r.page_info


def test_an_apply_button_with_an_arrow_isnt_read_as_an_emailed_link():
    """A posting that mentions checking your email, with "Apply now »": the way in, not a
    "we emailed you a link" page."""
    data = {"fields": [], "actions": [{"id": "a", "text": "Apply now »"}], "headings": ["Recruiter"]}
    assert classify(data, "After you apply, check your email for a link to verify your account.") == "page"


def test_arrow_buttons_on_a_posting_and_a_step(srv, monkeypatch):
    """A posting with a job-alerts box goes in through its "Apply now »", without filling the
    alerts box; on step 1, "Next ›" beside a Submit is the way on, not a sign it's the review page."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    site = "https://careers.acme-fab.example"
    posting = (f'<html><head><meta charset="utf-8"><title>Field Service Engineer - Acme Fab</title></head><body><h1>Field Service '
               f'Engineer</h1><p>Install and service equipment in Chandler.</p><a href="{site}/apply/step1">Apply now »'
               '</a><footer><p>Get job alerts</p><form id="alerts" method="post" action="/job-alerts"><label for="ae">'
               'Email</label><input id="ae" name="ae" type="email"><button type="submit">Subscribe</button></form>'
               '</footer></body></html>')
    step1 = ('<html><head><meta charset="utf-8"><title>Acme Fab application</title></head><body><div>current step 1 of 2</div>'
             '<h2>My Information</h2><form method="post" action="/review"><label for="fn">First Name *</label>'
             '<input id="fn" name="fn" required><label for="ln">Last Name *</label><input id="ln" name="ln" required>'
             '<button type="submit">Next ›</button><button type="button">Submit Application</button></form></body></html>')
    review = ('<html><body><div>current step 2 of 2</div><h2>Review</h2><form method="post" action="/posted">'
              '<button type="submit">Submit Application</button></form></body></html>')
    pages = {f"{site}/jobs/1": posting, f"{site}/apply/step1": step1, ("POST", f"{site}/review"): review}
    posts: list[str] = []
    job = srv.add_job(url=f"{site}/jobs/1", title="FSE", company="Acme Fab")["job"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, posts)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.log[1] == "clicked “Apply now »”", r.log  # nothing filled on the posting first
    assert "clicked “Next ›”" in r.log and posts == [f"{site}/review"], (posts, r.log)
    assert r.status == "ready", (r.reason, r.log)


def test_apply_online_and_a_page_with_only_a_captcha():
    """Kforce's Taleo postings go in through "Apply Online". A page with nothing but a CAPTCHA's
    box (its frame isn't read) and a button is a bot check, not a page to press on from."""
    acts = [{"id": "1", "text": "Apply Online"}, {"id": "2", "text": "Add to My Job Cart"}]
    assert pick_next(acts, False)["text"] == "Apply Online"
    gate = {"title": "Verify", "fields": [], "actions": [{"id": "1", "text": "Continue"}], "captcha": "A CAPTCHA..."}
    assert classify(gate, "Please confirm to continue.") == "bot_check"
    assert classify({**gate, "fields": [{"id": "f", "kind": "text", "label": "Email"}]}, "") == "form"


@pytest.mark.parametrize("page, said", [("forbidden.html", "403 Forbidden"), ("not-acceptable.html", "406 Not Acceptable"),
                                        ("request-blocked.html", "Your request has been blocked."),
                                        ("icims-not-acceptable.html", "Request Not Acceptable")])
def test_a_site_that_turns_the_browser_away_holds_nothing_up(srv, monkeypatch, page, said):
    """Valleywise Health's postings answer the desk's browser with a bare "403 Forbidden", and
    Deloitte's sign-in a headless one with "406 Not Acceptable", and Banner Health's Workday the live
    check's with a heading alone, "Your request has been blocked." (live, Oct 2026; the desk said it
    couldn't find the button). So did iCIMS's edge at four employers at once (run 74): its error page,
    "Oops, something went wrong", with "Request Not Acceptable" under its technical details and a Language
    picker. There's nothing to solve: the person applies in their own browser, and the queue isn't held
    for it as for a bot check."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/" + page), title="Analyst", company="Example Health")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "stuck" and not r.blocking, (r.need, r.reason)
    assert f"turned the desk's browser away ({said})" in r.reason


@pytest.mark.parametrize("page, said, reloaded", [("bad-gateway.html", "502 Bad Gateway", True),
                                                  ("ukg-posting-502.html", "502 Bad Gateway", False),
                                                  ("service-unavailable-retry.html", "503 Service Unavailable", True)])
def test_a_web_servers_own_error_page_is_said_as_the_sites_trouble(srv, monkeypatch, page, said, reloaded):
    """UKG Pro's new sign-in (Nikon Precision, UCT and Desert Financial, run 106) answered the desk's Apply now with
    nginx's bare "502 Bad Gateway", and the desk said it couldn't find the button that moves the application on. A
    page that is a web server's error page and nothing else (its title or first heading a 5xx's words, no box, nothing
    to press; a "Try again" button is no way on) is the site's own trouble, said in its words. A posting's own address
    that answers so is reloaded once first, and said with "even after I reloaded it"; a page a press led to is said at
    once, never reloaded (a reload may send its form again): the person reloads it later and presses Resume."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/" + page), title="Field Service Engineer", company="Example Precision")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "stuck" and not r.blocking, (r.need, r.reason)
    assert f"web server's error page ({said})" in r.reason and "site's own trouble" in r.reason, r.reason
    assert "couldn't find the button" not in r.reason, r.reason
    reloads = [line for line in r.log if "so I reloaded it" in line]
    if reloaded:
        assert len(reloads) == 1 and f"error page ({said})" in reloads[0], r.log
        assert "even after I reloaded it" in r.reason, r.reason
    else:
        assert not reloads and "even after" not in r.reason, (r.reason, r.log)
        assert any("clicked \u201cApply now\u201d" in line for line in r.log), r.log


def test_a_frames_5xx_heading_beside_a_postings_apply_is_never_the_sites_error_page(srv, monkeypatch):
    """The review of 0.3.184: a posting with no heading of its own, a frame from another site answering with a 502
    page (its heading the page's first) and its "Apply Now": applied to as before."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/posting-502-frame.html"), title="Field Service Engineer",
                      company="Example Precision")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.seen_form and "web server's error page" not in r.reason, (r.status, r.reason, r.log)
    assert not any("reloaded" in line for line in r.log), r.log


def test_a_web_servers_error_page_that_answered_submit_is_never_to_be_reloaded(srv, monkeypatch):
    """The review of 0.3.184: a Submit whose answer is the gateway's "502 Bad Gateway" leaves no confirmation (the desk
    asks the person to check), and on their Resume the page is still that error page. Reloading a Submit's answer
    sends the application again: the person is told to check their email or the site instead."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/submit-gateway.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        tab = await srv.browser.new_tab()
        await tab.goto(fixture_url("site/submit-gateway.html"))
        r = pipeline.Run(job["id"], "FSE", "Example Fab", status="queued", seen_form=True, page=tab)
        applier.runs[job["id"]] = r
        await applier._strict(applier._submit(r))
        assert (r.status, r.need) == ("needs_you", "check_submit") and r.pressed_before, (r.status, r.reason)
        applier.start()
        try:
            r = applier.resume(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "stuck" and "web server's error page (502 Bad Gateway) after Submit was pressed" in r.reason, r.reason
    assert "Don't reload it" in r.reason and "reload the page in the desk's browser window" not in r.reason, r.reason
    assert not any("reloaded" in line for line in r.log), r.log


def test_a_web_servers_error_words_are_read_as_a_whole_title_or_heading():
    """nginx's, IIS's, Tomcat's, Apache's and Varnish's 5xx pages; never a posting's or a form's own words, nor a
    number inside other words. A bare number ("Error 503") only as a page's title."""
    for words in ("502 Bad Gateway", "Bad Gateway", "503 Service Temporarily Unavailable", "504 Gateway Time-out",
                  "500 - Internal server error.", "HTTP Status 500 \u2013 Internal Server Error", "Proxy Error",
                  "503 Backend fetch failed", " 500 Internal Server Error "):
        assert pipeline._SERVER_ERROR.search(words), words
    for words in ("A server error occurred while saving your application", "Error 50", "Job 503", "Not Acceptable",
                  "example.com | 502: Bad gateway", "Gateway Services Technician", "Bad Gateway Road"):
        assert not pipeline._SERVER_ERROR.search(words), words
    for words in ("503", "Error 503", "HTTP Error 502", "HTTP Status 500"):
        assert pipeline._SERVER_ERROR_CODE.search(words), words
    for words in ("Job 503", "503 jobs", "Requisition 50301", "Error 50"):
        assert not pipeline._SERVER_ERROR_CODE.search(words), words


def test_a_postings_own_words_or_a_frames_403_never_read_as_the_site_turning_the_browser_away(srv, monkeypatch):
    """The review of 0.3.160: only iCIMS's error page has its "Request Not Acceptable" in a later heading. A posting with
    its "Apply Now", whose own words have a "Not Acceptable" heading (a dress code) and whose frame from another site
    shows a 403 page, is applied to as before."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/posting-not-acceptable-words.html"), title="Store Associate",
                      company="Example Store")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.seen_form and "turned the desk's browser away" not in r.reason, (r.status, r.reason, r.log)


def test_a_page_that_doesnt_move_on_says_its_next_is_greyed_out(srv, monkeypatch):
    """Phoenix Children's Quick Apply: the desk pressed the posting's "Apply!" (a link to the form,
    already on show) three times and said the page didn't move on; its Next is greyed out until
    a resume is attached, and the desk now says so."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/greyed-next.html"), title="TA Coordinator", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "stuck" and "“Next” is greyed out" in r.reason, (r.reason, r.log)
    assert not any("Apply!" in line for line in r.log), r.log  # a link to the form already on show: not pressed


def test_the_page_a_job_stops_on_is_saved_for_its_report_scrubbed(srv, monkeypatch, job_apply_home):
    """A job stuck on a Workday form page made a report with no pages ("pages": []): only a failed
    fill saved one. The page a job stops on is now saved in its folder's debug/, and its report
    holds it, without the person's details or what was filled in (a React site writes that into
    its HTML)."""
    from job_apply import report

    monkeypatch.setattr(pipeline, "STEP_LOAD_WAIT", 2)  # (a form with no button is waited on that long)

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/stuck-filled.html"), title="Technician", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            # (the step ends once the page it stopped on is saved: then the tools are given back)
            await until(lambda: r.status not in ("queued", "running") and applier.current is None, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "stuck" and "“Next” is greyed out" in r.reason, (r.reason, r.log)
    out = report.build(srv.tracker().get(job["id"]), r)
    assert out["pages"], "the report holds no page"
    [stop] = (Path(job["folder"]) / "debug").glob("*-stop")
    assert 'value="sam.rivera@example.com"' in (stop / "page.html").read_text(encoding="utf-8")  # what the site wrote in
    assert json.loads((stop / "snapshot.json").read_text(encoding="utf-8"))["note"] == "stopped: stuck"
    assert out["pages"] == [f"pages/{stop.name}.expect.json", f"pages/{stop.name}.html"]
    page = (Path(out["folder"]) / out["pages"][1]).read_text(encoding="utf-8")
    assert "My Information" in page and f"### Page saved {stop.name}" in out["preview"]
    for private in ("Sam", "Rivera", "sam.rivera", "value="):
        assert private not in page, private


def test_the_newest_few_stops_are_kept_and_one_that_cant_be_saved_still_waits(srv, monkeypatch, job_apply_home):
    """Each stop saves its page: only as many as a report holds are kept. A tailored resume is
    waited for before the job's tab opens, so there's no page to save; and whatever goes wrong
    saving one, the job still waits on the person."""
    job = srv.add_job(url=fixture_url("site/stuck-filled.html"), title="Technician", company="Example Fab")["job"]
    run(srv.open_application(job_id=job["id"]))
    applier = Applier(srv)
    r = Run(job["id"], "Technician", "Example Fab", status="running", page=srv.browser.current_tab)
    applier.runs[job["id"]] = r
    debug = Path(job["folder"]) / "debug"

    def stops():
        return sorted(p.name for p in debug.glob(f"*-{pipeline.STOP}")) if debug.is_dir() else []

    applier._pause(r, "tailor", "Waiting for a resume written for this job.", code="tailor")
    run(applier._save_stop(r))
    assert stops() == []
    for _ in range(4):
        applier._pause(r, "stuck", "The page didn't move on.", code="step_not_saved")
        run(applier._save_stop(r))
    assert len(stops()) == pipeline.report.PAGES == 3
    newest = stops()

    async def broken(*args, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(srv.browser, "snapshot", broken)
    applier._pause(r, "sign_in", "Sign in.", code="account_theirs")
    run(applier._save_stop(r))
    assert (r.status, r.need) == ("needs_you", "sign_in") and stops() == newest


def test_a_page_whose_only_way_on_makes_an_account_is_left_to_the_person(srv, monkeypatch):
    """amazon.jobs, after an email it doesn't know (live, Oct 2026), offers only "Proceed to create
    account". The desk said it couldn't find the button; the account is the person's to make."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/account-step.html"), title="HRBP", company="Example Jobs")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you", about=state(r))
            return r, await r.page.evaluate("() => window.creating || false")
        finally:
            await applier.stop()

    r, creating = run(go())
    assert r.need == "sign_in" and "wants an account for this email" in r.reason, (r.need, r.reason)
    assert not creating  # never pressed
    posting = {"title": "HR Business Partner", "headings": ["HR Business Partner"], "fields": [],
               "actions": [{"id": "1", "text": "Register"}, {"id": "2", "text": "Apply now ▾"}]}
    assert not pipeline._account_step(posting)  # a header's "Register" on a posting: not the way on
    assert not pipeline._account_step({**posting, "title": "Sign up", "actions": [{"id": "1", "text": "Sign up for job alerts"}]})


def test_an_account_pause_holds_until_the_tab_leaves_the_account_site(srv):
    """amazon.jobs' account steps after "Proceed to create account" (name, an emailed code) are
    forms with no password box: read as past the pause, the desk would fill them and press on,
    making the account the desk never makes. The pause holds until the tab leaves that site."""
    applier = Applier(srv)
    job = srv.add_job(url="https://www.jobs.example/jobs/1", title="HRBP", company="Example")["job"]
    r = Run(job_id=job["id"], need="sign_in", url="https://passport.jobs.example/unknownEmail")
    r.paused_host = r.hold_host = "passport.jobs.example"
    signup = {"url": "https://passport.jobs.example/signup", "title": "Create your account", "headings": ["Your name"],
              "fields": [{"id": "n", "kind": "text", "label": "Full name"}], "actions": [{"id": "c", "text": "Continue"}]}
    assert not applier._looks_past(r, signup, "")
    back = {**signup, "url": "https://www.jobs.example/jobs/1/apply", "title": "Apply", "headings": ["Apply"]}
    assert applier._looks_past(r, back, "")


@pytest.mark.parametrize("auto", [False, True])
def test_a_review_page_that_lists_errors_says_so_and_isnt_submitted_for_you(srv, monkeypatch, auto):
    """Insight Enterprises' review page (live, Oct 2026) listed "Error: Last Name cannot be left
    blank." and the desk called the job ready as if nothing were wrong; with Submit for me on, it
    would have pressed Submit on it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/review-errors.html"), title="HRBP", company="Example Fab")["job"]
    applier = Applier(srv)
    applier.auto_submit = auto

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"], submit=auto)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => window.sent || false")
        finally:
            await applier.stop()

    r, sent = run(go())
    assert "Last Name cannot be left blank" in r.reason and "error found" not in r.reason, (r.status, r.reason)
    assert not sent
    if auto:
        assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason)
    else:
        assert r.status == "ready", (r.status, r.reason)


def test_a_review_page_with_errors_after_an_earlier_press_says_both(srv, monkeypatch):
    """Submit pressed for this job before, no confirmation, and the review page now lists an
    error: the warning that it may have gone through doesn't hide what the page shows."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/review-errors.html"), title="HRBP", company="Example Fab")["job"]
    srv.tracker().update(job["id"], note="pressed Submit; no confirmation showed")
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and "pressed for this job before" in r.reason, (r.status, r.reason)
    assert "Last Name cannot be left blank" in r.reason, r.reason


def test_a_page_that_asks_if_youre_a_robot_is_a_bot_check(srv, monkeypatch):
    """Randstad's application (live, Oct 2026): Continue stayed put with "Please verify that you
    are not a robot", and the desk called it stuck. It's a bot check: the person's to pass."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/robot-step.html"), title="Recruiter", company="Example Staffing")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "bot_check"), (r.status, r.need, r.reason, r.log)


def test_an_application_that_ends_on_an_unreachable_host_says_so(srv, monkeypatch):
    """Edward Jones' home-office postings (live, Oct 2026): the Apply link's page went on through a
    staff sign-in to a host the public can't reach, and the desk said it couldn't find the button
    on Chrome's error page. It says the page didn't load, and where it went."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/sso-unreachable.html"), title="Analyst", company="Example Investments")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck") and "couldn't be reached" in r.reason, (r.reason, r.log)
    assert "find the button" not in r.reason, r.reason


def test_a_name_and_email_box_before_the_application_is_the_persons_to_send(srv, monkeypatch):
    """The State of Arizona's postings (live, Oct 2026) have an "Apply Now" box taking a name and
    email, whose APPLY sends them on before the application itself (on PageUp). The desk filled it
    and called it the review page. It isn't the application, and its button sends the person's
    details: the person presses it, and nothing is called ready."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/apply-now-sign-up.html"), title="Accounting Supervisor",
                      company="State of Example")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert "name and email" in r.reason and "\u201cAPPLY\u201d" in r.reason, r.reason
    assert not any("review page" in line for line in r.log), r.log


def test_a_name_and_email_box_sent_by_a_plain_button_is_the_persons_too(srv, monkeypatch):
    """The same box with an APPLY that isn't the form's submit (a page script sends it): the
    desk took it for the way in and pressed it, sending the name and email."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/apply-now-sign-up-button.html"), title="Accounting Supervisor",
                      company="State of Example")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => window.sent || 0")
        finally:
            await applier.stop()

    r, sent = run(go())
    assert (r.status, r.need, sent) == ("needs_you", "stuck", 0), (r.status, r.reason, r.log)
    assert "name and email" in r.reason and "\u201cAPPLY\u201d" in r.reason, r.reason


def test_a_jobs_missing_dates_are_named_not_asked_box_by_box(srv, monkeypatch, job_apply_home):
    """A Workday "My Experience" page (live, Oct 2026) with a profile whose jobs have no dates:
    the desk listed 21 bare "From" and "To" boxes, each with "Remember", and an answer typed
    there couldn't go in (those boxes come from the profile alone). It names the jobs whose
    months are missing and how to add them, with Resume; the other questions stay asked."""
    import yaml

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in profile["work_history"]:
        entry.pop("start"), entry.pop("end")
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    job = srv.add_job(url=fixture_url("site/workday-my-experience.html"), title="Equipment Technician",
                      company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert "Equipment Technician at Intel (From, To)" in r.reason, r.reason
    assert "Maintenance Technician at Example Fab Services (From, To)" in r.reason, r.reason
    assert "add the start and end months of my jobs from my resume" in r.reason, r.reason
    assert not [q for q in r.questions if q.get("section", "").startswith("Work Experience")], r.questions


@pytest.mark.slow
@pytest.mark.parametrize("case", ["once", "never", "resumed"])
def test_a_degree_list_with_no_choices_at_all_is_reloaded_once_then_said_to_have_not_loaded(srv, monkeypatch,
                                                                                            job_apply_home, case):
    """Grand Canyon University's Workday (run 61; runs 57 to 60 went to its review page): its Degree list opened on
    "Select One" alone, and the card said the list had no choice for the person's Bachelor of Science, to pick "the
    closest honest one". A list with no choice at all didn't finish loading: the page is reloaded once (and filled
    again), and a list still empty is said to be one the page didn't load. (One case a run, so the three go side by
    side under -n auto, the review of Oct 15.)"""
    import yaml

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in profile["work_history"]:
        entry.pop("start"), entry.pop("end")  # (so the page stops on them, after the Degree list)
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    applier = Applier(srv)

    async def go(query: str, hands_on: bool = False):
        job = srv.add_job(url=fixture_url("site/workday-my-experience.html") + query, title="Equipment Technician",
                          company="Example Litho")["job"]
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            r.hands_on = hands_on  # (as after the person's Resume on it)
            await until(lambda: r.status not in ("queued", "running"), timeout=180, about=state(r))  # (two passes: slow on CI's Windows)
            return r, await r.page.evaluate("() => document.getElementById('dg-1').textContent")
        finally:
            await applier.stop()

    if case == "once":
        r, degree = run(go("?nodegrees=once"))  # (loaded once the page is reloaded)
        assert any("showed no choices at all, so I reloaded the page" in line for line in r.log), r.log
        assert degree != "Select One" and "Degree" not in r.reason, (degree, r.reason)
        assert (r.status, r.need) == ("needs_you", "stuck") and "(From, To)" in r.reason, r.reason  # (the work dates)
    elif case == "never":
        r, degree = run(go("?nodegrees"))  # (never loaded)
        assert "list for " in r.reason and "(Degree) showed no choices at all, only its placeholder" in r.reason, r.reason
        assert "even after I reloaded it" in r.reason and "has no choice for your" not in r.reason, r.reason
        assert degree == "Select One" and sum("so I reloaded the page" in line for line in r.log) == 1, r.log  # (once)
    else:
        r, _ = run(go("?nodegrees=once&resumed", hands_on=True))  # (after the person's Resume: never reloaded, what they did stays)
        assert not any("reloaded" in line for line in r.log) and "(Degree) showed no choices at all" in r.reason, r.log
        assert "even after I reloaded it" not in r.reason, r.reason


def test_a_degree_list_with_no_choices_at_all_isnt_one_without_a_choice_for_classes(srv):
    """The review of 0.3.149: a Degree list read with its placeholder alone, for a school the profile says no degree
    was finished at, was taken for a list with no choice for classes without a degree (and its block was deleted). A
    list with nothing at all has nothing for anything: it's said to be one the page didn't load."""
    import yaml

    from job_apply.autofill import no_choice_for_no_degree

    path = config.profile_path()
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    school = profile["education_history"][0]["school"]
    profile["education_history"][0]["degree"] = "Some college (no degree)"
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    degree = {"id": "a", "label": "Degree*", "section": "Education 1", "required": True, "kind": "listbox",
              "options": ["Select One"]}
    assert not no_choice_for_no_degree(degree, config.Profile.load())
    said, rest, code = Applier(srv)._entry_gaps(Run(1, "Technician", "Example Litho"),
                                                {"url": "https://example.wd1.myworkdayjobs.com/x"}, [degree])
    assert f"list for {school} in Education 1 (Degree) showed no choices at all" in said, said
    assert code == "page_not_loaded"  # (its stop's code: T1)
    assert "classes without a degree" not in said and rest == [], said
    # (a school's own list, filled once its Country or State is chosen, is no Degree list that didn't load; nor is a
    # search prompt, whose choices come as it's typed in)
    school_box = {"id": "b", "label": "School*", "section": "Education 1", "required": True, "kind": "select",
                  "options": ["Select One"]}
    said, _, _ = Applier(srv)._entry_gaps(Run(1, "Technician", "Example Litho"),
                                          {"url": "https://example.wd1.myworkdayjobs.com/x"}, [school_box])
    assert "showed no choices" not in said, said
    prompt = {**degree, "kind": "combobox"}
    said, _, _ = Applier(srv)._entry_gaps(Run(1, "Technician", "Example Litho"), {
        "url": "https://example.wd1.myworkdayjobs.com/x", "fields": [{**prompt, "search": True}]}, [prompt])
    assert "showed no choices" not in said and "no choice for your" not in said, said


def test_a_resume_workday_already_lists_isnt_uploaded_again(srv, monkeypatch, job_apply_home):
    """Workday's resume box empties after each upload and lists the file below it: on Resume the
    desk filled the page again and uploaded the resume a second time (live, Oct 2026). A file the
    box lists as uploaded is its value, so it goes up once."""
    import yaml

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in profile["work_history"]:
        entry.pop("start"), entry.pop("end")  # so the page stops, and Resume fills it again
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    job = srv.add_job(url=fixture_url("site/workday-my-experience.html"), title="Equipment Technician",
                      company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            first = r.paused_at
            applier.enqueue(job["id"])  # Resume
            await until(lambda: r.paused_at > first and r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate(
                "() => document.querySelectorAll('[data-automation-id=\"file-upload-item\"]').length")
        finally:
            await applier.stop()

    r, uploads = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck") and uploads == 1, (uploads, r.reason, r.log)


@pytest.mark.slow
def test_a_section_left_short_by_a_covered_add_button_stops_the_job_until_the_person_says_go_on(srv, monkeypatch):
    """Workday's My Experience (issue #112, note 32): "added 1 work block(s)", then the Add button "covered", and the
    log only said so: 2 of 5 jobs reached the form and the card spoke of a Degree box alone; on another site the
    education Add was covered and the job went on to its review page, and was submitted, with education missing. The
    job stops on the page with what is short said first, and the Next isn't pressed. A Resume on it with nothing
    added goes on (it would only stop again), and says so."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 1)
    monkeypatch.setattr(pipeline, "NEW_TAB_WAIT", 1)
    monkeypatch.setattr(browser_module, "ADD_PRESS_TIMEOUT", 500, raising=False)  # (short: the cover stays)
    monkeypatch.setattr(browser_module, "ADD_COVER_WAIT", 100, raising=False)
    job = srv.add_job(url=fixture_url("site/workday-my-experience.html") + "?covered", title="Equipment Technician",
                      company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            first = (r.status, r.need, r.reason, list(r.log), r.paused_at)
            applier.resume(job["id"])
            await until(lambda: r.paused_at > first[4] and r.status not in ("queued", "running"), about=state(r))
            return first, r
        finally:
            await applier.stop()

    first, r = run(go())
    status, need, reason, log, _ = first
    assert (status, need) == ("needs_you", "stuck"), (status, reason, log)
    assert reason.startswith("Example Litho's site shows only 0 of your 1 school: the Add button is covered"), reason
    assert "then press Carry on" in reason, reason
    assert not any(line.startswith("clicked") or "review page" in line for line in log), log  # (stopped on the page)
    assert any(line == "added 1 work block(s)" for line in log), log
    # the Resume with nothing added: gone on with, said, and the page pressed on (this fixture's Next goes nowhere)
    assert any(line.startswith("went on with 0 of your 1 school") for line in r.log), r.log
    assert any(line.startswith("clicked") for line in r.log), r.log
    assert "Add button is covered" not in r.reason, r.reason


def test_a_review_page_reached_with_a_section_left_short_is_never_submitted_for_the_person(srv, monkeypatch):
    """"Submit for me" on a job whose school (or job) blocks were left short on a covered Add button (issue #112,
    note 32: it went on to its review page and was submitted with education missing): the review page waits for the
    person, with what's short said on its card."""
    job = srv.add_job(url=fixture_url("site/review.html"), title="Equipment Technician", company="Example Litho")["job"]
    applier = Applier(srv)
    applier.auto_submit = True
    sent = []

    async def submit(r, by_person=True):
        sent.append(r.job_id)

    monkeypatch.setattr(applier, "_submit", submit)
    data = {"url": "https://example.wd1.myworkdayjobs.com/x/review", "headings": ["Review"], "fields": [], "actions": [],
            "errors": []}
    r = Run(job["id"], "Equipment Technician", "Example Litho", submit=True)
    r.entries_short = {"work": (3, 5), "education": (0, 1)}
    run(applier._finish(r, data, ""))
    assert (r.status, sent) == ("ready", []), (r.status, r.reason, sent)
    assert "Note: Workday shows only 3 of your 5 jobs and 0 of your 1 school" in r.reason, r.reason
    assert "before you press Submit" in r.reason, r.reason
    r = Run(job["id"], "Equipment Technician", "Example Litho", submit=True)  # (nothing short: sent as asked)
    run(applier._finish(r, data, ""))
    assert sent == [job["id"]], sent


def test_a_school_the_sites_list_refuses_is_not_called_missing_from_the_profile(srv):
    """Where even "Other" isn't in a site's school list, the profile isn't missing the school:
    the site's list won't take it, which is said as that."""
    applier = Applier(srv)
    r = Run(1, "Technician", "Example Litho")
    applier.runs[1] = r
    pending = [{"id": "a", "label": "School or University*", "section": "Education 1", "required": True,
                "error": "nothing in its list matched"},
               {"id": "b", "label": "From", "sublabel": "Month", "section": "Work Experience 3", "required": True},
               {"id": "c", "label": "Are you 18 or older?", "required": True, "kind": "text"}]
    said, rest, code = applier._entry_gaps(r, {"url": "https://example.wd1.myworkdayjobs.com/x"}, pending)
    assert "didn't take your profile's answer for: Arizona State University in Education 1 (School or University)" \
        in said
    assert "more blocks than your profile has entries: Work Experience 3 (your profile has no job 3) (From)" in said
    assert "your profile doesn't have" not in said, said
    assert [q["id"] for q in rest] == ["c"]
    assert code == "box_refused_answer"  # (its stop's code: T1)


def test_what_a_jobs_or_schools_block_lacks_is_said_for_its_own_list(srv):
    """A school's missing year is education_history's, not work_history's; a block's "Reason
    for leaving" or "Day" box isn't the profile's to hold, so it stays a question."""
    import yaml

    path = config.profile_path()
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile["education_history"][0].pop("start")
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    applier = Applier(srv)
    r = Run(1, "Technician", "Example Litho")
    pending = [{"id": "a", "label": "From", "sublabel": "Year", "section": "Education 1", "required": True},
               {"id": "b", "label": "Reason for Leaving", "section": "Work Experience 1", "required": True},
               {"id": "c", "label": "From", "sublabel": "Day", "section": "Work Experience 2", "required": True}]
    said, rest, _ = applier._entry_gaps(r, {"url": "https://example.wd1.myworkdayjobs.com/x"}, pending)
    assert "your schools, and your profile doesn't have this for them: Arizona State University in Education 1 (From)" \
        in said
    assert "education_history" in said and "work_history" not in said, said
    assert [q["id"] for q in rest] == ["b", "c"]


def test_a_schools_missing_degree_is_asked_for_as_a_degree_never_as_dates(srv):
    """A Workday page asked for the Degree of a school the profile lists without one (classes,
    no degree): the desk said to add years and to ask Claude to add it from the resume. A Degree
    box is said as a degree, with the way to write a school not finished; never dates."""
    import yaml

    path = config.profile_path()
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile["education_history"][0].pop("degree", None)
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    applier = Applier(srv)
    r = Run(1, "Technician", "Example Litho")
    pending = [{"id": "a", "label": "Degree*", "section": "Education 1", "required": True, "kind": "listbox"}]
    said, rest, _ = applier._entry_gaps(r, {"url": "https://example.wd1.myworkdayjobs.com/x"}, pending)
    assert "Arizona State University in Education 1 (Degree)" in said, said
    assert "Some college (no degree)" in said and "never a degree you didn't finish" in said, said
    assert "years as start" not in said, said
    assert rest == []


def test_a_sign_in_that_takes_a_moment_is_waited_for_not_taken_for_a_refused_password(srv, monkeypatch, job_apply_home):
    """Intel's and TEL's Workday (nightly live check, Oct 11, the test identity's second visit): the saved password
    was the account's, but the site took a few seconds to sign in, its form still on show. The desk took that for a
    refused password, opened the password reset, found the application instead and stopped ("opened its password
    reset, which isn't one I can fill in"). A sign-in whose form is still there is waited for a moment first."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/signin-forgot.html") + "?late=3000", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=90, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert r.seen_form and "signin" not in r.url, (r.url, r.reason, r.log)
    assert "password reset" not in log and "didn't sign in" not in log and "Create Account" not in log, log


WORKDAY_DEGREES = ["High School", "GED", "Associates", "Bachelors", "Masters", "Doctorate", "PH.D"]


def test_a_degree_list_without_the_profiles_degree_says_so_not_that_the_profile_lacks_it(srv):
    """Live test 2 (issue #112, note 16): a Degree list of Certificate, Foundation, Associate, Bachelor,
    Master, Doctor, Ph.D., Others, Some College and No degree has no high school or GED choice. The card
    said "your profile doesn't have this for them", though the profile has degree: GED for that school.
    It says the list has no choice for the GED, and that the pick (an honest one, such as "Others") is the
    person's."""
    import yaml

    path = config.profile_path()
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    school = profile["education_history"][0]["school"]
    profile["education_history"][0]["degree"] = "GED"
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    applier = Applier(srv)
    degree = {"id": "a", "label": "Degree*", "section": "Education 1", "required": True, "kind": "listbox",
              "options": ["Certificate", "Foundation", "Associate", "Bachelor", "Master", "Doctor", "Ph.D.", "Others",
                          "Some College", "No degree (Dropped out)"]}
    said, rest, _ = applier._entry_gaps(Run(1, "Technician", "Example Litho"),
                                        {"url": "https://example.wd1.myworkdayjobs.com/x"}, [degree])
    assert f"The Degree list has no choice for your GED ({school} in Education 1)" in said, said
    assert "doesn't have this for them" not in said and "Others" in said, said
    assert rest == []


def test_a_degree_list_in_the_bare_singular_has_the_profiles_degree(srv):
    """The review of 0.3.175: "Bachelor" alone (the list of issue #112, note 16) was no degree level, so with
    0.3.175's "a level the list lacks is never another" the desk would have said the list has no choice for the
    profile's bachelor's and stopped. The level in the bare singular is the level."""
    applier = Applier(srv)
    degree = {"id": "a", "label": "Degree*", "section": "Education 1", "required": True, "kind": "listbox",
              "options": ["Certificate", "Foundation", "Associate", "Bachelor", "Master", "Doctor", "Ph.D.", "Others",
                          "Some College", "No degree (Dropped out)"]}
    said, rest, _ = applier._entry_gaps(Run(1, "Technician", "Example Litho"),
                                        {"url": "https://example.wd1.myworkdayjobs.com/x"}, [degree])
    assert not any("Degree list" in s for s in said), said  # (the conftest profile's BS Electrical Engineering is there)


def test_a_degree_list_with_several_kinds_of_the_profiles_degree_says_so(srv):
    """Onto Innovation's Workday (nightly live check, Oct 11): its Degree list has a Bachelor of Arts and a Bachelor
    of Science, and the profile says "Bachelor's Degree". The desk can't say which was earned, so the box is the
    person's, but the card said the list had no choice for it. It says the list has several kinds of it, and how to
    name the one earned in the profile."""
    import yaml

    path = config.profile_path()
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    school = profile["education_history"][0]["school"]
    profile["education_history"][0]["degree"] = "Bachelor's Degree"
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    applier = Applier(srv)
    degree = {"id": "a", "label": "Degree*", "section": "Education 1", "required": True, "kind": "listbox",
              "options": ["High School", "Associate of Science", "Bachelor of Arts", "Bachelor of Science",
                          "Master of Science"]}
    said, rest, _ = applier._entry_gaps(Run(1, "Technician", "Example Litho"),
                                        {"url": "https://example.wd1.myworkdayjobs.com/x"}, [degree])
    assert (f"The Degree list for {school} in Education 1 has several kinds of Bachelor's Degree (Bachelor of Arts, "
            "Bachelor of Science)") in said, said
    assert "has no choice for" not in said and "degree: Bachelor of Science" in said, said
    assert rest == []


def test_a_degree_list_with_nothing_for_classes_leaves_the_pick_to_the_person(srv):
    """A Workday site's Degree list holds High School, GED, Associates and up, and nothing for
    classes without a degree (live, Oct 2026). For a second block of one school, where the person
    took classes, the desk named only the school and said to add "Some college (no degree)" to the
    profile, which can't go in there. It names the block and leaves that pick to the person."""
    import yaml

    path = config.profile_path()
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    school = profile["education_history"][0]["school"]
    profile["education_history"].append({"school": school, "degree": "", "start": 2021, "end": 2022})
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    applier = Applier(srv)
    r = Run(1, "Technician", "Example Litho")
    site = {"url": "https://example.wd1.myworkdayjobs.com/x"}
    degree = {"id": "a", "label": "Degree*", "section": "Education 2", "required": True, "kind": "listbox",
              "options": WORKDAY_DEGREES}
    said, rest, code = applier._entry_gaps(r, site, [degree])
    assert f"The Degree list for {school} in Education 2 has no choice for classes without a degree" in said, said
    assert code == "degree_list"  # (its stop's code: T1)
    assert "choose in the desk's browser window" in said and "Some college" not in said, said
    assert rest == []

    # a list that has such a choice: the profile can say it
    said, _, code = applier._entry_gaps(r, site, [{**degree, "options": [*WORKDAY_DEGREES, "Some College, No Degree"]}])
    assert f"{school} in Education 2 (Degree)" in said and "Some college (no degree)" in said, said
    assert code == "profile_gap"

    # written as setup writes it, and turned down by the list: still the person's pick, never "the closest"
    profile["education_history"][1]["degree"] = "Some college (no degree)"
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    said, _, _ = applier._entry_gaps(r, site, [{**degree, "error": "ValueError: doesn't match any option"}])
    assert "no choice for classes without a degree" in said and "closest choice" not in said, said


def degree_box(section):
    return {"id": "a", "label": "Degree*", "section": section, "required": True, "kind": "listbox",
            "options": WORKDAY_DEGREES}


def test_a_privacy_notice_received_is_ticked_on_create_account_but_never_a_sign_up():
    """Run 44 (Oct 13): Boeing's Workday Create Account requires "I have received the Boeing Applicant Privacy
    Notice.", and the desk left it, as it read "received" as a sign-up's "receive emails". A privacy notice
    received is no more than its terms (settings.accept_notices); receiving emails or updates still never is."""
    from job_apply.pipeline import _terms_boxes

    def ticked(label, required=True):
        return bool(_terms_boxes({"fields": [{"id": "a", "kind": "checkbox", "label": label, "value": False,
                                              "required": required}]}))

    assert ticked("I have received the Example Fab Applicant Privacy Notice.")
    assert ticked("I have read and received a copy of the Privacy Policy")
    assert not ticked("I agree to receive emails about new jobs")
    # (the review of 0.3.176: a required consent that takes in more than the account: a drug screening)
    assert not ticked("I consent to a background check and drug screening as part of my application")
    assert not ticked("I have received the Privacy Notice and agree to receive job alerts by email")
    # (the review's) the notice's own name is still read: one for a talent pool or future openings isn't the terms
    for label in ("I have received the Talent Pool Privacy Notice.", "I have received the Future Opportunities Privacy Notice.",
                  "I have received the Third-Party Data Sharing Privacy Statement."):
        assert not ticked(label), label


def test_a_sections_blocks_are_counted_by_their_own_deletes_too(srv):
    """Run 44 (Oct 13): UKG Pro fills a Work Experience and an Education block from the resume, with no numbered
    heading, and covers its Add buttons with the resume's note; the desk read "0 of your 1 job" and stopped Nikon,
    UCT and Desert Financial, which reached their review pages before. Their own "Delete Work Experience 1" counts."""
    applier = Applier(srv)
    r = Run(1, "FSE", "Example Fab")
    covered = {"add_button_covered": True, "after": 0, "wanted": 1}
    ukg = {"actions": [{"id": "a", "text": "Add Experience"}, {"id": "b", "text": "Delete Work Experience 1"},
                       {"id": "c", "text": "Add Education"}, {"id": "d", "text": "Delete Education 1"},
                       {"id": "e", "text": "Delete resume.pdf"}]}
    for section in ("work", "education"):
        applier._note_short_entries(r, section, covered, ukg)
    assert r.entries_short == {}
    applier._note_short_entries(r, "work", {**covered, "wanted": 2}, ukg)
    assert r.entries_short == {"work": (1, 2)}  # (one of two, still short)
    spaced = {"actions": [{"id": "s", "text": "Delete Work  Experience\n2"}, {"id": "t", "text": "Delete Work Experience 1"}]}
    applier._note_short_entries(r, "work", {**covered, "wanted": 2}, spaced)
    assert "work" not in r.entries_short  # (its spacing is no other block)
    applier._note_short_entries(r, "education", covered, {"actions": [{"id": "e", "text": "Delete resume.pdf"}]})
    assert r.entries_short["education"] == (0, 1)
    # (the review's: another section's Delete, or one inside a block, is no block of this one)
    others = {"actions": [{"id": "m", "text": "Delete Military Experience 1"}, {"id": "j", "text": "Delete Job Reference 1"},
                          {"id": "a", "text": "Delete Work Experience 1 Attachment 2"}]}
    applier._note_short_entries(r, "work", covered, others)
    assert r.entries_short["work"] == (0, 1)
    # UKG Pro's saved items (run 49: still "0 of your 1 school" at Nikon, UCT and Desert Financial): each has its own
    # numbered Edit; a section's own Edit ("Edit Skills") is no block
    saved = {"actions": [{"id": "x", "text": "Edit Experience Item 1"}, {"id": "y", "text": "Add Education"},
                         {"id": "z", "text": "Edit Education Item 1"}, {"id": "k", "text": "Edit Skills"}]}
    r.entries_short = {}
    for section in ("work", "education"):
        applier._note_short_entries(r, section, covered, saved)
    assert r.entries_short == {}
    applier._note_short_entries(r, "education", covered, {"actions": [{"id": "k", "text": "Edit Skills"}]})
    assert r.entries_short["education"] == (0, 1)


def test_a_reset_form_is_still_showing_only_with_its_own_button_and_no_word_it_went():
    """The review of notes 14/34: a site that says the reset went in its own words ("has been successfully reset",
    "Password changed successfully"), or that has taken its button away, isn't a reset the site refused."""
    from job_apply.pipeline import _RESET_DONE

    for said in ("Your password has been successfully reset.", "Password changed successfully", "Password reset successful"):
        assert _RESET_DONE.search(said), said
    boxes = [{"id": "a", "kind": "password", "label": "New Password"}, {"id": "b", "kind": "password",
                                                                           "label": "Verify New Password"}]
    page = {"fields": boxes, "actions": [{"id": "s", "text": "Submit"}]}
    showing = Applier._reset_form_showing
    assert showing(page, "Reset Password", "submit")
    assert not showing({**page, "actions": []}, "Reset Password", "submit")
    assert not showing(page, "Reset Password. Your password has been successfully reset.", "submit")


def test_a_school_block_is_deleted_only_where_it_is_the_last_of_several(srv):
    """Issue #112, note 26: the desk deletes a classes-only school's block whose Degree list has nothing for it,
    but never the page's only school block (the site may need one), nor one before others (Workday numbers the
    blocks after it down, onto the wrong school), nor one it deleted before; those stay the person's."""
    import yaml

    path = config.profile_path()
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile["education_history"].append({"school": profile["education_history"][0]["school"], "degree": "",
                                         "start": 2021, "end": 2022})
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    applier, clicked, gone = Applier(srv), [], []

    class Stub:
        async def click(self, id):
            clicked.append(id)
            gone.append(f"Education {int(id[1:]) + 1}")
            return {"clicked": True}
    applier.srv = Stub()

    def page(*sections):
        return {"fields": [{"id": f"s{i}", "label": "School", "section": s} for i, s in enumerate(sections)],
                "actions": [{"id": f"d{i}", "text": "Delete", "section": s, "names_block": True}
                            for i, s in enumerate(sections)]}

    async def look(**kw):
        shown = page(*[s for s in ("Education 1", "Education 2") if s not in gone])
        return shown, ""
    applier._look = look

    r = Run(1, "Technician", "Example Litho")
    # a degree the profile lacks there, or one under way, is never taken for classes: the person's
    for said in ("", "BS in progress", "Bachelor of Science (expected 2027)"):
        profile["education_history"][1]["degree"] = said
        path.write_text(yaml.safe_dump(profile), encoding="utf-8")
        assert not run(applier._drop_unfit_school(r, page("Education 1", "Education 2"), [degree_box("Education 2")]))
    profile["education_history"][1]["degree"] = "Some college (no degree)"
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")

    assert not run(applier._drop_unfit_school(r, page("Education 2"), [degree_box("Education 2")]))  # its only one
    assert not run(applier._drop_unfit_school(r, page("Education 1", "Education 2"), [degree_box("Education 1")]))
    assert not clicked
    assert run(applier._drop_unfit_school(r, page("Education 1", "Education 2"), [degree_box("Education 2")]))
    assert clicked == ["d1"] and r.schools_dropped == ["Education 2"]
    # (a file's Delete, the only one in the block, named for the file: never pressed)
    r2 = Run(2, "Technician", "Example Litho")
    file_delete = {**page("Education 1", "Education 2"), "actions": [{"id": "f1", "text": "Delete", "section": "Education 2",
                                                                       "names_other": True}]}
    assert not run(applier._drop_unfit_school(r2, file_delete, [degree_box("Education 2")])) and clicked == ["d1"]
    assert not run(applier._drop_unfit_school(r, page("Education 1", "Education 2"), [degree_box("Education 2")]))  # once


@pytest.mark.slow
def test_a_workday_experience_page_is_filled_block_by_block(srv, monkeypatch, job_apply_home):
    """A Workday site's My Experience page as drawn live (Oct 2026): each date a fieldset of its
    own, and an Education block's boxes drawn a moment after its heading. Every job's and
    school's dates go in, and the second block of one school is filled once drawn. Its Degree
    list has no choice for its classes without a degree: the person deleted such a block by hand
    each time (issue #112, note 26), so the desk deletes it, and never adds it back."""
    import yaml

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    school = profile["education_history"][0]["school"]
    profile["education_history"].append({"school": school, "degree": "Some college (no degree)", "major": "Physics",
                                         "start": 2021, "end": 2022})
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    job = srv.add_job(url=fixture_url("site/workday-experience-blocks.html"), title="Equipment Technician",
                      company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            # (on past the school deleted, to its Save and Continue, which goes nowhere here: the stall's waits)
            await until(lambda: r.status not in ("queued", "running"), timeout=180, about=state(r))
            return r, await r.page.evaluate(
                "() => [...document.querySelectorAll('[role=spinbutton], [name=school], [name=degree]')]"
                ".filter((e) => e.getClientRects().length).map((e) => e.value || e.textContent)")
        finally:
            await applier.stop()

    r, shown = run(go())
    assert "no choice for classes without a degree" not in r.reason, (r.status, r.reason, r.log)
    assert any(line.startswith(f"deleted Education 2 ({school})") for line in r.log), r.log
    assert not r.questions, r.questions
    assert shown == ["03", "2021", "06", "2018", "02", "2021", school, "Bachelors", "2016", "2020"], shown


def test_an_extra_block_is_deleted_only_when_empty_last_and_one_the_desk_added(srv):
    """The review's limits on _drop_added_extra: never a block with anything in it, nor one of a kind the desk added
    none of on this pass, nor the only block of its kind, nor one before others (Workday numbers those after it down),
    nor more than the desk added, and each block's Delete pressed once."""
    applier, clicked, gone, marked = Applier(srv), [], [], ["EDUCATION 3"]

    class Browser:
        async def added_blocks(self):
            return marked

    class Stub:
        browser = Browser()

        async def click(self, id):
            clicked.append(id)
            gone.append(id.split(":", 1)[1])
            return {"clicked": True}
    applier.srv = Stub()

    def page(blocks, filled=(), boxless=()):
        return {"fields": [{"id": f"s{i}", "label": "School or University*", "section": b, "required": True,
                            "kind": "text", "value": "Example College" if b in filled else ""}
                           for i, b in enumerate(blocks) if b not in gone and b not in boxless],
                "actions": [{"id": f"d:{b}", "text": "Delete", "section": b, "names_block": True}
                            for b in blocks if b not in gone]}

    async def look(**kw):
        return page(["Education 1", "Education 2", "Education 3"]), ""
    applier._look = look

    def extra(block):
        return {"id": "x", "label": "School or University*", "section": block, "required": True, "kind": "text"}

    r = Run(1, "Technician", "Example Litho")
    two = ["Education 1", "Education 2"]
    # none added, something in it, or the only one: never
    assert not run(applier._drop_added_extra(r, page(two), [extra("Education 2")], {}, set()))
    assert not run(applier._drop_added_extra(r, page(two, filled=two), [extra("Education 2")], {"education": 1}, set()))
    assert not run(applier._drop_added_extra(r, page(["Education 2"]), [extra("Education 2")], {"education": 1}, set()))
    # one before others, or a work block where only a school was added: never
    three = ["Education 1", "Education 2", "Education 3"]
    assert not run(applier._drop_added_extra(r, page(three), [extra("Education 2")], {"education": 1}, set()))
    assert not run(applier._drop_added_extra(r, page(two), [extra("Education 2")], {"work": 1}, set()))
    # one whose boxes aren't drawn yet (its Delete alone), or one no Add of the desk's drew (a saved one, empty for a
    # moment): never
    assert not run(applier._drop_added_extra(r, page(three, boxless=["Education 3"]), [extra("Education 3")],
                                             {"education": 1}, set()))
    assert not run(applier._drop_added_extra(r, page(two), [extra("Education 2")], {"education": 1}, set()))
    # a job's block with only its "I currently work here" ticked: something in it
    jobs = {"fields": [{"id": "c2", "label": "I currently work here", "section": "Work Experience 2", "kind": "checkbox",
                        "value": True},
                       {"id": "t2", "label": "Job Title*", "section": "Work Experience 2", "required": True, "kind": "text",
                        "value": ""}],
            "actions": [{"id": "d:Work Experience 1", "text": "Delete", "section": "Work Experience 1", "names_block": True},
                        {"id": "d:Work Experience 2", "text": "Delete", "section": "Work Experience 2", "names_block": True}]}
    job_box = {"id": "y", "label": "Job Title*", "section": "Work Experience 2", "required": True, "kind": "text"}
    marked.append("Work Experience 2")
    assert not run(applier._drop_added_extra(r, jobs, [job_box], {"work": 1}, set()))
    assert not clicked
    # the last, empty, of a kind the desk added: deleted, and no more than it added
    added, tried = {"education": 1}, set()
    assert run(applier._drop_added_extra(r, page(three), [extra("Education 2"), extra("Education 3")], added, tried))
    assert clicked == ["d:Education 3"] and added == {"education": 0}
    assert any(line.startswith("deleted Education 3: an empty block past your profile's schools") for line in r.log)
    assert not run(applier._drop_added_extra(r, page(two), [extra("Education 2")], added, tried))
    # once a block: its Delete pressed before (and the block back, or still there), never again
    gone.clear()
    assert not run(applier._drop_added_extra(r, page(three), [extra("Education 3")], {"education": 1}, tried))
    assert clicked == ["d:Education 3"]


@pytest.mark.slow
def test_an_empty_block_the_desk_added_beside_a_saved_one_drawn_late_is_deleted_again(srv, monkeypatch, job_apply_home):
    """Hitachi High-Tech America's Workday (run 92), and U.S. Bank's and Baker Tilly's (runs 52 and 62): My Experience
    drew the account's saved school (or job) after the desk had looked for it and added a block, and the page then had
    more blocks than the profile has entries: the desk stopped, "Remove the extra block in the browser". The block the
    desk itself added, still empty, past the profile's entries, is deleted again with its own Delete, and the
    application goes on; the saved one, filled, stays."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/workday-experience-blocks.html") + "?savedschool", title="Equipment Technician",
                      company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            # (on past the block deleted, to its Save and Continue, which goes nowhere here: the stall's waits)
            await until(lambda: r.status not in ("queued", "running"), timeout=180, about=state(r))
            return r, await r.page.evaluate(
                "() => [...document.querySelectorAll('#ed-panels > div')].map((p) => [p.querySelector('h3').textContent, "
                "p.dataset.saved || ''])")
        finally:
            await applier.stop()

    r, blocks = run(go())
    assert "more blocks than your profile has entries" not in r.reason, (r.status, r.reason, r.log)
    assert any(line.startswith("deleted Education 2") for line in r.log), r.log
    assert blocks == [["Education 1", "1"]], (blocks, r.log)  # (the saved one stays)


def test_a_saved_block_drawn_after_the_desks_own_is_never_taken_for_it(srv, monkeypatch, job_apply_home):
    """The review of 0.3.173: the account's saved school drawn after the block the desk added, last, and its boxes
    filled a while after it shows (empty till then): that block is past the profile's entries and empty, but no Add of
    the desk's drew it, so it's never deleted: the person's, as before."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/workday-experience-blocks.html") + "?savedschool&savedlast&valueslate=30000",
                      title="Equipment Technician", company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=180, about=state(r))
            return r, await r.page.evaluate(
                "() => [...document.querySelectorAll('#ed-panels > div')].map((p) => [p.querySelector('h3').textContent, "
                "p.dataset.saved || ''])")
        finally:
            await applier.stop()

    r, blocks = run(go())
    assert not any(line.startswith("deleted Education") for line in r.log), r.log
    assert blocks == [["Education 1", ""], ["Education 2", "1"]], (blocks, r.log)


@pytest.mark.slow
def test_an_empty_block_the_desk_added_is_deleted_when_the_saved_one_took_its_place(srv, monkeypatch, job_apply_home):
    """Arizona State University's Workday (run 108): My Experience drew the account's saved job INTO the block the desk's
    Add had just drawn (Workday numbers its blocks by place: the desk's mark stayed on it, now filled by the site), and
    appended a new empty block after it, unmarked; the desk, finding that one past the profile's entries without its
    mark, left it and stopped ("Remove the extra block"). A marked block of that kind holding values the desk never
    typed is the saved entry drawn into it: the empty last block is the desk's own, deleted again."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/workday-experience-blocks.html") + "?savedschool&reused",
                      title="Equipment Technician", company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=180, about=state(r))
            return r, await r.page.evaluate(
                "() => [...document.querySelectorAll('#ed-panels > div')].map((p) => [p.querySelector('h3').textContent, "
                "p.dataset.saved || ''])")
        finally:
            await applier.stop()

    r, blocks = run(go())
    assert "more blocks than your profile has entries" not in r.reason, (r.status, r.reason, r.log)
    assert any(line.startswith("deleted Education 2") for line in r.log), r.log
    assert blocks == [["Education 1", "1"]], (blocks, r.log)  # (the saved one, in the desk's own block, stays)


@pytest.mark.slow
def test_a_framed_application_gets_its_blocks_and_both_jobs(srv, monkeypatch):
    """The same in the desk's own laps: an application inside a frame on the careers page (iCIMS's) gets its second
    work block and its school block added in the frame and filled from the profile, and the careers page's own "Add"
    under a heading of the kind is left alone."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/framed-experience-blocks.html") + "?outeradd", title="Equipment Technician",
                      company="Example Robotics")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            # (on to the frame's Save and Continue, which goes nowhere here: the stall's waits)
            await until(lambda: r.status not in ("queued", "running"), timeout=180, about=state(r))
            frame = next(f for f in r.page.frames if f is not r.page.main_frame)
            return r, await frame.evaluate(
                "() => [...document.querySelectorAll('[name=jobTitle], [name=companyName], [name=school]')].map((e) => e.value)"
            ), await r.page.evaluate("() => window.outerPressed || 0")
        finally:
            await applier.stop()

    r, values, outer = run(go())
    assert "added 1 work block(s)" in r.log and "added 1 education block(s)" in r.log, r.log
    assert values == ["Equipment Technician", "Intel", "Maintenance Technician", "Example Fab Services",
                      "Arizona State University"], (values, r.log)
    assert outer == 0, r.log
    assert "more blocks than your profile has entries" not in r.reason, (r.status, r.reason, r.log)


@pytest.mark.slow
def test_an_empty_block_the_desk_added_in_a_frame_is_deleted_when_the_saved_one_took_its_place(srv, monkeypatch):
    """The review of 0.3.186: browser.added_blocks read the page around a frame only, so the desk's own block in a
    framed application was never known for its, and never deleted again. As on a page of its own (Arizona State
    University's Workday, run 108), in a frame too."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/framed-experience-blocks.html") + "?savedschool&reused",
                      title="Equipment Technician", company="Example Robotics")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=180, about=state(r))
            frame = next(f for f in r.page.frames if f is not r.page.main_frame)
            return r, await frame.evaluate(
                "() => [...document.querySelectorAll('#ed-panels > div')].map((p) => [p.querySelector('h3').textContent, "
                "p.dataset.saved || ''])")
        finally:
            await applier.stop()

    r, blocks = run(go())
    assert "more blocks than your profile has entries" not in r.reason, (r.status, r.reason, r.log)
    assert any(line.startswith("deleted Education 2") for line in r.log), r.log
    assert blocks == [["Education 1", "1"]], (blocks, r.log)


@pytest.mark.parametrize("query, picked", [("?slow=6000", True), ("?slow=6000&placeholderfirst", True),
                                           ("?slow=6000&nolist", True), ("?slow=20000", False),
                                           ("?slow=600&closes", True), ("?slow=600&nolist&closes", True),
                                           ("?slow=20000&nolist", False)])
def test_a_self_identification_list_that_fetches_its_choices_on_first_open_is_waited_on(srv, monkeypatch, query, picked):
    """SuccessFactors' paginated selects (Freeport-McMoRan's Ethnicity, Edwards Vacuum's Protected Veteran, Rogers'
    Gender and Ethnicity: 20 of the live check's runs) fetch their entries when first opened, a few seconds on their
    servers: the desk read no choices in its two and a half seconds and asked the person, though the list's "I choose
    not to self-identify" is the profile's own answer. A list open and still empty, or on its "No Selection" alone,
    or not drawn at all yet (&nolist: Edwards' and Freeport's in run 111, where 0.3.185's wait never began), is waited
    on (PICK_LIST_WAIT, 10 s); one still empty after that is asked about as before. One that closed again by itself
    before its choices were read (&closes: Edwards' in run 114) is opened once more."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    for name, wait in (("LIST_OPEN_WAIT", 2000), ("COMBO_OPEN_WAIT", 900), ("LIST_PLACEHOLDER_WAIT", 5000),
                       ("PICK_OPEN_WAIT", 2500), ("LIST_REOPEN_WAIT", 3000)):
        monkeypatch.setattr(browser_module, name, wait)  # (production's: the list draws late)
    job = srv.add_job(url=fixture_url("site/sf-select-slow.html") + query, title="ET", company="Example Semi")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => [...document.querySelectorAll('input')].map((i) => i.value)")
        finally:
            await applier.stop()

    r, values = run(go())
    if picked:
        assert r.status == "ready" and not r.questions, (r.status, r.need, r.reason, r.log)  # (never asked)
        assert values == ["Sam", "I choose not to self-identify"], (values, r.log)
    else:
        assert r.need == "questions" and [q["label"] for q in r.questions] == ["Pre-Offer : Are you a Protected Veteran?"], \
            (r.status, r.need, r.reason, r.log)
        assert "showed no choices" in r.questions[0]["error"] and values == ["Sam", ""], (r.questions, values)


def test_a_posting_that_has_closed_is_skipped_by_itself(srv, monkeypatch):
    """Edward Jones' BrassRing page for an expired posting (live, Oct 2026) says "The job posting
    you are looking for has expired or the position has already been filled", and the desk said
    it couldn't find the button; then it said the posting had closed, and stopped for the person to skip
    it. Nothing about a closed posting needs the person (issue #112, note 39: the owner's call): it's
    skipped by itself, with the site's words, among the jobs done."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/expired-posting.html"), title="Analyst", company="Example Investments")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("skipped", "") and r.reason.startswith("Skipped: posting closed."), (r.reason, r.log)
    assert "“The job posting you are looking for has expired or the position has already been filled.”" \
        in r.reason and "find the button" not in r.reason, r.reason
    assert not r.blocking and srv.get_job(job["id"])["job"]["status"] == "skipped"


def test_a_filled_position_is_skipped_by_itself_quoted_in_its_own_words_and_undo_puts_it_back(srv, monkeypatch):
    """Issue #112, note 39: an employer's SuccessFactors career site said of a job "Sorry, this position has been
    filled." The desk stopped the job for the person, with a card telling them to skip it, quoting the site's header
    and menus up to the first comma ("Your Career at ... CREATE ALERT Sorry,"). The job is skipped by itself (the
    tracker's "skipped", its tab closed), quoting the paragraph that said it, and the queue goes on to the next job.
    Undo puts it back: the tracker's status from before, and the job queued again, where a posting still closed is
    the person's to look at (never skipped by itself again)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    closed = srv.add_job(url=fixture_url("site/sf-posting-filled.html"), title="Equipment Technician",
                         company="Example Fab")["job"]["id"]
    after = srv.add_job(url=fixture_url("site/step1.html"), title="FSE", company="Example Fab")["job"]["id"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(closed)
            nxt = applier.enqueue(after)
            # (the queue went on to the next job, and filled it as far as it goes)
            await until(lambda: r.status not in ("queued", "running") and nxt.status not in ("queued", "running"),
                        about=state(nxt))
            assert nxt.seen_form and not nxt.blocking, state(nxt)()
            skipped = (r.status, r.need, r.reason, r.page.is_closed(), srv.get_job(closed)["job"]["status"])
            await applier.unskip(closed)
            undone = srv.get_job(closed)["job"]["status"]
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, skipped, undone
        finally:
            await applier.stop()

    r, (status, need, reason, tab_closed, tracked), undone = run(go())
    assert (status, need, tracked) == ("skipped", "", "skipped") and tab_closed, (status, reason, r.log)
    assert reason == ("Skipped: posting closed. Example Fab's site says “Sorry, this position has been filled.” "
                      "Undo puts it back."), reason
    assert any(line.startswith("skipped by itself: posting closed") for line in r.log), r.log
    assert undone == "in_progress", undone
    # put back by the person: theirs to look at, never skipped by itself again
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert r.reason.startswith("Example Fab's site says “Sorry, this position has been filled.” You put this "
                               "job back"), r.reason
    assert srv.get_job(closed)["job"]["status"] == "in_progress"


@pytest.mark.parametrize("how", ["resumed", "in the form"])
def test_a_closed_posting_the_person_or_the_form_has_been_at_is_theirs_to_skip(srv, monkeypatch, how):
    """The review of note 39: a job is skipped by itself only on the way in. After the person's Resume, or once the
    desk is in the application, words saying the posting closed may be about something else there, and what's been
    done is theirs to judge: the job stops for them, as before, quoting the site's own words, and nothing is skipped."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/sf-posting-filled.html"), title="Equipment Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)
    applier.runs[job["id"]] = Run(job["id"], "Equipment Technician", "Example Fab", hands_on=how == "resumed",
                                  seen_form=how == "in the form")

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert r.reason.startswith("Example Fab's site says this posting has closed: “Sorry, this position has been "
                               "filled.” If so"), r.reason
    assert srv.get_job(job["id"])["job"]["status"] != "skipped" and not r.page.is_closed()


def test_a_closed_postings_words_are_quoted_from_the_element_that_says_so():
    """The quote is the sentence that says the posting closed, within its own line of the page's text (an element of
    its own), never the header and menus before it (issue #112, note 39)."""
    header = ("Your Career at Example Fab\nJobs at Example Group\nLanguage\nView profile / Sign in\nEmployee login\n"
              "Show More Options\nClear\nSelect how often (in days) to receive an alert:\nCREATE ALERT\n")
    assert pipeline._closed_notice({}, header + "Sorry, this position has been filled.\nExample Fab is an equal "
                                   "opportunity employer.") == "Sorry, this position has been filled."
    assert pipeline._closed_notice({}, header + "Sorry, this position has been filled") \
        == "Sorry, this position has been filled"
    said = "Jobs. The job posting you are looking for has expired or the position has already been filled. Look again."
    assert pipeline._closed_notice({}, said) == ("The job posting you are looking for has expired or the position has "
                                                 "already been filled.")
    assert pipeline._closed_notice({"errors": ["This job is no longer available."]}, "") == "This job is no longer available."
    assert pipeline._closed_notice({}, header) == ""
    # a posting's ordinary words about when or how it closes, never that it has (the review's)
    for said in ("Applications will be accepted until the position has been filled.",
                 "This posting will remain open until the position is filled.",
                 "This position is filled on a rolling basis.", "Our job board is closed for maintenance.",
                 "If this job has expired, search for similar ones.",
                 "Note: the requisition is closed to external applicants.",
                 "Recently closed jobs: Technician II (position has been filled)"):
        assert pipeline._closed_notice({}, said) == "", said
    assert pipeline._closed_notice({}, "The position you applied for has been filled by an internal candidate.") \
        == "The position you applied for has been filled by an internal candidate."
    # (closed to new applicants is closed: the second review)
    assert pipeline._closed_notice({}, "This position is closed to new applicants.") \
        == "This position is closed to new applicants."


@pytest.mark.parametrize("way_in", ["continue", "submit for me", "apply"])
def test_a_saved_drafts_continue_application_is_the_way_in_and_its_answers_are_checked(srv, monkeypatch, way_in):
    """Issue #112, note 38: Workday kept a job the desk began on an earlier visit as a draft, and its job page showed
    "Continue Application" where Apply usually is. The desk didn't take it for the way in: it reloaded the page and
    said it couldn't find the button. It's the way in, as Apply is; and the draft's boxes hold the answers given then,
    which may be out of date (the work descriptions had changed in the profile since): on its pages the profile's
    answer goes in where the box shows another, and the log says so by the boxes' labels. Never a box the profile
    doesn't answer (the favourite tool), a saved answer that only roughly matches (clean room's), nor their answer for
    this application (the phone). An application gone into by Apply, its boxes filled (a resume's parse, say), keeps
    what they show. The reviews: the draft's old answers are on the job's card alone ("What went in": "Last Name:
    Rivers → Rivera"), never in its log or a report; and Submit for me never sends a draft whose answers the desk
    changed: it waits on its review page for the person to check them."""
    from job_apply import report

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    query = "?apply" if way_in == "apply" else ""
    job = srv.add_job(url=fixture_url("site/workday-continue-posting.html") + query, title="Equipment Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)
    applier.auto_submit = way_in == "submit for me"
    applier.runs[job["id"]] = Run(job["id"], "Equipment Technician", "Example Fab",
                                  once={question_key("Phone Number"): "480-555-0000"})

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"], submit=applier.auto_submit)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and "review.html" in r.url, (r.status, r.reason, r.log)
    saved = dict(parse_qsl(urlparse(r.url).query))
    kept = {"fn": "Sam", "em": "sam.rivera@example.com", "ph": "480-555-0000", "jt-1": "Equipment Technician",
            "co-1": "Intel", "tool": "Torque wrench", "cr": "No"}
    assert {k: saved.get(k) for k in kept} == kept, (saved, r.log)
    checked = [line for line in r.log if line.startswith("re-checked the saved draft's answers")]
    if way_in != "apply":
        assert "clicked “Continue Application”" in r.log, r.log
        assert any("kept this application from an earlier visit (a saved draft)" in line for line in r.log), r.log
        assert (saved["ln"], saved["rd-1"]) == ("Rivera", "Maintained 300mm etch and deposition tools."), (saved, r.log)
        assert checked == ["re-checked the saved draft's answers on “My Information”: 2 field(s) showed another "
                           "answer than your profile's, so I put your profile's in: “Last Name”, “Role "
                           "Description”"], r.log
        assert r.draft_changed and "I changed some of its answers to your profile's (see What went in)" in r.reason
        assert ("Submit for me left it for you" in r.reason) == (way_in == "submit for me"), r.reason
        assert r.public()["went_in"]["changed"] == [
            "Last Name: Rivers → Rivera",
            "Work Experience 1: Role Description: Ran the wet benches on nights. → Maintained 300mm etch and "
            "deposition tools."]
        said = "\n".join(r.log) + report.build(srv.tracker().get(job["id"]), r)["preview"]
        for old in ("Rivers", "Ran the wet benches"):  # (the phone, kept, is in the test page's review address)
            assert old not in said, old  # (the draft's old answers: on the card alone)
    else:
        assert (saved["ln"], saved["rd-1"]) == ("Rivers", "Ran the wet benches on nights."), (saved, r.log)
        assert not checked and not r.draft and not r.draft_changed, r.log


@pytest.mark.parametrize("case", ["stopped", "refused"])
def test_a_drafts_boxes_are_left_after_a_stop_and_one_refusing_its_answer_is_read_again(srv, monkeypatch, case):
    """The reviews of note 38's refresh. A draft's job whose re-checking stopped ("stopped": the person's Resume, or a
    stop where they may have changed its boxes) has its pages filled as any others, what they show left. A box that
    won't take the profile's answer ("refused") is read again: one holding something is said by its label (never what
    it holds: a draft's old answer is no detail of the profile's a report's scrubber knows), and one the fill left empty
    is asked, as any fill that didn't go in. What changed is on the job's card ("What went in") alone."""
    from job_apply import report

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/draft-form.html"), title="Equipment Technician", company="Example Fab")["job"]
    applier = Applier(srv)
    applier.runs[job["id"]] = Run(job["id"], "Equipment Technician", "Example Fab", draft=True,
                                  draft_off=case == "stopped")
    if case == "refused":
        real = srv.autofill

        async def autofill(*args, **kwargs):  # (the last name's fill emptied the box; the description's left it)
            out = await real(*args, **kwargs)
            if not kwargs.get("refresh"):
                return out
            await srv.browser.current_tab.evaluate("() => { document.getElementById('ln').value = ''; "
                                                   "document.getElementById('rd-1').value = 'Ran the wet benches'; }")
            def gone(f):
                return bool(f.get("refreshed")) and any(w in (f.get("label") or "") for w in ("Last Name", "Role Description"))
            refused = [{**f, "error": "TimeoutError: didn't take", "kind": "text", "required": True}
                       for f in out["filled"] if gone(f)]
            return {**out, "filled": [f for f in out["filled"] if not gone(f)], "failed": out["failed"] + refused}
        monkeypatch.setattr(srv, "autofill", autofill)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    checked = [line for line in r.log if line.startswith("re-checked the saved draft's answers")]
    if case == "stopped":
        assert r.status == "ready" and not checked and not r.draft_changed, (r.status, r.reason, r.log)
        saved = dict(parse_qsl(urlparse(r.url).query))
        assert (saved["ln"], saved["rd-1"], saved["cr"]) == ("Rivers", "Ran the wet benches on nights.", "No"), saved
    else:
        assert checked == ["re-checked the saved draft's answers on “My Information”: 1 field(s) showed another "
                           "answer than your profile's, so I put your profile's in: “Phone Number”",
                           "re-checked the saved draft's answers on “My Information”: 2 field(s) wouldn't take "
                           "your profile's answer; as they are now: “Role Description”; 1 left empty, so "
                           "they're asked"], r.log
        assert r.status == "needs_you" and "Last Name" in r.reason, (r.need, r.reason, r.questions, r.log)
        assert r.draft_changed and r.went_in.changed == [
            "Phone Number: 480-555-0000 → 480-555-0123", "Last Name: Rivers → (empty)",
            "Work Experience 1: Role Description: Ran the wet benches on nights. → Ran the wet benches"], \
            r.went_in.changed
        said = "\n".join(r.log) + report.build(srv.tracker().get(job["id"]), r)["preview"] + report.note(
            srv.tracker().get(job["id"]), r)
        for old in ("Rivers", "Ran the wet benches", "480-555-0000"):
            assert old not in said, old  # (the draft's old answers: on the card alone)


def test_a_drafts_answers_are_rechecked_after_a_card_but_not_after_a_stop_or_a_resume(srv):
    """The second review of note 38's refresh: a draft's re-checking went on no further after its job's first pause,
    a questions card's among them, and said nothing. It goes on after a stop the person answers without the
    application's boxes (a questions card, an emailed code, a sign-in, a bot check); after any other, or the person's
    Resume, it stops for good, said once in the log ("draft not re-checked from here on")."""
    jobs = [srv.add_job(url=f"https://example.com/jobs/{i}", title="Technician", company="Example Fab")["job"]["id"]
            for i in range(2)]
    applier = Applier(srv)
    card, stop = (Run(j, "Technician", "Example Fab", draft=True) for j in jobs)
    applier.runs.update({card.job_id: card, stop.job_id: stop})
    for need in ("questions", "email_code", "sign_in", "bot_check"):
        applier._pause(card, need, "Waiting on you.", code="questions")
    assert not card.draft_off and not any("draft not re-checked" in line for line in card.log), card.log
    applier._pause(stop, "stuck", "The page didn't move on.", code="step_not_saved")
    applier._pause(stop, "stuck", "The page didn't move on again.", code="step_not_saved")
    assert stop.draft_off and sum("draft not re-checked from here on" in line for line in stop.log) == 1, stop.log
    applier.resume(card.job_id)
    assert card.draft_off and card.log[-1].startswith("draft not re-checked from here on"), card.log


def test_a_drafts_changed_answers_on_its_card_never_show_a_secret(monkeypatch):
    """What changed in a saved draft is shown on its card as "<label>: <old> → <new>", by the panel's rules: never a
    password, a code or a security answer, nor a value holding one of the desk's secrets."""
    monkeypatch.setenv("JOB_APPLY_SECRET_EXAMPLE_FAB", "Hunter2-Example!")
    r = Run(1, "Technician", "Example Fab", status="ready")
    page = {"url": "https://example.com/apply", "headings": ["My Information"], "fields": []}
    boxes = [{"id": "1", "kind": "text", "label": "Last Name *"}, {"id": "2", "kind": "password", "label": "Password"},
             {"id": "3", "kind": "text", "label": "Answer", "section": "Security questions"},
             {"id": "4", "kind": "text", "label": "Referral note"}, {"id": "5", "kind": "text", "label": "Passcode"}]
    pipeline._went_changed(r, [(boxes[0], "Rivers", "Rivera"), (boxes[1], "old-pass", "new-pass"),
                               (boxes[2], "Flagstaff", "Tucson"), (boxes[3], "note Hunter2-Example! here", "Hello"),
                               (boxes[4], "123456", "654321")], {**page, "fields": boxes})
    assert r.went_in.changed == ["Last Name: Rivers → Rivera"], r.went_in.changed
    assert r.public()["went_in"]["changed"] == ["Last Name: Rivers → Rivera"]


def test_a_drafts_changes_are_kept_across_a_restart_and_forgotten_once_it_goes_in(srv, monkeypatch):
    """The third review of note 38's: the hold on a draft the desk changed (never sent by Submit for me) and its card's
    lines lived in memory alone, and a restart of the desk lost them. They're kept in the job's own folder (never in the
    log, a note, a report or a saved page), read back when the job is applied to again (a line holding a saved secret
    left out), and forgotten once it goes in: the person's own Submit, or marked applied."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setenv("JOB_APPLY_SECRET_EXAMPLE_FAB", "Hunter2-Example!")
    job = srv.add_job(url=fixture_url("site/draft-form.html"), title="Equipment Technician", company="Example Fab")["job"]
    earlier = Run(job["id"], "Equipment Technician", "Example Fab")  # (the desk before its restart)
    Applier(srv)._draft_changes(earlier, [({"id": "1", "kind": "text", "label": "Last Name *"}, "Rivers", "Rivera")],
                                {"url": "https://example.com/apply", "fields": []})
    path = Path(srv.tracker().get(job["id"])["folder"]) / pipeline.DRAFT_FILE
    kept = json.loads(path.read_text(encoding="utf-8"))
    assert kept == {"draft_changed": True, "changed": ["Last Name: Rivers → Rivera"]}, kept
    kept["changed"].append("Note: Hunter2-Example! → hello")  # (a secret saved since: never read back)
    path.write_text(json.dumps(kept), encoding="utf-8")
    applier = Applier(srv)  # (the desk, restarted: the job's Run made afresh)
    applier.auto_submit = True
    applier.runs[job["id"]] = Run(job["id"], "Equipment Technician", "Example Fab", draft=True, draft_off=True)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"], submit=True)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            held = (r.status, r.reason, list(r.went_in.changed), path.exists())
            applier.submit_now(job["id"])  # (the person's own Submit, which goes)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, held
        finally:
            await applier.stop()

    r, (status, reason, changed, still) = run(go())
    assert status == "ready" and "Submit for me left it for you" in reason and still, (status, reason, r.log)
    assert changed == ["Last Name: Rivers → Rivera"], changed
    assert r.status == "submitted" and not path.exists(), (r.status, r.reason, r.log)
    assert not any("Rivers" in line for line in r.log)
    # marked applied: forgotten too
    Applier(srv)._draft_changes(earlier, [({"id": "1", "kind": "text", "label": "Last Name *"}, "Rivers", "Rivera")],
                                {"url": "https://example.com/apply", "fields": []})
    assert path.exists()
    applier.mark_applied(job["id"])
    assert not path.exists()


def test_a_drafts_kept_changes_that_wont_read_still_hold_it(srv):
    """The final review of note 38's: the draft's kept changes were written in place, so a crash mid-write could cut them
    short, and a file that wouldn't read was passed over: after a restart the job wasn't held, and Submit for me sent a
    draft the desk had changed. They're written whole or not at all, and one that won't read holds the job all the
    same, its card saying why."""
    job = srv.add_job(url=fixture_url("site/draft-form.html"), title="Equipment Technician", company="Example Fab")["job"]
    folder = Path(srv.tracker().get(job["id"])["folder"])
    earlier = Run(job["id"], "Equipment Technician", "Example Fab")
    Applier(srv)._draft_changes(earlier, [({"id": "1", "kind": "text", "label": "Last Name *"}, "Rivers", "Rivera")],
                                {"url": "https://example.com/apply", "fields": []})
    assert [f.name for f in folder.iterdir() if f.name.startswith(pipeline.DRAFT_FILE)] == [pipeline.DRAFT_FILE]
    for broken in ('{"draft_changed": true, "changed": ["Last Na', "[1, 2]", "\x00\xff"):
        (folder / pipeline.DRAFT_FILE).write_text(broken, encoding="utf-8")
        again = Run(job["id"], "Equipment Technician", "Example Fab")
        pipeline._restore_draft_changes(srv, again)
        assert again.draft_changed and again.went_in.changed == [pipeline.DRAFT_UNREAD], (broken, again.went_in.changed)
    (folder / pipeline.DRAFT_FILE).write_text('{"draft_changed": false}', encoding="utf-8")
    fine = Run(job["id"], "Equipment Technician", "Example Fab")
    pipeline._restore_draft_changes(srv, fine)
    assert not fine.draft_changed and not fine.went_in.changed


def test_a_draft_list_on_its_first_choice_answered_again_is_a_change_of_the_draft(srv, monkeypatch):
    """The third review of note 38's: a draft's boxes the desk answers again outside the re-check (a list on the site's
    first choice, a preset country set right) changed the draft all the same, and weren't counted: Submit for me sent
    it. They're the draft's changes too, on its card, and it waits for the person."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/draft-site-default.html"), title="Equipment Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)
    applier.auto_submit = True
    applier.runs[job["id"]] = Run(job["id"], "Equipment Technician", "Example Fab", draft=True)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"], submit=True)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready" and "Submit for me left it for you" in r.reason, (r.status, r.reason, r.log)
    assert dict(parse_qsl(urlparse(r.url).query)).get("g") == "Decline to self-identify", r.url
    assert r.went_in.changed == ["Gender: Male → Decline to self-identify"], r.went_in.changed


def test_a_draft_job_the_queue_carries_on_with_by_itself_is_no_longer_rechecked(srv, monkeypatch):
    """The third review of note 38's: a job left waiting on the person, whose tab the queue found past its pause and
    carried on with by itself: the person may have changed a saved draft's later pages by hand there. Its re-checking
    stops for good, said in its log."""
    job = srv.add_job(url="https://example.com/jobs/1", title="Technician", company="Example Fab")["job"]["id"]
    applier = Applier(srv)
    r = Run(job, "Technician", "Example Fab", status="needs_you", need="bot_check", left=True, draft=True)
    r.page = object()
    applier.runs[job] = r

    async def past(_run):
        return True
    monkeypatch.setattr(applier, "_past_pause", past)
    monkeypatch.setattr(srv.browser, "lost", lambda page: "")
    run(applier._pick_up_left())
    assert r.status == "queued" and r.draft_off, (r.status, r.log)
    assert r.log[-1].startswith("draft not re-checked from here on"), r.log


def test_continue_application_is_the_way_in_only_where_its_the_pages_one():
    """A saved draft's "Continue Application" (issue #112, note 38) is the way in on a page whose one way in it is (its
    job's page); never beside an Apply, on a form, nor among several (a candidate home's applications, each another
    job's). A dialog with one still stops the desk (_notice), as before."""
    def acts(*texts):
        return [{"id": str(i), "text": t} for i, t in enumerate(texts)]
    assert pick_next(acts("Sign In", "Continue Application"), False)["text"] == "Continue Application"
    assert pick_next(acts("Continue your application"), False)["text"] == "Continue your application"
    assert pick_next(acts("Continue Application", "Apply"), False)["text"] == "Apply"
    assert pick_next(acts("Continue Application", "Continue Application", "Search for Jobs"), False) is None
    assert pick_next(acts("Continue Application"), True) is None
    assert pick_next(acts("Save and Continue", "Continue Application"), True)["text"] == "Save and Continue"
    dialog = {"dialogs": [{"heading": "Application in progress", "buttons": [{"text": "Continue Application"},
                                                                          {"text": "Start Over"}]}]}
    assert pipeline._notice(dialog) is not None
    assert classify({"title": "Job", "fields": [], "actions": acts("Continue Application")},
                    "Check your email to verify your account later.") == "page"


def test_a_drafts_continue_application_is_pressed_only_where_its_this_jobs():
    """Whose draft a Continue Application is (the review of note 38's): the job's own page, or its link going on from
    the job's address or naming its requisition; never another page's (a candidate home with one draft)."""
    job = "https://acme.wd1.myworkdayjobs.com/External/job/Chandler-AZ/Equipment-Technician_R12345"
    button = {"id": "1", "text": "Continue Application"}
    assert pipeline._own_draft(button, {"url": job}, job)
    assert pipeline._own_draft({**button, "href": job + "/apply"}, {"url": "https://acme.wd1.myworkdayjobs.com/x"}, job)
    other_locale = "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Chandler-AZ/Equipment-Technician_R12345/apply"
    assert pipeline._own_draft({**button, "href": other_locale}, {"url": "https://acme.wd1.myworkdayjobs.com/en-US"}, job)
    home = {"url": "https://acme.wd1.myworkdayjobs.com/External/userHome"}
    assert not pipeline._own_draft(button, home, job)
    assert not pipeline._own_draft({**button, "href": "https://acme.wd1.myworkdayjobs.com/External/job/Tempe-AZ/"
                                                      "Process-Engineer_R12346/apply"}, home, job)
    eightfold = "https://careers.example-fab.example/careers?pid=7700123"
    assert pipeline._own_draft({**button, "href": "https://careers.example-fab.example/careers/apply?pid=7700123"},
                               home, eightfold)
    assert not pipeline._own_draft(button, {"url": "https://careers.example-fab.example/careers?pid=7700999"}, eightfold)
    # an id of a few characters is no job's id, and a job's id is a whole part of the link (the second review: a job
    # address ending "_2" made a link's "?step=2" this job's draft)
    short = "https://careers.example-fab.example/job/Technician_2"
    assert not pipeline._own_draft({**button, "href": "https://careers.example-fab.example/apply?step=2"}, home, short)
    assert not pipeline._own_draft({**button, "href": "https://acme.wd1.myworkdayjobs.com/External/job/Tempe-AZ/"
                                                      "Process-Engineer_R123456/apply"}, home, job)


def test_a_drafts_boxes_take_the_profiles_answer_only_where_it_differs():
    """plan_autofill's refresh (a saved draft's boxes, issue #112, note 38, and its reviews): the profile's answer where
    a box shows another, and a saved answer given for that very question (overtime's, answered on the desk); never a
    box the profile doesn't answer, one kept for them, a file, a self-identification answer (one declined at this
    employer stays declined), how they heard, a block whose employer isn't its entry's (a draft's jobs in another
    order), a saved answer whose pattern only roughly matches (clean room's "clean ?room"), the person's own words (a
    sentence, a text area) in place of a saved answer or a bare yes or no, nor one showing the profile's answer said
    another way (a ZIP+4, a phone's extension or country code, a salary's "$65,000", a place, an address, a URL, a date,
    a yes or no said more fully)."""
    import copy

    from conftest import PROFILE

    from job_apply.autofill import plan_autofill
    data = copy.deepcopy(PROFILE)
    data["preferences"]["desired_salary"] = "65000"
    overtime = "Are you able to work overtime?"
    data["answers"] = [{"match": config.question_pattern(overtime), "answer": "Yes", "question": overtime},
                       *data["answers"]]
    prof = config.Profile(data)

    def box(i, label, value, kind="text", **more):
        return {"id": i, "kind": kind, "label": label, "value": value, **more}
    yes_no = ["Select One", "Yes", "No"]
    fields = [box("fn", "First Name", "Sam"), box("ln", "Last Name", "Rivers"),
              box("ph", "Phone Number", "+1 (480) 555-0123"), box("ph2", "Mobile Phone", "480-555-0000"),
              box("ext", "Home Phone", "480-555-0123 ext 45"), box("zip", "Postal Code", "85225 1234"),
              box("city", "City", "Chandler, AZ"), box("addr", "Address Line 1", "100 West Main Street"),
              box("li", "LinkedIn Profile", "linkedin.com/in/samrivera"), box("sal", "Desired Salary", "$65,000"),
              box("cr", "Do you have clean room experience?", "No", "select", options=yes_no),
              box("crd", "Please describe your clean room experience", "4 years in Class 10 fabs, full bunny suit."),
              box("ot", overtime, "No", "select", options=yes_no),
              box("ot2", f"{overtime} If so, how many hours?", "Yes, up to 10 hours a week"),
              box("ot3", overtime, "Yes, I can"),
              box("rel", "Are you willing to relocate?", "Yes", "radio_group", options=["Yes", "No"]),
              box("rel2", "Would you consider relocating to Hillsboro, OR? Tell us more.",
                  "Yes, within 6 months for this role."),
              box("gen", "Gender", "Male", "select", options=["Select One", "Male", "Female", "Decline to self-identify"]),
              box("hh", "How did you hear about us?", "Employee Referral", "select",
                  options=["Select One", "LinkedIn", "Employee Referral"]),
              box("tool", "What is your favorite tool?", "Torque wrench"),
              box("cv", "Resume", "old-resume.pdf", "file"), box("em", "Email", "sam.old@example.com"),
              # (the first block, its employer the first job's: its own boxes checked; the second, the first job's again
              # where the second job's should be: left as it is)
              box("co1", "Company", "Intel", section="Work Experience 1"),
              box("lo1", "Location", "Chandler, Arizona", section="Work Experience 1"),
              box("from1", "From", "3/2021", section="Work Experience 1"),
              box("rd1", "Role Description", "Ran the wet benches.", "textarea", section="Work Experience 1"),
              box("co2", "Company", "Intel", section="Work Experience 2"),
              box("jt2", "Job Title", "Equipment Technician", section="Work Experience 2")]
    # (a choice's own wording is no sentence of the person's: the option the profile's answer picks against the one
    # shown, whatever the words; the third review's)
    spons = ["Yes, I will require sponsorship now or in the future", "No"]
    auth = ["Yes, I am authorized to work for any employer", "Yes, but I will need sponsorship", "No"]
    trav = ["Yes, up to 25%", "Yes, up to 75%", "No"]
    fields += [box("sp", "Will you now or in the future require sponsorship for employment visa status?", spons[0],
                   "radio_group", options=spons),
               box("au", "Are you legally authorized to work in the United States?", auth[1], "select",
                   options=["Select One", *auth]),
               box("tr", "Are you willing to travel?", trav[0], "select", options=["Select One", *trav]),
               box("rel3", "Are you willing to relocate?", "Yes, for the right role", "radio_group",
                   options=["Yes, for the right role", "No"])]
    plan = plan_autofill(fields, prof, {"company": "Example Fab"}, refresh=True, keep={"em"})
    assert {t["id"]: t["value"] for t in plan["to_fill"]} == {
        "ln": "Rivera", "ph2": "480-555-0123", "ot": "Yes", "rel": "No",
        "rd1": "Maintained 300mm etch and deposition tools.", "sp": "No", "au": auth[0], "tr": "Yes, up to 75%",
        "rel3": "No"}, plan["to_fill"]
    assert all(t.get("refreshed") for t in plan["to_fill"]) and not plan["needs_input"], plan
    assert plan_autofill(fields, prof, {})["to_fill"] == []  # (without refresh: what's there stays)
    # a street address is the whole of it (an apartment isn't the street's), and another ZIP is another
    apartment = [box("addr", "Address Line 1", "100 W Main St, Apt 5"), box("zip", "Postal Code", "85226")]
    assert {t["id"]: t["value"] for t in plan_autofill(apartment, prof, {}, refresh=True)["to_fill"]} == {
        "addr": "100 W Main St", "zip": "85225"}
    # a self-identification answer the draft declined stays declined, whatever the profile says now
    data["eeo"]["gender"] = "Male"
    declined = [box("gen", "Gender", "Decline to self-identify", "select",
                    options=["Select One", "Male", "Female", "Decline to self-identify"])]
    assert plan_autofill(declined, config.Profile(data), {}, refresh=True)["to_fill"] == []


@pytest.mark.parametrize("page", ["applied-before-posting.html", "applied-before-form.html"])
def test_a_job_the_site_says_was_applied_to_already_is_marked_applied_and_never_filled(srv, monkeypatch, page):
    """Issue #112, note 30: a job applied to by hand (the tracker never knew) was opened again. Workday's job page
    says "You applied for this job on <date>." over its View Application, and the desk stopped there, the job
    opened again three times in 8 minutes; an Eightfold application said it was applied to already, and the desk
    filled it all and offered Submit. Each is marked applied, nothing is filled, and it's never queued again."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url(f"site/{page}"), title="Equipment Technician", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            typed = await r.page.evaluate("() => [...document.querySelectorAll('input')].map((i) => i.value)")
            return r, typed
        finally:
            await applier.stop()

    r, typed = run(go())
    assert r.status == "submitted" and "applied to already" in r.reason.lower(), (r.status, r.reason, r.log)
    assert not any(typed), typed  # nothing filled in
    assert srv.tracker().get(job["id"])["status"] == "applied"
    with pytest.raises(ValueError):
        applier.enqueue(job["id"])  # never opened again by itself, or by Apply


def test_already_applied_wording():
    """What says a job was applied to already, and what doesn't (a sign-in's offer, a question on the form)."""
    from job_apply.pipeline import _applied_before

    said = {"You applied for this job on 10/09/2026.": True,
            "You have already applied for this position.": True,
            "You've already applied to this job. Track it in your candidate home.": True,
            "You have already submitted an application for this position.": True,
            "You have already submitted an application.": False,  # (to which job, it doesn't say)
            "If you have already applied, sign in to check your status.": False,
            "Already applied? Sign in to see your applications.": False,
            "Have you already applied for this position?": False,
            "Please indicate whether you have already applied for this position before.": False,
            "If you have already applied for this job, you don't need to apply again.": False,
            # (the review's: a posting's or a sign-in's boilerplate, and a limit on applications)
            "If you are an internal candidate or you have already applied for this position, please sign in.": False,
            "If you believe you have already applied for this position, please contact us.": False,
            "In case you have already applied for this job, please ignore this message.": False,
            "If\xa0you have already applied for this position, sign in.": False,
            "You have already applied. You may apply to 3 jobs every 30 days.": False,
            "Thank you.\nYou have already applied for this position.": True,
            "Equipment Technician\nYou applied for this job on 10/09/2026.": True,  # (under its heading)
            "If you are an internal candidate or\nyou have already applied for this position, sign in.": False}
    assert {t: bool(_applied_before({}, t)) for t in said} == said
    asked = {"fields": [{"label": "You have already applied for this position. Is that right?"}]}
    assert not _applied_before(asked, "You have already applied for this position. Is that right?")


def test_confirmation_wording():
    """What sites say once an application has gone: Eightfold's "Thanks for applying!", Oracle's
    "Thank you for your job application", iCIMS's "Thank you for submitting your application".
    And what never counts: a form sent back, a sign-in page's thanks, a sentence saying it didn't."""
    from job_apply.browser import confirmations

    for text in ("Thanks for applying!", "Thank you for your job application.", "Thank you for applying",
                 "Thank you for submitting your application to Example Fab.", "Application Submitted",
                 "Your application has been successfully submitted.", "You\u2019ve successfully applied for this job.",
                 "We\u2019ve received your application.", "Thank you for\napplying!", "Thank\xa0you for applying",
                 "Your application is in!"):
        assert confirmations(text), text
    for text in ("Submit your application", "Review your application before you submit it.",
                 "Applications are reviewed weekly.", "Your application is incomplete.",
                 "Your application is in progress.", "Your application is in draft.",
                 "No application was submitted. Please try again.",
                 "After your application has been submitted, you will get an email.",
                 "If you have successfully applied, sign in to check its status.",
                 "Thanks for your interest in Example Fab! Create an account to continue.",
                 "Application completeness: 80%"):
        assert not confirmations(text), text


def test_closed_posting_wording():
    """What closed postings say, and what they don't."""
    closed = pipeline._CLOSED.search
    assert closed("The job posting you are looking for has expired or the position has already been filled.")
    assert closed("This job is no longer available.")
    assert closed("We are no longer accepting applications for this position.")
    assert closed("Sorry, this position has been filled.")
    assert closed("The requisition is closed.")
    assert not closed("Applications close on Oct 30.")
    assert not closed("Your session has expired. Please sign in again.")
    assert not closed("This position is open to applicants in Arizona.")


def test_the_host_chromes_error_page_names_is_read():
    """Chrome's error page names the host in its text, and some versions in its title too."""
    said = pipeline.unreached_host
    assert said({}, "This site can\u2019t be reached\n127.0.0.1 refused to connect.") == "127.0.0.1"
    assert said({}, "The webpage at https://forgerock-ig-int.apps2.example.com/ig-nonce?jwt=x might be down") == \
        "forgerock-ig-int.apps2.example.com"
    assert said({}, "This site can\u2019t be reached\nCheck if there is a typo in sso.example.com.\n"
                    "sso.example.com\u2019s server IP address could not be found.") == "sso.example.com"
    assert said({"title": "forgerock-ig-int.apps2.example.com"}, "") == "forgerock-ig-int.apps2.example.com"
    assert said({"title": "Job Details"}, "") == ""


def test_a_greyed_out_next_names_the_required_boxes_still_empty(srv):
    """Phoenix Children's Next (live, Oct 2026) stays greyed out until its "I Agree" is ticked
    and a resume attached, and the page says nothing about it: the desk's "the site still
    wants something" now says what."""
    run(srv.open_application(url=fixture_url("site/qualification-table.html")))
    page = run(srv.browser.page())
    for row in (0, 1):
        run(page.check(f'input[name="Qual_Experience[{row}]"][value="Yes"]'))
    data = run(srv.browser.inspect(False))
    assert pipeline._greyed_step(data)
    assert pipeline._unanswered(data) == "“I Agree” isn't answered."
    run(page.check("#agree"))
    # its resume box isn't marked required, but Next waits on it
    assert pipeline._unanswered(run(srv.browser.inspect(False))) == "nothing is attached yet (your resume, say)."
    run(page.set_input_files("#resume", files=[{"name": "resume.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4"}]))
    data = run(srv.browser.inspect(False))
    assert not pipeline._greyed_step(data) and pipeline._unanswered(data) == ""


def _notes(home: Path) -> list[str]:
    folder = home / "notes"
    return [p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.md"))] if folder.is_dir() else []


def _paused_run(srv, need="stuck", reason="I couldn't find the button that moves this application on.",
                code="no_step_button", **kw):
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/x", title="FSE", company="Acme Fab")["job"]
    applier = Applier(srv)
    r = Run(job["id"], "FSE", "Acme Fab", status="running", url="https://acme.wd1.myworkdayjobs.com/External/job/x/apply",
            page_info={"url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply", "title": "Apply",
                       "headings": ["My Information"], "actions": ["Save and Continue"], "errors": [],
                       "fields": [{"label": "First Name", "kind": "text", "required": True, "empty": False}]})
    applier.runs[job["id"]] = r
    applier._pause(r, need, reason, code=code, **kw)
    return applier, r


def test_a_stop_the_desk_likely_got_wrong_is_noted_once(srv, job_apply_home, monkeypatch):
    """The owner told the developer about each stop with a Report a problem per job. A stop
    that's likely the desk's own is now noted with nothing pressed, once: the same job stopping
    the same way again isn't noted twice. In practice mode too, where the owner looks for them."""
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    applier, r = _paused_run(srv)
    assert (r.status, r.need) == ("needs_you", "stuck")
    [text] = _notes(job_apply_home)
    assert "I couldn't find the button that moves this application on." in text and "My Information" in text
    applier._pause(r, "stuck", "I couldn't find the button that moves this application on.", code="no_step_button")
    assert len(_notes(job_apply_home)) == 1
    applier._pause(r, "submit_failed", "I pressed Submit, but the form is still there.", code="submit_failed")
    assert len(_notes(job_apply_home)) == 2


@pytest.mark.parametrize("need", ["bot_check", "captcha", "email_code", "your_submit", "tailor", "questions"])
def test_a_stop_thats_the_persons_by_design_isnt_noted(srv, job_apply_home, need):
    """A bot check, a CAPTCHA, an emailed code, the person's own Submit, a tailored resume and
    questions the profile doesn't answer are theirs to do, not the desk getting it wrong."""
    applier, r = _paused_run(srv, need, "Over to you.",
                             questions=[{"id": "1", "label": "Why us?", "kind": "text", "required": True}])
    assert r.need == need and _notes(job_apply_home) == []
    # the same job stuck after it is noted
    applier._pause(r, "stuck", "The page didn't move on.", code="step_not_saved")
    assert len(_notes(job_apply_home)) == 1


def test_answers_that_didnt_go_in_are_noted_without_the_answer(srv, job_apply_home):
    """A questions stop is noted when a fill failed there: the desk picked an answer and the box
    didn't take it. What was picked is the person's, so it isn't in the note."""
    _paused_run(srv, "questions", "1 question(s) your profile doesn't answer.", questions=[
        {"id": "1", "label": "Are you a veteran?", "kind": "select", "options": ["Select One", "Yes", "No"],
         "required": True, "error": "ValueError: Picked 'I am a protected veteran' but the field shows 'Select One'"}])
    [text] = _notes(job_apply_home)
    assert "- Are you a veteran? (select, 3 choices): ValueError: Picked … but the field shows …" in text
    assert "protected veteran" not in text


def test_a_note_that_cant_be_taken_doesnt_stop_the_pause(srv, job_apply_home, monkeypatch):
    def broken(job, run, profile=None, hide=()):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline.report, "take_note", broken)
    applier, r = _paused_run(srv)
    assert (r.status, r.need, r.log[-1]) == ("needs_you", "stuck", r.reason)
    assert _notes(job_apply_home) == []


def test_a_live_runs_stops_are_noted_without_the_persons_answers(srv, monkeypatch, job_apply_home):
    """Through the desk's own run: a page that turns the browser away is noted; questions the
    profile doesn't answer aren't, until an answer the person gave doesn't go in, and then the
    note says why without the answer."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    turned_away = srv.add_job(url=fixture_url("site/forbidden.html"), title="Analyst", company="Example Health")["job"]
    asks = srv.add_job(url=fixture_url("site/sf-select-form.html"), title="ET", company="Example Semi")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            first = applier.enqueue(turned_away["id"])
            await until(lambda: first.status not in ("queued", "running"), about=state(first))
            assert first.need == "stuck", first.reason
            r = applier.enqueue(asks["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            assert r.need == "questions", (r.reason, r.log)
            assert len(_notes(job_apply_home)) == 1  # questions the profile doesn't answer: not noted
            r.once[question_key("Preferred Locale/Language")] = "Klingon"
            applier.enqueue(asks["id"], front=True)
            await until(lambda: any("didn't go in" in line for line in r.log) and r.status not in ("queued", "running"),
                        about=state(r))
            assert r.need == "questions" and "Klingon" in r.questions[0]["error"], (r.reason, r.questions)
        finally:
            await applier.stop()

    run(go())
    forbidden, answers = _notes(job_apply_home)
    assert "403 Forbidden" in forbidden and "## stuck: Analyst, at an employer with its own careers site" in forbidden
    assert "Example Health" not in forbidden
    assert "- Preferred Locale/Language (" in answers and "Klingon" not in answers and "Example Semi" not in answers


@pytest.mark.slow
def test_the_tabs_of_jobs_that_went_in_are_closed_but_the_newest_few(srv, monkeypatch):
    """One tab per job adds up over a long queue: in a long live run (Oct 2026) the desk's
    Chrome crashed with every job's tab still open. A job that went in keeps its tab (its
    confirmation, for the person to see) only while it's among the newest DONE_TABS_KEPT; a
    job waiting on the person keeps its tab, with the work in it, however old."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "DONE_TABS_KEPT", 2, raising=False)
    urls = [f"https://careers.acme-fab.example/apply/{n}" for n in range(6)]
    pages = {u: _form("Acme Fab") for u in urls[1:]}
    pages[urls[0]] = _form("Acme Fab", "Do you hold an active TS/SCI clearance?")  # waits on the person
    posts: list[str] = []
    jobs = [srv.add_job(url=u, title=f"Technician {n}", company="Acme Fab")["job"] for n, u in enumerate(urls)]
    applier = Applier(srv)
    applier.auto_submit = True

    async def go():
        await _serve(srv, pages, posts)
        applier.start()
        try:
            runs = [applier.enqueue(j["id"], submit=True) for j in jobs]
            states = [state(r) for r in runs]
            # (a callable, as until() calls it: a list raised TypeError and lost the diagnostic; and the time a six-job
            # queue takes on Windows' runner: the review of Oct 15)
            await until(lambda: all(r.status not in ("queued", "running") for r in runs) and applier.current is None,
                        timeout=180, about=lambda: [s() for s in states])
            await asyncio.sleep(0.5)
            return runs, [r.page is not None and not r.page.is_closed() for r in runs]
        finally:
            await applier.stop()

    runs, open_ = run(go())
    assert [r.status for r in runs] == ["needs_you"] + ["submitted"] * 5, [(r.status, r.reason) for r in runs]
    assert open_ == [True, False, False, False, True, True], open_


@pytest.mark.own_browser  # (it crashes a tab: now and then Chromium goes down whole with it)
def test_a_paused_jobs_tab_that_crashes_doesnt_hold_the_queue(srv, monkeypatch):
    """A job holding the queue for the person's sign-in, whose tab crashes ("Aw, Snap!"): a
    crashed tab isn't closed to Playwright, so the desk went on reading it and holding the
    queue for HANDS_ON_IDLE. Like a closed tab, it's opened again: there's nothing left in it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you")
            assert r.need == "sign_in" and r.blocking, r.reason
            await _crash(r.page)
            await until(lambda: any("its tab crashed" in line for line in r.log), timeout=20, about=state(r))
            await until(lambda: r.status == "needs_you", about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "sign_in" and not srv.browser.lost(r.page), (r.status, r.reason, r.log)


def test_a_stops_page_info_holds_a_whole_workday_experience_page():
    """Four jobs and two schools on a Workday site's My Experience page come to 70 boxes and
    more: a report that kept only the first 40 showed the last blocks as never read."""
    fields = [{"label": f"Box {n}", "kind": "text", "section": f"Work Experience {n // 11 + 1}", "value": ""}
              for n in range(80)]
    actions = [{"text": f"Delete {n}"} for n in range(45)]
    info = pipeline._page_info({"url": "https://x.example", "fields": fields, "actions": actions})
    assert len(info["fields"]) == 80 and info["fields"][-1]["section"] == "Work Experience 8"
    assert len(info["actions"]) == 45


def test_a_tab_another_job_still_has_is_never_closed_with_a_finished_job(srv, monkeypatch, loop):
    """A finished job's tabs are closed with the tab that opened its application, but never one
    another job still has: a waiting job's work would go with it."""
    monkeypatch.setattr(pipeline, "DONE_TABS_KEPT", 0)
    applier = Applier(srv)

    async def go():
        shared = await srv.browser.new_tab()
        await shared.goto(fixture_url("site/posting.html"))
        async with shared.expect_popup() as opened:
            await shared.evaluate("u => { window.open(u) }", fixture_url("site/step1.html"))
        waiting_tab = await opened.value
        applier.runs[1] = Run(1, status="submitted", page=shared, updated=1.0)
        applier.runs[2] = Run(2, status="needs_you", page=waiting_tab, updated=2.0)
        await applier._close_done_tabs()
        return shared.is_closed(), waiting_tab.is_closed(), applier.runs[1].page

    shared_closed, waiting_closed, done_page = run(go())
    assert not shared_closed and not waiting_closed and done_page is None


def test_an_email_the_site_has_no_account_for_is_said_so_with_its_way_to_make_one(srv, monkeypatch):
    """An Eightfold site's sign-in takes the email first. With no account for it, it says "We
    don't recognize this email. Create a new account" and stays put: the desk stopped with
    "“Email” is marked invalid. Fix it" (live, Oct 2026). It's the person's account to make
    (the desk holds no password for that system), and the card says so, naming the button."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/email-first-signin.html"), title="HR Generalist",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "sign_in"), (r.status, r.need, r.reason, r.log)
    assert "no account for your email" in r.reason and "“Create an account”" in r.reason, r.reason
    assert "We don't recognize this email" in r.reason and "marked invalid" not in r.reason


EIGHTFOLD = "site/eightfold-create-account.html"


def inbox_with_code(monkeypatch, codes=None, spam=False):
    """An email app password saved, and an inbox whose sign-up code is 123456 (`codes`: each
    kind asked for, in turn; `spam`: it's in the Spam folder)."""
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        if codes is not None:
            codes.append(want)
        found = pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time())
        found.spam = spam
        return found if want == "code" else None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)


@pytest.mark.parametrize("identity", [False, True], ids=["manage_accounts", "test-identity"])
def test_with_manage_accounts_an_email_first_site_makes_the_account_with_the_emailed_code(srv, monkeypatch,
                                                                                        job_apply_home, identity):
    """Northrop Grumman's Eightfold site (the live check's test identity, Oct 2026): its sign-in said
    "We don't recognize this email. Create a new account", and the desk stopped for the person to make
    the account, manage_accounts on. Now it makes it: "Create an account", the Privacy Policy box
    (the site's terms: nothing else is ticked) and its Submit, the email again and Continue, then the
    emailed code from the inbox and the code step's Submit, and it carries on with the application.
    No password goes on the site (Eightfold's asks none)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    if identity:
        as_test_identity(monkeypatch, job_apply_home)
    else:
        manage_accounts(job_apply_home)
    asked: list[str] = []
    inbox_with_code(monkeypatch, asked)
    job = srv.add_job(url=fixture_url(EIGHTFOLD), title="Financial Analyst", company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "questions" or r.status == "ready", about=state(r))  # nobody touched it
            return r, await r.page.evaluate("() => sessionStorage.getItem('account')")
        finally:
            await applier.stop()

    r, account = run(go())
    assert account == "sam.rivera@example.com" and "/site/" in r.url and "eightfold" not in r.url, (account, r.url, r.log)
    log = "\n".join(r.log)
    assert "has no account for your email, so I pressed “Create an account” to make one" in log, log
    assert "ticked “I have read and agree to the Privacy Policy” to make your account" in log, log
    assert "your email for the new account and pressed “Continue”" in log, log
    assert "entered the code from your email (sent from careers.example.com)" in log and "pressed “Submit”" in log, log
    assert any(line.startswith("created your account on") and "the code it emailed you" in line for line in r.log), log
    assert any(line.startswith("filled") for line in r.log) and asked and set(asked) == {"code"}, (asked, log)
    assert r.status != "submitted", (r.status, r.reason)


@pytest.mark.parametrize("words, pressed", [("Check your email. We sent a code to your email.", True),
                                            ("Enter the code we emailed you to submit your application.", False)])
def test_a_code_steps_submit_is_never_pressed_where_it_may_send_the_application(srv, words, pressed):
    """A step holding only an emailed code's boxes has its Submit pressed after the code ("Check your
    email"), but never one whose own words speak of an application ("Enter the code we emailed you to
    submit your application"): that Submit may send it, which stays the person's. The page behind a
    sign-in pop-up (an application's) doesn't count."""
    run(srv.open_application(url=fixture_url("jsonld_posting.html")))
    page = run(srv.browser.page())
    run(page.set_content(f"""<h1>Field Service Engineer: apply now</h1><p>Your application</p>
      <div role="dialog"><p>{words}</p><label for="c">Verification code</label><input id="c">
        <button type="button">Submit</button></div>"""))
    data = run(srv.inspect_form(include_dropdown_options=False))
    press = run(Applier(srv)._code_only_submit(data))
    assert (press is not None and press["text"] == "Submit") is pressed, (press, data["actions"])


def test_an_emailed_code_that_signs_in_gets_its_submit_pressed(srv, monkeypatch, job_apply_home):
    """Northrop Grumman's Eightfold sign-in, for an email that already has an account (live, Oct 2026):
    "Check your email", six "Please enter OTP character" boxes and a "Submit" that sends the code and
    nothing else. The desk entered the code and stopped there ("Press the page's button to carry on").
    Now a page holding only the code's boxes has its Submit pressed after the code, and the desk goes on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    manage_accounts(job_apply_home)
    inbox_with_code(monkeypatch)
    job = srv.add_job(url=fixture_url(EIGHTFOLD) + "?known", title="Financial Analyst", company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "questions" or r.status == "ready", about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    log = "\n".join(r.log)
    assert "/site/" in r.url and "eightfold" not in r.url, (r.url, log)
    assert "entered the code from your email" in log and "pressed “Submit”" in log, log
    assert not any(line.startswith("created your account") for line in r.log), log
    assert any(line.startswith("filled") for line in r.log), log


@pytest.mark.parametrize("contact", ["required", "optional", "required&late"])
def test_with_manage_accounts_a_required_choice_to_be_considered_for_other_jobs_is_given(srv, monkeypatch,
                                                                                       job_apply_home, contact):
    """Northrop Grumman's agreement step also asks for its "Contact Consent": "I understand that I may be
    considered for other open positions in my country", and won't go on without it ("You must select
    one option"; not marked required). The owner's call (Oct 10): a consent like that is given for the
    person when the site won't make the account without it. The desk ticks the Privacy Policy box and
    presses Submit; refused, it chooses the consent, says so, presses Submit again and makes the account
    with the email and the emailed code. Where the site goes on without it, it's never chosen. Live, its
    Submit was drawn a moment after its boxes, and the desk said it couldn't find the button (late)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    manage_accounts(job_apply_home)
    inbox_with_code(monkeypatch)
    job = srv.add_job(url=fixture_url(EIGHTFOLD) + f"?contact={contact}", title="Financial Analyst",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need == "questions" or r.status == "ready", about=state(r))  # nobody touched it
            return r, await r.page.evaluate("() => sessionStorage.getItem('account')")
        finally:
            await applier.stop()

    r, account = run(go())
    assert account == "sam.rivera@example.com" and "/site/" in r.url and "eightfold" not in r.url, (account, r.url, r.log)
    log = "\n".join(r.log)
    assert "ticked “I have read and agree to the Privacy Policy” to make your account" in log, log
    chose = [line for line in r.log if line.startswith("chose “Contact Consent: I understand that I may be considered "
                                                        "for other open positions")]
    if contact.startswith("required"):
        assert len(chose) == 1 and "as it wouldn't go on without it (settings.manage_accounts)" in chose[0], log
    else:
        assert not chose, log
    assert any(line.startswith("created your account on") for line in r.log), log


def test_a_refused_agreement_with_another_choice_left_never_gives_the_consent_for_it(srv, monkeypatch, job_apply_home):
    """The agreement step refused ("You must select one option"), and besides the consent to be considered
    for other open positions it has another choice left empty, one that's the person's. The refusal may be
    about that one: the consent isn't given on it, and the step is theirs, naming both."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    manage_accounts(job_apply_home)
    inbox_with_code(monkeypatch)
    job = srv.add_job(url=fixture_url(EIGHTFOLD) + "?contact=required&question", title="Financial Analyst",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => document.getElementById('contact-consent-choice-0').checked")
        finally:
            await applier.stop()

    r, consented = run(go())
    assert (r.status, r.need, consented) == ("needs_you", "sign_in", False), (r.status, r.reason, r.log)
    assert not any(line.startswith("chose “Contact Consent") for line in r.log), r.log
    assert "Preferred contact method" in r.reason and "the desk never chooses for you" in r.reason, r.reason


def test_an_account_forms_boxes_and_steps_never_take_in_a_sign_up(job_apply_home):
    """What an account form's boxes and steps are told from: a privacy box that also has the person
    considered for other open positions isn't a plain terms box; a page asking only for an email is an
    account's first step only under an account heading, never a talent community's; and a "new candidate,
    not a current employee" box is ticked only where the profile says where the person works."""
    import yaml

    both = {"id": "b", "kind": "checkbox", "label": "I agree to the Privacy Policy and understand I may be considered "
            "for other open positions", "value": False}
    assert pipeline._terms_boxes({"fields": [both]}) == []
    email = {"id": "e", "kind": "text", "label": "Email address", "value": ""}
    assert pipeline._email_first_account({"headings": ["Let's Get Started", "Account Information"], "fields": [email]})
    assert not pipeline._email_first_account({"headings": ["Join our Talent Community"], "fields": [email]})
    assert not pipeline._email_first_account({"headings": ["Get job alerts"], "fields": [email]})
    new = {"id": "n", "kind": "checkbox", "label": "Yes, I am a new candidate and not a current employee", "value": False}
    assert pipeline._new_candidate_boxes({"fields": [new]}, "Example Health") == [new]
    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    profile.pop("work_history", None)
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")
    assert pipeline._new_candidate_boxes({"fields": [new]}, "Example Health") == []


def test_a_consent_to_other_positions_is_given_only_on_its_own():
    """What counts as a consent to be considered for other open positions: on its own, as a box or a
    group's one choice. Never one that also signs the person up for job alerts, a newsletter or a
    talent pool, nor a group asking yes or no."""
    said = "I understand that I may be considered for other open positions in my country"
    assert pipeline._other_positions_consent({"kind": "checkbox", "label": said}) is True
    assert pipeline._other_positions_consent({"kind": "radio_group", "label": "Contact Consent", "options": [said]}) == said
    assert pipeline._other_positions_consent({"kind": "checkbox_group", "label": "", "options": [said]}) == [said]
    for label in [said + " and to receive job alerts", said + " and to join the talent community",
                  "Consider me for other opportunities and send me the newsletter", "I agree to the privacy policy"]:
        assert pipeline._other_positions_consent({"kind": "checkbox", "label": label}) is None, label
    assert pipeline._other_positions_consent({"kind": "radio_group", "label": "May we consider you for other positions?",
                                              "options": ["Yes", "No"]}) is None


def test_without_manage_accounts_an_email_first_sites_account_steps_stay_the_persons(srv, monkeypatch):
    """manage_accounts off: the desk stops at "We don't recognize this email", and the account's
    steps after it (the agreement, the email again, the code) are the person's: none is read as the
    application (its agreement's Submit taken for the review page's), nor is anything pressed in
    them. The desk carries on once the application shows."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url(EIGHTFOLD), title="Financial Analyst", company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            first = (r.status, r.need, r.reason)
            page = r.page
            await page.click("#switch")  # the person: Create an account
            await asyncio.sleep(2)  # (several of the desk's looks)
            on_consent = (r.status, r.need, await page.evaluate("() => document.getElementById('consent-checkbox')"
                                                                 ".checked"))
            await page.check("#consent-checkbox")
            await page.click("#agree")
            await asyncio.sleep(2)
            on_email = (r.status, r.need, await page.locator("#head").inner_text())
            await page.click("#go")
            await page.wait_for_selector(".digit")
            await asyncio.sleep(2)
            on_code = (r.status, r.need)
            await page.fill(".digit >> nth=0", "123456")
            await page.click("#confirm")
            await until(lambda: r.need == "questions" or r.status == "ready", about=state(r))
            return r, first, on_consent, on_email, on_code
        finally:
            await applier.stop()

    r, first, on_consent, on_email, on_code = run(go())
    assert first[:2] == ("needs_you", "sign_in") and "no account for your email" in first[2], first
    assert "“Create an account”" in first[2] and "We don't recognize this email" in first[2], first
    assert on_consent == ("needs_you", "sign_in", False), on_consent  # not ticked, nor its Submit pressed
    assert on_email == ("needs_you", "sign_in", "Create an account"), on_email  # nor its Continue
    assert on_code == ("needs_you", "sign_in"), on_code
    assert "eightfold" not in r.url and any("“My Information”" in line for line in r.log), (r.url, r.log)
    assert not any(line.startswith("created your account") for line in r.log), r.log


def test_a_job_sent_to_a_maintenance_page_is_said_down_for_maintenance(srv, monkeypatch):
    """A careers site down for its maintenance sends every job to its maintenance page ("We'll
    be back.", live, Oct 2026): the desk said it couldn't find the button that moves the
    application on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme-fab.example/job/1"
    job = srv.add_job(url=url, title="Field Service Engineer", company="Acme Fab")["job"]
    applier = Applier(srv)

    async def go():
        async def handler(route):
            if route.request.url.split("?")[0] == url:  # (sent on as the site's own script does)
                await route.fulfill(status=200, content_type="text/html", body="<html><head><script>location.replace("
                                    "'https://www.acme-fab.example/en/maintenance')</script></head></html>")
            else:
                await route.fulfill(status=200, content_type="text/html",
                                    body="<html><body><h1>We'll be back.</h1><p>Our site is being updated.</p>"
                                         "<a href='/careers'>Careers</a></body></html>")

        await srv.browser.page()
        await srv.browser._ctx.route("https://**.example/**", handler)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.need == "stuck" and "down for maintenance" in r.reason, (r.status, r.need, r.reason, r.log)
    assert "https://www.acme-fab.example/en/maintenance" in r.reason


@pytest.mark.parametrize("tab_url, boxes, gone", [
    ("https://careers.example-corp.example/careers?pid=123&domain=example-corp.com", 0, False),  # the posting
    ("https://careers.example-corp.example/careers/apply?pid=123", 5, False),  # its application, where it was left
    ("https://careers.example-corp.example/careers?pid=999&domain=example-corp.com", 0, True),  # another job's posting
    ("https://careers.example-corp.example/careers/apply?pid=999", 5, True),  # another job's application
    ("https://careers.example-corp.example/careers?query=technician", 1, True),  # the careers home's search
    # its application, the site's script having rewritten its address without the query (a single-page site)
    ("https://careers.example-corp.example/careers/apply", 5, False),
    # the careers home, its logo pressed: the posting's address without its id, and no application on it
    ("https://careers.example-corp.example/careers", 1, True),
])
def test_a_tab_on_another_job_named_in_the_query_isnt_carried_on_with(srv, monkeypatch, tab_url, boxes, gone):
    """An Eightfold site names the job in its address's query (/careers?pid=123). A paused job's
    tab taken to ?pid=999 is another job's posting, and /careers with no id its careers home:
    compared without their queries, both read as this job's own page, and the desk would have
    applied to job 999 as job 123."""
    from types import SimpleNamespace

    job = srv.add_job(url="https://careers.example-corp.example/careers?pid=123&domain=example-corp.com",
                      title="Field Service Engineer", company="Example Corp")["job"]
    applier = Applier(srv)
    left = Run(job["id"], url="https://careers.example-corp.example/careers/apply?pid=123")

    async def peek(tab):
        return {"fields": [{"label": f"Box {n}", "kind": "text"} for n in range(boxes)]}, "Careers at Example Corp"

    monkeypatch.setattr(srv.browser, "peek", peek)
    assert run(applier._gone_home(left, SimpleNamespace(url=tab_url))) is gone


def test_a_word_under_a_job_key_in_the_address_isnt_a_job_id(srv, monkeypatch):
    """"job" names a job in a Taleo site's query (?job=12345), but a site may name its application's
    steps with it (?job=apply, then ?job=questions). Taken for job ids, the next step was another job's
    page, and the job was opened again in a new tab, its form lost. A job's id has a digit."""
    from types import SimpleNamespace

    assert pipeline._job_ids("https://jobs.acme.example/apply?job=apply&pid=123&reqId=R-0042") == {
        "pid": "123", "reqid": "R-0042"}
    job = srv.add_job(url="https://jobs.acme.example/position/4512", title="Field Service Engineer",
                      company="Acme")["job"]
    applier = Applier(srv)
    left = Run(job["id"], url="https://jobs.acme.example/position/4512/apply?job=apply")

    async def peek(tab):
        return {}, "Careers at Acme"

    monkeypatch.setattr(srv.browser, "peek", peek)
    tab = SimpleNamespace(url="https://jobs.acme.example/position/4512/apply?job=questions")
    assert run(applier._gone_home(left, tab)) is False


def test_an_apply_link_with_its_address_in_data_href_is_pressed(srv, monkeypatch):
    """A careers site's "Apply Now" (KPMG's, live, Oct 2026) is a link with no href: its address is
    in data-href, and the site's script goes there on a click. The form reader only counted links
    with an href, so the desk found no way into the application."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/data-href-posting.html"), title="Senior Associate, Tax",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "clicked “Apply Now”" in r.log, (r.status, r.need, r.reason, r.log)
    assert any(line.startswith("filled") for line in r.log), r.log


def test_what_a_filled_page_still_needs_is_said_the_same_way_on_each_path(srv):
    """The pause for what a filled page still needs was written out twice (an application page, and
    one that makes the account as it applies) and had drifted: on the second, a missing file's card
    ran on into the next sentence with no full stop. One helper says it for both."""
    applier = Applier(srv)
    run_ = Run(1)
    unfilled = {"label": "Country of Residence", "kind": "combobox", "error": "TimeoutError: Locator.evaluate"}
    assert applier._stop_for_what_is_left(run_, "", [unfilled], [{"label": "Resume/CV*"}])
    assert run_.need == "stuck" and run_.reason.startswith(
        "The form wants a file (Resume/CV*) and the profile doesn't say where yours is: ask Claude to set "
        "documents.resume (or documents.cover_letter) in profile.yaml, then press Carry on. "), run_.reason
    nothing = Run(2)
    assert not applier._stop_for_what_is_left(nothing, "", [], []) and nothing.status != "needs_you"


def test_a_button_gone_as_it_was_clicked_is_looked_at_again(srv, monkeypatch):
    """APS's SuccessFactors sign-in (live run 17, Oct 11): a button went from the page as it was clicked, with
    nothing like it to find ("KeyError: Nothing clickable matches 'ewr4'"), and the job failed with "Something
    went wrong". The job is driven once more, from its tab as it is."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/ukg-prefill.html"), title="Field Service Engineer",
                      company="Example Fab")["job"]
    applier = Applier(srv)
    drive = Applier._drive
    calls = []

    async def redrawn(self, run):
        calls.append(run.job_id)
        if len(calls) == 1:
            raise KeyError("Nothing clickable matches 'ewr4'; call inspect_form for ids")
        await drive(self, run)

    monkeypatch.setattr(Applier, "_drive", redrawn)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert len(calls) == 2
    assert "review.html" in r.url, (r.url, r.reason, r.log)


def test_a_sign_in_that_led_elsewhere_is_told_from_an_email_first_one():
    """The review's cases (0.3.117): a sign-in page opened again that led to a job search is one with no sign-in
    (Edwards'); an email-first sign-in's step (an email box and Next, no password) at another address isn't, nor is
    the same address, nor a page with a way to an account."""
    r = Run(1, "Engineer", "Example", url="https://example.com/job/1")
    r.reset_from = "https://career5.successfactors.eu/career?_s.crb=abc"

    class Page:
        url = "https://career5.successfactors.eu/careers?company=atlascopcoP"
    r.page = Page()
    search = {"fields": [{"kind": "text", "label": "Keywords:"}, {"kind": "text", "label": "Requisition ID:"}],
              "actions": [{"text": "Sign In"}, {"text": "Search Jobs"}, {"text": "Clear"}]}
    assert pipeline._no_sign_in_here(r, search)
    # as it is live (the nightly of Oct 12): its filters too, enough boxes to read as an application's
    filters = {**search, "fields": [*search["fields"], *[{"kind": "text", "label": "Select one or more options"}] * 7,
                                    {"kind": "checkbox", "label": "Exact Match"}]}
    assert pipeline._application_like(filters) and pipeline._no_sign_in_here(r, filters)
    # the review's: an application with a job search in its header is still one
    applying = {**filters, "fields": [*filters["fields"], {"kind": "text", "label": "First Name"}]}
    assert not pipeline._no_sign_in_here(r, applying)
    email_first = {"fields": [{"kind": "text", "label": "Email Address"}], "actions": [{"text": "Next"}]}
    assert not pipeline._no_sign_in_here(r, email_first)
    with_account = {**search, "actions": [*search["actions"], {"text": "Create Account"}]}
    assert not pipeline._no_sign_in_here(r, with_account)
    Page.url = "https://career5.successfactors.eu/career?_s.crb=xyz"
    spent = {"fields": [], "actions": [{"text": "Back"}]}
    assert not pipeline._no_sign_in_here(r, spent)  # (the same page: said by the other cases)
    # a job search at the sign-in's own address (Edwards', the probe of Oct 12: careers?company=atlascopcoP both)
    r.reset_from = Page.url = "https://career5.successfactors.eu/careers?company=atlascopcoP"
    assert pipeline._no_sign_in_here(r, filters)
    assert not pipeline._no_sign_in_here(r, {**filters, "fields": [*filters["fields"],
                                                                   {"kind": "password", "label": "Password"}]})



@pytest.mark.parametrize("label, ticked", [
    ("By checking this box, I agree to create a candidate home account in Workday.", True),  # GCU's
    ("I agree to creating a Arizona State University Candidate Home Account.", True),
    ("I agree to creating this account to allow me to apply for positions with HonorHealth", True),
    ("I agree", True),  # Northern Trust's
    ("I consent", True),  # Entegris'
    ("I confirm Hitachi will process my personal data as per the GDPR Notice for Candidates", True),
    ("You acknowledge having read the documents above. Your agreement is valid for 24 months.", True),  # Air Liquide's
    ("Yes, I have read and understood.", True),  # Avnet's (the words above it read too: _said_terms)
    ("I agree to receive job alerts and the newsletter", False),
    ("I agree to create an account and be contacted about other open positions", False),
    ("I agree to join the talent community", False),
    ("I am at least 18 years old", False),
    # the review's: an agreement that takes in more than the account
    ("I agree that my account and data can be shared with partners", False),
    ("I agree to receive emails about my account", False),
    ("I agree to create an account and receive updates", False),
    ("I consent to receive text updates about my account", False),
    ("I agree to the processing of my personal data for recruitment and for future vacancies", False),
    ("I consent to the GDPR retention of my data in the talent pool", False),
    # the review of Oct 15: a legal waiver, the terms of employment or a fact about the person beside the terms
    ("I agree to the terms of use and to binding arbitration of any dispute", False),
    ("I acknowledge that my employment would be at-will and agree to the site's terms", False),
    ("I agree to the terms and confirm I am authorized to work in the United States", False),
    # (its review: an agreement's own asides claim nothing; an age the profile answers is ticked: Microchip's Workday)
    ("I consent to the processing of my personal data as described in the Privacy Policy. I am aware that I can "
     "withdraw my consent at any time.", True),
    ("I agree to the terms and conditions and I understand that I am providing my information voluntarily", True),
    ("I agree that I have read the data privacy statement and I have a copy of it", True),
    ("I am 16 years or older, and I have read the Microchip Recruitment Privacy Notice.", True),
])
def test_a_workday_create_accounts_own_agreement_is_its_terms(label, ticked):
    """The first nightly at every employer (Oct 11): nine Workday Create Account forms stopped on a box of their
    own, never marked required, worded other than "terms": the account itself, a bare "I agree" or "I consent",
    a GDPR notice, the documents above read. Those are the account's terms (settings.manage_accounts ticks them);
    never one that also signs the person up for anything (_NOT_TERMS), nor one saying something else."""
    box = {"id": "t", "kind": "checkbox", "label": label, "value": False}
    assert (pipeline._terms_boxes({"fields": [box]}) == [box]) is ticked


def test_a_terms_gate_on_a_page_that_waives_a_right_is_the_persons():
    """The review of Oct 15: _terms_gate read the gate's button, headings and title, never the page's words, so an
    "I Accept" under a "Privacy Agreement" heading was pressed on a page whose text also binds the person to
    arbitration. The page's words are read for a waiver."""
    gate = {"id": "b1", "text": "I Accept"}
    page = {"headings": ["Privacy Agreement"], "title": "Apply"}
    words = "Please read our privacy agreement. We keep the details you give us for recruiting."
    assert pipeline._terms_gate(gate, page, words)
    assert not pipeline._terms_gate(gate, page, words + " By accepting you agree to binding arbitration of any dispute "
                                                        "and waive your right to a jury trial.")


def test_a_bare_i_agree_is_ticked_only_where_the_words_above_it_are_the_sites_terms(srv, tmp_path):
    """The safety audit (Oct 12): a Create Account form's bare "I agree" was ticked by its label alone, whatever the
    paragraph above it asked (joining a talent network, texts). Its words above are read: the site's terms, ticked;
    anything more, the person's."""
    page = tmp_path / "account.html"
    page.write_text('<!doctype html><h2>Create Account</h2><label for="e">Email Address</label><input id="e" type="email">'
                    '<label for="pw">Password</label><input id="pw" type="password">'
                    '<div><p>Please review and accept our Terms of Use and Candidate Privacy Policy.</p>'
                    '<label><input type="checkbox" id="a"> I agree</label></div>'
                    '<div><p>Join our talent network to hear about new jobs and events by email.</p>'
                    '<label><input type="checkbox" id="b"> I agree</label></div>'
                    # the review's: words beside it, not in an element of their own; and a terms paragraph that names
                    # an email in passing
                    '<div>Sign me up for job alerts and our newsletter.<br><label><input type="checkbox" id="c"> I agree'
                    '</label></div>'
                    '<div><p>Accept our Terms of Use. You will receive an email to verify your account.</p>'
                    '<label><input type="checkbox" id="d"> I agree</label></div>'
                    '<div><p>Get special offers and updates by email.</p><label><input type="checkbox" id="e"> I agree'
                    '</label></div>'
                    # Workday's (Entegris', the full run of Oct 12): the box six wrappers down from its words
                    '<div><div><div><p>In order to proceed, you must consent to our <a href="#">Privacy Policy</a>.</p>'
                    '</div></div></div><div><div><div><div><div><div><input id="f" type="checkbox"><span></span></div>'
                    '<label for="f">I consent</label></div></div></div></div></div>'
                    # the review's: a short sign-up heading just above, a terms sentence further up
                    '<div><p>In order to proceed, you must consent to our Privacy Policy.</p><div><div>Job alerts</div>'
                    '<label><input type="checkbox" id="g"> I agree</label></div></div>'
                    # Air Liquide's Workday (live run 41, Oct 12): "the documents above", and how long that lasts;
                    # the same box under a talent community's words is the person's
                    '<div><p>By creating an account, you recognize that Air Liquide will process your personal data in '
                    'accordance with the data privacy notice applicable to the job application process.</p>'
                    '<label><input type="checkbox" id="h"> You acknowledge having read the documents above. Your '
                    'agreement is valid for any future visits.</label></div>'
                    '<div><p>Join our talent community to hear about future openings.</p>'
                    '<label><input type="checkbox" id="i"> You acknowledge having read the documents above. Your '
                    'agreement is valid for any future visits.</label></div>'
                    # the review's: a sign-up above in other words (a mailing list, being notified, opting in)
                    '<div><p>Join our mailing list to hear about new openings.</p>'
                    '<label><input type="checkbox" id="k"> You acknowledge having read the documents above. Your '
                    'agreement is valid for any future visits.</label></div>'
                    '<div><p>Opt in to be notified when new roles open.</p>'
                    '<label><input type="checkbox" id="l"> I agree</label></div>'
                    '<div><p>We will email you about roles like this one.</p>'
                    '<label><input type="checkbox" id="m"> I agree</label></div>'
                    # never a box whose own words ask for more than the terms, however long that lasts (required, and
                    # naming the terms: left for its own words alone)
                    '<label><input type="checkbox" id="j" required> I agree to the terms and to receive future job '
                    'updates by email. Your agreement is valid for any future visits.</label>'
                    '<button type="submit">Create Account</button>', encoding="utf-8")
    run(srv.browser.goto(page.as_uri()))
    data = run(srv.browser.inspect(include_dropdown_options=False))
    boxes = pipeline._terms_boxes(data)
    assert len(boxes) == 12, data["fields"]  # (by their labels alone, all but the last)
    kept = run(Applier(srv)._said_terms(boxes))
    assert [b["id"] for b in kept] == [boxes[0]["id"], boxes[3]["id"], boxes[5]["id"], boxes[7]["id"]], (boxes, kept)


def test_an_agreement_box_is_ticked_only_where_the_words_above_it_name_the_sites_terms(srv, tmp_path):
    """The review of 0.3.133's: a bare "I agree" was ticked whenever the words above it named no sign-up the desk
    knew, though they named no terms either ("Join our Careers Community", "Sign up for career alerts", a heading
    "Email updates" under the password). Its words must name the site's terms, privacy statement or its processing of
    personal data, and never attest or sign the person up (texts and calls among them)."""
    page = tmp_path / "account.html"
    page.write_text('<!doctype html><h2>Create Account</h2><form><label for="e">Email Address</label>'
                    '<input id="e" type="email"><label for="pw">Password</label><input id="pw" type="password">'
                    '<div>Email updates</div><label><input type="checkbox" id="a"> I agree</label></form>'
                    '<div><p>Join our Careers Community to hear about new roles at Acme.</p>'
                    '<label><input type="checkbox" id="b"> I agree</label></div>'
                    '<div><p>Sign up for career alerts.</p><label><input type="checkbox" id="c"> I consent</label></div>'
                    '<div><p>I agree to receive recruiting texts and calls from Acme, as our Privacy Policy describes.</p>'
                    '<label><input type="checkbox" id="d"> I agree</label></div>'
                    '<div><p>I certify that my answers are true and complete. See our Privacy Policy.</p>'
                    '<label><input type="checkbox" id="f"> I agree</label></div>'
                    '<div><p>Acme processes your personal data to consider your application.</p>'
                    '<label><input type="checkbox" id="g"> I agree</label></div>'
                    '<button type="submit">Create Account</button>', encoding="utf-8")
    run(srv.browser.goto(page.as_uri()))
    data = run(srv.browser.inspect(include_dropdown_options=False))
    boxes = pipeline._terms_boxes(data)
    assert len(boxes) == 6, data["fields"]
    kept = run(Applier(srv)._said_terms(boxes))
    assert [b["id"] for b in kept] == [boxes[5]["id"]], (boxes, kept)


def test_a_read_and_understood_box_is_ticked_only_under_the_sites_privacy_statement(srv, tmp_path):
    """Avnet's Workday Create Account (run 44, Oct 13): its box says only "Yes, I have read and understood.", under
    the paragraph that names its Applicant Data Protection and Privacy Statement, and no account was made without it.
    What it was read and understood is above it: the site's terms or privacy statement, ticked; anything else (a
    sign-up, an attestation), the person's."""
    page = tmp_path / "account.html"
    page.write_text('<!doctype html><p>By using this site you agree to our Terms of Use.</p><h2>Create Account</h2>'
                    '<form><label for="e">Email Address</label><input id="e" type="email">'
                    '<label for="pw">Password</label><input id="pw" type="password">'
                    # the review of 0.3.133's: the words above the form's email and password aren't this box's (its
                    # paragraph is below it)
                    '<div><label><input type="checkbox" id="z"> Yes, I have read and understood.</label></div>'
                    '<p>Acme will keep your profile in its talent database and may contact you about future vacancies.</p>'
                    '</form>'
                    # Avnet's: the paragraph a block of its own, the box a few wrappers down in the next
                    '<div><div dir="ltr"><div><p>By creating an Avnet Candidate Home account, you acknowledge Avnet will '
                    'collect and process your personal data as outlined in our <a href="#">Applicant Data Protection and '
                    'Privacy Statement.</a></p></div></div></div><div><div><div><input id="a" type="checkbox"><span></span>'
                    '</div><label for="a">Yes, I have read and understood.</label></div></div>'
                    '<div><p>Join our talent community to hear about new openings by email.</p>'
                    '<label><input type="checkbox" id="b"> Yes, I have read and understood.</label></div>'
                    '<div><p>I certify that the information I give is true and complete.</p>'
                    '<label><input type="checkbox" id="c"> I have read and understand</label></div>'
                    '<label><input type="checkbox" id="d"> Yes, I have read and understood the job description.</label>'
                    # the review of 0.3.133's: a privacy notice named in words that keep the profile for later, share
                    # it, attest, or waive
                    '<div><p>I consent to Acme keeping my personal data in its talent database for 24 months, as '
                    'described in the Privacy Notice, so that I may be considered for future vacancies.</p>'
                    '<label><input type="checkbox" id="f"> Yes, I have read and understood.</label></div>'
                    '<div><p>Acme may share your profile with its recruiting partners and staffing agencies, per our '
                    'Privacy Policy.</p><label><input type="checkbox" id="g"> Yes, I have read and understood.</label></div>'
                    '<div><p>I certify the information I provide is true. I understand any offer is subject to the terms '
                    'and conditions of employment, including a background check.</p>'
                    '<label><input type="checkbox" id="h"> Yes, I have read and understood.</label></div>'
                    '<div><p>By applying you agree to binding arbitration under our Terms of Use.</p>'
                    '<label><input type="checkbox" id="i"> I have read and understood</label></div>'
                    '<button type="submit">Create Account</button>', encoding="utf-8")
    run(srv.browser.goto(page.as_uri()))
    data = run(srv.browser.inspect(include_dropdown_options=False))
    boxes = pipeline._terms_boxes(data)
    assert len(boxes) == 8, data["fields"]  # (by their labels alone: not one that says what was read)
    kept = run(Applier(srv)._said_terms(boxes))
    assert [b["id"] for b in kept] == [boxes[1]["id"]], (boxes, kept)


def test_a_round_holds_while_the_person_is_at_a_review_page_and_ends_once_it_is_sent(srv):
    """The desk's review round (Oct 12): no other job starts while the person checks a page filled to their Submit
    (ready, or the site's own last button); once they've pressed Submit (it's queued to send), or said they sent
    it, the round lets go."""
    from job_apply.pipeline import Run

    applier = Applier(srv)
    for status, need, held in (("ready", "", True), ("needs_you", "your_submit", True),
                               ("needs_you", "questions", False), ("queued", "", False), ("submitted", "", False)):
        applier.runs[7] = Run(7, "Technician", "Example Fab", status=status, need=need)
        applier.start_round(7)
        assert applier._in_round() is held, (status, need)


def test_consents_the_page_says_are_required_are_asked_and_its_privacy_notice_agreed(srv, monkeypatch):
    """Edward Jones (run 83): BrassRing's "Communication Consent & Privacy Notice" step, four consents, each a single
    "I agree" radio marked required only by a class and an asterisk beside it, so read as optional and left. Its "Save
    and continue" listed them in its errors, "Required", and the desk stopped: "The page didn't move on". Named by the
    errors, they're asked once a step, as the form's own questions would be: the privacy notice's acknowledgement is
    agreed to with settings.accept_notices, and the electronic communications, texting and AI pledge are the person's,
    on the card. Answered there, the step goes on."""
    from job_apply.desk import Desk

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/brassring-consents.html"), title="Senior Analyst",
                      company="Example Corp")["job"]
    desk = Desk(srv)
    applier = desk.applier

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            asked = (r.status, r.need, list(r.questions or []), list(r.log),
                     await r.page.evaluate("() => sessionStorage.getItem('consents.chosen')"))
            if r.need == "questions":
                desk.answer(job["id"], [{"label": q["label"], "value": "I agree", "remember": False}
                                        for q in r.questions])
                await until(lambda: r.status not in ("queued", "running") and "consents" not in (r.url or ""),
                            about=state(r))
            return r, asked, await r.page.evaluate("() => sessionStorage.getItem('consents.chosen')")
        finally:
            await applier.stop()

    r, (status, need, questions, log, chosen), after = run(go())
    assert status == "needs_you" and need == "questions", (status, need, r.reason, log)
    assert sorted(q["label"][:40] for q in questions) == sorted([
        "By clicking the 'I agree' button below, ", "By clicking the 'I agree' button below, ",
        "Example Corp's success is built upon our"]), questions
    assert all("Privacy Notice" not in q["label"] for q in questions), questions
    assert chosen == '["privacy"]', (chosen, log)
    assert any(line.startswith("picked “I agree” for “By clicking the 'I agree' button below, you are acknowledging")
               and line.endswith("for you (settings.accept_notices)") for line in log), log
    assert after == '["mail","text","ai","privacy"]', (after, r.log)


@pytest.mark.parametrize("query", ["", "?label=terms", "?label=agree"])
def test_a_lone_box_the_page_marks_wrong_is_asked_and_an_at_will_statement_never_agreed_for_the_person(srv, monkeypatch,
                                                                                                         query):
    """Edward Jones (run 89): BrassRing's "Employment At Will" step, one check box ("I understand and agree") under the
    at-will statement, required only by a class and an asterisk. Its "Save and continue" marked the box invalid and said
    "Required", naming nothing, and the desk stopped: "The page didn't move on: Required." A lone box the page marks
    wrong is asked, as a group so marked is. An at-will statement isn't one of the notices settings.accept_notices
    agrees to, so it's the person's, on the card with its words; ticked there, the step goes on. Nor the terms and
    conditions of employment, in the box's words or its statement's (the review's: those were agreed as a site's terms
    of use)."""
    from job_apply.desk import Desk

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/brassring-at-will.html") + query, title="Senior Analyst",
                      company="Example Corp")["job"]
    desk = Desk(srv)
    applier = desk.applier

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            asked = (r.status, r.need, list(r.questions or []), list(r.log),
                     await r.page.evaluate("() => sessionStorage.getItem('atwill.agreed')"))
            if r.need == "questions":
                desk.answer(job["id"], [{"label": q["label"], "value": True, "remember": False} for q in r.questions])
                await until(lambda: r.status not in ("queued", "running") and "at-will" not in (r.url or ""),
                            about=state(r))
            return r, asked
        finally:
            await applier.stop()

    r, (status, need, questions, log, agreed) = run(go())
    assert status == "needs_you" and need == "questions", (status, need, r.reason, log)
    assert agreed is None, log  # (never ticked for the person)
    said = f"{questions[0]['label']} {questions[0].get('about') or ''}" if len(questions) == 1 else ""
    assert ("terms and conditions of employment" if query == "?label=terms" else "at will") in said, questions
    if not query:
        assert questions[0]["label"] == "I understand and agree", questions
    assert "brassring-at-will" not in (r.url or "") and any(line == "clicked “Save and continue”" for line in r.log), \
        (r.status, r.reason, r.url, r.log)


def test_the_terms_of_employment_are_never_agreed_as_a_sites_terms():
    """The review of 0.3.170: "the terms and conditions of employment", an at-will statement or an employment agreement
    are the person's, never the site's terms of use that settings.accept_notices agrees to; those still are."""
    for said in ("I understand and agree to the terms and conditions of employment",
                 "I agree to the terms and conditions of my employment, which is at will",
                 "I acknowledge my employment with Example Corp is at-will and I agree",
                 "I have read and agree to the Employment Agreement"):
        assert not pipeline._agrees_to_terms(said), said
        assert not pipeline._attestation({"label": said, "required": True, "kind": "checkbox"}), said
    # nor acknowledged as "accurate" through an attestation's words (the second review's); an application's own
    # information certified true still is, an at-will sentence beside it or not
    for said in ("I acknowledge that the statement above regarding at-will employment is accurate",
                 "I acknowledge and confirm that the statements above about at-will employment are correct"):
        assert not pipeline._attestation({"label": said, "required": True, "kind": "checkbox"}), said
    assert pipeline._attestation({"label": "I certify that the information in my application is true and complete and I "
                                           "understand my employment is at will", "required": True, "kind": "checkbox"})
    assert pipeline._terms_notice("Terms and Conditions of Employment", "Please read and accept.") == (False, False)
    assert pipeline._agrees_to_terms("I agree to the Terms of Use and the Privacy Policy")
    assert pipeline._attestation({"label": "I agree to the Terms of Use and the Privacy Policy", "required": True,
                                  "kind": "checkbox"})


def test_a_lone_box_marked_wrong_on_a_page_that_says_nothing_wrong_is_left_as_it_is():
    """The review of 0.3.170: a lone box a page leaves marked invalid from the start, with nothing on the page said wrong
    (an optional "Send me job alerts by email"), is no question to stop on before the step is pressed; one the page
    says is wrong (BrassRing's "Required" beside it) is."""
    box = {"id": "a", "kind": "checkbox", "label": "Send me job alerts by email", "required": False, "value": False,
           "invalid": True}
    quiet = {"needs_input": [dict(box)]}
    pipeline._required_by_errors(quiet, {"fields": [box], "errors": []})
    assert not quiet["needs_input"][0]["required"]
    saved = {"needs_input": [dict(box)]}  # (a word that's no error: the second review's)
    pipeline._required_by_errors(saved, {"fields": [box], "errors": ["Saved"]})
    assert not saved["needs_input"][0]["required"]
    said = {"needs_input": [dict(box)]}
    pipeline._required_by_errors(said, {"fields": [box], "errors": ["Required"]})
    assert said["needs_input"][0]["required"]
    text = {"id": "t", "kind": "text", "label": "Available Start Date", "required": False, "value": "", "invalid": True}
    marked = {"needs_input": [dict(text)]}
    pipeline._required_by_errors(marked, {"fields": [text], "errors": []})  # (a box to type in, as before)
    assert marked["needs_input"][0]["required"]


@pytest.mark.parametrize("drawn", ["late", "with the error", "Workday's, with the error"])
@pytest.mark.parametrize("case", ["asked", "readonly", "never asked", "other heading"])
def test_a_box_the_errors_name_is_gone_back_to_once_and_only_to_be_asked(srv, monkeypatch, case, drawn):
    """The review of 0.3.165: a box taken as optional that the errors name ("Available Start Date is required.") is
    gone back to once a step, where it's asked ("asked"), the step the same with Workday's "Errors Found" heading drawn
    after. Never for a read-only box, which is never filled or asked ("readonly"), and a box the form's pass doesn't ask
    after all ("never asked": one autofill leaves to the page) isn't pressed again for: both stop after the one press,
    with what the page said. The same with another heading saying what's wrong ("There are 1 errors on this page": the
    second review's), the step still the same. And the same whether the heading is drawn after the press's look at the
    page ("late": 400 ms after the error) or before it ("with the error"): one drawn within the look (under load, CI's)
    read as the page gone on, and Save and Continue was pressed again on the "new" step. Workday's too, a button in
    its heading and an "Error - Available Start Date" button for the box (the review of the fix)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 1)
    if case in ("never asked", "other heading"):
        real = srv.autofill

        async def autofill(*args, **kwargs):
            out = await real(*args, **kwargs)
            return {**out, "needs_input": [f for f in out.get("needs_input") or []
                                           if not (f.get("label") or "").startswith("Available Start Date")]}

        monkeypatch.setattr(srv, "autofill", autofill)
    query = {"readonly": "?readonly", "other heading": "?heading=other"}.get(case, "")
    query += ("&" if query else "?") + {"late": "late=400", "with the error": "late=0"}.get(drawn, "late=0&workday")
    job = srv.add_job(url=fixture_url("site/errors-name-optional-box.html") + query, title="Technician",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => sessionStorage.getItem('pressed')")
        finally:
            await applier.stop()

    r, pressed = run(go())
    assert pressed == "1", (pressed, r.reason, r.log)
    went_back = any("hadn't marked as required, so I went back to them" in line for line in r.log)
    if case == "asked":
        assert r.need == "questions" and [q["label"] for q in r.questions] == ["Available Start Date"], \
            (r.need, r.reason, r.questions, r.log)
        assert went_back, r.log
    else:
        # (Workday's list names the box too)
        also = ".; “Available Start Date” needs fixing" if drawn.startswith("Workday") else ""
        assert r.need == "stuck" and r.reason == (f"The page didn't move on: Available Start Date is required{also}. Fix "
                                                  "it in the desk's browser window, then press Carry on."), (r.reason, r.log)
        assert went_back == (case != "readonly"), r.log


@pytest.mark.parametrize("heading, says_wrong", [
    ("Errors Found", True), ("There are 1 errors on this page", True), ("2 Errors", True), ("Error", True),
    ("1 error found.", True), ("There is a problem with your answers", True), ("Please correct the following", True),
    ("Oops! Please fix the errors below", True), ("Fix the highlighted fields", True),
    ("Error Handling Experience", False), ("Please Review Your Application", False), ("Enter Your Information", False),
    ("Required Documents", False), ("Missing Information", False), ("Problem Solving", False),
    ("Data Center Technician", False), ("Errors and Omissions Insurance", False), ("My Questions", False)])
def test_a_step_is_the_same_with_or_without_a_heading_saying_whats_wrong(heading, says_wrong):
    """The page's fingerprint (did a press move it on?) leaves out a heading that only says the step has something
    wrong, and Workday's buttons for it ("Errors Found" in its heading, "Error - <the box>" for each), never a step's
    own heading: "Please Review Your Application" or "Data Center Technician" is another page."""
    page = {"url": "https://example.com/apply", "headings": ["My Questions"], "fields": 2, "questions": (),
            "actions": ["Save and Continue"]}
    drawn = {**page, "headings": [heading, "My Questions"], "actions": [heading, "Error - Available Start Date",
                                                                        "Save and Continue"]}
    assert (pipeline._fingerprint(drawn) == pipeline._fingerprint(page)) == says_wrong
    if not says_wrong:  # (its own heading kept, whatever the error buttons beside it)
        assert pipeline._step_headings(drawn) == (heading, "My Questions")
        assert pipeline._fingerprint({**drawn, "headings": ["My Questions"]}) != pipeline._fingerprint(page)


def test_a_notice_the_way_back_opened_is_agreed_and_the_step_pressed_again(srv, monkeypatch):
    """The second review of 0.3.165: on the way back to a step for what its errors name, the privacy notice's
    acknowledgement picked (settings.accept_notices) opened the notice itself in a dialog, agreed to; on the look after
    it, nothing was left to fill or pick, and the desk stopped with what the page had said before. Something went in on
    the way back: the step is pressed again, and goes on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/brassring-consents.html") + "?dialog", title="Senior Analyst",
                      company="Example Corp")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate("() => [sessionStorage.getItem('consents.chosen'), "
                                            "sessionStorage.getItem('consents.dialog')]")
        finally:
            await applier.stop()

    r, (chosen, oks) = run(go())
    assert chosen == '["privacy"]' and oks == "1", (chosen, oks, r.log)
    assert "consents" not in (r.url or "") and r.need != "stuck", (r.url, r.reason, r.log)


def test_a_box_workdays_own_field_alert_says_is_empty_though_it_shows_the_answer_is_typed_again(srv, monkeypatch):
    """Banner Health's Workday (the live check, run 99): the answer to its required "Can you provide an example…"
    text area showed in the box, but the page's own record of it was empty, and Save and Continue came back with
    "Errors Found": its list's "Error - <the question>" button (the whole question, over 200 characters: never among
    the page's buttons) and its hint, and the box marked invalid with Workday's own field alert beside it ("Error:
    The field … is required and must have a value."), which has no role="alert", "errorMessage" or "error" class. The
    desk read no error, pressed Save and Continue three times and stopped on "marked invalid". A box's own alert
    (what its aria-describedby points at, saying something's wrong; Workday's inputAlert) is an error the page
    shows, so the box is typed again, once, and the step goes on."""
    from job_apply.desk import Desk

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "STEP_SAVE_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/workday-field-alert.html"), title="Cardiovascular Special Procedures Tech",
                      company="Example Fab")["job"]
    desk = Desk(srv)
    applier = desk.applier

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            if r.need == "questions":  # (the person's answer, as the live check's)
                desk.answer(job["id"], [{"label": q["label"], "value": "Test answer", "remember": False}
                                        for q in r.questions])
                await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "review.html" in r.url, (r.url, r.reason, r.log)
    typed = next(i for i, line in enumerate(r.log) if line.startswith("typed again what the page said was empty though it showed it"))
    clicks = [i for i, line in enumerate(r.log) if line.startswith("clicked “Save and Continue”")]
    assert clicks and sum(c > typed for c in clicks) == 1, r.log  # (typed again once, then the step pressed once more)


def test_a_refusal_for_want_of_verification_drawn_after_the_look_is_still_the_wait(srv, monkeypatch, job_apply_home):
    """Vanguard's Workday again (run 100, on 0.3.178): its refusal, "Verify your account before you sign in…",
    is drawn a moment after Sign In is pressed; the desk's next look saw the form alone, the sign-in then
    counted as refused when the alert came, and a password reset was asked for before the page was looked at
    again. A refusal is read as it stands when it's judged: for want of the account's verification, it's the
    wait and the site's resend, never a reset."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    asked = []

    with served_fixtures() as site:
        link = f"{site}site/workday-verify.html"

        def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
            asked.append(want)
            return pipeline.mailbox.Found(want, link, "myworkday.com", time.time()) if want == "link" else None

        monkeypatch.setattr(pipeline.mailbox, "search", inbox)
        job = srv.add_job(url=f"{site}site/workday-account.html?exists=not-a-real-password&verify=stale&slow", title="FSE",
                          company="Example Fab")["job"]
        applier = Applier(srv)

        async def go():
            applier.start()
            try:
                r = applier.enqueue(job["id"])
                await until(lambda: r.seen_form and r.status not in ("queued", "running"), about=state(r))
                return r, await workday_did(r.page)
            finally:
                await applier.stop()

        r, did = run(go())
    log = r.log
    assert "link" in asked and "reset" not in asked, asked
    assert (did.get("resends", 0), did.get("resets", 0), did.get("presses", 0), did["signIns"]) == (1, 0, 0, 2), (did, log)
    assert not any("password reset" in line for line in log) and r.need not in ("sign_in", "email_code"), (r.reason, log)


@pytest.mark.slow
def test_a_new_accounts_refusal_drawn_after_the_look_is_still_the_wait(srv, monkeypatch, job_apply_home):
    """The same for the account the desk just made: Workday's "An email has been sent to you. Please verify your
    account" is its Sign In's answer too, drawn a moment after the press (the review of 0.3.180). After the one
    reset asked before the account was made (the saved password refused, no reset email: the way to Create
    Account), the desk waits for the link, asks for no second reset and presses no second Create Account; the link
    opened, it signs in once."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "RESET_MAIL_WAIT", 4)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    asked = []

    with served_fixtures() as site:
        link = f"{site}site/workday-verify.html"

        def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
            asked.append(want)
            return pipeline.mailbox.Found(want, link, "myworkday.com", time.time()) if want == "link" else None

        monkeypatch.setattr(pipeline.mailbox, "search", inbox)
        job = srv.add_job(url=f"{site}site/workday-account.html?verify=late&slow", title="FSE",
                          company="Example Fab")["job"]
        applier = Applier(srv)

        async def go():
            applier.start()
            try:
                r = applier.enqueue(job["id"])
                await until(lambda: r.seen_form and r.status not in ("queued", "running"), about=state(r))
                return r, await workday_did(r.page)
            finally:
                await applier.stop()

        r, did = run(go())
    log = r.log
    assert "reset" in asked and "link" in asked and asked.index("reset") < asked.index("link"), asked
    # (one refused before the account was made, one for want of the link, one after it; one reset, before the account)
    assert (did["created"], did["presses"], did.get("resets", 0), did["signIns"]) == (1, 1, 1, 3), (did, log)
    made = next(i for i, line in enumerate(log) if line.startswith("pressed \u201cCreate Account\u201d"))
    resets = [i for i, line in enumerate(log) if "asked it to email a password reset" in line]
    assert resets and all(i < made for i in resets), log
    waited = next(i for i, line in enumerate(log) if "emailed you a link to verify your new account" in line)
    opened = log.index("opened the confirmation link from your email (sent from myworkday.com)")
    assert made < waited < opened and r.need not in ("sign_in", "email_code"), (r.reason, log)
    assert sum(line.startswith("created your account on") for line in log) == 1, log


def test_a_posting_page_that_is_the_sites_own_error_page_is_said_in_its_words(srv, monkeypatch):
    """BrassRing's error page where Edward Jones' posting should be (run 100): "Error. An error occurred while
    processing your request.", the same after the one reload. The desk said it couldn't find the button that
    moves the application on; the page is the site's own trouble, said in its words."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/brassring-error.html"), title="Learning Experience Designer",
                      company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.reason, r.log)
    assert "An error occurred while processing your request" in r.reason and "reloaded" in r.reason, r.reason
    assert "couldn't find the button" not in r.reason, r.reason
    assert any("so I reloaded it" in line for line in r.log), r.log


@pytest.mark.parametrize("query", ["?slow=3000", "?slow=1500&late=1500", "?never"])
def test_a_cornerstone_posting_still_loading_is_waited_on_then_its_apply_pressed(srv, monkeypatch, query):
    """Linde's Cornerstone requisition page (the live check, run 105): the job's title and bar drawn, its Apply Now
    button there but disabled and still without its words, the description empty, and the site's loading overlay over
    the page (its spinner in a box); the desk read no button and stopped ("I couldn't find the button that moves this
    application on"), twice. The overlay is a loading indicator: the page is waited on, and its Apply Now pressed once
    it's enabled, also when the button gets its words a moment after the overlay goes (&late: the review of 0.3.183);
    a page that never finishes is reloaded once, then said to be still loading."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    # (the page's button gets its words 1.5 s after the overlay goes (&late): the wait after it at its own value, not
    # the fast tests' 0.5 s, which read the button before its words came, 1 run in 3)
    monkeypatch.setattr(pipeline, "INDICATOR_GONE_SETTLE", 3)
    job = srv.add_job(url=fixture_url("site/csod-posting-loading.html") + query, title="Analytical Systems Superintendent",
                      company="Example Gas")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), timeout=120, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert "couldn't find the button" not in r.reason, (r.reason, r.log)
    if query == "?never":
        assert sum("still loading after" in line and "reloaded it" in line for line in r.log) == 1, r.log
        assert (r.status, r.need) == ("needs_you", "stuck") and "didn't finish loading" in r.reason, (r.reason, r.log)
    else:
        assert any("clicked \u201cApply Now\u201d" in line for line in r.log), r.log
        assert r.seen_form and "/step" in r.url, (r.url, r.reason, r.log)  # (on into the application's steps)


def test_a_steady_address_drops_a_requests_tokens_and_keeps_the_steps_own():
    """The review of Oct 15: a step's address with a fresh token on each visit made every press look new."""
    from job_apply.pipeline import _steady_url

    assert _steady_url("https://x.example/apply?step=2&csrf=abc123def456&_=1760640000000") == \
        "https://x.example/apply?step=2"
    assert _steady_url("https://x.example/apply;jsessionid=AB12?jobId=9&t=17#/q2") == "https://x.example/apply?jobId=9#/q2"
    assert _steady_url("https://x.example/apply?id=3f2504e04f8911d39a0c0305e82c3301") == \
        "https://x.example/apply?id=3f2504e04f8911d39a0c0305e82c3301"  # (a step's own id, however long, stays)
    assert _steady_url(None) == ""
    # (the review of 0.3.189: a job's own long number stays, Eightfold's pid and Greenhouse's numeric token; a time in
    # milliseconds near now goes)
    import time

    assert _steady_url("https://careers.example.com/careers?pid=563808971834447") == \
        "https://careers.example.com/careers?pid=563808971834447"
    assert _steady_url("https://boards.example.io/embed/job_app?for=acme&token=4932681101") == \
        "https://boards.example.io/embed/job_app?for=acme&token=4932681101"
    assert _steady_url(f"https://x.example/apply?step=2&v={int(time.time() * 1000)}") == "https://x.example/apply?step=2"


def test_stopping_the_desk_ends_a_worker_that_dropped_a_cancellation(srv):
    """CI's Python 3.10 run of 0.3.188 hung for good: asyncio.wait_for there drops a cancellation that comes as what it
    waits on ends, and stop() cancelled the worker once, then waited on it for ever. A worker that drops one is
    cancelled again until it has ended."""
    applier = Applier(srv)

    async def stubborn():
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            pass  # (dropped, as 3.10's wait_for drops one)
        await asyncio.sleep(100)

    async def go():
        worker = applier._worker_task = asyncio.ensure_future(stubborn())
        await asyncio.sleep(0)
        stopping = asyncio.ensure_future(applier.stop())
        done, _ = await asyncio.wait({stopping}, timeout=5)
        ended = bool(done) and worker.done()
        for task in (stopping, worker):  # (the old stop, still waiting: let the test end)
            task.cancel()
        await asyncio.wait({stopping, worker}, timeout=5)
        return ended, applier._worker_task

    assert run(go()) == (True, None)


def test_a_bounded_read_never_drops_its_callers_cancellation():
    """The same on a read of the page: a caller cancelled as the read ends is cancelled (Python 3.10's wait_for returned
    the read instead, and the cancellation was gone)."""
    from job_apply.browser import _bounded

    async def go():
        read = asyncio.get_running_loop().create_future()
        caller = asyncio.ensure_future(_bounded(read, 10))
        await asyncio.sleep(0)  # (the caller waits on the read)
        read.set_result("read")
        caller.cancel()
        try:
            await caller
        except asyncio.CancelledError:
            return "cancelled"
        return "returned"

    assert asyncio.run(go()) == "cancelled"


def test_the_codes_given_out_are_kept_a_day(monkeypatch):
    """The review of Oct 15: each emailed code the desk put in was kept for as long as the desk ran. A day's are kept."""
    import time

    monkeypatch.setattr(pipeline, "_MAIL_TAKEN", {("a@x.example", "111111", time.time() - 2 * 86400): 1,
                                                  ("a@x.example", "222222", time.time() - 600): 2})
    pipeline._take_code(("a@x.example", "333333", time.time()), 3)
    assert sorted(pipeline._MAIL_TAKEN.values()) == [2, 3]


# ------------------------------------------------------------------ jobs at once (settings.jobs_at_once)
# The owner's ask (Oct 16): a queue of 76 jobs took hours, one at a time. Up to three go at once, each in a tab of its
# own; one at a time on a site, one at a time in a step that may email the person, and none while the person is at
# another job's tab for the desk.

_SIGN_IN = ('<html><head><title>{0} sign in</title></head><body><h1>Sign In</h1><form><label for="em">Email Address'
            '</label><input id="em" type="email"><label for="pw">Password</label><input id="pw" type="password">'
            '<button type="submit">Sign In</button></form></body></html>')


async def _serve_slowly(srv, pages: dict, posts: list, slow: float, holds: dict | None = None) -> None:
    """_serve's employer sites (any https:// address here), each page `slow` seconds in coming, so the passes of two
    jobs filled at once overlap; one in `holds` not before its condition is met."""
    async def handler(route):
        req = route.request
        url = req.url.split("?")[0]
        await asyncio.sleep(slow)
        while holds and url in holds and not holds[url]():
            await asyncio.sleep(0.1)
        if req.method == "POST":
            posts.append(url)
            body = pages.get(("POST", url), "<html><body><h1>Thank you for applying</h1></body></html>")
        else:
            body = pages.get(url, "<html><body>not found</body></html>")
        with contextlib.suppress(Exception):  # (a tab closed as the test ends)
            await route.fulfill(status=200, content_type="text/html", body=body)

    await srv.browser.page()
    await srv.browser._ctx.route(lambda url: url.startswith("https://"), handler)


def _passes(monkeypatch) -> list:
    """Each job's passes over its pages, as (job, when it began, when it ended)."""
    passes: list = []
    real = Applier._drive

    async def drive(self, r):
        began = time.monotonic()
        try:
            await real(self, r)
        finally:
            passes.append((r.job_id, began, time.monotonic()))

    monkeypatch.setattr(Applier, "_drive", drive)
    return passes


def _overlap(passes: list, a: int, b: int) -> bool:
    return any(x[1] < y[2] and y[1] < x[2] for x in passes if x[0] == a for y in passes if y[0] == b)


@pytest.mark.parametrize("at_once", [1, 2])
def test_jobs_at_different_employers_are_filled_at_once_each_in_its_own_tab(srv, monkeypatch, at_once):
    """With jobs_at_once at 2, two jobs at different employers are filled at the same time, each in its own tab and with
    its own log, and neither is sent: each waits on its review page, and the Submit the person presses sends that one
    alone. With 1, one after the other, in order, as the desk always did."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    passes = _passes(monkeypatch)
    a_url, b_url = "https://careers.acme-fab.example/apply/1", "https://careers.other-litho.example/apply/2"
    pages = {a_url: _form("Acme Fab"), b_url: _form("Other Litho")}
    posts: list = []
    a = srv.add_job(url=a_url, title="Field Service Engineer", company="Acme Fab")["job"]
    b = srv.add_job(url=b_url, title="Litho Technician", company="Other Litho")["job"]
    applier = Applier(srv)
    applier.jobs_at_once = at_once

    async def go():
        await _serve_slowly(srv, pages, posts, 1.0)
        applier.start()
        try:
            first, second = applier.enqueue(a["id"]), applier.enqueue(b["id"])
            both = False
            deadline = time.monotonic() + 60
            while not first.status == second.status == "ready":
                both = both or first.status == second.status == "running"
                assert time.monotonic() < deadline, (state(first)(), state(second)())
                await asyncio.sleep(0.05)
            sent_before = list(posts)
            applier.submit_now(a["id"])
            await until(lambda: first.status == "submitted", about=state(first))
            return first, second, both, sent_before
        finally:
            await applier.stop()

    first, second, both, sent_before = run(go())
    assert sent_before == [] and posts == ["https://careers.acme-fab.example/posted"], posts
    assert second.status == "ready"
    assert both == (at_once == 2) and _overlap(passes, a["id"], b["id"]) == (at_once == 2), passes
    if at_once == 1:
        assert [p[0] for p in passes] == [a["id"], b["id"]]
    assert first.page is not second.page
    assert first.log[0] == f"opened {a_url}" and second.log[0] == f"opened {b_url}", (first.log, second.log)
    assert not any("other-litho" in line for line in first.log) and not any("acme-fab" in line for line in second.log)
    assert first.url.startswith("https://careers.acme-fab.example/") and second.url.startswith(b_url)


def test_two_jobs_on_one_site_are_never_filled_at_once(srv, monkeypatch):
    """Sign-ins, accounts, password resets and emailed codes are a site's own, and two jobs at once on one site would
    collide. With three at once, two jobs at one employer, and two of different employers on one host, are each filled
    one after the other, while a job elsewhere goes alongside them: the queue isn't held up behind the one waiting."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    passes = _passes(monkeypatch)
    urls = {"a1": ("https://careers.acme-fab.example/apply/1", "Acme Fab"),
            "a2": ("https://careers.acme-fab.example/apply/2", "Acme Fab"),
            "b1": ("https://jobs.shared-board.example/litho/3", "Other Litho"),
            "b2": ("https://jobs.shared-board.example/etch/4", "Example Etch"),
            "c": ("https://careers.example-metrology.example/apply/5", "Example Metrology")}
    pages = {url: _form(company) for url, company in urls.values()}
    jobs = {k: srv.add_job(url=url, title="Technician", company=company)["job"]["id"] for k, (url, company) in urls.items()}
    applier = Applier(srv)
    applier.jobs_at_once = 3

    async def go():
        await _serve_slowly(srv, pages, [], 1.0)
        applier.start()
        try:
            runs = [applier.enqueue(jobs[k]) for k in ("a1", "a2", "b1", "b2", "c")]
            await until(lambda: all(r.status == "ready" for r in runs), timeout=90,
                        about=lambda: [state(r)() for r in runs])
        finally:
            await applier.stop()

    run(go())
    assert not _overlap(passes, jobs["a1"], jobs["a2"]) and not _overlap(passes, jobs["b1"], jobs["b2"]), passes
    assert _overlap(passes, jobs["a1"], jobs["b1"]) and _overlap(passes, jobs["a1"], jobs["c"]), passes


def test_a_job_systems_tenant_and_an_employers_own_site_each_take_one_job_at_a_time(srv):
    """Two jobs on one Workday tenant (intel.wd1) never go at once, whoever's jobs they are; another tenant (amkor.wd5)
    may. An employer's own site is one whatever its host (careers. and jobs.). And a job that comes to a site another
    job under way got to first waits for it there, unless it's in a step that may email the person (the other may be
    waiting on that)."""
    from types import SimpleNamespace

    def add(url, company):
        return srv.add_job(url=url, title="Technician", company=company)["job"]["id"]

    intel = add("https://intel.wd1.myworkdayjobs.com/External/job/Chandler/Technician_1", "Intel")
    altera = add("https://intel.wd1.myworkdayjobs.com/Altera/job/San-Jose/Technician_2", "Altera")
    amkor = add("https://amkor.wd5.myworkdayjobs.com/Amkor/job/Tempe/Technician_3", "Amkor")
    own = add("https://careers.acme-fab.example/job/4", "Acme Fab")
    own_too = add("https://jobs.acme-fab.example/job/5", "Acme Wafer")
    applier = Applier(srv)
    for job_id in (intel, altera, amkor, own, own_too):
        job = srv.tracker().get(job_id)
        applier.runs[job_id] = Run(job_id, "Technician", job["company"], status="queued")
    applier._slots = {intel: object(), own: object()}  # under way
    assert not applier._site_free(altera) and not applier._site_free(own_too)
    assert applier._site_free(amkor)
    applier._slots = {amkor: object()}
    assert applier._site_free(intel) and applier._site_free(altera) and applier._site_free(own)

    # come to one tenant in the middle of their passes: the first there goes on, the other waits for it
    first, second = applier.runs[amkor], applier.runs[own]
    applier._slots = {amkor: object(), own: object()}
    first.began, second.began = 1, 2
    first.page = SimpleNamespace(url="https://intel.wd1.myworkdayjobs.com/External/login", is_closed=lambda: False)
    assert applier._site_holder(first) is None
    second.page = SimpleNamespace(url="https://intel.wd1.myworkdayjobs.com/Altera/login", is_closed=lambda: False)
    assert applier._site_holder(second) is first and applier._site_holder(first) is None
    second.inbox = True  # (signing in: never held there, as the other may be waiting for it)
    assert applier._site_holder(second) is None


def test_a_wait_on_an_emailed_code_holds_another_jobs_sign_in_until_its_code_is_in(srv, monkeypatch):
    """The inbox is one: a code meant for one job is never another's. While one job waits on its emailed code, a job
    filled alongside it comes to its sign-in and waits there (nothing pressed, nothing emailed), and goes on once the
    code from the inbox is in."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    sent = {"yet": False}

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        return pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time()) if sent["yet"] else None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)
    b_url = "https://careers.other-litho.example/apply/2"
    waiting = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    signing_in = srv.add_job(url=b_url, title="Litho Technician", company="Other Litho")["job"]
    applier = Applier(srv)
    applier.jobs_at_once = 2
    paused: list = []
    real_pause = Applier._pause

    def pause(self, r, need, reason, questions=None, seen=None, *, code):
        other = self.runs.get(waiting["id"])
        code_in = other is not None and any(line.startswith("entered the code from your email") for line in other.log)
        paused.append((r.job_id, need, code_in))
        return real_pause(self, r, need, reason, questions, seen, code=code)

    monkeypatch.setattr(Applier, "_pause", pause)

    async def go():
        # (its sign-in page comes once the other job is waiting on its code)
        holds = {b_url: lambda: waiting["id"] in applier.runs and applier.runs[waiting["id"]].need == "email_code"}
        await _serve_slowly(srv, {b_url: _SIGN_IN.format("Other Litho")}, [], 0.5, holds)
        applier.start()
        try:
            a, b = applier.enqueue(waiting["id"]), applier.enqueue(signing_in["id"])
            await until(lambda: a.status == "needs_you" and a.need == "email_code", about=state(a))
            await until(lambda: any(line.startswith("waiting: ") for line in b.log), about=state(b))
            await asyncio.sleep(1.5)
            held = (b.status, b.need)
            sent["yet"] = True  # the code comes
            await until(lambda: b.status == "needs_you", timeout=30, about=state(b))
            return a, b, held
        finally:
            await applier.stop()

    a, b, held = run(go())
    assert held == ("running", ""), held
    assert "entered the code from your email (sent from careers.example.com)" in a.log, a.log
    assert b.need == "sign_in" and ("waiting: Example Fab is signing in or waiting on an email; this one carries on "
                                    "after that") in b.log, b.log
    b_paused = [p for p in paused if p[0] == signing_in["id"]]
    assert b_paused and all(code_in for _, _, code_in in b_paused), paused


def test_only_one_job_under_way_is_in_a_step_that_may_email_the_person(srv, monkeypatch):
    """A job under way signing in, making an account or waiting on an email holds the inbox: another job coming to its
    own sign-in waits (and says so) until that one is into its application, then goes on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    holder_job = srv.add_job(url="https://careers.acme-fab.example/apply/1", title="FSE", company="Acme Fab")["job"]
    job = srv.add_job(url=fixture_url("site/posting.html"), title="Technician", company="Example Fab")["job"]
    applier = Applier(srv)
    applier.jobs_at_once = 2

    async def go():
        holder = applier.runs[holder_job["id"]] = Run(holder_job["id"], "FSE", "Acme Fab", status="running")
        holder.inbox = True  # signing in, in its own tab
        holder_task = asyncio.ensure_future(asyncio.sleep(3600))
        applier._slots[holder.job_id] = holder_task
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: any(line.startswith("waiting: ") for line in r.log), about=state(r))
            await asyncio.sleep(1.5)
            held = (r.status, r.need, r.page.url.rsplit("/", 1)[-1])
            holder.inbox, holder.status = False, "ready"  # into its application, and on to its review page
            await until(lambda: r.status == "needs_you", timeout=30, about=state(r))
            return r, held
        finally:
            holder_task.cancel()
            await applier.stop()

    r, held = run(go())
    assert held == ("running", "", "signin.html"), held
    assert "waiting: Acme Fab is signing in or waiting on an email; this one carries on after that" in r.log, r.log
    assert r.need == "sign_in" and r.blocking, (r.need, r.reason)


def test_the_inbox_isnt_read_for_a_job_left_waiting_while_another_job_is_signing_in(srv, monkeypatch):
    """A job left waiting on its emailed code is looked for in the inbox between steps; while another job under way is
    signing in (or making an account), its site may be emailing a code this moment, before that job's pause says from
    when its mail is its own: the waiting job's look waits till it's past that."""
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    looked: list = []

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None, others=None):
        looked.append(want)
        return None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)
    left_job = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    signing_job = srv.add_job(url="https://careers.acme-fab.example/apply/1", title="FSE", company="Acme Fab")["job"]
    applier = Applier(srv)

    async def go():
        left = applier.runs[left_job["id"]] = Run(left_job["id"], "FSE", "Example Fab", status="needs_you",
                                                  need="email_code", left=True, paused_at=time.time())
        await srv.browser.new_tab()
        await srv.browser.goto(fixture_url("site/verify-email.html") + "?code")
        left.page = srv.browser.current_tab
        signing = applier.runs[signing_job["id"]] = Run(signing_job["id"], "FSE", "Acme Fab", status="running")
        signing.inbox = True
        applier._slots[signing.job_id] = object()
        await applier._check_mail(left)
        held = (list(looked), left.mail_last)
        signing.inbox = False  # into its application
        await applier._check_mail(left)
        return held

    held = run(go())
    assert held == ([], "not looked: another job is signing in or waiting on an email"), held
    assert looked == ["code"]


def test_in_one_of_the_desks_rounds_the_jobs_under_way_go_on_and_none_comes_to_the_front(srv):
    """The person at a job's tab in one of the desk's rounds (a bot check, a review page): a job under way goes on in its
    own tab (the review of jobs at once: held at its next step, it kept its place, and one in a step that may email the
    person could miss its code), and its tab is never brought up over the round's, which is the one kept in front;
    once the round is over, a stop of its that waited is brought up."""
    from types import SimpleNamespace

    fronted: list = []

    def tab(name):
        async def front():
            fronted.append(name)
        return SimpleNamespace(is_closed=lambda: False, bring_to_front=front, url=f"https://{name}.example/apply")

    jobs = [srv.add_job(url=f"https://{n}.example/job", title="FSE", company=n.title())["job"]["id"]
            for n in ("acme", "litho")]
    applier = Applier(srv)
    in_round = applier.runs[jobs[0]] = Run(jobs[0], "FSE", "Acme", status="ready", page=tab("acme"))
    under_way = applier.runs[jobs[1]] = Run(jobs[1], "FSE", "Litho", status="running", page=tab("litho"))
    applier._slots[under_way.job_id] = object()
    under_way.inbox = True  # (its site may have just emailed it a code)
    applier.start_round(in_round.job_id)

    async def go():
        waited = await asyncio.wait_for(applier._hold_for_site(under_way), 2)
        under_way.inbox = False
        return waited, await asyncio.wait_for(applier._hold_for_site(under_way), 2)

    assert run(go()) == (0.0, 0.0) and not any(line.startswith("waiting") for line in under_way.log), under_way.log
    assert applier._front_tab() is in_round.page
    run(applier._bring_forward(under_way))  # (it stops for the person meanwhile)
    applier._pause(under_way, "stuck", "The page didn't move on.", code="step_not_saved")
    applier._slots.clear()  # (its pass over)
    assert fronted == [] and under_way.job_id in applier._behind
    applier.end_round()
    assert applier._front_tab() is None
    run(applier._front_next())
    assert fronted == ["litho"] and not applier._behind


def test_a_pause_is_still_watched_when_every_job_under_way_waits_on_it(srv):
    """Jobs at once lowered (3 to 1, say) while jobs under way wait on the person's sign-in in another tab: the queue is
    as full as it may be, but the sign-in is watched still (or they'd wait on it for ever), and the job it held goes on
    once the person's past it; nothing else begins beside the ones under way."""
    jobs = [srv.add_job(url=f"https://careers.employer{n}.example/job/{n}", title="FSE", company=f"Employer {n}")["job"]["id"]
            for n in range(2)]
    applier = Applier(srv)
    applier.jobs_at_once = 1
    waited: list = []

    async def moved_on(r):
        return True

    async def until_one_is_done():
        waited.append(True)

    applier._moved_on, applier._until_one_is_done = moved_on, until_one_is_done

    async def go():
        busy = asyncio.ensure_future(asyncio.sleep(3600))
        applier.runs[jobs[0]] = Run(jobs[0], "FSE", "Employer 0", status="running")
        applier._slots[jobs[0]] = busy
        applier._waiting.add(jobs[0])  # under way, waiting on the sign-in below
        holder = applier.runs[jobs[1]] = Run(jobs[1], "FSE", "Employer 1", status="running")
        applier._pause(holder, "sign_in", "Sign in there.", code="account_theirs")
        try:
            await applier._tick()
            return holder
        finally:
            busy.cancel()

    holder = run(go())
    assert not holder.blocking and holder.status == "queued" and list(applier.tasks) == [("apply", jobs[1])]
    assert list(applier._slots) == [jobs[0]] and waited  # (nothing begun beside it)
    applier._waiting.clear()  # working again: the queue waits on it, as ever
    waited.clear()
    run(applier._tick())
    assert waited and list(applier.tasks) == [("apply", jobs[1])]


def test_a_submit_the_person_pressed_goes_before_the_jobs_queued_ahead_of_it(srv, monkeypatch):
    """Filled at once, a Submit the person presses still goes first: the next free place is its, before any job queued
    ahead of it."""
    started: list = []

    async def submit(self, r, by_person=True):
        started.append(("submit", r.job_id))

    async def drive(self, r):
        started.append(("apply", r.job_id))
        r.status = "ready"

    monkeypatch.setattr(Applier, "_submit", submit)
    monkeypatch.setattr(Applier, "_drive", drive)
    ids = [srv.add_job(url=f"https://careers.employer{n}.example/job/{n}", title="FSE", company=f"Employer {n}")["job"]["id"]
           for n in range(3)]
    applier = Applier(srv)
    applier.jobs_at_once = 2

    async def go():
        busy = asyncio.ensure_future(asyncio.sleep(3600))
        applier.runs[ids[0]] = Run(ids[0], "FSE", "Employer 0", status="running")
        applier._slots[ids[0]] = busy  # one place taken
        applier.enqueue(ids[1])
        applier.runs[ids[2]] = Run(ids[2], "FSE", "Employer 2", status="ready")
        applier.submit_now(ids[2])
        applier.tasks.rotate(-1)  # (the Submit behind the queued job)
        assert [t[0] for t in applier.tasks] == ["apply", "submit"]
        await applier._tick()
        busy.cancel()
        await asyncio.sleep(0.1)

    run(go())
    assert started[0] == ("submit", ids[2]), started


def test_a_pause_that_needs_the_person_holds_new_jobs_and_brings_no_other_tab_over_it(srv, monkeypatch):
    """A sign-in that needs the person holds the queue, as ever: no job starts while they're at it. A job already under
    way in another tab goes on in it (the review of jobs at once: held, it kept its place), and when it stops for the
    person too, its tab isn't brought over the sign-in they're at: it comes up once they're done there."""
    from playwright.async_api import Page

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 600)
    fronted: list = []
    real_front = Page.bring_to_front

    async def bring_to_front(self):
        fronted.append(self.url.rsplit("/", 1)[-1])
        await real_front(self)

    monkeypatch.setattr(Page, "bring_to_front", bring_to_front)
    c_url = "https://careers.example-metrology.example/apply/3"
    gateway = "<html><head><title>502 Bad Gateway</title></head><body><h1>502 Bad Gateway</h1><hr>nginx</body></html>"
    signing_in = srv.add_job(url=fixture_url("site/posting.html"), title="FSE", company="Example Fab")["job"]
    under_way = srv.add_job(url=c_url, title="Metrology Technician", company="Example Metrology")["job"]
    later = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)
    applier.jobs_at_once = 3
    # (its page comes once the other job holds for the person: its own stop comes as they're at that sign-in)
    holds = {c_url: lambda: signing_in["id"] in applier.runs and applier.runs[signing_in["id"]].blocking}

    async def go():
        await _serve_slowly(srv, {c_url: gateway}, [], 0.3, holds)
        applier.start()
        try:
            a, c = applier.enqueue(signing_in["id"]), applier.enqueue(under_way["id"])
            await until(lambda: a.status == "needs_you" and a.blocking, about=state(a))
            b = applier.enqueue(later["id"])
            await until(lambda: c.status == "needs_you", timeout=30, about=state(c))
            await asyncio.sleep(1.5)
            held = (b.status, c.need, list(fronted))
            a.active_at -= 3600  # (no one at work in its tab any more)
            applier.later(a.job_id)
            await until(lambda: b.status not in ("queued", "running") and "3" in fronted, timeout=60,
                        about=lambda: (state(b)(), fronted))
            return held
        finally:
            await applier.stop()

    held = run(go())
    assert held == ("queued", "stuck", ["signin.html"]), held
    assert fronted.index("3") > 0, fronted  # (brought up once the person was done at the sign-in)


def test_jobs_under_way_finish_their_pass_when_the_queue_is_paused(srv, monkeypatch):
    """Pause queue with two jobs under way: both finish their pass, as the one did before, and no other starts until
    Resume queue."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    urls = {"https://careers.acme-fab.example/apply/1": "Acme Fab",
            "https://careers.other-litho.example/apply/2": "Other Litho",
            "https://careers.example-metrology.example/apply/3": "Example Metrology"}
    pages = {url: _form(company) for url, company in urls.items()}
    jobs = [srv.add_job(url=url, title="Technician", company=company)["job"]["id"] for url, company in urls.items()]
    applier = Applier(srv)
    applier.jobs_at_once = 2

    async def go():
        await _serve_slowly(srv, pages, [], 1.0)
        applier.start()
        try:
            runs = [applier.enqueue(j) for j in jobs]
            await until(lambda: runs[0].status == runs[1].status == "running", about=lambda: [state(r)() for r in runs])
            applier.pause_queue()
            await until(lambda: runs[0].status == runs[1].status == "ready", timeout=60,
                        about=lambda: [state(r)() for r in runs])
            await asyncio.sleep(1.5)
            held = runs[2].status
            applier.resume_queue()
            await until(lambda: runs[2].status == "ready", timeout=60, about=state(runs[2]))
            return held
        finally:
            await applier.stop()

    assert run(go()) == "queued"


def test_a_job_that_pauses_for_the_persons_hands_behind_another_is_brought_up_after_it(srv):
    """Filled at once, two jobs can come to a pause that needs the person's hands: the first's tab is brought to the
    front, the second's isn't (it would come up over the tab they're at), and its turn comes once the first is done:
    its tab is brought up then, and its wait for them begins then."""
    from types import SimpleNamespace

    applier = Applier(srv)
    fronted: list = []

    def tab(name):
        async def front():
            fronted.append(name)
        return SimpleNamespace(is_closed=lambda: False, bring_to_front=front, url=f"https://{name}.example/signin")

    jobs = [srv.add_job(url=f"https://{n}.example/job", title="FSE", company=n.title())["job"]["id"]
            for n in ("acme", "litho")]
    a, b = (Run(j, "FSE", c, status="running") for j, c in zip(jobs, ("Acme", "Litho")))
    applier.runs = {a.job_id: a, b.job_id: b}
    a.page, b.page = tab("acme"), tab("litho")

    async def not_moved(r):
        return False

    async def sleep(seconds):
        pass

    async def go():
        await applier._bring_forward(a)
        applier._pause(a, "sign_in", "Sign in there.", code="account_theirs")
        await applier._bring_forward(b)  # (b comes to its own sign-in meanwhile)
        applier._pause(b, "sign_in", "Sign in there.", code="account_theirs")
        assert applier._front_tab() is a.page and b.job_id in applier._behind
        b.active_at -= 3600  # (long before a was done)
        a.active_at -= 3600
        applier.later(a.job_id)  # the person is done with a's
        applier._moved_on, applier._sleep = not_moved, sleep
        await applier._tick()

    run(go())
    assert fronted == ["acme", "litho"], fronted
    assert b.blocking and not b.left and time.time() - b.active_at < 60 and not applier._behind


def test_a_new_tab_a_job_opens_never_stays_over_the_tab_the_person_is_at():
    """A job filled in a tab of its own while the person is at another job's sign-in: a new tab its click opens comes
    to the front of the browser window, and the desk brings the one the person is at back up over it at once. A tab
    Claude's tools (or the person) open doesn't."""
    from job_apply.browser import BrowserSession

    fronted: list = []

    class Tab:
        context = None

        def __init__(self, name):
            self.name = name

        def on(self, event, handler):
            pass

        def is_closed(self):
            return False

        async def bring_to_front(self):
            fronted.append(self.name)

    async def go():
        session = BrowserSession()
        keep, opener, mine = Tab(name="sign-in"), Tab(name="job"), Tab(name="claude")
        session._own.page = mine
        with session.desk_view(7) as view:
            view.page = opener
            session.keep_front = lambda: keep
            session._on_popup(mine, Tab(name="claude's popup"))  # (Claude's tab: left as it is)
            await asyncio.sleep(0.05)
            assert fronted == []
            session._on_popup(opener, Tab(name="the job's popup"))
            await asyncio.sleep(0.05)
            assert fronted == ["sign-in"]
            session.keep_front = lambda: None  # (no one at a job's tab)
            session._on_popup(opener, Tab(name="another popup"))
            await asyncio.sleep(0.05)
        return fronted

    assert asyncio.run(go()) == ["sign-in"]


def test_each_job_filled_at_once_has_its_own_view_of_the_browser(srv):
    """Two jobs filled at once each act in their own tab: what one reads (its boxes, by id) and the tab it's on are its
    own, never the other's, however their steps interleave; and Claude's tools keep their own tab meanwhile."""
    async def go():
        claude = await srv.browser.new_tab()
        await srv.browser.goto(fixture_url("site/step1.html"))

        async def job(job_id, page_name, before, after):
            with srv.browser.desk_view(job_id):
                await srv.browser.new_tab()
                srv.browser.current_job_id = job_id
                await srv.browser.goto(fixture_url(page_name))
                await asyncio.sleep(before)
                data = await srv.browser.inspect(include_dropdown_options=False)
                await asyncio.sleep(after)  # (the other job reads its own page meanwhile)
                kept = [f["label"] for f in srv.browser._fields.values()]
                return srv.browser.current_tab, data["url"], kept == [f["label"] for f in data["fields"]], \
                    srv.browser.current_job_id

        one, two = await asyncio.gather(job(1, "generic_form.html", 0.1, 1.0), job(2, "site/signin.html", 0.5, 0.1))
        return claude, one, two, srv.browser.current_tab

    claude, one, two, after = run(go())
    assert one[0] is not two[0] and claude not in (one[0], two[0])
    assert one[1].endswith("generic_form.html") and two[1].endswith("signin.html")
    assert one[2] and two[2] and (one[3], two[3]) == (1, 2)
    assert after is claude  # Claude's tools are on their own tab still


def test_background_tabs_are_launched_never_to_be_throttled(srv, fake_launch):
    """Jobs filled at once work in tabs behind the one in front: the browser is launched so those run as the one in
    front does (Playwright's own defaults have these too, and its focus emulation keeps each page counted as shown: a
    headed window, a tab behind another, ran its timers and animation frames on, and a click, typing, a file chooser
    and a screenshot all worked there). Never a --disable-features of its own, which would drop Playwright's."""
    from job_apply import browser as browser_module

    launched = fake_launch("BrowserType.launch_persistent_context: Executable doesn't exist")
    with pytest.raises(browser_module.BrowserUnavailable):
        run(browser_module.BrowserSession()._launch())
    assert launched and all(set(browser_module.BACKGROUND_TABS_RUN) <= set(kw["args"]) for kw in launched)
    assert not any(a.startswith("--disable-features") for kw in launched for a in kw["args"])
    assert all("ignore_default_args" not in kw for kw in launched)


# ------------------------------------------------------------------ jobs at once: the review of f14a004

def _steps(company: str, base: str) -> dict:
    """A three-step application at `base` (each step's Next a POST to the next), its review page last."""
    return {base: _form(company, action=f"{urlparse(base).path}/2", button="Next"),
            ("POST", f"{base}/2"): _form(company, action=f"{urlparse(base).path}/3", button="Next"),
            ("POST", f"{base}/3"): _form(company)}


@pytest.mark.parametrize("at_once", [1, 2])
def test_a_submit_pressed_in_a_review_round_goes_while_jobs_are_under_way(srv, monkeypatch, at_once):
    """The review of jobs at once (HIGH): the person presses "Submit this one" on a review page in the desk's round, and
    the page brings up the next one at once, so the round goes on. Jobs under way were held at their next step for the
    whole round, keeping their places, and the Submit waited for one until the round ended. Now they go on in their
    own tabs; with two or more at once the Submit goes beside them at once, and with one, as soon as the pass under way
    is over, as it always did."""
    from types import SimpleNamespace

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    started: dict = {}
    under_urls = [f"https://careers.employer{n}.example/apply/{n}" for n in range(at_once)]

    async def submit(self, r, by_person=True):
        started[r.job_id] = (time.monotonic(), [self.runs[j].status for j in under])
        r.status = "submitted"

    monkeypatch.setattr(Applier, "_submit", submit)
    pages: dict = {}
    for n, url in enumerate(under_urls):
        pages.update(_steps(f"Employer {n}", url))
    under = [srv.add_job(url=url, title="Technician", company=f"Employer {n}")["job"]["id"]
             for n, url in enumerate(under_urls)]
    reviewed = [srv.add_job(url=f"https://careers.reviewed{n}.example/job/{n}", title="FSE",
                            company=f"Reviewed {n}")["job"]["id"] for n in range(2)]
    applier = Applier(srv)
    applier.jobs_at_once = at_once

    async def go():
        await _serve_slowly(srv, pages, [], 1.0)
        tab = SimpleNamespace(is_closed=lambda: True, url="")
        r1, r2 = (applier.runs.setdefault(j, Run(j, "FSE", f"Reviewed {n}", status="ready", page=tab))
                  for n, j in enumerate(reviewed))
        applier.start()
        try:
            runs = [applier.enqueue(j) for j in under]
            await until(lambda: all(r.status == "running" for r in runs), about=lambda: [state(r)() for r in runs])
            applier.start_round(r1.job_id)  # the person reviews the first
            t0 = time.monotonic()
            applier.submit_now(r1.job_id)  # "Submit this one"
            await asyncio.sleep(0.3)
            applier.start_round(r2.job_id)  # the page brings the next one up at once: the round goes on
            await until(lambda: r1.job_id in started, timeout=25, about=lambda: [state(r)() for r in runs])
            in_round = applier._in_round()
            await until(lambda: all(r.status == "ready" for r in runs), timeout=40,
                        about=lambda: [state(r)() for r in runs])
            return t0, in_round, runs
        finally:
            await applier.stop()

    t0, in_round, runs = run(go())
    when, statuses = started[reviewed[0]]
    assert in_round, "the round ended before the Submit went"
    if at_once > 1:
        assert when - t0 < 3 and all(s == "running" for s in statuses), (when - t0, statuses)  # beside them, at once
    else:
        assert statuses == ["ready"], statuses  # as soon as the pass under way was over
    assert not any(line.startswith("waiting") for r in runs for line in r.log), [r.log for r in runs]


def test_a_code_sent_as_a_job_took_the_inbox_is_its_own_not_an_earlier_jobs(srv, monkeypatch):
    """The review of jobs at once (HIGH): job B took the inbox and pressed Next (two Oracle tenants mail their codes
    from one address), and paused on its code a while later. Its own look went back only from its pause, so it missed
    its code, and an earlier job waiting on the same sender took it as its own: a code typed into another job's form.
    A job's mail begins as its step that may email did: the earlier job's mail ends there. Its own is looked for from
    shortly before its pause still, as on its own: what came between is either job's (the other's Resend, pressed in
    its tab meanwhile), and left for the person (the final check of jobs at once)."""
    from types import SimpleNamespace

    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    now = time.time()
    inbox = [("oracle.com", "111111", now - 290), ("oracle.com", "222222", now - 100)]  # the earlier job's, B's

    def search(address, password, since, senders, want, allowed_link, before=None, look_back=120, others=None):
        got = [m for m in inbox if m[2] >= since - look_back and (before is None or m[2] < before)]
        m = max(got, key=lambda m: m[2]) if got else None
        return pipeline.mailbox.Found("code", m[1], m[0], m[2]) if m else None

    monkeypatch.setattr(pipeline.mailbox, "search", search)
    monkeypatch.setattr(pipeline, "_another_employers_mail", lambda *a, **k: None)
    typed: list = []

    async def enter(self, r, found):
        typed.append((r.company, found.value))
        return True

    monkeypatch.setattr(Applier, "_enter_code", enter)
    page = SimpleNamespace(is_closed=lambda: False, url="https://x.example/")

    async def peek(tab, max_chars=4000):
        return {"url": tab.url, "fields": [{"kind": "text", "label": "Verification code", "id": "c"}]}, ""

    monkeypatch.setattr(srv.browser, "peek", peek)
    earlier_job = srv.add_job(url="https://eaaa.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/A/job/1",
                              title="FSE", company="Alpha")["job"]["id"]
    later_job = srv.add_job(url="https://ebbb.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/B/job/2",
                            title="FSE", company="Beta")["job"]["id"]
    applier = Applier(srv)
    earlier = applier.runs[earlier_job] = Run(earlier_job, "FSE", "Alpha", status="needs_you", need="email_code",
                                              left=True, paused_at=now - 300, page=page)
    earlier.url = "https://eaaa.fa.us2.oraclecloud.com/x"
    b = applier.runs[later_job] = Run(later_job, "FSE", "Beta", status="needs_you", need="email_code", blocking=True,
                                      paused_at=now - 10, page=page)
    b.url = "https://ebbb.fa.us2.oraclecloud.com/x"
    b.inbox_since = now - 120  # it took the inbox, and its Next sent the code; it paused well after
    pipeline._take_code(("oracle.com", "111111", now - 290), 999)  # (the earlier job's own code, used up already)
    run(applier._check_mail(b))
    run(applier._check_mail(earlier))
    assert typed == [], (typed, b.mail_last, earlier.mail_last)  # (in neither job's form)
    inbox.append(("oracle.com", "333333", now - 5))  # B's code sent again (its Resend), as it waits on it
    run(applier._check_mail(earlier))
    run(applier._check_mail(b))
    assert typed == [("Beta", "333333")], (typed, b.mail_last, earlier.mail_last)


def test_a_password_reset_isnt_called_overdue_while_its_mail_couldnt_be_read(srv, monkeypatch):
    """The review of jobs at once (MEDIUM): a job waiting on its password reset email, with the inbox watched, wasn't
    looked for while another job under way was signing in; yet in the same pass its wait was called out ("no reset
    email: most likely no account there") and it went off to make one. Its wait is over only once a look made after it
    has found nothing, and the time its looks were held off doesn't count; nor does it for the inbox's own window."""
    from types import SimpleNamespace

    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    looked: list = []
    monkeypatch.setattr(pipeline.mailbox, "search", lambda *a, **k: looked.append(1))
    monkeypatch.setattr(pipeline, "_another_employers_mail", lambda *a, **k: None)

    async def peek(tab, max_chars=4000):
        return {"url": tab.url, "fields": []}, ""

    monkeypatch.setattr(srv.browser, "peek", peek)
    monkeypatch.setattr(srv.browser, "lost", lambda tab: "")
    tab = SimpleNamespace(is_closed=lambda: False, url="https://c.example/reset")
    jobs = [srv.add_job(url=f"https://{n}.example/job", title="FSE", company=n.title())["job"]["id"] for n in ("c", "b")]
    applier = Applier(srv)

    async def not_past(r):
        return False

    applier._past_pause = not_past
    c = applier.runs[jobs[0]] = Run(jobs[0], "FSE", "C", status="needs_you", need="email_code", left=True, page=tab,
                                    paused_at=time.time() - pipeline.RESET_MAIL_WAIT - 5)
    c.resetting, c.reset_waited = True, True
    b = applier.runs[jobs[1]] = Run(jobs[1], "FSE", "B", status="running", page=tab)
    b.inbox = True  # signing in, in its own tab
    applier._slots[b.job_id] = object()
    run(applier._pick_up_left())
    assert looked == [] and not c.reset_no_mail and c.status == "needs_you", (c.mail_last, c.reset_no_mail)
    c.mail_held_at -= 600  # (ten minutes of it)
    assert pipeline._mail_waited(c) < pipeline.RESET_MAIL_WAIT
    b.inbox = False  # into its application: C's mail is read again, its wait goes on from where it was
    run(applier._pick_up_left())
    assert looked == [1] and not c.reset_no_mail and c.mail_held >= 600
    c.paused_at -= pipeline.RESET_MAIL_WAIT + 600  # (the wait's own time up: the next look that finds nothing ends it)
    run(applier._pick_up_left())
    assert looked == [1, 1] and c.reset_no_mail and c.status == "queued", (c.status, c.log[-2:])


def _waiting_on_resets(srv, monkeypatch, inbox: list) -> tuple:
    """Two KLA jobs waiting on their password reset's email, each asked for 3 seconds before it paused, with the inbox
    watched (`inbox`: what it holds, the newest last). The applier, the two runs, what each search asked for (want,
    since) and the links opened in their tabs."""
    from types import SimpleNamespace

    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    monkeypatch.setattr(pipeline, "_another_employers_mail", lambda *a, **k: None)
    asked: list = []

    def search(address, password, since, senders, want, allowed_link, before=None, look_back=120, others=None):
        asked.append((want, since))
        return inbox[-1] if want == "reset" and inbox else None

    monkeypatch.setattr(pipeline.mailbox, "search", search)

    async def peek(tab, max_chars=4000):
        return {"url": tab.url, "fields": []}, ""

    monkeypatch.setattr(srv.browser, "peek", peek)
    opened: list = []

    def tab():
        async def goto(url, **kwargs):
            opened.append(url)

        return SimpleNamespace(is_closed=lambda: False, url="https://kla.wd1.myworkdayjobs.com/Search/login", goto=goto)

    url = "https://kla.wd1.myworkdayjobs.com/Search/job/Phoenix-AZ/Customer-Service-Engineer_2637545"
    applier = Applier(srv)
    runs = []
    now = time.time()
    for n in (1, 2):
        job_id = srv.add_job(url=f"{url}?n={n}", title="Customer Service Engineer", company="KLA")["job"]["id"]
        r = applier.runs[job_id] = Run(job_id, "Customer Service Engineer", "KLA", url=url, status="needs_you",
                                       need="email_code", left=True, paused_at=now, page=tab())
        r.resetting = r.reset_waited = True
        r.reset_at = now - 3  # (its button pressed a moment before it paused)
        runs.append(r)
    return applier, runs, asked, opened


def test_a_reset_email_is_looked_for_from_when_the_desk_asked_for_it(srv, monkeypatch):
    """KLA's Workday, the reset probe's second pass a minute after the first (run 136): the newest reset email in the
    two minutes before its pause was the first pass's, its link spent. A reset's email is looked for from when the desk
    pressed the site's button (mailbox.search keeps to a few seconds before that), not from the pause; an emailed
    code's still from the pause, as before."""
    applier, (r, other), asked, opened = _waiting_on_resets(srv, monkeypatch, [])
    run(applier._check_mail(r))
    assert asked == [("reset", r.reset_at)] and r.reset_at < r.paused_at, (asked, r.mail_last)
    other.resetting = False  # (waiting on a code or a confirmation link instead)
    run(applier._check_mail(other))
    assert asked[1:] == [("link", other.paused_at)], asked


def test_a_reset_link_opened_once_is_never_opened_again(srv, monkeypatch):
    """Run 136 again: the first pass's reset link, opened again, set nothing (a reset's link works once). A reset link
    opened once, for this job or another, is passed over, and the new one waited for and opened."""
    first_link = "https://kla.wd1.myworkdayjobs.com/Search/passwordReset?token=first"
    inbox: list = []
    applier, (first, second), asked, opened = _waiting_on_resets(srv, monkeypatch, inbox)
    inbox.append(pipeline.mailbox.Found("reset", first_link, "workday.kla.com", first.reset_at + 1))
    run(applier._check_mail(first))
    assert opened == [first_link] and first.mail_done, (opened, first.mail_last)
    run(applier._check_mail(second))  # the same email: its link spent, passed over
    assert opened == [first_link] and not second.mail_done and "opened already" in second.mail_last, second.mail_last
    second_link = "https://kla.wd1.myworkdayjobs.com/Search/passwordReset?token=second"
    inbox.append(pipeline.mailbox.Found("reset", second_link, "workday.kla.com", time.time()))  # its own, come since
    run(applier._check_mail(second))
    assert opened == [first_link, second_link] and second.mail_done, second.mail_last


def test_a_reset_email_from_before_the_request_waits_a_moment_for_a_newer_one(srv, monkeypatch):
    """The review of 0.3.209: a reset's email is looked for as far back as a code's (the two clocks can be apart), and
    one from before the desk asked for the reset, by this computer's clock, may be an earlier reset's, its link spent
    (run 136: the first pass's, opened again, set nothing). One from a little before is this one's with the clocks apart:
    a newer one is waited for a moment, and used when it comes. One from further back (RESET_CLOCK_SKEW) is an earlier
    reset's: never opened, however long nothing newer comes (the re-review: after the wait it was), and its link was the
    account's, so the wait running out is said, never taken for no email (reset_spent)."""
    inbox: list = []
    applier, (r, _), asked, opened = _waiting_on_resets(srv, monkeypatch, inbox)
    stale = "https://kla.wd1.myworkdayjobs.com/Search/passwordReset?token=stale"
    inbox.append(pipeline.mailbox.Found("reset", stale, "workday.kla.com", r.reset_at - 60))
    run(applier._check_mail(r))
    assert opened == [] and not r.mail_done and "an earlier reset's" in r.mail_last, r.mail_last
    r.reset_at -= pipeline.RESET_FRESH_WAIT  # (the moment gone by, and nothing newer)
    inbox[-1] = pipeline.mailbox.Found("reset", stale, "workday.kla.com", r.reset_at - 60)
    run(applier._check_mail(r))
    assert opened == [] and not r.mail_done and r.reset_spent, (opened, r.mail_last)
    r.reset_at += pipeline.RESET_FRESH_WAIT
    near = "https://kla.wd1.myworkdayjobs.com/Search/passwordReset?token=near"
    inbox.append(pipeline.mailbox.Found("reset", near, "workday.kla.com", r.reset_at - 20))
    run(applier._check_mail(r))
    assert opened == [] and not r.mail_done and "a newer one is waited for" in r.mail_last, r.mail_last
    fresh = "https://kla.wd1.myworkdayjobs.com/Search/passwordReset?token=fresh"
    inbox.append(pipeline.mailbox.Found("reset", fresh, "workday.kla.com", r.reset_at + 4))
    run(applier._check_mail(r))
    assert opened == [fresh] and r.mail_done, (opened, r.mail_last)


def test_a_reset_email_that_looks_early_by_this_computers_clock_is_used_after_the_wait(srv, monkeypatch):
    """The review of 0.3.209: with this computer's clock 40 seconds fast, the reset email the site sent for this request
    arrived "before" it, by the mail service's clock, and 15 seconds back missed it. With nothing newer in
    RESET_FRESH_WAIT, it's the one: opened."""
    inbox: list = []
    applier, (r, _), asked, opened = _waiting_on_resets(srv, monkeypatch, inbox)
    link = "https://kla.wd1.myworkdayjobs.com/Search/passwordReset?token=skewed"
    inbox.append(pipeline.mailbox.Found("reset", link, "workday.kla.com", r.reset_at - 40))
    run(applier._check_mail(r))
    assert opened == [] and not r.mail_done, r.mail_last  # (a newer one waited for a moment)
    r.reset_at -= pipeline.RESET_FRESH_WAIT  # (that moment gone by, and nothing newer)
    run(applier._check_mail(r))
    assert opened == [link] and r.mail_done, (opened, r.mail_last)


@pytest.mark.parametrize("said, refused", [
    # a reset that went in, and the page after it (the review of 0.3.209): no refusal
    ("Sign In\nYour session has expired. Please sign in again.", False),
    ("Your session expired due to inactivity. Sign in to continue.", False),
    ("Sign In\nFor security, all sessions were ended. Session expired - please log in again with your new password",
     False),
    ("Sign In\nEmail Address\nPassword\nForgot your password?\nPasswords must not match your last 5 passwords", False),
    ("Sign In\nCreate Account\nRequests for reasonable accommodation: link below. Offers expired after 7 days", False),
    ("Must hold a valid, non-expired driver's license", False),
    ("Invalid email address or password", False),
    ("Invalid request", False),
    ("Your password has expired", False),  # (a reset's own form: a new one is wanted)
    ("Your password has been reset. This link has expired.", False),  # (it says the password is reset)
    # the link spent, or the password one the site had before
    ("This link has expired. Request a new password reset from the sign-in page.", True),
    ("The password reset link is invalid or has expired.", True),
    ("Invalid Password Reset Request", True),
    ("This password reset request is no longer valid.", True),
    ("Your password reset token has expired", True),
    ("The link you clicked has already been used.", True),
    ("This reset link is no longer active.", True),
    ("Sorry, this link is not valid anymore.", True),
    ("The password reset has timed out. Please request a new one.", True),
    ("Error: Password was recently used.", True),
    ("You can't reuse a recent password.", True),
    ("You cannot use a password that you have used before.", True),
    ("Password cannot be the same as your previous 5 passwords.", True),
    ("New password must be different from your current password.", True),
    # (the re-review: help that only says what to do if it were, and three more refusals)
    ("If this link has expired, request a new password reset from the sign-in page.", False),
    ("Note: the link may be invalid in some mail apps; copy it into your browser.", False),
    ("The new password cannot be the same as a previous password.", True),
    ("This request has expired.", True),
    ("Password Reset Failed", True)])
def test_what_a_reset_page_says_of_a_password_it_didnt_set(said, refused):
    """Only words said of a link, a token, a reset or a password, and never the site's password rules shown beside its
    boxes (but in an alert: test_a_reset_alert_that_says_the_rules_is_a_refusal)."""
    assert bool(pipeline._reset_refused({}, said)) == refused, pipeline._reset_refused({}, said)


def test_a_reset_alert_that_says_the_rules_is_a_refusal():
    rules = "Passwords must not match your last 5 passwords"
    assert pipeline._reset_refused({"errors": [rules]}, "") == rules
    assert pipeline._reset_refused({"headings": [rules]}, rules) == ""


def test_a_look_in_the_inbox_begun_before_another_job_took_it_isnt_used(srv, monkeypatch):
    """The review of jobs at once (MEDIUM): between the check that no other job is in an email step and the search
    itself come a look at the tab, the employer lists and the login, seconds in all; a job that took the inbox meanwhile
    may have had a code sent that this look finds. What it found isn't used: it's looked for again later."""
    from types import SimpleNamespace

    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    monkeypatch.setattr(pipeline, "_another_employers_mail", lambda *a, **k: None)
    applier = Applier(srv)

    def search(*args, **kwargs):
        applier._inbox_gen += 1  # (another job takes the inbox as the inbox is read)
        return pipeline.mailbox.Found("code", "333333", "oracle.com", time.time())

    monkeypatch.setattr(pipeline.mailbox, "search", search)
    typed: list = []

    async def enter(self, r, found):
        typed.append(found.value)
        return True

    monkeypatch.setattr(Applier, "_enter_code", enter)

    async def peek(tab, max_chars=4000):
        return {"url": tab.url, "fields": [{"kind": "text", "label": "Verification code", "id": "c"}]}, ""

    monkeypatch.setattr(srv.browser, "peek", peek)
    job = srv.add_job(url="https://eaaa.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/A/job/1",
                      title="FSE", company="Alpha")["job"]["id"]
    r = applier.runs[job] = Run(job, "FSE", "Alpha", status="needs_you", need="email_code", blocking=True,
                                paused_at=time.time(), page=SimpleNamespace(is_closed=lambda: False, url="https://x/"))
    run(applier._check_mail(r))
    assert typed == [] and not r.mail_done and r.mail_last.startswith("not used"), r.mail_last


def test_a_job_on_a_system_that_emails_codes_holds_the_inbox_from_its_start(srv, monkeypatch):
    """The review of jobs at once (MEDIUM): "one email step at a time" rested on the desk knowing each step that may
    send mail, and some do unlooked for (Workday's sign-in step drawn without its buttons, Oracle's email step with a
    second box). A job on a job system that emails codes holds the inbox from the start of its pass until its
    application's first form: it doesn't begin while another job is in an email step. Others go as before."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    began: dict = {}

    async def drive(self, r):
        began[r.job_id] = (r.inbox, time.monotonic())
        r.status = "ready"

    monkeypatch.setattr(Applier, "_drive", drive)
    workday = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/Chandler/Technician_1",
                          title="Technician", company="Acme")["job"]["id"]
    own_site = srv.add_job(url="https://careers.example-litho.example/job/2", title="Technician",
                           company="Example Litho")["job"]["id"]
    holder_job = srv.add_job(url="https://careers.acme-fab.example/apply/1", title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)
    applier.jobs_at_once = 3

    async def go():
        holder = applier.runs[holder_job] = Run(holder_job, "FSE", "Acme Fab", status="running")
        holder.inbox = True  # signing in, in its own tab
        busy = asyncio.ensure_future(asyncio.sleep(3600))
        applier._slots[holder_job] = busy
        applier.start()
        try:
            w = applier.enqueue(workday)
            applier.enqueue(own_site)
            await until(lambda: own_site in began)
            await asyncio.sleep(1)
            held = (workday in began, list(w.log))
            holder.inbox = False  # into its application
            await until(lambda: workday in began)
            return held
        finally:
            busy.cancel()
            await applier.stop()

    held = run(go())
    assert held[0] is False and "waiting: Acme Fab is signing in or waiting on an email; this one carries on after " \
        "that" in held[1], held
    assert began[workday][0] is True and began[own_site][0] is False, began


def test_a_submit_the_person_pressed_goes_beside_another_job_at_its_site(srv):
    """The review of jobs at once (LOW): a Submit the person pressed waited for any job under way at its employer's
    site, for that job's whole pass, though it only presses Submit in its own tab. It goes, unless the job there is
    in an email step on the very host its tab is on."""
    from types import SimpleNamespace

    def tab(url):
        return SimpleNamespace(is_closed=lambda: False, url=url)

    jobs = [srv.add_job(url=f"https://acme.wd1.myworkdayjobs.com/External/job/{n}", title="FSE",
                        company="Acme")["job"]["id"] for n in range(2)]
    applier = Applier(srv)
    ready = applier.runs[jobs[0]] = Run(jobs[0], "FSE", "Acme", status="ready",
                                        page=tab("https://acme.wd1.myworkdayjobs.com/External/review"))
    other = applier.runs[jobs[1]] = Run(jobs[1], "FSE", "Acme", status="running",
                                        page=tab("https://acme.wd1.myworkdayjobs.com/External/job/1/apply"))
    applier._slots[other.job_id] = object()
    assert not applier._site_free(ready.job_id)  # (a new pass of it would wait, as ever)
    assert applier._submit_free(ready.job_id)
    other.inbox = True  # signing in there
    assert not applier._submit_free(ready.job_id)
    other.page = tab("https://login.example-idp.example/signin")  # (elsewhere)
    assert applier._submit_free(ready.job_id)


def test_the_queue_never_looks_at_a_tab_whose_pass_is_still_saving_its_stop(srv, monkeypatch):
    """The review of jobs at once (LOW): a job paused for its code is still in its pass while the page it stopped on is
    saved in its tab; the queue looked at that tab meanwhile, and could have typed the code from the inbox into it as
    it was saved. A job's tab is looked at once its pass is over."""
    from types import SimpleNamespace

    applier = Applier(srv)
    looked: list = []

    async def moved_on(r):
        looked.append(("moved", r.job_id))
        return False

    async def check_mail(r):
        looked.append(("mail", r.job_id))

    async def sleep(seconds):
        pass

    applier._moved_on, applier._check_mail_safely, applier._sleep = moved_on, check_mail, sleep
    monkeypatch.setattr(srv.browser, "lost", lambda tab: "")
    jobs = [srv.add_job(url=f"https://{n}.example/job", title="FSE", company=n.title())["job"]["id"] for n in ("a", "b")]
    tab = SimpleNamespace(is_closed=lambda: False, url="https://a.example/code")
    holder = applier.runs[jobs[0]] = Run(jobs[0], "FSE", "A", status="running", page=tab)
    applier._pause(holder, "email_code", "The site emailed you a code.", code="email_code")
    left = applier.runs[jobs[1]] = Run(jobs[1], "FSE", "B", status="running", page=tab)
    applier._pause(left, "email_code", "The site emailed you a code.", code="email_code")
    left.blocking, left.left = False, True
    applier._slots = {holder.job_id: object(), left.job_id: object()}  # (both still saving their stops)
    applier.jobs_at_once = 3
    run(applier._tick())
    assert looked == [], looked
    applier._slots.clear()
    run(applier._tick())
    assert ("mail", holder.job_id) in looked and ("moved", holder.job_id) in looked, looked


def test_a_stop_isnt_brought_over_a_sign_in_the_person_is_still_at_after_the_queue_went_on(srv):
    """The review of jobs at once (LOW): a sign-in the queue went on without (Later, or five idle minutes), that the
    person is at work in again: another job's stop (a bot check, a stuck page) brought its tab over it as they typed.
    It comes up once they've left that tab be."""
    from types import SimpleNamespace

    fronted: list = []

    def tab(name):
        async def front():
            fronted.append(name)
        return SimpleNamespace(is_closed=lambda: False, bring_to_front=front, url=f"https://{name}.example/signin")

    jobs = [srv.add_job(url=f"https://{n}.example/job", title="FSE", company=n.title())["job"]["id"] for n in ("a", "b")]
    applier = Applier(srv)
    signing = applier.runs[jobs[0]] = Run(jobs[0], "FSE", "A", status="running", page=tab("a"))
    applier._pause(signing, "sign_in", "Sign in there.", code="account_theirs")
    applier.later(signing.job_id)  # the queue went on without it; the person is at it still
    signing.paused_at -= 30
    applier._note_activity(signing, {"url": signing.page.url, "fields": [{"id": "email", "value": ""}]})
    applier._note_activity(signing, {"url": signing.page.url, "fields": [{"id": "email", "value": "a@"}]})  # typing
    other = applier.runs[jobs[1]] = Run(jobs[1], "FSE", "B", status="running", page=tab("b"))
    run(applier._bring_forward(other))
    applier._pause(other, "bot_check", "A bot check.", code="bot_check")
    assert fronted == [] and applier._front_tab() is signing.page
    signing.active_at -= 3600  # nothing done in its tab for an hour
    run(applier._front_next())
    assert fronted == ["b"]


def test_a_saved_drafts_changes_stay_its_own_with_another_job_filled_alongside(srv, monkeypatch):
    """Two at once, one of them a saved draft (Workday's "Continue Application") whose answers the desk set right: what
    it changed (its card's lines, its hold on Submit for me, the record kept in its folder) is that job's alone; the
    job filled alongside it has none of it."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    draft = srv.add_job(url=fixture_url("site/workday-continue-posting.html"), title="Equipment Technician",
                        company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)
    applier.jobs_at_once = 2
    passes = _passes(monkeypatch)

    async def go():
        applier.start()
        try:
            d, o = applier.enqueue(draft["id"]), applier.enqueue(other["id"])
            await until(lambda: d.status not in ("queued", "running") and o.status not in ("queued", "running"),
                        about=lambda: (state(d)(), state(o)()))
            return d, o
        finally:
            await applier.stop()

    d, o = run(go())
    assert _overlap(passes, draft["id"], other["id"]), passes
    assert d.draft and d.draft_changed and d.public()["went_in"]["changed"], (d.reason, d.log)
    assert not o.draft and not o.draft_changed and not (o.went_in.changed if o.went_in else []), o.log
    assert not any("draft" in line for line in o.log), o.log
    folder = Path(srv.tracker().get(draft["id"])["folder"])
    assert (folder / pipeline.DRAFT_FILE).is_file()
    assert not (Path(srv.tracker().get(other["id"])["folder"]) / pipeline.DRAFT_FILE).exists()


# ------------------------------------------------------------------ jobs at once: the re-check of 3df60ad
def _fake_inbox(monkeypatch, inbox):
    """The inbox as a list of (sender, code, received): search's newest in its window, as mailbox.search."""
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    monkeypatch.setattr(pipeline, "_another_employers_mail", lambda *a, **k: None)

    def search(address, password, since, senders, want, allowed_link, before=None, look_back=120, others=None):
        got = [m for m in inbox if m[2] >= since - look_back and (before is None or m[2] < before)]
        m = max(got, key=lambda m: m[2]) if got else None
        return pipeline.mailbox.Found("code", m[1], m[0], m[2]) if m else None

    monkeypatch.setattr(pipeline.mailbox, "search", search)
    typed: list = []

    async def enter(self, r, found):
        typed.append((r.company, found.value))
        return True

    monkeypatch.setattr(Applier, "_enter_code", enter)
    return typed


def _code_page(monkeypatch, srv, box=None):
    """A paused tab on an emailed code's page (its box's value in `box`, when given)."""
    from types import SimpleNamespace

    async def peek(tab, max_chars=4000):
        field = {"kind": "text", "label": "Verification code", "id": "c", "value": (box or {}).get("value", "")}
        return {"url": tab.url, "fields": [field]}, ""

    monkeypatch.setattr(srv.browser, "peek", peek)

    async def front():
        pass

    return SimpleNamespace(is_closed=lambda: False, url="https://x.example/verify", bring_to_front=front)


def _oracle_job(srv, name):
    return srv.add_job(url=f"https://e{name.lower()}.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/A/job/1",
                       title="FSE", company=name)["job"]["id"]


def test_a_pass_on_a_system_that_emails_holds_the_inbox_but_its_mail_begins_at_its_email_step(srv, monkeypatch):
    """The re-check of jobs at once (MEDIUM): a job on a job system that emails codes holds the inbox from the start of
    its pass, and its mail was taken to begin there too, minutes before any step of it that may email. Its mail begins
    as such a step does (_take_inbox from _drive), as its code is sent no earlier."""
    seen: list = []

    async def drive(self, r):
        seen.append((r.inbox, r.inbox_since))
        await self._take_inbox(r)  # (its sign-in step)
        seen.append((r.inbox, r.inbox_since > 0))
        r.status = "ready"

    monkeypatch.setattr(Applier, "_drive", drive)
    monkeypatch.setattr(Applier, "_save_stop", lambda self, r: asyncio.sleep(0))
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/Chandler/Technician_1",
                      title="Technician", company="Acme")["job"]["id"]
    applier = Applier(srv)
    r = applier.runs[job] = Run(job, "Technician", "Acme", status="running")
    run(applier._run_task("apply", r))
    assert seen == [(True, 0.0), (True, True)], seen


def test_an_untaken_code_from_before_a_jobs_email_step_isnt_its_own(srv, monkeypatch):
    """The re-check of jobs at once (MEDIUM): one at a time, a job whose step that may email began minutes before its
    code step had its mail looked for from there: the code of the job filled before it, from the same address, put in
    there by the person by hand (so never marked as used), was typed into its form. Its mail is looked for no further
    back than on its own (LOOK_BACK before its pause), however early that step began."""
    now = time.time()
    inbox = [("oracle.com", "999999", now - 350)]  # the job before's, put in by the person
    typed = _fake_inbox(monkeypatch, inbox)
    page = _code_page(monkeypatch, srv)
    job = _oracle_job(srv, "Beta")
    applier = Applier(srv)
    b = applier.runs[job] = Run(job, "FSE", "Beta", status="needs_you", need="email_code", blocking=True,
                                paused_at=now - 3, page=page)
    b.inbox_since = now - 300  # (its sign-in began five minutes before its code step)
    run(applier._check_mail(b))
    assert typed == [], (typed, b.mail_last)
    inbox.append(("oracle.com", "424242", now - 10))  # its own, sent as it came to its code step
    run(applier._check_mail(b))
    assert typed == [("Beta", "424242")], (typed, b.mail_last)


@pytest.mark.parametrize("step_began", [100, 12])
def test_another_jobs_resent_code_is_never_typed_into_a_job_waiting_on_the_same_sender(srv, monkeypatch, step_began):
    """The re-check and final check of jobs at once (MEDIUM, LOW): a job left waiting on an Oracle code, and another
    job's step that may email under way (begun 100 s, or 12 s, before it paused on its own code). The person pressed the
    left job's Resend meanwhile; the new code came 60 s before the other paused, before that one's own. It was typed
    into the other's form: its mail was looked for back to its step's start. While another job waits on the same
    sender, a job's own is looked for from shortly before its pause alone (as on its own); the Resend's code is the
    person's to put in, and its own is typed as it comes."""
    now = time.time()
    inbox = [("oracle.com", "777777", now - 60)]  # the left job's Resend's
    typed = _fake_inbox(monkeypatch, inbox)
    page = _code_page(monkeypatch, srv)
    earlier, later_job = _oracle_job(srv, "Alpha"), _oracle_job(srv, "Beta")
    applier = Applier(srv)
    left = applier.runs[earlier] = Run(earlier, "FSE", "Alpha", status="needs_you", need="email_code", left=True,
                                       paused_at=now - 500, page=page)
    b = applier.runs[later_job] = Run(later_job, "FSE", "Beta", status="needs_you", need="email_code", blocking=True,
                                      paused_at=now - 2, page=page)
    b.inbox_since = now - step_began
    run(applier._check_mail(b))
    run(applier._check_mail(left))
    assert ("Beta", "777777") not in typed, (typed, b.mail_last, left.mail_last)
    # (the left job's own: its mail ends where the other's step began, before the Resend's came in when that began
    # long before; or after it, and it's typed there, as on its own)
    assert typed == ([] if step_began == 100 else [("Alpha", "777777")]), (typed, left.mail_last)
    inbox.append(("oracle.com", "222222", now - 1))  # its own, come in now
    run(applier._check_mail(b))
    assert typed[-1:] == [("Beta", "222222")], (typed, b.mail_last)


@pytest.mark.parametrize("wait", ["reset", "verify"])
def test_a_wait_on_an_email_the_desk_reads_itself_isnt_the_person_at_its_tab(srv, wait):
    """The re-check of jobs at once (MEDIUM): a job left waiting on a password reset's (or a new account's
    verification) email that the desk reads from the inbox itself has nothing for the person in its tab; yet for a
    minute after it paused it was taken for the tab they're at: another job's sign-in wasn't brought up, and stayed
    behind it."""
    from types import SimpleNamespace

    fronted: list = []

    def tab(name):
        async def front():
            fronted.append(name)
        return SimpleNamespace(is_closed=lambda: False, bring_to_front=front, url=f"https://{name}.example/apply")

    jobs = [srv.add_job(url=f"https://{n}.example/job", title="FSE", company=n.title())["job"]["id"] for n in ("a", "b")]
    applier = Applier(srv)
    a = applier.runs[jobs[0]] = Run(jobs[0], "FSE", "A", status="running", page=tab("a"))
    applier._pause(a, "email_code", "Waiting on the email.", code="email_code")
    a.blocking, a.left = False, True  # (as _await_reset_email and _await_verify_email leave it, the inbox watched)
    if wait == "reset":
        a.resetting = a.reset_waited = True
    else:
        a.verifying = True
    b = applier.runs[jobs[1]] = Run(jobs[1], "FSE", "B", status="running", page=tab("b"))
    run(applier._bring_forward(b))
    applier._pause(b, "sign_in", "Sign in there.", code="account_theirs")
    assert applier._person_at(b) is None and fronted == ["b"] and not applier._behind, (fronted, applier._behind)
    assert applier._front_tab() is b.page


def test_the_desk_typing_a_left_jobs_emailed_code_isnt_the_person_at_its_tab(srv, monkeypatch):
    """The re-check of jobs at once (MEDIUM): a code the queue went on without, typed into its tab from the inbox by the
    desk itself: the tab changed, and that was taken for the person at work in it for a minute (no other job's tab
    brought up meanwhile). What the desk does there isn't the person's; what changes there once it has settled is."""
    now = time.time()
    typed = _fake_inbox(monkeypatch, [("oracle.com", "123456", now - 20)])
    box = {"value": ""}

    async def enter(self, r, found):
        typed.append((r.company, found.value))
        box["value"] = found.value
        return True

    monkeypatch.setattr(Applier, "_enter_code", enter)
    page = _code_page(monkeypatch, srv, box)
    job = _oracle_job(srv, "Alpha")
    applier = Applier(srv)
    left = applier.runs[job] = Run(job, "FSE", "Alpha", status="running", page=page)
    applier._pause(left, "email_code", "Enter the code we emailed you.", code="email_code")
    applier.later(job)  # the queue went on without it
    left.paused_at = left.active_at = time.time() - 300  # (nothing done in its tab since)
    left.inbox_since = 0.0

    def look():
        applier._note_activity(left, run(srv.browser.peek(page))[0])

    look()
    run(applier._check_mail(left))
    look()
    assert typed == [("Alpha", "123456")] and applier._person_at(None) is None, (typed, left.mail_last)
    assert applier._poll_for([left]) == pipeline.POLL_SECONDS  # (looked at soon still: past its code, it carries on)
    left.desk_at -= pipeline.DESK_SETTLE + 1  # (its page settled long since)
    box["value"] = "12345"  # the person at work in it
    look()
    assert applier._person_at(None) is left


def test_a_password_resets_wait_runs_on_through_the_inboxs_own_errors(srv, monkeypatch):
    """The re-check of jobs at once (LOW): a reset's wait was called over only once a look in the inbox made after it
    found nothing: an inbox that kept failing to be read (a mail service down) held it for ever. A look that fails is a
    look tried; only looks held off for another job's email step stop its wait."""
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    monkeypatch.setattr(pipeline, "_another_employers_mail", lambda *a, **k: None)

    def search(*args, **kwargs):
        raise pipeline.mailbox.MailboxError("reading imap.example.com failed: timed out")

    monkeypatch.setattr(pipeline.mailbox, "search", search)
    page = _code_page(monkeypatch, srv)
    job = srv.add_job(url="https://c.example/job", title="FSE", company="C")["job"]["id"]
    applier = Applier(srv)
    c = applier.runs[job] = Run(job, "FSE", "C", status="needs_you", need="email_code", left=True, page=page,
                                paused_at=time.time() - 60)
    c.resetting = c.reset_waited = True
    run(applier._check_mail(c))
    assert not applier._reset_overdue(c) and applier.mail_problem
    c.paused_at -= pipeline.RESET_MAIL_WAIT
    run(applier._check_mail(c))
    assert applier._reset_overdue(c), (c.mail_looked, c.paused_at, applier.mail_problem)


def _person_at_a_left_sign_in(srv, fronted):
    """A sign-in the queue went on without, the person at work in its tab; and another job's sign-in come up meanwhile,
    waiting its turn to be brought up (_behind)."""
    from types import SimpleNamespace

    def tab(name):
        async def front():
            fronted.append(name)
        return SimpleNamespace(is_closed=lambda: False, bring_to_front=front, url=f"https://{name}.example/signin")

    jobs = [srv.add_job(url=f"https://{n}.example/job", title="FSE", company=n.title())["job"]["id"] for n in ("a", "b")]
    applier = Applier(srv)
    signing = applier.runs[jobs[0]] = Run(jobs[0], "FSE", "A", status="running", page=tab("a"))
    applier._pause(signing, "sign_in", "Sign in there.", code="account_theirs")
    applier.later(signing.job_id)
    signing.paused_at -= 30
    applier._note_activity(signing, {"url": signing.page.url, "fields": [{"id": "email", "value": ""}]})
    applier._note_activity(signing, {"url": signing.page.url, "fields": [{"id": "email", "value": "a@"}]})
    b = applier.runs[jobs[1]] = Run(jobs[1], "FSE", "B", status="running", page=tab("b"))
    run(applier._bring_forward(b))
    applier._pause(b, "sign_in", "Sign in there.", code="account_theirs")
    assert b.blocking and b.job_id in applier._behind and fronted == []
    return applier, signing, b


def test_a_pause_waiting_its_turn_behind_the_persons_tab_isnt_let_go_as_idle(srv, monkeypatch):
    """The re-check of jobs at once (LOW): a sign-in that came up while the person was at another job's tab waits its
    turn to be brought up; its five idle minutes were counted from its pause, so a long turn let the queue go on
    without it before its tab was ever brought up. Its idle wait begins as its tab is brought up."""
    fronted: list = []
    applier, signing, b = _person_at_a_left_sign_in(srv, fronted)
    b.paused_at = b.active_at = time.time() - pipeline.HANDS_ON_IDLE - 10  # (the person at the other for a while)

    async def not_moved(r):
        return False

    async def sleep(seconds):
        pass

    monkeypatch.setattr(srv.browser, "lost", lambda tab: "")
    applier._moved_on, applier._past_pause, applier._sleep = not_moved, not_moved, sleep
    run(applier._tick())
    assert b.blocking and not b.left and b.job_id in applier._behind and fronted == [], (b.log[-1:], fronted)
    signing.active_at -= 3600  # (they've left that tab be)
    run(applier._tick())
    assert fronted == ["b"] and b.blocking and not applier._behind and time.time() - b.active_at < 60


def test_the_tab_kept_in_front_is_the_one_the_person_is_at_past_a_pause_waiting_its_turn(srv):
    """The re-check of jobs at once (LOW): with a pause waiting its turn to be brought up (_behind) the first that holds
    the queue, no tab was kept in front: a new tab another job opened stayed over the sign-in the person was at."""
    fronted: list = []
    applier, signing, b = _person_at_a_left_sign_in(srv, fronted)
    assert applier._front_tab() is signing.page


def test_a_solved_checks_place_beside_the_others_goes_with_its_task(srv):
    """The re-check of jobs at once (LOW): a job whose check was solved in the round may begin beside as many jobs as
    go at once (Applier._first); a Skip of it, or its task put in again, kept that place for its next task."""
    jobs = [srv.add_job(url=f"https://{n}.example/job", title="FSE", company=n.title())["job"]["id"] for n in ("a", "b")]
    applier = Applier(srv)
    applier._first.add(jobs[0])
    run(applier.skip(jobs[0]))
    assert jobs[0] not in applier._first
    applier._first.add(jobs[1])
    applier.enqueue(jobs[1])
    assert jobs[1] not in applier._first


# ------------------------------------------------------------------ jobs at once: the final check of 046c2fa
def test_two_pauses_waiting_their_turn_come_up_one_after_the_other(srv, monkeypatch):
    """The final check of jobs at once (LOW): two jobs paused for a sign-in while the person was at a third's, each
    waiting its turn to be brought up. Once that one was done each kept the other down (a pause holding the queue
    counted as the tab the person is at even while it waited its turn), and neither came up for 45 minutes. The first
    to pause comes up first and the next once it's done; one that pauses meanwhile waits its turn behind them."""
    from types import SimpleNamespace

    fronted: list = []

    def tab(name):
        async def front():
            fronted.append(name)
        return SimpleNamespace(is_closed=lambda: False, bring_to_front=front, url=f"https://{name}.example/signin")

    ids = [srv.add_job(url=f"https://{n}.example/job", title="FSE", company=n.title())["job"]["id"] for n in "abcd"]
    applier = Applier(srv)
    a, b, c, d = (Run(j, "FSE", n.upper(), status="running", page=tab(n)) for j, n in zip(ids, "abcd"))
    applier.runs = {r.job_id: r for r in (a, b, c, d)}
    applier._pause(a, "sign_in", "Sign in there.", code="account_theirs")  # the person's
    for r in (b, c):
        run(applier._bring_forward(r))
        applier._pause(r, "sign_in", "Sign in there.", code="account_theirs")
    assert {b.job_id, c.job_id} <= applier._behind and fronted == []
    a.blocking, a.status = False, "queued"  # the person got past its sign-in
    b.paused_at -= 1  # (b paused first)
    b.active_at = c.active_at = time.time() - pipeline.HANDS_ON_IDLE - 10

    async def not_moved(r):
        return False

    async def sleep(seconds):
        pass

    monkeypatch.setattr(srv.browser, "lost", lambda tab: "")
    applier._moved_on, applier._past_pause, applier._sleep = not_moved, not_moved, sleep
    for _ in range(3):
        run(applier._tick())
    assert fronted == ["b"] and c.job_id in applier._behind and b.job_id not in applier._behind, (fronted,
                                                                                                    applier._behind)
    assert b.blocking and c.blocking and not b.left and not c.left
    run(applier._bring_forward(d))  # (another pauses meanwhile: its turn comes after theirs)
    applier._pause(d, "sign_in", "Sign in there.", code="account_theirs")
    assert d.job_id in applier._behind and fronted == ["b"]
    b.blocking, b.status = False, "queued"  # the person got past b's
    run(applier._tick())
    assert fronted == ["b", "c"] and d.job_id in applier._behind and c.job_id not in applier._behind, fronted


def test_a_pause_isnt_let_go_as_idle_just_after_the_desk_put_its_code_in(srv, monkeypatch):
    """The final check of jobs at once (LOW): at five idle minutes, a sign-in's emailed code the desk put in from the
    inbox, turned down by the site; the person puts the right one in. The desk's typing no longer counted as anything
    done in its tab, so the queue went on without it then, and the next job's tab came up over the one they were
    typing in. As before jobs at once, its idle wait begins again as the desk puts a code in."""
    from types import SimpleNamespace

    job = srv.add_job(url="https://b.example/job", title="FSE", company="B")["job"]["id"]
    applier = Applier(srv)
    b = applier.runs[job] = Run(job, "FSE", "B", status="running",
                                page=SimpleNamespace(is_closed=lambda: False, url="https://b.example/verify"))
    applier._pause(b, "email_code", "Enter the code we emailed you.", code="email_code")
    b.paused_at = b.active_at = time.time() - pipeline.HANDS_ON_IDLE - 10
    b.desk_at = time.time() - 5  # the desk put its code in five seconds ago

    async def not_moved(r):
        return False

    async def sleep(seconds):
        pass

    monkeypatch.setattr(srv.browser, "lost", lambda tab: "")
    applier._moved_on, applier._past_pause, applier._sleep = not_moved, not_moved, sleep
    run(applier._tick())
    assert b.blocking and not b.left, b.log[-1:]
    b.desk_at -= pipeline.HANDS_ON_IDLE  # (as long ago as its pause: nothing since)
    run(applier._tick())
    assert b.left and not b.blocking


def test_the_right_code_the_person_puts_in_after_the_desks_is_them_at_its_tab(srv, monkeypatch):
    """The final check of jobs at once (LOW): changes in a tab for DESK_SETTLE seconds after the desk typed its
    emailed code were all taken for the desk's own, the person's too: the site turned the code down, they put the
    right one in, and another job's tab could come up over the one they were typing in. Its code box holding another
    code than the desk's is the person's; one the site emptied isn't."""
    now = time.time()
    typed = _fake_inbox(monkeypatch, [("oracle.com", "123456", now - 20)])
    box = {"value": ""}

    async def enter(self, r, found):
        typed.append((r.company, found.value))
        box["value"] = found.value
        return True

    monkeypatch.setattr(Applier, "_enter_code", enter)
    page = _code_page(monkeypatch, srv, box)
    job = _oracle_job(srv, "Alpha")
    applier = Applier(srv)
    left = applier.runs[job] = Run(job, "FSE", "Alpha", status="running", page=page)
    applier._pause(left, "email_code", "Enter the code we emailed you.", code="email_code")
    applier.later(job)
    left.paused_at = left.active_at = time.time() - 300

    def look():
        applier._note_activity(left, run(srv.browser.peek(page))[0])

    look()
    run(applier._check_mail(left))
    look()
    assert typed == [("Alpha", "123456")] and applier._person_at(None) is None
    box["value"] = ""  # the site turned it down and emptied its box
    look()
    assert applier._person_at(None) is None
    box["value"] = "65"  # the person putting the right one in
    look()
    assert applier._person_at(None) is left


# ------------------------------------------------------------- the desk's history (runs.jsonl)
def _history_lines() -> list[dict]:
    from job_apply import history

    return [json.loads(line) for line in history.history_path().read_text(encoding="utf-8").splitlines()]


_STAMP = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d"


def test_a_pass_that_reaches_its_review_page_is_a_line_in_the_history(srv, monkeypatch):
    """The review of Oct 15 (worth building 5): the desk kept 40 untimed lines of a job's log and nothing after it. Each
    line of the log has its time beside it now (Run.log_at, in the desk's JSON), and a pass that ends (here at the
    review page, after one step) is a line in ~/.job-apply/runs.jsonl: the job, its site, when it began and ended and
    its minutes, how it ended and why, the steps it went through, its rounds and Resumes, and Jobs at once."""
    import re

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    base = "https://careers.acme-fab.example"
    step, review = f"{base}/apply/1", f"{base}/apply/2"
    pages = {step: '<html><head><title>Apply</title></head><body><h2>Your Details</h2><form method="post" '
                   'action="/apply/2"><label for="fn">First Name</label><input id="fn" name="fn" required>'
                   '<button type="submit">Next</button></form></body></html>',
             ("POST", review): _form("Acme Fab")}
    job = srv.add_job(url=step, title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)
    began = time.time()

    async def go():
        await _serve(srv, pages, [])
        applier.start()
        try:
            r = applier.enqueue(job)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            await until(lambda: job not in applier._slots)
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert r.status == "ready", (r.status, r.reason, r.log)
    # (each to a tenth of a second)
    assert len(r.log_at) == len(r.log) and all(began - 1 <= t <= time.time() + 1 for t in r.log_at), (r.log, r.log_at)
    assert r.log_at == sorted(r.log_at), r.log_at
    assert r.public()["log_at"] == r.log_at and r.public()["log"] == r.log
    lines = _history_lines()
    assert len(lines) == 1, lines
    line = lines[0]
    assert {k: line[k] for k in ("job_id", "company", "title", "host", "outcome", "pages", "rounds", "resumes",
                                 "jobs_at_once")} == {
        "job_id": job, "company": "Acme Fab", "title": "FSE", "host": "careers.acme-fab.example", "outcome": "ready",
        "pages": 1, "rounds": 0, "resumes": 0, "jobs_at_once": 1}, line
    assert re.fullmatch(_STAMP, line["started"]) and re.fullmatch(_STAMP, line["ended"]), line
    assert line["started"] <= line["ended"] and isinstance(line["minutes"], float) and line["minutes"] >= 0, line
    assert "need" not in line and line["reason"] and len(line["reason"]) <= 200, line


def test_a_pause_a_resume_a_round_and_a_skip_are_lines_in_the_history(srv, monkeypatch):
    """A pass that stops for the person is a line with its kind of stop (needs_you, questions); their Resume, once
    answered, is another pass and another line (at its review page, one Resume); and their Skip of the job waiting
    there is a line of its own (no pass: no minutes), with the desk's round that took them to it counted once."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme-fab.example/apply/1"
    pages = {url: _form("Acme Fab", "Do you hold an active TS/SCI clearance?")}
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, [])
        applier.start()
        try:
            r = applier.enqueue(job)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            assert (r.status, r.need) == ("needs_you", "questions"), (r.status, r.reason)
            assert r.public()["code"] == "questions"  # (its stop's code, on its card: T1, the review of Oct 18)
            await until(lambda: job not in applier._slots)
            first = _history_lines()
            r.once[question_key(r.questions[0]["label"])] = "No"
            applier.resume(job)
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            assert r.status == "ready", (r.status, r.reason)
            assert r.public()["code"] == ""  # (a stop gone by has none)
            await until(lambda: job not in applier._slots)
            applier.start_round(job)
            applier.start_round(job)  # (the same job still: one round)
            applier.end_round()
            await applier.skip(job)
            return first
        finally:
            await applier.stop()

    first = run(go())
    assert [(e["outcome"], e.get("need"), e.get("code")) for e in first] == [("needs_you", "questions", "questions")], \
        first
    assert "question" in first[0]["reason"] and first[0]["resumes"] == 0, first
    lines = _history_lines()
    assert [(e["outcome"], e.get("need"), e.get("code"), e["resumes"], e["rounds"]) for e in lines] == [
        ("needs_you", "questions", "questions", 0, 0), ("ready", None, None, 1, 0),
        ("skipped", None, None, 1, 1)], lines
    skipped = lines[-1]
    assert "minutes" not in skipped and "started" not in skipped and "pages" not in skipped and skipped["ended"], skipped
    assert skipped["reason"] == "Skipped. Undo puts it back." and skipped["host"] == "careers.acme-fab.example", skipped


def test_a_pass_the_desk_stopping_cuts_short_is_a_stopped_line(srv, monkeypatch):
    """The desk stopping (the Claude app closing, say) mid-pass: the pass's line says it was cut short ("stopped"),
    not that the job failed."""
    opening: list[int] = []

    async def hang(**kwargs):  # (a site that never answers: the pass under way as the desk stops)
        opening.append(kwargs.get("job_id"))
        await asyncio.Event().wait()

    monkeypatch.setattr(srv, "open_application", hang)
    job = srv.add_job(url="https://careers.acme-fab.example/apply/1", title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)

    async def go():
        applier.start()
        r = applier.enqueue(job)
        try:
            await until(lambda: opening == [job], about=state(r))
        finally:
            await applier.stop()
        return r

    r = run(go())
    assert r.status == "failed" and r.cut_short, (r.status, r.reason)
    lines = _history_lines()
    assert [(e["outcome"], e["job_id"]) for e in lines] == [("stopped", job)], lines
    assert lines[0]["minutes"] >= 0 and lines[0]["reason"].startswith("Stopped before it finished"), lines


def test_the_history_is_cut_back_past_its_bound():
    """Appended a line at a time; past HISTORY_MAX lines, the file is written again with the newest HISTORY_KEEP."""
    from job_apply import history

    path = history.history_path()
    old = [json.dumps({"job_id": i, "company": "Acme Fab", "outcome": "ready", "ended": "2026-10-01T09:00:00-07:00"})
           for i in range(history.HISTORY_MAX)]
    path.write_text("".join(line + "\n" for line in old), encoding="utf-8")
    assert history.append({"job_id": 99999, "company": "Acme Fab", "outcome": "skipped",
                           "ended": "2026-10-02T09:00:00-07:00"})
    kept = _history_lines()
    assert len(kept) == history.HISTORY_KEEP and kept[-1]["job_id"] == 99999, len(kept)
    assert kept[0]["job_id"] == history.HISTORY_MAX + 1 - history.HISTORY_KEEP, kept[0]
    assert not list(path.parent.glob(f"{history.HISTORY_FILE}.*.tmp"))  # (written whole, through a file of its own)


def test_a_history_that_cant_be_written_never_stops_a_job(srv, monkeypatch, caplog):
    """A folder where the history can't be written (read-only, a full disk): the jobs go on all the same, and it's
    said once in the log, not at every line."""
    import logging

    from job_apply import history

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(history, "_said", set())
    history.history_path().mkdir()  # (nothing can be written there now, as in a read-only folder)
    urls = [f"https://careers.{s}.example/apply/1" for s in ("acme-fab", "other-litho")]
    pages = {u: _form("Acme Fab") for u in urls}
    jobs = [srv.add_job(url=u, title="FSE", company="Acme Fab")["job"]["id"] for u in urls]
    applier = Applier(srv)

    async def go():
        await _serve(srv, pages, [])
        applier.start()
        try:
            runs = [applier.enqueue(j) for j in jobs]
            await until(lambda: all(r.status not in ("queued", "running") for r in runs) and not applier._slots,
                        about=lambda: [state(r)() for r in runs])
            return runs
        finally:
            await applier.stop()

    with caplog.at_level(logging.WARNING, logger="job_apply.history"):
        runs = run(go())
    assert [r.status for r in runs] == ["ready", "ready"], [(r.status, r.reason) for r in runs]
    said = [rec for rec in caplog.records if rec.name == "job_apply.history"]
    assert len(said) == 1 and "couldn't write" in said[0].getMessage(), [rec.getMessage() for rec in said]
    assert history.read() == []  # (and a history that can't be read is none)


def test_a_history_line_that_wont_read_is_passed_over():
    """A line cut short (the program ended mid-write), changed by hand, or not text at all is passed over; the others
    are read, in their order."""
    from job_apply import history

    good = [{"job_id": 1, "outcome": "ready", "ended": "2026-10-16T09:00:00-07:00"},
            {"job_id": 2, "outcome": "failed", "ended": "2026-10-16T10:00:00-07:00"}]
    history.history_path().write_bytes(b"".join([
        json.dumps(good[0]).encode() + b"\n", b'{"job_id": 3, "outcome": "rea\n', b"\xff\xfe\x00 not text\n",
        b"[1, 2, 3]\n", b"\n", b"null\n", json.dumps(good[1]).encode() + b"\r\n", b'{"job_id": 4']))
    assert history.read() == good


def test_a_history_line_holds_no_answer_code_or_password(srv, monkeypatch):
    """Whatever the reason a pass stopped for quotes (a page's words), its line never holds an answer (the person's
    for this application, an account form's security answers, what went into the form), a code the inbox gave, a
    saved password or its digest, an email address, or an address's tokens: the desk's own words, cut short."""
    from job_apply import history

    monkeypatch.setenv("JOB_APPLY_SECRET_ACME_PASSWORD", "Not-A-Real-Pass1")
    applier = Applier(srv)
    r = Run(7, "FSE", "Acme Fab", status="needs_you", need="stuck", url="https://careers.acme-fab.example/apply",
            once={"q1": "Mesa Verde Answer", "q2": ["Night shift only"]},
            account_answers={"Security | Answer": "Flagstaff Ridge 1987"}, sign_in_key="d1g3stofthepassword")
    r.went_in.put("fn", {"label": "Phone", "value": "480-555-0123", "page": "Details"})
    pipeline._MAIL_TAKEN[("careers.example.com", "Q7XK2P", time.time())] = 7
    try:
        r.reason = ("The page says \u201cFlagstaff Ridge 1987\u201d, \u201cMesa Verde Answer\u201d and \u201cNight "
                    "shift only\u201d are wrong; it sent Q7XK2P and 482913 to sam.rivera@example.com; Not-A-Real-Pass1 "
                    "d1g3stofthepassword 480-555-0123 at https://careers.acme-fab.example/reset?token=abc123def. "
                    + "Fix them in the browser, then press Carry on. " * 10)
        applier._note_history(r, "needs_you", time.time() - 90, "apply", 2)
    finally:
        pipeline._MAIL_TAKEN.clear()
    text = history.history_path().read_text(encoding="utf-8")
    for secret in ("Flagstaff Ridge 1987", "Mesa Verde Answer", "Night shift only", "Q7XK2P", "482913",
                   "sam.rivera@example.com", "Not-A-Real-Pass1", "d1g3stofthepassword", "480-555-0123", "abc123def"):
        assert secret not in text, (secret, text)
    line = json.loads(text)
    assert line["reason"].startswith("The page says") and len(line["reason"]) <= history.REASON_CHARS, line
    assert "https://careers.acme-fab.example/reset" in line["reason"] and line["need"] == "stuck", line
    assert line["minutes"] == 1.5 and line["jobs_at_once"] == 2 and line["pages"] == 0, line


@pytest.mark.slow
def test_the_history_of_an_account_and_security_question_flow_holds_no_secret(srv, monkeypatch, job_apply_home):
    """An account made with the saved password and the emailed code, its security question answered on the card: the
    lines its passes wrote hold none of the answers, the code, the passwords or the password's digest."""
    from job_apply.desk import Desk

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    as_test_identity(monkeypatch, job_apply_home)
    inbox_with_code(monkeypatch)
    job = srv.add_job(url=fixture_url("site/brassring-signin.html") + "?selectmenu", title="Senior Analyst",
                      company="Example Corp")["job"]
    desk = Desk(srv)
    applier = desk.applier
    secret = "Flagstaff Ridge 1987"

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.need not in ("", "email_code") and r.status not in ("queued", "running")
                        and r.questions, timeout=90, about=state(r))
            keys = [f"{q.get('section') or ''} | {q['label']} | {q.get('sublabel') or ''}" for q in r.questions]
            desk.answer(job["id"], [{"label": k, "value": "In what city were you born?" if q.get("options") else secret}
                                    for k, q in zip(keys, r.questions)])
            await until(lambda: any(line.startswith("filled in your answers") for line in r.log), about=state(r))
            await until(lambda: r.status not in ("queued", "running") and job["id"] not in applier._slots,
                        timeout=90, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    lines = _history_lines()
    # (its emailed code's stop, its security questions', and how the pass after their answers ended)
    assert len(lines) >= 2 and ("needs_you", "sign_in") in [(e["outcome"], e.get("need")) for e in lines], lines
    text = json.dumps(lines)
    for kept_out in (secret, "In what city were you born?", "not-a-real-password", "an-app-password", "123456"):
        assert kept_out not in text, (kept_out, text)
    assert not r.sign_in_key or r.sign_in_key not in text


def _hanging_open(monkeypatch, srv, url: str, ending: str = "returns"):
    """open_application held until the test lets it go (`go_on`, an event made on the test's loop), then answering as
    the site opened (or, `ending` "raises", failing): a pass under way, for as long as the test wants."""
    held: dict = {"opening": []}

    async def hang(**kwargs):
        held["opening"].append(kwargs.get("job_id"))
        await held["go_on"].wait()
        if ending == "raises":
            raise RuntimeError("the site went away")
        return {"url": url}

    monkeypatch.setattr(srv, "open_application", hang)
    return held


def test_a_skip_mid_pass_is_one_line_with_its_minutes(srv, monkeypatch):
    """The review of 0.3.204: Skip pressed while the job's pass is under way writes no line of its own; the pass, ending
    skipped, writes the one line, with its minutes."""
    url = "https://careers.acme-fab.example/apply/1"
    held = _hanging_open(monkeypatch, srv, url)
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)

    async def go():
        held["go_on"] = asyncio.Event()
        applier.start()
        r = applier.enqueue(job)
        try:
            await until(lambda: held["opening"] == [job], about=state(r))
            await applier.skip(job)
            held["go_on"].set()
            await until(lambda: job not in applier._slots, about=state(r))
        finally:
            await applier.stop()
        return r

    r = run(go())
    assert r.status == "skipped", (r.status, r.reason)
    lines = _history_lines()
    assert [e["outcome"] for e in lines] == ["skipped"] and "minutes" in lines[0] and "started" in lines[0], lines


def test_i_submitted_it_on_a_job_at_its_review_page_is_one_marked_applied_line(srv, monkeypatch):
    """The review of 0.3.204: the person's "I submitted it" on a job waiting at its review page (not under way) is a
    line of its own, with no minutes, and it's "marked_applied", never "submitted": that's the desk's own send."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme-fab.example/apply/1"
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)

    async def go():
        await _serve(srv, {url: _form("Acme Fab")}, [])
        applier.start()
        try:
            r = applier.enqueue(job)
            await until(lambda: r.status not in ("queued", "running") and job not in applier._slots, about=state(r))
            assert r.status == "ready", (r.status, r.reason)
            srv.tracker().update(job, status="applied", note="marked applied in the Job Desk")  # (as the desk's does)
            applier.mark_applied(job)
            applier.mark_applied(job)  # (twice: one line)
            return r
        finally:
            await applier.stop()

    run(go())
    lines = _history_lines()
    assert [e["outcome"] for e in lines] == ["ready", "marked_applied"], lines
    assert "minutes" not in lines[1] and "started" not in lines[1] and lines[1]["reason"] == "Marked as applied.", lines


@pytest.mark.parametrize("ending", ["returns", "raises"])
def test_i_submitted_it_mid_pass_is_one_line_and_stands(srv, monkeypatch, caplog, ending):
    """The review of 0.3.204: "I submitted it" pressed while the job's pass is under way writes no line of its own; the
    pass, ending, writes the one line (marked_applied, with its minutes). And should the pass then fail ("raises": the
    site going away under it), the job stays marked applied, as a skipped one stays skipped: it was "failed", and a
    Resume offered to fill it again. The error is still said in the log (the re-check of 4a17a0e)."""
    import logging

    caplog.set_level(logging.WARNING, logger="job_apply.pipeline")
    url = "https://careers.acme-fab.example/apply/1"
    held = _hanging_open(monkeypatch, srv, url, ending)
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)

    async def go():
        held["go_on"] = asyncio.Event()
        applier.start()
        r = applier.enqueue(job)
        try:
            await until(lambda: held["opening"] == [job], about=state(r))
            srv.tracker().update(job, status="applied", note="marked applied in the Job Desk")
            applier.mark_applied(job)
            held["go_on"].set()
            await until(lambda: job not in applier._slots, about=state(r))
        finally:
            await applier.stop()
        return r

    r = run(go())
    assert (r.status, r.reason) == ("submitted", "Marked as applied."), (r.status, r.reason, r.log)
    lines = _history_lines()
    assert [e["outcome"] for e in lines] == ["marked_applied"] and "minutes" in lines[0], lines
    said = [rec.getMessage() for rec in caplog.records if rec.name == "job_apply.pipeline"]
    assert (f"job {job} stopped on an error" in said) == (ending == "raises"), said


def test_a_stop_queued_again_as_its_page_is_saved_is_still_a_line(srv, monkeypatch):
    """The review of 0.3.204: Resume pressed while the pass that paused is still saving the page it stopped on (its last
    step) put the job back in the queue before the pass ended, so the pause had no line. The pass's line says how its
    pages ended (needs_you, questions), and the pass the Resume began has its own."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme-fab.example/apply/1"
    pages = {url: _form("Acme Fab", "Do you hold an active TS/SCI clearance?")}
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)
    real_save = Applier._save_stop
    held: dict = {"saves": 0}

    async def slow_save(self, run_):
        held["saves"] += 1
        if held["saves"] == 1:  # (the first pass's: held until the Resume is pressed)
            held["entered"].set()
            await held["go_on"].wait()
        await real_save(self, run_)

    monkeypatch.setattr(Applier, "_save_stop", slow_save)

    async def go():
        held["entered"], held["go_on"] = asyncio.Event(), asyncio.Event()
        await _serve(srv, pages, [])
        applier.start()
        try:
            r = applier.enqueue(job)
            await held["entered"].wait()
            assert (r.status, r.need) == ("needs_you", "questions"), (r.status, r.reason)
            applier.resume(job)  # (as its stop's page is being saved)
            held["go_on"].set()
            await until(lambda: held["saves"] == 2 and job not in applier._slots and r.status == "needs_you",
                        about=state(r))
            return r
        finally:
            await applier.stop()

    run(go())
    lines = _history_lines()
    assert [(e["outcome"], e.get("need")) for e in lines] == [("needs_you", "questions")] * 2, lines
    assert all("question" in e["reason"] for e in lines), lines


def test_the_history_is_counted_once_not_read_at_every_line(monkeypatch):
    """The review of 0.3.204: past about 250 KB (a real line is about 550 bytes), every line written read the whole
    file again to count its lines. They're counted once, then kept count of: the file is read again only to cut it
    back."""
    from job_apply import history

    monkeypatch.setattr(history, "_COUNTS", {}, raising=False)
    path = history.history_path()
    line = json.dumps({"job_id": 1, "company": "Acme Fab", "outcome": "ready", "reason": "x" * 400,
                       "ended": "2026-10-16T09:00:00-07:00"})
    path.write_text((line + "\n") * 1000, encoding="utf-8")
    reads: list[Path] = []
    real_read = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == path:
            reads.append(self)
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    for i in range(5):
        assert history.append({"job_id": i, "company": "Acme Fab", "outcome": "ready", "reason": "y" * 400,
                               "ended": "2026-10-17T09:00:00-07:00"})
    assert len(reads) == 1, len(reads)
    assert len(path.read_bytes().splitlines()) == 1005


def test_a_history_reason_hides_whole_words_and_the_profiles_own(srv, monkeypatch):
    """The review of 0.3.204. Answers are hidden from a line's reason as whole words or phrases, and never the desk's
    own words for a box ("ticked", "Yes"): a reason kept its sense. And beyond the run's own answers: the profile's own
    words (its last name, its city), a phone number however it's written, and a long answer by its start."""
    from job_apply import history

    applier = Applier(srv)
    long_answer = "I maintained the etch tools across three fabs and trained the night shift on " + "x" * 200
    r = Run(9, "FSE", "Acme Fab", status="needs_you", need="stuck", url="https://careers.acme-fab.example/apply",
            once={"q1": "Mesa", "q2": long_answer})
    r.went_in.put("t", {"label": "I agree", "value": "ticked", "page": "Details"})
    r.went_in.put("y", {"label": "Relocate?", "value": "Yes", "page": "Details"})
    r.reason = ("The box I ticked says Yes; the Mesas office and Mesa are closed to Rivera of Chandler at (602) 555-0199: "
                + long_answer[:60])
    applier._note_history(r, "needs_you", time.time() - 60, "apply", 1)
    said = _history_lines()[0]["reason"]
    assert said.startswith("The box I ticked says Yes; the Mesas office and … are closed"), said
    for hidden in ("Rivera", "Chandler", "555-0199", "I maintained the etch tools"):
        assert hidden not in said, (hidden, said)
    assert len(said) <= history.REASON_CHARS


def test_i_submitted_it_after_the_desks_press_showed_no_confirmation_is_the_desks_send(srv, monkeypatch):
    """The re-check of 4a17a0e: Submit for me pressed Submit and no confirmation showed (check_submit); the person then
    says it went in. The desk's own press sent it: its line is "submitted", not "marked_applied"."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme-fab.example/apply/1"
    pages = {url: _form("Acme Fab"),
             ("POST", "https://careers.acme-fab.example/posted"): "<h1>All set!</h1><p>Our team will be in touch.</p>"}
    posts: list[str] = []
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)
    applier.auto_submit = True

    async def go():
        await _serve(srv, pages, posts)
        applier.start()
        try:
            r = applier.enqueue(job, submit=True)
            await until(lambda: r.status not in ("queued", "running") and job not in applier._slots, about=state(r))
            assert (r.status, r.need) == ("needs_you", "check_submit"), (r.status, r.reason)
            srv.tracker().update(job, status="applied", note="marked applied in the Job Desk")
            applier.mark_applied(job)
            return r
        finally:
            await applier.stop()

    run(go())
    assert len(posts) == 1, posts
    lines = _history_lines()
    assert [(e["outcome"], e.get("need")) for e in lines] == [("needs_you", "check_submit"), ("submitted", None)], lines


def test_a_history_reason_keeps_a_saved_answers_question_and_employer(srv):
    """The re-check of 4a17a0e: a saved answer's question and the employer it was given at (answers.yaml's question,
    core and from) were hidden from a line's reason as if they were the person's answer: they're not. The answer is."""
    config.save_answer("Do you hold an active TS/SCI clearance?", "Not at this time", "Other Litho")
    applier = Applier(srv)
    r = Run(11, "FSE", "Other Litho", status="needs_you", need="stuck", url="https://careers.other-litho.example/apply",
            reason="Other Litho asks “Do you hold an active TS/SCI clearance?” again, and says “Not at "
                   "this time” isn't one of its choices.")
    applier._note_history(r, "needs_you", time.time() - 30, "apply", 1)
    said = _history_lines()[0]["reason"]
    assert said.startswith("Other Litho asks “Do you hold an active TS/SCI clearance?” again"), said
    assert "Not at this time" not in said, said


def test_i_submitted_it_while_submit_for_me_checks_the_page_sends_nothing(srv, monkeypatch):
    """The re-check of 4a17a0e (a bug from before the history): with Submit for me on, the desk reads the review page for
    required fields still empty before it presses Submit, and then looked only for a Skip. "I submitted it" pressed in
    that moment was passed over, and the desk pressed Submit on an application the person says is in already. It
    presses nothing."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme-fab.example/apply/1"
    posts: list[str] = []
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)
    applier.auto_submit = True
    real_submit, real_inspect = Applier._submit, srv.inspect_form
    held = {"checking": False, "marked": False}

    async def submit(self, run_, by_person=True):
        held["checking"] = not by_person  # (Submit for me's: the page read for empty fields first)
        try:
            return await real_submit(self, run_, by_person)
        finally:
            held["checking"] = False

    async def inspect(**kwargs):
        data = await real_inspect(**kwargs)
        if held["checking"] and not held["marked"]:  # the person's "I submitted it", as the page is being read
            held["marked"] = True
            srv.tracker().update(job, status="applied", note="marked applied in the Job Desk")
            applier.mark_applied(job)
        return data

    monkeypatch.setattr(Applier, "_submit", submit)
    monkeypatch.setattr(srv, "inspect_form", inspect)

    async def go():
        await _serve(srv, {url: _form("Acme Fab")}, posts)
        applier.start()
        try:
            r = applier.enqueue(job, submit=True)
            await until(lambda: r.status not in ("queued", "running") and job not in applier._slots, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert held["marked"] and posts == [], (held, posts, r.log)
    assert (r.status, r.reason) == ("submitted", "Marked as applied."), (r.status, r.reason)
    assert [e["outcome"] for e in _history_lines()] == ["marked_applied"]


def test_i_submitted_it_while_the_submit_tool_looks_the_page_over_sends_nothing(srv, monkeypatch):
    """The last re-check of the history (a window from before it): the Submit tool itself reads the page, its job
    system and its buttons, and keeps a picture of it, before it presses (a second or more). "I submitted it" pressed on
    the desk in that moment was passed over there, and the application the person says is in already was sent again. It
    presses nothing, and the job stays marked as applied."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    url = "https://careers.acme-fab.example/apply/1"
    posts: list[str] = []
    job = srv.add_job(url=url, title="FSE", company="Acme Fab")["job"]["id"]
    applier = Applier(srv)
    real_shot = srv.browser.screenshot
    held = {"marked": False}

    async def screenshot(*args, **kwargs):
        save_to = kwargs.get("save_to")
        if save_to is not None and Path(save_to).name == "before-submit.jpg" and not held["marked"]:
            held["marked"] = True  # the person's "I submitted it", as the tool keeps its picture of the page
            srv.tracker().update(job, status="applied", note="marked applied in the Job Desk")
            applier.mark_applied(job)
        return await real_shot(*args, **kwargs)

    monkeypatch.setattr(srv.browser, "screenshot", screenshot)

    async def go():
        await _serve(srv, {url: _form("Acme Fab")}, posts)
        applier.start()
        try:
            r = applier.enqueue(job)
            await until(lambda: r.status == "ready" and job not in applier._slots, about=state(r))
            applier.submit_now(job)
            await until(lambda: held["marked"] and job not in applier._slots, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert held["marked"] and posts == [], (held, posts, r.log)
    assert (r.status, r.reason) == ("submitted", "Marked as applied."), (r.status, r.reason, r.log)
    assert not r.pressed_before
    assert srv.tracker().get(job)["status"] == "applied"
    assert pipeline.FINISHED | {"skipped"} == set(srv._SUBMIT_GONE)  # (the tool's own list, kept as the desk's)


def _linked_pair(srv) -> tuple[int, int]:
    """An Indeed job saved weeks ago, and the employer's posting of it saved now: kept apart (a repost, as often as
    not) and noted as maybe the same (Tracker.maybe_same). (The employer's job, the Indeed job.)"""
    from datetime import datetime, timedelta, timezone

    t = srv.tracker()
    indeed = srv.add_job(url="https://www.indeed.com/viewjob?jk=0b0b0b0b0b0b0b0b", title="Equipment Technician",
                         company="Intel Corporation", location="Chandler, AZ", source="indeed")["job"]["id"]
    with t._lock:
        t.conn.execute("UPDATE jobs SET created_at = ? WHERE id = ?",
                       ((datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds"), indeed))
        t.conn.commit()
    own = srv.add_job(url="https://careers.intel.example/job/1", title="Equipment Technician", company="Intel",
                      location="Chandler, AZ")["job"]["id"]
    assert [o["id"] for o in t.maybe_same(own)] == [indeed]
    return own, indeed


def test_two_jobs_that_may_be_one_posting_never_go_at_once_maybe_duplicate(srv, monkeypatch):
    """The review of 0.3.206 (MEDIUM): with Jobs at once at 2, the employer's job and its Indeed copy (kept apart, as
    maybe the same) each read the other as saved (the tracker says "in progress" only once a page is opened) and both
    were filled: Submit for me sent one while the other waited at Indeed for the person. They go one at a time, as two
    jobs on one site, and the second reads the first as begun by this desk: it stops on the card, nothing opened."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    own, indeed = _linked_pair(srv)
    applier = Applier(srv)
    applier.jobs_at_once = 2
    opened: list[int] = []
    beside: list[tuple[int, bool, str]] = []

    async def open_(self, r):  # (its page opening takes a moment: the other could begin meanwhile)
        opened.append(r.job_id)
        await asyncio.sleep(1.5)
        other = indeed if r.job_id == own else own
        beside.append((r.job_id, other in self._slots, self.runs[other].status))
        r.status, r.reason = "ready", "Filled and waiting on the review page."
        return False

    monkeypatch.setattr(Applier, "_open", open_)

    async def go():
        applier.start()
        try:
            a, b = applier.enqueue(own, submit=True), applier.enqueue(indeed)
            await until(lambda: a.status == "ready" and b.status == "needs_you", about=lambda: (state(a)(), state(b)()))
            await asyncio.sleep(1)
            return a, b
        finally:
            await applier.stop()

    a, b = run(go())
    assert opened == [own] and beside == [(own, False, "queued")], (opened, beside)
    assert b.need == "maybe_duplicate" and srv.tracker().get(own)["status"] == "saved"  # (begun here, not yet there)
    assert b.reason.startswith("This may be the job you already started on the employer's site on "), b.reason


def test_submit_for_me_looks_again_for_a_maybe_duplicate_begun_since(srv, monkeypatch):
    """The review of 0.3.206: the job that may be this one could be begun after this one was filled (on Indeed, by the
    person or Claude). Submit for me looks once more before it presses: it stops on the card instead. A Submit the
    person pressed is theirs to send, and so is one after their Apply anyway."""
    own, indeed = _linked_pair(srv)
    applier = Applier(srv)
    pressed: list[dict] = []

    async def submit_application(**kwargs):
        pressed.append(kwargs)
        return {"submitted": True, "confirmed": True}

    monkeypatch.setattr(srv, "submit_application", submit_application)
    srv.update_job(indeed, status="in_progress")
    r = applier.runs[own] = Run(own, "Equipment Technician", "Intel", status="ready")
    run(applier._submit(r, by_person=False))
    assert (r.status, r.need, pressed) == ("needs_you", "maybe_duplicate", []), (r.status, r.need, r.reason)
    for by_person in (True, False):  # (the person's own Submit; then Submit for me after their Apply anyway)
        if not by_person:
            srv.tracker().clear_maybe_same(own, [indeed])
        r.status, r.need = "ready", ""
        run(applier._submit(r, by_person=by_person))
        assert r.need == "stuck" and "tab was closed" in r.reason, (by_person, r.need, r.reason)  # (on to its tab)


def _three_alike(srv) -> tuple[int, int, int]:
    """Intel's posting alike two job boards' copies, Indeed's applied to and LinkedIn's saved: which it's a copy of
    can't be told, so it's noted as maybe the same as both. (Intel's, Indeed's, LinkedIn's.)"""
    x = srv.add_job(url="https://www.indeed.com/viewjob?jk=0c0c0c0c0c0c0c0c", title="Equipment Technician",
                    company="Intel Corporation", location="Chandler, AZ", source="indeed")["job"]["id"]
    srv.update_job(x, status="applied")
    b = srv.add_job(url="https://www.linkedin.com/jobs/view/3900000777/", title="Equipment Technician", company="Intel",
                    location="Chandler, AZ", source="linkedin")["job"]["id"]
    a = srv.add_job(url="https://careers.intel.example/job/2", title="Equipment Technician", company="Intel",
                    location="Chandler, AZ")["job"]["id"]
    assert sorted(o["id"] for o in srv.tracker().maybe_same(a)) == sorted([x, b])
    return a, x, b


def test_apply_anyway_clears_only_the_jobs_its_maybe_duplicate_card_named(srv, monkeypatch, fake_open):
    """The review of 0.3.206 (MEDIUM): a job alike one applied to on Indeed and one saved from LinkedIn was held on a
    card naming the Indeed job alone; the LinkedIn job was then filled (for the person's own Submit), and Apply anyway
    on the card cleared both, so the first was filled and sent with no one asked about the LinkedIn one. Apply anyway
    clears what its card named; one begun since stops the job again, on a card naming it, and nothing is opened."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    a, x, b = _three_alike(srv)
    applier = Applier(srv)
    opened = fake_open

    async def go():
        applier.start()
        try:
            ra = applier.enqueue(a, submit=True)
            await until(lambda: ra.status == "needs_you", about=state(ra))
            first = ra.reason
            rb = applier.enqueue(b)
            await until(lambda: rb.status == "ready", about=state(rb))
            applier.apply_anyway(a)
            await until(lambda: a in opened or ra.status == "needs_you" and "LinkedIn" in ra.reason, about=state(ra))
            await asyncio.sleep(1)
            return ra, first
        finally:
            await applier.stop()

    ra, first = run(go())
    assert opened == [b], opened  # (Intel's never opened, so never sent)
    assert f"(job {x}: Equipment Technician at Intel Corporation)" in first and f"job {b}" not in first, first
    assert (ra.status, ra.need) == ("needs_you", "maybe_duplicate")
    assert ra.reason.startswith("This may be the job you already started on LinkedIn on ") and \
        f"(job {b}: Equipment Technician at Intel)" in ra.reason, ra.reason
    assert [o["id"] for o in srv.tracker().maybe_same(a)] == [b]  # (Indeed's cleared, as its card said; LinkedIn's not)


def test_a_maybe_duplicate_card_names_every_job_it_asks_about(srv):
    """The card names each job it's asking about (its title, employer, status, where and when), and keeps their ids:
    the ones its Apply anyway clears."""
    a, x, b = _three_alike(srv)
    srv.update_job(b, status="in_progress")
    applier = Applier(srv)
    r = applier.runs[a] = Run(a, "Equipment Technician", "Intel")
    found = applier._maybe_done_elsewhere(r)
    said, ids = found if isinstance(found, tuple) else (found, None)
    jobs = {j: srv.tracker().get(j) for j in (x, b)}
    assert said == ("This may be one of the jobs you already started or applied to: "
                    f"job {x}, Equipment Technician at Intel Corporation (applied on Indeed, {jobs[x]['applied_at'][:10]}); "
                    f"job {b}, Equipment Technician at Intel (started on LinkedIn, {jobs[b]['updated_at'][:10]}). "
                    "Apply anyway, or Skip?"), said
    assert ids == [x, b]


def test_a_linked_job_skipped_or_failed_since_holds_no_maybe_duplicate(srv):
    """The review of 0.3.206 (LOW): a linked job the desk began and then skipped, or that failed, still held this one
    ("already started"). Skipped or failed, it holds it only as the tracker says it's begun or done."""
    own, indeed = _linked_pair(srv)
    applier = Applier(srv)
    r = applier.runs[own] = Run(own, "Equipment Technician", "Intel")
    for status in ("skipped", "failed"):
        applier.runs[indeed] = Run(indeed, "Equipment Technician", "Intel Corporation", status=status, started=True)
        assert applier._maybe_done_elsewhere(r) is None, status
    applier.runs[indeed] = Run(indeed, "Equipment Technician", "Intel Corporation", status="needs_you", need="questions",
                               started=True)
    assert applier._maybe_done_elsewhere(r) is not None  # (begun, and waiting on the person: asked about)
    applier.runs[indeed].status = "failed"
    srv.update_job(indeed, status="in_progress")  # (its page opened before it failed: the tracker says begun)
    assert applier._maybe_done_elsewhere(r) is not None
