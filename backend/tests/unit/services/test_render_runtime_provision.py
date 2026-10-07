import hashlib
import io
import json
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from backend.app.services import render_runtime
from backend.app.services.render_runtime import ProvisionError, provision

VERSION = "v24.17.0"
TOP = f"node-{VERSION}-win-x64"


def _archive(tmp_path: Path, *, evil: bool = False) -> tuple[Path, str, int]:
    """The Windows build's shape: a zip with one top directory holding node.exe."""
    path = tmp_path / f"{TOP}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo(f"{TOP}/node.exe")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(info, b"MZ fake node")
        zf.writestr(f"{TOP}/LICENSE", b"MIT")
        if evil:
            # still outside after the top directory is stripped
            zf.writestr(f"{TOP}/../../escape.txt", b"no")
    data = path.read_bytes()
    return path, hashlib.sha256(data).hexdigest(), len(data)


def _manifest(sha256: str, size: int) -> dict:
    return {
        "version": VERSION,
        "platforms": {
            "win-x64": {"url": f"https://nodejs.org/dist/{VERSION}/{TOP}.zip", "sha256": sha256, "bytes": size}
        },
    }


def _fetch_from(source: Path):
    calls: list[str] = []

    def fetch(url: str, dest: Path, limit: int | None = None) -> None:
        calls.append(url)
        dest.write_bytes(source.read_bytes())

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_installs_and_marks_the_version(tmp_path: Path):
    archive, sha, size = _archive(tmp_path)
    target = provision(
        tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=_fetch_from(archive)
    )
    assert target == tmp_path / "runtime" / "node" / "win-x64"
    assert (target / "VERSION").read_text().strip() == VERSION
    assert (target / "node.exe").is_file()


def test_only_the_executable_and_the_licence_are_installed_from_a_zip(tmp_path: Path):
    # owner, 2026-10-07: the renderer needs node.exe alone -- no npm/npx/corepack, headers or docs beside a service
    path = tmp_path / f"{TOP}.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{TOP}/node.exe", b"MZ fake node")
        zf.writestr(f"{TOP}/LICENSE", b"MIT")
        zf.writestr(f"{TOP}/npm.cmd", b"@echo npm")
        zf.writestr(f"{TOP}/node_modules/npm/index.js", b"npm")
        zf.writestr(f"{TOP}/README.md", b"docs")
    data = path.read_bytes()
    target = provision(
        tmp_path / "runtime",
        manifest=_manifest(hashlib.sha256(data).hexdigest(), len(data)),
        platform="win-x64",
        fetch=_fetch_from(path),
    )
    installed = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert installed == ["LICENSE", "VERSION", "node.exe"]


def test_only_the_executable_and_the_licence_are_installed_from_a_tar_xz(tmp_path: Path):
    files = {
        "bin/node": b"#!fake",
        "LICENSE": b"MIT",
        "lib/node_modules/npm/index.js": b"npm",
        "include/node/node.h": b"/* */",
        "share/doc/node/gdbinit": b"doc",
    }
    data = _tar_xz(tmp_path, "node-v9-linux-x64", files).read_bytes()
    manifest = {
        "version": "v9",
        "platforms": {"linux-x64": {"url": "u.tar.xz", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}},
    }
    target = render_runtime.provision(
        tmp_path / "runtime",
        manifest=manifest,
        platform="linux-x64",
        fetch=lambda u, d, limit=None: d.write_bytes(data),
    )
    installed = sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())
    assert installed == ["LICENSE", "VERSION", "bin/node"]


def test_is_idempotent(tmp_path: Path):
    archive, sha, size = _archive(tmp_path)
    fetch = _fetch_from(archive)
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=fetch)
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=fetch)
    assert len(fetch.calls) == 1


def test_refuses_a_hash_mismatch_and_installs_nothing(tmp_path: Path):
    archive, _, size = _archive(tmp_path)
    with pytest.raises(ProvisionError, match="sha256"):
        provision(
            tmp_path / "runtime", manifest=_manifest("0" * 64, size), platform="win-x64", fetch=_fetch_from(archive)
        )
    assert not (tmp_path / "runtime" / "node" / "win-x64").exists()


