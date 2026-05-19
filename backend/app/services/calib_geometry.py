"""Geometry helpers for calibration scaffolds (W2 Phase 0).

Thin wrappers over trimesh for the operations the per-mode builders
need: cut a tower at the required Z, scale a flow-rate plate by nozzle
ratio, convert STEP source to STL when the slicer pipeline only accepts
mesh formats. Kept in their own module so the per-mode builders read as
intent (``z_cut(stl, 6.0)``) rather than trimesh-internals plumbing.

Inputs and outputs are byte strings so this module composes cleanly with
:mod:`calib_3mf_writer`: builders pipe ``z_cut(bytes) →
write_calibration_3mf(geometry_bytes=..., geometry_kind='stl')`` without
touching the filesystem.

Trimesh is already a dependency (``stl_thumbnail`` uses it). STEP support
is best-effort — the slicer's CLI accepts STEP directly, so for Vol-Speed
Tower (Phase 6) the builder can hand STEP through unchanged when
:func:`step_to_stl` isn't available in the deployment.
"""

from __future__ import annotations

import io
import logging
from typing import Literal

logger = logging.getLogger(__name__)


GeometryKind = Literal["stl", "3mf"]


class GeometryError(Exception):
    """Raised when the geometry operation can't complete (load failure,
    empty mesh, etc.). Caller decides whether to fall through to a
    shipped pre-converted fallback or surface as a 4xx."""


def z_cut(
    mesh_bytes: bytes,
    max_z_mm: float,
    *,
    source_fmt: GeometryKind = "stl",
) -> bytes:
    """Cut the mesh at ``z = max_z_mm`` and keep the lower part.

    Used by tower modes — the upstream STL covers the full BS-suggested
    range (e.g. PA Tower covers K = 0 → 0.1 over the full tower), but a
    given user sweep might only need a fraction of that range. We trim
    the geometry so the per-Z custom-gcode list has nothing to land on
    above the requested ``end`` value.

    The cut is done **per connected body** with capping on:

    - ``process=True`` welds the per-triangle duplicate vertices STL
      stores, so ``slice_plane`` has the edge connectivity it needs — a
      cut of an unwelded triangle soup comes back holed.
    - trimesh's ``cap=False`` returns an *open* shell (verified: even a
      primitive box loses its bottom face), which the slicer then prints
      as mid-air gaps. ``cap=True`` is required for a watertight result.
    - Capping triangulates the cut face via a polygon enclosure tree,
      which drags in the optional ``rtree`` package once a single cut
      crosses more than one disjoint contour. Splitting into bodies
      first means every body's cut is a single contour, so capping
      stays rtree-free. Bodies wholly below the plane pass through
      untouched; bodies wholly above are dropped.

    Output is the same format as the input (so the rest of the pipeline
    doesn't have to know we cut anything).
    """
    import trimesh

    try:
        mesh = trimesh.load(io.BytesIO(mesh_bytes), file_type=source_fmt, process=True)
    except Exception as exc:
        raise GeometryError(f"z_cut: failed to load {source_fmt}: {exc}") from exc

    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(mesh.dump())
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise GeometryError("z_cut: input did not decode to a non-empty mesh")
    mesh.merge_vertices()

    max_z = float(max_z_mm)
    plane_origin = (0.0, 0.0, max_z)
    plane_normal = (0.0, 0.0, -1.0)  # keep everything below

    kept: list = []
    try:
        for body in mesh.split(only_watertight=False):
            z_lo, z_hi = float(body.bounds[0, 2]), float(body.bounds[1, 2])
            if z_hi <= max_z:
                kept.append(body)  # wholly below the cut — pass through
            elif z_lo >= max_z:
                continue  # wholly above the cut — drop
            else:
                piece = body.slice_plane(plane_origin, plane_normal, cap=True)
                if piece is not None and not piece.is_empty:
                    kept.append(piece)
    except Exception as exc:
        raise GeometryError(f"z_cut: slice_plane failed: {exc}") from exc

    if not kept:
        raise GeometryError(f"z_cut: result empty (max_z={max_z_mm} below mesh bounds?)")
    cut = trimesh.util.concatenate(kept) if len(kept) > 1 else kept[0]

    out = io.BytesIO()
    cut.export(out, file_type=source_fmt)
    return out.getvalue()


def scale_xyz(
    mesh_bytes: bytes,
    *,
    sx: float = 1.0,
    sy: float = 1.0,
    sz: float = 1.0,
    source_fmt: GeometryKind = "stl",
) -> bytes:
    """Per-axis scale a mesh.

    Used by Flow Rate (XY scale by nozzle ratio, Z by layer-height
    multiplier) and Vol-Speed (XY fit to bed width). All-ones is a no-op
    and returns the original bytes unchanged so callers can pass through
    without a conditional.
    """
    if sx == 1.0 and sy == 1.0 and sz == 1.0:
        return mesh_bytes

    import numpy as np
    import trimesh

    try:
        mesh = trimesh.load(io.BytesIO(mesh_bytes), file_type=source_fmt, process=False)
    except Exception as exc:
        raise GeometryError(f"scale_xyz: failed to load {source_fmt}: {exc}") from exc

    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(mesh.dump())
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise GeometryError("scale_xyz: input did not decode to a non-empty mesh")

    matrix = np.diag([sx, sy, sz, 1.0])
    mesh.apply_transform(matrix)

    out = io.BytesIO()
    mesh.export(out, file_type=source_fmt)
    return out.getvalue()


def step_to_stl(step_bytes: bytes) -> bytes:
    """Convert STEP → STL via trimesh's STEP loader (OpenCascade-backed).

    Falls through with :class:`GeometryError` when the deployment lacks
    the underlying CAD libraries — the Vol-Speed builder catches it and
    drops back to the shipped pre-converted ``SpeedTestStructure.stl``.
    """
    import trimesh

    try:
        mesh = trimesh.load(io.BytesIO(step_bytes), file_type="step", process=False)
    except Exception as exc:
        # STEP loaders raise a wide range of exception classes depending
        # on which backend trimesh found (or didn't find); collapse them
        # to a single domain error so callers don't have to special-case.
        raise GeometryError(f"step_to_stl: STEP load unavailable ({exc})") from exc

    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(mesh.dump())
    if not isinstance(mesh, trimesh.Trimesh) or mesh.is_empty:
        raise GeometryError("step_to_stl: STEP decoded to empty mesh")

    out = io.BytesIO()
    mesh.export(out, file_type="stl")
    return out.getvalue()
