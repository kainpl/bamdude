"""Pure 3MF parsing shared by archive and library worker processes."""

from __future__ import annotations

import json
import logging
import re
import zipfile
from pathlib import Path

from defusedxml import ElementTree as ET

from backend.app.utils.threemf_tools import extract_nozzle_mapping_from_3mf

logger = logging.getLogger("backend.app.services.archive")

# How much of a plate's G-code to scan. The header block ends in the first
# kilobyte; the CONFIG_BLOCK after it is alphabetical and carries layer_height
# 14-25 KB in (Bambu Studio and OrcaSlicer alike), so 4 KB could only ever see
# the header (upstream 7e77bf58). The read decompresses on demand.
_GCODE_SCAN_BYTES = 64 * 1024

# Anchored to the line start so a key merely ENDING in layer_height
# (independent_support_layer_height) cannot answer.
_GCODE_LAYER_HEIGHT_RE = re.compile(r"^;\s*layer_height\s*=\s*([\d.]+)\s*$", re.IGNORECASE | re.MULTILINE)


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

                    # Same discovery the Skip Objects list uses, so the count on a
                    # library card and the list in the dialog can never disagree.
                    # Reading slice_info here directly is what made both report one
                    # object for a plate holding five.
                    plate_idx = 1
                    for meta_el in plate.findall("metadata"):
                        if meta_el.get("key") == "index":
                            try:
                                plate_idx = int(meta_el.get("value", "1"))
                            except ValueError:
                                pass
                            break

                    printable_objects = discover_plate_objects(zf, plate_idx)
                    if printable_objects:
                        self.metadata["printable_objects"] = printable_objects

                # Get filament info from filaments ACTUALLY USED in the print
                # slice_info has <filament id="1" type="PLA" color="#FFFFFF" used_g="100" />
                # Only include filaments where used_g > 0
                # #1785: scope per-slot filament to the printed plate — the headline
                # grams/time above already come from the matched <plate>; the per-slot
                # breakdown (and the archive card's per-slot list) must match, or a
                # multi-plate archive's notification shows other plates' filament rows.
                # Falls back to document-wide when no plate matched.
                filaments = plate.findall("filament") if plate is not None else root.findall(".//filament")
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
                    nozzle_mapping = extract_nozzle_mapping_from_3mf(zf, self.metadata.get("_plate_index")) or {}
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
                                    # Routing must not turn a small positive
                                    # consumption into an unused channel.
                                    "used_g_raw": used_g,
                                    "type": f.get("type", ""),
                                    "color": f.get("color", ""),
                                    # The slicer's spool identity for this slot
                                    # ("GFA00" generic PLA, "GFA01" PLA Matte,
                                    # "P4d64437" a custom preset, "" third-party).
                                    # Bambu reports every PLA variant as tray_type
                                    # "PLA", so this is the only field that tells
                                    # Basic from Matte from Silk (#2650).
                                    "tray_info_idx": f.get("tray_info_idx", ""),
                                }
                            )
                            if int(slot_id) in nozzle_mapping:
                                filament_slots[-1]["nozzle_id"] = nozzle_mapping[int(slot_id)]
                    if filament_slots:
                        self.metadata["filament_slots"] = filament_slots
        except Exception:
            pass  # Skip unparseable slice_info metadata

    def _first_used_filament_index(self) -> int:
        """0-based index of the first filament the printed plate consumes; 0 when unknown."""
        for slot in self.metadata.get("filament_slots") or []:
            slot_id = slot.get("slot_id")
            if isinstance(slot_id, int) and slot_id >= 1:
                return slot_id - 1
        return 0

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
                header = f.read(_GCODE_SCAN_BYTES).decode("utf-8", errors="ignore")

            layers = read_total_layers(zf, gcode_path)
            if layers is not None:
                self.metadata["total_layers"] = layers

            # The plate's G-code is what the printer executes; project_settings
            # records the PROJECT, and a multi-plate or re-sliced export can leave
            # it describing another plate or an earlier process. Where both are
            # present, the G-code decides. A source 3MF has no G-code and keeps
            # the project value.
            match = _GCODE_LAYER_HEIGHT_RE.search(header)
            if match:
                try:
                    self.metadata["layer_height"] = float(match.group(1))
                except ValueError:
                    pass  # malformed: keep what project_settings gave

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
        """Extract filament info from project settings — includes support
        materials so a PLA-model / PVA-support project shows both on the
        archive card badge (#1881).

        Earlier code filtered by ``filament_is_support``; that hid PVA
        (and any other soluble/breakaway support material) from the card
        even when the user had explicitly configured it, and made source
        3MFs look single-material until the print completed. slice_info
        (parsed separately) is still preferred when present — it lists
        only filaments the print actually consumes, this fallback only
        runs on unsliced source 3MFs.
        """
        try:
            filament_types = data.get("filament_type", [])
            filament_colors = data.get("filament_colour", [])

            if not filament_types:
                return

            unique_types: list[str] = []
            for ftype in filament_types:
                if ftype and ftype not in unique_types:
                    unique_types.append(ftype)

            unique_colors: list[str] = []
            for color in filament_colors:
                if color and color not in unique_colors:
                    unique_colors.append(color)

            if unique_types:
                self.metadata["filament_type"] = ", ".join(unique_types)
            if unique_colors:
                self.metadata["filament_color"] = ",".join(unique_colors)

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

            # Bed temperature — the plate's key, not a generic one.
            #
            # ``bed_temperature`` exists in BambuStudio's config *definitions*
            # but is never written into an exported 3MF: the bed temperature is
            # stored per plate type, and which one applies is decided by
            # ``curr_bed_type``. Checked against real archived 3MFs — they carry
            # cool_plate_temp / eng_plate_temp / hot_plate_temp /
            # textured_plate_temp / supertack_plate_temp and no
            # ``bed_temperature`` at all, which is why this field was NULL on
            # every archive ever recorded while nozzle temperature (a key that
            # does exist) filled in fine.
            bed_temp = self._bed_temperature_from(data)
            if bed_temp is not None:
                self.metadata["bed_temperature"] = bed_temp

            # Nozzle temperature: one value per filament, so the printed plate's
            # first USED filament's — not filament 1's. A plate that prints only
            # filament 3 (PETG) showed filament 1's PLA temperature (upstream
            # 879acdbf1). ``filament_slots`` is what slice_info says this plate
            # consumes (parsed before the project settings); without it, the
            # first value as before.
            for key in ["nozzle_temperature_initial_layer", "nozzle_temperature"]:
                if key in data:
                    val = data[key]
                    if isinstance(val, list) and val:
                        index = self._first_used_filament_index()
                        self.metadata["nozzle_temperature"] = int(float(val[index if index < len(val) else 0]))
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

    # Bed type → the config key holding that plate's temperature. Mirrors
    # BambuStudio's ``get_bed_temp_key`` (``PrintConfig.hpp``) and the
    # ``curr_bed_type`` enum values (``PrintConfig.cpp``) one for one — copied
    # from the source rather than inferred from the names, because the label
    # and the enum value differ ("Smooth PEI Plate / High Temp Plate" is shown
    # for the value "High Temp Plate").
    _BED_TEMP_KEY_BY_TYPE = {
        "cool plate": "cool_plate_temp",
        "engineering plate": "eng_plate_temp",
        "high temp plate": "hot_plate_temp",
        "textured pei plate": "textured_plate_temp",
        "supertack plate": "supertack_plate_temp",
        # Present in exported files but not in our BambuStudio snapshot's enum —
        # newer plate, same shape. Mapped so a file that uses it is not silently
        # left without a temperature.
        "textured cool plate": "textured_cool_plate_temp",
    }

    @staticmethod
    def _as_int(val) -> int | None:
        """First element of a slicer value, as an int. Bambu writes these as
        one-element string arrays (``['75']``), one per extruder."""
        if isinstance(val, list):
            val = val[0] if val else None
        if val is None or isinstance(val, bool):
            return None
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return None

    def _bed_temperature_from(self, data: dict) -> int | None:
        """The bed temperature for the plate this file was sliced for.

        Prefers the first-layer value: it is what the operator sees the printer
        do, it is the higher of the two on every stock profile, and it is what
        the archive comparison is useful for.

        Falls back to the highest plate temperature present when the bed type is
        missing or unknown. That is deliberately a guess and only reached when
        the alternative is NULL — a number from the wrong plate is still in the
        right ballpark, whereas nothing at all is what this whole change exists
        to fix. An unknown *and* empty file still yields None.
        """
        bed_type = (data.get("curr_bed_type") or "").strip().lower()
        key = self._BED_TEMP_KEY_BY_TYPE.get(bed_type)
        # The printed plate's filaments (1-based slots, from slice_info, parsed
        # first). Called on the class by some tests, hence the getattr.
        used = {
            int(slot["slot_id"])
            for slot in (getattr(self, "metadata", None) or {}).get("filament_slots") or []
            if isinstance(slot, dict) and slot.get("slot_id")
        }

        if key:
            for candidate in (f"{key}_initial_layer", key):
                value = ThreeMFParser._plate_temperature(data.get(candidate), used)
                if value:  # 0 means "this plate is not heated" — keep looking
                    return value

        # Unknown or missing bed type: take the warmest plate the file defines.
        temps = [
            t
            for k in self._BED_TEMP_KEY_BY_TYPE.values()
            for t in (
                ThreeMFParser._plate_temperature(data.get(f"{k}_initial_layer"), used),
                ThreeMFParser._plate_temperature(data.get(k), used),
            )
            if t
        ]
        return max(temps) if temps else None

    @staticmethod
    def _plate_temperature(val, used: set[int]) -> int | None:
        """The bed temperature one plate key asks for (upstream c001f596).

        The key holds one entry per filament, and 0 means that filament cannot
        print on this plate. The bed has one temperature, so the print runs at
        the highest its filaments ask for — Bambu Studio's
        ``get_highest_bed_temperature`` takes the max over the filaments the
        slice uses. Entry 0 alone stored another filament's value, or a 0.
        Without the used filaments (no slice_info), every entry counts.
        """
        if not isinstance(val, list):
            return ThreeMFParser._as_int(val)
        entries = [val[i - 1] for i in sorted(used) if 0 < i <= len(val)] if used else val
        # An array shorter than the slot numbers (one entry per extruder on some
        # exports) cannot be indexed by filament: read all of it rather than none.
        entries = entries or val
        temps = [t for t in (ThreeMFParser._as_int(entry) for entry in entries) if t is not None]
        return max(temps) if temps else None

    def _parse_3dmodel(self, zf: zipfile.ZipFile):
        """Parse 3D/3dmodel.model for MakerWorld metadata."""
        try:
            import html

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
                # 3MF metadata values are XML-encoded — `&` becomes `&amp;`, etc.
                # BambuStudio sometimes writes triple-encoded payloads
                # (`&amp;amp;amp;`), so unescape in a loop until stable (the same
                # trick `ThreeMFCardParser` uses). Without this a Title like
                # "Foo & Bar" lands in the DB as raw "Foo &amp; Bar" and React
                # double-escapes it on render to "Foo &amp;amp; Bar" (#1658).
                decoded = value.strip()
                prev = None
                while prev != decoded:
                    prev = decoded
                    decoded = html.unescape(decoded)
                makerworld_fields[name] = decoded

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