def test_refuses_entries_that_escape_the_target(tmp_path: Path):
    archive, sha, size = _archive(tmp_path, evil=True)
    with pytest.raises(ProvisionError, match="unsafe"):
        provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=_fetch_from(archive))
    assert not (tmp_path / "escape.txt").exists()


def test_a_failed_extract_leaves_no_target_and_the_next_run_installs(tmp_path: Path):
    archive, sha, size = _archive(tmp_path)
    broken = tmp_path / "broken.zip"
    broken.write_bytes(archive.read_bytes()[: size // 2])
    with pytest.raises(ProvisionError):
        provision(
            tmp_path / "runtime",
            manifest=_manifest(hashlib.sha256(broken.read_bytes()).hexdigest(), size // 2),
            platform="win-x64",
            fetch=_fetch_from(broken),
        )
    target = tmp_path / "runtime" / "node" / "win-x64"
    assert not target.exists()
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=_fetch_from(archive))
    assert (target / "VERSION").read_text().strip() == VERSION


def test_reinstalls_when_the_executable_is_gone(tmp_path: Path):
    # Antivirus quarantine or a hand-deleted file: VERSION alone must not
    # convince a re-run of the installer that nothing is to be done.
    archive, sha, size = _archive(tmp_path)
    fetch = _fetch_from(archive)
    target = provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=fetch)
    (target / "node.exe").unlink()
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=fetch)
    assert len(fetch.calls) == 2
    assert (target / "node.exe").is_file()


def test_replaces_an_older_version(tmp_path: Path):
    archive, sha, size = _archive(tmp_path)
    target = tmp_path / "runtime" / "node" / "win-x64"
    target.mkdir(parents=True)
    (target / "VERSION").write_text("v1.0.0")
    (target / "stale.txt").write_text("old")
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=_fetch_from(archive))
    assert (target / "VERSION").read_text().strip() == VERSION
    assert not (target / "stale.txt").exists()


def _flaky_rename(monkeypatch, refusals: int) -> list[str]:
    """Path.rename refusing ``refusals`` times first, as Windows does while an antivirus holds the new tree."""
    real = Path.rename
    calls: list[str] = []

    def rename(self, target):
        calls.append(self.name)
        if len(calls) <= refusals:
            raise PermissionError(5, "Access is denied", str(self))
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(render_runtime.time, "sleep", lambda s: None)
    return calls


def test_a_briefly_held_tree_is_renamed_once_it_is_released(tmp_path: Path, monkeypatch):
    # Seen on Windows 2026-10-07: the freshly unpacked node.exe is held for a moment and the
    # staged directory cannot be renamed into place; a moment later the same rename succeeds.
    archive, sha, size = _archive(tmp_path)
    calls = _flaky_rename(monkeypatch, refusals=2)
    target = provision(
        tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=_fetch_from(archive)
    )
    assert (target / "node.exe").is_file()
    assert len(calls) == 3


def test_a_tree_that_stays_held_is_an_error(tmp_path: Path, monkeypatch):
    archive, sha, size = _archive(tmp_path)
    _flaky_rename(monkeypatch, refusals=10_000)
    with pytest.raises(PermissionError):
        provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=_fetch_from(archive))
    assert not (tmp_path / "runtime" / "node" / "win-x64").exists()


@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("Linux", "x86_64", "linux-x64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Darwin", "arm64", "darwin-arm64"),
        ("Darwin", "x86_64", "darwin-x64"),
        ("Windows", "AMD64", "win-x64"),
    ],
)
def test_platform_key(system, machine, key):
    assert render_runtime.platform_key(system, machine) == key


@pytest.mark.parametrize("machine", ["armv7l", "armv6l", "riscv64"])
def test_platform_key_reports_a_platform_without_an_official_build(machine):
    with pytest.raises(render_runtime.UnsupportedPlatform):
        render_runtime.platform_key("Linux", machine)


