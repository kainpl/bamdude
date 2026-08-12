"""Each plate carries its own layer count.

The number lives in the plate's g-code header, as ``; total layer number: N``
(BambuStudio ``GCodeProcessor.cpp``) — not in ``slice_info.config``, which
carries the time, the weight and the first-layer time and no layer count at all.

⚠️ **Per PLATE, not per file.** Plate 1 of a container can be 200 layers and
plate 5 eighty, which is why this is a key inside
``file_metadata['plates'][*]`` and NOT a column on ``library_files``: one
number for the whole 3MF would be a guess dressed as a fact.

⚠️ Only the head of each entry is read. A plate's g-code runs to tens of
megabytes and the answer is in the first kilobyte.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from backend.app.services.archive import parse_plates_from_3mf, read_total_layers

_SLICE_INFO = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="prediction" value="3600"/>
    <metadata key="weight" value="12.5"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <metadata key="prediction" value="900"/>
    <metadata key="weight" value="3.0"/>
  </plate>
</config>
"""


def _three_mf(plate_headers: dict[int, str]) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Metadata/slice_info.config", _SLICE_INFO)
        for idx, header in plate_headers.items():
            zf.writestr(f"Metadata/plate_{idx}.gcode", header)
    buf.seek(0)
    return zipfile.ZipFile(buf)


class TestReadingOneHeader:
    @pytest.mark.parametrize(
        "header",
        [
            "; total layer number: 368\nG28\n",
            ";total layer number:368\n",
            "; TOTAL LAYER NUMBER: 368\n",
            "; HEADER_BLOCK_START\n; total layer number: 368\n; more\n",
        ],
    )
    def test_it_finds_the_marker_however_it_is_spaced(self, header: str) -> None:
        with _three_mf({1: header}) as zf:
            assert read_total_layers(zf, "Metadata/plate_1.gcode") == 368

    def test_a_header_without_it_answers_nothing(self) -> None:
        """``None``, not zero — a plate whose count we could not read is not a
        plate with no layers."""
        with _three_mf({1: "; some other comment\nG28\n"}) as zf:
            assert read_total_layers(zf, "Metadata/plate_1.gcode") is None

    def test_a_missing_entry_answers_nothing(self) -> None:
        with _three_mf({1: "; total layer number: 5\n"}) as zf:
            assert read_total_layers(zf, "Metadata/plate_9.gcode") is None


class TestPerPlate:
    def test_each_plate_reports_its_own(self) -> None:
        """⚠️ The whole reason this is not one number on the file."""
        with _three_mf({1: "; total layer number: 200\n", 2: "; total layer number: 80\n"}) as zf:
            plates = parse_plates_from_3mf(zf)

        assert {p["index"]: p["total_layers"] for p in plates} == {1: 200, 2: 80}

    def test_an_unsliced_plate_is_none_and_does_not_break_the_others(self) -> None:
        with _three_mf({1: "; total layer number: 200\n", 2: "; nothing useful\n"}) as zf:
            plates = parse_plates_from_3mf(zf)

        assert {p["index"]: p["total_layers"] for p in plates} == {1: 200, 2: None}

    def test_the_key_is_always_present(self) -> None:
        """Readers can ask for it without checking whether the file predates the
        field — absent and unknown collapse to the same ``None``."""
        with _three_mf({1: "; nothing\n"}) as zf:
            plates = parse_plates_from_3mf(zf)

        assert "total_layers" in plates[0]
