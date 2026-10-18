"""Whole applications on fake job systems: one walk per system, posting -> Apply -> account -> emailed code ->
questions -> review page, through the Job Desk's pipeline as the person runs it.

Each system's pages are hand-written mocks (tests/fixtures/site/<system>-walk-*.html, and the older pages of that
system where they fit), modelled on how the real system behaved in the live checks (tests/fixtures/live and the mock
pages' headers). The `fake_ats` fixture serves them from a local http server, reached through the desk's browser at
the system's own https address (routed: nothing leaves the machine), so the desk knows the system as it does live (its
saved password, its mail domains). What the pages send by email lands in the fake server's inbox, which the desk's
mailbox reader (a `mailbox.search` stand-in) reads as it would the person's. Made-up employers and values only.

In every walk the rules in CLAUDE.md's "Rules the product keeps" hold: nothing is submitted (the final Submit is
never pressed: the person's), an account is made only with manage_accounts on, cookie banners are declined, notices
are agreed only as accept_notices allows (never a job alerts box), and a CAPTCHA is left to the person.
"""

import asyncio
import http.server
import json
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

import pytest
from conftest import browser_available, run
from test_pipeline import notice_settings as settings
from test_pipeline import state, until

import job_apply.pipeline as pipeline
from job_apply.desk import Desk

pytestmark = pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")

SITE = Path(__file__).parent / "fixtures" / "site"


class _Handler(http.server.BaseHTTPRequestHandler):
    """The fake job systems' server: GET /site/<page> serves a mock page; POST /__fake/<what> is what a page tells
    its system (an email sent: `mail`; anything else is kept as an event)."""

    fake: "FakeAts"

    def log_message(self, *args):  # (quiet)
        pass

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        name = path.removeprefix("/site/")
        page = SITE / name
        if path.startswith("/site/") and "/" not in name and page.is_file():
            return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
        self._send(404, b"<html><body><h1>Not found</h1></body></html>", "text/html; charset=utf-8")

    def do_POST(self):
        path = urlparse(self.path).path
        size = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(size) or b"{}") if size else {}
        if not path.startswith("/__fake/"):
            return self._send(404, b"{}", "application/json")
        self.fake.told(path.removeprefix("/__fake/"), data)
        self._send(200, b"{}", "application/json")


class FakeAts:
    """A local http server for the mock job systems, its inbox, and what the pages told it (`events`)."""

    def __init__(self) -> None:
        self.inbox: list[dict] = []  # {sender, subject, to, kind, value, received}
        self.events: list[dict] = []  # {what, ...}
        self.looked: list[str] = []  # what the desk asked the inbox for ("code", "link", "reset")
        self._lock = threading.Lock()
        handler = type("Handler", (_Handler,), {"fake": self})
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def told(self, what: str, data: dict) -> None:
        with self._lock:
            if what == "mail":
                self.inbox.append({**data, "received": time.time()})
            else:
                self.events.append({"what": what, **data})

    def said(self, what: str) -> list[dict]:
        """The events a page told the server of this kind."""
        with self._lock:
            return [e for e in self.events if e["what"] == what]

    def _fetch(self, method: str, path: str, body: bytes | None) -> tuple[int, str, bytes]:
        request = urllib.request.Request(self.base + path, data=body, method=method,
                                         headers={"Content-Type": "application/json"} if body else {})
        try:
            with urllib.request.urlopen(request, timeout=10) as answer:
                return answer.status, answer.headers.get("Content-Type") or "text/html", answer.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type") or "text/html", e.read()

    async def serve(self, srv, origin: str, pages: list[tuple[str, str]]) -> None:
        """Serve the mock pages at `origin` (a job system's own https address) in the desk's browser: a request whose
        path (or path?query, for a system that tells its pages by their query: SuccessFactors') matches one of
        `pages`' patterns (re.fullmatch) gets that page from the fake server, one to /__fake/ goes to the server as it
        is, and anything else there is the server's 404. Nothing reaches the real site."""

        async def handler(route):
            request = route.request
            parts = urlparse(request.url)
            path = parts.path
            if path.startswith("/__fake/"):
                target = path
            else:
                name = next((page for pattern, page in pages
                             if re.fullmatch(pattern, path) or re.fullmatch(pattern, f"{path}?{parts.query}")), None)
                target = f"/site/{name}" if name else "/site/missing"
            status, kind, body = await asyncio.to_thread(self._fetch, request.method, target, request.post_data_buffer)
            await route.fulfill(status=status, content_type=kind, body=body)

        await srv.browser.page()
        await srv.browser._ctx.route(origin + "/**", handler)

    def search(self, address, password, since, senders, want, allowed_link, before=None, look_back=120, others=None):
        """mailbox.search over the fake inbox, as the real one reads the person's: the newest mail of the kind wanted
        (a link only where allowed_link takes it), from an allowed sender, come in the window asked about, and not
        another employer's (`others`, by its sender's domain and subject)."""
        self.looked.append(want)
        with self._lock:
            mail = list(self.inbox)
        got = []
        for m in mail:
            domain = m["sender"].rpartition("@")[2].lower()
            if m["kind"] != want or m["received"] < since - look_back or (before is not None and m["received"] >= before):
                continue
            if not any(domain == d or domain.endswith("." + d) for d in senders):
                continue
            if want != "code" and not allowed_link(m["value"]):
                continue
            if others is not None and others(domain, m.get("subject") or ""):
                continue
            got.append(m)
        if not got:
            return None
        m = max(got, key=lambda m: m["received"])
        return pipeline.mailbox.Found(want, m["value"], m["sender"].rpartition("@")[2].lower(), m["received"])


