"""G03 B2: a refusal retires exactly one proved AutoQueue placement."""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueReturn, AutoQueueUnsentReturn
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.project import Project
from backend.app.schemas.auto_queue import AutoQueueItemCreate
from backend.app.services import queue_sources, swap_compatibility
from backend.app.services.auto_queue_add import add_items_to_auto_queue
from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
from backend.app.services.auto_queue_unsent_return import UnsentAttempt
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.failure_reasons import DISPATCH_PRINTER_UNAVAILABLE, DISPATCH_UPLOAD_FAILED
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.integration.test_dispatch_without_original import (
    PLATE,
    a_library_source,
    clean_spool_state,  # noqa: F401
    dispatch_mocks,
    drain,
    scheduler_with_captured_dispatch,
    sessions,  # noqa: F401
)
from backend.tests.integration.test_filament_routing_dispatch import setup_source


@pytest.fixture
async def assignment(db_session, tmp_path, printer_factory, monkeypatch, sessions, request):
    service = dispatch_mocks(monkeypatch, sessions)
    _source, printer, queue, mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    source = await a_library_source(db_session, tmp_path)
    [auto] = await add_items_to_auto_queue(
        db_session, AutoQueueItemCreate(library_file_id=source.id, plate_ids=[PLATE], target_model="P1P"), None
    )
    auto_id = auto.id
    if getattr(request, "param", None) == "legacy":
        auto.source_snapshot = swap_compatibility.snapshot_without_fact(auto.source_snapshot)
        await db_session.commit()
    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", sessions)
    router = AutoQueueScheduler()
    await router.tick()
    async with sessions() as db:
        auto = await db.get(AutoQueueItem, auto_id)
        job = await db.get(PrintQueueItem, auto.assigned_to_item_id)
        assert job.placement_basis
        assert job.source_auto_identity == auto.recovery_identity
        return SimpleNamespace(
            auto=auto_id,
            job=job.id,
            printer=printer.id,
            queue=queue.id,
            mqtt=mqtt,
            router=router,
            dispatcher=service,
        )


async def assert_returned(sessions, assignment, code, *, archive):
    async with sessions() as db:
        auto = await db.get(AutoQueueItem, assignment.auto)
        row = await db.get(PrintQueueItem, assignment.job)
        queue = await db.get(PrinterQueue, assignment.queue)
        receipt = (await db.scalars(select(AutoQueueUnsentReturn))).one()
        assert (row.status, row.error_code, row.gate_acknowledged) == ("failed", code, True)
        assert (row.source_auto_item_id, row.source_auto_identity) == (None, auto.recovery_identity)
        assert row.returned_receipt_identity == receipt.id == auto.unsent_return_proof["receipt_identity"]
        assert (auto.status, auto.assigned_to_item_id, auto.assigned_at) == ("pending", None, None)
        assert (auto.unsent_return_count, auto.failed_return_count, auto.failed_return_proof) == (1, 0, None)
        assert (receipt.old_job_identity, receipt.auto_identity, receipt.failure_code, receipt.ordinal) == (
            row.recovery_identity,
            auto.recovery_identity,
            code,
            1,
        )
        assert (queue.status, queue.current_item_id, queue.pending_count) == ("error", None, 0)
        assert not await db.scalar(select(func.count(AutoQueueReturn.id)))
        if archive:
            execution = await db.get(PrintArchive, receipt.execution_archive_id)
            assert (execution.status, execution.failure_reason) == ("failed", code)
            assert execution.recovery_identity == receipt.execution_archive_identity
        else:
            assert receipt.execution_archive_id is receipt.execution_archive_identity is None
        return auto


async def test_refused_upload_returns_the_same_job_and_halts_the_printer(
    assignment, sessions, monkeypatch, db_session, tmp_path, printer_factory
):
    dispatch_mocks(monkeypatch, sessions, upload=AsyncMock(return_value=False))
    notify = AsyncMock()
    monkeypatch.setattr("backend.app.services.print_scheduler.notification_service.on_queue_job_failed", notify)
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    async with sessions() as db:
        auto = await db.get(AutoQueueItem, assignment.auto)
        saved = (auto.position, auto.filament_overrides, auto.plate_id, auto.flow_cali, auto.recovery_identity)

    assert await scheduler.check_queue()
    assert await drain(spawned) == 1

    auto = await assert_returned(sessions, assignment, DISPATCH_UPLOAD_FAILED, archive=True)
    assert (auto.position, auto.filament_overrides, auto.plate_id, auto.flow_cali, auto.recovery_identity) == saved
    notify.assert_awaited_once()
    assert "The job went back to AutoQueue." in notify.await_args.kwargs["reason"]
    assignment.mqtt._client.publish.assert_not_called()

    second = tmp_path / "second"
    second.mkdir()
    _source, printer, _, _mqtt = await setup_source(db_session, second, printer_factory, monkeypatch)
    await assignment.router.tick()
    async with sessions() as db:
        auto = await db.get(AutoQueueItem, assignment.auto)
        successor = await db.get(PrintQueueItem, auto.assigned_to_item_id)
        assert successor.queue_id == printer.id != assignment.printer
        assert successor.source_auto_identity == auto.recovery_identity
        assert successor.id != assignment.job


