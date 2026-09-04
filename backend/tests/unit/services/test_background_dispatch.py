"""Unit tests for background dispatch service."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.background_dispatch import (
    ActiveDispatchState,
    BackgroundDispatchService,
    DispatchEnqueueRejected,
    PrintDispatchJob,
)


@pytest.fixture
def dispatch_claim_db(monkeypatch, db_session):
    """Point the enqueue path's claim at the test session.

    ⚠️ Required by every test that reaches ``_dispatch``: accepting a direct
    print now writes the printer's queue claim, so the enqueue path is no
    longer pure. Without this the claim runs against the configured engine with
    an unconfigured mapper registry, which fails here *and* poisons the
    registry for every test file that runs after this one in the same process.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.services.background_dispatch.async_session", _session_ctx)


@pytest.mark.asyncio
async def test_dispatch_rejects_when_printer_busy_printing():
    """Reject enqueue when target printer is already printing."""
    service = BackgroundDispatchService()

    with (
        patch(
            "backend.app.services.background_dispatch.printer_manager.get_status",
            return_value=SimpleNamespace(state="RUNNING", gcode_file="active.gcode.3mf"),
        ),
        pytest.raises(DispatchEnqueueRejected, match="currently busy printing"),
    ):
        await service.dispatch_reprint_archive(
            archive_id=1,
            archive_name="Test Archive",
            printer_id=10,
            printer_name="Printer A",
            options={},
            requested_by_user_id=None,
            requested_by_username=None,
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dispatch_enqueues_job_and_broadcasts_state(dispatch_claim_db):
    """Enqueue succeeds and emits websocket queue update."""
    service = BackgroundDispatchService()

    with (
        patch("backend.app.services.background_dispatch.printer_manager.get_status", return_value=None),
        patch(
            "backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock
        ) as mock_broadcast,
    ):
        result = await service.dispatch_print_library_file(
            file_id=22,
            filename="cube.gcode.3mf",
            printer_id=7,
            printer_name="Printer B",
            options={"plate_id": 2},
            requested_by_user_id=5,
            requested_by_username="tester",
        )

    assert result["status"] == "dispatched"
    assert result["dispatch_job_id"] == 1
    assert result["dispatch_position"] == 1
    assert len(service._queued_jobs) == 1

    mock_broadcast.assert_awaited_once()
    payload = mock_broadcast.await_args.args[0]
    assert payload["type"] == "background_dispatch"
    assert payload["data"]["recent_event"]["status"] == "dispatched"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_claim_a_direct_print_takes_says_it_is_direct(dispatch_claim_db, db_session, printer_factory):
    """⚠️ The claim is what "Print now" leaves behind in the queue, and it is
    NOT queued work — nobody scheduled it. Stamping it here is what keeps the
    queue-completed notifications off a print the operator started by hand
    (m160); the other two stamps are the scheduler's default and the external
    one made at print start.
    """
    from sqlalchemy import select

    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue

    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    await db_session.commit()

    service = BackgroundDispatchService()
    with (
        patch("backend.app.services.background_dispatch.printer_manager.get_status", return_value=None),
        patch("backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock),
    ):
        await service.dispatch_print_library_file(
            file_id=22,
            filename="cube.gcode.3mf",
            printer_id=printer.id,
            printer_name=printer.name,
            options={},
            requested_by_user_id=None,
            requested_by_username=None,
        )

    rows = (
        (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.queue_id == printer.id))).scalars().all()
    )
    assert [r.origin for r in rows] == ["direct"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_direct_print_carries_the_line_the_claim_resolved(dispatch_claim_db, db_session, printer_factory):
    """⚠️ The claim resolves the order LINE; the job is what stamps the ARCHIVE.

    ``claim_printer_for_direct_print`` files the line when the caller named only
    an order (spec pass 7, Decision 4a) — and ``_run_print_library_file`` calls
    ``archive_print(project_line_id=job.project_line_id)``. Built from the
    caller's original ids, the job carried ``None``: the queue row knew the line
    and the print history did not, so a "print now" answered in the dialog's
    Order field landed in the archive unfiled and the order page had to
    re-derive it from the product every time.
    """
    from sqlalchemy import select

    from backend.app.models.library import LibraryFile
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.models.product import Product, ProductPart, ProductPlate
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine

    printer = await printer_factory()
    db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
    file = LibraryFile(
        filename="lamp.gcode.3mf",
        file_path="lamp.gcode.3mf",
        file_type="gcode",
        file_size=1,
        file_metadata={
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {"1": "shade"},
                    "print_time_seconds": 100,
                    "filaments": [{"slot_id": 1, "type": "PETG"}],
                }
            ]
        },
    )
    product = Product(name="Lamp")
    project = Project(name="O")
    db_session.add_all([file, product, project])
    await db_session.flush()
    db_session.add_all(
        [
            ProductPart(product_id=product.id, kind="printed", name="shade", name_key="shade", qty_per_unit=1),
            ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=0),
        ]
    )
    line = ProjectLine(project_id=project.id, product_id=product.id, quantity=1, material="PETG")
    db_session.add(line)
    await db_session.commit()

    service = BackgroundDispatchService()
    with (
        patch("backend.app.services.background_dispatch.printer_manager.get_status", return_value=None),
        patch("backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock),
    ):
        await service.dispatch_print_library_file(
            file_id=file.id,
            filename=file.filename,
            printer_id=printer.id,
            printer_name=printer.name,
            options={"plate_id": 1},
            requested_by_user_id=None,
            requested_by_username=None,
            # The dialog's Order field answered, with no line named — exactly
            # what every door other than the plan block sends.
            project_id=project.id,
        )

    (job,) = service._queued_jobs
    assert (job.project_id, job.project_line_id) == (project.id, line.id)

    # ...and it is the SAME answer the queue row got, not a second derivation.
    (claim,) = (
        (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.queue_id == printer.id))).scalars().all()
    )
    assert (claim.project_id, claim.project_line_id) == (job.project_id, job.project_line_id)


