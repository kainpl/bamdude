"""Two real sessions meet on a lane or on rows (G03 B): the helpers every pair scenario shares.

PostgreSQL (``DISPATCH_TEST_POSTGRES_URL``): the process lane lock is bypassed so the database guard is what a
second session waits on, and the waiter is observed by its own backend's pid -- its ungranted lock in ``pg_locks``
and the holder ``pg_blocking_pids`` names (:func:`postgres_edge`). A file-backed SQLite: it waits on the lane's
process lock or on SQLite's writer.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from sqlalchemy import select, text, update

from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_ops
from backend.app.services.printer_queues import ensure_printer_queue
from backend.tests.integration.queue_test_database import BACKENDS, HELD, descends_from


class NoProcessLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


LANE_WAIT = ("advisory",)
ROW_WAIT = ("transactionid", "tuple")
ANY_WAIT = ("transactionid", "tuple", "advisory")
LANE_GUARD = 1625  # pg_advisory_xact_lock(1625, queue_id): the lane's database guard (queue_ops)
ARCHIVE_GUARD = 182  # pg_advisory_xact_lock(182, archive_id): the archive's guard (archive_write_scope)

_UNGRANTED = text(
    "SELECT pid, locktype, classid, objid, objsubid, pg_blocking_pids(pid) AS blockers "
    "FROM pg_locks WHERE NOT granted AND pid = ANY(:pids)"
)


@dataclass(frozen=True)
class Edge:
    """What the monitor saw: the waiter's backend, the lock it waits on, and the backends ahead of it."""

    pid: int
    locktype: str
    classid: int | None
    objid: int | None
    blockers: tuple[int, ...]


def _outcome(task) -> str | None:
    if not task.done():
        return None
    if task.cancelled():
        return "cancelled"
    return repr(task.exception()) if task.exception() else repr(task.result())


def _matches(lock, on, queue_id, archive_id) -> bool:
    """The lock is of a named kind; an advisory lock counts only as the lane guard of ``queue_id`` or the archive
    guard of ``archive_id`` -- in every mode, so another advisory lock (another class, another key) never does."""
    if lock.locktype not in on:
        return False
    if lock.locktype != "advisory":
        return True
    if lock.objsubid != 2:
        return False
    if lock.classid == LANE_GUARD:
        return queue_id is not None and lock.objid == queue_id
    if lock.classid == ARCHIVE_GUARD:
        return archive_id is not None and lock.objid == archive_id
    return False


def _named_holder(backend, behind) -> bool:
    if isinstance(behind, int):
        return backend.pid == behind
    if isinstance(behind, (set, frozenset, list, tuple)):
        return backend.pid in behind
    return backend.task is behind


