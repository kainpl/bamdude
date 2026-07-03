"""FAT32/exFAT print-filename validation (upstream Bambuddy #1540).

Bambu Studio refuses these names client-side; BamDude rejects them at the
rename / upload / queue / print boundaries so the failure surfaces there
instead of at FTP-upload time (``553 Could not create file``).
"""

import pytest

from backend.app.utils.filename import (
    INVALID_FILENAME_CHARS,
    MAX_FILENAME_BYTES,
    InvalidFilenameError,
    validate_print_filename,
)


@pytest.mark.parametrize("name", ["model.3mf", "My Print v2.gcode.3mf", "part_01.stl", "a.3mf", "Модель.3mf"])
def test_accepts_legitimate_names(name):
    validate_print_filename(name)  # no raise


@pytest.mark.parametrize("char", list(INVALID_FILENAME_CHARS))
def test_rejects_each_illegal_char(char):
    with pytest.raises(InvalidFilenameError) as exc_info:
        validate_print_filename(f"L{char}R.3mf")
    assert exc_info.value.char == char


def test_rejects_the_exact_pipe_reproducer():
    """The #1540 reporter renamed a file to ``L|R.3mf``."""
    with pytest.raises(InvalidFilenameError) as exc_info:
        validate_print_filename("L|R.3mf")
    assert exc_info.value.char == "|"


@pytest.mark.parametrize("name", ["", "   ", "\t"])
def test_rejects_empty_or_whitespace(name):
    with pytest.raises(InvalidFilenameError, match="empty"):
        validate_print_filename(name)


@pytest.mark.parametrize("name", [".", ".."])
def test_rejects_dot_names(name):
    with pytest.raises(InvalidFilenameError):
        validate_print_filename(name)


def test_rejects_control_character():
    with pytest.raises(InvalidFilenameError):
        validate_print_filename("bad\x07name.3mf")


@pytest.mark.parametrize("name", ["trailing ", "trailing."])
def test_rejects_trailing_space_or_dot(name):
    with pytest.raises(InvalidFilenameError):
        validate_print_filename(name)


def test_byte_length_cap_is_bytes_not_codepoints():
    # 128 two-byte codepoints = 256 UTF-8 bytes > 255 cap, even though it's
    # only 128 characters — proves the check counts bytes.
    name = "é" * 128
    assert len(name) < MAX_FILENAME_BYTES
    assert len(name.encode("utf-8")) > MAX_FILENAME_BYTES
    with pytest.raises(InvalidFilenameError, match="bytes"):
        validate_print_filename(name)


def test_just_under_byte_cap_ok():
    validate_print_filename("a" * MAX_FILENAME_BYTES)
