"""Drop the ``vibration_cali`` column from ``print_queue``.

Bambu Studio itself hardcodes ``task_vibration_cali = false`` for every model
in the current source (see SelectMachine.cpp::set_print_config call and
SendMultiMachinePage.cpp), and the per-print checkbox has been removed from
the Studio UI. Vibration calibration now lives only in the standalone
calibration wizard, not as a per-print toggle. Dropping the column here so it
stops round-tripping through our schemas/UI; the MQTT payload still sends
``"vibration_cali": false`` for firmware compatibility.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import column_exists, recreate_table

version = 7
name = "drop_vibration_cali"


_NEW_DDL = """CREATE TABLE print_queue (
    id INTEGER PRIMARY KEY,
    queue_id INTEGER NOT NULL REFERENCES printer_queues(id),
    waiting_reason TEXT,
    archive_id INTEGER REFERENCES print_archives(id) ON DELETE CASCADE,
    library_file_id INTEGER REFERENCES library_files(id) ON DELETE CASCADE,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    position INTEGER NOT NULL DEFAULT 0,
    scheduled_time DATETIME,
    manual_start BOOLEAN NOT NULL DEFAULT 0,
    auto_off_after BOOLEAN NOT NULL DEFAULT 0,
    ams_mapping TEXT,
    plate_id INTEGER,
    bed_levelling BOOLEAN NOT NULL DEFAULT 1,
    flow_cali BOOLEAN NOT NULL DEFAULT 1,
    layer_inspect BOOLEAN NOT NULL DEFAULT 0,
    timelapse BOOLEAN NOT NULL DEFAULT 0,
    use_ams BOOLEAN NOT NULL DEFAULT 1,
    mesh_mode_fast_check BOOLEAN NOT NULL DEFAULT 1,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    batch_id VARCHAR(36),
    started_at DATETIME,
    completed_at DATETIME,
    error_message TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL
)"""

_COLUMNS_TO_COPY = (
    "id, queue_id, waiting_reason, archive_id, library_file_id, project_id, "
    "position, scheduled_time, manual_start, auto_off_after, ams_mapping, plate_id, "
    "bed_levelling, flow_cali, layer_inspect, timelapse, use_ams, mesh_mode_fast_check, "
    "status, batch_id, started_at, completed_at, error_message, created_at, created_by_id"
)


async def upgrade(conn):
    if not await column_exists(conn, "print_queue", "vibration_cali"):
        return
    await recreate_table(conn, "print_queue", _NEW_DDL, _COLUMNS_TO_COPY)
    # recreate_table on SQLite drops the table, so re-create the batch_id index.
    # PostgreSQL keeps indexes across ALTER TABLE DROP COLUMN, but IF NOT EXISTS
    # makes the statement safe for both dialects.
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_print_queue_batch_id ON print_queue(batch_id)"))
