"""The inbox, for Applier (a mixin): a job's emailed code or link looked for in the person's mail while it waits on
one, and the code put into the page's boxes.

Read with the email app password saved on the desk (mailbox.search); or, without a working one and with
settings.email_codes on, through Claude in the chat: the look records a request (MailAsk) instead, Claude's watch
(Desk's /api/mail/wait) wakes it, Claude reads only the messages the request's Gmail query names and hands them back
(give_mail), and the next look reads them with the inbox's own checks (mailbox.pick) and uses what it finds as it does
the inbox's. Claude never types the code: the desk does."""

from __future__ import annotations

import asyncio
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from collections import deque
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlparse

from . import config, mailbox
from .ats import detect_ats, host_of, shared_system

if TYPE_CHECKING:
    from .pipeline import Run

GIVE_MAIL_MOST = 10  # messages one give_mail hands over at most (the newest few the request's query found)


@dataclass
class MailAsk:
    """A job's emailed code or link asked of Claude (settings.email_codes, no working app password, Claude watching):
    what the job's look in the inbox would have searched for, as a Gmail query, for Claude to read through the Gmail
    connector and hand back (give_mail). Kept while the job waits on that email (MailMixin._live_ask), and renewed at
    each of its looks; never the code itself. Never a password reset's: that's the email app password's alone (the
    review of 0.3.228: a reset's link sets the saved password, and isn't one to pass through the chat)."""
    job_id: int
    paused_at: float  # the wait it's for: a new pause is a new request
    employer: str = ""
    want: str = "code"  # "code" or "link"
    senders: list[str] = field(default_factory=list)  # who may have sent it (MailMixin._mail_senders)
    after: float = 0.0  # mail from then on (its look's `since` less its look-back)
    before: float | None = None  # and before then: another job's mail may come after (a later wait, or its step)
    answered: float = 0.0  # when Claude last handed back nothing usable
    looks: int = 0  # how many times it has (each a little further apart: due)
    mails: list[mailbox.Mail] | None = None  # handed back, for the job's next look
    said: tuple[bool, str] | None = None  # what became of them: used, and in plain words (never the code)
    # the query given to Claude since its last hand-over: its `before`, and when it was first listed (Claude's search,
    # and so what it found, comes after that, never before), and whether it was listed again with another `before`
    # (then which one Claude searched with isn't known). A look that may be the window's last is one made with the
    # whole window before it in the past
    given_before: float | None = None
    given_at: float = 0.0
    given_other: bool = False
    # its window ends at another job's step on the same senders (MailMixin._others_step_since), was looked at whole,
    # and nothing in it could be used: what comes after can't be told from that job's, so it's the person's to enter
    closed: bool = False
    told: bool = False  # (said in the job's log, once)

    def spent(self) -> bool:
        """Nothing more to ask Claude for this wait: it has looked as many times as one wait asks (GMAIL_LOOKS_MOST),
        or its window is closed. The person's to enter now."""
        return self.closed or self.looks >= pipeline.GMAIL_LOOKS_MOST

    def spent_said(self) -> str:
        """Why it's the person's to enter now, for the job's log and its card."""
        if self.closed:
            return (f"Claude can't tell this job's {self.want} from another job's in your Gmail (one sender emails "
                    f"both, and the other began its own email step after this one paused), so it's yours to enter in "
                    "the desk's browser window")
        return (f"Claude looked in your Gmail {self.looks} times and none came that it could use, so the {self.want} "
                "is yours to enter in the desk's browser window")

    def handed_over(self) -> None:
        """Claude's hand-over came back (read, or nothing in it): the next listing's query is the next search's."""
        self.given_at, self.given_other = 0.0, False

    def due(self, now: float) -> bool:
        """For Claude to look for now: nothing handed back on, not spent, and MAIL_POLL_SECONDS after its last look came
        back with nothing usable, twice that after the next, and so on, GMAIL_WAIT_MOST apart at most."""
        wait = min(pipeline.MAIL_POLL_SECONDS * 2 ** max(0, self.looks - 1), pipeline.GMAIL_WAIT_MOST)
        return self.mails is None and not self.spent() and (self.looks == 0 or now - self.answered >= wait)

    def query(self) -> str:
        """The Gmail search for it: those senders, in its window (Gmail leaves out Spam and Trash by itself). Kept as
        given: what Claude hands back was found with it."""
        if not self.given_at:  # (its first listing since the last hand-over: listed again, the earliest stands)
            self.given_before, self.given_at, self.given_other = self.before, time.time(), False
        elif self.before != self.given_before:
            self.given_other = True
        q = f"from:({' OR '.join(sorted(self.senders))}) after:{math.floor(self.after)}"
        return q + (f" before:{math.ceil(self.before)}" if self.before is not None else "")

    def public(self) -> dict[str, Any]:
        """As mail_requests() lists it for Claude."""
        return {"job_id": self.job_id, "employer": self.employer, "want": self.want, "senders": sorted(self.senders),
                "after": math.floor(self.after),
                "after_time": datetime.fromtimestamp(self.after).astimezone().isoformat(timespec="seconds"),
                "before": math.ceil(self.before) if self.before is not None else None, "query": self.query()}


