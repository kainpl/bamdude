"""A queue Stop that could not reach the printer, and the print's REAL end after it.

``POST /queue/{id}/stop`` with the printer unreachable used to close the row and its
archive at once (``cancelled`` / ``userCancelled``). When the printer was only
unreachable and the print in fact went on, its later terminal report was refused
(``_accept_bound_terminal_run`` needs the archive and its queue row still
``printing``): the filament was never booked, the archive kept "cancelled by user"
for a print that completed, and the run stayed bound until the operator cleared it.

The rule now is the one a reachable printer already had: while a run is bound for
the row's archive the door closes nothing. A stop that was not sent stops nothing,
so it also leaves no stopped-by-user mark; it only takes the operator's pause and
says so. The print is closed by the printer's own report (here) or by
reconciliation after a reconnect or restart.

With NO bound run nothing on the printer owns the job, and the door closes the row
itself as before — which, after a restart that lost the binding, still leaves a
later real finish with no ``printing`` archive to book against (pinned below).
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.app import main
from backend.app.api.routes.print_queue import stop_queue_item
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import print_control, usage_tracker
from backend.app.services.print_run_binding import current_print_run, prepared_print_run_conflicts
from backend.app.utils.failure_reasons import USER_CANCELLED
from backend.tests.integration.swap_terminal_support import (  # noqa: F401
    an_execution,
    archive_of,
    complete,
    item_of,
    make_rig,
)
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def unreachable(monkeypatch):
    """The printer of a rig takes no command: the stop cannot be sent."""

    def arrange(rig):
        monkeypatch.setattr(print_control, "printer_manager", rig.manager)
        monkeypatch.setattr(rig.manager, "stop_print", lambda _printer_id: False)
        monkeypatch.setattr(rig.manager, "ensure_fresh_connection", AsyncMock(return_value=False))
        monkeypatch.setattr(main, "_user_stopped_printers", {})

    return arrange


async def _stop(rig, item_id: int):
    """The route itself, on the rig's database, as a caller allowed to stop any item."""
    async with rig.db() as db:
        return await stop_queue_item(item_id, db=db, auth_result=(None, True))


async def _queue(rig) -> PrinterQueue:
    async with rig.db() as db:
        return await db.get(PrinterQueue, rig.queue_id)


async def _stop_that_is_not_sent(rig, item_id: int) -> None:
    with pytest.raises(HTTPException) as refused:
        await _stop(rig, item_id)
    assert refused.value.status_code == 503
    assert refused.value.detail["error"] == "stop_not_sent"


async def test_an_unsent_stop_of_a_bound_run_closes_nothing_and_only_pauses(make_rig, unreachable):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, item_id = await an_execution(rig, fact=False, bound=True)
    unreachable(rig)

    await _stop_that_is_not_sent(rig, item_id)

    archive, item, queue = await archive_of(rig, archive_id), await item_of(rig, item_id), await _queue(rig)
    assert (item.status, item.completed_at, item.error_message) == ("printing", None, None)
    assert (archive.status, archive.completed_at, archive.failure_reason) == ("printing", None, None)
    # The operator's pause only: the queue still says the printer is printing this row.
    assert (queue.is_paused, queue.status, queue.current_item_id) == (True, "printing", item_id)
    # Nothing was stopped, so nothing is marked as stopped by the user.
    assert rig.printer_id not in main._user_stopped_printers
    run = current_print_run(rig.manager, rig.printer_id)
    assert run is not None and run.archive_id == archive_id


async def test_the_real_finish_after_an_unsent_stop_is_a_completed_print(make_rig, unreachable):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, item_id = await an_execution(rig, fact=False, bound=True)
    unreachable(rig)
    await _stop_that_is_not_sent(rig, item_id)

    await complete(rig, {"status": "completed", "subtask_id": "81"})

    archive, item, queue = await archive_of(rig, archive_id), await item_of(rig, item_id), await _queue(rig)
    assert (archive.status, archive.failure_reason) == ("completed", None)
    assert archive.completed_at is not None
    assert item is None or item.status == "completed"
    # The filament is booked: the completion reached the tracker.
    usage_tracker.on_print_complete.assert_awaited()
    # The run is over: nothing blocks the next start, and the queue no longer holds the printer.
    assert current_print_run(rig.manager, rig.printer_id) is None
    assert not prepared_print_run_conflicts(rig.manager, rig.printer_id)
    assert queue.status != "printing"
    # ... and the operator's pause stands until the operator lifts it.
    assert queue.is_paused is True


async def test_a_real_failure_after_an_unsent_stop_is_not_relabelled_as_cancelled(make_rig, unreachable):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, item_id = await an_execution(rig, fact=False, bound=True)
    unreachable(rig)
    await _stop_that_is_not_sent(rig, item_id)

    await complete(rig, {"status": "failed", "subtask_id": "81"})

    archive = await archive_of(rig, archive_id)
    assert archive.status == "failed"
    assert archive.failure_reason != USER_CANCELLED
    assert current_print_run(rig.manager, rig.printer_id) is None


async def test_without_a_bound_run_the_door_closes_the_row_and_a_later_finish_finds_nothing(make_rig, unreachable):
    """No binding (still being sent, or lost in a restart): the door closes as before."""
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, item_id = await an_execution(rig, fact=False, bound=False)
    unreachable(rig)

    answer = await _stop(rig, item_id)

    assert answer == {"message": "Queue item cancelled (printer was offline)"}
    archive, item, queue = await archive_of(rig, archive_id), await item_of(rig, item_id), await _queue(rig)
    assert (item.status, item.error_message) == ("cancelled", "Stopped by user (printer was offline)")
    assert (archive.status, archive.failure_reason) == ("cancelled", USER_CANCELLED)
    assert (queue.status, queue.current_item_id) == ("paused", None)

    await complete(rig, {"status": "completed", "filename": "job.gcode.3mf", "subtask_name": "job"})

    archive = await archive_of(rig, archive_id)
    assert (archive.status, archive.failure_reason) == ("cancelled", USER_CANCELLED)
    # "Could not find archive for print complete": the handler returns before the tracker.
    usage_tracker.on_print_complete.assert_not_awaited()
    assert current_print_run(rig.manager, rig.printer_id) is None
