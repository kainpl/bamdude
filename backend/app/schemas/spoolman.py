from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, model_validator


def _slot_ams_id(value: int) -> int:
    """The units ``spoolman_slot_assignments`` accepts (its CHECK is the authority).

    0–7 standard AMS, 32 / 33 a rack AMS (AMS 3H / 3P), 128–191 AMS-HT, 255 the
    external holder. Anything else reached the INSERT and came back as a 500 —
    after Spoolman had already been written on the link route (upstream
    c2bc41a5e).
    """
    if 0 <= value <= 7 or value in (32, 33) or 128 <= value <= 191 or value == 255:
        return value
    raise ValueError("ams_id must be 0-7 (AMS), 32-33 (rack AMS), 128-191 (AMS-HT) or 255 (external)")


SlotAmsId = Annotated[int, AfterValidator(_slot_ams_id)]
# The widest slot the table admits (a rack AMS / a direct rack's external input);
# ``slot_tray_in_range`` narrows it per unit as the CHECK does.
SlotTrayId = Annotated[int, Field(ge=0, le=8)]


def slot_tray_in_range(ams_id: int, tray_id: int) -> None:
    """``ck_spoolman_slot_tray_id_range``, asked before the INSERT: trays 0-3, up to 8 for 32 / 33 / 255."""
    if tray_id <= 3 or (ams_id in (32, 33, 255) and tray_id <= 8):
        return
    raise ValueError("tray_id must be 0-3 (0-8 for a rack AMS or an external input)")


class SpoolmanFilamentPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    spool_weight: float | None = Field(None, ge=0.0, le=10_000.0)
    keep_existing_spools: bool = False

    @model_validator(mode="after")
    def keep_existing_requires_weight(self) -> "SpoolmanFilamentPatch":
        if self.keep_existing_spools and self.spool_weight is None:
            raise ValueError("keep_existing_spools=True requires spool_weight to be provided")
        return self


class SpoolmanSlotAssignmentEnriched(BaseModel):
    """Slot assignment row enriched with printer name and AMS label.

    ``printer_name`` is null only in the cascade-deleted edge case where the
    Printer relation has been removed. ``ams_label`` is null when no
    ``ams_labels`` row matches the slot's MQTT serial (or the synthetic
    ``f"p{printer_id}a{ams_id}"`` fallback key).
    """

    printer_id: int
    printer_name: str | None
    ams_id: int
    tray_id: int
    spoolman_spool_id: int
    ams_label: str | None
