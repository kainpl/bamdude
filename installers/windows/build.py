"""Build script for the BamDude Windows installer.

Stages all artifacts under ``installers/windows/build/staging/`` for the
Inno Setup compiler to package. Run this on Windows (or in a Windows CI
runner) — it pip-installs BamDude's deps against the embedded Python it
downloads, which requires the matching platform.

Steps:
    1. Download python.org embeddable distribution for Windows x64
    2. Configure embedded Python (allow site-packages)
    3. Bootstrap pip into the embedded distribution
    4. Install ``requirements.txt`` into the embedded Python
    5. Build the React frontend (``frontend/npm run build``)
    6. Stage backend source + frontend bundle
    7. Download NSSM
    8. Download ffmpeg static build for Windows
    9. Print "ready for ISCC" message

After this script succeeds, run::

    "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe" bamdude.iss

to produce the final installer .exe under ``build/output/``.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Repo root: installers/windows/build.py -> ../../
REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER_DIR = Path(__file__).resolve().parent
BUILD_DIR = INSTALLER_DIR / "build"
STAGING = BUILD_DIR / "staging"
DOWNLOADS = BUILD_DIR / "downloads"
# The STEP-preview CAD bundle is installed into a build root of its own — its lock and staging never reach the
# payload — and only the bundle `locate` names is exported into staging (spec stage B R5). The records of the
# helper's whole tree (owner, launch, bootstrap) live beside it, so the next build can prove that tree over (R4).
CAD_ROOT = BUILD_DIR / "cad-build"
CAD_OWNER = BUILD_DIR / "cad-owner"
CAD_INSTALL_SECONDS = 15 * 60 + 300  # step_runtime.INSTALL_SECONDS plus the proof of the tree's end
CAD_VERIFY_SECONDS = 600  # a full rehash plus the STEP->PNG smoke (two children of up to 120 s each)
# The target's limits, whatever the build host: any file under MAX_PATH less its NUL, and a native library under the
# loader's own limit, which is lower — step_runtime.PATH_LIMIT on Windows (measured 2026-10-18 in the installer
# acceptance: a DLL at 259 did not load, at 250 it does, at 251 it does not).
WINDOWS_PATH_LIMIT = 259
WINDOWS_LIBRARY_PATH_LIMIT = 250
NATIVE_SUFFIXES = (".dll", ".pyd")

# Matches the Dockerfile (python:3.12-slim-trixie) and `requires-python`, so a
# Windows install runs the same interpreter CI tests. Bump when those bump.
#
# 3.12.10 specifically: it is the last 3.12 with a Windows embeddable build.
# Once a branch goes security-only, python.org publishes source tarballs but no
# binaries, so a later 3.12.x here would 404 at download time.
PYTHON_VERSION = "3.12.10"
PYTHON_EMBED_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"

# NSSM 2.24 is the long-time stable build (no new release since 2014).
# Vendored under installers/windows/vendor/nssm.exe rather than fetched
# at build time — nssm.cc has flaked with 503s mid-CI-run before, and
# pinning to a checked-in binary makes builds reproducible and lets us
# inspect the binary in PRs if it ever needs updating. SHA-256:
#   f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97
NSSM_VERSION = "2.24"

# ffmpeg static build. BtbN's gyan-equivalent build is the most reliable
# automated source. Pin to a release tag so builds are reproducible.
FFMPEG_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"

# get-pip.py for bootstrapping pip into the embedded distribution
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# C++ runtime DLLs the embeddable distribution does NOT ship. The python.org
# embeddable zip includes vcruntime140.dll but not vcruntime140_1.dll or
# msvcp140.dll. python312.dll is pure C and only needs vcruntime140.dll, so
# python.exe starts fine — but greenlet's _greenlet.pyd is C++ and needs
# vcruntime140_1.dll (table-based exception handling). On a fresh Windows box
# that never had the VC++ 2015-2022 redistributable installed, loading greenlet
# fails with "DLL load failed ... The specified module could not be found",
# SQLAlchemy's async engine can't start, init_db() raises, and the app never
# binds its port — the service shows "running" but the dashboard refuses the
# connection. These runtime DLLs are redistributable, so we ship them
# app-locally next to python.exe (where vcruntime140.dll already lives).
VCRUNTIME_DLLS = ("vcruntime140_1.dll", "msvcp140.dll")


def log(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


# Three artifacts are fetched from three different hosts (python.org, PyPA,
# GitHub's release CDN) in the one code path that runs on a release tag. That is
# the worst possible moment for a network blip and there was nothing to absorb
# one: v0.5.2's installer failed outright because the CDN closed the connection
# mid-transfer on ffmpeg, and a re-run of the whole job was the only remedy.
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_TIMEOUT = 60  # seconds without progress on a single connection


def download(url: str, dest: Path) -> Path:
    """Download ``url`` to ``dest`` if not already present.

    Transient failures are retried with a widening pause. An HTTP 4xx is not:
    the request is wrong, and asking three more times will be wrong three more
    times. 408 and 429 are the exceptions — those are the server telling us to
    come back later, which is precisely what a retry does.
    """
    if dest.exists():
        log(f"already downloaded: {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Written beside the target and renamed only once the transfer finished.
    # The `dest.exists()` check above accepts any file as complete, so a
    # half-written one left behind by an interrupted build would be unzipped
    # into the installer as though it were whole — and a truncated ffmpeg.zip
    # fails far from here, in a way that does not name the download.
    partial = dest.parent / (dest.name + ".part")
    partial.unlink(missing_ok=True)

    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        suffix = f" (attempt {attempt}/{_DOWNLOAD_ATTEMPTS})" if attempt > 1 else ""
        log(f"downloading {url}{suffix}")
        try:
            with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as resp, open(partial, "wb") as f:  # noqa: S310 — pinned URLs
                shutil.copyfileobj(resp, f)
            partial.replace(dest)
            return dest
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                raise
            failure: Exception = exc
        except (OSError, http.client.HTTPException) as exc:
            # Covers RemoteDisconnected, connection resets, DNS blips and read
            # timeouts. urllib.error.URLError is an OSError, so it lands here.
            failure = exc

        partial.unlink(missing_ok=True)
        if attempt == _DOWNLOAD_ATTEMPTS:
            raise RuntimeError(f"could not download {url} after {_DOWNLOAD_ATTEMPTS} attempts: {failure}") from failure
        pause = 2**attempt
        log(f"  {type(failure).__name__}: {failure} — retrying in {pause}s")
        time.sleep(pause)

    raise AssertionError("unreachable")  # pragma: no cover


def unzip(zip_path: Path, dest: Path) -> None:
    log(f"unzipping {zip_path.name} -> {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)


def configure_python_path(target: Path) -> None:
    """Pin sys.path for the embedded distribution via its ``._pth`` file.

    Applied on every build, including one that reuses an already-staged tree —
    otherwise a newly added path entry would silently miss the reused payload.
    """
    # Edit pythonXY._pth to allow site-packages. The embedded distribution
    # ships with `import site` commented out — uncomment it so pip-installed
    # packages in Lib\site-packages are importable.
    pth_files = list(target.glob("python3*._pth"))
    if not pth_files:
        raise RuntimeError(f"no python3*._pth file found in {target}")
    pth = pth_files[0]
    content = pth.read_text(encoding="utf-8")
    content = content.replace("#import site", "import site")
    # Also add Lib\site-packages explicitly. The embedded distribution
    # doesn't include this path by default even with `import site` enabled.
    if "Lib\\site-packages" not in content and "Lib/site-packages" not in content:
        content = content.rstrip() + "\nLib\\site-packages\n"
    # And the application tree itself, so `backend` is importable. A ._pth file
    # pins sys.path exactly: the working directory is NOT added and PYTHONPATH is
    # ignored, so `python -m backend.app.cli ...` failed with "No module named
    # backend" on every install — which broke the documented reset_admin recovery
    # and, once the installer started calling the CLI, the PostgreSQL service
    # setup. The service only worked because uvicorn inserts the working
    # directory itself. Entries are relative to this file: python\ -> ..\app.
    if "..\\app" not in content:
        content = content.rstrip() + "\n..\\app\n"
    pth.write_text(content, encoding="utf-8")


def stage_embedded_python(reuse: bool = False) -> Path:
    """Download and configure the embedded Python distribution.

    ``reuse`` keeps an already-staged tree instead of re-extracting it. Without
    it, ``--skip-pip`` was a trap: this function wiped the tree every run, so
    skipping the install left a Python holding nothing but pip and setuptools —
    and the installer compiled and shipped with no application dependencies at
    all, silently.
    """
    target = STAGING / "python"
    if reuse and (target / "python.exe").exists():
        log(f"reusing already-staged Python at {target}")
        configure_python_path(target)
        return target
    if target.exists():
        shutil.rmtree(target)

    zip_path = download(
        PYTHON_EMBED_URL,
        DOWNLOADS / f"python-{PYTHON_VERSION}-embed-amd64.zip",
    )
    unzip(zip_path, target)

    configure_python_path(target)

    # Bootstrap pip
    get_pip = download(GET_PIP_URL, DOWNLOADS / "get-pip.py")
    log("bootstrapping pip into embedded Python")
    subprocess.run(
        [str(target / "python.exe"), str(get_pip), "--no-warn-script-location"],
        check=True,
    )

    # Install setuptools + wheel. The embedded distribution ships without
    # them, and get-pip.py installs only pip — but pip needs
    # ``setuptools.build_meta`` (PEP 517 backend) to build any source-only
    # package. requirements.txt hit this with pyftpdlib 2.2.0 (sdist-only on
    # PyPI) until that dependency was dropped; any future source-only package
    # would fail the same way without this step, so it stays.
    log("installing setuptools + wheel for PEP 517 builds")
    subprocess.run(
        [
            str(target / "python.exe"),
            "-m",
            "pip",
            "install",
            "--no-warn-script-location",
            "setuptools",
            "wheel",
        ],
        check=True,
    )

    return target


def stage_vcruntime(python_dir: Path) -> None:
    """Ship the C++ runtime DLLs the embeddable distribution omits.

    Placed next to python.exe so the extension-module loader (which searches the
    interpreter's own directory) finds them without a redistributable install on
    the target machine. Prefers vendored copies under installers/windows/vendor/
    for reproducibility; falls back to the build runner's System32, where the
    redistributable runtime lives. Fails loudly if neither source has them, so a
    misconfigured build machine is caught here instead of by end users (a silent
    miss reproduces the greenlet-load crash on a fresh Win10 box).
    """
    vendor = INSTALLER_DIR / "vendor"
    system32 = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32"
    for dll in VCRUNTIME_DLLS:
        dst = python_dir / dll
        if dst.exists():
            log(f"{dll} already present in embedded Python")
            continue
        src = vendor / dll if (vendor / dll).exists() else system32 / dll
        if not src.exists():
            raise RuntimeError(
                f"required C++ runtime DLL not found: looked in {vendor} and {system32} "
                f"for {dll}. Install the Microsoft Visual C++ 2015-2022 Redistributable "
                f"(x64) on the build machine, or vendor {dll} under installers/windows/vendor/."
            )
        log(f"staging {dll} from {src}")
        shutil.copy(src, dst)


def install_requirements(python_dir: Path) -> None:
    """Install BamDude's requirements.txt into the embedded Python."""
    py = python_dir / "python.exe"
    requirements = REPO_ROOT / "requirements.txt"
    log(f"installing requirements.txt into {python_dir}")
    subprocess.run(
        [
            str(py),
            "-m",
            "pip",
            "install",
            "--no-warn-script-location",
            "-r",
            str(requirements),
        ],
        check=True,
    )


def verify_requirements(python_dir: Path) -> None:
    """Refuse to build a payload whose interpreter has no application packages.

    A staged Python missing its dependencies still compiles into a perfectly
    valid-looking installer that dies on the target machine at first import, so
    this runs on every build, not only after a pip install.
    """
    py = python_dir / "python.exe"
    names = ["fastapi", "sqlalchemy", "aiogram", "uvicorn", "asyncpg", "embedded-postgres", "embedded-nats", "nats-py"]
    script = (
        "import sys\n"
        "from importlib.metadata import version, PackageNotFoundError\n"
        "missing = []\n"
        "for name in sys.argv[1:]:\n"
        "    try:\n"
        "        version(name)\n"
        "    except PackageNotFoundError:\n"
        "        missing.append(name)\n"
        "print(','.join(missing))\n"
    )
    result = subprocess.run([str(py), "-c", script, *names], capture_output=True, text=True, check=True)
    missing = [name for name in result.stdout.strip().split(",") if name]
    if missing:
        raise RuntimeError(
            f"staged Python is missing {', '.join(missing)} — the installer would ship without them. "
            "Re-run without --skip-pip."
        )
    # The bundled PostgreSQL is binaries, not just an importable module.
    pg_bin = python_dir / "Lib" / "site-packages" / "embedded_postgres" / "pginstall" / "bin"
    for exe in ("postgres.exe", "initdb.exe", "pg_ctl.exe"):
        if not (pg_bin / exe).exists():
            raise RuntimeError(f"bundled PostgreSQL is incomplete: {pg_bin / exe} is missing")
    subprocess.run([str(py), "-c", "from embedded_nats import binary_path; assert binary_path().is_file()"], check=True)
    log(f"verified staged Python: {len(names)} key packages + PostgreSQL/NATS binaries present")


def build_frontend() -> Path:
    """Run ``npm ci && npm run build`` and return the build output path.

    Vite is configured with ``outDir: '../static'`` (see
    ``frontend/vite.config.ts``), so the bundle lands at ``<repo>/static/``
    — NOT ``frontend/dist/``. The path matches the runtime expectation in
    ``backend/app/core/config.py`` (``static_dir = _app_dir / "static"``).
    """
    frontend = REPO_ROOT / "frontend"
    dist = REPO_ROOT / "static"
    log("running npm ci in frontend/")
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found on PATH — install Node.js 22 LTS")
    subprocess.run([npm, "ci"], cwd=frontend, check=True, shell=False)
    log("running npm run build in frontend/")
    subprocess.run([npm, "run", "build"], cwd=frontend, check=True, shell=False)
    if not dist.exists():
        raise RuntimeError(f"expected frontend build output at {dist}")
    return dist


def build_ai_packages() -> Path:
    """Build immutable client downloads alongside the installer payload."""
    adapter = REPO_ROOT / "integrations" / "mcp-adapter"
    output = BUILD_DIR / "ai-plugins"
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found on PATH — install Node.js 22 LTS")
    subprocess.run([npm, "ci", "--ignore-scripts"], cwd=adapter, check=True, shell=False)
    subprocess.run([npm, "run", "build", "--", "--out", str(output)], cwd=adapter, check=True, shell=False)
    return output


def stage_backend(frontend_dist: Path, ai_packages: Path | None = None) -> None:
    """Copy backend source + frontend bundle into the staging tree.

    The runtime layout under STAGING/app/ mirrors a BamDude checkout:
    ``backend/`` (source), ``static/`` (frontend bundle served by FastAPI).
    """
    app = STAGING / "app"
    if app.exists():
        shutil.rmtree(app)
    app.mkdir(parents=True)

    # Backend source — copy the package tree, skip caches/tests/migrations
    log("staging backend source")
    shutil.copytree(
        REPO_ROOT / "backend",
        app / "backend",
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "tests",
            ".pytest_cache",
        ),
    )

    # The MCP guide and portable skill share these canonical sources.
    shutil.copytree(REPO_ROOT / "integrations" / "agent-skills", app / "integrations" / "agent-skills")
    if ai_packages is not None:
        shutil.copytree(ai_packages, app / "integrations" / "ai-plugins" / "dist")

    # Frontend bundle — FastAPI's StaticFiles mounts from app/static.
    # Strip macOS metadata files (.DS_Store, ._.*) that the dev box leaks
    # in; they'd just bloat the installer and never be served anyway.
    log("staging frontend bundle")
    shutil.copytree(
        frontend_dist,
        app / "static",
        ignore=shutil.ignore_patterns(".DS_Store", "._*"),
    )

    # gcode_viewer/ is a vendored 3D-preview iframe served via explicit
    # routes in main.py (looked up via static_dir.parent / "gcode_viewer").
    # In the staged layout STAGING/app/static/'s sibling is STAGING/app/,
    # so place the directory next to static/ to match runtime resolution.
    gcode_viewer_src = REPO_ROOT / "gcode_viewer"
    if gcode_viewer_src.exists():
        log("staging gcode_viewer/")
        shutil.copytree(
            gcode_viewer_src,
            app / "gcode_viewer",
            ignore=shutil.ignore_patterns(".DS_Store", "._*"),
        )


