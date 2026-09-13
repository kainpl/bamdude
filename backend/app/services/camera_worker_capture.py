"""Strict, redacted capture command shared by the main process and worker.

The runtime request remains an in-memory API.  This module is its deliberately
small IPC projection: it accepts no arbitrary dictionaries, keeps credentials
out of ``repr``, and never imports a camera transport itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal, Protocol

from backend.app.services.camera_worker_protocol import CameraWorkerProtocolError

CaptureKind = Literal["builtin", "external"]
CapturePurpose = Literal[
    "snapshot",
    "diagnose",
    "finish_photo",
    "obico",
    "plate_check",
    "layer_timelapse",
    "telegram",
    "cloud_link",
]
CaptureSource = Literal["fresh", "coalesced"]
_PURPOSES = frozenset(CapturePurpose.__args__)
_CAMERA_TYPES = frozenset({"mjpeg", "rtsp", "snapshot", "usb"})


class _RuntimeRequest(Protocol):
    kind: CaptureKind
    purpose: CapturePurpose
    timeout: int
    ip_address: str | None
    access_code: str | None
    model: str | None
    url: str | None
    camera_type: str | None
    snapshot_url: str | None


@dataclass(frozen=True, kw_only=True)
class WorkerCaptureCommand:
    """One bounded one-shot request, including a pre-authorised media lease."""

    kind: CaptureKind
    purpose: CapturePurpose
    timeout_ms: int
    media_session_id: str
    ip_address: str | None = field(default=None, repr=False)
    access_code: str | None = field(default=None, repr=False)
    model: str | None = None
    url: str | None = field(default=None, repr=False)
    camera_type: str | None = None
    snapshot_url: str | None = field(default=None, repr=False)

    @classmethod
    def from_runtime_request(cls, request: _RuntimeRequest, *, media_session_id: str) -> WorkerCaptureCommand:
        return cls(
            kind=request.kind,
            purpose=request.purpose,
            timeout_ms=request.timeout * 1000,
            media_session_id=media_session_id,
            ip_address=request.ip_address,
            access_code=request.access_code,
            model=request.model,
            url=request.url,
            camera_type=request.camera_type,
            snapshot_url=request.snapshot_url,
        )

    @classmethod
    def from_payload(cls, payload: dict) -> WorkerCaptureCommand:
        expected = {
            "kind",
            "purpose",
            "timeout_ms",
            "media_session_id",
            "ip_address",
            "access_code",
            "model",
            "url",
            "camera_type",
            "snapshot_url",
        }
        if set(payload) != expected:
            raise CameraWorkerProtocolError("capture command has an invalid schema")
        try:
            return cls(**payload)
        except (TypeError, ValueError) as exc:
            raise CameraWorkerProtocolError("capture command is invalid") from exc

    def __post_init__(self) -> None:
        if self.kind not in {"builtin", "external"} or self.purpose not in _PURPOSES:
            raise ValueError("capture command has an invalid kind or purpose")
        if not isinstance(self.timeout_ms, int) or not 1_000 <= self.timeout_ms <= 120_000:
            raise ValueError("capture command timeout is invalid")
        _validate_uuid(self.media_session_id, "capture media session")
        if self.kind == "builtin":
            if not _bounded_text(self.ip_address, 255) or not _bounded_text(self.access_code, 128):
                raise ValueError("built-in capture command is missing its endpoint")
            if self.url is not None or self.camera_type is not None or self.snapshot_url is not None:
                raise ValueError("built-in capture command contains external fields")
            if self.model is not None and not _bounded_text(self.model, 64):
                raise ValueError("built-in capture model is invalid")
            return
        if not _bounded_text(self.url, 4096) or self.camera_type not in _CAMERA_TYPES:
            raise ValueError("external capture command is missing its endpoint")
        if self.ip_address is not None or self.access_code is not None or self.model is not None:
            raise ValueError("external capture command contains built-in fields")
        if self.snapshot_url is not None and not _bounded_text(self.snapshot_url, 4096):
            raise ValueError("external capture snapshot URL is invalid")

    def to_payload(self) -> dict:
        return {
            "kind": self.kind,
            "purpose": self.purpose,
            "timeout_ms": self.timeout_ms,
            "media_session_id": self.media_session_id,
            "ip_address": self.ip_address,
            "access_code": self.access_code,
            "model": self.model,
            "url": self.url,
            "camera_type": self.camera_type,
            "snapshot_url": self.snapshot_url,
        }


def _validate_uuid(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} is invalid") from exc


def _bounded_text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum
