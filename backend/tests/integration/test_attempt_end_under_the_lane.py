"""An attempt ends addressed to its claim, under its lane; Stop acts only on the attempt it read (G03 B, A9-A11,
A16, A27 for the deferral's restore; acceptance TA9, the SQLite part of TA20, TA37 (b) for the deferral)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import print_control
from backend.app.services.filament_deferred import defer_claim
from backend.app.services.print_scheduler import PrintScheduler
from backend.app.services.printer_manager import printer_manager

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

T0 = datetime(2026, 11, 20, 10, 0, 0)


async def claimed(db_session, printer_factory, **row):
    """A printer whose queue header names the row it is printing, as a claim leaves them."""
    printer = await printer_factory()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.commit()
    item = PrintQueueItem(queue_id=queue.id, status="printing", position=0, started_at=T0, **row)
    db_session.add(item)
    await db_session.commit()
    queue.current_item_id = item.id
    await db_session.commit()
    return printer.id, queue.id, item.id


async def read(model, row_id):
    async with database.async_session() as db:
        return await db.get(model, row_id)


async def write(model, row_id, **values):
    async with database.async_session() as db:
        await db.execute(update(model).where(model.id == row_id).values(**values))
        await db.commit()


async def defer(item_id, queue_id, **kwargs):
    async with database.async_session() as db:
        return await defer_claim(
            db, item_id=item_id, started_at=kwargs.pop("started_at", T0), queue_id=queue_id, **kwargs
        )


# ---------------------------------------------------------------- A10 / A11 / A16: defer_claim


async def test_a_no_effect_deferral_returns_the_row_with_the_proof_of_its_attempt(db_session, printer_factory):
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    result = await defer(item_id, queue_id, reason="feed_settle_timeout")
    assert result and not result.terminal
    row = await read(PrintQueueItem, item_id)
    assert (row.status, row.started_at, row.deferred_claim_token) == ("pending", None, T0)
    header = await read(PrinterQueue, queue_id)
    assert (header.status, header.current_item_id) == ("idle", None)


async def test_every_assignment_of_status_voids_the_proof(db_session, printer_factory):
    """TA20 (k): the model's one validator of ``status`` clears the token, whatever the values."""
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    await defer(item_id, queue_id, reason="feed_settle_timeout")
    async with database.async_session() as db:
        row = await db.get(PrintQueueItem, item_id)
        row.status = "pending"  # the same value: still an assignment
        assert row.deferred_claim_token is None
        await db.commit()
    assert (await read(PrintQueueItem, item_id)).deferred_claim_token is None


async def test_a_lost_deferral_releases_a_header_that_still_names_the_row(db_session, printer_factory):
    """TA9 / A11: the row was cancelled meanwhile; the header must not stay claimed by it."""
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    await write(PrintQueueItem, item_id, status="cancelled")
    assert not await defer(item_id, queue_id, reason="feed_settle_timeout")
    header = await read(PrinterQueue, queue_id)
    assert (header.status, header.current_item_id) == ("idle", None)


async def test_a_lost_deferral_keeps_a_newer_attempt_s_header(db_session, printer_factory):
    """TA9: a row claimed again is printing with a new token — its header is never released."""
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    await write(PrintQueueItem, item_id, started_at=T0 + timedelta(minutes=5))
    assert not await defer(item_id, queue_id, reason="feed_settle_timeout")
    header = await read(PrinterQueue, queue_id)
    assert (header.status, header.current_item_id) == ("printing", item_id)


async def test_the_deferral_addresses_the_claim_queue_not_a_moved_row_s(db_session, printer_factory):
    """TA9: the row moved to another printer's queue; the claim queue's header is the one released."""
    printer_b = await printer_factory()
    db_session.add(PrinterQueue(id=printer_b.id, printer_id=printer_b.id, status="idle"))
    await db_session.commit()
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    other = printer_b.id
    await write(PrintQueueItem, item_id, queue_id=other)
    result = await defer(item_id, queue_id, reason="feed_settle_timeout")
    assert result
    assert (await read(PrinterQueue, queue_id)).status == "idle"
    assert (await read(PrinterQueue, other)).status == "idle"


