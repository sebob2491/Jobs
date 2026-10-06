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
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from . import config
from .ats import ATS_NAMES, detect_ats
from .autofill import clean_label, is_empty_value, norm
from .browser import TabClosed

NEW_TAB_WAIT = 4  # seconds to wait for a tab opened late by a click before calling it a stall
MAX_STEPS = 15
LATE_BUTTONS_WAIT = 10  # seconds for a page's buttons to be drawn
SIGN_IN_STEP_WAIT = 25  # Workday's sign-in step can take longer to draw its buttons (Applied's)
HANDS_ON = {"bot_check", "sign_in", "email_code"}
HANDS_ON_TIMEOUT = 15 * 60  # then the queue stops waiting and moves on
POLL_SECONDS = 3.0
FINISHED = {"applied", "interviewing", "offer", "rejected", "withdrawn"}  # tracker statuses never applied to again

_BOT_TITLE = re.compile(r"just a moment|attention required|access denied|pardon our interruption|security check|"
                        r"are you a robot|bot (?:check|detection)", re.I)
_BOT_TEXT = re.compile(r"verify (?:that )?you are (?:a )?human|are you a robot|checking (?:if the site connection is secure|"
                       r"your browser)|press (?:&|and) hold|complete the security check|unusual traffic from your|"
                       r"enable javascript and cookies to continue|request unsuccessful|you have been blocked", re.I)
_CAPTCHA = re.compile(r"i'?m not a robot|i am human|hcaptcha|recaptcha challenge", re.I)
_VERIFY_EMAIL = re.compile(r"verif(?:y|ication)\b.{0,40}\b(?:e-?mail|account|link)|check your (?:e-?mail|inbox)", re.I)
_CODE_FIELD = re.compile(r"verification code|one[- ]time (?:pass)?code|passcode|security code|\bcode\b.{0,40}"
                         r"(?:sent|email)|enter (?:the )?(?:\d-digit )?code|\botp\b", re.I)
_SIGN_IN_ACTION = re.compile(r"^(sign in|log ?in|sign in with email)$", re.I)
_CREATE_ACCOUNT = re.compile(r"^(create (?:an |your |a new )?account|sign up|register)[.!]?$", re.I)
_SOCIAL = re.compile(r"\b(google|apple|linked ?in|facebook|microsoft|indeed|seek)\b", re.I)
_STEP = re.compile(r"^(save (?:and|&) continue|continue|next|next step|review|review (?:and|&) submit|"
                   r"review application|proceed|go to next step)$", re.I)
_SIGN_IN_STEP = re.compile(r"create account\s*/\s*sign in|sign in\s*/\s*create account", re.I)  # Workday's step name
_ENTRY = re.compile(r"^(apply manually|apply now|apply|easy apply|quick apply|"
                    r"apply for (?:this|the) (?:job|position|role)(?: online)?|"
                    r"apply to (?:this )?job|start (?:your |my )?application|i'?m interested|"
                    r"continue to application|apply on (?:the )?(?:company|employer)(?:'s)? (?:site|website))$", re.I)
_AVOID = re.compile(r"autofill|with resume|resume parse|sign ?in|log ?in|create account|register|upload|back|"
                    r"previous|cancel|save for later|withdraw|delete|remove|search|share|print|email (?:me|this)", re.I)
_EXPERIENCE_PAGE = re.compile(r"my experience|work experience|employment history", re.I)
# Cookie banners: only ever the privacy-preserving choice, and only when the site offers one.
_DECLINE_COOKIES = re.compile(r"^(reject(?: all)?(?: cookies)?|decline(?: all)?(?: cookies)?|only (?:strictly )?necessary"
                              r"|necessary (?:cookies )?only|use necessary cookies only|accept (?:only )?necessary"
                              r"(?: cookies)?|reject optional(?: cookies)?)$", re.I)


@dataclass
class Run:
    job_id: int
    title: str = ""
    company: str = ""
    status: str = "queued"  # queued | running | needs_you | ready | submitted | failed | skipped
    # questions | sign_in | bot_check | email_code | captcha | your_submit | stuck
    # | submit_failed (pressed, the form is still there) | check_submit (pressed, no confirmation)
    need: str = ""
    reason: str = ""
    questions: list[dict[str, Any]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    url: str = ""
    blocking: bool = False  # the queue waits on this one
    paused_at: float = 0.0
    paused_site: str = ""  # the site it paused on, so a tab taken to webmail isn't "moved on"
    submit: bool = False  # submit once the review page is reached
    once: dict[str, Any] = field(default_factory=dict)  # answers for this application only, by question
    seen_form: bool = False  # got into the application itself (so a page with only Submit is its review page)
    page_info: dict[str, Any] = field(default_factory=dict)  # what the page looked like when it paused
    page: Any = None  # its browser tab
    updated: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "page"}


