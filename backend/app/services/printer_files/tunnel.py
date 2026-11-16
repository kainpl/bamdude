"""The internal medium, over the file tunnel.

⚠️ **This file is the only place that knows the wire names of storages.** The
same two media are spelled four ways depending on the operation — key absent
for external listing, ``internal`` for internal listing, ``emmc`` and ``udisk``
for upload — and none of the four derives from another. Above this layer the
vocabulary is ``external`` / ``internal`` and nothing else.

Each operation opens and closes its own connection. The printer's tunnel is
not a session we hold: the browser's calls are seconds apart and a socket kept
open across them would be one more thing to notice a reconnect.

⚠️ **One operation per printer endpoint at a time** (G10 A3). Each runs as a
task the printer file runtime owns, on the endpoint's tunnel lane — separate
from the FTP lane — and the lane is released by that task once its connection
has closed. BambuStudio keeps one tunnel and downloads one file at a time; how
many sessions the firmware accepts at once has never been measured, so we do
not open a second.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from backend.app.services.bambu_tunnel.client import (
    DEFAULT_PORT,
    WIRE_EMMC,
    WIRE_INTERNAL,
    BambuTunnelClient,
    TunnelError,
    TunnelProtocolError,
)
from backend.app.services.printer_file_lanes import LaneRefused, printer_file_runtime, tunnel_key
from backend.app.services.printer_files.base import (
    FILE_TYPE_MODEL,
    TRANSFER_BUSY,
    TRANSFER_COMPLETED,
    TRANSFER_INSUFFICIENT_STORAGE,
    TRANSFER_LIMIT_EXCEEDED,
    TRANSFER_NOT_READABLE,
    TRANSFER_PROTOCOL_FAILED,
    TRANSFER_UNAVAILABLE,
    DeleteResult,
    RemoteFile,
    TransferOutcome,
)
from backend.app.services.printer_files.staging import ReserveSchedule, discard_staging

logger = logging.getLogger(__name__)


class _Stop(Exception):
    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _finish(handle) -> None:
    """Flush and close a completed download's file: one step, on one thread."""
    try:
        handle.flush()
    finally:
        handle.close()


def _discard(handle, destination: Path) -> None:
    """Close a failed download's file, then remove it — one step, so no cancel can fall between."""
    try:
        if not handle.closed:
            handle.close()
    except OSError:
        pass  # a close that cannot flush still releases the descriptor; the file goes next
    finally:
        discard_staging(destination)


