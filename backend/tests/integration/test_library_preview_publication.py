"""Two independent DB sessions and real files: preview publication is a CAS."""

import asyncio
import os
import uuid
from datetime import datetime

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, import_all_models
from backend.app.models.library import LibraryFile
from backend.app.services.library_preview import Source, attach


@pytest.fixture
async def publication_db(tmp_path, monkeypatch):
    import_all_models()
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def seed(factory):
    async with factory() as db:
        row = LibraryFile(
            filename="cube.stl",
            file_path="library/cube.stl",
            file_type="stl",
            file_size=1000,
            file_hash="a" * 64,
            thumbnail_path="library/old.png",
        )
        db.add(row)
        await db.commit()
        return Source.capture(row)


def png(tmp_path):
    from PIL import Image

    path = tmp_path / "render.png"
    Image.new("RGBA", (32, 32), (0, 170, 30, 255)).save(path)
    return path


async def exercise_cas(factory, tmp_path):
    source = await seed(factory)
    output = png(tmp_path)
    async with factory() as first, factory() as second:
        outcomes = await asyncio.gather(attach(first, source, output), attach(second, source, output))
    assert sorted(outcomes) == [False, True]
    async with factory() as db:
        row = await db.get(LibraryFile, source.id)
        assert row.thumbnail_path != source.thumbnail_path
        assert (settings.base_dir / row.thumbnail_path).is_file()
    assert len(list((settings.library_dir / "thumbnails").glob("*.png"))) == 1


@pytest.mark.asyncio
async def test_independent_sqlite_cas(publication_db, tmp_path):
    await exercise_cas(publication_db, tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        {"deleted_at": datetime(2026, 9, 23)},
        {"file_hash": "b" * 64},
        {"thumbnail_path": "newer.png"},
        {"file_path": "replacement.stl"},
        {"purge": True},
    ],
)
@pytest.mark.asyncio
async def test_deleted_replaced_or_newer_preview_rejects_late_attachment(publication_db, tmp_path, change):
    source = await seed(publication_db)
    old = settings.base_dir / source.thumbnail_path
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(png(tmp_path).read_bytes())
    old_bytes = old.read_bytes()
    async with publication_db() as db:
        if change.get("purge"):
            await db.execute(delete(LibraryFile).where(LibraryFile.id == source.id))
        else:
            await db.execute(update(LibraryFile).where(LibraryFile.id == source.id).values(**change))
        await db.commit()
    async with publication_db() as db:
        assert not await attach(db, source, png(tmp_path))
    assert not list((settings.library_dir / "thumbnails").glob("*.png"))
    assert old.read_bytes() == old_bytes


@pytest.mark.asyncio
async def test_unknown_commit_reference_retains_output(publication_db, tmp_path, monkeypatch):
    import backend.app.services.library_preview as module

    source = await seed(publication_db)

    class Unknown(AsyncSession):
        async def commit(self):
            await super().commit()
            raise OSError("lost ACK")

        async def scalar(self, *args, **kwargs):
            raise OSError("reference check also unavailable")

    maker = async_sessionmaker
    monkeypatch.setattr(module, "async_sessionmaker", lambda bind, **kw: maker(bind, class_=Unknown, **kw))
    async with publication_db() as db:
        assert not await attach(db, source, png(tmp_path))
    async with publication_db() as db:
        path = await db.scalar(select(LibraryFile.thumbnail_path).where(LibraryFile.id == source.id))
    assert (settings.base_dir / path).is_file()


@pytest.mark.asyncio
async def test_lost_commit_ack_preserves_referenced_png(publication_db, tmp_path, monkeypatch):
    import backend.app.services.library_preview as module

    source = await seed(publication_db)

    class LostAck(AsyncSession):
        async def commit(self):
            await super().commit()
            raise OSError("lost commit acknowledgement")

    maker = async_sessionmaker
    monkeypatch.setattr(module, "async_sessionmaker", lambda bind, **kw: maker(bind, class_=LostAck, **kw))
    async with publication_db() as db:
        assert not await attach(db, source, png(tmp_path))
    async with publication_db() as db:
        path = await db.scalar(select(LibraryFile.thumbnail_path).where(LibraryFile.id == source.id))
        assert path != source.thumbnail_path
        assert (settings.base_dir / path).is_file()


