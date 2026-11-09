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


def _publishing():
    """The workflow, and its last step's script in the three parts it runs in: `before` the look at the Release,
    `kept` (a push that finds an installer already out: it ends there), and `publish` (everything after)."""
    flow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    (job,) = flow["jobs"].values()
    run = job["steps"][-1]["run"]
    guard = "if (($names -contains 'JobDeskSetup.exe') -and -not $byHand) {\n"
    ended = "  $global:LASTEXITCODE = 0\n  exit 0\n}\n"  # (the step's own indentation is gone once YAML has read it)
    assert run.count(guard) == 1 and run.count(ended) == 1 and run.count("exit 0") == 1, run
    before, rest = run.split(guard)
    kept, publish = rest.split(ended)
    return flow, before, kept, publish


def test_a_version_publishes_its_installer_once_with_its_sha256_under_its_own_number():
    """The design of Nov 9 (F1): every version publishes its installer by itself, and the desk's Update button reads
    the Release. So the step that publishes: refuses a version that isn't numbers and dots and a tag that isn't the
    manifest's version (an installer never goes out under another number) before it looks at anything, and writes the
    installer's sha256 beside it exactly as sha256sum would (the Update button checks a download against it)."""
    _flow, before, _kept, publish = _publishing()
    assert "if ($version -notmatch '^\\d+(\\.\\d+)+$') { throw" in before
    assert '$tag = "job-apply-v$version"' in before
    assert "if ($env:GITHUB_REF_TYPE -eq 'tag' -and $env:GITHUB_REF_NAME -ne $tag) {\n  throw \"The tag" in before
    assert before.index('throw "The tag') < before.index("gh release view")  # (before anything is looked at)
    assert "$byHand = $env:GITHUB_EVENT_NAME -ne 'push'" in before
    # the digest in lower case, two spaces, the name, one LF and no more (no BOM, no CRLF: -NoNewline, ascii)
    assert ("$sum = (Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLower()\n"
            '  Set-Content -LiteralPath "$file.sha256" -Value "$sum  JobDeskSetup.exe`n" -NoNewline -Encoding ascii\n'
            ) in before
    assert publish.lstrip().startswith("Write-Sum $exe\n")  # (this build's sum, only once it's this build going out)
    assert 'gh release create $tag $exe "$exe.sha256" --target $env:GITHUB_SHA' in publish
    assert 'gh release upload $tag $exe "$exe.sha256" --clobber' in publish


def test_a_push_never_replaces_an_installer_that_is_out_and_gives_it_its_sha256_if_it_has_none():
    """The review of F1: the test let the upload sit above the guard (every push would then replace what's out), and
    a push left an installer published without its sha256 (0.3.252's, or one whose upload was cut short) without one
    for good. So: a push that finds JobDeskSetup.exe on the Release ends there, and nothing in that part can replace
    a file (no --clobber, no upload of this build's installer, no Release made); where the sha256 is missing it is
    worked out from the installer that's out, downloaded, and only it is attached. Every way of replacing or making
    comes after that end."""
    _flow, before, kept, publish = _publishing()
    for part in (before, kept):
        assert "--clobber" not in part and "gh release create" not in part and "gh release upload $tag $exe" not in part
    assert "gh release upload" not in before and "gh release download" not in before
    assert "if ($names -contains 'JobDeskSetup.exe.sha256') {" in kept
    download = kept.index("gh release download $tag --pattern 'JobDeskSetup.exe' --dir $was")
    summed = kept.index("Write-Sum $published")
    attached = kept.index('gh release upload $tag "$published.sha256"')
    assert download < kept.index("$published = Join-Path $was 'JobDeskSetup.exe'") < summed < attached
    assert kept.count("gh release upload") == 1 and "$exe" not in kept  # (nothing of this build goes up)
    assert publish.count("--clobber") == 1  # (only where no installer is out, or by hand)


def test_a_release_is_made_with_its_files_one_run_at_a_time_and_an_older_one_is_not_the_latest():
    """The review of F1, the rest. The Release was made empty and then filled (the README's link led nowhere in
    between, and for good if the upload failed): it's made with both files, which gh keeps a draft until they're up.
    Two runs for one version (a merge and its tag) could both pass the look and one replace the other's files: runs
    that publish share one concurrency group, and none is cancelled. A Release made late for an older version would
    become the latest, which the README's link and the Update button read: it's told not to be."""
    flow, _before, _kept, publish = _publishing()
    assert flow["concurrency"] == {
        "group": "${{ (github.event_name == 'push' || inputs.release) && 'windows-installer-publish' "
                 "|| format('windows-installer-{0}', github.run_id) }}",
        "cancel-in-progress": False}
    made = publish.index('gh release create $tag $exe "$exe.sha256" --target $env:GITHUB_SHA --title '
                         '"job-apply $version" $latest `\n')
    assert publish.count("gh release create") == 1 and "if (-not $there) {" in publish[:made]
    listed = publish.index("$tags = @(gh api \"repos/$env:GITHUB_REPOSITORY/releases?per_page=100\" --jq "
                           "'.[].tag_name')")
    older = publish.index("$later = @($tags | Where-Object { $_ -match '^job-apply-v(\\d+(\\.\\d+)+)$' -and "
                          "[version]$Matches[1] -gt [version]$version })")
    chosen = publish.index("$latest = if ($later.Count -gt 0) { '--latest=false' } else { '--latest' }")
    assert listed < older < chosen < made
    # every gh call that changes or reads something says so when it fails (PowerShell goes on past a program's
    # failure by itself)
    lines = publish.splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("gh release create", "gh release upload", "$tags = @(gh api")):
            after = next(x for x in lines[i + 1:] if not x.lstrip().startswith("--notes"))
            assert after.lstrip().startswith("if ($LASTEXITCODE -ne 0) { throw "), line


def test_the_readme_offers_the_installer_first_and_says_how_past_smartscreen():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    link = "https://github.com/sebob2491/Jobs/releases/latest/download/JobDeskSetup.exe"
    assert link in readme and "More info" in readme and "Run anyway" in readme
    assert readme.index(link) < readme.index("install.ps1 | iex")