@pytest.mark.parametrize("machine", ["aarch64", "x86_64"])
def test_a_32_bit_linux_userland_on_a_64_bit_kernel_is_unsupported(machine):
    # Raspberry Pi OS 32-bit boots a 64-bit kernel on a Pi 4 / Pi 5: uname says aarch64, but the userland
    # (this Python included) is armhf and cannot run the arm64 Node -- that is no_runtime, not an install
    with pytest.raises(render_runtime.UnsupportedPlatform):
        render_runtime.platform_key("Linux", machine, bits=32)
    assert render_runtime.platform_key("Linux", machine, bits=64).startswith("linux-")


def test_a_32_bit_python_on_64_bit_windows_still_runs_the_x64_node():
    assert render_runtime.platform_key("Windows", "AMD64", bits=32) == "win-x64"


SHASUMS = "\n".join(
    f"{i:064x}  node-v24.17.0-{name}"
    for i, name in enumerate(
        [
            "linux-x64.tar.xz",
            "linux-arm64.tar.xz",
            "darwin-x64.tar.xz",
            "darwin-arm64.tar.xz",
            "win-x64.zip",
            "linux-x64.tar.gz",
            "headers.tar.xz",
        ]
    )
)


def test_artifacts_from_shasums_picks_one_archive_per_platform():
    found = render_runtime.artifacts_from_shasums(SHASUMS, "v24.17.0")
    assert set(found) == set(render_runtime.PLATFORMS)
    assert found["linux-x64"][0] == "node-v24.17.0-linux-x64.tar.xz"
    assert found["win-x64"][0] == "node-v24.17.0-win-x64.zip"


def test_artifacts_from_shasums_names_every_missing_platform():
    text = "\n".join(line for line in SHASUMS.splitlines() if "darwin" not in line)
    with pytest.raises(render_runtime.ProvisionError, match="darwin-arm64.*darwin-x64|darwin-x64.*darwin-arm64"):
        render_runtime.artifacts_from_shasums(text, "v24.17.0")


def _tar_xz(tmp_path: Path, top: str, files: dict[str, bytes]) -> Path:
    archive = tmp_path / "node.tar.xz"
    with tarfile.open(archive, "w:xz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return archive


def test_provision_unpacks_a_tar_xz_and_locates_bin_node(tmp_path):
    archive = _tar_xz(tmp_path, "node-v9-linux-x64", {"bin/node": b"#!fake", "LICENSE": b"x"})
    data = archive.read_bytes()
    manifest = {
        "version": "v9",
        "platforms": {"linux-x64": {"url": "u", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}},
    }
    target = render_runtime.provision(
        tmp_path / "runtime",
        manifest=manifest,
        platform="linux-x64",
        fetch=lambda url, dest, limit=None: dest.write_bytes(data),
    )
    assert (target / "VERSION").read_text().strip() == "v9"
    found = render_runtime.locate_in(tmp_path / "runtime", platform="linux-x64")
    assert found and found.executable.name == "node" and found.version == "v9"
    if os.name != "nt":
        # the tar's executable bit survives the "data" filter: Linux and macOS run bin/node as it lands
        assert found.executable.stat().st_mode & stat.S_IXUSR


def test_provision_refuses_a_tar_entry_escaping_the_target(tmp_path):
    archive = tmp_path / "evil.tar.xz"
    with tarfile.open(archive, "w:xz") as tf:
        # still outside after the top directory is stripped
        info = tarfile.TarInfo("node-v9-linux-x64/../../escape")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    data = archive.read_bytes()
    manifest = {
        "version": "v9",
        "platforms": {"linux-x64": {"url": "u", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}},
    }
    with pytest.raises(render_runtime.ProvisionError):
        render_runtime.provision(
            tmp_path / "rt", manifest=manifest, platform="linux-x64", fetch=lambda u, d, limit=None: d.write_bytes(data)
        )
    assert not (tmp_path / "escape").exists()


def _install(runtime: Path, key: str) -> Path:
    target = runtime / render_runtime.PRODUCT_DIR / key
    executable = target / "node.exe" if key == "win-x64" else target / "bin" / "node"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"#!fake")
    (target / "VERSION").write_text("v9\n", encoding="utf-8")
    return executable


