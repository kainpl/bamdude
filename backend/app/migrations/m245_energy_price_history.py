"""The electricity price gets a history (upstream #1251, adapted).

Energy used to be costed at one price: a print at the price read when it ended, the plugs' totals in
the statistics at whatever the price was on the day somebody looked. Changing the price re-costed
every past month of the "at the plug" figures, and a print that ran through a tariff change was
costed whole at the tariff of its last minute.

``energy_price_history`` keeps every change of ``energy_cost_per_kwh`` with the moment it took
effect; the price of a moment is the newest row at or before it. Only ``services/energy_price.py``
writes the table, and rows are only appended.

The seed writes ONE ``baseline`` row: the price stored in the settings when this migration runs
(the settings default when no row is stored), effective far in the past -- so every moment before
the upgrade is costed at the price that stood on the upgrade day (owner's decision: the earlier
prices were never recorded and are not guessed). Print figures already settled keep their stored
cost; nothing is re-costed.

Tables and columns are named here: an ORM model would bring today's columns into a step of the
chain where they may not exist.
"""

import logging
import math
from datetime import datetime

from backend.app.migrations.helpers import table_exists

logger = logging.getLogger(__name__)

version = 245
name = "energy_price_history"

#: Before any reading this installation can hold.
BASELINE_AT = datetime(1970, 1, 1)
#: ``AppSettings.energy_cost_per_kwh``'s default, frozen here as it stood when this migration shipped.
DEFAULT_PRICE = 0.15


async def upgrade(conn):
    sqlite = conn.dialect.name == "sqlite"
    pk = "INTEGER PRIMARY KEY" if sqlite else "SERIAL PRIMARY KEY"
    ts = "DATETIME" if sqlite else "TIMESTAMP"

    if not await table_exists(conn, "energy_price_history"):
        await conn.exec_driver_sql(
            f"""
            CREATE TABLE energy_price_history (
                id {pk},
                effective_at {ts} NOT NULL,
                price_per_kwh FLOAT NOT NULL,
                source VARCHAR(16) NOT NULL,
                CONSTRAINT ck_energy_price_history_price CHECK (price_per_kwh >= 0),
                CONSTRAINT ck_energy_price_history_source
                    CHECK (source IN ('baseline', 'manual', 'api', 'homeassistant', 'restore'))
            )
            """
        )
    await conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_energy_price_history_effective_at ON energy_price_history (effective_at)"
    )


def _price(raw) -> float:
    """The stored price as a number; an unreadable or negative row reads as the default."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_PRICE
    return value if math.isfinite(value) and value >= 0 else DEFAULT_PRICE


async def seed(session_factory):
    """One baseline row, never twice: a table that already holds a row is left alone."""
    from sqlalchemy import DateTime, Float, Integer, String, column, func, insert, select, table

    history = table(
        "energy_price_history",
        column("id", Integer),
        column("effective_at", DateTime),
        column("price_per_kwh", Float),
        column("source", String),
    )
    settings = table("settings", column("key", String), column("value", String))

    async with session_factory() as db:
        if await db.scalar(select(func.count()).select_from(history)):
            return
        stored = await db.scalar(select(settings.c.value).where(settings.c.key == "energy_cost_per_kwh"))
        price = _price(stored) if stored is not None else DEFAULT_PRICE
        await db.execute(insert(history).values(effective_at=BASELINE_AT, price_per_kwh=price, source="baseline"))
        await db.commit()
    logger.info("m245: electricity price history starts at %s per kWh", price)
