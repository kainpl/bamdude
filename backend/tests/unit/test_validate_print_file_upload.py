"""Regression tests for H.1 / upstream Bambuddy #1401.

``validate_print_file_upload`` rejects obvious slicer-output mistakes at
upload time so they don't cascade into the confusing 30-seconds-later
"printer was unable to parse the 3mf file" firmware error.

Two reject paths:

* Raw ``.gcode`` (not ``.gcode.3mf``) — Bambu printers in network mode
  only parse zip containers. The background dispatcher appends ``.3mf``
  to a raw-gcode filename when constructing the FTP destination, which
  is how the printer ends up with a file named ``.gcode.3mf`` whose
  body is raw gcode.
* ``.3mf`` / ``.gcode.3mf`` whose body doesn't start with the ZIP
  magic ``PK\\x03\\x04`` — either a corrupt file or a raw gcode renamed.

The pre-flight runs on every upload route (library, archive source-3MF,
source-3MF-by-name) so any of these entry points stops the bad file
before it gets persisted.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.app.api.routes.library import validate_print_file_upload

# Minimal valid ZIP container — empty zip starts with PK\x03\x04 too.
_ZIP_MAGIC = b"PK\x03\x04"
_VALID_3MF = _ZIP_MAGIC + b"\x00" * 100


class TestRawGcodeRejection:
    def test_plain_gcode_extension_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_print_file_upload("part.gcode", b"G28\nG1 X10\n")
        assert exc.value.status_code == 400
        assert ".gcode" in exc.value.detail.lower()

    def test_uppercase_extension_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_print_file_upload("PART.GCODE", b"G28\n")
        assert exc.value.status_code == 400

    def test_gcode_3mf_double_extension_accepted_when_valid_zip(self):
        """``.gcode.3mf`` is the correct shape — must pass."""
        validate_print_file_upload("part.gcode.3mf", _VALID_3MF)


class TestZipMagicValidation:
    def test_3mf_without_zip_magic_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_print_file_upload("model.3mf", b"not a zip header at all")
        assert exc.value.status_code == 400
        assert "zip" in exc.value.detail.lower()

    def test_3mf_with_zip_magic_accepted(self):
        validate_print_file_upload("model.3mf", _VALID_3MF)

    def test_gcode_3mf_without_zip_magic_rejected(self):
        """Reporter's exact scenario: raw gcode renamed to ``.gcode.3mf``."""
        with pytest.raises(HTTPException) as exc:
            validate_print_file_upload("part.gcode.3mf", b";HEADER\nG28\n")
        assert exc.value.status_code == 400


class TestIrrelevantUploadsPass:
    """STLs, images, etc. — validator is a no-op for them."""

    def test_stl_accepted(self):
        # STL doesn't need a zip header; the validator should ignore it.
        validate_print_file_upload("model.stl", b"solid bambu\n")

    def test_png_accepted(self):
        validate_print_file_upload("snapshot.png", b"\x89PNG\r\n\x1a\n")

    def test_empty_filename_no_extension_passes(self):
        # Defensive: edge case where the upload has no extension. Validator
        # should return None silently — downstream code handles the
        # missing-extension case.
        validate_print_file_upload("no-extension", b"anything")
