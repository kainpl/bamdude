"""A stitched video is attached as a FILE, never read into memory (G10 F, M27)."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from backend.app.models.archive_media_asset import ArchiveMediaAsset

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def target(db_session, printer_factory, archive_factory, monkeypatch, tmp_path):
    from backend.app.core.config import settings
    from backend.app.services.archive import ArchiveService

    monkeypatch.setattr(settings, "base_dir", tmp_path)
    printer = await printer_factory()
    archive = await archive_factory(printer.id, file_path="archives/p/print.gcode.3mf")
    (tmp_path / "archives" / "p").mkdir(parents=True, exist_ok=True)
    return ArchiveService(db_session), archive, tmp_path


async def test_the_file_is_moved_in_hashed_in_chunks_and_published(target, db_session, monkeypatch):
    service, archive, root = target
    archive_id = archive.id
    payload = b"stitched-video" * 200_000  # ~2.8 MB: more than one hashing chunk
    source = root / "timelapse_frames" / "1" / "timelapse_x.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)

    # The whole video is never handed to ``write_bytes`` or read back in one piece.
    from pathlib import Path

    def refuse(self, data):
        raise AssertionError("a stitched video must not be written from memory")

    with monkeypatch.context() as patched:
        patched.setattr(Path, "write_bytes", refuse)
        attached = await service.attach_timelapse_file(archive_id, source, "layer_timelapse.mp4", origin="layer")

    assert attached is True
    assert not source.exists()  # moved, not copied and left behind
    row = (
        await db_session.execute(select(ArchiveMediaAsset).where(ArchiveMediaAsset.archive_id == archive_id))
    ).scalar_one()
    published = root / row.relative_path
    assert published.read_bytes() == payload
    assert row.content_sha256 == hashlib.sha256(payload).hexdigest() and row.size_bytes == len(payload)
    assert row.origin == "layer"


async def test_a_refused_attach_removes_its_input_and_publishes_nothing(target, db_session):
    service, archive, root = target
    source = root / "timelapse_frames" / "1" / "timelapse_y.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")

    attached = await service.attach_timelapse_file(
        archive.id, source, "layer_timelapse.mp4", origin="layer", expected_token="7:somebody-elses-video"
    )
    assert attached is False and service.media_refusal == "source_changed"
    assert not source.exists()  # this attach's own input: not left for anyone to find
    rows = (await db_session.execute(select(ArchiveMediaAsset))).scalars().all()
    assert rows == []
    assert not list(root.rglob("*.part"))


async def test_a_traversal_name_is_refused_and_the_input_removed(target):
    service, archive, root = target
    source = root / "timelapse_z.mp4"
    source.write_bytes(b"video")
    assert await service.attach_timelapse_file(archive.id, source, "../../evil.mp4", origin="layer") is False
    assert not source.exists() and not (root.parent / "evil.mp4").exists()
