"""JobDeskSetup.exe, the Job Desk's double-click Windows installer (plugins/job-apply/setup/windows: the owner's choice
of Nov 2), and the workflow that builds it, installs it and uninstalls it on Windows
(.github/workflows/windows-installer.yml), read as Inno Setup and GitHub read them: it installs for the person alone
with no administrator, carries the plugin without its tests (and their fixtures) or caches, its shortcuts and
finish page open the desk, Claude Code is a box left unticked, uv's Python goes outside AppData as install.ps1 puts
it, the uninstaller takes only the program's own folders (never ~/.job-apply), and every action is pinned by its
commit. The workflow is what runs it on Windows; these keep its promises from drifting on every change."""

import fnmatch
import re
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[2]  # plugins/job-apply
REPO = PLUGIN.parents[1]
WINDOWS = PLUGIN / "setup" / "windows"
ISS = WINDOWS / "JobDeskSetup.iss"
BUILD = WINDOWS / "build.ps1"
STOP = WINDOWS / "stop-desk.ps1"
WORKFLOW = REPO / ".github" / "workflows" / "windows-installer.yml"
DESK_ARGS = 'run --frozen --python {#Python} --project "{app}\\plugin\\server" job-apply-desk'


def _sections(text: str) -> dict[str, list[str]]:
    """The script's lines by [Section], without comments or blank lines ([Code] kept whole, comments and all)."""
    out: dict[str, list[str]] = {"": []}
    name = ""
    for line in text.splitlines():
        head = re.fullmatch(r"\[(\w+)\]\s*", line)
        if head:
            name = head.group(1)
            out.setdefault(name, [])
            continue
        if name == "Code" or (line.strip() and not line.lstrip().startswith(";")):
            out[name].append(line)
    return out


def _entry(line: str) -> dict[str, str]:
    """`Name: "a"; Parameters: "run ""{app}"" x"; Flags: b c` as Inno Setup reads it ("" in a quoted value is ")."""
    fields: dict[str, str] = {}
    i = 0
    while i < len(line):
        colon = line.index(":", i)
        key = line[i:colon].strip()
        i = colon + 1
        while i < len(line) and line[i] == " ":
            i += 1
        if i < len(line) and line[i] == '"':
            value, i = "", i + 1
            while True:
                end = line.index('"', i)
                value += line[i:end]
                if line[end + 1:end + 2] == '"':
                    value, i = value + '"', end + 2
                    continue
                i = end + 1
                break
            semi = line.find(";", i)
            i = len(line) if semi < 0 else semi + 1
        else:
            semi = line.find(";", i)
            value = line[i:] if semi < 0 else line[i:semi]
            i = len(line) if semi < 0 else semi + 1
        fields[key] = value.strip()
    return fields


def _defines(text: str) -> dict[str, str]:
    return dict(re.findall(r'^#define (\w+) "(.*)"$', text, re.M))


@pytest.fixture(scope="module")
def iss():
    text = ISS.read_text(encoding="utf-8")
    parts = _sections(text)
    setup = dict(line.split("=", 1) for line in parts["Setup"])
    entries = {name: [_entry(line) for line in lines] for name, lines in parts.items()
               if name not in ("", "Setup", "Code")}
    return {"text": text, "setup": setup, "code": "\n".join(parts["Code"]), "defines": _defines(text), **entries}


def test_the_installer_files_are_plain_ascii():
    """Windows PowerShell 5.1 reads a script without a byte-order mark as the local code page, and Inno Setup a
    script as the computer's: plain ASCII reads the same everywhere."""
    for f in (ISS, BUILD, STOP, WORKFLOW):
        assert f.read_text(encoding="utf-8").isascii(), f


