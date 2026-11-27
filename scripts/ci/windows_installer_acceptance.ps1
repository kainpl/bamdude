<#
.SYNOPSIS
  Full acceptance of the REAL Windows installer on a disposable CI runner (spec stage B R13).

.DESCRIPTION
  Installs, upgrades, refuses and uninstalls the compiled installer exactly as an operator's machine would see it,
  and writes acceptance.json for the release gate (scripts/ci/check_release_payload.py). It registers, replaces and
  deletes Windows services named BamDude and wipes C:\ProgramData\BamDude: it refuses to run anywhere but a CI
  runner. Every call of Setup is silent (/VERYSILENT /SUPPRESSMSGBOXES /NORESTART) under a deadline that ends the
  whole process tree; nothing here may wait on a window.

  Order: S1 S2 S3 S4 S4b S5 S8 S6 S6b S7, a reset, then S0 (an upgrade from v0.7.0 needs a machine without BamDude).
  Only the full scope can open a release; the diagnostic scope runs a subset and never does.
#>
param(
    [Parameter(Mandatory)] [string] $Installer,
    [Parameter(Mandatory)] [string] $Payload,
    [ValidateSet('full', 'diagnostic')] [string] $Scope = 'full',
    [string] $Report = 'acceptance.json'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3
if (-not $env:GITHUB_ACTIONS) { throw 'this harness runs only on a disposable CI runner: it replaces Windows services' }

$Repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Installer = (Resolve-Path -LiteralPath $Installer).Path
$Work = Join-Path $env:RUNNER_TEMP 'bamdude-acceptance'
New-Item -ItemType Directory -Force $Work | Out-Null
$State = Join-Path $Work 'http-state.json'
$Data = Join-Path $env:ProgramData 'BamDude'
$Limit = [int](Get-Content -LiteralPath $Payload -Raw | ConvertFrom-Json).app_dir_limit
$Base = 'C:\БД тест '
$AppDir = $Base + ('я' * ($Limit - $Base.Length))  # exactly at the limit: Cyrillic, with a space
$TooLong = $AppDir + 'я'
$BangDir = 'C:\БД тест!'  # install-service.bat cannot use a path with `!`: Setup refuses it
$Key = 'win-x64-cp312'
$OldTag = 'v0.7.0'
$OldAsset = 'bamdude-0.7.0-windows-x64-setup.exe'
$OldSha256 = '53d4579e114a2f1622dc702118e91344503d929eb80b57fd30513414940563a2'  # the exact versioned asset
$OldDir = 'C:\BamDude070'
$SetupSeconds = 1500
$Csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$Stub = Join-Path $Work 'stubborn.exe'
$DefaultSddl = 'D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)'
$Results = [ordered]@{}
$script:InstalledStatus = $null
$Logs = Join-Path $env:GITHUB_WORKSPACE 'acceptance-logs'
$script:Resets = 0

function Assert([bool] $Condition, [string] $Message) { if (-not $Condition) { throw $Message } }

function Wait-Until([scriptblock] $Condition, [int] $Seconds, [string] $What) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while (-not (& $Condition)) {
        if ((Get-Date) -gt $deadline) { throw "$What within ${Seconds}s" }
        Start-Sleep -Seconds 2
    }
}

function Invoke-Setup {
    param([string] $Exe, [string] $Dir, [string] $Name)
    $log = Join-Path $Work "$Name.log"
    $arguments = @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', "/LOG=`"$log`"", "/DIR=`"$Dir`"")
    $p = Start-Process -FilePath $Exe -ArgumentList $arguments -PassThru
    $null = $p.Handle  # keeps the exit code readable after the wait
    if (-not $p.WaitForExit($SetupSeconds * 1000)) {
        & taskkill.exe /F /T /PID $p.Id | Out-Null
        throw "${Name}: Setup still ran after ${SetupSeconds}s; its tree was ended"
    }
    return $p.ExitCode
}

