"""Pydantic schemas for the Slicer Pipeline API (#1425, ported to BamDude).

A pipeline bundles printer / process / filament(s) / bed-type picks under a
reusable name plus a dispatch target (specific printer or printer-model class)
and a fanout strategy. The response/request shapes mirror upstream Bambuddy so
the frontend contract is unchanged.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from backend.app.schemas.slicer import PresetRef


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
    bed_type: str | None = Field(default=None, max_length=64)


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
    bed_type: str | None = Field(default=None, max_length=64)

    # Dispatch target. ``target_kind='specific_printer'`` requires
    # ``target_printer_id`` set (or cleared with 0 in the same payload).
    # ``target_kind='printer_class'`` pairs with ``target_model_class`` (a
    # Bambu model code like 'X1C') and the fanout strategy that distributes
    # copies across matching printers.
    target_kind: Literal["specific_printer", "printer_class"] | None = None
    target_printer_id: int | None = None
    target_model_class: str | None = Field(default=None, max_length=20)
    fanout_strategy: Literal["max_parallel", "fill_one_first", "round_robin"] | None = None


class SlicerPipelineResponse(SlicerPipelineBase):
    """A single pipeline as returned by the API."""

    id: int
    created_by: int | None
    created_at: datetime
    updated_at: datetime

    target_kind: Literal["specific_printer", "printer_class"] = "printer_class"
    target_printer_id: int | None = None
    target_model_class: str | None = None
    fanout_strategy: Literal["max_parallel", "fill_one_first", "round_robin"] = "max_parallel"

    model_config = {"from_attributes": True}


class SlicerPipelineListResponse(BaseModel):
    """Wraps the list so the response stays additive when run/job counts get
    surfaced later (e.g. a ``meta`` field for last-run timestamps)."""

    pipelines: list[SlicerPipelineResponse] = []