async def test_third_refusal_stays_failed_after_resume_and_new_scheduler(
    assignment, sessions, committing_client, monkeypatch
):
    for ordinal in (1, 2, 3):
        scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)
        async with sessions() as db:
            auto = await db.get(AutoQueueItem, assignment.auto)
            row = await db.get(PrintQueueItem, auto.assigned_to_item_id)
            outcome = await scheduler._fail_item(
                db,
                row,
                "Printer unavailable",
                DISPATCH_PRINTER_UNAVAILABLE,
                storage_epoch=queue_sources.current_epoch(),
            )
            await db.refresh(auto)
            await db.refresh(row)
            assert outcome.written and outcome.returned is (ordinal < 3)
            assert auto.unsent_return_count == min(ordinal, 2) and auto.failed_return_count == 0
            if ordinal == 3:
                assert auto.status == "assigned" and auto.assigned_to_item_id == row.id
                assert row.waiting_reason == "unsent_return_refused:unsent_return_limit"
                assert await db.scalar(select(func.count(AutoQueueUnsentReturn.id))) == 2
        if ordinal < 3:
            result = await committing_client.patch(
                f"/api/v1/queues/{assignment.queue}", json={"status": "idle", "is_paused": False}
            )
            assert result.status_code == 200, result.text
            await AutoQueueScheduler().tick()


async def test_proved_preclaim_disconnect_returns_without_an_execution(assignment, sessions, monkeypatch):
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    monkeypatch.setattr(printer_manager, "ensure_fresh_connection_for_printer", AsyncMock(return_value=False))
    async with sessions() as db:
        row = await db.get(PrintQueueItem, assignment.job)
        assert not await scheduler._start_print(db, row)
    assert not spawned
    await assert_returned(sessions, assignment, DISPATCH_PRINTER_UNAVAILABLE, archive=False)