def stage_nssm() -> None:
    target = STAGING / "bin"
    target.mkdir(parents=True, exist_ok=True)
    # Vendored binary — no network fetch at build time
    src = INSTALLER_DIR / "vendor" / "nssm.exe"
    if not src.exists():
        raise RuntimeError(f"vendored NSSM binary missing at {src} — was it committed?")
    log(f"staging nssm.exe from {src}")
    shutil.copy(src, target / "nssm.exe")


def stage_ffmpeg() -> None:
    target = STAGING / "bin"
    target.mkdir(parents=True, exist_ok=True)
    zip_path = download(FFMPEG_URL, DOWNLOADS / "ffmpeg-win64-gpl.zip")
    extract = DOWNLOADS / "ffmpeg-extracted"
    if not extract.exists():
        unzip(zip_path, extract)
    src = next(extract.rglob("bin/ffmpeg.exe"))
    log(f"staging ffmpeg.exe from {src}")
    shutil.copy(src, target / "ffmpeg.exe")
    # ffprobe is used by some camera/timelapse paths
    ffprobe = next(extract.rglob("bin/ffprobe.exe"), None)
    if ffprobe is not None:
        shutil.copy(ffprobe, target / "ffprobe.exe")


def stage_render_runtime() -> None:
    """Pinned Node.js into app\\runtime (spec §6.2).

    Lands under build\\staging\\app, which [Files] already packs recursively,
    so the installer needs no new entry. Verified against the manifest's sha256.
    Runs after stage_backend(), which recreates STAGING/app from scratch.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from backend.app.services.render_runtime import provision

    target = provision(STAGING / "app" / "runtime", platform="win-x64")
    log(f"staged render runtime at {target}")


def _backend():
    """step_runtime, part_render_tree and the whole-tree launcher, imported from the checkout (as
    stage_render_runtime imports render_runtime). The build host needs psutil for them."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from backend.app import step_runtime
    from backend.app.services import part_render_tree, step_runtime_child

    return step_runtime, part_render_tree, step_runtime_child


