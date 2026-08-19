"""3MF file parsing utilities for filament tracking.

This module provides functions to parse Bambu Lab 3MF files and extract
per-layer filament usage data from the embedded G-code. This enables
accurate partial usage reporting for multi-material prints.
"""

import json
import math
import re
import zipfile
from pathlib import Path

import defusedxml.ElementTree as ET

# Default filament properties
DEFAULT_FILAMENT_DIAMETER = 1.75  # mm
DEFAULT_FILAMENT_DENSITY = 1.24  # g/cm³ (PLA)


def parse_gcode_layer_filament_usage(gcode_content: str) -> dict[int, dict[int, float]]:
    """Parse G-code to extract per-layer, per-filament cumulative extrusion in mm.

    This function tracks filament extrusion across layers and tool changes,
    building a cumulative usage map that can be used to calculate partial
    usage at any layer.

    Args:
        gcode_content: The raw G-code content as a string

    Returns:
        A nested dictionary mapping layer numbers to filament usage:
        {layer: {filament_id: cumulative_mm}, ...}

    Example:
        {0: {0: 125.5}, 1: {0: 250.0, 1: 50.0}, 2: {0: 375.0, 1: 150.0}}

        This shows:
        - Layer 0: filament 0 used 125.5mm cumulative
        - Layer 1: filament 0 used 250mm cumulative, filament 1 used 50mm
        - Layer 2: filament 0 used 375mm cumulative, filament 1 used 150mm

    G-code commands parsed:
        - M73 L<layer>: Layer change marker
        - M620 S<filament>: Filament/tool change (S255 = unload)
        - G0/G1/G2/G3 E<amount>: Extrusion moves
    """
    layer_filaments: dict[int, dict[int, float]] = {}
    current_layer = 0
    active_filament: int | None = None
    cumulative_extrusion: dict[int, float] = {}  # filament_id -> total mm

    for line in gcode_content.splitlines():
        line = line.strip()
        if not line:
            continue

        # Handle comments - skip but check for layer markers
        if line.startswith(";"):
            # Some slicers use comment-based layer markers
            # e.g., "; CHANGE_LAYER" or ";LAYER_CHANGE"
            continue

        # Split line into command and inline comment
        if ";" in line:
            line = line.split(";")[0].strip()

        # Extract command and parameters
        parts = line.split()
        if not parts:
            continue
        cmd = parts[0].upper()

        # Layer change: M73 L<layer>
        # Bambu printers use M73 with L parameter for layer indication
        if cmd == "M73":
            for part in parts[1:]:
                part_upper = part.upper()
                if part_upper.startswith("L"):
                    try:
                        new_layer = int(part[1:])
                        # Save current state before layer change
                        if cumulative_extrusion:
                            layer_filaments[current_layer] = cumulative_extrusion.copy()
                        current_layer = new_layer
                    except ValueError:
                        pass  # Skip G-code lines with unparseable layer numbers

        # Filament change: M620 S<filament>
        # Bambu uses M620 for AMS filament switching
        # S255 means full unload (no active filament)
        elif cmd == "M620":
            for part in parts[1:]:
                part_upper = part.upper()
                if part_upper.startswith("S"):
                    filament_str = part[1:]
                    if filament_str == "255":
                        # Full unload - no active filament
                        active_filament = None
                    else:
                        try:
                            # Extract digits (e.g., "0A" -> 0, "1" -> 1)
                            match = re.match(r"(\d+)", filament_str)
                            if match:
                                active_filament = int(match.group(1))
                        except (ValueError, AttributeError):
                            pass  # Skip unparseable filament switch commands

        # Extrusion moves: G0/G1/G2/G3 with E parameter
        # Only G1 typically has extrusion, but check all for safety
        elif cmd in ("G0", "G1", "G2", "G3"):
            if active_filament is None:
                continue
            for part in parts[1:]:
                part_upper = part.upper()
                if part_upper.startswith("E"):
                    try:
                        extrusion = float(part[1:])
                        # Only count positive extrusion (not retractions)
                        if extrusion > 0:
                            current = cumulative_extrusion.get(active_filament, 0)
                            cumulative_extrusion[active_filament] = current + extrusion
                    except ValueError:
                        pass  # Skip G-code lines with unparseable extrusion values

    # Save final layer state
    if cumulative_extrusion:
        layer_filaments[current_layer] = cumulative_extrusion.copy()

    return layer_filaments