def test_it_installs_for_the_person_alone_without_an_administrator(iss):
    s = iss["setup"]
    assert s["PrivilegesRequired"] == "lowest"
    assert "PrivilegesRequiredOverridesAllowed" not in s  # (no /ALLUSERS: never Program Files)
    assert s["DefaultDirName"] == "{autopf}\\JobDesk"  # %LOCALAPPDATA%\Programs\JobDesk with "lowest"
    assert s["ArchitecturesInstallIn64BitMode"] == "x64compatible"
    assert s["OutputBaseFilename"] == "JobDeskSetup" and s["AppName"] == "Job Desk"
    assert re.fullmatch(r"\{\{[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\}", s["AppId"])  # (one AppId, kept for updates)
    for name in ("Files", "Icons", "Registry", "Dirs", "InstallDelete", "UninstallDelete"):
        for e in iss.get(name, []):
            where = e.get("DestDir", "") + e.get("Name", "") + e.get("Root", "")
            assert not re.search(r"\{(common|pf\b|pf32|pf64|win\b|sys\b)|HKLM|HKEY_LOCAL_MACHINE", where, re.I), e
    # ([Code] writes only the person's own settings; Program Files is only looked in, for Chrome and Edge)
    assert not re.search(r"HKLM|HKEY_LOCAL_MACHINE", iss["code"])
    assert re.findall(r"RegWrite\w*\((\w+)", iss["code"]) == ["HKEY_CURRENT_USER"]
    assert [e["Name"] for e in iss["Icons"]] == ["{autoprograms}\\Job Desk", "{autodesktop}\\Job Desk"]


def _excluded(parts: tuple[str, ...], patterns: list[str]) -> bool:
    """Inno Setup's Excludes: a pattern starting with a backslash matches the path from the source folder on, any
    other its end (whole names)."""
    path = "\\".join(parts).lower()
    for pattern in patterns:
        p = pattern.lower()
        if p.startswith("\\"):
            if fnmatch.fnmatchcase(path, p[1:]):
                return True
        else:
            depth = p.count("\\") + 1
            if len(parts) >= depth and fnmatch.fnmatchcase("\\".join(parts[-depth:]).lower(), p):
                return True
    return False


def _carried(source: Path, patterns: list[str]) -> list[tuple[str, ...]]:
    found = []

    def walk(folder: Path, parts: tuple[str, ...]) -> None:
        for child in sorted(folder.iterdir()):
            rel = (*parts, child.name)
            if _excluded(rel, patterns):
                continue
            if child.is_dir():
                walk(child, rel)
            else:
                found.append(rel)

    walk(source, ())
    return found


