"""A real, visible browser the applicant shares with Claude.

The browser uses a persistent profile in ~/.job-apply/browser, so sign-ins to
LinkedIn, Indeed and company career sites survive between sessions. The person
can watch every step and take over at any time.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Frame,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)

from . import config
from .ats import detect_ats
from .autofill import choose_option, choose_place, date_form, is_empty_value, norm, polarity
from .browser_blocks import BlocksMixin
from .browser_dates import DatesMixin
from .browser_menus import MenusMixin
from .browser_tabs import TabsMixin
from .browser_uploads import UploadsMixin
from .formjs import (CHALLENGE_JS, CLICK_CHOICE_JS, COVERED_JS, ELEMENT_INFO_JS, EXTRACT_JS, LOST_BOXES_JS,
                     NOTICE_OVER_JS, PEEK_JS, QUIET_JS, TWIN_TICKED_JS, VISIBLE_TEXT_JS, WORKDAY_PROMPT_JS)

SUBMIT_RE = re.compile(r"\bsubmit\b|send (my )?application|finish (my )?application|complete (my )?application", re.I)
# A form's own submit button with one of these labels is the final step too ("Apply", "Send").
FINALISH_RE = re.compile(r"^(apply( now| online)?( for (this|the) (job|position|role|opening))?|"
                         r"apply to (this |the )?(job|position|role|opening)|send( now| (my )?application)?|finish|"
                         r"complete( (my )?application)?|confirm( and send)?)$", re.I)


def final_text(text: str) -> str:
    """A button's words without the arrow or icon after them: "Apply Now ›", "Apply
    arrow_forward" (an icon font's ligature), "Apply!" -> "Apply Now", "Apply", "Apply"."""
    text = re.sub(r"\s+(arrow_forward|arrow_right_alt|chevron_right|navigate_next|east)$", "", text.strip(), flags=re.I)
    return re.sub(r"[\s\u203a\u00bb\u2192>!.]+$", "", text).strip()
# Words that send an application from any button, a form's or not: refused in every mode (the review of Oct 15: in
# review mode a click on "Finish" could send the application without the person's Submit)
_SENDS = re.compile(r"^(send( now| (my )?application)?|finish( (my )?application)?|complete (my )?application|"
                    r"confirm and send)$", re.I)
# An agreement's button that also sends the application ("I Accept and Apply"): the person's
_SENDS_TOO = re.compile(r"\b(?:apply|submit|send|finish|complete)\b", re.I)
# Form buttons that only move between steps; in a dry run every other form submit is refused.
NAVIGATION_RE = re.compile(
    r"^(next|continue|save( and| &)? continue|save( for later| draft)?|review|back|previous|add( another)?|search|"
    r"sign ?in|log ?in|create account|verify|send (me a )?code|ok|accept( all)?( cookies)?|"
    r"i agree|apply manually|start)\b",
    re.I,
)
# What accepts a cookie or privacy banner (never pressed for the person, unless they set
# settings.accept_cookies): "Accept All Cookies", or inside a banner "Accept", "Allow all",
# "I agree", "AGREE AND PROCEED" (TI's)
_ACCEPT_WORDS = re.compile(r"^(accept|allow|agree|ok|okay|got it|i agree|i accept|i understand|yes,? i agree|"
                           r"agree (and|&) (proceed|continue|close)|accept (and|&) (proceed|continue|close))"
                           r"( all)?( cookies)?[.!]?$", re.I)
# "Accept (all) cookies", "Allow cookies": not "I agree to the Terms of Use and Cookie Policy",
# an application's consent line
_COOKIE_ACCEPT = re.compile(r"\b(accept|allow)\b[^.]*\bcookies\b|\bcookies\b[^.]*\b(accept|allow)", re.I)
_DECLINES = re.compile(r"reject|decline|necessary|essential|required only|only required|deny|refuse|manage|settings|"
                       r"preferences|customi[sz]e|without", re.I)


# An <input> tag as Chrome writes a page out (each attribute's value in double quotes), and in it a
# password box's type and value
_INPUT_TAG = re.compile(r'<input\b(?:[^>"]|"[^"]*")*>', re.I)
_PASSWORD_TYPE = re.compile(r'\stype="password"', re.I)
_ONE_TIME_CODE = re.compile(r"passcode|one[- ]time (?:pass(?:word|code)|code)|verification code|security code|\botp\b",
                            re.I)
_VALUE_ATTR = re.compile(r'\svalue="[^"]*"', re.I)
# a box holding an account's security answer, marked when the desk put it in (Browser._keep_secret)
_SECRET_BOX = re.compile(r'\sdata-ja-secret\b', re.I)

# ms a pick-only list that is open and still empty, or owned and not drawn yet, is waited on (a site fetching its
# entries: SuccessFactors' took longer than 8 s in run 111). A list that is genuinely empty, or a box that stays
# "expanded", costs the wait once per such field per fill (under the lock); one found closed after the first read is
# opened once more first, and its wait begins then (about 12.5 s in all)
PICK_LIST_WAIT = 10000
# ms a box's list is given to show its choices (browser_menus.py's reader stops waiting as soon as one comes; these are
# the most it waits on a list that shows nothing):
LIST_OPEN_WAIT = 2000  # a listbox's choices when the form is read
COMBO_OPEN_WAIT = 900  # a search picker's on opening (often nothing until typed into)
LIST_PLACEHOLDER_WAIT = 5000  # a listbox on its placeholder alone when the form is read (Boeing's veteran list, run 56)
PICK_OPEN_WAIT = 2500  # a list opened to pick from (a pick-only list's, a multiselect's)
LIST_REOPEN_WAIT = 3000  # a list opened once more after it showed nothing
LIST_JUMP_WAIT = 1500  # a long list after typing to jump to the answer in it
LIST_FILTER_WAIT = 2500  # a list narrowed by the words typed into its box, or searched with Enter
LIST_SEARCH_WAIT = 3000  # a search's results, where the list still shows what it opened with (Oracle's ZIP)
LIST_NEW_WAIT = 1500  # the entries a choice opens as a group of its own (Workday's "How Did You Hear About Us?")
# s a frame's form read (EXTRACT_JS) is waited on: a page whose script has hung answers never
READ_TIMEOUT = 20
# Months a date picker's calendar is moved on (or back) at most to reach the day to pick: two years
DATE_PICKER_MONTHS = 24
# Is this box's list open with nothing in it yet: the box (or a wrapper up to 4 above it, as FIELD_OPTIONS_JS looks)
# saying it's expanded, or its named menu shown without a choice?
_LIST_LOADING_JS = """el => {
  for (let n = el, d = 0; n && n.getAttribute && d < 4; n = n.parentElement, d++) {
    if (n.getAttribute('aria-expanded') === 'true') return true;
    const owns = n.getAttribute('aria-owns'), ref = owns || n.getAttribute('aria-controls');
    if (!ref) continue;
    const box = document.getElementById(ref.split(/\\s+/)[0]);
    // (the list it owns not drawn at all yet: SuccessFactors' paginated select makes its list only once the entries
    // came from the server, and says nothing meanwhile: Edwards Vacuum's and Freeport-McMoRan's, run 111. Only an owned
    // one: Eightfold's and Oracle's lists named by aria-controls are drawn when opened, and say they're expanded then:
    // one absent there is a list that didn't open, the review of 0.3.188)
    if (!box) { if (owns) return true; continue; }
    return box.getClientRects().length > 0 && !box.querySelector('[role="option"]');
  }
  return false;
}"""


# Is this box's list open: the box (or a wrapper) saying it's expanded, or the list it owns or controls in the page and
# shown? (A pick-only list that closed itself again before its choices were read: _type_and_pick opens it once more)
_LIST_OPEN_JS = """el => {
  const up = [];
  for (let n = el, d = 0; n && n.getAttribute && d < 4; n = n.parentElement, d++) up.push(n);
  if (up.some((n) => n.getAttribute('aria-expanded') === 'true')) return true;  // (any level first: the review's)
  for (const n of up) {
    const ref = n.getAttribute('aria-owns') || n.getAttribute('aria-controls');
    const box = ref ? document.getElementById(ref.split(/\\s+/)[0]) : null;
    if (box) return box.getClientRects().length > 0;
  }
  return false;
}"""


# The ids the form reader gives (formjs: "f2-abc17", "abc17.0", "f1-add3"): a selector is only ever made from one so
# shaped, never from a page's own attribute with quotes in it (the review of Oct 15)
_JA_ID = re.compile(r"[\w.:-]{1,80}")


def _by_ja_id(element_id: str) -> str:
    if not _JA_ID.fullmatch(element_id):
        raise KeyError(f"{element_id!r} isn't one of the form's ids; call inspect_form for them")
    return f'[data-ja-id="{element_id}"]'


class _Hung(Exception):
    """A page's script answered no read within READ_TIMEOUT (it hangs): what it holds is read as nothing."""


def _quiet(task: asyncio.Future) -> None:
    """A read given up on: whatever it ends with is taken, so nothing is logged as never retrieved."""
    if not task.cancelled():
        task.exception()


async def _bounded(coro: Any, seconds: float | None = None) -> Any:
    """A read of a page that a hung script never answers, given up after READ_TIMEOUT seconds (raises _Hung), so it
    never holds the browser's lock for good (the review of 0.3.188: the title and the page's words were still
    unbounded after the form read was bounded). Not asyncio.wait_for: on Python 3.10 it drops a cancellation that
    comes as the read ends, and the desk's worker, told to stop, went on for ever (CI's 3.10 run of 0.3.188)."""
    task = asyncio.ensure_future(coro)
    try:
        done, _ = await asyncio.wait({task}, timeout=READ_TIMEOUT if seconds is None else seconds)
    except BaseException:  # (this read's own caller cancelled: the read goes too)
        task.cancel()
        task.add_done_callback(_quiet)
        raise
    if not done:
        task.cancel()
        task.add_done_callback(_quiet)
        raise _Hung()
    return task.result()


async def _page_title(page: Page) -> str:
    """A page's title, or "" from a page that hangs."""
    try:
        return await _bounded(page.title())
    except _Hung:
        return ""


def _without_passwords(html: str) -> str:
    """A page's HTML with nothing in its password boxes, nor in an account's security answer's: a site that writes
    what's typed into its HTML (React keeps a box's value attribute in step) would leave one in a saved page."""
    return _INPUT_TAG.sub(lambda m: _VALUE_ATTR.sub("", m.group())
                          if _PASSWORD_TYPE.search(m.group()) or _SECRET_BOX.search(m.group()) else m.group(), html)


CAPTCHA_SAYS = ("A CAPTCHA (a bot check: an \"I'm not a robot\" box or pictures) is on this page. It's for the "
                "user to solve; ask them to, and never try it yourself.")
# A CAPTCHA's own frames: never read as part of the form. iCIMS's sign-in pages carry a hidden
# hCaptcha whose "Verify" and "Refresh Challenge." read as the page's buttons, and the desk
# never touches a CAPTCHA anyway
# (and DataDome's, geo.captcha-delivery.com: its block over Wynn's and Boyd's SmartRecruiters postings, live run 144,
# whose feedback form's Comment box was read as the application)
_CAPTCHA_FRAME = re.compile(r"^https?://([\w-]+\.)*(hcaptcha\.com|recaptcha\.net|challenges\.cloudflare\.com|"
                            r"captcha-delivery\.com)/|"
                            r"^https?://(www\.)?google\.com/recaptcha/", re.I)
# A video player's frame: never read as part of the page. Oracle's job pages carry a media slider whose
# YouTube embed says "This video is private ... Sign in." in a `.ytp-error` box, which read as the
# page's error (the desk stopped on "The page didn't move on") and its "Sign in" link as a sign-in
# (issue #112, note 33). Only the players' hosts: an employer's form in a frame (iCIMS, Greenhouse)
# is cross-origin too, and its errors matter.
_MEDIA_FRAME = re.compile(r"^https?://([\w-]+\.)*(youtube(-nocookie)?\.com|youtu\.be|vimeo\.com|wistia\.(com|net)|"
                          r"dailymotion\.com|loom\.com|vidyard\.com|brightcove\.net|jwplatform\.com)([:/?#]|$)", re.I)


