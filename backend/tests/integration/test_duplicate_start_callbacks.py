"""Independent review r1 regressions for the real MQTT/start/dispatch chain."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_execution import StaleClaimRepeat, decide_start, publish_start, require_repeatable
from backend.app.services.print_observation import PrintObservation


async def _uploading_dispatch(db, printer_factory, archive_factory):
    """An attempt of ours mid-upload: archive_print wrote status=printing + intent, the queue row points at it."""
    p = await printer_factory()
    q = PrinterQueue(id=p.id, printer_id=p.id, status="printing")
    db.add(q)
    await db.commit()
    e = await archive_factory(
        printer_id=p.id,
        queue_id=q.id,
        status="printing",
        subtask_id=None,
        file_path="archive/1/e.3mf",
        extra_data={"dispatch_intent": {"version": 1, "submission_id": "777"}},
    )
    e.dispatch_phase = "prepared"
    row = PrintQueueItem(
        queue_id=q.id, archive_id=e.id, status="printing", origin="queue", started_at=datetime.now(timezone.utc)
    )
    db.add(row)
    await db.flush()
    q.current_item_id = row.id
    await db.commit()
    return p, q, e, row


@pytest.mark.asyncio
@pytest.mark.parametrize("live_id", ["5555", None])
@pytest.mark.parametrize("known_swap_fact", [False, True])
async def test_foreign_start_during_upload_preserves_our_unsent_job(
    db_session, printer_factory, archive_factory, live_id, known_swap_fact
):
    """F1: a print started at the printer while BamDude is still uploading E (no binding yet)."""
    db = db_session
    p, q, e, row = await _uploading_dispatch(db, printer_factory, archive_factory)
    if known_swap_fact:
        from backend.app.services.swap_compatibility import write_archive_fact

        write_archive_fact(e, False)
        await db.commit()
    data = {
        "subtask_id": live_id,
        "_print_observation": PrintObservation("observed_new_start", None, live_id, "IDLE", 1),
    }
    decision = await decide_start(db, p.id, data)
    # An unsent attempt stays distinct from the physically running B.
    assert not decision.old
    assert [attempt[0] for attempt in decision.attempts] == [e.id]
    b = PrintArchive(
        printer_id=p.id,
        queue_id=q.id,
        filename="b.3mf",
        file_path="",
        file_size=0,
        subtask_id=live_id,
        status="printing",
        extra_data={},
    )
    await publish_start(db, b, decision)
    await db.refresh(e)
    await db.refresh(row)
    assert e.status == "printing" and not e.extra_data.get("recovered_outcome_uncertain")
    assert row.status == "printing" and row.error_code is None
    # The queued job is still repeatable; its own dispatcher owns deferral.
    await require_repeatable(db, row)
    # A legacy unknown swap start fails conservatively but remains repeatable.
    from backend.app.services.filament_deferred import defer_claim

    result = await defer_claim(
        db,
        item_id=row.id,
        started_at=row.started_at,
        reason="dispatch_claim_changed",
        claim_detail="current_run_conflict",
        execution_archive_id=e.id,
    )
    await db.commit()
    await db.refresh(row)
    assert result
    assert row.status == ("pending" if known_swap_fact else "failed")
    assert row.error_code != "noncurrent_execution_claim"
    await require_repeatable(db, row)


def _start_harness(monkeypatch, db_session, test_engine, p, a):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app import main
    from backend.app.services import archive_download, bambu_mqtt, macro_trigger, usage_tracker
    from backend.app.services.printer_manager import printer_manager

    prior = bambu_mqtt.BambuMQTTClient(ip_address="192.0.2.1", serial_number="OLD", access_code="test")
    prior._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "a.3mf", "subtask_id": "100"}})
    c = bambu_mqtt.BambuMQTTClient(ip_address="192.0.2.1", serial_number="NEW", access_code="test")
    c.carry_print_lifecycle_from(prior)
    for name in (
        "_print_run_bindings",
        "_print_start_admissions",
        "_print_start_resolutions",
        "_pending_print_terminals",
        "_print_effect_generations",
        "_print_terminal_generations",
    ):
        monkeypatch.setattr(printer_manager, name, {}, raising=False)
    monkeypatch.setattr(printer_manager, "_clients", {p.id: c})
    monkeypatch.setattr(printer_manager, "get_printer", lambda pid: p if pid == p.id else None)
    monkeypatch.setattr(printer_manager, "_client_generations", {p.id: 1})
    monkeypatch.setattr(printer_manager, "_printer_lives", {p.id: 0})
    monkeypatch.setattr(printer_manager, "_unproven_starts", {}, raising=False)
    monkeypatch.setattr(main, "_active_prints", {})
    monkeypatch.setattr(main, "_expected_prints", {})
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    for name in (
        "notify_missing_spool_assignments_on_print_start",
        "_send_print_start_notification",
        "_record_energy_start",
        "_capture_timelapse_baseline_at_start",
    ):
        monkeypatch.setattr(main, name, AsyncMock())
    spoolman = AsyncMock()
    monkeypatch.setattr(main, "_store_spoolman_print_data", spoolman)
    monkeypatch.setattr(
        main,
        "ws_manager",
        MagicMock(
            send_print_start=AsyncMock(),
            send_archive_created=AsyncMock(),
            send_archive_updated=AsyncMock(),
            broadcast=AsyncMock(),
        ),
    )
    monkeypatch.setattr(main, "mqtt_relay", MagicMock(on_print_start=AsyncMock(), on_archive_created=AsyncMock()))
    monkeypatch.setattr(main.smart_plug_manager, "on_print_start", AsyncMock())
    monkeypatch.setattr(usage_tracker, "on_print_start", AsyncMock())
    from backend.app.services.print_run_binding import bind_print_run, claim_print_start, finish_print_start

    bind_print_run(
        printer_manager,
        printer_id=p.id,
        archive_id=a.id,
        observed_subtask_id="100",
        archive_identity=a.recovery_identity,
        origin="external",
    )
    finish_print_start(printer_manager, p.id, claim_print_start(printer_manager, p.id, {"subtask_id": "100"}))

    def reconnect():
        """What printer_manager.connect_printer does on a stale/dead session: a new client carrying the lifecycle."""
        fresh = bambu_mqtt.BambuMQTTClient(ip_address="192.0.2.1", serial_number="NEW2", access_code="test")
        fresh.carry_print_lifecycle_from(printer_manager._clients[p.id])
        printer_manager._clients[p.id] = fresh
        return fresh

    return main, c, factory, spoolman, reconnect, archive_download, macro_trigger


async def _old_run(db, printer_factory, archive_factory):
    p = await printer_factory()
    p.auto_archive, p.plate_detection_enabled, p.external_camera_enabled = True, False, False
    q = PrinterQueue(id=p.id, printer_id=p.id, status="printing")
    db.add(q)
    await db.commit()
    a = await archive_factory(
        printer_id=p.id, queue_id=q.id, status="printing", subtask_id="100", file_path="", filename="a.3mf"
    )
    item = PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing", started_at=datetime.now(timezone.utc))
    db.add(item)
    await db.flush()
    q.current_item_id = item.id
    await db.commit()
    return p, q, a, item


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect_mid_download", [False, True])
async def test_reconnect_during_start_download_preserves_attach_and_spoolman(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch, reconnect_mid_download
):
    """F3b: owns_start() now includes observation_is_current, which no carried client satisfies."""
    p, q, a, item = await _old_run(db_session, printer_factory, archive_factory)
    main, c, factory, spoolman, reconnect, archive_download, macro_trigger = _start_harness(
        monkeypatch, db_session, test_engine, p, a
    )
    monkeypatch.setattr(macro_trigger, "fire_event_macros", AsyncMock())
    at_ftp, release = asyncio.Event(), asyncio.Event()

    async def download(*_args, **_kwargs):
        at_ftp.set()
        await release.wait()
        return None

    monkeypatch.setattr(archive_download, "try_download_3mf", download)
    tasks = []
    c.on_print_start = lambda data: tasks.append(
        asyncio.create_task(
            main.on_print_start(p.id, {**data, "_bamdude_client_generation": 1, "_bamdude_printer_life": 0})
        )
    )
    c._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "b.3mf", "subtask_id": "200"}})
    await asyncio.wait_for(at_ftp.wait(), 10)
    if reconnect_mid_download:
        reconnect()
    release.set()
    await asyncio.gather(*tasks)
    async with factory() as witness:
        b = (await witness.scalars(select(PrintArchive).where(PrintArchive.subtask_id == "200"))).one()
    assert spoolman.await_count == 1
    assert (b.extra_data or {}).get("no_3mf_available") is True


@pytest.mark.asyncio
async def test_reconnect_before_publication_preserves_the_new_run(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    """F3a: a reconnect while start effects run (macros/plug/camera) abandons B; the carried client never re-fires."""
    p, q, a, item = await _old_run(db_session, printer_factory, archive_factory)
    main, c, factory, spoolman, reconnect, archive_download, macro_trigger = _start_harness(
        monkeypatch, db_session, test_engine, p, a
    )
    from backend.app.services.print_run_binding import effect_generation
    from backend.app.services.printer_manager import printer_manager

    fresh = []

    async def macros(*_args, **_kwargs):
        fresh.append(reconnect())

    monkeypatch.setattr(macro_trigger, "fire_event_macros", macros)
    monkeypatch.setattr(archive_download, "try_download_3mf", AsyncMock(return_value=None))
    before = effect_generation(printer_manager, p.id)
    tasks = []
    c.on_print_start = lambda data: tasks.append(
        asyncio.create_task(
            main.on_print_start(p.id, {**data, "_bamdude_client_generation": 1, "_bamdude_printer_life": 0})
        )
    )
    c._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "b.3mf", "subtask_id": "200"}})
    await asyncio.gather(*tasks)
    assert fresh, "the macro step ran"
    new_client = fresh[0]
    calls = []
    new_client.on_print_start = lambda data: calls.append(("start", data))
    new_client.on_print_running_observed = lambda data: calls.append(("running", data))
    for layer in (10, 11, 12):
        new_client._process_message(
            {"print": {"gcode_state": "RUNNING", "gcode_file": "b.3mf", "subtask_id": "200", "layer_num": layer}}
        )
    async with factory() as witness:
        archives = list(await witness.scalars(select(PrintArchive).where(PrintArchive.printer_id == p.id)))
        claims = list(await witness.scalars(select(PrintQueueItem).where(PrintQueueItem.status == "printing")))
        queue = await witness.get(PrinterQueue, q.id)
    assert [x.subtask_id for x in archives] == ["100", "200"]
    assert archives[0].status == "cancelled" and [x.archive_id for x in claims] == [archives[1].id]
    assert calls == []  # nothing ever delivers B again in this process
    assert effect_generation(printer_manager, p.id) > before  # A's effects were already fenced
    assert queue.is_paused and printer_manager._unproven_starts == {}


@pytest.mark.asyncio
async def test_refusal_after_admission_pauses_and_explains(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    """F2: publish_start refusals in _on_print_start_impl run after claim_print_start and start effects."""
    p, q, a, item = await _old_run(db_session, printer_factory, archive_factory)
    main, c, factory, spoolman, reconnect, archive_download, macro_trigger = _start_harness(
        monkeypatch, db_session, test_engine, p, a
    )
    from backend.app.services.print_execution import ExecutionChanged
    from backend.app.services.print_run_binding import effect_generation
    from backend.app.services.printer_manager import printer_manager

    effects = []

    async def macros(event, *_args, **_kwargs):
        effects.append(event)
        # Another writer changes the candidate set while start effects run (operator stop, sweep, etc.).
        async with factory() as other:
            row = await other.get(PrintArchive, a.id)
            row.status = "cancelled"
            await other.commit()

    monkeypatch.setattr(macro_trigger, "fire_event_macros", macros)
    before = effect_generation(printer_manager, p.id)
    tasks = []
    c.on_print_start = lambda data: tasks.append(
        asyncio.create_task(
            main.on_print_start(p.id, {**data, "_bamdude_client_generation": 1, "_bamdude_printer_life": 0})
        )
    )
    c._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "b.3mf", "subtask_id": "200"}})
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert results == [None]
    assert effects == ["print_started"]
    assert effect_generation(printer_manager, p.id) > before
    async with factory() as witness:
        archives = list(await witness.scalars(select(PrintArchive).where(PrintArchive.subtask_id == "200")))
        queue = await witness.get(PrinterQueue, q.id)
    assert archives == []  # the printing run has no archive and no claim
    assert queue.is_paused and printer_manager._unproven_starts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "archive_fields,data",
    [
        # Zero-id catch-up of an attached multi-plate archive (attach rewrote filename + print_name).
        (
            {"print_name": "Benchy - Plate 2", "filename": "Benchy_plate_2.gcode.3mf", "subtask_id": None},
            {"subtask_name": "Benchy_plate_2", "filename": "/data/Metadata/plate_2.gcode", "subtask_id": "0"},
        ),
        # The archive never learned an id (first start frame lacked it); the printer reports one after restart.
        (
            {"print_name": "cube", "filename": "cube.3mf", "subtask_id": None},
            {"subtask_name": "cube", "filename": "cube.3mf", "subtask_id": "4242"},
        ),
    ],
)
async def test_sole_live_archive_uses_the_same_recovery_matcher(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch, archive_fields, data
):
    """F5: on_print_running_observed finds A (unambiguous_name) and then decide_start(catch_up) refuses it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app import main
    from backend.app.services.print_execution import ExecutionChanged
    from backend.app.services.printer_manager import printer_manager

    p = await printer_factory()
    a = await archive_factory(printer_id=p.id, status="printing", file_path="archive/x.3mf", **archive_fields)
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setattr(printer_manager, "_print_run_bindings", {}, raising=False)
    live = await main._live_archive_for_running_print(p.id, data)
    assert (live.archive_id, live.reason) == (a.id, "unambiguous_name")
    decision = await decide_start(db_session, p.id, data, catch_up=True)
    assert decision.current_id == a.id and not decision.old


