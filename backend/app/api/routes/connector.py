"""The slicer connector's HTTP contract (OrcaBamDude spec r2 §7, protocol 1).

API key only, by ``X-API-Key``: a signed-in browser session is not a connector. Every route asks the ordinary
permission gate first (scope flags, the owner's permissions, the key's printer list -- ``core/api_key_scope`` reads
the connector's targets too), then names the key by its immutable ``connector_identity``. Refusals are
``{"error": code, "message": English sentence}``; only the message is translated, the plugin reads the code.
"""

from __future__ import annotations

import errno
import logging
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import (
    APIKeyValidationFailure,
    RequirePermission,
    require_any_permission,
    resolve_api_key_authority,
)
from backend.app.core.config import APP_VERSION
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.i18n import current_language
from backend.app.i18n.api_errors import translate_sentence
from backend.app.models.settings import Settings
from backend.app.schemas import connector as protocol
from backend.app.services import connector_submissions as submissions
from backend.app.services.connector_runtime import MAX_UPLOADS, connector_runtime
from backend.app.services.connector_submissions import ConnectorActor, ConnectorRefusal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/connector", tags=["connector"])

PLUGIN_MIN_VERSION = "0.1.0"
ENABLED_KEY = "connector_enabled"
MAX_JSON_BODY = 256 * 1024
SUBMIT_WAIT_SECONDS = 10.0
POLL_MIN_SECONDS = 3
STATUS_BATCH_SIZE = 100
FULL_RETENTION_DAYS = 30
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_BYTES_CEILING = 512 * 1024 * 1024
UPLOAD_BYTES_KEY = "connector_max_upload_bytes"
UPLOAD_PERMISSIONS = (Permission.LIBRARY_UPLOAD.value, Permission.LIBRARY_READ_ALL.value)

BODY_TOO_LARGE = "The request body is too large."
INVALID_BODY = "The request body is not a valid connector request."
DISABLED = "The slicer connector is switched off on this server."
OPERATION_NOT_FOUND = "The operation was not found."
UPLOAD_TOO_LARGE = "The file is larger than this server accepts from the slicer connector."
NOT_A_3MF = "Only a sliced .3mf or .gcode.3mf file can be sent."
NOT_VISIBLE = "The uploaded content is not accessible to this API key."
STORAGE_FULL = "The server has no space left for this file."
UPLOAD_FAILED = "The file could not be stored."


def _refused(refusal: ConnectorRefusal) -> HTTPException:
    headers = {"Retry-After": "5"} if refusal.status == 429 else None
    return HTTPException(refusal.status, refusal.detail(), headers=headers)


