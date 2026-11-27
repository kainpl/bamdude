# BamDude Windows Installer

Builds a self-contained Windows installer (`.exe`) for BamDude: embedded
Python 3.12 distribution + pre-built frontend + NSSM-supervised Windows
service. No Python or Node installation required on the target machine.

## Architecture

- **Install target:** `C:\Program Files\BamDude\`
- **Data target:** `C:\ProgramData\BamDude\data\` (preserved on uninstall by default)
- **Logs target:** `C:\ProgramData\BamDude\logs\`
- **Service:** registered via NSSM, runs as `LocalSystem`, autostart on boot
- **Service command:** `python.exe -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --loop asyncio --timeout-graceful-shutdown 15`
- **Bundled binaries:** Python 3.12 embeddable, NSSM, ffmpeg static build, the pinned Node.js render runtime
  (`app\runtime\node`) and the STEP-preview CAD bundle (OpenCascade through OCP, under
  `app\runtime\step-preview\<key>\<digest>`; its licence notices beside the backend in
  `backend\app\data\step_runtime_notices`)

Browser is the UI. Start Menu shortcut opens `http://localhost:8000`.

## Why these choices

Short version: PowerShell install scripts can't survive environmental drift
across the Windows host fleet, so we ship a self-contained bundle that
depends on nothing on the host. Inno Setup + embedded Python is the
lowest-maintenance path that delivers native-app UX. No Tauri/Electron
launcher in v1 — browser-as-UI matches every other BamDude platform.

## Build prerequisites

The build runs on Windows (or in a Windows GitHub Actions runner). Cross-
building from Linux is possible via Wine but not officially supported.

- Windows 10/11 x64 (or `windows-latest` GitHub Actions runner)
- Python 3.11+ (for running `build.py`; the embedded Python that ships
  in the installer is downloaded fresh by the build script)
- Node.js 22 LTS + npm (for building the frontend bundle)
- [Inno Setup 6](https://jrsoftware.org/isdl.php) (for compiling
  `bamdude.iss` → `.exe`)
- `psutil` for the Python that runs `build.py` (the specifier in `requirements.txt`): the build owns every
  call of the CAD helper in the embedded Python as one whole process tree through the backend's launcher

The build script downloads everything else automatically (embedded Python,
NSSM, ffmpeg).

## Build steps

```cmd
:: From the repo root on a Windows machine
cd installers\windows
python build.py
:: Then open bamdude.iss in Inno Setup Compiler and click Build → Compile
:: (or invoke ISCC.exe directly:)
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" bamdude.iss
```

Output: `installers\windows\build\output\bamdude-<version>-windows-x64-setup.exe`

The CAD bundle is installed into a build root of its own (`build\cad-build`, reused while the pin stays the
same) and only the verified bundle is exported into the payload, where it is verified again — a real STEP
turned into a PNG by the embedded Python. While the STEP-preview pin is a candidate (before its release
repin) a development build needs `python build.py --allow-candidate-cad`; a build of a tag refuses it.
`version.iss` carries `StepMaxAppDirLen`, the longest install directory the payload allows: every native
library (DLL, `.pyd`) under the Windows loader's own limit of 250 characters, which is lower than
MAX_PATH, and every other file under MAX_PATH; `bamdude.iss` does not compile without it.

## What Setup does on the target machine

- **Before anything is copied** (`PrepareToInstall`): an install directory longer than `StepMaxAppDirLen` is
  refused; then the BamDude service must be proven absent or stopped — its state is read from the Service
  Control Manager, and a service that does not stop within 100 seconds, or whose state cannot be read, stops
  the install with nothing changed (exit code 7 when silent).
- Only the Node runtime is replaced whole; an older CAD bundle stays until the new one has passed.
- `service\install-service.bat`: a proven stop of BamDude, then the CAD bundle's full verify, and only then the
  PostgreSQL setup and the service registration. Exit codes: 0 started; 1 NSSM or PostgreSQL setup failed;
  4 the CAD bundle failed its verify (the service stays stopped, on demand start, its settings kept);
  5 BamDude could not be proven stopped (nothing changed). Old CAD bundles are removed after a good verify.
- After the install Setup exits 41 when the service is not registered and 42 when it is registered but not
  running, each with a message `/SUPPRESSMSGBOXES` silences; `C:\ProgramData\BamDude\logs\install-service.log`
  says why. After a 42, start the service by hand only if you accept that the 3D preview component may not
  work; running the installer again re-copies the files and repairs it.
- A silent uninstall keeps the data; the interactive one asks.

## Signing

**Every build is unsigned, release builds included.** Windows
SmartScreen shows "Windows protected your PC" on first run; **More
info** → **Run anyway** proceeds. There is no signing step in CI and no
certificate to add one with. (An earlier version of this file repeated
upstream Bambuddy's "SignPath application in flight", which came along
with the port of their installer pipeline and covered *their* project;
BamDude's own application was withdrawn in 2026-09 — the programme asks
for a publication footprint the project does not have yet, and nothing
here depends on it.)

If a certificate ever arrives, the signing step goes between ISCC and
the release upload, and two things in this folder have to change first:
the installer must show the privacy policy and offer to disable
telemetry at install time, and the `.exe` needs explicit `VersionInfo*`
metadata (Inno defaults the binary version to `0.0.0.0`).

## CI build

See `.github/workflows/windows-installer.yml`: build → acceptance → release. The acceptance installs the real
`.exe` on a disposable Windows runner (`scripts/ci/windows_installer_acceptance.ps1`: a fresh install, reinstalls,
a service that will not stop, a failed verify, a missing CAD library, a too-long directory, a silent
uninstall, an upgrade from v0.7.0, and a real STEP rendered by the installed service). Only a pushed tag
publishes, only after the full acceptance, and only the artifact that was accepted, with the release pin
(`scripts/ci/check_release_payload.py`). A manual dispatch can build a candidate from a branch and run the
full or a diagnostic acceptance; it never publishes.

## Known limitations / open questions

- **VP feature on Windows:** the Virtual Printer needs to bind 322/990/8883
  (privileged ports). Service runs as LocalSystem which can bind these
  ports, but the user's Windows Firewall will prompt on first VP enable.
  Documenting this is TBD.
- **Spoolman:** explicitly NOT bundled in v1. Users who want Spoolman
  install it separately. BamDude internal-inventory mode is the default
  on Windows.
- **Bundle size:** the installer is about 305 MiB (v0.7.0: 253 MiB); the CAD bundle adds about 167 MB
  installed. Measured on the build of 2026-10-18.
- **Updates:** an in-place upgrade over a running install is part of the release acceptance (from the
  released v0.7.0, and a reinstall over itself).
- **Windows on ARM:** no native build; the x64 installer under emulation is not tested.