def extract_skip_support_from_3mf(data: bytes) -> bool:
    """Whether the 3MF supports per-object skipping.

    Requires ``gcode_label_objects`` AND ``exclude_object`` both true in
    ``Metadata/project_settings.config`` — mirrors
    ``ThreeMFParser._extract_print_settings`` (``gcode_label_objects`` defaults
    True for Bambu Studio, which omits it; ``exclude_object`` has no fallback).
    Read straight from the 3MF so the Skip-Objects UI gate doesn't depend on
    ``archive.extra_data`` being populated — the slicer-start load path doesn't
    set it, which left the button disabled even with objects loaded.
    """
    from io import BytesIO

    try:
        with zipfile.ZipFile(BytesIO(data), "r") as zf:
            if "Metadata/project_settings.config" not in zf.namelist():
                return False
            cfg = json.loads(zf.read("Metadata/project_settings.config").decode())
    except Exception:
        return False
    glo = _coerce_bool(cfg.get("gcode_label_objects"))
    glo = True if glo is None else glo
    eo = _coerce_bool(cfg.get("exclude_object")) if "exclude_object" in cfg else None
    return bool(glo) and bool(eo)


def _pick_centroids_from_3mf(zf: zipfile.ZipFile, plate_idx: int) -> dict[int, tuple[float, float]]:
    """Decode ``Metadata/pick_{plate}.png`` → ``{identify_id: (x_norm, y_norm)}``.

    Each printed instance is painted in a unique colour that encodes its
    ``identify_id`` as ``id = r | g<<8 | b<<16`` — the exact mapping the printer
    screen uses. The colour region's centroid (normalized to 0..1 in image space,
    y top-down — same orientation as the top-down cover render) is the object's
    on-plate position. This is the ONLY reliable source of per-instance positions
    when the slicer's "instances" copy feature is used (``plate_N.json`` then
    carries a single merged bbox for all copies). Returns ``{}`` on any problem.
    """
    pick_path = f"Metadata/pick_{plate_idx}.png"
    if pick_path not in zf.namelist():
        return {}
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(zf.read(pick_path))).convert("RGBA")
        w, h = img.size
        if w == 0 or h == 0:
            return {}
        raw = img.tobytes()  # flat RGBA, row-major
        acc: dict[int, list[float]] = {}  # id -> [sum_x, sum_y, count]
        for i in range(0, len(raw), 4):
            r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
            if a >= 16 and not (r < 16 and g < 16 and b < 16):
                oid = r | (g << 8) | (b << 16)
                p = i >> 2  # pixel index
                xx = p % w
                yy = p // w
                e = acc.get(oid)
                if e is None:
                    acc[oid] = [float(xx), float(yy), 1.0]
                else:
                    e[0] += xx
                    e[1] += yy
                    e[2] += 1.0
        min_count = max(50, int(w * h * 0.0005))
        out: dict[int, tuple[float, float]] = {}
        for oid, (sx, sy, c) in acc.items():
            if c >= min_count:
                out[oid] = (sx / c / w, sy / c / h)
        return out
    except Exception:
        return {}


