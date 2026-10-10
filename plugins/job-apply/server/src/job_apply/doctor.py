"""The doctor: is everything job-apply needs in place on this computer?

`job-apply-doctor` (the one-step installer runs it; by hand it's
`uv run --project <plugin>/server job-apply-doctor`) and the `doctor` tool check each thing
and say, in plain words, what to do about any that isn't: Python and uv, the plugin's version
and whether a newer one is out, a browser, ~/.job-apply, the profile and the resume, and on
Windows where uv keeps its Python. It changes nothing, and starts the browser only when asked
(`--launch`). Nothing from the profile is shown but which answers are missing, and the home
folder (which holds the account's name) is shown as ~ ($HOME in a command to paste), so the
report can be pasted where others see it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO

from . import config
from .browser import _NOT_INSTALLED, launch_attempts
from .updates import UPDATE_COMMANDS, UpdateCheck, newer

OK, PROBLEM, ADVICE, SKIPPED = "ok", "problem", "advice", "skipped"
MARKS = {OK: "✔", PROBLEM: "✘", ADVICE: "!", SKIPPED: "-"}
# For a console that can't show ✔ and ✘ (Windows' older console window)
ASCII_MARKS = {OK: "[ok]", PROBLEM: "[X]", ADVICE: "[!]", SKIPPED: "[-]"}

SAY_SETUP = 'say "set up job-apply" in the Claude app'
SETUP = 'Say "set up job-apply" in the Claude app (or run `claude` in a terminal and say it there).'
INSTALLER = "Run the one-step installer again (the plugin's README has it)."
RESUME_TYPES = (".pdf", ".docx", ".doc", ".rtf", ".odt", ".txt")  # what the installer copies in
NEEDS_PYTHON = (3, 10)

# The profile's required answers (config.Profile.missing_required), in plain words
FIELD_WORDS = {
    "personal.first_name": "your first name",
    "personal.last_name": "your last name",
    "personal.email": "your email",
    "personal.phone": "your phone number",
    "personal.address.city": "your city",
    "personal.address.state": "your state",
    "personal.address.country": "your country",
    "work_authorization.authorized_to_work": "whether you can work in the US",
    "work_authorization.requires_sponsorship": "whether you need visa sponsorship",
}
# What config.Profile.profile_gaps names, without the jobs and schools it lists
GAP_WORDS = {
    "work_history dates": "start and end months for some jobs",
    "education_history degree": "what you finished at some schools",
    "background": "the yes/no background questions employers ask",
}
BROWSER_NAMES = {"chrome": "Google Chrome", "chrome-beta": "Google Chrome Beta", "msedge": "Microsoft Edge"}


def home_folder() -> str:
    """The person's home folder, when it's one worth hiding (not "/" or "")."""
    home = str(Path.home())
    return home if len(Path(home).parts) > 1 else ""


def private(text: str) -> str:
    """The text with the home folder shown as ~ (C:\\Users\\First Last\\.job-apply -> ~\\.job-apply)."""
    home = home_folder()
    if not home or not text:
        return text
    flags = re.IGNORECASE if on_windows() else 0
    return re.sub(re.escape(home) + r"(?![\w-])", "~", text, flags=flags)  # (/home/pat, not /home/patricia)


def in_shell(path: Path) -> str:
    """A path for a command to paste, with the home folder as $HOME (PowerShell, bash and zsh all
    read that inside double quotes; ~ isn't read there)."""
    home = home_folder()
    text = str(path)
    same = (lambda a, b: a.lower() == b.lower()) if on_windows() else (lambda a, b: a == b)
    if home and (same(text, home) or same(text[:len(home) + 1], home + os.sep)):
        return "$HOME" + text[len(home):]
    return text


@dataclass
class Check:
    name: str  # which check, for Claude reading the tool's answer
    status: str  # ok, problem (needed: the doctor fails), advice or skipped (neither fails it)
    say: str  # one line, in plain words
    fix: str = ""  # what to do about it


@dataclass
class Report:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return not any(c.status == PROBLEM for c in self.checks)

    def text(self, marks: dict[str, str] = MARKS) -> str:
        lines = ["job-apply doctor", ""]
        for c in self.checks:
            lines.append(f"{marks[c.status]} {private(c.say)}")
            if c.fix:
                lines.append(f"    {'To fix: ' if c.status == PROBLEM else ''}{private(c.fix)}")
        problems = sum(c.status == PROBLEM for c in self.checks)
        lines.append("")
        lines.append("Everything the plugin needs is in place." if not problems else
                     f"{problems} thing{'s' if problems > 1 else ''} to fix (marked {marks[PROBLEM]}).")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        checks = [{**asdict(c), "say": private(c.say), "fix": private(c.fix)} for c in self.checks]
        return {"ok": self.ok, "checks": checks, "report": self.text()}


# --------------------------------------------------------------------- what's on this computer
# (each its own function, for the tests to stand in for)


def python_version() -> tuple[int, int, int]:
    return sys.version_info[:3]


def uv_version() -> str | None:
    """uv's version, "" when it's there but didn't say, None when it isn't on the PATH."""
    exe = shutil.which("uv")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\d+\.\d+[\w.+-]*", out or "")
    return m.group(0) if m else ""


async def latest_version() -> str | None:
    """The newest published version, as the Job Desk reads it (None: couldn't look)."""
    check = UpdateCheck()
    await check.refresh()
    return check.latest


def on_windows() -> bool:
    return sys.platform == "win32"


def user_env(name: str) -> str | None:
    """A variable as this program sees it, or as saved for the person (on Windows, one set
    after the Claude app started is in the registry only, until the app restarts)."""
    if os.environ.get(name):
        return os.environ[name]
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = winreg.QueryValueEx(key, name)[0]
        except OSError:
            return None
        return str(value) if value else None
    return None


def chrome_installed() -> bool:
    from .server import chrome_installed as installed  # setup_status's own look

    return installed()


def _app_installed(commands: tuple[str, ...], mac_app: str, linux: str, windows: tuple[str, ...]) -> bool:
    if any(shutil.which(c) for c in commands):
        return True
    places = [Path("/Applications") / mac_app, Path.home() / "Applications" / mac_app, Path(linux)]
    for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        if os.environ.get(var):
            places.append(Path(os.environ[var]).joinpath(*windows))
    return any(p.exists() for p in places)


def edge_installed() -> bool:
    return _app_installed(("msedge", "microsoft-edge", "microsoft-edge-stable"), "Microsoft Edge.app",
                          "/opt/microsoft/msedge/msedge", ("Microsoft", "Edge", "Application", "msedge.exe"))


def chrome_beta_installed() -> bool:
    return _app_installed(("google-chrome-beta",), "Google Chrome Beta.app", "/opt/google/chrome-beta/chrome",
                          ("Google", "Chrome Beta", "Application", "chrome.exe"))


def playwright_browsers_dir() -> Path:
    """Where `playwright install chromium` puts its browser."""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env == "0":  # inside the playwright package itself
        import playwright

        return Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
    if env:
        return Path(env).expanduser()
    if on_windows():
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def bundled_chromium_installed() -> bool:
    return any(p.is_dir() for p in playwright_browsers_dir().glob("chromium-*"))


async def launches(attempt: dict[str, Any]) -> str | None:
    """Start the browser out of sight, on a profile of its own, and close it: None when it
    started, else what went wrong."""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, timeout=60000, **attempt)
            await browser.close()
    except Exception as e:  # whatever it was, it's the answer
        return " ".join(str(e).split())[:400] or type(e).__name__
    return None


