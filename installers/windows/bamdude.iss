; BamDude Windows Installer — Inno Setup script
;
; Builds a self-contained installer that lays down:
;   - embedded Python 3.12 + pre-installed venv
;   - backend source + pre-built frontend bundle
;   - NSSM + ffmpeg under bin/
;   - a Windows service running as LocalSystem
;
; Build prerequisites: run installers/windows/build.py first to stage
; the build/staging/ tree, then compile this file with ISCC.exe.
;
; See installers/windows/README.md for the full pipeline.

#define MyAppName "BamDude"
#define MyAppPublisher "BamDude Contributors"
#define MyAppURL "https://bamdude.top"
#define MyAppExeName "bamdude.exe"
#define ServiceName "BamDude"
#define DefaultPort "8000"

; Version is stamped by build.py into build\staging\version.iss as a
; #define directive. Falls back to a placeholder if you ran ISCC without
; running build.py first (don't ship that build).
#ifexist "build\staging\version.iss"
  #include "build\staging\version.iss"
#else
  #define MyAppVersion "0.0.0+dev"
#endif

[Setup]
AppId={{6D2F9C41-8A73-4B25-9E60-2C7A1F0B4D88}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\BamDude
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
OutputDir=build\output
OutputBaseFilename=bamdude-{#MyAppVersion}-windows-x64-setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Admin required: we register a Windows service and write to ProgramData
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=
; BamDude branding — the dark TILE at every size, so the mark stays readable on
; light surfaces (Explorer, the Programs list, the setup .exe) and dark ones
; alike. The transparent dark-bars favicon.ico was tried and rejected: it reads
; as a bare glyph.
;
; ⚠️ Copied from the brand pack's app-icon-tile.ico, NOT app-icon.ico — the
; latter carries the tile only at 64/128/256 and drops it at 16/32/48, so small
; views showed a bare glyph while large ones showed the tile. Regenerate from
; the pack, never edit here; see the pack's README for how the variant is built.
;
; ⚠️ One file feeds SetupIconFile, UninstallDisplayIcon and every shortcut, so
; changing it changes all of them together. Lives next to this .iss so the
; SourcePath-relative reference works during compile, and the [Files] entry
; stages it into {app} for Add/Remove Programs.
SetupIconFile=bamdude.ico
UninstallDisplayIcon={app}\bamdude.ico
; Don't allow installing to a network drive — service won't start cleanly
DisableDirPage=no
DisableReadyPage=no
ChangesEnvironment=no
CloseApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"

; Every string the wizard shows that is ours rather than Inno's. Keep both
; languages in step — a key present in only one falls back to the other
; language's text, which reads like a bug to the user.
[CustomMessages]
english.TaskDesktopIcon=Create a desktop shortcut
english.TaskGroupShortcuts=Additional shortcuts:
english.TaskFirewall=Add Windows Firewall rule for BamDude (port {#DefaultPort})
english.TaskGroupNetwork=Network:
english.IconDashboard=Open BamDude Dashboard
english.IconLogs=BamDude Logs
english.IconUninstall=Uninstall BamDude
english.StatusRegisterService=Registering BamDude service...
english.StatusFirewall=Adding firewall rule...
english.DbPageCaption=Database
english.DbPageDescription=Where should BamDude keep its data?
english.DbPageSubCaption=SQLite needs nothing and is a fine choice for most farms. PostgreSQL suits large, busy farms.
english.DbOptSqlite=SQLite (a single file, no server - recommended)
english.DbOptEmbeddedService=Bundled PostgreSQL 18, as its own Windows service (most robust)
english.DbOptEmbeddedChild=Bundled PostgreSQL 18, started and stopped by BamDude (simpler)
english.DbOptExternal=An external PostgreSQL server (enter its URL)
english.UrlPageCaption=External PostgreSQL
english.UrlPageDescription=Connection URL
english.UrlPageSubCaption=The database must already exist - BamDude creates the tables, not the database.
english.UrlPrompt=postgresql+asyncpg://user:password@host:5432/bamdude
english.UrlRequired=Please enter the PostgreSQL connection URL, or go back and choose SQLite.
english.UninstallDataTitle=BamDude data
english.UninstallDataHeader=What should happen to your BamDude data?
english.UninstallDataFolder=All of it lives in one folder:%n%1
english.UninstallDataKeep=Keep it (recommended)
english.UninstallDataKeepDetail=The database, every print archive and your settings stay on disk. Installing BamDude again later picks them up automatically — this is what you want for an upgrade or a reinstall.
english.UninstallDataDelete=Delete everything
english.UninstallDataDeleteDetail=Removes that folder for good: the database, every print archive, all settings, and the bundled PostgreSQL cluster if you use one. There is no undo and no copy left behind — make a backup first if you are not certain.
english.UninstallDataContinue=Continue
english.ServiceSetupFailed=BamDude was installed, but its Windows service could not be registered, so nothing is running yet.%n%nThe setup log is at:%n%1%n%nOpen it to see what failed, then re-run this installer.

ukrainian.TaskDesktopIcon=Створити ярлик на робочому столі
ukrainian.TaskGroupShortcuts=Додаткові ярлики:
ukrainian.TaskFirewall=Додати правило брандмауера Windows для BamDude (порт {#DefaultPort})
ukrainian.TaskGroupNetwork=Мережа:
ukrainian.IconDashboard=Відкрити панель BamDude
ukrainian.IconLogs=Журнали BamDude
ukrainian.IconUninstall=Видалити BamDude
ukrainian.StatusRegisterService=Реєстрація служби BamDude...
ukrainian.StatusFirewall=Додавання правила брандмауера...
ukrainian.DbPageCaption=База даних
ukrainian.DbPageDescription=Де BamDude має зберігати дані?
ukrainian.DbPageSubCaption=SQLite не потребує нічого і підходить більшості ферм. PostgreSQL — для великих завантажених ферм.
ukrainian.DbOptSqlite=SQLite (один файл, без сервера — рекомендовано)
ukrainian.DbOptEmbeddedService=Вбудований PostgreSQL 18 окремою службою Windows (найнадійніше)
ukrainian.DbOptEmbeddedChild=Вбудований PostgreSQL 18 під керуванням BamDude (простіше)
ukrainian.DbOptExternal=Зовнішній сервер PostgreSQL (вкажіть URL)
ukrainian.UrlPageCaption=Зовнішній PostgreSQL
ukrainian.UrlPageDescription=URL підключення
ukrainian.UrlPageSubCaption=База вже має існувати — BamDude створює таблиці, а не базу.
ukrainian.UrlPrompt=postgresql+asyncpg://user:password@host:5432/bamdude
ukrainian.UrlRequired=Введіть URL підключення до PostgreSQL або поверніться назад і оберіть SQLite.
ukrainian.UninstallDataTitle=Дані BamDude
ukrainian.UninstallDataHeader=Що зробити з вашими даними BamDude?
ukrainian.UninstallDataFolder=Усе це лежить в одній теці:%n%1
ukrainian.UninstallDataKeep=Зберегти (рекомендовано)
ukrainian.UninstallDataKeepDetail=База, всі архіви друку й ваші налаштування лишаються на диску. Наступне встановлення BamDude підхопить їх само — саме це потрібно при оновленні чи перевстановленні.
ukrainian.UninstallDataDelete=Видалити все
ukrainian.UninstallDataDeleteDetail=Ця тека зникає остаточно: база, всі архіви друку, всі налаштування і кластер вбудованого PostgreSQL, якщо ви ним користуєтесь. Скасувати це не можна, копії не лишиться — якщо не впевнені, спершу зробіть резервну копію.
ukrainian.UninstallDataContinue=Продовжити
ukrainian.ServiceSetupFailed=BamDude встановлено, але службу Windows зареєструвати не вдалося, тож зараз нічого не запущено.%n%nЖурнал встановлення:%n%1%n%nВідкрийте його, щоб побачити причину, потім запустіть інсталятор ще раз.

[Tasks]
Name: "desktopicon"; Description: "{cm:TaskDesktopIcon}"; GroupDescription: "{cm:TaskGroupShortcuts}"; Flags: unchecked
Name: "firewallrule"; Description: "{cm:TaskFirewall}"; GroupDescription: "{cm:TaskGroupNetwork}"

[Files]
; Embedded Python (entire tree)
Source: "build\staging\python\*"; DestDir: "{app}\python"; Flags: recursesubdirs ignoreversion
; Backend + frontend
Source: "build\staging\app\*"; DestDir: "{app}\app"; Flags: recursesubdirs ignoreversion
; NSSM, ffmpeg, ffprobe
Source: "build\staging\bin\*"; DestDir: "{app}\bin"; Flags: recursesubdirs ignoreversion
; Service install/uninstall scripts
; ⚠️ Straight from the source tree, NOT from build\staging. Everything else in
; this section is generated and has to come from staging, but these two .bat
; files are checked-in sources — and staging them meant ISCC packaged whatever
; copy the last build.py run happened to leave there. Editing a script and
; recompiling with ISCC alone then shipped the OLD one, silently: exactly how a
; fixed uninstaller went out still carrying the bug it fixed.
Source: "service\*"; DestDir: "{app}\service"; Flags: recursesubdirs ignoreversion
; Version stamp
Source: "build\staging\VERSION"; DestDir: "{app}"; Flags: ignoreversion
; App icon — used by UninstallDisplayIcon (Add/Remove Programs) and the
; Start Menu / desktop shortcuts. Lives at the install root so the
; UninstallDisplayIcon path stays stable when the [Files] tree changes.
Source: "bamdude.ico"; DestDir: "{app}"; Flags: ignoreversion

[Dirs]
; ProgramData layout — created with permissions LocalSystem can write to
Name: "{commonappdata}\BamDude"; Permissions: users-modify
Name: "{commonappdata}\BamDude\data"; Permissions: users-modify
Name: "{commonappdata}\BamDude\logs"; Permissions: users-modify

[Icons]
Name: "{group}\{cm:IconDashboard}"; Filename: "http://localhost:{#DefaultPort}"; IconFilename: "{app}\bamdude.ico"
Name: "{group}\{cm:IconLogs}"; Filename: "{commonappdata}\BamDude\logs"
Name: "{group}\{cm:IconUninstall}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\BamDude"; Filename: "http://localhost:{#DefaultPort}"; IconFilename: "{app}\bamdude.ico"; Tasks: desktopicon

[Run]
; Register and start the Windows service. The trailing arguments (database
; backend + optional URL) are built by GetInstallServiceParams in [Code] from
; the storage-chooser wizard page.
Filename: "{app}\service\install-service.bat"; Parameters: "{code:GetInstallServiceParams}"; Flags: runhidden waituntilterminated; StatusMsg: "{cm:StatusRegisterService}"

; Open Windows Firewall on the dashboard port. We do this only if the
; user opted in via the firewallrule task — some environments manage
; firewall centrally and prefer to handle this themselves.
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""BamDude Dashboard"" dir=in action=allow protocol=TCP localport={#DefaultPort}"; Flags: runhidden waituntilterminated; Tasks: firewallrule; StatusMsg: "{cm:StatusFirewall}"

; Open the dashboard in the user's default browser at the end of install
Filename: "http://localhost:{#DefaultPort}"; Flags: shellexec postinstall nowait skipifsilent; Description: "{cm:IconDashboard}"

[UninstallRun]
; Stop + deregister the service before file removal. RunOnceId makes the
; entry run-once per uninstall pass (Inno Setup default is to re-run on
; every pass, which can fire multiple times during upgrade flows).
Filename: "{app}\service\uninstall-service.bat"; Parameters: """{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "StopBamDudeService"

; Remove the firewall rule (silently — if it doesn't exist, netsh just complains)
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""BamDude Dashboard"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveFirewallRule"

[UninstallDelete]
; Remove install dir contents; leave ProgramData\BamDude alone so the
; user keeps their database + archives. Re-installing on top picks them
; back up automatically.
Type: filesandordirs; Name: "{app}"

[Code]

// --- Storage backend chooser -------------------------------------------------
//
// One wizard page with four choices, mirroring the app's DATABASE_URL states:
//   0 SQLite                              -> sqlite
//   1 bundled PostgreSQL, own service     -> embedded-service
//   2 bundled PostgreSQL, run by BamDude  -> embedded-child
//   3 external PostgreSQL (URL)           -> external
// The external URL is asked on a second page, shown only for choice 3.

var
  StoragePage: TInputOptionWizardPage;
  UrlPage: TInputQueryWizardPage;
  RemoveDataOnUninstall: Boolean;

// Read the current backend from the installed BamDude service's environment
// (NSSM keeps AppEnvironmentExtra as a REG_MULTI_SZ), so an upgrade defaults to
// what is already in use instead of silently reverting to SQLite. Returns the
// selected index, and the external URL through ExistingUrl.
function DetectExistingChoice(var ExistingUrl: String): Integer;
var
  Env: String;
begin
  Result := 0;
  ExistingUrl := '';
  if RegQueryMultiStringValue(HKLM, 'SYSTEM\CurrentControlSet\Services\BamDude\Parameters',
       'AppEnvironmentExtra', Env) then
  begin
    if Pos('EMBEDDED_PG_EXTERNAL_SERVICE=1', Env) > 0 then
      Result := 1
    else if Pos('DATABASE_URL=embedded', Env) > 0 then
      Result := 2
    else if Pos('DATABASE_URL=postgresql', Env) > 0 then
    begin
      Result := 3;
      // pull the URL out of the DATABASE_URL=... line for the field default
      ExistingUrl := Copy(Env, Pos('DATABASE_URL=postgresql', Env) + Length('DATABASE_URL='), 4096);
      // Cut at the first separator, whichever form RegQueryMultiStringValue used.
      if Pos(#0, ExistingUrl) > 0 then ExistingUrl := Copy(ExistingUrl, 1, Pos(#0, ExistingUrl) - 1);
      if Pos(#13, ExistingUrl) > 0 then ExistingUrl := Copy(ExistingUrl, 1, Pos(#13, ExistingUrl) - 1);
      if Pos(#10, ExistingUrl) > 0 then ExistingUrl := Copy(ExistingUrl, 1, Pos(#10, ExistingUrl) - 1);
    end;
  end;
end;

procedure InitializeWizard();
var
  ExistingUrl: String;
begin
  StoragePage := CreateInputOptionPage(wpSelectDir,
    CustomMessage('DbPageCaption'), CustomMessage('DbPageDescription'),
    CustomMessage('DbPageSubCaption'),
    True, False);
  StoragePage.Add(CustomMessage('DbOptSqlite'));
  StoragePage.Add(CustomMessage('DbOptEmbeddedService'));
  StoragePage.Add(CustomMessage('DbOptEmbeddedChild'));
  StoragePage.Add(CustomMessage('DbOptExternal'));

  UrlPage := CreateInputQueryPage(StoragePage.ID,
    CustomMessage('UrlPageCaption'), CustomMessage('UrlPageDescription'),
    CustomMessage('UrlPageSubCaption'));
  UrlPage.Add(CustomMessage('UrlPrompt'), False);

  StoragePage.SelectedValueIndex := DetectExistingChoice(ExistingUrl);
  if ExistingUrl <> '' then
    UrlPage.Values[0] := ExistingUrl;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  // The URL page only matters for the external choice (index 3).
  if PageID = UrlPage.ID then
    Result := StoragePage.SelectedValueIndex <> 3;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = UrlPage.ID) and (Trim(UrlPage.Values[0]) = '') then
  begin
    MsgBox(CustomMessage('UrlRequired'), mbError, MB_OK);
    Result := False;
  end;
end;

function GetDbMode(): String;
begin
  case StoragePage.SelectedValueIndex of
    1: Result := 'embedded-service';
    2: Result := 'embedded-child';
    3: Result := 'external';
  else
    Result := 'sqlite';
  end;
end;

// Full argument string for install-service.bat:
//   "<app>" "<data root>" <port> <db mode> "<url>"
function GetInstallServiceParams(Param: String): String;
var
  Url: String;
begin
  Url := '';
  if StoragePage.SelectedValueIndex = 3 then
    Url := Trim(UrlPage.Values[0]);
  Result := '"' + ExpandConstant('{app}') + '" "' + ExpandConstant('{commonappdata}\BamDude') + '" ' +
            '{#DefaultPort}' + ' ' + GetDbMode() + ' "' + Url + '"';
end;

// Stop the BamDude service (and the bundled PostgreSQL service, if any) BEFORE
// the [Files] section copies anything, so file locks on python.exe / .pyd /
// nssm.exe / postgres.exe release in time for the overwrite. Without this,
// upgrading over a running install fails with "permission denied" on every
// file a service has open.
//
// The guard is the SERVICE, not a file on disk: ServiceExists returns at once
// on a first-time install, where nothing is registered yet.
function ServiceExists(const ServiceName: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\sc.exe'), 'query ' + ServiceName, '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

// Ask the SCM to stop a service and wait until it really is stopped.
//
// ⚠️ sc stop, never `net stop`. `net stop` asks whether to also stop the
// services that depend on this one, and everything here runs hidden with no
// console, so that question hangs the installer at "Preparing to install"
// with the service still up. uninstall-service.bat carries the same warning —
// it hit exactly this once; the install path was never updated to match.
//
// ⚠️ sc returns as soon as the control code is delivered, NOT when the service
// has stopped, so the poll is the point of this function. `sc query | find
// "STOPPED"` is the only state signal an Exec exit code can carry, since Inno
// cannot read a process's output.
function StopServiceAndWait(const ServiceName: String; TimeoutSeconds: Integer): Boolean;
var
  ResultCode, Waited: Integer;
begin
  if not ServiceExists(ServiceName) then
  begin
    Result := True;
    Exit;
  end;

  Log('Stopping ' + ServiceName + ' before file copy...');
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop ' + ServiceName, '',
    SW_HIDE, ewWaitUntilTerminated, ResultCode);

  Waited := 0;
  repeat
    if Exec(ExpandConstant('{cmd}'),
         '/c sc query ' + ServiceName + ' | find "STOPPED" >nul', '',
         SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
    begin
      Result := True;
      Exit;
    end;
    Sleep(2000);
    Waited := Waited + 2;
  until Waited >= TimeoutSeconds;

  Log(ServiceName + ' did not reach STOPPED within ' + IntToStr(TimeoutSeconds) + 's');
  Result := False;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  NeedsRestart := False;

  // ⚠️ BamDude FIRST — it depends on BamDudePostgres (DependOnService, set by
  // install-service.bat). Stopping the dependency while its dependent is still
  // running is exactly what makes the SCM ask about dependents, which is the
  // question that must never be asked here.
  //
  // Both are best-effort: a service that is not installed returns at once, so
  // this is a no-op on a first-time install.
  StopServiceAndWait('BamDude', 60);

  if not StopServiceAndWait('BamDudePostgres', 90) then
    // Not fatal on its own — say it in the log and let [Files] report the real
    // failure if a binary is still held. Aborting here would strand an install
    // that a second attempt would complete.
    Log('BamDudePostgres is still running; the file copy may fail on its binaries');

  // A beat for Windows to finalize the unload before [Files] takes exclusive
  // handles on nssm.exe / postgres.exe.
  Sleep(1500);
end;

// install-service.bat runs hidden and Inno does not abort on a non-zero [Run]
// exit code, so a failed service registration used to leave the user with a
// finished-looking install, no service, an empty data directory and no message
// at all. Check for the service and point at the log if it is not there.
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Ok: Boolean;
  LogPath: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  // A line may not START with '[' anywhere in the script — the parser reads
  // that as a section tag, even inside [Code] — so the message argument is
  // bound to a variable first rather than written as an inline array.
  LogPath := ExpandConstant('{commonappdata}\BamDude\logs\install-service.log');
  Ok := Exec(ExpandConstant('{sys}\sc.exe'), 'query BamDude', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if (not Ok) or (ResultCode <> 0) then
    MsgBox(FmtMessage(CustomMessage('ServiceSetupFailed'), [LogPath]), mbError, MB_OK);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  // Port-conflict check deferred: Inno has no native socket API, so a conflict
  // on 8000 surfaces at first service start and the user reads the log.
end;

// --- Uninstall: optionally remove all data ----------------------------------

// Ask about the data as its own step with two named choices, not as a yes/no
// box. What "yes" destroys here is the whole database and every print archive,
// and two identically-shaped buttons are the wrong control for a decision that
// has no undo — the option the user picks should say what it does.
//
// ⚠️ The uninstaller has NO wizard, so the usual page API (CreateInputOptionPage
// and friends) is unavailable: it builds on WizardForm, which does not exist at
// uninstall time. CreateCustomForm is the documented way to get a page-shaped
// window here, and the price is that the layout is positioned by hand.
//
// Returns False to abort the uninstall entirely (the user closed the window).
function AskAboutData(var RemoveData: Boolean): Boolean;
var
  Form: TSetupForm;
  Header, FolderLabel, KeepDetail, DeleteDetail: TNewStaticText;
  KeepRadio, DeleteRadio: TNewRadioButton;
  ContinueButton, CancelButton: TNewButton;
  DataPath: String;
  ButtonWidth: Integer;
begin
  RemoveData := False;
  DataPath := ExpandConstant('{commonappdata}\BamDude');

  // KeepSizeX is False, so the window is allowed to grow wider than the 470
  // asked for here — and on a wide screen it does, which makes the option
  // descriptions run in long lines. Verified in this shape on 2026-09-08 and
  // left alone deliberately.
  //
  // ⚠️ Passing True to narrow it is a one-word change but NOT a cosmetic one:
  // the height below is derived from how the labels wrapped, so a narrower
  // window wraps more, grows taller, and needs looking at again in BOTH
  // languages — the Ukrainian strings are the longer pair.
  Form := CreateCustomForm(ScaleX(470), ScaleY(320), False, True);
  try
    Form.Caption := CustomMessage('UninstallDataTitle');

    Header := TNewStaticText.Create(Form);
    Header.Parent := Form;
    Header.Left := ScaleX(16);
    Header.Top := ScaleY(16);
    Header.Width := Form.ClientWidth - ScaleX(32);
    Header.WordWrap := True;
    Header.AutoSize := True;
    Header.Font.Style := [fsBold];
    Header.Caption := CustomMessage('UninstallDataHeader');

    FolderLabel := TNewStaticText.Create(Form);
    FolderLabel.Parent := Form;
    FolderLabel.Left := ScaleX(16);
    FolderLabel.Top := Header.Top + Header.Height + ScaleY(8);
    FolderLabel.Width := Form.ClientWidth - ScaleX(32);
    FolderLabel.WordWrap := True;
    FolderLabel.AutoSize := True;
    FolderLabel.Caption := FmtMessage(CustomMessage('UninstallDataFolder'), [DataPath]);

    // Keep is first and pre-selected: the safe option should be the one the eye
    // lands on and the one Enter takes.
    KeepRadio := TNewRadioButton.Create(Form);
    KeepRadio.Parent := Form;
    KeepRadio.Left := ScaleX(16);
    KeepRadio.Top := FolderLabel.Top + FolderLabel.Height + ScaleY(16);
    KeepRadio.Width := Form.ClientWidth - ScaleX(32);
    KeepRadio.Caption := CustomMessage('UninstallDataKeep');
    KeepRadio.Checked := True;

    KeepDetail := TNewStaticText.Create(Form);
    KeepDetail.Parent := Form;
    KeepDetail.Left := ScaleX(34);
    KeepDetail.Top := KeepRadio.Top + KeepRadio.Height + ScaleY(4);
    KeepDetail.Width := Form.ClientWidth - ScaleX(50);
    KeepDetail.WordWrap := True;
    KeepDetail.AutoSize := True;
    KeepDetail.Caption := CustomMessage('UninstallDataKeepDetail');

    DeleteRadio := TNewRadioButton.Create(Form);
    DeleteRadio.Parent := Form;
    DeleteRadio.Left := ScaleX(16);
    DeleteRadio.Top := KeepDetail.Top + KeepDetail.Height + ScaleY(14);
    DeleteRadio.Width := Form.ClientWidth - ScaleX(32);
    DeleteRadio.Caption := CustomMessage('UninstallDataDelete');

    DeleteDetail := TNewStaticText.Create(Form);
    DeleteDetail.Parent := Form;
    DeleteDetail.Left := ScaleX(34);
    DeleteDetail.Top := DeleteRadio.Top + DeleteRadio.Height + ScaleY(4);
    DeleteDetail.Width := Form.ClientWidth - ScaleX(50);
    DeleteDetail.WordWrap := True;
    DeleteDetail.AutoSize := True;
    DeleteDetail.Caption := CustomMessage('UninstallDataDeleteDetail');

    // ⚠️ KeepSizeX / KeepSizeY above only say whether the form may GROW with
    // WizardSizePercent — they do not fit it to its contents. So the height is
    // taken from the labels once they have wrapped: these texts are localized
    // and are not the same number of lines in every language.
    Form.ClientHeight := DeleteDetail.Top + DeleteDetail.Height + ScaleY(56);

    ContinueButton := TNewButton.Create(Form);
    ContinueButton.Parent := Form;
    ContinueButton.Caption := CustomMessage('UninstallDataContinue');
    ContinueButton.Height := ScaleY(23);
    ContinueButton.Top := Form.ClientHeight - ScaleY(23 + 12);
    ContinueButton.ModalResult := mrOk;
    ContinueButton.Default := True;

    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := SetupMessage(msgButtonCancel);
    CancelButton.Height := ScaleY(23);
    CancelButton.Top := ContinueButton.Top;
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;

    // One width for both, wide enough for the longer of the two labels — the
    // Ukrainian captions are longer than the English ones.
    ButtonWidth := Form.CalculateButtonWidth([ContinueButton.Caption, CancelButton.Caption]);
    ContinueButton.Width := ButtonWidth;
    CancelButton.Width := ButtonWidth;
    CancelButton.Left := Form.ClientWidth - ScaleX(12) - ButtonWidth;
    ContinueButton.Left := CancelButton.Left - ScaleX(6) - ButtonWidth;

    // No FlipAndCenterIfNeeded here: it centers on WizardForm, which does not
    // exist during uninstall. Left alone, the form centers on the screen.
    Result := Form.ShowModal = mrOk;
    if Result then
      RemoveData := DeleteRadio.Checked;
  finally
    Form.Free;
  end;
end;
function InitializeUninstall(): Boolean;
begin
  // Keeping the data is the default and stays the default: an uninstall that
  // silently took the archives with it would be unrecoverable.
  Result := AskAboutData(RemoveDataOnUninstall);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // The service-stop + deregister runs from [UninstallRun] before file removal.
  // Delete the data tree only here, after everything is stopped, and only if
  // the user asked for it.
  if (CurUninstallStep = usPostUninstall) and RemoveDataOnUninstall then
  begin
    Log('Removing BamDude data directory at user request');
    DelTree(ExpandConstant('{commonappdata}\BamDude'), True, True, True);
  end;
end;
