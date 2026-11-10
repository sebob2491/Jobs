"""Paths, the applicant profile, settings and secrets.

Everything personal lives under JOB_APPLY_HOME (default ~/.job-apply), never in
the plugin directory, so updating or reinstalling the plugin can't lose it.
"""

from __future__ import annotations

import contextlib
import copy
import difflib
import json
import os
import re
import time
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent


def _plugin_root(env: str | None) -> Path:
    """The plugin's folder: CLAUDE_PLUGIN_ROOT when set (an installed copy can be pointed back
    that way), else the folder that holds server/src/job_apply. A value still reading
    "${CLAUDE_PLUGIN_ROOT}" is a host that didn't expand it (Claude Desktop's Code tab), and
    taking it made the profile template and the employer list unfindable."""
    if env and "${" not in env:
        return Path(env)
    return PACKAGE_DIR.parents[2]


PLUGIN_ROOT = _plugin_root(os.environ.get("CLAUDE_PLUGIN_ROOT"))
TEMPLATE_PROFILE = PLUGIN_ROOT / "templates" / "profile.example.yaml"

# Platforms whose user agreements prohibit automated use. The plugin fills the
# form, but the person clicks the final button themselves.
HUMAN_SUBMIT_ONLY = {"linkedin", "indeed"}

# Answer patterns from templates before 0.3.61, each with the template's pattern now. The old
# ones also answered the opposite question: the "Yes" for "willing to take a drug test?" went
# to "Have you ever failed one?". A profile still holding one word for word reads as the new one.
RETIRED_ANSWER_PATTERNS = {
    "clean ?room": r"^(?!.*\b(how (many|long|much)|years?|describe|explain|list)\b).*clean ?room",
    "lift .*(25|35|50) ?(lb|pound)": r"lift .*\b(25|35|50) ?(lb|pound)",
    "background check|drug (test|screen)":
        r"^(?!.*\b(fail\w*|refus\w*|positive|convict\w*|felon\w*|misdemeanor\w*|arrest\w*|guilty|crimes?|charge[sd]?|"
        r"offen[cs]es?|anything|prevent\w*|concerns?|issues?|problems?)\b)(?!.*\bcriminal (record|histor))"
        r".*(background (check|screen|investigation)|drug (test|screen))",
    "relatives?|family members?.*(employ|work)":
        r"^(?!relative\W).*(\b(relatives?|family members?)\b(.{0,60}\b(employ|work)| (at|in|with)\b)"
        r"|\b(employ|work).{0,60}\b(relatives?|family members?)\b)",
    "non-?compete|non-?solicit":
        r"^(?!.*\b(willing|agree|accept\w*|comply|abide|open to)\b)"
        r".*(\b(bound|subject|party|signed|have|has|currently|existing|restrict\w*)\b.{0,60}non-?(compete|solicit)"
        r"|non-?(compete|solicit).{0,80}\b(currently|in effect|restrict\w*|prevent\w*|bound|subject)\b)",
    "credit (check|history|report)":
        r"^(?!.*\b(ever|bankrupt\w*|delinquen\w*|default\w*|judgments?|liens?|collections?|negative|derogatory|"
        r"anything|explain|describe|issues?|problems?)\b).*credit (check|history|report)",
    "(employed|worked) (by|for) (a|any) .*government": r"^(?!.*\bcontract).*(employed|"
                                                       r"worked) (by|for) (a|any) .*government",
}


def _current_answer_patterns(data: dict[str, Any]) -> None:
    """Swap an older template's answer pattern for the template's own now, in memory only (the
    file is the person's). A pattern they wrote or changed is left alone."""
    answers = data.get("answers")
    if not isinstance(answers, list):
        return
    for item in answers:
        if isinstance(item, dict) and isinstance(item.get("match"), str):
            item["match"] = RETIRED_ANSWER_PATTERNS.get(item["match"], item["match"])


def plugin_version() -> str:
    """The plugin's version from its manifest, for the person to tell an update arrived."""
    try:
        return str(json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def home() -> Path:
    return Path(os.environ.get("JOB_APPLY_HOME", "~/.job-apply")).expanduser()


def own_file(url: str) -> bool:
    """A file: link into the desk's own folder (~/.job-apply: its settings, saved pages, the tracker, secrets).
    Never opened as a page or read as a posting, which would save its text where Claude reads it. In every
    spelling the browser opens: file:///home/x/..., file:/home/x/..., file:home/x/... and file://C:/Users/x/...
    (the review of 0.3.174: the last three got past a check for "file://" alone)."""
    if not url.strip().lower().startswith("file:"):
        return False
    try:
        parsed = urlparse(url.strip())
        path = parsed.path if parsed.path.startswith("/") else "/" + parsed.path
        if re.fullmatch(r"[A-Za-z]:", parsed.netloc):  # file://C:/Users/... (the drive read as the host)
            path = f"/{parsed.netloc}{path}"
        where = Path(url2pathname(path)).resolve()
        return where == home().resolve() or home().resolve() in where.parents
    except (OSError, ValueError):
        return True


def profile_path() -> Path:
    return home() / "profile.yaml"


def secrets_path() -> Path:
    return home() / "secrets.yaml"


def db_path() -> Path:
    return home() / "tracker.db"


def answers_path() -> Path:
    """Answers given in the Job Desk, kept apart so profile.yaml (and its comments) is never rewritten."""
    return home() / "answers.yaml"


def browser_profile_dir() -> Path:
    return home() / "browser"


def applications_dir() -> Path:
    return home() / "applications"


def ensure_home() -> Path:
    h = home()
    h.mkdir(parents=True, exist_ok=True)
    applications_dir().mkdir(exist_ok=True)
    if not profile_path().exists() and TEMPLATE_PROFILE.exists():
        shutil.copy(TEMPLATE_PROFILE, profile_path())
    return h


def expand(p: str | None) -> Path | None:
    if not p:
        return None
    return Path(os.path.expandvars(str(p))).expanduser()


SUBMIT_MODES = {"review", "auto", "dry_run"}
_MODE_ALIASES = {"dryrun": "dry_run", "practice": "dry_run", "test": "dry_run", "manual": "review", "confirm": "review"}


def _submit_mode(raw: Any) -> tuple[str, list[str]]:
    """'dry-run', 'Dry Run', 'dryrun' -> 'dry_run'. Unknown values fall back to review, with a warning."""
    if raw is None or raw == "":
        return "review", []
    mode = re.sub(r"[\s-]+", "_", str(raw).strip().lower())
    mode = _MODE_ALIASES.get(mode, mode)
    if mode in SUBMIT_MODES:
        return mode, []
    return "review", [f"Unknown settings.submit_mode {raw!r}; using 'review'. Use review, auto or dry_run."]


_CHANNELS = {"chrome": "chrome", "google chrome": "chrome", "chrome-beta": "chrome-beta", "chrome beta": "chrome-beta",
             "msedge": "msedge", "edge": "msedge",
             "microsoft edge": "msedge", "chromium": "chromium", "bundled": "chromium"}


def _channel(raw: Any) -> tuple[str, str]:
    """browser_channel as written ("edge", "Chrome", nothing) -> one the browser starts with."""
    if raw is None or str(raw).strip() == "":
        return "chrome", ""
    channel = _CHANNELS.get(" ".join(str(raw).strip().lower().split()))
    if channel:
        return channel, ""
    return "chrome", f"Unknown settings.browser_channel {raw!r}; using Chrome. Use chrome, msedge or chromium."


# What a yes/no setting says yes with, written in words or quotes ("yes", "on", "1"): one list for headless and the
# settings that are on unless turned off (the review of Oct 15: `1` turned headless on but left manage_accounts off).
# The opt-ins (email_codes, email_tracking, accept_cookies) take a real true only
_YES_WORDS = ("true", "yes", "on", "1")


def _says_yes(raw: Any) -> bool:
    return raw is True or type(raw) is int and raw == 1 or isinstance(raw, str) and raw.strip().lower() in _YES_WORDS


def _flag(raw: Any) -> bool:
    """A yes/no setting, also when it's written in quotes ("false")."""
    if isinstance(raw, str):
        return _says_yes(raw)
    return bool(raw)


def never_submit() -> bool:
    """Hard switch for tests and practice runs: nothing is ever submitted."""
    return os.environ.get("JOB_APPLY_NEVER_SUBMIT") == "1"


# The nightly live check's test identity (scripts/live_smoke.py --test-identity; the owner's call,
# Oct 10): a clearly fake applicant with an inbox of its own, which makes accounts and agrees to
# notices in practice mode, at one or two employers per job system, and never submits
LIVE_TEST_IDENTITY_ENV = "JOB_APPLY_LIVE_TEST_IDENTITY"
LIVE_TEST_EMAIL_ENV = "LIVE_TEST_EMAIL"


def live_test_identity(email: Any) -> bool:
    """Is this profile the live check's test identity? Only with JOB_APPLY_LIVE_TEST_IDENTITY=1
    and the hard switch JOB_APPLY_NEVER_SUBMIT=1 in the environment, and the profile's email
    being the environment's LIVE_TEST_EMAIL (or that inbox's own "+tag" address, which Gmail delivers to it:
    the live check's --plus probe): a person's own profile never turns it on."""
    wanted = os.environ.get(LIVE_TEST_EMAIL_ENV, "").strip().lower()
    if not (os.environ.get(LIVE_TEST_IDENTITY_ENV) == "1" and never_submit() and "@" in wanted
            and isinstance(email, str)):
        return False
    given = email.strip().lower()
    local, _, domain = wanted.partition("@")
    return (given == wanted
            or re.fullmatch(rf"{re.escape(local)}\+[a-z0-9-]{{1,40}}@{re.escape(domain)}", given) is not None)


# Account handling's default, where a profile doesn't say (settings.manage_accounts)
MANAGE_ACCOUNTS_DEFAULT = True
# And agreeing to notices about AI screening, privacy notices and terms, and attestations (settings.accept_notices)
ACCEPT_NOTICES_DEFAULT = True
# And the ranking's "worked before" part, from the person's own outcomes (settings.learn_from_responses)
LEARN_FROM_RESPONSES_DEFAULT = True


def _on_unless_off(raw: Any, malformed: bool, default: bool) -> bool:
    """`default` where the profile doesn't say; off for false, no or off, and for any other answer
    or a settings section that can't be read (it acts in the person's name: unclear is no)."""
    if malformed:
        return False
    if raw is None:
        return default
    return _says_yes(raw)


# How many jobs the Job Desk fills at once, each in its own tab (settings.jobs_at_once), where the profile doesn't say,
# and the most it takes
JOBS_AT_ONCE_DEFAULT = 2
JOBS_AT_ONCE_MAX = 5  # (the owner, Oct 24: a desk left to go through the whole list)


# How many days an application sent in waits with no reply before the desk's My applications marks it for a follow-up
# (settings.follow_up_days), where the profile doesn't say, and the most it takes
FOLLOW_UP_DAYS_DEFAULT = 10
FOLLOW_UP_DAYS_MAX = 90


def _whole_number(raw: Any, key: str, default: int, most: int) -> tuple[int, str]:
    """settings.<key> as a number from 1 to `most` (the default where it isn't one, the nearest end where it's past
    one), and what to say of it when it isn't."""
    if raw is None or isinstance(raw, str) and not raw.strip():
        return default, ""
    try:
        n = int(str(raw).strip()) if not isinstance(raw, bool) else None
    except ValueError:
        n = None
    if n is None or not 1 <= n <= most:
        return (default if n is None else min(max(n, 1), most),
                f"settings.{key} should be a number from 1 to {most}, not {raw!r}.")
    return n, ""


def _jobs_at_once(raw: Any) -> tuple[int, str]:
    return _whole_number(raw, "jobs_at_once", JOBS_AT_ONCE_DEFAULT, JOBS_AT_ONCE_MAX)


# A model's name as Claude Code's --model takes it: an alias ("sonnet") or a full name ("claude-sonnet-5-5"), 1 to 80
# of letters, digits and . _ : [ ] - (the Settings page's box: Version D), never beginning with one that isn't a letter
# or a digit (a "-" first would read as a switch of the claude command's)
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,79}$")


