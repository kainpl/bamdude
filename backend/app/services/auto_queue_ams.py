"""AMS mapping computation for auto-queue items.

When the AutoQueueScheduler assigns an auto-queue item to an idle
printer, it needs to compute which AMS tray to use for each filament
slot in the 3MF. This module ports upstream Bambuddy's AMS-matching
logic (see ``temp/upstream-queue-deep-dive.md``) adapted to BamDude's
AutoQueueItem.

Matching priority (mirrors upstream):
    1. Unique ``tray_info_idx`` — slicer-stamped spool ID present on
       exactly one loaded tray → use it.
    2. Exact color match (type + RGB).
    3. Similar color match (RGB within threshold).
    4. Type-only fallback (any tray of the right canonical type).

Filament overrides (with optional ``force_color_match``) and the
``prefer_lowest_filament`` setting are honoured the same way as in
upstream.
"""

from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings as app_settings
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.services.print_scheduler import _canonical_filament_type
from backend.app.services.printer_manager import printer_manager
from backend.app.utils.threemf_tools import extract_nozzle_mapping_from_3mf

logger = logging.getLogger(__name__)


def _normalize_color(color: str | None) -> str:
    """Normalize color to ``#RRGGBB`` format."""
    if not color:
        return "#808080"
    hex_color = color.replace("#", "")[:6]
    return f"#{hex_color}"


def _normalize_color_for_compare(color: str | None) -> str:
    """Normalize color for comparison (lowercase, no hash, 6 chars max)."""
    if not color:
        return ""
    return color.replace("#", "").lower()[:6]


def _colors_are_similar(color1: str | None, color2: str | None, threshold: int = 40) -> bool:
    """True if two RGB colors are within ``threshold`` per channel."""
    hex1 = _normalize_color_for_compare(color1)
    hex2 = _normalize_color_for_compare(color2)
    if not hex1 or not hex2 or len(hex1) < 6 or len(hex2) < 6:
        return False
    try:
        r1, g1, b1 = int(hex1[0:2], 16), int(hex1[2:4], 16), int(hex1[4:6], 16)
        r2, g2, b2 = int(hex2[0:2], 16), int(hex2[2:4], 16), int(hex2[4:6], 16)
        return abs(r1 - r2) <= threshold and abs(g1 - g2) <= threshold and abs(b1 - b2) <= threshold
    except ValueError:
        return False


async def _resolve_source_path(db: AsyncSession, item: AutoQueueItem) -> Path | None:
    """Return the on-disk path of the 3MF for an auto-queue item."""
    if item.archive_id:
        result = await db.execute(select(PrintArchive).where(PrintArchive.id == item.archive_id))
        archive = result.scalar_one_or_none()
        if archive and archive.file_path:
            return app_settings.base_dir / archive.file_path
    elif item.library_file_id:
        result = await db.execute(select(LibraryFile).where(LibraryFile.id == item.library_file_id))
        lib = result.scalar_one_or_none()
        if lib and lib.file_path:
            p = Path(lib.file_path)
            return p if p.is_absolute() else app_settings.base_dir / lib.file_path
    return None


