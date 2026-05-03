"""Regression test: reprint must create a NEW archive, not mutate the source.

Pre-fix bug: ``_run_reprint_archive`` reused the source archive row and
``on_print_start`` then unconditionally flipped its status to
``'printing'``, destroying any prior terminal state ('failed',
'cancelled', or 'completed'). Reprinting a failed run silently erased
the failure record from the print history.

Post-fix: the dispatcher creates a fresh ``PrintArchive`` row that
inherits chain identity from the source (``source_content_hash``,
``library_file_id``, ``plate_index``, ``project_id``) but has its own
``id``, its own ``status='printing'``, and its own ``created_by_id``
attributing the run to whoever clicked Reprint. The source row is left
untouched so the failure / completion record stays in the history.

The test mocks FTP / MQTT / printer-state callbacks so the dispatch
runner can complete without real hardware, then asserts the DB shape:
source row unchanged, new row materialised with the right inherited
fields, ``register_expected_print`` called with the new archive's id.
"""

from __future__ import annotations

import hashlib
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.services.background_dispatch import BackgroundDispatchService, PrintDispatchJob


def _write_minimal_3mf(path: Path) -> str:
    """Create a tiny valid ZIP at ``path`` and return its sha256."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/model_settings.config", "<config/>")
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_reprint_creates_new_archive_and_leaves_source_failed_row_intact(
    db_session, test_engine, tmp_path, monkeypatch, printer_factory
):
    """Reprinting a 'failed' archive creates a new 'printing' row + leaves source untouched."""
    from backend.app.core.config import settings as app_settings
    from backend.app.core.database import async_session as global_session_factory

    # Redirect file-on-disk lookups to the temp tree.
    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)

    # Source 3MF on disk (cross-printer dedup will reuse this path for the
    # new archive since content_hash matches). Path is repo-relative so
    # archive_print finds it via base_dir.
    src_dir = tmp_path / "archive" / "20260101_000000_source"
    src_dir.mkdir(parents=True)
    src_3mf = src_dir / "source.3mf"
    file_hash = _write_minimal_3mf(src_3mf)
    rel_3mf = str(src_3mf.relative_to(tmp_path))

    # Library file the source archive came from — present so we can assert
    # library_file_id is inherited by the new archive.
    lib = LibraryFile(
        filename="source.3mf",
        file_path="library/files/source.3mf",
        file_type="3mf",
        file_size=1,
        file_hash=file_hash,
    )
    db_session.add(lib)
    await db_session.commit()
    await db_session.refresh(lib)

    printer = await printer_factory()

    source = PrintArchive(
        printer_id=printer.id,
        filename="source.3mf",
        file_path=rel_3mf,
        file_size=src_3mf.stat().st_size,
        content_hash=file_hash,
        source_content_hash=file_hash,
        library_file_id=lib.id,
        plate_index=2,
        project_id=None,
        status="failed",  # the bug: this used to be flipped to 'printing'
        completed_at=datetime(2026, 5, 1, 12, 0, 0),
        print_name="source print",
    )
    db_session.add(source)
    await db_session.commit()
    await db_session.refresh(source)
    source_id = source.id
    # Capture all the field values we'll later assert against — after the
    # ``db_session.expire_all()`` below SQLAlchemy lazy-loads on attribute
    # access, which trips MissingGreenlet under aiosqlite from a sync context.
    source_chain_hash = source.source_content_hash
    lib_id = lib.id
    printer_id = printer.id

    # Test runs against the conftest's in-memory engine, but the dispatcher
    # imports ``async_session`` at module top from
    # ``backend.app.core.database``. Override that factory globally for the
    # duration of the test so the dispatcher's writes land in the same
    # in-memory DB ``db_session`` reads from.
    test_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.services.background_dispatch.async_session", test_factory)

    # Mock the network / hardware layer so the dispatcher reaches the
    # archive-creation block + the register_expected_print call.
    captured_register_args: dict = {}

    def _capture_register(printer_id, filename, archive_id, **kwargs):
        captured_register_args["printer_id"] = printer_id
        captured_register_args["filename"] = filename
        captured_register_args["archive_id"] = archive_id

    printer_name = printer.name
    job = PrintDispatchJob(
        id=1,
        kind="reprint_archive",
        source_id=source_id,
        source_name="source.3mf",
        printer_id=printer_id,
        printer_name=printer_name,
        options={"mesh_mode_fast_check": True, "ams_mapping": None},
        requested_by_user_id=99,  # the user who clicked Reprint
    )
    job.completion_event = MagicMock()

    service = BackgroundDispatchService()

    fake_status = MagicMock()
    fake_status.state = "FINISH"
    fake_status.subtask_id = None
    fake_status.gcode_file = ""

    with (
        patch("backend.app.services.background_dispatch.printer_manager.is_connected", return_value=True),
        patch(
            "backend.app.services.background_dispatch.printer_manager.ensure_fresh_connection_for_printer",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "backend.app.services.background_dispatch.printer_manager.get_status",
            return_value=fake_status,
        ),
        patch("backend.app.services.background_dispatch.printer_manager.start_print", return_value=True),
        patch("backend.app.services.background_dispatch.printer_manager.set_current_print_user"),
        patch(
            "backend.app.services.background_dispatch.delete_file_async",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "backend.app.services.background_dispatch.list_files_async",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "backend.app.services.background_dispatch.upload_file_async",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "backend.app.services.background_dispatch.get_ftp_retry_settings",
            new_callable=AsyncMock,
            return_value=(False, 0, 0, 30),
        ),
        patch.object(service, "_verify_print_response", new_callable=AsyncMock, return_value=True),
        patch.object(service, "_set_active_message", new_callable=AsyncMock),
        patch.object(service, "_set_active_upload_progress", new_callable=AsyncMock),
        patch.object(service, "_run_swap_macro_if_needed", new_callable=AsyncMock),
        patch("backend.app.main.register_expected_print", side_effect=_capture_register),
        patch("backend.app.main.register_swap_config"),
    ):
        await service._run_reprint_archive(job)

    # Restore the global session factory so subsequent test cleanup is clean.
    monkeypatch.setattr("backend.app.services.background_dispatch.async_session", global_session_factory)

    # 1) The source archive is UNTOUCHED — status still 'failed', completed_at preserved.
    db_session.expire_all()
    refreshed_source = await db_session.scalar(select(PrintArchive).where(PrintArchive.id == source_id))
    assert refreshed_source is not None
    assert refreshed_source.status == "failed", "Source archive's status was mutated by reprint — bug regressed"
    assert refreshed_source.completed_at == datetime(2026, 5, 1, 12, 0, 0)

    # 2) A NEW archive row exists and inherits chain identity.
    all_archives = (await db_session.execute(select(PrintArchive).order_by(PrintArchive.id))).scalars().all()
    assert len(all_archives) == 2, f"Expected 2 archive rows (source + new), got {len(all_archives)}"
    new_archive = next(a for a in all_archives if a.id != source_id)

    assert new_archive.status == "printing"
    assert new_archive.printer_id == printer_id
    assert new_archive.source_content_hash == source_chain_hash
    assert new_archive.library_file_id == lib_id
    assert new_archive.plate_index == 2
    assert new_archive.created_by_id == 99  # the user who clicked Reprint

    # 3) register_expected_print received the NEW archive's id (not the source's).
    assert captured_register_args.get("archive_id") == new_archive.id, (
        f"register_expected_print called with {captured_register_args.get('archive_id')}, "
        f"expected new archive id {new_archive.id}"
    )

    # 4) job.outcome carries the new archive id.
    assert job.outcome["success"] is True
    assert job.outcome["archive_id"] == new_archive.id
