# BamDude Windows Installer

Builds a self-contained Windows installer (`.exe`) for BamDude: embedded
Python 3.12 distribution + pre-built frontend + NSSM-supervised Windows
service. No Python or Node installation required on the target machine.

## Architecture

- **Install target:** `C:\Program Files\BamDude\`
- **Data target:** `C:\ProgramData\BamDude\data\` (preserved on uninstall by default)
- **Logs target:** `C:\ProgramData\BamDude\logs\`
- **Service:** registered via NSSM, runs as `LocalSystem`, autostart on boot
- **Service command:** `python.exe -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --loop asyncio`
- **Bundled binaries:** Python 3.12 embeddable, NSSM, ffmpeg static build

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

Output: `installers\windows\build\output\bamdude-windows-setup.exe`

## Signing

**Every build is unsigned so far, release builds included.** Windows
SmartScreen shows "Windows protected your PC" on first run; **More
info** → **Run anyway** proceeds. There is no signing step in CI yet and
no certificate to add one with.

BamDude applied to the SignPath Foundation OSS programme on 2026-09-06
(the project's own application — an earlier version of this file
repeated upstream Bambuddy's "in flight as of 2026-06-10", which came
along with the port of their installer pipeline and covered *their*
project, not this one). The public policy lives in the root README under
**Code signing policy**. Once approved, the signing step goes between
ISCC and the release upload; SignPath signs a GitHub *artifact*, so the
unsigned `.exe` is uploaded first and the signed copy is pulled back
into place. Two of SignPath's conditions still need work in this folder
before the first signed build: the installer must show the privacy
policy and offer to disable telemetry at install time, and the `.exe`
needs explicit `VersionInfo*` metadata (Inno defaults the binary version
to `0.0.0.0`, which SignPath's metadata restrictions reject).

## CI build

See `.github/workflows/windows-installer.yml` for the automated build.
The workflow runs on every tag matching `v*` and uploads the installer
as a release asset.

## Known limitations / open questions

- **VP feature on Windows:** the Virtual Printer needs to bind 322/990/8883
  (privileged ports). Service runs as LocalSystem which can bind these
  ports, but the user's Windows Firewall will prompt on first VP enable.
  Documenting this is TBD.
- **Spoolman:** explicitly NOT bundled in v1. Users who want Spoolman
  install it separately. BamDude internal-inventory mode is the default
  on Windows.
- **Bundle size:** estimated 250–350MB installed (mostly opencv +
  ffmpeg + matplotlib). Acceptable for a v1; can investigate slimming
  later if users complain.
- **Updates:** v1 ships as a fresh install / uninstall + install cycle.
  In-place upgrade via the same installer is supported by Inno Setup but
  needs end-to-end testing before we promise it.
