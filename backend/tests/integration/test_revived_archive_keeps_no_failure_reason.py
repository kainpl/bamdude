"""A row taken back as the running print keeps nothing of the end it had (upstream a8270b8fa / bb0bf03f6).

Ours creates a new archive for every reprint, so upstream's "reprint keeps the old
reason" cannot happen. The one row we do reuse: the dispatch watchdog fails a slow
attempt (``failed`` + a dispatch reason + its message + an end time) and the printer
then reports that very job running. The print that completed still read "dispatch:
no acknowledgement"; one that really failed kept the dispatch reason.
"""

from datetime import datetime, timezone

import pytest

from backend.app.models.archive import PrintArchive
from backend.app.services.archive import ArchiveService
from backend.app.services.print_execution import decide_start, publish_start
from backend.app.utils.failure_reasons import DISPATCH_NO_ACKNOWLEDGEMENT, USER_CANCELLED
from backend.tests.integration.swap_terminal_support import (  # noqa: F401
    an_execution,
    archive_of,
    complete,
    make_rig,
)
from backend.tests.integration.test_duplicate_print_execution import setup
from backend.tests.unit.services.test_terminal_queue_repair import telemetry  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

MESSAGE = "The printer did not acknowledge the print command"


async def test_the_start_that_takes_a_failed_dispatch_back_clears_its_end(db_session, printer_factory, archive_factory):
    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    a.status, item.status, a.dispatch_phase = "failed", "failed", "sent"
    a.failure_reason, a.error_message = DISPATCH_NO_ACKNOWLEDGEMENT, MESSAGE
    a.completed_at = datetime.now(timezone.utc)
    a.extra_data = {"dispatch_intent": {"version": 1, "submission_id": "100"}}
    await db_session.commit()

    decision = await decide_start(
        db_session,
        p.id,
        {"subtask_id": "100"},
        prepared_archive_id=a.id,
        prepared_archive_identity=a.recovery_identity,
    )
    assert decision.current_id == a.id
    await publish_start(db_session, a, decision)

    await db_session.refresh(a)
    assert a.status == "printing"
    assert (a.failure_reason, a.error_message, a.completed_at) == (None, None, None)


async def _stale_reason(rig, archive_id):
    async with rig.db() as db:
        archive = await db.get(PrintArchive, archive_id)
        archive.failure_reason = DISPATCH_NO_ACKNOWLEDGEMENT
        await db.commit()


async def test_a_bound_print_that_completes_carries_no_reason(make_rig):
    rig = await make_rig(require_plate_clear=False, swap_mode=False)
    archive_id, _ = await an_execution(rig, fact=False)
    await _stale_reason(rig, archive_id)

    await complete(rig, {"status": "completed", "subtask_id": "81"})

    archive = await archive_of(rig, archive_id)
    assert archive.status == "completed"
    assert archive.failure_reason is None


@pytest.mark.parametrize(
    ("status", "given", "kept"),
    [
        ("completed", None, None),
        ("printing", None, None),
        # A failure status never loses a reason an earlier writer of this end recorded.
        ("failed", None, DISPATCH_NO_ACKNOWLEDGEMENT),
        ("cancelled", None, DISPATCH_NO_ACKNOWLEDGEMENT),
        ("cancelled", USER_CANCELLED, USER_CANCELLED),
    ],
)
async def test_the_status_writer_clears_the_reason_only_for_a_status_that_is_no_failure(
    db_session, printer_factory, archive_factory, status, given, kept
):
    printer = await printer_factory()
    archive = await archive_factory(
        printer_id=printer.id, status="failed", failure_reason=DISPATCH_NO_ACKNOWLEDGEMENT, file_path=""
    )
    archive_id = archive.id

    assert await ArchiveService(db_session).update_archive_status(archive_id, status, failure_reason=given)

    row = await db_session.get(PrintArchive, archive_id, populate_existing=True)
    assert row.status == status
    assert row.failure_reason == kept
