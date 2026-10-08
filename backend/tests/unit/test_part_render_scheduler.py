"""PartRenderScheduler: one attempt in flight, every outcome settled, a stop that waits (plan E3, task 23)."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.plate_render import PlateRender
from backend.app.services import part_render_scheduler as prs, part_renders
from backend.app.services.part_render_types import AttemptResult, RuntimeUnavailable
from backend.tests.unit.test_part_renders_publish import attempt_files
from backend.tests.unit.test_part_renders_queue import linked


class FakeRuntime:
    """runtime.render's contract: it returns only when the attempt is over, and a cancel waits for the proof."""

    def __init__(self):
        self.calls: list[tuple[int, str]] = []
        self.results: list = []
        self.gate: asyncio.Event | None = None
        self.in_flight = 0
        self.most_in_flight = 0
        self.cancel_done = asyncio.Event()
        self.unsupported = False
        self.refused: str | None = None
        self.queue: dict = {}
        self.uncertain = False  # the runtime's ownership_uncertain
        self.proof: asyncio.Event | None = None  # set: a cancel waits for the test to release its proof
        self.refresh_requested = False

    def refresh_node_soon(self):
        self.refresh_requested = True

    def refusal(self, mode):
        return self.refused

    async def render(self, task, *, mode, deadline_ns):
        if self.refused:
            raise RuntimeUnavailable(self.refused)
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        self.calls.append((task.render_id, mode))
        try:
            if self.gate is not None:
                await self.gate.wait()
            return self.results.pop(0)(task)
        except asyncio.CancelledError:
            if self.proof is not None:
                await self.proof.wait()
            else:
                await asyncio.sleep(0.2)  # the worker's proof takes its time
            self.cancel_done.set()
            raise
        finally:
            self.in_flight -= 1

    async def discard(self, result):
        pass


class ParkedWake(asyncio.Event):
    """The scheduler's wake, telling the test when the loop is parked on it -- between steps, no query open."""

    def __init__(self):
        super().__init__()
        self.parked = asyncio.Event()

    async def wait(self):
        self.parked.set()
        try:
            return await super().wait()
        finally:
            self.parked.clear()


def make_scheduler(runtime, factory, root):
    scheduler = prs.PartRenderScheduler(runtime, factory, root)
    scheduler.wake = ParkedWake()
    return scheduler


async def stop_parked(scheduler) -> bool:
    """Stop a scheduler whose work is done, once its loop is parked. Every test session shares ONE connection
    (StaticPool), and a cancel that lands inside a query invalidates it for all of them -- db_session's
    teardown then finds it closed. Production's pool only replaces the one connection."""
    await asyncio.wait_for(scheduler.wake.parked.wait(), 10)
    return await scheduler.stop("shutdown")


def outcome(name: str, child: dict | None = None):
    return lambda task: AttemptResult(name, "c" * 32, 5, child)


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def queued(db_session):
    first = await linked(db_session, sha="a" * 64)
    second = await linked(db_session, sha="b" * 64)
    await part_renders.ensure_for_files(db_session, [first.id, second.id])
    await db_session.commit()


async def run_until(scheduler, predicate, seconds: float = 10):
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("never happened")


async def row(db_session, sha: str) -> PlateRender:
    db_session.expire_all()
    from sqlalchemy import select

    return (await db_session.execute(select(PlateRender).where(PlateRender.file_sha256 == sha))).scalar_one()


@pytest.mark.usefixtures("queued")
async def test_one_attempt_at_a_time_and_the_next_only_after_the_first_returned(factory, tmp_path):
    runtime = FakeRuntime()
    runtime.gate = asyncio.Event()
    runtime.results = [outcome("timeout"), outcome("timeout")]
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    try:
        await run_until(scheduler, lambda: runtime.calls)
        await asyncio.sleep(0.3)
        assert len(runtime.calls) == 1  # the second plate waits for the first attempt to return
        runtime.gate.set()
        await run_until(scheduler, lambda: len(runtime.calls) == 2)
        assert runtime.most_in_flight == 1
    finally:
        await stop_parked(scheduler)


@pytest.mark.usefixtures("queued")
async def test_a_transient_failure_is_counted_and_waits_for_its_retry(factory, db_session, tmp_path):
    runtime = FakeRuntime()
    runtime.results = [outcome("crashed"), outcome("crashed")]
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    try:
        await run_until(scheduler, lambda: len(runtime.calls) == 2)
        await asyncio.sleep(0.3)
        assert len(runtime.calls) == 2  # both back off; neither is retried at once
        assert (await row(db_session, "a" * 64)).attempts == 1
    finally:
        await stop_parked(scheduler)


