"""m183 — RFID-added spools that took the wrong catalogue tare get the Low Temp one (upstream #2909).

The lookup that gave an auto-added spool its ``core_weight`` took the first
"Bambu Lab%" catalogue row the database returned — 216 g High Temp on SQLite —
while a Bambu roll ships on the 250 g Low Temp spool. The seed corrects the
rows that bug wrote and nothing else.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations import m183_rfid_core_weight_repair as m183
from backend.app.models.spool import Spool
from backend.app.models.spool_catalog import SpoolCatalogEntry

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _catalog(db) -> dict[str, int]:
    ids = {}
    for name, weight in (
        ("Bambu Lab - Plastic High Temp", 216),
        ("Bambu Lab - Plastic Low Temp", 250),
        ("Bambu Lab - Plastic White", 253),
        ("Generic Cardboard", 180),
    ):
        entry = SpoolCatalogEntry(name=name, weight=weight, is_default=True)
        db.add(entry)
        await db.flush()
        ids[name] = entry.id
    return ids


def _spool(**fields) -> Spool:
    base = {"material": "PLA", "brand": "Bambu Lab", "label_weight": 1000, "weight_used": 100.0}
    base.update(fields)
    return Spool(**base)


async def test_repairs_only_the_rows_the_lookup_got_wrong(test_engine, db_session):
    ids = await _catalog(db_session)
    wrong_high = _spool(data_origin="rfid_auto", core_weight=216)
    wrong_white = _spool(data_origin="rfid_auto", core_weight=253)
    right = _spool(data_origin="rfid_auto", core_weight=250)
    manual = _spool(data_origin="manual", core_weight=216)
    cardboard = _spool(data_origin="rfid_auto", core_weight=180)
    db_session.add_all([wrong_high, wrong_white, right, manual, cardboard])
    await db_session.commit()

    await m183.seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))

    rows = {
        s.id: s for s in (await db_session.execute(select(Spool).execution_options(populate_existing=True))).scalars()
    }
    low_temp = ids["Bambu Lab - Plastic Low Temp"]
    assert (rows[wrong_high.id].core_weight, rows[wrong_high.id].core_weight_catalog_id) == (250, low_temp)
    assert (rows[wrong_white.id].core_weight, rows[wrong_white.id].core_weight_catalog_id) == (250, low_temp)
    # Never weighed: weight_used came from the AMS remain %, which the tare never touched.
    assert rows[wrong_high.id].weight_used == 100.0
    assert rows[right.id].core_weight == 250
    assert rows[manual.id].core_weight == 216
    assert rows[cardboard.id].core_weight == 180


async def test_a_weighed_spool_gets_the_tare_error_back_in_its_used_weight(test_engine, db_session):
    """A scale reading minus a 34 g-light tare credited 34 g of filament that
    was not there; the error is a constant, so adding it back is exact."""
    await _catalog(db_session)
    weighed = _spool(
        data_origin="rfid_auto", core_weight=216, weight_used=100.0, last_weighed_at=datetime.now(timezone.utc)
    )
    nearly_empty = _spool(
        data_origin="rfid_auto", core_weight=216, weight_used=990.0, last_weighed_at=datetime.now(timezone.utc)
    )
    db_session.add_all([weighed, nearly_empty])
    await db_session.commit()

    await m183.seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))

    rows = {
        s.id: s for s in (await db_session.execute(select(Spool).execution_options(populate_existing=True))).scalars()
    }
    assert rows[weighed.id].weight_used == 134.0
    assert rows[nearly_empty.id].weight_used == 1000.0  # clamped to the label weight


async def test_without_the_low_temp_row_the_documented_default_applies(test_engine, db_session):
    db_session.add(SpoolCatalogEntry(name="Bambu Lab - Plastic High Temp", weight=216, is_default=True))
    spool = _spool(data_origin="rfid_auto", core_weight=216)
    db_session.add(spool)
    await db_session.commit()

    await m183.seed(async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False))

    await db_session.refresh(spool)
    assert (spool.core_weight, spool.core_weight_catalog_id) == (250, None)
