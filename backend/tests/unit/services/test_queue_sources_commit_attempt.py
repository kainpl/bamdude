"""A publication whose commit was ATTEMPTED never removes the object it installed (OrcaBamDude B1, addendum C1).

``queue_sources.publish`` runs ``attach`` and commits. A commit can reach the database and still raise in the
caller -- the connection drops while COMMIT is in flight, or a cancellation lands in it -- and a rollback after that
proves nothing. The publication used to unlink the object it had just installed whenever anything raised past the
rename, so a commit that had in fact gone through left committed job rows naming bytes that were gone.

The rule now: once ``session.commit()`` has been called, no exception path of this publication unlinks the object.
If nothing was committed, no row names it, and the ordinary GC releases it after its grace; a failure BEFORE the
commit still removes it at once, as it always did.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import FORMAT_3MF, QueueSource
from backend.app.services import queue_sources
from backend.tests.unit.services.test_queue_sources_gc import (  # noqa: F401
    age,
    clean_spool_state,
    clockface,
    collect,
    make_3mf,
    object_of,
    sessions,
)

pytestmark = pytest.mark.unit


async def _capture(path: Path, payload: bytes = b"<model/>", *, same_bytes_as: Path | None = None):
    """Capture a 3MF. ``same_bytes_as`` copies that file instead of building another: a zip stamps its members with
    the current time to two seconds, so two builds a slow publication apart are other bytes (seen on PostgreSQL under
    load, 2026-11-08)."""
    if same_bytes_as is None:
        make_3mf(path, payload=payload)
    else:
        path.write_bytes(same_bytes_as.read_bytes())
    return await queue_sources.capture(
        queue_sources.CaptureRequest(path=path, format=FORMAT_3MF, display_filename=path.name)
    )


@pytest.fixture
async def queue_id(sessions):
    """A real printer and its queue: PostgreSQL enforces the job row's foreign key, SQLite does not."""
    async with sessions() as session:
        printer = Printer(name="B1", serial_number="B1-SERIAL", ip_address="192.0.2.1", access_code="x")
        session.add(printer)
        await session.flush()
        session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
        await session.commit()
        return printer.id


def _attach_a_job(queue_id: int):
    async def attach(session: AsyncSession, source: QueueSource) -> None:
        # The job row a writer commits together with the source -- what the bytes exist for.
        session.add(PrintQueueItem(queue_id=queue_id, status="pending", queue_source_id=source.id))
        await session.flush()

    return attach


def _commit_once(monkeypatch, *, reach_the_database: bool, error: BaseException):
    """The next commit raises -- after the real commit (it went through) or instead of it (it never happened)."""
    real_commit = AsyncSession.commit
    spent = []

    async def commit(self):
        if spent:
            return await real_commit(self)
        spent.append(True)
        if reach_the_database:
            await real_commit(self)
        raise error

    monkeypatch.setattr(AsyncSession, "commit", commit)
    return spent


async def _counts(sessions) -> tuple[int, int]:
    async with sessions() as session:
        sources = await session.scalar(select(func.count()).select_from(QueueSource))
        jobs = await session.scalar(select(func.count()).select_from(PrintQueueItem))
    return sources, jobs


def _the_object(receipt) -> Path:
    from backend.app.core.config import settings

    return Path(settings.base_dir) / queue_sources.object_relative_path(receipt.sha256, receipt.format)


@pytest.mark.parametrize(
    "error", [RuntimeError("commit outcome unknown"), asyncio.CancelledError()], ids=["error", "cancellation"]
)
async def test_a_commit_that_went_through_then_raised_keeps_the_new_object(
    tmp_path, sessions, queue_id, monkeypatch, error
):
    receipt = await _capture(tmp_path / "share" / "lamp.3mf")
    target = _the_object(receipt)
    spent = _commit_once(monkeypatch, reach_the_database=True, error=error)

    with pytest.raises(type(error)):
        await queue_sources.publish(receipt, _attach_a_job(queue_id), session_factory=sessions)

    assert spent
    assert await _counts(sessions) == (1, 1)  # the row and its job are committed
    assert target.is_file()  # ... and so are the bytes they name


async def test_a_commit_that_went_through_then_raised_keeps_a_reused_object(tmp_path, sessions, queue_id, monkeypatch):
    first = await _capture(tmp_path / "share" / "lamp.3mf")
    await queue_sources.publish(first, _attach_a_job(queue_id), session_factory=sessions)
    target = _the_object(first)
    second = await _capture(tmp_path / "share" / "again.3mf", same_bytes_as=tmp_path / "share" / "lamp.3mf")
    assert second.sha256 == first.sha256
    _commit_once(monkeypatch, reach_the_database=True, error=RuntimeError("commit outcome unknown"))

    with pytest.raises(RuntimeError):
        await queue_sources.publish(second, _attach_a_job(queue_id), session_factory=sessions)

    assert await _counts(sessions) == (1, 2)
    assert target.is_file()


async def test_a_commit_that_never_happened_leaves_the_object_to_the_gc(
    tmp_path, sessions, queue_id, monkeypatch, clockface
):
    receipt = await _capture(tmp_path / "share" / "lamp.3mf")
    target = _the_object(receipt)
    _commit_once(monkeypatch, reach_the_database=False, error=RuntimeError("commit outcome unknown"))

    with pytest.raises(RuntimeError):
        await queue_sources.publish(receipt, _attach_a_job(queue_id), session_factory=sessions)

    # Nothing committed -- but the publication cannot know that, so the bytes stay for the GC to judge.
    assert await _counts(sessions) == (0, 0)
    assert target.is_file()

    age(target, clockface.epoch(-queue_sources.ORPHAN_GRACE_SECONDS + 30))
    inside = await collect(sessions, clockface.at(0))
    assert inside.orphan_objects == 0 and target.is_file()

    age(target, clockface.epoch(-queue_sources.ORPHAN_GRACE_SECONDS - 30))
    past = await collect(sessions, clockface.at(0))
    assert past.orphan_objects == 1 and not target.exists()


async def test_the_gc_keeps_the_object_a_committed_row_names(tmp_path, sessions, queue_id, monkeypatch, clockface):
    receipt = await _capture(tmp_path / "share" / "lamp.3mf")
    target = _the_object(receipt)
    _commit_once(monkeypatch, reach_the_database=True, error=RuntimeError("commit outcome unknown"))
    with pytest.raises(RuntimeError):
        await queue_sources.publish(receipt, _attach_a_job(queue_id), session_factory=sessions)

    age(target, clockface.epoch(-queue_sources.ORPHAN_GRACE_SECONDS - 30))
    report = await collect(sessions, clockface.past_grace())

    assert report.orphan_objects == 0
    assert target.is_file()
    assert await _counts(sessions) == (1, 1)


async def test_a_failure_before_the_commit_still_removes_the_new_object(tmp_path, sessions, queue_id):
    receipt = await _capture(tmp_path / "share" / "lamp.3mf")
    target = _the_object(receipt)

    async def refuse(session: AsyncSession, source: QueueSource) -> None:
        await _attach_a_job(queue_id)(session, source)
        raise RuntimeError("the writer refused before committing")

    with pytest.raises(RuntimeError):
        await queue_sources.publish(receipt, refuse, session_factory=sessions)

    assert await _counts(sessions) == (0, 0)
    assert not target.exists()
