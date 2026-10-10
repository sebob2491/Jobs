"""The one-button apply pipeline, against a fake multi-step application site
(tests/fixtures/site: posting -> Apply -> Apply Manually -> sign-in -> two form steps ->
review -> submit)."""

import asyncio
import contextlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest
from conftest import browser_available, fixture_url, run

import job_apply.pipeline as pipeline
from job_apply import config
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


def test_bot_check_holds_the_queue_until_the_person_passes_it(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    checked = srv.add_job(url=fixture_url("site/botcheck.html"), title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        try:
            await check()
        finally:
            await applier.stop()

    async def check():
        applier.start()
        first = applier.enqueue(checked["id"])
        second = applier.enqueue(other["id"])
        await until(lambda: first.status == "needs_you")
        assert first.need == "bot_check" and first.blocking
        await asyncio.sleep(1.5)
        assert second.status == "queued"  # waits while the person deals with the check
        await first.page.click("#human")  # passes it
        await until(lambda: first.need == "sign_in")  # carried on by itself, up to the sign-in

    run(go())


def test_with_no_one_at_the_browser_the_other_jobs_go_ahead(srv, monkeypatch):
    """Apply pressed on a batch, then the person walks away. A job that needs them (a bot
    check) holds the queue only while something happens in its tab; then the other jobs go
    ahead. When the person gets past the check later, the desk carries on with it by itself."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 2)
    checked = srv.add_job(url=fixture_url("site/botcheck.html"), title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Technician", company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            first = applier.enqueue(checked["id"])
            second = applier.enqueue(other["id"])
            await until(lambda: first.status == "needs_you")
            assert first.need == "bot_check" and first.blocking
            await until(lambda: second.status not in ("queued", "running"))  # went ahead
            assert first.status == "needs_you" and first.need == "bot_check" and not first.blocking
            assert "nothing happened in its tab for" in first.reason and "carries on with it" in first.reason
            await first.page.click("#human")  # the person, back, passes the check
            await until(lambda: first.need == "sign_in")  # carried on by itself, up to the sign-in
            return first
        finally:
            await applier.stop()

    run(go())


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
    assert need == "bot_check" and blocking, (reason, log)
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
    """A paused tab that keeps failing to be read held the queue for the whole 45 minutes."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "HANDS_ON_IDLE", 1)
    job = srv.add_job(url=fixture_url("site/botcheck.html"), title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)

    async def broken(run):
        raise RuntimeError("the tab crashed")

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status == "needs_you" and r.blocking)
            applier._moved_on = broken
            await until(lambda: r.left, timeout=15, about=state(r))
            return r
        finally:
            await applier.stop()

    r = run(go())
    assert not r.blocking and "nothing happened in its tab" in r.reason


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

    async def look():
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


def test_cookie_dialog_is_declined_never_accepted(srv, monkeypatch):
    """Infineon's cookie dialog covers the whole form; the desk presses Reject, never Accept."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/cookie-form.html"), title="FSE", company="Example Fab")["job"]
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
    assert "declined cookies (“Reject”)" in r.log and accepted == 0


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
    profile.write_text(profile.read_text().replace("settings:\n", "settings:\n  accept_cookies: true\n"))
    r, (accepted, prefs) = apply_once()
    assert accepted == 1 and prefs == 0, (r.reason, r.log)
    assert any(line.startswith("accepted cookies (\u201cAGREE AND PROCEED\u201d)") for line in r.log), r.log
    assert r.status == "ready", (r.status, r.reason, r.log)


def test_a_banner_that_can_be_declined_is_declined_even_where_accepting_is_allowed(srv, monkeypatch, job_apply_home):
    """settings.accept_cookies lets the desk accept only a banner with no way to decline: one
    whose way is "Deny" is denied."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    profile = job_apply_home / "profile.yaml"
    profile.write_text(profile.read_text().replace("settings:\n", "settings:\n  accept_cookies: true\n"))
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
    profile = yaml.safe_load(path.read_text())
    profile["settings"].update(settings)
    path.write_text(yaml.safe_dump(profile))


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
    assert "press its button in the browser window, then press Resume" in reason and "accept_notices: true" in reason
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


@pytest.mark.parametrize("notice, settings", [("privacy", {}), ("other", {}), ("privacy", {"accept_notices": False})],
                         ids=["privacy", "other", "privacy-off"])
def test_a_privacy_dialog_over_the_form_is_agreed_and_another_is_the_persons(srv, monkeypatch, job_apply_home, notice,
                                                                              settings):
    """settings.accept_notices agrees to the employer's privacy notice too, the owner's call (Oct
    10): a privacy policy over the form, with Cancel and Ok, is taken in with its Ok. Any other
    dialog over the form (about neither AI screening nor privacy), or any with the setting off,
    stops the desk for the person."""
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
    else:
        assert "“Privacy Policy of Example Corp”" in r.reason and "accept_notices: true" in r.reason, r.reason


@pytest.mark.parametrize("settings", [{}, {"accept_notices": False}], ids=["on", "off"])
def test_a_privacy_agreement_before_the_application_is_accepted_for_the_person(srv, monkeypatch, job_apply_home,
                                                                                settings):
    """Kforce's Taleo career section (live, Oct 2026) puts a privacy agreement before the
    application, its only ways on "I Accept" and "I Decline". With settings.accept_notices (the
    owner's call, Oct 10) the desk accepts it for the person, says so, and goes on to fill the
    application; with it off the agreement is theirs to press."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    notice_settings(job_apply_home, **settings)
    job = srv.add_job(url=fixture_url("site/privacy-agreement.html"), title="Recruiter", company="Example Staffing")["job"]
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
    if settings:
        assert (r.status, r.need) == ("needs_you", "stuck") and "“I Accept”" in r.reason, (r.reason, r.log)
        assert r.url.endswith("privacy-agreement.html")
        return
    assert "agreed to Example Staffing's “I Accept” for you (settings.accept_notices)" in r.log, r.log
    assert r.url.endswith("generic_form.html") and any(line.startswith("filled") for line in r.log), r.log


def test_a_box_that_wouldnt_fill_is_said_as_that_not_asked(srv, monkeypatch):
    """A fill that failed with an error is no question the profile doesn't answer: Eightfold's
    Country of Residence came back as one, "TimeoutError: Locator.evaluate: Timeout 15000ms
    exceeded." on the card (live, Oct 2026). It's said as a fill that failed."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    fill_one = srv.browser._fill_one

    async def times_out(page, field, value):
        if field.get("label") == "Country of Residence":
            raise PlaywrightTimeout("Locator.evaluate: Timeout 15000ms exceeded.")
        return await fill_one(page, field, value)

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
                               "Fill it in the browser, then press Resume"), r.reason
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
    assert attestation({"kind": "radio_group", "label": "Do you accept our applicant privacy notice?", "required": True,
                        "options": ["Yes", "No"]}) is None  # (a radio group isn't one of the kinds it picks in)
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
    profile = yaml.safe_load(path.read_text())
    profile["settings"].update(manage_accounts=True, submit_mode=submit_mode)
    path.write_text(yaml.safe_dump(profile))


