"""The one-step installers (plugins/job-apply/setup): each step looks before it acts, a second
run changes nothing, and a dry run only says what it would do. `claude` and `uv` are stand-ins
that note how they were called, so nothing is installed or downloaded."""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from job_apply import config, updates

SETUP = config.PACKAGE_DIR.parents[2] / "setup"
INSTALL_SH = SETUP / "install.sh"
INSTALL_PS1 = SETUP / "install.ps1"
BASH = shutil.which("bash")
PWSH = shutil.which("pwsh")
needs_bash = pytest.mark.skipif(BASH is None or sys.platform == "win32", reason="no bash")

# The stand-ins, in Python so they run the same everywhere: a `claude` or `uv` beside them
# (a .cmd on Windows, which can't run a shell script) hands its arguments to this.
STAND_IN = r"""
import json, os, sys
from pathlib import Path

tool, args = sys.argv[1], sys.argv[2:]
state = Path(os.environ["STUB_STATE"])
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(" ".join([tool, *args]) + "\n")
said = " ".join(args)
if tool == "uv":
    if said == "--version":
        print("uv 0.11.32 (stand-in)")
elif said == "--version":
    print("2.1.0 (Claude Code)")
elif said == "plugin marketplace list --json":
    print(json.dumps([{"name": "sebob-jobs", "source": "github"}] if (state / "market").exists() else [], indent=2))
elif said == "plugin marketplace add sebob2491/Jobs":
    (state / "market").touch()
elif said == "plugin list --json":
    plugins = []
    if (state / "plugin").exists():
        enabled = not (state / "enabled").exists() or (state / "enabled").read_text().strip() != "false"
        plugins = [{"id": "dev-kit@sebob-jobs", "enabled": True, "installPath": "/elsewhere"},
                   {"id": "job-apply@sebob-jobs", "version": "0.3.92", "enabled": enabled,
                    "installPath": os.environ["STUB_PLUGIN"]}]
    print(json.dumps(plugins, indent=2))
elif said == "plugin install job-apply@sebob-jobs":
    (state / "plugin").touch()
elif said == "plugin enable job-apply@sebob-jobs":
    (state / "enabled").write_text("true")
elif said not in ("plugin marketplace update sebob-jobs", "plugin update job-apply@sebob-jobs"):
    print("unexpected: " + said, file=sys.stderr)
    sys.exit(3)
"""
CHANGES = ("install", "add", "update", "enable", "sync", "run")  # what a dry run never calls


