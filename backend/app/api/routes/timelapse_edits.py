"""The timelapse editor's doors: start an edit, follow it, cancel it; the videos saved beside the original (G10 E).

``POST /archives/{id}/timelapse/process`` no longer waits for ffmpeg. It
answers 202 with a job; the dialog polls the job and may close meanwhile.

⚠️ **The edit's numbers travel in the query string and the music is the request
body itself.** A multipart form is parsed — and its file written to the system
temp directory, whatever its size — before a handler runs, so nothing in a
handler can cap it or choose where it lands. A raw body is read here chunk by
chunk into the job's own directory, every byte counted, after the caller was
authorised and the edit admitted.

Every job door asks three things: the route's permission (``archives:update_all``),
that the job is this very credential's, and — through the live-authority
coordinator's own read — that the caller still holds what the job was made
with on that very archive. A job that is somebody else's, gone, or of an
archive that is no longer that archive is the same 404.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from backend.app.api.routes.media_downloads import _archive_stamp, _capture, _prove_job
from backend.app.core.auth import RequirePermission, require_ownership_permission
from backend.app.core.config import settings
from backend.app.core.permissions import Permission
from backend.app.services import timelapse_editor
from backend.app.services.live_authority import LiveDescriptor
from backend.app.services.media_downloads import JobScope
from backend.app.services.timelapse_editor import EditRefused, timelapse_edit_runtime

logger = logging.getLogger(__name__)

router = APIRouter(tags=["archives"])

_NOT_FOUND = "Edit not found"
_UPDATE = RequirePermission(Permission.ARCHIVES_UPDATE_ALL)
_ARCHIVE_READ = Depends(require_ownership_permission(Permission.ARCHIVES_READ_ALL, Permission.ARCHIVES_READ_OWN))
_ARCHIVE_DELETE = Depends(require_ownership_permission(Permission.ARCHIVES_DELETE_ALL, Permission.ARCHIVES_DELETE_OWN))


def _refusal(exc: EditRefused) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(exc.status, {"error": exc.code, "message": exc.message}, headers=headers)


def _descriptor(request: Request, archive) -> LiveDescriptor:
    """What an edit's caller must keep while it runs: the credential and ``archives:update_all`` on this very archive."""
    return LiveDescriptor(
        _capture(request),
        (Permission.ARCHIVES_UPDATE_ALL.value,),
        resource_kind="archive",
        resource_id=archive.id,
        resource_stamp=_archive_stamp(archive),
        # Print history is not narrowed by a key's printer list.
        resource_printer_scope=False,
    )


async def _shown_video(db, archive_id: int):
    """The archive, the video it shows as a contained path, and that video's token — or the 404."""
    import asyncio

    from backend.app.services import archive_media
    from backend.app.services.archive import ArchiveService

    archive = await ArchiveService(db).get_archive(archive_id)
    if not archive or not (archive.timelapse_path or "").strip():
        raise HTTPException(404, "Timelapse not found")
    path = await asyncio.to_thread(archive_media.contained, settings.base_dir, archive.timelapse_path)
    if path is None or not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, "Timelapse file not found")
    return archive, path, await archive_media.primary_token(db, archive)


@router.post("/archives/{archive_id}/timelapse/process", status_code=202)
async def process_timelapse(
    archive_id: int,
    request: Request,
    trim_start: float = Query(0),
    trim_end: float | None = Query(None),
    speed: float = Query(1.0),
    save_mode: str = Query("replace"),
    audio_volume: float = Query(1.0),
    mute: bool = Query(False),
    audio_name: str | None = Query(None, max_length=255, description="The music file's name; its bytes are the body"),
    _=_UPDATE,
):
    """Start an edit of the archive's video: trim, speed, optional music. Answers at once; poll the job."""
    from backend.app.core import database
    from backend.app.services import queue_sources
    from backend.app.services.timelapse_processor import ProbeFailed, TimelapseProcessor

    epoch = queue_sources.current_epoch()
    async with database.async_session() as db:
        archive, source, token = await _shown_video(db, archive_id)
        scope = JobScope("archive", archive.id, _descriptor(request, archive))
    declared = request.headers.get("content-length") or ""
    if audio_name and declared.isdigit() and int(declared) > timelapse_editor.AUDIO_LIMIT:
        raise _refusal(EditRefused(413, "audio_too_large", "The music file is too large (100 MB at most)"))
    try:
        processor = TimelapseProcessor(source)
    except RuntimeError:
        raise HTTPException(
            503, {"error": "ffmpeg_missing", "message": "FFmpeg is not installed; videos cannot be edited"}
        ) from None
    try:
        info = await processor.get_info()
    except ProbeFailed:
        raise _refusal(EditRefused(422, "video_unreadable", "The video's length could not be read")) from None
    try:
        edit = timelapse_editor.validated_request(
            trim_start=trim_start,
            trim_end=trim_end,
            speed=speed,
            audio_volume=audio_volume,
            mute=mute,
            save_mode=save_mode,
            duration=info["duration"],
        )
        prefix = "timelapse" if edit.save_mode == timelapse_editor.SAVE_REPLACE else "timelapse-edit"
        # The place is taken here, in one synchronous step with the count; the music is staged after it.
        job = timelapse_edit_runtime.reserve(
            scope,
            archive=archive,
            token=token,
            epoch=epoch,
            source=source,
            info=info,
            request=edit,
            output_name=f"{prefix}-{int(time.time())}-{{job}}.mp4",
        )
    except EditRefused as exc:
        raise _refusal(exc) from None
    job.output_name = job.output_name.format(job=job.job_id[:12])
    try:
        await timelapse_edit_runtime.prepare_directory(job)
        if audio_name:
            await timelapse_editor.stage_audio(job, request.stream(), audio_name)
    except EditRefused as exc:
        await timelapse_edit_runtime.abandon(job)
        raise _refusal(exc) from None
    except BaseException:
        await timelapse_edit_runtime.abandon(job)
        raise
    return timelapse_edit_runtime.start(job).status()


