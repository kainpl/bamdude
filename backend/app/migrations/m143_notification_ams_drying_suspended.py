"""Add the ``on_ams_drying_suspended`` event: auto-drying gave up on an AMS unit.

The counterpart of ``ams_humidity_high``, and the reason it needs to exist: that
alarm says "this unit is damp", which is also what it says while BamDude is
busily drying it. This one says BamDude has **stopped** — two cycles in a row
ended with the reading no lower, so re-arming a third would only loop.

⚠️ Backfill default is **TRUE**, unlike its AMS neighbours. It fires at most
once per unit per suspension, and it reports that something which was happening
has stopped happening. Silence there reads as "still drying", which is exactly
how the reported re-arm loop went unnoticed for two days.

For ``provider_type='telegram'`` the m045 normalisation applies — per-event
filtering lives on ``telegram_chats.notify_events``, so the provider column is
forced TRUE and the event key joins ``ALL_NOTIFY_EVENTS``. It also joins
``DEFAULT_NOTIFY_EVENTS`` for the same reason the column defaults TRUE.

``seed()`` inserts the template already localised to the active system language,
the same way m142 does — inserting English on a ``language='uk'`` install leaves
the operator with English copy until the startup reconcile catches up, and any
notification firing inside that window is delivered in the wrong language.
"""

import logging

from backend.app.migrations.helpers import add_column

logger = logging.getLogger(__name__)

version = 143
name = "notification_ams_drying_suspended"

_EVENT_TYPE = "ams_drying_suspended"


async def upgrade(conn):
    await add_column(conn, "notification_providers", "on_ams_drying_suspended BOOLEAN DEFAULT 1")
    # Defence in depth, per m045: telegram's per-event authority is the chat, so
    # a provider-level flag must never be the thing that drops an event.
    from sqlalchemy import text

    await conn.execute(
        text("UPDATE notification_providers SET on_ams_drying_suspended = 1 WHERE provider_type = 'telegram'")
    )


async def seed(session_factory):
    from sqlalchemy import select

    from backend.app.models.notification_template import DEFAULT_TEMPLATES, NotificationTemplate
    from backend.app.models.settings import Settings

    async with session_factory() as session:
        existing = await session.execute(
            select(NotificationTemplate.id).where(NotificationTemplate.event_type == _EVENT_TYPE)
        )
        if existing.scalar_one_or_none() is not None:
            return

        default = next((t for t in DEFAULT_TEMPLATES if t["event_type"] == _EVENT_TYPE), None)
        if default is None:
            logger.warning("m143: no default template for %s — skipping seed", _EVENT_TYPE)
            return

        values = dict(default)
        lang_row = await session.execute(select(Settings.value).where(Settings.key == "language"))
        lang = (lang_row.scalar_one_or_none() or "en").strip().lower()
        if lang and lang != "en":
            try:
                import json
                from pathlib import Path

                path = Path(__file__).resolve().parents[1] / "data" / f"notification_templates_{lang}.json"
                if path.is_file():
                    localised = json.loads(path.read_text(encoding="utf-8")).get(_EVENT_TYPE)
                    if isinstance(localised, dict):
                        values.update(
                            {k: v for k, v in localised.items() if k in ("name", "title_template", "body_template")}
                        )
            except Exception:  # noqa: BLE001 - a missing translation must not fail the upgrade
                logger.warning("m143: could not localise the %s template to %r", _EVENT_TYPE, lang, exc_info=True)

        session.add(NotificationTemplate(**values))
        await session.commit()
        logger.info("m143: seeded the %s notification template (%s)", _EVENT_TYPE, lang)
