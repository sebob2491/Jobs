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
import hashlib
import json
import logging
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from . import config, mailbox, report
from .ats import ATS_NAMES, detect_ats, shared_system
from .autofill import (clean_label, entry_of, is_empty_value, no_choice_for_no_degree, norm, plan_autofill,
                       polarity, tailored_document)
from .browser import (TabClosed, _accepts_cookies, _cookie_setting, confirmations, declines_cookies, final_text,
                      may_accept_cookies)

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
# Stops the desk most likely got wrong, noted for the developer with nothing pressed (report.take_note),
# as are questions whose answers didn't go in. A bot check, a CAPTCHA, an emailed code, the person's
# own Submit, a tailored resume and a question the profile doesn't answer are the person's by design.
# Practice mode notes them too: practice runs are where they're looked for
NOTED = {"stuck", "sign_in", "submit_failed", "check_submit"}
STOP = "stop"  # the page a job stopped on, saved for a problem report: its folder's debug/<time>-stop
POLL_SECONDS = 3.0
# With an email app password saved, a job waiting on an emailed code or link has the inbox
# looked at this often, for this long after it began waiting
MAIL_POLL_SECONDS = 20
MAIL_WINDOW = 15 * 60
# A password reset asked for with the inbox watched: when no email has come by then, the email most
# likely has no account there (most sites won't say), so the desk makes one instead
RESET_MAIL_WAIT = 3 * 60
# How long a site may take over a new account before the page is looked at again (a Workday site goes on
# to its Sign In page a few seconds after Create Account is pressed)
ACCOUNT_WAIT = 15
# A site locks an account after a few refused sign-ins: a job's sign-in is pressed with the same saved
# password this many times at most (once more after a password reset)
SIGN_IN_TRIES = 2
NOTICE_WAIT = 5  # seconds for a notice agreed to for the person to go (one may fade out)
# A job that went in keeps its tab (its confirmation, for the person to see) while it's among the newest
# this many: one tab per job adds up over a long queue
DONE_TABS_KEPT = 3
SHARED_LOOK_BACK = 30  # seconds looked back for a job's code while an earlier job waits on the same sender
FINISHED = {"applied", "interviewing", "offer", "rejected", "withdrawn"}  # tracker statuses never applied to again

_BOT_TITLE = re.compile(r"just a moment|attention required|access denied|pardon our interruption|security check|"
                        r"are you a robot|bot (?:check|detection)", re.I)
# A bare "403 Forbidden" (Valleywise Health's postings, to the desk's browser): the site turns the
# browser away. There's nothing to solve, so it holds nothing up: the person applies elsewhere
_TURNED_AWAY = re.compile(r"^\s*(?:403\s*)?forbidden\s*$", re.I)
# A posting that has closed: "The job posting you are looking for has expired or the position has
# already been filled" (Edward Jones' BrassRing), "This job is no longer available"
_CLOSED = re.compile(
    r"\b(?:posting|job|position|requisition|opening|vacancy)\b[^.]{0,60}?\b(?:has|have|is|was)\b(?: already)?(?: been)?"
    r" (?:expired|filled|closed|no longer (?:available|open|active|accepting))|no longer accepting applications", re.I)
_BOT_TEXT = re.compile(r"verify (?:that )?you are (?:a )?human|are you a robot|checking (?:if the site connection is secure|"
                       r"your browser)|press (?:&|and) hold|complete the security check|unusual traffic from your|"
                       r"enable javascript and cookies to continue|request unsuccessful|you have been blocked", re.I)
_CAPTCHA = re.compile(r"i'?m not a robot|i am human|hcaptcha|recaptcha challenge", re.I)
_VERIFY_EMAIL = re.compile(r"verif(?:y|ication)\b.{0,40}\b(?:e-?mail|account|link)|check your (?:e-?mail|inbox)", re.I)
_CODE_FIELD = re.compile(r"verification code|one[- ]time (?:pass)?code|passcode|security code|\bcode\b.{0,40}"
                         r"(?:sent|email)|enter (?:the )?(?:\d-digit )?code|\botp\b", re.I)
_SIGN_IN_ACTION = re.compile(r"^(sign in|log ?in|sign in with email)$", re.I)
# What else a form holding only the sign-in calls its button (SuccessFactors': "Submit")
_SIGN_IN_SUBMIT = re.compile(r"^(submit|continue|next|go|enter|log ?on|sign ?on|sign in now|log ?in now)$", re.I)
# The button pressed once the emailed code is in; one labelled Submit is left to the person
_AFTER_CODE = re.compile(r"^(verify|confirm|continue|next)( (code|e-?mail|account|my e-?mail))?$", re.I)
_TRY_LATER = re.compile(r"\btoo many\b.{0,30}\b(?:attempts|requests|tries)\b|\btry again (?:later|in \d+)|\brate[- ]limit",
                        re.I)
_CREATE_ACCOUNT = re.compile(r"^(?:proceed to |continue to )?(create (?:an |your |a new )?account|sign up|register)"
                             r"(?: now)?[.!]?$|^don['\u2019]?t have an account(?: yet)?\??$", re.I)  # (SuccessFactors' link)
# With settings.manage_accounts: a Create Account form's own button, the boxes on it that agree to
# the site's terms (required ones, or its terms, or a privacy policy or notice read: "Yes, I confirm that
# I have read the privacy notice", a Workday site's; never a newsletter's, job alerts', being kept informed
# or contacted), the site saying the email already has an account (said of the account or
# email, not "Already have an account? Sign in"), the way to a password reset (not a username
# reminder), the buttons of a reset's pages, and its "we've emailed you a link"
_MAKE_ACCOUNT = re.compile(r"^(create(?: an| my| your| a new)? account|register|sign ?up|create|submit|continue)$", re.I)
_TERMS_ONLY = re.compile(r"terms|conditions|(?:privacy|data protection) (?:policy|notice|statement)", re.I)
_TERMS_BOX = re.compile(r"terms|conditions|privacy|consent|agree|acknowledge|policy|notice", re.I)
_NOT_TERMS = re.compile(r"newsletter|marketing|job alerts?|text messages?|\bsms\b|promotion|offers|subscribe|similar jobs|"
                        r"talent (?:community|network)|keep me|stay informed|send me|contact(?:ed)? me|be contacted|"
                        r"share my|other (?:roles|positions|jobs|opportunities)|affiliat", re.I)
_ACCOUNT_EXISTS = re.compile(r"\b(?:account|e-?mail(?: address)?|user ?name|login)\b[^.?!]{0,40}\balready\b\s*(?:exists|"
                             r"registered|in use|associated|taken|been (?:registered|used|taken))", re.I)
_FORGOT = re.compile(r"\b(?:forgot|reset|recover)\b[^.?!]{0,20}\bpassword|trouble (?:signing|logging) in|"
                     r"can'?t (?:sign|log) in", re.I)
_RESET_PAGE = re.compile(r"forgot|reset|recover|new password|change (?:your )?password|set (?:a |your )?(?:new )?password", re.I)
_RESET_ASK = re.compile(r"\b(?:send|reset|request|submit|continue|next|e-?mail me)\b", re.I)
_RESET_SET = re.compile(r"\b(?:reset|update|change|save|set|submit|continue|confirm)\b", re.I)
_NOT_A_STEP = re.compile(r"\b(?:back|cancel|sign in|log ?in|return)\b", re.I)
_NEW_PASSWORD = re.compile(r"\bnew\b|confirm|verify|re-?enter|re-?type|again", re.I)
_OLD_PASSWORD = re.compile(r"\b(?:current|old|existing|temporary)\b", re.I)
_RESET_SENT = re.compile(r"\b(?:sent|emailed)\b.{0,80}\b(?:link|e-?mail|instructions)|check your (?:e-?mail|inbox)", re.I)
# A reset's page saying there's no account for the email ("There is no user with that username or email")
_NO_ACCOUNT = re.compile(r"\bno (?:user|account|record|match)\b|\b(?:not|isn'?t|wasn'?t) (?:found|registered|recogni[sz]ed)|"
                         r"\b(?:don'?t|do not|didn'?t|did not) recogni[sz]e (?:this|that|your|the) e-?mail|"
                         r"does(?:n'?t| not) (?:exist|have an account|match (?:any|an) account)|"
                         r"\b(?:unknown|unrecogni[sz]ed) (?:user|e-?mail|account)|could(?:n'?t| not) find (?:an? |your )?"
                         r"(?:account|user)", re.I)
# A page about making an account ("Create an account", amazon.jobs after an email it doesn't know)
_ACCOUNT_PAGE = re.compile(r"\b(create (?:an |your |a new )?account|sign up|register)\b", re.I)
_ACCOUNT_KINDS = {"text", "email", "tel", "select", "combobox", "listbox"}  # not check boxes or files
_SOCIAL = re.compile(r"\b(google|apple|linked ?in|facebook|microsoft|indeed|seek)\b", re.I)
_STEP = re.compile(r"^(save (?:and|&) continue|continue|next|next step|review|review (?:and|&) submit|"
                   r"review application|proceed|go to next step|start)$", re.I)
_FORWARD = re.compile(r"^(save (?:and|&) continue|continue|next|next step|review|review application|proceed|"
                      r"go to next step)$", re.I)