def _cover_letter_model(raw: Any) -> tuple[str, str]:
    """settings.cover_letter_model: the model Claude Code writes the desk's cover letters with ("" for the usual one,
    letters.DEFAULT_MODEL), and what to say of one that can't be a model's name."""
    if raw is None or isinstance(raw, str) and not raw.strip():
        return "", ""
    said = str(raw).strip()
    if isinstance(raw, str) and _MODEL_NAME.match(said):
        return said, ""
    return "", (f"settings.cover_letter_model should be a model's name, such as claude-opus-5-5, not {raw!r}; "
                "using the usual one.")


# Phone alerts (phone.py, the owner's choice of Nov 2): the daily summary's time where the profile doesn't say (this
# computer's clock, 24-hour)
PHONE_SUMMARY_DEFAULT = "18:00"
_CLOCK_TIME = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?\s*(?:([ap])\.?\s*m\.?)?$", re.I)


def _phone_summary_at(raw: Any) -> tuple[str, str]:
    """settings.phone_summary_at: the time of day the daily summary goes to the phone, as "HH:MM" ("" for none: false,
    off or no), and what to say of one that isn't a time. "18:00", "6 PM", "6:30pm"; an 18:00 written without quotes,
    which YAML reads as the number 1080 (minutes: base 60), is read as written."""
    if raw is None or isinstance(raw, str) and not raw.strip():
        return PHONE_SUMMARY_DEFAULT, ""
    if raw is False or isinstance(raw, str) and raw.strip().lower() in ("off", "no", "false", "none", "never"):
        return "", ""
    hour: int | None = None
    minute = 0
    if isinstance(raw, int) and not isinstance(raw, bool):
        if 0 <= raw <= 23:
            hour = raw
        elif 0 <= raw < 24 * 60:
            hour, minute = divmod(raw, 60)
    elif isinstance(raw, str) and (m := _CLOCK_TIME.match(raw.strip())):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3):  # "6 PM", "12 am"
            hour = hour % 12 + (12 if m.group(3).lower() == "p" else 0) if 1 <= hour <= 12 else None
    if hour is None or not (0 <= hour <= 23 and 0 <= minute <= 59):
        return PHONE_SUMMARY_DEFAULT, (f"settings.phone_summary_at should be a time of day, such as 18:00 or 6 PM (or "
                                       f"off), not {raw!r}; using {PHONE_SUMMARY_DEFAULT}.")
    return f"{hour:02d}:{minute:02d}", ""


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def _phone_alerts_server(raw: Any) -> tuple[str, str]:
    """settings.phone_alerts_server: the ntfy server phone alerts go through, for a person who runs their own ("" for
    ntfy.sh's), and what to say of one that can't be used: only https (http only on this computer itself), and never
    a name and password in the address (said without the address, which may hold them)."""
    if raw is None or isinstance(raw, str) and not raw.strip():
        return "", ""
    said = str(raw).strip().rstrip("/")
    try:
        parsed = urlparse(said)
        host = parsed.hostname or ""
        credentials = parsed.username is not None or parsed.password is not None or "@" in parsed.netloc
    except ValueError:
        parsed, host, credentials = urlparse(""), "", False
    if credentials:
        return "", ("settings.phone_alerts_server has a name or password in its address, which phone alerts never "
                    "send; using ntfy.sh. Give your server's address alone, such as https://ntfy.example.com.")
    if (isinstance(raw, str) and host and not parsed.query and not parsed.fragment and not re.search(r"\s", said)
            and (parsed.scheme == "https" or parsed.scheme == "http" and host in _LOCAL_HOSTS)):
        return said, ""
    return "", ("settings.phone_alerts_server should be your ntfy server's https address, such as "
                "https://ntfy.example.com (http only on this computer: http://localhost); using ntfy.sh.")


# How the Job Desk's page opens (settings.desk_window, window.py): in a window of its own, where the profile doesn't
# say, or in a tab of the person's usual browser
DESK_WINDOWS = ("app", "tab")
DESK_WINDOW_DEFAULT = "app"


def _desk_window(raw: Any) -> tuple[str, str]:
    """settings.desk_window as written ("tab", "Tab", nothing) -> "app" or "tab", and what to say of anything else."""
    if raw is None or isinstance(raw, str) and not raw.strip():
        return DESK_WINDOW_DEFAULT, ""
    said = raw.strip().lower() if isinstance(raw, str) else ""
    if said in DESK_WINDOWS:
        return said, ""
    return DESK_WINDOW_DEFAULT, f"Unknown settings.desk_window {raw!r}; using {DESK_WINDOW_DEFAULT}. Use app or tab."


# The keys the desk reads under settings: (the template's); any other is said, as it has no effect
# ("accept_notice: false")
KNOWN_SETTINGS = ("submit_mode", "auto_submit_ats", "browser_channel", "headless", "email_codes", "email_tracking",
                  "accept_cookies", "manage_accounts", "accept_notices", "jobs_at_once", "follow_up_days",
                  "cover_letter_model", "phone_alert_details", "phone_summary_at", "phone_alerts_server",
                  "learn_from_responses", "desk_window")


