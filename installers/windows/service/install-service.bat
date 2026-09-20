@echo off
REM Register BamDude as a Windows service via NSSM, and set up the chosen
REM database backend.
REM
REM Called from Inno Setup's [Run] section. Arguments:
REM   %1 = install dir  (e.g. C:\Program Files\BamDude)
REM   %2 = data root    (e.g. C:\ProgramData\BamDude)
REM   %3 = web port     (e.g. 8000)
REM   %4 = db backend   (sqlite | embedded-service | embedded-child | external)
REM   %5 = database url  (only for %4 == external; a SQLAlchemy URL)
REM
REM Database backends:
REM   sqlite           - no DATABASE_URL; the app uses SQLite (the default).
REM   embedded-service - the bundled PostgreSQL runs as its OWN service,
REM                      BamDudePostgres, ordered before BamDude by the SCM.
REM                      Most robust; survives a BamDude crash. Needs admin
REM                      (this installer already runs elevated).
REM   embedded-child   - the bundled PostgreSQL is started/stopped by BamDude
REM                      itself, inside its process. Simplest; one service.
REM   external         - an external PostgreSQL server at the given URL.
REM
REM If the services already exist (re-install / upgrade) they are removed and
REM re-created so config from this build applies. Data is never deleted here.

setlocal enabledelayedexpansion

set "INSTALL_DIR=%~1"
set "DATA_ROOT=%~2"
set "PORT=%~3"
set "DB_MODE=%~4"
set "DB_URL=%~5"

if "%DB_MODE%"=="" set "DB_MODE=sqlite"

set "NSSM=%INSTALL_DIR%\bin\nssm.exe"
set "PYTHON=%INSTALL_DIR%\python\python.exe"
set "APP_DIR=%INSTALL_DIR%\app"
set "BIN_DIR=%INSTALL_DIR%\bin"
set "DATA_DIR=%DATA_ROOT%\data"
set "LOG_DIR=%DATA_ROOT%\logs"

REM Port of the bundled PostgreSQL. Pinned so advanced users can reach it with
REM psql / DBeaver; change it here (and, for embedded-service, re-run) if it
REM clashes. 6432 matches the example in .env.example.
set "PG_PORT=6432"
set "PG_SERVICE=BamDudePostgres"
set "PGDATA=%DATA_DIR%\postgres\18"

REM Environment pairs added to the BamDude service for the chosen backend.
set "DB_ENV="
if /I "%DB_MODE%"=="embedded-service" set "DB_ENV="DATABASE_URL=embedded" "EMBEDDED_PG_EXTERNAL_SERVICE=1" "EMBEDDED_PG_PORT=%PG_PORT%""
if /I "%DB_MODE%"=="embedded-child"   set "DB_ENV="DATABASE_URL=embedded" "EMBEDDED_PG_PORT=%PG_PORT%""
if /I "%DB_MODE%"=="external"         set "DB_ENV="DATABASE_URL=!DB_URL!""

REM Everything below runs through a log. Inno starts this script hidden and does
REM not stop on a non-zero exit, so without a log a failure here is completely
REM invisible: no service, no message, an empty data directory and a browser
REM pointed at nothing.
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" 2>nul
set "SETUP_LOG=%LOG_DIR%\install-service.log"
echo ---- %DATE% %TIME% install-service %DB_MODE% ---->> "%SETUP_LOG%"
call :main >> "%SETUP_LOG%" 2>&1
set "MAIN_RC=!errorlevel!"
if not "!MAIN_RC!"=="0" echo [install-service] FAILED with code !MAIN_RC! - see "%SETUP_LOG%"
endlocal & exit /b %MAIN_RC%

REM ===========================================================================
:main

REM ---------------------------------------------------------------------------
REM  embedded-service: initialise the cluster and register it as its own service
REM ---------------------------------------------------------------------------
if /I "%DB_MODE%"=="embedded-service" (
    call :setup_pg_service || exit /b 1
)

