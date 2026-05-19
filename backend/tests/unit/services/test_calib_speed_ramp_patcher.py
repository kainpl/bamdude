"""Tests the post-slice speed-ramp patcher — calib_speed_ramp_patcher.

Covers the g-code rewrite core (regular + precise), the band-boundary
float-accumulation divergence the precise patcher exists to fix, and the
``patch_vfa_ramp`` 3MF wrapper.
"""

from __future__ import annotations

import hashlib
import io
import math
import zipfile

import pytest

from backend.app.services.calib_speed_ramp_patcher import (
    _patch_gcode,
    _patch_gcode_insert_temp,
    _patch_gcode_precise,
    _patch_gcode_retraction,
    patch_retraction_tower,
    patch_temp_tower,
    patch_vfa_ramp,
)


def _build_layers(n: int, layer_height: float = 0.2) -> str:
    """A minimal sliced-tower g-code: n layer blocks, each with a
    ``Z_HEIGHT`` comment (rounded display value), a ``LAYER_HEIGHT``
    comment, and one bare ``G1 F`` feedrate line."""
    chunks = []
    accum = 0.0
    for _ in range(n):
        accum += layer_height
        chunks.append(f"; CHANGE_LAYER\n; Z_HEIGHT: {round(accum, 3)}\n; LAYER_HEIGHT: {layer_height}\nG1 F1200\n")
    return "".join(chunks)


def _make_3mf(gcode_text: str, *, with_gcode: bool = True) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if with_gcode:
            z.writestr("Metadata/plate_1.gcode", gcode_text)
            z.writestr("Metadata/plate_1.gcode.md5", "STALE")
        z.writestr("3D/3dmodel.model", "<model/>")
    return buf.getvalue()


# ---------- _patch_gcode (regular) ----------


def test_patch_gcode_rewrites_per_layer_feedrate():
    """Each layer's bare ``G1 F`` is rewritten to round(speed_fn(z))·60;
    the first layer block keeps its slicer-assigned feedrate."""
    gcode = (
        "; CHANGE_LAYER\n; Z_HEIGHT: 0.2\n; LAYER_HEIGHT: 0.2\nG1 F1200\n"
        "; CHANGE_LAYER\n; Z_HEIGHT: 5\n; LAYER_HEIGHT: 0.2\nG1 F1200\n"
        "; CHANGE_LAYER\n; Z_HEIGHT: 10\n; LAYER_HEIGHT: 0.2\nG1 F1200\n"
        "; CHANGE_LAYER\n; Z_HEIGHT: 15\n; LAYER_HEIGHT: 0.2\nG1 F1200\n"
    )
    patched, count = _patch_gcode(gcode, lambda z: z)
    assert count == 3
    # speed = round(z); feedrate = speed·60.
    assert "G1 F300\n" in patched  # z=5
    assert "G1 F600\n" in patched  # z=10
    assert "G1 F900\n" in patched  # z=15
    # The first-layer block (block_index 0) is left untouched.
    assert patched.count("G1 F1200\n") == 1


def test_patch_gcode_ignores_travel_lifts():
    """A ``G1 Z.. F..`` travel move carries a Z word, so the bare-F regex
    never matches it — only the layer's print feedrate is rewritten."""
    gcode = "; CHANGE_LAYER\n; Z_HEIGHT: 0.2\nG1 F1200\n; CHANGE_LAYER\n; Z_HEIGHT: 5\nG1 Z5 F1200\nG1 F1200\n"
    patched, count = _patch_gcode(gcode, lambda z: 100)
    assert count == 1
    assert "G1 Z5 F1200\n" in patched  # travel lift untouched
    assert "G1 F6000\n" in patched  # round(100)·60


def test_patch_gcode_feedrate_floored_at_one():
    """A zero/negative speed still emits a positive feedrate (max(1, ·))."""
    gcode = "; CHANGE_LAYER\n; Z_HEIGHT: 0.2\nG1 F1200\n; CHANGE_LAYER\n; Z_HEIGHT: 5\nG1 F1200\n"
    patched, count = _patch_gcode(gcode, lambda z: 0)
    assert count == 1
    assert "G1 F60\n" in patched  # max(1, 0)·60


