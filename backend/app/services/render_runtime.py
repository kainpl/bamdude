"""The part-render runtime: the pinned official Node.js -- platform, provisioning, location (spec §6).

Stdlib only, importable before the app runs: Dockerfile, install.sh and the
Windows build call ``python -m backend.app.services.render_runtime provision``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform as _platform
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MANIFEST = Path(__file__).resolve().parents[1] / "data" / "render_runtime.json"
PRODUCT_DIR = "node"
# Node 24 official builds only (consilium N1).
PLATFORMS = ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win-x64")
_HEADERS = {"User-Agent": "BamDude render-runtime provisioning"}


class ProvisionError(RuntimeError):
    pass


class UnsupportedPlatform(ProvisionError):
    """No official Node.js build for this machine: part thumbnails run their fallback methods (no_runtime)."""


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    system = (system or _platform.system()).lower()
    machine = (machine or _platform.machine()).lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    key = {"linux": f"linux-{arch}", "darwin": f"darwin-{arch}", "windows": f"win-{arch}"}.get(system) if arch else None
    if key not in PLATFORMS:
        # Node 24 builds no 32-bit ARM (armv7l moved to experimental): part thumbnails
        # fall back to the top-view method there (spec §5.3, §6.1) -- never a third-party build.
        raise UnsupportedPlatform(f"no official Node.js build for {system}/{machine}")
    return key


def artifacts_from_shasums(text: str, version: str) -> dict[str, tuple[str, str]]:
    """The official archive of every platform in a release's SHASUMS256.txt: platform -> (file name, sha256).

    A platform the release does not publish is an error naming every such platform at once (consilium N1);
    no other build is ever substituted.
    """
    sums = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2:
            sums[fields[1]] = fields[0]
    found: dict[str, tuple[str, str]] = {}
    missing = []
    for key in PLATFORMS:
        name = f"node-{version}-{key}.{'zip' if key == 'win-x64' else 'tar.xz'}"
        if name in sums:
            found[key] = (name, sums[name])
        else:
            missing.append(key)
    if missing:
        raise ProvisionError(f"Node.js {version} publishes no official archive for {', '.join(missing)}")
    return found


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


def _inside(root: Path, name: str) -> Path:
    """The archive entry ``name`` with its top directory stripped, refused if it would leave ``root``."""
    parts = Path(name).parts[1:]
    target = (root / Path(*parts)).resolve() if parts else root
    if target != root and root not in target.parents:
        raise ProvisionError(f"unsafe archive entry: {name}")
    return target


def _extract(archive: Path, dest: Path) -> None:
    """Unpack the official archive (``node-vX-<platform>/...``) with its top directory stripped."""
    root = dest.resolve()
    try:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                infos = [(i, _inside(root, i.filename)) for i in zf.infolist()]
                for info, target in infos:
                    if target == root:
                        continue
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, target.open("wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
            return
        with tarfile.open(archive, "r:*") as tf:
            members = []
            for m in tf.getmembers():
                target = _inside(root, m.name)
                # bin/npm, npx, corepack are symlinks into lib/node_modules: the renderer needs none of them
                if target == root or m.issym() or m.islnk():
                    continue
                m.name = target.relative_to(root).as_posix()
                members.append(m)
            tf.extractall(root, members=members, filter="data")
    except (tarfile.TarError, zipfile.BadZipFile) as exc:
        raise ProvisionError(f"broken archive: {exc}") from exc


def _executable(target: Path, key: str) -> Path:
    return target / "node.exe" if key == "win-x64" else target / "bin" / "node"


def _rename(src: Path, dst: Path, *, attempts: int = 20, delay: float = 0.5) -> None:
    """Rename, waiting out a short hold: on Windows a just-unpacked tree is held for a moment
    (an antivirus scanning the new node.exe) and refuses the rename; a hold that outlasts the
    retries is an error."""
    for attempt in range(attempts):
        try:
            src.rename(dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


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
        and _executable(target, key).is_file()
    ):
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.parent / f".{key}.{version}.part"
    shutil.rmtree(staged, ignore_errors=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=f".{key}.download.") as tmp:
        # Named after the URL so _extract sees the archive's suffix.
        archive = Path(tmp) / Path(entry["url"]).name
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
        _rename(target, old)
    _rename(staged, target)
    shutil.rmtree(old, ignore_errors=True)
    return target


@dataclass(frozen=True)
class NodeInstall:
    executable: Path
    version: str
    platform: str


def locate(app_dir: Path) -> NodeInstall | None:
    """The provisioned Node under <app_dir>/runtime -- the only place looked at (spec §6.2)."""
    return locate_in(app_dir / "runtime")


def locate_in(runtime_root: Path, platform: str | None = None) -> NodeInstall | None:
    """The Node provisioned into this runtime directory.

    Three answers, never folded into one (consilium R5.3):
      NodeInstall          -- installed;
      None                 -- a supported platform with nothing installed: runtime_missing;
      UnsupportedPlatform  -- raised, never caught here: no official Node build exists for
                              this machine, part thumbnails run their fallback methods (no_runtime).
    """
    key = platform_key() if platform is None else platform
    if key not in PLATFORMS:
        raise UnsupportedPlatform(f"no official Node.js build for {key}")
    target = runtime_root / PRODUCT_DIR / key
    marker = target / "VERSION"
    executable = _executable(target, key)
    if not marker.is_file() or not executable.is_file():
        return None
    return NodeInstall(executable=executable, version=marker.read_text(encoding="utf-8").strip(), platform=key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="render_runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    prov = sub.add_parser("provision", help="install the pinned official Node.js")
    prov.add_argument("--runtime", type=Path, required=True)
    prov.add_argument("--platform", default=None)
    # The Docker build provisions from a copy of this module and the manifest
    # alone, before the rest of backend/ is copied, so the layer is cached.
    prov.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args(argv)
    try:
        target = provision(args.runtime, manifest=load_manifest(args.manifest), platform=args.platform)
    except UnsupportedPlatform as exc:
        print(f"render-runtime: {exc}: part thumbnails use the top-view fallback", file=sys.stderr)
        return 3
    except ProvisionError as exc:
        print(f"render-runtime: {exc}", file=sys.stderr)
        return 1
    print(f"render-runtime: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
