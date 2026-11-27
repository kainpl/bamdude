"""Camera facade: physical capture belongs exclusively to the worker."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from backend.app.services.camera_metrics import CameraCaptureResult
from backend.app.services.camera_worker_supervisor import CameraWorkerSupervisor, CameraWorkerUnavailable

logger = logging.getLogger(__name__)
_recovery_observers: set[Callable[[], object]] = set()


def register_camera_recovery(observer: Callable[[], object]) -> Callable[[], None]:
    _recovery_observers.add(observer)
    return lambda: _recovery_observers.discard(observer)


CameraPurpose = Literal[
    "snapshot",
    "diagnose",
    "finish_photo",
    "finish_bank",
    "obico",
    "octoeverywhere",
    "plate_check",
    "layer_timelapse",
    "telegram",
    "cloud_link",
]


@dataclass(frozen=True, kw_only=True)
class CameraCaptureRequest:
    """Validated description of one caller's capture need.

    This is intentionally an in-memory request.  The worker IPC schema gets a
    separate, versioned representation: credentials must never be accidentally
    serialized by a debug ``repr`` or a generic dataclass encoder.
    """

    kind: Literal["builtin", "external"]
    purpose: CameraPurpose
    timeout: int
    ip_address: str | None = field(default=None, repr=False)
    access_code: str | None = field(default=None, repr=False)
    model: str | None = None
    url: str | None = field(default=None, repr=False)
    camera_type: str | None = None
    snapshot_url: str | None = field(default=None, repr=False)
    #: The printer this frame is for. The chamber-light lease keys on it
    #: (services/camera_light); ``None`` means no lease. In-memory only —
    #: the worker command copies its fields by name and never sees this.
    printer_id: int | None = None
    #: A poller's declared hold on the light, seconds (camera_light.hold_for_poll).
    hold: float | None = None
    #: Whose live view answers instead of a second reader (see :func:`capture`). A printer's view is named by
    #: ``printer_id``; a caller that holds the light itself names ``view_printer_id`` alone. In-memory only.
    view_printer_id: int | None = None
    #: The standalone camera (``cameras`` row) this frame is of — its live view answers the same way.
    camera_id: int | None = None

    @classmethod
    def builtin(
        cls,
        *,
        ip_address: str,
        access_code: str,
        model: str | None,
        timeout: int = 15,
        purpose: CameraPurpose = "snapshot",
        printer_id: int | None = None,
        hold: float | None = None,
        view_printer_id: int | None = None,
    ) -> CameraCaptureRequest:
        return cls(
            kind="builtin",
            purpose=purpose,
            timeout=timeout,
            ip_address=ip_address,
            access_code=access_code,
            model=model,
            printer_id=printer_id,
            hold=hold,
            view_printer_id=view_printer_id,
        )

    @classmethod
    def external(
        cls,
        *,
        url: str,
        camera_type: str,
        snapshot_url: str | None = None,
        timeout: int = 15,
        purpose: CameraPurpose = "snapshot",
        printer_id: int | None = None,
        hold: float | None = None,
        view_printer_id: int | None = None,
        camera_id: int | None = None,
    ) -> CameraCaptureRequest:
        return cls(
            kind="external",
            purpose=purpose,
            timeout=timeout,
            url=url,
            camera_type=camera_type,
            snapshot_url=snapshot_url,
            printer_id=printer_id,
            hold=hold,
            view_printer_id=view_printer_id,
            camera_id=camera_id,
        )

    def same_source(self, other: CameraCaptureRequest) -> bool:
        """Compare camera configuration, excluding caller timeout and light purpose."""
        return (
            self.kind,
            self.ip_address,
            self.access_code,
            self.model,
            self.url,
            self.camera_type,
            self.snapshot_url,
        ) == (
            other.kind,
            other.ip_address,
            other.access_code,
            other.model,
            other.url,
            other.camera_type,
            other.snapshot_url,
        )

    def __post_init__(self) -> None:
        if not 1 <= self.timeout <= 120:
            raise ValueError("camera capture timeout must be between 1 and 120 seconds")
        if self.hold is not None and self.hold < 0:
            raise ValueError("camera light hold cannot be negative")
        if self.kind == "builtin":
            if not self.ip_address or self.access_code is None:
                raise ValueError("built-in camera capture requires address and access code")
            if self.url is not None or self.camera_type is not None or self.snapshot_url is not None:
                raise ValueError("built-in camera capture cannot contain an external camera URL")
            return
        if not self.url or not self.camera_type:
            raise ValueError("external camera capture requires URL and type")
        if self.ip_address is not None or self.access_code is not None or self.model is not None:
            raise ValueError("external camera capture cannot contain built-in camera fields")
        if self.camera_id is not None and (self.printer_id is not None or self.view_printer_id is not None):
            raise ValueError("a capture is of a printer's camera or of a standalone camera, never both")


class CameraRuntime(Protocol):
    async def capture(self, request: CameraCaptureRequest) -> CameraCaptureResult: ...


@dataclass
class WorkerCameraRuntime:
    """The only production adapter for physical camera operations."""

    supervisor: CameraWorkerSupervisor

    async def capture(self, request: CameraCaptureRequest) -> CameraCaptureResult:
        return await self.supervisor.capture(request)

    async def probe_tcp(self, host: str, port: int, timeout: float, *, identity: str) -> str:
        reply = await self.supervisor.request(
            "probe_tcp", {"host": host, "port": port, "timeout": timeout, "identity": identity}, timeout=timeout + 2
        )
        if not reply["ok"]:
            raise CameraWorkerUnavailable("camera worker rejected TCP probe")
        return reply["result"]["code"]

    async def stream_external(
        self,
        *,
        identity: str,
        url: str,
        camera_type: str,
        fps: int,
        disconnect_event: asyncio.Event,
        on_frame: Callable[[bytes], None] | None = None,
        on_still_scene: Callable[[bool], None] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Yield a worker-owned external camera stream as standard MJPEG parts.

        The HTTP layer still owns browser disconnect detection and its normal
        fan-out lifecycle.  This adapter only bridges its one physical source
        to that fan-out; it never exposes worker protocol frames to a client.
        ``identity`` is a stable UUID for the physical source, not a URL, so a
        credential rotation cannot accidentally create a second producer.
        """

        # Validate it here too: callers must not use an endpoint URL as the
        # worker identity, which would make credentials part of a registry key.
        uuid.UUID(identity)
        supervisor = self.supervisor
        lease_id, queue = await supervisor.subscribe_external(
            identity=identity,
            url=url,
            camera_type=camera_type,
            fps=fps,
        )
        try:
            from backend.app.services.external_camera import format_mjpeg_frame

            while not disconnect_event.is_set():
                try:
                    media = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if media is None:
                    return
                # The worker's verdict on this frame's source, before the frame itself.
                if on_still_scene is not None:
                    on_still_scene(media.still_scene)
                if on_frame is not None:
                    on_frame(media.frame)
                yield format_mjpeg_frame(media.frame)
        finally:
            await supervisor.unsubscribe(lease_id, queue)

    async def stream_builtin(
        self,
        *,
        identity: str,
        ip_address: str,
        access_code: str,
        model: str | None,
        fps: int,
        disconnect_event: asyncio.Event,
        on_frame: Callable[[bytes], None] | None = None,
        on_still_scene: Callable[[bool], None] | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Yield worker-owned Bambu chamber/RTSPS frames as MJPEG parts."""

        uuid.UUID(identity)
        supervisor = self.supervisor
        lease_id, queue = await supervisor.subscribe_builtin(
            identity=identity,
            ip_address=ip_address,
            access_code=access_code,
            model=model,
            fps=fps,
        )
        try:
            from backend.app.services.external_camera import format_mjpeg_frame

            while not disconnect_event.is_set():
                try:
                    media = await asyncio.wait_for(queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                if media is None:
                    return
                # The worker's verdict on this frame's source, before the frame itself.
                if on_still_scene is not None:
                    on_still_scene(media.still_scene)
                if on_frame is not None:
                    on_frame(media.frame)
                yield format_mjpeg_frame(media.frame)
        finally:
            await supervisor.unsubscribe(lease_id, queue)

    async def start_raw_proxy(
        self,
        *,
        identity: str,
        bind_address: str,
        listen_port: int,
        target_host: str,
        target_port: int,
    ) -> str:
        """Delegate Virtual Printer's byte-for-byte camera endpoint to the worker."""

        uuid.UUID(identity)
        return await self.supervisor.start_raw_proxy(
            identity=identity,
            bind_address=bind_address,
            listen_port=listen_port,
            target_host=target_host,
            target_port=target_port,
        )

    async def stop_raw_proxy(self, lease_id: str) -> None:
        await self.supervisor.stop_raw_proxy(lease_id)

    async def stop(self) -> None:
        await self.supervisor.stop()


@dataclass
class CameraRuntimeOwner:
    """Lifespan owner with bounded, camera-only process recovery."""

    runtime: WorkerCameraRuntime
    state: str = "stopped"
    reason: str | None = None
    restart_count: int = 0
    next_retry_at: datetime | None = None
    _task: asyncio.Task[None] | None = None
    _stopping: asyncio.Event = field(default_factory=asyncio.Event)
    _first_attempt: asyncio.Future[None] | None = None
    _failures: deque[float] = field(default_factory=deque)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self.state = "starting"
        self._first_attempt = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(), name="camera-worker-owner")
        await self._first_attempt

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.state = "stopped"
        self.next_retry_at = None

    async def _run(self) -> None:
        half_open = False
        try:
            while not self._stopping.is_set():
                supervisor = CameraWorkerSupervisor()
                self.runtime.supervisor = supervisor
                self.state = "starting" if self.restart_count == 0 else "recovering"
                self.next_retry_at = None
                stop_cause = "owner_stop"
                try:
                    await asyncio.wait_for(supervisor.start(), timeout=20)
                    self.state = "ready"
                    self.reason = None
                    half_open = False
                    if self._first_attempt is not None and not self._first_attempt.done():
                        self._first_attempt.set_result(None)
                    for observer in tuple(_recovery_observers):
                        try:
                            result = observer()
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as exc:
                            logger.warning("Camera proxy reconciliation failed: %s", type(exc).__name__)
                    while not self._stopping.is_set():
                        try:
                            await asyncio.wait_for(self._stopping.wait(), timeout=3)
                        except TimeoutError:
                            if supervisor.process is None or supervisor.process.returncode is not None:
                                raise CameraWorkerUnavailable("camera worker exited")
                            await supervisor.request("heartbeat", timeout=2)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    stop_cause = (
                        "unresponsive"
                        if isinstance(exc, (CameraWorkerUnavailable, TimeoutError))
                        else "startup_failure"
                    )
                    self.state = "unavailable"
                    self.reason = type(exc).__name__
                    logger.warning("Camera worker unavailable (%s); print service remains available", self.reason)
                finally:
                    try:
                        await supervisor.stop(cause=stop_cause)
                    except Exception as exc:
                        self.state = "unavailable"
                        self.reason = "cleanup_failed"
                        logger.error("Camera worker cleanup is uncertain: %s", type(exc).__name__)
                        break
                    if self._first_attempt is not None and not self._first_attempt.done():
                        self._first_attempt.set_result(None)
                if self._stopping.is_set():
                    break
                delay, half_open = self._restart_delay(time.monotonic(), half_open=half_open)
                self.restart_count += 1
                self.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            if self._first_attempt is not None and not self._first_attempt.done():
                self._first_attempt.set_result(None)

    def _restart_delay(self, now: float, *, half_open: bool) -> tuple[float, bool]:
        while self._failures and now - self._failures[0] >= 60:
            self._failures.popleft()
        if half_open or len(self._failures) >= 3:
            self._failures.clear()
            self.state = "unavailable"
            return 60.0, True
        delay = float(2 ** len(self._failures))
        self._failures.append(now)
        self.state = "recovering"
        return delay, False

    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "reason": self.reason,
            "restart_count": self.restart_count,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
        }


