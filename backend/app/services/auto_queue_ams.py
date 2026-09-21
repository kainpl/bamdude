"""What an auto-queue item's printer is holding, read off live status.

⚠️ **This module no longer decides anything.** It used to carry a port of
upstream Bambuddy's greedy AMS matcher, which nothing has called since complete
routing landed: ``services/filament_routing.resolve_filament_routing`` is the
one place a slot is bound to a feed, under the job's own
``RoutingPolicy``. Deleting the port removed a second, weaker answer to the same
question — never re-add one here.

What remains is the reader every caller still needs: one loaded-filament list
per printer, with an advertised-profile overlay seen through
(``ams_advertised_overlay``) so a masked slot reports the spool it REALLY holds.
"""

from __future__ import annotations

from backend.app.services import ams_advertised_overlay as overlay


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


def build_loaded_filaments(status, printer_id: int | None = None) -> list[dict]:
    """Build the loaded-filaments list from a printer status object.

    Each entry: ``{type, color, tray_info_idx, ams_id, tray_id, is_ht,
    is_external, global_tray_id, extruder_id, remain, tray_uuid, tag_uid}``.

    Mirrors upstream ``PrintScheduler._build_loaded_filaments``.

    ``printer_id``: when given, an AMS slot we advertised under a different
    profile (``ams_advertised_overlay``) reports the spool it REALLY holds —
    the same treatment ``PrintScheduler._build_loaded_filaments`` gets, and for
    the same reason: every reader that reasons about the SPOOL must see through
    the mask, or the mapping lands on the wrong tray and the low-filament
    announcement names a colour nobody loaded.
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
            tray_color = tray.get("tray_color", "")
            tray_info_idx = tray.get("tray_info_idx", "")
            entry = overlay.effective(printer_id, ams_id, tray_id, tray) if printer_id is not None else None
            if entry is not None:
                tray_type, tray_color, tray_info_idx = (
                    entry.actual_material,
                    entry.actual_color,
                    entry.actual_variant,
                )
            global_tray_id = ams_id if ams_id >= 128 else ams_id * 4 + tray_id
            filaments.append(
                {
                    "type": tray_type,
                    "color": _normalize_color(tray_color),
                    "tray_info_idx": tray_info_idx,
                    "ams_id": ams_id,
                    "tray_id": tray_id,
                    "is_ht": is_ht,
                    "is_external": False,
                    "global_tray_id": global_tray_id,
                    "extruder_id": ams_extruder_map.get(str(ams_id)),
                    "remain": tray.get("remain", -1),
                    # The spool's identity, for anything that must say "still the
                    # same spool" across syncs (the low-filament announcement).
                    "tray_uuid": tray.get("tray_uuid", ""),
                    "tag_uid": tray.get("tag_uid", ""),
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
                "tray_uuid": vt.get("tray_uuid", ""),
                "tag_uid": vt.get("tag_uid", ""),
            }
        )

    return filaments
