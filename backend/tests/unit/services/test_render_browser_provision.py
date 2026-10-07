import hashlib
import os
import stat
import zipfile
from pathlib import Path

import pytest

from backend.app.services.render_browser import ProvisionError, platform_key, provision

VERSION = "155.0.8059.39"


def _archive(tmp_path: Path, *, evil: bool = False) -> tuple[Path, str, int]:
    path = tmp_path / "chrome-headless-shell-linux64.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("chrome-headless-shell-linux64/chrome-headless-shell")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(info, b"#!/bin/sh\necho browser\n")
        zf.writestr("chrome-headless-shell-linux64/resources.pak", b"data")
        if evil:
            zf.writestr("../escape.txt", b"no")
    data = path.read_bytes()
    return path, hashlib.sha256(data).hexdigest(), len(data)


def _manifest(sha256: str, size: int) -> dict:
    return {
        "version": VERSION,
        "platforms": {
            "linux64": {
                "url": "https://storage.googleapis.com/x/chrome-headless-shell-linux64.zip",
                "sha256": sha256,
                "bytes": size,
            }
        },
    }


def _fetch_from(source: Path):
    calls: list[str] = []

    def fetch(url: str, dest: Path) -> None:
        calls.append(url)
        dest.write_bytes(source.read_bytes())

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("Windows", "AMD64", "win64"),
        ("Darwin", "arm64", "mac-arm64"),
        ("Darwin", "x86_64", "mac-x64"),
        ("Linux", "x86_64", "linux64"),
        ("Linux", "aarch64", "linux-arm64"),
    ],
)
def test_platform_key(system: str, machine: str, key: str):
    assert platform_key(system, machine) == key


def test_platform_key_refuses_unsupported():
    with pytest.raises(ProvisionError):
        platform_key("Linux", "armv7l")


def test_installs_and_marks_the_version(tmp_path: Path):
    archive, sha, size = _archive(tmp_path)
    target = provision(
        tmp_path / "runtime", manifest=_manifest(sha, size), platform="linux64", fetch=_fetch_from(archive)
    )
    assert target == tmp_path / "runtime" / "chrome-headless-shell" / "linux64"
    assert (target / "VERSION").read_text().strip() == VERSION
    exe = target / "chrome-headless-shell-linux64" / "chrome-headless-shell"
    assert exe.is_file()
    if os.name != "nt":
        assert exe.stat().st_mode & stat.S_IXUSR


def test_is_idempotent(tmp_path: Path):
    archive, sha, size = _archive(tmp_path)
    fetch = _fetch_from(archive)
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="linux64", fetch=fetch)
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="linux64", fetch=fetch)
    assert len(fetch.calls) == 1


def test_refuses_a_hash_mismatch_and_installs_nothing(tmp_path: Path):
    archive, _, size = _archive(tmp_path)
    with pytest.raises(ProvisionError, match="sha256"):
        provision(
            tmp_path / "runtime", manifest=_manifest("0" * 64, size), platform="linux64", fetch=_fetch_from(archive)
        )
    assert not (tmp_path / "runtime" / "chrome-headless-shell" / "linux64").exists()


def test_refuses_entries_that_escape_the_target(tmp_path: Path):
    archive, sha, size = _archive(tmp_path, evil=True)
    with pytest.raises(ProvisionError, match="unsafe"):
        provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="linux64", fetch=_fetch_from(archive))
    assert not (tmp_path / "escape.txt").exists()


def test_a_failed_extract_leaves_no_target_and_the_next_run_installs(tmp_path: Path):
    archive, sha, size = _archive(tmp_path)
    broken = tmp_path / "broken.zip"
    broken.write_bytes(archive.read_bytes()[: size // 2])
    with pytest.raises(ProvisionError):
        provision(
            tmp_path / "runtime",
            manifest=_manifest(hashlib.sha256(broken.read_bytes()).hexdigest(), size // 2),
            platform="linux64",
            fetch=_fetch_from(broken),
        )
    target = tmp_path / "runtime" / "chrome-headless-shell" / "linux64"
    assert not target.exists()
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="linux64", fetch=_fetch_from(archive))
    assert (target / "VERSION").read_text().strip() == VERSION


def test_reinstalls_when_the_executable_is_gone(tmp_path: Path):
    # Antivirus quarantine or a hand-deleted file: VERSION alone must not
    # convince a re-run of the installer that nothing is to be done.
    archive, sha, size = _archive(tmp_path)
    fetch = _fetch_from(archive)
    target = provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="linux64", fetch=fetch)
    (target / "chrome-headless-shell-linux64" / "chrome-headless-shell").unlink()
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="linux64", fetch=fetch)
    assert len(fetch.calls) == 2
    assert (target / "chrome-headless-shell-linux64" / "chrome-headless-shell").is_file()


def test_replaces_an_older_version(tmp_path: Path):
    archive, sha, size = _archive(tmp_path)
    target = tmp_path / "runtime" / "chrome-headless-shell" / "linux64"
    target.mkdir(parents=True)
    (target / "VERSION").write_text("1.0.0.0")
    (target / "stale.txt").write_text("old")
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="linux64", fetch=_fetch_from(archive))
    assert (target / "VERSION").read_text().strip() == VERSION
    assert not (target / "stale.txt").exists()
