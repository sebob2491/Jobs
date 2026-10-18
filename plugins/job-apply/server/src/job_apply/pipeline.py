"""One-button applying: carry each queued job from its posting to the review page.

The pipeline drives the same tools Claude uses (open_application, autofill, click,
add_entries, submit_application), so every guard still holds: final submit buttons are
only pressed through submit_application, LinkedIn and Indeed are left for the person to
submit, and nothing is answered that the profile or the person's saved answers don't
cover. Each job gets its own browser tab, and up to settings.jobs_at_once are filled at
once (Applier says what stays one at a time).

Whatever needs the person pauses that job with a plain reason. Questions wait in the
Job Desk while other jobs carry on. A bot check, a sign-in or an emailed code needs
their hands in the browser window, so the queue holds there (that tab is brought to the
front) and carries on by itself once the page moves past it, or when they choose
"later".
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from . import config, history, mailbox, report
from .search import every_employer
from .ats import ATS_NAMES, JOB_BOARDS, detect_ats, shared_system
from .autofill import (classes_only, clean_label, degree_kinds_listed, degree_not_listed, entry_list_unloaded, entry_of,
                       _CHOICE_KINDS, is_empty_value, kept_documents, no_choice_for_no_degree, norm,
                       plan_autofill, profile_entries, site_default, unset_choice,
                       polarity, tailored_document, works_there_now)
from .browser import (_SENDS_TOO, CONFIRMATION_RE, SubmitBlocked, TabClosed, _accepts_cookies, _cookie_setting,
                      confirmations, declines_cookies, final_text, may_accept_cookies, questions_shown)
from .never import (ANYWHERE, CALLS_OR_TEXTS, EMAILS, FUTURE_ROLES, MAIL, NEVER, PARAGRAPH_WORDS, PRECISE,
                    _never_for_them)

NEW_TAB_WAIT = 4  # seconds to wait for a tab opened late by a click before calling it a stall
ONCE_SETTLE = 1.0  # seconds after filling the person's answers before checking they stayed in
MAX_STEPS = 30  # passes over a job's pages (Avnet's Workday, run 53: an account and seven steps took 15)
# seconds a pass over a job's pages may take before it stops where it is: its waits (a page's buttons, a step saving, a
# list's choices) add up, and one job held the queue for as long as they took (the review of Oct 15)
PASS_SECONDS = 25 * 60
LATE_BUTTONS_WAIT = 10  # seconds for a page's buttons to be drawn
SIGN_IN_SETTLE = 10  # seconds a sign-in sent, its form still on show, may take before it counts as refused
REFUSAL_SETTLE = 2  # seconds a refused sign-in's alert may still take to show once it counts as refused (Workday's)
RESET_SETTLE = 15  # the same for a new password sent: its reset form still on show, no error yet (Workday's)
STEP_SAVE_WAIT = 20  # for a step pressed three times that's still saving (KLA's Workday), before it counts as stuck
STEP_LOAD_WAIT = 30  # for a step still loading its questions, its loading dots on show (KLA's Workday)
INDICATOR_GONE_SETTLE = 3  # seconds more, the indicator gone and nothing to press yet, for the step's button to follow
BLANK_PAGE_WAIT = 30  # for a page with nothing on it yet to draw (Infineon's application, now and then)
# times a page a press led to, with nothing drawn on it after the wait, is loaded again (on a pass at the job) before the
# person is asked to (Eightfold's apply page after its Apply Now came up blank white, and drew its form once the person
# reloaded it: issue #112, note 40)
BLANK_RELOADS = 2
SIGN_IN_STEP_WAIT = 25  # Workday's sign-in step can take longer to draw its buttons (Applied's)
LEFT_SIGN_IN_SETTLE = 3  # seconds before a sign-in page that led elsewhere is taken for one with no sign-in
ACCOUNT_DRAW_WAIT = 4  # for the application below a Create Account form to be drawn (Qorvo's)
HANDS_ON = {"sign_in", "email_code"}
# The stops after which a saved draft's answers are still re-checked (Run.draft_off): what the person answers without
# the application's own boxes (a questions card, an emailed code, a sign-in, a bot check). After any other, or a Resume,
# what they change in the tab is theirs
_DRAFT_GOES_ON = {"questions", "email_code", "sign_in", "bot_check"}
# What the desk's rounds take the person to, one tab at a time, starting no other job meanwhile: bot checks to solve,
# and applications filled to the step the person sends (a ready one's review page is "ready", not a need; one with a
# CAPTCHA before its Submit is "captcha": the person ticks it, then sends it)
ROUND_NEEDS = {"bot_check", "captcha", "your_submit", "check_submit", "submit_failed"}
# A bot check never holds the queue (the owner's call, Oct 11): its job waits in its tab, watched, while the other
# jobs go on, and the person solves the checks waiting in a round of their own from the desk ("Bot checks"); each
# job carries on by itself once its tab is past the check. The desk never solves one.
# A pause that needs the person (a sign-in, an emailed code) holds the queue while they're at it. Nothing done in its tab
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
# A tab waiting on the person that hasn't changed (nor paused) for STILL_SECONDS is looked at this often instead: each
# look reads every frame of it, every few seconds for up to 45 minutes (the review of Oct 15)
SLOW_POLL_SECONDS = 10.0
STILL_SECONDS = 60
# With an email app password saved, a job waiting on an emailed code or link has the inbox
# looked at this often, for this long after it began waiting
MAIL_POLL_SECONDS = 20
MAIL_WINDOW = 15 * 60
# seconds with nothing done in a tab holding the queue before the person's press on another job lets that one go
RELEASE_IDLE = 60
DESK_SETTLE = 30  # seconds a paused tab may go on changing after the desk typed its emailed code or opened its link
# A password reset asked for with the inbox watched: when no email has come by then, the email most
# likely has no account there (most sites won't say), so the desk makes one instead
RESET_MAIL_WAIT = 3 * 60
# A reset email from before the desk asked for the reset, by this computer's clock: an earlier reset's, its link spent
# (KLA's Workday, the reset probe's second pass, run 136), or this one's with the two clocks apart (the mail service's
# INTERNALDATE against this computer's time). A newer one is waited for this long after the reset was asked for, and
# that one is used only if none comes
RESET_FRESH_WAIT = 30
# ...but only within this much of the request: one from further back is an earlier reset's, whatever the clocks say,
# never opened (the review of 0.3.209: after the fresh wait, run 136's spent email was opened all the same). Its link was
# the account's, so its wait running out is said on a card (Run.reset_spent), never taken for no email
RESET_CLOCK_SKEW = 45
# How long a site may take over a new account before the page is looked at again (a Workday site goes on
# to its Sign In page a few seconds after Create Account is pressed)
ACCOUNT_WAIT = 15
ACCOUNT_STEP_WAIT = 10  # for an email-first site's next step of making an account to draw (Eightfold's)
# A site locks an account after a few refused sign-ins: a job's sign-in is pressed with the same saved
# password this many times at most (once more after a password reset or an account the desk made, and
# after each Resume the person presses on a sign-in card)
SIGN_IN_TRIES = 2
NOTICE_WAIT = 5  # seconds for a notice agreed to for the person to go (one may fade out)
# A job that went in keeps its tab (its confirmation, for the person to see) while it's among the newest
# this many: one tab per job adds up over a long queue
DONE_TABS_KEPT = 3
ABOUT_SAID = 400  # characters of the words above a box that says little, on its question card (their end)
ABOUT_LOOKS = 6  # and how many such boxes on one card are looked at (one gone since takes a few seconds)
SHARED_LOOK_BACK = 30  # seconds looked back for a job's code while an earlier job waits on the same sender
# A job's "What went in" panel on its desk card (the desk page alone): at most this many of its answers, each shown this
# long at most (a long text area's start), and at most this many lines of what was agreed to, accounts made and
# questions left empty, so the page's state stays small with 30 jobs on it (a Workday application with four jobs and
# two schools fills about 100 boxes)
WENT_IN_ANSWERS = 150
WENT_IN_CHARS = 200
WENT_IN_LINES = 30
# The codes the inbox gave, each with the job it went to: one is never another job's (Mayo Clinic's
# Oracle site, run 49, was given onsemi's code, mailed from the same Oracle address a minute before and put in
# there). And the password reset links it gave, each opened once: a reset's link works once, and one opened again
# sets nothing (KLA's Workday, the reset probe's second pass, run 136). For the whole process: the live check gives
# each employer a desk of its own.
_MAIL_TAKEN: dict[tuple[str, str, float], int] = {}


def _take_code(mail: tuple[str, str, float], job_id: int) -> None:
    """A code the inbox gave (or a reset link opened), noted as the job's it went to. A day's are kept: one is looked
    for within minutes of its mail, and the map grew for as long as the desk ran (the review of Oct 15)."""
    for old in [k for k in _MAIL_TAKEN if k[2] < time.time() - 86400]:
        del _MAIL_TAKEN[old]
    _MAIL_TAKEN[mail] = job_id


def _link_opened(link: str) -> bool:
    """A password reset's link opened already, for any job: spent."""
    return any(k[1] == link for k in list(_MAIL_TAKEN))


FINISHED = {"applied", "interviewing", "offer", "rejected", "withdrawn"}  # tracker statuses never applied to again
BEGUN = {"in_progress", "ready_to_submit"}  # tracker statuses of a job begun and not sent in yet
UNSKIP_WAIT = 10  # seconds an Undo waits for the worker to finish the step a job was skipped in
# The Job Desk's queue, kept in the person's own folder as it changes so a restart of the desk (a Claude app restart, a
# crash, a plugin update) picks it up again (the review of Oct 15): each job's number in its place in line, whether it
# was queued (never begun), under way or waiting on the person, whether Submit for me was on for it and whether a Submit
# was pressed for it; and whether the queue was paused. Never an answer, a code, a password or a page's words
# (Applier._saved_queue). One desk keeps it at a time (QUEUE_LOCK: two Claude sessions share the folder)
QUEUE_FILE = "desk-queue.json"
QUEUE_LOCK = "desk-queue.lock"
# A kept queue older than this (days) is said with its age as it comes back (paused, as any)
QUEUE_STALE_DAYS = 7
# said on the review card of a job whose Submit the person pressed before the desk restarted (never carried over)
SUBMIT_UNSENT = "Your Submit from before the desk restarted wasn't sent: press Submit again to send it."
# on the card of a job begun before the desk restarted (under way, or waiting on the person): it isn't queued again by
# itself, as the person may have worked in its tab (sent it on the site, say). Resume fills it again, never sending it
RESTART_BEGUN = ("The desk restarted before this application was finished. If you sent it yourself on the site, press I "
                 "submitted it already. Otherwise Resume fills it again up to its review page (nothing is sent), or Skip "
                 "job.")
# and on the card of one whose Submit was pressed (or being pressed) then: it may have gone, so it isn't queued again
RESTART_CHECK = ("Submit was pressed for this job before the desk restarted, and it may have gone in, so the desk "
                 "didn't queue it again. Check your email or the site: if it went in, press I submitted it; if it "
                 "didn't, press Resume and it's filled again up to its review page (nothing is sent).")
# said on a desk that another one (another Claude session's) keeps the queue for (looked at again about this often,
# seconds: once that one has ended, this one keeps it, and picks up what it kept), or that can't keep one at all
QUEUE_ELSEWHERE = ("Another Job Desk is running (another Claude Code session's), and the queue is kept there: this "
                   "one's queue isn't picked up again after a restart.")
QUEUE_LOCK_RETRY = 60
QUEUE_NO_LOCK = "The desk can't keep its queue across a restart here (its folder doesn't allow a lock)."


def queue_path() -> Path:
    return config.home() / QUEUE_FILE


# The queue lock this process holds, by its file: the open file (the operating system's lock on it goes with the
# process, however it ends) and how many of this process's desks keep the queue with it (one in the app; a test's few)
_QUEUE_LOCKS: dict[str, list[Any]] = {}


def _take_queue_lock() -> str:
    """Keep the desk's queue in this process: "mine"; "theirs" while another process's desk keeps it (its lock held);
    or "none" when the lock can't be had here at all (a folder that can't be written, or a file system with no locks)."""
    path = config.home() / QUEUE_LOCK
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        key = str(path.resolve())
        held = _QUEUE_LOCKS.get(key)
        if held is not None:
            held[1] += 1
            return "mine"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    except OSError:
        return "none"
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        # held by another (EWOULDBLOCK; Windows' lock violation, as EACCES), or no locks to be had (ENOLCK, ENOTSUP)
        held_errors = ({errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)} if sys.platform == "win32"
                       else {errno.EAGAIN, errno.EWOULDBLOCK})
        return "theirs" if e.errno in held_errors else "none"
    with contextlib.suppress(OSError):  # (which process holds it, for anyone looking)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
    _QUEUE_LOCKS[key] = [fd, 1]
    return "mine"


def _give_back_queue_lock() -> None:
    key = str((config.home() / QUEUE_LOCK).resolve())
    held = _QUEUE_LOCKS.get(key)
    if held is None:
        return
    held[1] -= 1
    if held[1] <= 0:
        del _QUEUE_LOCKS[key]
        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                import msvcrt
                os.lseek(held[0], 0, os.SEEK_SET)
                msvcrt.locking(held[0], msvcrt.LK_UNLCK, 1)
        with contextlib.suppress(OSError):
            os.close(held[0])

_BOT_TITLE = re.compile(r"just a moment|attention required|access denied|pardon our interruption|security check|"
                        r"are you a robot|bot (?:check|detection)", re.I)
# A bare "403 Forbidden" (Valleywise Health's postings, to the desk's browser) or "406 Not Acceptable"
# (Deloitte's sign-in, to a headless browser): the site turns the browser away. There's nothing to
# solve, so it holds nothing up: the person applies elsewhere
_RESET_DONE = re.compile(r"\b(?:your )?password (?:has been|was|is)(?: successfully)? (?:reset|changed|updated)\b|"
                         r"\bpassword (?:reset|change|changed|updated?) (?:was |is )?(?:a )?success", re.I)
# A reset's page saying it set nothing (opened from its email, or once its button is pressed). Its link spent: "This link
# has expired", "The password reset link is invalid or has expired", "This reset link is no longer active", "The
# password reset has timed out", "The link you clicked has already been used", "Invalid Password Reset Request". Or the
# password one the site had before: "Password was recently used", "You can't reuse a recent password", "Password cannot
# be the same as your previous 5 passwords". The usual words (none of Workday's is on record: KLA's, the reset probe's
# second pass, run 136, opened a spent link, saw its form go and said the password was set), and only said of a link, a
# token, a reset or a password: never "Your session has expired. Please sign in again." after a reset that went in, nor
# a posting's "non-expired driver's license" beside a sign-in
_LINK_SPENT = re.compile(r"\b(?:link|token|reset|url)s?\b[^.!?\n]{0,40}?\b(?:expired|no longer (?:valid|active)|"
                         r"(?:already )?been used|already used|invalid|not valid|timed out)\b|"
                         r"\b(?:expired|invalid)(?: or expired)? (?:(?:password )?reset (?:link|token|request)|link|token|"
                         r"url)\b|\b(?:this|your|the) (?:password )?(?:reset )?request (?:has )?expired\b|"
                         r"\bpassword reset failed\b", re.I)
# ...unless said only of what may be, or what to do if it were
_ONLY_IF = re.compile(r"^\W*(?:if|in case|should|when)\b|\b(?:may|might|could) be\b", re.I)
_PASSWORD_USED = re.compile(r"\bpasswords?\b[^.!?\n]{0,40}?\b(?:(?:recently|previously) used|used (?:recently|before|"
                            r"previously)|(?:already )?been used|can(?:not|'t|\u2019t| not) be re-?used|"
                            r"same as (?:your |the |a |any of your )?(?:current|old|previous|last|recent)|"
                            r"different from (?:your |the )?(?:current|old|previous)|(?:last|previous|recent) (?:\d+ )?"
                            r"passwords)\b|\bcan(?:not|'t|\u2019t| not) (?:re-?use|use a password)\b[^.!?\n]{0,40}?"
                            r"\b(?:password|used)\b", re.I)
# The site's password rules, shown beside its boxes ("Passwords must not match your last 5 passwords"): no refusal
# unless an alert says them
_PASSWORD_RULES = re.compile(r"\bpasswords (?:must|should|may|cannot|can't)\b|\b(?:must|should) (?:not )?(?:contain|"
                             r"include|match|have|be at least)\b|\bat least \d", re.I)
_STEPISH = re.compile(r"\b(next|continue|save|submit|review|apply|finish|done|proceed)\b", re.I)
# Workday saving again what a step saved a moment ago: "A phone number already exists for this application. Update or
# delete the phone number." (PayPal's, run 56; Grand Canyon Education's address, run 55)
_SAVED_ALREADY = re.compile(r"\balready exists for this application\b", re.I)
# A part of a page that failed to load, in the words of Hotwire's Turbo (the State of Arizona's posting page, run 63)
_CONTENT_MISSING = re.compile(r"\bcontent missing\b", re.I)
_REFRESH_ASKED = re.compile(r"\bsomething went wrong\b.{0,200}\b(?:refresh|reload) (?:the|this) page\b", re.I | re.S)
_TURNED_AWAY = re.compile(r"^\s*(?:403\s*)?forbidden\s*$|^\s*(?:406\s*)?(?:request )?not acceptable\s*$|"
                          r"^\s*your request has been blocked\.?\s*$", re.I)  # (Workday's, its title empty)
# A job system's own error page (iCIMS's, run 74: "Oops, something went wrong", then "Technical Details:")
_ERROR_PAGE = re.compile(r"\bsomething went wrong\b|\btechnical details\b", re.I)
# A web server's own error page, bare: its title or first heading a 5xx's words as a whole and nothing else (nginx's
# "502 Bad Gateway" where UKG Pro's new sign-in should be, run 106, at three employers at once, after the desk's Apply
# now; IIS's "500 - Internal server error.", Tomcat's "HTTP Status 500 – Internal Server Error", Apache's "Proxy
# Error", Varnish's "503 Backend fetch failed"); a bare number ("Error 503", "HTTP Error 502") only as the page's title
_5XX = r"(?:(?:http\s+(?:status|error)\s+|error\s+)?50[0-4]\s*[-\u2013\u2014:.]?\s*)"
_SERVER_ERROR = re.compile(r"^\s*" + _5XX + r"?(?:internal server error|bad gateway|proxy error|service (?:temporarily )?"
                           r"unavailable|gateway time-?out|server error|backend fetch failed)\.?\s*$", re.I)
_SERVER_ERROR_CODE = re.compile(r"^\s*(?:http\s+(?:status|error)\s+|error\s+)?50[0-4]\s*\.?\s*$", re.I)
# A page's own language picker: nothing to apply with (iCIMS's error page, run 74: "Language", English (US))
_LANGUAGE_PICKER = re.compile(r"^\W*(?:language|select (?:a |your )?language)\W*$", re.I)


def _page_chrome(f: dict[str, Any]) -> bool:
    """A list that's the page's, not the application's: its language picker (iCIMS's error page's "Language", run 74),
    or a list with no label and nothing in it (Cornerstone's language list on Linde's empty requisition page, run 75).
    Never one the page requires or marks wrong: that's a question."""
    if f.get("required") or f.get("invalid") or f.get("kind") not in ("select", "listbox", "combobox"):
        return False
    label = clean_label(f.get("label") or "")
    return bool(_LANGUAGE_PICKER.match(label)) or (f.get("kind") == "select" and not label and not f.get("options"))


def _app_fields(data: dict[str, Any]) -> list[dict[str, Any]]:
    """A page's boxes that are the application's: all but its own language picker (_page_chrome). Whether a page is a
    form, and has a way on, is told by these alone; the page as read keeps them all (what's filled, and what's
    compared from one look to the next, sees every box)."""
    return [f for f in data.get("fields") or [] if not _page_chrome(f)]
# A posting that has closed: "The job posting you are looking for has expired or the position has
# already been filled" (Edward Jones' BrassRing), "This job is no longer available"
_CLOSED = re.compile(
    r"\b(?:posting|job|position|requisition|opening|vacancy)\b[^.]{0,60}?\b(?:has|have|is|was)\b(?: already)?(?: been)?"
    r" (?:expired|filled|closed|no longer (?:available|open|active|accepting))|no longer accepting applications", re.I)
# A posting's words that only say when or how it closes, never that it has (the review of note 39's): a condition before
# them in their sentence ("Applications will be accepted until the position has been filled", "If this job has
# expired, ..."), and after them, how it's filled or what it's closed for or to ("filled on a rolling basis", "closed
# for maintenance", "closed to external applicants")
_CLOSED_IF = re.compile(r"\b(?:until|till|unless|if|when|whenever|once|after|before|as soon as)\b", re.I)
_CLOSED_HOW = re.compile(r"\s*(?:on (?:a |an )?(?:rolling|ongoing|first)|for (?:maintenance|the (?:day|weekend|holidays?)|"
                         r"today|now)|on weekends|to (?:external|internal|outside)\b|quickly|soon|as soon)", re.I)
# The site saying this person applied for this job before (issue #112, note 30: Workday's job page "You applied for
# this job on <date>." over its View Application, an Eightfold application saying it was applied to already): a
# sentence of its own that starts with it, never "If you are an internal candidate or you have already applied for
# this position, sign in", "Already applied? Sign in", a limit on applications, or a question asking whether they have.
_APPLIED_BEFORE = re.compile(
    r"\byou applied (?:for|to) this (?:job|position|role|posting|requisition|opening|vacancy) on\b|"
    r"\byou(?:'ve| have)? already (?:applied|submitted an application) (?:for|to) this "
    r"(?:job|position|role|posting|requisition|opening|vacancy)\b(?!\s*(?:\?|before|within|in the))", re.I)
_WRAPPED = re.compile(r"(?:,|\b(?:or|and|the|an?|to|for|of|if|that|who|you|have|whether|unless|when|once))\s*$", re.I)
_BOT_TEXT = re.compile(r"verify (?:that )?you are (?:a )?human|are you a robot|checking (?:if the site connection is secure|"
                       r"your browser)|press (?:&|and) hold|complete the security check|unusual traffic from your|"
                       r"enable javascript and cookies to continue|request unsuccessful|you have been blocked", re.I)
_CAPTCHA = re.compile(r"i'?m not a robot|i am human|hcaptcha|recaptcha challenge", re.I)
_VERIFY_EMAIL = re.compile(r"verif(?:y|ication)\b.{0,40}\b(?:e-?mail|account|link)|check your (?:e-?mail|inbox)", re.I)
# A sign-in refused because the account's email was never verified (Workday's "Verify your account before you sign in
# or request a verification email.", beside its "Resend Account Verification": the test identity's Vanguard, run 98,
# an account from an earlier run whose link went unopened). Said only after a refusal: the sign-in page as it opens
# has neither (live, Oct 2026: Vanguard's, Banner Health's, HonorHealth's)
_VERIFY_FIRST = re.compile(r"\bverify your (?:account|e-?mail(?: address)?)\b[^.?!]{0,40}\bbefore (?:you )?(?:can )?"
                           r"(?:sign(?:ing)?|log(?:ging)?) ?in\b|\byou need to verify your (?:account|e-?mail)\b|"
                           r"\b(?:account|e-?mail(?: address)?) (?:has not|hasn'?t|is not|isn'?t|was not|wasn'?t) (?:been )?"
                           r"(?:verified|confirmed|activated)\b", re.I)
# The site's word that it couldn't send that email (Workday's "Failed to resend verification email")
_SEND_FAILED = re.compile(r"\b(?:failed|unable|couldn'?t|could not|can'?t|cannot) to (?:re-?)?send\b|"
                          r"\b(?:failed|unable|couldn'?t|could not|can'?t|cannot) (?:to )?(?:re-?)?send(?:ing)?\b", re.I)
# The page's word that the verification email was just sent (Banner Health's "An email has been sent to you. Please
# verify your account"): that one is waited for, not asked for again
_VERIFY_SENT = re.compile(r"\b(?:e-?mail|link|message)\b[^.?!]{0,40}\b(?:has been|have been|was|were|is|been) "
                          r"(?:sent|e-?mailed)\b|\bwe(?:'ve| have)? (?:just )?(?:sent|e-?mailed)\b|"
                          r"\bsent (?:you )?(?:an? )?(?:new )?(?:verification |confirmation |activation )?(?:e-?mail|link)\b", re.I)
# A sign-in page's button that sends the verification email again (Workday's "Resend Account Verification")
_RESEND_VERIFY = re.compile(r"^(?:re-?send|request|send)(?: (?:the|a|an|me|my|another|new))*(?: account)?"
                            r"(?: (?:e-?mail|verification|confirmation|activation))+(?: (?:e-?mail|link|again))?$", re.I)
_CODE_FIELD = re.compile(r"verification code|one[- ]time (?:pass)?code|passcode|security code|\bcode\b.{0,40}"
                         r"(?:sent|email)|enter (?:the )?(?:\d-digit )?code|\botp\b", re.I)


# A box that asks for a phone number, though its words speak of codes sent to it (Edward Jones' BrassRing, run 85:
# "... an Authentication Code sent to you ... Please enter the phone number you would like to use to receive your
# code(s) ... in the following format: ###-###-####"): the application's, never a code's. (Not a code the number names,
# "Enter your phone number verification code", "the mobile number OTP", nor a "Re-enter your phone number" beside one:
# the review's)
_ASKS_A_PHONE = re.compile(r"(?<![\w-])enter (?:the|your|a) (?:\w+ ){0,2}(?:phone|mobile|cell|telephone) number\b"
                           r"(?!'?s? (?:verification |security |one[- ]time |sms )?(?:code|otp|passcode|pin)\b)|#{3}-#{3}-#{4}",
                           re.I)


def _code_box(f: dict[str, Any]) -> bool:
    """An emailed code's box: a text or number box named for one, or a password box named for one
    (SuccessFactors' "*Passcode:" after making an account, live, Oct 2026: the desk put the saved
    password in it). Never the saved password's, nor one that asks for a phone number (_ASKS_A_PHONE)."""
    label = f.get("label") or ""
    return f.get("kind") in ("text", "number", "password") and not _ASKS_A_PHONE.search(label) and bool(
        _CODE_FIELD.search(label) or f.get("kind") == "password" and re.search(r"one[- ]time password", label, re.I))
_SIGN_IN_ACTION = re.compile(r"^(sign in|log ?in|sign in with email)$", re.I)
# What else a form holding only the sign-in calls its button (SuccessFactors': "Submit")
_SIGN_IN_SUBMIT = re.compile(r"^(submit|continue|next|go|enter|log ?on|sign ?on|sign in now|log ?in now)$", re.I)
# The button pressed once the emailed code is in; one labelled Submit is left to the person (but on
# the code step of an account the desk is making, with nothing else on it: _account_code_button)
_AFTER_CODE = re.compile(r"^(verify|confirm|continue|next)( ((?:pass)?code|e-?mail|account|my e-?mail))?$", re.I)
# With settings.manage_accounts, the buttons of an email-first site's steps of making an account (an
# Eightfold site's: its agreement's "Submit", the email's "Continue", "Use a one-time code" in place of
# a password, and the code's "Submit")
_ACCOUNT_ON = re.compile(r"^(submit|continue|next|agree|i agree|accept|create(?: an| my| a new)? account)$", re.I)
_ONE_TIME_CODE = re.compile(r"^(?:use an? )?(?:one[- ]time (?:pass)?code|e-?mail (?:me )?an? code)(?: instead)?$", re.I)
_CODE_SENDS = re.compile(r"^submit$", re.I)
_APPLICATION_WORD = re.compile(r"\bapplication\b|\bapply\b", re.I)
_TRY_LATER = re.compile(r"\btoo many\b.{0,30}\b(?:attempts|requests|tries)\b|\btry again (?:later|in \d+)|\brate[- ]limit",
                        re.I)
_CREATE_ACCOUNT = re.compile(r"^(?:proceed to |continue to )?(create (?:an |your |a new )?account|sign up|register)"
                             r"(?: now)?[.!]?$|^don['\u2019]?t have an account(?: yet)?\??$|"  # (SuccessFactors' link)
                             r"^new user\??$", re.I)  # (Taleo's button)
# A sign-in or Create Account box for a user name, not the email (Taleo's "User Name"): the desk
# puts the profile email in it, and signs in with that later
_USER_NAME = re.compile(r"\buser ?name\b|\blog ?in\b", re.I)
# A Create Account form's security question or its answer (some Taleo sites ask for them): the
# person's to choose and answer, never made up
_SECURITY_QUESTION = re.compile(r"\b(?:security|secret|challenge|password (?:reminder|recovery|hint))\b[^.?!]{0,20}?"
                                r"\b(?:questions?|answers?)\b|\bpassword hint\b", re.I)
# A picture code's box, a CAPTCHA the site draws itself (BrassRing's "Enter Captcha", Benchmark's Infor
# registration's "Enter the text in image above"): the person's to solve
_PICTURE_CODE = re.compile(r"captcha|text in (?:the )?(?:image|picture)", re.I)
# With settings.manage_accounts: a Create Account form's own button, the boxes on it that agree to
# the site's terms (required ones, or its terms, or a privacy policy or notice read: "Yes, I confirm that
# I have read the privacy notice", a Workday site's; never a newsletter's, job alerts', being kept informed
# or contacted), the site saying the email already has an account (said of the account or
# email, not "Already have an account? Sign in"), the way to a password reset (not a username
# reminder), the buttons of a reset's pages, and its "we've emailed you a link"
_MAKE_ACCOUNT = re.compile(r"^(create(?: an| my| your| a new)? account|register|sign ?up|create|submit|continue)$", re.I)
# and the button of its second step, on a page of its own (UKG Pro's "Create account" after a name and phone)
_ACCOUNT_BUTTON = re.compile(r"^(create(?: an| my| your| a new)? account|register|sign ?up)$", re.I)
_TERMS_ONLY = re.compile(r"terms|conditions|(?:privacy|data protection) (?:policy|notice|statement)"
                         # (and what a Workday Create Account's own box says in their place, never marked required:
                         # the first nightly at every employer, Oct 11, nine stopped on it) the account itself
                         # ("I agree to create(ing) a candidate home account"), a bare "I agree" or "I consent", the
                         # processing of one's personal data (a GDPR notice), the documents above read
                         r"|\b(?:agree|consent)\w*\b.{0,40}\bcreat\w*\b.{0,60}\baccount\b|^\s*(?:yes,? )?i (?:agree|consent|accept)\W*$"
                         r"|\bgdpr\b|process\w* (?:of )?my personal data|acknowledge having read", re.I)
_TERMS_BOX = re.compile(r"terms|conditions|privacy|consent|agree|acknowledge|policy|notice", re.I)
# What tells a sign-up's page or box from an account's, where the desk only reads what step a page is
# (_email_first_account, _email_account_view) or names a box still empty (_account_wants). What's ticked or agreed to
# is _never_for_them's, and each word here is one of its (the tests check). A wider list here would move a step to
# another way of being taken (an account's agreement read as the application), not keep a tick or a press from it
_NOT_TERMS = re.compile(r"newsletter|mailing list|marketing|job alerts?|text messages?|\bsms\b|promotion|offers|subscribe|similar jobs|"
                        r"talent (?:community|network)|keep me|stay informed|send me|contact(?:ed)? me|be contacted|"
                        r"share my|other (?:\w+ )?(?:roles|positions|jobs|openings|opportunities)|affiliat|" + CALLS_OR_TEXTS, re.I)
# How long a box's agreement lasts, which is no more than it (Air Liquide's Workday, live run 41, Oct 12: "You
# acknowledge having read the documents above. Your agreement is valid for any future visits."): not "future" news
_LASTS = re.compile(r"\byour (?:agreement|consent|acceptance) (?:is|will be|remains) valid for (?:any|all) future visits\b"
                    # and a privacy notice received, which is no more than it (Boeing's Workday Create Account, run 44,
                    # Oct 13: "I have received the Boeing Applicant Privacy Notice."): not "receive" emails
                    # (only the verb taken out: the notice's own name is still read, "the Talent Pool Privacy Notice")
                    r"|\bhave (?:read and )?received(?= (?:a copy of )?(?:the|a|an|our|its)\b[^.]{0,60}?"
                    r"\b(?:privacy|data protection) (?:notice|policy|statement)\b)", re.I)
# A box whose words say it agrees to the words above it ("the documents above"): what it agrees to is up there
_READ_ABOVE = re.compile(r"\b(?:documents?|terms|text|information|notices?|statements?|policy|policies) above\b", re.I)
# A box that says only that the words above it were read (Avnet's Workday Create Account, run 44: "Yes, I have read and
# understood." under "you acknowledge Avnet will collect and process your personal data as outlined in our Applicant
# Data Protection and Privacy Statement"): ticked only where those words are the site's terms or privacy statement
_READ_UNDERSTOOD = re.compile(r"^\W*(?:yes,? )?i have read,? and (?:understood|understand)(?: (?:it|this|them|the above))?\W*$",
                              re.I)
# What the words above any such box (a bare "I agree", "the documents above", "read and understood") must name for the
# desk to tick it: the site's terms or privacy statement, or its processing of personal data; never the terms of
# employment, and never an attestation (the reviews of 0.3.133's: a paragraph that names none of these, "Join our
# Careers Community", is the person's however it's worded)
_SITES_TERMS = re.compile(r"\b(?:privacy|data protection)\b[\w ]{0,25}\b(?:policy|notice|statement)\b|"
                          r"\bterms (?:of (?:use|service)|and conditions)\b|\bpersonal (?:data|information)\b|\bgdpr\b",
                          re.I)
_ATTESTS_ABOVE = re.compile(r"of employment|certif|true and complete|background check", re.I)
# The kinds of what's never agreed to (never.py) each path reads: what it read before 0.3.208, and besides only words
# that are a sign-up or a consent wherever they stand (PRECISE: the review of 0.3.208)
# - an agreement's own words (_agrees_to_terms, the consent to other positions), a notice's heading and opening, a terms
#   gate's headings, as the agreements' and notices' own list read them: with the future roles kept on file for. What
#   a sign-up sends and the mail received beyond the application are an attestation's to read (_ATTESTED_KINDS), as
#   before: elsewhere they're said in passing ("you consent to receive communications electronically")
_AGREEMENT_KINDS = PRECISE | {FUTURE_ROLES}
# - a Create Account box's own words (_more_than_terms): every kind but a paragraph's, and neither cookies nor their data
#   kept, as before: a box that names the site's cookie policy beside its terms isn't a cookie banner, which keeps its
#   own rule ("I have read and agree to the Terms and Conditions, Privacy Policy and Cookie Policy"), and how long the
#   site keeps the account's data is its terms' ("... Acme may keep my data for 12 months": the review of 0.3.208)
_BOX_KINDS = frozenset(NEVER) - {PARAGRAPH_WORDS, "cookies", "their data kept"}
# - the words above a bare "I agree" (_terms_above): an agreement's, a paragraph's sign-up words, and what a sign-up
#   sends (not the account's own mail: MAIL)
_ABOVE_KINDS = PRECISE | {PARAGRAPH_WORDS, FUTURE_ROLES, EMAILS}


def _more_than_terms(label: str) -> str | None:
    """What a Create Account's terms box agrees to, by its own words, that's never agreed to for the person
    (_never_for_them, _BOX_KINDS: a terms box says no more than its terms), less how long its agreement lasts or a
    privacy notice received (_LASTS). None when it's no more than the terms."""
    return _never_for_them(_LASTS.sub(" ", label), only=_BOX_KINDS)


def _terms_above(above: str) -> bool:
    """Do the words just above a box that says only that it agrees (a bare "I agree", "the documents above", "read and
    understood") name the site's terms or privacy statement, or its processing of personal data, and nothing more?
    Never anything never agreed to for the person (_never_for_them, _ABOVE_KINDS: a sign-up, texts or calls, a waiver;
    the safety audit of Oct 12, the reviews of 0.3.126's and 0.3.133's), nor an attestation (_ATTESTS_ABOVE). The
    account's own mail it names isn't a sign-up ("You will receive an email to verify your account")."""
    return bool(above and not _never_for_them(above, only=_ABOVE_KINDS)
                and _SITES_TERMS.search(above) and not _ATTESTS_ABOVE.search(above))


# A box whose own words say only that the person agrees: what it agrees to is in the words above it
_BARE_AGREE = re.compile(r"^\W*(?:yes,? )?i (?:agree|consent|accept)\W*$", re.I)
# A box that says only that the words above it were understood and agreed to (BrassRing's "Employment At Will", Edward
# Jones' run 89: "I understand and agree"): on the card with those words. Never ticked for that alone
_UNDERSTOOD_AGREE = re.compile(r"^\W*(?:yes,? )?i (?:understand|acknowledge),? and (?:agree|accept)\W*$", re.I)
# A Create Account box that says the person applies from outside, not as one of the employer's staff
# (Banner Health's Workday, live, Oct 2026: "Yes, I am a new candidate and not a current employee"; no
# account without it, though it isn't marked required). That and nothing more: a box that says more
# about the person is theirs
_NEW_CANDIDATE = re.compile(r"^(?:yes,? )?i am (?:a |an )?(?:new|external) (?:candidate|applicant)(?:,? and (?:i am )?not "
                            r"(?:a |an )?(?:current|existing) (?:employee|associate|team member))?|^(?:yes,? )?i am not "
                            r"(?:a |an )?(?:current|existing) (?:employee|associate|team member)(?: of [\w&.' -]+)?", re.I)
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
# A page's title or heading that makes an account (BrassRing's "Create new account" over its "Account Information")
_MAKES_ACCOUNT = re.compile(r"\b(?:create|new|register|sign ?up|open)\b[^.]{0,20}\baccount\b|"
                            r"\baccount (?:creation|registration|set ?up)\b", re.I)
# A reset's page saying its email is on the way: "We have sent a link", "Check your inbox", and Workday's
# "You will receive an email with instructions ... if an account exists for this email address"
_RESET_SENT = re.compile(r"\b(?:sent|emailed)\b.{0,80}\b(?:link|e-?mail|instructions)|check your (?:e-?mail|inbox)|"
                         r"\byou(?:'ll| will) (?:receive|get) an? e-?mail\b|\bif an account exists\b", re.I)
# A reset's page saying there's no account for the email ("There is no user with that username or email")
_NO_ACCOUNT = re.compile(r"\bno (?:user|account|record|match)\b|\b(?:not|isn'?t|wasn'?t) (?:found|registered|recogni[sz]ed)|"
                         r"\b(?:don'?t|do not|didn'?t|did not) recogni[sz]e (?:this|that|your|the) e-?mail|"
                         r"does(?:n'?t| not) (?:exist|have an account|match (?:any|an) account)|"
                         r"\b(?:unknown|unrecogni[sz]ed) (?:user|e-?mail|account)|could(?:n'?t| not) find (?:an? |your )?"
                         r"(?:account|user)", re.I)
# A page that can't be opened again: SuccessFactors' sign-in page (career?_s.crb=...), good for one visit
_SPENT_PAGE = re.compile(r"an error occurred while processing your request|please go back to your original page|"
                         r"\b(?:session|page|link) (?:has )?expired\b", re.I)
# A page about making an account ("Create an account", amazon.jobs after an email it doesn't know)
_ACCOUNT_PAGE = re.compile(r"\b(create (?:an |your |a new )?account|sign up|register)\b", re.I)
_ACCOUNT_KINDS = {"text", "email", "tel", "select", "combobox", "listbox"}  # not check boxes or files
# A Create Account's own details, the only ones filled beside security questions (_fill_account_details)
_ACCOUNT_DETAIL_RULES = {"email", "first_name", "middle_name", "last_name", "preferred_last_name", "preferred_name",
                         "preferred_full_name", "full_name", "phone_type", "phone_code", "phone_ext", "phone", "country"}
# A box that asks a question of its own ("In what town were you born?", "Name of your first pet")
# (never a plain "First name": a first pet's, car's, school's or street's is a question's)
_QUESTION_LIKE = re.compile(r"\?|^\s*(?:what|which|who|whose|where|when|why|how|in what|name of)\b|"
                            r"\b(?:maiden|born|birth|childhood|favou?rite|first (?:pet|car|school|job|employer|street|"
                            r"teacher|concert))\b", re.I)
_SOCIAL = re.compile(r"\b(google|apple|linked ?in|facebook|microsoft|indeed|seek)\b", re.I)
# (and BrassRing's "Let's get started", its application's first page once the account is made: probe 81)
_STEP = re.compile(r"^(save (?:and|&) continue|continue|next|next step|review|review (?:and|&) submit|"
                   r"review application|proceed|go to next step|start|let[\u2019']?s get started)$", re.I)
_FORWARD = re.compile(r"^(save (?:and|&) continue|continue|next|next step|review|review application|proceed|"
                      r"go to next step)$", re.I)
_STEP_OF = re.compile(r"\bstep (\d+) of (\d+)\b", re.I)  # a progress bar's place: "current step 3 of 6"
# A sign-up for job alerts, a newsletter or a talent community (the form reader's SIDE_BOX), beside an
# application or on the page that thanks the person for one: its boxes and button are never the application's. What
# tells such a form from the application, as _NOT_TERMS tells an account's step: each word is _never_for_them's too (the
# tests check), and a wider list would take the application's own boxes for a sign-up's (a page taken for sent)
_SIDE_BOX =re.compile(r"job alerts?|alerts? by e-?mail|e-?mail alerts?|newsletter|\bsubscribe\b|talent (?:community|"
                       r"network|pool)|notify me|similar (?:jobs|openings|roles)|stay (?:connected|in touch)", re.I)
# Words that ask the person to go on with the form, after a step of it thanks them for applying
# ("Thank you for your application. Please complete the below questions.")
_GO_ON = re.compile(r"\bplease (?:complete|continue|answer|proceed|fill|finish|go on)\b|\b(?:complete|answer|"
                    r"fill (?:in|out)|respond to) (?:the |these |all )?(?:following|below|remaining)\b|"
                    r"\b(?:following|below|remaining) questions\b|\bquestions below\b", re.I)
_SIGN_IN_STEP = re.compile(r"create account\s*/\s*sign in|sign in\s*/\s*create account", re.I)  # Workday's step name
_ENTRY = re.compile(r"^(apply manually|apply now|apply online|apply|easy apply|quick apply|"
                    r"apply for (?:this|the) (?:job|position|role)(?: online)?|"
                    r"apply to (?:this )?job|start (?:your |my )?application|i'?m interested|"
                    r"continue to application|apply on (?:the )?(?:company|employer)(?:'s)? (?:site|website))$", re.I)
# The way back into an application the site kept from an earlier visit (Workday's job page shows "Continue Application"
# where its Apply was, its data-automation-id "continueButton": issue #112, note 38): a draft, whose answers are checked
# against the profile (Run.draft). The way in only on a page whose one way in it is, and only where it's this job's
# (_own_draft); never a dialog's way on (_notice), nor a sign-up's or an account page's button: those stop as before
_DRAFT_ENTRY = re.compile(r"^continue (?:your |my )?(?:saved )?application$", re.I)


def _way_in(text: str) -> bool:
    """A posting's way into its application: an Apply (_ENTRY), or a saved draft's Continue Application."""
    return bool(_ENTRY.match(text) or _DRAFT_ENTRY.match(text))
# The words of a button that sends an application once it's filled in: on a form, never the way in
_APPLY_SENDS = re.compile(r"^(apply|apply now|apply online|easy apply|quick apply|apply for (?:this|the) (?:job|position|role)"
                          r"(?: online)?|apply to (?:this )?job)$", re.I)
_AVOID = re.compile(r"autofill|with resume|resume parse|sign ?in|log ?in|create account|register|upload|back|"
                    r"previous|cancel|save for later|withdraw|delete|remove|search|share|print|email (?:me|this)", re.I)
# A button that agrees to something ("I Acknowledge the Privacy Notice", Schwab's iCIMS sign-in):
# never pressed for the person
_AGREEMENT = re.compile(r"^(?:i )?(?:acknowledge|agree|accept|consent)\b.*\b(?:notice|terms|policy|privacy|statement|"
                        r"agreement|conditions)\b|^i (?:acknowledge|agree|accept|consent)\b", re.I)
# A decline button's own words that say it's a cookie banner's ("Decline cookies", "Necessary only", "Reject optional")
_NAMES_COOKIES = re.compile(r"cookie|necessary|essential|optional", re.I)
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
# comes with applying. Never a newsletter's, job alerts' or marketing's (_attests_more)
_ATTESTS = re.compile(r"\b(?:certif(?:y|ies)|attest|affirm|declare|acknowledge|confirm)\b.{0,200}?\b(?:information|answers?|"
                      r"statements?|responses?|facts)\b.{0,200}?\b(?:true|correct|complete|accurate)\b|"
                      r"\b(?:consent|authori[sz]e|agree)\b.{0,120}?\bbackground (?:check|screening|investigation)", re.I | re.S)
_ATTEST_KINDS = {"select", "listbox", "combobox", "checkbox", "checkbox_group", "radio_group"}
# and, the owner's call (Oct 10), an employer's privacy notice and terms of use: a gate's "I Accept"
# (Kforce's Taleo privacy agreement), "I Acknowledge the Privacy Notice" (Schwab's iCIMS), a dialog,
# or a required box ("I have read and agree to the Privacy notice and Terms of use": ASM's;
# "Terms of Use & Data Privacy Statement: Please accept ...": EMD's)
_TERMS = re.compile(r"\bprivacy\b|\bdata protection\b|\bterms (?:of (?:use|service)|and conditions|& conditions)\b|"
                    r"\b(?:candidate|applicant) (?:notice|statement|agreement)\b|\bpersonal (?:data|information)\b|"
                    r"\b(?:gdpr|ccpa)\b", re.I)
_ACCEPTS = re.compile(r"\b(?:agree|accept|acknowledge|consent|have read|understand)\b", re.I)
_TAKES_IN = re.compile(r"^(?:ok|okay|got it|continue|proceed)$", re.I)
# A dialog that offers how to start an application's profile (Eightfold's "Profile Bootstrapping", Northrop
# Grumman's, live, Oct 2026: "Create your profile with existing information"): a button to upload the resume,
# with its file box in the dialog, and one to enter the information by hand. A choice, not an agreement
_UPLOAD_RESUME = re.compile(r"^(?:upload|attach|choose|select)(?: (?:your|my|a|the))? (?:resume|cv|résumé)"
                            r"(?: ?/ ?(?:resume|cv|résumé))?(?: file)?$", re.I)
_ENTER_INFO = re.compile(r"^(?:(?:enter|fill in|add)(?: (?:your|my|the))? (?:information|info|details|profile)"
                         r"(?: manually| myself)?|(?:enter|fill in) manually)$", re.I)
# A Create Account form's link that opens the site's data privacy statement to be read and accepted: the site
# makes no account without it (SuccessFactors', live, Oct 10, 2026: "Read and accept the data privacy statement.",
# which opens the statement in a dialog with Accept and Decline). Its words accept a privacy notice or terms
# (_agrees_to_terms), and nothing more
_STATEMENT_LINK = re.compile(r"^(?:please )?(?:read|view|open)\b", re.I)
# How much of a notice named for the employer's privacy statement or terms (in its heading) is read for what's
# never agreed to: its opening. The statement's long text speaks of its affiliates, marketing and cookies as
# its practices (APS's, live: "... Company and its affiliated companies ..."), which don't sign anyone up
NOTICE_OPENING = 300
# (but a legal waiver, an arbitration agreement or a jury trial waived, and a sign-up in so many words, are never agreed
# to for the person however deep in its text they sit: ANYWHERE)
# A consent to be considered for other open positions as well as the one applied to (Northrop Grumman's
# Eightfold "Contact Consent", live, Oct 2026: "I understand that I may be considered for other open
# positions in my country, in addition to the role(s) to which I apply"). The owner's call (Oct 10): given
# for the person on an account form the site won't make the account without it (marked required, or
# refused until it's given), never otherwise, nor with a newsletter, job alerts or the like in its words
_OTHER_POSITIONS = re.compile(r"\bconsider(?:ed|ation)?\b.{0,60}?\bother (?:open |suitable |relevant |available )?"
                              r"(?:positions|roles|jobs|openings|opportunities)\b", re.I | re.S)
# A box that also says something about the person ("... and confirm I am authorized to work in the US"):
# theirs, as the profile answers those
_CLAIMS = re.compile(r"\b(?:authori[sz]ed|sponsor\w*|eligib\w*|citizen\w*|years? (?:old|of age)|at least \d+|"
                     r"over (?:the age|\d+)|or older|licen[cs]e|degree|convict\w*|felon\w*|criminal|drug|relocat\w*|"
                     r"willing|veteran|disabilit\w*|cdl|i am|i have (?:a|an|been|never|not))\b", re.I)
# and a question about something else beside the agreement ("Have you ever been convicted ...? I certify ...")
_OTHER_QUESTION = re.compile(r"\b(?:have|do|are|will|can|did|were|would|has|is) you\b(?! (?:agree|accept|acknowledge|"
                             r"consent|certify|understand|have read|read|confirm)\b)", re.I)
# An attestation's consequence clauses, which say nothing about the person: "I understand that if I am employed,
# false statements are cause for dismissal", "this is a drug-free workplace", "eligibility for employment is
# contingent on a background check", "I may be required to present a valid driver's license" (the review of 0.3.175);
# and an agreement's own asides ("I am aware that I can withdraw my consent", "I understand that I am providing my
# information voluntarily", "I have a copy of it": the review of 0.3.176). Blanked before _CLAIMS and _OTHER_QUESTION
# read a label
_CONSEQUENCE = re.compile(r"\b(?:if|when|should|once) i am\b|\bi (?:understand|acknowledge)(?: that)? i am (?:subject|not guaranteed|"
                          r"applying|required|being considered|providing)\b|\bdrug[- ]free\b|\beligibility for (?:employment|hire)\b|"
                          r"\b(?:may|will|would) be required to (?:present|provide|show|obtain|pass|submit)\b[^.;]*|"
                          r"\bi am aware\b|\bi have a copy\b", re.I)
# A Create Account box's age statement ("I am 16 years or older, and I have read the Privacy Notice": Microchip's
# Workday), which the profile answers (work_authorization.over_18) when it says the person is over 18
_AGE_STATED = re.compile(r"\bi am (?:at least )?(?:1[3-8]|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen)"
                         r"(?: years?)?(?: of age| old)?(?: or (?:older|over|above))?\b", re.I)


def _about_person(label: str) -> bool:
    """An attestation's words that also say something about the person, or ask them another question (_CLAIMS,
    _OTHER_QUESTION), its consequence clauses aside: such a box is theirs."""
    said = _CONSEQUENCE.sub(" ", label)
    return bool(_CLAIMS.search(said) or _OTHER_QUESTION.search(said))


# A bot check's badge on a page ("Protected by hCaptcha", reCAPTCHA's): a press it holds is the person's
_BOT_BADGE = re.compile(r"\bh ?captcha\b|\brecaptcha\b", re.I)
# A dialog that mentions personal data but isn't a notice to take in ("could not be saved", "overwrite?")
_NOT_A_NOTICE = re.compile(r"\berror\b|could ?n[o']t|failed|signed? (?:you )?out|overwrite|existing (?:profile|account)|"
                           r"\bdelete|\bremove|withdraw|expired?\b", re.I)

# An attestation's words that certify the application's own information (beside which an at-will sentence may stand:
# the second review of 0.3.170)
_OWN_INFO_TRUE = re.compile(r"\b(?:information|answers?|responses?)\b[^.]{0,80}\b(?:true|correct|complete|accurate)\b", re.I)
# What an attestation names in passing that signs no one up: the employer's affiliates and its offers of employment ("...
# and authorize Acme and its affiliates to conduct a background check": the review's). Those words alone are taken out,
# and only for the kinds they'd be read as (_PASSING_KINDS): every other kind is read in the whole label
_ATTESTED_IN_PASSING = re.compile(r"\baffiliat\w*|\boffers? of employment", re.I)
_PASSING_KINDS = frozenset({"their data shared", "offers"})
# - an attestation's label: the precise kinds and the mail it may sign up for. Not the future opportunities false
#   statements cost ("... may disqualify me from future opportunities"), which a certification names, nor the site's
#   cookie policy it has read or how long it keeps the application ("I understand Acme will retain my application"),
#   as before (the review of 0.3.208); an agreement's are _agrees_to_terms's to read. Being considered for similar
#   roles is refused (job alerts' "similar roles"): a consent to other roles, which CLAUDE.md keeps narrow
_ATTESTED_KINDS = (PRECISE - {"cookies", "their data kept"}) | {EMAILS, MAIL}
# - a group of ticks' own label: a legal waiver and the terms of employment alone, as before ("Privacy and Marketing
#   Preferences" over its "I accept the privacy notice": the review of 0.3.208); each tick's words are read in full
_GROUP_LABEL_KINDS = frozenset({"arbitration", "waiver", "at-will"})


def _attests_more(label: str, group: bool = False) -> str | None:
    """What an attestation's label takes in that's never agreed to for the person (_never_for_them, _ATTESTED_KINDS, or
    a group's _GROUP_LABEL_KINDS), less what such words name in passing: its affiliates and its offers of employment
    (_ATTESTED_IN_PASSING), and an at-will sentence beside a certification of the application's own information
    (_OWN_INFO_TRUE: the second review of 0.3.170). None when it's no more than that."""
    kinds = (_GROUP_LABEL_KINDS if group else _ATTESTED_KINDS) - ({"at-will"} if _OWN_INFO_TRUE.search(label) else set())
    return _never_for_them(label, only=kinds - _PASSING_KINDS) \
        or _never_for_them(_ATTESTED_IN_PASSING.sub(" ", label), only=kinds & _PASSING_KINDS)


def _agrees_to_terms(text: str) -> bool:
    """Words that accept an employer's privacy notice or terms of use, and nothing more: nothing never agreed to for the
    person (_never_for_them, _AGREEMENT_KINDS: a newsletter's or the like, the terms of employment, a waiver), not a fact
    about the person, not another question."""
    return bool(_TERMS.search(text) and _ACCEPTS.search(text) and not _never_for_them(text, only=_AGREEMENT_KINDS)
                and not _CLAIMS.search(text) and not _OTHER_QUESTION.search(text))


def _terms_notice(heading: str, text: str) -> tuple[bool, bool]:
    """Is a dialog the employer's privacy notice or terms to take in, and may its Ok do it? Named so
    in its heading ("Privacy Policy of Example Corp": its Ok too), or asking in its words to accept
    them; never one about an error, a profile to overwrite or the like, nor what's never agreed to (_never_for_them).
    One named in its heading is the statement itself, and only its opening is read for those (NOTICE_OPENING),
    but all of it for a legal waiver or a sign-up in so many words (ANYWHERE)."""
    named = bool(_TERMS.search(heading))
    said = f"{heading} {text[:NOTICE_OPENING] if named else text}"
    if _never_for_them(said, only=_AGREEMENT_KINDS) or _never_for_them(text, only=ANYWHERE) or _NOT_A_NOTICE.search(said):
        return False, False
    return named or bool(_TERMS.search(text) and _ACCEPTS.search(text)), named
# A fill the page never let happen (it timed out, its script failed, a dialog stood over the box):
# the profile has the answer, so it's no question for the person
_FILL_BROKE = re.compile(r"^(?:TimeoutError|Error|TargetClosedError|DialogOpen)\b")


def _quietly(fn: Callable[..., None]) -> Callable[..., None]:
    """For the "What went in" panel's record: whatever goes wrong keeping it, the job goes on as it would have (nothing
    logged, that much of the record not kept)."""
    @functools.wraps(fn)
    def kept(*args: Any, **kwargs: Any) -> None:
        with contextlib.suppress(Exception):
            fn(*args, **kwargs)
    return kept


@dataclass
class WentIn:
    """What went into one application, for its card's "What went in" panel on the desk page, so the person can check
    the answers at a glance instead of reading each review page again. Only there: never logged, noted, reported or
    saved with a page. Each box's last answer (by its place: key_for), a file by its name, what was agreed to for the
    person, the account made, and the optional questions left empty. Never a password, an emailed code or a security
    question's answer (_kept_out), and bounded (WENT_IN_ANSWERS, WENT_IN_CHARS, WENT_IN_LINES). Plain text alone, so
    Run.public() stays JSON's."""
    # by place: page, section, label, value, kind ("answer", "choice": one of a list's own, "file")
    answers: dict[str, dict[str, str]] = field(default_factory=dict)
    left: dict[str, dict[str, str]] = field(default_factory=dict)  # optional questions left empty, by place
    agreed: list[str] = field(default_factory=list)  # notices, terms and attestations agreed to for the person
    accounts: list[str] = field(default_factory=list)  # accounts made
    # a saved draft's answers the desk changed to the profile's, "<label>: <old> → <new>" (issue #112, note 38's review):
    # here alone, as the draft's old answers are no detail of the profile's that a report's scrubber knows
    changed: list[str] = field(default_factory=list)
    full: bool = False  # answers past WENT_IN_ANSWERS went in too, not kept here
    # Each box's place as first kept, by its page's address and its id (a box keeps its place while the page around it
    # changes: an error heading drawn over it, a block added), and the page each place was first taken on. Never in
    # public()
    places: dict[str, str] = field(default_factory=dict)
    claimed: dict[str, str] = field(default_factory=dict)

    def key_for(self, url: str, box_id: str, base: str, page: str) -> str:
        """The place a box is kept under: its first one, by its page's address and its id (an id alone is its page
        load's three letters and a count, which a load elsewhere can repeat). New, it's `base` (the page's address, the
        box's section, label and sub-label, and which of the boxes so placed it is: _went_bases) where that place is
        free or was taken on the same page: the box drawn again (a Workday address block redrawn after its Country
        changed, a step gone back to) or the page loaded again, whose last answer is this one's. Taken on another page
        at the same address (a step drawn in place, or loaded afresh: Taleo's flow.jsf), this is another box: `base`
        with its page's name."""
        at = f"{url} {box_id}"
        if at in self.places:
            return self.places[at]
        for kept in (self.places, self.claimed):  # (the oldest go: boxes of pages long gone)
            if len(kept) >= 4 * WENT_IN_ANSWERS:
                for old in list(kept)[:WENT_IN_ANSWERS]:
                    del kept[old]
        first = self.claimed.setdefault(base, page)
        self.places[at] = key = base if first == page else f"{base} | {question_key(page)}"
        return key

    @_quietly
    def put(self, key: str, entry: dict[str, str]) -> None:
        """A box's answer: its last one stands, where (and under the page) its first one was."""
        if key in self.answers:
            entry = {**entry, "page": self.answers[key].get("page", entry.get("page", ""))}
        elif len(self.answers) >= WENT_IN_ANSWERS:
            self.full = True
            return
        self.answers[key] = entry
        self.left.pop(key, None)

    @_quietly
    def leave(self, here: set[str], left: dict[str, dict[str, str]]) -> None:
        """The optional questions a fill left empty, `here` the places of the boxes on its page: what an earlier fill
        left empty of those boxes is gone."""
        self.left = {k: v for k, v in self.left.items() if k not in here}
        for key, entry in left.items():
            if key not in self.answers and len(self.left) < WENT_IN_LINES:
                self.left[key] = entry

    @_quietly
    def forget(self, places: set[str]) -> None:
        """A block deleted from the page (a school's), with what went into it and what was left empty in it."""
        for kept in (self.answers, self.left):
            for key in places & set(kept):
                del kept[key]

    @staticmethod
    def _say(lines: list[str], line: str) -> None:
        line = " ".join(line.split())
        line = line if len(line) <= WENT_IN_CHARS else line[:WENT_IN_CHARS - 1].rstrip() + "…"
        if line and line not in lines and len(lines) < WENT_IN_LINES:
            lines.append(line)

    @_quietly
    def agree(self, line: str) -> None:
        self._say(self.agreed, line)

    @_quietly
    def change(self, line: str) -> None:
        self._say(self.changed, line)

    @_quietly
    def account(self, line: str) -> None:
        # (without the log's hint of where to turn the setting off)
        self._say(self.accounts, re.sub(r"\s*\(manage_accounts: false[^)]*\)\s*$", "", line))

    def public(self) -> dict[str, Any] | None:
        if not (self.answers or self.left or self.agreed or self.accounts or self.changed):
            return None
        return {"answers": [{k: v for k, v in e.items() if k != "kind"} for e in self.answers.values()
                            if e.get("kind") != "file"],
                "files": [{"label": e["label"], "name": e["value"]} for e in self.answers.values()
                          if e.get("kind") == "file"],
                "agreed": list(self.agreed), "accounts": list(self.accounts),
                **({"changed": list(self.changed)} if self.changed else {}),  # (a saved draft's only)
                "left": [dict(e) for e in self.left.values()], "full": self.full}


@dataclass
class Run:
    job_id: int
    title: str = ""
    company: str = ""
    status: str = "queued"  # queued | running | needs_you | ready | submitted | failed | skipped
    # questions | sign_in | bot_check | email_code | captcha | your_submit | stuck
    # | submit_failed (pressed, the form is still there) | check_submit (pressed, no confirmation)
    # | tailor (waiting for Claude to write a resume for this job) | restarted (begun before the desk restarted)
    need: str = ""
    reason: str = ""
    questions: list[dict[str, Any]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    logged: int = 0  # lines ever logged (the log keeps the last 40): something done since
    url: str = ""
    blocking: bool = False  # the queue waits on this one
    paused_at: float = 0.0
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
    # when the reset was asked for (the desk's press of its button, or the pause for the person's past a picture code):
    # its email comes after that, and one from before is an earlier reset's, its link spent (_check_mail)
    reset_at: float = 0.0
    # the saved password set as the site's new one through its reset (_set_new_password); or a reset link opened for the
    # account (the desk's, from the inbox, for this job; or one it found opened already, said on a card): the account is
    # there, so a sign-in refused after it is said for what it is, and never goes on to a Create Account. Until a
    # sign-in gets in, or another password is saved
    reset_set: bool = False
    reset_opened: bool = False
    # the reset email found was one whose link was opened already (_link_opened): an email came, so its wait running
    # out is said on a card, never taken for "no email: no account there"
    reset_spent: bool = False
    # ...and, for the job, whatever password is saved since: the site emailed a reset for the account, or the desk set or
    # opened one, so the account is there and never gets a Create Account (the re-review of 0.3.209: a password saved
    # since cleared the flags above, and its refusal pressed Create Account). Never cleared for the job
    account_known: bool = False
    # the card for a spent reset link was shown: its Resume goes back to the sign-in and tries the saved password, which
    # the person may have set through a new email's link in their own browser (the review of 0.3.209: on the same spent
    # page, the card came back on every Resume)
    reset_spent_said: bool = False
    refreshed: bool = False  # reloaded once for a page that asked for it ("Please refresh the page")
    start_reloaded: bool = False  # reloaded once for a posting page with no way on (the State of Arizona's, run 63)
    reuploaded: bool = False  # a resume the page showed but never kept: its Delete tried, to upload it again (EMD's, once a job)
    # the documents ("resume", "cover_letter") settled for this job: a file kept from before replaced with the one
    # tailored for the job, or tried, or left as the person's, or the desk's own put in (_replace_kept). Once a job, so a
    # page that draws the old file's name again isn't deleted from again and again
    kept_replaced: set[str] = field(default_factory=set)
    opened_fresh: bool = False  # this pass opened the job in a new tab (its page the posting's own, by its address)
    account_in: bool = False  # an account made for this job, and signed in to (Run.account_made said)
    load_reloaded: bool = False  # reloaded once for a step still loading after its wait (Workday's placeholders, run 57)
    list_reloaded: bool = False  # reloaded once for a school's list with no choices at all (GCU's Degree list, run 61)
    # gone in through the site's "Continue Application" (a draft it kept from an earlier visit, issue #112, note 38): its
    # boxes are given the profile's answer where they show another, once a page (draft_pages), until draft_off: the
    # person's Resume, or a stop other than one they answer without the application's boxes (a questions card, an emailed
    # code, a sign-in, a bot check: _DRAFT_GOES_ON), after which what they change in the tab is theirs. draft_changed:
    # a box the draft had filled holds another answer since (never sent by Submit for me: _finish)
    draft: bool = False
    draft_off: bool = False
    draft_changed: bool = False
    draft_pages: set[tuple] = field(default_factory=set)
    # skipped by the desk itself, its posting closed (issue #112, note 39); and that skip undone by the person: a closed
    # posting is theirs then, never skipped by itself again
    closed_skipped: bool = False
    closed_undone: bool = False
    hands_on: bool = False  # the person pressed Resume on it: what they did in the browser is never reloaded away
    # the desk went on with it, begun, without the person's Resume, where they may have worked in its tab meanwhile:
    # the queue picked it up by itself past its pause (_pick_up_left), their Undo of its Skip, or Apply on it again
    # (desk.apply). A file in its upload may be their own pick: never replaced (Applier._replace_kept: the
    # re-review of 0.3.209)
    in_their_hands: bool = False
    invalid_redone: bool = False  # the review page's invalid lists answered again from the profile (once a job)
    # its wait ran out in RESET_MAIL_WAIT with no email it could use: none came (no account there, most likely, so one is
    # made), or only one whose link was opened already (reset_spent: said on a card)
    reset_no_mail: bool = False
    reopened_elsewhere: bool = False  # its sign-in page, opened again, led elsewhere: the job opened again (once a job)
    schools_dropped: list[str] = field(default_factory=list)  # education blocks deleted (_drop_unfit_school)
    schools_tried: list[str] = field(default_factory=list)  # and those whose Delete was pressed, gone or not
    reopened: int = 0  # times _back_to_sign_in opened the job again: a press before one is another visit's
    # The job's sign-ins pressed with the saved password, and which password that was (a digest): a Run
    # outlasts Resume and the queue, so these hold for the job, not for one pass at it
    sign_in_tries: int = 0
    sign_in_key: str = ""
    accounts_tried: int = 0  # Create Account pressed for this job (settings.manage_accounts): never again
    account_made: str = ""  # what to log once the site shows the account made (not just the form gone)
    # An email-first sign-in said it has no account for the email here (an Eightfold site's modal): the
    # page (its address, bare) whose next steps make one (_email_account_view), until the application shows
    account_page: str = ""
    statement_accepted: bool = False  # a Create Account's data privacy statement accepted for them (_accept_statement)
    account_finished: bool = False  # pressed the Create Account's second step (UKG Pro's "Almost there!"): never again
    # the person's answers to an account form's security questions, given on the desk's card, by box (placed_key): for
    # that form alone, used once, never remembered, logged, nor shown anywhere (never in public())
    account_answers: dict[str, Any] = field(default_factory=dict)
    try_later: bool = False  # left on a "Try Again Later" page: only the person's Resume goes on from it
    active_at: float = 0.0  # when its paused tab last changed: someone at work in it
    captcha: bool = False  # paused on a bot check or CAPTCHA (the person's to solve): marked so on the desk
    tab_mark: int = 0  # what its paused tab looked like then (address and box values)
    desk_at: float = 0.0  # when the desk last typed or opened a link in its paused tab itself (its emailed code's)
    desk_code: int = 0  # and the code it typed then, as _code_mark has it (never the code itself, nor in public())
    left: bool = False  # paused for the person, and the queue went on without it
    moved_since: float = 0.0  # since when every look has found its paused tab past the pause
    pause_sig: tuple | None = None  # how the paused page looked: the same page is never past itself
    once_page: dict[str, tuple] = field(default_factory=dict)  # where each this-application answer went in
    mail_checked: float = 0.0  # when the inbox was last looked at for its emailed code or link
    mail_done: bool = False  # the code or link from the inbox went in: no more looking
    # the employer's own mail domains, from its list (looked up once), and _another_employers_mail's answer for this
    # job (False: not looked up yet): the desk's own, never in public() (a frozenset and a function aren't JSON's)
    employer_mail: frozenset[str] | None = None
    others_mail: Any = False
    # what the desk's looks in the inbox came to, for the live check's LIVE_INBOX line (not the person's)
    mail_looks: int = 0
    mail_last: str = ""
    # when its mail was last looked for (found or not); and how long its looks were held off meanwhile (another job
    # under way in a step that may email), and since when they are now: its waits don't run on while they are
    mail_looked: float = 0.0
    mail_held: float = 0.0
    mail_held_at: float = 0.0
    # This pause waits for the verification email of the account the desk just made (_await_verify_email):
    # a Resume after it, or its link opened from the inbox, means the account is verified: signed in to once
    verifying: bool = False
    # its "Resend Account Verification" pressed for this job (an account there from before, never verified): once
    verify_resent: bool = False
    # Submit was pressed for this job before and no confirmation showed (its folder keeps the
    # record): it may have gone, so it's never pressed again without the person
    pressed_before: bool = False
    # Kept in the desk's saved queue (QUEUE_FILE), for a restart: its pass has begun (in this run of the desk, or one
    # before a restart: such a job comes back on a card, never queued by itself); the person pressed Submit for it and
    # that Submit hasn't gone yet (waiting its turn, or under way); its Submit is being pressed this moment (a restart
    # meanwhile never queues it again: it may have gone); and its pass was cut short by the desk stopping or the browser
    # closing, not by anything of its own. After a restart: left for the person on a card with no tab (RESTART_BEGUN,
    # or RESTART_CHECK for a Submit pressed then that may have gone in), whose Resume fills it again and never sends it;
    # and a Submit the person pressed before it, which wasn't sent (its review card says to press it again)
    started: bool = False
    submit_pressed: bool = False
    sending: bool = False
    cut_short: bool = False
    submit_unsent: bool = False
    left_at_restart: bool = False
    # Its reload for "Something went wrong" brought back only the site's header and footer, and that was said: a Resume
    # on that page, still so, is said the same (Northern Trust's and Axcelis' Workday, runs 88 and 90)
    came_back_empty: bool = False
    # Sections ("work", "education") whose Add button stayed covered with fewer blocks on the page than the profile
    # has entries, as (blocks there, entries wanted): the job stops for them (issue #112, note 32). What a card said
    # of them, and what a Resume was pressed on: an unchanged shortfall is gone on with once, noted on the review card
    entries_short: dict[str, tuple[int, int]] = field(default_factory=dict)
    entries_told: dict[str, tuple[int, int]] = field(default_factory=dict)
    entries_ok: dict[str, tuple[int, int]] = field(default_factory=dict)
    page_info: dict[str, Any] = field(default_factory=dict)  # what the page looked like when it paused
    # what went into the application, for its card's "What went in" panel on the desk page alone (WentIn)
    went_in: WentIn = field(default_factory=WentIn)
    # Filled at once with other jobs (settings.jobs_at_once): in a step that may email the person now (a sign-in, an
    # account, a password reset, an emailed code), which one job under way at a time is; the site its tab is on (a
    # job system's host, or an employer's own domain) and since when; and its place in the order the jobs began
    inbox: bool = False
    inbox_since: float = 0.0  # when its step that may email began, on the way to this pause: others' mail ends there
    site: str = ""
    site_since: float = 0.0
    began: int = 0
    # When each line of the log was logged (time.time(), 0 for one put there some other way), beside it and cut with it
    log_at: list[float] = field(default_factory=list)
    # For its lines in the desk's history (history.HISTORY_FILE): the steps the desk pressed the way on from in this pass
    # (_step_key's, each once), and how many times one of the desk's rounds took the person to it and they pressed its
    # Resume, since it came to the desk
    pass_steps: set[tuple] = field(default_factory=set)
    rounds: int = 0
    resumes: int = 0
    # gone in, but not sent by the desk: the person's "I submitted it", the tracker saying so already, or the site saying
    # it went in (its lines' outcome is "marked_applied", not "submitted")
    marked: bool = False
    # the jobs its "may be one already begun or applied to" card named (_held_for_maybe_same): the ones its Apply anyway
    # clears, never another (a restart asks again, the card worked out anew as it goes)
    maybe_shown: list[int] = field(default_factory=list)
    page: Any = None  # its browser tab
    updated: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {**{k: v for k, v in self.__dict__.items() if k not in ("page", "tab_mark", "desk_at", "desk_code", "cookies_asked", "resetting",
                                                                        "reset_asked", "reset_from", "reset_waited",
                                                                        "reset_at", "reset_set", "reset_opened",
                                                                        "reset_spent", "reset_spent_said", "account_known",
                                                                        "reset_no_mail", "reopened_elsewhere", "reopened", "sign_in_tries", "sign_in_key",
                                                                        "accounts_tried", "account_made", "account_in", "account_page",
                                                                        "account_finished", "verifying", "verify_resent", "mail_looks", "mail_last",
                                                                        "entries_short", "entries_told", "entries_ok",
                                                                        "account_answers", "employer_mail", "others_mail",
                                                                        "went_in", "draft_pages", "submit_pressed",
                                                                        "sending", "cut_short", "started",
                                                                        "pass_steps", "rounds", "resumes", "marked",
                                                                        "maybe_shown", "kept_replaced")},
                # (only for the cards that show it: a job queued or under way has none, and 30 of them made the
                # page's state megabytes, every poll)
                "went_in": None if self.status in ("queued", "running") else self.went_in.public()}


@dataclass
class _Pass:
    """What one pass of Applier._drive over a job's pages has done so far, lap by lap: made afresh as the pass begins
    (the job's tab open), and gone with it. The parts of the lap with methods of their own (_no_way_on, _after_a_stall)
    read it and add to it as the lap does."""
    # when the pass began (time.monotonic()), later by the waits that are no part of its own time: PASS_SECONDS from it
    began: float
    stalls: int = 0
    entries_done: set[tuple[Any, ...]] = field(default_factory=set)
    refilled: set[tuple[Any, ...]] = field(default_factory=set)
    dismissed: set[str] = field(default_factory=set)
    # the blocks the desk added on this pass, by kind, and the empty ones past the profile's entries whose Delete it
    # pressed (_drop_added_extra)
    blocks_added: dict[str, int] = field(default_factory=dict)
    filled_ids: set[str] = field(default_factory=set)  # the boxes (by id) the desk's own fills went into on this pass
    # and what they put there (norm), the person's once-answers too
    filled_values: set[str] = field(default_factory=set)
    extra_tried: set[str] = field(default_factory=set)
    stalled_on: tuple[Any, ...] = ()  # the step (address and headings) the stalls were counted on
    stalled_in_a_row: int = 0  # and the stalls since a press last moved a page on, whatever step they were on
    # pages waited on to draw their buttons late, once a page each time it's reached (Baker Tilly's Workday, run 44:
    # the step after its sign-in drew them late too, and once a pass left it unwaited)
    waited: set[tuple[Any, ...]] = field(default_factory=set)
    # pages (as drawn) waited on for a greyed-out step button
    step_waited: set[tuple[Any, ...]] = field(default_factory=set)
    retyped: set[tuple[Any, ...]] = field(default_factory=set)  # pages whose lost boxes were typed again (once a page)
    # pages whose emptied boxes were filled again (once a page)
    refilled_pages: set[tuple[Any, ...]] = field(default_factory=set)
    # pages whose boxes said wrong were answered again (once a page)
    redone_pages: set[tuple[Any, ...]] = field(default_factory=set)
    # steps gone back to for the boxes their errors name (once a step)
    asked_back: set[tuple[Any, ...]] = field(default_factory=set)
    back_from: tuple[tuple[Any, ...], str] | None = None  # the step gone back to, and its stop as it would be said
    saved_waited: set[tuple[Any, ...]] = field(default_factory=set)  # steps waited on as still saving (once a step)
    # forms with no step button yet, waited on to be drawn (once a page)
    bare_waited: set[tuple[Any, ...]] = field(default_factory=set)
    # steps reloaded for saying what they saved already exists (once a page)
    reloaded: set[tuple[Any, ...]] = field(default_factory=set)
    # pages a press led to loaded again for coming up blank, by address (_nothing_drawn)
    blank_loads: dict[str, int] = field(default_factory=dict)
    account_waited: bool = False
    sign_ins: dict[str, int] = field(default_factory=dict)  # what the saved password was used for on this pass
    pressed: list[tuple[Any, ...]] = field(default_factory=list)  # (page, button) for each button pressed on this pass
    # of those, the ones pressed on a page with no form (a list of jobs)
    on_pages: set[tuple[Any, ...]] = field(default_factory=set)
    pressed_on: list[str] = field(default_factory=list)  # and where, in words
    # notices agreed to for the person on this pass (settings.accept_notices)
    agreed: set[str] = field(default_factory=set)
    since: int = 0  # where the presses that count for a circle start (after a refresh the site asked for)
    refreshed_at: int | None = None  # the presses made on this pass when the page was reloaded for its "went wrong"
    fixed_at: int = 0  # and those that count as a step pressed again and again (after the desk set right what it said)
    steps: int = 0
    more: int = 0  # (more: the steps that refresh made it take again)


class _Then(Enum):
    """What Applier._drive's lap does after a part of it with a method of its own (_no_way_on, _after_a_stall):
    AGAIN looks at the page again (the lap's `continue`), STOP ends the pass (its `return`), and ON goes on with the
    rest of the lap."""
    AGAIN = "again"
    STOP = "stop"
    ON = "on"


def classify(data: dict[str, Any], text: str) -> str:
    """bot_check, sign_in, email_code, form or page."""
    if data.get("challenge"):  # a CAPTCHA's pictures over the page (iCIMS after its email step)
        return "bot_check"
    # (never the page's own language picker: Linde's Cornerstone requisition page, run 75, came up empty but for its
    # unlabelled language list, and was read as a form, so it was never reloaded)
    fields = [f for f in _app_fields(data) if not f.get("disabled")]
    if not fields and (_BOT_TITLE.search(data.get("title") or "") or _BOT_TEXT.search(text[:3000])
                       or data.get("captcha")):  # (a page with only a CAPTCHA's box on it, and its button)
        return "bot_check"
    if any(f["kind"] == "password" and not _code_box(f) for f in fields):
        return "sign_in"
    actions = [a.get("text", "") for a in data.get("actions", [])]
    if not fields and any(re.match(r"^sign in with ", a, re.I) for a in actions):
        return "sign_in"  # Workday: "Sign in with email / Google / Apple"
    if any(_code_box(f) for f in fields):
        return "email_code"
    if not fields and _VERIFY_EMAIL.search(text[:3000]) and not any(_way_in(final_text(a)) for a in actions):
        return "email_code"  # "we sent you a link to verify your account"
    return "form" if fields else "page"


def pick_next(actions: list[dict[str, Any]], in_form: bool) -> dict[str, Any] | None:
    """The button that moves the application on: a step button inside a form, otherwise
    the way into it ("Apply Manually" before "Apply")."""
    usable = [a for a in actions if not a.get("disabled") and not a.get("is_submit") and not a.get("same_page")]
    steps = [a for a in usable if _STEP.match(final_text(a["text"]))]
    entries = [a for a in usable if _ENTRY.match(final_text(a["text"])) and not _SOCIAL.search(a["text"])
               and not (_AVOID.search(a["text"]) and "manually" not in a["text"].lower())]
    # a saved draft's "Continue Application", on a page (never a form's step), where it's the page's one way in: beside an
    # Apply, or with others like it (a list of applications kept, each another job's, as a candidate home lists them),
    # it's not (issue #112, note 38's review). Whose draft it is, _drive asks (_own_draft)
    drafts = [a for a in usable if _DRAFT_ENTRY.match(final_text(a["text"]))]
    if not in_form and not entries and len(drafts) == 1:
        entries = drafts
    # an open menu's own entry ("Apply Now" under Qorvo's "Apply now ▾") before the toggle again
    entries.sort(key=lambda a: (not a.get("menu"), "manually" not in a["text"].lower()))
    order = (steps + entries) if in_form else (entries + steps)
    return order[0] if order else None


# A query parameter that's a session's or a request's, not the page's: by its name (a number under "token" is a job's
# id: Greenhouse's), or a time in milliseconds near now. (Not any long value: a step's own id can be one, Eightfold's
# pid is 13 to 15 digits, and two steps alike but for it are two steps: the review of 0.3.189)
_TOKEN_PARAM = re.compile(r"^(?:_|t|ts|time(?:stamp)?|nonce|csrf\w*|xsrf\w*|_?token|access_token|auth\w*|sid|"
                          r"session\w*|jsessionid|state|rnd|rand\w*|cache\w*|cb)$", re.I)


def _request_token(name: str, value: str) -> bool:
    """A query parameter a request carries, not its page: _TOKEN_PARAM's names, or a time in milliseconds."""
    if _TOKEN_PARAM.match(name):
        return not (name.lower().lstrip("_") == "token" and value.isdigit())
    return value.isdigit() and len(value) == 13 and abs(int(value) - time.time() * 1000) < 86_400_000


def _steady_url(url: Any) -> str:
    """A page's address as the same step keeps it: without a session's or a request's tokens (a fresh one on each
    visit made every press look new, so going round in a circle or pressing one step again and again was never seen:
    the review of Oct 15), nor ";jsessionid=" path parameters. The step's own parameters stay ("?step=2")."""
    text = str(url or "")
    try:
        parts = urlsplit(text)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _request_token(k, v)]
    except ValueError:
        return text
    return urlunsplit((parts.scheme, parts.netloc, re.sub(r";[^/]*", "", parts.path), urlencode(query), parts.fragment))


def _step_key(data: dict[str, Any], button: str, run: "Run") -> tuple:
    """A press of a step's button, as the circle and stall checks compare them: the step's address (without a
    request's tokens), its headings, the button, which visit to the job, and which questions it shows (a
    questionnaire drawing its next one in place is a step further on: the review of Oct 15)."""
    return (_steady_url(data.get("url")), tuple(data.get("headings") or []), button.strip().lower(), run.reopened,
            questions_shown(data.get("fields") or []))


def _fingerprint(page: dict[str, Any]) -> tuple:
    fields = page.get("fields")
    count = fields if isinstance(fields, int) else len(fields or [])
    # (which questions it shows: a click's summary says it in short, a page read lists them)
    questions = page.get("questions") if isinstance(fields, int) else questions_shown(fields or [])
    actions = tuple(a if isinstance(a, str) else a.get("text", "") for a in page.get("actions") or [])
    return _steady_url(page.get("url")), tuple(page.get("headings") or []), count, actions, questions


_TAILOR_SAYS = ("Waiting for a resume written for this job. In Claude Code, say \u201ctailor my resumes\u201d; the "
                "desk carries on with this job as soon as its resume is ready. Or use your usual resume.")
_BOT_CHECK_SAYS = ("The site is checking that you're a person (a bot check or CAPTCHA). Pass it in the browser "
                   "window: the desk then carries on with this job by itself, and the other jobs go on meanwhile "
                   "(the desk's Bot checks round takes you to each one waiting).")
_ERROR_COUNT = re.compile(r"^\d+ errors? found\.?$", re.I)  # a section's count, not what's wrong


def _count(n: int, noun: str) -> str:
    """'1 question', '3 questions'."""
    return f"{n} {noun}{'' if n == 1 else 's'}"

_ROBOT = re.compile(r"not a robot|captcha|verify (?:that )?you(?:'re| are) (?:a )?human", re.I)
# A page that still asks the person to prove they're one (not a "protected by reCAPTCHA" badge's words): a bot
# check waiting on them isn't past it while it does (a form's "please verify you're not a robot" error, say)
_STILL_CHECKING = re.compile(r"(?:verify|confirm|prove) (?:that )?you(?:'re| are) (?:a )?(?:human|not a robot)|"
                             r"i'?m not a robot|complete the (?:captcha|security check|challenge)|"
                             r"captcha (?:is )?(?:required|incorrect|failed)|solve the (?:captcha|puzzle)", re.I)
_ERRORISH = re.compile(r"error|required|invalid|please|must|enter |select |missing|problem|fix|can'?t be blank", re.I)
# a note that only asks the person to look over what's there, which nothing on the page is wrong for: Nikon's UKG
# Pro after its resume parse (live run 16, Oct 11), "jobdesk-test-resume.pdf has been used to pre-fill part of
# your application. Please verify that all of your information is correct."
_ADVISORY = re.compile(r"has been used to (?:pre-?fill|populate)|^\W*please (?:verify|review|check|confirm) (?:that )?"
                       r"(?:all (?:of )?)?your (?:information|details) (?:is|are) (?:correct|accurate)\W*$", re.I)


def _errorish(said: str) -> bool:
    """Words the page shows that say something on it is wrong (not a note to look things over)."""
    # sentence by sentence, so a real error in the same message as the note still counts
    return any(_ERRORISH.search(part) and not _ADVISORY.search(part) for part in re.split(r"(?<=[.!?])\s+", said))


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
    out = [e for e in data.get("errors") or [] if _errorish(e)]
    for a in data.get("actions") or []:
        m = re.match(r"^error\s*-\s*(.+)$", (a if isinstance(a, str) else a.get("text", "")).strip(), re.I)
        if m:
            out.append(f"\u201c{_short(m.group(1))}\u201d needs fixing")
    fields = data.get("fields")  # a click's summary carries only how many there are
    # (a question in a few words: Maricopa County's salary question, whole, left "is mar" of what was said, run 49)
    out += [f"\u201c{_short(f.get('label') or '')}\u201d is marked invalid"
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
    """Works through queued jobs in the background: up to settings.jobs_at_once at a time (the desk's "Jobs at once"),
    each in a task and a browser tab of its own.

    What runs at once: the jobs' passes (_run_task), each held to its own tab (BrowserSession.desk_view). What stays
    one at a time: the jobs on one site (a job system's host, or an employer's own: _site_free, _site_holder), the
    steps that may email the person (a sign-in, an account, a password reset, an emailed code: _take_inbox), and the
    person's own attention: while a pause needs their hands (Run.blocking) or they're in one of the desk's rounds, no
    job starts, and no other tab comes to the front (_bring_forward, keep_front); the jobs under way go on in theirs."""

    def __init__(self, srv: Any, keep_queue: bool = False):
        self.srv = srv  # the server module: its tools, tracker() and browser
        # The Job Desk's own queue is kept in QUEUE_FILE as it changes, and picked up again as the desk starts after a
        # restart (keep_queue: the desk's; the live check's appliers and a test's own keep none). Whether this desk keeps
        # it (QUEUE_LOCK, taken as it starts: None until then, and nothing is written before it's read back), or another
        # desk does (queue_elsewhere); what was written last (the same queue isn't written again); and whether the desk
        # is stopping: what that cuts short stays as it was kept, under way. The jobs queued again as it started (they
        # wait for the person's Resume queue, said on the desk until then), and how old their kept queue was (days)
        self.keep_queue = keep_queue
        self._queue_owner: bool | None = None
        self.queue_elsewhere = self.queue_unlockable = False
        self._lock_refused = False  # (once refused: a queue picked up later is a takeover's, the desk's own going on)
        self._lock_tried = 0.0
        self._queue_restored = self._restoring = False
        self._queue_said = ""
        self._queue_since: float | None = None  # (when the queue kept now began: its age outlasts the restarts)
        self._closing = False
        self._restored_ids: set[int] = set()
        self._restored_days = 0
        self._taken_over = False
        self.runs: dict[int, Run] = {}
        self.tasks: deque[tuple[str, int]] = deque()
        self.auto_submit = False
        self.hold_submits = False  # the desk's troubleshooting is on: nothing is sent, by the person's Submit or for them
        # the openings the desk lists (set by the desk): one marked Not interested before its job was saved is that job's
        # only when nothing listed makes it ambiguous (Tracker.not_interested_marks)
        self.listed: Callable[[], list[dict[str, Any]]] = lambda: []
        self.tailor = False  # hold each job until Claude has written a resume for it
        # The person pressed Pause queue: no new job starts until Resume queue (in memory, like the queue it holds),
        # but those they pressed Resume on, or answered on their card (pressed)
        self.paused = False
        self.pressed: set[int] = set()
        self.jobs_at_once: int | None = None  # the desk's own "Jobs at once" (desk.json); None: the profile's setting
        # the jobs under way, each in a task of its own (_run_task), and how many have begun (their order: Run.began)
        self._slots: dict[int, asyncio.Task] = {}
        self._began = 0
        # the jobs whose steps hold the browser's tools now (Claude's acting tools wait meanwhile: current)
        self._driving: list[int] = []
        # jobs that paused for the person's hands while they were at another job's: theirs next, its tab brought up then
        self._behind: set[int] = set()
        # the jobs under way waiting between their steps (_hold_for_site, _take_inbox)
        self._waiting: set[int] = set()
        # how many times a job has taken the inbox: a look in it begun before another did isn't used (_check_mail)
        self._inbox_gen = 0
        # jobs that may go beside as many as go at once (jobs_at_once of 2 or more): the one whose check the person
        # just solved in the desk's round, as a Submit they pressed may
        self._first: set[int] = set()
        # The bot check the person is solving in the desk's round (Bot checks: "Solve them"), and since when: while
        # it waits, no other job is started (nothing opens over the tab they're at), and the solved one is noticed
        # at once. Not the person's otherwise: bot checks wait in their tabs while the queue goes on.
        self.round_job: int | None = None
        self.round_at = 0.0
        self._wake = asyncio.Event()
        self._worker_task: asyncio.Task | None = None
        self.mail_problem: str | None = None  # why the inbox couldn't be read, for the desk page
        self._mail_refused: str | None = None  # the app password the mail service turned down
        # where each skipped job was, for Undo: its run's status ("" with no run yet) and its place in the queue
        self._before_skip: dict[int, tuple[str, int | None]] = {}

    # ------------------------------------------------------------- control
    def restore(self) -> None:
        """The desk starting: keep its queue from now on, unless another desk does (QUEUE_LOCK), and, at its first start,
        pick up the queue it kept before a restart (_restore_queue). Before it serves its page (Desk.start): a call from
        a page left open must never meet the queue before it's back, nor write it over."""
        self._closing = False
        if not self.keep_queue:
            return
        if not self._queue_owner:
            taken = _take_queue_lock()
            self._lock_tried = time.monotonic()
            self._queue_owner = taken == "mine"
            self.queue_elsewhere, self.queue_unlockable = taken == "theirs", taken == "none"
            self._lock_refused = self._lock_refused or not self._queue_owner
        if self._queue_owner and not self._queue_restored:
            self._queue_restored = True
            self._restore_queue(takeover=self._lock_refused)
        self._save_queue()  # (as it is now: anything queued before it was back is kept from here on)

    def queue_note(self) -> str | None:
        """Said on the desk while its queue isn't kept: another desk keeps it, or the lock can't be had here."""
        return QUEUE_ELSEWHERE if self.queue_elsewhere else QUEUE_NO_LOCK if self.queue_unlockable else None

    def _retry_queue_lock(self) -> None:
        """While another desk keeps the queue, look again about every QUEUE_LOCK_RETRY seconds: once it has ended, this
        one keeps the queue, and picks up what it kept (held for the person, this desk's own queue going on)."""
        if (self.keep_queue and self.queue_elsewhere and not self._closing
                and time.monotonic() - self._lock_tried >= QUEUE_LOCK_RETRY):
            self.restore()

    def start(self) -> None:
        self.restore()
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.get_running_loop().create_task(self._worker())
        # (a tab a job filled meanwhile opens never stays over the one the person is at for another)
        with contextlib.suppress(AttributeError):
            self.srv.browser.keep_front = self._front_tab

    async def stop(self) -> None:
        # the queue kept as it stands, the jobs under way among it: what stopping cuts short is picked up again at the
        # next start, so nothing is kept from here on (each job cut short would be kept as failed)
        self._save_queue()
        self._closing = True
        # (cancelled until it has ended: a cancellation dropped on the way (Python 3.10's wait_for drops one that
        # comes as what it waits on ends) left the worker going on for ever, and this waiting on it, CI's 3.10
        # run of 0.3.188). The worker first, so it starts no job meanwhile, then the jobs under way
        for task in [self._worker_task, *self._slots.values()]:
            if task is None:
                continue
            while not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=1)
            if not task.cancelled():
                with contextlib.suppress(Exception):
                    task.result()
        self._worker_task = None
        self._slots.clear()
        with contextlib.suppress(AttributeError):
            if self.srv.browser.keep_front == self._front_tab:
                self.srv.browser.keep_front = None
        if self._queue_owner:  # (another desk may keep it now: this one takes it again as it starts, if it can)
            self._queue_owner = None
            _give_back_queue_lock()

    @property
    def current(self) -> int | None:
        """A job whose step holds the browser's tools now (the last to take them), if any: Claude's tools that act in
        the browser wait meanwhile (server._desk_driving)."""
        return self._driving[-1] if self._driving else None

    @current.setter
    def current(self, job_id: int | None) -> None:
        self._driving = [] if job_id is None else [job_id]

    def at_once(self) -> int:
        """How many jobs are filled at once: the desk's own choice, else settings.jobs_at_once (1 to 3)."""
        if self.jobs_at_once is not None:
            return min(max(self.jobs_at_once, 1), config.JOBS_AT_ONCE_MAX)
        try:
            return config.Profile.load().settings.jobs_at_once
        except Exception:  # (a typo in profile.yaml: said on the desk; the queue goes on, a job at a time meanwhile)
            return 1

    def enqueue(self, job_id: int, submit: bool = False, front: bool = False) -> Run:
        job = self.srv.tracker().get(job_id)
        if job is None:
            raise KeyError(f"No job with id {job_id}")
        run = self.runs.get(job_id)
        if job.get("status") in FINISHED or run is not None and run.status == "submitted":
            # applying again could send a second application ("Submit for me")
            raise ValueError(f"{job.get('title') or 'That job'} is already marked {job.get('status') or 'submitted'}")
        if job.get("status") == "skipped":  # never applied to until the person's Undo
            raise ValueError(f"{job.get('title') or 'That job'} is skipped. Press Undo on it first.")
        # nor one the person said Not interested in on the desk (by its number or any of its addresses), however it
        # comes: Apply, a Resume, a queue picked up from another desk
        if self.srv.tracker().marked_not_interested(job_id=job_id, listed=self.listed()) is not None:
            raise ValueError(f"{job.get('title') or 'That job'} is marked Not interested. Press Undo on it (under Not "
                             "interested) first.")
        run = run or Run(job_id, job.get("title", ""), job.get("company", ""))
        self.runs[job_id] = run
        if run.status == "running" and (job_id in self._slots or job_id in self._driving):
            return run
        run.status, run.need, run.reason, run.blocking, run.questions = "queued", "", "", False, []
        run.left = run.cut_short = run.left_at_restart = False
        self._behind.discard(job_id)  # (its pause, waiting to be brought up, is over)
        # a Submit pressed earlier with no confirmation (in this run of the desk or before it was
        # restarted) may have sent the application: Submit for me doesn't press it again
        run.pressed_before = run.pressed_before or self._press_recorded(job)
        run.submit = submit and not run.pressed_before
        run.updated = time.time()
        self._cancel(job_id)
        (self.tasks.appendleft if front else self.tasks.append)(("apply", job_id))
        self._wake.set()
        self._save_queue()
        return run

    def _press_recorded(self, job: dict[str, Any]) -> bool:
        """Was Submit pressed for this job with no confirmation showing: its folder's record (submission.json), or the
        tracker's note? It may have gone."""
        return _pressed_before(job) or any(str(e.get("note") or "").startswith("pressed Submit;")
                                           for e in self.srv.tracker().events(job["id"]))

    def resume(self, job_id: int) -> Run:
        """The person pressed Resume. On a sign-in card, a saved password the site refused gets one more
        press: they may have reset the password to it by hand, as the card asks. One for each such Resume;
        none for a Resume on any other card (a question, a stuck page), nor when the queue carries on with
        the job by itself."""
        run = self.runs.get(job_id)
        signing_in = run is not None and run.need == "sign_in"  # (before enqueue clears it)
        if run is not None and run.entries_told:
            run.entries_ok = dict(run.entries_told)  # (a card said a section was short: gone on with, if unchanged)
        if run is not None and run.need in ("stuck", "sign_in", "bot_check"):
            run.load_reloaded = True  # (what they did in the browser is never reloaded away: a loading page is said)
        if run is not None:
            run.hands_on = True  # (on any card: a questions card's Resume is for answers typed into the page itself)
            self._draft_off(run)  # (and a saved draft's boxes are theirs too from here on)
        run = self.enqueue(job_id, submit=bool(run and run.submit), front=True)
        run.resumes += 1  # (for its lines in the desk's history)
        self.go_on_for(run)
        self.pressed.add(job_id)  # (the person's own press: it goes even while the queue is paused)
        if signing_in:
            run.sign_in_tries = min(run.sign_in_tries, SIGN_IN_TRIES - 1)
        return run

    def go_on_for(self, run: Run) -> None:
        """The person pressed Resume on this job, or answered its card, while another holds the queue for them (a
        sign-in, an emailed code): this one goes now, and the other is left to be watched between jobs (as its Later
        leaves it), unless someone is at work in its tab (anything there in the last RELEASE_IDLE seconds: the keys
        they type mustn't land in a tab this job brings forward) or the desk is reading its code from the inbox
        itself (a code only looked for between jobs could run out meanwhile). Then this one waits, and says why (the
        reviews of Oct 15 and 0.3.189)."""
        for other in [r for r in self.runs.values() if r.blocking and r.job_id != run.job_id]:
            reading = (other.need == "email_code" and not other.mail_done and self.mail_login() is not None
                       and _mail_waited(other) < MAIL_WINDOW)
            # (a tab still a minute is looked at every SLOW_POLL_SECONDS: work begun in it since may not be seen yet)
            if reading or time.time() - other.active_at < RELEASE_IDLE + SLOW_POLL_SECONDS:
                what = "its emailed code, which the desk is reading from your inbox" if reading else \
                    "you, at work in its tab"
                run.reason = (f"Waiting: {other.company or 'another job'} holds the queue for {what}. This one goes "
                              "as soon as that's done.")
                continue
            self.later(other.job_id)
            self._log(other, "the queue went on: you pressed Resume on (or answered) another job. This one's tab is "
                      "watched between jobs, and the desk carries on with it once it's past this step.")

    def submit_now(self, job_id: int) -> Run:
        """The person pressed Submit for a job that's waiting on its review page."""
        run = self.runs[job_id]
        if self.hold_submits:
            raise ValueError("Troubleshooting is on, so nothing is sent. Turn it off on the desk to send this one.")
        if run.status not in ("ready", "needs_you"):
            raise ValueError(f"Job {job_id} isn't waiting on a review page")
        run.status, run.reason = "queued", "Submitting…"
        run.submit_pressed, run.submit_unsent = True, False  # (pressed again, after a restart that didn't send it)
        self.tasks.appendleft(("submit", job_id))
        self._wake.set()
        self._save_queue()
        return run

    def wake(self) -> None:
        """Look again now (a tailored resume was just written)."""
        self._wake.set()

    def pause_queue(self) -> None:
        """The person pressed Pause queue: the job under way finishes its pass, and none starts after it
        until Resume queue (issue #112, note 36: a queue of about 90 had no way to stop but quitting Claude
        Code, which broke the job in progress). A Submit they press still goes, and the tabs left waiting
        are still watched. Kept with the queue: a restarted desk is still paused."""
        self.paused = True
        self._save_queue()

    def resume_queue(self) -> None:
        self.paused = False
        self._restored_ids.clear()  # (the jobs back from before a restart go on: said no more)
        self._wake.set()
        self._save_queue()

    def restored_notice(self) -> str | None:
        """Said on the desk while jobs queued again after a restart (or picked up from a desk that has ended) wait for
        the person's Resume queue, with how many of them Submit for me sends once they're filled."""
        held = [job_id for kind, job_id in self.tasks if kind == "apply" and job_id in self._restored_ids]
        n = len(held)
        if not n:
            return None
        kept = f" (kept {self._restored_days} days ago)" if self._restored_days > QUEUE_STALE_DAYS else ""
        whose = (" the Job Desk before this one kept (it has ended)" if self._taken_over else
                 " from before the desk restarted")
        said = (f"{n} job{'s' if n != 1 else ''}{whose}{kept} {'are' if n != 1 else 'is'} back in the queue, "
                f"{'held' if self._taken_over else 'paused'}. Press Resume queue to go on.")
        sends = sum(1 for job_id in held if self.runs[job_id].submit) if self.auto_submit and not self.hold_submits else 0
        if sends and n == 1:
            said += " It goes in by Submit for me once it's filled; turn Submit for me off first if you'd rather check it."
        elif sends:
            goes, filled = ("goes", "it's") if sends == 1 else ("go", "they're")
            said += (f" {sends} of them {goes} in by Submit for me once {filled} filled; turn Submit for me off first if "
                     "you'd rather check each.")
        return said

    def _held_back(self, task: tuple[str, int]) -> bool:
        """A job queued again after a restart (or from a desk that has ended), waiting for the person's Resume queue."""
        return task[0] == "apply" and task[1] in self._restored_ids and task[1] not in self.pressed

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

    def apply_anyway(self, job_id: int) -> Run:
        """The person's Apply anyway on a job that may be one they began or applied to on another site
        (_maybe_done_elsewhere): it isn't, for this job only (the other is still asked about), and it goes on now."""
        run = self.runs.get(job_id)
        if run is None or run.status != "needs_you" or run.need != "maybe_duplicate":
            raise ValueError("That job isn't waiting on that question any more.")
        # (only the jobs its card named: one begun since, which the card didn't name, is asked about on its own card)
        self.srv.tracker().clear_maybe_same(job_id, run.maybe_shown)
        self._log(run, "Apply anyway: you said it isn't the job begun or applied to on the other site")
        run = self.enqueue(job_id, submit=run.submit, front=True)
        self.go_on_for(run)
        self.pressed.add(job_id)  # (the person's own press: it goes even while the queue is paused)
        return run

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
        _forget_draft_changes(self.srv, job_id)  # (gone in: a draft's changes kept for it go)
        run = self.runs.get(job_id)
        if run is not None:
            was = run.status
            run.status, run.need, run.blocking, run.reason = "submitted", "", False, "Marked as applied."
            run.left = run.submit_unsent = run.left_at_restart = False
            # (one the desk sent stays its own, as does one its press may have sent: no confirmation came)
            run.marked = run.marked or was != "submitted" and not run.pressed_before
            if was != "submitted" and job_id not in self._slots:  # (one under way: its pass's line says it)
                self._note_history(run, "marked_applied" if run.marked else "submitted")
            self._wake.set()
        self._save_queue()

    async def skip(self, job_id: int, closed: str = "") -> Run:
        """The person's Skip, or, with `closed` (what the site says, in its words), the desk's own Skip of a job whose
        posting has closed (issue #112, note 39: nothing about it needs the person). Undo puts either back."""
        if self.srv.tracker().get(job_id, with_description=False) is None:
            raise KeyError(f"No job with id {job_id}")  # a stale page: no entry is made for it
        run = self.runs.get(job_id)
        was = run.status if run else ""
        if run is None or run.status != "skipped":  # (a second Skip keeps where it was before the first)
            place = next((i for i, task in enumerate(self.tasks) if task == ("apply", job_id)), None)
            self._before_skip[job_id] = (run.status if run else "", place)
        run = run or Run(job_id)
        self.runs[job_id] = run
        self._cancel(job_id)
        run.status, run.need, run.blocking, run.reason = "skipped", "", False, "Skipped. Undo puts it back."
        run.closed_skipped = bool(closed)
        if closed:
            run.reason = f"Skipped: posting closed. {closed} Undo puts it back."
        run.account_answers = {}  # (an account form's answers from its card, for that form alone: never kept)
        run.submit_unsent = run.left_at_restart = False
        self._log(run, f"skipped by itself: posting closed ({closed})" if closed else "skipped")
        self.srv.tracker().update(job_id, status="skipped", note="skipped by the Job Desk: posting closed" if closed
                                  else "skipped in the Job Desk")
        # (a job on the desk, not under way: one under way has its pass's line, which says it)
        if was not in ("", "skipped") and job_id not in self._slots:
            self._note_history(run, "skipped")
        self._save_queue()
        for tab in self.srv.browser.lineage(run.page):  # its application tab, and the tab that opened it
            if not tab.is_closed():
                try:
                    await tab.close()
                except Exception:
                    pass
        # run.page stays, closed: put back by Undo, the job opens again in a new tab, and says why it starts over
        return run

    async def unskip(self, job_id: int) -> Run | None:
        """The person's Undo on a skipped job. The tracker gets back the status the job had before
        (its history keeps it), and the job goes back where it was: one the desk had begun goes back
        in the queue (at its place, if it was waiting there), opened again in a new tab when Skip
        closed its own; one it hadn't, back to the list, unselected. Its log, notes and answers stay.
        Undo never sends anything: the job goes back with Submit for me off for it (it stops at its
        review page for the person's Submit), and one sent or marked applied isn't queued again. A
        job that isn't skipped is left as it is."""
        tracker = self.srv.tracker()
        if tracker.get(job_id, with_description=False) is None:
            raise KeyError(f"No job with id {job_id}")
        run = self.runs.get(job_id)
        # Skipped while the worker was still on it (saving the page it paused on, or mid-step): its
        # step ends first, or it would carry on in the tab Skip closed
        deadline = time.monotonic() + UNSKIP_WAIT
        while run is not None and run.status == "skipped" and (job_id in self._driving or job_id in self._slots):
            if time.monotonic() > deadline:
                raise ValueError("That job is still stopping after its Skip. Press Undo again in a moment.")
            await asyncio.sleep(0.1)
        tracker.unskip(job_id, note="skip undone in the Job Desk")
        before, place = self._before_skip.pop(job_id, (None, None))
        try:
            return self._put_back(job_id, run, before, place)
        finally:
            self._save_queue()

    def _put_back(self, job_id: int, run: Run | None, before: str | None, place: int | None) -> Run | None:
        """Undo's second half: the job back where it was before its Skip (unskip)."""
        if run is None or run.status != "skipped":
            return run
        if run.closed_skipped:  # (the desk's own Skip of a closed posting, undone: that posting is the person's now)
            run.closed_skipped, run.closed_undone = False, True
        if before == "":  # skipped before the desk began it
            del self.runs[job_id]
            return None
        if self._already_done(run):  # marked applied (or past that): never queued again
            return run
        if before == "submitted":
            run.status, run.reason = "submitted", "Skip undone. It went in before, so it isn't applied to again."
            self._log(run, run.reason)
            return run
        self._log(run, "Skip undone: back in the queue")
        run.in_their_hands = run.in_their_hands or run.started  # (its tab was theirs before the Skip)
        self.enqueue(job_id)  # (submit=False: Undo is no Submit)
        if place is not None:
            self._cancel(job_id)
            self.tasks.insert(min(place, len(self.tasks)), ("apply", job_id))
        return run

    async def focus(self, job_id: int) -> bool:
        run = self.runs.get(job_id)
        if run is None or run.page is None or self.srv.browser.lost(run.page):
            return False
        await run.page.bring_to_front()
        return True

    def _cancel(self, job_id: int) -> None:
        if ("submit", job_id) in self.tasks and job_id in self.runs:
            self.runs[job_id].submit_pressed = False  # (the person's Submit, waiting its turn, is gone with its task)
        self.tasks = deque(t for t in self.tasks if t[1] != job_id)
        self.pressed.discard(job_id)  # (a press for a task that's gone isn't one for the next: the re-check of 0.3.196)
        self._first.discard(job_id)  # (and a solved check's place beside the others is that task's alone)

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
            if run.job_id in self._slots:  # (its pass isn't over: the page it stopped on is being saved in its tab)
                continue
            if run.need == "email_code":
                await self._check_mail_safely(run)  # the code or link came after the queue went on
            if self._reset_overdue(run):
                run.reset_no_mail = True  # none it could use: _drive makes an account, or says so
                moved = True
            else:
                try:
                    moved = await self._past_pause(run)
                except Exception:  # a tab mid-way through loading: looked at again next time, not holding the rest
                    continue
            if moved and run.left and run.status == "needs_you":
                # (carried on by itself: the person may have changed later pages of a saved draft by hand in its tab,
                # or put a file of their own in its upload)
                self._draft_off(run)
                run.in_their_hands = True
                solved = run.job_id == self.round_job  # (its check solved in the desk's round, the person at its tab)
                try:
                    self.enqueue(run.job_id, submit=run.submit, front=True)
                    self.pressed.add(run.job_id)  # (it was under way: it carries on, Pause queue or not, as in _tick)
                    if solved:
                        self._first.add(run.job_id)
                except (KeyError, ValueError):  # gone, or marked applied meanwhile: nothing to watch for
                    run.left = False

    async def _past_pause(self, run: Run) -> bool:
        """Is a job's tab, left waiting for the person, past its sign-in, check or code? Read
        without taking over the tools' tab (Claude may be using it), and only while it's on the
        address it paused on: a tab the person has taken to another posting, even one on the
        same job system, is no application of this job's."""
        data, text = await self.srv.browser.peek(run.page)
        self._note_activity(run, data)
        return self._twice(run, self._looks_past(run, data, text))

    # ------------------------------------------------------------- kept across a restart
    def _kept(self, run: Run) -> bool:
        """Is this job among the queue kept for a restart, outside the queue's own tasks: under way (or its pass cut
        short by the desk stopping or the browser closing), waiting on the person, or with a Submit pressed for it?"""
        if run.status in ("submitted", "skipped"):
            return False
        return (run.job_id in self._slots or run.status in ("running", "needs_you", "ready") or run.sending
                or run.submit_pressed or run.status == "failed" and run.cut_short)

    def _saved_queue(self) -> dict[str, Any]:
        """The queue as QUEUE_FILE keeps it: the jobs begun (under way, or waiting on the person) in the order they
        began, then the queue's in its order; for each only its number, where it was ("queued" for one whose
        application was never begun: waiting its turn, or for a tailored resume, said so), whether Submit for me was on
        for it (for one never begun: the only kind it goes on for after a restart), whether the person had pressed
        Submit for it, and whether a Submit pressed for it may have gone in; and since when the queue has been kept (its
        age outlasts the restarts, until it empties). Nothing the person typed or a page said."""
        in_line = list(dict.fromkeys(job_id for _, job_id in self.tasks))
        waiting = set(in_line)
        begun = sorted((r for r in self.runs.values() if r.job_id not in waiting and self._kept(r)),
                       key=lambda r: (r.began, r.job_id))
        jobs: list[dict[str, Any]] = []
        for job_id in [r.job_id for r in begun] + in_line:
            run = self.runs.get(job_id)
            if run is None:
                continue
            state = ("queued" if not run.started else
                     "waiting" if run.status in ("needs_you", "ready") else "under_way")
            entry: dict[str, Any] = {"id": job_id, "state": state}
            if run.submit and state == "queued":
                entry["submit"] = True
            if state == "queued" and run.status == "needs_you" and run.need == "tailor":
                entry["tailor"] = True
            if run.submit_pressed or run.submit_unsent:  # (and one not sent before an earlier restart: said still)
                entry["pressed"] = True
            if run.sending or run.pressed_before:
                entry["may_have_gone"] = True
            jobs.append(entry)
        if not jobs:
            self._queue_since = None  # (emptied: the next queue's age starts with it)
            return {"paused": self.paused, "jobs": jobs}
        if self._queue_since is None:
            self._queue_since = time.time()
        return {"paused": self.paused, "jobs": jobs, "since": self._queue_since}

    def _save_queue(self) -> bool:
        """Keep the queue (QUEUE_FILE) as it is now, whole or not at all, if this desk keeps it (only once it's been
        read back: Applier.restore). Whatever goes wrong, the queue goes on. False when it couldn't be kept."""
        if not self.keep_queue or not self._queue_owner or self._closing or self._restoring:
            return True
        try:
            text = json.dumps(self._saved_queue())
            if text == self._queue_said:
                return True
            path = queue_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            config.write_text_atomic(path, text)
            self._queue_said = text
            return True
        except Exception:
            logging.getLogger(__name__).warning("couldn't keep the desk's queue", exc_info=True)
            return False

    def _restore_queue(self, takeover: bool = False) -> None:
        """The desk starting after a restart, with the queue it kept (QUEUE_FILE). The jobs never begun are queued again
        in their order, each with Submit for me only if it had it and it's still on, as for any job queued now (one
        waiting for a tailored resume waits for it again); and the queue comes back paused, said on the desk, so none
        starts without the person's Resume queue (a queue kept weeks ago too, said with its age). A job begun before
        the restart (under way, or waiting on the person) isn't queued again: the person may have worked in its tab,
        even sent it on the site, so its card asks them to say so, and its Resume fills it again up to its review page,
        never sending it; a Submit they pressed then wasn't sent (its card and its review card say so). One whose Submit
        was pressed and may have gone in (its folder's record, the tracker's note, or a press under way as the desk
        stopped) is said so on its card. A job gone in since (applied, or past that), skipped, or no longer in the
        tracker is left out. Once back, they take their slots as any job does (Jobs at once).

        `takeover`: the queue of another desk that has ended, picked up by this one as it runs: only the jobs it adds
        wait for the person's Resume queue, this desk's own going on."""
        saved = _read_queue()
        if saved is None:
            return
        held_before = set(self._restored_ids)
        self._restoring = True  # (the kept queue isn't written over until it's all back)
        try:
            try:
                entries = self._merged_entries(saved["jobs"])
            except Exception:  # (the queue comes back as it was kept)
                logging.getLogger(__name__).warning("couldn't look for jobs made one in the kept queue", exc_info=True)
                entries = saved["jobs"]
            for entry in entries:
                try:
                    self._restore_job(entry)
                except Exception:  # (one job that won't come back never keeps the others out)
                    logging.getLogger(__name__).warning("couldn't pick job %s up again", entry.get("id"), exc_info=True)
        finally:
            self._restoring = False
        if saved["jobs"]:
            self._queue_since = min(t for t in (self._queue_since, saved["since"]) if t is not None)
        self._restored_days = int(max(time.time() - saved["since"], 0.0) // 86400)
        self._taken_over = takeover and self._restored_ids != held_before
        if not takeover:
            self.paused = self.paused or saved["paused"] or bool(self._restored_ids)
        self._wake.set()
        self._save_queue()

    def _merged_entries(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The kept queue's entries with one posting saved twice (from Indeed and the employer's site, say), made one
        job as the tracker opened, named by the job kept: once, in the first one's place. Submit for me only if each
        entry for it had it, and the job kept was never begun (begun on the job board, it may have gone in there); a
        Submit pressed, or one that may have gone in, for either holds for it, and so does either being begun."""
        t, out = self.srv.tracker(), dict[int, dict[str, Any]]()
        for entry in entries:
            into = t.merged_into(entry["id"]) if t.get(entry["id"], with_description=False) is None else None
            if into is not None:
                kept = t.get(into, with_description=False) or {}
                entry = {**entry, "id": into, "submit": entry.get("submit") is True and kept.get("status") == "saved"}
            was = out.get(entry["id"])
            if was is None:
                out[entry["id"]] = entry
                continue
            both = {**was, "submit": was.get("submit") is True and entry.get("submit") is True}
            for flag in ("pressed", "may_have_gone", "tailor"):
                if entry.get(flag) is True:
                    both[flag] = True
            if entry.get("state") != "queued":
                both["state"] = entry.get("state")
            out[entry["id"]] = both
        return list(out.values())

    def _restore_job(self, entry: dict[str, Any]) -> None:
        job_id, t = entry["id"], self.srv.tracker()
        if job_id in self.runs:  # (queued already, since the desk started)
            return
        job = t.get(job_id, with_description=False)
        if job is None or job.get("status") in FINISHED or job.get("status") == "skipped":
            return
        pressed = entry.get("pressed") is True
        if entry.get("may_have_gone") is True or self._press_recorded(job):
            card, pressed = RESTART_CHECK, False
            said = "the desk restarted after Submit was pressed for it, so it wasn't queued again: it may have gone in"
        elif entry.get("state") != "queued" or pressed:
            card = RESTART_BEGUN + (" Your Submit from before the restart wasn't sent." if pressed else "")
            said = "the desk restarted before it was finished, so it wasn't queued again: it's on its card for you"
        else:
            if t.marked_not_interested(job_id=job_id, listed=self.listed()) is not None:  # (said Not interested since)
                return
            # (never begun: queued again, Submit for me only if it had it and it's still on; held for Resume queue)
            submit = entry.get("submit") is True and self.auto_submit and not self.hold_submits
            self._restored_ids.add(job_id)
            if entry.get("tailor") is True and self.tailor and not tailored_ready(job):
                # (waiting for its tailored resume still: listed for Claude to write it, queued once it's there)
                run = Run(job_id, job.get("title") or "", job.get("company") or "", status="needs_you", need="tailor",
                          reason=_TAILOR_SAYS, submit=submit, paused_at=time.time())
                self.runs[job_id] = run
            else:
                run = self.enqueue(job_id, submit=submit)
                run.reason = "Picked up again after the desk restarted: waiting for Resume queue."
            self._log(run, "picked up again after the desk restarted")
            return
        run = Run(job_id, job.get("title") or "", job.get("company") or "", status="needs_you",
                  need="check_submit" if card == RESTART_CHECK else "restarted", reason=card,
                  pressed_before=card == RESTART_CHECK, left_at_restart=True, started=True, submit_unsent=pressed,
                  paused_at=time.time())
        self.runs[job_id] = run
        self._log(run, said)

    # ------------------------------------------------------------- history
    def _note_history(self, run: Run, outcome: str, began: float | None = None, kind: str = "",
                      at_once: int | None = None, need: str | None = None, reason: str | None = None) -> None:
        """A line in the desk's history (history.HISTORY_FILE): for a pass at this job that has ended (`began`: when it
        began, `kind` its task's; `need` and `reason` as it ended, where they've changed since), or for the person's Skip
        or I submitted it while it wasn't under way (no pass: no minutes). The job's number, employer, title and site; how
        it ended and why, in the desk's own words with anything the person or the inbox gave taken out, as far as can be
        told (_history_hidden); the steps it went through, its rounds and Resumes, and Jobs at once. Whatever goes wrong,
        the job goes on."""
        try:
            ended = time.time()
            company, title, url = run.company, run.title, run.url
            if not (company and title and url):  # (skipped before it was ever begun, say)
                job = self.srv.tracker().get(run.job_id, with_description=False) or {}
                company, title, url = (company or job.get("company") or "", title or job.get("title") or "",
                                       url or job.get("url") or "")
            entry: dict[str, Any] = {"job_id": run.job_id, "company": company, "title": title, "host": _host(url)}
            if began is not None:
                entry.update(started=history.stamp(began), ended=history.stamp(ended),
                             minutes=round(max(ended - began, 0.0) / 60, 1))
            else:
                entry["ended"] = history.stamp(ended)
            entry["outcome"] = outcome
            if outcome == "needs_you":
                entry["need"] = run.need if need is None else need
            entry["reason"] = history.short_reason(run.reason if reason is None else reason, self._history_hidden(run))
            if began is not None and kind != "submit":  # (a Submit's pass presses no step of the form)
                entry["pages"] = len(run.pass_steps)
            entry.update(rounds=run.rounds, resumes=run.resumes,
                         jobs_at_once=at_once if at_once is not None else self.at_once())
            history.append(entry)
        except Exception:
            logging.getLogger(__name__).warning("couldn't note job %s in the desk's history", run.job_id, exc_info=True)

    @staticmethod
    def _history_hidden(run: Run) -> set[str]:
        """What a line of the history keeps out, should a reason quote a page that shows it: the desk's secrets (each job
        system's password, the inbox's), the codes the inbox gave for this job, the digest of the password it signed in
        with, every answer it has (the person's for this application alone, an account form's security answers, what
        went into the form) and the profile's own words (but its settings, its files' paths, and a saved answer's
        question and where it was given). Not the desk's own
        words for a box ("ticked", "Yes"): history.short_reason takes them out as whole words, 4 characters or more."""
        hidden = set(_secret_values()) | {mail[1] for mail, job_id in list(_MAIL_TAKEN.items()) if job_id == run.job_id}
        hidden |= _texts(run.once) | _texts(run.account_answers) | {run.sign_in_key}
        hidden |= {str(e.get("value") or "") for e in list(run.went_in.answers.values())}
        try:
            data = config.Profile.load().data
            # (a saved answer's question, its core and the employer it was given at are no answer of the person's)
            hidden |= _texts({k: v for k, v in data.items() if k not in ("settings", "documents")},
                             skip=("match", "question", "core", "from"))
        except Exception:  # (a profile that won't load: nothing more is hidden)
            pass
        return {v for v in hidden if " ".join(v.split()).casefold() not in _DESK_WORDS}

    @staticmethod
    def _report_hidden(run: Run) -> set[str]:
        """What a problem report or a note on this run keeps out on top of the profile's details (report.report_strings
        has those, as a report says them): what's this run's alone, should a page's error the desk quotes echo it. The
        desk's secrets, the digest of the password it signed in with, the person's answers for this application and
        what was typed into the form (4 characters or more; not a choice a list offered: "Mobile Phone"), and an account
        form's security answers and the codes the inbox gave for this job (3 or more: report.build takes them out as
        whole words). Never the profile's own words, which a report also says as a page's ("Workday", a job's title)."""
        hidden = {v for v in set(_secret_values()) | _texts(run.once) | {run.sign_in_key}
                  | {str(e.get("value") or "") for e in list(run.went_in.answers.values()) if e.get("kind") == "answer"}
                  if len(v.strip()) >= 4}
        hidden |= {v for v in _texts(run.account_answers)
                   | {mail[1] for mail, job_id in list(_MAIL_TAKEN.items()) if job_id == run.job_id} if len(v.strip()) >= 3}
        return {v for v in hidden if " ".join(v.split()).casefold() not in _DESK_WORDS}

    # ------------------------------------------------------------- worker
    async def _worker(self) -> None:
        while True:
            try:
                self._retry_queue_lock()
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # the worker must outlive any one job's surprise
                await asyncio.sleep(POLL_SECONDS)

    def start_round(self, job_id: int) -> None:
        """The person is at this job's tab in one of the desk's rounds (focus() brought it up): its bot check, or
        its review page to check before they press Submit."""
        if self.round_job != job_id and job_id in self.runs:
            self.runs[job_id].rounds += 1  # (for its lines in the desk's history)
        self.round_job, self.round_at = job_id, time.time()
        self._wake.set()

    def end_round(self) -> None:
        self.round_job = None
        self._wake.set()

    def _in_round(self) -> bool:
        """Is the person at a job in one of the desk's rounds still: its bot check, or its review page waiting on
        their Submit (it waits on them, for no longer than a sign-in would hold the queue)?"""
        run = self.runs.get(self.round_job) if self.round_job is not None else None
        theirs = run is not None and (run.status == "ready" or run.status == "needs_you" and run.need in ROUND_NEEDS)
        if not theirs or time.time() - self.round_at > HANDS_ON_TIMEOUT:
            self.round_job = None
            return False
        return True

    async def _tick(self) -> None:
        if self._full() and not (self._slots.keys() <= self._waiting) and not self._may_go_beside():
            # as many jobs under way as go at once: nothing else is begun or looked at until one is done (with one at
            # once, between jobs, as ever). Unless each of them waits on the person's pause (Jobs at once lowered
            # meanwhile, say): that's looked at still, or they'd wait for ever; or the person pressed Submit, or solved
            # a check in the round, which goes beside them (two or more at once)
            await self._until_one_is_done()
            return
        self._resume_tailored()
        await self._front_next()
        # the person at a job in a round (a bot check, a review page): nothing else started over it, a solved check
        # noticed; a Submit they pressed (the round's own "Submit this one") still goes
        if self._in_round() and not any(t[0] == "submit" for t in self.tasks):
            await self._pick_up_left()
            self._wake.clear()
            await self._sleep(POLL_SECONDS)
            return
        # A Submit the person pressed goes first, even while the queue holds for a sign-in.
        submit = next((t for t in self.tasks if t[0] == "submit"), None)
        blocker = self._blocker()
        if blocker is not None and submit is None:
            if blocker.job_id in self._slots:
                # its pass isn't over (the page it stopped on is being saved in its tab): looked at once it is, so a
                # code is never typed there meanwhile
                self._wake.clear()
                await self._sleep(POLL_SECONDS / 3)
                return
            if blocker.job_id in self._behind:
                # paused for the person while they were at another job's tab: theirs now, its tab brought up (once
                # they're done there), and the wait for them in it begins then
                await self._bring_forward(blocker)
                if blocker.job_id not in self._behind:
                    blocker.active_at = time.time()
            if blocker.need == "email_code":
                await self._check_mail_safely(blocker)
            try:
                moved = await self._strict(self._moved_on(blocker))
            except Exception:  # a tab that can't be read just now (mid-load, crashed): not past it, and the
                moved = False  # idle and long-wait releases below still apply
            if not moved and self._reset_overdue(blocker):
                blocker.reset_no_mail = moved = True  # (none it could use: as above)
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
                    # the job under way, past its sign-in or code, carries on: Pause queue holds new jobs only (issue
                    # #112, note 43: Oracle's, past Verify, waited as "queued" with its tab half-filled and no card)
                    self.pressed.add(blocker.job_id)
                except ValueError:  # marked applied meanwhile
                    pass
            elif (time.time() - max(blocker.active_at, blocker.desk_at) > HANDS_ON_IDLE
                  and blocker.job_id not in self._behind):
                # (its idle wait begins as its tab is brought up: never while it waits its turn behind another's; and
                # again as the desk put its emailed code in, as ever: the person may be putting the right one in after)
                self._go_on_without(blocker, f"nothing happened in its tab for {HANDS_ON_IDLE // 60} minutes, "
                                    "so the other jobs went ahead")
            else:
                self._wake.clear()  # sleep the poll out, unless something new comes in
                await self._sleep(self._poll_for([blocker]))
                return
        await self._pick_up_left()
        if not self.tasks:
            self._wake.clear()
            # with a job left waiting on the person, look at its tab again soon
            left = [r for r in self.runs.values() if r.left and r.status == "needs_you"]
            # (and, while another desk keeps the queue, back in time to look again: _retry_queue_lock)
            await self._sleep(min(self._poll_for(left) if left else 30, QUEUE_LOCK_RETRY if self.queue_elsewhere else 30))
            return
        # paused: only what the person pressed for a job goes (its Submit, its Resume, its answers on its card)
        pressed = next((t for t in self.tasks if t[1] in self.pressed), None) if self.paused and submit is None else None
        if self.paused and submit is None and pressed is None:
            self._wake.clear()
            await self._sleep(POLL_SECONDS)
            return
        if submit is None and self._in_round():
            return  # a round begun meanwhile (the desk's Solve them): nothing started over the tab the person is at
        # the first of those whose site no job under way is on (one at a time there: its sign-ins, accounts and codes
        # are the site's own); a Submit the person pressed first, and only that while the queue holds for them
        held = self._blocker() is not None or self._in_round()
        order = [t for t in self.tasks if t[0] == "submit"] + (
            [] if held else [t for t in self.tasks if t[1] in self.pressed] if self.paused else
            [t for t in self.tasks if not self._held_back(t)])
        task = next((t for t in order if (self._submit_free(t[1]) if t[0] == "submit" else self._site_free(t[1]))),
                    None)
        if self._full() and not (task is not None and self._goes_beside(task)):
            # (each under way waiting on the person: none begun beside them, but the person's own Submit or solved check)
            await self._until_one_is_done()
            return
        if task is None:  # each waits for a job under way on its site
            self._wake.clear()
            await self._sleep(POLL_SECONDS)
            return
        self.pressed.discard(task[1])
        self._first.discard(task[1])
        self.tasks.remove(task)
        kind, job_id = task
        run = self.runs[job_id]
        self._began += 1
        run.began, run.site, run.site_since = self._began, "", 0.0
        self._slots[job_id] = asyncio.get_running_loop().create_task(self._run_task(kind, run))
        self._save_queue()  # (under way now)
        if self._full():
            await self._until_one_is_done()  # (one at once: the job's whole pass, as ever)

    def _full(self) -> bool:
        """As many jobs under way as go at once."""
        return len(self._slots) >= self.at_once()

    def _goes_beside(self, task: tuple[str, int]) -> bool:
        """May this task begin beside as many jobs as go at once: a Submit the person pressed, or the job whose check
        they just solved in the desk's round (it carries on as they watch)? One more at most, and only with two or more
        at once: with one, each waits for the job under way's pass, as ever (the review of jobs at once)."""
        n = self.at_once()
        return n > 1 and len(self._slots) < n + 1 and (task[0] == "submit" or task[1] in self._first)

    def _may_go_beside(self) -> bool:
        return any(self._goes_beside(t) for t in self.tasks)

    async def _front_next(self) -> None:
        """A job whose tab wasn't brought to the front as it paused (the person was at another's: _bring_forward) is
        brought up once they're done there: the first to pause."""
        for job_id in [j for j in self._behind if j not in self.runs or self.runs[j].status != "needs_you"]:
            self._behind.discard(job_id)  # (resumed, skipped or sent meanwhile)
        waiting = [self.runs[j] for j in self._behind if j not in self._slots and not self.runs[j].blocking]
        if not waiting or self._person_at(None) is not None:
            return
        for r in waiting:  # (one brought up: the rest are on the desk's cards, never brought over it one by one)
            self._behind.discard(r.job_id)
        await self._bring_forward(min(waiting, key=lambda r: r.paused_at))

    async def _until_one_is_done(self) -> None:
        """Wait until a job under way is done (or the queue is woken: a setting changed, say)."""
        self._wake.clear()
        waiter = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({waiter, *self._slots.values()}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()

    async def _run_task(self, kind: str, run: Run) -> None:
        """One job's pass (or the Submit the person pressed), in a task and a browser view of its own: up to
        settings.jobs_at_once go at once, each in its own tab."""
        cancelled = False  # (the desk stopping, or the program ending: kept as it was, for the next start)
        cut = False  # (and its pass cut short by that, not ended by itself)
        began, at_once = time.time(), self.at_once()  # (for the pass's line in the desk's history)
        run.pass_steps = set()
        ended_as: tuple[str, str, str] | None = None  # (how its pages ended, before its stop's page was saved)
        try:
            with self.srv.browser.desk_view(run.job_id):
                held = self._hold_tools(run.job_id)
                try:
                    if kind != "submit":
                        run.inbox_since = 0.0  # (a new pass: its mail, if it waits on any, begins in this one)
                        await self._hold_for_site(run)
                        if self._mails(run):
                            # a job system that emails codes and links: any step before the application's first form may
                            # send one (Workday's sign-in drawn without its buttons, Oracle's email step): the inbox is
                            # this job's from its start until then (the review of jobs at once)
                            await self._take_inbox(run, step=False)
                    await self._strict(self._submit(run) if kind == "submit" else self._drive_again_if_redrawn(run))
                    ended_as = (run.status, run.need, run.reason)
                    await self._strict(self._save_stop(run))
                except asyncio.CancelledError:
                    cancelled = cut = True
                    raise
                except TabClosed:
                    # (skipped, or marked applied, as it went: that stands)
                    if run.status not in ("skipped", "submitted"):
                        run.status, run.need, run.blocking = "failed", "", False
                        run.reason = "Its tab was closed. Press Resume to start this application again."
                        # (every tab goes with the browser: a pass the browser's closing cut short is picked up again
                        # after a restart, as one the desk's stopping cut short is)
                        run.cut_short = run.page is not None and self.srv.browser.lost(run.page) == "browser"
                        self._log(run, run.reason)
                except Exception as e:
                    if run.status not in ("skipped", "submitted"):
                        run.status, run.need = "failed", ""
                        run.reason = (f"Something went wrong: {type(e).__name__}: "
                                      f"{str(e).splitlines()[0][:200] if str(e) else ''}")
                        lost = self.srv.browser.lost(run.page) if run.page is not None else ""
                        if lost == "crashed":  # (Playwright: "Page crashed")
                            run.reason = "Its tab crashed. Press Resume to start this application again."
                        elif lost == "browser":
                            # (Playwright: "Target page, context or browser has been closed", issue #112, note 36)
                            run.reason = "The automation browser was closed. Press Resume to start this application again."
                            run.cut_short = True
                        self._log(run, run.reason)
                    # (said in the log either way: a skipped or marked applied job's status stands, the error's still one)
                    logging.getLogger(__name__).warning("job %s stopped on an error", run.job_id, exc_info=True)
                finally:
                    self._give_back_tools(held)
                    run.inbox = False  # (a code it waits on now holds the others as the person's pause does)
                    # the desk was stopped part-way (or before the pass had begun its pages): kept as under way
                    if run.status == "running" or cancelled and kind != "submit" and run.status == "queued":
                        run.status, run.reason = "failed", "Stopped before it finished. Press Resume to carry on."
                        run.cut_short = cancelled
                    if kind == "submit" and not cancelled:
                        run.submit_pressed = False  # (the person's Submit has had its go)
                    run.updated = time.time()
            with contextlib.suppress(Exception):  # (a tab that won't close: never the job's surprise)
                await self._close_done_tabs()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if self._slots.get(run.job_id) is asyncio.current_task():
                del self._slots[run.job_id]
            if not cancelled:  # (cut short: the queue stays as it was kept, this job under way in it)
                self._save_queue()
            outcome, need, reason = run.status, run.need, run.reason
            if cut and outcome in ("failed", "queued", "running"):
                outcome = "stopped"
            elif outcome in ("queued", "running") and ended_as is not None:
                outcome, need, reason = ended_as  # (queued again as its stop's page was saved: a Resume pressed then)
            if outcome == "submitted" and run.marked:
                outcome = "marked_applied"
            if outcome in history.OUTCOMES:
                self._note_history(run, outcome, began, kind, at_once, need, reason)
            self._wake.set()

    # ------------------------------------------------------------- jobs at once
    def _blocker(self) -> Run | None:
        """The job holding the queue for the person's hands (the first to pause, of those that do): one whose tab is up
        before those waiting their turn to be brought up (_behind), whose turn comes once it's done."""
        return min((r for r in self.runs.values() if r.blocking), key=lambda r: (r.job_id in self._behind, r.paused_at),
                   default=None)

    def _person_at(self, run: Run | None) -> Run | None:
        """The job whose tab the person is at for the desk, other than `run`: a round's, a pause holding the queue for
        their hands, or a sign-in or code the queue went on without that they're still at work in (anything done in its
        tab in the last RELEASE_IDLE seconds). No other job's tab is brought over it (_bring_forward, keep_front)."""
        return next((r for r in self._persons_tabs(run) if r is not run), None)

    def _persons_tabs(self, run: Run | None = None) -> Iterator[Run]:
        """The jobs whose tab the person may be at for the desk, the likeliest first: a round's, the pauses holding the
        queue for their hands (the first to pause first), then sign-ins and codes the queue went on without that they've
        been at work in since (_at_work_in). For `run`, a pause already holding the queue that waits its turn to be
        brought up (_behind) counts only when it paused before `run` did: the first of those comes up first, the next
        once it's done (the final check of jobs at once: two waiting so, each kept the other down for 45 minutes)."""
        if self._in_round() and (r := self.runs.get(self.round_job) if self.round_job is not None else None):
            yield r
        mine = run.paused_at if run is not None and run.status == "needs_you" and run.blocking else float("inf")
        yield from sorted((r for r in self.runs.values() if r.blocking
                           and (r.job_id not in self._behind or r.paused_at < mine)), key=lambda r: r.paused_at)
        yield from sorted((r for r in self.runs.values() if self._at_work_in(r)), key=lambda r: r.paused_at)

    @staticmethod
    def _at_work_in(run: Run) -> bool:
        """Is the person at work in a job's tab the queue went on without: a sign-in or code that's theirs, changed since
        it paused by something the desk didn't do (Run.active_at, _note_activity) in the last RELEASE_IDLE seconds?
        Never a wait that's the desk's own (a reset or verification email it reads from the inbox itself: the re-check of
        jobs at once, its tab was taken for the person's for a minute after every new Workday account)."""
        if not (run.left and run.status == "needs_you" and run.need in HANDS_ON):
            return False
        desks_own = run.need == "email_code" and (run.resetting and run.reset_waited or run.verifying)
        return (not desks_own and run.active_at > run.paused_at
                and time.time() - run.active_at < RELEASE_IDLE + SLOW_POLL_SECONDS)

    def _front_tab(self) -> Any:
        """The tab the person is at for the desk (BrowserSession.keep_front), if any: never one waiting its turn to be
        brought up (_behind), but the one they're at instead."""
        for r in self._persons_tabs():
            page = r.page if r.job_id not in self._behind else None
            if page is not None and not page.is_closed():
                return page
        return None

    def _mails(self, run: Run) -> bool:
        """Is the job on a job system that emails codes or links (an account's, a sign-in's)?"""
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        return any(detect_ats(u) in mailbox.ATS_MAIL_DOMAINS for u in (run.url, job.get("url"), job.get("apply_url"))
                   if u)

    def _under_way(self) -> list[Run]:
        return [self.runs[j] for j in list(self._slots) if j in self.runs]

    def _site_key(self, url: str) -> str:
        """A page's site, as jobs at once keep to one each: a job system's host (one employer's Workday, not
        another's), or an employer's own domain ("acme.com" for careers.acme.com and jobs.acme.com)."""
        host = (urlparse(url or "").hostname or "").lower()
        if not host:
            return ""
        return host if shared_system(url) is not None else mailbox.site_domain(host)

    def _sites(self, run: Run) -> set[str]:
        """Where a job is, or may go: its employer, its posting's and application's sites, where it paused, and its
        tab's now; and the posting, shared with every job that may be the same one (Tracker.linked: an Indeed job and
        the employer's posting kept apart), so two of those are never under way at once (the review of 0.3.206: both
        passed the card as each other was still saved, and both were filled)."""
        t = self.srv.tracker()
        job = t.get(run.job_id, with_description=False) or {}
        urls = [run.url, job.get("url"), job.get("apply_url"), f"https://{run.paused_host}/" if run.paused_host else ""]
        if run.page is not None and not run.page.is_closed():
            urls.append(run.page.url)
        sites = {self._site_key(u) for u in urls if u} - {""}
        company = norm(str(run.company or job.get("company") or ""))
        postings = {f"posting:{j}" for j in (run.job_id, *t.linked(run.job_id))}
        return sites | postings | ({f"employer:{company}"} if company else set())

    def _site_free(self, job_id: int) -> bool:
        """May this job begin now: no job under way is at its employer or on its sites (one at a time on a site: its
        sign-ins, accounts, password resets and emailed codes are the site's own)?"""
        run = self.runs.get(job_id)
        if run is None or job_id in self._slots:
            return run is None
        under_way = self._under_way()
        if not under_way:
            return True
        mine = self._sites(run)
        return not any(mine & self._sites(other) for other in under_way)

    def _submit_free(self, job_id: int) -> bool:
        """May the Submit the person pressed go now? It presses Submit in its own tab alone: never while another job
        under way is in a step that may email the person on the very host its tab is on (a sign-in there, say), but
        beside any other job at its site (the review of jobs at once)."""
        run = self.runs.get(job_id)
        if run is None or job_id in self._slots:
            return run is None
        page = run.page if run.page is not None and not run.page.is_closed() else None
        host = (urlparse(page.url if page is not None else run.url).hostname or "").lower()
        return not any(o.inbox and host and (urlparse(o.page.url if o.page is not None and not o.page.is_closed()
                                                      else o.url).hostname or "").lower() == host
                       for o in self._under_way() if o is not run)

    def _site_holder(self, run: Run) -> Run | None:
        """Another job under way that got to the site this job's tab is on first (two employers' applications can lead
        to one job system's host): this one waits for it there. Never one in a step that may email the person: the
        other may be waiting for that."""
        page = run.page if run.page is not None and not run.page.is_closed() else None
        site = self._site_key(page.url if page is not None else run.url)
        if site != run.site:
            run.site, run.site_since = site, time.monotonic()
        if not site or run.inbox:
            return None
        return next((o for o in self._under_way() if o is not run and o.site == site
                     and (o.site_since, o.began) < (run.site_since, run.began)), None)

    async def _hold_for_site(self, run: Run) -> float:
        """Before each step of a job under way: wait while another job got to this one's site first (never one in a
        step that may email the person: _site_holder). The person's pauses and rounds hold no job under way: those go
        on in their tabs, never brought to the front over the person's (_bring_forward, keep_front), and only new jobs
        wait (the review of jobs at once: a job held for a round kept its place, and the person's Submit waited for
        the whole round). Returns the seconds waited (a pass's own time doesn't count them), 0 for none."""
        began, said = time.monotonic(), ""
        try:
            while run.status not in ("skipped", "submitted"):
                o = self._site_holder(run)
                if o is None:
                    break
                why = f"{o.company or 'another job'} is on the same site"
                if why != said:
                    said = why
                    self._log(run, f"waiting: {why}; this one carries on after that")
                self._waiting.add(run.job_id)
                await asyncio.sleep(max(POLL_SECONDS / 3, 0.05))
        finally:
            self._waiting.discard(run.job_id)
        return time.monotonic() - began if said else 0.0

    def _inbox_holder(self, run: Run) -> Run | None:
        """Another job under way in a step that may email the person (Run.inbox), if any."""
        return next((o for o in self._under_way() if o is not run and o.inbox and o.status == "running"), None)

    def _hands_on(self, run: Run) -> Run | None:
        """Another job holding the queue for the person's sign-in or emailed code (an email step of theirs, in its
        tab), if any."""
        return min((r for r in self.runs.values() if r is not run and r.blocking and r.need in HANDS_ON),
                   key=lambda r: r.paused_at, default=None)

    async def _take_inbox(self, run: Run, step: bool = True) -> float:
        """Before a step that may email the person (a sign-in, making an account, a password reset, an emailed code):
        one job at a time is in such steps, one under way until it's into its application (or its pass ends), or one
        holding the queue for the person's sign-in or code. The inbox is one: a code meant for one job is never
        another's. Returns the seconds waited, 0 for none."""
        began, waited, said = time.monotonic(), False, ""
        try:
            while run.status not in ("skipped", "submitted"):
                other = self._inbox_holder(run) or self._hands_on(run)
                if other is None:
                    break
                why = f"{other.company or 'another job'} is signing in or waiting on an email"
                if why != said:
                    said = why
                    self._log(run, f"waiting: {why}; this one carries on after that")
                waited = True
                self._waiting.add(run.job_id)
                await asyncio.sleep(max(POLL_SECONDS / 3, 0.05))
        finally:
            self._waiting.discard(run.job_id)
        if not run.inbox:
            run.inbox = True
            self._inbox_gen += 1
        if step and not run.inbox_since:
            # (a step that may email: its code is sent from here on, the job's own mail. Never as a pass begins on a
            # system that emails (step False): mail from before its step then is no more its own than on its own: the
            # re-check of jobs at once, an earlier job's code found as this one's)
            run.inbox_since = time.time()
        return time.monotonic() - began if waited else 0.0

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

    def _stop_for_what_is_left(self, run: Run, gaps: str, pending: list[dict[str, Any]],
                               missing_files: list[dict[str, Any]]) -> bool:
        """Pause a filled page for what it still needs, if anything: a file the profile doesn't
        point to, entries the profile lacks (gaps), questions it doesn't answer, or boxes the desk
        couldn't fill. The questions come along on each card, so they can be answered meanwhile.
        True when it paused."""
        unfilled, pending = _unfilled(pending)
        couldnt = f" {_couldnt_fill(unfilled)}" if unfilled else ""
        them = "it" if len(pending) == 1 else "them"
        if missing_files:
            self._pause(run, "stuck", "The form wants a file (" + ", ".join(f["label"] for f in missing_files)
                        + ") and the profile doesn't say where yours is: ask Claude to set documents.resume (or "
                        "documents.cover_letter) in profile.yaml, then press Resume."
                        + (f" {gaps}" if gaps else "")
                        + (f" It also has {_count(len(pending), 'question')} your profile doesn't answer." if pending else "")
                        + couldnt, pending)
        elif gaps:
            self._pause(run, "stuck", gaps + (f" It also has {_count(len(pending), 'other question')}, here." if pending else "")
                        + couldnt, pending)
        elif pending:
            self._pause(run, "questions", f"{_count(len(pending), 'question')} your profile doesn't answer. Answer "
                        f"{them} here: the desk fills {them} in, and remembers {them} for next time unless you untick "
                        "that." + couldnt, pending)
        elif unfilled:
            self._pause(run, "stuck", f"{couldnt.strip()} Fill {'it' if len(unfilled) == 1 else 'them'} in the browser "
                        "window, then press Resume; or press Resume now and the desk tries again.")
        else:
            return False
        return True

    def _hold_tools(self, job_id: int) -> tuple[Any, Any, Any]:
        """Take the browser tools for one of this job's steps: Claude's calls that act in the
        browser wait meanwhile (server._desk_driving). Returns what to give back."""
        browser = self.srv.browser
        held = (job_id, browser.current_tab, browser.current_job_id)
        self._driving.append(job_id)
        return held

    def _give_back_tools(self, held: tuple[Any, Any, Any]) -> None:
        """Hand the tools back on the tab, and with the job, they had before: Claude may be at
        work in a tab of its own, and its next fill or click mustn't land in this job's tab."""
        mine, tab, job_id = held
        with contextlib.suppress(ValueError):  # (set straight by hand meanwhile: current)
            self._driving.remove(mine)
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
        """Wait `seconds`, or until something wakes the queue. (Not asyncio.wait_for, which on Python 3.10 drops a
        stop that comes as the wake does: stop.)"""
        waiter = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({waiter}, timeout=seconds)
        finally:
            waiter.cancel()

    async def _moved_on(self, run: Run) -> bool:
        """Has the person got the paused tab past its sign-in, check or code? Only on the
        same site, or on into an application system: a tab they've taken to their webmail
        or a sign-in provider isn't the application moving on."""
        if run.page is None or self.srv.browser.lost(run.page):
            return True  # they closed it (or it crashed, or the browser with it): start the job again
        # read without taking over the tools' tab: Claude may be using them meanwhile
        data, text = await self.srv.browser.peek(run.page)
        self._note_activity(run, data)
        return self._twice(run, self._looks_past(run, data, text))

    @staticmethod
    def _note_activity(run: Run, data: dict[str, Any]) -> None:
        """The address and what's in the boxes of a paused tab, as looked at: a change is the person at work in it (a
        bot check that reloads itself with a new token in its address isn't, nor the desk's own code typed there or
        link opened, while its page settles: the re-check of jobs at once, the person taken to be at that tab). Its code
        box holding another code than the desk's is the person's, settled or not (one the site turned down, the right
        one put in by them: the final check of jobs at once)."""
        mark = hash((_bare(data.get("url") or ""), tuple((f.get("id"), str(f.get("value"))) for f in data.get("fields") or [])))
        settling = time.time() - run.desk_at <= DESK_SETTLE
        if settling and run.desk_code:
            typed = "".join(str(f.get("value") or "") for f in data.get("fields") or [] if _code_box(f))
            settling = not typed.strip() or _code_mark(typed) == run.desk_code  # (emptied by the site: not theirs)
        if run.tab_mark and mark != run.tab_mark and not settling:
            run.active_at = time.time()
        run.tab_mark = mark

    def _looks_past(self, run: Run, data: dict[str, Any], text: str) -> bool:
        """Does this look at a job's paused tab show it past the pause? Settled, not the page it
        paused on, not that kind of page any more, and still this job's (see _own_place)."""
        if data.get("loading") or (run.pause_sig is not None and _page_sig(data) == run.pause_sig):
            return False
        if run.hold_host and (urlparse(data.get("url") or "").hostname or "").lower() == run.hold_host.lower():
            return False  # still on the account site the person is making their account on
        if (run.account_page and not run.accounts_tried and _bare(data.get("url") or "") == run.account_page
                and _email_account_view(data)):
            return False  # still in the steps of making their account, which are theirs (Eightfold's modal)
        if run.need == "bot_check" and _STILL_CHECKING.search(text[:4000]):
            return False  # the page still asks them to prove they're a person (an error on a form, say)
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
        # ?pid=999 there is another job, and /careers?query=... with no id its careers home. With no
        # query at all it's either: the careers home (the site's logo pressed), or the job's own page
        # with its address rewritten by the site's script (/careers/job?gh_jid=123 to /careers/job, the
        # application still open). The page tells: an application's boxes, or not
        mine: dict[str, set[str]] = {}
        for u in own:
            for key, value in _job_ids(u).items():
                mine.setdefault(key, set()).add(value)
        theirs = _job_ids(tab.url)
        rewritten = False
        if not any(k in mine and v not in mine[k] for k, v in theirs.items()):  # (not another job's)
            if any(_bare(tab.url) == _bare(u) for u in own):
                if not mine or any(k in theirs for k in mine):
                    return False  # the posting, its application, or the page it was left on
                rewritten = not urlparse(tab.url).query
            elif not any(_above(tab.url, u) for u in own):
                return False
        try:
            data, text = await self.srv.browser.peek(tab)
        except Exception:  # a tab mid-way through loading: carried on with, as before
            return False
        return not confirmations(text) and not (rewritten and _application_like(data))

    def _own_hosts(self, run: Run) -> set[str]:
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        return {(urlparse(u).hostname or "").lower() for u in (run.url, job.get("url"), job.get("apply_url")) if u}

    @staticmethod
    def _poll_for(runs: list[Run]) -> float:
        """How soon to look at tabs waiting on the person again: every POLL_SECONDS while one of them changed or
        paused within STILL_SECONDS (or the desk typed its code there, or opened its link: _note_activity), else every
        SLOW_POLL_SECONDS."""
        now = time.time()
        return (POLL_SECONDS if any(now - max(r.active_at, r.paused_at, r.desk_at) < STILL_SECONDS for r in runs)
                else SLOW_POLL_SECONDS)

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
        return mailbox.login_address(address), password

    def _mail_senders(self, run: Run) -> set[str]:
        """Who may have sent this job's code or link: the job site it's waiting on, the
        employer's own site, and the job system they run on (and a Workday tenant's own mail
        domain: KLA's kla.wd1.myworkdayjobs.com mails from workday.kla.com, live, Oct 2026), and
        the employer's own mail domains on the person's lists (_employer_mail_domains)."""
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        urls = [u for u in (run.url, job.get("url"), job.get("apply_url")) if u]
        allowed = {mailbox.site_domain(urlparse(u).hostname or "") for u in urls}
        for u in urls:
            allowed |= mailbox.ATS_MAIL_DOMAINS.get(detect_ats(u), set())
            if tenant := re.fullmatch(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", (urlparse(u).hostname or "").lower()):
                allowed.add(f"workday.{tenant.group(1)}.com")
        if run.employer_mail is None:
            try:
                own = str(config.Profile.load().get("personal.email") or "").rpartition("@")[2].strip().lower()
            except Exception:  # (a profile.yaml that doesn't read: no inbox to read either)
                own = ""
            run.employer_mail = frozenset(_employer_mail_domains(str(run.company or job.get("company") or ""),
                                                                 {(urlparse(u).hostname or "").lower() for u in urls}, own))
        return {d for d in allowed | run.employer_mail if d}

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
        if run.mail_done or now - run.mail_checked < MAIL_POLL_SECONDS or _mail_waited(run) > MAIL_WINDOW:
            return
        if self._inbox_holder(run) is not None:
            # another job under way may be bringing on an email of its own this moment, before its pause says from when
            # its email is its own (_check_mail's partition): looked at once that job is past it. Its waits (a reset's,
            # the inbox's own window) don't run on meanwhile: nothing was looked at
            run.mail_last = "not looked: another job is signing in or waiting on an email"
            run.mail_held_at = run.mail_held_at or now
            return
        if run.mail_held_at:
            run.mail_held += now - run.mail_held_at
            run.mail_held_at = 0.0
        login = self.mail_login()
        if login is None or run.page is None or run.page.is_closed():
            run.mail_last = "no inbox login" if login is None else "its tab is closed"
            return
        run.mail_checked = now
        gen = self._inbox_gen  # (a job taking the inbox while this look is under way: what it finds isn't used)
        try:
            data, _ = await self.srv.browser.peek(run.page)
        except Exception as e:  # a tab mid-way through loading: looked at again next time
            run.mail_last = f"couldn't read its tab: {type(e).__name__}"
            run.mail_looked = time.time()  # (a look that failed is one tried: a reset's wait runs on, as on its own)
            return
        boxes = [f for f in data.get("fields") or [] if _code_box(f)]
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

        # Another job waiting on mail from the same job system owns what came after its mail began (Workday's
        # codes come from one address for every employer): a later one's start ends this one's mail, and
        # with an earlier one waiting, mail from before this job's began is that one's. A job's mail begins
        # as it pauses, or as its step that may email began on the way there (_mail_from: the code is sent then)
        since = _mail_from(run)
        others = [_mail_from(r) for r in self.runs.values() if r is not run and r.status == "needs_you"
                  and r.need == "email_code" and self._mail_senders(r) & senders]
        later = [t for t in others if t > since]
        # (a few seconds back still: the site sends the code as the button is pressed, a moment before
        # the desk notices and pauses)
        look_back = SHARED_LOOK_BACK if any(t <= since for t in others) else mailbox.LOOK_BACK
        # Its own looked for from that far back before its pause, as on its own: never further, even where its step
        # began earlier. With an earlier job waiting on the same sender, mail between its step's start and that is
        # either job's (the other's Resend, pressed in its tab meanwhile): neither's, left for the person (the
        # re-checks of jobs at once)
        want = "code" if boxes else "reset" if run.resetting else "link"
        # (a reset's email from when the desk asked for it: an older one is an earlier reset's, or the clocks apart)
        asked = run.reset_at if want == "reset" and run.reset_at else run.paused_at
        try:
            if run.others_mail is False:  # (the lists read once a job, off the event loop)
                job = self.srv.tracker().get(run.job_id, with_description=False) or {}
                run.others_mail = await asyncio.to_thread(_another_employers_mail, str(run.company or job.get("company")
                                                                                       or ""), run.employer_mail or frozenset())
            found = await asyncio.to_thread(mailbox.search, *login, asked, senders, want, own_link,
                                            min(later) if later else None, look_back,
                                            others=run.others_mail if want == "code" else None)
        except mailbox.MailboxError as e:
            if "app password" in str(e):
                self._mail_refused = login[1]  # not tried again until a new one is saved
                self.mail_problem = (f"The desk couldn't read your email: {e}. Save a new email app password "
                                     "in the Site passwords card to try again.")
            else:
                self.mail_problem = f"The desk couldn't read your email: {e}. It tries again shortly."
            # (tried: a reset's wait runs on through the inbox's own errors, never for ever. Only looks held off for
            # another job's email step stop it: the re-check of jobs at once)
            run.mail_looked = time.time()
            return
        except Exception as e:  # a mail service's odd answer: tried again next time, not sinking the queue
            self.mail_problem = f"The desk couldn't read your email ({type(e).__name__}). It tries again shortly."
            run.mail_looked = time.time()
            return
        self.mail_problem = None
        if gen != self._inbox_gen or self._inbox_holder(run) is not None:
            # (the peek, the lists and the login take seconds: a job that took the inbox meanwhile may have had a code
            # sent that this look found as this one's. Looked at again once that job is past it)
            run.mail_last = "not used: another job began signing in or making an account while the inbox was read"
            run.mail_held_at = run.mail_held_at or time.time()
            return
        run.mail_looks += 1
        run.mail_looked = time.time()
        run.mail_last = (f"looked for a {want}: " + ("none" if found is None else f"found one from {found.sender}")
                         + f" ({len(boxes)} code box(es) on the page)")
        if found is None or run.status != "needs_you" or run.need != "email_code":
            return
        # (a code, and a reset's link: a confirmation link is the account's, and two jobs on one site share one: the
        # review)
        mail = (found.sender, found.value, found.received) if found.kind in ("code", "reset") else None
        if mail and found.kind == "code" and _MAIL_TAKEN.get(mail, run.job_id) != run.job_id:
            # (the job it went to has it: this one's is to come)
            run.mail_last += "; not used: another job's, put in there already"
            return
        if found.kind == "reset" and _link_opened(found.value):
            # a reset's link works once: one opened already (for this job or another) sets nothing. The new one is
            # waited for; and should none come, its wait running out is said, never taken for no email at all
            run.reset_spent = run.account_known = True
            run.mail_last += "; not used: that reset link was opened already"
            return
        if found.kind == "reset" and found.received < asked - RESET_CLOCK_SKEW:
            run.reset_spent = run.account_known = True
            run.mail_last += "; not used: it came well before the reset was asked for, an earlier reset's"
            return
        if found.kind == "reset" and found.received < asked and time.time() - asked < RESET_FRESH_WAIT:
            # from before the reset was asked for, by this computer's clock: an earlier reset's (its link spent: run 136),
            # or this one's with the clocks apart. A newer one is waited for a moment, and this one used if none comes
            run.mail_last += "; not used yet: it came before the reset was asked for, so a newer one is waited for"
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
                run.reset_opened = run.account_known = True  # (the account is there: a refused sign-in makes none)
                await run.page.goto(found.value, wait_until="domcontentloaded", timeout=45000)
                run.mail_done = True
                self._log(run, f"opened the password reset link from your email ({_sent_from(found)})")
            else:
                await self.srv.browser.visit(found.value)
                run.mail_done = True
                self._log(run, f"opened the confirmation link from your email ({_sent_from(found)})")
                if run.page is not None and not run.page.is_closed():
                    await run.page.reload()
                if run.verifying:  # the account's verified: the Sign In it paused on (the same page, reloaded) is past the pause
                    run.pause_sig = None
        except Exception as e:  # a slow site or a closed tab: the person finishes it, as without the inbox
            run.mail_done = True
            self._log(run, f"couldn't use the {found.kind} from your email ({type(e).__name__}); "
                      "it's in your inbox for you")
        finally:
            run.desk_at = time.time()  # (what changes in its tab now is the desk's doing, not the person's: _note_activity)
            run.desk_code = _code_mark(found.value) if found.kind == "code" else 0
        if mail and run.mail_done:
            _take_code(mail, run.job_id)

    async def _enter_code(self, run: Run, found: mailbox.Found) -> bool:
        """Type the emailed code into its box (a digit per box where there's one for each) and
        press the button that sends it. False when it didn't go in."""
        srv = self.srv
        if run.page is None or run.page.is_closed() or not self._own_place(run, run.page.url):
            run.mail_last += "; not typed: its tab is elsewhere"
            return False  # its tab has gone on to another page: the code isn't typed there
        if not srv.browser.use_tab(run.page):
            run.mail_last += "; not typed: its tab couldn't be used"
            return False
        data = await srv.inspect_form(include_dropdown_options=False)
        boxes = [f for f in data.get("fields") or [] if _code_box(f)]
        if not boxes:  # the page moved on meanwhile
            run.mail_last += "; not typed: no code box on the page"
            return False
        # a box per digit (Oracle's "Confirm Your Identity"), or one box; typed key by key (BrassRing's take a
        # digit only from a key pressed in them)
        ids = [box["id"] for box in boxes] if 1 < len(boxes) == len(found.value) else [boxes[0]["id"]]
        why = ""
        try:
            read = re.sub(r"[^0-9A-Za-z]", "", "".join(await srv.browser.type_code(ids, found.value)))
            # (all of it; or, typed into a first box that moved on by itself to boxes the reader didn't count, its
            # first digit)
            if read != re.sub(r"[^0-9A-Za-z]", "", found.value) and not (len(ids) == 1 and read and len(read) == 1
                                                                        and found.value.startswith(read)):
                # (how much they read, never what: that may be most of the code, and the log is shown and reported)
                why = f"its boxes read {f'{len(read)} character(s), not the code' if read else 'nothing'}"
        except Exception as e:
            why = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
        if why:  # (looked for again at the next look; said once, so the log says why none went in)
            said = f"found the code in your email but couldn't type it into the page ({why[:160]}); I'll try again"
            if said not in run.log[-3:]:
                self._log(run, said)
            return False
        self._log(run, f"entered the code from your email ({_sent_from(found)})")
        # one naming the code before a bare Continue: Edward Jones' BrassRing (live, Oct 2026) keeps its email
        # step's Continue above "Verify Passcode", and a press of it sends another passcode and empties the boxes
        after = [a for a in data.get("actions") or [] if not a.get("disabled")
                 and _AFTER_CODE.match(a.get("text", "").strip())]
        press = next((a for a in after if not re.match(r"^(continue|next)$", a["text"].strip(), re.I)),
                     after[0] if after else None)
        made = press is None and (press := self._account_code_button(run, data)) is not None
        mine = made or (press is None and (press := await self._code_only_submit(data)) is not None)
        try:
            if mine and press is not None:  # (the code step's own "Submit": it sends the code only)
                pressed = (await srv.browser.click(press["id"], allow_submit=True)).get("clicked") is not False
            else:
                pressed = press is not None and (await srv.click(press["id"])).get("clicked")
        except Exception:  # the button went (the page moved on by itself) or won't take a click
            pressed = False
        if press is not None and pressed:
            self._log(run, f"pressed \u201c{press['text'].strip()}\u201d")
            if made:  # said once the application shows (_drive), as a code turned down leaves it here
                run.account_made = (f"created your account on {_site(run, data)} with your email and the code it "
                                    "emailed you" + _manage_hint())
        else:  # (a "Confirm" that sends a form is left to the person, as any final button is)
            run.reason = ("I entered the code from your email. Press the page's button to carry on; "
                          "the desk continues after that.")
        return True

    async def _code_only_submit(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """_code_only_submit's button, unless the code step's own words (the block around its button and
        boxes, not the application behind a sign-in pop-up) speak of an application: a code that sends one
        ("Enter the code we emailed to submit your application") is the person's to send (or Submit for
        me's), and in practice mode never sent."""
        press = _code_only_submit(data)
        if press is None:
            return None
        words = await self.srv.browser.words_around(press["id"])
        return press if words is not None and not _APPLICATION_WORD.search(words) else None

    def _account_code_button(self, run: Run, data: dict[str, Any]) -> dict[str, Any] | None:
        """The "Submit" of the emailed code's step of an account the desk is making (an Eightfold site's,
        settings.manage_accounts): pressed after the code, as it sends nothing but the code. Only there,
        and only with the code's boxes on the page: a Submit beside anything else is the person's."""
        if not (run.account_page and run.accounts_tried and _bare(data.get("url") or "") == run.account_page
                and _email_account_view(data) == "code" and _may_manage_accounts()):
            return None
        fields = [f for f in data.get("fields") or [] if not f.get("disabled") and not f.get("aside")]
        if not all(_code_box(f) for f in fields):
            return None
        return self._account_button(data.get("actions") or [], _CODE_SENDS)

    # ------------------------------------------------------------- one job
    def _log(self, run: Run, text: str) -> None:
        """A line in the job's log (its last 40 are kept), with when it was logged beside it (Run.log_at)."""
        if len(run.log_at) != len(run.log):  # (lines put there some other way have no time of their own)
            run.log_at = (run.log_at + [0.0] * len(run.log))[:len(run.log)]
        run.log.append(text)
        run.log_at.append(round(time.time(), 1))
        run.logged += 1
        del run.log[:-40]
        del run.log_at[:-40]
        run.updated = time.time()

    def _pause(self, run: Run, need: str, reason: str, questions: list[dict[str, Any]] | None = None,
               seen: dict[str, Any] | None = None) -> None:
        """Wait on the person. `seen`: the page as it was paused on, which never counts as past
        the pause (Workday's sign-in step with no buttons drawn reads as an ordinary page)."""
        if run.status in ("skipped", "submitted"):  # skipped, or marked applied, while it ran
            return
        run.pause_sig = _page_sig(seen) if seen else None
        run.entries_told = {}  # (the card that says a section is short sets it again, after this)
        run.status, run.need, run.reason = "needs_you", need, reason
        run.questions = questions or []
        if not any(q.get("account") for q in run.questions):
            run.account_answers = {}  # (answers from an account form's card go into that form alone, on the way back)
        # (paused for their hands while they're at another job's tab, filled at once with it: its turn comes next, its
        # tab brought up then; one brought up as it paused, before this, isn't behind)
        if need in HANDS_ON and self._person_at(run) is not None:
            self._behind.add(run.job_id)
        run.blocking = need in HANDS_ON
        # a sign-in's or Create Account's own picture check (Edwards', live) counts too: the page it paused on shows it
        run.captcha = need in ("bot_check", "captcha") or bool(seen and (seen.get("captcha") or seen.get("challenge")))
        run.paused_at = run.active_at = time.time()
        run.tab_mark, run.left, run.moved_since, run.desk_at = 0, need == "bot_check", 0.0, 0.0  # (a bot check: watched)
        run.mail_checked, run.mail_done, run.verifying = 0.0, False, False
        run.mail_looked, run.mail_held, run.mail_held_at = 0.0, 0.0, 0.0
        run.paused_host = urlparse(run.url).hostname or ""
        run.hold_host = ""
        self._log(run, reason)
        if need not in _DRAFT_GOES_ON:  # (a stop the person may change the application's boxes at: theirs from here on)
            self._draft_off(run)
        if need in NOTED or need == "questions" and any(q.get("error") for q in run.questions):
            self._take_note(run)

    def _take_note(self, run: Run) -> None:
        """A note on this stop, for the person to file on the desk's Notes. Whatever goes wrong
        taking it, the job still pauses and the queue goes on."""
        try:
            job = self.srv.tracker().get(run.job_id, with_description=False) or {"id": run.job_id}
            report.take_note(job, run, hide=self._report_hidden(run))  # (what's this run's alone, kept out)
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

    async def _look(self, text: bool = True) -> tuple[dict[str, Any], str]:
        """The page as the desk reads it: its form, and with `text` its words (a read of their own, left out where
        they aren't wanted: the waits look every half second, the review of Oct 15)."""
        data = await self.srv.inspect_form(include_dropdown_options=False)
        said = await self.srv.page_text(4000) if text else ""
        data["challenge"] = await self.srv.browser.challenge_showing()
        return data, said

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
        run.opened_fresh = False
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
        # a form begun afresh: what went into one before is gone with it (an account made there is still there)
        run.went_in = WentIn(accounts=run.went_in.accounts, changed=run.went_in.changed)  # (and a draft's answers changed)
        run.page = await srv.browser.new_tab()
        srv.forget_presets(run.job_id)  # (a fresh form: the country lists it presets are the site's again)
        opened = await srv.open_application(job_id=run.job_id)
        run.page = srv.browser.current_tab or run.page
        if "error" in opened or opened.get("navigation_error"):
            run.status = "failed"
            run.reason = f"Couldn't open the page: {opened.get('error') or opened.get('navigation_error')}"
            self._log(run, run.reason)
            return False
        self._log(run, f"opened {opened['url']}")
        run.opened_fresh = True
        return True

    def _already_done(self, run: Run) -> bool:
        """Marked applied (or past that) since it was queued: by the person on the desk, by
        Claude, or from a confirmation email. It's never filled or submitted again."""
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        if job.get("status") not in FINISHED:
            return False
        run.status, run.need, run.blocking, run.marked = "submitted", "", False, not run.pressed_before
        self._forget_draft_changes(run)  # (gone in: its draft's changes are no longer kept)
        run.reason = f"Already marked {job['status']}, so it isn't applied to again."
        self._log(run, run.reason)
        return True

    async def _drive_again_if_redrawn(self, run: Run) -> None:
        """Drive the job, and once more, from its tab as it is, when a button went from the page as it was
        clicked and nothing like it was there to find (APS's SuccessFactors sign-in, live run 17, Oct 11:
        "KeyError: Nothing clickable matches 'ewr4'")."""
        try:
            await self._drive(run)
        except KeyError as e:
            if "Nothing clickable matches" not in str(e) or run.status in ("skipped", "submitted"):
                raise
            logging.getLogger(__name__).warning("job %s: a button went as it was clicked", run.job_id, exc_info=True)
            self._log(run, "the page changed as I clicked one of its buttons, so I looked at it again")
            await self._drive(run)

    def _maybe_done_elsewhere(self, run: Run) -> tuple[str, list[int]] | None:
        """The card's words when this job may be the same posting as jobs begun or applied to on another site (the
        tracker kept them apart, as it couldn't be sure: Tracker.maybe_same), each of them named, with their ids (the
        ones its Apply anyway clears: apply_anyway); None when there's none, or the person said Apply anyway for each.
        Begun is the tracker's word for it, or this desk's: under way, waiting on the person (but on this card of its
        own), at its review page, sent, or its application begun at all (the tracker says "in progress" only once its
        page is opened: two starting together each read the other as saved, the review of 0.3.206), unless it was
        skipped or failed since. Never for one only saved."""
        def begun_here(job_id: int) -> bool:
            other = self.runs.get(job_id)
            if other is not None and other.status in ("skipped", "failed"):
                return False  # (begun, and then not: the tracker's status says what it came to)
            return job_id in self._slots or other is not None and (
                other.started or other.status in ("running", "ready", "submitted")
                or other.status == "needs_you" and other.need != "maybe_duplicate")

        others = sorted((o for o in self.srv.tracker().maybe_same(run.job_id)
                         if o.get("status") in FINISHED | BEGUN or begun_here(o["id"])),
                        key=lambda o: (o["status"] not in FINISHED, o["id"]))
        if not others:
            return None

        def said(o: dict[str, Any]) -> tuple[str, str, str]:
            done = o["status"] in FINISHED
            when = str((o.get("applied_at") if done else "") or o.get("updated_at") or "")[:10]
            return ("applied to" if done else "started", JOB_BOARDS.get(o.get("board") or "", "the employer's site"),
                    when)

        ids = [o["id"] for o in others]
        if len(others) == 1:
            other = others[0]
            verb, where, when = said(other)
            return (f"This may be the job you already {verb} on {where}{f' on {when}' if when else ''} (job "
                    f"{other['id']}: {other.get('title') or 'a job'} at {other.get('company') or 'the same employer'}). "
                    "Apply anyway, or Skip?"), ids
        named = "; ".join(
            f"job {o['id']}, {o.get('title') or 'a job'} at {o.get('company') or 'the same employer'} "
            f"({o['status'] if o['status'] in FINISHED else 'started'} on {where}{f', {when}' if when else ''})"
            for o in others for _, where, when in [said(o)])
        return f"This may be one of the jobs you already started or applied to: {named}. Apply anyway, or Skip?", ids

    def _held_for_maybe_same(self, run: Run) -> bool:
        """Stop this job on the card that asks whether it's a job begun or applied to on another site
        (_maybe_done_elsewhere), keeping which jobs it named (the ones its Apply anyway clears); False when there's
        nothing to ask."""
        found = self._maybe_done_elsewhere(run)
        if found is None:
            return False
        run.maybe_shown = found[1]
        self._pause(run, "maybe_duplicate", found[0])
        return True

    async def _drive(self, run: Run) -> None:
        srv = self.srv
        if self._already_done(run):
            return
        # one that may be a job begun or applied to on another site: the person's say first, before any page opens
        # (Submit for me included: it never goes past this)
        if self._held_for_maybe_same(run):
            return
        if not run.draft_changed:  # (a draft the desk changed before it was restarted: still held, its card's lines back)
            _restore_draft_changes(srv, run)
        run.status, run.need, run.reason = "running", "", "Working on it"
        if self.tailor and not run.usual_resume:
            job = srv.tracker().get(run.job_id, with_description=False) or {}
            if not tailored_ready(job):  # before its tab opens: nothing to keep waiting
                return self._pause(run, "tailor", _TAILOR_SAYS)
        # left on a "Try Again Later" page, on its own tab still: the person has waited and pressed
        # Resume. Any other way back here (the queue carrying on after an emailed code, say) isn't that.
        waited_out = run.try_later and run.page is not None and not run.page.is_closed()
        run.try_later = False
        run.started = True  # (its application begins: after a restart it's left on a card, never queued by itself)
        if not await self._open(run):
            return
        ps = _Pass(began=time.monotonic())  # what this pass has done so far, lap by lap
        while ps.steps < MAX_STEPS + ps.more:
            ps.steps += 1
            # (filled at once with others: while the person is at another job's tab for the desk, or another job got to
            # this one's site first, nothing is done here; a wait that's no part of the pass's own time)
            ps.began += await self._hold_for_site(run)
            if run.status in ("skipped", "submitted"):  # Skip, or "I submitted it", pressed while it ran
                return
            data, text = await self._look()
            run.page_info = _page_info(data)
            run.url = data["url"]
            run.page = srv.browser.current_tab or run.page
            if (run.seen_form and not ps.pressed and (gone := sorted(confirmations(text)))
                    and not _mid_application(data, text)):
                # Back on a job the desk filled in, before it has pressed anything: the person pressed the
                # site's own Submit and then Resume (Workday's Candidate Home shows "Application Submitted").
                # It went, so it's marked applied and never filled in again. (Not a step of the form that
                # thanks them: "Thank you for your application. Please complete the below questions.")
                srv.tracker().update(run.job_id, status="applied", note="submitted on the site")
                run.status, run.need, run.blocking, run.left = "submitted", "", False, False
                run.marked = not run.pressed_before  # (sent by the person, unless the desk's own press may have)
                self._forget_draft_changes(run)  # (gone in: its draft's changes are no longer kept)
                run.reason = f"Submitted: {_site(run, data)} says \u201c{gone[0]}\u201d."
                self._log(run, run.reason)
                return
            if applied := _applied_before(data, text):
                # applied to already (by hand, or before the tracker knew): never filled, or sent, again
                srv.tracker().update(run.job_id, status="applied", note="the site says it was applied to already")
                run.status, run.need, run.blocking, run.left = "submitted", "", False, False
                run.marked = not run.pressed_before  # (sent by the person, unless the desk's own press may have)
                self._forget_draft_changes(run)  # (gone in: its draft's changes are no longer kept)
                run.reason = f"Applied to already: {_site(run, data)} says \u201c{applied}\u201d It isn't filled in again."
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
            if limited and not (waited_out and not ps.pressed and not ps.sign_ins):
                # "Too Many Attempts. Try Again Later." (Oracle after many sign-up emails in a day):
                # its Continue goes back to the posting, and pressing on only goes round again.
                # Where the person pressed Resume on it, they've waited: carry on from there.
                await self._bring_forward(run)
                self._pause(run, "stuck", f"{_site(run, data)} says \u201c{limited}\u201d Leave this job for a "
                            "while, then press Resume.")
                run.try_later = run.status == "needs_you"
                return
            actions = data.get("actions") or []
            entry_here = any(_way_in(final_text(a["text"])) and not a.get("disabled") for a in actions)
            if kind == "form" and entry_here and not _application_like(data):
                kind = "page"  # a posting with a "send me similar jobs" box: go in through Apply
            if (kind == "page" and not data.get("fields") and _RESET_DONE.search(text[:2000])
                    and not _RESET_SENT.search(text[:2000]) and password_for(data.get("url") or "") is not None
                    and any(re.match(r"^(sign|log) ?in$", a.get("text", "").strip(), re.I) for a in actions)):
                kind = "sign_in"  # "Password has been reset" and its "Sign In" (Workday's, issue #112, note 14)
            # a step of making an account on the email-first sign-in that had none for the email
            view = (_email_account_view(data) if run.account_page and run.account_page == _bare(data.get("url") or "")
                    else None)
            if run.account_page and view is None and kind in ("form", "sign_in"):
                run.account_page = ""  # on to the application (or back at its sign-in): no longer read as one
            # the rest of a Create Account, on a page of its own (UKG Pro's "Almost there!"): not the application
            finish = _account_details(data, text) if kind == "form" else None
            if (run.account_made and kind in ("form", "email_code") and not run.resetting and view is None
                    and finish is None):
                # on from Create Account to the application (its sign-in got in) or to verifying the email:
                # only now is the account said to be made. A form that went away (a Workday site swaps it
                # for its Sign In) isn't that, nor the account's own next step
                self._log(run, run.account_made)
                run.went_in.account(run.account_made)
                run.account_made = ""
                run.account_in = True
            if str(data.get("url") or "").startswith("chrome-error://"):
                host = unreached_host(data, text)
                where = f"{host}, which" if host else "a page that"
                return self._pause(run, "stuck", f"The application went on to {where} couldn't be reached, so the page "
                                   "didn't load. Press Resume to try again, or open the posting in your own browser to "
                                   "apply there.")
            if (kind == "page" and not data.get("fields") and not run.refreshed and run.page is not None
                    and _REFRESH_ASKED.search(text)):
                # KLA's Workday right after its sign-in (live, Oct 2026): "Something went wrong. Please refresh
                # the page and then try again." Done, once a job
                run.refreshed = True
                ps.waited.clear()  # (a new page: its late buttons waited on, whatever was before it)
                # and the steps it goes through again after that are no circle (Circle K's Workday, run 44: asked
                # on Voluntary Disclosures, it started again at My Information), nor more steps than expected. Once a
                # job, so a real circle after it is still caught on its next lap
                ps.since, ps.more = len(ps.pressed), ps.steps
                ps.refreshed_at = len(ps.pressed)
                self._log(run, f"{_site(run, data)} said something went wrong and asked for the page to be "
                          "refreshed, so I reloaded it")
                with contextlib.suppress(Exception):
                    await run.page.reload(wait_until="load")
                continue
            # (on a page with nothing to fill but its language, and its later headings only where the page is a job
            # system's error page with no way on: iCIMS's edge's, run 74, at four employers at once, said "Request Not
            # Acceptable" under "Oops, something went wrong" and "Technical Details:". Never a posting's own words, a dress
            # code's "Not Acceptable", nor a frame's 403 on a posting with its Apply: the review of 0.3.160)
            bare = not _app_fields(data)
            heads = data.get("headings") or []
            deep = heads[1:] if (_ERROR_PAGE.search(text[:2000])
                                 and pick_next(data.get("actions") or [], kind == "form") is None) else []
            refused = next((t for t in [data.get("title") or "", *heads[:1], *deep] if _TURNED_AWAY.search(t)), "")
            if kind in ("page", "form") and bare and refused:
                refusal = re.sub(r"\s+", " ", refused).strip()
                return self._pause(run, "stuck", f"{_site(run, data)} turned the desk's browser away ({refusal}). "
                                   "Open the posting in your own browser to apply there, or press Resume to try again "
                                   "later.")
            # (a web server's own error page and nothing else: the site's trouble, said in its words. Never a page with
            # a box or a way on, nor a posting whose own words say so beside its Apply. A posting's own address that
            # answers so is reloaded once first (nothing pressed on it: a 5xx is the most passing trouble there is);
            # a page a press led to is said at once (UKG Pro's sign-in, run 106: a reload may send its form again);
            # one that answered Submit is never to be reloaded, as the review page says of a Submit pressed before)
            page_title = str(data.get("title") or "")
            failed = page_title if _SERVER_ERROR.search(page_title) or _SERVER_ERROR_CODE.search(page_title) else \
                next((h for h in heads[:1] if _SERVER_ERROR.search(h)), "")
            if (kind == "page" and failed and not data.get("fields")
                    and pick_next(data.get("actions") or [], False) is None):
                words = re.sub(r"\s+", " ", failed).strip()
                site = _site(run, data)
                if run.pressed_before:
                    await self._bring_forward(run)
                    return self._pause(run, "stuck", f"{site}'s page came back as its web server's error page ({words}) "
                                       "after Submit was pressed for this job, with no confirmation showing. Don't "
                                       "reload it (that may send the application again): check your email or the site "
                                       "for a confirmation, then press \u201cI submitted it\u201d if it went through, "
                                       "or open the posting again later and press Resume.")
                if (run.opened_fresh and not ps.pressed and not ps.sign_ins and not ps.agreed and not run.seen_form
                        and not run.start_reloaded and not run.hands_on and not run.refreshed and not run.load_reloaded
                        and not run.accounts_tried and not run.account_made and not run.resetting
                        and not run.reset_from and run.page is not None):
                    run.start_reloaded = True
                    self._log(run, f"{site}'s page came back as its web server's error page ({words}), so I reloaded it")
                    with contextlib.suppress(Exception):
                        await run.page.reload(wait_until="load")
                    continue
                again = (", even after I reloaded it" if run.start_reloaded and run.opened_fresh and not ps.pressed
                         else "")
                await self._bring_forward(run)
                return self._pause(run, "stuck", f"{site}'s page came back as its web server's error page ({words}), "
                                   f"without the application{again}. That's the site's own trouble: reload the page in "
                                   "the browser window (now, or later if it still says so), then press Resume.")
            if kind == "bot_check":
                await self._bring_forward(run)
                return self._pause(run, "bot_check", _BOT_CHECK_SAYS, seen=data)
            if _may_email(run, data, kind, view, finish):
                # one job under way at a time in the steps that may email the person (the inbox is one)
                if inbox_wait := await self._take_inbox(run):
                    ps.began += inbox_wait
                    ps.steps -= 1  # (a wait's no step: the page is looked at again, as it is after it)
                    continue
            elif kind == "form":
                run.inbox, run.inbox_since = False, 0.0  # into its application: another job's sign-in may go ahead
            if view in ("consent", "email", "options"):
                # the desk's to take on only where it began making the account (settings.manage_accounts);
                # the code step is an emailed code's, below (with its Submit pressed: _account_code_button)
                if run.accounts_tried and _may_manage_accounts():
                    if await self._email_account_step(run, data, view):
                        continue
                    return
                await self._bring_forward(run)
                return self._pause(run, "sign_in", f"{_site(run, data)} has no account for your email yet, and its "
                                   "steps of making one are yours: finish them in the browser window (or sign in with "
                                   "the email you use there); the desk carries on by itself after that.", seen=data)
            if (kind == "sign_in" and not ps.account_waited and not run.resetting and not _account_and_application(data)
                    and sum(f.get("kind") == "password" and not _code_box(f) for f in data.get("fields") or []) >= 2):
                # A Create Account form: Qorvo's draws its application below it a moment later
                ps.account_waited = True
                await self._wait_for_application()
                continue
            if kind == "sign_in" and _account_and_application(data):
                return await self._apply_with_account(run, data)
            manage = (kind in ("sign_in", "page", "email_code") or run.resetting) and _may_manage_accounts()
            if (manage and run.resetting and run.reset_opened and not _new_password_boxes(data, text)
                    and (refused := _reset_refused(data, text))):
                # the reset link from the inbox opened on a page that says it's spent (expired, used, invalid), with no
                # new-password boxes: nothing to set there, and the account is there (never a Create Account)
                if run.reset_spent_said and run.reset_from and run.page is not None:
                    # Resume on its card, the tab still on the spent page: the person set the password through a new
                    # email's link in their own browser, as the card asks. Back to the sign-in for one more try; a
                    # refusal there is said (reset_opened), never a Create Account
                    run.resetting, run.reset_spent_said = False, False
                    await self._back_to_sign_in(run)
                    ps.sign_ins.clear()
                    run.sign_in_tries = min(run.sign_in_tries, SIGN_IN_TRIES - 1)
                    continue
                run.reset_spent_said = True
                await self._bring_forward(run)
                site = _site(run, data)
                return self._pause(run, "sign_in", f"The password reset link from your email opened on {site}'s page "
                                   f"saying \u201c{refused.rstrip(' .')}\u201d, so the desk couldn't set your saved "
                                   f"password there. Ask {site} for a new reset through its \u201cForgot password\u201d and open "
                                   "the new email's link, set the password you saved on the desk as the new one (or "
                                   "another, and save that one on the desk), then press Resume: the desk signs in with it.",
                                   seen=data)
            # On the reset's "email sent" page, or back from waiting on an email that didn't come, on whatever
            # page the site left (Workday's reset form stays, filled, with its "if an account exists" note,
            # which can read as a form: so before a form's "past the sign-in" below)
            sent_page = kind in ("page", "email_code") and bool(_RESET_SENT.search(text))
            if manage and run.resetting and (sent_page or run.reset_no_mail):
                if not run.reset_waited:
                    return await self._await_reset_email(run, data)
                if run.reset_no_mail and run.reset_spent:
                    # its wait ran out on an email whose link was opened already (a site that sent the same link again,
                    # or an earlier reset's): an email came, so the account is there, and it's never "no email: no
                    # account there, make one". Left where it is: a new reset's link opened in the tab is set on Resume
                    run.reset_no_mail = run.reset_spent = False
                    run.reset_opened = run.account_known = True
                    await self._bring_forward(run)
                    site = _site(run, data)
                    return self._pause(run, "sign_in", f"{site} emailed you a password reset, but its link is one the desk "
                                       "opened already (a reset link works once), so it couldn't set your saved password "
                                       f"with it. Ask {site} for a new one through its \u201cForgot password\u201d in the "
                                       "browser window, or open that email's link yourself; set the password you saved on "
                                       "the desk as the new one (or another, and save that one on the desk), then press "
                                       "Resume: the desk signs in with it.", seen=data)
                # Resume pressed on it: the person set the password from the email themselves (or the
                # inbox wait ran out), so back to the sign-in page, where the saved password is tried again.
                # No email in RESET_MAIL_WAIT: most likely no account there, so the way to a new one instead.
                run.resetting = False
                if run.reset_from and run.page is not None:
                    await self._back_to_sign_in(run)
                    ps.sign_ins.clear()
                    if run.reset_no_mail:
                        self._log(run, f"no password reset email came from {_site(run, data)} in "
                                  f"{RESET_MAIL_WAIT // 60} minutes, so I went back to make an account there")
                        ps.sign_ins["submitted"] = ps.sign_ins["wants_account"] = 1
                    else:  # the saved password is the site's now, as the person was asked: one more sign-in
                        run.sign_in_tries = min(run.sign_in_tries, SIGN_IN_TRIES - 1)
                    continue
            if kind == "form" and finish is None:
                run.resetting = False  # past the sign-in, however the password was set
                run.sign_in_tries = 0  # (and none of its sign-ins was refused)
                run.reset_set = run.reset_opened = run.reset_spent = run.reset_spent_said = False  # (a refusal after
                # this is the ordinary one)
            if kind == "sign_in":
                # signed in (or an account made) since a refresh for "Something went wrong": the page after it is the
                # account's, not what the refresh brought back (presses alone count the steps: the review of 0.3.172)
                ps.refreshed_at = None
            if kind == "sign_in" and manage and run.resetting:
                done_reset = await self._set_new_password(run, data, text)
                if done_reset == "set":
                    ps.sign_ins.clear()  # the saved password is the site's now: signed in with afresh
                    run.sign_in_tries = min(run.sign_in_tries, SIGN_IN_TRIES - 1)
                    continue
                if done_reset == "paused":
                    return
            said_exists = " ".join([*(data.get("errors") or []), text[:3000]])
            if kind == "sign_in" and manage and ps.sign_ins.get("made") and _ACCOUNT_EXISTS.search(said_exists):
                # the email has an account there already, and the saved password didn't sign in to it
                asked = await self._ask_for_reset(run, data, ps.sign_ins)
                if asked == "paused":
                    return
                if asked == "no_account":
                    ps.sign_ins["submitted"] = ps.sign_ins["wants_account"] = 1
                    continue
            if kind == "sign_in" and manage and not run.verifying and _wants_verification(run, data, text, ps.sign_ins):
                # the account just made says it emailed a link to verify it (Banner Health's Workday, live,
                # Oct 2026): its Sign In takes no password before the link is opened. Or an account there from
                # before, never verified, refuses the sign-in for that (Vanguard's, the test identity, run 98:
                # "Verify your account before you sign in or request a verification email", in the page's own
                # alert: a sign-in's help or a one-box Create Account form saying so elsewhere isn't a refusal, the
                # review of 0.3.178): the same wait, and not a password reset, which such an account gets no email
                # for (runs 96 and 97: none in 3 minutes). Or the account made in this job on an earlier pass (a
                # Resume before its link was opened), the page still saying it emailed the link (the review's).
                # Not on the pass after that wait (the link opened from the inbox, or the person's Resume: the
                # account is verified, though the page still shows the words it refused with): the saved password
                # is tried once, which spends run.verifying
                return await self._await_verify_email(run, data, text)
            if kind == "sign_in":
                # With the inbox watched, a refused password is reset before a new account is made (the
                # owner's choice: an account there already is the likelier, on a second application)
                reset_first = manage and not run.reset_asked and self.mail_login() is not None
                shown = data  # (what the page said before it's filled in again)
                done = await self._sign_in(run, data, ps.sign_ins, reset_first=reset_first)
                if done == "submitted":
                    run.verifying = False  # (the one sign-in after a verification wait, spent: refused again, it waits again)
                if done in ("email_step", "submitted", "create_account"):
                    ps.sign_ins[done] = ps.sign_ins.get(done, 0) + 1
                    if done == "create_account":
                        # its form a moment after the press (Edwards Vacuum's SAP screen set, run 60: the sign-in read
                        # again at once, its link pressed again, which starts it over, and the desk stopped)
                        await self._wait_for_change(data, LATE_BUTTONS_WAIT)
                    continue
                if done == "refused":
                    if manage and not run.verifying:
                        # the refusal as it stands now, not as the lap's look had it: Workday's comes a moment after the
                        # press (Vanguard's, run 100: the look saw the form alone, the sign-in counted as refused, and a
                        # reset was asked for). Its alert may still be on its way where the sign-in counted as refused
                        # at once (no settle wait on this pass: the review): a moment more for it. For want of the
                        # account's verification (an account from before, or the one just made), it's the wait
                        shown_now, shown_text = await self._look()
                        deadline = time.monotonic() + REFUSAL_SETTLE
                        while (not shown_now.get("errors") and time.monotonic() < deadline
                               and sum(f.get("kind") == "password" and not _code_box(f)
                                       for f in shown_now.get("fields") or []) == 1):
                            await asyncio.sleep(0.5)
                            shown_now, shown_text = await self._look()
                        if _wants_verification(run, shown_now, shown_text, ps.sign_ins):
                            # (the account made: its sign-in waits on the link, it isn't refused)
                            return await self._await_verify_email(run, shown_now, shown_text)
                    run.account_made = ""  # (its sign-in is refused: no account was made)
                asked = await self._ask_for_reset(run, data, ps.sign_ins) if done == "refused" and manage else None
                if asked == "paused":
                    return
                if asked == "no_account":  # back on the sign-in page: its way to a new account, next
                    ps.sign_ins["submitted"] = ps.sign_ins["wants_account"] = 1
                    continue
                await self._bring_forward(run)
                if done in ("prefilled", "filled"):
                    data, _ = await self._look(text=False)
                    run.page_info = _page_info(data)  # the page as filled
                if done == "prefilled" and _account_and_application(data):
                    return await self._apply_with_account(run, data)  # its application was drawn after all
                if (done == "prefilled" and manage and not run.accounts_tried and run.account_answers
                        and _security_asked(data) and not _security_answered(data)):
                    # as answered on the card: in, its button is pressed next (_make_account)
                    if await self._fill_account_answers(run, data) == "failed":
                        return self._pause(run, "sign_in", f"I filled in {_site(run, data)}'s Create Account form with "
                                           "your details and saved password, but couldn't put your answers to its "
                                           "security questions into it. Fill them in in the browser window, tick their "
                                           "terms box if there is one and create the account; the desk carries on after "
                                           "that.", seen=data)
                    data, _ = await self._look(text=False)
                if done == "prefilled" and manage and not run.accounts_tried and await self._make_account(run, data):
                    # a new account: its sign-in (where the site asks for one) is tried afresh, once more
                    # however many the refused password had
                    # (and its settle wait is the new sign-in's own: spent on the page before the account, the next
                    # press counted as refused at once, before Workday's "An email has been sent" was drawn: 0.3.180)
                    ps.sign_ins.pop("submitted", None), ps.sign_ins.pop("create_account", None), \
                        ps.sign_ins.pop("settled", None)
                    ps.sign_ins["made"] = 1
                    run.sign_in_tries = min(run.sign_in_tries, SIGN_IN_TRIES - 1)
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
                             "with them; if you do have an account, sign in instead."
                             if ps.sign_ins.get("create_account") else "")
                    fields = data.get("fields") or []
                    user = (" (your email as its user name: sign in there with it)" if any(
                        _user_name_box(f) for f in fields if f.get("kind") in ("text", "email")) else "")
                    asks = ("Fill in anything it still asks for (a picture code, say), tick their terms box if there is "
                            "one and create the account (then verify your email if they ask)")
                    card: list[dict[str, Any]] = []
                    if _security_asked(data):  # (never made up, so never pressed for the person: _make_account)
                        captcha = _captcha_on(data)
                        if _security_answered(data):  # (yours from the card, in: its button wasn't pressed)
                            statement = _privacy_statement(data) is not None and not run.statement_accepted
                            asks = ("Your answers to its security questions are in" + (
                                    ", and its CAPTCHA is yours to solve: solve it" if captcha else "") + (
                                    ". It asks you to read and accept its data privacy statement, which agrees to "
                                    "something in your name, so it's yours: open it in the browser window and accept it "
                                    "if you're happy to" if statement else "") + ". Tick their terms box if there is one "
                                    "and create the account (then verify your email if they ask)")
                        else:  # (a CAPTCHA too: the person's whatever's answered here, so no card)
                            card = [] if run.accounts_tried or captcha else await self._account_card(run, data)
                            asks = ("It also asks for security questions and their answers: those are yours to choose "
                                    "(the desk never makes up answers). " + (
                                        "Answer them here (they go into this form alone, and are never remembered) and "
                                        "the desk creates the account, or fill them in in the browser window and create "
                                        "the account yourself" if card else "Fill them in there" + (
                                            ", solve its CAPTCHA (that's yours)" if captcha else "")
                                        + " and create the account"))
                    elif _captcha_on(data):  # (never touched, so never pressed for the person either)
                        asks = ("Its CAPTCHA is yours to solve: solve it, tick their terms box if there is one and create "
                                "the account (then verify your email if they ask)")
                    elif _privacy_statement(data) is not None and not run.statement_accepted:  # (SuccessFactors')
                        asks = ("It asks you to read and accept its data privacy statement, which agrees to something in "
                                "your name, so it's yours: open it in the browser window and accept it if you're happy "
                                "to, then create the account (then verify your email if they ask)")
                        first += (" (The desk accepts an employer's privacy notice for you with accept_notices: true "
                                  "under settings: in profile.yaml, outside practice mode.)" if not _may_accept_notices()
                                  else " I tried to open it and accept it for you, and couldn't." if manage else "")
                        data, _ = await self._look(text=False)  # (the statement it opened for them is open still: paused on)
                    return self._pause(run, "sign_in", f"I filled in {_site(run, data)}'s Create Account form with your "
                                       f"details and saved password{user}. {asks}; the desk carries on after that."
                                       + first, card or None, seen=data)
                if done == "filled":
                    return self._pause(run, "sign_in", f"I filled in your email and saved password on {_site(run, data)}'s "
                                       "sign-in form. Press its sign-in button in the browser window; the desk carries on "
                                       "after that.", seen=data)
                if done == "refused" and (run.reset_set or run.reset_opened):
                    # Right after the desk set the saved password through the site's own reset, or opened its link: the
                    # account is there, so never a Create Account (KLA's Workday, the reset probe's second pass, run
                    # 136: a spent link's reset set nothing, and the desk pressed Create Account)
                    told = next((e for e in data.get("errors") or [] if not _ERROR_COUNT.match(e.strip())), "")
                    it_says = f" (it says \u201c{told[:160].rstrip(' .')}\u201d)" if told else ""
                    opening = (f"I set your saved password through {_site(run, data)}'s password reset, and it still "
                              f"doesn't sign in there{it_says}. The site may not have taken it" if run.reset_set else
                              f"Your saved password still doesn't sign in on {_site(run, data)} after its password "
                              f"reset{it_says}. The reset may not have set it")
                    return self._pause(run, "sign_in", opening + ": a reset link works once, and some sites won't take "
                                       "back a password you've used there before. Reset it there to the one you saved on "
                                       "the desk (or another, and save that one on the desk), then press Resume: the desk "
                                       "signs in with it.", seen=data)
                if done == "refused":  # and no reset (one was asked for already, or the site has no way to one)
                    told = next((e for e in data.get("errors") or [] if not _ERROR_COUNT.match(e.strip())), "")
                    return self._pause(run, "sign_in", f"Your saved password didn't sign in on {_site(run, data)}"
                                       + (", before or after I pressed its Create Account" if run.accounts_tried else "")
                                       + (f" (it says \u201c{told[:160].rstrip(' .')}\u201d)" if told else "") + "."
                                       + (" So that your account there isn't locked, the desk won't try it again for "
                                          "this job unless you press Resume." if run.sign_in_tries >= SIGN_IN_TRIES else "")
                                       + " Sign in in the browser window and the desk carries on by itself after that; or "
                                       "reset the password through its \u201cForgot password\u201d to the one you saved on "
                                       "the desk, then press Resume: the desk signs in with it.", seen=data)
                tip = _password_tip(data["url"])
                failed = " Your saved password didn't sign in there." if ps.sign_ins.get("submitted") else ""
                return self._pause(run, "sign_in", f"Sign in (or create your account) on {_site(run, data)} in "
                                   "the browser window; the desk carries on by itself after that." + failed + tip,
                                   seen=data)
            if kind == "email_code":
                await self._bring_forward(run)
                watching = (" Your email app password is saved, so the desk is also watching your inbox for it."
                            if self.mail_login() else "")
                if any(_code_box(f) for f in data.get("fields") or []):
                    return self._pause(run, "email_code", "The site emailed you a code. Enter it in the browser "
                                       "window; the desk carries on by itself after that." + watching, seen=data)
                # The link opens in the person's own browser, which leaves this tab where it is
                return self._pause(run, "email_code", "The site emailed you a link to confirm your email. Open it, then "
                                   "reload this job's tab in the desk's browser window: the link opens in your usual "
                                   "browser, so the tab doesn't change by itself. The desk carries on after that."
                                   + watching, seen=data)
            if (notice := _notice(data)) is not None:  # the boxes behind it are filled once it's answered
                # (over a page with boxes, its language picker alone too, as before 0.3.161: its review)
                said = await self._answer_notice(run, data, notice, ps.agreed,
                                                 over_form=kind == "form" or bool(data.get("fields")))
                if said in ("agreed", "started"):
                    continue
                if said == "paused":
                    return
            if finish is not None:  # its boxes, its terms and its button (with settings.manage_accounts)
                if await self._finish_account(run, data, finish):
                    continue
                return
            if kind == "form" and ps.sign_ins.get("create_account") and _email_first_account(data):
                # the Create Account opened for the saved password asks for the email first (BrassRing's,
                # live, Oct 2026: its Continue emails a passcode there, the account's first step). The
                # desk's to go on with only with settings.manage_accounts, and never with a CAPTCHA on it
                captcha = _captcha_on(data)
                if not captcha and _may_manage_accounts():
                    if await self._email_account_step(run, data, "email"):
                        continue  # (its passcode next: the inbox's, or the person's)
                    return
                await self._bring_forward(run)
                asks = (", with a CAPTCHA that's yours to solve: enter your email and solve it there"
                        if captcha else ". Making the account is yours: enter your email there")
                return self._pause(run, "sign_in", f"Your saved password didn't sign in on {_site(run, data)}, so I "
                                   f"opened its Create Account, which asks for your email first{asks}, and go on in "
                                   "the browser window (or sign in with the email you use there); the desk carries on "
                                   "by itself after that.", seen=data)
            if kind == "form":
                run.seen_form = True
                run.came_back_empty = False  # (the site's back: an empty page later is another's, the review of 0.3.171)
                once_failed = await self._fill_once(run, data)
                ps.filled_values |= {norm(v) for v in run.once.values() if isinstance(v, str) and v.strip()}
                key = _fingerprint(data)
                if key not in ps.entries_done and _EXPERIENCE_PAGE.search(" ".join(data.get("headings") or [])):
                    ps.entries_done.add(key)
                    for section in ("work", "education"):
                        # (one block fewer for each school deleted from this job's form: never added back)
                        added = await srv.add_entries(section, **({"count": len(profile_entries(
                            config.Profile.load(), "education_history")) - len(run.schools_dropped)}
                            if section == "education" and run.schools_dropped else {}))
                        if added.get("clicks"):
                            self._log(run, f"added {added['clicks']} {section} block(s)")
                            ps.blocks_added[section] = ps.blocks_added.get(section, 0) + added["clicks"]
                        if added.get("add_button_covered"):
                            self._log(run, f"couldn't press the {section} section's Add button: something on the page "
                                      "covers it, so its blocks are left as they are")
                        self._note_short_entries(run, section, added, data)
                # a resume kept from before in its box (Workday's), where this job has one tailored for it: deleted, for
                # the fill to put the tailored one in its place; or, once the page is filled, the person's (paused)
                replacing = await self._replace_kept(run, data)
                result = await self._autofill(run, data, ps.filled_ids, {str(b["id"]) for b in replacing[0]})
                if result["filled"]:
                    self._log(run, f"filled {len(result['filled'])} field(s) on {_where(data)}")
                ps.filled_ids |= {str(f["id"]) for f in result["filled"] if f.get("id")}
                ps.filled_values |= {norm(f["value"]) for f in result["filled"] if f.get("value") not in (None, "")}
                if not await self._replaced_in(run, data, replacing, result):
                    return
                _required_by_errors(result, data)
                pending, missing_files = _pending(result, once_failed)
                self._note_skipped(run, result)
                left = len(pending)
                pending = await self._attest(run, pending)
                attested = len(pending) < left
                before, page_key = data, (_steady_url(data.get("url")), tuple(data.get("headings") or []))
                data, text = await self._look()  # filling can add or enable things (State after Country, Submit)
                run.page_info = _page_info(data)  # what the person sees on the desk: the page as filled
                _went_in(run, result["filled"], before, data, shown=data)  # (and on the job's "What went in" panel)
                _went_left(run, result, before, data)
                if ps.back_from is not None and (result["filled"] or attested or _values(before) != _values(data)):
                    # something went in on the way back (the profile's, the person's own answers put back, an agreement):
                    # the step is pressed again, whatever comes up first (a notice the tick opened: the second review)
                    ps.back_from = None
                if (notice := _notice(data)) is not None:  # one the fills brought up (Eightfold's, as the resume went up)
                    said = await self._answer_notice(run, data, notice, ps.agreed, over_form=True)
                    if said in ("agreed", "started"):
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
                    return f is not None and not is_empty_value(f.get("value")) and not unset_choice(f)

                def refused(q: dict[str, Any]) -> bool:  # a box lost while being drawn again wasn't turned down
                    return bool(q.get("error")) and not str(q["error"]).startswith("KeyError")

                pending = [q for q in pending if refused(q) or not answered(q)]
                if (_new_required(before, data) or result.get("countries_corrected")) and page_key not in ps.refilled:
                    # answers drew new questions ("If yes, explain"): fill those too; or a country the site preset,
                    # corrected, drew that country's boxes over the old ones' ids (Onto's Workday, run 118: Mexico's
                    # "Father's Family Name" relabelled Last Name), and what was planned for the other is gone
                    ps.refilled.add(page_key)
                    continue
                if not run.list_reloaded and not run.hands_on and not run.pressed_before and run.page is not None \
                        and (unloaded := _unloaded_lists(pending, data, config.Profile.load())):
                    # a school's Degree (or major) list with its placeholder alone, no choice at all (Grand Canyon
                    # University's Workday, run 61): the page didn't finish loading it. Reloaded once a job (never
                    # after a Submit, nor after the person's Resume: what they typed there would go), and the page
                    # filled again, its blocks added again; still empty, it's said for what it is (_entry_gaps). Before
                    # a school's block is deleted for having no fitting choice: an empty list has none for anything
                    run.list_reloaded = True
                    ps.entries_done.clear()  # (the blocks it added are gone with the reload)
                    ps.blocks_added.clear()
                    ps.filled_ids.clear()
                    ps.filled_values.clear()
                    ps.refilled.discard(page_key)
                    ps.bare_waited.discard(_page_key(data))
                    ps.step_waited.difference_update({d for d in ps.step_waited if d and d[0] == _page_key(data)})
                    ps.waited.clear()
                    ps.since, ps.more = len(ps.pressed), ps.steps
                    self._log(run, f"{_site(run, data)}'s {_unloaded_said(unloaded)} showed no choices at all, so I "
                              "reloaded the page")
                    with contextlib.suppress(Exception):
                        await run.page.reload(wait_until="load")
                    continue
                if await self._drop_unfit_school(run, data, pending):
                    continue  # the page looked at again without that school's block
                if await self._drop_added_extra(run, data, pending, ps.blocks_added, ps.extra_tried, ps.filled_ids,
                                                ps.filled_values):
                    continue  # and without the empty block it had added beside a saved one
                gaps, pending = self._entry_gaps(run, data, pending)
                on_experience = _EXPERIENCE_PAGE.search(" ".join(data.get("headings") or []))
                short = self._short_entries(run) if on_experience else {}
                if short:  # (first on the card: the rest of what it needs comes after)
                    gaps = f"{_short_said(_site(run, data), short)} {gaps}".strip()
                pending = await self._said_above(pending)
                if self._stop_for_what_is_left(run, gaps, pending, missing_files):
                    run.entries_told = short
                    return
                if ps.back_from is not None:
                    (back_page, stuck), ps.back_from = ps.back_from, None
                    if back_page == _step_page(data):
                        return self._pause(run, "stuck", stuck)  # (nothing to fill, tick or ask: pressing again won't do)
                actions = data.get("actions") or []
                entry_here = any(_way_in(final_text(a["text"])) and not a.get("disabled") for a in actions)
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
            if note is not None and note["text"] not in ps.dismissed:
                ps.dismissed.add(note["text"])  # the posting under it is hidden until it goes
                await srv.click(note["id"])
                self._log(run, f"dismissed a note (\u201c{note['text'].strip()}\u201d)")
                continue
            sign_in_step = bool(_SIGN_IN_STEP.search(" ".join(data.get("headings") or [])))
            # (a page with nothing but its language picker too: _app_fields, the review of 0.3.161)
            blank = not (_app_fields(data) or data.get("actions") or data.get("headings")) and len(text.strip()) < 40
            # (a gate's page is read whole for a legal waiver, past the 4,000 characters a look reads: a long privacy
            # agreement's arbitration clause sits at its end, the review of 0.3.176)
            gate_text = (await srv.page_text(40000) if action is None and _agreement_button(data) is not None else text)
            gate_now = action is None and (button := _agreement_button(data)) is not None \
                and f"{_page_key(data)} {button['text'].strip()}" not in ps.agreed \
                and _terms_gate(button, data, gate_text) and _may_accept_notices()
            # (a form with no button at all yet is a step still being drawn too: KLA's Workday showed only the last
            # step's resume box for a while, its questions and Save and Continue drawn after, live run 9, Oct 11)
            # (a greyed-out step button, or a page busy loading, is the branch below's)
            bare_form = (kind == "form" and not data.get("busy") and not any(
                             not a.get("cookie") and _STEPISH.search(final_text(a.get("text") or ""))
                             for a in data.get("actions") or []) and not _greyed_step(data)
                         # (not one whose stop is explained: an agreement to press, a bot check)
                         and _agreement_button(data) is None and not data.get("captcha")
                         and not _BOT_BADGE.search(" ".join([text[:3000], *(a.get("text") or "" for a in data.get("actions") or [])])))
            if action is None and bare_form and _page_key(data) not in ps.bare_waited and not gate_now:
                # once a step, whatever was waited on before it on this pass (KLA's page reloaded after "Something
                # went wrong" drew its buttons late too, and its bare step after it went unwaited: live run 17)
                ps.bare_waited.add(_page_key(data))
                if await self._wait_for_step(STEP_LOAD_WAIT, data):
                    continue
            elif action is None and kind == "page" and (_page_key(data), run.logged) not in ps.waited and not gate_now:
                # slow pages (Intel's Workday, Eightfold forms) draw their buttons late (and a page reached again, the
                # desk having done something since, is a new visit: its address can be the same)
                ps.waited.add((_page_key(data), run.logged))
                # (a page a press led to with nothing drawn on it, its shell's words and all: the blank page's wait, note 40)
                empty_now = bool(ps.pressed) and _nothing_drawn(data, text)
                wait = SIGN_IN_STEP_WAIT if sign_in_step else BLANK_PAGE_WAIT if blank or empty_now else LATE_BUTTONS_WAIT
                if await self._wait_for_progress(wait, data):
                    continue
            drawn = (_page_key(data), _boxes(data))  # a page that has drawn more since is waited on again
            loading = bool(data.get("busy")) and not gate_now
            if action is None and drawn not in ps.step_waited and (loading or kind == "form" and _greyed_step(data)):
                # a form still being drawn: Oracle's Personal Info shows its upload boxes and a greyed-out
                # Next first, then its name, email and phone boxes and an enabled Next. Or a step still
                # loading: KLA's Workday showed its loading dots where its Application Questions go, and a
                # greyed-out Save and Continue, for a while (live, Oct 2026)
                ps.step_waited.add(drawn)
                if await self._wait_for_step(STEP_LOAD_WAIT if loading else LATE_BUTTONS_WAIT, data):
                    continue
            # a page still loading that nothing else explains the stop of (an agreement to press, a bot check, a closed
            # posting or a site down are said for what they are, the review of 0.3.147), and one the desk may reload
            # (once a job; never after a Submit was pressed, as a reload may send a form again, nor after any Resume of the
            # person's, a questions card's too, as what they typed there would go: Applier.resume, run.hands_on)
            reloadable = loading and _agreement_button(data) is None and not data.get("captcha") and not _BOT_BADGE.search(
                " ".join([text[:3000], *(a.get("text") or "" for a in data.get("actions") or [])])) \
                and not _closed_notice(data, text) and not _maintenance_page(data) and not _account_step(data) \
                and not any(g.startswith(f"{_page_key(data)} ") for g in ps.agreed) \
                and not any(_errorish(e) for e in data.get("errors") or []) \
                and not (data.get("empty_frame") and (ps.refreshed_at == len(ps.pressed)
                                                      or run.came_back_empty and not ps.pressed))
            # (Workday's application frame left empty is a step still loading, GCU's after its sign-in, run 91; not the
            # one its "Something went wrong" refresh brought back, nor that page after Resume: the site's, said below,
            # 0.3.158 and 0.3.171, the review of 0.3.172)
            # (nor after any Resume of the person's, a questions card's too: what they typed into the page would go, the
            # review of Oct 15, as for every other reload here)
            may_reload = reloadable and not run.load_reloaded and not run.pressed_before and not run.hands_on \
                and run.page is not None
            if action is None and sign_in_step and not may_reload:
                # Workday's sign-in step whose sign-in buttons never drew (Applied's, now and then):
                # read as an ordinary page, so only a change from this one is the person past it. One still loading
                # (Baker Tilly's, run 62: the whole step Workday's loading bars) is reloaded first, below
                await self._bring_forward(run)
                if loading:
                    return self._pause(run, "sign_in", f"{_site(run, data)}'s sign-in step hasn't finished loading. "
                                       "Reload it in the browser window, or sign in there (its Sign In at the top); "
                                       "the desk carries on by itself after that.", seen=data)
                return self._pause(run, "sign_in", f"Sign in (or create your account) on {_site(run, data)} in "
                                   "the browser window; the desk carries on by itself after that.", seen=data)
            if action is None:
                if await self._no_way_on(run, ps, data, text, blank=blank, drawn=drawn, reloadable=reloadable,
                                         may_reload=may_reload) is _Then.STOP:
                    return
                continue
            if _DRAFT_ENTRY.match(final_text(action["text"])) and not _own_draft(
                    action, data, str((srv.tracker().get(run.job_id, with_description=False) or {}).get("url") or "")):
                # an application kept from an earlier visit, on a page that doesn't say it's this job's (a candidate
                # home, a sidebar: another job's draft would be filled, and its review page shown, as this job's)
                await self._bring_forward(run)
                return self._pause(run, "stuck", f"{_site(run, data)} shows “{action['text'].strip()}”, for an "
                                   "application it kept from an earlier visit, but nothing on the page says it's this "
                                   "job's, so I didn't press it. If it is, press it in the browser window, then press "
                                   "Resume; if not, open this job's posting there, or skip the job.")
            if (kind == "form" and action.get("form_fields") == len(data.get("fields") or [])
                    and _sign_up_box(data, [action])):
                return await self._sign_up_pause(run, data, action)  # the same box, sent by a plain button
            if _APPLY_SENDS.match(final_text(action["text"])) and (kind == "form" or kind == "page" and _application_like(data)):
                # "Apply" on the form just filled in, or on a page that holds an application (SuccessFactors
                # shows a signed-in person's profile greyed out above its questions): the button that sends
                # it, which no live site has had as a way in. Never pressed for the person. (A posting the
                # site went back to, with no form on it, still has its Apply as the way in.)
                return await self._theirs_to_send(run, data, action)
            # (and which visit to the job: its Apply pressed again once it's opened again isn't a third press, EY's,
            # Freeport's and SRP's SuccessFactors, nightly Oct 11: "Apply now", then its menu's "Apply Now", before)
            key = _step_key(data, action["text"], run)
            # (the presses since a refresh the site asked for: the steps it takes again are new)
            lately = ps.pressed[ps.since:]
            # (and for a step pressed three times, or a circle, those since the desk set right what the page said was
            # wrong: Entegris' Self Identify, run 54, its Date typed again after the second press, then never pressed a
            # third time; Intel's, run 56, its "Errors Found" heading gone once the Date was, read as back where it was)
            tries = ps.pressed[max(ps.since, ps.fixed_at):]
            if key in tries and ps.pressed[-1] != key:
                # Round in a circle (Oracle sent its sites back to the posting from "Continue"):
                # going round again only repeats it, and may email the person another code.
                lap = " \u2192 ".join(ps.pressed_on[ps.pressed.index(key, max(ps.since, ps.fixed_at)):])
                return self._pause(run, "stuck", f"The site keeps sending me back to {_where(data)} instead of into "
                                   f"the application ({lap}). Go one step into it in the browser window, then press "
                                   "Resume.")
            test = _assessment_button(data)
            # "next" on a list of jobs pressed again and again: its pages differ, but no application opens. Only
            # presses on pages with no form count (Wells Fargo's Workday, run 55: its Application Questions drew its boxes
            # a moment late, and the two steps' Save and Continue before it, at the same address, read as two such)
            circling = kind == "page" and [p[:1] + p[2:4] for p in lately[-2:]] == [key[:1] + key[2:4]] * 2 \
                and all(p in ps.on_pages for p in lately[-2:])
            if test and (circling or kind == "form" and tries[-3:] == [key] * 3):
                # a step that won't go on until the applicant takes the employer's assessment (Circle K's Workday,
                # the nightly of Oct 12: "Take Assessment" beside its Save and Continue): a test of the person
                return await self._assessment_pause(run, data, test)
            if circling:
                # "next" on a list of jobs (a link to a search page): its pages differ, but no
                # application ever opens
                return self._pause(run, "stuck", f"I pressed \u201c{action['text'].strip()}\u201d three times on "
                                   f"{_where(data)} and no application opened. Have a look in the browser, then press "
                                   "Resume.")
            if kind == "form" and tries[-3:] == [key] * 3 and key not in ps.saved_waited:
                # a step still saving: KLA's Workday took a while over its Application Questions 2 of 2 (live run
                # 11, Oct 11: the desk stopped after the fourth press, and the next step was there a little later).
                # Waited on once, with nothing said wrong, before the step counts as stuck
                ps.saved_waited.add(key)
                if not any(_errorish(e) for e in data.get("errors") or []) and \
                        await self._wait_for_step_change(key, STEP_SAVE_WAIT):
                    continue
            if kind == "form" and tries[-3:] == [key] * 3:
                # the same step button pressed three times on the same step: Cornerstone's (Matheson's,
                # under load, Oct 10) redraws its page a little after each press, so no single press
                # looked like a stall, and the desk pressed Next fifteen times
                problems = _flagged(data) or [
                    f"\u201c{a['text'].strip()}\u201d is greyed out, so the site still wants something (a file, say, "
                    "or a box to tick)" for a in _greyed_step(data)[:1]]
                if not problems and (kept := await self._resume_not_kept(run, data)):
                    if kept == "again":
                        # (a new start on the step: its way on pressed again)
                        ps.stalls, ps.fixed_at = 0, len(ps.pressed)
                        continue
                    return
                errors = "; ".join(problems)[:300].rstrip(" .")
                return self._pause(run, "stuck", "The page didn't move on" + (f": {errors}." if errors else ".")
                                   + " Fix it in the browser, then press Resume.")
            if time.monotonic() - ps.began > PASS_SECONDS:
                # (before a press, never after one: a last press that reached the review page goes to it, the review
                # of 0.3.189)
                await self._bring_forward(run)
                return self._pause(run, "stuck", f"This application has taken longer than {PASS_SECONDS // 60} minutes "
                                   "without reaching its review page, so I stopped here. Check where it is in the "
                                   "browser window, press its Next if there's more, then press Resume.")
            ps.pressed.append(key)
            run.pass_steps.add(key)  # (the steps gone through, for the pass's line in the desk's history)
            if kind == "page":
                ps.on_pages.add(key)
            ps.pressed_on.append(f"\u201c{action['text'].strip()}\u201d on {_page_said(data)}")
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
                                   "and isn't a step button I know, so I didn't press it. Nothing was sent. This is as "
                                   "far as practice mode goes here: skip the job, or turn practice mode off to go on.")
            if clicked.get("clicked") is False and str(clicked.get("blocked") or "").startswith("Cookie"):
                # never picked by the desk (it only declines banners), but never taken for the review page
                return self._pause(run, "stuck", "A cookie or privacy banner is in the way, and the desk never accepts "
                                   "one for you. Choose in the browser window, then press Resume.")
            if clicked.get("clicked") is False:  # the guard says it's the final submit
                return await self._finish(run, data, text)
            self._log(run, f"clicked “{action['text']}”")
            if _DRAFT_ENTRY.match(final_text(action["text"])) and kind == "page" and not run.draft:
                # an application the site kept from an earlier visit (a draft), with the answers it was given then:
                # the profile's may have changed since (issue #112, note 38: the work descriptions), so its boxes are
                # checked against the profile, page by page (Applier._autofill)
                run.draft = True
                self._log(run, f"{_site(run, data)} kept this application from an earlier visit (a saved draft), so I "
                          "went on with it; I'll check the answers it holds against your profile")
                if run.hands_on:  # (gone on with on the person's Resume: the draft is theirs, kept so for a restart)
                    self._draft_off(run)
            run.page = srv.browser.current_tab or run.page
            if _fingerprint(clicked) == looked and await self._new_tab_soon(run, NEW_TAB_WAIT):
                continue  # asml.com's Apply Now opens Workday in a new tab a moment after the click
            if _fingerprint(clicked) == looked:
                then = await self._after_a_stall(run, ps, data, kind, key, looked, clicked)
                if then is _Then.STOP:
                    return
                if then is _Then.AGAIN:
                    continue
            else:
                ps.stalls = ps.stalled_in_a_row = 0
        self._pause(run, "stuck", "This application has more pages than I expected, and I haven't reached its review "
                    "page. Check where it is in the browser window, press its Next if there's more, then press Resume.")

    async def _no_way_on(self, run: Run, ps: _Pass, data: dict[str, Any], text: str, *, blank: bool,
                         drawn: tuple[Any, ...], reloadable: bool,
                         may_reload: bool) -> Literal[_Then.AGAIN, _Then.STOP]:
        """The lap's page has no step button to press (_drive's `action is None`), after the waits for one. What it is
        decides: a step just pressed and still saving is waited on; a page still loading, a blank one, or the posting's
        own page with no way on is waited on, loaded again or said; an agreement is pressed for the person
        (settings.accept_notices) or left to them; a sign-in the site left for its job search opens the job again; a
        closed posting is skipped; and the rest is said for what it is, down to "I couldn't find the button". AGAIN or
        STOP, never ON: the rest of the lap is for a button to press. `blank`, `drawn`, `reloadable` and `may_reload`
        are as _drive worked them out for its waits."""
        srv = self.srv
        greyed = [a for a in data.get("actions") or [] if a.get("is_submit") and a.get("disabled")
                  and not a.get("aside")] or _greyed_step(data)
        # the step button just pressed here, greyed while the site saves the step: EMD's Phenom (run 51's
        # practice check, Oct 13) took longer than the wait for a page's buttons, then went on to its next
        # step at the same address but its query. Waited on as a step still saving, once a step, and the page
        # then read again, whatever it shows (gone on, its Next back on, a bot check drawn meanwhile: the review)
        just = greyed and _step_key(data, greyed[0]["text"], run)
        if just and ps.pressed and ps.pressed[-1] == just and just not in ps.saved_waited \
                and not any(_errorish(e) for e in data.get("errors") or []):
            ps.saved_waited.add(just)
            await self._wait_for_step_change(just, STEP_SAVE_WAIT)
            return _Then.AGAIN
        if reloadable:
            # still loading after the wait (Wells Fargo's and Hitachi's Workday My Information after the account's
            # sign-in, run 57: its Address, Email and Phone stayed loading placeholders, its Save and Continue
            # greyed, and the desk said it couldn't find the way on): reloaded once a job, the reload waited on as
            # a step of its own, and then said for what it is
            if may_reload:
                run.load_reloaded = True
                ps.step_waited.discard(drawn)
                # (a new page: its late buttons waited on, and the steps it goes through again no circle, as
                # after a refresh the site asked for)
                ps.waited.clear()
                ps.since, ps.more = len(ps.pressed), ps.steps
                self._log(run, f"{_site(run, data)}'s page was still loading after {STEP_LOAD_WAIT} seconds, so I "
                          "reloaded it")
                with contextlib.suppress(Exception):
                    await run.page.reload(wait_until="load")
                return _Then.AGAIN
            await self._bring_forward(run)
            # (a reload only if there was one: never after the person's Resume, the review of 0.3.188)
            waited_for = "a wait and a reload" if run.load_reloaded and not run.hands_on else "a wait"
            if data.get("empty_frame"):
                self._pause(run, "stuck", f"{_site(run, data)}'s page didn't finish loading: the "
                            f"application never came up between the site's header and footer, after "
                            f"{waited_for}, so there's no way on yet. Reload it in the browser window (or "
                            "try again later), then press Resume.")
                return _Then.STOP
            self._pause(run, "stuck", f"{_site(run, data)}'s page didn't finish loading: parts of it still "
                        f"showed their loading placeholders after {waited_for}, so there's no way on "
                        "yet. Reload it in the browser window (or try again later), then press Resume.")
            return _Then.STOP
        if greyed:
            said = "; ".join(e for e in data.get("errors") or [] if _errorish(e))[:300] \
                or _unanswered(data)
            self._pause(run, "stuck", f"\u201c{greyed[0]['text']}\u201d is greyed out, so the site still "
                        "wants something" + (f": {said}" if said else ".") +
                        " Fix it in the browser, then press Resume.")
            return _Then.STOP
        # a page with nothing drawn on it after the wait, but the posting's own page opened on this pass with
        # nothing pressed (that one's said below, reloaded once first): Eightfold's apply page after its Apply
        # Now (issue #112, note 40) came up blank white, the app shell with no headings, boxes or buttons, and
        # the form came once the person reloaded it. It didn't load
        empty = not (run.opened_fresh and not ps.pressed) and _nothing_drawn(data, text)
        if blank or empty:  # still, after the wait? (it was read before it)
            now, now_text = await self._look()
            blank = blank and not (_app_fields(now) or now.get("actions") or now.get("headings")) \
                and len(now_text.strip()) < 40
            empty = empty and _nothing_drawn(now, now_text)
        if empty and now.get("busy") and (_page_key(now), "busy", run.logged) not in ps.waited:
            # turned busy on this look (a loading indicator drawn since the first): waited out as a blank page is,
            # then looked at again, and loaded again as any other if it still has nothing on it (the review)
            ps.waited.add((_page_key(now), "busy", run.logged))
            if await self._wait_for_progress(BLANK_PAGE_WAIT, now):
                return _Then.AGAIN
            now, now_text = await self._look()
            if not _nothing_drawn(now, now_text):
                return _Then.AGAIN  # (something drawn since: the page read again from the top)
        if empty:
            site, where = _site(run, now), _bare(now.get("url") or "")
            after = f" after {ps.pressed_on[-1]}" if ps.pressed else ""
            if (ps.pressed and ps.blank_loads.get(where, 0) < BLANK_RELOADS and not run.pressed_before
                    and not run.hands_on and run.page is not None):
                # loaded again where the desk's own press on this pass led to it, up to BLANK_RELOADS times a
                # page, each one waited on as a new page (its log line makes it one): by its address, so nothing
                # the press sent is sent again. Never after a Submit was pressed for the job, nor after the
                # person's Resume (what they did in the tab would go), as for every other reload here; nor a page
                # the queue came back to by itself (the person was at work in the tab)
                ps.blank_loads[where] = ps.blank_loads.get(where, 0) + 1
                self._log(run, f"{site}'s page came up blank{after} (no headings, boxes or buttons), so I loaded "
                          "it again")
                await _load_again(run.page)
                return _Then.AGAIN
            await self._bring_forward(run)
            times = ps.blank_loads.get(where, 0)
            again = f", even after I loaded it again{' twice' if times == 2 else ''}" if times else ""
            self._pause(run, "stuck", f"{site}'s page loaded blank{after}: nothing on it (no headings, "
                        f"boxes or buttons){again}. The site may be slow: reload the page in the browser "
                        "window (or try again later), then press Resume.")
            return _Then.STOP
        if blank:
            self._pause(run, "stuck", "The page stayed blank: the site may be slow or down. Reload it "
                        "in the browser, then press Resume.")
            return _Then.STOP
        agree = _agreement_button(data)
        if agree is not None:
            said = agree["text"].strip()
            gate = f"{_page_key(data)} {said}"
            if gate in ps.agreed:  # pressed for them on this pass, and the page stayed
                self._pause(run, "stuck", f"I pressed \u201c{said}\u201d for you (the desk agrees to notices "
                            "unless accept_notices is off in your profile), but the page stayed as it "
                            "was: it may want a box ticked or a check passed first. Look in the browser "
                            "window, then press Resume.")
                return _Then.STOP
            if _terms_gate(agree, data, await srv.page_text(40000)) and _may_accept_notices():
                try:
                    clicked = await self._press_agreement(agree["id"])
                except KeyError:  # the page changed between looking and clicking: look again
                    return _Then.AGAIN
                except Exception:  # it won't take a click: the person's, as without the setting
                    clicked = {"clicked": False}
                if clicked.get("clicked"):
                    ps.agreed.add(gate)  # (once a pass: a page that stays is the person's after all)
                    whose = f"{run.company}'s" if run.company else "the site's"
                    self._log(run, f"agreed to {whose} \u201c{_short(said)}\u201d for you (settings.accept_notices)")
                    run.went_in.agree(f"agreed to {whose} \u201c{_short(said)}\u201d")
                    return _Then.AGAIN
            self._pause(run, "stuck", f"The way on is \u201c{said}\u201d, which agrees to "
                        "something in your name, so it's yours to press. Read it and press it in the "
                        "browser window if you're happy to, then press Resume.")
            return _Then.STOP
        if _account_step(data):
            # the person's to do (an account site's steps aren't a Create Account form the desk knows,
            # even with settings.manage_accounts), and theirs until they're off the account site: its
            # next steps (name, email code) read as forms, but aren't the application
            await self._bring_forward(run)
            self._pause(run, "sign_in", f"{_site(run, data)} wants an account for this email, and its account "
                        "steps are yours: create it (or sign in with the email you use there) in the browser "
                        "window; the desk carries on by itself after that.", seen=data)
            run.hold_host = run.paused_host
            return _Then.STOP
        if closed := _closed_notice(data, text):  # only where nothing else explains the stop
            said = f"{_site(run, data)} says \u201c{closed}\u201d" + ("" if closed[-1:] in ".!?" else ".")
            if run.closed_undone:  # (the person put it back after the desk skipped it for this: theirs now)
                await self._bring_forward(run)
                self._pause(run, "stuck", f"{said} You put this job back after I skipped it for that, so "
                            "it's yours: look at it in the browser window, then skip it, or press Resume "
                            "if there's a way on after all.")
                return _Then.STOP
            if run.hands_on or run.seen_form or _app_fields(data):
                # in the application already, or after the person's Resume, or with boxes of it on the page: the
                # words may be about something else there, and what's been done is the person's to judge
                await self._bring_forward(run)
                self._pause(run, "stuck", f"{_site(run, data)} says this posting has closed: "
                            f"\u201c{closed}\u201d If so, there's nothing to apply to: skip this job; "
                            "otherwise go on in the browser window, then press Resume.")
                return _Then.STOP
            # nothing to apply to, and nothing in it for the person to do (issue #112, note 39: the owner's call):
            # skipped by itself, said among the jobs done, and the queue goes on. Undo puts it back
            await self.skip(run.job_id, closed=said)
            return _Then.STOP
        if _maintenance_page(data):
            self._pause(run, "stuck", f"{_site(run, data)} is down for maintenance (it sent the job to "
                        f"{_bare(data.get('url') or '')}). Try again in a few hours: press Resume once "
                        "it's back.")
            return _Then.STOP
        here = f"{_page_key(data)} "
        held = next((g[len(here):] for g in ps.agreed if g.startswith(here)), "")
        if held:  # an agreement pressed for them on this pass, its button greyed out since
            badge = _BOT_BADGE.search(" ".join([text, *(a.get("text") or "" for a in data.get("actions") or [])]))
            if badge:  # Charles Schwab's iCIMS (live check, Oct 2026): its hCaptcha held the press
                check = "reCAPTCHA" if "re" in badge.group(0).lower() else "hCaptcha"
                self._pause(run, "stuck", f"I pressed \u201c{held}\u201d for you "
                            "(the desk agrees to notices unless accept_notices is off in your profile), and "
                            "the page stayed with it greyed out. The page "
                            f"is protected by a bot check ({check}), which may want you to show you're "
                            "not a robot: that's yours. Look at it in the browser window, then press Resume.")
                return _Then.STOP
            self._pause(run, "stuck", f"I pressed \u201c{held}\u201d for you (the desk agrees to notices "
                        "unless accept_notices is off in your profile), but the page stayed as it "
                        "was: it may want a box ticked or a check passed first. Look in the browser "
                        "window, then press Resume.")
            return _Then.STOP
        # an agreement's button greyed out on a page a bot check guards, not pressed on this pass (Charles Schwab's
        # iCIMS, run 57, after the person's Resume: read as a page with no way on)
        badge = _BOT_BADGE.search(" ".join([text, *(a.get("text") or "" for a in data.get("actions") or [])]))
        greyed_agree = next((a["text"].strip() for a in data.get("actions") or [] if a.get("disabled")
                             and not a.get("cookie") and _AGREEMENT.search(a["text"].strip())), "")
        if badge and greyed_agree:
            check = "reCAPTCHA" if "re" in badge.group(0).lower() else "hCaptcha"
            self._pause(run, "stuck", f"\u201c{greyed_agree}\u201d is greyed out, and the page is protected "
                        f"by a bot check ({check}), which may want you to show you're not a robot; or the "
                        "page wants a box ticked or its text read to the end first. Those are yours: look "
                        "at it in the browser window, then press Resume.")
            return _Then.STOP
        if run.reset_from and not run.reopened_elsewhere and _no_sign_in_here(run, data) and _job_search(data):
            # mid sign-in, the site's session gone, on its job search with no way on (Edwards' SuccessFactors
            # after the reset email's wait, the probe of Oct 12, without the desk going back): the job opened
            # again, once a job, whose Apply leads to a fresh sign-in
            run.reopened_elsewhere = True
            self._log(run, f"{_site(run, data)} left its sign-in for its job search, so I opened the job again")
            run.reopened += 1
            await srv.open_application(job_id=run.job_id)
            return _Then.AGAIN
        if run.refreshed and not data.get("fields") and _REFRESH_ASKED.search(text):
            # asked to refresh again after the one reload a job (Circle K's Workday, run 63: its Take Assessment
            # step, reached again, said the same), with no way on: the site's own trouble, said for what it is
            await self._bring_forward(run)
            self._pause(run, "stuck", f"{_site(run, data)} said \u201cSomething went wrong\u201d again and "
                        "asked for the page to be refreshed, and I had reloaded once already for this "
                        "application. That's the site's own trouble: reload the page in the browser window "
                        "(now, or later if it still says so), then press Resume.")
            return _Then.STOP
        if (run.opened_fresh and not ps.pressed and not ps.sign_ins and not ps.agreed and not ps.dismissed
                and not run.seen_form and not run.start_reloaded and not run.hands_on and not run.pressed_before
                and not run.refreshed and not run.load_reloaded and not run.accounts_tried
                and not run.account_made and not run.resetting and not run.reset_from
                and not _app_fields(data) and run.page is not None):
            # the posting's own page (opened on this pass by its address, nothing done on it since) with no way
            # on (the State of Arizona's, runs 61 and 63: the block with its APPLY said "Content missing"; in run
            # 62 it drew): reloaded once a job. Never a page a press or a sign-in led to: a reload may send its
            # form again
            run.start_reloaded = True
            self._log(run, f"{_site(run, data)}'s page showed no way on to the application, so I reloaded it")
            with contextlib.suppress(Exception):
                await run.page.reload(wait_until="load")
            return _Then.AGAIN
        full = ""
        if not run.seen_form:
            with contextlib.suppress(Exception):
                full = await srv.page_text(40000)  # (the page as it is now, whole: the block comes after its text)
        if _CONTENT_MISSING.search(full):
            await self._bring_forward(run)
            self._pause(run, "stuck", f"{_site(run, data)}'s page didn't draw its way to apply: part of "
                        "it says \u201cContent missing\u201d" + (", even after I reloaded it" if
                                                              run.start_reloaded else "")
                        + ". That's the site's own trouble: reload the page in the browser window (now, or "
                        "later), then press Resume.")
            return _Then.STOP
        said_error = _SPENT_PAGE.search(" ".join([*(data.get("headings") or []), (full or text)[:3000]]))
        if (run.start_reloaded and run.opened_fresh and not ps.pressed and not run.seen_form and not run.pressed_before
                and not _app_fields(data) and not data.get("actions") and said_error):
            # the site's own error page where the posting should be, still after the one reload (BrassRing's for
            # Edward Jones, run 100: "Error. An error occurred while processing your request."): the site's
            # trouble, said in its words, not as a step button not found
            await self._bring_forward(run)
            self._pause(run, "stuck", f"{_site(run, data)}'s page for this job says \u201c{said_error.group(0)}"
                        "\u201d, even after I reloaded it. That's the site's own trouble: open the posting "
                        "in the browser window later (then press Resume), or skip the job.")
            return _Then.STOP
        if (run.start_reloaded and run.opened_fresh and not ps.pressed and not run.seen_form
                and not run.pressed_before and not _app_fields(data) and not data.get("actions")
                and not data.get("headings")):
            # the posting's own page, reloaded once (above) and nothing pressed since, still with nothing on it:
            # no job, nothing to press (Linde's Cornerstone requisition page, run 75; in runs 62 and 77 it drew).
            # The site's trouble, or a posting taken down: never a later page the reload had nothing to do with
            await self._bring_forward(run)
            self._pause(run, "stuck", f"{_site(run, data)}'s page for this job came up empty, even after I "
                        "reloaded it: nothing of the job on it, and nothing to press. The site may be having "
                        "trouble, or the posting may have been taken down: look at it in the browser window "
                        "(reload it later, or skip the job), then press Resume.")
            return _Then.STOP
        if (ps.refreshed_at == len(ps.pressed) and not run.pressed_before and not data.get("fields") and all(
                h.strip() == str(data.get("title") or "").strip() for h in data.get("headings") or [])):
            # the page the reload for its "Something went wrong" brought back (nothing pressed since, nor a Submit
            # before) with nothing but the site's own header and footer, no step at all (Northern Trust's
            # Workday, run 71): the site's own trouble, said so (the review of 0.3.158)
            run.came_back_empty = True
            await self._bring_forward(run)
            self._pause(run, "stuck", f"{_site(run, data)} said \u201cSomething went wrong\u201d and asked "
                        "for the page to be refreshed; I reloaded it, and it came back without the "
                        "application, only the site's header and footer. That's the site's "
                        "own trouble: reload the page in the browser window (now, or later if it's still "
                        "so), then press Resume.")
            return _Then.STOP
        if (run.came_back_empty and not ps.pressed and not run.pressed_before and not data.get("fields") and all(
                h.strip() == str(data.get("title") or "").strip() for h in data.get("headings") or [])):
            # Resume pressed on that page, nothing pressed since, and it's still only the header and footer (the
            # live check's Resume at Northern Trust and Axcelis, runs 88 and 90): still the site's, never reloaded
            # again here
            await self._bring_forward(run)
            self._pause(run, "stuck", f"{_site(run, data)}'s page still shows only its header and footer, "
                        "without the application, as after its \u201cSomething went wrong\u201d. That's "
                        "the site's own trouble: reload the page in the browser window (later, if it's "
                        "still so), then press Resume.")
            return _Then.STOP
        self._pause(run, "stuck", "I couldn't find the button that moves this application on. "
                    "Press it in the browser window, then press Resume.")
        return _Then.STOP

    async def _after_a_stall(self, run: Run, ps: _Pass, data: dict[str, Any], kind: str, key: tuple[Any, ...],
                             looked: tuple[Any, ...], clicked: dict[str, Any]) -> _Then:
        """A press that left the page as it was (its fingerprint the same as before it: `looked`), counted on its step
        (ps.stalls, ps.stalled_on) and in a row. A posting's Apply that goes nowhere on the way to a new account gives
        way to its own Register (_register_from_posting); and on the second stall on a step (or the eighth in a row),
        or one the page says something is wrong on, what it says is put right where the desk can (the step reloaded,
        boxes typed, answered or filled again, a step still saving waited on, the boxes the errors name gone back to)
        for the step to be pressed again, or the stop is said. AGAIN: the page looked at again (the lap's `continue`);
        STOP: the pass ends (its `return`); ON: a first stall with nothing said wrong, and the lap goes on (to its end)."""
        srv = self.srv
        if await srv.browser.challenge_showing():  # the click brought up a CAPTCHA
            await self._bring_forward(run)
            self._pause(run, "bot_check", _BOT_CHECK_SAYS)
            return _Then.STOP
        # (counted on this step only: a press on the step before that the site took a moment over, then went
        # on from by itself, isn't this one's stall. Baker Tilly's Workday, run 53: the first press of its
        # Voluntary Disclosures, whose error came a moment after, stopped the desk before it read the error)
        step = (_steady_url(data.get("url")), tuple(data.get("headings") or []))
        ps.stalls = ps.stalls + 1 if step == ps.stalled_on else 1
        ps.stalled_on = step
        # (and eight in a row stop it wherever they were, a page's retries among them: a step whose headings
        # change on every look, the review's)
        ps.stalled_in_a_row += 1
        if ps.stalled_in_a_row >= 8:
            ps.stalls = max(ps.stalls, 2)
        if (kind == "page" and ps.sign_ins.get("wants_account") and not run.accounts_tried
                and _may_manage_accounts() and await self._register_from_posting(run, looked)):
            # on its way to a new account (the sign-in page had no way to one: the job was opened
            # again), and the posting's Apply goes nowhere
            ps.sign_ins.pop("wants_account")
            ps.sign_ins["create_account"] = ps.sign_ins.get("create_account", 0) + 1
            ps.stalls = 0
            return _Then.AGAIN
        problems = [e for e in clicked.get("errors") or [] if _errorish(e)]
        if problems or ps.stalls >= 2:
            # say what's wrong: Workday lists it as links ("Error-Email") and marks fields
            problems = _flagged(clicked)
            if not problems:
                now = (await self._look(text=False))[0]
                problems = _flagged(now) or [  # a Next greyed out until a resume is attached, say
                    f"\u201c{a['text'].strip()}\u201d is greyed out, so the site still wants something (a file, "
                    "say, or a box to tick)" for a in _greyed_step(now)[:1]]
            if any(_ROBOT.search(p) for p in problems):  # Randstad's "verify that you are not a robot"
                await self._bring_forward(run)
                self._pause(run, "bot_check", _BOT_CHECK_SAYS)
                return _Then.STOP
            # (the page's words as they are too: the sentence alone may read as no error, the review's)
            if _SAVED_ALREADY.search(" ".join([*problems, *(clicked.get("errors") or [])])) \
                    and looked not in ps.reloaded and run.page is not None:
                # the step saved part of itself on an earlier press, and won't save it twice: reloaded, it shows
                # what it saved, and goes on (once a page; the error again after it is the person's)
                ps.reloaded.add(looked)
                self._log(run, f"{_site(run, clicked)} said what it saved a moment ago is already there, so I "
                          "reloaded the page")
                with contextlib.suppress(Exception):
                    await run.page.reload(wait_until="load")
                ps.stalls, ps.fixed_at = 0, len(ps.pressed)
                return _Then.AGAIN
            if told := next((e for e in clicked.get("errors") or [] if _NO_ACCOUNT.search(e)), ""):
                # an email-first sign-in with no account for the email (an Eightfold site: "We don't
                # recognize this email. Create a new account"): the desk makes it with
                # settings.manage_accounts (once a job), and otherwise it's the person's to make
                now = (await self._look(text=False))[0]
                create = next((a for a in now.get("actions") or [] if not a.get("disabled")
                               and _CREATE_ACCOUNT.match(final_text(a["text"]))), None)
                if create is not None:
                    run.account_page = _bare(now.get("url") or "")
                    if not run.accounts_tried and _may_manage_accounts():
                        if await self._make_email_account(run, now, create):
                            ps.stalls = 0
                            return _Then.AGAIN
                        return _Then.STOP
                    await self._bring_forward(run)
                    self._pause(run, "sign_in", f"{_site(run, now)} has no account for your email yet "
                                f"(it says \u201c{told[:160].rstrip(' .')}\u201d). Create one in the browser "
                                f"window (its \u201c{create['text'].strip()}\u201d), or sign in with the "
                                "email you use there; the desk carries on by itself after that.", seen=now)
                    return _Then.STOP
            # boxes the errors name that show a value all the same: the page lost its own record of them
            # (Nikon's UKG Pro, live run 10: its resume parse wiped Address 1 and Zip a moment after they
            # were typed). Typed again, once a page, and the step pressed again
            seen_now = (await self._look(text=False))[0] if (looked not in ps.retyped or looked not in ps.refilled_pages
                                                   or looked not in ps.redone_pages) else {}
            # (and Workday's error links, "Error-Date", where its words read as no error: Entegris' and Analog
            # Devices' Self Identify, runs 53 and 54, whose signature Date showed 10/13/2026)
            links = [a.get("text") or "" for a in seen_now.get("actions") or []
                     if re.match(r"\s*error\s*[-\u2013:]", a.get("text") or "", re.I)]
            said = norm(" ".join([*(e for e in seen_now.get("errors") or [] if _errorish(e)), *links]))
            # (a text area too: Microchip's Workday "Please specify your preferred work location.", run 84; one
            # short enough to be typed again key by key, as a cover letter isn't in the time it's given: the review)
            lost = [f for f in seen_now.get("fields") or [] if (f.get("kind") == "text" or f.get("kind") == "textarea"
                                                                and len(str(f.get("value"))) <= 300)
                    and not f.get("disabled")
                    and not is_empty_value(f.get("value")) and len(norm(clean_label(f.get("label") or ""))) >= 3
                    and re.search(rf"\b{re.escape(norm(clean_label(f.get('label') or '')))}\b", said)]
            # a date's parts (Workday's Month / Day / Year) are answered again as a date is, in one run: typed
            # again one by one, each left before the next, they'd go into the wrong part
            # (only a date its label names alone: an "Error-From" on My Experience isn't about every job's From)
            blocks: dict[str, set[str]] = {}
            for f in seen_now.get("fields") or []:
                if f.get("role") == "spinbutton":
                    blocks.setdefault(norm(clean_label(f.get("label") or "")), set()).add(str(f.get("section") or ""))
            # (never a text area: the profile never answers one again over what it shows, the review of 0.3.166)
            wrong = [f for f in lost if f.get("kind") != "textarea" and f.get("invalid") or f.get("role") == "spinbutton"
                     and len(blocks.get(norm(clean_label(f.get("label") or "")), ())) <= 1]
            if wrong and looked not in ps.redone_pages:
                # of those, the ones the page marks invalid themselves (not a box whose label an error about
                # another names: the review's) showing something other than the profile's answer: what the
                # page put there itself (EMD's Phenom, live run 41, Oct 12: its resume parse wrote the phone
                # as 4805550199 in a box asking for +1##########, then called it invalid). The profile's
                # answer, once a page; a box already showing it is typed again below
                ps.redone_pages.add(looked)
                ids = {f["id"] for f in wrong}
                redone = (await srv.autofill(job_id=run.job_id, redo=sorted(ids))).get("filled") or []
                _went_in(run, redone, seen_now)
                given = {f.get("id") for f in redone if f.get("id") in ids}
                ps.filled_ids |= {str(f["id"]) for f in redone if f.get("id")}
                ps.filled_values |= {norm(f["value"]) for f in redone if f.get("value") not in (None, "")}
                # and a date the person gave for this application, where the profile has none (Robert Half's
                # Workday "Anticipated eligibility time for employment?", run 72: 10/14/2026 shown, and
                # "required and must have a value" all the same): typed again as a date is, from its first part
                theirs = _lost_answers(run, seen_now, [f for f in wrong if f["id"] not in given
                                                       and f.get("role") == "spinbutton"])
                theirs_again: set[str] = set()
                if theirs:
                    out = await srv.fill_form([{"id": fid, "value": value, "again": True}
                                               for fid, value in theirs.items()])
                    results = out.get("results") or []
                    # (said only where every part went in: a date's later parts read "typed" with its first
                    # one, even where that one's run didn't read back as typed, the review's)
                    if results and all(r.get("ok") for r in results):
                        theirs_again = {str(r.get("id")) for r in results}
                if given or theirs_again:
                    if given:
                        names = ", ".join(clean_label(f.get("label") or "") for f in wrong if f["id"] in given)
                        self._log(run, f"filled from your profile what the page said was wrong: {names}")
                    if theirs_again:
                        names = ", ".join(dict.fromkeys(clean_label(f.get("label") or "") for f in wrong
                                                        if str(f["id"]) in theirs_again))
                        self._log(run, f"typed your answer again where the page said it was missing: {names}")
                    ps.stalls, ps.fixed_at = 0, len(ps.pressed)
                    return _Then.AGAIN
            lost = [f for f in lost if f.get("role") != "spinbutton"]
            if lost and looked not in ps.retyped:
                ps.retyped.add(looked)
                again = await srv.browser.retype([f["id"] for f in lost])
                if again:
                    names = ", ".join(clean_label(f.get("label") or "") for f in lost if f["id"] in again)
                    self._log(run, f"typed again what the page said was empty though it showed it: {names}")
                    ps.stalls, ps.fixed_at = 0, len(ps.pressed)
                    return _Then.AGAIN
            # boxes the errors name that the page has emptied since they were filled (Nikon's resume parse
            # cleared Address 1, Zip, Company and Skills, live run 12): filled from the profile again, once
            emptied = [f for f in seen_now.get("fields") or [] if f.get("kind") == "text" and not f.get("disabled")
                       and is_empty_value(f.get("value")) and len(norm(clean_label(f.get("label") or ""))) >= 3
                       and re.search(rf"\b{re.escape(norm(clean_label(f.get('label') or '')))}\b", said)]
            if emptied and looked not in ps.refilled_pages:
                ps.refilled_pages.add(looked)
                refill = (await srv.autofill(job_id=run.job_id)).get("filled") or []
                _went_in(run, refill, seen_now)
                again_filled = [f for f in refill if f.get("id") in {e["id"] for e in emptied}]
                ps.filled_ids |= {str(f["id"]) for f in again_filled if f.get("id")}
                ps.filled_values |= {norm(f["value"]) for f in again_filled if f.get("value") not in (None, "")}
                if again_filled:
                    self._log(run, f"filled again {len(again_filled)} field(s) the page had emptied")
                    ps.stalls, ps.fixed_at = 0, len(ps.pressed)
                    return _Then.AGAIN
            if not problems and key not in ps.saved_waited:
                # nothing said wrong and nothing showing while it works: KLA's Workday saving its
                # Application Questions (live run 16, Oct 11: the next step was there a little after the
                # desk stopped). Waited on once, as for a step pressed three times
                ps.saved_waited.add(key)
                if await self._wait_for_step_change(key, STEP_SAVE_WAIT):
                    ps.stalls = 0
                    return _Then.AGAIN
                if await srv.browser.challenge_showing():  # a bot check drawn while it was waited on
                    await self._bring_forward(run)
                    self._pause(run, "bot_check", _BOT_CHECK_SAYS)
                    return _Then.STOP
            if not problems and (kept := await self._resume_not_kept(run, data)):
                if kept == "again":
                    ps.stalls, ps.fixed_at = 0, len(ps.pressed)
                    return _Then.AGAIN
                return _Then.STOP  # (it never kept the resume: typing the boxes again can't fix that)
            if not problems and looked not in ps.retyped:
                # nothing said wrong, and the page didn't move: it may have lost its own record of boxes that
                # still show their values (EMD's Phenom, live check Oct 11: it drops some of the boxes filled
                # quickly one after another, and says nothing). The required boxes typed again, key by key,
                # once a page, and the step pressed again
                ps.retyped.add(looked)
                shown = (await self._look(text=False))[0]  # (as it is after the wait above)
                boxes = [f["id"] for f in shown.get("fields") or [] if f.get("kind") == "text"
                         and f.get("required") and not f.get("disabled") and not is_empty_value(f.get("value"))
                         and len(str(f.get("value"))) <= 80]  # (not a long answer: typed key by key)
                if boxes and await srv.browser.retype(boxes):
                    self._log(run, f"typed {len(boxes)} required box(es) again: the page may have lost what "
                              "was typed in them")
                    ps.stalls = 0
                    return _Then.AGAIN
            if test := _assessment_button(data):  # (a step waiting on the applicant's assessment)
                await self._assessment_pause(run, data, test)
                return _Then.STOP
            form = _screening_button(data)
            if form or _SCREENING_SAID.search(" ".join(problems)):
                # (a step waiting on a questionnaire about the applicant; what else the page says, said too:
                # the review's)
                await self._bring_forward(run)
                also = "; ".join(p for p in problems if not _SCREENING_SAID.search(p))[:240].rstrip(" .")
                self._pause(run, "stuck", f"{_site(run, data)} wants its tax credit screening filled in "
                            "before the application goes on" + (f" (its \u201c{form}\u201d)" if form else "")
                            + ": questions about you that only you can answer. Fill it in in the browser "
                            "window" + (f" (the page also says: {also})" if also else "")
                            + ", then press Resume.")
                return _Then.STOP
            errors = "; ".join(problems)[:300].rstrip(" .")
            stuck = "The page didn't move on" + (f": {errors}." if errors else ".") \
                + " Fix it in the browser, then press Resume."
            if kind == "form" and (page := _step_page(data)) not in ps.asked_back \
                    and _named_by_errors((await self._look(text=False))[0]):
                # boxes taken as optional, left empty, that the errors name or the page marks wrong (BrassRing's
                # consents, Edward Jones' run 83: each a lone "I agree" marked required only by a class and a
                # star beside it, "Required" once pressed): the form's questions after all. Gone back to, once
                # a step, where they're asked (or agreed to, a notice's, with settings.accept_notices); with
                # nothing there to fill, tick or ask after all, the stop is said as it was (the review's)
                ps.asked_back.add(page)
                ps.back_from = (page, stuck)
                self._log(run, f"{_site(run, data)} wants boxes it hadn't marked as required, so I went back to them")
                ps.stalls, ps.fixed_at = 0, len(ps.pressed)
                return _Then.AGAIN
            self._pause(run, "stuck", stuck)
            return _Then.STOP
        return _Then.ON  # (one stall, nothing said wrong: the lap ends, and the page is looked at again)

    async def _resume_not_kept(self, run: Run, data: dict[str, Any]) -> str:
        """A step that won't go on with nothing said wrong, whose page shows the resume as uploaded while the hidden
        fields it keeps the upload in are empty (EMD's Phenom, issue #112, note 28: Next did nothing and said nothing,
        and the desk pressed it three times and typed the required boxes again). Once a job, the file's own Delete is
        pressed and the resume goes up again (EMD's, runs 53 to 64: its server had the file; only the page hadn't
        kept it): "again" when the page then keeps it (its fields have a value). Otherwise the person's to put right,
        said as it stands: "paused". "" when it's not that."""
        srv = self.srv
        name = await srv.browser.upload_not_kept()
        if not name:
            return ""
        said = (f"{_site(run, data)} shows {name} as uploaded, but the resume didn't upload: its own upload fields "
                "are empty, so its Next does nothing. In the browser window, delete the file with its Delete link, "
                "upload your resume again, then press Resume.")
        if not run.reuploaded:
            run.reuploaded = True
            if (deleted := await srv.browser.delete_unkept_upload(name)) == "gone":  # (the row went; its name stays)
                said = (f"{_site(run, data)} showed {name} as uploaded, but hadn't kept it: I pressed its Delete to "
                        "upload it again, but the page still shows the file's name. In the browser window, delete the "
                        "file if it's still there, upload your resume again, then press Resume.")
            elif deleted == "deleted":
                # (the resume's box, empty again: its file goes up again)
                _went_in(run, (await srv.autofill(job_id=run.job_id)).get("filled") or [], data)
                state = ""
                for _ in range(20):  # (the page takes a moment to keep it)
                    await asyncio.sleep(0.5)
                    if (state := await srv.browser.upload_state(name)) == "kept":
                        self._log(run, f"{_site(run, data)} showed {name} as uploaded but hadn't kept it, so I "
                                  "deleted it and uploaded it again")
                        return "again"
                said = (f"{_site(run, data)} showed {name} as uploaded, but hadn't kept it: I deleted it and uploaded "
                        "it again, and it still didn't upload (its own upload fields are empty, so its Next does "
                        "nothing). In the browser window, delete the file with its Delete link, upload your resume "
                        "again, then press Resume." if state == "not kept" else
                        f"{_site(run, data)} showed {name} as uploaded, but hadn't kept it: I deleted it to upload it "
                        "again, and it didn't go up. In the browser window, upload your resume, then press Resume.")
        await self._bring_forward(run)
        self._pause(run, "stuck", said)
        return "paused"

    async def _replace_kept(self, run: Run, data: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        """A resume (or cover letter) box that shows a file kept from before, where this job has one tailored for it
        (autofill.kept_documents: Workday keeps an account's last resume in My Experience's "Resume/CV" upload, and the
        desk left it there, so the employer got the old resume: NXP's, the weekly probe of Oct 2026). The kept file is
        deleted with its own Delete in Workday's list (delete_kept_upload, in browser_uploads.py), and the fill that
        follows puts the tailored one in the box it emptied (_replaced_in says so). Only in Workday's list: another
        site's list of stored files may be the person's own file library, where a Delete takes the file off their
        account, so a file kept there stays as it is, as before. Once a job for each document (Run.kept_replaced), so a
        page that draws the old name again is never deleted from again and again. Never once the person has had the
        application in their hands (their Resume, after a stop or a restart of the desk, or a Submit pressed before; or
        the desk went on with it after they may have worked in its tab: Run.in_their_hands): a file there may be their
        own choice, and it stays, said in the log. A box whose file won't read (a slow page: browser_uploads.py's
        kept_upload_state's "unread") is the person's too, on a card: taken for a box gone, the kept file would go to
        the employer unreplaced. A file that won't delete, or a box where which file is the old one isn't sure, keeps
        what it shows, so the fill never uploads beside it (the employer would get two): the person's, said on a card
        once the page is filled, one box at a time (one after it is neither pressed nor settled, so the next pass says
        it). Returns the boxes emptied for their tailored file, and that card's words ("" for none)."""
        job = self.srv.tracker().get(run.job_id, with_description=False) or {}
        emptied: list[dict[str, Any]] = []
        for box in kept_documents(data.get("fields") or [], config.Profile.load(), job):
            if box["kind"] in run.kept_replaced:
                continue  # (settled for the job: replaced or tried, left as the person's, or the desk's own put in)
            # (the desk's own upload, whatever its name; not in Workday's list; or the box gone: as it is)
            state = await self.srv.browser.kept_upload_state(box["id"])
            if state and state != "unread":
                continue
            what, tailored, old = _DOCUMENT_SAID[box["kind"]], Path(box["tailored"]).name, ", ".join(box["shown"])
            upload = f"“{_short(box['where'])}” upload"
            if state == "unread" and not (run.hands_on or run.pressed_before or run.in_their_hands):
                return emptied, (f"The {upload} on {_site(run, data)} has a {what} kept from before"
                                 + (f" ({old})" if old else "") + f", and the page was too slow for me to read it, to put "
                                 f"the one tailored for this job ({tailored}) in its place. In the browser window, delete "
                                 f"the old file with its Delete, upload {tailored} (in this job's folder), then press "
                                 "Resume.")
            run.kept_replaced.add(box["kind"])
            if run.hands_on or run.pressed_before or run.in_their_hands:
                # (a restart's job goes on only by the person's Resume: hands_on. Never said of the tailored file's name)
                if not any(s.lower() == tailored.lower() for s in box["shown"]):
                    self._log(run, f"left the {what} in its {upload} as it is" + (f" ({old})" if old else "") + ", not "
                              "the one tailored for this job: you've had this application in your hands, so what's in "
                              "it is yours")
                continue
            # A box that shows only that a file is there ("uploaded": Workday's item with no name) can't say what it
            # holds. It's replaced all the same, once a job: the desk's own upload of the tailored file is its own
            # (KEPT_STATE_JS's "mine", or settled when it went in: _replaced_in), so what's there is another; at worst
            # it's the tailored one sent again. So is a file of the tailored one's name (every tailored resume has the
            # same name: autofill.kept_documents). Never where which file is the old one isn't sure (several, or a box
            # for a resume and a cover letter both): the person's
            deleted = "" if box["unsure"] else await self.srv.browser.delete_kept_upload(
                box["id"], box["shown"][0] if box["shown"] else "")
            if deleted == "deleted":
                emptied.append(box)
                continue
            upload = f"The {upload} on {_site(run, data)}"
            put = f"upload {tailored} (in this job's folder), then press Resume."
            if box["unsure"]:
                held = (f"files kept from before ({old})" if len(box["shown"]) > 1 else
                        f"a file kept from before ({old})" if old else "a file kept from before")
                return emptied, (f"{upload} still has {held}, and I can't tell "
                                 + ("which is" if len(box["shown"]) > 1 else "whether it's") + f" your old {what}, to "
                                 f"put the one tailored for this job in its place. In the browser window, delete your old "
                                 f"{what} there with its Delete, {put}")
            return emptied, (f"{upload} still has the {what} kept from before" + (f" ({old})" if old else "") + ", and I "
                             f"couldn't delete it to put in the one tailored for this job ({tailored}): uploaded beside "
                             f"it, the employer would get two {what}s. In the browser window, delete the old file with "
                             f"its Delete, {put}")
        return emptied, ""

    async def _replaced_in(self, run: Run, data: dict[str, Any], replacing: tuple[list[dict[str, Any]], str],
                           result: dict[str, Any]) -> bool:
        """After the fill: a document the desk put in itself is settled for the job (a box showing it later is the
        desk's own, whatever name the site gives it: never deleted as one kept from before). Each box emptied of a file
        kept from before (_replace_kept) is said replaced once the fill sent its tailored file itself (never one the
        fill found the page showing already). On a saved draft, that's a change to its answers whether they were being
        re-checked or not: Submit for me leaves it for the person to check (_draft_changes). Where the tailored file
        didn't go, the box is the person's to put it in, said on a card, as is a kept file that wouldn't go (False:
        paused)."""
        emptied, stuck = replacing
        filled = result.get("filled") or []
        for f in filled:
            if (kind := _DOCUMENT_KINDS.get(str(f.get("from") or ""))) is not None:
                run.kept_replaced.add(kind)
        boxes = {str(f.get("id")): f for f in data.get("fields") or [] if isinstance(f, dict)}
        for box in emptied:
            what, tailored, old = _DOCUMENT_SAID[box["kind"]], Path(box["tailored"]).name, ", ".join(box["shown"])
            sent = next((f for f in filled if f.get("uploaded") and f.get("from") == f"documents.{box['kind']}"
                         and Path(str(f.get("value") or "")).name.lower() == tailored.lower()), None)
            if sent is not None:
                self._log(run, f"replaced the {what} kept from before with the one tailored for this job")
                if run.draft:
                    self._draft_changes(run, [(boxes.get(str(box["id"])) or {"id": box["id"], "label": box["where"]},
                                               old or "uploaded", sent.get("value"))], data)
            elif not stuck:
                stuck = (f"I deleted the {what} {_site(run, data)} kept from before" + (f" ({old})" if old else "")
                         + f" to put in the one tailored for this job, and it didn't go in. In the browser window, "
                         f"upload {tailored} (in this job's folder) into its “{_short(box['where'])}” "
                         "upload, then press Resume.")
        if not stuck:
            return True
        _went_in(run, filled, data)  # (what this fill put in, on the job's "What went in" panel)
        await self._bring_forward(run)
        self._pause(run, "stuck", stuck)
        return False

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
            if _account_and_application((await self._look(text=False))[0]):
                return

    async def _said_terms(self, boxes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """A Create Account form's terms boxes, less a bare one ("I agree", "I consent") or one agreeing to "the
        documents above" (Air Liquide's) whose words just above it say what it agrees to and that's more than the
        site's terms: job alerts, a talent network, texts, marketing (the safety audit of Oct 12). Those are the
        person's (_terms_above)."""
        kept = []
        for box in boxes:
            label = box.get("label") or ""
            if _BARE_AGREE.match(label) or _READ_ABOVE.search(label) or _READ_UNDERSTOOD.match(label):
                # (gone, or not read: not ticked; agreed to, or read and understood, anything else: the person's)
                if not _terms_above(await self.srv.browser.words_before(box["id"]) or ""):
                    continue
            kept.append(box)
        return kept

    async def _assessment_pause(self, run: Run, data: dict[str, Any], button: str) -> None:
        """A step that won't go on until the applicant takes the employer's assessment: theirs to take."""
        await self._bring_forward(run)
        self._pause(run, "stuck", f"{_site(run, data)} wants you to take its assessment before the application goes "
                    f"on (its \u201c{button}\u201d), and that's yours to do. Take it in the browser window (the site "
                    "brings you back after), then press Resume.")

    async def _wait_for_step(self, seconds: float, before: dict[str, Any]) -> bool:
        """Wait for a form still being drawn: its step button enabled, or more boxes to fill, or
        (a page showing a loading indicator) the indicator gone, and then, with nothing to press yet, a
        moment more for the button that may follow a render later (Cornerstone's Apply Now gets its words
        after its loading overlay goes)."""
        had = _boxes(before)
        deadline = time.monotonic() + seconds
        gone_at: float | None = None
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            data = await self.srv.inspect_form(include_dropdown_options=False)
            if pick_next(data.get("actions") or [], in_form=True) or _boxes(data) > had or _came_on(before, data):
                return True
            if before.get("busy") and not data.get("busy"):
                gone_at = gone_at or time.monotonic()
                if time.monotonic() - gone_at >= INDICATOR_GONE_SETTLE:
                    return True
        return gone_at is not None

    async def _wait_for_step_change(self, key: tuple[Any, ...], seconds: float) -> bool:
        """Wait for the page to go on from a step (its address or headings no longer `key`'s)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            data, _ = await self._look(text=False)
            if (_steady_url(data.get("url")), tuple(data.get("headings") or [])) != key[:2]:
                return True
            # (or a question drawn in place of the one pressed on: a questionnaire asking one at a time, the review of
            # 0.3.189)
            if len(key) > 4 and questions_shown(data.get("fields") or []) != key[4]:
                return True
        return False

    async def _wait_for_change(self, before: dict[str, Any], seconds: float) -> bool:
        """Wait for the page to be other than `before` (its address, headings or boxes). True once it is."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            now, _ = await self._look(text=False)
            if _page_sig(now) != _page_sig(before):
                return True
        return False

    async def _wait_for_progress(self, seconds: float, before: dict[str, Any] | None = None) -> bool:
        """Wait for a form, a sign-in, a bot check or a button that moves things on to appear
        (KLA's Workday draws its "Sign in with email" button a few seconds late), or one greyed out on
        `before` to come on (Wells Fargo's review page drew its Submit greyed, run 49)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            data, text = await self._look()
            if (classify(data, text) != "page" or pick_next(data.get("actions") or [], in_form=False)
                    or before is not None and _came_on(before, data)):
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
        # (the page as it was, and each box an answer was sent to, with its key: for the "What went in" panel)
        first = data
        sent: dict[str, str] = {}
        for attempt in range(2):
            fields = {f["id"]: f for f in data.get("fields") or []}
            # a box's own answer (the desk's "section | label | sub-label", for a label more than
            # one box has), else the answer to its label
            by_id = {fid: placed if (placed := placed_key(f)) in run.once else question_key(f.get("label") or "")
                     for fid, f in fields.items()}
            # an answer stays with the page it first went in on: "If yes, please explain" about
            # relatives isn't the answer to a later page's "If yes, please explain" (the same step with its errors
            # shown is the same page: Robert Half's Workday, run 59, came back with "Errors Found" over it and an
            # answer lost, which was never put back)
            here = _answers_page(data)
            if attempt == 0:  # (an answer that is the site's first choice a list shows: the person's now, never asked
                # again for being the site's: the review of 0.3.138)
                same = [fid for fid, key in by_id.items() if key in run.once and run.once_page.get(key, here) == here
                        and site_default(fields[fid]) and norm(run.once[key]) == norm(fields[fid].get("value"))]
                if same:
                    await self.srv.browser.mark_judged(same, [])
            fills = [{"id": fid, "value": run.once[key]} for fid, key in by_id.items()
                     if key in run.once and run.once_page.get(key, here) == here
                     # (or still on its own "Not Specified", which isn't an answer: Kforce's, the probe of Oct 12; or on
                     # the site's first choice where the answer is another: the review of 0.3.138)
                     and (is_empty_value(fields[fid].get("value")) or unset_choice(fields[fid])
                          or site_default(fields[fid]) and norm(run.once[key]) != norm(fields[fid].get("value")))]
            if not fills:
                break
            sent.update({f["id"]: by_id[f["id"]] for f in fills})
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
                data, _ = await self._look(text=False)
        filled -= set(failed)
        _went_in(run, [{"id": fid, "value": run.once.get(key)} for fid, key in sent.items() if key in filled], first, data)
        if filled:
            self._log(run, f"filled {len(filled)} answer(s) you gave for this application")
        if failed:
            self._log(run, f"{len(failed)} of your answers didn't go in")
        return failed

    async def _autofill(self, run: Run, data: dict[str, Any], own: set[str] | None = None,
                        replacing: set[str] | None = None) -> dict[str, Any]:
        """The profile's answers into the page's empty boxes (srv.autofill). On a saved draft's pages (Run.draft: gone in
        through the site's "Continue Application", issue #112, note 38), once a page and only until the job first pauses
        after going in (from then on the person may be at work in its tab: a Resume, a tab the queue carried on with by
        itself, a card answered, and what they changed there is theirs), also into boxes that show another answer than
        the profile's: the draft's, given on an earlier visit, may be out of date. Never a box the profile doesn't
        answer, a self-identification or how-heard answer, this application's own answers (run.once), nor any other
        plan_autofill's refresh leaves alone. Said in the log either way. A box that won't take the profile's answer is
        read again: what it holds is said, and one left empty is asked as any box a fill didn't go into."""
        page = _answers_page(data)
        checking = run.draft and not run.draft_off and not run.hands_on
        before = {str(f.get("id")): f for f in data.get("fields") or [] if isinstance(f, dict)}

        def replaced(filled: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Any, Any]]:
            # what else went into a box the draft had filled, and changed it (the third review: a preset country set
            # right, a list on its first choice answered again): the draft's answers changed all the same. Never a box
            # this pass filled itself (filled_ids: its "before" is the desk's own fill), nor one whose file kept from
            # before the desk replaced (`replacing`: said by _replaced_in, whether the draft is re-checked or not)
            out = []
            for f in filled:
                box = before.get(str(f.get("id"))) or {}
                old = box.get("value")
                if f.get("refreshed") or str(f.get("id")) in (own or set()) | (replacing or set()) \
                        or is_empty_value(old) or unset_choice(box) or norm(old) == norm(f.get("value")):
                    continue
                out.append((box, old, f.get("value")))
            return out
        if not checking or page in run.draft_pages:
            result = await self.srv.autofill(job_id=run.job_id)
            if checking and (others := replaced(result.get("filled") or [])):
                self._draft_changes(run, others, data)
            return result
        run.draft_pages.add(page)
        keep = [str(f["id"]) for f in data.get("fields") or [] if f.get("id") is not None
                and (placed_key(f) in run.once or question_key(f.get("label") or "") in run.once)]
        result = await self.srv.autofill(job_id=run.job_id, refresh=True, keep=keep)
        changed = [f for f in result.get("filled") or [] if f.get("refreshed")]
        refused = [f for f in result.get("failed") or [] if f.get("refreshed")]
        # what each box the refresh went into held, and holds now (for the job's "What went in" panel alone: the log, notes
        # and reports never say a draft's answer, which the profile no longer holds and no scrubber knows)
        changes: list[tuple[dict[str, Any], Any, Any]] = [
            (before.get(str(f.get("id"))) or {"id": f.get("id"), "label": f.get("label")},
             (before.get(str(f.get("id"))) or {}).get("value"), f.get("value")) for f in changed]
        changes += replaced(result.get("filled") or [])
        kept: list[dict[str, Any]] = []
        emptied: set[str] = set()
        if refused:
            now = {str(f.get("id")): f for f in (await self._look(text=False))[0].get("fields") or []}
            emptied = {str(f.get("id")) for f in refused if is_empty_value(now.get(str(f.get("id")), {}).get("value"))}
            # (one left empty is a fill that didn't go in, as any other: asked; one holding something, said by its label)
            result["failed"] = [f for f in result.get("failed") or [] if not f.get("refreshed")
                                or str(f.get("id")) in emptied]
            kept = [f for f in refused if str(f.get("id")) not in emptied and str(f.get("id")) in now]
            for f in refused:  # (half typed, or emptied: changed all the same)
                box, held = before.get(str(f.get("id"))) or {}, now.get(str(f.get("id")), {}).get("value")
                if str(held if held is not None else "") != str(box.get("value") if box.get("value") is not None else ""):
                    changes.append((box or {"id": f.get("id"), "label": f.get("label")}, box.get("value"), held))
        if changes:
            self._draft_changes(run, changes, data)

        def named(fields: list[dict[str, Any]]) -> str:
            return ", ".join(dict.fromkeys(f"“{_short(clean_label(f.get('label') or '') or 'a field')}”"
                                           for f in fields))
        where = _where(data)
        if changed:
            self._log(run, f"re-checked the saved draft's answers on {where}: {len(changed)} field(s) showed another "
                      f"answer than your profile's, so I put your profile's in: {named(changed)}")
        elif not refused and (not replacing or any(not is_empty_value(f.get("value")) for fid, f in before.items()
                                                   if fid not in replacing)):
            # (never of a box whose kept file the desk replaces: that's said on its own. With no other box showing an
            # answer, there's nothing to say here)
            self._log(run, f"re-checked the saved draft's answers on {where}: they match your profile")
        if refused:
            self._log(run, f"re-checked the saved draft's answers on {where}: {len(refused)} field(s) wouldn't take "
                      "your profile's answer" + (f"; as they are now: {named(kept)}" if kept else "")
                      + (f"; {len(emptied)} left empty, so they're asked" if emptied else ""))
        return result

    def _draft_changes(self, run: Run, changes: list[tuple[dict[str, Any], Any, Any]], page: dict[str, Any]) -> None:
        """A saved draft's answers the desk changed: never sent by Submit for me (the person checks them first: _finish),
        each on the job's "What went in" panel, and both kept in the job's own folder for after a restart of the desk
        (_save_draft_changes)."""
        run.draft_changed = True
        _went_changed(run, changes, page)
        _save_draft_changes(self.srv, run)

    def _forget_draft_changes(self, run: Run) -> None:
        """The job went in (the person's Submit, or marked applied): its draft's changes are no longer kept."""
        _forget_draft_changes(self.srv, run.job_id)

    def _draft_off(self, run: Run) -> None:
        """A draft's answers are re-checked no more for this job (Run.draft_off), said once in its log, and kept in its
        folder: after a restart of the desk, a draft the person had taken over isn't re-checked either."""
        if run.draft and not run.draft_off:
            run.draft_off = True
            self._log(run, "draft not re-checked from here on: what you change in the browser stays as you leave it")
            _keep_draft_off(self.srv, run.job_id)

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

    async def _drop_unfit_school(self, run: Run, data: dict[str, Any], pending: list[dict[str, Any]]) -> bool:
        """A school attended without finishing a degree, whose block's Degree list has nothing for that (issue #112,
        note 26, the owner's call): the desk deletes that block with its own Delete and carries on (the resume
        still shows the classes; a degree not earned is never picked). Only the page's last school block, and
        never its only one (the site may need one): a block before others would hand its number to the next
        school's. Once a block; any other way, the person's (_entry_gaps says so). True when it deleted one."""
        prof = config.Profile.load()
        # (only where the profile says no degree was finished there: not a degree it lacks, nor one under way)
        unfit = {str(q.get("section") or "") for q in pending
                 if no_choice_for_no_degree(q, prof) and classes_only(q, prof)} - {""}
        if not unfit:
            return False
        school_block = re.compile(r"education\b.*\d\s*$", re.I)
        deletes = [a for a in data.get("actions") or [] if not a.get("disabled")
                   and re.match(r"^(?:delete|remove)\b", final_text(a.get("text") or ""), re.I)]
        # the blocks by their boxes, and by their Deletes (a block's boxes can be drawn a moment after it)
        blocks = sorted({str(x.get("section") or "") for x in [*(data.get("fields") or []), *deletes]
                         if school_block.match(str(x.get("section") or ""))}, key=lambda s: int(re.findall(r"\d+", s)[-1]))
        last = blocks[-1] if blocks else ""
        if len(blocks) < 2 or last not in unfit:
            return False
        delete = _own_delete(deletes, last)
        if delete is None or last in run.schools_tried:
            return False
        run.schools_tried.append(last)  # (pressed once a block, whatever comes of it)
        try:
            await self.srv.click(delete["id"])
        except Exception:  # gone, or it won't take a click: the person's, as before
            return False
        await asyncio.sleep(0.5)  # (the block goes from the page)
        now, _ = await self._look(text=False)
        if any(f.get("section") == last for f in now.get("fields") or []):
            return False  # still there (a "Are you sure?" over it, say): the person's
        run.schools_dropped.append(last)  # gone: one block fewer of the profile's schools from here on
        _went_forget(run, data, last)  # (and what went into it, off the "What went in" panel)
        found = next((entry_of(q, prof) for q in pending if q.get("section") == last and entry_of(q, prof)), None)
        school = found[0] if found else last
        self._log(run, f"deleted {last} ({school}): its Degree list has no choice for classes without a degree, and "
                  "the desk never picks a degree you didn't earn (your resume still shows the classes)")
        return True

    async def _drop_added_extra(self, run: Run, data: dict[str, Any], pending: list[dict[str, Any]],
                                added: dict[str, int], tried: set[str], filled: set[str] | None = None,
                                given: set[str] | None = None) -> bool:
        """A block past the profile's entries, with nothing in it, where the desk added a block of that kind on this
        pass: Workday's My Experience drew the account's saved school (or job) after the desk had looked for it and
        added one (Hitachi High-Tech America's, run 92; U.S. Bank's and Baker Tilly's, runs 52 and 62), and the page
        then had more blocks than the profile has entries. The empty one goes with its own Delete, as many as the desk
        added (`added`, by kind: taken down as they go), only the last block of its kind, never its only one (a block
        before others would hand its number on), and only the very block an Add the desk pressed drew
        (browser_blocks.py's added_blocks); once a block (`tried`). A block with anything in it, or one the desk didn't
        add, stays the person's (_entry_gaps says so). Where the site drew the saved entry INTO the desk's block
        (Workday numbers its blocks by place: Arizona State University's, run 108, the desk's mark left on a block now
        holding the saved job, and a new empty block after it, unmarked), the marked block holds a value the desk never
        put there (a text box or list neither among `filled`, the boxes its own fills went into on this pass, by id, nor
        showing one of `given`, what they put there: a box drawn again under a new id, or a once-answer, is still the
        desk's), and the empty last block is the desk's own. True when it deleted one."""
        prof = config.Profile.load()
        extra: dict[str, str] = {}  # block: its kind
        for q in pending:
            found = entry_of(q, prof)
            if found is not None and found[1] == "extra" and q.get("section"):
                extra[str(q["section"])] = "education" if "no school" in found[0] else "work"
        deletes = [a for a in data.get("actions") or [] if not a.get("disabled")
                   and re.match(r"^(?:delete|remove)\b", final_text(a.get("text") or ""), re.I)]
        for block, kind in extra.items():
            if added.get(kind, 0) < 1 or block in tried:
                continue
            numbered = re.compile(rf"^(?:{_BLOCK_KINDS[kind]})\s+(\d+)\s*$", re.I)
            # the blocks of its kind by their boxes, and by their Deletes (a block's boxes can be drawn after it)
            blocks = sorted({str(x.get("section") or "") for x in [*(data.get("fields") or []), *deletes]
                             if numbered.match(str(x.get("section") or ""))}, key=lambda s: int(re.findall(r"\d+", s)[-1]))
            if len(blocks) < 2 or blocks[-1] != block:
                continue
            boxes = [f for f in data.get("fields") or [] if f.get("section") == block]
            if not boxes or any(not is_empty_value(f.get("value")) and not unset_choice(f) for f in boxes):
                continue  # (something in it: someone's, never deleted)
            delete = _own_delete(deletes, block)
            if delete is None:
                continue
            # the very block the desk's own press drew (browser_blocks.py's add_entries marks it), never a saved one
            # drawn beside it, empty for a moment (the review of 0.3.173): a block it can't tell so is the person's.
            # Unless the mark sits on a block of this kind the site filled (values in it the desk never typed: the saved
            # entry drawn into the desk's own, the empty one after it then the desk's)
            marked = [h for h in await self.srv.browser.added_blocks() if numbered.match(h)]
            reused = _block_key(block) not in {_block_key(h) for h in marked}
            if reused:
                def site_filled(heading: str) -> bool:
                    # a text box or list with a value the desk didn't put there: not one it filled (by id), nor showing
                    # what it or the person gave (a box drawn again under a new id, a once-answer: the review's), nor a
                    # list left on the site's own default; never a date's part or a tick
                    return any(_block_key(str(f.get("section") or "")) == _block_key(heading)
                               and f.get("kind") in _TEXTLIKE_KINDS and f.get("role") != "spinbutton"
                               and str(f.get("id")) not in (filled or set())
                               and norm(f.get("value")) not in (given or set())
                               and not f.get("judged") and not site_default(f)
                               and not is_empty_value(f.get("value")) and not unset_choice(f)
                               for f in data.get("fields") or [])
                if not any(site_filled(h) for h in marked):
                    continue
            tried.add(block)  # (pressed once a block, whatever comes of it)
            try:
                await self.srv.click(delete["id"])
            except Exception:  # gone, or it won't take a click: the person's, as before
                return False
            await asyncio.sleep(0.5)  # (the block goes from the page)
            now, _ = await self._look(text=False)
            if any(f.get("section") == block for f in now.get("fields") or []):
                return False  # still there (a "Are you sure?" over it, say): the person's
            added[kind] -= 1
            thing = "schools" if kind == "education" else "jobs"
            self._log(run, f"deleted {block}: an empty block past your profile's {thing}, " + (
                f"left over after the page drew the {thing} saved there into the one I had added" if reused
                else f"one I had added while the page was still drawing the {thing} saved there"))
            return True
        return False

    def _note_short_entries(self, run: Run, section: str, added: dict[str, Any], data: dict[str, Any]) -> None:
        """A section whose Add button stayed covered, with fewer blocks on the page than the profile has entries
        (Workday's My Experience, issue #112, note 32: 2 of 5 jobs reached the form, and on another site the job went
        on to its review page with its school missing), is remembered on the run: the job stops for it. Its blocks
        are counted by their own Deletes too: UKG Pro's, filled from the resume, have no numbered heading ("Delete
        Work Experience 1"; run 44 stopped Nikon, UCT and Desert Financial on "0 of your 1 job")."""
        after, wanted = max(added.get("after", 0), _blocks_by_delete(data, section)), added.get("wanted", 0)
        if not added.get("add_button_covered") or after >= wanted:
            run.entries_short.pop(section, None)
            return
        run.entries_short[section] = (after, wanted)
        if run.entries_ok.get(section) == (after, wanted):
            self._log(run, f"went on with {_blocks(section, after, wanted)}: the Add button is still covered, and you "
                      "pressed Resume on it")

    def _short_entries(self, run: Run) -> dict[str, tuple[int, int]]:
        """The sections left short that the person hasn't pressed Resume on, as they stand (one pressed on, with the
        shortfall unchanged, is gone on with: pressing Resume on a page nothing was added to would only stop again)."""
        return {s: n for s, n in run.entries_short.items() if run.entries_ok.get(s) != n}

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
        unlisted: dict[str, str] = {}  # Degree lists with nothing for the profile's degree (a GED's)
        kinds: dict[str, tuple[str, list[str]]] = {}  # Degree lists with several kinds of it (BA, BS) and the profile naming none
        unloaded: dict[str, list[str]] = {}  # a school's lists with their placeholder alone: not loaded
        empty_lists = _unloaded_lists(pending, data, prof)
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
            if any(q is e for e in empty_lists):
                what = clean_label(q.get("label") or "").strip() or "a list"
                if what not in unloaded.setdefault(name, []):
                    unloaded[name].append(what)
                continue
            if no_choice_for_no_degree(q, prof):
                if name not in no_choice:
                    no_choice.append(name)
                continue
            if (degree := degree_not_listed(q, prof)) is not None:
                if listed_kinds := degree_kinds_listed(q, degree):
                    kinds.setdefault(name, (degree, listed_kinds))
                else:
                    unlisted.setdefault(name, degree)
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
                        "or delete that school's block with its Delete button where the site doesn't need it (your "
                        "resume still shows the classes), then press Resume. If you did finish a degree there, add it "
                        "to that school under education_history in profile.yaml (degree: the one you earned), and the "
                        "desk fills it in from then on.")
        if kinds:
            # the list has the level, in kinds the profile doesn't choose between: never one it didn't say
            said.append("; ".join(f"The Degree list for {name} has several kinds of {degree} ({', '.join(options[:4])})"
                                  for name, (degree, options) in kinds.items())
                        + ", and your profile doesn't say which you earned, so that box is yours: choose it in the "
                        "browser, then press Resume. Or write it in full under education_history in profile.yaml "
                        "(degree: Bachelor of Science, say), and the desk picks it from then on.")
        if unloaded:
            one = sum(len(w) for w in unloaded.values()) == 1
            said.append(f"{site}'s {'list' if one else 'lists'} for {listed(unloaded)} showed no choices at all, only "
                        f"{'its' if one else 'their'} placeholder: the page didn't finish loading {'it' if one else 'them'}"
                        + (", even after I reloaded it" if run.list_reloaded else "")
                        + ". Reload the page in the browser window (or try again later), then press Resume.")
        if unlisted:
            # the profile has it; the list doesn't (no high school or GED choice: issue #112, note 16)
            said.append("The Degree list has no choice for " + "; ".join(f"your {degree} ({name})" for name, degree in
                                                                         unlisted.items())
                        + ", so that box is yours: choose the closest honest one in the browser (“Others” or "
                        "the like, where it's there; never a degree you didn't earn), then press Resume.")
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
        the form's own button, after its password boxes (its submit button, or a link or plain button
        in that form that its script sends it with: ApplicantStack's "Submit"). A data privacy statement
        the site makes no account without (SuccessFactors') is opened and accepted first, where
        settings.accept_notices allows (_accept_statement); otherwise the form is left to the person.
        Once a job (Run.accounts_tried). Not with a CAPTCHA on the page, a picture code's box among them
        (Benchmark's Infor registration: "Enter the text in image above"): that's the person's. True
        when it was pressed; the account is said to be made only once the site shows it
        (Run.account_made, in _drive). Nor with security questions on the form (some Taleo sites'):
        their answers are the person's."""
        if _captcha_on(data) or _security_asked(data) and not _security_answered(data):
            return False
        srv = self.srv
        statement = _privacy_statement(data)
        if statement is not None:
            if not await self._accept_statement(run, data, statement):
                return False
            data, _ = await self._look(text=False)  # (as it is once accepted)
        terms = await self._said_terms(_terms_boxes(data))
        boxes = terms + _new_candidate_boxes(data, run.company) + _other_positions_boxes(data)
        own = [a for a in data.get("actions") or [] if a.get("account_form") or a.get("in_account_form")
               or a.get("form_submit") and a.get("after_password")]
        button = self._account_button(own, _MAKE_ACCOUNT)
        if button is None:
            return False
        if boxes and not (await srv.fill_form([{"id": f["id"], "value": _tick(f)} for f in boxes])).get("ok"):
            return False
        try:
            await srv.browser.click(button["id"], allow_submit=True)  # (the form's own button: it creates the account)
        except Exception:
            return False
        run.accounts_tried += 1
        site = _site(run, data)
        ticked = ", ".join(f"\u201c{_short(self._choice_said(f))}\u201d" for f in boxes)
        # (a user name it asks for is the email: _sign_in put it there, and signs in with it later)
        with_ = "your email as its user name and your saved password" if any(
            _user_name_box(f) for f in data.get("fields") or [] if f.get("kind") in ("text", "email")) else "your saved password"
        self._log(run, f"pressed \u201c{button['text'].strip()}\u201d to create your account on {site} with {with_}"
                  + (f", after ticking {ticked}" if boxes else ""))
        run.account_made = (f"created your account on {site} with {with_}" + (" and agreed to its terms"
                            if terms else "") + (" and accepted its data privacy statement" if statement else "")
                            + _manage_hint())
        await self._wait_for_account(data)
        return True

    async def _account_card(self, run: Run, data: dict[str, Any]) -> list[dict[str, Any]]:
        """An account form's security questions put to the person on the desk's card (_account_card_boxes), with their
        choices (a list's, opened to read them), with settings.manage_accounts: answered there, they go into this form
        alone (Run.account_answers), never remembered, and the desk creates the account. Empty when there's nothing it
        can ask there."""
        if not _may_manage_accounts() or not _account_card_boxes(data.get("fields") or [], data.get("headings")):
            return []
        try:
            full = await self.srv.inspect_form(include_dropdown_options=True)
        except Exception:  # (gone, or changed: the card as it was)
            return []
        keep = ("id", "kind", "label", "section", "sublabel", "required", "options", "role")
        return [{**{k: f[k] for k in keep if f.get(k) not in (None, [])}, "account": True}
                for f in _account_card_boxes(full.get("fields") or [], full.get("headings"))]

    async def _fill_account_answers(self, run: Run, data: dict[str, Any]) -> str:
        """The person's answers to an account form's security questions (given on the card: Run.account_answers) put
        into it, only with an answer for every one of its boxes, straight into the browser, each box marked as theirs
        first (Browser._keep_secret: never in a snapshot, a log or a report, said only as answers filled in). Used once,
        whatever comes of it. "filled" once every box reads back as filled by the desk (_security_answered), which lets
        its button be pressed; "failed" when they didn't all go in (what went wrong unsaid: it may name an answer);
        "" with no answers for its boxes."""
        boxes = _account_card_boxes(data.get("fields") or [], data.get("headings"))
        answers, run.account_answers = run.account_answers, {}
        if not boxes or not all(placed_key(f) in answers for f in boxes):
            if answers:  # (the form changed since its card: asked again, said so)
                self._log(run, f"your answers to the security questions didn't match the form on {_site(run, data)} "
                          "any more, so they're asked again")
            return ""
        results = await self.srv.browser.fill([{"id": f["id"], "value": answers[placed_key(f)], "secret": True}
                                               for f in boxes])
        now, _ = await self._look(text=False)
        if not results or not all(r.get("ok") for r in results) or not _security_answered(now):
            self._log(run, f"couldn't put your answers to the security questions into the form on {_site(run, data)}")
            return "failed"
        self._log(run, f"filled in your answers to the security questions on {_site(run, data)}")
        return "filled"

    async def _accept_statement(self, run: Run, data: dict[str, Any], link: dict[str, Any]) -> bool:
        """A Create Account form's data privacy statement (_privacy_statement), which the site makes no
        account without: its link pressed to open it (a dialog over the page, drawn a moment after) and
        its Accept pressed for the person, as for any employer notice the desk agrees to (_answer_notice:
        settings.accept_notices, never in practice mode; said in the log; never Decline). True once it's
        accepted. False when it isn't allowed, wouldn't open (SuccessFactors' won't before a country is
        chosen) or isn't one to agree to for them: the person's, left open for them to read."""
        if not _may_accept_notices():
            return False
        said = link["text"].strip()
        notice = None
        for _ in range(2):  # (live, APS's once opened nothing at the first press: a second press after a moment)
            try:
                await self.srv.browser.click(link["id"])
            except Exception as e:  # gone, or it won't take a click: the person's, as without the setting
                self._log(run, f"the click on “{said}” failed: {_error_said(e)}")
                return False
            deadline = time.monotonic() + ACCOUNT_STEP_WAIT
            while True:
                now, _ = await self._look(text=False)
                notice = _notice(now)
                if notice is not None or time.monotonic() > deadline:
                    break
                await asyncio.sleep(0.5)
            if notice is not None or now.get("dialogs"):
                break  # (a dialog of another sort opened: not pressed again over it)
            link = next((a for a in now.get("actions") or [] if a.get("text", "").strip() == said), link)
        # (each way it can fail is said in the log: APS's, live, Oct 2026, failed with no clue why)
        if notice is None:
            told = "; ".join(e for e in now.get("errors") or [] if not _ERROR_COUNT.match(e.strip()))[:200]
            self._log(run, f"I pressed “{said}” twice, and no statement opened over the page"
                      + (f" (it says “{told}”)" if told else ""))
            return False
        run.statement_accepted = await self._answer_notice(run, now, notice, set(), over_form=False) == "agreed"
        if not run.statement_accepted:
            self._log(run, f"“{_short(_notice_name(notice))}” opened over the page, and I couldn't accept it for you")
        return run.statement_accepted

    async def _finish_account(self, run: Run, data: dict[str, Any], button: dict[str, Any]) -> bool:
        """The rest of a Create Account, on a page of its own after the email and password (UKG Pro's
        "Almost there!", live, Oct 2026: a name and phone, a consent box, and "Create account", greyed
        out until the box is ticked). Its boxes are filled from the profile. With
        settings.manage_accounts (and no CAPTCHA, a picture code's box among them, nor security questions: the
        person's), its terms boxes are ticked (never a newsletter's or
        text messages': _terms_boxes) and its button pressed once it's enabled, once a job
        (Run.account_finished); the account is said made only once the site goes on from it
        (Run.account_made, in _drive). Otherwise, or when the page is still there after that, it's
        the person's. True when pressed."""
        srv = self.srv
        site = _site(run, data)
        said = button["text"].strip()
        if await self._fill_account_details(data.get("fields") or [], data.get("headings")):
            self._log(run, f"filled the rest of {site}'s Create Account form with your details")
        why = ""
        # (a CAPTCHA, and security questions' answers, are the person's, never made up: never pressed for them; theirs
        # given on the card go in, and it's pressed then)
        if (_may_manage_accounts() and not run.account_finished and run.account_answers and _security_asked(data)
                and not _security_answered(data)):
            if await self._fill_account_answers(run, data) == "failed":
                why = "your answers to its security questions wouldn't go into it"
            data, _ = await self._look(text=False)
        captcha, security = _captcha_on(data), _security_asked(data) and not _security_answered(data)
        if _may_manage_accounts() and not run.account_finished and not captcha and not security and not why:
            now, _ = await self._look(text=False)
            terms = await self._said_terms(_terms_boxes(now))
            boxes = terms + _other_positions_boxes(now)
            pressable = None
            if boxes and not (await srv.fill_form([{"id": f["id"], "value": _tick(f)} for f in boxes])).get("ok"):
                why = "its terms box wouldn't tick"
            else:  # greyed out until its terms are ticked: a moment for the page to take that in
                for _ in range(10):
                    now, _ = await self._look(text=False)
                    named = [a for a in now.get("actions") or [] if a.get("text", "").strip() == said]
                    pressable = next((a for a in named if not a.get("disabled")), None)
                    if pressable is not None or not named:
                        break
                    await asyncio.sleep(0.5)
                if pressable is None:
                    why = f"“{said}” stayed greyed out"
                elif _captcha_on(now) or _security_asked(now) and not _security_answered(now):  # (drawn since it was read)
                    pressable = None
                    why = ("it shows a CAPTCHA, which is yours to solve" if _captcha_on(now) else
                           "it asks security questions too, whose answers are yours to choose")
            if pressable is not None:
                try:
                    await srv.browser.click(pressable["id"], allow_submit=True)  # (it creates the account)
                except Exception as e:  # (said in the log: Nikon's UKG Pro, live, Oct 2026, gave no clue why)
                    why = f"“{said}” wouldn't take a click"
                    self._log(run, f"the click on “{said}” failed: {_error_said(e)}")
                else:
                    run.account_finished = True
                    ticked = ", ".join(f"“{_short(self._choice_said(f))}”" for f in boxes)
                    self._log(run, f"pressed “{said}” to finish creating your account on {site}"
                              + (f", after ticking {ticked}" if boxes else ""))
                    run.account_made = _account_made_says(site, terms=bool(terms) or "agreed to its terms" in run.account_made,
                                                          password=bool(run.account_made))
                    await self._wait_to_leave(now, said)
                    return True
        await self._bring_forward(run)
        now, _ = await self._look(text=False)
        run.page_info = _page_info(now)
        if run.account_finished or why:
            wants = _account_wants(data, now) or "see the page in the browser window."
            done = f"I pressed “{said}”" if run.account_finished else f"I couldn't finish it ({why})"
            self._pause(run, "sign_in", f"{site} asked for a few more details to create your account, and {done}; it "
                        f"wants something more: {wants} Finish it there; the desk carries on after that.", seen=now)
        else:
            card = await self._account_card(run, now) if security and not captcha else []
            if card:
                self._pause(run, "sign_in", f"{site} asks for a few more details to create your account. I filled in what "
                            "your profile has; its security questions and their answers are yours to choose (the desk "
                            "never makes up answers): answer them here (they go into this form alone, and are never "
                            "remembered) and the desk finishes the account, or fill them in in the browser window, tick "
                            f"its terms box if you agree and press “{said}” there; the desk carries on after that.",
                            card, seen=now)
                return False
            asks = (" its security questions and their answers are yours to choose (the desk never makes up answers): "
                    "fill them in," if security else "") + (" solve its CAPTCHA (that's yours)," if captcha else "")
            self._pause(run, "sign_in", f"{site} asks for a few more details to create your account. I filled in what "
                        f"your profile has;{asks} tick its terms box if you agree and press “{said}” in the "
                        "browser window (then verify your email if they ask); the desk carries on after that.", seen=now)
        return False

    async def _wait_to_leave(self, form: dict[str, Any], button: str) -> None:
        """After a press that sends a page (the rest of Create Account): until the site has done with
        it (gone on, its button gone, or something said about it), as _wait_for_account."""
        deadline = time.monotonic() + ACCOUNT_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            try:
                now = await self.srv.inspect_form(include_dropdown_options=False)
            except Exception:  # mid-way to the next page
                continue
            if (now.get("url") != form.get("url") or (now.get("errors") or []) != (form.get("errors") or [])
                    or not any(a.get("text", "").strip() == button for a in now.get("actions") or [])):
                return

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

    async def _make_email_account(self, run: Run, data: dict[str, Any], create: dict[str, Any]) -> bool:
        """settings.manage_accounts, on an email-first sign-in that has no account for the email (an
        Eightfold site's, live, Oct 2026: "We don't recognize this email. Create a new account"):
        press its way to a new one ("Create an account"), and take its steps (_email_account_step):
        its agreement, the email again, then the code it emails (the inbox's, or the person's). An
        Eightfold site asks no password for it (Northrop Grumman's hides the field; another offers a
        one-time code in its place), so no saved password goes on it. Once a job (Run.accounts_tried).
        True: on its way (the next look carries on); False: paused for the person."""
        site = _site(run, data)
        if data.get("captcha") or data.get("challenge"):
            return await self._account_theirs(run, data, f"{site} has no account for your email yet, and its sign-in shows "
                                        "a picture check (CAPTCHA), which is yours")
        run.accounts_tried += 1
        try:
            await self.srv.click(create["id"])
        except Exception:  # gone, or it won't take a click: the person's, as without the setting
            return await self._account_theirs(run, data, f"{site} has no account for your email yet, and its "
                                        f"“{create['text'].strip()}” didn't take my click")
        self._log(run, f"{site} has no account for your email, so I pressed “{create['text'].strip()}” to make "
                  "one" + _manage_hint())
        now, _ = await self._after_press(data)
        view = _email_account_view(now)
        if view == "code":
            return True  # its emailed code: the main loop's (the inbox's, or the person's)
        if view is None:
            return await self._account_theirs(run, now, f"I pressed {site}'s “{create['text'].strip()}”, as it has "
                                        "no account for your email yet, and it showed a step I don't know")
        return await self._email_account_step(run, now, view)

    async def _email_account_step(self, run: Run, data: dict[str, Any], view: str) -> bool:
        """One step of making an account the desk began on an email-first sign-in (_make_email_account):
        "consent": its boxes agreeing to the site's terms or privacy policy are ticked (never a
        newsletter's or job alerts': those choices are the person's) and its button pressed; a consent
        to be considered for other open positions too only when the site won't go on without it (marked
        required, or refused until it's given: Northrop's "You must select one option"; the owner's
        call, Oct 10); "email": the profile's email, and Continue; "options": the one-time code in place
        of a password. True: on its way; False: paused for the person (a choice that's theirs, a
        CAPTCHA, or what the site said)."""
        srv = self.srv
        site = _site(run, data)
        if data.get("captcha") or data.get("challenge"):
            return await self._account_theirs(run, data, f"{site}'s steps of making your account show a picture check "
                                        "(CAPTCHA), which is yours")
        ticked: list[dict[str, Any]] = []
        pattern = _ONE_TIME_CODE if view == "options" else _ACCOUNT_ON
        button = self._account_button(data.get("actions") or [], pattern)
        deadline = time.monotonic() + ACCOUNT_STEP_WAIT
        while button is None and time.monotonic() < deadline:
            # its boxes drawn before its button (Northrop's agreement, live, Oct 2026): a moment more
            await asyncio.sleep(0.5)
            now, _ = await self._look(text=False)
            if _email_account_view(now) != view:
                return True  # it went on by itself: the main loop's next look
            data, button = now, self._account_button(now.get("actions") or [], pattern)
        if button is None:
            return await self._account_theirs(run, data, f"I couldn't find the button that goes on with making your account "
                                        f"on {site}")
        if view == "consent":
            ticked = _consent_boxes(data)
            if ticked and not (await srv.fill_form([{"id": f["id"], "value": True} for f in ticked])).get("ok"):
                return await self._account_theirs(run, data, f"{site} asks you to agree to its terms before making your "
                                            "account, and its box didn't take my tick")
            for f in ticked:
                self._log(run, f"ticked “{_short(f.get('label') or '')}” to make your account on {site}")
            if not await self._give_other_positions(run, data, _other_positions_boxes(data)):
                return await self._account_theirs(run, data, f"{site} asks you to agree to be considered for other "
                                            "open positions before making your account, and its choice didn't take my "
                                            "click")
        elif view == "email":
            address = config.Profile.load().get("personal.email")
            box = next(f for f in data.get("fields") or [] if not f.get("disabled") and not f.get("aside"))
            if not address:
                return await self._account_theirs(run, data, f"{site} asks for your email to make your account, and your "
                                            "profile has none")
            if str(box.get("value") or "").strip().lower() != address.strip().lower():
                await srv.fill_form([{"id": box["id"], "value": address}])
        try:
            # (its agreement's "Submit" sends that agreement only: the application isn't on the page yet)
            await srv.browser.click(button["id"], allow_submit=True)
        except Exception:
            return await self._account_theirs(run, data, f"{site}'s “{button['text'].strip()}” didn't take my click "
                                        "while I was making your account there")
        if view == "email":
            self._log(run, f"gave {site} your email for the new account and pressed “{button['text'].strip()}”")
        elif view == "options":
            self._log(run, f"chose “{button['text'].strip()}” on {site}: the account needs no password there")
        now, _ = await self._after_press(data)
        wanted = _other_positions_boxes(now, required=False) if view == "consent" else []
        others = [f for f in now.get("fields") or [] if not f.get("disabled") and not f.get("aside")
                  and is_empty_value(f.get("value")) and f not in wanted]
        if (wanted and not others and _email_account_view(now) == view and _page_sig(now) == _page_sig(data)
                and (now.get("errors") or []) != (data.get("errors") or [])):
            # refused without its consent to be considered for other open positions too: given, and pressed again
            again = self._account_button(now.get("actions") or [], _ACCOUNT_ON)
            if again is not None and await self._give_other_positions(run, now, wanted, refused=True):
                with contextlib.suppress(Exception):
                    await srv.browser.click(again["id"], allow_submit=True)
                    now, _ = await self._after_press(now)
        if _email_account_view(now) not in (view, None) or _page_sig(now) != _page_sig(data):
            return True  # on to its next step (the main loop's next look); a page that stayed (BrassRing's email
            # step, which has no view of its own) is said, never pressed again
        # still there: a choice left that's the person's, or what the site said
        said = "; ".join(e for e in now.get("errors") or [] if not _ERROR_COUNT.match(e.strip()))[:200].rstrip(" .")
        left = [f for f in now.get("fields") or [] if not f.get("disabled") and not f.get("aside")
                and is_empty_value(f.get("value"))]
        if view == "consent" and left:
            named = " and ".join(f"“{self._choice_said(f)}”" for f in left[:2])
            agreed = f" and ticked “{_short(ticked[0].get('label') or '')}”" if ticked else ""
            return await self._account_theirs(run, now, f"{site} has no account for your email yet, so I began making "
                                        f"one there{agreed}. It also asks for {named}"
                                        + (f" (it says “{said}”)" if said else "")
                                        + ", which the desk never chooses for you: choose it and press its "
                                        f"“{button['text'].strip()}” in the browser window if you're happy to "
                                        "(or sign in with the email you use there). The desk then makes the account "
                                        "with your email and the code it emails you, and carries on",
                                        tail=False)
        return await self._account_theirs(run, now, f"I pressed “{button['text'].strip()}” while making your "
                                    f"account on {site}, and it didn't go on" + (f": “{said}”" if said else ""))

    async def _give_other_positions(self, run: Run, data: dict[str, Any], fields: list[dict[str, Any]],
                                    refused: bool = False) -> bool:
        """Give an account form's consents to be considered for other open positions (_OTHER_POSITIONS),
        which the site won't make the account without (`fields`: marked required, or `refused`: it
        said so when its button was pressed), logging each. False when one wouldn't take it."""
        if not fields:
            return True
        if not (await self.srv.fill_form([{"id": f["id"], "value": _tick(f)} for f in fields])).get("ok"):
            return False
        why = "it wouldn't go on without it" if refused else "it requires it"
        for f in fields:
            self._log(run, f"chose “{self._choice_said(f)}” to make your account on {_site(run, data)}, as {why} "
                      "(settings.manage_accounts)")
            run.went_in.agree(f"chose “{self._choice_said(f)}” to make your account on {_site(run, data)}, as {why}")
        return True

    @staticmethod
    def _choice_said(field: dict[str, Any]) -> str:
        """A box or choice left for the person, in their words: a group's name and its choice ("Contact
        Consent: I understand that I may be considered for other open positions…")."""
        label = clean_label(field.get("label") or "")
        options = [o for o in field.get("options") or [] if isinstance(o, str)]
        said = f"{label}: {options[0]}" if len(options) == 1 and label else label or (options[0] if options else "a box")
        return said if len(said) <= 160 else said[:158].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"

    async def _account_theirs(self, run: Run, data: dict[str, Any], why: str, tail: bool = True) -> bool:
        """Pause on a step of making an account that's the person's (False, for the step's caller)."""
        await self._bring_forward(run)
        self._pause(run, "sign_in", why + (". Finish making the account in the browser window (or sign in with the "
                                           "email you use there); the desk carries on by itself after that."
                                           if tail else "."), seen=data)
        return False

    async def _after_press(self, before: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The page once a step of making an account has done something after its button: drawn its
        next step, or said something. Looked at sooner, an Eightfold site's modal still shows the
        step just done while it asks its server."""
        deadline = time.monotonic() + ACCOUNT_STEP_WAIT
        while True:
            data, text = await self._look()
            if (_page_sig(data) != _page_sig(before) or (data.get("errors") or []) != (before.get("errors") or [])
                    or time.monotonic() > deadline):
                return data, text
            await asyncio.sleep(0.5)

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
            self._pause(run, "sign_in", f"Your saved password didn't sign in on {site}, and I {why}. Sign in in the "
                        "browser window and the desk carries on after that; or finish the reset there, to the password "
                        "you saved on the desk, then press Resume: the desk signs in with it.", seen=page)
            return "paused"

        def way_back() -> bool:
            # to the sign-in page, for its way to a new account: not once a Create Account here was told
            # the email has one
            return bool(run.reset_from) and run.page is not None and not tried.get("made")

        async def instead(why: str, page: dict[str, Any]) -> str:
            # No reset the desk can do there: back to the sign-in page, for its way to a new account. With
            # no account for the email (a first application there, the test identity's), that's the way
            # on; a site that has one says so when it's asked to make another, and that's the person's (as
            # it is once a Create Account here was told the email has one).
            if not way_back():
                return await stop(why, page)
            self._log(run, f"your saved password didn't sign in on {site}, and I {why}, so I went back to make an "
                      "account there instead")
            await self._back_to_sign_in(run)
            return "no_account"

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
            data, _ = await self._look(text=False)
            forgot = _forgot_action(data.get("actions") or [])
            if forgot is None:
                return await stop("couldn't find its way to a password reset", data)
        run.reset_from = data.get("url") or ""
        before, _ = await self._look(text=False)  # (the page as it is now, the refused sign-in drawn: what the press changes)
        forgot = _forgot_action(before.get("actions") or []) or forgot  # (its own, drawn again since, if it was)
        if not await press(forgot):
            return await instead("couldn't open its password reset", data)
        # The reset drawn, not the sign-in still on show nor a page between the two, still loading: a script's way
        # there can take a moment, and the sign-in read again was taken for a reset the desk can't fill in, so a reset
        # it could do was never asked for (the review of 0.3.156)
        deadline = time.monotonic() + LATE_BUTTONS_WAIT
        while True:
            data, text = await self._look()
            if (_page_sig(data) != _page_sig(before) and not data.get("busy") and data.get("fields")
                    or time.monotonic() > deadline):
                break
            await asyncio.sleep(0.5)
        address = config.Profile.load().get("personal.email")
        boxes = [f for f in data.get("fields") or [] if f.get("kind") in ("text", "email")
                 and re.search(r"e-?mail|user ?name|login", f.get("label") or "", re.I)]
        if (not _RESET_PAGE.search(" ".join([text[:2000], *(data.get("headings") or [])])) or not boxes or not address
                or any(f.get("kind") == "password" for f in data.get("fields") or [])):
            return await instead("opened its password reset, which isn't one I can fill in", data)
        captcha = _captcha_on(data)
        if captcha and way_back():
            # BrassRing's asks for a picture code (live, Oct 2026), never touched: its Continue pressed
            # without one only said "Captcha text Required field"
            return await instead("opened its password reset, which has a CAPTCHA (yours to solve)", data)
        if not (await srv.fill_form([{"id": boxes[0]["id"], "value": address}])).get("ok"):
            return await instead("opened its password reset, which didn't take your email", data)
        if captcha:  # (and no new account to go back for: the site said the email has one)
            run.resetting = True
            run.reset_at = time.time()  # (the person's press, past its picture code, comes after this)
            await self._bring_forward(run)
            self._pause(run, "bot_check", f"I opened {site}'s password reset for your saved password, which didn't sign "
                        "in, and filled in your email. Solve its picture code and press its button; the desk watches your "
                        "inbox for the reset email and sets your saved password as the new one.", seen=data)
            return "paused"
        button = self._account_button(data.get("actions") or [], _RESET_ASK, full=False)
        run.reset_at = time.time()  # (its email is sent as the button is pressed: none from before is this reset's)
        if button is None or not await press(button, allow_submit=True):  # (asks for the email: sends no application)
            return await instead("filled in your email on its password reset, but couldn't press its button", data)
        data, text = await self._look()
        if data.get("errors") and not _RESET_SENT.search(text):  # "No account found for this email"
            if _NO_ACCOUNT.search(" ".join(data["errors"])) and run.reset_from and run.page is not None:
                # nothing to reset: back to the sign-in page, for its way to a new account
                self._log(run, f"{site} has no account for your email, so I went back to make one there")
                await self._back_to_sign_in(run)
                return "no_account"
            return await stop(f"asked for a password reset, and it says \u201c{data['errors'][0][:160]}\u201d", data)
        run.resetting = True
        self._log(run, f"your saved password didn't sign in on {site}, so I asked it to email a password reset"
                  + _manage_hint())
        await self._await_reset_email(run, data)
        return "paused"

    async def _back_to_sign_in(self, run: Run) -> None:
        """Back to the sign-in page a password reset was asked from (run.reset_from). Where that page
        was good for one visit (SuccessFactors' career?_s.crb=..., Arizona Public Service's for the test
        identity, live Oct 2026: "An error occurred while processing your request"), or its address
        alone doesn't bring it back (Benchmark's Infor sign-in, live Oct 2026: a refused sign-in answers
        at /sso/SSOServlet, where its form is sent, and that address opened again is Infor's own
        sign-in, with no "Register" or "Forgot password?"), or it leads elsewhere with no sign-in (Edwards'
        SuccessFactors to its careers search home, its session gone), the job's posting is opened again in its
        tab instead: its Apply leads to a fresh sign-in page. Where the reset was drawn in its place,
        at the same address and fragment (BrassRing's #jobDetails=…, live, Oct 2026), going there again
        leaves the reset on show: the page is loaded again (BrassRing's shows the posting, whose Apply
        leads to the sign-in page)."""
        assert run.page is not None
        if run.page.url == run.reset_from and urlparse(run.reset_from).fragment:
            await run.page.reload(wait_until="domcontentloaded", timeout=45000)
        else:
            await run.page.goto(run.reset_from, wait_until="domcontentloaded", timeout=45000)
        data, text = await self._look()
        fields, actions = data.get("fields") or [], data.get("actions") or []
        if not fields and _SPENT_PAGE.search(text[:3000]):
            gone = " couldn't be opened again"
        elif (any(f.get("kind") == "password" for f in fields) and _forgot_action(actions) is None
              and _account_way(actions) is None):
            gone = ", opened again, had no way to a new account or a password reset"
        elif not run.reopened_elsewhere and _no_sign_in_here(run, data) and await self._still_no_sign_in(run):
            # its session gone, it led elsewhere: SuccessFactors' to the company's careers search home (Edwards
            # Vacuum's, after three minutes waiting on a reset email, the plus probe Oct 11), with only a Sign In.
            # Still so a moment later (not a sign-in still being drawn), and once a job
            run.reopened_elsewhere = True
            gone = ", opened again, led to the site's job search, not its sign-in"
        else:
            return
        self._log(run, f"{_site(run, data)}'s sign-in page{gone}, so I opened the job again")
        run.reopened += 1
        await self.srv.open_application(job_id=run.job_id)

    async def _still_no_sign_in(self, run: Run) -> bool:
        """Still no sign-in a moment later (not one still being drawn)?"""
        await asyncio.sleep(LEFT_SIGN_IN_SETTLE)
        data, _ = await self._look(text=False)
        return _no_sign_in_here(run, data)

    async def _register_from_posting(self, run: Run, looked: tuple) -> bool:
        """The desk is on its way to a new account at a job system (_back_to_sign_in opened the job again
        for the sign-in page's way to one), and the posting's "Apply" went nowhere (Benchmark's Infor, live,
        Oct 2026: once a sign-in was refused in the tab, Apply no longer opened the sign-in again). The
        posting's own "Register" (or "Create Account") is the way instead, as the sign-in page's would have
        been: pressed, and its form read and filled in like any Create Account. Only where the page is
        still as it was before Apply was pressed (a slow Apply isn't one that went nowhere). True once
        it's pressed."""
        data, _ = await self._look(text=False)
        way = _account_way(data.get("actions") or []) if _fingerprint(data) == looked else None
        if way is None:
            return False
        try:
            clicked = await self.srv.click(way["id"])
        except KeyError:  # the page changed between looking and clicking
            return False
        if clicked.get("clicked") is False:
            return False
        run.page = self.srv.browser.current_tab or run.page
        self._log(run, f"“Apply” on {_site(run, data)}'s posting went nowhere, so I pressed its "
                  f"“{way['text'].strip()}” to make an account there instead")
        return True

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

    async def _await_verify_email(self, run: Run, data: dict[str, Any], text: str = "") -> None:
        """The Sign In a Workday site goes back to after the desk made the account says it emailed a link
        to verify it (Banner Health's, live, Oct 2026: "An email has been sent to you. Please verify your
        account"). The saved password isn't taken until that link is opened (the desk pressed Sign In, was
        refused and stopped on "your saved password didn't sign in"), so it isn't pressed yet, and a
        refusal before this, for the same reason, doesn't count against the job's sign-ins. With the inbox
        watched, the link is opened in this browser when its email comes (_check_mail), and the job goes
        on to sign in, once; without it the person opens the link, then presses Resume, which signs in once.

        An account there from before that was never verified (its link unopened, or gone: Vanguard's, the test
        identity, run 98) is refused the same way, with no word of an email just sent, and the page offers to
        send it again (Workday's "Resend Account Verification"). With the inbox watched that button is pressed,
        once a job, and the new email waited for; without it the card names the button. One the page says it
        just sent is waited for, not asked for again (a second email for an account the desk just made, at a
        site that says nothing of the first, is the cost of a wait that would never end at Vanguard's: the
        newest email's link is the one opened, the review of 0.3.178)."""
        run.sign_in_tries = min(run.sign_in_tries, SIGN_IN_TRIES - 1)
        site = _site(run, data)
        resend = next((a for a in data.get("actions") or [] if not a.get("disabled") and not a.get("cookie")
                       and not a.get("form_submit") and _RESEND_VERIFY.match(final_text(a.get("text") or "").strip())), None)
        label = final_text(resend["text"]).strip() if resend is not None else ""
        # an account from before, by the page's words: the refusal in its alerts, and no word of an email just sent
        stale = (_VERIFY_FIRST.search(" ".join(data.get("errors") or [])) is not None
                 and _VERIFY_SENT.search(" ".join([*(data.get("errors") or []), text[:3000]])) is None)
        said = (f"{site} won't sign in until your account there is verified from the link it emailed." if stale
                else f"{site} emailed you a link to verify your new account, and won't sign in until it's opened.")
        if self.mail_login() is not None:
            watching = "The desk is watching your inbox for it: it opens the link, then signs in with your saved password."
            if stale and resend is not None and not run.verify_resent:
                try:
                    await self.srv.browser.click(resend["id"])
                    pressed = True
                except Exception:  # gone (the page drawn again) or won't take a click
                    pressed = False
                if pressed:
                    run.verify_resent = True  # (once a job)
                    data, text = await self._look()  # (the page as it is now, paused on: the site's answer to the press)
                    if _SEND_FAILED.search(" ".join([*(data.get("errors") or []), text[:3000]])):
                        self._log(run, f"pressed \u201c{label}\u201d, and {site} says it couldn't send the email")
                        watching = (f"The desk pressed \u201c{label}\u201d, and {site} says it couldn't send the email: "
                                    "press it yourself in the browser window a little later. The desk is watching your "
                                    "inbox for the new email: it opens the link, then signs in with your saved password.")
                    else:
                        self._log(run, f"pressed \u201c{label}\u201d, so {site} emails the link again")
                        watching = (f"The desk pressed \u201c{label}\u201d and is watching your inbox for the new email: "
                                    "it opens the link, then signs in with your saved password.")
                else:
                    watching = (f"The desk couldn't press its \u201c{label}\u201d: press it in the browser window, and the "
                                "desk opens the new email's link from your inbox, then signs in with your saved password.")
            elif stale and resend is not None:  # (pressed before for this job: the email it sent is the one to find)
                watching = (f"The desk is watching your inbox for it (its \u201c{label}\u201d in the browser window sends "
                            "it again): it opens the link, then signs in with your saved password.")
            self._pause(run, "email_code", f"{said} {watching} The other jobs carry on meanwhile.", seen=data)
            run.blocking, run.left = False, True  # nothing for the person to do: the queue goes on
            self._wake.set()
        else:
            await self._bring_forward(run)
            again = f" (its \u201c{label}\u201d sends a new one)" if stale and resend is not None else ""
            self._pause(run, "sign_in", f"{said} Open the link in that email{again}, then press Resume: the desk signs in "
                        "with your saved password (an email app password saved on the desk lets it open the link itself).",
                        seen=data)
        run.verifying = True

    def _reset_overdue(self, run: Run) -> bool:
        """Waiting on a reset email, with the inbox watched, that hasn't come in RESET_MAIL_WAIT (or only one whose link
        was opened already, Run.reset_spent: _drive says so on a card, and never takes it for no email at all)."""
        # (the wait's own time: not while another job's email step kept its mail from being read; and only once a look
        # made after it has found none, never in the pass that couldn't look: the review of jobs at once)
        return (run.resetting and run.reset_waited and run.status == "needs_you" and run.need == "email_code"
                and not run.mail_done and not run.reset_no_mail and self.mail_login() is not None
                and _mail_waited(run) > RESET_MAIL_WAIT and not run.mail_held_at
                and run.mail_looked - run.paused_at - run.mail_held > RESET_MAIL_WAIT)

    @staticmethod
    def _reset_form_showing(data: dict[str, Any], text: str, button: str) -> bool:
        """Still a password reset's form: its new-password boxes, its reset words, the button that was pressed, and
        not the site's word that the password is reset."""
        boxes = [f for f in data.get("fields") or [] if f.get("kind") == "password" and not _code_box(f)]
        pressed = any(final_text(a.get("text") or "").lower() == button for a in data.get("actions") or [])
        return (len(boxes) >= 2 and pressed and bool(_RESET_PAGE.search(text[:2000]))
                and not _RESET_DONE.search(text[:2000]))

    async def _set_new_password(self, run: Run, data: dict[str, Any], text: str) -> str | None:
        """A password reset's page (opened from its email): the saved password in its new-password
        boxes, and its button pressed. "set" when the site took it, "paused" when it's the
        person's (a picture code, or a password the site turned down); None for any other page."""
        srv = self.srv
        passwords = _new_password_boxes(data, text)
        if not passwords:
            return None
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
        said_on = final_text(button.get("text") or "").lower() if button is not None else ""
        # A site that takes a moment over the new password leaves its reset form on show, with no error, until it goes
        # on to its sign-in (Workday's, live, issue #112, note 34): waited for, or the form is read as a sign-in
        # with no email box and the person is asked to sign in. Still there after RESET_SETTLE, it wasn't taken.
        deadline = time.monotonic() + RESET_SETTLE
        while (pressed and time.monotonic() < deadline and self._reset_form_showing(after, after_text, said_on)
               and not after.get("errors")):
            await asyncio.sleep(1)
            after, after_text = await self._look()
        if not pressed or self._reset_form_showing(after, after_text, said_on):
            await self._bring_forward(run)
            said = f" It says \u201c{after['errors'][0][:160]}\u201d." if after.get("errors") else ""
            self._pause(run, "sign_in", f"I put your saved password in {site}'s new-password boxes, and it didn't take it "
                        f"as the new one.{said} Set it there (or another, and save that one on the desk), then sign in; "
                        "the desk carries on after that.", seen=after)
            return "paused"
        if refused := _reset_refused(after, after_text):
            # its form gone, and the site's words that it set nothing (its link spent, or a password it had before):
            # never said to be set (KLA's Workday, the reset probe's second pass, run 136, a spent link's)
            await self._bring_forward(run)
            self._pause(run, "sign_in", f"I put your saved password in {site}'s new-password boxes and pressed its button, "
                        f"and it says \u201c{refused.rstrip(' .')}\u201d: it didn't set it as the new one. Reset it there "
                        "(through its \u201cForgot password\u201d again if the link is spent) to the one you saved on the "
                        "desk, or to another and save that one on the desk, then press Resume: the desk signs in with it.",
                        seen=after)
            return "paused"
        run.resetting = False
        run.reset_set = run.account_known = True
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
        # (in a cookie banner, or on a page that speaks of cookies; but with a dialog open that isn't a cookie banner (an
        # application's data-privacy statement: the review of Oct 15), only a button whose own words name cookies or the
        # necessary ones: its bare "Decline" is that dialog's. A banner the reader doesn't know by its names, a dialog
        # about cookies or a bar along the edge, is declined as before: the review of 0.3.188)
        other_dialog = any(not d.get("cookie") for d in data.get("dialogs") or [])
        button = next((a for a in actions if declines_cookies(a.get("text", ""), bool(a.get("cookie")))
                       and (a.get("cookie") or mentioned and (not other_dialog
                                                              or _NAMES_COOKIES.search(a.get("text", ""))))), None)
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
        Any other one over a form is theirs, as the boxes behind it don't fill: "paused". A dialog
        that offers how to start the application's profile is answered from the profile (_start_profile):
        "started". None: nothing done (on a page with nothing to fill, its buttons are the way on as
        before)."""
        if (started := await self._start_profile(run, data, notice, agreed)) is not None:
            return started
        heading = _notice_name(notice)
        ai, takes_ok = _agreeable_notice(heading, notice.get("text") or "", _test_identity())
        agree = next((b for b in notice.get("buttons") or [] if not b.get("disabled")
                      and (_AGREES.match(final_text(b.get("text") or ""))
                           or takes_ok and _TAKES_IN.match(final_text(b.get("text") or "")))), None)
        allowed = ai and agree is not None and _may_accept_notices()
        if allowed and agree is not None and heading not in agreed:
            try:
                clicked = await self._press_agreement(agree["id"])
            except Exception as e:  # gone, or it won't take a click: the person's, as without the setting
                clicked = {"clicked": False, "blocked": _error_said(e)}
            if not clicked.get("clicked"):
                self._log(run, f"the click on “{agree.get('text') or ''}” didn't go through: {clicked.get('blocked')}")
            if clicked.get("clicked"):
                agreed.add(heading)
                whose = f"{run.company}'s" if run.company else "the site's"
                self._log(run, f"agreed to {whose} notice “{_short(heading)}” for you (settings.accept_notices)")
                run.went_in.agree(f"agreed to {whose} notice “{_short(heading)}”")
                await self._notice_closed(heading)
                return "agreed"
        if not over_form:
            return None
        await self._bring_forward(run)
        self._pause(run, "stuck", f"{_site(run, data)} shows a notice over the form that only you can answer: "
                    f"“{_short(heading)}”. Read it and press its button in the browser window, then press "
                    "Resume; the desk fills the form in after that." + (
                        " (The desk agrees to an employer's notice about AI screening, its privacy notice or terms for "
                        "you with accept_notices: true under settings: in profile.yaml, outside practice mode.)"
                        if ai and agree is not None
                        and not allowed else ""))
        return "paused"

    async def _start_profile(self, run: Run, data: dict[str, Any], notice: dict[str, Any],
                             agreed: set[str]) -> str | None:
        """A dialog that offers how to start the application's profile (_profile_start: Eightfold's
        "Profile Bootstrapping", Northrop Grumman's, live, Oct 2026: "Upload your resume" or "Enter
        information"). It's no agreement and no question, but a choice of how to begin: its "Enter
        information" is pressed, and the form is filled from the profile as usual. Never its resume
        upload: the site's reading of a resume would fill the boxes with its own guesses (a degree
        from some college classes), which the desk never overwrites and never claims; the resume
        goes in where the application asks for it. Nothing else in the dialog is touched. Said in
        the log. "started": on to the page behind it; "paused": it stayed open after that (once a
        pass); None: no such dialog, or its button wouldn't take the press."""
        enter = _profile_start(notice)
        if enter is None:
            return None
        heading = _notice_name(notice)
        said = enter["text"].strip()
        if f"{heading}: {said}" in agreed:  # pressed on this pass, and it stayed open
            await self._bring_forward(run)
            self._pause(run, "stuck", f"I pressed “{said}” in the “{_short(heading)}” dialog on {_site(run, data)}, and "
                        "it stayed open. Look at it in the browser window, then press Resume.")
            return "paused"
        try:
            pressed = await self.srv.click(enter["id"])
        except KeyError:  # the dialog went by itself meanwhile: looked at again
            return "started"
        except Exception as e:  # (still drawing, covered): said, and the dialog is the person's as before
            self._log(run, f"the click on “{said}” failed: {_error_said(e)}")
            return None
        if not pressed.get("clicked"):
            self._log(run, f"the click on “{said}” didn't go through: {pressed.get('blocked')}")
            return None
        whose = f"{run.company}'s" if run.company else "the site's"
        self._log(run, f"pressed “{said}” in {whose} “{_short(heading)}” dialog, to fill the application from your "
                  "profile (your resume goes in where it asks for one)")
        agreed.add(f"{heading}: {said}")
        await self._notice_closed(heading)
        return "started"

    async def _press_agreement(self, element_id: str) -> dict[str, Any]:
        """Press a button found to agree for the person (settings.accept_notices: a terms gate's way
        on, a notice's agree), as the click tool does. Saying so lets the live check's test identity
        press it in practice mode (Browser.click's `agreement`); every other check on it stays."""
        try:
            return {"clicked": True, **await self.srv.browser.click(element_id, agreement=True)}
        except SubmitBlocked as e:
            return {"clicked": False, "blocked": str(e)}

    async def _notice_closed(self, heading: str, wait: float = NOTICE_WAIT) -> None:
        """After a notice's agree button: until the notice has gone. One fading out is read a
        moment longer, and looked at that soon, it would be taken for one that stayed."""
        deadline = time.monotonic() + wait
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
                what = "ticked" if attest[q["id"]] is True or isinstance(attest[q["id"]], list) \
                    else f"picked “{attest[q['id']]}” for"
                self._log(run, f"{what} “{_short(q.get('label') or '')}” for you (settings.accept_notices)")
                run.went_in.agree(f"{what} “{_short(q.get('label') or '')}”")
        return [q for q in pending if q["id"] not in done]

    async def _said_above(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """A box to tick whose own words don't say what it asks (Taylor Morrison's Workday "Acknowledgment*", run 73:
        what it acknowledges, and certifies, is the paragraph above it) goes on the card with those words."""
        out, looks = [], 0
        for q in pending:
            label = clean_label(q.get("label") or "")
            if q.get("kind") == "checkbox" and q.get("id") and looks < ABOUT_LOOKS and (
                    len(label.split()) <= 3 or _BARE_AGREE.match(label) or _READ_ABOVE.search(label)
                    or _READ_UNDERSTOOD.match(label) or _UNDERSTOOD_AGREE.match(label)):
                looks += 1
                with contextlib.suppress(Exception):  # (gone, or not read: the card as it was)
                    above = await self.srv.browser.words_before(str(q["id"]))
                    if above and norm(above) != norm(label):
                        q = {**q, "about": above[-ABOUT_SAID:]}
            out.append(q)
        return out

    async def _signing_in_still(self) -> bool:
        """After a sign-in was sent, its form still on show and nothing said wrong: a site that takes a moment
        to sign in (Intel's and TEL's Workday took a few seconds, nightly live check Oct 11). True when the
        password box goes within SIGN_IN_SETTLE seconds: the sign-in went through."""
        deadline = time.monotonic() + SIGN_IN_SETTLE
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            data, _ = await self._look(text=False)
            if data.get("errors"):
                return False
            if not any(f.get("kind") == "password" and not _code_box(f) for f in data.get("fields") or []):
                return True
        return False

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
        password, but for one more after a reset, a new account or a Resume on its sign-in card);
        `details=False` leaves a Create Account form's other boxes as they are."""
        srv = self.srv
        secret = password_for(data["url"])
        saved = _secret(secret) if secret is not None else None
        if secret is None or saved is None:
            return None
        key = hashlib.sha256(saved.encode()).hexdigest()
        if key != run.sign_in_key:  # a password saved since: not the one the site refused
            run.sign_in_key, run.sign_in_tries = key, 0
            run.reset_set = run.reset_opened = False  # (nor the one a reset set: its refusal is the ordinary path's)
        # the page's own fields: never a password box inside a frame from another site
        fields = [f for f in data.get("fields") or [] if not re.match(r"f\d+-", str(f.get("id")))]
        actions = [a for a in data.get("actions") or [] if not a.get("disabled")]
        if not fields:
            # Workday's "Sign in with email"; or, after its own password reset, its "Password has been reset"
            # and a "Sign In" that opens the form (issue #112, note 14, live test 2, Oct 2026)
            email_button = next((a for a in actions if re.match(r"^sign in with email$", a["text"], re.I)), None) or next(
                (a for a in actions if re.match(r"^(sign|log) ?in$", a["text"].strip(), re.I) and not a.get("cookie")), None)
            if email_button is None or tried.get("email_step", 0) >= 2:
                return None
            await srv.click(email_button["id"])
            return "email_step"
        passwords = [f for f in fields if f["kind"] == "password" and not _code_box(f)]
        create = _account_way(actions)
        # Where the way to a new account led: a form with one password box (UKG Pro's "Create
        # your account") is the new account's, as it no longer offers a way to one.
        signing_up = len(passwords) == 1 and bool(tried.get("create_account")) and create is None
        if (len(passwords) == 1 and tried.get("submitted") and not signing_up and not data.get("errors")
                and run.sign_in_tries > 0 and not tried.get("settled")):
            tried["settled"] = 1  # (once a job, and only after a sign-in the desk sent itself)
            if await self._signing_in_still():
                return "submitted"  # it was only slow (Intel's and TEL's Workday, nightly live check Oct 11)
        if len(passwords) == 1 and (tried.get("submitted") or run.sign_in_tries >= SIGN_IN_TRIES) and not signing_up:
            # Signed in once already and still asked to: the password didn't get in. Trying it
            # again won't help (and can lock an account: the job's sign-ins are counted across
            # Resume and the queue); a first visit needs an account. The way there gets a second
            # press: Amkor's sign-in page reloads after a failed sign-in, and a click on its "Create
            # an account" made before that has finished is lost. A Create Account the desk pressed
            # for this job already didn't make one (a Workday site goes back to its Sign In either
            # way): never a second, the password is reset instead. Nor one after the desk set the
            # saved password through the site's own reset, or opened its link: the account is there
            # (_drive says so).
            if (create is None or tried.get("create_account", 0) >= 2 or run.accounts_tried
                    or reset_first and _forgot_action(actions) or run.reset_set or run.reset_opened
                    or run.account_known):
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
        if not address or not 1 <= len(passwords) <= 2:
            return None
        new_account = len(passwords) == 2 or signing_up
        if new_account and run.account_in and not tried.get("to_sign_in"):
            # the account made for this job already, and signed in to (never one only pressed for: a form the site turned
            # down is said for what it wants, the review of 0.3.148), its Create Account on show again (Applied Materials'
            # Workday, run 60: signed out by its "something went wrong" refresh, it showed Create Account, and the desk
            # filled it and stopped): its own Sign In is the way back in, never a second account. The form's own link
            # first ("Already have an account? Sign In"), then the page's; never another site's sign-in
            ways = [a for a in actions if not a.get("cookie") and not _SOCIAL.search(a["text"])
                    and re.match(r"^(sign|log) ?in$", a["text"].strip(), re.I)]
            way = next((a for a in ways if a.get("in_account_form") or a.get("after_password")), None) or next(iter(ways), None)
            if way is not None:
                tried["to_sign_in"] = 1
                try:
                    await srv.click(way["id"])
                except KeyError:
                    return None
                self._log(run, "your account there is made already, so I went to its Sign In")
                return "email_step"
        if not boxes:
            # No box for the email: the password alone only on a sign-in that shows it knows who's signing in
            # ("Signing in as <email>", as Workday's after its own password reset may: issue #112, note 14),
            # with no other box to fill (a "User ID" the reader doesn't know stays the person's) and a box
            # that isn't a new, old or temporary password's (a reset's, a Create Account's, an emailed one).
            # Or a Create Account form that shows the email it's for (BrassRing's, after its emailed passcode,
            # run 67: "Email address" and the address, then its two password boxes): under an account heading,
            # its two boxes neither an old nor a temporary password's
            others = [f for f in fields if f["kind"] not in ("password", "checkbox", "radio", "radio_group",
                                                               "checkbox_group", "file", "hidden")]
            label = passwords[0].get("label") or "" if passwords else ""
            alone = not (new_account or others or len(passwords) != 1 or _NEW_PASSWORD.search(label)
                         or _OLD_PASSWORD.search(label))
            # (the Create Account's: two boxes, neither a new, old or temporary password's, under a title or heading
            # that makes an account, never a reset's nor a password change's: the review of 0.3.154)
            said = " ".join([data.get("title") or "", *(data.get("headings") or [])])
            for_it = (len(passwords) == 2 and not any(re.search(r"\bnew\b", f.get("label") or "", re.I)
                                                      or _OLD_PASSWORD.search(f.get("label") or "") for f in passwords)
                      and bool(_MAKES_ACCOUNT.search(said)) and not _RESET_PAGE.search(said))
            if not (alone or for_it) or not _shows_address(await srv.page_text(4000), address):
                return None
        if new_account:
            # The email in every box that asks for it ("Retype Email Address"; the user name
            # SCREEN's and Taleo's forms ask for is the email too), and the rest from the profile.
            if details:
                await self._fill_account_details(fields, data.get("headings"))
            if boxes:
                await srv.fill_form([{"id": f["id"], "value": address} for f in boxes])
        elif boxes:
            await srv.fill_form([{"id": boxes[0]["id"], "value": address}])
        for box in passwords:
            if not (await srv.fill_secret(box["id"], secret)).get("ok"):
                return None  # it didn't go in: say nothing about a saved password
        as_user = any(_user_name_box(f) for f in (boxes if new_account else boxes[:1]))
        if new_account:  # its terms and button: the person's, unless they let the desk (_make_account)
            self._log(run, "filled the Create Account form with your details and saved password"
                      + (" (your email as its user name)" if as_user else ""))
            return "prefilled"
        # The form's own button, after its password box: a "Sign In" in the site's header opens
        # its sign-in page or pop-up instead, and sends nothing (Workday's). Failing that, the
        # button of a form that holds only the sign-in, whatever it reads (SuccessFactors' "Submit").
        own = [a for a in actions if a.get("after_password") and not _SOCIAL.search(a["text"]) and not a.get("cookie")
               and not a.get("covered")]  # (Workday's hidden button: its cover takes the click)
        button = next((a for a in own if _SIGN_IN_ACTION.match(a["text"].strip())), None) or next(
            (a for a in own if a.get("sign_in_form") and _SIGN_IN_SUBMIT.match(final_text(a["text"]).strip())), None)
        if button is None:
            return "filled"
        await srv.click(button["id"])
        run.sign_in_tries += 1
        # whether that signed in is the next look's to say
        self._log(run, f"pressed \u201c{button['text'].strip()}\u201d with your saved password"
                  + (" and your email as the user name" if as_user else ""))
        return "submitted"

    async def _fill_account_details(self, fields: list[dict[str, Any]], headings: list[str] | None = None) -> int:
        """A Create Account form's other boxes, from the profile: names, country. Its check
        boxes (a newsletter, the site's terms) and file boxes ("upload your resume now?") are
        left alone, as is anything the profile doesn't answer (Benchmark's picture code), and
        its security questions and answers (the person's). How many it filled."""
        questions = {f["id"] for f in _security_boxes(fields, headings)}
        # On a form that asks security questions, only the account's own details (a name, the email, a phone, the
        # country), never a box asking a question: "In what town were you born?" or "City of birth" would get the
        # profile's city, an answer made up (the review of 0.3.155)
        guarded = _security_asked({"fields": fields, "headings": headings or []})
        empty = [f for f in fields if f.get("kind") in _ACCOUNT_KINDS and is_empty_value(f.get("value"))
                 and f["id"] not in questions and not (guarded and _QUESTION_LIKE.search(f.get("label") or ""))]
        plan = plan_autofill(empty, config.Profile.load(), {})
        to_fill = [f for f in plan["to_fill"] if not guarded or f.get("rule") in _ACCOUNT_DETAIL_RULES]
        if not to_fill:
            return 0
        out = await self.srv.fill_form([{"id": f["id"], "value": f["value"]} for f in to_fill])
        return sum(1 for r in out.get("results") or [] if r.get("ok"))

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
        before = data
        data, _ = await self._look(text=False)
        run.page_info = _page_info(data)  # the page as filled
        _went_in(run, result["filled"], before, data, shown=data)  # (and on the job's "What went in" panel)
        _went_left(run, result, before, data)
        gaps, pending = self._entry_gaps(run, data, pending)
        pending = await self._said_above(pending)
        if self._stop_for_what_is_left(run, gaps, pending, missing_files):
            return
        saved = await self._sign_in(run, data, {}, details=False) == "prefilled"  # filled in above
        if saved:  # the record of the page shows its password boxes filled, too
            run.page_info = _page_info((await self._look(text=False))[0])
        srv._mark_ready(srv.tracker().get(run.job_id), "filled by the Job Desk; the site creates the account as it applies")
        await self._bring_forward(run)
        password = "Your saved password is in its password boxes" if saved else "Choose a password in its password boxes"
        return self._pause(run, "your_submit", f"Filled in on {_site(run, data)}, which creates your account as you "
                           f"apply. {password}, check the page, then press its Apply button yourself: that sends the "
                           "application. Then press \u201cI submitted it\u201d here.")

    async def _bring_forward(self, run: Run) -> None:
        """Bring the job's tab to the front for the person: never over another job's tab they're at for the desk (a
        round, a pause that needs their hands, or a sign-in they're still at work in after the queue went on): this one
        is brought up after that (_front_next, or as it holds the queue in turn)."""
        if self._person_at(run) is not None:
            self._behind.add(run.job_id)
            return
        self._behind.discard(run.job_id)
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
                               "press Submit here." + (f" {SUBMIT_UNSENT}" if run.submit_unsent else ""))
        shown = "; ".join(e for e in _flagged(data) if not _ERROR_COUNT.match(e))[:300].rstrip(" .")
        invalid = [f["id"] for f in data.get("fields") or [] if f.get("invalid")
                   and f.get("kind") in ("select", "listbox", "combobox", "radio_group")]
        if shown and invalid and not run.invalid_redone:
            # a list the page marks invalid, on a choice it made itself (Northrop's phone "Country code" on Mexico,
            # live run 13): the profile's answer, once a job (never again after the person has had the page), then
            # the page is read again
            run.invalid_redone = True
            redone = (await srv.autofill(job_id=run.job_id, redo=invalid)).get("filled") or []
            _went_in(run, redone, data)
            again = [f for f in redone if f.get("id") in invalid]
            if again:
                self._log(run, f"filled again {len(again)} field(s) the page marked invalid")
                data, text = await self._look()
                shown = "; ".join(e for e in _flagged(data) if not _ERROR_COUNT.match(e))[:300].rstrip(" .")
        sends = run.submit and self.auto_submit and not self.hold_submits
        if shown and sends:  # Insight's review page lists what's still missing
            return self._pause(run, "stuck", f"The review page shows errors: {shown}. Fix them in the browser, then "
                               "press Submit.")
        # (jobs or schools left off, and a saved draft whose answers the desk changed to the profile's: the person's to
        # check, and to send: issue #112, note 38's review)
        if sends and not run.entries_short and not run.draft_changed:
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
        if run.entries_short:  # gone on with, on the person's Resume (note 32): said where Submit is
            names = " and ".join(_blocks(s, *n) for s, n in run.entries_short.items())
            run.reason += (f" Note: {_site(run, data)} shows only {names} (the Add button was covered): add the rest "
                           "in the browser before you press Submit.")
        if run.draft_changed:
            run.reason += (" Note: this is an application the site kept from an earlier visit, and I changed some of its "
                           "answers to your profile's (see What went in)" + (", so Submit for me left it for you" if sends
                                                                              else "")
                           + ": check them before you press Submit.")
        if run.submit_unsent:  # (the person's Submit from before a restart is never carried over: theirs to press again)
            run.reason += f" {SUBMIT_UNSENT}"
        self._log(run, "reached the review page")

    async def _submit(self, run: Run, by_person: bool = True) -> None:
        """Press the final button: the person pressed Submit for this job, or (by_person
        False) "Submit for me" is on, which also needs every required field filled."""
        srv = self.srv
        if run.status == "skipped" or self._already_done(run):
            return
        if self.hold_submits:  # (a Submit pressed before troubleshooting was turned on, still waiting its turn)
            run.status, run.need, run.blocking = "ready", "", False
            run.reason = ("Troubleshooting is on, so it wasn't submitted. It waits on its review page: turn troubleshooting "
                          "off and press Submit to send it.")
            self._log(run, run.reason)
            return
        # Submit for me: once more, as the job that may be this one could have been begun since it was filled (a Submit the
        # person pressed is theirs to send)
        if not by_person and self._held_for_maybe_same(run):
            return
        if run.page is not None and not run.page.is_closed() and not self._own_place(run, run.page.url):
            # its tab was taken on to another posting: Submit there would send that one as this job
            return self._pause(run, "stuck", "Its tab has gone on to another page, so I didn't press Submit there. "
                               "Press Resume to fill this application again in a tab of its own.")
        if not srv.browser.use_tab(run.page):
            # waiting on Resume now, whatever it waited on before (a CAPTCHA's pause has no Resume button)
            run.status, run.need, run.blocking, run.captcha = "needs_you", "stuck", False, False
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
        if run.status in ("skipped", "submitted"):  # skipped, or "I submitted it" pressed, while the page was checked
            return
        review = run.url  # (the page it waited on for this Submit)
        # kept as being pressed while it is: a restart meanwhile (the program ended mid-press) never queues it again, as
        # it may have gone. Over once the press has had its answer, or failed (its record and the tracker's note say).
        # Not kept (a full disk, say, where its folder's record of the press may not be written either): not pressed,
        # as a restart mid-press couldn't tell it may have gone
        run.sending = True
        if not self._save_queue():
            run.sending = False
            return self._pause(run, "your_submit", "Not sent: the desk couldn't record that this application is being "
                               "sent (is the disk full?), so a restart could send it twice. Press Submit yourself in the "
                               "browser window, then press I submitted it here.")
        try:
            result = await srv.submit_application(job_id=run.job_id, user_confirmed=True)
        except Exception:
            run.sending = False
            raise
        run.sending = False
        if not result.get("submitted") and run.status in ("skipped", "submitted"):  # (and so not pressed: see there)
            return
        if result.get("submitted") and not result.get("confirmed"):
            run.pressed_before = True  # it may have gone: Submit for me never presses it again
        if result.get("submitted") and result.get("confirmed"):
            run.status, run.need, run.reason, run.marked = "submitted", "", "Submitted.", False  # (the desk's own send)
            self._forget_draft_changes(run)  # (gone in: its draft's changes are no longer kept)
        elif result.get("submitted"):
            try:
                still = bool(await srv.browser.find_submit())
            except Exception:
                still = False
            problems = "; ".join(e for e in result.get("errors") or [] if _errorish(e))[:300]
            if still:  # the form is still there: the site didn't take it
                return self._pause(run, "submit_failed", "I pressed Submit, but the form is still there"
                                   + (f": {problems}" if problems else ".") + " Fix it in the browser, then press "
                                   "Submit again (or \u201cI submitted it\u201d if it did go through).")
            return self._pause(run, "check_submit", "I pressed Submit, but no confirmation showed. Check the page: "
                               "if it went through, press \u201cI submitted it\u201d.")
        elif result.get("no_submit_button") and by_person:
            # Submit pressed on the desk after the person sent it on the site themselves (issue #112, note 31: Workday's
            # "Application Submitted" was showing, and the job ended up skipped, its application never counted): the
            # page's own word that it went is taken, nothing pressed. A confirmation only on a page the tab went on
            # to, not the review page it waited on or the posting (a session gone back to it): "Thank you for
            # applying" in a posting's words, or "this application submitted by me is true" in an attestation's
            data, text = await self._look()
            job = srv.tracker().get(run.job_id, with_description=False) or {}
            moved = _bare(data.get("url") or "") not in {_bare(u) for u in (review, job.get("url"), job.get("apply_url")) if u}
            if gone := _sent_already(data, text, moved):
                srv.tracker().update(run.job_id, status="applied", note="submitted on the site")
                run.status, run.need, run.blocking, run.left = "submitted", "", False, False
                run.marked = not run.pressed_before  # (sent by the person, unless the desk's own press may have)
                self._forget_draft_changes(run)  # (gone in: its draft's changes are no longer kept)
                run.reason = f"Submitted: {_site(run, data)} says \u201c{gone}\u201d."
            else:
                return self._pause(run, "stuck", "I couldn't find a Submit button on this page. If you sent it yourself "
                                   "on the site, press \u201cI submitted it already\u201d; otherwise press Resume and the "
                                   "desk takes it on to its review page.")
        else:
            run.status, run.need = "ready", ""
            run.reason = result.get("reason") or "Not submitted."
        self._log(run, run.reason)


_APPLICATION_FIELD = re.compile(r"first name|last name|full name|legal name|resume|\bcv\b|phone|address|"
                                r"authori[sz]ed|sponsor|start (?:the |your |an? )?appl", re.I)  # "email to start application"


_ACCOUNT_FIELD = re.compile(r"e-?mail|password|user ?name|log ?in|terms|privacy|captcha|language|locale|"
                            r"text in (?:the )?(?:image|picture)", re.I)  # Benchmark's "Enter the text in image above"


def _new_password_boxes(data: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """A password reset's page (opened from its email): its new-password boxes. [] for any other page (a Create Account
    form asks for the email; a change asks the old one; a sign-in has one box, not a new password's)."""
    fields = [f for f in data.get("fields") or [] if not re.match(r"f\d+-", str(f.get("id")))]
    passwords = [f for f in fields if f.get("kind") == "password" and not _code_box(f)]
    named = [f for f in fields if f.get("kind") in ("text", "email")
             and re.search(r"e-?mail|user ?name|login", f.get("label") or "", re.I)]
    new = len(passwords) >= 2 or any(_NEW_PASSWORD.search(f.get("label") or "") for f in passwords)
    if (not passwords or named or not new or any(_OLD_PASSWORD.search(f.get("label") or "") for f in passwords)
            or not _RESET_PAGE.search(" ".join([text[:2000], *(data.get("headings") or [])]))):
        return []
    return passwords


def _reset_refused(data: dict[str, Any], text: str) -> str:
    """What a reset's page says of a new password it didn't set, or can't (_LINK_SPENT, _PASSWORD_USED), in its own
    words: its alerts, then its headings and lines (but the site's password rules shown there). "" when it says no such
    thing, or says the password is reset."""
    if _RESET_DONE.search(text[:2000]):
        return ""
    errors = [" ".join(str(e or "").split()) for e in data.get("errors") or []]
    shown = [" ".join(str(t or "").split()) for t in [*(data.get("headings") or []), *text[:2000].splitlines()]]
    for said in [*errors, *(t for t in shown if not _PASSWORD_RULES.search(t))]:
        # (a sentence of help that only says what to do if it were: "If this link has expired, request a new one.",
        # "The link may be invalid in some mail apps.": the review of 0.3.209)
        if any((_LINK_SPENT.search(part) or _PASSWORD_USED.search(part)) and not _ONLY_IF.search(part)
               for part in re.split(r"(?<=[.!?])\s+", said)):
            return said[:160]
    return ""


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


def _required_by_errors(result: dict[str, Any], data: dict[str, Any]) -> None:
    """A question left empty as optional that the page's errors name ("Errors Found Error-Please indicate
    your veteran status.", Workday's, live test 2, issue #112, note 18) is required after all: the page
    won't go on without it, though nothing on it said so. It's asked, instead of the step button being
    pressed twice more."""
    # (and Workday's error links, "Error-Please indicate if you are Hispanic or Latino?", where its errors read as
    # none: Baker Tilly's, the probe of Oct 12)
    links = [a.get("text") or "" for a in data.get("actions") or [] if re.match(r"\s*error\s*[-\u2013:]", a.get("text") or "", re.I)]
    said = norm(" ".join([*(data.get("errors") or []), *links]))
    wrong = bool(links) or any(_errorish(e) for e in data.get("errors") or [])  # (not a "Saved": the second review's)
    invalid = {f.get("id") for f in data.get("fields") or [] if f.get("invalid")}
    if not said and not invalid:
        return
    # (only a label the page has once: one error about "Job Title" isn't about every block's)
    names = [norm(clean_label(f.get("label") or "")) for f in data.get("fields") or []]
    for f in result.get("needs_input") or []:
        # the page marks it wrong as it is, empty (a lone box to tick only where the page also says something's wrong:
        # one left marked from the start, an optional job alerts box, isn't a question to stop on: the review of 0.3.170)
        if not f.get("required") and f.get("id") in invalid and (f.get("kind") != "checkbox" or wrong):
            f["required"] = True
            continue
        name = norm(clean_label(f.get("label") or ""))
        # or its section's (Kforce's "The field Closest Metropolitan City is mandatory." over its Region)
        section = norm(clean_label(str(f.get("section") or "")))
        # (a section only where the errors say it's mandatory or required, not wherever its name comes up)
        if not f.get("required") and (len(name) >= 8 and name in said and names.count(name) <= 1
                                      or len(section) >= 8 and re.search(
                                          re.escape(section) + r".{0,24}\b(mandatory|required)\b", said)):
            f["required"] = True


def _values(data: dict[str, Any]) -> dict[Any, Any]:
    """A page's boxes and what they show."""
    return {f.get("id"): str(f.get("value")) for f in data.get("fields") or []}


def _named_by_errors(data: dict[str, Any]) -> bool:
    """After a press the page didn't go on from: does it name in its errors, or mark wrong, a box of the application's
    left empty as optional (_required_by_errors)? Then that box is what holds it up, and its question is asked."""
    # (as autofill takes them: a read-only text box is never filled or asked)
    empty = [dict(f) for f in _app_fields(data) if not f.get("required") and not f.get("aside") and not f.get("disabled")
             and not (f.get("readonly") and f.get("kind") not in _CHOICE_KINDS)
             and f.get("kind") not in ("file", "password") and (is_empty_value(f.get("value")) or unset_choice(f))]
    _required_by_errors({"needs_input": empty}, data)
    return any(f.get("required") for f in empty)


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
    # (and where the person's own answer was turned down too, theirs is what's said, with the entries it opened:
    # HonorHealth's and Microchip's "How Did You Hear About Us?", the probe of Oct 12, were asked again and again with
    # the profile's refusal and the same first list)
    pending += [{"label": f.get("label") or "", "kind": f.get("kind") or ("combobox" if f.get("options") else "text"),
                 "required": True, "error": f.get("error"),
                 **({"options": f["options"]} if f.get("options") else {}),
                 **{k: f[k] for k in ("section", "sublabel", "role") if f.get(k)}, **turned_down(f), "id": f["id"]}
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


def _profile_start(notice: dict[str, Any]) -> dict[str, Any] | None:
    """A dialog's "Enter information" button, where the dialog offers how to start the application's
    profile: a button to upload the resume and one to enter the information by hand (Eightfold's
    "Create your profile with existing information"). Known by that offer, not by a site's name."""
    buttons = [b for b in notice.get("buttons") or [] if not b.get("disabled")]
    if not any(_UPLOAD_RESUME.match(final_text(b.get("text") or "")) for b in buttons):
        return None
    return next((b for b in buttons if _ENTER_INFO.match(final_text(b.get("text") or ""))), None)


def _notice_name(notice: dict[str, Any]) -> str:
    """A dialog's heading, or its first words."""
    return notice.get("heading") or _short(notice.get("text") or "") or "a dialog"


def _agreeable_notice(heading: str, text: str, test_identity: bool) -> tuple[bool, bool]:
    """Is a dialog over the page one the desk may agree to for the person (settings.accept_notices, _answer_notice), and
    may its Ok take it in? A notice about AI screening of applications, or the employer's privacy notice or terms of use
    (_terms_notice: its "Ok" beside a Cancel, where its heading names it), or one the live check's test identity agrees
    to (_test_identity_agrees). Never one whose words waive a right or sign the person up in so many words (ANYWHERE),
    nor one headed as anything never agreed to (_never_for_them, _AGREEMENT_KINDS): the review of Oct 15 found the AI
    notice's path without the terms notice's guards. Only its heading is read for the rest: an AI notice's own words
    name the other positions it may suggest (Micron's, live), which it signs no one up for. The test identity's
    heading isn't read for the terms of employment, as before ("Employment At-Will Statement": the owner's call, Oct 13)."""
    said = f"{heading} {text}"
    terms, takes_ok = _terms_notice(heading, text)
    ai = bool(_AI_NOTICE.search(said) and _ABOUT_APPLYING.search(said)) or terms \
        or _test_identity_agrees(heading, said, test_identity)
    headed = _AGREEMENT_KINDS - ({"at-will"} if test_identity else set())
    if ai and (_never_for_them(text, only=ANYWHERE) or _never_for_them(heading, only=headed)):
        ai = False
    return ai, takes_ok


def _attestation(q: dict[str, Any]) -> Any:
    """What attests to an application's attestation, a required box whose label certifies its
    information true and complete, consents to the background check that comes with applying
    (Eightfold's "Terms and Conditions" dropdown, its one choice certifying the application is
    correct), or accepts the employer's privacy notice or terms of use: a tick, its one real choice
    where that agrees, or its one Yes. A group of one tick whose own words accept them (EMD's)
    is ticked, and a lone radio's one choice is picked. None for any other box."""
    label = q.get("label") or ""
    # (never anything never agreed to for the person in its words (_attests_more): a legal waiver, an arbitration
    # agreement or a jury trial waived, the review of 0.3.165; the terms of employment, an at-will statement
    # acknowledged "accurate", unless its words certify the application's own information, the second review of
    # 0.3.170; an agreement that also signs the person up, the safety audit's)
    if not q.get("required") or q.get("error") or q.get("kind") not in _ATTEST_KINDS \
            or _attests_more(label, group=q.get("kind") == "checkbox_group"):
        return None
    choices = [str(o) for o in q.get("options") or [] if not is_empty_value(o)]
    if q["kind"] == "radio_group" and (len(choices) != 1 or not (polarity(choices[0]) is True or _AGREES.match(
            choices[0].strip())) or _about_person(label)):
        # (a lone "I agree" is a tick box: BrassRing's consents, Edward Jones' run 83; a Yes / No isn't picked, nor a
        # lone "I disagree", nor one whose words say something about the person: the review's)
        return None
    if q["kind"] == "checkbox_group":
        # its ticks that accept the terms, and only those: EMD's required pair is its Terms of Use &
        # Data Privacy Statement and its "Email Communications" (marketing, left unticked)
        agreed = [o for o in choices if _agrees_to_terms(o) and not _never_for_them(o, only={EMAILS, MAIL})]
        return agreed or None
    if not (_ATTESTS.search(label) or _agrees_to_terms(label)):
        return None
    if q["kind"] == "checkbox":
        # (a box whose words also say something about the person, "... and that I am legally authorized to work in
        # the US", or ask another question, is theirs, as a lone choice's are: the review of Oct 15)
        return None if _about_person(label) else True
    if len(choices) == 1:
        # (and only a choice that says no more than that: not "I agree, and send me text messages and job alerts", "I
        # agree to binding arbitration" or "I am authorized to work in the US", the second review of 0.3.165)
        # (an agreement's kinds and emails beyond the application, as before: "I have received and agree to the Privacy
        # Notice" is one)
        one = choices[0]
        more = _never_for_them(one, only=_AGREEMENT_KINDS | {EMAILS, MAIL}) or _CLAIMS.search(one)
        return one if polarity(one) is not False and not more else None
    if _about_person(label):
        return None  # a Yes there would answer something else too ("Have you ever been convicted ...?")
    yes = [o for o in choices if polarity(o) is True]
    return yes[0] if len(yes) == 1 else None


def _terms_gate(button: dict[str, Any], data: dict[str, Any], text: str = "") -> bool:
    """A page whose way on accepts the employer's privacy notice or terms ("I Acknowledge the
    Privacy Notice"; "I Accept" under a "Privacy Agreement" heading): its button, headings or title
    say so, not a privacy link in its footer. Never one that also sends the application ("I Accept
    and Apply"), nor one about what's never agreed to (_never_for_them: a talent community, an arbitration agreement),
    nor one on a page whose words (`text`, the page's, read whole by the callers) waive a right (arbitration, a
    jury trial: the review of Oct 15).
    Its headings are those of its own frame: Charles Schwab's careers page around its iCIMS frame
    has a "Join our talent network" sign-up of its own beside the gate (live, Oct 10). So the page's words are read
    for a legal waiver alone, never for that sign-up beside it."""
    said = button.get("text") or ""
    framed = data.get("frame_headings")
    frame = re.match(r"f\d+-", str(button.get("id") or ""))
    own = framed.get(frame.group(0) if frame else "") if isinstance(framed, dict) else None
    about = " ".join([*(own if own is not None else data.get("headings") or []), str(data.get("title") or "")])
    if _SENDS_TOO.search(said) or _never_for_them(f"{said} {about}", only=_AGREEMENT_KINDS) \
            or _never_for_them(text or "", only={"arbitration", "waiver"}):
        return False
    return bool(_TERMS.search(said) or _TERMS.search(about))


def _agreement_button(data: dict[str, Any]) -> dict[str, Any] | None:
    """A page's button that agrees to something ("I Acknowledge the Privacy Notice", "I Accept"). Never a cookie
    banner's (their own rule): what else it agrees to is _terms_gate's to read, the page's first such button."""
    return next((a for a in data.get("actions") or [] if _AGREEMENT.search(a["text"].strip())
                 and not a.get("cookie") and not _never_for_them(a["text"], only={"cookies"}) and not a.get("disabled")),
                None)


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


def _read_queue() -> dict[str, Any] | None:
    """The queue the desk kept (QUEUE_FILE), as {"paused": bool, "jobs": [{"id": int, ...}], "since": when it began
    being kept}; None when there's none. One that won't read (cut short, changed by hand) is said in the log, without
    its words, and the desk starts without it."""
    path = queue_path()
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(kept, dict) or not isinstance(kept.get("jobs"), list):
            raise ValueError("not a kept queue")
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("the desk's kept queue (%s) wouldn't read, so it starts without it",
                                            QUEUE_FILE)
        return None
    jobs = [e for e in kept["jobs"] if isinstance(e, dict) and type(e.get("id")) is int]
    since = kept.get("since")
    if not isinstance(since, (int, float)) or isinstance(since, bool) or since > time.time():
        since = mtime  # (a queue kept without it: as old as its file)
    return {"paused": kept.get("paused") is True, "jobs": jobs, "since": float(since)}


def _host(url: Any) -> str:
    """An address's host, or "" for one that has none (or won't read)."""
    try:
        return urlparse(str(url or "")).hostname or ""
    except ValueError:
        return ""


def _texts(value: Any, skip: tuple[str, ...] = ()) -> set[str]:
    """Every text in an answer or the profile (one of a list's, a dict's values, but those under the keys in `skip`),
    for _history_hidden."""
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        value = [v for k, v in value.items() if k not in skip]
    if isinstance(value, (list, tuple, set)):
        return {t for v in value for t in _texts(v, skip)}
    return set()


# The desk's own words for what went into a box (_went_value's), and a choice's: never hidden from a line's reason
_DESK_WORDS = {"ticked", "not ticked", "checked", "unchecked", "selected", "yes", "no", "true", "false", "none",
               "(set)", "uploaded", "(unconfirmed)"}


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


def _wants_verification(run: Run, data: dict[str, Any], text: str, sign_ins: dict[str, int]) -> bool:
    """A sign-in page refusing for want of the account's verification, with one password box and the page's words
    of verifying: the account the desk just made (Banner Health's "An email has been sent to you. Please verify
    your account"), or made on an earlier pass with the page still saying it emailed the link (a Resume before the
    link was opened), or an account from before whose refusal is in the page's own alerts ("Verify your account
    before you sign in", Vanguard's). Judged at the lap's look and again as a refusal stands (0.3.178 to 0.3.180)."""
    alerts = " ".join(data.get("errors") or [])
    return bool((sign_ins.get("made") or _VERIFY_FIRST.search(alerts) or run.accounts_tried and _VERIFY_SENT.search(alerts))
                and _VERIFY_EMAIL.search(" ".join([alerts, text[:3000]]))
                and sum(f.get("kind") == "password" and not _code_box(f) for f in data.get("fields") or []) == 1)


def _forgot_action(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A sign-in page's way to a password reset ("Forgot your password?")."""
    return next((a for a in actions if not a.get("disabled") and not a.get("cookie") and _FORGOT.search(a["text"])), None)


def _account_way(actions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A sign-in page's way to a new account, a link or plain button. A Create Account form's own
    button sends that form (it creates the account), so it's never it: a form's submit, or a button
    in a form with two password boxes (Workday's, a div)."""
    return next((a for a in actions if not a.get("disabled") and _CREATE_ACCOUNT.match(a["text"].strip())
                 and not a.get("form_submit") and not a.get("account_form")), None)


def _user_name_box(field: dict[str, Any]) -> bool:
    """A box for a user name, not the email (Taleo's "User Name"): the desk puts the email in it."""
    label = field.get("label") or ""
    return bool(_USER_NAME.search(label)) and not re.search(r"e-?mail", label, re.I)


def _shows_address(text: str, address: str) -> bool:
    """Does the page show the profile email, whole ("Signing in as <email>"; never inside a longer address)?"""
    return bool(address) and re.search(r"(?<![\w.+-])" + re.escape(address) + r"(?![\w-]|\.\w)", text, re.I) is not None


def _security_asked(data: dict[str, Any]) -> bool:
    """Security questions on a form, the person's: boxes for them, or a heading that says so (BrassRing's "Security
    questions", run 67: its question a list the form reader doesn't see, the review of 0.3.154)."""
    headings = data.get("headings") or []
    return bool(_security_boxes(data.get("fields") or [], headings)) or any(
        _SECURITY_QUESTION.search(h or "") for h in headings)


def _security_boxes(fields: list[dict[str, Any]], headings: list[str] | None = None) -> list[dict[str, Any]]:
    """A Create Account form's security questions and their answer boxes (a plain "Answer"
    beside them too): the person's to choose and answer, never made up. Named so by a box, or by the
    form's heading (BrassRing's "Security questions" over "Question 1" and "Question 1 Answer", run 67)."""
    if not any(_SECURITY_QUESTION.search(f.get("label") or "") for f in fields) and not any(
            _SECURITY_QUESTION.search(h or "") for h in headings or []):
        return []
    return [f for f in fields if f.get("kind") not in ("password", "checkbox", "file")
            and re.search(r"\bquestions?\b|\banswers?\b|\bhint\b", f.get("label") or "", re.I)]


def _account_card_boxes(fields: list[dict[str, Any]], headings: list[str] | None = None) -> list[dict[str, Any]]:
    """An account form's security questions the desk can put to the person on its card: its boxes for them, where
    there's a question to choose or write as well as an answer to give (a heading over an answer box alone means a
    question the desk can't read: those stay in the browser), each told apart from the others on the card (by
    placed_key: two "Question" boxes under one heading can't be). Empty when there's nothing to ask."""
    boxes = _security_boxes(fields, headings)
    asks = [f for f in boxes if re.search(r"\bquestions?\b", f.get("label") or "", re.I)
            and not re.search(r"\banswers?\b", f.get("label") or "", re.I)]
    answers = [f for f in boxes if f not in asks]
    if len({placed_key(f) for f in boxes}) < len(boxes):
        return []
    # (an answer for each question: one the label patterns miss, "Your response", leaves the form to the browser)
    return boxes if asks and len(answers) >= len(asks) else []


def _security_answered(data: dict[str, Any]) -> bool:
    """An account form's security questions all hold the person's answers, as the desk put them in from its card: each
    box marked and filled (read back "(set)": Browser._keep_secret). Told from the form as it is, never from what was
    done before: a form drawn again since has them empty, and isn't pressed (the review of the card)."""
    fields = data.get("fields") or []
    boxes = _account_card_boxes(fields, data.get("headings"))
    # (and nothing under its security questions' heading left empty: a box the label patterns miss)
    left = [f for f in fields if f not in boxes and f.get("kind") not in (
                "password", "checkbox", "checkbox_group", "radio", "radio_group", "file") and not _page_chrome(f)
            and _SECURITY_QUESTION.search(f.get("section") or "") and is_empty_value(f.get("value"))]
    return bool(boxes) and all(f.get("value") == "(set)" for f in boxes) and not left


def _captcha_on(data: dict[str, Any]) -> bool:
    """A CAPTCHA on the page, always the person's: a check's own frame (reCAPTCHA's, hCaptcha's), its
    pictures over the page, or a picture code's box (BrassRing's password reset, live, Oct 2026)."""
    return bool(data.get("captcha") or data.get("challenge")) or any(
        f.get("kind") in ("text", "number") and _PICTURE_CODE.search(f.get("label") or "")
        for f in data.get("fields") or [])


def _email_first_account(data: dict[str, Any]) -> bool:
    """A Create Account that asks for the email before any password (BrassRing's "Let's Get Started",
    live, Oct 2026: an email box and Continue, which emails a passcode to it): an email box, and no
    other but a picture code's."""
    fields = [f for f in data.get("fields") or [] if not f.get("disabled") and not f.get("aside")]
    emails = [f for f in fields if f.get("kind") in ("text", "email") and re.search(r"e-?mail", f.get("label") or "", re.I)]
    headings = " ".join(data.get("headings") or [])
    return (bool(emails) and all(f in emails or _PICTURE_CODE.search(f.get("label") or "") for f in fields)
            and bool(re.search(r"\baccount\b", headings, re.I)) and not _NOT_TERMS.search(headings))


def _mail_from(run: Run) -> float:
    """When a job's emailed code or link may have been sent: as its step that may email began on the way to its pause
    (_take_inbox), but never more than LOOK_BACK before its pause (as on its own); else as it paused."""
    if 0 < run.inbox_since <= run.paused_at:
        return max(run.inbox_since, run.paused_at - mailbox.LOOK_BACK)
    return run.paused_at


def _code_mark(code: str) -> int:
    """An emailed code as Run.desk_code keeps it: its letters and digits alone, in any case, hashed."""
    return hash(re.sub(r"[^0-9A-Za-z]", "", code).upper())


def _mail_waited(run: Run) -> float:
    """How long a job has waited on its email, but for the time its looks in the inbox were held off."""
    now = time.time()
    return now - run.paused_at - run.mail_held - (now - run.mail_held_at if run.mail_held_at else 0.0)


def _email_step(data: dict[str, Any]) -> bool:
    """A step that asks for the email alone (with its ticks): what's pressed on it may email a code there (Oracle
    Recruiting's "email to start the application", whose Next emails one)."""
    fields = [f for f in data.get("fields") or [] if not f.get("disabled") and not f.get("aside")
              and f.get("kind") not in ("checkbox", "radio", "radio_group", "checkbox_group")]
    return (len(fields) == 1 and fields[0].get("kind") in ("text", "email")
            and re.search(r"e-?mail", fields[0].get("label") or "", re.I) is not None)


def _may_email(run: Run, data: dict[str, Any], kind: str, view: str | None, finish: dict[str, Any] | None) -> bool:
    """A step that may email the person, or waits on an email: a sign-in (a site may email a code for one), making an
    account, a password reset, a verification, an emailed code, or a step asking for the email alone. Jobs filled at
    once take these one at a time (Applier._take_inbox)."""
    return (kind in ("sign_in", "email_code") or view is not None or finish is not None or run.resetting
            or run.verifying or bool(run.account_page) or _account_step(data) or _email_first_account(data)
            or kind == "form" and _email_step(data))  # (never a posting's job alerts box: a "page")


# A posting page's own boxes, never an application's: Phenom's "Save Job" ticks and its chatbot's box
_PAGE_WIDGET = re.compile(r"^save (?:this )?job$|\bchat ?bot\b", re.I)


def _application_like(data: dict[str, Any]) -> bool:
    """Does a page hold an application's boxes: three or more, or one an application asks for?
    Not a sign-up's beside a posting (a talent community's email and consent, which the form
    reader marks aside) nor the posting page's own ("Save Job", a chatbot's box)."""
    fields = [f for f in data.get("fields") or []
              if not f.get("aside") and not _PAGE_WIDGET.search(clean_label(f.get("label") or ""))]
    return len(fields) >= 3 or sum(bool(_APPLICATION_FIELD.search(f.get("label") or "")) for f in fields) >= 1


# A saved password is typed only into its own system's pages, on that system's own domains:
# never into a page that just mentions one in its address (evil.example/myworkdayjobs.com).
# Taleo's are also an employer's own career section that Oracle hosts on the employer's address
# (Kforce's myhiring.kforce.com, data/lists/phoenix-metro.yaml): named here one by one.
PASSWORD_SITES = {
    "workday": ("myworkdayjobs.com", "myworkday.com", "myworkdaysite.com"),
    "successfactors": ("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu"),
    "icims": ("icims.com",), "applicantstack": ("applicantstack.com",), "ukg": ("ultipro.com",),
    "infor": ("inforcloudsuite.com",), "taleo": ("taleo.net", "myhiring.kforce.com"), "brassring": ("brassring.com",),
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
                  "applicantstack_password": "ApplicantStack", "ukg_password": "UKG Pro", "infor_password": "Infor",
                  "taleo_password": "Taleo"}


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


def _manage_hint() -> str:
    """What a log line about an account made or a reset asked for ends with, until the profile has
    chosen the setting (either way): where to turn it off. Said to someone who chose it, it's noise
    (issue #112, notes 23 and 37)."""
    try:
        chosen = config.Profile.load().settings.manage_accounts_chosen
    except ValueError:  # a profile with a typo has chosen nothing
        chosen = False
    return "" if chosen else " (manage_accounts: false in profile.yaml leaves this to you)"


# The live check's test identity agrees to the notices a person's desk leaves to them (a waiver of rights in
# Freeport-McMoRan's data privacy statement on its Create Account, run 56): the owner's call, Oct 13, for the most to
# fix from. A notice of any of these sorts, never an error's or an existing profile's; and only by an agree button
# (_AGREES): never a Submit, which stays the person's (Insight Enterprises' notice confirms with one)
_TEST_NOTICE = re.compile(r"\b(?:privacy|terms|consent|agreement|notice|statement|policy)\b", re.I)


def _test_identity_agrees(heading: str, said: str, test_identity: bool) -> bool:
    """Whether the live check's test identity agrees to a notice the person's own desk leaves to them: only the
    test identity (config.live_test_identity: never a person's profile), only a notice (_TEST_NOTICE), never a
    dialog about an error, an overwrite, a deletion, a withdrawal or one that sends or applies (by its opening), never
    one that signs up for anything in so many words (CLAUDE.md's rule, test identity or not), and never one about
    cookies by its opening (their own rule: the review of 0.3.208)."""
    opening = said[:300]  # (its heading and first words say what it is; a long statement's later words don't)
    return (test_identity and bool(_TEST_NOTICE.search(said)) and not _NOT_A_NOTICE.search(opening)
            and not re.search(r"\bwent wrong\b|\bsubmit\w*|\bapply\b|\bwithdraw", opening, re.I)
            and not _never_for_them(said, only={"signs up"}) and not _never_for_them(opening, only={"cookies"}))


def _test_identity() -> bool:
    try:
        return config.Profile.load().settings.test_identity
    except ValueError:  # a profile with a typo: never the test identity
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
    return [f for f in data.get("fields") or [] if f.get("required") and not f.get("disabled") and not f.get("aside")
            and f.get("kind") != "password" and is_empty_value(f.get("value"))]


def _application_boxes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """A page's boxes but a sign-up's for job alerts, a newsletter or a talent community: those whose
    own words say so, and the one or two name and email boxes of a page whose headings or buttons are
    about one ("Get job alerts": Email, Subscribe), as the form reader tells such a box's Submit. Those
    stay where they may be the application's after all: beside a step button (a sign-up has its own
    Subscribe or Sign up, not Next), or on a page whose sign-up email box its own words tell already."""
    fields = [f for f in data.get("fields") or [] if not f.get("disabled")]
    side = [bool(_SIDE_BOX.search(" ".join(str(f.get(k) or "") for k in ("label", "section", "sublabel"))))
            for f in fields]
    boxes = [f for f, s in zip(fields, side) if not s]
    about = any(_SIDE_BOX.search(h) for h in data.get("headings") or []) or any(
        a.get("aside") or _SIDE_BOX.search(a.get("text") or "") for a in data.get("actions") or [])
    told = any(s and f.get("kind") == "text" for f, s in zip(fields, side))  # ("Get job alerts by email")
    if (about and len(boxes) <= 2 and not told and not _step_beside(data)
            and all(_CONTACT_FIELD.match(norm(clean_label(f.get("label") or ""))) for f in boxes)):
        return []
    return boxes


def _step_beside(data: dict[str, Any]) -> bool:
    """Is a step button (Next, Continue) beside a page's boxes, greyed out or not (until they're
    filled in): one in a form with boxes, or any while no sign-up's button sits in a form of its own?
    Not a survey's bare Continue beside a job-alerts form's Email and Subscribe."""
    actions = data.get("actions") or []
    framed = [bool(a.get("form_submit") or a.get("form_fields")) for a in actions]
    own = any(a.get("aside") or f and _SIDE_BOX.search(a.get("text") or "") for a, f in zip(actions, framed))
    return any(_FORWARD.match(final_text(a.get("text") or "")) and (f or not own) for a, f in zip(actions, framed))


# What a page that has the application asks a person to go on to after it: more jobs ("Please continue
# to browse our open positions"). Not "optional" or "voluntary" questions, nor a survey: an application's
# own steps say those before its Submit ("Please complete the voluntary self-identification questions below")
_AFTER_SENT = re.compile(r"\b(?:browse|explore|search|look at|view (?:our|all|other|more))\b|"
                         r"\b(?:open|other|more|similar) (?:positions|jobs|roles|opportunities)\b", re.I)


def _goes_on(text: str) -> bool:
    """Does a page thank the person for applying and then ask them to go on with it, in the same
    sentence or the next ("Thank you for your application. Please complete the below questions.")?
    Not on to more jobs, as a page says once the application has gone."""
    flat = re.sub(r"\s+", " ", (text or "").replace("\u2019", "'").replace("\xa0", " "))
    for m in CONFIRMATION_RE.finditer(flat):
        after = " ".join(re.split(r"(?<=[.!?]) ", flat[m.end():], maxsplit=2)[:2])
        if (go := _GO_ON.search(after)) and not _AFTER_SENT.search(after[go.start():go.start() + 80]):
            return True
    return False


def _mid_application(data: dict[str, Any], text: str) -> bool:
    """A page partway through the form, though it may thank the person for applying: one that asks them
    to go on after that, a progress bar short of its last step (Workday's "current step 3 of 6"), a
    required box of the application's still empty, or a button on to the next step beside its boxes
    (Save and Continue even with none: nothing is saved once it's sent). Not a page that thanks the
    person and offers job alerts (a required Email, its Subscribe) or a bare Continue (to a voluntary
    survey, or back to the careers site). In doubt, partway: a confirmation missed leaves the person to
    press "I submitted it", and an application taken for sent is never filled in again."""
    if _goes_on(text) or any(int(m[1]) < int(m[2]) for h in data.get("headings") or [] for m in _STEP_OF.finditer(h)):
        return True
    boxes = _application_boxes(data)
    steps = [final_text(a.get("text") or "") for a in data.get("actions") or [] if not a.get("disabled")]
    return bool(_empty_required({"fields": boxes})) or any(
        _FORWARD.match(t) and (boxes or t.lower().startswith("save")) for t in steps)


def _new_required(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Did filling the page bring up required fields that weren't there before?"""
    seen = {f.get("id") for f in before.get("fields") or []}
    return any(f.get("id") not in seen for f in _empty_required(after))


def _no_sign_in_here(run: Run, data: dict[str, Any]) -> bool:
    """The sign-in page a reset was asked from, opened again, led elsewhere with no sign-in on it: another address,
    no password box, no box for an email or user name (an email-first sign-in's step), no way to an account, and
    nothing like an application (a job search's boxes aren't)."""
    fields = data.get("fields") or []
    # (a job search, at any address: Edwards' SuccessFactors draws its search at its sign-in's own address, the probe
    # of Oct 12)
    return (run.page is not None and (_bare(run.page.url) != _bare(run.reset_from or "") or _job_search(data))
            and not any(f.get("kind") == "password" or _SIGN_IN_NAME.search(f.get("label") or "") for f in fields)
            and _account_way(data.get("actions") or []) is None
            and (not _application_like(data) or _job_search(data) and not any(
                _APPLICATION_FIELD.search(f.get("label") or "") for f in fields)))


def _job_search(data: dict[str, Any]) -> bool:
    """A site's job search (Edwards' SuccessFactors careers home, the nightly of Oct 12: keywords, a requisition ID
    and seven filters, which read as an application's boxes): its Search button and a box for keywords."""
    return (any(_JOB_SEARCH_BUTTON.match(final_text(a.get("text") or "")) for a in data.get("actions") or [])
            and any(_JOB_SEARCH_BOX.search(f.get("label") or "") for f in data.get("fields") or []))


_JOB_SEARCH_BUTTON = re.compile(r"^\s*search(?: for)?(?: jobs| openings| positions| careers)?\s*$", re.I)
_JOB_SEARCH_BOX = re.compile(r"keywords?|requisition id|job id|search jobs", re.I)
_SIGN_IN_NAME = re.compile(r"e-?mail|user ?name|user ?id|login", re.I)


def _bare(url: str) -> str:
    """An address without its query and fragment."""
    return urlparse(url)._replace(query="", fragment="").geturl()


# The query keys that name a job in an address: Eightfold's pid, Taleo's job, SuccessFactors'
# career_job_req_id, Greenhouse's gh_jid, and the usual jobId / reqId
_JOB_ID_KEY = re.compile(r"pid|job|jid|gh_jid|job_?id|req_?id|job_?req_?id|requisition_?id|career_job_req_id", re.I)


def _job_ids(url: str) -> dict[str, str]:
    """The job ids an address carries in its query, by key (lower case): values with a digit, as a
    job's id has, not a word under one of those keys (?job=apply)."""
    return {k.lower(): v for k, v in parse_qsl(urlparse(url).query) if _JOB_ID_KEY.fullmatch(k) and re.search(r"\d", v)}


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


def _email_account_view(data: dict[str, Any]) -> str | None:
    """Which of an email-first site's steps of making an account a page shows (an Eightfold site's
    modal, Northrop Grumman's, live, Oct 2026, once its sign-in said "We don't recognize this
    email"), read only on the page that said so (Run.account_page):

    - "consent": its agreement ("Please review before continuing"): only boxes and choices, one of
      them agreeing to the site's terms or privacy policy;
    - "email": the email again, under a "Create an account" heading;
    - "options": a password or a one-time code (a site that doesn't hide its password field);
    - "code": the emailed code.

    None for any other page (its application, or its sign-in again)."""
    fields = [f for f in data.get("fields") or [] if not f.get("disabled") and not f.get("aside")]
    if any(_code_box(f) for f in fields):
        return "code"
    if not fields:
        offered = any(_ONE_TIME_CODE.match(final_text(a["text"]).strip()) and not a.get("disabled")
                      for a in data.get("actions") or [])
        return "options" if offered else None
    if (all(f.get("kind") in ("checkbox", "radio", "radio_group", "checkbox_group") for f in fields)
            and any(f.get("kind") == "checkbox" and _TERMS_BOX.search(f.get("label") or "")
                    and not _NOT_TERMS.search(f.get("label") or "") for f in fields)):
        return "consent"
    if (len(fields) == 1 and fields[0].get("kind") in ("text", "email")
            and re.search(r"e-?mail", fields[0].get("label") or "", re.I)
            and any(_ACCOUNT_PAGE.search(h) for h in data.get("headings") or [])):
        return "email"
    return None


def _account_details(data: dict[str, Any], text: str) -> dict[str, Any] | None:
    """The rest of a Create Account that asks for more after its email and password (UKG Pro's
    Register page, live, Oct 2026: "Almost there!", a name and phone, a consent box, and "Create
    account", greyed out until the box is ticked): a page about making an account (its title,
    headings or address), with boxes and no password box, whose only way on is its own Create
    Account button. Never a sign-up for job alerts, a newsletter or a talent community. That
    button (greyed out or not), else None."""
    fields = data.get("fields") or []
    if (not any(not f.get("aside") for f in fields) or any(f.get("kind") == "password" for f in fields)
            or _SIDE_BOX.search(text[:3000])):
        return None
    about = " ".join([data.get("title") or "", *(data.get("headings") or []), urlparse(data.get("url") or "").path])
    if not _ACCOUNT_PAGE.search(about):
        return None
    actions = [a for a in data.get("actions") or [] if not a.get("cookie") and not a.get("aside")]
    if any(not a.get("disabled") and (a.get("is_submit") or _FORWARD.match(final_text(a["text"]))
                                      or _ENTRY.match(final_text(a["text"]))) for a in actions):
        return None  # an application's step (or a posting's Apply) is the way on there
    return next((a for a in actions if _ACCOUNT_BUTTON.match(final_text(a["text"]).strip())
                 and not _SOCIAL.search(a["text"])), None)


def _terms_boxes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """A Create Account form's boxes that agree to the site's terms, still unticked: the required
    ones, or its terms, or a privacy notice read. Never one whose words take in more (_more_than_terms: a
    newsletter's, job alerts', text messages', a legal waiver's, the terms of employment), or a box that
    also says something about the person (the review of Oct 15), except an age the profile answers ("I am 16
    years or older, and I have read the Privacy Notice": Microchip's, ticked where the profile says over 18)."""
    over_18 = _profile_over_18()

    def claims(label: str) -> bool:
        return _about_person(_AGE_STATED.sub(" ", label) if over_18 else label)

    return [f for f in data.get("fields") or [] if f.get("kind") == "checkbox" and is_empty_value(f.get("value"))
            and (_READ_UNDERSTOOD.match(f.get("label") or "")  # (what it agrees to is above it: _said_terms reads it)
                 or _TERMS_BOX.search(f.get("label") or "") and not _more_than_terms(f.get("label") or "")
                 and not claims(f.get("label") or "")
                 and (f.get("required") or _TERMS_ONLY.search(f.get("label") or "")))]


def _consent_boxes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """An email-first site's agreement step's boxes that agree to its terms or privacy policy (_email_account_step's
    "consent"), still unticked: never one whose words take in more (_more_than_terms: a newsletter's, job alerts', a
    legal waiver's). Those choices are the person's."""
    return [f for f in data.get("fields") or [] if f.get("kind") == "checkbox" and not f.get("disabled")
            and is_empty_value(f.get("value")) and _TERMS_BOX.search(f.get("label") or "")
            and _TERMS_ONLY.search(f.get("label") or "") and not _more_than_terms(f.get("label") or "")]


def _profile_over_18() -> bool:
    """Whether the profile says the person is over 18 (work_authorization.over_18); a profile with a typo says nothing."""
    try:
        return config.Profile.load().get("work_authorization.over_18") is True
    except ValueError:
        return False


def _privacy_statement(data: dict[str, Any]) -> dict[str, Any] | None:
    """A Create Account form's link or button that opens the site's data privacy statement to be read
    and accepted (_STATEMENT_LINK: "Read and accept the data privacy statement."), after its password
    boxes and in the same form. Not one that also says something never agreed to (_agrees_to_terms:
    a newsletter's, job alerts', a talent community's), nor a privacy link that accepts nothing."""
    return next((a for a in data.get("actions") or [] if a.get("in_account_form") and not a.get("disabled")
                 and not a.get("cookie") and not a.get("aside") and _STATEMENT_LINK.match(a["text"].strip())
                 and _agrees_to_terms(a["text"])), None)


def _new_candidate_boxes(data: dict[str, Any], company: str) -> list[dict[str, Any]]:
    """A Create Account form's box saying the person is a new candidate, not a current employee
    (_NEW_CANDIDATE), still unticked: ticked from the profile, so only when it has no current job
    at this employer (autofill.works_there_now). Someone who works there now leaves it be (an
    employee applies the way their employer says)."""
    boxes = [f for f in data.get("fields") or [] if f.get("kind") == "checkbox" and not f.get("disabled")
             and is_empty_value(f.get("value")) and _NEW_CANDIDATE.fullmatch(clean_label(f.get("label") or "").rstrip(" ."))]
    profile = config.Profile.load()
    if not boxes or not company or not (profile.get("work_history") or profile.get("experience.current_company")):
        return []  # (a profile that says nothing of their work says nothing of this either)
    return [] if works_there_now(profile, company) else boxes


def _other_positions_consent(field: dict[str, Any]) -> Any:
    """The value that gives an account form's consent to be considered for other open positions
    (_OTHER_POSITIONS): True for its box, or a group's one choice. None for anything else: a group
    with a choice between yes and no (a question for the person), or words that also take in anything else never agreed
    to for them (_never_for_them, _AGREEMENT_KINDS: a newsletter, job alerts, a talent pool, keeping their data). Its
    own words, other roles, are the owner's call (Oct 10): that kind alone is read without them."""
    kind = field.get("kind")
    options = [o for o in field.get("options") or [] if isinstance(o, str)]
    value: Any
    if kind == "checkbox":
        said, value = field.get("label") or "", True
    elif kind in ("radio", "radio_group") and len(options) == 1:
        said, value = f"{field.get('label') or ''} {options[0]}", options[0]
    elif kind == "checkbox_group" and len(options) == 1:
        said, value = f"{field.get('label') or ''} {options[0]}", [options[0]]
    else:
        return None
    if not _OTHER_POSITIONS.search(said) or _never_for_them(said, only=_AGREEMENT_KINDS - {"other roles"}) \
            or _never_for_them(_OTHER_POSITIONS.sub(" ", said), only={"other roles"}):
        return None
    return value


def _tick(field: dict[str, Any]) -> Any:
    """The value that ticks an account form's box, or gives its consent to be considered for other
    open positions (_other_positions_consent)."""
    given = _other_positions_consent(field)
    return True if given is None else given


def _other_positions_boxes(data: dict[str, Any], required: bool = True) -> list[dict[str, Any]]:
    """An account form's consents to be considered for other open positions still not given: the
    ones it marks required (or, required=False, all of them: once the site has refused to go on
    without them)."""
    return [f for f in data.get("fields") or [] if not f.get("disabled") and not f.get("aside")
            and is_empty_value(f.get("value")) and (f.get("required") or not required)
            and _other_positions_consent(f) is not None]


def _account_made_says(site: str, terms: bool, password: bool = True) -> str:
    """What the log says once the site shows the account made (Run.account_made)."""
    return ((f"created your account on {site} with your saved password" if password
             else f"finished creating your account on {site}") + (" and agreed to its terms" if terms else "")
            + _manage_hint())


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


def _unloaded_lists(pending: list[dict[str, Any]], data: dict[str, Any], prof: Any) -> list[dict[str, Any]]:
    """The questions that are a school's Degree or major list read with its placeholder alone (entry_list_unloaded),
    never a search prompt (its choices come as it's typed in; the page's own record of the box says which)."""
    page = {f.get("id"): f for f in data.get("fields") or []}
    return [q for q in pending if not (page.get(q.get("id")) or {}).get("search") and entry_list_unloaded(q, prof)]


def _unloaded_said(unloaded: list[dict[str, Any]]) -> str:
    """Those lists by name ("“Degree” list")."""
    names = list(dict.fromkeys(clean_label(q.get("label") or "").strip() for q in unloaded))
    if not all(names):
        return "list" if len(names) == 1 else "lists"
    return f"{' and '.join(f'“{n}”' for n in names[:3])} list{'s' if len(names) > 1 else ''}"


def _account_wants(left: dict[str, Any], filled: dict[str, Any]) -> str:
    """What a Create Account form that didn't go through says it wants: as the site `left` it, its
    error or alert text and what it marks (Workday's "Error-Email" links, boxes marked invalid); or
    else, as `filled` in again, its boxes still empty that it requires or that agree to its terms, by
    name. "" when it shows nothing."""
    errors = left.get("errors") or []
    said = list(dict.fromkeys(" ".join(e.split()) for e in errors if not _ERROR_COUNT.match(e.strip())))
    marked = [m for m in _flagged(left) if m not in errors]
    if said or marked:
        return "; ".join(([f"it says \u201c{'; '.join(said)[:300].rstrip(' .')}\u201d"] if said else []) + marked) + "." \
            + _choices_left(left)
    empty = [f for f in filled.get("fields") or [] if f.get("label") and not f.get("disabled")
             and f.get("kind") not in ("password", "file") and is_empty_value(f.get("value"))
             and (f.get("required") or f.get("kind") == "checkbox" and (_TERMS_BOX.search(f["label"])
                  and not _NOT_TERMS.search(f["label"]) or _NEW_CANDIDATE.fullmatch(clean_label(f["label"]).rstrip(" ."))))]
    named = [f"\u201c{_short(f['label'])}\u201d " + ("isn't ticked" if f.get("kind") == "checkbox" else "is still empty")
             for f in empty[:3]]
    return "; ".join(named) + "." + _choices_left(filled) if named else ""


def _choices_left(data: dict[str, Any]) -> str:
    """A Create Account form's choices it marks or requires and the desk left (Edwards Vacuum's "Make my profile
    visible to:", run 61: which recruiters see the profile, the person's to say), each with what it offers."""
    left = [(f, options) for f in data.get("fields") or [] if f.get("kind") in ("radio_group", "select", "listbox")
            and f.get("label") and not f.get("disabled") and is_empty_value(f.get("value"))
            and (f.get("invalid") or f.get("required"))
            and (options := [o for o in f.get("options") or [] if not is_empty_value(o)])]
    return "".join(f" Its choices for \u201c{_short(f['label'])}\u201d: " + ", ".join(f"\u201c{_short(o)}\u201d" for o in options[:4])
                   + (f" and {len(options) - 4} more" if len(options) > 4 else "") + "; that choice is yours."
                   for f, options in left[:2])


_ASSESSMENT = re.compile(r"^(?:take|start|begin|launch|complete|go to)\s+(?:the\s+|your\s+|an?\s+)?"
                         r"(?:online\s+)?assessments?$", re.I)


# A step's link to a questionnaire about the applicant for a hiring tax credit (Kforce's Taleo, live run 41, Oct 12:
# ". Required Complete Tax Credit Screening Questionnaire", and "You must provide the information requested in the
# following section: Employment Tax Credit Screening"): answers about the person only they can give
_SCREENING = re.compile(r"^\W*(?:required\s+)?(?:complete|start|take|begin|launch|go to|open)\s+(?:the\s+|your\s+|an?\s+)?"
                        r"(?:employment\s+)?(?:tax credit|wotc)(?:\s+screening)?(?:\s+(?:questionnaire|survey|form))?$",
                        re.I)


# What a step's errors say when it waits on that questionnaire ("...the following section: Employment Tax Credit
# Screening"), where its link isn't one the form reader lists
_SCREENING_SAID = re.compile(r"\btax credit screening\b|\bwotc\b", re.I)


def _screening_button(data: dict[str, Any]) -> str:
    """The link that opens a tax credit screening questionnaire about the applicant, if the page has one."""
    return next((re.sub(r"^\W*(?:required\s+)?", "", final_text(a.get("text") or "").strip(), flags=re.I)
                 for a in data.get("actions") or []
                 if _SCREENING.match(final_text(a.get("text") or "").strip()) and not a.get("disabled")), "")


def _assessment_button(data: dict[str, Any]) -> str:
    """The button that starts an employer's assessment of the applicant ("Take Assessment"), if the page has one."""
    return next((a["text"].strip() for a in data.get("actions") or []
                 if _ASSESSMENT.match(final_text(a.get("text") or "").strip()) and not a.get("disabled")), "")


def _greyed_step(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The page's step buttons (Next, Continue) that are greyed out."""
    return [a for a in data.get("actions") or [] if a.get("disabled") and _FORWARD.match(final_text(a["text"]))]


def _came_on(before: dict[str, Any], data: dict[str, Any]) -> bool:
    """Has a Submit or step button greyed out on `before` come on? (Wells Fargo's Workday review page drew its
    Submit greyed for a moment, run 49.) Only those: another button coming on says nothing of the way on."""
    def way_on(a: dict[str, Any]) -> bool:
        return not a.get("aside") and bool(a.get("is_submit") or _FORWARD.match(final_text(a.get("text") or "")))
    # (not a text a button already on had then: a covered Workday twin of a greyed Submit isn't one that came on)
    was = ({final_text(a.get("text") or "") for a in before.get("actions") or [] if a.get("disabled") and way_on(a)}
           - {final_text(a.get("text") or "") for a in before.get("actions") or [] if not a.get("disabled") and way_on(a)})
    return any(not a.get("disabled") and way_on(a) and final_text(a.get("text") or "") in was
               for a in data.get("actions") or [])


def _boxes(data: dict[str, Any]) -> int:
    """How many boxes a page has to fill (not counting file uploads)."""
    return sum(f.get("kind") != "file" and not f.get("disabled") for f in data.get("fields") or [])


def _page_key(data: dict[str, Any]) -> tuple:
    """Which page of an application this is: its address and headings."""
    return _bare(data.get("url") or ""), tuple(data.get("headings") or [])


# A heading the page shows over its own step when something on it is wrong (Workday's "Errors Found")
_ERRORS_HEADING = re.compile(r"^(?:\d+\s+)?errors?\s+(?:found|on (?:this|the) page)\b", re.I)


def _answers_page(data: dict[str, Any]) -> tuple:
    """The page an answer the person gave went in on (_fill_once): _page_key without an errors heading, which
    the same step shows once it's pressed with something wrong (Workday's "Errors Found")."""
    return _bare(data.get("url") or ""), tuple(h for h in data.get("headings") or [] if not _ERRORS_HEADING.match(h.strip()))


def _step_page(data: dict[str, Any]) -> tuple:
    """A step, as it is before and after it's pressed with something wrong: its bare address and headings, without
    any heading that says what's wrong ("Errors Found", "There are 2 errors on this page": the review of 0.3.165)."""
    return _bare(data.get("url") or ""), tuple(h for h in data.get("headings") or []
                                               if not _ERRORS_HEADING.match(h.strip()) and not _errorish(h))


def _code_only_submit(data: dict[str, Any]) -> dict[str, Any] | None:
    """The "Submit" of a page whose only boxes are an emailed code's (Northrop Grumman's Eightfold sign-in,
    live, Oct 2026: "Check your email", six "Please enter OTP character" boxes and "Submit"): it sends the
    code and nothing else, so it's pressed after the code, as a "Verify" is (and only where the step's own
    words don't speak of an application: Applier._code_only_submit)."""
    fields = [f for f in data.get("fields") or [] if not f.get("disabled") and not f.get("aside")]
    if not fields or not all(_code_box(f) for f in fields):
        return None
    return next((a for a in data.get("actions") or [] if not a.get("disabled") and not a.get("cookie")
                 and not a.get("aside") and _CODE_SENDS.match(final_text(a.get("text") or "").strip())), None)


def _error_said(e: BaseException) -> str:
    """An error, as the job's log says it: its kind and its first line (a browser's has a call log after)."""
    return f"{type(e).__name__}: " + (str(e).strip().splitlines() or [""])[0][:200]


def _page_sig(data: dict[str, Any]) -> tuple:
    """What a page is, near enough: its address, headings and the questions on it."""
    return (_bare(data.get("url") or ""), tuple(data.get("headings") or []),
            tuple(sorted(f.get("label") or "" for f in data.get("fields") or [] if isinstance(f, dict))))


# A requisition's id at the end of a posting's address (Workday's ".../job/<place>/<title>_R12345", "_JR-0012345")
_REQ_IN_PATH = re.compile(r"_([A-Za-z]{0,6}-?\d[\w-]*)/?$")


def _own_draft(action: dict[str, Any], data: dict[str, Any], job_url: str) -> bool:
    """Is a saved draft's Continue Application this job's (issue #112, note 38's review)? On the job's own page (its
    address, and the job ids in its query, the job's), or its link going on from the job's address or naming the job's
    id (Workday's ".../<title>_<req id>/apply"). Not on any other page with one draft's button on it (a candidate home,
    a sidebar): whose it is isn't known there."""
    if not job_url:
        return False
    page, href = str(data.get("url") or ""), str(action.get("href") or "")

    def steady(url: str) -> str:
        return _bare(url).rstrip("/").lower()
    if steady(page) == steady(job_url) and _job_ids(page) == _job_ids(job_url):
        return True
    if not href:
        return False
    if steady(href) == steady(job_url) or steady(href).startswith(steady(job_url) + "/"):
        return True
    ids = set(_job_ids(job_url).values())
    if m := _REQ_IN_PATH.search(urlparse(job_url).path):
        ids.add(m.group(1))
    # (an id of 4 characters or more, as a whole part of the link: one of its path's parts, or what follows a part's last
    # "_" (Workday's "<title>_<req id>"), or a query value; a "2" from an address ending "_2" is no job's id, and a
    # link's "?step=2" isn't it: the review)
    ids = {i.lower() for i in ids if len(i) >= 4}
    parts = urlparse(href)
    segments = [s.lower() for s in parts.path.split("/") if s]
    pieces = set(segments) | {s.rsplit("_", 1)[-1] for s in segments} | {v.lower() for _, v in parse_qsl(parts.query)}
    return bool(ids & pieces)


def _nothing_drawn(data: dict[str, Any], text: str) -> bool:
    """A page with nothing drawn on it: no headings, boxes (but its own language picker) or buttons (Eightfold's apply
    page, issue #112, note 40: its app shell, and no form), and nothing in its words that says why. Never a bot check
    or a CAPTCHA (the person's), nor a page whose words are its own message: a posting closed, an error the page shows
    or the site's own error page, a confirmation, an application sent before."""
    if _app_fields(data) or data.get("actions") or data.get("headings") or data.get("captcha") \
            or data.get("challenge") or data.get("empty_frame") or any(_errorish(e) for e in data.get("errors") or []):
        return False
    said = (text or "")[:3000]
    return not (_BOT_BADGE.search(said) or _BOT_TEXT.search(said) or _ROBOT.search(said) or _closed_notice(data, said)
                or _maintenance_page(data) or _ERROR_PAGE.search(said) or _SPENT_PAGE.search(said)
                or _REFRESH_ASKED.search(said) or _CONTENT_MISSING.search(said) or confirmations(said)
                or _applied_before(data, said))


async def _load_again(page: Any) -> None:
    """Load a page again by its address: a fresh GET, so nothing a form's POST sent is sent again, as a reload of the
    page that POST answered would. One whose address has a fragment (a single-page app's route, which a visit to the
    same address wouldn't load again) is reloaded."""
    with contextlib.suppress(Exception):
        if urlsplit(page.url).fragment:
            await page.reload(wait_until="load")
        else:
            await page.goto(page.url, wait_until="load")


def _maintenance_page(data: dict[str, Any]) -> bool:
    """A site down for maintenance: the job sent on to a page named for it (Workday's
    community.workday.com/maintenance-page, asml.com/en/maintenance's "We'll be back.", live, Oct 2026)."""
    return bool(re.fullmatch(r"maintenance(?:-page)?|outage", urlparse(data.get("url") or "").path.rstrip("/")
                             .rsplit("/", 1)[-1], re.I))


def _sent_already(data: dict[str, Any], text: str, moved: bool) -> str:
    """The page's own words that this application has gone (a confirmation, on a page the tab `moved` on to, not a
    step of the form that thanks the person; or the job's page saying it was applied to), or "" when it doesn't."""
    if moved and (gone := sorted(confirmations(text))) and not _mid_application(data, text):
        return gone[0]
    return _applied_before(data, text)


# A block's own Delete, and nothing else's ("Delete Military Experience 2", "Delete Job Reference 1" aren't a job's)
_BLOCK_KINDS = {"work": r"(?:work|employment|professional)\s+(?:experience|history)|employment|experience",
                "education": r"education(?:al)?(?:\s+(?:history|background))?"}


_TEXTLIKE_KINDS = {"text", "textarea", "select", "combobox", "listbox"}  # (a box whose value says who filled it)


def _block_key(name: str) -> str:
    """A block's name as words alone ("WORK EXPERIENCE #2" is "work experience 2"), as the form reader's ENTRIES_JS keys
    them."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _own_delete(deletes: list[dict[str, Any]], block: str) -> dict[str, Any] | None:
    """A block's own Delete: one that names the block, else the only Delete in it that names nothing else (not a file's
    or a pill's)."""
    own = [a for a in deletes if a.get("section") == block]
    bare = [a for a in own if not a.get("names_other")]
    return next((a for a in own if a.get("names_block")), bare[0] if len(own) == 1 and bare else None)


def _blocks_by_delete(data: dict[str, Any], section: str) -> int:
    """How many of a section's blocks the page shows, by their own numbered Delete ("Delete Education 2"), or Edit of a
    saved one (UKG Pro's "Edit Education Item 1": Nikon, UCT and Desert Financial, run 49)."""
    kind = re.compile(rf"^(?:delete|remove|edit)\s+(?:{_BLOCK_KINDS.get(section, section)})\s+(?:item\s+)?(\d+)\s*$", re.I)
    return len({m.group(1) for a in data.get("actions") or [] if (m := kind.match(final_text(a.get("text") or "")))})


def _applied_before(data: dict[str, Any], text: str) -> str:
    """The page's own words that this job was applied to already, or "" when it doesn't say so. A
    question on the form that asks it ("Have you already applied for this position?") isn't that."""
    labels = " ".join(str(f.get("label") or "") for f in data.get("fields") or []).lower()
    for said in list(data.get("errors") or []) + [text or ""]:
        said = re.sub(r"[ \t\xa0]+", " ", said.replace("\u2019", "'"))
        for m in _APPLIED_BEFORE.finditer(said):
            begun = max(said.rfind(c, 0, m.start()) for c in ".!?")
            line = said.rfind("\n", 0, m.start())
            if line > begun and not _WRAPPED.search(said[:line]):
                begun = line  # a line of its own (under a heading), not one sentence wrapped onto the next
            if m.group(0).lower() in labels or not re.fullmatch(r"[\s\W]*", said[begun + 1:m.start()]):
                continue  # a question on the form, or a sentence that only supposes it ("If you have already…")
            start, end = said.rfind(".", 0, m.start()) + 1, said.find(".", m.end())
            sentence = " ".join(said[start:end + 1 if end >= 0 else len(said)].split())[:200]
            return sentence
    return ""


def _closed_notice(data: dict[str, Any], text: str) -> str:
    """The page's own words that its posting has closed, or "" when it doesn't say so: the sentence that says it, within
    the element that says it (the page's text puts each element of its own on a line of its own). Never the page's
    header and menus before it: SuccessFactors' "Sorry, this position has been filled.", a paragraph of its own, was
    quoted with the 200 characters of the site's header and navigation above it, to the first comma (issue #112, note
    39)."""
    for said in list(data.get("errors") or []) + [text or ""]:
        for m in _CLOSED.finditer(said):
            line = said.rfind("\n", 0, m.start()) + 1
            line_end = said.find("\n", m.end())
            line_end = len(said) if line_end < 0 else line_end
            start, end = max(said.rfind(".", 0, m.start()) + 1, line), said.find(".", m.end(), line_end)
            before = said[start:m.start()]
            if _CLOSED_IF.search(said[start:m.end()]) or _CLOSED_HOW.match(said[m.end():line_end]) \
                    or before.count("(") > before.count(")"):
                continue  # (a condition, how a posting is filled, what a board is closed for, or an aside about another)
            return " ".join(said[start:end + 1 if end >= 0 else line_end].split())[:200]
    return ""


def _blocks(section: str, have: int, wanted: int) -> str:
    """"1 of your 3 jobs", "0 of your 1 school": a section's blocks on the page against the profile's entries."""
    what = "job" if section == "work" else "school"
    return f"{have} of your {wanted} {what}{'' if wanted == 1 else 's'}"


def _short_said(site: str, short: dict[str, tuple[int, int]]) -> str:
    """The card for sections left short by a covered Add button: what is there, and what to do."""
    names = " and ".join(_blocks(s, *n) for s, n in short.items())
    return (f"{site} shows only {names}: the Add button is covered by something on the page, so the rest weren't "
            "added. Press Add in the browser until all are there (the desk fills them in), or close what covers it, "
            "then press Resume. A Resume with nothing added goes on without them.")


def _site(run: Run, data: dict[str, Any]) -> str:
    ats = detect_ats(data.get("url") or "")
    if ats in ("company_site", ""):
        return f"{run.company}'s site" if run.company else "the site"
    return ATS_NAMES.get(ats, ats)


def _sent_from(found: mailbox.Found) -> str:
    """Whose email a code or link came from, for the log (and where it was, when not the inbox)."""
    return f"sent from {found.sender}" + (", in your Spam folder" if found.spam else "")


# Never an employer's mail domain, whatever a list says: a mail service's or a big shared site's, where anyone
# may have an address (the review of 0.3.137)
_PUBLIC_MAIL = {*mailbox.IMAP_HOSTS, "outlook.com", "hotmail.com", "live.com", "msn.com", "proton.me", "protonmail.com",
                "pm.me", "protonmail.ch", "gmx.com", "gmx.net", "mail.com", "zoho.com", "fastmail.com", "yandex.com",
                "google.com", "microsoft.com", "apple.com", "amazon.com",
                "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com"}


def _employer_mail_domains(company: str, hosts: set[str], own: str = "") -> set[str]:
    """The domains an employer on the person's lists mails from besides its job system's: its own site's (its
    careers_url, where that isn't a job system's) and any its entry names (`mail_domains`). Oracle Recruiting mails
    its code from the employer's own (careers.bdo.com, recruitment.americanexpress.com, us.gt.com: run 49, where each
    waited out a code the inbox had). The entry is the job's employer by name, or by searching the job's own site
    where no other employer on the lists searches it too (Nikon's, UCT's and Desert Financial's UKG Pro is one
    site: the review). Never a mail service's domain, nor the person's own address's (`own`)."""
    def named(value: Any) -> list[str]:  # the addresses an entry names, its search's too
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [s for v in value.values() for s in named(v)]
        return [s for v in value for s in named(v)] if isinstance(value, list) else []

    def host(s: str) -> str:
        try:
            return (urlparse(s if "//" in s else f"https://{s}").hostname or "").lower()
        except ValueError:  # (a search's words, "Test [x", read as an address)
            return ""

    try:  # (an entry of the person's written oddly: the job site's and its system's domains still)
        entries = [(c, {host(s) for s in named([c.get("careers_url"), c.get("search")])} - {""})
                   for c in every_employer()]
        claimed: dict[str, set[str]] = {}
        for c, theirs in entries:
            for h in theirs:
                claimed.setdefault(h, set()).add(norm(c.get("name") or ""))
        sites = {h for h in hosts if len(claimed.get(h, ())) == 1}
        out: set[str] = set()
        for c, theirs in entries:
            if norm(c.get("name") or "") != norm(company) and not sites & theirs:
                continue
            careers = c.get("careers_url")
            if isinstance(careers, str) and shared_system(careers) is None:
                out.add(mailbox.site_domain(host(careers)))
            listed = c.get("mail_domains")
            out |= {d.strip().lower() for d in listed if isinstance(d, str)} if isinstance(listed, list) else set()
    except Exception:
        return set()
    never = _PUBLIC_MAIL | ({own} if own else set())
    return {d for d in out if "." in d and not any(d == p or d.endswith("." + p) for p in never)}


def _another_employers_mail(company: str, own: frozenset[str]) -> Callable[[str, str], bool] | None:
    """Whether a code's mail is another job's by its subject: mail from a job system's shared address (not the
    employer's own domains, `own`) whose subject names another employer on the person's lists and not this one.
    Oracle mails every employer's code from one address, "onsemi Confirm Your Identity" (run 54: onsemi's code came
    after onsemi's wait ended, and American Express, waiting a minute later, put it in). None when there's nothing
    to tell by. Employers whose names begin with the same two words (Grand Canyon Education and Grand Canyon
    University) aren't told apart this way."""
    def names(name: str) -> set[str]:  # "Tokyo Electron (TEL)": the name, and the one in brackets
        return {n for n in (norm(x) for x in (re.sub(r"\(.*?\)", " ", name), *re.findall(r"\((.*?)\)", name)))
                if len(n) >= 3}

    try:
        mine = names(company)
        theirs = {n for c in every_employer() for n in names(str(c.get("name") or ""))} - mine
    except Exception:  # (an entry of the person's written oddly: told by sender alone, as before)
        return None
    # (not told apart where their names begin with the same two words, or one is the other's first word: Grand
    # Canyon Education's mail may name Grand Canyon University; American Express and American Airlines are)
    def lead(name: str) -> str:
        return " ".join(name.split()[:2])

    theirs = {t for t in theirs if lead(t) not in {lead(m) for m in mine} and not any(t in m or m in t for m in mine)}
    if not mine or not theirs:
        return None

    def others(domain: str, subject: str) -> bool:
        if any(domain == d or domain.endswith("." + d) for d in own):
            return False
        said = f" {norm(subject)} "
        return any(f" {t} " in said for t in theirs) and not any(f" {m} " in said for m in mine)
    return others


# A box whose answer never goes on the "What went in" panel, by its words (each of its label, section, sub-label and
# name): a password's, an emailed code's (a bare "Code" too, never a ZIP or area code), a security question's or its
# answer's (_SECURITY_QUESTION's too), as none is ever logged, saved or reported either
_KEPT_OUT = re.compile(r"\bpass ?(?:word|code|phrase)|\bpin\b|security (?:question|answer)|secret (?:question|answer|word)|"
                       r"\bcreate (?:a |your )?secret\b|^\W*secret\W*$|mother'?s maiden name|verification code|"
                       r"one[- ]time (?:pass|code)|\botp\b|captcha|memorable (?:word|answer)|"
                       r"^\W*(?:enter (?:the |your )?)?(?:(?:access|auth(?:entication)?|confirmation|e-?mail|security|sms|"
                       r"text|verification) )?code\W*$", re.I)
_SECRETS_READ: dict[str, Any] = {}  # secrets.yaml's values, as last read, and when it was written then


def _secret_values() -> set[str]:
    """The secrets the desk keeps (each job system's password, the inbox's app password), from where get_secret reads
    them: the panel never shows one, whatever box it went into. The file is read again only once it has changed."""
    found = {v for k, v in os.environ.items() if k.startswith("JOB_APPLY_SECRET_") and v.strip()}
    with contextlib.suppress(Exception):  # (a file it can't read holds none it could show)
        if (path := config.secrets_path()).exists():
            stat = path.stat()
            stamp = (str(path), stat.st_mtime_ns, stat.st_size)
            if _SECRETS_READ.get("stamp") != stamp:
                _SECRETS_READ.update(stamp=stamp, values=frozenset(v for v in config.read_secrets(path).values()
                                                                   if v.strip()))
            found |= _SECRETS_READ["values"]
    return found


def _holds_secret(said: str, secrets: set[str]) -> bool:
    """A value that is one of the desk's secrets, or holds one that is no ordinary word (8 characters or more, not
    letters alone): a password "Arizona" doesn't take every answer naming the state off the panel."""
    said = said.strip()
    return any(s.strip() == said or len(s) >= 8 and not s.isalpha() and s in said for s in secrets)


def _kept_out(box: dict[str, Any], value: Any, secrets: set[str]) -> bool:
    """Never on the panel: a password box's value (anything typed into one), an emailed code's, a security question's
    or its answer's (by its words; _guarded's by the form's heading too), or a value that is (or holds) one of the
    desk's secrets."""
    if box.get("kind") == "password" or box.get("account") or _code_box(box):
        return True
    words = [_no_clearance(box.get(k)) for k in ("label", "section", "sublabel", "name")]
    if any(_KEPT_OUT.search(w) or _SECURITY_QUESTION.search(w) for w in words):
        return True
    return _holds_secret(", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else str(value), secrets)


def _no_clearance(text: Any) -> str:
    """Words as the panel reads them for a security question: without a security clearance's ("Security Clearance
    Questions" over Northrop's, Boeing's and Honeywell's clearance questions is no account's security question)."""
    return re.sub(r"\bsecurity\s+clearances?\b", "clearance", str(text or ""), flags=re.I)


def _guarded(pages: tuple[dict[str, Any], ...]) -> set[str]:
    """The boxes (by id) of a page's security questions and their answers, named so by the boxes or by the form's
    heading alone (BrassRing's "Security questions" over "Question 1 Answer"): never on the panel. Never a security
    clearance's questions (_no_clearance)."""
    out: set[str] = set()
    for data in pages:
        fields = [{**f, "label": _no_clearance(f.get("label"))} for f in data.get("fields") or [] if isinstance(f, dict)]
        headings = [_no_clearance(h) for h in data.get("headings") or []]
        out |= {str(f.get("id")) for f in _security_boxes(fields, headings)}
    return out


def _page_name(data: dict[str, Any]) -> str:
    """A page as the panel names it: _where's, without its quotes, from its headings less those saying what's wrong
    (drawn over the same step once it's pressed: "Errors Found", "There is a problem with your answers") and those its
    boxes are under (a one-page form's "Education" isn't the page its First Name is on)."""
    sections = {norm(f.get("section")) for f in data.get("fields") or [] if isinstance(f, dict) and f.get("section")}
    headings = [h for h in data.get("headings") or [] if isinstance(h, str) and not _ERRORS_HEADING.match(h.strip())
                and not _errorish(h) and norm(h) not in sections]
    return _where({**data, "headings": headings}).strip("“”")


def _went_bases(data: dict[str, Any], boxes: list[dict[str, Any]]) -> dict[str, str]:
    """Each box's place on the "What went in" panel when it's new (WentIn.key_for keeps its first), by its id: its
    page's address (never its headings, which an error or a block drawn since changes), its section, label and
    sub-label (placed_key), and which of the boxes so placed it is (Workday's Month, Day and Year boxes are each a
    "Date")."""
    url = _steady_url(data.get("url"))
    seen: dict[str, int] = {}
    out: dict[str, str] = {}
    for f in boxes:
        placed = placed_key(f)
        seen[placed] = seen.get(placed, 0) + 1
        out.setdefault(str(f.get("id")), f"{url} | {placed} | {seen[placed]}")
    return out


@_quietly
def _went_forget(run: Run, data: dict[str, Any], section: str) -> None:
    """A block deleted from the page (a school's): what went into its boxes, and what was left empty in them, off the
    panel (by their places as kept, and as an earlier load of the page would have kept them)."""
    boxes = [f for f in data.get("fields") or [] if isinstance(f, dict)]
    bases, url, page = _went_bases(data, boxes), _steady_url(data.get("url")), question_key(_page_name(data))
    ids = [str(f.get("id")) for f in boxes if f.get("section") == section]
    places = run.went_in.places
    run.went_in.forget({places[f"{url} {i}"] for i in ids if f"{url} {i}" in places}
                       | {bases[i] for i in ids} | {f"{bases[i]} | {page}" for i in ids})


def _page_boxes(pages: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """The boxes of a page read before a fill and after it (blocks added since), each once, by id, in page order."""
    boxes: dict[str, dict[str, Any]] = {}
    for data in pages:
        for f in data.get("fields") or []:
            if isinstance(f, dict) and f.get("id") is not None:
                boxes.setdefault(str(f["id"]), f)
    return list(boxes.values())


# The kinds of box whose answer is one of the page's own choices (a WentIn answer's kind "choice")
_CHOSEN = {"select", "radio_group", "checkbox_group", "checkbox", "listbox"}


def _went_value(box: dict[str, Any], value: Any, rule: str) -> tuple[str, str]:
    """An answer as the panel shows it: ("file", its file's name) for a document put up, else ("answer", the words),
    a tick said in words, a long text's start alone."""
    if isinstance(value, str) and (box.get("kind") == "file" or rule.startswith("documents.")):
        return "file", re.split(r"[\\/]", value.strip())[-1]
    if isinstance(value, bool):
        return "answer", "ticked" if value else "not ticked"
    said = ", ".join(str(v) for v in value) if isinstance(value, (list, tuple)) else "" if value is None else str(value)
    said = " ".join(said.split())
    return "answer", said if len(said) <= WENT_IN_CHARS else said[:WENT_IN_CHARS - 1].rstrip() + "…"


def _box_said(box: dict[str, Any]) -> dict[str, str]:
    """A box as the panel names it: its label in a few words, a question's mark kept (with its sub-label), its
    section."""
    label = _short(box.get("label") or "") or "(a box with no label)"
    if not label.endswith(("?", "…")) and re.sub(r"[\s*:]+$", "", box.get("label") or "").endswith("?"):
        label += "?"
    if box.get("sublabel"):
        label += f" ({_short(str(box['sublabel']))})"
    return {"section": _short(str(box.get("section") or "")), "label": label}


_DOCUMENTS = {"documents.resume": "Resume", "documents.cover_letter": "Cover letter"}  # the profile's, by autofill's rule
_DOCUMENT_KINDS = {"documents.resume": "resume", "documents.cover_letter": "cover_letter"}  # (Run.kept_replaced's)
_DOCUMENT_SAID = {"resume": "resume", "cover_letter": "cover letter"}


def _read_back(box: dict[str, Any] | None, secrets: set[str]) -> Any:
    """What a box shows once filled, where that says more than what was sent (the choice a list made of the profile's
    words, a phone as the site writes it): None for nothing there, a site's placeholder, a secret's "(set)", a file
    (its name is the desk's own), and anything kept off the panel."""
    if box is None or box.get("kind") in ("file", "password"):
        return None
    value = box.get("value")
    if is_empty_value(value) or unset_choice(box) or str(value).strip() in ("(set)", "(unconfirmed)", "uploaded") \
            or _kept_out(box, value, secrets):
        return None
    return value


@_quietly
def _went_in(run: Run, filled: list[dict[str, Any]], *pages: dict[str, Any], shown: dict[str, Any] | None = None) -> None:
    """What a fill put into the page (autofill's `filled`, or the person's answers that went in), kept for the job's
    "What went in" panel: each box's answer by its place (the page is the first of `pages`, read before the fill; any
    later read adds the boxes drawn since, such as blocks added), its last one standing, as the page shows it after the
    fill where `shown` (that read) has it. Never a password, a code or a security answer (_kept_out, _guarded).
    Nothing here goes into the log, and nothing going wrong here stops the job."""
    if not filled or not pages:
        return
    page, url = _page_name(pages[0]), _steady_url(pages[0].get("url"))
    boxes = _page_boxes(pages)
    by_id, bases, secrets, guarded = {str(f["id"]): f for f in boxes}, _went_bases(pages[0], boxes), _secret_values(), \
        _guarded(pages)
    after = {str(f.get("id")): f for f in (shown or {}).get("fields") or [] if isinstance(f, dict)}
    for f in filled:
        fid, value = str(f.get("id")), f.get("value")
        box = by_id.get(fid) or {"id": fid, "label": f.get("label") or ""}
        if value is None or value == "" or fid in guarded or _kept_out(box, value, secrets):
            continue
        rule = str(f.get("from") or f.get("rule") or "")
        kind, said = _went_value(box, value, rule)
        if kind != "file" and (back := _read_back(after.get(fid), secrets)) is not None:
            kind, said = _went_value(after[fid], back, "")
        if not said:
            continue
        if kind == "answer" and (str(box.get("kind") or "") in _CHOSEN or said in {
                o for o in box.get("options") or [] if isinstance(o, str)}):
            kind = "choice"  # (one of a list's own choices: nothing typed, and no report keeps it out: _report_hidden)
        named = _box_said(box)
        if kind == "file":  # (said as the document it is: an upload's own words can be "Accepted file format: .pdf")
            named["label"] = _DOCUMENTS.get(rule) or named["label"]
        key = run.went_in.key_for(url, fid, bases.get(fid) or f"{url} | {placed_key(box)} | 1", page)
        run.went_in.put(key, {"page": page, **named, "value": said, "kind": kind})


WENT_CHANGED_CHARS = 60  # of each side of a draft's answer changed, on the panel ("<label>: <old> → <new>")


@_quietly
def _went_changed(run: Run, changes: list[tuple[dict[str, Any], Any, Any]], page: dict[str, Any]) -> None:
    """A saved draft's answers the desk changed to the profile's (Applier._autofill), each as "<label>: <old> → <new>",
    for the job's "What went in" panel alone (never the log, a note or a report: the draft's old answer is no detail of
    the profile's that a scrubber would know). Never a password, a code or a security answer (_kept_out, _guarded), nor a
    value that holds one of the desk's secrets, on either side."""
    secrets, guarded = _secret_values(), _guarded((page,))

    def said(box: dict[str, Any], value: Any) -> str:
        _, words = _went_value(box, value, "")
        words = words or "(empty)"
        return words if len(words) <= WENT_CHANGED_CHARS else words[:WENT_CHANGED_CHARS - 1].rstrip() + "…"
    for box, old, new in changes:
        if str(box.get("id")) in guarded or _kept_out(box, old if old is not None else "", secrets) \
                or _kept_out(box, new if new is not None else "", secrets):
            continue
        named = _box_said(box)
        label = f"{named['section']}: {named['label']}" if named["section"] else named["label"]
        run.went_in.change(f"{label}: {said(box, old)} → {said(box, new)}")


# A saved draft's answers the desk changed, kept in the job's own folder (beside submission.json) so a restart of the desk
# still holds the job from Submit for me and shows them on its card: never in the log, a note, a report or a saved page
DRAFT_FILE = "draft-changes.json"
# said on the card of a job whose kept changes won't read back (cut short, or changed by hand): it's held all the same
DRAFT_UNREAD = "Earlier changes the desk made to this saved draft couldn't be read back: check its answers on the review page"


def _draft_file(srv: Any, job_id: int) -> Path | None:
    job = srv.tracker().get(job_id, with_description=False) or {}
    return Path(job["folder"]) / DRAFT_FILE if job.get("folder") else None


def _save_draft_changes(srv: Any, run: Run) -> None:
    """Keep that the desk changed this job's draft, and the lines its card shows for it (_went_changed's, a secret
    never among them). Whatever goes wrong writing it, the job goes on (held for this run of the desk all the same)."""
    try:
        path = _draft_file(srv, run.job_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # (whole or not at all: a file cut short by a crash would let the job go unheld: the final review)
        config.write_text_atomic(path, json.dumps({"draft_changed": True, "changed": list(run.went_in.changed),
                                                   **({"draft_off": True} if run.draft_off else {})}))
    except Exception:
        logging.getLogger(__name__).warning("couldn't keep job %s's draft changes", run.job_id, exc_info=True)


def _keep_draft_off(srv: Any, job_id: int) -> None:
    """Keep that this job's draft is the person's now (Run.draft_off), beside what the desk changed in it. One that
    won't read is left as it is (it holds the job all the same: _restore_draft_changes)."""
    try:
        path = _draft_file(srv, job_id)
        if path is None:
            return
        kept: Any = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(kept, dict) or kept.get("draft_off") is True:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        config.write_text_atomic(path, json.dumps({**kept, "draft_off": True}))
    except Exception:
        logging.getLogger(__name__).warning("couldn't keep that job %s's draft is yours now", job_id, exc_info=True)


def _restore_draft_changes(srv: Any, run: Run) -> None:
    """A job the desk changed the draft of before it was restarted: held from Submit for me again, and its card's lines
    back (none holding a secret saved since). One gone in since (applied, or past that) has none: they're forgotten."""
    try:
        path = _draft_file(srv, run.job_id)
        if path is None or not path.is_file():
            return
        job = srv.tracker().get(run.job_id, with_description=False) or {}
        if job.get("status") in FINISHED:
            path.unlink(missing_ok=True)
            return
    except Exception:
        logging.getLogger(__name__).warning("couldn't look for job %s's draft changes", run.job_id, exc_info=True)
        return
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(kept, dict):
            raise ValueError("not an object")
    except Exception:
        # there, but it won't read (cut short, changed by hand): the job is held all the same, and the card says why
        logging.getLogger(__name__).warning("couldn't read job %s's draft changes", run.job_id, exc_info=True)
        run.draft_changed = True
        run.went_in.change(DRAFT_UNREAD)
        return
    if kept.get("draft_off") is True:  # (taken over by the person before a restart: theirs still, never re-checked)
        run.draft_off = True
    if not kept.get("draft_changed"):
        return
    run.draft_changed = True
    try:
        secrets = _secret_values()
        for line in kept.get("changed") or []:
            if isinstance(line, str) and not _holds_secret(line, secrets):
                run.went_in.change(line)
    except Exception:
        logging.getLogger(__name__).warning("couldn't read job %s's draft changes", run.job_id, exc_info=True)


def _forget_draft_changes(srv: Any, job_id: int) -> None:
    with contextlib.suppress(Exception):
        if (path := _draft_file(srv, job_id)) is not None:
            path.unlink(missing_ok=True)


@_quietly
def _went_left(run: Run, result: dict[str, Any], *pages: dict[str, Any]) -> None:
    """The optional questions autofill left empty on its page (the profile doesn't answer them, none of their choices is
    its answer, or the box wouldn't take it), for the panel's "Left empty": the page's as its latest fill left them."""
    if not pages:
        return
    page, url = _page_name(pages[0]), _steady_url(pages[0].get("url"))
    secrets, guarded, left = _secret_values(), _guarded(pages), {}
    bases = _went_bases(pages[0], _page_boxes(pages))
    now = {str(f.get("id")): f for f in pages[-1].get("fields") or [] if isinstance(f, dict)}
    for q in [*(result.get("needs_input") or []), *(result.get("failed") or [])]:
        qid = str(q.get("id"))
        if q.get("required", True) or qid in guarded or _kept_out(q, "", secrets):
            continue
        shown = now.get(qid)
        if shown is not None and not is_empty_value(shown.get("value")) and not unset_choice(shown):
            continue  # (the site has answered it since: Oracle's County from the ZIP)
        why = (f"your profile's answer “{q['unmatched']}” isn't one of its choices" if q.get("unmatched")
               else "it wouldn't take your profile's answer" if q.get("error") else "your profile doesn't answer it")
        _, why = _went_value({}, why, "")
        key = run.went_in.key_for(url, qid, bases.get(qid) or f"{url} | {placed_key(q)} | 1", page)
        left[key] = {"page": page, **_box_said(q), "why": why}
    places = run.went_in.places
    run.went_in.leave({places[f"{url} {i}"] for i in bases if f"{url} {i}" in places}, left)


def question_key(label: str) -> str:
    return norm(clean_label(label))


def placed_key(field: dict[str, Any]) -> str:
    """The key of an answer given for one box: the desk sends "section | label | sub-label"."""
    return question_key(f"{field.get('section') or ''} | {field.get('label') or ''} | {field.get('sublabel') or ''}")


def _lost_answers(run: Run, data: dict[str, Any], fields: list[dict[str, Any]]) -> dict[str, Any]:
    """The person's answers for this application to these boxes that the page shows but says are missing, by id:
    keyed as _fill_once keys them (a box's own answer, else its label's), only on the page each first went in on, and
    only where the box still shows that answer, as a number (a date's part): never another answer in its place, nor
    over what the person has typed in the browser since (the review of 0.3.159)."""
    here = _answers_page(data)
    out = {}
    for f in fields:
        key = placed if (placed := placed_key(f)) in run.once else question_key(f.get("label") or "")
        if key not in run.once or run.once_page.get(key, here) != here:
            continue
        shown, given = re.sub(r"\D", "", str(f.get("value") or "")), re.sub(r"\D", "", str(run.once[key] or ""))
        if shown and given and int(shown) == int(given):
            out[str(f["id"])] = run.once[key]
    return out


def _page_said(data: dict[str, Any]) -> str:
    """A page in a few words: its heading, and what it says if it shows a message."""
    message = next((e for e in data.get("errors") or [] if len(e) > 12), "")
    return _where(data) + (f" (\u201c{message[:160]}\u201d)" if message else "")


# Workday's progress bar names the step it is on: "current step 4 of 6 Voluntary Disclosures"
_STEP_NAME = re.compile(r"current step \d+ of \d+\s+(\S.*)", re.I)
# Headings that name no page: an error banner, the site's footer and menus
_NOT_A_PAGE = re.compile(r"errors? found|\d+ errors?.*|follow us|contact us|share.*|menu|skip to.*", re.I)


def _names_a_page(heading: str) -> bool:
    """Whether a heading could be the page's name: not a progress bar's, nor a banner's or a footer's, nor a
    sentence (a checkbox's legend: "I agree that I am 18 years of age or older and")."""
    words = heading.lower().split()
    return bool(words) and not (heading.lower().startswith("current step") or _NOT_A_PAGE.fullmatch(heading)
                                or len(words) > 8 or words[-1] in ("and", "or", "of"))


def _where(data: dict[str, Any]) -> str:
    """A page named for the log: its step's name, else its last heading that names a page (the site's
    own name comes first), else its title less the site's name (notes 23 and 37: the last heading was
    a footer's "follow us" or a checkbox's legend, and a Workday step's own name was thrown away)."""
    headings = [h.strip() for h in data.get("headings") or [] if h and h.strip()]
    name = next((m[1] for h in headings if (m := _STEP_NAME.match(h))), None) \
        or next((h for h in reversed(headings) if _names_a_page(h)), None) \
        or re.split(r"\s+\|\s+", str(data.get("title") or "").strip())[0]
    return f"“{name[:60]}”" if name else "this page"


def _short(label: str) -> str:
    """A question in a few words for the log: Micron's veteran question carries the whole
    VEVRAA notice in its label."""
    text = clean_label(label)
    return text if len(text) <= 80 else text[:78].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
