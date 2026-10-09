"""One-button applying: carry each queued job from its posting to the review page.

The pipeline drives the same tools Claude uses (open_application, autofill, click,
add_entries, submit_application), so every guard still holds: final submit buttons are
only pressed through submit_application, LinkedIn and Indeed are left for the person to
submit, and nothing is answered that the profile or the person's saved answers don't
cover. Each job gets its own browser tab.

Whatever needs the person pauses that job with a plain reason. Questions wait in the
Job Desk while other jobs carry on. A bot check, a sign-in or an emailed code needs
their hands in the browser window, so the queue holds there (that tab is brought to the
front) and carries on by itself once the page moves past it, or when they choose
"later".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config, mailbox
from .ats import ATS_NAMES, detect_ats, shared_system
from .autofill import clean_label, is_empty_value, norm, plan_autofill, tailored_document
from .browser import TabClosed, final_text

NEW_TAB_WAIT = 4  # seconds to wait for a tab opened late by a click before calling it a stall
ONCE_SETTLE = 1.0  # seconds after filling the person's answers before checking they stayed in
MAX_STEPS = 15
LATE_BUTTONS_WAIT = 10  # seconds for a page's buttons to be drawn
BLANK_PAGE_WAIT = 30  # for a page with nothing on it yet to draw (Infineon's application, now and then)
SIGN_IN_STEP_WAIT = 25  # Workday's sign-in step can take longer to draw its buttons (Applied's)
ACCOUNT_DRAW_WAIT = 4  # for the application below a Create Account form to be drawn (Qorvo's)
HANDS_ON = {"bot_check", "sign_in", "email_code"}
# A pause that needs the person holds the queue while they're at it. Nothing done in its tab
# for HANDS_ON_IDLE (no one's at the browser) and the other jobs go ahead; it's watched still,
# and carried on with once its tab is past the pause.
HANDS_ON_IDLE = 5 * 60
HANDS_ON_TIMEOUT = 45 * 60  # the longest it holds the queue, even for someone at work in the tab
POLL_SECONDS = 3.0
# With an email app password saved, a job waiting on an emailed code or link has the inbox
# looked at this often, for this long after it began waiting
MAIL_POLL_SECONDS = 20
MAIL_WINDOW = 15 * 60
SHARED_LOOK_BACK = 30  # seconds looked back for a job's code while an earlier job waits on the same sender
FINISHED = {"applied", "interviewing", "offer", "rejected", "withdrawn"}  # tracker statuses never applied to again

_BOT_TITLE = re.compile(r"just a moment|attention required|access denied|pardon our interruption|security check|"
                        r"are you a robot|bot (?:check|detection)", re.I)
# A bare "403 Forbidden" (Valleywise Health's postings, to the desk's browser): the site turns the
# browser away. There's nothing to solve, so it holds nothing up: the person applies elsewhere
_TURNED_AWAY = re.compile(r"^\s*(?:403\s*)?forbidden\s*$", re.I)
_BOT_TEXT = re.compile(r"verify (?:that )?you are (?:a )?human|are you a robot|checking (?:if the site connection is secure|"
                       r"your browser)|press (?:&|and) hold|complete the security check|unusual traffic from your|"
                       r"enable javascript and cookies to continue|request unsuccessful|you have been blocked", re.I)
_CAPTCHA = re.compile(r"i'?m not a robot|i am human|hcaptcha|recaptcha challenge", re.I)
_VERIFY_EMAIL = re.compile(r"verif(?:y|ication)\b.{0,40}\b(?:e-?mail|account|link)|check your (?:e-?mail|inbox)", re.I)
_CODE_FIELD = re.compile(r"verification code|one[- ]time (?:pass)?code|passcode|security code|\bcode\b.{0,40}"
                         r"(?:sent|email)|enter (?:the )?(?:\d-digit )?code|\botp\b", re.I)
_SIGN_IN_ACTION = re.compile(r"^(sign in|log ?in|sign in with email)$", re.I)
# The button pressed once the emailed code is in; one labelled Submit is left to the person
_AFTER_CODE = re.compile(r"^(verify|confirm|continue|next)( (code|e-?mail|account|my e-?mail))?$", re.I)
_TRY_LATER = re.compile(r"\btoo many\b.{0,30}\b(?:attempts|requests|tries)\b|\btry again (?:later|in \d+)|\brate[- ]limit",
                        re.I)
_CREATE_ACCOUNT = re.compile(r"^(?:proceed to |continue to )?(create (?:an |your |a new )?account|sign up|register)"
                             r"(?: now)?[.!]?$", re.I)
# A page about making an account ("Create an account", amazon.jobs after an email it doesn't know)
_ACCOUNT_PAGE = re.compile(r"\b(create (?:an |your |a new )?account|sign up|register)\b", re.I)
_ACCOUNT_KINDS = {"text", "email", "tel", "select", "combobox", "listbox"}  # not check boxes or files
_SOCIAL = re.compile(r"\b(google|apple|linked ?in|facebook|microsoft|indeed|seek)\b", re.I)
_STEP = re.compile(r"^(save (?:and|&) continue|continue|next|next step|review|review (?:and|&) submit|"
                   r"review application|proceed|go to next step|start)$", re.I)
_FORWARD = re.compile(r"^(save (?:and|&) continue|continue|next|next step|review|review application|proceed|"
                      r"go to next step)$", re.I)
_SIGN_IN_STEP = re.compile(r"create account\s*/\s*sign in|sign in\s*/\s*create account", re.I)  # Workday's step name
_ENTRY = re.compile(r"^(apply manually|apply now|apply online|apply|easy apply|quick apply|"
                    r"apply for (?:this|the) (?:job|position|role)(?: online)?|"
                    r"apply to (?:this )?job|start (?:your |my )?application|i'?m interested|"
                    r"continue to application|apply on (?:the )?(?:company|employer)(?:'s)? (?:site|website))$", re.I)
_AVOID = re.compile(r"autofill|with resume|resume parse|sign ?in|log ?in|create account|register|upload|back|"
                    r"previous|cancel|save for later|withdraw|delete|remove|search|share|print|email (?:me|this)", re.I)
# A button that agrees to something ("I Acknowledge the Privacy Notice", Schwab's iCIMS sign-in):
# never pressed for the person
_AGREEMENT = re.compile(r"^(?:i )?(?:acknowledge|agree|accept|consent)\b.*\b(?:notice|terms|policy|privacy|statement|"
                        r"agreement|conditions)\b|^i (?:acknowledge|agree|accept|consent)\b", re.I)
_EXPERIENCE_PAGE = re.compile(r"my experience|work experience|employment history", re.I)
# A note laid over the page (Nikon's UKG board: "Accessibility Note") with nothing else to press.
_DISMISS_NOTE = re.compile(r"^(dismiss(?: (?:note|notice|message))?|close (?:note|notice|message))$", re.I)
# Cookie banners: only ever the privacy-preserving choice, and only when the site offers one.
_DECLINE_COOKIES = re.compile(r"^(reject(?: all)?(?: cookies)?|decline(?: all)?(?: cookies)?|only (?:strictly )?necessary"
                              r"|necessary (?:cookies )?only|use necessary cookies only|accept (?:only )?necessary"
                              r"(?: cookies)?|reject optional(?: cookies)?|(?:reject|decline) non-?essential(?: cookies)?)$",
                              re.I)


@dataclass
class Run:
    job_id: int
    title: str = ""
    company: str = ""
    status: str = "queued"  # queued | running | needs_you | ready | submitted | failed | skipped
    # questions | sign_in | bot_check | email_code | captcha | your_submit | stuck
    # | submit_failed (pressed, the form is still there) | check_submit (pressed, no confirmation)
    # | tailor (waiting for Claude to write a resume for this job)
    need: str = ""
    reason: str = ""
    questions: list[dict[str, Any]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    url: str = ""
    blocking: bool = False  # the queue waits on this one
    paused_at: float = 0.0
    paused_site: str = ""  # the site it paused on, so a tab taken to webmail isn't "moved on"
    paused_host: str = ""  # and the exact address host (one employer's Workday, not any)
    hold_host: str = ""  # an account site paused on: nothing there is past the pause (amazon.jobs' passport)
    submit: bool = False  # submit once the review page is reached
    usual_resume: bool = False  # the person chose to go ahead without a tailored resume
    once: dict[str, Any] = field(default_factory=dict)  # answers for this application only, by question
    seen_form: bool = False  # got into the application itself (so a page with only Submit is its review page)
    try_later: bool = False  # left on a "Try Again Later" page: only the person's Resume goes on from it
    active_at: float = 0.0  # when its paused tab last changed: someone at work in it
    tab_mark: int = 0  # what its paused tab looked like then (address and box values)
    left: bool = False  # paused for the person, and the queue went on without it
    moved_since: float = 0.0  # since when every look has found its paused tab past the pause
    pause_sig: tuple | None = None  # how the paused page looked: the same page is never past itself
    once_page: dict[str, tuple] = field(default_factory=dict)  # where each this-application answer went in
    mail_checked: float = 0.0  # when the inbox was last looked at for its emailed code or link
    mail_done: bool = False  # the code or link from the inbox went in: no more looking
    # Submit was pressed for this job before and no confirmation showed (its folder keeps the
    # record): it may have gone, so it's never pressed again without the person
    pressed_before: bool = False
    page_info: dict[str, Any] = field(default_factory=dict)  # what the page looked like when it paused
    page: Any = None  # its browser tab
    updated: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k not in ("page", "tab_mark")}


def classify(data: dict[str, Any], text: str) -> str:
    """bot_check, sign_in, email_code, form or page."""
    if data.get("challenge"):  # a CAPTCHA's pictures over the page (iCIMS after its email step)
        return "bot_check"
    fields = [f for f in data.get("fields", []) if not f.get("disabled")]
    if not fields and (_BOT_TITLE.search(data.get("title") or "") or _BOT_TEXT.search(text[:3000])
                       or data.get("captcha")):  # (a page with only a CAPTCHA's box on it, and its button)
        return "bot_check"
    if any(f["kind"] == "password" for f in fields):
        return "sign_in"
    actions = [a.get("text", "") for a in data.get("actions", [])]
    if not fields and any(re.match(r"^sign in with ", a, re.I) for a in actions):
        return "sign_in"  # Workday: "Sign in with email / Google / Apple"
    if any(_CODE_FIELD.search(f.get("label") or "") for f in fields if f["kind"] in ("text", "number")):
        return "email_code"
    if not fields and _VERIFY_EMAIL.search(text[:3000]) and not any(_ENTRY.match(final_text(a)) for a in actions):
        return "email_code"  # "we sent you a link to verify your account"
    return "form" if fields else "page"


def pick_next(actions: list[dict[str, Any]], in_form: bool) -> dict[str, Any] | None:
    """The button that moves the application on: a step button inside a form, otherwise
    the way into it ("Apply Manually" before "Apply")."""
    usable = [a for a in actions if not a.get("disabled") and not a.get("is_submit") and not a.get("same_page")]
    steps = [a for a in usable if _STEP.match(final_text(a["text"]))]
    entries = [a for a in usable if _ENTRY.match(final_text(a["text"])) and not _SOCIAL.search(a["text"])
               and not (_AVOID.search(a["text"]) and "manually" not in a["text"].lower())]
    # an open menu's own entry ("Apply Now" under Qorvo's "Apply now ▾") before the toggle again
    entries.sort(key=lambda a: (not a.get("menu"), "manually" not in a["text"].lower()))
    order = (steps + entries) if in_form else (entries + steps)
    return order[0] if order else None


def _fingerprint(page: dict[str, Any]) -> tuple:
    fields = page.get("fields")
    count = fields if isinstance(fields, int) else len(fields or [])
    actions = tuple(a if isinstance(a, str) else a.get("text", "") for a in page.get("actions") or [])
    return page.get("url"), tuple(page.get("headings") or []), count, actions


_TAILOR_SAYS = ("Waiting for a resume written for this job. In Claude Code, say \u201ctailor my resumes\u201d; the "
                "desk carries on with this job as soon as its resume is ready. Or use your usual resume.")
_BOT_CHECK_SAYS = ("The site is checking that you're a person (a bot check or CAPTCHA). Solve it in the browser "
                   "window; the desk carries on by itself after that.")
_ERROR_COUNT = re.compile(r"^\d+ errors? found\.?$", re.I)  # a section's count, not what's wrong
_ROBOT = re.compile(r"not a robot|captcha|verify (?:that )?you(?:'re| are) (?:a )?human", re.I)
_ERRORISH = re.compile(r"error|required|invalid|please|must|enter |select |missing|problem|fix|can'?t be blank", re.I)


def _flagged(data: dict[str, Any]) -> list[str]:
    """What a page marks as wrong: its error messages, Workday's "Error-Email" links in its
    "Errors Found" box, and fields marked invalid."""
    out = [e for e in data.get("errors") or [] if _ERRORISH.search(e)]
    for a in data.get("actions") or []:
        m = re.match(r"^error\s*-\s*(.+)$", (a if isinstance(a, str) else a.get("text", "")).strip(), re.I)
        if m:
            out.append(f"\u201c{clean_label(m.group(1))}\u201d needs fixing")
    fields = data.get("fields")  # a click's summary carries only how many there are
    out += [f"\u201c{clean_label(f.get('label') or '')}\u201d is marked invalid"
            for f in (fields if isinstance(fields, list) else []) if isinstance(f, dict) and f.get("invalid") and f.get("label")]
    return list(dict.fromkeys(out))


def _page_info(data: dict[str, Any]) -> dict[str, Any]:
    """Enough to see why a job paused, without any of the values typed into the page."""
    return {
        "url": data.get("url"), "title": data.get("title"), "headings": (data.get("headings") or [])[:8],
        "actions": [a.get("text", "") + (" (disabled)" if a.get("disabled") else "") for a in data.get("actions") or []][:30],
        "fields": [{**{k: f.get(k) for k in ("label", "kind", "required") if f.get(k) is not None},
                    "empty": is_empty_value(f.get("value"))} for f in data.get("fields") or []][:40],
        "errors": (data.get("errors") or [])[:5],
    }


class Applier:
    """Works through queued jobs one at a time in the background."""

    def __init__(self, srv: Any):
        self.srv = srv  # the server module: its tools, tracker() and browser
        self.runs: dict[int, Run] = {}
        self.tasks: deque[tuple[str, int]] = deque()
        self.auto_submit = False
        self.tailor = False  # hold each job until Claude has written a resume for it
        self.current: int | None = None
        self._wake = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self.mail_problem: str | None = None  # why the inbox couldn't be read, for the desk page
        self._mail_refused: str | None = None  # the app password the mail service turned down

    # ------------------------------------------------------------- control
    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.get_running_loop().create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

    def enqueue(self, job_id: int, submit: bool = False, front: bool = False) -> Run:
        job = self.srv.tracker().get(job_id)
        if job is None:
            raise KeyError(f"No job with id {job_id}")
        run = self.runs.get(job_id)
        if job.get("status") in FINISHED or run is not None and run.status == "submitted":
            # applying again could send a second application ("Submit for me")
            raise ValueError(f"{job.get('title') or 'That job'} is already marked {job.get('status') or 'submitted'}")
        run = run or Run(job_id, job.get("title", ""), job.get("company", ""))
        self.runs[job_id] = run
        if run.status == "running" and self.current == job_id:
            return run
        run.status, run.need, run.reason, run.blocking, run.questions = "queued", "", "", False, []
        run.left = False
        # a Submit pressed earlier with no confirmation (in this run of the desk or before it was
        # restarted) may have sent the application: Submit for me doesn't press it again
        run.pressed_before = run.pressed_before or _pressed_before(job) or any(
            str(e.get("note") or "").startswith("pressed Submit;") for e in self.srv.tracker().events(job_id))
        run.submit = submit and not run.pressed_before
        run.updated = time.time()
        self._cancel(job_id)
        (self.tasks.appendleft if front else self.tasks.append)(("apply", job_id))
        self._wake.set()
        return run

    def submit_now(self, job_id: int) -> Run:
        """The person pressed Submit for a job that's waiting on its review page."""
        run = self.runs[job_id]
        if run.status not in ("ready", "needs_you"):
            raise ValueError(f"Job {job_id} isn't waiting on a review page")
        run.status, run.reason = "queued", "Submitting…"
        self.tasks.appendleft(("submit", job_id))
        self._wake.set()
        return run

    def wake(self) -> None:
        """Look again now (a tailored resume was just written)."""
        self._wake.set()

    def set_tailor(self, on: bool) -> None:
        self.tailor = on
        if not on:  # nothing to wait for any more
            for run in [r for r in self.runs.values() if r.status == "needs_you" and r.need == "tailor"]:
                with contextlib.suppress(KeyError, ValueError):
                    self.enqueue(run.job_id, submit=run.submit)

    def use_usual_resume(self, job_id: int) -> Run:
        """Go ahead with this job without a tailored resume."""
        run = self.runs[job_id]
        run.usual_resume = True
        return self.enqueue(job_id, submit=run.submit, front=True)

    def tailoring(self) -> list[Run]:
        """The jobs waiting for a resume written for them."""
        return [r for r in self.runs.values() if r.status == "needs_you" and r.need == "tailor"]

    def _resume_tailored(self) -> None:
        """Carry on with each waiting job whose tailored resume is now in its folder."""
        for run in self.tailoring():
            job = self.srv.tracker().get(run.job_id, with_description=False)
            if job and tailored_ready(job):
                with contextlib.suppress(KeyError, ValueError):
                    self.enqueue(run.job_id, submit=run.submit)

    def later(self, job_id: int) -> Run:
        """Stop holding the queue for this job. It stays paused until resumed, or until its tab
        is past the pause (then the desk carries on with it between jobs)."""
        run = self.runs[job_id]
        run.left = run.left or run.blocking
        run.blocking = False
        self._wake.set()
        return run

    def mark_applied(self, job_id: int) -> None:
        """The person (or Claude) says this job is applied to: it leaves the queue, and any
        hold on the queue for it ends. The tracker already says so."""
        self._cancel(job_id)
        run = self.runs.get(job_id)
        if run is not None:
            run.status, run.need, run.blocking, run.reason = "submitted", "", False, "Marked as applied."
            run.left = False
            self._wake.set()

    async def skip(self, job_id: int) -> Run:
        if self.srv.tracker().get(job_id, with_description=False) is None:
            raise KeyError(f"No job with id {job_id}")  # a stale page: no entry is made for it
        run = self.runs.get(job_id) or Run(job_id)
        self.runs[job_id] = run
        self._cancel(job_id)
        run.status, run.need, run.blocking, run.reason = "skipped", "", False, "Skipped"
        self.srv.tracker().update(job_id, status="skipped", note="skipped in the Job Desk")
        for tab in self.srv.browser.lineage(run.page):  # its application tab, and the tab that opened it
            if not tab.is_closed():
                try:
                    await tab.close()
                except Exception:
                    pass
        run.page = None
        return run

    async def focus(self, job_id: int) -> bool:
        run = self.runs.get(job_id)
        if run is None or run.page is None or run.page.is_closed():
            return False
        await run.page.bring_to_front()
        return True

    def _cancel(self, job_id: int) -> None:
        self.tasks = deque(t for t in self.tasks if t[1] != job_id)

    def _go_on_without(self, run: Run, why: str) -> None:
        """Stop holding the queue for a job that waits on the person; it's still watched."""
        run.blocking, run.left = False, True
        run.reason += " (" + why + ": finish it in its tab and the desk carries on with it, or press Resume)"
        self._log(run, why)
        self._wake.set()

    async def _pick_up_left(self) -> None:
        """Between jobs: carry on with any the queue went on without whose tab the person has
        since got past its sign-in, check or code. A closed tab waits for Resume."""
        for run in [r for r in self.runs.values() if r.left and r.status == "needs_you" and not r.blocking]:
            if run.page is None or run.page.is_closed():
                continue
            if run.need == "email_code":
                await self._check_mail_safely(run)  # the code or link came after the queue went on
            try:
                moved = await self._past_pause(run)
            except Exception:  # a tab mid-way through loading: looked at again next time, not holding the rest
                continue
            if moved and run.left and run.status == "needs_you":
                try:
                    self.enqueue(run.job_id, submit=run.submit, front=True)
                except (KeyError, ValueError):  # gone, or marked applied meanwhile: nothing to watch for
                    run.left = False

    async def _past_pause(self, run: Run) -> bool:
        """Is a job's tab, left waiting for the person, past its sign-in, check or code? Read
        without taking over the tools' tab (Claude may be using it), and only while it's on the
        address it paused on: a tab the person has taken to another posting, even one on the
        same job system, is no application of this job's."""
        data, text = await self.srv.browser.peek(run.page)
        return self._twice(run, self._looks_past(run, data, text))

    # ------------------------------------------------------------- worker
    async def _worker(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # the worker must outlive any one job's surprise
                await asyncio.sleep(POLL_SECONDS)

    async def _tick(self) -> None:
        self._resume_tailored()
        # A Submit the person pressed goes first, even while the queue holds for a sign-in.
        submit = next((t for t in self.tasks if t[0] == "submit"), None)
        blocker = next((r for r in self.runs.values() if r.blocking), None)
        if blocker is not None and submit is None:
            if blocker.need == "email_code":
                await self._check_mail_safely(blocker)
            try:
                moved = await self._strict(self._moved_on(blocker))
            except Exception:  # a tab that can't be read just now (mid-load, crashed): not past it, and the
                moved = False  # idle and long-wait releases below still apply
            if time.time() - blocker.paused_at > HANDS_ON_TIMEOUT:
                self._go_on_without(blocker, "the other jobs went ahead after a long wait")
            elif moved:
                blocker.blocking = False
                if blocker.status != "needs_you":
                    # skipped (or resumed) while its tab was being looked at: a tab closed by
                    # Skip reads as moved on, and the job would start again by itself
                    return
                try:
                    self.enqueue(blocker.job_id, submit=blocker.submit, front=True)
                except ValueError:  # marked applied meanwhile
                    pass
            elif time.time() - blocker.active_at > HANDS_ON_IDLE:
                self._go_on_without(blocker, f"nothing happened in its tab for {HANDS_ON_IDLE // 60} minutes, "
                                    "so the other jobs went ahead")
            else:
                self._wake.clear()  # sleep the poll out, unless something new comes in
                await self._sleep(POLL_SECONDS)
                return
        await self._pick_up_left()
        if not self.tasks:
            self._wake.clear()
            # with a job left waiting on the person, look at its tab again soon
            await self._sleep(POLL_SECONDS if any(r.left and r.status == "needs_you" for r in self.runs.values()) else 30)
            return
        task = submit or self.tasks[0]
        self.tasks.remove(task)
        kind, job_id = task
        run = self.runs[job_id]
        held = self._hold_tools(job_id)
        try:
            await self._strict(self._submit(run) if kind == "submit" else self._drive(run))
        except TabClosed:
            if run.status != "skipped":
                run.status, run.need, run.blocking = "failed", "", False
                run.reason = "Its tab was closed. Press Resume to start this application again."
                self._log(run, run.reason)
        except Exception as e:
            if run.status != "skipped":
                run.status, run.need = "failed", ""
                run.reason = f"Something went wrong: {type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"
                self._log(run, run.reason)
        finally:
            self._give_back_tools(held)
            if run.status == "running":  # the desk was stopped part-way
                run.status, run.reason = "failed", "Stopped before it finished. Press Resume to carry on."
            run.updated = time.time()

    def _hold_tools(self, job_id: int) -> tuple[Any, Any, Any]:
        """Take the browser tools for one of this job's steps: Claude's calls that act in the
        browser wait meanwhile (server._desk_driving). Returns what to give back."""
        browser = self.srv.browser
        held = (self.current, browser.current_tab, browser.current_job_id)
        self.current = job_id
        return held

    def _give_back_tools(self, held: tuple[Any, Any, Any]) -> None:
        """Hand the tools back on the tab, and with the job, they had before: Claude may be at
        work in a tab of its own, and its next fill or click mustn't land in this job's tab."""
        self.current, tab, job_id = held
        browser = self.srv.browser
        if tab is not None and browser.use_tab(tab):
            browser.current_job_id = job_id

    async def _strict(self, step: Any) -> Any:
        """Run a job's step with the browser held to that job's tab (and tabs it opens)."""
        browser = self.srv.browser
        browser.strict_tabs = True
        try:
            return await step
        finally:
            browser.strict_tabs = False

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _moved_on(self, run: Run) -> bool:
        """Has the person got the paused tab past its sign-in, check or code? Only on the
        same site, or on into an application system: a tab they've taken to their webmail
        or a sign-in provider isn't the application moving on."""
        if run.page is None or run.page.is_closed():
            return True  # they closed it: start the job again
        # read without taking over the tools' tab: Claude may be using them meanwhile
        data, text = await self.srv.browser.peek(run.page)
        # the address and what's in the boxes: a change is the person at work in the tab (a bot
        # check that reloads itself with a new token in its address isn't)
        mark = hash((_bare(data.get("url") or ""), tuple((f.get("id"), str(f.get("value"))) for f in data.get("fields") or [])))
        if run.tab_mark and mark != run.tab_mark:
            run.active_at = time.time()
        run.tab_mark = mark
        return self._twice(run, self._looks_past(run, data, text))

    def _looks_past(self, run: Run, data: dict[str, Any], text: str) -> bool:
        """Does this look at a job's paused tab show it past the pause? Settled, not the page it
        paused on, not that kind of page any more, and still this job's (see _own_place)."""
        if data.get("loading") or (run.pause_sig is not None and _page_sig(data) == run.pause_sig):
            return False
        if run.hold_host and (urlparse(data.get("url") or "").hostname or "").lower() == run.hold_host.lower():
            return False  # still on the account site the person is making their account on
        return classify(data, text) != run.need and self._own_place(run, data.get("url") or "")

    def _own_place(self, run: Run, url: str, own: set[str] | None = None) -> bool:
        """Is this address still this job's? The host it paused on or one of the job's own (its
        posting's, its application's), or on from the employer's own site into a job system.
        Not another tenant of a job system many employers share (acme.wd1.myworkdayjobs.com vs
        other.wd5.myworkdayjobs.com), nor another posting elsewhere: driving another
        application as this job could submit it under this job's name. `own`: the job's hosts,
        looked up beforehand (off the event loop's thread the tracker can't be read)."""
        host = (urlparse(url).hostname or "").lower()
        own = self._own_hosts(run) if own is None else own
        if host == run.paused_host or host and host in own:  # (a page saved on this computer has no host)
            return True
        if not run.paused_host:
            return False  # never paused, so nowhere it went on from: only its own addresses
        # paused on the employer's own address, and on from there into a job system many employers
        # share: not another employer's own careers site, which isn't one
        paused_on_own_site = shared_system(f"https://{run.paused_host}/") is None
        return paused_on_own_site and shared_system(url) not in (None, "linkedin", "indeed")

    def _own_hosts(self, run: Run) -> set[str]:
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        return {(urlparse(u).hostname or "").lower() for u in (run.url, job.get("url"), job.get("apply_url")) if u}

    @staticmethod
    def _twice(run: Run, moved: bool) -> bool:
        """Past the pause on looks at least a poll apart, with none between them saying
        otherwise: a page caught mid-way between two states (a moment with nothing drawn, a
        spinner) isn't the person having got past it."""
        if not moved:
            run.moved_since = 0.0
            return False
        if not run.moved_since:
            run.moved_since = time.time()
            return False
        return time.time() - run.moved_since >= POLL_SECONDS

    # ------------------------------------------------------------- the inbox
    def mail_login(self) -> tuple[str, str] | None:
        """The profile's email address and the app password saved for it, unless that
        password was turned down (a new one is tried)."""
        password = _secret("email_password")
        try:
            address = str(config.Profile.load().get("personal.email") or "").strip()
        except Exception as e:  # a typo in profile.yaml: said, and the queue carries on without the inbox
            self.mail_problem = f"The desk can't watch your inbox until profile.yaml reads again: {e}"
            return None
        if not password or "@" not in address or password == self._mail_refused:
            return None
        if mailbox.imap_host(address) is None:  # watching an inbox it can't read would only mislead
            self.mail_problem = (f"The desk can't read mail for {address.rsplit('@', 1)[1]} addresses, so it can't "
                                 "watch your inbox for sign-up codes.")
            return None
        return address, password

    def _mail_senders(self, run: Run) -> set[str]:
        """Who may have sent this job's code or link: the job site it's waiting on, the
        employer's own site, and the job system they run on."""
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        urls = [u for u in (run.url, job.get("url"), job.get("apply_url")) if u]
        allowed = {mailbox.site_domain(urlparse(u).hostname or "") for u in urls}
        for u in urls:
            allowed |= mailbox.ATS_MAIL_DOMAINS.get(detect_ats(u), set())
        return {d for d in allowed if d}

    async def _check_mail_safely(self, run: Run) -> None:
        """_check_mail, with any surprise in it (a profile.yaml typo, say) kept from stalling the
        queue: the hold's long-wait release and the other jobs still go on."""
        try:
            await self._check_mail(run)
        except Exception as e:
            self.mail_problem = f"The desk couldn't look in your inbox ({type(e).__name__}). It tries again shortly."

    async def _check_mail(self, run: Run) -> None:
        """Look in the inbox for the code or link a waiting job's site emailed, and put it in."""
        now = time.time()
        if run.mail_done or now - run.mail_checked < MAIL_POLL_SECONDS or now - run.paused_at > MAIL_WINDOW:
            return
        login = self.mail_login()
        if login is None or run.page is None or run.page.is_closed():
            return
        run.mail_checked = now
        try:
            data, _ = await self.srv.browser.peek(run.page)
        except Exception:  # a tab mid-way through loading: looked at again next time
            return
        boxes = [f for f in data.get("fields") or [] if f.get("kind") in ("text", "number")
                 and _CODE_FIELD.search(f.get("label") or "")]
        senders = self._mail_senders(run)

        own = {h for h in self._own_hosts(run) | {run.paused_host} if h}
        own_sites = {mailbox.site_domain(h) for h in own if shared_system(f"https://{h}/") is None}

        def own_link(url: str) -> bool:
            """A link back to this job's own site: its hosts, or another address on the employer's
            own site (careers.acme.com from acme.com). Never another tenant of a job system, nor any
            address that only names one in its path (evil.example/myworkdayjobs.com/confirm)."""
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if parsed.scheme not in ("https", "http", "file"):
                return False
            return host == run.paused_host or host in own or bool(host) and mailbox.site_domain(host) in own_sites

        # Another job waiting on mail from the same job system owns what came after it paused (Workday's
        # codes come from one address for every employer): a later one's wait ends this one's mail, and
        # with an earlier one waiting, mail from before this job paused is that one's
        others = [r.paused_at for r in self.runs.values() if r is not run and r.status == "needs_you"
                  and r.need == "email_code" and self._mail_senders(r) & senders]
        later = [t for t in others if t > run.paused_at]
        # (a few seconds back still: the site sends the code as the button is pressed, a moment before
        # the desk notices and pauses)
        look_back = SHARED_LOOK_BACK if any(t <= run.paused_at for t in others) else mailbox.LOOK_BACK
        try:
            found = await asyncio.to_thread(mailbox.search, *login, run.paused_at, senders,
                                            "code" if boxes else "link", own_link, min(later) if later else None,
                                            look_back)
        except mailbox.MailboxError as e:
            if "app password" in str(e):
                self._mail_refused = login[1]  # not tried again until a new one is saved
                self.mail_problem = (f"The desk couldn't read your email: {e}. Save a new email app password "
                                     "in the Site passwords card to try again.")
            else:
                self.mail_problem = f"The desk couldn't read your email: {e}. It tries again shortly."
            return
        except Exception as e:  # a mail service's odd answer: tried again next time, not sinking the queue
            self.mail_problem = f"The desk couldn't read your email ({type(e).__name__}). It tries again shortly."
            return
        self.mail_problem = None
        if found is None or run.status != "needs_you" or run.need != "email_code":
            return
        try:
            if found.kind == "code":
                # not put in (the page was being drawn again, a box turned it down): looked for again.
                # Held to the job's own tab: a closed one mustn't let the code into another job's page.
                # Between jobs Claude may be using the tools: they're held, then given back as they were
                held = self._hold_tools(run.job_id)
                try:
                    run.mail_done = await self._strict(self._enter_code(run, found))
                finally:
                    self._give_back_tools(held)
            else:
                await self.srv.browser.visit(found.value)
                run.mail_done = True
                self._log(run, f"opened the confirmation link from your email (sent from {found.sender})")
                if run.page is not None and not run.page.is_closed():
                    await run.page.reload()
        except Exception as e:  # a slow site or a closed tab: the person finishes it, as without the inbox
            run.mail_done = True
            self._log(run, f"couldn't use the {found.kind} from your email ({type(e).__name__}); "
                      "it's in your inbox for you")

    async def _enter_code(self, run: Run, found: mailbox.Found) -> bool:
        """Type the emailed code into its box (a digit per box where there's one for each) and
        press the button that sends it. False when it didn't go in."""
        srv = self.srv
        if run.page is None or run.page.is_closed() or not self._own_place(run, run.page.url):
            return False  # its tab has gone on to another page: the code isn't typed there
        if not srv.browser.use_tab(run.page):
            return False
        data = await srv.inspect_form(include_dropdown_options=False)
        boxes = [f for f in data.get("fields") or [] if f.get("kind") in ("text", "number")
                 and _CODE_FIELD.search(f.get("label") or "")]
        if not boxes:  # the page moved on meanwhile
            return False
        if 1 < len(boxes) == len(found.value):  # a box per digit (Oracle's "Confirm Your Identity")
            fills = [{"id": box["id"], "value": digit} for box, digit in zip(boxes, found.value)]
        else:
            fills = [{"id": boxes[0]["id"], "value": found.value}]
        out = await srv.fill_form(fills)
        if not out.get("ok"):
            return False
        self._log(run, f"entered the code from your email (sent from {found.sender})")
        press = next((a for a in data.get("actions") or [] if not a.get("disabled")
                      and _AFTER_CODE.match(a.get("text", "").strip())), None)
        try:
            pressed = press is not None and (await srv.click(press["id"])).get("clicked")
        except Exception:  # the button went (the page moved on by itself) or won't take a click
            pressed = False
        if press is not None and pressed:
            self._log(run, f"pressed \u201c{press['text'].strip()}\u201d")
        else:  # (a "Confirm" that sends a form is left to the person, as any final button is)
            run.reason = ("I entered the code from your email. Press the page's button to carry on; "
                          "the desk continues after that.")
        return True

    # ------------------------------------------------------------- one job
    def _log(self, run: Run, text: str) -> None:
        run.log.append(text)
        del run.log[:-40]
        run.updated = time.time()

    def _pause(self, run: Run, need: str, reason: str, questions: list[dict[str, Any]] | None = None,
               seen: dict[str, Any] | None = None) -> None:
        """Wait on the person. `seen`: the page as it was paused on, which never counts as past
        the pause (Workday's sign-in step with no buttons drawn reads as an ordinary page)."""
        if run.status in ("skipped", "submitted"):  # skipped, or marked applied, while it ran
            return
        run.pause_sig = _page_sig(seen) if seen else None
        run.status, run.need, run.reason = "needs_you", need, reason
        run.questions = questions or []
        run.blocking = need in HANDS_ON
        run.paused_at = run.active_at = time.time()
        run.tab_mark, run.left, run.moved_since = 0, False, 0.0
        run.mail_checked, run.mail_done = 0.0, False
        run.paused_site = _site_key(run.url)
        run.paused_host = urlparse(run.url).hostname or ""
        run.hold_host = ""
        self._log(run, reason)

    async def _look(self) -> tuple[dict[str, Any], str]:
        data = await self.srv.inspect_form(include_dropdown_options=False)
        text = await self.srv.page_text(4000)
        data["challenge"] = await self.srv.browser.challenge_showing()
        return data, text

    async def _open(self, run: Run) -> bool:
        srv = self.srv
        # its own tab, while that still shows this job's application. One taken on to another
        # posting meanwhile (by the person, or by Claude's tools) is no longer this job's: filling
        # it would put this job's details into another employer's form, and Submit for me send it
        if (run.page is not None and not run.page.is_closed()
                and not self._own_place(run, run.page.url)):
            self._log(run, "its tab had gone on to another page, so I opened the job again in a new tab")
            run.page = None
        if srv.browser.use_tab(run.page):
            srv.browser.current_job_id = run.job_id
            return True
        run.page = await srv.browser.new_tab()
        opened = await srv.open_application(job_id=run.job_id)
        run.page = srv.browser.current_tab or run.page
        if "error" in opened or opened.get("navigation_error"):
            run.status = "failed"
            run.reason = f"Couldn't open the page: {opened.get('error') or opened.get('navigation_error')}"
            self._log(run, run.reason)
            return False
        self._log(run, f"opened {opened['url']}")
        return True

    def _already_done(self, run: Run) -> bool:
        """Marked applied (or past that) since it was queued: by the person on the desk, by
        Claude, or from a confirmation email. It's never filled or submitted again."""
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        if job.get("status") not in FINISHED:
            return False
        run.status, run.need, run.blocking = "submitted", "", False
        run.reason = f"Already marked {job['status']}, so it isn't applied to again."
        self._log(run, run.reason)
        return True

    async def _drive(self, run: Run) -> None:
        srv = self.srv
        if self._already_done(run):
            return
        run.status, run.need, run.reason = "running", "", "Working on it"
        if self.tailor and not run.usual_resume:
            job = srv.tracker().get(run.job_id, with_description=False) or {}
            if not tailored_ready(job):  # before its tab opens: nothing to keep waiting
                return self._pause(run, "tailor", _TAILOR_SAYS)
        # left on a "Try Again Later" page, on its own tab still: the person has waited and pressed
        # Resume. Any other way back here (the queue carrying on after an emailed code, say) isn't that.
        waited_out = run.try_later and run.page is not None and not run.page.is_closed()
        run.try_later = False
        if not await self._open(run):
            return
        stalls, entries_done, waited, refilled, dismissed = 0, set(), False, set(), set()
        step_waited: set[tuple[Any, ...]] = set()  # pages (as drawn) waited on for a greyed-out step button
        account_waited = False
        sign_ins: dict[str, int] = {}  # what the saved password was used for on this pass
        pressed: list[tuple[Any, ...]] = []  # (page, button) for each button pressed on this pass
        pressed_on: list[str] = []  # and where, in words
        for _ in range(MAX_STEPS):
            if run.status in ("skipped", "submitted"):  # Skip, or "I submitted it", pressed while it ran
                return
            data, text = await self._look()
            run.page_info = _page_info(data)
            run.url = data["url"]
            run.page = srv.browser.current_tab or run.page
            if await self._decline_cookies(run, data, text):
                data, text = await self._look()
                run.page_info = _page_info(data)
            kind = classify(data, text)
            limited = _try_later(data)
            if limited and not (waited_out and not pressed and not sign_ins):
                # "Too Many Attempts. Try Again Later." (Oracle after many sign-up emails in a day):
                # its Continue goes back to the posting, and pressing on only goes round again.
                # Where the person pressed Resume on it, they've waited: carry on from there.
                await self._bring_forward(run)
                self._pause(run, "stuck", f"{_site(run, data)} says \u201c{limited}\u201d Leave this job for a "
                            "while, then press Resume.")
                run.try_later = run.status == "needs_you"
                return
            actions = data.get("actions") or []
            entry_here = any(_ENTRY.match(final_text(a["text"])) and not a.get("disabled") for a in actions)
            if kind == "form" and entry_here and not _application_like(data):
                kind = "page"  # a posting with a "send me similar jobs" box: go in through Apply
            if kind == "page" and not data.get("fields") and _TURNED_AWAY.search(data.get("title") or ""):
                return self._pause(run, "stuck", f"{_site(run, data)} turned the desk's browser away (403 Forbidden). "
                                   "Open the posting in your own browser to apply there.")
            if kind == "bot_check":
                await self._bring_forward(run)
                return self._pause(run, "bot_check", _BOT_CHECK_SAYS, seen=data)
            if (kind == "sign_in" and not account_waited and not _account_and_application(data)
                    and sum(f.get("kind") == "password" for f in data.get("fields") or []) >= 2):
                # A Create Account form: Qorvo's draws its application below it a moment later
                account_waited = True
                await self._wait_for_application()
                continue
            if kind == "sign_in" and _account_and_application(data):
                return await self._apply_with_account(run, data)
            if kind == "sign_in":
                done = await self._sign_in(run, data, sign_ins)
                if done in ("email_step", "submitted", "create_account"):
                    sign_ins[done] = sign_ins.get(done, 0) + 1
                    continue
                await self._bring_forward(run)
                if done in ("prefilled", "filled"):
                    data, _ = await self._look()
                    run.page_info = _page_info(data)  # the page as filled
                if done == "prefilled" and _account_and_application(data):
                    return await self._apply_with_account(run, data)  # its application was drawn after all
                if done == "prefilled":
                    first = (" Your saved password didn't sign in there, so this is probably your first application "
                             "with them; if you do have an account, sign in instead." if sign_ins.get("create_account") else "")
                    return self._pause(run, "sign_in", f"I filled in {_site(run, data)}'s Create Account form with your "
                                       "details and saved password. Fill in anything it still asks for (a picture code, "
                                       "say), tick their terms box if there is one and create the account (then verify "
                                       "your email if they ask); the desk carries on after that." + first, seen=data)
                if done == "filled":
                    return self._pause(run, "sign_in", f"I filled in your email and saved password on {_site(run, data)}'s "
                                       "sign-in form. Press its sign-in button in the browser window; the desk carries on "
                                       "after that.", seen=data)
                tip = _password_tip(data["url"])
                failed = " Your saved password didn't sign in there." if sign_ins.get("submitted") else ""
                return self._pause(run, "sign_in", f"Sign in (or create your account) on {_site(run, data)} in "
                                   "the browser window; the desk carries on by itself after that." + failed + tip,
                                   seen=data)
            if kind == "email_code":
                await self._bring_forward(run)
                watching = (" Your email app password is saved, so the desk is also watching your inbox for it."
                            if self.mail_login() else "")
                if any(_CODE_FIELD.search(f.get("label") or "") for f in data.get("fields") or []):
                    return self._pause(run, "email_code", "The site emailed you a code. Enter it in the browser "
                                       "window; the desk carries on by itself after that." + watching, seen=data)
                # The link opens in the person's own browser, which leaves this tab where it is
                return self._pause(run, "email_code", "The site emailed you a link to confirm your email. Open it, then "
                                   "reload this job's tab in the desk's browser window: the link opens in your usual "
                                   "browser, so the tab doesn't change by itself. The desk carries on after that."
                                   + watching, seen=data)
            if kind == "form":
                run.seen_form = True
                once_failed = await self._fill_once(run, data)
                key = _fingerprint(data)
                if key not in entries_done and _EXPERIENCE_PAGE.search(" ".join(data.get("headings") or [])):
                    entries_done.add(key)
                    for section in ("work", "education"):
                        added = await srv.add_entries(section)
                        if added.get("clicks"):
                            self._log(run, f"added {added['clicks']} {section} block(s)")
                result = await srv.autofill(job_id=run.job_id)
                if result["filled"]:
                    self._log(run, f"filled {len(result['filled'])} field(s) on {_where(data)}")
                pending, missing_files = _pending(result, once_failed)
                self._note_skipped(run, result)
                before, page_key = data, (data.get("url"), tuple(data.get("headings") or []))
                data, text = await self._look()  # filling can add or enable things (State after Country, Submit)
                run.page_info = _page_info(data)  # what the person sees on the desk: the page as filled
                # a question the site has since answered itself (Oracle fills County from the ZIP
                # picked); one whose answer was turned down (it has an error) is still asked
                now = {f["id"]: f for f in data.get("fields") or []}
                # a box drawn again has a new id: found by its label, when no other box has it
                # (Workday's Month / Day / Year boxes are all "Date")
                by_label: dict[str, list[dict[str, Any]]] = {}
                for f in data.get("fields") or []:
                    by_label.setdefault(f.get("label") or "", []).append(f)

                def answered(q: dict[str, Any]) -> bool:  # (called only in this pass of the loop)
                    same = by_label.get(q.get("label") or "") or []  # noqa: B023
                    f = now.get(q.get("id")) or (same[0] if len(same) == 1 and q.get("label") else None)  # noqa: B023
                    return f is not None and not is_empty_value(f.get("value"))

                def refused(q: dict[str, Any]) -> bool:  # a box lost while being drawn again wasn't turned down
                    return bool(q.get("error")) and not str(q["error"]).startswith("KeyError")

                pending = [q for q in pending if refused(q) or not answered(q)]
                if _new_required(before, data) and page_key not in refilled:
                    refilled.add(page_key)  # answers drew new questions ("If yes, explain"): fill those too
                    continue
                if missing_files:  # questions come along, so they can be answered meanwhile
                    return self._pause(run, "stuck", "The form needs a file the profile doesn't point to (set "
                                       "documents.resume in profile.yaml): " + ", ".join(f["label"] for f in missing_files)
                                       + (f". It also has {len(pending)} question(s) your profile doesn't answer."
                                          if pending else ""), pending)
                if pending:
                    return self._pause(run, "questions", f"{len(pending)} question(s) your profile doesn't answer. "
                                       "Answer them here and the desk fills them in (and remembers them).", pending)
                actions = data.get("actions") or []
                entry_here = any(_ENTRY.match(final_text(a["text"])) and not a.get("disabled") for a in actions)
            # a step button beside a Submit (a footer "Submit" on step 1 of 4) means there's more to
            # fill: the review page is only where Submit is the way on
            forward_here = any(_FORWARD.match(final_text(a["text"])) and not a.get("disabled") and not a.get("is_submit")
                               for a in data.get("actions") or [])
            if ((kind == "form" or run.seen_form and not entry_here) and not forward_here
                    and await srv.browser.find_submit()):
                return await self._finish(run, data, text)
            action = pick_next(data.get("actions") or [], in_form=kind == "form")
            note = next((a for a in data.get("actions") or [] if _DISMISS_NOTE.match(a.get("text", "").strip())
                         and not a.get("disabled")), None) if action is None and kind == "page" else None
            if note is not None and note["text"] not in dismissed:
                dismissed.add(note["text"])  # the posting under it is hidden until it goes
                await srv.click(note["id"])
                self._log(run, f"dismissed a note (\u201c{note['text'].strip()}\u201d)")
                continue
            sign_in_step = bool(_SIGN_IN_STEP.search(" ".join(data.get("headings") or [])))
            blank = not (data.get("fields") or data.get("actions") or data.get("headings")) and len(text.strip()) < 40
            if action is None and kind == "page" and not waited:
                waited = True  # slow pages (Intel's Workday, Eightfold forms) draw their buttons late
                wait = SIGN_IN_STEP_WAIT if sign_in_step else BLANK_PAGE_WAIT if blank else LATE_BUTTONS_WAIT
                if await self._wait_for_progress(wait):
                    continue
            drawn = (_page_key(data), _boxes(data))  # a page that has drawn more since is waited on again
            if action is None and kind == "form" and drawn not in step_waited and _greyed_step(data):
                # a form still being drawn: Oracle's Personal Info shows its upload boxes and a greyed-out
                # Next first, then its name, email and phone boxes and an enabled Next
                step_waited.add(drawn)
                if await self._wait_for_step(LATE_BUTTONS_WAIT, data):
                    continue
            if action is None and sign_in_step:
                # Workday's sign-in step whose sign-in buttons never drew (Applied's, now and then):
                # read as an ordinary page, so only a change from this one is the person past it
                await self._bring_forward(run)
                return self._pause(run, "sign_in", f"Sign in (or create your account) on {_site(run, data)} in "
                                   "the browser window; the desk carries on by itself after that.", seen=data)
            if action is None:
                greyed = [a for a in data.get("actions") or [] if a.get("is_submit") and a.get("disabled")
                          and not a.get("aside")] or _greyed_step(data)
                if greyed:
                    said = "; ".join(e for e in data.get("errors") or [] if _ERRORISH.search(e))[:300]
                    return self._pause(run, "stuck", f"\u201c{greyed[0]['text']}\u201d is greyed out, so the site still "
                                       "wants something" + (f": {said}" if said else ".") +
                                       " Fix it in the browser, then press Resume.")
                if blank:  # still, after the wait? (it was read before it)
                    now, now_text = await self._look()
                    blank = not (now.get("fields") or now.get("actions") or now.get("headings")) \
                        and len(now_text.strip()) < 40
                if blank:
                    return self._pause(run, "stuck", "The page stayed blank: the site may be slow or down. Reload it "
                                       "in the browser, then press Resume.")
                agree = next((a for a in data.get("actions") or [] if _AGREEMENT.search(a["text"].strip())
                              and not a.get("cookie") and "cookie" not in a["text"].lower()
                              and not a.get("disabled")), None)
                if agree is not None:
                    return self._pause(run, "stuck", f"The way on is \u201c{agree['text'].strip()}\u201d, which agrees to "
                                       "something in your name, so it's yours to press. Read it and press it in the "
                                       "browser window if you're happy to, then press Resume.")
                if _account_step(data):
                    # the person's to do (the desk never makes an account), and theirs until they're off the
                    # account site: its next steps (name, email code) read as forms, but aren't the application
                    await self._bring_forward(run)
                    self._pause(run, "sign_in", f"{_site(run, data)} wants an account for this email, and the desk "
                                "never makes one: create it (or sign in with the email you use there) in the browser "
                                "window; the desk carries on by itself after that.", seen=data)
                    run.hold_host = run.paused_host
                    return
                return self._pause(run, "stuck", "I couldn't find the button that moves this application on. "
                                   "Take it a step further in the browser, then press Resume.")
            key = (data.get("url"), tuple(data.get("headings") or []), action["text"].strip().lower())
            if key in pressed and pressed[-1] != key:
                # Round in a circle (Oracle sent its sites back to the posting from "Continue"):
                # going round again only repeats it, and may email the person another code.
                lap = " \u2192 ".join(pressed_on[pressed.index(key):])
                return self._pause(run, "stuck", f"I went round in a circle ({lap}, then back to {_where(data)}), so "
                                   "the site isn't letting this application on. Have a look in the browser, then press "
                                   "Resume.")
            if kind == "page" and [p[:1] + p[2:] for p in pressed[-2:]] == [key[:1] + key[2:]] * 2:
                # "next" on a list of jobs (a link to a search page): its pages differ, but no
                # application ever opens
                return self._pause(run, "stuck", f"I pressed \u201c{action['text'].strip()}\u201d three times on "
                                   f"{_where(data)} and no application opened. Have a look in the browser, then press "
                                   "Resume.")
            pressed.append(key)
            pressed_on.append(f"\u201c{action['text'].strip()}\u201d on {_page_said(data)}")
            looked = _fingerprint(data)
            try:
                clicked = await srv.click(action["id"])
            except KeyError:  # the page changed between looking and clicking (a tab opened): look again
                self._log(run, f"“{action['text']}” was gone by the time I clicked; looking again")
                run.page = srv.browser.current_tab or run.page
                continue
            if clicked.get("clicked") is False and str(clicked.get("blocked") or "").startswith("Dry run"):
                # practice mode: a button that sends a form and isn't a step button ("Quick Apply" on
                # a posting) isn't pressed. That's no review page: nothing has been filled
                return self._pause(run, "stuck", f"Practice mode: “{action['text'].strip()}” sends a form "
                                   "and isn't a step button I know, so I didn't press it. Nothing was sent.")
            if clicked.get("clicked") is False and str(clicked.get("blocked") or "").startswith("Cookie"):
                # never picked by the desk (it only declines banners), but never taken for the review page
                return self._pause(run, "stuck", "A cookie or privacy banner is in the way, and the desk never accepts "
                                   "one for you. Choose in the browser window, then press Resume.")
            if clicked.get("clicked") is False:  # the guard says it's the final submit
                return await self._finish(run, data, text)
            self._log(run, f"clicked “{action['text']}”")
            run.page = srv.browser.current_tab or run.page
            if _fingerprint(clicked) == looked and await self._new_tab_soon(run, NEW_TAB_WAIT):
                continue  # asml.com's Apply Now opens Workday in a new tab a moment after the click
            if _fingerprint(clicked) == looked:
                if await srv.browser.challenge_showing():  # the click brought up a CAPTCHA
                    await self._bring_forward(run)
                    return self._pause(run, "bot_check", _BOT_CHECK_SAYS)
                stalls += 1
                problems = [e for e in clicked.get("errors") or [] if _ERRORISH.search(e)]
                if problems or stalls >= 2:
                    # say what's wrong: Workday lists it as links ("Error-Email") and marks fields
                    problems = _flagged(clicked)
                    if not problems:
                        now = (await self._look())[0]
                        problems = _flagged(now) or [  # a Next greyed out until a resume is attached, say
                            f"\u201c{a['text'].strip()}\u201d is greyed out, so the site still wants something (a file, "
                            "say, or a box to tick)" for a in _greyed_step(now)[:1]]
                    if any(_ROBOT.search(p) for p in problems):  # Randstad's "verify that you are not a robot"
                        await self._bring_forward(run)
                        return self._pause(run, "bot_check", _BOT_CHECK_SAYS)
                    errors = "; ".join(problems)[:300].rstrip(" .")
                    return self._pause(run, "stuck", "The page didn't move on" + (f": {errors}." if errors else ".")
                                       + " Fix it in the browser, then press Resume.")
            else:
                stalls = 0
        self._pause(run, "stuck", "This application has more steps than I expected. Have a look in the browser, "
                    "then press Resume.")

    async def _new_tab_soon(self, run: Run, seconds: float) -> bool:
        """Follow a tab that opens a little after a click (the browser makes it current)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            tab = self.srv.browser.current_tab
            if tab is not None and tab is not run.page:
                run.page = tab
                self._log(run, "followed the application into a new tab")
                return True
            await asyncio.sleep(0.25)
        return False

    async def _wait_for_application(self) -> None:
        """Give a Create Account form a moment to draw an application below it."""
        deadline = time.monotonic() + ACCOUNT_DRAW_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            if _account_and_application((await self._look())[0]):
                return

    async def _wait_for_step(self, seconds: float, before: dict[str, Any]) -> bool:
        """Wait for a form still being drawn: its step button enabled, or more boxes to fill."""
        had = _boxes(before)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            data = await self.srv.inspect_form(include_dropdown_options=False)
            if pick_next(data.get("actions") or [], in_form=True) or _boxes(data) > had:
                return True
        return False

    async def _wait_for_progress(self, seconds: float) -> bool:
        """Wait for a form, a sign-in, a bot check or a button that moves things on to appear
        (KLA's Workday draws its "Sign in with email" button a few seconds late)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            data, text = await self._look()
            if classify(data, text) != "page" or pick_next(data.get("actions") or [], in_form=False):
                return True
        return False

    async def _fill_once(self, run: Run, data: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Answers the person gave for this application only (not remembered). Returns the
        ones that didn't go in, by question: the field, the reason, and the entries to
        choose from when the answer was a group of them.

        An answer that didn't go in, or went in and was gone a moment later, gets a second
        try before the person is asked for it again: Eightfold's dropdowns (Lam's and
        Micron's consent questions, live) now and then take one only the second time."""
        if not run.once:
            return {}
        filled: set[str] = set()
        failed: dict[str, dict[str, Any]] = {}
        for attempt in range(2):
            fields = {f["id"]: f for f in data.get("fields") or []}
            by_id = {fid: question_key(f.get("label") or "") for fid, f in fields.items()}
            # an answer stays with the page it first went in on: "If yes, please explain" about
            # relatives isn't the answer to a later page's "If yes, please explain"
            here = _page_key(data)
            fills = [{"id": fid, "value": run.once[key]} for fid, key in by_id.items()
                     if key in run.once and run.once_page.get(key, here) == here
                     and is_empty_value(fields[fid].get("value"))]
            if not fills:
                break
            out = await self.srv.fill_form(fills)
            failed = {by_id[r["id"]]: {**{k: fields[r["id"]][k] for k in ("id", "kind", "label", "section", "required",
                                                                          "options")
                                          if fields[r["id"]].get(k) not in (None, [])},
                                       "error": r.get("error") or "didn't take",
                                       **({"options": r["options"]} if r.get("options") else {})}
                      for r in out.get("results", []) if not r.get("ok") and r.get("id") in fields}
            filled |= {by_id[f["id"]] for f in fills} - set(failed)
            for key in {by_id[f["id"]] for f in fills} - set(failed):
                run.once_page.setdefault(key, here)
            if attempt == 0:
                await asyncio.sleep(ONCE_SETTLE)  # long enough for an answer that won't stick to be gone
                data, _ = await self._look()
        filled -= set(failed)
        if filled:
            self._log(run, f"filled {len(filled)} answer(s) you gave for this application")
        if failed:
            self._log(run, f"{len(failed)} of your answers didn't go in")
        return failed

    def _note_skipped(self, run: Run, result: dict[str, Any]) -> None:
        """Optional fields that wouldn't take the profile's answer are skipped, and said so."""
        skipped = [f.get("label") or "a field" for f in result["failed"] if f.get("required") is False]
        if skipped:
            self._log(run, f"skipped {len(skipped)} optional field(s) that wouldn't take your profile's answer: "
                      + ", ".join(f"\u201c{label}\u201d" for label in skipped[:3]))

    async def _decline_cookies(self, run: Run, data: dict[str, Any], text: str) -> bool:
        """Press Reject / Decline / Necessary only on a cookie banner (never Accept). Banners
        cover forms and catch clicks; ones with no way to decline are left for the person.
        A banner is known by its text, or by its buttons sitting in a cookie/consent box: on
        a long page the banner comes after the part of the text that's read."""
        mentioned = "cookie" in text.lower()
        button = next((a for a in data.get("actions") or []
                       if _DECLINE_COOKIES.match(a.get("text", "").strip()) and not a.get("disabled")
                       and (mentioned or a.get("cookie"))), None)
        if button is None:
            return False
        result = await self.srv.click(button["id"])
        if result.get("clicked"):
            self._log(run, f"declined cookies (\u201c{button['text']}\u201d)")
            return True
        return False

    async def _sign_in(self, run: Run, data: dict[str, Any], tried: dict[str, int], details: bool = True) -> str | None:
        """With the profile email and a stored <ats>_password (if the person saved one):

        - "email_step": pressed Workday's "Sign in with email" to reach the form;
        - "submitted": filled in the sign-in form and pressed its button (once a pass);
        - "create_account": that didn't get in, most likely because there's no account
          there yet, so it opened the site's Create Account form;
        - "prefilled": filled in a Create Account form, leaving its terms and button to
          the person;
        - "filled": filled in a sign-in form whose button it doesn't recognise.

        None when there's nothing (more) to do. `tried` counts what this pass already did;
        `details=False` leaves a Create Account form's other boxes as they are."""
        srv = self.srv
        secret = password_for(data["url"])
        if secret is None or _secret(secret) is None:
            return None
        # the page's own fields: never a password box inside a frame from another site
        fields = [f for f in data.get("fields") or [] if not re.match(r"f\d+-", str(f.get("id")))]
        actions = [a for a in data.get("actions") or [] if not a.get("disabled")]
        if not fields:
            email_button = next((a for a in actions if re.match(r"^sign in with email$", a["text"], re.I)), None)
            if email_button is None or tried.get("email_step", 0) >= 2:
                return None
            await srv.click(email_button["id"])
            return "email_step"
        passwords = [f for f in fields if f["kind"] == "password"]
        # The way to a new account is a link or a plain button. A Create Account form's own
        # button sends that form (it creates the account), so it's never it: a form's submit,
        # or a button in a form with two password boxes (Workday's, a div).
        create = next((a for a in actions if _CREATE_ACCOUNT.match(a["text"].strip()) and not a.get("form_submit")
                       and not a.get("account_form")), None)
        # Where the way to a new account led: a form with one password box (UKG Pro's "Create
        # your account") is the new account's, as it no longer offers a way to one.
        signing_up = len(passwords) == 1 and bool(tried.get("create_account")) and create is None
        if len(passwords) == 1 and tried.get("submitted") and not signing_up:
            # Signed in once already and still asked to: the password didn't get in. Trying it
            # again won't help (and can lock an account); a first visit needs an account. The
            # way there gets a second press: Amkor's sign-in page reloads after a failed sign-in,
            # and a click on its "Create an account" made before that has finished is lost.
            if create is None or tried.get("create_account", 0) >= 2:
                return None
            try:
                await srv.click(create["id"])
            except KeyError:
                return None
            if not tried.get("create_account"):
                self._log(run, "your saved password didn't sign in, so I opened Create Account")
            return "create_account"
        boxes = [f for f in fields if f["kind"] in ("text", "email")
                 and re.search(r"e-?mail|user ?name|login", f.get("label") or "", re.I)]
        address = config.Profile.load().get("personal.email")
        if not boxes or not address or not 1 <= len(passwords) <= 2:
            return None
        new_account = len(passwords) == 2 or signing_up
        if new_account:
            # The email in every box that asks for it ("Retype Email Address"; the user name
            # SCREEN's form asks for is the email too), and the rest from the profile.
            if details:
                await self._fill_account_details(fields)
            await srv.fill_form([{"id": f["id"], "value": address} for f in boxes])
        else:
            await srv.fill_form([{"id": boxes[0]["id"], "value": address}])
        for box in passwords:
            if not (await srv.fill_secret(box["id"], secret)).get("ok"):
                return None  # it didn't go in: say nothing about a saved password
        if new_account:  # accepting the site's terms, and creating the account, are the person's call
            self._log(run, "filled the Create Account form with your details and saved password")
            return "prefilled"
        # The form's own button, after its password box: a "Sign In" in the site's header opens
        # its sign-in page or pop-up instead, and sends nothing (Workday's)
        button = next((a for a in actions if _SIGN_IN_ACTION.match(a["text"].strip()) and not _SOCIAL.search(a["text"])
                       and a.get("after_password")), None)
        if button is None:
            return "filled"
        await srv.click(button["id"])
        # whether that signed in is the next look's to say
        self._log(run, f"pressed \u201c{button['text'].strip()}\u201d with your saved password")
        return "submitted"

    async def _fill_account_details(self, fields: list[dict[str, Any]]) -> None:
        """A Create Account form's other boxes, from the profile: names, country. Its check
        boxes (a newsletter, the site's terms) and file boxes ("upload your resume now?") are
        left alone, as is anything the profile doesn't answer (Benchmark's picture code)."""
        empty = [f for f in fields if f.get("kind") in _ACCOUNT_KINDS and is_empty_value(f.get("value"))]
        plan = plan_autofill(empty, config.Profile.load(), {})
        if plan["to_fill"]:
            await self.srv.fill_form([{"id": f["id"], "value": f["value"]} for f in plan["to_fill"]])

    async def _apply_with_account(self, run: Run, data: dict[str, Any]) -> None:
        """A page that creates the account as it applies: Qorvo's SuccessFactors puts Create
        Account and the whole application on one page. The application is filled in around
        the password boxes; choosing the password and pressing the site's own Apply, which
        sends the application, are the person's."""
        srv = self.srv
        run.seen_form = True
        once_failed = await self._fill_once(run, data)
        result = await srv.autofill(job_id=run.job_id)
        if result["filled"]:
            self._log(run, f"filled {len(result['filled'])} field(s) on {_where(data)}")
        pending, missing_files = _pending(result, once_failed)
        self._note_skipped(run, result)
        data, _ = await self._look()
        run.page_info = _page_info(data)  # the page as filled
        if missing_files:
            return self._pause(run, "stuck", "The form needs a file the profile doesn't point to (set documents.resume "
                               "in profile.yaml): " + ", ".join(f["label"] for f in missing_files), pending)
        if pending:
            return self._pause(run, "questions", f"{len(pending)} question(s) your profile doesn't answer. Answer them "
                               "here and the desk fills them in (and remembers them).", pending)
        saved = await self._sign_in(run, data, {}, details=False) == "prefilled"  # filled in above
        if saved:  # the record of the page shows its password boxes filled, too
            run.page_info = _page_info((await self._look())[0])
        srv._mark_ready(srv.tracker().get(run.job_id), "filled by the Job Desk; the site creates the account as it applies")
        await self._bring_forward(run)
        password = "Your saved password is in its password boxes" if saved else "Choose a password in its password boxes"
        return self._pause(run, "your_submit", f"Filled in on {_site(run, data)}, which creates your account as you "
                           f"apply. {password}, check the page, then press its Apply button yourself: that sends the "
                           "application. Then press \u201cI submitted it\u201d here.")

    async def _bring_forward(self, run: Run) -> None:
        if run.page is not None and not run.page.is_closed():
            try:
                await run.page.bring_to_front()
            except Exception:
                pass

    async def _finish(self, run: Run, data: dict[str, Any], text: str) -> None:
        """The review page (or a one-page form with its submit button) is reached."""
        srv = self.srv
        if run.status in ("skipped", "submitted"):
            return
        try:  # Indeed's form inside an employer's page is Indeed's
            ats = await srv.browser.human_submit_ats(run.page) or detect_ats(data["url"])
        except Exception:
            ats = detect_ats(data["url"])
        job = srv.tracker().get(run.job_id)
        if ats in config.HUMAN_SUBMIT_ONLY:
            srv._mark_ready(job, f"filled on {ATS_NAMES.get(ats, ats)} by the Job Desk")
            return self._pause(run, "your_submit", f"Filled. {ATS_NAMES.get(ats, ats)} doesn't allow automated "
                               "submitting: review it in the browser and click Submit yourself.")
        if _CAPTCHA.search(text):
            srv._mark_ready(job, "filled by the Job Desk; has a CAPTCHA")
            return self._pause(run, "captcha", "Filled. The form has a CAPTCHA: tick it in the browser, then "
                               "press Submit here.")
        shown = "; ".join(e for e in _flagged(data) if not _ERROR_COUNT.match(e))[:300].rstrip(" .")
        if shown and run.submit and self.auto_submit:  # Insight's review page lists what's still missing
            return self._pause(run, "stuck", f"The review page shows errors: {shown}. Fix them in the browser, then "
                               "press Submit.")
        if run.submit and self.auto_submit:
            return await self._submit(run, by_person=False)
        srv._mark_ready(job, "filled by the Job Desk")
        run.status, run.need = "ready", ""
        run.reason = "Filled and waiting on the review page. Check it in the browser, then press Submit."
        if shown:
            run.reason = (f"Filled, but the review page shows errors: {shown}. Fix those in the browser before you "
                          "press Submit.")
        if run.pressed_before:
            run.reason = ("Filled and waiting on the review page. Submit was pressed for this job before and no "
                          "confirmation showed, so it may have gone through: check your email or the site before you "
                          "press Submit." + (f" The review page also shows errors: {shown}." if shown else ""))
        self._log(run, "reached the review page")

    async def _submit(self, run: Run, by_person: bool = True) -> None:
        """Press the final button: the person pressed Submit for this job, or (by_person
        False) "Submit for me" is on, which also needs every required field filled."""
        srv = self.srv
        if run.status == "skipped" or self._already_done(run):
            return
        if run.page is not None and not run.page.is_closed() and not self._own_place(run, run.page.url):
            # its tab was taken on to another posting: Submit there would send that one as this job
            return self._pause(run, "stuck", "Its tab has gone on to another page, so I didn't press Submit there. "
                               "Press Resume to fill this application again in a tab of its own.")
        if not srv.browser.use_tab(run.page):
            # waiting on Resume now, whatever it waited on before (a CAPTCHA's pause has no Resume button)
            run.status, run.need, run.blocking = "needs_you", "stuck", False
            run.reason = "Its tab was closed. Press Resume to fill it again first."
            self._log(run, run.reason)
            return
        srv.browser.current_job_id = run.job_id
        if not by_person:
            data = await srv.inspect_form(include_dropdown_options=False)
            empty = [f.get("label") or "a field" for f in _empty_required(data)]
            if empty:
                return self._pause(run, "stuck", "Not submitted: required fields are still empty (" + ", ".join(empty[:5])
                                   + "). Fill them in the browser, then press Resume.")
        if run.status == "skipped":  # skipped while the page was being checked
            return
        result = await srv.submit_application(job_id=run.job_id, user_confirmed=True)
        if result.get("submitted") and not result.get("confirmed"):
            run.pressed_before = True  # it may have gone: Submit for me never presses it again
        if result.get("submitted") and result.get("confirmed"):
            run.status, run.need, run.reason = "submitted", "", "Submitted."
        elif result.get("submitted"):
            try:
                still = bool(await srv.browser.find_submit())
            except Exception:
                still = False
            problems = "; ".join(e for e in result.get("errors") or [] if _ERRORISH.search(e))[:300]
            if still:  # the form is still there: the site didn't take it
                return self._pause(run, "submit_failed", "I pressed Submit, but the form is still there"
                                   + (f": {problems}" if problems else ".") + " Fix it in the browser, then press "
                                   "Submit again (or \u201cI submitted it\u201d if it did go through).")
            return self._pause(run, "check_submit", "I pressed Submit, but no confirmation showed. Check the page: "
                               "if it went through, press \u201cI submitted it\u201d.")
        else:
            run.status, run.need = "ready", ""
            run.reason = result.get("reason") or "Not submitted."
        self._log(run, run.reason)


_APPLICATION_FIELD = re.compile(r"first name|last name|full name|legal name|resume|\bcv\b|phone|address|"
                                r"authori[sz]ed|sponsor|start (?:the |your |an? )?appl", re.I)  # "email to start application"


_ACCOUNT_FIELD = re.compile(r"e-?mail|password|user ?name|log ?in|terms|privacy|captcha|language|locale|"
                            r"text in (?:the )?(?:image|picture)", re.I)  # Benchmark's "Enter the text in image above"


def _try_later(data: dict[str, Any]) -> str | None:
    """A heading or error saying the site won't go on for now ("Too Many Attempts. Try Again
    Later."), in its own words, ending in a full stop."""
    for t in [*(data.get("headings") or []), *(data.get("errors") or [])]:
        if _TRY_LATER.search(t or ""):
            t = " ".join(t.split())
            return t if t[-1:] in ".!?" else t + "."
    return None


def _account_and_application(data: dict[str, Any]) -> bool:
    """A Create Account form that is also the whole application (Qorvo's SuccessFactors):
    password boxes, and five or more fields an application asks for, a name or address
    among them. Check boxes and file boxes don't count: Benchmark's registration asks for
    names, offers a resume upload and a "no resume" box, and is still only an account."""
    fields = data.get("fields") or []
    if not any(f.get("kind") == "password" for f in fields):
        return False
    others = [f for f in fields if f.get("kind") not in ("password", "checkbox", "file")
              and not _ACCOUNT_FIELD.search(f.get("label") or "")]
    return len(others) >= 5 and any(_APPLICATION_FIELD.search(f.get("label") or "") for f in others)


def _pending(result: dict[str, Any], once_failed: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """After autofill: the required questions to put to the person (with any answer of theirs
    the page turned down), and the required file inputs nothing could go in."""
    pending = [{**f, **once_failed[question_key(f.get("label") or "")]}
               if question_key(f.get("label") or "") in once_failed else f
               for f in result["needs_input"] if f.get("required") and f.get("kind") != "file"]
    # the profile's answers that didn't go in, where the site requires one (an optional field
    # is skipped: Qorvo's optional veteran question has no "don't wish to answer")
    pending += [{"id": f["id"], "label": f.get("label") or "", "kind": f.get("kind") or ("combobox" if f.get("options") else "text"),
                 "required": True, "error": f.get("error"),
                 **({"options": f["options"]} if f.get("options") else {})}
                for f in result["failed"] if f.get("required", True)]
    # an answer of theirs the page turned down is asked again, even when the box isn't empty
    # (words left in a picker's search box read as an answer), unless the profile's answer
    # went in after it
    asked = {question_key(f.get("label") or "") for f in pending + (result.get("filled") or [])}
    pending += [f for key, f in once_failed.items() if key not in asked and f.get("kind") != "file"]
    missing_files = [f for f in result["needs_input"] if f.get("required") and f.get("kind") == "file"]
    return pending, missing_files


def _pressed_before(job: dict[str, Any]) -> bool:
    """Was Submit pressed for this job with no confirmation showing? submit_application keeps
    a record of each press in the job's folder, so this outlasts a restart of the desk."""
    if not job.get("folder"):
        return False
    try:
        record = json.loads((Path(job["folder"]) / "submission.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and not record.get("confirmed")


def tailored_ready(job: dict[str, Any]) -> bool:
    """A resume written for this job is in its folder, and not a draft that came out too long."""
    folder = Path(job["folder"]) if job.get("folder") else None
    # only the resume's marker holds the job: a cover letter that came out long doesn't
    too_long = folder and any(p.stem.lower().endswith("resume") for p in folder.glob("*.too-long"))
    return bool(tailored_document(job, "resume")) and not too_long


def _application_like(data: dict[str, Any]) -> bool:
    fields = data.get("fields") or []
    return len(fields) >= 3 or sum(bool(_APPLICATION_FIELD.search(f.get("label") or "")) for f in fields) >= 1


# A saved password is typed only into its own system's pages, on that system's own domains:
# never into a page that just mentions one in its address (evil.example/myworkdayjobs.com).
PASSWORD_SITES = {
    "workday": ("myworkdayjobs.com", "myworkday.com", "myworkdaysite.com"),
    "successfactors": ("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu"),
    "icims": ("icims.com",), "applicantstack": ("applicantstack.com",), "ukg": ("ultipro.com",),
    "infor": ("inforcloudsuite.com",), "taleo": ("taleo.net",), "brassring": ("brassring.com",),
    "avature": ("avature.net",),
}


def password_for(url: str) -> str | None:
    """The name of the saved password that belongs on this page, if any."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    for ats, domains in PASSWORD_SITES.items():
        if parsed.scheme == "https" and any(host == d or host.endswith("." + d) for d in domains):
            return f"{ats}_password"
    return None


# The systems whose password the desk page lets the person save, by the name it shows
DESK_PASSWORDS = {"workday_password": "Workday", "successfactors_password": "SuccessFactors", "icims_password": "iCIMS",
                  "applicantstack_password": "ApplicantStack", "ukg_password": "UKG Pro", "infor_password": "Infor"}


def _password_tip(url: str) -> str:
    """At a sign-in the person does by hand: that a password saved on the desk would do it
    for them next time, when one can be saved for this system and none is."""
    name = password_for(url)
    if name not in DESK_PASSWORDS or _secret(name) is not None:
        return ""
    system = DESK_PASSWORDS[name]
    article = "an" if system[0].lower() in "aeio" else "a"  # an iCIMS, an Infor; a UKG Pro, a Workday
    return f" Save {article} {system} password on the desk and it fills these in for you next time."


def _secret(name: str) -> str | None:
    try:
        return config.get_secret(name)
    except Exception:  # a hand-edited secrets.yaml with a typo: sign in by hand rather than fail the job
        return None


def _empty_required(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in data.get("fields") or [] if f.get("required") and not f.get("disabled")
            and f.get("kind") != "password" and is_empty_value(f.get("value"))]


def _new_required(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Did filling the page bring up required fields that weren't there before?"""
    seen = {f.get("id") for f in before.get("fields") or []}
    return any(f.get("id") not in seen for f in _empty_required(after))


def _bare(url: str) -> str:
    """An address without its query and fragment."""
    return urlparse(url)._replace(query="", fragment="").geturl()


def _account_step(data: dict[str, Any]) -> bool:
    """A page about making an account, with nothing to fill and a button that makes one: not a
    page that only has a "Register" link in its header."""
    about = " ".join([data.get("title") or "", *(data.get("headings") or [])])
    return (not data.get("fields") and bool(_ACCOUNT_PAGE.search(about))
            and any(_CREATE_ACCOUNT.match(final_text(a["text"])) and not a.get("disabled")
                    for a in data.get("actions") or []))


def _greyed_step(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The page's step buttons (Next, Continue) that are greyed out."""
    return [a for a in data.get("actions") or [] if a.get("disabled") and _FORWARD.match(final_text(a["text"]))]


def _boxes(data: dict[str, Any]) -> int:
    """How many boxes a page has to fill (not counting file uploads)."""
    return sum(f.get("kind") != "file" and not f.get("disabled") for f in data.get("fields") or [])


def _page_key(data: dict[str, Any]) -> tuple:
    """Which page of an application this is: its address and headings."""
    return _bare(data.get("url") or ""), tuple(data.get("headings") or [])


def _page_sig(data: dict[str, Any]) -> tuple:
    """What a page is, near enough: its address, headings and the questions on it."""
    return (_bare(data.get("url") or ""), tuple(data.get("headings") or []),
            tuple(sorted(f.get("label") or "" for f in data.get("fields") or [] if isinstance(f, dict))))


def _site_key(url: str) -> str:
    """The part of a page's address that names its site ("myworkdayjobs.com", "asml.com"), near enough."""
    host = (urlparse(url).hostname or "").lower()
    return ".".join(host.split(".")[-2:])


def _site(run: Run, data: dict[str, Any]) -> str:
    ats = detect_ats(data.get("url") or "")
    if ats in ("company_site", ""):
        return f"{run.company}'s site" if run.company else "the site"
    return ATS_NAMES.get(ats, ats)


def question_key(label: str) -> str:
    return norm(clean_label(label))


def _page_said(data: dict[str, Any]) -> str:
    """A page in a few words: its heading, and what it says if it shows a message."""
    message = next((e for e in data.get("errors") or [] if len(e) > 12), "")
    return _where(data) + (f" (\u201c{message[:160]}\u201d)" if message else "")


def _where(data: dict[str, Any]) -> str:
    headings = [h for h in data.get("headings") or [] if h and not re.match(r"current step", h, re.I)]
    return f"“{headings[-1][:60]}”" if headings else "this page"
