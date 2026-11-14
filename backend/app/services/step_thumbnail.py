"""The STEP conversion child (spec §6–§7): one STEP/STP in, one bounded binary STL out. A disposable process.

Run by the preview service through the worker guardian as ``python -m backend.app.services.step_thumbnail``, one
JSON line on stdin (bootstrap withheld until containment is attached). It is the only process that imports OCP —
after it moved into its private attempt directory, capped its data segment, silenced Windows error dialogs and
activated exactly the bundle the service named. It never imports the PNG renderer, the ORM or any route, and never
publishes: it writes ``mesh.stl`` and a bounded ``result.json`` (closed outcome, reason, stage) and exits.

Recipe (stage-0 evidence, addendum §1): STEPControl_Reader only (no CAF); static unit before ReadFile and
SetSystemLengthUnit(1.0) after it; load and transfer check lists; every root transferred; no external reference
(counted, FileName() never called); no left-handed CTO3d; finite non-degenerate bounds; face occurrences capped
before meshing; one tessellation max(0.1 mm, diag/1000), angular 0.5, absolute, sequential; mesher status flags;
triangles counted before anything is gathered; every face occurrence triangulated; location and winding
(REVERSED XOR negative transform) applied; float32 range; N in the header and 84 + 50·N bytes. A model that is
partial in any of these ways is render_failed, never a picture of part of it.

``mode: "print"`` (project assembly) is the same recipe for a mesh that will be PRINTED: Bambu Studio's import
precision (0.003 mm absolute, angular 0.5 — its STEP.hpp defaults) under limits of its own, a model no printer
could hold refused before it is meshed, and only whole outward solids — a loose surface or an inside-out body is
no part. The thumbnail mode does not change.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import sys
import time
from pathlib import Path

from backend.app.services.preview_protocol import (
    CAD_FACE_OCCURRENCES,
    CAD_PRINT_FACE_OCCURRENCES,
    CAD_PRINT_TRIANGLES,
    CAD_TRIANGLES,
    JSON_BYTES,
    STEP_DATA_LIMIT_BYTES,
    STEP_INPUT_BYTES,
    STEP_PRINT_DATA_LIMIT_BYTES,
    STEP_PRINT_INPUT_BYTES,
)

INPUT, OUTPUT, RESULT = "input.step", "mesh.stl", "result.json"
F32_MAX = 3.4028234663852886e38
MAX_ABS_MM = 1.0e7  # ±1e100 from an unbounded face is finite; only this bound catches it (stage 0)
ANGULAR = 0.5
PRINT_DEFLECTION_MM = 0.003  # Bambu Studio's default linear deflection for a STEP import
PRINT_MAX_EXTENT_MM = 2000.0  # a longer diagonal fits no printer; at 0.003 mm it would also be meshed for hours
#: per mode: (input bytes, face occurrences, triangles, data segment)
MODES = {
    "thumbnail": (STEP_INPUT_BYTES, CAD_FACE_OCCURRENCES, CAD_TRIANGLES, STEP_DATA_LIMIT_BYTES),
    "print": (STEP_PRINT_INPUT_BYTES, CAD_PRINT_FACE_OCCURRENCES, CAD_PRINT_TRIANGLES, STEP_PRINT_DATA_LIMIT_BYTES),
}
FAULTS = frozenset({"drop_triangulation", "static_unit_m", "access_violation", "interrupt_write"})  # test seams (S3)

# Measured allowlist (addendum §3, 35/40 real files + 34/34 fixtures). Anything else refuses.
NON_GEOMETRIC = re.compile(
    r"^(StepVisual_|StepAP242_|StepDimTol_|StepRepr_PropertyDefinition$|StepBasic_GeneralPropertyAssociation$|"
    r"StepRepr_IntegerRepresentationItem$|StepRepr_DescriptiveRepresentationItem$|"
    r"StepRepr_MechanicalDesignAndDraughtingRelationship$|StepRepr_ShapeAspectRelationship$|"
    r"StepRepr_CompoundRepresentationItem$|StepRepr_ReprItemAnd\w*MeasureWithUnit\w*$|"
    r"StepShape_(DimensionalCharacteristicRepresentation|PlusMinusTolerance|ToleranceValue|LimitsAndFits|"
    r"DimensionalSize\w*|DimensionalLocation\w*|ShapeDimensionRepresentation|AngularSize|AngularLocation)$)"
)
SYNTAX_ONLY = ("not in alphabetic order",)
LABEL_PARAMS = frozenset({"name", "description", "id", "property_definition.name"})
UNIT_TYPES = re.compile(r"^StepBasic_(ConversionBasedUnitAnd\w+Unit|AreaUnit|VolumeUnit)$")
HEALING = (
    "Missing seam-edge added",
    "Edges were intersecting, corrected",
    "Surface forced to be periodic",
    "Segments were disordered; fixed",
    # A real library model (2026-10-19, owner) transferred whole and passed every later check, refused only on these
    # notes. Two are repairs OpenCascade made: a wire split into several, faces turned the right way in their shell.
    # The projection note is no repair: the curve stays as it is and OCCT raises the vertex tolerances to the gap it
    # measured (StepToTopoDS_TranslateEdge, OCCT V8_0_1; Codex F1-M1). Accepted for a thumbnail — a picture, not a
    # promise of geometric accuracy: a whole transfer and every face triangulated show the model is all there, not
    # that the result is exact.
    "Wire was split on several wires",
    "Poor result from projection vertex / curve 3d",
    "Faces were incorrectly oriented in the shell, corrected",
)


# The strict reasons in the order a refusal names them: the most specific first (all of them travel in "reasons").
STRICT_ORDER = (
    "external_reference",
    "unsupported_mirror_axes",
    "no_roots",
    "root_not_transferred",
    "transfer_fail",
    "load_fail",
    "transfer_warn",
    "load_warn",
)


class Refusal(Exception):
    def __init__(self, outcome: str, why: str):
        super().__init__(why)
        self.outcome, self.why = outcome, why


def print_refusal(*, faces: int, solid_faces: int, solids: int, negative: int) -> str | None:
    """Why a transferred model is no printable part: a face outside every solid, or a body turned inside out."""
    if solids == 0 or solid_faces != faces:
        return "not_solid"
    if negative:
        return "inverted_solid"
    return None


def load_check_allowed(etype: str | None, message: str) -> bool:
    match = re.search(r"\(([\w.]+)\)", message)
    param = match.group(1) if match else None
    if NON_GEOMETRIC.match(etype or "") or any(s in message for s in SYNTAX_ONLY):
        return True
    if param in LABEL_PARAMS and ("quoted String" in message or "empty" in message):
        return True
    return bool(UNIT_TYPES.match(etype or "") and param == "dimensions")


def transfer_warning_allowed(message: str) -> bool:
    return any(h in message for h in HEALING)


def _set_data_limit(limit: int) -> None:
    """A per-process allocation backstop before any heavy import (Linux); not an RSS limit of the tree."""
    if sys.platform.startswith("linux"):
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_DATA)
        cap = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        resource.setrlimit(resource.RLIMIT_DATA, (cap, cap))


def _windows_error_mode() -> None:
    if os.name == "nt":
        import ctypes

        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX: no dialog, ever
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)


def _native_crash() -> None:
    """The ``access_violation`` seam: a real native fault (a ctypes NULL read is caught by ctypes' own guard)."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.CreateThread.restype = ctypes.c_void_p
        kernel32.CreateThread(None, 0, ctypes.c_void_p(0), None, 0, None)
        time.sleep(10)
    else:
        import signal

        os.kill(os.getpid(), signal.SIGSEGV)
        time.sleep(10)


STAGE = "stage"


_REPLACE_DELAYS = (0.005, 0.02, 0.05, 0.1)  # ~0.2 s in all


def _replace(source: str, target: str) -> None:
    """os.replace with a short bounded retry: on Windows a reader that holds the target open (an antivirus scan, a
    poll of the marker) makes the rename fail with a sharing violation for as long as it holds it."""
    for delay in _REPLACE_DELAYS:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(source, target)


def _stage(result: dict, name: str) -> None:
    """The stage in result.json AND in a one-word marker, renamed into place: a child stopped by the parent's
    watchdog or deadline writes no result, but the parent can still log where it was (bounded enum). The marker is
    diagnostics only — one that cannot be written after the retry is skipped and never fails the conversion."""
    result["stage"] = name
    try:
        with open(STAGE + ".part", "w", encoding="ascii") as f:
            f.write(name)
        _replace(STAGE + ".part", STAGE)
    except OSError:
        pass


def _write_result(result: dict) -> None:
    data = json.dumps(result, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > JSON_BYTES:
        data = json.dumps(
            {"outcome": "render_failed", "why": "result_too_large", "stage": result.get("stage")}
        ).encode()
    with open(RESULT + ".part", "wb") as f:
        f.write(data)
    _replace(RESULT + ".part", RESULT)


def _remove_own_outputs() -> None:
    for name in (OUTPUT + ".part", OUTPUT):
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _check_lists(iterator):
    """[(entity type, message, is_fail)] — entity numbers are unreliable through OCP (Number() = 0)."""
    out = []
    iterator.Start()
    while iterator.More():
        check = iterator.Value()
        entity = check.Entity()
        etype = entity.DynamicType().Name() if entity is not None else None
        for i in range(1, check.NbFails() + 1):
            out.append((etype, check.CFail(i), True))
        for i in range(1, check.NbWarnings() + 1):
            out.append((etype, check.CWarning(i), False))
        iterator.Next()
    return out


def _left_handed_cto3d(model) -> int:
    """A CartesianTransformationOperator3d whose derived axes (ISO 10303-42 base_axis) are left-handed: OCCT turns
    the mirror into a rotation without any diagnostic (stage 0) — counted so it can be refused."""

    def ratios(d):
        r = d.DirectionRatios()
        return list(r) if isinstance(r, (list, tuple)) else [r.Value(k) for k in range(r.Lower(), r.Upper() + 1)]

    def unit(v):
        n = math.sqrt(sum(c * c for c in v))
        return [c / n for c in v] if n > 0 else None

    def dot(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    def cross(a, b):
        return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]

    count = 0
    for i in range(1, model.NbEntities() + 1):
        ent = model.Value(i)
        if ent.DynamicType().Name() != "StepGeom_CartesianTransformationOperator3d":
            continue
        u3 = unit(ratios(ent.Axis3())) if ent.HasAxis3() else [0.0, 0.0, 1.0]
        if u3 is None:
            count += 1
            continue
        a1 = ratios(ent.Axis1()) if ent.HasAxis1() else ([1.0, 0.0, 0.0] if abs(u3[0]) < 0.9 else [0.0, 1.0, 0.0])
        u1 = unit([a1[k] - dot(a1, u3) * u3[k] for k in range(3)])
        if ent.HasAxis2():
            a2 = ratios(ent.Axis2())
            u2 = unit([a2[k] - dot(a2, u3) * u3[k] - dot(a2, u1 or [0, 0, 0]) * (u1 or [0, 0, 0])[k] for k in range(3)])
        else:
            u2 = cross(u3, u1) if u1 else None
        if not (u1 and u2) or dot(u1, cross(u2, u3)) < 0:
            count += 1
    return count


def convert(cfg: dict, result: dict) -> None:
    """The recipe. Raises Refusal for every closed refusal; writes mesh.stl only after the last check."""
    mode = cfg.get("mode", "thumbnail")
    faults = set(cfg.get("faults") or ())
    if faults - FAULTS or mode not in MODES:
        raise Refusal("render_failed", "protocol_error")
    for_print = mode == "print"
    if for_print:
        result["mode"] = mode
    byte_cap, face_cap, triangle_cap, _data = MODES[mode]
    caps = {
        "byte_cap": min(int(cfg.get("byte_cap", byte_cap)), byte_cap),
        "face_cap": min(int(cfg.get("face_cap", face_cap)), face_cap),
        "triangle_cap": min(int(cfg.get("triangle_cap", triangle_cap)), triangle_cap),
    }
    _stage(result, "snapshot")
    size = os.path.getsize(INPUT)
    if size > caps["byte_cap"]:
        raise Refusal("resource_limit", "byte_cap")

    _stage(result, "cad_import")
    from backend.app.step_preview_probe import BundleOriginError, activate_bundle

    try:
        activate_bundle(Path(cfg["site"]))
        from OCP.Bnd import Bnd_Box
        from OCP.BRep import BRep_Builder, BRep_Tool
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.IFSelect import IFSelect_ReturnStatus
        from OCP.Interface import Interface_Static
        from OCP.STEPConstruct import STEPConstruct_ExternRefs
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS, TopoDS_Compound
    except (ImportError, OSError, BundleOriginError) as exc:  # missing package, broken DLL, foreign OCP
        raise Refusal("cad_unavailable", "bundle_import") from exc
    if "access_violation" in faults:
        _native_crash()

    _stage(result, "transfer")
    reader = STEPControl_Reader()
    # Static unit first (it exists only after the reader's constructor), the system unit after ReadFile: the
    # setter before ReadFile is a silent no-op and leaves metres (stage 0, both orders measured).
    Interface_Static.SetCVal_s("xstep.cascade.unit", "M" if "static_unit_m" in faults else "MM")
    status = reader.ReadFile(INPUT)
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise Refusal("render_failed", "read_status")
    reader.SetSystemLengthUnit(1.0)
    model = reader.Model()
    reasons = []
    for etype, message, is_fail in _check_lists(reader.WS().ModelCheckList(False)):
        if not load_check_allowed(etype, message):
            reasons.append("load_fail" if is_fail else "load_warn")
    if _left_handed_cto3d(model):
        reasons.append("unsupported_mirror_axes")
    refs = STEPConstruct_ExternRefs(reader.WS())
    refs.LoadExternRefs()
    if refs.NbExternRefs() > 0:  # FileName(i) stats the referenced path: never called
        reasons.append("external_reference")
    roots = reader.NbRootsForTransfer()
    if roots == 0:
        reasons.append("no_roots")
    for i in range(1, roots + 1):
        before = reader.NbShapes()
        if not reader.TransferRoot(i) or reader.NbShapes() - before < 1:
            reasons.append("root_not_transferred")
    for _etype, message, is_fail in _check_lists(reader.WS().TransferReader().TransientProcess().CheckList(False)):
        if is_fail:
            reasons.append("transfer_fail")
        elif not transfer_warning_allowed(message):
            reasons.append("transfer_warn")
    if reasons:
        result["reasons"] = [r for r in STRICT_ORDER if r in reasons]
        raise Refusal("render_failed", result["reasons"][0])
    shapes = [reader.Shape(k) for k in range(1, reader.NbShapes() + 1)]
    if any(s.IsNull() for s in shapes):
        raise Refusal("render_failed", "null_shape")
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)

    def faces(shape):
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            yield TopoDS.Face(explorer.Current())
            explorer.Next()

    face_count = sum(1 for _ in faces(compound))
    result["face_occurrences"] = face_count
    box = Bnd_Box()
    BRepBndLib.Add_s(compound, box, False)
    if box.IsVoid():
        raise Refusal("render_failed", "void_bbox")
    lo, hi, gap = box.CornerMin(), box.CornerMax(), box.GetGap()
    bounds = [[lo.X() + gap, lo.Y() + gap, lo.Z() + gap], [hi.X() - gap, hi.Y() - gap, hi.Z() - gap]]
    extent = [bounds[1][i] - bounds[0][i] for i in range(3)]
    if not all(math.isfinite(v) for row in bounds for v in row) or sum(1 for x in extent if x > 1e-9) < 2:
        raise Refusal("render_failed", "degenerate_bbox")
    if any(
        (box.IsOpenXmin(), box.IsOpenXmax(), box.IsOpenYmin(), box.IsOpenYmax(), box.IsOpenZmin(), box.IsOpenZmax())
    ) or any(abs(v) > MAX_ABS_MM for row in bounds for v in row):
        raise Refusal("render_failed", "implausible_bounds")
    result["bounds_mm"] = bounds
    if face_count > caps["face_cap"]:
        raise Refusal("resource_limit", "face_cap")
    diagonal = math.sqrt(sum(x * x for x in extent))
    if for_print:
        if diagonal > PRINT_MAX_EXTENT_MM:
            raise Refusal("resource_limit", "extent_cap")
        # topology alone says whether every face belongs to a body: asked before minutes of fine meshing
        bodies = in_bodies = 0
        body_explorer = TopExp_Explorer(compound, TopAbs_SOLID)
        while body_explorer.More():
            bodies += 1
            in_bodies += sum(1 for _ in faces(body_explorer.Current()))
            body_explorer.Next()
        early = print_refusal(faces=face_count, solid_faces=in_bodies, solids=bodies, negative=0)
        if early:
            raise Refusal("render_failed", early)

    _stage(result, "mesh")
    deflection = PRINT_DEFLECTION_MM if for_print else max(0.1, diagonal / 1000.0)
    result["deflection_mm"] = deflection
    mesher = BRepMesh_IncrementalMesh(compound, deflection, False, ANGULAR, False)
    flags = int(mesher.GetStatusFlags())
    result["mesh_status_flags"] = flags
    if flags:  # no warning flag is allowlisted (addendum: every measured non-zero flag meant a missing face)
        raise Refusal("render_failed", "mesh_flags")
    if "drop_triangulation" in faults:
        builder.UpdateFace(next(faces(compound)), None)
    count = missing = 0
    for face in faces(compound):
        tri = BRep_Tool.Triangulation_s(face, TopLoc_Location())
        if tri is None:
            missing += 1
        else:
            count += tri.NbTriangles()
    if missing:
        raise Refusal("render_failed", "missing_triangulation")
    if count == 0:
        raise Refusal("render_failed", "empty_mesh")
    if count > caps["triangle_cap"]:
        raise Refusal("resource_limit", "triangle_cap")  # before anything is gathered or written

    _stage(result, "mesh_write")
    solids = negative = solid_faces = 0
    total_volume = 0.0
    lo_m = [math.inf] * 3
    hi_m = [-math.inf] * 3
    digest = hashlib.sha256()
    written = 0

    def oriented(face):
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        trsf = loc.Transformation()
        flip = (face.Orientation() == TopAbs_REVERSED) != trsf.IsNegative()
        nodes = [tri.Node(i).Transformed(trsf) for i in range(1, tri.NbNodes() + 1)]
        for i in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(i).Get()
            if flip:
                b, c = c, b
            yield tuple((p.X(), p.Y(), p.Z()) for p in (nodes[a - 1], nodes[b - 1], nodes[c - 1]))

    def signed(v1, v2, v3):
        return (
            v1[0] * (v2[1] * v3[2] - v2[2] * v3[1])
            - v1[1] * (v2[0] * v3[2] - v2[2] * v3[0])
            + v1[2] * (v2[0] * v3[1] - v2[1] * v3[0])
        ) / 6.0

    solid_explorer = TopExp_Explorer(compound, TopAbs_SOLID)
    while solid_explorer.More():
        volume = 0.0
        for face in faces(solid_explorer.Current()):
            solid_faces += 1
            volume += sum(signed(*t) for t in oriented(face))
        solids += 1
        negative += volume < 0
        total_volume += abs(volume)
        solid_explorer.Next()
    if for_print:
        why = print_refusal(faces=face_count, solid_faces=solid_faces, solids=solids, negative=negative)
        if why:
            raise Refusal("render_failed", why)
    with open(OUTPUT + ".part", "wb") as out:
        header = b"BamDude STEP preview".ljust(80, b"\0") + struct.pack("<I", count)
        out.write(header)
        digest.update(header)
        for face in faces(compound):
            for v1, v2, v3 in oriented(face):
                for vertex in (v1, v2, v3):
                    for x in vertex:
                        if not math.isfinite(x) or abs(x) > F32_MAX:
                            raise Refusal("render_failed", "nonfinite")
                    for k in range(3):
                        lo_m[k] = min(lo_m[k], vertex[k])
                        hi_m[k] = max(hi_m[k], vertex[k])
                ux, uy, uz = (v2[0] - v1[0], v2[1] - v1[1], v2[2] - v1[2])
                wx, wy, wz = (v3[0] - v1[0], v3[1] - v1[1], v3[2] - v1[2])
                nx, ny, nz = uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx
                length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
                record = struct.pack("<12fH", nx / length, ny / length, nz / length, *v1, *v2, *v3, 0)
                out.write(record)
                digest.update(record)
                written += 1
                if "interrupt_write" in faults and written * 2 >= count:
                    raise OSError("interrupted write (test seam)")
    if written != count or os.path.getsize(OUTPUT + ".part") != 84 + 50 * count:
        raise Refusal("render_failed", "stl_size")
    _replace(OUTPUT + ".part", OUTPUT)
    result.update(
        outcome="ok",
        why=None,
        triangles=count,
        stl_bytes=84 + 50 * count,
        stl_sha256=digest.hexdigest(),
        solids=solids,
        negative_volume_solids=negative,
        abs_volume_mm3=total_volume,
        bounds_mesh_mm=[lo_m, hi_m],
    )


def _classify(exc: BaseException) -> tuple[str, str]:
    """A refusal says its own outcome; running out of memory — Python's MemoryError or OCCT's Standard_OutOfMemory,
    an ordinary exception class in OCP — is resource_limit (spec §6); anything else is render_failed by type."""
    if isinstance(exc, Refusal):
        return exc.outcome, exc.why
    if isinstance(exc, MemoryError) or any(c.__name__ == "Standard_OutOfMemory" for c in type(exc).__mro__):
        return "resource_limit", "memory_error"
    return "render_failed", type(exc).__name__[:64]


def main() -> int:
    started = time.monotonic()
    request = json.loads(sys.stdin.buffer.readline(JSON_BYTES + 1))
    os.chdir(request["root"])  # private cwd before OCP: no XSTEP.us/SHAPE.us from the install directory
    _windows_error_mode()
    _set_data_limit(MODES.get(request.get("mode", "thumbnail"), MODES["thumbnail"])[3])
    result = {"pid": os.getpid(), "outcome": "render_failed", "why": "unknown", "stage": "snapshot"}
    try:
        if time.monotonic_ns() >= int(request["deadline"]):
            raise Refusal("timeout", "deadline")
        convert(request, result)
    except Exception as exc:  # MemoryError included; bounded and closed: no message text (it can carry paths)
        _remove_own_outputs()
        outcome, why = _classify(exc)
        result.update(outcome=outcome, why=why)
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    _write_result(result)
    return 0 if result["outcome"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
