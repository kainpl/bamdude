import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# A slow browser gets disconnected and receives a new snapshot on reconnect.
# Never discard/reorder print/queue events to keep a stalled socket alive.
MAX_PENDING_MESSAGES = 256
MAX_PENDING_BYTES = 4 * 1024 * 1024
SEND_TIMEOUT_SECONDS = 5.0


@dataclass
class _Outbox:
    messages: deque[tuple[str, int]] = field(default_factory=deque)
    pending_bytes: int = 0
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    progress: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    close_code: int | None = None


class ConnectionManager:
    """Manages WebSocket connections and broadcasts."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        # Per-connection user id (None for API-key callers) — enables per-user
        # broadcasts. BamDude has no anonymous users, so this is almost always set.
        self._user_by_conn: dict[WebSocket, int | None] = {}
        # In-process subscribers to the same fan-out the browsers get — today
        # only the Cloud Link uplink. See ``add_internal_listener``.
        self._internal_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._outboxes: dict[WebSocket, _Outbox] = {}

    # ------------------------------------------------------ internal listeners

    def add_internal_listener(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Subscribe an in-process consumer to every broadcast message.

        One choke point instead of a hook at each of the two dozen callsites
        that broadcast: whatever the product learns to push tomorrow, a
        listener sees it without anybody remembering to wire it up.

        The contract for ``cb`` is narrow on purpose — **synchronous, fast, and
        it must not block.** It is called from inside ``broadcast``, on the
        event loop, before a single browser has been written to; anything that
        awaits or does I/O there delays every printer card in the app. The
        intended shape is an enqueue and nothing else (see
        ``services/cloud_link/uplink.py::Uplink.feed``).

        A listener that raises is swallowed — see ``_notify_internal_listeners``
        for why that is not the usual anti-pattern.
        """
        if cb not in self._internal_listeners:
            self._internal_listeners.append(cb)

    def remove_internal_listener(self, cb: Callable[[dict[str, Any]], None]) -> None:
        """Unsubscribe. Removing something that was never added is not an error
        — a link being switched off should not have to remember whether it was
        ever switched on."""
        if cb in self._internal_listeners:
            self._internal_listeners.remove(cb)

    def _notify_internal_listeners(self, message: dict[str, Any]) -> None:
        """Hand the message to each listener, and let none of them escape.

        ⚠️ **The swallow is load-bearing.** ``broadcast`` feeds every printer
        card, queue view and archive list in the product. A listener is an
        optional add-on — Cloud Link ships disabled — and an add-on must never
        be able to take the dashboard down with it. Debug rather than warning
        because a broken listener would otherwise log once per status push,
        several times a second per printer, and bury everything else.

        Failures are per-listener, so one bad subscriber cannot rob the next
        one of the message.

        Iterated over a copy: a listener is allowed to unregister itself from
        inside its own call — a link shutting down on the very message that
        told it to — and mutating the list mid-iteration would silently skip
        whoever happened to be standing next to it.
        """
        for cb in list(self._internal_listeners):
            try:
                cb(message)
            except Exception as e:
                logger.debug("Internal broadcast listener raised (ignored): %s", e)

    async def connect(self, websocket: WebSocket, user_id: int | None = None):
        """Accept a new WebSocket connection, tagged with the authenticated user
        so per-user broadcasts can target it."""
        await websocket.accept()
        self.active_connections.append(websocket)
        self._user_by_conn[websocket] = user_id
        outbox = _Outbox()
        self._outboxes[websocket] = outbox
        outbox.task = asyncio.create_task(self._writer(websocket, outbox), name="websocket-writer")

    def _remove(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        self._user_by_conn.pop(websocket, None)

    def _enqueue(self, websocket: WebSocket, data: str, size: int) -> None:
        outbox = self._outboxes.get(websocket)
        if outbox is None or outbox.close_code is not None:
            return
        if len(outbox.messages) >= MAX_PENDING_MESSAGES or outbox.pending_bytes + size > MAX_PENDING_BYTES:
            logger.warning(
                "WebSocket outbox overflow: pending=%s bytes=%s; closing slow client",
                len(outbox.messages),
                outbox.pending_bytes,
            )
            # The writer owns close as well as send. No second concurrent ASGI
            # writer, including when overflow occurs inside a broadcast.
            outbox.close_code = 1013
            self._remove(websocket)
            outbox.messages.clear()
            outbox.pending_bytes = 0
        else:
            outbox.messages.append((data, size))
            outbox.pending_bytes += size
        outbox.wake.set()

    async def _writer(self, websocket: WebSocket, outbox: _Outbox) -> None:
        try:
            while outbox.close_code is None:
                await outbox.wake.wait()
                outbox.wake.clear()
                while outbox.messages and outbox.close_code is None:
                    data, size = outbox.messages.popleft()
                    outbox.pending_bytes -= size
                    started = time.monotonic()
                    # asyncio.timeout keeps one task per client, not per frame.
                    async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                        await websocket.send_text(data)
                    outbox.progress.set()
                    elapsed = time.monotonic() - started
                    if elapsed >= 0.25:
                        logger.warning(
                            "Slow WebSocket send: elapsed=%.3fs pending=%s bytes=%s",
                            elapsed,
                            len(outbox.messages),
                            outbox.pending_bytes,
                        )
        except TimeoutError:
            logger.warning("WebSocket send timed out; closing slow client")
            outbox.close_code = 1013
        except asyncio.CancelledError:
            raise
        except Exception:
            outbox.close_code = 1011
        finally:
            self._remove(websocket)
            outbox.messages.clear()
            outbox.pending_bytes = 0
            outbox.progress.set()
            try:
                if outbox.close_code is not None:
                    with suppress(Exception):
                        async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
                            await websocket.close(code=outbox.close_code)
            finally:
                self._outboxes.pop(websocket, None)

    async def send(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        """Enqueue a direct reply, pacing only this client's snapshot producer."""
        data = json.dumps(message)
        self._enqueue(websocket, data, len(data.encode("utf-8")))
        outbox = self._outboxes.get(websocket)
        # A large bootstrap must give its own writer time to drain. A single
        # sleep(0) is insufficient when ASGI send itself yields several times.
        # Broadcast producers never wait here, and can keep feeding all viewers.
        while (
            outbox is not None
            and self._outboxes.get(websocket) is outbox
            and outbox.close_code is None
            and (
                len(outbox.messages) >= min(32, MAX_PENDING_MESSAGES) or outbox.pending_bytes >= MAX_PENDING_BYTES // 2
            )
        ):
            outbox.progress.clear()
            await outbox.progress.wait()

    async def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection."""
        self._remove(websocket)
        outbox = self._outboxes.get(websocket)
        if outbox and outbox.task:
            outbox.task.cancel()
            try:
                with suppress(asyncio.CancelledError):
                    await outbox.task
            finally:
                # A task cancelled before its first turn never enters the
                # writer's finally block. Disconnect must release that queue too.
                outbox.messages.clear()
                outbox.pending_bytes = 0
                outbox.progress.set()
                self._outboxes.pop(websocket, None)

    async def shutdown(self) -> None:
        """Lifespan owns the writers, including clients already evicted."""
        await asyncio.gather(*(self.disconnect(ws) for ws in list(self._outboxes)))

    async def broadcast(self, message: dict[str, Any]):
        """Broadcast a message to all connected clients.

        ⚠️ **Internal listeners fire first, and above the empty-connection
        early return.** A farm running headless overnight has no browser
        attached, and that is exactly the situation Cloud Link exists for — a
        tap placed after the return would report nothing precisely when there
        is nobody in the room to notice.
        """
        self._notify_internal_listeners(message)

        if not self.active_connections:
            return

        data = json.dumps(message)
        started = time.monotonic()
        size = len(data.encode("utf-8"))
        for connection in list(self.active_connections):
            self._enqueue(connection, data, size)

        elapsed = time.monotonic() - started
        if elapsed >= 0.25:
            logger.warning(
                "Slow WebSocket broadcast enqueue: type=%s clients=%s bytes=%s elapsed=%.3fs",
                message.get("type", "unknown"),
                len(self.active_connections),
                size,
                elapsed,
            )

    async def broadcast_to_user(self, user_id: int | None, message: dict[str, Any]):
        """Send a message only to the given user's connections.

        BamDude has no anonymous users (auth always-on), so owner-scoped events —
        e.g. a Slicer Pipeline run's dashboard refresh — can target just the owner
        instead of the whole farm. Falls back to a global broadcast when ``user_id``
        is None (e.g. an API-key-owned resource).

        ⚠️ **Internal listeners are deliberately NOT fired for the targeted
        path.** A message here is scoped to one person's browser sessions by
        design, and an agent is not a person — it has no ``user_id`` to be, so
        delivering it one would widen an audience the caller narrowed on
        purpose. The ``user_id is None`` branch below is a genuine global
        broadcast and taps normally, because that is what it is."""
        if user_id is None:
            await self.broadcast(message)
            return
        data = json.dumps(message)
        size = len(data.encode("utf-8"))
        for connection in list(self.active_connections):
            if self._user_by_conn.get(connection) == user_id:
                self._enqueue(connection, data, size)

    async def send_printer_status(self, printer_id: int, status: dict):
        """Send printer status update to all clients."""
        await self.broadcast(
            {
                "type": "printer_status",
                "printer_id": printer_id,
                "data": status,
            }
        )

    async def send_print_start(self, printer_id: int, data: dict):
        """Notify clients that a print has started."""
        await self.broadcast(
            {
                "type": "print_start",
                "printer_id": printer_id,
                "data": data,
            }
        )

    async def send_print_complete(self, printer_id: int, data: dict):
        """Notify clients that a print has completed."""
        await self.broadcast(
            {
                "type": "print_complete",
                "printer_id": printer_id,
                "data": data,
            }
        )

    async def send_print_paused(self, printer_id: int, data: dict):
        """Notify clients that a print transitioned RUNNING→PAUSE.

        ``data`` carries ``filename``, ``reason`` (human-readable),
        ``reason_code`` (normalised key — see ``hms_errors.classify_pause_reason``),
        and optional ``hms_code`` so the frontend can route by reason
        category without repeating the HMS-table lookup.
        """
        await self.broadcast(
            {
                "type": "print_paused",
                "printer_id": printer_id,
                "data": data,
            }
        )

    async def send_print_resumed(self, printer_id: int, data: dict):
        """Notify clients that a print transitioned PAUSE→RUNNING.

        ``data`` carries ``filename`` and ``paused_for_seconds`` so the UI
        can display "resumed after Nm Ms" without keeping its own pause
        timestamp.
        """
        await self.broadcast(
            {
                "type": "print_resumed",
                "printer_id": printer_id,
                "data": data,
            }
        )

    async def send_archive_created(self, archive: dict):
        """Notify clients that a new archive was created."""
        await self.broadcast(
            {
                "type": "archive_created",
                "data": archive,
            }
        )

    async def send_archive_updated(self, archive: dict):
        """Notify clients that an archive was updated."""
        await self.broadcast(
            {
                "type": "archive_updated",
                "data": archive,
            }
        )

    async def send_library_file_added(self, file_data: dict):
        """Notify clients that a file was added to the library."""
        await self.broadcast({"type": "library_file_added", "data": file_data})

    async def send_library_scan_progress(self, data: dict):
        """How far an external-folder scan has got.

        ⚠️ Throttled by the caller, not here. Per-file progress on a
        five-thousand-file share is five thousand messages to every open tab —
        the per-file ``library_file_added`` earns its noise because it carries a
        row somebody wants to see appear, but a percentage does not.
        """
        await self.broadcast({"type": "library_scan_progress", "data": data})

    async def send_library_scan_finished(self, data: dict):
        """A scan ended — with its counters, and whether it refused to delete."""
        await self.broadcast({"type": "library_scan_finished", "data": data})

    async def send_library_file_notes_changed(self, file_id: int, notes_count: int):
        """Notify clients that a library file's notes changed (gh#3).

        Carries the new total count so the file-card icon switches between
        MessageSquarePlus / MessageSquare without an extra fetch. Frontend
        also invalidates the per-file notes query so any open popover refreshes.
        """
        await self.broadcast(
            {
                "type": "library_file_notes_changed",
                "data": {"file_id": file_id, "notes_count": notes_count},
            }
        )

    async def send_missing_spool_assignment(
        self,
        printer_id: int,
        printer_name: str,
        missing_slots: list[dict[str, str]],
    ):
        """Notify clients that a print started with missing spool assignments."""
        await self.broadcast(
            {
                "type": "missing_spool_assignment",
                "printer_id": printer_id,
                "printer_name": printer_name,
                "missing_slots": missing_slots,
            }
        )

    async def send_filament_deficit(
        self,
        printer_id: int,
        printer_name: str,
        print_name: str,
        shortfalls: list[dict],
    ):
        """Tell open tabs a print started that will exhaust a slot.

        Informative only — the print is already running. It rides the socket
        rather than waiting for the notification channel because the operator
        standing at the farm screen is exactly who can walk over and put a new
        spool on before it matters.
        """
        await self.broadcast(
            {
                "type": "filament_deficit",
                "printer_id": printer_id,
                "printer_name": printer_name,
                "print_name": print_name,
                "shortfalls": shortfalls,
            }
        )


# Global connection manager
ws_manager = ConnectionManager()
