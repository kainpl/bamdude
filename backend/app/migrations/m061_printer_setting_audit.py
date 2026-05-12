"""Audit trail for the Printer Settings dialog (Print Options + Parts tabs).

Why this exists:
    Same rationale as ams_setting_audit (m060): BS has no RBAC, BamDude
    gates the dialog behind PRINTERS_UPDATE and records every applied
    change so operators can answer "who flipped auto-recovery last
    Thursday?" without diffing MQTT logs.

What this migration does:
    Creates printer_setting_audit with (printer_id, user_id, tab, action,
    payload_json, sequence_id, result, error_message, created_at) and a
    descending index on (printer_id, created_at DESC).

Idempotent: CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import table_exists

version = 61
name = "printer_setting_audit"


async def upgrade(conn):
    if not await table_exists(conn, "printer_setting_audit"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE printer_setting_audit (
                        id SERIAL PRIMARY KEY,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        tab VARCHAR(30) NOT NULL,
                        action VARCHAR(50) NOT NULL,
                        payload_json TEXT NOT NULL,
                        sequence_id VARCHAR(50),
                        result VARCHAR(20) NOT NULL,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT now()
                    )
                    """
                )
            )
        else:
            await conn.execute(
                text(
                    """
                    CREATE TABLE printer_setting_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        tab TEXT NOT NULL,
                        action TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        sequence_id TEXT,
                        result TEXT NOT NULL,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT (CURRENT_TIMESTAMP)
                    )
                    """
                )
            )

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_printer_setting_audit_printer "
            "ON printer_setting_audit(printer_id, created_at DESC)"
        )
    )
