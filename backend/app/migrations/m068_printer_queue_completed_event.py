"""Add the ``on_printer_queue_completed`` event flag to providers.

The existing ``queue_completed`` event is global — it fires only once every
printer's queue is empty (``pending_count == 0`` across the whole install).
A paused queue or a ``manual_start`` item anywhere keeps that count above
zero and permanently suppresses it. ``printer_queue_completed`` is the
per-printer counterpart: it fires the moment one printer drains its own
queue, and its message carries the printer name.

Backfill default is **TRUE** so existing installs receive it without a
manual provider edit — this is the event operators actually want, and the
global ``queue_completed`` stays opt-in (default FALSE) beside it.

For ``provider_type='telegram'`` rows the m045 normalisation applies: the
per-event filter lives on ``telegram_chats.notify_events``, so the new
provider column is forced TRUE. The new event key is added to
``ALL_NOTIFY_EVENTS`` **and** ``DEFAULT_NOTIFY_EVENTS`` in
``models/telegram_chat.py`` so chats with ``notify_events=NULL`` pick it up
automatically (chats with a custom event list are unaffected — same trade
as m052).

``seed()`` inserts the new ``notification_templates`` row localised to the
active ``settings.language`` (see m052 for the full rationale).

Idempotent — ``add_column`` is a no-op when the column exists, the UPDATEs
are stable, and ``seed()`` only inserts the row when its ``event_type`` is
missing.
"""

import json
from pathlib import Path

from sqlalchemy import select, text

from backend.app.migrations.helpers import add_column

version = 68
name = "printer_queue_completed_event"

_NEW_TEMPLATE_EVENT_TYPES = ("printer_queue_completed",)
_SUPPORTED_LOCALES = {"en", "uk"}
_DATA_DIR = Path(__file__).parent.parent / "data"


async def upgrade(conn):
    await add_column(conn, "notification_providers", "on_printer_queue_completed BOOLEAN DEFAULT 1")

    # Backfill rows where the column landed NULL (e.g. Postgres without a
    # server-side default) so every existing provider ends up TRUE.
    await conn.execute(
        text("UPDATE notification_providers SET on_printer_queue_completed=1 WHERE on_printer_queue_completed IS NULL")
    )

    # Telegram normalisation (mirror of m045/m052): force the flag TRUE for
    # telegram rows — the per-chat ``notify_events`` list is the authority.
    await conn.execute(
        text("UPDATE notification_providers SET on_printer_queue_completed=1 WHERE provider_type='telegram'")
    )


async def seed(session_factory):
    """Insert the ``printer_queue_completed`` template localised to ``settings.language``.

    Mirrors m052: prefer ``data/notification_templates_<lang>.json`` when the
    system language is supported and carries the event_type; fall back to the
    English ``DEFAULT_TEMPLATES`` copy. Skips the row if it already exists.
    """
    from backend.app.models.notification_template import DEFAULT_TEMPLATES, NotificationTemplate
    from backend.app.models.settings import Settings as SettingsModel

    async with session_factory() as session:
        lang_row = await session.execute(select(SettingsModel.value).where(SettingsModel.key == "language"))
        lang_value = lang_row.scalar_one_or_none()
        lang = lang_value if lang_value in _SUPPORTED_LOCALES else "en"

        locale_templates: dict = {}
        if lang != "en":
            json_path = _DATA_DIR / f"notification_templates_{lang}.json"
            if json_path.exists():
                try:
                    with open(json_path, encoding="utf-8") as f:
                        locale_templates = json.load(f)
                except (OSError, ValueError):
                    locale_templates = {}

        existing = await session.execute(
            select(NotificationTemplate.event_type).where(
                NotificationTemplate.event_type.in_(_NEW_TEMPLATE_EVENT_TYPES)
            )
        )
        existing_types = {row[0] for row in existing.fetchall()}

        defaults_by_type = {t["event_type"]: t for t in DEFAULT_TEMPLATES}

        for event_type in _NEW_TEMPLATE_EVENT_TYPES:
            if event_type in existing_types:
                continue
            data = locale_templates.get(event_type) or defaults_by_type.get(event_type)
            if data is None:
                continue
            session.add(
                NotificationTemplate(
                    event_type=event_type,
                    name=data["name"],
                    title_template=data["title_template"],
                    body_template=data["body_template"],
                    is_default=True,
                )
            )

        await session.commit()
