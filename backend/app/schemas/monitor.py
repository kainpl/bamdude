"""Small, explicit wire projection for the authenticated and TV monitors."""

from datetime import datetime as MonitorTime
from typing import Literal

from pydantic import BaseModel, Field

from backend.app.services.queue_wait_reason import WaitCode

MonitorView = Literal["printers", "queues"]
DispatchPhase = Literal["preparing", "uploading", "heating", "swapping", "starting", "acknowledging"]


class RestrictedJob(BaseModel):
    visibility: Literal["restricted"] = "restricted"


class MonitorJob(BaseModel):
    visibility: Literal["visible"] = "visible"
    name: str | None
    item_id: int | None = None


class MonitorWait(BaseModel):
    code: WaitCode
    since: MonitorTime | None = None
    until: MonitorTime | None = None


class MonitorQueue(BaseModel):
    queue_id: int
    status: str
    is_paused: bool
    auto_distribute_eligible: bool
    pending_count: int
    next_job: MonitorJob | RestrictedJob | None = None
    waiting: MonitorWait | None = None


class MonitorDispatch(BaseModel):
    phase: DispatchPhase
    upload_progress: float | None = None


class MonitorHMS(BaseModel):
    code: str
    severity: int


class MonitorPrinter(BaseModel):
    printer_id: int
    name: str
    model: str | None
    location: str | None
    tags: list[str] = Field(default_factory=list)
    is_active: bool
    connected: bool
    state: str | None
    derived_stage: Literal["heating", "preparing", "swapping"] | None = None
    progress: float | None = None
    remaining_seconds: int | None = None
    layer_num: int | None = None
    total_layers: int | None = None
    temperatures: dict[str, float] = Field(default_factory=dict)
    status_received_at: MonitorTime | None = None
    source_stale: bool = False
    last_known_work_active: bool | None = None
    hms_errors: list[MonitorHMS] = Field(default_factory=list)
    pause_reason: str | None = None
    pause_started_at: MonitorTime | None = None
    require_plate_clear: bool
    awaiting_plate_clear: bool
    current_job: MonitorJob | RestrictedJob | None = None
    queue: MonitorQueue | None = None
    dispatch: MonitorDispatch | None = None


class MonitorCapabilities(BaseModel):
    queues: bool
    forecast: bool
    job_details: bool
    open_printer: bool
    open_queue: bool


class MonitorSnapshot(BaseModel):
    generated_at: MonitorTime
    view: MonitorView
    capabilities: MonitorCapabilities
    printers: list[MonitorPrinter]
