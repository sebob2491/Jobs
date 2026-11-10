"""Phone alerts (phone.py, the owner's choice of Nov 2): the desk pushes what's new to the person's phone through ntfy.
The topic is a secret (secrets.yaml, never the page's state or HTML); a message says only counts and kinds unless the
person asks for the employer and job; one a job a stop, close ones batched; a daily summary; failures said once and
never raised; nothing in practice mode or the live check. Every POST goes to an httpx.MockTransport: nothing leaves."""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import yaml
from conftest import browser_available, open_desk, person_browser, run

from job_apply import config, phone
from job_apply.desk import Desk
from job_apply.pipeline import Run

TOPIC = "jobdesk-" + "T" * 24


@pytest.fixture(autouse=True)
def _not_a_test_run(monkeypatch):
    """The desk as a person runs it: phone alerts are never sent with JOB_APPLY_NEVER_SUBMIT=1 (the live check's)."""
    monkeypatch.delenv("JOB_APPLY_NEVER_SUBMIT", raising=False)
    monkeypatch.delenv("JOB_APPLY_LIVE_TEST_IDENTITY", raising=False)
    monkeypatch.delenv("JOB_APPLY_SECRET_NTFY_TOPIC", raising=False)


class Phone:
    """A stand-in for ntfy: each POST kept as (url, title, text); `fail` makes the next ones fail that way."""

    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []
        self.fail: object = None
        self.tries = 0  # (every POST, failed or not)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.tries += 1
        if isinstance(self.fail, Exception):
            raise self.fail
        if isinstance(self.fail, int):
            return httpx.Response(self.fail, request=request)
        self.sent.append((str(request.url), request.headers.get("title", ""), request.content.decode("utf-8")))
        return httpx.Response(200, json={"id": "x"}, request=request)

    @property
    def texts(self) -> list[str]:
        return [text for _, _, text in self.sent]


def _applier(*runs: Run) -> SimpleNamespace:
    return SimpleNamespace(runs={r.job_id: r for r in runs}, letters=False, letters_problem=None, mail_problem=None,
                           _desk_reads_code=lambda run: False)


def _alerts(applier, tracker=None) -> tuple[phone.PhoneAlerts, Phone]:
    alerts = phone.PhoneAlerts(applier, tracker or (lambda: None))
    fake = Phone()
    alerts.transport = httpx.MockTransport(fake.handler)
    return alerts, fake


def _settings(**settings) -> config.Settings:
    return config.Settings.from_dict({"submit_mode": "review", "phone_summary_at": "off", **settings})


def _stop(job_id: int, need: str = "bot_check", company: str = "Example Fab", title: str = "Field Technician",
          status: str = "needs_you") -> Run:
    return Run(job_id, title, company, status=status, need=need if status == "needs_you" else "",
               reason="Solve the check")


def _tick(alerts, settings, at: float) -> None:
    run(alerts.tick(settings, now=at))


def test_a_new_stop_sends_one_message_with_no_employer_unless_asked(loop):
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    applier = _applier()
    alerts, fake = _alerts(applier)
    t0 = 1_000_000.0
    _tick(alerts, _settings(), t0)  # (the first look: what waits already isn't news)
    applier.runs[1] = _stop(1)
    _tick(alerts, _settings(), t0 + 5)
    assert fake.sent == []  # (a moment for others that come with it)
    _tick(alerts, _settings(), t0 + 5 + phone.GATHER)
    assert fake.sent == [(f"https://ntfy.sh/{TOPIC}", "Job Desk", "A job needs you: a bot check")]
    assert "Example Fab" not in fake.texts[0] and "Technician" not in fake.texts[0]
    # the same stop, looked at again and again: never sent twice
    for i in range(1, 30):
        _tick(alerts, _settings(), t0 + 100 + i * phone.TICK)
    assert len(fake.sent) == 1

    # with Say which employer and job on: the employer and the job too
    applier.runs[2] = _stop(2, "questions", "Example Bank", "Accountant")
    later = t0 + 1000
    _tick(alerts, _settings(phone_alert_details=True), later)
    _tick(alerts, _settings(phone_alert_details=True), later + phone.GATHER)
    assert fake.texts[1] == "A job needs you: questions to answer\n- Example Bank, Accountant: questions to answer"


