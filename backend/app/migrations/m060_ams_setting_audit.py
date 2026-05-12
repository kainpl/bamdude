"""Audit trail for AMS settings dialog (BamDude port of Bambu Studio AMSSetting).

Why this exists:
    BambuStudio's AMS Settings dialog has no audit trail and no RBAC — anyone
    with the desktop slicer can flip ``calibrate_remain_flag`` or
    ``auto_switch_filament`` on every printer they're paired with. On a farm
    we gate the same surface behind ``Permission.PRINTERS_UPDATE`` and record
    one row per applied change so operators can answer "who turned RFID
    auto-read off?" without diffing MQTT logs.

What this migration does:
    Creates ``ams_setting_audit`` with (printer_id, user_id, action,
    payload_json, sequence_id, result, error_message, created_at) plus a
    descending index on ``(printer_id, created_at DESC)`` for the most-recent-
    first lookup the future UI will want.

Idempotent: ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS``.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import table_exists

version = 60
name = "ams_setting_audit"


async def upgrade(conn):
    if not await table_exists(conn, "ams_setting_audit"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE ams_setting_audit (
                        id SERIAL PRIMARY KEY,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
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
                    CREATE TABLE ams_setting_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
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
            "CREATE INDEX IF NOT EXISTS ix_ams_setting_audit_printer ON ams_setting_audit(printer_id, created_at DESC)"
        )
    )