async def _settle(awaitable):
    """Run cleanup to its end through any number of cancels; raise the cancel afterwards.

    A failure of the cleanup itself outranks the cancel: a file left behind must
    be seen, not hidden behind "cancelled".
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    if task.cancelled():
        raise asyncio.CancelledError
    error = task.exception()
    if error is not None:
        raise error
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


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

    async def _on_lane(self, name: str, factory):
        return await printer_file_runtime.run_task(tunnel_key(self._ip, self._port), name, factory)

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

        async def operation():
            async with self._client() as client:
                return await client.list_files(WIRE_INTERNAL, file_type=file_type)

        entries = await self._on_lane("list", operation)
        return [self._to_remote(entry) for entry in entries]

    async def list_directory(
        self, path: str, file_type: str = FILE_TYPE_MODEL
    ) -> tuple[str, list[RemoteFile], str | None]:
        """The typed listing of a catalogue: complete, or unavailable with a safe reason."""
        try:
            return "complete", await self.list_files(path, file_type=file_type), None
        except TunnelProtocolError as exc:
            return "unavailable", [], exc.category
        except TunnelError:
            return "unavailable", [], "tunnel"
        except LaneRefused as refused:
            return "unavailable", [], refused.reason

    async def read_bytes(self, path: str) -> bytes | None:
        """⚠️ Two different commands, chosen by the shape of the path.

        A whole file is ``FILE_DOWNLOAD`` — one request, many streamed frames.
        A member of a container (``…gcode.3mf#Metadata/plate_1.png``) is
        ``SUB_FILE``. They are not interchangeable: handing a plain path to
        SUB_FILE gets ``result=14`` from the printer, which is how this was
        found — every whole-file read failed on the first real machine while
        passing every test, because the fake answered SUB_FILE with the bytes.

        ⚠️ The whole file is held in memory; new consumers use
        :meth:`download_to_file`.
        """

        async def operation():
            async with self._client() as client:
                if "#" in path:
                    return await client.read_sub_file(path, WIRE_INTERNAL)
                return await client.download_file(path)

        try:
            return await self._on_lane("read", operation)
        except TunnelProtocolError as exc:
            logger.warning("[%s] tunnel read of %s broke the framing (%s)", self._ip, path, exc.category)
            return None
        except TunnelError as exc:
            # None becomes a 404 at the route. Raising here would become a
            # 500 and blame us for the printer's answer.
            logger.info("[%s] tunnel read refused for %s: %s", self._ip, path, exc)
            return None
        except LaneRefused as refused:
            logger.info("[%s] tunnel read of %s not started (%s)", self._ip, path, refused.reason)
            return None

    async def download_to_file(
        self,
        path: str,
        destination: Path,
        *,
        byte_budget: int | None = None,
        reserve_bytes: int = 0,
        progress: Callable[[int], None] | None = None,
    ) -> TransferOutcome:
        """Stream one whole file to ``destination``: each frame's body goes to disk as it comes.

        Disk work runs off the loop; the frame bounds, offsets, total and the
        printer's ``file_md5`` are checked by the client. The volume's reserve
        is checked before the first write, every 8 MiB and for the unchecked
        tail (review A5).

        ⚠️ The file is owned from the open to its removal (review A2), inside
        the lane-owned task: a cancel during the open still leaves this code the
        handle the open's thread returns, and the cleanup — close, then remove,
        one step on one thread — runs to its end through any number of further
        cancels before the task, and with it the lane, lets go. Anything short
        of a completed, checked file removes the destination; one that cannot
        be removed raises :class:`StagingNotRemoved` instead of an outcome.
        """
        from backend.app.services.preview_artifacts import owned

        received = 0
        reserve = ReserveSchedule(reserve_bytes)
        volume = destination.parent

        async def free_space() -> int:
            return (await owned(asyncio.to_thread(shutil.disk_usage, volume))).free

        async def operation() -> TransferOutcome:
            nonlocal received
            handle = None
            completed = False

            async def open_staging() -> None:
                nonlocal handle
                await asyncio.to_thread(volume.mkdir, parents=True, exist_ok=True)
                # Assigned the moment the thread returns, even when a cancel came
                # in the meantime: ``owned`` waits for this task to end.
                handle = await asyncio.to_thread(open, destination, "wb")

            try:
                await owned(open_staging())

                async def sink(chunk) -> None:
                    nonlocal received
                    received += len(chunk)
                    if byte_budget is not None and received > byte_budget:
                        raise _Stop(TRANSFER_LIMIT_EXCEEDED, "byte_budget")
                    if reserve.due_before(received) and not reserve.fits(await free_space(), len(chunk)):
                        raise _Stop(TRANSFER_INSUFFICIENT_STORAGE, "reserve")
                    await owned(asyncio.to_thread(handle.write, chunk))
                    if progress is not None:
                        progress(received)

                async with self._client() as client:
                    receipt = await client.download_into(path, sink)
                await owned(asyncio.to_thread(_finish, handle))
                if reserve.due_final(received) and not reserve.fits(await free_space()):
                    raise _Stop(TRANSFER_INSUFFICIENT_STORAGE, "reserve")
                completed = True
                return TransferOutcome(TRANSFER_COMPLETED, receipt.total, receipt.checksum)
            finally:
                if handle is not None and not completed:
                    await _settle(asyncio.to_thread(_discard, handle, destination))

        try:
            return await self._on_lane("fetch", operation)
        except _Stop as stop:
            return TransferOutcome(stop.status, received, reason=stop.reason)
        except TunnelProtocolError as exc:
            return TransferOutcome(TRANSFER_PROTOCOL_FAILED, received, reason=exc.category)
        except TunnelError as exc:
            # A refused download names no reason we could tell apart (the tunnel's
            # error codes have never been collected): not readable, not "missing".
            status = TRANSFER_NOT_READABLE if exc.result > 0 else TRANSFER_UNAVAILABLE
            return TransferOutcome(status, received, reason=f"result={exc.result}" if exc.result > 0 else None)
        except LaneRefused as refused:
            return TransferOutcome(TRANSFER_BUSY, reason=refused.reason)

    async def delete(self, path: str, *, may_run: Callable[[], bool] | None = None) -> DeleteResult:
        """⚠️ Never reports ``NOT_FOUND``.

        The tunnel's error codes have never been collected, so a refusal cannot
        be told apart from "no such file". ``FAILED`` is the honest answer;
        guessing ``NOT_FOUND`` would turn a real failure into a reassuring 404.
        """

        async def operation() -> DeleteResult:
            async with self._client() as client:
                if may_run is not None and not may_run():
                    return DeleteResult.FAILED
                await client.delete_files([path])
                return DeleteResult.DELETED

        try:
            return await self._on_lane("delete", operation)
        except TunnelError as exc:
            logger.info("[%s] tunnel delete refused for %s: %s", self._ip, path, exc)
            return DeleteResult.FAILED
        except LaneRefused as refused:
            logger.info("[%s] tunnel delete of %s not started (%s)", self._ip, path, refused.reason)
            return DeleteResult.FAILED

    async def upload(
        self,
        local_path: Path,
        remote_name: str,
        progress_cb: Callable[[int, int], None] | None = None,
        *,
        may_run: Callable[[], bool] | None = None,
    ) -> bool:
        """⚠️ Only ``TunnelError`` is caught.

        The dispatcher cancels a job by raising from inside ``progress_cb``, and
        turning that into ``False`` would report a cancelled job as a failed
        transfer — two different things with two different follow-ups.
        """

        async def operation() -> bool:
            async with self._client() as client:
                # After the lane wait and the connection, before the first byte (G10 B).
                if may_run is not None and not may_run():
                    return False
                await client.upload_file(local_path, remote_name, WIRE_EMMC, progress_cb=progress_cb)
                return True

        try:
            return await self._on_lane("upload", operation)
        except TunnelError as exc:
            logger.info("[%s] tunnel upload refused for %s: %s", self._ip, remote_name, exc)
            return False
        except LaneRefused as refused:
            logger.info("[%s] tunnel upload of %s not started (%s)", self._ip, remote_name, refused.reason)
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
            # An epoch is an instant: this one is known, unlike an FTP wall time.
            mtime_quality=("exact" if raw_time else "unknown"),
            # Empty in the model catalogue, the printed model's name in the
            # timelapse one — see RemoteFile.
            model_name=entry.get("model_name") or None,
        )
