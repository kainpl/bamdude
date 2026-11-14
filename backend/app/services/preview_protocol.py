"""Closed local preview wire contract. No application/runtime imports."""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass

VERSION = 2  # one constant for Command, ready and launch; 2 = STEP role/kind + cad descriptor
QUEUE_LIMIT = 8
QUEUE_SECONDS = 10
ACTIVE_SECONDS = 120
CONTROL_SECONDS = 2
# Cancel/reap budget (spec §4.2, §6.1): a cancel reply waits the service's OWNED stop — PreviewProcess.stop
# gives the guardian 2 s, then kills and waits up to 5 s — plus cleanup. Never the 2 s control RPC: a healthy
# but slow stop must not retire the service. A candidate the A3 barrier tests confirm on the real chain.
C_CANCEL_SECONDS = 10
# Pending STEP executions (queued + running), spec §4.1: min(64, 1800 // max(2·T_max, 120 + 3·C_cancel)),
# T_max = 60 s on the accepted caps — 12 at C_cancel = 10 s.
STEP_QUEUE_LIMIT = min(64, 1800 // max(2 * 60, 120 + 3 * C_CANCEL_SECONDS))
STARTUP_SECONDS = 20
CHUNK_BYTES = 128 * 1024
OBJECT_BYTES = 256 * 1024**2
ATTEMPT_BYTES = 1024**3
BUCKET_BYTES = 2 * 1024**3
STAGING_BYTES = 2 * 1024**3
QUARANTINE_ATTEMPTS = 16  # proven-dead attempts whose staging could not be removed yet (spec §4.2)
PNG_BYTES = 16 * 1024**2
PNG_PIXELS = 16_000_000
ZIP_BYTES = 1024**3
RSS_BYTES = 1024**3
FACE_LIMIT = 200_000
# STEP preview (spec §7.1, accepted stage-0 numbers): input bytes before ReadFile, face occurrences after transfer,
# triangles after the mesh (before anything is written); the early aggregate-RSS watchdog of a STEP attempt; and the
# allocation backstops, each its own named limit and NOT an RSS guarantee (Windows Job commit / Linux RLIMIT_DATA).
STEP_INPUT_BYTES = 9 * 1024**2
CAD_FACE_OCCURRENCES = 6_000
CAD_TRIANGLES = 180_000
STEP_WATCHDOG_BYTES = 800 * 1024**2
JOB_COMMIT_BYTES = 1024**3
STEP_DATA_LIMIT_BYTES = 1024**3
# The converter's print mode (project assembly): a mesh that is printed, under the assembly job's own limits.
STEP_PRINT_INPUT_BYTES = 64 * 1024**2
CAD_PRINT_FACE_OCCURRENCES = 60_000
CAD_PRINT_TRIANGLES = 2_000_000
STEP_PRINT_DATA_LIMIT_BYTES = 2 * 1024**3
JSON_BYTES = 16 * 1024
LOG_BYTES = 64 * 1024
TTL_SECONDS = 900
TERMINAL_LIMIT = 256
ROLES = frozenset({"mesh", "sliced", "source_png", "preview", "checkpoint", "output"})
OPERATIONS = frozenset({"ready", "run", "status", "cancel", "checkpoint-receipt", "shutdown"})
KINDS = frozenset({"stl", "obj", "3mf", "png", "step", "stp"})
CAD_KINDS = frozenset({"step", "stp"})
OUTCOMES = frozenset(
    {
        "ok",
        "busy",
        "unavailable",
        "timeout",
        "resource_limit",
        "render_failed",
        "canceled",
        "protocol_error",
        # the STEP component is absent or unusable here: a terminal of the attempt, never a fault of the service
        "cad_unavailable",
    }
)
#: main-side outcomes beside the wire's: a positive ENOENT reading the SOURCE itself (spec §6), and a source whose
#: bytes moved while main read it. The service never answers either — a reply naming one is still a protocol error,
#: because replies are checked against OUTCOMES alone.
MAIN_OUTCOMES = OUTCOMES | {"file_missing", "source_changed"}
STAGES = ("snapshot", "cad_import", "transfer", "mesh", "mesh_write", "png", "publish")
_HEX = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_BUNDLE_KEY = re.compile(r"(linux|win|darwin)-(x64|arm64)-cp3\d{1,2}\Z")


#: who an attempt works for, as the parent log names it ("job=<id> file=<id>", "file=<id>"): set by the caller in
#: its own context before the attempt starts, printed beside the attempt id on the attempt's start and end lines
#: (spec §6 correlation: file, job and attempt joined without guessing by time).
PREVIEW_OWNER: contextvars.ContextVar[str | None] = contextvars.ContextVar("preview_owner", default=None)


#: who decided a failure: a disposable child, by its operation, or the preview service itself
DECIDERS = frozenset({"convert", "cad_png", "mesh", "source", "plates", "service"})
_WHY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def closed_why(value) -> str | None:
    """A failure's reason as the parent log may carry it: a closed token or an exception's class name — never a
    path, stderr or an exception's text."""
    return value if isinstance(value, str) and _WHY.match(value) else None


def _exit_code(value) -> int | None:
    return value if type(value) is int and -(2**31) <= value < 2**32 else None


def _decider(value) -> str | None:
    # the type first: a JSON array or object is unhashable, and a TypeError here turned an ok render unavailable
    return value if isinstance(value, str) and value in DECIDERS else None


def reply_diagnostics(reply: dict) -> tuple[str | None, str | None, int | None]:
    """(why, by, exit) of a service reply, each dropped unless it is of its closed form."""
    return closed_why(reply.get("why")), _decider(reply.get("by")), _exit_code(reply.get("exit"))


class PreviewError(Exception):
    def __init__(self, outcome: str, *, why=None, by=None, exit_code=None):
        if outcome not in MAIN_OUTCOMES:
            raise ValueError("invalid preview outcome")
        self.outcome = outcome
        # diagnostics only, for the parent log: nothing decides on them
        self.why, self.by, self.exit_code = closed_why(why), _decider(by), _exit_code(exit_code)
        super().__init__(outcome)

    def diagnostics(self) -> dict:
        found = (("why", self.why), ("by", self.by), ("exit", self.exit_code))
        return {key: value for key, value in found if value is not None}


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
            "mesh": {"stl", "obj", "step", "stp"},
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
    # STEP only: {manifest_digest, bundle_key} — the bundle the service may hand the child, never a path
    cad: dict | None = None

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
        step = any(x.role == "mesh" and x.kind in CAD_KINDS for x in manifest)
        if step and any(x.role == "sliced" for x in manifest):
            raise PreviewError("protocol_error")  # STEP never rides a slicing attempt
        cad = data["cad"]
        if step != (cad is not None) or (
            cad is not None
            and (
                not isinstance(cad, dict)
                or set(cad) != {"manifest_digest", "bundle_key"}
                or not isinstance(cad["manifest_digest"], str)
                or not _SHA.fullmatch(cad["manifest_digest"])
                or not isinstance(cad["bundle_key"], str)
                or not _BUNDLE_KEY.fullmatch(cad["bundle_key"])
            )
        ):
            raise PreviewError("protocol_error")
        return cls(**{**data, "manifest": manifest, "cad": dict(cad) if cad is not None else None})

    def wire(self, operation: str | None = None) -> dict:
        return {
            **asdict(self),
            "manifest": [item.wire() for item in self.manifest],
            "operation": operation or self.operation,
        }

    @property
    def kind(self) -> str:
        return attempt_kind(self.manifest)

    @property
    def subject(self) -> str:
        return f"bamdude.preview.{self.app_generation}.{self.service_epoch}"


def attempt_kind(manifest) -> str:
    """From the ROLES, never from the first entry: 'sliced', 'step' (a CAD mesh) or 'mesh' (STL/OBJ)."""
    roles = {x.role: x for x in manifest}
    if "sliced" in roles:
        return "sliced"
    if "mesh" in roles and roles["mesh"].kind in CAD_KINDS:
        return "step"
    return "mesh"


PROOF_MODES = frozenset({"terminal_replay", "cancel_ack"})
_PROOF_KEYS = frozenset(
    {"mode", "version", "app_generation", "service_epoch", "attempt_id", "attempt_seq", "command_digest"}
)


def command_digest(command: Command) -> str:
    """The digest of the canonical RUN command (operation fixed), so a proof names exactly one attempt."""
    return hashlib.sha256(json.dumps(command.wire("run"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def proof_envelope(command: Command, mode: str) -> dict:
    if mode not in PROOF_MODES:
        raise ValueError("invalid proof mode")
    return {
        "mode": mode,
        "version": VERSION,
        "app_generation": command.app_generation,
        "service_epoch": command.service_epoch,
        "attempt_id": command.attempt_id,
        "attempt_seq": command.attempt_seq,
        "command_digest": command_digest(command),
    }


def proves_end(command: Command, reply: dict) -> bool:
    """Main's check of a cancel reply (spec §6.1). A valid terminal_replay with a closed terminal outcome other than
    unavailable/busy, or a cancel_ack with canceled, for EXACTLY this command; anything else is no proof."""
    if not isinstance(reply, dict):
        return False
    proof = reply.get("proof")
    if (
        not isinstance(proof, dict)
        or set(proof) != _PROOF_KEYS
        or proof != {**proof_envelope(command, "cancel_ack"), "mode": proof.get("mode")}
    ):
        return False
    outcome = reply.get("outcome")
    if proof["mode"] == "cancel_ack":
        return outcome == "canceled"
    return proof["mode"] == "terminal_replay" and outcome in OUTCOMES - {"unavailable", "busy"}
