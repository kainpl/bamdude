"""Tests for the unified ``apply_3mf_transforms`` pipeline (#422 + mesh-mode).

Folds the M970-commenting pass and gcode-injection pass into one
open/mutate/write cycle. Without this, dispatching a job that needs
both transforms would unzip + rezip a 50+ MB 3MF twice.
"""

import hashlib
import shutil
import zipfile
from pathlib import Path

from backend.app.services.gcode_patcher import (
    GcodeInjectionSpec,
    apply_3mf_transforms,
)

# Plate gcode carrying the ``; EXECUTABLE_BLOCK_END`` marker (#1516) — end
# snippets must land before it, not at EOF.
_PLATE_GCODE_WITH_BLOCK_END = """; HEADER_BLOCK_START
; max_z_height: 16.00
; HEADER_BLOCK_END
; MACHINE_START_GCODE_END
G1 X10 Y10 F3000
M104 S0 ; printer machine-end
; EXECUTABLE_BLOCK_END
"""

_PLATE_GCODE = """; HEADER_BLOCK_START
; max_z_height: 16.00
; HEADER_BLOCK_END
; machine_start_gcode = G28\\nM970.3 Q1\\n; do not patch this
M104 S220
G28
M970.3 P1 ; vibration check — should get commented
M970 P0 ; older variant — should get commented
; MACHINE_START_GCODE_END
G1 X10 Y10 F3000
"""


def _build_3mf(path: Path, plates: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for name, content in plates.items():
            zf.writestr(name, content)
            zf.writestr(f"{name}.md5", "deadbeef")


def _read_plate(path: Path, name: str) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name).decode()


def _read_md5(path: Path, name: str) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(f"{name}.md5").decode()


class TestNoOps:
    def test_returns_source_when_no_transforms_requested(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, {"Metadata/plate_1.gcode": _PLATE_GCODE})
        out, applied = apply_3mf_transforms(src)
        assert out == src
        assert applied == []

    def test_passthrough_when_mesh_already_patched(self, tmp_path: Path):
        # Already-commented M970 lines — mesh transform finds nothing to do.
        clean = "; HEADER_BLOCK_START\n; HEADER_BLOCK_END\n;M970 P0\n;M970.3 P1\n"
        src = tmp_path / "src.3mf"
        _build_3mf(src, {"Metadata/plate_1.gcode": clean})
        out, applied = apply_3mf_transforms(src, mesh_mode_fast_check_off=True)
        assert out == src
        assert applied == []


class TestMeshOnly:
    def test_comments_m970_lines_and_updates_md5(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, {"Metadata/plate_1.gcode": _PLATE_GCODE})
        out, applied = apply_3mf_transforms(src, mesh_mode_fast_check_off=True)
        try:
            assert out != src
            assert applied == ["mesh_mode_fast_check_off"]
            patched = _read_plate(out, "Metadata/plate_1.gcode")
            # Both uncommented M970 lines now start with semicolon
            assert ";M970.3 P1" in patched
            assert ";M970 P0" in patched
            # The masked machine_start_gcode parameter line is restored intact
            assert "; machine_start_gcode = G28" in patched
            # Sidecar got recomputed (no longer the placeholder)
            assert _read_md5(out, "Metadata/plate_1.gcode") != "deadbeef"
        finally:
            import shutil

            shutil.rmtree(out.parent, ignore_errors=True)


