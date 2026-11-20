"""The queue's log says why a printer sat out a pass (upstream 09b4584d, #3018).

``busy_printers`` holds two opposite facts: printers that could not take work
(running, offline, held by a plate or drying) and printers the pass has just
CLAIMED by dispatching to them. The summary called every one of them "not
available", reading the printer's state at logging time — so a report carried
"printer 1 not available — connected=True, state=IDLE" one line before
"Starting queue item 18" on that very printer. Every site that takes a printer
out of the pass now records why, and the summary tells a reservation from an
obstruction; the live fields stay, labelled as read now, not as the cause.

(The other half of #3018 — the queue calling an unscheduled item "ASAP" — is
not ours: the time column never said it.)
"""

from __future__ import annotations

import inspect
import logging
import re
from types import SimpleNamespace

import pytest

from backend.app.services import print_scheduler
from backend.app.services.print_scheduler import PrintScheduler


class TestEverySiteSaysWhy:
    def test_no_printer_is_taken_out_of_the_pass_without_a_reason(self):
        # One add, inside mark_busy; every site goes through it.
        source = inspect.getsource(PrintScheduler.check_queue)
        assert len(re.findall(r"busy_printers\.add\(", source)) == 1
        helper = source[source.index("def mark_busy") :]
        assert helper.index("busy_printers.add(") < helper.index("\n\n")

    def test_the_reasons_are_the_closed_set(self):
        source = inspect.getsource(PrintScheduler.check_queue)
        reasons = set(re.findall(r'mark_busy\([^,]+,\s*"([a-z_]+)"\)', source))
        # The seed (active claims) is labelled before the loop runs.
        assert 'busy_reasons[claimed_printer_id] = "claimed"' in source
        assert reasons == {
            "dispatch_hold",
            "dispatch_runtime_conflict",
            "powering_on",
            "power_on_failed",
            "offline",
            "not_idle",
            "drying",
            "dispatched",
            # G03 B, A8: the row changed since the pass loaded it, or a verdict's proof refused.
            "item_changed",
            "verdict_changed",
        }


class TestTheSummary:
    def _log(self, caplog, busy, reasons):
        with caplog.at_level(logging.INFO, logger=print_scheduler.logger.name):
            PrintScheduler._log_busy_printers(busy, reasons)
        return [r.getMessage() for r in caplog.records]

    def test_a_printer_dispatched_to_is_reserved_not_unavailable(self, caplog, monkeypatch):
        monkeypatch.setattr(print_scheduler.printer_manager, "get_status", lambda pid: None)
        lines = self._log(caplog, {1}, {1: "dispatched"})
        assert len(lines) == 1
        assert "not available" not in lines[0]
        assert "reserved" in lines[0]

    def test_an_obstruction_names_its_reason_and_the_state_as_read_now(self, caplog, monkeypatch):
        monkeypatch.setattr(print_scheduler.printer_manager, "get_status", lambda pid: None)
        monkeypatch.setattr(print_scheduler.printer_manager, "is_connected", lambda pid: False)
        monkeypatch.setattr(print_scheduler.printer_manager, "is_awaiting_plate_clear", lambda pid: False)
        lines = self._log(caplog, {2}, {2: "offline"})
        assert "offline" in lines[0]
        assert "now:" in lines[0]


class _NeverSleeps:
    """The scheduler module's ``asyncio``, with a ``sleep`` that fails the test: the pass must never wait for a boot.

    Replaces only ``print_scheduler``'s reference, never the asyncio module the event loop and fixtures share.
    """

    def __getattr__(self, name):
        import asyncio

        return getattr(asyncio, name)

    async def sleep(self, _seconds):
        raise AssertionError("the pass must never wait for a printer to boot")


class _Plug:
    def __init__(self, plug_id: int, name: str = "plug"):
        self.id = plug_id
        self.name = name
        self.auto_on = True
        self.enabled = True


class _Service:
    """A plug service that records what it was asked and checks the pass's transaction was ended first."""

    def __init__(self, db=None, *, reachable=True, state="OFF", switches=True):
        self.db = db
        self.reachable = reachable
        self.state = state
        self.switches = switches
        self.turned_on: list[int] = []

    async def get_status(self, plug):
        assert self.db is None or not self.db.in_transaction(), "the pass's transaction must end before plug I/O"
        return {"reachable": self.reachable, "state": self.state}

    async def turn_on(self, plug):
        self.turned_on.append(plug.id)
        return self.switches


