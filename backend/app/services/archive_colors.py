"""Fill ``PrintArchive.filament_color`` from the loaded built-in-inventory spool
per slot, at print start.

At start the archive shows the operator-curated spool colour immediately; the
usage-tracking completion path later refines it (and is the *sole* source in
Spoolman mode). **Built-in inventory only** — Spoolman colours are applied
per-slot at completion by ``spoolman_tracking`` (resolving them needs a live
Spoolman fetch, kept out of the dispatch hot path). Per-slot fallback and colour
normalisation are shared with the completion path via
``usage_tracker._archive_colors_from_spools`` / ``_spool_color_to_hex``.

Spec: docs/superpowers/specs/2026-07-23-archive-filament-color-from-loaded-spool-design.md
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes.settings import get_setting
from backend.app.models.archive import PrintArchive
from backend.app.models.spool import Spool
from backend.app.services.usage_tracker import (
    _archive_colors_from_spools,
    _resolve_spool_id_for_tray,
    _spool_color_to_hex,
)

logger = logging.getLogger(__name__)


def _global_to_ams_tray(global_id: int) -> tuple[int, int]:
    """Invert a bamdude global tray id → ``(ams_id, tray_id)``.

    Mirrors the encoding in ``usage_tracker._decode_mqtt_mapping``: regular AMS
    is ``ams_id * 4 + tray``; AMS-HT (``>= 128``) and external (254/255) carry a
    single slot per unit, so the id *is* the ams_id with tray 0.
    """
    if global_id >= 128:
        return global_id, 0
    return global_id // 4, global_id % 4


def _plate_filaments(archive: PrintArchive) -> list[dict]:
    """3MF per-slot filaments for the printed plate as ``[{slot_id, used_g, color}]``.

    Reads the parsed per-plate cache in ``extra_data['plates']`` (see
    ``services/archive.py::parse_plates_from_3mf``), selecting the plate that
    actually ran (``archive.plate_index``) or the only plate. Empty for no-3MF
    fallback archives.
    """
    extra = archive.extra_data or {}
    plates = extra.get("plates") or []
    if not plates:
        return []
    plate = None
    if archive.plate_index is not None:
        plate = next((p for p in plates if p.get("index") == archive.plate_index), None)
    if plate is None:
        plate = plates[0]
    out: list[dict] = []
    for f in plate.get("filaments") or []:
        if f.get("slot_id") is not None:
            out.append({"slot_id": f["slot_id"], "used_g": f.get("used_grams", 0), "color": f.get("color")})
    return out


def _colors_no_3mf(results: list[dict]) -> list[str] | None:
    """Spool colours only (no 3MF fallback exists for no-3MF prints), slot-ordered
    and de-duplicated. A used slot with no assigned spool contributes nothing."""
    ordered: list[str] = []
    for r in sorted(results, key=lambda x: x.get("slot_id", 0)):
        color = r.get("color")
        if color and color not in ordered:
            ordered.append(color)
    return ordered or None


async def _spool_hex_for_slot(db: AsyncSession, printer_id: int, ams_id: int, tray_id: int) -> str | None:
    """``#RRGGBB`` of the built-in spool assigned to a slot, or ``None``."""
    spool_id = await _resolve_spool_id_for_tray(printer_id, ams_id, tray_id, db)
    if spool_id is None:
        return None
    spool = (await db.execute(select(Spool).where(Spool.id == spool_id))).scalar_one_or_none()
    return _spool_color_to_hex(spool.rgba) if spool else None


async def apply_loaded_spool_colors(
    db: AsyncSession,
    archive: PrintArchive,
    printer_id: int,
    ams_mapping: list[int] | None,
) -> None:
    """Overwrite ``archive.filament_color`` from the built-in inventory spools
    loaded on the slots this print uses. Per-slot: spool colour, else that slot's
    3MF colour. No-op in Spoolman mode (completion handles it) or when nothing
    resolves. The caller commits.
    """
    if ((await get_setting(db, "spoolman_enabled")) or "").lower() == "true":
        return

    filament_usage = _plate_filaments(archive)
    mapping = ams_mapping or []

    # 3MF ``slot_id`` is the 1-based filament id; ``ams_mapping`` is 0-based by
    # print-filament index → ``mapping[slot_id - 1]`` is that slot's global tray.
    if filament_usage:
        used_slot_ids = [u["slot_id"] for u in filament_usage]
    else:
        used_slot_ids = list(range(1, len(mapping) + 1))  # no-3MF: every mapped slot

    results: list[dict] = []
    for slot_id in used_slot_ids:
        gi = slot_id - 1
        if gi < 0 or gi >= len(mapping):
            continue
        global_id = mapping[gi]
        if not isinstance(global_id, int) or global_id < 0:
            continue
        ams_id, tray_id = _global_to_ams_tray(global_id)
        hex_ = await _spool_hex_for_slot(db, printer_id, ams_id, tray_id)
        if hex_:
            results.append({"slot_id": slot_id, "color": hex_})

    colors = _archive_colors_from_spools(filament_usage, results) if filament_usage else _colors_no_3mf(results)
    if not colors:
        return

    joined = ",".join(colors)
    if joined != archive.filament_color:
        logger.info(
            "[ArchiveColors] archive %s filament_color %r -> %r (loaded spools, start)",
            archive.id,
            archive.filament_color,
            joined,
        )
        archive.filament_color = joined