def test_with_manage_accounts_the_desk_creates_the_account(srv, monkeypatch, job_apply_home):
    """The person turned on settings.manage_accounts: the desk ticks the Create Account form's
    terms box and presses its button itself, with the saved password, and carries on."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
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
    assert any(line.startswith("created your account on") and "agreed to its terms" in line for line in r.log), r.log
    assert r.need != "sign_in" and "Create Account" not in r.reason, (r.status, r.reason, r.log)


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


def test_an_agreement_a_bot_check_holds_is_said_to_be_the_persons(srv, monkeypatch, job_apply_home):
    """Charles Schwab's iCIMS in the live check (Oct 2026): "I Acknowledge the Privacy Notice" was pressed
    for the person, then went blank and greyed out behind the page's hCaptcha, and the desk said it
    couldn't find the button. It says what it pressed, and that the bot check is theirs."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/icims-privacy-gate.html") + "?bot", title="Analyst",
                      company="Example Financial")["job"]
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
    assert "I pressed “I Acknowledge the Privacy Notice” for you" in r.reason and "(hCaptcha)" in r.reason, r.reason
    assert "couldn't find the button" not in r.reason


def test_with_manage_accounts_a_refused_saved_password_is_reset(srv, monkeypatch, job_apply_home):
    """The email has an account there, and the saved password isn't its password. With the
    inbox watched, the desk asks for a password reset before trying a new account (the owner's
    choice), opens the emailed link in the job's tab, sets the saved password as the new one,
    and signs in."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    wanted = []

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None):
        wanted.append(want)
        link = fixture_url("site/reset-password.html") + "?token=abc"
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
    log = "\n".join(r.log)
    assert "asked it to email a password reset" in log, log
    assert "opened the password reset link from your email (sent from careers.example.com)" in log, r.log
    assert "set your saved password as" in log, r.log
    assert r.seen_form and not any(w in r.url for w in ("signin", "reset", "forgot")), (r.url, r.reason, r.log)
    assert "reset" in wanted
    assert "opened Create Account" not in log and "created your account" not in log, log


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


def test_a_step_still_loading_its_questions_is_waited_for(srv, monkeypatch):
    """KLA's Workday (live, Oct 2026): past My Experience, its Application Questions step showed its
    loading dots where the questions go and a greyed-out Save and Continue the form reader found no
    button for, beside the previous step's resume box, and the desk stopped at once: "I couldn't find
    the button that moves this application on." A step still loading is waited for."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    job = srv.add_job(url=fixture_url("site/workday-questions-loading.html"), title="Customer Service Engineer",
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

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None):
        if want == "reset" and reset_link:
            return pipeline.mailbox.Found(want, reset_link, "careers.example.com", time.time())
        return None

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)


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


