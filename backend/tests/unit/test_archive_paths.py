"""An archive with no 3MF still has a folder, and everyone must agree which.

Ported from upstream `0623cc46` (#1820). ``Path("").parent`` is ``Path(".")``,
so deriving an archive's folder from an empty ``file_path`` resolves to the
**data directory itself** — photos landed in ``<DATA_DIR>/photos``.

⚠️ An empty ``file_path`` is normal here, not corruption: the archive row is
created at print start, before the 3MF is fetched, and stays empty when there is
nothing to fetch — a job started from the printer's own internal library never
yields one.

The fault here was not just the wrong folder but **two answers**: the
finish-photo background writer had grown a fallback of its own, while the three
photo endpoints had not. A photo captured automatically and a photo uploaded by
hand went to different places, and each endpoint read the wrong one.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.core.config import settings
from backend.app.utils.archive_paths import archive_dir_for, find_photo, photos_dir_for
from backend.app.utils.safe_path import PathTraversalError


def _archive(file_path, archive_id=42):
    return SimpleNamespace(id=archive_id, file_path=file_path)


class TestAnArchiveWithAFile:
    def test_the_folder_is_the_one_holding_the_3mf(self):
        archive = _archive("archive/20260818_120000_job/job.gcode.3mf")

        assert archive_dir_for(archive) == settings.base_dir / Path("archive/20260818_120000_job")

    def test_photos_sit_under_it(self):
        archive = _archive("archive/20260818_120000_job/job.gcode.3mf")

        assert photos_dir_for(archive) == archive_dir_for(archive) / "photos"


class TestAnArchiveWithoutOne:
    def test_it_never_resolves_to_the_data_directory(self):
        """The bug, stated directly: an empty path must not put files in the
        root of DATA_DIR."""
        assert archive_dir_for(_archive("")) != settings.base_dir
        assert photos_dir_for(_archive("")) != settings.base_dir / "photos"

    def test_it_falls_back_to_a_folder_of_its_own_under_no_source(self):
        """Not ``archive/<id>``: printer folders are ``archive/<printer id>/``,
        so archive 3's files landed in printer 3's folder, and deleting them
        with the row was not safe (audit 1.2.5.6, D14). ``no_source/<id>`` is
        where a source 3MF for such an archive already goes."""
        assert archive_dir_for(_archive("", archive_id=7)) == settings.archive_dir / "no_source" / "7"

    def test_none_is_treated_the_same_as_empty(self):
        assert archive_dir_for(_archive(None, archive_id=7)) == settings.archive_dir / "no_source" / "7"

    def test_two_archives_do_not_share_a_folder(self):
        assert archive_dir_for(_archive("", 1)) != archive_dir_for(_archive("", 2))


class TestAPhotoIsFoundWhereverItWasWritten:
    """A photo's folder is not fixed for the life of an archive: one captured
    before the 3MF arrived sits in the fallback folder, and the moment the 3MF
    is attached ``photos_dir_for`` answers the 3MF's folder instead. Earlier
    fallbacks (``archive/<id>/`` until 2026-09-24) hold photos too. Only the
    name is stored, so reading looks in every place a photo of THIS archive can
    have been written; writing always goes to the current one."""

    @pytest.fixture
    def data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "base_dir", tmp_path)
        monkeypatch.setattr(settings, "archive_dir", tmp_path / "archive")
        return tmp_path

    def _write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpg")
        return path

    def test_the_current_folder_answers_first(self, data_dir):
        archive = _archive("archive/3/20260924_job/job.gcode.3mf", archive_id=7)
        current = self._write(data_dir / "archive/3/20260924_job/photos/a.jpg")
        self._write(data_dir / "archive/no_source/7/photos/a.jpg")

        assert find_photo(archive, "a.jpg") == current

    def test_a_photo_taken_before_the_3mf_arrived_is_still_found(self, data_dir):
        archive = _archive("archive/3/20260924_job/job.gcode.3mf", archive_id=7)
        early = self._write(data_dir / "archive/no_source/7/photos/a.jpg")

        assert find_photo(archive, "a.jpg") == early

    def test_a_photo_from_the_old_per_id_folder_is_still_found(self, data_dir):
        legacy = self._write(data_dir / "archive/7/photos/a.jpg")

        assert find_photo(_archive("", archive_id=7), "a.jpg") == legacy

    def test_a_photo_nobody_wrote_is_none(self, data_dir):
        assert find_photo(_archive("", archive_id=7), "a.jpg") is None

    def test_a_traversal_name_is_refused(self, data_dir):
        with pytest.raises(PathTraversalError):
            find_photo(_archive("", archive_id=7), "../../evil.jpg")


class TestEveryoneAgrees:
    def test_the_writer_and_the_reader_resolve_alike(self):
        """The point of the helper: one answer, so a photo written by the
        background capture is found by the endpoint that serves it."""
        for path in ("archive/x/y.gcode.3mf", "", None):
            archive = _archive(path)
            assert photos_dir_for(archive) == archive_dir_for(archive) / "photos"
