"""Bulk-update applies only the sent fields across many spools.

Usage / per-physical-spool identity columns (consumed weight, RFID UID) are
stripped server-side regardless of what the client sends — bulk edit must never
touch them. Fields the caller didn't send are left untouched per spool.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool


async def _make_spool(db: AsyncSession, **kwargs) -> Spool:
    defaults = {"material": "PLA", "color_name": "Red", "rgba": "FF0000FF", "label_weight": 1000, "weight_used": 0}
    defaults.update(kwargs)
    spool = Spool(**defaults)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


@pytest.mark.asyncio
async def test_bulk_update_applies_only_sent_fields(async_client: AsyncClient, db_session: AsyncSession):
    a = await _make_spool(db_session, brand="Old", category="x", weight_used=100.0, subtype="Matte")
    b = await _make_spool(db_session, brand="Old", category="y", weight_used=250.0, subtype="Silk")

    resp = await async_client.patch(
        "/api/v1/inventory/spools/bulk-update",
        json={"spool_ids": [a.id, b.id], "fields": {"brand": "NewBrand", "color_name": "Blue"}},
    )
    assert resp.status_code == 200, resp.text

    for sid, prev_used, prev_sub, prev_cat in ((a.id, 100.0, "Matte", "x"), (b.id, 250.0, "Silk", "y")):
        s = (await db_session.execute(select(Spool).where(Spool.id == sid))).scalar_one()
        await db_session.refresh(s)
        assert s.brand == "NewBrand"  # sent → applied to both
        assert s.color_name == "Blue"
        assert s.weight_used == prev_used  # usage untouched
        assert s.subtype == prev_sub  # not sent → untouched
        assert s.category == prev_cat  # not sent → untouched


@pytest.mark.asyncio
async def test_bulk_update_ignores_usage_and_identity_fields(async_client: AsyncClient, db_session: AsyncSession):
    a = await _make_spool(db_session, weight_used=100.0)

    resp = await async_client.patch(
        "/api/v1/inventory/spools/bulk-update",
        json={
            "spool_ids": [a.id],
            # All of these must be ignored; brand is the only legit change.
            "fields": {"brand": "X", "weight_used": 999, "tag_uid": "AABBCC", "weight_locked": True},
        },
    )
    assert resp.status_code == 200, resp.text

    s = (await db_session.execute(select(Spool).where(Spool.id == a.id))).scalar_one()
    await db_session.refresh(s)
    assert s.brand == "X"
    assert s.weight_used == 100.0  # never bulk-set
    assert s.tag_uid is None  # RFID UID never copied across spools


@pytest.mark.asyncio
async def test_bulk_update_rejects_empty_field_set(async_client: AsyncClient, db_session: AsyncSession):
    a = await _make_spool(db_session)
    # Only protected fields → nothing editable left → 400.
    resp = await async_client.patch(
        "/api/v1/inventory/spools/bulk-update",
        json={"spool_ids": [a.id], "fields": {"weight_used": 5}},
    )
    assert resp.status_code == 400