class TestInjectionOnly:
    def test_splices_start_snippet_at_marker(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, {"Metadata/plate_1.gcode": _PLATE_GCODE})
        spec = GcodeInjectionSpec(plate_id=1, start_gcode="; HELLO\n", end_gcode=None)
        out, applied = apply_3mf_transforms(src, gcode_injection=spec)
        try:
            assert out != src
            assert applied == [{"name": "gcode_injection", "plate_id": 1}]
            patched = _read_plate(out, "Metadata/plate_1.gcode")
            assert "; HELLO\n; MACHINE_START_GCODE_END" in patched
        finally:
            import shutil

            shutil.rmtree(out.parent, ignore_errors=True)

    def test_resolves_prusa_alias_via_header(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, {"Metadata/plate_1.gcode": _PLATE_GCODE})
        # {max_layer_z} → resolved to max_z_height = 16.00 via Prusa alias.
        spec = GcodeInjectionSpec(plate_id=1, start_gcode="G1 Z{max_layer_z}", end_gcode=None)
        out, applied = apply_3mf_transforms(src, gcode_injection=spec)
        try:
            patched = _read_plate(out, "Metadata/plate_1.gcode")
            assert "G1 Z16.00" in patched
            assert "{max_layer_z}" not in patched
        finally:
            import shutil

            shutil.rmtree(out.parent, ignore_errors=True)


class TestBothInOnePass:
    def test_single_pass_applies_both_transforms(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, {"Metadata/plate_1.gcode": _PLATE_GCODE})
        spec = GcodeInjectionSpec(plate_id=1, start_gcode="; INJECTED\n", end_gcode="; END\n")
        out, applied = apply_3mf_transforms(src, mesh_mode_fast_check_off=True, gcode_injection=spec)
        try:
            # Both patches show up in the chain-of-custody
            assert "mesh_mode_fast_check_off" in applied
            assert {"name": "gcode_injection", "plate_id": 1} in applied
            patched = _read_plate(out, "Metadata/plate_1.gcode")
            # M970 lines commented
            assert ";M970.3 P1" in patched
            # Snippet spliced before marker
            assert "; INJECTED\n; MACHINE_START_GCODE_END" in patched
            # End snippet at EOF
            assert patched.rstrip().endswith("; END")
            # md5 recomputed once for the touched plate
            assert _read_md5(out, "Metadata/plate_1.gcode") != "deadbeef"
        finally:
            import shutil

            shutil.rmtree(out.parent, ignore_errors=True)

    def test_only_target_plate_md5_recomputed(self, tmp_path: Path):
        # Two plates; injection touches plate 1, mesh transform touches both.
        src = tmp_path / "src.3mf"
        _build_3mf(
            src,
            {
                "Metadata/plate_1.gcode": _PLATE_GCODE,
                "Metadata/plate_2.gcode": _PLATE_GCODE,
            },
        )
        spec = GcodeInjectionSpec(plate_id=1, start_gcode="; X\n", end_gcode=None)
        out, applied = apply_3mf_transforms(src, mesh_mode_fast_check_off=True, gcode_injection=spec)
        try:
            # Both plates' md5 sidecars should be updated since mesh patched both
            assert _read_md5(out, "Metadata/plate_1.gcode") != "deadbeef"
            assert _read_md5(out, "Metadata/plate_2.gcode") != "deadbeef"
            # Only plate 1 has the injected snippet
            assert "; X\n; MACHINE_START_GCODE_END" in _read_plate(out, "Metadata/plate_1.gcode")
            assert "; X\n; MACHINE_START_GCODE_END" not in _read_plate(out, "Metadata/plate_2.gcode")
        finally:
            import shutil

            shutil.rmtree(out.parent, ignore_errors=True)


class TestPlateFallback:
    def test_missing_target_plate_falls_back_to_first(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, {"Metadata/plate_5.gcode": _PLATE_GCODE})
        # Asking for plate 1 but only plate 5 exists — should still inject
        # rather than no-op (matches inject_gcode_into_3mf's own fallback).
        spec = GcodeInjectionSpec(plate_id=1, start_gcode="; X\n", end_gcode=None)
        out, applied = apply_3mf_transforms(src, gcode_injection=spec)
        try:
            assert out != src
            assert "; X\n; MACHINE_START_GCODE_END" in _read_plate(out, "Metadata/plate_5.gcode")
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)


