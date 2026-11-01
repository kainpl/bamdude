"""Strict input and evidence contracts shared by the nine diagnostic readers."""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, model_validator

from backend.app.mcp_server.contracts import Coverage, DiagnosticResult, EmptyInput

PositiveId = Annotated[int, Field(strict=True, gt=0)]


class PageInput(EmptyInput):
    cursor: str | None = Field(default=None, max_length=2048)
    limit: int = Field(default=50, strict=True, ge=1, le=100)


class InspectInput(EmptyInput):
    printer_id: PositiveId


class TimelineInput(PageInput):
    archive_id: PositiveId
    from_: AwareDatetime | None = Field(default=None, alias="from")
    to: AwareDatetime | None = None

    @model_validator(mode="after")
    def dates(self):
        now = datetime.now(timezone.utc) + timedelta(minutes=1)
        if self.to and self.to > now or self.from_ and self.to and self.to <= self.from_:
            raise ValueError("invalid_range")
        return self


class RangeInput(PageInput):
    from_: AwareDatetime = Field(alias="from")
    to: AwareDatetime

    @model_validator(mode="after")
    def dates(self):
        if not timedelta(0) < self.to - self.from_ <= timedelta(days=31):
            raise ValueError("invalid_range")
        if self.to > datetime.now(timezone.utc) + timedelta(minutes=1):
            raise ValueError("invalid_range")
        return self


class EnergyInput(RangeInput):
    printer_id: PositiveId


class PeriodInput(RangeInput):
    timezone: str = Field(max_length=100)
    printer_ids: list[PositiveId] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def zone(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("invalid_timezone") from None
        return self


class LogsInput(RangeInput):
    component: str | None = Field(default=None, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] | None = None
    search: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def log_range(self):
        if self.to - self.from_ > timedelta(days=1):
            raise ValueError("invalid_range")
        return self


class IssuesInput(PageInput):
    printer_id: PositiveId | None = None


class RecoveryInput(InspectInput):
    kind: Literal["item", "header", "archive", "runtime", "episode", "repair"]
    target_id: PositiveId
    action: Literal[
        "align_terminal_item",
        "fill_estimated_end_and_align",
        "release_orphan_header",
        "delete_stale_item",
        "close_interrupted_attempt",
        "clear_terminal_runtime_context",
    ]


class Evidence(EmptyInput):
    id: str
    source: str
    observed_at: AwareDatetime | None = None
    collected_at: AwareDatetime
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    coverage: Coverage = Field(default_factory=Coverage)


class NextStep(EmptyInput):
    action_kind: Literal["observe", "open_ui", "ask_operator"]
    explanation: str
    ui_link: str | None = None


class Finding(EmptyInput):
    code: Literal["printer_busy", "queue_claim_requires_inspection", "farm_admission_held", "worker_unavailable"]
    severity: Literal["info", "warning", "error"]
    certainty: Literal["observed", "inferred", "insufficient_evidence"]
    evidence_ids: list[str]
    explanation: str
    missing_evidence: list[str] = Field(default_factory=list)
    next_steps: list[NextStep] = Field(default_factory=list)


class ReadResult(DiagnosticResult):
    collection_started_at: AwareDatetime
    collection_finished_at: AwareDatetime
    evidence: list[Evidence] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