class _Db:
    def __init__(self):
        self.open = True
        self.commits = 0

    def in_transaction(self):
        return self.open

    async def commit(self):
        self.open = False
        self.commits += 1


class TestTheWakeDoesNotBlockThePass:
    """G03 E-5: the per-printer wake switches the plug and goes on, like AutoQueue's wake.

    It used to wait up to ~3.5 min INSIDE the pass, on the pass's session: every printer after this one waited,
    PostgreSQL held a connection idle in transaction, and a printer that never came up cost that every pass.
    """

    @pytest.fixture
    def wake(self, monkeypatch):
        from unittest.mock import AsyncMock

        from backend.app.services import farm_startup_guard as guard_module

        clock = {"now": 1000.0}
        monkeypatch.setattr(print_scheduler.time, "monotonic", lambda: clock["now"])

        monkeypatch.setattr(print_scheduler, "asyncio", _NeverSleeps())
        switched = AsyncMock()
        monkeypatch.setattr("backend.app.services.smart_plug_manager.plug_switched", switched)
        monkeypatch.setattr(guard_module.farm_startup_guard, "allowed", lambda: True)

        def services(service):
            async def for_plug(_plug, _db):
                return service

            monkeypatch.setattr(print_scheduler.smart_plug_manager, "get_service_for_plug", for_plug)

        return SimpleNamespace(clock=clock, switched=switched, services=services)

    async def test_it_switches_the_plug_and_returns_booting(self, wake):
        scheduler = PrintScheduler()
        db = _Db()
        service = _Service(db)
        wake.services(service)

        assert await scheduler._begin_power_on(db, 7, _Plug(1)) == "booting"

        assert service.turned_on == [1]
        assert db.commits == 1
        wake.switched.assert_awaited_once()
        assert wake.switched.await_args.kwargs == {"scripts": "none"}
        assert scheduler._power_on_phase(7) == "booting"

    async def test_a_printer_that_never_connects_cools_off_then_is_tried_again(self, wake):
        scheduler = PrintScheduler()
        wake.services(_Service())
        await scheduler._begin_power_on(_Db(), 7, _Plug(1))

        wake.clock["now"] += scheduler._power_on_wait_time - 1
        assert scheduler._power_on_phase(7) == "booting"
        wake.clock["now"] += 2
        assert scheduler._power_on_phase(7) == "cooling_off"
        wake.clock["now"] += print_scheduler._POWER_ON_COOLOFF_SECONDS
        assert scheduler._power_on_phase(7) is None
        assert 7 not in scheduler._power_on_wakes

    async def test_an_unreachable_plug_cools_off_at_once(self, wake):
        scheduler = PrintScheduler()
        service = _Service(reachable=False)
        wake.services(service)

        assert await scheduler._begin_power_on(_Db(), 7, _Plug(1)) == "cooling_off"
        assert service.turned_on == []
        assert scheduler._power_on_phase(7) == "cooling_off"

    async def test_a_plug_that_will_not_switch_cools_off(self, wake):
        scheduler = PrintScheduler()
        wake.services(_Service(switches=False))

        assert await scheduler._begin_power_on(_Db(), 7, _Plug(1)) == "cooling_off"
        wake.switched.assert_not_awaited()

    async def test_a_plug_already_on_just_waits_for_the_boot(self, wake):
        scheduler = PrintScheduler()
        service = _Service(state="ON")
        wake.services(service)

        assert await scheduler._begin_power_on(_Db(), 7, _Plug(1)) == "booting"
        assert service.turned_on == []

    async def test_a_closed_farm_admission_switches_nothing_and_records_nothing(self, wake, monkeypatch):
        from backend.app.services import farm_startup_guard as guard_module

        monkeypatch.setattr(guard_module.farm_startup_guard, "allowed", lambda: False)
        scheduler = PrintScheduler()
        service = _Service()
        wake.services(service)

        assert await scheduler._begin_power_on(_Db(), 7, _Plug(1)) is None
        assert service.turned_on == []
        assert scheduler._power_on_wakes == {}

    async def test_a_connected_printer_ends_its_wake_and_its_other_plugs_follow(self, wake, monkeypatch):
        from unittest.mock import AsyncMock

        scheduler = PrintScheduler()
        service = _Service()
        wake.services(service)
        await scheduler._begin_power_on(_Db(), 7, _Plug(1))
        service.turned_on.clear()
        monkeypatch.setattr(scheduler, "_get_smart_plugs", AsyncMock(return_value=[_Plug(1), _Plug(2, "filter")]))

        await scheduler._finish_power_on(_Db(), 7)

        assert service.turned_on == [2], "only the plugs the wake did not switch"
        assert scheduler._power_on_wakes == {}

    def test_the_plug_that_feeds_the_printer_wakes_it_first(self):
        """A light's plug listed first must not be the one the wake switches (order is the heap's on PG)."""
        light, power, off, disabled = _Plug(1, "light"), _Plug(7, "power"), _Plug(3, "off"), _Plug(4, "disabled")
        light.controls_printer_power = False
        power.controls_printer_power = True
        off.auto_on = False
        disabled.enabled = False

        chosen = PrintScheduler._wake_plugs([light, off, disabled, power])

        assert [p.name for p in chosen] == ["power", "light"]

    def test_a_deleted_printer_takes_its_wake_with_it(self):
        from backend.app.services import printer_deletion

        entry = ("backend.app.services.print_scheduler", "scheduler._power_on_wakes")
        assert entry in printer_deletion._ID_KEYED
        assert isinstance(printer_deletion._container(*entry), dict)

    @pytest.mark.parametrize("deleted", [False, True], ids=["control", "deleted-during-the-plug-read"])
    async def test_a_wake_switches_only_the_record_it_started_for(self, wake, deleted):
        """The record's life is captured before the first await (spec gh109-o1 r2 R1): a printer deleted while
        its plug answered never has that plug switched for the printer created with its id."""
        from backend.app.services.printer_manager import printer_manager

        pid = 4848
        scheduler = PrintScheduler()

        class _DeletedMidRead(_Service):
            async def get_status(self, plug):
                if deleted:
                    life = printer_manager.begin_printer_deletion(pid)
                    printer_manager.end_printer_deletion(pid, life)
                return await super().get_status(plug)

        service = _DeletedMidRead()
        wake.services(service)

        result = await scheduler._begin_power_on(_Db(), pid, _Plug(1))

        if deleted:
            assert result is None and service.turned_on == []
            wake.switched.assert_not_awaited()
        else:
            assert result == "booting" and service.turned_on == [1]


