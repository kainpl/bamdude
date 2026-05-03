"""Integration tests for m041 — drain pending_uploads + purge legacy archived rows.

Covers both halves of the migration end-to-end:

* **Part A** — pending row with file on disk → saved to library +
  status flipped; pending row whose file is gone → discarded; pending
  row whose hash already lives in the library → deduped (no new
  library_files row); already-archived pending row left alone.
* **Part B** — `print_archives.status='archived'` rows hard-deleted;
  `'completed'` / `'printing'` controls untouched; `spool_usage_history`
  rows pre-NULLed before the cascade hits.

Idempotency is checked by running the seed twice in a row and
asserting the second run is a no-op.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations.m041_drain_pending_and_purge_archived import seed as m041_seed
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.pending_upload import PendingUpload
from backend.app.models.spool_usage_history import SpoolUsageHistory


@pytest.mark.asyncio
@pytest.mark.integration
async def test_m041_drains_pending_to_library(db_session, test_engine, tmp_path, monkeypatch):
    """Pending row with file on disk lands in the library and flips to archived."""
    # Library writes go under app_settings.base_dir; redirect both to
    # tmp_path so the test stays self-contained.
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    # ``get_library_dir()`` keys off archive_dir, not base_dir — keep
    # the library tree under tmp_path so to_relative_path produces a
    # repo-relative string the dest_path under tmp_path can satisfy.
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(exist_ok=True)

    # Real bytes on disk so the migration can hash + copy them.
    payload = b"PK\x03\x04 not really a zip but the migration only hashes"
    src = tmp_path / "incoming.3mf"
    src.write_bytes(payload)

    pending = PendingUpload(
        filename="incoming.3mf",
        file_path=str(src),
        file_size=src.stat().st_size,
        status="pending",
    )
    db_session.add(pending)
    await db_session.commit()
    await db_session.refresh(pending)
    pending_id = pending.id

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m041_seed(factory)

    db_session.expire_all()
    refreshed = await db_session.scalar(select(PendingUpload).where(PendingUpload.id == pending_id))
    assert refreshed is not None
    assert refreshed.status == "archived"
    assert refreshed.archived_to_library_id is not None
    assert refreshed.archived_at is not None

    # Library row carries the hash + filename + a real on-disk copy.
    lib = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == refreshed.archived_to_library_id))
    assert lib is not None
    assert lib.filename == "incoming.3mf"
    assert lib.file_hash and len(lib.file_hash) == 64
    on_disk = tmp_path / lib.file_path
    assert on_disk.exists()
    assert on_disk.read_bytes() == payload

    # Source temp was cleaned up.
    assert not src.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_m041_discards_pending_when_file_missing(db_session, test_engine, tmp_path, monkeypatch):
    """File-on-disk gone → status='discarded', archived_to_library_id stays NULL."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    # ``get_library_dir()`` keys off archive_dir, not base_dir — keep
    # the library tree under tmp_path so to_relative_path produces a
    # repo-relative string the dest_path under tmp_path can satisfy.
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(exist_ok=True)

    pending = PendingUpload(
        filename="ghost.3mf",
        file_path=str(tmp_path / "does-not-exist.3mf"),
        file_size=0,
        status="pending",
    )
    db_session.add(pending)
    await db_session.commit()
    await db_session.refresh(pending)
    pending_id = pending.id

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m041_seed(factory)

    db_session.expire_all()
    refreshed = await db_session.scalar(select(PendingUpload).where(PendingUpload.id == pending_id))
    assert refreshed.status == "discarded"
    assert refreshed.archived_to_library_id is None
    assert refreshed.archived_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_m041_dedupes_pending_against_existing_library_file(db_session, test_engine, tmp_path, monkeypatch):
    """Pending file whose hash already lives in the library → linked, no new row."""
    import hashlib

    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    # ``get_library_dir()`` keys off archive_dir, not base_dir — keep
    # the library tree under tmp_path so to_relative_path produces a
    # repo-relative string the dest_path under tmp_path can satisfy.
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(exist_ok=True)

    payload = b"already known to the library"
    file_hash = hashlib.sha256(payload).hexdigest()

    existing = LibraryFile(
        filename="known.3mf",
        file_path="library/files/known.3mf",
        file_type="3mf",
        file_size=len(payload),
        file_hash=file_hash,
    )
    db_session.add(existing)
    await db_session.commit()
    await db_session.refresh(existing)
    existing_id = existing.id

    src = tmp_path / "duplicate.3mf"
    src.write_bytes(payload)

    pending = PendingUpload(
        filename="duplicate.3mf",
        file_path=str(src),
        file_size=src.stat().st_size,
        status="pending",
    )
    db_session.add(pending)
    await db_session.commit()
    await db_session.refresh(pending)
    pending_id = pending.id

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m041_seed(factory)

    db_session.expire_all()

    # Pending links to the EXISTING library row, no extra one was created.
    refreshed_pending = await db_session.scalar(select(PendingUpload).where(PendingUpload.id == pending_id))
    assert refreshed_pending.archived_to_library_id == existing_id

    lib_count = (await db_session.execute(select(LibraryFile).where(LibraryFile.file_hash == file_hash))).all()
    assert len(lib_count) == 1

    # Source temp cleaned up even on the dedup path.
    assert not src.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_m041_skips_already_processed_pending_rows(db_session, test_engine, tmp_path, monkeypatch):
    """Idempotency: pending rows already at status='archived'/'discarded' are left alone."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    # ``get_library_dir()`` keys off archive_dir, not base_dir — keep
    # the library tree under tmp_path so to_relative_path produces a
    # repo-relative string the dest_path under tmp_path can satisfy.
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(exist_ok=True)

    # SQLite stores naive datetimes; aware-vs-naive comparison would
    # spuriously trip the "untouched" assertion below.
    pre_archived_at = datetime(2024, 1, 1)
    pre = PendingUpload(
        filename="old.3mf",
        file_path=str(tmp_path / "irrelevant"),
        file_size=0,
        status="archived",
        archived_at=pre_archived_at,
    )
    db_session.add(pre)
    await db_session.commit()
    await db_session.refresh(pre)
    pre_id = pre.id

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m041_seed(factory)

    db_session.expire_all()
    refreshed = await db_session.scalar(select(PendingUpload).where(PendingUpload.id == pre_id))
    assert refreshed.status == "archived"
    assert refreshed.archived_at == pre_archived_at  # untouched


@pytest.mark.asyncio
@pytest.mark.integration
async def test_m041_purges_legacy_archived_and_keeps_real_history(db_session, test_engine, tmp_path, monkeypatch):
    """status='archived' rows die; 'completed' / 'printing' rows survive."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(exist_ok=True)

    legacy = PrintArchive(
        filename="legacy.3mf",
        file_path="archive/x/legacy.3mf",
        file_size=1,
        content_hash="d" * 64,
        status="archived",
    )
    real_completed = PrintArchive(
        filename="real.3mf",
        file_path="archive/y/real.3mf",
        file_size=1,
        content_hash="e" * 64,
        status="completed",
        completed_at=datetime(2025, 1, 1),
    )
    in_flight = PrintArchive(
        filename="live.3mf",
        file_path="archive/z/live.3mf",
        file_size=1,
        content_hash="b" * 64,
        status="printing",
    )
    db_session.add_all([legacy, real_completed, in_flight])
    await db_session.commit()
    for a in (legacy, real_completed, in_flight):
        await db_session.refresh(a)
    legacy_id = legacy.id
    completed_id = real_completed.id
    in_flight_id = in_flight.id

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m041_seed(factory)

    db_session.expire_all()
    survivors = (await db_session.execute(select(PrintArchive.id, PrintArchive.status).order_by(PrintArchive.id))).all()
    survivor_ids = {row[0] for row in survivors}
    assert legacy_id not in survivor_ids
    assert {completed_id, in_flight_id} <= survivor_ids


