"""Stable in-process boundary for one-shot camera capture.

The current implementation is deliberately inline.  Callers use this module so
the future worker can take ownership of physical camera connections without
changing every consumer at once.  It must stay free of FastAPI, database, MQTT,
browser-token and worker-process imports.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from backend.app.services.camera_metrics import CameraCaptureResult

if TYPE_CHECKING:
    from backend.app.services.camera_worker_supervisor import CameraWorkerSupervisor

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

    @classmethod
    def builtin(
        cls,
        *,
        ip_address: str,
        access_code: str,
        model: str | None,
        timeout: int = 15,
        purpose: CameraPurpose = "snapshot",
    ) -> CameraCaptureRequest:
        return cls(
            kind="builtin",
            purpose=purpose,
            timeout=timeout,
            ip_address=ip_address,
            access_code=access_code,
            model=model,
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
    ) -> CameraCaptureRequest:
        return cls(
            kind="external",
            purpose=purpose,
            timeout=timeout,
            url=url,
            camera_type=camera_type,
            snapshot_url=snapshot_url,
        )

    def __post_init__(self) -> None:
        if not 1 <= self.timeout <= 120:
            raise ValueError("camera capture timeout must be between 1 and 120 seconds")
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


class InlineCameraRuntime:
    """The existing capture paths behind the worker-ready interface."""

    async def capture(self, request: CameraCaptureRequest) -> CameraCaptureResult:
        if request.kind == "builtin":
            from backend.app.services.camera import capture_camera_frame_with_provenance

            return await capture_camera_frame_with_provenance(
                request.ip_address or "",
                request.access_code or "",
                request.model,
                request.timeout,
            )

        from backend.app.services.external_camera import capture_frame_with_provenance

        return await capture_frame_with_provenance(
            request.url or "",
            request.camera_type or "",
            request.timeout,
            request.snapshot_url,
        )


@dataclass
class WorkerCameraRuntime:
    """Explicit test/rollout adapter for the supervised capture worker.

    Nothing selects this adapter globally yet.  Application startup will add a
    validated runtime setting only after the worker's producer and relay gates
    have passed on supported hosts.
    """

    supervisor: CameraWorkerSupervisor

    async def capture(self, request: CameraCaptureRequest) -> CameraCaptureResult:
        return await self.supervisor.capture(request)

    async def stop(self) -> None:
        await self.supervisor.stop()


_inline_runtime = InlineCameraRuntime()
_configured_runtime: CameraRuntime = _inline_runtime
_runtime_override: ContextVar[CameraRuntime | None] = ContextVar("camera_runtime_override", default=None)


def get_camera_runtime() -> CameraRuntime:
    return _runtime_override.get() or _configured_runtime


async def configure_camera_runtime(mode: Literal["inline", "worker"]) -> None:
    """Select one process-wide physical camera owner at application startup."""

    global _configured_runtime
    if mode == "inline":
        if isinstance(_configured_runtime, WorkerCameraRuntime):
            await _configured_runtime.stop()
        _configured_runtime = _inline_runtime
        return
    if isinstance(_configured_runtime, WorkerCameraRuntime):
        return
    from backend.app.services.camera_worker_supervisor import CameraWorkerSupervisor

    runtime = WorkerCameraRuntime(CameraWorkerSupervisor())
    await runtime.supervisor.start()
    _configured_runtime = runtime


async def stop_configured_camera_runtime() -> None:
    """Release the worker tree before the app tears down camera dependencies."""

    await configure_camera_runtime("inline")


async def capture(request: CameraCaptureRequest) -> CameraCaptureResult:
    """Capture through the selected runtime without exposing implementation."""

    return await get_camera_runtime().capture(request)


@contextmanager
def override_camera_runtime(runtime: CameraRuntime) -> Iterator[None]:
    """Scope a test/runtime experiment without leaking into other tasks."""

    token = _runtime_override.set(runtime)
    try:
        yield
    finally:
        _runtime_override.reset(token)