class TestP1SEndAnchor:
    """#1516: end snippets land before ``; EXECUTABLE_BLOCK_END`` (P1S ignores
    g-code after that marker), with an EOF fallback when the marker is absent."""

    def test_end_lands_before_executable_block_end(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, {"Metadata/plate_1.gcode": _PLATE_GCODE_WITH_BLOCK_END})
        spec = GcodeInjectionSpec(plate_id=1, start_gcode=None, end_gcode="; EJECT-SWEEP")
        out, applied = apply_3mf_transforms(src, gcode_injection=spec)
        try:
            patched = _read_plate(out, "Metadata/plate_1.gcode")
            assert patched.index("; EJECT-SWEEP") < patched.index("; EXECUTABLE_BLOCK_END")
            assert patched.index("M104 S0 ; printer machine-end") < patched.index("; EJECT-SWEEP")
            # Nothing executable remains after the marker.
            assert patched[patched.index("; EXECUTABLE_BLOCK_END") :].strip() == "; EXECUTABLE_BLOCK_END"
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)

    def test_end_falls_back_to_eof_without_marker(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, {"Metadata/plate_1.gcode": _PLATE_GCODE})  # no EXECUTABLE_BLOCK_END
        spec = GcodeInjectionSpec(plate_id=1, start_gcode=None, end_gcode="; BYE")
        out, applied = apply_3mf_transforms(src, gcode_injection=spec)
        try:
            patched = _read_plate(out, "Metadata/plate_1.gcode")
            assert patched.rstrip().endswith("; BYE")
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)


class TestP1SMd5Sidecar:
    """#1516: recomputed ``.gcode.md5`` matches Bambu's on-disk format —
    uppercase, 32 hex chars, no trailing newline — and is never invented."""

    def test_md5_uppercase_32char_no_newline_and_matches(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        _build_3mf(src, {"Metadata/plate_1.gcode": _PLATE_GCODE})
        spec = GcodeInjectionSpec(plate_id=1, start_gcode="; HELLO\n", end_gcode=None)
        out, applied = apply_3mf_transforms(src, gcode_injection=spec)
        try:
            with zipfile.ZipFile(out) as zf:
                gcode = zf.read("Metadata/plate_1.gcode")
                sidecar = zf.read("Metadata/plate_1.gcode.md5")
            assert sidecar != b"deadbeef"
            assert len(sidecar) == 32
            assert sidecar == sidecar.upper()
            assert not sidecar.endswith(b"\n")
            expected = hashlib.md5(gcode, usedforsecurity=False).hexdigest().upper().encode("ascii")
            assert sidecar == expected
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)

    def test_md5_not_invented_when_absent(self, tmp_path: Path):
        """A plate without a sidecar shouldn't gain one after a transform."""
        src = tmp_path / "src.3mf"
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("Metadata/plate_1.gcode", _PLATE_GCODE)  # no .md5 member
        spec = GcodeInjectionSpec(plate_id=1, start_gcode="; X\n", end_gcode=None)
        out, applied = apply_3mf_transforms(src, gcode_injection=spec)
        try:
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
            assert "Metadata/plate_1.gcode.md5" not in names
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)


class TestP1SCompressionPreserved:
    """#1516: the rewrite keeps each member's original compress_type — the P1S
    preview parser rejects a re-DEFLATE'd STORE'd preview PNG."""

    def test_stored_member_stays_stored(self, tmp_path: Path):
        src = tmp_path / "src.3mf"
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Metadata/plate_1.gcode", _PLATE_GCODE)
            zf.writestr("Metadata/plate_1.gcode.md5", "deadbeef")
            stored = zipfile.ZipInfo("Metadata/plate_1.png")
            stored.compress_type = zipfile.ZIP_STORED
            zf.writestr(stored, b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        spec = GcodeInjectionSpec(plate_id=1, start_gcode="; X\n", end_gcode=None)
        out, applied = apply_3mf_transforms(src, gcode_injection=spec)
        try:
            with zipfile.ZipFile(out) as zf:
                assert zf.getinfo("Metadata/plate_1.png").compress_type == zipfile.ZIP_STORED
                # The gcode plate keeps its DEFLATE compression.
                assert zf.getinfo("Metadata/plate_1.gcode").compress_type == zipfile.ZIP_DEFLATED
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)