def classify(data: dict[str, Any], text: str) -> str:
    """bot_check, sign_in, email_code, form or page."""
    if data.get("challenge"):  # a CAPTCHA's pictures over the page (iCIMS after its email step)
        return "bot_check"
    fields = [f for f in data.get("fields", []) if not f.get("disabled")]
    if not fields and (_BOT_TITLE.search(data.get("title") or "") or _BOT_TEXT.search(text[:3000])):
        return "bot_check"
    if any(f["kind"] == "password" for f in fields):
        return "sign_in"
    actions = [a.get("text", "") for a in data.get("actions", [])]
    if not fields and any(re.match(r"^sign in with ", a, re.I) for a in actions):
        return "sign_in"  # Workday: "Sign in with email / Google / Apple"
    if any(_CODE_FIELD.search(f.get("label") or "") for f in fields if f["kind"] in ("text", "number")):
        return "email_code"
    if not fields and _VERIFY_EMAIL.search(text[:3000]) and not any(_ENTRY.match(a.strip()) for a in actions):
        return "email_code"  # "we sent you a link to verify your account"
    return "form" if fields else "page"


def pick_next(actions: list[dict[str, Any]], in_form: bool) -> dict[str, Any] | None:
    """The button that moves the application on: a step button inside a form, otherwise
    the way into it ("Apply Manually" before "Apply")."""
    usable = [a for a in actions if not a.get("disabled") and not a.get("is_submit")]
    steps = [a for a in usable if _STEP.match(a["text"].strip())]
    entries = [a for a in usable if _ENTRY.match(a["text"].strip()) and not _SOCIAL.search(a["text"])
               and not (_AVOID.search(a["text"]) and "manually" not in a["text"].lower())]
    entries.sort(key=lambda a: "manually" not in a["text"].lower())
    order = (steps + entries) if in_form else (entries + steps)
    return order[0] if order else None


def _fingerprint(page: dict[str, Any]) -> tuple:
    fields = page.get("fields")
    count = fields if isinstance(fields, int) else len(fields or [])
    actions = tuple(a if isinstance(a, str) else a.get("text", "") for a in page.get("actions") or [])
    return page.get("url"), tuple(page.get("headings") or []), count, actions


