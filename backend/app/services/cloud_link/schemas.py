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


AnyFrame = Hello | HelloOk | HelloErr | Snapshot | Status | Event | Heartbeat | Cmd | CmdResult
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
