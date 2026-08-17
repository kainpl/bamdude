"""The same bytes never become a second library row.

⚠️ These two functions used to answer the duplicate question and act on neither
— ``save_3mf_bytes_to_library`` returned ``was_existing``, ``store_library_upload``
returned ``duplicate_of``, and both created the row regardless. The behaviour was
pinned by a test whose comment defended it: *"Both rows persist (same hash is a
hint, not a hard dedup)"*. This file is what replaced that intent.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from backend.app.api.routes.library import save_3mf_bytes_to_library, store_library_upload
from backend.app.core.config import settings as app_settings


def _3mf(payload: bytes = b"<model/>") -> bytes:
    """A ZIP that passes ``validate_print_file_upload``'s magic-byte check."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("3D/3dmodel.model", payload)
    return buf.getvalue()


@pytest.fixture
def isolated_archive_dir(tmp_path: Path, monkeypatch):
    """Redirect library writes to a per-test tmp dir. Same shape as the one in
    ``test_save_3mf_to_library`` — these tests count files on disk, so they must
    not see anything another test left behind."""
    monkeypatch.setattr(app_settings, "archive_dir", str(tmp_path), raising=False)
    return tmp_path


class TestTheUploadPath:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_second_upload_of_the_same_bytes_reuses_the_row(self, db_session, isolated_archive_dir):
        content = _3mf()
        first = await store_library_upload(db_session, filename="original.3mf", content=content, target_folder=None)
        second = await store_library_upload(
            db_session, filename="a-different-name.3mf", content=content, target_folder=None
        )

        assert first.outcome == "created"
        assert second.outcome == "deduped"
        assert second.file.id == first.file.id
        assert second.file.filename == "original.3mf", "the existing row keeps its own name"
        assert second.superseded_name == "a-different-name.3mf"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_different_bytes_still_get_their_own_row(self, db_session, isolated_archive_dir):
        """The guard against a dedup that deduplicates everything."""
        first = await store_library_upload(db_session, filename="a.3mf", content=_3mf(b"<a/>"), target_folder=None)
        second = await store_library_upload(db_session, filename="b.3mf", content=_3mf(b"<b/>"), target_folder=None)

        assert second.outcome == "created"
        assert second.file.id != first.file.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_redundant_bytes_are_not_left_on_disk(self, db_session, isolated_archive_dir):
        """⚠️ The dedup branch writes the file before it decides. Leaving it
        behind would turn this feature into an orphan-file generator."""
        from backend.app.api.routes.library import get_library_files_dir

        content = _3mf()
        await store_library_upload(db_session, filename="original.3mf", content=content, target_folder=None)
        before = sorted(p.name for p in get_library_files_dir().rglob("*") if p.is_file())

        await store_library_upload(db_session, filename="again.3mf", content=content, target_folder=None)
        after = sorted(p.name for p in get_library_files_dir().rglob("*") if p.is_file())

        assert after == before, "the deduped upload must clean up the bytes it wrote"


class TestARowThatLostItsFile:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_row_is_re_pointed_rather_than_replaced(self, db_session, isolated_archive_dir):
        """It is missing only its BYTES, and we are holding them.

        Deleting the row and creating a fresh one would return the same content
        with no history — and ``trash_or_purge``'s purge branch nulls
        ``PrintArchive.library_file_id`` and deletes queue rows on the way out.
        """
        from backend.app.api.routes.library import to_absolute_path

        content = _3mf()
        first = await save_3mf_bytes_to_library(db_session, content=content, filename="model.3mf")
        original_id = first.file.id
        first.file.print_count = 7
        await db_session.commit()

        on_disk = to_absolute_path(first.file.file_path)
        assert on_disk is not None
        on_disk.unlink()

        second = await save_3mf_bytes_to_library(db_session, content=content, filename="model.3mf")

        assert second.outcome == "restored"
        assert second.file.id == original_id, "the row survives, with everything hanging off it"
        assert second.file.print_count == 7, "including its print history"
        restored = to_absolute_path(second.file.file_path)
        assert restored is not None and restored.is_file()
        assert restored.read_bytes() == content