async def test_a_terminal_deferral_halts_the_claim_queue_through_halt_lane(db_session, printer_factory):
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    result = await defer(item_id, queue_id, reason="source_unreadable")
    assert result.terminal
    row = await read(PrintQueueItem, item_id)
    assert (row.status, row.deferred_claim_token) == ("failed", None)
    header = await read(PrinterQueue, queue_id)
    assert (header.status, header.current_item_id) == ("error", None)


# ---------------------------------------------------------------- A27: the deferral's source restore


async def archive_row(db_session, **values) -> PrintArchive:
    archive = PrintArchive(filename="a.3mf", file_path="", file_size=0, status="completed", **values)
    db_session.add(archive)
    await db_session.commit()
    return archive


async def test_the_deferral_restores_the_original_it_read_at_its_start(db_session, printer_factory):
    original = await archive_row(db_session)
    original_id, identity = original.id, original.recovery_identity
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    await defer(
        item_id,
        queue_id,
        reason="feed_settle_timeout",
        restore_source=True,
        source_archive_id=original_id,
        source_archive_identity=identity,
    )
    assert (await read(PrintQueueItem, item_id)).archive_id == original_id


async def test_the_deferral_writes_no_link_to_another_row_under_the_original_s_id(db_session, printer_factory):
    """TA37 (b): the original deleted and another archive inserted with its id — another incarnation, NULL."""
    original = await archive_row(db_session)
    original_id, identity = original.id, original.recovery_identity
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    async with database.async_session() as db:
        await db.execute(PrintArchive.__table__.delete().where(PrintArchive.id == original_id))
        db.add(PrintArchive(id=original_id, filename="other.3mf", file_path="", file_size=0, status="completed"))
        await db.commit()
    await defer(
        item_id,
        queue_id,
        reason="feed_settle_timeout",
        restore_source=True,
        source_archive_id=original_id,
        source_archive_identity=identity,
    )
    row = await read(PrintQueueItem, item_id)
    assert (row.archive_id, row.library_file_id) == (None, None)


# ---------------------------------------------------------------- A10: _cancel_item / _fail_attempt


async def test_a_cancel_of_an_attempt_that_is_no_longer_ours_writes_nothing(db_session, printer_factory):
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    await write(PrintQueueItem, item_id, started_at=T0 + timedelta(minutes=5))  # claimed again
    async with database.async_session() as db:
        item = await db.get(PrintQueueItem, item_id)
        assert not await PrintScheduler()._cancel_item(db, item, claim_started_at=T0, queue_id=queue_id)
    row = await read(PrintQueueItem, item_id)
    assert row.status == "printing"
    assert (await read(PrinterQueue, queue_id)).status == "printing"


async def test_a_failed_attempt_while_another_run_holds_the_header_only_pauses(db_session, printer_factory):
    """A9: the header names another run's row; this attempt's end pauses the queue and leaves that claim."""
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    async with database.async_session() as db:
        theirs = PrintQueueItem(queue_id=queue_id, status="printing", position=1, started_at=T0)
        db.add(theirs)
        await db.flush()
        theirs_id = theirs.id
        await db.execute(update(PrinterQueue).where(PrinterQueue.id == queue_id).values(current_item_id=theirs_id))
        await db.commit()
    async with database.async_session() as db:
        assert await PrintScheduler()._fail_attempt(
            db, item_id=item_id, claim_started_at=T0, message="x", code="dispatch_internal_error", queue_id=queue_id
        )
    header = await read(PrinterQueue, queue_id)
    assert (header.status, header.current_item_id, header.is_paused) == ("printing", theirs_id, True)


