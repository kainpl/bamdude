"""m163: hidden HMS stack entries — one row per (printer, full 16-char code).

Backs ``services/hms_mute``: the operator can hide one ``hms[]`` entry on one
printer until the printer itself stops reporting it. The firmware owns the
stack — ``clean_print_error`` empties only the ``print_error`` register — so an
entry the printer keeps re-sending (a P2S code Bambu ships with no text, seen
in a user's log 2026-09-04) could not be answered at all. Persisted, because
the point is not being asked the same question again after every restart; the
row is deleted by the manager the moment the entry leaves the stack.

No ``seed()``: the table starts empty by definition — a mute is an operator's
decision about one incident, never something to infer.

DDL is written dialect-neutral (m162 is the pattern). The table is in
``Base.metadata``, so ``create_all`` builds it on a fresh install before this
migration is reached and the guard below finds it and does nothing; the CREATE
is the path an EXISTING database walks, and on PostgreSQL that path must parse —
see ``tests/unit/test_migration_ddl_dialects.py``. Names match the model
(``uq_hms_muted_printer_code``, ``ix_hms_muted_entries_printer_id``) so the two
paths describe the same table.

The feature shipped without this file for one commit on the branch it was
written on — deliberately, to keep clear of the m157–m162 block a parallel
branch was carrying and of the ``DEBUG=true`` re-run of the highest number.
Both branches are merged now, so the table gets the number it would have had.
"""

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import table_exists

version = 163
name = "hms_muted_entries"


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME DEFAULT CURRENT_TIMESTAMP" if sqlite else "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    if not await table_exists(conn, "hms_muted_entries"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE hms_muted_entries (
                id {pk},
                printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                full_code VARCHAR(16) NOT NULL,
                created_at {ts} NOT NULL,
                CONSTRAINT uq_hms_muted_printer_code UNIQUE (printer_id, full_code)
            )
            """
        )
    # Outside the guard and IF NOT EXISTS on both dialects: a fresh install has
    # it from ``create_all`` (``index=True`` on the model column), a database
    # that somehow has the table without it still ends up with it. It is the
    # one read the manager does — every mute of a printer, on connect.
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_hms_muted_entries_printer_id ON hms_muted_entries (printer_id)"
    )