_STEP_OF = re.compile(r"\bstep (\d+) of (\d+)\b", re.I)  # a progress bar's place: "current step 3 of 6"
_SIGN_IN_STEP = re.compile(r"create account\s*/\s*sign in|sign in\s*/\s*create account", re.I)  # Workday's step name
_ENTRY = re.compile(r"^(apply manually|apply now|apply online|apply|easy apply|quick apply|"
                    r"apply for (?:this|the) (?:job|position|role)(?: online)?|"
                    r"apply to (?:this )?job|start (?:your |my )?application|i'?m interested|"
                    r"continue to application|apply on (?:the )?(?:company|employer)(?:'s)? (?:site|website))$", re.I)
# The words of a button that sends an application once it's filled in: on a form, never the way in
_APPLY_SENDS = re.compile(r"^(apply|apply now|apply online|easy apply|quick apply|apply for (?:this|the) (?:job|position|role)"
                          r"(?: online)?|apply to (?:this )?job)$", re.I)
_AVOID = re.compile(r"autofill|with resume|resume parse|sign ?in|log ?in|create account|register|upload|back|"
                    r"previous|cancel|save for later|withdraw|delete|remove|search|share|print|email (?:me|this)", re.I)
# A button that agrees to something ("I Acknowledge the Privacy Notice", Schwab's iCIMS sign-in):
# never pressed for the person
_AGREEMENT = re.compile(r"^(?:i )?(?:acknowledge|agree|accept|consent)\b.*\b(?:notice|terms|policy|privacy|statement|"
                        r"agreement|conditions)\b|^i (?:acknowledge|agree|accept|consent)\b", re.I)
_EXPERIENCE_PAGE = re.compile(r"my experience|work experience|employment history", re.I)
# A note laid over the page (Nikon's UKG board: "Accessibility Note") with nothing else to press.
_DISMISS_NOTE = re.compile(r"^(dismiss(?: (?:note|notice|message))?|close (?:note|notice|message))$", re.I)
# With settings.accept_notices: an employer's notice about AI or automated screening of applications
# (Eightfold's: "… uses an artificial intelligence ("AI") recruiting software …"), and its button that
# agrees to it
_AI_NOTICE = re.compile(r"\bartificial intelligence\b|\b(?-i:AI)\b|\bmachine learning\b|\bautomated (?:employment )?"
                        r"decision|\bautomated (?:screening|assessment|evaluation|processing|tools?|systems?)\b", re.I)
_ABOUT_APPLYING = re.compile(r"recruit|applica|candidate|hiring|resume|screening", re.I)
_AGREES = re.compile(r"^(?:yes,? )?(?:i )?(?:agree|accept|consent|acknowledge)(?: (?:and|&) (?:continue|proceed))?$|"
                     r"^i understand$", re.I)
# and an application's attestation: that its information is true and complete ("I certify that the
# information contained in the application … is correct"), or consent to the background check that
# comes with applying. Never a newsletter's, job alerts' or marketing's
_ATTESTS = re.compile(r"\b(?:certif(?:y|ies)|attest|affirm|declare|acknowledge|confirm)\b.{0,200}?\b(?:information|answers?|"
                      r"statements?|responses?|facts)\b.{0,200}?\b(?:true|correct|complete|accurate)\b|"
                      r"\b(?:consent|authori[sz]e|agree)\b.{0,120}?\bbackground (?:check|screening|investigation)", re.I | re.S)
_NOT_ATTESTED = re.compile(r"newsletter|marketing|job alerts?|text messages?|\bsms\b|promotion|subscribe|"
                           r"talent (?:community|network)", re.I)
_ATTEST_KINDS = {"select", "listbox", "combobox", "checkbox"}
# A fill the page never let happen (it timed out, its script failed, a dialog stood over the box):
# the profile has the answer, so it's no question for the person
_FILL_BROKE = re.compile(r"^(?:TimeoutError|Error|TargetClosedError|DialogOpen)\b")


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
    cookies_asked: set[str] = field(default_factory=set)  # sites paused on once for a cookie banner
    resetting: bool = False  # asked the site for a password reset (settings.manage_accounts): its link is awaited
    reset_asked: bool = False  # (once a job: a reset that didn't get in isn't asked for again)
    reset_from: str = ""  # the sign-in page a reset was asked from: back there once it's done elsewhere
    reset_waited: bool = False  # paused for the reset email (a Resume after that means it's done)
    reset_no_mail: bool = False  # and none came in RESET_MAIL_WAIT: no account there, most likely, so one is made
    # The job's sign-ins pressed with the saved password, and which password that was (a digest): a Run
    # outlasts Resume and the queue, so these hold for the job, not for one pass at it
    sign_in_tries: int = 0
    sign_in_key: str = ""
    accounts_tried: int = 0  # Create Account pressed for this job (settings.manage_accounts): never again
    account_made: str = ""  # what to log once the site shows the account made (not just the form gone)
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
        return {k: v for k, v in self.__dict__.items() if k not in ("page", "tab_mark", "cookies_asked", "resetting",
                                                                     "reset_asked", "reset_from", "reset_waited",
                                                                     "reset_no_mail", "sign_in_tries", "sign_in_key",
                                                                     "accounts_tried", "account_made")}


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


_UNREACHED = [re.compile(p) for p in (r"\bhttps?://([\w.-]+)", r"([\w.-]+\.[a-z]{2,})(?:\u2019|')s server IP address",
                                     r"([\w.-]+) refused to connect", r"([\w.-]+) took too long to respond")]