def _objects_from_slice_info(zf: zipfile.ZipFile, plate_idx: int) -> dict[int, str]:
    """Tier 3: the plate's ``<object>`` entries. Today's only source.

    Kept last because it is wrong in both directions on real files: OrcaSlicer
    2.4+ lists one entry for N instances, and one archived file listed two ids
    that appear in neither the gcode nor the pick PNG.
    """
    if "Metadata/slice_info.config" not in zf.namelist():
        return {}
    try:
        root = ET.fromstring(zf.read("Metadata/slice_info.config").decode())
    except Exception:
        return {}

    plate = None
    for candidate in root.findall(".//plate"):
        for meta in candidate.findall("metadata"):
            if meta.get("key") == "index":
                try:
                    if int(meta.get("value", "")) == plate_idx:
                        plate = candidate
                        break
                except ValueError:
                    continue
        if plate is not None:
            break
    if plate is None:
        plate = root.find(".//plate")
    if plate is None:
        return {}

    out: dict[int, str] = {}
    for obj in plate.findall("object"):
        identify_id = obj.get("identify_id")
        name = obj.get("name")
        if identify_id and name and obj.get("skipped", "false").lower() != "true":
            try:
                out[int(identify_id)] = name
            except ValueError:
                continue  # non-numeric identify_id is not addressable by the printer
    return out


