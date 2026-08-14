"""The internal medium, over the file tunnel.

⚠️ **This file is the only place that knows the wire names of storages.** The
same two media are spelled four ways depending on the operation — key absent
for external listing, ``internal`` for internal listing, ``emmc`` and ``udisk``
for upload — and none of the four derives from another. Above this layer the
vocabulary is ``external`` / ``internal`` and nothing else.

Each operation opens and closes its own connection. The printer's tunnel is
not a session we hold: the browser's calls are seconds apart and a socket kept
open across them would be one more thing to notice a reconnect.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from backend.app.services.bambu_tunnel.client import (
    DEFAULT_PORT,
    WIRE_EMMC,
    WIRE_INTERNAL,
    BambuTunnelClient,
    TunnelError,
)
from backend.app.services.printer_files.base import FILE_TYPE_MODEL, DeleteResult, RemoteFile

logger = logging.getLogger(__name__)


class TunnelTransport:
    def __init__(self, printer, *, port: int = DEFAULT_PORT, connector=None):
        self._ip = printer.ip_address
        self._access_code = printer.access_code
        self._port = port
        # Tests inject a plain-TCP connector so no certificate is involved.
        self._connector = connector

    def _client(self) -> BambuTunnelClient:
        return BambuTunnelClient(
            self._ip,
            self._access_code,
            port=self._port,
            connector=self._connector,
        )

    async def list_files(self, path: str, file_type: str = FILE_TYPE_MODEL) -> list[RemoteFile]:
        """``path`` is ignored: the internal catalogue is flat, not a tree.

        Asking for a subdirectory is not an error here — there are none, so the
        only honest answer is the same catalogue. ``file_type`` is what actually
        selects between models and timelapses, because on this medium they are
        two catalogues rather than two directories.

        ⚠️ Their availability is gated by two DIFFERENT flags: models by ``fun2``
        bit 17, timelapses by ``fun`` bit 28. A machine can have one and not the
        other, so the caller must check the right one — see
        ``utils/timelapse.py::capability_for`` for the timelapse side.
        """
        async with self._client() as client:
            entries = await client.list_files(WIRE_INTERNAL, file_type=file_type)
        return [self._to_remote(entry) for entry in entries]

    async def read_bytes(self, path: str) -> bytes | None:
        """⚠️ Two different commands, chosen by the shape of the path.

        A whole file is ``FILE_DOWNLOAD`` — one request, many streamed frames.
        A member of a container (``…gcode.3mf#Metadata/plate_1.png``) is
        ``SUB_FILE``. They are not interchangeable: handing a plain path to
        SUB_FILE gets ``result=14`` from the printer, which is how this was
        found — every whole-file read failed on the first real machine while
        passing every test, because the fake answered SUB_FILE with the bytes.
        """
        async with self._client() as client:
            try:
                if "#" in path:
                    return await client.read_sub_file(path, WIRE_INTERNAL)
                return await client.download_file(path)
            except TunnelError as exc:
                # None becomes a 404 at the route. Raising here would become a
                # 500 and blame us for the printer's answer.
                logger.info("[%s] tunnel read refused for %s: %s", self._ip, path, exc)
                return None

    async def delete(self, path: str) -> DeleteResult:
        """⚠️ Never reports ``NOT_FOUND``.

        The tunnel's error codes have never been collected, so a refusal cannot
        be told apart from "no such file". ``FAILED`` is the honest answer;
        guessing ``NOT_FOUND`` would turn a real failure into a reassuring 404.
        """
        async with self._client() as client:
            try:
                await client.delete_files([path])
                return DeleteResult.DELETED
            except TunnelError as exc:
                logger.info("[%s] tunnel delete refused for %s: %s", self._ip, path, exc)
                return DeleteResult.FAILED

    async def upload(
        self,
        local_path: Path,
        remote_name: str,
        progress_cb: Callable[[int, int], None] | None = None,
    ) -> bool:
        """⚠️ Only ``TunnelError`` is caught.

        The dispatcher cancels a job by raising from inside ``progress_cb``, and
        turning that into ``False`` would report a cancelled job as a failed
        transfer — two different things with two different follow-ups.
        """
        async with self._client() as client:
            try:
                await client.upload_file(local_path, remote_name, WIRE_EMMC, progress_cb=progress_cb)
                return True
            except TunnelError as exc:
                logger.info("[%s] tunnel upload refused for %s: %s", self._ip, remote_name, exc)
                return False

    @staticmethod
    def _to_remote(entry: dict) -> RemoteFile:
        raw_time = entry.get("time")
        return RemoteFile(
            name=entry.get("name", ""),
            path=entry.get("path", ""),
            size=entry.get("size", 0),
            is_directory=False,
            mtime=datetime.fromtimestamp(raw_time, tz=UTC) if raw_time else None,
            # Empty in the model catalogue, the printed model's name in the
            # timelapse one — see RemoteFile.
            model_name=entry.get("model_name") or None,
        )
