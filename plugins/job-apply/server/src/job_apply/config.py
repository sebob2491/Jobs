"""Paths, the applicant profile, settings and secrets.

Everything personal lives under JOB_APPLY_HOME (default ~/.job-apply), never in
the plugin directory, so updating or reinstalling the plugin can't lose it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    "(employed|worked) (by|for) (a|any) .*government": r"^(?!.*\bcontract).*(employed|worked) (by|for) (a|any) .*government",
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


def _flag(raw: Any) -> bool:
    """A yes/no setting, also when it's written in quotes ("false")."""
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "on", "1")
    return bool(raw)


def never_submit() -> bool:
    """Hard switch for tests and practice runs: nothing is ever submitted."""
    return os.environ.get("JOB_APPLY_NEVER_SUBMIT") == "1"


@dataclass
class Settings:
    submit_mode: str = "review"  # "review" | "auto" | "dry_run" (fill everything, never submit)
    auto_submit_ats: list[str] = field(default_factory=list)
    headless: bool = False
    browser_channel: str = "chrome"  # "chrome", "msedge" or "chromium" (bundled)
    email_codes: bool = False  # may Claude read sign-in/verification codes from the user's email
    email_tracking: bool = False  # may Claude scan email for replies to applications
    accept_cookies: bool = False  # may the desk accept a cookie banner that offers no way to decline
    # may the desk make the person's accounts on job sites, and reset a saved password a site refuses
    manage_accounts: bool = False
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Settings":
        d = d if isinstance(d, dict) else {}
        mode, warnings = _submit_mode(d.get("submit_mode"))
        ats = d.get("auto_submit_ats") or []
        if isinstance(ats, str):  # "workday" or "workday, greenhouse", not a list
            ats = re.split(r"[,\s]+", ats)
        channel, warning = _channel(d.get("browser_channel"))
        s = cls(
            submit_mode=mode,
            warnings=warnings + ([warning] if warning else []),
            auto_submit_ats=[str(a).strip().lower() for a in ats if str(a).strip()] if isinstance(ats, list) else [],
            headless=_flag(d.get("headless")),
            browser_channel=channel,
            email_codes=d.get("email_codes") is True,
            email_tracking=d.get("email_tracking") is True,
            accept_cookies=d.get("accept_cookies") is True,
            manage_accounts=d.get("manage_accounts") is True,
        )
        if os.environ.get("JOB_APPLY_HEADLESS") == "1":
            s.headless = True
        if never_submit():
            s.submit_mode = "dry_run"
        return s

    @property
    def dry_run(self) -> bool:
        return self.submit_mode == "dry_run"

    @property
    def may_manage_accounts(self) -> bool:
        """Making an account sends the person's details: never in practice mode, which sends nothing."""
        return self.manage_accounts and not self.dry_run

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


class Profile:
    """Thin wrapper over the profile YAML with dotted-path lookup."""

    def __init__(self, data: dict[str, Any] | None, path: Path | None = None):
        self.data = data or {}
        self.path = path

    @classmethod
    def load(cls, path: Path | None = None) -> "Profile":
        own = path is None
        path = path or profile_path()
        data: dict[str, Any] = {}
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                mark = getattr(e, "problem_mark", None)
                where = f" near line {mark.line + 1}" if mark is not None else ""
                tip = (" A Windows path in double quotes needs its backslashes doubled, or single quotes: "
                       "'C:\\Users\\you\\resume.pdf'.") if "\\" in path.read_text(encoding="utf-8", errors="replace") else ""
                raise ValueError(f"{path} has a typo{where}, so it can't be read.{tip}") from None
            if not isinstance(data, dict):
                raise ValueError(f"{path} should hold sections like 'personal:' and 'settings:'; it can't be read as it is")
            _zip_as_written(data, path)
            _current_answer_patterns(data)
        if own:
            saved = saved_answers()
            if saved:  # exact questions answered in the Job Desk come before the general patterns
                data["answers"] = saved + list(data.get("answers") or [])
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
        return Settings.from_dict(self.data.get("settings"))

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
        remember an old job's months can still apply where they aren't asked)."""
        from .autofill import undated_jobs  # (autofill reads profiles: imported when asked)

        undated = undated_jobs(self)
        return ["work_history dates: " + "; ".join(undated)] if undated else []


def get_secret(name: str) -> str | None:
    """Look up a secret by name without ever returning it to the model.

    Order: env var JOB_APPLY_SECRET_<NAME>, then ~/.job-apply/secrets.yaml.
    """
    env_key = "JOB_APPLY_SECRET_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
    if os.environ.get(env_key):
        return os.environ[env_key]
    path = secrets_path()
    if path.exists():
        value = read_secrets(path).get(name)
        if value:
            return value
    return None


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


def save_site_password(name: str, value: str) -> None:
    """Store a career-site password the person typed into the Job Desk. Only its own
    entry in secrets.yaml changes; the rest of the file, comments included, stays as
    they wrote it. The file is readable by the person alone."""
    if not SITE_PASSWORD.match(name):
        raise ValueError(f"not a site password name: {name!r}")
    if not value or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError("the password is empty or has a line break")
    ensure_home()
    path = secrets_path()
    old = path.read_text(encoding="utf-8-sig").split("\n") if path.exists() else []
    old = [line.rstrip("\r") for line in old if line.strip() or line != ""]
    kept, skipping = [], False
    for line in old:
        if re.match(rf"{re.escape(name)}\s*:", line):
            skipping = True  # drop the old entry and any lines continuing it
            continue
        if skipping and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        skipping = False
        kept.append(line)
    # always in double quotes, which YAML escapes everything awkward in (U+2028, a leading
    # no-break space, quotes, backslashes), and which read_secrets reads back exactly
    entry = f"{name}: " + yaml.safe_dump(value, default_style='"', allow_unicode=True, width=10**6).strip()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join([*kept, entry]) + "\n")
    path.chmod(0o600)


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
    return r"\s+".join(re.escape(w) for w in text.split())


def save_answer(label: str, answer: Any, source: str = "") -> dict[str, Any]:
    """Remember the answer to a question for every later application that asks it.
    Everything else in answers.yaml stays; a file that doesn't parse is left untouched
    (ValueError) rather than overwritten."""
    match = question_pattern(label)
    if not match:
        raise ValueError("A question needs a label to be remembered")
    data = _answers_file()
    old = data.get("answers")
    entries = [a for a in (old if isinstance(old, list) else []) if not (isinstance(a, dict) and a.get("match") == match)]
    entry: dict[str, Any] = {"match": match, "answer": answer, "question": label.strip()}
    if source:
        entry["from"] = source
    entries.insert(0, entry)
    ensure_home()
    header = ("# Answers you gave in the Job Desk. Each one fills the same question on later\n"
              "# applications; edit or delete entries freely. profile.yaml's own answers come after these.\n")
    path = answers_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(header + yaml.safe_dump({**data, "answers": entries}, sort_keys=False, allow_unicode=True),
                   encoding="utf-8")
    os.replace(tmp, path)  # all or nothing: a crash mid-write can't leave half a file
    return entry
