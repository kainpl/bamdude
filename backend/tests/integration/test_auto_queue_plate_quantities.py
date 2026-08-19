"""One quantity cannot say "plate 1 once, plate 2 twice".

A multi-plate file is exactly where different counts per plate come up — one
plate of brackets, three of the clip that keeps breaking. Until now the only
answer was to queue each plate as its own submission and keep the counts in
your head (upstream #342).

⚠️ Only the **auto-queue** route needed changing. The per-printer tier already
receives one request per plate from the dialog, so a different quantity per
plate is simply a different number in each of those requests. Adding a
per-plate map there too would be a second way to say the same thing.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from backend.app.models.auto_queue import AutoQueueItem


async def _archive(db_session):
    from backend.app.models.archive import PrintArchive

    archive = PrintArchive(
        filename="three_plates.3mf",
        print_name="Three Plates",
        file_path="/tmp/three_plates.3mf",
        file_size=1024,
        content_hash="plate_qty_hash_0001",
        status="completed",
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


async def _rows(db_session):
    result = await db_session.execute(select(AutoQueueItem).order_by(AutoQueueItem.position))
    return result.scalars().all()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_each_plate_gets_the_count_it_was_given(async_client: AsyncClient, db_session):
    archive = await _archive(db_session)

    response = await async_client.post(
        "/api/v1/auto-queue/",
        json={"archive_id": archive.id, "plate_ids": [1, 2, 3], "plate_quantities": {"1": 1, "2": 2, "3": 3}},
    )
    assert response.status_code == 200, response.text

    counts = {}
    for row in await _rows(db_session):
        counts[row.plate_id] = counts.get(row.plate_id, 0) + 1
    assert counts == {1: 1, 2: 2, 3: 3}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_plate_left_out_of_the_map_falls_back_to_the_shared_quantity(async_client: AsyncClient, db_session):
    """Partial maps are the normal case: the operator changes one plate."""
    archive = await _archive(db_session)

    response = await async_client.post(
        "/api/v1/auto-queue/",
        json={"archive_id": archive.id, "plate_ids": [1, 2], "quantity": 2, "plate_quantities": {"2": 5}},
    )
    assert response.status_code == 200, response.text

    counts = {}
    for row in await _rows(db_session):
        counts[row.plate_id] = counts.get(row.plate_id, 0) + 1
    assert counts == {1: 2, 2: 5}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_without_the_map_nothing_changes(async_client: AsyncClient, db_session):
    """Every caller that predates this keeps its meaning."""
    archive = await _archive(db_session)

    response = await async_client.post(
        "/api/v1/auto-queue/",
        json={"archive_id": archive.id, "plate_ids": [1, 2], "quantity": 3},
    )
    assert response.status_code == 200, response.text

    assert len(await _rows(db_session)) == 6


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_rows_still_share_one_batch(async_client: AsyncClient, db_session):
    """A batch is "created together", so the TOTAL decides — not whether any
    single plate asked for more than one."""
    archive = await _archive(db_session)

    await async_client.post(
        "/api/v1/auto-queue/",
        json={"archive_id": archive.id, "plate_ids": [1, 2], "plate_quantities": {"1": 1, "2": 1}},
    )

    batch_ids = {row.batch_id for row in await _rows(db_session)}
    assert len(batch_ids) == 1 and None not in batch_ids


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_single_run_is_not_a_batch(async_client: AsyncClient, db_session):
    archive = await _archive(db_session)

    await async_client.post(
        "/api/v1/auto-queue/",
        json={"archive_id": archive.id, "plate_ids": [1], "plate_quantities": {"1": 1}},
    )

    assert [row.batch_id for row in await _rows(db_session)] == [None]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_absurd_count_is_refused_rather_than_clamped(async_client: AsyncClient, db_session):
    """Asking for 200 copies is a mistake, and silently giving 50 hides it."""
    archive = await _archive(db_session)

    response = await async_client.post(
        "/api/v1/auto-queue/",
        json={"archive_id": archive.id, "plate_ids": [1], "plate_quantities": {"1": 200}},
    )

    assert response.status_code == 422
    assert (await db_session.execute(select(func.count()).select_from(AutoQueueItem))).scalar() == 0
