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

import base64
import binascii
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import or_, select, update
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
COMPACTED = "This operation's full record has been compacted; submit a new operation to print again."
FULL_RETENTION_DAYS = 30
_TERMINAL = ("published", "rejected", "cancelled")
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
    capture: object | None = None


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


async def stage_storage_epoch(db: AsyncSession) -> str:
    """Stage a new namespace in the caller's restore transaction, without committing it."""
    epoch = str(uuid.uuid4())
    result = await db.execute(update(Settings).where(Settings.key == STORAGE_EPOCH_KEY).values(value=epoch))
    if result.rowcount == 0:
        db.add(Settings(key=STORAGE_EPOCH_KEY, value=epoch))
    return epoch


async def rotate_storage_epoch(db: AsyncSession) -> str:
    """A new storage epoch, committed: every operation of the old one stops answering (spec r2 §8.4)."""
    epoch = await stage_storage_epoch(db)
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
    from backend.app.core.auth import AUTH_DATABASE_FAILURES
    from backend.app.core.auth_lifetime import FrozenAuthority, validate_captured

    if (
        not isinstance(actor.capture, FrozenAuthority)
        or actor.capture.kind != "key"
        or actor.capture.key_identity != actor.key_identity
        or actor.capture.user_id != actor.owner_id
    ):
        raise ConnectorRefusal(401, "invalid_key", INVALID_KEY)
    try:
        principal = await validate_captured(db, actor.capture, required_permissions=permissions)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise ConnectorRefusal(401, "invalid_key", INVALID_KEY) from exc
        if exc.status_code == 503:
            raise ConnectorRefusal(
                503, "authentication_unavailable", "Authentication temporarily unavailable", retryable=True
            ) from exc
        raise ConnectorRefusal(exc.status_code, "permission_denied", PERMISSION_DENIED) from exc
    except AUTH_DATABASE_FAILURES as exc:
        raise ConnectorRefusal(
            503, "authentication_unavailable", "Authentication temporarily unavailable", retryable=True
        ) from exc
    key = principal.api_key
    if key is None or key.id != actor.key_id:
        raise ConnectorRefusal(401, "invalid_key", INVALID_KEY)
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
    """A recorded operation and the proof of it, fixed in the session that found or created it: its answer is read
    through that proof, never by the numeric id alone (H0 review r2)."""

    submission_id: int
    created: bool
    proof: OwnProof


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


OPERATION_NOT_FOUND = "The operation was not found."


@dataclass(frozen=True)
class OwnProof:
    """That an operation is this key's own, in the namespace fixed when it was proven. Every later read by the
    numeric id checks the row against it: the id is an address, never the authority (H0 review R1)."""

    submission_id: int
    operation_id: str
    key_identity: str
    server_id: str
    epoch: str


async def prove_own(actor: ConnectorActor, operation_id: str, server_id: str | None, epoch: str | None) -> OwnProof:
    """Find THIS key's operation and fix the namespace in the same session: an omitted server/epoch is the current
    one, read once here and never again later; one that differs from the current is 409."""
    async with database.async_session() as db:
        current = await read_server_identity(db)
        if (server_id and server_id != current[0]) or (epoch and epoch != current[1]):
            raise ConnectorRefusal(409, "context_changed", CONTEXT_CHANGED)
        expected = (server_id or current[0], epoch or current[1])
        row = None
        if actor.key_identity and expected[0] and expected[1]:
            row = await _find(db, actor, expected[0], expected[1], operation_id)
    if row is None:
        raise ConnectorRefusal(404, "operation_not_found", OPERATION_NOT_FOUND)
    return OwnProof(row.id, operation_id, actor.key_identity, expected[0], expected[1])


def _proof_of(row: ConnectorSubmission) -> OwnProof:
    """The proof of a row just found by its full identity (``_find``) -- in the session that found it."""
    return OwnProof(row.id, row.operation_id, row.api_key_identity, row.server_id, row.storage_epoch)


async def _verified(db: AsyncSession, proof: OwnProof) -> ConnectorSubmission:
    """The proven operation's row, read now -- still this operation of this key in the proven namespace, and the
    server still in it. Anything else is 409: a restore or replacement happened in between."""
    row = await db.get(ConnectorSubmission, proof.submission_id, populate_existing=True)
    if (
        row is None
        or (row.operation_id, row.api_key_identity, row.server_id, row.storage_epoch)
        != (proof.operation_id, proof.key_identity, proof.server_id, proof.epoch)
        or await read_server_identity(db) != (proof.server_id, proof.epoch)
    ):
        raise ConnectorRefusal(409, "context_changed", CONTEXT_CHANGED)
    return row


