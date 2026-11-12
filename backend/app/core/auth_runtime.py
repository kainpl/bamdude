"""Own authentication work through actual SQL/hash completion during restore."""

import asyncio
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from typing import Any

from fastapi import HTTPException
from sqlalchemy import event


class AuthRuntime:
    def __init__(self):
        # ASGI unit clients omit lifespan; real startup closes before bootstrap.
        self.admitting = True
        self._restore_drained = False
        self._tasks: set[asyncio.Task] = set()
        self._operations: set[asyncio.Task] = set()
        self._transactions: set[asyncio.Event] = set()

    def ensure_admitting(self):
        if not self.admitting:
            raise HTTPException(503, "Authentication temporarily unavailable", headers={"Retry-After": "5"})

    def open_admission(self):
        self._restore_drained = False
        self.admitting = True

    def close_admission(self):
        self.admitting = False
        self._restore_drained = False

    def ensure_restore_drained(self):
        if self.admitting or not self._restore_drained or self._tasks or self._operations or self._transactions:
            raise HTTPException(503, "Authentication restore drain is not proven", headers={"Retry-After": "5"})

    def spawn(self, work: Coroutine[Any, Any, Any]) -> asyncio.Task:
        try:
            self.ensure_admitting()
        except BaseException:
            work.close()
            raise
        task = asyncio.create_task(work)
        self._tasks.add(task)

        def done(completed):
            self._tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(done)
        return task

    @asynccontextmanager
    async def operation(self):
        self.ensure_admitting()
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("Authentication operation has no task owner")
        self._operations.add(owner)
        try:
            yield
        finally:
            self._operations.discard(owner)

    def track_transaction(self, db):
        session = db.sync_session
        existing = session.info.get("auth_runtime_transaction")
        if existing is not None and not existing.is_set():
            return
        self.ensure_admitting()
        released = asyncio.Event()
        self._transactions.add(released)
        session.info["auth_runtime_transaction"] = released
        if session.get_transaction() is None:
            session.begin()

        if not session.info.get("auth_runtime_listener"):

            def ended(_session, transaction):
                if transaction.parent is None:
                    owned = _session.info.pop("auth_runtime_transaction", None)
                    if owned is not None:
                        owned.set()
                        self._transactions.discard(owned)

            event.listen(session, "after_transaction_end", ended)
            session.info["auth_runtime_listener"] = True

    async def quiesce_for_restore(self, timeout: float = 30) -> bool:
        self.close_admission()
        # Waiters never own the underlying work; cancellation leaves it tracked.
        tasks = self._tasks | self._operations
        waits = [asyncio.create_task(released.wait()) for released in self._transactions]
        try:
            if tasks or waits:
                _, pending = await asyncio.wait(tasks | set(waits), timeout=timeout)
                if pending:
                    return False
            self._restore_drained = not self._tasks and not self._operations and not self._transactions
            return self._restore_drained
        finally:
            for waiter in waits:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def shutdown(self, timeout: float = 30) -> bool:
        return await self.quiesce_for_restore(timeout)


auth_runtime = AuthRuntime()
