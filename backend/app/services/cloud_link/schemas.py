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

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError


class _Model(BaseModel):
    """Common config for every model in the contract — see the forever-rule above."""

    model_config = ConfigDict(extra="ignore")


class _Base(_Model):
    """Fields every frame carries, whichever direction it travels."""

    v: Literal[1]
    id: str
    ts: str


# --- shared payload pieces -------------------------------------------------


class Temps(_Model):
    """A printer's temperatures. Every reading is nullable: a printer that is
    offline, or a model without a chamber, simply has nothing to report."""

    bed: float | None
    bed_target: float | None
    nozzle: float | None
    nozzle_target: float | None
    chamber: float | None


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
    # ``{code, message}`` — left untyped until the portal contract pins its
    # optionality; a guess here would drift from zod silently.
    error: dict | None


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
    error: str | None = None
    payload: dict | None = None


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


def make_frame(model: AnyFrame) -> dict:
    """Serialise a frame model to the plain dict that goes on the wire.

    Nulls are kept: the contract's nullable fields say "nothing to report",
    which is not the same as an absent field.
    """
    return model.model_dump(mode="json", exclude_none=False)
