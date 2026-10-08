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
# server/src/job_apply -> plugin root (an installed copy can be pointed back via env)
PLUGIN_ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or PACKAGE_DIR.parents[2])
TEMPLATE_PROFILE = PLUGIN_ROOT / "templates" / "profile.example.yaml"

# Platforms whose user agreements prohibit automated use. The plugin fills the
# form, but the person clicks the final button themselves.
HUMAN_SUBMIT_ONLY = {"linkedin", "indeed"}


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
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Settings":
        d = d or {}
        mode, warnings = _submit_mode(d.get("submit_mode"))
        s = cls(
            submit_mode=mode,
            warnings=warnings,
            auto_submit_ats=[str(a).lower() for a in d.get("auto_submit_ats") or []],
            headless=bool(d.get("headless", False)),
            browser_channel=str(d.get("browser_channel", "chrome")).lower(),
            email_codes=d.get("email_codes") is True,
            email_tracking=d.get("email_tracking") is True,
        )
        if os.environ.get("JOB_APPLY_HEADLESS") == "1":
            s.headless = True
        if never_submit():
            s.submit_mode = "dry_run"
        return s

    @property
    def dry_run(self) -> bool:
        return self.submit_mode == "dry_run"

    def may_auto_submit(self, ats: str) -> bool:
        if ats in HUMAN_SUBMIT_ONLY or self.dry_run:
            return False
        return self.submit_mode == "auto" and ats in self.auto_submit_ats


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
            with path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
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


def get_secret(name: str) -> str | None:
    """Look up a secret by name without ever returning it to the model.

    Order: env var JOB_APPLY_SECRET_<NAME>, then ~/.job-apply/secrets.yaml.
    """
    env_key = "JOB_APPLY_SECRET_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
    if os.environ.get(env_key):
        return os.environ[env_key]
    path = secrets_path()
    if path.exists():
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        value = data.get(name)
        if value is not None:
            return str(value)
    return None


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
    old = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    kept, skipping = [], False
    for line in old:
        if re.match(rf"{re.escape(name)}\s*:", line):
            skipping = True  # drop the old entry and any lines continuing it
            continue
        if skipping and (line.startswith((" ", "\t")) or not line.strip()):
            continue
        skipping = False
        kept.append(line)
    entry = yaml.safe_dump({name: value}, default_flow_style=False, allow_unicode=True, width=10**6).strip()
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
