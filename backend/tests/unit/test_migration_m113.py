"""m113 adds the MQTT control + lifetime-energy columns."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m113_mqtt_plug_control


@pytest.mark.asyncio
async def test_m113_adds_columns_to_existing_table(tmp_path):
    """Columns land on a table created without them (the upgrade path)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE smart_plugs (id INTEGER PRIMARY KEY, name VARCHAR(100))")
        await m113_mqtt_plug_control.upgrade(conn)
        rows = await conn.exec_driver_sql("PRAGMA table_info(smart_plugs)")
        columns = {r[1] for r in rows.fetchall()}
    await engine.dispose()

    assert {
        "mqtt_command_topic",
        "mqtt_command_on",
        "mqtt_command_off",
        "mqtt_energy_total_topic",
        "mqtt_energy_total_path",
        "mqtt_energy_total_multiplier",
    } <= columns


@pytest.mark.asyncio
async def test_m113_is_idempotent(tmp_path):
    """Re-running must not raise — DEBUG=true re-runs the latest migration."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t2.db")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE TABLE smart_plugs (id INTEGER PRIMARY KEY, name VARCHAR(100))")
        await m113_mqtt_plug_control.upgrade(conn)
        await m113_mqtt_plug_control.upgrade(conn)
    await engine.dispose()


def test_m113_version_and_name():
    assert m113_mqtt_plug_control.version == 113
    assert m113_mqtt_plug_control.name == "mqtt_plug_control"


def test_model_declares_the_columns_for_fresh_installs():
    """The second of the two places a column must exist (CLAUDE.md).

    Fresh installs get their schema from ``Base.metadata.create_all``, not from
    the migration chain, so a column added only to the migration would be
    missing on every new database. Asserted against the mapped table rather
    than by running ``create_all``: that needs the *whole* model graph imported
    (``print_queue`` has an FK to ``auto_queue_items``) and would be testing
    SQLAlchemy, not this change.
    """
    from backend.app.models.smart_plug import SmartPlug

    columns = set(SmartPlug.__table__.columns.keys())
    assert {
        "mqtt_command_topic",
        "mqtt_command_on",
        "mqtt_command_off",
        "mqtt_energy_total_topic",
        "mqtt_energy_total_path",
        "mqtt_energy_total_multiplier",
    } <= columns
