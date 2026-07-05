"""Tests for the Auto-Print G-code injector (#422 / B.17 / A.17 / #1516)."""

import hashlib
import zipfile
from pathlib import Path

from backend.app.services.gcode_injector import (
    _inject_end_before_marker,
    _inject_start_at_marker,
    _parse_3mf_gcode_header,
    _substitute_placeholders,
    inject_gcode_into_3mf,
)

_SAMPLE_PLATE_GCODE = """; HEADER_BLOCK_START
; total layer number: 80
; max_z_height: 16.00
; total filament length [mm] : 12155.34
; HEADER_BLOCK_END
M104 S220 ; nozzle preheat
G28 ; home
G29 ; bed mesh
M109 S220 ; wait for nozzle
; MACHINE_START_GCODE_END
G1 Z0.2 F600
G1 X10 Y10 F3000
; user print starts
"""


def _build_3mf(path: Path, plate_gcode: str, plate_name: str = "Metadata/plate_1.gcode") -> None:
    """Pack a minimal 3MF with a single plate gcode for testing."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(plate_name, plate_gcode)


class TestParseHeader:
    def test_parses_simple_header(self):
        header = _parse_3mf_gcode_header(_SAMPLE_PLATE_GCODE)
        assert header["total_layer_number"] == "80"
        assert header["max_z_height"] == "16.00"
        # Units suffix should be stripped from the key
        assert header["total_filament_length"] == "12155.34"

    def test_returns_empty_when_no_header_block(self):
        assert _parse_3mf_gcode_header("M104 S220\nG28\n") == {}

    def test_stops_at_header_end(self):
        gcode = "; HEADER_BLOCK_START\n; foo: 1\n; HEADER_BLOCK_END\n; bar: 2\n"
        header = _parse_3mf_gcode_header(gcode)
        assert header == {"foo": "1"}


class TestSubstitutePlaceholders:
    def test_basic_substitution(self):
        out = _substitute_placeholders("M141 Z{max_z_height}", {"max_z_height": "16.00"})
        assert out == "M141 Z16.00"

    def test_prusa_alias_resolves_to_bambu_key(self):
        # Snippet copy-pasted from PrusaSlicer uses {max_layer_z} — should
        # resolve via alias to Bambu's max_z_height (A.17 placeholder fix).
        out = _substitute_placeholders("Z{max_layer_z}", {"max_z_height": "16.00"})
        assert out == "Z16.00"

    def test_unknown_placeholder_left_intact(self):
        out = _substitute_placeholders("Z{unknown_key}", {"max_z_height": "16.00"})
        assert out == "Z{unknown_key}"


class TestInjectAtMarker:
    def test_inserts_before_marker(self):
        body = "; foo\n; MACHINE_START_GCODE_END\nG1 X0\n"
        out = _inject_start_at_marker(body, "; SNIPPET\nG28\n")
        # Snippet should land on its own lines before the marker
        assert "; SNIPPET\nG28\n; MACHINE_START_GCODE_END" in out
        # And the marker comment is still there
        assert out.count("; MACHINE_START_GCODE_END") == 1

    def test_falls_back_to_prepend_when_marker_missing(self):
        body = "M104 S220\nG28\n"
        out = _inject_start_at_marker(body, "; INJECTED\n")
        assert out.startswith("; INJECTED\n")


class TestInjectEndBeforeMarker:
    """#1516: end snippets must land INSIDE the executable block (before
    ``; EXECUTABLE_BLOCK_END``) — Bambu firmware (P1S) ignores g-code after it."""

    def test_inserts_before_block_end_marker(self):
        body = "G1 X0\nM104 S0 ; machine-end\n; EXECUTABLE_BLOCK_END\n"
        out = _inject_end_before_marker(body, "; EJECT\n")
        # Snippet sits before the marker, after the printer's own machine-end.
        assert out.index("; EJECT") < out.index("; EXECUTABLE_BLOCK_END")
        assert out.index("M104 S0 ; machine-end") < out.index("; EJECT")
        # Nothing executable remains after the marker.
        assert out[out.index("; EXECUTABLE_BLOCK_END") :].strip() == "; EXECUTABLE_BLOCK_END"

    def test_falls_back_to_eof_when_marker_missing(self):
        body = "G1 X0\nM104 S0\n"
        out = _inject_end_before_marker(body, "; BYE\n")
        assert out.rstrip().endswith("; BYE")


class TestInjectInto3MF:
    def test_no_op_when_both_snippets_empty(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, _SAMPLE_PLATE_GCODE)
        assert inject_gcode_into_3mf(src, plate_id=1, start_gcode=None, end_gcode=None) is None

    def test_returns_none_when_no_gcode_in_3mf(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
        assert inject_gcode_into_3mf(src, plate_id=1, start_gcode="G28", end_gcode=None) is None

    def test_injects_start_snippet_before_marker(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, _SAMPLE_PLATE_GCODE)
        out_path = inject_gcode_into_3mf(src, plate_id=1, start_gcode="; HELLO\n", end_gcode=None)
        assert out_path is not None and out_path.exists()
        try:
            with zipfile.ZipFile(out_path) as zf:
                content = zf.read("Metadata/plate_1.gcode").decode()
            assert "; HELLO\n; MACHINE_START_GCODE_END" in content
        finally:
            out_path.unlink(missing_ok=True)

    def test_injects_end_snippet_at_eof(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, _SAMPLE_PLATE_GCODE)
        out_path = inject_gcode_into_3mf(src, plate_id=1, start_gcode=None, end_gcode="; BYE\n")
        assert out_path is not None
        try:
            with zipfile.ZipFile(out_path) as zf:
                content = zf.read("Metadata/plate_1.gcode").decode()
            assert content.rstrip().endswith("; BYE")
        finally:
            out_path.unlink(missing_ok=True)

    def test_resolves_placeholders_from_header(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, _SAMPLE_PLATE_GCODE)
        out_path = inject_gcode_into_3mf(src, plate_id=1, start_gcode="G1 Z{max_layer_z}", end_gcode=None)
        assert out_path is not None
        try:
            with zipfile.ZipFile(out_path) as zf:
                content = zf.read("Metadata/plate_1.gcode").decode()
            # {max_layer_z} → resolved via Prusa→Bambu alias to 16.00
            assert "G1 Z16.00" in content
            # Original literal must NOT survive — that was the A.17 head-crash bug
            assert "{max_layer_z}" not in content
        finally:
            out_path.unlink(missing_ok=True)

    def test_falls_back_to_first_gcode_when_plate_missing(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, _SAMPLE_PLATE_GCODE, plate_name="Metadata/plate_5.gcode")
        # Asking for plate 1 — the only file is plate_5.gcode, so the
        # fallback must inject into it rather than silently no-op.
        out_path = inject_gcode_into_3mf(src, plate_id=1, start_gcode="; X\n", end_gcode=None)
        assert out_path is not None
        try:
            with zipfile.ZipFile(out_path) as zf:
                names = zf.namelist()
            assert "Metadata/plate_5.gcode" in names
        finally:
            out_path.unlink(missing_ok=True)

    def test_end_lands_before_executable_block_end(self, tmp_path: Path):
        """#1516: with the marker present, end snippet lands inside the block."""
        gcode = (
            "; HEADER_BLOCK_START\n; max_z_height: 16.00\n; HEADER_BLOCK_END\n"
            "; MACHINE_START_GCODE_END\nG1 X10 Y10\nM104 S0 ; machine-end\n; EXECUTABLE_BLOCK_END\n"
        )
        src = tmp_path / "src.3mf"
        _build_3mf(src, gcode)
        out_path = inject_gcode_into_3mf(src, plate_id=1, start_gcode=None, end_gcode="; EJECT-SWEEP")
        assert out_path is not None
        try:
            with zipfile.ZipFile(out_path) as zf:
                content = zf.read("Metadata/plate_1.gcode").decode()
            assert content.index("; EJECT-SWEEP") < content.index("; EXECUTABLE_BLOCK_END")
            assert content.index("M104 S0 ; machine-end") < content.index("; EJECT-SWEEP")
            assert content[content.index("; EXECUTABLE_BLOCK_END") :].strip() == "; EXECUTABLE_BLOCK_END"
        finally:
            out_path.unlink(missing_ok=True)


