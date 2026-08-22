"""Authoring request/response schemas (spec B §1–§4)."""

from typing import Literal

from pydantic import BaseModel


class CreateFamilyRequest(BaseModel):
    vendor: str
    filament_type: str
    serial: str
    printer_ids: list[int] = []
    source_mode: Literal["type", "preset"] = "type"
    source: Literal["orca_cloud", "cloud", "local", "standard"] | None = None
    source_id: str | None = None
    push_to_bambu: bool = False


class AddPrintersRequest(BaseModel):
    printer_ids: list[int]
    source_mode: Literal["type", "preset"] = "type"
    source: Literal["orca_cloud", "cloud", "local", "standard"] | None = None
    source_id: str | None = None


class ClonedRootOut(BaseModel):
    printer_id: int
    printer_name: str | None
    local_preset_id: int | None
    preset_name: str | None
    error: str | None = None


class CreateFamilyResponse(BaseModel):
    filament_id: str
    name: str
    attached: bool
    roots: list[ClonedRootOut]
    warnings: list[str]
    push: list[dict] | None = None