def cad_candidate_allowed(flag: bool, env) -> bool:
    """A candidate pin only on an explicit request, and never on a tag: any refs/tags/* ships the release pin,
    whatever an input or the environment says (spec stage B R6)."""
    wanted = bool(flag) or env.get("STEP_PREVIEW_CANDIDATE") == "1"
    if wanted and env.get("GITHUB_REF", "").startswith("refs/tags/"):
        raise RuntimeError("candidate pin on a tag: a tag build ships the release pin only")
    return wanted


def prior_cad_tree_gone(owner_dir: Path | None = None) -> bool:
    """True only when no CAD helper of an earlier build may still run (spec stage B R4, Codex B2-R1): the same
    proof the app gives an earlier in-app run (``step_runtime_child.prior_tree_gone``)."""
    _step_runtime, _tree, child = _backend()
    return child.prior_tree_gone(Path(owner_dir or CAD_OWNER), log=log)


def _helper_reason(stdout: str) -> str:
    for line in reversed(stdout.strip().splitlines()):
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if isinstance(answer, dict):
            return str(answer.get("reason") or answer.get("problems") or "failed")
    return "failed"


def run_helper(
    python: Path,
    args: list,
    *,
    timeout: float,
    cwd: Path,
    helper: tuple[str, ...] = ("-m", "backend.app.step_runtime"),
) -> subprocess.CompletedProcess:
    """One call of the CAD helper in the embedded Python, owned as ONE tree from its spawn — install, its pip, the
    import smoke, the converter and the PNG child alike (spec stage B R4). This build's own record goes first: no
    owner record, no launch. A proven end (also of a timeout) takes the owner record with the launcher's; an
    unproven end or an interruption keeps them all for the next build to judge."""
    step_runtime, tree, child = _backend()
    CAD_OWNER.mkdir(parents=True, exist_ok=True)
    owner = CAD_OWNER / step_runtime.OWNER_RECORD
    try:
        tree.record(owner, os.getpid())
    except Exception as exc:
        raise RuntimeError(f"the CAD helper's owner could not be recorded ({type(exc).__name__})") from exc
    cmd = [str(python), *helper, *(str(a) for a in args)]
    try:
        result = child.run_tree(cmd, interpreter=python, records=CAD_OWNER, timeout=timeout, cwd=str(cwd))
    except subprocess.TimeoutExpired:
        owner.unlink(missing_ok=True)  # the launcher proved the tree over before it raised
        raise RuntimeError(f"the CAD helper ran past {timeout:.0f} s; its tree was ended") from None
    except child.TreeUnproven as exc:
        raise RuntimeError("the CAD helper's process tree could not be proven ended; its records stay") from exc
    owner.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"step_runtime {' '.join(map(str, args[:1]))} failed: {_helper_reason(result.stdout)}")
    return result


