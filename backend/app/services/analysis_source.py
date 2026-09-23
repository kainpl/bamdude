"""Local, read-only archive references for the isolated G-code parser."""

from __future__ import annotations

import ctypes
import os
import stat
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path


class AnalysisSourceError(ValueError):
    """The archive binding changed or is not a regular file in archive storage."""


@dataclass(frozen=True)
class AnalysisSource:
    path: str
    plate_id: int | None
    size: int
    mtime_ns: int
    device: int
    inode: int
    token: str

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict) -> AnalysisSource:
        if not isinstance(payload, dict) or set(payload) != {
            "path",
            "plate_id",
            "size",
            "mtime_ns",
            "device",
            "inode",
            "token",
        }:
            raise AnalysisSourceError("invalid local source descriptor")
        if (
            not isinstance(payload["path"], str)
            or not 0 < len(payload["path"]) <= 4096
            or not isinstance(payload["token"], str)
            or not 0 < len(payload["token"]) <= 256
        ):
            raise AnalysisSourceError("invalid local source identity")
        for key in ("size", "mtime_ns", "device", "inode"):
            if type(payload[key]) is not int or payload[key] < 0:
                raise AnalysisSourceError("invalid local source stat")
        if payload["plate_id"] is not None and (type(payload["plate_id"]) is not int or payload["plate_id"] < 1):
            raise AnalysisSourceError("invalid local source plate")
        return cls(**payload)


def resolve_source(
    *, base_dir: Path, archive_dir: Path, archive_file_path: str, plate_id: int | None, token: str
) -> AnalysisSource:
    """Build a descriptor only from an authoritative archive-row path."""
    if not archive_file_path or not token:
        raise AnalysisSourceError("missing archive binding")
    try:
        root = archive_dir.resolve(strict=True)
        path = (base_dir / archive_file_path).resolve(
            strict=True
        )  # SEC-PATH-OK: relative_to(root) below rejects escapes and symlinks
        path.relative_to(root)
        details = path.stat()
    except (OSError, ValueError) as exc:
        raise AnalysisSourceError("archive path is outside storage or unavailable") from exc
    if not stat.S_ISREG(details.st_mode):
        raise AnalysisSourceError("archive source is not a regular file")
    return AnalysisSource(
        path=str(path),
        plate_id=plate_id,
        size=details.st_size,
        mtime_ns=details.st_mtime_ns,
        device=details.st_dev,
        inode=details.st_ino,
        token=token,
    )


def _same_file(expected: AnalysisSource, details: os.stat_result) -> bool:
    return (
        stat.S_ISREG(details.st_mode)
        and details.st_size == expected.size
        and details.st_mtime_ns == expected.mtime_ns
        and details.st_dev == expected.device
        and details.st_ino == expected.inode
    )


def _open_shared_readonly(path: str):
    if os.name != "nt":
        return os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)), "rb")

    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    # GENERIC_READ, FILE_SHARE_READ|WRITE|DELETE, OPEN_EXISTING.
    handle = kernel32.CreateFileW(path, 0x80000000, 0x7, None, 3, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_last_error(), "could not open shared read-only archive")
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel32.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


@contextmanager
def open_verified_archive(source: AnalysisSource) -> Iterator[zipfile.ZipFile]:
    """One source handle, one ZIP, pre/post identity checks; no write access."""
    try:
        with _open_shared_readonly(source.path) as handle:
            if not _same_file(source, os.fstat(handle.fileno())):
                raise AnalysisSourceError("archive source changed before parsing")
            with zipfile.ZipFile(handle, "r") as archive:
                yield archive
            if not _same_file(source, os.fstat(handle.fileno())):
                raise AnalysisSourceError("archive source changed during parsing")
    except (OSError, zipfile.BadZipFile) as exc:
        raise AnalysisSourceError("archive source could not be read") from exc
