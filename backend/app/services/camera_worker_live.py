"""Worker-side ownership for one live producer and bounded frame fan-out."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from backend.app.services.camera_worker_protocol import CameraWorkerProtocolError

MAX_LIVE_SUBSCRIBERS = 64
# ``bytes`` queues use a zero-length value solely as the terminal marker.  A
# valid JPEG is always non-empty and is validated again at the media boundary.
LIVE_STREAM_ENDED = b""


@dataclass(frozen=True)
class LiveLease:
    identity: str
    lease_id: str


@dataclass(frozen=True)
class RawCameraLease:
    identity: str
    lease_id: str


@dataclass(frozen=True, kw_only=True)
class RawProxyCommand:
    """Validated transparent TCP lease for a Virtual Printer camera endpoint."""

    identity: str
    bind_address: str
    listen_port: int
    target_host: str = field(repr=False)
    target_port: int

    @classmethod
    def from_payload(cls, payload: dict) -> RawProxyCommand:
        if set(payload) != {"identity", "bind_address", "listen_port", "target_host", "target_port"}:
            raise CameraWorkerProtocolError("raw proxy has an invalid schema")
        try:
            return cls(**payload)
        except (TypeError, ValueError) as exc:
            raise CameraWorkerProtocolError("raw proxy is invalid") from exc

    def __post_init__(self) -> None:
        try:
            uuid.UUID(self.identity)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("raw proxy identity is invalid") from exc
        for value, label in ((self.bind_address, "bind address"), (self.target_host, "target host")):
            if not isinstance(value, str) or not 0 < len(value) <= 255 or any(c in value for c in "\r\n"):
                raise ValueError(f"raw proxy {label} is invalid")
        for value in (self.listen_port, self.target_port):
            if not isinstance(value, int) or not 1 <= value <= 65535:
                raise ValueError("raw proxy port is invalid")


@dataclass(frozen=True, kw_only=True)
class LiveBuiltinSubscription:
    """Validated worker command for a physical Bambu live-camera source."""

    identity: str
    media_session_id: str
    ip_address: str = field(repr=False)
    access_code: str = field(repr=False)
    model: str | None
    fps: int

    @classmethod
    def from_payload(cls, payload: dict) -> LiveBuiltinSubscription:
        expected = {"identity", "media_session_id", "ip_address", "access_code", "model", "fps"}
        if set(payload) != expected:
            raise CameraWorkerProtocolError("built-in live subscription has an invalid schema")
        try:
            return cls(**payload)
        except (TypeError, ValueError) as exc:
            raise CameraWorkerProtocolError("built-in live subscription is invalid") from exc

    def __post_init__(self) -> None:
        for value in (self.identity, self.media_session_id):
            try:
                uuid.UUID(value)
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValueError("built-in live subscription identity is invalid") from exc
        if not isinstance(self.ip_address, str) or not 0 < len(self.ip_address) <= 255:
            raise ValueError("built-in live subscription address is invalid")
        if not isinstance(self.access_code, str) or len(self.access_code) > 128:
            raise ValueError("built-in live subscription access code is invalid")
        if self.model is not None and (not isinstance(self.model, str) or len(self.model) > 128):
            raise ValueError("built-in live subscription model is invalid")
        if not isinstance(self.fps, int) or not 1 <= self.fps <= 30:
            raise ValueError("built-in live subscription FPS is invalid")


@dataclass(frozen=True, kw_only=True)
class LiveExternalSubscription:
    """Validated worker command for one external physical producer identity."""

    identity: str
    media_session_id: str
    url: str = field(repr=False)
    camera_type: str
    fps: int

    @classmethod
    def from_payload(cls, payload: dict) -> LiveExternalSubscription:
        if set(payload) != {"identity", "media_session_id", "url", "camera_type", "fps"}:
            raise CameraWorkerProtocolError("live subscription has an invalid schema")
        try:
            return cls(**payload)
        except (TypeError, ValueError) as exc:
            raise CameraWorkerProtocolError("live subscription is invalid") from exc

    def __post_init__(self) -> None:
        for value in (self.identity, self.media_session_id):
            try:
                uuid.UUID(value)
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValueError("live subscription identity is invalid") from exc
        if not isinstance(self.url, str) or not 0 < len(self.url) <= 4096:
            raise ValueError("live subscription URL is invalid")
        if self.camera_type not in {"mjpeg", "rtsp", "snapshot", "usb"}:
            raise ValueError("live subscription camera type is invalid")
        if not isinstance(self.fps, int) or not 1 <= self.fps <= 30:
            raise ValueError("live subscription FPS is invalid")


class LiveProducerRegistry:
    """One producer per physical identity; each subscriber keeps only its latest frame."""

    def __init__(self) -> None:
        self._producers: dict[str, asyncio.Task[None]] = {}
        self._subscribers: dict[str, dict[str, asyncio.Queue[bytes]]] = {}
        self._latest: dict[str, bytes] = {}
        self._raw_leases: dict[str, RawCameraLease] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self, identity: str, start: Callable[[], Awaitable[None]]
    ) -> tuple[LiveLease, asyncio.Queue[bytes]]:
        async with self._lock:
            if identity in self._raw_leases:
                raise RuntimeError("camera worker raw lease is active")
            subscribers = self._subscribers.setdefault(identity, {})
            if len(subscribers) >= MAX_LIVE_SUBSCRIBERS:
                raise RuntimeError("camera worker live subscriber limit reached")
            if identity not in self._producers:
                task = asyncio.create_task(start(), name=f"camera-worker-live-{uuid.uuid4().hex[:12]}")
                self._producers[identity] = task
                task.add_done_callback(lambda completed, key=identity: self._producer_finished(key, completed))
            lease = LiveLease(identity, str(uuid.uuid4()))
            queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1)
            subscribers[lease.lease_id] = queue
            if frame := self._latest.get(identity):
                queue.put_nowait(frame)
            return lease, queue

    async def acquire_raw(self, identity: str) -> RawCameraLease:
        """Reserve an identity for transparent VP TCP, never JPEG relay."""

        async with self._lock:
            if identity in self._raw_leases or self._subscribers.get(identity) or identity in self._producers:
                raise RuntimeError("camera worker identity is busy")
            lease = RawCameraLease(identity, str(uuid.uuid4()))
            self._raw_leases[identity] = lease
            return lease

    async def release_raw(self, lease: RawCameraLease) -> None:
        async with self._lock:
            if self._raw_leases.get(lease.identity) == lease:
                self._raw_leases.pop(lease.identity, None)

    async def unsubscribe(self, lease: LiveLease) -> None:
        producer: asyncio.Task[None] | None = None
        async with self._lock:
            subscribers = self._subscribers.get(lease.identity)
            if not subscribers or subscribers.pop(lease.lease_id, None) is None:
                return
            if subscribers:
                return
            self._subscribers.pop(lease.identity, None)
            self._latest.pop(lease.identity, None)
            producer = self._producers.pop(lease.identity, None)
            if producer is not None:
                producer.cancel()

        # A cancelled producer may own an ffmpeg process/socket in its
        # ``finally``.  Wait for that cleanup outside the registry lock so a
        # new, unrelated lease is never blocked behind it.
        if producer is not None:
            await asyncio.gather(producer, return_exceptions=True)

    def publish(self, identity: str, frame: bytes) -> None:
        self._latest[identity] = frame
        for queue in self._subscribers.get(identity, {}).values():
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

    def _producer_finished(self, identity: str, task: asyncio.Task[None]) -> None:
        if self._producers.get(identity) is task:
            self._producers.pop(identity, None)
            for queue in self._subscribers.get(identity, {}).values():
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(LIVE_STREAM_ENDED)