# ---------- _patch_gcode_precise ----------


def test_patch_gcode_precise_accumulates_layer_height():
    """The precise patcher reconstructs print_z as a running sum of the
    LAYER_HEIGHT comment instead of reading Z_HEIGHT."""
    gcode = (
        "; CHANGE_LAYER\n; LAYER_HEIGHT: 0.2\nG1 F1\n"  # block 0 — accum 0.2, skipped
        "; CHANGE_LAYER\n; LAYER_HEIGHT: 0.2\nG1 F1\n"  # block 1 — accum 0.4
        "; CHANGE_LAYER\n; LAYER_HEIGHT: 0.2\nG1 F1\n"  # block 2 — accum 0.6
    )
    patched, count = _patch_gcode_precise(gcode, lambda z: z * 100)
    assert count == 2
    assert "G1 F2400\n" in patched  # z=0.4 → 40 → ·60
    assert "G1 F3600\n" in patched  # z=0.6 → 60 → ·60


def test_patch_gcode_precise_matches_engine_at_band_boundary():
    """The reason the precise patcher exists (analysis §8).

    The engine accumulates print_z as a running float sum; 50 layers of
    0.2 mm sum to 9.999999999999996, not 10.0. The rounded ``Z_HEIGHT``
    comment says ``10.0``, which flips a floor()-banded VFA ramp by one
    band at the 5 mm boundary. The regular patcher (reads Z_HEIGHT) lands
    in the wrong band; the precise patcher bit-matches the engine.
    """
    gcode = _build_layers(50)  # last layer: Z_HEIGHT comment rounds to 10.0
    vfa = lambda z: 40 + math.floor(z / 5.0) * 10  # noqa: E731

    regular, _ = _patch_gcode(gcode, vfa)
    precise, _ = _patch_gcode_precise(gcode, vfa)

    # Regular reads Z_HEIGHT 10.0 → floor(10/5)=2 → 60 mm/s → F3600.
    assert regular.rstrip().endswith("G1 F3600")
    # Precise reconstructs 9.999999999999996 → floor(.../5)=1 → 50 → F3000.
    assert precise.rstrip().endswith("G1 F3000")
    assert regular != precise


# ---------- patch_vfa_ramp (3MF wrapper) ----------


def test_patch_vfa_ramp_rewrites_3mf_and_recomputes_md5():
    threemf = _make_3mf(_build_layers(10))
    out = patch_vfa_ramp(threemf, start=40, step=10)

    with zipfile.ZipFile(io.BytesIO(out)) as z:
        names = z.namelist()
        gcode = z.read("Metadata/plate_1.gcode").decode()
        md5 = z.read("Metadata/plate_1.gcode.md5").decode()

    # Other entries are copied verbatim.
    assert "3D/3dmodel.model" in names
    # The md5 sidecar is recomputed for the rewritten g-code.
    assert md5 != "STALE"
    assert md5 == hashlib.md5(gcode.encode()).hexdigest().upper()
    # The ramp law start=40/step=10 produced banded feedrates.
    assert "G1 F2400\n" in gcode  # band 0 → 40 mm/s → ·60


def test_patch_vfa_ramp_rejects_unrecognised_gcode():
    """No '; CHANGE_LAYER' + bare 'G1 F' structure → explicit failure."""
    threemf = _make_3mf("; just a comment\nG1 X1 Y1\n")
    with pytest.raises(ValueError, match="vfa patcher"):
        patch_vfa_ramp(threemf, start=40, step=10)


def test_patch_vfa_ramp_rejects_3mf_without_gcode():
    threemf = _make_3mf("", with_gcode=False)
    with pytest.raises(ValueError, match="no Metadata/plate_1.gcode"):
        patch_vfa_ramp(threemf, start=40, step=10)


