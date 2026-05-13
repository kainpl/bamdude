import hashlib
import json
import logging
import os
import re
import shutil
import zipfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from defusedxml import ElementTree as ET
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer

logger = logging.getLogger(__name__)


def _copy_and_fsync(src: Path, dst: Path, chunk_size: int = 1024 * 1024) -> None:
    """Copy ``src`` to ``dst`` with an explicit chunked read/write and fsync the dst.

    Replacement for ``shutil.copy2`` in the archive pipeline. ``shutil.copy2``
    uses Linux ``sendfile()``, which on some kernels/filesystems has returned
    a short count on the first call and truncated the destination for larger
    3MF uploads (#1032, observed on Raspberry Pi OS bookworm / armv7l). An
    explicit loop with ``fsync`` avoids that path and guarantees the dest
    bytes are on disk before the caller inspects them as a ZIP.
    """
    with src.open("rb") as rf, dst.open("wb") as wf:
        while True:
            buf = rf.read(chunk_size)
            if not buf:
                break
            wf.write(buf)
        wf.flush()
        os.fsync(wf.fileno())
    shutil.copystat(src, dst)


def resolve_display_stem(filename: str) -> str:
    """Return a clean human-readable stem from a 3MF/gcode filename (#1152).

    Bambu Studio's "Send to printer" dialog typically writes files like
    ``Plate_1.gcode.3mf`` (a sliced gcode payload wrapped in a 3MF container).
    The naive ``Path(filename).stem`` only drops the last suffix, leaving
    ``Plate_1.gcode`` — which then surfaces in the archive UI / timelapse
    name-match path as a confusing ``Plate_1.gcode`` rather than ``Plate_1``.

    Strip the recognised print-format suffixes in order (case-insensitive):

    - ``.gcode.3mf`` → bare stem (Bambu Studio FTP send)
    - ``.3mf``       → bare stem
    - ``.gcode``     → bare stem (rare standalone gcode upload)

    Anything else passes through ``Path(...).stem`` unchanged. Path components
    are stripped first so callers can pass either a basename or a full path.
    """
    name = Path(filename).name
    lower = name.lower()
    for suffix in (".gcode.3mf", ".3mf", ".gcode"):
        if lower.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def peek_plate_index_in_3mf(file_path: Path) -> int | None:
    """Return the plate index recorded inside a Bambu 3MF, or None (#1204).

    Reads only ``Metadata/slice_info.config`` to keep this cheap — used by
    the print-start callback to verify that the 3MF we just downloaded over
    FTP actually matches the plate the printer is running. The full
    ``ThreeMFParser`` does much more work and runs later inside
    ``ArchiveService``; this is a one-shot peek for the validation gate.
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return None
            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)
            plate = root.find(".//plate")
            if plate is None:
                return None
            for meta in plate.findall("metadata"):
                if meta.get("key") == "index":
                    value = meta.get("value")
                    if value:
                        try:
                            return int(value)
                        except ValueError:
                            return None
    except Exception:
        return None
    return None


_PLATE_SUFFIX_RE = re.compile(r"^(.*?)(\s*-\s*Plate\s+|_plate_)(\d+)$", re.IGNORECASE)


def swap_plate_suffix(name: str | None, target_plate: int) -> str | None:
    """Return ``name`` with its trailing plate number replaced, or None (#1204).

    Bambu Studio names multi-plate uploads ``"<Project> - Plate <N>"`` (and
    a lowercase ``"_plate_<N>"`` variant exists too). When MQTT
    ``subtask_name`` lags across consecutive plates of the same model the
    suffix points at the previous plate; swapping it gives us the correct
    upload to re-fetch from FTP. Returns ``None`` if no recognised suffix
    is present so the caller can fall through to the no-3MF archive path.
    """
    if not name:
        return None
    m = _PLATE_SUFFIX_RE.match(name)
    if not m:
        return None
    base, separator, _ = m.groups()
    return f"{base}{separator}{target_plate}"


class ThreeMFParser:
    """Parser for Bambu Lab 3MF files."""

    def __init__(self, file_path: Path, plate_number: int | None = None):
        self.file_path = file_path
        self.plate_number = plate_number  # Which plate was printed (1, 2, 3, etc.)
        self.metadata: dict = {}

    def parse(self) -> dict:
        """Extract metadata from 3MF file."""
        try:
            with zipfile.ZipFile(self.file_path, "r") as zf:
                self._parse_slice_info(zf)  # Now sets self.plate_number from slice_info
                self._parse_project_settings(zf)
                self._parse_gcode_header(zf)
                self._parse_3dmodel(zf)
                self._extract_thumbnail(zf)  # Uses correct plate_number for thumbnail

                # Enhance print_name with plate info if this is a multi-plate export
                plate_index = self.metadata.get("_plate_index")
                if plate_index and plate_index > 1:
                    # Append plate number to distinguish from other plates
                    existing_name = self.metadata.get("print_name", "")
                    if existing_name and f"Plate {plate_index}" not in existing_name:
                        self.metadata["print_name"] = f"{existing_name} - Plate {plate_index}"

                # ALWAYS prefer slice_info values - they contain ONLY filaments actually used in print
                # project_settings contains ALL configured filaments (AMS slots), not just used ones
                if self.metadata.get("_slice_filament_type"):
                    self.metadata["filament_type"] = self.metadata["_slice_filament_type"]
                if self.metadata.get("_slice_filament_color"):
                    self.metadata["filament_color"] = self.metadata["_slice_filament_color"]

                # Clean up internal keys
                self.metadata.pop("_slice_filament_type", None)
                self.metadata.pop("_slice_filament_color", None)
                self.metadata.pop("_plate_index", None)
        except Exception as e:
            # Return whatever metadata was extracted before the error, but
            # surface the failure so corrupted / truncated 3MF archives are
            # visible in support bundles (#1032).
            logger.warning(
                "ThreeMFParser: failed to parse %s: %s(%s) — returning partial metadata",
                self.file_path,
                type(e).__name__,
                e,
            )
        return self.metadata

    def _parse_slice_info(self, zf: zipfile.ZipFile):
        """Parse slice_info.config for print settings and printable objects."""
        try:
            if "Metadata/slice_info.config" in zf.namelist():
                content = zf.read("Metadata/slice_info.config").decode()
                root = ET.fromstring(content)

                # Extract printer_model_id from plate metadata
                # Format: <plate><metadata key="printer_model_id" value="C11" /></plate>
                for meta in root.findall(".//metadata"):
                    key = meta.get("key")
                    value = meta.get("value")
                    if key == "printer_model_id" and value:
                        from backend.app.utils.printer_models import normalize_printer_model_id

                        normalized = normalize_printer_model_id(value)
                        if normalized:
                            self.metadata["sliced_for_model"] = normalized
                        break

                # Find the plate element. Single-plate exports only have one,
                # but multi-plate containers carry every plate's metadata side
                # by side. When ``self.plate_number`` is set (caller knows
                # which plate ran) prefer the matching ``<plate>`` element so
                # print_time / weight / printable_objects / per-slot filament
                # usage all reflect the printed plate, not whatever happened
                # to be plate 1 in the container.
                plate = None
                if self.plate_number:
                    for candidate in root.findall(".//plate"):
                        for meta in candidate.findall("metadata"):
                            if meta.get("key") == "index":
                                try:
                                    if int(meta.get("value", "")) == self.plate_number:
                                        plate = candidate
                                        break
                                except ValueError:
                                    continue
                        if plate is not None:
                            break
                if plate is None:
                    plate = root.find(".//plate")

                if plate is not None:
                    # Extract metadata from plate element
                    for meta in plate.findall("metadata"):
                        key = meta.get("key")
                        value = meta.get("value")
                        if key == "index" and value:
                            # Extract plate index - this tells us which plate was exported
                            try:
                                extracted_index = int(value)
                                # Set plate_number if not already set from filename
                                if not self.plate_number:
                                    self.plate_number = extracted_index
                                # Store in metadata for print_name generation
                                self.metadata["_plate_index"] = extracted_index
                            except ValueError:
                                pass  # Skip non-numeric plate index
                        elif key == "prediction" and value:
                            self.metadata["print_time_seconds"] = int(value)
                        elif key == "weight" and value:
                            self.metadata["filament_used_grams"] = float(value)
                        elif key == "curr_bed_type" and value:
                            self.metadata["bed_type"] = value

                    # Extract printable objects for skip object functionality
                    # Objects are stored as <object identify_id="123" name="Part1" skipped="false" />
                    printable_objects = {}
                    for obj in plate.findall("object"):
                        identify_id = obj.get("identify_id")
                        name = obj.get("name")
                        skipped = obj.get("skipped", "false")

                        # Only include objects that are not pre-skipped
                        if identify_id and name and skipped.lower() != "true":
                            try:
                                printable_objects[int(identify_id)] = name
                            except ValueError:
                                pass  # Skip objects with non-numeric identify_id

                    if printable_objects:
                        self.metadata["printable_objects"] = printable_objects

                # Get filament info from filaments ACTUALLY USED in the print
                # slice_info has <filament id="1" type="PLA" color="#FFFFFF" used_g="100" />
                # Only include filaments where used_g > 0
                filaments = root.findall(".//filament")
                if filaments:
                    # Collect unique filament types and colors for filaments that are actually used
                    types = []
                    colors = []
                    for f in filaments:
                        # Check if this filament is actually used in the print
                        used_g = f.get("used_g", "0")
                        try:
                            used_amount = float(used_g)
                        except (ValueError, TypeError):
                            used_amount = 0

                        # Only include if used_g > 0 (filament is actually consumed)
                        if used_amount > 0:
                            ftype = f.get("type")
                            fcolor = f.get("color")
                            if ftype and ftype not in types:
                                types.append(ftype)
                            if fcolor and fcolor not in colors:
                                colors.append(fcolor)

                    if types:
                        self.metadata["_slice_filament_type"] = ", ".join(types)
                    if colors:
                        self.metadata["_slice_filament_color"] = ",".join(colors)

                    # Collect per-slot filament usage for tracking & notifications
                    filament_slots = []
                    for f in filaments:
                        slot_id = f.get("id")
                        used_g_str = f.get("used_g", "0")
                        try:
                            used_g = float(used_g_str)
                        except (ValueError, TypeError):
                            used_g = 0
                        if used_g > 0 and slot_id:
                            filament_slots.append(
                                {
                                    "slot_id": int(slot_id),
                                    "used_g": round(used_g, 2),
                                    "type": f.get("type", ""),
                                    "color": f.get("color", ""),
                                }
                            )
                    if filament_slots:
                        self.metadata["filament_slots"] = filament_slots
        except Exception:
            pass  # Skip unparseable slice_info metadata

    def _parse_project_settings(self, zf: zipfile.ZipFile):
        """Parse project settings for print configuration."""
        try:
            if "Metadata/project_settings.config" in zf.namelist():
                content = zf.read("Metadata/project_settings.config").decode()
                try:
                    data = json.loads(content)
                    self._extract_filament_info(data)
                    self._extract_print_settings(data)
                except json.JSONDecodeError:
                    pass  # Skip malformed project_settings JSON
        except Exception:
            pass  # Skip unreadable project settings file

    def _parse_gcode_header(self, zf: zipfile.ZipFile):
        """Parse G-code file header for total layer count and printer model."""
        try:
            # Look for plate_1.gcode or similar
            gcode_files = [f for f in zf.namelist() if f.endswith(".gcode")]
            if not gcode_files:
                return

            # Pick the actually-printed plate's gcode when known —
            # ``total_layers`` and ``printer_model`` differ between
            # plates of a multi-plate container (e.g. plate 1 might
            # be 200 layers, plate 5 might be 80). Falls back to the
            # first gcode entry when the requested plate isn't in
            # the container or no plate was specified.
            gcode_path = gcode_files[0]
            if self.plate_number:
                expected_suffix = f"plate_{self.plate_number}.gcode"
                preferred = next(
                    (n for n in gcode_files if n.lower().endswith(expected_suffix)),
                    None,
                )
                if preferred is not None:
                    gcode_path = preferred
            with zf.open(gcode_path) as f:
                header = f.read(4096).decode("utf-8", errors="ignore")

            # Look for "; total layer number: XX" pattern
            match = re.search(r";\s*total\s+layer\s+number[:\s]+(\d+)", header, re.IGNORECASE)
            if match:
                self.metadata["total_layers"] = int(match.group(1))

            # Look for printer_model in gcode header (fallback if not found in slice_info)
            # Format: "; printer_model = Bambu Lab X1 Carbon" or "; printer_model = X1C"
            if "sliced_for_model" not in self.metadata:
                match = re.search(r";\s*printer_model\s*=\s*(.+)", header, re.IGNORECASE)
                if match:
                    from backend.app.utils.printer_models import normalize_printer_model

                    raw_model = match.group(1).strip()
                    self.metadata["sliced_for_model"] = normalize_printer_model(raw_model)
        except Exception:
            pass  # G-code header parsing is best-effort; metadata may come from other sources

    def _extract_filament_info(self, data: dict):
        """Extract filament info, preferring non-support filaments."""
        try:
            filament_types = data.get("filament_type", [])
            filament_colors = data.get("filament_colour", [])
            filament_is_support = data.get("filament_is_support", [])

            if not filament_types:
                return

            # Collect all non-support filaments
            non_support_types = []
            non_support_colors = []

            for i, ftype in enumerate(filament_types):
                is_support = filament_is_support[i] if i < len(filament_is_support) else "0"
                if is_support == "0":
                    if ftype and ftype not in non_support_types:
                        non_support_types.append(ftype)
                    if i < len(filament_colors) and filament_colors[i]:
                        color = filament_colors[i]
                        if color not in non_support_colors:
                            non_support_colors.append(color)

            # Fallback to first filament if all are support
            if not non_support_types and filament_types:
                non_support_types = [filament_types[0]]
            if not non_support_colors and filament_colors:
                non_support_colors = [filament_colors[0]]

            # Store filament type(s)
            if non_support_types:
                self.metadata["filament_type"] = ", ".join(non_support_types)

            # Store all colors as comma-separated (for multi-color display)
            if non_support_colors:
                self.metadata["filament_color"] = ",".join(non_support_colors)

        except Exception:
            pass  # Filament info is optional; fall back to slice_info values

    def _extract_print_settings(self, data: dict):
        """Extract print settings from JSON config."""
        # gcode_label_objects: Orca writes this; Bambu Studio doesn't (it
        # emits label_object markers unconditionally) — so a missing field
        # means "Bambu, label_object on by default" → True. Coerce because
        # slicers store these as ``["1"]``, ``"1"``, bool, or int depending
        # on version.
        glo_raw = data.get("gcode_label_objects")
        glo = _coerce_bool(glo_raw)
        self.metadata["gcode_label_objects"] = True if glo is None else glo

        # exclude_object: present in both slicers — emit only when
        # interpretable, no fallback (per design: "значення без фаллбека").
        if "exclude_object" in data:
            eo = _coerce_bool(data["exclude_object"])
            if eo is not None:
                self.metadata["exclude_object"] = eo

        try:
            # Layer height - usually an array, get first value
            if "layer_height" in data:
                val = data["layer_height"]
                if isinstance(val, list) and val:
                    self.metadata["layer_height"] = float(val[0])
                elif isinstance(val, (int, float, str)):
                    self.metadata["layer_height"] = float(val)

            # Nozzle diameter
            if "nozzle_diameter" in data:
                val = data["nozzle_diameter"]
                if isinstance(val, list) and val:
                    self.metadata["nozzle_diameter"] = float(val[0])
                elif isinstance(val, (int, float, str)):
                    self.metadata["nozzle_diameter"] = float(val)

            # Bed temperature - first layer or regular
            for key in ["bed_temperature_initial_layer", "bed_temperature"]:
                if key in data:
                    val = data[key]
                    if isinstance(val, list) and val:
                        self.metadata["bed_temperature"] = int(float(val[0]))
                    elif isinstance(val, (int, float, str)):
                        self.metadata["bed_temperature"] = int(float(val))
                    break

            # Nozzle temperature
            for key in ["nozzle_temperature_initial_layer", "nozzle_temperature"]:
                if key in data:
                    val = data[key]
                    if isinstance(val, list) and val:
                        self.metadata["nozzle_temperature"] = int(float(val[0]))
                    elif isinstance(val, (int, float, str)):
                        self.metadata["nozzle_temperature"] = int(float(val))
                    break

            # Printer model (extract and normalize)
            if "printer_model" in data:
                from backend.app.utils.printer_models import normalize_printer_model

                self.metadata["sliced_for_model"] = normalize_printer_model(data["printer_model"])

            # Build plate type — only set from project_settings if slice_info didn't
            # already provide it (slice_info reflects the exported plate, so it's
            # the authoritative source on multi-plate 3MFs).
            if "bed_type" not in self.metadata and "curr_bed_type" in data:
                val = data["curr_bed_type"]
                if isinstance(val, str) and val.strip():
                    self.metadata["bed_type"] = val.strip()
        except Exception:
            pass  # Print settings are optional; missing values are left unset

    def _extract_settings_from_content(self, content: str):
        """Extract print settings from config content."""
        settings_map = {
            "layer_height": ("layer_height", float),
            "nozzle_diameter": ("nozzle_diameter", float),
            "bed_temperature": ("bed_temperature", int),
            "nozzle_temperature": ("nozzle_temperature", int),
        }

        for key, (search_key, converter) in settings_map.items():
            if key not in self.metadata:
                try:
                    # Try JSON format
                    if f'"{search_key}"' in content:
                        start = content.find(f'"{search_key}"')
                        value_start = content.find(":", start) + 1
                        value_end = content.find(",", value_start)
                        if value_end == -1:
                            value_end = content.find("}", value_start)
                        value = content[value_start:value_end].strip().strip('"')
                        self.metadata[key] = converter(value)
                except (ValueError, TypeError):
                    pass  # Skip settings with unconvertible values

    def _parse_3dmodel(self, zf: zipfile.ZipFile):
        """Parse 3D/3dmodel.model for MakerWorld metadata."""
        try:
            model_path = "3D/3dmodel.model"
            if model_path not in zf.namelist():
                return

            content = zf.read(model_path).decode("utf-8", errors="ignore")

            # Parse XML metadata elements
            # MakerWorld adds metadata like: <metadata name="Designer">username</metadata>
            metadata_pattern = r'<metadata\s+name="([^"]+)"[^>]*>([^<]*)</metadata>'
            matches = re.findall(metadata_pattern, content)

            makerworld_fields = {}
            for name, value in matches:
                makerworld_fields[name] = value.strip()

            # Check for direct MakerWorld URL in content
            url_pattern = r'https?://makerworld\.com/[^\s<>"\']+/models/(\d+)'
            url_match = re.search(url_pattern, content)
            if url_match:
                self.metadata["makerworld_url"] = url_match.group(0)
                self.metadata["makerworld_model_id"] = url_match.group(1)

            # Extract model ID from DSM reference in image URLs
            # Format: https://makerworld.bblmw.com/makerworld/model/DSM00000001275614/...
            # The numeric part (1275614) is the MakerWorld model ID
            if "makerworld_url" not in self.metadata:
                dsm_pattern = r"DSM0+(\d+)"
                dsm_match = re.search(dsm_pattern, content)
                if dsm_match:
                    model_id = dsm_match.group(1)
                    self.metadata["makerworld_url"] = f"https://makerworld.com/en/models/{model_id}"
                    self.metadata["makerworld_model_id"] = model_id

            # Store designer info
            if "Designer" in makerworld_fields:
                self.metadata["designer"] = makerworld_fields["Designer"]
            if "Title" in makerworld_fields:
                self.metadata["print_name"] = makerworld_fields["Title"]

        except Exception:
            pass  # MakerWorld/3dmodel metadata is optional

    def _extract_thumbnail(self, zf: zipfile.ZipFile):
        """Extract thumbnail image from 3MF.

        If a plate_number was specified, try to use that plate's thumbnail first.
        """
        thumbnail_paths = []

        # If a specific plate was printed, try that thumbnail first
        if self.plate_number:
            thumbnail_paths.append(f"Metadata/plate_{self.plate_number}.png")

        # Fallback to default paths
        thumbnail_paths.extend(
            [
                "Metadata/plate_1.png",
                "Metadata/thumbnail.png",
                "Metadata/model_thumbnail.png",
            ]
        )

        for thumb_path in thumbnail_paths:
            if thumb_path in zf.namelist():
                self.metadata["_thumbnail_data"] = zf.read(thumb_path)
                self.metadata["_thumbnail_ext"] = ".png"
                break


def _coerce_bool(value) -> bool | None:
    """Best-effort bool coercion for slicer config values.

    Bambu Studio + Orca store config values inconsistently: lists with one
    string element (``["1"]``), bare strings (``"1"`` / ``"true"``),
    booleans, or ints — sometimes mixing across versions of the same
    slicer. Returns None when the value is uninterpretable; callers
    decide whether to fall back to a default.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
        return None
    if isinstance(value, list) and value:
        return _coerce_bool(value[0])
    return None


def extract_printable_objects_from_3mf(
    data: bytes, plate_number: int | None = None, include_positions: bool = False
) -> dict[int, str] | dict[int, dict] | tuple[dict[int, dict], list | None]:
    """Extract printable objects from 3MF file bytes.

    This is a lightweight function used during print start to get the list
    of objects that can be skipped.

    Args:
        data: Raw bytes of the 3MF file
        plate_number: Which plate was printed (1-based), or None for first plate
        include_positions: If True, return tuple of (objects dict, bbox_all)

    Returns:
        If include_positions=False: Dictionary mapping identify_id (int) to object name (str)
        If include_positions=True: Tuple of (dict mapping identify_id to {name, x, y}, bbox_all list or None)
    """
    from io import BytesIO

    printable_objects: dict = {}
    bbox_all: list | None = None

    try:
        with zipfile.ZipFile(BytesIO(data), "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return printable_objects

            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)

            # Find the correct plate
            if plate_number:
                plate = root.find(f".//plate[@plate_idx='{plate_number}']")
                if plate is None:
                    plate = root.find(".//plate")
            else:
                plate = root.find(".//plate")

            if plate is None:
                return printable_objects

            # Get actual plate index from metadata (sliced files only have one plate)
            plate_idx = plate_number or 1
            for meta in plate.findall("metadata"):
                if meta.get("key") == "index":
                    try:
                        plate_idx = int(meta.get("value", "1"))
                    except ValueError:
                        pass  # Use default plate_idx if value is non-numeric
                    break

            # Load position data from plate_N.json if we need positions
            # Build a lookup by name - use list to handle duplicate names
            bbox_by_name: dict[str, list[list]] = {}
            if include_positions:
                plate_json_path = f"Metadata/plate_{plate_idx}.json"
                if plate_json_path in zf.namelist():
                    try:
                        plate_json = json.loads(zf.read(plate_json_path).decode())
                        # Get bbox_all - the bounding box of all objects (used for image bounds)
                        bbox_all = plate_json.get("bbox_all")
                        for bbox_obj in plate_json.get("bbox_objects", []):
                            obj_name = bbox_obj.get("name")
                            bbox = bbox_obj.get("bbox", [])
                            if obj_name and len(bbox) >= 4:
                                if obj_name not in bbox_by_name:
                                    bbox_by_name[obj_name] = []
                                bbox_by_name[obj_name].append(bbox)
                    except (json.JSONDecodeError, KeyError):
                        pass  # Position data is optional; objects will lack x/y coordinates

            # Extract objects from slice_info.config
            for obj in plate.findall("object"):
                identify_id = obj.get("identify_id")
                name = obj.get("name")
                skipped = obj.get("skipped", "false")

                if identify_id and name and skipped.lower() != "true":
                    try:
                        obj_id = int(identify_id)
                        if include_positions:
                            x, y = None, None
                            # Match by name - pop first bbox to handle duplicates
                            bboxes = bbox_by_name.get(name)
                            if bboxes:
                                bbox = bboxes.pop(0)
                                # Calculate center from bbox [x_min, y_min, x_max, y_max]
                                x = (bbox[0] + bbox[2]) / 2
                                y = (bbox[1] + bbox[3]) / 2
                            printable_objects[obj_id] = {"name": name, "x": x, "y": y}
                        else:
                            printable_objects[obj_id] = name
                    except ValueError:
                        pass  # Skip objects with non-numeric identify_id

    except Exception:
        pass  # Return empty dict if 3MF is corrupt or unreadable

    if include_positions:
        return printable_objects, bbox_all
    return printable_objects


def parse_plates_from_3mf(zf: zipfile.ZipFile) -> list[dict]:
    """Build the full per-plate metadata list for one 3MF.

    Returns a list of dicts ready for the ``/library/files/{id}/plates`` /
    ``/archives/{id}/plates`` response shape AND for caching in
    ``library_files.file_metadata['plates']`` /
    ``print_archives.extra_data['plates']``. The caller adds
    ``thumbnail_url`` (it depends on whether we're serving a library file
    or an archive) — everything else is computed here.

    Per-plate fields:
        ``index``, ``name``, ``objects`` (list of names),
        ``object_count``, ``has_thumbnail``,
        ``print_time_seconds``, ``filament_used_grams``,
        ``filaments`` (list of {slot_id, type, color, used_grams, used_meters}),
        ``printable_objects`` (dict[identify_id, name]),
        ``bbox_all`` (or None),
        ``gcode_label_objects`` (file-global, copied per-plate),
        ``exclude_object`` (file-global, copied per-plate).

    Returns ``[]`` when the 3MF has no recognisable plate metadata
    (corrupt / source-only without slicing).
    """
    namelist = zf.namelist()

    # Plate index discovery: prefer the gcode files (sliced 3MF), fall
    # back to the JSON / PNG metadata when the file is source-only.
    gcode_files = [n for n in namelist if n.startswith("Metadata/plate_") and n.endswith(".gcode")]
    plate_indices: list[int] = []
    if gcode_files:
        for gf in gcode_files:
            try:
                plate_indices.append(int(gf[15:-6]))  # strip "Metadata/plate_" + ".gcode"
            except ValueError:
                pass
    else:
        plate_re = re.compile(r"^Metadata/plate_(\d+)\.(json|png)$")
        seen: set[int] = set()
        for name in namelist:
            match = plate_re.match(name)
            if not match:
                continue
            # Skip the size-suffixed thumbnails ("plate_1_small.png" etc.).
            if "_small" in name or "no_light" in name:
                continue
            try:
                idx = int(match.group(1))
            except ValueError:
                continue
            if idx in seen:
                continue
            seen.add(idx)
            plate_indices.append(idx)

    if not plate_indices:
        return []

    plate_indices.sort()

    # model_settings.config: per-plate custom name + per-plate object id list.
    plate_names: dict[int, str] = {}
    plate_object_ids: dict[int, list[str]] = {}
    object_names_by_id: dict[str, str] = {}
    if "Metadata/model_settings.config" in namelist:
        try:
            model_content = zf.read("Metadata/model_settings.config").decode()
            model_root = ET.fromstring(model_content)
            for obj_elem in model_root.findall(".//object"):
                obj_id = obj_elem.get("id")
                if not obj_id:
                    continue
                name_meta = obj_elem.find("metadata[@key='name']")
                obj_name = name_meta.get("value") if name_meta is not None else None
                if obj_name:
                    object_names_by_id[obj_id] = obj_name
            for plate_elem in model_root.findall(".//plate"):
                plater_id: int | None = None
                plater_name: str | None = None
                for meta in plate_elem.findall("metadata"):
                    key = meta.get("key")
                    value = meta.get("value")
                    if key == "plater_id" and value:
                        try:
                            plater_id = int(value)
                        except ValueError:
                            pass
                    elif key == "plater_name" and value:
                        plater_name = value.strip()
                if plater_id is not None and plater_name:
                    plate_names[plater_id] = plater_name
                if plater_id is not None:
                    for instance_elem in plate_elem.findall("model_instance"):
                        for inst_meta in instance_elem.findall("metadata"):
                            if inst_meta.get("key") == "object_id":
                                obj_id = inst_meta.get("value")
                                if not obj_id:
                                    continue
                                plate_object_ids.setdefault(plater_id, [])
                                if obj_id not in plate_object_ids[plater_id]:
                                    plate_object_ids[plater_id].append(obj_id)
        except Exception:  # noqa: BLE001 — model_settings is optional, best-effort
            pass

    # slice_info.config: per-plate prediction (time), weight, filaments, objects.
    plate_metadata: dict[int, dict] = {}
    if "Metadata/slice_info.config" in namelist:
        try:
            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)
            for plate_elem in root.findall(".//plate"):
                plate_info: dict = {
                    "filaments": [],
                    "prediction": None,
                    "weight": None,
                    "name": None,
                    "objects": [],
                }
                plate_index: int | None = None
                for meta in plate_elem.findall("metadata"):
                    key = meta.get("key")
                    value = meta.get("value")
                    if key == "index" and value:
                        try:
                            plate_index = int(value)
                        except ValueError:
                            pass
                    elif key == "prediction" and value:
                        try:
                            plate_info["prediction"] = int(value)
                        except ValueError:
                            pass
                    elif key == "weight" and value:
                        try:
                            plate_info["weight"] = float(value)
                        except ValueError:
                            pass
                for filament_elem in plate_elem.findall("filament"):
                    filament_id = filament_elem.get("id")
                    filament_type = filament_elem.get("type", "")
                    filament_color = filament_elem.get("color", "")
                    used_g = filament_elem.get("used_g", "0")
                    used_m = filament_elem.get("used_m", "0")
                    try:
                        used_grams = float(used_g)
                    except (ValueError, TypeError):
                        used_grams = 0
                    if used_grams > 0 and filament_id:
                        plate_info["filaments"].append(
                            {
                                "slot_id": int(filament_id),
                                "type": filament_type,
                                "color": filament_color,
                                "used_grams": round(used_grams, 1),
                                "used_meters": float(used_m) if used_m else 0,
                            }
                        )
                plate_info["filaments"].sort(key=lambda x: x["slot_id"])
                for obj_elem in plate_elem.findall("object"):
                    obj_name = obj_elem.get("name")
                    if obj_name and obj_name not in plate_info["objects"]:
                        plate_info["objects"].append(obj_name)
                if plate_index is not None:
                    custom_name = plate_names.get(plate_index)
                    if custom_name:
                        plate_info["name"] = custom_name
                    elif plate_info["objects"]:
                        plate_info["name"] = plate_info["objects"][0]
                    plate_metadata[plate_index] = plate_info
        except (OSError, ET.ParseError):
            pass

    # plate_*.json: object names fallback when slice_info is missing/empty.
    plate_json_objects: dict[int, list[str]] = {}
    for name in namelist:
        match = re.match(r"^Metadata/plate_(\d+)\.json$", name)
        if not match:
            continue
        try:
            idx = int(match.group(1))
        except ValueError:
            continue
        try:
            payload = json.loads(zf.read(name).decode())
            bbox_objects = payload.get("bbox_objects", [])
            obj_names: list[str] = []
            for obj in bbox_objects:
                obj_name = obj.get("name") if isinstance(obj, dict) else None
                if obj_name and obj_name not in obj_names:
                    obj_names.append(obj_name)
            if obj_names:
                plate_json_objects[idx] = obj_names
        except Exception:  # noqa: BLE001 — fallback parse, best-effort
            continue

    # Skip-objects + label-object metadata in one zip-pass.
    skip_meta = parse_per_plate_skip_metadata(zf, plate_indices)
    global_glo = skip_meta["gcode_label_objects"]
    global_eo = skip_meta["exclude_object"]

    plates: list[dict] = []
    for idx in plate_indices:
        meta = plate_metadata.get(idx, {})
        has_thumbnail = f"Metadata/plate_{idx}.png" in namelist
        objects = meta.get("objects", [])
        if not objects:
            objects = plate_json_objects.get(idx, [])
        if not objects and plate_object_ids.get(idx):
            objects = [object_names_by_id.get(obj_id, f"Object {obj_id}") for obj_id in plate_object_ids.get(idx, [])]
        plate_name = meta.get("name")
        if not plate_name:
            plate_name = plate_names.get(idx)
        if not plate_name and objects:
            plate_name = objects[0]
        skip_plate = skip_meta["plates"].get(idx, {})
        printable_objects = skip_plate.get("printable_objects", {})
        # ``object_count`` reflects the count of physical INSTANCES on the
        # plate, not unique names. For multi-instance arrays (the same STL
        # cloned N times) the ``objects`` list is name-deduplicated and
        # collapses to one entry — using it as the count would lie. The
        # ``printable_objects`` dict is keyed by ``identify_id`` (the same
        # id space the firmware addresses via M623), so each clone gets
        # its own row and ``len(...)`` is the truthful instance count.
        # Fallback to ``len(objects)`` only for source-only / unsliced
        # 3MFs that have no identify_id metadata at all.
        if printable_objects:
            object_count = len(printable_objects)
        else:
            object_count = len(objects)
        plates.append(
            {
                "index": idx,
                "name": plate_name,
                "objects": objects,
                "object_count": object_count,
                "has_thumbnail": has_thumbnail,
                "print_time_seconds": meta.get("prediction"),
                "filament_used_grams": meta.get("weight"),
                "filaments": meta.get("filaments", []),
                "printable_objects": printable_objects,
                "bbox_all": skip_plate.get("bbox_all"),
                "gcode_label_objects": global_glo,
                "exclude_object": global_eo,
            }
        )
    return plates


