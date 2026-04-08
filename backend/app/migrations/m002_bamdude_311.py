"""BamDude 3.0.1 → 3.1.1 schema changes.

Adds: printer_queues, macros, swap mode, stagger, maintenance history tracking,
queue rework (queue_id), printer_models on maintenance types.
Drops: filaments table (dead code, cost now from spool.cost_per_kg).
"""

version = 2
name = "bamdude_311"


async def upgrade(conn):
    """Schema changes for 3.0.1 → 3.1.1."""

    from sqlalchemy import text

    from backend.app.migrations.helpers import add_column, column_exists, recreate_table, table_exists

    # ── Printer enhancements ──
    await add_column(conn, "printers", "stagger_interval_minutes INTEGER NOT NULL DEFAULT 0")
    await add_column(conn, "printers", "swap_mode_enabled BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "printers", "auto_light_off BOOLEAN NOT NULL DEFAULT 0")

    # ── Library files: add swap_compatible, drop print_count ──
    await add_column(conn, "library_files", "swap_compatible BOOLEAN NOT NULL DEFAULT 0")
    if await column_exists(conn, "library_files", "print_count"):
        await recreate_table(
            conn,
            "library_files",
            """CREATE TABLE library_files (
                id INTEGER PRIMARY KEY,
                folder_id INTEGER REFERENCES library_folders(id) ON DELETE CASCADE,
                project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
                is_external BOOLEAN NOT NULL DEFAULT 0,
                filename VARCHAR(255) NOT NULL,
                file_path VARCHAR(500) NOT NULL,
                file_type VARCHAR(10) NOT NULL,
                file_size INTEGER NOT NULL,
                file_hash VARCHAR(64),
                thumbnail_path VARCHAR(500),
                file_metadata JSON,
                last_printed_at DATETIME,
                notes TEXT,
                created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                swap_compatible BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
            "id, folder_id, project_id, is_external, filename, file_path, file_type, "
            "file_size, file_hash, thumbnail_path, file_metadata, last_printed_at, notes, "
            "created_by_id, swap_compatible, created_at, updated_at",
        )

    # ── Archive: swap compatibility ──
    await add_column(conn, "print_archives", "swap_compatible BOOLEAN NOT NULL DEFAULT 0")

    # ── Macros table ──
    if not await table_exists(conn, "macros"):
        await conn.execute(text("""
            CREATE TABLE macros (
                id INTEGER PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                printer_models TEXT NOT NULL DEFAULT '["*"]',
                swap_mode_only BOOLEAN NOT NULL DEFAULT 0,
                event VARCHAR(50) NOT NULL,
                gcode TEXT NOT NULL DEFAULT '',
                is_custom BOOLEAN NOT NULL DEFAULT 0,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))

    # ── Printer queues ──
    if not await table_exists(conn, "printer_queues"):
        await conn.execute(text("""
            CREATE TABLE printer_queues (
                id INTEGER PRIMARY KEY,
                printer_id INTEGER NOT NULL UNIQUE REFERENCES printers(id) ON DELETE CASCADE,
                status VARCHAR(20) NOT NULL DEFAULT 'idle',
                last_activity_at DATETIME,
                current_item_id INTEGER,
                pending_count INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                cancelled_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """))

        # Create printer_queues for existing printers
        await conn.execute(text(
            "INSERT OR IGNORE INTO printer_queues "
            "(id, printer_id, status, pending_count, completed_count, failed_count, "
            "cancelled_count, skipped_count, total_count) "
            "SELECT id, id, 'idle', 0, 0, 0, 0, 0, 0 FROM printers "
            "WHERE id NOT IN (SELECT printer_id FROM printer_queues)"
        ))

    # ── Queue rework: add queue_id to print_queue ──
    await add_column(conn, "print_queue", "queue_id INTEGER REFERENCES printer_queues(id)")

    # Migrate existing print_queue items: set queue_id = printer_id (only if old schema)
    if await column_exists(conn, "print_queue", "printer_id"):
        await conn.execute(text(
            "UPDATE print_queue SET queue_id = printer_id "
            "WHERE queue_id IS NULL AND printer_id IS NOT NULL"
        ))

        # Delete orphaned items (model-based items without printer)
        await conn.execute(text(
            "DELETE FROM print_queue WHERE queue_id IS NULL AND printer_id IS NULL"
        ))

        # Fix queue_id where printer_queues.id != printer_id
        try:
            await conn.execute(text(
                "UPDATE print_queue SET queue_id = ("
                "  SELECT pq.id FROM printer_queues pq WHERE pq.printer_id = print_queue.printer_id"
                ") WHERE printer_id IS NOT NULL AND EXISTS ("
                "  SELECT 1 FROM printer_queues pq2 "
                "  WHERE pq2.printer_id = print_queue.printer_id AND pq2.id != print_queue.queue_id"
                ")"
            ))
        except Exception:
            pass

    # Recount queue counters
    await conn.execute(text(
        "UPDATE printer_queues SET "
        "pending_count = (SELECT COUNT(*) FROM print_queue WHERE print_queue.queue_id = printer_queues.id AND print_queue.status = 'pending'), "
        "completed_count = (SELECT COUNT(*) FROM print_queue WHERE print_queue.queue_id = printer_queues.id AND print_queue.status = 'completed'), "
        "failed_count = (SELECT COUNT(*) FROM print_queue WHERE print_queue.queue_id = printer_queues.id AND print_queue.status = 'failed'), "
        "cancelled_count = (SELECT COUNT(*) FROM print_queue WHERE print_queue.queue_id = printer_queues.id AND print_queue.status = 'cancelled'), "
        "skipped_count = (SELECT COUNT(*) FROM print_queue WHERE print_queue.queue_id = printer_queues.id AND print_queue.status = 'skipped'), "
        "total_count = (SELECT COUNT(*) FROM print_queue WHERE print_queue.queue_id = printer_queues.id)"
    ))

    # ── Clean up print_queue: drop legacy columns ──
    # Remove: printer_id, require_previous_success, target_model, target_location,
    # filament_overrides, required_filament_types (all from removed model-based assignment)
    has_legacy = await column_exists(conn, "print_queue", "require_previous_success")
    if has_legacy:
        await recreate_table(
            conn,
            "print_queue",
            """CREATE TABLE print_queue (
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
                vibration_cali BOOLEAN NOT NULL DEFAULT 0,
                layer_inspect BOOLEAN NOT NULL DEFAULT 0,
                timelapse BOOLEAN NOT NULL DEFAULT 0,
                use_ams BOOLEAN NOT NULL DEFAULT 1,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                started_at DATETIME,
                completed_at DATETIME,
                error_message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL
            )""",
            "id, queue_id, waiting_reason, archive_id, library_file_id, project_id, "
            "position, scheduled_time, manual_start, auto_off_after, ams_mapping, plate_id, "
            "bed_levelling, flow_cali, vibration_cali, layer_inspect, timelapse, use_ams, "
            "status, started_at, completed_at, error_message, created_at, created_by_id",
        )

    # ── Clean up removed notification template and event ──
    await conn.execute(text("DELETE FROM notification_templates WHERE event_type = 'queue_job_assigned'"))

    # ── Notification providers: drop on_queue_job_assigned ──
    if await column_exists(conn, "notification_providers", "on_queue_job_assigned"):
        await recreate_table(
            conn,
            "notification_providers",
            """CREATE TABLE notification_providers (
                id INTEGER PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                provider_type VARCHAR(50) NOT NULL,
                enabled BOOLEAN DEFAULT 1,
                config TEXT NOT NULL,
                on_print_start BOOLEAN DEFAULT 0,
                on_print_complete BOOLEAN DEFAULT 1,
                on_print_failed BOOLEAN DEFAULT 1,
                on_print_stopped BOOLEAN DEFAULT 1,
                on_print_progress BOOLEAN DEFAULT 0,
                on_print_missing_spool_assignment BOOLEAN DEFAULT 0,
                on_printer_offline BOOLEAN DEFAULT 0,
                on_printer_error BOOLEAN DEFAULT 0,
                on_filament_low BOOLEAN DEFAULT 0,
                on_maintenance_due BOOLEAN DEFAULT 0,
                on_ams_humidity_high BOOLEAN DEFAULT 0,
                on_ams_temperature_high BOOLEAN DEFAULT 0,
                on_ams_ht_humidity_high BOOLEAN DEFAULT 0,
                on_ams_ht_temperature_high BOOLEAN DEFAULT 0,
                on_plate_not_empty BOOLEAN DEFAULT 1,
                on_bed_cooled BOOLEAN DEFAULT 0,
                on_first_layer_complete BOOLEAN DEFAULT 0,
                on_queue_job_added BOOLEAN DEFAULT 0,
                on_queue_job_started BOOLEAN DEFAULT 0,
                on_queue_job_waiting BOOLEAN DEFAULT 1,
                on_queue_job_skipped BOOLEAN DEFAULT 1,
                on_queue_job_failed BOOLEAN DEFAULT 1,
                on_queue_completed BOOLEAN DEFAULT 0,
                quiet_hours_enabled BOOLEAN DEFAULT 0,
                quiet_hours_start VARCHAR(5),
                quiet_hours_end VARCHAR(5),
                daily_digest_enabled BOOLEAN DEFAULT 0,
                daily_digest_time VARCHAR(5),
                printer_id INTEGER REFERENCES printers(id) ON DELETE SET NULL,
                last_success DATETIME,
                last_error TEXT,
                last_error_at DATETIME,
                created_at DATETIME,
                updated_at DATETIME
            )""",
            "id, name, provider_type, enabled, config, "
            "on_print_start, on_print_complete, on_print_failed, on_print_stopped, "
            "on_print_progress, on_print_missing_spool_assignment, "
            "on_printer_offline, on_printer_error, on_filament_low, on_maintenance_due, "
            "on_ams_humidity_high, on_ams_temperature_high, "
            "on_ams_ht_humidity_high, on_ams_ht_temperature_high, "
            "on_plate_not_empty, on_bed_cooled, on_first_layer_complete, "
            "on_queue_job_added, on_queue_job_started, on_queue_job_waiting, "
            "on_queue_job_skipped, on_queue_job_failed, on_queue_completed, "
            "quiet_hours_enabled, quiet_hours_start, quiet_hours_end, "
            "daily_digest_enabled, daily_digest_time, "
            "printer_id, last_success, last_error, last_error_at, "
            "created_at, updated_at",
        )

    # ── Maintenance types: printer_models ──
    await add_column(conn, "maintenance_types", "printer_models TEXT NOT NULL DEFAULT '[\"*\"]'")

    # Backfill model-specific maintenance types (EN + UK names)
    _carbon = '["X1C", "X1", "X1E", "P1P", "P1S"]'
    _steel = '["P2S"]'
    _linear = '["A1", "A1 Mini", "H2D", "H2D Pro", "H2C", "H2S"]'
    _type_models = [
        (_carbon, ["Clean Carbon Rods", "Очистити карбонові штанги"]),
        (_steel, ["Lubricate Steel Rods", "Змастити сталеві штанги"]),
        (_steel, ["Clean Steel Rods", "Очистити сталеві штанги"]),
        (_linear, ["Lubricate Linear Rails", "Змастити лінійні рейки"]),
        (_linear, ["Clean Linear Rails", "Очистити лінійні рейки"]),
    ]
    for models, names in _type_models:
        for name in names:
            await conn.execute(
                text("UPDATE maintenance_types SET printer_models = :models WHERE name = :name AND printer_models = '[\"*\"]'"),
                {"models": models, "name": name},
            )

    # ── Maintenance history: who performed ──
    await add_column(conn, "maintenance_history", "performed_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL")
    await add_column(conn, "maintenance_history", "performed_by_chat_id INTEGER REFERENCES telegram_chats(id) ON DELETE SET NULL")

    # Backfill performed_by_user_id from first user
    await conn.execute(text(
        "UPDATE maintenance_history SET performed_by_user_id = ("
        "  SELECT id FROM users ORDER BY id LIMIT 1"
        ") WHERE performed_by_user_id IS NULL AND performed_by_chat_id IS NULL"
    ))

    # ── Drop dead tables ──
    for dead_table in ("filaments", "print_log_entries"):
        if await table_exists(conn, dead_table):
            await conn.execute(text(f"DROP TABLE {dead_table}"))  # noqa: S608

    # ── Force auto_archive on all printers ──
    await conn.execute(text("UPDATE printers SET auto_archive = 1 WHERE auto_archive = 0"))

    # ── Rename github_backup → git_backup, add provider support ──
    if await table_exists(conn, "github_backup_config") and not await table_exists(conn, "git_backup_config"):
        await conn.execute(text("ALTER TABLE github_backup_config RENAME TO git_backup_config"))
        await add_column(conn, "git_backup_config", "provider VARCHAR(20) NOT NULL DEFAULT 'github'")
        await add_column(conn, "git_backup_config", "api_base_url VARCHAR(500)")
    # ── Git backup: spool + archive backup flags ──
    await add_column(conn, "git_backup_config", "backup_spools BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "git_backup_config", "backup_archives BOOLEAN NOT NULL DEFAULT 0")

    if await table_exists(conn, "github_backup_logs") and not await table_exists(conn, "git_backup_logs"):
        # Recreate logs with FK pointing to new table name
        await recreate_table(
            conn,
            "github_backup_logs",
            """CREATE TABLE git_backup_logs (
                id INTEGER PRIMARY KEY,
                config_id INTEGER REFERENCES git_backup_config(id) ON DELETE CASCADE,
                started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME,
                status VARCHAR(20) NOT NULL,
                trigger VARCHAR(20) NOT NULL,
                commit_sha VARCHAR(40),
                files_changed INTEGER NOT NULL DEFAULT 0,
                error_message TEXT
            )""",
            "id, config_id, started_at, completed_at, status, trigger, commit_sha, files_changed, error_message",
        )

    # ── Migrate legacy VP modes to file_manager ──
    if await table_exists(conn, "virtual_printers"):
        await conn.execute(text(
            "UPDATE virtual_printers SET mode = 'file_manager' "
            "WHERE mode IN ('immediate', 'review')"
        ))
        await conn.execute(text(
            "UPDATE virtual_printers SET mode = 'print_queue' "
            "WHERE mode = 'queue'"
        ))


async def seed(session_factory):
    """Seed new data for 3.1.1."""
    from backend.app.migrations.m001_bamdude_baseline import _seed_default_macros

    await _seed_default_macros(session_factory)
