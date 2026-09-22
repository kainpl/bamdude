"""Tests for ``resolve_display_stem`` — Bambu Studio filename normalisation (#1152).

Bambu Studio's "Send to printer" dialog typically writes ``Plate_1.gcode.3mf``
(a sliced gcode payload wrapped in a 3MF container). ``Path(name).stem`` only
strips the last suffix and leaves ``Plate_1.gcode``, which then surfaces in
the archive UI as a confusing ``Plate_1.gcode`` rather than ``Plate_1`` and
defeats the substring-match used by the timelapse-fetch route to locate the
matching ``Plate_1.mp4`` on the printer's SD card.

Pin the canonicalisation rules so a future refactor can't silently regress
this path.
"""

import pytest

from backend.app.services.archive import archive_storage_stem, create_archive_directory, resolve_display_stem
from backend.app.utils.safe_path import safe_join_under


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        # The headline case: Bambu Studio's default name for a sliced 3MF.
        ("Plate_1.gcode.3mf", "Plate_1"),
        # User-renamed file with the double-suffix pattern.
        ("MyAwesomeBenchy.gcode.3mf", "MyAwesomeBenchy"),
        # Plain .3mf (already-clean export from Bambu Studio's Save As).
        ("Benchy.3mf", "Benchy"),
        # Standalone gcode upload — rare but supported.
        ("standalone.gcode", "standalone"),
        # Mixed-case suffix — many slicers / OSes preserve user-typed case.
        ("UPPERCASE.GCODE.3MF", "UPPERCASE"),
        ("mixed.GCode.3mf", "mixed"),
        # Names that contain dots in the middle should keep them.
        ("my.cool.model.gcode.3mf", "my.cool.model"),
        ("v1.2.3-prototype.3mf", "v1.2.3-prototype"),
        # No recognised suffix → fall through to Path.stem.
        ("Cura_export.zip", "Cura_export"),
        ("README.md", "README"),
        # Edge: just the suffix with nothing in front. Strip honestly — the
        # caller is responsible for sanity-checking empty stems.
        (".gcode.3mf", ""),
        (".3mf", ""),
        # Path components must not leak in. The helper takes a filename, but
        # callers occasionally pass a full path string.
        ("/some/dir/Plate_1.gcode.3mf", "Plate_1"),
        ("subdir/MyModel.3mf", "MyModel"),
    ],
)
def test_resolve_display_stem(filename: str, expected: str) -> None:
    assert resolve_display_stem(filename) == expected


@pytest.mark.parametrize(
    "filename",
    [
        "Корпус нічної камери -0.2.stl_1 + Корпус нічної камери -0.2.s....gcode.3mf",
        "Povorotka_GSC_Repiter v1.1.stl_8 + Povorotka_GSC_Repiter v1.1.stl_8 + Povorotka_GSC_Repiter v1.1.....gcode.3mf",
    ],
)
def test_storage_directory_removes_only_windows_normalized_suffixes(tmp_path, filename: str) -> None:
    """Exact farm names must not turn a legal child into a traversal veto."""
    display_stem = resolve_display_stem(filename)
    assert display_stem.endswith(".")
    archive_dir = create_archive_directory(tmp_path, display_stem, timestamp="20260922_200000")

    assert archive_storage_stem(display_stem) == display_stem.rstrip(" .")
    assert not archive_dir.name.endswith((".", " "))
    assert safe_join_under(archive_dir, filename, http=False).is_relative_to(archive_dir)