async def get_filament_requirements(db: AsyncSession, item: AutoQueueItem) -> list[dict] | None:
    """Extract per-slot filament requirements from the source 3MF.

    Each entry: ``{slot_id, type, color, tray_info_idx, used_grams,
    nozzle_id?}``. Filters by ``item.plate_id`` if set; otherwise returns
    all filaments with ``used_g > 0``. Returns ``None`` on missing /
    unparseable file.

    Mirrors upstream ``PrintScheduler._get_filament_requirements`` but
    targets AutoQueueItem.
    """
    file_path = await _resolve_source_path(db, item)
    if not file_path or not file_path.exists():
        return None

    filaments: list[dict] = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            if "Metadata/slice_info.config" not in zf.namelist():
                return None

            content = zf.read("Metadata/slice_info.config").decode()
            root = ET.fromstring(content)

            plate_id = item.plate_id

            def _collect(filament_elems):
                for fel in filament_elems:
                    fid = fel.get("id")
                    used_g_str = fel.get("used_g", "0")
                    try:
                        used_g = float(used_g_str)
                    except (ValueError, TypeError):
                        continue
                    if used_g > 0 and fid:
                        filaments.append(
                            {
                                "slot_id": int(fid),
                                "type": fel.get("type", ""),
                                "color": fel.get("color", ""),
                                "tray_info_idx": fel.get("tray_info_idx", ""),
                                "used_grams": round(used_g, 1),
                            }
                        )

            if plate_id:
                for plate_elem in root.findall("./plate"):
                    idx = None
                    for meta in plate_elem.findall("metadata"):
                        if meta.get("key") == "index":
                            try:
                                idx = int(meta.get("value", "0"))
                            except (ValueError, TypeError):
                                idx = None
                            break
                    if idx == plate_id:
                        _collect(plate_elem.findall("./filament"))
                        break
            else:
                _collect(root.findall("./filament"))

            filaments.sort(key=lambda x: x["slot_id"])

            # Dual-nozzle (H2D, H2D Pro) extruder mapping
            nozzle_mapping = extract_nozzle_mapping_from_3mf(zf)
            if nozzle_mapping:
                for f in filaments:
                    f["nozzle_id"] = nozzle_mapping.get(f["slot_id"])
    except Exception as e:
        logger.warning("Failed to parse filament requirements for auto item %s: %s", item.id, e)
        return None

    return filaments or None


def build_loaded_filaments(status) -> list[dict]:
    """Build the loaded-filaments list from a printer status object.

    Each entry: ``{type, color, tray_info_idx, ams_id, tray_id, is_ht,
    is_external, global_tray_id, extruder_id, remain}``.

    Mirrors upstream ``PrintScheduler._build_loaded_filaments``.
    """
    filaments: list[dict] = []
    raw = status.raw_data
    ams_extruder_map = raw.get("ams_extruder_map", {})

    for ams_unit in raw.get("ams", []) or []:
        ams_id = int(ams_unit.get("id", 0))
        trays = ams_unit.get("tray", [])
        is_ht = len(trays) == 1
        for tray in trays:
            tray_type = tray.get("tray_type")
            if not tray_type:
                continue
            tray_id = int(tray.get("id", 0))
            global_tray_id = ams_id if ams_id >= 128 else ams_id * 4 + tray_id
            filaments.append(
                {
                    "type": tray_type,
                    "color": _normalize_color(tray.get("tray_color", "")),
                    "tray_info_idx": tray.get("tray_info_idx", ""),
                    "ams_id": ams_id,
                    "tray_id": tray_id,
                    "is_ht": is_ht,
                    "is_external": False,
                    "global_tray_id": global_tray_id,
                    "extruder_id": ams_extruder_map.get(str(ams_id)),
                    "remain": tray.get("remain", -1),
                }
            )

    for idx, vt in enumerate(raw.get("vt_tray") or []):
        if not vt.get("tray_type"):
            continue
        tray_id = int(vt.get("id", 254))
        filaments.append(
            {
                "type": vt["tray_type"],
                "color": _normalize_color(vt.get("tray_color", "")),
                "tray_info_idx": vt.get("tray_info_idx", ""),
                "ams_id": -1,
                "tray_id": idx,
                "is_ht": False,
                "is_external": True,
                "global_tray_id": tray_id,
                "extruder_id": (255 - tray_id) if ams_extruder_map else None,
                "remain": vt.get("remain", -1),
            }
        )

    return filaments


