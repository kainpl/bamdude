"""Schemas for per-(user, printer-model) saved PrintModal toggles."""

from datetime import datetime

from pydantic import BaseModel, Field


class PrintOptionsToggles(BaseModel):
    """The 6 boolean toggles from the PrintModal "Print options" panel.

    Mirrors the frontend ``PrintOptions`` interface in
    ``frontend/src/components/PrintModal/types.ts``.
    """

    bed_levelling: bool
    flow_cali: bool
    layer_inspect: bool
    timelapse: bool
    mesh_mode_fast_check: bool
    gcode_injection: bool


class SwapMacrosPref(BaseModel):
    """Swap-macros sub-section of the preference."""

    execute: bool
    # Subset of {'swap_mode_start', 'swap_mode_change_table'} — kept as a
    # free-form string list so future swap-macro events can be added without
    # a schema migration.
    events: list[str] = Field(default_factory=list)


class PrintOptionsPreferenceData(BaseModel):
    """Body shape for upsert + the ``options`` payload returned on read."""

    print_options: PrintOptionsToggles
    swap_macros: SwapMacrosPref


class PrintOptionsPreferenceResponse(BaseModel):
    """Full response: preference payload + metadata."""

    printer_model: str
    options: PrintOptionsPreferenceData
    updated_at: datetime

    model_config = {"from_attributes": True}


class PrintOptionsPreferenceAdminEntry(BaseModel):
    """Admin list entry: same payload + user identity attached.

    Returned by the admin "list every saved preference across users" route
    that powers the Settings → Print → Saved Profiles widget.
    """

    user_id: int
    username: str
    printer_model: str
    options: PrintOptionsPreferenceData
    updated_at: datetime


class PrintOptionsPreferenceCopy(BaseModel):
    """Body shape for the admin "copy preference between users" route.

    Source preference is identified by ``(src_user_id, src_printer_model)``.
    Destination is ``(dst_user_id, dst_printer_model)`` — defaults to the
    same model unless overridden, so the common case (give operator B the
    same toggles operator A uses for their P1S) is one POST without
    repeating the model.
    """

    src_user_id: int
    src_printer_model: str
    dst_user_id: int
    dst_printer_model: str | None = None  # None → reuse src_printer_model
