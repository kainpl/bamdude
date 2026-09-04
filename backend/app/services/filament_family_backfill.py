"""One-shot (but idempotent) startup backfill of spool.filament_family_id
(spec A §6). Runs OUTSIDE migrations: resolution wants the catalog and the
mirrors, and a migration must not touch the network or app services. NULL
links are filled and links the resolver refuses are re-derived, so re-running
is always safe — an offline boot backfills what the catalog alone can resolve
and the next boot picks up mirror-dependent ones. (The FE-era
resolved_filament_id column was folded into filament_family_id by m150 itself
— plain SQL, before this ever runs.)

⚠️ A non-NULL link is not proof of a good link. m079 derived the FE-era column
by stripping the S off every ``GFS*`` code, which turned the support families
(``GFS00`` Support W, ``GFS04`` PVA …) into ``GF00``-style ids that exist
nowhere, and m150 copied them raw. "Only NULLs are filled" then protected the
garbage forever, and every edit of such a spool was refused with ``unknown
filament family`` (2026-09-04). So a link ``resolve_tray`` rejects is
re-derived from the slicer code, and when nothing can re-derive it, it is
cleared — an honest NULL the picker can fill beats a value the API refuses.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool
from backend.app.services.filament_identity import resolve_raw, resolve_tray

logger = logging.getLogger(__name__)


async def backfill_spool_families(db: AsyncSession) -> dict[str, int]:
    summary = {"resolved_raw": 0, "unresolved": 0, "already_linked": 0, "repaired": 0, "cleared": 0}
    spools = (await db.execute(select(Spool))).scalars().all()
    for spool in spools:
        if spool.filament_family_id:
            if (await resolve_tray(db, spool.filament_family_id)).family is not None:
                summary["already_linked"] += 1
                continue
            resolved = await resolve_raw(db, spool.slicer_filament)
            if resolved.family:
                logger.info(
                    "spool %s: family link %r resolves nowhere, re-derived %r from slicer code %r",
                    spool.id,
                    spool.filament_family_id,
                    resolved.family.filament_id,
                    spool.slicer_filament,
                )
                spool.filament_family_id = resolved.family.filament_id
                summary["repaired"] += 1
            else:
                logger.warning(
                    "spool %s: family link %r resolves nowhere and slicer code %r cannot replace it — cleared",
                    spool.id,
                    spool.filament_family_id,
                    spool.slicer_filament,
                )
                spool.filament_family_id = None
                summary["cleared"] += 1
            continue
        resolved = await resolve_raw(db, spool.slicer_filament)
        if resolved.family:
            spool.filament_family_id = resolved.family.filament_id
            summary["resolved_raw"] += 1
            continue
        summary["unresolved"] += 1
    await db.commit()
    logger.info("spool family backfill: %s", summary)
    return summary
