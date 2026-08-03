"""Thresholds on sensor readings, and the alerts they raise.

Three schema changes and a seed.

``smart_sensor_thresholds`` holds one row per (sensor, quantity): the limits,
the deadband, and **the alarm state**. State in the row rather than in memory is
the whole point — ``_ams_alarm_cooldown`` in ``main.py`` is a dictionary in the
process, so a restart forgets that it has already rung.

``smart_sensors`` gains ``silent_since`` / ``silence_notified_at``. Silence has
no quantity, so it cannot live in a per-quantity row.

``notification_providers`` gains two flags, both defaulting to FALSE: nobody
who has not asked should start receiving anything. For ``provider_type =
'telegram'`` they are forced TRUE, the m045 normalisation — per-event filtering
for telegram lives on ``telegram_chats.notify_events`` and the provider columns
must stay transparent.

``seed()`` inserts the five templates already localised to ``settings.language``,
the same way m052 and m068 do: prefer ``data/notification_templates_<lang>.json``,
fall back to the English ``DEFAULT_TEMPLATES``, skip rows that already exist.

Idempotent: the CREATE is guarded, ``add_column`` is a no-op when the column is
there, and the seed only inserts what is missing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy import select, text

from backend.app.core.db_dialect import is_sqlite
from backend.app.migrations.helpers import add_column, table_exists

logger = logging.getLogger(__name__)

version = 127
name = "sensor_thresholds"

_NEW_TEMPLATE_EVENT_TYPES = (
    "sensor_above_max",
    "sensor_below_min",
    "sensor_back_in_range",
    "sensor_silent",
    "sensor_speaking_again",
)
_SUPPORTED_LOCALES = {"en", "uk"}
_DATA_DIR = Path(__file__).parent.parent / "data"


async def upgrade(conn):
    sqlite = is_sqlite()
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"

    if not await table_exists(conn, "smart_sensor_thresholds"):
        await conn.execute(
            text(
                f"""
                CREATE TABLE smart_sensor_thresholds (
                    id {pk},
                    sensor_id INTEGER NOT NULL REFERENCES smart_sensors(id) ON DELETE CASCADE,
                    kind VARCHAR(32) NOT NULL,
                    min_value FLOAT,
                    max_value FLOAT,
                    deadband FLOAT NOT NULL DEFAULT 0,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    state VARCHAR(8) NOT NULL DEFAULT 'ok',
                    state_since {ts},
                    notified_at {ts}
                )
                """
            )
        )
        logger.info("m127: created smart_sensor_thresholds")

    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_smart_sensor_thresholds_sensor_kind "
            "ON smart_sensor_thresholds (sensor_id, kind)"
        )
    )
    await conn.execute(
        text("CREATE INDEX IF NOT EXISTS ix_smart_sensor_thresholds_sensor_id ON smart_sensor_thresholds (sensor_id)")
    )

    await add_column(conn, "smart_sensors", f"silent_since {ts}")
    await add_column(conn, "smart_sensors", f"silence_notified_at {ts}")

    await add_column(conn, "notification_providers", "on_sensor_threshold BOOLEAN DEFAULT 0")
    await add_column(conn, "notification_providers", "on_sensor_silent BOOLEAN DEFAULT 0")

    # A column added without a server-side default lands NULL on PostgreSQL.
    await conn.execute(
        text("UPDATE notification_providers SET on_sensor_threshold=FALSE WHERE on_sensor_threshold IS NULL")
    )
    await conn.execute(text("UPDATE notification_providers SET on_sensor_silent=FALSE WHERE on_sensor_silent IS NULL"))

    # m045 normalisation: for telegram the per-chat list is the only authority,
    # so the provider columns are forced TRUE and stay transparent.
    await conn.execute(
        text(
            "UPDATE notification_providers SET on_sensor_threshold=TRUE, on_sensor_silent=TRUE "
            "WHERE provider_type='telegram'"
        )
    )


async def seed(session_factory):
    """Insert the five templates localised to ``settings.language`` (mirrors m068)."""
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
