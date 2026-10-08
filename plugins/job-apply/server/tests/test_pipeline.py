"""The one-button apply pipeline, against a fake multi-step application site
(tests/fixtures/site: posting -> Apply -> Apply Manually -> sign-in -> two form steps ->
review -> submit)."""

import asyncio
import time
from pathlib import Path

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
    """A run's state and the end of its log, for a test that times out waiting on it."""
    return lambda: (r.status, r.need, r.reason, r.url, r.log[-8:])


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
    # served over http, so the link opened in another tab confirms the address for this one
    # (a file:// page has no storage shared between tabs)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(Path(__file__).parent / "fixtures"))
    site = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    page = f"http://127.0.0.1:{site.server_address[1]}/site/verify-email.html"
    job = srv.add_job(url=page + query, title="FSE", company="Example Fab")["job"]
    applier = Applier(srv)
    asked = []

    def inbox(address, password, since, senders, want, allowed_link):
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


def test_a_refused_email_app_password_is_said_once_and_not_tried_again(srv, monkeypatch):
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "a-wrong-one")
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
    assert applier.mail_login() == ("sam.rivera@example.com", "a-new-one")
    assert r.need == "email_code"  # still waiting for the person


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
    header's "Sign In" isn't pressed in its place. The form is filled in for the person to send."""
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
            assert r.need == "sign_in" and "Press its sign-in button" in r.reason, (r.reason, r.log)
            state = await r.page.evaluate("() => [window.popupOpened, window.signInTries, em.value, pw.value.length > 0]")
            assert state == [False, 0, "sam.rivera@example.com", True]
            return r
        finally:
            await applier.stop()

    run(go())


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
    # SCREEN's ApplicantStack: the user name is the email, then the name and the email again
    ("create-account-username.html", {"user": "sam.rivera@example.com", "same": True, "name": "Sam Rivera",
                                      "email": "sam.rivera@example.com"}),
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
    assert "filled the Create Account form with your details and saved password" in r.log
    assert srv.tracker().get(job["id"])["status"] != "ready"


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
    assert pipeline._password_tip("https://www.taleo.net/careersection/login") == ""  # not offered on the page
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
