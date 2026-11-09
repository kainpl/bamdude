"""Re-review r2: what main does with a catch-up the replacement client reissues on an IDLE frame.

Desired contract (spec §4 P1/P4, §5.2A): a recovery join exists only for a print in
flight. After the print has ended (completion handled, plate gate armed), a reconnect
must not adopt a phantom "printing" archive, claim the queue, or release the plate gate.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue


@pytest.mark.asyncio
@pytest.mark.parametrize("late_callback", [False, True])
async def test_reissued_catch_up_after_the_print_ended_adopts_nothing(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch, tmp_path, late_callback
):
    from backend.app import main
    from backend.app.core.config import settings as app_settings
    from backend.app.services import archive_colors, bambu_mqtt, plate_hold, usage_tracker
    from backend.app.services.printer_manager import printer_manager

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)

    p = await printer_factory()
    p.auto_archive, p.plate_detection_enabled, p.external_camera_enabled = True, False, False
    q = PrinterQueue(id=p.id, printer_id=p.id, status="idle")
    db_session.add(q)
    await db_session.commit()
    # A ran and was completed by its terminal before the reconnect.
    a = await archive_factory(
        printer_id=p.id, queue_id=q.id, status="completed", subtask_id="100", filename="job.3mf", print_name="job"
    )
    a.completed_at = datetime.now(timezone.utc)
    await db_session.commit()

    # MQTT: process-fresh join at PAUSE, catch-up queued; its seed has not landed when A ends.
    reissued = []
    old = bambu_mqtt.BambuMQTTClient(ip_address="192.0.2.1", serial_number="OLD", access_code="t")
    old.on_print_running_observed = MagicMock()
    old.on_print_complete = MagicMock()
    old._process_message({"print": {"gcode_state": "PAUSE", "gcode_file": "job.3mf", "subtask_id": "100"}})
    queued_payload = old.on_print_running_observed.call_args.args[0]
    old._process_message({"print": {"gcode_state": "RUNNING"}})
    old._process_message({"print": {"gcode_state": "FINISH"}})
    assert old.on_print_complete.call_count == 1
    new = bambu_mqtt.BambuMQTTClient(ip_address="192.0.2.1", serial_number="NEW", access_code="t")
    new.carry_print_lifecycle_from(old)
    new.on_print_running_observed = reissued.append
    new.on_print_complete = MagicMock()

    for name in (
        "_print_run_bindings",
        "_print_start_admissions",
        "_print_start_resolutions",
        "_pending_print_terminals",
        "_print_effect_generations",
        "_print_terminal_generations",
    ):
        monkeypatch.setattr(printer_manager, name, {}, raising=False)
    monkeypatch.setattr(printer_manager, "_clients", {p.id: new})
    monkeypatch.setattr(printer_manager, "_client_generations", {p.id: 2})
    monkeypatch.setattr(printer_manager, "_printer_lives", {p.id: 0})
    gate = {p.id}
    monkeypatch.setattr(printer_manager, "_awaiting_plate_clear", gate)
    monkeypatch.setattr(
        printer_manager, "set_awaiting_plate_clear", lambda pid, on: gate.add(pid) if on else gate.discard(pid)
    )
    monkeypatch.setattr(main, "_active_prints", {})
    monkeypatch.setattr(main, "_timelapse_baselines", {})
    for name in (
        "_restore_usage_tracking_session",
        "_seed_runout_detector_from_journal",
        "_capture_timelapse_baseline_at_start",
        "_record_energy_start",
        "maybe_register_external_stagger",
    ):
        monkeypatch.setattr(main, name, AsyncMock())
    monkeypatch.setattr(main, "spawn_background_task", lambda coro, **_k: coro.close())
    monkeypatch.setattr(main, "ws_manager", MagicMock(send_archive_created=AsyncMock(), broadcast=AsyncMock()))
    monkeypatch.setattr(main, "mqtt_relay", MagicMock(on_archive_created=AsyncMock()))
    monkeypatch.setattr(usage_tracker, "on_print_start", AsyncMock())
    monkeypatch.setattr(archive_colors, "apply_loaded_spool_colors", AsyncMock())
    clearing = AsyncMock()
    monkeypatch.setattr(plate_hold, "answer_by_clearing", clearing)

    # The replacement's first push: the printer is idle, the plate still holds A's part.
    new._process_message({"print": {"gcode_state": "IDLE", "gcode_file": "job.3mf"}})
    if late_callback:
        # Even a queued callback accepted by the current connection generation
        # cannot turn a terminal status into an adoption or release the plate.
        await main.on_print_running_observed(
            p.id, {**queued_payload, "_bamdude_client_generation": 2, "_bamdude_printer_life": 0}
        )
    # Terminal processing owns release/replay under the accounting lock. The
    # MQTT guard must cancel adoption without throwing this held evidence away.
    assert new.journal_held()

    async def terminal_impl(_pid, _data, _completion):
        assert not new.journal_held()  # the real wrapper releases it before terminal consumers

    complete = AsyncMock(side_effect=terminal_impl)
    monkeypatch.setattr(main, "_on_print_complete_impl", complete)
    monkeypatch.setattr("backend.app.services.queue_leftovers.retire_for_printer", AsyncMock())
    await main.on_print_complete(p.id, {"status": "completed", "subtask_id": "100"})
    complete.assert_awaited_once()
    assert not new.journal_held()
    for payload in reissued:
        await main.on_print_running_observed(
            p.id, {**payload, "_bamdude_client_generation": 2, "_bamdude_printer_life": 0}
        )
    await asyncio.sleep(0)

    async with factory() as witness:
        printing = list(
            await witness.scalars(
                select(PrintArchive).where(PrintArchive.printer_id == p.id, PrintArchive.status == "printing")
            )
        )
        claims = list(await witness.scalars(select(PrintQueueItem).where(PrintQueueItem.status == "printing")))
    facts = {
        "reissued_callbacks": len(reissued),
        "phantom_printing_archives": len(printing),
        "printing_claims": len(claims),
        "plate_gate_armed": p.id in gate,
        "answer_by_clearing_calls": clearing.await_count,
    }
    assert facts == {
        "reissued_callbacks": 0,
        "phantom_printing_archives": 0,
        "printing_claims": 0,
        "plate_gate_armed": True,
        "answer_by_clearing_calls": 0,
    }, facts
