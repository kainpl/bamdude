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
    # ``firmware_idx_run`` — what the AMS is running; ``firmware_idx_sel`` —
    # what runs after a switch finishes. They differ only mid-switch.
    firmware_idx_run: int | None = None
    firmware_idx_sel: int | None = None
    # BS hides the picker entirely while ``status == "SWITCHING"`` and shows
    # progress instead (AMSSetting.cpp) — a reflash in progress is not a moment
    # to offer a second one.
    firmware_switching: bool = False


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
    """One switchable AMS firmware, exactly as the device reported it.

    ``idx`` is the device's own id (BS ``IDX_LITE = 0``,
    ``IDX_AMS_AMS2_AMSHT = 1``) and ``label`` its own name — neither is ours to
    choose. An empty list means the printer offers no switch.
    """

    idx: int
    label: str
    version: str | None = None


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