def parse_per_plate_skip_metadata(zf: zipfile.ZipFile, plate_indices: list[int]) -> dict:
    """Extract skip-objects + label-object metadata for *every* plate in a 3MF.

    Used by the ``/library/files/{id}/plates`` and ``/archives/{id}/plates``
    endpoints to enrich the gallery payload — each plate gets its full
    ``printable_objects`` map (id → name), its ``bbox_all`` for UI overlays,
    and the file-global ``gcode_label_objects`` + ``exclude_object`` flags
    copied per plate (they live in ``project_settings.config`` and apply to
    the whole 3MF, but copying makes the per-plate UI logic simpler — no
    cross-referencing required).

    Single-pass over the ZIP: opens slice_info.config + project_settings.config
    + plate_N.json once each, never re-decodes per plate.

    Returns:
        ``{
            "plates": {plate_idx: {"printable_objects": dict[int,str],
                                    "bbox_all": list | None}},
            "gcode_label_objects": bool,
            "exclude_object": bool | None,
        }``
    """
    namelist = zf.namelist()
    out: dict = {"plates": {}, "gcode_label_objects": True, "exclude_object": None}

    # Global flags from project_settings.config (apply to whole 3MF).
    if "Metadata/project_settings.config" in namelist:
        try:
            content = zf.read("Metadata/project_settings.config").decode("utf-8", errors="replace")
            data = json.loads(content)
            glo = _coerce_bool(data.get("gcode_label_objects"))
            out["gcode_label_objects"] = True if glo is None else glo
            if "exclude_object" in data:
                eo = _coerce_bool(data["exclude_object"])
                if eo is not None:
                    out["exclude_object"] = eo
        except (json.JSONDecodeError, OSError, KeyError):
            pass  # Defaults already applied; missing/corrupt config is non-fatal.

    # Per-plate ``printable_objects`` (id → name) from slice_info.config.
    if "Metadata/slice_info.config" in namelist:
        try:
            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)
            for plate_elem in root.findall(".//plate"):
                # Resolve plate's own index — slice_info <plate><metadata key="index">.
                idx: int | None = None
                for meta in plate_elem.findall("metadata"):
                    if meta.get("key") == "index":
                        try:
                            idx = int(meta.get("value", ""))
                        except ValueError:
                            pass  # Plate without a usable index — skip below.
                        break
                if idx is None:
                    continue
                printable: dict[int, str] = {}
                for obj in plate_elem.findall("object"):
                    identify_id = obj.get("identify_id")
                    name = obj.get("name")
                    skipped = obj.get("skipped", "false")
                    if identify_id and name and skipped.lower() != "true":
                        try:
                            printable[int(identify_id)] = name
                        except ValueError:
                            pass  # Non-numeric identify_id — frontend can't M623 it anyway.
                out["plates"].setdefault(idx, {})["printable_objects"] = printable
        except (OSError, ET.ParseError):
            pass

    # Per-plate ``bbox_all`` from plate_N.json.
    for idx in plate_indices:
        plate_json_path = f"Metadata/plate_{idx}.json"
        if plate_json_path not in namelist:
            continue
        try:
            payload = json.loads(zf.read(plate_json_path).decode())
            bbox_all = payload.get("bbox_all")
            if bbox_all is not None:
                out["plates"].setdefault(idx, {})["bbox_all"] = bbox_all
        except (json.JSONDecodeError, OSError):
            pass

    # Defensive defaults so callers don't need to .get() each field.
    for idx in plate_indices:
        plate_dict = out["plates"].setdefault(idx, {})
        plate_dict.setdefault("printable_objects", {})
        plate_dict.setdefault("bbox_all", None)

    return out


