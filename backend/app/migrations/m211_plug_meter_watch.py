"""Plug meter watch: measurement sources, unusable history moments and stall incidents.

Spec plug-meter-watchdog r4 (vault 60-specs). A plug's lifetime counter can stand still while the plug
keeps reporting watts; per-print energy then reads 0 for prints that drew hundreds of watt-hours.

* ``smart_plugs.measurement_source_id`` / ``measurement_source_since`` -- a random token for one
  measurement configuration and the strict cutoff after which history belongs to it (W2). Seeded
  here for every plug with cutoff = max(now, its newest power row, its newest counter snapshot), so
  no history from before this migration ever feeds a new estimate. The big history tables get NO
  source column: producers capture the token, the flush drops a fact of another token, readers read
  only after the cutoff.
* ``mqtt_expected_power_interval_seconds`` (W4: the gap limit of an MQTT source), ``meter_min_expected_wh``
  (W5: a coarse counter's own threshold), ``phantom_watts_risk`` (W3: an unknown relay state makes a
  reading unusable; NULL = the type's default, Zigbee yes) and ``meter_watch_cursor`` (the last counter
  point the sweep evaluated).
* ``smart_plug_power_history.unusable_reason`` -- a received reading found unusable (O16). NULL is a
  usable row; old rows stay NULL.
* ``smart_plug_meter_incidents`` -- one stall episode per row; the partial unique index allows one
  active episode per plug.
* the ``plug_meter_stalled`` notification template (W7, in the system language), for databases that
  predate it; a fresh install gets it from m001's seed of ``DEFAULT_TEMPLATES``.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from backend.app.migrations.helpers import add_column, table_exists

version = 211
name = "plug_meter_watch"


async def upgrade(conn):
    sqlite = conn.dialect.name == "sqlite"
    pk = "INTEGER PRIMARY KEY" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"
    json_type = "TEXT" if sqlite else "JSON"
    false = "0" if sqlite else "FALSE"

    await add_column(conn, "smart_plugs", "measurement_source_id VARCHAR(32)")
    await add_column(conn, "smart_plugs", f"measurement_source_since {ts}")
    await add_column(conn, "smart_plugs", "mqtt_expected_power_interval_seconds INTEGER")
    await add_column(conn, "smart_plugs", "meter_min_expected_wh FLOAT")
    await add_column(conn, "smart_plugs", "phantom_watts_risk BOOLEAN")
    await add_column(conn, "smart_plugs", f"meter_watch_cursor {ts}")
    await add_column(conn, "smart_plug_power_history", "unusable_reason VARCHAR(32)")

    if not await table_exists(conn, "smart_plug_meter_incidents"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE smart_plug_meter_incidents (
                id {pk},
                plug_id INTEGER NOT NULL REFERENCES smart_plugs(id) ON DELETE CASCADE,
                source_id VARCHAR(32) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'active',
                opened_at {ts} NOT NULL,
                flat_since {ts} NOT NULL,
                flat_until {ts} NOT NULL,
                total_kwh FLOAT NOT NULL,
                evidence {json_type},
                notified BOOLEAN NOT NULL DEFAULT {false},
                closed_at {ts},
                close_reason VARCHAR(32),
                CONSTRAINT ck_meter_incidents_status CHECK (status IN ('active', 'closed'))
            )
            """
        )
    await conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_meter_incidents_active "
        "ON smart_plug_meter_incidents (plug_id) WHERE status = 'active'"
    )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_meter_incidents_plug ON smart_plug_meter_incidents (plug_id, opened_at)"
    )


def _naive(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


async def seed(session_factory):
    """A token and a strict cutoff for every plug that has none (W2: the migration's own rule)."""
    await _seed_template(session_factory)
    async with session_factory() as db:
        plugs = (await db.execute(text("SELECT id FROM smart_plugs WHERE measurement_source_id IS NULL"))).all()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for (plug_id,) in plugs:
            newest_power = (
                await db.execute(
                    text("SELECT MAX(recorded_at) FROM smart_plug_power_history WHERE plug_id = :p"), {"p": plug_id}
                )
            ).scalar()
            newest_snapshot = (
                await db.execute(
                    text("SELECT MAX(recorded_at) FROM smart_plug_energy_snapshots WHERE plug_id = :p"),
                    {"p": plug_id},
                )
            ).scalar()
            cutoff = max(t for t in (now, _naive(newest_power), _naive(newest_snapshot)) if t is not None)
            await db.execute(
                text(
                    "UPDATE smart_plugs SET measurement_source_id = :token, measurement_source_since = :since "
                    "WHERE id = :p"
                ),
                {"token": uuid.uuid4().hex, "since": cutoff, "p": plug_id},
            )
        await db.commit()


_EVENT = "plug_meter_stalled"


async def _seed_template(session_factory) -> None:
    """The stall warning's template, by named columns, never twice."""
    from backend.app.models.notification_template import DEFAULT_TEMPLATES

    async with session_factory() as db:
        present = (
            await db.execute(text("SELECT 1 FROM notification_templates WHERE event_type = :e"), {"e": _EVENT})
        ).first()
        if present is not None:
            return
        lang = (await db.execute(text("SELECT value FROM settings WHERE key = 'language'"))).scalar()
        data = next(t for t in DEFAULT_TEMPLATES if t["event_type"] == _EVENT)
        if lang == "uk":
            path = Path(__file__).parent.parent / "data" / "notification_templates_uk.json"
            data = json.loads(path.read_text(encoding="utf-8")).get(_EVENT, data)
        await db.execute(
            text(
                "INSERT INTO notification_templates (event_type, name, title_template, body_template, is_default) "
                "VALUES (:e, :n, :t, :b, :d)"
            ),
            {"e": _EVENT, "n": data["name"], "t": data["title_template"], "b": data["body_template"], "d": True},
        )
        await db.commit()
