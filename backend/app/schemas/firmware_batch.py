"""Schemas for the bulk (mass) firmware update flow."""

from pydantic import BaseModel


class BatchTargetIn(BaseModel):
    printer_id: int
    version: str | None = None  # None → latest for that model


class BatchStartRequest(BaseModel):
    targets: list[BatchTargetIn]
    skip_printing: bool = True


class BatchStartResponse(BaseModel):
    run_id: int


class PreviewModelGroup(BaseModel):
    model: str
    printer_ids: list[int]
    available_versions: list[str]  # both directions, newest first, incl. cached-only
    default_version: str | None
    remote_apply: bool
    skipped_printer_ids: list[int]  # currently printing


class BatchPreviewResponse(BaseModel):
    groups: list[PreviewModelGroup]


class BatchItemOut(BaseModel):
    printer_id: int
    model: str
    from_version: str | None
    to_version: str
    status: str
    message: str | None
    error: str | None


class BatchRunOut(BaseModel):
    id: int
    status: str
    total: int
    succeeded: int
    skipped: int
    failed: int
    items: list[BatchItemOut]