@dataclass
class Settings:
    submit_mode: str = "review"  # "review" | "auto" | "dry_run" (fill everything, never submit)
    auto_submit_ats: list[str] = field(default_factory=list)
    headless: bool = False
    browser_channel: str = "chrome"  # "chrome", "msedge" or "chromium" (bundled)
    # may Claude read sign-in/verification codes from the user's email (in the chat, and for the desk while its chat is
    # open: pipeline_mail.MailAsk)
    email_codes: bool = False
    email_tracking: bool = False  # may Claude scan email for replies to applications
    accept_cookies: bool = False  # may the desk accept a cookie banner that offers no way to decline
    # may the desk make the person's accounts on job sites, and reset a saved password a site refuses
    # (on unless the profile turns it off: the owner's choice; setup and the desk page tell each person)
    manage_accounts: bool = MANAGE_ACCOUNTS_DEFAULT
    manage_accounts_chosen: bool = False  # the profile says, either way (until then the desk page says it's on)
    # may the desk agree to an employer's notice about AI screening of the application, and pick an
    # application's attestation that its information is true (on unless the profile turns it off:
    # the owner's choice; setup tells each person)
    accept_notices: bool = ACCEPT_NOTICES_DEFAULT
    # how many jobs the Job Desk fills at once, each in its own tab (1 to JOBS_AT_ONCE_MAX); the desk's own
    # "Jobs at once" control changes it there (kept in desk.json)
    jobs_at_once: int = JOBS_AT_ONCE_DEFAULT
    # the days an application sent in waits with no reply before My applications marks it for a follow-up (1 to
    # FOLLOW_UP_DAYS_MAX)
    follow_up_days: int = FOLLOW_UP_DAYS_DEFAULT
    # the model Claude Code writes the desk's cover letters with (the desk's "Write a cover letter for each job"; "":
    # letters.DEFAULT_MODEL)
    cover_letter_model: str = ""
    # Phone alerts (phone.py; on once the desk's Phone alerts card made a topic, kept in secrets.yaml): whether they
    # name the employer and the job (off: only counts and kinds go through ntfy's servers), the daily summary's time
    # ("HH:MM"; "" for none) and the ntfy server ("" for ntfy.sh). Never in practice mode, which sends nothing
    phone_alert_details: bool = False
    phone_summary_at: str = PHONE_SUMMARY_DEFAULT
    phone_alerts_server: str = ""
    # may the openings' scores lean on what worked before: the person's own applications' outcomes, kept on this
    # computer (recommend.worked_before; on unless the profile turns it off)
    learn_from_responses: bool = LEARN_FROM_RESPONSES_DEFAULT
    # how the Job Desk's page opens: "app", in a window of its own (the person's own browser, with no tabs and no
    # address bar: window.py), or "tab", in a tab of their usual browser
    desk_window: str = DESK_WINDOW_DEFAULT
    # the live check's test identity (live_test_identity): accounts and notices in practice mode too
    test_identity: bool = False
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, email: Any = None) -> "Settings":
        """`email`: the profile's, which says whether it's the live check's test identity."""
        malformed = d is not None and not isinstance(d, dict)  # "settings: review": nothing in it can be read
        raw, d = d, d if isinstance(d, dict) else {}
        mode, warnings = _submit_mode(d.get("submit_mode"))
        if malformed:  # (said: every setting is at its default, and the desk acts in no one's name until it's fixed)
            warnings.append("settings: should be a section with each setting on its own line under it, as the "
                            f"template has it, not {raw!r}: nothing in it can be read, so the desk makes no accounts "
                            "and agrees to no notices until it's fixed.")
        ats = d.get("auto_submit_ats") or []
        if isinstance(ats, str):  # "workday" or "workday, greenhouse", not a list
            ats = re.split(r"[,\s]+", ats)
        channel, warning = _channel(d.get("browser_channel"))
        at_once, at_once_said = _jobs_at_once(d.get("jobs_at_once"))
        follow_up, follow_up_said = _whole_number(d.get("follow_up_days"), "follow_up_days", FOLLOW_UP_DAYS_DEFAULT,
                                                  FOLLOW_UP_DAYS_MAX)
        letter_model, letter_model_said = _cover_letter_model(d.get("cover_letter_model"))
        summary_at, summary_said = _phone_summary_at(d.get("phone_summary_at"))
        phone_server, phone_server_said = _phone_alerts_server(d.get("phone_alerts_server"))
        desk_window, desk_window_said = _desk_window(d.get("desk_window"))
        warnings += [said for said in (at_once_said, follow_up_said, letter_model_said, summary_said, phone_server_said,
                                       desk_window_said)
                     if said]
        for key in d:  # a key the desk doesn't read, a letter short of one it does (the review of Oct 15): said
            if str(key) not in KNOWN_SETTINGS:
                near = difflib.get_close_matches(str(key), KNOWN_SETTINGS, n=1, cutoff=0.6)
                warnings.append(f"Unknown settings key {key!r} (it has no effect)"
                                + (f"; did you mean {near[0]!r}?" if near else "."))
        s = cls(
            submit_mode=mode,
            warnings=warnings + ([warning] if warning else []),
            auto_submit_ats=[str(a).strip().lower() for a in ats if str(a).strip()] if isinstance(ats, list) else [],
            headless=_flag(d.get("headless")),
            browser_channel=channel,
            # (the opt-ins that read the person's mail or accept for them: on only for a real true, as written; a
            # quoted "yes" leaves them off, test_accepting_cookies_is_the_persons_choice)
            email_codes=d.get("email_codes") is True,
            email_tracking=d.get("email_tracking") is True,
            accept_cookies=d.get("accept_cookies") is True,
            manage_accounts=_on_unless_off(d.get("manage_accounts"), malformed, MANAGE_ACCOUNTS_DEFAULT),
            manage_accounts_chosen=d.get("manage_accounts") is not None,
            accept_notices=_on_unless_off(d.get("accept_notices"), malformed, ACCEPT_NOTICES_DEFAULT),
            jobs_at_once=at_once,
            follow_up_days=follow_up,
            cover_letter_model=letter_model,
            # (what goes through ntfy's servers: the employer and job only for a real true, as written)
            phone_alert_details=d.get("phone_alert_details") is True,
            phone_summary_at=summary_at,
            phone_alerts_server=phone_server,
            learn_from_responses=_on_unless_off(d.get("learn_from_responses"), malformed,
                                                LEARN_FROM_RESPONSES_DEFAULT),
            desk_window=desk_window,
        )
        if os.environ.get("JOB_APPLY_HEADLESS") == "1":
            s.headless = True
        if never_submit():
            s.submit_mode = "dry_run"
        s.test_identity = live_test_identity(email)
        return s

    @property
    def dry_run(self) -> bool:
        return self.submit_mode == "dry_run"

    @property
    def may_manage_accounts(self) -> bool:
        """Making an account sends the person's details: never in practice mode, which sends nothing,
        except as the live check's test identity (a fake applicant, whose Submit stays refused)."""
        return self.manage_accounts and (not self.dry_run or self.test_identity)

    @property
    def may_accept_notices(self) -> bool:
        """Agreeing for the person is never part of practice mode, as making an account isn't
        (the live check's test identity aside)."""
        return self.accept_notices and (not self.dry_run or self.test_identity)

    def may_auto_submit(self, ats: str) -> bool:
        if ats in HUMAN_SUBMIT_ONLY or self.dry_run:
            return False
        return self.submit_mode == "auto" and ats in self.auto_submit_ats


def _zip_as_written(data: dict[str, Any], path: Path) -> None:
    """A ZIP code written without quotes is read by YAML as a number: 85225 loses nothing,
    but 02134 becomes 1116 (a leading 0 makes it octal). Keep it as the person wrote it."""
    address = (data.get("personal") or {}).get("address") if isinstance(data.get("personal"), dict) else None
    if not isinstance(address, dict) or not isinstance(address.get("postal_code"), int):
        return
    m = re.search(r"^\s*postal_code:\s*([0-9][0-9-]*)\s*(?:#.*)?$", path.read_text(encoding="utf-8"), re.M)
    address["postal_code"] = m.group(1) if m else str(address["postal_code"])


# Profile.load()'s last reading of the person's own profile: the bytes of the files it was read from, and what they
# made. The desk asks for the profile many times a step (each setting it checks, each plan); a YAML reading is 10 ms
# and more, a file's bytes a few microseconds (the review of Oct 15). By content, not by time: an edit is never missed
_LOADED: tuple[Any, dict[str, Any]] | None = None  # (read and replaced whole: report.build loads from a thread)


def _bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


