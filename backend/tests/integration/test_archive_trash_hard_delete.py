"""Emptying the archive trash actually removes the files.

⚠️ **It never did.** ``ArchiveService.delete_archive`` opens with

    archive = await self.get_archive(archive_id)   # no include_trashed
    if not archive:
        return False

and ``get_archive`` drops rows with ``deleted_at`` unless asked not to. Every
caller of ``delete_archive`` on the trash side hands it a trashed id — its own
docstring says "Caller should verify the archive is in trash before invoking" —
so it answered ``False`` and did nothing, every time.

What that cost:

* the retention sweeper removed the ROWS (via a "defensive" bulk delete at the
  end of ``_sweep``, whose comment — "if delete_archive somehow left rows
  behind" — was describing this hole without knowing it) and left every 3MF,
  preview, timelapse and photo on disk, permanently;
* ``empty_trash`` has no such fallback, so "Empty trash" removed **nothing at
  all**, not even rows, and reported 0.

Both halves shipped in one commit — ``fb448f9d``, v0.4.2, 2026-04-30 — so the
archive trash has never once freed a byte.

⚠️ These tests matter more than most, because this is a scheduled ``rmtree``
over somebody else's prints that has not run in production since it was
written. The deduplication guard in particular has never actually been
exercised: several archives can share one folder, and until now nothing ever
reached the code that decides whether to keep it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.settings import Settings
from backend.app.services.archive import ArchiveService
from backend.app.services.archive_purge import TRASH_RETENTION_KEY, archive_purge_service

# ⚠️ ``async_client`` is requested by the tests below even though they issue no
# HTTP: ``hard_delete_now`` deliberately opens its OWN session from the global
# ``database.async_session``, and that factory is only redirected at the test
# engine inside this fixture. Without it the sweeper queries a different
# database, finds nothing, and the test passes or fails for the wrong reason.


def _folder(name: str) -> tuple[Path, Path]:
    """An archive folder with a file in it, under the real archive root."""
    directory = settings.archive_dir / name
    directory.mkdir(parents=True, exist_ok=True)
    threemf = directory / "job.gcode.3mf"
    threemf.write_bytes(b"bytes that should stop existing")
    return directory, threemf


async def _archive(
    db,
    *,
    threemf: Path,
    trashed_days_ago: float | None,
    printer_id: int | None = None,
) -> PrintArchive:
    deleted_at = None
    if trashed_days_ago is not None:
        deleted_at = datetime.now(timezone.utc) - timedelta(days=trashed_days_ago)
    row = PrintArchive(
        printer_id=printer_id,
        file_path=str(threemf.relative_to(settings.base_dir)),
        file_size=threemf.stat().st_size,
        print_name="job",
        filename="job.gcode.3mf",
        status="completed",
        deleted_at=deleted_at,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
@pytest.mark.integration
async def test_hard_deleting_a_trashed_archive_removes_its_files(db_session):
    """The bug, at its narrowest: the one function everything else goes through."""
    directory, threemf = _folder("20260818_140000_trashed")
    row = await _archive(db_session, threemf=threemf, trashed_days_ago=40)
    await db_session.commit()

    assert await ArchiveService(db_session).delete_archive(row.id) is True
    assert not threemf.exists(), "the 3MF outlived the archive it belonged to"
    assert not directory.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_emptying_the_trash_removes_rows_and_files(async_client: AsyncClient, db_session):
    """``empty_trash`` has no fallback row delete, so before the fix the button
    did nothing whatsoever and said so with a 0."""
    directory, threemf = _folder("20260818_141000_empty")
    row = await _archive(db_session, threemf=threemf, trashed_days_ago=1)
    row_id = row.id
    await db_session.commit()

    removed = await archive_purge_service.empty_trash(db_session)

    assert removed == 1
    assert not threemf.exists()
    assert not directory.exists()
    db_session.expire_all()
    left = await db_session.execute(select(func.count()).select_from(PrintArchive).where(PrintArchive.id == row_id))
    assert left.scalar() == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_emptying_the_trash_leaves_active_archives_alone(async_client: AsyncClient, db_session):
    """It empties the trash, not the archive."""
    kept_dir, kept_file = _folder("20260818_141500_kept")
    kept = await _archive(db_session, threemf=kept_file, trashed_days_ago=None)
    kept_id = kept.id
    await db_session.commit()

    await archive_purge_service.empty_trash(db_session)

    assert kept_file.exists(), "an archive nobody deleted lost its file"
    assert kept_dir.exists()
    db_session.expire_all()
    still_there = await db_session.execute(
        select(func.count()).select_from(PrintArchive).where(PrintArchive.id == kept_id)
    )
    assert still_there.scalar() == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_sweeper_removes_files_past_retention(async_client: AsyncClient, db_session):
    """The scheduled path — the one that has been logging 'hard-deleted 0'."""
    db_session.add(Settings(key=TRASH_RETENTION_KEY, value="7"))
    directory, threemf = _folder("20260818_142000_swept")
    row = await _archive(db_session, threemf=threemf, trashed_days_ago=30)
    row_id = row.id
    await db_session.commit()

    swept = await archive_purge_service._sweep(db_session)

    assert swept == 1
    assert not threemf.exists()
    assert not directory.exists()
    db_session.expire_all()
    left = await db_session.execute(select(func.count()).select_from(PrintArchive).where(PrintArchive.id == row_id))
    assert left.scalar() == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_sweeper_leaves_a_recent_deletion_alone(async_client: AsyncClient, db_session):
    """The retention window is the whole point of a trash bin."""
    db_session.add(Settings(key=TRASH_RETENTION_KEY, value="30"))
    directory, threemf = _folder("20260818_142500_recent")
    await _archive(db_session, threemf=threemf, trashed_days_ago=2)
    await db_session.commit()

    swept = await archive_purge_service._sweep(db_session)

    assert swept == 0
    assert threemf.exists(), "a file deleted two days ago must still be restorable"
    assert directory.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_folder_another_archive_still_uses_survives(async_client: AsyncClient, db_session):
    """Storage is deduplicated by content, so one folder can back several
    archives.

    ⚠️ This guard has never actually run: the trash could not reach the code
    that decides whether to remove a folder. Now that it can, the first thing to
    pin is that emptying the trash cannot take a file an ACTIVE archive is
    still pointing at.
    """
    directory, threemf = _folder("20260818_143000_shared")
    trashed = await _archive(db_session, threemf=threemf, trashed_days_ago=40)
    active = await _archive(db_session, threemf=threemf, trashed_days_ago=None)
    trashed_id, active_id = trashed.id, active.id
    await db_session.commit()

    removed = await archive_purge_service.empty_trash(db_session)

    assert removed == 1
    assert threemf.exists(), "the surviving archive lost the file it points at"
    db_session.expire_all()
    rows = await db_session.execute(select(PrintArchive.id).where(PrintArchive.file_path.isnot(None)))
    ids = set(rows.scalars().all())
    assert active_id in ids and trashed_id not in ids

    # ...and once the last archive goes, so does the folder.
    await db_session.execute(
        PrintArchive.__table__.update()
        .where(PrintArchive.id == active_id)
        .values(deleted_at=datetime.now(timezone.utc))
    )
    await db_session.commit()
    await archive_purge_service.empty_trash(db_session)

    assert not threemf.exists()
    assert not directory.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_restoring_still_gets_the_files_back(db_session):
    """Soft-delete leaves the bytes in place — that is what makes restore a
    metadata-only operation, and the fix must not change it."""
    directory, threemf = _folder("20260818_143500_restore")
    row = await _archive(db_session, threemf=threemf, trashed_days_ago=1)
    await db_session.commit()

    await archive_purge_service.restore(db_session, row)

    assert threemf.exists()
    assert directory.exists()
    assert row.deleted_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_delete_permanently_endpoint_answered_404(async_client: AsyncClient, db_session):
    """The per-item purge did not fail quietly: ``hard_delete_now`` answered
    False, and the route turns that into ``404 Archive vanished during delete``.
    So pressing "Delete permanently" on one archive showed an error while
    leaving the row and the files exactly where they were."""
    directory, threemf = _folder("20260818_144000_route_one")
    row = await _archive(db_session, threemf=threemf, trashed_days_ago=1)
    row_id = row.id
    await db_session.commit()

    response = await async_client.delete(f"/api/v1/archives/trash/{row_id}")

    assert response.status_code == 200, response.text
    assert not threemf.exists()
    assert not directory.exists()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_empty_trash_endpoint_reports_what_it_removed(async_client: AsyncClient, db_session):
    """It counted ``hard_delete_now`` successes, of which there were never any,
    so the response was always ``{"deleted": 0}``."""
    first_dir, first = _folder("20260818_144500_route_a")
    second_dir, second = _folder("20260818_144600_route_b")
    await _archive(db_session, threemf=first, trashed_days_ago=1)
    await _archive(db_session, threemf=second, trashed_days_ago=99)
    await db_session.commit()

    response = await async_client.delete("/api/v1/archives/trash")

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 2
    assert not first.exists() and not second.exists()
    assert not first_dir.exists() and not second_dir.exists()
