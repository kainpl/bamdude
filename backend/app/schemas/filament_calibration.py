"""Pydantic schemas for the Filament Calibration wizard API (m062 / Plan 1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.app.services.calibration_constants import CaliMethod, CaliMode

# ---------- Capabilities ----------


class ExtruderInfo(BaseModel):
    id: int
    name: str


class NozzleInfo(BaseModel):
    id: int
    diameter: float | None = None
    type: str | None = None
    flow_type: str | None = None


class CalibCapabilities(BaseModel):
    pa_manual: bool
    flow_manual: bool
    temp_tower: bool
    vol_speed_tower: bool
    vfa_tower: bool
    retraction_tower: bool
    pa_auto: bool
    flow_auto: bool
    dual_extruder: bool
    extruders: list[ExtruderInfo]
    nozzles: list[NozzleInfo]


# ---------- Start ----------


class CalibFilamentIn(BaseModel):
    ams_id: int
    slot_id: int
    tray_id: int
    filament_id: str
    filament_setting_id: str | None = None
    bed_temp: int
    nozzle_temp: int
    max_volumetric_speed: float
    flow_rate: float = 0.98
    extruder_id: int | None = None


class StartSessionIn(BaseModel):
    cali_mode: CaliMode
    method: CaliMethod
    nozzle_diameter: float
    nozzle_volume_type: Literal["standard", "high_flow", "tpu_high_flow", "hybrid"]
    extruder_id: int = 0
    filaments: list[CalibFilamentIn]


# ---------- Submit ----------


class ManualResultIn(BaseModel):
    best_line_index: int | None = None
    coarse_modifier: int | None = None
    skip_fine: bool = False
    fine_modifier: int | None = None


class AutoResultEditIn(BaseModel):
    tray_id: int
    k_value: float | None = None
    n_coef: float | None = None
    name: str | None = None
    save: bool = True


class AutoResultIn(BaseModel):
    results: list[AutoResultEditIn]


# ---------- Outputs ----------


class FilamentCalibrationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    printer_model: str
    filament_id: str
    filament_setting_id: str | None
    nozzle_diameter: float
    nozzle_volume_type: str
    extruder_id: int
    pa_k_value: float | None
    pa_n_coef: float | None
    flow_ratio: float | None
    confidence: int | None
    cali_mode: str
    source: str
    is_active: bool
    cali_idx: int | None
    name: str
    notes: str | None
    calibrated_on_printer_id: int | None
    calibrated_by_user_id: int | None
    created_at: datetime


class CalibrationSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    printer_id: int
    user_id: int | None
    cali_mode: str
    method: str
    nozzle_diameter: float
    nozzle_volume_type: str
    extruder_id: int
    status: str
    stage: int
    coarse_ratio: float | None
    parent_session_id: int | None
    mqtt_sequence_id: str | None
    print_queue_item_id: int | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ManualResultOutSchema(BaseModel):
    saved_rows: list[FilamentCalibrationOut]
    next_session_id: int | None = None


class PACalibHistoryEntryOut(BaseModel):
    cali_idx: int
    name: str
    filament_id: str
    setting_id: str
    nozzle_diameter: float
    nozzle_volume_type: str
    extruder_id: int
    k_value: float
    n_coef: float