def test_no_reset_email_in_a_few_minutes_makes_an_account_and_the_queue_goes_on(srv, monkeypatch, job_apply_home):
    """The site says it emailed a reset (most say so whether or not the email has an account),
    and nothing comes: the other jobs go ahead meanwhile, and after RESET_MAIL_WAIT the desk goes
    back to the sign-in page and makes an account."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "RESET_MAIL_WAIT", 4)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=fixture_url("site/signin-submit.html"), title="FSE", company="Example Fab")["job"]
    other = srv.add_job(url=fixture_url("generic_form.html"), title="Tech", company="Example Litho")["job"]
    applier = Applier(srv)

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


def test_a_sign_in_page_good_for_one_visit_is_reached_again_through_the_posting(srv, monkeypatch, job_apply_home):
    """SuccessFactors' sign-in page (career?_s.crb=…) is good for one visit: going back to it after a
    reset the desk couldn't do showed "An error occurred while processing your request" (Arizona
    Public Service, the test identity, live Oct 2026). The desk opens the job's posting again, whose
    Apply leads to a fresh sign-in page, and makes the account there."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    watched_inbox(monkeypatch)
    job = srv.add_job(url=fixture_url("site/sf-posting.html"), title="Energy Analyst", company="Example Utility")["job"]
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
    assert "so I went back to make an account there instead" in log, log
    assert "sign-in page couldn't be opened again, so I opened the job again" in log, log
    assert "created your account" in log and r.seen_form, (r.reason, r.log)


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
        assert need == "sign_in" and "won't try it again for this job unless you press Resume" in reason, (reason, r.log)
    assert [tries for _, _, tries in refused] == [3, 3, 4], seen  # the queue's pass pressed nothing
    assert signed_in == 5 and r.seen_form and r.need != "sign_in", (r.reason, r.log)


@pytest.mark.parametrize("need, tries", [("sign_in", 1), ("questions", 2), ("stuck", 2)])
def test_only_a_resume_on_a_sign_in_card_presses_a_refused_password_again(srv, need, tries):
    """The sign-in card asks the person to reset the password by hand and press Resume, and that
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


def test_with_manage_accounts_ukg_pros_second_account_step_is_finished(srv, monkeypatch, job_apply_home):
    """UKG Pro (Nikon Precision, live, Oct 2026, the test identity): after the sign-up's email,
    password and Continue, its Register page asks for a name and phone, a consent box ("By checking
    this box, I have read and agree to the Consent and Privacy Policy*", its words beside it, not in a
    label) and "Create account", greyed out until the box is ticked. The desk said the account was made
    as that page came up, filled it as an application, and stopped: it couldn't find the button. It
    finishes the account now (the consent ticked, never the text messages box), and says it's made
    only once the site goes on to the application."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    saved_password(monkeypatch)
    manage_accounts(job_apply_home)
    job = srv.add_job(url=fixture_url("site/signin-signup-link.html") + "?register", title="FSE",
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

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None):
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

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None):
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


