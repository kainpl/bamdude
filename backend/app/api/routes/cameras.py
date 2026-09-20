"""Cameras that belong to no printer — a room, a shelf, a dryer.

Vault 60-specs/standalone-cameras-spec. A printer's external camera is columns
on ``printers`` and REPLACES that printer's own camera: the finish photo, the
plate check, Obico and the layer timelapse all read through it. A row in
``cameras`` is the other thing — a view of a place, only ever displayed.

⚠️ **This module keeps its own registries.** Everything in ``routes/camera.py``
is keyed by ``printer_id: int``; a camera is not a printer and must never be
smuggled in there under a negative id, which would make every one of those
annotations a lie. What IS shared, deliberately, is ``_spawned_ffmpeg_pids``:
the orphan janitor scans for ffmpeg processes we started, and a camera's ffmpeg
holding ``/dev/video0`` open is exactly what it exists to reap.

One upstream per camera, like the printers' built-in cameras: the stream goes
through ``MjpegBroadcaster`` under ``camera-{id}``, so two viewers of the lobby
camera share one connection. A printer's EXTERNAL camera does not do this yet
(process per viewer) — that is its own change, not this one's.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import database
from backend.app.core.auth import RequireCameraStreamToken, RequirePermission
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.camera import Camera
from backend.app.models.printer_location import PrinterLocation
from backend.app.models.user import User
from backend.app.schemas.camera import CameraCreate, CameraOut, CameraTestRequest, CameraUpdate, _validated_source
from backend.app.services.camera_fanout import MjpegBroadcaster, get_or_create_broadcaster, iter_subscriber

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cameras", tags=["cameras"])

#: Last raw frame seen on a camera's live stream, so a snapshot taken while
#: somebody is watching reuses it instead of opening a second reader. A USB
#: camera allows exactly one; an MJPEG one usually does too.
_last_frames: dict[int, bytes] = {}
_last_frame_times: dict[int, float] = {}
#: One-shot captures are cached briefly — a wall of tiles polling the same
#: camera should cost one capture, not one per tile.
_snapshot_frames: dict[int, bytes] = {}
_snapshot_frame_times: dict[int, float] = {}
_SNAPSHOT_CACHE_TTL_SECONDS = 5.0
#: A live frame older than this is stale: the stream died and nobody cleaned up.
_LIVE_FRAME_MAX_AGE_SECONDS = 10.0


def _fanout_key(camera_id: int) -> str:
    return f"camera-{camera_id}"


def _publish_frame(camera_id: int, frame: bytes) -> None:
    _last_frames[camera_id] = frame
    _last_frame_times[camera_id] = time.monotonic()


def _live_frame(camera_id: int) -> bytes | None:
    frame = _last_frames.get(camera_id)
    if frame is None:
        return None
    if time.monotonic() - _last_frame_times.get(camera_id, 0.0) > _LIVE_FRAME_MAX_AGE_SECONDS:
        _last_frames.pop(camera_id, None)
        _last_frame_times.pop(camera_id, None)
        return None
    return frame


def _forget_frames(camera_id: int) -> None:
    for registry in (_last_frames, _last_frame_times, _snapshot_frames, _snapshot_frame_times):
        registry.pop(camera_id, None)


def _cached_snapshot(camera_id: int) -> bytes | None:
    frame = _snapshot_frames.get(camera_id)
    if frame is None:
        return None
    if time.monotonic() - _snapshot_frame_times.get(camera_id, 0.0) > _SNAPSHOT_CACHE_TTL_SECONDS:
        _snapshot_frames.pop(camera_id, None)
        _snapshot_frame_times.pop(camera_id, None)
        return None
    return frame


def _remember_snapshot(camera_id: int, frame: bytes) -> None:
    _snapshot_frames[camera_id] = frame
    _snapshot_frame_times[camera_id] = time.monotonic()


def _out(camera: Camera) -> CameraOut:
    return CameraOut(
        id=camera.id,
        name=camera.name,
        camera_type=camera.camera_type,
        url=camera.url,
        snapshot_url=camera.snapshot_url,
        rotation=camera.rotation,
        enabled=camera.enabled,
        location_id=camera.location_id,
        location_name=camera.location.name if camera.location else None,
        created_at=camera.created_at,
        updated_at=camera.updated_at,
    )


async def _get_camera_or_404(camera_id: int, db: AsyncSession) -> Camera:
    camera = (await db.execute(select(Camera).where(Camera.id == camera_id))).scalar_one_or_none()
    if camera is None:
        raise HTTPException(404, "No such camera.")
    return camera


async def _require_known_location(db: AsyncSession, location_id: int | None) -> None:
    if location_id is None:
        return
    exists = (
        await db.execute(select(PrinterLocation.id).where(PrinterLocation.id == location_id))
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(404, "No such location.")


async def _require_free_name(db: AsyncSession, name: str, exclude_id: int | None = None) -> None:
    query = select(Camera.id).where(Camera.name == name)
    if exclude_id is not None:
        query = query.where(Camera.id != exclude_id)
    if (await db.execute(query)).scalar_one_or_none() is not None:
        raise HTTPException(409, "A camera with this name already exists.")


# ---------------------------------------------------------------- CRUD


@router.get("/", response_model=list[CameraOut])
async def list_cameras(
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_READ),
):
    """Every camera, in name order. Carries the URL: this is the settings list."""
    rows = (await db.execute(select(Camera).order_by(Camera.name))).scalars().all()
    return [_out(camera) for camera in rows]


@router.post("/", response_model=CameraOut, status_code=201)
async def create_camera(
    payload: CameraCreate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    await _require_free_name(db, payload.name)
    await _require_known_location(db, payload.location_id)
    camera = Camera(**payload.model_dump())
    db.add(camera)
    await db.commit()
    await db.refresh(camera)
    logger.info("Camera %s created (%s)", camera.id, camera.camera_type)
    return _out(camera)


@router.patch("/{camera_id}", response_model=CameraOut)
async def update_camera(
    camera_id: int,
    payload: CameraUpdate,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    camera = await _get_camera_or_404(camera_id, db)
    data = payload.model_dump(exclude_unset=True)

    if "name" in data:
        await _require_free_name(db, data["name"], exclude_id=camera_id)
    if "location_id" in data:
        await _require_known_location(db, data["location_id"])
    # The URL's validity depends on the type, and either may be the one that
    # changed — so the pair is checked against what the row will BE, not what
    # the payload happens to carry.
    if "url" in data or "camera_type" in data:
        camera_type = data.get("camera_type", camera.camera_type)
        try:
            data["url"] = _validated_source(data.get("url", camera.url), camera_type)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
    if data.get("snapshot_url"):
        from backend.app.services.external_camera import _sanitize_camera_url

        if _sanitize_camera_url(data["snapshot_url"].strip()) is None:
            raise HTTPException(422, "Snapshot URL must be a valid http(s) or rtsp(s) URL.")
        data["snapshot_url"] = data["snapshot_url"].strip()

    for key, value in data.items():
        setattr(camera, key, value)
    await db.commit()
    await db.refresh(camera)

    # A camera that just changed where it points, or was switched off, must not
    # keep serving the old source from a cache or a running stream.
    if {"url", "camera_type", "snapshot_url", "enabled"} & set(data):
        await _stop_stream(camera_id)
    return _out(camera)


@router.delete("/{camera_id}")
async def delete_camera(
    camera_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.SETTINGS_UPDATE),
):
    camera = await _get_camera_or_404(camera_id, db)
    await db.delete(camera)
    await db.commit()
    await _stop_stream(camera_id)
    logger.info("Camera %s deleted", camera_id)
    return {"deleted": camera_id}


# ---------------------------------------------------------------- test


@router.post("/test")
async def test_camera_source(
    payload: CameraTestRequest,
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Open the source once, confirm a frame, disconnect. For a camera not yet saved."""
    from backend.app.services.external_camera import test_connection

    return await test_connection(payload.url, payload.camera_type)