def match_filaments_to_slots(
    required: list[dict],
    loaded: list[dict],
    prefer_lowest: bool = False,
) -> list[int] | None:
    """Match required filaments to loaded trays and build the AMS mapping.

    Priority: unique tray_info_idx > exact color > similar color > type-only.

    Returns: ``[global_tray_id_for_slot_1, ..., global_tray_id_for_slot_N]``
    where the index is ``slot_id - 1``. ``-1`` for slots with no match.
    Returns ``None`` if no required filaments.

    Direct port of upstream ``PrintScheduler._match_filaments_to_slots``.
    """
    if not required:
        return None

    used_tray_ids: set[int] = set()
    comparisons: list[dict] = []

    for req in required:
        req_type = (req.get("type") or "").upper()
        req_color = req.get("color", "")
        req_tray_info_idx = req.get("tray_info_idx", "")

        idx_match = None
        exact_match = None
        similar_match = None
        type_only_match = None

        available = [f for f in loaded if f["global_tray_id"] not in used_tray_ids]

        # Hard filter by nozzle (dual-nozzle cross-assignment causes failures)
        req_nozzle_id = req.get("nozzle_id")
        if req_nozzle_id is not None:
            available = [f for f in available if f.get("extruder_id") == req_nozzle_id]

        if prefer_lowest:
            available.sort(key=lambda f: f.get("remain", -1) if f.get("remain", -1) >= 0 else 101)

        # Pass 1: unique tray_info_idx
        if req_tray_info_idx:
            idx_matches = [f for f in available if f.get("tray_info_idx") == req_tray_info_idx]
            if len(idx_matches) == 1:
                idx_match = idx_matches[0]
            elif len(idx_matches) > 1:
                if prefer_lowest:
                    idx_matches.sort(key=lambda f: f.get("remain", -1) if f.get("remain", -1) >= 0 else 101)
                for f in idx_matches:
                    f_color = f.get("color", "")
                    if _normalize_color_for_compare(f_color) == _normalize_color_for_compare(req_color):
                        if not exact_match:
                            exact_match = f
                    elif _colors_are_similar(f_color, req_color):
                        if not similar_match:
                            similar_match = f
                    elif not type_only_match:
                        type_only_match = f

        # Pass 2: standard type/color matching when no idx match
        if not idx_match and not exact_match and not similar_match and not type_only_match:
            for f in available:
                f_type = (f.get("type") or "").upper()
                if _canonical_filament_type(f_type) != _canonical_filament_type(req_type):
                    continue
                f_color = f.get("color", "")
                if _normalize_color_for_compare(f_color) == _normalize_color_for_compare(req_color):
                    if not exact_match:
                        exact_match = f
                elif _colors_are_similar(f_color, req_color):
                    if not similar_match:
                        similar_match = f
                elif not type_only_match:
                    type_only_match = f

        match = idx_match or exact_match or similar_match or type_only_match
        if match:
            used_tray_ids.add(match["global_tray_id"])
            comparisons.append({"slot_id": req.get("slot_id", 0), "global_tray_id": match["global_tray_id"]})
        else:
            comparisons.append({"slot_id": req.get("slot_id", 0), "global_tray_id": -1})

    if not comparisons:
        return None
    max_slot_id = max(c["slot_id"] for c in comparisons)
    if max_slot_id <= 0:
        return None

    mapping = [-1] * max_slot_id
    for c in comparisons:
        slot_id = c["slot_id"]
        if slot_id and slot_id > 0:
            mapping[slot_id - 1] = c["global_tray_id"]
    return mapping


async def compute_ams_mapping_for_printer(
    db: AsyncSession,
    printer_id: int,
    item: AutoQueueItem,
    prefer_lowest: bool = False,
) -> list[int] | None:
    """End-to-end: read 3MF, apply overrides, build mapping for the printer.

    Returns the ``ams_mapping`` value to store on the dispatched
    print_queue item. ``None`` if no mapping is needed (no filament
    requirements) or possible (printer offline, file missing).
    """
    status = printer_manager.get_status(printer_id)
    if not status:
        logger.warning("AMS mapping: printer %s status unavailable", printer_id)
        return None

    requirements = await get_filament_requirements(db, item)
    if not requirements:
        return None

    if item.filament_overrides:
        try:
            overrides = json.loads(item.filament_overrides)
            override_map = {o["slot_id"]: o for o in overrides}
            for req in requirements:
                if req["slot_id"] in override_map:
                    o = override_map[req["slot_id"]]
                    req["type"] = o.get("type", req["type"])
                    req["color"] = o.get("color", req["color"])
                    # Clear tray_info_idx so matching falls to type+color
                    req["tray_info_idx"] = ""
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to apply filament_overrides for auto item %s: %s", item.id, e)

    loaded = build_loaded_filaments(status)
    if not loaded:
        return None

    return match_filaments_to_slots(requirements, loaded, prefer_lowest)
