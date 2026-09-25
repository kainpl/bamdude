"""What the slicer wrote about Filament Track Switch inlets, read back from a 3MF.

BambuStudio's print dialog recommends which switch inlet each filament should
sit behind and says how much time the current arrangement costs
(``SelectMachineDialog::get_filament_suggest_pos`` /
``get_filament_change_gap_time``). Both are computed from data the slicer
already put in the file, so this module only reads it; the recommendation and
the channel simulator run in the print dialog (``frontend/src/utils/ftsArrangement.ts``),
where the live mapping and the printer's inlet bindings are.

A leaf reader used by the two ``filament-requirements`` routes and nothing else
— print-time matching, the queues and the dispatcher never see it.
"""

from __future__ import annotations

import json
import logging
import zipfile

import defusedxml.ElementTree as ET

logger = logging.getLogger(__name__)

SEQUENCE_FILE = "Metadata/filament_sequence.json"
SLICE_INFO_FILE = "Metadata/slice_info.config"
PROJECT_SETTINGS_FILE = "Metadata/project_settings.config"


def _seconds(value: object) -> float:
    """A ``coFloat`` from project settings: a string, a number, or (defensively) a list."""
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _plate_key(sequences: dict, plate_id: int | None) -> str | None:
    if plate_id is not None:
        key = f"plate_{plate_id}"
        return key if key in sequences else None
    keys = [k for k in sequences if k.startswith("plate_")]
    return keys[0] if len(keys) == 1 else None


def _plate_nozzles(zf: zipfile.ZipFile, plate_number: int) -> list[dict[str, int]]:
    """The plate's ``<nozzle id=… extruder_id=…/>`` tags; extruder written 1-based."""
    root = ET.fromstring(zf.read(SLICE_INFO_FILE).decode())
    for plate in root.findall(".//plate"):
        index = next((m.get("value") for m in plate.findall("metadata") if m.get("key") == "index"), None)
        if index is None or int(index) != plate_number:
            continue
        return [
            {"id": int(n.get("id")), "extruder_id": int(n.get("extruder_id")) - 1}
            for n in plate.findall("nozzle")
            if n.get("id") is not None and n.get("extruder_id") is not None
        ]
    return []


def read_track_switch_plan(zf: zipfile.ZipFile, plate_id: int | None) -> dict | None:
    """The plate's slicer inputs for the inlet recommendation, or ``None``.

    ``filament_sequence`` is converted to 0-based filament ids (the file stores
    them 1-based, BambuStudio subtracts 1 on load); ``optimal_assignment`` is
    already indexed by 0-based filament id; nozzle extruders are converted to
    0-based. ``None`` whenever any input is missing or unreadable — a file
    sliced elsewhere, an empty plate, times of zero — because a partial plan
    would make the simulator report a saving that is not there.
    """
    try:
        names = set(zf.namelist())
        if not {SEQUENCE_FILE, SLICE_INFO_FILE, PROJECT_SETTINGS_FILE} <= names:
            return None
        sequences = json.loads(zf.read(SEQUENCE_FILE))
        if not isinstance(sequences, dict):
            return None
        key = _plate_key(sequences, plate_id)
        if key is None:
            return None
        plate = sequences[key]
        if not isinstance(plate, dict):
            return None
        raw_sequence = plate.get("filament_sequence", plate.get("sequence")) or []
        nozzle_sequence = [int(n) for n in plate.get("nozzle_sequence") or []]
        assignment = [int(g) for g in plate.get("optimal_assignment") or []]
        filament_sequence = [int(f) - 1 for f in raw_sequence]
        if not filament_sequence or not nozzle_sequence or not assignment:
            return None

        nozzles = _plate_nozzles(zf, int(key.removeprefix("plate_")))
        if not nozzles:
            return None

        settings = json.loads(zf.read(PROJECT_SETTINGS_FILE))
        load_time = _seconds(settings.get("machine_load_filament_time"))
        unload_time = _seconds(settings.get("machine_unload_filament_time"))
        if load_time <= 0 and unload_time <= 0:
            return None
    except (KeyError, ValueError, TypeError, AttributeError, ET.ParseError, zipfile.BadZipFile) as exc:
        logger.debug("No track-switch plan in 3MF: %s", exc)
        return None

    return {
        "optimal_assignment": assignment,
        "filament_sequence": filament_sequence,
        "nozzle_sequence": nozzle_sequence,
        "nozzles": nozzles,
        "load_time": load_time,
        "unload_time": unload_time,
    }