async def postgres_edge(
    factory, task, queue_id=None, *, on=LANE_WAIT, behind=None, archive_id=None, timeout: float = 10
) -> Edge:
    """PostgreSQL: wait until a transaction of ``task`` waits on the named lock behind the named holder, and return
    the :class:`Edge` -- ``pg_blocking_pids`` of that very backend.

    The waiter is known by pid: ``queue_test_database`` names each transaction's backend after the task that began
    it, and each task after the task that created it, so a transaction of ``task`` is one begun by it or by a task it
    created (a route the HTTP client runs is in a task of the middleware's). The holder is pinned to the current
    transaction at the sole active barrier, or the transactions of the named pid(s) or exact task. An observed
    barrier must still be active. A foreign backend or a waiter that ended before its wait was seen is a failure.
    An advisory wait counts only on the lane guard's key
    (``queue_id``) or the archive guard's (``archive_id``). ``pg_locks`` and ``pg_blocking_pids`` are read live, the
    monitor's transaction ended on every poll (``pg_stat_activity`` would answer from a snapshot of its first read)."""
    barriers = {backend for backend in HELD if BACKENDS.get(backend.pid) is backend}
    if behind is None:
        assert len(barriers) == 1, "expected one active PostgreSQL barrier or an explicitly named holder"
        holders = barriers
    else:
        holders = {backend for backend in BACKENDS.values() if _named_holder(backend, behind)}
        # If the named holder is stopped at a barrier, its other transactions are outside that barrier.
        holders = (holders & barriers) or holders

    async with factory() as monitor:
        try:
            async with asyncio.timeout(timeout):
                while True:
                    if task.done():
                        pytest.fail(f"the waiter ended before it was seen waiting on {on}: {_outcome(task)}")
                    mine = {pid: backend for pid, backend in BACKENDS.items() if descends_from(backend.task, task)}
                    locks = (await monitor.execute(_UNGRANTED, {"pids": list(mine)})).all() if mine else []
                    await monitor.rollback()
                    for lock in locks:
                        if BACKENDS.get(lock.pid) is not mine[lock.pid] or not _matches(lock, on, queue_id, archive_id):
                            continue
                        blockers = list(lock.blockers or [])
                        backends = [BACKENDS.get(pid) for pid in blockers]
                        if any(backend is None for backend in backends):
                            pytest.fail(f"pid {lock.pid} of the waiter waits behind a foreign backend: {blockers}")
                        if any(
                            backend in holders and (backend not in barriers or backend in HELD) for backend in backends
                        ):
                            return Edge(lock.pid, lock.locktype, lock.classid, lock.objid, tuple(blockers))
                    await asyncio.sleep(0.02)
        except TimeoutError:
            # A query cut short leaves the connection unusable: drop it rather than roll it back.
            await monitor.invalidate()
    # Diagnose in a session of its own: the timeout may have cut the monitor's query short.
    async with factory() as monitor:
        rows = (
            await monitor.execute(
                text(
                    "SELECT l.pid, l.locktype, l.classid, l.objid, l.granted, pg_blocking_pids(l.pid) "
                    "FROM pg_locks l WHERE l.pid = ANY(:pids) AND l.locktype IN ('advisory','transactionid','tuple')"
                ),
                {"pids": list(BACKENDS)},
            )
        ).all()
        named = [(*row, BACKENDS[row[0]].task.get_name() if row[0] in BACKENDS else "?") for row in rows]
        pytest.fail(
            f"no transaction of the waiter waited on {on} (lane {queue_id}, archive {archive_id}) behind "
            f"{behind!r}; outcome={_outcome(task)}; "
            f"its pids={[pid for pid, backend in BACKENDS.items() if descends_from(backend.task, task)]}; "
            f"locks of this test: {named}"
        )


async def a_waiter_appears(
    engine, factory, task, queue_id=None, *, on=LANE_WAIT, behind=None, archive_id=None, lane=None
):
    """The second writer of a pair is waiting -- on the lock it is meant to wait on, behind this test's holder.

    PostgreSQL: :func:`postgres_edge` -- the waiter's own backend, its exact edge to the holder and the lock's type
    and key; ``lane`` names the lane guard's key there when ``queue_id`` is not passed (on SQLite ``queue_id`` means
    "waits on that lane's process lock"). Returns the :class:`Edge`. SQLite: a task waits on the lane's process lock
    (when a lane is named), else on SQLite's writer -- which only the task's still being unfinished can show."""
    edge = None
    if engine.dialect.name == "postgresql":
        key = queue_id if queue_id is not None else lane
        edge = await postgres_edge(factory, task, key, on=on, behind=behind, archive_id=archive_id)
    elif queue_id is not None:
        lock = queue_ops._queue_claim_locks[queue_id]
        async with asyncio.timeout(10):
            while not getattr(lock, "_waiters", None):
                await asyncio.sleep(0.02)
    else:
        await asyncio.sleep(0.3)
    assert not task.done(), "the second writer did not wait"
    return edge


async def hold_until(reached, release) -> None:
    """Register the current transaction for the lifetime of the barrier. SQLite has no backend to register."""
    task = asyncio.current_task()
    active = [backend for backend in BACKENDS.values() if backend.task is task]
    assert len(active) <= 1, "the barrier must identify one PostgreSQL transaction"
    HELD.update(active)
    try:
        reached.set()
        await asyncio.wait_for(release.wait(), 20)
    finally:
        HELD.difference_update(active)


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
        await hold_until(reached, release)
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
            await hold_until(reached, release)
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
                await hold_until(reached, release)
            yield value

    monkeypatch.setattr(owner, name, held)
    return reached, release
