"""Two real sessions meet on a lane or on rows (G03 B): the helpers every pair scenario shares.

PostgreSQL (``DISPATCH_TEST_POSTGRES_URL``): the process lane lock is bypassed so the database guard is what a
second session waits on, and the waiter is observed by its own backend's pid -- its ungranted lock in ``pg_locks``
and the holder ``pg_blocking_pids`` names (:func:`postgres_edge`). A file-backed SQLite: it waits on the lane's
process lock or on SQLite's writer.
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
ANY_WAIT = ("transactionid", "tuple", "advisory")
LANE_GUARD = 1625  # pg_advisory_xact_lock(1625, queue_id): the lane's database guard (queue_ops)

_UNGRANTED = text(
    "SELECT pid, locktype, classid, objid, objsubid, pg_blocking_pids(pid) AS blockers "
    "FROM pg_locks WHERE NOT granted AND pid = ANY(:pids)"
)


def _outcome(task) -> str | None:
    if not task.done():
        return None
    if task.cancelled():
        return "cancelled"
    return repr(task.exception()) if task.exception() else repr(task.result())


def _matches(lock, on, queue_id) -> bool:
    if lock.locktype not in on:
        return False
    if lock.locktype == "advisory" and on == LANE_WAIT:
        # The lane's guard itself, and for a named lane that very key -- never another advisory lock.
        if lock.classid != LANE_GUARD or lock.objsubid != 2:
            return False
        return queue_id is None or lock.objid == queue_id
    return True


async def postgres_edge(factory, task, queue_id=None, *, on=LANE_WAIT, behind=None) -> tuple[int, list[int]]:
    """PostgreSQL: wait until a transaction of ``task`` itself waits on a lock of the named kind, and return
    ``(waiter pid, holder pids)`` -- the edge ``pg_blocking_pids`` reports for that very backend.

    The waiter and the holder are known by pid: ``queue_test_database`` names each transaction's backend after the
    task that began it, and each task after the task that created it -- a transaction of ``task`` is one begun by it
    or by a task it created (a route the HTTP client runs is in a task of the middleware's). A lane wait is the lane guard's advisory lock (for a named lane, that key); a row wait is
    ``transactionid`` / ``tuple``. Every holder must be another task of this test (``behind`` or a task it created,
    when given) -- a foreign backend is a failure, not a match -- and the waiter's task must still be running: a task that ended
    before its wait was seen fails at once. ``pg_locks`` and ``pg_blocking_pids`` are read live, the monitor's
    transaction is ended on every poll (``pg_stat_activity`` would answer from a snapshot of its first read)."""
    from backend.tests.integration.queue_test_database import BACKENDS, descends_from

    async with factory() as monitor:
        try:
            async with asyncio.timeout(10):
                while True:
                    if task.done():
                        pytest.fail(f"the waiter ended before it was seen waiting on {on}: {_outcome(task)}")
                    mine = [pid for pid, owner in BACKENDS.items() if descends_from(owner, task)]
                    locks = (await monitor.execute(_UNGRANTED, {"pids": mine})).all() if mine else []
                    await monitor.rollback()
                    for lock in locks:
                        if not _matches(lock, on, queue_id):
                            continue
                        blockers = list(lock.blockers or [])
                        if not blockers:
                            continue
                        owners = [BACKENDS.get(pid) for pid in blockers]
                        if any(owner is None for owner in owners):
                            pytest.fail(f"pid {lock.pid} of the waiter waits behind a foreign backend: {blockers}")
                        if all(owner is BACKENDS.get(lock.pid) for owner in owners):
                            continue  # behind another transaction of the very same task: no pair edge
                        if behind is not None and not any(descends_from(owner, behind) for owner in owners):
                            continue  # not (yet) behind the named holder; a wait that stays so times out below
                        return lock.pid, blockers
                    await asyncio.sleep(0.02)
        except TimeoutError:
            rows = (
                await monitor.execute(
                    text(
                        "SELECT l.pid, l.locktype, l.classid, l.objid, l.granted, pg_blocking_pids(l.pid) "
                        "FROM pg_locks l WHERE l.pid = ANY(:pids) AND l.locktype IN ('advisory','transactionid','tuple')"
                    ),
                    {"pids": list(BACKENDS)},
                )
            ).all()
            named = [(*row, getattr(BACKENDS.get(row[0]), "get_name", lambda: "?")()) for row in rows]
            pytest.fail(
                f"no transaction of the waiter waited on {on} (lane {queue_id}); outcome={_outcome(task)}; "
                f"its pids={[pid for pid, owner in BACKENDS.items() if descends_from(owner, task)]}; "
                f"locks of this test: {named}"
            )


async def a_waiter_appears(engine, factory, task, queue_id=None, *, on=LANE_WAIT, behind=None) -> None:
    """The second writer of a pair is waiting -- on the lock it is meant to wait on, behind this test's holder.

    PostgreSQL: :func:`postgres_edge` -- the waiter's own backend, its exact blocking edge and the lock's type (the
    lane guard's key for a lane wait). SQLite: a task waits on the lane's process lock (when a lane is named), else
    on SQLite's writer -- which only the task's still being unfinished can show."""
    if engine.dialect.name == "postgresql":
        await postgres_edge(factory, task, queue_id, on=on, behind=behind)
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
