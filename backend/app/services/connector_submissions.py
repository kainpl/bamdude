"""A slicer connector's submissions: immutable manifests, entries published once, receipts that prove it.

OrcaBamDude spec r2 §7-8 with addendum C1-C3. The rules this module keeps:

- **Namespace.** An operation is the client's UUID inside the namespace of this server's connector identity, its
  storage epoch and the API key's immutable ``connector_identity``. The numeric key and user ids are reused on
  SQLite, so they never address one.
- **Manifest.** Immutable and canonical. The same operation with a different body is a conflict, never "last write
  wins".
- **Published.** An entry is published iff its ``published_receipt`` is set. The receipt is written by
  :class:`ConnectorReceipt` inside the writer's own publication transaction, so jobs and receipt commit together or
  not at all. Every other state write is a compare-and-set on ``published_receipt IS NULL``, which is also the
  read-back after a failure whose commit outcome is unknown: a receipt that committed makes the write match nothing.
- **Fresh rights.** Checked inside the publication, on its own session. ``resolve_api_key_authority`` is never used
  there: it writes ``last_used`` through a session of its own, which would wait for SQLite's writer that this
  transaction holds. Library visibility is the key's: ``library:read_all`` through ``authorize_api_key``.
- **Lock order.** storage guard → SQLite writer / PostgreSQL transaction → the printer queue's scope lock → the
  submission row → the entry row. Cancel takes only the submission and entry rows.
- **Messages.** A refusal carries a stable code and an English sentence; the sentence is stored and translated when
  it is answered, never a raw exception text.
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
from backend.app.schemas.connector import EntryError, EntryResult, SubmissionCreate, SubmissionResult
from backend.app.services.queue_publication import AlreadyPublished

logger = logging.getLogger(__name__)

SERVER_ID_KEY = "connector_server_id"
STORAGE_EPOCH_KEY = "connector_storage_epoch"
SUBMIT_PERMISSIONS = (Permission.QUEUE_CREATE.value, Permission.LIBRARY_READ_ALL.value)
_TRANSIENT_STATUS = frozenset({429, 503, 504, 507})
# What a coordinator may start: an entry never attempted, or one whose failure was transient.
NEW_ONLY = ("pending",)
RESUMABLE = ("pending", "retryable_failure")

INVALID_KEY = "The API key is no longer valid."
PERMISSION_DENIED = "The API key or its owner lacks a required permission."
AUTO_RESTRICTED = "An API key restricted to specific printers cannot use the AutoQueue."
PRINTER_GONE = "The target printer no longer exists."
PRINTER_SCOPE = "The API key is not allowed to use this printer."
SOURCE_NOT_VISIBLE = "The library file is not available."
SOURCE_CHANGED = "The library file has changed since it was uploaded."
CONTEXT_CHANGED = "This server is not the one the operation was made for."
OPERATION_CONFLICT = "This operation was already submitted with different content."
CANCELLED = "The entry was cancelled before it was published."
PUBLICATION_FAILED = "The print could not be queued; it can be resumed."
WRITER_REFUSED = "The print could not be queued."
NOT_SLICED = "Not a sliced file. Only G-code, or a 3MF with sliced G-code inside, can be printed."

# Known writer refusals and the protocol code each one is (spec r2 §7.6); anything else keeps its own sentence under
# ``writer_refused``.
_WRITER_CODES = {
    "Printer not found": "printer_not_found",
    "Queue not found": "printer_not_found",
    "Library file not found": "source_not_visible",
    NOT_SLICED: "invalid_file",
}


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

    def detail(self) -> dict:
        return {"error": self.code, "message": self.message}


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


async def read_server_identity(db: AsyncSession) -> tuple[str | None, str | None]:
    """The same two values, read only -- for readers (the scope gate among them) that must not write."""
    rows = dict(
        (
            await db.execute(
                select(Settings.key, Settings.value).where(Settings.key.in_((SERVER_ID_KEY, STORAGE_EPOCH_KEY)))
            )
        ).all()
    )
    return rows.get(SERVER_ID_KEY) or None, rows.get(STORAGE_EPOCH_KEY) or None


async def rotate_storage_epoch(db: AsyncSession) -> str:
    """A new storage epoch, committed: every operation of the old one stops answering (spec r2 §8.4)."""
    epoch = str(uuid.uuid4())
    result = await db.execute(update(Settings).where(Settings.key == STORAGE_EPOCH_KEY).values(value=epoch))
    if result.rowcount == 0:
        db.add(Settings(key=STORAGE_EPOCH_KEY, value=epoch))
    await db.commit()
    return epoch


def _digest(payload) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_manifest(body: SubmissionCreate) -> tuple[dict, str]:
    """Every effective choice materialised, keys sorted, entry order kept; the hash covers all of it."""
    manifest = body.model_dump(mode="json")
    return manifest, _digest(manifest)


def manifest_targets(manifest: dict) -> tuple[set[int], bool]:
    """The printers a stored manifest names and whether any entry goes to the AutoQueue."""
    printers, auto = set(), False
    for entry in manifest.get("entries") or []:
        target = entry.get("target") or {}
        if target.get("kind") == "auto":
            auto = True
        elif isinstance(target.get("printer_id"), int):
            printers.add(target["printer_id"])
    return printers, auto


# --------------------------------------------------------------------------- #
# Fresh authority on the caller's session
# --------------------------------------------------------------------------- #


async def fresh_key(db: AsyncSession, actor: ConnectorActor, permissions) -> APIKey:
    """The actor's key read again on THIS session and checked with its owner -- nothing written, nothing cached.

    The integer id is reused on SQLite, so the immutable identity must match too: a new key that took the id is
    not the actor, and a key without an identity is nobody.
    """
    from backend.app.core.auth import authorize_api_key

    key = await db.get(APIKey, actor.key_id, populate_existing=True)
    if key is None or not key.enabled or not key.connector_identity or key.connector_identity != actor.key_identity:
        raise ConnectorRefusal(401, "invalid_key", INVALID_KEY)
    if key.expires_at is not None:
        expires = key.expires_at if key.expires_at.tzinfo else key.expires_at.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            raise ConnectorRefusal(401, "invalid_key", INVALID_KEY)
    try:
        await authorize_api_key(db, key, list(permissions))
    except HTTPException as exc:
        raise ConnectorRefusal(403, "permission_denied", PERMISSION_DENIED) from exc
    return key


async def _check_target(db: AsyncSession, key: APIKey, target: dict) -> None:
    if target["kind"] == "auto":
        if key.printer_ids is not None:
            raise ConnectorRefusal(403, "auto_requires_unrestricted_key", AUTO_RESTRICTED)
        return
    printer = await db.get(Printer, target["printer_id"], populate_existing=True)
    if (
        printer is None
        or printer.archived
        or not printer.connector_identity
        or printer.connector_identity != target["printer_identity"]
    ):
        raise ConnectorRefusal(404, "printer_not_found", PRINTER_GONE)
    if key.printer_ids is not None and printer.id not in key.printer_ids:
        raise ConnectorRefusal(403, "printer_scope", PRINTER_SCOPE)


async def _check_source(db: AsyncSession, manifest: dict) -> None:
    source = (
        await db.execute(
            LibraryFile.active()
            .where(LibraryFile.id == manifest["library_file_id"])
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if source is None:
        raise ConnectorRefusal(404, "source_not_visible", SOURCE_NOT_VISIBLE)
    if source.file_hash and source.file_hash != manifest["source_sha256"]:
        raise ConnectorRefusal(409, "source_changed", SOURCE_CHANGED)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Registered:
    submission_id: int
    created: bool


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


async def find_own(db: AsyncSession, key_identity: str | None, operation_id: str) -> ConnectorSubmission | None:
    """The operation of THIS key identity in this server's current namespace -- a foreign one is no answer."""
    if not key_identity:
        return None
    server_id, epoch = await read_server_identity(db)
    if server_id is None or epoch is None:
        return None  # nothing was ever submitted in a namespace that does not exist yet
    actor = ConnectorActor(key_id=0, key_identity=key_identity, owner_id=None)
    return await _find(db, actor, server_id, epoch, operation_id)


