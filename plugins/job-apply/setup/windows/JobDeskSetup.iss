; JobDeskSetup.exe: the Job Desk for Windows in one double-click (the owner's choice, Nov 2). No Claude Code needed:
; the "Job Desk" shortcut opens the Job Desk page (job-apply-desk, http://127.0.0.1:8765). Claude Code, for the chat
; features, is a box to tick (off unless ticked): it runs the one-step PowerShell installer (setup\install.ps1).
;
; Built by build.ps1 (next to this file), which fetches the pinned uv into build\uv first, and by CI
; (.github/workflows/windows-installer.yml). Inno Setup 6.
;
; Where it goes: %LOCALAPPDATA%\Programs\JobDesk ({autopf} for a per-user install), Windows' own place for a program
; one person installs without an administrator; it's the person's alone, and out of the way in their home folder.
; The Claude desktop app keeps its own copy of what its processes write under AppData, but nothing here runs as
; one of them: the shortcut, the installer and uv are started from the Start menu or Explorer. The one thing the
; Claude app may also use, uv's Python, goes outside AppData anyway (UV_PYTHON_INSTALL_DIR, as install.ps1 sets it).
;
; What stays on uninstall: the person's job-apply folder (~/.job-apply: profile, resume, tracker, passwords), uv's
; Python (~\.uv-python, shared with the Claude Code plugin) and the UV_PYTHON_INSTALL_DIR setting. Only this
; program's own folders go.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
; the plugin (plugins/job-apply), and what of it isn't carried: its tests (their fixtures among them), caches and
; the Python environment of a checkout, and this folder's build output
; the Python the desk runs on: uv would otherwise take its newest (3.15 by Nov 2026), for which uv.lock has no
; pydantic-core wheel to install, and a build of it needs Rust
#define Python "3.13"
#define PluginDir "..\.."
#define PluginExcludes "\server\tests,\server\.venv,\server\.coverage,__pycache__,*.pyc,.pytest_cache,.mypy_cache,.ruff_cache,\setup\windows\build,\setup\windows\Output"

[Setup]
AppId={{F086D54A-D345-4532-B3E8-10892A1A6DE5}
AppName=Job Desk
AppVersion={#AppVersion}
AppVerName=Job Desk {#AppVersion}
AppPublisher=job-apply
AppPublisherURL=https://github.com/sebob2491/Jobs
AppSupportURL=https://github.com/sebob2491/Jobs/issues
; per-user: no administrator, nothing outside this person's own folders
PrivilegesRequired=lowest
DefaultDirName={autopf}\JobDesk
DisableDirPage=yes
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=JobDeskSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; (the installer stops a desk running from here itself: stop-desk.ps1)
CloseApplications=no
; UV_PYTHON_INSTALL_DIR, set for the person: programs started after setup see it
ChangesEnvironment=yes
SetupLogging=yes
UninstallDisplayName=Job Desk

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put a Job Desk shortcut on the desktop"; GroupDescription: "Shortcuts:"
Name: "claudecode"; Description: "Also set up Claude Code (for the chat features)"; GroupDescription: "Optional:"; Flags: unchecked

[InstallDelete]
; an older version's code goes first, so only this version's is there
Type: filesandordirs; Name: "{app}\plugin\server\src"

[Files]
Source: "{#PluginDir}\*"; DestDir: "{app}\plugin"; Excludes: "{#PluginExcludes}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "build\uv\uv.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "build\uv\uvw.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
; (for PrepareToInstall: an update stops the desk running from the older version)
Source: "stop-desk.ps1"; Flags: dontcopy

[Icons]
; uvw is uv without a console window: the desk runs out of sight, and it opens in a window of its own
Name: "{autoprograms}\Job Desk"; Filename: "{app}\bin\uvw.exe"; Parameters: "run --frozen --python {#Python} --project ""{app}\plugin\server"" job-apply-desk"; WorkingDir: "{app}\plugin\server"; Comment: "Open the Job Desk"
Name: "{autodesktop}\Job Desk"; Filename: "{app}\bin\uvw.exe"; Parameters: "run --frozen --python {#Python} --project ""{app}\plugin\server"" job-apply-desk"; WorkingDir: "{app}\plugin\server"; Comment: "Open the Job Desk"; Tasks: desktopicon

[Run]
; Claude Code, when its box was ticked: the one-step PowerShell installer (Git, Claude Code, the plugin), in a
; window of its own that stays open to read
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "{code:ClaudeCodeParams}"; StatusMsg: "Starting the Claude Code setup in its own window..."; Tasks: claudecode; Check: not Relaunch; Flags: nowait
Filename: "{app}\bin\uvw.exe"; Parameters: "run --frozen --python {#Python} --project ""{app}\plugin\server"" job-apply-desk"; WorkingDir: "{app}\plugin\server"; Description: "Open the Job Desk now"; Flags: postinstall nowait skipifsilent
; (an update from the desk's Update button, /RELAUNCH=1, opens the desk again from [Code], at ssPostInstall once SetUp
; has installed its packages and browser: a [Run] line would run before that)

[UninstallDelete]
; what uv made here (the Python environment) and what ran from here wrote (caches): this program's folders alone
Type: filesandordirs; Name: "{app}\plugin"
Type: filesandordirs; Name: "{app}\bin"
Type: dirifempty; Name: "{app}"

[Code]
var
  Problems: String;
  { (true once the install has finished: DeinitializeSetup tells a finished update from one that stopped) }
  Installed: Boolean;
  { (true once the files have begun to be copied, at ssInstall: an update that stops after that may have left a copy
    that won't open, so DeinitializeSetup opens nothing then, and says so) }
  CopyBegan: Boolean;
  { (true once an update has opened the desk again, at ssPostInstall) }
  Reopened: Boolean;

{ Started by the desk's Update button (updates.py: /RELAUNCH=1): open the desk again once installed, and never run
  the Claude Code setup, whatever box was ticked last time }
function Relaunch(): Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;

function SetEnvironmentVariable(lpName, lpValue: String): BOOL;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

function InAppData(Path: String): Boolean;
var
  Text, Root: String;
begin
  Text := Lowercase(AddBackslash(Path));
  StringChangeEx(Text, '/', '\', True);
  Result := Pos('\appdata\', Text) > 0;
  Root := GetEnv('LOCALAPPDATA');
  if (Root <> '') and (Pos(Lowercase(AddBackslash(Root)), Text) = 1) then
    Result := True;
  Root := GetEnv('APPDATA');
  if (Root <> '') and (Pos(Lowercase(AddBackslash(Root)), Text) = 1) then
    Result := True;
end;

{ Where uv keeps the desk's Python: outside AppData, as install.ps1 does (the Claude app keeps its own copy of what's
  written there, so a Python uv puts there can be missing for it). A setting already outside AppData is kept. }
procedure UsePythonDir();
var
  Dir: String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'UV_PYTHON_INSTALL_DIR', Dir) or (Trim(Dir) = '')
      or InAppData(Dir) then
  begin
    Dir := ExpandConstant('{%USERPROFILE}\.uv-python');
    if not RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'UV_PYTHON_INSTALL_DIR', Dir) then
      Log('Could not save UV_PYTHON_INSTALL_DIR');
  end;
  ForceDirectories(Dir);
  SetEnvironmentVariable('UV_PYTHON_INSTALL_DIR', Dir);  { (for uv below, and the desk opened at the end) }
  Log('UV_PYTHON_INSTALL_DIR: ' + Dir);
end;

{ A line without its terminal colors (Playwright's downloader dims some words) }
function Plain(const S: String): String;
var
  I: Integer;
  InColor: Boolean;
begin
  Result := '';
  InColor := False;
  for I := 1 to Length(S) do
    if InColor then
      InColor := S[I] <> 'm'
    else if S[I] = #27 then
      InColor := True
    else
      Result := Result + S[I];
end;

procedure ShowLine(const S: String; const Error, FirstLine: Boolean);
var
  Line: String;
begin
  Line := Trim(Plain(S));
  Log(Line);
  if Line <> '' then
    WizardForm.FilenameLabel.Caption := Line;
end;

{ uv, with what it says shown line by line under the progress bar (and in Setup's log); no console window }
function RunUv(const Status, Params: String): Boolean;
var
  Code: Integer;
begin
  WizardForm.StatusLabel.Caption := Status;
  WizardForm.FilenameLabel.Caption := '';
  Log('uv ' + Params);
  Result := False;
  try
    Result := ExecAndLogOutput(ExpandConstant('{app}\bin\uv.exe'), Params, ExpandConstant('{app}\plugin\server'),
      SW_HIDE, ewWaitUntilTerminated, Code, @ShowLine) and (Code = 0);
    if not Result then
      Log('uv ended with ' + IntToStr(Code));
  except
    Log(GetExceptionMessage);
  end;
end;

function FoundIn(const Rel: String): Boolean;
begin
  Result := FileExists(ExpandConstant('{commonpf64}\') + Rel) or FileExists(ExpandConstant('{commonpf32}\') + Rel)
    or FileExists(ExpandConstant('{localappdata}\') + Rel);
end;

{ The browser the desk fills applications in: Chrome or Edge already here (Playwright starts them by channel), else
  Playwright's own Chromium }
function HasBrowser(): Boolean;
begin
  Result := FoundIn('Google\Chrome\Application\chrome.exe') or FoundIn('Microsoft\Edge\Application\msedge.exe');
end;

procedure SetUp();
var
  Project: String;
begin
  Project := '--project "' + ExpandConstant('{app}\plugin\server') + '"';
  WizardForm.ProgressGauge.Style := npbstMarquee;
  try
    UsePythonDir();
    if not RunUv('Getting Python and the Job Desk''s packages (a few minutes the first time)...',
        'sync --frozen --managed-python --python {#Python} ' + Project) then
      Problems := Problems + #13#10 + '- Python and the Job Desk''s packages didn''t install. Check the internet ' +
        'connection and run this installer again.';
    if HasBrowser() then
      Log('Browser: Chrome or Edge is here')
    else if not RunUv('Getting a browser for the Job Desk (there''s no Chrome or Edge here)...',
        'run --frozen --python {#Python} ' + Project + ' playwright install chromium') then
      Problems := Problems + #13#10 + '- The browser didn''t download. Install Google Chrome, or run this ' +
        'installer again.';
  finally
    WizardForm.ProgressGauge.Style := npbstNormal;
    WizardForm.FilenameLabel.Caption := '';
  end;
end;

procedure StopDesk(const Script: String);
var
  Code: Integer;
begin
  if not FileExists(Script) then
    Exit;
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), '-NoProfile -NonInteractive ' +
    '-ExecutionPolicy Bypass -File "' + Script + '" -AppDir "' + ExpandConstant('{app}') + '"', '', SW_HIDE,
    ewWaitUntilTerminated, Code);
end;

{ An update: the desk running from the older version stops first (its files are about to change) }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if DirExists(ExpandConstant('{app}\plugin')) then
  begin
    ExtractTemporaryFile('stop-desk.ps1');
    StopDesk(ExpandConstant('{tmp}\stop-desk.ps1'));
  end;
end;

{ The desk installed in App, started as the Start menu's shortcut starts it, and not waited for }
function OpenDesk(const App: String): Boolean;
var
  Code: Integer;
begin
  Result := Exec(App + '\bin\uvw.exe', 'run --frozen --python {#Python} --project "' + App +
    '\plugin\server" job-apply-desk', App + '\plugin\server', SW_SHOWNORMAL, ewNoWait, Code);
  if not Result then
    Log('Job Desk: the desk could not be opened: ' + SysErrorMessage(Code));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    CopyBegan := True;
    Log('Job Desk: copying the files.');
  end;
  if CurStep = ssPostInstall then
  begin
    SetUp();
    if Problems <> '' then
      SuppressibleMsgBox('The Job Desk is installed, but:' + Problems, mbError, MB_OK, IDOK);
    { an update from the desk's Update button: the desk opens again, now that its packages and browser are set up }
    if Relaunch() then
    begin
      Log('Job Desk: opening the updated desk.');
      Reopened := OpenDesk(ExpandConstant('{app}'));
    end;
  end;
  if CurStep = ssDone then
  begin
    Installed := True;
    Log('Job Desk: the install finished.');
  end;
end;

{ An update from the desk's Update button (Relaunch) that didn't finish. The desk stopped for it (PrepareToInstall).
  Stopped before its files began to be copied (it ran into something before ssInstall): the desk still installed
  opens again, as the Start menu's shortcut opens it, and the person is back where they were on the version they had.
  Nothing starts when that desk isn't there, or when Setup stopped before it knew where the program is. Stopped once
  the copy had begun (it failed part-way: [InstallDelete] had taken the older code, and a rollback doesn't bring it
  back): nothing is opened, and the person is told to run the installer again (a message a silent install shows,
  unless /SUPPRESSMSGBOXES). }
procedure DeinitializeSetup();
var
  App: String;
begin
  if Installed or Reopened or not Relaunch() then
    Exit;
  if CopyBegan then
  begin
    Log('Job Desk: the update did not finish after its files began to change; nothing was opened.');
    SuppressibleMsgBox('The Job Desk''s update didn''t finish, and the Job Desk may not open. Run the Job Desk''s ' +
      'installer again to put it right.', mbError, MB_OK, IDOK);
    Exit;
  end;
  try
    App := ExpandConstant('{app}');
  except
    Exit;
  end;
  if not FileExists(App + '\bin\uvw.exe') or not FileExists(App + '\plugin\server\src\job_apply\desk.py') then
    Exit;
  Log('Job Desk: the update did not finish; opening the desk that is still installed.');
  OpenDesk(App);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    StopDesk(ExpandConstant('{app}\plugin\setup\windows\stop-desk.ps1'));
end;

{ powershell -Command "& '<install.ps1>' -Yes; Read-Host ...": the window stays open once it's done, to read }
function ClaudeCodeParams(Param: String): String;
var
  Script: String;
begin
  Script := ExpandConstant('{app}\plugin\setup\install.ps1');
  StringChangeEx(Script, '''', '''''', True);
  Result := '-NoProfile -ExecutionPolicy Bypass -Command "& ''' + Script + ''' -Yes; ' +
    'Read-Host ''Done. Press Enter to close this window''"';
end;
