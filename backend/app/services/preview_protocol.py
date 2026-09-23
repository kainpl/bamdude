"""Closed local preview wire contract. No application/runtime imports."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass

VERSION = 1
QUEUE_LIMIT = 8
QUEUE_SECONDS = 10
ACTIVE_SECONDS = 120
CONTROL_SECONDS = 2
STARTUP_SECONDS = 20
CHUNK_BYTES = 128 * 1024
OBJECT_BYTES = 256 * 1024**2
ATTEMPT_BYTES = 1024**3
BUCKET_BYTES = 2 * 1024**3
STAGING_BYTES = 2 * 1024**3
PNG_BYTES = 16 * 1024**2
PNG_PIXELS = 16_000_000
ZIP_BYTES = 1024**3
RSS_BYTES = 1024**3
FACE_LIMIT = 200_000
JSON_BYTES = 16 * 1024
LOG_BYTES = 64 * 1024
TTL_SECONDS = 900
TERMINAL_LIMIT = 256
ROLES = frozenset({"mesh", "sliced", "source_png", "preview", "checkpoint", "output"})
OPERATIONS = frozenset({"ready", "run", "status", "cancel", "checkpoint-receipt", "shutdown"})
KINDS = frozenset({"stl", "obj", "3mf", "png"})
OUTCOMES = frozenset(
    {"ok", "busy", "unavailable", "timeout", "resource_limit", "render_failed", "canceled", "protocol_error"}
)
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


class PreviewError(Exception):
    def __init__(self, outcome: str):
        if outcome not in OUTCOMES:
            raise ValueError("invalid preview outcome")
        self.outcome = outcome
        super().__init__(outcome)


def remaining(deadline: int) -> float:
    seconds = (deadline - time.monotonic_ns()) / 1e9
    if seconds <= 0:
        raise PreviewError("timeout")
    return seconds


def encode(value: dict) -> bytes:
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > JSON_BYTES:
        raise PreviewError("protocol_error")
    return payload


def decode(payload: bytes) -> dict:
    if len(payload) > JSON_BYTES:
        raise PreviewError("protocol_error")

    def object_pairs(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise PreviewError("protocol_error")
        return value

    def invalid_constant(value):
        raise PreviewError("protocol_error")

    try:
        value = json.loads(payload, object_pairs_hook=object_pairs, parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise PreviewError("protocol_error") from exc
    if not isinstance(value, dict):
        raise PreviewError("protocol_error")
    return value


@dataclass(frozen=True)
class Artifact:
    role: str
    key: str
    kind: str
    size: int
    digest: str

    @classmethod
    def parse(cls, data: dict, attempt_id: str) -> Artifact:
        if not isinstance(data, dict) or set(data) != {"role", "key", "kind", "size", "digest"}:
            raise PreviewError("protocol_error")
        obj = cls(**data)
        if (
            not isinstance(obj.role, str)
            or obj.role not in ROLES
            or obj.key != f"{attempt_id}_{obj.role}"
            or not isinstance(obj.kind, str)
            or obj.kind not in KINDS
            or type(obj.size) is not int
            or not 0 <= obj.size <= OBJECT_BYTES
            or not isinstance(obj.digest, str)
            or not _SHA.fullmatch(obj.digest)
            or (obj.kind == "png" and obj.size > PNG_BYTES)
        ):
            raise PreviewError("protocol_error")
        allowed = {
            "mesh": {"stl", "obj"},
            "sliced": {"3mf"},
            "source_png": {"png"},
            "preview": {"png"},
            "checkpoint": {"3mf"},
            "output": {"3mf"},
        }
        if obj.kind not in allowed[obj.role]:
            raise PreviewError("protocol_error")
        return obj

    def wire(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Command:
    protocol_version: int
    app_generation: str
    service_epoch: str
    attempt_id: str
    attempt_seq: int
    operation: str
    deadline_monotonic_ns: int
    manifest: tuple[Artifact, ...]

    @classmethod
    def parse(cls, data: dict, generation: str, epoch: str) -> Command:
        fields = set(cls.__dataclass_fields__)
        if set(data) != fields:
            raise PreviewError("protocol_error")
        if (
            type(data["protocol_version"]) is not int
            or data["protocol_version"] != VERSION
            or data["app_generation"] != generation
            or data["service_epoch"] != epoch
            or not isinstance(data["attempt_id"], str)
            or not _HEX.fullmatch(data["attempt_id"])
            or type(data["attempt_seq"]) is not int
            or data["attempt_seq"] < 1
            or not isinstance(data["operation"], str)
            or data["operation"] not in OPERATIONS
            or type(data["deadline_monotonic_ns"]) is not int
            or not isinstance(data["manifest"], list)
            or len(data["manifest"]) > len(ROLES)
        ):
            raise PreviewError("protocol_error")
        manifest = tuple(Artifact.parse(item, data["attempt_id"]) for item in data["manifest"])
        if len({item.role for item in manifest}) != len(manifest) or sum(x.size for x in manifest) > ATTEMPT_BYTES:
            raise PreviewError("protocol_error")
        return cls(**{**data, "manifest": manifest})

    def wire(self, operation: str | None = None) -> dict:
        return {
            **asdict(self),
            "manifest": [item.wire() for item in self.manifest],
            "operation": operation or self.operation,
        }

    @property
    def subject(self) -> str:
        return f"bamdude.preview.{self.app_generation}.{self.service_epoch}"