def unreached_host(data: dict[str, Any], text: str) -> str:
    """The host Chrome's "This site can't be reached" page names: in its text ("The webpage at
    https://...", "127.0.0.1 refused to connect"), or its title, which some versions set to it."""
    for pattern in _UNREACHED:
        if m := pattern.search(text or ""):
            return m.group(1)
    title = (data.get("title") or "").strip()
    return title if re.fullmatch(r"[\w.-]+\.[\w-]+|\d+(?:\.\d+){3}", title) else ""


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
    """Enough to see why a job paused, without any of the values typed into the page. Room for
    a whole Workday My Experience page (four jobs and two schools: 70 boxes and more): a
    report that stops at its 40th box shows the last blocks as never read."""
    return {
        "url": data.get("url"), "title": data.get("title"), "headings": (data.get("headings") or [])[:8],
        "actions": [a.get("text", "") + (" (disabled)" if a.get("disabled") else "") for a in data.get("actions") or []][:60],
        "fields": [{**{k: f.get(k) for k in ("label", "kind", "required", "section", "sublabel") if f.get(k) is not None},
                    "empty": is_empty_value(f.get("value"))} for f in data.get("fields") or []][:150],
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
        if run is None or run.page is None or self.srv.browser.lost(run.page):
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
            if run.page is None or self.srv.browser.lost(run.page):
                continue
            if run.need == "email_code":
                await self._check_mail_safely(run)  # the code or link came after the queue went on
            if self._reset_overdue(run):
                run.reset_no_mail = True  # no reset email: _drive goes back to make an account
                moved = True
            else:
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
            if not moved and self._reset_overdue(blocker):
                blocker.reset_no_mail = moved = True  # no reset email: _drive goes back to make an account
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
            await self._strict(self._save_stop(run))
        except TabClosed:
            if run.status != "skipped":
                run.status, run.need, run.blocking = "failed", "", False
                run.reason = "Its tab was closed. Press Resume to start this application again."
                self._log(run, run.reason)
        except Exception as e:
            if run.status != "skipped":
                run.status, run.need = "failed", ""
                run.reason = f"Something went wrong: {type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"
                if run.page is not None and self.srv.browser.lost(run.page) == "crashed":  # (Playwright: "Page crashed")
                    run.reason = "Its tab crashed. Press Resume to start this application again."
                self._log(run, run.reason)
        finally:
            self._give_back_tools(held)
            if run.status == "running":  # the desk was stopped part-way
                run.status, run.reason = "failed", "Stopped before it finished. Press Resume to carry on."
            run.updated = time.time()
        await self._close_done_tabs()

    async def _close_done_tabs(self) -> None:
        """Close the tabs of jobs that went in, but the newest DONE_TABS_KEPT. With one tab per
        job, a long queue left dozens open (a long live run's Chrome crashed with them, Oct 2026).
        A job waiting on the person, ready for their Submit, or failed (Resume carries on in its
        tab) keeps its tab with the work in it."""
        done = sorted((r for r in self.runs.values() if r.status == "submitted" and r.page is not None),
                      key=lambda r: r.updated, reverse=True)
        closing = done[DONE_TABS_KEPT:]
        # never a tab another job still has (each job opens its own, but a tab never goes twice)
        gone = {r.job_id for r in closing}
        kept = {t for r in self.runs.values() if r.job_id not in gone and r.page is not None
                for t in self.srv.browser.lineage(r.page)}
        for run in closing:
            for tab in self.srv.browser.lineage(run.page):  # its application tab, and the tab that opened it
                if tab not in kept and not tab.is_closed():
                    with contextlib.suppress(Exception):
                        await tab.close()
            run.page = None

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
        if run.page is None or self.srv.browser.lost(run.page):
            return True  # they closed it (or it crashed, or the browser with it): start the job again
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

    async def _gone_home(self, run: Run, tab: Any) -> bool:
        """Has the person taken a job's tab back up to its site's careers home or front page: an
        address above the job's posting, or above the page it was left on ("/careers" from
        "/careers/job/12")? Never the page it was left on or the posting itself, nor a page that
        says the application went (a site may go home after its own Submit): those carry on."""
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        own = [u for u in (run.url, job.get("url"), job.get("apply_url")) if u]
        # a site that names the job in its address's query (an Eightfold site's /careers?pid=123):
        # ?pid=999 there is another job, and /careers?query=... with no id its careers home
        mine: dict[str, set[str]] = {}
        for u in own:
            for key, value in _job_ids(u).items():
                mine.setdefault(key, set()).add(value)
        theirs = _job_ids(tab.url)
        if not any(k in mine and v not in mine[k] for k, v in theirs.items()):  # (not another job's)
            if any(_bare(tab.url) == _bare(u) for u in own):
                if not mine or any(k in theirs for k in mine):
                    return False  # the posting, its application, or the page it was left on
            elif not any(_above(tab.url, u) for u in own):
                return False
        try:
            _, text = await self.srv.browser.peek(tab)
        except Exception:  # a tab mid-way through loading: carried on with, as before
            return False
        return not confirmations(text)

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
            want = "code" if boxes else "reset" if run.resetting else "link"
            found = await asyncio.to_thread(mailbox.search, *login, run.paused_at, senders, want, own_link,
                                            min(later) if later else None, look_back)
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
            elif found.kind == "reset":  # its page sets the new password: opened in the job's own tab
                if run.page is None or run.page.is_closed():
                    return
                await run.page.goto(found.value, wait_until="domcontentloaded", timeout=45000)
                run.mail_done = True
                self._log(run, f"opened the password reset link from your email (sent from {found.sender})")
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
        if need in NOTED or need == "questions" and any(q.get("error") for q in run.questions):
            self._take_note(run)

    def _take_note(self, run: Run) -> None:
        """A note on this stop, for the person to file on the desk's Notes. Whatever goes wrong
        taking it, the job still pauses and the queue goes on."""
        try:
            job = self.srv.tracker().get(run.job_id, with_description=False) or {"id": run.job_id}
            report.take_note(job, run)
        except Exception:
            logging.getLogger(__name__).warning("couldn't take a note on job %s's stop", run.job_id, exc_info=True)

    async def _save_stop(self, run: Run) -> None:
        """Save the page a job stopped on for the person in its folder's debug/, beside the pages a
        failed fill saves, so a problem report holds it (scrubbed) among its pages. The newest few
        stops are kept (as many as a report holds). Whatever goes wrong saving it, the job still
        waits on the person."""
        if run.status != "needs_you" or run.need == "tailor" or run.page is None or run.page.is_closed():
            return  # (waiting for a tailored resume is before its tab opens)
        try:
            job = self.srv.tracker().get(run.job_id, with_description=False) or {}
            if not job.get("folder") or not self.srv.browser.use_tab(run.page):
                return
            debug = Path(job["folder"]) / "debug"
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
            await self.srv.browser.snapshot(debug / f"{stamp}-{STOP}", note=f"stopped: {run.need}")
            for old in sorted(debug.glob(f"*-{STOP}"))[:-report.PAGES]:
                shutil.rmtree(old, ignore_errors=True)
        except Exception:
            logging.getLogger(__name__).warning("couldn't save the page job %s stopped on", run.job_id, exc_info=True)

    async def _look(self) -> tuple[dict[str, Any], str]:
        data = await self.srv.inspect_form(include_dropdown_options=False)
        text = await self.srv.page_text(4000)
        data["challenge"] = await self.srv.browser.challenge_showing()
        return data, text

    async def _open(self, run: Run) -> bool:
        srv = self.srv
        # its own tab, while that still shows this job's application: carried on with as it is,
        # with what's filled in and uploaded there. One taken on to another posting meanwhile (by
        # the person, or by Claude's tools) is no longer this job's: filling it would put this
        # job's details into another employer's form, and Submit for me send it
        if run.page is not None and not srv.browser.lost(run.page):
            if not self._own_place(run, run.page.url):
                self._log(run, "its tab had gone on to another page, so I opened the job again in a new tab")
                run.page = None
            elif await self._gone_home(run, run.page):
                # no application there, and the jobs it lists aren't this one: an Apply pressed
                # there would apply to another job as this one
                self._log(run, "its tab had gone back to the site's careers home, so I opened the job again in a "
                          "new tab")
                run.page = None
        if srv.browser.use_tab(run.page):
            srv.browser.current_job_id = run.job_id
            return True
        if run.page is not None:
            # The only other time it starts over: its tab is gone, and the form with it (nothing
            # brings back a resume uploaded there). Said, as all the person sees is a fresh form,
            # and after a browser crash Chrome's "Restore pages?"
            why = srv.browser.lost(run.page)
            self._log(run, {"browser": "the browser had closed (or crashed) since, and its tab with it",
                            "crashed": "its tab crashed"}.get(why, "its tab was closed")
                      + ", so I opened the job again in a new tab: its application starts over")
            if why == "crashed":
                with contextlib.suppress(Exception):
                    await run.page.close()  # its "Aw, Snap!" page: the new tab takes its place
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
        agreed: set[str] = set()  # notices agreed to for the person on this pass (settings.accept_notices)
        for _ in range(MAX_STEPS):
            if run.status in ("skipped", "submitted"):  # Skip, or "I submitted it", pressed while it ran
                return
            data, text = await self._look()
            run.page_info = _page_info(data)
            run.url = data["url"]
            run.page = srv.browser.current_tab or run.page
            if run.seen_form and not pressed and (gone := sorted(confirmations(text))) and not _mid_application(data):
                # Back on a job the desk filled in, before it has pressed anything: the person pressed the
                # site's own Submit and then Resume (Workday's Candidate Home shows "Application Submitted").
                # It went, so it's marked applied and never filled in again. (Not a step of the form that
                # thanks them: "Thank you for your application. Please complete the below questions.")
                srv.tracker().update(run.job_id, status="applied", note="submitted on the site")
                run.status, run.need, run.blocking, run.left = "submitted", "", False, False
                run.reason = f"Submitted: {_site(run, data)} says \u201c{gone[0]}\u201d."
                self._log(run, run.reason)
                return
            cookies = await self._decline_cookies(run, data, text)
            if cookies is False:
                await self._bring_forward(run)
                return self._pause(run, "stuck", f"{_site(run, data)} shows a cookie banner over the page that the desk "
                                   "can't decline for you. Choose in the banner in the browser window, then press "
                                   "Resume." + ("" if _cookie_setting() else " (To let the desk accept banners with no "
                                   "way to decline for you, set accept_cookies: true under settings: in profile.yaml.)"))
            if cookies:
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
            if run.account_made and kind in ("form", "email_code") and not run.resetting:
                # on from Create Account to the application (its sign-in got in) or to verifying the email:
                # only now is the account said to be made. A form that went away (a Workday site swaps it
                # for its Sign In) isn't that
                self._log(run, run.account_made)
                run.account_made = ""
            if str(data.get("url") or "").startswith("chrome-error://"):
                host = unreached_host(data, text)
                where = f"{host}, which" if host else "a page that"
                return self._pause(run, "stuck", f"The application went on to {where} couldn't be reached, so the page "
                                   "didn't load. Press Resume to try again, or open the posting in your own browser to "
                                   "apply there.")
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
            manage = (kind in ("sign_in", "page", "email_code") or run.resetting) and _may_manage_accounts()
            if kind == "form":
                run.resetting = False  # past the sign-in, however the password was set
                run.sign_in_tries = 0  # (and none of its sign-ins was refused)
            if manage and run.resetting and kind in ("page", "email_code") and _RESET_SENT.search(text):
                if not run.reset_waited:
                    return await self._await_reset_email(run, data)
                # Resume pressed on it: the person set the password from the email themselves (or the
                # inbox wait ran out), so back to the sign-in page, where the saved password is tried again.
                # No email in RESET_MAIL_WAIT: most likely no account there, so the way to a new one instead.
                run.resetting = False
                if run.reset_from and run.page is not None:
                    await run.page.goto(run.reset_from, wait_until="domcontentloaded", timeout=45000)
                    sign_ins.clear()
                    if run.reset_no_mail:
                        self._log(run, f"no password reset email came from {_site(run, data)} in "
                                  f"{RESET_MAIL_WAIT // 60} minutes, so I went back to make an account there")
                        sign_ins["submitted"] = 1
                    else:  # the saved password is the site's now, as the person was asked: one more sign-in
                        run.sign_in_tries = min(run.sign_in_tries, SIGN_IN_TRIES - 1)
                    continue
            if kind == "sign_in" and manage and run.resetting:
                done_reset = await self._set_new_password(run, data, text)
                if done_reset == "set":
                    sign_ins.clear()  # the saved password is the site's now: signed in with afresh
                    run.sign_in_tries = min(run.sign_in_tries, SIGN_IN_TRIES - 1)
                    continue
                if done_reset == "paused":
                    return
            said_exists = " ".join([*(data.get("errors") or []), text[:3000]])
            if kind == "sign_in" and manage and sign_ins.get("made") and _ACCOUNT_EXISTS.search(said_exists):
                # the email has an account there already, and the saved password didn't sign in to it
                asked = await self._ask_for_reset(run, data, sign_ins)
                if asked == "paused":
                    return
                if asked == "no_account":
                    sign_ins["submitted"] = 1
                    continue
            if kind == "sign_in":
                # With the inbox watched, a refused password is reset before a new account is made (the
                # owner's choice: an account there already is the likelier, on a second application)
                reset_first = manage and not run.reset_asked and self.mail_login() is not None
                shown = data  # (what the page said before it's filled in again)
                done = await self._sign_in(run, data, sign_ins, reset_first=reset_first)
                if done in ("email_step", "submitted", "create_account"):
                    sign_ins[done] = sign_ins.get(done, 0) + 1
                    continue
                if done == "refused":
                    run.account_made = ""  # (its sign-in is refused: no account was made)
                asked = await self._ask_for_reset(run, data, sign_ins) if done == "refused" and manage else None
                if asked == "paused":
                    return
                if asked == "no_account":  # back on the sign-in page: its way to a new account, next
                    sign_ins["submitted"] = 1
                    continue
                await self._bring_forward(run)
                if done in ("prefilled", "filled"):
                    data, _ = await self._look()
                    run.page_info = _page_info(data)  # the page as filled
                if done == "prefilled" and _account_and_application(data):
                    return await self._apply_with_account(run, data)  # its application was drawn after all
                if done == "prefilled" and manage and not run.accounts_tried and await self._make_account(run, data):
                    # a new account: its sign-in (where the site asks for one) is tried afresh
                    sign_ins.pop("submitted", None), sign_ins.pop("create_account", None)
                    sign_ins["made"] = 1
                    continue
                if done == "prefilled" and run.accounts_tried:
                    # pressed for this job already, and it didn't go through: never pressed again
                    run.account_made = ""
                    wants = _account_wants(shown, data) or ("see the page in the browser window (a picture code, "
                                                            "say, or a password it doesn't accept).")
                    return self._pause(run, "sign_in", f"I pressed {_site(run, data)}'s Create Account with your details "
                                       f"and saved password, and it wants something more: {wants} Finish it there; the "
                                       "desk carries on after that.", seen=data)
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
                if done == "refused":  # and no reset (one was asked for already, or the site has no way to one)
                    told = next((e for e in data.get("errors") or [] if not _ERROR_COUNT.match(e.strip())), "")
                    return self._pause(run, "sign_in", f"Your saved password didn't sign in on {_site(run, data)}"
                                       + (", before or after I pressed its Create Account" if run.accounts_tried else "")
                                       + (f" (it says \u201c{told[:160].rstrip(' .')}\u201d)" if told else "") + "."
                                       + (" So that your account there isn't locked, the desk won't try it again for "
                                          "this job." if run.sign_in_tries >= SIGN_IN_TRIES else "")
                                       + " Sign in in the browser window (or reset the password through its \u201cForgot "
                                       "password\u201d to the one you saved on the desk); the desk carries on by itself "
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
            if (notice := _notice(data)) is not None:  # the boxes behind it are filled once it's answered
                said = await self._answer_notice(run, data, notice, agreed, over_form=kind == "form")
                if said == "agreed":
                    continue
                if said == "paused":
                    return
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
                pending = await self._attest(run, pending)
                before, page_key = data, (data.get("url"), tuple(data.get("headings") or []))
                data, text = await self._look()  # filling can add or enable things (State after Country, Submit)
                run.page_info = _page_info(data)  # what the person sees on the desk: the page as filled
                if (notice := _notice(data)) is not None:  # one the fills brought up (Eightfold's, as the resume went up)
                    said = await self._answer_notice(run, data, notice, agreed, over_form=True)
                    if said == "agreed":
                        continue
                    if said == "paused":
                        return
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
                gaps, pending = self._entry_gaps(run, data, pending)
                unfilled, pending = _unfilled(pending)
                couldnt = f" {_couldnt_fill(unfilled)}" if unfilled else ""
                if missing_files:  # questions come along, so they can be answered meanwhile
                    return self._pause(run, "stuck", "The form needs a file the profile doesn't point to (set "
                                       "documents.resume in profile.yaml): " + ", ".join(f["label"] for f in missing_files)
                                       + "." + (f" {gaps}" if gaps else "")
                                       + (f" It also has {len(pending)} question(s) your profile doesn't answer."
                                          if pending else "") + couldnt, pending)
                if gaps:
                    return self._pause(run, "stuck", gaps + (f" It also has {len(pending)} other question(s), here."
                                                             if pending else "") + couldnt, pending)
                if pending:
                    return self._pause(run, "questions", f"{len(pending)} question(s) your profile doesn't answer. "
                                       "Answer them here and the desk fills them in (and remembers them)." + couldnt,
                                       pending)
                if unfilled:
                    return self._pause(run, "stuck", f"{couldnt.strip()} Fill {'it' if len(unfilled) == 1 else 'them'} in "
                                       "the browser, then press Resume (or press Resume for the desk to try again).")
                actions = data.get("actions") or []
                entry_here = any(_ENTRY.match(final_text(a["text"])) and not a.get("disabled") for a in actions)
            # a step button beside a Submit (a footer "Submit" on step 1 of 4) means there's more to
            # fill: the review page is only where Submit is the way on
            forward_here = any(_FORWARD.match(final_text(a["text"])) and not a.get("disabled") and not a.get("is_submit")
                               for a in data.get("actions") or [])
            if ((kind == "form" or run.seen_form and not entry_here) and not forward_here
                    and (submits := await srv.browser.find_submit())):
                if _sign_up_box(data, submits):
                    # the State of Arizona's "Apply Now": its button sends the name and email on, and the
                    # application is after it. A sending button is the person's to press
                    return await self._sign_up_pause(run, data, submits[0])
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
                    said = "; ".join(e for e in data.get("errors") or [] if _ERRORISH.search(e))[:300] \
                        or _unanswered(data)
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
                    # the person's to do (an account site's steps aren't a Create Account form the desk knows,
                    # even with settings.manage_accounts), and theirs until they're off the account site: its
                    # next steps (name, email code) read as forms, but aren't the application
                    await self._bring_forward(run)
                    self._pause(run, "sign_in", f"{_site(run, data)} wants an account for this email, and its account "
                                "steps are yours: create it (or sign in with the email you use there) in the browser "
                                "window; the desk carries on by itself after that.", seen=data)
                    run.hold_host = run.paused_host
                    return
                if closed := _closed_notice(data, text):  # only where nothing else explains the stop
                    return self._pause(run, "stuck", f"{_site(run, data)} says this posting has closed: \u201c{closed}\u201d "
                                       "There's nothing to apply to, so skip this job.")
                if _maintenance_page(data):
                    return self._pause(run, "stuck", f"{_site(run, data)} is down for maintenance (it sent the job to "
                                       f"{_bare(data.get('url') or '')}). Try again in a few hours: press Resume once "
                                       "it's back.")
                return self._pause(run, "stuck", "I couldn't find the button that moves this application on. "
                                   "Take it a step further in the browser, then press Resume.")
            if (kind == "form" and action.get("form_fields") == len(data.get("fields") or [])
                    and _sign_up_box(data, [action])):
                return await self._sign_up_pause(run, data, action)  # the same box, sent by a plain button
            if _APPLY_SENDS.match(final_text(action["text"])) and (kind == "form" or kind == "page" and _application_like(data)):
                # "Apply" on the form just filled in, or on a page that holds an application (SuccessFactors
                # shows a signed-in person's profile greyed out above its questions): the button that sends
                # it, which no live site has had as a way in. Never pressed for the person. (A posting the
                # site went back to, with no form on it, still has its Apply as the way in.)
                return await self._theirs_to_send(run, data, action)
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
                    if told := next((e for e in clicked.get("errors") or [] if _NO_ACCOUNT.search(e)), ""):
                        # an email-first sign-in with no account for the email (an Eightfold site: "We don't
                        # recognize this email. Create a new account"): the person's account to make
                        now = (await self._look())[0]
                        create = next((a for a in now.get("actions") or [] if not a.get("disabled")
                                       and _CREATE_ACCOUNT.match(final_text(a["text"]))), None)
                        if create is not None:
                            await self._bring_forward(run)
                            return self._pause(run, "sign_in", f"{_site(run, now)} has no account for your email yet "
                                               f"(it says \u201c{told[:160].rstrip(' .')}\u201d). Create one in the browser "
                                               f"window (its \u201c{create['text'].strip()}\u201d), or sign in with the "
                                               "email you use there; the desk carries on by itself after that.", seen=now)
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
            # a box's own answer (the desk's "section | label | sub-label", for a label more than
            # one box has), else the answer to its label
            by_id = {fid: placed if (placed := placed_key(f)) in run.once else question_key(f.get("label") or "")
                     for fid, f in fields.items()}
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
        """Optional fields that wouldn't take the profile's answer are skipped, and said so. So
        are optional ones whose choices don't have it (Qorvo's veteran list has no "I don't
        wish to answer"): left empty, since no other choice is the person's. Each said once."""
        said = []
        skipped = [f.get("label") or "a field" for f in result["failed"] if f.get("required") is False]
        if skipped:
            said.append(f"skipped {len(skipped)} optional field(s) that wouldn't take your profile's answer: "
                        + ", ".join(f"\u201c{_short(label)}\u201d" for label in skipped[:3]))
        unoffered = [f for f in result.get("needs_input") or [] if f.get("unmatched") and not f.get("required")]
        if unoffered:
            said.append(f"left {len(unoffered)} optional question(s) empty, as none of their choices is your profile's "
                        "answer: " + ", ".join(f"\u201c{_short(f.get('label') or 'a field')}\u201d (yours: "
                                               f"\u201c{f['unmatched']}\u201d)" for f in unoffered[:3]))
        for line in said:
            if line not in run.log:
                self._log(run, line)

    def _entry_gaps(self, run: Run, data: dict[str, Any],
                    pending: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """Boxes in a job's or school's block ("Work Experience 2": its From and To) whose answer
        lives in the profile's work_history or education_history are never asked one by one:
        twenty bare "From" and "To" boxes on the desk couldn't say whose they were, and an answer
        typed there would go in every one. What's said instead (which entries lack what, or
        which of the profile's answers the site didn't take), and the questions left to ask.
        A school is named with its block on the page ("Education 2"): one school can have two."""
        prof = config.Profile.load()
        missing: dict[str, dict[str, list[str]]] = {"work": {}, "education": {}, "extra": {}}
        refused: dict[str, list[str]] = {}  # the profile has it; the page didn't take it
        no_choice: list[str] = []  # Degree lists with nothing for classes without a degree
        rest = []
        for q in pending:
            found = entry_of(q, prof)
            if found is None:
                rest.append(q)
                continue
            name, kind = found
            block = str(q.get("section") or "")
            if kind == "education" and block and block != name:
                name = f"{name} in {block}"
            if no_choice_for_no_degree(q, prof):
                if name not in no_choice:
                    no_choice.append(name)
                continue
            what = clean_label(q.get("label") or "").strip() or "a box"
            into = refused if q.get("error") else missing[kind]
            if what not in into.setdefault(name, []):
                into[name].append(what)

        def listed(entries: dict[str, list[str]]) -> str:
            return "; ".join(f"{name} ({', '.join(what)})" for name, what in entries.items())

        site, said = _site(run, data), []
        if missing["work"]:
            said.append(f"{site} asks about your past jobs, and your profile doesn't have this for them: "
                        f"{listed(missing['work'])}. Add it to those jobs under work_history in profile.yaml (dates "
                        "as start: 2021-03 and end: 2023-06, or end: present), or ask Claude: \u201cadd the start and "
                        "end months of my jobs from my resume to my profile\u201d.")
        if missing["education"]:
            # what each missing box takes: a Degree box isn't dates, and coursework is never a degree
            asked = " ".join(w for boxes in missing["education"].values() for w in boxes).lower()
            how = []
            if re.search(r"degree|qualification|diploma", asked):
                how.append("degree: the one you earned there, or Some college (no degree) where you took classes "
                           "without finishing one")
            if re.search(r"major|field|study|discipline|concentration", asked):
                how.append("major: your field of study")
            if re.search(r"from|to\b|start|end|date|year|month|graduat", asked) or not how:
                how.append("years as start: 2016 and end: 2018")
            said.append(f"{site} asks about your schools, and your profile doesn't have this for them: "
                        f"{listed(missing['education'])}. Add it to those schools under education_history in "
                        f"profile.yaml ({'; '.join(how)}), or ask Claude to add it from your resume (only what it "
                        "says: never a degree you didn't finish).")
        if said:
            said.append("Then press Resume: the desk fills them in from your profile on every application after that. "
                        "Or type them into the page in the browser and press Resume.")
        if missing["extra"]:
            said.append(f"The page has more blocks than your profile has entries: {listed(missing['extra'])}. Remove "
                        "the extra block in the browser (or fill it in there), then press Resume.")
        if refused:
            said.append(f"{site} didn't take your profile's answer for: {listed(refused)}. Fill those in the browser "
                        "(in a list, the closest choice), then press Resume.")
        if no_choice:
            # "Some college (no degree)" in the profile can't go there, and coursework is never a degree
            said.append(f"The Degree list for {'; '.join(no_choice)} has no choice for classes without a degree, so "
                        "that box is yours (the desk never picks a degree you didn't earn): choose in the browser, "
                        "then press Resume. If you did finish a degree there, add it to that school under "
                        "education_history in profile.yaml (degree: the one you earned), and the desk fills it in "
                        "from then on.")
        return " ".join(said), rest

    @staticmethod
    def _account_button(actions: list[dict[str, Any]], pattern: re.Pattern[str], full: bool = True) -> dict[str, Any] | None:
        """A page's own button for an account step: not a cookie banner's, a footer box's, or a
        sign-in with another site's account."""
        return next((a for a in actions if not a.get("disabled") and not a.get("cookie") and not a.get("aside")
                     and not _SOCIAL.search(a["text"]) and (pattern.match if full else pattern.search)(final_text(a["text"]).strip())
                     and not (not full and _NOT_A_STEP.search(a["text"]))), None)

    async def _make_account(self, run: Run, data: dict[str, Any]) -> bool:
        """settings.manage_accounts: tick a filled Create Account form's terms boxes (the required
        ones, or its terms, or a privacy notice read: never a newsletter's or job alerts') and press
        the form's own button, after its password boxes. Once a job (Run.accounts_tried). Not with a
        CAPTCHA on the page: that's the person's. True when it was pressed; the account is said to be
        made only once the site shows it (Run.account_made, in _drive)."""
        if data.get("captcha") or data.get("challenge"):
            return False
        srv = self.srv
        boxes = [f for f in data.get("fields") or [] if f.get("kind") == "checkbox" and is_empty_value(f.get("value"))
                 and _TERMS_BOX.search(f.get("label") or "") and not _NOT_TERMS.search(f.get("label") or "")
                 and (f.get("required") or _TERMS_ONLY.search(f.get("label") or ""))]
        own = [a for a in data.get("actions") or [] if a.get("account_form") or a.get("form_submit") and a.get("after_password")]
        button = self._account_button(own, _MAKE_ACCOUNT)
        if button is None:
            return False
        if boxes and not (await srv.fill_form([{"id": f["id"], "value": True} for f in boxes])).get("ok"):
            return False
        try:
            await srv.browser.click(button["id"], allow_submit=True)  # (the form's own button: it creates the account)
        except Exception:
            return False
        run.accounts_tried += 1
        site = _site(run, data)
        ticked = ", ".join(f"\u201c{_short(f.get('label') or '')}\u201d" for f in boxes)
        self._log(run, f"pressed \u201c{button['text'].strip()}\u201d to create your account on {site} with your saved "
                  "password" + (f", after ticking {ticked}" if boxes else ""))
        run.account_made = (f"created your account on {site} with your saved password" + (" and agreed to its terms"
                            if boxes else "") + " (manage_accounts: false in profile.yaml leaves this to you)")
        await self._wait_for_account(data)
        return True

    async def _wait_for_account(self, form: dict[str, Any]) -> None:
        """After Create Account: until the site has done with the form (gone on, or said something
        about it). Looked at again sooner, the same form would be filled in a second time and taken
        for one that wants something more."""
        deadline = time.monotonic() + ACCOUNT_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            try:
                now = await self.srv.inspect_form(include_dropdown_options=False)
            except Exception:  # mid-way to the next page
                continue
            passwords = sum(f.get("kind") == "password" for f in now.get("fields") or [])
            if (now.get("url") != form.get("url") or passwords < 2
                    or (now.get("errors") or []) != (form.get("errors") or [])):
                return

    async def _ask_for_reset(self, run: Run, data: dict[str, Any], tried: dict[str, int]) -> str | None:
        """settings.manage_accounts: the saved password didn't sign in to an account there, so ask
        the site to email a password reset (its "Forgot password?", the profile's email), and wait
        for the email: its link is opened in the job's tab, and the saved password set as the new
        one (_set_new_password). Once a job. "paused" when it paused: once it has gone anywhere, it
        always does (with what it got to), so the page is never driven as an application, unless
        the site says it has no account for the email: then it's back on the sign-in page, and
        "no_account" says the way on is a new one. None: nothing was done."""
        if run.reset_asked:
            return None
        run.reset_asked = True
        run.account_made = ""  # (a Create Account pressed before this didn't make one it signs in to)
        srv = self.srv
        site = _site(run, data)

        async def stop(why: str, page: dict[str, Any]) -> str:
            run.resetting = False
            await self._bring_forward(run)
            self._pause(run, "sign_in", f"Your saved password didn't sign in on {site}, and I {why}. Finish it in the "
                        "browser window (reset the password to the one you saved on the desk, or sign in); the desk "
                        "carries on after that.", seen=page)
            return "paused"

        async def press(action: dict[str, Any], allow_submit: bool = False) -> bool:
            try:
                out = await (srv.browser.click(action["id"], allow_submit=True) if allow_submit else srv.click(action["id"]))
            except Exception:  # gone (a page drawn again after the failed sign-in) or won't take a click
                return False
            return out.get("clicked") is not False

        forgot = _forgot_action(data.get("actions") or [])
        if forgot is None:  # on the Create Account form: back to the sign-in page, which has the way to a reset
            # (its "Sign In" there is a link or a plain button: the form's own button creates the account, and
            # a sign-in form's own would only send the refused password again)
            back = next((a for a in data.get("actions") or [] if not a.get("disabled") and not a.get("form_submit")
                         and not a.get("account_form") and not a.get("sign_in_form") and not a.get("cookie")
                         and _SIGN_IN_ACTION.match(final_text(a["text"]).strip())), None)
            if back is None:
                return None
            if not await press(back):
                return None
            data, _ = await self._look()
            forgot = _forgot_action(data.get("actions") or [])
            if forgot is None:
                return await stop("couldn't find its way to a password reset", data)
        run.reset_from = data.get("url") or ""
        if not await press(forgot):
            return await stop("couldn't open its password reset", data)
        data, text = await self._look()
        address = config.Profile.load().get("personal.email")
        boxes = [f for f in data.get("fields") or [] if f.get("kind") in ("text", "email")
                 and re.search(r"e-?mail|user ?name|login", f.get("label") or "", re.I)]
        if (not _RESET_PAGE.search(" ".join([text[:2000], *(data.get("headings") or [])])) or not boxes or not address
                or any(f.get("kind") == "password" for f in data.get("fields") or [])):
            return await stop("opened its password reset, which isn't one I can fill in", data)
        if not (await srv.fill_form([{"id": boxes[0]["id"], "value": address}])).get("ok"):
            return await stop("opened its password reset, which didn't take your email", data)
        if data.get("captcha") or data.get("challenge"):
            run.resetting = True
            await self._bring_forward(run)
            self._pause(run, "bot_check", f"I opened {site}'s password reset for your saved password, which didn't sign "
                        "in, and filled in your email. Solve its picture code and press its button; the desk watches your "
                        "inbox for the reset email and sets your saved password as the new one.", seen=data)
            return "paused"
        button = self._account_button(data.get("actions") or [], _RESET_ASK, full=False)
        if button is None or not await press(button, allow_submit=True):  # (asks for the email: sends no application)
            return await stop("filled in your email on its password reset, but couldn't press its button", data)
        data, text = await self._look()
        if data.get("errors") and not _RESET_SENT.search(text):  # "No account found for this email"
            if _NO_ACCOUNT.search(" ".join(data["errors"])) and run.reset_from and run.page is not None:
                # nothing to reset: back to the sign-in page, for its way to a new account
                self._log(run, f"{site} has no account for your email, so I went back to make one there")
                await run.page.goto(run.reset_from, wait_until="domcontentloaded", timeout=45000)
                return "no_account"
            return await stop(f"asked for a password reset, and it says \u201c{data['errors'][0][:160]}\u201d", data)
        run.resetting = True
        self._log(run, f"your saved password didn't sign in on {site}, so I asked it to email a password reset "
                  "(manage_accounts: false in profile.yaml leaves this to you)")
        await self._await_reset_email(run, data)
        return "paused"

    async def _await_reset_email(self, run: Run, data: dict[str, Any]) -> None:
        watched = self.mail_login() is not None
        if not watched:
            await self._bring_forward(run)
        watching = (" The desk is watching your inbox for it, and sets your saved password as the new one. If none "
                    f"comes in {RESET_MAIL_WAIT // 60} minutes (most likely you have no account there), it makes one "
                    "instead. The other jobs carry on meanwhile."
                    if watched else " Open the link in the email, set the password you saved on the desk as "
                    "the new one, then press Resume: the desk signs in with it (an email app password saved on the desk "
                    "lets it do all this itself).")
        run.reset_waited = True
        self._pause(run, "email_code", f"{_site(run, data)} is emailing you a password reset: your saved password didn't "
                    "sign in there." + watching, seen=data)
        if watched:  # nothing for the person to do: the queue goes on, and the job is picked up again after
            run.blocking, run.left = False, True
            self._wake.set()

    def _reset_overdue(self, run: Run) -> bool:
        """Waiting on a reset email, with the inbox watched, that hasn't come in RESET_MAIL_WAIT."""
        return (run.resetting and run.reset_waited and run.status == "needs_you" and run.need == "email_code"
                and not run.mail_done and not run.reset_no_mail and self.mail_login() is not None
                and time.time() - run.paused_at > RESET_MAIL_WAIT)

    async def _set_new_password(self, run: Run, data: dict[str, Any], text: str) -> str | None:
        """A password reset's page (opened from its email): the saved password in its new-password
        boxes, and its button pressed. "set" when the site took it, "paused" when it's the
        person's (a picture code, or a password the site turned down); None for any other page."""
        srv = self.srv
        fields = [f for f in data.get("fields") or [] if not re.match(r"f\d+-", str(f.get("id")))]
        passwords = [f for f in fields if f.get("kind") == "password"]
        named = [f for f in fields if f.get("kind") in ("text", "email")
                 and re.search(r"e-?mail|user ?name|login", f.get("label") or "", re.I)]
        new = len(passwords) >= 2 or any(_NEW_PASSWORD.search(f.get("label") or "") for f in passwords)
        if (not passwords or named or not new or any(_OLD_PASSWORD.search(f.get("label") or "") for f in passwords)
                or not _RESET_PAGE.search(" ".join([text[:2000], *(data.get("headings") or [])]))):
            return None  # not a reset's page (a Create Account form asks for the email; a change asks the old one)
        secret = password_for(data["url"])
        if secret is None or _secret(secret) is None:
            return None
        for box in passwords:
            if not (await srv.fill_secret(box["id"], secret)).get("ok"):
                return None
        site = _site(run, data)
        if data.get("captcha") or data.get("challenge"):
            await self._bring_forward(run)
            self._pause(run, "bot_check", f"I put your saved password in {site}'s new-password boxes. Solve its picture "
                        "code and press its button; the desk carries on after that.", seen=data)
            return "paused"
        button = self._account_button(data.get("actions") or [], _RESET_SET, full=False)
        try:
            pressed = button is not None and (await srv.browser.click(button["id"], allow_submit=True)).get("clicked") is not False
        except Exception:
            pressed = False
        after, after_text = await self._look()
        still = [f for f in after.get("fields") or [] if f.get("kind") == "password" and not named]
        if not pressed or (len(still) >= 2 and _RESET_PAGE.search(after_text[:2000]) and after.get("errors")):
            await self._bring_forward(run)
            said = f" It says \u201c{after['errors'][0][:160]}\u201d." if after.get("errors") else ""
            self._pause(run, "sign_in", f"I put your saved password in {site}'s new-password boxes, and it didn't take it "
                        f"as the new one.{said} Set it there (or another, and save that one on the desk), then sign in; "
                        "the desk carries on after that.", seen=after)
            return "paused"
        run.resetting = False
        self._log(run, f"set your saved password as {site}'s new one")
        return "set"

    async def _theirs_to_send(self, run: Run, data: dict[str, Any], button: dict[str, Any]) -> None:
        srv = self.srv
        srv._mark_ready(srv.tracker().get(run.job_id), "filled by the Job Desk; its own button sends it")
        await self._bring_forward(run)
        self._pause(run, "your_submit", f"Filled as far as the desk can. On {_site(run, data)} the button that sends "
                    f"the application reads \u201c{button['text'].strip()}\u201d, so it's yours to press: check the "
                    "page in the browser, press it there, then press \u201cI submitted it\u201d here.")

    async def _sign_up_pause(self, run: Run, data: dict[str, Any], button: dict[str, Any]) -> None:
        await self._bring_forward(run)
        self._pause(run, "stuck", f"{_site(run, data)} asks for your name and email first, and its "
                    f"\u201c{button['text'].strip()}\u201d sends them before the application itself, so it's yours "
                    "to press: press it in the browser window if you're happy to, then press Resume.")

    async def _decline_cookies(self, run: Run, data: dict[str, Any], text: str) -> bool | None:
        """Press Reject / Decline / Necessary on a cookie banner. Banners cover forms and catch
        clicks. One with no way to decline (TI's "Manage Preferences" or "Agree and Proceed") is
        accepted only where the person set settings.accept_cookies; otherwise, where it covers the
        page, it's theirs, and False says so (once a site). True: the banner was answered. None:
        nothing to do (a bar along the edge is left be).
        A banner is known by its text, or by its buttons sitting in a cookie/consent box: on
        a long page the banner comes after the part of the text that's read."""
        mentioned = "cookie" in text.lower()
        actions = [a for a in data.get("actions") or [] if not a.get("disabled")]
        button = next((a for a in actions if declines_cookies(a.get("text", ""), bool(a.get("cookie")))
                       and (mentioned or a.get("cookie"))), None)
        if button is not None:
            result = await self.srv.click(button["id"])
            if result.get("clicked"):
                self._log(run, f"declined cookies (\u201c{button['text']}\u201d)")
                return True
            return None
        accept = next((a for a in actions if a.get("cookie") and _accepts_cookies(a["text"], a["text"].strip(), True)), None)
        if accept is None:
            return None
        banner = await self.srv.browser.element_info(accept["id"])
        if not banner or not banner.get("cookie"):  # gone, or an application's own consent box
            return None
        if may_accept_cookies(banner):
            result = await self.srv.click(accept["id"])  # (its guard checks the banner again)
            if result.get("clicked"):
                self._log(run, f"accepted cookies (\u201c{accept['text'].strip()}\u201d), as your settings allow: "
                               "the banner had no way to decline")
                return True
            return None
        host = urlparse(data.get("url") or "").hostname or ""
        if not banner.get("cookieBlocking") or host in run.cookies_asked:
            return None  # out of the way, or asked once and the person pressed Resume with it there
        run.cookies_asked.add(host)
        return False

    async def _answer_notice(self, run: Run, data: dict[str, Any], notice: dict[str, Any], agreed: set[str],
                             over_form: bool) -> str | None:
        """A dialog open over the page that nothing else answers (_notice). An employer's notice about
        AI screening of the application is agreed to for the person where they allow it
        (settings.accept_notices; never in practice mode), once a pass, and said in the log: "agreed".
        Any other one over a form is theirs, as the boxes behind it don't fill: "paused". None:
        nothing done (on a page with nothing to fill, its buttons are the way on as before)."""
        heading = _notice_name(notice)
        said = f"{heading} {notice.get('text') or ''}"
        ai = bool(_AI_NOTICE.search(said) and _ABOUT_APPLYING.search(said))
        agree = next((b for b in notice.get("buttons") or [] if not b.get("disabled")
                      and _AGREES.match(final_text(b.get("text") or ""))), None)
        allowed = ai and agree is not None and _may_accept_notices()
        if allowed and agree is not None and heading not in agreed:
            try:
                clicked = await self.srv.click(agree["id"])
            except Exception:  # gone, or it won't take a click: the person's, as without the setting
                clicked = {"clicked": False}
            if clicked.get("clicked"):
                agreed.add(heading)
                whose = f"{run.company}'s" if run.company else "the site's"
                self._log(run, f"agreed to {whose} notice “{_short(heading)}” for you (settings.accept_notices)")
                await self._notice_closed(heading)
                return "agreed"
        if not over_form:
            return None
        await self._bring_forward(run)
        self._pause(run, "stuck", f"{_site(run, data)} shows a notice over the form that only you can answer: "
                    f"“{_short(heading)}”. Read it and press its button in the browser window, then press "
                    "Resume; the desk fills the form in after that." + (
                        " (The desk agrees to an employer's notice about AI screening for you with accept_notices: true "
                        "under settings: in profile.yaml, outside practice mode.)" if ai and agree is not None
                        and not allowed else ""))
        return "paused"

    async def _notice_closed(self, heading: str) -> None:
        """After a notice's agree button: until the notice has gone. One fading out is read a
        moment longer, and looked at that soon, it would be taken for one that stayed."""
        deadline = time.monotonic() + NOTICE_WAIT
        while time.monotonic() < deadline:
            data = await self.srv.inspect_form(include_dropdown_options=False)
            if not any(_notice_name(d) == heading for d in data.get("dialogs") or []):
                return
            await asyncio.sleep(0.5)

    async def _attest(self, run: Run, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """settings.accept_notices: an application's attestation that its information is true and
        complete, or its consent to the background check that comes with applying (_attestation),
        is picked for the person and said in the log. Its answers come only from the profile and
        the person, so it is true; questions still open are theirs to answer before it's sent.
        Returns the questions left."""
        attest = {q["id"]: value for q in pending if (value := _attestation(q)) is not None}
        if not attest or not _may_accept_notices():
            return pending
        out = await self.srv.fill_form([{"id": fid, "value": value} for fid, value in attest.items()])
        done = {r["id"] for r in out.get("results") or [] if r.get("ok")}
        for q in pending:
            if q["id"] in done:
                what = "ticked" if attest[q["id"]] is True else f"picked “{attest[q['id']]}” for"
                self._log(run, f"{what} “{_short(q.get('label') or '')}” for you (settings.accept_notices)")
        return [q for q in pending if q["id"] not in done]

    async def _sign_in(self, run: Run, data: dict[str, Any], tried: dict[str, int], details: bool = True,
                       reset_first: bool = False) -> str | None:
        """With the profile email and a stored <ats>_password (if the person saved one):

        - "email_step": pressed Workday's "Sign in with email" to reach the form;
        - "submitted": filled in the sign-in form and pressed its button (once a pass);
        - "create_account": that didn't get in, most likely because there's no account
          there yet, so it opened the site's Create Account form;
        - "prefilled": filled in a Create Account form, leaving its terms and button to
          the person (or to _make_account, with settings.manage_accounts);
        - "filled": filled in a sign-in form whose button it doesn't recognise;
        - "refused": the saved password didn't sign in, and there's no Create Account to try (or,
          with `reset_first`, the page has a way to a password reset, tried before a new account;
          or a Create Account pressed for this job already didn't make one).

        None when there's nothing (more) to do. `tried` counts what this pass already did, and the
        run what the whole job did (Run.sign_in_tries: never more than SIGN_IN_TRIES with one saved
        password); `details=False` leaves a Create Account form's other boxes as they are."""
        srv = self.srv
        secret = password_for(data["url"])
        saved = _secret(secret) if secret is not None else None
        if secret is None or saved is None:
            return None
        key = hashlib.sha256(saved.encode()).hexdigest()
        if key != run.sign_in_key:  # a password saved since: not the one the site refused
            run.sign_in_key, run.sign_in_tries = key, 0
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
        if len(passwords) == 1 and (tried.get("submitted") or run.sign_in_tries >= SIGN_IN_TRIES) and not signing_up:
            # Signed in once already and still asked to: the password didn't get in. Trying it
            # again won't help (and can lock an account: the job's sign-ins are counted across
            # Resume and the queue); a first visit needs an account. The way there gets a second
            # press: Amkor's sign-in page reloads after a failed sign-in, and a click on its "Create
            # an account" made before that has finished is lost. A Create Account the desk pressed
            # for this job already didn't make one (a Workday site goes back to its Sign In either
            # way): never a second, the password is reset instead.
            if (create is None or tried.get("create_account", 0) >= 2 or run.accounts_tried
                    or reset_first and _forgot_action(actions)):
                return "refused"
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
        if new_account:  # its terms and button: the person's, unless they let the desk (_make_account)
            self._log(run, "filled the Create Account form with your details and saved password")
            return "prefilled"
        # The form's own button, after its password box: a "Sign In" in the site's header opens
        # its sign-in page or pop-up instead, and sends nothing (Workday's). Failing that, the
        # button of a form that holds only the sign-in, whatever it reads (SuccessFactors' "Submit").
        own = [a for a in actions if a.get("after_password") and not _SOCIAL.search(a["text"]) and not a.get("cookie")]
        button = next((a for a in own if _SIGN_IN_ACTION.match(a["text"].strip())), None) or next(
            (a for a in own if a.get("sign_in_form") and _SIGN_IN_SUBMIT.match(final_text(a["text"]).strip())), None)
        if button is None:
            return "filled"
        await srv.click(button["id"])
        run.sign_in_tries += 1
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
        pending = await self._attest(run, pending)
        data, _ = await self._look()
        run.page_info = _page_info(data)  # the page as filled
        gaps, pending = self._entry_gaps(run, data, pending)
        unfilled, pending = _unfilled(pending)
        couldnt = f" {_couldnt_fill(unfilled)}" if unfilled else ""
        if missing_files:
            return self._pause(run, "stuck", "The form needs a file the profile doesn't point to (set documents.resume "
                               "in profile.yaml): " + ", ".join(f["label"] for f in missing_files)
                               + (f". {gaps}" if gaps else "") + couldnt, pending)
        if gaps:
            return self._pause(run, "stuck", gaps + (f" It also has {len(pending)} other question(s), here."
                                                     if pending else "") + couldnt, pending)
        if pending:
            return self._pause(run, "questions", f"{len(pending)} question(s) your profile doesn't answer. Answer them "
                               "here and the desk fills them in (and remembers them)." + couldnt, pending)
        if unfilled:
            return self._pause(run, "stuck", f"{couldnt.strip()} Fill {'it' if len(unfilled) == 1 else 'them'} in the "
                               "browser, then press Resume (or press Resume for the desk to try again).")
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
    def turned_down(f: dict[str, Any]) -> dict[str, Any]:  # an answer of theirs for the box, or its label
        return once_failed.get(placed_key(f)) or once_failed.get(question_key(f.get("label") or "")) or {}

    def unoffered(f: dict[str, Any]) -> dict[str, Any]:  # why the profile's answer didn't go in
        return {"error": f"your profile's answer “{f['unmatched']}” isn't one of its choices"} \
            if f.get("unmatched") else {}

    pending = [{**f, **unoffered(f), **turned_down(f)} for f in result["needs_input"]
               if f.get("required") and f.get("kind") != "file"]
    # the profile's answers that didn't go in, where the site requires one (an optional field
    # is skipped: Qorvo's optional veteran question has no "don't wish to answer")
    pending += [{"id": f["id"], "label": f.get("label") or "", "kind": f.get("kind") or ("combobox" if f.get("options") else "text"),
                 "required": True, "error": f.get("error"),
                 **({"options": f["options"]} if f.get("options") else {}),
                 **{k: f[k] for k in ("section", "sublabel") if f.get(k)}}
                for f in result["failed"] if f.get("required", True)]
    # an answer of theirs the page turned down is asked again, even when the box isn't empty
    # (words left in a picker's search box read as an answer), unless the profile's answer
    # went in after it
    asked = ({question_key(f.get("label") or "") for f in pending + (result.get("filled") or [])}
             | {placed_key(f) for f in pending})
    pending += [f for key, f in once_failed.items() if key not in asked and f.get("kind") != "file"]
    missing_files = [f for f in result["needs_input"] if f.get("required") and f.get("kind") == "file"]
    return pending, missing_files


def _unfilled(pending: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The boxes the page never let the desk fill (_FILL_BROKE), whose answer the profile or the
    person gave, and the questions left for the person."""
    broke = [q for q in pending if _FILL_BROKE.match(str(q.get("error") or ""))]
    return broke, [q for q in pending if not _FILL_BROKE.match(str(q.get("error") or ""))]


def _couldnt_fill(unfilled: list[dict[str, Any]]) -> str:
    """Boxes the page didn't let the desk fill, said as that: not questions the profile doesn't
    answer (Eightfold's Country of Residence timed out behind a dialog, its error on the card)."""
    names = ", ".join(f"“{_short(q.get('label') or 'a box')}”" for q in unfilled[:3])
    late = all(str(q.get("error") or "").startswith("TimeoutError") for q in unfilled)
    return (f"I couldn't fill {names}{' and more' if len(unfilled) > 3 else ''}"
            + (" (the page didn't respond in time)." if late else "."))


def _notice(data: dict[str, Any]) -> dict[str, Any] | None:
    """A dialog open over the page (formjs' openDialogs) that the desk answers no other way: not a
    cookie banner (declined, or the person's, by its own rule and setting), nor one whose buttons
    are a way on the desk knows (Workday's "Start Your Application" with its Apply Manually, its
    "Sign in with email", a note's Dismiss)."""
    for d in data.get("dialogs") or []:
        texts = [final_text(b.get("text") or "") for b in d.get("buttons") or [] if not b.get("disabled")]
        if not d.get("cookie") and not any(_ENTRY.match(t) or _DISMISS_NOTE.match(t) or re.match(r"^sign in with ", t, re.I)
                                           for t in texts):
            return d
    return None


def _notice_name(notice: dict[str, Any]) -> str:
    """A dialog's heading, or its first words."""
    return notice.get("heading") or _short(notice.get("text") or "") or "a dialog"


def _attestation(q: dict[str, Any]) -> Any:
    """What attests to an application's attestation, a required box whose label certifies its
    information true and complete, or consents to the background check that comes with applying
    (Eightfold's "Terms and Conditions" dropdown, its one choice certifying the application is
    correct): a tick, or its one real choice where that agrees. None for any other box."""
    label = q.get("label") or ""
    if (not q.get("required") or q.get("error") or q.get("kind") not in _ATTEST_KINDS
            or not _ATTESTS.search(label) or _NOT_ATTESTED.search(label)):
        return None
    if q["kind"] == "checkbox":
        return True
    choices = [o for o in q.get("options") or [] if not is_empty_value(o)]
    return choices[0] if len(choices) == 1 and polarity(choices[0]) is not False else None


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


_CONTACT_FIELD = re.compile(r"^((first|last|full|given|family|legal) )?name$|^e ?mail( address)?$|^(mobile |cell )?phone"
                            r"( number)?$|^(zip|postal)( code)?$")


def _sign_up_box(data: dict[str, Any], submits: list[dict[str, Any]]) -> bool:
    """A box taking only a name and email (a phone, a ZIP) whose one button is an "Apply": the
    way into an application that sends them on first (the State of Arizona's "Apply Now"), not
    a filled application. An application asks for more, a resume at least."""
    fields = data.get("fields") or []
    return (0 < len(fields) <= 4 and len(submits) == 1 and bool(_ENTRY.match(final_text(submits[0]["text"])))
            and all(f.get("kind") != "file" and _CONTACT_FIELD.match(norm(clean_label(f.get("label") or "")))
                    for f in fields))


def _forgot_action(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A sign-in page's way to a password reset ("Forgot your password?")."""
    return next((a for a in actions if not a.get("disabled") and not a.get("cookie") and _FORGOT.search(a["text"])), None)


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


def _may_manage_accounts() -> bool:
    try:
        return config.Profile.load().settings.may_manage_accounts
    except ValueError:  # a profile with a typo allows nothing
        return False


def _may_accept_notices() -> bool:
    try:
        return config.Profile.load().settings.may_accept_notices
    except ValueError:  # a profile with a typo allows nothing
        return False


def _secret(name: str) -> str | None:
    try:
        return config.get_secret(name)
    except Exception:  # a hand-edited secrets.yaml with a typo: sign in by hand rather than fail the job
        return None


def _empty_required(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in data.get("fields") or [] if f.get("required") and not f.get("disabled")
            and f.get("kind") != "password" and is_empty_value(f.get("value"))]


def _mid_application(data: dict[str, Any]) -> bool:
    """A page partway through the form, whatever its words: a required box still empty, a button on
    to the next step, or a progress bar short of its last step (Workday's "current step 3 of 6")."""
    if _empty_required(data) or any(_FORWARD.match(final_text(a.get("text") or "")) and not a.get("disabled")
                                    for a in data.get("actions") or []):
        return True
    return any(int(m[1]) < int(m[2]) for h in data.get("headings") or [] for m in _STEP_OF.finditer(h))


def _new_required(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Did filling the page bring up required fields that weren't there before?"""
    seen = {f.get("id") for f in before.get("fields") or []}
    return any(f.get("id") not in seen for f in _empty_required(after))


def _bare(url: str) -> str:
    """An address without its query and fragment."""
    return urlparse(url)._replace(query="", fragment="").geturl()


# The query keys that name a job in an address: Eightfold's pid, Taleo's job, SuccessFactors'
# career_job_req_id, Greenhouse's gh_jid, and the usual jobId / reqId
_JOB_ID_KEY = re.compile(r"pid|job|jid|gh_jid|job_?id|req_?id|job_?req_?id|requisition_?id|career_job_req_id", re.I)


def _job_ids(url: str) -> dict[str, str]:
    """The job ids an address carries in its query, by key (lower case)."""
    return {k.lower(): v for k, v in parse_qsl(urlparse(url).query) if _JOB_ID_KEY.fullmatch(k) and v}


def _above(url: str, below: str) -> bool:
    """Is `url` a page above `below` on the same site: "/careers" above "/careers/job/12", the
    front page above any other?"""
    a, b = urlparse(url), urlparse(below)
    if not a.hostname or a.hostname.lower() != (b.hostname or "").lower():
        return False
    return b.path.rstrip("/").startswith(a.path.rstrip("/") + "/")


def _account_step(data: dict[str, Any]) -> bool:
    """A page about making an account, with nothing to fill and a button that makes one: not a
    page that only has a "Register" link in its header."""
    about = " ".join([data.get("title") or "", *(data.get("headings") or [])])
    return (not data.get("fields") and bool(_ACCOUNT_PAGE.search(about))
            and any(_CREATE_ACCOUNT.match(final_text(a["text"])) and not a.get("disabled")
                    for a in data.get("actions") or []))


def _unanswered(data: dict[str, Any]) -> str:
    """What a page whose Next is greyed out, and that says nothing itself, still waits on: its
    required boxes still empty, by name ("I Agree", which is the person's), or else a file."""
    fields = [f for f in data.get("fields") or [] if not f.get("disabled") and is_empty_value(f.get("value"))]
    left = [clean_label(f["label"]) for f in fields if f.get("required") and f.get("label") and f.get("kind") != "file"]
    left = [label if len(label) <= 80 else label[:77].rsplit(" ", 1)[0] + "\u2026" for label in left if label]
    if left:
        names = ", ".join(f"\u201c{label}\u201d" for label in left[:3]) + (" and more" if len(left) > 3 else "")
        return names + (" isn't answered." if len(left) == 1 else " aren't answered.")
    if any(f.get("kind") == "file" for f in fields):  # Phoenix Children's waits on a resume, its box not marked required
        return "nothing is attached yet (your resume, say)."
    return ""


def _account_wants(left: dict[str, Any], filled: dict[str, Any]) -> str:
    """What a Create Account form that didn't go through says it wants: as the site `left` it, its
    error or alert text and what it marks (Workday's "Error-Email" links, boxes marked invalid); or
    else, as `filled` in again, its boxes still empty that it requires or that agree to its terms, by
    name. "" when it shows nothing."""
    errors = left.get("errors") or []
    said = list(dict.fromkeys(" ".join(e.split()) for e in errors if not _ERROR_COUNT.match(e.strip())))
    marked = [m for m in _flagged(left) if m not in errors]
    if said or marked:
        return "; ".join(([f"it says \u201c{'; '.join(said)[:300].rstrip(' .')}\u201d"] if said else []) + marked) + "."
    empty = [f for f in filled.get("fields") or [] if f.get("label") and not f.get("disabled")
             and f.get("kind") not in ("password", "file") and is_empty_value(f.get("value"))
             and (f.get("required") or f.get("kind") == "checkbox" and _TERMS_BOX.search(f["label"])
                  and not _NOT_TERMS.search(f["label"]))]
    named = [f"\u201c{_short(f['label'])}\u201d " + ("isn't ticked" if f.get("kind") == "checkbox" else "is still empty")
             for f in empty[:3]]
    return "; ".join(named) + "." if named else ""


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


def _maintenance_page(data: dict[str, Any]) -> bool:
    """A site down for maintenance: the job sent on to a page named for it (Workday's
    community.workday.com/maintenance-page, asml.com/en/maintenance's "We'll be back.", live, Oct 2026)."""
    return bool(re.fullmatch(r"maintenance(?:-page)?|outage", urlparse(data.get("url") or "").path.rstrip("/")
                             .rsplit("/", 1)[-1], re.I))


def _closed_notice(data: dict[str, Any], text: str) -> str:
    """The page's own words that its posting has closed, or "" when it doesn't say so."""
    for said in list(data.get("errors") or []) + [text or ""]:
        if m := _CLOSED.search(said):
            start, end = said.rfind(".", 0, m.start()) + 1, said.find(".", m.end())
            return " ".join(said[start:end + 1 if end >= 0 else len(said)].split())[:200]
    return ""


def _site(run: Run, data: dict[str, Any]) -> str:
    ats = detect_ats(data.get("url") or "")
    if ats in ("company_site", ""):
        return f"{run.company}'s site" if run.company else "the site"
    return ATS_NAMES.get(ats, ats)


def question_key(label: str) -> str:
    return norm(clean_label(label))


def placed_key(field: dict[str, Any]) -> str:
    """The key of an answer given for one box: the desk sends "section | label | sub-label"."""
    return question_key(f"{field.get('section') or ''} | {field.get('label') or ''} | {field.get('sublabel') or ''}")


def _page_said(data: dict[str, Any]) -> str:
    """A page in a few words: its heading, and what it says if it shows a message."""
    message = next((e for e in data.get("errors") or [] if len(e) > 12), "")
    return _where(data) + (f" (\u201c{message[:160]}\u201d)" if message else "")


def _where(data: dict[str, Any]) -> str:
    headings = [h for h in data.get("headings") or [] if h and not re.match(r"current step", h, re.I)]
    return f"“{headings[-1][:60]}”" if headings else "this page"


def _short(label: str) -> str:
    """A question in a few words for the log: Micron's veteran question carries the whole
    VEVRAA notice in its label."""
    text = clean_label(label)
    return text if len(text) <= 80 else text[:78].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