_configured_runtime = WorkerCameraRuntime(CameraWorkerSupervisor())
_owner: CameraRuntimeOwner | None = None
_runtime_override: ContextVar[CameraRuntime | None] = ContextVar("camera_runtime_override", default=None)


def get_camera_runtime() -> CameraRuntime:
    return _runtime_override.get() or _configured_runtime


async def configure_camera_runtime() -> None:
    """Start the only physical owner; degraded cameras never block printing."""

    global _owner
    if _owner is None:
        if os.environ.get("CAMERA_RUNTIME") is not None:
            logger.warning("CAMERA_RUNTIME is obsolete and ignored; cameras always use the isolated worker")
        _owner = CameraRuntimeOwner(_configured_runtime)
    await _owner.start()


async def stop_configured_camera_runtime() -> None:
    """Release the worker tree before the app tears down camera dependencies."""

    global _owner
    if _owner is not None:
        await _owner.stop()
        _owner = None


def camera_runtime_health() -> dict:
    return (
        _owner.snapshot()
        if _owner is not None
        else {"state": "stopped", "reason": None, "restart_count": 0, "next_retry_at": None}
    )


#: How long a one-shot capture waits for the first frame of a live view that is still starting (or between
#: two sessions) before it answers "busy". Never longer than the caller's own timeout.
LIVE_VIEW_FIRST_FRAME_WAIT = 3.0


