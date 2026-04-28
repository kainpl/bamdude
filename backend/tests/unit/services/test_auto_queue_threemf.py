"""Unit tests for ``services/auto_queue_threemf.py``.

Verifies the thin wrapper over ThreeMFParser that the auto-queue
scheduler uses to extract routing requirements (target model, required
filament types, print time) from a 3MF.
"""

import zipfile
from pathlib import Path

from backend.app.services.auto_queue_threemf import (
    AutoQueueRequirements,
    extract_auto_queue_requirements,
)

SLICE_INFO_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="printer_model_id" value="{model_id}"/>
    <metadata key="prediction" value="{prediction}"/>
    <metadata key="weight" value="42.5"/>
    {filaments}
  </plate>
</config>
"""


def _make_3mf(path: Path, model_id: str = "C11", prediction: int = 3600, filaments: list[dict] | None = None) -> None:
    """Build a minimal 3MF with a single plate and filament info.

    filaments: list of {id, type, color, used_g}.
    """
    if filaments is None:
        filaments = [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": 30.0}]
    fil_xml = "\n".join(
        f'    <filament id="{f["id"]}" type="{f["type"]}" color="{f["color"]}" used_g="{f["used_g"]}"/>'
        for f in filaments
    )
    content = SLICE_INFO_TEMPLATE.format(model_id=model_id, prediction=prediction, filaments=fil_xml)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", content)


class TestExtractAutoQueueRequirements:
    def test_extracts_model_and_single_filament(self, tmp_path: Path) -> None:
        f = tmp_path / "single.3mf"
        # C11 → X1C in normalize_printer_model_id
        _make_3mf(f, model_id="C11", prediction=1800)

        reqs = extract_auto_queue_requirements(f)

        assert isinstance(reqs, AutoQueueRequirements)
        # Whatever the printer registry maps "C11" to, target_model is set.
        assert reqs.target_model is not None
        assert reqs.required_filament_types == ["PLA"]
        assert reqs.print_time_seconds == 1800
        assert len(reqs.filament_slots) == 1
        assert reqs.filament_slots[0]["type"] == "PLA"

    def test_dedupes_filament_types(self, tmp_path: Path) -> None:
        f = tmp_path / "multi.3mf"
        _make_3mf(
            f,
            filaments=[
                {"id": 1, "type": "PLA", "color": "#FF0000", "used_g": 10},
                {"id": 2, "type": "PLA", "color": "#00FF00", "used_g": 20},
                {"id": 3, "type": "PETG", "color": "#0000FF", "used_g": 15},
            ],
        )

        reqs = extract_auto_queue_requirements(f)

        # PLA appears twice in slots but only once in required types
        assert reqs.required_filament_types == ["PLA", "PETG"]
        assert len(reqs.filament_slots) == 3

    def test_excludes_unused_filaments(self, tmp_path: Path) -> None:
        """Slots with used_g=0 are AMS-loaded but not consumed — must not appear."""
        f = tmp_path / "unused.3mf"
        _make_3mf(
            f,
            filaments=[
                {"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": 50},
                {"id": 2, "type": "ABS", "color": "#000000", "used_g": 0},  # loaded but unused
                {"id": 3, "type": "PETG", "color": "#888888", "used_g": 5},
            ],
        )

        reqs = extract_auto_queue_requirements(f)

        assert "ABS" not in reqs.required_filament_types
        assert set(reqs.required_filament_types) == {"PLA", "PETG"}

    def test_corrupted_3mf_returns_empty_requirements(self, tmp_path: Path) -> None:
        """ThreeMFParser swallows + warns on bad files; our wrapper must not raise."""
        f = tmp_path / "bad.3mf"
        f.write_bytes(b"not a zip")

        reqs = extract_auto_queue_requirements(f)

        assert reqs.target_model is None
        assert reqs.required_filament_types == []
        assert reqs.print_time_seconds is None
        assert reqs.filament_slots == []

    def test_minimal_3mf_no_filaments_returns_empty_types(self, tmp_path: Path) -> None:
        """Valid 3MF without filament info → empty types, no crash."""
        f = tmp_path / "empty.3mf"
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("Metadata/slice_info.config", "<config><plate/></config>")

        reqs = extract_auto_queue_requirements(f)

        assert reqs.required_filament_types == []
        assert reqs.filament_slots == []

    def test_plate_id_passed_through(self, tmp_path: Path) -> None:
        """plate_id arg threads to ThreeMFParser.plate_number."""
        f = tmp_path / "p.3mf"
        _make_3mf(f)

        # Just verify it doesn't blow up with a plate id; ThreeMFParser's
        # plate-specific extraction is exercised in archive tests.
        reqs = extract_auto_queue_requirements(f, plate_id=1)
        assert reqs.target_model is not None