@pytest.mark.parametrize("archive_state", ["sending", "other_identity", "already_closed", "stale_claim"])
async def test_failure_only_closes_its_current_prepared_execution(db_session, printer_factory, archive_state):
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    archive = await archive_row(db_session)
    archive_id, identity = archive.id, archive.recovery_identity
    status = "failed" if archive_state == "already_closed" else "printing"
    phase = "sending" if archive_state == "sending" else "prepared"
    await write(PrintArchive, archive_id, status=status, dispatch_phase=phase, error_message="original")
    token = T0 - timedelta(minutes=1) if archive_state == "stale_claim" else T0
    async with database.async_session() as db:
        written = await PrintScheduler()._fail_attempt(
            db,
            item_id=item_id,
            claim_started_at=token,
            message="Upload refused",
            code="dispatchUploadFailed",
            sent=False,
            queue_id=queue_id,
            execution_archive_id=archive_id,
            execution_archive_identity="stale-archive-identity" if archive_state == "other_identity" else identity,
        )
    assert written is (archive_state != "stale_claim")
    row = await read(PrintQueueItem, item_id)
    assert row.status == ("failed" if written else "printing")
    execution = await read(PrintArchive, archive_id)
    assert (execution.status, execution.dispatch_phase, execution.error_message) == (status, phase, "original")


# ---------------------------------------------------------------- A16: Stop acts only on the attempt it read


def the_printer_takes_the_stop(monkeypatch, *, between=None):
    async def fresh(_printer_id):
        if between is not None:
            await between()
        return True

    monkeypatch.setattr(printer_manager, "ensure_fresh_connection", fresh)
    monkeypatch.setattr(print_control, "stop_print_by_user", lambda _printer_id, **_kwargs: True)


async def test_stop_cancels_the_attempt_a_deferral_sent_back_meanwhile(
    async_client, db_session, printer_factory, monkeypatch
):
    """TA20 (a): the no-effect deferral commits while the Stop awaits — the very attempt it read, by its proof."""
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)

    async def deferral():
        await defer(item_id, queue_id, reason="feed_settle_timeout")

    the_printer_takes_the_stop(monkeypatch, between=deferral)
    response = await async_client.post(f"/api/v1/queue/{item_id}/stop")
    assert response.status_code == 200, response.text
    row = await read(PrintQueueItem, item_id)
    assert (row.status, row.deferred_claim_token) == ("cancelled", None)
    header = await read(PrinterQueue, queue_id)
    assert header.status == "paused"


async def test_stop_writes_nothing_over_a_newer_attempt(async_client, db_session, printer_factory, monkeypatch):
    """TA20 (b): deferred and claimed again before the Stop's SQL phase — 409, the new attempt untouched."""
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    newer = T0 + timedelta(minutes=5)

    async def deferral_then_claim():
        await defer(item_id, queue_id, reason="feed_settle_timeout")
        await write(PrintQueueItem, item_id, status="printing", started_at=newer, deferred_claim_token=None)
        await write(PrinterQueue, queue_id, status="printing", current_item_id=item_id)

    the_printer_takes_the_stop(monkeypatch, between=deferral_then_claim)
    response = await async_client.post(f"/api/v1/queue/{item_id}/stop")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "stop_target_changed"
    row = await read(PrintQueueItem, item_id)
    assert (row.status, row.started_at) == ("printing", newer)
    assert (await read(PrinterQueue, queue_id)).status == "printing"


async def test_stop_after_the_dispatcher_honoured_its_cancel_writes_nothing(
    async_client, db_session, printer_factory, monkeypatch
):
    """TA20 (d): ``_cancel_item`` committed meanwhile — the job is stopped either way, 200."""
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)

    async def dispatcher_cancels():
        async with database.async_session() as db:
            item = await db.get(PrintQueueItem, item_id)
            await PrintScheduler()._cancel_item(db, item, claim_started_at=T0, queue_id=queue_id)

    the_printer_takes_the_stop(monkeypatch, between=dispatcher_cancels)
    response = await async_client.post(f"/api/v1/queue/{item_id}/stop")
    assert response.status_code == 200, response.text
    row = await read(PrintQueueItem, item_id)
    assert (row.status, row.error_message) == ("cancelled", "Cancelled by user")


