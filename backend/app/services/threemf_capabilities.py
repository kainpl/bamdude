"""3MF capabilities probe shared by archive + library viewer endpoints.

Both ``GET /api/v1/archives/{id}/capabilities`` and the library-side
``GET /api/v1/library/files/{id}/capabilities`` answer the same set of
questions before opening the 3D / G-code viewer:

- Does the file carry a viewable mesh?
- Does it carry G-code?
- What's the bed size to draw under the model?
- What filament colours should toolchanger segments paint with?

The archive route grew this logic inline in 2024 and the library route
went without it (defaulting bed size to 256x256x256, wrong for A1 mini /
H2D). Pulling it into a service so the two routes share a single
extractor — fewer surfaces to keep in sync when Bambu adds a new printer
config field.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import defusedxml.ElementTree as ET

# X1/P1/A1 family default. Matches the slicer's curr_bed_type fallback —
# a sensible "if we couldn't read it, assume the most common Bambu" guess.
_DEFAULT_VOLUME = {"x": 256, "y": 256, "z": 256}


@dataclass
class ThreeMfCapabilities:
    """Probe result for a 3MF (and optional paired source 3MF).

    Mesh presence is reported as two granular flags rather than a
    single ``has_model`` boolean so callers can apply their own policy:
    the archive route only wants to show the 3D-model tab when an
    unsliced source 3MF is available (``has_mesh_in_source``), because
    a sliced container's embedded mesh is already rasterised into the
    G-code preview and re-rendering it under "3D Model" is misleading.
    The legacy ``has_model`` property keeps the OR-combined signal for
    callers that don't care.
    """

    has_mesh_in_primary: bool = False
    # Only set when a source path was supplied AND was a readable 3MF.
    # ``False`` for both "no source supplied" and "source supplied but
    # no mesh found inside" — callers needing to distinguish should
    # combine with their own ``source_path is not None`` check.
    has_mesh_in_source: bool = False
    has_gcode: bool = False
    build_volume: dict = field(default_factory=lambda: dict(_DEFAULT_VOLUME))
    filament_colors: list[str] = field(default_factory=list)

    @property
    def has_model(self) -> bool:
        """Combined OR — mesh present anywhere. Use the granular flags
        when the policy is stricter than "any mesh anywhere"."""
        return self.has_mesh_in_primary or self.has_mesh_in_source


def _file_has_mesh_data(zf: zipfile.ZipFile, names: list[str]) -> bool:
    """Look for any ``.model`` entry that carries actual geometry."""
    for name in names:
        if not name.endswith(".model"):
            continue
        try:
            content = zf.read(name).decode("utf-8")
        except Exception:  # noqa: BLE001 — corrupt entries shouldn't break the probe
            continue
        if "<vertex" in content or "<mesh" in content:
            return True
    return False


def _parse_project_settings(zf: zipfile.ZipFile, names: list[str]) -> tuple[dict, list[str]]:
    """Read ``Metadata/project_settings.config`` for build volume + colours.

    Returns ``(volume, colors)``. Either field may be empty / default
    when the config isn't present or the relevant key is missing —
    callers fall back to the previous best signal.
    """
    volume = dict(_DEFAULT_VOLUME)
    colors: list[str] = []
    if "Metadata/project_settings.config" not in names:
        return volume, colors

    try:
        config_data = json.loads(zf.read("Metadata/project_settings.config").decode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed JSON, treat as missing
        return volume, colors

    # printable_area: ['0x0', '256x0', '256x256', '0x256']
    printable_area = config_data.get("printable_area") or []
    if isinstance(printable_area, list) and len(printable_area) >= 3:
        max_x = max_y = 0
        for coord in printable_area:
            if not isinstance(coord, str) or "x" not in coord:
                continue
            parts = coord.split("x")
            if len(parts) != 2:
                continue
            try:
                x, y = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            max_x = max(max_x, x)
            max_y = max(max_y, y)
        if max_x > 0 and max_y > 0:
            volume["x"] = max_x
            volume["y"] = max_y

    printable_height = config_data.get("printable_height")
    if printable_height:
        try:
            volume["z"] = int(printable_height)
        except (ValueError, TypeError):
            pass

    raw_colors = config_data.get("filament_colour") or []
    if isinstance(raw_colors, list):
        colors = [c for c in raw_colors if isinstance(c, str) and c]

    return volume, colors


def _parse_slice_info_colors(zf: zipfile.ZipFile, names: list[str]) -> list[str]:
    """Read tool/extruder colours from ``Metadata/slice_info.config``.

    These are the actual filaments used in the print, indexed by tool
    ID (1-based in the file, returned 0-based). Used for G-code preview
    where toolchanger segments need consistent colours per extruder slot.
    """
    if "Metadata/slice_info.config" not in names:
        return []
    try:
        root = ET.fromstring(zf.read("Metadata/slice_info.config").decode("utf-8"))
    except Exception:  # noqa: BLE001 — malformed XML, treat as missing
        return []

    filament_map: dict[int, str] = {}
    for f in root.findall(".//filament"):
        fid = f.get("id")
        fcolor = f.get("color")
        try:
            used_amount = float(f.get("used_g", "0"))
        except (ValueError, TypeError):
            used_amount = 0
        if fid is None or not fcolor or used_amount <= 0:
            continue
        try:
            tool_id = int(fid) - 1
        except ValueError:
            continue
        if tool_id >= 0:
            filament_map[tool_id] = fcolor

    if not filament_map:
        return []
    max_tool = max(filament_map.keys())
    return [filament_map.get(i, "#00AE42") for i in range(max_tool + 1)]


def extract_3mf_capabilities(
    *,
    primary_path: Path,
    source_path: Path | None = None,
) -> ThreeMfCapabilities:
    """Inspect a 3MF (and optionally a paired source 3MF) for viewer capabilities.

    ``primary_path`` is the file the user actually opened — usually a
    sliced ``.gcode.3mf`` for archives or a library-stored 3MF.
    ``source_path`` (archive-only) points at the unsliced upstream 3MF
    when one is preserved alongside; mesh data + per-toolhead colours
    are preferred from there because the source contains the cleanest
    geometry and the original colour assignments.

    Returns a populated :class:`ThreeMfCapabilities`. Bad zips don't
    raise — the caller gets the default volume + empty colour list,
    which is the same behaviour as a 3MF without the relevant metadata.
    """
    caps = ThreeMfCapabilities()

    # 1. Source 3MF first (if supplied + readable). Mesh + colours +
    # volume from here win over the sliced container's metadata.
    if source_path is not None and source_path.exists():
        try:
            with zipfile.ZipFile(source_path, "r") as zf:
                names = zf.namelist()
                if _file_has_mesh_data(zf, names):
                    caps.has_mesh_in_source = True
                src_volume, src_colors = _parse_project_settings(zf, names)
                if src_colors:
                    caps.filament_colors = src_colors
                if src_volume != _DEFAULT_VOLUME:
                    caps.build_volume = src_volume
        except zipfile.BadZipFile:
            pass

    # 2. Primary 3MF for G-code presence + mesh / volume / colours.
    try:
        with zipfile.ZipFile(primary_path, "r") as zf:
            names = zf.namelist()

            caps.has_gcode = any(n.startswith("Metadata/") and n.endswith(".gcode") for n in names)
            caps.has_mesh_in_primary = _file_has_mesh_data(zf, names)

            slice_colors = _parse_slice_info_colors(zf, names)

            # Build volume + project colours fallback if the source pass
            # didn't supply them.
            if caps.build_volume == _DEFAULT_VOLUME:
                pri_volume, pri_colors = _parse_project_settings(zf, names)
                if pri_volume != _DEFAULT_VOLUME:
                    caps.build_volume = pri_volume
                if not caps.filament_colors and pri_colors:
                    caps.filament_colors = pri_colors

            if not caps.filament_colors and slice_colors:
                caps.filament_colors = slice_colors
    except zipfile.BadZipFile:
        # Surface a clear error to the caller — the route handler
        # decides whether to 4xx or fall back.
        raise

    return caps
