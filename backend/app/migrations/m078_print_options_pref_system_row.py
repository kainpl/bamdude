"""Allow a system fallback row in ``print_options_preferences``.

Relaxes ``print_options_preferences.user_id`` to be nullable so a row with
``user_id IS NULL`` can act as the per-model *system default* — the fallback
the virtual-printer queue-receive path consults when a slicer sends a file
but omits the print-option flags (upstream Bambuddy #1235; precedence:
slicer-sent value → system row → column default).

Adds a partial unique index so at most one system row exists per model. The
existing composite ``(user_id, printer_model)`` unique can't guard this:
both SQLite and PostgreSQL treat distinct NULLs as non-conflicting in a
multi-column UNIQUE, so two ``(NULL, "P1S")`` rows would otherwise be legal.

Fresh installs get the nullable column + index directly from
``Base.metadata.create_all`` (model: ``models/print_options_preference.py``);
this migration only repairs existing installs whose column is still
``NOT NULL``.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import recreate_table, table_exists

version = 78
name = "print_options_pref_system_row"


async def upgrade(conn):
    if not await table_exists(conn, "print_options_preferences"):
        return  # fresh install — create_all already built the nullable schema

    if is_postgres():
        # PostgreSQL can relax the constraint in place.
        await conn.execute(text("ALTER TABLE print_options_preferences ALTER COLUMN user_id DROP NOT NULL"))
    else:
        # SQLite has no ALTER COLUMN DROP NOT NULL — rebuild the table with a
        # nullable user_id, preserving every existing row. The recreate drops
        # the table's secondary index, so re-create it afterwards.
        new_ddl = """
            CREATE TABLE print_options_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                printer_model VARCHAR(64) NOT NULL,
                options JSON NOT NULL,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT uq_print_options_pref_user_model UNIQUE (user_id, printer_model)
            )
        """
        await recreate_table(
            conn,
            "print_options_preferences",
            new_ddl,
            "id, user_id, printer_model, options, updated_at",
        )
        await conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_print_options_preferences_user_id ON print_options_preferences (user_id)"
            )
        )

    # Defense-in-depth: one system row per model. Partial unique index is
    # supported on both SQLite (3.8.0+) and PostgreSQL.
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_print_options_pref_system_model "
            "ON print_options_preferences (printer_model) WHERE user_id IS NULL"
        )
    )