def _live_view_probe(request: CameraCaptureRequest) -> Callable[[], tuple[bool, bytes | None]] | None:
    """The question "is this camera being watched, and what has the view got" for the camera a request names.

    The frame buffers live with the HTTP relays that fill them (``routes/camera`` for a printer's camera,
    ``routes/cameras`` for a standalone one); the imports are late because those modules import this one.
    """
    if request.camera_id is not None:
        from backend.app.api.routes import cameras

        camera_id = request.camera_id
        return lambda: cameras.live_frame_for_capture(camera_id)
    printer_id = request.view_printer_id if request.view_printer_id is not None else request.printer_id
    if printer_id is None:
        return None
    from backend.app.api.routes import camera

    kind = request.kind
    return lambda: camera.live_view_frame(printer_id, kind)


async def frame_from_live_view(request: CameraCaptureRequest) -> tuple[bool, bytes | None]:
    """``(watched, frame)`` — the single-reader rule every one-shot capture obeys.

    Most firmware allows ONE camera connection and a USB camera one handle, so a capture beside a live view
    does not degrade: it fails, or it drops the viewer (``40-invariants/inv-single-camera-socket``).
    ``watched`` True means the worker must NOT be asked: ``frame`` is the view's frame, or None when the view
    delivered none within :data:`LIVE_VIEW_FIRST_FRAME_WAIT` — "busy", for the caller to report as its
    contract says. A view that leaves during the wait hands the camera back, and the caller captures.

    A request that carries a snapshot URL is not this function's to hold back — :func:`capture` fetches the
    still first and asks here only when that failed; a camera TEST of such a camera is of its stream and is
    answered here like any other.
    """
    probe = _live_view_probe(request)
    if probe is None:
        return False, None
    deadline = time.monotonic() + min(LIVE_VIEW_FIRST_FRAME_WAIT, request.timeout)
    while True:
        watched, frame = probe()
        if not watched:
            return False, None
        if frame is not None:
            return True, frame
        if time.monotonic() >= deadline:
            logger.info(
                "Camera capture (%s) stood down: a live view holds the camera and has no frame yet", request.purpose
            )
            return True, None
        await asyncio.sleep(0.05)


