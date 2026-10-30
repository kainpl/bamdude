"""Cancellation shares dispatch/transfer lanes before authoritative row reads."""

from contextlib import asynccontextmanager

from fastapi import HTTPException
from sqlalchemy import or_, select

from backend.app.core import database
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.queue_ops import claim_lanes


async def _selection(db, ids, auto):
    model = AutoQueueItem if auto else PrintQueueItem
    rows = (await db.scalars(select(model).where(model.id.in_(ids)).execution_options(populate_existing=True))).all()
    identities = {row.id: row.swap_declaration_identity for row in rows}
    if auto:
        linked = [row.assigned_to_item_id for row in rows if row.assigned_to_item_id is not None]
        lanes = set(
            (
                await db.scalars(
                    select(PrintQueueItem.queue_id).where(
                        or_(PrintQueueItem.id.in_(linked), PrintQueueItem.source_auto_item_id.in_(ids))
                    )
                )
            ).all()
        )
    else:
        lanes = {row.queue_id for row in rows}
    return identities, lanes


@asynccontextmanager
async def cancellation_scope(ids, *, auto=False):
    # No writer is held while discovering lanes. A new clean session acquires
    # every lane before SQLite's writer, matching transfer and dispatch.
    async with database.async_session() as preflight:
        identities, lanes = await _selection(preflight, ids, auto)
    async with database.async_session() as db, claim_lanes(db, sorted(lanes)):
        current, current_lanes = await _selection(db, ids, auto)
        if not current_lanes <= lanes or any(
            current.get(key) != value for key, value in identities.items() if key in current
        ):
            raise HTTPException(409, {"code": "queue_selection_changed"})
        yield db
