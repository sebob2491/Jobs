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
                       polarity, tailored_document, works_there_now)
from .browser import (_SENDS_TOO, CONFIRMATION_RE, SubmitBlocked, TabClosed, _accepts_cookies, _cookie_setting,
                      confirmations, declines_cookies, final_text, may_accept_cookies)

NEW_TAB_WAIT = 4  # seconds to wait for a tab opened late by a click before calling it a stall
ONCE_SETTLE = 1.0  # seconds after filling the person's answers before checking they stayed in
MAX_STEPS = 15
LATE_BUTTONS_WAIT = 10  # seconds for a page's buttons to be drawn
STEP_LOAD_WAIT = 30  # for a step still loading its questions, its loading dots on show (KLA's Workday)
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
ACCOUNT_STEP_WAIT = 10  # for an email-first site's next step of making an account to draw (Eightfold's)
# A site locks an account after a few refused sign-ins: a job's sign-in is pressed with the same saved
# password this many times at most (once more after a password reset or an account the desk made, and
# after each Resume the person presses on a sign-in card)
SIGN_IN_TRIES = 2
NOTICE_WAIT = 5  # seconds for a notice agreed to for the person to go (one may fade out)
# A job that went in keeps its tab (its confirmation, for the person to see) while it's among the newest
# this many: one tab per job adds up over a long queue
DONE_TABS_KEPT = 3
SHARED_LOOK_BACK = 30  # seconds looked back for a job's code while an earlier job waits on the same sender
FINISHED = {"applied", "interviewing", "offer", "rejected", "withdrawn"}  # tracker statuses never applied to again
UNSKIP_WAIT = 10  # seconds an Undo waits for the worker to finish the step a job was skipped in

_BOT_TITLE = re.compile(r"just a moment|attention required|access denied|pardon our interruption|security check|"
                        r"are you a robot|bot (?:check|detection)", re.I)
# A bare "403 Forbidden" (Valleywise Health's postings, to the desk's browser) or "406 Not Acceptable"
# (Deloitte's sign-in, to a headless browser): the site turns the browser away. There's nothing to
# solve, so it holds nothing up: the person applies elsewhere
_TURNED_AWAY = re.compile(r"^\s*(?:403\s*)?forbidden\s*$|^\s*(?:406\s*)?not acceptable\s*$", re.I)
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
# The button pressed once the emailed code is in; one labelled Submit is left to the person (but on
# the code step of an account the desk is making, with nothing else on it: _account_code_button)
_AFTER_CODE = re.compile(r"^(verify|confirm|continue|next)( (code|e-?mail|account|my e-?mail))?$", re.I)
# With settings.manage_accounts, the buttons of an email-first site's steps of making an account (an
# Eightfold site's: its agreement's "Submit", the email's "Continue", "Use a one-time code" in place of
# a password, and the code's "Submit")
_ACCOUNT_ON = re.compile(r"^(submit|continue|next|agree|i agree|accept|create(?: an| my| a new)? account)$", re.I)
_ONE_TIME_CODE = re.compile(r"^(?:use an? )?(?:one[- ]time (?:pass)?code|e-?mail (?:me )?an? code)(?: instead)?$", re.I)
_CODE_SENDS = re.compile(r"^submit$", re.I)
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
_TERMS_ONLY = re.compile(r"terms|conditions|(?:privacy|data protection) (?:policy|notice|statement)", re.I)
_TERMS_BOX = re.compile(r"terms|conditions|privacy|consent|agree|acknowledge|policy|notice", re.I)
_NOT_TERMS = re.compile(r"newsletter|marketing|job alerts?|text messages?|\bsms\b|promotion|offers|subscribe|similar jobs|"
                        r"talent (?:community|network)|keep me|stay informed|send me|contact(?:ed)? me|be contacted|"
                        r"share my|other (?:roles|positions|jobs|opportunities)|affiliat", re.I)
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
_SOCIAL = re.compile(r"\b(google|apple|linked ?in|facebook|microsoft|indeed|seek)\b", re.I)
_STEP = re.compile(r"^(save (?:and|&) continue|continue|next|next step|review|review (?:and|&) submit|"
                   r"review application|proceed|go to next step|start)$", re.I)
_FORWARD = re.compile(r"^(save (?:and|&) continue|continue|next|next step|review|review application|proceed|"
                      r"go to next step)$", re.I)
