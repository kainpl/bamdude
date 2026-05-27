"""Per-row usage-history delete returns the row's weight to the spool.

Deleting a single ``spool_usage_history`` row subtracts its ``weight_used`` from
``spool.weight_used`` (clamped at 0), so the filament it recorded counts as
unused again and remaining weight goes back up. Mismatched / missing ids 404.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.archive import PrintArchive
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory


async def _make_spool(db: AsyncSession, **kwargs) -> Spool:
    defaults = {"material": "PLA", "color_name": "Red", "rgba": "FF0000FF", "label_weight": 1000, "weight_used": 0}
    defaults.update(kwargs)
    spool = Spool(**defaults)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


async def _add_usage(
    db: AsyncSession, spool_id: int, weight: float, archive_id: int | None = None
) -> SpoolUsageHistory:
    row = SpoolUsageHistory(
        spool_id=spool_id, weight_used=weight, percent_used=0, status="completed", archive_id=archive_id
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _make_archive(db: AsyncSession, grams: float) -> PrintArchive:
    archive = PrintArchive(
        printer_id=None,
        filename="t.gcode.3mf",
        file_path="archives/t/t.gcode.3mf",
        file_size=1,
        status="completed",
        filament_used_grams=grams,
    )
    db.add(archive)
    await db.commit()
    await db.refresh(archive)
    return archive


@pytest.mark.asyncio
async def test_delete_usage_row_returns_weight(async_client: AsyncClient, db_session: AsyncSession):
    spool = await _make_spool(db_session, weight_used=300.0)
    row1 = await _add_usage(db_session, spool.id, 100.0)
    row2 = await _add_usage(db_session, spool.id, 50.0)

    resp = await async_client.delete(f"/api/v1/inventory/spools/{spool.id}/usage/{row1.id}")
    assert resp.status_code == 200
    assert resp.json()["weight_used"] == 200.0  # 300 - 100 returned

    # Row gone, the other one survives.
    remaining = (
        (await db_session.execute(select(SpoolUsageHistory).where(SpoolUsageHistory.spool_id == spool.id)))
        .scalars()
        .all()
    )
    assert [r.id for r in remaining] == [row2.id]

    # Deleting the second returns its weight too.
    resp = await async_client.delete(f"/api/v1/inventory/spools/{spool.id}/usage/{row2.id}")
    assert resp.status_code == 200
    assert resp.json()["weight_used"] == 150.0


@pytest.mark.asyncio
async def test_delete_usage_row_clamps_at_zero(async_client: AsyncClient, db_session: AsyncSession):
    # Counter lower than the row weight (e.g. after a manual edit) → never negative.
    spool = await _make_spool(db_session, weight_used=30.0)
    row = await _add_usage(db_session, spool.id, 100.0)

    resp = await async_client.delete(f"/api/v1/inventory/spools/{spool.id}/usage/{row.id}")
    assert resp.status_code == 200
    assert resp.json()["weight_used"] == 0.0


@pytest.mark.asyncio
async def test_delete_usage_row_baseline_follows_down(async_client: AsyncClient, db_session: AsyncSession):
    # baseline must stay <= weight_used so "consumed since reset" display is sane.
    spool = await _make_spool(db_session, weight_used=300.0, weight_used_baseline=300.0)
    row = await _add_usage(db_session, spool.id, 100.0)

    resp = await async_client.delete(f"/api/v1/inventory/spools/{spool.id}/usage/{row.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["weight_used"] == 200.0
    assert body["weight_used_baseline"] == 200.0


@pytest.mark.asyncio
async def test_delete_usage_row_decrements_linked_archive(async_client: AsyncClient, db_session: AsyncSession):
    # Single-colour print: one usage row ↔ one archive → archive lands at 0.
    archive = await _make_archive(db_session, grams=100.0)
    spool = await _make_spool(db_session, weight_used=100.0)
    row = await _add_usage(db_session, spool.id, 100.0, archive_id=archive.id)

    resp = await async_client.delete(f"/api/v1/inventory/spools/{spool.id}/usage/{row.id}")
    assert resp.status_code == 200

    await db_session.refresh(archive)
    assert archive.filament_used_grams == 0.0


@pytest.mark.asyncio
async def test_delete_one_row_of_multicolor_print_subtracts_only_its_share(
    async_client: AsyncClient, db_session: AsyncSession
):
    # Multi-colour print: two usage rows (different spools) share one archive
    # (300g). Removing the 100g slot must leave the archive at 200g — matching
    # the surviving 200g usage row — not zero it.
    archive = await _make_archive(db_session, grams=300.0)
    spool_a = await _make_spool(db_session, weight_used=100.0)
    spool_b = await _make_spool(db_session, weight_used=200.0)
    row_a = await _add_usage(db_session, spool_a.id, 100.0, archive_id=archive.id)
    await _add_usage(db_session, spool_b.id, 200.0, archive_id=archive.id)

    resp = await async_client.delete(f"/api/v1/inventory/spools/{spool_a.id}/usage/{row_a.id}")
    assert resp.status_code == 200

    await db_session.refresh(archive)
    assert archive.filament_used_grams == 200.0


@pytest.mark.asyncio
async def test_clear_all_returns_weight_and_decrements_archives(async_client: AsyncClient, db_session: AsyncSession):
    arch1 = await _make_archive(db_session, grams=80.0)
    arch2 = await _make_archive(db_session, grams=50.0)
    spool = await _make_spool(db_session, weight_used=130.0, weight_used_baseline=130.0)
    await _add_usage(db_session, spool.id, 80.0, archive_id=arch1.id)
    await _add_usage(db_session, spool.id, 50.0, archive_id=arch2.id)

    resp = await async_client.delete(f"/api/v1/inventory/spools/{spool.id}/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["weight_used"] == 0.0  # 130 - (80 + 50)
    assert body["weight_used_baseline"] == 0.0  # baseline followed down

    # Both linked archives drop by their respective shares.
    await db_session.refresh(arch1)
    await db_session.refresh(arch2)
    assert arch1.filament_used_grams == 0.0
    assert arch2.filament_used_grams == 0.0

    # History is empty afterwards.
    rows = (
        (await db_session.execute(select(SpoolUsageHistory).where(SpoolUsageHistory.spool_id == spool.id)))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_delete_usage_row_wrong_spool_404(async_client: AsyncClient, db_session: AsyncSession):
    spool_a = await _make_spool(db_session, weight_used=100.0)
    spool_b = await _make_spool(db_session, weight_used=100.0)
    row = await _add_usage(db_session, spool_a.id, 40.0)

    # Row belongs to spool_a, not spool_b → 404, and spool_a is untouched.
    resp = await async_client.delete(f"/api/v1/inventory/spools/{spool_b.id}/usage/{row.id}")
    assert resp.status_code == 404

    resp = await async_client.delete(f"/api/v1/inventory/spools/{spool_a.id}/usage/999999")
    assert resp.status_code == 404
