"""Bounded, authenticated one-shot JPEG relay for the camera worker."""

from __future__ import annotations

import asyncio
import json
import secrets
import struct
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.app.services.camera_worker_protocol import PROTOCOL_VERSION, CameraWorkerProtocolError

MAX_MEDIA_HEADER_BYTES = 4 * 1024
MAX_MEDIA_JPEG_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, kw_only=True)
class MediaHello:
    generation: str
    session_id: str
    secret: bytes = field(repr=False)
    version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_uuid(self.generation, "media generation")
        _validate_uuid(self.session_id, "media session")
        if self.version != PROTOCOL_VERSION or len(self.secret) != 32:
            raise CameraWorkerProtocolError("media hello is invalid")


@dataclass(frozen=True, kw_only=True)
class WorkerMediaFrame:
    generation: str
    session_id: str
    frame: bytes = field(repr=False)
    source: str | None
    attempt_id: str | None
    first_frame_ms: float | None
    cleanup_ms: float | None

    def __post_init__(self) -> None:
        _validate_uuid(self.generation, "media generation")
        _validate_uuid(self.session_id, "media session")
        if not _is_jpeg(self.frame):
            raise CameraWorkerProtocolError("media frame is not a JPEG")
        if self.source not in {None, "fresh", "coalesced"}:
            raise CameraWorkerProtocolError("media frame source is invalid")
        if self.attempt_id is not None and (not isinstance(self.attempt_id, str) or len(self.attempt_id) > 128):
            raise CameraWorkerProtocolError("media frame attempt is invalid")
        for value in (self.first_frame_ms, self.cleanup_ms):
            if value is not None and (not isinstance(value, (int, float)) or value < 0 or value > 120_000):
                raise CameraWorkerProtocolError("media frame timing is invalid")


async def write_media_hello(writer, hello: MediaHello) -> None:
    await _write_json(
        writer,
        {
            "version": hello.version,
            "generation": hello.generation,
            "session_id": hello.session_id,
            "secret": hello.secret.hex(),
        },
        limit=MAX_MEDIA_HEADER_BYTES,
    )


async def read_media_hello(reader) -> MediaHello:
    payload = await _read_json(reader, limit=MAX_MEDIA_HEADER_BYTES)
    if set(payload) != {"version", "generation", "session_id", "secret"} or not isinstance(payload["secret"], str):
        raise CameraWorkerProtocolError("media hello has an invalid schema")
    try:
        secret = bytes.fromhex(payload["secret"])
    except ValueError as exc:
        raise CameraWorkerProtocolError("media hello secret is invalid") from exc
    return MediaHello(
        generation=payload["generation"], session_id=payload["session_id"], secret=secret, version=payload["version"]
    )


async def write_media_frame(writer, frame: WorkerMediaFrame) -> None:
    await _write_json(
        writer,
        {
            "generation": frame.generation,
            "session_id": frame.session_id,
            "source": frame.source,
            "attempt_id": frame.attempt_id,
            "first_frame_ms": frame.first_frame_ms,
            "cleanup_ms": frame.cleanup_ms,
        },
        limit=MAX_MEDIA_HEADER_BYTES,
    )
    writer.write(_frame(frame.frame, limit=MAX_MEDIA_JPEG_BYTES))
    await writer.drain()


async def read_media_frame(reader) -> WorkerMediaFrame:
    payload = await _read_json(reader, limit=MAX_MEDIA_HEADER_BYTES)
    expected = {"generation", "session_id", "source", "attempt_id", "first_frame_ms", "cleanup_ms"}
    if set(payload) != expected:
        raise CameraWorkerProtocolError("media frame has an invalid schema")
    return WorkerMediaFrame(
        generation=payload["generation"],
        session_id=payload["session_id"],
        source=payload["source"],
        attempt_id=payload["attempt_id"],
        first_frame_ms=payload["first_frame_ms"],
        cleanup_ms=payload["cleanup_ms"],
        frame=await _read_frame(reader, limit=MAX_MEDIA_JPEG_BYTES),
    )


def valid_media_hello(hello: MediaHello, *, generation: str, secret: bytes) -> bool:
    return hello.generation == generation and secrets.compare_digest(hello.secret, secret)


async def _write_json(writer, payload: dict[str, Any], *, limit: int) -> None:
    try:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CameraWorkerProtocolError("media message is not JSON serializable") from exc
    writer.write(_frame(encoded, limit=limit))
    await writer.drain()


async def _read_json(reader, *, limit: int) -> dict[str, Any]:
    data = await _read_frame(reader, limit=limit)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CameraWorkerProtocolError("media message is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CameraWorkerProtocolError("media message must be an object")
    return value


def _frame(data: bytes, *, limit: int) -> bytes:
    if not 0 < len(data) <= limit:
        raise CameraWorkerProtocolError("media message exceeds its size limit")
    return struct.pack(">I", len(data)) + data


async def _read_frame(reader, *, limit: int) -> bytes:
    try:
        header = await reader.readexactly(4)
        length = struct.unpack(">I", header)[0]
        if not 0 < length <= limit:
            raise CameraWorkerProtocolError("media message exceeds its size limit")
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise CameraWorkerProtocolError("media message ended early") from exc


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise CameraWorkerProtocolError(f"{label} is invalid")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise CameraWorkerProtocolError(f"{label} is invalid") from exc


def _is_jpeg(value: object) -> bool:
    return (
        isinstance(value, bytes)
        and 4 <= len(value) <= MAX_MEDIA_JPEG_BYTES
        and value.startswith(b"\xff\xd8")
        and value.endswith(b"\xff\xd9")
    )