class MailMixin:
    """Applier's inbox: codes and links."""

    if TYPE_CHECKING:  # (the applier's own)
        srv: Any
        runs: dict[int, Run]
        _inbox_gen: int
        _inbox_takes: deque[tuple[float, int]]
        _mail_refused: str | None
        mail_problem: str | None
        mail_asks: dict[int, MailAsk]
        mail_watchers: int
        mail_watched_at: float
        _closing: bool
        _wake: asyncio.Event

        def _inbox_holder(self, run: Run) -> Run | None: ...

        def _hold_tools(self, job_id: int) -> tuple[Any, Any, Any]: ...

        def _give_back_tools(self, held: tuple[Any, Any, Any]) -> None: ...

        async def _strict(self, step: Any) -> Any: ...

        def _own_place(self, run: Run, url: str, own: set[str] | None = None) -> bool: ...

        def _own_hosts(self, run: Run) -> set[str]: ...

        def _log(self, run: Run, text: str) -> None: ...

        @staticmethod
        def _account_button(actions: list[dict[str, Any]], pattern: re.Pattern[str],
                            full: bool = True) -> dict[str, Any] | None: ...

    def _code_boxes(self, run: Run, data: dict[str, Any]) -> list[dict[str, Any]]:
        """An emailed code's boxes on a job's tab: the page's own (a tab still this job's: _enter_code), and a frame's
        whose own address is this job's too, as the tab's must be (_own_place: an iCIMS frame on the employer's careers
        page), or on the employer's own site (careers.acme.com's frame from apply.acme.com, as the mail's links are
        taken). Never a frame from anywhere else, nor one whose address isn't known: as a saved password goes only
        into a frame on its own system (_password_place)."""
        urls = data.get("frame_urls") or {}
        own = {h for h in self._own_hosts(run) | {run.paused_host} if h}
        own_sites = {mailbox.site_domain(h) for h in own if shared_system(f"https://{h}/") is None}

        def ours(prefix: str) -> bool:
            url = str(urls.get(prefix) or "")
            host = host_of(url)
            return bool(url) and (self._own_place(run, url, own) or bool(host) and shared_system(url) is None
                                  and mailbox.site_domain(host) in own_sites)

        return [f for f in data.get("fields") or []
                if pipeline._code_box(f) and (not pipeline._frame_of(f) or ours(pipeline._frame_of(f)))]

    def mail_login(self) -> tuple[str, str] | None:
        """The profile's email address and the app password saved for it, unless that
        password was turned down (a new one is tried)."""
        password = pipeline._secret("email_password")
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
            if tenant := re.fullmatch(r"([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com", host_of(u)):
                allowed.add(f"workday.{tenant.group(1)}.com")
        if run.employer_mail is None:
            try:
                own = str(config.Profile.load().get("personal.email") or "").rpartition("@")[2].strip().lower()
            except Exception:  # (a profile.yaml that doesn't read: no inbox to read either)
                own = ""
            run.employer_mail = frozenset(pipeline._employer_mail_domains(str(run.company or job.get("company") or ""),
                                                                          {host_of(u) for u in urls}, own))
        return {d for d in allowed | run.employer_mail if d}

    async def _check_mail_safely(self, run: Run) -> None:
        """_check_mail, with any surprise in it (a profile.yaml typo, say) kept from stalling the
        queue: the hold's long-wait release and the other jobs still go on."""
        try:
            await self._check_mail(run)
        except Exception as e:
            self.mail_problem = f"The desk couldn't look in your inbox ({type(e).__name__}). It tries again shortly."

    async def _check_mail(self, run: Run) -> None:
        """Look in the inbox for the code or link a waiting job's site emailed, and put it in. Without a working email
        app password, and with settings.email_codes on and Claude watching, the look is asked of Claude instead
        (MailAsk), and the messages Claude hands back are read at the next look, with the inbox's own checks."""
        ask = self._live_ask(run.job_id)
        handed = ask if ask is not None and ask.mails is not None else None
        try:
            await self._look_in_mail(run, handed)
        finally:
            if handed is not None and handed.mails is None:  # (read: due again a little later, unless used)
                handed.answered = time.time()
                handed.looks += 1
                handed.handed_over()
                self._tell_spent(run, handed)
                handed.said = handed.said or (False, run.mail_last)
            if run.mail_done:
                self.mail_asks.pop(run.job_id, None)

    async def _look_in_mail(self, run: Run, handed: MailAsk | None) -> None:
        """_check_mail's look. `handed`: the job's request, with the messages Claude handed back for it."""
        now = time.time()
        if (run.mail_done or now - run.mail_checked < pipeline.MAIL_POLL_SECONDS and handed is None
                or pipeline._mail_waited(run) > pipeline.MAIL_WINDOW):
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
        # without the inbox's own login: Claude's Gmail, with settings.email_codes on, while Claude watches (or what it
        # handed back meanwhile). Never a password reset's email: that's the app password's alone
        codes_on = login is None and not run.resetting and self._email_codes_on()
        gmail = codes_on and (handed is not None or self.claude_watching())
        if not gmail:  # (the inbox itself, or nothing: no request of Claude stands for this job)
            self._drop_ask(run.job_id, "the desk reads your inbox itself now" if login is not None else
                           "a password reset's email is read only with your email app password" if run.resetting
                           else "settings.email_codes is off" if not codes_on else "Claude isn't watching")
            handed = None  # (its look, if any, is the inbox's own: its turn now's, and never said to be Gmail's)
        if not (login or gmail) or run.page is None or run.page.is_closed():
            run.mail_last = "no inbox login" if not (login or gmail) else "its tab is closed"
            if handed is not None:  # (nowhere to put it)
                handed.mails = None
            return
        run.mail_checked = now
        gen = self._inbox_gen  # (a job taking the inbox while this look is under way: what it finds isn't used)
        logged = run.logged
        try:
            data, _ = await self.srv.browser.peek(run.page)
        except Exception as e:  # a tab mid-way through loading: looked at again next time
            run.mail_last = f"couldn't read its tab: {type(e).__name__}"
            run.mail_looked = time.time()  # (a look that failed is one tried: a reset's wait runs on, as on its own)
            return
        boxes = self._code_boxes(run, data)
        senders = self._mail_senders(run)
        if login is None and not senders:  # (whose mail it would be isn't known: nothing to ask Claude for)
            self._drop_ask(run.job_id, "the desk can't tell who would send it")
            run.mail_last = "not asked of Claude: the desk can't tell who would send it"
            return

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
        since = pipeline._mail_from(run)
        others = [pipeline._mail_from(r) for r in self.runs.values() if r is not run and r.status == "needs_you"
                  and r.need == "email_code" and self._mail_senders(r) & senders]
        later = [t for t in others if t > since]
        # (a few seconds back still: the site sends the code as the button is pressed, a moment before
        # the desk notices and pauses)
        look_back = pipeline.SHARED_LOOK_BACK if any(t <= since for t in others) else mailbox.LOOK_BACK
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
                run.others_mail = await asyncio.to_thread(pipeline._another_employers_mail,
                                                          str(run.company or job.get("company") or ""),
                                                          run.employer_mail or frozenset())
            before = min(later) if later else None
            others_mail = run.others_mail if want == "code" else None
            bound = None
            if login is None:
                # Gmail's: only what came before another job on the same senders began a step that may email the
                # person, since this one paused (less a margin for this computer's clock against Gmail's): what came
                # after may be that job's. The inbox's own look is refused while one began meanwhile; Claude's
                # messages say when each came, so the rest are read, never refused whole (which left a job the queue
                # went on without with no code at all while it went on: the review of 0.3.228). In the request's
                # query too, so Claude's newest few are from its window
                if (stepped := self._others_step_since(run, senders)) is not None:
                    bound = stepped - pipeline.STEP_CLOCK_SKEW
                    before = bound if before is None else min(before, bound)
            if login is not None:
                found = await asyncio.to_thread(mailbox.search, *login, asked, senders, want, own_link, before,
                                                look_back, others=others_mail)
            elif handed is None:  # asked of Claude: read at a look after Claude hands its Gmail's messages back
                self._ask_claude(run, want, senders, asked - look_back, before)
                return
            else:  # the messages Claude read in Gmail for it, by the inbox's own checks (off the event loop, as those)
                mails, handed.mails = handed.mails or [], None
                found = await asyncio.to_thread(mailbox.pick, mails, asked, senders, want, own_link, before, look_back,
                                                others=others_mail)
                # (the window, closed by that step, looked at whole by a search that ended there, a minute after it
                # ended: nothing more can come in it, so asking again would only be Claude's turns for nothing)
                if (found is None and bound is not None and handed.given_before is not None and handed.given_at
                        and not handed.given_other and abs(handed.given_before - bound) <= 1
                        and handed.given_at > bound + pipeline.MAIL_SETTLE):
                    handed.closed = True
        except mailbox.MailboxError as e:
            if login is not None and "app password" in str(e):  # (only the inbox's own sign-in says so)
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
        # (Gmail's look worked: an earlier look's trouble is past. Not a saved app password's, which is still said)
        if login is not None or not pipeline._secret("email_password"):
            self.mail_problem = None
        if (gen != self._inbox_gen and handed is None) or self._inbox_holder(run) is not None:
            # (the peek, the lists and the login take seconds: a job that took the inbox meanwhile may have had a code
            # sent that this look found as this one's. Looked at again once that job is past it)
            run.mail_last = "not used: another job began signing in or making an account while the inbox was read"
            run.mail_held_at = run.mail_held_at or time.time()
            return
        run.mail_looks += 1
        run.mail_looked = time.time()
        run.mail_last = (f"looked for a {want}" + (" in your Gmail, through Claude" if handed is not None else "")
                         + ": " + ("none" if found is None else f"found one from {found.sender}")
                         + f" ({len(boxes)} code box(es) on the page)")
        if found is None or run.status != "needs_you" or run.need != "email_code":
            return
        try:
            await self._use_mail(run, found, asked)
        finally:
            if handed is not None:  # (what it came to, in the log's own words: never the code)
                lines = run.log[-(run.logged - logged):] if run.logged > logged else []
                handed.said = (run.mail_done, "; ".join(lines) if run.mail_done and lines else run.mail_last)

    async def _use_mail(self, run: Run, found: mailbox.Found, asked: float) -> None:
        """A code or link found in the job's mail (its inbox's, or the Gmail messages Claude handed back): put in, or
        opened in its own tab, unless another job's or spent."""
        # (a code, and a reset's link: a confirmation link is the account's, and two jobs on one site share one: the
        # review)
        mail = (found.sender, found.value, found.received) if found.kind in ("code", "reset") else None
        if mail and found.kind == "code" and pipeline._MAIL_TAKEN.get(mail, run.job_id) != run.job_id:
            # (the job it went to has it: this one's is to come)
            run.mail_last += "; not used: another job's, put in there already"
            return
        if found.kind == "reset" and pipeline._link_opened(found.value):
            # a reset's link works once: one opened already (for this job or another) sets nothing. The new one is
            # waited for; and should none come, its wait running out is said, never taken for no email at all
            run.reset_spent = run.account_known = True
            pipeline.note_account(run.reset_from or run.url, "reset email")  # (an account there: accounts.json)
            run.mail_last += "; not used: that reset link was opened already"
            return
        if found.kind == "reset" and found.received < asked - pipeline.RESET_CLOCK_SKEW:
            run.reset_spent = run.account_known = True
            pipeline.note_account(run.reset_from or run.url, "reset email")  # (an account there: accounts.json)
            run.mail_last += "; not used: it came well before the reset was asked for, an earlier reset's"
            return
        if found.kind == "reset" and found.received < asked and time.time() - asked < pipeline.RESET_FRESH_WAIT:
            # from before the reset was asked for, by this computer's clock: an earlier reset's (its link spent: run
            # 136), or this one's with the clocks apart. A newer one is waited for a moment, and
            # this one used if none comes
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
                pipeline.note_account(run.reset_from or run.url, "reset email")
                await run.page.goto(found.value, wait_until="domcontentloaded", timeout=45000)
                run.mail_done = True
                self._log(run, f"opened the password reset link from your email ({pipeline._sent_from(found)})")
            else:
                await self.srv.browser.visit(found.value)
                run.mail_done = True
                self._log(run, f"opened the confirmation link from your email ({pipeline._sent_from(found)})")
                if run.page is not None and not run.page.is_closed():
                    await run.page.reload()
                # the account's verified: the Sign In it paused on (the same page, reloaded) is past the pause
                if run.verifying:
                    run.pause_sig = None
        except Exception as e:  # a slow site or a closed tab: the person finishes it, as without the inbox
            run.mail_done = True
            self._log(run, f"couldn't use the {found.kind} from your email ({type(e).__name__}); "
                      "it's in your inbox for you")
        finally:
            # (what changes in its tab now is the desk's doing, not the person's: _note_activity)
            run.desk_at = time.time()
            run.desk_code = pipeline._code_mark(found.value) if found.kind == "code" else 0
        if mail and run.mail_done:
            pipeline._take_code(mail, run.job_id)

    # ------------------------------------------------- the inbox through Claude's Gmail (settings.email_codes)
    @staticmethod
    def _email_codes_on() -> bool:
        """settings.email_codes, as the profile says now (one that doesn't read: off)."""
        try:
            return config.Profile.load().settings.email_codes is True
        except Exception:
            return False

    def claude_watching(self) -> bool:
        """Is Claude watching for the desk's mail requests: a wait of its watch open, or one (or a call of its mail
        tools) ended less than CLAUDE_WATCH_GRACE seconds ago?"""
        return self.mail_watchers > 0 or time.time() - self.mail_watched_at < pipeline.CLAUDE_WATCH_GRACE

    def claude_seen(self) -> None:
        """Claude called one of its mail tools: it's at work on the requests (and watching) for a while yet. A job
        waiting on its email is looked at again now (its request made, should it have none)."""
        if not self.claude_watching():
            self._wake.set()
        self.mail_watched_at = time.time()

    def _tell_spent(self, run: Run, ask: MailAsk) -> None:
        """A request that's spent (MailAsk.spent): said once in the job's log, and as its last word on the inbox."""
        if not ask.spent() or self.mail_asks.get(run.job_id) is not ask:
            return
        run.mail_last = ask.spent_said()
        if not ask.told:
            ask.told = True
            self._log(run, ask.spent_said())

    def _others_step_since(self, run: Run, senders: set[str]) -> float | None:
        """When another job whose mail may come from these senders first began a step that may email the person (took
        the inbox: _take_inbox) since this one paused, or None. Mail from then on may be that job's. Where the record
        of those steps doesn't reach back to the pause, the pause itself: none of it is known to be this job's."""
        takes = list(self._inbox_takes)
        if takes and len(takes) == self._inbox_takes.maxlen and takes[0][0] > run.paused_at:
            return run.paused_at
        for at, job_id in takes:
            if at <= run.paused_at or job_id == run.job_id:
                continue
            other = self.runs.get(job_id)
            if other is None or self._mail_senders(other) & senders:
                return at
        return None

    def _live_ask(self, job_id: int) -> MailAsk | None:
        """The job's request of Claude, while the job still waits on that email (the same pause, its code not in, its
        window not over, and no password reset's); one that's done with is dropped."""
        ask = self.mail_asks.get(job_id)
        run = self.runs.get(job_id)
        if ask is None:
            return None
        if (run is None or run.status != "needs_you" or run.need != "email_code" or run.mail_done or run.resetting
                or run.paused_at != ask.paused_at or pipeline._mail_waited(run) > pipeline.MAIL_WINDOW):
            self._drop_ask(job_id, "the job no longer waits on that email")
            return None
        return ask

    def _drop_ask(self, job_id: int, why: str) -> None:
        ask = self.mail_asks.pop(job_id, None)
        if ask is not None and ask.mails is not None:  # (handed back meanwhile: said what became of them)
            ask.mails, ask.said = None, (False, f"not used: {why}")

    def _ask_claude(self, run: Run, want: str, senders: set[str], after: float, before: float | None) -> None:
        """The look _check_mail would make in the inbox, asked of Claude: made, or renewed as it stands now."""
        ask = self.mail_asks.get(run.job_id)
        if ask is None or ask.paused_at != run.paused_at:
            ask = self.mail_asks[run.job_id] = MailAsk(run.job_id, run.paused_at)
        ask.employer, ask.want, ask.senders = run.company or "", want, sorted(senders)
        ask.after, ask.before = after, before
        run.mail_last = f"asked Claude to look in your Gmail for the {want}"
        self._tell_spent(run, ask)

    def open_mail_asks(self) -> list[MailAsk]:
        """The requests of Claude whose jobs still wait on their email."""
        return [a for a in (self._live_ask(j) for j in list(self.mail_asks)) if a is not None]

    def due_mail_asks(self) -> list[MailAsk]:
        """The open requests for Claude to look for now: not handed back on, and not answered with nothing in the last
        MAIL_POLL_SECONDS (each look a moment apart, as the inbox's)."""
        now = time.time()
        return [a for a in self.open_mail_asks() if a.due(now)]

    def gmail_looking(self, job_id: int) -> str | None:
        """What Claude is looking in the person's Gmail for, for this job ("code", "link" or "reset"), while it is: for
        the job's card."""
        ask = self._live_ask(job_id)
        return ask.want if ask is not None and not ask.spent() and self.claude_watching() else None

    def gmail_note(self, job_id: int) -> str | None:
        """Why Claude no longer looks in the person's Gmail for this job's code or link (MailAsk.spent_said), while
        the job still waits on it: for its card."""
        ask = self._live_ask(job_id)
        return ask.spent_said() + "." if ask is not None and ask.spent() else None

    def mail_watch_unwanted(self, queue: bool = True) -> str | None:
        """Why there's nothing for Claude's watch to wait for (its command needn't run again), or None while there is:
        settings.email_codes on, no working email app password, and (with `queue`) a job queued, under way or waiting
        on the person."""
        if not self._email_codes_on():
            return "settings.email_codes is off"
        before = self.mail_problem  # (a look at the login, said nowhere from here)
        try:
            login = self.mail_login()
        finally:
            self.mail_problem = before
        if login is not None:
            return "the desk reads your inbox itself, with the email app password saved on it"
        if queue and not any(r.status in ("queued", "running", "needs_you") for r in list(self.runs.values())):
            return "no job is queued or waiting on the desk"
        return None

    async def wait_for_mail_ask(self, seconds: float,
                                gone: Callable[[], Awaitable[bool]] | None = None) -> bool:
        """Claude's watch: True as soon as a request is due (due_mail_asks), False after `seconds` with none, or once
        the desk stops or the watch's caller has gone. Claude counts as watching meanwhile, and for
        CLAUDE_WATCH_GRACE after."""
        self.mail_watchers += 1
        self.mail_watched_at = time.time()
        self._wake.set()  # (a job waiting on an email is looked at again: its request made now)
        end = time.monotonic() + max(0.0, seconds)
        try:
            while True:
                if self.due_mail_asks():
                    return True
                left = end - time.monotonic()
                if left <= 0 or self._closing or (gone is not None and await gone()):
                    return False
                await asyncio.sleep(min(1.0, left))
        finally:
            self.mail_watchers -= 1
            self.mail_watched_at = time.time()

    async def hand_mail(self, job_id: int, messages: list[Any]) -> tuple[bool, str]:
        """give_mail: the messages Claude read in Gmail for a job's request, read at its next look with the inbox's
        own checks (mailbox.pick) and used as the inbox's are. Whether it was used, and what became of it in plain
        words: never the code. No messages (or none that read): nothing yet, and the request stays."""
        self.claude_seen()
        ask = self._live_ask(job_id)
        if ask is None:
            return False, (f"No mail request is open for job {job_id} (it isn't waiting on an emailed code or link, or "
                           "the desk reads its inbox itself), so nothing was used.")
        if not isinstance(messages, list) or len(messages) > GIVE_MAIL_MOST:
            raise ValueError(f"messages should be a list of at most {GIVE_MAIL_MOST}: the newest few the request's "
                             "query found")
        # (read off the event loop, as the inbox's are: a long or odd message mustn't hold the desk up)
        mails = await asyncio.to_thread(lambda: [m for m in (mailbox.connector_mail(d) for d in messages
                                                             if isinstance(d, dict)) if m is not None])
        if not mails:
            ask.answered = time.time()
            ask.looks += 1
            ask.handed_over()
            run = self.runs.get(job_id)
            if run is not None:
                self._tell_spent(run, ask)
            ahead = any(mailbox.dated_ahead(d) for d in messages if isinstance(d, dict))
            why = (" (their dates are ahead of this computer's clock by more than a few minutes: its clock may be "
                   "behind, so the desk can't tell when they came)." if ahead else
                   " (none of those messages had a sender and a date it could read)." if messages else ".")
            return False, ("Nothing yet: the request stays, and comes up again a little later" if not ask.spent() else
                           "Nothing yet, and that was the last look for this wait: the code is the person's to enter"
                           ) + why
        ask.mails, ask.said = mails, None
        self._wake.set()  # (looked at as soon as the desk is back to that job: at once while it holds the queue)
        end = time.monotonic() + pipeline.GIVE_MAIL_WAIT
        while ask.said is None and time.monotonic() < end and self.mail_asks.get(job_id) is ask:
            await asyncio.sleep(0.2)
        if ask.said is None:
            if self.mail_asks.get(job_id) is not ask:
                return False, ("Not used: the job's wait changed meanwhile (it paused again, or stopped waiting). "
                               "Call mail_requests() for what it waits on now.")
            return False, ("Handed to the desk: it reads them as soon as it's back to that job (between jobs, as with "
                           "its own inbox). The request stays open until then.")
        used, said = ask.said
        return used, said

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
        boxes = self._code_boxes(run, data)
        if not boxes:  # the page moved on meanwhile
            run.mail_last += "; not typed: no code box on the page"
            return False
        # a box per digit (Oracle's "Confirm Your Identity"), or one box; typed key by key (BrassRing's take a
        # digit only from a key pressed in them)
        ids = [box["id"] for box in boxes] if 1 < len(boxes) == len(found.value) else [boxes[0]["id"]]
        why = ""
        try:
            run.did("typed", boxes[:1] if len(ids) == 1 else boxes)
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
        self._log(run, f"entered the code from your email ({pipeline._sent_from(found)})")
        # one naming the code before a bare Continue: Edward Jones' BrassRing (live, Oct 2026) keeps its email
        # step's Continue above "Verify Passcode", and a press of it sends another passcode and empties the boxes
        after = [a for a in data.get("actions") or [] if not a.get("disabled")
                 and pipeline._AFTER_CODE.match(a.get("text", "").strip())]
        press = next((a for a in after if not re.match(r"^(continue|next)$", a["text"].strip(), re.I)),
                     after[0] if after else None)
        made = press is None and (press := self._account_code_button(run, data)) is not None
        mine = made or (press is None and (press := await self._code_only_submit(data)) is not None)
        run.did("pressed", press)
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
                run.account_made = (f"created your account on {pipeline._site(run, data)} with your email and the code "
                                    "it emailed you" + pipeline._manage_hint())
        else:  # (a "Confirm" that sends a form is left to the person, as any final button is)
            run.reason = ("I entered the code from your email. Press the page's button to carry on; "
                          "the desk continues after that.")
        return True

    async def _code_only_submit(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """_code_only_submit's button, unless the code step's own words (the block around its button and
        boxes, not the application behind a sign-in pop-up) speak of an application: a code that sends one
        ("Enter the code we emailed to submit your application") is the person's to send (or Submit for
        me's), and in practice mode never sent."""
        press = pipeline._code_only_submit(data)
        if press is None:
            return None
        words = await self.srv.browser.words_around(press["id"])
        return press if words is not None and not pipeline._APPLICATION_WORD.search(words) else None

    def _account_code_button(self, run: Run, data: dict[str, Any]) -> dict[str, Any] | None:
        """The "Submit" of the emailed code's step of an account the desk is making (an Eightfold site's,
        settings.manage_accounts): pressed after the code, as it sends nothing but the code. Only there,
        and only with the code's boxes on the page: a Submit beside anything else is the person's."""
        if not (run.account_page and run.accounts_tried and pipeline._bare(data.get("url") or "") == run.account_page
                and pipeline._email_account_view(data) == "code" and pipeline._may_manage_accounts()):
            return None
        fields = [f for f in data.get("fields") or [] if not f.get("disabled") and not f.get("aside")]
        if not all(pipeline._code_box(f) for f in fields):
            return None
        return self._account_button(data.get("actions") or [], pipeline._CODE_SENDS)


# pipeline.py's own names (its waits, patterns, helpers and classes) are read from it as pipeline.<name> when a method
# runs: a test's patch of one (monkeypatch.setattr(pipeline, ...)) reaches the code here as it does pipeline.py's.
# Imported last: pipeline.py imports this module for Applier's bases, and either may be imported first.
from . import pipeline  # noqa: E402
