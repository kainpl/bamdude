"""Drop the legacy ``printers.auto_light_off`` column.

Replaced by the generic macro framework: operators configure a
``chamber_light_off`` mqtt-action macro on the ``print_started`` event
(and the freshly-added ``print_finished`` event for "light back on").
The boolean flag was strictly weaker — no delay, no on/off symmetry,
no per-printer-model filtering — and keeping both a flag + macros
around was redundant.

PostgreSQL path: native ``ALTER TABLE ... DROP COLUMN``.
SQLite path: ``recreate_table`` — copy into a new table without the
column, drop the old, rename (same dance m019 uses for printer_queues).
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import recreate_table

version = 21
name = "drop_auto_light_off"


# New DDL mirrors models/printer.py with ``auto_light_off`` removed. When
# adding a column to the Printer model you must update this DDL too (or the
# column won't survive a SQLite reinstall that reapplies m021 on top of a
# partially-migrated DB). Foreign keys stay implicit — other tables
# reference ``printers(id)`` and are untouched by this rename dance.
_PRINTERS_NEW_DDL = """CREATE TABLE printers (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    serial_number VARCHAR(50) NOT NULL UNIQUE,
    ip_address VARCHAR(253) NOT NULL,
    access_code VARCHAR(20) NOT NULL,
    model VARCHAR(50),
    location VARCHAR(100),
    nozzle_count INTEGER NOT NULL DEFAULT 1,
    is_active BOOLEAN NOT NULL DEFAULT 1,
    auto_archive BOOLEAN NOT NULL DEFAULT 1,
    cleanup_after_print BOOLEAN NOT NULL DEFAULT 0,
    mqtt_connection_timeout INTEGER NOT NULL DEFAULT 900,
    print_hours_offset FLOAT NOT NULL DEFAULT 0.0,
    runtime_seconds INTEGER NOT NULL DEFAULT 0,
    last_runtime_update DATETIME,
    external_camera_url VARCHAR(500),
    external_camera_type VARCHAR(20),
    external_camera_enabled BOOLEAN NOT NULL DEFAULT 0,
    camera_rotation INTEGER NOT NULL DEFAULT 0,
    plate_detection_enabled BOOLEAN NOT NULL DEFAULT 0,
    plate_detection_roi_x FLOAT,
    plate_detection_roi_y FLOAT,
    plate_detection_roi_w FLOAT,
    plate_detection_roi_h FLOAT,
    stagger_interval_minutes INTEGER NOT NULL DEFAULT 0,
    swap_mode_enabled BOOLEAN NOT NULL DEFAULT 0,
    swap_profile VARCHAR(50),
    require_plate_clear BOOLEAN NOT NULL DEFAULT 1,
    awaiting_plate_clear BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
)"""

_PRINTERS_KEEP_COLUMNS = (
    "id, name, serial_number, ip_address, access_code, model, location, "
    "nozzle_count, is_active, auto_archive, cleanup_after_print, "
    "mqtt_connection_timeout, print_hours_offset, runtime_seconds, "
    "last_runtime_update, external_camera_url, external_camera_type, "
    "external_camera_enabled, camera_rotation, plate_detection_enabled, "
    "plate_detection_roi_x, plate_detection_roi_y, plate_detection_roi_w, "
    "plate_detection_roi_h, stagger_interval_minutes, swap_mode_enabled, "
    "swap_profile, require_plate_clear, awaiting_plate_clear, "
    "created_at, updated_at"
)


async def upgrade(conn):
    if is_postgres():
        await conn.execute(text("ALTER TABLE printers DROP COLUMN IF EXISTS auto_light_off"))
        return

    # SQLite: recreate_table copies rows, drops old, renames — same pattern
    # as m019 for printer_queues. Row-count assertion guards against silent
    # data loss during the copy.
    before = (await conn.execute(text("SELECT COUNT(*) FROM printers"))).scalar() or 0
    await recreate_table(conn, "printers", _PRINTERS_NEW_DDL, _PRINTERS_KEEP_COLUMNS)
    after = (await conn.execute(text("SELECT COUNT(*) FROM printers"))).scalar() or 0
    if after != before:
        raise RuntimeError(f"m021 printers recreate lost rows: before={before} after={after}")


async def seed(session_factory):  # pragma: no cover — no-op
    async with session_factory() as db:
        _ = db  # noqa: ARG001
