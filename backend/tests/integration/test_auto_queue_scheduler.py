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
from unittest.mock import patch

import pytest

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


def _patch_printer_manager(idle_ids: set[int], status_map: dict | None = None):
    """Context manager that mocks both printer_manager singletons used across modules."""
    status_map = status_map or {}

    def get_status_side_effect(pid):
        return status_map.get(pid, _idle_status(["PLA"]))

    def is_connected_side_effect(pid):
        return pid in idle_ids

    def is_awaiting_pc_side_effect(pid):
        return False

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
    async def test_drying_printer_skipped_when_block_enabled(
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
        assert item.status == "pending"  # drying blocks the queue
        assert item.waiting_reason is not None

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