class Profile:
    """Thin wrapper over the profile YAML with dotted-path lookup."""

    def __init__(self, data: dict[str, Any] | None, path: Path | None = None):
        self.data = data or {}
        self.path = path

    @classmethod
    def load(cls, path: Path | None = None) -> "Profile":
        global _LOADED
        own = path is None
        path = path or profile_path()
        key = ((str(path), _bytes(path), str(answers_path()), _bytes(answers_path()), _bytes(QUESTION_BANK))
               if own else None)
        loaded = _LOADED
        if key is not None and loaded is not None and loaded[0] == key:
            return cls(copy.deepcopy(loaded[1]), path)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                mark = getattr(e, "problem_mark", None)
                where = f" near line {mark.line + 1}" if mark is not None else ""
                tip = ((" A Windows path in double quotes needs its backslashes doubled, or single quotes: "
                        "'C:\\Users\\you\\resume.pdf'.")
                       if "\\" in path.read_text(encoding="utf-8", errors="replace") else "")
                raise ValueError(f"{path} has a typo{where}, so it can't be read.{tip}") from None
            if not isinstance(data, dict):
                raise ValueError(f"{path} should hold sections like 'personal:' and "
                                 "'settings:'; it can't be read as it is")
            _zip_as_written(data, path)
            _current_answer_patterns(data)
        if own:
            saved = saved_answers()
            if saved:  # exact questions answered in the Job Desk come before the general patterns
                _current_bank_patterns(saved)
                data["answers"] = saved + list(data.get("answers") or [])
            _LOADED = (key, copy.deepcopy(data))  # (a copy each way: no caller's change reaches the next)
        return cls(data, path)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return default if cur is None or cur == "" else cur

    @property
    def settings(self) -> Settings:
        return Settings.from_dict(self.data.get("settings"), email=self.get("personal.email"))

    @property
    def avoid_titles(self) -> list[str]:
        """preferences.avoid_titles: words or titles the person leaves out ("Senior", "Call Center"), each once as
        written; an opening whose title has one as whole words is never recommended (recommend.avoided_title)."""
        return avoid_list(listed(self.get("preferences.avoid_titles")))

    @property
    def full_name(self) -> str:
        parts = [self.get("personal.first_name"), self.get("personal.last_name")]
        return " ".join(str(p) for p in parts if p)

    def missing_required(self) -> list[str]:
        required = [
            "personal.first_name",
            "personal.last_name",
            "personal.email",
            "personal.phone",
            "personal.address.city",
            "personal.address.state",
            "personal.address.country",
            "documents.resume",
            "work_authorization.authorized_to_work",
            "work_authorization.requires_sponsorship",
        ]
        missing = [k for k in required if self.get(k) is None]
        resume = expand(self.get("documents.resume"))
        if resume and not resume.exists():
            missing.append(f"documents.resume (file not found: {resume})")
        return missing

    def profile_gaps(self) -> list[str]:
        """What the profile could hold for fewer stops, short of what's required: the jobs
        without a start or end month, which Workday asks for every job (someone who doesn't
        remember an old job's months can still apply where they aren't asked); the schools
        without a degree written ("Some college (no degree)" where none was finished: Workday's
        Degree box asks); the background questions not answered yet; and the transcripts, reference
        list or certificates it names whose file isn't there."""
        from .autofill import undated_jobs  # (autofill reads profiles: imported when asked)

        gaps = []
        undated = undated_jobs(self)
        if undated:
            gaps.append("work_history dates: " + "; ".join(undated))
        entries = self.get("education_history")
        schools = [str(e.get("school") or "a school").strip() for e in entries if isinstance(e, dict)
                   and not str(e.get("degree") or "").strip()] if isinstance(entries, list) else []
        if schools:
            gaps.append("education_history degree: " + "; ".join(schools))
        unsaid = [k for k in BACKGROUND_KEYS if self.get(f"background.{k}") is None]
        if unsaid:
            gaps.append("background: " + ", ".join(unsaid))
        unsaid = [k for k in SERVICE_KEYS if self.get(f"history.{k}") is None]
        if unsaid:
            gaps.append("history: " + ", ".join(unsaid))
        # (and a transcript, reference list or certificate it names that isn't there: never uploaded, so said. One it
        # doesn't name isn't: most forms never ask, and the desk says the slot where one does)
        gaps += [f"documents.{slot} (file not found: {path})" for slot, path in self.missing_documents()]
        return gaps

    def document_paths(self, slot: str) -> list[Path]:
        """documents.<slot> as written (one path, or a list of them), each with ~ and $VARS expanded, each once; never
        anything that isn't a path (a number, an entry with keys of its own)."""
        out: list[Path] = []
        written = self.get(f"documents.{slot}")
        for raw in [] if isinstance(written, dict) else listed(written):
            path = expand(raw.strip()) if isinstance(raw, str) else None
            if path is not None and path not in out:
                out.append(path)
        return out

    def document_files(self, slot: str) -> list[Path]:
        """documents.<slot>'s files that are there, in the order written (most recent first, as the template asks): the
        ones a form's box for that document gets. Never one named that isn't there (missing_documents says those)."""
        return [p for p in self.document_paths(slot) if p.is_file()]

    def missing_documents(self) -> list[tuple[str, Path]]:
        """The files documents.transcripts, .references and .certificates name that aren't there, each with its slot."""
        return [(slot, p) for slot in EXTRA_DOCUMENTS for p in self.document_paths(slot) if not p.is_file()]

    def references(self) -> list[dict[str, str]]:
        """The profile's references: list, the person's own entries as they wrote them: each its name and whatever
        else it gives of REFERENCE_KEYS (relationship, company, phone, email), as text on one line. An entry with no
        name is no reference, and nothing is ever added to one."""
        raw = self.get("references")
        out = []
        for entry in raw if isinstance(raw, list) else []:
            if not isinstance(entry, dict):
                continue
            said = {k: " ".join(str(entry[k]).split()) for k in REFERENCE_KEYS
                    if entry.get(k) not in (None, "") and not isinstance(entry.get(k), (dict, list, bool))}
            if said.get("name"):
                out.append({k: v for k, v in said.items() if v})
        return out


# The documents beyond a resume and a cover letter that the profile may name under documents: (each optional: one file,
# or a list of files), each as the desk says it. A form's file box asking for one gets the person's own (autofill's
# documents_asked), never another document, and never one of these in a box for another
EXTRA_DOCUMENTS = {"transcripts": "transcripts", "references": "reference list", "certificates": "certificates"}
# What an entry of the profile's references: list gives (all of it the person's own): the desk's references page says it
REFERENCE_KEYS = ("name", "relationship", "company", "phone", "email")


# The yes/no questions many employers ask, answered once in the profile's background: section
BACKGROUND_KEYS = ("relatives_at_employer", "government_employee", "military", "defense_department",
                   "restrictive_agreement", "intellectual_property", "outside_work", "board_member")
# ...and the U.S. service the profile's history: section says, both ways (ever worked for the U.S. federal government,
# ever served in the U.S. military): NNSS's "Have you ever worked for the U.S. Government ... or served in the U.S.
# Military in any capacity?" (the owner's call, Oct 21)
SERVICE_KEYS = ("us_government_employee", "us_military")


def get_secret(name: str) -> str | None:
    """Look up a secret by name without ever returning it to the model.

    Order: env var JOB_APPLY_SECRET_<NAME>, then ~/.job-apply/secrets.yaml, then the computer's
    password vault (vault.py). The file only has a line the person wrote there by hand (the desk
    saves to the vault and takes the line out), or one saved while the vault wouldn't take it:
    either way the newer, which the desk's next start moves into the vault.
    """
    from . import vault

    env_key = "JOB_APPLY_SECRET_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
    if os.environ.get(env_key):
        return os.environ[env_key]
    path = secrets_path()
    if path.exists():
        value = read_secrets(path).get(name)
        if value:
            return value
    return vault.get(name) or None


def read_secrets(path: Path) -> dict[str, str]:
    """secrets.yaml, every value as written: a password is text, never a number or a date
    ("0123456", "12:30:45", "yes"). A hand-written line YAML can't read ("!Summer2024x",
    "*pw") is read as written too, and no error repeats a password."""
    out: dict[str, str] = {}
    # utf-8-sig: Notepad's older files start with a byte-order mark; lines end only at "\n", as
    # YAML's do (a password can hold U+2028, which str.splitlines would break at)
    for line in path.read_text(encoding="utf-8-sig").split("\n"):
        m = re.match(r"([A-Za-z0-9_]+)[ \t]*:[ \t]*(.*?)[ \t\r]*$", line)  # name: value, one to a line
        if not m:
            continue
        value = m.group(2)
        quoted = re.match(r"""(["'])(.*)\1[ \t]*(?:#.*)?$""", value)  # "abc"  # a note after it
        if quoted:
            try:  # quoted as YAML quotes it ('it''s', "q\"x", "a\Lb")
                value = str(yaml.load(quoted.group(1) + quoted.group(2) + quoted.group(1), Loader=yaml.BaseLoader))
            except yaml.YAMLError:
                value = quoted.group(2)
        else:
            value = re.sub(r"[ \t]+#.*$", "", value)  # a comment after it, as YAML reads one
        if value:
            out[m.group(1)] = value
    return out


SITE_PASSWORD = re.compile(r"^[a-z][a-z0-9]{1,30}_password$")  # e.g. workday_password
# The systems whose password the desk page lets the person save, by the name it shows (pipeline.DESK_PASSWORDS)
DESK_PASSWORDS = {"workday_password": "Workday", "successfactors_password": "SuccessFactors", "icims_password": "iCIMS",
                  "applicantstack_password": "ApplicantStack", "ukg_password": "UKG Pro", "infor_password": "Infor",
                  "taleo_password": "Taleo", "neogov_password": "NEOGOV"}
# The Anthropic API key the desk writes cover letters with (letters.api_key), saved as a site password is
API_KEY_NAME = "anthropic_api_key"
# The ntfy topic phone alerts go to (phone.py): anyone who knows it can read them, so it's kept as a password is, and
# never in the desk's state, a log or a problem report. ntfy's own rule for a topic's name, at least 16 long here
NTFY_TOPIC_NAME = "ntfy_topic"
NTFY_TOPIC = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# What the desk page's Forget takes out (forget_password): each job system's password, the email app password and the
# Anthropic API key. Never the phone alerts' topic, which its own card turns off (forget_secret), nor any other name
FORGETTABLE = (*DESK_PASSWORDS, "email_password", API_KEY_NAME)


# Held across a save, a forget and the start's move of secrets.yaml into the vault
_SECRETS_LOCK = threading.RLock()