# --------------------------------------------------------------------- the checks


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def sentence(text: str) -> str:
    return cap(text) + ("" if text.endswith(".") else ".")


def check_python() -> Check:
    version = python_version()
    shown = ".".join(str(p) for p in version)
    if tuple(version[:2]) >= NEEDS_PYTHON:
        return Check("python", OK, f"Python {shown}")
    return Check("python", PROBLEM, f"Python {shown} is too old: the plugin needs 3.10 or newer",
                 f"{INSTALLER} uv then fetches a newer Python for the plugin.")


def check_uv() -> Check:
    version = uv_version()
    if version is None:
        return Check("uv", PROBLEM, "uv isn't installed, or isn't on the PATH. The Claude app starts the plugin with it.",
                     f"{INSTALLER} Or install uv (https://docs.astral.sh/uv/getting-started/installation/), "
                     "then restart the Claude app.")
    return Check("uv", OK, f"uv {version}".strip())


async def check_version() -> list[Check]:
    current = config.plugin_version()
    have = Check("plugin", OK, f"job-apply {current}") if current else \
        Check("plugin", ADVICE, "Couldn't read the plugin's version (its .claude-plugin/plugin.json)")
    if not current:
        return [have]
    if os.environ.get("JOB_APPLY_NO_UPDATE_CHECK") == "1":
        return [have, Check("update", SKIPPED, "Didn't look for a newer version (JOB_APPLY_NO_UPDATE_CHECK is set)")]
    latest = await latest_version()
    if latest is None:
        return [have, Check("update", SKIPPED, "Couldn't look for a newer version (no internet?)")]
    if newer(latest, current):
        how = ", then ".join(f"`{c}`" for c in UPDATE_COMMANDS)
        return [have, Check("update", ADVICE, f"A newer job-apply is out: {latest} (you have {current})",
                            f"To update, run {how} in a terminal, then restart the Claude app.")]
    return [have, Check("update", OK, "That's the newest version")]


