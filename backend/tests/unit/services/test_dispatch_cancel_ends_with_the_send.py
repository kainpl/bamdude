"""The dispatch window cancels a dispatch -- and a dispatch ends when the start command is sent.

Owner, 2026-11-18: once the command has gone out there is a print, and a print is stopped from
the printer's card. Before this the Cancel button stayed live through the wait for the printer's
answer, where nothing reads the cancel: it said "Cancelling" and the print started.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.app.services.background_dispatch import (
    SEND_ACCEPTED,
    SEND_REFUSED,
    SEND_SENT,
    ActiveDispatchState,
    BackgroundDispatchService,
    PrintDispatchJob,
)


def _active(service, send_fact=None):
    job = PrintDispatchJob(
        id=42, kind="reprint_archive", source_id=1, source_name="gearbox.gcode.3mf", printer_id=3, printer_name="P"
    )
    job.send_fact = send_fact
    service._active_jobs[job.id] = ActiveDispatchState(job=job, message="…")
    return job


@pytest.mark.asyncio
@pytest.mark.parametrize("send_fact", [SEND_SENT, SEND_ACCEPTED])
async def test_a_job_whose_start_was_sent_is_not_cancelled(send_fact):
    service = BackgroundDispatchService()
    job = _active(service, send_fact)

    with patch("backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock) as broadcast:
        result = await service.cancel_job(job.id)

    assert result == {"cancelled": False, "reason": "start_sent"}
    assert job.id not in service._cancel_requested_job_ids
    broadcast.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("send_fact", [None, SEND_REFUSED])
async def test_before_the_send_a_cancel_is_taken_as_before(send_fact):
    service = BackgroundDispatchService()
    job = _active(service, send_fact)

    with patch("backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock):
        result = await service.cancel_job(job.id)

    assert result["cancelled"] is True and job.id in service._cancel_requested_job_ids


@pytest.mark.parametrize(
    ("send_fact", "cancellable"), [(None, True), (SEND_REFUSED, True), (SEND_SENT, False), (SEND_ACCEPTED, False)]
)
def test_the_window_is_told_whether_the_job_can_still_be_cancelled(send_fact, cancellable):
    service = BackgroundDispatchService()
    _active(service, send_fact)

    payload = service._build_state_payload_unlocked()

    assert [job["cancellable"] for job in payload["active_jobs"]] == [cancellable]
    assert payload["active_job"]["cancellable"] is cancellable


@pytest.mark.asyncio
async def test_the_route_says_the_print_is_stopped_from_the_printer(async_client: AsyncClient):
    with patch(
        "backend.app.services.background_dispatch.background_dispatch.cancel_job",
        new=AsyncMock(return_value={"cancelled": False, "reason": "start_sent"}),
    ):
        response = await async_client.delete("/api/v1/background-dispatch/7")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "start_sent"


def test_both_runners_tell_the_window_when_the_command_has_gone():
    """The window learns of the send from a broadcast; without one the button would stay until the next message."""
    import inspect

    from backend.app.services.background_dispatch import BackgroundDispatchService as Service

    for runner in (Service._run_reprint_archive, Service._run_print_library_file):
        source = inspect.getsource(runner)
        sent = source.index("await self._record_phase(db, job, dispatch_phase.SENT)")
        assert 'phase="sent"' in source[sent : sent + 600], runner.__name__
