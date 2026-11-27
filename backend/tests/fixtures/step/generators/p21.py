"""Minimal ISO 10303-21 (AP214) writer for stage-0 fixtures.

Independent of OCCT: every expectation (bounds, face occurrences, volume, instance count,
units) is computed here from construction parameters, never from an OCCT read (S3).
Only planar polyhedral solids; curved fixtures come from the OCCT writer separately and are
checked against construction parameters plus the Part 21 text.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def fmt(x: float) -> str:
    x = float(x)
    if x == 0:
        return "0."
    s = repr(x)
    if "e" in s or "E" in s:
        mant, exp = s.lower().split("e")
        if "." not in mant:
            mant += "."
        return f"{mant}E{int(exp)}"
    if "." not in s:
        s += "."
    return s


def vec_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def norm(v):
    n = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    if n == 0:
        raise ValueError("zero vector")
    return (v[0] / n, v[1] / n, v[2] / n)


def newell(points):
    nx = ny = nz = 0.0
    for i, p in enumerate(points):
        q = points[(i + 1) % len(points)]
        nx += (p[1] - q[1]) * (p[2] + q[2])
        ny += (p[2] - q[2]) * (p[0] + q[0])
        nz += (p[0] - q[0]) * (p[1] + q[1])
    return norm((nx, ny, nz))


@dataclass
class Polyhedron:
    vertices: list
    faces: list  # vertex index loops, counter-clockwise seen from outside

    def volume(self) -> float:
        vol = 0.0
        for f in self.faces:
            p0 = self.vertices[f[0]]
            for i in range(1, len(f) - 1):
                p1, p2 = self.vertices[f[i]], self.vertices[f[i + 1]]
                vol += (
                    p0[0] * (p1[1] * p2[2] - p1[2] * p2[1])
                    - p0[1] * (p1[0] * p2[2] - p1[2] * p2[0])
                    + p0[2] * (p1[0] * p2[1] - p1[1] * p2[0])
                ) / 6.0
        return vol

    def bounds(self):
        xs, ys, zs = zip(*self.vertices, strict=True)
        return [[min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]]


def box(dx, dy, dz, origin=(0.0, 0.0, 0.0)) -> Polyhedron:
    ox, oy, oz = origin
    v = [
        (ox, oy, oz),
        (ox + dx, oy, oz),
        (ox + dx, oy + dy, oz),
        (ox, oy + dy, oz),
        (ox, oy, oz + dz),
        (ox + dx, oy, oz + dz),
        (ox + dx, oy + dy, oz + dz),
        (ox, oy + dy, oz + dz),
    ]
    f = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]]
    return Polyhedron(v, f)


def prism(outline_ccw, height) -> Polyhedron:
    """Extrude a CCW (seen from +Z) planar outline along +Z."""
    n = len(outline_ccw)
    v = [(x, y, 0.0) for x, y in outline_ccw] + [(x, y, float(height)) for x, y in outline_ccw]
    bottom = list(reversed(range(n)))
    top = [n + i for i in range(n)]
    sides = [[i, (i + 1) % n, n + (i + 1) % n, n + i] for i in range(n)]
    return Polyhedron(v, [bottom, top, *sides])


def l_prism() -> Polyhedron:
    # Non-convex, not symmetric in X/Y: 40 x 30 L, 10 thick legs, 12 high.
    return prism([(0, 0), (40, 0), (40, 10), (10, 10), (10, 30), (0, 30)], 12)


# --- rigid transforms (row-major 3x3 + translation) -------------------------------------------


def rot_z(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))


IDENTITY = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def apply(m, t, p):
    return (
        m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + t[0],
        m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + t[1],
        m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + t[2],
    )


def det(m):
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def transformed_bounds(poly: Polyhedron, m, t):
    pts = [apply(m, t, p) for p in poly.vertices]
    xs, ys, zs = zip(*pts, strict=True)
    return [[min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)]]


def union_bounds(bs):
    lo = [min(b[0][i] for b in bs) for i in range(3)]
    hi = [max(b[1][i] for b in bs) for i in range(3)]
    return [lo, hi]


# --- writer -------------------------------------------------------------------------------------


@dataclass
class Writer:
    unit: str = "mm"  # mm | inch
    lines: list = field(default_factory=list)
    n: int = 0
    scale_to_file: float = 1.0  # file number = mm value * scale_to_file

    def __post_init__(self):
        if self.unit == "inch":
            self.scale_to_file = 1.0 / 25.4
        self.app = self.add("APPLICATION_CONTEXT('core data for automotive mechanical design processes')")
        self.add(f"APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,#{self.app})")
        self.prod_ctx = self.add(f"PRODUCT_CONTEXT('',#{self.app},'mechanical')")
        self.pd_ctx = self.add(f"PRODUCT_DEFINITION_CONTEXT('part definition',#{self.app},'design')")
        mm = self.add("( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
        if self.unit == "inch":
            dim = self.add("DIMENSIONAL_EXPONENTS(1.,0.,0.,0.,0.,0.,0.)")
            lmwu = self.add(f"LENGTH_MEASURE_WITH_UNIT(LENGTH_MEASURE(25.4),#{mm})")
            length = self.add(f"( CONVERSION_BASED_UNIT('INCH',#{lmwu}) LENGTH_UNIT() NAMED_UNIT(#{dim}) )")
        else:
            length = mm
        rad = self.add("( NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.) )")
        sr = self.add("( NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT() )")
        unc = self.add(
            f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),#{length},'distance_accuracy_value','confusion accuracy')"
        )
        self.ctx = self.add(
            f"( GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{unc})) "
            f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{length},#{rad},#{sr})) "
            f"REPRESENTATION_CONTEXT('Context #1','3D Context with UNIT and UNCERTAINTY') )"
        )

    def add(self, text: str) -> int:
        self.n += 1
        self.lines.append(f"#{self.n} = {text};")
        return self.n

    def raw(self, text: str):
        self.lines.append(text)

    # geometry
    def point(self, p):
        s = self.scale_to_file
        return self.add(f"CARTESIAN_POINT('',({fmt(p[0] * s)},{fmt(p[1] * s)},{fmt(p[2] * s)}))")

    def direction(self, d):
        return self.add(f"DIRECTION('',({fmt(d[0])},{fmt(d[1])},{fmt(d[2])}))")

    def axis(self, origin=(0, 0, 0), z=(0, 0, 1), x=(1, 0, 0)):
        return self.add(f"AXIS2_PLACEMENT_3D('',#{self.point(origin)},#{self.direction(z)},#{self.direction(x)})")

    def solid(self, poly: Polyhedron, name="solid", break_face: int | None = None, defect: str = "zero_normal") -> int:
        """break_face + defect inject ONE internal defect that still parses:
        zero_normal (plane axis with a zero direction), bspline_bad (bilinear B-spline with invalid knot
        multiplicities instead of the plane), edge_zero_dir (the face's first new edge has a zero line
        direction), dangling_bound (face bound points at a missing entity — a load failure)."""
        vp = [self.add(f"VERTEX_POINT('',#{self.point(p)})") for p in poly.vertices]
        edges = {}
        face_ids = []
        for fi, loop in enumerate(poly.faces):
            oes = []
            for i, a in enumerate(loop):
                b = loop[(i + 1) % len(loop)]
                key = (min(a, b), max(a, b))
                if key not in edges:
                    pa, pb = poly.vertices[key[0]], poly.vertices[key[1]]
                    d = vec_sub(pb, pa)
                    length = math.sqrt(sum(c * c for c in d)) * self.scale_to_file
                    zero_dir = break_face == fi and defect == "edge_zero_dir" and not getattr(self, "_zero_done", False)
                    if zero_dir:
                        self._zero_done = True
                    vec = self.add(f"VECTOR('',#{self.direction((0, 0, 0) if zero_dir else norm(d))},{fmt(length)})")
                    line = self.add(f"LINE('',#{self.point(pa)},#{vec})")
                    edges[key] = self.add(f"EDGE_CURVE('',#{vp[key[0]]},#{vp[key[1]]},#{line},.T.)")
                orient = ".T." if (a, b) == key else ".F."
                oes.append(self.add(f"ORIENTED_EDGE('',*,*,#{edges[key]},{orient})"))
            loop_id = self.add(f"EDGE_LOOP('',({','.join(f'#{o}' for o in oes)}))")
            bound = self.add(f"FACE_OUTER_BOUND('',#{loop_id},.T.)")
            pts = [poly.vertices[i] for i in loop]
            n = newell(pts)
            ref = norm(vec_sub(pts[1], pts[0]))
            broken = break_face == fi
            if broken and defect == "zero_normal":
                plane_axis = self.add(
                    f"AXIS2_PLACEMENT_3D('',#{self.point(pts[0])},#{self.direction((0, 0, 0))},#{self.direction(ref)})"
                )
            else:
                plane_axis = self.add(
                    f"AXIS2_PLACEMENT_3D('',#{self.point(pts[0])},#{self.direction(n)},#{self.direction(ref)})"
                )
            if broken and defect == "bspline_bad" and len(pts) == 4:
                p = [self.point(q) for q in pts]
                surface = self.add(
                    f"B_SPLINE_SURFACE_WITH_KNOTS('',1,1,((#{p[0]},#{p[3]}),(#{p[1]},#{p[2]})),.UNSPECIFIED.,.F.,.F.,.F.,"
                    "(1,1),(1,1),(0.,1.),(0.,1.),.UNSPECIFIED.)"
                )
            elif getattr(self, "reverse_odd_faces", False) and fi % 2 == 1:
                # Same face, plane normal written inward and same_sense .F. -> OCCT face orientation REVERSED.
                inward = self.add(
                    f"AXIS2_PLACEMENT_3D('',#{self.point(pts[0])},#{self.direction((-n[0], -n[1], -n[2]))},#{self.direction(ref)})"
                )
                surface = self.add(f"PLANE('',#{inward})")
                face_ids.append(self.add(f"ADVANCED_FACE('',(#{bound}),#{surface},.F.)"))
                continue
            else:
                surface = self.add(f"PLANE('',#{plane_axis})")
            bound_ref = "#999999" if broken and defect == "dangling_bound" else f"#{bound}"
            face_ids.append(self.add(f"ADVANCED_FACE('',({bound_ref}),#{surface},.T.)"))
        shell = self.add(f"CLOSED_SHELL('',({','.join(f'#{f}' for f in face_ids)}))")
        return self.add(f"MANIFOLD_SOLID_BREP('{name}',#{shell})")

    def brep_rep(self, items, origin_axis=None):
        origin_axis = origin_axis or self.axis()
        ids = ",".join(f"#{i}" for i in [origin_axis, *items])
        rep = self.add(f"ADVANCED_BREP_SHAPE_REPRESENTATION('',({ids}),#{self.ctx})")
        return rep, origin_axis

    def product(self, pid, name, rep):
        prod = self.add(f"PRODUCT('{pid}','{name}','',(#{self.prod_ctx}))")
        self.add(f"PRODUCT_RELATED_PRODUCT_CATEGORY('part',$,(#{prod}))")
        pdf = self.add(f"PRODUCT_DEFINITION_FORMATION('','',#{prod})")
        pd = self.add(f"PRODUCT_DEFINITION('design','',#{pdf},#{self.pd_ctx})")
        pds = self.add(f"PRODUCT_DEFINITION_SHAPE('','',#{pd})")
        if rep is not None:
            self.add(f"SHAPE_DEFINITION_REPRESENTATION(#{pds},#{rep})")
        return pd

    def assembly(self, pid, name, children):
        """children: list of (part_pd, part_rep, part_origin_axis, rotation matrix, translation)."""
        asm_origin = self.axis()
        placements = []
        for _pd, _rep, _ax, m, t in children:
            z = (m[0][2], m[1][2], m[2][2])
            x = (m[0][0], m[1][0], m[2][0])
            placements.append(self.axis(t, z, x))
        items = ",".join(f"#{i}" for i in [asm_origin, *placements])
        asm_rep = self.add(f"SHAPE_REPRESENTATION('',({items}),#{self.ctx})")
        asm_pd = self.product(pid, name, asm_rep)
        for k, ((part_pd, part_rep, part_axis, _m, _t), place) in enumerate(zip(children, placements, strict=True), 1):
            nauo = self.add(f"NEXT_ASSEMBLY_USAGE_OCCURRENCE('{pid}-{k}','instance {k}','',#{asm_pd},#{part_pd},$)")
            pds = self.add(f"PRODUCT_DEFINITION_SHAPE('','',#{nauo})")
            idt = self.add(f"ITEM_DEFINED_TRANSFORMATION('','',#{part_axis},#{place})")
            rrwt = self.add(
                f"( REPRESENTATION_RELATIONSHIP('','',#{part_rep},#{asm_rep}) "
                f"REPRESENTATION_RELATIONSHIP_WITH_TRANSFORMATION(#{idt}) SHAPE_REPRESENTATION_RELATIONSHIP() )"
            )
            self.add(f"CONTEXT_DEPENDENT_SHAPE_REPRESENTATION(#{rrwt},#{pds})")
        return asm_pd, asm_rep, asm_origin

    def mapped(self, src_rep, src_origin_axis, axis1, axis2, axis3, origin, scale):
        rm = self.add(f"REPRESENTATION_MAP(#{src_origin_axis},#{src_rep})")
        a1 = f"#{self.direction(axis1)}" if axis1 else "$"
        a2 = f"#{self.direction(axis2)}" if axis2 else "$"
        a3 = f"#{self.direction(axis3)}" if axis3 else "$"
        sc = fmt(scale) if scale is not None else "$"
        # 8 parameters: representation_item.name, functionally_defined_transformation.(name, description),
        # axis1, axis2, local_origin, scale, axis3 (OCCT refuses 7 with "Count of Parameters is not 8").
        cto = self.add(f"CARTESIAN_TRANSFORMATION_OPERATOR_3D('','','',{a1},{a2},#{self.point(origin)},{sc},{a3})")
        mi = self.add(f"MAPPED_ITEM('',#{rm},#{cto})")
        holder_axis = self.axis()
        rep = self.add(f"SHAPE_REPRESENTATION('',(#{holder_axis},#{mi}),#{self.ctx})")
        return rep, holder_axis

    def external_reference(self, pd, filename):
        """OCCT's own pattern (STEPCAFControl_Writer multi-file) for an external document on `pd`."""
        doc_type = self.add("DOCUMENT_TYPE('configuration controlled document version')")
        doc = self.add(f"DOCUMENT('','',$,#{doc_type})")
        app2 = self.add("APPLICATION_CONTEXT('')")
        pctx = self.add(f"PRODUCT_CONTEXT('',#{app2},'')")
        dprod = self.add(f"PRODUCT('20001','{filename}-Doc','',(#{pctx}))")
        pdf = self.add(f"PRODUCT_DEFINITION_FORMATION('1','',#{dprod})")
        self.add(f"DOCUMENT_PRODUCT_EQUIVALENCE('equivalence',$,#{doc},#{pdf})")
        dctx = self.add(f"PRODUCT_DEFINITION_CONTEXT('digital document definition',#{app2},'')")
        ftype = self.add("DOCUMENT_TYPE('')")
        dfile = self.add(f"DOCUMENT_FILE('{filename}','',$,#{ftype},'',$)")
        self.add(f"PRODUCT_DEFINITION_WITH_ASSOCIATED_DOCUMENTS('1','',#{pdf},#{dctx},(#{dfile}))")
        role = self.add("IDENTIFICATION_ROLE('external document id and location',$)")
        src = self.add(f"EXTERNAL_SOURCE(IDENTIFIER('{filename}'))")
        self.add(f"APPLIED_EXTERNAL_IDENTIFICATION_ASSIGNMENT('{filename}',#{role},#{src},(#{dfile}))")
        orole = self.add("OBJECT_ROLE('mandatory',$)")
        adr = self.add(f"APPLIED_DOCUMENT_REFERENCE(#{doc},'',(#{pd}))")
        self.add(f"ROLE_ASSOCIATION(#{orole},#{adr})")
        pdef = self.add(f"PROPERTY_DEFINITION('document property','',#{dfile})")
        item = self.add("DESCRIPTIVE_REPRESENTATION_ITEM('data format','STEP AP214')")
        rctx = self.add("REPRESENTATION_CONTEXT('','document parameters')")
        rep = self.add(f"REPRESENTATION('document format',(#{item}),#{rctx})")
        self.add(f"PROPERTY_DEFINITION_REPRESENTATION(#{pdef},#{rep})")
        self.add(f"DOCUMENT_REPRESENTATION_TYPE('digital',#{dfile})")

    def text(self, name="stage0 fixture") -> str:
        head = (
            "ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('BamDude stage-0 fixture'),'2;1');\n"
            f"FILE_NAME('{name}','2026-10-16T00:00:00',('BamDude'),('BamDude'),'p21.py','p21.py','');\n"
            "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\nDATA;\n"
        )
        return head + "\n".join(self.lines) + "\nENDSEC;\nEND-ISO-10303-21;\n"