@router.post("/{camera_id}/test")
async def test_saved_camera(
    camera_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    camera = await _get_camera_or_404(camera_id, db)
    from backend.app.services.external_camera import test_connection

    return await test_connection(camera.url, camera.camera_type)


# ---------------------------------------------------------------- stream


async def _stop_stream(camera_id: int) -> bool:
    """Tear the upstream down and forget the frames. Safe when nothing is running."""
    from backend.app.services.camera_fanout import shutdown_broadcaster

    stopped = await shutdown_broadcaster(_fanout_key(camera_id))
    _forget_frames(camera_id)
    return stopped


@router.get("/{camera_id}/stream")
async def camera_stream(
    camera_id: int,
    request: Request,
    fps: int = 10,
    _token: None = RequireCameraStreamToken,
):
    """MJPEG multipart, one upstream shared by every viewer.

    Gated by ``?token=…`` like the printers' streams, because an ``<img>`` tag
    cannot send an Authorization header.
    """
    # Short-lived session: a stream stays open for as long as the tab does, and
    # holding a pooled connection across it is #2572 all over again.
    async with database.async_session() as db:
        camera = await _get_camera_or_404(camera_id, db)
        if not camera.enabled:
            raise HTTPException(404, "This camera is switched off.")
        url, camera_type = camera.url, camera.camera_type

    fps = min(max(fps, 1), 15)
    stop_event = asyncio.Event()
    stream_id = f"camera-{camera_id}-{uuid.uuid4().hex[:8]}"

    def _register_process(proc: asyncio.subprocess.Process) -> None:
        # The janitor's registry, shared with the printer routes on purpose:
        # an ffmpeg we started holding a device open is its business whoever
        # asked for it.
        from backend.app.api.routes.camera import _spawned_ffmpeg_pids

        _spawned_ffmpeg_pids[proc.pid] = time.time()

    def _factory(disconnect_event: asyncio.Event):
        from backend.app.services.camera_runtime import WorkerCameraRuntime, get_camera_runtime

        runtime = get_camera_runtime()
        if isinstance(runtime, WorkerCameraRuntime):
            # A stable, secret-free identity, exactly as the printer paths do
            # it: rotating a camera's credentials must not create a second
            # physical producer.
            return runtime.stream_external(
                identity=str(uuid.uuid5(uuid.NAMESPACE_URL, f"bamdude:camera:{camera_id}")),
                url=url,
                camera_type=camera_type,
                fps=fps,
                disconnect_event=disconnect_event,
                on_frame=lambda frame: _publish_frame(camera_id, frame),
            )

        from backend.app.services.external_camera import generate_mjpeg_stream

        return generate_mjpeg_stream(
            url,
            camera_type,
            fps,
            on_process=_register_process,
            on_frame=lambda frame: _publish_frame(camera_id, frame),
            stop_event=disconnect_event,
            stream_id=stream_id,
        )

    key = _fanout_key(camera_id)
    broadcaster: MjpegBroadcaster = await get_or_create_broadcaster(key, _factory)
    try:
        queue = await broadcaster.subscribe()
    except RuntimeError:
        # The grace-window teardown flipped it to stopped between the lookup
        # and the subscribe; the registry mints a fresh one on the retry.
        broadcaster = await get_or_create_broadcaster(key, _factory)
        queue = await broadcaster.subscribe()

    logger.info("Camera viewer attached to %s (subscribers=%d)", key, broadcaster.subscriber_count)

    async def _is_disconnected() -> bool:
        try:
            return await request.is_disconnected()
        except Exception:
            return True

    async def _generate():
        try:
            async for chunk in iter_subscriber(
                broadcaster,
                queue,
                is_disconnected=_is_disconnected,
                on_unsubscribe=lambda remaining: logger.info(
                    "Camera viewer detached from %s (subscribers=%d)", key, remaining
                ),
            ):
                yield chunk
        finally:
            stop_event.set()
            if broadcaster.subscriber_count == 0:
                _forget_frames(camera_id)

    return StreamingResponse(
        _generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# Declared once per method rather than as a single
# `api_route(methods=["GET", "POST"])`: FastAPI derives one operationId per
# ROUTE, so a two-method route publishes the same id for both operations —
# and an unstable one, because the method that names it comes from
# `list(route.methods)[0]`, a set whose order follows the process's hash seed.
@router.get("/{camera_id}/stop")
@router.post("/{camera_id}/stop")
async def stop_camera_stream(
    camera_id: int,
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Force this camera's upstream down. Kept for parity with the printers' route."""
    stopped = await _stop_stream(camera_id)
    return {"stopped": 1 if stopped else 0}


# ---------------------------------------------------------------- snapshot


@router.get("/{camera_id}/snapshot")
async def camera_snapshot(
    camera_id: int,
    _token: None = RequireCameraStreamToken,
):
    """One JPEG: the live stream's last frame, a recent capture, or a fresh one.

    The live frame comes first for the same reason it does on a printer: many
    of these sources allow a single reader, and opening a competing one while
    somebody is watching would drop the viewer rather than degrade.
    """
    async with database.async_session() as db:
        camera = await _get_camera_or_404(camera_id, db)
        if not camera.enabled:
            raise HTTPException(404, "This camera is switched off.")
        url, camera_type, snapshot_url = camera.url, camera.camera_type, camera.snapshot_url

    frame = _live_frame(camera_id) or _cached_snapshot(camera_id)
    if frame is None:
        from backend.app.services.camera_runtime import CameraCaptureRequest, capture

        result = await capture(
            CameraCaptureRequest.external(
                url=url,
                camera_type=camera_type,
                snapshot_url=snapshot_url,
                purpose="snapshot",
            )
        )
        frame = result.frame
        if not frame:
            raise HTTPException(503, "Failed to capture a frame from this camera.")
        _remember_snapshot(camera_id, frame)

    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Content-Disposition": f'inline; filename="camera_{camera_id}.jpg"',
        },
    )