def test_backup_map_excludes_only_runtime_not_final_previews():
    from backend.app.services.backup_files import directories

    roots = directories(settings)
    assert roots["library"] == settings.library_dir
    ephemeral = settings.base_dir / ".cache" / "preview-service"
    assert all(not ephemeral.is_relative_to(root) for root in roots.values())


@pytest.mark.asyncio
async def test_upload_commits_before_preview_and_off_dedup_never_render(publication_db, tmp_path, monkeypatch):
    import trimesh

    from backend.app.api.routes import library

    calls = []

    async def held_render(db, source):
        assert not db.in_transaction()
        # Independent writer succeeds while the upload caller is awaiting us.
        async with publication_db() as writer:
            row = await writer.get(LibraryFile, source.id)
            assert row and row.thumbnail_path is None
            row.notes = "writer is not held by preview"
            await writer.commit()
        calls.append(source.id)
        return False

    monkeypatch.setattr(library, "generate_library_preview", held_render)
    content = trimesh.creation.box().export(file_type="stl")
    async with publication_db() as db:
        first = await library.store_library_upload(db, filename="cube.stl", content=content, target_folder=None)
        assert first.outcome == "created"
        duplicate = await library.store_library_upload(db, filename="copy.stl", content=content, target_folder=None)
        assert duplicate.outcome == "deduped"
        await library.store_library_upload(
            db, filename="other.stl", content=content + b"\n", target_folder=None, generate_stl_thumbnails=False
        )
    assert calls == [first.file.id]


@pytest.mark.asyncio
async def test_telegram_and_product_import_use_committed_shared_preview(publication_db, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import trimesh

    from backend.app.api.routes import library
    from backend.app.core import database
    from backend.app.services.product_card import _ingest_into_library
    from backend.app.services.telegram_handlers import library_upload_scene as scene

    calls = []

    async def preview(db, source):
        assert not db.in_transaction()
        calls.append(source.filename)
        return False

    monkeypatch.setattr(library, "generate_library_preview", preview)
    monkeypatch.setattr(database, "async_session", publication_db)
    monkeypatch.setattr(scene, "get_language", AsyncMock(return_value="en"))
    content = trimesh.creation.box().export(file_type="stl")

    async def download(file_id, destination):
        destination.write(content)

    callback = SimpleNamespace(
        data="libup:folder:0",
        answer=AsyncMock(),
        message=SimpleNamespace(edit_text=AsyncMock()),
        bot=SimpleNamespace(download=download),
    )
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"file_id": "tg1", "file_name": "telegram.stl"}), clear=AsyncMock()
    )
    await scene.cb_folder_chosen(callback, state)
    assert calls == ["telegram.stl"]
    assert "saved" in callback.message.edit_text.call_args.args[0].lower()
    async with publication_db() as db:
        result = await _ingest_into_library(
            db, filename="product.stl", content=content + b"\n", target_folder=None, user=None
        )
        assert result.outcome == "created"
    assert calls == ["telegram.stl", "product.stl"]


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("PREVIEW_TEST_PG_DSN"), reason="isolated Docker PostgreSQL acceptance only")
async def test_preview_publication_docker_postgres(tmp_path, monkeypatch):
    # This test-only DSN is not DATABASE_URL and never changes the app's DB.
    # All DDL/data lives in a fresh schema owned solely by this invocation.
    schema = "preview_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        os.environ["PREVIEW_TEST_PG_DSN"], execution_options={"schema_translate_map": {None: schema}}
    )
    import_all_models()
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(settings, "base_dir", tmp_path)
        monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        await exercise_cas(factory, tmp_path)
        await test_upload_commits_before_preview_and_off_dedup_never_render(factory, tmp_path, monkeypatch)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
