"""Build the stage-0 authored fixtures and their independent expectations (S3).

Run: python -I make_fixtures.py <out_dir>
Writes <out_dir>/*.step and <out_dir>/expected.json. No OCCT import here.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p21  # noqa: E402

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
expected = {}


def save(name, text_or_bytes, exp):
    data = text_or_bytes.encode("ascii") if isinstance(text_or_bytes, str) else text_or_bytes
    (out / name).write_bytes(data)
    exp = dict(exp)
    exp["sha256"] = hashlib.sha256(data).hexdigest()
    exp["bytes"] = len(data)
    expected[name] = exp


def r(b):
    return [[round(v, 6) for v in row] for row in b]


L = p21.l_prism()
LV = abs(L.volume())
assert math.isclose(LV, (40 * 10 + 10 * 20) * 12)
BOX_IN = p21.box(50.8, 38.1, 25.4)  # 2 x 1.5 x 1 inch, expressed in mm

# 1-2. Unit twins: the same physical box written in mm and in inch.
for unit in ("mm", "inch"):
    w = p21.Writer(unit=unit)
    rep, ax = w.brep_rep([w.solid(BOX_IN, "box")])
    w.product("box", "box", rep)
    save(
        f"box_{unit}.step",
        w.text(f"box_{unit}"),
        {
            "kind": "ok",
            "unit": unit,
            "bounds_mm": r(BOX_IN.bounds()),
            "face_occurrences": 6,
            "solids": 1,
            "abs_volume_mm3": abs(BOX_IN.volume()),
            "instances": 1,
            "negative_instances": 0,
            "text_checks": {"CONVERSION_BASED_UNIT('INCH'": 1 if unit == "inch" else 0, "ADVANCED_FACE(": 6},
        },
    )

# 3. Non-convex single part.
w = p21.Writer()
rep, ax = w.brep_rep([w.solid(L, "L")])
w.product("L", "L", rep)
save(
    "l_prism.step",
    w.text("l_prism"),
    {
        "kind": "ok",
        "unit": "mm",
        "bounds_mm": r(L.bounds()),
        "face_occurrences": 8,
        "solids": 1,
        "abs_volume_mm3": LV,
        "instances": 1,
        "negative_instances": 0,
        "text_checks": {"ADVANCED_FACE(": 8},
    },
)

# 4. Assembly: repeated + rotated instances of one part (NAUO placements are always proper rotations).
placements = [(p21.IDENTITY, (0.0, 0.0, 0.0)), (p21.IDENTITY, (100.0, 0.0, 0.0)), (p21.rot_z(90), (0.0, 100.0, 0.0))]
w = p21.Writer()
prep, pax = w.brep_rep([w.solid(L, "L")])
ppd = w.product("L", "L", prep)
w.assembly("asm", "asm", [(ppd, prep, pax, m, t) for m, t in placements])
bounds = p21.union_bounds([p21.transformed_bounds(L, m, t) for m, t in placements])
save(
    "asm_instances.step",
    w.text("asm_instances"),
    {
        "kind": "ok",
        "unit": "mm",
        "bounds_mm": r(bounds),
        "face_occurrences": 24,
        "solids": 3,
        "abs_volume_mm3": 3 * LV,
        "instances": 3,
        "negative_instances": 0,
        "text_checks": {"ADVANCED_FACE(": 8, "NEXT_ASSEMBLY_USAGE_OCCURRENCE(": 3, "ITEM_DEFINED_TRANSFORMATION(": 3},
    },
)

# 5-7. Mirrors via MAPPED_ITEM + CARTESIAN_TRANSFORMATION_OPERATOR_3D (the only AP214 way to mirror).
MIRRORS = {
    "mirror_point_scale": {
        "axes": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
        "scale": -1.0,
        "m": ((-1, 0, 0), (0, -1, 0), (0, 0, -1)),
    },
    "mirror_z_axis3": {
        "axes": ((1, 0, 0), (0, 1, 0), (0, 0, -1)),
        "scale": 1.0,
        "m": ((1, 0, 0), (0, 1, 0), (0, 0, -1)),
    },
    "mirror_x_axis1": {
        "axes": ((-1, 0, 0), (0, 1, 0), (0, 0, 1)),
        "scale": 1.0,
        "m": ((-1, 0, 0), (0, 1, 0), (0, 0, 1)),
    },
}
for name, spec in MIRRORS.items():
    w = p21.Writer()
    src_rep, src_ax = w.brep_rep([w.solid(L, "L")])
    a1, a2, a3 = spec["axes"]
    hrep, hax = w.mapped(src_rep, src_ax, a1, a2, a3, (0, 0, 0), spec["scale"])
    w.product(name, name, hrep)
    m = spec["m"]
    assert p21.det(m) < 0
    # Stage-0 finding: OCCT honours a negative scale but rebuilds a right-handed frame from left-handed
    # axes (a silent 180-degree rotation). Those files must be refused as an unsupported mirror form.
    kind = "ok" if spec["scale"] < 0 else "reject"
    save(
        f"{name}.step",
        w.text(name),
        {
            "kind": kind,
            "unit": "mm",
            "bounds_mm": r(p21.transformed_bounds(L, m, (0, 0, 0))),
            "face_occurrences": 8,
            "solids": 1,
            "abs_volume_mm3": LV,
            "instances": 1,
            "negative_instances": 1,
            "why": None if kind == "ok" else "left-handed CTO axes: OCCT applies a rotation instead of the mirror",
            "text_checks": {"MAPPED_ITEM(": 1, "CARTESIAN_TRANSFORMATION_OPERATOR_3D(": 1},
        },
    )

# 7b. Faces written with same_sense .F. (REVERSED in OCCT), plain and under a scale -1 mirror (REVERSED XOR negative).
for name, mirrored in (("l_prism_reversed_faces", False), ("mirror_point_scale_reversed_faces", True)):
    w = p21.Writer()
    w.reverse_odd_faces = True
    src_rep, src_ax = w.brep_rep([w.solid(L, "L")])
    w.reverse_odd_faces = False
    if mirrored:
        hrep, _ = w.mapped(src_rep, src_ax, (1, 0, 0), (0, 1, 0), (0, 0, 1), (0, 0, 0), -1.0)
        w.product(name, name, hrep)
        m = ((-1, 0, 0), (0, -1, 0), (0, 0, -1))
    else:
        w.product(name, name, src_rep)
        m = p21.IDENTITY
    save(
        f"{name}.step",
        w.text(name),
        {
            "kind": "ok",
            "unit": "mm",
            "bounds_mm": r(p21.transformed_bounds(L, m, (0, 0, 0))),
            "face_occurrences": 8,
            "solids": 1,
            "abs_volume_mm3": LV,
            "instances": 1,
            "negative_instances": 1 if mirrored else 0,
            "text_checks": {"ADVANCED_FACE(": 8},
        },
    )
    reversed_faces = sum(
        1 for line in w.text(name).splitlines() if "= ADVANCED_FACE(" in line and line.endswith(",.F.);")
    )
    assert reversed_faces == 4, (name, reversed_faces)
    expected[f"{name}.step"]["advanced_faces_same_sense_false"] = reversed_faces

# 8. Assembly with a plain instance and a mirrored (mapped-item holder) instance: both mirror forms.
for name, axes, scale, m, kind in (
    ("asm_mirror_axes", ((1, 0, 0), (0, 1, 0), (0, 0, -1)), 1.0, ((1, 0, 0), (0, 1, 0), (0, 0, -1)), "reject"),
    ("asm_mirror_scale", ((1, 0, 0), (0, 1, 0), (0, 0, 1)), -1.0, ((-1, 0, 0), (0, -1, 0), (0, 0, -1)), "ok"),
):
    w = p21.Writer()
    prep, pax = w.brep_rep([w.solid(L, "L")])
    ppd = w.product("L", "L", prep)
    hrep, hax = w.mapped(prep, pax, *axes, (0, 0, 0), scale)
    hpd = w.product("Lm", "L mirrored", hrep)
    w.assembly(
        "asm",
        "asm",
        [(ppd, prep, pax, p21.IDENTITY, (0.0, 0.0, 0.0)), (hpd, hrep, hax, p21.IDENTITY, (0.0, 0.0, 50.0))],
    )
    bounds = p21.union_bounds([L.bounds(), p21.transformed_bounds(L, m, (0, 0, 50.0))])
    save(
        f"{name}.step",
        w.text(name),
        {
            "kind": kind,
            "unit": "mm",
            "bounds_mm": r(bounds),
            "face_occurrences": 16,
            "solids": 2,
            "abs_volume_mm3": 2 * LV,
            "instances": 2,
            "negative_instances": 1,
            "why": None if kind == "ok" else "left-handed CTO axes in an assembly",
            "text_checks": {"NEXT_ASSEMBLY_USAGE_OCCURRENCE(": 2, "MAPPED_ITEM(": 1},
        },
    )

# 9-11. Empty inputs.
save("empty_0bytes.step", b"", {"kind": "reject", "why": "empty file"})
save(
    "empty_data.step",
    p21.Writer().text("empty").replace("\n".join(p21.Writer().lines), ""),
    {"kind": "reject", "why": "header + empty DATA (only contexts) — no product"},
)
w = p21.Writer()
w.product("nogeo", "no geometry", None)
save("nongeometric.step", w.text("nongeometric"), {"kind": "reject", "why": "product without any shape representation"})


# 12. Two independent roots (for truncation and null-root cases).
def two_roots(break_second=None, second_face_break=None, defect="zero_normal"):
    w = p21.Writer()
    rep1, _ = w.brep_rep([w.solid(L, "L")])
    w.product("A", "A", rep1)
    marker = len(w.lines)
    solid2 = w.solid(BOX_IN, "box", break_face=second_face_break, defect=defect)
    rep2, _ = w.brep_rep([solid2])
    if break_second == "dangling":
        w.product("B", "B", None)
        pds = w.n  # PRODUCT_DEFINITION_SHAPE is the last line product() wrote
        w.add(f"SHAPE_DEFINITION_REPRESENTATION(#{pds},#999999)")
    elif break_second == "wrong_type":
        w.product("B", "B", None)
        pds = w.n
        bad = w.point((1, 2, 3))
        w.add(f"SHAPE_DEFINITION_REPRESENTATION(#{pds},#{bad})")
    else:
        w.product("B", "B", rep2)
    return w, marker


w, marker = two_roots()
two_ok_text = w.text("two_roots")
both_bounds = p21.union_bounds([L.bounds(), BOX_IN.bounds()])
save(
    "two_roots.step",
    two_ok_text,
    {
        "kind": "ok",
        "unit": "mm",
        "bounds_mm": r(both_bounds),
        "face_occurrences": 14,
        "solids": 2,
        "abs_volume_mm3": LV + abs(BOX_IN.volume()),
        "instances": 2,
        "negative_instances": 0,
        "text_checks": {"PRODUCT(": 2, "ADVANCED_FACE(": 14},
    },
)
data = two_ok_text.encode("ascii")
for frac in (0.25, 0.5, 0.9):
    save(
        f"trunc_{int(frac * 100)}.step",
        data[: int(len(data) * frac)],
        {"kind": "reject", "why": f"raw cut at {frac:.0%}"},
    )
# cut right after the first root's complete SHAPE_DEFINITION_REPRESENTATION line, no trailer
first_sdr = two_ok_text.index("SHAPE_DEFINITION_REPRESENTATION(")
cut = two_ok_text.index("\n", first_sdr) + 1
save(
    "trunc_after_root1.step",
    data[:cut],
    {"kind": "reject", "why": "cut after a complete first root, no ENDSEC/trailer"},
)
save(
    "trunc_no_end_marker.step",
    two_ok_text.replace("END-ISO-10303-21;\n", "").encode("ascii"),
    {"kind": "reject", "why": "missing END-ISO-10303-21 trailer only"},
)

w, _ = two_roots(break_second="dangling")
save(
    "null_root_dangling.step",
    w.text("null_root_dangling"),
    {
        "kind": "reject",
        "why": "root B's shape definition points at a missing entity (#999999)",
        "good_root_bounds_mm": r(L.bounds()),
    },
)
w, _ = two_roots(break_second="wrong_type")
save(
    "null_root_wrong_type.step",
    w.text("null_root_wrong_type"),
    {
        "kind": "reject",
        "why": "root B's shape definition points at a CARTESIAN_POINT",
        "good_root_bounds_mm": r(L.bounds()),
    },
)
for defect in ("zero_normal", "bspline_bad", "edge_zero_dir", "dangling_bound"):
    w, _ = two_roots(second_face_break=2, defect=defect)
    save(
        f"face_{defect}.step",
        w.text(f"face_{defect}"),
        {"kind": "reject", "why": f"one ADVANCED_FACE of root B carries defect {defect}", "text_faces": 14},
    )

# 13. Mixed embedded + external: an embedded instance and an external-only component.
w = p21.Writer()
prep, pax = w.brep_rep([w.solid(L, "L")])
ppd = w.product("L", "L", prep)
ext_ax = w.axis()
ext_rep = w.add(f"SHAPE_REPRESENTATION('',(#{ext_ax}),#{w.ctx})")
epd = w.product("EXT", "external part", ext_rep)
w.external_reference(epd, "external-part.stp")
w.assembly(
    "asm",
    "asm",
    [(ppd, prep, pax, p21.IDENTITY, (0.0, 0.0, 0.0)), (epd, ext_rep, ext_ax, p21.IDENTITY, (200.0, 0.0, 0.0))],
)
save(
    "mixed_embedded_external.step",
    w.text("mixed_embedded_external"),
    {
        "kind": "reject",
        "why": "one component's geometry lives in external-part.stp",
        "embedded_bounds_mm": r(L.bounds()),
        "text_checks": {"DOCUMENT_FILE(": 1, "NEXT_ASSEMBLY_USAGE_OCCURRENCE(": 2},
    },
)
# The external file itself (a valid single part) so a CAF positive control can resolve it.
w = p21.Writer()
rep, _ = w.brep_rep([w.solid(BOX_IN, "box")])
w.product("EXT", "external part", rep)
save("external-part.stp", w.text("external-part"), {"kind": "ok-auxiliary", "bounds_mm": r(BOX_IN.bounds())})

# 14. External-only root (OCCT multi-file pattern).
w = p21.Writer()
ext_ax = w.axis()
ext_rep = w.add(f"SHAPE_REPRESENTATION('',(#{ext_ax}),#{w.ctx})")
epd = w.product("EXT", "external part", ext_rep)
w.external_reference(epd, "external-part.stp")
save(
    "external_only.step", w.text("external_only"), {"kind": "reject", "why": "root geometry only by external reference"}
)

# 15. A float32 overflow: finite doubles in the file, out of float32 range in mm (2e37 inch).
w = p21.Writer(unit="inch")
rep, _ = w.brep_rep([w.solid(p21.box(5.08e38, 1.0, 1.0), "huge")])
w.product("huge", "huge", rep)
save("box_float32_overflow.step", w.text("huge"), {"kind": "reject", "why": "x extent 5.08e38 mm exceeds float32"})

(out / "expected.json").write_text(json.dumps(expected, indent=1, sort_keys=True))
for k, v in expected.items():
    tc = v.get("text_checks") or {}
    if tc:
        txt = (out / k).read_text("ascii")
        for needle, count in tc.items():
            got = txt.count(needle)
            if got != count:
                raise SystemExit(f"{k}: text check {needle!r} expected {count}, got {got}")
print(json.dumps({k: v["kind"] for k, v in expected.items()}, indent=0))
