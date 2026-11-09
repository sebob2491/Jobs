<#
.SYNOPSIS
Build JobDeskSetup.exe, the Job Desk's double-click Windows installer (JobDeskSetup.iss, next to this file).

.DESCRIPTION
Downloads the pinned uv release for Windows (x86_64-pc-windows-msvc), checks its sha256, takes uv.exe and uvw.exe
from it into build\uv, and compiles JobDeskSetup.iss with Inno Setup's compiler (ISCC.exe) at the plugin's version
(.claude-plugin/plugin.json). The installer lands in Output\JobDeskSetup.exe. CI runs it
(.github/workflows/windows-installer.yml); on a Windows computer with Inno Setup 6 it runs the same:

    powershell -ExecutionPolicy Bypass -File plugins\job-apply\setup\windows\build.ps1

.PARAMETER Iscc
Inno Setup's compiler (default: ISCC.exe on the PATH, else Inno Setup 6's usual folders).
#>
param([string]$Iscc = '')

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # (Invoke-WebRequest's progress bar slows a download many times over)

# uv, pinned: a new version is a new line here, its sha256 from the release's own .sha256 file
$UvVersion = '0.13.1'
$UvZip = 'uv-x86_64-pc-windows-msvc.zip'
$UvSha256 = '3b2661a463a5baea7f5076ea2b25ab71d77f36a6c0d809e5350651792f80a672'

$here = $PSScriptRoot
$plugin = (Resolve-Path (Join-Path $here '..\..')).Path
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $plugin '.claude-plugin\plugin.json') | ConvertFrom-Json
$version = [string]$manifest.version
if ($version -notmatch '^\d+(\.\d+){1,3}$') { throw "plugin.json's version ($version) isn't numbers and dots" }

$build = Join-Path $here 'build'
$uvDir = Join-Path $build 'uv'
New-Item -ItemType Directory -Force -Path $uvDir | Out-Null
$zip = Join-Path $build $UvZip
$url = "https://github.com/astral-sh/uv/releases/download/$UvVersion/$UvZip"
Write-Host "Downloading uv $UvVersion ($url)"
Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
$got = (Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash.ToLower()
if ($got -ne $UvSha256) { throw "$UvZip's sha256 is $got, not the pinned ${UvSha256}: not using it" }
$unpacked = Join-Path $build 'uv-unpacked'
if (Test-Path -LiteralPath $unpacked) { Remove-Item -LiteralPath $unpacked -Recurse -Force }
Expand-Archive -LiteralPath $zip -DestinationPath $unpacked
foreach ($exe in @('uv.exe', 'uvw.exe')) {
    $found = Get-ChildItem -LiteralPath $unpacked -Recurse -Filter $exe | Select-Object -First 1
    if (-not $found) { throw "$UvZip has no $exe" }
    Copy-Item -LiteralPath $found.FullName -Destination (Join-Path $uvDir $exe) -Force
}

if (-not $Iscc) {
    $onPath = Get-Command 'ISCC.exe' -ErrorAction SilentlyContinue
    $places = @()
    if ($onPath) { $places += $onPath.Source }
    foreach ($root in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA 'Programs'))) {
        if ($root) { $places += (Join-Path $root 'Inno Setup 6\ISCC.exe') }
    }
    $Iscc = $places | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $Iscc) { throw 'No Inno Setup compiler (ISCC.exe): install Inno Setup 6, or name it with -Iscc' }
}
Write-Host "Compiling JobDeskSetup.iss for job-apply $version with $Iscc"
& $Iscc "/DAppVersion=$version" (Join-Path $here 'JobDeskSetup.iss')
if ($LASTEXITCODE -ne 0) { throw "ISCC failed (exit $LASTEXITCODE)" }
$setup = Join-Path $here 'Output\JobDeskSetup.exe'
if (-not (Test-Path -LiteralPath $setup)) { throw "No $setup after compiling" }
Write-Host "Built $setup"