async def _own_job(request: Request, archive_id: int, job_id: str):
    job = timelapse_edit_runtime.get(job_id, _capture(request), archive_id)
    if job is None:
        raise HTTPException(404, _NOT_FOUND)
    await _prove_job(request, job)
    return job


@router.get("/archives/{archive_id}/timelapse/edit-jobs")
async def list_timelapse_edit_jobs(archive_id: int, request: Request, _=_UPDATE):
    """The caller's own edits of this archive — what a reopened dialog picks up again."""
    shown = []
    for job in timelapse_edit_runtime.jobs_of(_capture(request), archive_id):
        try:
            await _prove_job(request, job)
        except HTTPException as exc:
            if exc.status_code not in (403, 404):
                raise  # authority that could not be read is not "no edits": the caller is told to ask again
            continue
        shown.append(job.status())
    return {"jobs": shown}


@router.get("/archives/{archive_id}/timelapse/edit-jobs/{job_id}")
async def get_timelapse_edit_job(archive_id: int, job_id: str, request: Request, _=_UPDATE):
    return (await _own_job(request, archive_id, job_id)).status()


@router.delete("/archives/{archive_id}/timelapse/edit-jobs/{job_id}")
async def cancel_timelapse_edit_job(archive_id: int, job_id: str, request: Request, _=_UPDATE):
    """Ask the edit to stop. It reads ``cancelled`` only once its encoder has ended and its files are gone."""
    job = await _own_job(request, archive_id, job_id)
    timelapse_edit_runtime.request_cancel(job)
    return job.status()


# -- videos saved beside the original ("save as new") ------------------------------------


@router.get("/archives/{archive_id}/timelapse/variants")
async def list_timelapse_variants(archive_id: int, auth_result=_ARCHIVE_READ):
    """The videos the editor saved beside the one the archive shows."""
    from backend.app.api.routes.media_downloads import _visible_archive, variant_videos
    from backend.app.core import database

    async with database.async_session() as db:
        archive = await _visible_archive(db, archive_id, auth_result)
        videos = await variant_videos(db, archive)
    return {"variants": [{"asset": v["asset"], "name": v["name"], "size": v["size"]} for v in videos]}


@router.delete("/archives/{archive_id}/timelapse/variants/{asset}")
async def delete_timelapse_variant(archive_id: int, asset: str, request: Request, auth_result=_ARCHIVE_DELETE):
    """Remove one saved video. The row goes in the seam's transaction, its file after the commit."""
    from backend.app.api.routes.archives import _announce_media, _media_authority, _refuse_media
    from backend.app.api.routes.media_downloads import _visible_archive
    from backend.app.core import database
    from backend.app.services import archive_media, queue_sources

    authority = await _media_authority(request)
    epoch = queue_sources.current_epoch()
    _user, can_delete_all = auth_result
    async with database.async_session() as db:
        archive = await _visible_archive(db, archive_id, auth_result)
        outcome = await archive_media.remove_variant(
            db,
            archive_id,
            asset_uuid=asset,
            base_dir=settings.base_dir,
            expected_identity=archive.recovery_identity,
            expected_epoch=epoch,
            authority=authority,
            permissions=((Permission.ARCHIVES_DELETE_ALL if can_delete_all else Permission.ARCHIVES_DELETE_OWN).value,),
        )
    if not outcome.ok:
        if outcome.refusal == archive_media.REFUSED_SOURCE_CHANGED:
            raise HTTPException(404, "The video is no longer saved with this archive")
        _refuse_media(outcome.refusal)
    await _announce_media(archive_id)
    return {"status": "deleted"}