def _same_or_conflict(existing: ConnectorSubmission, manifest_hash: str) -> Registered:
    if existing.manifest_hash != manifest_hash:
        raise ConnectorRefusal(409, "operation_conflict", OPERATION_CONFLICT)
    return Registered(existing.id, False)


async def register(actor: ConnectorActor, body: SubmissionCreate, *, sessions=None) -> Registered:
    """Record the immutable operation and its entries -- or find the one already recorded with this body."""
    factory = sessions or database.async_session
    manifest, manifest_hash = canonical_manifest(body)
    async with factory() as db:
        server_id, epoch = await server_identity(db)
        if body.server_id != server_id or body.storage_epoch != epoch:
            raise ConnectorRefusal(409, "context_changed", CONTEXT_CHANGED)
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
                        intent_hash=_digest(entry),
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
            raise ConnectorRefusal(409, "operation_cancelled", CANCELLED)
        key = await fresh_key(session, self.actor, SUBMIT_PERMISSIONS)
        await _check_target(session, key, self.entry["target"])
        if source.sha256 != self.manifest["source_sha256"]:
            raise ConnectorRefusal(409, "source_changed", SOURCE_CHANGED)

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
        entry.error_code = entry.error_message = None
        entry.updated_at = _now()
        await session.flush()


# --------------------------------------------------------------------------- #
# Running an operation
# --------------------------------------------------------------------------- #


