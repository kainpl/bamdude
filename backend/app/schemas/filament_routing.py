"""Routing choices shared by queue and direct-print request bodies."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


class FilamentOverride(BaseModel):
    """Override for a single filament slot. Mirrors upstream's filament_overrides format."""

    slot_id: int = Field(ge=1)  # 1-indexed slot
    type: str | None = None  # e.g. "PLA", "PETG"
    color: str | None = None  # hex like "#FF0000"
    # Slicer spool identity ("GFA00" PLA Basic, "GFA01" PLA Matte, "GFA06" Silk,
    # "P4d64437" a custom preset). Only meaningful alongside force_color_match,
    # where it keeps the variants apart — everything reports tray_type "PLA"
    # (#2650). Blank means "no variant constraint".
    tray_info_idx: str | None = None
    force_color_match: bool = False  # exact-color requirement


class FilamentRoutingChoices(BaseModel):
    feed_policy: Literal["auto", "ams_only", "external_only"] | None = None
    force_color_match: bool = False
    allow_base_material_match: bool = True
    filament_overrides: list[FilamentOverride] | None = None
    # Did a person point at these trays? The accompanying ``ams_mapping`` cannot
    # answer that: the dialog sends back the routing it displayed, so every add
    # carries one. Only this flag turns that array into physical pins — a
    # statement, never an inference. It is a request choice and not a column;
    # what persists is the intent's ``mode``.
    manual_mapping: bool = False


class RoutingPreviewRequest(FilamentRoutingChoices):
    archive_id: int | None = None
    library_file_id: int | None = None
    plate_ids: list[Annotated[int, Field(ge=0)]] = Field(default_factory=lambda: [0], min_length=1, max_length=64)
    target_location_id: int | None = None

    @model_validator(mode="after")
    def one_source(self):
        if bool(self.archive_id) == bool(self.library_file_id):
            raise ValueError("Exactly one source is required")
        return self
