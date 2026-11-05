"""Observations follow a receipt by immutable identities only (OrcaBamDude host spec r2.2 §11 S3).

A connector's receipt names each job by (kind, id, identity). What happened to the job afterwards -- its promotion,
its executions, their outcome -- is read by identities: the AutoQueue job's ``recovery_identity``, the queue row's
``recovery_identity`` or ``source_auto_identity``, the archive's ``queue_item_identity`` / ``source_auto_identity``.
A numeric id is never followed, because SQLite hands a deleted row's id to the next row.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select, update

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.connector_submission import ConnectorSubmission, ConnectorSubmissionEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.services import connector_submissions as cs, job_lineage
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_connector_submissions import (  # noqa: F401 - fixtures
    _body,
    _clean_spool_state,
    _printer_entry,
    farm,
    sessions,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _publish(farm, *entries):
    body = _body(farm, *entries)
    registered = await cs.register(farm["actor"], body)
    await cs.run(registered.submission_id, farm["actor"])
    return body.operation_id, registered.submission_id


async def _observe(farm, operation_id, submission_id, cursor=None):
    proof = await cs.prove_own(farm["actor"], operation_id, farm["server_id"], farm["epoch"])
    assert proof.submission_id == submission_id
    return await cs.observations(farm["actor"], proof, cursor=cursor)


async def _rows(sessions, model):
    async with sessions() as db:
        return (await db.execute(select(model).order_by(model.id))).scalars().all()


async def _execute(sessions, row, printer_id, *, status="printing", **kwargs):
    """What the dispatcher does: an archive for this run, stamped from the launched row."""
    async with sessions() as db:
        archive = PrintArchive(
            printer_id=printer_id,
            filename="part.gcode.3mf",
            file_path="archive/part.gcode.3mf",
            file_size=1,
            status=status,
            **kwargs,
        )
        job_lineage.stamp_execution(archive, row)
        db.add(archive)
        await db.commit()
        await db.refresh(archive)
        return archive


def _single(farm):
    return _printer_entry(farm, quantity=1)


async def test_a_queued_direct_job_names_its_printer(farm, sessions):
    operation_id, submission_id = await _publish(farm, _single(farm))
    [row] = await _rows(sessions, PrintQueueItem)

    observed = await _observe(farm, operation_id, submission_id)

    [job] = observed["jobs"]
    assert job["job"] == {"kind": "queue", "identity": row.recovery_identity}
    assert job["state"] == "queued"
    assert job["printer"] == {"id": farm["printer"].id, "identity": farm["printer"].connector_identity}
    assert job["executions"] == [] and job["executions_complete"] is True
    assert "assignment" not in job
    assert observed["next_cursor"] is None


async def test_a_running_execution_proves_its_first_layer_from_the_live_printer(farm, sessions, monkeypatch):
    operation_id, submission_id = await _publish(farm, _single(farm))
    [row] = await _rows(sessions, PrintQueueItem)
    async with sessions() as db:
        await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == row.id).values(status="printing"))
        await db.commit()
    archive = await _execute(sessions, row, farm["printer"].id, dispatch_phase="accepted")
    live = SimpleNamespace(state="RUNNING", layer_num=0)
    monkeypatch.setattr(printer_manager, "get_status", lambda printer_id: live)
    _bind(monkeypatch, archive.id, farm["printer"].id)

    before = (await _observe(farm, operation_id, submission_id))["jobs"][0]
    live.layer_num = 2
    after = (await _observe(farm, operation_id, submission_id))["jobs"][0]
    monkeypatch.setattr(printer_manager, "get_status", lambda printer_id: None)
    unknown = (await _observe(farm, operation_id, submission_id))["jobs"][0]

    assert before["state"] == "printing"
    assert before["executions"][0]["identity"] == archive.recovery_identity
    assert before["executions"][0]["first_layer"] is False
    assert after["executions"][0]["first_layer"] is True
    assert unknown["executions"][0]["first_layer"] is None


async def test_a_finished_job_is_read_from_its_execution_after_the_row_is_gone(farm, sessions):
    operation_id, submission_id = await _publish(farm, _single(farm))
    [row] = await _rows(sessions, PrintQueueItem)
    archive = await _execute(sessions, row, farm["printer"].id, status="completed")
    async with sessions() as db:  # the plate was cleared: the completed row is auto-cleaned
        await db.execute(delete(PrintQueueItem).where(PrintQueueItem.id == row.id))
        await db.commit()

    [job] = (await _observe(farm, operation_id, submission_id))["jobs"]

    assert job["state"] == "completed"
    assert job["executions"][0] == {
        "identity": archive.recovery_identity,
        "state": "completed",
        "started_at": None,
        "completed_at": None,
        "first_layer": None,  # no observation of a layer was ever made; a status label proves none
    }
    assert job["printer"]["id"] == farm["printer"].id


async def test_a_reused_id_never_lends_another_job_its_print(farm, sessions):
    operation_id, submission_id = await _publish(farm, _single(farm))
    [row] = await _rows(sessions, PrintQueueItem)
    async with sessions() as db:  # our row goes away without a print; a stranger takes its number
        await db.execute(delete(PrintQueueItem).where(PrintQueueItem.id == row.id))
        stranger = PrintQueueItem(
            id=row.id, queue_id=row.queue_id, queue_source_id=row.queue_source_id, status="printing"
        )
        db.add(stranger)
        await db.commit()
        await db.refresh(stranger)
    assert stranger.id == row.id and stranger.recovery_identity != row.recovery_identity
    await _execute(sessions, stranger, farm["printer"].id, status="completed")

    [job] = (await _observe(farm, operation_id, submission_id))["jobs"]

    assert job["state"] == "unavailable" and job["reason"] == "chain_broken"
    assert job["executions"] == [] and job["printer"] is None


async def test_executions_are_newest_first_and_bounded(farm, sessions):
    operation_id, submission_id = await _publish(farm, _single(farm))
    [row] = await _rows(sessions, PrintQueueItem)
    start = datetime(2026, 11, 5, 12, 0, 0)
    made = [
        await _execute(sessions, row, farm["printer"].id, status="failed", created_at=start + timedelta(minutes=n))
        for n in range(4)
    ]

    [job] = (await _observe(farm, operation_id, submission_id))["jobs"]

    assert [e["identity"] for e in job["executions"]] == [a.recovery_identity for a in reversed(made)][:3]
    assert job["executions_complete"] is False
    assert len({e["identity"] for e in job["executions"]}) == 3


async def test_an_auto_job_names_each_assignment_by_its_own_identity(farm, sessions):
    entry = {
        "entry_id": str(uuid.uuid4()),
        "plate_id": 15,
        "quantity": 1,
        "target": {"kind": "auto", "target_model": "P1P"},
        "action": "queue_asap",
    }
    operation_id, submission_id = await _publish(farm, entry)
    [auto] = await _rows(sessions, AutoQueueItem)

    waiting = (await _observe(farm, operation_id, submission_id))["jobs"][0]

    assert waiting["job"] == {"kind": "auto", "identity": auto.recovery_identity}
    assert waiting["state"] == "queued" and waiting["assignment"] is None

    async def promote():
        async with sessions() as db:
            row = PrintQueueItem(
                queue_id=farm["queue"].id,
                queue_source_id=auto.queue_source_id,
                status="pending",
                source_auto_item_id=auto.id,
                source_auto_identity=auto.recovery_identity,
            )
            db.add(row)
            await db.execute(update(AutoQueueItem).where(AutoQueueItem.id == auto.id).values(status="assigned"))
            await db.commit()
            await db.refresh(row)
            return row

    first = await promote()
    assigned = (await _observe(farm, operation_id, submission_id))["jobs"][0]
    async with sessions() as db:  # the operator moves it back: same AutoQueue row re-armed, the printer row goes
        await db.execute(delete(PrintQueueItem).where(PrintQueueItem.id == first.id))
        await db.execute(update(AutoQueueItem).where(AutoQueueItem.id == auto.id).values(status="pending"))
        await db.commit()
    second = await promote()  # promoted again -- to the same printer
    reassigned = (await _observe(farm, operation_id, submission_id))["jobs"][0]

    assert assigned["state"] == "assigned"
    assert assigned["assignment"] == {
        "identity": first.recovery_identity,
        "printer": {"id": farm["printer"].id, "identity": farm["printer"].connector_identity},
    }
    assert reassigned["state"] == "assigned"
    assert reassigned["assignment"]["identity"] == second.recovery_identity != first.recovery_identity


async def test_an_auto_job_finishes_through_its_lineage_after_its_rows_are_gone(farm, sessions):
    entry = {
        "entry_id": str(uuid.uuid4()),
        "plate_id": 15,
        "quantity": 1,
        "target": {"kind": "auto", "target_model": "P1P"},
        "action": "queue_asap",
    }
    operation_id, submission_id = await _publish(farm, entry)
    [auto] = await _rows(sessions, AutoQueueItem)
    row = PrintQueueItem(
        recovery_identity=str(uuid.uuid4()), source_auto_identity=auto.recovery_identity, queue_id=farm["queue"].id
    )
    archive = await _execute(sessions, row, farm["printer"].id, status="completed")
    async with sessions() as db:  # the AutoQueue row is deleted once its print finished
        await db.execute(delete(AutoQueueItem).where(AutoQueueItem.id == auto.id))
        await db.commit()

    [job] = (await _observe(farm, operation_id, submission_id))["jobs"]

    assert job["state"] == "completed"
    assert job["executions"][0]["identity"] == archive.recovery_identity
    assert job["assignment"] is None


async def test_a_direct_job_moved_into_the_auto_queue_is_unavailable(farm, sessions):
    operation_id, submission_id = await _publish(farm, _single(farm))
    [row] = await _rows(sessions, PrintQueueItem)
    async with sessions() as db:  # the transfer deletes the printer row; its new AutoQueue ancestor is a new job
        await db.execute(delete(PrintQueueItem).where(PrintQueueItem.id == row.id))
        db.add(AutoQueueItem(target_model="P1P", position=1, queue_source_id=row.queue_source_id))
        await db.commit()

    [job] = (await _observe(farm, operation_id, submission_id))["jobs"]

    assert (job["state"], job["reason"]) == ("unavailable", "chain_broken")


async def test_a_receipt_without_identities_is_unavailable_and_stays_published(farm, sessions):
    operation_id, submission_id = await _publish(farm, _single(farm))
    async with sessions() as db:
        entry = await db.scalar(select(ConnectorSubmissionEntry))
        receipt = dict(entry.published_receipt)
        receipt.pop("jobs")
        entry.published_receipt = receipt
        await db.commit()

    [job] = (await _observe(farm, operation_id, submission_id))["jobs"]
    result = await cs.result(farm["actor"], operation_id)

    assert job["job"]["identity"] is None
    assert (job["state"], job["reason"]) == ("unavailable", "legacy_receipt")
    assert result.entries[0].state == "published"


async def test_a_printer_outside_the_keys_list_is_not_shown(farm, sessions):
    operation_id, submission_id = await _publish(farm, _single(farm))
    from backend.app.models.api_key import APIKey

    async with sessions() as db:
        await db.execute(
            update(APIKey).where(APIKey.id == farm["key"].id).values(printer_ids=[farm["printer"].id + 99])
        )
        await db.commit()

    [job] = (await _observe(farm, operation_id, submission_id))["jobs"]

    assert (job["state"], job["reason"], job["printer"]) == ("unavailable", "out_of_scope", None)


async def test_a_large_operation_is_paged_and_its_tail_is_reachable(farm, sessions):
    operation_id, submission_id = await _publish(farm, _printer_entry(farm, quantity=150))
    rows = await _rows(sessions, PrintQueueItem)

    first = await _observe(farm, operation_id, submission_id)
    second = await _observe(farm, operation_id, submission_id, cursor=first["next_cursor"])

    assert len(first["jobs"]) == cs.OBSERVATION_PAGE == 100 and first["next_cursor"]
    assert len(second["jobs"]) == 50 and second["next_cursor"] is None
    seen = [j["job"]["identity"] for j in first["jobs"] + second["jobs"]]
    assert seen == [r.recovery_identity for r in rows]
    assert len(json.dumps(first).encode()) < cs.OBSERVATION_MAX_BYTES


async def test_a_foreign_or_damaged_cursor_is_refused(farm, sessions):
    operation_id, submission_id = await _publish(farm, _printer_entry(farm, quantity=150))
    other_id, other_submission = await _publish(farm, _single(farm))
    cursor = (await _observe(farm, operation_id, submission_id))["next_cursor"]

    for bad in (cursor[:-3], "not-a-cursor", "e30"):
        with pytest.raises(cs.ConnectorRefusal) as refused:
            await _observe(farm, operation_id, submission_id, cursor=bad)
        assert (refused.value.status, refused.value.code) == (400, "invalid_cursor")
    with pytest.raises(cs.ConnectorRefusal) as foreign:
        await _observe(farm, other_id, other_submission, cursor=cursor)
    assert (foreign.value.status, foreign.value.code) == (400, "invalid_cursor")


async def test_a_restore_between_the_gate_and_the_answer_is_refused(farm, sessions):
    operation_id, submission_id = await _publish(farm, _single(farm))
    async with sessions() as db:  # what a restore does to the namespace while a read is under way
        await cs.rotate_storage_epoch(db)
        await db.commit()

    with pytest.raises(cs.ConnectorRefusal) as refused:
        await _observe(farm, operation_id, submission_id)

    assert (refused.value.status, refused.value.code) == (409, "context_changed")


# --------------------------------------------------------------------------- #
# H0 review r1 (R1, R2): run identity for first_layer, and no foreign operation through a restore window
# --------------------------------------------------------------------------- #


def _bind(monkeypatch, archive_id, printer_id, *, origin="dispatch", generation=7, live_generation=7):
    """Install the run binding the dispatcher would have made, and the client generation now connected."""
    from backend.app.services import print_run_binding

    run = print_run_binding.PrintRunBinding(
        printer_id=printer_id, archive_id=archive_id, origin=origin, client_generation=generation
    )
    monkeypatch.setattr(print_run_binding, "current_print_run", lambda manager, pid: run if pid == printer_id else None)
    monkeypatch.setattr(printer_manager, "current_client_generation", lambda pid: live_generation)


async def test_first_layer_needs_a_proven_binding_to_this_very_execution(farm, sessions, monkeypatch):
    operation_id, submission_id = await _publish(farm, _single(farm))
    [row] = await _rows(sessions, PrintQueueItem)
    archive = await _execute(sessions, row, farm["printer"].id, dispatch_phase="accepted")
    live = SimpleNamespace(state="RUNNING", layer_num=0)
    monkeypatch.setattr(printer_manager, "get_status", lambda printer_id: live)

    _bind(monkeypatch, archive.id, farm["printer"].id)
    at_zero = (await _observe(farm, operation_id, submission_id))["jobs"][0]["executions"][0]["first_layer"]
    live.layer_num = 3
    past_one = (await _observe(farm, operation_id, submission_id))["jobs"][0]["executions"][0]["first_layer"]
    _bind(monkeypatch, archive.id, farm["printer"].id, origin="observed")
    unproven = (await _observe(farm, operation_id, submission_id))["jobs"][0]["executions"][0]["first_layer"]
    _bind(monkeypatch, archive.id + 1000, farm["printer"].id)
    other_run = (await _observe(farm, operation_id, submission_id))["jobs"][0]["executions"][0]["first_layer"]
    _bind(monkeypatch, archive.id, farm["printer"].id, live_generation=8)
    reconnected = (await _observe(farm, operation_id, submission_id))["jobs"][0]["executions"][0]["first_layer"]

    assert (at_zero, past_one) == (False, True)
    assert unproven is None and other_run is None and reconnected is None


async def test_a_prepared_execution_borrows_no_layer_and_has_not_started(farm, sessions, monkeypatch):
    """The reviewer's case: an older print runs at layer 42 while our newer archive is only prepared."""
    operation_id, submission_id = await _publish(farm, _single(farm))
    [row] = await _rows(sessions, PrintQueueItem)
    now = datetime(2026, 11, 5, 14)
    async with sessions() as db:
        other = PrintArchive(
            printer_id=farm["printer"].id,
            filename="physical-other.3mf",
            file_path="archive/physical-other.3mf",
            file_size=1,
            status="printing",
            created_at=now,
            dispatch_phase="accepted",
        )
        db.add(other)
        await db.commit()
        await db.refresh(other)
    archive = await _execute(
        sessions, row, farm["printer"].id, created_at=now + timedelta(seconds=1), dispatch_phase="prepared"
    )
    monkeypatch.setattr(printer_manager, "get_status", lambda _: SimpleNamespace(state="RUNNING", layer_num=42))
    _bind(monkeypatch, other.id, farm["printer"].id)

    [execution] = (await _observe(farm, operation_id, submission_id))["jobs"][0]["executions"]

    assert execution["identity"] == archive.recovery_identity
    assert execution["state"] == "dispatching" and execution["first_layer"] is None


