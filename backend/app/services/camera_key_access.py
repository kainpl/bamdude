"""Interim connector API-key stream freshness (OQ2), before G01 D's shared coordinator.

One viewer checks current authority in a short owned read, never per frame and never by hashing
its raw credential again. Delivery stops on uncertainty; pending SQL remains restore-owned.
"""

from __future__ import annotations

import asyncio
import time
import uuid

from sqlalchemy import select

from backend.app.core import database
from backend.app.core.auth import CameraKeyIdentity
from backend.app.core.auth_lifetime import validate_captured
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.services.connector_runtime import connector_runtime
from backend.app.services.connector_submissions import read_server_identity

RECHECK_SECONDS = 2.0
CHECK_TIMEOUT_SECONDS = 1.0


class CameraKeyStreamGuard:
    def __init__(self, identity: CameraKeyIdentity | None, printer: Printer, namespace: tuple):
        self.identity = identity
        self.printer_id = printer.id
        self.printer_identity = printer.connector_identity
        self.namespace = namespace
        self._fresh_until = 0.0
        self._closed = False

    async def _allowed(self) -> bool:
        identity = self.identity
        if identity is None or identity.capture is None:
            return False
        try:
            incarnations = (*self.namespace, self.printer_identity, identity.identity)
            if len(self.namespace) != 2 or any(str(uuid.UUID(value)) != value for value in incarnations):
                return False
        except (ValueError, TypeError, AttributeError):
            return False
        async with database.async_session() as db:
            principal = await validate_captured(
                db, identity.capture, required_permissions=(Permission.CAMERA_VIEW.value,), printer_id=self.printer_id
            )
            key = principal.api_key
            if (
                key is None
                or not key.enabled
                or key.connector_identity != identity.identity
                or key.key_hash != identity.key_hash
                or key.user_id != identity.owner_id
            ):
                return False
            if key.printer_ids is not None and self.printer_id not in key.printer_ids:
                return False
            printer_identity = await db.scalar(select(Printer.connector_identity).where(Printer.id == self.printer_id))
            return printer_identity == self.printer_identity and await read_server_identity(db) == self.namespace

    async def closed(self) -> bool:
        if self._closed or not connector_runtime.admitting:
            self._closed = True
            return True
        if time.monotonic() < self._fresh_until:
            return False
        started = time.monotonic()
        try:
            # own_request keeps actual SQL/cleanup in the existing restore drain even after this deadline.
            async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
                allowed = await connector_runtime.own_request(self._allowed, new_work=False)
        except asyncio.CancelledError:
            self._closed = True
            raise
        except Exception:
            # EOF means reconnect/retry, not a claim that an uncertain credential is invalid. No raw error logging.
            allowed = False
        self._closed = not allowed or not connector_runtime.admitting or time.monotonic() >= started + RECHECK_SECONDS
        self._fresh_until = started + RECHECK_SECONDS
        return self._closed