@pytest.fixture
def fake_ats(monkeypatch):
    """The fake job systems' server and inbox, with the desk watching that inbox (an email app password saved). No
    code is taken from it yet (the desk keeps those for its whole process)."""
    pipeline._MAIL_TAKEN.clear()
    fake = FakeAts()
    monkeypatch.setattr(pipeline, "POLL_SECONDS", 0.3)
    monkeypatch.setattr(pipeline, "MAIL_POLL_SECONDS", 0)
    monkeypatch.setenv("JOB_APPLY_SECRET_EMAIL_PASSWORD", "an-app-password")
    monkeypatch.setattr(pipeline.mailbox, "imap_host", lambda address: "imap.example.com")
    monkeypatch.setattr(pipeline.mailbox, "search", fake.search)
    yield fake
    fake.close()
    pipeline._MAIL_TAKEN.clear()


def stopped(r) -> bool:
    """The job is stopped for the person, or on its review page: not running, and not waiting on an emailed code or
    link (the desk reads those from the inbox itself)."""
    return r.status not in ("queued", "running") and r.need != "email_code"


async def until_stopped(r, also=lambda: True) -> None:
    """Until the job has stopped (and `also`), for a minute and a half at most: a walk's legs take up to half a
    minute here, more on a loaded CI machine."""
    await until(lambda: stopped(r) and also(), timeout=90, about=state(r))


def no_submit(fake: FakeAts, srv, r) -> None:
    """The rules every walk keeps at its end: the final Submit never pressed, nothing marked applied, no cookies
    accepted."""
    assert fake.said("submit") == [], fake.events
    assert not any(line.startswith(("pressed “Submit”", "clicked “Submit”")) for line in r.log), r.log
    assert srv.tracker().get(r.job_id)["status"] != "applied"
    assert all(e["choice"] == "declined" for e in fake.said("cookies")), fake.events


# ---------------------------------------------------------------------------------------- Oracle Recruiting Cloud
ORACLE = "https://eexm.fa.us2.oraclecloud.com"
ORACLE_JOB = "/hcmUI/CandidateExperience/en/sites/CX_1/job/1001"
ORACLE_PAGES = [(re.escape(ORACLE_JOB), "oracle-walk-posting.html"),
                (re.escape(ORACLE_JOB) + "/apply/email", "oracle-walk-email.html"),
                (re.escape(ORACLE_JOB) + r"/apply/section/\d", "oracle-walk-apply.html")]


