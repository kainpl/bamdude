"""Return one AutoQueue assignment inside its own proved failure transaction (G03 B2)."""

import logging
import uuid
from dataclasses import dataclass
from functools import wraps

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.core.database import LOCK_NOT_AVAILABLE, sqlstate
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueUnsentReturn
from backend.app.models.printer import Printer
from backend.app.services import order_filing, order_resources, queue_sources, swap_compatibility
from backend.app.services.auto_queue_placement_basis import placement_basis
from backend.app.services.filament_deferred import swap_start_may_have_run
from backend.app.services.filament_policy import queue_policy
from backend.app.services.print_execution import NONCURRENT
from backend.app.utils.failure_reasons import DISPATCH_PRINTER_UNAVAILABLE, DISPATCH_UPLOAD_FAILED

logger = logging.getLogger(__name__)
MAX_UNSENT_RETURNS = 2
REFUSAL_PREFIX = "unsent_return_refused:"
RETURNABLE_FAILURES = frozenset({DISPATCH_PRINTER_UNAVAILABLE, DISPATCH_UPLOAD_FAILED})


class ReturnOrderBusy(Exception):
    def __init__(self, project_id: int | None, line_id: int | None):
        self.project_id = project_id
        self.line_id = line_id


def retry_busy_return_order(write_failure):
    """Release every guard before waiting for the order alone, then rerun the addressed verdict."""

    @wraps(write_failure)
    async def retry(scheduler, db, *args, **kwargs):
        while True:
            try:
                return await write_failure(scheduler, db, *args, **kwargs)
            except ReturnOrderBusy as exc:
                if db.in_transaction():
                    await db.rollback()
                # V3's caller keeps the rows loaded for its pass in db.
                async with async_sessionmaker(db.bind)() as waiter:
                    await order_resources.policy_for(waiter, exc.project_id, project_line_id=exc.line_id, lock=True)

    return retry


@dataclass(frozen=True)
class UnsentAttempt:
    storage_epoch: int
    archive_id: int | None = None
    archive_identity: str | None = None
    swap_start_effect: str | None = None

    @classmethod
    def from_runner(cls, outcome, storage_epoch: int | None):
        if (
            storage_epoch is None
            or outcome.get("failure_code") not in RETURNABLE_FAILURES
            or any(outcome.get(flag) for flag in ("success", "uncertain", "deferred", "cancelled"))
            or "send_fact" not in outcome
            or outcome["send_fact"] not in (None, "refused")
            or not {"execution_archive_id", "execution_archive_identity", "swap_start_effect"} <= outcome.keys()
        ):
            return None
        archive_id, identity = outcome["execution_archive_id"], outcome["execution_archive_identity"]
        effect = outcome["swap_start_effect"]
        if (archive_id is None and identity is not None) or (
            archive_id is not None and (type(archive_id) is not int or not isinstance(identity, str) or not identity)
        ):
            return None
        if effect not in (None, "not_called", "fired", "possibly_fired"):
            return None
        return cls(storage_epoch, archive_id, identity, effect)


def has_auto_ancestor(job) -> bool:
    return (
        job.origin == "queue"
        and not job.is_calibration
        and job.calibration_session_id is None
        and job.source_auto_item_id is not None
        and bool(job.source_auto_identity)
    )


async def lock_order_for_return(db, project_id, line_id) -> str | None:
    """Probe under the lane before the execution/job rows. An order writer may already need this archive or lane."""
    try:
        async with db.begin_nested():
            await order_resources.policy_for(db, project_id, project_line_id=line_id, lock=True, nowait=True)
            await order_filing.resolve_link(db, project_id, line_id)
    except HTTPException:
        return "order_changed"
    except Exception as exc:
        if sqlstate(exc) == LOCK_NOT_AVAILABLE:
            raise ReturnOrderBusy(project_id, line_id) from exc
        logger.warning("Could not lock order for an unsent return: %s", type(exc).__name__)
        return "return_unavailable"
    return None


