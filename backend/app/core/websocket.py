import asyncio
import json
from typing import Any

from fastapi import WebSocket


class ConnectionManager:
    """Manages WebSocket connections and broadcasts."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        # Per-connection user id (None for API-key callers) — enables per-user
        # broadcasts. BamDude has no anonymous users, so this is almost always set.
        self._user_by_conn: dict[WebSocket, int | None] = {}
        self._lock = asyncio.Lock()

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
        """Broadcast a message to all connected clients."""
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
        is None (e.g. an API-key-owned resource)."""
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

    async def send_pipeline_run_updated(self, run: dict):
        """Notify the run's owner that a Slicer Pipeline run changed state (#1425).

        ``run`` is a full ``PipelineRunResponse.model_dump(mode="json")``. The
        frontend keys off ``run.pipeline_id`` to refresh both the dashboard and
        the per-pipeline "Last run" chip. Routed to the run's ``created_by`` so only
        the owner's dashboard refreshes (falls back to a global broadcast when the
        owner is unknown — an API-key-created run)."""
        await self.broadcast_to_user(
            run.get("created_by"),
            {
                "type": "pipeline_run_updated",
                "run": run,
            },
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
