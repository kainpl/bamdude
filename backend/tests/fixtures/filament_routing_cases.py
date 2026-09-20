"""Synthetic sliced files; no farm files, credentials or model geometry."""

import json
import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring


def write_routing_3mf(
    path: Path,
    plates: dict[int, list[dict]],
    *,
    model: str = "C11",
    settings: dict | None = None,
    gcode_plates: list[int] | None = None,
    nozzle_groups: dict[int, int] | None = None,
    prediction: int | None = 3600,
    bed_type: str | None = None,
    plate_pngs: dict[int, bytes] | None = None,
) -> Path:
    """Preserve supplied usage strings and sparse IDs, including invalid data.

    ``nozzle_groups`` maps slicer group IDs to one-based extruder IDs (H2C).
    Geometry is unnecessary: these tests never send a file to a printer.

    ``prediction`` is the per-plate print-time estimate the strict source reader
    hands back as ``PrintRequirements.print_time_seconds``. It is a parameter
    because a caller that also writes ``LibraryFile.file_metadata`` has to make
    the two agree: the plan reads the metadata, the writers read the file, and a
    row whose stored estimate came from a 3MF that says something else is a
    fixture that proves nothing.

    ``prediction=None`` omits the key entirely, which is a real 3MF: a plate whose
    slicer wrote no estimate. It is not the same as ``0`` — a reader can tell "no
    answer" from "zero seconds", and the queue card has to.

    ``bed_type`` writes the plate's ``curr_bed_type`` — the third value the queue
    card's per-plate reader takes out of a 3MF, beside the estimate and the
    filament weight. Omitted by default so every existing caller's file is
    byte-identical.

    ``plate_pngs`` writes ``Metadata/plate_<N>.png`` — the slicer's render of a
    plate, which is the picture a queue row shows. Per plate and opt-in, because
    "this plate has no render" is a real 3MF (an STL-sourced convert, a raw
    G-code source) and a fixture that always had one could not tell the two
    apart.
    """
    root = Element("config")
    for plate_id, filaments in plates.items():
        plate = SubElement(root, "plate")
        facts = [("index", plate_id), ("printer_model_id", model)]
        if prediction is not None:
            facts.append(("prediction", prediction))
        for key, value in facts:
            SubElement(plate, "metadata", key=key, value=str(value))
        if bed_type is not None:
            SubElement(plate, "metadata", key="curr_bed_type", value=bed_type)
        for filament in filaments:
            SubElement(plate, "filament", {k: str(v) for k, v in filament.items() if v is not None})
        for group_id, extruder_id in (nozzle_groups or {}).items():
            SubElement(plate, "nozzle", id=str(group_id), extruder_id=str(extruder_id))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", tostring(root, encoding="utf-8"))
        if settings is not None:
            zf.writestr("Metadata/project_settings.config", json.dumps(settings))
        for plate_id in plates if gcode_plates is None else gcode_plates:
            zf.writestr(f"Metadata/plate_{plate_id}.gcode", f"; printer_model = {model}\n; plate {plate_id}\n")
        for plate_id, png in (plate_pngs or {}).items():
            zf.writestr(f"Metadata/plate_{plate_id}.png", png)
    return path


def mixed_filaments(*, reverse: bool = False) -> list[dict]:
    """Two used channels with opposite physical-nozzle bindings across plates."""
    return [
        {"id": 1, "type": "PLA", "color": "#FF0000", "used_g": "12.5", "group_id": int(reverse)},
        {"id": 2, "type": "PETG", "color": "#00FF00", "used_g": "0.0001", "group_id": int(not reverse)},
    ]


DUAL_SETTINGS = {
    "physical_extruder_map": ["1", "0"],
    "filament_nozzle_map": ["0", "1"],
    "nozzle_diameter": ["0.4", "0.6"],
}
