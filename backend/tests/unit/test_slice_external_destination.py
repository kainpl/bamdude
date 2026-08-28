"""A slice of a file on an external mount is written to that mount.

Ported from upstream #2810. ``slice_and_persist`` always wrote to the managed
library directory while giving the new row the *source folder's* id. The file
therefore appeared in the right folder in the UI and never arrived on the share
— which is the one place the user was looking, and the reason it could not be
reproduced from the web UI at all.

⚠️ Unlike an upload, a failure here does NOT raise. The bytes exist and cost
minutes of CPU to produce, so an unwritable target falls back to managed storage
**with a reason attached** rather than discarding the slice. Silent fallback is
what made the original bug invisible.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.api.routes.library import _resolve_slice_destination, _unique_external_name


def _folder(tmp_path: Path, **over) -> SimpleNamespace:
    values = {
        "is_external": True,
        "external_readonly": False,
        "external_path": str(tmp_path),
    }
    values.update(over)
    return SimpleNamespace(**values)


class TestTheNormalPaths:
    def test_a_managed_folder_gets_a_uuid_in_the_library_dir(self):
        path, is_external, reason = _resolve_slice_destination(None, "Benchy.gcode.3mf")

        assert is_external is False
        assert reason is None
        assert path.name.endswith(".gcode.3mf")

    def test_a_non_external_folder_is_the_same(self, tmp_path):
        folder = _folder(tmp_path, is_external=False)

        _path, is_external, reason = _resolve_slice_destination(folder, "Benchy.gcode.3mf")

        assert is_external is False
        assert reason is None

    def test_a_writable_external_folder_receives_the_file(self, tmp_path):
        """⚠️ The whole bug: this used to return the managed directory."""
        path, is_external, reason = _resolve_slice_destination(_folder(tmp_path), "Benchy.gcode.3mf")

        assert is_external is True
        assert reason is None
        assert path.parent == tmp_path
        assert path.name == "Benchy.gcode.3mf"


class TestWhenTheMountCannotTakeIt:
    def test_a_read_only_folder_falls_back_and_says_so(self, tmp_path):
        path, is_external, reason = _resolve_slice_destination(
            _folder(tmp_path, external_readonly=True), "Benchy.gcode.3mf"
        )

        assert is_external is False
        assert reason == "external_readonly"
        assert path.parent != tmp_path

    def test_a_folder_with_no_path_configured(self, tmp_path):
        _path, is_external, reason = _resolve_slice_destination(_folder(tmp_path, external_path=""), "x.gcode.3mf")

        assert is_external is False
        assert reason == "external_no_path"

    def test_an_unreachable_mount(self, tmp_path):
        missing = _folder(tmp_path, external_path=str(tmp_path / "not-mounted"))

        _path, is_external, reason = _resolve_slice_destination(missing, "x.gcode.3mf")

        assert is_external is False
        assert reason == "external_unreachable"

    def test_a_path_that_is_a_file_not_a_directory(self, tmp_path):
        target = tmp_path / "a-file"
        target.write_text("not a directory")

        _path, is_external, reason = _resolve_slice_destination(_folder(tmp_path, external_path=str(target)), "x.3mf")

        assert is_external is False
        assert reason == "external_unreachable"

    @pytest.mark.skipif(os.name == "nt", reason="chmod read-only is not enforced for the owner on Windows")
    def test_a_directory_we_cannot_write_to(self, tmp_path):
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            _path, is_external, reason = _resolve_slice_destination(
                _folder(tmp_path, external_path=str(locked)), "x.gcode.3mf"
            )

            assert is_external is False
            assert reason == "external_not_writable"
        finally:
            locked.chmod(0o700)

    def test_a_name_that_would_escape_the_mount(self, tmp_path):
        """⚠️ Defensive — the name comes from a 3MF on disk — but a name that
        escapes must land in managed storage, never outside it."""
        _path, is_external, reason = _resolve_slice_destination(_folder(tmp_path), "../../escape.gcode.3mf")

        assert is_external is False
        assert reason == "external_invalid_name"

    def test_every_fallback_still_returns_a_usable_path(self, tmp_path):
        """The slice must be stored somewhere; the reason is a report, not a
        refusal."""
        for folder in (
            _folder(tmp_path, external_readonly=True),
            _folder(tmp_path, external_path=""),
            _folder(tmp_path, external_path=str(tmp_path / "gone")),
        ):
            path, _is_external, reason = _resolve_slice_destination(folder, "x.gcode.3mf")
            assert reason is not None
            assert path.name.endswith(".gcode.3mf")


class TestCollisionsOnTheShare:
    def test_a_free_name_is_used_as_is(self, tmp_path):
        assert _unique_external_name(tmp_path, "Bidoof.gcode.3mf") == "Bidoof.gcode.3mf"

    def test_a_taken_name_is_suffixed(self, tmp_path):
        (tmp_path / "Bidoof.gcode.3mf").write_text("first")

        assert _unique_external_name(tmp_path, "Bidoof.gcode.3mf") == "Bidoof (2).gcode.3mf"

    def test_it_splits_on_the_compound_extension(self, tmp_path):
        """⚠️ Not "Bidoof.gcode (2).3mf" — the double extension is one suffix."""
        (tmp_path / "Bidoof.gcode.3mf").write_text("first")
        (tmp_path / "Bidoof (2).gcode.3mf").write_text("second")

        assert _unique_external_name(tmp_path, "Bidoof.gcode.3mf") == "Bidoof (3).gcode.3mf"

    def test_it_does_not_overwrite(self, tmp_path):
        """⚠️ The target is somebody's NAS, and the file being replaced may not
        even be ours. Re-slicing is routine, so a 409 would throw away minutes
        of CPU — uniquifying is the only answer that loses nothing."""
        original = tmp_path / "Bidoof.gcode.3mf"
        original.write_text("do not touch")

        chosen = _unique_external_name(tmp_path, "Bidoof.gcode.3mf")

        assert chosen != "Bidoof.gcode.3mf"
        assert original.read_text() == "do not touch"

    def test_the_search_is_bounded(self, tmp_path):
        """⚠️ An unbounded loop would hang the request on a mount that lies
        about exists() — some SMB shares do under contention."""
        import unittest.mock

        with unittest.mock.patch.object(Path, "exists", return_value=True):
            name = _unique_external_name(tmp_path, "Bidoof.gcode.3mf")

        assert name == "Bidoof (999).gcode.3mf"

    def test_a_single_extension_still_works(self, tmp_path):
        (tmp_path / "model.3mf").write_text("first")

        assert _unique_external_name(tmp_path, "model.3mf") == "model (2).3mf"
