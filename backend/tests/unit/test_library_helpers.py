"""Unit tests for ``services.library_helpers`` — the single source of
truth for ``LibraryFile.file_type`` + ``LibraryFile.file_tags`` derivation.

Both helpers are pure, so we can test against the canonical inputs
(filename, prior file_type, file_metadata, source_type, swap_compatible)
without touching the database. The 7 LibraryFile() construction sites
+ the m035/m036 migrations all funnel through these — if these tests
hold, every write path is internally consistent.
"""

from backend.app.services.library_helpers import compute_file_tags, detect_file_type


class TestDetectFileType:
    def test_sliced_3mf_collapses_to_gcode(self):
        # The historical inconsistency: upload route stored "3mf",
        # external scan stored "gcode.3mf", slicer-output stored "gcode".
        # Helper picks slicer's interpretation as canonical.
        assert detect_file_type("model.gcode.3mf") == "gcode"
        assert detect_file_type("Some Multi.Word.Project.gcode.3mf") == "gcode"

    def test_unsliced_3mf(self):
        assert detect_file_type("project.3mf") == "3mf"

    def test_raw_gcode(self):
        assert detect_file_type("ready_to_print.gcode") == "gcode"

    def test_stl(self):
        assert detect_file_type("part.stl") == "stl"
        assert detect_file_type("PART.STL") == "stl"  # case-insensitive

    def test_step_variants(self):
        assert detect_file_type("model.step") == "step"
        assert detect_file_type("model.stp") == "stp"

    def test_unknown_extension(self):
        assert detect_file_type("readme.txt") == "txt"  # actual ext kept
        assert detect_file_type("noextension") == "unknown"
        assert detect_file_type("") == "unknown"

    def test_case_insensitive_compound(self):
        # User-uploaded files from Windows often have mixed case in the
        # extension. The compound detection has to ignore case so a
        # ``.GCODE.3MF`` upload doesn't slip through as plain "3mf".
        assert detect_file_type("MODEL.GCODE.3MF") == "gcode"


