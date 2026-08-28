"""Add the ``on_filament_deficit`` event: this print needs more than the spool holds.

Distinct from ``filament_low``, which is a **threshold on a spool** ("slot A1 at
12%"). This one is a **comparison against a specific job**: the print about to
start needs 20.5 g and the slot it is mapped to holds 9. A percentage cannot say
that — a spool at 40% is plenty for one job and short for the next.

⚠️ **It warns; it does not block.** The job dispatches either way. A farm
routinely finishes a spool mid-plate and swaps it, and a gate that refused would
stop work the operator fully intended. What was missing was being told at all:
until now the only sufficiency check lived in the print dialog, so an
auto-dispatched job — which never opens one — went out silently.

Backfill default is **TRUE** on existing providers: an operator who wanted
filament notifications wants this one, and it fires rarely by construction.

For ``provider_type='telegram'`` the m045 normalisation applies — per-event
filtering lives on ``telegram_chats.notify_events``, so the provider column is
forced TRUE and the event key joins ``ALL_NOTIFY_EVENTS``. It also joins
``DEFAULT_NOTIFY_EVENTS``: a chat that has never been configured should hear
about a print that is going to run out, which is the whole point.

``seed()`` inserts the template already localised to the active system language,
the same way m052 does — inserting English on a ``language='uk'`` install leaves
the operator with English copy until the startup reconcile catches up, and any
notification firing inside that window is delivered in the wrong language.
"""

import logging

from backend.app.migrations.helpers import add_column

logger = logging.getLogger(__name__)

version = 142
name = "notification_filament_deficit"

_EVENT_TYPE = "filament_deficit"


async def upgrade(conn):
    await add_column(conn, "notification_providers", "on_filament_deficit BOOLEAN DEFAULT 1")
    # Defence in depth, per m045: telegram's per-event authority is the chat, so
    # a provider-level flag must never be the thing that drops an event.
    from sqlalchemy import text

    # TRUE, not 1: SQLite reads both, PostgreSQL rejects an integer in a
    # boolean column and aborts the whole migration chain.
    await conn.execute(
        text("UPDATE notification_providers SET on_filament_deficit = TRUE WHERE provider_type = 'telegram'")
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
            logger.warning("m142: no default template for %s — skipping seed", _EVENT_TYPE)
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
                logger.warning("m142: could not localise the %s template to %r", _EVENT_TYPE, lang, exc_info=True)

        session.add(NotificationTemplate(**values))
        await session.commit()
        logger.info("m142: seeded the %s notification template (%s)", _EVENT_TYPE, lang)