@pytest.mark.usefixtures("queued")
async def test_memory_limit_moves_to_the_fallback_and_the_fallback_runs_without_node(factory, db_session, tmp_path):
    runtime = FakeRuntime()
    runtime.results = [outcome("memory_limit"), outcome("timeout"), outcome("crashed")]
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    try:
        await run_until(scheduler, lambda: len(runtime.calls) == 3)
        # plate "a": render -> memory_limit -> its fallback at once (same id, same age, before "b")
        assert [mode for _, mode in runtime.calls] == ["render", "fallback", "render"]
        assert runtime.calls[0][0] == runtime.calls[1][0]
        first = await row(db_session, "a" * 64)
        assert (first.status, first.reason) == ("failed", "timeout")  # a failure in the fallback is final
    finally:
        await stop_parked(scheduler)


async def test_an_unsupported_platform_sends_the_row_to_the_fallback_without_an_attempt(factory, db_session, tmp_path):
    file = await linked(db_session)
    await part_renders.ensure_for_files(db_session, [file.id])
    await db_session.commit()
    runtime = FakeRuntime()
    runtime.unsupported = True
    runtime.results = [outcome("crashed")]
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    try:
        await run_until(scheduler, lambda: runtime.calls)
        assert runtime.calls == [(runtime.calls[0][0], "fallback")]
        assert (await row(db_session, "a" * 64)).attempts == 0
    finally:
        await stop_parked(scheduler)


@pytest.mark.usefixtures("queued")
async def test_a_runtime_that_refuses_counts_nothing(factory, db_session, tmp_path):
    runtime = FakeRuntime()
    runtime.refused = "runtime_missing"
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    try:
        await asyncio.sleep(0.5)
        stored = await row(db_session, "a" * 64)
        assert (stored.status, stored.attempts) == ("pending", 0)
    finally:
        await stop_parked(scheduler)


@pytest.mark.usefixtures("queued")
async def test_a_file_property_is_a_terminal_mark(factory, db_session, tmp_path):
    runtime = FakeRuntime()
    runtime.results = [
        outcome("done", {"outcome": "unavailable", "reason": "no_gcode"}),
        outcome("done", {"outcome": "failed", "reason": "source_changed"}),
    ]
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    try:
        await run_until(scheduler, lambda: len(runtime.calls) == 2)
        await asyncio.sleep(0.3)
        rows = {(await row(db_session, sha)).status for sha in ("a" * 64, "b" * 64)}
        assert rows == {"unavailable", "failed"}
    finally:
        await stop_parked(scheduler)


@pytest.mark.usefixtures("queued")
async def test_a_stop_returns_only_after_the_attempts_cancel_and_fences_the_old_generation(factory, tmp_path):
    runtime = FakeRuntime()
    runtime.gate = asyncio.Event()  # never opened: the attempt hangs
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    await run_until(scheduler, lambda: runtime.calls)
    old = scheduler.generation
    assert await scheduler.stop("restore") is True
    assert runtime.cancel_done.is_set()  # the cancel and its proof finished before stop returned
    assert scheduler.generation != old
    assert part_renders._live_generation is None
    await asyncio.sleep(0.3)
    assert len(runtime.calls) == 1  # stopped for good: a restore needs a process restart


@pytest.mark.usefixtures("queued")
async def test_a_stop_reports_an_attempt_whose_end_is_not_proven(factory, tmp_path):
    """Consilium E3-R4: the cancel's retire could not prove the tree gone -- the stop says so."""
    runtime = FakeRuntime()
    runtime.gate = asyncio.Event()
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    await run_until(scheduler, lambda: runtime.calls)
    runtime.uncertain = True  # what a failed proof leaves behind
    assert await scheduler.stop("restore") is False
    assert part_renders._live_generation is None  # admission stays closed
    assert await scheduler.stop("restore") is False  # and asking again does not change the answer


@pytest.mark.usefixtures("queued")
@pytest.mark.parametrize("proven", [True, False])
async def test_a_second_stop_waits_for_the_first_and_gets_its_answer(factory, tmp_path, proven):
    """Consilium E3.2-R2: two restores at once. Neither may replace the database before the one stop has its
    proof, and both get the answer that proof gave."""
    runtime = FakeRuntime()
    runtime.gate = asyncio.Event()  # the attempt hangs
    runtime.proof = asyncio.Event()  # and its cancel waits for a proof the test releases
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    await run_until(scheduler, lambda: runtime.calls)
    first = asyncio.create_task(scheduler.stop("restore"))
    await asyncio.sleep(0.1)
    second = asyncio.create_task(scheduler.stop("restore"))
    await asyncio.sleep(0.3)
    assert not first.done() and not second.done()  # started is not finished
    assert runtime.in_flight == 1
    runtime.uncertain = not proven  # what the proof found
    runtime.proof.set()
    assert (await first, await second) == (proven, proven)
    assert runtime.cancel_done.is_set()
    assert await scheduler.stop("shutdown") == proven  # after the end: the answer that was proven


