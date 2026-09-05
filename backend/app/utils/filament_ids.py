"""Utility functions for converting between filament_id and setting_id formats, and shared filament constants.

Bambu printers use two ID formats for filament presets:
  - **filament_id** (aka tray_info_idx): e.g. "GFL05", "GFG02", "GFA00"
    Reported by printer firmware (RFID tags, AMS status).
  - **setting_id**: e.g. "GFSL05", "GFSG02", "GFSA00"
    Used by BambuStudio / Bambu Cloud API to resolve presets.

The only difference for official Bambu filaments is an "S" inserted after "GF".
User presets (starting with "P") use the same ID in both contexts.

⚠️ "GFS" is NOT a reliable marker of a setting_id. The support-material
families are spelled with it too — ``GFS00`` Support W, ``GFS01`` Support G,
``GFS04`` PVA, ``GFS99`` Generic PVA, ``GFSNL02`` SUNLU PLA Matte — those are
filament_ids the printer reports as ``tray_info_idx``, and their presets are
``GFSS00`` / ``GFSSNL02``. Deciding by prefix turned ``GFS00`` into ``GF00``,
an id that exists nowhere, and a support spool's edits were refused with
``unknown filament family`` (2026-09-04). Decide by SHAPE instead: a family
id is ``GF`` + one letter + two digits or ``GF`` + three letters + two digits
(every family in ``data/filament_catalog/bambu.json`` has one of the two,
no setting_id does), and the S comes off only when a family shape is left.
"""

import re

# Every Bambu family id in the catalog has one of these two shapes; a
# setting_id (family with an S after "GF") never does.
_FAMILY_SHAPE = re.compile(r"^GF[A-Z](?:[A-Z]{2})?\d{2}$")


def is_family_id_shape(value: str) -> bool:
    """True when ``value`` is spelled like a Bambu family id (``GFA00``,
    ``GFS00``, ``GFSNL02``) — the shape test the two converters share."""
    return bool(_FAMILY_SHAPE.match(value or ""))


def filament_id_to_setting_id(filament_id: str) -> str:
    """Convert filament_id → setting_id (e.g. "GFL05" → "GFSL05", "GFS00" → "GFSS00").

    - Already a setting_id (not a family shape) → returned unchanged.
    - User presets ("P…") → returned unchanged.
    - Empty / unknown → returned unchanged.
    """
    if not filament_id:
        return filament_id

    # User presets start with "P" - leave unchanged
    if filament_id.startswith("P"):
        return filament_id

    # Official Bambu families: GFx## -> GFSx## — only a FAMILY gets the S;
    # anything else spelled "GF…" is already a setting_id.
    if is_family_id_shape(filament_id):
        return f"GFS{filament_id[2:]}"

    return filament_id


def setting_id_to_filament_id(setting_id: str) -> str:
    """Convert setting_id → filament_id (e.g. "GFSL05" → "GFL05", "GFSS00" → "GFS00").

    - Already a filament_id (family shape, ``GFS00`` included) → returned unchanged.
    - User presets ("P…") → returned unchanged.
    - Empty / unknown → returned unchanged.
    """
    if not setting_id:
        return setting_id

    # User presets start with "P" - leave unchanged
    if setting_id.startswith("P"):
        return setting_id

    # A family is a family — the S in "GFS00" belongs to Support W itself.
    if is_family_id_shape(setting_id):
        return setting_id

    # Setting_id format: GFSx## -> GFx##  (remove the "S") — but only when
    # what is left is a family; "GFS00" minus its S would be "GF00", nothing.
    if setting_id.startswith("GFS"):
        stripped = f"GF{setting_id[3:]}"
        if is_family_id_shape(stripped):
            return stripped

    return setting_id


def normalize_slicer_filament(slicer_filament: str | None) -> tuple[str, str]:
    """Normalize a slicer_filament value into (tray_info_idx, setting_id).

    The slicer_filament field on a spool can be stored in either format:
      - filament_id: "GFL05"  (from RFID tag scan)
      - setting_id:  "GFSL05" or "GFSL05_07"  (from cloud preset picker)

    Returns (tray_info_idx, setting_id) with version suffixes stripped.
    """
    raw = slicer_filament or ""
    if not raw:
        return ("", "")

    # Strip version suffix (e.g. "GFSL05_07" → "GFSL05")
    base = raw.split("_")[0] if "_" in raw else raw

    tray_info_idx = setting_id_to_filament_id(base)
    sid = filament_id_to_setting_id(base)

    return (tray_info_idx, sid)
