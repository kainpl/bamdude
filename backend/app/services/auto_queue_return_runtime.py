"""Own return preparation and SQL through actual completion, even after caller cancellation."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from time import monotonic

from sqlalchemy import and_, or_, select

logger = logging.getLogger(__name__)

# These invalidate this immutable attempt, rather than deferring its admission.
# Keep the receipt for history/diagnostics, but never schedule it again.
FINAL_REFUSAL_CODES = frozenset(
    {
        "return_limit",
        "attempt_changed",
        "execution_changed",
        "history_changed",
        "settlement_changed",
        "settlement_missing",
        "source_or_policy_changed",
        "order_changed",
    }
)


class AutoQueueReturnRuntime:
    def __init__(self):
        self.admitting = False
        self.tasks = set()
        self.pass_task = None
        self.cursor = None

    def open_admission(self):
        if not self.admitting and self.tasks:
            return False
        self.admitting = True
        return True

    def close_admission(self):
        self.admitting = False

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    def _admit(self, factory):
        from backend.app.services.farm_startup_guard import farm_startup_guard

        if not self.admitting or not farm_startup_guard.allowed():
            return None
        task = asyncio.create_task(factory())
        self.tasks.add(task)
        task.add_done_callback(self._done)
        return task

    async def run(self, factory):
        task = self._admit(factory)
        if task is None:
            return None
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

    async def pass_ready(self):
        """One bounded keyset pass before the router opens its ordinary session."""
        task = self.pass_task
        if task is None or task.done():
            task = self._admit(self._pass_ready)
            if task is None:
                return
            self.pass_task = task
        return await asyncio.shield(task)

    async def _diagnose(self, receipt_id, code, epoch):
        from backend.app.core import database
        from backend.app.models.auto_queue_return import AutoQueueReturn
        from backend.app.services import queue_sources

        if code == "disabled" or epoch != queue_sources.current_epoch():
            return
        # An unchanged episode is a read, not another SQLite writer reservation.
        # The transaction below repeats the check after acquiring its guard.
        async with database.async_session() as db:
            row = await db.get(AutoQueueReturn, receipt_id)
            if (
                row is None
                or row.returned_at is not None
                or row.last_refusal_code == code
                or row.last_refusal_code in FINAL_REFUSAL_CODES
            ):
                return
        async with queue_sources.storage_mutation(), database.async_session() as db:
            if epoch != queue_sources.current_epoch():
                return
            await database.take_write_lock(db, AutoQueueReturn.__table__, receipt_id)
            row = await db.get(AutoQueueReturn, receipt_id, with_for_update=True)
            if (
                row is None
                or row.returned_at is not None
                or row.last_refusal_code == code
                or row.last_refusal_code in FINAL_REFUSAL_CODES
            ):
                return
            row.last_refusal_code = code
            row.last_refusal_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await db.commit()
        logger.info("AutoQueue return deferred: %s", code)

    async def _pass_ready(self):
        from backend.app.core import database
        from backend.app.models.auto_queue_return import AutoQueueReturn
        from backend.app.models.settings import Settings
        from backend.app.services import queue_sources
        from backend.app.services.auto_queue_return import COOLDOWN_SECONDS, SETTING_KEY, ReturnRefused, return_ready

        epoch = queue_sources.current_epoch()
        deadline = monotonic() + 2
        async with database.async_session() as db:
            enabled = await db.scalar(select(Settings.value).where(Settings.key == SETTING_KEY)) == "true"
            if not enabled:
                self.cursor = None
                return
            query = select(AutoQueueReturn.id, AutoQueueReturn.ready_at, AutoQueueReturn.printer_id).where(
                AutoQueueReturn.returned_at.is_(None),
                or_(
                    AutoQueueReturn.last_refusal_code.is_(None),
                    AutoQueueReturn.last_refusal_code.notin_(FINAL_REFUSAL_CODES),
                ),
                AutoQueueReturn.completed_at
                <= datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=COOLDOWN_SECONDS),
            )
            if self.cursor is not None:
                stamp, identity = self.cursor
                query = query.where(
                    or_(
                        AutoQueueReturn.ready_at > stamp,
                        and_(AutoQueueReturn.ready_at == stamp, AutoQueueReturn.id > identity),
                    )
                )
            rows = (await db.execute(query.order_by(AutoQueueReturn.ready_at, AutoQueueReturn.id).limit(8))).all()
        if not rows:
            self.cursor = None
            return
        for receipt_id, stamp, printer_id in rows:
            if not self.admitting or queue_sources.current_epoch() != epoch:
                break
            try:
                await return_ready(receipt_id)
            except ReturnRefused as exc:
                await self._diagnose(receipt_id, exc.code, epoch)
            except Exception as exc:
                # Fixed code only; no raw SQL, paths or exception text in logs.
                await self._diagnose(receipt_id, "temporarily_unavailable", epoch)
                logger.debug("AutoQueue return failed: %s", type(exc).__name__)
            else:
                from backend.app.core.websocket import ws_manager

                try:
                    await ws_manager.send_queue_changed(printer_id)
                except Exception as exc:
                    logger.info("AutoQueue return delivery failed: %s", type(exc).__name__)
            self.cursor = (stamp, receipt_id)
            if monotonic() >= deadline:
                break


auto_queue_return_runtime = AutoQueueReturnRuntime()
