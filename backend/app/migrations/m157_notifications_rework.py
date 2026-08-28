"""The notifications subsystem rework of the 0.5.6 cycle, in one migration.

Squash of four consecutive same-subsystem migrations (m157..m160 of the
unreleased line — none ever shipped, so the freeze rule does not apply):

1. **Per-chat progress-milestone floor** (#28) —
   ``telegram_chats.progress_min_duration_minutes``. NULL or 0 = always
   send, N mutes prints estimated shorter than N minutes. Per chat because
   that is telegram's authority model (m045).
2. **Per-provider progress-milestone floor** —
   ``notification_providers.progress_min_duration_minutes``, same
   semantics for every non-telegram channel. No global fallback exists.
3. **Per-chat printer scope** — ``telegram_chats.printer_ids`` (JSON list,
   NULL = all printers), covering notifications AND bot control. An
   existing provider-level binding is copied down onto the chats and
   cleared; ``_coerce_telegram_provider_fields`` keeps it cleared.
4. **One JSON subscription list instead of 34 per-event boolean columns**
   — ``notification_providers.subscribed_events`` (``on_*`` keys, the API
   contract's own names). The backfill materialises each row's current
   True set, then every legacy column is dropped. A future event is a
   registry row in ``models/notification.py`` plus a schema field — never
   DDL again.

Chain safety on fresh installs: event columns younger than m045 re-create
themselves (their migrations start with ``add_column``), and the 23
columns m045 UPDATEs by name are kept alive by the schema shim in
``models/notification.py`` (``_M045_SHIM_COLUMNS``) until this migration
reaches its point in the chain and drops them. Every step is guarded, so
a ``DEBUG=true`` re-run after the fact is a no-op.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column, column_exists, drop_column

version = 157
name = "notifications_rework"

# The full legacy event-column set at the moment of this migration. A FROZEN
# copy on purpose — the live registry keeps growing, and this list must
# forever describe the schema as it stood here.
_LEGACY_EVENT_COLUMNS = (
    "on_print_start",
    "on_print_complete",
    "on_print_failed",
    "on_print_stopped",
    "on_print_progress",
    "on_print_missing_spool_assignment",
    "on_print_paused",
    "on_print_resumed",
    "on_printer_offline",
    "on_printer_error",
    "on_ai_failure_detection",
    "on_filament_low",
    "on_filament_runout",
    "on_filament_deficit",
    "on_maintenance_due",
    "on_ams_humidity_high",
    "on_ams_temperature_high",
    "on_ams_drying_suspended",
    "on_ams_ht_humidity_high",
    "on_ams_ht_temperature_high",
    "on_sensor_threshold",
    "on_sensor_silent",
    "on_plate_not_empty",
    "on_bed_cooled",
    "on_first_layer_complete",
    "on_queue_job_added",
    "on_queue_job_started",
    "on_queue_job_waiting",
    "on_queue_job_skipped",
    "on_queue_job_failed",
    "on_queue_completed",
    "on_printer_queue_completed",
    "on_stock_reorder_alert",
    "on_stock_break_alert",
)


async def upgrade(conn):
    import json

    # --- 1 + 2: the two progress-milestone floors -------------------------
    await add_column(conn, "telegram_chats", "progress_min_duration_minutes INTEGER")
    await add_column(conn, "notification_providers", "progress_min_duration_minutes INTEGER")

    # --- 3: per-chat printer scope ---------------------------------------
    await add_column(conn, "telegram_chats", "printer_ids TEXT")

    # Copy a telegram provider's printer binding down onto the chats, then
    # clear it (only where the chat has no scope of its own yet).
    result = await conn.execute(
        text(
            "SELECT printer_id FROM notification_providers "
            "WHERE provider_type = 'telegram' AND printer_id IS NOT NULL "
            "ORDER BY id LIMIT 1"
        )
    )
    row = result.first()
    if row and row[0] is not None:
        await conn.execute(
            text("UPDATE telegram_chats SET printer_ids = :scope WHERE printer_ids IS NULL").bindparams(
                scope=f"[{int(row[0])}]"
            )
        )
    await conn.execute(text("UPDATE notification_providers SET printer_id = NULL WHERE provider_type = 'telegram'"))

    # --- 4: subscriptions become one JSON list ---------------------------
    await add_column(conn, "notification_providers", "subscribed_events TEXT")

    if not await column_exists(conn, "notification_providers", "on_print_start"):
        return  # already migrated (DEBUG re-run)

    # Some columns can be missing on exotic upgrade paths (a DB that never
    # ran one of the event migrations) — read only what exists.
    present = [c for c in _LEGACY_EVENT_COLUMNS if await column_exists(conn, "notification_providers", c)]

    rows = (
        await conn.execute(
            text(f"SELECT id, {', '.join(present)} FROM notification_providers WHERE subscribed_events IS NULL")
        )
    ).fetchall()
    for provider_row in rows:
        enabled = [col for col, value in zip(present, provider_row[1:], strict=True) if value]
        await conn.execute(
            text("UPDATE notification_providers SET subscribed_events = :events WHERE id = :id").bindparams(
                events=json.dumps(sorted(enabled)), id=provider_row[0]
            )
        )

    for col in _LEGACY_EVENT_COLUMNS:
        await drop_column(conn, "notification_providers", col)
