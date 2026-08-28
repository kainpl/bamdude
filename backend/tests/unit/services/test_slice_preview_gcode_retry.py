"""A preview slice survives custom G-code the sidecar cannot parse.

Ported from upstream #15ea11e1. Opening the slice dialog on an unsliced project
runs a preview slice purely to ask the slicer which AMS slots the chosen plate
consumes. Bambu Studio 2.8 writes ``{if timelapse_inline_photo}`` into the
machine's ``time_lapse_gcode`` without exporting a definition for that variable,
so the template is unresolvable the moment it leaves Studio — the slice dies on
a placeholder parse error before any slice_info exists, and the preview has
nothing to read.

⚠️ **Retried with the one named template emptied, still on the file's own
settings.** Keeping the embedded settings is what keeps the answer honest:
overriding the process preset instead discards the project's support
configuration, which loses whole slots and moves the reported grams.

⚠️ **Only templates that cannot extrude are eligible.** A start or
filament-change template lays a prime line or purges, so emptying one would move
the very grams the preview reports — and returning nothing beats a confident
wrong number.
"""

from __future__ import annotations

import json
import zipfile
from io import BytesIO

import pytest

from backend.app.services.slice_preview import (
    _blank_custom_gcode,
    _normalise_option,
    _unparsable_gcode_option,
)

PARSE_ERROR = (
    "Slicing failed\ntimelapse_gcode Parsing error at line 13: Not a variable name\n    {if timelapse_inline_photo}\n"
)


def _threemf(settings: dict, *, extra: dict[str, bytes] | None = None) -> bytes:
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("Metadata/project_settings.config", json.dumps(settings))
        zf.writestr("3D/3dmodel.model", b"<model/>")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return out.getvalue()


def _settings_of(file_bytes: bytes) -> dict:
    with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
        return json.loads(zf.read("Metadata/project_settings.config"))


class TestSpottingTheFailure:
    def test_the_reported_error_names_its_field(self):
        assert _unparsable_gcode_option(PARSE_ERROR) == "timelapsegcode"

    def test_the_name_is_normalised_across_spellings(self):
        """⚠️ The slicer reports ``timelapse_gcode`` while the 3MF stores
        ``time_lapse_gcode`` — a literal comparison finds nothing."""
        assert _normalise_option("timelapse_gcode") == _normalise_option("time_lapse_gcode")

    def test_an_unrelated_failure_is_not_a_retry(self):
        assert _unparsable_gcode_option("Slicing failed: objects outside the bed boundary") is None
        assert _unparsable_gcode_option("") is None

    @pytest.mark.parametrize(
        "field",
        ["machine_start_gcode", "change_filament_gcode", "machine_end_gcode", "filament_start_gcode"],
    )
    def test_a_template_that_can_extrude_is_never_eligible(self, field):
        """⚠️ The narrowness is the whole point. A start template lays a prime
        line, a filament-change template purges — silence either and the grams
        the preview reports are quietly wrong."""
        error = f"{field} Parsing error at line 4: Not a variable name"

        assert _unparsable_gcode_option(error) is None


class TestBlankingTheTemplate:
    def test_it_empties_the_named_field(self):
        original = _threemf({"time_lapse_gcode": "{if timelapse_inline_photo}\nM400", "layer_gcode": "M73"})

        patched = _blank_custom_gcode(original, "timelapsegcode")

        assert patched is not None
        assert _settings_of(patched)["time_lapse_gcode"] == ""

    def test_everything_else_is_left_alone(self):
        """⚠️ The file's own process settings, supports and per-slot filament
        assignments are what make the answer trustworthy."""
        original = _threemf(
            {
                "time_lapse_gcode": "boom",
                "support_type": "tree(auto)",
                "filament_settings_id": ["PLA", "PETG"],
            }
        )

        settings = _settings_of(_blank_custom_gcode(original, "timelapsegcode"))

        assert settings["support_type"] == "tree(auto)"
        assert settings["filament_settings_id"] == ["PLA", "PETG"]

    def test_a_per_extruder_template_keeps_its_container_type(self):
        """⚠️ Handing the CLI a bare string where it expects a list trades this
        parse error for a different one."""
        original = _threemf({"time_lapse_gcode": ["a", "b", "c"]})

        settings = _settings_of(_blank_custom_gcode(original, "timelapsegcode"))

        assert settings["time_lapse_gcode"] == ["", "", ""]

    def test_other_members_of_the_archive_survive(self):
        original = _threemf({"time_lapse_gcode": "boom"}, extra={"Metadata/plate_1.png": b"PNGDATA"})

        patched = _blank_custom_gcode(original, "timelapsegcode")

        with zipfile.ZipFile(BytesIO(patched)) as zf:
            assert zf.read("Metadata/plate_1.png") == b"PNGDATA"
            assert zf.read("3D/3dmodel.model") == b"<model/>"


class TestWhenThereIsNothingToDo:
    def test_a_field_that_is_already_empty(self):
        """Retrying would fail identically, so the caller must be able to skip."""
        assert _blank_custom_gcode(_threemf({"time_lapse_gcode": ""}), "timelapsegcode") is None

    def test_a_3mf_with_no_embedded_settings(self):
        out = BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("3D/3dmodel.model", b"<model/>")

        assert _blank_custom_gcode(out.getvalue(), "timelapsegcode") is None

    def test_something_that_is_not_a_zip(self):
        assert _blank_custom_gcode(b"not a zip at all", "timelapsegcode") is None

    def test_settings_that_are_not_an_object(self):
        out = BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("Metadata/project_settings.config", json.dumps(["a", "list"]))

        assert _blank_custom_gcode(out.getvalue(), "timelapsegcode") is None

    def test_unparsable_settings_json(self):
        out = BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("Metadata/project_settings.config", b"{not json")

        assert _blank_custom_gcode(out.getvalue(), "timelapsegcode") is None

    def test_a_non_template_setting_with_the_same_stem_is_not_touched(self):
        """⚠️ Only ``*_gcode`` keys are eligible, so the name fold cannot reach a
        setting that merely shares a stem."""
        original = _threemf({"time_lapse": "true", "time_lapse_gcode": "boom"})

        settings = _settings_of(_blank_custom_gcode(original, "timelapsegcode"))

        assert settings["time_lapse"] == "true"
        assert settings["time_lapse_gcode"] == ""


def test_the_recovery_is_decided_before_anything_is_logged():
    """⚠️ A slice that recovers must not announce itself as a failure twenty
    seconds before it succeeds."""
    import inspect

    from backend.app.services import slice_preview

    source = inspect.getsource(slice_preview.get_preview_filaments)
    block = source[source.index("except SlicerApiError as e:") :]
    decision = block.index("_unparsable_gcode_option")
    first_warning = block.index("logger.warning")
    assert decision < first_warning
