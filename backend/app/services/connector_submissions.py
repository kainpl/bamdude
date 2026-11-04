"""A slicer connector's submissions: immutable manifests, entries published once, receipts that prove it.

OrcaBamDude spec r2 §7-8 with addendum C1-C3. The rules this module keeps:

- **Namespace.** An operation is the client's UUID inside the namespace of this server's connector identity, its
  storage epoch and the API key's immutable ``connector_identity``. The numeric key and user ids are reused on
  SQLite, so they never address one.
- **Manifest.** Immutable and canonical. The same operation with a different body is a conflict, never "last write
  wins".
- **Published.** An entry is published iff its ``published_receipt`` is set. The receipt is written by
  :class:`ConnectorReceipt` inside the writer's own publication transaction, so jobs and receipt commit together or
  not at all. Every other state write is a compare-and-set on ``published_receipt IS NULL``.
- **Lost commit outcome.** A writer that raised may still have committed. Its outcome is read back from the
  database before anything is concluded, and a receipt found there wins.
- **Fresh rights.** Checked inside the publication, on its own session. ``resolve_api_key_authority`` is never used
  there: it writes ``last_used`` through a session of its own, which would wait for SQLite's writer that this
  transaction holds. Library visibility is the key's: ``library:read_all`` through ``authorize_api_key``.
- **Lock order.** storage guard → SQLite writer / PostgreSQL transaction → the printer queue's scope lock → the
  submission row → the entry row. Cancel takes only the submission and entry rows.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import database
from backend.app.core.permissions import Permission
from backend.app.models.api_key import APIKey
from backend.app.models.connector_submission import ConnectorSubmission, ConnectorSubmissionEntry
from backend.app.models.library import LibraryFile
from backend.app.models.printer import Printer
from backend.app.models.queue_source import QueueSource
from backend.app.models.settings import Settings
from backend.app.schemas.connector import (
    EntryError,
    EntryResult,
    SubmissionCreate,
    SubmissionEntry,
    SubmissionResult,
)
from backend.app.services.queue_publication import AlreadyPublished

logger = logging.getLogger(__name__)

SERVER_ID_KEY = "connector_server_id"
STORAGE_EPOCH_KEY = "connector_storage_epoch"
_TRANSIENT_STATUS = frozenset({429, 503, 504, 507})
_RUNNABLE = ("pending", "retryable_failure")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class ConnectorActor:
    """Who submits: scalar identities only, never a request, session or ORM object."""

    key_id: int
    key_identity: str
    owner_id: int | None


class ConnectorRefusal(Exception):
    """A refusal with a stable machine code; ``message`` is an English sentence, translated at the boundary."""

    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.status, self.code, self.message, self.retryable = status, code, message, retryable


class OperationCancelled(Exception):
    """Raised from a claim: the entry was cancelled before it could publish."""


# --------------------------------------------------------------------------- #
# Server identity and the canonical manifest
# --------------------------------------------------------------------------- #


async def server_identity(db: AsyncSession) -> tuple[str, str]:
    """This server's connector identity and storage epoch, created once and stable across a normal restart."""
    values = {}
    for key in (SERVER_ID_KEY, STORAGE_EPOCH_KEY):
        value = await db.scalar(select(Settings.value).where(Settings.key == key))
        if not value:
            try:
                async with db.begin_nested():
                    db.add(Settings(key=key, value=str(uuid.uuid4())))
                await db.commit()
            except IntegrityError:
                await db.rollback()
            value = await db.scalar(select(Settings.value).where(Settings.key == key))
        values[key] = value
    return values[SERVER_ID_KEY], values[STORAGE_EPOCH_KEY]


def _digest(payload) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_manifest(body: SubmissionCreate) -> tuple[dict, str]:
    """Every effective choice materialised, keys sorted, entry order kept; the hash covers all of it."""
    manifest = body.model_dump(mode="json")
    return manifest, _digest(manifest)


def _entry_hash(entry: dict) -> str:
    return _digest(entry)


# --------------------------------------------------------------------------- #
# Fresh authority on the caller's session
# --------------------------------------------------------------------------- #