@pytest.mark.asyncio
async def test_foreign_start_upload_conflict_through_mqtt_callback_and_dispatch_deferral(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    from backend.app.services import swap_compatibility
    from backend.app.services.filament_deferred import defer_claim
    from backend.app.services.print_run_binding import bind_prepared_print_run, current_print_run
    from backend.app.services.printer_manager import printer_manager

    p, q, e, row = await _uploading_dispatch(db_session, printer_factory, archive_factory)
    p.auto_archive, p.plate_detection_enabled, p.external_camera_enabled = True, False, False
    swap_compatibility.write_archive_fact(e, False)
    await db_session.commit()
    main, c, factory, _, _, download, macros = _start_harness(monkeypatch, db_session, test_engine, p, e)
    printer_manager._print_run_bindings.clear()
    printer_manager._print_start_admissions.clear()
    monkeypatch.setattr(macros, "fire_event_macros", AsyncMock())
    monkeypatch.setattr(download, "try_download_3mf", AsyncMock(return_value=None))
    c._process_message({"print": {"gcode_state": "IDLE", "gcode_file": ""}})
    c._process_message({"print": {"gcode_state": "PREPARE", "gcode_file": "b.3mf", "subtask_id": "5555"}})
    tasks = []
    c.on_print_start = lambda data: tasks.append(
        asyncio.create_task(
            main.on_print_start(p.id, {**data, "_bamdude_printer_life": 0, "_bamdude_client_generation": 1})
        )
    )
    c._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "b.3mf", "subtask_id": "5555"}})
    await asyncio.gather(*tasks)
    run = current_print_run(printer_manager, p.id)
    assert run is not None and run.archive_id != e.id
    assert (
        bind_prepared_print_run(
            printer_manager,
            printer_id=p.id,
            archive_id=e.id,
            archive_identity=e.recovery_identity,
            queue_item_id=row.id,
            claim_started_at=row.started_at,
            expected_submission_id="777",
        )
        is None
    )
    async with factory() as db:
        result = await defer_claim(
            db,
            item_id=row.id,
            started_at=row.started_at,
            reason="dispatch_claim_changed",
            claim_detail="current_run_conflict",
            execution_archive_id=e.id,
        )
        await db.commit()
        assert result
    await db_session.refresh(row)
    await db_session.refresh(e)
    assert row.status == "pending" and row.error_code != "noncurrent_execution_claim"
    assert not (e.extra_data or {}).get("recovered_outcome_uncertain")
    assert current_print_run(printer_manager, p.id) == run
    await db_session.refresh(q)
    current = await db_session.get(PrintQueueItem, q.current_item_id)
    assert q.status == "printing" and current is not None and current.archive_id == run.archive_id


