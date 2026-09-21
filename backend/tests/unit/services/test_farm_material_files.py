"""Minimal, geometry-free reproductions of the owner's four farm files.

The originals stay in temp/. They contain real structured ABS/PETG types and
foreign profile IDs, not a profile name masquerading as a material. No catalogue
or cloud synchronisation is consulted by this reader -> resolver test.
"""

import pytest

from backend.app.services.filament_requirements import read_print_requirements
from backend.app.services.filament_routing import RoutingPolicy, resolve_filament_routing
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.unit.services.test_filament_routing import feed, snapshot


@pytest.mark.parametrize(
    "model,material,profile,color",
    [
        ("N7", "ABS", "Pa240002", "#00FF00"),
        ("N7", "ABS", "GFB99", "#67DB25"),
        ("C12", "PETG", "P8e36324", "#000000"),
        ("N7", "PETG", "P8e36324", "#000000"),
    ],
)
def test_foreign_slicer_profiles_need_no_cloud(tmp_path, model, material, profile, color):
    path = write_routing_3mf(
        tmp_path / "foreign.gcode.3mf",
        {
            1: [
                {
                    "id": 1,
                    "type": material,
                    "tray_info_idx": profile,
                    "color": color,
                    "used_g": "46.87",
                    "group_id": "0",
                    "nozzle_diameter": "0.40",
                    "used_for_object": "true",
                    "used_for_support": "true",
                }
            ]
        },
        model=model,
        settings={
            "filament_type": [material],
            "filament_ids": [profile],
            "filament_settings_id": ["Test-test"],
            "nozzle_diameter": ["0.4"],
        },
        nozzle_groups={0: 1},
    )
    req = read_print_requirements(path, 1)
    assert req.status == "ok"
    assert req.used_filaments[0]["type"] == material
    state = snapshot(
        feed(0, color, kind="ams", material=material, variant="OTHER-UNKNOWN"),
        model=req.model,
        nozzle_diameters={0: (0.4,)},
    )
    assert resolve_filament_routing(req, RoutingPolicy(), state).plan.mapping == [0]
    assert (
        resolve_filament_routing(req, RoutingPolicy(allow_base_material_match=False), state).reason
        == "variant_mismatch"
    )
    wrong = snapshot(feed(0, color, kind="ams", material="PLA"), model=req.model, nozzle_diameters={0: (0.4,)})
    assert resolve_filament_routing(req, RoutingPolicy(), wrong).reason == "material_mismatch"