async def bounded_json_body(request: Request) -> None:
    """Read the body once, never more than the cap, and leave it cached for the gate and the handler."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_JSON_BODY:
        raise HTTPException(413, {"error": "request_too_large", "message": BODY_TOO_LARGE})
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_JSON_BODY:
            raise HTTPException(413, {"error": "request_too_large", "message": BODY_TOO_LARGE})
        chunks.append(chunk)
    request._body = b"".join(chunks)


async def connector_actor(
    request: Request, x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None
) -> ConnectorActor:
    """The key, named by its immutable identity -- resolved once per request (the gate already did)."""
    if not x_api_key:
        raise HTTPException(401, {"error": "invalid_key", "message": submissions.INVALID_KEY})
    try:
        authority = await resolve_api_key_authority(request, x_api_key)
    except APIKeyValidationFailure as exc:
        raise HTTPException(401, {"error": "invalid_key", "message": submissions.INVALID_KEY}) from exc
    key = authority.key
    if not key.connector_identity:
        raise HTTPException(401, {"error": "invalid_key", "message": submissions.INVALID_KEY})
    return ConnectorActor(key_id=key.id, key_identity=key.connector_identity, owner_id=key.user_id)


async def _enabled(db: AsyncSession) -> bool:
    value = await db.scalar(select(Settings.value).where(Settings.key == ENABLED_KEY))
    return (value or "true").strip().lower() != "false"


async def _require_enabled(db: AsyncSession) -> None:
    """New work only; reading and cancelling what exists stays available (spec r2 §13 kill switch)."""
    if not await _enabled(db):
        raise HTTPException(503, {"error": "connector_disabled", "message": DISABLED})


def _answer(result: protocol.SubmissionResult, status: int) -> JSONResponse:
    lang = current_language()
    body = result.model_dump(mode="json")
    for entry in body["entries"]:
        if entry.get("error"):
            entry["error"]["message"] = translate_sentence(entry["error"]["message"], lang)
    return JSONResponse(status_code=status, content=body)


async def _may(actor: ConnectorActor, permissions) -> bool:
    from backend.app.core import database

    try:
        async with database.async_session() as db:
            await submissions.fresh_key(db, actor, permissions)
    except ConnectorRefusal:
        return False
    return True


@router.get("/capabilities")
async def capabilities(
    request: Request,
    _: None = Depends(
        require_any_permission(Permission.PRINTERS_READ, Permission.QUEUE_CREATE, Permission.LIBRARY_UPLOAD)
    ),
    actor: ConnectorActor = Depends(connector_actor),
    db: AsyncSession = Depends(get_db),
):
    """What this server speaks and what THIS key may do with it -- never a grant by itself."""
    from backend.app.core.api_key_scope import key_printer_scope

    server_id, epoch = await submissions.server_identity(db)
    enabled = await _enabled(db)
    unrestricted = key_printer_scope(request) is None
    may_submit = await _may(actor, submissions.SUBMIT_PERMISSIONS)
    may_upload = await _may(actor, (Permission.LIBRARY_UPLOAD.value, Permission.LIBRARY_READ_ALL.value))
    return {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "product": "bamdude",
        "plugin_min_version": PLUGIN_MIN_VERSION,
        "server_version": APP_VERSION,
        "connector_server_id": server_id,
        "storage_epoch": epoch,
        "actor_id": actor.key_identity,
        "enabled": enabled,
        "admitting": connector_runtime.admitting,
        "features": {
            "monitor": await _may(actor, (Permission.PRINTERS_READ.value,)),
            "library_upload": enabled and may_upload,
            "submissions": enabled and may_submit,
            "auto_target": enabled and may_submit and unrestricted,
            "camera_snapshot": False,
            "camera_stream": False,
            "native_single_plate": True,
            "supported_options": ["bed_levelling", "flow_cali", "nozzle_offset_cali", "timelapse", "layer_inspect"],
            "supported_topologies": ["single_external", "ams"],
        },
        "limits": {
            "max_upload_bytes": await _max_upload_bytes(db),
            "max_concurrent_uploads": MAX_UPLOADS,
            "max_entries": protocol.MAX_ENTRIES,
            "max_copies_per_entry": protocol.MAX_COPIES_PER_ENTRY,
            "max_total_jobs": protocol.MAX_TOTAL_JOBS,
            "status_batch_size": STATUS_BATCH_SIZE,
            "poll_min_seconds": POLL_MIN_SECONDS,
            "submission_full_retention_days": FULL_RETENTION_DAYS,
        },
    }


@router.post("/submissions")
async def create_submission(
    request: Request,
    _body: None = Depends(bounded_json_body),
    _: None = RequirePermission(Permission.QUEUE_CREATE, Permission.LIBRARY_READ_ALL),
    actor: ConnectorActor = Depends(connector_actor),
    db: AsyncSession = Depends(get_db),
):
    """Record an immutable operation and publish its entries. 201 first answer, 200 replay, 202 still running."""
    await _require_enabled(db)
    try:
        connector_runtime.ensure_admitting()
    except ConnectorRefusal as refusal:
        raise _refused(refusal) from refusal
    try:
        body = protocol.SubmissionCreate.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(422, {"error": "invalid_request", "message": INVALID_BODY}) from exc
    try:
        registered = await submissions.register(actor, body)
        task = connector_runtime.running(registered.submission_id)
        if task is None:
            current = await submissions.result_of(registered.submission_id, replayed=not registered.created)
            # A replay starts only what was never attempted (a coordinator that died before it began): a stale
            # "preparing" or a transient failure waits for an explicit resume, and a finished operation is read.
            if not any(entry.state == "pending" for entry in current.entries):
                return _answer(current, 201 if registered.created else 200)
            task = connector_runtime.start(registered.submission_id, actor, states=submissions.NEW_ONLY)
    except ConnectorRefusal as refusal:
        raise _refused(refusal) from refusal
    finished = await connector_runtime.wait(task, SUBMIT_WAIT_SECONDS)
    result = await submissions.result_of(registered.submission_id, replayed=not registered.created)
    if not finished:
        return _answer(result, 202)
    return _answer(result, 201 if registered.created else 200)


async def _own(actor: ConnectorActor, operation_id: str, server_id: str | None, storage_epoch: str | None) -> int:
    from backend.app.core import database

    async with database.async_session() as db:
        current = await submissions.read_server_identity(db)
        if (server_id and server_id != current[0]) or (storage_epoch and storage_epoch != current[1]):
            raise HTTPException(409, {"error": "context_changed", "message": submissions.CONTEXT_CHANGED})
        own = await submissions.find_own(db, actor.key_identity, operation_id)
    if own is None:
        raise HTTPException(404, {"error": "operation_not_found", "message": OPERATION_NOT_FOUND})
    return own.id


@router.get("/submissions/{operation_id}")
async def get_submission(
    operation_id: str,
    server_id: str | None = None,
    storage_epoch: str | None = None,
    _: None = RequirePermission(Permission.QUEUE_CREATE),
    actor: ConnectorActor = Depends(connector_actor),
):
    """This key's own operation, addressed by its id. A foreign one and a missing one are the same 404."""
    submission_id = await _own(actor, operation_id, server_id, storage_epoch)
    return _answer(await submissions.result_of(submission_id), 200)