# What declines a cookie banner, pressed for the person wherever a banner offers it: anywhere
# cookies are spoken of, "Reject all" or "Necessary only"; in a cookie box, also "Deny", "Refuse",
# "Opt out", "No thanks" and "Continue without accepting"
_DECLINE_COOKIES = re.compile(
    r"^(reject(?: all)?(?: cookies)?|decline(?: all)?(?: cookies)?|only (?:strictly )?necessary|"
    r"necessary (?:cookies )?only"
    r"|use necessary cookies only|accept (?:only )?necessary(?: cookies)?|reject optional(?: cookies)?"
    r"|(?:reject|decline) non-?essential(?: cookies)?)$", re.I)
_DECLINE_IN_BANNER = re.compile(
    r"^((?:deny|refuse|disagree|opt out)(?: all)?(?: cookies)?|(?:i )?do not (?:accept|agree)|no,? thanks"
    r"|(?:reject|decline|deny|refuse) (?:all )?(?:optional|additional|non-?essential|non-?necessary)(?: cookies)?"
    r"|(?:only|accept|allow|use) (?:only )?(?:strictly )?(?:essential|required)(?: cookies)?(?: only)?"
    r"|(?:essential|required) (?:cookies )?only|continue without (?:accepting|agreeing|consent(?:ing)?|cookies))[.!]?$",
    re.I)


def _accepts_cookies(label: str, text: str, in_banner: bool) -> bool:
    if _DECLINES.search(label):
        return False  # "Accept necessary cookies only", "Reject all": the private choice
    return bool(_COOKIE_ACCEPT.search(label) or in_banner and _ACCEPT_WORDS.match(text))


def declines_cookies(text: str, in_banner: bool) -> bool:
    text = text.strip()
    return bool(_DECLINE_COOKIES.match(text) or in_banner and _DECLINE_IN_BANNER.match(text))


def _cookie_setting() -> bool:
    try:
        return config.Profile.load().settings.accept_cookies
    except ValueError:  # a profile with a typo allows nothing
        return False


def may_accept_cookies(info: dict[str, Any]) -> bool:
    """Whether a cookie banner's accept (ELEMENT_INFO_JS's facts about it) may be pressed for the
    person: only where they set settings.accept_cookies, and only on a banner with no way to
    decline (TI's "Manage Preferences" or "Agree and Proceed"). One that can be declined is."""
    return (bool(info.get("cookie")) and not any(declines_cookies(t, True) for t in info.get("cookieButtons") or [])
            and _cookie_setting())


# SuccessFactors' older career sites show a posting inside a form whose submit button is
# "Apply". With nothing in the form to fill, it opens the application; it sends nothing.
POSTING_PAGE_RE = re.compile(r"career(?:_|%5f)ns=job(?:_|%5f)listing(?:&|#|$)", re.I)
SHORT_MENU = 12  # a menu this short shows every choice; a longer one may show only some
_SEARCH_PAGE = 15  # BrassRing's search box lists its choices 15 at a time: a full page may have more past it
# a multiselect's row that shows its choice ticked (BrassRing's pseudo-checkbox, or its row's own class; a list of
# several choices whose row says it's selected: Oracle's cx-multi-select, run 144)
_ROW_TICKED_JS = ("el => !!(el.closest('li.checked') "
                  "|| el.querySelector('.pseudocheckbox.checked, .pseudocheckbox[aria-checked=\"true\"]') "
                  "|| el.closest('[role=\"listbox\"][aria-multiselectable=\"true\"] "
                  "[role=\"option\"][aria-selected=\"true\"]'))")
POPUP_FOLLOW = 15  # seconds after a click of ours in which a tab it opens is followed
FRAME_WAIT = 8  # seconds for a job board's frame (iCIMS's openings) to load its page, all told
SETTLE_WAIT = 8  # seconds for a job board's page to stop loading things, at most
LOST_FILL_WAIT = 0.5  # seconds after filling for a page to mark the boxes whose answers it lost
LOST_FILL_TRIES = 3  # times a box is typed again on a page, at most (one the page refuses stays marked)
BLOCK_DRAW_WAIT = 6000  # ms for blocks just added (Workday's Education 2) to draw their boxes, at most
SAVED_DRAW_WAIT = 2500  # ms an experience page is given to draw its saved blocks before any is added
# A section's Add button that a click can't reach (a toast or a re-render drawn over it for a moment, Workday's My
# Experience, issue #112, note 32): pressed up to this many times, each for this many ms, and between them
# the page is given a little longer each time (this many ms times the try) to clear
ADD_TRIES, ADD_PRESS_TIMEOUT, ADD_COVER_WAIT = 3, 3000, 1500
# How long a click may wait for its button to become clickable, in ms.
CLICK_TIMEOUT = 8000
FORM_DRAW_TRIES, FORM_DRAW_WAIT = 5, 500  # a page read with no form is read again, each this many ms apart
UPLOAD_CHOOSER_WAIT = 3000  # ms for a button that opens a site's own upload to open a file chooser
UPLOAD_SHOWN_WAIT = 15  # seconds for a file given through a site's own upload to show on the page
# An upload that sends its form and loads it again (iCIMS's resume box submits its frame's form: Valley Health's, run
# 144, whose every box after it was then waited on for 15 s, and what went in was lost with the page): seconds for that
# page load to begin after the file is given (it's under way in milliseconds; this much is spent on every upload that
# loads nothing), and for the page to have come back, at most (the site reads the resume first)
UPLOAD_RELOAD_START = 0.5
UPLOAD_RELOAD_WAIT = 30
CONFIRM_WAIT = 12  # seconds a submit's confirmation is watched for after its page goes quiet
# How long a box under an overlay (or being drawn again) may take to take the focus, in ms: not
# Playwright's 30 s for each of Workday's 16 date parts on My Experience
FOCUS_TIMEOUT = 5000
# What a page says once an application has gone ("Thanks for applying!", Oracle's "Thank you for
# your job application", "Thank you for submitting your application"); only words that weren't
# on the page before Submit count. Not "Thank you for your interest", which a sign-in page says
# too, nor "Your application is in progress" or "...incomplete"
CONFIRMATION_RE = re.compile(
    r"thank(?:s| you) for (applying|your (job )?application|submitting (your |an )?application)"
    r"|application (has been |was )?(successfully )?(submitted|received|completed?)\b"
    r"|we('ve| have) received your application|successfully (submitted|applied)\b"
    r"|your application is (in(?=\s*(?:[.!]|$))|on its way)",
    re.I,
)
# Words earlier in the sentence that make it no confirmation: "No application was submitted",
# "after your application has been submitted, you will...", "If you have successfully applied"
_NOT_CONFIRMED = re.compile(r"\b(no|not|never|once|after|when|if|until|before|unless)\b[^.!?]{0,40}$", re.I)
# A form sent back: what its errors say
_FORM_ERROR = re.compile(r"required|invalid|error|must be|can'?t be blank|missing|"
                         r"please (enter|select|provide|fix|correct|complete|review)", re.I)


# Is this spinbutton a date's part: one of several in its date, or named a month, day or year?
DATE_LIKE_JS = """
(el) => {
  for (let n = el.parentElement, d = 0; n && d < 6; n = n.parentElement, d++) {
    const found = n.querySelectorAll('[role="spinbutton"]').length;
    if (found > 1) return found <= 3;
  }
  return /\\b(month|day|year)\\b|^(mm|dd|yyyy)$/i.test(el.getAttribute('aria-label') || el.placeholder || '');
}
"""


# Does this part's date mark another of its parts as the current one (Workday's section with tabindex 0)?
_ANOTHER_PART_CURRENT_JS = """
(el) => {
  const section = el.parentElement, group = el.closest('[role="group"]');
  if (!section || !group || section.getAttribute('tabindex') === '0') return false;
  return [...group.querySelectorAll('[tabindex="0"]')].some((s) => s !== section && s.querySelector('[role="spinbutton"]'));
}
"""  # noqa: E501


# A date's parts (Workday's MM / DD / YYYY spinbuttons), in order: those in the nearest box around this one
# that holds more than one (its date), up to three; this one alone otherwise. Marked for the locator, with
# this one as "mine"; how many there are.
DATE_PARTS_JS = """
(el) => {
  let parts = [el];
  for (let n = el.parentElement, d = 0; n && d < 6; n = n.parentElement, d++) {
    const found = [...n.querySelectorAll('[role="spinbutton"]')];
    if (found.length > 1) { if (found.length <= 3) parts = found; break; }
  }
  parts.forEach((p) => p.setAttribute('data-ja-date-part', ''));
  el.setAttribute('data-ja-date-mine', '');
  return parts.length;
}
"""


def _bare_choice(text: str) -> str:
    """A list's choice without the arrow that says it opens a list of its own ("Employee Referral ›")."""
    return re.sub(r"\s*[\u203a>\u276f\u25b8\u25b6]+\s*$", "", text or "").strip()


def _inner_choice(value: Any, picked: str, inner: list[str], names: bool = False) -> str | None:
    """The choice to pick in the list a choice opened: the answer, if it's there; else the one named as
    the choice picked; else the only one (Workday's "<employer> website ›" opens a list of one, named
    alike: live test 2, issue #112, note 15)."""
    entry = choose_option(value, inner, names=names)
    if entry is not None:
        return entry
    # (never just any lone entry: "Job Board ›" opening ["Indeed"] isn't the profile's answer)
    picked_name = norm(_bare_choice(picked))
    same = [o for o in inner if norm(_bare_choice(o)) == picked_name]
    if len(same) == 1:
        return same[0]
    alike = [o for o in inner if picked_name and picked_name in norm(_bare_choice(o))]
    return alike[0] if len(inner) == 1 and alike else None


async def _covered(loc: Locator) -> bool:
    """Is something drawn over this box, so a click can't reach it? Asked for 2 s at most, as a
    click would be: a box Workday is drawing again (after a menu pick) isn't waited on for the
    default 30 s, and counts as covered (focus takes the keys)."""
    try:
        return bool(await loc.evaluate(COVERED_JS, timeout=2000))
    except (PlaywrightError, PlaywrightTimeout):
        return True


def confirmations(text: str) -> set[str]:
    """The confirmations a page's text gives, however its spaces, lines and apostrophes come."""
    flat = re.sub(r"\s+", " ", (text or "").replace("\u2019", "'").replace("\xa0", " "))
    return {m.group(0).lower() for m in CONFIRMATION_RE.finditer(flat)
            if not _NOT_CONFIRMED.search(flat[max(0, m.start() - 60):m.start()])}


def _css_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class BrowserUnavailable(Exception):
    pass