_MODEL_LABEL_RE = re.compile(rb"; model label id: *([\d,]+)")

# The header sits in the first few hundred bytes; one read covers it with room
# to spare, and never decompresses the rest of a multi-megabyte entry.
_GCODE_HEAD_BYTES = 65536


def _objects_from_gcode_header(zf: zipfile.ZipFile, plate_idx: int, slice_names: dict[int, str]) -> dict[int, str]:
    """Tier 1: the ``; model label id: a,b,c`` line at the top of the plate gcode.

    This is the list the firmware itself works from, which is why a printer
    happily skips instances that slice_info never mentions. Measured at byte
    offset 234 of a 34 MB gcode, present in 133 of 178 archived sliced files,
    and correct in all three archives where the other two sources disagreed.

    Absent for single-object Bambu Studio files by design — its guard requires
    ``num_object_instances() > 1`` (BambuStudio GCode.cpp:2344). OrcaSlicer has
    no such condition and enumerates per *instance*
    (OrcaSlicer v2.4.2 GCode.cpp:2588), which is precisely why its header lists
    five ids where its own slice_info.config lists one.

    Neither slicer gates this on ``gcode_label_objects`` or ``exclude_object``,
    so the presence of this header says nothing about whether skipping is
    allowed — that stays the job of ``extract_skip_support_from_3mf``.
    """
    names = zf.namelist()
    candidate = f"Metadata/plate_{plate_idx}.gcode"
    if candidate not in names:
        gcodes = [n for n in names if n.endswith(".gcode")]
        if len(gcodes) != 1:
            return {}
        candidate = gcodes[0]

    try:
        with zf.open(candidate) as fh:
            head = fh.read(_GCODE_HEAD_BYTES)
    except Exception:
        return {}

    match = _MODEL_LABEL_RE.search(head)
    if not match:
        return {}

    out: dict[int, str] = {}
    for token in match.group(1).split(b","):
        try:
            oid = int(token)
        except ValueError:
            continue
        out[oid] = _name_for(oid, slice_names)
    return out


