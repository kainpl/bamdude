"""The finish photo's own terminal callback, run for real (G10 G, review G1–G2).

``_background_finish_photo`` is a nested function of the completion handler, so
it is taken out of the handler's code object and given its closure by hand — the
function that runs here is the very code production runs, against a real
database and real files. Only the camera is a canned frame.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from types import CodeType, FunctionType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app import main
from backend.app.models.archive import PrintArchive
from backend.app.services import finish_photo_store
from backend.app.utils import archive_paths

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

GENERATION = 548


def _nested(name: str, **bindings):
    """The nested function ``name`` of the completion handler, closed over ``bindings``."""
    parent = main._on_print_complete_impl
    code = next(c for c in parent.__code__.co_consts if isinstance(c, CodeType) and c.co_name == name)
    missing = [free for free in code.co_freevars if free not in bindings]
    assert not missing, f"{name} closes over {missing}: the test must bind them"

    def cell(value):
        return (lambda: value).__closure__[0]

    return FunctionType(code, parent.__globals__, name, None, tuple(cell(bindings[free]) for free in code.co_freevars))


@pytest.fixture
async def terminal(async_client, db_session, archive_factory, printer_factory, tmp_path, monkeypatch):
    from backend.app.api.routes import settings as settings_routes
    from backend.app.core import database

    printer = await printer_factory()
    archive = await archive_factory(printer.id, file_path="")
    monkeypatch.setattr(main, "async_session", database.async_session)
    monkeypatch.setattr(archive_paths.settings, "base_dir", tmp_path)
    monkeypatch.setattr(archive_paths.settings, "archive_dir", tmp_path / "archive")
    monkeypatch.setattr(settings_routes, "get_setting", AsyncMock(return_value="true"))
    stages = AsyncMock()
    monkeypatch.setattr(main, "_record_terminal_effect_stage", stages)
    finish_photo_store._own.clear()
    box = SimpleNamespace(
        printer_id=printer.id,
        archive_id=archive.id,
        identity=archive.recovery_identity,
        root=tmp_path,
        owns=True,
        stages=stages,
    )

    def run(**overrides):
        bindings = {
            "archive_id": box.archive_id,
            "terminal_archive_identity": box.identity,
            "printer_id": box.printer_id,
            "effect_token": SimpleNamespace(generation=GENERATION),
            "data": {},
            "logger": logging.getLogger("finish-photo-terminal"),
            "_owns_printer_effect": lambda: box.owns,
        }
        bindings.update(overrides)
        return _nested("_background_finish_photo", **bindings)()

    box.run = run
    yield box
    finish_photo_store._own.clear()


def _bank(monkeypatch, box, frame: bytes = b"\xff\xd8banked-at-the-last-layer") -> None:
    monkeypatch.setitem(main._stage22_finish_frames, (box.printer_id, GENERATION), frame)


def _photos(box) -> list:
    return sorted(box.root.rglob("finish_*.jpg"))


async def _listed(db, box) -> list:
    return list(
        (await db.execute(select(PrintArchive.photos).where(PrintArchive.id == box.archive_id))).scalar_one() or []
    )


def _stage_names(box) -> list:
    return [call.args[2] for call in box.stages.await_args_list]


async def test_a_banked_frame_becomes_the_archives_photo(terminal, monkeypatch, db_session):
    _bank(monkeypatch, terminal)
    name = await terminal.run()
    assert name
    assert [path.name for path in _photos(terminal)] == [name]
    assert await _listed(db_session, terminal) == [name]
    assert finish_photo_store._own == {}  # attached: no longer the store's to remove
    assert _stage_names(terminal)[-1] == "finished"


async def test_a_commit_that_fails_leaves_no_file(terminal, monkeypatch, db_session):
    """Codex G2: the frame was written, the commit raised, and the JPG stayed with nothing naming it."""
    from sqlalchemy.ext.asyncio import AsyncSession

    _bank(monkeypatch, terminal)
    real = AsyncSession.commit

    async def commit(session):
        if any(isinstance(row, PrintArchive) and row.photos for row in session.dirty):
            await session.rollback()
            raise RuntimeError("a controlled commit failure")
        return await real(session)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    assert await terminal.run() is None
    monkeypatch.setattr(AsyncSession, "commit", real)
    assert _photos(terminal) == []
    assert await _listed(db_session, terminal) == []
    assert finish_photo_store._own == {}
    assert _stage_names(terminal)[-1] == "failed"


async def test_a_commit_that_took_but_answered_with_an_error_keeps_the_photo(terminal, monkeypatch, db_session):
    """The other half of "unknown": the row was written; removing the file would leave a dead name."""
    from sqlalchemy.ext.asyncio import AsyncSession

    _bank(monkeypatch, terminal)
    real = AsyncSession.commit

    async def commit(session):
        mine = any(isinstance(row, PrintArchive) and row.photos for row in session.dirty)
        await real(session)
        if mine:
            raise RuntimeError("the answer to the commit was lost")

    monkeypatch.setattr(AsyncSession, "commit", commit)
    name = await terminal.run()
    monkeypatch.setattr(AsyncSession, "commit", real)
    assert name and [path.name for path in _photos(terminal)] == [name]
    assert await _listed(db_session, terminal) == [name]


async def test_a_printer_that_went_on_to_another_print_gets_no_photo_and_no_file(terminal, monkeypatch, db_session):
    _bank(monkeypatch, terminal)
    calls = {"n": 0}

    def owns():
        # The terminal's own checks pass until the frame is about to be attached.
        calls["n"] += 1
        return calls["n"] <= 3

    assert await terminal.run(_owns_printer_effect=owns) is None
    assert _photos(terminal) == [] and await _listed(db_session, terminal) == []
    assert finish_photo_store._own == {}


async def test_the_built_in_camera_writes_where_the_archive_is_after_the_capture(terminal, monkeypatch, db_session):
    """Codex G1, through the real callback: the 3MF arrives while the camera is being read."""
    from backend.app.services import camera_runtime

    async def capture(_request):
        # During the capture the archive gets its 3MF: its folder is another one now.
        async with main.async_session() as db:
            row = await db.get(PrintArchive, terminal.archive_id)
            row.file_path = "archive/9/arrived/model.gcode.3mf"
            await db.commit()
        return SimpleNamespace(frame=b"\xff\xd8fresh-from-the-camera")

    monkeypatch.setattr(camera_runtime, "capture", capture)
    monkeypatch.setattr("backend.app.api.routes.camera.live_frame_for_picture", lambda _pid, _printer: (False, None))
    monkeypatch.setattr("backend.app.api.routes.camera.get_buffered_frame", lambda _pid: None)
    monkeypatch.setattr("backend.app.api.routes.camera.is_stream_active", lambda _pid: False)
    name = await terminal.run()
    assert name
    written = _photos(terminal)
    assert [path.name for path in written] == [name]
    assert written[0].parent == terminal.root / "archive" / "9" / "arrived" / "photos"
    assert await _listed(db_session, terminal) == [name]


async def test_a_cancel_during_the_photos_write_leaves_no_file(terminal, monkeypatch, db_session):
    """Codex G2-R: the cleanup ran before the write's thread created the JPG, which then stayed."""
    _bank(monkeypatch, terminal, b"\xff\xd8a frame being written")
    entered, release = threading.Event(), threading.Event()
    real = Path.write_bytes

    def slow(path, data):
        if not path.name.startswith("finish_"):
            return real(path, data)
        entered.set()
        assert release.wait(10)
        return real(path, data)

    monkeypatch.setattr(Path, "write_bytes", slow)
    task = asyncio.create_task(terminal.run())
    try:
        assert await asyncio.to_thread(entered.wait, 4)
        task.cancel()
        await asyncio.sleep(0.15)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _listed(db_session, terminal) == []
    assert _photos(terminal) == []
    assert finish_photo_store._own == {}


@pytest.mark.parametrize("committed", [False, True], ids=["rolled-back", "committed"])
async def test_a_cancel_inside_the_commit_settles_the_photo_and_is_passed_on(
    terminal, monkeypatch, db_session, committed
):
    """Codex G2-R: a cancel inside the commit left the file, or the store's record of it, unsettled."""
    from sqlalchemy.ext.asyncio import AsyncSession

    _bank(monkeypatch, terminal)
    real = AsyncSession.commit

    async def commit(session):
        if any(isinstance(row, PrintArchive) and row.photos for row in session.dirty):
            if committed:
                await real(session)
            else:
                await session.rollback()
            raise asyncio.CancelledError()
        return await real(session)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    with pytest.raises(asyncio.CancelledError):
        await terminal.run()
    monkeypatch.setattr(AsyncSession, "commit", real)
    listed = await _listed(db_session, terminal)
    assert bool(listed) is committed
    assert [path.name for path in _photos(terminal)] == listed  # committed: kept with the archive; else gone
    assert finish_photo_store._own == {}
