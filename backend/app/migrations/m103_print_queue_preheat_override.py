"""Add preheat / heat-soak override columns to ``print_queue`` (#1468).

The preheat / heat-soak scheduler stage runs on the idle printer between FTP
upload and start_print (M191 is silently ignored by Bambu firmware, so the soak
has to happen at the orchestration layer). Two per-item overrides let an operator
tune it per print:

  - ``preheat_override`` — 'inherit' (use the global ``preheat_enabled`` setting),
    'on' (force the stage even when the global is off), or 'off' (skip entirely).
  - ``preheat_chamber_target_override`` — an explicit chamber target °C that
    bypasses the per-filament-type derivation; NULL means derive from the loaded
    AMS filament types.

Default 'inherit' + NULL so existing queue items behave exactly as before the
migration. Fresh installs get the columns from the model's ``create_all``; this
backfills existing DBs.

Upstream Bambuddy #1468 / commit ``61a7f2e4`` (which used an inline migration in
``core/database.py``; BamDude uses numbered migrations).
"""

from backend.app.migrations.helpers import add_column

version = 103
name = "print_queue_preheat_override"


async def upgrade(conn):
    await add_column(conn, "print_queue", "preheat_override VARCHAR(20) DEFAULT 'inherit' NOT NULL")
    await add_column(conn, "print_queue", "preheat_chamber_target_override INTEGER")
