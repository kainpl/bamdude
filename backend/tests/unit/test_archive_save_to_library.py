"""Copying an archived print's 3MF into the library.

A user asked for it, and the mechanism already existed: ``save_3mf_bytes_to_library``
is what MakerWorld import and slicer output both go through, so the metadata
parse, the thumbnail, the per-plate cache and the content-hash dedupe are not
reimplemented here. The route is thin on purpose — a second copy of that
pipeline is the thing worth avoiding.

⚠️ **An archive may legitimately have no file.** A print started from the
printer's own screen whose 3MF could not be pulled gets a row with an empty
``file_path`` and a retry marker. That is a 409 explaining itself, not a 404 and
not a crash: the archive is real, the file is what is missing.

⚠️ **The bytes on disk are the UNPATCHED original**, which is what belongs in a
library — the patched variant is built per dispatch and thrown away.

⚠️ **Read-only external folders are refused by the shared helper**, not by this
route. ``_resolve_upload_destination`` already answers 403 for them, so
repeating the check here would be a second copy of a rule that is already
enforced one layer down.
"""

from __future__ import annotations

import inspect

from backend.app.api.routes import archives as archive_routes
from backend.app.services.library_helpers import compute_file_tags


def _source() -> str:
    src = inspect.getsource(archive_routes.save_archive_to_library)
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


class TestItReusesTheOnePipeline:
    def test_it_calls_the_shared_helper(self) -> None:
        """Not its own parse. Everything that makes a library row — metadata,
        thumbnail, plates, dedupe — lives in that helper."""
        assert "save_3mf_bytes_to_library" in _source()

    def test_it_does_not_reimplement_the_parse(self) -> None:
        src = _source()

        assert "ThreeMFParser" not in src
        assert "parse_plates_from_3mf" not in src

    def test_it_records_where_the_file_came_from(self) -> None:
        assert 'source_type="archive"' in _source()


class TestWhatItRefuses:
    def test_an_archive_with_no_file_is_a_409_not_a_404(self) -> None:
        """⚠️ The archive exists; the file does not. A 404 would say the wrong
        thing about which of the two is missing."""
        src = _source()

        assert "if not archive.file_path:" in src
        assert "status_code=409" in src

    def test_a_file_missing_from_disk_is_a_404(self) -> None:
        src = _source()

        assert "file_path.is_file()" in src
        assert 'HTTPException(404, "The archive\'s file is no longer on disk")' in src

    def test_an_unknown_folder_is_refused_before_anything_is_written(self) -> None:
        src = _source()

        assert src.index('HTTPException(404, "Folder not found")') < src.index("save_3mf_bytes_to_library(")

    def test_it_does_not_repeat_the_read_only_check(self) -> None:
        """⚠️ Deliberate. ``_resolve_upload_destination`` inside the helper
        already answers 403 for a read-only external folder; a copy here would
        be a second place to keep that rule true."""
        assert "external_readonly" not in _source()


class TestItRequiresBothPermissions:
    def test_reading_the_archive_and_writing_to_the_library(self) -> None:
        """Two different things are being touched, so both are asked for."""
        src = inspect.getsource(archive_routes.save_archive_to_library)

        assert "require_ownership_permission" in src
        assert "Permission.LIBRARY_UPLOAD" in src


class TestTheReadinessTag:
    def test_a_file_from_an_archive_reads_as_sliced(self) -> None:
        """⚠️ It was printed, so it is sliced by definition. Without covering
        ``archive`` in that branch the row gets NO readiness tag at all:
        ``detect_file_type`` collapses ``.gcode.3mf`` to ``gcode``, and the
        ``project`` branch only catches a bare ``3mf``."""
        tags = compute_file_tags(
            filename="print.gcode.3mf",
            file_type="gcode",
            file_metadata={},
            source_type="archive",
            swap_compatible=False,
        )

        assert "sliced" in tags

    def test_it_matches_what_the_slicer_produces(self) -> None:
        """Same bytes, same badges, whichever door they came in through."""
        kwargs = {"filename": "print.gcode.3mf", "file_type": "gcode", "file_metadata": {}, "swap_compatible": False}

        assert compute_file_tags(source_type="archive", **kwargs) == compute_file_tags(source_type="sliced", **kwargs)

    def test_an_unknown_source_still_gets_no_provenance_tag(self) -> None:
        """The new value must not have widened anything else — only the
        readiness branch was meant to learn about it."""
        tags = compute_file_tags(
            filename="print.gcode.3mf",
            file_type="gcode",
            file_metadata={},
            source_type="archive",
            swap_compatible=False,
        )

        assert "makerworld" not in tags
