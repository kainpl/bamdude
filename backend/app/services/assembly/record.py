"""What an assembled project was made from — stored inside the 3MF so it can be laid out again.

The record is one JSON member of the archive. It is how "Re-layout" reopens the dialog with the values the
file was made with, and how the files of one "a file per plate" set find each other (``set_id``).

A 3MF can come from anywhere, so :func:`load` trusts nothing: anything that is not exactly a record is a
``ValueError``. Pure module; imported by the packing child, so it must not import the application.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

RECORD_MEMBER = "Metadata/bamdude_assembly.json"
RECORD_VERSION = 1
MAX_RECORD_BYTES = 256 * 1024
ORIENTATIONS = ("as_is", "x+90", "x-90", "y+90", "y-90", "x180")
ROTATION_STEPS = (0, 45, 90, 180)


@dataclass(frozen=True)
class PartInput:
    name: str
    quantity: int
    fill: bool
    orientation: str


@dataclass(frozen=True)
class PrinterInput:
    printer: dict  # the three preset references, as {"source", "id"}
    process: dict
    filament: dict
    nozzle: int
    model: str  # display only


@dataclass(frozen=True)
class LayoutInput:
    spacing: float
    rotation_step: int
    centre: bool
    split_plates: bool


@dataclass(frozen=True)
class AssemblyRecord:
    set_id: str  # shared by every file written for one printer row
    base_name: str
    parts: tuple[PartInput, ...]  # object index in the 3MF = position here
    printer: PrinterInput
    layout: LayoutInput
    plates: tuple[dict, ...]  # the whole set: {"plate": n, "counts": {"<part index>": copies}, "fill": bool}
    version: int = RECORD_VERSION


def rotations(step: int) -> tuple[float, ...]:
    """The angles the packer may use for a rotation step in degrees; 0 means "do not rotate"."""
    if step not in ROTATION_STEPS:
        raise ValueError(f"rotation step {step!r} is not one of {ROTATION_STEPS}")
    if step == 0:
        return (0.0,)
    return tuple(float(angle) for angle in range(0, 360, step))


def dump(record: AssemblyRecord) -> bytes:
    return json.dumps(asdict(record), sort_keys=True, ensure_ascii=False).encode("utf-8")


def _fail(why: str) -> ValueError:
    return ValueError(f"not an assembly record: {why}")


def _ref(value: object) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("source"), str) or not isinstance(value.get("id"), str):
        raise _fail("a preset reference is not {source, id}")
    return {"source": value["source"], "id": value["id"]}


def _whole(value: object, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def load(data: bytes) -> AssemblyRecord:
    """Parse and validate. ``ValueError("not an assembly record: …")`` for anything else."""
    if len(data) > MAX_RECORD_BYTES:
        raise _fail("too large")
    try:
        raw = json.loads(data)
    except (ValueError, RecursionError):
        raise _fail("not JSON") from None
    if not isinstance(raw, dict) or raw.get("version") != RECORD_VERSION:
        raise _fail("unknown version")
    try:
        set_id, base_name = raw["set_id"], raw["base_name"]
        parts_raw, printer_raw, layout_raw, plates_raw = raw["parts"], raw["printer"], raw["layout"], raw["plates"]
    except KeyError as error:
        raise _fail(f"{error.args[0]} is missing") from None
    if not isinstance(set_id, str) or not set_id or not isinstance(base_name, str):
        raise _fail("set id or name is not text")
    if not isinstance(parts_raw, list) or not parts_raw or not isinstance(plates_raw, list):
        raise _fail("parts or plates are not lists")
    if not isinstance(printer_raw, dict) or not isinstance(layout_raw, dict):
        raise _fail("printer or layout is not an object")

    parts = []
    for part in parts_raw:
        if not isinstance(part, dict) or not isinstance(part.get("name"), str):
            raise _fail("a part has no name")
        if not _whole(part.get("quantity"), 1) or not isinstance(part.get("fill"), bool):
            raise _fail("a part's quantity or fill flag is wrong")
        if part.get("orientation") not in ORIENTATIONS:
            raise _fail("a part has an unknown orientation")
        parts.append(PartInput(part["name"], part["quantity"], part["fill"], part["orientation"]))

    if not _whole(printer_raw.get("nozzle"), 1) or not isinstance(printer_raw.get("model"), str):
        raise _fail("the printer's nozzle or model is wrong")
    printer = PrinterInput(
        _ref(printer_raw.get("printer")),
        _ref(printer_raw.get("process")),
        _ref(printer_raw.get("filament")),
        printer_raw["nozzle"],
        printer_raw["model"],
    )

    spacing = layout_raw.get("spacing")
    if isinstance(spacing, bool) or not isinstance(spacing, (int, float)) or not 0 <= spacing <= 1000:
        raise _fail("the spacing is wrong")
    if layout_raw.get("rotation_step") not in ROTATION_STEPS or isinstance(layout_raw.get("rotation_step"), bool):
        raise _fail("the rotation step is wrong")
    if not isinstance(layout_raw.get("centre"), bool) or not isinstance(layout_raw.get("split_plates"), bool):
        raise _fail("a layout flag is wrong")
    layout = LayoutInput(float(spacing), layout_raw["rotation_step"], layout_raw["centre"], layout_raw["split_plates"])

    plates = []
    for plate in plates_raw:
        if not isinstance(plate, dict) or not _whole(plate.get("plate"), 0) or not isinstance(plate.get("fill"), bool):
            raise _fail("a plate entry is wrong")
        counts = plate.get("counts")
        if not isinstance(counts, dict):
            raise _fail("a plate has no counts")
        for index, copies in counts.items():
            if not str(index).isdigit() or int(index) >= len(parts) or not _whole(copies, 1):
                raise _fail("a plate counts a part that does not exist")
        plates.append(
            {"plate": plate["plate"], "counts": {str(k): v for k, v in counts.items()}, "fill": plate["fill"]}
        )

    return AssemblyRecord(set_id, base_name, tuple(parts), printer, layout, tuple(plates))