def test_a_job_that_stops_again_is_a_new_stop_and_close_ones_go_in_one_message(loop):
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    applier = _applier()
    alerts, fake = _alerts(applier)
    t0 = 2_000_000.0
    _tick(alerts, _settings(), t0)
    applier.runs[1] = _stop(1, "sign_in")
    _tick(alerts, _settings(), t0 + 1)
    applier.runs[2] = _stop(2, "bot_check")
    applier.runs[3] = _stop(3, "bot_check")
    applier.runs[4] = _stop(4, status="ready")
    _tick(alerts, _settings(), t0 + 6)
    _tick(alerts, _settings(), t0 + 1 + phone.GATHER)
    assert fake.texts == ["3 jobs need you: a sign-in, 2 bot checks\nAn application is ready for your Submit"]
    # one more within the minute after: held for the next message, not sent on its own straight away
    applier.runs[5] = _stop(5, status="ready")
    applier.runs[1].status, applier.runs[1].need = "running", ""  # (the sign-in done: it goes on)
    _tick(alerts, _settings(), t0 + 20)
    _tick(alerts, _settings(), t0 + 40)
    assert len(fake.sent) == 1
    applier.runs[1].status, applier.runs[1].need = "needs_you", "sign_in"  # (and stops at a sign-in again: a new stop)
    _tick(alerts, _settings(), t0 + 50)
    _tick(alerts, _settings(), t0 + 1 + phone.GATHER + phone.BATCH_GAP)
    assert fake.texts[1] == "A job needs you: a sign-in\nAn application is ready for your Submit"
    # a stop seen to before its message went says nothing
    applier.runs[6] = _stop(6, "captcha")
    _tick(alerts, _settings(), t0 + 500)
    del applier.runs[6]
    _tick(alerts, _settings(), t0 + 500 + phone.GATHER)
    assert len(fake.sent) == 2


def test_what_isnt_the_persons_to_do_sends_nothing(loop):
    """A job the desk tries again by itself after a site's error, a tailored resume (Claude's), and an emailed code the
    desk is reading itself aren't the person's: no alert. The code becomes theirs once the desk stops reading it."""
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    reading = {"on": True}
    applier = _applier()
    applier._desk_reads_code = lambda r: reading["on"]
    alerts, fake = _alerts(applier)
    t0 = 3_000_000.0
    _tick(alerts, _settings(), t0)
    retry = _stop(1, "stuck")
    retry.retry_at = t0 + 600
    applier.runs.update({1: retry, 2: _stop(2, "tailor"), 3: _stop(3, "email_code")})
    _tick(alerts, _settings(), t0 + 1)
    _tick(alerts, _settings(), t0 + 100)
    assert fake.sent == []
    reading["on"] = False
    _tick(alerts, _settings(), t0 + 200)
    _tick(alerts, _settings(), t0 + 200 + phone.GATHER)
    assert fake.texts == ["A job needs you: an emailed code to type"]


def test_cover_letters_stopping_is_said_by_its_first_sentence(loop):
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    applier = _applier()
    applier.letters = True
    alerts, fake = _alerts(applier)
    t0 = 4_000_000.0
    _tick(alerts, _settings(), t0)
    applier.letters_problem = ("Claude Code isn't signed in. Open a terminal and run claude in C:\\Users\\someone to "
                               "sign in.")
    _tick(alerts, _settings(), t0 + 5)
    _tick(alerts, _settings(), t0 + 5 + phone.GATHER)
    assert fake.texts == ["Cover letters stopped: Claude Code isn't signed in."]
    _tick(alerts, _settings(), t0 + 500)  # (the same problem: said once)
    assert len(fake.sent) == 1