def mm_to_grams(
    length_mm: float,
    diameter_mm: float = DEFAULT_FILAMENT_DIAMETER,
    density_g_cm3: float = DEFAULT_FILAMENT_DENSITY,
) -> float:
    """Convert filament length in mm to weight in grams.

    Uses the formula: mass = volume × density
    where volume = π × r² × length

    Args:
        length_mm: Length of filament in millimeters
        diameter_mm: Filament diameter in millimeters (default: 1.75)
        density_g_cm3: Material density in g/cm³ (default: 1.24 for PLA)

    Returns:
        Weight in grams
    """
    radius_cm = (diameter_mm / 2) / 10  # Convert mm to cm
    length_cm = length_mm / 10  # Convert mm to cm
    volume_cm3 = math.pi * radius_cm * radius_cm * length_cm
    return volume_cm3 * density_g_cm3


def extract_layer_filament_usage_from_3mf(
    file_path: Path, plate_id: int | None = None
) -> dict[int, dict[int, float]] | None:
    """Extract per-layer filament usage from a 3MF file's embedded G-code.

    ⚠️ **Pass the plate.** This used to take ``gcode_files[0]`` — the first
    ``.gcode`` member in whatever order the ZIP happens to store them — with a
    comment saying "typically only one per 3MF export". On a multi-plate file
    that is simply untrue, and a Bambu Studio export can store plate 2 first, so
    the per-layer figures were measured against a plate the printer never ran.
    Both inventory backends consume this, so partial-usage tracking and the
    Spoolman debit were wrong together and in the same direction (upstream
    `454457a0`).

    ⚠️ This is the same family as the ``plate_number`` inertness that made Skip
    Objects cancel the wrong part: identifying a plate by position rather than
    by name, and failing silently onto plate 1.

    Falls back to the lowest-numbered plate when no id is given or the named one
    is absent — deterministic, unlike ZIP order, and the same plate the rest of
    the code means by "the first one".

    Args:
        file_path: Path to the 3MF file
        plate_id: 1-based plate the print actually used

    Returns:
        Dictionary mapping layers to filament usage, or None if parsing fails.
        Format: {layer: {filament_id: cumulative_mm}, ...}
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            gcode_files = [f for f in zf.namelist() if f.endswith(".gcode")]
            if not gcode_files:
                return None

            gcode_path = None
            if plate_id:
                wanted = f"plate_{int(plate_id)}.gcode"
                gcode_path = next((f for f in gcode_files if f.endswith(wanted)), None)
            if gcode_path is None:
                # Lowest plate number, then name, so the answer never depends on
                # how the archive was written.
                def _plate_no(name: str) -> tuple[int, str]:
                    stem = name.rsplit("/", 1)[-1]
                    digits = "".join(c for c in stem if c.isdigit())
                    return (int(digits) if digits else 1 << 30, name)

                gcode_path = sorted(gcode_files, key=_plate_no)[0]
            gcode_content = zf.read(gcode_path).decode("utf-8", errors="ignore")

            return parse_gcode_layer_filament_usage(gcode_content)
    except Exception:
        return None


def get_cumulative_usage_at_layer(
    layer_usage: dict[int, dict[int, float]],
    target_layer: int,
) -> dict[int, float]:
    """Get cumulative filament usage (in mm) up to and including target_layer.

    Args:
        layer_usage: The output from parse_gcode_layer_filament_usage()
        target_layer: The layer number to get usage for

    Returns:
        Dictionary of {filament_id: cumulative_mm} for each filament used
        up to target_layer. Returns empty dict if no data available.
    """
    if not layer_usage:
        return {}

    # Find the highest recorded layer <= target_layer
    # (we store snapshots at layer changes, so we need the closest one)
    relevant_layers = [layer for layer in layer_usage if layer <= target_layer]
    if not relevant_layers:
        return {}

    max_layer = max(relevant_layers)
    return layer_usage.get(max_layer, {})


def extract_filament_properties_from_3mf(file_path: Path) -> dict[int, dict]:
    """Extract filament properties (density, diameter, type) from 3MF metadata.

    Args:
        file_path: Path to the 3MF file

    Returns:
        Dictionary mapping filament IDs to their properties:
        {filament_id: {"diameter": 1.75, "density": 1.24, "type": "PLA"}, ...}

        Note: filament_id is 1-based (matches slot_id in slice_info.config)
    """
    properties: dict[int, dict] = {}
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Try slice_info.config first for filament types
            if "Metadata/slice_info.config" in zf.namelist():
                content = zf.read("Metadata/slice_info.config").decode()
                root = ET.fromstring(content)
                for f in root.findall(".//filament"):
                    try:
                        # id is 1-based in slice_info.config
                        fid = int(f.get("id", 0))
                        properties[fid] = {
                            "type": f.get("type", "PLA"),
                            "diameter": DEFAULT_FILAMENT_DIAMETER,
                            "density": DEFAULT_FILAMENT_DENSITY,
                        }
                    except ValueError:
                        pass  # Skip filament entries with unparseable IDs

            # Try project_settings.config for density values
            if "Metadata/project_settings.config" in zf.namelist():
                content = zf.read("Metadata/project_settings.config").decode()
                try:
                    data = json.loads(content)
                    densities = data.get("filament_density", [])
                    for i, density in enumerate(densities):
                        # project_settings uses 0-based indexing, convert to 1-based
                        fid = i + 1
                        if fid not in properties:
                            properties[fid] = {
                                "type": "",
                                "diameter": DEFAULT_FILAMENT_DIAMETER,
                            }
                        try:
                            properties[fid]["density"] = float(density)
                        except (ValueError, TypeError):
                            properties[fid]["density"] = DEFAULT_FILAMENT_DENSITY
                except json.JSONDecodeError:
                    pass  # Skip malformed project_settings.config JSON
    except Exception:
        pass  # Return whatever properties were collected before the error

    return properties


def _first_settings_id(value: object) -> str | None:
    """A ``*_settings_id`` value is usually a string, occasionally a list (one
    entry per extruder). Return the first non-empty string, else None."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def extract_embedded_presets_from_3mf(zf: zipfile.ZipFile) -> dict[str, str | None]:
    """Read the printer / process preset names a 3MF project was prepared with.

    BambuStudio / OrcaSlicer write the chosen preset names into
    ``Metadata/project_settings.config`` (``printer_settings_id`` and
    ``print_settings_id``). The SliceModal uses them to default its printer
    and process dropdowns to what the file was sliced for (#1325) instead of
    blindly taking the first listed preset.

    Returns ``{"printer": <name|None>, "process": <name|None>}``. Every failure
    mode (missing config, malformed JSON, unexpected shape) yields ``None``
    values so the modal falls back to its own defaults.
    """
    result: dict[str, str | None] = {"printer": None, "process": None}
    try:
        if "Metadata/project_settings.config" not in zf.namelist():
            return result
        data = json.loads(zf.read("Metadata/project_settings.config").decode())
    except (KeyError, ValueError, OSError):
        return result
    if not isinstance(data, dict):
        return result
    result["printer"] = _first_settings_id(data.get("printer_settings_id"))
    result["process"] = _first_settings_id(data.get("print_settings_id"))
    return result