@pytest.mark.asyncio
async def test_an_attempt_accepted_after_the_proof_must_be_reclassified(db_session, printer_factory, archive_factory):
    from backend.app.services.print_execution import ExecutionChanged

    p, q, e, _ = await _uploading_dispatch(db_session, printer_factory, archive_factory)
    data = {"subtask_id": "5555", "_print_observation": PrintObservation("observed_new_start", None, "5555", "IDLE", 1)}
    decision = await decide_start(db_session, p.id, data)
    assert decision.attempts
    e.dispatch_phase = "accepted"
    await db_session.commit()
    b = PrintArchive(
        printer_id=p.id,
        queue_id=q.id,
        filename="b.3mf",
        file_path="",
        file_size=0,
        status="printing",
        subtask_id="5555",
    )
    with pytest.raises(ExecutionChanged, match="accepted"):
        await publish_start(db_session, b, decision)
    await db_session.rollback()
    assert not (await db_session.scalars(select(PrintArchive).where(PrintArchive.subtask_id == "5555"))).all()


@pytest.mark.asyncio
async def test_foreign_claim_refuses_before_start_admission_and_effects(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    from backend.app.services.printer_manager import printer_manager

    p, q, a, item = await _old_run(db_session, printer_factory, archive_factory)
    other = await printer_factory()
    other_queue = PrinterQueue(id=other.id, printer_id=other.id, status="printing")
    db_session.add(other_queue)
    await db_session.flush()
    db_session.add(PrintQueueItem(queue_id=other_queue.id, archive_id=a.id, status="printing"))
    await db_session.commit()
    main, c, _, _, _, _, macros = _start_harness(monkeypatch, db_session, test_engine, p, a)
    fire = AsyncMock()
    monkeypatch.setattr(macros, "fire_event_macros", fire)
    tasks = []
    c.on_print_start = lambda data: tasks.append(
        asyncio.create_task(
            main.on_print_start(p.id, {**data, "_bamdude_printer_life": 0, "_bamdude_client_generation": 1})
        )
    )
    c._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "b.3mf", "subtask_id": "200"}})
    await asyncio.gather(*tasks)
    fire.assert_not_awaited()
    assert printer_manager._unproven_starts[p.id]["reason"] == "foreign claim"
    await db_session.refresh(q)
    assert q.is_paused


