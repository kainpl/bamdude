"""A claimed job that nobody reports on must not sit there looking like progress.

``claimed`` reads on screen as "printing". A bridge that dies mid-job is exactly
the case where no further poll arrives, so nothing else will ever notice — which
is why this is a background sweep and not a check on the next poll.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.label_device import LabelJob

logger = logging.getLogger(__name__)

#: How long a device may hold a job before we assume it will never report.
#: Generous on purpose: a printer that is mid-paper-feed is not stuck.
CLAIM_DEADLINE_SECONDS = 300
#: ⚠️ After this many, the job fails visibly instead of cycling forever. A job
#: that requeues itself indefinitely is a queue that never empties and never
#: says why.
MAX_ATTEMPTS = 3
_SWEEP_INTERVAL_SECONDS = 60


def _now() -> datetime:
    """⚠️ UTC, naive — every timestamp in this database is."""
    return datetime.now(UTC).replace(tzinfo=None)


async def reclaim_stale_jobs(db: AsyncSession, *, older_than_seconds: int, max_attempts: int) -> int:
    """Put timed-out claims back in the queue, or fail them. Returns how many.

    The attempt counter goes up on the way back to the queue rather than on the
    way out of it: a job handed out and printed successfully never passes
    through here, so counting at hand-out time would punish the healthy path.
    """
    cutoff = _now() - timedelta(seconds=older_than_seconds)
    stale = (
        (
            await db.execute(
                select(LabelJob).where(
                    LabelJob.status == "claimed",
                    LabelJob.claimed_at.is_not(None),
                    LabelJob.claimed_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    if not stale:
        return 0

    requeued, failed = [], []
    for job in stale:
        if (job.attempts or 0) + 1 > max_attempts:
            failed.append(job.id)
        else:
            requeued.append(job.id)

    if requeued:
        await db.execute(
            update(LabelJob)
            .where(LabelJob.id.in_(requeued))
            .values(status="queued", claimed_at=None, attempts=LabelJob.attempts + 1)
        )
    if failed:
        # ⚠️ ``error`` is deliberately not touched. A device that managed to say
        # why before dying has already written the useful message there, and
        # overwriting it with "gave up" would lose the only diagnosis there is.
        # A job that never reported anything keeps a NULL, which is itself the
        # accurate answer: nobody ever said.
        await db.execute(update(LabelJob).where(LabelJob.id.in_(failed)).values(status="failed"))
    await db.commit()

    total = len(requeued) + len(failed)
    logger.info("label job sweep: %d requeued, %d failed", len(requeued), len(failed))
    return total


async def reclaim_loop() -> None:
    """A background sweep, not a check on the next poll.

    The case that matters is the bridge dying mid-job — precisely the case where
    no further poll arrives. A lazy sweep would leave that job ``claimed``
    forever, and ``claimed`` reads on screen as "printing".
    """
    from backend.app.core.database import async_session

    while True:
        try:
            async with async_session() as db:
                await reclaim_stale_jobs(db, older_than_seconds=CLAIM_DEADLINE_SECONDS, max_attempts=MAX_ATTEMPTS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("label job reclaim sweep failed")
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)


__all__ = ["CLAIM_DEADLINE_SECONDS", "MAX_ATTEMPTS", "reclaim_loop", "reclaim_stale_jobs"]