async def test_stop_of_an_untouched_unbound_attempt_cancels_it(async_client, db_session, printer_factory, monkeypatch):
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    the_printer_takes_the_stop(monkeypatch)
    response = await async_client.post(f"/api/v1/queue/{item_id}/stop")
    assert response.status_code == 200, response.text
    row = await read(PrintQueueItem, item_id)
    assert (row.status, row.error_message) == ("cancelled", "Stopped by user")
    header = await read(PrinterQueue, queue_id)
    assert (header.status, header.current_item_id) == ("paused", None)
    assert await db_session.scalar(select(PrintQueueItem.status).where(PrintQueueItem.id == item_id)) == "cancelled"


# ---------------------------------------------------------------- TA20 (e)-(j): the attempt the Stop read, by its proof


async def test_stop_leaves_a_row_whose_run_became_bound_before_its_sql_phase(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    """TA20 (e): a run is bound to the row's archive between the send and the SQL phase -- the printer's report
    closes it: the row stays printing, the archive is not written, 200."""
    from backend.app.services import print_run_binding
    from backend.app.services.print_run_binding import PrintRunBinding

    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="printing")
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.commit()
    item = PrintQueueItem(queue_id=queue.id, status="printing", position=0, started_at=T0, archive_id=archive.id)
    db_session.add(item)
    await db_session.commit()
    item_id, archive_id = item.id, archive.id
    bound: list[PrintRunBinding] = []

    async def binds_meanwhile():
        bound.append(PrintRunBinding(printer_id=printer.id, archive_id=archive_id, queue_item_id=item_id))

    monkeypatch.setattr(print_run_binding, "current_print_run", lambda _manager, _pid: bound[0] if bound else None)
    the_printer_takes_the_stop(monkeypatch, between=binds_meanwhile)
    response = await async_client.post(f"/api/v1/queue/{item_id}/stop")
    assert response.status_code == 200, response.text
    assert (await read(PrintQueueItem, item_id)).status == "printing"
    assert (await read(PrintArchive, archive_id)).status == "printing", "no archive write"


async def test_an_offline_stop_cancels_the_row_and_its_archive_in_one_commit(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    """TA20 (f): the printer unreachable, the row still this attempt's -- row and archive cancelled together."""
    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="printing")
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
    db_session.add(queue)
    await db_session.commit()
    item = PrintQueueItem(queue_id=queue.id, status="printing", position=0, started_at=T0, archive_id=archive.id)
    db_session.add(item)
    await db_session.commit()

    async def unreachable(_printer_id):
        return False

    monkeypatch.setattr(printer_manager, "ensure_fresh_connection", unreachable)
    monkeypatch.setattr(print_control, "stop_print_by_user", lambda _printer_id, **_kwargs: False)
    response = await async_client.post(f"/api/v1/queue/{item.id}/stop")
    assert response.status_code == 200, response.text
    row = await read(PrintQueueItem, item.id)
    assert (row.status, row.error_message) == ("cancelled", "Stopped by user (printer was offline)")
    assert (await read(PrintArchive, archive.id)).status == "cancelled"


async def test_stop_writes_nothing_over_another_job_that_took_the_row_s_id(
    async_client, db_session, printer_factory, monkeypatch
):
    """TA20 (g): the Stop read A (printing, token T); A was deferred, then deleted, and B took A's id -- 409, B
    pending and untouched, its queue not paused."""
    from sqlalchemy import delete

    _printer, queue_id, item_id = await claimed(db_session, printer_factory)

    async def deleted_and_reused():
        await defer(item_id, queue_id, reason="feed_settle_timeout")
        async with database.async_session() as db:
            await db.execute(delete(PrintQueueItem).where(PrintQueueItem.id == item_id))
            db.add(PrintQueueItem(id=item_id, queue_id=queue_id, status="pending", position=0))
            await db.commit()

    the_printer_takes_the_stop(monkeypatch, between=deleted_and_reused)
    response = await async_client.post(f"/api/v1/queue/{item_id}/stop")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "stop_target_changed"
    assert (await read(PrintQueueItem, item_id)).status == "pending"
    assert (await read(PrinterQueue, queue_id)).status != "paused"