def test_it_carries_the_plugin_without_its_tests_or_caches_and_uv_beside_it(iss):
    plugin_entry = next(e for e in iss["Files"] if e["DestDir"] == "{app}\\plugin")
    assert plugin_entry["Source"] == "{#PluginDir}\\*"
    assert (WINDOWS / iss["defines"]["PluginDir"].replace("\\", "/")).resolve() == PLUGIN
    assert {"recursesubdirs", "createallsubdirs"} <= set(plugin_entry["Flags"].split())
    assert plugin_entry["Excludes"] == "{#PluginExcludes}"
    patterns = iss["defines"]["PluginExcludes"].split(",")
    carried = _carried(PLUGIN, patterns)
    assert carried and not [p for p in carried if p[:2] == ("server", "tests")]  # (the fixtures among them)
    assert not [p for p in carried if {"__pycache__", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache"} & set(p)
                or p[-1].endswith(".pyc")]
    assert not [p for p in carried if p[:3] in (("setup", "windows", "build"), ("setup", "windows", "Output"))]
    for needed in [(".claude-plugin", "plugin.json"), (".mcp.json",), ("server", "pyproject.toml"),
                   ("server", "uv.lock"), ("server", "src", "job_apply", "desk.py"),
                   ("server", "src", "job_apply", "static", "desk.html"), ("data", "companies.yaml"),
                   ("setup", "install.ps1"), ("setup", "windows", "stop-desk.ps1")]:
        assert needed in carried, needed
    assert _excluded(("server", "tests"), patterns)  # (the folder itself, so nothing under it is looked at)
    assert {(e["Source"], e["DestDir"]) for e in iss["Files"] if e.get("DestDir") == "{app}\\bin"} == {
        ("build\\uv\\uv.exe", "{app}\\bin"), ("build\\uv\\uvw.exe", "{app}\\bin")}


def test_the_shortcuts_and_the_finish_page_open_the_desk_with_no_console_window(iss):
    """uvw is uv without a console window; job-apply-desk serves the desk and opens its page (a second start, the desk
    already running, opens that one: desk.running_desk)."""
    pyproject = (PLUGIN / "server" / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^job-apply-desk = "job_apply\.desk:main"$', pyproject, re.M)
    launches = [*iss["Icons"], *[e for e in iss["Run"] if "postinstall" in e.get("Flags", "")]]
    assert len(launches) == 3
    for e in launches:
        assert e["Filename"] == "{app}\\bin\\uvw.exe" and e["Parameters"] == DESK_ARGS, e
        assert e["WorkingDir"] == "{app}\\plugin\\server"
    start_menu, desktop = iss["Icons"]
    assert "Tasks" not in start_menu and desktop["Tasks"] == "desktopicon"
    finish = launches[-1]
    assert finish["Description"] == "Open the Job Desk now"
    assert set(finish["Flags"].split()) == {"postinstall", "nowait", "skipifsilent"}  # (ticked: no "unchecked")


def test_claude_code_is_a_box_left_unticked_that_runs_the_one_step_installer(iss):
    task = next(t for t in iss["Tasks"] if t["Name"] == "claudecode")
    assert task["Description"] == "Also set up Claude Code (for the chat features)" and task["Flags"] == "unchecked"
    desktop = next(t for t in iss["Tasks"] if t["Name"] == "desktopicon")
    assert "unchecked" not in desktop.get("Flags", "")
    run = next(e for e in iss["Run"] if e.get("Tasks") == "claudecode")
    assert run["Filename"].endswith("powershell.exe") and run["Parameters"] == "{code:ClaudeCodeParams}"
    assert "{app}\\plugin\\setup\\install.ps1" in iss["code"] and " -Yes" in iss["code"]
    assert (PLUGIN / "setup" / "install.ps1").exists()


def test_uv_installs_the_desks_python_outside_appdata_on_a_python_the_lock_has_wheels_for(iss):
    """As install.ps1: UV_PYTHON_INSTALL_DIR outside AppData (%USERPROFILE%\\.uv-python), and uv sync --frozen for the
    server. On the Python pinned here: uv's newest has no pydantic-core wheel in uv.lock, and building one needs Rust
    (the first build's install under Wine stopped there)."""
    code = iss["code"]
    assert "'UV_PYTHON_INSTALL_DIR'" in code and "{%USERPROFILE}\\.uv-python" in code and "InAppData(Dir)" in code
    assert "'sync --frozen --managed-python --python {#Python} ' + Project" in code
    assert "ExecAndLogOutput(" in code and "SW_HIDE" in code  # (its lines under the progress bar; no console window)
    assert iss["setup"]["ChangesEnvironment"] == "yes"
    python = iss["defines"]["Python"]
    tag = "cp" + python.replace(".", "")
    lock = (PLUGIN / "server" / "uv.lock").read_text(encoding="utf-8")
    pydantic_core = lock.split('\nname = "pydantic-core"\n', 1)[1].split("\n[[package]]", 1)[0]
    assert f"-{tag}-{tag}-win_amd64.whl" in pydantic_core, f"uv.lock has no pydantic-core wheel for Windows {python}"
    assert f"--python {python} " in WORKFLOW.read_text(encoding="utf-8")


def test_the_browser_is_edge_or_chrome_when_there_and_chromium_only_without_them(iss):
    code = iss["code"]
    assert "Google\\Chrome\\Application\\chrome.exe" in code and "Microsoft\\Edge\\Application\\msedge.exe" in code
    assert re.search(r"if HasBrowser\(\) then\s+Log\([^)]*\)\s+else if not RunUv\([^;]*playwright install chromium",
                     code, re.S)


def test_the_uninstaller_takes_only_the_programs_own_folders_never_the_persons(iss):
    """~/.job-apply (the profile, resume, tracker and passwords) is the person's: nothing in the script names it, and
    what the uninstaller deletes beyond the files it installed is under the program's folder alone. The desk running
    from it is stopped first, matched by the install folder (stop-desk.ps1)."""
    deletes = iss["UninstallDelete"]
    assert deletes and all(e["Name"].startswith("{app}\\") or (e["Name"], e["Type"]) == ("{app}", "dirifempty")
                           for e in deletes), deletes
    assert all(e["Name"].startswith("{app}\\") for e in iss["InstallDelete"])
    everything = iss["text"].lower()
    for theirs in (".job-apply", "{userdocs}", "{%home", "{userappdata}", "uninsdeletevalue", "uninsdeletekey",
                   "deltree", "deletefile("):
        assert theirs not in everything.replace("~/.job-apply: profile", ""), theirs
    assert not iss.get("Registry")  # (UV_PYTHON_INSTALL_DIR is written by [Code], and stays for the plugin)
    assert "StopDesk(ExpandConstant('{app}\\plugin\\setup\\windows\\stop-desk.ps1'))" in iss["code"]
    assert "CurUninstallStep = usUninstall" in iss["code"]
    stop = STOP.read_text(encoding="utf-8")
    assert "StartsWith($dir" in stop and "IndexOf($dir" in stop and "Stop-Process" in stop
    assert "Remove-Item" not in stop and ".job-apply" not in stop.replace("(~/.job-apply) isn't touched", "")


def test_the_build_fetches_a_pinned_uv_and_checks_its_sha256():
    text = BUILD.read_text(encoding="utf-8")
    version = re.search(r"^\$UvVersion = '(\d+\.\d+\.\d+)'$", text, re.M)
    sha = re.search(r"^\$UvSha256 = '([0-9a-f]{64})'$", text, re.M)
    assert version and sha
    assert "$UvZip = 'uv-x86_64-pc-windows-msvc.zip'" in text
    assert "https://github.com/astral-sh/uv/releases/download/$UvVersion/$UvZip" in text
    assert re.search(r"if \(\$got -ne \$UvSha256\) \{ throw", text)
    assert "'uv.exe', 'uvw.exe'" in text and '"/DAppVersion=$version"' in text
    assert "'.claude-plugin\\plugin.json'" in text


def test_no_powershell_here_names_a_variable_straight_before_a_colon():
    """The workflow's first run (Nov 9) stopped in build.ps1 before it built anything: "... the pinned $UvSha256: not
    using it" is a parse error, as PowerShell reads `$name:` as a variable in a scope or drive ($env:PATH) and wants a
    name after the colon. Written ${name}: it's the variable, then a colon. Nothing here runs PowerShell to find the
    next one, so every script the installer's build and its workflow run is read for it."""
    sources = {path.name: path.read_text(encoding="utf-8") for path in (BUILD, STOP, PLUGIN / "setup" / "install.ps1")}
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in flow["jobs"].values():
        for n, step in enumerate(job["steps"]):
            if "run" in step:
                sources[f"{WORKFLOW.name}, step {n + 1} ({step.get('name', 'unnamed')})"] = step["run"]
    assert len(sources) > 6
    for name, text in sources.items():
        # ($env:PATH, $script:x and a type's $t::Member have a name, or a second colon, after it)
        wrong = [line.strip()[:120] for line in text.splitlines() if re.search(r"\$[A-Za-z_]\w*:(?![\w:])", line)]
        assert not wrong, (name, wrong)


def test_the_workflow_builds_on_windows_installs_checks_and_uninstalls_with_actions_pinned_by_commit():
    text = WORKFLOW.read_text(encoding="utf-8")
    flow = yaml.safe_load(text)
    on = flow[True]  # (YAML 1.1 reads the key `on` as true)
    # by hand, for a tag, and for every version: a push to main that changes the manifest, which each version bumps
    assert "workflow_dispatch" in on and on["push"] == {
        "tags": ["job-apply-v*"], "branches": ["main"], "paths": ["plugins/job-apply/.claude-plugin/plugin.json"]}
    assert flow["permissions"] == {"contents": "read"}
    (job,) = flow["jobs"].values()
    assert job["runs-on"] == "windows-latest"
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) for u in uses), uses
    assert re.fullmatch(r"[0-9a-f]{64}", job["env"]["INNO_SHA256"]) and re.fullmatch(r"\d+\.\d+\.\d+",
                                                                                     job["env"]["INNO_VERSION"])
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert "build.ps1 -Iscc" in runs
    assert "'/VERYSILENT', '/SUPPRESSMSGBOXES', '/CURRENTUSER'" in runs
    doctor = next(line for line in runs.splitlines() if "job-apply-doctor" in line)
    assert "--launch" not in doctor
    # the doctor ends with 1 on a clean computer (no folder, profile or resume yet), and GitHub's PowerShell ends a
    # step with the last program's exit code: the second run (Nov 9) failed there with every check as expected
    step = next(s["run"] for s in job["steps"] if "job-apply-doctor" in s.get("run", ""))
    assert step.rstrip().endswith("$global:LASTEXITCODE = 0")
    assert "Job Desk.lnk" in runs and "http://127.0.0.1:8765/" in runs and "already running" in runs
    assert "unins000.exe" in runs and "keep-me.txt" in runs
    upload = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["path"].endswith("Output/JobDeskSetup.exe")
    release = job["steps"][-1]
    assert release["if"] == "github.event_name == 'push' || inputs.release" and "gh release upload" in release["run"]


