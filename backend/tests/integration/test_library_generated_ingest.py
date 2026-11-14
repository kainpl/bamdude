"""A file BamDude generated itself (an assembled project) entering the library through the ordinary ingest."""

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.api.routes.library import save_3mf_bytes_to_library, to_absolute_path
from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.services import library_file_runtime, swap_compatibility

NAME = "jar - X1.3mf"


def _project(marker: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr("Metadata/model_settings.config", f"<config><!-- {marker} --></config>")
    return buffer.getvalue()


@pytest.fixture
def isolated_archive_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(app_settings, "archive_dir", str(tmp_path / "data"), raising=False)
    return tmp_path


async def _folder(db, **fields) -> LibraryFolder:
    folder = LibraryFolder(name="target", **fields)
    db.add(folder)
    await db.commit()
    return await db.scalar(
        select(LibraryFolder).where(LibraryFolder.id == folder.id).options(selectinload(LibraryFolder.products))
    )


async def _save(db, folder, marker, name=NAME, **kwargs):
    return await save_3mf_bytes_to_library(
        db,
        content=_project(marker),
        filename=name,
        folder=folder,
        source_type="assembled",
        destination="unique",
        swap_origin="generated",
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unique_destination_never_overwrites_on_an_external_folder(db_session, isolated_archive_dir):
    share = isolated_archive_dir / "share"
    share.mkdir()
    folder = await _folder(db_session, is_external=True, external_path=str(share), external_readonly=False)
    first = await _save(db_session, folder, "one")
    second = await _save(db_session, folder, "two")
    assert (first.file.filename, second.file.filename) == ("jar - X1.3mf", "jar - X1 (2).3mf")
    assert first.file.is_external and second.file.is_external
    assert first.fallback_reason is None and second.fallback_reason is None
    assert (share / "jar - X1.3mf").read_bytes() == _project("one")
    assert (share / "jar - X1 (2).3mf").read_bytes() == _project("two")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unique_destination_gives_a_free_name_in_a_managed_folder(db_session, isolated_archive_dir):
    folder = await _folder(db_session)
    names = [(await _save(db_session, folder, marker)).file.filename for marker in ("one", "two", "three")]
    assert names == ["jar - X1.3mf", "jar - X1 (2).3mf", "jar - X1 (3).3mf"]
    root = [(await _save(db_session, None, marker)).file.filename for marker in ("four", "five")]
    assert root == ["jar - X1.3mf", "jar - X1 (2).3mf"], "the library's root is a folder of its own"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_trashed_file_does_not_hold_its_name(db_session, isolated_archive_dir):
    folder = await _folder(db_session)
    first = await _save(db_session, folder, "one")
    first.file.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db_session.commit()
    assert (await _save(db_session, folder, "two")).file.filename == "jar - X1.3mf"


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("readonly", "exists", "reason"),
    [(True, True, "external_readonly"), (False, False, "external_unreachable")],
)
async def test_an_external_folder_that_cannot_take_the_file_falls_back_and_says_why(
    db_session, isolated_archive_dir, readonly, exists, reason
):
    share = isolated_archive_dir / "share"
    if exists:
        share.mkdir()
    folder = await _folder(db_session, is_external=True, external_path=str(share), external_readonly=readonly)
    result = await _save(db_session, folder, "one")
    assert result.outcome == "created" and result.fallback_reason == reason
    assert result.file.is_external is False and result.file.folder_id == folder.id
    stored = to_absolute_path(result.file.file_path)
    assert stored.is_file() and share not in stored.parents
    assert not share.exists() or not list(share.iterdir())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_generated_origin_is_declared_false_whatever_the_name(db_session, isolated_archive_dir):
    result = await _save(db_session, None, "one", name="jar.swap.3mf")
    row = await db_session.scalar(select(LibraryFile).where(LibraryFile.id == result.file.id))
    assert swap_compatibility.library_fact(row) is False
    assert row.swap_source_declaration["origin"] == "generated" and row.source_type == "assembled"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_restore_hold_is_a_503_and_writes_nothing(db_session, isolated_archive_dir, monkeypatch):
    monkeypatch.setattr(library_file_runtime.upload_writes, "closed", True)
    with pytest.raises(HTTPException) as refused:
        await _save(db_session, None, "one")
    assert refused.value.status_code == 503
    assert not (await db_session.scalars(select(LibraryFile))).all()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_default_destination_is_unchanged(db_session, isolated_archive_dir):
    first = await save_3mf_bytes_to_library(db_session, content=_project("one"), filename="model.3mf")
    second = await save_3mf_bytes_to_library(db_session, content=_project("two"), filename="model.3mf")
    assert (first.file.filename, second.file.filename) == ("model.3mf", "model.3mf")
    assert first.fallback_reason is None
    assert first.file.swap_source_declaration["origin"] == "classifier"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_list_and_detail_expose_can_relayout(async_client, db_session):
    plain = LibraryFile(filename="a.3mf", file_path="/x/a.3mf", file_size=1, file_type="3mf")
    made = LibraryFile(
        filename="b.3mf",
        file_path="/x/b.3mf",
        file_size=1,
        file_type="3mf",
        file_metadata={"assembled": True, "assembly_set": "a" * 32},
    )
    db_session.add_all([plain, made])
    await db_session.commit()
    listed = {row["filename"]: row for row in (await async_client.get("/api/v1/library/files")).json()}
    assert listed["a.3mf"]["can_relayout"] is False and listed["b.3mf"]["can_relayout"] is True
    assert (await async_client.get(f"/api/v1/library/files/{made.id}")).json()["can_relayout"] is True
    assert (await async_client.get(f"/api/v1/library/files/{plain.id}")).json()["can_relayout"] is False
