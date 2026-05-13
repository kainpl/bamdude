"""Filament Calibration wizard tables + print archive/queue flags.

What this migration does:
    1. CREATE TABLE filament_calibration — per-filament-type cali results
       (multi-row history with partial unique on is_active).
    2. CREATE TABLE calibration_session — wizard orchestration row.
    3. CREATE TABLE calibration_audit — BS-parity action log.
    4. ALTER print_archives ADD is_calibration BOOLEAN, calibration_session_id INT.
    5. ALTER print_queue ADD same two cols.

Why:
    BS PressureAdvanceWizard + FlowRateWizard need (filament_id, nozzle_dia,
    nozzle_vol_type, extruder_id, printer_model) as cali key; results carry
    K + N (PA) or flow_ratio + confidence. Multi-row per combo + is_active
    mirrors printer-side 16-slot history. print_archives.is_calibration
    flags cali prints separately from normal print archive entries.

Idempotent: CREATE TABLE IF NOT EXISTS + add_column helpers.
"""

from sqlalchemy import text

from backend.app.core.db_dialect import is_postgres
from backend.app.migrations.helpers import add_column, table_exists

version = 62
name = "filament_calibration"


async def upgrade(conn):
    # --- 1. filament_calibration ----------------------------------------
    if not await table_exists(conn, "filament_calibration"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE filament_calibration (
                        id SERIAL PRIMARY KEY,
                        printer_model VARCHAR(50) NOT NULL,
                        filament_id VARCHAR(50) NOT NULL,
                        filament_setting_id VARCHAR(100),
                        nozzle_diameter DOUBLE PRECISION NOT NULL,
                        nozzle_volume_type VARCHAR(20) NOT NULL,
                        extruder_id INTEGER NOT NULL DEFAULT 0,
                        pa_k_value DOUBLE PRECISION,
                        pa_n_coef DOUBLE PRECISION,
                        flow_ratio DOUBLE PRECISION,
                        confidence INTEGER,
                        cali_mode VARCHAR(30) NOT NULL,
                        source VARCHAR(20) NOT NULL,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        cali_idx INTEGER,
                        name VARCHAR(120) NOT NULL,
                        notes TEXT,
                        nozzle_id VARCHAR(20),
                        calibrated_on_printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
                        calibrated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT now()
                    )
                    """
                )
            )
        else:
            await conn.execute(
                text(
                    """
                    CREATE TABLE filament_calibration (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_model TEXT NOT NULL,
                        filament_id TEXT NOT NULL,
                        filament_setting_id TEXT,
                        nozzle_diameter REAL NOT NULL,
                        nozzle_volume_type TEXT NOT NULL,
                        extruder_id INTEGER NOT NULL DEFAULT 0,
                        pa_k_value REAL,
                        pa_n_coef REAL,
                        flow_ratio REAL,
                        confidence INTEGER,
                        cali_mode TEXT NOT NULL,
                        source TEXT NOT NULL,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        cali_idx INTEGER,
                        name TEXT NOT NULL,
                        notes TEXT,
                        nozzle_id TEXT,
                        calibrated_on_printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
                        calibrated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_filament_cali_lookup "
            "ON filament_calibration(printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id)"
        )
    )

    # Partial unique index — only one is_active row per combo
    if is_postgres():
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_filament_cali_active
                ON filament_calibration
                  (printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id)
                WHERE is_active = TRUE
                """
            )
        )
    else:
        await conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_filament_cali_active
                ON filament_calibration
                  (printer_model, filament_id, nozzle_diameter, nozzle_volume_type, extruder_id)
                WHERE is_active = 1
                """
            )
        )

    # --- 2. calibration_session ----------------------------------------
    if not await table_exists(conn, "calibration_session"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE calibration_session (
                        id SERIAL PRIMARY KEY,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        cali_mode VARCHAR(30) NOT NULL,
                        method VARCHAR(20) NOT NULL,
                        nozzle_diameter DOUBLE PRECISION NOT NULL,
                        nozzle_volume_type VARCHAR(20) NOT NULL,
                        extruder_id INTEGER NOT NULL DEFAULT 0,
                        filaments_json TEXT NOT NULL,
                        status VARCHAR(30) NOT NULL,
                        mqtt_sequence_id VARCHAR(50),
                        print_queue_item_id INTEGER REFERENCES print_queue(id) ON DELETE SET NULL,
                        parent_session_id INTEGER REFERENCES calibration_session(id) ON DELETE SET NULL,
                        stage INTEGER NOT NULL DEFAULT 1,
                        coarse_ratio DOUBLE PRECISION,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT now(),
                        updated_at TIMESTAMP NOT NULL DEFAULT now()
                    )
                    """
                )
            )
        else:
            await conn.execute(
                text(
                    """
                    CREATE TABLE calibration_session (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        cali_mode TEXT NOT NULL,
                        method TEXT NOT NULL,
                        nozzle_diameter REAL NOT NULL,
                        nozzle_volume_type TEXT NOT NULL,
                        extruder_id INTEGER NOT NULL DEFAULT 0,
                        filaments_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        mqtt_sequence_id TEXT,
                        print_queue_item_id INTEGER REFERENCES print_queue(id) ON DELETE SET NULL,
                        parent_session_id INTEGER REFERENCES calibration_session(id) ON DELETE SET NULL,
                        stage INTEGER NOT NULL DEFAULT 1,
                        coarse_ratio REAL,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_calibration_session_printer "
            "ON calibration_session(printer_id, status, created_at DESC)"
        )
    )

    # --- 3. calibration_audit ------------------------------------------
    if not await table_exists(conn, "calibration_audit"):
        if is_postgres():
            await conn.execute(
                text(
                    """
                    CREATE TABLE calibration_audit (
                        id SERIAL PRIMARY KEY,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        session_id INTEGER REFERENCES calibration_session(id) ON DELETE SET NULL,
                        filament_calibration_id INTEGER REFERENCES filament_calibration(id) ON DELETE SET NULL,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        action VARCHAR(40) NOT NULL,
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
                    CREATE TABLE calibration_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                        session_id INTEGER REFERENCES calibration_session(id) ON DELETE SET NULL,
                        filament_calibration_id INTEGER REFERENCES filament_calibration(id) ON DELETE SET NULL,
                        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        action TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        sequence_id TEXT,
                        result TEXT NOT NULL,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

    await conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_calibration_audit_printer ON calibration_audit(printer_id, created_at DESC)"
        )
    )

    # --- 4. print_archives flags ---------------------------------------
    await add_column(conn, "print_archives", "is_calibration BOOLEAN NOT NULL DEFAULT FALSE")
    await add_column(conn, "print_archives", "calibration_session_id INTEGER")

    # --- 5. print_queue flags ------------------------------------------
    await add_column(conn, "print_queue", "is_calibration BOOLEAN NOT NULL DEFAULT FALSE")
    await add_column(conn, "print_queue", "calibration_session_id INTEGER")