@pytest.mark.integration
class TestThePassGoesOnWhileAPrinterBoots:
    """Through the real ``check_queue``: the woken printer sits the pass out; the next pass starts its job."""

    @pytest.fixture
    def scheduler(self, monkeypatch, db_session):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _session_ctx():
            yield db_session

        monkeypatch.setattr(print_scheduler, "async_session", _session_ctx)
        return PrintScheduler()

    async def test_one_wake_then_the_job_starts_once_the_printer_is_up(
        self, db_session, printer_factory, smart_plug_factory, scheduler, monkeypatch
    ):
        from unittest.mock import AsyncMock, patch

        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_queue import PrinterQueue

        printer = await printer_factory()
        db_session.add(PrinterQueue(id=printer.id, printer_id=printer.id))
        await smart_plug_factory(name="power", printer_id=printer.id)
        item = PrintQueueItem(queue_id=printer.id, status="pending", position=1)
        db_session.add(item)
        await db_session.commit()

        monkeypatch.setattr(print_scheduler, "asyncio", _NeverSleeps())
        service = _Service()

        async def for_plug(_plug, _db):
            return service

        monkeypatch.setattr(print_scheduler.smart_plug_manager, "get_service_for_plug", for_plug)
        monkeypatch.setattr("backend.app.services.smart_plug_manager.plug_switched", AsyncMock())
        connected = {"up": False}
        start = AsyncMock(return_value=False)
        with (
            patch.multiple(
                "backend.app.services.print_scheduler.printer_manager",
                is_connected=lambda pid: connected["up"],
                get_status=lambda pid: SimpleNamespace(state="IDLE", raw_data={}) if connected["up"] else None,
                is_awaiting_plate_clear=lambda pid: False,
            ),
            patch.object(scheduler, "_start_print", start),
            patch("backend.app.services.print_scheduler.preflight_item", AsyncMock(return_value=None)),
            patch.object(scheduler, "_check_auto_drying", AsyncMock()),
        ):
            await scheduler.check_queue()
            await db_session.refresh(item)
            assert item.waiting_reason == "Printer powering on"
            assert len(service.turned_on) == 1

            await scheduler.check_queue()
            assert len(service.turned_on) == 1, "a printer still booting is not switched again"
            start.assert_not_awaited()

            connected["up"] = True
            await scheduler.check_queue()

        start.assert_awaited_once()
        assert scheduler._power_on_wakes == {}
