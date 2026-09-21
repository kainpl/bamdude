"""Persist the owner of a plate-clear hold and its one completion receipt."""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column, table_exists

version = 182
name = "completion_receipts"


async def upgrade(conn):
    await add_column(conn, "printers", "awaiting_plate_clear_archive_id INTEGER")
    await add_column(conn, "printers", "awaiting_plate_clear_token VARCHAR(64)")
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_printers_awaiting_plate_clear_archive_id "
            "ON printers (awaiting_plate_clear_archive_id)"
        )
    )
    if await table_exists(conn, "print_completion_receipts"):
        return
    await conn.execute(
        text(
            "CREATE TABLE print_completion_receipts ("
            "id " + ("SERIAL PRIMARY KEY" if conn.dialect.name == "postgresql" else "INTEGER PRIMARY KEY") + ", "
            "archive_id INTEGER NOT NULL UNIQUE, "
            "assessment " + ("JSON" if conn.dialect.name == "postgresql" else "TEXT") + ", "
            "assessment_at TIMESTAMP, assessment_actor_id INTEGER, "
            "plate_action VARCHAR(16), plate_action_at TIMESTAMP, plate_action_actor_id INTEGER, "
            "gate_token VARCHAR(64), rearmed_queue_item_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "FOREIGN KEY(archive_id) REFERENCES print_archives(id) ON DELETE CASCADE, "
            "FOREIGN KEY(assessment_actor_id) REFERENCES users(id) ON DELETE SET NULL, "
            "FOREIGN KEY(plate_action_actor_id) REFERENCES users(id) ON DELETE SET NULL"
            ")"
        )
    )
    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_print_completion_receipts_archive_id ON print_completion_receipts (archive_id)"
        )
    )
