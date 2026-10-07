"""The part-render browser: platform, provisioning, location and launch (spec §6).

Stdlib only, importable before the app runs: Dockerfile, install.sh and the
Windows build call ``python -m backend.app.services.render_browser provision``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform as _platform
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "data" / "render_browser.json"
PRODUCT_DIR = "chrome-headless-shell"
_HEADERS = {"User-Agent": "BamDude render-browser provisioning"}


class ProvisionError(RuntimeError):
    pass


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    system = (system or _platform.system()).lower()
    machine = (machine or _platform.machine()).lower()
    if system == "windows" and machine in ("amd64", "x86_64"):
        return "win64"
    if system == "darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    if system == "linux" and machine in ("x86_64", "amd64"):
        return "linux64"
    if system == "linux" and machine in ("aarch64", "arm64"):
        return "linux-arm64"
    raise ProvisionError(f"no chrome-headless-shell build for {system}/{machine}")


def load_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def download(url: str, dest: Path) -> None:
    with (
        urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=60) as resp,
        dest.open("wb") as out,
    ):
        shutil.copyfileobj(resp, out, 1024 * 1024)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _extract(archive: Path, dest: Path) -> None:
    """Unzip with POSIX modes restored (zipfile drops them) and no entry leaving ``dest``."""
    root = dest.resolve()
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                target = (root / info.filename).resolve()
                if target != root and root not in target.parents:
                    raise ProvisionError(f"unsafe archive entry: {info.filename}")
            for info in zf.infolist():
                target = root / info.filename
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    target.chmod(mode | stat.S_IRUSR)
    except zipfile.BadZipFile as exc:
        raise ProvisionError(f"broken archive: {exc}") from exc


def provision(
    runtime_root: Path,
    *,
    manifest: dict | None = None,
    platform: str | None = None,
    fetch: Callable[[str, Path], None] = download,
) -> Path:
    manifest = manifest or load_manifest()
    key = platform or platform_key()
    entry = manifest["platforms"].get(key)
    if entry is None:
        raise ProvisionError(f"the manifest pins no build for {key}")
    version = manifest["version"]
    target = runtime_root / PRODUCT_DIR / key
    marker = target / "VERSION"
    # The marker alone is not an install: a quarantined or deleted executable
    # must be put back by the next run, not reported as already there.
    if (
        marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == version
        and any(target.rglob(_executable_name(key)))
    ):
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.parent / f".{key}.{version}.part"
    shutil.rmtree(staged, ignore_errors=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=f".{key}.download.") as tmp:
        archive = Path(tmp) / "browser.zip"
        fetch(entry["url"], archive)
        if archive.stat().st_size != entry["bytes"] or _sha256(archive) != entry["sha256"]:
            raise ProvisionError(f"{key}: sha256 or size does not match the manifest")
        staged.mkdir()
        try:
            _extract(archive, staged)
            (staged / "VERSION").write_text(version + "\n", encoding="utf-8")
        except BaseException:
            shutil.rmtree(staged, ignore_errors=True)
            raise
    old = target.parent / f".{key}.old"
    shutil.rmtree(old, ignore_errors=True)
    if target.exists():
        target.rename(old)
    staged.rename(target)
    shutil.rmtree(old, ignore_errors=True)
    return target


@dataclass(frozen=True)
class BrowserInstall:
    executable: Path
    version: str
    platform: str


def _executable_name(key: str) -> str:
    return "chrome-headless-shell.exe" if key == "win64" else "chrome-headless-shell"


def locate(app_dir: Path) -> BrowserInstall | None:
    """The provisioned browser under <app_dir>/runtime -- the only place looked at (spec §6.2)."""
    return locate_in(app_dir / "runtime")


def locate_in(runtime_root: Path) -> BrowserInstall | None:
    """The browser provisioned into this runtime directory, whatever it is called."""
    try:
        key = platform_key()
    except ProvisionError:
        return None
    root = runtime_root / PRODUCT_DIR / key
    marker = root / "VERSION"
    if not marker.is_file():
        return None
    hits = sorted(root.rglob(_executable_name(key)))
    if not hits:
        return None
    return BrowserInstall(executable=hits[0], version=marker.read_text(encoding="utf-8").strip(), platform=key)


# Nothing listens on the discard port; with a fixed proxy list Chromium has no
# DIRECT fallback, so every URL request that is not bypassed fails (spec §6.3).
DEAD_PROXY = "http://127.0.0.1:9"


def launch_args(*, profile_dir: Path, origin: str, netlog: Path | None = None) -> list[str]:
    """Flags for one attempt. The sandbox stays on: no flag here disables it.

    ``<-loopback>`` removes Chromium's implicit proxy bypass, which covers
    loopback AND link-local; only the task's exact HTTP origin is bypassed
    back (Chromium net/docs/proxy.md, implicit bypass rules). This limits URL
    requests through the proxy stack; it is not an OS network sandbox. The
    rule is proven by the network probe (task 10), never re-ordered to make
    a probe pass.
    """
    args = [
        "--use-angle=swiftshader",
        "--enable-unsafe-swiftshader",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-breakpad",
        "--disable-domain-reliability",
        "--metrics-recording-only",
        "--mute-audio",
        f"--proxy-server={DEAD_PROXY}",
        # Order matters: first drop the implicit exceptions, then add back only
        # the task's exact HTTP origin. Without the scheme the exception would
        # also cover https:// and ws:// on the same host:port.
        f"--proxy-bypass-list=<-loopback>;http://{origin}",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
    ]
    if netlog is not None:
        args.append(f"--log-net-log={netlog}")
    return args


# PreviewProcess's allowlist (services/preview_process.py): what a process needs
# to start on each OS, and nothing of the service's own -- no DATABASE_URL, API
# keys, tokens or .env values reach the browser (spec §6.3).
_ENV_KEYS = frozenset(
    {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"}
)


def browser_env(
    home: Path, *, environ: Mapping[str, str] | None = None, posix: bool = os.name != "nt"
) -> dict[str, str]:
    """The browser's whole environment for one attempt.

    On POSIX, HOME and the XDG directories point into the attempt's own
    directory (``home``, which the caller creates and removes): fontconfig's
    cache and NSS's database are written there and nowhere else. The probe
    (E2) measured that the pinned build renders with nothing more.
    """
    source = os.environ if environ is None else environ
    env = {key: value for key, value in source.items() if key.upper() in _ENV_KEYS}
    if posix:
        env.update(HOME=str(home), XDG_CACHE_HOME=str(home / ".cache"), XDG_CONFIG_HOME=str(home / ".config"))
    return env


@dataclass(frozen=True)
class LibraryCheck:
    ok: bool
    missing: list[str]
    errors: list[str]


def elf_files(install_root: Path, executable: Path) -> list[Path]:
    """The executable plus every shipped shared object -- SwiftShader and ANGLE are dlopen()ed at run time."""
    shared = sorted({*install_root.rglob("*.so"), *install_root.rglob("*.so.*")})
    return [executable, *shared]


def check_libraries(paths: list[Path], run: Callable = subprocess.run) -> LibraryCheck:
    """`ldd` over each ELF. A failed or unreadable ldd is an error, never "nothing missing"."""
    missing: list[str] = []
    errors: list[str] = []
    for path in paths:
        proc = run(["ldd", str(path)], capture_output=True, text=True)
        out, err = proc.stdout or "", (proc.stderr or "").strip()
        if proc.returncode != 0:
            errors.append(f"{path.name}: ldd exited {proc.returncode}: {(err or out).strip()[:200]}")
            continue
        if "not a dynamic executable" in out or ("=>" not in out and "statically linked" not in out):
            errors.append(f"{path.name}: unusable ldd output: {out.strip()[:200]!r}")
            continue
        if err:
            errors.append(f"{path.name}: {err[:200]}")
        for line in out.splitlines():
            if "=> not found" in line:
                missing.append(f"{path.name}: {line.split('=>', 1)[0].strip()}")
            elif "version `" in line and "not found" in line:
                missing.append(f"{path.name}: {line.strip()}")
    return LibraryCheck(ok=not missing and not errors, missing=missing, errors=errors)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="render_browser")
    sub = parser.add_subparsers(dest="command", required=True)
    prov = sub.add_parser("provision", help="install the pinned chrome-headless-shell")
    prov.add_argument("--runtime", type=Path, required=True)
    prov.add_argument("--platform", default=None)
    # The Docker build provisions from a copy of this module and the manifest
    # alone, before the rest of backend/ is copied, so the layer is cached.
    prov.add_argument("--manifest", type=Path, default=MANIFEST)
    libs = sub.add_parser("check-libs", help="Linux: shared libraries the browser and its shipped ELFs cannot load")
    libs.add_argument("--runtime", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "check-libs":
        found = locate_in(args.runtime)
        if found is None:
            print("render-browser: not provisioned", file=sys.stderr)
            return 1
        install_root = args.runtime / PRODUCT_DIR / found.platform
        result = check_libraries(elf_files(install_root, found.executable))
        for line in result.missing:
            print(f"render-browser: missing {line}", file=sys.stderr)
        for line in result.errors:
            print(f"render-browser: ldd error {line}", file=sys.stderr)
        return 0 if result.ok else 1
    try:
        target = provision(args.runtime, manifest=load_manifest(args.manifest), platform=args.platform)
    except ProvisionError as exc:
        print(f"render-browser: {exc}", file=sys.stderr)
        return 1
    print(f"render-browser: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