async def test_a_completed_label_is_no_first_layer_observation(farm, sessions, monkeypatch):
    operation_id, submission_id = await _publish(farm, _single(farm))
    [row] = await _rows(sessions, PrintQueueItem)
    await _execute(sessions, row, farm["printer"].id, status="completed", dispatch_phase="prepared")
    monkeypatch.setattr(printer_manager, "get_status", lambda _: None)

    [execution] = (await _observe(farm, operation_id, submission_id))["jobs"][0]["executions"]

    assert execution["state"] == "completed" and execution["first_layer"] is None


async def _replace_under(sessions, submission_id):
    """What a replaced database looks like at an await: the key kept, the namespace rotated, and the numeric
    submission address now a different key's different operation."""
    foreign = str(uuid.uuid4())
    async with sessions() as db:
        epoch = await cs.rotate_storage_epoch(db)
        await db.execute(
            update(ConnectorSubmission)
            .where(ConnectorSubmission.id == submission_id)
            .values(operation_id=foreign, api_key_identity=str(uuid.uuid4()), storage_epoch=epoch)
        )
        await db.commit()
    return foreign


@pytest.mark.parametrize("namespace_in_query", [False, True])
@pytest.mark.parametrize("window", ["after_proof", "after_observations", "after_result"])
async def test_a_restore_window_never_answers_with_another_operation(
    farm, sessions, monkeypatch, window, namespace_in_query
):
    from fastapi import HTTPException

    from backend.app.api.routes import connector as route

    operation_id, submission_id = await _publish(farm, _single(farm))
    hooks = {
        "after_proof": (route, "_own"),
        "after_observations": (cs, "observations"),
        "after_result": (cs, "result_of"),
    }
    module, name = hooks[window]
    original = getattr(module, name)

    async def then_replace(*args, **kwargs):
        value = await original(*args, **kwargs)
        await _replace_under(sessions, submission_id)
        return value

    monkeypatch.setattr(module, name, then_replace)
    query = {"server_id": farm["server_id"], "storage_epoch": farm["epoch"]} if namespace_in_query else {}

    with pytest.raises(HTTPException) as refused:
        await route.get_submission(
            operation_id,
            server_id=query.get("server_id"),
            storage_epoch=query.get("storage_epoch"),
            observations=True,
            cursor=None,
            _=None,
            actor=farm["actor"],
        )

    assert refused.value.status_code == 409
    assert refused.value.detail["error"] == "context_changed"