# Nothing here runs PowerShell, so the publishing step's script is pinned whole, comments aside: a change to one of
# its lines is a change to look over again. (The reviews of Nov 9 took a line out at a time: a push went on to replace
# a published installer, a failed upload ended green, and the tests of the time passed.)
_STEP = """\
$ErrorActionPreference = 'Stop'
$version = (Get-Content -Raw -Encoding UTF8 plugins/job-apply/.claude-plugin/plugin.json | ConvertFrom-Json).version
if ($version -notmatch '^\\d+(\\.\\d+)+$') { throw "The plugin's version, $version, isn't numbers and dots" }
$tag = "job-apply-v$version"
if ($env:GITHUB_REF_TYPE -eq 'tag' -and $env:GITHUB_REF_NAME -ne $tag) {
  throw "The tag $env:GITHUB_REF_NAME isn't $tag, the plugin's version"
}
function Write-Sum([string]$file) {
  $sum = (Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLower()
  Set-Content -LiteralPath "$file.sha256" -Value "$sum  JobDeskSetup.exe`n" -NoNewline -Encoding ascii
  Get-Content -LiteralPath "$file.sha256" | Write-Host
}
$byHand = $env:GITHUB_EVENT_NAME -ne 'push'
$exe = 'plugins/job-apply/setup/windows/Output/JobDeskSetup.exe'
$draft = gh release view $tag --json isDraft --jq '.isDraft' 2>$null
$there = $LASTEXITCODE -eq 0
if ($there -and $draft -eq 'true') {
  throw "There's a draft Release for $tag. Publish it or delete it on GitHub's Releases page, then run this again"
}
$names = @()
if ($there) {
  $names = @(gh release view $tag --json assets --jq '.assets[].name')
  if ($LASTEXITCODE -ne 0) { throw "Couldn't read the Release $tag" }
}
if (($names -contains 'JobDeskSetup.exe') -and -not $byHand) {
  if ($names -contains 'JobDeskSetup.exe.sha256') {
    Write-Host "$tag already has its installer, left as it is"
  } else {
    $was = Join-Path $env:RUNNER_TEMP 'published'
    New-Item -ItemType Directory -Force -Path $was | Out-Null
    gh release download $tag --pattern 'JobDeskSetup.exe' --dir $was
    if ($LASTEXITCODE -ne 0) { throw "Couldn't download the installer $tag has" }
    $published = Join-Path $was 'JobDeskSetup.exe'
    Write-Sum $published
    gh release upload $tag "$published.sha256"
    if ($LASTEXITCODE -ne 0) { throw "Couldn't attach the sha256 to $tag" }
  }
} else {
  Write-Sum $exe
  if (-not $there) {
    gh release create $tag $exe "$exe.sha256" --target $env:GITHUB_SHA --title "job-apply $version" `
      --notes "JobDeskSetup.exe: the Job Desk for Windows. Download it, double-click it (Windows may say it protected \
your PC: More info, then Run anyway), and open the Job Desk from the Start menu."
    if ($LASTEXITCODE -ne 0) { throw "Couldn't make the Release $tag" }
  } else {
    gh release upload $tag $exe "$exe.sha256" --clobber
    if ($LASTEXITCODE -ne 0) { throw "Couldn't attach the installer to $tag" }
  }
}
$tags = @(gh api "repos/$env:GITHUB_REPOSITORY/releases?per_page=100" --jq '.[] | select(.draft | not) | .tag_name')
if ($LASTEXITCODE -ne 0) { throw "Couldn't list the Releases" }
$newest = (@($tags) + $tag) | Where-Object { $_ -match '^job-apply-v\\d+(\\.\\d+)+$' } |
  Sort-Object { [version]($_ -replace '^job-apply-v', '') } | Select-Object -Last 1
gh release edit $newest --latest
if ($LASTEXITCODE -ne 0) { throw "Couldn't mark $newest as the latest Release" }
$latest = ''
foreach ($look in 1..10) {
  $latest = gh api "repos/$env:GITHUB_REPOSITORY/releases/latest" --jq '.tag_name'
  if ($LASTEXITCODE -eq 0 -and $latest -eq $newest) { break }
  Start-Sleep -Seconds 3
}
if ($latest -ne $newest) { throw "The latest Release is $latest, not $newest, the newest version's" }
"""
# its three parts: before the push's guard, a push that finds an installer already out, and making or replacing one
_GUARD = "if (($names -contains 'JobDeskSetup.exe') -and -not $byHand) {\n"
_BEFORE, _REST = _STEP.split(_GUARD)
_KEPT, _REST = _REST.split("} else {\n  Write-Sum $exe\n")
_MADE, _ENDING = _REST.split("}\n$tags = ")


