"""A print that was not started from a queue must still report its failure.

``notification_service.on_queue_job_failed`` is called from exactly two places,
both in ``print_scheduler`` — so only a queue job that fails to start was ever
announced. Reprint from an archive, print from the library, and the Telegram
bot all enqueue a dispatch job and return "dispatched" straight away; when the
upload or the start then failed, the news went to the dispatch panel over the
websocket and nowhere else.

⚠️ The discriminator is ``queue_item_id``. A queue job is already awaited and
reported by the scheduler, so announcing it here as well would send two
notifications for one failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.background_dispatch import PrintDispatchJob, report_failure_if_unwatched


def _job(**over) -> PrintDispatchJob:
    fields = {
        "id": 1,
        "kind": "print_library_file",
        "source_id": 7,
        "source_name": "Cube.3mf",
        "printer_id": 10,
        "printer_name": "X2D",
    }
    fields.update(over)
    outcome = fields.pop("outcome", None)
    job = PrintDispatchJob(**fields)
    if outcome is not None:
        job.outcome = outcome
    return job


@pytest.fixture
def notify():
    with patch("backend.app.services.notification_service.notification_service") as service:
        service.on_queue_job_failed = AsyncMock()
        yield service.on_queue_job_failed


@pytest.mark.asyncio
async def test_a_direct_print_that_failed_is_announced(notify):
    job = _job(outcome={"success": False, "archive_id": None, "error": "FTP upload failed", "cancelled": False})

    await report_failure_if_unwatched(job)

    notify.assert_awaited_once()
    kwargs = notify.await_args.kwargs
    assert kwargs["printer_id"] == 10
    assert kwargs["job_name"] == "Cube.3mf"
    assert kwargs["reason"] == "FTP upload failed"


@pytest.mark.asyncio
async def test_a_queue_job_is_left_to_the_scheduler(notify):
    """⚠️ Otherwise one failure produces two messages."""
    job = _job(
        queue_item_id=42,
        awaited_by_scheduler=True,
        outcome={"success": False, "archive_id": None, "error": "FTP upload failed", "cancelled": False},
    )

    await report_failure_if_unwatched(job)

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_direct_print_is_announced_even_though_it_has_a_queue_item(notify):
    """⚠️ The discriminator is ``awaited_by_scheduler``, not ``queue_item_id``.

    A direct print carries a queue item of its own now — that row is how it
    claims the printer for the length of its dispatch. Reading the item as
    "the scheduler has this" would leave whoever pressed Print now with no
    word at all that their upload failed: the scheduler only reports the items
    it dispatched itself.
    """
    job = _job(
        queue_item_id=42,
        outcome={"success": False, "archive_id": None, "error": "FTP upload failed", "cancelled": False},
    )

    await report_failure_if_unwatched(job)

    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_successful_print_says_nothing(notify):
    job = _job(outcome={"success": True, "archive_id": 5, "error": None, "cancelled": False})

    await report_failure_if_unwatched(job)

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cancelled_print_is_not_a_failure(notify):
    """The operator who pressed Cancel does not need telling what they did."""
    job = _job(outcome={"success": False, "archive_id": 5, "error": "Cancelled", "cancelled": True})

    await report_failure_if_unwatched(job)

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failure_with_no_message_still_reports_something(notify):
    # start_print returning False leaves no exception text behind. "Nothing
    # happened" is the one thing the operator must not be told.
    job = _job(outcome={"success": False, "archive_id": None, "error": None, "cancelled": False})

    await report_failure_if_unwatched(job)

    assert notify.await_args.kwargs["reason"] == "Dispatch failed"


@pytest.mark.asyncio
async def test_an_unreachable_provider_does_not_replace_the_real_error(notify):
    """This runs on the way out of a dispatch that has already gone wrong."""
    notify.side_effect = OSError("telegram unreachable")
    job = _job(outcome={"success": False, "archive_id": None, "error": "FTP upload failed", "cancelled": False})

    await report_failure_if_unwatched(job)  # must not raise


# --------------------------------------------------------------------------- #
# A refused direct print (spec direct-print-silent-cancel §4.1). The reprint of
# 2026-09-24 was refused at the final guard after its upload and nobody heard:
# ``report_failure_if_unwatched`` skipped every deferral, and it runs before the
# refusal is put into words anyway.
# --------------------------------------------------------------------------- #


class _NullSession:
    """The handler's own session, with nothing behind it — the writers it calls are stubbed."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


def _deferring_service(monkeypatch, *, released: bool):
    from backend.app.services import background_dispatch as bd

    service = bd.BackgroundDispatchService()
    monkeypatch.setattr(bd, "async_session", _NullSession)
    monkeypatch.setattr("backend.app.services.filament_deferred.abort_execution_archive", AsyncMock())
    monkeypatch.setattr("backend.app.services.filament_deferred.defer_claim", AsyncMock(return_value=released))
    monkeypatch.setattr("backend.app.services.print_scheduler.scheduler.release_prepared_dispatch", AsyncMock())
    monkeypatch.setattr(bd.ws_manager, "broadcast", AsyncMock())
    return service


def _finished_event() -> dict:
    """The batch state broadcast by ``_mark_job_finished`` — the tallies reset to 0
    right after it once the batch is empty, so the broadcast is where they are read."""
    from backend.app.services import background_dispatch as bd

    return bd.ws_manager.broadcast.await_args.args[0]["data"]


@pytest.mark.asyncio
async def test_a_deferral_is_left_to_the_deferral_handler(notify):
    """The runner's ``finally`` runs before the refusal is put into words; the
    announcement belongs to ``_handle_routing_deferred``, or the operator would
    read a machine code."""
    job = _job(outcome={"success": False, "archive_id": None, "error": "feed_state_unavailable", "deferred": True})

    await report_failure_if_unwatched(job)

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_direct_deferral_is_announced_with_its_sentence(notify, monkeypatch):
    from backend.app.services.filament_routing import RoutingDeferred

    service = _deferring_service(monkeypatch, released=True)
    job = _job(queue_item_id=5, claim_started_at=object())

    await service._handle_routing_deferred(job, RoutingDeferred("feed_state_unavailable"))

    notify.assert_awaited_once()
    reason = notify.await_args.kwargs["reason"]
    assert reason == job.outcome["reason"]["message"]
    assert reason != "feed_state_unavailable", "a sentence, not the machine code"
    assert _finished_event()["failed"] == 1, "the batch closes as one failed, not a spinning toast"


@pytest.mark.asyncio
async def test_a_rowless_direct_deferral_is_still_announced(notify, monkeypatch):
    """No claim row to release is not a reason to stay silent."""
    from backend.app.services.filament_routing import RoutingDeferred

    service = _deferring_service(monkeypatch, released=False)
    job = _job(queue_item_id=None)

    await service._handle_routing_deferred(job, RoutingDeferred("feed_state_changed"))

    notify.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_deferral_whose_row_was_taken_is_not_a_failure(notify, monkeypatch):
    """The operator cancelled it, another attempt reclaimed it, or it was deleted."""
    from backend.app.services.filament_routing import RoutingDeferred

    service = _deferring_service(monkeypatch, released=False)
    job = _job(queue_item_id=5, claim_started_at=object())

    await service._handle_routing_deferred(job, RoutingDeferred("dispatch_claim_changed"))

    notify.assert_not_awaited()
    assert _finished_event()["failed"] == 0