async def capture(request: CameraCaptureRequest) -> CameraCaptureResult:
    """Capture through the selected runtime without exposing implementation.

    A camera that is being watched is never dialled a second time: a request that names its camera (a
    printer's by ``printer_id`` / ``view_printer_id``, a standalone one by ``camera_id``) is answered from
    the live view — see :func:`frame_from_live_view`. The result then says ``source="live_view"``, with no
    frame when the view had none to give. This is the one place the rule lives; a caller does not repeat it.

    A camera with its own snapshot URL answers with that still even while it is watched (owner, 2026-11-15;
    upstream 7cea7acf0 / cf0ba31f1): a plain HTTP GET the worker makes beside the stream — full quality, and
    no second reader of the stream. Only when the still cannot be fetched does the view's frame answer; the
    worker is never asked for anything but the still while the view holds the camera.

    The chamber light is taken here, in the main process, whichever runtime
    does the capture: a request that names its printer holds the light for
    the frame and waits for the printer to confirm it before the capture
    (services/camera_light — off unless the farm or the printer asks for it).

    A built-in request for a printer whose live client said it has no camera is answered empty before any of
    that (services/camera_capability) — except a diagnosis, which the operator asked for and which really tries.
    """

    from backend.app.services import camera_capability, camera_light

    if _has_own_still(request) and _is_watched(request) and not _still_failed_lately(request):
        return await _still_beside_live_view(request)
    watched, live_frame = await frame_from_live_view(request)
    if watched:
        # No light of our own either: the view that holds the camera holds its light.
        return CameraCaptureResult(frame=live_frame, source="live_view")
    if (
        request.kind == "builtin"
        and request.printer_id is not None
        and request.purpose != "diagnose"
        and camera_capability.skip_builtin_capture(request.printer_id, request.ip_address, request.access_code)
    ):
        return CameraCaptureResult(frame=None, source=None)
    async with camera_light.held(request.printer_id, request.purpose, hold=request.hold) as lease:
        if lease is not None:
            await lease.settle()
        return await get_camera_runtime().capture(request)


