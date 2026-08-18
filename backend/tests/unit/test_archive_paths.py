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

from backend.app.core.config import settings
from backend.app.utils.archive_paths import archive_dir_for, photos_dir_for


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

    def test_it_falls_back_to_the_per_id_folder(self):
        """The shape the finish-photo writer already used — chosen so existing
        captures stay findable."""
        assert archive_dir_for(_archive("", archive_id=7)) == settings.archive_dir / "7"

    def test_none_is_treated_the_same_as_empty(self):
        assert archive_dir_for(_archive(None, archive_id=7)) == settings.archive_dir / "7"

    def test_two_archives_do_not_share_a_folder(self):
        assert archive_dir_for(_archive("", 1)) != archive_dir_for(_archive("", 2))


class TestEveryoneAgrees:
    def test_the_writer_and_the_reader_resolve_alike(self):
        """The point of the helper: one answer, so a photo written by the
        background capture is found by the endpoint that serves it."""
        for path in ("archive/x/y.gcode.3mf", "", None):
            archive = _archive(path)
            assert photos_dir_for(archive) == archive_dir_for(archive) / "photos"
