"""Wear is stamped where a live completion gives the archive its terminal status, and nowhere else.

Driven through the real completion callback (the swap-terminal rig): the bound path, which makes the
archive terminal in the queue row's commit, and the status update that follows it. The closes without a
measured end -- the reconnect sweep and the operator's close -- leave both columns empty.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.app import main
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.archive import ArchiveService
from backend.tests.integration.swap_terminal_support import (  # noqa: F401
    an_execution,
    archive_of,
    complete,
    make_rig,
    recover,
)
from backend.tests.integration.test_print_state_recovery import prepare_stuck_print
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

RATE = 3.6  # per hour: a thousand seconds of printing is 1.0
RAN_FOR = 1000


async def _a_run(rig, *, rate=RATE, started="measured", bound=True, extra=None):
    """A run in flight that began ``RAN_FOR`` seconds ago on a printer with ``rate``."""
    archive_id, item_id = await an_execution(rig, fact=False, bound=bound)
    async with rig.db() as db:
        printer = await db.get(Printer, rig.printer_id)
        printer.wear_cost_per_hour = rate
        archive = await db.get(PrintArchive, archive_id)
        archive.started_at = (
            None if started is None else datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=RAN_FOR)
        )
        if started == "reconstructed":
            archive.extra_data = {**(archive.extra_data or {}), "started_at_reconstructed": True}
        if extra:
            archive.extra_data = {**(archive.extra_data or {}), **extra}
        await db.commit()
    return archive_id, item_id


def _is_the_measured_figure(archive) -> bool:
    # The run took RAN_FOR seconds plus however long the test itself needed.
    return archive.wear_rate == RATE and 1.0 <= archive.wear_cost < 1.1


@pytest.mark.parametrize(
    ("reported", "stored"),
    [("completed", "completed"), ("failed", "failed"), ("aborted", "cancelled")],
)
async def test_a_print_with_a_measured_duration_is_charged_however_it_ended(make_rig, reported, stored):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await _a_run(rig)

    await complete(rig, {"status": reported, "subtask_id": "81"})

    archive = await archive_of(rig, archive_id)
    assert archive.status in (stored, reported)
    assert _is_the_measured_figure(archive), (archive.wear_cost, archive.wear_rate)


@pytest.mark.parametrize("rate", [None, 0.0])
async def test_a_printer_without_a_rate_charges_nothing(make_rig, rate):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await _a_run(rig, rate=rate)

    await complete(rig)

    archive = await archive_of(rig, archive_id)
    assert archive.status == "completed"
    assert (archive.wear_cost, archive.wear_rate) == (None, None)


@pytest.mark.parametrize("started", [None, "reconstructed"])
async def test_a_start_nobody_measured_charges_nothing(make_rig, started):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await _a_run(rig, started=started)

    await complete(rig)

    archive = await archive_of(rig, archive_id)
    assert archive.status == "completed"
    assert (archive.wear_cost, archive.wear_rate) == (None, None)


async def test_an_unbound_completion_is_charged_by_the_status_update(make_rig):
    """No runtime binding (an external print, a restart): the archive leaves ``printing`` in
    ``update_archive_status``, and that is where it is stamped."""
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, item_id = await _a_run(rig, bound=False)
    async with rig.db() as db:
        # No queue row either: the completion finds the archive by the printer's active print.
        queue = await db.get(PrinterQueue, rig.queue_id)
        queue.status, queue.current_item_id = "idle", None
        await db.delete(await db.get(PrintQueueItem, item_id))
        await db.commit()
    main._active_prints[(rig.printer_id, "job.gcode.3mf")] = archive_id

    await complete(rig, {"status": "completed", "filename": "job.gcode.3mf", "subtask_name": "job"})

    archive = await archive_of(rig, archive_id)
    assert archive.status == "completed"
    assert _is_the_measured_figure(archive), (archive.wear_cost, archive.wear_rate)


async def test_a_completion_of_a_deleted_printer_record_charges_nothing(make_rig):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await _a_run(rig)
    life = rig.manager._printer_lives.get(rig.printer_id, 0)
    rig.manager._printer_lives[rig.printer_id] = life + 2  # a deletion began and ended since

    await complete(rig, {"status": "completed", "subtask_id": "81", "_bamdude_printer_life": life})

    archive = await archive_of(rig, archive_id)
    assert (archive.wear_cost, archive.wear_rate) == (None, None)


async def test_the_status_update_asks_the_record_life_before_it_stamps(make_rig):
    """The life can end between the completion's first check and the terminal write."""
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await _a_run(rig, bound=False)

    async with rig.db() as db:
        updated = await ArchiveService(db).update_archive_status(
            archive_id,
            status="completed",
            completed_at=datetime.now(timezone.utc),
            wear_printer_id=rig.printer_id,
            wear_record_holds=lambda: False,
        )

    archive = await archive_of(rig, archive_id)
    assert updated and archive.status == "completed"
    assert (archive.wear_cost, archive.wear_rate) == (None, None)