def build_cad_bundle(python: Path, *, allow_candidate: bool) -> Path:
    """The pinned bundle installed by the embedded Python into the build root (download only when the pin there
    is not already intact) — returns the bundle the helper names."""
    args = ["install", "--app-dir", CAD_ROOT, *(["--allow-candidate"] if allow_candidate else [])]
    result = run_helper(python, args, timeout=CAD_INSTALL_SECONDS, cwd=STAGING / "app")
    answer = json.loads(result.stdout.strip().splitlines()[-1])
    log(f"CAD bundle ready in the build root ({answer['acceptance']})")
    return CAD_ROOT / answer["bundle"]


def export_cad_bundle(bundle: Path, app: Path) -> Path:
    """Only the located bundle — <key>/<digest> with its receipt and site — into the payload."""
    step_runtime, _tree, _child = _backend()
    target = step_runtime.bundle_root(app) / bundle.relative_to(step_runtime.bundle_root(CAD_ROOT))
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, target)
    return target


STEP_PAYLOAD_FILES = (
    "backend/app/step_runtime.py",
    "backend/app/services/step_runtime_child.py",
    "backend/app/step_preview_probe.py",
    "backend/app/services/step_thumbnail.py",
    "backend/app/preview_render.py",
    "backend/app/data/step_runtime_manifest.json",
    "backend/app/data/step_runtime_closure.json",
    "backend/app/data/step_runtime_notices.json",
    "backend/app/data/step_runtime_notices",
    "backend/app/data/step_probe/box_mm.step",
)


