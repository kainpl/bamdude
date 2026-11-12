"""An operator's swap declaration of a source: inspect the bytes, then declare them (swap-source-declaration spec r3 §4-5).

Inspect copies what a reprint would read (the bounded queue-source capture, ``plan_capture`` over the row), checks
it is printable, discards the copy, and answers the bytes' hash, the source's current effective fact and an opaque
revision. Declare copies it AGAIN -- no lease is kept between the two -- and only the very bytes the operator saw,
under an unchanged revision and rights read afresh, get the declaration, written by ``swap_compatibility``'s one
writer together with its audit line in one short transaction. No file I/O happens under a database lock.

The executed run's fact of an archive is never touched here; a library row's hash and metadata are never repaired
here either (an invalid hash asks for the folder's scan first). Who may: the read AND the update right of that very
source (own or all); an API key both scopes; an invisible source is a 404.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.core.permissions import Permission
from backend.app.services import queue_sources, swap_compatibility as sc

logger = logging.getLogger(__name__)

SOURCE_KINDS = ("archive", "library")

SOURCE_CHANGED = "The source changed since it was inspected. Inspect it again."
REFRESH_REQUIRED = (
    "This library file has no verified content hash yet. Scan its folder, or upload it again as a new copy, "
    "then inspect it."
)
OPERATION_CONFLICT = "This operation was already used for another declaration."
SOURCE_PRINTING = "This archive is still printing. Declare its source once the print has ended."
RIGHTS_GONE = "You may no longer declare this source."

_PERMISSIONS = {
    "archive": (
        Permission.ARCHIVES_READ_ALL.value,
        Permission.ARCHIVES_READ_OWN.value,
        Permission.ARCHIVES_UPDATE_ALL.value,
        Permission.ARCHIVES_UPDATE_OWN.value,
    ),
    "library": (
        Permission.LIBRARY_READ_ALL.value,
        Permission.LIBRARY_READ_OWN.value,
        Permission.LIBRARY_UPDATE_ALL.value,
        Permission.LIBRARY_UPDATE_OWN.value,
    ),
}
_NOT_FOUND = {"archive": "Archive not found", "library": "File not found"}
_NOT_OWN = {"archive": "You can only update your own archives", "library": "You can only update your own files"}


@dataclass(frozen=True)
class Actor:
    """Who declares, as the audit records it: a signed-in user, or an API key (its owner, if any)."""

    kind: str  # "user" | "api_key"
    user_id: int | None
    label: str
    #: The API key's row id, for the rights read afresh before the commit -- on the write's own session.
    key_id: int | None = None
    capture: object | None = None


@dataclass(frozen=True)
class Rights:
    """What the request's gates granted: ``*_all``, else only the caller's own rows."""

    read_all: bool
    update_all: bool


def _model(kind: str):
    if kind == "archive":
        from backend.app.models.archive import PrintArchive

        return PrintArchive
    if kind == "library":
        from backend.app.models.library import LibraryFile

        return LibraryFile
    raise ValueError(f"not a source kind: {kind!r}")


def _mine(row, actor: Actor) -> bool:
    owner = getattr(row, "created_by_id", None)
    return owner is not None and actor.user_id is not None and owner == actor.user_id


def _gate(kind: str, row, actor: Actor, rights: Rights, *, update: bool = True) -> None:
    """Visible (read), then updatable, then in a state a declaration may be made in. Invisible is a 404."""
    if row is None or getattr(row, "deleted_at", None) is not None:
        raise HTTPException(404, _NOT_FOUND[kind])
    mine = _mine(row, actor)
    if not (rights.read_all or mine):
        raise HTTPException(404, _NOT_FOUND[kind])
    if update and not (rights.update_all or mine):
        raise HTTPException(403, _NOT_OWN[kind])
    if update and kind == "archive" and getattr(row, "status", None) == "printing":
        raise HTTPException(409, {"error": "source_printing", "message": SOURCE_PRINTING})


async def actor_for(request, db, user) -> Actor:
    """The actor of a request whose gates passed: the signed-in user, or the API key and its owner."""
    from backend.app.core.auth import capture_request_authority

    capture = await capture_request_authority(request)
    if user is not None:
        return Actor(kind="user", user_id=user.id, label=user.username, capture=capture)
    from backend.app.core.auth import acting_user, is_api_key_token

    credential = request.headers.get("X-API-Key")
    if not credential:
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else None
        credential = token if is_api_key_token(token) else None
    owner = await acting_user(request, db, None)
    key_id = None
    if credential:
        from backend.app.core.auth import APIKeyValidationFailure, resolve_api_key_authority

        try:  # the gate's own lookup for this request, cached on it: no second validation
            key_id = (await resolve_api_key_authority(request, credential)).key.id
        except APIKeyValidationFailure:
            key_id = None
    return Actor(
        kind="api_key",
        user_id=owner.id if owner is not None else None,
        label=f"API key ({owner.username})" if owner is not None else "API key",
        key_id=key_id,
        capture=capture,
    )


async def fresh_api_key(db, actor: Actor):
    """An API key's row read again on THIS session -- enabled, unexpired -- for the rights checked before a commit.

    Never ``resolve_api_key_authority``: that validates in sessions of its own and writes the key's ``last_used``,
    and on SQLite that write waits for the very writer this transaction holds (T9 final review, C1).
    """
    from backend.app.core.auth_lifetime import validate_captured

    if actor.capture is None or actor.capture.kind != "key":
        raise HTTPException(403, RIGHTS_GONE)
    key = (await validate_captured(db, actor.capture)).api_key
    if key is None or key.id != actor.key_id:
        raise HTTPException(403, RIGHTS_GONE)
    return key


async def _fresh_rights(db, kind: str, actor: Actor) -> Rights:
    """The actor's rights read again, inside the write transaction: a right revoked while the bytes were being
    read commits nothing (§4). No per-request cache is consulted."""
    read_all, read_own, update_all, update_own = _PERMISSIONS[kind]
    from backend.app.core.auth_lifetime import validate_captured

    if actor.capture is None:
        raise HTTPException(403, RIGHTS_GONE)
    principal = await validate_captured(db, actor.capture)
    if actor.kind == "api_key":
        from backend.app.core.auth import authorize_api_key

        # The key and its owner read on this session; nothing is written.
        await authorize_api_key(db, await fresh_api_key(db, actor), [read_all, update_all])
        return Rights(read_all=True, update_all=True)

    user = principal.user
    if user is None or not user.is_active:
        raise HTTPException(403, RIGHTS_GONE)
    may_read_all, may_update_all = user.has_permission(read_all), user.has_permission(update_all)
    if not (may_read_all or user.has_permission(read_own)) or not (may_update_all or user.has_permission(update_own)):
        raise HTTPException(403, RIGHTS_GONE)
    return Rights(read_all=may_read_all, update_all=may_update_all)


async def _capture(request):
    """The bounded queue-source copy, under the dispatcher's own deadline; its typed refusals, never retried."""
    from backend.app.services.background_dispatch import DISPATCH_CAPTURE_DEADLINE_SECONDS
    from backend.app.services.queue_source_capture import capture_for_dispatch, refusal

    try:
        return await capture_for_dispatch(request, deadline=DISPATCH_CAPTURE_DEADLINE_SECONDS)
    except queue_sources.QueueSourceError as exc:
        raise refusal(exc) from exc


