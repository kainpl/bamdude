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

import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.models.project import Project
from backend.app.models.project_line import ProjectLine
from backend.app.models.settings import Settings
from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
from backend.app.services.farm_forecast import model_key
from backend.app.services.printer_feed_snapshot import FeedTelemetry, snapshot_from_state
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

_test_models = {}


@pytest.fixture
def routing_item(tmp_path):
    """Scheduler scenarios carry readable source evidence, as real intake now guarantees."""
    count = 0

    def make(**kwargs):
        nonlocal count
        count += 1
        path = write_routing_3mf(
            tmp_path / f"item-{count}.3mf",
            {
                1: [
                    {"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": "1"},
                ]
            },
            model=kwargs.get("target_model", "A1MINI"),
        )
        return AutoQueueItem(
            library_file=LibraryFile(
                filename=path.name, file_path=str(path), file_type="gcode", file_size=path.stat().st_size
            ),
            **kwargs,
        )

    return make


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
    _test_models[p.id] = p.model
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
    """Mock the printer_manager singleton every module in this flow shares.

    Two paths to the SAME object — ``patch.multiple`` replaces attributes on it,
    so the eligibility read and the scheduler read see one mock. The third path,
    ``auto_queue_ams``, went with the matcher that used to live there.
    """
    status_map = status_map or {}
    awaiting_ids = awaiting_ids or set()

    def get_status_side_effect(pid):
        return status_map.get(pid, _idle_status(["PLA"]))

    def get_snapshot(pid):
        status = get_status_side_effect(pid)
        telemetry = FeedTelemetry()
        telemetry.observe({"print": status.raw_data}, _test_models.get(pid, "A1MINI"))
        return snapshot_from_state(
            pid,
            _test_models.get(pid, "A1MINI"),
            SimpleNamespace(connected=pid in idle_ids, connection_generation=1, feed_telemetry=telemetry),
        )

    def is_connected_side_effect(pid):
        return pid in idle_ids

    def is_awaiting_pc_side_effect(pid):
        return pid in awaiting_ids

    return (
        patch.multiple(
            "backend.app.services.auto_queue_eligibility.printer_manager",
            is_connected=is_connected_side_effect,
            get_feed_snapshot=get_snapshot,
            get_status=get_status_side_effect,
            is_awaiting_plate_clear=is_awaiting_pc_side_effect,
        ),
        patch.multiple(
            "backend.app.services.print_scheduler.printer_manager",
            is_connected=is_connected_side_effect,
            get_feed_snapshot=get_snapshot,
            get_status=get_status_side_effect,
            is_awaiting_plate_clear=is_awaiting_pc_side_effect,
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
    async def test_assigns_to_idle_printer_with_matching_model(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")

        item = routing_item(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({printer.id})
        with (
            p_elig,
            p_sched,
            patch(
                "backend.app.services.auto_queue_scheduler.ws_manager.send_queue_changed", new_callable=AsyncMock
            ) as queue_changed,
        ):
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
        await db_session.refresh(pq)
        assert pq.pending_count == 1
        queue_changed.assert_awaited_once_with(printer.id)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_assignment_recounts_skipped_and_is_idempotent(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        from backend.app.services.queue_counters import update_queue_counters

        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        db_session.add(PrintQueueItem(queue_id=pq.id, status="skipped", position=1))
        item = routing_item(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({printer.id})
        with p_elig, p_sched:
            await scheduler.tick()
            await scheduler.tick()

        await db_session.refresh(pq)
        assert (pq.pending_count, pq.skipped_count) == (1, 1)
        rows = (await db_session.scalars(select(PrintQueueItem).where(PrintQueueItem.queue_id == pq.id))).all()
        assert len(rows) == 2
        promoted = next(row for row in rows if row.source_auto_item_id == item.id)
        # The existing dispatch counter path removes printing rows from pending.
        promoted.status = "printing"
        await db_session.flush()
        await update_queue_counters(db_session, pq.id)
        await db_session.commit()
        await db_session.refresh(pq)
        assert (pq.pending_count, pq.skipped_count) == (0, 1)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_counter_failure_rolls_back_assignment(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        from backend.app.services.queue_counters import update_queue_counters

        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        item = routing_item(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()
        queue_id = pq.id

        async def fail_after_recount(db, qid):
            await update_queue_counters(db, qid)
            await db.flush()
            raise RuntimeError("counter write failed")

        p_elig, p_sched = _patch_printer_manager({printer.id})
        with (
            p_elig,
            p_sched,
            patch("backend.app.services.auto_queue_scheduler.update_queue_counters", side_effect=fail_after_recount),
            pytest.raises(RuntimeError, match="counter write failed"),
        ):
            await scheduler._assign(db_session, item, printer)

        await db_session.commit()
        await db_session.refresh(pq)
        await db_session.refresh(item)
        assert pq.pending_count == 0
        assert item.status == "pending"
        assert item.assigned_to_item_id is None
        assert not (await db_session.scalars(select(PrintQueueItem).where(PrintQueueItem.queue_id == queue_id))).all()

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.parametrize("allow_base_material_match,assigned", [(True, True), (False, False)])
    async def test_a_profile_only_retag_inside_the_tick_defers_only_a_job_that_asked_for_the_profile(
        self,
        db_session,
        scheduler,
        printer_factory,
        routing_item,
        monkeypatch,
        allow_base_material_match,
        assigned,
    ) -> None:
        """Routing and assignment are two reads of the feed, and a spool can be
        re-profiled between them.

        ``_assign`` re-reads the feed before it claims the row. Read RAW, a
        variant-only retag moves the marker — and for a job that said «any ABS
        will do» that is a fact its own plan had already been told to ignore, so
        the placement died on ``Filament routing evidence changed`` and the tick
        logged a full stack trace for something benign. With the option off the
        profile IS part of what the job asked for, and the refusal is right.
        """
        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        item = routing_item(
            target_model="A1MINI",
            status="pending",
            position=1,
            allow_base_material_match=allow_base_material_match,
        )
        db_session.add(item)
        await db_session.commit()

        status = _idle_status(["PLA"])
        assign = AutoQueueScheduler._assign

        async def retag_then_assign(self, db, auto_item, target, *args, **kwargs):
            status.raw_data["ams"][0]["tray"][0]["tray_info_idx"] = "GFA01"
            return await assign(self, db, auto_item, target, *args, **kwargs)

        monkeypatch.setattr(AutoQueueScheduler, "_assign", retag_then_assign)
        p_elig, p_sched = _patch_printer_manager({printer.id}, {printer.id: status})
        with p_elig, p_sched:
            await scheduler.tick()

        await db_session.refresh(item)
        rows = (
            (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.queue_id == pq.id))).scalars().all()
        )
        assert (item.status == "assigned") == assigned
        assert len(rows) == (1 if assigned else 0)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_sets_waiting_reason_when_no_printer_matches(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        # P1S printer, but auto item wants A1MINI → no match
        await _make_printer_with_queue(db_session, printer_factory, model="P1S")

        item = routing_item(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager(set())
        with p_elig, p_sched:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "pending"
        assert item.waiting_reason is not None
        assert "A1 Mini" in item.waiting_reason

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_stalled_tick_says_so_at_info(
        self, db_session, scheduler, printer_factory, caplog, routing_item
    ) -> None:
        """The visibility gap behind an unexplainable support bundle.

        A farm reported "the queue stopped moving". Three support bundles came
        back with no errors, because the only trace of a tick that placed
        nothing was a DEBUG line — invisible at the INFO level those bundles are
        collected at. The reason was computed and written to the row all along;
        it simply never reached the log.
        """
        await _make_printer_with_queue(db_session, printer_factory, model="P1S")
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager(set())
        with caplog.at_level("INFO"), p_elig, p_sched:
            await scheduler.tick()

        assert "placed nothing this tick" in caplog.text
        assert "A1 Mini" in caplog.text, "the reason itself has to be in the log, not just the fact of a stall"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_persistent_stall_does_not_repeat_every_tick(
        self, db_session, scheduler, printer_factory, caplog, routing_item
    ) -> None:
        """At 30s a permanently stuck queue would write 120 identical lines an
        hour and bury everything else in the support log."""
        await _make_printer_with_queue(db_session, printer_factory, model="P1S")
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager(set())
        with caplog.at_level("INFO"), p_elig, p_sched:
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
    async def test_a_gated_printer_still_receives_the_work(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager(
            {printer.id},
            status_map={printer.id: _finished_status(["PLA"])},
            awaiting_ids={printer.id},
        )
        with p_elig, p_sched:
            await scheduler.tick()

        item = (await db_session.execute(select(AutoQueueItem))).scalars().one()
        assert item.status == "assigned", "the gate belongs to dispatch, not to routing"
        placed = (await db_session.execute(select(PrintQueueItem))).scalars().one()
        assert placed.queue_id == pq.id
        assert placed.status == "pending", "placed, not started — check_queue still holds it at the gate"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_ready_printer_is_preferred_over_a_gated_one(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        """Not a filter, but still a preference — work should land where it can
        start now, when there is a choice."""
        gated, gated_q = await _make_printer_with_queue(db_session, printer_factory, name="gated", model="A1MINI")
        ready, ready_q = await _make_printer_with_queue(db_session, printer_factory, name="ready", model="A1MINI")
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager(
            {gated.id, ready.id},
            status_map={gated.id: _finished_status(["PLA"]), ready.id: _idle_status(["PLA"])},
            awaiting_ids={gated.id},
        )
        with p_elig, p_sched:
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
    async def test_a_gated_item_goes_to_the_healthy_printer(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        broken, broken_q = await _make_printer_with_queue(db_session, printer_factory, name="broken", model="A1MINI")
        healthy, healthy_q = await _make_printer_with_queue(db_session, printer_factory, name="healthy", model="A1MINI")
        await self._finish(db_session, broken_q.id, "failed", minutes_ago=5)

        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1, require_previous_success=True))
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({broken.id, healthy.id})
        with p_elig, p_sched:
            await scheduler.tick()

        placed = (
            (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.status == "pending"))).scalars().all()
        )
        assert len(placed) == 1
        assert placed[0].queue_id == healthy_q.id, "the gate must route around the failure, not sit on it"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_ungated_item_still_uses_the_printer(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        broken, broken_q = await _make_printer_with_queue(db_session, printer_factory, name="broken", model="A1MINI")
        await self._finish(db_session, broken_q.id, "failed", minutes_ago=5)

        db_session.add(
            routing_item(target_model="A1MINI", status="pending", position=1, require_previous_success=False)
        )
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({broken.id})
        with p_elig, p_sched:
            await scheduler.tick()

        item = (await db_session.execute(select(AutoQueueItem))).scalars().one()
        assert item.status == "assigned"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_when_every_candidate_just_failed_the_reason_says_so(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        broken, broken_q = await _make_printer_with_queue(db_session, printer_factory, name="broken", model="A1MINI")
        await self._finish(db_session, broken_q.id, "failed", minutes_ago=5)

        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1, require_previous_success=True))
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({broken.id})
        with p_elig, p_sched:
            await scheduler.tick()

        item = (await db_session.execute(select(AutoQueueItem))).scalars().one()
        assert item.status == "pending"
        assert "previous print failed" in (item.waiting_reason or "").lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_flag_is_carried_onto_the_per_printer_row(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        """Eligibility only proves the printer was clean at routing time; the
        per-printer scheduler re-checks at dispatch, which needs the flag."""
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1, require_previous_success=True))
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({printer.id})
        with p_elig, p_sched:
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
    async def test_the_operator_is_told_once_per_cause(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        await _make_printer_with_queue(db_session, printer_factory, model="P1S")
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        sent = AsyncMock()
        p_elig, p_sched = _patch_printer_manager(set())
        with (
            patch("backend.app.services.notification_service.notification_service.on_queue_job_waiting", sent),
            p_elig,
            p_sched,
        ):
            await scheduler.tick()
            await scheduler.tick()
            await scheduler.tick()

        assert sent.await_count == 1, "a stuck queue must not message the operator every 30 seconds"
        assert "A1 Mini" in sent.await_args.kwargs["waiting_reason"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_tick_that_places_work_tells_nobody(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        sent = AsyncMock()
        p_elig, p_sched = _patch_printer_manager({printer.id})
        with (
            patch("backend.app.services.notification_service.notification_service.on_queue_job_waiting", sent),
            p_elig,
            p_sched,
        ):
            await scheduler.tick()

        sent.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_failing_notification_does_not_break_the_tick(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        """Routing is the job; telling someone about it is not allowed to stop
        the scheduler from running the next tick."""
        await _make_printer_with_queue(db_session, printer_factory, model="P1S")
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()

        boom = AsyncMock(side_effect=RuntimeError("telegram is down"))
        p_elig, p_sched = _patch_printer_manager(set())
        with (
            patch("backend.app.services.notification_service.notification_service.on_queue_job_waiting", boom),
            p_elig,
            p_sched,
        ):
            await scheduler.tick()

        item = (await db_session.execute(select(AutoQueueItem))).scalars().one()
        assert item.waiting_reason is not None, "the reason still has to reach the row"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_busy_printer_excluded(self, db_session, scheduler, printer_factory, routing_item) -> None:
        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        # Mark queue as printing — should be in busy_printers
        pq.status = "printing"
        await db_session.commit()

        item = routing_item(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({printer.id})
        with p_elig, p_sched:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "pending"
        assert item.waiting_reason is not None  # Busy: ...

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_batch_fan_out_2_printers_4_items(self, db_session, scheduler, printer_factory, routing_item) -> None:
        p1, _ = await _make_printer_with_queue(db_session, printer_factory, name="A1m-01", model="A1MINI")
        p2, _ = await _make_printer_with_queue(db_session, printer_factory, name="A1m-02", model="A1MINI")

        items = [
            routing_item(
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

        p_elig, p_sched = _patch_printer_manager({p1.id, p2.id})
        with p_elig, p_sched:
            await scheduler.tick()

        for it in items:
            await db_session.refresh(it)
        assigned_count = sum(1 for it in items if it.status == "assigned")
        pending_count = sum(1 for it in items if it.status == "pending")
        assert assigned_count == 2
        assert pending_count == 2
        queues = (
            await db_session.scalars(select(PrinterQueue).where(PrinterQueue.printer_id.in_([p1.id, p2.id])))
        ).all()
        for queue in queues:
            await db_session.refresh(queue)
            assert queue.pending_count == 1

        # First two by position should be the assigned ones
        assert items[0].status == "assigned"
        assert items[1].status == "assigned"
        assert items[2].status == "pending"
        assert items[3].status == "pending"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_skips_manual_start(self, db_session, scheduler, printer_factory, routing_item) -> None:
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")

        item = routing_item(target_model="A1MINI", status="pending", position=1, manual_start=True)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({printer.id})
        with p_elig, p_sched:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "pending"  # Skipped, never visited

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_skips_future_scheduled(self, db_session, scheduler, printer_factory, routing_item) -> None:
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")

        future = datetime.now(timezone.utc) + timedelta(hours=1)
        item = routing_item(target_model="A1MINI", status="pending", position=1, scheduled_time=future)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({printer.id})
        with p_elig, p_sched:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "pending"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_sjf_been_jumped_marks_longer_peers(
        self, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        # Enable SJF
        sjf = Settings(key="queue_shortest_first", value="true")
        db_session.add(sjf)

        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")

        # 3 items: long-unknown, short, long-known.
        # ORDER BY (sjf): target_model, been_jumped DESC, print_time ASC NULLS LAST, position
        # Without been_jumped marks: short (300) comes first, then long_known (3600), then long_unknown (NULL last)
        long_unknown = routing_item(target_model="A1MINI", status="pending", position=1, print_time_seconds=None)
        short = routing_item(target_model="A1MINI", status="pending", position=2, print_time_seconds=300)
        long_known = routing_item(target_model="A1MINI", status="pending", position=3, print_time_seconds=3600)
        for it in (long_unknown, short, long_known):
            db_session.add(it)
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({printer.id})
        with p_elig, p_sched:
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
        self, monkeypatch, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        printer, pq = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        self._mark_drying(monkeypatch, printer.id)

        item = routing_item(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        # Printer is connected (in idle_ids) but reports a non-idle (drying) state.
        p_elig, p_sched = _patch_printer_manager({printer.id}, {printer.id: _drying_status(["PLA"])})
        with p_elig, p_sched:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "assigned"  # print takes priority over drying
        assert item.assigned_to_item_id is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_drying_printer_is_still_routed_to_when_the_block_is_on(
        self, monkeypatch, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        db_session.add(Settings(key="queue_drying_block", value="true"))
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        self._mark_drying(monkeypatch, printer.id)

        item = routing_item(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({printer.id}, {printer.id: _drying_status(["PLA"])})
        with p_elig, p_sched:
            await scheduler.tick()

        await db_session.refresh(item)
        # ``queue_drying_block`` answers "may a print interrupt drying?", which is
        # a dispatch question — check_queue still honours it. Routing the item to
        # the printer's queue costs nothing and makes the wait visible.
        assert item.status == "assigned"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_idle_printer_preferred_over_drying(
        self, monkeypatch, db_session, scheduler, printer_factory, routing_item
    ) -> None:
        drying_p, drying_pq = await _make_printer_with_queue(
            db_session, printer_factory, name="A1m-dry", model="A1MINI"
        )
        idle_p, idle_pq = await _make_printer_with_queue(db_session, printer_factory, name="A1m-idle", model="A1MINI")
        self._mark_drying(monkeypatch, drying_p.id)

        item = routing_item(target_model="A1MINI", status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        status_map = {drying_p.id: _drying_status(["PLA"]), idle_p.id: _idle_status(["PLA"])}
        p_elig, p_sched = _patch_printer_manager({drying_p.id, idle_p.id}, status_map)
        with p_elig, p_sched:
            await scheduler.tick()

        await db_session.refresh(item)
        assert item.status == "assigned"

        # The routed per-printer item must land on the IDLE printer, not the drying one.
        from sqlalchemy import select

        result = await db_session.execute(select(PrintQueueItem))
        pq_items = result.scalars().all()
        assert len(pq_items) == 1
        assert pq_items[0].queue_id == idle_pq.id


# ---------- rebalancing across models (spec 2026-09-10) ----------


def _plate_file(tmp_path, filename: str, model: str, *, seconds: int, hooks: int) -> LibraryFile:
    """A sliced single-plate file for ``model`` making ``hooks`` hooks in ``seconds``.

    A real routing 3MF on disk, because the rebalancer reads the target plate
    through the strict source reader before it converts anything — and the 3MF's
    own ``prediction`` carries ``seconds`` too, since the plan decides on
    ``file_metadata`` while the converted row's estimate comes from the read.
    """
    path = write_routing_3mf(
        tmp_path / filename,
        {1: [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": "1"}]},
        model=model,
        prediction=seconds,
    )
    return LibraryFile(
        filename=filename,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="gcode",
        file_metadata={
            "sliced_for_model": model,
            "print_time_seconds": seconds,
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {str(i + 1): ("hook" if i == 0 else f"hook_{i + 1}") for i in range(hooks)},
                    "print_time_seconds": seconds,
                    "filaments": [{"slot_id": 1, "type": "PLA"}],
                }
            ],
        },
    )


async def rebalance_farm(db_session, printer_factory, tmp_path, *, mini_seconds: int = 1000):
    """One P1S two hours into a print, one idle A1 mini; a 6-hook order line with one
    pending P1S print of a 6-hook plate; the same hooks sliced two per plate for the mini.

    Returns a namespace: p1s, mini, mini_q, big, small, project, line, item.
    """
    p1s, p1s_q = await _make_printer_with_queue(db_session, printer_factory, name="P1S-1", model="P1S")
    mini, mini_q = await _make_printer_with_queue(db_session, printer_factory, name="Mini-1", model="A1MINI")
    p1s_q.status = "printing"
    db_session.add(
        PrintArchive(
            printer_id=p1s.id, status="printing", print_time_seconds=7200, filename="running", file_path="", file_size=0
        )
    )
    big = _plate_file(tmp_path, "hooks-p1s.gcode.3mf", "P1S", seconds=3600, hooks=6)
    small = _plate_file(tmp_path, "hooks-mini.gcode.3mf", "A1MINI", seconds=mini_seconds, hooks=2)
    product = Product(name="Hook")
    db_session.add_all([big, small, product])
    await db_session.flush()
    db_session.add_all(
        [
            ProductPart(
                product_id=product.id, kind="printed", name="hook", name_key="hook", qty_per_unit=1, aliases=["hook"]
            ),
            ProductPlate(product_id=product.id, library_file_id=big.id, plate_index=0),
            ProductPlate(product_id=product.id, library_file_id=small.id, plate_index=0),
        ]
    )
    project = Project(name="Hooks", status="active")
    db_session.add(project)
    await db_session.flush()
    line = ProjectLine(project_id=project.id, product_id=product.id, quantity=6)
    db_session.add(line)
    await db_session.flush()
    item = AutoQueueItem(
        library_file_id=big.id,
        plate_id=1,
        project_id=project.id,
        project_line_id=line.id,
        target_model="P1S",
        required_filament_types=json.dumps(["PLA"]),
        print_time_seconds=3600,
        status="pending",
        position=1,
    )
    db_session.add(item)
    await db_session.commit()
    return SimpleNamespace(
        p1s=p1s, mini=mini, mini_q=mini_q, big=big, small=small, project=project, line=line, item=item
    )


async def _pending_rows(db_session) -> list[AutoQueueItem]:
    return list(
        (
            await db_session.execute(
                select(AutoQueueItem)
                .where(AutoQueueItem.status == "pending")
                .order_by(AutoQueueItem.position, AutoQueueItem.id)
            )
        )
        .scalars()
        .all()
    )


class TestRebalanceAcrossModels:
    """The tick moves a busy model's pending line work to an idle model when that
    finishes sooner — parts-based (6 hooks → three 2-hook mini prints), behind a
    setting that is off by default, and never touching what the operator pinned."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_setting_off_moves_nothing(self, db_session, scheduler, printer_factory, tmp_path) -> None:
        farm = await rebalance_farm(db_session, printer_factory, tmp_path)
        p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
        with p_elig, p_sched:
            await scheduler.tick()
        await db_session.refresh(farm.item)
        assert (farm.item.status, farm.item.target_model, farm.item.rebalanced_at) == ("pending", "P1S", None)
        assert len(await _pending_rows(db_session)) == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_setting_on_moves_six_hooks_as_three_mini_prints_and_the_next_tick_places_one(
        self, db_session, scheduler, printer_factory, tmp_path
    ) -> None:
        farm = await rebalance_farm(db_session, printer_factory, tmp_path)
        db_session.add(Settings(key="auto_queue_rebalance_models", value="true"))
        await db_session.commit()

        p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
        with p_elig, p_sched:
            await scheduler.tick()

        rows = await _pending_rows(db_session)
        assert len(rows) == 3, "one converted in place, two created"
        converted = next(r for r in rows if r.id == farm.item.id)
        assert (converted.library_file_id, model_key(converted.target_model), converted.plate_id) == (
            farm.small.id,
            model_key("A1MINI"),
            1,
        )
        assert converted.rebalanced_from_model == "P1S" and converted.rebalanced_at is not None
        assert converted.print_time_seconds == 1000 and converted.position == 1
        assert json.loads(converted.required_filament_types) == ["PLA"]
        for r in rows:
            assert (r.project_line_id, r.project_id, model_key(r.target_model)) == (
                farm.line.id,
                farm.project.id,
                model_key("A1MINI"),
            )
            assert r.rebalanced_from_model == "P1S" and r.batch_id == converted.batch_id and r.batch_id is not None

        with p_elig, p_sched:
            await scheduler.tick()
        placed = (await db_session.execute(select(PrintQueueItem))).scalars().all()
        assert len(placed) == 1 and placed[0].queue_id == farm.mini_q.id
        assert placed[0].source_auto_item_id == converted.id, "the converted row is first in queue order"
        assert len(await _pending_rows(db_session)) == 2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_slower_model_does_not_take_the_work(
        self, db_session, scheduler, printer_factory, tmp_path
    ) -> None:
        """Three mini prints of 5000 s = 15000 s; home is free in 7200 s and prints in 3600 s = 10800 s."""
        farm = await rebalance_farm(db_session, printer_factory, tmp_path, mini_seconds=5000)
        db_session.add(Settings(key="auto_queue_rebalance_models", value="true"))
        await db_session.commit()
        p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
        with p_elig, p_sched:
            await scheduler.tick()
        await db_session.refresh(farm.item)
        assert (farm.item.target_model, farm.item.rebalanced_at) == ("P1S", None)
        assert len(await _pending_rows(db_session)) == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_gated_receiver_only_looks_free(self, db_session, scheduler, printer_factory, tmp_path) -> None:
        """An empty queue is not readiness: a mini still waiting for its plate clear receives nothing."""
        farm = await rebalance_farm(db_session, printer_factory, tmp_path)
        db_session.add(Settings(key="auto_queue_rebalance_models", value="true"))
        await db_session.commit()
        p_elig, p_sched = _patch_printer_manager(
            {farm.p1s.id, farm.mini.id},
            status_map={farm.mini.id: _finished_status(["PLA"])},
            awaiting_ids={farm.mini.id},
        )
        with p_elig, p_sched:
            await scheduler.tick()
        await db_session.refresh(farm.item)
        assert farm.item.target_model == "P1S"
        assert len(await _pending_rows(db_session)) == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.parametrize("queue_status", ["paused", "error"])
    async def test_a_queue_that_would_never_dispatch_is_no_home(
        self, db_session, scheduler, printer_factory, tmp_path, queue_status
    ) -> None:
        """A paused or errored queue may not RECEIVE work: ``check_queue`` never
        dispatches from one, so the move would land there, the next tick would answer
        ``home_model_idle`` for ever, and nothing would print."""
        farm = await rebalance_farm(db_session, printer_factory, tmp_path)
        farm.mini_q.status = queue_status
        db_session.add(Settings(key="auto_queue_rebalance_models", value="true"))
        await db_session.commit()
        p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
        with p_elig, p_sched:
            await scheduler.tick()
        await db_session.refresh(farm.item)
        assert (farm.item.target_model, farm.item.rebalanced_at) == ("P1S", None)
        assert len(await _pending_rows(db_session)) == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_pinned_item_stays(self, db_session, scheduler, printer_factory, tmp_path) -> None:
        farm = await rebalance_farm(db_session, printer_factory, tmp_path)
        farm.item.filament_overrides = json.dumps(
            [{"slot_id": 1, "type": "PLA", "color": "#FFFFFF", "force_color_match": True}]
        )
        db_session.add(Settings(key="auto_queue_rebalance_models", value="true"))
        await db_session.commit()
        p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
        with p_elig, p_sched:
            await scheduler.tick()
        await db_session.refresh(farm.item)
        assert (farm.item.target_model, farm.item.rebalanced_at) == ("P1S", None)
        assert len(await _pending_rows(db_session)) == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_line_is_left_alone_for_five_minutes_after_a_move(
        self, db_session, scheduler, printer_factory, tmp_path
    ) -> None:
        farm = await rebalance_farm(db_session, printer_factory, tmp_path)
        db_session.add(Settings(key="auto_queue_rebalance_models", value="true"))
        await db_session.commit()
        p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
        with p_elig, p_sched:
            await scheduler.tick()
        moved = await _pending_rows(db_session)
        assert len(moved) == 3

        # Park the moved prints (staged rows are neither placed nor moved) so the
        # mini stays idle — then the ONLY thing between a second P1S print of the
        # same line and that idle mini is the cooldown.
        for row in moved:
            row.manual_start = True
        second = AutoQueueItem(
            library_file_id=farm.big.id,
            plate_id=1,
            project_id=farm.project.id,
            project_line_id=farm.line.id,
            target_model="P1S",
            required_filament_types=json.dumps(["PLA"]),
            print_time_seconds=3600,
            status="pending",
            position=9,
        )
        db_session.add(second)
        await db_session.commit()
        with p_elig, p_sched:
            await scheduler.tick()
        await db_session.refresh(second)
        assert (second.target_model, second.rebalanced_at) == ("P1S", None), "cooldown: the line was moved seconds ago"

        # Ten minutes later the line is fair game again.
        for row in moved:
            row.rebalanced_at = row.rebalanced_at - timedelta(minutes=10)
        await db_session.commit()
        with p_elig, p_sched:
            await scheduler.tick()
        await db_session.refresh(second)
        assert second.rebalanced_from_model == "P1S" and model_key(second.target_model) == model_key("A1MINI")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_creation_that_refuses_leaves_the_row_exactly_as_it_was(
        self, monkeypatch, db_session, scheduler, printer_factory, tmp_path
    ) -> None:
        """A move is all-or-nothing, and the writer refusing is where that is decided.

        ``add_items_to_auto_queue`` refuses the companions (a dangling
        ``project_id``, a line that is not the order's) and the tick's blanket
        ``except`` swallows it, so without the field-by-field restore below the
        line would keep ONE print of a 2-hook plate where six hooks were owed
        and never learn that four went missing.

        ⚠️ The *mechanism* that makes a half-move durable changed on
        2026-09-13: the writer now commits FIRST and copies the bytes into the
        queue spool afterwards, so durability no longer needs an autoflush or
        the tick's commit — the converted row is already on disk while the copy
        runs. The restore this test pins is what covers a writer refusal; the
        crash-during-copy window is named in ``queue_rebalance._apply`` and is
        closed by capturing before the row is mutated.
        """
        from backend.app.services import queue_rebalance

        farm = await rebalance_farm(db_session, printer_factory, tmp_path)
        db_session.add(Settings(key="auto_queue_rebalance_models", value="true"))
        await db_session.commit()

        async def _refuse(*_args, **_kwargs):
            raise HTTPException(404, "Project not found")

        monkeypatch.setattr(queue_rebalance, "add_items_to_auto_queue", _refuse)

        p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
        with p_elig, p_sched:
            await scheduler.tick()

        await db_session.refresh(farm.item)
        assert (farm.item.library_file_id, farm.item.plate_id, farm.item.target_model) == (
            farm.big.id,
            1,
            "P1S",
        )
        assert (farm.item.print_time_seconds, farm.item.batch_id) == (3600, None)
        assert (farm.item.rebalanced_at, farm.item.rebalanced_from_model) == (None, None)
        assert json.loads(farm.item.required_filament_types) == ["PLA"]
        assert len(await _pending_rows(db_session)) == 1, "no companion row was created either"

        # The same run, called directly, names the reason — and counts nothing.
        # ⚠️ Inside the patch: outside it no printer is connected, so the run
        # would refuse for want of a receiver and never reach the writer.
        with p_elig, p_sched:
            result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
        assert (result.converted, result.created, result.moved_parts) == (0, 0, 0)
        assert (farm.item.id, "creation_failed") in result.skipped

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_companions_the_writer_already_committed_are_deleted_when_the_stamping_fails(
        self, monkeypatch, db_session, scheduler, printer_factory, tmp_path
    ) -> None:
        """The writer COMMITS, so forgetting its rows is not undoing them.

        ``add_items_to_auto_queue`` succeeds and commits — the converted row and
        its two companions are durable — and the ``flush`` of the stamping loop
        right after it fails. Restoring the row's fields is then only half the
        undo: the companions would stay, filed under the line, unstamped (so no
        cooldown holds the line) and covering parts nobody owes — six hooks
        queued as ten.
        """
        from backend.app.services import queue_rebalance

        farm = await rebalance_farm(db_session, printer_factory, tmp_path)
        db_session.add(Settings(key="auto_queue_rebalance_models", value="true"))
        await db_session.commit()

        real_writer = queue_rebalance.add_items_to_auto_queue
        real_flush = db_session.flush

        async def _fail_once(*args, **kwargs):
            db_session.flush = real_flush  # only the stamping flush fails; the undo needs the real one
            raise RuntimeError("flush failed")

        async def _commit_then_arm_the_flush(*args, **kwargs):
            rows = await real_writer(*args, **kwargs)
            db_session.flush = _fail_once
            return rows

        monkeypatch.setattr(queue_rebalance, "add_items_to_auto_queue", _commit_then_arm_the_flush)

        p_elig, p_sched = _patch_printer_manager({farm.p1s.id, farm.mini.id})
        with p_elig, p_sched:
            await scheduler.tick()

        rows = await _pending_rows(db_session)
        assert len(rows) == 1, "the two committed companions were deleted"
        await db_session.refresh(farm.item)
        assert (farm.item.library_file_id, farm.item.plate_id, farm.item.target_model) == (farm.big.id, 1, "P1S")
        assert (farm.item.print_time_seconds, farm.item.batch_id) == (3600, None)
        assert (farm.item.rebalanced_at, farm.item.rebalanced_from_model) == (None, None)

        with p_elig, p_sched:
            result = await queue_rebalance.rebalance(db_session, line_ids=[farm.line.id], force=True)
        assert (result.converted, result.created, result.moved_parts) == (0, 0, 0)
        assert (farm.item.id, "creation_failed") in result.skipped
        assert len(await _pending_rows(db_session)) == 1
