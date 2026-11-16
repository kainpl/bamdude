"""A library PDF gets a first-page thumbnail through the REAL chain: main -> preview service -> disposable child ->
main's bound publication. No fake worker: a small PDF written by the test is rendered end to end, and what the
operator can read of it is asserted in the parent's own logger."""

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


# --- the runtime alone ------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_real_pdf_is_rendered_by_the_real_child_and_the_parent_log_says_so(tmp_path, real_runtime, caplog):
    from PIL import Image

    caplog.set_level(logging.INFO, logger=RUNTIME_LOG)
    source = tmp_path / "manual.pdf"
    source.write_bytes(pdf_fixtures.page_pdf(200, 300))
    async with real_runtime.attempt({"document": (source, "pdf")}) as result:
        assert (result.outcome, result.kind) == ("ok", "pdf")
        with Image.open(result.files["preview"]) as picture:
            assert picture.format == "PNG" and picture.size == (342, 512)
        attempt_id = result.attempt_id
    assert f"Preview attempt start attempt={attempt_id} kind=pdf" in caplog.text
    assert re.search(rf"Preview attempt={attempt_id} kind=pdf outcome=ok stage=None elapsed_ms=\d+", caplog.text)
    assert str(source) not in caplog.text


@pytest.mark.parametrize(
    ("content", "outcome", "why"),
    [
        (pdf_fixtures.BROKEN_PDF, "render_failed", "pdf_unreadable"),
        (pdf_fixtures.encrypted_pdf(), "render_failed", "pdf_encrypted"),
        (pdf_fixtures.page_pdf(20000, 20000), "resource_limit", "pdf_page_size"),
    ],
    ids=["broken", "encrypted", "oversized-page"],
)
@pytest.mark.asyncio
async def test_a_pdf_that_cannot_be_pictured_ends_with_one_named_parent_line(
    tmp_path, real_runtime, caplog, content, outcome, why
):
    caplog.set_level(logging.INFO, logger=RUNTIME_LOG)
    source = tmp_path / "manual.pdf"
    source.write_bytes(content)
    async with real_runtime.attempt({"document": (source, "pdf")}) as result:
        assert (result.outcome, result.why, result.by, result.exit_code) == (outcome, why, "pdf_png", 0)
        assert not result.files
        attempt_id = result.attempt_id
    assert (
        f"Preview attempt={attempt_id} kind=pdf outcome={outcome} stage=None why={why} by=pdf_png exit=0 "
        in caplog.text
    )
    assert str(source) not in caplog.text and str(tmp_path) not in caplog.text
    # the service is healthy afterwards: the next attempt runs
    source.write_bytes(pdf_fixtures.page_pdf())
    async with real_runtime.attempt({"document": (source, "pdf")}) as result:
        assert result.outcome == "ok"


@pytest.mark.asyncio
async def test_a_document_never_rides_with_another_input(tmp_path, real_runtime):
    source = tmp_path / "manual.pdf"
    source.write_bytes(pdf_fixtures.page_pdf())
    async with real_runtime.attempt({"document": (source, "pdf"), "source_png": (source, "png")}) as result:
        assert result.outcome == "protocol_error" and result.attempt_id is None


def test_a_pdf_is_an_ordinary_priority_attempt_never_behind_step():
    assert preview_runtime.PRIORITY_BY_KIND["pdf"] == "normal"


# --- main's own budgets and the broken install ----------------------------------------------------------------------------


async def _row(factory, tmp_path, content: bytes, name: str = "manual.pdf"):
    path = settings.library_dir / "files" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    async with factory() as db:
        row = LibraryFile(
            filename=name,
            file_path=str(path.relative_to(settings.base_dir)).replace("\\", "/"),
            file_type="pdf",
            file_size=len(content),
        )
        db.add(row)
        await db.commit()
        return library_preview.Source.capture(row)


@pytest.mark.asyncio
async def test_a_pdf_over_the_input_budget_is_refused_before_any_attempt(library_db, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="backend.app.services.library_preview")
    monkeypatch.setattr(preview_protocol, "PDF_INPUT_BYTES", 64)

    def never(*args, **kwargs):
        raise AssertionError("an attempt was started for an oversized PDF")

    monkeypatch.setattr(library_preview, "preview_attempt", never)
    source = await _row(library_db, tmp_path, pdf_fixtures.page_pdf())
    async with library_db() as db:
        assert await library_preview.generate(db, source) is False
    assert f"library file {source.id} not made: outcome=resource_limit why=pdf_input_bytes" in caplog.text


@pytest.mark.asyncio
async def test_the_pdf_attempt_runs_on_its_own_time_budget(library_db, tmp_path, monkeypatch):
    import time
    from contextlib import asynccontextmanager

    seen = {}

    @asynccontextmanager
    async def attempt(inputs, **kwargs):
        seen["inputs"] = inputs
        seen["deadline"] = kwargs["on_admit"]()  # counted from the moment the slot is granted
        yield preview_runtime.PreviewResult(outcome="busy")

    monkeypatch.setattr(library_preview, "preview_attempt", attempt)
    source = await _row(library_db, tmp_path, pdf_fixtures.page_pdf())
    before = time.monotonic_ns()
    async with library_db() as db:
        assert await library_preview.generate(db, source) is False
    assert set(seen["inputs"]) == {"document"} and seen["inputs"]["document"][1] == "pdf"
    assert 0 < seen["deadline"] - before <= int((preview_protocol.PDF_SECONDS + 1) * 1e9)


@pytest.mark.asyncio
async def test_a_missing_renderer_is_reported_once_and_not_asked_again(library_db, tmp_path, monkeypatch, caplog):
    from contextlib import asynccontextmanager

    caplog.set_level(logging.INFO, logger="backend.app.services.library_preview")
    attempts = []

    @asynccontextmanager
    async def attempt(inputs, **kwargs):
        attempts.append(inputs)
        yield preview_runtime.PreviewResult(outcome="render_failed", why="pdf_renderer_missing", by="pdf_png")

    monkeypatch.setattr(library_preview, "preview_attempt", attempt)
    first = await _row(library_db, tmp_path, pdf_fixtures.page_pdf(), "a.pdf")
    second = await _row(library_db, tmp_path, pdf_fixtures.page_pdf(), "b.pdf")
    async with library_db() as db:
        assert await library_preview.generate(db, first) is False
        assert await library_preview.generate(db, second) is False
    assert len(attempts) == 1
    assert caplog.text.count("PDF thumbnails are unavailable") == 1
    assert [record.levelname for record in caplog.records if "PDF thumbnails are unavailable" in record.message] == [
        "WARNING"
    ]