def check_uv_python_dir() -> Check:
    """Windows: the Claude app keeps its own copy of what's written under AppData, so a Python
    uv puts there can be missing for it. UV_PYTHON_INSTALL_DIR outside AppData avoids that."""
    value = user_env("UV_PYTHON_INSTALL_DIR")
    if value and not in_appdata(value):
        return Check("uv_python_dir", OK, f"uv keeps the plugin's Python outside AppData ({value})")
    say = (f"UV_PYTHON_INSTALL_DIR ({value}) is inside AppData" if value else
           "UV_PYTHON_INSTALL_DIR isn't set, so uv keeps the plugin's Python inside AppData")
    return Check("uv_python_dir", PROBLEM, say + ", where the Claude app can't always find it",
                 f"{INSTALLER} It sets it. Or, in PowerShell, run "
                 "[Environment]::SetEnvironmentVariable('UV_PYTHON_INSTALL_DIR', \"$env:USERPROFILE\\.uv-python\", 'User') "
                 "and restart the Claude app.")


def in_appdata(path: str) -> bool:
    text = path.replace("/", "\\").rstrip("\\").lower() + "\\"
    roots = [os.environ.get(v, "") for v in ("APPDATA", "LOCALAPPDATA")]
    return "\\appdata\\" in text or any(r and text.startswith(r.replace("/", "\\").rstrip("\\").lower() + "\\")
                                         for r in roots)


def installed(attempt: dict[str, Any]) -> bool:
    """Is this launch attempt's browser on the computer (looked for, not started)?"""
    if "executable_path" in attempt:
        return Path(attempt["executable_path"]).exists()
    channel = attempt.get("channel")
    if channel == "chrome":
        return chrome_installed()
    if channel == "msedge":
        return edge_installed()
    if channel == "chrome-beta":
        return chrome_beta_installed()
    return bundled_chromium_installed()


def browser_name(attempt: dict[str, Any]) -> str:
    if "executable_path" in attempt:
        return f"the browser JOB_APPLY_CHROMIUM_PATH names ({attempt['executable_path']})"
    return BROWSER_NAMES.get(attempt.get("channel", ""), "the plugin's own Chromium")


async def check_browser(settings: config.Settings, launch: bool) -> Check:
    """The browser the Job Desk would start: the first of its choices (browser.launch_attempts)
    that's on the computer."""
    attempts = launch_attempts(settings)
    get_chromium = (f'run `uv run --project "{in_shell(config.PLUGIN_ROOT / "server")}" playwright install chromium`'
                    " in a terminal")
    install = f"Install Google Chrome (https://www.google.com/chrome/), or {get_chromium}."
    found = [a for a in attempts if installed(a)]
    if not found:
        if "executable_path" in attempts[0]:
            return Check("browser", PROBLEM, f"No browser at {attempts[0]['executable_path']} "
                         "(JOB_APPLY_CHROMIUM_PATH)", "Point JOB_APPLY_CHROMIUM_PATH at the browser, or remove it.")
        if settings.browser_channel == "chromium":
            return Check("browser", PROBLEM, "Your profile asks for the plugin's own Chromium "
                         "(settings.browser_channel: chromium), and it isn't installed", sentence(get_chromium))
        return Check("browser", PROBLEM, "No browser the plugin can use: no Google Chrome, Microsoft Edge "
                     "or the plugin's own Chromium", install)
    if not launch:
        return Check("browser", OK, f"Browser: {browser_name(found[0])}")
    error = ""
    for attempt in found:
        error = await launches(attempt) or ""
        if not error:
            return Check("browser", OK, f"Browser: {browser_name(attempt)} (it starts)")
        if not _NOT_INSTALLED.search(error):  # it's there but won't start: the desk would stop here too
            return Check("browser", PROBLEM, f"{cap(browser_name(attempt))} is installed but wouldn't start: {error}",
                         f"Restart the computer and try again. If it still won't start, {get_chromium} and set "
                         "settings.browser_channel: chromium in your profile.")
    return Check("browser", PROBLEM, f"No browser would start: {error}", install)


def check_home() -> Check:
    folder = config.home()
    if folder.is_dir():
        return Check("folder", OK, f"Your job-apply folder: {folder}")
    return Check("folder", PROBLEM, f"Your job-apply folder ({folder}) isn't there yet",
                 f"Run the one-step installer, or {SAY_SETUP}: setup makes it.")


