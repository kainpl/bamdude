"""The plug meter watch on PostgreSQL (spec plug-meter-watchdog r4, plan T9; A19, A26, A32).

The same bodies as ``test_plug_meter_watch.py`` on a real server, each test in a schema of its own:
the flush's token and cutoff, the episode's partial unique index under a race, the archive merge
under PostgreSQL's advisory guard, rotations and policy changes from independent sessions. Every
test runs twice -- with the session time zone UTC and Europe/Kyiv (a DST zone): the history columns
hold naive UTC, so the boundaries and totals must not move with the server's zone (A26).

Skipped unless ``DISPATCH_TEST_POSTGRES_URL`` is set; the harness entry
``test_postgres_scenarios.py::test_plug_meter_watch_on_postgres`` sets it. Never point it at a
shared database -- each test creates and drops its own schema, but a disposable server is the rule.
"""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, _strip_tz_from_params, import_all_models
from backend.tests.integration.test_plug_meter_watch import *  # noqa: F403
from backend.tests.integration.test_plug_meter_watch import _clean_buffer  # noqa: F401 -- autouse, not star-exported

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DISPATCH_TEST_POSTGRES_URL"), reason="runs only against PostgreSQL (see the docstring)"
    ),
]


def _engine(url: str, schema: str, zone: str):
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": schema, "timezone": zone, "application_name": schema}},
    )
    event.listen(engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    return engine


@pytest.fixture(params=["UTC", "Europe/Kyiv"])
async def test_engine(request, monkeypatch):
    url = os.environ["DISPATCH_TEST_POSTGRES_URL"]
    schema = "meter_" + uuid4().hex
    monkeypatch.setattr(settings, "database_url", url)
    engine = _engine(url, schema, request.param)
    import_all_models()
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr("backend.app.core.database.async_session", async_sessionmaker(engine, expire_on_commit=False))
    yield engine
    async with engine.begin() as connection:
        await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await engine.dispose()
    await asyncio.sleep(0.1)


async def _admin(url: str, sql: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://", 1), timeout=20)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def test_m211_upgrades_an_existing_postgresql_schema(monkeypatch):
    """A32 on PostgreSQL: the columns, the table, the partial index and the seed on a database from
    before m211 -- twice, as DEBUG re-runs the latest migration on every start. A database of its own,
    in its ``public`` schema, as an install has it: the helpers ask ``public`` and read the dialect
    off the configured URL."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.exc import IntegrityError

    from backend.app.migrations import m211_plug_meter_watch as m211

    base = os.environ["DISPATCH_TEST_POSTGRES_URL"]
    name = "m211_" + uuid4().hex
    url = base.rsplit("/", 1)[0] + "/" + name
    await _admin(base, f'CREATE DATABASE "{name}"')
    monkeypatch.setattr(settings, "database_url", url)
    engine = _engine(url, "public", "Europe/Kyiv")
    future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE smart_plugs (id SERIAL PRIMARY KEY, name VARCHAR(100))"))
            await conn.execute(
                text(
                    "CREATE TABLE smart_plug_power_history (id SERIAL PRIMARY KEY, plug_id INTEGER, "
                    "power FLOAT NOT NULL, recorded_at TIMESTAMP)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE smart_plug_energy_snapshots (id SERIAL PRIMARY KEY, plug_id INTEGER, "
                    "lifetime_kwh FLOAT, recorded_at TIMESTAMP)"
                )
            )
            await conn.execute(
                text(
                    "CREATE TABLE notification_templates (id SERIAL PRIMARY KEY, event_type VARCHAR(50) UNIQUE, "
                    "name VARCHAR(100), title_template TEXT, body_template TEXT, is_default BOOLEAN, "
                    "created_at TIMESTAMP DEFAULT now(), updated_at TIMESTAMP DEFAULT now())"
                )
            )
            await conn.execute(text("CREATE TABLE settings (id SERIAL PRIMARY KEY, key VARCHAR(100), value TEXT)"))
            await conn.execute(text("INSERT INTO settings (key, value) VALUES ('language', 'uk')"))
            await conn.execute(text("INSERT INTO smart_plugs (name) VALUES ('a'), ('b')"))
            await conn.execute(
                text("INSERT INTO smart_plug_power_history (plug_id, power, recorded_at) VALUES (1, 5.0, :at)"),
                {"at": future},
            )
            await m211.upgrade(conn)
            await m211.upgrade(conn)

        sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        await m211.seed(sessions)
        await m211.seed(sessions)

        async with engine.begin() as conn:
            columns = {
                r[0]
                for r in await conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public'")
                )
            }
            plugs = (
                await conn.execute(text("SELECT id, measurement_source_id, measurement_source_since FROM smart_plugs"))
            ).all()
            index = (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes "
                        "WHERE schemaname = 'public' AND indexname = 'uq_meter_incidents_active'"
                    )
                )
            ).scalar()
            templates = (
                await conn.execute(
                    text("SELECT body_template FROM notification_templates WHERE event_type = 'plug_meter_stalled'")
                )
            ).all()
        assert {
            "measurement_source_id",
            "measurement_source_since",
            "mqtt_expected_power_interval_seconds",
            "meter_min_expected_wh",
            "phantom_watts_risk",
            "meter_watch_cursor",
            "unusable_reason",
        } <= columns
        assert all(len(row[1]) == 32 for row in plugs) and plugs[0][1] != plugs[1][1]
        assert {row[0]: row[2] for row in plugs}[1] >= future  # no old row is ever the new source's
        assert "WHERE" in index and "active" in index
        assert len(templates) == 1 and "розетки" in templates[0][0]

        async with engine.begin() as conn:
            insert = text(
                "INSERT INTO smart_plug_meter_incidents (plug_id, source_id, status, opened_at, flat_since, "
                "flat_until, total_kwh, notified) VALUES (1, 'x', 'active', now(), now(), now(), 1.0, true)"
            )
            await conn.execute(insert)
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(insert)  # one active episode per plug
    finally:
        await engine.dispose()
        await _admin(base, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