_STEP_OF = re.compile(r"\bstep (\d+) of (\d+)\b", re.I)  # a progress bar's place: "current step 3 of 6"
# A sign-up for job alerts, a newsletter or a talent community (the form reader's SIDE_BOX), beside an
# application or on the page that thanks the person for one: its boxes and button are never the application's
_SIDE_BOX = re.compile(r"job alerts?|alerts? by e-?mail|e-?mail alerts?|newsletter|\bsubscribe\b|talent (?:community|"
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
_ATTEST_KINDS = {"select", "listbox", "combobox", "checkbox", "checkbox_group"}
# and, the owner's call (Oct 10), an employer's privacy notice and terms of use: a gate's "I Accept"
# (Kforce's Taleo privacy agreement), "I Acknowledge the Privacy Notice" (Schwab's iCIMS), a dialog,
# or a required box ("I have read and agree to the Privacy notice and Terms of use": ASM's;
# "Terms of Use & Data Privacy Statement: Please accept ...": EMD's)
_TERMS = re.compile(r"\bprivacy\b|\bdata protection\b|\bterms (?:of (?:use|service)|and conditions|& conditions)\b|"
                    r"\b(?:candidate|applicant) (?:notice|statement|agreement)\b|\bpersonal (?:data|information)\b|"
                    r"\b(?:gdpr|ccpa)\b", re.I)
_ACCEPTS = re.compile(r"\b(?:agree|accept|acknowledge|consent|have read|understand)\b", re.I)
_TAKES_IN = re.compile(r"^(?:ok|okay|got it|continue|proceed)$", re.I)
# Never agreed to for the person, wherever it's asked: what _NOT_TERMS keeps out of account forms
# (newsletters, marketing, job alerts, a talent community, being contacted, other roles), a talent pool,
# keeping their profile for later, cookies (their own rule), and legal waivers (arbitration, a jury trial)
_NEVER_AGREED = re.compile(_NOT_TERMS.pattern + r"|talent pool|future (?:job |career )?(?:opportunities|openings|roles|"
                           r"positions)|keep (?:my|your) (?:profile|information|data|resume)|retain (?:my|your)|campaigns?|"
                           r"cookie|arbitration|waive|jury|non-?compete|non-?solicit", re.I)
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
# A bot check's badge on a page ("Protected by hCaptcha", reCAPTCHA's): a press it holds is the person's
_BOT_BADGE = re.compile(r"\bh ?captcha\b|\brecaptcha\b", re.I)
# A dialog that mentions personal data but isn't a notice to take in ("could not be saved", "overwrite?")
_NOT_A_NOTICE = re.compile(r"\berror\b|could ?n[o']t|failed|signed? (?:you )?out|overwrite|existing (?:profile|account)|"
                           r"\bdelete|\bremove|withdraw|expired?\b", re.I)

def _agrees_to_terms(text: str) -> bool:
    """Words that accept an employer's privacy notice or terms of use, and nothing more: not a
    newsletter's or the like (_NEVER_AGREED), not a fact about the person, not another question."""
    return bool(_TERMS.search(text) and _ACCEPTS.search(text) and not _NEVER_AGREED.search(text)
                and not _CLAIMS.search(text) and not _OTHER_QUESTION.search(text))


def _terms_notice(heading: str, text: str) -> tuple[bool, bool]:
    """Is a dialog the employer's privacy notice or terms to take in, and may its Ok do it? Named so
    in its heading ("Privacy Policy of Example Corp": its Ok too), or asking in its words to accept
    them; never one about an error, a profile to overwrite or the like, nor what's never agreed to."""
    said = f"{heading} {text}"
    if _NEVER_AGREED.search(said) or _NOT_A_NOTICE.search(said):
        return False, False
    named = bool(_TERMS.search(heading))
    return named or bool(_TERMS.search(text) and _ACCEPTS.search(text)), named
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
    # An email-first sign-in said it has no account for the email here (an Eightfold site's modal): the
    # page (its address, bare) whose next steps make one (_email_account_view), until the application shows
    account_page: str = ""
    account_finished: bool = False  # pressed the Create Account's second step (UKG Pro's "Almost there!"): never again
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
                                                                     "accounts_tried", "account_made", "account_page",
                                                                     "account_finished")}


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
        # where each skipped job was, for Undo: its run's status ("" with no run yet) and its place in the queue
        self._before_skip: dict[int, tuple[str, int | None]] = {}

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
        if job.get("status") == "skipped":  # never applied to until the person's Undo
            raise ValueError(f"{job.get('title') or 'That job'} is skipped. Press Undo on it first.")
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

    def resume(self, job_id: int) -> Run:
        """The person pressed Resume. On a sign-in card, a saved password the site refused gets one more
        press: they may have reset the password to it by hand, as the card asks. One for each such Resume;
        none for a Resume on any other card (a question, a stuck page), nor when the queue carries on with
        the job by itself."""
        run = self.runs.get(job_id)
        signing_in = run is not None and run.need == "sign_in"  # (before enqueue clears it)
        run = self.enqueue(job_id, submit=bool(run and run.submit), front=True)
        if signing_in:
            run.sign_in_tries = min(run.sign_in_tries, SIGN_IN_TRIES - 1)
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
        run = self.runs.get(job_id)
        if run is None or run.status != "skipped":  # (a second Skip keeps where it was before the first)
            place = next((i for i, task in enumerate(self.tasks) if task == ("apply", job_id)), None)
            self._before_skip[job_id] = (run.status if run else "", place)
        run = run or Run(job_id)
        self.runs[job_id] = run
        self._cancel(job_id)
        run.status, run.need, run.blocking, run.reason = "skipped", "", False, "Skipped. Undo puts it back."
        self._log(run, "skipped")
        self.srv.tracker().update(job_id, status="skipped", note="skipped in the Job Desk")
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
        while run is not None and run.status == "skipped" and self.current == job_id:
            if time.monotonic() > deadline:
                raise ValueError("That job is still stopping after its Skip. Press Undo again in a moment.")
            await asyncio.sleep(0.1)
        tracker.unskip(job_id, note="skip undone in the Job Desk")
        before, place = self._before_skip.pop(job_id, (None, None))
        if run is None or run.status != "skipped":
            return run
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

    def _stop_for_what_is_left(self, run: Run, gaps: str, pending: list[dict[str, Any]],
                               missing_files: list[dict[str, Any]]) -> bool:
        """Pause a filled page for what it still needs, if anything: a file the profile doesn't
        point to, entries the profile lacks (gaps), questions it doesn't answer, or boxes the desk
        couldn't fill. The questions come along on each card, so they can be answered meanwhile.
        True when it paused."""
        unfilled, pending = _unfilled(pending)
        couldnt = f" {_couldnt_fill(unfilled)}" if unfilled else ""
        if missing_files:
            self._pause(run, "stuck", "The form needs a file the profile doesn't point to (set documents.resume in "
                        "profile.yaml): " + ", ".join(f["label"] for f in missing_files) + "."
                        + (f" {gaps}" if gaps else "")
                        + (f" It also has {len(pending)} question(s) your profile doesn't answer." if pending else "")
                        + couldnt, pending)
        elif gaps:
            self._pause(run, "stuck", gaps + (f" It also has {len(pending)} other question(s), here." if pending else "")
                        + couldnt, pending)
        elif pending:
            self._pause(run, "questions", f"{len(pending)} question(s) your profile doesn't answer. Answer them here "
                        "and the desk fills them in (and remembers them)." + couldnt, pending)
        elif unfilled:
            self._pause(run, "stuck", f"{couldnt.strip()} Fill {'it' if len(unfilled) == 1 else 'them'} in the browser, "
                        "then press Resume (or press Resume for the desk to try again).")
        else:
            return False
        return True

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
        if (run.account_page and not run.accounts_tried and _bare(data.get("url") or "") == run.account_page
                and _email_account_view(data)):
            return False  # still in the steps of making their account, which are theirs (Eightfold's modal)
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
        mine = press is None and (press := self._account_code_button(run, data)) is not None
        try:
            if mine and press is not None:  # (the code step's own "Submit": it sends the code only)
                pressed = (await srv.browser.click(press["id"], allow_submit=True)).get("clicked") is not False
            else:
                pressed = press is not None and (await srv.click(press["id"])).get("clicked")
        except Exception:  # the button went (the page moved on by itself) or won't take a click
            pressed = False
        if press is not None and pressed:
            self._log(run, f"pressed \u201c{press['text'].strip()}\u201d")
            if mine:  # said once the application shows (_drive), as a code turned down leaves it here
                run.account_made = (f"created your account on {_site(run, data)} with your email and the code it "
                                    "emailed you (manage_accounts: false in profile.yaml leaves this to you)")
        else:  # (a "Confirm" that sends a form is left to the person, as any final button is)
            run.reason = ("I entered the code from your email. Press the page's button to carry on; "
                          "the desk continues after that.")
        return True

    def _account_code_button(self, run: Run, data: dict[str, Any]) -> dict[str, Any] | None:
        """The "Submit" of the emailed code's step of an account the desk is making (an Eightfold site's,
        settings.manage_accounts): pressed after the code, as it sends nothing but the code. Only there,
        and only with the code's boxes on the page: a Submit beside anything else is the person's."""
        if not (run.account_page and run.accounts_tried and _bare(data.get("url") or "") == run.account_page
                and _email_account_view(data) == "code" and _may_manage_accounts()):
            return None
        fields = [f for f in data.get("fields") or [] if not f.get("disabled") and not f.get("aside")]
        if not all(f.get("kind") in ("text", "number") and _CODE_FIELD.search(f.get("label") or "") for f in fields):
            return None
        return self._account_button(data.get("actions") or [], _CODE_SENDS)

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
            if (run.seen_form and not pressed and (gone := sorted(confirmations(text)))
                    and not _mid_application(data, text)):
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
                run.account_made = ""
            if str(data.get("url") or "").startswith("chrome-error://"):
                host = unreached_host(data, text)
                where = f"{host}, which" if host else "a page that"
                return self._pause(run, "stuck", f"The application went on to {where} couldn't be reached, so the page "
                                   "didn't load. Press Resume to try again, or open the posting in your own browser to "
                                   "apply there.")
            if kind == "page" and not data.get("fields") and _TURNED_AWAY.search(data.get("title") or ""):
                refusal = re.sub(r"\s+", " ", str(data.get("title"))).strip()
                return self._pause(run, "stuck", f"{_site(run, data)} turned the desk's browser away ({refusal}). "
                                   "Open the posting in your own browser to apply there.")
            if kind == "bot_check":
                await self._bring_forward(run)
                return self._pause(run, "bot_check", _BOT_CHECK_SAYS, seen=data)
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
            if (kind == "sign_in" and not account_waited and not _account_and_application(data)
                    and sum(f.get("kind") == "password" for f in data.get("fields") or []) >= 2):
                # A Create Account form: Qorvo's draws its application below it a moment later
                account_waited = True
                await self._wait_for_application()
                continue
            if kind == "sign_in" and _account_and_application(data):
                return await self._apply_with_account(run, data)
            manage = (kind in ("sign_in", "page", "email_code") or run.resetting) and _may_manage_accounts()
            # On the reset's "email sent" page, or back from waiting on an email that didn't come, on whatever
            # page the site left (Workday's reset form stays, filled, with its "if an account exists" note,
            # which can read as a form: so before a form's "past the sign-in" below)
            sent_page = kind in ("page", "email_code") and bool(_RESET_SENT.search(text))
            if manage and run.resetting and (sent_page or run.reset_no_mail):
                if not run.reset_waited:
                    return await self._await_reset_email(run, data)
                # Resume pressed on it: the person set the password from the email themselves (or the
                # inbox wait ran out), so back to the sign-in page, where the saved password is tried again.
                # No email in RESET_MAIL_WAIT: most likely no account there, so the way to a new one instead.
                run.resetting = False
                if run.reset_from and run.page is not None:
                    await self._back_to_sign_in(run)
                    sign_ins.clear()
                    if run.reset_no_mail:
                        self._log(run, f"no password reset email came from {_site(run, data)} in "
                                  f"{RESET_MAIL_WAIT // 60} minutes, so I went back to make an account there")
                        sign_ins["submitted"] = 1
                    else:  # the saved password is the site's now, as the person was asked: one more sign-in
                        run.sign_in_tries = min(run.sign_in_tries, SIGN_IN_TRIES - 1)
                    continue
            if kind == "form" and finish is None:
                run.resetting = False  # past the sign-in, however the password was set
                run.sign_in_tries = 0  # (and none of its sign-ins was refused)
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
                    # a new account: its sign-in (where the site asks for one) is tried afresh, once more
                    # however many the refused password had
                    sign_ins.pop("submitted", None), sign_ins.pop("create_account", None)
                    sign_ins["made"] = 1
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
                             "with them; if you do have an account, sign in instead." if sign_ins.get("create_account") else "")
                    fields = data.get("fields") or []
                    user = (" (your email as its user name: sign in there with it)" if any(
                        _user_name_box(f) for f in fields if f.get("kind") in ("text", "email")) else "")
                    asks = ("Fill in anything it still asks for (a picture code, say), tick their terms box if there is "
                            "one and create the account (then verify your email if they ask)")
                    if _security_boxes(fields):  # (never made up, so never pressed for the person: _make_account)
                        asks = ("It also asks for security questions and their answers: those are yours to choose (the "
                                "desk never makes up answers). Fill them in there and create the account")
                    elif _captcha_on(data):  # (never touched, so never pressed for the person either)
                        asks = ("Its CAPTCHA is yours to solve: solve it, tick their terms box if there is one and create "
                                "the account (then verify your email if they ask)")
                    return self._pause(run, "sign_in", f"I filled in {_site(run, data)}'s Create Account form with your "
                                       f"details and saved password{user}. {asks}; the desk carries on after that."
                                       + first, seen=data)
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
                                          "this job unless you press Resume." if run.sign_in_tries >= SIGN_IN_TRIES else "")
                                       + " Sign in in the browser window and the desk carries on by itself after that; or "
                                       "reset the password through its \u201cForgot password\u201d to the one you saved on "
                                       "the desk, then press Resume: the desk signs in with it.", seen=data)
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
            if finish is not None:  # its boxes, its terms and its button (with settings.manage_accounts)
                if await self._finish_account(run, data, finish):
                    continue
                return
            if kind == "form" and sign_ins.get("create_account") and _email_first_account(data):
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
                if self._stop_for_what_is_left(run, gaps, pending, missing_files):
                    return
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
            gate_now = action is None and (button := _agreement_button(data)) is not None \
                and f"{_page_key(data)} {button['text'].strip()}" not in agreed and _terms_gate(button, data) \
                and _may_accept_notices()
            if action is None and kind == "page" and not waited and not gate_now:
                waited = True  # slow pages (Intel's Workday, Eightfold forms) draw their buttons late
                wait = SIGN_IN_STEP_WAIT if sign_in_step else BLANK_PAGE_WAIT if blank else LATE_BUTTONS_WAIT
                if await self._wait_for_progress(wait):
                    continue
            drawn = (_page_key(data), _boxes(data))  # a page that has drawn more since is waited on again
            loading = bool(data.get("busy")) and not gate_now
            if action is None and drawn not in step_waited and (loading or kind == "form" and _greyed_step(data)):
                # a form still being drawn: Oracle's Personal Info shows its upload boxes and a greyed-out
                # Next first, then its name, email and phone boxes and an enabled Next. Or a step still
                # loading: KLA's Workday showed its loading dots where its Application Questions go, and a
                # greyed-out Save and Continue, for a while (live, Oct 2026)
                step_waited.add(drawn)
                if await self._wait_for_step(STEP_LOAD_WAIT if loading else LATE_BUTTONS_WAIT, data):
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
                agree = _agreement_button(data)
                if agree is not None:
                    said = agree["text"].strip()
                    gate = f"{_page_key(data)} {said}"
                    if gate in agreed:  # pressed for them on this pass, and the page stayed
                        return self._pause(run, "stuck", f"I pressed \u201c{said}\u201d for you (settings.accept_notices), "
                                           "but the page stayed: it may want a box filled or a check passed first. "
                                           "Look at it in the browser window, then press Resume.")
                    if _terms_gate(agree, data) and _may_accept_notices():
                        try:
                            clicked = await self._press_agreement(agree["id"])
                        except KeyError:  # the page changed between looking and clicking: look again
                            continue
                        except Exception:  # it won't take a click: the person's, as without the setting
                            clicked = {"clicked": False}
                        if clicked.get("clicked"):
                            agreed.add(gate)  # (once a pass: a page that stays is the person's after all)
                            whose = f"{run.company}'s" if run.company else "the site's"
                            self._log(run, f"agreed to {whose} \u201c{_short(said)}\u201d for you (settings.accept_notices)")
                            continue
                    return self._pause(run, "stuck", f"The way on is \u201c{said}\u201d, which agrees to "
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
                here = f"{_page_key(data)} "
                held = next((g[len(here):] for g in agreed if g.startswith(here)), "")
                if held:  # an agreement pressed for them on this pass, its button greyed out since
                    if _BOT_BADGE.search(text) or any(_BOT_BADGE.search(a.get("text") or "") for a in data.get("actions") or []):
                        # Charles Schwab's iCIMS (live check, Oct 2026): its hCaptcha held the press
                        return self._pause(run, "stuck", f"I pressed \u201c{held}\u201d for you "
                                           "(settings.accept_notices), and the page stayed with it greyed out. The page "
                                           "is protected by a bot check (hCaptcha), which may want you to show you're "
                                           "not a robot: that's yours. Look at it in the browser window, then press Resume.")
                    return self._pause(run, "stuck", f"I pressed \u201c{held}\u201d for you (settings.accept_notices), "
                                       "but the page stayed: it may want a box filled or a check passed first. "
                                       "Look at it in the browser window, then press Resume.")
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
            if kind == "form" and pressed[-3:] == [key] * 3:
                # the same step button pressed three times on the same step: Cornerstone's (Matheson's,
                # under load, Oct 10) redraws its page a little after each press, so no single press
                # looked like a stall, and the desk pressed Next fifteen times
                problems = _flagged(data) or [
                    f"\u201c{a['text'].strip()}\u201d is greyed out, so the site still wants something (a file, say, "
                    "or a box to tick)" for a in _greyed_step(data)[:1]]
                errors = "; ".join(problems)[:300].rstrip(" .")
                return self._pause(run, "stuck", "The page didn't move on" + (f": {errors}." if errors else ".")
                                   + " Fix it in the browser, then press Resume.")
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
                        # recognize this email. Create a new account"): the desk makes it with
                        # settings.manage_accounts (once a job), and otherwise it's the person's to make
                        now = (await self._look())[0]
                        create = next((a for a in now.get("actions") or [] if not a.get("disabled")
                                       and _CREATE_ACCOUNT.match(final_text(a["text"]))), None)
                        if create is not None:
                            run.account_page = _bare(now.get("url") or "")
                            if not run.accounts_tried and _may_manage_accounts():
                                if await self._make_email_account(run, now, create):
                                    stalls = 0
                                    continue
                                return
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
        """Wait for a form still being drawn: its step button enabled, or more boxes to fill, or
        (a page showing a loading indicator) the indicator gone."""
        had = _boxes(before)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            data = await self.srv.inspect_form(include_dropdown_options=False)
            if (pick_next(data.get("actions") or [], in_form=True) or _boxes(data) > had
                    or before.get("busy") and not data.get("busy")):
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
        the form's own button, after its password boxes (its submit button, or a link or plain button
        in that form that its script sends it with: ApplicantStack's "Submit"). Once a job
        (Run.accounts_tried). Not with a CAPTCHA on the page, a picture code's box among them
        (Benchmark's Infor registration: "Enter the text in image above"): that's the person's. True
        when it was pressed; the account is said to be made only once the site shows it
        (Run.account_made, in _drive). Nor with security questions on the form (some Taleo sites'):
        their answers are the person's."""
        if _captcha_on(data) or _security_boxes(data.get("fields") or []):
            return False
        srv = self.srv
        terms = _terms_boxes(data)
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
                            if terms else "") + " (manage_accounts: false in profile.yaml leaves this to you)")
        await self._wait_for_account(data)
        return True

    async def _finish_account(self, run: Run, data: dict[str, Any], button: dict[str, Any]) -> bool:
        """The rest of a Create Account, on a page of its own after the email and password (UKG Pro's
        "Almost there!", live, Oct 2026: a name and phone, a consent box, and "Create account", greyed
        out until the box is ticked). Its boxes are filled from the profile. With
        settings.manage_accounts (and no CAPTCHA), its terms boxes are ticked (never a newsletter's or
        text messages': _terms_boxes) and its button pressed once it's enabled, once a job
        (Run.account_finished); the account is said made only once the site goes on from it
        (Run.account_made, in _drive). Otherwise, or when the page is still there after that, it's
        the person's. True when pressed."""
        srv = self.srv
        site = _site(run, data)
        said = button["text"].strip()
        if await self._fill_account_details(data.get("fields") or []):
            self._log(run, f"filled the rest of {site}'s Create Account form with your details")
        why = ""
        if _may_manage_accounts() and not run.account_finished and not (data.get("captcha") or data.get("challenge")):
            now, _ = await self._look()
            terms = _terms_boxes(now)
            boxes = terms + _other_positions_boxes(now)
            pressable = None
            if boxes and not (await srv.fill_form([{"id": f["id"], "value": _tick(f)} for f in boxes])).get("ok"):
                why = "its terms box wouldn't tick"
            else:  # greyed out until its terms are ticked: a moment for the page to take that in
                for _ in range(10):
                    now, _ = await self._look()
                    named = [a for a in now.get("actions") or [] if a.get("text", "").strip() == said]
                    pressable = next((a for a in named if not a.get("disabled")), None)
                    if pressable is not None or not named:
                        break
                    await asyncio.sleep(0.5)
                if pressable is None:
                    why = f"“{said}” stayed greyed out"
            if pressable is not None:
                try:
                    await srv.browser.click(pressable["id"], allow_submit=True)  # (it creates the account)
                except Exception:
                    why = f"“{said}” wouldn't take a click"
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
        now, _ = await self._look()
        run.page_info = _page_info(now)
        if run.account_finished or why:
            wants = _account_wants(data, now) or "see the page in the browser window."
            done = f"I pressed “{said}”" if run.account_finished else f"I couldn't finish it ({why})"
            self._pause(run, "sign_in", f"{site} asked for a few more details to create your account, and {done}; it "
                        f"wants something more: {wants} Finish it there; the desk carries on after that.", seen=now)
        else:
            self._pause(run, "sign_in", f"{site} asks for a few more details to create your account. I filled in what "
                        f"your profile has; tick its terms box if you agree and press “{said}” in the "
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
                  "one (manage_accounts: false in profile.yaml leaves this to you)")
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
            now, _ = await self._look()
            if _email_account_view(now) != view:
                break
            data, button = now, self._account_button(now.get("actions") or [], pattern)
        if button is None:
            return await self._account_theirs(run, data, f"I couldn't find the button that goes on with making your account "
                                        f"on {site}")
        if view == "consent":
            ticked = [f for f in data.get("fields") or [] if f.get("kind") == "checkbox" and not f.get("disabled")
                      and is_empty_value(f.get("value")) and _TERMS_BOX.search(f.get("label") or "")
                      and not _NOT_TERMS.search(f.get("label") or "") and _TERMS_ONLY.search(f.get("label") or "")]
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
        if wanted and _email_account_view(now) == view and _page_sig(now) == _page_sig(data):
            # refused without its consent to be considered for other open positions too: given, and pressed again
            again = self._account_button(now.get("actions") or [], _ACCOUNT_ON)
            if again is not None and await self._give_other_positions(run, now, wanted, refused=True):
                with contextlib.suppress(Exception):
                    await srv.browser.click(again["id"], allow_submit=True)
                    now, _ = await self._after_press(now)
        if _email_account_view(now) != view or _page_sig(now) != _page_sig(data):
            return True  # on to its next step (the main loop's next look)
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
            data, _ = await self._look()
            forgot = _forgot_action(data.get("actions") or [])
            if forgot is None:
                return await stop("couldn't find its way to a password reset", data)
        run.reset_from = data.get("url") or ""
        if not await press(forgot):
            return await instead("couldn't open its password reset", data)
        data, text = await self._look()
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
            await self._bring_forward(run)
            self._pause(run, "bot_check", f"I opened {site}'s password reset for your saved password, which didn't sign "
                        "in, and filled in your email. Solve its picture code and press its button; the desk watches your "
                        "inbox for the reset email and sets your saved password as the new one.", seen=data)
            return "paused"
        button = self._account_button(data.get("actions") or [], _RESET_ASK, full=False)
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
        self._log(run, f"your saved password didn't sign in on {site}, so I asked it to email a password reset "
                  "(manage_accounts: false in profile.yaml leaves this to you)")
        await self._await_reset_email(run, data)
        return "paused"

    async def _back_to_sign_in(self, run: Run) -> None:
        """Back to the sign-in page a password reset was asked from (run.reset_from). Where that page
        was good for one visit (SuccessFactors' career?_s.crb=..., Arizona Public Service's for the test
        identity, live Oct 2026: "An error occurred while processing your request"), or its address
        alone doesn't bring it back (Benchmark's Infor sign-in, live Oct 2026: a refused sign-in answers
        at /sso/SSOServlet, where its form is sent, and that address opened again is Infor's own
        sign-in, with no "Register" or "Forgot password?"), the job's posting is opened again in its
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
        else:
            return
        self._log(run, f"{_site(run, data)}'s sign-in page{gone}, so I opened the job again")
        await self.srv.open_application(job_id=run.job_id)

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
        # about AI screening of applications, or the employer's privacy notice or terms of use (whose
        # "Ok" takes it in, beside a Cancel, where its heading names it)
        terms, takes_ok = _terms_notice(heading, notice.get("text") or "")
        ai = bool(_AI_NOTICE.search(said) and _ABOUT_APPLYING.search(said)) or terms
        agree = next((b for b in notice.get("buttons") or [] if not b.get("disabled")
                      and (_AGREES.match(final_text(b.get("text") or ""))
                           or takes_ok and _TAKES_IN.match(final_text(b.get("text") or "")))), None)
        allowed = ai and agree is not None and _may_accept_notices()
        if allowed and agree is not None and heading not in agreed:
            try:
                clicked = await self._press_agreement(agree["id"])
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
                        " (The desk agrees to an employer's notice about AI screening, its privacy notice or terms for "
                        "you with accept_notices: true under settings: in profile.yaml, outside practice mode.)"
                        if ai and agree is not None
                        and not allowed else ""))
        return "paused"

    async def _press_agreement(self, element_id: str) -> dict[str, Any]:
        """Press a button found to agree for the person (settings.accept_notices: a terms gate's way
        on, a notice's agree), as the click tool does. Saying so lets the live check's test identity
        press it in practice mode (Browser.click's `agreement`); every other check on it stays."""
        try:
            return {"clicked": True, **await self.srv.browser.click(element_id, agreement=True)}
        except SubmitBlocked as e:
            return {"clicked": False, "blocked": str(e)}

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
                what = "ticked" if attest[q["id"]] is True or isinstance(attest[q["id"]], list) \
                    else f"picked “{attest[q['id']]}” for"
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
        create = _account_way(actions)
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
            # SCREEN's and Taleo's forms ask for is the email too), and the rest from the profile.
            if details:
                await self._fill_account_details(fields)
            await srv.fill_form([{"id": f["id"], "value": address} for f in boxes])
        else:
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
        own = [a for a in actions if a.get("after_password") and not _SOCIAL.search(a["text"]) and not a.get("cookie")]
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

    async def _fill_account_details(self, fields: list[dict[str, Any]]) -> int:
        """A Create Account form's other boxes, from the profile: names, country. Its check
        boxes (a newsletter, the site's terms) and file boxes ("upload your resume now?") are
        left alone, as is anything the profile doesn't answer (Benchmark's picture code), and
        its security questions and answers (the person's). How many it filled."""
        questions = {f["id"] for f in _security_boxes(fields)}
        empty = [f for f in fields if f.get("kind") in _ACCOUNT_KINDS and is_empty_value(f.get("value"))
                 and f["id"] not in questions]
        plan = plan_autofill(empty, config.Profile.load(), {})
        if not plan["to_fill"]:
            return 0
        out = await self.srv.fill_form([{"id": f["id"], "value": f["value"]} for f in plan["to_fill"]])
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
        data, _ = await self._look()
        run.page_info = _page_info(data)  # the page as filled
        gaps, pending = self._entry_gaps(run, data, pending)
        if self._stop_for_what_is_left(run, gaps, pending, missing_files):
            return
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
    information true and complete, consents to the background check that comes with applying
    (Eightfold's "Terms and Conditions" dropdown, its one choice certifying the application is
    correct), or accepts the employer's privacy notice or terms of use: a tick, its one real choice
    where that agrees, or its one Yes. A group of one tick whose own words accept them (EMD's)
    is ticked. None for any other box."""
    label = q.get("label") or ""
    if not q.get("required") or q.get("error") or q.get("kind") not in _ATTEST_KINDS:
        return None
    choices = [str(o) for o in q.get("options") or [] if not is_empty_value(o)]
    if q["kind"] == "checkbox_group":
        # its ticks that accept the terms, and only those: EMD's required pair is its Terms of Use &
        # Data Privacy Statement and its "Email Communications" (marketing, left unticked)
        agreed = [o for o in choices if _agrees_to_terms(o)]
        return agreed or None
    if not (_ATTESTS.search(label) and not _NOT_ATTESTED.search(label) or _agrees_to_terms(label)):
        return None
    if q["kind"] == "checkbox":
        return True
    if len(choices) == 1:
        return choices[0] if polarity(choices[0]) is not False else None
    if _CLAIMS.search(label) or _OTHER_QUESTION.search(label):
        return None  # a Yes there would answer something else too ("Have you ever been convicted ...?")
    yes = [o for o in choices if polarity(o) is True]
    return yes[0] if len(yes) == 1 else None


def _terms_gate(button: dict[str, Any], data: dict[str, Any]) -> bool:
    """A page whose way on accepts the employer's privacy notice or terms ("I Acknowledge the
    Privacy Notice"; "I Accept" under a "Privacy Agreement" heading): its button, headings or title
    say so, not a privacy link in its footer. Never one that also sends the application ("I Accept
    and Apply"), nor one about what's never agreed to (a talent community, an arbitration agreement).
    Its headings are those of its own frame: Charles Schwab's careers page around its iCIMS frame
    has a "Join our talent network" sign-up of its own beside the gate (live, Oct 10)."""
    said = button.get("text") or ""
    framed = data.get("frame_headings")
    frame = re.match(r"f\d+-", str(button.get("id") or ""))
    own = framed.get(frame.group(0) if frame else "") if isinstance(framed, dict) else None
    about = " ".join([*(own if own is not None else data.get("headings") or []), str(data.get("title") or "")])
    if _SENDS_TOO.search(said) or _NEVER_AGREED.search(f"{said} {about}"):
        return False
    return bool(_TERMS.search(said) or _TERMS.search(about))


def _agreement_button(data: dict[str, Any]) -> dict[str, Any] | None:
    """A page's button that agrees to something ("I Acknowledge the Privacy Notice", "I Accept")."""
    return next((a for a in data.get("actions") or [] if _AGREEMENT.search(a["text"].strip())
                 and not a.get("cookie") and "cookie" not in a["text"].lower() and not a.get("disabled")), None)


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


def _security_boxes(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A Create Account form's security questions and their answer boxes (a plain "Answer"
    beside them too): the person's to choose and answer, never made up."""
    if not any(_SECURITY_QUESTION.search(f.get("label") or "") for f in fields):
        return []
    return [f for f in fields if f.get("kind") not in ("password", "checkbox", "file")
            and re.search(r"\bquestions?\b|\banswers?\b|\bhint\b", f.get("label") or "", re.I)]


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
    return bool(emails) and all(f in emails or _PICTURE_CODE.search(f.get("label") or "") for f in fields)


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
    if any(f.get("kind") in ("text", "number") and _CODE_FIELD.search(f.get("label") or "") for f in fields):
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
    ones, or its terms, or a privacy notice read. Never a newsletter's, job alerts' or text
    messages' (_NOT_TERMS)."""
    return [f for f in data.get("fields") or [] if f.get("kind") == "checkbox" and is_empty_value(f.get("value"))
            and _TERMS_BOX.search(f.get("label") or "") and not _NOT_TERMS.search(f.get("label") or "")
            and (f.get("required") or _TERMS_ONLY.search(f.get("label") or ""))]


def _new_candidate_boxes(data: dict[str, Any], company: str) -> list[dict[str, Any]]:
    """A Create Account form's box saying the person is a new candidate, not a current employee
    (_NEW_CANDIDATE), still unticked: ticked from the profile, so only when it has no current job
    at this employer (autofill.works_there_now). Someone who works there now leaves it be (an
    employee applies the way their employer says)."""
    boxes = [f for f in data.get("fields") or [] if f.get("kind") == "checkbox" and not f.get("disabled")
             and is_empty_value(f.get("value")) and _NEW_CANDIDATE.fullmatch(clean_label(f.get("label") or "").rstrip(" ."))]
    if not boxes or not company or works_there_now(config.Profile.load(), company):
        return []
    return boxes


def _other_positions_consent(field: dict[str, Any]) -> Any:
    """The value that gives an account form's consent to be considered for other open positions
    (_OTHER_POSITIONS): True for its box, or a group's one choice. None for anything else: a group
    with a choice between yes and no (a question for the person), or words that also sign them up
    for something (_NEVER_AGREED: a newsletter, job alerts, a talent pool, keeping their data)."""
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
    if not _OTHER_POSITIONS.search(said) or _NEVER_AGREED.search(_OTHER_POSITIONS.sub(" ", said)):
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
            + " (manage_accounts: false in profile.yaml leaves this to you)")


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
             and (f.get("required") or f.get("kind") == "checkbox" and (_TERMS_BOX.search(f["label"])
                  and not _NOT_TERMS.search(f["label"]) or _NEW_CANDIDATE.fullmatch(clean_label(f["label"]).rstrip(" ."))))]
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
