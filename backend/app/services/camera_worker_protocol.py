"""Versioned, bounded local IPC primitives for the future camera worker.

This module carries no camera configuration and never writes a secret to logs.
It is deliberately independent from FastAPI, database and camera transports so
the child process cannot acquire those subsystems by importing its protocol.
"""

from __future__ import annotations

import base64
import json
import secrets
import struct
import uuid
from dataclasses import dataclass, field
from typing import Any, BinaryIO

PROTOCOL_VERSION = 1
MAX_BOOTSTRAP_BYTES = 64 * 1024
MAX_CONTROL_BYTES = 64 * 1024


class CameraWorkerProtocolError(ValueError):
    """A peer sent malformed, stale or unauthenticated local IPC."""


@dataclass(frozen=True, kw_only=True)
class WorkerBootstrap:
    """One-use startup material passed only over the child's stdin pipe."""

    generation: str
    control_port: int
    media_port: int
    secret: bytes = field(repr=False)
    version: int = PROTOCOL_VERSION

    @classmethod
    def create(cls, *, control_port: int, media_port: int) -> WorkerBootstrap:
        return cls(
            generation=str(uuid.uuid4()),
            control_port=control_port,
            media_port=media_port,
            secret=secrets.token_bytes(32),
        )

    def __post_init__(self) -> None:
        _validate_version(self.version)
        _validate_generation(self.generation)
        _validate_port(self.control_port)
        _validate_port(self.media_port)
        if len(self.secret) != 32:
            raise CameraWorkerProtocolError("bootstrap secret has an invalid length")

    def to_bytes(self) -> bytes:
        payload = {
            "version": self.version,
            "generation": self.generation,
            "control_port": self.control_port,
            "media_port": self.media_port,
            "secret": _encode_secret(self.secret),
        }
        return _encode(payload, limit=MAX_BOOTSTRAP_BYTES)

    @classmethod
    def from_bytes(cls, data: bytes) -> WorkerBootstrap:
        payload = _decode(data, limit=MAX_BOOTSTRAP_BYTES)
        _require_keys(payload, {"version", "generation", "control_port", "media_port", "secret"})
        secret = _decode_secret(payload["secret"])
        return cls(
            version=payload["version"],
            generation=payload["generation"],
            control_port=payload["control_port"],
            media_port=payload["media_port"],
            secret=secret,
        )


def read_bootstrap(stream: BinaryIO) -> WorkerBootstrap:
    """Read exactly one length-prefixed bootstrap message from stdin."""

    return WorkerBootstrap.from_bytes(_read_sync_frame(stream, limit=MAX_BOOTSTRAP_BYTES))


async def read_control(reader) -> dict[str, Any]:
    """Read and validate one control request from a StreamReader."""

    data = await _read_async_frame(reader, limit=MAX_CONTROL_BYTES)
    payload = _decode(data, limit=MAX_CONTROL_BYTES)
    _require_keys(payload, {"version", "generation", "request_id", "operation", "payload"})
    _validate_version(payload["version"])
    _validate_generation(payload["generation"])
    _validate_request_id(payload["request_id"])
    if not isinstance(payload["operation"], str) or not payload["operation"]:
        raise CameraWorkerProtocolError("control operation is invalid")
    if not isinstance(payload["payload"], dict):
        raise CameraWorkerProtocolError("control payload is invalid")
    return payload


async def write_control(writer, payload: dict[str, Any]) -> None:
    writer.write(_encode(payload, limit=MAX_CONTROL_BYTES))
    await writer.drain()


def make_request(*, generation: str, request_id: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "generation": generation,
        "request_id": request_id,
        "operation": operation,
        "payload": payload,
    }


def make_reply(
    *, generation: str, request_id: str, ok: bool, result: dict[str, Any] | None = None, error: str | None = None
) -> dict[str, Any]:
    """Create a reply with a fixed, sanitised error surface."""

    return {
        "version": PROTOCOL_VERSION,
        "generation": generation,
        "request_id": request_id,
        "operation": "reply",
        "payload": {"ok": ok, "result": result or {}, "error": error if not ok else None},
    }


def validate_reply(reply: dict[str, Any], *, generation: str, request_id: str) -> dict[str, Any]:
    if reply["generation"] != generation or reply["request_id"] != request_id:
        raise CameraWorkerProtocolError("control reply belongs to another worker generation")
    payload = reply["payload"]
    _require_keys(payload, {"ok", "result", "error"})
    if not isinstance(payload["ok"], bool) or not isinstance(payload["result"], dict):
        raise CameraWorkerProtocolError("control reply is invalid")
    if payload["error"] not in {None, "authentication_failed", "unknown_operation", "protocol_error"}:
        raise CameraWorkerProtocolError("control reply contains an invalid error")
    return payload


def _encode(payload: dict[str, Any], *, limit: int) -> bytes:
    try:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CameraWorkerProtocolError("control message is not JSON serializable") from exc
    if not encoded or len(encoded) > limit:
        raise CameraWorkerProtocolError("control message exceeds its size limit")
    return struct.pack(">I", len(encoded)) + encoded


def _decode(data: bytes, *, limit: int) -> dict[str, Any]:
    if not data or len(data) > limit:
        raise CameraWorkerProtocolError("control message exceeds its size limit")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CameraWorkerProtocolError("control message is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CameraWorkerProtocolError("control message must be an object")
    return value


def _read_sync_frame(stream: BinaryIO, *, limit: int) -> bytes:
    length = _decode_length(stream.read(4), limit=limit)
    data = stream.read(length)
    if len(data) != length:
        raise CameraWorkerProtocolError("control message ended early")
    return data


async def _read_async_frame(reader, *, limit: int) -> bytes:
    try:
        header = await reader.readexactly(4)
        length = _decode_length(header, limit=limit)
        return await reader.readexactly(length)
    except EOFError as exc:
        raise CameraWorkerProtocolError("control message ended early") from exc
    except Exception as exc:
        if exc.__class__.__name__ == "IncompleteReadError":
            raise CameraWorkerProtocolError("control message ended early") from exc
        raise


def _decode_length(header: bytes, *, limit: int) -> int:
    if len(header) != 4:
        raise CameraWorkerProtocolError("control message ended early")
    length = struct.unpack(">I", header)[0]
    if not 0 < length <= limit:
        raise CameraWorkerProtocolError("control message exceeds its size limit")
    return length


def _require_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise CameraWorkerProtocolError("control message has an invalid schema")


def _validate_version(value: object) -> None:
    if value != PROTOCOL_VERSION:
        raise CameraWorkerProtocolError("unsupported camera worker protocol version")


def _validate_generation(value: object) -> None:
    if not isinstance(value, str):
        raise CameraWorkerProtocolError("worker generation is invalid")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise CameraWorkerProtocolError("worker generation is invalid") from exc


def _validate_request_id(value: object) -> None:
    if not isinstance(value, str):
        raise CameraWorkerProtocolError("control request ID is invalid")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise CameraWorkerProtocolError("control request ID is invalid") from exc


def _validate_port(value: object) -> None:
    if not isinstance(value, int) or not 1 <= value <= 65535:
        raise CameraWorkerProtocolError("worker loopback port is invalid")


def _encode_secret(secret: bytes) -> str:
    return base64.urlsafe_b64encode(secret).decode("ascii")


def _decode_secret(value: object) -> bytes:
    if not isinstance(value, str):
        raise CameraWorkerProtocolError("bootstrap secret is invalid")
    try:
        decoded = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise CameraWorkerProtocolError("bootstrap secret is invalid") from exc
    return decoded
