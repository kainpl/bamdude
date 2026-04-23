"""Integration tests for the archive↔library_file link (m014).

Covers:

1. ``archive_print()`` writes ``library_file_id`` when the dispatcher
   passes one (library-file branch of background_dispatch).
2. The m014 seed backfills ``library_file_id`` on unlinked archives by
   hash match, and recomputes ``library_files.print_count`` +
   ``last_printed_at`` from completed archives only.
3. ``attach_3mf_to_archive()`` backfills ``library_file_id`` on
   fallback archives once the 3MF lands and a content-hash match
   into library_files becomes possible.
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.migrations.m014_archive_library_link import seed as m014_seed
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.services.archive import ArchiveService


def _make_tempfile(tmp_path: Path, name: str, payload: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(payload)
    return p


@pytest.mark.asyncio
@pytest.mark.integration
async def test_archive_print_persists_library_file_id(db_session, printer_factory, tmp_path):
    """``archive_print(library_file_id=...)`` stores the FK on the row."""
    printer = await printer_factory()
    src = _make_tempfile(tmp_path, "lib.3mf", b"lib-bytes")

    lib = LibraryFile(
        filename="lib.3mf",
        file_path="/library/lib.3mf",
        file_type="3mf",
        file_size=9,
        file_hash="abc" * 21 + "a",  # 64 chars
    )
    db_session.add(lib)
    await db_session.commit()
    await db_session.refresh(lib)

    service = ArchiveService(db_session)
    archive = await service.archive_print(
        printer_id=printer.id,
        source_file=src,
        original_filename="lib.3mf",
        source_content_hash=lib.file_hash,
        library_file_id=lib.id,
    )

    assert archive is not None
    assert archive.library_file_id == lib.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_m014_seed_backfills_link_and_recomputes_counts(db_session, test_engine):
    """Seed backfill links archives by hash and recomputes print_count + last_printed_at.

    Exercises the success-only gating: a failed archive pointing at the
    same library file must NOT count toward print_count, but IS still
    linked (so future UI could surface "3 successful / 1 failed").
    """
    # Library file with a known hash.
    lib = LibraryFile(
        filename="seed.3mf",
        file_path="/library/seed.3mf",
        file_type="3mf",
        file_size=1,
        file_hash="f" * 64,
        print_count=999,  # stale pre-migration value — must be reset
        last_printed_at=datetime(2020, 1, 1),  # stale
    )
    db_session.add(lib)
    await db_session.commit()
    await db_session.refresh(lib)
    lib_id = lib.id

    # Archive rows without library_file_id populated: one completed, one
    # failed, one completed matching via source_content_hash, plus an
    # orphan with no library hash to confirm NULL stays NULL.
    archives = [
        PrintArchive(
            filename="a1.3mf",
            file_path="/a/a1.3mf",
            file_size=1,
            content_hash="f" * 64,
            status="completed",
            completed_at=datetime(2025, 6, 1),
        ),
        PrintArchive(
            filename="a2.3mf",
            file_path="/a/a2.3mf",
            file_size=1,
            content_hash="f" * 64,
            status="failed",
            completed_at=datetime(2025, 6, 2),
        ),
        PrintArchive(
            filename="a3.3mf",
            file_path="/a/a3.3mf",
            file_size=1,
            content_hash="patched1" + "0" * 56,
            source_content_hash="f" * 64,
            status="completed",
            completed_at=datetime(2025, 7, 1),
        ),
        PrintArchive(
            filename="orphan.3mf",
            file_path="/a/orphan.3mf",
            file_size=1,
            content_hash="9" * 64,
            status="completed",
            completed_at=datetime(2025, 8, 1),
        ),
    ]
    db_session.add_all(archives)
    await db_session.commit()
    for a in archives:
        await db_session.refresh(a)

    # Seed drives its own session via the factory — build one off the same
    # test engine so it shares the in-memory DB with db_session.
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    await m014_seed(factory)

    # Reload everything — seed committed, our session is stale.
    db_session.expire_all()
    rows = (await db_session.execute(select(PrintArchive).order_by(PrintArchive.id))).scalars().all()
    # First three link to lib; orphan stays NULL.
    assert rows[0].library_file_id == lib_id
    assert rows[1].library_file_id == lib_id
    assert rows[2].library_file_id == lib_id
    assert rows[3].library_file_id is None

    # print_count counts completed archives only — failed (#2) excluded.
    refreshed_lib = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == lib_id))
    assert refreshed_lib.print_count == 2  # a1 + a3
    assert refreshed_lib.last_printed_at == datetime(2025, 7, 1)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_attach_3mf_backfills_library_file_id(db_session, printer_factory, tmp_path):
    """When a fallback archive gets its 3MF later, the attach helper hash-links it."""
    printer = await printer_factory()
    payload = b"recovered-3mf-bytes"
    import hashlib

    expected_hash = hashlib.sha256(payload).hexdigest()

    lib = LibraryFile(
        filename="recovered.3mf",
        file_path="/library/recovered.3mf",
        file_type="3mf",
        file_size=len(payload),
        file_hash=expected_hash,
    )
    db_session.add(lib)

    # Fallback archive (no 3MF yet, no library link yet).
    archive = PrintArchive(
        printer_id=printer.id,
        filename="recovered.3mf",
        file_path="",
        file_size=0,
        content_hash=None,
        status="printing",
        extra_data={"no_3mf_available": True},
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    await db_session.refresh(lib)

    src = _make_tempfile(tmp_path, "recovered.3mf", payload)
    service = ArchiveService(db_session)
    ok = await service.attach_3mf_to_archive(archive.id, src, original_filename="recovered.3mf")

    assert ok is True
    await db_session.refresh(archive)
    assert archive.library_file_id == lib.id
    assert archive.content_hash == expected_hash