async def _read_bytes(kind: str, row, request) -> tuple[str, int]:
    """Copy the source, check it is printable, discard the copy: its hash and size."""
    from backend.app.services.filament_requirements import PrintRequirementsCache
    from backend.app.services.queue_source_capture import discard_staged, staged_requirements

    staged = await _capture(request)
    try:
        await staged_requirements(
            staged,
            PrintRequirementsCache(),
            archive=row if kind == "archive" else None,
            library_file=row if kind == "library" else None,
            allow_raw_gcode=True,
        )
        return staged.receipt.sha256, staged.receipt.size_bytes
    finally:
        await discard_staged(staged)


def _plan(kind: str, row):
    from backend.app.services.queue_source_capture import plan_capture

    return plan_capture(archive=row) if kind == "archive" else plan_capture(library_file=row)


def _require_library_hash(kind: str, row) -> None:
    if kind == "library" and not sc.valid_hash(getattr(row, "file_hash", None)):
        raise HTTPException(409, {"error": "library_refresh_required", "message": REFRESH_REQUIRED})


def _log(operation: str, kind: str, source_id: int, outcome: str, started: float, reason: str | None = None) -> None:
    logger.info(
        "swap declaration %s kind=%s id=%s outcome=%s elapsed=%.2fs%s",
        operation,
        kind,
        source_id,
        outcome,
        time.monotonic() - started,
        f" reason={reason}" if reason else "",
    )