def _name_for(oid: int, slice_names: dict[int, str]) -> str:
    """Name for an id that slice_info never listed.

    Instances of one model are consecutive ids, so the nearest smaller known id
    is the model they were copied from. Duplicate names across instances are
    correct — the slicer shows them the same way, and the id is what every skip
    command actually addresses.
    """
    if oid in slice_names:
        return slice_names[oid]
    smaller = [k for k in slice_names if k <= oid]
    if smaller:
        return slice_names[max(smaller)]
    if slice_names:
        return slice_names[min(slice_names)]
    return f"Object_{oid}"


def _objects_from_pick_png(zf: zipfile.ZipFile, plate_idx: int, slice_names: dict[int, str]) -> dict[int, str]:
    """Tier 2: ids painted into ``Metadata/pick_{plate}.png``.

    Each printed instance is filled with a colour encoding its identify_id as
    ``id = r | g<<8 | b<<16`` — the same mapping the printer screen uses.

    Rejects ``r == g == b``. Five archived files carry an ordinary greyscale
    render under this name; their greys decode to large plausible-looking ids
    (3881787 = RGB(59,59,59) and similar) which no pixel-count threshold filters
    out, because the grey areas are enormous. A real identify_id is small enough
    that b is 0, so a perfect grey is never one.
    """
    pick_path = f"Metadata/pick_{plate_idx}.png"
    if pick_path not in zf.namelist():
        return {}
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(zf.read(pick_path))).convert("RGBA")
        w, h = img.size
        if w == 0 or h == 0:
            return {}
        raw = img.tobytes()
        counts: dict[int, int] = {}
        for i in range(0, len(raw), 4):
            r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
            if a < 16 or (r < 16 and g < 16 and b < 16) or (r == g == b):
                continue
            oid = r | (g << 8) | (b << 16)
            counts[oid] = counts.get(oid, 0) + 1
        min_count = max(50, int(w * h * 0.0005))
        return {oid: _name_for(oid, slice_names) for oid, c in counts.items() if c >= min_count}
    except Exception:
        return {}