@pytest.mark.slow
def test_oracle_recruiting_walk(srv, fake_ats, job_apply_home):
    """Oracle Recruiting Cloud, start to review page: the posting's cookie consent declined, Apply Now, the email step
    (its terms agreed to, as accept_notices allows), the code Oracle emails read from the inbox and typed a digit a box,
    the resume first, Contact Information and the Address lookups from the profile, the questions (one only the person
    can answer, asked on the desk and answered there), the E-Signature, and the review page, where Submit is left to
    the person. The job alerts box stays empty."""
    job = srv.add_job(url=ORACLE + ORACLE_JOB, title="Field Service Technician", company="Example Fab")["job"]
    desk = Desk(srv)
    applier = desk.applier

    async def go():
        await fake_ats.serve(srv, ORACLE, ORACLE_PAGES)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until_stopped(r)
            asked = (r.status, r.need, [q["label"] for q in r.questions], r.reason)
            if r.need == "questions":
                desk.answer(job["id"], [{"label": q["label"], "value": "Yes", "remember": False} for q in r.questions])
                await until_stopped(r)
            return r, asked, await r.page.evaluate("() => window.answers ? window.answers() : null")
        finally:
            await applier.stop()

    r, asked, answers = run(go())
    assert (r.status, r.need) == ("ready", ""), (r.status, r.need, r.reason, r.log)
    assert r.page.url.endswith("/apply/section/3"), r.page.url
    assert "reached the review page" in r.log, r.log
    # (the code: sent once, read from the inbox, typed in)
    assert [m["kind"] for m in fake_ats.inbox] == ["code"] and "code" in fake_ats.looked, (fake_ats.inbox, fake_ats.looked)
    assert "entered the code from your email (sent from oraclecloud.com)" in r.log, r.log
    # (the questions: the one the profile can't answer was the person's)
    assert asked[:2] == ("needs_you", "questions") and asked[2] == [
        "Are you willing to work rotating 12-hour shifts, including weekends?"], asked
    assert answers["resume"] == "resume.pdf", answers
    assert answers["2"]["firstName"] == "Sam" and answers["2"]["lastName"] == "Rivera", answers
    assert answers["2"]["email"] == "sam.rivera@example.com", answers
    assert answers["picked"]["region1"] == "Maricopa", answers
    final = answers["3"]
    assert (final["authorized"], final["sponsorship"], final["adult"], final["shifts"]) == ("Yes", "No", "Yes", "Yes"), final
    assert final["heard"] == "LinkedIn" and final["fullName"] == "Sam Rivera", final
    assert final["jobAlerts"] is False, final  # (never a job alerts box)
    assert fake_ats.said("cookies") == [{"what": "cookies", "choice": "declined"}], fake_ats.events
    assert "ticked “I agree with the terms and conditions.” for you (settings.accept_notices)" in r.log, r.log
    no_submit(fake_ats, srv, r)


@pytest.mark.parametrize("how", [{"accept_notices": False}, {"submit_mode": "dry_run"}], ids=["notices-off", "practice"])
def test_oracle_walk_leaves_its_terms_to_the_person_where_notices_are_theirs(srv, fake_ats, job_apply_home, how):
    """The same Oracle walk with settings.accept_notices off, or in practice mode (which agrees to nothing): the email
    step's terms box is the person's, put to them on the desk. It's left unticked and Next unpressed, so no code is
    sent."""
    settings(job_apply_home, **how)
    job = srv.add_job(url=ORACLE + ORACLE_JOB, title="Field Service Technician", company="Example Fab")["job"]
    applier = Desk(srv).applier

    async def go():
        await fake_ats.serve(srv, ORACLE, ORACLE_PAGES)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until_stopped(r)
            return r, await r.page.evaluate("() => [document.getElementById('legal').checked, "
                                            "sessionStorage.getItem('orc.code')]")
        finally:
            await applier.stop()

    r, (ticked, code) = run(go())
    assert (r.status, r.need) == ("needs_you", "questions") and r.page.url.endswith("/apply/email"), (r.reason, r.log)
    assert [q["label"] for q in r.questions] == ["I agree with the terms and conditions."], r.questions
    assert (ticked, code) == (False, None) and fake_ats.inbox == [], (ticked, code, fake_ats.inbox)
    assert not any(line.startswith(("ticked", "agreed")) for line in r.log), r.log
    assert fake_ats.said("cookies") == [{"what": "cookies", "choice": "declined"}], fake_ats.events  # (as before)


# ---------------------------------------------------------------------------------------- iCIMS
ICIMS = "https://careers-examplefab.icims.com"
ICIMS_JOB = "/jobs/2002/field-service-technician"
ICIMS_PAGES = [(re.escape(ICIMS_JOB) + "/job", "icims-walk-posting.html"),
               (re.escape(ICIMS_JOB) + "/login", "icims-walk-login.html"),
               (re.escape(ICIMS_JOB) + "/candidate", "icims-walk-apply.html")]
HCAPTCHA = "https://newassets.hcaptcha.com"