def _build_3mf_with_md5(path: Path, plate_gcode: str, plate_name: str = "Metadata/plate_1.gcode") -> None:
    """A 3MF carrying a deliberately-wrong md5 sidecar, like a real sliced 3MF."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr(plate_name, plate_gcode)
        zf.writestr(f"{plate_name}.md5", "STALEHASHVALUE")


class TestMd5SidecarRecompute:
    """#1516: the plate ``.gcode.md5`` sidecar must match the injected gcode
    (P1S rejects a stale hash with HMS 0500-4003)."""

    def test_md5_recomputed_to_match_injected_gcode(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf_with_md5(src, "G28\nM400\n")
        out_path = inject_gcode_into_3mf(src, plate_id=1, start_gcode=None, end_gcode="M104 S0")
        assert out_path is not None
        try:
            with zipfile.ZipFile(out_path) as zf:
                gcode = zf.read("Metadata/plate_1.gcode")
                sidecar = zf.read("Metadata/plate_1.gcode.md5")
            expected = hashlib.md5(gcode, usedforsecurity=False).hexdigest().upper().encode("ascii")
            assert sidecar == expected
            assert sidecar != b"STALEHASHVALUE"
        finally:
            out_path.unlink(missing_ok=True)

    def test_sidecar_is_uppercase_hex_no_newline(self, tmp_path: Path):
        """Match Bambu's on-disk format: uppercase, 32 chars, no trailing newline."""
        src = tmp_path / "src.3mf"
        _build_3mf_with_md5(src, "G28\n")
        out_path = inject_gcode_into_3mf(src, plate_id=1, start_gcode="; START", end_gcode=None)
        assert out_path is not None
        try:
            with zipfile.ZipFile(out_path) as zf:
                sidecar = zf.read("Metadata/plate_1.gcode.md5")
            assert sidecar == sidecar.upper()
            assert not sidecar.endswith(b"\n")
            assert len(sidecar) == 32
        finally:
            out_path.unlink(missing_ok=True)

    def test_no_md5_member_is_not_created(self, tmp_path: Path):
        """A 3MF without an md5 sidecar shouldn't gain one — firmware isn't
        validating it and inventing a member could surprise older files."""
        src = tmp_path / "src.3mf"
        _build_3mf(src, _SAMPLE_PLATE_GCODE)  # no .md5 member
        out_path = inject_gcode_into_3mf(src, plate_id=1, start_gcode="; START", end_gcode=None)
        assert out_path is not None
        try:
            with zipfile.ZipFile(out_path) as zf:
                names = zf.namelist()
            assert "Metadata/plate_1.gcode.md5" not in names
        finally:
            out_path.unlink(missing_ok=True)

    def test_other_member_compression_preserved(self, tmp_path: Path):
        """Non-target members keep their original compression (P1S preview
        parser chokes on re-DEFLATE'd STORE'd PNGs)."""
        src = tmp_path / "src.3mf"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr(zipfile.ZipInfo("Metadata/plate_1.gcode"), "G28\n")
            stored = zipfile.ZipInfo("Metadata/plate_1.png")
            stored.compress_type = zipfile.ZIP_STORED
            zf.writestr(stored, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        out_path = inject_gcode_into_3mf(src, plate_id=1, start_gcode=None, end_gcode="; END")
        assert out_path is not None
        try:
            with zipfile.ZipFile(out_path) as zf:
                assert zf.getinfo("Metadata/plate_1.png").compress_type == zipfile.ZIP_STORED
        finally:
            out_path.unlink(missing_ok=True)