def extract_nozzle_mapping_from_3mf(zf: zipfile.ZipFile) -> dict[int, int] | None:
    """Extract per-slot nozzle/extruder mapping from a 3MF file.

    On dual-nozzle printers (H2D, H2D Pro), each filament slot is assigned to a
    specific nozzle. The slicer may override user preferences when using "Auto For
    Flush" mode, so the actual assignment comes from slice_info.config group_id
    attributes, not from the user's filament_nozzle_map preference.

    Priority:
        1. group_id on <filament> elements in slice_info.config (actual assignment)
        2. filament_nozzle_map in project_settings.config (user preference fallback)

    Both are mapped through physical_extruder_map to get MQTT extruder IDs (0=right, 1=left).

    Args:
        zf: An open ZipFile of the 3MF archive

    Returns:
        Dictionary mapping {slot_id: extruder_id} for dual-nozzle files,
        or None if single-nozzle, missing data, or parse error.
    """
    try:
        if "Metadata/project_settings.config" not in zf.namelist():
            return None

        content = zf.read("Metadata/project_settings.config").decode()
        data = json.loads(content)

        physical_extruder_map = data.get("physical_extruder_map")
        if not physical_extruder_map or len(physical_extruder_map) <= 1:
            return None  # Single-nozzle printer

        # Detect single-active-extruder: dual-nozzle 3MF with only one nozzle installed.
        # extruder_nozzle_stats format: ["Standard#1|High Flow#0", "Standard#0"]
        # Each entry = one extruder. #N = count of nozzles of that type (0 = none).
        active_extruders = []
        for stats_str in data.get("extruder_nozzle_stats") or []:
            nozzle_counts = [n.partition("#")[2] for n in stats_str.split("|")]
            active_extruders.append(1 if any(c not in ("0", "") for c in nozzle_counts) else 0)

        # Parse slice_info.config once — both the single-active shortcut and the
        # group_id path below read its filament elements. distinct_group_ids tells us
        # whether the slice genuinely uses more than one nozzle group.
        si_root = None
        distinct_group_ids: set[int] = set()
        if "Metadata/slice_info.config" in zf.namelist():
            si_content = zf.read("Metadata/slice_info.config").decode()
            si_root = ET.fromstring(si_content)
            for filament_elem in si_root.findall(".//filament"):
                group_id_str = filament_elem.get("group_id")
                if group_id_str is not None:
                    try:
                        distinct_group_ids.add(int(group_id_str))
                    except (ValueError, TypeError):
                        pass

        # Single-active-extruder shortcut: a dual-nozzle 3MF sliced with only one nozzle
        # installed maps every filament to that nozzle. Only valid when the slice uses a
        # single group — a multi-group slice (more than one distinct group_id) carries
        # authoritative per-filament nozzle assignment that must NOT be collapsed to one
        # nozzle, even if extruder_nozzle_stats under-reports the second nozzle (#1825).
        if active_extruders and sum(active_extruders) == 1 and len(distinct_group_ids) <= 1:
            # Only one extruder has nozzles - map all filaments to it directly
            nozzle_mapping: dict[int, int] = {}
            active_idx = active_extruders.index(1)
            target_extruder = int(physical_extruder_map[active_idx])
            if si_root is not None:
                for filament_elem in si_root.findall(".//filament"):
                    try:
                        nozzle_mapping[int(filament_elem.get("id"))] = target_extruder
                    except (ValueError, TypeError):
                        pass
            return nozzle_mapping or None

        # Priority 1: Use group_id from slice_info filament elements.
        # This reflects the actual slicer assignment (respects "Auto For Flush").
        nozzle_mapping: dict[int, int] = {}
        if si_root is not None:
            for filament_elem in si_root.findall(".//filament"):
                group_id_str = filament_elem.get("group_id")
                filament_id_str = filament_elem.get("id")
                if group_id_str is not None and filament_id_str:
                    try:
                        group_id = int(group_id_str)
                        slot_id = int(filament_id_str)
                        if group_id < len(physical_extruder_map):
                            nozzle_mapping[slot_id] = int(physical_extruder_map[group_id])
                    except (ValueError, TypeError, IndexError):
                        pass

        if nozzle_mapping:
            return nozzle_mapping

        # Priority 2: Fall back to filament_nozzle_map (user preference).
        # This is correct when the user manually assigned nozzles, but may be
        # wrong when the slicer overrides via "Auto For Flush".
        filament_nozzle_map = data.get("filament_nozzle_map")
        if not filament_nozzle_map:
            return None

        for i, slicer_ext_str in enumerate(filament_nozzle_map):
            slot_id = i + 1
            try:
                slicer_ext = int(slicer_ext_str)
                if slicer_ext < len(physical_extruder_map):
                    nozzle_mapping[slot_id] = int(physical_extruder_map[slicer_ext])
            except (ValueError, TypeError, IndexError):
                pass

        return nozzle_mapping if nozzle_mapping else None
    except Exception:
        return None


