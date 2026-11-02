"""Phone alerts (the owner's choice, Nov 2): the Job Desk pushes a short message to the person's phone through ntfy
(https://ntfy.sh, a free push service: the person installs its app and subscribes to a topic), so the desk can tell
them something with its page closed.

- Opt-in: nothing is sent until the desk's Phone alerts card makes a topic, a long random name kept as a password
  is (config.NTFY_TOPIC_NAME: in the password vault, or secrets.yaml where there's none). Anyone who knows it can read
  the alerts, so it's never in the page's state, a log or a problem report: the page only learns whether alerts are
  on.
- Private by default: an alert goes through ntfy's servers, so it says only counts and kinds ("A job needs you: a bot
  check"), never an employer, a job's title, a name or anything from the profile; settings.phone_alert_details adds the
  employer and the job.
- When: a job newly needing the person (the page's own desktop alerts' rule: a need of theirs, or ready for their
  Submit), at most once a job a stop, those that come close together in one message, no more than one message a
  BATCH_GAP; cover letters stopping; and a summary of the day at settings.phone_summary_at, while the desk runs.
- A failure (no network, ntfy down, a refusal) never disturbs the desk: it's said once on the desk's page, and what
  waits is tried again with the next alert, or by itself after RETRY_FIRST (doubling to RETRY_MOST); a summary that
  failed is tried again so up to SUMMARY_RETRY after its time.
- Never in practice mode, which sends nothing (submit_mode: dry_run, JOB_APPLY_NEVER_SUBMIT=1: the live check and the
  tests), nor as the live check's test identity, Send a test included. Nor from a test that hasn't a transport of its
  own (PYTEST_CURRENT_TEST): a developer's own topic in their environment never gets a test's alerts.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Callable

import httpx

from . import config

DEFAULT_SERVER = "https://ntfy.sh"
TITLE = "Job Desk"
TICK = 5.0  # how often the desk looks for something to send (seconds)
GATHER = 10.0  # a new alert waits this long for others that come with it, to go in one message
BATCH_GAP = 60.0  # and messages go this far apart at least: what comes meanwhile goes in the next one
SUMMARY_LATE = 60 * 60  # the daily summary still goes this long after its time (a computer asleep at it), not later
SUMMARY_RETRY = 3 * 60 * 60  # and one that failed is tried again up to this long after its time
# A message that failed is tried again by itself this long after (or with the next alert, sooner), each failure in a row
# doubling the wait, up to RETRY_MOST
RETRY_FIRST = max(5 * BATCH_GAP, 5 * 60)
RETRY_MOST = 30 * 60
# A summary at a time before this hour is the day that just ended's (00:00: since the midnight before)
SUMMARY_NIGHT_HOUR = 4
TIMEOUT = 10.0  # seconds a send may take
MESSAGE_MOST = 3500  # characters (ntfy takes 4096 bytes)

# a stop's kind, as a message says it (one, and several)
KINDS = {
    "bot_check": ("a bot check", "bot checks"), "captcha": ("a CAPTCHA", "CAPTCHAs"),
    "questions": ("questions to answer", "question cards"),
    "sign_in": ("a sign-in", "sign-ins"), "email_code": ("an emailed code to type", "emailed codes to type"),
    "stuck": ("a stuck page", "stuck pages"), "submit_failed": ("a Submit that didn't go", "Submits that didn't go"),
    "check_submit": ("a Submit to check", "Submits to check"),
    "cover_letter": ("a first cover letter to check", "first cover letters to check"),
    "maybe_duplicate": ("a job that may be one already begun", "jobs that may be ones already begun"),
    "restarted": ("a job begun before a restart", "jobs begun before a restart"),
}
OTHER_KIND = ("a stop", "stops")
# the needs that are the person's Submit (an application at the step they send): said as ready, not as a need
READY_NEEDS = {"your_submit"}
# needs_you that aren't the person's to act on: a tailored resume (Claude writes it)
NOT_THEIRS = {"tailor"}


def make_topic() -> str:
    """A topic no one can guess: "jobdesk-" and 24 random letters, digits, - and _ (144 bits)."""
    return "jobdesk-" + secrets.token_urlsafe(18)


def topic() -> str | None:
    """The saved topic, if it's one ntfy takes (never said anywhere)."""
    saved = config.get_secret(config.NTFY_TOPIC_NAME)
    return saved.strip() if saved and config.NTFY_TOPIC.match(saved.strip()) else None


