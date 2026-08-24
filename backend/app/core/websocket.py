import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


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
        self._lock = asyncio.Lock()

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
        async with self._lock:
            self.active_connections.append(websocket)
            self._user_by_conn[websocket] = user_id

    async def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection."""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
            self._user_by_conn.pop(websocket, None)

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
        async with self._lock:
            disconnected = []
            for connection in self.active_connections:
                try:
                    await connection.send_text(data)
                except Exception:
                    disconnected.append(connection)

            # Clean up disconnected clients
            for conn in disconnected:
                if conn in self.active_connections:
                    self.active_connections.remove(conn)
                self._user_by_conn.pop(conn, None)

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
        async with self._lock:
            disconnected = []
            for connection in self.active_connections:
                if self._user_by_conn.get(connection) != user_id:
                    continue
                try:
                    await connection.send_text(data)
                except Exception:
                    disconnected.append(connection)
            for conn in disconnected:
                if conn in self.active_connections:
                    self.active_connections.remove(conn)
                self._user_by_conn.pop(conn, None)

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


# Global connection manager
ws_manager = ConnectionManager()
