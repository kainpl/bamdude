"""Record on each spool when it was last dried (upstream 84f320b85).

An AMS drying cycle dries every spool in that unit, but the AMS humidity only
describes the unit, so once a spool is moved the fact is lost. When a cycle
ends, every spool ASSIGNED to a slot of that printer + AMS unit is stamped with
the time, the target temperature and the hours the cycle actually ran, in
whichever inventory holds the slot assignments.

A cycle counts when it ran at least ``DRIED_MIN_FRACTION`` of its length or
ended with its countdown all but exhausted. One stopped a few minutes in — by
the operator, the firmware or a print taking priority — dried nothing and must
not overwrite an earlier, real drying.

The rule reads only what the MQTT client saw of the countdown
(``DryingCycleEnd``), so it is the same for a cycle started from the printer
card, by auto-drying, by a schedule or at the printer's own screen.

This module and the spool PATCH (single and bulk, which take the date alone)
are the only writers of ``spool.last_dried_*``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.services.bambu_mqtt import DryingCycleEnd

logger = logging.getLogger(__name__)

# Share of a cycle's length that must have run for its spools to count as dried.
DRIED_MIN_FRACTION = 0.5

# A cycle that runs to term ends with its countdown all but exhausted; the last
# ``dry_time`` seen can trail the end by a push or two.
RAN_TO_TERM_MINUTES = 5

# Spoolman ``spool.extra`` keys of the record. JSON strings, like the other
# ``bambu_*`` extras; an empty string means unknown or cleared.
SPOOLMAN_LAST_DRIED_AT_KEY = "bambu_last_dried_at"
SPOOLMAN_LAST_DRIED_TEMP_KEY = "bambu_last_dried_temp"
SPOOLMAN_LAST_DRIED_HOURS_KEY = "bambu_last_dried_hours"


@dataclass(frozen=True)
class DryingRecord:
    temp: int | None
    hours: float | None


def drying_record(cycle: DryingCycleEnd) -> DryingRecord | None:
    """What a finished cycle says about its spools, or None when it does not count.

    The cycle's length is the countdown itself when its start was watched —
    never the request: a start sent over a timer the printer keeps and does not
    run would otherwise read, at the stop, as most of a long cycle that never
    heated. Met already counting, the length is the requested duration when
    BamDude started the cycle, else the highest countdown seen; with nothing
    requested that peak understates the length, so the elapsed share is a lower
    bound and the hours are left unknown rather than understated. A cycle that
    ran to term counts either way.
    """
    requested = (cycle.target_hours or 0) * 60
    length = cycle.peak_minutes if cycle.start_seen else max(cycle.peak_minutes, requested)
    elapsed = max(0, length - cycle.remaining_minutes)
    ran_to_term = cycle.remaining_minutes <= RAN_TO_TERM_MINUTES
    if not ran_to_term and (length <= 0 or elapsed / length < DRIED_MIN_FRACTION):
        return None
    length_known = cycle.start_seen or requested > 0
    hours = round(elapsed / 60, 1) if length_known and elapsed > 0 else None
    return DryingRecord(temp=cycle.target_temp, hours=hours)


def utc_now() -> datetime:
    """Now as the spool's naive ``DateTime`` columns hold a moment: UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def spoolman_extra(dried_at: datetime | None, record: DryingRecord | None = None) -> dict[str, str]:
    """The three extra values of a record; ``None`` clears a value an earlier cycle left."""
    return {
        SPOOLMAN_LAST_DRIED_AT_KEY: json.dumps(dried_at.isoformat(timespec="seconds") if dried_at else ""),
        SPOOLMAN_LAST_DRIED_TEMP_KEY: json.dumps("" if record is None or record.temp is None else str(record.temp)),
        SPOOLMAN_LAST_DRIED_HOURS_KEY: json.dumps("" if record is None or record.hours is None else str(record.hours)),
    }


async def _spoolman_owns_slots(db: AsyncSession) -> bool:
    from backend.app.api.routes.settings import get_setting

    return ((await get_setting(db, "spoolman_enabled")) or "").lower() == "true"