def _local(day: datetime, hour: int, minute: int = 0, second: int = 0) -> float:
    return day.replace(hour=hour, minute=minute, second=second, microsecond=0).timestamp()


def test_the_daily_summary_counts_the_day_at_its_time_once(srv):
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    t = srv.tracker()
    sent = [srv.add_job(url=f"https://careers.example.com/fab/{n}", title="Technician", company="Example Fab")["job"]
            for n in range(3)]
    for job in sent[:2]:
        t.update(job["id"], status="applied")
    t.log_email(sent[0]["id"], "thread-1", "interview", "Example Fab wants to talk")
    t.log_email(sent[1]["id"], "thread-2", "confirmation", "We received your application")  # (not a reply)
    # one sent yesterday: not today's
    t.conn.execute("UPDATE jobs SET applied_at = ? WHERE id = ?",
                   ((datetime.now().astimezone() - timedelta(days=2)).isoformat(), sent[2]["id"]))
    t.conn.commit()
    applier = _applier(_stop(7, status="ready"), _stop(8, "sign_in"), _stop(9, "questions"))
    saved = []
    alerts, fake = _alerts(applier, srv.tracker)
    alerts.saved = lambda: saved.append(alerts.summary_day)
    today = datetime.now()
    at_six = _settings(phone_summary_at="18:00")
    _tick(alerts, at_six, _local(today, 17, 59, 30))
    assert fake.sent == []
    _tick(alerts, at_six, _local(today, 18, 0, 3))
    assert fake.texts == ["Today: 2 submitted, 1 ready for you, 2 need you, 1 reply"]
    assert saved == [today.date().isoformat()]  # (kept in desk.json: a restart doesn't send it again)
    for minute in (1, 5, 30):
        _tick(alerts, at_six, _local(today, 18, minute))
    assert len(fake.sent) == 1  # (once a day)
    tomorrow = today + timedelta(days=1)
    _tick(alerts, at_six, _local(tomorrow, 18, 0, 4))
    assert len(fake.sent) == 2
    # a desk opened long after the time (or a computer asleep through it) sends no summary that day
    late, late_phone = _alerts(applier, srv.tracker)
    _tick(late, at_six, _local(today, 17, 0))
    _tick(late, at_six, _local(today, 18, 0) + phone.SUMMARY_LATE + 60)
    assert late_phone.sent == []
    # at the time the person picked; none at all when it's off
    _tick(late, _settings(phone_summary_at="9:30 PM"), _local(today, 21, 30, 2))
    assert len(late_phone.sent) == 1
    off, off_phone = _alerts(applier, srv.tracker)
    _tick(off, _settings(phone_summary_at="off"), _local(today, 18, 0, 2))
    assert off_phone.sent == []


def test_the_summary_day_outlasts_a_restart_of_the_desk(srv):
    desk = Desk(srv)
    desk.phone.summary_day = "2026-11-02"
    desk.phone.saved()
    assert json.loads((config.home() / "desk.json").read_text(encoding="utf-8"))["phone_summary_day"] == "2026-11-02"
    assert Desk(srv).phone.summary_day == "2026-11-02"


@pytest.mark.parametrize("raw, want", [(None, "18:00"), ("18:00", "18:00"), ("6 PM", "18:00"), ("6:30pm", "18:30"),
                                       ("12 am", "00:00"), (1080, "18:00"), (7, "07:00"), ("off", ""), (False, "")])
def test_the_summary_time_reads_as_people_write_it(raw, want):
    assert config._phone_summary_at(raw) == (want, "")


def test_a_summary_time_or_server_that_cant_be_read_is_said():
    s = config.Settings.from_dict({"phone_summary_at": "dinner", "phone_alerts_server": "ntfy.example.com"})
    assert s.phone_summary_at == "18:00" and s.phone_alerts_server == ""
    assert len(s.warnings) == 2 and "phone_summary_at" in s.warnings[0] and "phone_alerts_server" in s.warnings[1]
    own = config.Settings.from_dict({"phone_alerts_server": "https://ntfy.example.com/"})
    assert own.phone_alerts_server == "https://ntfy.example.com" and own.warnings == []


