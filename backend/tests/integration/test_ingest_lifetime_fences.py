"""Prepared bytes confer no right to publish after their actor loses its lifetime."""

import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update

from backend.app.api.routes import library
from backend.app.core.auth_lifetime import capture_key
from backend.app.models.api_key import APIKey
from backend.app.models.library import LibraryFile
from backend.app.models.user import User
from backend.app.services import library_ingest
from backend.app.services.library_preview_jobs import Authority

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def content():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
    return output.getvalue()


async def actor(db, *, read=True):
    user = User(username="upload-owner", role="admin", is_active=True)
    db.add(user)
    await db.flush()
    key = APIKey(
        name="uploader",
        key_hash="not-used-for-captured-authority",
        key_prefix="test",
        user_id=user.id,
        owner_identity=user.account_identity,
        contract_version=1,
        enabled=True,
        can_manage_library=True,
        can_read_status=read,
    )
    db.add(key)
    await db.commit()
    return key, Authority.api_key(key.id, capture=capture_key(key))


@pytest.fixture
def prepared_worker(monkeypatch):
    class Worker:
        async def hash(self, path, **kwargs):
            return {"digest": hashlib.sha256(path.read_bytes()).hexdigest()}

        async def prepare(self, path, **kwargs):
            return SimpleNamespace(metadata={}, thumbnail=None, digest=hashlib.sha256(path.read_bytes()).hexdigest())

    monkeypatch.setattr("backend.app.services.library_file_runtime.get_library_file_runtime", lambda: Worker())


async def test_key_disabled_after_preparation_commits_no_upload_or_repair(db_session, monkeypatch, prepared_worker):
    key, authority = await actor(db_session)
    key_id = key.id
    payload = content()
    old = LibraryFile(
        filename="existing.3mf",
        file_path="library/files/missing.3mf",
        file_type="3mf",
        file_hash=hashlib.sha256(payload).hexdigest(),
        file_size=len(payload),
        created_by_id=key.user_id,
    )
    db_session.add(old)
    await db_session.commit()
    old_id = old.id
    real_prepare = library_ingest.prepare_reuse

    async def revoke_after_prepare(db, **kwargs):
        presence = await real_prepare(db, **kwargs)
        assert not db.in_transaction(), "Presence I/O must finish without a principal/DB writer"
        await db.execute(update(APIKey).where(APIKey.id == key_id).values(enabled=False))
        await db.commit()
        return presence

    monkeypatch.setattr(library_ingest, "prepare_reuse", revoke_after_prepare)
    with pytest.raises(HTTPException) as exc:
        await library.store_library_upload(
            db_session, filename="incoming.3mf", content=payload, target_folder=None, authority=authority
        )
    assert exc.value.status_code == 401
    saved = await db_session.get(LibraryFile, old_id, populate_existing=True)
    assert saved.file_path == "library/files/missing.3mf"
    assert await db_session.scalar(select(func.count()).select_from(LibraryFile)) == 1
    assert not list(library.get_library_files_dir().glob("*.3mf"))


async def test_hidden_duplicate_is_refused_before_presence_probe(db_session, monkeypatch, prepared_worker):
    key, authority = await actor(db_session, read=False)
    payload = content()
    db_session.add(
        LibraryFile(
            filename="private-name.3mf",
            file_path="library/files/private.3mf",
            file_type="3mf",
            file_hash=hashlib.sha256(payload).hexdigest(),
            file_size=len(payload),
        )
    )
    await db_session.commit()

    def forbidden_probe(row):
        raise AssertionError("Hidden byte presence must not be probed")

    monkeypatch.setattr(library_ingest, "_file_present", forbidden_probe)
    with pytest.raises(HTTPException) as exc:
        await library.store_library_upload(
            db_session, filename="incoming.3mf", content=payload, target_folder=None, authority=authority
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == {"code": "source_unavailable"}
    assert await db_session.scalar(select(func.count()).select_from(LibraryFile)) == 1


async def test_captured_upload_publishes_under_its_current_owner(db_session, prepared_worker):
    key, authority = await actor(db_session)
    owner_id = key.user_id
    saved = await library.store_library_upload(
        db_session, filename="model.3mf", content=content(), target_folder=None, authority=authority
    )
    assert saved.outcome == "created"
    assert saved.file.created_by_id == owner_id