def check_profile() -> tuple[config.Profile | None, list[Check]]:
    """The profile as setup_status reads it (config.Profile): its required answers, and, once
    those are in, what it could also hold. Only which answers are missing is said, never one."""
    path = config.profile_path()
    if not path.exists():
        return None, [Check("profile", PROBLEM, f"No profile yet ({path})", SETUP)]
    try:
        prof = config.Profile.load()
    except (ValueError, OSError) as e:  # its own plain words: the file, and the line with a typo
        return None, [Check("profile", PROBLEM, str(e), f"Fix that line, or {SAY_SETUP} and Claude fixes it.")]
    except Exception as e:  # a section in a shape it can't take (answers: 5): which isn't said, nor its values
        return None, [unreadable(path, e)]
    try:
        return prof, profile_checks(prof, path)
    except Exception as e:
        return None, [unreadable(path, e)]


def unreadable(path: Path, e: Exception) -> Check:
    return Check("profile", PROBLEM, f"Your profile ({path}) has a section the plugin can't read ({type(e).__name__})",
                 f"{cap(SAY_SETUP)} and Claude fixes it.")


def profile_checks(prof: config.Profile, path: Path) -> list[Check]:
    checks = []
    missing = [m for m in prof.missing_required() if not m.startswith("documents.resume")]  # the resume's own check
    if len(missing) > len(FIELD_WORDS) // 2:  # a new one, as the template made it
        checks.append(Check("profile", PROBLEM, f"Your profile ({path}) isn't filled in yet", SETUP))
    elif missing:
        words = ", ".join(FIELD_WORDS.get(m, m) for m in missing)
        checks.append(Check("profile", PROBLEM, f"Your profile ({path}) still needs {words}", SETUP))
    else:
        checks.append(Check("profile", OK, f"Your profile has its answers ({path})"))
        gaps = [GAP_WORDS.get(g.split(":")[0], g.split(":")[0]) for g in prof.profile_gaps()]
        if gaps:
            checks.append(Check("profile_gaps", ADVICE, "Your profile could also hold " + "; ".join(gaps),
                                'With these the Job Desk stops less often: in Claude, say "ask me everything the desk needs".'))
    for warning in prof.settings.warnings:
        checks.append(Check("settings", ADVICE, warning, "Change it under settings: in your profile."))
    return checks


def resume_in_home() -> Path | None:
    """A resume the installer copied into ~/.job-apply (resume.pdf, resume.docx, ...)."""
    for suffix in RESUME_TYPES:
        p = config.home() / f"resume{suffix}"
        if p.is_file():
            return p
    return None


def check_resume(prof: config.Profile | None) -> Check:
    """The resume the profile names (its own name isn't said: it may be the person's)."""
    named = config.expand(prof.get("documents.resume")) if prof else None
    if named and named.is_file():
        return Check("resume", OK, "Your resume is there (the file your profile names)")
    copied = resume_in_home()
    if named:
        if copied:
            return Check("resume", PROBLEM, f"The resume file your profile names isn't there, but {copied.name} is "
                         "in your job-apply folder", sentence(f"{SAY_SETUP}: setup points your profile at it"))
        return Check("resume", PROBLEM, "The resume file your profile names isn't there",
                     f"Put it back, or {SAY_SETUP} and give Claude your resume.")
    if copied:
        return Check("resume", OK, f"{copied.name} is in your job-apply folder: setup will use it")
    return Check("resume", PROBLEM, "No resume yet",
                 f"Run the one-step installer with your resume, or {SAY_SETUP} and give Claude your resume.")


async def run(launch: bool = False) -> Report:
    """Every check, in the order a person would fix them. Creates nothing."""
    checks = [check_python(), check_uv(), *(await check_version())]
    if on_windows():
        checks.append(check_uv_python_dir())
    prof, profile_checks = check_profile()
    checks.append(await check_browser(prof.settings if prof else config.Settings.from_dict(None), launch))
    checks.append(check_home())
    checks.extend(profile_checks)
    checks.append(check_resume(prof))
    return Report(checks)


def marks_for(stream: TextIO) -> dict[str, str]:
    """✔ and ✘ where the console shows them: not Windows' older console window (it shows a box
    for each), nor one whose encoding hasn't got them."""
    if on_windows() and not (os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM")):
        return ASCII_MARKS
    try:
        "".join(MARKS.values()).encode(getattr(stream, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return ASCII_MARKS
    return MARKS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="job-apply-doctor",
        description="Check that everything job-apply needs is in place on this computer, and say what to do "
                    "about anything that isn't. Changes nothing.")
    parser.add_argument("--launch", action="store_true", help="also start the browser (out of sight) to be sure it opens")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    report = asyncio.run(run(launch=args.launch))
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:  # a path the console can't show mustn't stop the report
        reconfigure(errors="replace")
    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(report.text(marks_for(sys.stdout)))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
