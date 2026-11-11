"""The restore and a plate answer on the same gate serialise on its archive, in either order (spec B6, T13).

Two sessions on a WAL file, one holding its archive guard on an event: the other must wait, then both finish
with one consistent outcome and no deadlock.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import (
    plate_answers,
    print_state_recovery_preview as tokens,
    print_state_recovery_store as store,
)
from backend.app.services.plate_hold import PlateOwnerRequeued
from backend.app.services.print_state_recovery import GATE_OWNER_ACTION
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def wal_requeued(tmp_path, monkeypatch):
    database.import_all_models()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gate-owner.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def connection(dbapi, _):
        dbapi.execute("PRAGMA journal_mode=WAL")
        dbapi.execute("PRAGMA busy_timeout=300")

    async with engine.begin() as db:
        await db.run_sync(database.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    claim = datetime.now(timezone.utc) - timedelta(hours=2)
    async with factory() as db:
        printer = Printer(name="Synthetic", serial_number="gate-owner", ip_address="127.0.0.1", access_code="00000000")
        db.add(printer)
        await db.flush()
        queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="paused", is_paused=True, pending_count=1)
        db.add(queue)
        await db.flush()
        archive = PrintArchive(
            printer_id=printer.id,
            queue_id=queue.id,
            filename="aborted.gcode.3mf",
            file_path="synthetic",
            file_size=0,
            status="cancelled",
            started_at=claim + timedelta(seconds=1),
            completed_at=claim + timedelta(minutes=45),
            extra_data={},
        )
        db.add(archive)
        await db.flush()
        row = PrintQueueItem(
            queue_id=queue.id,
            archive_id=archive.id,
            status="pending",
            position=1,
            started_at=claim,
            dispatch_attempts=0,
            library_file_id=1,
        )
        db.add(row)
        printer.awaiting_plate_clear = True
        printer.awaiting_plate_clear_archive_id = archive.id
        printer.awaiting_plate_clear_token = "gate-token"
        await db.commit()
        ids = printer.id, archive.id, row.id
    yield factory, ids
    await engine.dispose()


def held(monkeypatch, module, name):
    real = getattr(module, name)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def wrapper(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return entered, release


async def clear(factory, pid):
    async with factory() as db:
        return await plate_answers.answer_plate_run(db, printer_id=pid, expected_archive_id=None, action="clear")


async def final_state(factory, pid, row_id):
    async with factory() as db:
        printer = await db.get(Printer, pid)
        row = await db.get(PrintQueueItem, row_id)
        return row.status, printer.awaiting_plate_clear


async def contend(first, second_factory, entered, release):
    first_task = asyncio.create_task(first)
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(second_factory())
        await asyncio.sleep(0.1)
        assert not second.done(), "the second writer must wait on the first one's archive"
        release.set()
        return await asyncio.wait_for(asyncio.gather(first_task, second, return_exceptions=True), 5)
    finally:
        release.set()
        for task in (first_task, second):
            if task is not None and not task.done():
                task.cancel()


async def test_restore_first_then_the_waiting_clear_succeeds(wal_requeued, monkeypatch, telemetry):
    factory, (pid, _aid, row_id) = wal_requeued
    async with factory() as db:
        preview = await store.make_preview(db, pid, "item", row_id, GATE_OWNER_ACTION, "user:1")
    entered, release = held(monkeypatch, store, "load_active_archive_for_write")

    restored, answered = await contend(
        store.apply_preview(factory, tokens.verify(preview["preview_token"]), "user:1", "Operator"),
        lambda: clear(factory, pid),
        entered,
        release,
    )

    assert not isinstance(restored, BaseException), restored
    assert not isinstance(answered, BaseException), answered
    assert await final_state(factory, pid, row_id) == ("cancelled", False)


async def test_clear_first_is_refused_then_the_waiting_restore_applies(wal_requeued, monkeypatch, telemetry):
    factory, (pid, _aid, row_id) = wal_requeued
    async with factory() as db:
        preview = await store.make_preview(db, pid, "item", row_id, GATE_OWNER_ACTION, "user:1")
    entered, release = held(monkeypatch, plate_answers, "waiting_archive")

    answered, restored = await contend(
        clear(factory, pid),
        lambda: store.apply_preview(factory, tokens.verify(preview["preview_token"]), "user:1", "Operator"),
        entered,
        release,
    )

    assert isinstance(answered, PlateOwnerRequeued), answered
    assert not isinstance(restored, BaseException), restored
    assert await final_state(factory, pid, row_id) == ("cancelled", True)