async def _build_return(db, job, proof, *, code, message, at) -> str | None:
    if queue_sources.current_epoch() != proof.storage_epoch:
        return "storage_changed"
    archive = None
    if proof.archive_id is not None:
        archive = await db.get(PrintArchive, proof.archive_id, populate_existing=True)
        if (
            archive is None
            or archive.recovery_identity != proof.archive_identity
            or archive.queue_item_identity != job.recovery_identity
            or archive.source_auto_identity != job.source_auto_identity
            or archive.deleted_at is not None
            or not isinstance(archive.extra_data, (dict, type(None)))
            or NONCURRENT in (archive.extra_data or {})
            or archive.dispatch_phase != "prepared"
            or archive.status != "failed"
            or archive.failure_reason != code
        ):
            return "archive_changed"
    if await swap_start_may_have_run(db, proof.archive_id, proof.swap_start_effect):
        return "swap_start_possible"
    policy = queue_policy(job)
    if policy.mode != "auto" or policy.review_required or job.nozzle_rack_choice is not None:
        return "policy_changed"
    if not job.placement_basis or job.placement_basis != placement_basis(job):
        return "placement_changed"
    auto = await db.get(
        AutoQueueItem, job.source_auto_item_id, with_for_update={"key_share": True}, populate_existing=True
    )
    if (
        auto is None
        or auto.recovery_identity != job.source_auto_identity
        or auto.status != "assigned"
        or auto.cancelled_at is not None
        or auto.assigned_to_item_id != job.id
        or job.returned_receipt_identity is not None
        or auto.created_by_id != job.created_by_id
        or (auto.project_id, auto.project_line_id) != (job.project_id, job.project_line_id)
    ):
        return "assignment_changed"
    if auto.queue_source_id != job.queue_source_id or not swap_compatibility.same_snapshot_for_return(
        auto.source_snapshot, job.source_snapshot
    ):
        return "source_changed"
    if auto.unsent_return_count >= MAX_UNSENT_RETURNS:
        return "unsent_return_limit"
    printer = await db.get(Printer, job.queue_id)
    if printer is None:
        return "printer_changed"
    if queue_sources.current_epoch() != proof.storage_epoch:
        return "storage_changed"

    receipt = AutoQueueUnsentReturn(
        id=str(uuid.uuid4()),
        old_job_id=job.id,
        old_job_identity=job.recovery_identity,
        old_queue_id=job.queue_id,
        execution_archive_id=proof.archive_id,
        execution_archive_identity=proof.archive_identity,
        auto_item_id=auto.id,
        auto_identity=auto.recovery_identity,
        printer_id=printer.id,
        printer_identity=printer.connector_identity,
        failure_code=code,
        ordinal=auto.unsent_return_count + 1,
        returned_at=at,
    )
    db.add(receipt)
    job.returned_receipt_identity = receipt.id
    job.source_auto_item_id = None
    auto.status = "pending"
    auto.assigned_to_item_id = None
    auto.assigned_at = None
    auto.waiting_reason = message
    auto.unsent_return_count = receipt.ordinal
    auto.unsent_return_proof = {
        "version": 1,
        "receipt_identity": receipt.id,
        "old_job_identity": job.recovery_identity,
        "execution_archive_identity": proof.archive_identity,
        "printer_identity": printer.connector_identity,
        "failure_code": code,
        "returned_at": at.isoformat(),
    }
    return None


async def return_unsent(db, job, proof, *, code, message, at, order_refusal) -> bool:
    """The caller owns the addressed failure. A savepoint can abandon just the return."""
    refusal = order_refusal
    if refusal is None:
        try:
            async with db.begin_nested():
                refusal = await _build_return(db, job, proof, code=code, message=message, at=at)
        except Exception as exc:
            logger.warning("Could not return unsent AutoQueue job: %s", type(exc).__name__)
            refusal = "return_unavailable"
            # A rollback can expire a job changed in the savepoint. The waiting-reason validator reads its old value.
            await db.refresh(job, ["waiting_reason"])
    if refusal is None:
        return True
    job.waiting_reason = REFUSAL_PREFIX + refusal
    return False
