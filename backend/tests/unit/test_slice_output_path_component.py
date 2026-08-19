"""A slice output's path is built from a name a folder can actually have.

Ported from upstream #2832. A print's display name comes from inside the 3MF,
not from the filename, so a MakerWorld title arrives with its punctuation:
"Planter Pot with Drip Tray, 12 cm / 5 inches". The slice-to-archive sink used
it verbatim for both the output folder and the output file.

⚠️ A slash in a folder name is not a character — it is another folder.
``mkdir(parents=True)`` created the level it implied, the file's own join added
a third that nobody had made, and the slice died on ENOENT against a path that
half existed. Renaming the print first was the only way through.

⚠️ The containment guard does not catch this. The deeper path is still *under*
the archive directory, so ``safe_join_under`` passes it and the failure happens
afterwards. Containment answers "could this escape", not "is this one
component".
"""

from __future__ import annotations

import pytest

from backend.app.utils.filename import MAX_FILENAME_BYTES, safe_path_component


class TestReducingAName:
    def test_the_reported_title(self):
        result = safe_path_component("Planter Pot with Drip Tray, 12 cm / 5 inches", fallback="x")

        assert "/" not in result
        assert result == "Planter Pot with Drip Tray, 12 cm - 5 inches"

    def test_a_name_still_reads_like_itself(self):
        """⚠️ Offending characters are REPLACED, not dropped, so the folder is
        still recognisable as the model it holds."""
        assert safe_path_component("Model: v2", fallback="x") == "Model- v2"

    def test_an_ordinary_name_is_untouched(self):
        assert safe_path_component("Benchy", fallback="x") == "Benchy"

    def test_punctuation_a_folder_can_hold_survives(self):
        assert safe_path_component("Bracket (v2) [final]", fallback="x") == "Bracket (v2) [final]"


class TestItCannotProduceMoreThanOneComponent:
    @pytest.mark.parametrize(
        "name",
        [
            "a/b",
            "a\\b",
            "../../etc/passwd",
            "..",
            ".",
            "nested/deep/path",
        ],
    )
    def test_no_separator_survives(self, name):
        result = safe_path_component(name, fallback="fallback")

        assert "/" not in result
        assert "\\" not in result

    def test_a_traversal_becomes_a_plain_name(self):
        assert safe_path_component("../../etc/passwd", fallback="x") == "-..-etc-passwd"

    def test_a_name_that_reduces_to_nothing_falls_back(self):
        """⚠️ Otherwise the reduction returns "" and the join produces the parent
        directory itself."""
        assert safe_path_component("..", fallback="fallback-used") == "fallback-used"
        assert safe_path_component("   ", fallback="fallback-used") == "fallback-used"
        assert safe_path_component("...", fallback="fallback-used") == "fallback-used"

    def test_a_name_of_only_separators_keeps_their_replacements(self):
        """Not a fallback case: "///" becomes "---", which is a usable folder
        name and closer to what the author typed than a generic stand-in. The
        fallback is for nothing surviving at all."""
        assert safe_path_component("///", fallback="fallback-used") == "---"

    def test_leading_and_trailing_dots_and_spaces_go(self):
        assert safe_path_component("  spaced  ", fallback="x") == "spaced"
        assert safe_path_component("...dotted...", fallback="x") == "dotted"


class TestControlCharacters:
    def test_a_control_character_is_replaced(self):
        assert safe_path_component("a\nb", fallback="x") == "a-b"
        assert safe_path_component("a\x00b", fallback="x") == "a-b"

    def test_del_is_replaced_too(self):
        assert safe_path_component("a\x7fb", fallback="x") == "a-b"


class TestTheLengthBudget:
    def test_a_long_name_is_cut_to_the_budget(self):
        result = safe_path_component("a" * 400, fallback="x", max_bytes=20)

        assert len(result.encode("utf-8")) <= 20

    def test_the_cut_never_splits_a_character(self):
        """⚠️ The cap is in BYTES because that is what the filesystem limits, so
        a naive slice can leave half a multi-byte character behind."""
        result = safe_path_component("ф" * 40, fallback="x", max_bytes=15)

        assert len(result.encode("utf-8")) <= 15
        result.encode("utf-8").decode("utf-8")  # must not raise

    def test_the_default_budget_is_one_components_worth(self):
        result = safe_path_component("b" * 400, fallback="x")

        assert len(result.encode("utf-8")) == MAX_FILENAME_BYTES

    def test_a_name_cut_down_to_nothing_falls_back(self):
        assert safe_path_component("...", fallback="fallback-used", max_bytes=1) == "fallback-used"


class TestTheCallersLeaveRoomForWhatTheyWrap:
    """⚠️ ``max_bytes`` is the budget for the component ALONE. A caller that
    wraps the result in a timestamp prefix and a ``.gcode.3mf`` extension has to
    subtract those, or the composed name still exceeds the filesystem limit."""

    @staticmethod
    def _source() -> str:
        import inspect

        from backend.app.api.routes import library

        return inspect.getsource(library)

    def test_the_archive_sink_reserves_its_prefix_and_extension(self):
        source = self._source()
        assert "reserve = max(" in source
        assert "MAX_FILENAME_BYTES - reserve" in source

    def test_the_library_sink_reserves_its_extension(self):
        assert 'MAX_FILENAME_BYTES - len(b".gcode.3mf")' in self._source()

    def test_both_the_folder_and_the_file_use_the_reduced_name(self):
        source = self._source()
        assert 'out_filename = f"{safe_base}.gcode.3mf"' in source
        assert 'archive_subdir = f"{timestamp}_{safe_base}_sliced"' in source

    def test_no_sink_still_builds_a_path_from_the_raw_name(self):
        source = self._source()
        assert 'out_filename = f"{base_name}.gcode.3mf"' not in source
        assert 'archive_subdir = f"{timestamp}_{base_name}_sliced"' not in source


def test_a_composed_archive_path_stays_within_one_component_each():
    """End to end on the reported shape: the folder and the file the sink builds
    are each a single component."""
    display = "Planter Pot with Drip Tray, 12 cm / 5 inches"
    timestamp = "20260819_120000"
    reserve = max(len(f"{timestamp}__sliced".encode()), len(b".gcode.3mf"))
    safe_base = safe_path_component(display, fallback="archive_1", max_bytes=MAX_FILENAME_BYTES - reserve)

    folder = f"{timestamp}_{safe_base}_sliced"
    filename = f"{safe_base}.gcode.3mf"

    for part in (folder, filename):
        assert "/" not in part and "\\" not in part
        assert len(part.encode("utf-8")) <= MAX_FILENAME_BYTES
