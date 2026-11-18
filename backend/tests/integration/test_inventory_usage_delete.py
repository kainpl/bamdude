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
@pytest.mark.parametrize("clear_all", [False, True])
async def test_real_runout_tail_returns_spool_weight_without_subtracting_unrelated_archive_grams(
    async_client: AsyncClient, db_session: AsyncSession, clear_all
):
    from backend.app.models.print_usage_event import PrintUsageEvent
    from backend.app.models.settings import Settings
    from backend.app.services.usage_tracker import apply_runout_zero_corrections

    spool = await _make_spool(db_session, weight_used=400, weight_used_baseline=390, low_stock_notified=True)
    archive = await _make_archive(db_session, 120)
    archive.cost = 12
    # An ordinary row may have no print_name; names cannot classify contribution.
    normal = await _add_usage(db_session, spool.id, 30, archive.id)
    db_session.add(Settings(key="runout_zero_point_enabled", value="true"))
    db_session.add(Settings(key="runout_archive_spool_enabled", value="false"))
    await db_session.commit()
    await apply_runout_zero_corrections(
        db_session,
        1,
        [
            PrintUsageEvent(
                id=1,
                printer_id=1,
                archive_id=archive.id,
                layer_num=10,
                event="runout",
                kind="autoswitch",
                global_tray_id=0,
                spool_id=spool.id,
            )
        ],
        20,
    )
    tail = await db_session.scalar(select(SpoolUsageHistory).where(SpoolUsageHistory.status == "runout"))
    assert tail is not None and tail.weight_used == 600
    assert archive.filament_used_grams == 120
    suffix = "" if clear_all else f"/{tail.id}"
    response = await async_client.delete(f"/api/v1/inventory/spools/{spool.id}/usage{suffix}")
    assert response.status_code == 200, response.text
    await db_session.refresh(archive)
    await db_session.refresh(spool)
    assert archive.filament_used_grams == (90 if clear_all else 120)
    assert archive.cost == 12
    assert spool.weight_used == (370 if clear_all else 400)
    assert spool.weight_used_baseline <= spool.weight_used
    assert spool.low_stock_notified is False
    if not clear_all:
        # The ordinary print still contributes exactly its own share.
        response = await async_client.delete(f"/api/v1/inventory/spools/{spool.id}/usage/{normal.id}")
        assert response.status_code == 200, response.text
        await db_session.refresh(archive)
        assert archive.filament_used_grams == 90


@pytest.mark.asyncio
async def test_a_service_row_linked_by_import_is_still_not_print_consumption(async_client, db_session):
    spool = await _make_spool(db_session, weight_used=200)
    archive = await _make_archive(db_session, 120)
    row = await _add_usage(db_session, spool.id, 150, archive.id)
    row.status = "ams_sync"
    row.print_name = "A name does not change its explicit correction status"
    await db_session.commit()
    response = await async_client.delete(f"/api/v1/inventory/spools/{spool.id}/usage/{row.id}")
    assert response.status_code == 200, response.text
    await db_session.refresh(archive)
    await db_session.refresh(spool)
    assert archive.filament_used_grams == 120 and spool.weight_used == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", [True, False])
async def test_cost_recalculation_uses_only_print_rows_not_lifetime_corrections(async_client, db_session, linked):
    from backend.app.models.settings import Settings
    from backend.app.services.filament_cost import SETTING_KEY

    spool = await _make_spool(db_session, weight_used=1000)
    archive = await _make_archive(db_session, 120)
    archive.print_name = "cost-by-name"
    normal = await _add_usage(db_session, spool.id, 50, archive.id if linked else None)
    normal.print_name = archive.print_name
    normal.cost = 1.25
    tail = await _add_usage(db_session, spool.id, 950, archive.id if linked else None)
    tail.print_name = archive.print_name
    tail.cost = 19
    tail.status = "runout"
    db_session.add(Settings(key=SETTING_KEY, value="20"))
    await db_session.commit()
    response = await async_client.post("/api/v1/statistics/recalculate-costs")
    assert response.status_code == 200, response.text
    await db_session.refresh(archive)
    # Only linked print rows are evidence; unlinked rows leave the 120g estimate.
    assert archive.cost == (2.65 if linked else 2.40)
    assert archive.filament_used_grams == 120


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
