"""Add tri-state calibration-mode overlay columns to ``print_queue``.

Three NULLABLE ``VARCHAR(8)`` columns — ``bed_levelling_mode``,
``flow_cali_mode``, ``nozzle_offset_cali_mode`` — carry the off/auto/on
tri-state for the print-calibration toggles on models whose firmware supports
an ``auto`` mode (H2/X-series). See
``docs/superpowers/specs/2026-07-22-tristate-print-calibration-SAFE.md``.

DRIFT-SAFE design (this is the migration whose earlier form broke production —
rebuilt per the SAFE spec): the columns are **nullable with NO default**.
``NULL`` means "derive from the legacy bool companion" so every existing row is
behaviour-identical without a data backfill. The legacy bool columns
(``bed_levelling`` / ``flow_cali`` / ``nozzle_offset_cali``) stay the source of
truth for off/on; these columns carry only the extra ``'auto'`` state (or
mirror the bool). Readers: ``mode = <x>_mode or ('on' if <x> else 'off')``.

Fresh installs get the columns from the model's ``create_all``; this backfills
existing DBs. ``add_column`` is idempotent (skips a column that already exists),
so a re-run under DEBUG mode — or a DB that already carries the columns from an
earlier attempt — is a safe no-op.

A companion boot self-check in ``core/database.py::_verify_calibration_mode_columns``
asserts these three columns exist after migrations, turning any future ORM↔DB
drift into a loud startup failure instead of a silent mid-print 500.
"""

from backend.app.migrations.helpers import add_column

version = 106
name = "print_queue_calibration_modes"


async def upgrade(conn):
    await add_column(conn, "print_queue", "bed_levelling_mode VARCHAR(8)")
    await add_column(conn, "print_queue", "flow_cali_mode VARCHAR(8)")
    await add_column(conn, "print_queue", "nozzle_offset_cali_mode VARCHAR(8)")
