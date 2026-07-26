"""Dedicate an AI Failure Detection notification event (upstream #1794).

Splits Obico failure-detection dispatch off the multiplexed ``on_printer_error``
gate onto its own ``on_ai_failure_detection`` provider column + ``ai_failure_detection``
template, so non-telegram providers can subscribe to spaghetti alerts without also
enabling HMS/AMS hardware-error pages. Reporter had a Discord provider with Obico
``action=notify`` and detection firing per the logs, yet spaghetti alerts never
reached Discord because ``obico_actions._notify`` rode ``on_printer_error`` (default
False) — an unrelated toggle the user never enabled.

Telegram is unaffected — per-chat ``telegram_chats.notify_events`` is the authority;
the new column is forced TRUE for telegram rows by ``_coerce_telegram_provider_fields``
(and here in ``upgrade``) so any residual provider-level fallback stays transparent.

``seed()`` localises the new template to ``settings.language`` from the locale JSON,
falling back to ``DEFAULT_TEMPLATES`` (English). Mirrors the m052 shape.

Idempotent. Re-runs are no-ops: ``add_column`` skips when the column exists, the
backfill UPDATEs leave rows in the same state, and ``seed()`` only inserts the row
when its ``event_type`` is missing.
"""

import json
from pathlib import Path

from sqlalchemy import select, text

from backend.app.migrations.helpers import add_column

version = 97
name = "ai_failure_detection_event"

_NEW_TEMPLATE_EVENT_TYPES = ("ai_failure_detection",)
_SUPPORTED_LOCALES = {"en", "uk"}
_DATA_DIR = Path(__file__).parent.parent / "data"


async def upgrade(conn):
    # DEFAULT 0 mirrors the m052 pattern; the WHERE IS NULL backfill below covers
    # the Postgres case where the column lands without a usable server default.
    await add_column(conn, "notification_providers", "on_ai_failure_detection BOOLEAN DEFAULT 0")
    await conn.execute(
        text("UPDATE notification_providers SET on_ai_failure_detection=FALSE WHERE on_ai_failure_detection IS NULL")
    )
    # Telegram normalisation (mirror of m045/m052): force the flag TRUE for all
    # telegram rows so the per-chat notify_events list stays the sole authority.
    await conn.execute(
        text("UPDATE notification_providers SET on_ai_failure_detection=TRUE WHERE provider_type='telegram'")
    )


async def seed(session_factory):
    """Insert the ``ai_failure_detection`` template localised to ``settings.language``.

    Resolution order: locale JSON (``data/notification_templates_<lang>.json``) when
    the system language is a supported locale and the JSON carries the event_type,
    else ``DEFAULT_TEMPLATES`` (English). Skips rows that already exist (idempotent).
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
