"""Which printer an AutoQueue job takes among printers equally fit for it (C01, upstream #3480 / #2915).

The operator chooses the order in Settings › Queue (``model_dispatch_order``):

* ``fewest_hours`` (the default) — the least worn machine first, so the farm's work and wear
  spread instead of all landing on the lowest printer id;
* ``most_hours`` — the most worn first, to use up the machines already near replacement;
* ``least_recently_used`` — the printer whose last finished print is the oldest; one that never
  finished a print goes first;
* ``priority`` — the printers' own ``dispatch_priority``, lowest first; one without goes last.

Hours are the maintenance counter's: ``runtime_seconds / 3600 + print_hours_offset``. Ties in
every order fall to the lower printer id — ``placement_rank``'s last key.

⚠️ **The one reader** (Key Invariant «The printer order of a placement has one reader»). The
router (``auto_queue_eligibility.find_eligible_printer``), the wake step
(``offline_candidates_for``), the rebalancer's receivers and its replay all rank with the key
this module builds, in ``printer_occupancy.placement_rank``'s one tuple — after readiness, exact
model, colour matches and deferred rows, before the remaining-filament preference (owner D3). A
second reader of the setting or of a printer's priority would let one of them pick differently,
and the rebalancer would forecast a router that does not exist.
``test_printer_pick_order_has_one_reader.py`` holds it.

**What is read** (spec r2 §1). Every active, non-archived printer — no queue, auto-distribute or
connection filter — so the switched-off printers the wake step orders have facts too. A printer
the read did not see (one created during the pass) has none, and ``()`` sorts below every key:
it ranks after every printer with facts at this key, never "neutral". Under
``least_recently_used`` the last finish is the newest ``completed_at`` of the printer's archives
that are prints: a dispatch error, an aborted attempt and a row that only filed a file
(``status == "archived"``) printed nothing, and upstream's print log never holds them. A finished
print moved to the trash still counts — upstream's run outlives the deletion of its archive.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

SETTING_KEY = "model_dispatch_order"
ORDERS = ("fewest_hours", "most_hours", "least_recently_used", "priority")
DEFAULT = "fewest_hours"


@dataclass(frozen=True)
class PickFacts:
    """What the order reads about one printer."""

    hours: float  # runtime_seconds / 3600 + print_hours_offset
    #: Seconds relative to the read's ``now`` (≤ 0 when read; the replay moves it forward); None = never finished a print.
    last_finished: float | None
    priority: int | None


def pick_key(order: str, facts: PickFacts | None) -> tuple:
    """The printer's place in ``order`` — larger is picked first. No facts → ``()``, below every key."""
    if facts is None:
        return ()
    if order == "most_hours":
        return (facts.hours,)
    if order == "least_recently_used":
        return (1, 0.0) if facts.last_finished is None else (0, -facts.last_finished)
    if order == "priority":
        return (facts.priority is not None, -(facts.priority or 0))
    return (-facts.hours,)


@dataclass(frozen=True)
class PickOrder:
    """The order and every printer's facts, read once for a pass."""

    order: str
    facts: Mapping[int, PickFacts]

    def key(self, printer_id: int) -> tuple:
        return pick_key(self.order, self.facts.get(printer_id))


def advanced(facts: PickFacts | None, *, seconds: float, finished_at: float) -> PickFacts | None:
    """The facts after a print of ``seconds`` printing time ended at ``finished_at`` — the replay's step.

    Printing time only: the printer's counter grows only while it reports RUNNING, so neither the
    preparation before a start nor the plate service after it belongs here.
    """
    if facts is None:
        return None
    return replace(facts, hours=facts.hours + seconds / 3600.0, last_finished=finished_at)


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


async def read_pick_order(db: AsyncSession, *, now: datetime) -> PickOrder:
    """The order setting and the facts of every active, non-archived printer, read now."""
    from backend.app.models.archive import PrintArchive
    from backend.app.models.printer import Printer
    from backend.app.models.settings import Settings
    from backend.app.services.filament_deferred import dispatched_archive_filter
    from backend.app.services.statistics.outcomes import not_dispatch_failure

    stored = await db.scalar(select(Settings.value).where(Settings.key == SETTING_KEY))
    order = stored if stored in ORDERS else DEFAULT
    rows = (
        await db.execute(
            select(Printer.id, Printer.runtime_seconds, Printer.print_hours_offset, Printer.dispatch_priority).where(
                Printer.is_active.is_(True), Printer.archived.is_(False)
            )
        )
    ).all()
    finished: dict[int, float] = {}
    if order == "least_recently_used" and rows:
        reference = _utc(now)
        last = (
            await db.execute(
                select(PrintArchive.printer_id, func.max(PrintArchive.completed_at))
                .where(
                    PrintArchive.printer_id.in_([row.id for row in rows]),
                    PrintArchive.completed_at.is_not(None),
                    PrintArchive.status != "archived",
                    not_dispatch_failure(),
                    dispatched_archive_filter(),
                )
                .group_by(PrintArchive.printer_id)
            )
        ).all()
        finished = {
            printer_id: (_utc(moment) - reference).total_seconds()
            for printer_id, moment in last
            if printer_id is not None and moment is not None
        }
    facts = {
        row.id: PickFacts(
            hours=(row.runtime_seconds or 0) / 3600.0 + (row.print_hours_offset or 0.0),
            last_finished=finished.get(row.id),
            priority=row.dispatch_priority,
        )
        for row in rows
    }
    return PickOrder(order, facts)
