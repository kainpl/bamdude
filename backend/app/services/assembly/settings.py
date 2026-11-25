"""``Metadata/project_settings.config`` of an assembled project, composed from three flattened presets.

No slicer takes part. The rule below was checked by slicing its output in both slicers (2026-11-13):
the Bambu Studio and OrcaSlicer command lines accept a config holding exactly the settings of the process,
filament and printer presets plus the identity keys, and fill in their own compiled-in defaults for
everything else. A config that names presets but carries no settings crashes the command line, so the
settings are always written in full.

How the slicer's window recognises the presets when the file is opened
(Bambu Studio ``PresetBundle::load_config_file_config``): by NAME — ``print_settings_id``,
``printer_settings_id``, ``filament_settings_id`` — with ``inherits_group`` naming each one's system parent
and ``different_settings_to_system`` listing the keys it changed, both in the order process, filaments,
printer. There is no cloud id in a project file.

Pure function; no I/O.
"""

from __future__ import annotations

import json

from backend.app.services.assembly.presets import META_KEYS, FlatPreset

PROJECT_VERSION = "02.05.00.66"  # the project format the writer declares; the slicer rewrites it on save
_DEFAULT_COLOUR = "#FFFFFF"


def _settings(preset: FlatPreset) -> dict:
    return {key: value for key, value in preset.flat.items() if key not in META_KEYS}


def _colour(filament: FlatPreset) -> str:
    colours = filament.flat.get("default_filament_colour")
    if isinstance(colours, str):
        colours = [colours]
    for colour in colours or []:
        if isinstance(colour, str) and colour.strip():
            return colour.strip()
    return _DEFAULT_COLOUR


def compose_project_settings(
    process: FlatPreset,
    filament: FlatPreset,
    printer: FlatPreset,
    *,
    nozzle: int,
    nozzle_count: int,
    bed_type: str | None = None,
) -> bytes:
    """The settings of a single-filament project printed with extruder ``nozzle`` (1-based)."""
    config: dict = {}
    for preset in (process, filament, printer):  # later presets win a shared key, as in the slicer
        config.update(_settings(preset))

    variants = filament.flat.get("filament_extruder_variant")
    config.update(
        {
            "name": "project_settings",
            "from": "project",
            "version": PROJECT_VERSION,
            "print_settings_id": process.name,
            "printer_settings_id": printer.name,
            "filament_settings_id": [filament.name],
            "filament_ids": [str(filament.flat.get("filament_id") or "")],
            "filament_colour": [_colour(filament)],
            "inherits_group": [process.parent, filament.parent, printer.parent],
            "different_settings_to_system": [
                ";".join(process.changed),
                ";".join(filament.changed),
                ";".join(printer.changed),
            ],
            "filament_self_index": ["1"] * max(1, len(variants) if isinstance(variants, list) else 0),
        }
    )
    if nozzle_count > 1:
        # Without these the slicer chooses the nozzle itself ("Auto For Flush") and may pick the one whose
        # reach the layout was not made for. It keeps both keys when it saves or slices.
        config["filament_map"] = [str(nozzle)]
        config["filament_map_mode"] = "Manual"
    if bed_type is not None:
        config["curr_bed_type"] = bed_type
    return json.dumps(config, indent=4, sort_keys=True, ensure_ascii=False).encode("utf-8")
