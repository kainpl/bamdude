"""3MF composer for calibration prints (W2 Phase 0+).

Composes a calibration ``.3mf`` file in Bambu Studio / OrcaSlicer
format. For STL / STEP inputs the writer uses BS's own
``pa_pattern.3mf`` as a *scaffold*: BS's full 322-key
``Metadata/project_settings.config``, the slice_info / model_settings
boilerplate, the Content_Types + relationship files all pass through
verbatim. We only replace what's mode-specific: the mesh content, the
per-layer custom g-code, per-object overrides, the build item's
transform (so our mesh prints at native coordinates rather than
inheriting pa_pattern's scale-down), and a few project-level keys we
have to force (compatibility lists → empty; ``curr_bed_type`` → a
filament-permissive default).

Why scaffold-based and not synthesized-from-scratch: re-deriving BS's
3MF schema by hand turned into a tail of compatibility footguns
(missing slice_info, partial project_settings → "process preset in the
3mf" rejection, plate-vs-filament validation, ...). Pa_pattern.3mf is
a BS-blessed minimal 3MF that BS / Orca accept unconditionally; using
it as scaffold means we inherit every boilerplate the slicer expects
without enumerating it. See
``temp/w2-calibration-slicing-feasibility.md`` for the architectural
context.

For 3MF inputs (PA Pattern, Flow Rate, Auto PA — modes where Bambu
already ships a 3MF) we keep the original 3MF as scaffold and only
overlay our metadata.

The composer is a pure utility — no slicer call, no DB, no MQTT.
Per-mode orchestrators in ``calib_3mf_builder`` decide *what* to
inject; this module decides *how* to package it. Output is a byte
string ready to POST to ``SlicerApiService.slice_with_profiles``.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape as xml_escape

logger = logging.getLogger(__name__)


GeometryKind = Literal["3mf", "stl"]


# Scaffold path: BS's pa_pattern.3mf has the right structure (full
# project_settings.config, Bambu-format model_settings, slice_info,
# all rels). Used as the template for STL-wrap output.
_SCAFFOLD_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "calib_assets" / "pressure_advance" / "pa_pattern.3mf"
)

# Pa_pattern uses object id=2 at top level (referenced by build) and
# object id=1 in 3D/Objects/Cube_1.model. We keep the same IDs and the
# same per-object filename so we don't have to also patch
# 3D/3dmodel.model's <component p:path>. Callers' ObjectOverride.object_id
# should point at WRAPPED_OBJECT_ID.
WRAPPED_OBJECT_ID = 2
WRAPPED_INNER_OBJECT_ID = 1
_SCAFFOLD_MESH_PATH = "3D/Objects/Cube_1.model"

# Filament-permissive default plate. PETG / TPU don't list "Cool Plate"
# in their compatible_prints, so BS rejects the slice with "Plate 1:
# Cool Plate does not support filament 1". Textured PEI Plate is
# listed by every common filament we care about (PLA / PETG / ABS /
# ASA / TPU / PA / PC). Caller can override via the ``bed_type``
# parameter; the route's ``body.bed_type`` becomes a CLI
# ``--curr-bed-type`` flag that further overrides at slice time.
_DEFAULT_CURR_BED_TYPE = "Textured PEI Plate"


# BS encodes ``<layer type="...">`` as a numeric code, not a string.
# Source: BS GCodeProcessor enum (CustomGCode::Type). We expose a
# readable name in the dataclass and translate at render time.
_CUSTOM_GCODE_TYPE_CODE = {
    "ColorChange": 1,
    "PausePrint": 2,
    "ToolChange": 3,
    "Custom": 4,
}

# BS writes ``extruder="-858993460"`` (0xCCCCCCCC, uninitialised int)
# for non-tool-change entries. Mirroring the literal so BS's parser
# accepts it without re-interpreting the value.
_UNSET_EXTRUDER_SENTINEL = -858993460


@dataclass(frozen=True)
class CustomGcodeItem:
    """One entry in ``Metadata/custom_gcode_per_layer.xml``.

    ``print_z`` is the layer-boundary Z (mm) at which the slicer should
    splice ``extra`` into the generated g-code. ``type='Custom'`` covers
    the M900 / M104 / retraction / speed sweeps each tower mode needs;
    the other type values are accepted by both BS and Orca but not used
    by W2 calibration yet.
    """

    print_z: float
    extra: str
    type: Literal["Custom", "ColorChange", "PausePrint", "ToolChange"] = "Custom"
    extruder: int | None = None  # None → BS's uninitialised sentinel
    color: str = ""


@dataclass(frozen=True)
class ObjectOverride:
    """One ``<object>`` block in ``Metadata/model_settings.config``.

    For STL-wrapped 3MFs, ``object_id`` should be
    :data:`WRAPPED_OBJECT_ID` — the scaffold puts a single object at
    that id. For 3MF pass-through the caller must use whatever id the
    upstream 3MF declared.

    ``config`` is a flat string-keyed map of slicer parameters to
    override (e.g. ``{"seam_position": "rear"}``).
    """

    object_id: int
    config: dict[str, str] = field(default_factory=dict)


def write_calibration_3mf(
    *,
    geometry_bytes: bytes,
    geometry_kind: GeometryKind,
    custom_gcodes: list[CustomGcodeItem] | None = None,
    object_overrides: list[ObjectOverride] | None = None,
    project_settings_patch: dict[str, str] | None = None,
    bed_type: str | None = None,
    build_transform_scale: tuple[float, float, float] | None = None,
    build_transform_translate: tuple[float, float, float] | None = None,
    printable: bool | None = None,
    target_printer_settings_id: str | None = None,
    output_filename: str = "calibration.3mf",
) -> bytes:
    """Compose a calibration 3MF.

    ``geometry_bytes`` is either an existing Bambu-format 3MF
    (``geometry_kind='3mf'``, pass-through with metadata overwrite) or
    a single-mesh STL (``geometry_kind='stl'``, wrapped via the
    pa_pattern.3mf scaffold).

    ``bed_type`` overrides the safe default ``Textured PEI Plate``
    embedded in the scaffold's ``curr_bed_type``. Pass ``None`` to keep
    the default. The slicer CLI's ``--curr-bed-type`` flag (from the
    route's ``body.bed_type``) wins over this at slice time anyway.

    Returns the composed 3MF as a byte string ready to feed to
    :meth:`SlicerApiService.slice_with_profiles`.
    """
    custom_gcodes = list(custom_gcodes or [])
    object_overrides = list(object_overrides or [])

    if geometry_kind == "stl":
        return _compose_from_stl(
            stl_bytes=geometry_bytes,
            custom_gcodes=custom_gcodes,
            object_overrides=object_overrides,
            project_settings_patch=project_settings_patch,
            bed_type=bed_type or _DEFAULT_CURR_BED_TYPE,
            build_transform_scale=build_transform_scale,
            target_printer_settings_id=target_printer_settings_id,
            output_filename=output_filename,
        )

    if geometry_kind == "3mf":
        return _compose_from_3mf(
            base_3mf_bytes=geometry_bytes,
            custom_gcodes=custom_gcodes,
            object_overrides=object_overrides,
            project_settings_patch=project_settings_patch,
            bed_type=bed_type,
            target_printer_settings_id=target_printer_settings_id,
            output_filename=output_filename,
            build_transform_scale=build_transform_scale,
            build_transform_translate=build_transform_translate,
            printable=printable,
        )

    raise ValueError(f"Unsupported geometry_kind: {geometry_kind!r}")


# -- STL composition (scaffold-based) ------------------------------------


def _compose_from_stl(
    *,
    stl_bytes: bytes,
    custom_gcodes: list[CustomGcodeItem],
    object_overrides: list[ObjectOverride],
    project_settings_patch: dict[str, str] | None,
    bed_type: str,
    build_transform_scale: tuple[float, float, float] | None,
    target_printer_settings_id: str | None,
    output_filename: str,
) -> bytes:
    """Build a Bambu-format 3MF using pa_pattern.3mf as scaffold.

    Patches inside the scaffold:

    - ``3D/Objects/Cube_1.model``: replace with our mesh (same file name
      so the scaffold's ``<component p:path>`` reference keeps working
      without further 3dmodel.model edits).
    - ``3D/3dmodel.model``: neutralise the build ``<item>`` transform
      (scale → identity, translate → plate centre) so our mesh prints
      at its native coordinates instead of inheriting pa_pattern's
      0.28× scale.
    - ``Metadata/custom_gcode_per_layer.xml``: our per-Z items.
    - ``Metadata/model_settings.config``: rename "Cube" → "calibration",
      neutralise the ``<assemble_item>`` scale, merge per-object
      overrides into the ``<object>`` block.
    - ``Metadata/project_settings.config``: force compat lists empty,
      force ``curr_bed_type`` to the filament-permissive default (or
      caller's override), layer caller's project-level patch on top.
    """
    if not _SCAFFOLD_PATH.exists():
        raise FileNotFoundError(
            f"Calibration 3MF scaffold not found at {_SCAFFOLD_PATH}. "
            "Was the BS-mirrored pa_pattern.3mf ever added to "
            "backend/app/data/calib_assets/pressure_advance/?"
        )
    scaffold_bytes = _SCAFFOLD_PATH.read_bytes()

    vertices, faces = _load_stl_mesh(stl_bytes)
    new_mesh_xml = _emit_inner_object_model(vertices, faces)

    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(scaffold_bytes), "r") as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for name in src.namelist():
            if name == _SCAFFOLD_MESH_PATH:
                dst.writestr(name, new_mesh_xml)
            elif name == "3D/3dmodel.model":
                dst.writestr(
                    name,
                    _patch_top_level_model_transform(
                        src.read(name).decode("utf-8"),
                        scale=build_transform_scale,
                    ),
                )
            elif name == "Metadata/custom_gcode_per_layer.xml":
                dst.writestr(name, _render_custom_gcodes(custom_gcodes))
            elif name == "Metadata/model_settings.config":
                dst.writestr(
                    name,
                    _patch_model_settings_for_calibration(
                        src.read(name).decode("utf-8"),
                        object_overrides,
                    ),
                )
            elif name == "Metadata/project_settings.config":
                dst.writestr(
                    name,
                    _patch_project_settings_for_calibration(
                        src.read(name).decode("utf-8"),
                        project_settings_patch,
                        bed_type,
                        target_printer_settings_id=target_printer_settings_id,
                    ),
                )
            else:
                # Pass through: [Content_Types].xml, _rels/.rels,
                # 3D/_rels/3dmodel.model.rels, Metadata/slice_info.config,
                # Metadata/cut_information.xml, Metadata/plate_*.png,
                # Metadata/top_*.png, Metadata/pick_*.png — all things
                # BS / Orca expect or tolerate verbatim.
                dst.writestr(name, src.read(name))

    logger.debug(
        "calib_3mf_writer: composed %s from STL via scaffold, %d custom_gcodes, %d overrides",
        output_filename,
        len(custom_gcodes),
        len(object_overrides),
    )
    return out.getvalue()


# -- 3MF pass-through (BS-shipped scaffold modes) -------------------------


def _compose_from_3mf(
    *,
    base_3mf_bytes: bytes,
    custom_gcodes: list[CustomGcodeItem],
    object_overrides: list[ObjectOverride],
    project_settings_patch: dict[str, str] | None,
    bed_type: str | None,
    target_printer_settings_id: str | None,
    output_filename: str,
    build_transform_scale: tuple[float, float, float] | None = None,
    build_transform_translate: tuple[float, float, float] | None = None,
    printable: bool | None = None,
) -> bytes:
    """Copy the base 3MF through, overwriting our metadata files.

    Used when Bambu ships a per-mode 3MF directly (pa_pattern.3mf,
    flowrate-test-*.3mf, auto_pa_line_*.3mf) — the input is already
    valid Bambu, we just inject per-Z gcode + per-object overrides +
    project-level patches.
    """
    out_buf = io.BytesIO()
    needs_model_settings_patch = bool(object_overrides)
    needs_project_patch = bool(project_settings_patch) or bed_type is not None or target_printer_settings_id is not None

    upstream_model_settings: str | None = None
    upstream_project_settings: str | None = None

    # If the caller didn't pass any per-Z custom-gcode items, keep
    # whatever ``custom_gcode_per_layer.xml`` the scaffold shipped —
    # blowing it away with an empty render would erase BS's pre-baked
    # patterns (PA Pattern ships its entire comb-and-digit comb gcode
    # inside this file, and overriding with an empty wrapper turns the
    # output into a plain cube print). We only overwrite when the
    # builder explicitly produces new entries (e.g. PA Tower's M900 K
    # sweep).
    overwrite_custom_gcode = bool(custom_gcodes)
    needs_build_transform_patch = (
        build_transform_scale is not None or build_transform_translate is not None or printable is not None
    )

    with (
        zipfile.ZipFile(io.BytesIO(base_3mf_bytes), "r") as src,
        zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as dst,
    ):
        for name in src.namelist():
            if name == "Metadata/custom_gcode_per_layer.xml" and overwrite_custom_gcode:
                continue
            if name == "Metadata/model_settings.config" and needs_model_settings_patch:
                upstream_model_settings = src.read(name).decode("utf-8", errors="replace")
                continue
            if name == "Metadata/project_settings.config" and needs_project_patch:
                upstream_project_settings = src.read(name).decode("utf-8", errors="replace")
                continue
            if name == "3D/3dmodel.model" and needs_build_transform_patch:
                xml = src.read(name).decode("utf-8", errors="replace")
                dst.writestr(
                    name,
                    _patch_top_level_model_transform(
                        xml,
                        scale=build_transform_scale,
                        translate=build_transform_translate,
                        printable=printable,
                    ),
                )
                continue
            dst.writestr(name, src.read(name))

        if overwrite_custom_gcode:
            dst.writestr(
                "Metadata/custom_gcode_per_layer.xml",
                _render_custom_gcodes(custom_gcodes),
            )
        if needs_model_settings_patch and upstream_model_settings is not None:
            dst.writestr(
                "Metadata/model_settings.config",
                _merge_model_settings_overrides(upstream_model_settings, object_overrides),
            )
        if needs_project_patch and upstream_project_settings is not None:
            dst.writestr(
                "Metadata/project_settings.config",
                _patch_project_settings_for_calibration(
                    upstream_project_settings,
                    project_settings_patch,
                    bed_type,
                    target_printer_settings_id=target_printer_settings_id,
                ),
            )

    logger.debug(
        "calib_3mf_writer: composed %s from existing 3MF, %d custom_gcodes, %d overrides",
        output_filename,
        len(custom_gcodes),
        len(object_overrides),
    )
    return out_buf.getvalue()


# -- Per-object mesh emission --------------------------------------------


def _emit_inner_object_model(vertices, faces) -> bytes:
    """Emit ``3D/Objects/Cube_1.model``'s replacement.

    The scaffold's top-level ``3D/3dmodel.model`` references this file
    via ``<component p:path>``, so the on-disk name must stay
    ``Cube_1.model`` even though our mesh is no longer a cube.
    """
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
        'requiredextensions="p">',
        ' <metadata name="BambuStudio:3mfVersion">1</metadata>',
        " <resources>",
        f'  <object id="{WRAPPED_INNER_OBJECT_ID}" type="model">',
        "   <mesh>",
        "    <vertices>",
    ]
    for v in vertices:
        lines.append(f'     <vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>')
    lines.append("    </vertices>")
    lines.append("    <triangles>")
    for f in faces:
        lines.append(f'     <triangle v1="{int(f[0])}" v2="{int(f[1])}" v3="{int(f[2])}"/>')
    lines.append("    </triangles>")
    lines.append("   </mesh>")
    lines.append("  </object>")
    lines.append(" </resources>")
    lines.append("</model>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _load_stl_mesh(stl_bytes: bytes):
    """Load STL via trimesh, normalise placement, return (vertices, faces).

    Calibration STLs come from different sources (BS-shipped, Orca-
    exported, user-supplied) and ship at varying world-space
    positions. ``tower_with_seam.stl`` from Orca's File → Export
    keeps the world coords (centre at ~218, 218, ~55); BS's own
    version is centred near (218, 218, 60); some are origin-centred
    already. The 3MF build-item transform we emit adds a fixed
    ``(90, 90, 0)`` translation (the plate centre), so any STL that
    isn't origin-centred lands far off the actual print plate
    (218 + 90 = 308 → way beyond A1 mini's 180 mm bed → slicer
    rejects ``-50 plate is empty``).

    BS itself sidesteps this by calling ``ensure_on_bed()`` +
    auto-centre after ``add_model()``; we replicate the same
    normalisation here so the wrapper is robust against arbitrary
    STL contributions — XY bbox centre → origin, Z bbox bottom → 0.
    Meshes that are already normalised pass through unchanged (the
    subtraction is a no-op).
    """
    import numpy as np
    import trimesh

    mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", process=False)
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(mesh.dump())
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError("STL input did not decode to a usable mesh")
    if mesh.is_empty:
        raise ValueError("STL decoded to an empty mesh")
    bounds = mesh.bounds
    cx = (bounds[0, 0] + bounds[1, 0]) / 2.0
    cy = (bounds[0, 1] + bounds[1, 1]) / 2.0
    z_min = bounds[0, 2]
    verts = np.asarray(mesh.vertices, dtype=np.float64) - np.array([cx, cy, z_min], dtype=np.float64)
    return verts, mesh.faces


def _stl_z_extent(stl_bytes: bytes) -> float:
    """Return the Z height (max_z - min_z) of an STL mesh in millimetres.

    Used by per-mode builders that compute the build-item Z-scale as
    ``target_height / native_z`` — relying on a hard-coded native_z
    breaks the moment someone swaps the STL for a re-export with a
    different native height (BS's tower_with_seam.stl ships at 60 mm,
    Orca's Export-as-STL flow trims the same mesh down to 51 mm).
    Reading the actual extent makes the scale calculation robust to
    STL revisions without code changes.
    """
    import trimesh

    mesh = trimesh.load(io.BytesIO(stl_bytes), file_type="stl", process=False)
    if isinstance(mesh, trimesh.Scene):
        mesh = trimesh.util.concatenate(mesh.dump())
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError("STL input did not decode to a usable mesh")
    return float(mesh.bounds[1, 2] - mesh.bounds[0, 2])


# -- Scaffold patchers ---------------------------------------------------


def _patch_top_level_model_transform(
    xml: str,
    scale: tuple[float, float, float] | None = None,
    translate: tuple[float, float, float] | None = None,
    printable: bool | None = None,
) -> str:
    """Patch the build ``<item>`` transform with caller-supplied
    scale + translate.

    Pa_pattern's build item carries a 0.28× scale (the scaffold cube is
    18×18×18 and BS scales it down to a small 5×5×0.85 print bed
    placeholder). For other meshes (PA Tower's 80×80×60 scaffold etc.)
    that scale is wrong; the caller computes per-mode scale factors
    instead. ``scale=None`` falls back to identity scale.

    ``translate`` overrides the default centre ``(90, 90, 0)``. Per-mode
    builders (PA Pattern) need the cube positioned in the upper-left
    of the print region — anchored to the pattern's frame — so the
    cube's perimeters don't overprint the pattern's V-walls or glyph
    digits. PA Tower keeps the default centre.

    ``printable`` — flip the BS ``printable`` attribute on the build
    item (BS reads it at ``bbs_3mf.cpp:4035``). ``False`` marks the
    object non-printable so the slicer keeps the geometry for plate
    bbox math but skips perimeters/infill. Used by PA Line's cube
    placeholder. ``None`` leaves the existing attribute untouched.

    ``p:uuid`` attributes are stripped (irrelevant to slicing; brittle
    to inherit verbatim from the scaffold).
    """
    if scale is None:
        sx, sy, sz = 1.0, 1.0, 1.0
    else:
        sx, sy, sz = scale
    if translate is None:
        tx, ty, tz = 90.0, 90.0, 0.0
    else:
        tx, ty, tz = translate
    transform = f"{sx} 0 0 0 {sy} 0 0 0 {sz} {tx} {ty} {tz}"
    xml = re.sub(r'\s+p:uuid="[^"]*"', "", xml)
    xml = re.sub(
        r'(<item\b[^>]*?\stransform=")[^"]+(")',
        rf"\g<1>{transform}\g<2>",
        xml,
    )
    if printable is not None:
        flag = "1" if printable else "0"
        if re.search(r'<item\b[^>]*\sprintable="[^"]*"', xml):
            xml = re.sub(
                r'(<item\b[^>]*?\sprintable=")[^"]+(")',
                rf"\g<1>{flag}\g<2>",
                xml,
            )
        else:
            # Inject ``printable="..."`` just before the self-closing
            # slash (or closing ``>`` when not self-closed).
            xml = re.sub(
                r"(<item\b[^>]*?)(\s*/?>)",
                rf'\g<1> printable="{flag}"\g<2>',
                xml,
                count=1,
            )
    return xml


def _patch_model_settings_for_calibration(
    xml: str,
    overrides: list[ObjectOverride],
) -> str:
    """Calibration-clean model_settings.config.

    Rename ``"Cube"`` → ``"calibration"`` so the BS GUI doesn't show a
    misleading object label. Neutralise the ``<assemble_item>``
    transform (same scaling concern as the build ``<item>``). Then
    merge per-object overrides by appending ``<metadata>`` rows before
    the matching ``</object>`` close tag.
    """
    xml = xml.replace(
        '<metadata key="name" value="Cube"/>',
        '<metadata key="name" value="calibration"/>',
    )
    xml = re.sub(
        r'(<assemble_item\b[^>]*?\stransform=")[^"]+(")',
        r"\g<1>1 0 0 0 1 0 0 0 1 0 0 0\g<2>",
        xml,
    )
    if not overrides:
        return xml
    for ov in overrides:
        marker_open = f'<object id="{ov.object_id}">'
        if marker_open not in xml:
            logger.warning(
                "calib_3mf_writer: object id %d not in upstream model_settings; skipping %d overrides",
                ov.object_id,
                len(ov.config),
            )
            continue
        rows = "\n".join(
            '    <metadata key="{k}" value="{v}"/>'.format(
                k=xml_escape(k, {'"': "&quot;"}),
                v=xml_escape(v, {'"': "&quot;"}),
            )
            for k, v in ov.config.items()
        )
        # BS / Orca only honor per-object <metadata> entries that appear
        # BEFORE the first <part> element inside <object>. Inserting at
        # </object> (after </part>) is structurally still valid XML but
        # BS silently ignores those entries during model_settings load —
        # verified empirically (sidecar-sliced 3MF showed seam_position=
        # "aligned" after we'd set "rear" via this path). Anchor on the
        # first <part inside the matching <object> and insert there.
        idx_open = xml.index(marker_open)
        part_anchor = xml.find("<part ", idx_open)
        if part_anchor == -1:
            # No <part> in this object — fall back to inserting just
            # before </object> (e.g. for synthetic objects that carry
            # only metadata). Behaviour matches the pre-fix code.
            part_anchor = xml.index("</object>", idx_open)
        # Walk back to the indent of the <part anchor so the inserted
        # rows align to the same indentation depth.
        line_start = xml.rfind("\n", 0, part_anchor) + 1
        indent = xml[line_start:part_anchor]
        prefixed_rows = "\n".join(indent + r.lstrip() for r in rows.splitlines())
        xml = xml[:line_start] + prefixed_rows + "\n" + xml[line_start:]
    return xml


# Keys we strip from the inherited pa_pattern.3mf project_settings.config
# before shipping the bake.
#
# **Why only brim, not identity:** stripping ``printer_settings_id`` /
# ``printer_model`` / etc. crashes BS CLI with SIGSEGV. The sidecar's
# PresetRef resolver passes flatten-stubs (``{inherits: ..., id: ...}``)
# via ``--load-settings``, NOT fully-flattened configs — so when BS opens
# our 3MF, it relies on the embedded project_settings.config for the
# canonical printer identity (which feeds bed-mesh / calibration lookups
# / lots of cross-references downstream). Stripping ``printer_settings_id``
# leaves BS with neither ours nor a flatten — it crashes when it tries
# to dereference. Trade-off: we keep N1 identity in the embedded config
# (so GUI inspection of the bake shows N1 until ``--load-settings``
# overlays in CLI), but the sliced output's actual settings come from
# ``--load-settings`` (verified — sliced gcode footer correctly shows
# the operator's printer).
#
# The brim keys are the one set we can safely strip — they're rendered
# pre-slice in GUI and aren't dereferenced by any critical code path.
# Removing them keeps the bake.3mf from showing "auto_brim" in the GUI
# even though the operator's process preset says ``no_brim``.
_IDENTITY_KEYS_TO_STRIP = frozenset(
    {
        "brim_type",
        "brim_width",
        "brim_object_gap",
    }
)


def _derive_printer_model_from_settings_id(printer_settings_id: str) -> str:
    """Strip BS's ``" 0.X nozzle"`` suffix to get the bare printer_model.

    ``"Bambu Lab A1 mini 0.4 nozzle"`` → ``"Bambu Lab A1 mini"``.
    ``"Bambu Lab A1 mini"`` → ``"Bambu Lab A1 mini"`` (no-op).
    """
    return re.sub(r" \d+(?:\.\d+)? nozzle$", "", printer_settings_id).strip()


def _patch_project_settings_for_calibration(
    upstream_json: str,
    patch: dict[str, str] | None,
    bed_type: str | None,
    target_printer_settings_id: str | None = None,
) -> bytes:
    """Patch the inherited ``project_settings.config`` for calibration.

    Pa_pattern.3mf ships with 322 keys tuned for Bambu Lab N1 + Bambu
    PLA Basic + ``brim_type=auto_brim``. We need the *technical*
    defaults (retraction, fan, temps, ...) because the sidecar's
    PresetRef resolver doesn't fully flatten cloud stubs through
    ``--load-settings`` — without a baseline the slicer crashes
    mid-validate. But we don't want the *identity / user-visible*
    keys because they mislead GUI inspection of the bake.

    Transforms applied:

    1. **Strip identity keys** (``printer_settings_id``,
       ``filament_settings_id``, ``brim_type``, ...) — see
       :data:`_IDENTITY_KEYS_TO_STRIP`. The operator's GUI fills
       these from their current preset when the file's opened.
    2. **Compatibility lists → empty.** Short-circuits the slicer's
       printer-process / filament-plate compat check.
    3. **``curr_bed_type``** → operator's pick (or filament-
       permissive ``Textured PEI Plate`` default).
    4. **Caller patch** layered on top. The forced keys above win
       so a builder can't accidentally re-introduce restrictive
       values.
    """
    try:
        data = json.loads(upstream_json) if upstream_json.strip() else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, TypeError):
        logger.warning("calib_3mf_writer: project_settings.config not valid JSON, replacing")
        data = {}
    for key in _IDENTITY_KEYS_TO_STRIP:
        data.pop(key, None)
    if patch:
        data.update(patch)
    if bed_type:
        data["curr_bed_type"] = bed_type
    data["compatible_printers"] = []
    data["compatible_printers_condition"] = ""
    data["compatible_prints"] = []
    data["compatible_prints_condition"] = ""
    data["print_compatible_printers"] = []
    # Clear ``upward_compatible_machine``. Otherwise BS CLI's machine-switch
    # guard (BambuStudio.cpp:2942-2961) rejects when bundle/--load-settings
    # carries a printer that's not in the scaffold's hard-coded upward list
    # (pa_pattern.3mf ships with N1 + [P1S/P1P/X1/X1C] — so A1 mini, P1, X1E
    # bundles all hit -16 CLI_3MF_NEW_MACHINE_NOT_SUPPORTED). With the list
    # empty, the size() > 0 branch is skipped and BS falls through cleanly.
    data["upward_compatible_machine"] = []
    # Overwrite the scaffold's printer identity to match what the caller is
    # actually loading via --load-settings. Without this, BS CLI enters the
    # "machine switch" case (line 2942) when names differ, which then
    # depends on upward_compatible_machine to allow the change — but we
    # cleared that list. Matching names short-circuits the whole check.
    if target_printer_settings_id:
        data["printer_settings_id"] = target_printer_settings_id
        data["printer_model"] = _derive_printer_model_from_settings_id(target_printer_settings_id)
    return json.dumps(data, indent=2).encode("utf-8")


# -- Metadata renderers ---------------------------------------------------


def _render_custom_gcodes(items: list[CustomGcodeItem]) -> bytes:
    """Render the BS / Orca ``Metadata/custom_gcode_per_layer.xml`` shape.

    BS schema (from pa_pattern.3mf and BS source):

    .. code-block:: xml

       <custom_gcodes_per_layer>
       <plate>
       <plate_info id="1"/>
       <layer top_z="0.25" type="4" extruder="-858993460" color="" extra="..."/>
       ...
       </plate>
       </custom_gcodes_per_layer>

    Note: ``<plate>`` is unattributed; the id lives on a separate
    ``<plate_info>`` child. ``type`` is a numeric BS enum
    (``CustomGCode::Type``, 4 = Custom). ``extruder`` defaults to BS's
    uninitialised sentinel (0xCCCCCCCC = -858993460) for non-tool-change
    rows. I had a wrong shape earlier (``<plate id="1">`` with string
    ``type``) — BS silently ignored every ``<layer>`` entry because the
    parser rejected the attribute layout, leaving the sliced gcode
    devoid of our M900 commands.

    Items with ``print_z <= 0`` are filtered; sorted ascending by Z.
    Empty list produces an empty ``<plate>`` so the file's presence
    assumption holds.
    """
    sorted_items = sorted((i for i in items if i.print_z > 0), key=lambda i: i.print_z)
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<custom_gcodes_per_layer>"]
    lines.append("<plate>")
    lines.append('<plate_info id="1"/>')
    for item in sorted_items:
        type_code = _CUSTOM_GCODE_TYPE_CODE.get(item.type, _CUSTOM_GCODE_TYPE_CODE["Custom"])
        extruder = item.extruder if item.extruder is not None else _UNSET_EXTRUDER_SENTINEL
        lines.append(
            '<layer top_z="{z}" type="{kind}" extruder="{extr}" color="{color}" extra="{extra}"/>'.format(
                # Match BS's formatting: bare decimal, no trailing zeros.
                z=(f"{item.print_z:.6f}").rstrip("0").rstrip("."),
                kind=type_code,
                extr=extruder,
                color=xml_escape(item.color, {'"': "&quot;"}),
                extra=xml_escape(item.extra, {'"': "&quot;", "\n": "&#10;"}),
            )
        )
    lines.append("</plate>")
    lines.append("</custom_gcodes_per_layer>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _merge_model_settings_overrides(
    upstream_xml: str,
    overrides: list[ObjectOverride],
) -> bytes:
    """Append per-object override entries into an upstream ``model_settings.config``.

    For overrides whose object id doesn't exist upstream, the entries
    are skipped with a warning. Used by the 3MF pass-through path.
    """
    text = upstream_xml
    for ov in overrides:
        marker_open = f'<object id="{ov.object_id}">'
        if marker_open not in text:
            logger.warning(
                "calib_3mf_writer: object id %d not found in upstream model_settings.config; skipping %d overrides",
                ov.object_id,
                len(ov.config),
            )
            continue
        metadata_rows = "\n".join(
            '    <metadata key="{k}" value="{v}"/>'.format(
                k=xml_escape(k, {'"': "&quot;"}),
                v=xml_escape(v, {'"': "&quot;"}),
            )
            for k, v in ov.config.items()
        )
        idx_open = text.index(marker_open)
        idx_close = text.index("</object>", idx_open)
        text = text[:idx_close] + metadata_rows + "\n  " + text[idx_close:]
    return text.encode("utf-8")