def remove_swap_pending_event(archive: PrintArchive, event: str) -> bool:
    """Drop *event* from ``archive.extra_data['swap_macro_events_pending']``.

    Used by dispatch right after firing ``swap_mode_start`` and by
    ``on_print_complete`` right after firing ``swap_mode_change_table``
    so the pending list is a real checklist of what's left to do — not a
    static record of what was originally planned. The caller is responsible
    for committing the session.

    If removing *event* leaves the list empty, the key is dropped
    entirely so a future on_print_complete sees a clean ``extra_data``.

    Returns True iff *event* was present (i.e. caller has dirty state to
    commit). Returns False if the event was already absent (idempotent
    no-op — second call can't double-fire).
    """
    if not isinstance(archive.extra_data, dict):
        return False
    pending = archive.extra_data.get("swap_macro_events_pending")
    if not isinstance(pending, list) or event not in pending:
        return False
    remaining = [e for e in pending if e != event]
    merged = dict(archive.extra_data)
    if remaining:
        merged["swap_macro_events_pending"] = remaining
    else:
        merged.pop("swap_macro_events_pending", None)
    archive.extra_data = merged
    return True


def load_objects_from_archive_into_state(archive: PrintArchive, printer_id: int) -> bool:
    """Parse the archive's stored 3MF and push printable_objects into MQTT state.

    Used by ``main.on_print_start`` and the on-demand
    ``ArchiveDownloadRetryService`` so the skip-objects modal sees objects
    even on prints that started from the printer (where BamDude's initial
    FTP fetch may have failed and only succeeded on a later retry).

    State is reset unconditionally as soon as the function is called for a
    given archive — we're declaring "this is the current print on
    *printer_id*". Stale ``printable_objects`` from the prior print would
    otherwise misreport the count and let the frontend show the skip
    dialog with object IDs that don't exist in the new gcode (the firmware
    would reject the resulting ``M623`` with "Invalid object IDs"). If the
    parser yields nothing (Orca with ``support_skip_objects`` disabled, a
    corrupt 3MF, or a non-3MF archive), the state stays empty — the
    skip-objects button stays hidden, which is the truthful UI for "we
    don't know the object list for this print".

    Returns True iff non-empty objects were loaded.
    """
    # Local import — printer_manager pulls in archive_download_retry which
    # in turn would close a load-time cycle if we imported at module top.
    from backend.app.services.printer_manager import printer_manager

    client = printer_manager.get_client(printer_id)
    if client is not None:
        client.state.printable_objects = {}
        client.state.printable_objects_bbox_all = None
        client.state.skipped_objects = []
        client.state.skip_objects_supported = False

    try:
        if not archive.file_path:
            return False
        file_path = settings.base_dir / archive.file_path
        if not file_path.is_file() or not str(file_path).endswith(".3mf"):
            return False
        with open(file_path, "rb") as f:
            threemf_data = f.read()
        result = extract_printable_objects_from_3mf(threemf_data, include_positions=True)
        # Function returns either dict or (dict, bbox_all) depending on flag —
        # we always pass include_positions=True so unpack accordingly.
        printable_objects, bbox_all = (
            result if isinstance(result, tuple) else (result, None)  # type: ignore[misc]
        )
        if not printable_objects:
            return False
        if client is None:
            return False
        client.state.printable_objects = printable_objects
        client.state.printable_objects_bbox_all = bbox_all

        # skip_objects_supported gate: requires BOTH flags true in
        # archive.extra_data. Strict — None / missing → False (hide
        # the button). For Bambu Studio files the parser defaults
        # gcode_label_objects to True at extraction time; exclude_object
        # is only stored when interpretable. Old archives without the
        # m022 backfill will fail this gate cleanly (button hidden, no
        # firmware-reject surprises on click).
        meta = archive.extra_data if isinstance(archive.extra_data, dict) else {}
        glo = meta.get("gcode_label_objects")
        eo = meta.get("exclude_object")
        client.state.skip_objects_supported = bool(glo) and bool(eo)

        logger.info(
            "Loaded %s printable objects for printer %s from archive %s (skip_objects_supported=%s)",
            len(printable_objects),
            printer_id,
            archive.id,
            client.state.skip_objects_supported,
        )
        return True
    except Exception as e:
        logger.debug("Failed to extract printable objects from archive %s: %s", archive.id, e)
        return False


