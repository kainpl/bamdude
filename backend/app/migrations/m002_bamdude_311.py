"""BamDude 3.0.1 → 3.1.1 schema changes.

Adds: printer_queues, macros, swap mode, stagger, maintenance history tracking,
queue rework (queue_id), printer_models on maintenance types.
"""

version = 2
name = "bamdude_311"


async def upgrade(conn):
    """Schema changes for 3.0.1 → 3.1.1."""

    from sqlalchemy import text

    from backend.app.migrations.helpers import add_column, table_exists

    # ── Printer enhancements ──
    await add_column(conn, "printers", "stagger_interval_minutes INTEGER NOT NULL DEFAULT 0")
    await add_column(conn, "printers", "swap_mode_enabled BOOLEAN NOT NULL DEFAULT 0")

    # ── Library & Archive: swap compatibility ──
    await add_column(conn, "library_files", "swap_compatible BOOLEAN NOT NULL DEFAULT 0")
    await add_column(conn, "library_files", "print_count INTEGER NOT NULL DEFAULT 0")
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

    # Migrate existing print_queue items: set queue_id = printer_id
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


async def seed(session_factory):
    """Seed new data for 3.1.1."""
    from backend.app.migrations.m001_bamdude_baseline import _seed_default_macros

    await _seed_default_macros(session_factory)