@pytest.mark.asyncio
async def test_lost_publication_ack_binds_b_and_accepts_its_terminal(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    from backend.app.services import print_execution
    from backend.app.services.print_run_binding import current_print_run
    from backend.app.services.printer_manager import printer_manager

    p, q, a, _ = await _old_run(db_session, printer_factory, archive_factory)
    main, c, factory, _, _, download, macros = _start_harness(monkeypatch, db_session, test_engine, p, a)
    monkeypatch.setattr(macros, "fire_event_macros", AsyncMock())
    monkeypatch.setattr(download, "try_download_3mf", AsyncMock(return_value=None))
    publish = print_execution.publish_start
    lost = []

    async def publication(db, archive, decision, **kwargs):
        commit = db.commit

        async def acknowledge():
            should_lose = archive.id is not None and not lost
            await commit()
            if should_lose:
                lost.append(True)
                raise OSError("commit acknowledgement lost")

        monkeypatch.setattr(db, "commit", acknowledge)
        return await publish(db, archive, decision, **kwargs)

    monkeypatch.setattr(print_execution, "publish_start", publication)
    tasks = []
    c.on_print_start = lambda data: tasks.append(
        asyncio.create_task(
            main.on_print_start(p.id, {**data, "_bamdude_printer_life": 0, "_bamdude_client_generation": 1})
        )
    )
    c._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "b.3mf", "subtask_id": "200"}})
    await asyncio.gather(*tasks)
    assert lost == [True]
    run = current_print_run(printer_manager, p.id)
    assert run is not None and run.archive_id != a.id and run.observed_subtask_id == "200"
    assert (
        await main._accept_bound_terminal_run(p.id, run, {"status": "completed", "subtask_id": "200"}) == run.archive_id
    )
    async with factory() as db:
        assert len((await db.scalars(select(PrintArchive).where(PrintArchive.subtask_id == "200"))).all()) == 1


