"""Request and response models of "Assemble project" (File Library → dense layout → project 3MF)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from backend.app.schemas.slicer import PresetRef

MAX_PARTS = 100
MAX_COPIES = 2000
MAX_PRINTERS = 8
MAX_SOURCE_BYTES = 200 * 1024 * 1024

Orientation = Literal["as_is", "x+90", "x-90", "y+90", "y-90", "x180"]


class AssemblyPart(BaseModel):
    file_id: int | None = None  # required for a new assembly; ignored on a re-layout (the record's order is used)
    quantity: int = Field(1, ge=1, le=MAX_COPIES)
    fill: bool = False  # as many copies as one plate takes, on a plate of its own
    orientation: Orientation = "as_is"


class AssemblyPrinter(BaseModel):
    printer_preset: PresetRef
    process_preset: PresetRef
    filament_preset: PresetRef
    nozzle: int = Field(1, ge=1, le=4)  # 1-based extruder the parts are printed with


class AssemblyLayout(BaseModel):
    spacing: float = Field(2.0, ge=0, le=50)
    rotation_step: Literal[0, 45, 90, 180] = 90
    centre: bool = True
    split_plates: bool = False  # a file per plate instead of one multi-plate file


class AssemblyRequest(BaseModel):
    parts: list[AssemblyPart] = Field(min_length=1, max_length=MAX_PARTS)
    # one entry per printer model the project is assembled for: its three presets and the nozzle. Named
    # ``targets`` because ``printers`` in a request body means farm printers to the API-key scope gate.
    targets: list[AssemblyPrinter] = Field(min_length=1, max_length=MAX_PRINTERS)
    layout: AssemblyLayout = AssemblyLayout()
    name: str = Field(min_length=1, max_length=120)


class RelayoutRequest(AssemblyRequest):
    trash_previous: bool = True


class NozzleReach(BaseModel):
    index: int
    area: tuple[float, float, float, float]  # x0, y0, x1, y1
    max_height: float


class PrinterInfo(BaseModel):
    model: str | None
    bed_size: tuple[float, float]
    nozzles: list[NozzleReach]


class AssemblySetFile(BaseModel):
    id: int
    filename: str


class AssemblyRecordResponse(BaseModel):
    """The input record stored in an assembled file, as the re-layout dialog reads it."""

    set_id: str
    base_name: str
    parts: list[dict]
    printer: dict
    layout: dict
    plates: list[dict]
    set_files: list[AssemblySetFile]