def _publishing() -> dict:
    """The workflow, once its publishing step's script (its last step's) is the one pinned here."""
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    (job,) = flow["jobs"].values()
    run = job["steps"][-1]["run"]
    code = "".join(line for line in run.splitlines(keepends=True) if not line.lstrip().startswith("#"))
    assert code == _STEP
    return flow


def test_a_version_publishes_its_installer_once_with_its_sha256_under_its_own_number():
    """The design of Nov 9 (F1): every version publishes its installer by itself, and the desk's Update button reads
    the Release. So the step that publishes: refuses a version that isn't numbers and dots and a tag that isn't the
    manifest's version (an installer never goes out under another number) before it looks at anything, and writes the
    installer's sha256 beside it exactly as sha256sum would (the Update button checks a download against it)."""
    _publishing()
    # the version and the tag are checked before anything is looked at, and nothing is written before the look
    assert _BEFORE.index("-notmatch") < _BEFORE.index('throw "The tag') < _BEFORE.index("gh ")
    assert _BEFORE.count("gh ") == 2 and "gh release view" in _BEFORE
    # the sum: the digest in lower case, two spaces, the name, one LF and no more (no BOM, no CRLF: -NoNewline, ascii)
    assert '-Value "$sum  JobDeskSetup.exe`n" -NoNewline -Encoding ascii' in _BEFORE
    # this build's sum is written only where it's this build going out, and the Release is made with both files
    assert _STEP.count("Write-Sum $exe") == 1 and 'gh release create $tag $exe "$exe.sha256"' in _MADE


