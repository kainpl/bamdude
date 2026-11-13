"""project_settings.config composed from three flattened presets — the rule both slicers were seen to accept."""

import json

from backend.app.services.assembly.presets import FlatPreset
from backend.app.services.assembly.settings import compose_project_settings

PROCESS = FlatPreset(
    "process",
    "0.20mm Standard @X",
    {"name": "0.20mm Standard @X", "inherits": "fdm_process_common", "from": "system", "type": "process",
     "setting_id": "GP004", "layer_height": "0.2", "shared": "from-process", "compatible_printers": ["X 0.4 nozzle"]},
    "",
    (),
)  # fmt: skip
FILAMENT = FlatPreset(
    "filament",
    "Bambu PLA Basic @X",
    {"name": "Bambu PLA Basic @X", "type": "filament", "filament_id": "GFA00", "instantiation": "true",
     "nozzle_temperature": ["220"], "default_filament_colour": [""], "compatible_prints": [], "shared": "from-filament"},
    "",
    (),
)  # fmt: skip
PRINTER = FlatPreset(
    "printer",
    "X 0.4 nozzle",
    {"name": "X 0.4 nozzle", "type": "machine", "printer_model": "X", "nozzle_diameter": ["0.4"],
     "shared": "from-printer", "description": "d", "include": [], "base_id": "b"},
    "",
    (),
)  # fmt: skip


def _compose(process=PROCESS, filament=FILAMENT, printer=PRINTER, **kw) -> dict:
    kw.setdefault("nozzle", 1)
    kw.setdefault("nozzle_count", 1)
    return json.loads(compose_project_settings(process, filament, printer, **kw))


def test_settings_of_all_three_are_merged_and_the_printer_wins_a_shared_key():
    cfg = _compose()
    assert cfg["layer_height"] == "0.2" and cfg["nozzle_temperature"] == ["220"] and cfg["printer_model"] == "X"
    assert cfg["shared"] == "from-printer"


def test_keys_that_describe_a_preset_are_not_copied():
    cfg = _compose()
    for key in ("inherits", "type", "setting_id", "instantiation", "filament_id", "description", "include", "base_id",
                "compatible_printers", "compatible_prints"):  # fmt: skip
        assert key not in cfg, key


def test_the_identity_the_slicer_recognises_presets_by():
    cfg = _compose()
    assert (cfg["name"], cfg["from"], cfg["version"]) == ("project_settings", "project", "02.05.00.66")
    assert cfg["print_settings_id"] == "0.20mm Standard @X" and cfg["printer_settings_id"] == "X 0.4 nozzle"
    assert cfg["filament_settings_id"] == ["Bambu PLA Basic @X"] and cfg["filament_ids"] == ["GFA00"]
    assert cfg["inherits_group"] == ["", "", ""] and cfg["different_settings_to_system"] == ["", "", ""]


def test_a_user_preset_names_its_parent_and_its_changed_keys_in_slicer_order():
    process = FlatPreset("process", "My fine", {**PROCESS.flat, "name": "My fine"}, "0.20mm Standard @X",
                         ("layer_height", "wall_loops"))  # fmt: skip
    printer = FlatPreset("printer", "My X", {**PRINTER.flat, "name": "My X"}, "X 0.4 nozzle", ("retraction_length",))
    cfg = _compose(process=process, printer=printer)
    # order: process, filaments, printer
    assert cfg["inherits_group"] == ["0.20mm Standard @X", "", "X 0.4 nozzle"]
    assert cfg["different_settings_to_system"] == ["layer_height;wall_loops", "", "retraction_length"]
    assert cfg["print_settings_id"] == "My fine" and cfg["printer_settings_id"] == "My X"


def test_filament_colour_is_the_presets_own_or_white():
    assert _compose()["filament_colour"] == ["#FFFFFF"]
    tinted = FlatPreset("filament", "T", {**FILAMENT.flat, "default_filament_colour": ["", "#112233"]}, "", ())
    assert _compose(filament=tinted)["filament_colour"] == ["#112233"]
    bare = FlatPreset(
        "filament", "B", {k: v for k, v in FILAMENT.flat.items() if k != "default_filament_colour"}, "", ()
    )
    assert _compose(filament=bare)["filament_colour"] == ["#FFFFFF"]


def test_filament_self_index_follows_the_number_of_extruder_variants():
    assert _compose()["filament_self_index"] == ["1"]
    two = FlatPreset("filament", "V", {**FILAMENT.flat, "filament_extruder_variant": ["Standard", "High Flow"]}, "", ())
    assert _compose(filament=two)["filament_self_index"] == ["1", "1"]


def test_the_nozzle_is_pinned_only_on_a_printer_with_several():
    assert "filament_map" not in _compose() and "filament_map_mode" not in _compose()
    cfg = _compose(nozzle=2, nozzle_count=2)
    assert cfg["filament_map"] == ["2"] and cfg["filament_map_mode"] == "Manual"


def test_a_missing_filament_id_is_an_empty_string():
    bare = FlatPreset("filament", "B", {"name": "B", "nozzle_temperature": ["200"]}, "", ())
    assert _compose(filament=bare)["filament_ids"] == [""]


def test_output_is_stable_json_bytes():
    a = compose_project_settings(PROCESS, FILAMENT, PRINTER, nozzle=1, nozzle_count=1)
    assert a == compose_project_settings(PROCESS, FILAMENT, PRINTER, nozzle=1, nozzle_count=1)
    assert isinstance(a, bytes) and list(json.loads(a)) == sorted(json.loads(a))