# The longest site password the desk keeps (a real one is far shorter; the file is read on every poll)
PASSWORD_MAX = 256


def save_site_password(name: str, value: str) -> None:
    """Store a career-site password the person typed into the Job Desk: in the computer's password vault
    (vault.py), and then its line in secrets.yaml, if it had one, goes. Where there's no vault (or it won't take it),
    in secrets.yaml, which get_secret reads before the vault: only its own entry changes, and the rest of the file,
    comments included, stays as they wrote it (readable by the person alone). All or nothing either way: a save that
    fails leaves what was saved before."""
    if not SITE_PASSWORD.match(name) and name not in (API_KEY_NAME, NTFY_TOPIC_NAME):
        raise ValueError(f"not a site password name: {name!r}")
    if not value or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError("the password is empty or has a line break")
    if name == NTFY_TOPIC_NAME and not NTFY_TOPIC.match(value):
        raise ValueError("A phone alerts topic is 16 to 64 letters, digits, - and _")
    if name == API_KEY_NAME:
        value = value.strip()
        if not value.startswith("sk-ant-"):
            raise ValueError("That isn't an Anthropic API key: those start with sk-ant- (make one in the Claude "
                             "Console, console.anthropic.com, under API keys)")
    if len(value) > PASSWORD_MAX:
        raise ValueError(f"the password is longer than {PASSWORD_MAX} characters")
    ensure_home()
    with _SECRETS_LOCK:  # (the start's move never puts the file's older one back over this: move_secrets_to_vault)
        _save_secret(name, value)


def _save_secret(name: str, value: str) -> None:
    from . import vault

    path = secrets_path()
    before = vault.get(name, fresh=True)
    if vault.put(name, value):
        try:
            _drop_secrets(path, [name])
        except BaseException:
            # (the file's older line still comes first, and would come back at the next start's move, over this one:
            # the vault goes back to as it was, and the save fails as a whole)
            with contextlib.suppress(Exception):
                if before is None:
                    vault.forget(name)
                else:
                    vault.put(name, before)
            raise
        return
    # (the vault wouldn't take it, or there's none: in the file, which comes before whatever the vault still holds;
    # the vault's is left as it was, so a file that can't be written either leaves the older one, never none)
    # always in double quotes, which YAML escapes everything awkward in (U+2028, a leading
    # no-break space, quotes, backslashes), and which read_secrets reads back exactly
    entry = f"{name}: " + yaml.safe_dump(value, default_style='"', allow_unicode=True, width=10**6).strip()
    # All or nothing, through a temporary file the person alone can read (mkstemp makes it 0600, and binary on Windows,
    # so the text-mode write below puts Windows' own line ends once): the file was truncated before it was written,
    # so a failure in between (a full disk, an exit, an antivirus hold) emptied every saved password, the review of
    # Oct 18
    write_text_atomic(path, "\n".join([*_without_secrets(_secret_lines(path), [name]), entry]) + "\n")
    path.chmod(0o600)


def make_site_password(name: str, replace: bool = False) -> bool:
    """A password of the desk's own making (passwords.make) saved for the job system `name` (one of DESK_PASSWORDS, by
    its exact name: never the email app password, the API key, the phone alerts' topic or any other), as a typed one
    is saved (save_site_password: the vault, the file and their lock). Without `replace`, only where none is saved,
    wherever get_secret reads one (the environment's too), as the vault has it now and not as it was last read: looked
    for and saved under the one lock, so a password saved meanwhile is never written over. Whether one was made. It is
    never returned or logged: whatever needs it afterwards asks get_secret, as for a typed one."""
    from . import passwords, vault

    if not isinstance(name, str) or name not in DESK_PASSWORDS:
        raise ValueError(f"not a job system's password: {name!r}")
    with _SECRETS_LOCK:
        # (read again: another program's save since the vault was last read, or a vault locked since, found out now)
        vault.get(name, fresh=True)
        # A vault out of use can't say what it holds: "none saved" would be a guess, and the made one, kept in the
        # file, would come before the person's own in it and, at the next start, be moved over it. Never then, with
        # `replace` or without
        if (why := vault_unreachable()) is not None:
            raise VaultUnreachable(why)
        if not replace and get_secret(name):
            return False
        save_site_password(name, passwords.make())
    return True


class VaultUnreachable(Exception):
    """The computer's password vault is out of use (it failed or didn't answer, locked): what's saved in it can't be
    read, so the desk neither makes, nor shows as none, nor forgets a password until it answers again. Its words are
    vault_unreachable's, for the page."""


def vault_unreachable() -> str | None:
    """Why a password can't be made, shown or forgotten just now, in one plain sentence for the page: the computer's
    password vault is there but out of use (vault.status().down: locked, or not answering). None while it answers, and
    on a computer with no vault at all (the file is the store there)."""
    from . import vault

    s = vault.status()
    if not s.down:
        return None
    named = f" ({s.name})" if s.name and s.name != "The password vault" else ""
    return (f"Your password vault{named} isn't answering, so the desk can't read the passwords saved in it: unlock "
            "it, then open the Job Desk again.")


def forget_secret(name: str) -> bool:
    """The secret `name` forgotten (the phone alerts topic, turned off or replaced on the desk): taken out of the
    computer's password vault and its list of names, and its entry out of secrets.yaml, the rest of the file,
    comments included, as written. Whether there was one. A ValueError when the vault won't let it go."""
    if name != NTFY_TOPIC_NAME:
        raise ValueError(f"not a secret the desk forgets: {name!r}")
    return _forget(name)


def forget_password(name: str) -> bool:
    """A saved password forgotten on the desk's page (its Forget): a job system's, the email app password or the
    Anthropic API key (FORGETTABLE, by its exact name), taken out as forget_secret takes the phone alerts' topic out,
    under the same lock and all or nothing. Whether there was one. A ValueError for any other name (the
    topic's too: its own card turns it off), or when the vault won't let it go; VaultUnreachable while the vault is out
    of use (it can't take out what it can't reach, and the file's line alone going would leave the vault's to come
    back)."""
    from . import vault

    if not isinstance(name, str) or name not in FORGETTABLE:
        raise ValueError(f"not a password the desk forgets: {name!r}")
    with _SECRETS_LOCK:
        vault.get(name, fresh=True)  # (a vault locked since the last read, found out now)
        if (why := vault_unreachable()) is not None:
            raise VaultUnreachable(why)
        if (path := secrets_path()).exists():
            read_secrets(path)  # (a file that can't be read stops it here, before the vault's is taken out)
        return _forget(name)


def _forget(name: str) -> bool:
    """forget_secret's and forget_password's work, for a name each has checked."""
    from . import vault

    with _SECRETS_LOCK:
        there = vault.get(name, fresh=True) is not None
        try:
            vault.forget(name)
        except vault.Stuck:
            raise ValueError(f"Your password vault ({vault.status().name}) wouldn't let it go just now (is it "
                             "locked?). Unlock it and try again.") from None
        vault.unlist(name)
        path = secrets_path()
        if not path.exists() or name not in read_secrets(path):
            return there
        _drop_secrets(path, [name])
        return True


def set_setting(key: str, value: Any) -> None:
    """profile.yaml's settings.<key> set to `value` from the desk (its Phone alerts card), every other line and comment
    kept as written. A ValueError when there's no profile, or it's laid out so it can't be changed safely."""
    if key not in KNOWN_SETTINGS:
        raise ValueError(f"not a setting: {key!r}")
    path = profile_path()
    with PROFILE_LOCK:  # (read, changed and written as one: two saves at once both kept)
        if not path.exists():
            raise ValueError("Set up your profile first")
        try:
            text = set_yaml_value(path.read_text(encoding="utf-8"), ("settings", key), value)
        except ValueError as e:
            raise ValueError(f"Your profile's settings couldn't be changed here ({e}): set {key} under settings: in "
                             f"{path} yourself") from None
        write_text_atomic(path, text)


# The desk's own changes to profile.yaml (set_setting, the documents' lines) and to answers.yaml: each read, changed and
# written whole under its file's lock, so two at once (two of the page's switches, two threads) never lose one
PROFILE_LOCK = threading.RLock()
ANSWERS_LOCK = threading.RLock()


def _secret_lines(path: Path) -> list[str]:
    old = path.read_text(encoding="utf-8-sig").split("\n") if path.exists() else []
    return [line.rstrip("\r") for line in old if line.strip() or line != ""]


def _without_secrets(lines: list[str], names: list[str]) -> list[str]:
    """secrets.yaml's lines without these entries (each one's line, and any lines continuing it)."""
    kept, skipping = [], False
    for line in lines:
        if any(re.match(rf"{re.escape(name)}\s*:", line) for name in names):
            skipping = True  # drop the old entry and any lines continuing it
            continue
        if skipping and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        skipping = False
        kept.append(line)
    return kept


def _drop_secrets(path: Path, names: list[str]) -> None:
    """Take these entries out of secrets.yaml, the rest of it (comments too) as written. Nothing to do without them."""
    if not path.exists():
        return
    lines = _secret_lines(path)
    kept = _without_secrets(lines, names)
    if kept == lines:
        return
    write_text_atomic(path, "".join(line + "\n" for line in kept))
    path.chmod(0o600)


