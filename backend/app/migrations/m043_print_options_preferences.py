"""Per-(user, printer-model) saved PrintModal toggles.

Adds the ``print_options_preferences`` table (model:
``models/print_options_preference.py``). Holds one row per
``(user_id, printer_model)`` pair with a JSON blob of the operator's
last-submitted PrintModal toggle values. Read on modal open, written on
modal submit (direct print / queue add / auto-queue add).

Per-user, per-model so a farm of identical printers shares one row per
operator — see model docstring for the rationale.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import table_exists

version = 43
name = "print_options_preferences"


async def upgrade(conn):
    if await table_exists(conn, "print_options_preferences"):
        return
    # Plain CREATE TABLE — no ALTER, no data backfill. ``Base.metadata.create_all``
    # would also create this for fresh installs; the migration is here so
    # existing installs upgrading to 0.4.3 pick it up without needing to
    # re-run create_all manually.
    await conn.execute(
        text(
            """
            CREATE TABLE print_options_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                printer_model VARCHAR(64) NOT NULL,
                options JSON NOT NULL,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_print_options_pref_user_model UNIQUE (user_id, printer_model)
            )
            """
        )
    )
    await conn.execute(text("CREATE INDEX ix_print_options_preferences_user_id ON print_options_preferences (user_id)"))