@pytest.mark.parametrize("assignment", ["legacy"], indirect=True)
async def test_promotion_resolves_a_legacy_fact_without_changing_the_returned_ancestor(
    assignment, sessions, monkeypatch
):
    from backend.app.services import swap_compatibility as sc

    async with sessions() as db:
        auto = await db.get(AutoQueueItem, assignment.auto)
        job = await db.get(PrintQueueItem, assignment.job)
        assert sc.snapshot_fact(auto.source_snapshot).missing
        assert not sc.snapshot_fact(job.source_snapshot).missing
        ancestor_snapshot = auto.source_snapshot
        job.position += 20
        job.batch_id = "operator-group"
        await db.commit()
    dispatch_mocks(monkeypatch, sessions, upload=AsyncMock(return_value=False))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    assert await scheduler.check_queue()
    assert await drain(spawned) == 1
    auto = await assert_returned(sessions, assignment, DISPATCH_UPLOAD_FAILED, archive=True)
    assert auto.source_snapshot == ancestor_snapshot
    assert auto.batch_id is None


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("edited", "placement_changed"),
        ("missing_basis", "placement_changed"),
        ("future_basis", "placement_changed"),
        ("rack", "policy_changed"),
        ("bound", "unsent_return_limit"),
        ("storage", "storage_changed"),
        ("closed_order", "order_changed"),
        ("pinned", "policy_changed"),
        ("source", "source_changed"),
        ("changed_declaration", "source_changed"),
        ("legacy", None),
        ("position", None),
    ],
)
async def test_placement_and_ancestor_must_still_match(assignment, sessions, monkeypatch, change, expected):
    from backend.app.services import swap_compatibility as sc
    from backend.app.services.auto_queue_placement_basis import placement_basis
    from backend.app.services.auto_queue_unsent_return import REFUSAL_PREFIX

    epoch = queue_sources.current_epoch()
    async with sessions() as db:
        job = await db.get(PrintQueueItem, assignment.job)
        auto = await db.get(AutoQueueItem, assignment.auto)
        if change == "edited":
            job.flow_cali = not job.flow_cali
        elif change in {"missing_basis", "future_basis"}:
            job.placement_basis = None if change == "missing_basis" else "v2:unknown"
        elif change == "rack":
            job.nozzle_rack_choice = "{}"
            job.placement_basis = placement_basis(job)
        elif change == "pinned":
            job.filament_routing = None
            job.placement_basis = placement_basis(job)
        elif change == "bound":
            auto.unsent_return_count = 2
        elif change == "storage":
            epoch -= 1
        elif change == "closed_order":
            project = Project(name="Closed order", status="completed")
            db.add(project)
            await db.flush()
            auto.project_id = job.project_id = project.id
            job.placement_basis = placement_basis(job)
        elif change == "source":
            auto.source_snapshot = {**auto.source_snapshot, "changed": True}
        elif change == "changed_declaration":
            auto.source_snapshot = sc.snapshot_with_fact(
                auto.source_snapshot, sc.SourceFact(not sc.snapshot_fact(job.source_snapshot).value)
            )
        elif change == "legacy":
            auto.source_snapshot = sc.snapshot_without_fact(auto.source_snapshot)
        elif change == "position":
            job.position += 5
        await db.commit()

    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)
    async with sessions() as db:
        item = await db.get(PrintQueueItem, assignment.job)
        result = await scheduler._fail_item(
            db, item, "Printer refused", DISPATCH_PRINTER_UNAVAILABLE, storage_epoch=epoch
        )
    assert result.written and result.returned is (expected is None)
    if expected is None:
        auto = await assert_returned(sessions, assignment, DISPATCH_PRINTER_UNAVAILABLE, archive=False)
        if change == "legacy":
            assert sc.snapshot_fact(auto.source_snapshot).missing
    else:
        async with sessions() as db:
            row = await db.get(PrintQueueItem, assignment.job)
            auto = await db.get(AutoQueueItem, assignment.auto)
            assert (row.status, row.error_code, row.waiting_reason) == (
                "failed",
                DISPATCH_PRINTER_UNAVAILABLE,
                REFUSAL_PREFIX + expected,
            )
            assert (auto.status, auto.assigned_to_item_id) == ("assigned", row.id)
            assert not await db.scalar(select(func.count(AutoQueueUnsentReturn.id)))


async def prepare_claim(sessions, assignment, *, archive_phase=None):
    """Seed the addressed state after claim, and (optionally) archive creation, before the wrapper returns."""
    async with sessions() as db:
        row = await db.get(PrintQueueItem, assignment.job)
        header = await db.get(PrinterQueue, assignment.queue)
        started_at = datetime.now(timezone.utc)
        row.status, row.started_at, row.dispatch_attempts = "printing", started_at, 1
        header.status, header.current_item_id = "printing", row.id
        archive = None
        if archive_phase is not None:
            archive = PrintArchive(
                printer_id=assignment.printer,
                filename="execution.3mf",
                file_path="",
                file_size=0,
                status="printing",
                dispatch_phase=archive_phase,
                queue_item_identity=row.recovery_identity,
                source_auto_identity=row.source_auto_identity,
            )
            swap_compatibility.write_archive_fact(archive, False)
            db.add(archive)
            await db.flush()
            row.archive_id = archive.id
        await db.commit()
        return started_at, (archive.id, archive.recovery_identity) if archive else (None, None)