def discover_plate_objects(zf: zipfile.ZipFile, plate_idx: int) -> dict[int, str]:
    """Every printable object on ``plate_idx``, as ``{identify_id: name}``.

    Three sources, tried in descending order of trustworthiness; the FIRST one
    that yields anything wins. Not a union — unioning would resurrect ids that
    exist only in slice_info and nowhere else (measured on a real archive).

    Discovery approach adapted from @latsss' bambu-cli 3mf-parser, with thanks.
    """
    slice_names = _objects_from_slice_info(zf, plate_idx)

    from_gcode = _objects_from_gcode_header(zf, plate_idx, slice_names)
    if from_gcode:
        return from_gcode

    from_pick = _objects_from_pick_png(zf, plate_idx, slice_names)
    if from_pick:
        return from_pick

    return slice_names


def extract_printable_objects_from_3mf(
    data: bytes,
    plate_number: int | None = None,
    include_positions: bool = False,
    with_confidence: bool = False,
) -> dict[int, str] | dict[int, dict] | tuple[dict[int, dict], list | None] | tuple[dict[int, dict], list | None, bool]:
    """Extract printable objects from 3MF file bytes.

    This is a lightweight function used during print start to get the list
    of objects that can be skipped.

    Args:
        data: Raw bytes of the 3MF file
        plate_number: Which plate was printed (1-based), or None for first plate
        include_positions: If True, return tuple of (objects dict, bbox_all)
        with_confidence: Adds a third element saying the positions are guesses.
            Opt-in so the two existing return shapes stay exactly as they were.

    Returns:
        If include_positions=False: Dictionary mapping identify_id (int) to object name (str)
        If include_positions=True: Tuple of (dict mapping identify_id to {name, x, y}, bbox_all list or None)
        If with_confidence=True as well: that tuple plus ``positions_approximate``
        — True when NOT ONE object could be located in the pick PNG, so every
        marker falls to the frontend's grid fallback and the plate it draws is
        fictional. One real position is enough to anchor the rest, so the flag
        is about a total absence, not partial coverage.
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

            # Find the correct plate.
            #
            # Bambu identifies a plate with a CHILD ``<metadata key="index">``,
            # not a ``plate_idx`` attribute — which is what this selector used to
            # look for. That attribute does not exist in a real
            # ``slice_info.config``, so the lookup always missed and fell through
            # to the first plate: ``plate_number`` was silently inert, and a
            # multi-plate job served plate 1's ``identify_id``s no matter which
            # plate was actually running. Skipping by one of those ids cancels
            # whatever object happens to carry it on the printing plate.
            #
            # This is the same walk ``ThreeMFParser`` already uses for filament
            # and time scoping a few hundred lines up; the two now agree.
            plate = None
            if plate_number:
                for candidate in root.findall(".//plate"):
                    for meta in candidate.findall("metadata"):
                        if meta.get("key") == "index":
                            try:
                                if int(meta.get("value", "")) == plate_number:
                                    plate = candidate
                                    break
                            except ValueError:
                                continue
                    if plate is not None:
                        break
            if plate is None:
                # Unknown/absent plate number → first plate, as before. A single-
                # plate sliced file has exactly one, so this is the normal path.
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

            # Load position data when positions are requested. Primary source is
            # the pick PNG (per-instance centroids, correct even for "instances"
            # copies). Secondary: plate_N.json bbox_objects matched by id, then by
            # name (legacy, last resort — wrong for duplicate-name copies).
            bbox_by_name: dict[str, list[list]] = {}
            bbox_by_id: dict[int, list] = {}
            pick_centroids: dict[int, tuple[float, float]] = {}
            if include_positions:
                pick_centroids = _pick_centroids_from_3mf(zf, plate_idx)
                plate_json_path = f"Metadata/plate_{plate_idx}.json"
                if plate_json_path in zf.namelist():
                    try:
                        plate_json = json.loads(zf.read(plate_json_path).decode())
                        # Get bbox_all - the bounding box of all objects (used for image bounds)
                        bbox_all = plate_json.get("bbox_all")
                        for bbox_obj in plate_json.get("bbox_objects", []):
                            obj_name = bbox_obj.get("name")
                            bbox = bbox_obj.get("bbox", [])
                            if len(bbox) >= 4:
                                try:
                                    bbox_by_id[int(bbox_obj.get("id"))] = bbox
                                except (TypeError, ValueError):
                                    pass
                                if obj_name:
                                    bbox_by_name.setdefault(obj_name, []).append(bbox)
                    except (json.JSONDecodeError, KeyError):
                        pass  # Position data is optional; objects will lack x/y coordinates

            # Object list comes from discover_plate_objects, which prefers the
            # gcode header over slice_info — this loop used to read slice_info
            # directly and so reported one object for a plate holding five.
            for obj_id, name in discover_plate_objects(zf, plate_idx).items():
                if include_positions:
                    x, y, norm = None, None, False
                    if obj_id in pick_centroids:
                        # Normalized image-space centroid from the pick PNG —
                        # matches the printer screen. Frontend places directly.
                        x, y = pick_centroids[obj_id]
                        norm = True
                    else:
                        # Fallback: bbox center in mm (frontend maps via bbox_all).
                        # Prefer id-match; fall back to name-match (pop for dup names).
                        bbox = bbox_by_id.get(obj_id)
                        if bbox is None:
                            bboxes = bbox_by_name.get(name)
                            if bboxes:
                                bbox = bboxes.pop(0)
                        if bbox and len(bbox) >= 4:
                            x = (bbox[0] + bbox[2]) / 2
                            y = (bbox[1] + bbox[3]) / 2
                    printable_objects[obj_id] = {"name": name, "x": x, "y": y, "norm": norm}
                else:
                    printable_objects[obj_id] = name

    except Exception:
        pass  # Return empty dict if 3MF is corrupt or unreadable

    if include_positions:
        if with_confidence:
            # Approximate only when nothing at all could be placed. A single real
            # centroid means the pick PNG was readable and the rest simply are
            # not visible in it (occluded), which is not the same as a wholly
            # invented layout.
            approximate = not any(o.get("norm") for o in printable_objects.values())
            return printable_objects, bbox_all, approximate
        return printable_objects, bbox_all
    return printable_objects


def build_plate_objects_payload(data: bytes, plate_idx: int) -> dict:
    """Everything the read-only plate preview needs, from one set of 3MF reads.

    Lives here rather than in either route so ``/library/files/{id}/plate-objects``
    and ``/archives/{id}/plate-objects`` answer identically — they differ only in
    how they find the file and which plate they ask for.

    ``has_top_view`` is checked rather than assumed. Markers are placed in
    pick-PNG image space, which is top-down; over ``plate_N.png`` — a ¾ render —
    they would sit convincingly on the wrong parts. The frontend suppresses the
    image entirely when this is False, because no image beats a lying one.

    Never raises: a corrupt or non-ZIP file yields an empty preview. The caller
    has already established the row exists and the file is on disk, so a parse
    failure here is a broken archive, not a missing one, and a 500 would tell
    the operator less than an empty dialog does.
    """
    from io import BytesIO

    objects, bbox_all, approximate = extract_printable_objects_from_3mf(
        data, plate_idx, include_positions=True, with_confidence=True
    )

    has_top = False
    try:
        with zipfile.ZipFile(BytesIO(data), "r") as zf:
            has_top = f"Metadata/top_{plate_idx}.png" in zf.namelist()
    except Exception:
        has_top = False

    items: list[dict] = []
    for oid, value in objects.items():
        if isinstance(value, dict):
            items.append(
                {
                    "id": oid,
                    "name": value.get("name") or f"Object_{oid}",
                    "x": value.get("x"),
                    "y": value.get("y"),
                    "norm": bool(value.get("norm")),
                }
            )
        else:
            items.append({"id": oid, "name": value, "x": None, "y": None, "norm": False})
    # Sorted by id so the list order matches the marker numbers on the plate —
    # dict order here is discovery order, which differs per tier.
    items.sort(key=lambda o: o["id"])

    # ⚠️ Sorted BEFORE the markers are computed: the grid fallback places by
    # index, so laying out first and sorting second would scramble it.
    from backend.app.services.plate_markers import marker_position

    for index, item in enumerate(items):
        item["marker"] = marker_position(item, index, len(items), bbox_all)

    return {
        "plate_index": plate_idx,
        "objects": items,
        "bbox_all": bbox_all,
        "positions_approximate": approximate,
        "skip_objects_supported": extract_skip_support_from_3mf(data),
        "has_top_view": has_top,
    }


def read_total_layers(zf: zipfile.ZipFile, gcode_path: str) -> int | None:
    """Layer count from one plate's g-code header, or ``None``.

    BambuStudio writes it as ``; total layer number: N`` (``GCodeProcessor.cpp``),
    in the first few lines, so only the head of the entry is read — these files
    are tens of megabytes and the answer is in the first kilobyte.

    ⚠️ **This is per PLATE, not per file.** Plate 1 of a container can be 200
    layers and plate 5 eighty; a single number for the whole 3MF would be a
    guess dressed as a fact. Both callers pass the plate they mean.
    """
    try:
        with zf.open(gcode_path) as f:
            header = f.read(4096).decode("utf-8", errors="ignore")
    except Exception:
        return None
    match = re.search(r";\s*total\s+layer\s+number[:\s]+(\d+)", header, re.IGNORECASE)
    return int(match.group(1)) if match else None


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
        ``print_time_seconds``, ``filament_used_grams``, ``total_layers``,
        ``filaments`` (list of {slot_id, type, color, used_grams, used_meters}),
        ``bed_type`` (per-plate ``curr_bed_type``, or None),
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
                    "bed_type": None,
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
                    elif key == "curr_bed_type" and value:
                        # Per-plate build plate type so the picker can show the
                        # right plate alongside each option (#1281).
                        plate_info["bed_type"] = value.strip()
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
                # ⚠️ Read from this plate's own g-code, not shared across the
                # file: plates of one container routinely differ by hundreds of
                # layers. ``None`` for a source-only 3MF that was never sliced.
                "total_layers": read_total_layers(zf, f"Metadata/plate_{idx}.gcode"),
                "filaments": meta.get("filaments", []),
                "bed_type": meta.get("bed_type"),
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

    Not a single pass any more: ``discover_plate_objects`` re-reads
    slice_info.config and decodes ``pick_{idx}.png`` per plate, so a ten-plate
    file pays ten pick decodes rather than one. That cost lands on the
    ``/plates`` cache-MISS path and inside the m114 seed; a page render reads the
    cached ``file_metadata["plates"]`` / ``extra_data["plates"]`` and is
    unaffected. Worth it — the previous single pass produced wrong counts.

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

    # Per-plate ``printable_objects`` (id → name). The slice_info walk below
    # survives only to ENUMERATE the plate indices — the objects themselves come
    # from ``discover_plate_objects``, because slice_info undercounts every plate
    # that uses instances (OrcaSlicer records the source object once for N
    # copies) and overcounts on at least one archived file. This was the fourth
    # parse site and the last one still reading slice_info directly; the other
    # three moved in the cycle that introduced the cascade.
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
                out["plates"].setdefault(idx, {})["printable_objects"] = discover_plate_objects(zf, idx)
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
