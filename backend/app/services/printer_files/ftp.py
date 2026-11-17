"""The external medium, over the FTP client we already had.

A thin adapter, on purpose: every behaviour worth keeping — per-IP
serialisation, the A1 socket-timeout workarounds, the model-specific TLS
handling — already lives in ``bambu_ftp`` and is reached by calling it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from backend.app.services.bambu_ftp import (
    DeleteResult,
    delete_file_async,
    delete_file_verified_async,
    download_file_bytes_async,
    download_to_path_async,
    list_directory_async,
    list_files_async,
    upload_file_async,
)
from backend.app.services.printer_files.base import FILE_TYPE_MODEL, RemoteFile, TransferOutcome


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
                mtime_quality=entry.get("mtime_quality"),
            )
            for entry in entries
        ]

    async def list_directory(
        self, path: str, file_type: str = FILE_TYPE_MODEL
    ) -> tuple[str, list[RemoteFile], str | None]:
        """The typed listing: a 550 is told apart from absence only by a complete parent listing.

        No caller cap here — a coalesced listing waits for its worker, whose stall
        is the socket idle deadline; each caller bounds its own wait.
        """
        listing = await list_directory_async(
            self._ip, self._access_code, path, timeout=None, printer_model=self._model, prove_missing=True
        )
        files = [
            RemoteFile(
                name=entry["name"],
                path=entry.get("path") or self._join(path, entry["name"]),
                size=entry.get("size", 0),
                is_directory=bool(entry.get("is_directory")),
                mtime=entry.get("mtime"),
                mtime_quality=entry.get("mtime_quality"),
            )
            for entry in listing.entries
        ]
        return listing.status, files, listing.reason

    async def read_bytes(self, path: str) -> bytes | None:
        return await download_file_bytes_async(self._ip, self._access_code, path, printer_model=self._model)

    async def download_to_file(
        self,
        path: str,
        destination: Path,
        *,
        byte_budget: int | None = None,
        reserve_bytes: int = 0,
        progress: Callable[[int], None] | None = None,
    ) -> TransferOutcome:
        return await download_to_path_async(
            self._ip,
            self._access_code,
            path,
            destination,
            printer_model=self._model,
            byte_budget=byte_budget,
            reserve_bytes=reserve_bytes,
            progress=progress,
        )

    async def delete(self, path: str, *, may_run: Callable[[], bool] | None = None) -> DeleteResult:
        return await delete_file_async(self._ip, self._access_code, path, printer_model=self._model, may_run=may_run)

    async def delete_verified(
        self,
        path: str,
        *,
        ready: Callable[[], bool],
        verify: Callable[[bytes | None], bool],
        may_run: Callable[[], bool] | None = None,
    ) -> DeleteResult:
        return await delete_file_verified_async(
            self._ip, self._access_code, path, printer_model=self._model, ready=ready, verify=verify, may_run=may_run
        )

    async def upload(
        self,
        local_path: Path,
        remote_name: str,
        progress_cb: Callable[[int, int], None] | None = None,
        *,
        may_run: Callable[[], bool] | None = None,
    ) -> bool:
        """The printer's FTP root is the card, so the name is the whole path."""
        return bool(
            await upload_file_async(
                self._ip,
                self._access_code,
                str(local_path),
                f"/{remote_name}",
                progress_callback=progress_cb,
                printer_model=self._model,
                may_run=may_run,
            )
        )

    @staticmethod
    def _join(directory: str, name: str) -> str:
        return f"/{name}" if directory == "/" else f"{directory.rstrip('/')}/{name}"
