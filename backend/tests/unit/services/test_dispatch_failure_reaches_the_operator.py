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