async def test_a_restore_during_publication_lets_it_finish_and_refuses_the_old_generation_after(
    factory, db_session, tmp_path, monkeypatch
):
    file = await linked(db_session)
    await part_renders.ensure_for_files(db_session, [file.id])
    await db_session.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    real = part_renders.publish

    async def slow_publish(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(part_renders, "publish", slow_publish)
    runtime = FakeRuntime()
    staged = attempt_files(tmp_path / "files")
    runtime.results = [lambda task: staged]
    scheduler = make_scheduler(runtime, factory, tmp_path / "part-renders")
    await scheduler.start()
    await asyncio.wait_for(entered.wait(), 10)
    old = scheduler.generation
    stopping = asyncio.create_task(scheduler.stop("restore"))
    await asyncio.sleep(0.3)
    assert not stopping.done()  # a publication already under way is waited for (spec §9.6 step 2)
    release.set()
    await stopping
    assert (await row(db_session, "a" * 64)).status == "ready"  # it committed into the database it began in
    task = await part_renders.next_task(db_session, part_renders.utcnow() + timedelta(days=1))
    assert task is None
    assert await real(factory, tmp_path / "x", (await _any_task(db_session)), staged, old, reason=None) == "stale"


async def _any_task(db_session):
    from backend.app.services.part_render_types import RenderTask, SourceRef

    stored = await row(db_session, "a" * 64)
    return RenderTask(
        stored.id,
        stored.file_sha256,
        stored.plate_index,
        stored.renderer_version,
        "render",
        None,
        0,
        1,
        True,
        SourceRef(1, "/x", "/", "3mf", 1),
    )


async def test_start_reconciles_before_it_admits_and_backfills(factory, tmp_path, monkeypatch):
    order: list[str] = []

    async def reconcile(sf, root):
        order.append("reconcile")
        assert part_renders._live_generation is None  # no write may be admitted yet
        return part_renders.ReconcileReport()

    async def backfill(sf):
        order.append("backfill")
        return 0

    monkeypatch.setattr(part_renders, "reconcile", reconcile)
    monkeypatch.setattr(part_renders, "backfill", backfill)
    scheduler = make_scheduler(FakeRuntime(), factory, tmp_path)
    await scheduler.start()
    try:
        assert order == ["reconcile", "backfill"]
        assert part_renders._live_generation == scheduler.generation
    finally:
        await stop_parked(scheduler)


async def test_gc_runs_only_when_no_attempt_is_alive(factory, db_session, tmp_path, monkeypatch):
    file = await linked(db_session)
    await part_renders.ensure_for_files(db_session, [file.id])
    await db_session.commit()
    runtime = FakeRuntime()
    runtime.gate = asyncio.Event()
    runtime.results = [outcome("crashed")]
    collected: list[int] = []

    async def gc(sf, root, now):
        collected.append(runtime.in_flight)
        return 0

    monkeypatch.setattr(part_renders, "gc", gc)
    monkeypatch.setattr(prs, "_GC_INTERVAL_SECONDS", 0)
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    try:
        await run_until(scheduler, lambda: runtime.calls)
        runtime.gate.set()
        await run_until(scheduler, lambda: collected)
        assert set(collected) == {0}
    finally:
        await stop_parked(scheduler)


def ok_with_files(root):
    return lambda task: AttemptResult("done", "c" * 32, 5, {"outcome": "ok", "reason": None}, root)


@pytest.mark.usefixtures("queued")
@pytest.mark.parametrize("published", ["unknown", "failed", "raises"])
async def test_a_publication_that_did_not_land_waits_instead_of_rendering_again(
    factory, db_session, tmp_path, monkeypatch, published
):
    """Final review C1(b) / I2: the row waits its pause, nothing is counted, and the queue moves on."""
    runtime = FakeRuntime()
    runtime.results = [ok_with_files(tmp_path) for _ in range(6)]

    async def publish(*args, **kwargs):
        if published == "raises":
            raise RuntimeError("an unforeseen failure in the writer")
        return published

    monkeypatch.setattr(prs.part_renders, "publish", publish)
    scheduler = make_scheduler(runtime, factory, tmp_path)
    now = part_renders.utcnow()
    await scheduler.start()
    try:
        await run_until(scheduler, lambda: len(runtime.calls) >= 2)
        await asyncio.sleep(0.5)
        assert len(runtime.calls) == 2  # each plate once, then both wait
        stored = await row(db_session, "a" * 64)
        assert (stored.status, stored.attempts) == ("pending", 0)
        assert stored.next_attempt_at > now
    finally:
        await stop_parked(scheduler)


@pytest.mark.usefixtures("queued")
async def test_a_bundle_mismatch_in_the_child_asks_the_runtime_to_look_again_and_waits(factory, db_session, tmp_path):
    """Final review M6: the runtime's fault counts nothing and never loops on one plate."""
    runtime = FakeRuntime()
    runtime.results = [outcome("done", {"outcome": "failed", "reason": "bundle_mismatch"}) for _ in range(6)]
    scheduler = make_scheduler(runtime, factory, tmp_path)
    await scheduler.start()
    try:
        await run_until(scheduler, lambda: len(runtime.calls) >= 2)
        await asyncio.sleep(0.5)
        assert len(runtime.calls) == 2 and runtime.refresh_requested
        assert (await row(db_session, "a" * 64)).attempts == 0
    finally:
        await stop_parked(scheduler)
