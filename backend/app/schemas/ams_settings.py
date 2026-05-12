"""Request/response schemas for /printers/{id}/ams/settings."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# ---------------- Response ----------------


class AmsSystemSettingState(BaseModel):
    insertion_update: bool | None = None
    power_on_update: bool | None = None
    remain_capacity: bool | None = None
    auto_switch_filament: bool | None = None
    air_print_detect: bool | None = None
    firmware_idx_run: int | None = None
    firmware_idx_sel: int | None = None


class AmsSystemSettingSupports(BaseModel):
    insertion_update: bool = False
    power_on_update: bool = False
    remain_capacity: bool = False
    auto_switch_filament: bool = False
    air_print_detect: bool = False
    firmware_switch: bool = False
    reorder: bool = False


class AmsUnitInfo(BaseModel):
    ams_id: int
    label: str


class AmsFirmwareOption(BaseModel):
    idx: int
    label: str


class AmsSettingsGetResponse(BaseModel):
    state: AmsSystemSettingState
    supports: AmsSystemSettingSupports
    ams_units: list[AmsUnitInfo]
    firmware_options: list[AmsFirmwareOption]


# ---------------- POST body — discriminated union ----------------


class AmsUserSettingAction(BaseModel):
    action: Literal["user_setting"]
    startup_read_option: bool
    tray_read_option: bool
    calibrate_remain_flag: bool


class AmsAutoSwitchAction(BaseModel):
    action: Literal["auto_switch_filament"]
    enabled: bool


class AmsAirPrintAction(BaseModel):
    action: Literal["air_print_detect"]
    enabled: bool


class AmsCalibrateAction(BaseModel):
    action: Literal["calibrate"]
    ams_id: int = Field(ge=0, le=255)


class AmsFirmwareSwitchAction(BaseModel):
    action: Literal["firmware_switch"]
    firmware_idx: int = Field(ge=0, le=10)


class AmsReorderAction(BaseModel):
    # BS sends ``ams_reset`` with no payload; user physically reconnects AMS
    # units in the desired order. We mirror this contract.
    action: Literal["reorder"]


AmsSettingsPostBody = Annotated[
    AmsUserSettingAction
    | AmsAutoSwitchAction
    | AmsAirPrintAction
    | AmsCalibrateAction
    | AmsFirmwareSwitchAction
    | AmsReorderAction,
    Field(discriminator="action"),
]


class AmsSettingsPostResponse(BaseModel):
    ok: bool
    sequence_id: str | None = None