async def test_a_terminal_archive_is_not_stamped_by_a_later_status_update(make_rig):
    """Double stamping is excluded by the transition, not only by the writer's refusal: a print the
    bound path closed without a figure (no rate then) gets none from the status update after it."""
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await _a_run(rig, rate=None)
    await complete(rig)
    async with rig.db() as db:
        (await db.get(Printer, rig.printer_id)).wear_cost_per_hour = RATE
        await db.commit()

    async with rig.db() as db:
        await ArchiveService(db).update_archive_status(
            archive_id,
            status="completed",
            completed_at=datetime.now(timezone.utc),
            wear_printer_id=rig.printer_id,
            wear_record_holds=lambda: True,
        )

    archive = await archive_of(rig, archive_id)
    assert (archive.wear_cost, archive.wear_rate) == (None, None)


async def test_a_later_rate_change_does_not_move_a_stamped_figure(make_rig):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await _a_run(rig)
    await complete(rig)
    stamped = await archive_of(rig, archive_id)
    figure = stamped.wear_cost
    assert _is_the_measured_figure(stamped)

    async with rig.db() as db:
        (await db.get(Printer, rig.printer_id)).wear_cost_per_hour = 99.0
        await db.commit()
    async with rig.db() as db:
        await ArchiveService(db).update_archive_status(
            archive_id,
            status="completed",
            completed_at=datetime.now(timezone.utc) + timedelta(hours=5),
            wear_printer_id=rig.printer_id,
            wear_record_holds=lambda: True,
        )

    archive = await archive_of(rig, archive_id)
    assert (archive.wear_cost, archive.wear_rate) == (figure, RATE)


@pytest.mark.parametrize("uncertain", [False, True])
async def test_a_reconciliation_close_charges_nothing(make_rig, uncertain):
    """The sweep's end is an estimate: no measured duration, so no figure -- NULL, never 0."""
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await _a_run(rig, bound=False)

    await recover(rig, archive_id, uncertain=uncertain)

    archive = await archive_of(rig, archive_id)
    assert archive.status != "printing"
    assert (archive.wear_cost, archive.wear_rate) == (None, None)


async def test_an_operator_close_charges_nothing(
    async_client,
    db_session,
    printer_factory,
    archive_factory,
    telemetry,
    monkeypatch,  # noqa: F811
):
    (printer, _queue, archive, item, _pending), tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch
    )
    printer.wear_cost_per_hour = RATE
    archive.started_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=RAN_FOR)
    await db_session.commit()
    await async_client.get("/api/v1/system/print-state-issues")
    tick[0] += 11
    path = f"/api/v1/system/printers/{printer.id}/print-state"
    preview = await async_client.post(
        path + "/preview",
        json={
            "kind": "item",
            "target_id": item.id,
            "action": "close_interrupted_print",
            "confirmed_idle": True,
            "operator_outcome": "interrupted",
        },
    )
    assert preview.status_code == 200, preview.text
    applied = await async_client.post(
        path + "/apply", json={"preview_token": preview.json()["preview_token"], "confirmed": True}
    )
    assert applied.status_code == 200, applied.text

    await db_session.refresh(archive)
    assert archive.status == "failed"
    assert (archive.wear_cost, archive.wear_rate) == (None, None)


async def test_the_measured_figure_is_what_the_rows_say(make_rig):
    """One stamp per print: the archive's own start and the terminal write's end."""
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await _a_run(rig)
    await complete(rig)

    async with rig.db() as db:
        archive = await db.scalar(select(PrintArchive).where(PrintArchive.id == archive_id))
    seconds = (archive.completed_at - archive.started_at).total_seconds()
    assert archive.wear_cost == round(seconds / 3600 * RATE, 3)