def test_a_push_never_replaces_an_installer_that_is_out_and_gives_it_its_sha256_if_it_has_none():
    """The review of F1: the test let the upload sit above the guard (every push would then replace what's out), and
    a push left an installer published without its sha256 (0.3.252's, or one whose upload was cut short) without one
    for good. So: on a push that finds JobDeskSetup.exe on the Release, nothing can replace a file (no --clobber, no
    upload of this build's installer, no Release made); where the sha256 is missing it is worked out from the
    installer that's out, downloaded, and only it is attached. Every way of replacing or making is in the other
    branch. The second look: the lines that read what the Release holds decide all of that (one of them read wrongly,
    or its failure unchecked, and a push went on to replace a published installer), so the whole script is pinned;
    and a draft isn't out, nor this step's to finish or take away (a person's, its notes written ahead, or one left
    by a stopped run): the step stops and says so."""
    _publishing()
    for part in (_BEFORE, _KEPT, _ENDING):
        assert "--clobber" not in part and "gh release create" not in part and "gh release upload $tag $exe" not in part
        assert "gh release delete" not in part and "$exe" not in part.replace("$exe = '", "")
    assert "gh release upload" not in _BEFORE and "gh release download" not in _BEFORE
    assert _KEPT.index("gh release download $tag --pattern 'JobDeskSetup.exe'") < _KEPT.index("Write-Sum $published") \
        < _KEPT.index('gh release upload $tag "$published.sha256"') and _KEPT.count("gh release upload") == 1
    assert _MADE.count("--clobber") == 1 and "gh release delete" not in _STEP
    assert "if ($there -and $draft -eq 'true') {\n  throw \"There's a draft Release for $tag." in _BEFORE


