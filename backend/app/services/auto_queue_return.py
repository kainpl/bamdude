"""Addressed settlement facts and the single failed-return transaction.

Only the live completion's successful terminal commit can supply a candidate.
Neither a timestamp scan, reconciliation nor a missing memory scope creates one.
File verification is outside the storage/archive/lane writer; final admission
rechecks the frozen logical job and all identities on its own connection.
"""

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.auto_queue_return import AutoQueueReturn
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import STATE_READY, QueueSource
from backend.app.models.settings import Settings
from backend.app.models.user import User
from backend.app.services import order_filing, order_resources, queue_sources, swap_terminal
from backend.app.services.filament_intake import item_descriptor
from backend.app.services.filament_policy import auto_policy, queue_policy
from backend.app.services.print_execution import STALE_CLAIM_CODES
from backend.app.services.print_run_binding import PrintEffectToken, print_effect_is_current
from backend.app.services.queue_counters import update_queue_counters
from backend.app.services.queue_ops import claim_lanes

SETTING_KEY = "auto_queue_return_failed"
MAX_RETURNS = 2
COOLDOWN_SECONDS = 120
_AUTO_FIELDS = (
    "queue_source_id",
    "source_snapshot",
    "created_by_id",
    "project_id",
    "project_line_id",
    "batch_id",
    "position",
    "target_model",
    "target_location_id",
    "plate_id",
    "print_time_seconds",
    "required_filament_types",
    "feed_policy",
    "force_color_match",
    "allow_base_material_match",
    "filament_overrides",
    "order_print_options",
    "use_ams",
    "bed_levelling",
    "flow_cali",
    "layer_inspect",
    "timelapse",
    "timelapse_storage",
    "mesh_mode_fast_check",
    "gcode_injection",
    "execute_swap_macros",
    "swap_macro_events",
    "selected_macro_ids",
    "nozzle_mapping",
    "manual_start",
    "scheduled_time",
    "auto_off_after",
    "require_previous_success",
    "auto_eject",
    "auto_eject_settings",
    "been_jumped",
    "rebalanced_at",
    "rebalanced_from_model",
)


