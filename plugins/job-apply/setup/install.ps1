<#
.SYNOPSIS
One-step setup for job-apply, the Claude Code plugin that applies to jobs for you (Windows).

.DESCRIPTION
Paste this into PowerShell (Start menu, type PowerShell, press Enter):

    irm https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/install.ps1 | iex

With your resume, or to see the plan without changing anything:

    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/install.ps1))) -Resume "$HOME\Documents\resume.pdf"
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/install.ps1))) -DryRun

Each step looks first and only acts when something is missing, so it's safe to run again (to
update the plugin, say). It never asks for a password: site passwords go in the Job Desk page.
macOS and Linux have their own: install.sh, next to this file.

.PARAMETER Resume
Your resume (PDF or Word), copied into your job-apply folder for setup to read.

.PARAMETER DryRun
Say what it would do, and change nothing.

.PARAMETER Yes
Don't ask; take the default answer (yes) everywhere.
#>
param(
    [string]$Resume = '',
    [switch]$DryRun,
    [switch]$Yes
)

# (Everything is in functions, called at the end: pasted through iex, the script runs in the
# person's own PowerShell window, which an `exit` would close.)

$script:JADryRun = [bool]$DryRun
$script:JAYes = [bool]$Yes
$script:JAResume = $Resume
$script:JAFailed = $false
$script:JAMarketRepo = 'sebob2491/Jobs'
$script:JAMarket = 'sebob-jobs'
$script:JAPlugin = 'job-apply@sebob-jobs'
$script:JAResumeTypes = @('.pdf', '.docx', '.doc', '.rtf', '.odt', '.txt')
$script:JAUserHome = $null  # (Get-JAUserHome, when Install-JobApply starts)
$script:JAOnWindows = ($env:OS -eq 'Windows_NT')

