"""Two real sessions meet on a lane or on rows (G03 B): the helpers every pair scenario shares.

PostgreSQL (``DISPATCH_TEST_POSTGRES_URL``): the process lane lock is bypassed so the database guard is what a
second session waits on, and the waiter is observed in ``pg_stat_activity``. A file-backed SQLite: it waits on the
lane's process lock or on SQLite's writer.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text, update

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_ops
from backend.app.services.printer_queues import ensure_printer_queue


class NoProcessLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


LANE_WAIT = ("advisory",)
ROW_WAIT = ("transactionid", "tuple")


async def a_waiter_appears(engine, factory, task, queue_id=None, *, on=LANE_WAIT) -> None:
    """PostgreSQL: a backend of this database waits on a lock of the named kind -- ``advisory`` is the lane guard, a
    row lock shows as ``transactionid`` / ``tuple``. SQLite: a task waits on the lane's process lock (when a lane is
    named), else on SQLite's writer -- which only the task's still being unfinished can show.

    Not filtered by ``application_name``: behind a pooler (the dev server's PgBouncer) a server connection does not
    reliably carry the client's name, and a check by name missed waiters -- and holders -- that were there. The
    disposable database serves one test at a time (each file runs alone), so a waiter in it is this test's."""
    if engine.dialect.name == "postgresql":
        async with factory() as monitor:
            try:
                async with asyncio.timeout(10):
                    while not await monitor.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                            "AND pid <> pg_backend_pid() AND wait_event_type='Lock' AND wait_event = ANY(:events)"
                        ),
                        {"events": list(on)},
                    ):
                        await asyncio.sleep(0.02)
            except TimeoutError:
                # Say what the backends of this database are doing instead, so a failure names the real wait.
                rows = (
                    await monitor.execute(
                        text(
                            "SELECT pid, application_name, state, wait_event_type, wait_event, left(query, 160) "
                            "FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
                        )
                    )
                ).all()
                outcome = None
                if task.done() and not task.cancelled():
                    outcome = repr(task.exception()) if task.exception() else repr(task.result())
                pytest.fail(f"no backend waited on {on}; task outcome={outcome}; backends: {[tuple(r) for r in rows]}")
    elif queue_id is not None:
        lock = queue_ops._queue_claim_locks[queue_id]
        async with asyncio.timeout(10):
            while not getattr(lock, "_waiters", None):
                await asyncio.sleep(0.02)
    else:
        await asyncio.sleep(0.3)
    assert not task.done(), "the second writer did not wait"


async def pending_rows(db, printer_factory, count=2) -> tuple[int, list[int]]:
    printer = await printer_factory()
    await ensure_printer_queue(db, printer.id)
    rows = [PrintQueueItem(queue_id=printer.id, position=i, status="pending") for i in range(count)]
    db.add_all(rows)
    await db.commit()
    return printer.id, [row.id for row in rows]


def bypass_the_process_lane(engine, monkeypatch, queue_id):
    if engine.dialect.name == "postgresql":
        monkeypatch.setitem(queue_ops._queue_claim_locks, queue_id, NoProcessLock())


async def claim_holding_the_lane(factory, queue_id, item_id, reached, release, **claimed):
    """A claim's shape: the lane, the header and the job row FOR NO KEY UPDATE, then pending -> printing on a row that
    is still runnable (pending, no manual start). ``claimed`` adds what a claim stamps (its token, the voided proof)."""
    async with factory() as db, queue_ops.claim_lanes(db, [queue_id]):
        await db.scalar(select(PrinterQueue.id).where(PrinterQueue.id == queue_id).with_for_update(key_share=True))
        await db.scalar(select(PrintQueueItem.id).where(PrintQueueItem.id == item_id).with_for_update(key_share=True))
        reached.set()
        await asyncio.wait_for(release.wait(), 20)
        result = await db.execute(
            update(PrintQueueItem)
            .where(
                PrintQueueItem.id == item_id,
                PrintQueueItem.status == "pending",
                PrintQueueItem.manual_start.is_(False),
            )
            .values(status="printing", **claimed)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount


def held_after(monkeypatch, owner, name):
    """``owner.name`` runs, then waits once for ``release``: the writer stopped inside its lane after its row work."""
    reached, release = asyncio.Event(), asyncio.Event()
    real = getattr(owner, name)
    armed = [True]

    async def held(*args, **kwargs):
        result = await real(*args, **kwargs)
        if armed[0]:
            armed[0] = False
            reached.set()
            await asyncio.wait_for(release.wait(), 20)
        return result

    monkeypatch.setattr(owner, name, held)
    return reached, release


def go() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


def held_inside(monkeypatch, owner, name):
    """``owner.name`` -- an async context manager -- is entered, then waits once for ``release`` before its body: the
    writer stopped just inside its lane."""
    from contextlib import asynccontextmanager

    reached, release = asyncio.Event(), asyncio.Event()
    real = getattr(owner, name)
    armed = [True]

    @asynccontextmanager
    async def held(*args, **kwargs):
        async with real(*args, **kwargs) as value:
            if armed[0]:
                armed[0] = False
                reached.set()
                await asyncio.wait_for(release.wait(), 20)
            yield value

    monkeypatch.setattr(owner, name, held)
    return reached, release
