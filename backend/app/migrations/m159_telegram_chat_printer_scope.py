"""Per-chat printer scope for the Telegram bot.

``NotificationProvider.printer_id`` was the LAST provider-level telegram
knob (m045 moved everything else onto the chat), and it blocked the real
farm shape: the admin's chat watches every printer while a partner's chat
on the same bot watches only their machines — notifications and bot
control alike. ``telegram_chats.printer_ids`` is a JSON list; NULL = all
printers.

The seed preserves behaviour: if an enabled telegram provider was bound to
one printer, every existing chat inherits ``[that id]`` (only where the
chat has no scope of its own yet), and the provider binding is cleared —
``_coerce_telegram_provider_fields`` keeps it cleared from here on.
Columns are named explicitly throughout (no entity-wide selects).
"""

from sqlalchemy import text

from backend.app.migrations.helpers import add_column

version = 159
name = "telegram_chat_printer_scope"


async def upgrade(conn):
    await add_column(conn, "telegram_chats", "printer_ids TEXT")

    # Copy a telegram provider's printer binding down onto the chats, then
    # clear it. String comparison against NULL keeps this one statement per
    # dialect-safe operation; JSON is written as its text form.
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
