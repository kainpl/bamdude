"""STEP thumbnail jobs: the explicit button, its status and its cancel (spec §5, plan A4).

POST asks ``library:update_all`` and takes exactly one mode — up to 64 file ids, or a folder / all-missing
selection read as a keyset page of at most min(64, Q) STEP files (the next page is the caller's explicit choice).
GET answers only receipts the caller may read: its own while it can still read the file, another actor's only
with both ``library:read_all`` and ``library:update_all`` — and never says whose. An unknown receipt and one the
caller may not read look the same. DELETE detaches one receipt; it never cancels another subscriber's work.
A receipt id or epoch is no right to anything: every answer is decided again here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import (
    APIKeyValidationFailure,
    RequestCredentials,
    is_api_key_token,
    library_name_scope,
    request_credentials,
    require_any_permission,
    require_permission,
    resolve_api_key_authority,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.library import LibraryFile
from backend.app.models.user import User
from backend.app.schemas.library import ThumbnailJobItem, ThumbnailJobRequest, ThumbnailJobResponse, ThumbnailJobStatus
from backend.app.services import library_preview_jobs as jobs_module
from backend.app.services.library_preview_jobs import (
    QUEUE_LIMIT,
    REQUEST_LIMIT,
    Authority,
    EpochExpired,
    Refusal,
    StepSource,
)
from backend.app.services.mesh_preview_types import CAD_TYPES

router = APIRouter(prefix="/library", tags=["library"])

JOBS_UNAVAILABLE = "STEP thumbnail jobs are not available right now"
JOB_EXPIRED = "This thumbnail job belongs to an earlier session; read the file again"
CANCEL_TOO_LATE = "The thumbnail is already being saved; it can no longer be cancelled"
RECEIPT_NOT_FOUND = "Thumbnail job not found"

_READ_ALL = require_permission(Permission.LIBRARY_READ_ALL)
_UPDATE_ALL = require_permission(Permission.LIBRARY_UPDATE_ALL)


def _service():
    service = jobs_module.get_library_preview_jobs()
    if service is None or not service.open:
        raise HTTPException(503, {"error": "unavailable", "message": JOBS_UNAVAILABLE})
    return service


def _known_service():
    """For reads and cancels: a stopped service still answers 410 for its own old epoch."""
    service = jobs_module.get_library_preview_jobs()
    if service is None:
        raise HTTPException(503, {"error": "unavailable", "message": JOBS_UNAVAILABLE})
    return service


async def request_authority(request: Request, user: User | None) -> Authority:
    """The caller as a receipt's actor: the signed-in user, or the API key itself (never its owner's JWT)."""
    from backend.app.core.auth import capture_request_authority

    capture = await capture_request_authority(request)
    if user is not None:
        return Authority.user(user.id, capture=capture)
    credential = request.headers.get("X-API-Key")
    if not credential:
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else None
        credential = token if is_api_key_token(token) else None
    if credential:
        try:  # the gate's own lookup for this request, cached on it: no second validation
            return Authority.api_key((await resolve_api_key_authority(request, credential)).key.id, capture=capture)
        except APIKeyValidationFailure:
            pass
    raise HTTPException(401, "Not authenticated")


async def _may_read(
    receipt, viewer: Authority, request: Request, db: AsyncSession, user, creds: RequestCredentials
) -> bool:
    if receipt.authority.same_actor(viewer):
        from backend.app.api.routes.library import file_name_visible

        row = await db.get(LibraryFile, receipt.execution.source.id)
        return file_name_visible(row, user, await library_name_scope(request, db, user))
    return await creds.allows(_READ_ALL) and await creds.allows(_UPDATE_ALL)


def _item(receipt) -> ThumbnailJobItem:
    return ThumbnailJobItem(**jobs_module.LibraryPreviewJobs.describe(receipt))


@router.post("/thumbnail-jobs", status_code=202, response_model=ThumbnailJobResponse)
async def create_thumbnail_jobs(
    body: ThumbnailJobRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_permission(Permission.LIBRARY_UPDATE_ALL)),
):
    service = _service()
    authority = await request_authority(request, user)
    query = LibraryFile.active().where(LibraryFile.file_type.in_(CAD_TYPES))
    next_after_id = None
    if body.file_ids is not None:
        wanted = list(dict.fromkeys(body.file_ids))[:REQUEST_LIMIT]
        rows = {row.id: row for row in (await db.execute(query.where(LibraryFile.id.in_(wanted)))).scalars()}
        refused_types = (
            await db.execute(
                select(LibraryFile.id).where(
                    LibraryFile.id.in_(wanted),
                    LibraryFile.deleted_at.is_(None),
                    LibraryFile.file_type.notin_(CAD_TYPES),
                )
            )
        ).scalars()
        not_step = set(refused_types)
        order = [file_id for file_id in wanted if file_id in rows or file_id in not_step]
    else:
        page = min(body.limit, REQUEST_LIMIT, QUEUE_LIMIT)
        if body.folder_id is not None:
            query = query.where(LibraryFile.folder_id == body.folder_id)
        if body.all_missing:
            query = query.where(LibraryFile.thumbnail_path.is_(None))
        selected = list(
            (await db.execute(query.where(LibraryFile.id > body.after_id).order_by(LibraryFile.id).limit(page + 1)))
            .scalars()
            .all()
        )
        if len(selected) > page:
            selected = selected[:page]
            next_after_id = selected[-1].id
        rows = {row.id: row for row in selected}
        not_step = set()
        order = list(rows)
    sources = {file_id: StepSource.capture(row) for file_id, row in rows.items()}
    await db.commit()  # selection only: no session is held by the jobs
    results = []
    for file_id in order:
        if file_id in not_step:
            results.append(ThumbnailJobItem(file_id=file_id, error_code="not_step"))  # a request for a non-STEP
            continue
        try:
            receipt = service.enqueue(sources[file_id], authority, "manual")
        except Refusal as refused:
            results.append(ThumbnailJobItem(file_id=file_id, error_code=refused.code))
            continue
        results.append(_item(receipt))
    return ThumbnailJobResponse(jobs_epoch=service.epoch, results=results, next_after_id=next_after_id)


@router.get("/thumbnail-jobs", response_model=ThumbnailJobStatus)
async def read_thumbnail_jobs(
    request: Request,
    epoch: str = Query(..., min_length=32, max_length=32),
    ids: list[str] = Query(..., max_length=REQUEST_LIMIT),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_any_permission(Permission.LIBRARY_READ_OWN, Permission.LIBRARY_READ_ALL)),
    creds: RequestCredentials = Depends(request_credentials),
):
    service = _known_service()
    viewer = await request_authority(request, user)
    receipts, missing = [], []
    for receipt_id in dict.fromkeys(ids):
        try:
            receipt = service.lookup(epoch, receipt_id)
        except EpochExpired:
            raise HTTPException(410, {"error": "preview_job_expired", "message": JOB_EXPIRED}) from None
        if receipt is not None and await _may_read(receipt, viewer, request, db, user, creds):
            receipts.append(_item(receipt))
        else:
            missing.append(receipt_id)
    return ThumbnailJobStatus(jobs_epoch=service.epoch, receipts=receipts, missing=missing)


@router.delete("/thumbnail-jobs/{receipt_id}", response_model=ThumbnailJobItem)
async def cancel_thumbnail_job(
    receipt_id: str,
    request: Request,
    epoch: str = Query(..., min_length=32, max_length=32),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(require_permission(Permission.LIBRARY_UPDATE_ALL)),
    creds: RequestCredentials = Depends(request_credentials),
):
    service = _known_service()
    viewer = await request_authority(request, user)
    try:
        receipt = service.lookup(epoch, receipt_id)
    except EpochExpired:
        raise HTTPException(410, {"error": "preview_job_expired", "message": JOB_EXPIRED}) from None
    if receipt is None or not await _may_read(receipt, viewer, request, db, user, creds):
        raise HTTPException(404, RECEIPT_NOT_FOUND)
    try:
        service.cancel(receipt)
    except Refusal:
        raise HTTPException(409, {"error": "cancel_too_late", "message": CANCEL_TOO_LATE}) from None
    return _item(receipt)
