"""Add ``nozzle_offset_cali`` to ``print_queue``.

Dual-nozzle printers (H2D / H2D Pro / H2C / X2D) expose a nozzle-offset
calibration toggle in BambuStudio's send dialog. Our project_file MQTT payload
previously hardcoded ``nozzle_offset_cali: 2`` (skip), so users had no way to
control it — critical for diamond-nozzle setups that must keep the calibration
OFF. The per-item flag threads through the dispatcher into ``start_print``, which
encodes it as 1 (run) / 2 (skip) and forces 2 on single-nozzle machines.

Boolean, ``DEFAULT 1`` (run) to match BambuStudio's default. Fresh installs get
the column from the model's ``create_all``; this backfills existing DBs.

Upstream Bambuddy #1682 / commit ``2c2725cb`` (which used an inline
``run_migrations`` step; BamDude uses numbered migrations).
"""

from backend.app.migrations.helpers import add_column

version = 88
name = "print_queue_nozzle_offset_cali"


async def upgrade(conn):
    await add_column(conn, "print_queue", "nozzle_offset_cali BOOLEAN DEFAULT 1 NOT NULL")
