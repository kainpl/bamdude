"""The actual AMS quick palette must never lose its exact catalog names."""

import re
from pathlib import Path

from backend.app.core.catalog_defaults import DEFAULT_COLOR_CATALOG


def test_every_quick_swatch_has_an_exact_catalog_entry():
    source = (Path(__file__).resolve().parents[3] / "frontend/src/components/ConfigureAmsSlotModal.tsx").read_text(
        encoding="utf-8"
    )
    arrays = re.findall(r"const QUICK_COLORS_(?:BASIC|EXTENDED) = \[(.*?)\];", source, re.S)
    assert len(arrays) == 2
    swatches = []
    for array in arrays:
        entries = re.findall(r"name: '([^']+)', hex: '([A-F0-9]{6})'", array)
        assert len(entries) == array.count("{")  # Do not silently skip a new chip.
        swatches.extend(entries)
    catalog_hexes = {hex_color.lstrip("#").upper() for _, _, hex_color, _ in DEFAULT_COLOR_CATALOG}
    assert [(name, hex_color) for name, hex_color in swatches if hex_color not in catalog_hexes] == []