class ProjectPageParser:
    """Parser for extracting project page data from Bambu Lab 3MF files."""

    def __init__(self, file_path: Path):
        self.file_path = file_path

    def parse(self, archive_id: int) -> dict:
        """Extract project page metadata and images from 3MF file."""
        import html

        result = {
            "title": None,
            "description": None,
            "designer": None,
            "designer_user_id": None,
            "license": None,
            "copyright": None,
            "creation_date": None,
            "modification_date": None,
            "origin": None,
            "profile_title": None,
            "profile_description": None,
            "profile_cover": None,
            "profile_user_id": None,
            "profile_user_name": None,
            "design_model_id": None,
            "design_profile_id": None,
            "design_region": None,
            "model_pictures": [],
            "profile_pictures": [],
            "thumbnails": [],
        }

        try:
            with zipfile.ZipFile(self.file_path, "r") as zf:
                # Parse 3D/3dmodel.model for metadata
                model_path = "3D/3dmodel.model"
                if model_path in zf.namelist():
                    content = zf.read(model_path).decode("utf-8", errors="ignore")

                    # Extract metadata elements using regex
                    # Format: <metadata name="Key">Value</metadata> or <metadata name="Key" />
                    metadata_pattern = r'<metadata\s+name="([^"]+)"[^>]*>([^<]*)</metadata>'
                    matches = re.findall(metadata_pattern, content)

                    field_mapping = {
                        "Title": "title",
                        "Description": "description",
                        "Designer": "designer",
                        "DesignerUserId": "designer_user_id",
                        "License": "license",
                        "Copyright": "copyright",
                        "CreationDate": "creation_date",
                        "ModificationDate": "modification_date",
                        "Origin": "origin",
                        "ProfileTitle": "profile_title",
                        "ProfileDescription": "profile_description",
                        "ProfileCover": "profile_cover",
                        "ProfileUserId": "profile_user_id",
                        "ProfileUserName": "profile_user_name",
                        "DesignModelId": "design_model_id",
                        "DesignProfileId": "design_profile_id",
                        "DesignRegion": "design_region",
                    }

                    for name, value in matches:
                        if name in field_mapping:
                            # Decode HTML entities multiple times (content is often triple-encoded)
                            decoded = value.strip()
                            prev = None
                            while prev != decoded:
                                prev = decoded
                                decoded = html.unescape(decoded)
                            # Normalize non-breaking spaces to regular spaces
                            decoded = decoded.replace("\xa0", " ")
                            result[field_mapping[name]] = decoded if decoded else None

                # List images in Auxiliaries folder
                from urllib.parse import quote

                for name in zf.namelist():
                    if name.startswith("Auxiliaries/Model Pictures/"):
                        filename = name.split("/")[-1]
                        if filename:
                            result["model_pictures"].append(
                                {
                                    "name": filename,
                                    "path": name,
                                    "url": f"/api/v1/archives/{archive_id}/project-image/{quote(name, safe='')}",
                                }
                            )
                    elif name.startswith("Auxiliaries/Profile Pictures/"):
                        filename = name.split("/")[-1]
                        if filename:
                            result["profile_pictures"].append(
                                {
                                    "name": filename,
                                    "path": name,
                                    "url": f"/api/v1/archives/{archive_id}/project-image/{quote(name, safe='')}",
                                }
                            )
                    elif name.startswith("Auxiliaries/.thumbnails/"):
                        filename = name.split("/")[-1]
                        if filename:
                            result["thumbnails"].append(
                                {
                                    "name": filename,
                                    "path": name,
                                    "url": f"/api/v1/archives/{archive_id}/project-image/{quote(name, safe='')}",
                                }
                            )

        except Exception as e:
            result["_error"] = str(e)

        return result

    def get_image(self, image_path: str) -> tuple[bytes, str] | None:
        """Extract an image from the 3MF file.

        Returns tuple of (image_data, content_type) or None if not found.
        """
        try:
            with zipfile.ZipFile(self.file_path, "r") as zf:
                if image_path in zf.namelist():
                    data = zf.read(image_path)
                    # Determine content type from extension
                    ext = image_path.lower().split(".")[-1]
                    content_types = {
                        "png": "image/png",
                        "jpg": "image/jpeg",
                        "jpeg": "image/jpeg",
                        "webp": "image/webp",
                        "gif": "image/gif",
                    }
                    content_type = content_types.get(ext, "application/octet-stream")
                    return (data, content_type)
        except Exception:
            pass  # Return None if image cannot be extracted from 3MF
        return None

    def update_metadata(self, updates: dict) -> bool:
        """Update project page metadata in the 3MF file.

        Args:
            updates: Dict with fields to update (title, description, designer, etc.)

        Returns:
            True if successful, False otherwise.
        """
        import html
        import tempfile

        try:
            # Read the 3MF file
            with zipfile.ZipFile(self.file_path, "r") as zf_read:
                # Find and read the 3dmodel.model file
                model_path = "3D/3dmodel.model"
                if model_path not in zf_read.namelist():
                    return False

                content = zf_read.read(model_path).decode("utf-8")

                # Update metadata fields
                field_mapping = {
                    "title": "Title",
                    "description": "Description",
                    "designer": "Designer",
                    "license": "License",
                    "copyright": "Copyright",
                    "profile_title": "ProfileTitle",
                    "profile_description": "ProfileDescription",
                }

                for field, xml_name in field_mapping.items():
                    if field in updates and updates[field] is not None:
                        new_value = html.escape(updates[field])
                        # Replace existing metadata or we'd need to add it
                        pattern = rf'(<metadata\s+name="{xml_name}"[^>]*>)[^<]*(</metadata>)'
                        replacement = rf"\g<1>{new_value}\g<2>"
                        content = re.sub(pattern, replacement, content)

                # Write to a temporary file first
                with tempfile.NamedTemporaryFile(delete=False, suffix=".3mf") as tmp:
                    tmp_path = Path(tmp.name)

                # Create new zip with updated content
                with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_write:
                    for item in zf_read.namelist():
                        if item == model_path:
                            zf_write.writestr(item, content.encode("utf-8"))
                        else:
                            zf_write.writestr(item, zf_read.read(item))

            # Replace original file with updated one
            shutil.move(tmp_path, self.file_path)
            return True

        except Exception:
            # Clean up temp file if it exists
            if "tmp_path" in locals() and tmp_path.exists():
                tmp_path.unlink()
            return False