def move_secrets_to_vault() -> dict[str, int]:
    """At the desk's start: each entry of secrets.yaml into the computer's password vault, where there's one. One at a
    time and all or nothing: written to the vault, read back and compared, and only then taken out of the file, which
    keeps its comments and every entry that didn't move (one the vault refused or gave back otherwise, a value
    continued on lines below it). A line in the file is the newer (the desk saves to the vault and takes the file's
    line out; a line there now was written since, by hand), so it replaces the vault's. An entry that changed or went
    while it moved (a save from the page) is left as it is now. Logs only names and counts, never a value. Returns
    {"moved": n, "kept": m}."""
    import logging

    from . import vault

    log = logging.getLogger(__name__)
    if (moved_from := vault.moved_warning()) is not None:
        log.warning("%s", moved_from)
    with _SECRETS_LOCK:
        return _move_secrets(log)


def _move_secrets(log: Any) -> dict[str, int]:
    from . import vault

    path = secrets_path()
    out = {"moved": 0, "kept": 0}
    if not path.exists() or not vault.status().usable:
        return out
    try:
        lines = _secret_lines(path)
        entries = read_secrets(path)
    except (OSError, ValueError):
        log.warning("secrets.yaml couldn't be read, so nothing in it was moved into the password vault")
        return out
    # (a name whose value goes on below it: read_secrets has its first line alone, which mustn't stand for it)
    continued = {m.group(1) for line, below in zip(lines, lines[1:])
                 if (m := re.match(r"([A-Za-z0-9_]+)[ \t]*:", line)) and below.startswith((" ", "\t"))}
    moved: list[str] = []
    kept: list[str] = []
    for name, value in entries.items():
        try:
            if read_secrets(path).get(name) != value:  # (changed or gone since it was read: left as it is now)
                continue
            ok = name not in continued and vault.put(name, value)
        except (OSError, ValueError):  # (the file unreadable now; a name no secret is kept under in the vault)
            ok = False
        (moved if ok else kept).append(name)
    with contextlib.suppress(OSError, ValueError):  # (only the lines still as they were moved)
        now = read_secrets(path)
        moved = [name for name in moved if now.get(name) == entries[name]]
    if moved:
        try:
            _drop_secrets(path, moved)
        except OSError:
            # (each is in the vault as the file has it, so the next start's move takes them out of the file)
            log.warning("%d saved password(s) went into %s, but secrets.yaml couldn't be rewritten just now",
                        len(moved), vault.status().name)
            return {"moved": 0, "kept": len(entries)}
    if kept:
        log.warning("Moved %d saved password(s) from secrets.yaml into %s; %d stayed in the file (%s)", len(moved),
                    vault.status().name, len(kept), ", ".join(sorted(kept)))
    elif moved:
        log.info("Moved %d saved password(s) from secrets.yaml into %s", len(moved), vault.status().name)
    return {"moved": len(moved), "kept": len(kept)}


def _answers_file() -> dict[str, Any]:
    """answers.yaml as written. Raises ValueError, in plain words, if it doesn't parse."""
    path = answers_path()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" near line {mark.line + 1}" if mark is not None else ""
        raise ValueError(f"{path} has a typo{where}, so it's set aside until it's fixed") from e
    if data is not None and not isinstance(data, dict):
        raise ValueError(f"{path} should hold a list under 'answers:'; it's set aside until it's fixed")
    return data or {}


def answers_problem() -> str | None:
    """Why answers.yaml is being ignored, if it is."""
    try:
        _answers_file()
    except (ValueError, OSError) as e:
        return str(e)
    return None


def saved_answers() -> list[dict[str, Any]]:
    """Answers given in the Job Desk. A hand edit that broke the file mustn't stop the
    profile loading (or the desk with it): the file is skipped until it's fixed."""
    try:
        data = _answers_file()
    except (ValueError, OSError):
        return []
    entries = data.get("answers")
    return [a for a in entries if isinstance(a, dict) and a.get("match")] if isinstance(entries, list) else []


def question_pattern(label: str) -> str:
    """A pattern that matches this question's label again, however its spacing and
    required-markers come out ("Are you willing to relocate?*")."""
    text = re.sub(r"\(required\)|\*", " ", label or "", flags=re.I).strip(" :?")
    # (a marker between its words too: "Email * (work)" read again with its star never matched the pattern made
    # without it, so its answer was never used again: a property test of the review of Oct 15's found it)
    return r"(?:\s|\*|\((?i:required)\))+".join(re.escape(w) for w in text.split())


def label_sentences(label: str) -> list[str]:
    """A label's sentences, required-markers and extra spaces gone."""
    text = re.sub(r"\(required\)|\*", " ", label or "", flags=re.I)
    return [s for part in re.split(r"(?<=[.?!])\s+", text) if (s := " ".join(part.split()))]


# A label this short is its own question; a longer one (a notice with a question in it) is told by the one
# sentence that asks, which is at most so long and not so short that it says nothing without the rest
OWN_QUESTION_WORDS = 15
CORE_WORDS = (5, 25)


def question_core(label: str) -> str:
    """The question a long label asks, to remember an answer by: the one sentence ending in "?" when the label
    is longer than a question (Workday's "Use of Your Profile: …" paragraph, worded for each employer, asks
    one thing). Else the whole label, as it was, with its markers gone (issue #112, note 29)."""
    sentences = label_sentences(label)
    whole = " ".join(sentences)
    if len(whole.split()) <= OWN_QUESTION_WORDS:
        return whole
    asks = [s for s in sentences if s.endswith("?")]
    if len(asks) == 1 and CORE_WORDS[0] <= len(asks[0].split()) <= CORE_WORDS[1]:
        return asks[0]
    return whole


_KEY_LINE = r"^(?P<indent>{indent}){key}:(?P<rest>.*)$"


def _value_and_comment(rest: str) -> tuple[str, str]:
    """A key line's value and its trailing comment ("  # e.g. ..."), the comment kept as written: the first " #"
    outside quotes and brackets. A quote opens a quoted value only where one can start (the value's first character,
    or after "[", "{" or ","; after ":" in a flow mapping): an apostrophe inside a plain value (Associate's Degree,
    12 O'Brien St) is a letter. A flow list left open (written over several lines) is a ValueError."""
    quote, depth, escaped, last = "", 0, False, ""
    for i, ch in enumerate(rest):
        if quote:  # (in double quotes a backslash escapes what follows, itself too: "Tech \\" ends at its quote)
            if escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote, last = "", ch
            continue
        if ch in "\"'" and (last in ("", "[", "{", ",") or last == ":" and depth):
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t") and not depth:
            head = rest[:i]
            return head.strip(), rest[len(head.rstrip()):].rstrip()
        if not ch.isspace():
            last = ch
    if depth > 0 or quote:
        raise ValueError("a value written over several lines")
    return rest.strip(), ""


def set_yaml_value(text: str, keys: tuple[str, ...], value: Any) -> str:
    """The YAML text with the value at `keys` (a top-level key; a key under a top-level mapping: ("preferences",
    "locations"); or one a level deeper: ("personal", "address", "city")) set, written on its one line in JSON's
    spelling (which YAML reads: a flow list of quoted strings, true, false); every other line, comments too, stays as
    the person wrote it. A section that isn't there is added (at the end of the file, or at the top of the section it
    goes in). A file laid out so that can't be done safely (a flow mapping, a value over several lines, a key spelled
    another way, a file that doesn't read) is a ValueError, and nothing is changed: the person edits it by hand."""
    if not 1 <= len(keys) <= 3:
        raise ValueError("one key, or up to two sections and a key")
    try:
        before = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        raise ValueError(f"it doesn't read as YAML ({str(e).splitlines()[0]})") from e
    if before is None:
        before = {}
    node: Any = before
    for name in keys[:-1]:  # (each section on the way a mapping, or not there yet)
        if not isinstance(node, dict):
            break
        node = node.get(name) or {}
    if not isinstance(before, dict) or not isinstance(node, dict):
        raise ValueError("it isn't laid out as sections of settings")
    bom = "\ufeff" if text.startswith("\ufeff") else ""  # (PowerShell 5 writes one before the first key)
    lines = text[len(bom):].splitlines(keepends=True)
    nl = "\r\n" if "\r\n" in text else "\n"  # (the file's own line ending: Windows' too)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += nl
    written = json.dumps(value, ensure_ascii=False)
    key = keys[-1]
    start, stop, indent = 0, len(lines), ""
    section: dict[str, Any] = before
    for depth, name in enumerate(keys[:-1]):
        head = re.compile(rf"^{re.escape(indent)}{re.escape(name)}:\s*(#.*)?$")
        top = next((i for i in range(start, stop) if head.match(lines[i].rstrip("\r\n"))), None)
        if top is None:
            if name in section:
                raise ValueError(f"its {name} section is written in a way this can't change")
            # (this section, any under it and the value: at the end of the file, or the top of the section it's in)
            added = [f"{indent}{'  ' * i}{k}:{nl}" for i, k in enumerate(keys[depth:-1])]
            added.append(f"{indent}{'  ' * (len(keys) - 1 - depth)}{key}: {written}{nl}")
            where = start if depth else len(lines)
            lines[where:where] = added
            return _checked(text, bom + "".join(lines), keys, value, before)
        section = section.get(name) or {}
        start = top + 1
        # (a section ends at the next line as far out as its own, comments aside)
        stop = next((i for i in range(start, stop) if lines[i].strip() and not lines[i].lstrip().startswith("#")
                     and len(lines[i]) - len(lines[i].lstrip()) <= len(indent)), stop)
        child = next((ln for ln in lines[start:stop] if ln.strip() and not ln.lstrip().startswith("#")), "")
        deeper = child[:len(child) - len(child.lstrip())]
        indent = deeper if len(deeper) > len(indent) else indent + "  "
    pattern = re.compile(_KEY_LINE.format(indent=re.escape(indent), key=re.escape(key)))
    at = next((i for i in range(start, stop) if pattern.match(lines[i].rstrip("\r\n"))), None)
    if at is None:
        if key in section:  # (spelled another way: quoted, a space before its colon; a second one would win)
            raise ValueError(f"its {key} is written in a way this can't change")
        lines.insert(start if len(keys) > 1 else len(lines), f"{indent}{key}: {written}{nl}")
        return _checked(text, bom + "".join(lines), keys, value, before)
    m = pattern.match(lines[at].rstrip("\r\n"))
    assert m is not None
    old, comment = _value_and_comment(m.group("rest"))
    end = at + 1
    if not old:  # a block list (or nothing) under it: its "- " lines, deeper than the key or at its own indent
        while end < stop and (not lines[end].strip() and end + 1 < stop and lines[end + 1].startswith(indent + " ")
                              or lines[end].startswith(indent + " ") or lines[end].startswith(indent + "- ")):
            end += 1
    lines[at:end] = [f"{indent}{key}: {written}{comment}{nl}"]
    return _checked(text, bom + "".join(lines), keys, value, before)