def test_locate_in_tells_unsupported_from_missing_from_installed(tmp_path, monkeypatch):
    # consilium R5.3: three answers, never folded together
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(render_runtime._platform, "system", lambda: "Linux")
    monkeypatch.setattr(render_runtime._platform, "machine", lambda: "armv7l")
    with pytest.raises(render_runtime.UnsupportedPlatform):
        render_runtime.locate_in(runtime)  # no_runtime: fallback methods only
    monkeypatch.setattr(render_runtime._platform, "machine", lambda: "aarch64")
    assert render_runtime.locate_in(runtime) is None  # runtime_missing
    executable = _install(runtime, "linux-arm64")
    assert render_runtime.locate_in(runtime) == render_runtime.NodeInstall(executable, "v9", "linux-arm64")


def test_locate_in_refuses_an_explicit_platform_without_an_official_build(tmp_path):
    with pytest.raises(render_runtime.UnsupportedPlatform):
        render_runtime.locate_in(tmp_path, platform="linux-armv7l")


def test_a_version_marker_without_its_executable_is_not_an_install(tmp_path):
    executable = _install(tmp_path, "win-x64")
    executable.unlink()
    assert render_runtime.locate_in(tmp_path, platform="win-x64") is None


def test_provision_hands_the_pinned_size_to_the_download(tmp_path: Path):
    archive, sha, size = _archive(tmp_path)
    limits: list[int | None] = []

    def fetch(url: str, dest: Path, limit: int | None = None) -> None:
        limits.append(limit)
        dest.write_bytes(archive.read_bytes())

    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="win-x64", fetch=fetch)
    assert limits == [size]


def test_download_stops_at_the_pinned_size(tmp_path: Path, monkeypatch):
    # a server that keeps sending must not fill the disk before the hash check can refuse the archive
    class Endless(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(render_runtime.urllib.request, "urlopen", lambda request, timeout: Endless(b"x" * (8 * 2**20)))
    dest = tmp_path / "node.zip"
    with pytest.raises(ProvisionError, match="larger than"):
        render_runtime.download("https://nodejs.org/dist/node.zip", dest, limit=2**20)
    assert dest.stat().st_size <= 2 * 2**20


def _pin_file(tmp_path: Path, sha: str, size: int) -> Path:
    # an unresolvable host: a code path that reaches the network by mistake fails fast instead of downloading
    pin = {
        "version": VERSION,
        "platforms": {"win-x64": {"url": "https://invalid.invalid/node.zip", "sha256": sha, "bytes": size}},
    }
    path = tmp_path / "render_runtime.json"
    path.write_text(json.dumps(pin), encoding="utf-8")
    return path


def test_cli_provision_exits_0_when_installed(tmp_path: Path, monkeypatch):
    archive, sha, size = _archive(tmp_path)
    monkeypatch.setattr(
        render_runtime, "download", lambda url, dest, limit=None: dest.write_bytes(archive.read_bytes())
    )
    args = ["provision", "--runtime", str(tmp_path / "rt"), "--manifest", str(_pin_file(tmp_path, sha, size))]
    assert render_runtime.main([*args, "--platform", "win-x64"]) == 0


def test_cli_provision_exits_1_with_a_message_when_the_download_fails(tmp_path: Path, monkeypatch, capsys):
    _, sha, size = _archive(tmp_path)

    def offline(url, dest, limit=None):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(render_runtime, "download", offline)
    args = ["provision", "--runtime", str(tmp_path / "rt"), "--manifest", str(_pin_file(tmp_path, sha, size))]
    assert render_runtime.main([*args, "--platform", "win-x64"]) == 1
    err = capsys.readouterr().err
    assert "Network is unreachable" in err and "Traceback" not in err


def test_cli_provision_exits_3_on_a_platform_without_an_official_build(tmp_path: Path, monkeypatch, capsys):
    _, sha, size = _archive(tmp_path)
    monkeypatch.setattr(render_runtime._platform, "system", lambda: "Linux")
    monkeypatch.setattr(render_runtime._platform, "machine", lambda: "armv7l")
    args = ["provision", "--runtime", str(tmp_path / "rt"), "--manifest", str(_pin_file(tmp_path, sha, size))]
    assert render_runtime.main(args) == 3
    assert "top-view fallback" in capsys.readouterr().err
