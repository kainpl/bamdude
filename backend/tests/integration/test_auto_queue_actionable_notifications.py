"""Notification policy through real routing/placement ticks and a real database."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.settings import Settings
from backend.app.services import auto_queue_eligibility as eligibility, auto_queue_scheduler as scheduler_module
from backend.tests.integration.queue_test_database import test_engine as test_engine
from backend.tests.integration.test_auto_queue_scheduler import (
    _idle_status,
    _make_printer_with_queue,
    _patch_printer_manager,
    routing_item as routing_item,
    scheduler as scheduler,
)


@pytest.fixture
def sent(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr("backend.app.services.notification_service.notification_service.on_queue_job_waiting", spy)
    return spy


async def test_busy_for_many_ticks_is_visible_without_notifications(
    db_session, printer_factory, scheduler, routing_item, sent
):
    printer, queue = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    queue.status = "printing"
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    one, two = _patch_printer_manager({printer.id})
    with one, two:
        for _ in range(8):
            await scheduler.tick()
    assert item.waiting_reason and item.status == "pending"
    sent.assert_not_awaited()


async def test_busy_cycle_and_removal_of_one_shared_contributor_do_not_rearm_a_mismatch(
    db_session, printer_factory, scheduler, routing_item, sent
):
    printer, queue = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    items = [routing_item(target_model="A1MINI", status="pending", position=i) for i in (1, 2)]
    db_session.add_all(items)
    await db_session.commit()
    states = {printer.id: _idle_status(["PETG"])}
    one, two = _patch_printer_manager({printer.id}, states)
    with one, two:
        await scheduler.tick()
        assert sent.await_count == 1
        queue.status = "printing"
        await db_session.commit()
        await scheduler.tick()
        items[0].status = "cancelled"
        await db_session.commit()
        await scheduler.tick()
        queue.status = "idle"
        await db_session.commit()
        await scheduler.tick()
    assert sent.await_count == 1, "busy is no proof that the remaining job's material problem was resolved"


async def test_offline_alert_does_not_claim_an_existing_material_incident_was_resolved(
    db_session, printer_factory, scheduler, routing_item, sent
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    connected = {printer.id}
    states = {printer.id: _idle_status(["PETG"])}
    one, two = _patch_printer_manager(connected, states)
    with one, two:
        await scheduler.tick()
        assert sent.await_count == 1
        connected.clear()
        await scheduler.tick()
        assert sent.await_count == 2  # a real offline observation remains actionable
        connected.add(printer.id)
        await scheduler.tick()
        assert sent.await_count == 2  # the material was never proved corrected


async def test_known_offline_material_shortage_does_not_power_on_a_printer(
    db_session, printer_factory, smart_plug_factory, scheduler, routing_item, sent, monkeypatch
):
    from backend.app.services.offline_feed import OfflineFeed, OfflineSource

    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    await smart_plug_factory(printer_id=printer.id, auto_on=True)
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    monkeypatch.setattr(
        "backend.app.services.offline_feed.OfflineFeedCache.get",
        AsyncMock(return_value=OfflineFeed((OfflineSource("ams", "PETG", None),), ams_known=True, external_known=True)),
    )
    service = AsyncMock()
    monkeypatch.setattr("backend.app.services.smart_plug_manager.smart_plug_manager.get_service_for_plug", service)
    one, two = _patch_printer_manager(set())
    with one, two:
        await scheduler.tick()
    service.assert_not_awaited()
    assert sent.await_count == 1
    assert "cannot run this job" in sent.await_args.kwargs["waiting_reason"]


@pytest.mark.parametrize("code", ["incompatible_target", "compatible_fallback_disabled"])
async def test_permanent_placement_refusal_never_becomes_printer_busy(
    db_session, printer_factory, scheduler, routing_item, sent, monkeypatch, code
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    monkeypatch.setattr(scheduler, "_assign", AsyncMock(side_effect=scheduler_module.AutoPlacementConflict(code)))
    one, two = _patch_printer_manager({printer.id})
    with one, two:
        await scheduler.tick()
    assert sent.await_count == 1
    assert "occupied" not in item.waiting_reason.lower()
    assert code not in item.waiting_reason  # operator text, not an internal code


@pytest.mark.parametrize("failure", ["digest", "projection", "rebalance"])
async def test_notification_failure_cannot_rollback_placement_or_hide_its_invalidation(
    test_engine, routing_item, monkeypatch, failure
):
    from sqlalchemy import func, text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue
    from backend.tests.integration.test_auto_queue_scheduler import _test_models

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        printers = [
            Printer(
                name=model,
                model=model,
                serial_number=f"AUDIT-{model}",
                ip_address="192.0.2.1",
                access_code="00000000",
                is_active=True,
            )
            for model in ("A1MINI", "P1S")
        ]
        db.add_all(printers)
        await db.flush()
        ids = [p.id for p in printers]
        for printer in printers:
            _test_models[printer.id] = printer.model
            db.add(PrinterQueue(id=printer.id, printer_id=printer.id))
        db.add_all(
            [
                routing_item(target_model=model, status="pending", position=i)
                for i, model in enumerate(("A1MINI", "P1S"))
            ]
        )
        if failure == "rebalance":
            db.add(Settings(key=scheduler_module.REBALANCE_SETTING_KEY, value="true"))
        await db.commit()
    scheduler = scheduler_module.AutoQueueScheduler()
    scheduler._active_waiting_facts = {("previous-complete-observation",)}
    monkeypatch.setattr(scheduler_module, "async_session", factory)
    announced = AsyncMock()
    monkeypatch.setattr(scheduler_module.ws_manager, "send_queue_changed", announced)

    async def prove_commit():
        async with factory() as reader:
            assert await reader.scalar(select(func.count()).select_from(PrintQueueItem)) == 1

    if failure == "digest":

        async def broken_notice(*_args):
            await prove_commit()
            raise RuntimeError("synthetic digest failure")

        monkeypatch.setattr(scheduler, "_waiting_notice", broken_notice)
    elif failure == "projection":
        fetch = scheduler._fetch_pending
        calls = 0

        async def broken_read(db, *args):
            nonlocal calls
            calls += 1
            if calls == 2:
                await prove_commit()
                await db.execute(text("SELECT * FROM absent_notification_projection"))
            return await fetch(db, *args)

        monkeypatch.setattr(scheduler, "_fetch_pending", broken_read)
    else:

        async def broken_rebalance(db, **_kwargs):
            await prove_commit()
            await db.execute(text("SELECT * FROM absent_rebalance_projection"))

        monkeypatch.setattr(scheduler_module.queue_rebalance, "rebalance", broken_rebalance)
    states = {ids[0]: _idle_status(["PETG"]), ids[1]: _idle_status(["PLA"])}
    one, two = _patch_printer_manager(set(ids), states)
    with one, two:
        await scheduler.tick()
    await prove_commit()
    assert scheduler._active_waiting_facts == {("previous-complete-observation",)}
    announced.assert_awaited_once_with(ids[1])


async def test_digest_uses_location_names_localized_fallbacks_and_a_shown_representative(
    db_session, scheduler, monkeypatch
):
    from backend.app.models.printer_location import PrinterLocation
    from backend.app.services.auto_queue_waiting import fact

    room = PrinterLocation(name="Цех", name_key="цех")
    db_session.add(room)
    await db_session.flush()
    shelf = PrinterLocation(name="Полиця", name_key="полиця", parent_id=room.id)
    db_session.add(shelf)
    await db_session.commit()
    shown = SimpleNamespace(id=99, target_model=None, target_location_id=shelf.id)
    omitted = SimpleNamespace(id=1, target_model=None, target_location_id=None)
    current = {
        ("cause", i): (shown if i < 5 else omitted, fact("material_mismatch", i, message="Причина")) for i in range(6)
    }
    monkeypatch.setattr("backend.app.i18n._current_language", "uk")
    monkeypatch.setattr(scheduler, "_job_name", AsyncMock(side_effect=lambda _db, item: f"job-{item.id}"))
    notice = await scheduler._waiting_notice(db_session, current, set(current))
    assert notice.job_name == "job-99"
    assert notice.target_model == "будь-яка модель принтера"
    assert "Цех / Полиця" in notice.waiting_reason
    assert "Ще 1 перешкода." in notice.waiting_reason
    assert "any model" not in notice.waiting_reason and f"#{shelf.id}" not in notice.waiting_reason


async def test_permutation_rename_and_copies_do_not_create_incidents(
    db_session, printer_factory, scheduler, routing_item, sent, monkeypatch
):
    p1, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI", name="First")
    p2, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI", name="Second")
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    original = eligibility.printers_for_item
    reversed_order = False

    async def printers(*args, **kwargs):
        rows, model, suffix = await original(*args, **kwargs)
        return (list(reversed(rows)) if reversed_order else rows), model, suffix

    monkeypatch.setattr(eligibility, "printers_for_item", printers)
    states = {p1.id: _idle_status(["PETG"]), p2.id: _idle_status(["PETG"])}
    one, two = _patch_printer_manager({p1.id, p2.id}, states)
    with one, two:
        await scheduler.tick()
        reversed_order = True
        await scheduler.tick()
        p1.name = "Renamed"
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=2))
        await db_session.commit()
        await scheduler.tick()
    assert sent.await_count == 1


async def test_actionable_incident_survives_other_jobs_placement(
    db_session, printer_factory, scheduler, routing_item, sent
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="P1S")
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    db_session.add(routing_item(target_model="P1S", status="pending", position=2))
    await db_session.commit()
    connected = {printer.id}
    one, two = _patch_printer_manager(connected)
    with one, two:
        await scheduler.tick()
        assert sent.await_count == 1, "placed work must not silence another model's problem"
        another, _ = await _make_printer_with_queue(db_session, printer_factory, model="P1S")
        connected.add(another.id)
        db_session.add(routing_item(target_model="P1S", status="pending", position=3))
        await db_session.commit()
        await scheduler.tick()
        await scheduler.tick()
    assert sent.await_count == 1
    rows = (await db_session.scalars(select(AutoQueueItem).order_by(AutoQueueItem.position))).all()
    assert [item.status for item in rows] == ["pending", "assigned", "assigned"]


async def test_busy_first_item_is_not_the_actionable_message(
    db_session, printer_factory, scheduler, routing_item, sent
):
    printer, queue = await _make_printer_with_queue(db_session, printer_factory, model="P1S")
    queue.status = "printing"
    db_session.add(routing_item(target_model="P1S", status="pending", position=1))
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=2))
    await db_session.commit()
    one, two = _patch_printer_manager({printer.id})
    with one, two:
        await scheduler.tick()
    assert sent.await_count == 1
    assert "A1 Mini" in sent.await_args.kwargs["waiting_reason"]
    assert "occupied" not in sent.await_args.kwargs["waiting_reason"].lower()


async def test_incomplete_rebalance_does_not_publish_or_forget_a_snapshot(
    db_session, printer_factory, scheduler, routing_item, sent, monkeypatch
):
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    await scheduler.tick()
    assert sent.await_count == 1
    db_session.add(routing_item(target_model="P1S", status="pending", position=2))
    db_session.add(Settings(key=scheduler_module.REBALANCE_SETTING_KEY, value="true"))
    await db_session.commit()
    monkeypatch.setattr(scheduler_module.queue_rebalance, "rebalance", AsyncMock(side_effect=RuntimeError("test")))
    await scheduler.tick()
    assert sent.await_count == 1, "a partial pass cannot send its new facts"


async def test_locale_telemetry_resolution_empty_queue_and_restart(
    db_session, printer_factory, scheduler, routing_item, sent, monkeypatch
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    states = {printer.id: _idle_status(["PETG"])}
    one, two = _patch_printer_manager({printer.id}, states)
    with one, two:
        await scheduler.tick()
        assert sent.await_count == 1
        monkeypatch.setattr("backend.app.i18n._current_language", "uk")
        states[printer.id].raw_data["ams"][0]["tray"][0]["remain"] = 1
        await scheduler.tick()
        assert sent.await_count == 1
        states[printer.id] = _idle_status(["ASA"])
        await scheduler.tick()
        assert sent.await_count == 1, "an unrelated wrong material is still the same missing PLA"
        await db_session.delete(item)
        await db_session.commit()
        await scheduler.tick()
        assert not scheduler._active_waiting_facts
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
        await db_session.commit()
        await scheduler.tick()
        assert sent.await_count == 2
        scheduler.stop()
        assert not scheduler._active_waiting_facts
        await scheduler.tick()
        assert sent.await_count == 3, "one event after process restart is permitted"


async def test_commit_failure_does_not_publish_or_advance_seen_facts(
    db_session, scheduler, routing_item, sent, monkeypatch
):
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    await scheduler.tick()
    previous = set(scheduler._active_waiting_facts)
    db_session.add(routing_item(target_model="P1S", status="pending", position=2))
    await db_session.commit()
    original = db_session.commit
    monkeypatch.setattr(db_session, "commit", AsyncMock(side_effect=RuntimeError("commit refused")))
    with pytest.raises(RuntimeError, match="commit refused"):
        await scheduler.tick()
    assert scheduler._active_waiting_facts == previous
    assert sent.await_count == 1
    monkeypatch.setattr(db_session, "commit", original)
    await db_session.rollback()
    await scheduler.tick()
    assert sent.await_count == 2
    assert "P1S" in sent.await_args.kwargs["waiting_reason"]
    assert "A1 Mini" not in sent.await_args.kwargs["waiting_reason"], "only new facts belong in the digest"


async def test_delivery_failure_is_not_retried_and_send_follows_commit(db_session, scheduler, routing_item, sent):
    async def unavailable(**kwargs):
        assert not db_session.in_transaction(), "provider I/O must not hold the placement transaction"
        raise RuntimeError("provider unavailable")

    sent.side_effect = unavailable
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    await scheduler.tick()
    await scheduler.tick()
    assert sent.await_count == 1
    db_session.add(routing_item(target_model="P1S", status="pending", position=2))
    await db_session.commit()
    await scheduler.tick()
    assert sent.await_count == 2


async def test_rebalance_removal_and_new_rows_use_the_final_pending_projection(
    db_session, scheduler, routing_item, sent, monkeypatch
):
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    db_session.add(Settings(key=scheduler_module.REBALANCE_SETTING_KEY, value="true"))
    await db_session.commit()
    once = True

    async def rebalance(db, **kwargs):
        nonlocal once
        if once:
            item.status = "cancelled"
            db.add(routing_item(target_model="P1S", status="pending", position=2))
            once = False
        return SimpleNamespace(converted=0)

    monkeypatch.setattr(scheduler_module.queue_rebalance, "rebalance", rebalance)
    await scheduler.tick()
    sent.assert_not_awaited()
    await scheduler.tick()
    assert sent.await_count == 1
    assert "P1S" in sent.await_args.kwargs["waiting_reason"]
    assert "A1 Mini" not in sent.await_args.kwargs["waiting_reason"]


@pytest.mark.parametrize("kind", ["manual", "scheduled", "cancelled", "failed", "deleted"])
async def test_due_set_changes_resolve_seen_incidents(db_session, scheduler, routing_item, sent, kind):
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    await scheduler.tick()
    assert scheduler._active_waiting_facts
    if kind == "manual":
        item.manual_start = True
    elif kind == "scheduled":
        item.scheduled_time = datetime.now(timezone.utc) + timedelta(days=1)
    elif kind == "deleted":
        await db_session.delete(item)
    else:
        item.status = kind
    await db_session.commit()
    await scheduler.tick()
    assert not scheduler._active_waiting_facts
    assert sent.await_count == 1


async def test_many_models_emit_one_bounded_digest(db_session, scheduler, routing_item, sent):
    for position, model in enumerate(["A1MINI", "P1S", "X1C", "H2D", "H2S", "H2C", "A1"]):
        db_session.add(routing_item(target_model=model, status="pending", position=position))
    await db_session.commit()
    await scheduler.tick()
    assert sent.await_count == 1
    payload = sent.await_args.kwargs
    assert len(payload["waiting_reason"].splitlines()) == 6
    assert "2" in payload["waiting_reason"].splitlines()[-1]
    assert len(payload["waiting_reason"]) < 1900
    assert payload["target_model"] == "multiple printer models"


async def test_failed_read_does_not_erase_seen_facts(db_session, scheduler, routing_item, sent, monkeypatch):
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    await scheduler.tick()
    previous = set(scheduler._active_waiting_facts)
    with monkeypatch.context() as patch:
        patch.setattr(scheduler, "_fetch_pending", AsyncMock(side_effect=OSError("read failed")))
        with pytest.raises(OSError):
            await scheduler.tick()
    assert scheduler._active_waiting_facts == previous
    await scheduler.tick()
    assert sent.await_count == 1


async def test_stop_during_commit_does_not_repopulate_seen_facts(
    db_session, scheduler, routing_item, sent, monkeypatch
):
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    original = db_session.commit

    async def commit():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(db_session, "commit", commit)
    task = asyncio.create_task(scheduler.tick())
    await asyncio.wait_for(entered.wait(), 5)
    scheduler.stop()
    release.set()
    await asyncio.wait_for(task, 5)
    assert not scheduler._active_waiting_facts
    sent.assert_not_awaited()


@pytest.mark.parametrize("powered_on", [True, False])
async def test_only_a_successful_wake_suppresses_that_printers_offline_fact(
    db_session, printer_factory, smart_plug_factory, scheduler, routing_item, sent, monkeypatch, powered_on
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    await smart_plug_factory(printer_id=printer.id, auto_on=True)
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    scheduler._wake_cooldowns = {}
    now = 100.0
    monkeypatch.setattr(scheduler, "_wake_now", lambda: now, raising=False)
    switch = AsyncMock(return_value=powered_on)
    monkeypatch.setattr(
        "backend.app.services.smart_plug_manager.smart_plug_manager.get_service_for_plug",
        AsyncMock(return_value=SimpleNamespace(turn_on=switch)),
    )
    one, two = _patch_printer_manager(set())
    with one, two:
        await scheduler.tick()
        assert sent.await_count == int(not powered_on)
        await scheduler.tick()
        assert sent.await_count == int(not powered_on), "a confirmed wake has the same bounded grace on later ticks"
        now += scheduler._WAKE_COOLDOWN_SECONDS + 1
        await scheduler.tick()
        await scheduler.tick()
        assert sent.await_count == 1, "another ON cannot renew the grace forever while the printer remains offline"
    assert switch.await_count == 2


@pytest.mark.parametrize("controllable", [False, True])
async def test_meter_is_ignored_by_autoqueue_wake(
    db_session, printer_factory, smart_plug_factory, scheduler, routing_item, sent, monkeypatch, controllable
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    await smart_plug_factory(printer_id=printer.id, plug_type="mqtt", auto_on=True)
    if controllable:
        relay = await smart_plug_factory(
            printer_id=printer.id, plug_type="mqtt", mqtt_command_topic="mains/set", auto_on=True
        )
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    switch = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "backend.app.services.smart_plug_manager.smart_plug_manager.get_service_for_plug",
        AsyncMock(return_value=SimpleNamespace(turn_on=switch)),
    )
    one, two = _patch_printer_manager(set())
    with one, two:
        await scheduler.tick()
    if controllable:
        switch.assert_awaited_once_with(relay)
        sent.assert_not_awaited()
    else:
        switch.assert_not_awaited()
        sent.assert_awaited_once()


async def test_one_failed_plug_does_not_skip_the_other_plugs_or_claim_success(
    db_session, printer_factory, smart_plug_factory, scheduler, routing_item, sent, monkeypatch
):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    plugs = [await smart_plug_factory(name=f"power-{i}", printer_id=printer.id, auto_on=True) for i in range(3)]
    db_session.add(routing_item(target_model="A1MINI", status="pending", position=1))
    await db_session.commit()
    attempted = []

    async def turn_on(plug):
        attempted.append(plug.id)
        if plug.id == plugs[1].id:
            raise OSError("one outlet unavailable")
        return plug.id == plugs[2].id

    monkeypatch.setattr(
        "backend.app.services.smart_plug_manager.smart_plug_manager.get_service_for_plug",
        AsyncMock(return_value=SimpleNamespace(turn_on=turn_on)),
    )
    one, two = _patch_printer_manager(set())
    with one, two:
        await scheduler.tick()
    assert attempted == [p.id for p in plugs]
    assert sent.await_count == 1  # failed wake has no grace


async def test_failed_on_keeps_the_existing_one_wake_attempt_per_pass(
    db_session, printer_factory, smart_plug_factory, scheduler, routing_item, sent, monkeypatch
):
    for _ in range(2):
        printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
        await smart_plug_factory(printer_id=printer.id, auto_on=True)
    for position in range(2):
        db_session.add(routing_item(target_model="A1MINI", status="pending", position=position))
    await db_session.commit()
    scheduler._wake_cooldowns = {}
    switch = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "backend.app.services.smart_plug_manager.smart_plug_manager.get_service_for_plug",
        AsyncMock(return_value=SimpleNamespace(turn_on=switch)),
    )
    one, two = _patch_printer_manager(set())
    with one, two:
        await scheduler.tick()
    switch.assert_awaited_once()
    assert sent.await_count == 1


@pytest.mark.parametrize("reason", ["source_check_busy", "source_timeout"])
async def test_source_wait_or_failure_has_no_duplicate_waiting_event(
    db_session, printer_factory, scheduler, routing_item, sent, monkeypatch, reason
):
    from backend.app.services.filament_requirements import PrintRequirements

    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    monkeypatch.setattr(
        eligibility,
        "read_item_requirements",
        AsyncMock(return_value=PrintRequirements(status="unavailable", reason=reason)),
    )
    one, two = _patch_printer_manager({printer.id})
    with one, two:
        await scheduler.tick()
    sent.assert_not_awaited()
    assert item.status == ("failed" if reason == "source_timeout" else "pending")


async def test_swap_exclusion_does_not_claim_a_printer_is_busy(db_session, printer_factory, routing_item):
    printer, _ = await _make_printer_with_queue(db_session, printer_factory, model="A1MINI")
    item = routing_item(target_model="A1MINI", status="pending", position=1)
    db_session.add(item)
    await db_session.commit()
    one, two = _patch_printer_manager({printer.id})
    with one, two:
        result = await eligibility.find_eligible_printer(
            db_session, item, {printer.id}, exclusion_reasons={printer.id: "swap_request_refused"}
        )
    assert result.facts[0].code == "swap_request_refused"
    assert not result.facts[0].actionable
    assert "occupied" not in result.reason.lower()


@pytest.mark.parametrize("language", ["en", "uk"])
async def test_provider_selection_custom_template_and_no_backfill(
    db_session, scheduler, routing_item, monkeypatch, language
):
    from backend.app.models.notification import NotificationProvider
    from backend.app.models.notification_template import NotificationTemplate
    from backend.app.services.locale_updater import _update_notification_templates
    from backend.app.services.notification_service import NotificationService

    custom = NotificationTemplate(
        event_type="queue_job_waiting",
        name="Own",
        title_template="Custom",
        body_template="OWN {job_name} {target_model} {waiting_reason}",
        is_default=False,
    )
    provider = NotificationProvider(
        name="Isolated", provider_type="ntfy", config="{}", enabled=False, subscribed_events=["on_queue_job_waiting"]
    )
    db_session.add_all([custom, provider, routing_item(target_model="A1MINI", status="pending", position=1)])
    await _update_notification_templates(db_session, language)
    await db_session.commit()
    assert custom.body_template == "OWN {job_name} {target_model} {waiting_reason}"
    service = NotificationService()
    delivery = AsyncMock()
    monkeypatch.setattr(service, "_send_to_providers", delivery)
    monkeypatch.setattr(
        "backend.app.services.notification_service.notification_service.on_queue_job_waiting",
        service.on_queue_job_waiting,
    )
    await scheduler.tick()
    assert delivery.await_count == 1  # The in-app inbox remains an existing channel.
    assert provider not in delivery.await_args.args[0]
    provider.enabled = True
    await db_session.commit()
    await scheduler.tick()
    assert delivery.await_count == 1, "enabling a provider must not backfill a seen incident"
    db_session.add(routing_item(target_model="P1S", status="pending", position=2))
    await db_session.commit()
    await scheduler.tick()
    assert delivery.await_count == 2
    args = delivery.await_args.args
    assert [item.id for item in args[0] if isinstance(item, NotificationProvider)] == [provider.id]
    assert args[1] == "Custom" and args[2].startswith("OWN ")
    assert args[4] == "queue_job_waiting"
