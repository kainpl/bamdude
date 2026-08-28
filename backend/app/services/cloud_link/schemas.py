"""Pydantic mirror of the Cloud Link envelope v1 wire contract.

Every frame on the wire is one JSON object carrying ``v`` (envelope version),
``type`` (the discriminator), ``id``, ``ts`` and a ``type``-specific ``data``.
The authority for this shape is the zod schema in the portal repo; the golden
fixtures snapshotted under ``backend/tests/fixtures/cloud_link/`` are what pins
this file to it.

Two rules outlive any single frame type:

* **Unknown fields are ignored, never rejected** (``extra="ignore"`` on every
  model). The portal must be able to add a field without stranding agents that
  predate it — a stricter setting would turn every additive change into a
  breaking one.
* **:func:`parse_frame` raises :class:`ValueError`**, not pydantic's
  ``ValidationError``. Callers (uplink, client loop, command dispatcher) catch
  ``ValueError``, so the parser can be reimplemented without touching them.
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    TypeAdapter,
    ValidationError,
    model_serializer,
)


class _Model(BaseModel):
    """Common config for every model in the contract — see the forever-rule above."""

    model_config = ConfigDict(extra="ignore")


class _Base(_Model):
    """Fields every frame carries, whichever direction it travels."""

    v: Literal[1]
    # ``z.string().min(1)`` in the contract — an empty id or timestamp is not a
    # frame anybody can correlate or order.
    id: Annotated[str, Field(min_length=1)]
    ts: Annotated[str, Field(min_length=1)]


# --- shared payload pieces -------------------------------------------------


class Temps(_Model):
    """A printer's temperatures. Every reading is nullable: a printer that is
    offline, or a model without a chamber, simply has nothing to report."""

    bed: float | None
    bed_target: float | None
    nozzle: float | None
    nozzle_target: float | None
    chamber: float | None


class PrinterError(_Model):
    """What a printer is complaining about. Both halves are required: a code
    nobody can read, or a message nothing can be keyed off, is half an error."""

    code: str
    message: str


class UplinkPrinter(_Model):
    """One printer as the portal sees it — deliberately a small projection of
    our own model, not a dump of it."""

    id: str
    name: str
    model: str
    state: Literal["idle", "printing", "paused", "offline", "error", "unknown"]
    # Percent, not a fraction. Null while nothing is printing.
    progress: Annotated[float, Field(ge=0, le=100)] | None
    job_name: str | None
    temps: Temps
    error: PrinterError | None


# --- handshake -------------------------------------------------------------


class HelloData(_Model):
    instance_id: str
    secret: str
    agent_version: str
    envelope_versions: list[int]
    capabilities: list[str]


class Hello(_Base):
    type: Literal["hello"]
    data: HelloData


class HelloOkData(_Model):
    envelope_version: int
    heartbeat_interval_s: float
    throttle_min_interval_s: float


class HelloOk(_Base):
    type: Literal["hello_ok"]
    data: HelloOkData


class HelloErrData(_Model):
    code: Literal["bad_credentials", "revoked", "unsupported_version"]


class HelloErr(_Base):
    type: Literal["hello_err"]
    data: HelloErrData


# --- telemetry -------------------------------------------------------------

# ⚠️ **``Snapshot``/``Status`` below are LEGACY, superseded by
# ``StatusBatch``/``SnapshotChunk`` further down.** The delta-transport rework
# replaced a single unbounded ``snapshot`` (one frame, every printer, no size
# bound) and a single ``status`` (one printer, one frame, no coalescing) with
# the chunked/batched pair — see :mod:`~backend.app.services.cloud_link.uplink`.
# This farm no longer emits either class; both are kept only as a contract
# mirror (the golden fixtures under ``backend/tests/fixtures/cloud_link/``
# still exercise them, and an older agent or a portal built against the prior
# shape may still send or expect one). New code should never construct either.


class SnapshotData(_Model):
    printers: list[UplinkPrinter]


class Snapshot(_Base):
    type: Literal["snapshot"]
    data: SnapshotData


class StatusData(_Model):
    printer: UplinkPrinter


class Status(_Base):
    type: Literal["status"]
    data: StatusData


class EventData(_Model):
    kind: Literal["print_started", "print_finished", "printer_online", "printer_offline", "hms_error"]
    printer_id: str
    detail: dict = {}


class Event(_Base):
    type: Literal["event"]
    data: EventData


class Heartbeat(_Base):
    type: Literal["heartbeat"]
    data: dict = {}


# The coalescing counterpart to the legacy ``status`` above: one printer's
# reading, coalesced with whatever else changed the same tick, into a batch of
# up to BATCH_MAX_PRINTERS. ``seq`` is scoped to ONE connection (see
# ``Uplink._seq`` / ``reset_transient``) and orders batches within it — the
# portal keys ``lastSeq`` off it, so a gap or an out-of-order arrival reads as
# a resync trigger, not silent corruption.
class StatusBatchData(_Model):
    seq: Annotated[int, Field(ge=1)]
    printers: list[UplinkPrinter]


class StatusBatch(_Base):
    type: Literal["status_batch"]
    data: StatusBatchData


# The coalescing counterpart to the legacy ``snapshot`` above: a farm past
# SNAPSHOT_CHUNK_PRINTERS splits into several of these sharing one ``sync_id``.
# ``chunk``/``of`` are 1-based; deliberately NOT cross-validated here
# (``chunk <= of``) — the portal's own zod schema carries no such check
# either, and a stricter one here than the wire contract enforces would break
# byte-parity with the golden fixtures the moment a hand-authored one exercises
# an edge case. ``base_seq`` is the ``status_batch`` sequence this act is
# consistent with — see ``Uplink.build_snapshot_chunks``.
class SnapshotChunkData(_Model):
    sync_id: Annotated[str, Field(min_length=1)]
    chunk: Annotated[int, Field(ge=1)]
    of: Annotated[int, Field(ge=1)]
    base_seq: Annotated[int, Field(ge=0)]
    printers: list[UplinkPrinter]


class SnapshotChunk(_Base):
    type: Literal["snapshot_chunk"]
    data: SnapshotChunkData


# --- commands --------------------------------------------------------------


class CmdData(_Model):
    cmd: str
    args: dict = {}


class Cmd(_Base):
    type: Literal["cmd"]
    data: CmdData


class CmdResultData(_Model):
    ok: bool
    # OPTIONAL, not nullable. The contract says ``z.string().optional()`` /
    # ``z.record(...).optional()``, and zod refuses an explicit ``null`` for
    # those — so when there is nothing to say, the key must be ABSENT.
    # Everything else nullable in this contract (progress, job_name, temps,
    # printer error) is the opposite and must keep its explicit null.
    error: str | None = None
    payload: dict | None = None

    @model_serializer(mode="wrap")
    def _omit_the_unset_optionals(self, handler: SerializerFunctionWrapHandler) -> dict:
        """Drop the None-valued keys — no field of this model is nullable, so a
        None here always means "unset", never "reported as empty"."""
        return {key: value for key, value in handler(self).items() if value is not None}


class CmdResult(_Base):
    type: Literal["cmd_result"]
    # The id of the ``cmd`` this answers — required, or a result cannot be
    # matched to its request.
    re: str
    data: CmdResultData


AnyFrame = (
    Hello | HelloOk | HelloErr | Snapshot | Status | Event | Heartbeat | StatusBatch | SnapshotChunk | Cmd | CmdResult
)
Frame = Annotated[AnyFrame, Field(discriminator="type")]

_adapter: TypeAdapter[AnyFrame] = TypeAdapter(Frame)


def parse_frame(raw: dict) -> AnyFrame:
    """Validate one decoded JSON frame into its model.

    Raises:
        ValueError: the frame is not a valid envelope v1 frame. This is the
            stable contract — never let pydantic's ``ValidationError`` escape.
    """
    try:
        return _adapter.validate_python(raw)
    except ValidationError as e:
        raise ValueError(str(e)) from e


def new_frame_id() -> str:
    """A fresh correlation id for one outgoing frame.

    A random hex rather than a counter: the agent reconnects, and a counter
    that restarts at zero would hand the portal a second frame ``1`` for the
    same instance — which is exactly the id a ``cmd_result`` is matched back to.
    """
    return uuid.uuid4().hex


def frame_timestamp() -> str:
    """Now, in the shape the contract's fixtures carry — ``...Z``, to the second.

    Explicitly formatted rather than ``isoformat()``, which renders the offset
    as ``+00:00``; the fixtures under ``tests/fixtures/cloud_link/`` are the
    only sample of the portal's parser we hold, and every one of them ends in
    ``Z``. Sub-second precision is deliberately absent: ordering on the portal
    side is by arrival, and :func:`new_frame_id` is what correlates a frame —
    a timestamp is a human-readable fact about a frame, not its identity.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_frame(model: AnyFrame) -> dict:
    """Serialise a frame model to the plain dict that goes on the wire.

    Nulls are kept, because the contract's ``.nullable()`` fields say "nothing to
    report", which is not the same as an absent field. Its ``.optional()`` fields
    are the mirror image and must be omitted instead — that is not a global
    setting but a property of those two fields, so it lives on the model that has
    them (:class:`CmdResultData`).
    """
    return model.model_dump(mode="json", exclude_none=False)
