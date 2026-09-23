"""Camera streaming API endpoints for Bambu Lab printers."""

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core import database
from backend.app.core.auth import (
    RequireCameraStreamToken,
    RequirePermission,
    create_camera_stream_token,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services import camera_metrics
from backend.app.services.camera import (
    is_chamber_image_model,
    test_camera_connection,
)
from backend.app.services.camera_fanout import (
    MjpegBroadcaster,
    get_or_create_broadcaster,
    get_subscriber_count,
    iter_subscriber,
    shutdown_broadcaster,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/printers", tags=["camera"])

# Store last frame for each printer (for photo capture from active stream)
_last_frames: dict[int, bytes] = {}

# Track last frame timestamp for each printer (for stall detection)
_last_frame_times: dict[int, float] = {}

# Track stream start times for each printer
_stream_start_times: dict[int, float] = {}

# Store recent one-shot snapshots to avoid opening a fresh camera connection
# for every Cam Wall poll when no live stream is attached.
_snapshot_frames: dict[int, bytes] = {}
_snapshot_frame_times: dict[int, float] = {}
_SNAPSHOT_CACHE_TTL_SECONDS = 5.0

# Keep the worker-owned source type and a per-upstream token here so
# snapshots and background consumers still honour the single-camera-reader
# invariant while a worker relay is active.
_active_worker_streams: dict[int, tuple[str, str]] = {}


def get_buffered_frame(printer_id: int) -> bytes | None:
    """Get the last buffered frame for a printer from an active stream.

    Returns the JPEG frame data if available, or None if no active stream.
    """
    return _last_frames.get(printer_id)


def is_stream_active(printer_id: int) -> bool:
    """Return True iff any MJPEG fan-out stream is currently registered for
    this printer (main camera or chamber camera).

    Used by callers that want to AVOID opening a competing upstream RTSP /
    HTTP socket while a viewer is attached — some firmwares (notably X2D
    01.01.00.00) enforce strict single-camera-connection and will drop the
    live fan-out stream the moment a second socket opens. Checking
    ``_active_worker_streams`` independently of buffer state lets us skip the
    competing-socket path even during the 1–3 s startup window before the
    first JPEG lands in ``_last_frames``. Upstream Bambuddy #1348 / commit
    ce5f4e5f.
    """
    return printer_id in _active_worker_streams


def live_frame_for_capture(printer_id: int) -> tuple[bool, bytes | None]:
    """Should a one-shot capture stand down for the live view, and to what frame?

    Returns ``(defer, frame)``. ``defer`` True means **do not open a capture of
    your own**: use ``frame`` when it is not None, and otherwise skip this
    attempt rather than competing.

    Both camera kinds allow exactly one reader — Bambu firmware permits one
    connection, a USB camera permits one V4L2 handle — so a capture that races
    the live view does not degrade, it fails outright. Upstream #2707 measured
    **0 of 87** and **0 of 105** layer-timelapse captures on prints that were
    watched throughout, and finish photos going out with no image at all.

    Skipping while the buffer is momentarily empty (stream starting, mid-
    reconnect) rather than falling through to a capture is the #1348 rule:
    opening a competing handle kicks the viewer off, which is a worse outcome
    than missing one frame.

    One helper rather than each caller pairing ``is_stream_active`` with
    ``get_buffered_frame`` itself — that pairing is the whole invariant, and
    every consumer that reimplemented it is a consumer that can get it subtly
    wrong (see ``40-invariants/inv-single-camera-socket``).
    """
    if not is_stream_active(printer_id):
        return False, None
    # Through the public accessor, not ``_last_frames`` directly: there is one
    # way to read the buffer and this is not a second one.
    return True, get_buffered_frame(printer_id)


def _new_fanout_stream_id(printer_id: int) -> str:
    """A registry key for one fan-out broadcaster (#2707).

    Keeps the ``{printer_id}-`` prefix used by the relay log and a unique suffix so two broadcasters for one
    printer cannot share an entry. The external path already mints ids this way
    (#2675); this is the same shape so the two read alike.

    A constant ``f"{printer_id}-fanout"`` meant a departing generator's ``finally``
    popped its **successor's** entry when a view was closed and reopened inside
    the teardown window.
    """
    return f"{printer_id}-fanout-{uuid.uuid4().hex[:8]}"


def _release_printer_frame_state(printer_id: int | None) -> None:
    """Clear the per-printer frame state, unless another stream still needs it.

    ``_last_frames`` / ``_last_frame_times`` / ``_stream_start_times`` are keyed
    by **printer**, while streams are keyed by stream id — so a departing
    generator's ``finally`` used to wipe state belonging to a *successor*
    (upstream #2707). Close a camera view and reopen it inside the teardown
    window and the new stream loses its buffer and its start time to the old
    one's cleanup.

    Every consequence is indirect and none of them looks like a camera bug:
    :func:`try_get_active_buffered_frame` returns nothing, so Obico polling and
    snapshots open a **second** upstream socket against the live view — exactly
    what ``inv-single-camera-socket`` exists to prevent on firmware that allows
    only one; and ``/camera/status`` reports a stream uptime that restarts.

    Called *after* the departing stream has removed itself from the registries,
    so :func:`is_stream_active` answers about the survivors only.
    """
    if printer_id is None:
        return
    if is_stream_active(printer_id):
        logger.debug("Keeping frame state for printer %s — another stream is still attached", printer_id)
        return
    _last_frames.pop(printer_id, None)
    _last_frame_times.pop(printer_id, None)
    _stream_start_times.pop(printer_id, None)
    _snapshot_frames.pop(printer_id, None)
    _snapshot_frame_times.pop(printer_id, None)


def _snapshot_response(printer_id: int, image_data: bytes) -> Response:
    return Response(
        content=image_data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Content-Disposition": f'inline; filename="snapshot_{printer_id}.jpg"',
        },
    )


def _get_cached_snapshot(printer_id: int) -> bytes | None:
    image_data = _snapshot_frames.get(printer_id)
    if image_data is None:
        return None

    captured_at = _snapshot_frame_times.get(printer_id, 0.0)
    if time.monotonic() - captured_at > _SNAPSHOT_CACHE_TTL_SECONDS:
        _snapshot_frames.pop(printer_id, None)
        _snapshot_frame_times.pop(printer_id, None)
        return None

    return image_data


def _remember_snapshot(printer_id: int, image_data: bytes) -> None:
    _snapshot_frames[printer_id] = image_data
    _snapshot_frame_times[printer_id] = time.monotonic()


def try_get_active_buffered_frame(printer_id: int) -> bytes | None:
    """Return the broadcaster's last buffered frame ONLY when a viewer is
    attached for this printer; ``None`` otherwise.

    Distinct from :func:`get_buffered_frame` — that one returns whatever sits
    in the buffer regardless of whether a viewer is currently watching, which
    can return stale frames from a previously-attached viewer. This helper
    is the right gate for the "avoid competing socket" decision: it's the
    *combination* of "viewer attached" and "fresh frame available". Callers
    that need to know "viewer attached" independently of buffer state should
    use :func:`is_stream_active` (e.g. Obico, which prefers to skip a poll
    cycle entirely rather than open a competing socket).

    Upstream Bambuddy #1271 / commit c097140e + #1348 / commit ce5f4e5f.
    """
    if not is_stream_active(printer_id):
        return None
    return _last_frames.get(printer_id)


async def get_printer_or_404(printer_id: int, db: AsyncSession) -> Printer:
    """Get printer by ID or raise 404."""
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer


async def _worker_stream_response(
    *,
    printer: Printer,
    printer_id: int,
    request: Request,
    fps: int,
    runtime,
    builtin: bool = False,
) -> StreamingResponse:
    """Serve worker-owned live video through the existing HTTP fan-out.

    The worker owns the physical source; this process keeps the existing HTTP
    fan-out and browser-disconnect behaviour.  That preserves the public MJPEG
    endpoint and avoids multiplying worker leases when a camera wall opens the
    same printer in several places.
    """

    fanout_key = f"printer-{printer_id}"
    # Stable, secret-free source identity.  The URL intentionally is not the
    # key: an access-token rotation must not create a second physical producer.
    identity = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"bamdude:printer:{printer_id}:{'builtin' if builtin else 'external-camera'}")
    )
    source = "chamber_image" if builtin and is_chamber_image_model(printer.model) else "rtsp" if builtin else "external"

    def _publish_worker_frame(frame: bytes) -> None:
        now = time.time()
        _last_frames[printer_id] = frame
        _last_frame_times[printer_id] = now
        # The external snapshot endpoint consults this short cache before it
        # opens a competing one-shot connection to a single-reader camera.
        _remember_snapshot(printer_id, frame)

    def _factory(disconnect_event: asyncio.Event):
        # The factory runs once per actual upstream, not once per browser
        # subscriber.  Its token prevents an older grace-window teardown from
        # erasing a successor's worker state.
        worker_stream_token = uuid.uuid4().hex
        _active_worker_streams[printer_id] = (worker_stream_token, source)
        _stream_start_times.setdefault(printer_id, time.time())
        started = time.monotonic()
        frames = 0
        first_frame_ms: float | None = None

        def _observe_worker_frame(frame: bytes) -> None:
            nonlocal frames, first_frame_ms
            frames += 1
            if first_frame_ms is None:
                first_frame_ms = round((time.monotonic() - started) * 1000, 1)
                logger.info(
                    "Camera worker relay first frame: printer=%s session=%s first_frame_ms=%s",
                    printer_id,
                    worker_stream_token,
                    first_frame_ms,
                )
            _publish_worker_frame(frame)

        async def _stream():
            reason = "source_ended"
            logger.info(
                "Camera worker relay started: printer=%s session=%s identity=%s source=%s requested_fps=%s",
                printer_id,
                worker_stream_token,
                identity,
                source,
                fps,
            )
            try:
                stream = (
                    runtime.stream_builtin(
                        identity=identity,
                        ip_address=printer.ip_address,
                        access_code=printer.access_code,
                        model=printer.model,
                        fps=fps,
                        disconnect_event=disconnect_event,
                        on_frame=_observe_worker_frame,
                    )
                    if builtin
                    else runtime.stream_external(
                        identity=identity,
                        url=printer.external_camera_url,
                        camera_type=printer.external_camera_type,
                        fps=fps,
                        disconnect_event=disconnect_event,
                        on_frame=_observe_worker_frame,
                    )
                )
                async for chunk in stream:
                    yield chunk
            except asyncio.CancelledError:
                reason = "cancelled"
                raise
            except Exception:
                reason = "relay_failed"
                raise
            finally:
                if disconnect_event.is_set():
                    reason = "viewers_gone"
                logger.info(
                    "Camera worker relay ended: printer=%s session=%s reason=%s frames=%s first_frame_ms=%s duration_ms=%.1f",
                    printer_id,
                    worker_stream_token,
                    reason,
                    frames,
                    first_frame_ms,
                    (time.monotonic() - started) * 1000,
                )
                if _active_worker_streams.get(printer_id) == (worker_stream_token, source):
                    _active_worker_streams.pop(printer_id, None)
                _release_printer_frame_state(printer_id)

        return _stream()

    broadcaster: MjpegBroadcaster = await get_or_create_broadcaster(fanout_key, _factory)
    try:
        queue = await broadcaster.subscribe()
    except RuntimeError:
        broadcaster = await get_or_create_broadcaster(fanout_key, _factory)
        queue = await broadcaster.subscribe()

    logger.info("Camera worker viewer attached to %s (subscribers=%d)", fanout_key, broadcaster.subscriber_count)

    async def _is_disconnected() -> bool:
        try:
            return await request.is_disconnected()
        except Exception:
            return True

    async def _generate():
        # One light lease per viewer, for exactly as long as the viewer reads
        # (services/camera_light): taken inside the generator so a response
        # that is never iterated never holds it.
        from backend.app.services import camera_light

        light = await camera_light.acquire(printer_id, "stream")
        try:
            async for chunk in iter_subscriber(
                broadcaster,
                queue,
                is_disconnected=_is_disconnected,
                on_unsubscribe=lambda remaining: logger.info(
                    "Camera worker viewer detached from %s (subscribers=%d)", fanout_key, remaining
                ),
            ):
                yield chunk
        finally:
            camera_light.release(light)

    return StreamingResponse(
        _generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.post("/camera/stream-token")
async def create_stream_token(
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Mint a short-lived camera-stream token for the caller.

    The token is appended as ``?token=...`` to stream/snapshot URLs that are
    loaded by ``<img>`` / ``<video>`` tags — those can't send Authorization
    headers. Tokens last 60 minutes and are reusable within that window.
    """
    return {"token": await create_camera_stream_token()}


@router.get("/{printer_id}/camera/stream")
async def camera_stream(
    printer_id: int,
    request: Request,
    fps: int = 10,
    _token: None = RequireCameraStreamToken,
):
    """Stream live video from printer camera as MJPEG.

    This endpoint returns a multipart MJPEG stream that can be used directly
    in an <img> tag or video player.

    Gated by a ``?token=...`` query param from ``POST /printers/camera/stream-token``
    because ``<img>`` / ``<video>`` tags can't send Authorization headers.

    Uses external camera if configured, otherwise uses built-in camera:
    - External: MJPEG, RTSP, or HTTP snapshot
    - A1/P1: Chamber image protocol (port 6000)
    - X1/H2/P2: RTSP via ffmpeg (port 322)

    Args:
        printer_id: Printer ID
        fps: Target frames per second (default: 10, max: 30)
    """
    # Fetch the printer in a short-lived session so the pooled DB connection is
    # released BEFORE we start streaming. A live MJPEG stream runs for as long as
    # the browser tab stays open (potentially hours); holding the Depends(get_db)
    # session across it pinned one pooled connection per open camera tab per
    # printer — a top contributor to pool exhaustion on large farms (#2572).
    # expire_on_commit=False keeps the printer's already-loaded columns readable
    # after the session closes, and everything below reads only scalar attributes
    # (model, ip_address, access_code, external_camera_*) — no lazy loads.
    # Reference async_session via the module so the maker is looked up at call
    # time (stays in sync with reinitialize_database() + test patches).
    async with database.async_session() as db:
        printer = await get_printer_or_404(printer_id, db)

    # Check for external camera first
    if printer.external_camera_enabled and printer.external_camera_url:
        from backend.app.services.camera_runtime import get_camera_runtime

        # Limit external camera FPS to reduce browser load
        fps = min(max(fps, 1), 15)
        logger.info(
            "Using external camera (%s) for printer %s at %s fps", printer.external_camera_type, printer_id, fps
        )

        return await _worker_stream_response(
            printer=printer,
            printer_id=printer_id,
            request=request,
            fps=fps,
            runtime=get_camera_runtime(),
        )

    # Validate FPS - A1/P1 models max out at ~5 FPS
    if is_chamber_image_model(printer.model):
        fps = min(max(fps, 1), 5)
    else:
        fps = min(max(fps, 1), 30)

    from backend.app.services.camera_runtime import get_camera_runtime

    return await _worker_stream_response(
        printer=printer,
        printer_id=printer_id,
        request=request,
        fps=fps,
        runtime=get_camera_runtime(),
        builtin=True,
    )


# Declared once per method rather than as a single
# `api_route(methods=["GET", "POST"])`: FastAPI derives one operationId per
# ROUTE, so a two-method route publishes the same id for both operations —
# and an unstable one, because the method that names it comes from
# `list(route.methods)[0]`, a set whose order follows the process's hash seed.
@router.get("/{printer_id}/camera/stop")
@router.post("/{printer_id}/camera/stop")
async def stop_camera_stream(
    printer_id: int,
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Stop active camera streams for a printer.

    Called by the frontend on viewer unmount (cam-wall tile, embedded viewer,
    popup window). Accepts both GET and POST (POST for sendBeacon compatibility).

    Reference-count guard (#451): every viewer of a printer subscribes to the
    same fan-out broadcaster, so a force-shutdown triggered by ONE leaving
    viewer used to kill the others' streams (a cam-wall tile froze when a user
    opened then closed the embedded viewer). If any subscriber is still
    attached, skip the force-teardown — the broadcaster's natural grace-shutdown
    (5 s after subscribers drop to 0) handles cleanup when the leaving viewer's
    HTTP connection actually closes.
    """
    broadcaster_key = f"printer-{printer_id}"
    remaining_subscribers = get_subscriber_count(broadcaster_key)
    if remaining_subscribers >= 1:
        logger.info(
            "Skipping camera force-shutdown for printer %s: %d subscriber(s) still attached; "
            "natural cleanup will tear down when the last viewer disconnects",
            printer_id,
            remaining_subscribers,
        )
        return {"stopped": 0, "skipped": True}

    # The worker owns every physical camera socket. The broadcaster releases
    # its worker lease when the final viewer leaves; main never kills ffmpeg.
    stopped = int(await shutdown_broadcaster(broadcaster_key))
    if stopped:
        logger.info("Shut down camera fan-out broadcaster for printer %s", printer_id)

    logger.info("Stopped %s camera stream(s) for printer %s", stopped, printer_id)
    return {"stopped": stopped}


@router.get("/{printer_id}/camera/snapshot")
async def camera_snapshot(
    printer_id: int,
    poll: int | None = None,
    _token: None = RequireCameraStreamToken,
):
    """Capture a single frame from the printer camera.

    Returns a JPEG image.

    Gated by a ``?token=...`` query param from ``POST /printers/camera/stream-token``
    because ``<img>`` / ``<video>`` tags can't send Authorization headers.

    ``?poll=<ms>`` is a poller declaring its cadence (the Camera Wall in
    snapshot mode, the embedded viewer): the chamber light, when the farm
    or the printer asks for it, is then held for that cadence plus the
    capture timeout instead of the one-shot grace, so a wall that is open
    keeps the light on rather than blinking it on every frame.
    """
    # Fetch the printer in a short-lived session and release the pooled DB
    # connection BEFORE the camera capture below (up to 15s, longer under a
    # saturated FTP/camera pool). Holding a Depends(get_db) session across the
    # grab pinned one connection per snapshot — and the Cam Wall polls this per
    # tile — so overlapping captures could pile up connections on a large farm
    # (#2572, sibling of the camera_stream fix). Everything below reads only
    # already-loaded scalar columns (expire_on_commit=False).
    async with database.async_session() as db:
        printer = await get_printer_or_404(printer_id, db)

    # The light for the whole answer — buffered, cached or fresh — so a
    # poller keeps it on between two frames. The capture requests below do
    # NOT name the printer: this lease already waited for the light.
    from backend.app.services import camera_light

    async with camera_light.held(printer_id, "snapshot", hold=camera_light.hold_for_poll(poll)) as lease:
        if lease is not None:
            await lease.settle()
        return await _snapshot_from(printer_id, printer)


async def _snapshot_from(printer_id: int, printer):
    """One frame from ``printer``: external camera, live buffer, recent cache, or a fresh capture."""
    import tempfile
    from pathlib import Path

    # Check for external camera first
    if printer.external_camera_enabled and printer.external_camera_url:
        from backend.app.services.camera_runtime import CameraCaptureRequest, CameraWorkerUnavailable, capture

        cached = _get_cached_snapshot(printer_id)
        if cached is not None:
            camera_metrics.remember_delivery(printer_id, "snapshot_cache")
            return _snapshot_response(printer_id, cached)

        try:
            result = await capture(
                CameraCaptureRequest.external(
                    url=printer.external_camera_url,
                    camera_type=printer.external_camera_type,
                    timeout=15,
                    snapshot_url=printer.external_camera_snapshot_url,
                )
            )
        except CameraWorkerUnavailable as exc:
            raise HTTPException(status_code=503, detail="Camera worker is unavailable.") from exc
        frame_data = result.frame
        camera_metrics.remember_delivery(
            printer_id,
            "shared_capture" if result.source == "coalesced" else "own_capture" if frame_data else None,
            result,
        )
        if not frame_data:
            raise HTTPException(
                status_code=503,
                detail="Failed to capture frame from external camera.",
            )
        _remember_snapshot(printer_id, frame_data)
        return _snapshot_response(printer_id, frame_data)

    # If a live fan-out stream is already running for this printer, reuse
    # the broadcaster's buffered frame instead of opening a competing RTSP
    # socket. Some firmwares (notably X2D 01.01.00.00) enforce strict
    # single-camera-connection — a fresh socket here would drop the live
    # viewer's stream. Falls through to fresh capture when no viewer is
    # attached (snapshot is user-initiated single-shot; that's fine).
    # Upstream Bambuddy #1271 / commit c097140e.
    buffered = try_get_active_buffered_frame(printer_id)
    if buffered is not None:
        camera_metrics.remember_delivery(printer_id, "live_buffer")
        return _snapshot_response(printer_id, buffered)

    cached = _get_cached_snapshot(printer_id)
    if cached is not None:
        camera_metrics.remember_delivery(printer_id, "snapshot_cache")
        return _snapshot_response(printer_id, cached)

    # Create temporary file for the snapshot
    import os

    fd, tmp_name = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    temp_path = Path(tmp_name)
    temp_path.chmod(0o600)

    try:
        from backend.app.services.camera_runtime import CameraCaptureRequest, CameraWorkerUnavailable, capture

        try:
            result = await capture(
                CameraCaptureRequest.builtin(
                    ip_address=printer.ip_address,
                    access_code=printer.access_code,
                    model=printer.model,
                    timeout=15,
                )
            )
        except CameraWorkerUnavailable as exc:
            raise HTTPException(status_code=503, detail="Camera worker is unavailable.") from exc
        camera_metrics.remember_delivery(
            printer_id,
            "shared_capture" if result.source == "coalesced" else "own_capture" if result.frame else None,
            result,
        )
        if not result.frame:
            raise HTTPException(
                status_code=503,
                detail="Failed to capture camera frame. Ensure printer is on and camera is enabled.",
            )

        temp_path.write_bytes(result.frame)
        image_data = result.frame

        _remember_snapshot(printer_id, image_data)
        return _snapshot_response(printer_id, image_data)
    finally:
        # Clean up temp file
        if temp_path.exists():
            temp_path.unlink()


@router.get("/{printer_id}/camera/test")
async def test_camera(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Test camera connection for a printer.

    Returns success status and any error message.
    """
    printer = await get_printer_or_404(printer_id, db)

    result = await test_camera_connection(
        ip_address=printer.ip_address,
        access_code=printer.access_code,
        model=printer.model,
    )

    return result


@router.post("/{printer_id}/camera/diagnose")
async def diagnose_camera_route(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Run staged diagnostics for a printer's camera path.

    Returns a structured result the frontend renders inline so users
    can self-diagnose "connection lost" before opening a ticket. See
    ``services/camera_diagnose.py`` for stage details and the live-
    stream shortcut. Upstream Bambuddy #1395 follow-up / commit
    ``134847a3``.
    """
    import time

    from backend.app.services.camera_diagnose import diagnose_camera

    printer = await get_printer_or_404(printer_id, db)

    # Look up live-stream evidence so the diagnostic can short-circuit
    # instead of fighting a viewer for the printer's single camera slot.
    has_live = is_stream_active(printer_id)
    last_ts = _last_frame_times.get(printer_id) if has_live else None
    live_age = (time.time() - last_ts) if (has_live and last_ts) else None

    result = await diagnose_camera(
        ip_address=printer.ip_address,
        access_code=printer.access_code,
        model=printer.model,
        printer_id=printer_id,
        has_live_stream=has_live,
        live_frame_age_seconds=live_age,
    )
    return result.to_dict()


@router.get("/{printer_id}/camera/status")
async def camera_status(
    printer_id: int,
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Get the status of an active camera stream.

    Returns whether a stream is active and when the last frame was received.
    Used by the frontend to detect stalled streams and auto-reconnect.
    """
    import time

    # Check if there's an active stream for this printer
    has_active_stream = False
    source: str | None = None

    # The worker owns the physical socket; status still reports its source.
    worker_stream = _active_worker_streams.get(printer_id)
    if worker_stream is not None:
        has_active_stream = True
        source = worker_stream[1]

    # Get timing information
    current_time = time.time()
    last_frame_time = _last_frame_times.get(printer_id)
    stream_start_time = _stream_start_times.get(printer_id)

    # Calculate seconds since last frame
    seconds_since_frame = None
    if last_frame_time is not None:
        seconds_since_frame = current_time - last_frame_time

    # Calculate stream uptime
    stream_uptime = None
    if stream_start_time is not None:
        stream_uptime = current_time - stream_start_time

    return {
        "active": has_active_stream,
        "has_frames": printer_id in _last_frames,
        "seconds_since_frame": seconds_since_frame,
        "stream_uptime": stream_uptime,
        # A support bundle can distinguish a built-in protocol from an
        # operator-configured external camera without inspecting credentials.
        "source": source,
        "telemetry": camera_metrics.for_printer(printer_id),
        "last_snapshot": camera_metrics.delivery_for_printer(printer_id),
        # External-camera streams are intentionally direct today, so this is
        # their viewer count only when the built-in fan-out path is active.
        "subscribers": get_subscriber_count(f"printer-{printer_id}"),
        # Consider stalled if no frame for more than 10 seconds after stream started
        "stalled": (
            has_active_stream
            and stream_uptime is not None
            and stream_uptime > 5  # Give 5 seconds for stream to start
            and (seconds_since_frame is None or seconds_since_frame > 10)
        ),
    }


@router.post("/{printer_id}/camera/external/test")
async def test_external_camera(
    printer_id: int,
    url: str,
    camera_type: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Test external camera connection.

    Args:
        printer_id: Printer ID (for authorization)
        url: Camera URL or USB device path to test
        camera_type: Camera type ("mjpeg", "rtsp", "snapshot", "usb")

    Returns:
        Dict with {success: bool, error?: str, resolution?: str}
    """
    # Verify printer exists (for authorization)
    await get_printer_or_404(printer_id, db)

    from backend.app.services.camera_runtime import test_external_connection

    return await test_external_connection(url, camera_type)


@router.get("/{printer_id}/camera/check-plate")
async def check_plate_empty(
    printer_id: int,
    plate_type: str | None = None,
    use_external: bool | None = None,
    include_debug_image: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Check if the build plate is empty using camera vision.

    Uses calibration-based difference detection - compares current frame
    to a reference image of the empty plate.

    IMPORTANT: Chamber light must be ON for reliable detection.

    Args:
        printer_id: Printer ID
        plate_type: Type of build plate (e.g., "High Temp Plate") for calibration lookup
        use_external: If True, prefer external camera over built-in. When
            omitted (None), defaults to the printer's
            ``external_camera_enabled`` setting — mirroring the runtime
            auto-check at print start (``main.py``). Without this default
            the UI's manual check would always use the built-in camera,
            mismatching the reference saved during calibration (upstream
            Bambuddy #1359 / commit 29379e3b).
        include_debug_image: If True, return URL to annotated debug image

    Returns:
        Dict with detection results:
        - is_empty: bool - Whether plate appears empty
        - confidence: float - Confidence level (0.0 to 1.0)
        - difference_percent: float - How different from calibration reference
        - message: str - Human-readable result message
        - needs_calibration: bool - True if calibration is required
        - light_warning: bool - True if chamber light is off
    """
    from backend.app.services.plate_detection import (
        check_plate_empty as do_check,
        is_plate_detection_available,
    )
    from backend.app.services.printer_manager import printer_manager

    # Check printer exists first (before OpenCV check)
    printer = await get_printer_or_404(printer_id, db)

    # #1359: when the caller doesn't pin a value, default to the printer's
    # external-camera config so the UI's manual check captures from the
    # same source the runtime auto-check uses at print start. Without this
    # the manual check would always use the built-in camera and never
    # match the reference saved during calibration.
    if use_external is None:
        use_external = bool(
            printer.external_camera_enabled and printer.external_camera_url and printer.external_camera_type
        )

    if not is_plate_detection_available():
        raise HTTPException(
            status_code=503,
            detail="Plate detection not available. Install opencv-python-headless to enable.",
        )

    # Check chamber light status
    light_warning = False
    state = printer_manager.get_status(printer_id)
    if state and not state.chamber_light:
        light_warning = True

    from backend.app.services.plate_detection import PlateDetector

    # Build ROI tuple from printer settings if available
    roi = None
    if all(
        [
            printer.plate_detection_roi_x is not None,
            printer.plate_detection_roi_y is not None,
            printer.plate_detection_roi_w is not None,
            printer.plate_detection_roi_h is not None,
        ]
    ):
        roi = (
            printer.plate_detection_roi_x,
            printer.plate_detection_roi_y,
            printer.plate_detection_roi_w,
            printer.plate_detection_roi_h,
        )

    result = await do_check(
        printer_id=printer.id,
        ip_address=printer.ip_address,
        access_code=printer.access_code,
        model=printer.model,
        plate_type=plate_type,
        include_debug_image=include_debug_image,
        external_camera_url=printer.external_camera_url if printer.external_camera_enabled else None,
        external_camera_type=printer.external_camera_type if printer.external_camera_enabled else None,
        use_external=use_external,
        roi=roi,
        external_camera_snapshot_url=printer.external_camera_snapshot_url if printer.external_camera_enabled else None,
    )

    # Get reference count for the response
    detector = PlateDetector()
    ref_count = detector.get_calibration_count(printer.id)

    response = result.to_dict()
    response["light_warning"] = light_warning
    response["reference_count"] = ref_count
    response["max_references"] = detector.MAX_REFERENCES
    # Include current ROI in response
    if roi:
        response["roi"] = {"x": roi[0], "y": roi[1], "w": roi[2], "h": roi[3]}
    else:
        # Return default ROI
        response["roi"] = {"x": 0.15, "y": 0.35, "w": 0.70, "h": 0.55}

    # If debug image requested and available, encode as base64 data URL
    if include_debug_image and result.debug_image:
        import base64

        b64_image = base64.b64encode(result.debug_image).decode("utf-8")
        response["debug_image_url"] = f"data:image/jpeg;base64,{b64_image}"

    return response


@router.post("/{printer_id}/camera/plate-detection/calibrate")
async def calibrate_plate_detection(
    printer_id: int,
    label: str | None = None,
    use_external: bool | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Calibrate plate detection by capturing a reference image of the empty plate.

    The plate MUST be empty when calling this endpoint. The captured image
    will be used as the reference for future detection comparisons.

    Supports up to 5 reference images per printer. When adding a 6th, the oldest
    is automatically removed.

    IMPORTANT: Chamber light should be ON for calibration.

    Args:
        printer_id: Printer ID
        label: Optional label for this reference (e.g., "High Temp Plate", "Wham Bam")
        use_external: If True, prefer external camera over built-in. When
            omitted (None), defaults to the printer's
            ``external_camera_enabled`` setting so calibration captures
            from the same source the runtime auto-check uses at print
            start (upstream Bambuddy #1359 / commit 29379e3b).

    Returns:
        Dict with:
        - success: bool - Whether calibration succeeded
        - message: str - Status message
        - index: int - The reference slot used (0-4)
    """
    from backend.app.services.plate_detection import (
        calibrate_plate,
        is_plate_detection_available,
    )
    from backend.app.services.printer_manager import printer_manager

    # Check printer exists first (before OpenCV check)
    printer = await get_printer_or_404(printer_id, db)

    # #1359: default to the printer's external-camera config when the
    # caller doesn't pin a value, so the reference saved here uses the
    # same camera the runtime auto-check captures from at print start.
    if use_external is None:
        use_external = bool(
            printer.external_camera_enabled and printer.external_camera_url and printer.external_camera_type
        )

    if not is_plate_detection_available():
        raise HTTPException(
            status_code=503,
            detail="Plate detection not available. Install opencv-python-headless to enable.",
        )

    # Check chamber light - warn but don't block
    state = printer_manager.get_status(printer_id)
    light_warning = state and not state.chamber_light

    success, message, index = await calibrate_plate(
        printer_id=printer.id,
        ip_address=printer.ip_address,
        access_code=printer.access_code,
        model=printer.model,
        label=label,
        external_camera_url=printer.external_camera_url if printer.external_camera_enabled else None,
        external_camera_type=printer.external_camera_type if printer.external_camera_enabled else None,
        use_external=use_external,
        external_camera_snapshot_url=printer.external_camera_snapshot_url if printer.external_camera_enabled else None,
    )

    if light_warning and success:
        message += " (Warning: Chamber light was off)"

    return {"success": success, "message": message, "index": index}


@router.delete("/{printer_id}/camera/plate-detection/calibrate")
async def delete_plate_calibration(
    printer_id: int,
    plate_type: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Delete the plate detection calibration for a printer and plate type.

    Args:
        printer_id: Printer ID
        plate_type: Type of build plate (if None, deletes legacy non-plate-specific calibration)

    Returns:
        Dict with:
        - success: bool - Whether deletion succeeded
        - message: str - Status message
    """
    from backend.app.services.plate_detection import (
        delete_calibration,
        is_plate_detection_available,
    )

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(
            status_code=503,
            detail="Plate detection not available. Install opencv-python-headless to enable.",
        )

    deleted = delete_calibration(printer_id, plate_type)
    plate_msg = f" for '{plate_type}'" if plate_type else ""

    return {
        "success": deleted,
        "message": f"Calibration deleted{plate_msg}" if deleted else f"No calibration found{plate_msg}",
    }


@router.get("/{printer_id}/camera/plate-detection/status")
async def get_plate_detection_status(
    printer_id: int,
    plate_type: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Check plate detection status for a printer and plate type.

    Returns:
        Dict with:
        - available: bool - Whether OpenCV is installed
        - calibrated: bool - Whether printer has calibration for this plate type
        - plate_type: str - The plate type queried
        - chamber_light: bool - Whether chamber light is on
        - message: str - Status message
    """
    from backend.app.services.plate_detection import (
        get_calibration_status,
        is_plate_detection_available,
    )
    from backend.app.services.printer_manager import printer_manager

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        return {
            "available": False,
            "calibrated": False,
            "plate_type": plate_type,
            "chamber_light": False,
            "message": "OpenCV not installed",
        }

    # Get chamber light status
    state = printer_manager.get_status(printer_id)
    chamber_light = state.chamber_light if state else False

    status = get_calibration_status(printer_id, plate_type)
    status["chamber_light"] = chamber_light

    return status


@router.get("/{printer_id}/camera/plate-detection/references")
async def get_plate_references(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Get all calibration references for a printer with metadata.

    Returns list of references with index, label, timestamp, and thumbnail URL.
    """
    from backend.app.services.plate_detection import PlateDetector, is_plate_detection_available

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(503, "Plate detection not available")

    detector = PlateDetector()
    references = detector.get_references(printer_id)

    # Add thumbnail URLs
    for ref in references:
        ref["thumbnail_url"] = (
            f"/api/v1/printers/{printer_id}/camera/plate-detection/references/{ref['index']}/thumbnail"
        )

    return {
        "references": references,
        "max_references": detector.MAX_REFERENCES,
    }


@router.get("/{printer_id}/camera/plate-detection/references/{index}/thumbnail")
async def get_reference_thumbnail(
    printer_id: int,
    index: int,
    db: AsyncSession = Depends(get_db),
    _token: None = RequireCameraStreamToken,
):
    """Get thumbnail image for a calibration reference.

    Gated by ``?token=...`` query param (short-lived camera-stream token)
    since ``<img src>`` cannot send Authorization headers.
    """
    from fastapi.responses import Response

    from backend.app.services.plate_detection import PlateDetector, is_plate_detection_available

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(503, "Plate detection not available")

    detector = PlateDetector()
    thumbnail = detector.get_reference_thumbnail(printer_id, index)

    if thumbnail is None:
        raise HTTPException(404, "Reference not found")

    return Response(content=thumbnail, media_type="image/jpeg")


@router.put("/{printer_id}/camera/plate-detection/references/{index}")
async def update_reference_label(
    printer_id: int,
    index: int,
    label: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Update the label for a calibration reference."""
    from backend.app.services.plate_detection import PlateDetector, is_plate_detection_available

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(503, "Plate detection not available")

    detector = PlateDetector()
    success = detector.update_reference_label(printer_id, index, label)

    if not success:
        raise HTTPException(404, "Reference not found")

    return {"success": True, "index": index, "label": label}


@router.delete("/{printer_id}/camera/plate-detection/references/{index}")
async def delete_reference(
    printer_id: int,
    index: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.CAMERA_VIEW),
):
    """Delete a specific calibration reference."""
    from backend.app.services.plate_detection import PlateDetector, is_plate_detection_available

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(503, "Plate detection not available")

    detector = PlateDetector()
    success = detector.delete_reference(printer_id, index)

    if not success:
        raise HTTPException(404, "Reference not found")

    return {"success": True, "message": "Reference deleted"}
