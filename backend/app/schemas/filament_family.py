"""Authoring request/response schemas (spec B §1–§4)."""

from typing import Literal

from pydantic import BaseModel


class CreateFamilyRequest(BaseModel):
    vendor: str
    filament_type: str
    serial: str
    # Device-based targeting (legacy spool-form path) ...
    printer_ids: list[int] = []
    # ... and the BS-native one: printer PROFILE names ("Bambu Lab P1S 0.4
    # nozzle") picked from authoring-options. Both may be combined.
    printer_names: list[str] = []
    source_mode: Literal["type", "preset"] = "type"
    source: Literal["orca_cloud", "cloud", "local", "standard"] | None = None
    source_id: str | None = None
    push_to_bambu: bool = False
    push_to_orca: bool = False
    # False = cloud-only creation (cloud-tab flows): identity + pushed cloud
    # copies, no LocalPreset rows. Requires at least one push_to_* flag.
    save_local: bool = True


class AddPrintersRequest(BaseModel):
    printer_ids: list[int] = []
    printer_names: list[str] = []
    source_mode: Literal["type", "preset"] = "type"
    source: Literal["orca_cloud", "cloud", "local", "standard"] | None = None
    source_id: str | None = None


class ClonedRootOut(BaseModel):
    printer_id: int | None
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
    push_orca: list[dict] | None = None


class FamilyPushResolveRequest(BaseModel):
    """One conflicted preset row + the user's explicit answer."""

    preset_row_id: int
    action: Literal["force", "adopt"]