@pytest.mark.parametrize(
    "case",
    [
        "missing_send_fact",
        "sent",
        "uncertain",
        "raised",
        "fired",
        "possibly_fired",
        "sending",
        "wrong_lineage",
        "reused_id",
        "missing_archive",
        "no_archive",
        "archive_close_failed",
    ],
)
async def test_wrapper_proof_and_durable_archive_gate_the_return(assignment, sessions, monkeypatch, case):
    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)
    phase = "sending" if case == "sending" else "prepared"
    token, (archive_id, archive_identity) = await prepare_claim(
        sessions,
        assignment,
        archive_phase=None if case == "no_archive" else phase,
    )
    if case in {"wrong_lineage", "sending"}:
        async with sessions() as db:
            archive = await db.get(PrintArchive, archive_id)
            if case == "wrong_lineage":
                archive.queue_item_identity = "a-different-attempt"
            else:
                # This negative control would pass if the phase check alone disappeared.
                archive.status, archive.failure_reason = "failed", DISPATCH_UPLOAD_FAILED
            await db.commit()

    async def wrapper(**_):
        if case == "raised":
            raise RuntimeError("no typed wrapper response")
        outcome = {
            "success": False,
            "failure_code": DISPATCH_UPLOAD_FAILED,
            "error": "upload refused",
            "archive_terminal": "failed",
            "send_fact": "sent" if case == "sent" else None,
            "uncertain": case == "uncertain",
            "swap_start_effect": case if case in {"fired", "possibly_fired"} else None,
            "execution_archive_id": 9000 if case == "missing_archive" else archive_id,
            "execution_archive_identity": "different-archive" if case == "reused_id" else archive_identity,
        }
        if case == "missing_send_fact":
            outcome.pop("send_fact")
        return outcome

    monkeypatch.setattr(assignment.dispatcher, "run_from_queue_item", wrapper)
    await scheduler._dispatch_and_finalize(
        queue_item_id=assignment.job,
        printer_id=assignment.printer,
        printer_name="Printer",
        printer_serial=None,
        dispatch_kind="print_library_file",
        dispatch_source_id=None,
        dispatch_source_name="test",
        options={},
        requested_by_user_id=None,
        project_id=None,
        project_line_id=None,
        job_name_short="test",
        swap_events=[],
        claim_started_at=token,
        storage_epoch=queue_sources.current_epoch(),
    )
    if case in {"no_archive", "archive_close_failed"}:
        await assert_returned(sessions, assignment, DISPATCH_UPLOAD_FAILED, archive=archive_id is not None)
    else:
        async with sessions() as db:
            row = await db.get(PrintQueueItem, assignment.job)
            auto = await db.get(AutoQueueItem, assignment.auto)
            assert row.status == ("printing" if case == "uncertain" else "failed")
            assert row.returned_receipt_identity is None
            assert (auto.status, auto.unsent_return_count, auto.assigned_to_item_id) == ("assigned", 0, row.id)
            assert not await db.scalar(select(func.count(AutoQueueUnsentReturn.id)))


@pytest.mark.parametrize("landed", [False, True])
async def test_commit_unknown_replay_writes_no_second_return(assignment, sessions, monkeypatch, landed):
    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)
    token, (archive_id, identity) = await prepare_claim(sessions, assignment, archive_phase="prepared")
    args = {
        "item_id": assignment.job,
        "queue_id": assignment.queue,
        "claim_started_at": token,
        "message": "Upload refused",
        "code": DISPATCH_UPLOAD_FAILED,
        "sent": False,
        "execution_archive_id": archive_id,
        "execution_archive_identity": identity,
        "unsent_attempt": UnsentAttempt(queue_sources.current_epoch(), archive_id, identity),
    }

    def fail_commit(session):
        if not session.in_nested_transaction():
            raise RuntimeError("commit interrupted")

    async with sessions() as db:
        event.listen(db.sync_session, "after_commit" if landed else "before_commit", fail_commit)
        with pytest.raises(RuntimeError, match="commit interrupted"):
            await scheduler._fail_attempt(db, **args)
    if landed:
        await assert_returned(sessions, assignment, DISPATCH_UPLOAD_FAILED, archive=True)
    else:
        async with sessions() as db:
            header = await db.get(PrinterQueue, assignment.queue)
            row = await db.get(PrintQueueItem, assignment.job)
            auto = await db.get(AutoQueueItem, assignment.auto)
            archive = await db.get(PrintArchive, archive_id)
            assert (header.status, header.current_item_id) == ("printing", row.id)
            assert (row.status, row.returned_receipt_identity) == ("printing", None)
            assert (auto.status, auto.unsent_return_count) == ("assigned", 0)
            assert archive.status == "printing"
            assert not await db.scalar(select(func.count(AutoQueueUnsentReturn.id)))
    async with sessions() as db:
        result = await scheduler._fail_attempt(db, **args)
        assert result.returned is (not landed)
    await assert_returned(sessions, assignment, DISPATCH_UPLOAD_FAILED, archive=True)