def _frame(r, part: str):
    return next((f for f in r.page.frames if part in f.url), None)


# The desk's bug this walk found (to be fixed in the desk, with a review): Applier._sign_in reads only the page's own
# boxes, never a frame's ("never a password box inside a frame from another site"), but iCIMS' sign-in and Create
# Account are drawn in the employer's iCIMS frame, on iCIMS' own address. So past the CAPTCHA the framed walk stops on
# "Sign in (or create your account) on iCIMS" with an iCIMS password saved and manage_accounts on. Its boxes count
# where the frame's own address is the saved password's system (password_for of the frame's url).
ICIMS_FRAME_BUG = ("desk bug: Applier._sign_in skips every frame's boxes, so the saved iCIMS password and manage_accounts "
                   "never reach iCIMS' own sign-in and Create Account inside its frame")


@pytest.mark.slow
@pytest.mark.parametrize("framed", [pytest.param(True, marks=pytest.mark.xfail(strict=True, reason=ICIMS_FRAME_BUG)),
                                    False], ids=["framed", "alone"])
def test_icims_walk(srv, fake_ats, job_apply_home, monkeypatch, framed):
    """iCIMS, start to review page, inside the employer's iCIMS frame: the employer's cookie bar declined, "Apply for
    this job online", the Privacy Notice acknowledged (accept_notices), the email step, whose Next brings up hCaptcha's
    pictures: the desk stops there for the person and never touches the challenge. Once the person has solved it, the
    desk carries on by itself: the new candidate's account made with the saved iCIMS password (manage_accounts; never
    its job alerts box), the code iCIMS emails read from the inbox, the profile with the resume, the questions (one
    the person answers on the desk), and the review page, whose attestation is ticked (accept_notices) and whose Submit
    Application is left to the person. "alone": the same pages opened without the employer's page around them (an
    iCIMS address with in_iframe=1, as the desk's search finds them), so without its cookie bar."""
    monkeypatch.setenv("JOB_APPLY_SECRET_ICIMS_PASSWORD", "not-a-real-password")
    settings(job_apply_home, manage_accounts=True)
    url = ICIMS + ICIMS_JOB + "/job" + ("" if framed else "?in_iframe=1")
    job = srv.add_job(url=url, title="Field Service Technician", company="Example Fab")["job"]
    desk = Desk(srv)
    applier = desk.applier

    async def go():
        await fake_ats.serve(srv, ICIMS, ICIMS_PAGES)
        await fake_ats.serve(srv, HCAPTCHA, [(r"/captcha/.*", "icims-walk-hcaptcha.html")])
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until_stopped(r)
            at_captcha = (r.status, r.need, r.reason, r.captcha, list(r.log))
            before = (fake_ats.said("account"), list(fake_ats.inbox))
            await asyncio.sleep(2)  # (the desk's polls meanwhile: none of them touches the challenge)
            still = (r.status, r.need)
            await _frame(r, "/login").evaluate("() => window.solved()")  # the person solves it
            await until_stopped(r, lambda: r.need not in ("bot_check", "captcha"))
            asked = (r.status, r.need, [q["label"] for q in r.questions], r.reason)
            if r.need == "questions":
                desk.answer(job["id"], [{"label": q["label"], "value": "Yes", "remember": False} for q in r.questions])
                await until_stopped(r)
            frame = _frame(r, "/candidate") if framed else r.page.main_frame
            return r, at_captcha, before, still, asked, await frame.evaluate("() => window.answers()") if frame else None
        finally:
            await applier.stop()

    r, at_captcha, (made, mail), still, asked, answers = run(go())
    status, need, reason, captcha, log = at_captcha
    assert (status, need, captcha) == ("needs_you", "bot_check", True), at_captcha
    assert "CAPTCHA" in reason, reason
    assert still == ("needs_you", "bot_check"), still
    assert fake_ats.said("captcha") == [], fake_ats.events  # (never a press in the challenge)
    assert made == [] and mail == [], (made, mail)  # (no account, no email, before the person has passed the check)
    assert any(line.startswith("agreed to Example Fab's “I Acknowledge the Privacy Notice”") for line in log), log
    # (past the check: the account made with the saved password, and on to the questions; framed, the bug's stop)
    assert asked[:3] == ("needs_you", "questions", ["Do you have a valid driver's license? *"]), asked
    [account] = fake_ats.said("account")
    assert (account["email"], account["password"], account["alerts"]) == (
        "sam.rivera@example.com", "not-a-real-password", False), account  # (its own system's password; never alerts)
    assert "created your account on iCIMS with your saved password" in r.log, r.log
    assert "entered the code from your email (sent from icims.com)" in r.log, r.log
    assert (r.status, r.need) == ("ready", ""), (r.status, r.need, r.reason, r.log)
    assert "reached the review page" in r.log, r.log
    assert answers["resume"] == "resume.pdf" and answers["state"] == "Arizona" and answers["zip"] == "85225", answers
    assert (answers["authorized"], answers["sponsorship"], answers["license"]) == ("Yes", "No", "Yes"), answers
    assert answers["gender"] == "Decline to self-identify", answers
    assert answers["certify"] is True, answers
    no_submit(fake_ats, srv, r)
    assert fake_ats.said("cookies") == ([{"what": "cookies", "choice": "declined"}] if framed else []), fake_ats.events


