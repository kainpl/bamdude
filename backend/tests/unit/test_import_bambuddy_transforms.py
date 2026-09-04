"""The per-row transforms of the upstream-database importer.

``_import_table`` matches source columns to destination columns BY NAME, so a
column that was renamed needs a ``rename=`` and a **value** that was retired
needs a ``transform=``. The second kind is the quiet one: the column still
exists on both sides, so the dead value copies straight through and the row
looks imported until something asks it what its status means.


The whole-database run at the bottom is the other half: the transforms can all
be right while the importer writes a column the schema no longer has.
"""

import sqlite3

import pytest
from sqlalchemy import text

from backend.app.migrations.import_bambuddy import _transform_project, import_bambuddy_data


def test_the_retired_archived_status_becomes_completed():
    """m158 folded ``archived`` into ``completed``. An imported project that
    kept ``archived`` would match no filter, no picker and no validator — and
    ``PATCH`` on it answers 400, because the status it already holds is not one
    this build accepts."""
    assert _transform_project({"name": "Old", "status": "archived"})["status"] == "completed"


def test_every_live_status_is_left_alone():
    for status in ("active", "completed", "cancelled"):
        assert _transform_project({"status": status})["status"] == status


def test_a_row_without_a_status_column_survives_untouched():
    """``_import_table`` selects only the columns both sides have, so a source
    without ``status`` hands the transform a dict that simply lacks the key."""
    row = _transform_project({"name": "No status", "price": 12.5})
    assert row == {"name": "No status", "price": 12.5}


@pytest.mark.asyncio
async def test_a_legacy_database_imports_into_the_schema_we_actually_have(test_engine, tmp_path):
    """⚠️ It died on ``printer_queues.completed_count``.

    Phase 2 creates one queue per imported printer and Phase 8 recounts that
    queue's counters — both naming ``completed_count``, ``failed_count``,
    ``cancelled_count`` and ``total_count``. m019 removed those columns: only
    the LIVE-state counters are cached on the queue now, and the terminal ones
    roll off the archive table at read time. So every import of a Bambuddy
    database aborted at the first printer with "table printer_queues has no
    column named completed_count", and the whole migration was unreachable.

    Two more of the same class surfaced the moment the run got that far: a raw
    ``INSERT`` does not run a PYTHON-side column default, so every BamDude-only
    NOT NULL column the upstream table cannot supply has to be named in
    ``_COPY_WITH_DEFAULTS`` — ``printers.archived`` and ``printers``
    ``mqtt_recording`` were not, nor were ``print_queue``'s
    ``mesh_mode_fast_check`` and ``execute_swap_macros``.

    And the queue rows themselves never arrived: ``print_queue.printer_id`` is
    not a column on this side any more, so auto-detection dropped it before
    ``_make_queue_transform`` — which keys on exactly that — could read it, and
    every row was skipped without a word. That is why the queue assertions
    below are as load-bearing as the counters.

    ⚠️ The source tables below are UPSTREAM's own columns (checked against the
    Bambuddy checkout's models), not ours. A fixture written from our schema
    would supply the very columns whose absence is the bug.
    """
    legacy = tmp_path / "bambuddy.db"
    old_db = sqlite3.connect(legacy)
    old_db.execute(
        "CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT, serial_number TEXT, ip_address TEXT, "
        "access_code TEXT, model TEXT, location TEXT, nozzle_count INTEGER, is_active BOOLEAN, "
        "auto_archive BOOLEAN, print_hours_offset FLOAT, runtime_seconds INTEGER, "
        "external_camera_enabled BOOLEAN, camera_rotation INTEGER, plate_detection_enabled BOOLEAN, "
        "awaiting_plate_clear BOOLEAN)"
    )
    old_db.executemany(
        "INSERT INTO printers (id, name, serial_number, ip_address, access_code, model, nozzle_count, "
        "is_active, auto_archive, print_hours_offset, runtime_seconds, external_camera_enabled, "
        "camera_rotation, plate_detection_enabled, awaiting_plate_clear) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, 1, 1, 0.0, 0, 0, 0, 0, 0)",
        [
            (1, "A", "SER-A", "192.168.1.10", "11111111", "X1C"),
            (2, "B", "SER-B", "192.168.1.11", "22222222", "P1S"),
        ],
    )
    old_db.execute(
        "CREATE TABLE print_queue (id INTEGER PRIMARY KEY, printer_id INTEGER, position INTEGER, "
        "status TEXT, manual_start BOOLEAN, auto_off_after BOOLEAN, bed_levelling BOOLEAN, "
        "flow_cali BOOLEAN, layer_inspect BOOLEAN, timelapse BOOLEAN, use_ams BOOLEAN)"
    )
    old_db.executemany(
        "INSERT INTO print_queue (id, printer_id, position, status, manual_start, auto_off_after, "
        "bed_levelling, flow_cali, layer_inspect, timelapse, use_ams) VALUES (?, ?, ?, ?, 0, 0, 1, 1, 0, 0, 1)",
        [(1, 1, 0, "pending"), (2, 1, 1, "skipped"), (3, 2, 0, "pending")],
    )
    old_db.commit()
    old_db.close()

    # ``test_engine`` is a database built the way production builds a fresh one
    # — ``Base.metadata.create_all`` — which is the state the legacy import runs
    # against in ``init_db``.
    await import_bambuddy_data(test_engine, legacy)

    async with test_engine.begin() as conn:
        printers = (await conn.execute(text("SELECT id, name, archived FROM printers ORDER BY id"))).fetchall()
        queues = (
            await conn.execute(
                text("SELECT printer_id, status, pending_count, skipped_count FROM printer_queues ORDER BY id")
            )
        ).fetchall()
        queued = (await conn.execute(text("SELECT id, queue_id, status FROM print_queue ORDER BY id"))).fetchall()

    assert [tuple(row) for row in printers] == [(1, "A", 0), (2, "B", 0)]
    assert [tuple(row) for row in queued] == [(1, 1, "pending"), (2, 1, "skipped"), (3, 2, "pending")]
    # Phase 8 recounts what is still a column: one pending and one skipped on
    # the first printer, one pending on the second.
    assert [tuple(row) for row in queues] == [(1, "idle", 1, 1), (2, "idle", 1, 0)]
