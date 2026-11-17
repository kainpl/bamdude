"""Trash candidates can be restored before their owning per-id delete starts."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.core.credential_config import initialize_credential_config_anchor
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_media_asset import ArchiveMediaAsset
from backend.app.services.archive import ArchiveService
from backend.app.services.archive_purge import ArchivePurgeService
from backend.app.utils.archive_paths import fallback_dir_for
from backend.tests.integration.test_content_delete_protocol import independent_factory  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def test_engine(independent_factory):
    engine = independent_factory.kw["bind"]
    async with engine.begin() as conn:
        await initialize_credential_config_anchor(conn)
    return engine


@pytest.mark.parametrize("door", ["single", "empty", "sweep"])
async def test_restore_after_trash_scan_keeps_archive_video_row_and_bytes(
    async_client, db_session, independent_factory, monkeypatch, door
):
    archive = PrintArchive(
        filename="restored.3mf",
        file_path="",
        file_size=0,
        status="completed",
        deleted_at=datetime.now() - timedelta(days=100),
    )
    db_session.add(archive)
    await db_session.flush()
    archive_id = archive.id
    video = fallback_dir_for(archive_id) / f"restore-race-{uuid4().hex}.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"restored video must survive")
    db_session.add(
        ArchiveMediaAsset(
            asset_uuid=str(uuid4()),
            archive_id=archive_id,
            archive_identity=archive.recovery_identity,
            role="primary",
            revision=1,
            relative_path=str(video),
            origin="legacy",
            created_at=datetime(2026, 1, 1),
        )
    )
    await db_session.commit()
    real = ArchivePurgeService.hard_delete_now
    observed = []

    async def restore_after_scan(selected_id):
        observed.append(selected_id)
        async with independent_factory() as operator:
            current = await operator.get(PrintArchive, selected_id)
            await ArchivePurgeService.restore(operator, current)
        return await real(selected_id)

    monkeypatch.setattr(ArchivePurgeService, "hard_delete_now", staticmethod(restore_after_scan))
    try:
        if door == "sweep":
            assert await ArchivePurgeService()._sweep(db_session) == 0
        else:
            path = "/api/v1/archives/trash" + (f"/{archive_id}" if door == "single" else "")
            response = await async_client.delete(path)
            assert response.status_code == (404 if door == "single" else 200), response.text
            if door == "empty":
                assert response.json() == {"deleted": 0, "retained": []}
        assert observed == [archive_id]
        async with independent_factory() as check:
            current = await check.get(PrintArchive, archive_id)
            assert current is not None and current.deleted_at is None
            assert (
                await check.scalar(select(ArchiveMediaAsset.id).where(ArchiveMediaAsset.archive_id == archive_id))
                is not None
            )
        assert video.read_bytes() == b"restored video must survive"
    finally:
        video.unlink(missing_ok=True)


async def test_trash_guard_does_not_change_ordinary_archive_deletion(db_session):
    archive = PrintArchive(filename="active-list.3mf", file_path="", file_size=0, status="completed")
    db_session.add(archive)
    await db_session.commit()
    archive_id = archive.id
    assert await ArchivePurgeService.hard_delete_now(archive_id) is False
    assert await ArchiveService(db_session).delete_archive(archive_id) is True