def _reason(exc: BaseException) -> str:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict) and "error" in detail:
        return str(detail["error"])
    status = getattr(exc, "status_code", None)
    return f"http_{status}" if status else type(exc).__name__


async def inspect(kind: str, source_id: int, *, actor: Actor, rights: Rights) -> dict:
    """The bytes a reprint would read, their current effective fact and the revision a declaration must match."""
    from backend.app.core.database import async_session

    started = time.monotonic()
    try:
        async with async_session() as db:
            row = await db.get(_model(kind), source_id)
            _gate(kind, row, actor, rights)
            _require_library_hash(kind, row)
            request = _plan(kind, row)
            revision = sc.source_revision(row, epoch=queue_sources.current_epoch())
            current = sc.source_effective(row)
            declared = sc.current_declaration(row)
            display = row.filename
        actual, size = await _read_bytes(kind, row, request)
        if kind == "library" and actual != row.file_hash:
            raise HTTPException(409, {"error": "swap_source_changed", "message": SOURCE_CHANGED})
    except Exception as exc:
        _log("inspect", kind, source_id, "refused", started, _reason(exc))
        raise
    _log("inspect", kind, source_id, "ok", started)
    return {
        "kind": kind,
        "id": source_id,
        "sha256": actual,
        "size_bytes": size,
        "display_filename": display,
        "current": current,
        "known": current is not None,
        "revision": revision,
        "origin": declared.origin if declared is not None else None,
        "declaration_id": (declared.operator or {}).get("declaration_id") if declared is not None else None,
    }


def _request_digest(kind: str, source_id: int, value: bool, expected_sha256: str, expected_revision: str) -> str:
    payload = {"kind": kind, "id": source_id, "value": value, "sha256": expected_sha256, "revision": expected_revision}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _bound_digest(digest, actor):
    from backend.app.core.auth_lifetime import FrozenAuthority

    if not isinstance(actor.capture, FrozenAuthority):
        raise HTTPException(401, "Immutable authority is required")
    return hashlib.sha256(
        json.dumps([digest, actor.capture.account_identity, actor.capture.key_identity], separators=(",", ":")).encode()
    ).hexdigest()


def _audit_line(**fields):
    """The audit row of one operation -- its own function, so a refused write is a refused commit."""
    from backend.app.models.swap_source_declaration_audit import SwapSourceDeclarationAudit

    return SwapSourceDeclarationAudit(**fields)