@pytest.mark.parametrize("server, kept", [
    ("https://ntfy.example.com", True), ("http://localhost:8080", True), ("http://127.0.0.1", True),
    ("http://[::1]:2586", True), ("http://ntfy.example.com", False), ("ftp://ntfy.example.com", False),
    ("https://jo:hunter2secret@ntfy.example.com", False), ("https://jo@ntfy.example.com", False),
    ("http://jo:hunter2secret@localhost", False)])
def test_the_server_is_https_with_no_credentials_and_never_said_back(server, kept):
    """The review of dafeaf9: an alert goes over https (http only to this computer itself), and a name and password in
    the server's address are never sent, nor said back on the page (its warning doesn't repeat the address)."""
    s = config.Settings.from_dict({"phone_alerts_server": server})
    assert s.phone_alerts_server == (server if kept else "")
    assert (s.warnings == []) == kept
    assert not any("hunter2secret" in w or "jo@" in w for w in s.warnings)


def test_practice_mode_has_no_setting_of_its_own_for_phone_alerts():
    """The review of dafeaf9: practice mode sends nothing (CLAUDE.md): phone_alerts_in_practice is no setting."""
    assert "phone_alerts_in_practice" not in config.KNOWN_SETTINGS
    said = config.Settings.from_dict({"submit_mode": "dry_run", "phone_alerts_in_practice": True})
    assert any("Unknown settings key 'phone_alerts_in_practice'" in w for w in said.warnings)
    assert "never sends anything" in (phone.held(said) or "")


def test_a_failure_is_said_once_never_raised_and_tried_again_with_the_next_alert(loop, caplog):
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    applier = _applier()
    alerts, fake = _alerts(applier)
    t0 = 5_000_000.0
    _tick(alerts, _settings(), t0)
    fake.fail = httpx.ConnectError(f"can't reach https://ntfy.sh/{TOPIC}")  # (an error that names the address)
    applier.runs[1] = _stop(1)
    with caplog.at_level(logging.WARNING, logger="job_apply.phone"):
        _tick(alerts, _settings(), t0 + 1)
        _tick(alerts, _settings(), t0 + 1 + phone.GATHER)  # (it fails: never raised)
        assert alerts.problem and alerts.problem.startswith("Phone alerts couldn't be sent: ConnectError")
        assert TOPIC not in alerts.problem
        for i in range(1, 20):  # (not tried again every few seconds: with the next alert)
            _tick(alerts, _settings(), t0 + 100 + i * phone.TICK)
    assert fake.tries == 1
    tries = [r for r in caplog.records if "phone alert not sent" in r.getMessage()]
    assert len(tries) == 1 and TOPIC not in caplog.text  # (logged once, without the topic)
    fake.fail = None
    applier.runs[2] = _stop(2, status="ready")
    _tick(alerts, _settings(), t0 + 300)
    _tick(alerts, _settings(), t0 + 300 + phone.GATHER)
    # the one that failed goes with it
    assert fake.texts == ["A job needs you: a bot check\nAn application is ready for your Submit"]
    assert alerts.problem is None
    # a refusal (a 4xx) is said the same way
    fake.fail = 403
    applier.runs[3] = _stop(3, "sign_in")
    _tick(alerts, _settings(), t0 + 600)
    _tick(alerts, _settings(), t0 + 600 + phone.GATHER)
    assert alerts.problem == ("Phone alerts couldn't be sent: the alert service answered 403. The desk tries again "
                              "with the next alert.")