def _is_watched(request: CameraCaptureRequest) -> bool:
    probe = _live_view_probe(request)
    return probe is not None and probe()[0]


def _has_own_still(request: CameraCaptureRequest) -> bool:
    """A snapshot URL that is the stream's own address is no still: fetching it beside the view would be a
    second reader of a source that may serve one client (saves refuse the pair; this is the backstop)."""
    from backend.app.services.camera_credentials import same_camera_address

    return bool(request.snapshot_url) and not same_camera_address(request.snapshot_url, request.url)


#: How long a WATCHED camera's still may take when its view already has a frame to answer with. A dead or
#: slow frame endpoint beside a healthy stream must not cost every snapshot tile, notification photo and
#: timelapse layer the request's whole timeout.
STILL_BESIDE_VIEW_WAIT = 4.0
#: How long a still that failed or ran out of that time is not asked again while the camera is watched; the
#: view answers at once meanwhile. One retry after it; the first success, or another snapshot URL, clears it.
STILL_FAILURE_MEMORY = 45.0
# camera -> (the snapshot URL that failed, monotonic time until which it is not asked). In memory only.
_failed_stills: dict[tuple[str, int], tuple[str, float]] = {}


def _still_key(request: CameraCaptureRequest) -> tuple[str, int] | None:
    if request.camera_id is not None:
        return "camera", request.camera_id
    printer_id = request.view_printer_id if request.view_printer_id is not None else request.printer_id
    return None if printer_id is None else ("printer", printer_id)


def _still_failed_lately(request: CameraCaptureRequest) -> bool:
    key = _still_key(request)
    remembered = _failed_stills.get(key) if key is not None else None
    if remembered is None:
        return False
    if remembered[0] != request.snapshot_url or time.monotonic() >= remembered[1]:
        del _failed_stills[key]
        return False
    return True


