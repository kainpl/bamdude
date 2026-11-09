"""Re-review r2 (Q3): the lost-ack verification in print_execution.publish_start.

Desired contract (spec §5.3 "uncertain commit is reconciled by decision_identity and
immutable addresses"; invariant: "Втрату commit acknowledgement звіряють із доведеною
публікацією"): whether the publication is DURABLE is a fact of the database. A caller's
liveness predicate turning false after the commit must not turn a durable publication
into "publication not confirmed".

The catch-up callers pass still_current=recovery_current (main.py), which includes the
MQTT client generation: an ordinary reconnect flips it. Simulated here by flipping the
predicate inside the commit, after durability.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.print_execution import ExecutionChanged, decide_start, publish_start


async def _setup(db, printer_factory, archive_factory):
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
@pytest.mark.parametrize("invalidation", ["reconnect", "epoch"])
async def test_durable_publication_is_not_reported_unconfirmed_after_a_reconnect(
    db_session, printer_factory, archive_factory, monkeypatch, test_engine, invalidation
):
    from backend.app.core import database
    from backend.app.services import queue_sources

    monkeypatch.setattr(database, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    db = db_session
    p, q, a, item = await _setup(db, printer_factory, archive_factory)
    # catch-up path: no client observation, different durable ids (spec §5.2Б different_run_id)
    data = {"subtask_id": "200", "subtask_name": "b", "filename": "b.3mf"}
    decision = await decide_start(db, p.id, data, catch_up=True)
    assert [row[0] for row in decision.old] == [a.id]
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
    client_generation = {"value": 1}
    epoch = {"value": decision.epoch}
    monkeypatch.setattr(queue_sources, "current_epoch", lambda: epoch["value"])
    commit = db.commit
    lost = []

    async def commit_then_lose_ack():
        published = b.id is not None and a.status == "cancelled"
        await commit()
        if published and not lost:
            lost.append(True)
            if invalidation == "reconnect":
                client_generation["value"] = 2
            else:
                epoch["value"] += 1
            raise OSError("commit acknowledgement lost")

    monkeypatch.setattr(db, "commit", commit_then_lose_ack)
    outcome = None
    try:
        result = await publish_start(db, b, decision, still_current=lambda: client_generation["value"] == 1)
        assert result.current is False  # committed, but not authority to bind this callback
        outcome = "returned"
    except ExecutionChanged as exc:
        outcome = f"ExecutionChanged({exc})"
    async with database.async_session() as witness:
        durable_b = (await witness.scalars(select(PrintArchive).where(PrintArchive.subtask_id == "200"))).all()
        durable_a = await witness.get(PrintArchive, a.id)
        claims = (await witness.scalars(select(PrintQueueItem).where(PrintQueueItem.status == "printing"))).all()
    facts = {
        "lost_ack": lost == [True],
        "b_durable": len(durable_b) == 1,
        "a_closed": durable_a.status == "cancelled",
        "b_claim_durable": [c.archive_id for c in claims] == [b.id],
    }
    assert all(facts.values()), facts
    assert outcome == "returned", f"durable publication reported as {outcome}; facts {facts}"


@pytest.mark.asyncio
async def test_attempt_that_leaves_printing_after_the_proof_does_not_cost_b_its_archive(
    db_session, printer_factory, archive_factory
):
    """Foreign B starts while our E uploads; E's own dispatcher then fails/defers E (printer
    busy) during B's start effects, before B's publish. Desired: B is still published (E was
    never a candidate execution); current: 'candidate set changed' -> B gets no archive."""
    db = db_session
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
    e.dispatch_phase = "sent"
    await db.commit()
    from backend.app.services.print_observation import PrintObservation

    data = {"subtask_id": "5555", "_print_observation": PrintObservation("observed_new_start", None, "5555", "IDLE", 1)}
    decision = await decide_start(db, p.id, data)
    assert [x[0] for x in decision.attempts] == [e.id] and not decision.old
    e.status = "failed"  # its runner gave up: the printer refused the start (busy with B)
    await db.commit()
    b = PrintArchive(
        printer_id=p.id,
        queue_id=q.id,
        filename="b.3mf",
        file_path="",
        file_size=0,
        subtask_id="5555",
        status="printing",
        extra_data={},
    )
    outcome = "returned"
    try:
        await publish_start(db, b, decision)
    except ExecutionChanged as exc:
        outcome = f"ExecutionChanged({exc})"
        await db.rollback()
    assert outcome == "returned", outcome


@pytest.mark.asyncio
async def test_stale_committed_join_is_handed_to_the_replacement_without_replaying_start_effects(
    db_session, test_engine, printer_factory, archive_factory, monkeypatch
):
    from unittest.mock import AsyncMock

    from backend.app.services import bambu_mqtt, print_execution
    from backend.app.services.print_run_binding import current_print_run
    from backend.app.services.printer_manager import printer_manager
    from backend.tests.integration.test_duplicate_start_callbacks import _old_run, _start_harness

    p, _, a, _ = await _old_run(db_session, printer_factory, archive_factory)
    main, _, factory, _, _, _, macros = _start_harness(monkeypatch, db_session, test_engine, p, a)
    client = bambu_mqtt.BambuMQTTClient(ip_address="192.0.2.1", serial_number="JOIN", access_code="test")
    printer_manager._clients[p.id] = client
    monkeypatch.setattr(main, "spawn_background_task", lambda coro, **_kw: coro.close())
    monkeypatch.setattr(main, "_restore_usage_tracking_session", AsyncMock())

    async def seeded(*_args, **_kwargs):
        current = printer_manager.get_client(p.id)
        current.abandon_journal_hold(current.journal_generation())
        current.seed_runout_fired({})

    monkeypatch.setattr(main, "_seed_runout_detector_from_journal", seeded)
    fire = AsyncMock()
    monkeypatch.setattr(macros, "fire_event_macros", fire)
    publish = print_execution.publish_start
    replacements = []

    async def lose_ack(db, archive, decision, **kwargs):
        commit = db.commit

        async def acknowledge():
            published = archive.id is not None and not replacements
            await commit()
            if published:
                fresh = bambu_mqtt.BambuMQTTClient(ip_address="192.0.2.1", serial_number="NEW", access_code="test")
                fresh.carry_print_lifecycle_from(client)
                printer_manager._clients[p.id] = fresh
                printer_manager._client_generations[p.id] = 2
                replacements.append(fresh)
                raise OSError("commit acknowledgement lost across reconnect")

        monkeypatch.setattr(db, "commit", acknowledge)
        return await publish(db, archive, decision, **kwargs)

    monkeypatch.setattr(print_execution, "publish_start", lose_ack)
    payloads = []
    client.on_print_running_observed = payloads.append
    client._process_message({"print": {"gcode_state": "PAUSE", "gcode_file": "b.3mf", "subtask_id": "200"}})
    await main.on_print_running_observed(
        p.id, {**payloads.pop(), "_bamdude_client_generation": 1, "_bamdude_printer_life": 0}
    )
    fresh = replacements[0]
    assert fresh._inflight_recovery_pending  # recovery waits for an actual in-flight frame
    assert current_print_run(printer_manager, p.id).archive_id == a.id
    fresh.on_print_running_observed = payloads.append
    fresh._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "b.3mf", "subtask_id": "200"}})
    assert len(payloads) == 1
    await main.on_print_running_observed(
        p.id, {**payloads.pop(), "_bamdude_client_generation": 2, "_bamdude_printer_life": 0}
    )
    run = current_print_run(printer_manager, p.id)
    assert run.archive_id != a.id and run.observed_subtask_id == "200"
    async with factory() as witness:
        assert len((await witness.scalars(select(PrintArchive).where(PrintArchive.subtask_id == "200"))).all()) == 1
    fire.assert_not_awaited()