async def _stamp_internal(db: AsyncSession, printer_id: int, ams_id: int, record: DryingRecord) -> int:
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment

    result = await db.execute(
        select(Spool)
        .join(SpoolAssignment, SpoolAssignment.spool_id == Spool.id)
        .where(SpoolAssignment.printer_id == printer_id, SpoolAssignment.ams_id == ams_id)
    )
    spools = list(result.scalars().unique().all())
    if not spools:
        return 0
    # The rows were read by printer id, and the id may name the next printer by
    # now: the write is made under the record's hold, proved again under it
    # (read, hold, prove, then write - inv-deleted-printer-runtime-not-inherited).
    from backend.app.services.printer_manager import printer_manager

    if not await printer_manager.hold_record(db, printer_id):
        await db.rollback()
        logger.info("Drying not recorded for printer %d: the printer record is gone", printer_id)
        return 0
    now = utc_now()
    for spool in spools:
        spool.last_dried_at = now
        spool.last_dried_temp = record.temp
        spool.last_dried_hours = record.hours
    await db.commit()
    return len(spools)


async def _stamp_spoolman(db: AsyncSession, printer_id: int, ams_id: int, record: DryingRecord) -> int:
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
    from backend.app.services import spoolman, spoolman_spool_identity

    result = await db.execute(
        select(SpoolmanSlotAssignment.spoolman_spool_id, SpoolmanSlotAssignment.spool_registered).where(
            SpoolmanSlotAssignment.printer_id == printer_id,
            SpoolmanSlotAssignment.ams_id == ams_id,
        )
    )
    # A row names its spool by stamp too (m266): number → the stamp the binding holds.
    stamps = dict(result.all())
    spool_ids = sorted(stamps)
    if not spool_ids:
        return 0
    await db.commit()  # no read transaction is carried across the network
    client = await spoolman.get_spoolman_client()
    if client is None:
        logger.info("Drying not recorded on Spoolman spools %s: Spoolman is not connected", spool_ids)
        return 0
    # ``merge_spool_extra`` registers the keys a write carries before it sends
    # them (Spoolman refuses an unknown extra key).
    fields = spoolman_extra(utc_now(), record)
    # These writes go to Spoolman, not to our database, so no row lock can cover
    # them (and none is held across the network): the session is asked again
    # after every await, before the next spool of this printer id is addressed.
    from backend.app.services.printer_manager import printer_manager

    stamped = 0
    for spool_id in spool_ids:
        if not printer_manager.session_still_lives(printer_id):
            logger.info("Drying not recorded on Spoolman spool %d: the printer record is gone", spool_id)
            break
        try:
            # The spool the number names now must be the bound one (spec
            # ``spoolman-binding-identity`` §5.3): another spool's stamp, or no spool, is
            # not written to, and the binding goes (outside a print's accounting).
            try:
                spool = await client.get_spool(spool_id)
            except spoolman.SpoolmanNotFoundError:
                spool = None
            judged = spoolman_spool_identity.verdict(stamps[spool_id], spool)
            if judged in (spoolman_spool_identity.OTHER, spoolman_spool_identity.GONE):
                logger.warning(
                    "Drying not recorded on Spoolman spool %d: the number names %s now",
                    spool_id,
                    "no spool" if spool is None else "another spool",
                )
                await spoolman_spool_identity.drop_stale(
                    spool_id,
                    spoolman_spool_identity.stamp_of(spool),
                    "the spool is gone from Spoolman" if spool is None else "the number names another spool",
                )
                continue
            if judged == spoolman_spool_identity.UNKNOWN:
                await spoolman_spool_identity.adopt_now(spool_id, spoolman_spool_identity.stamp_of(spool))
            await client.merge_spool_extra(spool_id, fields)
            stamped += 1
        except Exception as exc:  # noqa: BLE001 - one unreachable spool must not skip the rest
            logger.warning("Could not record drying on Spoolman spool %d: %s", spool_id, exc)
    return stamped


async def record_drying_cycle(db: AsyncSession, printer_id: int, cycle: DryingCycleEnd) -> int:
    """Stamp the spools of the AMS unit whose drying cycle just ended. Returns how many."""
    record = drying_record(cycle)
    if record is None:
        logger.info(
            "Printer %d AMS %d drying ended with %d of %d minutes left: too short to mark its spools as dried",
            printer_id,
            cycle.ams_id,
            cycle.remaining_minutes,
            max(cycle.peak_minutes, (cycle.target_hours or 0) * 60),
        )
        return 0

    if await _spoolman_owns_slots(db):
        stamped = await _stamp_spoolman(db, printer_id, cycle.ams_id, record)
    else:
        stamped = await _stamp_internal(db, printer_id, cycle.ams_id, record)

    logger.info(
        "Printer %d AMS %d drying finished (%s °C, %s h): marked %d spool(s) as dried",
        printer_id,
        cycle.ams_id,
        record.temp if record.temp is not None else "?",
        record.hours if record.hours is not None else "?",
        stamped,
    )
    if stamped:
        from backend.app.core.websocket import ws_manager

        await ws_manager.broadcast({"type": "inventory_changed"})
    return stamped