def test_the_library_file_archive_is_stamped_from_the_job():
    """The other half of the test above, which cannot be driven without a
    printer and an FTP server: the archive row this job becomes takes its order
    ids from the JOB. Read off the source because the alternative is a full
    dispatch; the assertion above is what makes the job's own value right.
    """
    import inspect

    from backend.app.services import background_dispatch as module

    source = inspect.getsource(module.BackgroundDispatchService._run_print_library_file)
    assert "project_id=job.project_id" in source
    assert "project_line_id=job.project_line_id" in source


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancel_queued_job_removes_it_and_broadcasts(dispatch_claim_db):
    """Cancelling queued job removes it immediately."""
    service = BackgroundDispatchService()

    with (
        patch("backend.app.services.background_dispatch.printer_manager.get_status", return_value=None),
        patch(
            "backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock
        ) as mock_broadcast,
    ):
        result = await service.dispatch_reprint_archive(
            archive_id=1,
            archive_name="benchy.gcode.3mf",
            printer_id=1,
            printer_name="Printer 1",
            options={},
            requested_by_user_id=None,
            requested_by_username=None,
        )
        mock_broadcast.reset_mock()

        cancel_result = await service.cancel_job(result["dispatch_job_id"])

    assert cancel_result["cancelled"] is True
    assert cancel_result["pending"] is False
    assert len(service._queued_jobs) == 0
    assert service._batch_total == 0

    mock_broadcast.assert_awaited_once()
    payload = mock_broadcast.await_args.args[0]
    assert payload["data"]["recent_event"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_active_job_marks_pending_and_sets_cancel_flag():
    """Cancelling active job marks it as pending cancellation."""
    service = BackgroundDispatchService()
    job = PrintDispatchJob(
        id=42,
        kind="reprint_archive",
        source_id=100,
        source_name="gearbox.gcode.3mf",
        printer_id=3,
        printer_name="Printer C",
    )
    service._active_jobs[job.id] = ActiveDispatchState(job=job, message="Uploading...")

    with patch(
        "backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock
    ) as mock_broadcast:
        result = await service.cancel_job(job.id)

    assert result["cancelled"] is True
    assert result["pending"] is True
    assert job.id in service._cancel_requested_job_ids

    mock_broadcast.assert_awaited_once()
    payload = mock_broadcast.await_args.args[0]
    assert payload["data"]["recent_event"]["status"] == "cancelling"


def test_resolve_plate_id_uses_request_value_when_provided(tmp_path):
    """Explicit plate_id wins over auto-detection."""
    file_path = tmp_path / "dummy.3mf"
    file_path.write_text("not-a-zip")

    plate_id = BackgroundDispatchService._resolve_plate_id(file_path, requested_plate_id=9)
    assert plate_id == 9


def test_resolve_plate_id_auto_detects_from_3mf(tmp_path):
    """Auto-detect plate from Metadata/plate_X.gcode entry."""
    import zipfile

    file_path = tmp_path / "multi.3mf"
    with zipfile.ZipFile(file_path, "w") as zf:
        zf.writestr("Metadata/plate_7.gcode", b"G1 X0 Y0")

    plate_id = BackgroundDispatchService._resolve_plate_id(file_path, requested_plate_id=None)
    assert plate_id == 7


def test_is_sliced_file_recognizes_supported_extensions():
    """Only .gcode and .gcode.3mf should be accepted."""
    assert BackgroundDispatchService._is_sliced_file("part.gcode") is True
    assert BackgroundDispatchService._is_sliced_file("part.gcode.3mf") is True
    assert BackgroundDispatchService._is_sliced_file("part.3mf") is False


@pytest.mark.asyncio
async def test_cancel_job_not_found_returns_false():
    """Cancelling a nonexistent job returns not_found."""
    service = BackgroundDispatchService()

    with patch("backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock):
        result = await service.cancel_job(999)

    assert result["cancelled"] is False
    assert result["reason"] == "not_found"


@pytest.mark.asyncio
async def test_cancel_job_single_lock_covers_both_active_and_queued():
    """cancel_job checks both active and queued jobs under a single lock acquisition.

    Regression test for TOCTOU race: previously two separate lock acquisitions allowed
    the dispatcher loop to move a job from queue to active between them, causing cancel
    to find it in neither place.
    """
    service = BackgroundDispatchService()

    # Set up a job in the queue AND an active job for a different printer
    active_job = PrintDispatchJob(
        id=1,
        kind="reprint_archive",
        source_id=10,
        source_name="active.3mf",
        printer_id=1,
        printer_name="Printer 1",
    )
    service._active_jobs[active_job.id] = ActiveDispatchState(job=active_job, message="Uploading...")

    queued_job = PrintDispatchJob(
        id=2,
        kind="reprint_archive",
        source_id=20,
        source_name="queued.3mf",
        printer_id=2,
        printer_name="Printer 2",
    )
    service._queued_jobs.append(queued_job)
    service._batch_total = 2

    with patch(
        "backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock
    ) as mock_broadcast:
        # Cancel the queued job - should find it in single lock acquisition
        result = await service.cancel_job(2)

    assert result["cancelled"] is True
    assert result["pending"] is False
    assert len(service._queued_jobs) == 0
    # Active job should be untouched
    assert 1 in service._active_jobs

    mock_broadcast.assert_awaited_once()
    payload = mock_broadcast.await_args.args[0]
    assert payload["data"]["recent_event"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_mark_job_finished_resets_batch_when_all_done():
    """Batch counters reset after last job completes."""
    service = BackgroundDispatchService()
    job = PrintDispatchJob(
        id=1,
        kind="reprint_archive",
        source_id=10,
        source_name="test.3mf",
        printer_id=1,
        printer_name="Printer 1",
    )
    service._active_jobs[job.id] = ActiveDispatchState(job=job, message="Done")
    service._batch_total = 1

    with patch("backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock):
        await service._mark_job_finished(job, failed=False, message="Complete")

    assert service._batch_total == 0
    assert service._batch_completed == 0
    assert service._batch_failed == 0


@pytest.mark.asyncio
async def test_mark_job_finished_no_reset_when_jobs_remain():
    """Batch counters NOT reset when queued jobs remain."""
    service = BackgroundDispatchService()
    job = PrintDispatchJob(
        id=1,
        kind="reprint_archive",
        source_id=10,
        source_name="test.3mf",
        printer_id=1,
        printer_name="Printer 1",
    )
    remaining_job = PrintDispatchJob(
        id=2,
        kind="reprint_archive",
        source_id=20,
        source_name="next.3mf",
        printer_id=2,
        printer_name="Printer 2",
    )
    service._active_jobs[job.id] = ActiveDispatchState(job=job, message="Done")
    service._queued_jobs.append(remaining_job)
    service._batch_total = 2

    with patch("backend.app.services.background_dispatch.ws_manager.broadcast", new_callable=AsyncMock):
        await service._mark_job_finished(job, failed=False, message="Complete")

    # Batch counters should NOT be reset - remaining job still queued
    assert service._batch_total == 2
    assert service._batch_completed == 1


@pytest.mark.asyncio
async def test_mark_job_finished_batch_reset_rechecks_under_lock():
    """Batch reset re-checks condition inside second lock acquisition.

    Regression test for TOCTOU: a new dispatch between the two lock acquisitions
    could get its counters zeroed if the re-check is missing.
    """
    service = BackgroundDispatchService()
    job = PrintDispatchJob(
        id=1,
        kind="reprint_archive",
        source_id=10,
        source_name="test.3mf",
        printer_id=1,
        printer_name="Printer 1",
    )
    service._active_jobs[job.id] = ActiveDispatchState(job=job, message="Done")
    service._batch_total = 1

    original_broadcast = AsyncMock()

    async def inject_new_job_during_broadcast(msg):
        """Simulate a new dispatch arriving between the two lock acquisitions."""
        await original_broadcast(msg)
        # After broadcast (lock released), inject a new job before reset re-check
        if not service._queued_jobs:
            new_job = PrintDispatchJob(
                id=99,
                kind="reprint_archive",
                source_id=99,
                source_name="injected.3mf",
                printer_id=5,
                printer_name="Printer 5",
            )
            service._queued_jobs.append(new_job)
            service._batch_total = 1

    with patch(
        "backend.app.services.background_dispatch.ws_manager.broadcast",
        side_effect=inject_new_job_during_broadcast,
    ):
        await service._mark_job_finished(job, failed=False, message="Complete")

    # Re-check should prevent reset since a new job appeared
    assert service._batch_total == 1
    assert len(service._queued_jobs) == 1