@pytest.mark.asyncio
async def test_failed_publication_commit_leaves_an_explained_hold(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    from backend.app.services import print_execution
    from backend.app.services.printer_manager import printer_manager

    p, q, a, _ = await _old_run(db_session, printer_factory, archive_factory)
    main, c, factory, _, _, download, macros = _start_harness(monkeypatch, db_session, test_engine, p, a)
    monkeypatch.setattr(macros, "fire_event_macros", AsyncMock())
    monkeypatch.setattr(download, "try_download_3mf", AsyncMock(return_value=None))
    publish = print_execution.publish_start
    failed = []

    async def publication(db, archive, decision, **kwargs):
        commit = db.commit

        async def fail_before_commit():
            if archive.id is not None and not failed:
                failed.append(True)
                raise OSError("commit refused before durability")
            await commit()

        monkeypatch.setattr(db, "commit", fail_before_commit)
        return await publish(db, archive, decision, **kwargs)

    monkeypatch.setattr(print_execution, "publish_start", publication)
    tasks = []
    c.on_print_start = lambda data: tasks.append(
        asyncio.create_task(
            main.on_print_start(p.id, {**data, "_bamdude_printer_life": 0, "_bamdude_client_generation": 1})
        )
    )
    c._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "b.3mf", "subtask_id": "200"}})
    await asyncio.gather(*tasks)
    assert failed == [True]
    assert printer_manager._unproven_starts[p.id]["reason"] == "publication not confirmed"
    async with factory() as db:
        queue = await db.get(PrinterQueue, q.id)
        assert queue.is_paused
        assert not (await db.scalars(select(PrintArchive).where(PrintArchive.subtask_id == "200"))).all()
