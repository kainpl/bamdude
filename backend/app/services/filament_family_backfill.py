"""One-shot (but idempotent) startup backfill of spool.filament_family_id
(spec A §6). Runs OUTSIDE migrations: resolution wants the catalog and the
mirrors, and a migration must not touch the network or app services. Only
NULL links are filled, so re-running is always safe — an offline boot
backfills what the catalog alone can resolve and the next boot picks up
mirror-dependent ones. (The FE-era resolved_filament_id column was folded
into filament_family_id by m150 itself — plain SQL, before this ever runs.)
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.spool import Spool
from backend.app.services.filament_identity import resolve_raw

logger = logging.getLogger(__name__)


async def backfill_spool_families(db: AsyncSession) -> dict[str, int]:
    summary = {"resolved_raw": 0, "unresolved": 0, "already_linked": 0}
    spools = (await db.execute(select(Spool))).scalars().all()
    for spool in spools:
        if spool.filament_family_id:
            summary["already_linked"] += 1
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