def extract_filament_usage_from_3mf(file_path: Path, plate_id: int | None = None) -> list[dict]:
    """Extract per-filament total usage from 3MF slice_info.config.

    This extracts the slicer-estimated total usage per filament slot,
    not the per-layer breakdown.

    Args:
        file_path: Path to the 3MF file
        plate_id: Optional plate index to filter for (for multi-plate files)

    Returns:
        List of filament usage dictionaries:
        [{"slot_id": 1, "used_g": 50.5, "type": "PLA", "color": "#FF0000"}, ...]
    """
    filament_usage = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return []

            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)

            if plate_id is not None:
                # Find the plate element with matching index
                for plate_elem in root.findall(".//plate"):
                    plate_index = None
                    for meta in plate_elem.findall("metadata"):
                        if meta.get("key") == "index":
                            try:
                                plate_index = int(meta.get("value", "0"))
                            except ValueError:
                                pass
                            break

                    if plate_index == plate_id:
                        for f in plate_elem.findall("filament"):
                            filament_id = f.get("id")
                            used_g = f.get("used_g", "0")
                            try:
                                used_amount = float(used_g)
                                if filament_id:
                                    filament_usage.append(
                                        {
                                            "slot_id": int(filament_id),
                                            "used_g": used_amount,
                                            "type": f.get("type", ""),
                                            "color": f.get("color", ""),
                                        }
                                    )
                            except (ValueError, TypeError):
                                pass
                        break
            else:
                # No plate_id specified - extract all filaments
                for f in root.findall(".//filament"):
                    filament_id = f.get("id")
                    used_g = f.get("used_g", "0")
                    try:
                        used_amount = float(used_g)
                        if filament_id:
                            filament_usage.append(
                                {
                                    "slot_id": int(filament_id),
                                    "used_g": used_amount,
                                    "type": f.get("type", ""),
                                    "color": f.get("color", ""),
                                }
                            )
                    except (ValueError, TypeError):
                        pass  # Skip filament entries with unparseable usage values

    except Exception:
        pass  # Return whatever usage data was collected before the error

    return filament_usage