def test_a_refused_email_app_password_is_said_once_and_not_tried_again(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "a-wrong-one")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    job = srv.add_job(url=fixture_url("site/verify-email.html") + "?code", title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    tries = []

    def refused(*args):
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
                        lambda *a: pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time()))
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

    def odd(*a):
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
    monkeypatch.setattr(pipeline.mailbox, "search", lambda *a: pytest.fail("the inbox was read"))

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
    prof = yaml.safe_load((job_apply_home / "profile.yaml").read_text())
    prof["documents"] = {"resume": None}
    (job_apply_home / "profile.yaml").write_text(yaml.safe_dump(prof))
    job = srv.add_job(url=fixture_url("site/upload-form.html"), title="Engineer", company="Example Fab")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"))
            assert r.need == "stuck" and "Resume/CV" in r.reason and "1 question(s)" in r.reason, r.reason
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


def test_a_flow_that_goes_round_in_a_circle_stops_after_one_lap(srv, monkeypatch):
    """Oracle's sites, refusing a code for an address: Next leads to a Continue that goes back
    to the posting. Going round again would only repeat it (and might email another code)."""
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    job = srv.add_job(url=fixture_url("site/cycle-posting.html"), title="FSE", company="Example Fab")["job"]
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
    assert r.need == "stuck" and "went round in a circle" in r.reason, (r.status, r.reason, r.log)
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
    assert sum("question(s) your profile doesn't answer" in line for line in r.log) == 1  # asked once


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
    pages = {f"{EXAMPLE_CORP}{path}": (site / name).read_text() for path, name in (
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
    if srv.browser.is_open:  # (open already: served from now on)
        run(srv.browser._ctx.route("https://**.example/**", handler))
    return f"{EXAMPLE_CORP}/careers/job/1"


async def _crash(tab):
    """Crash a tab's page, as Chrome's "Aw, Snap!" is."""
    crashed = []
    tab.once("crash", lambda _: crashed.append(True))
    with contextlib.suppress(Exception):
        await tab.goto("chrome://crash", timeout=5000)
    await until(lambda: crashed)


@pytest.mark.parametrize("how", ["answered", "resumed", "crashed", "closed", "browser", "home"])
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
    since = r.log[next(i for i, line in enumerate(r.log) if "question(s)" in line):]  # from the pause on
    if how in ("answered", "resumed"):
        # the same tab, its page never loaded again: what was uploaded and filled there is kept
        assert r.page is tab and after[0] == before[0], r.log
        assert loads.count(posting) == 1 and not any(line.startswith(("opened", "its tab")) for line in since), r.log
    else:
        said = {"crashed": "its tab crashed", "closed": "its tab was closed",
                "browser": "the browser had closed", "home": "its tab had gone back to the site's careers home"}[how]
        assert r.page is not tab and loads.count(posting) == 2, (loads, r.log)
        assert any(line.startswith(said) for line in since), r.log
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
    assert said == "Its tab crashed. Press Resume to start this application again.", said
    assert r.need == "questions" and r.page is not crashed[0] and crashed[0].is_closed(), (r.reason, r.log)
    assert any(line.startswith("its tab crashed, so I opened the job again") for line in r.log), r.log


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
    """Submit pressed on the desk for a job whose tab was closed: it says to press Resume, and
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
    assert (r.status, r.need) == ("needs_you", "stuck") and "Press Resume" in r.reason, (r.status, r.need, r.reason)


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
    real_fill = srv.fill_form

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None):
        return pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time()) if sent["yet"] else None

    async def fill_form(fills, *args, **kwargs):
        if any(f.get("value") == "123456" for f in fills):
            held_by.append(applier.current)
        return await real_fill(fills, *args, **kwargs)

    monkeypatch.setattr(pipeline.mailbox, "search", inbox)
    monkeypatch.setattr(srv, "fill_form", fill_form)

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
    (job_apply_home / "profile.yaml").write_text("personal:\n  email: sam@example.com\n   phone: [oops\n")
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
    assert posts == [] and (r.status, r.need) == ("needs_you", "stuck") and "Resume" in r.reason, (r.status, r.reason)
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

    def search(address, password, since, senders, want, allowed_link, before=None, look_back=pipeline.mailbox.LOOK_BACK):
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
    applier._pause(r, "questions", "Answer these")
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


@pytest.mark.parametrize("page, said", [("forbidden.html", "403 Forbidden"), ("not-acceptable.html", "406 Not Acceptable")])
def test_a_site_that_turns_the_browser_away_holds_nothing_up(srv, monkeypatch, page, said):
    """Valleywise Health's postings answer the desk's browser with a bare "403 Forbidden", and
    Deloitte's sign-in a headless one with "406 Not Acceptable" (live, Oct 2026; the desk said it
    couldn't find the button). There's nothing to solve: the person applies in their own browser,
    and the queue isn't held for it as for a bot check."""
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
    assert 'value="sam.rivera@example.com"' in (stop / "page.html").read_text()  # what the site wrote in
    assert json.loads((stop / "snapshot.json").read_text())["note"] == "stopped: stuck"
    assert out["pages"] == [f"pages/{stop.name}.expect.json", f"pages/{stop.name}.html"]
    page = (Path(out["folder"]) / out["pages"][1]).read_text()
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

    applier._pause(r, "tailor", "Waiting for a resume written for this job.")
    run(applier._save_stop(r))
    assert stops() == []
    for _ in range(4):
        applier._pause(r, "stuck", "The page didn't move on.")
        run(applier._save_stop(r))
    assert len(stops()) == pipeline.report.PAGES == 3
    newest = stops()

    async def broken(*args, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(srv.browser, "snapshot", broken)
    applier._pause(r, "sign_in", "Sign in.")
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
    profile = yaml.safe_load(path.read_text())
    for entry in profile["work_history"]:
        entry.pop("start"), entry.pop("end")
    path.write_text(yaml.safe_dump(profile))
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


def test_a_resume_workday_already_lists_isnt_uploaded_again(srv, monkeypatch, job_apply_home):
    """Workday's resume box empties after each upload and lists the file below it: on Resume the
    desk filled the page again and uploaded the resume a second time (live, Oct 2026). A file the
    box lists as uploaded is its value, so it goes up once."""
    import yaml

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text())
    for entry in profile["work_history"]:
        entry.pop("start"), entry.pop("end")  # so the page stops, and Resume fills it again
    path.write_text(yaml.safe_dump(profile))
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
    said, rest = applier._entry_gaps(r, {"url": "https://example.wd1.myworkdayjobs.com/x"}, pending)
    assert "didn't take your profile's answer for: Arizona State University in Education 1 (School or University)" \
        in said
    assert "more blocks than your profile has entries: Work Experience 3 (your profile has no job 3) (From)" in said
    assert "your profile doesn't have" not in said, said
    assert [q["id"] for q in rest] == ["c"]


def test_what_a_jobs_or_schools_block_lacks_is_said_for_its_own_list(srv):
    """A school's missing year is education_history's, not work_history's; a block's "Reason
    for leaving" or "Day" box isn't the profile's to hold, so it stays a question."""
    import yaml

    path = config.profile_path()
    profile = yaml.safe_load(path.read_text())
    profile["education_history"][0].pop("start")
    path.write_text(yaml.safe_dump(profile))
    applier = Applier(srv)
    r = Run(1, "Technician", "Example Litho")
    pending = [{"id": "a", "label": "From", "sublabel": "Year", "section": "Education 1", "required": True},
               {"id": "b", "label": "Reason for Leaving", "section": "Work Experience 1", "required": True},
               {"id": "c", "label": "From", "sublabel": "Day", "section": "Work Experience 2", "required": True}]
    said, rest = applier._entry_gaps(r, {"url": "https://example.wd1.myworkdayjobs.com/x"}, pending)
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
    profile = yaml.safe_load(path.read_text())
    profile["education_history"][0].pop("degree", None)
    path.write_text(yaml.safe_dump(profile))
    applier = Applier(srv)
    r = Run(1, "Technician", "Example Litho")
    pending = [{"id": "a", "label": "Degree*", "section": "Education 1", "required": True, "kind": "listbox"}]
    said, rest = applier._entry_gaps(r, {"url": "https://example.wd1.myworkdayjobs.com/x"}, pending)
    assert "Arizona State University in Education 1 (Degree)" in said, said
    assert "Some college (no degree)" in said and "never a degree you didn't finish" in said, said
    assert "years as start" not in said, said
    assert rest == []


WORKDAY_DEGREES = ["High School", "GED", "Associates", "Bachelors", "Masters", "Doctorate", "PH.D"]


def test_a_degree_list_with_nothing_for_classes_leaves_the_pick_to_the_person(srv):
    """A Workday site's Degree list holds High School, GED, Associates and up, and nothing for
    classes without a degree (live, Oct 2026). For a second block of one school, where the person
    took classes, the desk named only the school and said to add "Some college (no degree)" to the
    profile, which can't go in there. It names the block and leaves that pick to the person."""
    import yaml

    path = config.profile_path()
    profile = yaml.safe_load(path.read_text())
    school = profile["education_history"][0]["school"]
    profile["education_history"].append({"school": school, "degree": "", "start": 2021, "end": 2022})
    path.write_text(yaml.safe_dump(profile))
    applier = Applier(srv)
    r = Run(1, "Technician", "Example Litho")
    site = {"url": "https://example.wd1.myworkdayjobs.com/x"}
    degree = {"id": "a", "label": "Degree*", "section": "Education 2", "required": True, "kind": "listbox",
              "options": WORKDAY_DEGREES}
    said, rest = applier._entry_gaps(r, site, [degree])
    assert f"The Degree list for {school} in Education 2 has no choice for classes without a degree" in said, said
    assert "choose in the browser" in said and "Some college" not in said, said
    assert rest == []

    # a list that has such a choice: the profile can say it
    said, _ = applier._entry_gaps(r, site, [{**degree, "options": [*WORKDAY_DEGREES, "Some College, No Degree"]}])
    assert f"{school} in Education 2 (Degree)" in said and "Some college (no degree)" in said, said

    # written as setup writes it, and turned down by the list: still the person's pick, never "the closest"
    profile["education_history"][1]["degree"] = "Some college (no degree)"
    path.write_text(yaml.safe_dump(profile))
    said, _ = applier._entry_gaps(r, site, [{**degree, "error": "ValueError: doesn't match any option"}])
    assert "no choice for classes without a degree" in said and "closest choice" not in said, said


def test_a_workday_experience_page_is_filled_block_by_block(srv, monkeypatch, job_apply_home):
    """A Workday site's My Experience page as drawn live (Oct 2026): each date a fieldset of its
    own, and an Education block's boxes drawn a moment after its heading. Every job's and
    school's dates go in, the second block of one school is filled once drawn, and its Degree
    (classes, no degree, in a list without such a choice) is named with its block as the person's."""
    import yaml

    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "LATE_BUTTONS_WAIT", 1)
    path = job_apply_home / "profile.yaml"
    profile = yaml.safe_load(path.read_text())
    school = profile["education_history"][0]["school"]
    profile["education_history"].append({"school": school, "degree": "", "major": "Physics", "start": 2021,
                                         "end": 2022})
    path.write_text(yaml.safe_dump(profile))
    job = srv.add_job(url=fixture_url("site/workday-experience-blocks.html"), title="Equipment Technician",
                      company="Example Litho")["job"]
    applier = Applier(srv)

    async def go():
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until(lambda: r.status not in ("queued", "running"), about=state(r))
            return r, await r.page.evaluate(
                "() => [...document.querySelectorAll('[role=spinbutton], [name=school], [name=degree]')]"
                ".filter((e) => e.getClientRects().length).map((e) => e.value || e.textContent)")
        finally:
            await applier.stop()

    r, shown = run(go())
    assert (r.status, r.need) == ("needs_you", "stuck"), (r.status, r.reason, r.log)
    assert f"The Degree list for {school} in Education 2 has no choice for classes without a degree" in r.reason, \
        r.reason
    assert not r.questions, r.questions
    assert shown == ["03", "2021", "06", "2018", "02", "2021", school, "Bachelors", "2016", "2020",
                     school, "Select One", "2021", "2022"], shown


def test_a_posting_that_has_closed_says_so(srv, monkeypatch):
    """Edward Jones' BrassRing page for an expired posting (live, Oct 2026) says "The job posting
    you are looking for has expired or the position has already been filled", and the desk said
    it couldn't find the button. It says the posting has closed."""
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
    assert (r.status, r.need) == ("needs_you", "stuck") and "has closed" in r.reason, (r.reason, r.log)
    assert "expired or the position has already been filled" in r.reason and "find the button" not in r.reason
    assert not r.blocking


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


def _paused_run(srv, need="stuck", reason="I couldn't find the button that moves this application on.", **kw):
    job = srv.add_job(url="https://acme.wd1.myworkdayjobs.com/External/job/x", title="FSE", company="Acme Fab")["job"]
    applier = Applier(srv)
    r = Run(job["id"], "FSE", "Acme Fab", status="running", url="https://acme.wd1.myworkdayjobs.com/External/job/x/apply",
            page_info={"url": "https://acme.wd1.myworkdayjobs.com/External/job/x/apply", "title": "Apply",
                       "headings": ["My Information"], "actions": ["Save and Continue"], "errors": [],
                       "fields": [{"label": "First Name", "kind": "text", "required": True, "empty": False}]})
    applier.runs[job["id"]] = r
    applier._pause(r, need, reason, **kw)
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
    applier._pause(r, "stuck", "I couldn't find the button that moves this application on.")
    assert len(_notes(job_apply_home)) == 1
    applier._pause(r, "submit_failed", "I pressed Submit, but the form is still there.")
    assert len(_notes(job_apply_home)) == 2


@pytest.mark.parametrize("need", ["bot_check", "captcha", "email_code", "your_submit", "tailor", "questions"])
def test_a_stop_thats_the_persons_by_design_isnt_noted(srv, job_apply_home, need):
    """A bot check, a CAPTCHA, an emailed code, the person's own Submit, a tailored resume and
    questions the profile doesn't answer are theirs to do, not the desk getting it wrong."""
    applier, r = _paused_run(srv, need, "Over to you.",
                             questions=[{"id": "1", "label": "Why us?", "kind": "text", "required": True}])
    assert r.need == need and _notes(job_apply_home) == []
    applier._pause(r, "stuck", "The page didn't move on.")  # the same job stuck after it is noted
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
    def broken(job, run, profile=None):
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
            await until(lambda: all(r.status not in ("queued", "running") for r in runs) and applier.current is None,
                        about=[state(r) for r in runs])
            await asyncio.sleep(0.5)
            return runs, [r.page is not None and not r.page.is_closed() for r in runs]
        finally:
            await applier.stop()

    runs, open_ = run(go())
    assert [r.status for r in runs] == ["needs_you"] + ["submitted"] * 5, [(r.status, r.reason) for r in runs]
    assert open_ == [True, False, False, False, True, True], open_


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


def inbox_with_code(monkeypatch, codes=None):
    """An email app password saved, and an inbox whose sign-up code is 123456 (`codes`: each
    kind asked for, in turn)."""
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")

    def inbox(address, password, since, senders, want, allowed_link, before=None, look_back=None):
        if codes is not None:
            codes.append(want)
        return pipeline.mailbox.Found("code", "123456", "careers.example.com", time.time()) if want == "code" else None

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
        "The form needs a file the profile doesn't point to (set documents.resume in profile.yaml): Resume/CV*. "), \
        run_.reason
    nothing = Run(2)
    assert not applier._stop_for_what_is_left(nothing, "", [], []) and nothing.status != "needs_you"
