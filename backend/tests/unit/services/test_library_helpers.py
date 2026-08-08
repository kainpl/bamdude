"""skip_objects_supported must agree with the live 3MF gate, by construction.

The stored flags and the live read are two paths to one answer; if they ever
disagree the list badge contradicts what the preview says a line below it.
"""

import io
import json
import zipfile
from datetime import datetime
from types import SimpleNamespace

import pytest

from backend.app.services.archive import extract_skip_support_from_3mf
from backend.app.services.library_helpers import folder_activity_at, skip_objects_supported_from_metadata


@pytest.mark.parametrize(
    "meta,expected",
    [
        ({"gcode_label_objects": True, "exclude_object": True}, True),
        ({"gcode_label_objects": True, "exclude_object": False}, False),
        ({"gcode_label_objects": False, "exclude_object": True}, False),
        ({"gcode_label_objects": False, "exclude_object": False}, False),
        # exclude_object absent — the parser omits it when the 3MF has no
        # interpretable value, and "unknown" must not read as "allowed".
        ({"gcode_label_objects": True}, False),
        ({}, False),
        (None, False),
    ],
)
def test_truth_table(meta, expected):
    assert skip_objects_supported_from_metadata(meta) is expected


@pytest.mark.parametrize(
    "glo,eo,expected",
    [(True, True, True), (True, False, False), (False, True, False), (False, False, False)],
)
def test_agrees_with_live_3mf_gate(glo, eo, expected):
    """Same 3MF, both paths, same answer.

    The live gate reads project_settings.config; the helper reads what the
    parser stored from it. Any divergence puts a badge in the list that the
    preview's own banner contradicts a line below.
    """
    settings_json = {"gcode_label_objects": glo, "exclude_object": eo}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/project_settings.config", json.dumps(settings_json))

    assert extract_skip_support_from_3mf(buf.getvalue()) is expected
    # What _extract_print_settings writes for this input: both keys present and
    # already coerced, so the stored dict is the settings dict.
    assert skip_objects_supported_from_metadata(settings_json) is expected


def test_schemas_expose_skip_objects_supported():
    """Every list/detail surface the badge renders on must carry the field.

    ``FileResponse`` is what ``routes/library.py`` imports as
    ``FileResponseSchema`` — the alias only exists to avoid colliding with
    Starlette's class of that name.
    """
    from backend.app.schemas.archive import ArchiveResponse
    from backend.app.schemas.library import FileListResponse, FileResponse

    for model in (FileListResponse, FileResponse, ArchiveResponse):
        assert "skip_objects_supported" in model.model_fields
        assert model.model_fields["skip_objects_supported"].default is False


class TestFolderActivityAt:
    """``folder_activity_at`` — the folder-sort key (#1770, #2680).

    Replaced seven copies of the same expression in ``routes/library.py``. The
    six that were not the folder tree would otherwise have kept ignoring the
    on-disk mtime, which is the shape of half-fix this cycle has paid for twice
    already (G7 status blocks, G9 sanitizers, H4b Spoolman guard).
    """

    @staticmethod
    def _folder(updated_at, fs_modified_at=None):
        return SimpleNamespace(updated_at=updated_at, fs_modified_at=fs_modified_at)

    def test_folder_with_no_files_reports_its_own_stamp(self):
        stamp = datetime(2026, 1, 1, 12, 0)
        assert folder_activity_at(self._folder(stamp)) == stamp

    def test_a_newer_file_bubbles_up(self):
        older = datetime(2026, 1, 1, 12, 0)
        newer = datetime(2026, 6, 1, 12, 0)
        assert folder_activity_at(self._folder(older), newer) == newer

    def test_an_older_file_does_not_pull_the_folder_back(self):
        newer = datetime(2026, 6, 1, 12, 0)
        older = datetime(2026, 1, 1, 12, 0)
        assert folder_activity_at(self._folder(newer), older) == newer

    def test_the_on_disk_mtime_wins_even_when_it_is_older(self):
        """The point of m129, and the case a naive ``max()`` would get wrong.

        An external scan writes ``updated_at`` = now onto every row it touches.
        Taking the newest of the two columns would therefore always return the
        scan instant and reproduce exactly the tie the column exists to break —
        so ``fs_modified_at`` is a *replacement* for ``updated_at``, not a
        candidate against it.
        """
        scanned_now = datetime(2026, 8, 8, 12, 0)
        real_mtime = datetime(2020, 9, 13, 12, 26)
        assert folder_activity_at(self._folder(scanned_now, real_mtime)) == real_mtime