@pytest.mark.asyncio
@pytest.mark.integration
async def test_m041_pre_nulls_spool_usage_history_archive_id(db_session, test_engine, tmp_path, monkeypatch):
    """spool_usage_history.archive_id pointing at a soon-to-be-deleted archive is NULLed."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    # ``get_library_dir()`` keys off archive_dir, not base_dir — keep
    # the library tree under tmp_path so to_relative_path produces a
    # repo-relative string the dest_path under tmp_path can satisfy.
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(exist_ok=True)

    legacy = PrintArchive(
        filename="legacy.3mf",
        file_path="archive/x/legacy.3mf",
        file_size=1,
        content_hash="9" * 64,
        status="archived",
    )
    db_session.add(legacy)
    await db_session.commit()
    await db_session.refresh(legacy)
    legacy_id = legacy.id

    # spool_id is NOT NULL but the test conftest doesn't enable
    # PRAGMA foreign_keys = ON, so an arbitrary int satisfies the
    # column without needing a real spool row.
    usage = SpoolUsageHistory(
        spool_id=999,
        archive_id=legacy_id,
        weight_used=1.0,
    )
    db_session.add(usage)
    await db_session.commit()
    await db_session.refresh(usage)
    usage_id = usage.id

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m041_seed(factory)

    db_session.expire_all()
    refreshed_usage = await db_session.scalar(select(SpoolUsageHistory).where(SpoolUsageHistory.id == usage_id))
    assert refreshed_usage is not None  # row survives
    assert refreshed_usage.archive_id is None  # FK pre-NULLed


@pytest.mark.asyncio
@pytest.mark.integration
async def test_m041_is_idempotent(db_session, test_engine, tmp_path, monkeypatch):
    """Second run finds zero work — no errors, no row count changes."""
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    # ``get_library_dir()`` keys off archive_dir, not base_dir — keep
    # the library tree under tmp_path so to_relative_path produces a
    # repo-relative string the dest_path under tmp_path can satisfy.
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(exist_ok=True)

    src = tmp_path / "once.3mf"
    src.write_bytes(b"hello world")
    pending = PendingUpload(
        filename="once.3mf",
        file_path=str(src),
        file_size=src.stat().st_size,
        status="pending",
    )
    legacy = PrintArchive(
        filename="legacy.3mf",
        file_path="archive/x/legacy.3mf",
        file_size=1,
        content_hash="7" * 64,
        status="archived",
    )
    db_session.add_all([pending, legacy])
    await db_session.commit()

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m041_seed(factory)
    await m041_seed(factory)  # second run — must not raise + must not delete more

    db_session.expire_all()
    archived_left = (
        await db_session.execute(text("SELECT COUNT(*) FROM print_archives WHERE status = 'archived'"))
    ).scalar()
    assert archived_left == 0

    pending_count = (
        await db_session.execute(text("SELECT COUNT(*) FROM pending_uploads WHERE status = 'pending'"))
    ).scalar()
    assert pending_count == 0
