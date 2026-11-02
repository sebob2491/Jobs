"""Phone alerts (phone.py, the owner's choice of Nov 2): the desk pushes what's new to the person's phone through ntfy.
The topic is a secret (secrets.yaml, never the page's state or HTML); a message says only counts and kinds unless the
person asks for the employer and job; one a job a stop, close ones batched; a daily summary; failures said once and
never raised; nothing in practice mode or the live check. Every POST goes to an httpx.MockTransport: nothing leaves."""

import json
import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import yaml
from conftest import browser_available, person_browser, run

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

    def handler(self, request: httpx.Request) -> httpx.Response:
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
        return len(fake.sent)

    assert sends(_settings()) == 2  # (the alert and the test: as a person runs the desk)
    assert sends(_settings(submit_mode="dry_run")) == 0
    assert sends(_settings(submit_mode="dry_run", phone_alerts_in_practice=True)) == 2
    # the live check and the tests: JOB_APPLY_NEVER_SUBMIT=1 (which makes every profile practice mode), whatever the
    # profile says
    monkeypatch.setenv("JOB_APPLY_NEVER_SUBMIT", "1")
    assert sends(_settings(phone_alerts_in_practice=True)) == 0
    assert "never sent in a test run" in (phone.held(_settings(phone_alerts_in_practice=True)) or "")


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
                made = (await c.post("/api/phone/make", headers=h, json={})).json()
                topic = made["topic"]
                assert made["server"] == "https://ntfy.sh"
                assert topic.startswith("jobdesk-") and len(topic) == len("jobdesk-") + 24
                assert config.NTFY_TOPIC.match(topic)
                assert config.read_secrets(config.secrets_path())[config.NTFY_TOPIC_NAME] == topic
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
                again = (await c.post("/api/phone/make", headers=h, json={})).json()["topic"]
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
                assert "practice mode" in (await c.get("/api/state", headers=h)).json()["phone"]["held"]
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
                page.on("dialog", lambda d: d.accept())
                await page.goto(desk.url)
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
