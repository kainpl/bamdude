"""Request and response shapes for direct-to-device label printing."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class LabelSpoolEntry(BaseModel):
    """One spool to print.

    The same shape ``routes/labels.py`` already accepts, so the frontend
    forwards ``formatSpoolDisplayName`` output unchanged. Omitting it lets the
    server interpolate the naming setting itself.
    """

    id: int
    display_name: str | None = None


class LabelJobCreate(BaseModel):
    device_id: int
    spools: list[LabelSpoolEntry] = Field(..., min_length=1, max_length=100)
    #: Which design. Omitted, the server picks the one that fits the loaded
    #: cassette — and refuses if nothing does, rather than guessing a size.
    template_id: int | None = None
    copies: int = Field(1, ge=1, le=20)


class LabelJobPreview(BaseModel):
    device_id: int
    spool: LabelSpoolEntry
    template_id: int | None = None


class LabelJobOut(BaseModel):
    id: int
    device_id: int
    spool_id: int | None
    template_id: int | None
    width_mm: float
    height_mm: float
    copies: int
    status: str
    attempts: int
    error: str | None
    claimed_at: datetime | None
    created_at: datetime | None = None


class LabelDeviceOut(BaseModel):
    id: int
    installation_id: str
    driver: str
    model: str | None
    protocol_version: int | None
    transport: str | None
    address: str | None
    name: str | None
    enabled: bool
    density: int
    app_version: str | None
    last_seen_at: datetime | None
    cassette_barcode: str | None
    cassette_width_mm: float | None
    cassette_height_mm: float | None
    paper_state: int | None
    power_level: int | None
    printer_reachable: bool
    #: How many of its jobs are still waiting. The one number somebody looking
    #: at a device list actually wants.
    queued: int = 0


class LabelDeviceUpdate(BaseModel):
    """What a person may change about a device. Everything else is reported."""

    name: str | None = None
    enabled: bool | None = None
    density: int | None = Field(default=None, ge=1, le=5)


class LabelCassetteIn(BaseModel):
    width_mm: float = Field(gt=0, le=500)
    height_mm: float = Field(gt=0, le=500)
    name: str | None = Field(default=None, max_length=128)


class LabelCassetteOut(BaseModel):
    id: int
    barcode: str
    width_mm: float
    height_mm: float
    name: str | None


__all__ = [
    "LabelCassetteIn",
    "LabelCassetteOut",
    "LabelDeviceOut",
    "LabelDeviceUpdate",
    "LabelJobCreate",
    "LabelJobOut",
    "LabelJobPreview",
    "LabelSpoolEntry",
]