def extract_bed_type_from_3mf(file_path: Path, plate_id: int | None = None) -> str | None:
    """Extract the build plate type (``curr_bed_type``) for a specific plate (#1281).

    ``archive.bed_type`` is captured at ingest time but is one value per archive
    (the first plate's ``curr_bed_type`` — see ``services/archive.py``). For a
    multi-plate 3MF where different plates target different beds (e.g. a 40-plate
    file mixing PEI + Engineering), the archive-level value lies. When a queue
    item or print modal targets a specific plate, this re-reads the 3MF and
    returns that plate's actual bed type.

    Args:
        file_path: Path to the 3MF file.
        plate_id: Plate index to filter for; if None, returns the first plate's
            ``curr_bed_type`` (matches the archive-level capture).

    Returns:
        Bed type string (e.g. "Textured PEI Plate"), or None if not found.
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return None

            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)

            for plate_elem in root.findall(".//plate"):
                plate_index = None
                bed_value: str | None = None
                for meta in plate_elem.findall("metadata"):
                    key = meta.get("key")
                    if key == "index":
                        try:
                            plate_index = int(meta.get("value", "0"))
                        except ValueError:
                            pass  # Skip plate with unparseable index
                    elif key == "curr_bed_type" and meta.get("value"):
                        bed_value = (meta.get("value") or "").strip()

                if plate_id is None:
                    # First plate wins when no plate_id is requested.
                    return bed_value
                if plate_index == plate_id:
                    return bed_value
    except Exception:
        pass  # Return None on any failure rather than raising — caller decides

    return None


def extract_project_filaments_from_3mf(zf: zipfile.ZipFile) -> list[dict]:
    """Project-wide AMS slot config from ``Metadata/project_settings.config``.

    Returns one dict per configured AMS slot in slot order (1-indexed), with
    ``type`` and ``color`` populated from the project's ``filament_type`` and
    ``filament_colour`` arrays. ``used_grams`` / ``used_meters`` are 0 because
    project_settings carries the configuration, not per-print usage — the
    fields exist for shape compatibility with the slice_info-derived list.

    The SliceModal needs this on **unsliced** project files: slice_info.config
    is empty until Bambu Studio has actually sliced the project, but the user
    can still pick filament profiles for a slice we're about to perform.
    """
    if "Metadata/project_settings.config" not in zf.namelist():
        return []
    try:
        proj = json.loads(zf.read("Metadata/project_settings.config").decode())
    except (ValueError, OSError):
        return []
    if not isinstance(proj, dict):
        return []
    types_arr = proj.get("filament_type") or []
    colors_arr = proj.get("filament_colour") or []
    slot_count = max(
        len(types_arr) if isinstance(types_arr, list) else 0,
        len(colors_arr) if isinstance(colors_arr, list) else 0,
    )
    out: list[dict] = []
    for i in range(slot_count):
        out.append(
            {
                "slot_id": i + 1,
                "type": types_arr[i] if i < len(types_arr) and isinstance(types_arr[i], str) else "",
                "color": colors_arr[i] if i < len(colors_arr) and isinstance(colors_arr[i], str) else "",
                "used_grams": 0,
                "used_meters": 0,
            }
        )
    return out


def expand_to_project_slots(zf: zipfile.ZipFile, used: list[dict]) -> list[dict]:
    """Widen a used-only filament list to one entry per project slot (#2712).

    ``used`` is the slice_info-derived list: only the slots whose G-code
    actually consumed filament, each carrying real usage figures. That is the
    right answer for print-time AMS matching, and the **wrong** one for the
    slice modal, because the list the modal builds is *positional* — index 0 is
    slot 1, all the way down to the ``filament_N.json`` parts handed to the
    slicer CLI. A source whose only used slot is 4 therefore produced a single
    dropdown whose pick the CLI bound to slot 1, leaving slot 4 — the one the
    model actually prints with — on whatever the source had baked in.

    Returns the project's slots in slot order, each flagged ``used_in_plate``.
    Rows present in ``used`` are kept whole, so their usage figures, resolved
    type/colour and ``tray_info_idx`` survive; the rest come from the project
    configuration with zero usage. A used slot beyond the project's slot count
    is appended rather than dropped — the caller asked for a superset, and
    silently losing the one slot that prints would be the original bug again.

    ``used`` comes back unchanged when the file carries no project settings to
    widen against: a narrower-than-ideal list still prints correctly, an
    invented one might not.
    """
    project = extract_project_filaments_from_3mf(zf)
    if not project:
        return used

    by_slot = {f["slot_id"]: f for f in used}
    out: list[dict] = []
    for slot in project:
        known = by_slot.pop(slot["slot_id"], None)
        if known is not None:
            known["used_in_plate"] = True
            out.append(known)
        else:
            slot["used_in_plate"] = False
            out.append(slot)
    # Anything slice_info reported that the project does not declare.
    for leftover in by_slot.values():
        leftover["used_in_plate"] = True
        out.append(leftover)
    out.sort(key=lambda f: f["slot_id"])
    return out


# paint_color attr on <triangle> elements (per-face filament painting).
_PAINT_COLOR_ATTR_RE = re.compile(rb'paint_color="([0-9A-Fa-f]+)"')
# Min share of painted triangles an extruder must cover to count as "used"
# — filters one-off paint edit artifacts.
_PAINT_NOISE_THRESHOLD = 0.05


def extract_plate_extruder_set_from_3mf(zf: zipfile.ZipFile, plate_id: int) -> set[int]:
    """Extruder/AMS slot indices (1-indexed) used by objects on ``plate_id``.

    Three sources are unioned because Bambu Studio splits per-object extruder
    info across THREE places depending on how the user assigned colours:

    1. ``model_settings.config`` — top-level ``<metadata key="extruder">``
       on each ``<object>`` (the "default extruder" for the whole object).
    2. ``model_settings.config`` — per-``<part>`` ``<metadata key="extruder">``
       overrides (used when the user split an object into multiple parts
       with distinct filaments).
    3. ``3D/Objects/object_*.model`` — ``paint_color`` attributes on
       individual ``<triangle>`` elements (used when the user "painted" a
       face with a different filament). Each nibble is a TriangleSelector
       tree node: ``0`` = unpainted leaf, ``F`` = branch, ``1``..``E`` = leaf
       painted with extruder N. A flat hex scan yields the set without
       decoding the tree.

    Without (3) the painted-face data is invisible: model_settings says
    every object on a multi-color plate uses extruder 1 by default but the
    actual print uses 3, 4, 12 etc. via face paint (#1150 follow-up).
    """
    if "Metadata/model_settings.config" not in zf.namelist():
        return set()
    try:
        root = ET.fromstring(zf.read("Metadata/model_settings.config").decode())
    except (ET.ParseError, OSError):
        return set()

    # Pass 1: object → set of extruders from XML metadata (sources 1 + 2)
    # plus the per-object .model file path so we can later scan source 3.
    object_extruders: dict[str, set[int]] = {}
    object_model_paths: dict[str, list[str]] = {}
    for obj_elem in root.findall(".//object"):
        obj_id = obj_elem.get("id")
        if not obj_id:
            continue
        extruders: set[int] = set()
        top = obj_elem.find("metadata[@key='extruder']")
        if top is not None:
            try:
                v = int(top.get("value", "0"))
                if v > 0:
                    extruders.add(v)
            except (ValueError, TypeError):
                pass
        for part_elem in obj_elem.findall(".//part"):
            part_ext = part_elem.find("metadata[@key='extruder']")
            if part_ext is None:
                continue
            try:
                v = int(part_ext.get("value", "0"))
                if v > 0:
                    extruders.add(v)
            except (ValueError, TypeError):
                pass
        object_extruders[obj_id] = extruders

    # Pass 2: 3dmodel.model maps each <object id="N"> to its component
    # .model file path(s). Strip xmlns prefixes so ElementTree finds the
    # attributes without namespace gymnastics.
    if "3D/3dmodel.model" in zf.namelist():
        try:
            raw = zf.read("3D/3dmodel.model").decode()
            stripped = re.sub(r'xmlns:?\w*="[^"]*"', "", raw)
            stripped = re.sub(r"<(/?)\w+:", r"<\1", stripped)
            stripped = re.sub(r" \w+:(\w+=)", r" \1", stripped)
            model_root = ET.fromstring(stripped)
            for obj_elem in model_root.findall(".//object"):
                oid = obj_elem.get("id")
                if not oid:
                    continue
                comps = obj_elem.find("components")
                if comps is None:
                    continue
                paths = []
                for c in comps.findall("component"):
                    p = c.get("path")
                    if p:
                        paths.append(p.lstrip("/"))
                if paths:
                    object_model_paths[oid] = paths
        except (ET.ParseError, OSError):
            pass  # No 3dmodel — paint scan just won't apply

    # Pass 3: scan paint_color attrs in each per-object .model file. Cache
    # by file path because two objects often share the same component tree.
    paint_cache: dict[str, set[int]] = {}

    def _scan_paint(path: str) -> set[int]:
        if path in paint_cache:
            return paint_cache[path]
        out: set[int] = set()
        if path not in zf.namelist():
            paint_cache[path] = out
            return out
        try:
            data = zf.read(path)
        except OSError:
            paint_cache[path] = out
            return out
        extruder_triangles: dict[int, int] = {}
        total_painted = 0
        for match in _PAINT_COLOR_ATTR_RE.finditer(data):
            total_painted += 1
            seen: set[int] = set()
            for ch in match.group(1):
                # Hex digit → 4-bit value. 0 = unpainted leaf, F = branch,
                # 1-E = leaf painted with extruder N.
                if ch in b"123456789":
                    seen.add(ch - 0x30)
                elif ch in b"ABCDEabcde":
                    seen.add((ch & 0x4F) - 0x37)
            for e in seen:
                extruder_triangles[e] = extruder_triangles.get(e, 0) + 1
        if total_painted > 0:
            cutoff = max(1, int(total_painted * _PAINT_NOISE_THRESHOLD))
            for ext, count in extruder_triangles.items():
                if count >= cutoff:
                    out.add(ext)
        paint_cache[path] = out
        return out

    # Walk plates — collect extruders for objects on the requested plate.
    used: set[int] = set()
    for plate_elem in root.findall(".//plate"):
        plater_id = None
        for meta in plate_elem.findall("metadata"):
            if meta.get("key") == "plater_id":
                try:
                    plater_id = int(meta.get("value", ""))
                except (ValueError, TypeError):
                    pass
                break
        if plater_id != plate_id:
            continue
        for inst in plate_elem.findall("model_instance"):
            for inst_meta in inst.findall("metadata"):
                if inst_meta.get("key") != "object_id":
                    continue
                obj_id = inst_meta.get("value")
                if not obj_id:
                    continue
                used.update(object_extruders.get(obj_id, set()))
                for path in object_model_paths.get(obj_id, []):
                    used.update(_scan_paint(path))
        break
    return used


def extract_support_filament_slots_from_3mf(zf: zipfile.ZipFile) -> set[int]:
    """Slots referenced by the process settings for support material.

    Supports aren't attached to object geometry — they're generated by
    the slicer's process pass — so :func:`extract_plate_extruder_set_from_3mf`,
    which walks per-object extruder metadata + paint_color triangles,
    doesn't see them. Callers that need the complete set of slots a
    plate print will exercise (e.g. the SliceModal's filament-
    substitution logic) must union this in — otherwise a support-only
    slot (typical PLA-model + PVA-support setup) looks "unused" and its
    user-picked profile gets silently overwritten with slot 1's,
    producing a single-material print (#1881).

    Returns the empty set when supports are disabled, ``support_filament``
    / ``support_interface_filament`` are 0 (== "same as model"), the
    project has no embedded settings, or the file isn't a valid 3MF.
    """
    if "Metadata/project_settings.config" not in zf.namelist():
        return set()
    try:
        cfg = json.loads(zf.read("Metadata/project_settings.config").decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return set()
    if not isinstance(cfg, dict):
        return set()
    # BambuStudio serialises bool config options as string "1"/"0" in
    # project_settings.config, but forks / older versions occasionally
    # write real booleans or ints — accept anything that isn't
    # unambiguously falsy.
    enable = cfg.get("enable_support")
    if enable in (False, 0, "0", "false", "False", "", None):
        return set()
    out: set[int] = set()
    for key in ("support_filament", "support_interface_filament"):
        raw = cfg.get(key)
        if raw is None:
            continue
        try:
            slot = int(raw)
        except (ValueError, TypeError):
            continue
        # Slot 0 means "same as model" — no dedicated slot to preserve.
        if slot > 0:
            out.add(slot)
    return out
