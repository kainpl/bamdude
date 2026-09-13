"""Bounded camera evidence. No frames, owners, URLs or credentials are retained.

The producer owns timings; joining callers never restart its clock. Active
records live with their producers, and completed evidence has a fixed LRU cap.
All mutations run on the camera event loop, without I/O or per-frame logging.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import logging
import re
import time
import uuid
from collections import OrderedDict, deque
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)
_MAX_COMPLETED = 128
_MAX_COUNTER = 2**53 - 1
_active: dict[str, CameraMetrics] = {}
_completed: OrderedDict[str, CameraMetrics] = OrderedDict()
_deliveries: OrderedDict[int, dict] = OrderedDict()
current: ContextVar[CameraMetrics | None] = ContextVar("camera_metrics", default=None)
_VIDEO = re.compile(r"Video:\s*([\w]+)[^\r\n]*?\b(\d{2,5})x(\d{2,5})\b")
_identity_salt = uuid.uuid4().bytes


@dataclass(frozen=True)
class CameraCaptureResult:
    frame: bytes | None
    source: Literal["fresh", "coalesced"] | None
    attempt_id: str | None = None
    first_frame_ms: float | None = None
    caller_wait_ms: float | None = None
    cleanup_ms: float | None = None

    def for_caller(self, started: float, *, shared: bool = False) -> CameraCaptureResult:
        return replace(
            self,
            source="coalesced" if shared and self.frame is not None else self.source,
            caller_wait_ms=round((time.monotonic() - started) * 1000, 3),
        )


class CameraMetrics:
    def __init__(
        self,
        protocol: str,
        *,
        session_id: str | None = None,
        printer_id: int | None = None,
        identity: str | None = None,
    ):
        self.session_id = session_id or f"camera-{uuid.uuid4().hex[:12]}"
        self.printer_id = printer_id
        self.protocol = protocol
        self.identity = identity
        self.started_at = datetime.now(UTC).isoformat()
        self.started = time.monotonic()
        self.attempt_id: str | None = None
        self.attempt_started: float | None = None
        self.first_frame_ms: float | None = None
        self.last_frame_at: float | None = None
        self.cleanup_ms: float | None = None
        self.attempts_total = 0
        self.reconnects_total = 0
        self.output_frames = 0
        self.consecutive_failures = 0
        self.subscriber_dropped_frames = 0
        self.end_reason: str | None = None
        self.ended: float | None = None
        self.source_codec: str | None = None
        self.source_resolution: list[int] | None = None
        self._frame_times: deque[float] = deque(maxlen=64)
        _active[self.session_id] = self

    def begin_attempt(self, *, reconnect: bool = True) -> None:
        if reconnect and self.attempts_total:
            self.reconnects_total = min(_MAX_COUNTER, self.reconnects_total + 1)
        self.attempts_total = min(_MAX_COUNTER, self.attempts_total + 1)
        # A capture task already has a unique ID; live attempts add an ordinal.
        self.attempt_id = self.session_id if self.attempts_total == 1 else f"{self.session_id}:{self.attempts_total}"
        self.attempt_started = time.monotonic()
        self.first_frame_ms = None
        self.cleanup_ms = None
        self.source_codec = None
        self.source_resolution = None

    def frame(self, frame: bytes, *, output: bool = True) -> None:
        if not (frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9")):
            return
        now = time.monotonic()
        if self.first_frame_ms is None and self.attempt_started is not None:
            self.first_frame_ms = round((now - self.attempt_started) * 1000, 3)
        self.last_frame_at = now
        if output:
            self.output_frames = min(_MAX_COUNTER, self.output_frames + 1)
            self._frame_times.append(now)

    def observe_stderr(self, text: str) -> None:
        match = _VIDEO.search(text)
        if match:
            self.source_codec = match[1][:32]
            self.source_resolution = [int(match[2]), int(match[3])]

    def snapshot(self) -> dict:
        now = time.monotonic()
        recent = [t for t in self._frame_times if now - t <= 10]
        fps = None
        if len(recent) > 1 and recent[-1] > recent[0]:
            fps = round((len(recent) - 1) / (recent[-1] - recent[0]), 3)
        return {
            "session_id": self.session_id,
            "attempt_id": self.attempt_id,
            "protocol": self.protocol,
            "started_at": self.started_at,
            "active": self.ended is None,
            "first_frame_ms": self.first_frame_ms,
            "frame_age_ms": round((now - self.last_frame_at) * 1000, 3) if self.last_frame_at is not None else None,
            "attempts_total": self.attempts_total,
            "reconnects_total": self.reconnects_total,
            "consecutive_failures": self.consecutive_failures,
            "cleanup_ms": self.cleanup_ms,
            "output_frames": self.output_frames,
            "output_fps": fps,
            "subscriber_dropped_frames": self.subscriber_dropped_frames,
            "end_reason": self.end_reason,
            "source_codec": self.source_codec,
            "source_resolution": self.source_resolution,
        }

    def finish(self, reason: str) -> None:
        if self.ended is not None:
            return
        self.ended = time.monotonic()
        self.end_reason = self.end_reason or reason
        if _active.get(self.session_id) is self:
            _active.pop(self.session_id, None)
        key = f"printer-{self.printer_id}" if self.printer_id is not None else self.identity or self.session_id
        _completed[key] = self
        _completed.move_to_end(key)
        while len(_completed) > _MAX_COMPLETED:
            _completed.popitem(last=False)
        logger.info("Camera session completed: %s", self.snapshot())


def for_printer(printer_id: int) -> dict | None:
    records = [m for m in _active.values() if m.printer_id == printer_id]
    metric = max(records, key=lambda m: m.started) if records else _completed.get(f"printer-{printer_id}")
    return metric.snapshot() if metric is not None else None


def drop_for_printer(printer_id: int) -> None:
    metric = next((m for m in _active.values() if m.printer_id == printer_id), None)
    if metric is not None:
        metric.subscriber_dropped_frames = min(_MAX_COUNTER, metric.subscriber_dropped_frames + 1)


def remember_delivery(printer_id: int, source: str | None, result: CameraCaptureResult | None = None) -> None:
    _deliveries[printer_id] = {
        "frame_source": source,
        "attempt_id": result.attempt_id if result else None,
        "first_frame_ms": result.first_frame_ms if result else None,
        "caller_wait_ms": result.caller_wait_ms if result else None,
        "cleanup_ms": result.cleanup_ms if result else None,
    }
    _deliveries.move_to_end(printer_id)
    while len(_deliveries) > _MAX_COMPLETED:
        _deliveries.popitem(last=False)


def delivery_for_printer(printer_id: int) -> dict | None:
    value = _deliveries.get(printer_id)
    return dict(value) if value is not None else None


def forget_printer(printer_id: int) -> None:
    _completed.pop(f"printer-{printer_id}", None)
    _deliveries.pop(printer_id, None)
    for metric in _active.values():
        if metric.printer_id == printer_id:
            metric.printer_id = None


def observed_stream(protocol: str):
    """Scope producer instrumentation without leaking ContextVars across yield.

    In particular, a consumer doing another capture between yields must not write
    into the live stream's record. Closing also resumes the generator in its scope.
    """

    def decorate(function):
        signature = inspect.signature(function)

        @functools.wraps(function)
        async def wrapped(*args, **kwargs):
            arguments = signature.bind(*args, **kwargs)
            arguments.apply_defaults()
            bound = arguments.arguments
            source_protocol = protocol
            if protocol == "external" and bound.get("camera_type") in {"rtsp", "mjpeg", "snapshot", "usb"}:
                source_protocol = f"external_{bound['camera_type']}"
            metric = CameraMetrics(
                source_protocol, session_id=bound.get("stream_id"), printer_id=bound.get("printer_id")
            )
            generator = function(*args, **kwargs)
            reason = "upstream_ended"
            try:
                while True:
                    token = current.set(metric)
                    try:
                        chunk = await anext(generator)
                    except StopAsyncIteration:
                        event = bound.get("disconnect_event") or bound.get("stop_event")
                        if event is not None and event.is_set():
                            reason = "client_disconnected"
                        break
                    finally:
                        current.reset(token)
                    yield chunk
            except (asyncio.CancelledError, GeneratorExit):
                reason = "client_disconnected"
                raise
            except Exception:
                reason = "upstream_error"
                raise
            finally:
                token = current.set(metric)
                try:
                    await generator.aclose()
                finally:
                    current.reset(token)
                    metric.finish(reason)

        return wrapped

    return decorate


def record_frame(frame: bytes, *, output: bool = True) -> bytes:
    metric = current.get()
    if metric is not None:
        metric.frame(frame, output=output)
    return frame


def captured_frame(frame: bytes) -> bytes:
    metric = current.get()
    return record_frame(frame, output=metric is None or metric.identity is not None)


async def capture_result(capture, protocol: str, identity: str) -> CameraCaptureResult:
    task = asyncio.current_task()
    key = hashlib.sha256(_identity_salt + identity.encode()).hexdigest()
    metric = CameraMetrics(protocol, session_id=task.get_name() if task else None, identity=key)
    token = current.set(metric)
    metric.begin_attempt()
    reason = "capture_failed"
    try:
        frame = await capture
        reason = "captured" if frame else "capture_failed"
        metric.consecutive_failures = 0 if frame else 1
        return CameraCaptureResult(
            frame, "fresh" if frame else None, metric.attempt_id, metric.first_frame_ms, cleanup_ms=metric.cleanup_ms
        )
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    finally:
        current.reset(token)
        metric.finish(reason)