# The Job Desk fills jobs side by side, each in its own tab, while the person may be at another: a tab behind the one
# in front must run as one in front does (its timers, its page's own drawing, its renderer's share of the computer),
# or its waits run out. Playwright's own launch has these too (and its focus emulation keeps each page counted as
# shown); named here so they hold whatever its defaults become. (Never a --disable-features: Chromium takes only the
# last one, and Playwright's own would be lost.)
BACKGROUND_TABS_RUN = ("--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                       "--disable-renderer-backgrounding")


UNAVAILABLE_HELP = (
    "Could not start a browser. Install Google Chrome, or run "
    "`uv run --project <plugin>/server playwright install chromium`. Details: "
)


# The browser's own password offers, off in the desk's profile: saving, signing in with a saved
# one, and the "found in a data breach" check after one is typed
_QUIET = {"credentials_enable_service": False, "credentials_enable_autosignin": False}
_QUIET_PROFILE = {"password_manager_enabled": False, "password_manager_leak_detection": False}


def quiet_password_manager(profile: Path) -> None:
    """Turn off the browser's own "Save password?" offer in the desk's profile, before it starts:
    the desk keeps the passwords (on its page) and types them in itself, and the bubble popped
    up over the page after every sign-in. The profile's other settings are kept, a file that
    can't be read is left as it is, and the file is replaced whole (a half-written one would
    reset the profile)."""
    prefs = profile / "Default" / "Preferences"
    try:
        data = json.loads(prefs.read_text(encoding="utf-8")) if prefs.exists() else {}
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    found = data.get("profile")
    profile_prefs: dict[str, Any] = found if isinstance(found, dict) else {}
    if (all(data.get(k) == v for k, v in _QUIET.items())
            and all(profile_prefs.get(k) == v for k, v in _QUIET_PROFILE.items())):
        return
    data.update(_QUIET, profile={**profile_prefs, **_QUIET_PROFILE})
    temp = prefs.with_name(prefs.name + ".tmp")
    try:
        for folder in (profile, prefs.parent):  # private, as the browser makes them
            folder.mkdir(mode=0o700, exist_ok=True)
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            f.write(json.dumps(data))
        os.replace(temp, prefs)
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink()


def profile_dir(usual: Path, attempt: dict[str, Any], settings: config.Settings) -> Path:
    """The browser profile for a launch attempt. Edge standing in for Chrome keeps one of its
    own: each keeps its sign-ins in a form the other can't read."""
    if attempt.get("channel") == "msedge" and settings.browser_channel != "msedge":
        return usual.with_name(usual.name + "-msedge")
    return usual


def launch_attempts(settings: config.Settings) -> list[dict[str, Any]]:
    """Browser choices to try in order: an explicit executable, the installed Chrome (or the
    channel chosen), Microsoft Edge (every Windows computer has it, so no download is needed
    there without Chrome), then Playwright's bundled Chromium."""
    exe = os.environ.get("JOB_APPLY_CHROMIUM_PATH")
    if exe:
        return [{"executable_path": exe}]
    attempts: list[dict[str, Any]] = []
    if settings.browser_channel in ("chrome", "msedge", "chrome-beta"):
        attempts.append({"channel": settings.browser_channel})
        if settings.browser_channel != "msedge":
            attempts.append({"channel": "msedge"})
    attempts.append({})
    return attempts


class SubmitBlocked(Exception):
    pass


# A launch error that means the browser isn't on this computer (so the next one is tried)
_NOT_INSTALLED = re.compile(r"is not found|not installed|executable doesn'?t exist|no such file|cannot find|"
                            r"ENOENT|distribution .* not found|failed to launch.*not found", re.I)


_DOCUMENT_SUFFIXES = {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".png", ".jpg", ".jpeg"}


def _desk_file(path: Path) -> bool:
    """A file in ~/.job-apply that isn't a document (secrets.yaml, answers.yaml, the tracker)."""
    try:
        home = config.home().resolve()
        real = path.resolve()
    except OSError:
        return True
    return (real == home or home in real.parents) and real.suffix.lower() not in _DOCUMENT_SUFFIXES


def _choose(text: str, options: list[str], field: dict, exact_only: bool = False) -> str | None:
    """The entry to pick for `text`. An address part looks first for the place that matches
    the rest of the address (field["near"]): Oracle's City lists "Chandler, Henderson, TX"
    before "Chandler, Maricopa, AZ"."""
    if field.get("near"):
        place = choose_place(text, options, field["near"])
        if place is not None:
            return place
    return choose_option(text, options, exact_only=exact_only, names=bool(field.get("names")))


def questions_shown(fields: list[dict]) -> str:
    """Which questions a step shows (its boxes' labels, in order), in short: a questionnaire drawing its next question
    in place, at one address under one heading, is a step further on, not the same step pressed again (the review of
    Oct 15)."""
    # (its numbers left out: a label with a counter ticking in it, "Reference (expires in 600s)", is the same question
    # on every look, and a step stuck under it was pressed thirty times: the review of 0.3.189)
    labels = "\n".join(re.sub(r"\d+", "#", norm(str(f.get("label") or ""))) for f in fields)
    return hashlib.sha1(labels.encode("utf-8")).hexdigest()[:12]


def _groups_key(url: str, field: dict) -> tuple[str, str] | None:
    """Where a list's groups are remembered: the site (its host and first two path parts, so one Workday tenant's
    lists aren't another's on a shared host) and the list's label. None for a list with no label."""
    label = norm(field.get("label") or "")
    if not label:
        return None
    parsed = urlparse(url)
    # (not a language's part, nor Workday's shared "recruiting": /en-US/recruiting/<tenant>/<site> is the tenant's)
    parts = [p for p in parsed.path.split("/") if p and not re.fullmatch(r"[a-z]{2}(?:[-_][A-Za-z]{2})?", p)
             and p.lower() != "recruiting"]
    return f"{parsed.hostname or ''}/{'/'.join(parts[:2])}", label


class PickedAGroup(ValueError):
    """The option picked was a group (Workday's "How Did You Hear About Us?" has "Job Board"
    holding Indeed, LinkedIn, ...): it opened its own list instead of being chosen."""

    def __init__(self, group: str, entries: list[str]) -> None:
        super().__init__(f"{group!r} is a group; pick one of its entries: {entries[:30]}")
        self.entries = entries


class NoMatch(ValueError):
    """The answer isn't among the list's suggestions. The list is carried: one that drew its choices late
    wasn't read when the answer was chosen (Workday's "How Did You Hear About Us?", issue #112, note 27), and
    the answer is chosen again among them."""

    def __init__(self, text: str, entries: list[str]) -> None:
        super().__init__(f"{text!r} doesn't match any suggestion: {entries[:30]}")
        self.entries = entries


class DialogOpen(Exception):
    """A dialog is open over the page (a notice to answer first): nothing is filled behind it."""


class TabClosed(Exception):
    """The tab a job was working in is gone (closed, or skipped in the Job Desk)."""


_CHALLENGE_TITLE = re.compile(r"just a moment|attention required|checking your browser|are you a robot|"
                              r"verify(ing)? you are human", re.I)


class SiteDown(Exception):
    """A job board answered with a server error page (Daifuku's iCIMS, live: HTTP 521), which
    would otherwise read as a board with no openings."""


def _among(choice: str, ticked: list[str]) -> bool:
    """Is this choice among those a multiselect has ticked, however each is spaced or cased?"""
    return norm(choice) in {norm(t) for t in ticked}


def _search_words(text: str) -> str:
    """What to type into a picker's search box: the words, without flags, dial codes or
    bracketed extras. Eightfold's Country code list searches the country's name, so
    "🇺🇸 (+1) United States of America" typed whole finds nothing; "United States of
    America" does."""
    words = re.sub(r"\([^)]*\)|\+\d+|[^\w\s,.'&/-]", " ", text)
    return re.sub(r"\s+", " ", words).strip() or text


# How a date box may show its day once picked: the desk's own form first (the US month/day/year), then the others
_DAY_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%y", "%b %d, %Y",
                "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%a, %b %d, %Y", "%A, %B %d, %Y")
# Months longer than three letters as some pickers shorten them ("Sept 2, 2026")
_SEPT = re.compile(r"\bSept\b", re.I)


def _as_date(value: Any, field: dict) -> date | None:
    """A date answer for a date box, in the form autofill gives the box one (date_form: "11/02/2026", "02/11/2026" for
    one hinted "DD/MM/YYYY") or the ISO form: None for anything else (the profile's own words, "2 weeks
    after offer")."""
    text = str(value or "").strip()
    for form in (date_form(field), "%Y-%m-%d"):
        with contextlib.suppress(ValueError):
            return datetime.strptime(text, form).date()
    return None


def _shows_day(shown: str, day: date) -> bool:
    """Does a date box show this day, in whatever form it writes dates ("11/02/2026", "2026-11-02", "Nov 2, 2026")?"""
    text = _SEPT.sub("Sep", re.sub(r"\s+", " ", str(shown or "")).strip())
    for form in _DAY_FORMATS:
        with contextlib.suppress(ValueError):
            if datetime.strptime(text, form).date() == day:
                return True
    return False


# A bar fixed to the window (Workday's "Save and Continue" footer) over a menu entry: the menu
# of a dropdown at the end of the page opens under it, where no scrolling can bring it out. The
# bar lets clicks through while the entry is clicked (a real click: Workday ignores a script's).
# Only a bar outside the entry's own menu, and only the bars on top of it; how many it lifted.
_THROUGH_BARS_JS = r"""
(el) => {
  const r = el.getBoundingClientRect();
  const x = r.left + r.width / 2, y = r.top + r.height / 2;
  let lifted = 0;
  for (let top = document.elementFromPoint(x, y); top && top !== el && !el.contains(top) && lifted < 3;
       top = document.elementFromPoint(x, y)) {
    let bar = null;
    for (let n = top; n && n !== document.body; n = n.parentElement) {
      if (['fixed', 'sticky'].includes(getComputedStyle(n).position)) bar = n;
    }
    if (!bar || bar.contains(el) || bar.hasAttribute('data-ja-through')) break;
    bar.setAttribute('data-ja-through', bar.style.pointerEvents || '');
    bar.style.pointerEvents = 'none';
    lifted++;
  }
  return lifted;
}
"""
_BARS_BACK_JS = r"""
() => { for (const bar of document.querySelectorAll('[data-ja-through]')) {
  bar.style.pointerEvents = bar.getAttribute('data-ja-through');
  bar.removeAttribute('data-ja-through');
} }
"""


@dataclass(eq=False)
class _View:
    """What the tools act on, for one user of the browser: its tab, what was last read there (its boxes and buttons,
    by the ids given out), a click's new tab still to follow, the job, and its own lock (one step at a time in the
    tab). Claude's tools, and the Job Desk's queue between jobs, share the session's own view; each job the desk fills
    has a view of its own (BrowserSession.desk_view), so jobs filled side by side, each in its own tab, never read or
    click in each other's."""
    page: Page | None = None
    fields: dict[str, dict] = field(default_factory=dict)
    actions: dict[str, dict] = field(default_factory=dict)
    follow_until: float = 0.0  # a new tab opened before then, by this view's tab, is followed
    date_typed: set[str] = field(default_factory=set)  # a date's parts typed in the fill under way
    # While the Job Desk works on a job, the tools stay on that job's tab (and the tabs it opens): a closed one
    # raises TabClosed instead of moving on to another tab
    strict: bool = False
    job_id: int | None = None
    desk: bool = False  # a job the desk is filling
    locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = field(default_factory=dict)
    owner: Any = field(default=None, repr=False)  # the BrowserSession it's a view of


# The view of the task running now (a job the desk fills), or None: the session's own
_VIEW: contextvars.ContextVar[_View | None] = contextvars.ContextVar("job_apply_view", default=None)


def _per_loop(locks: dict[asyncio.AbstractEventLoop, asyncio.Lock]) -> asyncio.Lock:
    """The lock for the running event loop. A lock belongs to the event loop it first waited in, and the session
    outlives a loop (each test has its own; the MCP server one): a loop that's gone takes its lock with it."""
    loop = asyncio.get_running_loop()
    lock = locks.get(loop)
    if lock is None:
        for gone in [lp for lp in locks if lp.is_closed()]:
            del locks[gone]
        lock = locks[loop] = asyncio.Lock()
    return lock


