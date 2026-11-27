"""Curved fixtures written by the OCCT writer (S3: OCCT may CREATE fixtures, never be their oracle).

python -I make_occt_fixtures.py <OCP target> <out dir>
Expectations come from construction parameters; the Part 21 text is checked for the face count and units.
"""

import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere  # noqa: E402
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf  # noqa: E402
from OCP.IFSelect import IFSelect_ReturnStatus  # noqa: E402
from OCP.Interface import Interface_Static  # noqa: E402
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer  # noqa: E402
from OCP.TopLoc import TopLoc_Location  # noqa: E402

out = Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
expected = {}


def write(name, shape, exp, unit="MM"):
    w = STEPControl_Writer()
    Interface_Static.SetCVal_s("write.step.unit", unit)
    w.Transfer(shape, STEPControl_AsIs)
    path = out / name
    assert w.Write(str(path)) == IFSelect_ReturnStatus.IFSelect_RetDone
    text = path.read_text("ascii", "replace")
    exp = dict(exp)
    exp["text_counts"] = {
        k: text.count(k)
        for k in (
            "ADVANCED_FACE(",
            "MAPPED_ITEM(",
            "CARTESIAN_TRANSFORMATION_OPERATOR_3D(",
            "CYLINDRICAL_SURFACE(",
            "SPHERICAL_SURFACE(",
        )
    }
    data = path.read_bytes()
    exp.update(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), writer="OCCT 8.0.1 STEPControl_Writer")
    expected[name] = exp


# Plate 60 x 40 x 8 with two through holes d=10 at (15,20) and (45,20).
plate = BRepPrimAPI_MakeBox(60.0, 40.0, 8.0).Shape()
for cx in (15.0, 45.0):
    cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(cx, 20.0, -1.0), gp_Dir(0, 0, 1)), 5.0, 10.0).Shape()
    plate = BRepAlgoAPI_Cut(plate, cyl).Shape()
plate_vol = 60 * 40 * 8 - 2 * math.pi * 25 * 8
plate_area = 2 * (60 * 40 - 2 * math.pi * 25) + 2 * (60 * 8 + 40 * 8) + 2 * (2 * math.pi * 5 * 8)
write(
    "plate_holes.step",
    plate,
    {
        "kind": "ok",
        "bounds_mm": [[0, 0, 0], [60, 40, 8]],
        "abs_volume_mm3": plate_vol,
        "area_mm2": plate_area,
        "solids": 1,
        "construction": "box 60x40x8 minus 2 cylinders r5 through",
    },
)

# The same plate placed with a mirror location (negative gp_Trsf) — how does the writer carry it?
mirror = gp_Trsf()
mirror.SetMirror(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)))  # z -> -z
mirrored = plate.Moved(TopLoc_Location(mirror))
write(
    "plate_holes_mirror_loc.step",
    mirrored,
    {
        "kind": "ok",
        "bounds_mm": [[0, 0, -8], [60, 40, 0]],
        "abs_volume_mm3": plate_vol,
        "area_mm2": plate_area,
        "solids": 1,
        "construction": "plate.Moved(mirror z) — negative location",
    },
)

# Spheres: printable r=50 and the synthetic 1 km scale regression.
for name, rad in (("sphere_r50.step", 50.0), ("sphere_r1km.step", 1.0e6)):
    sph = BRepPrimAPI_MakeSphere(rad).Shape()
    write(
        name,
        sph,
        {
            "kind": "ok",
            "bounds_mm": [[-rad] * 3, [rad] * 3],
            "abs_volume_mm3": 4 / 3 * math.pi * rad**3,
            "area_mm2": 4 * math.pi * rad**2,
            "solids": 1,
            "construction": f"sphere r={rad} mm",
        },
    )

# Inch twin of the plate (writer unit INCH) — same physical geometry, bounds in mm.
write(
    "plate_holes_inch.step",
    plate,
    {
        "kind": "ok",
        "bounds_mm": [[0, 0, 0], [60, 40, 8]],
        "abs_volume_mm3": plate_vol,
        "area_mm2": plate_area,
        "solids": 1,
        "construction": "plate, written in INCH",
    },
    unit="INCH",
)
(out / "expected.json").write_text(json.dumps(expected, indent=1, sort_keys=True))
print(json.dumps({k: v["text_counts"] for k, v in expected.items()}, indent=1))
