"""Whether a 3MF is sliced is decided by looking inside it, not by its name.

`compute_file_tags` derived the ``gcode`` tag from ``file_type``, which is
``detect_file_type(filename)`` — so the tag answered *"is it NAMED like a
sliced file"*. That tag gates every "can this be printed" affordance in the web
UI and, from this batch, in the Telegram bot.

A Bambu slicer writes ``Metadata/plate_N.gcode`` into the container. A project
or model export does not, whatever the file is called.

⚠️ **Absent means unknown, not false.** Three migrations (m036, m037, m041)
call the helper from stored metadata and never open a file, and every row
written before the key existed has no answer either. Both must keep the old
filename rule rather than be told their files are unsliced.
"""

from __future__ import annotations

import zipfile

import pytest

from backend.app.services.library_helpers import (
    SLICED_GCODE_META_KEY,
    compute_file_tags,
    sliced_gcode_in_3mf,
)


def _tags(filename: str, file_type: str, meta: dict | None = None, source_type: str | None = None) -> set[str]:
    return set(
        compute_file_tags(
            filename=filename,
            file_type=file_type,
            file_metadata=meta,
            source_type=source_type,
            swap_compatible=False,
        )
    )


def _make_3mf(path, *, sliced: bool):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Metadata/slice_info.config", "<config/>")
        if sliced:
            zf.writestr("Metadata/plate_1.gcode", "G1 X0\n")
        else:
            zf.writestr("Metadata/plate_1.png", b"not really a png")
    return path


class TestTheContentCheck:
    def test_a_sliced_container_is_recognised(self, tmp_path):
        assert sliced_gcode_in_3mf(_make_3mf(tmp_path / "a.3mf", sliced=True)) is True

    def test_a_project_container_is_recognised(self, tmp_path):
        assert sliced_gcode_in_3mf(_make_3mf(tmp_path / "b.3mf", sliced=False)) is False

    def test_an_unreadable_file_answers_none_not_false(self, tmp_path):
        """⚠️ False would be a claim. None is the absence of one, and the
        callers branch on exactly that difference."""
        broken = tmp_path / "c.3mf"
        broken.write_bytes(b"not a zip at all")

        assert sliced_gcode_in_3mf(broken) is None
        assert sliced_gcode_in_3mf(tmp_path / "missing.3mf") is None


class TestTheTagFollowsTheContent:
    def test_a_file_named_sliced_but_empty_inside_is_not_tagged_gcode(self):
        """The case the printer would otherwise answer thirty seconds later
        with "unable to parse the 3mf file"."""
        tags = _tags("model.gcode.3mf", "gcode", {SLICED_GCODE_META_KEY: False})

        assert "gcode" not in tags
        assert {"3mf", "project"} <= tags

    def test_a_plain_named_3mf_that_is_sliced_is_tagged_gcode(self):
        tags = _tags("model.3mf", "3mf", {SLICED_GCODE_META_KEY: True})

        assert {"gcode", "3mf"} <= tags
        assert "project" not in tags

    def test_format_and_readiness_never_contradict(self):
        """⚠️ They are resolved from one value on purpose. Derived separately,
        a file could carry ``gcode`` and ``project`` at once — nonsense that
        stays invisible until something filters on one of them."""
        for named, ftype, sliced in (
            ("model.gcode.3mf", "gcode", False),
            ("model.3mf", "3mf", True),
            ("model.gcode.3mf", "gcode", True),
            ("model.3mf", "3mf", False),
        ):
            tags = _tags(named, ftype, {SLICED_GCODE_META_KEY: sliced})
            assert not ("gcode" in tags and "project" in tags), f"{named} -> {sorted(tags)}"


class TestTheFallback:
    @pytest.mark.parametrize(
        ("filename", "file_type", "expected"),
        [
            ("model.gcode.3mf", "gcode", {"gcode", "3mf"}),
            ("model.3mf", "3mf", {"3mf", "project"}),
            ("part.stl", "stl", {"stl", "geometry"}),
            ("raw.gcode", "gcode", {"gcode"}),
        ],
    )
    def test_without_the_key_the_filename_rule_still_applies(self, filename, file_type, expected):
        """What the three backfill migrations depend on: they read stored
        metadata and cannot open files."""
        assert expected <= _tags(filename, file_type, None)

    def test_a_raw_gcode_is_never_re_judged(self):
        """The key is written by the 3MF parse. A raw ``.gcode`` cannot carry
        it, and must not be turned into a project if something ever does."""
        tags = _tags("raw.gcode", "gcode", {SLICED_GCODE_META_KEY: True})

        assert "gcode" in tags
        assert "3mf" not in tags

    def test_an_archive_saved_file_is_still_sliced(self):
        """``source_type`` wins on readiness — it was printed, by definition."""
        tags = _tags("out.gcode.3mf", "gcode", {SLICED_GCODE_META_KEY: True}, source_type="archive")

        assert "sliced" in tags
        assert "project" not in tags
