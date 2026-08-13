"""The external medium, over the FTP client we already had.

A thin adapter, on purpose: every behaviour worth keeping — per-IP
serialisation, the A1 socket-timeout workarounds, the model-specific TLS
handling — already lives in ``bambu_ftp`` and is reached by calling it.
"""

from __future__ import annotations

from backend.app.services.bambu_ftp import (
    DeleteResult,
    delete_file_async,
    download_file_bytes_async,
    list_files_async,
)
from backend.app.services.printer_files.base import FILE_TYPE_MODEL, RemoteFile


class FtpTransport:
    def __init__(self, printer):
        self._ip = printer.ip_address
        self._access_code = printer.access_code
        self._model = printer.model

    async def list_files(self, path: str, file_type: str = FILE_TYPE_MODEL) -> list[RemoteFile]:
        """⚠️ ``file_type`` is accepted and ignored, on purpose.

        On the card a timelapse is not a catalogue but a directory —
        ``/timelapse`` — so the path the caller already passed says which is
        meant. Acting on the argument here would invent a second way to express
        the same thing, and the two would eventually disagree.
        """
        entries = await list_files_async(self._ip, self._access_code, path, printer_model=self._model)
        return [
            RemoteFile(
                name=entry["name"],
                path=entry.get("path") or self._join(path, entry["name"]),
                size=entry.get("size", 0),
                is_directory=bool(entry.get("is_directory")),
                mtime=entry.get("mtime"),
            )
            for entry in entries
        ]

    async def read_bytes(self, path: str) -> bytes | None:
        return await download_file_bytes_async(self._ip, self._access_code, path, printer_model=self._model)

    async def delete(self, path: str) -> DeleteResult:
        return await delete_file_async(self._ip, self._access_code, path, printer_model=self._model)

    @staticmethod
    def _join(directory: str, name: str) -> str:
        return f"/{name}" if directory == "/" else f"{directory.rstrip('/')}/{name}"