# ---------------------------------------------------------------------------------------- Greenhouse
GREENHOUSE = "https://job-boards.greenhouse.io"


@pytest.mark.slow
def test_greenhouse_walk(srv, fake_ats, job_apply_home):
    """Greenhouse, in practice mode: a job-boards posting is opened as Greenhouse's own one-page form
    (embed/job_app?for=<board>&token=<job id>), which is the review page too: no account and no code (Greenhouse
    emails its security code only once Submit is pressed, which stays the person's). Its boxes and react-select lists
    from the profile (Location (City) a search whose Chandler is picked in Arizona), the resume under "Attach", the
    questions (whether the person worked there before, from the profile's employers), the voluntary
    self-identification. In practice mode its consent to the privacy notice and terms is the person's, asked on the
    desk; the marketing opt-in is never chosen."""
    settings(job_apply_home, submit_mode="dry_run")
    job = srv.add_job(url=GREENHOUSE + "/examplelitho/jobs/3003", title="Field Service Technician",
                      company="Example Litho")["job"]
    desk = Desk(srv)
    applier = desk.applier
    consent = "I have read and agree to the Privacy notice and Terms of use.*"

    async def go():
        await fake_ats.serve(srv, GREENHOUSE, [(r"/embed/job_app", "greenhouse-walk-form.html")])
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until_stopped(r)
            asked = (r.status, r.need, sorted(q["label"] for q in r.questions), r.reason, r.page.url)
            if r.need == "questions":
                desk.answer(job["id"], [{"label": q["label"], "value": "Yes", "remember": False} for q in r.questions])
                await until_stopped(r)
            return r, asked, await r.page.evaluate("() => window.answers ? window.answers() : null")
        finally:
            await applier.stop()

    r, asked, answers = run(go())
    assert asked[4] == GREENHOUSE + "/embed/job_app?for=examplelitho&token=3003", asked
    assert asked[:3] == ("needs_you", "questions", [consent]), asked
    assert (r.status, r.need) == ("ready", ""), (r.status, r.need, r.reason, r.log)
    assert "reached the review page" in r.log, r.log
    assert (answers["first_name"], answers["last_name"], answers["email"]) == ("Sam", "Rivera", "sam.rivera@example.com")
    assert answers["candidate-location"] == "Chandler, Arizona, United States", answers
    assert answers["country"] == "United States" and answers["resume"] == "resume.pdf", answers
    assert answers["question_authorized"] == "I am authorized to work in this country for any employer", answers
    assert (answers["question_sponsorship"], answers["question_before"]) == ("No", "No"), answers
    assert answers["gender"] == "Decline to self-identify", answers
    assert answers["question_consent"] == "Yes", answers  # (the person's answer)
    assert "question_marketing" not in answers, answers  # (never a marketing opt-in)
    assert not any(line.startswith(("agreed", "ticked")) for line in r.log), r.log  # (practice mode agrees to nothing)
    assert fake_ats.inbox == [] and fake_ats.said("account") == [], (fake_ats.inbox, fake_ats.events)
    no_submit(fake_ats, srv, r)


# ---------------------------------------------------------------------------------------- SuccessFactors
SF = "https://career4.successfactors.com"
SF_PAGES = [(r"/career\?.*career_ns=job_listing.*", "sf-walk-posting.html"),
            (r"/career\?.*career_ns=job_application.*", "sf-walk-signin.html"),
            (r"/career\?.*login_ns=register.*", "sf-create-account.html"),
            (r"/step1\.html", "sf-walk-apply.html")]  # (where the Create Account goes on to)


