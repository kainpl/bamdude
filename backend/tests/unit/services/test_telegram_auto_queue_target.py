"""Choosing "any printer of model X" in the bot must reach the auto-queue.

The buttons were drawn, the handler stored `printer_id=None, target_model=X`,
and the confirm step opened with `if not printer_id: … failed`. So every model
target rendered, invited a press, and refused it — the UI of a feature whose
backend was never wired. The map even described the target as working.

Auto-queue is the tier that routes a job to whichever machine can take it, so
"any printer of model X" is precisely what it is for.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.services.telegram_handlers.queue_scene import _add_to_auto_queue


@pytest.fixture
def session_factory(test_engine):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("backend.app.core.database.async_session", maker):
        yield maker


def _callback():
    return SimpleNamespace(answer=AsyncMock(), message=SimpleNamespace())


async def _library_file(db, *, path: str | None = "library/cube.3mf") -> LibraryFile:
    lib = LibraryFile(filename="cube.3mf", file_path=path, file_type="3mf", file_size=1)
    db.add(lib)
    await db.commit()
    return lib


@pytest.mark.asyncio
async def test_a_model_target_creates_an_auto_queue_item(db_session, session_factory):
    lib = await _library_file(db_session)

    with patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()):
        await _add_to_auto_queue(_callback(), "en", lib.id, "X2D", None)

    items = (await db_session.execute(select(AutoQueueItem))).scalars().all()
    assert len(items) == 1
    assert items[0].target_model == "X2D"
    assert items[0].library_file_id == lib.id
    assert items[0].status == "pending"


@pytest.mark.asyncio
async def test_the_routing_requirements_come_out_of_the_3mf(db_session, session_factory):
    """Same inputs the web route fills in. An item queued from the bot that
    matched on model alone would behave differently from the same file queued
    from the browser — two behaviours for one action."""
    lib = await _library_file(db_session)
    reqs = SimpleNamespace(required_filament_types=["PLA", "PETG"], target_model=None, print_time_seconds=None)

    with (
        patch("backend.app.services.auto_queue_threemf.extract_auto_queue_requirements", return_value=reqs),
        patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()),
    ):
        await _add_to_auto_queue(_callback(), "en", lib.id, "X2D", None)

    item = (await db_session.execute(select(AutoQueueItem))).scalar_one()
    assert json.loads(item.required_filament_types) == ["PLA", "PETG"]


@pytest.mark.asyncio
async def test_a_file_we_cannot_read_still_queues_on_its_model(db_session, session_factory):
    """⚠️ Refusing here would be worse than routing loosely: the operator
    picked a target that is perfectly valid, and the extractor is documented
    never to raise."""
    lib = await _library_file(db_session)

    with (
        patch(
            "backend.app.services.auto_queue_threemf.extract_auto_queue_requirements",
            side_effect=OSError("truncated"),
        ),
        patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()),
    ):
        await _add_to_auto_queue(_callback(), "en", lib.id, "X2D", None)

    item = (await db_session.execute(select(AutoQueueItem))).scalar_one()
    assert item.target_model == "X2D"
    assert item.required_filament_types is None


@pytest.mark.asyncio
async def test_the_auto_queue_keeps_one_global_ordering(db_session, session_factory):
    """Unlike the per-printer queues, this tier is a single list the
    distributor walks — so position counts across the whole table."""
    lib = await _library_file(db_session)
    db_session.add(AutoQueueItem(library_file_id=lib.id, target_model="P1S", status="pending", position=7))
    await db_session.commit()

    with patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()):
        await _add_to_auto_queue(_callback(), "en", lib.id, "X2D", None)

    positions = sorted((await db_session.execute(select(AutoQueueItem.position))).scalars().all())
    assert positions == [7, 8]


@pytest.mark.asyncio
async def test_a_missing_file_is_refused_rather_than_queued_empty(db_session, session_factory):
    callback = _callback()

    with patch("backend.app.services.telegram_handlers.queue.render_queue", new=AsyncMock()):
        await _add_to_auto_queue(callback, "en", 999_999, "X2D", None)

    assert (await db_session.execute(select(AutoQueueItem))).scalars().all() == []
    assert callback.answer.await_args.kwargs.get("show_alert") is True
