"""Serialize a sensor bind with deletion of its target on both databases."""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres

_TABLES = {"printers", "printer_locations", "locations"}


async def lock_sensor_target(db, table: str, target_id: int) -> bool:
    if table not in _TABLES:
        raise ValueError("Unknown sensor target")
    if is_postgres():
        # FOR NO KEY UPDATE (G03 B, L6; found by TA26 on PostgreSQL): it still serialises a bind with the target's
        # deletion (the two conflict), but not with the foreign-key check of a row that merely names the target --
        # a lane writer that updates its queue header twice in one transaction makes PostgreSQL check
        # ``printer_queues.printer_id`` again (KEY SHARE on the printer). Under FOR UPDATE that check waited for a
        # printer deletion that waited for the writer's lane: 40P01. The deletion's own DELETE takes the full lock
        # later, under the lanes.
        statement = text(f"SELECT id FROM {table} WHERE id = :id FOR NO KEY UPDATE")
    else:
        # SQLite has no row locks. A no-op write obtains the writer lock without
        # touching updated_at; the existence check is then serialized with delete.
        statement = text(f"UPDATE {table} SET id = id WHERE id = :id RETURNING id")
    return (await db.execute(statement, {"id": target_id})).scalar_one_or_none() is not None
