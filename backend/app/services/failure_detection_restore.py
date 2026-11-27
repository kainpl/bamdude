"""A full ZIP restore never starts sending frames to OctoEverywhere's cloud on its own.

The restored database may come from another farm or another time. If it had OctoEverywhere on,
``octoeverywhere_enabled`` becomes ``false`` in the copy that replaces the database -- before the
first start that could inspect a print -- and a private marker names this restore. The first start
of the restored database tells every active administrator in its inbox, in the one transaction that
consumes the marker: a failure leaves the marker for the next start, a start after it finds nothing
to do, and another restore writes a marker of its own. The key, the list and the other
OctoEverywhere settings stay; Obico and everything else restore as they are. Only the ZIP restore
route calls ``turn_off_in_prepared_copy``: the SQLite -> PostgreSQL move keeps the setting as it is.
"""

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select, update

from backend.app.core.schema_repair_state import PREFIX

logger = logging.getLogger(__name__)

MARKER = PREFIX + "octoeverywhere_restore_off"
EVENT = "octoeverywhere_restore_off"
TEMPLATE_WAIT_SECONDS = 120.0


def turn_off_in_prepared_copy(path: Path) -> str | None:
    """In the prepared SQLite copy: OctoEverywhere on -> off, plus a marker. Returns the marker's id.

    A file in a worker thread (``_file_work``), before the copy replaces the database or is loaded
    into PostgreSQL in the restore's one transaction. Read as the settings reader reads a bool:
    ``"true"`` in any case is on.
    """
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'octoeverywhere_enabled'").fetchone()
        if row is None or (row[0] or "").strip().lower() != "true":
            return None
        operation = uuid4().hex
        with conn:
            conn.execute("UPDATE settings SET value = 'false' WHERE key = 'octoeverywhere_enabled'")
            conn.execute("DELETE FROM settings WHERE key = ?", (MARKER,))
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                (MARKER, json.dumps({"operation": operation}, separators=(",", ":"))),
            )
        logger.info("Restore: OctoEverywhere is off in the restored database until an administrator turns it on")
        return operation
    finally:
        conn.close()


async def deliver_restore_notice(session_factory=None, *, wait: float = TEMPLATE_WAIT_SECONDS) -> bool:
    """First start of a restored database: the inbox notice and the marker's end, in one transaction.

    Returns whether a notice was delivered. Every active administrator gets it, whatever their
    subscriptions; no network provider is involved.
    """
    from backend.app.models.settings import Settings
    from backend.app.services.notification_inbox import dispatch_staged, stage_admin_event
    from backend.app.services.notification_service import InternalTemplateNotReady, render_internal_event

    if session_factory is None:
        from backend.app.core.database import async_session as session_factory

    async with session_factory() as db:
        if await db.scalar(select(Settings.value).where(Settings.key == MARKER)) is None:
            return False

    # The shipped template reaches the database with the startup locale reconcile.
    deadline = time.monotonic() + wait
    while True:
        async with session_factory() as db:
            try:
                await render_internal_event(db, EVENT, {})
                break
            except InternalTemplateNotReady:
                pass
        if time.monotonic() >= deadline:
            logger.warning("OctoEverywhere restore notice postponed: the notification template is not ready")
            return False
        await asyncio.sleep(0.5)

    async with session_factory() as db:
        if db.get_bind().dialect.name == "sqlite":
            # Take SQLite's write reservation before reading the marker, as the other episodes do.
            await db.execute(update(Settings).where(Settings.key == MARKER).values(value=Settings.value))
        raw = await db.scalar(select(Settings.value).where(Settings.key == MARKER).with_for_update())
        if raw is None:
            return False
        try:
            operation = str(json.loads(raw)["operation"])
        except (ValueError, KeyError, TypeError):
            operation = "unknown"
        title, message = await render_internal_event(db, EVENT, {})
        batch = await stage_admin_event(
            db,
            event_type=EVENT,
            episode_id=operation,
            title=title,
            message=message,
            extra_data={"url": "/settings?tab=failure-detection"},
            mandatory=True,
        )
        await db.execute(delete(Settings).where(Settings.key == MARKER))
        await db.commit()
    await dispatch_staged(batch)
    return True
