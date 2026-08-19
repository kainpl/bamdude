"""Per-layer filament usage is read from the plate that actually printed.

Ported from upstream `454457a0`. The extractor took ``gcode_files[0]`` — the
first ``.gcode`` member in whatever order the ZIP stores them — under a comment
claiming there is "typically only one per 3MF export". On a multi-plate file
that is untrue, and a Bambu Studio export can store plate 2 first, so the
figures were measured against a plate the printer never ran.

⚠️ Both inventory backends consume this, so partial-usage tracking and the
Spoolman debit were wrong together and in the same direction — nothing
disagreed with anything, which is why it could sit unnoticed.

⚠️ Same family as the ``plate_number`` inertness that had Skip Objects
cancelling the wrong part: identifying a plate by position instead of by name,
and failing silently onto whatever came first.
"""

from __future__ import annotations

import zipfile

from backend.app.utils.threemf_tools import extract_layer_filament_usage_from_3mf


# One filament, one layer, distinct extrusion per plate so the answer says
# which plate was read. M620 picks the slot, M73 marks the layer, G1 E
# extrudes -- the three things the parser reads.
def _gcode(total_mm: float) -> str:
    return f"M620 S0\nM73 L1\nG1 E{total_mm}\n"


def _archive(tmp_path, members: list[tuple[str, float]]):
    path = tmp_path / "job.gcode.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        for name, mm in members:
            zf.writestr(name, _gcode(mm))
    return path


def _first_value(usage) -> float | None:
    if not usage:
        return None
    layer = sorted(usage)[0]
    values = usage[layer]
    return next(iter(values.values())) if values else None


class TestThePlateIsAsked:
    def test_the_named_plate_is_read_even_when_stored_second(self, tmp_path):
        path = _archive(tmp_path, [("Metadata/plate_1.gcode", 100.0), ("Metadata/plate_2.gcode", 200.0)])

        assert _first_value(extract_layer_filament_usage_from_3mf(path, 2)) == 200.0

    def test_the_reported_bug_shape_a_bambu_export_storing_plate_2_first(self, tmp_path):
        """Written in ZIP order 2, 1 — the case that made the old code answer
        for a plate the print never used."""
        path = _archive(tmp_path, [("Metadata/plate_2.gcode", 200.0), ("Metadata/plate_1.gcode", 100.0)])

        assert _first_value(extract_layer_filament_usage_from_3mf(path, 1)) == 100.0


class TestWithoutAPlate:
    def test_it_falls_back_to_the_lowest_numbered_plate(self, tmp_path):
        path = _archive(tmp_path, [("Metadata/plate_2.gcode", 200.0), ("Metadata/plate_1.gcode", 100.0)])

        assert _first_value(extract_layer_filament_usage_from_3mf(path)) == 100.0

    def test_the_answer_does_not_depend_on_zip_order(self, tmp_path, tmp_path_factory):
        """Determinism is the point: the same file written the other way round
        must not produce a different debit."""
        forward = _archive(tmp_path, [("Metadata/plate_1.gcode", 100.0), ("Metadata/plate_3.gcode", 300.0)])
        other = tmp_path_factory.mktemp("rev")
        reverse = _archive(other, [("Metadata/plate_3.gcode", 300.0), ("Metadata/plate_1.gcode", 100.0)])

        assert _first_value(extract_layer_filament_usage_from_3mf(forward)) == _first_value(
            extract_layer_filament_usage_from_3mf(reverse)
        )

    def test_an_absent_plate_falls_back_rather_than_returning_nothing(self, tmp_path):
        """A stale plate index must not cost the whole measurement."""
        path = _archive(tmp_path, [("Metadata/plate_1.gcode", 100.0)])

        assert _first_value(extract_layer_filament_usage_from_3mf(path, 7)) == 100.0


class TestItStillSurvivesOddInput:
    def test_a_single_plate_file_is_unaffected(self, tmp_path):
        path = _archive(tmp_path, [("Metadata/plate_1.gcode", 100.0)])

        assert _first_value(extract_layer_filament_usage_from_3mf(path, 1)) == 100.0

    def test_no_gcode_member_returns_none(self, tmp_path):
        path = tmp_path / "empty.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Metadata/model_settings.config", "<config/>")

        assert extract_layer_filament_usage_from_3mf(path, 1) is None

    def test_an_unnumbered_gcode_member_is_still_usable(self, tmp_path):
        path = _archive(tmp_path, [("Metadata/plate.gcode", 50.0)])

        assert _first_value(extract_layer_filament_usage_from_3mf(path)) == 50.0