@pytest.mark.slow
def test_successfactors_walk(srv, fake_ats, job_apply_home, monkeypatch):
    """SuccessFactors' older career site, start to its final button: the posting's "Apply now »", its Sign In with the
    saved SuccessFactors password (refused: no account yet), its Create an Account filled from the profile with that
    password, its data privacy statement opened and accepted (accept_notices; never its marketing box), the account
    made (manage_accounts), the passcode it emails read from the inbox, then the one-page application from the profile
    and the resume, the question only the person can answer asked on the desk, and its "Apply", the button that sends
    it all, left to the person."""
    monkeypatch.setenv("JOB_APPLY_SECRET_SUCCESSFACTORS_PASSWORD", "Not-a-real-Password1")
    settings(job_apply_home, manage_accounts=True)
    job = srv.add_job(url=SF + "/career?career_ns=job_listing&company=examplefab&career_job_req_id=4004",
                      title="Field Service Technician", company="Example Fab")["job"]
    desk = Desk(srv)
    applier = desk.applier

    async def go():
        await fake_ats.serve(srv, SF, SF_PAGES)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until_stopped(r)
            asked = (r.status, r.need, [q["label"] for q in r.questions], r.reason)
            if r.need == "questions":
                desk.answer(job["id"], [{"label": q["label"], "value": "3", "remember": False} for q in r.questions])
                await until_stopped(r)
            kept = await r.page.evaluate("() => Object.fromEntries(Object.keys(sessionStorage).filter(k => "
                                         "k.startsWith('sf.')).map(k => [k.slice(3), JSON.parse(sessionStorage.getItem(k))]))")
            return r, asked, kept, await r.page.evaluate("() => window.answers ? window.answers() : null")
        finally:
            await applier.stop()

    r, asked, kept, answers = run(go())
    assert asked[:3] == ("needs_you", "questions", [
        "*How many years of experience do you have servicing dry vacuum pumps?"]), asked
    assert (kept.get("statement"), kept.get("created"), kept.get("campaign")) == ("accepted", 1, False), kept
    assert kept.get("declined") is None, kept
    assert any(line.startswith("created your account on") for line in r.log), r.log
    assert "entered the code from your email (sent from successfactors.com)" in r.log, r.log
    assert (r.status, r.need) == ("needs_you", "your_submit"), (r.status, r.need, r.reason, r.log)
    assert "“Apply”, so it's yours to press" in r.reason, r.reason
    assert (answers["firstName"], answers["lastName"], answers["email"]) == ("Sam", "Rivera", "sam.rivera@example.com")
    assert (answers["state"], answers["country"], answers["resume"]) == ("Arizona", "United States", "resume.pdf"), answers
    assert (answers["authorized"], answers["sponsorship"], answers["cleanroom"]) == ("Yes", "No", "Yes"), answers
    assert answers["pumps"] == "3" and answers["gender"] == "Decline to self-identify", answers
    no_submit(fake_ats, srv, r)


# ---------------------------------------------------------------------------------------- UKG Pro
UKG = "https://recruiting.ultipro.com"
UKG_BOARD = "/EXA1000EXFB/JobBoard/0a1b2c3d-0000-4000-8000-000000005005"
UKG_PAGES = [(re.escape(UKG_BOARD) + "/OpportunityDetail", "ukg-walk-posting.html"),
             # (the older UKG pages, in the board's folder as their links have them)
             (re.escape(UKG_BOARD) + "/signin-signup-link.html", "signin-signup-link.html"),
             (re.escape(UKG_BOARD) + "/signup.html", "signup.html"),
             (re.escape(UKG_BOARD) + "/ukg-register.html", "ukg-register.html"),
             (re.escape(UKG_BOARD) + "/step1.html", "ukg-walk-apply.html")]  # (where the Register page goes on to)


