"""Integration tests for AutoQueueScheduler.

Exercises the full assign flow against an in-memory DB with mocked
printer_manager state — covers:

- Successful assignment when a printer is idle, model matches, and
  filament types are loaded.
- waiting_reason populated when no eligible printer is available.
- busy_printers honoured (printer marked printing in PrinterQueue).
- Batch fan-out: 4 items, 2 idle printers → 2 assigned, 2 wait.
- SJF + been_jumped guard marks longer pending peers.
- ``manual_start=True`` items skipped.
- ``scheduled_time`` in future skipped.

The full per-printer dispatch (FTP / MQTT) is NOT tested here — these
tests only verify the auto-queue → print_queue handoff.
"""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.settings import Settings
from backend.app.services.auto_queue_scheduler import AutoQueueScheduler


def _idle_status(filament_types: list[str], colors: list[str] | None = None) -> SimpleNamespace:
    """Build a printer status with the given AMS filaments loaded."""
    colors = colors or ["#FFFFFF"] * len(filament_types)
    trays = []
    for i, (t, c) in enumerate(zip(filament_types, colors, strict=False)):
        trays.append({"id": i, "tray_type": t, "tray_color": c, "tray_info_idx": ""})
    return SimpleNamespace(
        state="IDLE",
        raw_data={"ams": [{"id": 0, "tray": trays}], "vt_tray": [], "ams_extruder_map": {}},
    )


def _drying_status(filament_types: list[str], colors: list[str] | None = None) -> SimpleNamespace:
    """Like ``_idle_status`` but reports a non-IDLE state — what a printer shows
    while AMS auto-drying runs (so ``_is_printer_idle`` returns False). Trays stay
    loaded; drying doesn't remove filament."""
    s = _idle_status(filament_types, colors)
    s.state = "RUNNING"
    return s


async def _make_printer_with_queue(db_session, printer_factory, **kwargs):
    p = await printer_factory(**kwargs)
    pq = PrinterQueue(id=p.id, printer_id=p.id)
    db_session.add(pq)
    await db_session.commit()
    await db_session.refresh(pq)
    return p, pq


def _finished_status(filament_types: list[str], colors: list[str] | None = None) -> SimpleNamespace:
    """A printer sitting at FINISH — the only state (with FAILED) in which the
    plate-clear gate actually blocks dispatch."""
    s = _idle_status(filament_types, colors)
    s.state = "FINISH"
    return s


def _patch_printer_manager(idle_ids: set[int], status_map: dict | None = None, awaiting_ids: set[int] | None = None):
    """Context manager that mocks both printer_manager singletons used across modules."""
    status_map = status_map or {}
    awaiting_ids = awaiting_ids or set()

    def get_status_side_effect(pid):
        return status_map.get(pid, _idle_status(["PLA"]))

    def is_connected_side_effect(pid):
        return pid in idle_ids

    def is_awaiting_pc_side_effect(pid):
        return pid in awaiting_ids

    return (
        patch.multiple(
            "backend.app.services.auto_queue_eligibility.printer_manager",
            is_connected=is_connected_side_effect,
            get_status=get_status_side_effect,
            is_awaiting_plate_clear=is_awaiting_pc_side_effect,
        ),
        patch.multiple(
            "backend.app.services.print_scheduler.printer_manager",
            is_connected=is_connected_side_effect,
            get_status=get_status_side_effect,
            is_awaiting_plate_clear=is_awaiting_pc_side_effect,
        ),
        patch.multiple(
            "backend.app.services.auto_queue_ams.printer_manager",
            get_status=get_status_side_effect,
        ),
    )


@pytest.fixture
async def scheduler(monkeypatch, db_session):
    """Yield an AutoQueueScheduler that uses the test db_session."""
    sch = AutoQueueScheduler()

    # Override async_session so tick() uses our test session
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", _session_ctx)
    return sch


