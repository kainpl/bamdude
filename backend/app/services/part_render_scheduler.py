"""The part-render scheduler (spec §4, §9; plan E3, task 23).

One asyncio loop and ONE attempt in flight. The loop takes a task, hands it to the runtime and settles
the outcome through the writer. It takes the next task only once ``runtime.render`` has returned, and
render returns only when the attempt's tree is proven gone (task 18). A retry therefore never starts
beside the attempt it retries. A generation fences every write. stop() closes admission, waits for the
attempt in flight (its cancel, its proof, a publication already under way), then changes the
generation, so a late result of the old one writes nothing (spec §9.6).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from uuid import uuid4

from backend.app.core.config import settings
from backend.app.services import part_renders
from backend.app.services.part_render_protocol import PLATE_SECONDS, TRANSIENT
from backend.app.services.part_render_runtime import (
    get_part_render_runtime,
    start_part_render_runtime,
    stop_part_render_runtime,
)
from backend.app.services.part_render_types import AttemptResult, Mode, RenderTask, RuntimeUnavailable
from backend.app.services.preview_artifacts import owned

logger = logging.getLogger(__name__)

_IDLE_SECONDS = 5
_GC_INTERVAL_SECONDS = 3600


class PartRenderScheduler:
    def __init__(self, runtime, session_factory, root: Path):
        self.runtime = runtime
        self.session_factory = session_factory
        self.root = root
        self.generation = uuid4().hex
        self.admission = False
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()
        self.last_gc = float("-inf")
        self.stopping: asyncio.Task | None = None

    async def start(self) -> None:
        await part_renders.reconcile(self.session_factory, self.root)  # spec §8.4: before admission
        if self.runtime.refusal("render") is None:
            requeued = await part_renders.requeue_no_runtime(self.session_factory)
            if requeued:
                logger.info("Part render requeued=%d plates rendered by the fallback alone", requeued)
        queued = await part_renders.backfill(self.session_factory)
        logger.info("Part render backfill queued=%d", queued)
        part_renders.set_live_generation(self.generation)
        self.admission = True
        self.task = asyncio.create_task(self._loop(), name="part-render-scheduler")

    def poke(self) -> None:
        self.wake.set()

    async def stop(self, reason: str) -> bool:
        """Spec §9.6: True when the attempt in flight is proven over. False when ownership is uncertain -- a
        restore must then not replace the database (consilium E3-R4). Admission stays closed either way.

        ONE stop per scheduler. Every caller -- a second restore at the same time, the lifespan after a
        restore -- awaits that stop and gets its answer: a stop that has begun is not one that has ended
        (consilium E3.2-R2). Shielded, so a caller that goes away does not cut the stop in half. A runtime
        that has become uncertain since the stop ended turns a later True into False, never the reverse."""
        if self.stopping is None:
            self.stopping = asyncio.create_task(self._stop(reason), name="part-render-scheduler-stop")
        proven = await asyncio.shield(self.stopping)
        return proven and not self.runtime.uncertain

    async def _stop(self, reason: str) -> bool:
        self.admission = False  # 1. no new task is taken
        if self.task is not None:
            self.task.cancel()  # 2. the attempt in flight: its cancel and proof; a publication finishes (owned)
            await asyncio.gather(self.task, return_exceptions=True)
        old, self.generation = self.generation, uuid4().hex  # 3. a late result of the old one writes nothing
        part_renders.set_live_generation(None)  # 4. stopped until the process restarts
        proven = not self.runtime.uncertain
        logger.log(
            logging.INFO if proven else logging.ERROR,
            "Part render scheduler stopped reason=%s proven=%s generation=%s->%s",
            reason,
            proven,
            old[:8],
            self.generation[:8],
        )
        return proven

    async def _loop(self) -> None:
        while self.admission:
            try:
                worked = await self._step()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Part render scheduler step failed")
                worked = False
            if not worked:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=_IDLE_SECONDS)
                except TimeoutError:
                    pass

    async def _step(self) -> bool:
        now = part_renders.utcnow()
        async with self.session_factory() as db:
            task = await part_renders.next_task(db, now)
            self.runtime.queue = await part_renders.queue_counts(db)
        if task is None:
            await self._maybe_gc(now)
            return False
        generation = self.generation
        if task.phase == "render" and self.runtime.unsupported:
            # spec §5.3: no official Node here -- the fallback answers, and no attempt is counted
            await part_renders.enter_fallback(self.session_factory, task, "no_runtime", generation, now)
            return True
        mode: Mode = "fallback" if task.phase == "fallback" else "render"
        try:
            result = await self.runtime.render(task, mode=mode, deadline_ns=time.monotonic_ns() + PLATE_SECONDS * 10**9)
        except RuntimeUnavailable:
            return False  # nothing was attempted: the row stays as it is, nothing is counted (spec §5.3, §13)
        try:
            await owned(self._settle(task, mode, result, generation))
        finally:
            await owned(self.runtime.discard(result))
        return True

    async def _settle(self, task: RenderTask, mode: Mode, result: AttemptResult, generation: str) -> None:
        now = part_renders.utcnow()
        factory = self.session_factory
        if result.outcome == "canceled":
            return
        if result.outcome == "done":
            child = result.result or {}
            if child.get("outcome") == "ok" and result.files is not None:
                reason = task.reason if mode == "fallback" else child.get("reason")
                try:
                    published = await part_renders.publish(factory, self.root, task, result, generation, reason=reason)
                except part_renders.InvalidResult as exc:
                    logger.warning("Part render render_id=%s refused its result: %s", task.render_id, exc)
                    await self._failed(task, mode, "invalid_output", generation, now)
                    return
                if published != "published":
                    logger.warning("Part render render_id=%s publication %s", task.render_id, published)
                return
            if child.get("outcome") == "unavailable":
                await part_renders.mark_terminal(factory, task, "unavailable", child["reason"], generation, now)
                return
            reason = child.get("reason")
            if reason == "bundle_mismatch":
                logger.error("Part render bundle_mismatch in the child: nothing counted")  # the runtime's fault
                return
            if reason == "source_changed":
                await part_renders.mark_terminal(factory, task, "failed", "source_changed", generation, now)
                return
            await self._failed(task, mode, reason if reason in TRANSIENT else "crashed", generation, now)
            return
        if result.outcome == "memory_limit":
            if mode == "render":
                await part_renders.enter_fallback(factory, task, "memory_limit", generation, now)
            else:
                await part_renders.mark_terminal(factory, task, "failed", "memory_limit", generation, now)
            return
        await self._failed(task, mode, result.outcome, generation, now)  # timeout / crashed / invalid_output

    async def _failed(self, task: RenderTask, mode: Mode, reason: str, generation: str, now) -> None:
        if mode == "fallback":
            await part_renders.mark_terminal(self.session_factory, task, "failed", reason, generation, now)
        else:
            await part_renders.record_attempt_failure(self.session_factory, task, reason, generation, now)

    async def _maybe_gc(self, now) -> None:
        if time.monotonic() - self.last_gc < _GC_INTERVAL_SECONDS:
            return
        self.last_gc = time.monotonic()
        removed = await part_renders.gc(self.session_factory, self.root, now)
        logger.info("Part render gc removed=%d", removed)


scheduler: PartRenderScheduler | None = None


async def start_part_render(base: Path, app_dir: Path) -> None:
    global scheduler
    from backend.app.core import database

    runtime = await start_part_render_runtime(base, app_dir)
    candidate = PartRenderScheduler(runtime, database.async_session, settings.part_renders_dir)
    try:
        await candidate.start()
    except Exception:
        logger.exception("Part render scheduler did not start")
        return
    scheduler = candidate


async def stop_part_render_scheduler(reason: str) -> bool:
    """True when nothing of the queue may still run. A scheduler that never started has nothing in flight,
    but its runtime may still be uncertain from its start."""
    if scheduler is None:
        current = get_part_render_runtime()
        return current is None or not current.uncertain
    return await scheduler.stop(reason)


async def stop_part_render() -> None:
    """Lifespan shutdown, fail-soft: the scheduler first (its attempt ends with its proof, or the stop logs that
    it could not), the runtime after it."""
    await stop_part_render_scheduler("shutdown")
    await stop_part_render_runtime()


def poke() -> None:
    if scheduler is not None:
        scheduler.poke()
