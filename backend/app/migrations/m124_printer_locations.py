"""A place on the farm becomes an entity instead of three free-text columns.

``printers.location``, ``smart_sensors.location`` and
``auto_queue_items.target_location`` were independent strings. Routing compared
them exactly, so "Цех 2" and "цех 2" were two places, and an item aimed at a
mistyped one routed nowhere — silently and for ever, because from the
dispatcher's point of view "no printer matches" is a legitimate state.

**The seed is the part that has to be right.** A ``target_location`` may name a
place no printer has, which is precisely what a typo leaves behind. Such an item
matches nothing today. If this migration left it NULL it would begin matching
EVERYTHING — queued work quietly going somewhere nobody chose, on a live farm.
So a row is created for every distinct value found anywhere, orphans included,
and the matching semantics come out unchanged for everyone.

Where two spellings differ only in case or surrounding space, the first in a
deterministic order keeps its capitalisation: printers, then sensors, then queue
items, alphabetically within each. The other becomes the same row. One of them
therefore appears to change case — visible, and fixable by renaming, unlike the
two silently separate places it replaces.

The old columns are dropped with ``drop_column`` rather than ``recreate_table``:
the latter needs the full ``CREATE TABLE`` of the table it rewrites, and
hand-writing that for ``printers`` is how NOT NULL, DEFAULT and FK get lost with
no way to notice or repair it.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column, drop_column, get_table_columns, table_exists

logger = logging.getLogger(__name__)

version = 124
name = "printer_locations"

# (table, string column, fk column) in the order that decides which spelling is
# kept when two differ only in case.
_SOURCES = (
    ("printers", "location", "location_id"),
    ("smart_sensors", "location", "location_id"),
    ("auto_queue_items", "target_location", "target_location_id"),
)


def _key(value: str | None) -> str:
    return (value or "").strip().lower()


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"

    if not await table_exists(conn, "printer_locations"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE printer_locations (
                    id {pk},
                    name VARCHAR(100) NOT NULL UNIQUE,
                    name_key VARCHAR(100) NOT NULL UNIQUE,
                    created_at {ts} DEFAULT CURRENT_TIMESTAMP,
                    updated_at {ts} DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        logger.info("m124: created printer_locations")

    await conn.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS ix_printer_locations_name_key ON printer_locations (name_key)")
    )

    for table, _column, fk in _SOURCES:
        if await table_exists(conn, table):
            await add_column(conn, table, f"{fk} INTEGER REFERENCES printer_locations(id)")

    known = {key: row_id for row_id, key in await conn.execute(text("SELECT id, name_key FROM printer_locations"))}

    # --- seed -------------------------------------------------------------
    for table, column, _fk in _SOURCES:
        if not await table_exists(conn, table) or column not in await get_table_columns(conn, table):
            continue
        rows = await conn.execute(
            text(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL ORDER BY {column}")
        )
        for (raw,) in rows.all():
            place = (raw or "").strip()
            key = _key(place)
            if not key or key in known:
                continue
            await conn.execute(
                text("INSERT INTO printer_locations (name, name_key) VALUES (:n, :k)"),
                {"n": place, "k": key},
            )
            known[key] = (
                await conn.execute(text("SELECT id FROM printer_locations WHERE name_key = :k"), {"k": key})
            ).scalar_one()

    # --- backfill ---------------------------------------------------------
    for table, column, fk in _SOURCES:
        if not await table_exists(conn, table) or column not in await get_table_columns(conn, table):
            continue
        rows = (await conn.execute(text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"))).all()
        for row_id, raw in rows:
            key = _key(raw)
            if not key:
                continue
            await conn.execute(
                text(f"UPDATE {table} SET {fk} = :loc WHERE id = :id"),
                {"loc": known[key], "id": row_id},
            )

    # --- drop the strings -------------------------------------------------
    for table, column, _fk in _SOURCES:
        if await table_exists(conn, table):
            await drop_column(conn, table, column)

    logger.info("m124: %d location(s) recorded", len(known))