def _checked(text: str, out: str, keys: tuple[str, ...], value: Any, before: dict[str, Any]) -> str:
    """The new text, when it reads as the old one with only that value changed."""
    try:
        after = yaml.safe_load(out)
    except yaml.YAMLError as e:
        raise ValueError("it couldn't be changed safely") from e
    want = copy.deepcopy(before)
    section = want
    for name in keys[:-1]:
        if not isinstance(section.get(name), dict):
            section[name] = {}
        section = section[name]
    section[keys[-1]] = value
    if after != want:
        raise ValueError("it couldn't be changed safely")
    return out


def avoid_list(values: list[Any]) -> list[str]:
    """Titles to leave out as given (a profile's, the window's, a tool's): each split at a comma, semicolon or bar
    ("Senior, Call Center" is two), its spaces folded, each once (any case) as first written, none empty."""
    out: list[str] = []
    for value in values:
        for part in re.split(r"[,;|]", str(value)):
            said = " ".join(part.split())
            if said and said.casefold() not in {o.casefold() for o in out}:
                out.append(said)
    return out


def listed(value: Any) -> list[Any]:
    """A profile's list as a list, one written as a single value too ("titles: Help Desk Technician"), never its
    letters."""
    return [value] if isinstance(value, (str, int, float)) else list(value or [])


def search_profile_text(places: list[str] | None = None, titles: list[str] | None = None,
                        avoid_titles: list[str] | None = None) -> tuple[Path, str]:
    """profile.yaml with these places as preferences.locations (where the person looks for work: "Phoenix, AZ",
    "Las Vegas, NV"; Find jobs searches their states, a form asking where they'd like to work gets the first), these
    titles as preferences.titles (the jobs Find jobs looks for) and these as preferences.avoid_titles (the titles to
    leave out; none, in a profile without the key, isn't written there), worked out but not written: a ValueError when
    it can't be."""
    path = profile_path()
    if not path.exists():
        raise ValueError("Set up your profile first.")
    text = path.read_text(encoding="utf-8")
    if places is not None:
        text = set_yaml_value(text, ("preferences", "locations"), places)
    if titles is not None:
        text = set_yaml_value(text, ("preferences", "titles"), titles)
    if avoid_titles == []:
        try:
            had = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            had = {}
        preferences = had.get("preferences") if isinstance(had, dict) else None
        if not isinstance(preferences, dict) or "avoid_titles" not in preferences:
            avoid_titles = None  # (nothing to clear)
    if avoid_titles is not None:
        text = set_yaml_value(text, ("preferences", "avoid_titles"), avoid_titles)
    return path, text


# how many times a replace refused on Windows is tried, 0.1 s apart
REPLACE_TRIES = 5


def write_text_atomic(path: Path, text: str) -> None:
    """All or nothing (a crash mid-write can't leave half a file), into the file a link points at, through a
    temporary file of its own (two writers never share one)."""
    import tempfile

    target = path.resolve()
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # (on Windows its lines end as Windows writes them)
            f.write(text)
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                # Windows refuses to replace a file another program has open at that moment (another session's
                # desk reading it on its poll): a moment later it's free (the review of 0.3.210)
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(0.1)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


QUESTION_BANK = PLUGIN_ROOT / "data" / "question_bank.yaml"
HANDLED = PLUGIN_ROOT / "data" / "desk_handled.yaml"


def handled_employers() -> set[str]:
    """The employers where the desk goes all the way (data/desk_handled.yaml: in the live check, a test applicant
    reached the review page there), by name in lower case. A file that can't be read names none."""
    try:
        with HANDLED.open(encoding="utf-8") as f:
            names = (yaml.safe_load(f) or {}).get("employers") or []
    except (OSError, yaml.YAMLError, AttributeError):
        return set()
    return {str(n).strip().casefold() for n in names if n}


def _bank_entries() -> list[dict[str, Any]]:
    """data/question_bank.yaml's entries that can be used: an id, a question to ask, a pattern that compiles.
    A bank that can't be read is no bank: the desk goes on without it."""
    try:
        with QUESTION_BANK.open(encoding="utf-8") as f:
            entries = (yaml.safe_load(f) or {}).get("questions") or []
    except (OSError, yaml.YAMLError, AttributeError):
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("id") or not e.get("match") or not e.get("ask"):
            continue
        try:
            re.compile(str(e["match"]))
        except re.error:
            continue
        out.append(e)
    return out


def question_bank() -> list[dict[str, Any]]:
    """The questions most applications ask that a profile doesn't answer (data/question_bank.yaml), each
    with the person's answer from answers.yaml where they gave one ("answer"), and how many of the nightly's
    applications asked it ("seen": the desk's card shows the most asked first)."""
    given = {a.get("bank"): a.get("answer") for a in saved_answers() if a.get("bank")}
    return [{k: e[k] for k in ("id", "ask", "kind", "choices", "match", "example", "seen") if k in e}
            | {"answer": given.get(e["id"])} for e in _bank_entries()]


def _current_bank_patterns(saved: list[dict[str, Any]]) -> None:
    """An answer saved on the desk to one of the bank's questions keeps the bank's pattern of that day in
    answers.yaml (save_bank_answer): in memory it takes the bank's pattern of today, so a pattern corrected
    since reaches everyone who answered before (the review of 0.3.175). The file stays the person's."""
    current = {str(e["id"]): str(e["match"]) for e in _bank_entries()}
    for item in saved:
        if isinstance(item.get("bank"), str) and item["bank"] in current:
            item["match"] = current[item["bank"]]


def save_bank_answer(bank_id: str, answer: Any) -> None:
    """Keep the person's answer to one of the question bank's questions in answers.yaml, with the bank's
    pattern, so it fills every wording of that question at every employer; an empty answer takes it out.
    Everything else in answers.yaml stays."""
    entry = next((e for e in question_bank() if e["id"] == bank_id), None)
    if entry is None:
        raise ValueError(f"No common question {bank_id!r}")
    if isinstance(answer, str):
        answer = answer.strip()
    if entry.get("kind") == "choice" and answer not in (None, "") and answer not in (entry.get("choices") or []):
        raise ValueError(f"{answer!r} isn't one of that question's choices")
    with ANSWERS_LOCK:  # (read, changed and written as one)
        data = _answers_file()
        old = data.get("answers")
        entries = [a for a in (old if isinstance(old, list) else [])
                   if not (isinstance(a, dict) and a.get("bank") == bank_id)]
        if answer not in (None, ""):
            entries.insert(0, {"match": entry["match"], "answer": answer, "bank": bank_id})
        _write_answers(data, entries)


def _write_answers(data: dict[str, Any], entries: list[Any]) -> None:
    ensure_home()
    header = ("# Answers you gave in the Job Desk. Each one fills the same question on later\n"
              "# applications (one with `at:` only at that employer's, as an employee ID there; one\n"
              "# with no answer is a question left empty there, never asked there again);\n"
              "# edit or delete entries freely. profile.yaml's own answers come after these.\n")
    # (all or nothing, through a temporary file of its own: a crash mid-write can't leave half a file, and two writers
    # never share one)
    write_text_atomic(answers_path(), header + yaml.safe_dump({**data, "answers": entries}, sort_keys=False,
                                                              allow_unicode=True))


