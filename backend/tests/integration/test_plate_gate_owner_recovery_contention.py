"""The restore and a plate answer on the same gate serialise, in either order (spec B6, T13; Codex review RG-1).

Two sessions on a WAL file, one stopped on an event inside its operation: the other must wait, then both
finish with one consistent outcome and no deadlock. A Repeat of a row with captured bytes needs the storage
guard as well as the archive's; it takes storage FIRST, as the restore's apply does, so neither ever waits for
the other's second guard while holding its own first.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import QueueSource
from backend.app.services import (
    plate_answers,
    print_state_recovery_preview as tokens,
    print_state_recovery_store as store,
    queue_source_capture,
    queue_sources,
)
from backend.app.services.archive_write_scope import archive_process_lock
from backend.app.services.plate_answers import PlateAnswerBusy
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


# --- A Repeat of a row with captured bytes against a restore (Codex review RG-1) ---------------------------


async def with_captured_source(factory, row_id):
    async with factory() as db:
        source = QueueSource(sha256="a" * 64, size_bytes=1, relative_path="synthetic", format="3mf", state="ready")
        db.add(source)
        await db.flush()
        (await db.get(PrintQueueItem, row_id)).queue_source_id = source.id
        await db.commit()


async def restore_preview(factory, pid, row_id, *, then_cancel):
    async with factory() as db:
        preview = await store.make_preview(db, pid, "item", row_id, GATE_OWNER_ACTION, "user:1")
        if then_cancel:
            # Another operator takes the documented way out meanwhile: Cancel the job, then Repeat on the card.
            (await db.get(PrintQueueItem, row_id)).status = "cancelled"
            await db.commit()
    return tokens.verify(preview["preview_token"])


def held_scope(monkeypatch, module, name):
    """Stop the first entry of an async context manager until released; the guards themselves are real."""
    real = getattr(module, name)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    @asynccontextmanager
    async def wrapper(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        async with real(*args, **kwargs) as value:
            yield value

    monkeypatch.setattr(module, name, wrapper)
    return entered, release


async def repeat(factory, pid, aid):
    async with factory() as db:
        return await plate_answers.answer_plate_run(db, printer_id=pid, expected_archive_id=aid, action="repeat")


async def both(first, start_second, entered, release, *, while_waiting=None):
    first_task = asyncio.create_task(first)
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(start_second())
        await asyncio.sleep(0.1)
        assert not second.done(), "the second writer must wait for the first one's storage guard"
        if while_waiting is not None:
            while_waiting()
        release.set()
        # Both finish: a lock cycle would leave them pending, and wait_for would name it.
        return await asyncio.wait_for(asyncio.gather(first_task, second, return_exceptions=True), 5)
    finally:
        release.set()
        for task in (first_task, second):
            if task is not None and not task.done():
                task.cancel()


async def test_a_repeat_holding_its_guards_first_then_the_stale_restore_refuses(wal_requeued, monkeypatch, telemetry):
    factory, (pid, aid, row_id) = wal_requeued
    await with_captured_source(factory, row_id)
    payload = await restore_preview(factory, pid, row_id, then_cancel=True)
    entered, release = held_scope(monkeypatch, queue_source_capture, "reusing_sources")

    def repeat_owns_both():
        # The Repeat holds storage and then the archive; the apply waits for storage, before reading anything.
        assert queue_sources._guard().locked() and archive_process_lock(aid).locked()

    answered, restored = await both(
        repeat(factory, pid, aid),
        lambda: store.apply_preview(factory, payload, "user:1", "Operator"),
        entered,
        release,
        while_waiting=repeat_owns_both,
    )

    assert not isinstance(answered, BaseException), answered
    assert isinstance(restored, ValueError) and str(restored) == "recovery_changed", restored
    assert await final_state(factory, pid, row_id) == ("pending", False)


async def test_a_stale_restore_holding_storage_first_then_the_waiting_repeat_succeeds(
    wal_requeued, monkeypatch, telemetry
):
    factory, (pid, aid, row_id) = wal_requeued
    await with_captured_source(factory, row_id)
    payload = await restore_preview(factory, pid, row_id, then_cancel=True)
    # The apply has the storage guard and is about to take the archive's.
    entered, release = held_scope(monkeypatch, store, "claim_lanes")

    def repeat_holds_no_archive():
        # Before RG-1 the Repeat took the archive here and then waited for storage under it: a lock cycle.
        assert queue_sources._guard().locked() and not archive_process_lock(aid).locked()

    restored, answered = await both(
        store.apply_preview(factory, payload, "user:1", "Operator"),
        lambda: repeat(factory, pid, aid),
        entered,
        release,
        while_waiting=repeat_holds_no_archive,
    )

    assert isinstance(restored, ValueError) and str(restored) == "recovery_changed", restored
    assert not isinstance(answered, BaseException), answered
    assert await final_state(factory, pid, row_id) == ("pending", False)


async def test_a_current_restore_first_then_the_waiting_repeat_prints_it_again(wal_requeued, monkeypatch, telemetry):
    # The intended way through (B5), concurrently: the row is restored, then the card's Repeat re-arms it.
    factory, (pid, aid, row_id) = wal_requeued
    await with_captured_source(factory, row_id)
    payload = await restore_preview(factory, pid, row_id, then_cancel=False)
    entered, release = held_scope(monkeypatch, store, "claim_lanes")

    restored, answered = await both(
        store.apply_preview(factory, payload, "user:1", "Operator"),
        lambda: repeat(factory, pid, aid),
        entered,
        release,
    )

    assert not isinstance(restored, BaseException), restored
    assert not isinstance(answered, BaseException), answered
    assert await final_state(factory, pid, row_id) == ("pending", False)


async def test_a_repeat_that_already_read_is_refused_rather_than_waiting_for_storage(
    wal_requeued, monkeypatch, telemetry
):
    # As /repeat-print: the route's own read opened a SQLite transaction, and such a caller never waits for a
    # process guard. It answers busy, changes nothing, and the restore completes.
    factory, (pid, aid, row_id) = wal_requeued
    await with_captured_source(factory, row_id)
    payload = await restore_preview(factory, pid, row_id, then_cancel=False)
    entered, release = held_scope(monkeypatch, store, "claim_lanes")
    apply_task = asyncio.create_task(store.apply_preview(factory, payload, "user:1", "Operator"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with factory() as db:
            await db.execute(select(Printer.id).where(Printer.id == pid))
            with pytest.raises(PlateAnswerBusy):
                await plate_answers.answer_plate_run(db, printer_id=pid, expected_archive_id=aid, action="repeat")
        release.set()
        await asyncio.wait_for(apply_task, 5)
    finally:
        release.set()
        if not apply_task.done():
            apply_task.cancel()

    assert await final_state(factory, pid, row_id) == ("cancelled", True)
