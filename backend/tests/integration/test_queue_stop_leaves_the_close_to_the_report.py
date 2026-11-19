"""Stop on a printing queue item: the printer's report closes the print, not the route (0.7.3 field report).

``POST /queue/{id}/stop`` used to cancel the row at once and leave the archive to the completion handler.
The handler accepts a bound run's terminal only while its queue row is still ``printing`` -- so a few seconds
later the report found no row, was refused ("Bound terminal archive … is unresolved; preserving current run"),
and the archive stayed ``printing`` with the run bound in memory: the printer took no further job until
BamDude was restarted.
"""

from unittest.mock import AsyncMock

import pytest

from backend.app.services import print_control
from backend.app.services.print_run_binding import current_print_run
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_print_state_recovery import prepare_stuck_print

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _printer_takes_the_stop(monkeypatch, sent: bool) -> list[int]:
    calls: list[int] = []

    def stop(printer_id, **_kwargs):
        calls.append(printer_id)
        return sent

    monkeypatch.setattr(print_control, "stop_print_by_user", stop)
    monkeypatch.setattr(printer_manager, "ensure_fresh_connection", AsyncMock(return_value=True))
    return calls


async def test_a_running_print_is_left_for_its_own_report_to_close(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    from backend.app.main import _accept_bound_terminal_run

    (printer, queue, archive, item, _pending), _tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch
    )
    calls = _printer_takes_the_stop(monkeypatch, sent=True)

    response = await async_client.post(f"/api/v1/queue/{item.id}/stop")

    assert response.status_code == 200, response.text
    assert calls == [printer.id]
    for row in (queue, archive, item):
        await db_session.refresh(row)
    assert (item.status, archive.status) == ("printing", "printing")
    assert item.completed_at is None

    # The printer's report arrives: the handler finds the row it needs and takes the run.
    binding = current_print_run(printer_manager, printer.id)
    assert await _accept_bound_terminal_run(printer.id, binding, {"status": "cancelled"}) == item.id


async def test_the_report_is_refused_once_its_row_was_closed_ahead_of_it(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    """What the route used to do, written out: this is the state that never recovered."""
    from backend.app.main import _accept_bound_terminal_run

    (printer, _queue, archive, item, _pending), _tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch
    )
    item.status = "cancelled"
    await db_session.commit()

    binding = current_print_run(printer_manager, printer.id)

    assert await _accept_bound_terminal_run(printer.id, binding, {"status": "cancelled"}) is None
    await db_session.refresh(archive)
    assert archive.status == "printing"


async def test_a_job_with_no_bound_run_is_closed_by_the_route(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    """Still being sent or heated: no print is bound, so no report will come for it."""
    (printer, queue, _archive, item, _pending), _tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch, with_context=False
    )
    _printer_takes_the_stop(monkeypatch, sent=True)

    response = await async_client.post(f"/api/v1/queue/{item.id}/stop")

    assert response.status_code == 200, response.text
    for row in (queue, item):
        await db_session.refresh(row)
    assert item.status == "cancelled"
    assert queue.status == "paused" and queue.current_item_id is None


async def test_an_unreachable_printer_leaves_a_bound_run_open_and_says_the_stop_was_not_sent(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    """Not stopped is not closed: the row and the archive wait for the printer's report or reconciliation."""
    from backend.app import main

    (printer, queue, archive, item, _pending), _tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch
    )
    monkeypatch.setattr(print_control.printer_manager, "stop_print", lambda _printer_id: False)
    monkeypatch.setattr(printer_manager, "ensure_fresh_connection", AsyncMock(return_value=False))
    monkeypatch.setattr(main, "_user_stopped_printers", {})

    response = await async_client.post(f"/api/v1/queue/{item.id}/stop")

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "stop_not_sent"
    for row in (queue, archive, item):
        await db_session.refresh(row)
    assert (item.status, archive.status) == ("printing", "printing")
    assert (item.completed_at, archive.completed_at, archive.failure_reason) == (None, None, None)
    assert (queue.is_paused, queue.status, queue.current_item_id) == (True, "printing", item.id)
    assert printer.id not in main._user_stopped_printers
    assert current_print_run(printer_manager, printer.id) is not None


async def test_an_unreachable_printer_with_no_bound_run_closes_the_row_and_its_archive(
    async_client, db_session, printer_factory, archive_factory, monkeypatch
):
    (printer, _queue, archive, item, _pending), _tick = await prepare_stuck_print(
        db_session, printer_factory, archive_factory, monkeypatch, with_context=False
    )
    _printer_takes_the_stop(monkeypatch, sent=False)

    response = await async_client.post(f"/api/v1/queue/{item.id}/stop")

    assert response.status_code == 200, response.text
    for row in (archive, item):
        await db_session.refresh(row)
    assert (item.status, archive.status) == ("cancelled", "cancelled")
