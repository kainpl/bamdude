"""Pydantic schemas for the Slicer Pipeline API (#1425, ported to BamDude).

A pipeline bundles printer / process / filament(s) / bed-type picks under a
reusable name plus a dispatch target (specific printer or printer-model class)
and a fanout strategy. The response/request shapes mirror upstream Bambuddy so
the frontend contract is unchanged.
"""

from datetime import datetime

from pydantic import BaseModel, Field

from backend.app.schemas.slicer import BedType, PresetRef


class SlicerPipelineBase(BaseModel):
    """Fields editable on create + update."""

    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)

    printer_preset: PresetRef
    process_preset: PresetRef
    filament_presets: list[PresetRef] = Field(
        ...,
        min_length=1,
        description="One PresetRef per AMS slot. Order matches the source plate's filament-slot order.",
    )
    # The slice request's own list: a set is saved to be sliced with, so a
    # plate no slice would take is refused here rather than stored and then
    # silently skipped when the set is applied.
    bed_type: BedType | None = None


class SlicerPipelineCreate(SlicerPipelineBase):
    """Payload for POST /slicer-pipelines."""


class SlicerPipelineUpdate(BaseModel):
    """Payload for PUT /slicer-pipelines/{id}. All fields optional; only those
    present are written. Preset and filament list are replaced wholesale when
    set (we don't support partial filament-slot edits)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    printer_preset: PresetRef | None = None
    process_preset: PresetRef | None = None
    filament_presets: list[PresetRef] | None = Field(default=None, min_length=1)
    bed_type: BedType | None = None


class SlicerPipelineResponse(SlicerPipelineBase):
    """A single pipeline as returned by the API."""

    # A free string on the way OUT: a row saved before the check may name a
    # plate the list does not have, and one such row must not take the whole
    # listing down. The dialog says the plate was not applied.
    bed_type: str | None = None

    id: int
    created_by: int | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SlicerPipelineListResponse(BaseModel):
    """Wraps the list so the response stays additive when run/job counts get
    surfaced later (e.g. a ``meta`` field for last-run timestamps)."""

    pipelines: list[SlicerPipelineResponse] = []