@pytest.mark.slow
def test_ukg_walk(srv, fake_ats, job_apply_home, monkeypatch):
    """UKG Pro, start to review page: the posting's "Apply Now", UKG's sign-in with the saved UKG password (refused: no
    account yet), its "Sign up" (the email and that password, Continue), its Register page (names and phone from the
    profile, its Consent and Privacy Policy ticked as accept_notices allows, never its text messages box, then
    "Create account": manage_accounts), and the application's steps from the profile, the resume, the question only
    the person can answer asked on the desk, and the review page, whose Submit is left to the person. (UKG Pro emailed
    no code in the live checks: none here.)"""
    monkeypatch.setenv("JOB_APPLY_SECRET_UKG_PASSWORD", "not-a-real-password")
    settings(job_apply_home, manage_accounts=True)
    job = srv.add_job(url=UKG + UKG_BOARD + "/OpportunityDetail?opportunityId=5005", title="Field Service Technician",
                      company="Example Fab")["job"]
    desk = Desk(srv)
    applier = desk.applier

    async def go():
        await fake_ats.serve(srv, UKG, UKG_PAGES)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until_stopped(r)
            asked = (r.status, r.need, [q["label"] for q in r.questions], r.reason)
            if r.need == "questions":
                desk.answer(job["id"], [{"label": q["label"], "value": "Yes", "remember": False} for q in r.questions])
                await until_stopped(r)
            kept = await r.page.evaluate("() => Object.fromEntries(Object.keys(sessionStorage).filter(k => "
                                         "k.startsWith('ukg.')).map(k => [k.slice(4), JSON.parse(sessionStorage.getItem(k))]))")
            return r, asked, kept
        finally:
            await applier.stop()

    r, asked, kept = run(go())
    assert asked[:3] == ("needs_you", "questions", ["Are you willing to work overtime and weekends as needed? *"]), asked
    assert (kept.get("created"), kept.get("texts"), kept.get("presses")) == (True, False, 1), kept
    assert any(line.startswith("created your account on") for line in r.log), r.log
    assert (r.status, r.need) == ("ready", ""), (r.status, r.need, r.reason, r.log)
    assert r.page.url.endswith("/step1.html?step=review") and "reached the review page" in r.log, (r.page.url, r.log)
    answers = kept["answers"]
    assert (answers["first"], answers["last"], answers["email"]) == ("Sam", "Rivera", "sam.rivera@example.com"), answers
    assert (answers["state"], answers["zip"], answers["resume"]) == ("Arizona", "85225", "resume.pdf"), answers
    assert (answers["authorized"], answers["sponsorship"], answers["overtime"]) == ("Yes", "No", "Yes"), answers
    assert answers["gender"] == "Decline to self-identify", answers
    no_submit(fake_ats, srv, r)


@pytest.mark.parametrize("how", [{"manage_accounts": False}, {"manage_accounts": True, "submit_mode": "dry_run"}],
                         ids=["accounts-off", "practice"])
def test_ukg_walk_leaves_the_account_to_the_person_where_accounts_are_theirs(srv, fake_ats, job_apply_home, monkeypatch,
                                                                              how):
    """The same UKG walk with settings.manage_accounts off, or in practice mode (which makes no account, whatever
    manage_accounts says): the sign-up is filled with the saved password, and its Continue is the person's. No account
    is made."""
    monkeypatch.setenv("JOB_APPLY_SECRET_UKG_PASSWORD", "not-a-real-password")
    settings(job_apply_home, **how)
    job = srv.add_job(url=UKG + UKG_BOARD + "/OpportunityDetail?opportunityId=5005", title="Field Service Technician",
                      company="Example Fab")["job"]
    applier = Desk(srv).applier

    async def go():
        await fake_ats.serve(srv, UKG, UKG_PAGES)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            await until_stopped(r)
            return r, await r.page.evaluate("() => [window.created, sessionStorage.getItem('ukg.created')]")
        finally:
            await applier.stop()

    r, (pressed, created) = run(go())
    assert (r.status, r.need) == ("needs_you", "sign_in") and r.page.url.endswith("/signup.html?register"), (r.reason, r.log)
    assert (pressed, created) == (0, None), (pressed, created, r.log)
    assert "filled the Create Account form with your details and saved password" in r.log, r.log
    assert not any(line.startswith(("pressed “Continue”", "created your account")) for line in r.log), r.log


# ---------------------------------------------------------------------------------------- BrassRing
BRASSRING = "https://sjobs.brassring.com"
BR_HOME = "/TGnewUI/Search/Home"
BR_PAGES = [(re.escape(BR_HOME) + "/Home", "brassring-signin.html"),
            (re.escape(BR_HOME) + r"(?:/consents|/atwill)?/step1\.html", "brassring-walk-apply.html"),
            (re.escape(BR_HOME) + "/consents/start", "brassring-consents.html"),
            (re.escape(BR_HOME) + "/atwill/start", "brassring-at-will.html")]


