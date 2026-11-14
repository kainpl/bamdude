"""Polling endpoint for the in-memory slice-job dispatcher.

``POST /library/files/{id}/slice`` and ``POST /archives/{id}/slice`` return a
``job_id`` and a ``status_url`` pointing here. The frontend polls this until
status flips to ``completed`` or ``failed``.
"""

from fastapi import APIRouter, Depends, HTTPException, Response

from backend.app.core.auth import require_ownership_permission, require_permission
from backend.app.core.permissions import Permission
from backend.app.models.user import User
from backend.app.services.slice_dispatch import slice_dispatch

router = APIRouter(prefix="/slice-jobs", tags=["slice-jobs"])


def _visible_job(job_id: int, auth: tuple[User | None, bool]):
    user, can_read_all = auth
    job = slice_dispatch.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Slice job not found or expired")
    # Per-row scoping. Jobs started by API-key / auth-disabled callers have
    # owner_id=None and are visible only to READ_ALL pollers (fail-closed,
    # mirrors the ownerless-row rule). 404 not 403 to avoid job-id enumeration.
    if not can_read_all and (
        user is None or job.authority is None or job.authority.account_identity != user.account_identity
    ):
        raise HTTPException(status_code=404, detail="Slice job not found or expired")
    return job


@router.get("/{job_id}")
async def get_slice_job(
    job_id: int,
    # Job IDs are sequential integers and the body leaks source filenames plus
    # the resulting library_file_id / archive_id. Gate on the library read-split
    # (security #2), then scope per-row: a READ_OWN caller may only poll jobs
    # they started (SliceJob.owner_id).
    auth: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.LIBRARY_READ_ALL,
            Permission.LIBRARY_READ_OWN,
        )
    ),
):
    job = _visible_job(job_id, auth)
    body: dict = {
        "job_id": job.id,
        "status": job.status,
        "kind": job.kind,
        "source_id": job.source_id,
        "source_name": job.source_name,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        # Live progress fed by the sidecar's --pipe channel. Null when the
        # slicer hasn't emitted yet (early "Initializing" phase) or the
        # sidecar doesn't support progress (older versions).
        "progress": job.progress,
    }
    if job.status == "completed":
        body["result"] = job.result
    elif job.status == "failed":
        body["error_status"] = job.error_status
        body["error_detail"] = job.error_detail
    return body


@router.delete("/{job_id}", status_code=204)
async def cancel_slice_job(
    job_id: int,
    auth: tuple[User | None, bool] = Depends(
        require_ownership_permission(
            Permission.LIBRARY_READ_ALL,
            Permission.LIBRARY_READ_OWN,
        )
    ),
    _starter: User | None = Depends(require_permission(Permission.LIBRARY_UPLOAD)),
):
    """Stop an assembly job that is waiting or running. The same visibility rule as the read above.

    Only assembly jobs: stopping one ends its packing child with proof. A slice in a sidecar has no such
    stop, so it is refused rather than left running behind a job that says "canceled".
    """
    job = _visible_job(job_id, auth)
    if job.kind != "assembly":
        raise HTTPException(status_code=409, detail="This job cannot be stopped")
    if not slice_dispatch.cancel(job_id):
        raise HTTPException(status_code=409, detail="The job has already finished")
    return Response(status_code=204)
