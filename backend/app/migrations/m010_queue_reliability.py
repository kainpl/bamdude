"""Queue reliability schema: subtask_id on archives, awaiting_plate_clear on printers.

Two column additions backing the v0.2.3 upstream sync:

    * ``print_archives.subtask_id VARCHAR(64) NULL`` — printer-assigned
      subtask identifier observed in MQTT push_status. Used as an
      additional pre-check in ``on_print_start`` to resume the same
      archive row across backend restarts when a long-running print is
      interrupted (#972). Secondary to our existing name + content_hash
      matching — advisory only.
    * ``printers.awaiting_plate_clear BOOLEAN NOT NULL DEFAULT 0`` —
      persisted form of the previously in-memory ``_plate_cleared`` set
      on ``PrinterManager``. When the plate-clear gate is armed and the
      printer power-cycles (Auto Off), the flag used to be lost and the
      queue would auto-dispatch onto a dirty plate (#961). Persisting
      it in DB + rehydrating on startup closes that window.

Index: ``subtask_id`` is queried once per print_start — unique per
invocation, rarely collides, a plain index is fine.

Both columns default to SQL-level NULL / 0 so existing rows need no
backfill — old archives simply stay without a subtask_id, and printers
with no pending plate-clear start at False, which matches the "nothing
awaiting" semantic we want post-upgrade.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 10
name = "queue_reliability"


async def upgrade(conn):
    await add_column(conn, "print_archives", "subtask_id VARCHAR(64)")
    # Index speeds up the subtask_id pre-check in on_print_start. Name kept
    # short so sqlite and postgres agree on identifier length limits.
    already_indexed = await conn.execute(
        text(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ix_print_archives_subtask_id'"
            if not _is_pg(conn)
            else "SELECT 1 FROM pg_indexes WHERE indexname='ix_print_archives_subtask_id'"
        )
    )
    if not already_indexed.scalar():
        await conn.execute(text("CREATE INDEX ix_print_archives_subtask_id ON print_archives(subtask_id)"))

    await add_column(
        conn,
        "printers",
        "awaiting_plate_clear BOOLEAN NOT NULL DEFAULT 0",
    )


def _is_pg(conn) -> bool:
    # Lazy import to avoid circular init during migration bootstrap.
    from backend.app.core.db_dialect import is_postgres

    return is_postgres()


async def seed(session_factory):  # pragma: no cover — no-op, columns are self-defaulting
    async with session_factory() as db:
        # Nothing to seed — existing archives keep subtask_id=NULL, existing printers keep
        # awaiting_plate_clear=FALSE. Rehydration of awaiting flags is a runtime concern
        # (PrinterManager.load_awaiting_plate_clear_from_db in 6A port), not a migration one.
        _ = db  # noqa: ARG001