_BOT_CHECK_SAYS = ("The site is checking that you're a person (a bot check or CAPTCHA). Solve it in the browser "
                   "window; the desk carries on by itself after that.")
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
        self.current: int | None = None
        self._wake = asyncio.Event()
        self._worker_task: asyncio.Task | None = None

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
        run.submit = submit
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

    def later(self, job_id: int) -> Run:
        """Stop holding the queue for this job; it stays paused until resumed."""
        run = self.runs[job_id]
        run.blocking = False
        self._wake.set()
        return run

    async def skip(self, job_id: int) -> Run:
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
        # A Submit the person pressed goes first, even while the queue holds for a sign-in.
        submit = next((t for t in self.tasks if t[0] == "submit"), None)
        blocker = next((r for r in self.runs.values() if r.blocking), None)
        if blocker is not None and submit is None:
            if time.time() - blocker.paused_at > HANDS_ON_TIMEOUT:
                blocker.blocking = False
                blocker.reason += " (stopped waiting; press Resume when you're ready)"
            elif await self._strict(self._moved_on(blocker)):
                blocker.blocking = False
                try:
                    self.enqueue(blocker.job_id, submit=blocker.submit, front=True)
                except ValueError:  # marked applied meanwhile
                    pass
            else:
                self._wake.clear()  # sleep the poll out, unless something new comes in
                await self._sleep(POLL_SECONDS)
                return
        if not self.tasks:
            self._wake.clear()
            await self._sleep(30)
            return
        task = submit or self.tasks[0]
        self.tasks.remove(task)
        kind, job_id = task
        run = self.runs[job_id]
        self.current = job_id
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
            self.current = None
            if run.status == "running":  # the desk was stopped part-way
                run.status, run.reason = "failed", "Stopped before it finished. Press Resume to carry on."
            run.updated = time.time()

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
        if not self.srv.browser.use_tab(run.page):
            return True  # they closed it: start the job again
        data, text = await self._look()
        if classify(data, text) == run.need:
            return False
        url = data.get("url") or ""
        return _site_key(url) == run.paused_site or detect_ats(url) not in ("company_site", "linkedin", "indeed")

    # ------------------------------------------------------------- one job
    def _log(self, run: Run, text: str) -> None:
        run.log.append(text)
        del run.log[:-40]
        run.updated = time.time()

    def _pause(self, run: Run, need: str, reason: str, questions: list[dict[str, Any]] | None = None) -> None:
        if run.status == "skipped":
            return
        run.status, run.need, run.reason = "needs_you", need, reason
        run.questions = questions or []
        run.blocking = need in HANDS_ON
        run.paused_at = time.time()
        run.paused_site = _site_key(run.url)
        self._log(run, reason)

    async def _look(self) -> tuple[dict[str, Any], str]:
        data = await self.srv.inspect_form(include_dropdown_options=False)
        text = await self.srv.page_text(4000)
        data["challenge"] = await self.srv.browser.challenge_showing()
        return data, text

    async def _open(self, run: Run) -> bool:
        srv = self.srv
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

    async def _drive(self, run: Run) -> None:
        srv = self.srv
        run.status, run.need, run.reason = "running", "", "Working on it"
        if not await self._open(run):
            return
        stalls, entries_done, waited, refilled = 0, set(), False, set()
        sign_ins: dict[str, int] = {}  # what the saved password was used for on this pass
        pressed: list[tuple[Any, ...]] = []  # (page, button) for each button pressed on this pass
        pressed_on: list[str] = []  # and where, in words
        for _ in range(MAX_STEPS):
            if run.status == "skipped":  # pressed while this job was running
                return
            data, text = await self._look()
            run.page_info = _page_info(data)
            run.url = data["url"]
            run.page = srv.browser.current_tab or run.page
            if await self._decline_cookies(run, data, text):
                data, text = await self._look()
                run.page_info = _page_info(data)
            kind = classify(data, text)
            actions = data.get("actions") or []
            entry_here = any(_ENTRY.match(a["text"].strip()) and not a.get("disabled") for a in actions)
            if kind == "form" and entry_here and not _application_like(data):
                kind = "page"  # a posting with a "send me similar jobs" box: go in through Apply
            if kind == "bot_check":
                await self._bring_forward(run)
                return self._pause(run, "bot_check", _BOT_CHECK_SAYS)
            if kind == "sign_in":
                done = await self._sign_in(run, data, sign_ins)
                if done in ("email_step", "submitted", "create_account"):
                    sign_ins[done] = sign_ins.get(done, 0) + 1
                    continue
                await self._bring_forward(run)
                if done == "prefilled":
                    first = (" Your saved password didn't sign in there, so this is probably your first application "
                             "with them; if you do have an account, sign in instead." if sign_ins.get("create_account") else "")
                    return self._pause(run, "sign_in", f"I filled in your email and saved password on {_site(run, data)}'s "
                                       "Create Account form. Tick their terms box if there is one and create the account "
                                       "(then verify your email if they ask); the desk carries on after that." + first)
                if done == "filled":
                    return self._pause(run, "sign_in", f"I filled in your email and saved password on {_site(run, data)}'s "
                                       "sign-in form. Press its sign-in button in the browser window; the desk carries on "
                                       "after that.")
                tip = (" Save a Workday password on the desk and it fills these in for you next time."
                       if password_for(data["url"]) == "workday_password" and _secret("workday_password") is None else "")
                failed = " Your saved password didn't sign in there." if sign_ins.get("submitted") else ""
                return self._pause(run, "sign_in", f"Sign in (or create your account) on {_site(run, data)} in "
                                   "the browser window; the desk carries on by itself after that." + failed + tip)
            if kind == "email_code":
                await self._bring_forward(run)
                return self._pause(run, "email_code", "The site emailed you a code or a link. Enter the code in the "
                                   "browser window, or open the link; the desk carries on by itself after that.")
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
                pending = [{**f, **once_failed[question_key(f.get("label") or "")]}
                           if question_key(f.get("label") or "") in once_failed else f
                           for f in result["needs_input"] if f.get("required") and f.get("kind") != "file"]
                pending += [{"id": f["id"], "label": f.get("label") or "", "kind": "combobox" if f.get("options") else "text",
                             "required": True, "error": f.get("error"),
                             **({"options": f["options"]} if f.get("options") else {})} for f in result["failed"]]
                missing_files = [f for f in result["needs_input"] if f.get("required") and f.get("kind") == "file"]
                before, page_key = data, (data.get("url"), tuple(data.get("headings") or []))
                data, text = await self._look()  # filling can add or enable things (State after Country, Submit)
                run.page_info = _page_info(data)  # what the person sees on the desk: the page as filled
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
                entry_here = any(_ENTRY.match(a["text"].strip()) and not a.get("disabled") for a in actions)
            if (kind == "form" or run.seen_form and not entry_here) and await srv.browser.find_submit():
                return await self._finish(run, data, text)
            action = pick_next(data.get("actions") or [], in_form=kind == "form")
            sign_in_step = bool(_SIGN_IN_STEP.search(" ".join(data.get("headings") or [])))
            if action is None and kind == "page" and not waited:
                waited = True  # slow pages (Intel's Workday, Eightfold forms) draw their buttons late
                if await self._wait_for_progress(SIGN_IN_STEP_WAIT if sign_in_step else LATE_BUTTONS_WAIT):
                    continue
            if action is None and sign_in_step:
                # Workday's sign-in step whose sign-in buttons never drew (Applied's, now and then)
                await self._bring_forward(run)
                return self._pause(run, "sign_in", f"Sign in (or create your account) on {_site(run, data)} in "
                                   "the browser window; the desk carries on by itself after that.")
            if action is None:
                greyed = [a for a in data.get("actions") or [] if a.get("is_submit") and a.get("disabled")]
                if greyed:
                    problems = "; ".join(e for e in data.get("errors") or [] if _ERRORISH.search(e))[:300]
                    return self._pause(run, "stuck", f"\u201c{greyed[0]['text']}\u201d is greyed out, so the site still "
                                       "wants something" + (f": {problems}" if problems else ".") +
                                       " Fix it in the browser, then press Resume.")
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
            before = _fingerprint(data)
            try:
                clicked = await srv.click(action["id"])
            except KeyError:  # the page changed between looking and clicking (a tab opened): look again
                self._log(run, f"“{action['text']}” was gone by the time I clicked; looking again")
                run.page = srv.browser.current_tab or run.page
                continue
            if clicked.get("clicked") is False:  # the guard says it's the final submit
                return await self._finish(run, data, text)
            self._log(run, f"clicked “{action['text']}”")
            run.page = srv.browser.current_tab or run.page
            if _fingerprint(clicked) == before and await self._new_tab_soon(run, NEW_TAB_WAIT):
                continue  # asml.com's Apply Now opens Workday in a new tab a moment after the click
            if _fingerprint(clicked) == before:
                if await srv.browser.challenge_showing():  # the click brought up a CAPTCHA
                    await self._bring_forward(run)
                    return self._pause(run, "bot_check", _BOT_CHECK_SAYS)
                stalls += 1
                problems = [e for e in clicked.get("errors") or [] if _ERRORISH.search(e)]
                if problems or stalls >= 2:
                    # say what's wrong: Workday lists it as links ("Error-Email") and marks fields
                    problems = _flagged(clicked) or _flagged((await self._look())[0])
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
        ones that didn't go in, by question: the reason, and the entries to choose from
        when the answer was a group of them."""
        if not run.once:
            return {}
        by_id = {f["id"]: question_key(f.get("label") or "") for f in data.get("fields") or []}
        fills = [{"id": fid, "value": run.once[key]} for fid, key in by_id.items()
                 if key in run.once and is_empty_value(next(f.get("value") for f in data["fields"] if f["id"] == fid))]
        if not fills:
            return {}
        out = await self.srv.fill_form(fills)
        failed = {by_id[r["id"]]: {"error": r.get("error") or "didn't take",
                                   **({"options": r["options"]} if r.get("options") else {})}
                  for r in out.get("results", []) if not r.get("ok")}
        done = len(fills) - len(failed)
        if done:
            self._log(run, f"filled {done} answer(s) you gave for this application")
        if failed:
            self._log(run, f"{len(failed)} of your answers didn't go in")
        return failed

    async def _decline_cookies(self, run: Run, data: dict[str, Any], text: str) -> bool:
        """Press Reject / Decline / Necessary only on a cookie banner (never Accept). Banners
        cover forms and catch clicks; ones with no way to decline are left for the person."""
        if "cookie" not in text.lower():
            return False
        button = next((a for a in data.get("actions") or []
                       if _DECLINE_COOKIES.match(a.get("text", "").strip()) and not a.get("disabled")), None)
        if button is None:
            return False
        result = await self.srv.click(button["id"])
        if result.get("clicked"):
            self._log(run, f"declined cookies (\u201c{button['text']}\u201d)")
            return True
        return False

    async def _sign_in(self, run: Run, data: dict[str, Any], tried: dict[str, int]) -> str | None:
        """With the profile email and a stored <ats>_password (if the person saved one):

        - "email_step": pressed Workday's "Sign in with email" to reach the form;
        - "submitted": filled in the sign-in form and pressed its button (once a pass);
        - "create_account": that didn't get in, most likely because there's no account
          there yet, so it opened the site's Create Account form;
        - "prefilled": filled in a Create Account form, leaving its terms and button to
          the person;
        - "filled": filled in a sign-in form whose button it doesn't recognise.

        None when there's nothing (more) to do. `tried` counts what this pass already did."""
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
        if len(passwords) == 1 and tried.get("submitted"):
            # Signed in once already and still asked to: the password didn't get in. Trying it
            # again won't help (and can lock an account); a first visit needs an account.
            create = next((a for a in actions if _CREATE_ACCOUNT.match(a["text"].strip())), None)
            if create is None or tried.get("create_account"):
                return None
            try:
                await srv.click(create["id"])
            except KeyError:
                return None
            self._log(run, "your saved password didn't sign in, so I opened Create Account")
            return "create_account"
        email = next((f for f in fields if f["kind"] in ("text", "email")
                      and re.search(r"e-?mail|user ?name|login", f.get("label") or "", re.I)), None)
        address = config.Profile.load().get("personal.email")
        if email is None or not address or not 1 <= len(passwords) <= 2:
            return None
        await srv.fill_form([{"id": email["id"], "value": address}])
        for box in passwords:
            await srv.fill_secret(box["id"], secret)
        if len(passwords) == 2:  # a new account: accepting the site's terms is the person's call
            self._log(run, "filled the Create Account form with your email and saved password")
            return "prefilled"
        button = next((a for a in actions if _SIGN_IN_ACTION.match(a["text"].strip()) and not _SOCIAL.search(a["text"])),
                      None)
        if button is None:
            return "filled"
        await srv.click(button["id"])
        self._log(run, "signed in with your saved password")
        return "submitted"

    async def _bring_forward(self, run: Run) -> None:
        if run.page is not None and not run.page.is_closed():
            try:
                await run.page.bring_to_front()
            except Exception:
                pass

    async def _finish(self, run: Run, data: dict[str, Any], text: str) -> None:
        """The review page (or a one-page form with its submit button) is reached."""
        srv = self.srv
        if run.status == "skipped":
            return
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
        if run.submit and self.auto_submit:
            return await self._submit(run, by_person=False)
        srv._mark_ready(job, "filled by the Job Desk")
        run.status, run.need = "ready", ""
        run.reason = "Filled and waiting on the review page. Check it in the browser, then press Submit."
        self._log(run, "reached the review page")

    async def _submit(self, run: Run, by_person: bool = True) -> None:
        """Press the final button: the person pressed Submit for this job, or (by_person
        False) "Submit for me" is on, which also needs every required field filled."""
        srv = self.srv
        if run.status == "skipped":
            return
        if not srv.browser.use_tab(run.page):
            run.status, run.reason = "needs_you", "Its tab was closed. Press Resume to fill it again first."
            return
        srv.browser.current_job_id = run.job_id
        if not by_person:
            data = await srv.inspect_form(include_dropdown_options=False)
            empty = [f.get("label") or "a field" for f in _empty_required(data)]
            if empty:
                return self._pause(run, "stuck", "Not submitted: required fields are still empty (" + ", ".join(empty[:5])
                                   + "). Fill them in the browser, then press Resume.")
        result = await srv.submit_application(job_id=run.job_id, user_confirmed=True)
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
                                r"authori[sz]ed|sponsor", re.I)


def _application_like(data: dict[str, Any]) -> bool:
    fields = data.get("fields") or []
    return len(fields) >= 3 or sum(bool(_APPLICATION_FIELD.search(f.get("label") or "")) for f in fields) >= 1


# A saved password is typed only into its own system's pages, on that system's own domains:
# never into a page that just mentions one in its address (evil.example/myworkdayjobs.com).
PASSWORD_SITES = {
    "workday": ("myworkdayjobs.com", "myworkday.com", "myworkdaysite.com"),
    "successfactors": ("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu"),
    "icims": ("icims.com",), "taleo": ("taleo.net",), "brassring": ("brassring.com",), "avature": ("avature.net",),
}


def password_for(url: str) -> str | None:
    """The name of the saved password that belongs on this page, if any."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    for ats, domains in PASSWORD_SITES.items():
        if parsed.scheme == "https" and any(host == d or host.endswith("." + d) for d in domains):
            return f"{ats}_password"
    return None


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
