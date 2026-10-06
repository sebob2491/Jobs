"""Paths, the applicant profile, settings and secrets.

Everything personal lives under JOB_APPLY_HOME (default ~/.job-apply), never in
the plugin directory, so updating or reinstalling the plugin can't lose it.
"""

from __future__ import annotations

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


def home() -> Path:
    return Path(os.environ.get("JOB_APPLY_HOME", "~/.job-apply")).expanduser()


def profile_path() -> Path:
    return home() / "profile.yaml"


def secrets_path() -> Path:
    return home() / "secrets.yaml"


def db_path() -> Path:
    return home() / "tracker.db"


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
        path = path or profile_path()
        if not path.exists():
            return cls({}, path)
        with path.open(encoding="utf-8") as f:
            return cls(yaml.safe_load(f) or {}, path)

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