def test_a_release_is_made_with_its_files_one_run_at_a_time_and_the_latest_stays_the_newest_versions():
    """The review of F1, the rest. The Release was made empty and then filled (the README's link led nowhere in
    between, and for good if the upload failed): it's made with both files, which gh keeps a draft until they're up.
    Two runs for one version (a merge and its tag) could both pass the look and one replace the other's files: runs
    that publish share one concurrency group, where each waits its turn (`queue: max`: without it GitHub keeps only
    the newest one waiting and cancels the rest). A Release made late for an older version becomes the latest, which
    the README's link and the Update button read: the newest version's is marked the latest on every run that
    publishes, a push that found its installer already out among them (it ended before this, so a marking that had
    failed stayed wrong), with this run's own tag among the candidates (the list can trail what was just made), and
    the step ends by checking it. And a gh call that fails says so: PowerShell goes on past a program's failure."""
    flow = _publishing()
    assert flow["concurrency"] == {
        "group": "${{ (github.event_name == 'push' || inputs.release) && 'windows-installer-publish' "
                 "|| format('windows-installer-{0}', github.run_id) }}",
        "cancel-in-progress": False, "queue": "max"}
    # no such flag where gh starts a draft, and no early way out: every path reaches the ending
    assert "--latest" not in _MADE and "exit" not in _STEP
    assert "(@($tags) + $tag) | Where-Object" in _ENDING and "gh release edit $newest --latest\n" in _ENDING
    assert _STEP.rstrip().endswith('if ($latest -ne $newest) { throw "The latest Release is $latest, not $newest, '
                                   'the newest version\'s" }')
    lines = _STEP.splitlines()
    calls = [i for i, line in enumerate(lines) if "gh " in line and not line.lstrip().startswith(("throw", "if "))]
    assert len(calls) == 9, [lines[i] for i in calls]
    for i in calls:
        after = next(x.lstrip() for x in lines[i + 1:] if not x.lstrip().startswith(("--notes", "your PC")))
        # (the two that ask whether something is there are read from their exit code on the next line)
        assert after.startswith(("if ($LASTEXITCODE -ne 0) { throw ", "$there = $LASTEXITCODE -eq 0",
                                 "if ($LASTEXITCODE -eq 0 -and $latest -eq $newest) { break }")), lines[i]


def test_the_readme_offers_the_installer_first_and_says_how_past_smartscreen():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    link = "https://github.com/sebob2491/Jobs/releases/latest/download/JobDeskSetup.exe"
    assert link in readme and "More info" in readme and "Run anyway" in readme
    assert readme.index(link) < readme.index("install.ps1 | iex")


def test_an_update_reopens_the_desk_and_never_runs_the_claude_code_setup_again(iss):
    """The desk's Update button starts the installer with /SILENT ... /RELAUNCH=1 (updates.py): only then does a [Run]
    line start the desk again, exactly as the Start menu's shortcut does; the finish page's box stays skipped in a
    silent install, and the Claude Code setup (a box the installer remembers) never runs in an update."""
    code = iss["code"]
    assert re.search(r"function Relaunch\(\): Boolean;\s*begin\s*"
                     r"Result := ExpandConstant\('\{param:RELAUNCH\|0\}'\) = '1';\s*end;", code), code
    start_menu = iss["Icons"][0]
    relaunch = [e for e in iss["Run"] if e.get("Check") == "Relaunch"]
    assert len(relaunch) == 1
    assert {k: v for k, v in relaunch[0].items() if k not in ("Flags", "Check")} == {
        "Filename": start_menu["Filename"], "Parameters": start_menu["Parameters"],
        "WorkingDir": start_menu["WorkingDir"]}
    assert relaunch[0]["Flags"] == "nowait"
    claude = next(e for e in iss["Run"] if e.get("Tasks") == "claudecode")
    assert claude["Check"] == "not Relaunch"
    finish = next(e for e in iss["Run"] if e.get("Description") == "Open the Job Desk now")
    assert "skipifsilent" in finish["Flags"].split() and "Check" not in finish


