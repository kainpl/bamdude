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

import re
import zipfile
from pathlib import Path

import pytest

from backend.app.services.library_helpers import (
    SLICED_GCODE_META_KEY,
    compute_file_tags,
    names_carry_sliced_gcode,
    sliced_by_content,
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


# -- the print gates' content arm (upstream #2993) ----------------------------
#
# ``sliced_by_content`` is what every print gate adds beside its own filename
# rule, so a sliced ``Foo.3mf`` the file manager offers a Print button for is not
# refused by the route behind that button. ``names_carry_sliced_gcode`` is the
# rule itself: the archive side and the library side must never answer it
# differently — upstream's card showed a GCODE badge for a file its library then
# filed as a source project.

_SLICED_NAMES = ["3D/3dmodel.model", "Metadata/plate_1.gcode", "Metadata/plate_1.gcode.md5"]
_SOURCE_NAMES = ["3D/3dmodel.model", "Metadata/model_settings.config"]


class TestThePrintGatesContentArm:
    def test_the_rule_itself(self):
        assert names_carry_sliced_gcode(_SLICED_NAMES) is True
        assert names_carry_sliced_gcode(_SOURCE_NAMES) is False
        # G-code outside Metadata/ is not what a slicer writes nor what the printer runs.
        assert names_carry_sliced_gcode(["plate_1.gcode"]) is False

    @pytest.mark.asyncio
    async def test_a_3mf_holding_gcode_is_sliced_whatever_its_name(self, tmp_path):
        assert await sliced_by_content("lamp.3mf", _make_3mf(tmp_path / "lamp.3mf", sliced=True)) is True

    @pytest.mark.asyncio
    async def test_a_3mf_without_gcode_is_not(self, tmp_path):
        assert await sliced_by_content("model.3mf", _make_3mf(tmp_path / "model.3mf", sliced=False)) is False

    @pytest.mark.asyncio
    async def test_a_stored_answer_is_used_and_the_file_is_not_opened(self, tmp_path):
        gone = tmp_path / "gone.3mf"
        assert await sliced_by_content("lamp.3mf", gone, file_metadata={SLICED_GCODE_META_KEY: True}) is True
        assert await sliced_by_content("lamp.3mf", gone, file_metadata={SLICED_GCODE_META_KEY: False}) is False

    @pytest.mark.asyncio
    async def test_unreadable_or_pathless_is_not_sliced(self, tmp_path):
        """No answer is not a yes: a gate refuses rather than send bytes it
        could not look at."""
        assert await sliced_by_content("lamp.3mf", tmp_path / "gone.3mf") is False
        assert await sliced_by_content("lamp.3mf", None) is False
        (tmp_path / "junk.3mf").write_bytes(b"not a zip")
        assert await sliced_by_content("junk.3mf", tmp_path / "junk.3mf") is False

    @pytest.mark.asyncio
    async def test_only_a_3mf_is_judged_by_content(self, tmp_path):
        """A mesh is never "sliced", whatever sits in it."""
        assert await sliced_by_content("part.stl", _make_3mf(tmp_path / "part.stl", sliced=True)) is False

    def test_the_rule_has_one_home(self):
        """Every "does this container hold sliced G-code" question goes through
        ``names_carry_sliced_gcode``. A second inline copy is how two screens
        came to disagree about the same file upstream."""
        backend = Path(__file__).resolve().parents[3] / "app"
        inline = re.compile(r'startswith\("Metadata/"\)\s+and\s+\w+\.endswith\("\.gcode"\)')
        offenders = [
            str(path.relative_to(backend))
            for path in backend.rglob("*.py")
            if path.name != "library_helpers.py" and inline.search(path.read_text(encoding="utf-8"))
        ]
        assert offenders == []