@pytest.fixture
def machine(tmp_path):
    """A home folder, `claude` and `uv` stand-ins, and where the plugin "is installed" (a path
    with a space, as a Windows user name often has)."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    plugin = tmp_path / "Claude plugins" / "job-apply" / "0.3.92"
    (plugin / "server").mkdir(parents=True)
    (plugin / "server" / "pyproject.toml").write_text('[project.scripts]\njob-apply-doctor = "job_apply.doctor:main"\n')
    (tmp_path / "stand_in.py").write_text(STAND_IN)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("JOB_APPLY_", "CLAUDE", "UV_", "PLAYWRIGHT", "XDG_"))
           and k.upper() not in ("USERPROFILE", "VIRTUAL_ENV", "APPDATA", "LOCALAPPDATA")}
    env.update(HOME=str(home), STUB_LOG=str(tmp_path / "calls.log"), STUB_STATE=str(state), STUB_PLUGIN=str(plugin))
    if sys.platform == "win32":  # the person's folders, all in the same home
        env.update(USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
                   LOCALAPPDATA=str(home / "AppData" / "Local"),
                   PATH=os.pathsep.join([str(bin_dir), os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32")]))
    else:
        env["PATH"] = os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])

    class Machine:
        def __init__(self):
            self.home, self.bin, self.state, self.plugin, self.env = home, bin_dir, state, plugin, env
            self.log = tmp_path / "calls.log"

        def tools(self, *names):
            stand_in = tmp_path / "stand_in.py"
            for name in names:
                if sys.platform == "win32":  # Windows runs a .cmd, not a script without an extension
                    (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{stand_in}" {name} %*\r\n'
                                                         "@exit /b %ERRORLEVEL%\r\n")
                else:
                    path = bin_dir / name
                    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stand_in}" {name} "$@"\n')
                    path.chmod(path.stat().st_mode | stat.S_IEXEC)

        def calls(self):
            text = self.log.read_text() if self.log.exists() else ""
            self.log.write_text("")
            return text.splitlines()

        def run(self, command):
            done = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120,
                                  stdin=subprocess.DEVNULL, start_new_session=True)  # no keyboard to ask on
            return done.returncode, done.stdout + done.stderr

    return Machine()


def sh(m, *args):
    return m.run([BASH, str(INSTALL_SH), *args])


@needs_bash
def test_install_sh_is_valid_bash():
    done = subprocess.run([BASH, "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert os.access(INSTALL_SH, os.X_OK)


def test_installers_are_plain_ascii_and_agree_with_the_desk():
    """Windows PowerShell 5.1 reads a script file without a byte-order mark as the local code
    page: plain ASCII reads the same everywhere. Both name the marketplace and plugin the desk's
    update notice does."""
    for script in (INSTALL_SH, INSTALL_PS1):
        text = script.read_text(encoding="utf-8")
        assert text.isascii(), script
        for command in updates.UPDATE_COMMANDS:
            assert command.split()[-1] in text  # sebob-jobs, job-apply@sebob-jobs
        assert "sebob2491/Jobs" in text and "job-apply-doctor --launch" in text and "UV_PYTHON_INSTALL_DIR" in INSTALL_PS1.read_text()


@needs_bash
def test_dry_run_on_a_bare_computer_changes_nothing(machine):
    code, out = sh(machine, "--dry-run")
    assert code == 0, out
    assert "Dry run" in out and "That's the plan." in out
    assert "Claude Code isn't installed" in out and "claude.ai/install.sh" in out
    assert "Would run, once Claude Code is installed: claude plugin marketplace add sebob2491/Jobs" in out
    assert "Would install it with Astral's official installer: curl -LsSf https://astral.sh/uv/install.sh | sh" in out
    assert "Would make it:" in out
    assert list(machine.home.iterdir()) == []


@needs_bash
def test_dry_run_with_the_plugin_installed_only_looks(machine):
    machine.tools("claude", "uv")
    (machine.state / "market").touch()
    (machine.state / "plugin").touch()
    resume = machine.home / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")
    code, out = sh(machine, "--dry-run", "--resume", str(resume))
    assert code == 0, out
    calls = machine.calls()
    assert calls and not [c for c in calls if any(f" {word} " in f" {c} " for word in CHANGES)], calls
    server = machine.plugin / "server"
    assert "Would run: claude plugin marketplace update sebob-jobs" in out
    assert "Would run: claude plugin update job-apply@sebob-jobs" in out
    assert f'Would run: uv sync --frozen --project "{server}"' in out
    assert f'Would run: uv run --frozen --project "{server}" job-apply-doctor --launch' in out
    assert f"Would copy your resume to {machine.home}/.job-apply/resume.pdf" in out
    assert not (machine.home / ".job-apply").exists()


@needs_bash
def test_install_then_run_again(machine, tmp_path):
    machine.tools("claude", "uv")
    resume = tmp_path / "My Resume.PDF"
    resume.write_bytes(b"%PDF-1.4\n% resume\n")
    server = machine.plugin / "server"

    code, out = sh(machine, "--yes", "--resume", str(resume))
    assert code == 0, out
    calls = machine.calls()
    assert "claude plugin marketplace add sebob2491/Jobs" in calls
    assert "claude plugin install job-apply@sebob-jobs" in calls
    assert f"uv sync --frozen --project {server}" in calls
    assert f"uv run --quiet --frozen --project {server} job-apply-doctor --launch" in calls
    folder = machine.home / ".job-apply"
    assert (folder / "resume.pdf").read_bytes() == resume.read_bytes()
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert 'say "set up job-apply"' in out and "All set." in out

    # again: the marketplace and plugin are updated rather than added, the resume left as it is
    code, out = sh(machine, "--yes", "--resume", str(resume))
    assert code == 0, out
    calls = machine.calls()
    assert "claude plugin marketplace update sebob-jobs" in calls and "claude plugin update job-apply@sebob-jobs" in calls
    assert not [c for c in calls if c.startswith(("claude plugin install", "claude plugin marketplace add"))]
    assert "already in your job-apply folder" in out
    assert sorted(p.name for p in folder.iterdir()) == ["resume.pdf"]

    # a new resume: the old one is kept under another name, so setup reads the new one
    newer = tmp_path / "resume 2026.docx"
    newer.write_bytes(b"PK new")
    code, out = sh(machine, "--yes", f"--resume={newer}")
    assert code == 0, out
    names = sorted(p.name for p in folder.iterdir())
    assert names[1:] == ["resume.docx"] and names[0].startswith("resume-before-") and names[0].endswith(".pdf")

    # a plugin someone turned off is turned back on
    (machine.state / "enabled").write_text("false\n")
    code, out = sh(machine, "--yes")
    assert "claude plugin enable job-apply@sebob-jobs" in machine.calls()


@needs_bash
def test_a_resume_that_isnt_there_is_said(machine):
    machine.tools("claude", "uv")
    code, out = sh(machine, "--yes", "--resume", "~/Documents/missing resume.pdf")
    assert code == 1
    assert f"There's no file at {machine.home}/Documents/missing resume.pdf" in out
    assert "Some steps didn't finish" in out


@pytest.mark.skipif(PWSH is None, reason="no PowerShell (pwsh)")
def test_install_ps1_dry_run(machine):
    machine.tools("claude", "uv")
    (machine.state / "market").touch()
    (machine.state / "plugin").touch()
    code, out = machine.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(INSTALL_PS1), "-DryRun"])
    assert code == 0, out
    assert "Dry run" in out and "That's the plan." in out and "went wrong" not in out
    # the stand-ins ran, and what they said was read
    assert "Claude Code is installed (version 2.1.0)." in out and "uv is installed (version 0.11.32)." in out
    assert "Would run: claude plugin marketplace update sebob-jobs" in out
    assert "Would run: claude plugin update job-apply@sebob-jobs" in out
    assert "marketplace add" not in out and "plugin install" not in out
    # never an empty path (a Windows runner's own registry may already hold a setting: then it's kept)
    assert (f"Would set UV_PYTHON_INSTALL_DIR to {machine.home / '.uv-python'} for you" in out
            or "uv keeps the plugin's Python in" in out), out
    assert f"Would make it: {machine.home / '.job-apply'}" in out
    assert "Would run: uv sync --frozen --project" in out and "job-apply-doctor --launch" in out
    calls = machine.calls()
    assert calls and not [c for c in calls if any(f" {word} " in f" {c} " for word in CHANGES)], calls
    assert not (machine.home / ".job-apply").exists()


@pytest.mark.skipif(PWSH is None or sys.platform != "win32", reason="Windows PowerShell only")
def test_install_ps1_remakes_an_appdata_python_environment_only_when_its_free(machine, tmp_path):
    """A .venv made with uv's Python inside AppData is made again outside it. While the Claude
    app runs the plugin from it, deleting it took what wasn't held and left half an environment
    under the running server; now it's moved aside whole or left alone, and the person is asked
    to quit the Claude app first."""
    machine.tools("claude", "uv")
    (machine.state / "market").touch()
    (machine.state / "plugin").touch()
    venv = machine.plugin / "server" / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    python_home = machine.home / "AppData" / "Roaming" / "uv" / "python" / "cpython-3.12-windows-x86_64-none"
    (venv / "pyvenv.cfg").write_text(f"home = {python_home}\n")
    (venv / "Scripts" / "python.exe").write_bytes(b"MZ")
    # uv keeps Python outside AppData now (a folder that needn't exist: nothing is installed)
    machine.env["UV_PYTHON_INSTALL_DIR"] = os.path.join(os.path.splitdrive(str(tmp_path))[0] + os.sep, "uv-python-elsewhere")
    command = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(INSTALL_PS1)]

    with open(venv / "Scripts" / "python.exe", "rb"):  # in use, as by the plugin's running server
        code, out = machine.run(command)
    assert code == 1, out
    assert "Quit the Claude app, then run this installer again" in out
    assert (venv / "pyvenv.cfg").exists() and (venv / "Scripts" / "python.exe").exists()  # all of it, untouched
    assert not list(venv.parent.glob(".venv-old-*"))
    assert not [c for c in machine.calls() if c.startswith("uv sync")]

    code, out = machine.run(command)  # the app quit: now it's made again
    assert code == 0, out
    assert "Making the plugin's Python environment again, outside AppData." in out
    assert not venv.exists() and not list(venv.parent.glob(".venv-old-*"))
    assert [c for c in machine.calls() if c.startswith("uv sync --frozen --project")]


def test_the_readme_gives_both_commands():
    readme = (config.PACKAGE_DIR.parents[4] / "README.md").read_text(encoding="utf-8")
    base = "https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/"
    assert f"irm {base}install.ps1 | iex" in readme and f"curl -fsSL {base}install.sh | bash" in readme
    assert Path(SETUP / "install.ps1").exists() and Path(SETUP / "install.sh").exists()