function Write-JAStep([string]$Text) { Write-Host ''; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-JAOk([string]$Text) { Write-Host "  OK: $Text" -ForegroundColor Green }
function Write-JASay([string]$Text) { Write-Host "  $Text" }
function Write-JAProblem([string]$Text) { Write-Host "  ! $Text" -ForegroundColor Yellow; $script:JAFailed = $true }

function Join-JAPath {
    # Join-Path for several parts; $null when a part is missing (an unset variable)
    $path = $null
    foreach ($part in $args) {
        if (-not $part) { return $null }
        if ($null -eq $path) { $path = [string]$part } else { $path = Join-Path $path $part }
    }
    return $path
}

function Test-JAInteractive {
    if (-not [Environment]::UserInteractive) { return $false }
    try { if ([Console]::IsInputRedirected) { return $false } } catch { }
    foreach ($a in [Environment]::GetCommandLineArgs()) { if ($a -like '-NonI*') { return $false } }
    return $true
}

function Confirm-JA([string]$Question) {
    # Yes unless the person says no (with --Yes, or no keyboard to ask on: yes)
    if ($script:JAYes -or -not (Test-JAInteractive)) { Write-JASay "$Question Yes."; return $true }
    $reply = Read-Host "  $Question [Y/n]"
    return -not ($reply -match '^\s*[nN]')
}

function Get-JAShown([string]$Exe, [string[]]$Arguments) {
    # A command as a person would type it: the program's own name, not its whole path
    $words = @([IO.Path]::GetFileNameWithoutExtension($Exe))
    foreach ($a in $Arguments) { if ($a -match '\s') { $words += "`"$a`"" } else { $words += $a } }
    return ($words -join ' ')
}

function Invoke-JA([string]$What, [string]$Exe, [string[]]$Arguments) {
    # Run a command (saying what it's for first), or only say it in a dry run. True when it worked.
    $shown = Get-JAShown $Exe $Arguments
    if ($script:JADryRun) { Write-JASay "Would run: $shown   ($What)"; return $true }
    Write-JASay "${What}: $shown"
    & $Exe @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-JAProblem "That didn't work (exit code $LASTEXITCODE)."; return $false }
    return $true
}

function Get-JAUserHome {
    # The person's own folder: USERPROFILE, else what Windows (or .NET elsewhere) says it is,
    # else HOME. $null only when none of them says (the caller stops rather than use '').
    if ($env:USERPROFILE) { return [string]$env:USERPROFILE }
    try {
        $profileDir = [Environment]::GetFolderPath('UserProfile')
        if ($profileDir) { return [string]$profileDir }
    } catch { }
    foreach ($candidate in @($HOME, $env:HOME)) { if ($candidate) { return [string]$candidate } }
    return $null
}

function Get-JAOutput([string]$Exe, [string[]]$Arguments) {
    # What a command prints, as one string: '' (never $null) when it prints nothing or can't run
    $text = $null
    try { $text = (& $Exe @Arguments 2>$null) | Out-String } catch { }
    if ($null -eq $text) { return '' }
    return [string]$text
}

function Get-JAVersion([string]$Exe, [int]$Word) {
    # One word of `<program> --version` ('2.1.0 (Claude Code)': word 0; 'uv 0.11.32 (...)': word 1)
    $words = @((Get-JAOutput $Exe @('--version')).Trim() -split '\s+')
    if ($words.Count -gt $Word -and $words[$Word]) { return [string]$words[$Word] }
    return ''
}

function Find-JACommand([string]$Name) {
    $found = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Path }
    foreach ($file in @("$Name.exe", $Name)) {
        $p = Join-JAPath $script:JAUserHome '.local' 'bin' $file
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { return $p }
    }
    return $null
}

function Update-JAPath {
    # Installers add their folders to the saved PATH; this window still has the old one
    $dirs = @()
    foreach ($scope in @('Machine', 'User')) {
        $saved = $null
        try { $saved = [Environment]::GetEnvironmentVariable('Path', $scope) } catch { }
        if ($saved) { $dirs += ($saved -split ';') }
    }
    $dirs += (Join-JAPath $script:JAUserHome '.local' 'bin')
    $current = @($env:PATH -split [IO.Path]::PathSeparator)
    foreach ($d in $dirs) {
        if ($d -and ($current -notcontains $d) -and (Test-Path -LiteralPath $d)) {
            $env:PATH = $env:PATH + [IO.Path]::PathSeparator + $d
            $current += $d
        }
    }
}

function Get-JAPowerShell {
    # This PowerShell, to run an official installer in a window of its own (an `exit` in it
    # mustn't end this script)
    $exe = $null
    try { $exe = (Get-Process -Id $PID).Path } catch { }
    if (-not $exe -or ((Split-Path $exe -Leaf) -notmatch '^(powershell|pwsh)(\.exe)?$')) { $exe = 'powershell.exe' }
    return $exe
}

function Invoke-JAOfficialInstaller([string]$What, [string]$Url) {
    $command = "[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072; irm $Url | iex"
    Write-JASay "${What}: irm $Url | iex"
    & (Get-JAPowerShell) -NoProfile -ExecutionPolicy Bypass -Command $command | Out-Host
    Update-JAPath
}

function Test-JAInAppData([string]$Path) {
    if (-not $Path) { return $false }
    $text = ($Path -replace '/', '\').TrimEnd('\').ToLower() + '\'
    if ($text.Contains('\appdata\')) { return $true }
    foreach ($root in @($env:APPDATA, $env:LOCALAPPDATA)) {
        if ($root -and $text.StartsWith(($root -replace '/', '\').TrimEnd('\').ToLower() + '\')) { return $true }
    }
    return $false
}

# ---------------------------------------------------------------- 1. Git

function Install-JAGit {
    Write-JAStep '1 of 9: Git (Claude Code uses it to fetch the plugin)'
    $script:JAGit = Find-JACommand 'git'
    if (-not $script:JAGit) {
        $p = Join-JAPath $env:ProgramFiles 'Git' 'cmd' 'git.exe'
        if ($p -and (Test-Path -LiteralPath $p)) { $script:JAGit = $p }
    }
    if ($script:JAGit) { Write-JAOk 'Git is installed.'; return }
    Write-JASay 'Git for Windows is not installed.'
    $winget = Find-JACommand 'winget'
    $wingetArgs = @('install', '--id', 'Git.Git', '-e', '--source', 'winget', '--accept-package-agreements', '--accept-source-agreements')
    if ($script:JADryRun) {
        Write-JASay 'Would ask to install it (the answer is yes unless you say no), with: winget install --id Git.Git -e --source winget'
        return
    }
    if ($winget -and (Confirm-JA 'Install Git for Windows now?')) {
        if (Invoke-JA 'Installing Git for Windows' $winget $wingetArgs) {
            Update-JAPath
            $script:JAGit = Find-JACommand 'git'
            $p = Join-JAPath $env:ProgramFiles 'Git' 'cmd' 'git.exe'
            if (-not $script:JAGit -and $p -and (Test-Path -LiteralPath $p)) {
                $script:JAGit = $p
                $env:PATH = $env:PATH + ';' + (Split-Path $p)
            }
        }
    }
    if ($script:JAGit) { Write-JAOk 'Git is installed.'; return }
    Write-JAProblem 'Git is not installed yet. Get it from https://git-scm.com/downloads/win, then run this again.'
}

# ---------------------------------------------------------------- 2. Claude Code

function Install-JAClaude {
    Write-JAStep '2 of 9: Claude Code'
    $script:JAClaude = Find-JACommand 'claude'
    if ($script:JAClaude) {
        $version = Get-JAVersion $script:JAClaude 0
        if ($version) { Write-JAOk "Claude Code is installed (version $version)." } else { Write-JAOk 'Claude Code is installed.' }
        return
    }
    Write-JASay 'Claude Code is not installed. Its official installer is: irm https://claude.ai/install.ps1 | iex'
    if ($script:JADryRun) { Write-JASay 'Would ask to run it (the answer is yes unless you say no).'; return }
    if (Confirm-JA 'Install Claude Code now?') {
        Invoke-JAOfficialInstaller 'Installing Claude Code' 'https://claude.ai/install.ps1'
        $script:JAClaude = Find-JACommand 'claude'
    }
    if ($script:JAClaude) { Write-JAOk 'Claude Code is installed.'; return }
    Write-JAProblem 'Claude Code is not installed. Install it (https://claude.com/claude-code), then run this again.'
}

# ---------------------------------------------------------------- 3. the plugin

function Get-JAPluginInfo {
    # What Claude Code says about the plugin: $null when it isn't installed
    if (-not $script:JAClaude) { return $null }
    $text = Get-JAOutput $script:JAClaude @('plugin', 'list', '--json')
    if (-not $text -or -not $text.Trim()) { return $null }
    try { $items = $text | ConvertFrom-Json -ErrorAction Stop } catch { return $null }
    foreach ($p in $items) { if ($p.id -eq $script:JAPlugin) { return $p } }
    return $null
}

function Install-JAPlugin {
    Write-JAStep '3 of 9: The job-apply plugin'
    if (-not $script:JAClaude) {
        if ($script:JADryRun) {
            Write-JASay "Would run, once Claude Code is installed: claude plugin marketplace add $($script:JAMarketRepo)"
            Write-JASay "Would run: claude plugin install $($script:JAPlugin)"
        } else { Write-JAProblem 'Skipped: it needs Claude Code (above).' }
        return
    }
    if (-not $script:JAGit -and -not $script:JADryRun) { Write-JAProblem 'Skipped: it needs Git (above).'; return }
    $markets = Get-JAOutput $script:JAClaude @('plugin', 'marketplace', 'list', '--json')
    if ($markets -match ('"name":\s*"' + [regex]::Escape($script:JAMarket) + '"')) {
        $null = Invoke-JA "Getting the newest list of the marketplace's plugins" $script:JAClaude @('plugin', 'marketplace', 'update', $script:JAMarket)
    } else {
        $null = Invoke-JA "Adding the plugin's marketplace ($($script:JAMarket)) to Claude Code" $script:JAClaude @('plugin', 'marketplace', 'add', $script:JAMarketRepo)
    }
    $info = Get-JAPluginInfo
    if ($info) {
        $null = Invoke-JA 'Updating the plugin to its newest version' $script:JAClaude @('plugin', 'update', $script:JAPlugin)
        if ($info.enabled -eq $false) {
            $null = Invoke-JA 'Turning the plugin back on' $script:JAClaude @('plugin', 'enable', $script:JAPlugin)
        }
    } elseif (-not (Invoke-JA 'Installing the plugin' $script:JAClaude @('plugin', 'install', $script:JAPlugin))) {
        return
    }
    if (-not $script:JADryRun) { Write-JAOk 'The plugin is installed.' }
}

# ---------------------------------------------------------------- 4. uv

function Install-JAUv {
    Write-JAStep "4 of 9: uv (it runs the plugin's Python server)"
    $script:JAUv = Find-JACommand 'uv'
    if ($script:JAUv) {
        $version = Get-JAVersion $script:JAUv 1
        if ($version) { Write-JAOk "uv is installed (version $version)." } else { Write-JAOk 'uv is installed.' }
        return
    }
    if ($script:JADryRun) {
        Write-JASay "Would install it with Astral's official installer: irm https://astral.sh/uv/install.ps1 | iex"
        return
    }
    Invoke-JAOfficialInstaller "Installing uv with Astral's official installer" 'https://astral.sh/uv/install.ps1'
    $script:JAUv = Find-JACommand 'uv'
    if ($script:JAUv) { Write-JAOk 'uv is installed.'; return }
    Write-JAProblem "uv didn't install. See https://docs.astral.sh/uv/getting-started/installation/ and run this again."
}

# ---------------------------------------------------------------- 5. where uv keeps Python

function Set-JAPythonDir {
    Write-JAStep "5 of 9: A place for the plugin's Python outside AppData"
    # The Claude app for Windows keeps its own copy of what's written under AppData, so a Python
    # that uv puts there can be missing for it. UV_PYTHON_INSTALL_DIR outside AppData avoids that.
    $wanted = Join-JAPath $script:JAUserHome '.uv-python'
    if (-not $wanted) { Write-JAProblem "Couldn't tell where your user folder is, so UV_PYTHON_INSTALL_DIR wasn't set."; return }
    $saved = $null
    try { $saved = [Environment]::GetEnvironmentVariable('UV_PYTHON_INSTALL_DIR', 'User') } catch { }
    $value = if ($env:UV_PYTHON_INSTALL_DIR) { $env:UV_PYTHON_INSTALL_DIR } else { $saved }
    if ($value -and -not (Test-JAInAppData $value)) {
        $env:UV_PYTHON_INSTALL_DIR = $value
        Write-JAOk "uv keeps the plugin's Python in $value"
        return
    }
    if ($value) {
        Write-JASay "UV_PYTHON_INSTALL_DIR is $value, inside AppData, where the Claude app can't always find it."
        if (-not $script:JADryRun -and -not (Confirm-JA "Change it to $wanted?")) { return }
    }
    if ($script:JADryRun) {
        Write-JASay "Would set UV_PYTHON_INSTALL_DIR to $wanted for you (so uv keeps Python outside AppData)."
        return
    }
    try {
        New-Item -ItemType Directory -Force -Path $wanted | Out-Null
        [Environment]::SetEnvironmentVariable('UV_PYTHON_INSTALL_DIR', $wanted, 'User')
        $env:UV_PYTHON_INSTALL_DIR = $wanted
        Write-JAOk "Set UV_PYTHON_INSTALL_DIR to $wanted, so uv keeps the plugin's Python outside AppData."
    } catch {
        Write-JAProblem "Couldn't set UV_PYTHON_INSTALL_DIR: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- 6. the plugin's server

function Find-JAServer {
    # Where Claude Code put the plugin: what it says, its list of installed plugins, its cache
    $claudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-JAPath $script:JAUserHome '.claude' }
    $places = @()
    $info = Get-JAPluginInfo
    if ($info -and $info.installPath) { $places += [string]$info.installPath }
    $list = Join-JAPath $claudeDir 'plugins' 'installed_plugins.json'
    if ($list -and (Test-Path -LiteralPath $list)) {
        try {
            $data = Get-Content -Raw -Encoding UTF8 -LiteralPath $list | ConvertFrom-Json -ErrorAction Stop
            foreach ($entry in @($data.plugins.($script:JAPlugin))) { if ($entry.installPath) { $places += [string]$entry.installPath } }
        } catch { }
    }
    $cache = Join-JAPath $claudeDir 'plugins' 'cache' $script:JAMarket 'job-apply'
    if ($cache -and (Test-Path -LiteralPath $cache)) {
        $versions = Get-ChildItem -LiteralPath $cache -Directory | Where-Object { $_.Name -match '^\d+(\.\d+)+$' } |
            Sort-Object { [version]$_.Name } -Descending
        foreach ($v in $versions) { $places += $v.FullName }
    }
    foreach ($place in $places) {
        $server = Join-JAPath $place 'server'
        if ($server -and (Test-Path -LiteralPath (Join-Path $server 'pyproject.toml'))) { return $server }
    }
    return $null
}

function Initialize-JAServer {
    Write-JAStep "6 of 9: The plugin's Python packages (so its first start is quick)"
    $script:JAServer = Find-JAServer
    if (-not $script:JAServer) {
        if ($script:JADryRun) { Write-JASay "Would find the plugin's folder once it's installed, and run: uv sync --frozen there." }
        else { Write-JAProblem "Couldn't find where Claude Code put the plugin. Run: claude plugin list, to check it's installed." }
        return
    }
    Write-JASay "The plugin is in: $($script:JAServer)"
    if (-not $script:JAUv) {
        if ($script:JADryRun) { Write-JASay "Would run, once uv is installed: uv sync --frozen --project `"$($script:JAServer)`"" }
        else { Write-JAProblem 'Skipped: it needs uv (above).' }
        return
    }
    # A Python environment made earlier with uv's Python inside AppData is made again outside it
    $cfg = Join-JAPath $script:JAServer '.venv' 'pyvenv.cfg'
    if ($script:JAOnWindows -and $cfg -and (Test-Path -LiteralPath $cfg)) {
        $line = Select-String -LiteralPath $cfg -Pattern '^\s*home\s*=\s*(.+)$' | Select-Object -First 1
        $pythonHome = if ($line) { $line.Matches[0].Groups[1].Value.Trim() } else { '' }
        $movedOut = $env:UV_PYTHON_INSTALL_DIR -and -not (Test-JAInAppData $env:UV_PYTHON_INSTALL_DIR)
        if ($movedOut -and (Test-JAInAppData $pythonHome) -and ($pythonHome -match '\\uv\\python\\')) {
            if ($script:JADryRun) { Write-JASay "Would make the plugin's Python environment again, outside AppData." }
            else {
                Write-JASay "Making the plugin's Python environment again, outside AppData."
                Remove-Item -LiteralPath (Join-JAPath $script:JAServer '.venv') -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
    }
    $null = Invoke-JA "Installing the plugin's Python packages" $script:JAUv @('sync', '--frozen', '--project', $script:JAServer)
}

# ---------------------------------------------------------------- 7. a browser

function Find-JABrowser {
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        $p = Join-JAPath $root 'Google' 'Chrome' 'Application' 'chrome.exe'
        if ($p -and (Test-Path -LiteralPath $p)) { return 'Google Chrome' }
    }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        $p = Join-JAPath $root 'Microsoft' 'Edge' 'Application' 'msedge.exe'
        if ($p -and (Test-Path -LiteralPath $p)) { return 'Microsoft Edge' }
    }
    $browsers = if ($env:PLAYWRIGHT_BROWSERS_PATH) { $env:PLAYWRIGHT_BROWSERS_PATH } else { Join-JAPath $env:LOCALAPPDATA 'ms-playwright' }
    if ($browsers -and (Test-Path -LiteralPath $browsers)) {
        if (Get-ChildItem -LiteralPath $browsers -Directory -Filter 'chromium-*' -ErrorAction SilentlyContinue) {
            return "the plugin's own Chromium"
        }
    }
    return $null
}

function Install-JABrowser {
    Write-JAStep '7 of 9: A browser for the plugin to fill applications in'
    $found = Find-JABrowser
    if ($found) { Write-JAOk "Found $found."; return }
    Write-JASay 'No Google Chrome or Microsoft Edge here, so the plugin gets its own Chromium (about 150 MB).'
    if (-not $script:JAUv -or -not $script:JAServer) {
        if ($script:JADryRun) { Write-JASay "Would run, in the plugin's folder: uv run --frozen playwright install chromium" }
        else { Write-JAProblem 'Skipped: it needs uv and the plugin (above). Or install Google Chrome.' }
        return
    }
    $null = Invoke-JA 'Downloading Chromium' $script:JAUv @('run', '--frozen', '--project', $script:JAServer, 'playwright', 'install', 'chromium')
}

# ---------------------------------------------------------------- 8. your folder and resume

function Get-JAExistingResume {
    foreach ($t in $script:JAResumeTypes) {
        $p = Join-JAPath $script:JAHome "resume$t"
        if ($p -and (Test-Path -LiteralPath $p -PathType Leaf)) { return $p }
    }
    return $null
}

function Copy-JAResume([string]$Source) {
    $src = $Source.Trim().Trim('"').Trim("'")
    if ($src -eq '~') { $src = $script:JAUserHome }
    elseif ($src.StartsWith('~\') -or $src.StartsWith('~/')) { $src = Join-Path $script:JAUserHome $src.Substring(2) }
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
        Write-JAProblem "There's no file at $src, so no resume was copied. Setup can ask for it instead."
        return
    }
    $ext = [IO.Path]::GetExtension($src).ToLower()
    if ($script:JAResumeTypes -notcontains $ext) {
        Write-JAProblem "$src isn't a PDF or Word file, so it wasn't copied. Setup can ask for it instead."
        return
    }
    $dest = Join-Path $script:JAHome "resume$ext"
    if ((Test-Path -LiteralPath $dest) -and ((Get-FileHash -LiteralPath $src).Hash -eq (Get-FileHash -LiteralPath $dest).Hash)) {
        Write-JAOk "Your resume is already in your job-apply folder (resume$ext)."
        return
    }
    if ($script:JADryRun) {
        Write-JASay "Would copy your resume to $dest (keeping any resume already there under another name)."
        return
    }
    try {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        foreach ($t in $script:JAResumeTypes) {
            # an older resume stays, under another name, so setup reads the new one
            $old = Join-Path $script:JAHome "resume$t"
            if (Test-Path -LiteralPath $old) { Move-Item -LiteralPath $old -Destination (Join-Path $script:JAHome "resume-before-$stamp$t") }
        }
        Copy-Item -LiteralPath $src -Destination $dest -ErrorAction Stop
        Write-JAOk "Copied your resume to $dest. Setup reads it from there."
    } catch {
        Write-JAProblem "Couldn't copy your resume: $($_.Exception.Message)"
    }
}

function Initialize-JAHome {
    Write-JAStep '8 of 9: Your job-apply folder (your profile and resume stay on this computer, in it)'
    $script:JAHome = if ($env:JOB_APPLY_HOME) { [string]$env:JOB_APPLY_HOME } else { Join-JAPath $script:JAUserHome '.job-apply' }
    if (-not $script:JAHome) { Write-JAProblem "Couldn't tell where your user folder is, so the job-apply folder wasn't made."; return }
    if ($script:JAHome.StartsWith('~')) { $script:JAHome = $script:JAUserHome + $script:JAHome.Substring(1) }
    if (Test-Path -LiteralPath $script:JAHome -PathType Container) {
        Write-JAOk "It's there: $($script:JAHome)"
    } elseif ($script:JADryRun) {
        Write-JASay "Would make it: $($script:JAHome)"
    } else {
        try {
            New-Item -ItemType Directory -Force -Path $script:JAHome -ErrorAction Stop | Out-Null
            Write-JAOk "Made it: $($script:JAHome)"
        } catch {
            Write-JAProblem "Couldn't make $($script:JAHome): $($_.Exception.Message)"
            return
        }
    }
    $resume = $script:JAResume
    if (-not $resume -and -not (Get-JAExistingResume) -and -not $script:JADryRun -and -not $script:JAYes -and (Test-JAInteractive)) {
        Write-JASay 'Your resume (optional): drag the file into this window, or type its path, then press Enter.'
        $resume = Read-Host '  Press Enter alone to skip'
    }
    $existing = Get-JAExistingResume
    if ($resume -and $resume.Trim()) { Copy-JAResume $resume }
    elseif ($existing) { Write-JAOk "Your resume is there ($(Split-Path $existing -Leaf))." }
    else { Write-JASay 'No resume yet: Claude asks for it during setup.' }
}

# ---------------------------------------------------------------- 9. the doctor

function Invoke-JADoctor {
    Write-JAStep '9 of 9: Checking everything (the plugin''s doctor)'
    if (-not $script:JAUv -or -not $script:JAServer) {
        Write-JASay 'It runs once uv and the plugin are in place (run this installer again then).'
        return
    }
    $project = Join-Path $script:JAServer 'pyproject.toml'
    if (-not (Select-String -LiteralPath $project -Pattern 'job-apply-doctor' -Quiet)) {
        Write-JASay 'This version of the plugin has no doctor yet: it comes with the next update.'
        return
    }
    if ($script:JADryRun) {
        Write-JASay "Would run: uv run --frozen --project `"$($script:JAServer)`" job-apply-doctor --launch"
        return
    }
    Write-Host ''
    $oldEncoding = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        & $script:JAUv run --quiet --frozen --project $script:JAServer job-apply-doctor --launch | Out-Host
        $status = $LASTEXITCODE
    } finally {
        $env:PYTHONIOENCODING = $oldEncoding
    }
    Write-Host ''
    if ($status -ne 0) {
        Write-JASay 'Your profile and resume are what setup does next. For anything else the doctor'
        Write-JASay 'marked, the line under it says what to do.'
    }
}

function Install-JobApply {
    Write-Host 'Setting up job-apply, the Claude Code plugin that applies to jobs for you.'
    if ($script:JADryRun) { Write-Host 'Dry run: this says what it would do, and changes nothing.' }
    if (-not $script:JAOnWindows) {
        if (-not $script:JADryRun) {
            Write-Host 'This installer is for Windows. On a Mac or Linux, run this in Terminal instead:'
            Write-Host '  curl -fsSL https://raw.githubusercontent.com/sebob2491/Jobs/main/plugins/job-apply/setup/install.sh | bash'
            $script:JAFailed = $true
            return
        }
        Write-Host '(Not Windows: this shows the plan Windows would get.)'
    }
    $script:JAUserHome = Get-JAUserHome
    if (-not $script:JAUserHome) {
        Write-JAProblem "Couldn't tell where your user folder is (USERPROFILE isn't set). Open a new PowerShell window and try again."
        return
    }
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } catch { }
    $oldOutput = $null
    try { $oldOutput = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
    try {
        Update-JAPath
        Install-JAGit
        Install-JAClaude
        Install-JAPlugin
        Install-JAUv
        Set-JAPythonDir
        Initialize-JAServer
        Install-JABrowser
        Initialize-JAHome
        Invoke-JADoctor
    } catch {
        Write-JAProblem "Something went wrong: $($_.Exception.Message)"
    } finally {
        if ($oldOutput) { try { [Console]::OutputEncoding = $oldOutput } catch { } }
    }
    Write-Host ''
    if ($script:JADryRun) {
        Write-Host "That's the plan. Run it without -DryRun to do it."
        if ($script:JAFailed) { Write-Host "(Fix what's marked ! above first.)" }
        return
    }
    if ($script:JAFailed) {
        Write-Host "Some steps didn't finish (marked ! above). Fix those, then run this again: it picks up where it left off." -ForegroundColor Yellow
        return
    }
    Write-Host 'All set. Next: open the Claude app (or type claude in a new PowerShell window) and say "set up job-apply".' -ForegroundColor Green
    Write-Host "Claude reads your resume and asks the few questions it can't answer. If the Claude app was open, quit and reopen it first."
}

Install-JobApply
# Run as a file (powershell -File install.ps1), say how it went; pasted, leave the window open
if ($PSCommandPath -and $MyInvocation.InvocationName -ne '.') { if ($script:JAFailed) { exit 1 } else { exit 0 } }
