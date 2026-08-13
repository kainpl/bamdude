"""m134 rewrites the pre-collapse chamber-light rows into the new grammar.

The action id used to carry its value (``chamber_light_off``); it now carries
only the command, with the value in ``mqtt_action_param``. Rows written before
that change have to be moved over, and — because ``DEBUG=true`` re-runs the
latest migration on every startup — moving them twice must be a no-op.
"""

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from backend.app.migrations import m134_macro_action_param_and_layer as m134


class _Factory:
    """Async-session factory shim: the migration takes one, not an engine."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self._session


class _Session:
    """Just enough AsyncSession for m134: execute + commit, no ORM."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, *args, **kwargs):
        return await self._conn.execute(*args, **kwargs)

    async def commit(self):
        pass  # the caller's engine.begin() owns the transaction


# The macros table as it stood BEFORE m134 — no mqtt_action_param, no
# trigger_layer. Building it from today's model would hand the migration a
# database that already has its work done, and every assertion below would
# pass without the migration doing anything.
_MACROS_BEFORE_M134 = """
CREATE TABLE macros (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    description TEXT,
    printer_models TEXT,
    swap_mode_only BOOLEAN,
    swap_profile VARCHAR(50),
    event VARCHAR(50) NOT NULL,
    action_type VARCHAR(20) NOT NULL DEFAULT 'gcode',
    mqtt_action VARCHAR(50),
    delay_seconds INTEGER NOT NULL DEFAULT 0,
    gcode TEXT,
    is_custom BOOLEAN,
    enabled BOOLEAN
)
"""


async def _prepared(path, rows: list[tuple[str, str | None, str]]):
    """A pre-m134 database carrying *rows* of ``(name, mqtt_action, action_type)``."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_MACROS_BEFORE_M134)
        for name, action, action_type in rows:
            await conn.exec_driver_sql(
                "INSERT INTO macros (name, printer_models, swap_mode_only, event, action_type, "
                "mqtt_action, delay_seconds, gcode, is_custom, enabled) "
                "VALUES (?, '[\"*\"]', 0, ?, ?, ?, 0, '', 1, 1)",
                (name, "print_started", action_type, action),
            )
    return engine


async def _rows(engine) -> list[tuple]:
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT name, mqtt_action, mqtt_action_param FROM macros ORDER BY name"))
        return [tuple(r) for r in result]


async def _upgrade_and_seed(engine) -> None:
    async with engine.begin() as conn:
        await m134.upgrade(conn)
        await m134.seed(_Factory(_Session(conn)))


def test_the_migration_declares_its_version_and_name():
    assert m134.version == 134
    assert m134.name == "macro_action_param_and_layer"


@pytest.mark.asyncio
async def test_the_prepared_database_really_lacks_the_columns(tmp_path):
    """A guard on the guard."""
    engine = await _prepared(tmp_path / "guard.db", [])
    async with engine.begin() as conn:
        found = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns("macros")})
    assert not ({"mqtt_action_param", "trigger_layer"} & found), found
    await engine.dispose()


@pytest.mark.asyncio
async def test_it_adds_both_columns(tmp_path):
    engine = await _prepared(tmp_path / "a.db", [])
    async with engine.begin() as conn:
        await m134.upgrade(conn)
        found = await conn.run_sync(lambda c: {x["name"] for x in inspect(c).get_columns("macros")})
    assert {"mqtt_action_param", "trigger_layer"} <= found
    await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_is_idempotent(tmp_path):
    """DEBUG=true re-runs the newest migration on every start."""
    engine = await _prepared(tmp_path / "b.db", [])
    async with engine.begin() as conn:
        await m134.upgrade(conn)
        await m134.upgrade(conn)
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_old_light_ids_are_rewritten(tmp_path):
    engine = await _prepared(
        tmp_path / "c.db",
        [
            ("lights off", "chamber_light_off", "mqtt_action"),
            ("lights on", "chamber_light_on", "mqtt_action"),
        ],
    )
    await _upgrade_and_seed(engine)

    assert await _rows(engine) == [
        ("lights off", "chamber_light", "off"),
        ("lights on", "chamber_light", "on"),
    ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_running_the_seed_twice_changes_nothing(tmp_path):
    engine = await _prepared(tmp_path / "d.db", [("lights off", "chamber_light_off", "mqtt_action")])
    await _upgrade_and_seed(engine)
    first = await _rows(engine)

    async with engine.begin() as conn:
        await m134.seed(_Factory(_Session(conn)))

    assert await _rows(engine) == first
    await engine.dispose()


@pytest.mark.asyncio
async def test_a_gcode_macro_is_left_alone(tmp_path):
    engine = await _prepared(tmp_path / "e.db", [("swap", None, "gcode")])
    await _upgrade_and_seed(engine)

    assert await _rows(engine) == [("swap", None, None)]
    await engine.dispose()
