@echo off
REM Stop and deregister the BamDude services.
REM
REM Called from Inno Setup's [UninstallRun] section. Argument:
REM   %1 = install dir (e.g. C:\Program Files\BamDude)
REM
REM Removes the BamDude service and, if it was registered, the bundled
REM PostgreSQL service (BamDudePostgres). The data directory is NOT touched
REM here - the uninstaller asks about that separately.

setlocal enabledelayedexpansion

set "INSTALL_DIR=%~1"
set "NSSM=%INSTALL_DIR%\bin\nssm.exe"
set "PG_SERVICE=BamDudePostgres"
set "DATA_ROOT=%ProgramData%\BamDude"
set "LOG_DIR=%DATA_ROOT%\logs"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" 2>nul
set "UNINSTALL_LOG=%LOG_DIR%\uninstall-service.log"
echo ---- %DATE% %TIME% uninstall-service ---->> "%UNINSTALL_LOG%"
call :main >> "%UNINSTALL_LOG%" 2>&1

REM Always succeed: a service that is already gone must never stop the
REM uninstaller from removing files.
endlocal
exit /b 0

REM ===========================================================================
:main

REM BamDude first — it depends on the PostgreSQL service.
"%NSSM%" stop BamDude
"%NSSM%" remove BamDude confirm

sc query %PG_SERVICE% >nul 2>&1
if errorlevel 1 (
    echo [uninstall-service] %PG_SERVICE% is not installed - nothing to do
    goto :done
)

REM ⚠️ sc stop, never `net stop`. `net stop` asks whether to also stop services
REM that depend on this one, and this script runs hidden with no console input,
REM so that question hangs the uninstaller forever with PostgreSQL still up —
REM which is exactly what happened once. sc never prompts.
echo [uninstall-service] stopping %PG_SERVICE%
sc stop %PG_SERVICE%

set /a WAITED=0
:wait_pg
sc query %PG_SERVICE% | find "STOPPED" >nul
if not errorlevel 1 goto :pg_stopped
set /a WAITED+=2
if !WAITED! GEQ 90 goto :pg_force
REM ping as the sleep: `timeout` needs a console this script does not have.
ping -n 3 127.0.0.1 >nul
goto :wait_pg

:pg_force
echo [uninstall-service] still running after !WAITED!s - asking pg_ctl directly
set "PGCTL=%INSTALL_DIR%\python\Lib\site-packages\embedded_postgres\pginstall\bin\pg_ctl.exe"
if exist "%PGCTL%" "%PGCTL%" -D "%DATA_ROOT%\data\postgres\18" -m fast -w -t 60 stop

:pg_stopped
echo [uninstall-service] deleting %PG_SERVICE%
sc delete %PG_SERVICE%

:done
echo [uninstall-service] BamDude services deregistered
exit /b 0
