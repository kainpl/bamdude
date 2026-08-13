"""One narrow protocol over two very different transports.

⚠️ **The protocol covers only what BOTH transports do honestly.** FTP also
carries firmware, logs, directory traversal and ``clear-sdcard``; the tunnel
carries model and timelapse catalogues and will never do the rest. Those
operations keep calling ``bambu_ftp`` directly and are deliberately absent from
this interface — one that pretended the two were interchangeable would be false
exactly where somebody leaned on it.

⚠️ **Upload is absent on purpose.** It arrives with the dispatch stage, when
both implementations can honour it. A method that one implementation raises on
is the design this replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

# Re-exported rather than redefined: a delete has three outcomes, not two, and
# flattening "no such file" into "failed" is the exact mistake this enum was
# introduced to stop (see its docstring in bambu_ftp).
from backend.app.services.bambu_ftp import DeleteResult

__all__ = ["DeleteResult", "PrinterFileTransport", "RemoteFile"]


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

    def as_dict(self) -> dict:
        entry: dict = {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "is_directory": self.is_directory,
        }
        if self.mtime is not None:
            entry["mtime"] = self.mtime
        return entry


class PrinterFileTransport(Protocol):
    async def list_files(self, path: str) -> list[RemoteFile]: ...

    async def read_bytes(self, path: str) -> bytes | None: ...

    async def delete(self, path: str) -> DeleteResult: ...