@pytest.mark.slow
def test_brassring_walk(srv, fake_ats, job_apply_home, monkeypatch):
    """BrassRing (TGnewUI), start to review page: the posting's "Apply to job", its sign-in with the saved BrassRing
    password (refused: no account yet), its Create Account's email step, the passcode it emails read from the inbox and
    typed a digit a box, its account form with that password and a security question, which is the person's (asked on
    the desk, answered there, never remembered), the account made (manage_accounts), "Let's get started", the
    Personal Information with the resume, the Communication Consent & Privacy Notice and the Employment At Will steps
    (agreed as accept_notices allows), the questions (one only the person can answer), and the review page, whose
    Submit is left to the person."""
    monkeypatch.setenv("JOB_APPLY_SECRET_BRASSRING_PASSWORD", "not-a-real-password")
    settings(job_apply_home, manage_accounts=True)
    job = srv.add_job(url=BRASSRING + BR_HOME + "/Home?partnerid=25678&siteid=5374&selectmenu&apply&mail",
                      title="Senior Analyst", company="Example Corp")["job"]
    desk = Desk(srv)
    applier = desk.applier
    stops = []

    async def go():
        await fake_ats.serve(srv, BRASSRING, BR_PAGES)
        applier.start()
        try:
            r = applier.enqueue(job["id"])
            for _ in range(6):  # (the account's security question, then the application's questions)
                await until_stopped(r)
                stops.append((r.status, r.need, [q["label"] for q in r.questions], r.reason))
                if r.need not in ("questions", "sign_in") or not r.questions:
                    break
                # (the person's: the security question and its answer; their consents to be contacted and to the
                # employer's rules on AI, agreed; the at-will statement, ticked; and no Series 7)
                desk.answer(job["id"], [
                    {"label": f"{q.get('section') or ''} | {q['label']} | {q.get('sublabel') or ''}",
                     "value": "In what city were you born?" if q.get("options") else "Flagstaff Ridge 1987",
                     "remember": False} if q.get("account") else
                    {"label": q["label"], "value": True if q.get("kind") == "checkbox" else "No"
                     if "No" in (q.get("options") or []) else (q.get("options") or ["No"])[0], "remember": False}
                    for q in r.questions])
            kept = await r.page.evaluate("() => Object.fromEntries(Object.keys(sessionStorage).map(k => [k, "
                                         "sessionStorage.getItem(k)]))")
            return r, kept, await r.page.evaluate("() => window.answers ? window.answers() : null")
        finally:
            await applier.stop()

    r, kept, answers = run(go())
    assert [s[1] for s in stops] == ["sign_in", "questions", "questions", "questions", ""], stops
    assert "Answer them here" in stops[0][3], stops  # (the security question: the person's)
    # (the consents: the privacy notice's acknowledged for the person, as accept_notices allows; being contacted, texts
    # and the employer's rules on AI are theirs. The at-will statement is theirs too)
    assert len(stops[1][2]) == 3 and "electronic communications" in stops[1][2][0], stops
    assert any(line.startswith("picked “I agree” for “By clicking the 'I agree' button below, you are acknowledging")
               and line.endswith("for you (settings.accept_notices)") for line in r.log), r.log
    assert stops[2][2] == ["I understand and agree"], stops
    assert stops[3][2] == ["*Do you currently hold a FINRA Series 7 license?"], stops
    assert "entered the code from your email (sent from brassring.com)" in r.log, r.log
    assert json.loads(kept["br.accountMade"]) == 1 and json.loads(kept["br.accountForm"])["passwords"] == 2, kept
    assert kept.get("consents.chosen") and kept.get("atwill.agreed"), kept
    assert (r.status, r.need) == ("ready", ""), (r.status, r.need, r.reason, r.log)
    assert "reached the review page" in r.log, r.log
    assert (answers["first"], answers["last"], answers["email"]) == ("Sam", "Rivera", "sam.rivera@example.com"), answers
    assert (answers["phone"], answers["state"], answers["resume"]) == ("480-555-0123", "Arizona", "resume.pdf"), answers
    assert (answers["authorized"], answers["sponsorship"], answers["series"]) == ("Yes", "No", "No"), answers
    assert "Flagstaff Ridge 1987" not in "\n".join(r.log), r.log  # (the security answer said nowhere)
    no_submit(fake_ats, srv, r)
