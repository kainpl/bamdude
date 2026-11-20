"""An archive's deletion is a plan writer for the jobs it writes (G03 B, A19).

Both deletes of an archive -- the trash (``source_trashed``: its pending jobs that cannot print without it are
cancelled) and the hard delete (``source_purged``: those cancelled, then the link cut on every row naming it) --
write job rows and router rows of other printers' queues. They take the shape ``library_delete_scope`` has for the
library:

1. *footprint, before any guard*: in a clean session, the lanes of the printer rows the delete will write (the
   trash: the pending rows naming the archive; the hard delete: every row naming it), the AutoQueue rows naming it
   and the printers whose plate gate names it;
2. *guards in one step*: ``claim_lanes(lanes, archive_id=<the archive>, before_db=<early rows>)`` -- the archive's
   process lock, the lanes' process locks, then SQLite's writer, the caller's early rows (the orders a trashed
   print leaves) and the gate printers ``FOR NO KEY UPDATE``, then PostgreSQL's archive and lane guards;
3. *authoritative re-read* (:meth:`ArchiveDeletePlan.recheck`, after the caller locked the archive row): a printer
   row in a lane not held, a router row not in the footprint or a gate armed on the archive since is a refusal --
   409 ``queue_selection_changed``, nothing written -- and the router rows are locked ascending (A20);
4. *writes*: the release's compare-and-sets (``queue_source_release``), inside the lanes.

The recount stays after the commit (``recount_released_queues``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer


@dataclass(frozen=True)
class ArchiveDeleteFootprint:
    lanes: frozenset[int]
    auto_ids: frozenset[int]
    gate_printers: frozenset[int]


async def footprint(db: AsyncSession, archive_id: int, *, purge: bool) -> ArchiveDeleteFootprint:
    jobs = select(PrintQueueItem.queue_id).where(PrintQueueItem.archive_id == archive_id)
    autos = select(AutoQueueItem.id).where(AutoQueueItem.archive_id == archive_id)
    if not purge:
        jobs = jobs.where(PrintQueueItem.status == "pending")
        autos = autos.where(AutoQueueItem.status == "pending")
    gates = select(Printer.id).where(Printer.awaiting_plate_clear_archive_id == archive_id)
    return ArchiveDeleteFootprint(
        frozenset(await db.scalars(jobs)),
        frozenset(await db.scalars(autos)),
        frozenset(await db.scalars(gates)),
    )


class ArchiveDeletePlan:
    def __init__(self, db: AsyncSession, archive_id: int, purge: bool, planned: ArchiveDeleteFootprint):
        self.db = db
        self.archive_id = archive_id
        self.purge = purge
        self.planned = planned

    async def recheck(self) -> None:
        """Step 3, after the caller's archive row lock: the footprint read again under the guards."""
        from backend.app.services.queue_cancel_scope import selection_changed
        from backend.app.services.queue_counters import lock_auto_rows

        now = await footprint(self.db, self.archive_id, purge=self.purge)
        if (
            not now.lanes <= self.planned.lanes
            or not now.auto_ids <= self.planned.auto_ids
            or not now.gate_printers <= self.planned.gate_printers
        ):
            raise selection_changed()
        await lock_auto_rows(self.db, self.planned.auto_ids)


@asynccontextmanager
async def archive_delete_scope(
    db: AsyncSession, archive_id: int, *, purge: bool, first: Callable[[], Awaitable[object]] | None = None
):
    """Steps 1 and 2; yields the plan whose :meth:`~ArchiveDeletePlan.recheck` the caller runs under its row lock."""
    from backend.app.services.queue_ops import claim_lanes

    if db.in_transaction():
        await db.commit()  # the footprint and the guards start clean: no writer of ours waits for itself
    async with async_sessionmaker(db.bind, expire_on_commit=False)() as preflight:
        planned = await footprint(preflight, archive_id, purge=purge)

    async def early(session: AsyncSession) -> None:
        if session.get_bind().dialect.name == "sqlite" and not session.in_transaction():
            await session.execute(text("BEGIN IMMEDIATE"))
        if first is not None:
            await first()
        for printer_id in sorted(planned.gate_printers):
            await session.scalar(select(Printer.id).where(Printer.id == printer_id).with_for_update(key_share=True))

    async with claim_lanes(db, sorted(planned.lanes), archive_id=archive_id, before_db=early):
        yield ArchiveDeletePlan(db, archive_id, purge, planned)
