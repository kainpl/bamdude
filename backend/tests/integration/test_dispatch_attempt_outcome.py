"""A dispatch attempt ends as what was published, never as a later bookkeeping error.

Spec: vault ``60-specs/post-rebuild-dispatch-transactions-spec.md`` (D-R1, D-R2, D-R4,
D-R5), acceptance rows D01, D02 and D04. Everything runs the REAL scheduler and both
runners with only device I/O faked — the harness of ``test_dispatch_without_original``.

The two defects these pin:

* a failure in the bookkeeping that FOLLOWS a published start command used to fail the
  attempt, and the scheduler's busy check then saw the printer printing our job and put
  the row back to ``pending`` — so the next free moment printed it a second time;
* a rollback of the runner's or the tick's session expired the ORM rows the cleanup then
  read, so a cancel, a refused upload or a lost final claim ended in ``MissingGreenlet``
  instead of its own answer.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.tests.integration.test_dispatch_without_original import (  # noqa: F401 - fixtures
    PLATE,
    a_library_source,
    an_archive_source,
    clean_spool_state,
    dispatch_mocks,
    drain,
    scheduler_with_captured_dispatch,
    sessions,
)
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration


async def queued(db, queue, source, kind: str) -> PrintQueueItem:
    from backend.app.services.queue_add import add_items_to_printer_queue

    fields = {"library_file_id": source.id} if kind == "library" else {"archive_id": source.id}
    items, _queue = await add_items_to_printer_queue(
        db, PrintQueueItemCreate(queue_id=queue.id, plate_id=PLATE, **fields), None
    )
    return items[0]


async def a_queued_job(db, tmp_path, printer_factory, monkeypatch, kind: str):
    _unused, printer, queue, mqtt = await setup_source(db, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    if kind == "library":
        source = await a_library_source(db, tmp_path, name="lamp.gcode.3mf")
    else:
        source = await an_archive_source(db, tmp_path, printer.id)
    item = await queued(db, queue, source, kind)
    # Plain ids: the tests expire the session to re-read what the dispatch wrote.
    return printer.id, queue.id, mqtt, item.id


def the_printer_takes_it(mqtt) -> None:
    """The published start lands: from then on the printer reports its print running."""

    def publish(*_args, **_kwargs):
        mqtt.state.state = "RUNNING"

    mqtt._client.publish.side_effect = publish


async def reread(db, model, row_id):
    db.expire_all()
    return await db.get(model, row_id)


async def execution_archive(db):
    """``(status, error_message, extra_data)`` of the one attempt archive (the source one is completed)."""
    db.expire_all()
    return (
        await db.execute(
            select(PrintArchive.status, PrintArchive.error_message, PrintArchive.extra_data).where(
                PrintArchive.status != "completed"
            )
        )
    ).one()


@pytest.mark.parametrize("kind", ["library", "archive"])
@pytest.mark.parametrize("failing", ["dispatched_mapping", "macro_selection"])
async def test_a_bookkeeping_error_after_the_start_command_keeps_the_attempt_printing(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, kind, failing
):
    """D01/D04: exception after a publish — not failed, not pending, never printed twice."""
    import backend.app.services.background_dispatch as bd

    printer_id, queue_id, mqtt, item_id = await a_queued_job(db_session, tmp_path, printer_factory, monkeypatch, kind)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    the_printer_takes_it(mqtt)
    if failing == "dispatched_mapping":
        monkeypatch.setattr(bd, "_record_dispatched_mapping", AsyncMock(side_effect=RuntimeError("bookkeeping")))
    else:
        monkeypatch.setattr("backend.app.main.register_macro_selection", MagicMock(side_effect=RuntimeError("ui")))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    assert await scheduler.check_queue() is True
    await drain(spawned)

    mqtt._client.publish.assert_called_once()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "printing", row.error_message
    archive = await db_session.get(PrintArchive, row.archive_id)
    assert archive.status == "printing"
    queue_row = await db_session.get(PrinterQueue, queue_id)
    assert queue_row.status == "printing" and queue_row.current_item_id == item_id

    # The printer finishes and nothing has told us yet: the row must not go out again.
    mqtt.state.state = "FINISH"
    await scheduler.check_queue()
    await drain(spawned)
    mqtt._client.publish.assert_called_once()


@pytest.mark.parametrize("kind", ["library", "archive"])
async def test_a_cancel_after_the_runner_read_its_session_cancels_its_own_archive(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, kind
):
    """D02: the cancel's own answer after the runner rolled back a session it had read in.

    Cancelled from the preheat's ``cancel_check``: by then the runner has read through
    its session (the admission check), so its rollback really expires what it holds.
    """
    from backend.app.services.background_dispatch import DispatchJobCancelled

    printer_id, queue_id, mqtt, item_id = await a_queued_job(db_session, tmp_path, printer_factory, monkeypatch, kind)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    monkeypatch.setattr(
        "backend.app.services.preheat.preheat_and_soak", AsyncMock(side_effect=DispatchJobCancelled("cancelled"))
    )
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    assert await scheduler.check_queue() is True
    await drain(spawned)

    mqtt._client.publish.assert_not_called()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "cancelled", row.error_message
    status, message, _extra = await execution_archive(db_session)
    assert status == "cancelled", message


@pytest.mark.parametrize("kind", ["library", "archive"])
async def test_a_refused_start_returns_the_row_to_pending_with_its_attempt(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, kind
):
    """D02/D05: ``start_print`` refused (nothing published) after the runner read its session.

    A proven refusal is a deferral: the row waits for the printer again, the attempt it
    spent is returned, and its archive is the aborted, never-published kind.
    """
    import backend.app.services.background_dispatch as bd

    printer_id, queue_id, mqtt, item_id = await a_queued_job(db_session, tmp_path, printer_factory, monkeypatch, kind)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    monkeypatch.setattr(bd.printer_manager, "start_print", MagicMock(return_value=False))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    assert await scheduler.check_queue() is True
    await drain(spawned)

    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "pending", row.error_message
    assert row.dispatch_attempts == 0
    assert "greenlet" not in (row.error_message or "").lower()
    status, message, extra = await execution_archive(db_session)
    assert status == "cancelled" and (extra or {}).get("dispatch_aborted") is True, message


@pytest.mark.parametrize("kind", ["library", "archive"])
async def test_a_refused_upload_fails_with_its_own_reason(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, kind
):
    """A refused upload is a failed attempt that says why, and halts the printer's queue."""
    printer_id, queue_id, mqtt, item_id = await a_queued_job(db_session, tmp_path, printer_factory, monkeypatch, kind)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory, upload=AsyncMock(return_value=False))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    assert await scheduler.check_queue() is True
    await drain(spawned)

    mqtt._client.publish.assert_not_called()
    row = await reread(db_session, PrintQueueItem, item_id)
    assert row.status == "failed"
    assert "greenlet" not in (row.error_message or "").lower()
    status, message, _extra = await execution_archive(db_session)
    assert status == "failed"
    assert "greenlet" not in (message or "").lower()
    queue_row = await db_session.get(PrinterQueue, queue_id)
    assert queue_row.status == "error"


