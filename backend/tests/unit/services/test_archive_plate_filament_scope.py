"""#1785: per-slot filament in a multi-plate archive must be scoped to the
printed plate.

``ThreeMFParser`` already pulls the headline ``print_time_seconds`` /
``filament_used_grams`` from the matched ``<plate>``. Before this fix the
per-slot ``filament_slots`` list was collected with a document-wide
``root.findall(".//filament")``, so a multi-plate 3MF's archive card (and the
completion notification's per-slot breakdown) listed every plate's filament,
not just the printed one. The one-line fix scopes the filament scan to the
matched plate, falling back to document-wide only when no plate matched.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from backend.app.services.archive import ThreeMFParser

_SLICE_INFO_TWO_PLATES = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="3600"/>
    <metadata key="weight" value="10.5"/>
    <filament id="1" type="PLA" color="#FF0000" used_g="10.5"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <metadata key="prediction" value="7200"/>
    <metadata key="weight" value="25.0"/>
    <filament id="1" type="PETG" color="#00FF00" used_g="15.0"/>
    <filament id="2" type="TPU" color="#0000FF" used_g="10.0"/>
  </plate>
</config>
"""


def _write_3mf(path: Path, slice_info: str) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", slice_info)
    return path


def test_filament_slots_scoped_to_printed_plate(tmp_path):
    """plate_number=2 → filament_slots contains only plate 2's PETG + TPU, not
    plate 1's PLA. Headline grams/time come from plate 2 too."""
    path = _write_3mf(tmp_path / "multi.3mf", _SLICE_INFO_TWO_PLATES)

    meta = ThreeMFParser(path, plate_number=2).parse()

    slots = meta["filament_slots"]
    types = {s["type"] for s in slots}
    assert types == {"PETG", "TPU"}, f"expected only plate-2 filaments, got {types}"
    assert "PLA" not in types  # plate 1's filament must not leak in

    # Headline figures are plate-2 scoped (already the case pre-fix; pinned here).
    assert meta["print_time_seconds"] == 7200
    assert meta["filament_used_grams"] == 25.0

    # Per-slot filament type/color strings also reflect the printed plate.
    assert set(meta["filament_type"].split(", ")) == {"PETG", "TPU"}


def test_filament_slots_falls_back_to_document_wide_without_plate_match(tmp_path):
    """No matching plate (unknown index) → parser keeps the first plate's
    ``<plate>`` (root.find fallback) so filament stays plate-1 scoped, never
    empty. This pins the ``plate is not None`` fallback branch."""
    path = _write_3mf(tmp_path / "multi.3mf", _SLICE_INFO_TWO_PLATES)

    # plate_number=99 doesn't exist → _parse_slice_info falls back to the first
    # <plate>, so filament scopes to plate 1 (PLA).
    meta = ThreeMFParser(path, plate_number=99).parse()

    types = {s["type"] for s in meta["filament_slots"]}
    assert types == {"PLA"}
