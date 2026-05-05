"""Telegram provider/chat config refactor — normalise legacy provider-side gates.

The Telegram notification model used to live in two places: 24 ``on_*`` event
columns + ``quiet_hours_*`` on ``notification_providers`` (provider-wide gate)
AND ``notify_events`` + ``quiet_hours_*`` on ``telegram_chats`` (per-chat
gate). Operators had to keep both in sync per event, and disabling a flag on
the provider silently muted ALL chats for that event.

After the refactor the Telegram provider exposes only ``enabled``,
``daily_digest_enabled``, and ``daily_digest_time`` (+ token + name + optional
printer scope); per-event opt-in, quiet hours, and per-chat digest opt-in all
live on each ``TelegramChat`` row.

This migration normalises existing ``provider_type='telegram'`` rows so that
the legacy provider-level gates become transparent and the per-chat values
become authoritative:

* All 24 ``on_*`` flags → ``TRUE`` (so any future fallback path that still
  reads them won't drop events; the new dispatch path skips this filter for
  telegram entirely, but defence-in-depth).
* ``quiet_hours_enabled`` → ``FALSE`` (matching the dispatch path which
  already skipped provider-level quiet-hours for telegram on
  ``notification_service.py:714``).

Idempotent. Re-runs are no-ops because the UPDATE leaves rows in the same
state. Schema is untouched: the columns remain on ``notification_providers``
because they are shared with email / ntfy / pushover / discord / webhook /
homeassistant / callmebot — those provider types still rely on them.

``telegram_chats`` is untouched. Existing chats with ``notify_events=NULL``
keep the runtime fallback to ``DEFAULT_NOTIFY_EVENTS``
(see ``models/telegram_chat.py``).
"""

from sqlalchemy import text

version = 45
name = "telegram_config_refactor"


_ON_FIELDS = (
    "on_print_start",
    "on_print_complete",
    "on_print_failed",
    "on_print_stopped",
    "on_print_progress",
    "on_print_missing_spool_assignment",
    "on_printer_offline",
    "on_printer_error",
    "on_filament_low",
    "on_maintenance_due",
    "on_ams_humidity_high",
    "on_ams_temperature_high",
    "on_ams_ht_humidity_high",
    "on_ams_ht_temperature_high",
    "on_plate_not_empty",
    "on_bed_cooled",
    "on_first_layer_complete",
    "on_queue_job_added",
    "on_queue_job_started",
    "on_queue_job_waiting",
    "on_queue_job_skipped",
    "on_queue_job_failed",
    "on_queue_completed",
)


async def upgrade(conn):
    # Use 1 (TRUE) / 0 (FALSE) as plain INTEGER literals — both SQLite and
    # PostgreSQL accept this and we avoid bool-vs-int dialect quirks.
    set_clause = ", ".join(f"{f}=1" for f in _ON_FIELDS)
    await conn.execute(
        text(f"UPDATE notification_providers SET {set_clause}, quiet_hours_enabled=0 WHERE provider_type='telegram'")
    )
