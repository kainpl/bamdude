import hashlib
import os
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from backend.app.services.render_browser import ProvisionError, check_libraries, elf_files, platform_key, provision

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


def test_replaces_an_older_version(tmp_path: Path):
    archive, sha, size = _archive(tmp_path)
    target = tmp_path / "runtime" / "chrome-headless-shell" / "linux64"
    target.mkdir(parents=True)
    (target / "VERSION").write_text("1.0.0.0")
    (target / "stale.txt").write_text("old")
    provision(tmp_path / "runtime", manifest=_manifest(sha, size), platform="linux64", fetch=_fetch_from(archive))
    assert (target / "VERSION").read_text().strip() == VERSION
    assert not (target / "stale.txt").exists()


def _ldd(stdout: str = "", stderr: str = "", code: int = 0):
    def run(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, code, stdout=stdout, stderr=stderr)

    return run


OK = "\tlinux-vdso.so.1 (0x00007ffd)\n\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x7f)\n"


def test_clean_ldd_output_is_ok():
    assert check_libraries([Path("chrome-headless-shell")], run=_ldd(OK)).ok


def test_not_found_lines_are_missing_libraries():
    out = OK + "\tlibnss3.so => not found\n\tlibgbm.so.1 => not found\n"
    result = check_libraries([Path("chrome-headless-shell")], run=_ldd(out))
    assert not result.ok
    assert result.missing == ["chrome-headless-shell: libnss3.so", "chrome-headless-shell: libgbm.so.1"]


def test_version_errors_are_missing():
    out = (
        OK
        + "./chrome-headless-shell: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found (required by ./chrome-headless-shell)\n"
    )
    result = check_libraries([Path("chrome-headless-shell")], run=_ldd(out))
    assert not result.ok and result.missing


def test_a_failed_ldd_is_an_error_not_a_pass():
    result = check_libraries([Path("chrome-headless-shell")], run=_ldd("", "ldd: cannot execute", code=1))
    assert not result.ok
    assert result.errors and "exited 1" in result.errors[0]


def test_stderr_is_an_error():
    result = check_libraries(
        [Path("libvk_swiftshader.so")], run=_ldd(OK, "warning: you do not have execution permission")
    )
    assert not result.ok and result.errors


def test_empty_or_unusable_output_is_an_error():
    assert not check_libraries([Path("x")], run=_ldd("")).ok
    assert not check_libraries([Path("x")], run=_ldd("\tnot a dynamic executable\n")).ok


def test_elf_files_include_every_shipped_shared_object(tmp_path: Path):
    exe = tmp_path / "chrome-headless-shell"
    exe.write_bytes(b"")
    (tmp_path / "libvk_swiftshader.so").write_bytes(b"")
    (tmp_path / "libEGL.so").write_bytes(b"")
    (tmp_path / "resources.pak").write_bytes(b"")
    names = [p.name for p in elf_files(tmp_path, exe)]
    assert names == ["chrome-headless-shell", "libEGL.so", "libvk_swiftshader.so"]