async def test_a_return_exception_leaves_the_failure_and_archive_committed(assignment, sessions, monkeypatch):
    from backend.app.services import auto_queue_unsent_return

    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)
    token, (archive_id, identity) = await prepare_claim(sessions, assignment, archive_phase="prepared")
    real = auto_queue_unsent_return._build_return

    async def after_writes(db, *args, **kwargs):
        assert await real(db, *args, **kwargs) is None
        await db.flush()
        raise RuntimeError("abandon the optional return")

    monkeypatch.setattr(auto_queue_unsent_return, "_build_return", after_writes)
    async with sessions() as db:
        result = await scheduler._fail_attempt(
            db,
            item_id=assignment.job,
            queue_id=assignment.queue,
            claim_started_at=token,
            message="Upload refused",
            code=DISPATCH_UPLOAD_FAILED,
            sent=False,
            execution_archive_id=archive_id,
            execution_archive_identity=identity,
            unsent_attempt=UnsentAttempt(queue_sources.current_epoch(), archive_id, identity),
        )
        assert result.written and not result.returned
    async with sessions() as db:
        row = await db.get(PrintQueueItem, assignment.job)
        auto = await db.get(AutoQueueItem, assignment.auto)
        archive = await db.get(PrintArchive, archive_id)
        assert (row.status, row.returned_receipt_identity) == ("failed", None)
        assert row.waiting_reason == "unsent_return_refused:return_unavailable"
        assert (auto.status, auto.unsent_return_count, auto.assigned_to_item_id) == ("assigned", 0, row.id)
        assert (archive.status, archive.failure_reason) == ("failed", DISPATCH_UPLOAD_FAILED)
        assert not await db.scalar(select(func.count(AutoQueueUnsentReturn.id)))


async def test_real_runner_close_rollback_still_returns_the_assignment(assignment, sessions, monkeypatch):
    from backend.app.services import background_dispatch as bd

    dispatch_mocks(monkeypatch, sessions, upload=AsyncMock(return_value=False))
    scheduler, spawned = scheduler_with_captured_dispatch(monkeypatch)
    cleanup = bd.BackgroundDispatchService._mark_dispatch_archive_terminal
    observed = set()

    @asynccontextmanager
    async def broken_cleanup_session():
        async with sessions() as db:

            def abort(_session):
                raise RuntimeError("runner close failed")

            event.listen(db.sync_session, "before_commit", abort, once=True)
            yield db

    async def commit_failure(archive_id, *args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(bd, "async_session", broken_cleanup_session)
            result = await cleanup(archive_id, *args, **kwargs)
        async with sessions() as db:
            archive = await db.get(PrintArchive, archive_id)
            observed.add((result, archive.status, archive.dispatch_phase))
        return result

    monkeypatch.setattr(bd.BackgroundDispatchService, "_mark_dispatch_archive_terminal", staticmethod(commit_failure))
    assert await scheduler.check_queue()
    assert await drain(spawned) == 1
    assert observed == {("failed", "printing", "prepared")}
    await assert_returned(sessions, assignment, DISPATCH_UPLOAD_FAILED, archive=True)


async def test_preclaim_verdict_does_not_end_the_job_after_another_claim(assignment, sessions, monkeypatch):
    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)
    async with sessions() as db:
        row = await db.get(PrintQueueItem, assignment.job)
        await db.commit()
        async with sessions() as other:
            queue = await other.get(PrinterQueue, assignment.queue)
            queue.status = "printing"
            # The foreign claim uses the header while the loaded row stays pending.
            queue.current_item_id = None
            await other.commit()
        outcome = await scheduler._fail_item(
            db, row, "Not connected", DISPATCH_PRINTER_UNAVAILABLE, storage_epoch=queue_sources.current_epoch()
        )
        assert not outcome.written
    async with sessions() as db:
        row = await db.get(PrintQueueItem, assignment.job)
        assert (row.status, row.returned_receipt_identity) == ("pending", None)
        assert not await db.scalar(select(func.count(AutoQueueUnsentReturn.id)))