def in_environment() -> bool:
    """Is the topic set in the environment (JOB_APPLY_SECRET_NTFY_TOPIC), which comes before a saved one?"""
    return bool(os.environ.get("JOB_APPLY_SECRET_NTFY_TOPIC"))


def server(settings: config.Settings) -> str:
    return settings.phone_alerts_server or DEFAULT_SERVER


def held(settings: config.Settings) -> str | None:
    """Why no alert goes now, whatever is saved: practice mode sends nothing (CLAUDE.md), the live check's and the
    tests' (JOB_APPLY_NEVER_SUBMIT=1) and the test identity's included."""
    if config.never_submit() or settings.test_identity:
        return ("Phone alerts are held: nothing is sent in a test run or the plugin's live check "
                "(JOB_APPLY_NEVER_SUBMIT=1), a test included.")
    if settings.dry_run:
        return ("Phone alerts are held: practice mode (submit_mode: dry_run in your profile) never sends anything, a "
                "test included.")
    return None


def first_sentence(text: str) -> str:
    """A problem's first sentence, for a message: what stopped, never the rest (which may name a file)."""
    said = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(?:\s|$)", said)
    return (m.group(1) if m else said)[:200]


def letters_line(problem: str) -> str:
    """A cover letters problem as a message's line: its first sentence, said as stopped letters where it doesn't say
    so itself."""
    said = first_sentence(problem)
    return said if said.lower().startswith("cover letter") else f"Cover letters stopped: {said}"


def _many(n: int, kind: tuple[str, str]) -> str:
    return kind[0] if n == 1 else f"{n} {kind[1]}"