def restored_audit(kind: str, source_id: int, record: dict):
    """The audit line of a declaration a git restore brought in: it names the ORIGINAL declaration and its actor's
    label, and no local user -- a restore is not a local operator's confirmation (§9)."""
    return _audit_line(
        operation_id=str(uuid.uuid4()),
        action="restore",
        source_kind=kind,
        source_id=source_id,
        sha256=record["sha256"],
        old_value=None,
        new_value=record["value"],
        declaration_id=record["declaration_id"],
        previous_declaration_id=None,
        actor_id=None,
        actor_label=record["actor_label"],
        actor_kind="restored",
        revision_before=None,
        revision_after=None,
        request_digest=None,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


def _replay(previous, row, *, kind: str, source_id: int, digest: str, actor: Actor) -> dict:
    """An operation id already used: the same request by the same actor, whose declaration still stands, answers
    its first result; anything else is a conflict. The caller has already passed the source's gate."""
    current = sc.current_declaration(row)
    same = (
        previous.request_digest == digest
        and previous.source_kind == kind
        and previous.source_id == source_id
        and previous.actor_kind == actor.kind
        and previous.actor_id == actor.user_id
        and current is not None
        and (current.operator or {}).get("declaration_id") == previous.declaration_id
    )
    if not same:
        raise HTTPException(409, {"error": "swap_declaration_conflict", "message": OPERATION_CONFLICT})
    return {
        "kind": kind,
        "id": source_id,
        "declaration_id": previous.declaration_id,
        "value": previous.new_value,
        "sha256": previous.sha256,
        "revision": sc.source_revision(row, epoch=queue_sources.current_epoch()),
        "replayed": True,
    }


async def _operation(db, operation_id: str):
    """The audit line of an operation id, read fresh."""
    from backend.app.models.swap_source_declaration_audit import SwapSourceDeclarationAudit

    return (
        await db.execute(
            select(SwapSourceDeclarationAudit)
            .where(SwapSourceDeclarationAudit.operation_id == operation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def declare(
    kind: str,
    source_id: int,
    *,
    value: bool,
    expected_sha256: str,
    expected_revision: str,
    operation_id: str,
    actor: Actor,
    rights: Rights,
) -> dict:
    """Declare the source over the bytes the operator inspected -- or refuse, writing nothing."""
    from backend.app.core.database import async_session

    if type(value) is not bool:
        raise HTTPException(422, "A declaration is true or false.")
    started = time.monotonic()
    digest = _bound_digest(_request_digest(kind, source_id, value, expected_sha256, expected_revision), actor)
    try:
        async with async_session() as db:
            row = await db.get(_model(kind), source_id)
            _gate(kind, row, actor, rights)
            previous = await _operation(db, operation_id)
            if previous is not None:
                result = _replay(previous, row, kind=kind, source_id=source_id, digest=digest, actor=actor)
                _log(operation_id, kind, source_id, "replayed", started)
                return result
            _require_library_hash(kind, row)
            if sc.source_revision(row, epoch=queue_sources.current_epoch()) != expected_revision:
                raise HTTPException(409, {"error": "swap_source_changed", "message": SOURCE_CHANGED})
            request = _plan(kind, row)
        actual, _size = await _read_bytes(kind, row, request)
        if actual != expected_sha256:
            raise HTTPException(409, {"error": "swap_source_changed", "message": SOURCE_CHANGED})
        result = await _write(
            kind,
            source_id,
            value=value,
            sha256=actual,
            expected_revision=expected_revision,
            operation_id=operation_id,
            digest=digest,
            actor=actor,
        )
    except Exception as exc:
        _log(operation_id, kind, source_id, "refused", started, _reason(exc))
        raise
    _log(operation_id, kind, source_id, "replayed" if result["replayed"] else "declared", started)
    return result


async def _locked_row(db, kind: str, source_id: int):
    """The authoritative row under the source's write protocol: an archive's write scope (``load_active_archive_for_
    write``, entered by the caller), a library row's SQLite writer then ``FOR UPDATE``."""
    if kind == "archive":
        from backend.app.services.archive_write_scope import load_active_archive_for_write

        return await load_active_archive_for_write(db, source_id)
    from backend.app.core.database import take_write_lock
    from backend.app.models.library import LibraryFile

    await take_write_lock(db, LibraryFile.__table__, source_id)
    return (
        await db.execute(
            select(LibraryFile)
            .where(LibraryFile.id == source_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _write(
    kind: str,
    source_id: int,
    *,
    value: bool,
    sha256: str,
    expected_revision: str,
    operation_id: str,
    digest: str,
    actor: Actor,
) -> dict:
    from contextlib import nullcontext

    from backend.app.core.database import async_session
    from backend.app.services.archive_write_scope import archive_write_scope

    async with async_session() as db:
        from backend.app.core.auth_lifetime import guard_accounts

        async def prepare():
            if actor.capture is None:
                raise HTTPException(401, "Immutable authority is required")
            await guard_accounts(db, [actor.capture])

        scope = archive_write_scope(db, source_id, first=prepare) if kind == "archive" else nullcontext()
        async with scope:
            if kind != "archive":
                await prepare()
            row = await _locked_row(db, kind, source_id)
            _gate(kind, row, actor, await _fresh_rights(db, kind, actor))
            # The same request may have overtaken this one after both passed the preflight: read the operation
            # again now that the source is ours, before any stale-revision refusal (T9.7 R3).
            previous = await _operation(db, operation_id)
            if previous is not None:
                return _replay(previous, row, kind=kind, source_id=source_id, digest=digest, actor=actor)
            if kind == "library" and (not sc.valid_hash(row.file_hash) or row.file_hash != sha256):
                raise HTTPException(409, {"error": "swap_source_changed", "message": SOURCE_CHANGED})
            if sc.source_revision(row, epoch=queue_sources.current_epoch()) != expected_revision:
                raise HTTPException(409, {"error": "swap_source_changed", "message": SOURCE_CHANGED})
            old_value = sc.source_effective(row)
            replaced = sc.current_declaration(row)
            declaration_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc)
            sc.write_source_declaration(
                row,
                value=value,
                sha256=sha256,
                origin="operator",
                operator={
                    "declaration_id": declaration_id,
                    "declared_at": now.isoformat(),
                    "actor_id": actor.user_id,
                    "actor_label": actor.label,
                    "actor_kind": actor.kind,
                },
            )
            if kind == "library":
                from backend.app.services.library_helpers import sync_system_tags

                await sync_system_tags(db, row)
            revision_after = sc.source_revision(row, epoch=queue_sources.current_epoch())
            db.add(
                _audit_line(
                    operation_id=operation_id,
                    action="declare",
                    source_kind=kind,
                    source_id=source_id,
                    sha256=sha256,
                    old_value=old_value,
                    new_value=value,
                    declaration_id=declaration_id,
                    previous_declaration_id=(replaced.operator or {}).get("declaration_id") if replaced else None,
                    actor_id=actor.user_id,
                    actor_label=actor.label,
                    actor_kind=actor.kind,
                    revision_before=expected_revision,
                    revision_after=revision_after,
                    request_digest=digest,
                    created_at=now.replace(tzinfo=None),
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                # Another session -- another process, or another source the operation id was reused for -- committed
                # this operation id between our read and our commit. Only the same request is its replay.
                await db.rollback()
                previous = await _operation(db, operation_id)
                if previous is None:
                    raise
                row = await db.get(_model(kind), source_id, populate_existing=True)
                _gate(kind, row, actor, await _fresh_rights(db, kind, actor))
                return _replay(previous, row, kind=kind, source_id=source_id, digest=digest, actor=actor)
    return {
        "kind": kind,
        "id": source_id,
        "declaration_id": declaration_id,
        "value": value,
        "sha256": sha256,
        "revision": revision_after,
        "replayed": False,
    }


async def history(kind: str, source_id: int, *, actor: Actor, rights: Rights) -> list[dict]:
    """The current declaration's chain -- never every audit row filed under a (possibly reused) source id."""
    from backend.app.core.database import async_session
    from backend.app.models.swap_source_declaration_audit import SwapSourceDeclarationAudit as Audit

    async with async_session() as db:
        row = await db.get(_model(kind), source_id)
        _gate(kind, row, actor, rights, update=False)
        current = sc.current_declaration(row)
        declaration_id = (current.operator or {}).get("declaration_id") if current is not None else None
        chain: list[dict] = []
        seen: set[str] = set()
        while declaration_id and declaration_id not in seen:
            seen.add(declaration_id)
            line = (
                await db.execute(
                    select(Audit)
                    .where(
                        Audit.declaration_id == declaration_id, Audit.source_kind == kind, Audit.source_id == source_id
                    )
                    .where(Audit.action.in_(("declare", "restore")))
                    .order_by(Audit.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if line is None:
                break
            chain.append(
                {
                    "declaration_id": line.declaration_id,
                    "action": line.action,
                    "old_value": line.old_value,
                    "new_value": line.new_value,
                    "sha256": line.sha256,
                    "actor_label": line.actor_label,
                    "actor_kind": line.actor_kind,
                    "created_at": line.created_at.isoformat() if line.created_at else None,
                }
            )
            declaration_id = line.previous_declaration_id
        return chain
