"""Scheduled AMS drying: ``drying_schedules`` (rules) + ``scheduled_dryings`` (runs).

Upstream Bambuddy #2703 has one-shot runs only; the recurring rule is ours
(owner's decision 2026-09-25, vault 60-specs/scheduled-drying-spec). Fresh
installs get both tables from ``create_all``; this creates them on existing
databases. The same DDL as the models: SERIAL / AUTOINCREMENT keys by dialect
(m176 is the pattern), and the indexes outside the table guard so a fresh
install — whose tables came from ``create_all`` — gets them all the same.
"""

import logging

from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 184
name = "scheduled_drying"


async def upgrade(conn):
    sqlite = conn.dialect.name == "sqlite"
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if sqlite else "SERIAL PRIMARY KEY"
    stamp = "DATETIME" if sqlite else "TIMESTAMP"
    false, true = ("0", "1") if sqlite else ("FALSE", "TRUE")

    if not await table_exists(conn, "drying_schedules"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE drying_schedules (
                id {pk},
                printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                ams_id INTEGER NOT NULL,
                temp INTEGER NOT NULL,
                duration_hours INTEGER NOT NULL,
                filament VARCHAR(50) NOT NULL DEFAULT '',
                rotate_tray BOOLEAN NOT NULL DEFAULT {false},
                start_time VARCHAR(5) NOT NULL,
                weekdays INTEGER NOT NULL DEFAULT 127,
                latest_start VARCHAR(5),
                enabled BOOLEAN NOT NULL DEFAULT {true},
                created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at {stamp} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at {stamp} NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    if not await table_exists(conn, "scheduled_dryings"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE scheduled_dryings (
                id {pk},
                printer_id INTEGER NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
                ams_id INTEGER NOT NULL,
                temp INTEGER NOT NULL,
                duration_hours INTEGER NOT NULL,
                filament VARCHAR(50) NOT NULL DEFAULT '',
                rotate_tray BOOLEAN NOT NULL DEFAULT {false},
                schedule_id INTEGER REFERENCES drying_schedules(id) ON DELETE SET NULL,
                start_after {stamp},
                latest_start {stamp},
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                reason VARCHAR(40),
                detail TEXT,
                created_by_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at {stamp} NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at {stamp},
                completed_at {stamp}
            )
            """
        )

    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_drying_schedules_printer ON drying_schedules (printer_id)"
    )
    await conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_scheduled_dryings_status ON scheduled_dryings (status)")
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_scheduled_dryings_printer ON scheduled_dryings (printer_id)"
    )


_EVENT_TYPES = ("scheduled_drying_started", "scheduled_drying_completed", "scheduled_drying_failed")


async def seed(session_factory):
    """The three notification templates, localised to the system language as m143 does.

    Inserting English on a ``language='uk'`` install would leave English copy
    until the startup reconcile catches up, and a notification firing in that
    window would go out in the wrong language.
    """
    import json
    from pathlib import Path

    from sqlalchemy import select

    from backend.app.models.notification_template import DEFAULT_TEMPLATES, NotificationTemplate
    from backend.app.models.settings import Settings

    async with session_factory() as session:
        lang_row = await session.execute(select(Settings.value).where(Settings.key == "language"))
        lang = (lang_row.scalar_one_or_none() or "en").strip().lower()
        localised_all: dict = {}
        if lang and lang != "en":
            path = Path(__file__).resolve().parents[1] / "data" / f"notification_templates_{lang}.json"
            try:
                if path.is_file():
                    localised_all = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - a missing translation must not fail the upgrade
                logger.warning("m184: could not read %s templates", lang, exc_info=True)

        for event_type in _EVENT_TYPES:
            existing = await session.execute(
                select(NotificationTemplate.id).where(NotificationTemplate.event_type == event_type)
            )
            if existing.scalar_one_or_none() is not None:
                continue
            default = next((t for t in DEFAULT_TEMPLATES if t["event_type"] == event_type), None)
            if default is None:
                logger.warning("m184: no default template for %s — skipping seed", event_type)
                continue
            values = dict(default)
            localised = localised_all.get(event_type)
            if isinstance(localised, dict):
                values.update({k: v for k, v in localised.items() if k in ("name", "title_template", "body_template")})
            session.add(NotificationTemplate(**values))
        await session.commit()
