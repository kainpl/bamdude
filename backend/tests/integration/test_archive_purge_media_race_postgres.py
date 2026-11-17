"""Per-id trash deletion and restore on real embedded PostgreSQL (G02 F1/O1)."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_media_asset import ArchiveMediaAsset
from backend.app.services import archive_media
from backend.app.services.archive_purge import ArchivePurgeService
from backend.tests.integration.test_content_delete_protocol import wait_for_pg_lock
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


@pytest.fixture
def factory(test_engine, monkeypatch):  # noqa: F811
    event.listen(test_engine.sync_engine, "before_cursor_execute", database._strip_tz_from_params, retval=True)
    result = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", result)
    return result


async def _trashed(factory, count, *, live=0):
    ids = []
    async with factory() as db:
        for n in range(count + live):
            archive = PrintArchive(
                filename="a.3mf",
                file_path="",
                file_size=1,
                status="completed",
                deleted_at=datetime.now(timezone.utc) - timedelta(days=100) if n < count else None,
            )
            db.add(archive)
            await db.flush()
            db.add(
                ArchiveMediaAsset(
                    asset_uuid=f"asset-{archive.id}",
                    archive_id=archive.id,
                    archive_identity=archive.recovery_identity,
                    role="primary",
                    revision=1,
                    relative_path="archive/video.mp4",
                    origin="legacy",
                    created_at=datetime(2026, 1, 1),
                )
            )
            ids.append(archive.id)
        await db.commit()
    return ids


def _sweeper(monkeypatch):
    service = ArchivePurgeService()
    monkeypatch.setattr(service, "_read_retention", AsyncMock(return_value=1))
    return service


async def _state(factory):
    async with factory() as db:
        archives = {a.id: a.deleted_at for a in (await db.execute(select(PrintArchive))).scalars()}
        assets = sorted((await db.execute(select(ArchiveMediaAsset.archive_id))).scalars())
    return archives, assets


async def _restore(factory, archive_id):
    async with factory() as operator:
        row = await operator.get(PrintArchive, archive_id)
        if row is None:
            return False
        try:
            await ArchivePurgeService.restore(operator, row)
        except StaleDataError:
            await operator.rollback()
            return False
        return True


@pytest.mark.parametrize("retrash", [False, True], ids=["restore", "restore-then-trash"])
async def test_a_restore_that_wins_keeps_the_archive_and_its_video_rows(factory, monkeypatch, retrash):
    """Restore owns the row; the per-id root must reread after waiting for it."""
    (archive_id,) = await _trashed(factory, 1)
    service = _sweeper(monkeypatch)
    async with factory() as operator, factory() as sweep:
        row = await operator.scalar(select(PrintArchive).where(PrintArchive.id == archive_id).with_for_update())
        task = asyncio.create_task(service._sweep(sweep))
        try:
            await wait_for_pg_lock(factory, contender=task)
            if retrash:
                # The two state transitions finish before releasing the row lock.
                row.deleted_at = None
                await operator.flush()
                row.deleted_at = datetime.now(timezone.utc)
                await operator.commit()
            else:
                await ArchivePurgeService.restore(operator, row)
            assert await task == 0
        finally:
            await operator.rollback()
            await asyncio.gather(task, return_exceptions=True)
    archives, assets = await _state(factory)
    assert set(archives) == {archive_id}
    assert (archives[archive_id] is not None) is retrash
    assert assets == [archive_id]


async def test_a_delete_that_wins_takes_the_archive_and_its_video_rows(factory, monkeypatch):
    (archive_id,) = await _trashed(factory, 1)
    service = _sweeper(monkeypatch)
    real = archive_media.forget_archives
    late = {}

    async def restore_meanwhile(db, ids):
        assert list(ids) == [archive_id]
        late["task"] = asyncio.create_task(_restore(factory, archive_id))
        await wait_for_pg_lock(factory, contender=late["task"])
        return await real(db, ids)

    monkeypatch.setattr(archive_media, "forget_archives", restore_meanwhile)
    async with factory() as sweep:
        assert await service._sweep(sweep) == 1
    assert await late["task"] is False
    assert await _state(factory) == ({}, [])


async def test_a_mixed_batch_gives_each_archive_its_own_outcome(factory, monkeypatch):
    restored, gone_a, gone_b, live = await _trashed(factory, 3, live=1)
    service = _sweeper(monkeypatch)
    real = ArchivePurgeService.hard_delete_now
    seen = []

    async def delete_after_scan(archive_id, **kwargs):
        seen.append(archive_id)
        if archive_id == restored:
            assert await _restore(factory, archive_id)
        return await real(archive_id, **kwargs)

    monkeypatch.setattr(ArchivePurgeService, "hard_delete_now", staticmethod(delete_after_scan))
    async with factory() as sweep:
        assert await service._sweep(sweep) == 2
    assert set(seen) == {restored, gone_a, gone_b}
    assert await _state(factory) == ({restored: None, live: None}, sorted([restored, live]))


async def test_refused_per_id_delete_keeps_both_parent_and_video_rows(factory, monkeypatch):
    ids = await _trashed(factory, 1)
    service = _sweeper(monkeypatch)
    refused = AsyncMock(return_value=False)
    monkeypatch.setattr(ArchivePurgeService, "hard_delete_now", staticmethod(refused))
    async with factory() as sweep:
        assert await service._sweep(sweep) == 0
    refused.assert_awaited_once()
    assert refused.await_args.args == (ids[0],)
    assert refused.await_args.kwargs["retention_cutoff"] is not None
    archives, assets = await _state(factory)
    assert set(archives) == set(ids) and assets == ids