def step_payload_problems(app: Path) -> list[str]:
    """The CAD part of the payload, exactly (spec stage B R5): one bundle of the staged catalog's digest with its
    receipt and site, nothing else under runtime/step-preview (no lock, no staging, no trash, no other digest or
    key), and the helper, catalog, notices and probe STEP beside the backend."""
    step_runtime, _tree, _child = _backend()
    problems = [f"missing:{rel}" for rel in STEP_PAYLOAD_FILES if not (app / rel).exists()]
    try:
        catalog = step_runtime.load_catalog(app / "backend" / "app" / "data", decode_tags=False)
    except step_runtime.CatalogError as exc:
        return [*problems, f"catalog_invalid:{exc.reason}"]
    root = step_runtime.bundle_root(app)
    entries = sorted(root.iterdir()) if root.is_dir() else []
    keys = [e for e in entries if e.is_dir() and not e.name.startswith(".")]
    for entry in entries:
        if entry.name == ".staging":
            problems.append("staging")
        elif entry.name.endswith(".lock"):
            problems.append(f"lock:{entry.name}")
        elif entry not in keys:
            problems.append(f"unexpected:{entry.name}")
    if len(keys) != 1:
        return [*problems, f"keys:{len(keys)}"]
    digests = sorted(keys[0].iterdir())
    for extra in digests[1:]:
        problems.append(f"extra_digest:{extra.name}")
    bundle = digests[0] if digests else None
    if bundle is None or bundle.name != catalog.digest[:32]:
        return [*problems, "digest_mismatch"]
    try:
        receipt = json.loads((bundle / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [*problems, "receipt_missing"]
    if receipt.get("manifest_digest") != catalog.digest or receipt.get("key") != keys[0].name:
        problems.append("receipt_mismatch")
    if not (bundle / "site").is_dir():
        problems.append("site_missing")
    return problems


def step_max_app_dir_len(catalog=None, key: str = "win-x64-cp312") -> int:
    """The longest {app} the bundle's deepest library allows under the loader's limit, in UTF-16 units as
    step_runtime counts them: {app} + this tail + a separator + the deepest library path."""
    step_runtime, _tree, _child = _backend()
    catalog = catalog or step_runtime.load_catalog(decode_tags=False)
    wheel = next(a for a in catalog.manifest["artifacts"] if a["key"] == key)
    deepest = max(len(lib["path"]) for lib in catalog.closure["artifacts"][wheel["filename"]])
    tail = "\\app\\runtime\\" + step_runtime.PRODUCT_DIR + "\\" + key + "\\" + "0" * 32 + "\\site"
    return WINDOWS_LIBRARY_PATH_LIMIT - deepest - 1 - len(tail.encode("utf-16-le")) // 2


def payload_max_app_dir_len(staging: Path) -> int:
    """The same bound over every file staged for {app} — Python's site-packages, the backend, Node, the tools: a
    native library (Python's extensions, PostgreSQL's and Node's libraries) under the loader's limit, any other file
    (an .exe included) under MAX_PATH. A dependency deeper than the CAD bundle's deepest DLL would otherwise be laid down where it
    cannot be read or loaded."""
    bounds = [
        (WINDOWS_LIBRARY_PATH_LIMIT if f.suffix.lower() in NATIVE_SUFFIXES else WINDOWS_PATH_LIMIT)
        - 1
        - len(str(f.relative_to(staging)).encode("utf-16-le")) // 2
        for f in staging.rglob("*")
        if f.is_file()
    ]
    return min(bounds, default=WINDOWS_PATH_LIMIT - 1)


def drop_bytecode(root: Path) -> None:
    """The final verify imports the staged backend — in the helper and in the probe it starts with an allowlist
    environment, which no PYTHONDONTWRITEBYTECODE reaches — so its caches go again after it: stage_backend leaves
    them out on purpose."""
    for cache in sorted(root.rglob("__pycache__"), reverse=True):
        shutil.rmtree(cache)


def _report_sizes(bundle: Path) -> None:
    size = sum(f.stat().st_size for f in bundle.rglob("*") if f.is_file())
    log(f"CAD bundle in the payload: {size / 1024**2:.1f} MB ({bundle.parent.name})")


def _read_app_version() -> str:
    """Read APP_VERSION from backend/app/core/config.py (the canonical
    source used by every other BamDude surface — FastAPI OpenAPI title,
    /system info, support bundles, spoolbuddy update check).
    """
    config_py = REPO_ROOT / "backend" / "app" / "core" / "config.py"
    if not config_py.exists():
        return "0.0.0+dev"
    # utf-8, never the locale: config.py carries non-ASCII (a U+FE0F among them), and a Windows runner's cp1252
    # cannot map its 0x8f byte -- a bare read_text() failed the build on its last step
    for raw in config_py.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("APP_VERSION"):
            # APP_VERSION = "0.2.5b1"  ->  0.2.5b1
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0+dev"


def _resolve_installer_version() -> str:
    """Decide what version string the installer carries.

    Priority:
      1. ``GITHUB_REF`` env var when set to a tag (e.g.
         ``refs/tags/v0.2.5b1-daily.20260610``) — the daily-beta and stable
         publish scripts both push tags in the ``v<APP_VERSION>[-daily.<date>]``
         shape, and we want the installer filename + Inno Setup AppVersion
         to match the GitHub release exactly so dailies stay distinguishable
         from each other and from the eventual stable.
      2. ``APP_VERSION`` from config.py for manual workflow_dispatch runs
         (no tag) and for local builds.

    Strips the leading ``v`` from tags so the installer filename is
    ``bamdude-0.2.5b1-daily.20260610-windows-x64-setup.exe``, not
    ``bamdude-v0.2.5b1-...``.
    """
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/"):
        tag = ref.removeprefix("refs/tags/")
        if tag.startswith("v"):
            tag = tag[1:]
        return tag or _read_app_version()
    return _read_app_version()


def write_version_file() -> None:
    """Write the installer version as both a plain VERSION file and an
    Inno Setup include file so the .iss script can pick it up at compile
    time without a fragile file-read hack.
    """
    version = _resolve_installer_version()
    (STAGING / "VERSION").write_text(version, encoding="utf-8")

    # Inno Setup include — bamdude.iss does `#include "build\staging\version.iss"`
    iss_version = STAGING / "version.iss"
    # The longest {app} the whole payload allows: the CAD bundle's deepest DLL (from the catalog) or anything
    # staged deeper.
    app_dir_limit = min(step_max_app_dir_len(), payload_max_app_dir_len(STAGING))
    iss_version.write_text(
        f'#define MyAppVersion "{version}"\n#define StepMaxAppDirLen {app_dir_limit}\n', encoding="utf-8"
    )
    log(f"staged VERSION = {version}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="Skip frontend build (use existing frontend/dist/)",
    )
    parser.add_argument(
        "--skip-pip",
        action="store_true",
        help="Skip pip install (use existing staged Python)",
    )
    parser.add_argument(
        "--allow-non-windows",
        action="store_true",
        help=(
            "Override the Windows-only guard. Only useful if you have a "
            "working wine + windows-python toolchain. Not exercised by CI."
        ),
    )
    parser.add_argument(
        "--allow-candidate-cad",
        action="store_true",
        help="development only: stage the candidate STEP-preview pin (refused on a tag; STEP_PREVIEW_CANDIDATE=1)",
    )
    args = parser.parse_args(argv)

    if sys.platform != "win32" and not args.allow_non_windows:
        log("ERROR: this build script must run on Windows.")
        log("")
        log("It downloads a Windows embeddable Python distribution and")
        log("pip-installs BamDude's requirements.txt against it — both")
        log("require executing python.exe, which only runs on Windows.")
        log("")
        log("Supported build paths:")
        log("  1. GitHub Actions: trigger '.github/workflows/windows-")
        log("     installer.yml' (Actions tab -> Windows Installer ->")
        log("     Run workflow). Downloads the .exe as a workflow artifact.")
        log("  2. Windows VM / box: clone, install Python 3.12 + Node 22 +")
        log("     Inno Setup 6, run this script.")
        log("")
        log("Unsupported escape hatch (cross-build under Wine): rerun with")
        log("--allow-non-windows. Requires wine + a Windows Python in $PATH")
        log("via wine python.exe — fragile and not exercised by CI.")
        return 1

    # Before anything under build/ is removed: no CAD helper of an earlier build may still be running from it.
    if not prior_cad_tree_gone():
        log("ERROR: refusing to rebuild over a CAD helper whose end is not proven; end it (or reboot) and retry.")
        return 1
    allow_candidate = cad_candidate_allowed(args.allow_candidate_cad, os.environ)

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    STAGING.mkdir(parents=True, exist_ok=True)

    python_dir = stage_embedded_python(reuse=args.skip_pip)
    stage_vcruntime(python_dir)
    if not args.skip_pip:
        install_requirements(python_dir)
    verify_requirements(python_dir)

    if args.skip_frontend:
        # Vite writes to <repo>/static (outDir: '../static'), never frontend/dist —
        # see build_frontend(). This branch used to look at frontend/dist and so
        # refused every --skip-frontend run.
        frontend_dist = REPO_ROOT / "static"
        if not frontend_dist.exists():
            raise RuntimeError("--skip-frontend given but static/ doesn't exist — run `npm run build` in frontend/")
    else:
        frontend_dist = build_frontend()

    stage_backend(frontend_dist, build_ai_packages())
    stage_nssm()
    stage_ffmpeg()
    stage_render_runtime()
    python_exe = python_dir / "python.exe"
    bundle = export_cad_bundle(build_cad_bundle(python_exe, allow_candidate=allow_candidate), STAGING / "app")
    run_helper(python_exe, ["verify", "--app-dir", STAGING / "app"], timeout=CAD_VERIFY_SECONDS, cwd=STAGING / "app")
    drop_bytecode(STAGING / "app" / "backend")
    problems = step_payload_problems(STAGING / "app")
    if problems:
        raise RuntimeError(f"the CAD payload is not exact: {', '.join(problems[:10])}")
    _report_sizes(bundle)
    write_version_file()

    log("")
    log("=" * 60)
    log("Staging complete.")
    log(f"Staged tree: {STAGING}")
    log("")
    log("Next: compile the Inno Setup script:")
    log('  "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe" bamdude.iss')
    log("")
    log(f"Installer will be written to: {BUILD_DIR / 'output'}")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