# Its tabs, its lists and menus, its dates, its uploads and a section's blocks are mixins of their own (browser_tabs.py,
# browser_menus.py, browser_dates.py, browser_uploads.py, browser_blocks.py); the state they act on is the session's
class BrowserSession(TabsMixin, MenusMixin, DatesMixin, UploadsMixin, BlocksMixin):
    def __init__(self) -> None:
        self._own = _View()  # Claude's tools' (and the desk's queue between jobs): _View
        self._desk_views: set[_View] = set()  # the views of the jobs the desk is filling now
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._launch_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._openers: dict[Page, Page] = {}  # tab -> the tab that opened it
        self._frame_ids: dict[Frame, str] = {}
        # what the desk typed on a page (by address), and how often each box was typed again
        self._typed: dict[str, set[str]] = {}
        self._retyped: dict[tuple[str, str, str], int] = {}
        # the entries a list's group opened (by the site and the list's label: entry -> group), so an entry the
        # person picked from them is picked inside its group (_type_and_pick)
        self._groups: dict[tuple[str, str], dict[str, str]] = {}  # (_groups_key's: the site, its list's label)
        self.tab_jobs: dict[Page, int] = {}  # the job each tab was opened for
        self._crashed: set[Page] = set()  # tabs whose page crashed (Chrome's "Aw, Snap!")
        # The tab the person is at for the Job Desk (a sign-in or code that needs their hands, or one of its rounds),
        # if any: a new tab opened by a job filled meanwhile in another tab never stays in front of it
        self.keep_front: Callable[[], Page | None] | None = None
        self._fronting: set[asyncio.Future] = set()

    # ------------------------------------------------------------- views
    @property
    def _view(self) -> _View:
        view = _VIEW.get()
        return view if view is not None and view.owner is self else self._own

    @contextlib.contextmanager
    def desk_view(self, job_id: int | None = None) -> Iterator[_View]:
        """A view of its own for a job the Job Desk fills, for the task running it: its tab, its reads and its
        lock are its own, and two jobs filled at once, each in its own tab, never act in each other's. Afterwards
        the tools' own view (Claude's) is left on the job's tab when it has no live tab of its own, as when the two
        shared one."""
        view = _View(job_id=job_id, desk=True, owner=self)
        token = _VIEW.set(view)
        self._desk_views.add(view)
        try:
            yield view
        finally:
            self._desk_views.discard(view)
            _VIEW.reset(token)
            own = self._own
            if (view.page is not None and not view.page.is_closed() and view.page.context is self._ctx
                    and (own.page is None or own.page.is_closed())):
                own.page, own.job_id = view.page, view.job_id

    def _views(self) -> list[_View]:
        return [self._own, *self._desk_views]

    @property
    def _page(self) -> Page | None:
        return self._view.page

    @_page.setter
    def _page(self, page: Page | None) -> None:
        self._view.page = page

    @property
    def _fields(self) -> dict[str, dict]:
        return self._view.fields

    @_fields.setter
    def _fields(self, fields: dict[str, dict]) -> None:
        self._view.fields = fields

    @property
    def _actions(self) -> dict[str, dict]:
        return self._view.actions

    @_actions.setter
    def _actions(self, actions: dict[str, dict]) -> None:
        self._view.actions = actions

    @property
    def _follow_until(self) -> float:
        return self._view.follow_until

    @_follow_until.setter
    def _follow_until(self, until: float) -> None:
        self._view.follow_until = until

    @property
    def _date_typed(self) -> set[str]:
        return self._view.date_typed

    @_date_typed.setter
    def _date_typed(self, typed: set[str]) -> None:
        self._view.date_typed = typed

    @property
    def strict_tabs(self) -> bool:
        return self._view.strict

    @strict_tabs.setter
    def strict_tabs(self, strict: bool) -> None:
        self._view.strict = strict

    @property
    def current_job_id(self) -> int | None:
        return self._view.job_id

    @current_job_id.setter
    def current_job_id(self, job_id: int | None) -> None:
        self._view.job_id = job_id

    @property
    def _lock(self) -> asyncio.Lock:
        """One step at a time in the view's tab (each job the desk fills at once has its own)."""
        return _per_loop(self._view.locks)

    async def _ensure_ctx(self) -> None:
        """The browser, started once however many views ask for it at the same moment."""
        if self._ctx is not None:
            return
        async with _per_loop(self._launch_locks):
            if self._ctx is None:
                await self._launch()

    # ---------------------------------------------------------------- lifecycle
    async def _launch(self) -> None:
        settings = config.Profile.load().settings
        user_dir = config.browser_profile_dir()
        user_dir.mkdir(parents=True, exist_ok=True)
        if self._pw is None:  # still running if only the browser window was closed
            self._pw = await async_playwright().start()
        kwargs: dict[str, Any] = {
            "user_data_dir": str(user_dir),
            "headless": settings.headless,
            "accept_downloads": True,
            "no_viewport": not settings.headless,
            "args": list(BACKGROUND_TABS_RUN),
        }
        if settings.headless:
            kwargs["viewport"] = {"width": 1280, "height": 900}
        errors = []
        for extra in launch_attempts(settings):
            kwargs["user_data_dir"] = str(profile_dir(user_dir, extra, settings))
            quiet_password_manager(Path(kwargs["user_data_dir"]))
            try:
                self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs, **extra)
                break
            except PlaywrightError as e:
                errors.append(f"{extra or 'bundled chromium'}: {str(e).splitlines()[0]}")
                if not _NOT_INSTALLED.search(str(e)):
                    # it's there but wouldn't start (its profile in use by another window, an
                    # update half done): the next browser, without its sign-ins, isn't the answer
                    await self._pw.stop()
                    self._pw = None
                    name = {"chrome": "Chrome", "msedge": "Edge"}.get(extra.get("channel", ""), "The browser")
                    detail = " ".join(line.strip() for line in str(e).splitlines()[:4] if line.strip())[:500]
                    raise BrowserUnavailable(
                        f"{name} is installed but wouldn't start. If another window of it is open on the desk's "
                        f"profile ({kwargs['user_data_dir']}), close that window and try again. Details: {detail}"
                    ) from e
        if self._ctx is None:
            await self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(UNAVAILABLE_HELP + " | ".join(errors))
        self._ctx.set_default_timeout(15000)  # a vanished element fails in 15 s, not 30
        ctx = self._ctx
        ctx.on("page", self._watch)
        ctx.on("close", lambda _: self._forget(ctx))
        for tab in ctx.pages:
            self._watch(tab)
        # (the view that started it: a job's own leaves the tools' to follow the job's tab,
        # desk_view, as one shared did)
        self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    async def close(self) -> None:
        if self._ctx is not None:
            try:
                await self._ctx.close()
            except PlaywrightError:
                pass
        if self._pw is not None:
            await self._pw.stop()
        self._ctx = self._pw = None
        for view in self._views():
            view.page = None
            view.fields.clear()
            view.actions.clear()
            view.follow_until = 0.0  # a click in the window just closed isn't one in the next
        self._frame_ids.clear()

    # ---------------------------------------------------------------- navigation
    async def goto(self, url: str) -> dict[str, Any]:
        async with self._lock:
            page = await self.page()
            failure = ""
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except PlaywrightTimeout:
                pass
            except PlaywrightError as e:  # DNS failure, blocked host, refused connection...
                failure = str(e).splitlines()[0][:300]
            await self._settle(page)
            summary = await self._summary(page)
            if failure:
                summary["navigation_error"] = failure
            return summary

    async def _settle(self, page: Page, timeout: int = 6000) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeout:
            pass
        try:
            # Oracle and Workday keep drawing the next step after the network goes quiet (its watch ends by 2 s: one
            # that answers nothing is a page that hangs)
            await _bounded(page.evaluate(QUIET_JS, [350, 2000]), 5)
        except _Hung:
            pass
        except PlaywrightError:
            await page.wait_for_timeout(300)  # it navigated meanwhile

    # ---------------------------------------------------------------- reading
    def _frame_prefix(self, frame: Frame, page: Page) -> str:
        if frame is page.main_frame:
            return ""
        if frame not in self._frame_ids:
            self._frame_ids[frame] = f"f{len(self._frame_ids) + 1}-"
        return self._frame_ids[frame]

    def _frame_for(self, page: Page, element_id: str) -> Frame:
        m = re.match(r"(f\d+-)", element_id)
        if not m:
            return page.main_frame
        for frame, prefix in self._frame_ids.items():
            if prefix == m.group(1) and not frame.is_detached():
                return frame
        raise KeyError(f"The frame holding {element_id} is gone; call inspect_form again")

    async def _extract(self, page: Page) -> dict[str, Any]:
        """Every frame's fields, buttons, errors, headings and dialogs, together; and each frame's own
        headings under `frame_headings`, by the prefix of its ids ("" for the page itself, "f2-"):
        what heads a button is in its own frame, not in the careers page around an iCIMS frame. Each
        frame's own address under `frame_urls`, by the same prefix: a saved password goes only into a
        frame on its own system (the pipeline's _password_place)."""
        result: dict[str, Any] = {"fields": [], "actions": [], "errors": [], "headings": [], "dialogs": [],
                                  "frame_headings": {}, "frame_urls": {}}
        for frame in page.frames:
            if frame.is_detached():
                continue
            if frame is not page.main_frame and _CAPTCHA_FRAME.match(frame.url):
                if await self._shown(frame):  # never read, but said: it's for the person to solve
                    result["captcha"] = CAPTCHA_SAYS
                continue
            if frame is not page.main_frame and (not frame.url or frame.url == "about:blank"
                                                 or _MEDIA_FRAME.match(frame.url)):
                continue
            prefix = self._frame_prefix(frame, page)
            try:
                # (a frame whose page has hung gives no answer: bounded, so it never holds the
                # lock, the review of Oct 15)
                data = await _bounded(frame.evaluate(EXTRACT_JS, prefix))
            except _Hung:
                if frame is page.main_frame:
                    result["hung"] = True  # (read again at once would only wait as long again: inspect stops)
                continue
            except PlaywrightError:
                continue  # cross-origin frame that refused, or navigated mid-read
            if frame is not page.main_frame and not data.get("fields"):
                data["errors"] = []  # nothing to fill: an embed (a player, a widget), whose errors aren't the form's
            for key in ("fields", "actions", "errors", "headings", "dialogs"):
                result[key].extend(data.get(key, []))
            if data.get("busy") and (frame is page.main_frame or data.get("fields")):
                # a loading indicator on show (Workday's dots while a step loads); not an embed's with nothing to fill
                # (a chat widget's spinner: the review of 0.3.147)
                result["busy"] = True
            if data.get("empty_frame") and frame is page.main_frame:
                result["empty_frame"] = True  # (Workday's application frame with nothing in it yet: formjs's unmounted)
            result["frame_headings"][prefix] = list(data.get("headings", []))
            result["frame_urls"][prefix] = frame.url
        self._fields = {f["id"]: f for f in result["fields"]}
        self._actions = {a["id"]: a for a in result["actions"]}
        return result

    @staticmethod
    async def _shown(frame: Frame) -> bool:
        """Is this frame on show (a CAPTCHA's box or picture check), not kept hidden on the page?
        An invisible reCAPTCHA's badge (size=invisible) is on show but asks nothing."""
        if re.search(r"[?&]size=invisible\b", frame.url):
            return False
        try:
            element = await frame.frame_element()
            box = await element.bounding_box()
            return bool(box and box["width"] > 20 and box["height"] > 20 and await element.is_visible())
        except PlaywrightError:
            return False

    async def _summary(self, page: Page) -> dict[str, Any]:
        data = await self._extract(page)
        fields = data["fields"]
        return {
            "url": page.url,
            "title": await _page_title(page),
            "headings": data["headings"],
            "fields": len(fields),
            "questions": questions_shown(fields),
            "empty_required": sum(1 for f in fields if f.get("required") and is_empty_value(f.get("value"))),
            "actions": [a["text"] for a in data["actions"]][:25],
            "errors": data["errors"],
            **({"captcha": data["captcha"]} if data.get("captcha") else {}),
        }

    async def inspect(self, include_dropdown_options: bool = True) -> dict[str, Any]:
        async with self._lock:
            page = await self.page()
            data = await self._extract(page)
            for _ in range(FORM_DRAW_TRIES):  # Oracle can draw the form a moment after the rest of the page
                if data["fields"] or data.get("hung"):
                    break
                await page.wait_for_timeout(FORM_DRAW_WAIT)
                data = await self._extract(page)
            if include_dropdown_options:
                for f in data["fields"]:
                    # Only empty ones: opening a dropdown that has its answer (and pressing
                    # Escape after) can clear it, as Micron's did, so the same question
                    # came back after every fill.
                    # (nor a date picker's: its calendar has days, not choices, and on Eightfold it opens as a
                    # dialog that holds the focus, issue #112, note 41)
                    if (f["kind"] in ("listbox", "combobox") and not f.get("options") and not f.get("disabled")
                            and not f.get("date") and is_empty_value(f.get("value"))):
                        f["options"] = await self._read_listbox_options(page, f)
                        self._fields[f["id"]] = f
            return {"url": page.url, "title": await _page_title(page), **data}

    async def visible_text(self, max_chars: int = 8000) -> str:
        async with self._lock:
            page = await self.page()
            parts = []
            for frame in page.frames:
                if frame is not page.main_frame and _MEDIA_FRAME.match(frame.url):
                    continue
                try:
                    t = await _bounded(frame.evaluate(VISIBLE_TEXT_JS))
                except (PlaywrightError, _Hung):
                    continue
                if t:
                    parts.append(t)
            return "\n\n".join(parts)[:max_chars]

    async def html(self) -> str:
        async with self._lock:
            page = await self.page()
            return await page.content()

    async def capture_json(self, url: str, url_part: str, timeout: int = 25000,
                           want: Callable[[Any], bool] | None = None,
                           rewrite: Callable[[Any], Any] | None = None,
                           rewrite_url: Callable[[str], str] | None = None) -> Any:
        """Open `url` in a background tab and return the JSON of the first response whose URL
        contains `url_part`: the data a careers page loads for itself, when its API refuses
        direct requests. `want` picks among several such calls by their JSON request body;
        `rewrite` may change that body on its way out (e.g. a bigger page size), and
        `rewrite_url` the address of a call that has no body (Infor's "pagesize=10")."""
        def body_of(request: Any) -> Any:
            try:
                return json.loads(request.post_data or "null")
            except (ValueError, TypeError):
                return None

        async with self._lock:
            await self._ensure_ctx()
            assert self._ctx is not None
            tab = await self._ctx.new_page()  # not a popup of the job's tab, so it never becomes current
            try:
                if rewrite is not None or rewrite_url is not None:
                    async def handle(route: Any) -> None:
                        changed = rewrite(body_of(route.request)) if rewrite is not None else None
                        address = rewrite_url(route.request.url) if rewrite_url is not None else route.request.url
                        if changed is None:
                            await route.continue_(url=address)
                        else:
                            await route.continue_(url=address, post_data=json.dumps(changed))
                    await tab.route(lambda u: url_part in u, handle)

                def matches(r: Any) -> bool:
                    return url_part in r.url and r.ok and (want is None or bool(want(body_of(r.request))))

                async with tab.expect_response(matches, timeout=timeout) as info:
                    await tab.goto(url, wait_until="domcontentloaded", timeout=45000)
                return await (await info.value).json()
            finally:
                await tab.close()

    async def background_html(self, url: str) -> str:
        """The HTML of `url`, read in a background tab so the tab an application is in
        stays where it is (the Job Desk adds pasted links while it applies)."""
        async with self._lock:
            await self._ensure_ctx()
            assert self._ctx is not None
            tab = await self._ctx.new_page()
            try:
                await tab.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    await tab.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeout:
                    pass  # pages that keep polling: what's drawn by now is enough
                return await tab.content()
            finally:
                await tab.close()

    async def challenge_showing(self) -> bool:
        """Is a CAPTCHA challenge showing on the current tab, in any of its frames? It's
        for the person to solve; this only notices it."""
        async with self._lock:
            page = await self.page()
            for frame in page.frames:
                try:
                    if await _bounded(frame.evaluate(CHALLENGE_JS)):
                        return True
                except (PlaywrightError, _Hung):  # a frame that went away meanwhile, or hung
                    continue
            return False

    async def peek(self, tab: Page, max_chars: int = 4000) -> tuple[dict[str, Any], str]:
        """What a tab shows (its fields, buttons, text and any CAPTCHA), read without making it
        the tab the tools act on and without touching what they last read: the Job Desk looks
        in on jobs left waiting for the person this way, while Claude may be using the tools."""
        async with self._lock:
            data: dict[str, Any] = {"fields": [], "actions": [], "errors": [], "headings": []}
            urls: dict[str, str] = {}  # (each frame's own address, by its ids' prefix: as _extract's frame_urls)
            parts, challenge = [], False
            for frame in tab.frames:
                if frame.is_detached() or frame is not tab.main_frame and (not frame.url or frame.url == "about:blank"
                                                                           or _MEDIA_FRAME.match(frame.url)):
                    continue
                form = not _CAPTCHA_FRAME.match(frame.url)  # (a CAPTCHA's own frame: no form, nor a frame number)
                try:
                    seen = await _bounded(frame.evaluate(PEEK_JS,
                                                         [form, self._frame_prefix(frame, tab) if form else ""]))
                except (PlaywrightError, _Hung):
                    continue
                found, text = seen.get("found") or {}, seen.get("text") or ""
                challenge = challenge or bool(seen.get("challenge"))
                if frame is not tab.main_frame and not found.get("fields"):
                    found = {**found, "errors": []}  # (as in _extract: an embed's errors aren't the form's)
                for key in data:
                    data[key].extend(found.get(key, []))
                if form:
                    urls[self._frame_prefix(frame, tab)] = frame.url
                if text:
                    parts.append(text)
            loading = await tab.evaluate("() => document.readyState !== 'complete'")
            return ({"url": tab.url, "title": await tab.title(), **data, "frame_urls": urls, "challenge": challenge,
                     "loading": loading},
                    "\n\n".join(parts)[:max_chars])

    async def visit(self, url: str) -> str:
        """Open a link in a tab of its own and close it again, without changing the tab the
        tools act on: a confirmation link from the person's email, opened in this browser so
        the site knows the address is confirmed. Returns the address it ended on."""
        async with self._lock:
            await self._ensure_ctx()
            assert self._ctx is not None
            tab = await self._ctx.new_page()
            try:
                await tab.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    await tab.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeout:
                    pass
                return tab.url
            finally:
                await tab.close()

    @staticmethod
    async def _open_board(tab: Page, url: str) -> None:
        """Open a job board's page in a background tab. A server error page is the board being
        down, not a board with no openings."""
        response = await tab.goto(url, wait_until="domcontentloaded", timeout=45000)
        if response is not None and response.status >= 500 and not _CHALLENGE_TITLE.search(await tab.title()):
            # (Cloudflare's "Just a moment…" check comes as a 503 too, and clears by itself)
            raise SiteDown(f"{urlparse(url).hostname} is down right now (HTTP {response.status}); try again later")

    async def frames_html(self, url: str, inner: str | None = None) -> list[str]:
        """The HTML of `url` and of each frame on it, read in a background tab. Some job
        boards (iCIMS) turn away plain requests and list their openings inside a frame. With
        `inner` (that frame's selector), the page is read as soon as the frame's own page is
        in: iCIMS portals go on loading trackers for seconds after their openings are drawn,
        and the browser is held meanwhile."""
        async with self._lock:
            await self._ensure_ctx()
            assert self._ctx is not None
            tab = await self._ctx.new_page()
            try:
                await self._open_board(tab, url)
                started = time.monotonic()
                if not inner or not await self._frame_loaded(tab, inner):
                    # (for no longer than the page would have been given anyway: the browser is held)
                    left = SETTLE_WAIT - (time.monotonic() - started)
                    try:
                        if left > 0:
                            await tab.wait_for_load_state("networkidle", timeout=left * 1000)
                    except PlaywrightTimeout:
                        pass  # pages that keep polling: what's drawn by now is enough
                pages = []
                for frame in tab.frames:
                    try:
                        pages.append(await frame.content())
                    except PlaywrightError:  # a frame that went away meanwhile
                        continue
                return pages
            finally:
                await tab.close()

    @staticmethod
    async def _frame_loaded(tab: Page, selector: str) -> bool:
        """Wait for the frame `selector` names to load its page (not the about:blank it starts
        on), for FRAME_WAIT seconds in all."""
        deadline = time.monotonic() + FRAME_WAIT
        try:
            element = await tab.wait_for_selector(selector, state="attached", timeout=FRAME_WAIT * 1000)
            inner = await element.content_frame() if element is not None else None
            left = deadline - time.monotonic()
            if inner is None or left <= 0:
                return False
            await inner.wait_for_url(re.compile(r"^https?://"), wait_until="domcontentloaded", timeout=left * 1000)
            return True
        except PlaywrightError:  # (a timeout too): read it once the page has settled instead
            return False

    async def listing_pages(self, url: str, rows: str, per_page: tuple[str, str] | None = None,
                            next_button: str | None = None, max_pages: int = 5) -> list[str]:
        """The HTML of a job list the page's script draws (SuccessFactors' older career sites),
        a page at a time, read in a background tab: wait for its `rows`, choose a bigger page
        size where it offers one (`per_page`: the select and the option), then press
        `next_button` until it's gone or `max_pages` are read."""
        async with self._lock:
            await self._ensure_ctx()
            assert self._ctx is not None
            tab = await self._ctx.new_page()
            try:
                await self._open_board(tab, url)
                await tab.wait_for_selector(rows, timeout=20000)
                if per_page is not None and await tab.locator(per_page[0]).count():
                    shown = await self._rows_shown(tab, rows)
                    await tab.locator(per_page[0]).first.select_option(per_page[1])
                    await self._rows_change(tab, rows, shown)
                pages = [await tab.content()]
                while next_button and len(pages) < max_pages:
                    nxt = tab.locator(next_button)
                    if not await nxt.count():
                        break
                    shown = await self._rows_shown(tab, rows)
                    await nxt.first.evaluate("el => el.click()")  # an icon-only arrow: its own click handler
                    if not await self._rows_change(tab, rows, shown):
                        break  # the list didn't move on: what's read so far is the list
                    pages.append(await tab.content())
                return pages
            finally:
                await tab.close()

    @staticmethod
    async def _rows_shown(tab: Page, rows: str) -> tuple[int, str]:
        loc = tab.locator(rows)
        n = await loc.count()
        return n, (await loc.first.inner_text() if n else "")

    async def _rows_change(self, tab: Page, rows: str, shown: tuple[int, str], wait_ms: int = 8000) -> bool:
        """Wait until the list shows other rows than `shown` (how many, and the first)."""
        waited = 0
        while waited < wait_ms:
            await tab.wait_for_timeout(250)
            waited += 250
            try:
                if await self._rows_shown(tab, rows) != shown:
                    await tab.wait_for_timeout(500)  # the rest of the list is drawn with it
                    return True
            except PlaywrightError:  # redrawn while being read
                continue
        return False

    async def snapshot(self, dest: Path, note: str = "", details: Any = None) -> Path:
        """Save what's needed to debug a page later: HTML of every frame (never what's in a
        password box), a screenshot and the extracted fields. Stays on the user's machine."""
        async with self._lock:
            page = await self.page()
            dest.mkdir(parents=True, exist_ok=True)
            data = await self._extract(page)
            frames, secret = [], False
            for i, frame in enumerate(f for f in page.frames if not f.is_detached()):
                name = "page.html" if frame is page.main_frame else f"frame-{i}.html"
                try:
                    html = await frame.content()
                    secret = secret or bool(_SECRET_BOX.search(html))
                    (dest / name).write_text(_without_passwords(html), encoding="utf-8")
                    frames.append({"file": name, "url": frame.url})
                except PlaywrightError:
                    continue
            try:
                if not secret:  # (never a picture of an account's security answer)
                    (dest / "screenshot.jpg").write_bytes(
                        await page.screenshot(type="jpeg", quality=60, full_page=True)
                    )
            except PlaywrightError:
                pass
            meta = {"url": page.url, "title": await _page_title(page), "note": note, "frames": frames,
                    "details": details, **data}
            (dest / "snapshot.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
            return dest

    async def screenshot(self, full_page: bool = False, save_to: Path | None = None) -> bytes:
        async with self._lock:
            page = await self.page()
            data = await page.screenshot(type="jpeg", quality=60, full_page=full_page)
        if save_to:
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_bytes(data)
        return data

    # ---------------------------------------------------------------- writing
    async def mark_judged(self, left: list[str], filled: list[str]) -> None:
        """Mark lists whose first choice the desk judged the site's default (autofill.site_default), or whose country
        it judged the site's preset (autofill.preset_country): judged once, so a pick of that choice later (the
        person's, while the desk waits) is never taken for the site's again. A select `filled` only once it's off
        that first choice: one that went back to it didn't take the answer."""
        async with self._lock:
            page = await self.page()
            for element_id in [*left, *filled]:
                with contextlib.suppress(PlaywrightError, KeyError):
                    await self._locator(page, element_id).evaluate(
                        "(el, moved) => { if (!moved || el.tagName !== 'SELECT' || el.selectedIndex > 0)"
                        " el.setAttribute('data-ja-default-judged', '1'); }",
                        element_id in filled, timeout=2000)

    def _locator(self, page: Page, element_id: str) -> Locator:
        frame = self._frame_for(page, element_id)
        return frame.locator(_by_ja_id(element_id)).first

    async def _field(self, page: Page, field_id: str) -> dict:
        if field_id not in self._fields:
            await self._extract(page)
        if field_id not in self._fields:
            raise KeyError(f"No field {field_id!r} on the current page; call inspect_form to refresh ids")
        return self._fields[field_id]

    async def _current(self, page: Page, field: dict) -> dict:
        """The field as the page has it now. A site that draws a box afresh (Oracle redraws City,
        State and County once a ZIP is picked) drops the id it was given: the box is found
        again by its label, when only one box has it."""
        # a group's id is on its choices ("<id>.0", "<id>.1", …), not on an element of its own
        if field.get("kind") in ("radio_group", "checkbox_group"):
            _by_ja_id(field["id"])  # (a group's id of the reader's own shape only: raises on any other)
            here = self._frame_for(page, field["id"]).locator(f'[data-ja-gid-member="{field["id"]}"]').first
        else:
            here = self._locator(page, field["id"])
        if await self._present(here):
            return field
        for wait in (0, 500, 1000, 1500):  # a box part-way through being drawn again isn't there yet
            if wait:
                await page.wait_for_timeout(wait)
            await self._extract(page)
            same = [f for f in self._fields.values()
                    if f.get("label") == field.get("label") and f.get("kind") == field.get("kind")]
            if len(same) == 1:
                return same[0]
            if same:
                break  # two boxes with its label: waiting won't tell them apart
        raise KeyError(f"{field.get('label')!r} was drawn again and can't be told apart; call inspect_form")

    async def fill(self, values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fill fields by id. Each item: {"id": ..., "value": ...}. Files go first (an upload may load its form again,
        or fill it from the file: the rest goes into the form as it is after it); the results come in the order given.
        """
        async with self._lock:
            page = await self.page()
            await self._close_menus(page)
            out: dict[int, dict[str, Any]] = {}
            known = dict(self._fields)  # the boxes as read before filling, by the ids given out then
            given = {str(item.get("id", "")): item.get("value") for item in values}  # (a date's other parts)
            self._date_typed = set()  # a date's parts typed in this fill (with its part given first)
            order = sorted(range(len(values)),
                           key=lambda n: (known.get(str(values[n].get("id", ""))) or {}).get("kind") != "file")
            gone: set[str] = set()  # boxes the page no longer has, once an upload loaded it again
            loads: list[tuple[Any, Frame]] = []  # page loads begun meanwhile, and in which frame

            def loading(request: Any) -> None:
                with contextlib.suppress(Exception):
                    if request.is_navigation_request():
                        loads.append((request, request.frame))
            page.on("request", loading)
            try:
                for at, i in enumerate(order):
                    item = values[i]
                    fid = str(item.get("id", ""))
                    if item.get("again") and fid in self._date_typed:  # (typed again with its date's first part)
                        out[i] = {"id": fid, "label": "", "ok": True, "result": "typed"}
                        continue
                    if fid in gone:
                        out[i] = {"id": fid, "ok": False, "error": "KeyError: the page loaded its form again after "
                                  "the upload, and this box isn't on it now; call inspect_form"}
                        continue
                    try:
                        field = await self._current(page, known.get(fid) or await self._field(page, fid))
                        if item.get("secret"):  # (the box as found now, marked before anything goes in: _keep_secret)
                            await self._keep_secret(page, field["id"])
                        if item.get("names"):  # a school or an employer: matched by name only
                            field = {**field, "names": True}
                        if item.get("near"):  # an address part: the rest of the address, for a place lookup
                            field = {**field, "near": item["near"]}
                        if item.get("pick_only"):  # a self-identification answer: picked from the list, never typed
                            field = {**field, "pick_only": True}
                        if item.get("again"):  # answered again where the page lost it (a date: cleared first)
                            field = {**field, "again": True}
                        if not item.get("again") and await self._holds(page, field, item.get("value")):
                            if item.get("secret"):
                                await self._keep_secret(page, field["id"], confirmed=True)
                            out[i] = {"id": fid, "label": field.get("label", ""), "ok": True, "result": "already set"}
                            continue
                        await self._clear_of_dialogs(page, field)
                        upload = field.get("kind") == "file"
                        if upload:  # (this page told from one an upload may load in its place)
                            frame = self._frame_for(page, field["id"])
                            with contextlib.suppress(PlaywrightError):
                                await frame.evaluate("() => { window.__jaBeforeUpload = true; }")
                            began = len(loads)
                        outcome = await self._fill_one(page, field, item.get("value"), given)
                        if item.get("secret"):
                            await self._keep_secret(page, field["id"], confirmed=True)
                        out[i] = {"id": fid, "label": field.get("label", ""), "ok": True, "result": outcome}
                        if outcome in ("filled", "typed") and not item.get("secret"):
                            typed = self._typed.setdefault(page.url, set())
                            if len(typed) < 200:
                                typed.add(norm(str(item.get("value"))))
                        if upload and outcome.startswith("uploaded"):
                            rest = [str(values[n].get("id", "")) for n in order[at + 1:]]
                            gone |= await self._after_reload(page, frame, loads, began, known, rest)
                    except Exception as e:  # report and keep going; one odd widget shouldn't stop the rest
                        out[i] = {"id": fid, "ok": False,
                                  "error": f"{type(e).__name__}: {str(e).splitlines()[0][:300]}",
                                  **({"options": e.entries} if isinstance(e, (PickedAGroup, NoMatch)) else {})}
                    finally:
                        # a picker this box opened (a date's calendar) closed before the next box: on Eightfold one
                        # left open kept the focus, and Country of Residence took nothing typed (issue #112, note 41)
                        with contextlib.suppress(Exception):
                            await self._close_pickers(page, self._own_frames(page, fid), fid)
                results = [out[i] for i in range(len(values))]
                if self._typed.get(page.url) and any(r["ok"] and r["result"] != "already set" for r in results):
                    await self._fill_lost(page)
            finally:
                page.remove_listener("request", loading)
                await self._close_menus(page)  # none left open over the buttons, or over its own field
            return results

    async def _after_reload(self, page: Page, frame: Frame, loads: list[tuple[Any, Frame]], began: int,
                            known: dict[str, dict], rest: list[str]) -> set[str]:
        """After an upload that sent its form, and so loads its page (or the frame it's in) again (iCIMS's resume box
        submits its frame's form to read the file: Valley Health's, run 144): that page waited for (at most
        UPLOAD_RELOAD_WAIT), read afresh, and each box still to fill (`rest`) found on it again as the same question in
        the same place (its label, kind and section, and which of those it is), put in `known` as it is now. Gives back
        the boxes the page no longer has, to be said at once: on the ids read before, each was waited on for 15 s, and
        what went into the page before it came back was lost with it. Nothing where no page load began."""
        def own() -> list[Any]:
            return [r for r, f in loads[began:] if f in (frame, page.main_frame)]
        waited = 0.0
        while not own() and waited < UPLOAD_RELOAD_START:
            await page.wait_for_timeout(100)
            waited += 0.1
        if not (sent := own()):
            return set()
        reloaded = next(f for r, f in loads[began:] if r is sent[0])
        deadline = time.monotonic() + UPLOAD_RELOAD_WAIT
        while time.monotonic() < deadline and not reloaded.is_detached() and not any(r.failure for r in sent):
            try:
                if await reloaded.evaluate("() => !window.__jaBeforeUpload && document.readyState === 'complete'"):
                    break
            except PlaywrightError:  # (between the two pages)
                pass
            await page.wait_for_timeout(200)
        prefix = "" if reloaded is page.main_frame else self._frame_ids.get(reloaded, "?")
        before = list(known.values())
        fresh = (await self._extract(page))["fields"]

        def places(fields: list[dict]) -> dict[tuple, dict]:  # the reloaded frame's boxes by question, and which one
            seen: dict[tuple, int] = {}
            out = {}
            for f in fields:
                m = re.match(r"f\d+-", str(f.get("id")))
                if (m.group(0) if m else "") != prefix:
                    continue
                key = (f.get("label"), f.get("kind"), f.get("section"))
                seen[key] = seen.get(key, 0) + 1
                out[key + (seen[key],)] = f
            return out
        then = {str(f.get("id")): where for where, f in places(before).items()}
        now = places(fresh)
        lost = set()
        for fid in rest:
            if fid in then:
                if (box := now.get(then[fid])) is not None:
                    known[fid] = box
                else:
                    lost.add(fid)
        return lost

    async def _keep_secret(self, page: Page, element_id: str, confirmed: bool = False) -> None:
        """A box for an account's security answer, the person's (the desk's card), marked before it's filled
        ("pending"), and once its answer is in ("in", `confirmed`): the form reader never reads it back (a real answer
        reads "(set)" only once it's in, anything else in it "(unconfirmed)": formjs), a saved page never holds it, it's
        never typed again, and what's typed in it shows as dots, as a password's does (in a screenshot too; a chosen
        question shows as chosen; a list's choice is kept as confirmed, and another one since reads "(unconfirmed)").
        Raises when it can't be marked, and then nothing goes in (or it isn't confirmed)."""
        await self._frame_for(page, element_id).locator(_by_ja_id(element_id)).first.evaluate(
            """(el, state) => {
              el.setAttribute('data-ja-secret', state);
              const list = el.tagName === 'SELECT' ? el
                : el.id && el.id.endsWith('-button') ? document.getElementById(el.id.slice(0, -'-button'.length)) : null;
              if (state === 'in' && list && list.tagName === 'SELECT')
                el.setAttribute('data-ja-secret-index', String(list.selectedIndex));
              if (['INPUT', 'TEXTAREA'].includes(el.tagName)) el.style.setProperty('-webkit-text-security', 'disc');
            }""", "in" if confirmed else "pending", timeout=3000)  # noqa: E501

    async def _clear_of_dialogs(self, page: Page, field: dict) -> None:
        """Nothing is filled behind a dialog open over the page: the boxes under it time out or
        lose what's put in them, and the dialog is answered first (Eightfold opens its notice about
        its AI screening as the resume goes up, part-way through a page's fills)."""
        try:
            over = await self._frame_for(page, field["id"]).evaluate(NOTICE_OVER_JS, field["id"])
        except PlaywrightError:  # a frame mid-way through loading: the fill says what's wrong, if anything
            return
        if over:
            raise DialogOpen(f"“{over[:160]}” is open over the page; it's answered first, then this is filled")

    async def _fill_lost(self, page: Page) -> None:
        """Type again what a text box shows when it's something the desk typed on this page and
        the page marks the box invalid all the same: the page's own record of it was lost.
        Insight's Eightfold form drops some of a quick run of fills (this one's or an earlier
        one's), then says "Last Name cannot be left blank" beside the name shown. One at a time,
        emptied first (a framework ignores a box set to the value it already shows); never a
        hidden box or the one the person is in. A second look after that, for one the page
        marked a little later (or lost again in the rush). A box the page goes on refusing is
        left alone after LOST_FILL_TRIES."""
        typed = self._typed.get(page.url) or set()
        for _ in range(2):
            await page.wait_for_timeout(LOST_FILL_WAIT * 1000)
            again = False
            for frame in [f for f in page.frames if not f.is_detached()]:
                try:
                    lost = await frame.evaluate(LOST_BOXES_JS)
                except PlaywrightError:
                    continue
                for box in lost:
                    key = (page.url, box["id"], norm(box["value"]))
                    if key[2] not in typed or self._retyped.get(key, 0) >= LOST_FILL_TRIES:
                        continue
                    self._retyped[key] = self._retyped.get(key, 0) + 1
                    if not _JA_ID.fullmatch(str(box["id"])):
                        continue
                    loc = frame.locator(_by_ja_id(box["id"])).first
                    try:
                        await loc.fill("", timeout=3000)
                        await loc.fill(box["value"], timeout=3000)
                        await loc.evaluate("el => el.blur()")
                        await page.wait_for_timeout(LOST_FILL_WAIT * 800)
                        again = True
                    except (PlaywrightError, PlaywrightTimeout):
                        continue
            if not again:
                return

    async def retype(self, field_ids: list[str]) -> list[str]:
        """Type again, key by key, what text boxes show: emptied first (a framework ignores a box set to the value
        it already shows), then blurred. For boxes the page says are empty though they show a value (its own
        record of them was lost). The ids typed again."""
        done = []
        async with self._lock:
            page = await self.page()
            for field_id in field_ids:
                loc = self._locator(page, field_id)
                value = ""
                try:
                    if await loc.evaluate("el => /^(date|time|datetime-local|month|week)$/i.test(el.type || '')",
                                          timeout=FOCUS_TIMEOUT):
                        continue  # (a date's segments take keys their own way)
                    value = await loc.input_value(timeout=FOCUS_TIMEOUT)
                    if not value.strip():
                        continue
                    await loc.fill("", timeout=FOCUS_TIMEOUT)
                    await loc.press_sequentially(value, delay=30, timeout=FOCUS_TIMEOUT * 3)
                    if await loc.input_value(timeout=FOCUS_TIMEOUT) != value:  # (an autocomplete changed it, say)
                        await loc.fill(value, timeout=FOCUS_TIMEOUT)
                    await loc.evaluate("el => el.blur()", timeout=FOCUS_TIMEOUT)
                    done.append(field_id)
                except (PlaywrightError, PlaywrightTimeout):
                    if value:  # never left emptied or half typed
                        with contextlib.suppress(PlaywrightError, PlaywrightTimeout):
                            await loc.fill(value, timeout=FOCUS_TIMEOUT)
                    continue
        return done

    async def type_code(self, field_ids: list[str], code: str) -> list[str]:
        """An emailed code typed key by key, as the person would: a digit into each box of a box-per-digit
        code, or the whole code into its one box. Edward Jones' BrassRing (live run 9, Oct 11) took a digit
        only from a key pressed in its box: a value put in by the page's script left the boxes empty, and
        Verify Passcode with them. What each box reads after."""
        async with self._lock:
            page = await self.page()
            await self._clear_of_dialogs(page, {"id": field_ids[0]})  # (never typed behind a dialog)
            per_box = len(field_ids) > 1
            for i, field_id in enumerate(field_ids):
                loc = self._locator(page, field_id)
                # (focus within its own document isn't enough: a frame keeps its activeElement after focus left it)
                if not await loc.evaluate("el => el.ownerDocument.hasFocus() && el === el.ownerDocument.activeElement",
                                          timeout=FOCUS_TIMEOUT):
                    try:
                        await loc.click(timeout=2000)
                    except PlaywrightTimeout:
                        await loc.focus(timeout=FOCUS_TIMEOUT)
                if await loc.evaluate("el => !!el.value", timeout=FOCUS_TIMEOUT):
                    await loc.fill("", timeout=FOCUS_TIMEOUT)
                await page.keyboard.type(code[i] if per_box else code, delay=60)
            await page.wait_for_timeout(150)
            return [await self._locator(page, field_id).evaluate(
                "el => el.value !== undefined ? String(el.value) : (el.innerText || '')", timeout=FOCUS_TIMEOUT)
                for field_id in field_ids]

    async def fill_secret(self, field_id: str, secret: str, site_ok: Callable[[str], bool] | None = None) -> None:
        """Type a secret into a password box, on a page `site_ok` accepts (by its frame's
        address): never into a text box, where the next inspect_form would read it back."""
        async with self._lock:
            page = await self.page()
            await self._field(page, field_id)
            frame = self._frame_for(page, field_id)
            loc = frame.locator(_by_ja_id(field_id)).first
            if not await loc.evaluate("el => el.tagName === 'INPUT' && el.type === 'password'"):
                raise PermissionError("A saved password only goes into a password box")
            if site_ok is not None and not site_ok(frame.url):
                raise PermissionError("That saved password belongs to another site")
            await loc.fill(secret)

    async def human_submit_ats(self, page: Page | None = None) -> str | None:
        """LinkedIn or Indeed, when the page or any frame in it is theirs: an employer's page
        can embed Indeed's form, whose Submit is the person's to press."""
        page = page or await self.page()
        for frame in page.frames:
            ats = detect_ats(frame.url)
            if ats not in config.HUMAN_SUBMIT_ONLY:
                continue
            if frame is page.main_frame:
                return ats
            try:  # a frame holding a form, not a "Follow us on LinkedIn" widget
                if await frame.evaluate("() => [...document.querySelectorAll('input, textarea, select')].some((e) => "
                                        "e.type !== 'hidden' && e.getClientRects().length > 0)"):
                    return ats
            except Exception:  # couldn't look: theirs to submit, to be safe
                return ats
        return None

    async def _holds(self, page: Page, field: dict, value: Any) -> bool:
        """Does this box already show the answer? The site may have filled it from an earlier
        one (Oracle fills City, State and County from the ZIP picked), and filling it again can
        undo that: picking a State there empties the City and ZIP below it. A box the site marks
        invalid, or a Workday prompt (whose box holds search words, not its choice), never counts."""
        if field.get("kind") not in ("text", "combobox") or value is None or isinstance(value, (bool, list, dict)):
            return False
        loc = self._locator(page, field["id"])
        try:
            current = (await loc.input_value(timeout=2000)).strip()
            if not current or await loc.evaluate(
                    "el => el.getAttribute('aria-invalid') === 'true' || el.tagName !== 'INPUT'") \
                    or await loc.evaluate(WORKDAY_PROMPT_JS):
                return False
        except (PlaywrightError, PlaywrightTimeout):
            return False
        if field.get("date") and (day := _as_date(value, field)) is not None and _shows_day(current, day):
            return True  # (a date picker shows its day in its own form: "Nov 2, 2026" for "11/02/2026")
        return norm(current) == norm(str(value)) or choose_option(str(value), [current], exact_only=True) is not None

    async def _fill_one(self, page: Page, field: dict, value: Any, given: dict[str, Any] | None = None) -> str:
        kind = field["kind"]
        loc = self._locator(page, field["id"])
        if kind == "password" and not _ONE_TIME_CODE.search(field.get("label") or ""):
            # (but an emailed code drawn in a password box, SuccessFactors' "*Passcode:", is no password)
            raise PermissionError("Passwords are only filled with fill_secret")
        if kind == "file":
            path = config.expand(str(value))
            if not path or not path.exists():
                raise FileNotFoundError(f"No file at {value}")
            if _desk_file(path):
                raise PermissionError(f"{path.name} is one of the desk's own files (settings, passwords, answers), "
                                      "not a document to upload")
            # Once a page: UKG Pro (Nikon's, live run 13) empties its upload box once it has the file, so it read as
            # never uploaded, and each upload set its resume reading going again, which emptied Address 1 and Zip
            # after they were filled. Remembered on the box, so a reloaded page (its form reset) gets it again
            # (on the box itself: another box, or a step a single-page app draws afresh, gets its own). UKG draws its
            # box afresh too, losing that, but names the file it has ("<file> has been used to pre-fill part of your
            # application", live run 15): a file the page names is there already. So is one iCIMS keeps in its hidden
            # "<name>_FileName" box once its page is back from reading it (Valley Health's, run 144)
            if await loc.evaluate("(el, k) => el.__jaUploaded === k || (el.ownerDocument.body.innerText || "
                                  "'').includes(k) || !!(el.id && el.id.endsWith('_File') && (el.ownerDocument"
                                  ".getElementById(el.id.slice(0, -5) + '_FileName') || {}).value === k)",
                                  path.name):
                return "already uploaded on this page"
            if not await loc.evaluate("el => el.tagName === 'INPUT' && el.type === 'file'"):
                return await self._upload_through(page, loc, path)
            await loc.set_input_files(str(path))
            try:
                said = await self._send_dialog_upload(page, loc, path)
            except PlaywrightError as e:  # the file sent its form at once, and its page is loading again (iCIMS's)
                if "navigation" not in str(e):
                    raise
                return f"uploaded {path.name}"
            with contextlib.suppress(PlaywrightError, PlaywrightTimeout):  # (a box drawn afresh at once is gone)
                # (only once it's sent: a dialog's Upload that didn't go is pressed again on the next fill)
                await loc.evaluate("(el, k) => { el.__jaUploaded = k; }", path.name, timeout=1500)
            return said
        if kind == "checkbox":
            want = value if isinstance(value, bool) else bool(polarity(value))
            await self._set_checked(loc, want)
            return "checked" if want else "unchecked"
        if kind in ("radio_group", "checkbox_group"):
            wanted = value if isinstance(value, list) else [value]
            picked = []
            for w in wanted:
                choice = choose_option(w, field.get("options") or [])
                if choice is None:
                    raise ValueError(f"{w!r} doesn't match any option: {field.get('options')}")
                idx = field["options"].index(choice)
                await self._set_checked(self._locator(page, f"{field['id']}.{idx}"), True)
                picked.append(choice)
            return "selected " + ", ".join(picked)
        if kind == "select":
            try:  # as the page has them now: a State list fills in once a Country is picked
                options = [o for o in await loc.evaluate(
                    "el => Array.from(el.options).map((o) => (o.text || '').replace(/\\s+/g, ' ').trim())") if o]
            except PlaywrightError:
                options = field.get("options") or []
            choice = choose_option(value, options)
            if choice is None:
                raise ValueError(f"{value!r} doesn't match any option: {options[:40]}")
            await loc.select_option(label=choice)
            return f"selected {choice}"
        if kind == "listbox":
            return await self._pick_from_listbox(page, loc, field, value)
        if kind == "combobox":
            if field.get("date") and (day := _as_date(value, field)) is not None:  # a date picker's: its day picked
                return await self._pick_date(page, loc, field, day)
            if field.get("readonly"):  # pick-only: open the menu and choose, nothing to type into
                return await self._pick_from_listbox(page, loc, field, value)
            if await loc.evaluate(TWIN_TICKED_JS) is not None:  # BrassRing's search box for several choices (run 88)
                return await self._tick_in_multiselect(page, loc, field, value)
            return await self._type_and_pick(page, loc, field, value)
        text = "" if value is None else str(value)
        if isinstance(value, bool):
            text = "Yes" if value else "No"
        if field.get("role") == "spinbutton":
            if await loc.evaluate(DATE_LIKE_JS, timeout=FOCUS_TIMEOUT):
                return await self._type_date_part(page, loc, text, given, clear=bool(field.get("again")))
            # a lone number box drawn as a spinbutton: its value typed as given
            await loc.fill("", timeout=FOCUS_TIMEOUT)
            await loc.press_sequentially(text, delay=40, timeout=FOCUS_TIMEOUT)
            await loc.evaluate("el => el.blur()", timeout=FOCUS_TIMEOUT)
            return "typed"
        await loc.fill(text)
        await loc.evaluate("el => el.blur()")
        return "filled"

    async def _set_checked(self, loc: Locator, want: bool) -> None:
        try:
            await loc.set_checked(want, timeout=3000)
            return
        except (PlaywrightError, PlaywrightTimeout):
            pass
        state = "el => el.checked === true || el.getAttribute('aria-checked') === 'true'"
        if await loc.evaluate(state) != want:
            await loc.evaluate(CLICK_CHOICE_JS)
            await loc.page.wait_for_timeout(200)
            if await loc.evaluate(state) != want:  # disabled until the terms are opened, or the page put it back
                raise ValueError("the page didn't take it: " + ("still unticked" if want else "still ticked"))

    async def click(self, target: str, allow_submit: bool = False, *, agreement: bool = False) -> dict[str, Any]:
        """Click an action/field by id, or the first visible button/link with that text.

        `agreement`: the Job Desk found the button agrees to an employer's notice or terms for the
        person (settings.accept_notices), which lets the live check's test identity press it in
        practice mode (_check_clickable). Only the desk's own presses say so, never the click tool."""
        async with self._lock:
            page = await self.page()
            by_id = (target in self._actions or target in self._fields
                     or re.fullmatch(r"(f\d+-)?[a-z]*\d+(\.\d+)?", target))
            loc = self._locator(page, target) if by_id else None
            text = self._actions[target]["text"] if target in self._actions else "" if by_id else target
            loc, info = await self._clickable(page, loc, text, allow_submit, target, agreement)
            before, note = page.url, None
            # (a new tab from now on is this press's: set under the lock, so a click waiting for it never claims
            # another's tab, the review of Oct 15)
            self._follow_until = time.monotonic() + POPUP_FOLLOW
            try:
                await loc.click(timeout=CLICK_TIMEOUT)
            except PlaywrightTimeout:
                present = await self._present(loc)
                # A page that moved on took the click (a slow navigation can outlast the
                # timeout); clicking again could act on the next page.
                if present or page.url == before:
                    # Knockout/React swapped the button out, or a cookie banner sits on top:
                    # click it directly, after finding and checking it again.
                    loc, info = await self._clickable(
                        page, loc if present else None, info["text"] or text, allow_submit, target, agreement)
                    await loc.evaluate("el => el.click()", timeout=5000)
                    note = "clicked directly: the button was covered or replaced"
                else:
                    note = "the click timed out but the page moved on, so it wasn't repeated; check the page"
            await self._settle(page)
            page = await self.page()  # the click may have opened a new tab
            summary = await self._summary(page)
            return {**summary, "note": note} if note else summary

    async def _clickable(self, page: Page, loc: Locator | None, text: str, allow_submit: bool,
                         target: str, agreement: bool = False) -> tuple[Locator, dict[str, Any]]:
        """The element to click and what it is; final submit buttons are refused.

        Oracle's and other Knockout/React pages replace buttons as they re-render, so one
        that's gone (since inspect_form, or while it was being read) is found again by its
        text, and whatever is found is checked again.
        """
        for _ in range(3):
            # (never for a press allowed to send, a Create Account's or a code's: its own button, or none. Found by its
            # words it could be any "Submit" on the page, past every check: the safety audit of Oct 12)
            if (loc is None or not await self._present(loc)) and text and not allow_submit:
                loc = await self._find_by_text(page, text)
            if loc is None or not await self._present(loc):
                break
            try:
                info = await loc.evaluate(ELEMENT_INFO_JS, timeout=5000)
            except PlaywrightTimeout:
                loc = None
                continue
            if not allow_submit:
                self._check_clickable(info, page.url, agreement)
            if await self._present(loc):
                return loc, info
            loc = None
        raise KeyError(f"Nothing clickable matches {target!r}; call inspect_form for ids")

    @staticmethod
    async def _present(loc: Locator) -> bool:
        try:
            return bool(await loc.count())
        except PlaywrightError:
            return False

    @staticmethod
    def _check_clickable(info: dict[str, Any], url: str = "", agreement: bool = False) -> None:
        label = " ".join((info.get("label") or "").split())
        text = (info.get("text") or "").strip()
        if _accepts_cookies(label, text, bool(info.get("cookie"))) and not may_accept_cookies(info):
            raise SubmitBlocked(
                f"Cookie and privacy banners aren't accepted for the user: {label!r} would accept one. Decline it if "
                "the banner offers that, or leave the choice to the user in the browser window. (Only where they set "
                "accept_cookies: true under settings: in profile.yaml is a banner with no way to decline accepted.)")
        if info.get("signInForm"):
            return  # a sign-in form's own button, though it reads "Submit" (SuccessFactors'): it only signs in
        # a posting's own Apply on SuccessFactors' older sites: an empty form, so nothing is sent
        opens = bool(info.get("formSubmit") and POSTING_PAGE_RE.search(url) and re.match(r"^apply( now)?$", text, re.I)
                     and not info.get("formFields"))
        if SUBMIT_RE.search(label) or (info.get("formSubmit") and FINALISH_RE.match(final_text(text)) and not opens):
            raise SubmitBlocked(
                f"{label!r} looks like the final submit button. Use submit_application "
                "(after the user confirms), or let the user click it in the browser."
            )
        # (a page's script can send the application from a plain button: "Finish", "Send": never pressed here, in
        # any mode; the person's Submit goes through submit_application, which reads the page for itself)
        if _SENDS.match(final_text(text)):
            # (and submit_application finds only a page's submit button, not a plain one with these words: the person
            # presses it, the review of 0.3.176)
            raise SubmitBlocked(f"{label!r} may send the application, so the click tool never presses it. Ask the user "
                                "to press it in the browser window, then record the application with update_job.")
        if info.get("formSubmit") and not NAVIGATION_RE.match(text) and not opens:
            settings = config.Profile.load().settings
            # The live check's test identity agrees to notices in practice mode (Settings.may_accept_notices):
            # a form's button the desk found agrees to one (iCIMS's "I Acknowledge the Privacy Notice"), and
            # never one whose words send. Any other profile's practice mode presses none of these.
            agrees = (agreement and not _SENDS_TOO.search(f"{label} {text}") and settings.test_identity
                      and settings.may_accept_notices)
            if settings.dry_run and not agrees:
                raise SubmitBlocked(f"Dry run: {label!r} may send the application, and it "
                                    "isn't a recognised step button.")

    async def _find_by_text(self, page: Page, text: str) -> Locator | None:
        """First visible button/link named `text` exactly, then one whose name holds it, then any visible text match,
        across frames: "Next" is the button named Next before "Next of kin details" (the review of Oct 15: a role's
        name matched as a part, and the first such on the page was pressed)."""
        frames = [f for f in page.frames if not f.is_detached() and not _CAPTCHA_FRAME.match(f.url)
                  and not _MEDIA_FRAME.match(f.url)]
        for how in ("exact", "named", "text"):
            for frame in frames:
                if how == "text":
                    loc = frame.get_by_text(text, exact=False)
                else:
                    exact = how == "exact"
                    loc = frame.get_by_role("button", name=text, exact=exact).or_(
                        frame.get_by_role("link", name=text, exact=exact))
                loc = loc.filter(visible=True).first
                try:
                    if await loc.count():
                        return loc
                except PlaywrightError:
                    continue
        return None

    async def _page_text(self, page: Page) -> str:
        text = ""
        for frame in page.frames:
            if frame is not page.main_frame and _MEDIA_FRAME.match(frame.url):
                continue
            try:
                text += "\n" + await _bounded(frame.evaluate(VISIBLE_TEXT_JS))
            except (PlaywrightError, _Hung):
                continue
        return text

    async def find_submit(self) -> list[dict[str, Any]]:
        async with self._lock:
            page = await self.page()
            data = await self._extract(page)
            # not a footer's job-alerts "Submit" (marked aside): pressing it sends no application
            # nor Workday's hidden button under the cover that takes the click (covered): pressing it does nothing
            return [a for a in data["actions"] if a.get("is_submit") and not a.get("disabled") and not a.get("aside")
                    and not a.get("covered")]

    async def words_around(self, element_id: str) -> str | None:
        """The words of the smallest block around an element that also holds a text box (a code
        step's Submit: the step's own words, not the page's behind its pop-up), or None once it's gone."""
        async with self._lock:
            page = await self.page()
            try:
                return await self._locator(page, element_id).evaluate(
                    "el => { for (let n = el.parentElement; n; n = n.parentElement) {"
                    " if (n.querySelector('input:not([type=hidden]):not([type=checkbox]):not([type=radio])'))"
                    " return (n.innerText || '').slice(0, 3000); } return ''; }", timeout=3000)
            except (PlaywrightError, PlaywrightTimeout):
                return None

    async def words_before(self, element_id: str) -> str | None:
        """The words just before a box: the nearest text before it, its own label's aside, within the blocks around
        it (a bare "I agree" whose question is the paragraph above it; Workday's sits six wrappers down from it, the
        full run of Oct 12), or None once it's gone."""
        async with self._lock:
            page = await self.page()
            try:
                return await self._locator(page, element_id).evaluate("""(el) => {
                  const own = [...(el.labels || [])].map((l) => (l.innerText || '').trim()).filter(Boolean);
                  const boxes = 'input:not([type=hidden]), select, textarea';
                  const said = (node) => node.nodeType === 3 ? (node.textContent || '').trim()
                    : node.nodeType === 1 ? (node.innerText || '').trim() : '';
                  // the text before it, text nodes too ("Join our talent network.<br><label>..."), up to another box
                  // (kept across levels: a short "Job alerts" just above it isn't dropped for a longer sentence further up,
                  // the review's)
                  const out = [];
                  for (let n = el, d = 0; n && n !== document.body && d < 8; n = n.parentElement, d++) {
                    const level = [];
                    let boxed = false;
                    for (let s = n.previousSibling; s && level.join(' ').length < 400; s = s.previousSibling) {
                      if (s.nodeType === 1 && (s.matches(boxes) || s.querySelector(boxes))) { boxed = true; break; }
                      const t = said(s);
                      if (t && !own.includes(t)) level.unshift(t);
                    }
                    out.unshift(...level);
                    // (and never past another box: the words above a form's email and password aren't this box's,
                    // the review of 0.3.133's)
                    if (out.join(' ').length > 15 || boxed) return out.join(' ').slice(-500);
                  }
                  // nothing before it: the widest of the few blocks around it that's still short, less its own label's
                  // (more words only make the box the person's, never the desk's: the review's)
                  let wide = '';
                  for (let n = el.parentElement, d = 0; n && n !== document.body && d < 8; n = n.parentElement, d++) {
                    let t = (n.innerText || '').trim();
                    for (const o of own) t = t.replace(o, '').trim();
                    if (t.length > 1500) break;
                    if (t.length > 15) wide = t;
                  }
                  return wide;
                }""", timeout=3000)  # noqa: E501
            except (PlaywrightError, PlaywrightTimeout):
                return None

    async def element_info(self, element_id: str) -> dict[str, Any] | None:
        """ELEMENT_INFO_JS's facts about an element on the page (a cookie banner's button: the
        banner's other buttons, and whether it covers the page), or None once it's gone."""
        async with self._lock:
            page = await self.page()
            try:
                return await self._locator(page, element_id).evaluate(ELEMENT_INFO_JS, timeout=3000)
            except (PlaywrightError, PlaywrightTimeout):
                return None

    async def press_submit(self, action_id: str) -> dict[str, Any]:
        if config.Profile.load().settings.dry_run:  # second lock on the door, after submit_application's
            raise SubmitBlocked("Dry run: submitting is disabled")
        async with self._lock:
            page = await self.page()
            if await self.human_submit_ats(page):  # and a third: LinkedIn's and Indeed's are the person's
                raise SubmitBlocked("LinkedIn and Indeed applications are submitted by you, in the browser")
            button = self._locator(page, action_id)
            try:
                info = await button.evaluate(ELEMENT_INFO_JS, timeout=3000)
            except (PlaywrightError, PlaywrightTimeout):
                raise SubmitBlocked("The submit button isn't on the page any more; look at the page again") from None
            if not (SUBMIT_RE.search(info.get("label") or "")
                    or info.get("formSubmit") and FINALISH_RE.match(final_text(info.get("text") or ""))):
                # an id from before the page (or the tab) changed now names something else
                raise SubmitBlocked(f"{info.get('label')!r} isn't a submit button; look at the page again")
            before = await self._page_text(page)
            self._follow_until = time.monotonic() + POPUP_FOLLOW  # a confirmation in a new tab
            await button.click(timeout=8000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                pass
            await page.wait_for_timeout(1500)
            # A thank-you that wasn't there before, on a page that doesn't say what's wrong with the form. Watched
            # for a while: Workday draws its "Application Submitted" dialog over the candidate home a few seconds
            # after the page has gone quiet (live test 2, issue #112, note 19)
            waited = 0.0
            while True:
                page = await self.page()
                text = await self._page_text(page)
                data = await self._extract(page)
                new = confirmations(text) - confirmations(before)
                sent_back = any(_FORM_ERROR.search(e or "") for e in data["errors"])
                if new or sent_back or waited >= CONFIRM_WAIT:
                    break
                await page.wait_for_timeout(1000)
                waited += 1
            return {
                "url": page.url,
                "confirmed": bool(new) and not sent_back,
                "errors": data["errors"],
                "text_excerpt": text.strip()[:1500],
            }
