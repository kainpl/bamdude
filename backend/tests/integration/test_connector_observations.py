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
from backend.app.models.connector_submission import ConnectorSubmissionEntry
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
    return await cs.observations(
        farm["actor"],
        submission_id,
        operation_id=operation_id,
        server_id=farm["server_id"],
        epoch=farm["epoch"],
        cursor=cursor,
    )


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
    archive = await _execute(sessions, row, farm["printer"].id)
    live = SimpleNamespace(state="RUNNING", layer_num=0)
    monkeypatch.setattr(printer_manager, "get_status", lambda printer_id: live)

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
        "first_layer": True,
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