@pytest.mark.parametrize("kind", ["library", "archive"])
async def test_attempt_end_closes_a_prepared_archive_after_the_runner_commit_fails(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, caplog, kind
):
    import backend.app.services.background_dispatch as bd
    from backend.app.utils.failure_reasons import DISPATCH_UPLOAD_FAILED

    printer_id, queue_id, mqtt, item_id = await a_queued_job(db_session, tmp_path, printer_factory, monkeypatch, kind)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory, upload=AsyncMock(return_value=False))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    mark_terminal = bd.BackgroundDispatchService._mark_dispatch_archive_terminal
    failed_closes = set()

    @asynccontextmanager
    async def reject_commit_session():
        async with factory() as db:

            def reject_commit(_session):
                raise RuntimeError("runner archive close could not commit")

            event.listen(db.sync_session, "before_commit", reject_commit, once=True)
            yield db

    async def fail_runner_close(archive_id, *args, **kwargs):
        with monkeypatch.context() as cleanup:
            cleanup.setattr(bd, "async_session", reject_commit_session)
            result = await mark_terminal(archive_id, *args, **kwargs)
        async with factory() as db:
            archive = await db.get(PrintArchive, archive_id)
            failed_closes.add((archive_id, result, archive.status, archive.dispatch_phase))
        return result

    monkeypatch.setattr(
        bd.BackgroundDispatchService, "_mark_dispatch_archive_terminal", staticmethod(fail_runner_close)
    )
    assert await scheduler.check_queue() is True
    await drain(spawned)

    [(archive_id, result, status, phase)] = failed_closes
    assert (result, status, phase) == ("failed", "printing", "prepared")
    assert "Failed to mark dispatch archive" in caplog.text
    mqtt._client.publish.assert_not_called()
    async with factory() as db:
        row = await db.get(PrintQueueItem, item_id)
        archive = await db.get(PrintArchive, archive_id)
        header = await db.get(PrinterQueue, queue_id)
        assert (row.status, row.error_code, row.gate_acknowledged) == ("failed", DISPATCH_UPLOAD_FAILED, True)
        assert row.returned_receipt_identity is None
        assert (archive.status, archive.failure_reason) == ("failed", DISPATCH_UPLOAD_FAILED)
        assert archive.completed_at is not None
        assert header.status == "error"


async def test_a_lost_final_claim_does_not_break_the_tick_for_the_next_printer(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions
):
    """D02: the claim is refused for one printer; the same pass still dispatches the other."""
    import backend.app.services.print_scheduler as ps
    from backend.app.services.printer_occupancy import PrinterOccupancyConflict

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _printer_a, queue_a, mqtt_a, item_a = await a_queued_job(
        db_session, tmp_path / "a", printer_factory, monkeypatch, "library"
    )
    _printer_b, _queue_b, mqtt_b, item_b = await a_queued_job(
        db_session, tmp_path / "b", printer_factory, monkeypatch, "library"
    )
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    dispatch_mocks(monkeypatch, factory)
    real_require = ps.require_scheduler_claim

    def require(occupancy):
        if occupancy.queue.id == queue_a:
            raise PrinterOccupancyConflict("active_claim", queue_id=queue_a)
        return real_require(occupancy)

    monkeypatch.setattr(ps, "require_scheduler_claim", require)
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)

    assert await scheduler.check_queue() is True
    assert await drain(spawned) == 1

    mqtt_a._client.publish.assert_not_called()
    mqtt_b._client.publish.assert_called_once()
    assert (await reread(db_session, PrintQueueItem, item_a)).status == "pending"
    assert (await reread(db_session, PrintQueueItem, item_b)).status == "printing"