# ---------- _patch_gcode_insert_temp / patch_temp_tower ----------


def test_patch_gcode_insert_temp_inserts_m104_per_layer():
    """One M104 is inserted right after each layer's Z_HEIGHT comment."""
    gcode = (
        "; CHANGE_LAYER\n; Z_HEIGHT: 2\nG1 X1\n"
        "; CHANGE_LAYER\n; Z_HEIGHT: 12\nG1 X1\n"
        "; CHANGE_LAYER\n; Z_HEIGHT: 25\nG1 X1\n"
    )
    temp_fn = lambda z: 230 - math.floor(z / 10.001) * 5  # noqa: E731
    patched, count = _patch_gcode_insert_temp(gcode, temp_fn)
    assert count == 3
    # z=2 → band 0 → 230; z=12 → band 1 → 225; z=25 → band 2 → 220.
    assert "M104 S230 ; calib temp\n" in patched
    assert "M104 S225 ; calib temp\n" in patched
    assert "M104 S220 ; calib temp\n" in patched
    # The M104 sits immediately after the Z_HEIGHT line it belongs to.
    lines = patched.splitlines()
    assert lines[lines.index("; Z_HEIGHT: 2") + 1] == "M104 S230 ; calib temp"


def test_patch_gcode_insert_temp_10001_divisor_keeps_z10_in_band0():
    """The 10.001 divisor (verbatim from BS) keeps a layer at z=10.0 in
    band 0 — the band boundary sits just above the 10 mm grid line."""
    gcode = "; CHANGE_LAYER\n; Z_HEIGHT: 10.0\nG1 X1\n; CHANGE_LAYER\n; Z_HEIGHT: 10.2\nG1 X1\n"
    patched, count = _patch_gcode_insert_temp(gcode, lambda z: 230 - math.floor(z / 10.001) * 5)
    assert count == 2
    assert "M104 S230 ; calib temp\n" in patched  # z=10.0 → band 0
    assert "M104 S225 ; calib temp\n" in patched  # z=10.2 → band 1


def test_patch_temp_tower_inserts_ramp_and_recomputes_md5():
    threemf = _make_3mf(_build_layers(60))  # 60 × 0.2 mm → z up to 12 mm
    out = patch_temp_tower(threemf, start=230)

    with zipfile.ZipFile(io.BytesIO(out)) as z:
        gcode = z.read("Metadata/plate_1.gcode").decode()
        md5 = z.read("Metadata/plate_1.gcode.md5").decode()

    # One M104 inserted per layer block.
    assert gcode.count("; calib temp") == 60
    # Bands below 10.001 mm hold the start temp; above it, start − 5.
    assert "M104 S230 ; calib temp" in gcode
    assert "M104 S225 ; calib temp" in gcode
    assert md5 == hashlib.md5(gcode.encode()).hexdigest().upper()


def test_patch_temp_tower_rejects_unrecognised_gcode():
    threemf = _make_3mf("; just a comment\nG1 X1 Y1\n")
    with pytest.raises(ValueError, match="temp patcher"):
        patch_temp_tower(threemf, start=230)


# ---------- _patch_gcode_retraction ----------


def _build_retraction_layers(n: int, layer_height: float = 0.2) -> str:
    """A minimal sliced retraction tower: each layer has a deretract
    (E-only positive), one print-extrusion move (X/Y + positive E), a
    wipe-while-retract (X/Y + negative E) and a final pure retract
    (E-only negative). Preset retraction length = 0.2 mm (0.19 wipe +
    0.01 final = the deretract 0.2). Ends with machine end g-code whose
    own retract must survive untouched."""
    chunks = []
    for _ in range(n):
        chunks.append(
            f"; CHANGE_LAYER\n; LAYER_HEIGHT: {layer_height}\n"
            "G1 E0.2 F1800\n"  # deretract
            "G1 X1 Y1 E0.05\n"  # print extrusion — must NOT be scaled
            "; WIPE_START\n"
            "G1 X2 Y2 E-0.19\n"  # wipe-while-retract
            "; WIPE_END\n"
            "G1 E-0.01 F3000\n"  # final pure retract
        )
    chunks.append("; filament end gcode\nG1 E-0.8 F1800 ; retract\n")
    return "".join(chunks)


