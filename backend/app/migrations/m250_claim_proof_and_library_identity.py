"""The deferral proof of a queue row, and a library row's own identity (G03 B, §A).

``print_queue.deferred_claim_token`` holds the start token of the attempt a no-effect deferral sent back to
pending. It is the only thing that tells a Stop which waited for the printer's lane that the pending row it
finds is the very attempt it read: SQLite gives a deleted job's id to the next job, and a second claim and
deferral leave a row that looks the same in every other column. ``defer_claim`` is its one writer; the next
claim and every other status write void it. NULL proves nothing, which is the right answer for every row
before this upgrade, so there is no backfill and no index (it is read by id).

``library_files.recovery_identity`` is a random identity of the ROW, as ``print_archives`` and
``print_queue`` have had since m204. On SQLite a library row's id is a plain ``INTEGER PRIMARY KEY`` and its
``created_at`` a one-second ``CURRENT_TIMESTAMP``, so the row inserted after the newest one is deleted can carry
both; a stored proof of id + path + hash + creation time then matches a different row (a file on a share
registered again in the same second). The model default writes it at every insert; it is never assigned
afterwards. Existing rows get one each, in batches, then a unique index.

Both columns are named here with plain SQL; no model is imported.
"""

from uuid import uuid4

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 250
name = "claim_proof_and_library_identity"


async def upgrade(conn):
    await add_column(conn, "print_queue", "deferred_claim_token DATETIME")

    await add_column(conn, "library_files", "recovery_identity VARCHAR(36)")
    while True:
        ids = (
            (await conn.execute(text("SELECT id FROM library_files WHERE recovery_identity IS NULL LIMIT 500")))
            .scalars()
            .all()
        )
        if not ids:
            break
        await conn.execute(
            text("UPDATE library_files SET recovery_identity=:identity WHERE id=:id"),
            [{"id": row_id, "identity": str(uuid4())} for row_id in ids],
        )
    await conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_library_files_recovery_identity ON library_files (recovery_identity)"
    )
