"""One narrow protocol over two very different transports.

⚠️ **The protocol covers only what BOTH transports do honestly.** FTP also
carries firmware, logs, directory traversal and ``clear-sdcard``; the tunnel
carries model and timelapse catalogues and will never do the rest. Those
operations keep calling ``bambu_ftp`` directly and are deliberately absent from
this interface — one that pretended the two were interchangeable would be false
exactly where somebody leaned on it.

Upload joined in the dispatch stage, once both implementations could honour it
— which was the condition for adding it at all. A method that one side raises
on is the design this replaced.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

# Re-exported rather than redefined: a delete has three outcomes, not two, and
# flattening "no such file" into "failed" is the exact mistake this enum was
# introduced to stop (see its docstring in bambu_ftp).
from backend.app.services.bambu_ftp import DeleteResult

__all__ = [
    "FILE_TYPES",
    "FILE_TYPE_MODEL",
    "FILE_TYPE_TIMELAPSE",
    "TRANSFER_STATUSES",
    "DeleteResult",
    "PrinterFileTransport",
    "RemoteFile",
    "TransferOutcome",
]


@dataclass(frozen=True)
class RemoteFile:
    """A file on the printer, in the shape the API already returns.

    ``mtime`` stays optional because the FTP listing parses a date only on a
    best-effort basis, and the endpoint omits the key rather than sending null.
    """

    name: str
    path: str
    size: int
    is_directory: bool = False
    mtime: datetime | None = None
    # How much of ``mtime`` is known (G10 A6): ``exact`` (an instant, the tunnel's
    # epoch), ``year_inferred`` / ``date_only`` (an FTP wall time, zone unknown),
    # ``unknown``. A quality is never a proof of the printer's time zone.
    mtime_quality: str | None = None
    # ⚠️ Only the tunnel's timelapse catalogue fills this, and it is worth
    # carrying: it holds the printed model's name — the same string the print
    # command sent as ``subtask_name`` — which is an exact key for matching a
    # recording to its archive. FTP listings have no such field, which is why
    # the FTP path has to guess from filenames and timestamps.
    model_name: str | None = None

    def as_dict(self) -> dict:
        entry: dict = {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "is_directory": self.is_directory,
        }
        if self.mtime is not None:
            entry["mtime"] = self.mtime
        if self.mtime_quality:
            entry["mtime_quality"] = self.mtime_quality
        if self.model_name:
            entry["model_name"] = self.model_name
        return entry


# The two catalogues the tunnel serves. ⚠️ On the external medium these are not
# catalogues at all — ``/timelapse`` is an ordinary directory and the path
# already says which is meant. The argument exists for the medium where the
# distinction is a field in the request rather than a place on a disk.
FILE_TYPE_MODEL = "model"
FILE_TYPE_TIMELAPSE = "timelapse"
FILE_TYPES = (FILE_TYPE_MODEL, FILE_TYPE_TIMELAPSE)


# What one download-to-file proved (G10 A, M01). Wrappers never turn a cancel, an
# unavailable printer or a short read into an empty file or an ordinary "missing".
TRANSFER_COMPLETED = "completed"
TRANSFER_NOT_READABLE = "not_readable"  # the printer refused the path: missing OR forbidden
TRANSFER_UNAVAILABLE = "unavailable"  # no session, or the transport failed mid-file
TRANSFER_STALLED = "stalled"  # the socket idle deadline ran out
TRANSFER_CANCELLED = "cancelled"
TRANSFER_LIMIT_EXCEEDED = "limit_exceeded"  # more bytes than the caller's budget
TRANSFER_INSUFFICIENT_STORAGE = "insufficient_storage"  # the local volume fell under its reserve
TRANSFER_SHORT_READ = "short_read"  # fewer bytes than the printer said the file has
TRANSFER_PROTOCOL_FAILED = "protocol_failed"  # framing, offset, total or checksum broken
TRANSFER_BUSY = "busy"  # never started: the lane or a worker slot was not granted, or restoring
TRANSFER_STATUSES = (
    TRANSFER_COMPLETED,
    TRANSFER_NOT_READABLE,
    TRANSFER_UNAVAILABLE,
    TRANSFER_STALLED,
    TRANSFER_CANCELLED,
    TRANSFER_LIMIT_EXCEEDED,
    TRANSFER_INSUFFICIENT_STORAGE,
    TRANSFER_SHORT_READ,
    TRANSFER_PROTOCOL_FAILED,
    TRANSFER_BUSY,
)


@dataclass(frozen=True)
class TransferOutcome:
    """The typed result of a download to a local file.

    ``received`` counts the bytes actually written. ``check`` says what proved
    a completed file whole: ``checksum_verified`` (the printer's MD5 matched),
    ``size_verified`` (its SIZE matched), ``length_verified`` (the stated total
    and no checksum), ``size_unknown`` (no size to compare — the transfer ended
    normally, nothing more is known). ``reason`` is a safe, fixed detail.
    On anything but ``completed`` the destination file does not exist.
    """

    status: str
    received: int = 0
    check: str | None = None
    reason: str | None = None

    @property
    def completed(self) -> bool:
        return self.status == TRANSFER_COMPLETED


class PrinterFileTransport(Protocol):
    async def list_files(self, path: str, file_type: str = FILE_TYPE_MODEL) -> list[RemoteFile]: ...

    async def list_directory(
        self, path: str, file_type: str = FILE_TYPE_MODEL
    ) -> tuple[str, list[RemoteFile], str | None]:
        """``(status, files, reason)`` — what the listing proves (see printer_files.listing)."""
        ...

    async def read_bytes(self, path: str) -> bytes | None: ...

    async def download_to_file(
        self,
        path: str,
        destination: Path,
        *,
        byte_budget: int | None = None,
        reserve_bytes: int = 0,
        progress: Callable[[int], None] | None = None,
    ) -> TransferOutcome:
        """Stream one whole file to ``destination``, a path the caller owns.

        ``byte_budget`` stops a transfer that outgrows it; ``reserve_bytes`` stops
        one that would leave the destination volume with less free space;
        ``progress`` gets the byte count so far (it may be called from a worker
        thread). Nothing about the file is held in memory but one chunk.
        """
        ...

    async def delete(self, path: str, *, may_run: Callable[[], bool] | None = None) -> DeleteResult:
        """Delete one file. ``may_run`` is asked after the lane wait, immediately before the command."""
        ...

    async def upload(
        self,
        local_path: Path,
        remote_name: str,
        progress_cb: Callable[[int, int], None] | None = None,
        *,
        may_run: Callable[[], bool] | None = None,
    ) -> bool: ...