@router.post("/submissions/{operation_id}/resume")
async def resume_submission(
    operation_id: str,
    server_id: str | None = None,
    storage_epoch: str | None = None,
    _: None = RequirePermission(Permission.QUEUE_CREATE, Permission.LIBRARY_READ_ALL),
    actor: ConnectorActor = Depends(connector_actor),
    db: AsyncSession = Depends(get_db),
):
    """Run the unpublished, not permanently refused entries again. A published entry is read, never re-run."""
    await _require_enabled(db)
    submission_id = await _own(actor, operation_id, server_id, storage_epoch)
    try:
        connector_runtime.ensure_admitting()
        await submissions.ensure_resumable(submission_id)
        task = connector_runtime.running(submission_id)
        if task is None:
            await submissions.make_resumable(submission_id)
            task = connector_runtime.start(submission_id, actor, states=submissions.RESUMABLE)
    except ConnectorRefusal as refusal:
        raise _refused(refusal) from refusal
    finished = await connector_runtime.wait(task, SUBMIT_WAIT_SECONDS)
    return _answer(await submissions.result_of(submission_id, replayed=True), 200 if finished else 202)


@router.post("/submissions/{operation_id}/cancel")
async def cancel_submission(
    operation_id: str,
    server_id: str | None = None,
    storage_epoch: str | None = None,
    _: None = RequirePermission(Permission.QUEUE_CREATE),
    actor: ConnectorActor = Depends(connector_actor),
):
    """Fence the entries not yet published. Published jobs stay; nothing on a printer is stopped."""
    submission_id = await _own(actor, operation_id, server_id, storage_epoch)
    await submissions.cancel(actor, operation_id)
    return _answer(await submissions.result_of(submission_id), 200)


async def _max_upload_bytes(db: AsyncSession) -> int:
    """The administrator's cap, never above the ceiling the memory acceptance measured (spec r2 §6)."""
    value = await db.scalar(select(Settings.value).where(Settings.key == UPLOAD_BYTES_KEY))
    try:
        cap = int(value) if value else DEFAULT_MAX_UPLOAD_BYTES
    except ValueError:
        cap = DEFAULT_MAX_UPLOAD_BYTES
    return max(1, min(cap, MAX_UPLOAD_BYTES_CEILING))


