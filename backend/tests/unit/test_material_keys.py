"""A tray's material resolves to its base row in drying / threshold / preheat tables (upstream #3067)."""

import re
from pathlib import Path

import pytest

from backend.app.services.preheat import _target_for_type
from backend.app.services.print_scheduler import PrintScheduler
from backend.app.utils.material_keys import MATERIAL_KEY_ALIASES, resolve_material_key

TABLE = {"PLA": 1, "PETG": 2, "ABS": 3, "PA": 4, "PETG-CF": 5}


@pytest.mark.parametrize(
    ("tray_type", "key"),
    [
        ("PLA Basic", "PLA"),
        ("PETG-CF", "PETG-CF"),  # a row of its own wins over the base
        ("PLA-CF", "PLA"),
        ("ABS-GF", "ABS"),
        ("PA6-CF", "PA"),
        ("PAHT-CF", "PA"),
        ("PPA-CF", "PA"),
        ("Nylon", "PA"),
        ("TPU", None),
        ("", None),
        (None, None),
    ],
)
def test_resolve_material_key(tray_type, key):
    assert resolve_material_key(tray_type, TABLE) == key


def test_auto_drying_dries_a_composite_as_its_base():
    presets = {"PA": {"n3f": 65, "n3s": 85, "n3f_hours": 12, "n3s_hours": 8}}
    params = PrintScheduler()._get_conservative_drying_params([{"tray_type": "PA6-CF"}], "n3s", presets)
    assert params == (85, 8, "PA")


def test_humidity_threshold_applies_to_composites():
    assert PrintScheduler.resolve_humidity_threshold([{"tray_type": "ABS-GF"}], {"ABS": 25, "default": 40}, 60) == 25


def test_preheat_reads_polyamide_aliases():
    assert _target_for_type("PA6-CF", {"PA": 50, "DEFAULT": 0}) == 50


def test_frontend_aliases_mirror_the_backend():
    src = (Path(__file__).resolve().parents[3] / "frontend/src/utils/dryingPresets.ts").read_text(encoding="utf-8")
    start = src.index("DRYING_MATERIAL_ALIASES")
    block = src[start : src.index("};", start)]
    frontend = dict(re.findall(r"(\w+): '(\w+)'", block))
    assert frontend == MATERIAL_KEY_ALIASES