async def test_stop_writes_nothing_over_a_row_deferred_again_by_a_newer_attempt(
    async_client, db_session, printer_factory, monkeypatch
):
    """TA20 (h): defer (proof T) -> the next claim (token T2, proof voided) -> deferred again (proof T2): the Stop
    that read T answers 409 and the row keeps T2's proof."""
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)
    newer = T0 + timedelta(minutes=5)

    async def defer_claim_defer():
        await defer(item_id, queue_id, reason="feed_settle_timeout")
        await write(PrintQueueItem, item_id, status="printing", started_at=newer, deferred_claim_token=None)
        await write(PrinterQueue, queue_id, status="printing", current_item_id=item_id)
        await defer(item_id, queue_id, reason="feed_settle_timeout", started_at=newer)

    the_printer_takes_the_stop(monkeypatch, between=defer_claim_defer)
    response = await async_client.post(f"/api/v1/queue/{item_id}/stop")
    assert response.status_code == 409, response.text
    row = await read(PrintQueueItem, item_id)
    assert (row.status, row.deferred_claim_token) == ("pending", newer)


@pytest.mark.parametrize(
    ("change", "expected"),
    [("skip_unskip", (409, "pending")), ("edit_options", (200, "cancelled"))],
    ids=["defer-skip-unskip", "defer-patch-options"],
)
async def test_stop_after_a_deferral_follows_the_proof_through_later_writes(
    change, expected, async_client, db_session, printer_factory, monkeypatch
):
    """TA20 (i): every status write voids the deferral proof (skip, then unskip -> 409); an edit of the row's options
    keeps it (the Stop cancels the very attempt it read, 200)."""
    _printer, queue_id, item_id = await claimed(db_session, printer_factory)

    async def deferral_then_change():
        await defer(item_id, queue_id, reason="feed_settle_timeout")
        async with database.async_session() as db:
            row = await db.get(PrintQueueItem, item_id)
            if change == "skip_unskip":
                row.status = "skipped"
                await db.commit()
                row.status = "pending"
            else:
                row.auto_off_after = True
            await db.commit()

    the_printer_takes_the_stop(monkeypatch, between=deferral_then_change)
    response = await async_client.post(f"/api/v1/queue/{item_id}/stop")
    assert response.status_code == expected[0], response.text
    assert (await read(PrintQueueItem, item_id)).status == expected[1]


@pytest.mark.parametrize("same_job", [False, True], ids=["another-job-cancelled", "this-job-cancelled"])
async def test_a_cancelled_row_is_the_stop_s_only_when_it_is_the_same_job(
    same_job, async_client, db_session, printer_factory, monkeypatch
):
    """TA20 (j): a row found cancelled is "already stopped" (200) only when it is the job the Stop read (same
    recovery identity); a job that took the id and was cancelled is another job's end (409)."""
    from sqlalchemy import delete

    _printer, queue_id, item_id = await claimed(db_session, printer_factory)

    async def cancelled_meanwhile():
        async with database.async_session() as db:
            if same_job:
                await db.execute(
                    update(PrintQueueItem)
                    .where(PrintQueueItem.id == item_id)
                    .values(status="cancelled", deferred_claim_token=None)
                )
            else:
                await db.execute(delete(PrintQueueItem).where(PrintQueueItem.id == item_id))
                db.add(PrintQueueItem(id=item_id, queue_id=queue_id, status="cancelled", position=0))
            await db.commit()

    the_printer_takes_the_stop(monkeypatch, between=cancelled_meanwhile)
    response = await async_client.post(f"/api/v1/queue/{item_id}/stop")
    assert response.status_code == (200 if same_job else 409), response.text
