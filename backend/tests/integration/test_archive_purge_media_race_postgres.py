"""The trash sweeper's fallback and a concurrent restore, on PostgreSQL (G10 C1, review C1-1).

Two real sessions. Whichever of the two wins, an archive that is alive keeps
its video rows and an archive that is gone has none.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.database import _strip_tz_from_params
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_media_asset import ArchiveMediaAsset
from backend.app.services import archive_media
from backend.app.services.archive_purge import ArchivePurgeService
from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_printer_status_batch_postgres import test_engine  # noqa: F401

pytest.importorskip("embedded_postgres")
pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


@pytest.fixture
def factory(test_engine):  # noqa: F811
    # The fixture makes its engine directly; production binds naive UTC through this listener.
    event.listen(test_engine.sync_engine, "before_cursor_execute", _strip_tz_from_params, retval=True)
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def _trashed(factory, count, *, live=0):
    """``count`` archives past retention in the trash and ``live`` archives outside it, one video row each."""
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
    # The fallback is what is under test: the per-archive delete "left the row behind".
    monkeypatch.setattr(service, "hard_delete_now", AsyncMock(return_value=False))
    return service


async def _state(factory):
    async with factory() as db:
        archives = {a.id: a.deleted_at for a in (await db.execute(select(PrintArchive))).scalars()}
        assets = sorted((await db.execute(select(ArchiveMediaAsset.archive_id))).scalars().all())
    return archives, assets


async def _restore(factory, archive_id):
    async with factory() as operator:
        row = await operator.get(PrintArchive, archive_id)
        await ArchivePurgeService.restore(operator, row)


def _restore_just_before_the_delete(sweep, factory, archive_id):
    """Commit a restore at the last possible moment: after every read, as the parent DELETE is sent.

    This is the window of review C1-1. Whatever the sweeper read about the
    archive before this statement is now stale.
    """
    real = sweep.execute
    done = []

    async def execute(statement, *args, **kwargs):
        if not done and getattr(statement, "is_delete", False) and statement.table.name == "print_archives":
            done.append(True)
            await _restore(factory, archive_id)
        return await real(statement, *args, **kwargs)

    sweep.execute = execute
    return done


async def test_a_restore_that_wins_keeps_the_archive_and_its_video_rows(factory, monkeypatch):
    """The restore commits after everything the sweeper read, immediately before its delete."""
    (archive_id,) = await _trashed(factory, 1)
    service = _sweeper(monkeypatch)
    async with factory() as sweep:
        hit = _restore_just_before_the_delete(sweep, factory, archive_id)
        await service._sweep(sweep)
    assert hit == [True]

    archives, assets = await _state(factory)
    assert archives == {archive_id: None}
    assert assets == [archive_id]


async def test_a_delete_that_wins_takes_the_archive_and_its_video_rows(factory, monkeypatch):
    """The restore starts while the delete is uncommitted, waits for it, and finds nothing to restore."""
    (archive_id,) = await _trashed(factory, 1)
    service = _sweeper(monkeypatch)
    real = archive_media.forget_archives
    late = {}

    async def restore_meanwhile(db, ids):
        assert list(ids) == [archive_id]  # exactly what the DELETE took
        # Started, not awaited: it blocks on the row the uncommitted delete holds.
        late["task"] = asyncio.create_task(_restore(factory, archive_id))
        await asyncio.sleep(0.3)
        assert not late["task"].done()
        return await real(db, ids)

    monkeypatch.setattr(archive_media, "forget_archives", restore_meanwhile)
    async with factory() as sweep:
        await service._sweep(sweep)

    with pytest.raises(Exception):  # noqa: B017, PT011 — the row is gone: nothing was restored
        await late["task"]
    archives, assets = await _state(factory)
    assert archives == {}
    assert assets == []


async def test_a_mixed_batch_gives_each_archive_its_own_outcome(factory, monkeypatch):
    """Three in the trash: one restored in time, two deleted. A live archive is no part of it."""
    restored, gone_a, gone_b, live = await _trashed(factory, 3, live=1)
    service = _sweeper(monkeypatch)
    async with factory() as sweep:
        hit = _restore_just_before_the_delete(sweep, factory, restored)
        await service._sweep(sweep)
    assert hit == [True]

    archives, assets = await _state(factory)
    assert archives == {restored: None, live: None}
    assert assets == sorted([restored, live])
    assert gone_a not in archives and gone_b not in archives
