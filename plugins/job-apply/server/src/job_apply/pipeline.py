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

from . import config
from .ats import ATS_NAMES, detect_ats
from .autofill import clean_label, is_empty_value, norm

MAX_STEPS = 15
HANDS_ON = {"bot_check", "sign_in", "email_code"}
HANDS_ON_TIMEOUT = 15 * 60  # then the queue stops waiting and moves on
POLL_SECONDS = 3.0

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
_SOCIAL = re.compile(r"\b(google|apple|linked ?in|facebook|microsoft|indeed|seek)\b", re.I)
_STEP = re.compile(r"^(save (?:and|&) continue|continue|next|next step|review|review (?:and|&) submit|"
                   r"review application|proceed|go to next step)$", re.I)
_ENTRY = re.compile(r"^(apply manually|apply now|apply|easy apply|apply for (?:this|the) (?:job|position|role)|"
                    r"apply to (?:this )?job|start (?:your |my )?application|i'?m interested|"
                    r"continue to application)$", re.I)
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
    need: str = ""  # questions | sign_in | bot_check | email_code | captcha | your_submit | stuck
    reason: str = ""
    questions: list[dict[str, Any]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    url: str = ""
    blocking: bool = False  # the queue waits on this one
    paused_at: float = 0.0
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


_ERRORISH = re.compile(r"error|required|invalid|please|must|enter |select |missing|problem|fix|can'?t be blank", re.I)


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
        run = self.runs.get(job_id) or Run(job_id, job.get("title", ""), job.get("company", ""))
        self.runs[job_id] = run
        if run.status == "running":
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
        if run.page is not None and not run.page.is_closed():
            try:
                await run.page.close()
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
        blocker = next((r for r in self.runs.values() if r.blocking), None)
        if blocker is not None:
            if time.time() - blocker.paused_at > HANDS_ON_TIMEOUT:
                blocker.blocking = False
                blocker.reason += " (stopped waiting; press Resume when you're ready)"
            elif await self._moved_on(blocker):
                blocker.blocking = False
                self.enqueue(blocker.job_id, submit=blocker.submit, front=True)
            else:
                await self._sleep(POLL_SECONDS)
                return
        if not self.tasks:
            self._wake.clear()
            await self._sleep(30)
            return
        kind, job_id = self.tasks.popleft()
        run = self.runs[job_id]
        self.current = job_id
        try:
            if kind == "submit":
                await self._submit(run)
            else:
                await self._drive(run)
        except Exception as e:
            run.status, run.need = "failed", ""
            run.reason = f"Something went wrong: {type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"
            self._log(run, run.reason)
        finally:
            self.current = None
            run.updated = time.time()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _moved_on(self, run: Run) -> bool:
        """Has the person got the paused tab past its sign-in, check or code?"""
        if not self.srv.browser.use_tab(run.page):
            return True  # they closed it: start the job again
        data, text = await self._look()
        return classify(data, text) != run.need

    # ------------------------------------------------------------- one job
    def _log(self, run: Run, text: str) -> None:
        run.log.append(text)
        del run.log[:-40]
        run.updated = time.time()

    def _pause(self, run: Run, need: str, reason: str, questions: list[dict[str, Any]] | None = None) -> None:
        run.status, run.need, run.reason = "needs_you", need, reason
        run.questions = questions or []
        run.blocking = need in HANDS_ON
        run.paused_at = time.time()
        self._log(run, reason)

    async def _look(self) -> tuple[dict[str, Any], str]:
        data = await self.srv.inspect_form(include_dropdown_options=False)
        text = await self.srv.page_text(4000)
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
        stalls, sign_ins, entries_done, waited = 0, 0, set(), False
        for _ in range(MAX_STEPS):
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
                return self._pause(run, "bot_check", "The site is showing a bot check. Solve it in the browser "
                                   "window; the desk carries on by itself after that.")
            if kind == "sign_in":
                done = await self._sign_in(run, data) if sign_ins < 2 else None
                if done == "signed_in":
                    sign_ins += 1
                    continue
                await self._bring_forward(run)
                if done == "prefilled":
                    return self._pause(run, "sign_in", f"I filled in your email and saved password on {_site(run, data)}'s "
                                       "Create Account form. Tick their terms box if there is one and create the account "
                                       "(then verify your email if they ask); the desk carries on after that.")
                tip = (" Save a Workday password on the desk and it fills these in for you next time."
                       if detect_ats(data["url"]) == "workday" and config.get_secret("workday_password") is None else "")
                return self._pause(run, "sign_in", f"Sign in (or create your account) on {_site(run, data)} in "
                                   "the browser window; the desk carries on by itself after that." + tip)
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
                pending = [{**f, "error": once_failed[question_key(f.get("label") or "")]}
                           if question_key(f.get("label") or "") in once_failed else f
                           for f in result["needs_input"] if f.get("required") and f.get("kind") != "file"]
                pending += [{"id": f["id"], "label": f.get("label") or "", "kind": "text", "required": True,
                             "error": f.get("error")} for f in result["failed"]]
                missing_files = [f for f in result["needs_input"] if f.get("required") and f.get("kind") == "file"]
                data, text = await self._look()  # filling can add or enable things (State after Country, Submit)
                run.page_info = _page_info(data)  # what the person sees on the desk: the page as filled
                if missing_files:
                    return self._pause(run, "stuck", "The form needs a file the profile doesn't point to (set "
                                       "documents.resume in profile.yaml): " + ", ".join(f["label"] for f in missing_files))
                if pending:
                    return self._pause(run, "questions", f"{len(pending)} question(s) your profile doesn't answer. "
                                       "Answer them here and the desk fills them in (and remembers them).", pending)
                actions = data.get("actions") or []
                entry_here = any(_ENTRY.match(a["text"].strip()) and not a.get("disabled") for a in actions)
            if (kind == "form" or run.seen_form and not entry_here) and await srv.browser.find_submit():
                return await self._finish(run, data, text)
            action = pick_next(data.get("actions") or [], in_form=kind == "form")
            if action is None and kind == "page" and not waited:
                waited = True  # slow pages (Intel's Workday, Eightfold forms) draw their buttons late
                if await self._wait_for_progress(10):
                    continue
            if action is None:
                greyed = [a for a in data.get("actions") or [] if a.get("is_submit") and a.get("disabled")]
                if greyed:
                    problems = "; ".join(e for e in data.get("errors") or [] if _ERRORISH.search(e))[:300]
                    return self._pause(run, "stuck", f"\u201c{greyed[0]['text']}\u201d is greyed out, so the site still "
                                       "wants something" + (f": {problems}" if problems else ".") +
                                       " Fix it in the browser, then press Resume.")
                return self._pause(run, "stuck", "I couldn't find the button that moves this application on. "
                                   "Take it a step further in the browser, then press Resume.")
            before = _fingerprint(data)
            clicked = await srv.click(action["id"])
            if clicked.get("clicked") is False:  # the guard says it's the final submit
                return await self._finish(run, data, text)
            self._log(run, f"clicked “{action['text']}”")
            run.page = srv.browser.current_tab or run.page
            if _fingerprint(clicked) == before:
                stalls += 1
                problems = [e for e in clicked.get("errors") or [] if _ERRORISH.search(e)]
                if problems or stalls >= 2:
                    errors = "; ".join(problems)[:300]
                    return self._pause(run, "stuck", "The page didn't move on" + (f": {errors}" if errors else ".")
                                       + " Fix it in the browser, then press Resume.")
            else:
                stalls = 0
        self._pause(run, "stuck", "This application has more steps than I expected. Have a look in the browser, "
                    "then press Resume.")

    async def _wait_for_progress(self, seconds: float) -> bool:
        """Wait for a form or a button that moves things on to appear."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            data = await self.srv.inspect_form(include_dropdown_options=False)
            if data.get("fields") or pick_next(data.get("actions") or [], in_form=False):
                return True
        return False

    async def _fill_once(self, run: Run, data: dict[str, Any]) -> dict[str, str]:
        """Answers the person gave for this application only (not remembered). Returns the
        ones that didn't go in, by question, with the reason."""
        if not run.once:
            return {}
        by_id = {f["id"]: question_key(f.get("label") or "") for f in data.get("fields") or []}
        fills = [{"id": fid, "value": run.once[key]} for fid, key in by_id.items()
                 if key in run.once and is_empty_value(next(f.get("value") for f in data["fields"] if f["id"] == fid))]
        if not fills:
            return {}
        out = await self.srv.fill_form(fills)
        failed = {by_id[r["id"]]: r.get("error") or "didn't take" for r in out.get("results", []) if not r.get("ok")}
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

    async def _sign_in(self, run: Run, data: dict[str, Any]) -> str | None:
        """With the profile email and a stored <ats>_password (if the person saved one): sign
        in ("signed_in"), or fill a Create Account form and leave its terms and button to the
        person ("prefilled"). None when there's nothing to do."""
        srv = self.srv
        secret = f"{detect_ats(data['url'])}_password"
        if config.get_secret(secret) is None:
            return None
        fields = data.get("fields") or []
        actions = data.get("actions") or []
        if not fields:
            email_button = next((a for a in actions if re.match(r"^sign in with email$", a["text"], re.I)), None)
            if email_button is None:
                return None
            await srv.click(email_button["id"])
            return "signed_in"
        passwords = [f for f in fields if f["kind"] == "password"]
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
            return "prefilled"
        await srv.click(button["id"])
        self._log(run, "signed in with your saved password")
        return "signed_in"

    async def _bring_forward(self, run: Run) -> None:
        if run.page is not None and not run.page.is_closed():
            try:
                await run.page.bring_to_front()
            except Exception:
                pass

    async def _finish(self, run: Run, data: dict[str, Any], text: str) -> None:
        """The review page (or a one-page form with its submit button) is reached."""
        srv = self.srv
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
            return await self._submit(run)
        srv._mark_ready(job, "filled by the Job Desk")
        run.status, run.need = "ready", ""
        run.reason = "Filled and waiting on the review page. Check it in the browser, then press Submit."
        self._log(run, "reached the review page")

    async def _submit(self, run: Run) -> None:
        srv = self.srv
        if not srv.browser.use_tab(run.page):
            run.status, run.reason = "needs_you", "Its tab was closed. Press Resume to fill it again first."
            return
        srv.browser.current_job_id = run.job_id
        result = await srv.submit_application(job_id=run.job_id, user_confirmed=True)
        if result.get("submitted"):
            run.status, run.need = "submitted", ""
            run.reason = "Submitted." if result.get("confirmed") else \
                "Submitted, but no confirmation showed. Check the page, and mark it applied if it went through."
        else:
            run.status, run.need = "ready", ""
            run.reason = result.get("reason") or "Not submitted."
        self._log(run, run.reason)


_APPLICATION_FIELD = re.compile(r"first name|last name|full name|legal name|resume|\bcv\b|phone|address|"
                                r"authori[sz]ed|sponsor", re.I)


def _application_like(data: dict[str, Any]) -> bool:
    fields = data.get("fields") or []
    return len(fields) >= 3 or sum(bool(_APPLICATION_FIELD.search(f.get("label") or "")) for f in fields) >= 1


def _site(run: Run, data: dict[str, Any]) -> str:
    ats = detect_ats(data.get("url") or "")
    if ats in ("company_site", ""):
        return f"{run.company}'s site" if run.company else "the site"
    return ATS_NAMES.get(ats, ats)


def question_key(label: str) -> str:
    return norm(clean_label(label))


def _where(data: dict[str, Any]) -> str:
    headings = [h for h in data.get("headings") or [] if h and not re.match(r"current step", h, re.I)]
    return f"“{headings[-1][:60]}”" if headings else "this page"
