"""One JSON subscription list instead of 34 per-event boolean columns.

``notification_providers`` grew a column per notification event, migration
after migration, for years. The subscriptions now live in ONE JSON list
(``subscribed_events`` — ``on_*`` keys, the API contract's own names), and a
future event is a registry row in ``models/notification.py`` plus a schema
field — never DDL again.

The backfill materialises each row's current True set into the list, then
every legacy column is dropped. Chain safety on fresh installs: the columns
younger than m045 re-create themselves (their migrations start with
``add_column``), and the 23 columns m045 UPDATEs by name are kept alive by
the schema shim in ``models/notification.py`` (``_M045_SHIM_COLUMNS``) until
this migration reaches its point in the chain and drops them. The whole body
is guarded on ``on_print_start`` existing, so a ``DEBUG=true`` re-run after
the drop is a no-op.
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column, column_exists, drop_column

version = 160
name = "provider_events_json"

# The full legacy set at the moment of this migration. A FROZEN copy on
# purpose — the live registry keeps growing, and this list must forever
# describe the schema as it stood here.
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

    await add_column(conn, "notification_providers", "subscribed_events TEXT")

    if not await column_exists(conn, "notification_providers", "on_print_start"):
        return  # already migrated (DEBUG re-run)

    # Some columns can be missing on exotic upgrade paths (a DB that never ran
    # one of the event migrations) — read only what exists.
    present = [c for c in _LEGACY_EVENT_COLUMNS if await column_exists(conn, "notification_providers", c)]

    rows = (
        await conn.execute(
            text(f"SELECT id, {', '.join(present)} FROM notification_providers WHERE subscribed_events IS NULL")
        )
    ).fetchall()
    for row in rows:
        enabled = [col for col, value in zip(present, row[1:], strict=True) if value]
        await conn.execute(
            text("UPDATE notification_providers SET subscribed_events = :events WHERE id = :id").bindparams(
                events=json.dumps(sorted(enabled)), id=row[0]
            )
        )

    for col in _LEGACY_EVENT_COLUMNS:
        await drop_column(conn, "notification_providers", col)