async def test_a_submission_gone_after_the_proof_is_refused_not_a_crash(farm, sessions, monkeypatch):
    from fastapi import HTTPException

    from backend.app.api.routes import connector as route

    operation_id, submission_id = await _publish(farm, _single(farm))
    original = route._own

    async def then_delete(*args, **kwargs):
        value = await original(*args, **kwargs)
        async with sessions() as db:
            await db.execute(delete(ConnectorSubmissionEntry))
            await db.execute(delete(ConnectorSubmission).where(ConnectorSubmission.id == submission_id))
            await db.commit()
        return value

    monkeypatch.setattr(route, "_own", then_delete)

    with pytest.raises(HTTPException) as refused:
        await route.get_submission(
            operation_id,
            server_id=None,
            storage_epoch=None,
            observations=True,
            cursor=None,
            _=None,
            actor=farm["actor"],
        )

    assert refused.value.status_code in (404, 409)


async def test_a_read_during_a_restore_hold_is_refused(farm, sessions):
    from fastapi import HTTPException

    from backend.app.api.routes import connector as route
    from backend.app.services.connector_runtime import connector_runtime

    operation_id, _submission_id = await _publish(farm, _single(farm))
    assert await connector_runtime.quiesce_for_restore()
    try:
        with pytest.raises(HTTPException) as refused:
            await route.get_submission(
                operation_id, server_id=None, storage_epoch=None, observations=True, cursor=None, _=None,
                actor=farm["actor"],
            )  # fmt: skip
    finally:
        connector_runtime._reset_for_tests()

    assert refused.value.status_code == 503 and refused.value.detail["error"] == "storage_restoring"


async def test_every_reader_by_id_checks_the_proof(farm, sessions):
    """result_of, ensure_resumable and make_resumable each refuse a row that is no longer the proven operation."""
    operation_id, submission_id = await _publish(farm, _single(farm))
    proof = await cs.prove_own(farm["actor"], operation_id, None, None)
    await _replace_under(sessions, submission_id)

    for read in (
        lambda: cs.result_of(submission_id, proof=proof),
        lambda: cs.ensure_resumable(submission_id, proof=proof),
        lambda: cs.make_resumable(submission_id, proof=proof),
        lambda: cs.observations(farm["actor"], proof),
    ):
        with pytest.raises(cs.ConnectorRefusal) as refused:
            await read()
        assert (refused.value.status, refused.value.code) == (409, "context_changed")
