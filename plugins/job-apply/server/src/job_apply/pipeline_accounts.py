"""Accounts, for Applier (a mixin): with settings.manage_accounts, an account made at a job system, a saved
password the site refuses reset through its own "Forgot password" and its emailed link, and the sign-in with a saved
password."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from . import config
from .autofill import clean_label, is_empty_value, plan_autofill
from .browser import final_text

if TYPE_CHECKING:
    from .pipeline import Run


# ----------------------------------------------- the accounts the desk knows of
# Where the desk knows the person has an account (~/.job-apply/accounts.json, theirs alone): a place it made one, signed
# in to one with their saved password, had a reset email from, or was told one exists. Where it knows of none, a saved
# password the site refuses goes on to Create Account first, never to a password reset (the owner's note 46, Oct 20:
# the desk asked a site the person had never had an account at for a reset); a reset only once the site says the
# account exists ("already exists", "already registered"), or for an account it knows of.
ACCOUNTS_FILE = "accounts.json"
_ACCOUNTS_LOCK = threading.Lock()  # (note_account's: the desk's jobs at once, and Claude's tools)
# A job system many employers share at one host, and the part of its address that is the employer's: SuccessFactors'
# ?company= (or its career_company), BrassRing's partnerid, UKG Pro's and Jobvite's first folder, and the folder after
# Workday's "recruiting" on its shared myworkdaysite.com (wd1.myworkdaysite.com/recruiting/<tenant>/<site>)
_TENANT_QUERY = ("company", "career_company", "companyid", "partnerid")
_TENANT_FOLDER = ("recruiting.ultipro.com", "recruiting2.ultipro.com", "jobs.jobvite.com")


def account_place(url: str) -> str:
    """Where an account at this address is: its host (a Workday tenant's, an iCIMS site's, a Taleo career section's),
    with the employer's part of the address on a host many employers share (_TENANT_QUERY, _TENANT_FOLDER). A page saved
    on this computer, by its folder. "" for none."""
    try:
        parts = urlparse(url or "")
    except ValueError:
        return ""
    if parts.scheme == "file":
        return "file:" + parts.path.rsplit("/", 1)[0]
    host = (parts.hostname or "").lower().removeprefix("www.")
    if not host:
        return ""
    query = {k.lower(): v for k, v in (p.partition("=")[::2] for p in parts.query.split("&") if p)}
    tenant = next((f"?{k}={query[k].lower()}" for k in _TENANT_QUERY if query.get(k)), "")
    folders = [f.lower() for f in parts.path.split("/") if f]
    if host in _TENANT_FOLDER and folders:
        tenant = "/" + folders[0]
    elif host.endswith(".myworkdaysite.com") and "recruiting" in folders[:-1]:
        tenant = "/" + folders[folders.index("recruiting") + 1]
    return host + tenant


def _accounts() -> dict[str, Any]:
    """The accounts.json record: {place: {"how": ..., "at": ...}}; empty where there's none or it doesn't read."""
    try:
        got = json.loads((config.home() / ACCOUNTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def account_known(url: str, applied: Iterable[str] = ()) -> bool:
    """Does the desk know of an account of the person's at this address's place (account_place): one kept in
    accounts.json, or a job applied to at that place (`applied`: the tracker's addresses for them, an account from
    before the desk kept any, the review of note 46's fix)?"""
    place = account_place(url)
    return bool(place) and (place in _accounts() or any(account_place(u) == place for u in applied))


def note_account(url: str, how: str) -> None:
    """Keep that the person has an account at this address's place: `how` the desk knows ("made", "signed in",
    "reset email", "the site says it exists"). Whatever goes wrong keeping it, the job goes on (it's asked again:
    Create Account first, then the site's word)."""
    place = account_place(url)
    if not place:
        return
    # (read again and written under one lock: two jobs at once noting theirs never lose one, the review)
    with _ACCOUNTS_LOCK, contextlib.suppress(OSError, ValueError):
        known = _accounts()
        if place in known:
            return
        known[place] = {"how": how, "at": round(time.time())}
        config.ensure_home()
        config.write_text_atomic(config.home() / ACCOUNTS_FILE, json.dumps(known, indent=1))


class AccountsMixin:
    """Applier's accounts, password resets and sign-in."""

    if TYPE_CHECKING:  # (the applier's own)
        srv: Any
        _wake: asyncio.Event

        def _stop_for_what_is_left(self, run: Run, gaps: str, pending: list[dict[str, Any]],
                                   missing_files: list[dict[str, Any]], gap_code: str = "profile_gap") -> bool: ...

        def mail_login(self) -> tuple[str, str] | None: ...

        def _log(self, run: Run, text: str) -> None: ...

        def _pause(self, run: Run, need: str, reason: str, questions: list[dict[str, Any]] | None = None,
                   seen: dict[str, Any] | None = None, *, code: str) -> None: ...

        async def _look(self, text: bool = True) -> tuple[dict[str, Any], str]: ...

        @staticmethod
        async def _until(seconds: float, step: float, check: Callable[[], Awaitable[bool]]) -> bool: ...

        async def _said_terms(self, boxes: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

        async def _fill_once(self, run: Run, data: dict[str, Any]) -> dict[str, dict[str, Any]]: ...

        async def _profile_fill(self, run: Run, **how: Any) -> dict[str, Any]: ...

        def _note_skipped(self, run: Run, result: dict[str, Any]) -> None: ...

        def _entry_gaps(self, run: Run, data: dict[str, Any],
                        pending: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], str]: ...

        async def _answer_notice(self, run: Run, data: dict[str, Any], notice: dict[str, Any], agreed: set[str],
                                 over_form: bool) -> str | None: ...

        async def _attest(self, run: Run, pending: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

        async def _said_above(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]: ...

        async def _bring_forward(self, run: Run) -> None: ...

    @staticmethod
    def _account_button(actions: list[dict[str, Any]], pattern: re.Pattern[str],
                        full: bool = True) -> dict[str, Any] | None:
        """A page's own button for an account step: not a cookie banner's, a footer box's, or a
        sign-in with another site's account, nor one that signs them up ("Email me jobs like this": _signs_them_up)."""
        return next((a for a in actions if not a.get("disabled") and not a.get("cookie") and not a.get("aside")
                     and not pipeline._signs_them_up(a)
                     and not pipeline._SOCIAL.search(a["text"])
                     and (pattern.match if full else pattern.search)(final_text(a["text"]).strip())
                     and not (not full and pipeline._NOT_A_STEP.search(a["text"]))), None)

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
        if pipeline._captcha_on(data) or pipeline._security_asked(data) and not pipeline._security_answered(data):
            return False
        srv = self.srv
        form = data  # (the page as a whole: what _wait_for_account sees change)
        # its boxes, buttons and statement: the saved password's own place's (_password_view), never another site's
        # frame's around it (a talent community widget's terms box and "Sign Up" before iCIMS' frame)
        data = pipeline._password_view(data)
        statement = pipeline._privacy_statement(data)
        if statement is not None:
            if not await self._accept_statement(run, data, statement):
                return False
            form, _ = await self._look(text=False)  # (as it is once accepted)
            data = pipeline._password_view(form)
        terms = await self._said_terms(pipeline._terms_boxes(data))
        boxes = terms + pipeline._new_candidate_boxes(data, run.company) + pipeline._other_positions_boxes(data)
        own = [a for a in data.get("actions") or [] if a.get("account_form") or a.get("in_account_form")
               or a.get("form_submit") and a.get("after_password")]
        button = self._account_button(own, pipeline._MAKE_ACCOUNT)
        if button is None:
            return False
        run.did("picked", boxes)
        if boxes and not (await srv.fill_form([{"id": f["id"], "value": pipeline._tick(f)} for f in boxes])).get("ok"):
            return False
        run.did("pressed", button)
        try:
            await srv.browser.click(button["id"], allow_submit=True)  # (the form's own button: it creates the account)
        except Exception:
            return False
        run.accounts_tried += 1
        site = pipeline._site(run, data)
        ticked = ", ".join(f"\u201c{pipeline._short(self._choice_said(f))}\u201d" for f in boxes)
        # (a user name it asks for is the email: _sign_in put it there, and signs in with it later)
        with_ = ("your email as its user name and your saved password" if any(
            pipeline._user_name_box(f) for f in data.get("fields") or [] if f.get("kind") in ("text", "email"))
                 else "your saved password")
        self._log(run, f"pressed \u201c{button['text'].strip()}\u201d to create your account on {site} with {with_}"
                  + (f", after ticking {ticked}" if boxes else ""))
        self._keep_ticked(run, terms + pipeline._other_positions_boxes(data), button["text"].strip())
        run.account_made = (f"created your account on {site} with {with_}" + (" and agreed to its terms"
                            if terms else "") + (" and accepted its data privacy statement" if statement else "")
                            + pipeline._manage_hint())
        await self._wait_for_account(form)
        return True

    async def _account_card(self, run: Run, data: dict[str, Any]) -> list[dict[str, Any]]:
        """An account form's security questions put to the person on the desk's card (_account_card_boxes), with their
        choices (a list's, opened to read them), with settings.manage_accounts: answered there, they go into this form
        alone (Run.account_answers), never remembered, and the desk creates the account. Empty when there's nothing it
        can ask there."""
        # (the job system's own form's questions: never a box in another site's frame, where the answers would go)
        data = pipeline._account_view(data)
        if (not pipeline._may_manage_accounts()
                or not pipeline._account_card_boxes(data.get("fields") or [], data.get("headings"))):
            return []
        try:
            full = pipeline._account_view(await self.srv.inspect_form(include_dropdown_options=True))
        except Exception:  # (gone, or changed: the card as it was)
            return []
        keep = ("id", "kind", "label", "section", "sublabel", "required", "options", "role")
        return [{**{k: f[k] for k in keep if f.get(k) not in (None, [])}, "account": True}
                for f in pipeline._account_card_boxes(full.get("fields") or [], full.get("headings"))]

    async def _fill_account_answers(self, run: Run, data: dict[str, Any]) -> str:
        """The person's answers to an account form's security questions (given on the card: Run.account_answers) put
        into it, only with an answer for every one of its boxes, straight into the browser, each box marked as theirs
        first (Browser._keep_secret: never in a snapshot, a log or a report, said only as answers filled in). Used once,
        whatever comes of it. "filled" once every box reads back as filled by the desk (_security_answered), which lets
        its button be pressed; "failed" when they didn't all go in (what went wrong unsaid: it may name an answer);
        "" with no answers for its boxes."""
        # (only the job system's own form's boxes: the person's answers never go into another site's frame)
        boxes = pipeline._account_card_boxes(pipeline._account_view(data)["fields"], data.get("headings"))
        answers, run.account_answers = run.account_answers, {}
        if not boxes or not all(pipeline.placed_key(f) in answers for f in boxes):
            if answers:  # (the form changed since its card: asked again, said so)
                self._log(run, "your answers to the security questions didn't match the form on "
                          f"{pipeline._site(run, data)} any more, so they're asked again")
            return ""
        run.did("typed", "your answers to its security questions")  # (never a box's label: it says the question chosen)
        results = await self.srv.browser.fill([{"id": f["id"], "value": answers[pipeline.placed_key(f)], "secret": True}
                                               for f in boxes])
        now, _ = await self._look(text=False)
        if not results or not all(r.get("ok") for r in results) or not pipeline._security_answered(now):
            self._log(run, "couldn't put your answers to the security questions into the form on "
                      f"{pipeline._site(run, data)}")
            return "failed"
        self._log(run, f"filled in your answers to the security questions on {pipeline._site(run, data)}")
        return "filled"

    async def _accept_statement(self, run: Run, data: dict[str, Any], link: dict[str, Any]) -> bool:
        """A Create Account form's data privacy statement (_privacy_statement), which the site makes no
        account without: its link pressed to open it (a dialog over the page, drawn a moment after) and
        its Accept pressed for the person, as for any employer notice the desk agrees to (_answer_notice:
        settings.accept_notices, never in practice mode; said in the log; never Decline). True once it's
        accepted. False when it isn't allowed, wouldn't open (SuccessFactors' won't before a country is
        chosen) or isn't one to agree to for them: the person's, left open for them to read."""
        if not pipeline._may_accept_notices():
            return False
        said = link["text"].strip()
        notice = None
        for _ in range(2):  # (live, APS's once opened nothing at the first press: a second press after a moment)
            run.did("pressed", link)
            try:
                await self.srv.browser.click(link["id"])
            except Exception as e:  # gone, or it won't take a click: the person's, as without the setting
                self._log(run, f"the click on “{said}” failed: {pipeline._error_said(e)}")
                return False
            deadline = time.monotonic() + pipeline.ACCOUNT_STEP_WAIT
            while True:
                now, _ = await self._look(text=False)
                # (the statement over the password's own place: _make_account)
                notice = pipeline._notice(pipeline._password_view(now))
                if notice is not None or time.monotonic() > deadline:
                    break
                await asyncio.sleep(0.5)
            if notice is not None or now.get("dialogs"):
                break  # (a dialog of another sort opened: not pressed again over it)
            link = next((a for a in pipeline._password_view(now)["actions"] if a.get("text", "").strip() == said), link)
        # (each way it can fail is said in the log: APS's, live, Oct 2026, failed with no clue why)
        if notice is None:
            if now.get("dialogs"):  # (one not known to be the job system's own: no button to read, or another site's)
                self._log(run, f"I pressed “{said}”, and something opened over the page that I can't accept for you")
                return False
            told = "; ".join(e for e in now.get("errors") or [] if not pipeline._ERROR_COUNT.match(e.strip()))[:200]
            self._log(run, f"I pressed “{said}” twice, and no statement opened over the page"
                      + (f" (it says “{told}”)" if told else ""))
            return False
        run.statement_accepted = await self._answer_notice(run, pipeline._password_view(now), notice, set(),
                                                           over_form=False) == "agreed"
        if not run.statement_accepted:
            self._log(run, f"“{pipeline._short(pipeline._notice_name(notice))}” opened over the page, and I couldn't "
                      "accept it for you")
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
        site = pipeline._site(run, data)
        said = button["text"].strip()
        # (its own boxes and buttons: the account's place's, never another site's frame around it: _account_view)
        if await self._fill_account_details(pipeline._account_view(data)["fields"], data.get("headings")):
            self._log(run, f"filled the rest of {site}'s Create Account form with your details")
        why = ""
        # (a CAPTCHA, and security questions' answers, are the person's, never made up: never pressed for them; theirs
        # given on the card go in, and it's pressed then)
        if (pipeline._may_manage_accounts() and not run.account_finished and run.account_answers
                and pipeline._security_asked(data) and not pipeline._security_answered(data)):
            if await self._fill_account_answers(run, data) == "failed":
                why = "your answers to its security questions wouldn't go into it"
            data, _ = await self._look(text=False)
        captcha, security = (pipeline._captcha_on(data),
                             pipeline._security_asked(data) and not pipeline._security_answered(data))
        if pipeline._may_manage_accounts() and not run.account_finished and not captcha and not security and not why:
            now, _ = await self._look(text=False)
            terms = await self._said_terms(pipeline._terms_boxes(pipeline._account_view(now)))
            boxes = terms + pipeline._other_positions_boxes(pipeline._account_view(now))
            pressable = None
            run.did("picked", boxes)
            if boxes and not (await srv.fill_form([{"id": f["id"], "value": pipeline._tick(f)}
                                                   for f in boxes])).get("ok"):
                why = "its terms box wouldn't tick"
            else:  # greyed out until its terms are ticked: a moment for the page to take that in
                for _ in range(10):
                    now, _ = await self._look(text=False)
                    named = [a for a in pipeline._account_view(now)["actions"] if a.get("text", "").strip() == said
                             and not pipeline._signs_them_up(a)]
                    pressable = next((a for a in named if not a.get("disabled")), None)
                    if pressable is not None or not named:
                        break
                    await asyncio.sleep(0.5)
                if pressable is None:
                    why = f"“{said}” stayed greyed out"
                elif (pipeline._captcha_on(now) or pipeline._security_asked(now)
                        and not pipeline._security_answered(now)):  # (drawn since it was read)
                    pressable = None
                    why = ("it shows a CAPTCHA, which is yours to solve" if pipeline._captcha_on(now) else
                           "it asks security questions too, whose answers are yours to choose")
            if pressable is not None:
                run.did("pressed", pressable)
                try:
                    await srv.browser.click(pressable["id"], allow_submit=True)  # (it creates the account)
                except Exception as e:  # (said in the log: Nikon's UKG Pro, live, Oct 2026, gave no clue why)
                    why = f"“{said}” wouldn't take a click"
                    self._log(run, f"the click on “{said}” failed: {pipeline._error_said(e)}")
                else:
                    run.account_finished = True
                    ticked = ", ".join(f"“{pipeline._short(self._choice_said(f))}”" for f in boxes)
                    self._log(run, f"pressed “{said}” to finish creating your account on {site}"
                              + (f", after ticking {ticked}" if boxes else ""))
                    self._keep_ticked(run, boxes, said)
                    run.account_made = pipeline._account_made_says(site, terms=bool(terms)
                                                                   or "agreed to its terms" in run.account_made,
                                                                   password=bool(run.account_made))
                    await self._wait_to_leave(now, said)
                    return True
        await self._bring_forward(run)
        now, _ = await self._look(text=False)
        run.page_info = pipeline._page_info(now)
        if run.account_finished or why:
            wants = pipeline._account_wants(data, now) or "see the page in the desk's browser window."
            done = f"I pressed “{said}”" if run.account_finished else f"I couldn't finish it ({why})"
            self._pause(run, "sign_in", f"{site} asked for a few more details to create your account, and {done}; it "
                        f"wants something more: {wants} Finish it there; the desk carries on after that.", seen=now,
                        code="account_form_more")
        else:
            card = await self._account_card(run, now) if security and not captcha else []
            if card:
                self._pause(run, "sign_in", f"{site} asks for a few more details to create your account. I filled in "
                            "what your profile has; its security questions and their answers are yours to choose (the "
                            "desk never makes up answers): answer them here (they go into this form alone, and are "
                            "never remembered) and the desk finishes the account, or fill them in in the desk's "
                            f"browser window, tick its terms box if you agree and press “{said}” there; "
                            "the desk carries on after that.",
                            card, seen=now, code="security_questions")
                return False
            asks = (" its security questions and their answers are yours to choose (the desk never makes up answers): "
                    "fill them in," if security else "") + (" solve its CAPTCHA (that's yours)," if captcha else "")
            self._pause(run, "sign_in", f"{site} asks for a few more details to create your account. I filled in what "
                        f"your profile has;{asks} tick its terms box if you agree and press “{said}” in the desk's "
                        "browser window (then verify your email if they ask); the desk carries on after that.",
                        seen=now, code="account_captcha" if captcha else "security_questions" if security
                        else "account_form_filled")
        return False

    async def _wait_to_leave(self, form: dict[str, Any], button: str) -> None:
        """After a press that sends a page (the rest of Create Account): until the site has done with
        it (gone on, its button gone, or something said about it), as _wait_for_account."""
        async def done() -> bool:
            try:
                now = await self.srv.inspect_form(include_dropdown_options=False)
            except Exception:  # mid-way to the next page
                return False
            return (now.get("url") != form.get("url") or (now.get("errors") or []) != (form.get("errors") or [])
                    or not any(a.get("text", "").strip() == button for a in now.get("actions") or []))

        await self._until(pipeline.ACCOUNT_WAIT, 1, done)

    async def _wait_for_account(self, form: dict[str, Any]) -> None:
        """After Create Account: until the site has done with the form (gone on, or said something
        about it). Looked at again sooner, the same form would be filled in a second time and taken
        for one that wants something more."""
        async def done() -> bool:
            try:
                now = await self.srv.inspect_form(include_dropdown_options=False)
            except Exception:  # mid-way to the next page
                return False
            passwords = sum(f.get("kind") == "password" for f in now.get("fields") or [])
            return (now.get("url") != form.get("url") or passwords < 2
                    or (now.get("errors") or []) != (form.get("errors") or []))

        await self._until(pipeline.ACCOUNT_WAIT, 1, done)

    async def _make_email_account(self, run: Run, data: dict[str, Any], create: dict[str, Any]) -> bool:
        """settings.manage_accounts, on an email-first sign-in that has no account for the email (an
        Eightfold site's, live, Oct 2026: "We don't recognize this email. Create a new account"):
        press its way to a new one ("Create an account"), and take its steps (_email_account_step):
        its agreement, the email again, then the code it emails (the inbox's, or the person's). An
        Eightfold site asks no password for it (Northrop Grumman's hides the field; another offers a
        one-time code in its place), so no saved password goes on it. Once a job (Run.accounts_tried).
        True: on its way (the next look carries on); False: paused for the person."""
        site = pipeline._site(run, data)
        if data.get("captcha") or data.get("challenge"):
            return await self._account_theirs(run, data,
                                        f"{site} has no account for your email yet, and its sign-in shows "
                                        "a picture check (CAPTCHA), which is yours", code="account_theirs")
        run.accounts_tried += 1
        run.did("pressed", create)
        try:
            await self.srv.click(create["id"])
        except Exception:  # gone, or it won't take a click: the person's, as without the setting
            return await self._account_theirs(run, data, f"{site} has no account for your email yet, and its "
                                        f"“{create['text'].strip()}” didn't take my click", code="account_step_failed")
        self._log(run, f"{site} has no account for your email, so I pressed “{create['text'].strip()}” to make "
                  "one" + pipeline._manage_hint())
        now, _ = await self._after_press(data)
        view = pipeline._email_account_view(now)
        if view == "code":
            return True  # its emailed code: the main loop's (the inbox's, or the person's)
        if view is None:
            return await self._account_theirs(run, now, f"I pressed {site}'s “{create['text'].strip()}”, as it has "
                                        "no account for your email yet, and it showed a step I don't know",
                                              code="account_step_failed")
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
        site = pipeline._site(run, data)
        if data.get("captcha") or data.get("challenge"):
            return await self._account_theirs(run, data, f"{site}'s steps of making your account show a picture check "
                                        "(CAPTCHA), which is yours", code="account_theirs")
        ticked: list[dict[str, Any]] = []
        pattern = pipeline._ONE_TIME_CODE if view == "options" else pipeline._ACCOUNT_ON
        button = self._account_button(data.get("actions") or [], pattern)
        deadline = time.monotonic() + pipeline.ACCOUNT_STEP_WAIT
        while button is None and time.monotonic() < deadline:
            # its boxes drawn before its button (Northrop's agreement, live, Oct 2026): a moment more
            await asyncio.sleep(0.5)
            now, _ = await self._look(text=False)
            if pipeline._email_account_view(now) != view:
                return True  # it went on by itself: the main loop's next look
            data, button = now, self._account_button(now.get("actions") or [], pattern)
        if button is None:
            return await self._account_theirs(run, data,
                                        f"I couldn't find the button that goes on with making your account "
                                        f"on {site}", code="no_step_button")
        if view == "consent":
            ticked = pipeline._consent_boxes(data)
            run.did("picked", ticked)
            if ticked and not (await srv.fill_form([{"id": f["id"], "value": True} for f in ticked])).get("ok"):
                return await self._account_theirs(run, data,
                                            f"{site} asks you to agree to its terms before making your "
                                            "account, and its box didn't take my tick", code="account_step_failed")
            for f in ticked:
                self._log(run, f"ticked “{pipeline._short(f.get('label') or '')}” to make your account on {site}")
            self._keep_ticked(run, ticked, button["text"].strip())
            if not await self._give_other_positions(run, data, pipeline._other_positions_boxes(data)):
                return await self._account_theirs(run, data, f"{site} asks you to agree to be considered for other "
                                            "open positions before making your account, and its choice didn't take my "
                                            "click", code="account_step_failed")
        elif view == "email":
            address = config.Profile.load().get("personal.email")
            box = next(f for f in data.get("fields") or [] if not f.get("disabled") and not f.get("aside"))
            if not address:
                return await self._account_theirs(run, data,
                                            f"{site} asks for your email to make your account, and your "
                                            "profile has none", code="profile_gap")
            if str(box.get("value") or "").strip().lower() != address.strip().lower():
                run.did("typed", box)
                await srv.fill_form([{"id": box["id"], "value": address}])
        run.did("pressed", button)
        try:
            # (its agreement's "Submit" sends that agreement only: the application isn't on the page yet)
            await srv.browser.click(button["id"], allow_submit=True)
        except Exception:
            return await self._account_theirs(run, data, f"{site}'s “{button['text'].strip()}” didn't take my click "
                                        "while I was making your account there", code="account_step_failed")
        if view == "email":
            self._log(run, f"gave {site} your email for the new account and pressed “{button['text'].strip()}”")
        elif view == "options":
            self._log(run, f"chose “{button['text'].strip()}” on {site}: the account needs no password there")
        now, _ = await self._after_press(data)
        wanted = pipeline._other_positions_boxes(now, required=False) if view == "consent" else []
        others = [f for f in now.get("fields") or [] if not f.get("disabled") and not f.get("aside")
                  and is_empty_value(f.get("value")) and f not in wanted]
        if (wanted and not others and pipeline._email_account_view(now) == view
                and pipeline._page_sig(now) == pipeline._page_sig(data)
                and (now.get("errors") or []) != (data.get("errors") or [])):
            # refused without its consent to be considered for other open positions too: given, and pressed again
            again = self._account_button(now.get("actions") or [], pipeline._ACCOUNT_ON)
            if again is not None and await self._give_other_positions(run, now, wanted, refused=True):
                run.did("pressed", again)
                with contextlib.suppress(Exception):
                    await srv.browser.click(again["id"], allow_submit=True)
                    now, _ = await self._after_press(now)
        if pipeline._email_account_view(now) not in (view, None) or pipeline._page_sig(now) != pipeline._page_sig(data):
            return True  # on to its next step (the main loop's next look); a page that stayed (BrassRing's email
            # step, which has no view of its own) is said, never pressed again
        # still there: a choice left that's the person's, or what the site said
        said = "; ".join(e for e in now.get("errors") or []
                         if not pipeline._ERROR_COUNT.match(e.strip()))[:200].rstrip(" .")
        left = [f for f in now.get("fields") or [] if not f.get("disabled") and not f.get("aside")
                and is_empty_value(f.get("value"))]
        if view == "consent" and left:
            named = " and ".join(f"“{self._choice_said(f)}”" for f in left[:2])
            agreed = f" and ticked “{pipeline._short(ticked[0].get('label') or '')}”" if ticked else ""
            return await self._account_theirs(run, now, f"{site} has no account for your email yet, so I began making "
                                        f"one there{agreed}. It also asks for {named}"
                                        + (f" (it says “{said}”)" if said else "")
                                        + ", which the desk never chooses for you: choose it and press its "
                                        f"“{button['text'].strip()}” in the desk's browser window if you're happy to "
                                        "(or sign in with the email you use there). The desk then makes the account "
                                        "with your email and the code it emails you, and carries on",
                                        tail=False, code="account_theirs")
        return await self._account_theirs(run, now, f"I pressed “{button['text'].strip()}” while making your "
                                    f"account on {site}, and it didn't go on" + (f": “{said}”" if said else ""),
                                          code="account_step_failed")

    @classmethod
    def _keep_ticked(cls, run: Run, boxes: list[dict[str, Any]], button: str) -> None:
        """An account form's boxes ticked for the person (its terms, a consent it requires), each kept in its own words,
        the choice given (_tick's, for a choice and not a tick), and the words above it that it agrees to where those
        were read (_said_terms), each apart: Run.agreed."""
        for f in boxes:
            value = pipeline._tick(f)
            choices = [str(v) for v in value] if isinstance(value, list) else [value] if isinstance(value, str) else []
            run.keep_agreed("account box", clean_label(f.get("label") or ""), button, str(f.get("words_above") or ""),
                            choices)

    async def _give_other_positions(self, run: Run, data: dict[str, Any], fields: list[dict[str, Any]],
                                    refused: bool = False) -> bool:
        """Give an account form's consents to be considered for other open positions (_OTHER_POSITIONS),
        which the site won't make the account without (`fields`: marked required, or `refused`: it
        said so when its button was pressed), logging each. False when one wouldn't take it."""
        if not fields:
            return True
        run.did("picked", fields)
        if not (await self.srv.fill_form([{"id": f["id"], "value": pipeline._tick(f)} for f in fields])).get("ok"):
            return False
        why = "it wouldn't go on without it" if refused else "it requires it"
        for f in fields:
            self._log(run, f"chose “{self._choice_said(f)}” to make your account on {pipeline._site(run, data)}, "
                      f"as {why} (settings.manage_accounts)")
            run.went_in.agree(f"chose “{self._choice_said(f)}” to make your account on {pipeline._site(run, data)}, "
                              f"as {why}")
        self._keep_ticked(run, fields, "")
        return True

    @staticmethod
    def _choice_said(field: dict[str, Any]) -> str:
        """A box or choice left for the person, in their words: a group's name and its choice ("Contact
        Consent: I understand that I may be considered for other open positions…")."""
        label = clean_label(field.get("label") or "")
        options = [o for o in field.get("options") or [] if isinstance(o, str)]
        said = (f"{label}: {options[0]}" if len(options) == 1 and label
                else label or (options[0] if options else "a box"))
        return said if len(said) <= 160 else said[:158].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"

    async def _account_theirs(self, run: Run, data: dict[str, Any], why: str, tail: bool = True, *,
                              code: str) -> bool:
        """Pause on a step of making an account that's the person's (False, for the step's caller). `code`: the stop's
        (STOP_CODES): account_theirs for a step that's theirs (a CAPTCHA, a choice), another where the desk's own
        step didn't work (a click not taken, a button not found)."""
        await self._bring_forward(run)
        self._pause(run, "sign_in", why + (". Finish making the account in the desk's browser window (or sign in with "
                                           "the email you use there); the desk carries on by itself after that."
                                           if tail else "."), seen=data, code=code)
        return False

    async def _after_press(self, before: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """The page once a step of making an account has done something after its button: drawn its
        next step, or said something. Looked at sooner, an Eightfold site's modal still shows the
        step just done while it asks its server."""
        deadline = time.monotonic() + pipeline.ACCOUNT_STEP_WAIT
        while True:
            data, text = await self._look()
            if (pipeline._page_sig(data) != pipeline._page_sig(before)
                    or (data.get("errors") or []) != (before.get("errors") or []) or time.monotonic() > deadline):
                return data, text
            await asyncio.sleep(0.5)

    # ----------------------------------------------------- password resets
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
        site = pipeline._site(run, data)

        async def stop(why: str, page: dict[str, Any]) -> str:
            run.resetting = False
            await self._bring_forward(run)
            self._pause(run, "sign_in", f"Your saved password didn't sign in on {site}, and I {why}. Sign in in the "
                        "desk's browser window and the desk carries on after that; or finish the reset there, to the "
                        "password you saved on the desk, then press Carry on: the desk signs in with it.", seen=page,
                        code="sign_in_refused")
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
            run.did("pressed", action)
            try:
                out = await (srv.browser.click(action["id"], allow_submit=True) if allow_submit
                             else srv.click(action["id"]))
            except Exception:  # gone (a page drawn again after the failed sign-in) or won't take a click
                return False
            return out.get("clicked") is not False

        # (each step's buttons and boxes: the saved password's own place's, never another site's frame around it, an
        # employer's "Next" or "Email me jobs like this": _password_view)
        forgot = pipeline._forgot_action(pipeline._sign_in_ways(data))
        if forgot is None:  # on the Create Account form: back to the sign-in page, which has the way to a reset
            # (its "Sign In" there is a link or a plain button: the form's own button creates the account, and
            # a sign-in form's own would only send the refused password again)
            back = next((a for a in pipeline._sign_in_ways(data) if not a.get("disabled")
                         and not a.get("form_submit")
                         and not a.get("account_form") and not a.get("sign_in_form") and not a.get("cookie")
                         and pipeline._SIGN_IN_ACTION.match(final_text(a["text"]).strip())), None)
            if back is None:
                return None
            if not await press(back):
                return None
            data, _ = await self._look(text=False)
            forgot = pipeline._forgot_action(pipeline._sign_in_ways(data))
            if forgot is None:
                return await stop("couldn't find its way to a password reset", data)
        run.reset_from = data.get("url") or ""
        # (the page as it is now, the refused sign-in drawn: what the press changes)
        before, _ = await self._look(text=False)
        forgot = pipeline._forgot_action(pipeline._sign_in_ways(before)) or forgot  # (its own, drawn again since)
        if not await press(forgot):
            return await instead("couldn't open its password reset", data)
        # The reset drawn, not the sign-in still on show nor a page between the two, still loading: a script's way
        # there can take a moment, and the sign-in read again was taken for a reset the desk can't fill in, so a reset
        # it could do was never asked for (the review of 0.3.156)
        deadline = time.monotonic() + pipeline.LATE_BUTTONS_WAIT
        while True:
            data, text = await self._look()
            if (pipeline._page_sig(data) != pipeline._page_sig(before) and not data.get("busy") and data.get("fields")
                    or time.monotonic() > deadline):
                break
            await asyncio.sleep(0.5)
        address = config.Profile.load().get("personal.email")
        # (the reset's own boxes and button: the page's, or its frame's on the system)
        mine = pipeline._password_view(data)
        # (never a side box's, a job alerts sign-up's "Email" beside it; and the reset's own frame's, by its headings,
        # where the look says each frame's: an iCIMS page around iCIMS' frame has an "Email me jobs like this" form)
        boxes = [f for f in mine["fields"] if f.get("kind") in ("text", "email") and not f.get("aside")
                 and re.search(r"e-?mail|user ?name|login", f.get("label") or "", re.I)]
        # the frame its "Forgot your password?" was pressed in is the reset's (iCIMS': its own page may head it
        # "Password Assistance", or not at all); else the frame its headings say is a reset's. Boxes in more than one
        # frame, and none of them named so: not one the desk can tell, the person's (the fourth review)
        own = [f for f in boxes if pipeline._frame_of(f) == pipeline._frame_of(forgot)]
        if own:
            boxes = own
        else:
            raw = data.get("frame_headings")
            framed: dict[str, Any] = raw if isinstance(raw, dict) else {}
            resets = {p for p, hs in framed.items() if pipeline._RESET_PAGE.search(" ".join(hs or []))}
            if resets:
                boxes = [f for f in boxes if pipeline._frame_of(f) in resets]
            if len({pipeline._frame_of(f) for f in boxes}) > 1:
                return await instead("opened its password reset, whose email box I couldn't tell from another form's "
                                     "on the page", data)
        if (not pipeline._RESET_PAGE.search(" ".join([text[:2000], *(data.get("headings") or [])])) or not boxes
                or not address or any(f.get("kind") == "password" for f in mine["fields"])):
            return await instead("opened its password reset, which isn't one I can fill in", data)
        # One box: as ever. More than one (a job alerts sign-up beside the reset in its frame, "Get new jobs by email"
        # and its "Submit", the reset's own in a <form> or in none): the one whose own group holds a button that names
        # the reset, and that button (_reset_choice); else the person's (the fifth and sixth reviews)
        named: dict[str, Any] | None = None
        if len(boxes) > 1:
            chosen = pipeline._reset_choice(boxes, mine["actions"])
            if chosen is None:
                return await instead("opened its password reset, whose email box I couldn't tell from another form's "
                                     "on the page", data)
            boxes, named = [chosen[0]], chosen[1]
        captcha = pipeline._captcha_on(data)
        if captcha and way_back():
            # BrassRing's asks for a picture code (live, Oct 2026), never touched: its Continue pressed
            # without one only said "Captcha text Required field"
            return await instead("opened its password reset, which has a CAPTCHA (yours to solve)", data)
        run.did("typed", boxes[:1])
        if not (await srv.fill_form([{"id": boxes[0]["id"], "value": address}])).get("ok"):
            return await instead("opened its password reset, which didn't take your email", data)
        if captcha:  # (and no new account to go back for: the site said the email has one)
            run.resetting = True
            run.reset_at = time.time()  # (the person's press, past its picture code, comes after this)
            await self._bring_forward(run)
            self._pause(run, "bot_check",
                        f"I opened {site}'s password reset for your saved password, which didn't sign "
                        "in, and filled in your email. Solve its picture code and press its button; the desk watches "
                        "your inbox for the reset email and sets your saved password as the new one.", seen=data,
                        code="captcha")
            return "paused"
        # (the email box's own form's button: "Email me jobs like this" around it matches as well, and a sign-up's
        # "Submit" in another form beside it)
        button = named or self._account_button(pipeline._box_buttons(mine["actions"], boxes[0], mine["fields"]),
                                               pipeline._RESET_ASK, full=False)
        run.reset_at = time.time()  # (its email is sent as the button is pressed: none from before is this reset's)
        if button is None or not await press(button, allow_submit=True):  # (asks for the email: sends no application)
            return await instead("filled in your email on its password reset, but couldn't press its button", data)
        data, text = await self._look()
        if data.get("errors") and not pipeline._RESET_SENT.search(text):  # "No account found for this email"
            if pipeline._NO_ACCOUNT.search(" ".join(data["errors"])) and run.reset_from and run.page is not None:
                # nothing to reset: back to the sign-in page, for its way to a new account
                self._log(run, f"{site} has no account for your email, so I went back to make one there")
                await self._back_to_sign_in(run)
                return "no_account"
            return await stop(f"asked for a password reset, and it says \u201c{data['errors'][0][:160]}\u201d", data)
        run.resetting = True
        self._log(run, f"your saved password didn't sign in on {site}, so I asked it to email a password reset"
                  + pipeline._manage_hint())
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
        if not fields and pipeline._SPENT_PAGE.search(text[:3000]):
            gone = " couldn't be opened again"
        elif (any(f.get("kind") == "password" for f in fields) and pipeline._forgot_action(actions) is None
              and pipeline._account_way(actions) is None):
            gone = ", opened again, had no way to a new account or a password reset"
        elif not run.reopened_elsewhere and pipeline._no_sign_in_here(run, data) and await self._still_no_sign_in(run):
            # its session gone, it led elsewhere: SuccessFactors' to the company's careers search home (Edwards
            # Vacuum's, after three minutes waiting on a reset email, the plus probe Oct 11), with only a Sign In.
            # Still so a moment later (not a sign-in still being drawn), and once a job
            run.reopened_elsewhere = True
            gone = ", opened again, led to the site's job search, not its sign-in"
        else:
            return
        self._log(run, f"{pipeline._site(run, data)}'s sign-in page{gone}, so I opened the job again")
        run.reopened += 1
        await self.srv.open_application(job_id=run.job_id)

    async def _still_no_sign_in(self, run: Run) -> bool:
        """Still no sign-in a moment later (not one still being drawn)?"""
        await asyncio.sleep(pipeline.LEFT_SIGN_IN_SETTLE)
        data, _ = await self._look(text=False)
        return pipeline._no_sign_in_here(run, data)

    async def _register_from_posting(self, run: Run, looked: tuple) -> bool:
        """The desk is on its way to a new account at a job system (_back_to_sign_in opened the job again
        for the sign-in page's way to one), and the posting's "Apply" went nowhere (Benchmark's Infor, live,
        Oct 2026: once a sign-in was refused in the tab, Apply no longer opened the sign-in again). The
        posting's own "Register" (or "Create Account") is the way instead, as the sign-in page's would have
        been: pressed, and its form read and filled in like any Create Account. Only where the page is
        still as it was before Apply was pressed (a slow Apply isn't one that went nowhere). True once
        it's pressed."""
        data, _ = await self._look(text=False)
        way = pipeline._account_way(data.get("actions") or []) if pipeline._fingerprint(data) == looked else None
        if way is None:
            return False
        run.did("pressed", way)
        try:
            clicked = await self.srv.click(way["id"])
        except KeyError:  # the page changed between looking and clicking
            return False
        if clicked.get("clicked") is False:
            return False
        run.page = self.srv.browser.current_tab or run.page
        self._log(run, f"“Apply” on {pipeline._site(run, data)}'s posting went nowhere, so I pressed its "
                  f"“{way['text'].strip()}” to make an account there instead")
        return True

    async def _await_reset_email(self, run: Run, data: dict[str, Any]) -> None:
        watched = self.mail_login() is not None
        if not watched:
            await self._bring_forward(run)
        watching = (" The desk is watching your inbox for it, and sets your saved password as the new one. If none "
                    f"comes in {pipeline.RESET_MAIL_WAIT // 60} minutes (most likely you have no account there), it "
                    "makes one instead. The other jobs carry on meanwhile."
                    if watched else " Open the link in the email, set the password you saved on the desk as "
                    "the new one, then press Carry on: the desk signs in with it (an email app password saved on the "
                    "desk lets it do all this itself).")
        run.reset_waited = True
        self._pause(run, "email_code",
                    f"{pipeline._site(run, data)} is emailing you a password reset: your saved password didn't "
                    "sign in there." + watching, seen=data, code="reset_pending")
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
        run.sign_in_tries = min(run.sign_in_tries, pipeline.SIGN_IN_TRIES - 1)
        site = pipeline._site(run, data)
        resend = next((a for a in pipeline._account_view(data)["actions"]
                       if not a.get("disabled") and not a.get("cookie") and not pipeline._signs_them_up(a)
                       and not a.get("form_submit")
                       and pipeline._RESEND_VERIFY.match(final_text(a.get("text") or "").strip())), None)
        label = final_text(resend["text"]).strip() if resend is not None else ""
        # an account from before, by the page's words: the refusal in its alerts, and no word of an email just sent
        stale = (pipeline._VERIFY_FIRST.search(" ".join(data.get("errors") or [])) is not None
                 and pipeline._VERIFY_SENT.search(" ".join([*(data.get("errors") or []), text[:3000]])) is None)
        said = (f"{site} won't sign in until your account there is verified from the link it emailed." if stale
                else f"{site} emailed you a link to verify your new account, and won't sign in until it's opened.")
        if self.mail_login() is not None:
            watching = ("The desk is watching your inbox for it: it opens the link, then "
                        "signs in with your saved password.")
            if stale and resend is not None and not run.verify_resent:
                try:
                    run.did("pressed", resend)
                    await self.srv.browser.click(resend["id"])
                    pressed = True
                except Exception:  # gone (the page drawn again) or won't take a click
                    pressed = False
                if pressed:
                    run.verify_resent = True  # (once a job)
                    # (the page as it is now, paused on: the site's answer to the press)
                    data, text = await self._look()
                    if pipeline._SEND_FAILED.search(" ".join([*(data.get("errors") or []), text[:3000]])):
                        self._log(run, f"pressed \u201c{label}\u201d, and {site} says it couldn't send the email")
                        watching = (f"The desk pressed \u201c{label}\u201d, and {site} says it couldn't send the "
                                    "email: press it yourself in the desk's browser window a little later. The desk "
                                    "is watching your inbox for the new email: it opens the link, then "
                                    "signs in with your saved password.")
                    else:
                        self._log(run, f"pressed \u201c{label}\u201d, so {site} emails the link again")
                        watching = (f"The desk pressed \u201c{label}\u201d and is watching your inbox for the new "
                                    "email: it opens the link, then signs in with your saved password.")
                else:
                    watching = (f"The desk couldn't press its \u201c{label}\u201d: press it in the desk's browser "
                                "window, and the desk opens the new email's link from your inbox, then "
                                "signs in with your saved password.")
            elif stale and resend is not None:  # (pressed before for this job: the email it sent is the one to find)
                watching = (f"The desk is watching your inbox for it (its \u201c{label}\u201d in the desk's browser "
                            "window sends it again): it opens the link, then signs in with your saved password.")
            self._pause(run, "email_code", f"{said} {watching} The other jobs carry on meanwhile.", seen=data,
                        code="verify_pending")
            run.blocking, run.left = False, True  # nothing for the person to do: the queue goes on
            self._wake.set()
        else:
            await self._bring_forward(run)
            again = f" (its \u201c{label}\u201d sends a new one)" if stale and resend is not None else ""
            self._pause(run, "sign_in", f"{said} Open the link in that email{again}, then press Carry on: the desk "
                        "signs in with your saved password (an email app password saved on the desk lets it open the "
                        "link itself).",
                        seen=data, code="verify_pending")
        run.verifying = True

    def _reset_overdue(self, run: Run) -> bool:
        """Waiting on a reset email, with the inbox watched, that hasn't come in RESET_MAIL_WAIT (or only one whose link
        was opened already, Run.reset_spent: _drive says so on a card, and never takes it for no email at all)."""
        # (the wait's own time: not while another job's email step kept its mail from being read; and only once a look
        # made after it has found none, never in the pass that couldn't look: the review of jobs at once)
        return (run.resetting and run.reset_waited and run.status == "needs_you" and run.need == "email_code"
                and not run.mail_done and not run.reset_no_mail and self.mail_login() is not None
                and pipeline._mail_waited(run) > pipeline.RESET_MAIL_WAIT and not run.mail_held_at
                and run.mail_looked - run.paused_at - run.mail_held > pipeline.RESET_MAIL_WAIT)

    @staticmethod
    def _reset_form_showing(data: dict[str, Any], text: str, button: str) -> bool:
        """Still a password reset's form: its new-password boxes, its reset words, the button that was pressed, and
        not the site's word that the password is reset."""
        boxes = [f for f in data.get("fields") or [] if f.get("kind") == "password" and not pipeline._code_box(f)]
        pressed = any(final_text(a.get("text") or "").lower() == button for a in data.get("actions") or [])
        return (len(boxes) >= 2 and pressed and bool(pipeline._RESET_PAGE.search(text[:2000]))
                and not pipeline._RESET_DONE.search(text[:2000]))

    async def _set_new_password(self, run: Run, data: dict[str, Any], text: str) -> str | None:
        """A password reset's page (opened from its email): the saved password in its new-password
        boxes, and its button pressed. "set" when the site took it, "paused" when it's the
        person's (a picture code, or a password the site turned down); None for any other page."""
        srv = self.srv
        passwords = pipeline._new_password_boxes(data, text)
        if not passwords:
            return None
        secret = pipeline._password_place(data)[0]  # (the system whose page or frame the boxes are on)
        if secret is None or pipeline._secret(secret) is None:
            return None
        for box in passwords:
            run.did("typed", box)
            if not (await srv.fill_secret(box["id"], secret)).get("ok"):
                return None
        site = pipeline._site(run, data)
        if data.get("captcha") or data.get("challenge"):
            await self._bring_forward(run)
            self._pause(run, "bot_check",
                        f"I put your saved password in {site}'s new-password boxes. Solve its picture "
                        "code and press its button; the desk carries on after that.", seen=data, code="captcha")
            return "paused"
        # (the reset's own button: never an employer's "Save Job" around iCIMS' frame)
        mine = pipeline._password_view(data)
        button = self._account_button(pipeline._box_buttons(mine["actions"], passwords[0], mine["fields"]),
                                      pipeline._RESET_SET, full=False)
        run.did("pressed", button)
        try:
            pressed = (button is not None
                       and (await srv.browser.click(button["id"], allow_submit=True)).get("clicked") is not False)
        except Exception:
            pressed = False
        after, after_text = await self._look()
        said_on = final_text(button.get("text") or "").lower() if button is not None else ""
        # A site that takes a moment over the new password leaves its reset form on show, with no error, until it goes
        # on to its sign-in (Workday's, live, issue #112, note 34): waited for, or the form is read as a sign-in
        # with no email box and the person is asked to sign in. Still there after RESET_SETTLE, it wasn't taken.
        deadline = time.monotonic() + pipeline.RESET_SETTLE
        while (pressed and time.monotonic() < deadline and self._reset_form_showing(after, after_text, said_on)
               and not after.get("errors")):
            await asyncio.sleep(1)
            after, after_text = await self._look()
        if not pressed or self._reset_form_showing(after, after_text, said_on):
            await self._bring_forward(run)
            said = f" It says \u201c{after['errors'][0][:160]}\u201d." if after.get("errors") else ""
            self._pause(run, "sign_in",
                        f"I put your saved password in {site}'s new-password boxes, and it didn't take it "
                        f"as the new one.{said} Set it there (or another, and save that one on the desk), then sign "
                        "in; the desk carries on after that.", seen=after, code="reset_failed")
            return "paused"
        if refused := pipeline._reset_refused(after, after_text):
            # its form gone, and the site's words that it set nothing (its link spent, or a password it had before):
            # never said to be set (KLA's Workday, the reset probe's second pass, run 136, a spent link's)
            await self._bring_forward(run)
            self._pause(run, "sign_in",
                        f"I put your saved password in {site}'s new-password boxes and pressed its button, "
                        f"and it says \u201c{refused.rstrip(' .')}\u201d: it didn't set it as the new one. Reset it "
                        "there (through its \u201cForgot password\u201d again if the link is spent) to the one you "
                        "saved on the desk, or to another and save that one on the desk, then press Carry on: the desk "
                        "signs in with it.", seen=after, code="reset_failed")
            return "paused"
        run.resetting = False
        run.reset_set = run.account_known = True
        note_account(run.reset_from or run.url, "reset email")  # (an account of theirs there: _sign_in_lap)
        self._log(run, f"set your saved password as {site}'s new one")
        return "set"

    async def _theirs_to_send(self, run: Run, data: dict[str, Any], button: dict[str, Any]) -> None:
        srv = self.srv
        srv._mark_ready(srv.tracker().get(run.job_id), "filled by the Job Desk; its own button sends it")
        await self._bring_forward(run)
        self._pause(run, "your_submit", f"Filled as far as the desk can. On {pipeline._site(run, data)} the button "
                    f"that sends the application reads \u201c{button['text'].strip()}\u201d, so it's yours to press: "
                    "check the page in the desk's browser window, press it there, then press \u201cI "
                    "submitted it myself\u201d here.", code="your_submit")

    async def _sign_up_pause(self, run: Run, data: dict[str, Any], button: dict[str, Any]) -> None:
        await self._bring_forward(run)
        self._pause(run, "stuck", f"{pipeline._site(run, data)} asks for your name and email first, and its "
                    f"\u201c{button['text'].strip()}\u201d sends them before the application itself, so it's yours to "
                    "press: press it in the desk's browser window if you're happy to, then press Carry on.",
                    code="sends_first")

    # ------------------------------------------------------------- sign-in
    async def _signing_in_still(self) -> bool:
        """After a sign-in was sent, its form still on show and nothing said wrong: a site that takes a moment
        to sign in (Intel's and TEL's Workday took a few seconds, nightly live check Oct 11). True when the
        password box goes within SIGN_IN_SETTLE seconds: the sign-in went through."""
        deadline = time.monotonic() + pipeline.SIGN_IN_SETTLE
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            data, _ = await self._look(text=False)
            if data.get("errors"):
                return False
            if not any(f.get("kind") == "password" and not pipeline._code_box(f) for f in data.get("fields") or []):
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
        secret, places = pipeline._password_place(data)
        saved = pipeline._secret(secret) if secret is not None else None
        if secret is None or saved is None:
            return None
        key = hashlib.sha256(saved.encode()).hexdigest()
        if key != run.sign_in_key:  # a password saved since: not the one the site refused
            run.sign_in_key, run.sign_in_tries = key, 0
            run.reset_set = run.reset_opened = False  # (nor the one a reset set: its refusal is the ordinary path's)
        # the boxes and buttons of the page and its frames on the password's own system: never a frame from another site
        fields = [f for f in data.get("fields") or [] if pipeline._frame_of(f) in places]
        actions = [a for a in data.get("actions") or [] if not a.get("disabled") and pipeline._frame_of(a) in places
                   and not pipeline._signs_them_up(a)]  # (never one that signs them up: "Email me jobs like this")
        if not fields:
            # Workday's "Sign in with email"; or, after its own password reset, its "Password has been reset"
            # and a "Sign In" that opens the form (issue #112, note 14, live test 2, Oct 2026)
            email_button = (next((a for a in actions if re.match(r"^sign in with email$", a["text"], re.I)), None)
                            or next((a for a in actions if re.match(r"^(sign|log) ?in$", a["text"].strip(), re.I)
                                     and not a.get("cookie")), None))
            if email_button is None or tried.get("email_step", 0) >= 2:
                return None
            run.did("pressed", email_button)
            await srv.click(email_button["id"])
            return "email_step"
        passwords = [f for f in fields if f["kind"] == "password" and not pipeline._code_box(f)]
        create = pipeline._account_way(actions)
        # Where the way to a new account led: a form with one password box (UKG Pro's "Create
        # your account") is the new account's, as it no longer offers a way to one.
        signing_up = len(passwords) == 1 and bool(tried.get("create_account")) and create is None
        if (len(passwords) == 1 and tried.get("submitted") and not signing_up and not data.get("errors")
                and run.sign_in_tries > 0 and not tried.get("settled")):
            tried["settled"] = 1  # (once a job, and only after a sign-in the desk sent itself)
            if await self._signing_in_still():
                return "submitted"  # it was only slow (Intel's and TEL's Workday, nightly live check Oct 11)
        if (len(passwords) == 1 and (tried.get("submitted") or run.sign_in_tries >= pipeline.SIGN_IN_TRIES)
                and not signing_up):
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
                    or reset_first and pipeline._forgot_action(actions) or run.reset_set or run.reset_opened
                    or run.account_known):
                return "refused"
            run.did("pressed", create)
            try:
                await srv.click(create["id"])
            except KeyError:
                return None
            if not tried.get("create_account"):
                self._log(run, "your saved password didn't sign in, so I opened Create Account")
            return "create_account"
        # (the sign-in's own: in its password boxes' frame, never a side box's, a job alerts sign-up's "Email" on the
        # page around iCIMS' frame)
        boxes = [f for f in fields if f["kind"] in ("text", "email") and not f.get("aside")
                 and (not passwords or pipeline._frame_of(f) == pipeline._frame_of(passwords[0]))
                 and re.search(r"e-?mail|user ?name|login", f.get("label") or "", re.I)]
        # and in its password box's own form (formjs' form index), or in none with it in none: never a job alerts
        # sign-up's box beside it in the same frame, whose change the typing alone sets off (the fifth and sixth
        # reviews). A sign-in's one box among several left is the person's to tell (_sign_in_boxes)
        held = pipeline._sign_in_boxes(boxes, passwords, len(passwords) == 2 or signing_up)
        if held is None:
            return None
        boxes = held
        address = config.Profile.load().get("personal.email")
        if not address or not 1 <= len(passwords) <= 2:
            return None
        new_account = len(passwords) == 2 or signing_up
        if new_account and run.account_in and not tried.get("to_sign_in"):
            # the account made for this job already, and signed in to (never one only pressed for: a form the site
            # turned down is said for what it wants, the review of 0.3.148), its Create Account on show again (Applied
            # Materials' Workday, run 60: signed out by its "something went wrong" refresh, it showed Create Account,
            # and the desk filled it and stopped): its own Sign In is the way back in, never a second account. The
            # form's own link first ("Already have an account? Sign In"), then the page's; never another site's sign-in
            ways = [a for a in actions if not a.get("cookie") and not pipeline._SOCIAL.search(a["text"])
                    and re.match(r"^(sign|log) ?in$", a["text"].strip(), re.I)]
            way = (next((a for a in ways if a.get("in_account_form") or a.get("after_password")), None)
                   or next(iter(ways), None))
            if way is not None:
                tried["to_sign_in"] = 1
                run.did("pressed", way)
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
            alone = not (new_account or others or len(passwords) != 1 or pipeline._NEW_PASSWORD.search(label)
                         or pipeline._OLD_PASSWORD.search(label))
            # (the Create Account's: two boxes, neither a new, old or temporary password's, under a title or heading
            # that makes an account, never a reset's nor a password change's: the review of 0.3.154)
            said = " ".join([data.get("title") or "", *(data.get("headings") or [])])
            for_it = (len(passwords) == 2 and not any(re.search(r"\bnew\b", f.get("label") or "", re.I)
                                                      or pipeline._OLD_PASSWORD.search(f.get("label") or "")
                                                      for f in passwords)
                      and bool(pipeline._MAKES_ACCOUNT.search(said)) and not pipeline._RESET_PAGE.search(said))
            if not (alone or for_it) or not pipeline._shows_address(await srv.page_text(4000), address):
                return None
        if new_account:
            # The email in every box that asks for it ("Retype Email Address"; the user name
            # SCREEN's and Taleo's forms ask for is the email too), and the rest from the profile: its password box's
            # own group's (_box_group), never a job alerts form's box beside it (the sixth review)
            if details:
                await self._fill_account_details(pipeline._box_group(fields, passwords[0]), data.get("headings"))
            if boxes:
                run.did("typed", boxes)
                await srv.fill_form([{"id": f["id"], "value": address} for f in boxes])
        elif boxes:
            run.did("typed", boxes[:1])
            await srv.fill_form([{"id": boxes[0]["id"], "value": address}])
        for box in passwords:
            run.did("typed", box)
            if not (await srv.fill_secret(box["id"], secret)).get("ok"):
                return None  # it didn't go in: say nothing about a saved password
        as_user = any(pipeline._user_name_box(f) for f in (boxes if new_account else boxes[:1]))
        if new_account:  # its terms and button: the person's, unless they let the desk (_make_account)
            self._log(run, "filled the Create Account form with your details and saved password"
                      + (" (your email as its user name)" if as_user else ""))
            return "prefilled"
        # The form's own button, after its password box: a "Sign In" in the site's header opens
        # its sign-in page or pop-up instead, and sends nothing (Workday's). Failing that, the
        # button of a form that holds only the sign-in, whatever it reads (SuccessFactors' "Submit").
        own = [a for a in actions if a.get("after_password") and not pipeline._SOCIAL.search(a["text"])
               and not a.get("cookie") and not a.get("covered")]  # (Workday's hidden button: its cover takes the click)
        button = next((a for a in own if pipeline._SIGN_IN_ACTION.match(a["text"].strip())), None) or next(
            (a for a in own if a.get("sign_in_form") and pipeline._SIGN_IN_SUBMIT.match(final_text(a["text"]).strip())),
            None)
        if button is None:
            return "filled"
        run.did("pressed", button)
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
        questions = {f["id"] for f in pipeline._security_boxes(fields, headings)}
        # On a form that asks security questions, only the account's own details (a name, the email, a phone, the
        # country), never a box asking a question: "In what town were you born?" or "City of birth" would get the
        # profile's city, an answer made up (the review of 0.3.155)
        guarded = pipeline._security_asked({"fields": fields, "headings": headings or []})
        empty = [f for f in fields if f.get("kind") in pipeline._ACCOUNT_KINDS and is_empty_value(f.get("value"))
                 and f["id"] not in questions
                 and not (guarded and pipeline._QUESTION_LIKE.search(f.get("label") or ""))]
        plan = plan_autofill(empty, config.Profile.load(), {})
        to_fill = [f for f in plan["to_fill"] if not guarded or f.get("rule") in pipeline._ACCOUNT_DETAIL_RULES]
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
        result = await self._profile_fill(run)
        if result["filled"]:
            self._log(run, f"filled {len(result['filled'])} field(s) on {pipeline._where(data)}")
        run.did("", result.get("filled"), data)
        pending, missing_files = pipeline._pending(result, once_failed, run.asked_once)
        self._note_skipped(run, result)
        pending = await self._attest(run, pending)
        before = data
        data, _ = await self._look(text=False)
        run.page_info = pipeline._page_info(data)  # the page as filled
        pipeline._went_in(run, result["filled"], before, data, shown=data)  # (and on the job's "What went in" panel)
        pipeline._went_left(run, result, before, data)
        gaps, pending, gap_code = self._entry_gaps(run, data, pending)
        pending = await self._said_above(pending)
        if self._stop_for_what_is_left(run, gaps, pending, missing_files, gap_code):
            return
        saved = await self._sign_in(run, data, {}, details=False) == "prefilled"  # filled in above
        if saved:  # the record of the page shows its password boxes filled, too
            run.page_info = pipeline._page_info((await self._look(text=False))[0])
        srv._mark_ready(srv.tracker().get(run.job_id),
                        "filled by the Job Desk; the site creates the account as it applies")
        await self._bring_forward(run)
        password = ("Your saved password is in its password boxes" if saved
                    else "Choose a password in its password boxes")
        return self._pause(run, "your_submit", f"Filled in on {pipeline._site(run, data)}, which creates your account "
                           f"as you apply. {password}, check the page, then press its Apply button yourself: that "
                           "sends the application. Then press \u201cI submitted it myself\u201d here.",
                           code="account_as_you_apply")


# pipeline.py's own names (its waits, patterns, helpers and classes) are read from it as pipeline.<name> when a method
# runs: a test's patch of one (monkeypatch.setattr(pipeline, ...)) reaches the code here as it does pipeline.py's.
# Imported last: pipeline.py imports this module for Applier's bases, and either may be imported first.
from . import pipeline  # noqa: E402