REM ---------------------------------------------------------------------------
REM  BamDude service (NSSM-wrapped uvicorn)
REM ---------------------------------------------------------------------------
"%NSSM%" stop BamDude 2>nul
"%NSSM%" remove BamDude confirm 2>nul

REM --loop asyncio: uvicorn[standard] auto-selects uvloop, which can truncate VP FTP uploads (#1896).
"%NSSM%" install BamDude "%PYTHON%" "-m uvicorn backend.app.main:app --host 0.0.0.0 --port %PORT% --loop asyncio"
if errorlevel 1 (
    echo [install-service] nssm install failed
    exit /b 1
)

"%NSSM%" set BamDude AppDirectory "%APP_DIR%"
"%NSSM%" set BamDude DisplayName "BamDude"
"%NSSM%" set BamDude Description "BamDude - local-first Bambu Lab printer manager"
"%NSSM%" set BamDude Start SERVICE_AUTO_START

REM Environment: DATA_DIR + LOG_DIR under ProgramData, our bin/ on PATH for
REM ffmpeg/ffprobe, plus the database backend selected above.
"%NSSM%" set BamDude AppEnvironmentExtra "DATA_DIR=%DATA_DIR%" "LOG_DIR=%LOG_DIR%" "PORT=%PORT%" "PATH=%BIN_DIR%;%PATH%" %DB_ENV%

REM embedded-child: BamDude starts and stops the server inside its own lifespan.
REM Give the console-stop a long window so the clean fast-shutdown completes
REM before NSSM would kill the tree.
if /I "%DB_MODE%"=="embedded-child" (
    "%NSSM%" set BamDude AppStopMethodConsole 90000
)

REM embedded-service: the SCM must start BamDudePostgres before BamDude, and
REM stop BamDude first on the way down.
if /I "%DB_MODE%"=="embedded-service" (
    "%NSSM%" set BamDude DependOnService %PG_SERVICE%
)

"%NSSM%" set BamDude AppStdout "%LOG_DIR%\service-stdout.log"
"%NSSM%" set BamDude AppStderr "%LOG_DIR%\service-stderr.log"
"%NSSM%" set BamDude AppRotateFiles 1
"%NSSM%" set BamDude AppRotateOnline 1
"%NSSM%" set BamDude AppRotateBytes 10485760

"%NSSM%" start BamDude
if errorlevel 1 (
    echo [install-service] nssm start failed - check %LOG_DIR%\service-stderr.log
    exit /b 1
)

echo [install-service] BamDude service registered and started on port %PORT% (database: %DB_MODE%)
exit /b 0

REM ===========================================================================
:setup_pg_service
REM Initialise the bundled PostgreSQL cluster and register it as its own
REM auto-start service running as NetworkService. Returns non-zero on failure.

REM Stop and unregister any previous instance so this build's config applies.
REM sc delete needs no PostgreSQL binaries (they aren't located yet) and leaves
REM the data directory untouched.
REM
REM ⚠️ BamDude first — it depends on %PG_SERVICE% (DependOnService, below), and
REM stopping a dependency while its dependent still runs is what makes the SCM
REM ask about dependents. See :stop_service for why that question is fatal here.
REM
REM ⚠️ And WAIT for stopped before sc delete: deleting a service that is still
REM running only marks it for deletion, and the name stays taken until reboot —
REM so the register below would fail on a machine that looks idle.
call :stop_service BamDude 60
call :stop_service %PG_SERVICE% 90
sc delete %PG_SERVICE% 2>nul

REM Locate the PostgreSQL binaries inside the embedded Python's wheel. Written
REM through a temp file rather than a for/f backtick block, whose nested quoting
REM around an interpreter path with spaces is a known way to get an empty result.
set "PGBIN="
set "PGBIN_TMP=%TEMP%\bamdude_pgbin.txt"
"%PYTHON%" -c "from embedded_postgres._commands import POSTGRES_BIN_PATH; print(POSTGRES_BIN_PATH)" > "%PGBIN_TMP%"
if exist "%PGBIN_TMP%" set /p PGBIN=<"%PGBIN_TMP%"
del "%PGBIN_TMP%" 2>nul
if not defined PGBIN (
    echo [install-service] could not locate the embedded PostgreSQL binaries
    exit /b 1
)
if not exist "%PGBIN%\pg_ctl.exe" (
    echo [install-service] pg_ctl.exe not found at "%PGBIN%"
    exit /b 1
)

REM Initialise the cluster (initdb + our conf, no server started) with the
REM application's own bootstrap, so the flags and the conf have one source of
REM truth. Skips initdb automatically if the cluster already exists.
pushd "%APP_DIR%"
set "DATA_DIR=%DATA_DIR%"
set "DATABASE_URL=embedded"
set "EMBEDDED_PG_PORT=%PG_PORT%"
"%PYTHON%" -m backend.app.cli init_embedded_pg
set "INIT_RC=!errorlevel!"
popd
if not "!INIT_RC!"=="0" (
    echo [install-service] init_embedded_pg failed
    exit /b 1
)

REM A registered service has no controlling terminal, so send its log to a
REM file via the logging collector (the child-process path uses pg_ctl -l).
findstr /C:"logging_collector" "%PGDATA%\bamdude.conf" >nul 2>&1 || (
    echo.>> "%PGDATA%\bamdude.conf"
    echo # Written by install-service.bat for the BamDudePostgres service.>> "%PGDATA%\bamdude.conf"
    echo logging_collector = on>> "%PGDATA%\bamdude.conf"
    echo log_directory = 'log'>> "%PGDATA%\bamdude.conf"
    echo log_filename = 'postgresql-%%Y-%%m-%%d.log'>> "%PGDATA%\bamdude.conf"
)

REM Let the service account read and write the cluster and the password file.
icacls "%DATA_DIR%\postgres" /grant "NT AUTHORITY\NetworkService:(OI)(CI)F" /T /Q

REM Register + start the server as its own auto-start service.
call :pg_ctl register -N %PG_SERVICE% -D "%PGDATA%" -S auto -U "NT AUTHORITY\NetworkService"
if errorlevel 1 (
    echo [install-service] pg_ctl register failed
    exit /b 1
)
sc config %PG_SERVICE% start= auto >nul
net start %PG_SERVICE%
if errorlevel 1 (
    echo [install-service] %PG_SERVICE% failed to start - check %PGDATA%\log
    exit /b 1
)
echo [install-service] %PG_SERVICE% registered and started on 127.0.0.1:%PG_PORT%
exit /b 0

REM ---------------------------------------------------------------------------
REM Ask the SCM to stop a service and wait until it really is stopped.
REM   %1 = service name, %2 = seconds to wait before giving up
REM
REM ⚠️ sc stop, never `net stop`. `net stop` asks whether to also stop the
REM services that depend on this one, and this script runs hidden with no
REM console input, so that question hangs the installer forever with the
REM service still up. uninstall-service.bat carries the same warning — it hit
REM this once already; the install path was simply never updated to match.
REM
REM ⚠️ sc returns as soon as the control code is delivered, NOT when the
REM service has stopped, so the poll is the point of this routine rather than
REM a nicety. `sc query | find "STOPPED"` is the only state signal an errorlevel
REM can carry.
:stop_service
sc query %~1 >nul 2>&1
if errorlevel 1 exit /b 0
echo [install-service] stopping %~1
sc stop %~1 >nul 2>&1
set /a SVC_WAITED=0
:stop_service_wait
sc query %~1 | find "STOPPED" >nul
if not errorlevel 1 exit /b 0
set /a SVC_WAITED+=2
if !SVC_WAITED! GEQ %~2 (
    echo [install-service] %~1 still running after !SVC_WAITED!s
    exit /b 1
)
REM ping as the sleep: `timeout` needs a console this script does not have.
ping -n 3 127.0.0.1 >nul
goto :stop_service_wait

:pg_ctl
"%PGBIN%\pg_ctl.exe" %*
exit /b %errorlevel%