def test_a_failed_alert_is_tried_again_by_itself_after_a_growing_wait(loop):
    """The review of dafeaf9: a failed alert waits for no other job to stop. It's tried again RETRY_FIRST later, then
    after twice that, and so on up to RETRY_MOST apart, until it goes."""
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    applier = _applier()
    alerts, fake = _alerts(applier)
    t0 = 5_500_000.0
    _tick(alerts, _settings(), t0)
    fake.fail = 503
    applier.runs[1] = _stop(1)
    _tick(alerts, _settings(), t0 + 1)
    first = t0 + 1 + phone.GATHER
    _tick(alerts, _settings(), first)
    assert fake.tries == 1 and phone.RETRY_FIRST == 300
    tried_at = []
    for second in range(0, 4 * 3600, 5):
        before = fake.tries
        _tick(alerts, _settings(), first + second)
        if fake.tries > before:
            tried_at.append(second)
    assert tried_at[:5] == [300, 900, 2100, 3900, 5700]  # (300, 600, 1200, 1800, 1800 apart)
    fake.fail = None
    _tick(alerts, _settings(), first + 4 * 3600 + phone.RETRY_MOST)
    assert fake.texts == ["A job needs you: a bot check"] and alerts.problem is None
    assert alerts.retry_wait == phone.RETRY_FIRST  # (gone: the next failure waits the first wait again)


def test_a_failed_summary_is_tried_again_within_three_hours_of_its_time(srv):
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    applier = _applier()
    alerts, fake = _alerts(applier, srv.tracker)
    today = datetime.now()
    at_six = _settings(phone_summary_at="18:00")
    fake.fail = httpx.ConnectError("no network")
    for minute in range(0, 5 * 60):
        _tick(alerts, at_six, _local(today, 18) + minute * 60 + 2)
    tries = fake.tries
    assert tries == 8 and alerts.summary_day is None  # (at 0, 5, 15, 35, 65, 95, 125 and 155 min; none after 3 h)
    # one that goes on a retry goes once
    again, phone2 = _alerts(applier, srv.tracker)
    phone2.fail = 500
    _tick(again, at_six, _local(today, 18, 0, 2))
    phone2.fail = None
    for minute in range(1, 60):
        _tick(again, at_six, _local(today, 18, minute, 2))
    assert phone2.tries == 2 and len(phone2.sent) == 1 and again.summary_day == today.date().isoformat()


def test_a_summary_at_midnight_counts_the_day_that_ended(srv):
    """The review of dafeaf9: a summary at 00:00 is the day just ended's, since the midnight before, not a minute's."""
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    t = srv.tracker()
    today = datetime.now()
    jobs = [srv.add_job(url=f"https://careers.example.com/fab/{n}", title="Technician", company="Example Fab")["job"]
            for n in range(2)]
    for job, days in ((jobs[0], 1), (jobs[1], 2)):  # (yesterday afternoon, and the day before)
        when = (today - timedelta(days=days)).replace(hour=15, minute=0, second=0, microsecond=0).astimezone()
        t.conn.execute("UPDATE jobs SET applied_at = ? WHERE id = ?", (when.isoformat(), job["id"]))
    t.conn.commit()
    alerts, fake = _alerts(_applier(), srv.tracker)
    _tick(alerts, _settings(phone_summary_at="00:00"), _local(today, 0, 0, 30))
    assert fake.texts == ["Yesterday: 1 submitted, 0 ready for you, 0 need you, 0 replies"]


def test_a_look_that_keeps_failing_is_logged_once(loop, caplog):
    """The review of dafeaf9: a look every 5 seconds that fails the same way logs it once, not a traceback each time."""
    applier = _applier()
    applier.runs = None  # (every look fails the same way)
    alerts, _ = _alerts(applier)
    with caplog.at_level(logging.WARNING, logger="job_apply.phone"):
        for i in range(20):
            _tick(alerts, _settings(), 8_000_000.0 + i * phone.TICK)
    assert len([r for r in caplog.records if "a look failed" in r.getMessage()]) == 1


def test_a_letters_problem_reads_as_one_line():
    """The review of dafeaf9: never "Cover letters stopped: Cover letters aren't being written: ..."."""
    from job_apply import letters

    line = phone.letters_line(letters.NOT_SIGNED_IN)
    assert line.startswith("Cover letters aren't being written: ") and "stopped" not in line
    assert line == phone.first_sentence(letters.NOT_SIGNED_IN)
    assert phone.letters_line("The API is down. Try later.") == "Cover letters stopped: The API is down."


