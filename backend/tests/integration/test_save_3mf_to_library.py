"""Integration tests for ``save_3mf_bytes_to_library`` helper (Phase 0 of
the 0.5.x slicer + MakerWorld cycle).

Both the upcoming MakerWorld import path and the slicer dispatch path call
this helper. It must:

- Write the bytes to disk and create a LibraryFile row identical to a normal
  upload (same file_path semantics, same hash, same metadata extraction).
- Dedupe by ``source_url`` BEFORE writing — MakerWorld re-imports of the
  same plate must not re-download or repack.
- Surface the hash-equal "already exists" hint via ``was_existing`` so the
  caller can show "already in library" UX even when source_url is empty.
- Forward ``source_type`` + ``source_url`` to the row, propagate
  ``extra_metadata`` into ``file_metadata``.
- Honour the trash-bin filter — soft-deleted rows do not poison either
  dedup path.
"""

import io
import zipfile
from pathlib import Path

import pytest

from backend.app.api.routes.library import save_3mf_bytes_to_library
from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile, LibraryFolder


def _minimal_3mf_bytes() -> bytes:
    """Build the smallest valid ZIP a 3MF parser will not crash on."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        zf.writestr("Metadata/model_settings.config", "<config/>")
    return buf.getvalue()


@pytest.fixture
def isolated_archive_dir(tmp_path: Path, monkeypatch):
    """Redirect library writes to a per-test tmp dir so we don't pollute
    the real archive directory between tests."""
    monkeypatch.setattr(app_settings, "archive_dir", str(tmp_path), raising=False)
    return tmp_path


class TestSave3mfHappyPath:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_writes_row_and_bytes(self, db_session, isolated_archive_dir):
        content = _minimal_3mf_bytes()
        lib_file, was_existing = await save_3mf_bytes_to_library(
            db_session,
            content=content,
            filename="model.3mf",
            source_type="sliced",
        )
        assert was_existing is False
        assert lib_file.id is not None
        assert lib_file.filename == "model.3mf"
        assert lib_file.file_type == "3mf"
        assert lib_file.file_size == len(content)
        assert lib_file.source_type == "sliced"
        assert lib_file.source_url is None
        # Bytes actually landed on disk.
        from backend.app.api.routes.library import to_absolute_path

        on_disk = to_absolute_path(lib_file.file_path)
        assert on_disk is not None and on_disk.exists()
        assert on_disk.read_bytes() == content

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_extra_metadata_merged(self, db_session, isolated_archive_dir):
        lib_file, _ = await save_3mf_bytes_to_library(
            db_session,
            content=_minimal_3mf_bytes(),
            filename="result.gcode.3mf",
            source_type="sliced",
            extra_metadata={
                "print_time_seconds": 1234,
                "filament_used_g": 18.5,
                "filament_used_mm": 6210.0,
                "used_embedded_settings": True,
            },
        )
        assert lib_file.file_metadata is not None
        assert lib_file.file_metadata["print_time_seconds"] == 1234
        assert lib_file.file_metadata["filament_used_g"] == 18.5
        assert lib_file.file_metadata["used_embedded_settings"] is True


class TestSourceUrlDedupe:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_returns_existing_row_without_rewrite(self, db_session, isolated_archive_dir):
        canonical = "https://makerworld.com/models/12345#profileId-678"
        first, was_existing_first = await save_3mf_bytes_to_library(
            db_session,
            content=_minimal_3mf_bytes(),
            filename="plate1.3mf",
            source_type="makerworld",
            source_url=canonical,
        )
        assert was_existing_first is False
        # Re-import: different filename, but same source_url.
        second, was_existing_second = await save_3mf_bytes_to_library(
            db_session,
            content=b"different content; helper must not even look at it",
            filename="plate1-renamed.3mf",
            source_type="makerworld",
            source_url=canonical,
        )
        assert was_existing_second is True
        assert second.id == first.id
        # Filename of the existing row is unchanged — dedup short-circuits
        # before any new row materialises.
        assert second.filename == "plate1.3mf"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_trashed_row_does_not_block_reimport(self, db_session, isolated_archive_dir):
        from datetime import datetime, timezone

        canonical = "https://makerworld.com/models/777#profileId-1"
        first, _ = await save_3mf_bytes_to_library(
            db_session,
            content=_minimal_3mf_bytes(),
            filename="orig.3mf",
            source_type="makerworld",
            source_url=canonical,
        )
        first.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()
        second, was_existing = await save_3mf_bytes_to_library(
            db_session,
            content=_minimal_3mf_bytes(),
            filename="reimport.3mf",
            source_type="makerworld",
            source_url=canonical,
        )
        # Soft-deleted row is filtered by ``LibraryFile.active()`` — re-import
        # creates a new row instead of resurrecting the trashed one.
        assert second.id != first.id
        assert was_existing is False


class TestHashDedupeHint:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_byte_identical_reupload_flags_was_existing(self, db_session, isolated_archive_dir):
        content = _minimal_3mf_bytes()
        first, was_existing_first = await save_3mf_bytes_to_library(db_session, content=content, filename="a.3mf")
        second, was_existing_second = await save_3mf_bytes_to_library(db_session, content=content, filename="b.3mf")
        assert was_existing_first is False
        # Both rows persist (same hash is a hint, not a hard dedup) so the
        # caller can show "this is a duplicate of file #N" without losing the
        # second row's distinct provenance.
        assert was_existing_second is True
        assert second.id != first.id
        assert second.file_hash == first.file_hash


class TestFolderRouting:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_id_propagated(self, db_session, isolated_archive_dir):
        folder = LibraryFolder(name="MakerWorld")
        db_session.add(folder)
        await db_session.commit()
        await db_session.refresh(folder)
        lib_file, _ = await save_3mf_bytes_to_library(
            db_session,
            content=_minimal_3mf_bytes(),
            filename="plate.3mf",
            folder=folder,
            source_type="makerworld",
            source_url="https://makerworld.com/models/1#profileId-1",
        )
        assert lib_file.folder_id == folder.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_folder_means_root(self, db_session, isolated_archive_dir):
        lib_file, _ = await save_3mf_bytes_to_library(
            db_session,
            content=_minimal_3mf_bytes(),
            filename="plate.3mf",
            source_type="sliced",
        )
        assert lib_file.folder_id is None
