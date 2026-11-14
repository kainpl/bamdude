"""Own return preparation and SQL through actual completion, even after caller cancellation."""

import asyncio
import logging

logger = logging.getLogger(__name__)


class AutoQueueReturnRuntime:
    def __init__(self):
        self.admitting = False
        self.tasks = set()

    def open_admission(self):
        self.admitting = True

    def close_admission(self):
        self.admitting = False

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def run(self, factory):
        if not self.admitting:
            return None
        task = asyncio.create_task(factory())
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return await asyncio.shield(task)

    async def quiesce_for_restore(self, timeout=10):
        self.close_admission()
        if not self.tasks:
            return True
        _, pending = await asyncio.wait(tuple(self.tasks), timeout=timeout)
        return not pending

    async def publish(self, candidate, *, manager):
        from backend.app.services.auto_queue_return import ReturnRefused, publish_ready

        async def work():
            try:
                return await publish_ready(candidate, manager=manager)
            except ReturnRefused as exc:
                logger.info("AutoQueue settlement not published: %s", exc.code)
            except Exception as exc:
                # Never let optional return proof undo an already committed
                # physical completion or disclose SQL/source/credential text.
                logger.warning("AutoQueue settlement publication failed: %s", type(exc).__name__)
            return None

        return await self.run(work)


auto_queue_return_runtime = AutoQueueReturnRuntime()