def test_a_test_never_posts_without_a_transport_of_its_own(loop, monkeypatch):
    """The review of dafeaf9: a developer's own topic in their environment never gets a test's alerts. conftest takes
    JOB_APPLY_SECRET_NTFY_TOPIC out, and phone.post refuses in a test (PYTEST_CURRENT_TEST) with no transport given."""
    assert "JOB_APPLY_SECRET_NTFY_TOPIC" not in os.environ
    monkeypatch.setenv("JOB_APPLY_SECRET_NTFY_TOPIC", TOPIC)
    posted = []

    async def post(self, *args, **kwargs):
        posted.append(args)
        raise AssertionError("posted for real")

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    alerts = phone.PhoneAlerts(_applier(), lambda: None)
    said = run(alerts.test(_settings()))
    assert said and "from a test" in said and posted == []


def test_nothing_is_sent_in_practice_mode_or_the_live_check(loop, monkeypatch):
    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)

    def sends(settings, env=None) -> int:
        applier = _applier()
        alerts, fake = _alerts(applier)
        _tick(alerts, settings, 6_000_000.0)
        applier.runs[1] = _stop(1)
        _tick(alerts, settings, 6_000_001.0)
        _tick(alerts, settings, 6_000_001.0 + phone.GATHER)
        assert run(alerts.test(settings)) is None or not fake.sent  # (nor the card's test)
        return fake.tries

    assert sends(_settings()) == 2  # (the alert and the test: as a person runs the desk)
    assert sends(_settings(submit_mode="dry_run")) == 0  # (practice mode sends nothing, a test included)
    # the live check and the tests: JOB_APPLY_NEVER_SUBMIT=1 (which makes every profile practice mode)
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    assert sends(_settings()) == 0
    assert "nothing is sent in a test run" in (phone.held(_settings()) or "")


def test_no_topic_no_alert(loop):
    applier = _applier()
    alerts, fake = _alerts(applier)
    _tick(alerts, _settings(), 7_000_000.0)
    applier.runs[1] = _stop(1)
    _tick(alerts, _settings(), 7_000_001.0)
    _tick(alerts, _settings(), 7_000_001.0 + phone.GATHER)
    assert fake.sent == [] and alerts.pending == {}  # (nothing piles up to go when they're turned on)


def _idle_desk(srv) -> Desk:
    config.set_setting("phone_summary_at", "off")  # (no summary of the day in the middle of a test run at 6 PM)
    desk = Desk(srv)
    desk.applier.start = lambda: None
    desk.search.update(status="done", at=time.time())
    return desk


