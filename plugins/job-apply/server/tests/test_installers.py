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

CLAUDE = r"""#!/bin/sh
echo "claude $*" >> "$STUB_LOG"
case "$*" in
  "--version") echo "2.1.0 (Claude Code)" ;;
  "plugin marketplace list --json")
    if [ -f "$STUB_STATE/market" ]; then printf '[\n  {\n    "name": "sebob-jobs",\n    "source": "github"\n  }\n]\n'
    else echo '[]'; fi ;;
  "plugin marketplace add sebob2491/Jobs") touch "$STUB_STATE/market" ;;
  "plugin marketplace update sebob-jobs") ;;
  "plugin list --json")
    if [ -f "$STUB_STATE/plugin" ]; then
      printf '[\n  {\n    "id": "dev-kit@sebob-jobs",\n    "enabled": true,\n    "installPath": "/elsewhere"\n  },\n'
      printf '  {\n    "id": "job-apply@sebob-jobs",\n    "version": "0.3.92",\n    "enabled": %s,\n    "installPath": "%s"\n  }\n]\n' \
        "$(cat "$STUB_STATE/enabled" 2>/dev/null || echo true)" "$STUB_PLUGIN"
    else echo '[]'; fi ;;
  "plugin install job-apply@sebob-jobs") touch "$STUB_STATE/plugin" ;;
  "plugin update job-apply@sebob-jobs") ;;
  "plugin enable job-apply@sebob-jobs") echo true > "$STUB_STATE/enabled" ;;
  *) echo "unexpected: $*" >&2; exit 3 ;;
esac
"""
UV = r"""#!/bin/sh
echo "uv $*" >> "$STUB_LOG"
[ "$1" = --version ] && echo "uv 0.11.32"
exit 0
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
    env = {k: v for k, v in os.environ.items() if not k.startswith(("JOB_APPLY_", "CLAUDE", "UV_", "PLAYWRIGHT", "XDG_"))
           and k not in ("USERPROFILE", "VIRTUAL_ENV")}
    env.update(HOME=str(home), PATH=f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", STUB_LOG=str(tmp_path / "calls.log"),
               STUB_STATE=str(state), STUB_PLUGIN=str(plugin))

    class Machine:
        def __init__(self):
            self.home, self.bin, self.state, self.plugin, self.env = home, bin_dir, state, plugin, env
            self.log = tmp_path / "calls.log"

        def tools(self, *names):
            for name in names:
                path = bin_dir / name
                path.write_text({"claude": CLAUDE, "uv": UV}[name])
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
    assert "Would run: claude plugin marketplace update sebob-jobs" in out
    assert "Would run: claude plugin update job-apply@sebob-jobs" in out
    assert "Would set UV_PYTHON_INSTALL_DIR" in out
    assert "Would run: uv sync --frozen --project" in out and "job-apply-doctor --launch" in out
    calls = machine.calls()
    assert calls and not [c for c in calls if any(f" {word} " in f" {c} " for word in CHANGES)], calls
    assert not (machine.home / ".job-apply").exists()


def test_the_readme_gives_both_commands():
    readme = (config.PACKAGE_DIR.parents[4] / "README.md").read_text(encoding="utf-8")
    base = "https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/"
    assert f"irm {base}install.ps1 | iex" in readme and f"curl -fsSL {base}install.sh | bash" in readme
    assert Path(SETUP / "install.ps1").exists() and Path(SETUP / "install.sh").exists()
