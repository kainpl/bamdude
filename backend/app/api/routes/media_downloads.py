"""Download jobs over HTTP (G10 D, spec r4 §8).

A download is prepared by a job (``services/media_downloads``) and leaves by
one of two doors, both behind the live-media final gate:

* ``GET /media-downloads/{grant}/{filename}`` — the browser's native download.
  The page mints a one-time grant for a READY job
  (``POST …/download-jobs/{job_id}/download-token``, browser sessions only) and
  navigates to this URL; no bytes pass through JavaScript.
* ``GET …/download-jobs/{job_id}/artifact`` — the same artifact for a client
  that sends headers (an API key). The printer comes from the path and must be
  the job's; the centralized key gate checks it against the key's printer list.

Every door asks the caller's CURRENT rights (the route gate) and finds the job
only by the very credential that made it, in its own scope — somebody else's,
another session's of the same account and a missing job are the same 404.

A job carries the requirements it was made under (``JobScope.descriptor``): the
permissions, the printer and its identity, the archive and its incarnation. A
route gate knows only its own path — an archive's route asks for the archive,
not for ``printers:files`` on the printer a recording came from. So EVERY door
of a job puts the job's own requirements on the request's current credential
(``_job_requirements``): status, list, cancel and mint prove them through the
live coordinator before they answer (``_prove_job``), and both artifact doors
send the body under them, so a caller who lost the right to the printer gets
neither the file nor its state. Both doors always answer the whole file (200,
``Accept-Ranges: none``): a one-time grant cannot honour a resume.

A failure on the native door before the headers is ``download-failed.txt`` with
a safe sentence, not JSON saved under the archive's name.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import stat
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.core.api_key_scope import in_key_scope, printer_refusal
from backend.app.core.auth import (
    MEDIA_DOWNLOAD_GRANT_SECONDS,
    RequestCredentials,
    RequirePermission,
    create_media_download_grant,
    refused,
    request_credentials,
    require_media_download_grant,
    require_media_ownership_permission,
    require_media_permission,
    require_ownership_permission,
    require_permission,
)
from backend.app.core.config import settings
from backend.app.core.permissions import Permission
from backend.app.i18n import current_language
from backend.app.i18n.api_errors import translate_detail
from backend.app.services import media_downloads
from backend.app.services.live_authority import LiveDescriptor, live_authority
from backend.app.services.live_media import OwnedMediaResponse, WholeFileResponse
from backend.app.services.media_downloads import (
    MAX_PATHS,
    DownloadJob,
    DownloadRefused,
    JobScope,
    file_identity,
    media_download_runtime,
    remote_source,
    same_credential,
)
from backend.app.utils.http import build_content_disposition

logger = logging.getLogger(__name__)
router = APIRouter(tags=["downloads"])

_NOT_FOUND = "Download not found"
_MAX_PATH_LENGTH = 1024


class NativeDownloadFailure(HTTPException):
    """A refusal on the native download door: answered as a small text file, never as JSON."""


def install(app) -> None:
    """Answer a failed native download with ``download-failed.txt``.

    The browser is saving a file: JSON would land on disk under the archive's
    name. The sentence is the refusal's own, in the system language; it names
    no path and no credential.
    """

    async def handler(_request: Request, exc: NativeDownloadFailure):
        detail = translate_detail(exc.detail, current_language())
        if isinstance(detail, dict):
            detail = detail.get("message")
        text = detail if isinstance(detail, str) else "The download could not be started."
        headers = {
            "Content-Disposition": 'attachment; filename="download-failed.txt"',
            "Cache-Control": "no-store",
        }
        for name in ("Retry-After", "WWW-Authenticate", "Allow"):
            if exc.headers and name in exc.headers:
                headers[name] = exc.headers[name]
        return PlainTextResponse(text + "\n", status_code=exc.status_code, headers=headers)

    app.add_exception_handler(NativeDownloadFailure, handler)


class _NativeDelivery(OwnedMediaResponse):
    """The native door's answer: an admission refusal before the headers becomes the text file too."""

    async def __call__(self, scope, receive, send):
        started = False

        async def tracking(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await super().__call__(scope, receive, tracking)
        except HTTPException as exc:
            if started or isinstance(exc, NativeDownloadFailure):
                raise
            raise NativeDownloadFailure(exc.status_code, exc.detail, exc.headers) from None


# -- shared ------------------------------------------------------------------------------


def _refusal(exc: DownloadRefused) -> HTTPException:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
    return HTTPException(exc.status, {"error": exc.code, "message": exc.message}, headers=headers)


def _capture(request: Request):
    descriptor = getattr(request.state, "live_descriptor", None)
    if descriptor is None:
        raise refused()
    return descriptor.capture


def _job_or_404(request: Request, job_id: str, kind: str, resource_id: int) -> DownloadJob:
    job = media_download_runtime.get(job_id, _capture(request), kind, resource_id)
    if job is None:
        raise HTTPException(404, _NOT_FOUND)
    return job


def _job_requirements(request: Request, job: DownloadJob) -> LiveDescriptor:
    """The request's CURRENT credential under the requirements the job was made with.

    The credential, its expiry and its key binding are this request's; what it
    must hold — permissions, the printer and its identity, the resource and its
    incarnation — is the job's. A route gate cannot supply that: it reads its
    own path, and an archive's path names no printer.
    """
    current = getattr(request.state, "live_descriptor", None)
    if current is None:
        raise refused()
    wanted = job.scope.descriptor
    return replace(
        current,
        permissions=wanted.permissions,
        any_permissions=wanted.any_permissions,
        printer_id=wanted.printer_id,
        printer_identity=wanted.printer_identity,
        resource_kind=wanted.resource_kind,
        resource_id=wanted.resource_id,
        resource_stamp=wanted.resource_stamp,
        resource_printer_scope=wanted.resource_printer_scope,
    )


def _bind_job(request: Request, job: DownloadJob) -> None:
    """An artifact door: the body is admitted and sent under the job's own requirements."""
    request.state.live_descriptor = _job_requirements(request, job)
    # Nothing the route's path captured may loosen or replace them.
    request.state.live_media_binding = {}


async def _prove_job(request: Request, job: DownloadJob) -> None:
    """A door that sends no file: prove the job's requirements now, through the coordinator's own read.

    The same check the artifact's body passes before its headers — a fresh
    durable read of the credential, the permissions, the printer's identity and
    the archive's incarnation — so a job's state, its cancel and its one-time
    URL are refused to a caller who no longer holds what the job needs: 403, or
    503 when authority cannot be read.

    The coordinator answers 401 both for a dead credential and for a target that
    is no longer the one the job named (a printer or an archive that took the
    id). This request's credential was accepted by the route's gate a moment
    ago, and the browser treats a 401 from a JSON route as "signed out" — so
    here that answer is the job's: it is gone, 404.
    """
    handle = live_authority.register(_job_requirements(request, job))
    try:
        await live_authority.admit(handle)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise HTTPException(404, _NOT_FOUND) from None
        raise
    finally:
        live_authority.release(handle)


async def _own_job(request: Request, job_id: str, kind: str, resource_id: int) -> DownloadJob:
    job = _job_or_404(request, job_id, kind, resource_id)
    await _prove_job(request, job)
    return job


async def _own_jobs(request: Request, kind: str, resource_id: int) -> list[dict]:
    """The caller's jobs in this scope whose requirements it still meets.

    A job is left out only on a PROVEN refusal (403, or the 404 of a target that
    is gone). Authority that could not be read just now (503) is not a verdict:
    the whole list answers 503 with its ``Retry-After``, because an empty list
    would tell the operator that a download which is still there has vanished.
    """
    verdicts: dict = {}
    shown = []
    for job in media_download_runtime.jobs_of(_capture(request), kind, resource_id):
        wanted = _job_requirements(request, job)
        if wanted not in verdicts:
            try:
                await _prove_job(request, job)
                verdicts[wanted] = True
            except HTTPException as exc:
                if exc.status_code not in (403, 404):
                    raise
                verdicts[wanted] = False
        if verdicts[wanted]:
            shown.append(job.status())
    return shown


def _headers_only(request: Request) -> None:
    """An artifact door takes its credential from the headers and from nowhere else.

    The media dependency behind it would also read ``?token=``; with a header
    AND a token in one request the principal must not be a matter of order.
    """
    if "token" in request.query_params:
        raise HTTPException(400, "This download takes its credentials from the request headers")


def _media_type(job: DownloadJob) -> str:
    if job.as_zip:
        return "application/zip"
    from mimetypes import guess_type

    return guess_type(job.filename)[0] or "application/octet-stream"


def _artifact_response(job: DownloadJob, *, accept_partial: bool) -> WholeFileResponse:
    """The ready artifact as a whole-file answer holding a reader lease, or the refusal."""
    if job.phase != media_downloads.PHASE_READY:
        raise HTTPException(409, {"error": "download_not_ready", "message": "The download is not ready"})
    if job.partial and not accept_partial:
        raise HTTPException(
            409,
            {
                "error": "partial_not_confirmed",
                "message": "Some files could not be downloaded; confirm to take the rest",
            },
        )
    lease = media_download_runtime.lease(job)
    if lease is None:
        raise HTTPException(404, _NOT_FOUND)
    return WholeFileResponse(
        job.artifact,
        media_type=_media_type(job),
        headers={
            "Content-Disposition": build_content_disposition(job.filename),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        lease=lease,
    )


def _cancel_or_discard(job: DownloadJob) -> dict:
    """``DELETE``: an active job is asked to stop (it reads ``cancelled`` only once it has); a finished one is dropped."""
    if job.active:
        media_download_runtime.request_cancel(job)
    else:
        media_download_runtime.discard_soon(job, forget=True)
    return job.status()


async def _mint(request: Request, job: DownloadJob, *, accept_partial: bool) -> dict:
    if job.phase != media_downloads.PHASE_READY:
        raise HTTPException(409, {"error": "download_not_ready", "message": "The download is not ready"})
    if job.partial and not accept_partial:
        raise HTTPException(
            409,
            {
                "error": "partial_not_confirmed",
                "message": "Some files could not be downloaded; confirm to take the rest",
            },
        )
    wanted = job.scope.descriptor
    grant = await create_media_download_grant(
        authority=_capture(request),
        bound_to=f"{job.job_id}:{media_download_runtime.generation}",
        required_permissions=wanted.permissions,
        printer_id=wanted.printer_id,
    )
    job.touched = time.monotonic()
    return {
        "url": f"{settings.api_prefix}/media-downloads/{grant}/{quote(job.filename, safe='')}",
        "expires_in": MEDIA_DOWNLOAD_GRANT_SECONDS,
    }


class TokenRequest(BaseModel):
    accept_partial: bool = False


# -- printer files -----------------------------------------------------------------------


def _is_file_path(path) -> bool:
    """An absolute path of one file on a printer: no traversal, no control characters, bounded."""
    return (
        isinstance(path, str)
        and path.startswith("/")
        and len(path) <= _MAX_PATH_LENGTH
        and not path.endswith("/")
        and not any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
        and not any(part in ("..", ".") for part in path.split("/"))
    )


class PrinterDownloadRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=MAX_PATHS)
    storage: str | None = None
    #: What the listing said each file weighs — advice for the disk reservation, never trusted as a limit.
    sizes: dict[str, int] | None = None
    as_zip: bool = False
    filename: str | None = Field(default=None, max_length=255)

    @field_validator("paths")
    @classmethod
    def _paths(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for path in value:
            if not _is_file_path(path):
                raise ValueError("a path must be an absolute file path on the printer")
            if path in seen:
                raise ValueError("a path is listed twice")
            seen.add(path)
        return value


def _printer_descriptor(request: Request, printer) -> LiveDescriptor:
    """What a printer job's caller must keep while it runs: the credential, ``printers:files`` and this very printer."""
    return LiveDescriptor(
        _capture(request),
        (Permission.PRINTERS_FILES.value,),
        printer_id=printer.id,
        printer_identity=printer.connector_identity,
    )


async def create_printer_job(request: Request, printer_id: int, body: PrinterDownloadRequest, *, hidden: bool = False):
    """Build and admit one job over a printer's files. Shared with the compatibility downloads."""
    from backend.app.api.routes.printers import _load_printer_or_404, _resolve_storage
    from backend.app.services.printer_files.factory import transport_for
    from backend.app.services.printer_manager import printer_manager

    printer = await _load_printer_or_404(printer_id)
    storage = _resolve_storage(body.storage, printer.model, printer_manager.get_status(printer_id))
    # One transport for the whole job, built from the row as it is now: the endpoint and the
    # access code are captured here, and a later edit of the printer never moves the job.
    transport = transport_for(printer, storage)
    sizes = body.sizes or {}
    sources = [
        remote_source(transport, path, advisory_size=size if isinstance(size := sizes.get(path), int) else None)
        for path in body.paths
    ]
    scope = JobScope("printer", printer.id, _printer_descriptor(request, printer))
    try:
        return await media_download_runtime.create(
            scope, sources, as_zip=body.as_zip, filename=body.filename, hidden=hidden
        )
    except DownloadRefused as exc:
        raise _refusal(exc) from None


@router.post("/printers/{printer_id}/files/download-jobs", status_code=202)
async def create_printer_download_job(
    printer_id: int,
    body: PrinterDownloadRequest,
    request: Request,
    _=RequirePermission(Permission.PRINTERS_FILES),
):
    """Start preparing a download of this printer's files. Answers at once; poll the job."""
    return (await create_printer_job(request, printer_id, body)).status()


@router.get("/printers/{printer_id}/files/download-jobs")
async def list_printer_download_jobs(printer_id: int, request: Request, _=RequirePermission(Permission.PRINTERS_FILES)):
    """The caller's own jobs for this printer — what a reopened dialog picks up again."""
    return {"jobs": await _own_jobs(request, "printer", printer_id)}


@router.get("/printers/{printer_id}/files/download-jobs/{job_id}")
async def get_printer_download_job(
    printer_id: int, job_id: str, request: Request, _=RequirePermission(Permission.PRINTERS_FILES)
):
    return (await _own_job(request, job_id, "printer", printer_id)).status()


@router.delete("/printers/{printer_id}/files/download-jobs/{job_id}")
async def cancel_printer_download_job(
    printer_id: int, job_id: str, request: Request, _=RequirePermission(Permission.PRINTERS_FILES)
):
    return _cancel_or_discard(await _own_job(request, job_id, "printer", printer_id))


@router.post("/printers/{printer_id}/files/download-jobs/{job_id}/download-token")
async def mint_printer_download_token(
    printer_id: int,
    job_id: str,
    request: Request,
    body: TokenRequest | None = None,
    _=RequirePermission(Permission.PRINTERS_FILES),
):
    """A one-time URL for the browser to save the ready result. Browser sessions only."""
    job = await _own_job(request, job_id, "printer", printer_id)
    return await _mint(request, job, accept_partial=bool(body and body.accept_partial))


@router.get("/printers/{printer_id}/files/download-jobs/{job_id}/artifact", dependencies=[Depends(_headers_only)])
async def get_printer_download_artifact(
    printer_id: int,
    job_id: str,
    request: Request,
    accept_partial: bool = False,
    _=Depends(require_media_permission(Permission.PRINTERS_FILES)),
):
    """The ready result for a client that sends headers (an API key). Always the whole file.

    The media-route dependency, like every other route behind the final delivery
    gate; its header path is the ordinary permission gate, key scopes and a
    key's printer list included. No pattern opens this path to a header-less
    request, and a request that also carries ``?token=`` is refused, so the
    principal is the header's and nothing else's. The body is admitted under
    the job's own requirements — this very printer, by its identity.
    """
    job = _job_or_404(request, job_id, "printer", printer_id)
    _bind_job(request, job)
    return _artifact_response(job, accept_partial=accept_partial)


# -- the native door ---------------------------------------------------------------------


def _grant_requirements(bound_to: str | None, capture):
    """What a grant opens, asked before it is spent: the job must be this runtime's, ready and its minter's."""
    job_id, _, generation = (bound_to or "").partition(":")
    job = media_download_runtime.find(job_id)
    if (
        job is None
        or generation != media_download_runtime.generation
        or job.phase != media_downloads.PHASE_READY
        or capture.kind != "session"
        or not same_credential(job.capture, capture)
    ):
        raise HTTPException(404, _NOT_FOUND)
    descriptor = job.scope.descriptor
    return descriptor.permissions, descriptor.printer_id


@router.get("/media-downloads/{grant}/{filename}")
async def redeem_download_grant(
    grant: str,
    filename: str,
    request: Request,
    redeemed=Depends(require_media_download_grant(_grant_requirements, NativeDownloadFailure)),
):
    """The browser's native download of a ready job. One use; HEAD is not served (405)."""
    job_id = (redeemed or "").partition(":")[0]
    job = media_download_runtime.find(job_id)
    if job is None or filename != job.filename:
        raise NativeDownloadFailure(404, _NOT_FOUND)
    # The download runs under the job's own requirements, on the session that minted the grant.
    _bind_job(request, job)
    try:
        # The partial result was confirmed when the grant was minted.
        response = _artifact_response(job, accept_partial=True)
    except HTTPException as exc:
        raise NativeDownloadFailure(exc.status_code, exc.detail, exc.headers) from None
    return _NativeDelivery(response, request)


# -- an archive's videos -----------------------------------------------------------------
#
# The chooser of an archive offers three kinds of file (spec r4 §8.3): the video already
# attached (local), the timelapse recordings on the archive's printer, and the chunks its
# onboard camera recorded to the card (``/ipcam``). The local one is the archive's own
# and follows the archive's ownership rule alone — no printer right, no connection. The
# two remote kinds need ``printers:files`` and, for an API key, the printer in its list.

_ARCHIVE_READ = Depends(require_ownership_permission(Permission.ARCHIVES_READ_ALL, Permission.ARCHIVES_READ_OWN))
_ARCHIVE_ARTIFACT_READ = Depends(
    require_media_ownership_permission(Permission.ARCHIVES_READ_ALL, Permission.ARCHIVES_READ_OWN)
)
_FILES_GATE = require_permission(Permission.PRINTERS_FILES)

CAMERA_DIRECTORY = "/ipcam"
CAMERA_PREFIX = "ipcam-record."
#: How long the chooser waits for the printer before it says "slow" (the listing goes on).
_SOURCES_WAIT_SECONDS = 20.0
DEFAULT_MARGIN_MINUTES = 10
#: An archive still marked as printing does not open a window over everything recorded since.
_OPEN_WINDOW = timedelta(hours=48)

_timelapse_listings: dict[int, asyncio.Task] = {}


class ArchiveRemoteFile(BaseModel):
    #: Must be the archive's own printer. Named so the API-key gate reads it: a key
    #: without that printer is refused before the route runs.
    printer_id: int
    storage: Literal["external", "internal"]
    path: str = Field(max_length=_MAX_PATH_LENGTH)
    size: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not _is_file_path(value):
            raise ValueError("a path must be an absolute file path on the printer")
        return value


class ArchiveDownloadRequest(BaseModel):
    #: ``asset`` ids from the chooser's local list.
    assets: list[str] = Field(default_factory=list, max_length=MAX_PATHS)
    remote: list[ArchiveRemoteFile] = Field(default_factory=list, max_length=MAX_PATHS)
    as_zip: bool = False
    filename: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _selection(self):
        if not self.assets and not self.remote:
            raise ValueError("nothing selected")
        if len(self.assets) + len(self.remote) > MAX_PATHS:
            raise ValueError("too many files")
        if len(set(self.assets)) != len(self.assets):
            raise ValueError("an asset is listed twice")
        keys = [(item.storage, item.path) for item in self.remote]
        if len(set(keys)) != len(keys):
            raise ValueError("a path is listed twice")
        return self


async def _visible_archive(db, archive_id: int, auth_result):
    from backend.app.api.routes.archives import _ensure_archive_visible
    from backend.app.services.archive import ArchiveService

    user, can_read_all = auth_result
    return _ensure_archive_visible(await ArchiveService(db).get_archive(archive_id), user, can_read_all)


def local_asset_id(token: str) -> str:
    """The chooser's name for "the video this archive shows now".

    Derived from the seam's own token (``archive_media.primary_token``) — which
    publication made the video, or that it is a video from before the registry
    — so it names a VERSION: after a replace or a remove the old id opens
    nothing. Hashed, because a legacy token spells a path on this server.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


async def _local_videos(db, archive) -> list[dict]:
    """The video the archive shows now: ``asset``, ``name``, ``size``, ``path``, ``identity``.

    The archive's pointer is what it shows — the same thing its own video route
    serves — whether or not a registry row describes it yet (a video from before
    the registry has none). Read only: nothing is written and no stale row is
    brought back; the seam reconciles the registry when it next writes.
    """
    from backend.app.services.archive_media import contained, primary_token

    pointer = (archive.timelapse_path or "").strip()
    if not pointer:
        return []
    token = await primary_token(db, archive)

    def look():
        path = contained(settings.base_dir, pointer)
        if path is None:
            return None
        try:
            info = path.stat()
        except OSError:
            return None
        return (path, info) if stat.S_ISREG(info.st_mode) else None

    found = await asyncio.to_thread(look)
    if found is None:
        return []
    path, info = found
    suffix = path.suffix.lower() if path.suffix.lower() in (".mp4", ".avi", ".mkv") else ".mp4"
    return [
        {
            "asset": local_asset_id(token),
            "name": f"{archive.print_name or 'timelapse'}{suffix}",
            "size": info.st_size,
            "path": path,
            "identity": file_identity(info),
        }
    ]


async def variant_videos(db, archive) -> list[dict]:
    """The videos the editor saved BESIDE the one the archive shows (G10 E): same shape as :func:`_local_videos`.

    A variant is named by its registry row's ``asset_uuid``. A row whose file is
    gone or whose stored path leaves the data directory is not offered.
    """
    from backend.app.services.archive_media import contained, variants_of

    rows = await variants_of(db, archive.id, archive.recovery_identity)

    def look(relative_path: str):
        path = contained(settings.base_dir, relative_path)
        if path is None:
            return None
        try:
            info = path.stat()
        except OSError:
            return None
        return (path, info) if stat.S_ISREG(info.st_mode) else None

    videos = []
    for number, row in enumerate(rows, start=1):
        found = await asyncio.to_thread(look, row.relative_path)
        if found is None:
            continue
        path, info = found
        videos.append(
            {
                "asset": row.asset_uuid,
                "name": f"{archive.print_name or 'timelapse'} (edit {number}){path.suffix.lower() or '.mp4'}",
                "size": info.st_size,
                "path": path,
                "identity": file_identity(info),
                "variant": True,
            }
        )
    return videos


def _still_saved(archive_id: int, archive_identity, asset: str):
    """Is the saved video ``asset`` still that very archive's? The variant's counterpart of :func:`_still_shown`."""

    async def check() -> bool:
        from backend.app.core import database
        from backend.app.services.archive_media import variants_of

        try:
            async with database.async_session() as db:
                return any(row.asset_uuid == asset for row in await variants_of(db, archive_id, archive_identity))
        except Exception as exc:  # noqa: BLE001 — unreadable is not "still saved"
            logger.warning(
                "[DOWNLOADS] an archive's saved video could not be confirmed, left out as unconfirmed (%s)",
                type(exc).__name__,
            )
            return False

    return check


def _still_shown(archive_id: int, archive_identity, asset: str):
    """Is ``asset`` still the video that very archive shows? Asked when the copy starts and when it ends."""

    async def check() -> bool:
        from sqlalchemy import select

        from backend.app.core import database
        from backend.app.models.archive import PrintArchive
        from backend.app.services.archive_media import primary_token

        try:
            async with database.async_session() as db:
                archive = (
                    await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
                ).scalar_one_or_none()
                if archive is None or archive.recovery_identity != archive_identity:
                    return False
                return local_asset_id(await primary_token(db, archive)) == asset
        except Exception as exc:  # noqa: BLE001 — unreadable is not "still shown"
            # The file is left out either way; the log says this was not a proven replacement.
            logger.warning(
                "[DOWNLOADS] an archive's video could not be confirmed, left out as unconfirmed (%s)",
                type(exc).__name__,
            )
            return False

    return check


async def _archive_printer(db, archive):
    from sqlalchemy import select

    from backend.app.models.printer import Printer

    if not archive.printer_id:
        return None
    return (await db.execute(select(Printer).where(Printer.id == archive.printer_id))).scalar_one_or_none()


def _archive_stamp(archive) -> tuple:
    # The same incarnation the timelapse route captures for its live proof.
    return (
        getattr(archive, "recovery_identity", None) or getattr(archive, "connector_identity", None),
        archive.created_at,
    )


def _archive_descriptor(request: Request, archive, can_read_all: bool, printer=None) -> LiveDescriptor:
    """What an archive job's caller must keep: the archive's read rule on this very archive, and — only when
    the job reads the printer — ``printers:files`` on this very printer."""
    read = Permission.ARCHIVES_READ_ALL if can_read_all else Permission.ARCHIVES_READ_OWN
    permissions = (read.value,) if printer is None else (read.value, Permission.PRINTERS_FILES.value)
    return LiveDescriptor(
        _capture(request),
        permissions,
        printer_id=None if printer is None else printer.id,
        printer_identity=None if printer is None else printer.connector_identity,
        resource_kind="archive",
        resource_id=archive.id,
        resource_stamp=_archive_stamp(archive),
        # Print history is not narrowed by a key's printer list; reading the printer is, above.
        resource_printer_scope=False,
    )


@router.post("/archives/{archive_id}/media-download-jobs", status_code=202)
async def create_archive_download_job(
    archive_id: int,
    body: ArchiveDownloadRequest,
    request: Request,
    auth_result=_ARCHIVE_READ,
    creds: RequestCredentials = Depends(request_credentials),
):
    """Start preparing a download of this archive's video and/or recordings on its printer."""
    from backend.app.core import database
    from backend.app.services.printer_files.factory import transport_for

    async with database.async_session() as db:
        archive = await _visible_archive(db, archive_id, auth_result)
        local = {
            video["asset"]: video for video in [*await _local_videos(db, archive), *await variant_videos(db, archive)]
        }
        printer = await _archive_printer(db, archive) if body.remote else None
    sources = []
    for asset in body.assets:
        video = local.get(asset)
        if video is None:
            raise HTTPException(404, "The video is no longer attached to this archive")
        sources.append(
            media_downloads.local_source(
                video["path"],
                name=video["name"],
                advisory_size=video["size"],
                # The job may wait; what it copies must be the file chosen here, still shown then.
                identity=video["identity"],
                still_shown=(_still_saved if video.get("variant") else _still_shown)(
                    archive.id, archive.recovery_identity, asset
                ),
            )
        )
    if body.remote:
        # A second gate, the canonical one: the caller's own right to read printer files.
        await creds.check(_FILES_GATE)
        if any(item.printer_id != archive.printer_id for item in body.remote):
            raise HTTPException(400, "A recording must be on this archive's printer")
        if not in_key_scope(request, archive.printer_id):
            raise printer_refusal(archive.printer_id)
        if printer is None:
            raise HTTPException(404, "Printer not found")
        transports: dict[str, object] = {}
        for item in body.remote:
            transport = transports.setdefault(item.storage, transport_for(printer, item.storage))
            sources.append(remote_source(transport, item.path, advisory_size=item.size))
    scope = JobScope("archive", archive.id, _archive_descriptor(request, archive, auth_result[1], printer))
    filename = body.filename or (f"{archive.print_name or 'archive'}.zip" if len(sources) > 1 or body.as_zip else None)
    try:
        job = await media_download_runtime.create(scope, sources, as_zip=body.as_zip, filename=filename)
    except DownloadRefused as exc:
        raise _refusal(exc) from None
    return job.status()


async def _archive_job(request: Request, archive_id: int, job_id: str, auth_result, *, prove: bool = True):
    """The caller's own job for an archive they may still see, its own requirements proven.

    ``prove=False`` only for the artifact door, whose body is admitted under the
    same requirements before its headers.
    """
    from backend.app.core import database

    async with database.async_session() as db:
        await _visible_archive(db, archive_id, auth_result)
    job = _job_or_404(request, job_id, "archive", archive_id)
    if prove:
        await _prove_job(request, job)
    return job


@router.get("/archives/{archive_id}/media-download-jobs")
async def list_archive_download_jobs(archive_id: int, request: Request, auth_result=_ARCHIVE_READ):
    from backend.app.core import database

    async with database.async_session() as db:
        await _visible_archive(db, archive_id, auth_result)
    return {"jobs": await _own_jobs(request, "archive", archive_id)}


@router.get("/archives/{archive_id}/media-download-jobs/{job_id}")
async def get_archive_download_job(archive_id: int, job_id: str, request: Request, auth_result=_ARCHIVE_READ):
    return (await _archive_job(request, archive_id, job_id, auth_result)).status()


@router.delete("/archives/{archive_id}/media-download-jobs/{job_id}")
async def cancel_archive_download_job(archive_id: int, job_id: str, request: Request, auth_result=_ARCHIVE_READ):
    return _cancel_or_discard(await _archive_job(request, archive_id, job_id, auth_result))


@router.post("/archives/{archive_id}/media-download-jobs/{job_id}/download-token")
async def mint_archive_download_token(
    archive_id: int,
    job_id: str,
    request: Request,
    body: TokenRequest | None = None,
    auth_result=_ARCHIVE_READ,
):
    job = await _archive_job(request, archive_id, job_id, auth_result)
    return await _mint(request, job, accept_partial=bool(body and body.accept_partial))


@router.get("/archives/{archive_id}/media-download-jobs/{job_id}/artifact", dependencies=[Depends(_headers_only)])
async def get_archive_download_artifact(
    archive_id: int,
    job_id: str,
    request: Request,
    accept_partial: bool = False,
    auth_result=_ARCHIVE_ARTIFACT_READ,
):
    """The ready result for a client that sends headers. Always the whole file.

    This route's gate asks for the archive only. A job that read the archive's
    PRINTER was made under more — ``printers:files`` on that very printer — and
    the body is admitted under all of it (``_bind_job``): a caller who lost the
    printer since gets no recording. A job of the attached video alone carries
    the archive's rule and nothing of the printer's, so history stays readable
    without the printer, a key's printer list included.
    """
    job = await _archive_job(request, archive_id, job_id, auth_result, prove=False)
    _bind_job(request, job)
    return _artifact_response(job, accept_partial=accept_partial)


# -- the chooser's data ------------------------------------------------------------------


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value


def archive_window(archive, margin: timedelta, now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """The stretch of time a file of this print can have been written in, or ``None`` without a start."""
    start = _naive_utc(archive.started_at)
    if start is None:
        return None
    now = _naive_utc(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    end = _naive_utc(archive.completed_at) or min(now, start + _OPEN_WINDOW)
    return start - margin, max(start, end) + margin


def time_overlap(mtime, quality, window, offset: timedelta | None) -> tuple[bool | None, bool]:
    """``(overlaps, assumed)`` for one remote file.

    Only an ``exact`` time (the tunnel's epoch) is compared as it is. An FTP
    listing's time is a wall clock of unknown zone: without the operator's
    offset the answer is ``None`` — not a guess — and with it the comparison is
    made and marked ``assumed``. The offset is the dialog's own; it changes no
    setting and no automatic attach.

    A ``date_only`` time (an older file: the listing gives its day, no hour) is
    a whole day, and an offset does not tell which hour of it. So it can only
    rule a file OUT — its day, shifted, does not touch the window — and a day
    that does touch the window stays unknown, never a match.
    """
    stamp = _naive_utc(mtime) if isinstance(mtime, datetime) else None
    if stamp is None or window is None:
        return None, False
    if quality == "exact":
        return window[0] <= stamp <= window[1], False
    if offset is None:
        return None, False
    if quality == "date_only":
        day = stamp.replace(hour=0, minute=0, second=0, microsecond=0) - offset
        if day + timedelta(days=1) <= window[0] or day > window[1]:
            return False, True
        return None, False
    return window[0] <= stamp - offset <= window[1], True


def _remote_item(printer_id: int, storage: str, entry: dict, window, offset) -> dict:
    mtime = entry.get("mtime")
    quality = entry.get("mtime_quality") or "unknown"
    overlap, assumed = time_overlap(mtime, quality, window, offset)
    return {
        "printer_id": printer_id,
        "storage": storage,
        "path": entry.get("path"),
        "name": entry.get("name"),
        "size": entry.get("size"),
        "mtime": mtime.isoformat() if isinstance(mtime, datetime) else None,
        "time_basis": "exact" if quality == "exact" else "unconfirmed" if isinstance(mtime, datetime) else "unknown",
        "overlap": overlap,
        "assumed": assumed,
    }


def _by_time(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda item: (item["mtime"] or "", item["name"] or ""), reverse=True)


async def _timelapse_source(printer, window, offset) -> dict:
    from backend.app.services.timelapse_files import last_listing_media, list_timelapse_videos, recording_storage

    task = _timelapse_listings.get(printer.id)
    if task is None or task.done():
        task = asyncio.get_running_loop().create_task(list_timelapse_videos(printer), name="archive-sources-timelapse")
        _timelapse_listings[printer.id] = task
        task.add_done_callback(
            lambda done, pid=printer.id: (
                _timelapse_listings.pop(pid, None) if _timelapse_listings.get(pid) is done else None
            )
        )
    try:
        videos, _source = await asyncio.wait_for(asyncio.shield(task), _SOURCES_WAIT_SECONDS)
    except TimeoutError:
        return {"status": "slow", "items": []}
    except Exception as exc:  # noqa: BLE001 — a listing that failed is "unavailable", never a 500 of the chooser
        logger.info("[DOWNLOADS] the recordings of %s could not be listed (%s)", printer.name, type(exc).__name__)
        return {"status": "unavailable", "items": []}
    media = last_listing_media(printer.id)
    items = [
        _remote_item(printer.id, recording_storage(video.get("path") or ""), video, window, offset) for video in videos
    ]
    if items:
        status = "ok"
    else:
        status = "unavailable" if "unavailable" in media.values() else "empty"
    return {"status": status, "items": _by_time(items), "media": media}


async def _camera_source(printer, window, offset) -> dict:
    from backend.app.services.printer_files.base import FILE_TYPE_MODEL
    from backend.app.services.printer_files.factory import transport_for
    from backend.app.services.printer_files.listing import listing_coalescer
    from backend.app.services.printer_manager import printer_manager
    from backend.app.utils.printer_storage import EXTERNAL, SDCARD_NORMAL

    state = printer_manager.get_status(printer.id)
    if (
        state is not None
        and getattr(state, "sdcard_state_seen", False)
        and int(getattr(state, "sdcard_state", SDCARD_NORMAL) or 0) != SDCARD_NORMAL
    ):
        # The printer SAID it has no usable card. The camera records to the card only; internal
        # storage has no such catalogue we can read. A state that has not reported the card yet
        # (its default reads "none") says nothing: the card is asked, and the answer decides —
        # no folder is ``empty``, a printer that does not answer is ``unavailable``.
        return {"status": "unsupported", "items": []}
    answer = await listing_coalescer.list(
        printer,
        EXTERNAL,
        CAMERA_DIRECTORY,
        FILE_TYPE_MODEL,
        transport_for(printer, EXTERNAL),
        wait=_SOURCES_WAIT_SECONDS,
    )
    if answer.status == "missing":
        return {"status": "empty", "items": []}
    if answer.status != "complete":
        return {"status": "slow" if answer.status == "slow" else "unavailable", "items": []}
    items = [
        _remote_item(printer.id, EXTERNAL, entry.as_dict(), window, offset)
        for entry in answer.files
        if not entry.is_directory
        and entry.name.lower().startswith(CAMERA_PREFIX)
        and entry.name.lower().endswith((".mp4", ".avi"))
    ]
    return {"status": "ok" if items else "empty", "items": _by_time(items)}


@router.get("/archives/{archive_id}/media-sources")
async def list_archive_media_sources(
    archive_id: int,
    request: Request,
    offset_minutes: int | None = Query(default=None, ge=-24 * 60, le=24 * 60),
    margin_minutes: int = Query(default=DEFAULT_MARGIN_MINUTES, ge=0, le=12 * 60),
    auth_result=_ARCHIVE_READ,
    creds: RequestCredentials = Depends(request_credentials),
):
    """What can be downloaded for this print: its attached video, and what its printer still holds.

    The three lists answer separately — ``ok``, ``empty``, ``forbidden``,
    ``unsupported``, ``unavailable``, ``slow``, ``no_printer`` — so a printer
    that is off never hides the local video, and "the printer did not answer"
    is never shown as "there is nothing".

    A remote file says how well its time is known. ``overlap`` is true/false
    only for an exact time, or when ``offset_minutes`` — how far the printer's
    clock is ahead of UTC, the operator's own estimate — is given, and then it
    is marked ``assumed``; otherwise it is null and nothing is preselected.
    """
    from backend.app.core import database

    async with database.async_session() as db:
        archive = await _visible_archive(db, archive_id, auth_result)
        local = [*await _local_videos(db, archive), *await variant_videos(db, archive)]
        printer = await _archive_printer(db, archive)
    margin = timedelta(minutes=margin_minutes)
    offset = None if offset_minutes is None else timedelta(minutes=offset_minutes)
    window = archive_window(archive, margin)
    answer = {
        "archive_id": archive.id,
        "window": None
        if window is None
        else {"from": window[0].isoformat(), "to": window[1].isoformat(), "margin_minutes": margin_minutes},
        "offset_minutes": offset_minutes,
        "local": {
            "status": "ok" if local else "empty",
            "items": [
                {"asset": v["asset"], "name": v["name"], "size": v["size"], "variant": bool(v.get("variant"))}
                for v in local
            ],
        },
        "printer_id": None,
    }
    if printer is None:
        status = "no_printer"
    elif not await creds.allows(_FILES_GATE) or not in_key_scope(request, printer.id):
        status = "forbidden"
    else:
        status = None
    if status is not None:
        answer["timelapse"] = {"status": status, "items": []}
        answer["camera"] = {"status": status, "items": []}
        return answer
    answer["printer_id"] = printer.id
    answer["timelapse"], answer["camera"] = await asyncio.gather(
        _timelapse_source(printer, window, offset), _camera_source(printer, window, offset)
    )
    return answer


# -- compatibility: one request, one answer ----------------------------------------------


class NothingDownloaded(Exception):
    """A compatibility download got no file at all; its endpoint words the 404 it always gave."""


async def compat_download(job: DownloadJob, *, media_type: str | None = None) -> WholeFileResponse:
    """Wait for a hidden job and answer with its file — the older synchronous endpoints' shape.

    Disk-backed like every other download: the request holds no file in memory.
    The job is dropped once its one answer has been sent (or was never read).
    Raises :class:`NothingDownloaded` when the printer gave nothing.
    """
    await media_download_runtime.wait(job)
    if job.phase != media_downloads.PHASE_READY:
        media_download_runtime.discard_soon(job, forget=True)
        if job.error == media_downloads.ERROR_NOTHING:
            raise NothingDownloaded
        if job.error == media_downloads.ERROR_LIMIT:
            raise HTTPException(413, "The selection is larger than one download may be")
        if job.error == media_downloads.ERROR_STORAGE:
            raise HTTPException(507, "Not enough free space on the server to prepare this download")
        if job.error == media_downloads.ERROR_AUTHORITY:
            raise HTTPException(403, "Access denied")
        if job.error == media_downloads.ERROR_TIME:
            raise HTTPException(504, "The download took too long to prepare")
        raise HTTPException(503, "The download could not be prepared")
    lease = media_download_runtime.lease(job, discard_after=True)
    if lease is None:
        media_download_runtime.discard_soon(job, forget=True)
        raise HTTPException(503, "The download could not be prepared")
    headers = {"Content-Disposition": build_content_disposition(job.filename)}
    if job.partial:
        # The older ZIP endpoint skipped unreadable files silently; say how many.
        headers["X-Download-Failed"] = str(len(job.sources) - job.succeeded)
    return WholeFileResponse(job.artifact, media_type=media_type or _media_type(job), headers=headers, lease=lease)
