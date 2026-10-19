"""The inbox, for Applier (a mixin): a job's emailed code or link looked for in the person's mail while it waits on
one, and the code put into the page's boxes."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from . import config, mailbox
from .ats import detect_ats, host_of, shared_system

if TYPE_CHECKING:
    from .pipeline import Run


class MailMixin:
    """Applier's inbox: codes and links."""

    if TYPE_CHECKING:  # (the applier's own)
        srv: Any
        runs: dict[int, Run]
        _inbox_gen: int
        _mail_refused: str | None
        mail_problem: str | None

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
        """Look in the inbox for the code or link a waiting job's site emailed, and put it in."""
        now = time.time()
        if (run.mail_done or now - run.mail_checked < pipeline.MAIL_POLL_SECONDS
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
        boxes = self._code_boxes(run, data)
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
        if mail and found.kind == "code" and pipeline._MAIL_TAKEN.get(mail, run.job_id) != run.job_id:
            # (the job it went to has it: this one's is to come)
            run.mail_last += "; not used: another job's, put in there already"
            return
        if found.kind == "reset" and pipeline._link_opened(found.value):
            # a reset's link works once: one opened already (for this job or another) sets nothing. The new one is
            # waited for; and should none come, its wait running out is said, never taken for no email at all
            run.reset_spent = run.account_known = True
            run.mail_last += "; not used: that reset link was opened already"
            return
        if found.kind == "reset" and found.received < asked - pipeline.RESET_CLOCK_SKEW:
            run.reset_spent = run.account_known = True
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