def test_an_update_that_didnt_finish_opens_the_desk_still_installed(iss):
    """The desk stops for an update (PrepareToInstall), so an update the person cancelled, or one that failed and
    rolled back, must open the desk still installed, or nothing would: DeinitializeSetup (run as Setup ends, whether it
    finished or not) starts it as the Start menu's shortcut does, only for an update (Relaunch) that didn't finish
    (Installed, set at ssDone, is false), and only when that desk is there."""
    code = iss["code"]
    assert re.search(r"var\s+Problems: String;\s*(\{[^}]*\}\s*)?Installed: Boolean;", code), code
    assert re.search(r"if CurStep = ssDone then\s*begin\s*Installed := True;\s*"
                     r"Log\('Job Desk: the install finished\.'\);\s*end;", code), code
    proc = re.search(r"procedure DeinitializeSetup\(\);(.*?)\nend;", code, re.S)
    assert proc, code
    body = proc.group(1)
    assert re.search(r"begin\s*if Installed or not Relaunch\(\) then\s*Exit;", body), body
    assert re.search(r"try\s*App := ExpandConstant\('\{app\}'\);\s*except\s*Exit;\s*end;", body), body
    assert ("if not FileExists(App + '\\bin\\uvw.exe') or not FileExists(App + "
            "'\\plugin\\server\\src\\job_apply\\desk.py') then\n    Exit;") in body, body
    start = re.search(r"Exec\((.*?)\);", body, re.S)
    assert start, body
    args = [a.strip() for a in re.split(r",\s*", start.group(1).replace("\n", " "))]
    # what it starts: the shortcut's program, arguments and folder, and it isn't waited for
    start_menu = iss["Icons"][0]
    app = "{app}"
    assert args[0] == "App + '\\bin\\uvw.exe'" and start_menu["Filename"] == app + "\\bin\\uvw.exe"
    said = args[1].replace("' + App + '", app)
    assert said == "'" + start_menu["Parameters"] + "'", said
    assert args[2] == "App + '\\plugin\\server'" and start_menu["WorkingDir"] == app + "\\plugin\\server"
    assert args[3:] == ["SW_SHOWNORMAL", "ewNoWait", "Code"]


def test_the_workflow_tries_an_update_started_as_the_desk_starts_one(iss):
    """Whether the installer outlives the processes it stops (the one that started it among them) can only be seen
    on Windows: a step, after the window's and before the uninstall, starts the installer just built with the code's
    own updates.start_installer, from the installed program's Python run through its uvw.exe, and waits up to 3
    minutes for the installer's log to say it finished (the very line JobDeskSetup.iss logs), a new desk on the watch
    file's port, and the starter gone, saying which didn't happen."""
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    (job,) = flow["jobs"].values()
    names = [s.get("name", "") for s in job["steps"]]
    window = names.index("The desk's own window outlives the start that opened it")
    update = names.index("An update started by the desk replaces it and brings it back")
    uninstall = names.index("Uninstall silently; the person's job-apply folder stays")
    assert window + 1 == update == uninstall - 1, names
    run = job["steps"][update]["run"]
    assert "updates.start_installer(Path(sys.argv[1]), Path(sys.argv[2]))" in run
    assert re.search(r'Start-Process -FilePath "\$env:APP\\bin\\uvw\.exe" .*\n\s*"run --frozen --python 3\.13 '
                     r'--project `"\$env:SERVER`" python `"\$script`" `"\$exe`" `"\$log`" `"\$starterId`""\)', run), run
    assert "plugins/job-apply/setup/windows/Output/JobDeskSetup.exe" in run and "$log = Join-Path $updates" in run
    assert "$deadline = (Get-Date).AddMinutes(3)" in run and "desk_watch_8765.json" in run
    finished = re.search(r"Log\('(Job Desk: the install finished\.)'\);", iss["code"])
    assert finished and f"-Pattern '{finished.group(1)}' -SimpleMatch" in run
    assert "$now.pid -ne $before.pid" in run and 'http://127.0.0.1:$($now.port)/' in run
    assert "Get-Process -Id $id" in run and "if ($missing.Count -gt 0) { throw ($missing -join '. ') }" in run