def save_answer(label: str, answer: Any, source: str = "", at: str = "") -> dict[str, Any]:
    """Remember the answer to a question for every later application that asks it; with `at`, only for those at that
    employer (the person's ID there: autofill.same_employer_as tells the employer), and with no answer (None) that the
    person left it empty there (it isn't asked at that employer again).
    Everything else in answers.yaml stays; a file that doesn't parse is left untouched
    (ValueError) rather than overwritten."""
    from .autofill import same_employer_as  # (autofill reads this module)

    match = question_pattern(label)
    if not match:
        raise ValueError("A question needs a label to be remembered")
    with ANSWERS_LOCK:  # (read, changed and written as one)
        data = _answers_file()
        old = data.get("answers")

        def same_scope(a: dict[str, Any]) -> bool:  # kept for every employer, as this one is, or for the same one
            kept_at = str(a.get("at") or "").strip()
            return not kept_at if not at.strip() else same_employer_as(kept_at, at)
        # (the same question answered again replaces its answer, by its pattern or by its words: a pattern written
        # before question_pattern took markers between words is the same question; one kept for an employer alone,
        # only at that employer: another's ID stays)
        entries = [a for a in (old if isinstance(old, list) else [])
                   if not (isinstance(a, dict) and (a.get("match") == match or a.get("question") == label.strip())
                           and same_scope(a))]
        entry: dict[str, Any] = {"match": match, "answer": answer, "question": label.strip()}
        core = question_core(label)
        if core != " ".join(label_sentences(label)):  # the question in a longer label: asked in other wording around it
            entry["core"] = core
        if source:
            entry["from"] = source
        if at.strip():
            entry["at"] = at.strip()
        entries.insert(0, entry)
        _write_answers(data, entries)
    return entry


def answer_id(entry: dict[str, Any]) -> str:
    """An answers.yaml entry's id on the Your answers page: its pattern and its employer together, which is what
    save_answer treats as one entry."""
    return f"{entry.get('match') or ''}\n{str(entry.get('at') or '').strip()}"


def _remembered(data: dict[str, Any]) -> tuple[list[Any], list[int]]:
    """answers.yaml's entries as written, and where in them the remembered answers are: each with a pattern, and none
    of the common questions' (their own card has them)."""
    old = data.get("answers")
    entries = list(old) if isinstance(old, list) else []
    return entries, [i for i, a in enumerate(entries) if isinstance(a, dict) and a.get("match") and not a.get("bank")]


def remembered_answers() -> list[dict[str, Any]]:
    """The answers the person gave on a job's card with "remember" ticked, newest first (as answers.yaml keeps them),
    for the Your answers page: each with its id, question, answer (None for one left empty), the employer it's kept
    for ("" for every one), and whether it's an ID (autofill.remembered_ids: the page hides it until Show). A file
    that doesn't parse is a ValueError."""
    entries, at = _remembered(_answers_file())
    out = []
    for i in at:
        a = entries[i]
        answer = a.get("answer")
        private = _private(a)
        out.append({"id": answer_id(a), "question": str(a.get("question") or a.get("match")),
                    # (an ID's value never in the list: private_answer gives it, when the person presses Show)
                    "answer": None if private else
                    answer if answer is None or isinstance(answer, (str, list)) else str(answer),
                    "empty": _left_empty(answer), "at": str(a.get("at") or "").strip(), "private": private})
    return out


def _private(entry: dict[str, Any]) -> bool:
    """Whether a remembered answer is an ID (autofill.remembered_ids, as the scrubbers take them out)."""
    from .autofill import remembered_ids  # (autofill reads this module)

    return bool(remembered_ids(Profile({"answers": [entry]})))


def _left_empty(answer: Any) -> bool:
    return answer is None or (isinstance(answer, (str, list)) and not (answer.strip() if isinstance(answer, str)
                                                                      else answer))


def private_answer(entry_id: Any) -> str:
    """The value of the remembered answer with this id, an ID's (the Your answers page's Show): a LookupError for an
    id that names none; a ValueError for one that isn't private (the page has it already) or was left empty. Its
    value is never in what either says."""
    with ANSWERS_LOCK:
        _, entries, i = _named_answer(entry_id)
    entry = entries[i]
    if _left_empty(entry.get("answer")):
        raise ValueError("That answer was left empty.")
    if not _private(entry):
        raise ValueError("That answer isn't hidden: it's on the page already.")
    return str(entry.get("answer"))


def _named_answer(entry_id: Any) -> tuple[dict[str, Any], list[Any], int]:
    """answers.yaml, its entries, and where the remembered answer with this id is in them: a LookupError for an id
    that names none (a common question's among them), a ValueError for a file that doesn't parse."""
    data = _answers_file()
    entries, at = _remembered(data)
    i = next((i for i in at if isinstance(entry_id, str) and answer_id(entries[i]) == entry_id), None)
    if i is None:
        raise LookupError("That answer isn't remembered any more.")
    return data, entries, i


def change_answer(entry_id: Any, answer: Any) -> dict[str, Any]:
    """The remembered answer with this id given a new answer, for the same question and the same employer, where it
    is in the file. Only that entry's answer changes: every other line, comments too, stays as written
    (_edit_answers)."""
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Type an answer, or press Forget.")
    with ANSWERS_LOCK:
        data, entries, i = _named_answer(entry_id)
        entry = {**entries[i], "answer": answer.strip()}
        _edit_answers(data, entries, i, entry)
    return entry


def forget_answer(entry_id: Any) -> None:
    """The remembered answer with this id taken out of answers.yaml (the question is asked again the next time a site
    asks it). Every other line, comments too, stays as written (_edit_answers)."""
    with ANSWERS_LOCK:
        data, entries, i = _named_answer(entry_id)
        _edit_answers(data, entries, i, None)


def _edit_answers(data: dict[str, Any], entries: list[Any], i: int, entry: dict[str, Any] | None) -> None:
    """answers.yaml with its i-th entry's answer changed to `entry`'s (or, with None, that entry gone), edited in the
    file's own text (_answers_text_edit) so the person's comments and layout stay; written only once the new text reads
    as the old one with that change alone. Where it can't be edited so, the file is written anew as before
    (_write_answers: the desk's header, its entries as data)."""
    want = [*entries[:i], *([entry] if entry is not None else []), *entries[i + 1:]]
    path = answers_path()
    try:
        text = path.read_text(encoding="utf-8")
        edited = _answers_text_edit(text, i, None if entry is None else entry["answer"])
        reads = (yaml.safe_load(edited) or {}) if edited is not None else None
        if isinstance(reads, dict) and "answers" in reads and reads["answers"] is None:
            reads["answers"] = []  # ("answers:" with nothing under it, its last entry forgotten: the empty list it is)
        if edited is not None and reads == {**data, "answers": want}:
            write_text_atomic(path, edited)
            return
    except (OSError, ValueError, yaml.YAMLError):
        pass
    _write_answers(data, want)


def _answers_text_edit(text: str, i: int, answer: str | None) -> str | None:
    """`text` (answers.yaml) with the i-th entry of its answers: list cut out (answer None: from the line its "- "
    begins to its last value's line, those alone) or its answer's value replaced (written as a quoted string), found
    by the nodes' marks; None where the file isn't laid out so (a flow list, an entry sharing a line, no answer key)."""
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode):
        return None
    seq = next((v for k, v in root.value if isinstance(k, yaml.ScalarNode) and k.value == "answers"), None)
    if not isinstance(seq, yaml.SequenceNode) or seq.flow_style or not 0 <= i < len(seq.value):
        return None
    item = seq.value[i]
    if not isinstance(item, yaml.MappingNode) or item.flow_style or not item.value:
        return None
    if answer is not None:
        value = next((v for k, v in item.value if isinstance(k, yaml.ScalarNode) and k.value == "answer"), None)
        if not isinstance(value, yaml.ScalarNode):
            return None
        start, end = value.start_mark.index, value.end_mark.index
        span = text[start:end]
        # (a block scalar's | or > takes its line's end with it: kept, so the next key stays on its own line)
        tail = span[len(span.rstrip("\n")):] if value.style in ("|", ">") else ""
        return text[:start] + (" " if not span else "") + json.dumps(answer, ensure_ascii=False) + tail + text[end:]
    lines = text.splitlines(keepends=True)
    first = item.start_mark.line
    if item.start_mark.column == 0 or lines[first][:item.start_mark.column].strip() != "-":
        return None  # (its "- " isn't alone before it on its first line)
    # its own last line: the last before the next entry (or the list's end) that isn't blank or a comment. A value
    # ending in a block list ends, by its node, on the next entry's dash; the comment and blank lines above that dash
    # are the next entry's, and stay
    if i + 1 < len(seq.value):
        bound = seq.value[i + 1].start_mark.line
    else:
        end = seq.end_mark
        bound = min(len(lines), end.line + (1 if end.column > 0 else 0))
    last = bound - 1
    while last >= first and (not lines[last].strip() or lines[last].lstrip().startswith("#")):
        last -= 1
    if last < first:
        return None  # (the next entry begins on this one's line)
    return "".join(lines[:first] + lines[last + 1:])