function Invoke-ServiceScript([string] $Name) {
    $script = Join-Path $AppDir 'service\install-service.bat'
    $arguments = @("`"$AppDir`"", "`"$Data`"", '8000', 'sqlite', '""')
    $p = Start-Process -FilePath $script -ArgumentList $arguments -PassThru -WindowStyle Hidden
    $null = $p.Handle
    if (-not $p.WaitForExit(900 * 1000)) {
        & taskkill.exe /F /T /PID $p.Id | Out-Null
        throw "${Name}: install-service.bat still ran after 900s; its tree was ended"
    }
    return $p.ExitCode
}

function Svc([string] $Name) { Get-Service -Name $Name -ErrorAction SilentlyContinue }

# 'Absent' for a service that is not there: under strict mode, reading Status off a missing one fails with a
# message about a property, not about the service.
function Svc-Status([string] $Name) {
    $service = Svc $Name
    if ($service) { return [string] $service.Status } else { return 'Absent' }
}

function Invoke-Uninstall([string] $Dir) {
    $uninstaller = Join-Path $Dir 'unins000.exe'
    Assert (Test-Path -LiteralPath $uninstaller) "no uninstaller in $Dir"
    $p = Start-Process -FilePath $uninstaller -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART') -PassThru
    $null = $p.Handle
    # The uninstaller hands over to a copy of itself in TEMP (_iu*.tmp) and returns at once: wait for its work
    # instead, and at the deadline end both, so nothing is left behind waiting.
    $deadline = (Get-Date).AddSeconds(600)
    while ((Test-Path -LiteralPath $uninstaller) -or (Svc BamDude)) {
        if ((Get-Date) -gt $deadline) {
            if (-not $p.HasExited) { & taskkill.exe /F /T /PID $p.Id | Out-Null }
            Get-Process -Name '_iu*' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
            throw 'the uninstall did not finish within 600s; the uninstaller was ended'
        }
        Start-Sleep -Seconds 2
    }
}

function Invoke-Http([string] $Mode) {
    & python (Join-Path $Repo 'scripts\ci\windows_installer_http.py') $Mode --state $State
    Assert ($LASTEXITCODE -eq 0) "http $Mode failed"
}

function Wait-Running([int] $Seconds = 240) {
    Wait-Until { (Svc-Status BamDude) -eq 'Running' } $Seconds 'BamDude was not RUNNING'
    Invoke-Http 'health'
}

function Install-Ok([string] $Exe, [string] $Dir, [string] $Name) {
    $code = Invoke-Setup $Exe $Dir $Name
    Assert ($code -eq 0) "${Name}: Setup exited $code"
    Wait-Running
}

function Bundle-Root([string] $Dir) { Join-Path $Dir 'app\runtime\step-preview' }

# The digests of the key, counted as @(Digests $Dir).Count: a function's one-element answer is unrolled to a
# string, and strict mode refuses .Count on it (S2/S3 of run 39131019056 failed on exactly that).
function Digests([string] $Dir) {
    @(Get-ChildItem -LiteralPath (Join-Path (Bundle-Root $Dir) $Key) -Directory | ForEach-Object Name)
}

function Plant-OldDigest {
    $site = Join-Path (Bundle-Root $AppDir) "$Key\$('e' * 32)\site"
    New-Item -ItemType Directory -Force $site | Out-Null
    Set-Content -LiteralPath (Join-Path $site 'x.dll') 'an older pin'
    return (Split-Path -Parent $site)
}

function Tree-Hash([string] $Dir) {
    $lines = Get-ChildItem -LiteralPath $Dir -Recurse -File -Force | Sort-Object FullName | ForEach-Object {
        '{0} {1}' -f $_.FullName.Substring($Dir.Length), (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    return [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes))
}

function Stop-RealService {
    if (-not (Svc BamDude)) { return }
    & sc.exe stop BamDude | Out-Null
    Wait-Until { (Svc-Status BamDude) -eq 'Stopped' } 180 'BamDude did not stop'
}

function Remove-Service([string] $Name) {
    $service = Get-CimInstance Win32_Service -Filter "Name='$Name'"
    if (-not $service) { return }
    if ($service.ProcessId) { & taskkill.exe /F /T /PID $service.ProcessId | Out-Null }
    & sc.exe delete $Name | Out-Null
    Wait-Until { -not (Svc $Name) } 120 "$Name was not deleted"
}

function New-Stub([string] $Sddl) {
    if (-not (Test-Path -LiteralPath $Stub)) {
        & $Csc /nologo "/out:$Stub" /r:System.ServiceProcess.dll (Join-Path $Repo 'installers\windows\tests\StubbornService.cs')
        Assert ($LASTEXITCODE -eq 0) 'the stub service did not compile'
    }
    & sc.exe create BamDude binPath= "`"$Stub`"" start= demand | Out-Null
    Assert ($LASTEXITCODE -eq 0) 'the stub service could not be created'
    if ($Sddl) {
        & sc.exe sdset BamDude $Sddl | Out-Null
        Assert ($LASTEXITCODE -eq 0) 'the stub service DACL could not be set'
    }
}

function Service-LogSince([long] $Mark) {
    $log = Join-Path $Data 'logs\install-service.log'
    if (-not (Test-Path -LiteralPath $log)) { return '' }
    $bytes = [IO.File]::ReadAllBytes($log)
    return [Text.Encoding]::Default.GetString($bytes, [Math]::Min($Mark, $bytes.Length), $bytes.Length - [Math]::Min($Mark, $bytes.Length))
}

function Service-LogMark {
    $log = Join-Path $Data 'logs\install-service.log'
    if (Test-Path -LiteralPath $log) { return (Get-Item -LiteralPath $log).Length } else { return 0 }
}

# Setup's logs and the service's, copied into the workspace for the logs artifact: a reset deletes both. Never
# throws — a log that could not be kept is a warning, not a failed acceptance.
function Save-Logs([string] $Label) {
    try {
        $target = Join-Path $Logs $Label
        New-Item -ItemType Directory -Force $target | Out-Null
        Get-ChildItem -LiteralPath $Work -Filter '*.log' -File | Copy-Item -Destination $target -Force
        $service = Join-Path $Data 'logs'
        if (Test-Path -LiteralPath $service) { Get-ChildItem -LiteralPath $service -File | Copy-Item -Destination $target -Force }
    } catch {
        Write-Host "::warning::the logs ($Label) were not kept: $($_.Exception.Message)"
    }
}

function Reset-Machine {
    $script:Resets += 1
    Save-Logs "before-reset-$($script:Resets)"
    foreach ($dir in @($AppDir, $OldDir)) {
        if (Test-Path -LiteralPath (Join-Path $dir 'unins000.exe')) { Invoke-Uninstall $dir }
    }
    Remove-Service BamDude
    Remove-Service BamDudePostgres
    foreach ($dir in @($AppDir, $TooLong, $BangDir, $OldDir, $Data)) {
        if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
    }
    Remove-Item -LiteralPath $State -ErrorAction SilentlyContinue
}

# --- the scenarios -------------------------------------------------------------------------------------------------

function S1 {  # a fresh install exactly at the directory limit, Cyrillic, with a space; a real STEP over HTTP
    Assert ($AppDir.Length -eq $Limit) 'the install directory is not at the limit'
    Install-Ok $Installer $AppDir 'S1'
    Invoke-Http 'step-ok'
    $receipt = Get-ChildItem -LiteralPath (Bundle-Root $AppDir) -Recurse -Filter receipt.json | Select-Object -First 1
    $script:InstalledStatus = (Get-Content -LiteralPath $receipt.FullName -Raw | ConvertFrom-Json).status
}

function S2 {  # a reinstall over the running service
    Install-Ok $Installer $AppDir 'S2'
    Assert (@(Digests $AppDir).Count -eq 1) 'more than one CAD digest after a reinstall'
}

function S3 {  # old CAD (same key, another Windows ABI) and a stale Node file go with a good reinstall
    $old = Plant-OldDigest
    $abi = Join-Path (Bundle-Root $AppDir) "win-x64-cp313\$('f' * 32)\site"
    New-Item -ItemType Directory -Force $abi | Out-Null
    Set-Content -LiteralPath (Join-Path $abi 'x.dll') 'another Python ABI'
    $stale = Join-Path $AppDir 'app\runtime\node\stale-from-an-old-pin.txt'
    Set-Content -LiteralPath $stale 'stale'
    Install-Ok $Installer $AppDir 'S3'
    Assert (-not (Test-Path -LiteralPath $old)) 'the old digest of this key was not pruned'
    Assert (-not (Test-Path -LiteralPath (Split-Path -Parent $abi))) 'the other Windows ABI was not pruned'
    Assert (-not (Test-Path -LiteralPath $stale)) 'the Node runtime was not replaced whole'
    Assert (@(Digests $AppDir).Count -eq 1) 'not exactly one CAD digest'
}

function S4 {  # a service that never finishes stopping: Setup refuses before touching the payload
    Stop-RealService
    Remove-Service BamDude
    New-Stub $null
    try {
        & sc.exe start BamDude | Out-Null
        Wait-Until { (Svc-Status BamDude) -eq 'Running' } 60 'the stub service did not start'
        $old = Plant-OldDigest
        $before = Tree-Hash $AppDir
        $code = Invoke-Setup $Installer $AppDir 'S4'
        Assert ($code -eq 7) "Setup exited $code, not 7"
        Assert ((& sc.exe query BamDude | Out-String) -match 'STOP_PENDING') 'the stub is not STOP_PENDING'
        Assert ((Tree-Hash $AppDir) -eq $before) 'the installed payload changed'
        Assert (Test-Path -LiteralPath $old) 'a CAD bundle was removed'
        Assert ((Get-Content -LiteralPath (Join-Path $Work 'S4.log') -Raw) -match 'did not reach STOPPED') 'the Setup log does not say why'
    } finally {
        Remove-Service BamDude
    }
    Install-Ok $Installer $AppDir 'S4-restore'
}

function S4b {  # a state that cannot be read is no stop, for the installer and for install-service.bat alike
    Stop-RealService
    Remove-Service BamDude
    New-Stub 'D:(D;;LC;;;BA)(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;BA)'
    try {
        & sc.exe query BamDude | Out-Null
        Assert ($LASTEXITCODE -eq 5) "sc query answered $LASTEXITCODE, not access denied"
        $before = Tree-Hash $AppDir
        $code = Invoke-Setup $Installer $AppDir 'S4b'
        Assert ($code -eq 7) "Setup exited $code, not 7"
        Assert ((Tree-Hash $AppDir) -eq $before) 'the installed payload changed'
        $setupLog = Get-Content -LiteralPath (Join-Path $Work 'S4b.log') -Raw
        Assert ($setupLog.Contains('BamDude could not be opened (error 5)')) 'the Setup log does not say the state was unreadable'
        $mark = Service-LogMark
        $rc = Invoke-ServiceScript 'S4b-script'
        Assert ($rc -eq 5) "install-service.bat exited $rc, not 5"
        $tail = Service-LogSince $mark
        Assert ($tail -match 'not proven stopped') 'install-service.log does not say why'
        Assert ($tail -notmatch 'CAD runtime|registered and started|init_embedded_pg|prune') 'install-service.bat went on'
    } finally {
        & sc.exe sdset BamDude $DefaultSddl | Out-Null
        Remove-Service BamDude
    }
    Install-Ok $Installer $AppDir 'S4b-restore'
}

function S5 {  # the verify fails: the service stays down on demand start, registered, and Setup says 42
    $bundle = Get-ChildItem -LiteralPath (Join-Path (Bundle-Root $AppDir) $Key) -Directory | Select-Object -First 1
    $unlisted = Join-Path $bundle.FullName 'site\OCP\acceptance_unlisted.txt'
    $old = Plant-OldDigest
    Set-Content -LiteralPath $unlisted 'in no RECORD'
    $mark = Service-LogMark
    $code = Invoke-Setup $Installer $AppDir 'S5'
    Assert ($code -eq 42) "Setup exited $code, not 42"
    Assert ((Svc-Status BamDude) -eq 'Stopped') 'BamDude is not stopped'
    Assert ((Get-CimInstance Win32_Service -Filter "Name='BamDude'").StartMode -eq 'Manual') 'BamDude is not on demand start'
    $params = 'HKLM:\SYSTEM\CurrentControlSet\Services\BamDude\Parameters'
    Assert ($null -ne (Get-ItemProperty -LiteralPath $params -Name AppEnvironmentExtra -ErrorAction SilentlyContinue)) 'the registration lost its settings'
    Assert ((Service-LogSince $mark) -match 'failed its verification') 'install-service.log does not name the verify'
    Assert (Test-Path -LiteralPath $old) 'an old digest was pruned although the verify failed'
    & sc.exe start BamDude | Out-Null
    Wait-Running  # the app lives (whether CAD renders is not this scenario's question: the verify is stricter)
    Stop-RealService
    $rc = Invoke-ServiceScript 'S5-script'
    Assert ($rc -eq 4) "install-service.bat exited $rc, not 4"
    Remove-Item -LiteralPath $unlisted -Force
    Install-Ok $Installer $AppDir 'S5-repair'
    Assert (-not (Test-Path -LiteralPath $old)) 'the old digest was not pruned after a good verify'
}

function S8 {  # a library the CAD needs vanishes while BamDude runs: STEP says cad_unavailable, STL still works
    Assert ((Svc-Status BamDude) -eq 'Running') 'BamDude is not running'
    $dll = Get-ChildItem -LiteralPath (Bundle-Root $AppDir) -Recurse -Filter 'TKernel*.dll' | Select-Object -First 1
    Assert ($null -ne $dll) 'no TKernel library in the bundle'
    Remove-Item -LiteralPath $dll.FullName -Force
    Assert (-not (Test-Path -LiteralPath $dll.FullName)) 'the library is still there'
    Invoke-Http 'step-cad-gone'
    Install-Ok $Installer $AppDir 'S8-repair'
    Invoke-Http 'step-ok'
}

function S6 {  # one unit past the limit: refused before anything is stopped or created
    Assert ($TooLong.Length -eq $Limit + 1) 'the too-long directory is not one past the limit'
    $code = Invoke-Setup $Installer $TooLong 'S6'
    Assert ($code -eq 7) "Setup exited $code, not 7"
    Assert (-not (Test-Path -LiteralPath $TooLong)) 'Setup created the too-long directory'
    Assert ((Svc-Status BamDude) -eq 'Running') 'a refused install stopped the service'
}

function S6b {  # a `!` in the install directory: refused before anything is stopped or created
    $code = Invoke-Setup $Installer $BangDir 'S6b'
    Assert ($code -eq 7) "Setup exited $code, not 7"
    Assert (-not (Test-Path -LiteralPath $BangDir)) 'Setup created the directory with a !'
    Assert ((Svc-Status BamDude) -eq 'Running') 'a refused install stopped the service'
}

function S7 {  # a silent uninstall keeps the data and opens no window
    Invoke-Uninstall $AppDir
    Assert (-not (Svc BamDude)) 'the service survived the uninstall'
    Assert (-not (Test-Path -LiteralPath (Join-Path $AppDir 'app'))) 'the app files survived the uninstall'
    Assert (Test-Path -LiteralPath (Join-Path $Data 'data')) 'a silent uninstall removed the data'
}

function S0 {  # an upgrade from the released v0.7.0: Node replaced, CAD added, a real STEP renders
    Reset-Machine
    & gh release download $OldTag --repo $env:GITHUB_REPOSITORY --pattern $OldAsset --dir $Work --clobber
    Assert ($LASTEXITCODE -eq 0) 'the v0.7.0 installer could not be downloaded'
    $old = Join-Path $Work $OldAsset
    Assert ((Get-FileHash -LiteralPath $old -Algorithm SHA256).Hash.ToLower() -eq $OldSha256) 'the v0.7.0 installer is not the recorded asset'
    Install-Ok $old $OldDir 'S0-v070'
    Assert (-not (Test-Path -LiteralPath (Bundle-Root $OldDir))) 'v0.7.0 already had a CAD runtime'
    Install-Ok $Installer $OldDir 'S0-upgrade'
    $node = Get-ChildItem -LiteralPath (Join-Path $OldDir 'app\runtime\node') -Recurse -Filter node.exe | Select-Object -First 1
    Assert ($null -ne $node) 'no Node runtime after the upgrade'
    & $node.FullName --version | Out-Null
    Assert ($LASTEXITCODE -eq 0) 'Node does not run after the upgrade'
    Invoke-Http 'step-ok'
}

# --- run --------------------------------------------------------------------------------------------------------

$Full = @('S1', 'S2', 'S3', 'S4', 'S4b', 'S5', 'S8', 'S6', 'S6b', 'S7', 'S0')
$Diagnostic = @('S1', 'S2', 'S6', 'S7')
$Run = if ($Scope -eq 'full') { $Full } else { $Diagnostic }
Reset-Machine
foreach ($name in $Run) {
    Write-Host "::group::$name"
    try {
        & $name
        $Results[$name] = 'passed'
        Write-Host "$name passed"
    } catch {
        $Results[$name] = "failed: $($_.Exception.Message)"
        Write-Host "::error::$name failed: $($_.Exception.Message)"
    }
    Write-Host '::endgroup::'
    if ($name -eq 'S1' -and $Results[$name] -ne 'passed') { break }  # nothing after it has an install to work on
}

Save-Logs 'final'
$passed = @($Results.Values | Where-Object { $_ -ne 'passed' }).Count -eq 0 -and $Results.Count -eq $Run.Count
$answer = [ordered]@{
    scope                    = $Scope
    full                     = ($Scope -eq 'full' -and $passed)
    sha256                   = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLower()
    installed_receipt_status = $script:InstalledStatus
    scenarios                = $Results
}
$answer | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Report -Encoding utf8
if ($env:GITHUB_OUTPUT) { "full=$($answer.full.ToString().ToLower())" | Add-Content -LiteralPath $env:GITHUB_OUTPUT }
if (-not $passed) { exit 1 }