async def fresh_key(db: AsyncSession, actor: ConnectorActor, permissions: list[str]) -> APIKey:
    """The actor's key read again on THIS session and checked with its owner -- nothing written, nothing cached.

    The integer id is reused on SQLite, so the immutable identity must match too: a new key that took the id is
    not the actor.
    """
    from backend.app.core.auth import authorize_api_key

    key = await db.get(APIKey, actor.key_id, populate_existing=True)
    if key is None or not key.enabled or not key.connector_identity or key.connector_identity != actor.key_identity:
        raise ConnectorRefusal(401, "invalid_key", "The API key is no longer valid.")
    if key.expires_at is not None:
        expires = key.expires_at if key.expires_at.tzinfo else key.expires_at.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            raise ConnectorRefusal(401, "invalid_key", "The API key is no longer valid.")
    try:
        await authorize_api_key(db, key, permissions)
    except HTTPException as exc:
        raise ConnectorRefusal(
            403, "permission_denied", "The API key or its owner lacks a required permission."
        ) from exc
    return key


def key_allows_printer(key: APIKey, printer_id: int) -> bool:
    return key.printer_ids is None or printer_id in key.printer_ids


async def _check_target(db: AsyncSession, key: APIKey, entry: dict) -> None:
    target = entry["target"]
    if target["kind"] == "auto":
        if key.printer_ids is not None:
            raise ConnectorRefusal(
                403,
                "auto_requires_unrestricted_key",
                "An API key restricted to specific printers cannot use the AutoQueue.",
            )
        return
    printer = await db.get(Printer, target["printer_id"], populate_existing=True)
    if (
        printer is None
        or printer.archived
        or not printer.connector_identity
        or printer.connector_identity != target["printer_identity"]
    ):
        raise ConnectorRefusal(404, "printer_not_found", "The target printer no longer exists.")
    if not key_allows_printer(key, printer.id):
        raise ConnectorRefusal(403, "printer_scope", "The API key is not allowed to use this printer.")