def test_the_card_makes_a_topic_kept_as_a_secret_never_in_the_state_or_page(srv):
    desk = _idle_desk(srv)
    fake = Phone()
    desk.phone.transport = httpx.MockTransport(fake.handler)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False) as c:
                assert (await c.post("/api/phone/make", json={})).status_code == 403  # (the page's key)
                s = (await c.get("/api/state", headers=h)).json()
                assert s["phone"] == {"on": False, "in_environment": False, "details": False, "summary_at": "",
                                      "custom_server": False, "held": None, "problem": None}  # (_idle_desk's off)
                assert (await c.post("/api/phone/test", headers=h, json={})).json()["error"].startswith(
                    "Phone alerts are off")
                # (the topic is made here alone: never saved through Site passwords)
                r = await c.post("/api/password", headers=h, json={"name": "ntfy_topic", "value": TOPIC})
                assert r.status_code == 400 and config.get_secret(config.NTFY_TOPIC_NAME) is None
                made = (await c.post("/api/phone/make", headers=h, json={})).json()
                topic = made["topic"]
                assert made["server"] == "https://ntfy.sh"
                assert topic.startswith("jobdesk-") and len(topic) == len("jobdesk-") + 24
                assert config.NTFY_TOPIC.match(topic)
                assert config.get_secret(config.NTFY_TOPIC_NAME) == topic  # (kept as a password is: the vault here)
                state = await c.get("/api/state", headers=h)
                assert state.json()["phone"]["on"] is True and topic not in state.text
                page = await c.get("/")
                assert topic not in page.text and 'aria-labelledby="phone-h"' in page.text
                # Send a test
                assert (await c.post("/api/phone/test", headers=h, json={})).json() == {"sent": True}
                assert fake.sent == [(f"https://ntfy.sh/{topic}", "Job Desk", fake.texts[0])]
                assert fake.texts[0].startswith("Test from the Job Desk")
                # shown again on asking (to this page alone), and a new one each time it's made
                assert (await c.post("/api/phone/show", headers=h, json={})).json()["topic"] == topic
                # a new one only when the page says replace (the phone subscribed to the old one stops getting them)
                r = await c.post("/api/phone/make", headers=h, json={})
                assert r.status_code == 409 and config.get_secret(config.NTFY_TOPIC_NAME) == topic
                assert topic not in r.text
                again = (await c.post("/api/phone/make", headers=h, json={"replace": True})).json()["topic"]
                assert again != topic and config.get_secret(config.NTFY_TOPIC_NAME) == again
                # the switch and the summary's time: the profile's settings
                assert (await c.post("/api/phone/settings", headers=h,
                                     json={"details": True, "summary_at": "07:15"})).status_code == 200
                written = yaml.safe_load(config.profile_path().read_text(encoding="utf-8"))["settings"]
                assert written["phone_alert_details"] is True and written["phone_summary_at"] == "07:15"
                s = (await c.get("/api/state", headers=h)).json()["phone"]
                assert s["details"] is True and s["summary_at"] == "07:15"
                await c.post("/api/phone/settings", headers=h, json={"summary_at": ""})
                assert (await c.get("/api/state", headers=h)).json()["phone"]["summary_at"] == ""
                assert (await c.post("/api/phone/settings", headers=h,
                                     json={"summary_at": "dinner"})).status_code == 400
                # practice mode: the test is refused, said why
                config.set_setting("submit_mode", "dry_run")
                said = (await c.post("/api/phone/test", headers=h, json={})).json()["error"]
                assert "practice mode" in said and len(fake.sent) == 1
                held = (await c.get("/api/state", headers=h)).json()["phone"]["held"]
                assert "practice mode" in held and "never sends anything" in held
                # a failure: said on the page, never with the topic
                config.set_setting("submit_mode", "review")
                desk.phone.problem = None
                fake.fail = httpx.ConnectError(f"no route to ntfy.sh/{again}")
                desk.applier.runs[41] = Run(41, "Technician", "Example Fab", status="ready")
                for _ in range(3):
                    await desk.phone.tick(now=time.time() + 120)
                    await desk.phone.tick(now=time.time() + 120 + phone.GATHER)
                state = await c.get("/api/state", headers=h)
                assert state.json()["phone"]["problem"].startswith("Phone alerts couldn't be sent")
                assert again not in state.text
                # Turn off: the topic forgotten, the rest of secrets.yaml kept
                config.save_site_password("workday_password", "not-a-real-password")
                assert (await c.post("/api/phone/off", headers=h, json={})).json() == {"on": False}
                assert config.get_secret(config.NTFY_TOPIC_NAME) is None
                assert config.get_secret("workday_password") == "not-a-real-password"
                assert (await c.get("/api/state", headers=h)).json()["phone"]["on"] is False
        finally:
            await desk.stop()

    run(go())


def test_a_topic_from_the_environment_is_used_and_not_changed_here(srv, monkeypatch):
    monkeypatch.setenv("JOB_APPLY_SECRET_NTFY_TOPIC", TOPIC)
    assert phone.topic() == TOPIC and phone.in_environment()
    desk = _idle_desk(srv)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            h = {"x-desk-token": desk.token}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{desk.port}", timeout=20, trust_env=False) as c:
                for action in ("make", "off"):
                    r = await c.post(f"/api/phone/{action}", headers=h, json={})
                    assert r.status_code == 400 and "environment" in r.json()["error"]
                assert TOPIC not in (await c.get("/api/state", headers=h)).text
        finally:
            await desk.stop()

    run(go())


