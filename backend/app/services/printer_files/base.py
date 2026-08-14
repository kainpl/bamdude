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
    "DeleteResult",
    "PrinterFileTransport",
    "RemoteFile",
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


class PrinterFileTransport(Protocol):
    async def list_files(self, path: str, file_type: str = FILE_TYPE_MODEL) -> list[RemoteFile]: ...

    async def read_bytes(self, path: str) -> bytes | None: ...

    async def delete(self, path: str) -> DeleteResult: ...

    async def upload(
        self,
        local_path: Path,
        remote_name: str,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> bool: ...
