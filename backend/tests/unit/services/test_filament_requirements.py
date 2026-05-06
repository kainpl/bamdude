"""Unit tests for ``services/filament_requirements.py``.

The helper extracts per-slot ``{slot_id, type, color, used_grams}``
entries out of a 3MF for the VP auto-queue intake to pin
``force_color_match=True`` overrides on the new queue row (#1188).

The shape needs to match what
``services/auto_queue_eligibility._get_missing_force_color_slots``
validates — exact ``type+color`` matching against the printer's loaded
AMS state — so these tests pin the JSON shape, the slot ordering, and
the ``used_g <= 0`` exclusion (slot present in the slicer config but
not consumed by the chosen plate must NOT appear in the requirements
list — that would force the scheduler to refuse printers that don't
have the unused colour loaded).
"""

import zipfile
from pathlib import Path

from backend.app.services.filament_requirements import extract_filament_requirements

SLICE_INFO_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="printer_model_id" value="{model_id}"/>
    <metadata key="prediction" value="3600"/>
    {filaments}
  </plate>
</config>
"""


def _make_3mf(path: Path, model_id: str = "C11", filaments: list[dict] | None = None) -> None:
    if filaments is None:
        filaments = [{"id": 1, "type": "PLA", "color": "#FF0000", "used_g": 30.0}]
    fil_xml = "\n".join(
        f'    <filament id="{f["id"]}" type="{f["type"]}" color="{f["color"]}" used_g="{f["used_g"]}"/>'
        for f in filaments
    )
    content = SLICE_INFO_TEMPLATE.format(model_id=model_id, filaments=fil_xml)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", content)


class TestExtractFilamentRequirements:
    def test_returns_per_slot_type_color_pairs(self, tmp_path: Path) -> None:
        f = tmp_path / "two-slots.3mf"
        _make_3mf(
            f,
            filaments=[
                {"id": 1, "type": "PLA", "color": "#FF0000", "used_g": 10.0},
                {"id": 2, "type": "PETG", "color": "#00FF00", "used_g": 20.0},
            ],
        )

        slots = extract_filament_requirements(f)

        assert len(slots) == 2
        assert {s["slot_id"] for s in slots} == {1, 2}
        # Pin the exact shape the eligibility helper expects.
        for slot in slots:
            assert "slot_id" in slot
            assert "type" in slot
            assert "color" in slot
            assert "used_grams" in slot
            assert isinstance(slot["used_grams"], float)
        red_pla = next(s for s in slots if s["slot_id"] == 1)
        assert red_pla["type"] == "PLA"
        assert red_pla["color"] == "#FF0000"

    def test_sorts_by_slot_id(self, tmp_path: Path) -> None:
        f = tmp_path / "out-of-order.3mf"
        _make_3mf(
            f,
            filaments=[
                {"id": 3, "type": "PLA", "color": "#FF0000", "used_g": 5},
                {"id": 1, "type": "PETG", "color": "#00FF00", "used_g": 5},
                {"id": 2, "type": "ABS", "color": "#0000FF", "used_g": 5},
            ],
        )

        slots = extract_filament_requirements(f)

        assert [s["slot_id"] for s in slots] == [1, 2, 3]

    def test_excludes_unused_slots(self, tmp_path: Path) -> None:
        """Slots with used_g <= 0 are AMS-loaded but not consumed by the plate.
        Including them in the override list would force the scheduler to refuse
        any printer that doesn't have the unused colour loaded — exactly the
        regression #1188 was designed to avoid in reverse."""
        f = tmp_path / "unused.3mf"
        _make_3mf(
            f,
            filaments=[
                {"id": 1, "type": "PLA", "color": "#FF0000", "used_g": 30.0},
                {"id": 2, "type": "ABS", "color": "#000000", "used_g": 0},
                {"id": 3, "type": "PETG", "color": "#888888", "used_g": 12.0},
            ],
        )

        slots = extract_filament_requirements(f)

        assert {s["slot_id"] for s in slots} == {1, 3}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        slots = extract_filament_requirements(tmp_path / "does-not-exist.3mf")
        assert slots == []

    def test_corrupt_zip_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "garbage.3mf"
        f.write_bytes(b"not a zip file")
        slots = extract_filament_requirements(f)
        assert slots == []

    def test_zip_without_slice_info_returns_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "no-slice-info.3mf"
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("3D/3dmodel.model", "<model/>")
        slots = extract_filament_requirements(f)
        assert slots == []

    def test_slot_without_type_is_skipped(self, tmp_path: Path) -> None:
        """A filament entry without `type=` can't be matched against AMS state —
        better to silently drop it than to emit an override that always fails."""
        f = tmp_path / "missing-type.3mf"
        # Build by hand: one valid slot + one with empty type
        content = SLICE_INFO_TEMPLATE.format(
            model_id="C11",
            filaments=(
                '    <filament id="1" type="PLA" color="#FF0000" used_g="10"/>\n'
                '    <filament id="2" type="" color="#00FF00" used_g="5"/>'
            ),
        )
        with zipfile.ZipFile(f, "w") as zf:
            zf.writestr("Metadata/slice_info.config", content)

        slots = extract_filament_requirements(f)
        assert {s["slot_id"] for s in slots} == {1}

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        """Helper takes ``Path | str`` so callers don't have to wrap."""
        f = tmp_path / "str-path.3mf"
        _make_3mf(f)
        slots = extract_filament_requirements(str(f))
        assert len(slots) == 1
