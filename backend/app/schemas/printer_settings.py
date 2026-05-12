"""Request/response schemas for /printers/{id}/settings."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# ---------------- Response ----------------


class AiDetectorState(BaseModel):
    enabled: bool | None = None
    sensitivity: str | None = None


class PrintOptionsState(BaseModel):
    auto_recovery: bool | None = None
    sound: bool | None = None
    filament_tangle: bool | None = None
    nozzle_blob: bool | None = None
    save_remote_to_storage: int | None = None
    purify_air: int | None = None
    open_door: int | None = None
    plate_type: bool | None = None
    plate_align: bool | None = None
    snapshot: bool | None = None
    spaghetti_detector: AiDetectorState = AiDetectorState()
    pileup_detector: AiDetectorState = AiDetectorState()
    nozzleclumping_detector: AiDetectorState = AiDetectorState()
    airprinting_detector: AiDetectorState = AiDetectorState()
    first_layer_inspector: AiDetectorState = AiDetectorState()
    ai_monitoring: AiDetectorState = AiDetectorState()
    fod_check: bool | None = None
    displacement_detection: bool | None = None


class NozzleInfoOut(BaseModel):
    id: int
    type: str | None = None
    diameter: float | None = None
    flow_type: str | None = None


class PartsState(BaseModel):
    nozzles: list[NozzleInfoOut] = []


class PrinterSettingsSupports(BaseModel):
    spaghetti_detector: bool = False
    pileup_detector: bool = False
    nozzleclumping_detector: bool = False
    airprinting_detector: bool = False
    first_layer_inspector: bool = False
    ai_monitoring: bool = False
    filament_tangle: bool = False
    nozzle_blob: bool = False
    fod_check: bool = False
    displacement_detection: bool = False
    open_door_check: bool = False
    purify_air: bool = False
    auto_recovery: bool = False
    sound: bool = False
    save_remote_to_storage: bool = False
    snapshot: bool = False
    plate_type: bool = False
    plate_align: bool = False
    parts_editable: bool = False
    parts_dual: bool = False


class PrinterSettingsGetResponse(BaseModel):
    print_options: PrintOptionsState
    parts: PartsState
    supports: PrinterSettingsSupports


# ---------------- POST body — discriminated union ----------------


class PrintOptionBoolAction(BaseModel):
    action: Literal["print_option_bool"]
    key: Literal[
        "auto_recovery",
        "sound",
        "filament_tangle",
        "nozzle_blob",
        "plate_type",
        "plate_align",
    ]
    enabled: bool


class PrintOptionIntAction(BaseModel):
    action: Literal["print_option_int"]
    key: Literal["save_remote_to_storage", "purify_air", "open_door"]
    value: int = Field(ge=0, le=10)


class XCamControlAction(BaseModel):
    action: Literal["xcam_control"]
    module: Literal[
        "first_layer_inspector",
        "spaghetti_detector",
        "purgechutepileup_detector",
        "nozzleclumping_detector",
        "airprinting_detector",
        "fod_check",
        "displacement_detection",
        "ai_monitoring",
    ]
    enabled: bool
    sensitivity: Literal["low", "medium", "high"] | None = None


class CameraSnapshotAction(BaseModel):
    action: Literal["camera_snapshot"]
    enabled: bool


class SetNozzleAction(BaseModel):
    # Phase-2 stub — backend returns 409 parts_not_editable.
    action: Literal["set_nozzle"]
    nozzle_id: int = Field(ge=0, le=1)
    type: str
    diameter: float
    flow_type: str


PrinterSettingsPostBody = Annotated[
    PrintOptionBoolAction | PrintOptionIntAction | XCamControlAction | CameraSnapshotAction | SetNozzleAction,
    Field(discriminator="action"),
]


class PrinterSettingsPostResponse(BaseModel):
    ok: bool
    sequence_id: str | None = None
