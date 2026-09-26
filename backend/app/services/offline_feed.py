"""What a switched-off printer holds — so the auto-queue wakes only one that can
run the job (upstream dd50c51c, #2876).

The wake step used to choose by model alone: with every matching printer off it
switched on the first one with an Auto On plug, and only once that printer
reported did routing read what it had loaded — a job for a colour at the far
end of the farm woke every earlier printer in turn, each left running until its
own auto-off.

What an off printer holds is readable while it is off (the owner's ruling,
2026-09-26):

- a slot bound to a spool — BamDude's or Spoolman's — is that spool, as every
  type the assign path can have written for it (``tray_types_written_for``;
  the material column alone is not what the slot says). The assigned inventory
  is the truth, and it outlives a restart;
- an unbound slot is what the printer last reported in this process
  (``printer_manager.last_tray_reading``): its configured type and colour, or
  nothing routing could use;
- with no reading at all a holder is UNKNOWN, and so is a slot whose spool
  could not be read (Spoolman down). Never having heard is not the same as
  nothing being loaded.

:func:`offline_shortfall` passes a printer over only when every holder the job
may draw from is known and no known source gives a channel its material (the
routing equivalences) or its forced colour. Nozzles, profile ids, FTS and
distinct sources are left to routing once the printer is up: a wrong "no" here
would strand the job with nothing ever switched on for it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.app.services.ams_advertised_overlay import slot_key
from backend.app.services.filament_routing import RoutingPolicy, effective_slots, normalized_color
from backend.app.utils.filament_types import filament_types_compatible

logger = logging.getLogger(__name__)

#: A bound slot: every type it can show for its spool, and the spool's colour.
#: ``None`` in the map means the slot has a spool nobody could read.
BoundSpool = tuple[tuple[str, ...], str | None]

_KINDS = {"auto": ("ams", "external"), "ams_only": ("ams",), "external_only": ("external",)}

# Assignments key the external holder as ams_id 255, tray 0/1; the printer
# reports it as vt_tray 254/255.
_EXTERNAL_AMS_ID = 255
_EXTERNAL_TRAY_BASE = 254


@dataclass(frozen=True)
class OfflineSource:
    kind: str
    material: str
    color: str | None


@dataclass(frozen=True)
class OfflineFeed:
    sources: tuple[OfflineSource, ...] = ()
    #: Every AMS slot is accounted for — bound and read, or in a reading.
    ams_known: bool = False
    #: The same for the external holder(s).
    external_known: bool = False

    def known(self, kind: str) -> bool:
        return self.ams_known if kind == "ams" else self.external_known


def _integer(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reported_slots(reading: dict) -> dict[tuple[int, int], tuple[str, dict]]:
    """``{assignment key: (kind, tray)}`` for every slot the reading names."""
    slots: dict[tuple[int, int], tuple[str, dict]] = {}
    for unit in reading.get("ams") or []:
        uid = _integer(unit.get("id")) if isinstance(unit, dict) else None
        if uid is None:
            continue
        for tray in unit.get("tray") or []:
            tid = _integer(tray.get("id")) if isinstance(tray, dict) else None
            if tid is not None:
                slots[slot_key(uid, tid)] = ("ams", tray)
    for tray in reading.get("vt_tray") or []:
        tid = _integer(tray.get("id")) if isinstance(tray, dict) else None
        if tid is not None and tid >= _EXTERNAL_TRAY_BASE:
            slots[(_EXTERNAL_AMS_ID, tid - _EXTERNAL_TRAY_BASE)] = ("external", tray)
    return slots


def feed_from(reading: dict, bound: dict[tuple[int, int], BoundSpool | None]) -> OfflineFeed:
    """Combine the assigned inventory with the last reading — see the module."""
    reported = _reported_slots(reading)
    ams_known = isinstance(reading.get("ams"), list)
    external_known = isinstance(reading.get("vt_tray"), list)
    sources: list[OfflineSource] = []

    def kind_of(key: tuple[int, int]) -> str:
        return "external" if key[0] == _EXTERNAL_AMS_ID else "ams"

    for key in sorted(set(reported) | set(bound)):
        kind = kind_of(key)
        if key in bound:
            spool = bound[key]
            if spool is None:
                if kind == "ams":
                    ams_known = False
                else:
                    external_known = False
                continue
            materials, color = spool
            for material in dict.fromkeys(m for m in materials if m):
                sources.append(OfflineSource(kind, material, color))
            continue
        tray = reported[key][1]
        if tray.get("tray_type"):
            sources.append(OfflineSource(kind, tray["tray_type"], tray.get("tray_color") or None))
    return OfflineFeed(tuple(sources), ams_known, external_known)


def offline_shortfall(requirements, policy: RoutingPolicy, feed: OfflineFeed) -> list[dict]:
    """The channels no known source of this off printer can supply.

    ``[]`` means it cannot be ruled out — including every case this cannot
    judge. Each entry is ``{"slot": channel, "wanted": "TYPE" | "TYPE (#RRGGBB)"}``.
    """
    if requirements.status != "ok" or policy.review_required or policy.mode == "pinned":
        return []
    slots = effective_slots(requirements, policy)
    kinds = _KINDS.get(policy.feed_policy, _KINDS["auto"])
    if slots is None or not all(feed.known(kind) for kind in kinds):
        return []
    usable = [s for s in feed.sources if s.kind in kinds]
    missing: list[dict] = []
    for slot in slots:
        target_color = normalized_color(slot.get("color"))
        if slot["strict"] and target_color is None:
            continue  # routing cannot judge this channel either
        if any(
            filament_types_compatible(source.material, slot["type"])
            and (not slot["strict"] or normalized_color(source.color) == target_color)
            for source in usable
        ):
            continue
        wanted = f"{slot['type']} (#{target_color})" if slot["strict"] else slot["type"]
        missing.append({"slot": slot["slot_id"], "wanted": wanted})
    return missing


async def _is_spoolman_mode(db) -> bool:
    from backend.app.api.routes.settings import get_setting

    try:
        value = await get_setting(db, "spoolman_enabled")
    except Exception:  # noqa: BLE001
        return False
    return bool(value) and str(value).lower() == "true"


async def _bound_internal(db, printer_id: int) -> dict[tuple[int, int], BoundSpool | None]:
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from backend.app.api.routes.inventory import tray_types_written_for
    from backend.app.models.spool_assignment import SpoolAssignment

    rows = (
        (
            await db.execute(
                select(SpoolAssignment)
                .options(selectinload(SpoolAssignment.spool))
                .where(SpoolAssignment.printer_id == printer_id)
            )
        )
        .scalars()
        .all()
    )
    bound: dict[tuple[int, int], BoundSpool | None] = {}
    for row in rows:
        key = slot_key(row.ams_id, row.tray_id)
        if row.spool is None:
            bound[key] = None
            continue
        try:
            written = await tray_types_written_for(db, row.spool, printer_id, row.ams_id, row.tray_id)
        except Exception:  # noqa: BLE001 — a lookup failure is "unknown", never "empty"
            logger.debug("offline feed: slot plan for spool %s failed", row.spool_id, exc_info=True)
            written = {(row.spool.material or "").upper()}
        bound[key] = (tuple(sorted(written)), row.spool.rgba)
    return bound


async def _bound_spoolman(db, printer_id: int) -> dict[tuple[int, int], BoundSpool | None]:
    from sqlalchemy import select

    from backend.app.api.routes._spoolman_helpers import _map_spoolman_spool
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
    from backend.app.services.spoolman import get_spoolman_client
    from backend.app.utils.filament_catalog import family_for_material, material_type

    rows = (
        (await db.execute(select(SpoolmanSlotAssignment).where(SpoolmanSlotAssignment.printer_id == printer_id)))
        .scalars()
        .all()
    )
    if not rows:
        return {}
    client = await get_spoolman_client()
    bound: dict[tuple[int, int], BoundSpool | None] = {}
    for row in rows:
        key = slot_key(row.ams_id, row.tray_id)
        try:
            mapped = _map_spoolman_spool(await client.get_spool(row.spoolman_spool_id))
        except Exception:  # noqa: BLE001 — unreadable is unknown, not absent
            bound[key] = None
            continue
        material = (mapped.get("material") or "").strip()
        # The type the slot plan writes for a spool without a family — the
        # same answer the Spoolman assign path gets (slot_assignment._stand_in).
        family = family_for_material(material)
        written = {material, material_type(material) or "", (family.filament_type if family else "") or ""}
        bound[key] = (tuple(sorted(w.upper() for w in written if w)), mapped.get("rgba"))
    return bound


async def read_offline_feed(db, printer_id: int) -> OfflineFeed:
    """The assigned inventory of either backend, over the printer's last reading."""
    from backend.app.services.printer_manager import printer_manager

    try:
        if await _is_spoolman_mode(db):
            bound = await _bound_spoolman(db, printer_id)
        else:
            bound = await _bound_internal(db, printer_id)
    except Exception:  # noqa: BLE001 — never let this decide "cannot print"
        logger.warning("offline feed: assignments of printer %s unreadable", printer_id, exc_info=True)
        return OfflineFeed()
    return feed_from(printer_manager.last_tray_reading(printer_id), bound)


class OfflineFeedCache:
    """One read per printer per scheduler pass — the matcher's reason and the
    wake step ask about the same printers, and a Spoolman read is a request."""

    def __init__(self) -> None:
        self._feeds: dict[int, OfflineFeed] = {}

    async def get(self, db, printer_id: int) -> OfflineFeed:
        if printer_id not in self._feeds:
            self._feeds[printer_id] = await read_offline_feed(db, printer_id)
        return self._feeds[printer_id]