async def test_the_retired_row_api_refuses_repeat_and_delete_keeps_the_pool_job(
    assignment, sessions, committing_client, monkeypatch
):
    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)
    async with sessions() as db:
        row = await db.get(PrintQueueItem, assignment.job)
        assert (
            await scheduler._fail_item(
                db,
                row,
                "Printer unavailable",
                DISPATCH_PRINTER_UNAVAILABLE,
                storage_epoch=queue_sources.current_epoch(),
            )
        ).returned
    url = f"/api/v1/queue/{assignment.job}"
    response = await committing_client.get(url)
    assert response.status_code == 200, response.text
    assert (
        response.json()["return_kind"],
        response.json()["return_ordinal"],
        response.json()["returned_auto_item_id"],
    ) == (
        "unsent",
        1,
        assignment.auto,
    )
    for endpoint in (f"{url}/retry", f"{url}/clone", "/api/v1/queue/bulk-retry"):
        response = await committing_client.post(
            endpoint, json={"queue_id": assignment.queue, "item_ids": [assignment.job]}
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "already_returned_to_auto_queue"
    result = await committing_client.delete(url)
    assert result.status_code == 200, result.text
    async with sessions() as db:
        assert await db.get(PrintQueueItem, assignment.job) is None
        auto = await db.get(AutoQueueItem, assignment.auto)
        assert (auto.status, auto.unsent_return_count) == ("pending", 1)
        assert await db.get(AutoQueueUnsentReturn, auto.unsent_return_proof["receipt_identity"])


@pytest.mark.parametrize(("observed", "portable"), [(True, True), (False, True), (True, False)])
async def test_unsent_failure_reapplies_hold_after_resume_beside_an_external_run(
    assignment, sessions, committing_client, monkeypatch, observed, portable
):
    from backend.app.services.filament_intake import routing_detail
    from backend.app.services.print_claims import observe_claim
    from backend.app.services.queue_counters import set_queue_idle
    from backend.app.services.queue_ops import claim_lanes

    scheduler, _ = scheduler_with_captured_dispatch(monkeypatch)
    token, (archive_id, identity) = await prepare_claim(sessions, assignment, archive_phase="prepared")
    async with sessions() as db:
        archive = PrintArchive(
            printer_id=assignment.printer, filename="external.3mf", file_path="", file_size=0, status="printing"
        )
        db.add(archive)
        await db.commit()
        external_archive_id = archive.id
    async with sessions() as db, claim_lanes(db, [assignment.queue], archive_id=external_archive_id):
        if observed:
            claim = await observe_claim(db, assignment.printer, archive_id=external_archive_id)
            assert claim.state == "created"
            external = claim.item_id
        else:
            row = PrintQueueItem(
                queue_id=assignment.queue, origin="external", status="printing", archive_id=external_archive_id
            )
            db.add(row)
            await db.flush()
            external = row.id
            (await db.get(PrinterQueue, assignment.queue)).current_item_id = external
        await db.commit()
    response = await committing_client.patch(f"/api/v1/queues/{assignment.queue}", json={"is_paused": False})
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["is_paused"]) == ("printing", False)

    async with sessions() as db:
        if not portable:
            (await db.get(PrintQueueItem, assignment.job)).filament_routing = None
            await db.commit()
        outcome = await scheduler._fail_attempt(
            db,
            item_id=assignment.job,
            queue_id=assignment.queue,
            claim_started_at=token,
            message="Upload refused",
            code=DISPATCH_UPLOAD_FAILED,
            sent=False,
            execution_archive_id=archive_id,
            execution_archive_identity=identity,
            unsent_attempt=UnsentAttempt(queue_sources.current_epoch(), archive_id, identity),
        )
        assert outcome.written and outcome.returned is portable
    async with sessions() as db, claim_lanes(db, [assignment.queue], archive_id=external_archive_id):
        header = await db.get(PrinterQueue, assignment.queue)
        assert (header.status, header.current_item_id, header.is_paused) == ("printing", external, True)
        (await db.get(PrintQueueItem, external)).status = "completed"
        assert await set_queue_idle(db, assignment.queue, expected_item_id=external)
        await db.commit()
    await assignment.router.tick()
    assert not await scheduler.check_queue()
    async with sessions() as db:
        header = await db.get(PrinterQueue, assignment.queue)
        auto = await db.get(AutoQueueItem, assignment.auto)
        assert (header.status, header.current_item_id, header.is_paused) == ("idle", None, True)
        assert (auto.status, auto.unsent_return_count) == (("pending", 1) if portable else ("assigned", 0))
        if portable:
            name = (await db.get(Printer, assignment.printer)).name
            assert auto.waiting_reason == f"{name}: {routing_detail('queue_halted')['message']}"
    if not portable:
        return
    response = await committing_client.patch(f"/api/v1/queues/{assignment.queue}", json={"is_paused": False})
    assert response.status_code == 200, response.text
    await assignment.router.tick()
    async with sessions() as db:
        auto = await db.get(AutoQueueItem, assignment.auto)
        new = await db.get(PrintQueueItem, auto.assigned_to_item_id)
        assert (new.queue_id, auto.status, auto.unsent_return_count) == (assignment.queue, "assigned", 1)
