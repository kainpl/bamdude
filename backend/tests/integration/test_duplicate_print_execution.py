"""First publication, never a later reconnect, retires proven noncurrent claims."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_claims import observe_claim
from backend.app.services.print_execution import ExecutionChanged, decide_start, publish_start
from backend.app.services.print_observation import PrintObservation
from backend.app.services.queue_ops import claim_lanes


async def setup(db, printer_factory, archive_factory):
    p = await printer_factory()
    q = PrinterQueue(id=p.id, printer_id=p.id, status="printing")
    db.add(q)
    await db.commit()
    a = await archive_factory(printer_id=p.id, queue_id=q.id, status="printing", subtask_id="100", file_path="")
    item = PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing", started_at=datetime.now(timezone.utc))
    db.add(item)
    await db.flush()
    q.current_item_id = item.id
    await db.commit()
    return p, q, a, item


@pytest.mark.asyncio
async def test_first_commit_closes_only_noncurrent_claim(db_session, printer_factory, archive_factory):
    db = db_session
    p, q, a, item = await setup(db, printer_factory, archive_factory)
    data = {
        "subtask_id": "200",
        "_print_observation": PrintObservation("different_client_run_id", "100", "200", "RUNNING", 2),
    }
    decision = await decide_start(db, p.id, data)
    b = PrintArchive(
        printer_id=p.id,
        queue_id=q.id,
        filename="same.3mf",
        file_path="",
        file_size=0,
        subtask_id="200",
        status="printing",
        extra_data={},
    )
    await publish_start(db, b, decision)
    assert a.status == "cancelled"
    assert a.extra_data["recovered_outcome_uncertain"] is True
    assert item.status == "cancelled"
    assert item.error_code == "noncurrent_execution_claim"
    assert q.is_paused
    rows = list((await db.scalars(select(PrintQueueItem).where(PrintQueueItem.status == "printing"))).all())
    assert len(rows) == 1 and rows[0].archive_id == b.id


@pytest.mark.asyncio
@pytest.mark.parametrize("intent_only", [False, True])
async def test_adopted_current_archive_also_retires_proven_noncurrent_execution(
    db_session, printer_factory, archive_factory, intent_only
):
    p, q, old, item = await setup(db_session, printer_factory, archive_factory)
    current = await archive_factory(printer_id=p.id, queue_id=q.id, status="printing", subtask_id="200", file_path="")
    if intent_only:
        current.subtask_id = None
        current.extra_data = {"dispatch_intent": {"version": 1, "submission_id": "200"}}
        await db_session.commit()
    decision = await decide_start(
        db_session,
        p.id,
        {
            "subtask_id": "200",
            "_print_observation": PrintObservation("different_client_run_id", "100", "200", "RUNNING", 1),
        },
    )
    assert decision.current_id == current.id
    result = await publish_start(db_session, current, decision)
    assert result.state == "created"
    assert old.status == item.status == "cancelled"
    assert current.status == "printing" and q.is_paused
    assert len((await db_session.scalars(select(PrintArchive))).all()) == 2


@pytest.mark.asyncio
async def test_db_difference_cannot_promote_unproven_client(db_session, printer_factory, archive_factory):
    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    data = {
        "subtask_id": "200",
        "_print_observation": PrintObservation("file_change_unproven", None, "200", "RUNNING", 0),
    }
    with pytest.raises(ExecutionChanged):
        await decide_start(db_session, p.id, data)
    assert a.status == item.status == "printing"
    assert q.current_item_id == item.id


@pytest.mark.asyncio
async def test_duplicate_claims_return_unresolved_without_third_row(db_session, printer_factory, archive_factory):
    db = db_session
    p, q, a, _ = await setup(db, printer_factory, archive_factory)
    db.add(PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing"))
    await db.commit()
    async with claim_lanes(db, [q.id], archive_id=a.id):
        result = await observe_claim(db, p.id, archive_id=a.id)
        await db.commit()
    assert result.state == "unresolved"
    rows = list((await db.scalars(select(PrintQueueItem).where(PrintQueueItem.queue_id == q.id))).all())
    assert len(rows) == 2
    assert q.is_paused


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_reconcile_duplicate_owners_books_once_but_never_releases_one(
    db_session, printer_factory, archive_factory, monkeypatch, count
):
    from backend.app import main
    from backend.app.services import print_reconciliation as reconcile, usage_tracker
    from backend.app.services.printer_manager import printer_manager

    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    db_session.add_all(PrintQueueItem(queue_id=q.id, archive_id=a.id, status="printing") for _ in range(count - 1))
    await db_session.commit()
    booking, library, gate = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(usage_tracker, "on_print_complete", booking)
    monkeypatch.setattr(main, "_bump_library_file_usage", library)
    monkeypatch.setattr(printer_manager, "arm_awaiting_plate_clear", gate)
    report = []
    assert (
        await reconcile._reconcile_complete_archive(db_session, a, status="completed", uncertain=False, report=report)
        == a.id
    )
    assert report == []
    gate.assert_not_called()
    assert q.is_paused
    assert (
        await reconcile._reconcile_complete_archive(db_session, a, status="completed", uncertain=False, report=report)
        is None
    )
    booking.assert_awaited_once()
    library.assert_awaited_once()


@pytest.mark.asyncio
async def test_matching_name_or_hash_without_run_proof_never_adopts(db_session, printer_factory, archive_factory):
    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    with pytest.raises(ExecutionChanged):
        await decide_start(
            db_session, p.id, {"subtask_id": "200", "filename": a.filename, "content_hash": a.content_hash}
        )


@pytest.mark.asyncio
async def test_dispatch_submission_is_durable_adoption_proof(db_session, printer_factory, archive_factory):
    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    a.subtask_id = None
    a.extra_data = {"dispatch_intent": {"version": 1, "submission_id": "200"}}
    await db_session.commit()
    decision = await decide_start(db_session, p.id, {"subtask_id": "200"})
    assert decision.current_id == a.id and decision.current_identity == a.recovery_identity
    assert not decision.old


@pytest.mark.asyncio
@pytest.mark.parametrize("same_identity", [True, False])
async def test_late_running_dispatch_recovers_only_its_prepared_uuid(
    db_session, printer_factory, archive_factory, same_identity
):
    from uuid import uuid4

    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    a.status, item.status, a.dispatch_phase = "failed", "failed", "sent"
    a.extra_data = {"dispatch_intent": {"version": 1, "submission_id": "100"}}
    await db_session.commit()
    decision = await decide_start(
        db_session,
        p.id,
        {"subtask_id": "100"},
        prepared_archive_id=a.id,
        prepared_archive_identity=a.recovery_identity if same_identity else str(uuid4()),
    )
    assert (decision.current_id == a.id) is same_identity


@pytest.mark.asyncio
async def test_unproven_start_without_live_archive_cannot_publish(db_session, printer_factory):
    p = await printer_factory()
    with pytest.raises(ExecutionChanged):
        await decide_start(
            db_session,
            p.id,
            {
                "subtask_id": "200",
                "_print_observation": PrintObservation("file_change_unproven", None, "200", "RUNNING", 0),
            },
        )


@pytest.mark.asyncio
async def test_terminal_archive_cannot_gain_a_new_active_claim(db_session, printer_factory, archive_factory):
    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    a.status, item.status = "completed", "completed"
    await db_session.commit()
    async with claim_lanes(db_session, [q.id], archive_id=a.id):
        result = await observe_claim(db_session, p.id, archive_id=a.id)
    assert result.state == "unresolved"


@pytest.mark.asyncio
async def test_real_client_unproven_db_difference_never_enters_new_start(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app import main
    from backend.app.services.bambu_mqtt import BambuMQTTClient
    from backend.app.services.printer_manager import printer_manager

    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    c = BambuMQTTClient(ip_address="192.0.2.1", serial_number="SYNTHETIC", access_code="test")
    c._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "a.3mf", "layer_num": 9}})
    c._runout_fired[1] = 1
    old_generation = c._journal_generation
    monkeypatch.setattr(printer_manager, "_clients", {p.id: c})
    monkeypatch.setattr(printer_manager, "_client_generations", {p.id: 1})
    monkeypatch.setattr(printer_manager, "_printer_lives", {p.id: 0})
    monkeypatch.setattr(printer_manager, "_unproven_starts", {}, raising=False)
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    start = AsyncMock()
    monkeypatch.setattr(main, "_on_print_start_impl", start)
    tasks = []
    c.on_print_start = lambda data: tasks.append(
        asyncio.create_task(
            main.on_print_start(p.id, {**data, "_bamdude_client_generation": 1, "_bamdude_printer_life": 0})
        )
    )
    c._process_message({"print": {"gcode_file": "b.3mf", "subtask_id": "200"}})
    await asyncio.gather(*tasks)
    start.assert_not_called()
    await db_session.refresh(a)
    await db_session.refresh(item)
    assert a.status == item.status == "printing"
    assert c._journal_generation == old_generation and c._runout_fired == {1: 1}
    for _ in range(3):
        c._process_message({"print": {"subtask_id": "200"}})
    assert c._last_known_run_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("new_id", ["200", None])
async def test_reconnect_boundary_publishes_and_retires_before_ftp(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch, new_id
):
    """Actual MQTT -> main -> DB; the downloader sees the first committed shape."""
    from unittest.mock import MagicMock

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app import main
    from backend.app.services import archive_download, bambu_mqtt, macro_trigger, usage_tracker
    from backend.app.services.printer_manager import printer_manager

    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    p.auto_archive, p.plate_detection_enabled, p.external_camera_enabled = True, False, False
    a.filename, a.print_name, a.print_time_seconds = "a.3mf", "a", 1
    await db_session.commit()
    prior = bambu_mqtt.BambuMQTTClient(ip_address="192.0.2.1", serial_number="OLD", access_code="test")
    prior._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "a.3mf", "subtask_id": "100"}})
    c = bambu_mqtt.BambuMQTTClient(ip_address="192.0.2.1", serial_number="NEW", access_code="test")
    c.carry_print_lifecycle_from(prior)
    c._runout_fired[1] = 1
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
    monkeypatch.setattr(macro_trigger, "fire_event_macros", AsyncMock())
    monkeypatch.setattr(usage_tracker, "on_print_start", AsyncMock())
    from backend.app.services.print_run_binding import (
        bind_print_run,
        claim_print_start,
        effect_generation,
        finish_print_start,
    )

    bind_print_run(
        printer_manager,
        printer_id=p.id,
        archive_id=a.id,
        observed_subtask_id="100",
        archive_identity=a.recovery_identity,
        origin="external",
    )
    old_admission = claim_print_start(printer_manager, p.id, {"subtask_id": "100"})
    finish_print_start(printer_manager, p.id, old_admission)
    old_effect = effect_generation(printer_manager, p.id)
    if new_id is None:
        c._process_message({"print": {"gcode_state": "IDLE"}})
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
    c._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "b.3mf", "subtask_id": new_id}})
    try:
        await asyncio.wait_for(at_ftp.wait(), 10)
        async with factory() as witness:
            old = await witness.get(PrintArchive, a.id)
            old_item = await witness.get(PrintQueueItem, item.id)
            live = list((await witness.scalars(select(PrintArchive).where(PrintArchive.status == "printing"))).all())
            assert len(live) == 1 and live[0].subtask_id == new_id
            assert effect_generation(printer_manager, p.id) > old_effect
            assert old.status == old_item.status == "cancelled"
            assert old.extra_data["recovered_outcome_uncertain"]
            assert old_item.error_code == "noncurrent_execution_claim"
            assert not c._runout_fired
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_replayed_noncurrent_marker_never_books_and_closes_own_claims(
    db_session, printer_factory, archive_factory, monkeypatch
):
    from backend.app import main
    from backend.app.services import print_reconciliation as reconcile, usage_tracker

    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    decision = await decide_start(
        db_session,
        p.id,
        {
            "subtask_id": "200",
            "_print_observation": PrintObservation("different_client_run_id", "100", "200", "RUNNING", 1),
        },
    )
    b = PrintArchive(
        printer_id=p.id,
        queue_id=q.id,
        filename="b.3mf",
        file_path="",
        file_size=0,
        subtask_id="200",
        status="printing",
        extra_data={},
    )
    await publish_start(db_session, b, decision)
    marker = dict(a.extra_data)
    a.status, a.completed_at, a.extra_data = "printing", None, marker
    item.status, item.completed_at = "printing", None
    q.current_item_id = item.id
    await db_session.commit()
    forbidden = AsyncMock(side_effect=AssertionError("noncurrent execution booked"))
    monkeypatch.setattr(usage_tracker, "on_print_complete", forbidden)
    monkeypatch.setattr(main, "_bump_library_file_usage", forbidden)
    assert await reconcile._reconcile(db_session, p.id, "FINISH", "b.3mf", "200") == []
    await db_session.refresh(a)
    await db_session.refresh(item)
    assert a.status == item.status == "cancelled"
    assert item.error_code == "noncurrent_execution_claim"
    assert q.current_item_id is None and q.status == "paused"
    forbidden.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_marker_cannot_cancel_claims(db_session, printer_factory, archive_factory):
    from backend.app.services.print_execution import close_noncurrent

    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    a.extra_data = {"noncurrent_execution": {"version": 1, "archive_identity": "not-this-archive"}}
    await db_session.commit()
    with pytest.raises(ExecutionChanged):
        await close_noncurrent(db_session, a)
    assert a.status == item.status == "printing"
    from backend.app.services.archive import ArchiveService

    assert not await ArchiveService(db_session).update_archive_status(
        a.id, "completed", expected_identity=a.recovery_identity
    )


@pytest.mark.asyncio
async def test_failure_during_claim_close_rolls_back_new_and_old_archives(db_session, printer_factory, archive_factory):
    from sqlalchemy import event

    p, q, a, item = await setup(db_session, printer_factory, archive_factory)
    old_id, item_id, printer_id = a.id, item.id, p.id
    decision = await decide_start(
        db_session,
        p.id,
        {
            "subtask_id": "200",
            "_print_observation": PrintObservation("different_client_run_id", "100", "200", "RUNNING", 1),
        },
    )
    new = PrintArchive(
        printer_id=p.id,
        queue_id=q.id,
        filename="b.3mf",
        file_path="",
        file_size=0,
        status="printing",
        subtask_id="200",
        extra_data={},
    )

    def unavailable(_mapper, _connection, target):
        if target.id == item_id and target.error_code == "noncurrent_execution_claim":
            raise RuntimeError("claim writer failed")

    event.listen(PrintQueueItem, "before_update", unavailable)
    try:
        with pytest.raises(RuntimeError, match="claim writer failed"):
            await publish_start(db_session, new, decision)
        await db_session.rollback()
    finally:
        event.remove(PrintQueueItem, "before_update", unavailable)
    old = await db_session.get(PrintArchive, old_id)
    item = await db_session.get(PrintQueueItem, item_id)
    assert old.status == item.status == "printing"
    assert "noncurrent_execution" not in (old.extra_data or {})
    assert len((await db_session.scalars(select(PrintArchive).where(PrintArchive.printer_id == printer_id))).all()) == 1


@pytest.mark.asyncio
async def test_lost_commit_acknowledgement_does_not_publish_a_second_execution(
    db_session, printer_factory, archive_factory, monkeypatch, test_engine
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.core import database

    monkeypatch.setattr(database, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    db = db_session
    p, q, a, item = await setup(db, printer_factory, archive_factory)
    data = {
        "subtask_id": "200",
        "_print_observation": PrintObservation("different_client_run_id", "100", "200", "RUNNING", 2),
    }
    decision = await decide_start(db, p.id, data)
    b = PrintArchive(
        printer_id=p.id,
        queue_id=q.id,
        filename="same.3mf",
        file_path="",
        file_size=0,
        subtask_id="200",
        status="printing",
        extra_data={},
    )
    commit = db.commit
    lost = [False]

    async def acknowledge():
        published = b.id is not None and a.status == "cancelled"
        await commit()
        if published and not lost[0]:
            lost[0] = True
            raise OSError("commit succeeded but its acknowledgement was lost")

    monkeypatch.setattr(db, "commit", acknowledge)
    published = await publish_start(db, b, decision)
    assert published
    assert lost[0]
    retry = await decide_start(db, p.id, data)
    assert retry.current_id == b.id and not retry.old
    result = await publish_start(db, b, retry)
    assert result.state == "owned"
    archives = list(await db.scalars(select(PrintArchive).order_by(PrintArchive.id)))
    assert [row.status for row in archives] == ["cancelled", "printing"]
    claims = list(await db.scalars(select(PrintQueueItem).where(PrintQueueItem.status == "printing")))
    assert len(claims) == 1 and claims[0].archive_id == b.id
