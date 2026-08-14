"""Schemas for per-(user, printer-model) saved PrintModal toggles."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.app.schemas.calibration_mode import CalibrationMode
from backend.app.schemas.timelapse import TimelapseStorage


class PrintOptionsToggles(BaseModel):
    """The toggles from the PrintModal "Print options" panel.

    Mirrors the frontend ``PrintOptions`` interface in
    ``frontend/src/components/PrintModal/types.ts``. bed_levelling / flow_cali /
    nozzle_offset_cali are tri-state (off/auto/on); the CalibrationMode field
    also accepts a legacy bool, so preferences saved before the tri-state parse.
    """

    bed_levelling: CalibrationMode
    flow_cali: CalibrationMode
    layer_inspect: bool
    timelapse: bool
    # Which medium records it. Defaulted so preferences saved before the picker
    # existed still parse, and None means "no preference" rather than internal —
    # remembering a medium nobody chose would move where recordings land on a
    # machine that has been writing to its card all along.
    timelapse_storage: TimelapseStorage | None = None
    mesh_mode_fast_check: bool
    gcode_injection: bool
    # Dual-nozzle-only toggle (#1682). Defaulted so preferences saved before it
    # existed still parse; the PrintModal only surfaces it on dual-nozzle printers.
    nozzle_offset_cali: CalibrationMode = "on"
    # Per-item preheat / heat-soak override (#1468). Defaulted so preferences
    # saved before it existed still parse — 'inherit' means "follow the global
    # Settings → Printing preheat_enabled toggle"; 'on'/'off' force the decision.
    preheat_override: Literal["inherit", "on", "off"] = "inherit"
    # Optional explicit chamber-target override (°C, 0–60). None keeps the
    # per-filament-type derivation.
    preheat_chamber_target_override: int | None = Field(default=None, ge=0, le=60)


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