class TestComputeFileTags:
    def test_sliced_3mf_carries_both_format_tags(self):
        # The whole reason the function exists — restoring the visual
        # distinction the singular file_type lost when m035 collapsed
        # ``.gcode.3mf`` to ``"gcode"``.
        tags = compute_file_tags(
            filename="output.gcode.3mf",
            file_type="gcode",
            file_metadata=None,
            source_type=None,
            swap_compatible=False,
        )
        assert tags == ["gcode", "3mf"]

    def test_raw_gcode_only_gcode_tag(self):
        tags = compute_file_tags(
            filename="raw.gcode",
            file_type="gcode",
            file_metadata=None,
            source_type=None,
            swap_compatible=False,
        )
        assert tags == ["gcode"]

    def test_unsliced_3mf_emits_3mf_and_project(self):
        # Unsliced .3mf carries both the format chip (``3mf``) and the
        # readiness chip (``project``). Emission order is purely
        # semantic — visual ordering happens on the frontend via
        # ``sortTagsForDisplay``.
        tags = compute_file_tags(
            filename="project.3mf",
            file_type="3mf",
            file_metadata=None,
            source_type=None,
            swap_compatible=False,
        )
        assert tags == ["3mf", "project"]

    def test_stl_with_makerworld_provenance(self):
        tags = compute_file_tags(
            filename="part.stl",
            file_type="stl",
            file_metadata=None,
            source_type="makerworld",
            swap_compatible=False,
        )
        assert tags == ["stl", "geometry", "makerworld"]

    def test_obj_emits_obj_and_geometry(self):
        # OBJ wasn't recognised pre-m037 — it landed in the catch-all
        # "no format tag" bucket. Now it gets both its own format chip
        # AND the ``geometry`` readiness chip.
        tags = compute_file_tags(
            filename="model.obj",
            file_type="obj",
            file_metadata=None,
            source_type=None,
            swap_compatible=False,
        )
        assert tags == ["obj", "geometry"]

    def test_step_emits_step_and_geometry(self):
        tags = compute_file_tags(
            filename="cad.step",
            file_type="step",
            file_metadata=None,
            source_type=None,
            swap_compatible=False,
        )
        assert tags == ["step", "geometry"]

    def test_stp_collapses_to_step_format_chip(self):
        # The ``.stp`` extension is the same CAD format as ``.step``;
        # they share the ``step`` format chip so the filter doesn't
        # split a single concept across two adjacent toggles.
        tags = compute_file_tags(
            filename="cad.stp",
            file_type="stp",
            file_metadata=None,
            source_type=None,
            swap_compatible=False,
        )
        assert tags == ["step", "geometry"]

    def test_multi_plate_via_metadata_flag(self):
        tags = compute_file_tags(
            filename="multi.3mf",
            file_type="3mf",
            file_metadata={"is_multi_plate": True},
            source_type=None,
            swap_compatible=False,
        )
        assert tags == ["3mf", "project", "multiplate"]

    def test_multi_plate_via_plate_count(self):
        # Some rows pre-date m023's is_multi_plate flag and only carry
        # the plates list. The helper has to recognise both.
        tags = compute_file_tags(
            filename="multi.3mf",
            file_type="3mf",
            file_metadata={"plates": [{"id": 1}, {"id": 2}]},
            source_type=None,
            swap_compatible=False,
        )
        assert "multiplate" in tags

    def test_single_plate_metadata_no_multiplate_tag(self):
        tags = compute_file_tags(
            filename="single.3mf",
            file_type="3mf",
            file_metadata={"plates": [{"id": 1}]},
            source_type=None,
            swap_compatible=False,
        )
        assert "multiplate" not in tags

    def test_swap_tag_mirrors_flag(self):
        tags = compute_file_tags(
            filename="swap_set.3mf",
            file_type="3mf",
            file_metadata=None,
            source_type=None,
            swap_compatible=True,
        )
        assert "swap" in tags

    def test_full_composite_for_sliced_multiplate_swap(self):
        # The full kitchen sink — sliced multi-plate swap-compatible 3MF
        # produced by the BamDude sidecar. Emission order is purely
        # semantic (format → readiness → modifiers → provenance); the
        # frontend re-sorts for display.
        tags = compute_file_tags(
            filename="kitchen.gcode.3mf",
            file_type="gcode",
            file_metadata={"is_multi_plate": True},
            source_type="sliced",
            swap_compatible=True,
        )
        assert tags == ["gcode", "3mf", "sliced", "multiplate", "swap"]

    def test_project_source_type_no_longer_emits_project_tag(self):
        # Pre-m037, ``source_type`` starting with ``project_`` would emit
        # the ``project`` tag as a provenance signal. Post-m037 the tag is
        # reserved for "unsliced 3MF" (file-type semantic). This test pins
        # the new behavior so a regression doesn't silently re-emit it.
        tags = compute_file_tags(
            filename="asset.stl",
            file_type="stl",
            file_metadata=None,
            source_type="project_zip_import",
            swap_compatible=False,
        )
        assert "project" not in tags
        # STL still picks up its format + readiness chips despite the
        # unrecognised provenance.
        assert tags == ["stl", "geometry"]

    def test_unknown_format_yields_empty_format_tags(self):
        # Project-imported images / readme.txt / etc. don't get a format
        # tag. compute_file_tags returns whatever non-format tags apply
        # (none here) so the row simply renders without a primary badge.
        tags = compute_file_tags(
            filename="readme.txt",
            file_type="txt",
            file_metadata=None,
            source_type=None,
            swap_compatible=False,
        )
        assert tags == []

    def test_metadata_none_safe(self):
        # File_metadata may be None on legacy / external rows; the helper
        # has to treat that as "no metadata" without raising.
        tags = compute_file_tags(
            filename="legacy.3mf",
            file_type="3mf",
            file_metadata=None,
            source_type=None,
            swap_compatible=False,
        )
        assert tags == ["3mf", "project"]