@pytest.mark.skipif(not browser_available(), reason="no Playwright Chromium installed")
def test_the_phone_alerts_card_turns_them_on_and_sends_a_test(srv):
    """Pressed through the real page: Turn on phone alerts shows the topic with the steps, Send a test sends one, and
    the topic is gone from the page once hidden."""
    desk = _idle_desk(srv)
    fake = Phone()
    desk.phone.transport = httpx.MockTransport(fake.handler)

    async def go():
        await desk.start(port=0, open_browser=False)
        try:
            async with person_browser() as browser:
                page = await browser.new_page()

                def accept(d):
                    asyncio.ensure_future(d.accept())
                page.on("dialog", accept)
                await open_desk(page, desk, "settings/phone")  # (the Phone alerts card: Settings, Phone alerts)
                await page.wait_for_selector("#plugin-version:not(:empty)")
                assert await page.inner_text("#phone-pill") == "Off"
                await page.click("[data-act=phone-make]")
                await page.wait_for_selector("#phone-on:not([hidden])")
                shown = await page.inner_text("#phone-topic-name")
                assert shown == config.get_secret(config.NTFY_TOPIC_NAME)
                assert "subscribe to this topic" in await page.inner_text("#phone-topic")
                assert await page.inner_text("#phone-pill") == "On"
                await page.click("[data-act=phone-test]")
                for _ in range(100):
                    if fake.sent:
                        break
                    await page.wait_for_timeout(50)
                assert fake.sent and fake.sent[0][0].endswith("/" + shown)
                # New topic: asked first (the phone stops getting alerts until it subscribes again), then made
                asked = []
                page.remove_listener("dialog", accept)
                page.on("dialog", lambda d: (asked.append(d.message), asyncio.ensure_future(d.accept())))
                await page.click("#phone-on [data-act=phone-make]")
                for _ in range(100):
                    if config.get_secret(config.NTFY_TOPIC_NAME) != shown:
                        break
                    await page.wait_for_timeout(50)
                assert asked and asked[0].startswith("This makes a new topic")
                shown = config.get_secret(config.NTFY_TOPIC_NAME)
                await page.wait_for_function("(t) => document.getElementById('phone-topic-name').textContent === t",
                                             arg=shown)
                # the summary's time: written once the box is left, never while it's typed
                await page.focus("#phone-summary")
                await page.keyboard.type("0715a")
                await page.wait_for_timeout(300)
                assert config.Profile.load().settings.phone_summary_at == ""
                await page.keyboard.press("Enter")
                for _ in range(100):
                    if config.Profile.load().settings.phone_summary_at == "07:15":
                        break
                    await page.wait_for_timeout(50)
                assert config.Profile.load().settings.phone_summary_at == "07:15"
                assert await page.get_attribute("#phone-pill", "aria-live") == "polite"
                await page.click("[data-act=phone-show]")  # (Hide the topic)
                await page.wait_for_selector("#phone-topic", state="hidden")
                assert shown not in await page.content()
                await page.check("#phone-details")
                for _ in range(100):
                    if config.Profile.load().settings.phone_alert_details:
                        break
                    await page.wait_for_timeout(50)
                assert config.Profile.load().settings.phone_alert_details is True
                await browser.close()
        finally:
            await desk.stop()

    run(go())


def test_the_topic_is_never_typed_into_a_page(loop):
    """The review of dafeaf9: Claude's fill_secret types a saved password onto its own site's pages; the topic has no
    site, and a page whose domain spells its name (ntfytopic.com) is somebody else's."""
    from job_apply import server

    config.save_site_password(config.NTFY_TOPIC_NAME, TOPIC)
    for name in ("ntfy_topic", "NTFY_TOPIC", " ntfy-topic "):
        said = run(server.fill_secret("f1", name))
        assert said == {"ok": False, "error": "The phone alerts topic is never typed into a page."}