async def _check_source(db: AsyncSession, manifest: dict) -> LibraryFile:
    source = (
        await db.execute(
            LibraryFile.active()
            .where(LibraryFile.id == manifest["library_file_id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if source is None:
        raise ConnectorRefusal(404, "source_not_visible", "The library file is not available.")
    if source.file_hash and source.file_hash != manifest["source_sha256"]:
        raise ConnectorRefusal(409, "source_changed", "The library file has changed since it was uploaded.")
    return source


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Registered:
    submission_id: int
    created: bool


async def register(actor: ConnectorActor, body: SubmissionCreate, *, sessions=None) -> Registered:
    """Record the immutable operation and its entries -- or find the one already recorded with this body."""
    factory = sessions or database.async_session
    manifest, manifest_hash = canonical_manifest(body)
    async with factory() as db:
        server_id, epoch = await server_identity(db)
        if body.server_id != server_id or body.storage_epoch != epoch:
            raise ConnectorRefusal(409, "context_changed", "This server is not the one the operation was made for.")
        existing = await _find(db, actor, server_id, epoch, body.operation_id)
        if existing is not None:
            return _same_or_conflict(existing, manifest_hash)
        now = _now()
        submission = ConnectorSubmission(
            operation_id=body.operation_id,
            api_key_identity=actor.key_identity,
            owner_id=actor.owner_id,
            server_id=server_id,
            storage_epoch=epoch,
            manifest=manifest,
            manifest_hash=manifest_hash,
            state="processing",
            cancel_requested=False,
            created_at=now,
            updated_at=now,
        )
        db.add(submission)
        try:
            await db.flush()
            for ordinal, entry in enumerate(manifest["entries"]):
                db.add(
                    ConnectorSubmissionEntry(
                        submission_id=submission.id,
                        entry_id=entry["entry_id"],
                        ordinal=ordinal,
                        intent_hash=_entry_hash(entry),
                        state="pending",
                        attempt_generation=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await db.commit()
        except IntegrityError:
            # A concurrent request recorded the same operation first.
            await db.rollback()
            existing = await _find(db, actor, server_id, epoch, body.operation_id)
            if existing is None:
                raise
            return _same_or_conflict(existing, manifest_hash)
        return Registered(submission.id, True)


async def _find(db, actor: ConnectorActor, server_id: str, epoch: str, operation_id: str):
    return (
        await db.execute(
            select(ConnectorSubmission).where(
                ConnectorSubmission.server_id == server_id,
                ConnectorSubmission.storage_epoch == epoch,
                ConnectorSubmission.api_key_identity == actor.key_identity,
                ConnectorSubmission.operation_id == operation_id,
            )
        )
    ).scalar_one_or_none()


def _same_or_conflict(existing: ConnectorSubmission, manifest_hash: str) -> Registered:
    if existing.manifest_hash != manifest_hash:
        raise ConnectorRefusal(
            409, "operation_conflict", "This operation was already submitted with different content."
        )
    return Registered(existing.id, False)


# --------------------------------------------------------------------------- #
# The receipt the writer records with the jobs
# --------------------------------------------------------------------------- #


class ConnectorReceipt:
    """The :class:`~backend.app.services.queue_publication.PublicationReceipt` of one entry."""

    def __init__(self, actor: ConnectorActor, submission_id: int, entry_pk: int, manifest: dict, entry: dict):
        self.actor, self.submission_id, self.entry_pk = actor, submission_id, entry_pk
        self.manifest, self.entry = manifest, entry

    async def _lock_rows(self, session: AsyncSession) -> tuple[ConnectorSubmission, ConnectorSubmissionEntry]:
        # The parent, then the entry: the order cancellation takes them in too. On SQLite the publication already
        # holds the writer, so FOR UPDATE compiles to nothing and the writer is the lock.
        submission = (
            await session.execute(
                select(ConnectorSubmission)
                .where(ConnectorSubmission.id == self.submission_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        entry = (
            await session.execute(
                select(ConnectorSubmissionEntry)
                .where(ConnectorSubmissionEntry.id == self.entry_pk)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        return submission, entry

    async def claim(self, session: AsyncSession, source: QueueSource) -> None:
        submission, entry = await self._lock_rows(session)
        if entry.published_receipt is not None:
            raise AlreadyPublished(self.entry["entry_id"])
        if entry.state == "cancelled" or submission.cancel_requested:
            raise OperationCancelled(self.entry["entry_id"])
        key = await fresh_key(session, self.actor, [Permission.QUEUE_CREATE.value, Permission.LIBRARY_READ_ALL.value])
        await _check_target(session, key, self.entry)
        if source.sha256 != self.manifest["source_sha256"]:
            raise ConnectorRefusal(409, "source_changed", "The library file has changed since it was uploaded.")

    async def record(self, session: AsyncSession, *, queue_item_ids: list[int], auto_item_ids: list[int]) -> None:
        entry = await session.get(ConnectorSubmissionEntry, self.entry_pk)
        target = self.entry["target"]
        entry.published_receipt = {
            "source_sha256": self.manifest["source_sha256"],
            "library_file_id": self.manifest["library_file_id"],
            "plate_id": self.entry["plate_id"],
            "quantity": self.entry["quantity"],
            "target": {k: target[k] for k in sorted(target)},
            "queue_item_ids": list(queue_item_ids),
            "auto_item_ids": list(auto_item_ids),
            "published_at": _now().isoformat(),
        }
        entry.state = "published"
        entry.error_code = None
        entry.updated_at = _now()
        await session.flush()


# --------------------------------------------------------------------------- #
# Running an operation
# --------------------------------------------------------------------------- #


async def _set_state(factory, entry_pk: int, state: str, error_code: str | None) -> bool:
    """A non-publication state, only while no receipt exists: a published entry never goes back."""
    async with factory() as db:
        result = await db.execute(
            update(ConnectorSubmissionEntry)
            .where(ConnectorSubmissionEntry.id == entry_pk, ConnectorSubmissionEntry.published_receipt.is_(None))
            .values(state=state, error_code=error_code, updated_at=_now())
        )
        await db.commit()
        return result.rowcount == 1


async def _start(factory, entry_pk: int) -> bool:
    async with factory() as db:
        result = await db.execute(
            update(ConnectorSubmissionEntry)
            .where(
                ConnectorSubmissionEntry.id == entry_pk,
                ConnectorSubmissionEntry.published_receipt.is_(None),
                ConnectorSubmissionEntry.state.in_(_RUNNABLE),
            )
            .values(
                state="preparing",
                attempt_generation=ConnectorSubmissionEntry.attempt_generation + 1,
                updated_at=_now(),
            )
        )
        await db.commit()
        return result.rowcount == 1


def _queue_request(manifest: dict, entry: dict):
    from backend.app.schemas.auto_queue import AutoQueueItemCreate
    from backend.app.schemas.print_queue import PrintQueueItemCreate

    filament, options, target = entry["filament"], entry["options"], entry["target"]
    if filament["mode"] == "external_only":
        feed, use_ams = "external_only", False
    elif filament["mode"] == "pinned":
        feed, use_ams = None, True
    else:
        feed, use_ams = filament["feed_policy"], True
    common = {
        "library_file_id": manifest["library_file_id"],
        "plate_id": entry["plate_id"],
        "quantity": entry["quantity"],
        "manual_start": entry["action"] == "queue_manual",
        "bed_levelling": options["bed_levelling"],
        "flow_cali": options["flow_cali"],
        "timelapse": options["timelapse"],
        "layer_inspect": options["layer_inspect"],
        "feed_policy": feed,
        "use_ams": use_ams,
        # No side effect the operator was not shown (spec r2 §9.3).
        "execute_swap_macros": False,
        "auto_eject_enabled": False,
    }
    if target["kind"] == "auto":
        return AutoQueueItemCreate(
            target_model=target["target_model"], target_location_id=target.get("target_location_id"), **common
        )
    return PrintQueueItemCreate(
        queue_id=target["printer_id"],
        enqueue_position="end",
        nozzle_offset_cali=options["nozzle_offset_cali"],
        gcode_injection=False,
        ams_mapping=filament["ams_mapping"] if filament["mode"] == "pinned" else None,
        manual_mapping=filament["mode"] == "pinned",
        **common,
    )


def _refusal_from_http(exc: HTTPException) -> ConnectorRefusal:
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code") or detail.get("error") or "writer_refused")
        message = str(detail.get("message") or "The print could not be queued.")
    else:
        code, message = "writer_refused", str(detail or "The print could not be queued.")
    return ConnectorRefusal(exc.status_code, code, message, retryable=exc.status_code in _TRANSIENT_STATUS)


async def _publish_entry(factory, actor: ConnectorActor, submission_id: int, entry_pk: int, manifest, entry) -> None:
    from backend.app.services.auto_queue_add import add_items_to_auto_queue
    from backend.app.services.queue_add import add_items_to_printer_queue

    request = _queue_request(manifest, entry)
    receipt = ConnectorReceipt(actor, submission_id, entry_pk, manifest, entry)
    async with factory() as db:
        # The same checks the publication repeats under its locks, made once before the capture so a refused
        # entry costs no copy of the file.
        key = await fresh_key(db, actor, [Permission.QUEUE_CREATE.value, Permission.LIBRARY_READ_ALL.value])
        await _check_target(db, key, entry)
        await _check_source(db, manifest)
        await db.commit()
        # ``None`` exactly as the REST queue routes pass it for an API key: the key's library visibility is
        # all-or-none (``library:read_all`` above), never its owner's per-file ownership.
        if entry["target"]["kind"] == "auto":
            await add_items_to_auto_queue(db, request, None, publication_receipt=receipt)
        else:
            await add_items_to_printer_queue(db, request, None, publication_receipt=receipt)


async def _published(factory, entry_pk: int) -> bool | None:
    """Whether a receipt exists for the entry; ``None`` when the database cannot answer."""
    try:
        async with factory() as db:
            receipt = await db.scalar(
                select(ConnectorSubmissionEntry.published_receipt).where(ConnectorSubmissionEntry.id == entry_pk)
            )
            return receipt is not None
    except Exception:  # noqa: BLE001 -- unknown outcome, decided later by a read that works
        logger.warning("Connector entry %s: its outcome could not be read back", entry_pk, exc_info=True)
        return None


async def run(submission_id: int, actor: ConnectorActor, *, sessions=None) -> None:
    """Publish the runnable entries of one operation, in their order. Never re-runs a published entry."""
    factory = sessions or database.async_session
    async with factory() as db:
        submission = await db.get(ConnectorSubmission, submission_id)
        if submission is None or submission.api_key_identity != actor.key_identity:
            return
        manifest = submission.manifest
        rows = (
            await db.execute(
                select(ConnectorSubmissionEntry.id, ConnectorSubmissionEntry.ordinal)
                .where(ConnectorSubmissionEntry.submission_id == submission_id)
                .order_by(ConnectorSubmissionEntry.ordinal)
            )
        ).all()
    for entry_pk, ordinal in rows:
        if not await _start(factory, entry_pk):
            continue
        entry = manifest["entries"][ordinal]
        try:
            await _publish_entry(factory, actor, submission_id, entry_pk, manifest, entry)
        except AlreadyPublished:
            continue
        except OperationCancelled:
            await _set_state(factory, entry_pk, "cancelled", "operation_cancelled")
        except ConnectorRefusal as refusal:
            await _set_state(factory, entry_pk, "retryable_failure" if refusal.retryable else "rejected", refusal.code)
        except HTTPException as exc:
            refusal = _refusal_from_http(exc)
            await _set_state(factory, entry_pk, "retryable_failure" if refusal.retryable else "rejected", refusal.code)
        except BaseException as exc:
            # The writer may have committed before it raised (C1/B1): ask the database, never the exception.
            published = await _published(factory, entry_pk)
            if published is False:
                await _set_state(factory, entry_pk, "retryable_failure", "publication_failed")
            if not isinstance(exc, Exception):
                raise
            logger.warning(
                "Connector entry %s of submission %s failed: %s", entry_pk, submission_id, type(exc).__name__
            )


# --------------------------------------------------------------------------- #
# Reading and cancelling
# --------------------------------------------------------------------------- #


def _entry_result(entry_id: str, row: ConnectorSubmissionEntry | None) -> EntryResult:
    if row is not None and row.published_receipt is not None:
        receipt = row.published_receipt
        return EntryResult(
            entry_id=entry_id,
            state="published",
            queue_item_ids=receipt.get("queue_item_ids") or [],
            auto_item_ids=receipt.get("auto_item_ids") or [],
        )
    if row is None or row.state in ("pending",):
        return EntryResult(entry_id=entry_id, state="pending")
    if row.state == "preparing":
        return EntryResult(entry_id=entry_id, state="processing")
    error = EntryError(
        error=row.error_code or "unknown",
        message=_ENTRY_MESSAGES.get(row.error_code or "", "The entry was not published."),
        retryable=row.state == "retryable_failure",
    )
    return EntryResult(entry_id=entry_id, state=row.state, error=error)


_ENTRY_MESSAGES = {
    "operation_cancelled": "The entry was cancelled before it was published.",
    "printer_not_found": "The target printer no longer exists.",
    "printer_scope": "The API key is not allowed to use this printer.",
    "auto_requires_unrestricted_key": "An API key restricted to specific printers cannot use the AutoQueue.",
    "source_changed": "The library file has changed since it was uploaded.",
    "source_not_visible": "The library file is not available.",
    "permission_denied": "The API key or its owner lacks a required permission.",
    "invalid_key": "The API key is no longer valid.",
    "publication_failed": "The print could not be queued; it can be resumed.",
}


def _operation_state(entries: list[EntryResult]) -> str:
    states = [e.state for e in entries]
    published = sum(s == "published" for s in states)
    if published == len(states):
        return "complete"
    if any(s in ("pending", "processing", "retryable_failure") for s in states):
        return "partial" if published else "processing"
    if published:
        return "partial"
    if all(s == "cancelled" for s in states):
        return "cancelled"
    return "rejected"


async def result(actor: ConnectorActor, operation_id: str, *, sessions=None, replayed: bool = False):
    """The addressed result of this key's own operation, or ``None`` -- a foreign one and a missing one look alike."""
    factory = sessions or database.async_session
    async with factory() as db:
        server_id, epoch = await server_identity(db)
        submission = await _find(db, actor, server_id, epoch, operation_id)
        if submission is None:
            return None
        rows = {
            row.entry_id: row
            for row in (
                await db.execute(
                    select(ConnectorSubmissionEntry).where(ConnectorSubmissionEntry.submission_id == submission.id)
                )
            ).scalars()
        }
        entries = [_entry_result(e["entry_id"], rows.get(e["entry_id"])) for e in submission.manifest["entries"]]
        return SubmissionResult(
            operation_id=operation_id, state=_operation_state(entries), entries=entries, replayed=replayed
        )


async def submission_id_for(actor: ConnectorActor, operation_id: str, *, sessions=None) -> int | None:
    factory = sessions or database.async_session
    async with factory() as db:
        server_id, epoch = await server_identity(db)
        submission = await _find(db, actor, server_id, epoch, operation_id)
        return None if submission is None else submission.id


async def cancel(actor: ConnectorActor, operation_id: str, *, sessions=None) -> bool:
    """Fence the unpublished entries of this key's own operation. Published ones stay as they are."""
    factory = sessions or database.async_session
    async with factory() as db:
        server_id, epoch = await server_identity(db)
        submission = await _find(db, actor, server_id, epoch, operation_id)
        if submission is None:
            return False
        await db.execute(select(ConnectorSubmission).where(ConnectorSubmission.id == submission.id).with_for_update())
        await db.execute(
            select(ConnectorSubmissionEntry.id)
            .where(ConnectorSubmissionEntry.submission_id == submission.id)
            .order_by(ConnectorSubmissionEntry.ordinal)
            .with_for_update()
        )
        await db.execute(
            update(ConnectorSubmission)
            .where(ConnectorSubmission.id == submission.id)
            .values(cancel_requested=True, updated_at=_now())
        )
        await db.execute(
            update(ConnectorSubmissionEntry)
            .where(
                ConnectorSubmissionEntry.submission_id == submission.id,
                ConnectorSubmissionEntry.published_receipt.is_(None),
                ConnectorSubmissionEntry.state.in_(("pending", "preparing", "retryable_failure")),
            )
            .values(state="cancelled", error_code="operation_cancelled", updated_at=_now())
        )
        await db.commit()
        return True


def entry_targets(manifest: dict) -> list[dict]:
    return [SubmissionEntry.model_validate(e).target.model_dump() for e in manifest.get("entries", [])]