def test_patch_retraction_scales_retraction_moves_by_band():
    """Every retraction move is scaled by length(z)/preset; print
    extrusion is left alone. start=0/step=0.1: band 0 zeroes all
    retraction, band 1 (factor 0.1/0.2=0.5) halves it."""
    gcode = _build_retraction_layers(10)
    patched, count = _patch_gcode_retraction(gcode, start=0.0, step=0.1)
    assert count > 0
    lines = patched.splitlines()

    # Band 0 (layers 1-7, z ≤ 1.4): every retraction move zeroed.
    assert "G1 E0 F1800" in lines  # deretract → 0
    assert "G1 X2 Y2 E0" in lines  # wipe-while-retract → 0
    assert "G1 E0 F3000" in lines  # final retract → 0
    # Band 1 (layer 8+, factor 0.5): 0.2→0.1, 0.19→0.095, 0.01→0.005.
    assert "G1 E0.1 F1800" in lines
    assert "G1 X2 Y2 E-0.095" in lines
    assert "G1 E-0.005 F3000" in lines
    # Print extrusion is never touched, in any band.
    assert patched.count("G1 X1 Y1 E0.05") == 10


def test_patch_retraction_leaves_end_gcode_untouched():
    """The machine end g-code's own retract (``G1 E-0.8``) sits past the
    '; filament end gcode' marker and must never be scaled."""
    gcode = _build_retraction_layers(10)
    patched, _ = _patch_gcode_retraction(gcode, start=0.0, step=0.1)
    assert "G1 E-0.8 F1800 ; retract" in patched


def test_patch_retraction_band_boundary_is_engine_exact():
    """The band steps on the engine's float-accumulated print_z, not the
    nominal grid: 7 layers of 0.2 sum to 1.4, and 1.4 - 0.4 floors to 0,
    so band 1 starts at layer 8 (z=1.6), not layer 7 (z=1.4)."""
    gcode = _build_retraction_layers(8)
    patched, _ = _patch_gcode_retraction(gcode, start=0.0, step=0.1)
    blocks = patched.split("; CHANGE_LAYER")
    # block[7] = layer 7 (z=1.4) still band 0 → retraction zeroed.
    assert "G1 E0.2 F1800" not in blocks[7]
    assert "G1 E0 F1800" in blocks[7]
    # block[8] = layer 8 (z=1.6) band 1 → deretract scaled to 0.1.
    assert "G1 E0.1 F1800" in blocks[8]


def test_patch_retraction_nonzero_start():
    """start=0.4: band 0 holds retraction at 0.4 mm (factor 0.4/0.2=2)."""
    gcode = _build_retraction_layers(3)
    patched, _ = _patch_gcode_retraction(gcode, start=0.4, step=0.1)
    assert "G1 E0.4 F1800" in patched  # deretract 0.2 → 0.4
    assert "G1 X2 Y2 E-0.38" in patched  # wipe 0.19 → 0.38


def test_patch_retraction_tower_rewrites_3mf_and_recomputes_md5():
    threemf = _make_3mf(_build_retraction_layers(10))
    out = patch_retraction_tower(threemf, start=0.0, step=0.1)
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        names = z.namelist()
        gcode = z.read("Metadata/plate_1.gcode").decode()
        md5 = z.read("Metadata/plate_1.gcode.md5").decode()
    assert "3D/3dmodel.model" in names
    assert md5 == hashlib.md5(gcode.encode()).hexdigest().upper()
    assert md5 != "STALE"


def test_patch_retraction_tower_rejects_unrecognised_gcode():
    threemf = _make_3mf("; just a comment\nG1 X1 Y1\n")
    with pytest.raises(ValueError, match="retraction patcher"):
        patch_retraction_tower(threemf, start=0.0, step=0.1)
