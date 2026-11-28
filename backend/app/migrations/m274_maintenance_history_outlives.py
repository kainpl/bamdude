"""The maintenance history outlives its printer, task and performer (N09, upstream 8afcb3fbd).

``maintenance_history.printer_maintenance_id`` was NOT NULL with ``ON DELETE CASCADE``,
and three doors removed the history with its item: a printer's deletion (an explicit
DELETE — SQLite keeps foreign keys off), unassigning a task and deleting a custom type
(ORM cascades). Upstream keeps a separate log with the names as they were; BamDude keeps
its one history (owner, 2026-11-28) and gives it the same shape:

* ``printer_maintenance_id`` becomes nullable, ``ON DELETE SET NULL``;
* ``printer_id`` (``printers.id``, ``SET NULL``, indexed) — the printer the reset was for;
* ``printer_name``, ``maintenance_type_name`` (NOT NULL, ``''``) and ``performed_by_name``
  (nullable) — the names at the reset, which every reader shows from now on.

The doors detach the history before they delete (``services/content_delete_rows``); no
FK action or ORM cascade stands in for them.

**The fill writes only what is still empty** (spec r2, R1-01): each ``SET`` touches rows
whose target is ``NULL`` / ``''``, so a second run — DEBUG's ``replay``, a rerun of the
chain — changes nothing, and a printer or task renamed after a reset keeps its old name in
the history.

**A performer is named only if it existed at the reset** (R1-02): ``users`` is not an
AUTOINCREMENT table and SQLite keeps foreign keys off, so a deleted user's id can stay in
the history and be given to a new user. A user's name is taken only where its
``created_at <= performed_at``, else the chat's label under the same rule, else NULL —
the order the History tab used (user first, then chat).

SQLite: the table is rebuilt with the model's DDL (``recreate_table``, indexes carried);
a table already in the new form (a fresh install built from the model) skips the rebuild.
PostgreSQL: ``DROP NOT NULL``, the FK swapped to ``SET NULL`` under the name the database
has (historical databases differ), the columns added. No FK found — nothing to drop: after
an SQLite→PostgreSQL import the chain runs before ``conform_imported_schema``, which adds
the model's FK.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column, recreate_table, table_exists

version = 274
name = "maintenance_history_outlives"

_TABLE = "maintenance_history"

_SQLITE_DDL = (
    f"CREATE TABLE {_TABLE} ("
    "id INTEGER NOT NULL PRIMARY KEY, "
    "printer_maintenance_id INTEGER REFERENCES printer_maintenance(id) ON DELETE SET NULL, "
    "printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL, "
    "printer_name VARCHAR(255) NOT NULL DEFAULT '', "
    "maintenance_type_name VARCHAR(100) NOT NULL DEFAULT '', "
    "performed_by_name VARCHAR(100), "
    "performed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
    "hours_at_maintenance FLOAT NOT NULL, "
    "notes TEXT, "
    "performed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, "
    "performed_by_chat_id INTEGER REFERENCES telegram_chats(id) ON DELETE SET NULL"
    ")"
)
# Every column the new form has: recreate_table copies only those the source has.
_COLUMNS = (
    "id, printer_maintenance_id, printer_id, printer_name, maintenance_type_name, performed_by_name, "
    "performed_at, hours_at_maintenance, notes, performed_by_user_id, performed_by_chat_id"
)
_INDEX = f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_printer_id ON {_TABLE} (printer_id)"

# Each statement writes only where its target is still empty (R1-01).
_FILL = (
    # The printer, from the task the row belongs to, while both are there.
    f"UPDATE {_TABLE} SET printer_id = ("
    "SELECT pm.printer_id FROM printer_maintenance pm JOIN printers p ON p.id = pm.printer_id "
    f"WHERE pm.id = {_TABLE}.printer_maintenance_id"
    ") WHERE printer_id IS NULL AND printer_maintenance_id IS NOT NULL",
    f"UPDATE {_TABLE} SET printer_name = COALESCE(("
    f"SELECT p.name FROM printers p WHERE p.id = {_TABLE}.printer_id"
    "), '') WHERE printer_name = '' AND printer_id IS NOT NULL",
    f"UPDATE {_TABLE} SET maintenance_type_name = COALESCE(("
    "SELECT mt.name FROM printer_maintenance pm JOIN maintenance_types mt ON mt.id = pm.maintenance_type_id "
    f"WHERE pm.id = {_TABLE}.printer_maintenance_id"
    "), '') WHERE maintenance_type_name = '' AND printer_maintenance_id IS NOT NULL",
    # R1-02: only a performer that existed at the reset; the user first, then the chat.
    f"UPDATE {_TABLE} SET performed_by_name = COALESCE(("
    f"SELECT u.username FROM users u WHERE u.id = {_TABLE}.performed_by_user_id "
    f"AND u.created_at <= {_TABLE}.performed_at"
    "), ("
    f"SELECT c.label FROM telegram_chats c WHERE c.id = {_TABLE}.performed_by_chat_id "
    f"AND c.created_at <= {_TABLE}.performed_at"
    ")) WHERE performed_by_name IS NULL "
    "AND (performed_by_user_id IS NOT NULL OR performed_by_chat_id IS NOT NULL)",
)


async def _fill(conn) -> None:
    for statement in _FILL:
        await conn.execute(text(statement))


async def _postgres(conn) -> None:
    await conn.execute(text(f"ALTER TABLE {_TABLE} ALTER COLUMN printer_maintenance_id DROP NOT NULL"))
    rows = (
        await conn.execute(
            text(
                "SELECT con.conname, con.confdeltype::text FROM pg_constraint con "
                "JOIN pg_class rel ON rel.oid = con.conrelid "
                "JOIN pg_namespace ns ON ns.oid = rel.relnamespace "
                "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey) "
                "WHERE con.contype = 'f' AND rel.relname = :t AND ns.nspname = current_schema() "
                "AND att.attname = 'printer_maintenance_id'"
            ),
            {"t": _TABLE},
        )
    ).fetchall()
    for constraint, delete_rule in rows:
        if delete_rule == "n":
            continue  # already SET NULL
        await conn.execute(text(f'ALTER TABLE {_TABLE} DROP CONSTRAINT "{constraint}"'))
        await conn.execute(
            text(
                f'ALTER TABLE {_TABLE} ADD CONSTRAINT "{constraint}" FOREIGN KEY (printer_maintenance_id) '
                "REFERENCES printer_maintenance(id) ON DELETE SET NULL"
            )
        )
    await add_column(conn, _TABLE, "printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL")
    await add_column(conn, _TABLE, "printer_name VARCHAR(255) NOT NULL DEFAULT ''")
    await add_column(conn, _TABLE, "maintenance_type_name VARCHAR(100) NOT NULL DEFAULT ''")
    await add_column(conn, _TABLE, "performed_by_name VARCHAR(100)")


async def _sqlite(conn) -> None:
    row = (
        await conn.execute(text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :t"), {"t": _TABLE})
    ).fetchone()
    sql = (row[0] if row else "") or ""
    info = (await conn.execute(text(f"PRAGMA table_info({_TABLE})"))).fetchall()
    columns = {r[1] for r in info}
    link_not_null = any(r[1] == "printer_maintenance_id" and r[3] for r in info)
    link_rule = {
        r[3]: (r[6] or "").upper() for r in (await conn.execute(text(f"PRAGMA foreign_key_list({_TABLE})"))).fetchall()
    }.get("printer_maintenance_id")
    if "printer_id" in columns and not link_not_null and link_rule == "SET NULL":
        return  # already in the new form
    ddl = _SQLITE_DDL
    if "AUTOINCREMENT" in sql.upper():
        ddl = ddl.replace("id INTEGER NOT NULL PRIMARY KEY", "id INTEGER PRIMARY KEY AUTOINCREMENT")
    original = (await conn.execute(text(f"SELECT count(*) FROM {_TABLE}"))).scalar_one()
    await recreate_table(conn, _TABLE, ddl, _COLUMNS)
    copied = (await conn.execute(text(f"SELECT count(*) FROM {_TABLE}"))).scalar_one()
    if copied != original:
        raise RuntimeError(f"{_TABLE} rebuild: {original} rows before, {copied} after")


async def upgrade(conn):
    if not await table_exists(conn, _TABLE):
        return
    if is_postgres():
        await _postgres(conn)
    else:
        await _sqlite(conn)
    await conn.execute(text(_INDEX))
    await _fill(conn)


async def replay(conn):
    """Repeat-safe for DEBUG: the form checks skip the DDL, and the fill writes only empties."""
    await upgrade(conn)
