"""New target media/import metadata retain their sole writers through C roots."""

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.models.archive_media_asset import ArchiveMediaAsset
from backend.app.models.library import LibraryFile
from backend.app.models.library_file_import_meta import LibraryFileImportMeta
from backend.app.services import content_delete_rows, library_import_meta
from backend.app.services.library_delete_scope import library_delete_scope
from backend.app.services.library_identity import LibraryIdentity
from backend.app.services.library_trash import library_trash_service
from backend.tests.integration.test_content_delete_atomic import account, archive_of, file_of
from backend.tests.integration.test_content_delete_protocol import independent_factory, source  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def media(archive):
    return ArchiveMediaAsset(
        asset_uuid=str(uuid4()),
        archive_id=archive.id,
        archive_identity=archive.recovery_identity,
        role="primary",
        revision=1,
        relative_path="videos/old.mp4",
        origin="legacy",
        created_at=datetime(2026, 1, 1),
    )


async def import_record(db, file_id):
    await library_import_meta.record(
        db,
        library_file_id=file_id,
        source_type="manyfold",
        origin=library_import_meta.ImportOrigin(source_model_id="owned-model", title="owned title"),
    )


async def test_user_bulk_root_removes_both_new_child_domains(async_client, db_session):
    owner = await account(db_session)
    file, _path = await file_of(db_session, owner)
    archive = await archive_of(db_session, owner, library_file_id=file.id)
    owner_id, file_id, archive_id = owner.id, file.id, archive.id
    await import_record(db_session, file_id)
    db_session.add(media(archive))
    await db_session.commit()
    response = await async_client.delete(f"/api/v1/users/{owner_id}?delete_items=true")
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert (
        list(
            await db_session.scalars(
                select(ArchiveMediaAsset.archive_id).where(ArchiveMediaAsset.archive_id == archive_id)
            )
        )
        == []
    )
    assert await library_import_meta.for_file(db_session, file_id) is None


@pytest.mark.parametrize("commit", [False, True], ids=["rollback", "commit"])
async def test_composed_child_rows_belong_to_the_parent_transaction(independent_factory, commit):
    factory = independent_factory
    proof, archive_id, _identity = await source(factory)
    async with factory() as db:
        from backend.app.models.archive import PrintArchive

        archive = await db.get(PrintArchive, archive_id)
        db.add(media(archive))
        await import_record(db, proof.id)
        await db.commit()
        file = await db.get(LibraryFile, proof.id)
        async with library_delete_scope(db, [LibraryIdentity.capture(file)], archive_ids=[archive_id]):
            await content_delete_rows.archive_rows(db, archive)
            await library_trash_service.purge_rows(db, file)
            if commit:
                await db.commit()
            else:
                await db.rollback()
    async with factory() as db:
        assert (
            await db.scalar(select(ArchiveMediaAsset.id).where(ArchiveMediaAsset.archive_id == archive_id)) is None
        ) is commit
        assert (
            await db.scalar(select(LibraryFileImportMeta.id).where(LibraryFileImportMeta.library_file_id == proof.id))
            is None
        ) is commit
