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
    "obico",
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


async def capture(request: CameraCaptureRequest) -> CameraCaptureResult:
    """Capture through the selected runtime without exposing implementation.

    The chamber light is taken here, in the main process, whichever runtime
    does the capture: a request that names its printer holds the light for
    the frame and waits for the printer to confirm it before the capture
    (services/camera_light — off unless the farm or the printer asks for it).
    """

    from backend.app.services import camera_light

    async with camera_light.held(request.printer_id, request.purpose, hold=request.hold) as lease:
        if lease is not None:
            await lease.settle()
        return await get_camera_runtime().capture(request)


async def test_external_connection(url: str, camera_type: str) -> dict:
    """Probe a proposed external source through the worker, never in main."""
    try:
        result = await capture(
            CameraCaptureRequest.external(url=url, camera_type=camera_type, timeout=10, purpose="diagnose")
        )
        frame = result.frame
        if not frame:
            return {"success": False, "error": "Failed to capture frame from camera"}
        resolution = None
        for marker in (b"\xff\xc0", b"\xff\xc1", b"\xff\xc2"):
            index = frame.find(marker)
            if index >= 0 and index + 9 <= len(frame):
                height = (frame[index + 5] << 8) | frame[index + 6]
                width = (frame[index + 7] << 8) | frame[index + 8]
                resolution = f"{width}x{height}"
                break
        return {"success": True, "resolution": resolution}
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