class ArchiveService:
    """Service for archiving print jobs."""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def compute_file_hash(file_path: Path) -> str:
        """Compute SHA256 hash of a file for duplicate detection."""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            # Read in chunks to handle large files
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    async def get_duplicate_hashes_and_names(self) -> tuple[set[str], set[tuple[str, str]]]:
        """Get all effective hashes and (print name, hash) pairs that appear more than once.

        Uses ``COALESCE(source_content_hash, content_hash)`` as the effective
        hash so BamDude-patched archives collapse against their original
        source. External prints (no source hash) fall back to raw content
        hash — same behaviour as before m009.

        Returns a tuple of (duplicate_hashes, duplicate_name_hash_pairs).
        """
        from sqlalchemy import func

        effective_hash = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)

        # Trashed archives are excluded — a duplicate badge against a trashed
        # sibling is misleading; those rows are slated for hard-delete.
        result = await self.db.execute(
            select(effective_hash)
            .where(PrintArchive.content_hash.isnot(None), PrintArchive.deleted_at.is_(None))
            .group_by(effective_hash)
            .having(func.count(PrintArchive.id) > 1)
        )
        duplicate_hashes = {row[0] for row in result.all()}

        # Find print names that have multiple archives with the SAME effective hash
        # This avoids marking different files with the same name as duplicates
        result = await self.db.execute(
            select(func.lower(PrintArchive.print_name), effective_hash)
            .where(
                PrintArchive.print_name.isnot(None),
                PrintArchive.content_hash.isnot(None),
                PrintArchive.deleted_at.is_(None),
            )
            .group_by(func.lower(PrintArchive.print_name), effective_hash)
            .having(func.count(PrintArchive.id) > 1)
        )
        duplicate_name_hash_pairs = {(row[0], row[1]) for row in result.all()}

        return duplicate_hashes, duplicate_name_hash_pairs

    async def find_duplicates(
        self,
        archive_id: int,
        content_hash: str | None = None,
        print_name: str | None = None,
        makerworld_model_id: str | None = None,
    ) -> list[dict]:
        """Find duplicate archives based on hash or name matching.

        Returns list of dicts with id, print_name, created_at, match_type.
        """
        duplicates = []

        # First, find exact matches by effective hash (source_content_hash or
        # content_hash). This groups BamDude-patched archives with their
        # library originals; external prints still dedup by raw content_hash.
        if content_hash:
            effective_hash = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)
            result = await self.db.execute(
                select(PrintArchive)
                .where(
                    and_(
                        effective_hash == content_hash,
                        PrintArchive.id != archive_id,
                        PrintArchive.deleted_at.is_(None),
                    )
                )
                .order_by(PrintArchive.created_at.desc())
                .limit(10)
            )
            for archive in result.scalars().all():
                duplicates.append(
                    {
                        "id": archive.id,
                        "print_name": archive.print_name,
                        "created_at": archive.created_at,
                        "match_type": "exact",
                    }
                )

        # Then, find similar matches by print name or MakerWorld ID
        # Prefer strict name+hash matching when hash exists; fallback to name-only for legacy/manual
        # archives that may not have a content_hash.
        if print_name or makerworld_model_id:
            conditions = [PrintArchive.id != archive_id, PrintArchive.deleted_at.is_(None)]

            name_conditions = []
            if print_name:
                if content_hash:
                    # Match if print names are similar AND share the same effective hash
                    # (chain-coalesced so a patched re-print of the same library file is
                    # treated as the same file even though its raw content_hash differs).
                    name_conditions.append(
                        and_(
                            PrintArchive.print_name.ilike(print_name),
                            func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash) == content_hash,
                        )
                    )
                else:
                    # Fallback for archives without hash data: match by print name only.
                    name_conditions.append(PrintArchive.print_name.ilike(print_name))
            if makerworld_model_id:
                # Match by MakerWorld model ID stored in extra_data (same design from MakerWorld)
                # Use json_extract for SQLite compatibility (astext is PostgreSQL-only)
                name_conditions.append(
                    func.json_extract(PrintArchive.extra_data, "$.makerworld_model_id") == str(makerworld_model_id)
                )

            if name_conditions:
                conditions.append(or_(*name_conditions))

                result = await self.db.execute(
                    select(PrintArchive).where(and_(*conditions)).order_by(PrintArchive.created_at.desc()).limit(10)
                )
                for archive in result.scalars().all():
                    # Don't add if already in duplicates (exact match)
                    if not any(d["id"] == archive.id for d in duplicates):
                        duplicates.append(
                            {
                                "id": archive.id,
                                "print_name": archive.print_name,
                                "created_at": archive.created_at,
                                "match_type": "similar",
                            }
                        )

        return duplicates

    async def archive_print(
        self,
        printer_id: int | None,
        source_file: Path,
        print_data: dict | None = None,
        created_by_id: int | None = None,
        original_filename: str | None = None,
        project_id: int | None = None,
        *,
        source_content_hash: str | None = None,
        applied_patches: list[str] | None = None,
        subtask_id: str | None = None,
        library_file_id: int | None = None,
        swap_macro_events_pending: list[str] | None = None,
        prefer_filename_for_name: bool = False,
        plate_index: int | None = None,
        dispatched_file: Path | None = None,
    ) -> PrintArchive | None:
        """Archive a 3MF file with metadata.

        Args:
            printer_id: ID of the printer (optional)
            source_file: Path to the original (unpatched) 3MF — chain root.
                Used for ``source_content_hash`` (when not passed explicitly),
                for filename / stem / suffix derivation, and for fallback when
                ``dispatched_file`` is not provided.
            dispatched_file: Path to the EXACT bytes that went to the printer
                (post-patcher). When provided this controls what gets hashed
                into ``content_hash`` and copied into the archive folder, so
                restart-recovery in ``on_print_start`` can match the archive
                by hashing the printer's copy. ``None`` means no patching
                happened and ``content_hash == source_content_hash`` —
                identical to the legacy single-file flow.
            print_data: Print data from MQTT (optional)
            created_by_id: User ID who created this archive (optional, for user tracking)
            original_filename: Original human-readable filename (optional, for library files
                stored with UUID names)
            source_content_hash: SHA256 of the UNPATCHED source file, when the
                caller (BamDude dispatch) knows it. None for external prints.
            applied_patches: Patch identifiers applied by the dispatch pipeline
                before upload. None for external prints.
            subtask_id: Printer-assigned subtask identifier from MQTT push_status,
                captured by on_print_start when available. Advisory pre-check
                key in later resume attempts (#972).
            library_file_id: ID of the ``library_files`` row this print was
                dispatched from, when BamDude drove the dispatch. None for
                external / direct SD / reprint-from-archive paths; m014 later
                backfills those by hash where possible.
            prefer_filename_for_name: When True, use the uploaded filename stem as
                the archive's display name even if the 3MF embeds a `print_name`
                in its metadata. Used by virtual-printer flows so users who rename
                a job in BambuStudio's "send to printer" dialog see that name
                instead of the creator-baked title (#1152, audit B.14).
        """
        # Verify printer exists if specified
        if printer_id is not None:
            result = await self.db.execute(select(Printer).where(Printer.id == printer_id))
            printer = result.scalar_one_or_none()
            if not printer:
                return None

        # Two distinct hashes, two distinct purposes:
        # - ``content_hash`` = SHA256 of the *dispatched* bytes (post-patch,
        #   what FTP sent to the printer). Drives ``on_print_start``'s
        #   restart-recovery query: it pulls the printer's copy back over
        #   FTP, hashes it, and looks for ``content_hash == temp_hash``.
        #   Must be the patched hash for that to match.
        # - ``source_content_hash`` (set further down via ``chain_lookup``
        #   or as ``content_hash`` when the row is its own chain root) =
        #   SHA256 of the unpatched original. Drives chain-of-custody
        #   grouping and file-on-disk dedup.
        # On disk we keep the ORIGINAL (unpatched) bytes — that way reprint
        # can re-run the patcher against a clean source and toggle
        # mesh_mode_fast_check / gcode injection in either direction. The
        # patcher's M970 regex only matches *uncommented* lines, so a
        # patched-on-disk source could never have an earlier patch undone.
        bytes_for_disk: Path = source_file
        hashed_bytes: Path = dispatched_file if dispatched_file is not None else source_file
        content_hash = self.compute_file_hash(hashed_bytes)

        # External-print fallback: if the caller didn't provide source_content_hash
        # (i.e. print was initiated outside BamDude), try to link this archive to
        # an existing chain by looking for any prior archive that has either the
        # same content_hash (exact bytes on disk) or the same source_content_hash
        # (this file is itself the original someone patched before). The oldest
        # match wins — it's closest to the root of the chain.
        if source_content_hash is None:
            # Trashed archives are excluded — a chain anchor in the trash is
            # about to be hard-deleted, so reusing its hash would orphan us.
            chain_lookup = await self.db.execute(
                select(func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash))
                .where(
                    or_(
                        PrintArchive.content_hash == content_hash,
                        PrintArchive.source_content_hash == content_hash,
                    ),
                    PrintArchive.deleted_at.is_(None),
                )
                .order_by(PrintArchive.created_at.asc())
                .limit(1)
            )
            chain_hash = chain_lookup.scalar_one_or_none()
            if chain_hash:
                # Always inherit a chain hash when one exists, even when it
                # equals our content_hash — keeps the always-fill invariant
                # consistent regardless of whether a previous variant was
                # patched or unpatched.
                source_content_hash = chain_hash
            else:
                # Standalone-row case: no existing archive shares this hash.
                # Set source = content so this row becomes the chain root for
                # any future patched variant. Always-fill invariant: every
                # row written by this code path has source_content_hash set.
                source_content_hash = content_hash

        # File-on-disk dedup is on the chain-root hash (``effective_hash =
        # COALESCE(source_content_hash, content_hash)``) — every row that
        # shares the same unpatched origin shares the same on-disk file,
        # because that's exactly what we now write to disk regardless of
        # which patches the dispatcher applied. Two patched variants and
        # the unpatched original of the same source all collapse to one
        # disk copy. Cross-printer share works for free for the same
        # reason. ``delete_archive`` ref-counts shared ``file_path``s;
        # oldest match wins to keep the on-disk anchor stable.
        printer_folder = str(printer_id) if printer_id is not None else "unassigned"
        effective_hash = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)
        existing = await self.db.execute(
            select(PrintArchive)
            .where(
                effective_hash == source_content_hash,
                PrintArchive.file_path.isnot(None),
                PrintArchive.file_path != "",
                PrintArchive.deleted_at.is_(None),
            )
            .order_by(PrintArchive.created_at.asc())
            .limit(1)
        )
        existing_archive = existing.scalar_one_or_none()

        # `display_stem` is used below as a fallback for `print_name` when the
        # 3MF has no name metadata. Hoist it out of the if/else so the reuse
        # path (existing_archive) also has a valid value.
        display_stem = resolve_display_stem(original_filename if original_filename else source_file.name)

        if existing_archive and existing_archive.file_path:
            # Reuse existing file on disk
            dest_file = settings.base_dir / existing_archive.file_path
            archive_dir = dest_file.parent
            thumbnail_reuse = existing_archive.thumbnail_path
        else:
            # Create new archive directory and copy file. Belt-and-suspenders
            # uniqueness: timestamp is per-second, so theoretically a same-name
            # different-content print on the same printer in the same second
            # could land in an existing dir and overwrite it. In practice the
            # printer can't run two prints at once, but the suffix loop costs
            # nothing and removes the silent-overwrite footgun.
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_archive_name = f"{timestamp}_{display_stem}"
            archive_name = base_archive_name
            archive_dir = settings.archive_dir / printer_folder / archive_name
            suffix = 2
            while archive_dir.exists():
                archive_name = f"{base_archive_name}_{suffix}"
                archive_dir = settings.archive_dir / printer_folder / archive_name
                suffix += 1
            archive_dir.mkdir(parents=True)
            dest_file = archive_dir / source_file.name
            # Explicit fsync'd loop avoids the shutil.copy2 → sendfile short-read
            # quirk that silently truncated 3MF archives on some platforms (#1032).
            # ``bytes_for_disk`` is the post-patch file when the dispatcher
            # passed ``dispatched_file``; otherwise it's ``source_file``.
            _copy_and_fsync(bytes_for_disk, dest_file)

            # Verify the dest is a valid ZIP before going any further. Staying
            # quiet here is how #1032 escaped review — the archive row was
            # written but every later zipfile.ZipFile() call on the dest failed
            # with "File is not a zip file".
            if (
                source_file.suffix.lower() == ".3mf"
                and zipfile.is_zipfile(bytes_for_disk)
                and not zipfile.is_zipfile(dest_file)
            ):
                try:
                    src_size = bytes_for_disk.stat().st_size
                    dst_size = dest_file.stat().st_size
                except OSError:
                    src_size = dst_size = -1
                logger.error(
                    "Archive copy corrupted 3MF: src=%s (%s bytes, valid ZIP) -> dst=%s "
                    "(%s bytes, NOT a ZIP). Refusing to create archive row.",
                    bytes_for_disk,
                    src_size,
                    dest_file,
                    dst_size,
                )
                # Narrow cleanup: remove only the truncated file and the archive
                # directory if it's now empty. The dir is freshly created (the
                # suffix loop above guarantees no pre-existing collision), so
                # rmdir is safe — but keep it gated on emptiness as defence in
                # depth in case any future caller adds files before this point.
                try:
                    dest_file.unlink()
                except OSError:
                    pass
                try:
                    archive_dir.rmdir()
                except OSError:
                    pass  # directory not empty — leave untouched
                return None

            thumbnail_reuse = None

        # Extract plate number from filename (e.g., "plate_5" from "/data/Metadata/plate_5.gcode")
        plate_number = None
        if print_data:
            filename = print_data.get("filename", "")
            match = re.search(r"plate_(\d+)", filename)
            if match:
                plate_number = int(match.group(1))

        # Resolve "which plate ran" BEFORE parsing so the parser's
        # thumbnail extraction + slice_info filtering both align with
        # the actually-printed plate. Priority:
        #   1. Caller-supplied (queue item / dispatch options) — what
        #      the user picked before the print started.
        #   2. ``plate_number`` from the printer's gcode filename
        #      ("Metadata/plate_N.gcode") — what MQTT saw on the wire.
        #   3. After parse(): ``parser.plate_number`` — slice_info
        #      single-plate-export fallback when neither (1) nor (2)
        #      was available.
        # The thumbnail + per-plate slice_info both honour the value
        # passed to the constructor, so a multi-plate container of
        # which plate 5 was printed produces an archive with plate 5's
        # thumbnail, print_time, weight, and per-slot filament usage —
        # not plate 1's.
        plate_for_parser = plate_index or plate_number

        # Parse 3MF metadata
        parser = ThreeMFParser(dest_file, plate_number=plate_for_parser)
        metadata = parser.parse()

        resolved_plate_index = plate_for_parser or parser.plate_number

        # Per-plate cache so the gallery / list endpoint doesn't reopen the
        # ZIP every time. Same idea as the library upload route.
        try:
            with zipfile.ZipFile(dest_file, "r") as _zfh:
                plates_payload = parse_plates_from_3mf(_zfh)
            if plates_payload:
                metadata["plates"] = plates_payload
                metadata["is_multi_plate"] = len(plates_payload) > 1
        except Exception as _pe:
            logger.debug("archive_print: per-plate parse failed (non-critical): %s", _pe)

        # Save thumbnail if present (reuse existing if file was deduped)
        thumbnail_path = thumbnail_reuse
        if "_thumbnail_data" in metadata:
            if not thumbnail_reuse:
                thumb_file = archive_dir / f"thumbnail{metadata['_thumbnail_ext']}"
                thumb_file.write_bytes(metadata["_thumbnail_data"])
                thumbnail_path = str(thumb_file.relative_to(settings.base_dir))
            del metadata["_thumbnail_data"]
            del metadata["_thumbnail_ext"]

        # Merge with print data from MQTT
        if print_data:
            metadata["_print_data"] = print_data

        # Persist swap-macro intent in the same INSERT as the rest of
        # metadata. ``on_print_complete`` reads this when its in-memory
        # ``_active_swap_config`` is empty (post-restart recovery) and
        # clears the key after firing the macro to keep idempotency.
        # Only meaningful when ``swap_mode_change_table`` is in the list
        # (the only event ``on_print_complete`` consults). Pre-stamping
        # at archive creation avoids a separate post-start_print UPDATE
        # that races the runtime-tracker write loop on SQLite's single
        # writer and times out under busy_timeout.
        if swap_macro_events_pending and "swap_mode_change_table" in swap_macro_events_pending:
            metadata["swap_macro_events_pending"] = list(swap_macro_events_pending)

        # Determine status and timestamps. Default `'completed'` covers the
        # path where on_print_complete archives a finished print without
        # passing print_data (rare, but defensive). The pre-0.4.2 fallback
        # was `'archived'` — used by the now-removed manual upload + VP
        # placeholder + pending_uploads approval routes (Audits 1+2+3) to
        # mark "uploaded but never printed" rows. After those writers are
        # gone there is no caller that wants `'archived'` as a default;
        # any caller that genuinely means "uploaded, never printed" must
        # pass `print_data={'status': 'archived'}` explicitly so the
        # intent is auditable at the call site.
        status = print_data.get("status", "completed") if print_data else "completed"
        started_at = datetime.now(timezone.utc) if status == "printing" else None
        completed_at = datetime.now(timezone.utc) if status in ("completed", "failed", "archived") else None

        # Calculate initial cost estimate from default setting.
        # This is a placeholder - usage_tracker.on_print_complete() will overwrite
        # archive.cost with the actual cost from spool.cost_per_kg later.
        cost = None
        filament_grams = metadata.get("filament_used_grams")
        if filament_grams:
            from backend.app.api.routes.settings import get_setting

            default_cost_setting = await get_setting(self.db, "default_filament_cost")
            default_cost_per_kg = float(default_cost_setting) if default_cost_setting else 25.0
            cost = round((filament_grams / 1000) * default_cost_per_kg, 2)

        # Calculate quantity from printable objects count
        # printable_objects is a dict of {identify_id: name} for non-skipped objects
        quantity = 1  # Default to 1
        printable_objects = metadata.get("printable_objects")
        if printable_objects and isinstance(printable_objects, dict):
            quantity = len(printable_objects)
            logger.debug("Auto-detected %s parts from 3MF printable objects", quantity)

        # Mirror the resolved plate index into ``extra_data['plate_id']`` so
        # the existing reader in ``queue_virtual.py`` finds it for VP-recreate
        # flows. Column is the source of truth; the JSON copy keeps the
        # legacy contract working without an extra reader rewrite.
        if resolved_plate_index is not None:
            metadata["plate_id"] = resolved_plate_index

        # Create archive record
        archive = PrintArchive(
            printer_id=printer_id,
            filename=original_filename or source_file.name,
            file_path=str(dest_file.relative_to(settings.base_dir)),
            file_size=dest_file.stat().st_size,
            content_hash=content_hash,
            source_content_hash=source_content_hash,
            applied_patches=json.dumps(applied_patches) if applied_patches else None,
            thumbnail_path=thumbnail_path,
            print_name=display_stem if prefer_filename_for_name else (metadata.get("print_name") or display_stem),
            print_time_seconds=metadata.get("print_time_seconds"),
            filament_used_grams=metadata.get("filament_used_grams"),
            filament_type=metadata.get("filament_type"),
            filament_color=metadata.get("filament_color"),
            layer_height=metadata.get("layer_height"),
            total_layers=metadata.get("total_layers"),
            nozzle_diameter=metadata.get("nozzle_diameter"),
            bed_temperature=metadata.get("bed_temperature"),
            bed_type=metadata.get("bed_type"),
            nozzle_temperature=metadata.get("nozzle_temperature"),
            sliced_for_model=metadata.get("sliced_for_model"),
            plate_index=resolved_plate_index,
            makerworld_url=metadata.get("makerworld_url"),
            designer=metadata.get("designer"),
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            cost=cost,
            quantity=quantity,
            extra_data=metadata,
            created_by_id=created_by_id,
            project_id=project_id,
            subtask_id=subtask_id,
            library_file_id=library_file_id,
        )

        self.db.add(archive)
        await self.db.commit()
        await self.db.refresh(archive)

        return archive

    async def attach_3mf_to_archive(
        self,
        archive_id: int,
        source_file: Path,
        original_filename: str | None = None,
    ) -> bool:
        """Fill in an empty/fallback archive with a 3MF that was recovered
        later (e.g. by the background download-retry service).

        Unlike :meth:`archive_print`, this updates an existing row in place
        instead of inserting a new one.  Use case: ``on_print_start`` could
        not download the 3MF at the time, so a fallback row was created
        with ``file_path=""`` + ``extra_data["no_3mf_available"]=True``;
        the retry service later manages to grab the file from SD.

        Does NOT touch ``status``, ``started_at``, ``completed_at``,
        ``project_id``, or ``created_by_id`` — those were set when the
        archive was originally created.

        Returns True on success, False on parse/copy failure.
        """
        result = await self.db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
        archive = result.scalar_one_or_none()
        if archive is None:
            return False

        try:
            content_hash = self.compute_file_hash(source_file)

            # Inherit chain root from any existing archive with matching
            # content/source hash — mirrors archive_print's chain_lookup.
            # Fallback archives are created with source_content_hash=NULL
            # (no dispatch context), so without this they'd never link to
            # the chain that already exists for the same source file.
            if archive.source_content_hash is None:
                chain_lookup = await self.db.execute(
                    select(func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash))
                    .where(
                        or_(
                            PrintArchive.content_hash == content_hash,
                            PrintArchive.source_content_hash == content_hash,
                        ),
                        PrintArchive.id != archive_id,
                        PrintArchive.deleted_at.is_(None),
                    )
                    .order_by(PrintArchive.created_at.asc())
                    .limit(1)
                )
                chain_hash = chain_lookup.scalar_one_or_none()
                # Always-fill invariant: source_content_hash is never NULL
                # for rows written by this code path. Inherit chain root if
                # any sibling exists, else seed with our own content_hash.
                archive.source_content_hash = chain_hash or content_hash

            printer_folder = str(archive.printer_id) if archive.printer_id is not None else "unassigned"
            display_stem = resolve_display_stem(original_filename if original_filename else source_file.name)
            dest_name = original_filename or source_file.name

            # Reuse the chain's on-disk file when one exists. Match on the
            # chain-root hash (``effective_hash = COALESCE(source_content_hash,
            # content_hash)``) — every row that shares an unpatched origin
            # also shares the same on-disk file because ``archive_print``
            # writes the unpatched source there. The bytes we just hashed
            # from the printer (``source_file``) are post-patch and would
            # disagree with what's on disk, but that's exactly the point:
            # ``content_hash`` stays as the FTP/restart-recovery key, while
            # ``file_path`` always points at the unpatched copy reprint can
            # safely re-feed to the patcher.
            effective_hash = func.coalesce(PrintArchive.source_content_hash, PrintArchive.content_hash)
            existing_with_file = await self.db.execute(
                select(PrintArchive)
                .where(
                    effective_hash == archive.source_content_hash,
                    PrintArchive.id != archive_id,
                    PrintArchive.file_path.isnot(None),
                    PrintArchive.file_path != "",
                    PrintArchive.deleted_at.is_(None),
                )
                .order_by(PrintArchive.created_at.asc())
                .limit(1)
            )
            existing_archive = existing_with_file.scalar_one_or_none()

            if existing_archive and existing_archive.file_path:
                # Reuse existing on-disk file. ``delete_archive`` ref-counts
                # shared paths so the file stays as long as any row refs it.
                dest_file = settings.base_dir / existing_archive.file_path
                archive_dir = dest_file.parent
                thumbnail_reuse = existing_archive.thumbnail_path
            else:
                # No existing copy — create a fresh archive_dir and copy
                # from the temp source. Suffix loop guards the
                # theoretical-only same-second collision.
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                base_archive_name = f"{timestamp}_{display_stem}"
                archive_name = base_archive_name
                archive_dir = settings.archive_dir / printer_folder / archive_name
                suffix = 2
                while archive_dir.exists():
                    archive_name = f"{base_archive_name}_{suffix}"
                    archive_dir = settings.archive_dir / printer_folder / archive_name
                    suffix += 1
                archive_dir.mkdir(parents=True)
                # Prefer the clean original_filename (e.g. "Swapmod_STL.gcode.3mf")
                # over the potentially-prefixed temp source_file name (e.g.
                # "cover_1_Swapmod_STL.gcode.3mf" when it came from the cover
                # endpoint's temp download).
                dest_file = archive_dir / dest_name
                # Same fsync'd loop as archive_print (#1032).
                _copy_and_fsync(source_file, dest_file)

                if (
                    source_file.suffix.lower() == ".3mf"
                    and zipfile.is_zipfile(source_file)
                    and not zipfile.is_zipfile(dest_file)
                ):
                    logger.error(
                        "attach_3mf_to_archive: copy corrupted 3MF for archive %s — refusing to attach",
                        archive_id,
                    )
                    try:
                        dest_file.unlink()
                    except OSError:
                        pass
                    try:
                        archive_dir.rmdir()
                    except OSError:
                        pass
                    return False
                thumbnail_reuse = None

            # Parse 3MF metadata (reuse the same parser as archive_print).
            # Pass the archive's recorded plate_index so per-plate
            # thumbnail + slice_info come from the actually-printed
            # plate, not whatever happened to be plate 1 in the
            # container. None for legacy/external rows where the index
            # is unknown — parser falls back to slice_info's first
            # plate, matching pre-m038 behaviour.
            parser = ThreeMFParser(dest_file, plate_number=archive.plate_index)
            metadata = parser.parse()

            # Per-plate cache populated alongside the rest of the metadata.
            try:
                with zipfile.ZipFile(dest_file, "r") as _zfh:
                    plates_payload = parse_plates_from_3mf(_zfh)
                if plates_payload:
                    metadata["plates"] = plates_payload
                    metadata["is_multi_plate"] = len(plates_payload) > 1
            except Exception as _pe:
                logger.debug("attach_3mf_to_archive: per-plate parse failed (non-critical): %s", _pe)

            thumbnail_path = None
            if thumbnail_reuse:
                # File-share branch: reuse the existing archive's thumbnail
                # path. It lives in the same shared directory so it's
                # already on disk and ref-counted via delete_archive's
                # file_path share check (the dir as a whole is kept while
                # any row points into it).
                thumbnail_path = thumbnail_reuse
                metadata.pop("_thumbnail_data", None)
                metadata.pop("_thumbnail_ext", None)
            elif "_thumbnail_data" in metadata:
                thumb_file = archive_dir / f"thumbnail{metadata['_thumbnail_ext']}"
                thumb_file.write_bytes(metadata["_thumbnail_data"])
                thumbnail_path = str(thumb_file.relative_to(settings.base_dir))
                del metadata["_thumbnail_data"]
                del metadata["_thumbnail_ext"]

            # Merge metadata into existing extra_data.  Preserve _print_data
            # (set at fallback creation with the MQTT start payload) and
            # drop the retry / no-3mf flags now that we have the file.
            merged_extra = dict(archive.extra_data or {})
            preserved_print_data = merged_extra.get("_print_data")
            merged_extra.update(metadata)
            if preserved_print_data is not None:
                merged_extra["_print_data"] = preserved_print_data
            merged_extra.pop("no_3mf_available", None)
            merged_extra.pop("download_retry_count", None)
            merged_extra.pop("download_next_retry", None)

            archive.filename = original_filename or source_file.name
            archive.file_path = str(dest_file.relative_to(settings.base_dir))
            archive.file_size = dest_file.stat().st_size
            archive.content_hash = content_hash
            archive.thumbnail_path = thumbnail_path
            archive.print_name = metadata.get("print_name") or display_stem
            archive.print_time_seconds = metadata.get("print_time_seconds")
            archive.filament_used_grams = metadata.get("filament_used_grams")
            archive.filament_type = metadata.get("filament_type")
            archive.filament_color = metadata.get("filament_color")
            archive.layer_height = metadata.get("layer_height")
            archive.total_layers = metadata.get("total_layers")
            archive.nozzle_diameter = metadata.get("nozzle_diameter")
            archive.bed_temperature = metadata.get("bed_temperature")
            archive.nozzle_temperature = metadata.get("nozzle_temperature")
            archive.sliced_for_model = metadata.get("sliced_for_model")
            archive.makerworld_url = metadata.get("makerworld_url")
            archive.designer = metadata.get("designer")
            archive.extra_data = merged_extra

            # Backfill cost + quantity — fallback creation seeded them with
            # NULL / 1, and without this the archive stays stuck there even
            # after the 3MF lands.  Mirrors the logic in archive_print().
            filament_grams = metadata.get("filament_used_grams")
            if filament_grams:
                from backend.app.api.routes.settings import get_setting

                default_cost_setting = await get_setting(self.db, "default_filament_cost")
                default_cost_per_kg = float(default_cost_setting) if default_cost_setting else 25.0
                archive.cost = round((filament_grams / 1000) * default_cost_per_kg, 2)

            printable_objects = metadata.get("printable_objects")
            if printable_objects and isinstance(printable_objects, dict):
                archive.quantity = len(printable_objects)

            # Swap-compatible detection by filename suffix — mirrors the
            # post-archive_print check in on_print_start. Fallback creation
            # defaulted swap_compatible=False, so without this backfill a
            # *.swap.3mf / *.swaps.3mf file landed via retry would stay
            # flagged as non-swap.
            fname_lower = (original_filename or source_file.name).lower()
            if fname_lower.endswith((".swap.3mf", ".swaps.3mf")) or ".swap." in fname_lower or ".swaps." in fname_lower:
                archive.swap_compatible = True

            # Link to the originating library file by content hash when we
            # didn't know it at fallback-creation time (on_print_start's FTP
            # miss path has no dispatch context). Mirrors m014's backfill
            # logic — oldest matching library row wins.
            if archive.library_file_id is None:
                from backend.app.models.library import LibraryFile

                match_hash = archive.source_content_hash or content_hash
                if match_hash:
                    lib_match = await self.db.execute(
                        select(LibraryFile.id)
                        .where(LibraryFile.file_hash == match_hash)
                        .order_by(LibraryFile.created_at.asc(), LibraryFile.id.asc())
                        .limit(1)
                    )
                    matched_id = lib_match.scalar_one_or_none()
                    if matched_id is not None:
                        archive.library_file_id = matched_id

            await self.db.commit()
            await self.db.refresh(archive)
            return True
        except Exception as e:
            logger.exception("attach_3mf_to_archive failed for archive %s: %s", archive_id, e)
            await self.db.rollback()
            return False

    async def get_archive(self, archive_id: int, *, include_trashed: bool = False) -> PrintArchive | None:
        """Get an archive by ID with relationships loaded.

        Trashed archives return None unless ``include_trashed=True`` so user-
        facing GET /archives/{id} 404s on trashed rows. Internal callers (e.g.
        the trash routes themselves, restore flows) pass the flag explicitly.
        """
        from sqlalchemy.orm import selectinload

        conditions = [PrintArchive.id == archive_id]
        if not include_trashed:
            conditions.append(PrintArchive.deleted_at.is_(None))
        result = await self.db.execute(
            select(PrintArchive)
            .options(selectinload(PrintArchive.created_by), selectinload(PrintArchive.project))
            .where(*conditions)
        )
        return result.scalar_one_or_none()

    async def update_archive_status(
        self,
        archive_id: int,
        status: str,
        completed_at: datetime | None = None,
        failure_reason: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Update the status of an archive.

        ``failure_reason`` is the short cause code (VARCHAR(100), e.g.
        "Filament runout"); ``error_message`` is the verbose diagnostic text
        (TEXT) carried over from the queue item on failure.
        """
        archive = await self.get_archive(archive_id)
        if not archive:
            return False

        archive.status = status
        if completed_at:
            archive.completed_at = completed_at
        if failure_reason:
            archive.failure_reason = failure_reason
        if error_message and not archive.error_message:
            archive.error_message = error_message

        await self.db.commit()
        return True

    async def list_archives(
        self,
        printer_id: int | None = None,
        project_id: int | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        search: str | None = None,
        collection: str | None = None,
        material: str | None = None,
        colors: list[str] | None = None,
        color_mode: str = "or",
        favorites_only: bool = False,
        hide_failed: bool = False,
        hide_duplicates: bool = False,
        tag: str | None = None,
        kind: str | None = None,
        sort_by: str = "date-desc",
        limit: int | None = 50,
        offset: int = 0,
    ) -> tuple[list[PrintArchive], int]:
        """List archives with server-side filtering, sorting and pagination.

        Returns (items, total_count).
        """
        from sqlalchemy.orm import selectinload

        # Trashed archives never appear in the main listing — they live in
        # the archive trash bin until restored or hard-deleted by the sweeper.
        filters = [PrintArchive.deleted_at.is_(None)]

        # Printer / project filters
        if printer_id:
            filters.append(PrintArchive.printer_id == printer_id)
        if project_id:
            filters.append(PrintArchive.project_id == project_id)

        # Date range
        if date_from:
            dt_from = datetime.combine(date_from, time.min, tzinfo=timezone.utc)
            filters.append(PrintArchive.created_at >= dt_from)
        if date_to:
            dt_to = datetime.combine(date_to, time.max, tzinfo=timezone.utc)
            filters.append(PrintArchive.created_at <= dt_to)

        # Search (LIKE on print_name and filename)
        if search and search.strip():
            search_term = f"%{search.strip()}%"
            filters.append(
                or_(
                    PrintArchive.print_name.ilike(search_term),
                    PrintArchive.filename.ilike(search_term),
                )
            )

        # Collection presets
        if collection:
            now = datetime.now(timezone.utc)
            if collection == "recent":
                filters.append(PrintArchive.created_at >= now - timedelta(hours=24))
            elif collection == "this-week":
                filters.append(PrintArchive.created_at >= now - timedelta(days=7))
            elif collection == "this-month":
                first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                filters.append(PrintArchive.created_at >= first_of_month)
            elif collection == "favorites":
                filters.append(PrintArchive.is_favorite == True)  # noqa: E712
            elif collection == "printed":
                # Any final-status archive — covers a print attempt regardless
                # of outcome. The narrower Failed collection covers the
                # failure subset.
                filters.append(PrintArchive.status.in_(["completed", "failed", "aborted", "cancelled", "stopped"]))
            elif collection == "failed":
                filters.append(PrintArchive.status.in_(["failed", "aborted"]))
            elif collection == "duplicates":
                # Subquery: content_hash values that appear more than once
                dup_hashes = (
                    select(PrintArchive.content_hash)
                    .where(PrintArchive.content_hash.isnot(None))
                    .group_by(PrintArchive.content_hash)
                    .having(func.count() > 1)
                    .scalar_subquery()
                )
                filters.append(PrintArchive.content_hash.in_(dup_hashes))

        # Material filter (comma-separated field)
        if material:
            filters.append(PrintArchive.filament_type.ilike(f"%{material}%"))

        # Color filter (comma-separated field, OR/AND mode)
        if colors:
            color_conditions = [PrintArchive.filament_color.ilike(f"%{c}%") for c in colors]
            if color_mode == "and":
                filters.extend(color_conditions)  # All must match
            else:
                filters.append(or_(*color_conditions))  # Any must match

        # Favorites toggle
        if favorites_only:
            filters.append(PrintArchive.is_favorite == True)  # noqa: E712

        # Hide failed
        if hide_failed:
            filters.append(PrintArchive.status.notin_(["failed", "aborted"]))

        # Tag filter (comma-separated field)
        if tag:
            filters.append(PrintArchive.tags.ilike(f"%{tag}%"))

        # Calibration kind filter. Archive is print-history-only (m062+), so
        # the old "gcode vs source-only" split lost meaning — every row has a
        # print attached. Now we split on the calibration flag the wizard sets.
        if kind == "calibration":
            filters.append(PrintArchive.is_calibration.is_(True))
        elif kind == "regular":
            filters.append(PrintArchive.is_calibration.is_(False))

        # Hide duplicates: keep only first occurrence per content_hash
        if hide_duplicates:
            # Subquery: earliest id per content_hash
            first_per_hash = (
                select(func.min(PrintArchive.id))
                .where(PrintArchive.content_hash.isnot(None))
                .group_by(PrintArchive.content_hash)
                .scalar_subquery()
            )
            filters.append(
                or_(
                    PrintArchive.content_hash.is_(None),
                    PrintArchive.id.in_(first_per_hash),
                )
            )

        # Sorting
        order_clause = PrintArchive.created_at.desc()  # default
        if sort_by == "date-asc":
            order_clause = PrintArchive.created_at.asc()
        elif sort_by == "name-asc":
            order_clause = func.coalesce(PrintArchive.print_name, PrintArchive.filename).asc()
        elif sort_by == "name-desc":
            order_clause = func.coalesce(PrintArchive.print_name, PrintArchive.filename).desc()
        elif sort_by == "size-desc":
            order_clause = PrintArchive.file_size.desc()
        elif sort_by == "size-asc":
            order_clause = PrintArchive.file_size.asc()

        # Total count (same filters, no limit/offset)
        count_query = select(func.count()).select_from(PrintArchive).where(*filters)
        total = (await self.db.execute(count_query)).scalar() or 0

        # Data query — limit=None means "no pagination, return all matching rows".
        query = (
            select(PrintArchive)
            .options(selectinload(PrintArchive.project), selectinload(PrintArchive.created_by))
            .where(*filters)
            .order_by(order_clause)
            .offset(offset)
        )
        if limit is not None:
            query = query.limit(limit)
        result = await self.db.execute(query)
        items = list(result.scalars().all())

        return items, total

    async def get_filter_options(self) -> dict:
        """Get distinct filter values for archive dropdowns."""
        # Materials
        mat_result = await self.db.execute(
            select(PrintArchive.filament_type)
            .where(PrintArchive.filament_type.isnot(None), PrintArchive.filament_type != "")
            .distinct()
        )
        raw_materials = [r[0] for r in mat_result.all()]
        # Flatten comma-separated values
        materials = sorted({m.strip() for raw in raw_materials for m in raw.split(",") if m.strip()})

        # Colors
        col_result = await self.db.execute(
            select(PrintArchive.filament_color)
            .where(PrintArchive.filament_color.isnot(None), PrintArchive.filament_color != "")
            .distinct()
        )
        raw_colors = [r[0] for r in col_result.all()]
        colors = sorted({c.strip() for raw in raw_colors for c in raw.split(",") if c.strip()})

        # Tags
        tag_result = await self.db.execute(
            select(PrintArchive.tags).where(PrintArchive.tags.isnot(None), PrintArchive.tags != "").distinct()
        )
        raw_tags = [r[0] for r in tag_result.all()]
        tags = sorted({t.strip() for raw in raw_tags for t in raw.split(",") if t.strip()})

        return {"materials": materials, "colors": colors, "tags": tags}

    async def delete_archive(self, archive_id: int) -> bool:
        """Delete an archive and its files."""
        archive = await self.get_archive(archive_id)
        if not archive:
            return False

        # Resolve the directory to delete BEFORE committing the DB change
        dir_to_delete: Path | None = None

        if archive.file_path and archive.file_path.strip():
            file_path = settings.base_dir / archive.file_path
            if file_path.exists():
                archive_dir = file_path.parent

                # Safety check 1: archive_dir must be inside archive_dir
                try:
                    archive_dir.resolve().relative_to(settings.archive_dir.resolve())
                except ValueError:
                    logger.error(
                        f"SECURITY: Refusing to delete archive {archive_id} - "
                        f"path {archive_dir} is outside archive directory {settings.archive_dir}"
                    )
                    await self.db.delete(archive)
                    await self.db.commit()
                    return True

                # Safety check 2: archive_dir must be at least 1 level deep inside archive_dir
                try:
                    relative_path = archive_dir.resolve().relative_to(settings.archive_dir.resolve())
                    if len(relative_path.parts) < 1:
                        logger.error(
                            f"SECURITY: Refusing to delete archive {archive_id} - "
                            f"path {archive_dir} is not deep enough inside archive directory"
                        )
                        await self.db.delete(archive)
                        await self.db.commit()
                        return True
                except ValueError:
                    pass  # Already handled above

                dir_to_delete = archive_dir
        else:
            logger.error(
                f"SECURITY: Refusing to delete files for archive {archive_id} - "
                f"file_path is empty or invalid: '{archive.file_path}'"
            )

        # Check if other archives share the same file (deduplication)
        shared = False
        if archive.file_path:
            shared_result = await self.db.execute(
                select(func.count(PrintArchive.id)).where(
                    PrintArchive.file_path == archive.file_path,
                    PrintArchive.id != archive_id,
                )
            )
            shared = (shared_result.scalar() or 0) > 0

        # Delete database record FIRST - if the commit fails (e.g. database locked
        # during concurrent bulk deletes), the files stay on disk and nothing is lost.
        await self.db.delete(archive)
        await self.db.commit()

        # Only delete files AFTER the DB commit succeeds and no other archives reference them
        if dir_to_delete and not shared:
            shutil.rmtree(dir_to_delete, ignore_errors=True)

        return True

    async def attach_timelapse(
        self,
        archive_id: int,
        timelapse_data: bytes,
        filename: str = "timelapse.mp4",
    ) -> bool:
        """Attach a timelapse video to an archive.

        Non-MP4 videos (e.g. AVI from P1S) are saved as-is and a background
        task converts them to MP4 for browser compatibility.
        """
        import asyncio

        archive = await self.get_archive(archive_id)
        if not archive:
            return False

        # Get archive directory
        file_path = settings.base_dir / archive.file_path
        archive_dir = file_path.parent

        # Save timelapse - use thread pool to avoid blocking event loop
        # (timelapse files can be 100MB+, sync write blocks for seconds)
        timelapse_file = archive_dir / filename
        await asyncio.to_thread(timelapse_file.write_bytes, timelapse_data)

        # Update archive record
        archive.timelapse_path = str(timelapse_file.relative_to(settings.base_dir))
        await self.db.commit()

        # For non-MP4 videos (e.g. AVI from P1S), kick off background conversion
        if not filename.lower().endswith(".mp4"):
            asyncio.create_task(
                _convert_timelapse_to_mp4(archive_id, timelapse_file),
                name=f"timelapse-convert-{archive_id}",
            )

        return True


async def _convert_timelapse_to_mp4(archive_id: int, source_path: Path) -> None:
    """Background task: convert non-MP4 timelapse (e.g. AVI from P1S) to MP4.

    Runs with low CPU priority (-threads 1, nice) so it doesn't starve
    other processes on resource-constrained devices like Raspberry Pi.
    """
    import asyncio

    from backend.app.core.database import async_session
    from backend.app.services.camera import get_ffmpeg_path

    logger = logging.getLogger(__name__)

    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        logger.info(
            "FFmpeg not available, skipping timelapse conversion for archive %s (file saved as %s)",
            archive_id,
            source_path.suffix,
        )
        return

    mp4_path = source_path.with_suffix(".mp4")

    try:
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(source_path),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-threads",
            "1",
            "-movflags",
            "+faststart",
            str(mp4_path),
        ]

        # Try with nice for lower CPU priority (standard on Linux/macOS)
        try:
            process = await asyncio.create_subprocess_exec(
                "nice",
                "-n",
                "19",
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            # nice not available (e.g. Windows), run without
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        _, stderr = await process.communicate()

        if process.returncode != 0:
            logger.warning(
                "Timelapse conversion failed for archive %s: %s",
                archive_id,
                stderr.decode()[-500:],
            )
            if mp4_path.exists():
                mp4_path.unlink()
            return

        # Update DB path to the new MP4 file
        async with async_session() as db:
            from backend.app.models.archive import PrintArchive

            result = await db.execute(select(PrintArchive).where(PrintArchive.id == archive_id))
            archive = result.scalar_one_or_none()
            if archive:
                archive.timelapse_path = str(mp4_path.relative_to(settings.base_dir))
                await db.commit()

        # Remove original non-MP4 file
        if source_path.exists():
            source_path.unlink()

        logger.info(
            "Converted timelapse to MP4 for archive %s (%s → %s)",
            archive_id,
            source_path.name,
            mp4_path.name,
        )

    except Exception as e:
        logger.warning("Timelapse conversion error for archive %s: %s", archive_id, e)
        if mp4_path.exists():
            mp4_path.unlink()
