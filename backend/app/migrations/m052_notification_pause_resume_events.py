"""Add ``on_print_paused`` + ``on_print_resumed`` event flags to providers.

Pause / resume are first-class lifecycle events alongside start / complete /
failed / stopped. Operators want to know when a printer goes idle mid-print
because of a door, filament runout, or someone hit Pause on the screen — and
when it comes back. Backfill default is **TRUE** for both flags so existing
installs immediately receive the events without a manual provider edit.

For ``provider_type='telegram'`` rows the same defence-in-depth normalisation
from m045 applies: per-event filtering for telegram lives on
``telegram_chats.notify_events``, so the new provider columns are forced to
``TRUE`` so any future fallback that still reads them won't drop events. The
new event keys (``print_paused`` / ``print_resumed``) are added to
``ALL_NOTIFY_EVENTS`` in ``models/telegram_chat.py``; ``print_paused`` joins
``DEFAULT_NOTIFY_EVENTS`` so chats with ``notify_events=NULL`` get the
fallback automatically. ``print_resumed`` stays opt-in (lower-priority signal
— operators usually only care about the pause).

``seed()`` inserts the two new ``notification_templates`` rows on existing
installs **already-localised** to the active system language: it reads
``settings.language`` from the DB, opens
``data/notification_templates_<lang>.json``, and uses those values
(``name`` / ``title_template`` / ``body_template``) for the INSERT. Falls
back to the English copy from ``DEFAULT_TEMPLATES`` when the language
setting is missing / not in ``SUPPORTED_LOCALES`` / the JSON file lacks
the event_type. Without this localisation step the seed inserted English
defaults and operators with ``language='uk'`` saw English templates until
the post-startup ``_reconcile_locale_on_startup`` task UPDATEd them — a
race window where notifications fired before the reconcile finished
delivered English copy on a UK install. Fresh installs going through
``m001._seed_notification_templates`` still get English defaults at seed
time and rely on the same startup reconcile to localise; this migration
sidesteps that pattern because it can read ``settings.language`` (which
m001 can't — it runs before the language has been chosen).

Idempotent. Re-runs are no-ops because ``add_column`` is a no-op when the
column already exists, the UPDATE leaves rows in the same state, and
``seed()`` only inserts rows whose ``event_type`` is missing.
"""

import json
from pathlib import Path

from sqlalchemy import select, text

from backend.app.migrations.helpers import add_column

version = 52
name = "notification_pause_resume_events"

_NEW_TEMPLATE_EVENT_TYPES = ("print_paused", "print_resumed")
_SUPPORTED_LOCALES = {"en", "uk"}
_DATA_DIR = Path(__file__).parent.parent / "data"


async def upgrade(conn):
    await add_column(conn, "notification_providers", "on_print_paused BOOLEAN DEFAULT 1")
    await add_column(conn, "notification_providers", "on_print_resumed BOOLEAN DEFAULT 1")

    # Backfill any rows where the column landed but is NULL (e.g. when the
    # column was added without a server-side default on Postgres). Keeps
    # SQLite + Postgres behaviour identical: every existing provider row ends
    # up with the new flag = TRUE so operators don't have to re-edit each
    # provider after upgrade just to receive pause notifications.
    await conn.execute(text("UPDATE notification_providers SET on_print_paused=1 WHERE on_print_paused IS NULL"))
    await conn.execute(text("UPDATE notification_providers SET on_print_resumed=1 WHERE on_print_resumed IS NULL"))

    # Telegram normalisation (mirror of m045): force the new flags TRUE for
    # all telegram rows so the provider-level gate stays transparent and the
    # per-chat ``notify_events`` list is the only authority.
    await conn.execute(
        text("UPDATE notification_providers SET on_print_paused=1, on_print_resumed=1 WHERE provider_type='telegram'")
    )


async def seed(session_factory):
    """Insert the two new notification templates localised to ``settings.language``.

    Resolution order per event_type:
      1. Locale JSON (``data/notification_templates_<lang>.json``) — if the
         system language is set to one of ``_SUPPORTED_LOCALES`` AND the JSON
         carries the event_type.
      2. ``DEFAULT_TEMPLATES`` (English) — fallback when language is unset,
         not supported, JSON missing on disk, or JSON lacks the event_type.

    Skips any rows that already exist (idempotent + safe on partial-prior-runs).
    """
    from backend.app.models.notification_template import DEFAULT_TEMPLATES, NotificationTemplate
    from backend.app.models.settings import Settings as SettingsModel

    async with session_factory() as session:
        # Resolve active language from DB-backed settings. Reading the model
        # directly (instead of going through routes/settings.get_setting)
        # avoids a circular-import risk and keeps the migration
        # dependency-light.
        lang_row = await session.execute(select(SettingsModel.value).where(SettingsModel.key == "language"))
        lang_value = lang_row.scalar_one_or_none()
        lang = lang_value if lang_value in _SUPPORTED_LOCALES else "en"

        # Try to load the locale JSON; tolerate missing file or malformed JSON
        # by falling back to English defaults — never let this seed fail the
        # whole migration over an i18n-asset issue.
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

        # Index DEFAULT_TEMPLATES by event_type for the English fallback
        # lookup so we don't iterate the full list per insert.
        defaults_by_type = {t["event_type"]: t for t in DEFAULT_TEMPLATES}

        for event_type in _NEW_TEMPLATE_EVENT_TYPES:
            if event_type in existing_types:
                continue
            # Prefer locale copy; fall back to DEFAULT_TEMPLATES (English)
            data = locale_templates.get(event_type) or defaults_by_type.get(event_type)
            if data is None:
                # Should never happen — DEFAULT_TEMPLATES owns the canonical
                # English copy and is updated alongside this migration.
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
