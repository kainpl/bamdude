@echo off
REM Stop and deregister the BamDude Windows service.
REM
REM Called from Inno Setup's [UninstallRun] section. Argument:
REM   %1 = install dir (e.g. C:\Program Files\BamDude)

setlocal

set "INSTALL_DIR=%~1"
set "NSSM=%INSTALL_DIR%\bin\nssm.exe"

REM Stop is best-effort — if the service is already stopped, NSSM
REM returns non-zero and we want to proceed to the remove step.
"%NSSM%" stop BamDude 2>nul

REM Remove the service registration. confirm flag skips the
REM interactive prompt.
"%NSSM%" remove BamDude confirm 2>nul

echo [uninstall-service] BamDude service deregistered
endlocal
exit /b 0
