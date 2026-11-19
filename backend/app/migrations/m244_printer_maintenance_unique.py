"""One maintenance item per printer and type (upstream 175758d25).

``printer_maintenance`` never had a constraint on ``(printer_id, maintenance_type_id)``, and its rows
are created on first read: two reads of a new printer (two tabs, the page and the check after a
finished print) or a double click on "assign" each inserted one. A duplicate is not harmless -- the
page shows whichever copy it read last, so a task marked done comes back overdue when the other copy
is read, its history is split between the copies, and "assign" answered 500.

Duplicates are merged first, because the index cannot be built over them. Per pair, ONE row stays,
whole, with its own settings (custom interval, custom interval type, enabled) -- nothing is blended
from the others:

* the row performed last (the latest ``last_performed_at``; on a tie the lowest id);
* when none was ever performed, the lowest id.

The history of the discarded rows is moved onto the kept row by an explicit UPDATE before they are
deleted: SQLite runs no FK actions, and on PostgreSQL the CASCADE would otherwise delete that history.
Then the unique index, under the name upstream uses and the model declares, so a fresh install
(``create_all``), an upgraded one and a database that already carries it end up identical.

The duplicated pairs are found with GROUP BY / HAVING and the survivor is chosen in Python, so the
same statements run on SQLite and PostgreSQL. Re-running it is harmless: no duplicates, and the
index already exists.
"""

import logging

from sqlalchemy import text

from backend.app.migrations.helpers import table_exists

version = 244
name = "printer_maintenance_unique"

logger = logging.getLogger(__name__)

_INDEX = "uq_printer_maintenance_printer_type"


def _survivor(group):
    """The row of one (printer, type) pair that stays: performed last, else the lowest id."""
    performed = [row for row in group if row.last_performed_at is not None]
    if not performed:
        return min(group, key=lambda row: row.id)
    # One column of one database: every value has the same type and format, so text order is time order
    # (SQLite hands back the stored string, PostgreSQL a datetime).
    latest = max(str(row.last_performed_at) for row in performed)
    return min((row for row in performed if str(row.last_performed_at) == latest), key=lambda row: row.id)


async def _merge_duplicates(conn) -> None:
    pairs = (
        await conn.execute(
            text(
                "SELECT printer_id, maintenance_type_id FROM printer_maintenance "
                "GROUP BY printer_id, maintenance_type_id HAVING COUNT(*) > 1"
            )
        )
    ).all()
    history = await table_exists(conn, "maintenance_history")
    removed = 0
    for pair in pairs:
        group = (
            await conn.execute(
                text(
                    "SELECT id, last_performed_at FROM printer_maintenance "
                    "WHERE printer_id = :printer_id AND maintenance_type_id = :type_id ORDER BY id"
                ),
                {"printer_id": pair.printer_id, "type_id": pair.maintenance_type_id},
            )
        ).all()
        keep = _survivor(group)
        for row in group:
            if row.id == keep.id:
                continue
            if history:
                await conn.execute(
                    text(
                        "UPDATE maintenance_history SET printer_maintenance_id = :keep "
                        "WHERE printer_maintenance_id = :gone"
                    ),
                    {"keep": keep.id, "gone": row.id},
                )
            await conn.execute(text("DELETE FROM printer_maintenance WHERE id = :gone"), {"gone": row.id})
            removed += 1
    if removed:
        logger.info(
            "m244: merged %d duplicate maintenance item(s) of %d printer/type pair(s); their history was kept",
            removed,
            len(pairs),
        )


async def upgrade(conn):
    if not await table_exists(conn, "printer_maintenance"):
        return
    await _merge_duplicates(conn)
    await conn.execute(
        text(f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX} ON printer_maintenance (printer_id, maintenance_type_id)")
    )
