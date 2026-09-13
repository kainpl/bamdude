"""Worker-side ownership for one live producer and bounded frame fan-out."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

MAX_LIVE_SUBSCRIBERS = 64


@dataclass(frozen=True)
class LiveLease:
    identity: str
    lease_id: str


class LiveProducerRegistry:
    """One producer per physical identity; each subscriber keeps only its latest frame."""

    def __init__(self) -> None:
        self._producers: dict[str, asyncio.Task[None]] = {}
        self._subscribers: dict[str, dict[str, asyncio.Queue[bytes]]] = {}
        self._latest: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self, identity: str, start: Callable[[], Awaitable[None]]
    ) -> tuple[LiveLease, asyncio.Queue[bytes]]:
        async with self._lock:
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

    async def unsubscribe(self, lease: LiveLease) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(lease.identity)
            if not subscribers or subscribers.pop(lease.lease_id, None) is None:
                return
            if subscribers:
                return
            self._subscribers.pop(lease.identity, None)
            self._latest.pop(lease.identity, None)
            task = self._producers.pop(lease.identity, None)
            if task is not None:
                task.cancel()

    def publish(self, identity: str, frame: bytes) -> None:
        self._latest[identity] = frame
        for queue in self._subscribers.get(identity, {}).values():
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(frame)

    def _producer_finished(self, identity: str, task: asyncio.Task[None]) -> None:
        if self._producers.get(identity) is task:
            self._producers.pop(identity, None)
