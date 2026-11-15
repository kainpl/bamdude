"""One "empty" report does not take a spool out of an idle slot (owner decision Р8; upstream 3497cf0d4).

While nothing prints, a slot whose presence bit reads 0 loses its spool
assignment — the operator took the reel out. An idle X1 Carbon was seen to send
ONE status push that showed a whole AMS unit empty and the reels back in the
next: four assignments were deleted at once, and nothing restores the
assignment of a reel without a tag (with auto stock loading on, the "returning"
reel was even given the oldest full stock spool's identity).

So that one unlink — presence bit off, while idle — waits. It is confirmed only
when the slot still reports the bit off ``HOLD_SECONDS`` later AND at least one
more AMS report has arrived since the first one. A bit that comes back inside
the window was a glitch: the row never left, so no insertion claim runs for a
slot that shows the row's own filament again (a reel that comes back unreadable
is another reel: the stock claim treats the held row as leaving). A reel
really taken out loses its row ``HOLD_SECONDS`` later — the AMS callback runs
only on an AMS change, so :func:`schedule_recheck` asks again by itself.

Deliberately narrow:

* only this branch. A print that runs out (the runout rule, asked first), a
  different tag, a different filament — all as before, at once;
* the external holder is not covered: it has no presence bit;
* memory only, like the weight-sync debounce: a restart starts every clock anew;
* nothing is published and nothing is journalled while a hold is open.

The shape of the real push was not captured on the owner's printers: this is
parity with upstream's fix, with a window well under its 120 s — on a farm a
real swap of an untagged reel is routine, and a print started inside the window
after one would still be charged to the reel that left.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

HOLD_SECONDS = 25.0
# How many times the re-check asks by itself before leaving the slot to the next AMS change.
RECHECKS = 2

Key = tuple[int, int, int]  # (printer_id, ams_id, tray_id)


@dataclass
class _Hold:
    row_id: int
    generation: object
    first_seen: float
    first_report: float | None


_holds: dict[Key, _Hold] = {}
_recheck_tasks: dict[int, asyncio.Task] = {}


def _now() -> float:
    return time.monotonic()


def offer(key: Key, *, row_id: int, generation: object = None, report_stamp: float | None = None) -> bool:
    """The slot reports "no spool" for this assignment again: is the unlink confirmed?

    ``row_id`` — the assignment row; a new row on the slot (a hand assignment
    made meanwhile) is a new clock. ``generation`` — the printer connection: a
    reconnect restarts the clock, cached state confirms nothing. ``report_stamp``
    — when the printer's AMS data last arrived (``state.last_ams_update``):
    confirmation needs a report newer than the first; None = not known, time
    alone decides.
    """
    hold = _holds.get(key)
    now = _now()
    if hold is None or hold.row_id != row_id or hold.generation != generation:
        _holds[key] = _Hold(row_id=row_id, generation=generation, first_seen=now, first_report=report_stamp)
        return False
    if now - hold.first_seen < HOLD_SECONDS:
        return False
    if report_stamp is not None and hold.first_report is not None and report_stamp <= hold.first_report:
        return False
    del _holds[key]
    return True


def is_open(key: Key, *, row_id: int) -> bool:
    """Whether this assignment was reported "no spool" and its clock is still running."""
    hold = _holds.get(key)
    return hold is not None and hold.row_id == row_id


def keep_only(printer_id: int, keys: set[Key]) -> None:
    """Drop every hold of the printer the last pass did not renew — the slot stopped reporting empty."""
    for key in [k for k in _holds if k[0] == printer_id and k not in keys]:
        del _holds[key]


def has_open(printer_id: int) -> bool:
    return any(key[0] == printer_id for key in _holds)


def forget_printer(printer_id: int) -> None:
    keep_only(printer_id, set())
    task = _recheck_tasks.pop(printer_id, None)
    if task is not None and not task.done():
        task.cancel()


def schedule_recheck(printer_id: int, run: Callable[[], Awaitable[None]]) -> None:
    """Ask again when the window ends: a slot that stays empty produces no AMS change of its own."""
    task = _recheck_tasks.get(printer_id)
    if task is not None and not task.done():
        return
    _recheck_tasks[printer_id] = asyncio.create_task(
        _recheck(printer_id, run), name=f"slot-unlink-recheck-{printer_id}"
    )


async def _recheck(printer_id: int, run: Callable[[], Awaitable[None]]) -> None:
    try:
        for _attempt in range(RECHECKS):
            await asyncio.sleep(HOLD_SECONDS + 1)
            if not has_open(printer_id):
                return
            await run()
            if not has_open(printer_id):
                return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — the next AMS change settles it
        logger.exception("Slot unlink re-check failed for printer %s", printer_id)
    finally:
        if _recheck_tasks.get(printer_id) is asyncio.current_task():
            _recheck_tasks.pop(printer_id, None)


async def cancel_all() -> None:
    """Shutdown: no re-check outlives the process's services."""
    tasks = [task for task in _recheck_tasks.values() if not task.done()]
    _recheck_tasks.clear()
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _holds.clear()