class TestAutoQueueSchedulerTick:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_assigns_to_idle_printer_with_matching_model(self, db_session, scheduler, printer_factory) -> None:
        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")

        item = AutoQueueItem(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({printer.id})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "assigned"
        assert item.assigned_to_item_id is not None
        assert item.assigned_at is not None
        assert item.waiting_reason is None

        # Verify per-printer item was created
        from sqlalchemy import select

        result = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.queue_id == pq.id))
        pq_items = result.scalars().all()
        assert len(pq_items) == 1
        assert pq_items[0].source_auto_item_id == item.id
        assert pq_items[0].position == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_sets_waiting_reason_when_no_printer_matches(self, db_session, scheduler, printer_factory) -> None:
        # P1S printer, but auto item wants A1MINI → no match
        await _make_printer_with_queue(db_session, printer_factory, model="P1S")

        item = AutoQueueItem(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager(set())
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "pending"
        assert item.waiting_reason is not None
        assert "A1MINI" in item.waiting_reason

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_stalled_tick_says_so_at_info(self, db_session, scheduler, printer_factory, caplog) -> None:
        """The visibility gap behind an unexplainable support bundle.

        A farm reported "the queue stopped moving". Three support bundles came
        back with no errors, because the only trace of a tick that placed
        nothing was a DEBUG line — invisible at the INFO level those bundles are
        collected at. The reason was computed and written to the row all along;
        it simply never reached the log.
        """
        await _make_printer_with_queue(db_session, printer_factory, model="P1S")
        db_session.add(AutoQueueItem(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager(set())
        with caplog.at_level("INFO"), p_elig, p_sched, p_ams:
            await scheduler.tick()

        assert "placed nothing this tick" in caplog.text
        assert "A1MINI" in caplog.text, "the reason itself has to be in the log, not just the fact of a stall"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_persistent_stall_does_not_repeat_every_tick(
        self, db_session, scheduler, printer_factory, caplog
    ) -> None:
        """At 30s a permanently stuck queue would write 120 identical lines an
        hour and bury everything else in the support log."""
        await _make_printer_with_queue(db_session, printer_factory, model="P1S")
        db_session.add(AutoQueueItem(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager(set())
        with caplog.at_level("INFO"), p_elig, p_sched, p_ams:
            await scheduler.tick()
            await scheduler.tick()
            await scheduler.tick()

        assert caplog.text.count("placed nothing this tick") == 1


class TestRoutingIsNotDispatching:
    """Readiness stopped being a filter here (see the module docstring of
    ``auto_queue_eligibility``).

    A printer held by the plate-clear gate used to be excluded from routing and
    reported as "Busy" — a farm operator saw three idle machines described as
    busy, and no Clear Plate prompt anywhere, because that prompt renders off
    the *printer's own queue* and auto-queue was refusing to put anything in it.
    Placing the work is what makes the block visible and fixable; the
    per-printer scheduler still decides when it may actually start.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_gated_printer_still_receives_the_work(self, db_session, scheduler, printer_factory) -> None:
        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        db_session.add(AutoQueueItem(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager(
            {printer.id},
            status_map={printer.id: _finished_status(["PLA"])},
            awaiting_ids={printer.id},
        )
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        item = (await db_session.execute(select(AutoQueueItem))).scalars().one()
        assert item.status == "assigned", "the gate belongs to dispatch, not to routing"
        placed = (await db_session.execute(select(PrintQueueItem))).scalars().one()
        assert placed.queue_id == pq.id
        assert placed.status == "pending", "placed, not started — check_queue still holds it at the gate"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_ready_printer_is_preferred_over_a_gated_one(self, db_session, scheduler, printer_factory) -> None:
        """Not a filter, but still a preference — work should land where it can
        start now, when there is a choice."""
        gated, gated_q = await _make_printer_with_queue(db_session, printer_factory, name="gated", model="A1MINI")
        ready, ready_q = await _make_printer_with_queue(db_session, printer_factory, name="ready", model="A1MINI")
        db_session.add(AutoQueueItem(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager(
            {gated.id, ready.id},
            status_map={gated.id: _finished_status(["PLA"]), ready.id: _idle_status(["PLA"])},
            awaiting_ids={gated.id},
        )
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        placed = (await db_session.execute(select(PrintQueueItem))).scalars().one()
        assert placed.queue_id == ready_q.id, "a printer that can start now should win the tie"


class TestRequirePreviousSuccessRoutesAround:
    """The distributor tier reads the gate differently from the per-printer one,
    on purpose. Upstream has one flat queue and can only mark the item skipped;
    we have somewhere else to send it, so a printer whose last print failed is
    simply not a candidate for a gated item — one bad machine must not stop a
    farm. Only when every candidate has just failed does the item wait, and a
    success on any of them undoes that by itself.
    """

    @staticmethod
    async def _finish(db_session, queue_id: int, status: str, minutes_ago: int) -> None:
        db_session.add(
            PrintQueueItem(
                queue_id=queue_id,
                status=status,
                position=0,
                completed_at=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc) - timedelta(minutes=minutes_ago),
            )
        )
        await db_session.commit()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_gated_item_goes_to_the_healthy_printer(self, db_session, scheduler, printer_factory) -> None:
        broken, broken_q = await _make_printer_with_queue(db_session, printer_factory, name="broken", model="A1MINI")
        healthy, healthy_q = await _make_printer_with_queue(db_session, printer_factory, name="healthy", model="A1MINI")
        await self._finish(db_session, broken_q.id, "failed", minutes_ago=5)

        db_session.add(
            AutoQueueItem(target_model="A1MINI", status="pending", position=1, require_previous_success=True)
        )
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({broken.id, healthy.id})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        placed = (
            (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.status == "pending"))).scalars().all()
        )
        assert len(placed) == 1
        assert placed[0].queue_id == healthy_q.id, "the gate must route around the failure, not sit on it"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_ungated_item_still_uses_the_printer(self, db_session, scheduler, printer_factory) -> None:
        broken, broken_q = await _make_printer_with_queue(db_session, printer_factory, name="broken", model="A1MINI")
        await self._finish(db_session, broken_q.id, "failed", minutes_ago=5)

        db_session.add(
            AutoQueueItem(target_model="A1MINI", status="pending", position=1, require_previous_success=False)
        )
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({broken.id})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        item = (await db_session.execute(select(AutoQueueItem))).scalars().one()
        assert item.status == "assigned"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_when_every_candidate_just_failed_the_reason_says_so(
        self, db_session, scheduler, printer_factory
    ) -> None:
        broken, broken_q = await _make_printer_with_queue(db_session, printer_factory, name="broken", model="A1MINI")
        await self._finish(db_session, broken_q.id, "failed", minutes_ago=5)

        db_session.add(
            AutoQueueItem(target_model="A1MINI", status="pending", position=1, require_previous_success=True)
        )
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({broken.id})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        item = (await db_session.execute(select(AutoQueueItem))).scalars().one()
        assert item.status == "pending"
        assert "Previous print failed" in (item.waiting_reason or "")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_flag_is_carried_onto_the_per_printer_row(self, db_session, scheduler, printer_factory) -> None:
        """Eligibility only proves the printer was clean at routing time; the
        per-printer scheduler re-checks at dispatch, which needs the flag."""
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        db_session.add(
            AutoQueueItem(target_model="A1MINI", status="pending", position=1, require_previous_success=True)
        )
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({printer.id})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        placed = (await db_session.execute(select(PrintQueueItem))).scalars().one()
        assert placed.require_previous_success is True


class TestAStalledQueueTellsTheOperator:
    """``on_queue_job_waiting`` was defined, wired to a provider column that
    defaults to enabled, given a per-chat Telegram toggle and en+uk templates —
    and never called by anything. On an unattended farm that silence is the
    failure: one bad print arms the gate, auto-queue stops routing to that
    printer, and the machine leaves the rotation with nobody told.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_operator_is_told_once_per_cause(self, db_session, scheduler, printer_factory) -> None:
        await _make_printer_with_queue(db_session, printer_factory, model="P1S")
        db_session.add(AutoQueueItem(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        sent = AsyncMock()
        p_elig, p_sched, p_ams = _patch_printer_manager(set())
        with (
            patch("backend.app.services.notification_service.notification_service.on_queue_job_waiting", sent),
            p_elig,
            p_sched,
            p_ams,
        ):
            await scheduler.tick()
            await scheduler.tick()
            await scheduler.tick()

        assert sent.await_count == 1, "a stuck queue must not message the operator every 30 seconds"
        assert "A1MINI" in sent.await_args.kwargs["waiting_reason"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_tick_that_places_work_tells_nobody(self, db_session, scheduler, printer_factory) -> None:
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        db_session.add(AutoQueueItem(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        sent = AsyncMock()
        p_elig, p_sched, p_ams = _patch_printer_manager({printer.id})
        with (
            patch("backend.app.services.notification_service.notification_service.on_queue_job_waiting", sent),
            p_elig,
            p_sched,
            p_ams,
        ):
            await scheduler.tick()

        sent.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_failing_notification_does_not_break_the_tick(self, db_session, scheduler, printer_factory) -> None:
        """Routing is the job; telling someone about it is not allowed to stop
        the scheduler from running the next tick."""
        await _make_printer_with_queue(db_session, printer_factory, model="P1S")
        db_session.add(AutoQueueItem(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        boom = AsyncMock(side_effect=RuntimeError("telegram is down"))
        p_elig, p_sched, p_ams = _patch_printer_manager(set())
        with (
            patch("backend.app.services.notification_service.notification_service.on_queue_job_waiting", boom),
            p_elig,
            p_sched,
            p_ams,
        ):
            await scheduler.tick()

        item = (await db_session.execute(select(AutoQueueItem))).scalars().one()
        assert item.waiting_reason is not None, "the reason still has to reach the row"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_busy_printer_excluded(self, db_session, scheduler, printer_factory) -> None:
        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        # Mark queue as printing — should be in busy_printers
        pq.status = "printing"
        await db_session.commit()

        item = AutoQueueItem(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({printer.id})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "pending"
        assert item.waiting_reason is not None  # Busy: ...

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_batch_fan_out_2_printers_4_items(self, db_session, scheduler, printer_factory) -> None:
        p1, _ = await _make_printer_with_queue(db_session, printer_factory, name="A1m-01", model="A1MINI")
        p2, _ = await _make_printer_with_queue(db_session, printer_factory, name="A1m-02", model="A1MINI")

        items = [
            AutoQueueItem(
                target_model="A1MINI",
                status="pending",
                position=i + 1,
                batch_id="batch-X",
            )
            for i in range(4)
        ]
        for it in items:
            db_session.add(it)
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({p1.id, p2.id})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        for it in items:
            await db_session.refresh(it)
        assigned_count = sum(1 for it in items if it.status == "assigned")
        pending_count = sum(1 for it in items if it.status == "pending")
        assert assigned_count == 2
        assert pending_count == 2

        # First two by position should be the assigned ones
        assert items[0].status == "assigned"
        assert items[1].status == "assigned"
        assert items[2].status == "pending"
        assert items[3].status == "pending"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_skips_manual_start(self, db_session, scheduler, printer_factory) -> None:
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")

        item = AutoQueueItem(target_model="A1MINI", status="pending", position=1, manual_start=True)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({printer.id})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "pending"  # Skipped, never visited

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_skips_future_scheduled(self, db_session, scheduler, printer_factory) -> None:
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        item = AutoQueueItem(target_model="A1MINI", status="pending", position=1, scheduled_time=future)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({printer.id})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "pending"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_sjf_been_jumped_marks_longer_peers(self, db_session, scheduler, printer_factory) -> None:
        # Enable SJF
        sjf = Settings(key="queue_shortest_first", value="true")
        db_session.add(sjf)

        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")

        # 3 items: long-unknown, short, long-known.
        # ORDER BY (sjf): target_model, been_jumped DESC, print_time ASC NULLS LAST, position
        # Without been_jumped marks: short (300) comes first, then long_known (3600), then long_unknown (NULL last)
        long_unknown = AutoQueueItem(target_model="A1MINI", status="pending", position=1, print_time_seconds=None)
        short = AutoQueueItem(target_model="A1MINI", status="pending", position=2, print_time_seconds=300)
        long_known = AutoQueueItem(target_model="A1MINI", status="pending", position=3, print_time_seconds=3600)
        for it in (long_unknown, short, long_known):
            db_session.add(it)
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({printer.id})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        # short should win the assignment (only 1 printer)
        for it in (long_unknown, short, long_known):
            await db_session.refresh(it)

        assert short.status == "assigned"

        # long_unknown was at position 1 (earlier than short) and has unknown time
        # → should be marked been_jumped
        assert long_unknown.been_jumped is True

        # long_known is at position 3 (LATER than short, which is position 2)
        # → should NOT be marked (only earlier-positioned peers get jumped)
        assert long_known.been_jumped is False


class TestAutoQueueDryingPriority:
    """Auto-queue divergence from upstream: a print takes priority over AMS
    drying. A printer that is non-idle ONLY because it is auto-drying is still
    eligible when ``queue_drying_block`` is False (the default), but a truly-idle
    printer is always preferred. When ``queue_drying_block`` is True, drying
    blocks the queue (parity with upstream's printer-specific path)."""

    @staticmethod
    def _mark_drying(monkeypatch, printer_id: int) -> None:
        from backend.app.services.print_scheduler import scheduler as print_scheduler_singleton

        monkeypatch.setitem(print_scheduler_singleton._drying_in_progress, printer_id, time.monotonic())

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_drying_printer_eligible_when_block_disabled(
        self, monkeypatch, db_session, scheduler, printer_factory
    ) -> None:
        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        self._mark_drying(monkeypatch, printer.id)

        item = AutoQueueItem(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        # Printer is connected (in idle_ids) but reports a non-idle (drying) state.
        p_elig, p_sched, p_ams = _patch_printer_manager({printer.id}, {printer.id: _drying_status(["PLA"])})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "assigned"  # print takes priority over drying
        assert item.assigned_to_item_id is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_drying_printer_is_still_routed_to_when_the_block_is_on(
        self, monkeypatch, db_session, scheduler, printer_factory
    ) -> None:
        db_session.add(Settings(key="queue_drying_block", value="true"))
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        self._mark_drying(monkeypatch, printer.id)

        item = AutoQueueItem(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched, p_ams = _patch_printer_manager({printer.id}, {printer.id: _drying_status(["PLA"])})
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        await db_session.refresh(item)
        # ``queue_drying_block`` answers "may a print interrupt drying?", which is
        # a dispatch question — check_queue still honours it. Routing the item to
        # the printer's queue costs nothing and makes the wait visible.
        assert item.status == "assigned"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_idle_printer_preferred_over_drying(
        self, monkeypatch, db_session, scheduler, printer_factory
    ) -> None:
        drying_p, drying_pq = await _make_printer_with_queue(
            db_session, printer_factory, name="A1m-dry", model="A1MINI"
        )
        idle_p, idle_pq = await _make_printer_with_queue(db_session, printer_factory, name="A1m-idle", model="A1MINI")
        self._mark_drying(monkeypatch, drying_p.id)

        item = AutoQueueItem(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        status_map = {drying_p.id: _drying_status(["PLA"]), idle_p.id: _idle_status(["PLA"])}
        p_elig, p_sched, p_ams = _patch_printer_manager({drying_p.id, idle_p.id}, status_map)
        with p_elig, p_sched, p_ams:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "assigned"

        # The routed per-printer item must land on the IDLE printer, not the drying one.
        from sqlalchemy import select

        result = await db_session.execute(select(PrintQueueItem))
        pq_items = result.scalars().all()
        assert len(pq_items) == 1
        assert pq_items[0].queue_id == idle_pq.id
