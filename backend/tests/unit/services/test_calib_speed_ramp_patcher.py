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
    _patch_gcode_precise,
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