class ReturnRefused(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def utc(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _canonical(value):
    def encode(item):
        if isinstance(item, datetime):
            return utc(item).isoformat()
        raise TypeError("Unsupported return basis")

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=encode,
    )


def _basis(auto, job, source):
    return _canonical(
        {
            "version": 1,
            "auto": {field: getattr(auto, field) for field in _AUTO_FIELDS},
            "job": {
                field: getattr(job, field)
                for field in (
                    "queue_source_id",
                    "source_snapshot",
                    "created_by_id",
                    "project_id",
                    "project_line_id",
                    "plate_id",
                    "filament_routing",
                    "nozzle_rack_choice",
                    "started_at",
                    "dispatch_attempts",
                )
            },
            "source": {"id": source.id, "sha256": source.sha256, "size_bytes": source.size_bytes},
        }
    )


@dataclass(frozen=True)
class ReturnCandidate:
    job_id: int
    job_identity: str
    archive_id: int
    archive_identity: str
    queue_id: int
    auto_id: int
    auto_identity: str
    printer_id: int
    printer_identity: str
    owner_id: int
    owner_identity: str
    completed_at: datetime
    basis_json: str
    epoch: int
    effect_token: PrintEffectToken | None


def _chain(auto, job, archive):
    return bool(
        auto is not None
        and auto.recovery_identity
        and auto.status == "assigned"
        and auto.cancelled_at is None
        and auto.assigned_to_item_id == job.id
        and job.source_auto_item_id == auto.id
        and job.source_auto_identity == auto.recovery_identity
        and archive.queue_item_identity == job.recovery_identity
        and archive.source_auto_identity == auto.recovery_identity
        and auto.created_by_id == job.created_by_id
        and job.created_by_id is not None
        and auto.queue_source_id == job.queue_source_id
        and auto.source_snapshot == job.source_snapshot
        and isinstance(job.source_snapshot, dict)
        and type(job.source_snapshot.get("version")) is int
        and job.source_snapshot.get("version") == 1
    )


async def capture_candidate(db, job, archive, *, epoch, effect_token):
    """Freeze scalars only after the caller's addressed terminal commit succeeds."""
    if (
        job.status != "failed"
        or archive.status != "failed"
        or job.origin != "queue"
        or job.is_calibration
        or job.calibration_session_id is not None
        or not job.started_at
        or not job.dispatch_attempts
        or not job.recovery_identity
        or not archive.recovery_identity
        or job.archive_id != archive.id
        or not job.source_auto_item_id
        or not job.queue_source_id
        or not job.completed_at
        or job.returned_receipt_identity is not None
        or job.created_by_id is None
        or job.error_code in STALE_CLAIM_CODES
        or not isinstance(archive.extra_data, (dict, type(None)))
        or "noncurrent_execution" in (archive.extra_data or {})
    ):
        return None
    if await db.scalar(select(Settings.value).where(Settings.key == SETTING_KEY)) != "true":
        return None
    auto = await db.get(AutoQueueItem, job.source_auto_item_id)
    if not _chain(auto, job, archive):
        return None
    owner = await db.get(User, job.created_by_id)
    printer = await db.get(Printer, archive.printer_id)
    source = await db.get(QueueSource, job.queue_source_id)
    if (
        owner is None
        or not owner.is_active
        or not owner.account_identity
        or printer is None
        or printer.archived
        or not printer.connector_identity
        or source is None
        or source.state != STATE_READY
    ):
        return None
    try:
        basis = _basis(auto, job, source)
    except (TypeError, ValueError):
        return None
    return ReturnCandidate(
        job.id,
        job.recovery_identity,
        archive.id,
        archive.recovery_identity,
        job.queue_id,
        auto.id,
        auto.recovery_identity,
        printer.id,
        printer.connector_identity,
        owner.id,
        owner.account_identity,
        utc(job.completed_at),
        basis,
        epoch,
        effect_token,
    )


def _verify_blob(path, expected_sha, expected_size):
    """Owned by the caller task until this thread actually exits, including cancellation."""
    try:
        deadline = time.monotonic() + 10
        if path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                if time.monotonic() > deadline:
                    return False
                digest.update(chunk)
        return digest.hexdigest() == expected_sha
    except OSError:
        return False


async def preflight(candidate):
    """Read-only preparation ends before the final writer opens its connection."""
    basis = json.loads(candidate.basis_json)
    async with database.async_session() as db:
        job = await db.get(PrintQueueItem, candidate.job_id)
        auto = await db.get(AutoQueueItem, candidate.auto_id)
        if (
            job is None
            or job.recovery_identity != candidate.job_identity
            or job.status != "failed"
            or auto is None
            or auto.recovery_identity != candidate.auto_identity
        ):
            raise ReturnRefused("attempt_changed")
        descriptor = await item_descriptor(db, job, fresh=True)
        if descriptor is None or descriptor.sha256 != basis["source"]["sha256"]:
            raise ReturnRefused("source_unavailable")
        if descriptor.size_bytes != basis["source"]["size_bytes"]:
            raise ReturnRefused("source_changed")
        if auto_policy(auto).review_required or queue_policy(job).review_required or queue_policy(job).mode != "auto":
            raise ReturnRefused("policy_not_portable")
        if job.nozzle_rack_choice is not None:
            raise ReturnRefused("policy_not_portable")
        path, sha, size = descriptor.path, descriptor.sha256, descriptor.size_bytes
    if not await asyncio.to_thread(_verify_blob, path, sha, size):
        raise ReturnRefused("source_unavailable")


async def _owner(db, candidate):
    # NOWAIT avoids introducing a principal -> domain row deadlock with deletion.
    # Group/account mutations already lock their member account rows first.
    owner = await db.scalar(
        select(User)
        .options(selectinload(User.groups))
        .where(User.id == candidate.owner_id, User.account_identity == candidate.owner_identity)
        .with_for_update(read=True, nowait=True)
        .execution_options(populate_existing=True)
    )
    if (
        owner is None
        or not owner.is_active
        or not owner.has_permission("queue:create")
        or not any(owner.has_permission(p) for p in ("queue:read_own", "queue:read_all", "queue:read"))
    ):
        raise ReturnRefused("owner_unavailable")
    return owner


async def _locked_rows(db, candidate):
    from backend.app.services.farm_startup_guard import farm_startup_guard

    if not farm_startup_guard.allowed():
        raise ReturnRefused("farm_held")
    if queue_sources.current_epoch() != candidate.epoch:
        raise ReturnRefused("storage_changed")
    enabled = await db.scalar(select(Settings.value).where(Settings.key == SETTING_KEY).with_for_update(read=True))
    if enabled != "true":
        raise ReturnRefused("disabled")
    await _owner(db, candidate)
    basis = json.loads(candidate.basis_json)
    project = basis["auto"]["project_id"]
    line = basis["auto"]["project_line_id"]
    await order_resources.policy_for(db, project, project_line_id=line, lock=True)
    try:
        await order_filing.resolve_link(db, project, line)
    except HTTPException as exc:
        raise ReturnRefused("order_changed") from exc
    archive = await db.get(PrintArchive, candidate.archive_id, with_for_update=True, populate_existing=True)
    job = await db.get(PrintQueueItem, candidate.job_id, with_for_update=True, populate_existing=True)
    auto = await db.get(AutoQueueItem, candidate.auto_id, with_for_update=True, populate_existing=True)
    queue = await db.get(PrinterQueue, candidate.queue_id, populate_existing=True)
    printer = await db.get(Printer, candidate.printer_id, populate_existing=True)
    if (
        archive is None
        or archive.recovery_identity != candidate.archive_identity
        or archive.status != "failed"
        or archive.deleted_at is not None
        or archive.printer_id != candidate.printer_id
        or job is None
        or job.recovery_identity != candidate.job_identity
        or job.status != "failed"
        or job.archive_id != archive.id
        or job.queue_id != candidate.queue_id
        or job.returned_receipt_identity is not None
        or job.error_code in STALE_CLAIM_CODES
        or not isinstance(archive.extra_data, (dict, type(None)))
        or "noncurrent_execution" in (archive.extra_data or {})
        or not _chain(auto, job, archive)
        or auto.recovery_identity != candidate.auto_identity
        or job.created_by_id != candidate.owner_id
        or auto.created_by_id != candidate.owner_id
        or queue is None
        or queue.printer_id != candidate.printer_id
        or queue.current_item_id not in (None, job.id)
        or printer is None
        or printer.archived
        or printer.connector_identity != candidate.printer_identity
        or job.completed_at is None
        or archive.completed_at is None
        or utc(job.completed_at) != candidate.completed_at
        or utc(archive.completed_at) != candidate.completed_at
    ):
        raise ReturnRefused("attempt_changed")
    source = await db.get(QueueSource, job.queue_source_id, populate_existing=True)
    if source is None or source.state != STATE_READY or _basis(auto, job, source) != candidate.basis_json:
        raise ReturnRefused("source_or_policy_changed")
    return auto, job, archive, queue, printer


async def publish_ready(candidate, *, manager):
    """Called only from the normal live-completion tail, inside the return runtime owner."""
    await preflight(candidate)
    async with (
        queue_sources.storage_mutation(),
        database.async_session() as db,
        claim_lanes(db, [candidate.queue_id], archive_id=candidate.archive_id),
    ):
        auto, job, archive, queue, printer = await _locked_rows(db, candidate)
        if candidate.effect_token is None or not print_effect_is_current(manager, candidate.effect_token):
            raise ReturnRefused("execution_changed")
        old = await db.scalar(
            select(AutoQueueReturn).where(
                AutoQueueReturn.old_job_identity == candidate.job_identity,
                AutoQueueReturn.execution_archive_identity == candidate.archive_identity,
            )
        )
        digest = hashlib.sha256(candidate.basis_json.encode("utf-8")).hexdigest()
        if old is not None:
            if old.auto_identity != candidate.auto_identity or old.source_basis_digest != digest:
                raise ReturnRefused("settlement_changed")
            return old.id
        receipt = AutoQueueReturn(
            id=str(uuid.uuid4()),
            old_job_id=job.id,
            old_job_identity=job.recovery_identity,
            old_queue_id=queue.id,
            execution_archive_id=archive.id,
            execution_archive_identity=archive.recovery_identity,
            auto_item_id=auto.id,
            auto_identity=auto.recovery_identity,
            printer_id=printer.id,
            printer_identity=printer.connector_identity,
            owner_identity=candidate.owner_identity,
            source_basis=json.loads(candidate.basis_json),
            source_basis_digest=digest,
            dispatch_evidence={
                "started_at": utc(job.started_at).isoformat(),
                "dispatch_attempts": job.dispatch_attempts,
            },
            completed_at=candidate.completed_at,
            ready_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(receipt)
        await db.commit()
        return receipt.id


@dataclass(frozen=True)
class ReturnResult:
    receipt_id: str
    auto_id: int
    ordinal: int
    returned_at: datetime


def _receipt_candidate(receipt):
    """Only durable ready receipts survive restart; never derive one from failed rows."""
    try:
        basis = _canonical(receipt.source_basis)
        if hashlib.sha256(basis.encode("utf-8")).hexdigest() != receipt.source_basis_digest:
            raise ReturnRefused("settlement_changed")
        return ReturnCandidate(
            receipt.old_job_id,
            receipt.old_job_identity,
            receipt.execution_archive_id,
            receipt.execution_archive_identity,
            receipt.old_queue_id,
            receipt.auto_item_id,
            receipt.auto_identity,
            receipt.printer_id,
            receipt.printer_identity,
            receipt.source_basis["auto"]["created_by_id"],
            receipt.owner_identity,
            utc(receipt.completed_at),
            basis,
            queue_sources.current_epoch(),
            None,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ReturnRefused("settlement_changed") from exc


def _returned(receipt):
    if receipt.returned_at is not None and receipt.ordinal is not None:
        return ReturnResult(receipt.id, receipt.auto_item_id, receipt.ordinal, utc(receipt.returned_at))
    return None


async def return_ready(receipt_id, *, now=None):
    """The only reset writer. Caller must own this entire task through actual SQL/I/O exit."""
    async with database.async_session() as db:
        receipt = await db.get(AutoQueueReturn, receipt_id)
        if receipt is None:
            raise ReturnRefused("settlement_missing")
        candidate = _receipt_candidate(receipt)
        previous = _returned(receipt)
        if previous is not None:
            return previous
    await preflight(candidate)
    commit_started = False
    try:
        async with (
            queue_sources.storage_mutation(),
            database.async_session() as db,
            claim_lanes(db, [candidate.queue_id], archive_id=candidate.archive_id),
        ):
            # The lane guards serialize a duplicate before we lock domain rows.
            receipt = await db.get(AutoQueueReturn, receipt_id, populate_existing=True)
            if receipt is None or _receipt_candidate(receipt) != candidate:
                raise ReturnRefused("settlement_changed")
            previous = _returned(receipt)
            if previous is not None:
                return previous
            auto, job, archive, queue, printer = await _locked_rows(db, candidate)
            receipt = await db.get(AutoQueueReturn, receipt_id, with_for_update=True, populate_existing=True)
            if receipt is None or _receipt_candidate(receipt) != candidate:
                raise ReturnRefused("settlement_changed")
            at = utc(now) if now is not None else datetime.now(timezone.utc).replace(tzinfo=None)
            if auto.failed_return_count >= MAX_RETURNS:
                raise ReturnRefused("return_limit")
            proof = auto.failed_return_proof
            if auto.failed_return_count == 0:
                if proof is not None:
                    raise ReturnRefused("history_changed")
            else:
                if (
                    not isinstance(proof, dict)
                    or type(proof.get("version")) is not int
                    or proof["version"] != 1
                    or not isinstance(proof.get("receipt_identity"), str)
                ):
                    raise ReturnRefused("history_changed")
                prior = await db.get(AutoQueueReturn, proof["receipt_identity"])
                if (
                    prior is None
                    or prior.auto_identity != auto.recovery_identity
                    or prior.ordinal != auto.failed_return_count
                    or prior.returned_at is None
                    or prior.old_job_identity != proof.get("old_job_identity")
                    or prior.execution_archive_identity != proof.get("execution_archive_identity")
                    or prior.printer_identity != proof.get("last_failed_printer_identity")
                ):
                    raise ReturnRefused("history_changed")
            if at < candidate.completed_at + timedelta(seconds=COOLDOWN_SECONDS) or at < utc(receipt.ready_at):
                raise ReturnRefused("cooldown")
            stage = swap_terminal.claim_stage(archive.extra_data)
            if stage not in {None, *swap_terminal.ANSWERED, swap_terminal.SETTLED_BY_OPERATOR} or swap_terminal.sending(
                archive.id
            ):
                raise ReturnRefused("physical_claim_unsettled")
            if queue.status == "printing" or (
                printer.awaiting_plate_clear and printer.awaiting_plate_clear_archive_id != archive.id
            ):
                raise ReturnRefused("physical_owner_changed")
            later = await db.scalar(
                select(PrintArchive.id)
                .where(
                    PrintArchive.printer_id == printer.id,
                    PrintArchive.id != archive.id,
                    PrintArchive.started_at >= candidate.completed_at,
                )
                .limit(1)
            )
            if later is not None:
                raise ReturnRefused("execution_changed")
            ordinal = auto.failed_return_count + 1
            # Copy positive dispatch facts before clearing assignment. None of
            # the old printer's error/pause/plate/SWAP facts is reset here.
            auto.failed_return_proof = {
                "version": 1,
                "receipt_identity": receipt.id,
                "old_job_identity": job.recovery_identity,
                "execution_archive_identity": archive.recovery_identity,
                "last_failed_printer_identity": printer.connector_identity,
                "dispatch_evidence": dict(receipt.dispatch_evidence),
            }
            auto.failed_return_count = ordinal
            auto.status = "pending"
            auto.assigned_at = None
            auto.assigned_to_item_id = None
            auto.waiting_reason = None
            receipt.returned_at, receipt.ordinal = at, ordinal
            receipt.last_refusal_code, receipt.last_refusal_at = None, None
            job.returned_receipt_identity = receipt.id
            job.source_auto_item_id = None
            await update_queue_counters(db, queue.id)
            commit_started = True
            await db.commit()
            return _returned(receipt)
    except Exception:
        if not commit_started:
            raise
        # A lost commit acknowledgement is not permission to increment again.
        # Reacquire the same guards on a fresh connection after the old scope
        # has exited. A missing result remains a retryable owned operation.
        async with (
            queue_sources.storage_mutation(),
            database.async_session() as verify,
            claim_lanes(verify, [candidate.queue_id], archive_id=candidate.archive_id),
        ):
            settled = await verify.get(AutoQueueReturn, receipt_id)
            if settled is not None and _receipt_candidate(settled) == candidate:
                result = _returned(settled)
                if result is not None:
                    return result
        raise