class PhoneAlerts:
    """The desk's phone alerts: `tick` looks at the Applier's jobs, and sends what's new (and the daily summary when
    it's time). `tracker`: a function giving the tracker. `transport`: httpx's, for a test."""

    def __init__(self, applier: Any, tracker: Callable[[], Any], saved: Callable[[], None] | None = None):
        self.applier = applier
        self.tracker = tracker
        self.saved = saved  # (called when summary_day changes: the desk keeps it in desk.json)
        self.transport: httpx.AsyncBaseTransport | None = None
        self.announced: set[str] | None = None  # the stops already seen (None until the first look)
        self.pending: dict[str, tuple[float, dict[str, Any]]] = {}  # new stops waiting to go: when first seen, what
        self.letters_said: str | None = None  # the cover letters' problem last seen
        self.letters_pending: str | None = None
        self.last_sent = 0.0  # when the last message was tried
        self.waiting_retry = False  # the last one failed: tried again with the next alert, or at retry_at
        self.retry_at = 0.0
        self.retry_wait = RETRY_FIRST  # (doubles with each failure in a row)
        self.summary_failed: str | None = None  # the day whose summary failed, tried again until SUMMARY_RETRY
        self._tick_logged: str | None = None  # (a look's failure, logged once a kind)
        self.problem: str | None = None  # why the last one couldn't be sent, for the desk's page
        self._logged: str | None = None
        self.summary_day: str | None = None  # the day (YYYY-MM-DD) the daily summary last went

    # ------------------------------------------------------------- what's due
    def _desk_reads_code(self, run: Any) -> bool:
        """Is the desk (or Claude) reading this job's emailed code itself? Then it isn't the person's to type yet."""
        a = self.applier
        before = getattr(a, "mail_problem", None)
        try:
            return bool(a._desk_reads_code(run))
        except Exception:
            return False
        finally:
            a.mail_problem = before

    def due(self) -> dict[str, dict[str, Any]]:
        """Each job that needs the person now, or is ready for their Submit, by "job:status:need" (the page's alerts'
        key: a job that goes on and stops again is a new stop): {"ready", "kind", "company", "title"}. Not a job the
        desk tries again by itself after a site's error, nor one waiting on Claude (a tailored resume), nor an emailed
        code the desk is reading itself."""
        out: dict[str, dict[str, Any]] = {}
        for run in list(self.applier.runs.values()):
            if run.status == "ready":
                ready = True
            elif run.status == "needs_you" and not run.retry_at and run.need not in NOT_THEIRS:
                ready = run.need in READY_NEEDS
                if run.need == "email_code" and self._desk_reads_code(run):
                    continue
            else:
                continue
            out[f"{run.job_id}:{run.status}:{run.need or ''}"] = {"ready": ready, "kind": "" if ready else run.need,
                                                                   "company": run.company or "",
                                                                   "title": run.title or ""}
        return out

    # ------------------------------------------------------------- the words
    @staticmethod
    def message(items: list[dict[str, Any]], details: bool, letters: str | None = None) -> str:
        """What a batch says. Only counts and kinds, unless `details` (then each job's employer and title too)."""
        lines: list[str] = []
        needs = [i for i in items if not i["ready"]]
        ready = [i for i in items if i["ready"]]
        if needs:
            kinds: dict[str, int] = {}
            for i in needs:
                kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
            said = ", ".join(_many(n, KINDS.get(k, OTHER_KIND)) for k, n in kinds.items())
            lines.append(f"A job needs you: {said}" if len(needs) == 1 else f"{len(needs)} jobs need you: {said}")
        if ready:
            lines.append("An application is ready for your Submit" if len(ready) == 1
                         else f"{len(ready)} applications are ready for your Submit")
        if details:
            for i in needs + ready:
                what = "ready for your Submit" if i["ready"] else KINDS.get(i["kind"], OTHER_KIND)[0]
                who = ", ".join(x for x in (i["company"], i["title"]) if x) or "A job"
                lines.append(f"- {who}: {what}")
        if letters:  # (letters_line's)
            lines.append(letters)
        return "\n".join(lines)[:MESSAGE_MOST]

    def summary(self, now: float, night: bool = False) -> str:
        """The day so far: "Today: N submitted, M ready for you, K need you, R replies" (sent in and replies from the
        tracker, since this computer's midnight; ready and needing the person from the desk's jobs now). `night`: a
        summary set before SUMMARY_NIGHT_HOUR, the day that just ended's ("Yesterday:", since the midnight before)."""
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        if night:
            midnight -= timedelta(days=1)
        midnight = midnight.astimezone()
        try:
            day = self.tracker().since(midnight)
        except Exception:
            day = {"submitted": 0, "replies": 0}
        due = self.due()
        ready = sum(1 for i in due.values() if i["ready"])
        needs = len(due) - ready
        replies = day["replies"]
        return (f"{'Yesterday' if night else 'Today'}: {day['submitted']} submitted, {ready} ready for you, "
                f"{needs} need you, {replies} {'reply' if replies == 1 else 'replies'}")

    # ------------------------------------------------------------- sending
    async def post(self, text: str, settings: config.Settings, title: str = TITLE) -> str | None:
        """`text` to the phone. None when it went; else why not, in words that never hold the topic. Never raises."""
        name = topic()
        if name is None:
            return "Phone alerts are off (no topic saved)."
        why = held(settings)
        if why:
            return why
        if self.transport is None and os.environ.get("PYTEST_CURRENT_TEST"):
            return "Phone alerts aren't sent from a test without a transport of its own."
        url = f"{server(settings)}/{name}"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport) as client:
                r = await client.post(url, content=text.encode("utf-8"),
                                      headers={"Title": title, "Content-Type": "text/plain; charset=utf-8"})
            if r.status_code >= 400:
                return f"the alert service answered {r.status_code}" + (" (too many alerts: ntfy limits them)"
                                                                        if r.status_code == 429 else "")
            return None
        except Exception as e:  # (no network, a timeout, a bad server address: said, never raised)
            said = " ".join(str(e).replace(url, "<its address>").replace(name, "<topic>").split())
            return f"{type(e).__name__}" + (f": {said[:200]}" if said else "")

    async def _send(self, text: str, settings: config.Settings, now: float) -> bool:
        """Sent, with a failure noted once (and logged once a kind, without the topic), and tried again at the next
        alert or at retry_at, whichever comes first."""
        self.last_sent = now
        why = await self.post(text, settings)
        if why is None:
            self.problem, self._logged, self.waiting_retry = None, None, False
            self.retry_wait = RETRY_FIRST
            return True
        self.problem = f"Phone alerts couldn't be sent: {why}. The desk tries again with the next alert."
        self.waiting_retry = True
        self.retry_at = now + self.retry_wait
        self.retry_wait = min(self.retry_wait * 2, RETRY_MOST)
        if self._logged != why:
            self._logged = why
            logging.getLogger(__name__).warning("phone alert not sent: %s", why)
        return False

    async def test(self, settings: config.Settings) -> str | None:
        """The card's Send a test, at once (no wait for the others): None when it went, else why not."""
        why = await self.post("Test from the Job Desk: phone alerts work. You'll get one like this when a job needs "
                              "you or is ready for your Submit.", settings)
        if why is None:
            self.problem, self._logged = None, None
        return why

    # ------------------------------------------------------------- the desk's look, every TICK seconds
    async def tick(self, settings: config.Settings | None = None, now: float | None = None) -> None:
        """Look at the jobs and send what's due. `settings`: the profile's (read here when not given). Never raises."""
        now = time.time() if now is None else now
        try:
            await self._tick(settings, now)
        except Exception as e:  # (every TICK seconds: logged once a kind, not a traceback every 5 s)
            said = f"{type(e).__name__}: {e}"
            if said != self._tick_logged:
                self._tick_logged = said
                logging.getLogger(__name__).warning("phone alerts: a look failed", exc_info=True)

    async def _tick(self, settings: config.Settings | None, now: float) -> None:
        due = self.due()
        first = self.announced is None  # (what waits as the desk starts was there before: not news)
        new = [] if self.announced is None else [k for k in due if k not in self.announced]
        self.announced = set(due)
        for k in new:
            self.pending.setdefault(k, (now, due[k]))
        self.pending = {k: v for k, v in self.pending.items() if k in due}  # (seen to meanwhile: nothing to say)
        problem = self.applier.letters_problem if self.applier.letters else None
        said = letters_line(problem) if problem else None
        letters_new = said is not None and said != self.letters_said and not first
        self.letters_said = said
        if letters_new:
            self.letters_pending = said
        elif said is None:
            self.letters_pending = None
        if new or letters_new or self.waiting_retry and now >= self.retry_at:
            self.waiting_retry = False  # (a failed one is tried again with the next alert, or after its wait)
        if settings is None:
            try:
                settings = config.Profile.load().settings
            except Exception:
                return  # (a profile that doesn't read: said on the page; nothing goes meanwhile)
        summary = self._summary_due(settings, now)
        if not (self.pending or self.letters_pending or summary):
            return
        if topic() is None or held(settings):  # (off: nothing piles up to go when it's turned on)
            self.pending, self.letters_pending = {}, None
            return
        if summary:
            today = datetime.fromtimestamp(now).date().isoformat()
            if await self._send(self.summary(now, int(settings.phone_summary_at[:2]) < SUMMARY_NIGHT_HOUR), settings,
                                now):
                self.summary_day, self.summary_failed = today, None  # (once a day)
                if self.saved:
                    self.saved()
            else:
                self.summary_failed = today
            return
        if self.waiting_retry or not (self.pending or self.letters_pending):
            return
        oldest = min((at for at, _ in self.pending.values()), default=now - GATHER)
        if now - oldest < GATHER or now - self.last_sent < BATCH_GAP:
            return
        items = [item for _, item in sorted(self.pending.values(), key=lambda v: v[0])]
        if await self._send(self.message(items, settings.phone_alert_details, self.letters_pending), settings, now):
            self.pending, self.letters_pending = {}, None

    def _summary_due(self, settings: config.Settings, now: float) -> bool:
        """Is the daily summary due now? At its time, or up to SUMMARY_LATE after it, once a day; one that failed, again
        after the retry's wait, up to SUMMARY_RETRY after its time."""
        if not settings.phone_summary_at:
            return False
        hour, minute = (int(x) for x in settings.phone_summary_at.split(":"))
        here = datetime.fromtimestamp(now)
        today = here.date().isoformat()
        late = here - here.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if today == self.summary_day or late < timedelta(0):
            return False
        if self.summary_failed == today:
            return late < timedelta(seconds=SUMMARY_RETRY) and now >= self.retry_at
        return late < timedelta(seconds=SUMMARY_LATE)

    def public(self, settings: config.Settings, on: bool) -> dict[str, Any]:
        """For the desk's state: whether alerts are on, never the topic."""
        return {"on": on, "in_environment": in_environment(), "details": settings.phone_alert_details,
                "summary_at": settings.phone_summary_at, "custom_server": bool(settings.phone_alerts_server),
                "held": held(settings) if on else None, "problem": self.problem if on else None}