async def _still_beside_live_view(request: CameraCaptureRequest) -> CameraCaptureResult:
    """The still of a watched camera that has its own snapshot URL; the view's frame when it cannot be had.

    The request carries the snapshot URL, so the worker fetches that and opens no stream reader
    (``external_camera._capture_frame_uncoalesced``). The light is taken as for any capture: a view lights
    the chamber only when the farm asks it to, and a picture somebody keeps is taken in light either way.

    The still is bounded and its failure remembered (review I2): beside a view that already has a frame it
    gets :data:`STILL_BESIDE_VIEW_WAIT`, and a failed or late one is not asked again for
    :data:`STILL_FAILURE_MEMORY` (see :func:`capture`, which then answers from the view at once).
    """
    from backend.app.services import camera_light

    result: CameraCaptureResult | None = None
    failure: Exception | None = None
    key = _still_key(request)
    probe = _live_view_probe(request)
    try:
        async with camera_light.held(request.printer_id, request.purpose, hold=request.hold) as lease:
            if lease is not None:
                await lease.settle()
            if probe is not None and probe()[1] is not None:
                # The view has a frame to answer with: the still gets a short time of its own (the light's
                # settling is not counted against it).
                result = await asyncio.wait_for(get_camera_runtime().capture(request), STILL_BESIDE_VIEW_WAIT)
            else:
                result = await get_camera_runtime().capture(request)
    except Exception as exc:
        failure = exc
    if result is not None and result.frame:
        if key is not None:
            _failed_stills.pop(key, None)
        return result
    if key is not None and request.snapshot_url:
        _failed_stills[key] = (request.snapshot_url, time.monotonic() + STILL_FAILURE_MEMORY)
    watched, live_frame = await frame_from_live_view(request)
    if watched:
        logger.info(
            "Camera capture (%s): the camera's own still could not be fetched (%s); answered from its live view",
            request.purpose,
            type(failure).__name__ if failure is not None else "no frame",
        )
        return CameraCaptureResult(frame=live_frame, source="live_view")
    if failure is not None:
        raise failure
    return result


#: What a camera test answers when a live view holds the camera and has not delivered a frame yet.
CAMERA_BUSY_WITH_LIVE_VIEW = "The camera is in use by its live view, which has no picture yet. Try again in a moment."


def _jpeg_resolution(frame: bytes) -> str | None:
    for marker in (b"\xff\xc0", b"\xff\xc1", b"\xff\xc2"):
        index = frame.find(marker)
        if index >= 0 and index + 9 <= len(frame):
            height = (frame[index + 5] << 8) | frame[index + 6]
            width = (frame[index + 7] << 8) | frame[index + 8]
            return f"{width}x{height}"
    return None


async def test_on_live_view(request: CameraCaptureRequest) -> dict | None:
    """A camera test of a camera that is being watched, answered from its live view; None when nobody watches.

    The Test button beside a camera's settings must not dial a second time while the Camera page is open on
    that camera — on a USB camera that reads as "broken" (upstream #2707). ``request`` names the camera as a
    capture would; the caller passes it only for the SAVED source, since a draft address is another device.
    """
    watched, frame = await frame_from_live_view(request)
    if not watched:
        return None
    if frame is None:
        return {"success": False, "error": CAMERA_BUSY_WITH_LIVE_VIEW, "live_view": True}
    return {"success": True, "resolution": _jpeg_resolution(frame), "live_view": True}


async def test_external_connection(url: str, camera_type: str) -> dict:
    """Probe a proposed external source through the worker, never in main."""
    try:
        result = await capture(
            CameraCaptureRequest.external(url=url, camera_type=camera_type, timeout=10, purpose="diagnose")
        )
        frame = result.frame
        if not frame:
            return {"success": False, "error": "Failed to capture frame from camera"}
        return {"success": True, "resolution": _jpeg_resolution(frame)}
    except Exception as exc:
        logger.warning("External camera test failed: %s", type(exc).__name__)
        return {"success": False, "error": f"Connection failed: {type(exc).__name__}"}


@contextmanager
def override_camera_runtime(runtime: CameraRuntime) -> Iterator[None]:
    """Scope a test/runtime experiment without leaking into other tasks."""

    token = _runtime_override.set(runtime)
    try:
        yield
    finally:
        _runtime_override.reset(token)
