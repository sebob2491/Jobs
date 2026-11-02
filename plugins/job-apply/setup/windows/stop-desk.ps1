<#
.SYNOPSIS
Stop a Job Desk running from this install (JobDeskSetup.exe runs it, hidden, before an update and before
uninstalling).

.DESCRIPTION
The desk's Python, the uv that started it and the browser driver hold files in the install folder; an update
couldn't replace them and the uninstaller couldn't remove them. Only those programs are stopped: each by name
(python, uv, the desk's own commands, Playwright's node), and only when it runs from the install folder or names
it on its command line. A desk Claude Code's plugin runs (from its own folder) is left alone, and so is everything
else on the computer. The person's job-apply folder (~/.job-apply) isn't touched.
#>
param([Parameter(Mandatory = $true)][string]$AppDir)

$dir = $AppDir.TrimEnd('\', '/') + '\'
if ($dir.Length -lt 4) { exit 0 }  # (never a drive's root)
$names = @('python.exe', 'pythonw.exe', 'uv.exe', 'uvw.exe', 'node.exe', 'job-apply-desk.exe', 'job-apply-doctor.exe',
    'job-apply-mcp.exe', 'job-apply-wait-mail.exe')
$stopped = 0
foreach ($p in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
    if ($names -notcontains ([string]$p.Name).ToLower() -or $p.ProcessId -eq $PID) { continue }
    $exe = [string]$p.ExecutablePath
    $line = [string]$p.CommandLine
    $ours = $exe.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) -or
        ($line.IndexOf($dir, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    if ($ours) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $stopped++ } catch { }
    }
}
if ($stopped -gt 0) { Start-Sleep -Seconds 1 }  # (Windows lets go of their files a moment after)
exit 0
