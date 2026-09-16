"""``filament_low`` for the spools usage tracking cannot see.

``usage_tracker._warn_if_low_stock`` has fired ``filament_low`` for every spool
bound to BamDude's own inventory since m117 — right after each consumption
write, against the spool's own override or the global ``low_stock_threshold``,
with the persisted ``Spool.low_stock_notified`` memory. Two kinds of slot never
reach it: a tray with **no inventory binding** (a Bambu RFID spool whose only
figure is the printer's ``remain`` counter) and a **Spoolman-bound** slot (no
``Spool`` row, so no ``weight_used`` write ever happens here). This module
covers those two, from ``on_ams_change``, against the SAME global threshold —
one setting, Settings → Inventory — so the notification can never disagree
with what the Inventory page calls low. BamDude-bound slots are skipped here
on purpose: two announcers for one spool would be the flood m117 exists to
prevent.

**Remaining** is what prefer-lowest reads (#1508), not a fourth figure: a
Spoolman-bound slot uses ``remaining_weight`` over ``initial_weight``
(``PrintScheduler._build_inventory_remain_overrides`` + ``_inventory_label_weights``);
an unbound slot uses the printer's ``remain``, which is ``-1`` for anything but
a Bambu spool and then says nothing — no percent, no event.

**One announcement per spool per slot**, per process: the event fires when a
slot crosses below the threshold and is re-armed when the tray identity changes
(a new spool went in) or remaining climbs back to threshold + ``HYSTERESIS``.
A restart may repeat an already-low slot once — a reminder, not a flood. The
bound spools' memory is persisted because their trigger is every print; this
trigger is an AMS change, which a restart does not replay by itself.

⚠️ Not ``filament_deficit``. That event compares ONE job with what is in the
slot; this one is a threshold on the spool, regardless of any job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services.auto_queue_ams import build_loaded_filaments
from backend.app.services.filament_deficit import _slot_label
from backend.app.services.usage_tracker import _global_low_stock_threshold

logger = logging.getLogger(__name__)

#: Percent points ABOVE the threshold a slot must climb back to before it can
#: announce again — a spool hovering at the line must not fire on every sync.
HYSTERESIS = 5.0

#: ``(printer_id, global_tray_id) -> tray identity`` of the spool already announced.
_announced: dict[tuple[int, int], str] = {}


@dataclass(frozen=True)
class LowSlot:
    global_tray_id: int
    label: str
    percent: int
    color: str | None


async def read_threshold(db: AsyncSession) -> float:
    """The inventory's global low-stock threshold (percent), clamped to 0..100.
    0 disables the event — the Inventory page's own setting never allows it,
    but a hand-edited row must not fire on everything."""
    try:
        return max(0.0, min(100.0, float(await _global_low_stock_threshold(db))))
    except (TypeError, ValueError):
        return 0.0


def _identity(f: dict) -> str:
    """What makes this tray THIS spool: the RFID uuid when the printer has one,
    else the material and colour the slot reports."""
    for key in ("tray_uuid", "tag_uid"):
        v = f.get(key)
        if v and str(v).strip("0"):
            return f"{key}:{v}"
    return f"{f.get('type') or ''}|{f.get('color') or ''}"


async def _bindings(
    db: AsyncSession, printer_id: int, loaded: list[dict]
) -> tuple[bool, dict[int, float], dict[int, float]]:
    """``(spoolman_mode, remaining_grams_by_gtid, label_grams_by_gtid)`` for the
    slots bound to an inventory spool — the binding prefer-lowest reads."""
    from backend.app.services.print_scheduler import scheduler

    grams = await scheduler._build_inventory_remain_overrides(db, printer_id, loaded)
    if not grams:
        return False, {}, {}
    spoolman = await scheduler._is_spoolman_mode(db)
    labels = await scheduler._inventory_label_weights(db, printer_id, loaded) if spoolman else {}
    return spoolman, grams, labels


def _percent(f: dict, bound: tuple[float, float | None] | None) -> int | None:
    if bound is not None:
        remaining, label = bound
        if not label or label <= 0:
            return None
        return max(0, min(100, round(remaining / label * 100)))
    remain = f.get("remain", -1)
    if isinstance(remain, bool) or not isinstance(remain, (int, float)) or remain < 0:
        return None
    return max(0, min(100, int(remain)))


async def evaluate(db: AsyncSession, printer_id: int, status) -> list[LowSlot]:
    """Decide which slots of this printer cross the threshold now, and remember them.

    Returns the slots to announce (possibly none). Mutates the per-process
    memory: a slot that fires is remembered under its tray identity; a slot
    that climbed back above threshold + hysteresis, whose spool changed, that
    is no longer loaded, or that inventory tracking owns, is forgotten.
    """
    threshold = await read_threshold(db)
    loaded = build_loaded_filaments(status, printer_id) if status is not None else []
    loaded_ids = {f["global_tray_id"] for f in loaded}
    for key in [k for k in _announced if k[0] == printer_id and k[1] not in loaded_ids]:
        del _announced[key]
    if threshold <= 0 or not loaded:
        return []

    spoolman, grams, labels = await _bindings(db, printer_id, loaded)
    to_announce: list[LowSlot] = []
    for f in loaded:
        gtid = f["global_tray_id"]
        key = (printer_id, gtid)
        bound = gtid in grams
        if bound and not spoolman:
            # BamDude's own inventory: usage tracking warns for this spool with
            # a persisted memory. Not a second announcer.
            _announced.pop(key, None)
            continue
        percent = _percent(f, (grams[gtid], labels.get(gtid)) if bound else None)
        if percent is None:
            continue
        identity = _identity(f)
        if percent < threshold:
            if _announced.get(key) == identity:
                continue
            _announced[key] = identity
            to_announce.append(LowSlot(gtid, _slot_label(f, gtid), percent, f.get("color") or None))
        elif key in _announced and (percent >= threshold + HYSTERESIS or _announced[key] != identity):
            del _announced[key]
    return to_announce


async def check_printer(db: AsyncSession, printer_id: int) -> int:
    """Evaluate one printer off its live status and send what crossed. Returns
    how many events were sent. Best-effort: never raises into the AMS sync."""
    from backend.app.services.notification_service import notification_service
    from backend.app.services.printer_manager import printer_manager

    try:
        status = printer_manager.get_status(printer_id)
        printer = printer_manager.get_printer(printer_id)
        low = await evaluate(db, printer_id, status)
        if not low:
            return 0
        printer_name = getattr(printer, "name", None) or f"Printer {printer_id}"
        for slot in low:
            await notification_service.on_filament_low(
                printer_id, printer_name, slot.label, slot.percent, db, color=slot.color
            )
        return len(low)
    except Exception as e:  # noqa: BLE001 — a notification must never break the AMS sync
        logger.warning("filament_low check failed for printer %s: %s", printer_id, e)
        return 0


def forget_printer(printer_id: int) -> None:
    """Drop the memory for a printer (tests, and a printer that was removed)."""
    for key in [k for k in _announced if k[0] == printer_id]:
        del _announced[key]
