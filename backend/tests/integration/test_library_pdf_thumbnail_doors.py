"""Where a library PDF gets its thumbnail: the shared upload door (a ZIP member goes through it too) and the
«Generate Thumbnails» batch. The upload cases run the REAL chain; a failed picture never fails the upload."""

import logging
import re

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.core.database import Base, import_all_models
from backend.app.models.library import LibraryFile
from backend.app.services import library_preview, preview_protocol, preview_runtime
from backend.tests import pdf_fixtures
from backend.tests.preview_support import started

RUNTIME_LOG = "backend.app.services.preview_runtime"


@pytest.fixture
async def library_db(tmp_path, monkeypatch):
    import_all_models()
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "library_dir", tmp_path / "library")
    monkeypatch.setattr(library_preview, "_pdf_renderer_missing", False)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'library.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def real_runtime(tmp_path, monkeypatch):
    runtime = await started(preview_runtime.PreviewRuntime(tmp_path / "preview"))
    monkeypatch.setattr(preview_runtime, "runtime", runtime)
    try:
        yield runtime
    finally:
        await runtime.stop()


def _thumbnail(row) -> bytes | None:
    return (settings.base_dir / row.thumbnail_path).read_bytes() if row.thumbnail_path else None


# --- the upload door ----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_uploaded_pdf_gets_its_first_page_as_its_thumbnail(library_db, real_runtime):
    from backend.app.api.routes import library

    async with library_db() as db:
        result = await library.store_library_upload(
            db, filename="manual.pdf", content=pdf_fixtures.page_pdf(), target_folder=None
        )
        assert result.outcome == "created" and result.file.file_type == "pdf"
    async with library_db() as db:
        row = await db.get(LibraryFile, result.file.id)
        assert _thumbnail(row)[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_an_upload_succeeds_although_its_thumbnail_failed(library_db, real_runtime, caplog):
    from backend.app.api.routes import library

    caplog.set_level(logging.INFO, logger=RUNTIME_LOG)
    async with library_db() as db:
        result = await library.store_library_upload(
            db, filename="broken.pdf", content=pdf_fixtures.BROKEN_PDF, target_folder=None
        )
        assert result.outcome == "created"
    async with library_db() as db:
        row = await db.get(LibraryFile, result.file.id)
        assert row is not None and row.deleted_at is None and row.thumbnail_path is None
        assert (settings.base_dir / row.file_path).read_bytes() == pdf_fixtures.BROKEN_PDF
    assert "kind=pdf outcome=render_failed stage=None why=pdf_unreadable by=pdf_png exit=0" in caplog.text
    assert f"file={result.file.id}" in caplog.text


@pytest.mark.asyncio
async def test_no_thumbnail_is_asked_when_the_upload_opted_out(library_db, monkeypatch):
    from backend.app.api.routes import library

    async def never(*args, **kwargs):
        raise AssertionError("no picture was asked for")

    monkeypatch.setattr(library, "generate_library_preview", never)
    async with library_db() as db:
        result = await library.store_library_upload(
            db,
            filename="manual.pdf",
            content=pdf_fixtures.page_pdf(),
            target_folder=None,
            generate_stl_thumbnails=False,
        )
        assert result.outcome == "created" and result.file.thumbnail_path is None


# --- «Generate Thumbnails» --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_generate_thumbnails_covers_pdfs_missing_one(async_client, db_session, tmp_path, monkeypatch):
    from backend.app.api.routes import library

    asked = []

    async def render(db, source, **kwargs):
        asked.append((source.id, source.file_type))
        return True

    monkeypatch.setattr(library, "generate_library_preview", render)
    path = tmp_path / "manual.pdf"
    path.write_bytes(pdf_fixtures.page_pdf())  # far below the smallest usable mesh: that floor is not a PDF's
    missing = LibraryFile(filename="manual.pdf", file_path=str(path), file_type="pdf", file_size=path.stat().st_size)
    pictured = LibraryFile(
        filename="done.pdf", file_path=str(path), file_type="pdf", file_size=1, thumbnail_path="library/t.png"
    )
    other = LibraryFile(filename="notes.txt", file_path=str(path), file_type="txt", file_size=1)
    db_session.add_all([missing, pictured, other])
    await db_session.commit()

    response = await async_client.post("/api/v1/library/generate-stl-thumbnails", json={"all_missing": True})
    assert response.status_code == 200
    body = response.json()
    assert asked == [(missing.id, "pdf")]
    assert body["succeeded"] == 1 and body["results"][0]["file_id"] == missing.id

    asked.clear()
    response = await async_client.post("/api/v1/library/generate-stl-thumbnails", json={"file_ids": [pictured.id]})
    assert response.status_code == 200 and asked == [(pictured.id, "pdf")]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_pdf_inside_a_zip_asks_for_its_thumbnail_through_the_same_door(async_client, db_session, monkeypatch):
    import io
    import zipfile

    from backend.app.api.routes import library

    asked = []

    async def render(db, source, **kwargs):
        asked.append((source.filename, source.file_type))
        return False  # no picture: the member is still extracted

    monkeypatch.setattr(library, "generate_library_preview", render)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("docs/manual.pdf", pdf_fixtures.page_pdf())
        zf.writestr("notes.txt", b"plain")
    files = {"file": ("bundle.zip", archive.getvalue(), "application/zip")}
    response = await async_client.post("/api/v1/library/files/extract-zip", files=files)
    assert response.status_code == 200
    body = response.json()
    assert body["extracted"] == 2 and body["errors"] == []
    assert asked == [("manual.pdf", "pdf")]