async def _set_state(factory, entry_pk: int, state: str, refusal: ConnectorRefusal | None) -> bool:
    """A non-publication state, only while no receipt exists: a published entry never goes back."""
    async with factory() as db:
        result = await db.execute(
            update(ConnectorSubmissionEntry)
            .where(ConnectorSubmissionEntry.id == entry_pk, ConnectorSubmissionEntry.published_receipt.is_(None))
            .values(
                state=state,
                error_code=refusal.code if refusal else None,
                error_message=refusal.message[:500] if refusal else None,
                updated_at=_now(),
            )
        )
        await db.commit()
        return result.rowcount == 1


async def _start(factory, entry_pk: int, states) -> bool:
    async with factory() as db:
        result = await db.execute(
            update(ConnectorSubmissionEntry)
            .where(
                ConnectorSubmissionEntry.id == entry_pk,
                ConnectorSubmissionEntry.published_receipt.is_(None),
                ConnectorSubmissionEntry.state.in_(states),
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


def _writer_reason(exc: HTTPException) -> tuple[str, str | None]:
    """A writer's own refusal as a protocol code and its sentence (the writer's, already translatable)."""
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("error") or detail.get("code") or "writer_refused")
        return code, detail.get("message") if isinstance(detail.get("message"), str) else None
    text = detail if isinstance(detail, str) else None
    return _WRITER_CODES.get(text or "", "writer_refused"), text


async def _publish_entry(factory, actor: ConnectorActor, submission_id: int, entry_pk: int, manifest, entry) -> None:
    from backend.app.services.auto_queue_add import add_items_to_auto_queue
    from backend.app.services.queue_add import add_items_to_printer_queue

    request = _queue_request(manifest, entry)
    receipt = ConnectorReceipt(actor, submission_id, entry_pk, manifest, entry)
    async with factory() as db:
        # The same checks the publication repeats under its locks, made once before the capture so a refused
        # entry costs no copy of the file.
        key = await fresh_key(db, actor, SUBMIT_PERMISSIONS)
        await _check_target(db, key, entry["target"])
        await _check_source(db, manifest)
        await db.commit()
        try:
            # ``None`` exactly as the REST queue routes pass it for an API key: the key's library visibility is
            # all-or-none (``library:read_all`` above), never its owner's per-file ownership.
            if entry["target"]["kind"] == "auto":
                await add_items_to_auto_queue(db, request, None, publication_receipt=receipt)
            else:
                await add_items_to_printer_queue(db, request, None, publication_receipt=receipt)
        except (AlreadyPublished, ConnectorRefusal):
            raise
        except HTTPException as exc:
            code, text = _writer_reason(exc)
            retryable = exc.status_code in _TRANSIENT_STATUS
            raise ConnectorRefusal(exc.status_code, code, text or WRITER_REFUSED, retryable=retryable) from exc
        except Exception as exc:
            # The commit may have happened before this raised (addendum C1): the caller's guarded write decides.
            raise ConnectorRefusal(503, "publication_failed", PUBLICATION_FAILED, retryable=True) from exc


async def _announce(factory, entry_pk: int, target: dict) -> None:
    """After a fresh commit only: what the REST printer-queue route announces (``queue_add_effects``). The AutoQueue
    keeps its own semantics -- nothing is announced before an assignment. Never raises."""
    if target["kind"] != "printer":
        return
    from sqlalchemy.orm import selectinload

    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.queue_add_effects import announce_printer_queue_add

    try:
        async with factory() as db:
            receipt = await db.scalar(
                select(ConnectorSubmissionEntry.published_receipt).where(ConnectorSubmissionEntry.id == entry_pk)
            )
            ids = (receipt or {}).get("queue_item_ids") or []
            if not ids:
                return
            item = (
                await db.execute(
                    select(PrintQueueItem)
                    .options(selectinload(PrintQueueItem.archive), selectinload(PrintQueueItem.library_file))
                    .where(PrintQueueItem.id == min(ids))
                )
            ).scalar_one_or_none()
            queue = (
                await db.execute(
                    select(PrinterQueue)
                    .options(selectinload(PrinterQueue.printer))
                    .where(PrinterQueue.id == target["printer_id"])
                )
            ).scalar_one_or_none()
            if item is not None and queue is not None:
                await announce_printer_queue_add(db, item, queue)
    except Exception:  # noqa: BLE001 -- best effort, the publication stands
        logger.warning("Connector entry %s: the queue announcement failed", entry_pk, exc_info=True)


async def run(submission_id: int, actor: ConnectorActor, *, states=RESUMABLE, sessions=None) -> None:
    """Publish the startable entries of one operation, in their order. Never re-runs a published entry."""
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
        if not await _start(factory, entry_pk, states):
            continue
        entry = manifest["entries"][ordinal]
        try:
            await _publish_entry(factory, actor, submission_id, entry_pk, manifest, entry)
            await _announce(factory, entry_pk, entry["target"])
        except AlreadyPublished:
            continue
        except ConnectorRefusal as refusal:
            if refusal.code == "operation_cancelled":
                state = "cancelled"
            else:
                state = "retryable_failure" if refusal.retryable else "rejected"
            try:
                # CAS on an absent receipt: after an unknown commit outcome this IS the read-back.
                await _set_state(factory, entry_pk, state, refusal)
            except Exception:  # noqa: BLE001 -- the database cannot answer: the entry stays unknown (processing)
                logger.warning("Connector entry %s: its outcome could not be recorded", entry_pk, exc_info=True)
            if refusal.code == "publication_failed":
                logger.warning(
                    "Connector entry %s of submission %s failed: %s",
                    entry_pk,
                    submission_id,
                    type(refusal.__cause__).__name__,
                )


async def make_resumable(submission_id: int, *, sessions=None) -> int:
    """Entries a crashed or abandoned coordinator left ``preparing`` become startable again -- only by an explicit
    resume, and only while no receipt exists (spec r2 §8.2). The caller guarantees no coordinator is running."""
    factory = sessions or database.async_session
    async with factory() as db:
        result = await db.execute(
            update(ConnectorSubmissionEntry)
            .where(
                ConnectorSubmissionEntry.submission_id == submission_id,
                ConnectorSubmissionEntry.published_receipt.is_(None),
                ConnectorSubmissionEntry.state == "preparing",
            )
            .values(state="pending", error_code=None, error_message=None, updated_at=_now())
        )
        await db.commit()
        return result.rowcount


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
    if row is None or row.state == "pending":
        return EntryResult(entry_id=entry_id, state="pending")
    if row.state == "preparing":
        return EntryResult(entry_id=entry_id, state="processing")
    error = EntryError(
        error=row.error_code or "publication_failed",
        message=row.error_message or PUBLICATION_FAILED,
        retryable=row.state == "retryable_failure",
    )
    return EntryResult(entry_id=entry_id, state=row.state, error=error)


def operation_state(entries: list[EntryResult]) -> str:
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


async def result_of(submission_id: int, *, replayed: bool = False, sessions=None) -> SubmissionResult:
    factory = sessions or database.async_session
    async with factory() as db:
        submission = await db.get(ConnectorSubmission, submission_id, populate_existing=True)
        rows = {
            row.entry_id: row
            for row in (
                await db.execute(
                    select(ConnectorSubmissionEntry)
                    .where(ConnectorSubmissionEntry.submission_id == submission_id)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }
        entries = [_entry_result(e["entry_id"], rows.get(e["entry_id"])) for e in submission.manifest["entries"]]
        return SubmissionResult(
            operation_id=submission.operation_id, state=operation_state(entries), entries=entries, replayed=replayed
        )


async def result(actor: ConnectorActor, operation_id: str, *, sessions=None, replayed: bool = False):
    """The addressed result of this key's own operation, or ``None`` -- a foreign one and a missing one look alike."""
    factory = sessions or database.async_session
    async with factory() as db:
        submission = await find_own(db, actor.key_identity, operation_id)
    if submission is None:
        return None
    return await result_of(submission.id, replayed=replayed, sessions=factory)


async def submission_id_for(actor: ConnectorActor, operation_id: str, *, sessions=None) -> int | None:
    factory = sessions or database.async_session
    async with factory() as db:
        submission = await find_own(db, actor.key_identity, operation_id)
        return None if submission is None else submission.id


async def cancel(actor: ConnectorActor, operation_id: str, *, sessions=None) -> bool:
    """Fence the unpublished entries of this key's own operation. Published ones stay as they are."""
    factory = sessions or database.async_session
    async with factory() as db:
        submission = await find_own(db, actor.key_identity, operation_id)
        if submission is None:
            return False
        # The parent, then the entries in their order -- the order the publication's claim takes them in.
        await db.execute(
            select(ConnectorSubmission.id).where(ConnectorSubmission.id == submission.id).with_for_update()
        )
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
            .values(state="cancelled", error_code="operation_cancelled", error_message=CANCELLED, updated_at=_now())
        )
        await db.commit()
        return True