async def confirm(proof: OwnProof) -> None:
    """After every read of an answer: the proof still holds, so nothing in it came from another database."""
    async with database.async_session() as db:
        await _verified(db, proof)


async def _lock_proven(db: AsyncSession, proof: OwnProof) -> ConnectorSubmission:
    """Before a write addressed by the numeric id: lock the operation's row -- SQLite's writer, PostgreSQL's row --
    and verify the proof under that lock, in the transaction the write commits in. Nothing replaces the row between
    the check and the write; a row that is no longer the proven operation is 409 and nothing is written (H0 review
    r2). The parent first, as the publication's claim and every other writer of an operation take it."""
    await database.take_write_lock(db, ConnectorSubmission.__table__, proof.submission_id)
    await db.execute(
        select(ConnectorSubmission.id).where(ConnectorSubmission.id == proof.submission_id).with_for_update()
    )
    return await _verified(db, proof)


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
    return Registered(existing.id, False, _proof_of(existing))


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
        return Registered(submission.id, True, _proof_of(submission))


# --------------------------------------------------------------------------- #
# The receipt the writer records with the jobs
# --------------------------------------------------------------------------- #


async def _job_identities(session: AsyncSession, queue_item_ids: list[int], auto_item_ids: list[int]) -> list[dict]:
    """(kind, id, identity) of every job a publication created, read in its own session (host spec r2.2 §11 S2).

    The numeric id is an address inside this transaction only; the identity is what a later reader follows. A row
    without one (never expected here) is recorded with a null identity and reads as unavailable, never by its id.
    """
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.print_queue import PrintQueueItem

    queue_rows = {}
    if queue_item_ids:
        queue_rows = dict(
            (
                await session.execute(
                    select(PrintQueueItem.id, PrintQueueItem.recovery_identity).where(
                        PrintQueueItem.id.in_(queue_item_ids)
                    )
                )
            ).all()
        )
    auto_rows = {}
    if auto_item_ids:
        auto_rows = dict(
            (
                await session.execute(
                    select(AutoQueueItem.id, AutoQueueItem.recovery_identity).where(AutoQueueItem.id.in_(auto_item_ids))
                )
            ).all()
        )
    return [{"kind": "queue", "id": i, "identity": queue_rows.get(i)} for i in queue_item_ids] + [
        {"kind": "auto", "id": i, "identity": auto_rows.get(i)} for i in auto_item_ids
    ]


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
        jobs = await _job_identities(session, queue_item_ids, auto_item_ids)
        entry.published_receipt = {
            "source_sha256": self.manifest["source_sha256"],
            "library_file_id": self.manifest["library_file_id"],
            "plate_id": self.entry["plate_id"],
            "quantity": self.entry["quantity"],
            "target": {k: target[k] for k in sorted(target)},
            "queue_item_ids": list(queue_item_ids),
            "auto_item_ids": list(auto_item_ids),
            "jobs": jobs,
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
        if submission is None or submission.api_key_identity != actor.key_identity or is_compacted(submission):
            return
        if (submission.server_id, submission.storage_epoch) != await read_server_identity(db):
            return  # another namespace: a restore came between the request and this coordinator
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


async def ensure_resumable(proof: OwnProof, *, sessions=None) -> None:
    """A compacted operation is never published again: 410, and a new operation is the way to print."""
    factory = sessions or database.async_session
    async with factory() as db:
        ensure_not_compacted(await _verified(db, proof))


async def make_resumable(proof: OwnProof, *, sessions=None) -> int:
    """Entries a crashed or abandoned coordinator left ``preparing`` become startable again -- only by an explicit
    resume, and only while no receipt exists (spec r2 §8.2). The caller guarantees no coordinator is running."""
    factory = sessions or database.async_session
    async with factory() as db:
        await _lock_proven(db, proof)
        result = await db.execute(
            update(ConnectorSubmissionEntry)
            .where(
                ConnectorSubmissionEntry.submission_id == proof.submission_id,
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


async def result_of(proof: OwnProof, *, replayed: bool = False, sessions=None) -> SubmissionResult:
    """The proven operation's answer. The proof is the address: a row that is no longer it is 409."""
    factory = sessions or database.async_session
    async with factory() as db:
        submission = await _verified(db, proof)
        submission_id = proof.submission_id
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
    return await result_of(_proof_of(submission), replayed=replayed, sessions=factory)


async def submission_id_for(actor: ConnectorActor, operation_id: str, *, sessions=None) -> int | None:
    factory = sessions or database.async_session
    async with factory() as db:
        submission = await find_own(db, actor.key_identity, operation_id)
        return None if submission is None else submission.id


async def cancel(proof: OwnProof, *, sessions=None) -> None:
    """Fence the unpublished entries of the proven operation. Published ones stay as they are."""
    factory = sessions or database.async_session
    async with factory() as db:
        # The parent, then the entries in their order -- the order the publication's claim takes them in.
        submission = await _lock_proven(db, proof)
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


# --------------------------------------------------------------------------- #
# Compaction
# --------------------------------------------------------------------------- #


def is_compacted(submission: ConnectorSubmission) -> bool:
    return submission.compacted_at is not None


def ensure_not_compacted(submission: ConnectorSubmission) -> None:
    if is_compacted(submission):
        raise ConnectorRefusal(410, "receipt_compacted", COMPACTED)


async def compact(*, older_than_days: int = FULL_RETENTION_DAYS, sessions=None) -> int:
    """Finished operations older than the retention keep a tombstone: the namespace, the manifest hash, each entry's
    id and target, and every receipt. A compacted operation still answers its recorded result and its replay; it is
    never published again (spec r2 §8.1). An operation with work left keeps its full manifest."""
    from datetime import timedelta

    factory = sessions or database.async_session
    cutoff = _now() - timedelta(days=older_than_days)
    compacted = 0
    async with factory() as db:
        candidates = (
            await db.execute(
                select(ConnectorSubmission.id).where(
                    ConnectorSubmission.compacted_at.is_(None), ConnectorSubmission.created_at < cutoff
                )
            )
        ).scalars()
        ids = list(candidates)
    for submission_id in ids:
        async with factory() as db:
            submission = (
                await db.execute(
                    select(ConnectorSubmission).where(ConnectorSubmission.id == submission_id).with_for_update()
                )
            ).scalar_one_or_none()
            if submission is None or is_compacted(submission):
                continue
            entries = (
                (
                    await db.execute(
                        select(ConnectorSubmissionEntry)
                        .where(ConnectorSubmissionEntry.submission_id == submission_id)
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if any(entry.published_receipt is None and entry.state not in _TERMINAL for entry in entries):
                await db.rollback()
                continue
            submission.manifest = {
                "compacted": True,
                "entries": [
                    {"entry_id": entry["entry_id"], "target": entry.get("target") or {}}
                    for entry in submission.manifest.get("entries") or []
                ],
            }
            submission.compacted_at = _now()
            await db.commit()
            compacted += 1
    return compacted


# --------------------------------------------------------------------------- #
# Observations: what became of each published job (host spec r2.2 §11 S3)
# --------------------------------------------------------------------------- #

OBSERVATION_PAGE = 100
OBSERVATION_EXECUTIONS = 3
OBSERVATION_MAX_BYTES = 256 * 1024
INVALID_CURSOR = "The observation cursor does not belong to this operation."

# A queue row's status -> the job's state. An AutoQueue job placed on a printer reads "assigned" until it prints.
_ROW_STATE = {
    "pending": "queued",
    "printing": "printing",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "skipped": "cancelled",
}
_ASSIGNED_STATE = {**_ROW_STATE, "pending": "assigned"}
_AUTO_STATE = {"pending": "queued", "assigned": "assigned", "cancelled": "cancelled", "failed": "failed"}
_EXECUTION_STATE = {
    "printing": "printing",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "stopped": "cancelled",
    "aborted": "cancelled",
}
_RUNNING = ("RUNNING", "PAUSE")


def _encode_cursor(operation_id: str, server_id: str, epoch: str, offset: int) -> str:
    raw = json.dumps({"o": operation_id, "s": server_id, "e": epoch, "n": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def _decode_cursor(cursor: str, operation_id: str, server_id: str, epoch: str) -> int:
    """The offset a cursor of THIS operation and namespace carries; anything else is refused."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError, binascii.Error, UnicodeError):
        data = None
    offset = data.get("n") if isinstance(data, dict) else None
    if (
        not isinstance(data, dict)
        or data.get("o") != operation_id
        or data.get("s") != server_id
        or data.get("e") != epoch
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset <= 0
    ):
        raise ConnectorRefusal(400, "invalid_cursor", INVALID_CURSOR)
    return offset


def _receipt_jobs(receipt: dict) -> list[dict]:
    """The jobs a receipt names. A receipt from before the lineage carries ids only: its jobs have no identity."""
    if "jobs" in receipt:
        return [dict(job) for job in receipt["jobs"]]
    return [{"kind": "queue", "id": i, "identity": None} for i in receipt.get("queue_item_ids") or []] + [
        {"kind": "auto", "id": i, "identity": None} for i in receipt.get("auto_item_ids") or []
    ]


def _newest(rows: list) -> list:
    """Newest first: creation time, then identity -- a deterministic order that never reads a numeric id."""
    return sorted(rows, key=lambda row: (row.created_at or datetime.min, row.recovery_identity or ""), reverse=True)


def _unavailable(observed: dict, reason: str) -> dict:
    return {**observed, "state": "unavailable", "reason": reason, "printer": None, "executions": [],
            "executions_complete": True}  # fmt: skip


def _first_layer(archive, state: str) -> bool | None:
    """Whether THIS execution has printed its first layer, observed now -- else None (H0 review R2).

    Only a binding of the live run with a proven origin (our dispatch, a live external start, an exact subtask id)
    to this very execution archive, on the client connection that made it, lets the printer's layer count speak for
    it. A newer row, a "printing" status, a name or a "completed" label prove nothing about a layer, and neither
    does the state a disconnected or stale printer left in its cache: ``get_status`` marks staleness on this very
    read, and only ``connected`` is True counts -- a generation still on record is no proof of a live link (H0 review
    r2). Read without an await, so the binding and the live state are one moment.
    """
    if state != "printing" or archive.printer_id is None:
        return None
    from backend.app.services import print_run_binding
    from backend.app.services.printer_manager import printer_manager

    run = print_run_binding.current_print_run(printer_manager, archive.printer_id)
    if not print_run_binding.origin_is_proven(run) or run.archive_id != archive.id:
        return None
    if run.client_generation is not None and printer_manager.current_client_generation(archive.printer_id) != (
        run.client_generation
    ):
        return None
    live = printer_manager.get_status(archive.printer_id)
    if live is None or getattr(live, "connected", False) is not True or getattr(live, "state", None) not in _RUNNING:
        return None
    return (getattr(live, "layer_num", 0) or 0) >= 1


def _execution_state(archive) -> str:
    """An execution our dispatch has not seen the printer accept is ``dispatching``, never ``printing``."""
    from backend.app.services.dispatch_phase import UNACCEPTED

    state = _EXECUTION_STATE.get(archive.status, "printing")
    if state == "printing" and archive.dispatch_phase in UNACCEPTED:
        return "dispatching"
    return state


async def observations(actor: ConnectorActor, proof: OwnProof, *, cursor: str | None = None) -> dict:
    """One page of what became of an operation's published jobs, found by identities only.

    The caller has already proven the operation is this key's own in this namespace. The key is read again for its
    current printer list: a printer outside it is shown as ``out_of_scope`` with no detail. Nothing is written.
    """
    from backend.app.models.archive import PrintArchive
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.print_queue import PrintQueueItem

    operation_id, server_id, epoch, submission_id = (
        proof.operation_id,
        proof.server_id,
        proof.epoch,
        proof.submission_id,
    )
    offset = _decode_cursor(cursor, operation_id, server_id, epoch) if cursor else 0
    async with database.async_session() as db:
        submission = await _verified(db, proof)
        key = await fresh_key(db, actor, (Permission.QUEUE_CREATE.value,))
        scope = set(key.printer_ids) if key.printer_ids is not None else None
        rows = {
            row.entry_id: row
            for row in (
                await db.execute(
                    select(ConnectorSubmissionEntry).where(ConnectorSubmissionEntry.submission_id == submission_id)
                )
            ).scalars()
        }
        listed: list[tuple[str, dict]] = []
        for manifest_entry in submission.manifest["entries"]:
            entry = rows.get(manifest_entry["entry_id"])
            if entry is not None and entry.published_receipt is not None:
                listed += [(entry.entry_id, job) for job in _receipt_jobs(entry.published_receipt)]
        page = listed[offset : offset + OBSERVATION_PAGE]
        following = offset + OBSERVATION_PAGE
        next_cursor = _encode_cursor(operation_id, server_id, epoch, following) if following < len(listed) else None

        queue_ids = {job["identity"] for _, job in page if job["kind"] == "queue" and job.get("identity")}
        auto_ids = {job["identity"] for _, job in page if job["kind"] == "auto" and job.get("identity")}
        live_rows: dict = {}
        if queue_ids:
            query = select(PrintQueueItem).where(PrintQueueItem.recovery_identity.in_(queue_ids))
            live_rows = {row.recovery_identity: row for row in (await db.execute(query)).scalars()}
        live_auto: dict = {}
        assignments: dict[str, list] = {}
        if auto_ids:
            query = select(AutoQueueItem).where(AutoQueueItem.recovery_identity.in_(auto_ids))
            live_auto = {row.recovery_identity: row for row in (await db.execute(query)).scalars()}
            query = select(PrintQueueItem).where(PrintQueueItem.source_auto_identity.in_(auto_ids))
            for row in (await db.execute(query)).scalars():
                assignments.setdefault(row.source_auto_identity, []).append(row)
        executions: dict[tuple[str, str], list] = {}
        if queue_ids or auto_ids:
            conditions = []
            if queue_ids:
                conditions.append(PrintArchive.queue_item_identity.in_(queue_ids))
            if auto_ids:
                conditions.append(PrintArchive.source_auto_identity.in_(auto_ids))
            for archive in (await db.execute(select(PrintArchive).where(or_(*conditions)))).scalars():
                if archive.queue_item_identity in queue_ids:
                    executions.setdefault(("queue", archive.queue_item_identity), []).append(archive)
                if archive.source_auto_identity in auto_ids:
                    executions.setdefault(("auto", archive.source_auto_identity), []).append(archive)

        printer_ids: set[int] = {row.queue_id for row in live_rows.values()}
        for placed in assignments.values():
            printer_ids.update(row.queue_id for row in placed)
        for runs in executions.values():
            printer_ids.update(a.printer_id for a in runs if a.printer_id is not None)
        printer_identity: dict = {}
        if printer_ids:
            query = select(Printer.id, Printer.connector_identity).where(Printer.id.in_(printer_ids))
            printer_identity = dict((await db.execute(query)).all())

        def printer_of(printer_id):
            return None if printer_id is None else {"id": printer_id, "identity": printer_identity.get(printer_id)}

        jobs = []
        for entry_id, job in page:
            identity = job.get("identity")
            observed: dict = {"entry_id": entry_id, "job": {"kind": job["kind"], "identity": identity}}
            if job["kind"] == "auto":
                observed["assignment"] = None
            if not identity:
                jobs.append(_unavailable(observed, "legacy_receipt"))
                continue
            runs = _newest(executions.get((job["kind"], identity), []))
            state, printer_id, assignment = None, None, None
            if job["kind"] == "queue":
                row = live_rows.get(identity)
                if row is not None:
                    state, printer_id = _ROW_STATE.get(row.status, "queued"), row.queue_id
                elif runs:
                    state, printer_id = _EXECUTION_STATE.get(runs[0].status, "printing"), runs[0].printer_id
            else:
                placed = _newest(assignments.get(identity, []))
                auto = live_auto.get(identity)
                if placed:
                    assignment = placed[0]
                    state, printer_id = _ASSIGNED_STATE.get(assignment.status, "assigned"), assignment.queue_id
                elif runs:
                    state, printer_id = _EXECUTION_STATE.get(runs[0].status, "printing"), runs[0].printer_id
                elif auto is not None:
                    state = _AUTO_STATE.get(auto.status, "queued")
            if state is None:
                jobs.append(_unavailable(observed, "chain_broken"))
                continue
            if printer_id is not None and scope is not None and printer_id not in scope:
                jobs.append(_unavailable(observed, "out_of_scope"))
                continue
            shown = []
            for archive in runs[:OBSERVATION_EXECUTIONS]:
                run_state = _execution_state(archive)
                shown.append(
                    {
                        "identity": archive.recovery_identity,
                        "state": run_state,
                        "started_at": archive.started_at.isoformat() if archive.started_at else None,
                        "completed_at": archive.completed_at.isoformat() if archive.completed_at else None,
                        "first_layer": _first_layer(archive, run_state),
                    }
                )
            if assignment is not None:
                observed["assignment"] = {
                    "identity": assignment.recovery_identity,
                    "printer": printer_of(assignment.queue_id),
                }
            jobs.append(
                {
                    **observed,
                    "state": state,
                    "printer": printer_of(printer_id),
                    "executions": shown,
                    "executions_complete": len(runs) <= OBSERVATION_EXECUTIONS,
                }
            )

        # A restore between the proof and this answer must not hand back the facts of another database.
        await _verified(db, proof)
    return {"jobs": jobs, "next_cursor": next_cursor}