def _staging_dir() -> Path:
    from backend.app.core.config import settings

    path = Path(settings.base_dir) / "connector-uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _receive(request: Request, target: Path, cap: int) -> None:
    """Every chunk counted, whatever Content-Length said; written to our own file, never accumulated in memory."""
    import asyncio

    size = 0
    handle = await asyncio.to_thread(open, target, "wb")
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > cap:
                raise HTTPException(413, {"error": "upload_too_large", "message": UPLOAD_TOO_LARGE})
            await asyncio.to_thread(handle.write, chunk)
    finally:
        await asyncio.to_thread(handle.close)


async def _ingest(request: Request, actor: ConnectorActor, filename: str, cap: int):
    """The owned upload work: receive, then the library's own ingest, then visibility -- in a fresh session."""
    import asyncio

    from backend.app.api.routes.library import store_library_upload
    from backend.app.core import database
    from backend.app.models.library import LibraryFile
    from backend.app.schemas.library import FileUploadResponse

    staging = _staging_dir() / f"{uuid.uuid4().hex}.part"
    try:
        try:
            await _receive(request, staging, cap)
            content = await asyncio.to_thread(staging.read_bytes)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise HTTPException(507, {"error": "storage_full", "message": STORAGE_FULL}) from exc
            raise HTTPException(500, {"error": "upload_failed", "message": UPLOAD_FAILED}) from exc
        async with database.async_session() as db:
            try:
                result = await store_library_upload(db, filename=filename, content=content, target_folder=None)
            except HTTPException as exc:
                text = exc.detail if isinstance(exc.detail, str) else UPLOAD_FAILED
                if exc.status_code in (400, 415, 422):
                    raise HTTPException(422, {"error": "invalid_file", "message": text}) from exc
                raise HTTPException(exc.status_code, {"error": "upload_failed", "message": text}) from exc
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    raise HTTPException(507, {"error": "storage_full", "message": STORAGE_FULL}) from exc
                raise
            file_id, outcome, superseded = result.file.id, result.outcome, result.superseded_name
        del content
        # Visibility before anything about the row is answered (B2/B7): fresh rights, the row as it is now.
        async with database.async_session() as db:
            try:
                await submissions.fresh_key(db, actor, UPLOAD_PERMISSIONS)
            except ConnectorRefusal as refusal:
                raise _refused(refusal) from refusal
            row = (await db.execute(LibraryFile.active().where(LibraryFile.id == file_id))).scalar_one_or_none()
            if row is None:
                raise HTTPException(409, {"error": "exists_not_visible", "message": NOT_VISIBLE})
            return FileUploadResponse(
                id=row.id,
                filename=row.filename,
                file_type=row.file_type,
                file_tags=row.file_tags or [],
                file_size=row.file_size,
                thumbnail_path=row.thumbnail_path,
                duplicate_of=row.id if outcome != "created" else None,
                outcome=outcome,
                superseded_name=superseded,
                metadata=row.file_metadata,
            ).model_dump(mode="json") | {"file_hash": row.file_hash}
    finally:
        await asyncio.to_thread(staging.unlink, True)


@router.post("/files")
async def upload_file(
    request: Request,
    filename: str,
    _: None = RequirePermission(Permission.LIBRARY_UPLOAD, Permission.LIBRARY_READ_ALL),
    actor: ConnectorActor = Depends(connector_actor),
    db: AsyncSession = Depends(get_db),
):
    """A sliced 3MF into the library as raw bytes. The upload queues nothing; a refused print keeps the file."""
    from backend.app.utils.filename import InvalidFilenameError, validate_print_filename

    await _require_enabled(db)
    try:
        validate_print_filename(filename)
    except InvalidFilenameError as exc:
        raise HTTPException(422, {"error": "invalid_file", "message": str(exc)}) from exc
    if not filename.lower().endswith(".3mf"):
        raise HTTPException(422, {"error": "invalid_file", "message": NOT_A_3MF})
    cap = await _max_upload_bytes(db)
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > cap:
        raise HTTPException(413, {"error": "upload_too_large", "message": UPLOAD_TOO_LARGE})
    await db.commit()  # nothing of this request's session is held across the transfer
    try:
        return await connector_runtime.run_upload(lambda: _ingest(request, actor, filename, cap))
    except ConnectorRefusal as refusal:
        raise _refused(refusal) from refusal
