"""A connector operation publishes each entry once, and its receipt is the proof (OrcaBamDude spec r2 §7-8, C1-C3).

Driven through the real queue writers, so the receipt is claimed and recorded inside the very publication that
creates the jobs. What is pinned here:
- an entry publishes exactly ``quantity`` jobs, and running the operation again creates none;
- the same operation with other content is a conflict, another server's operation is refused;
- a printer whose id was reused, a key whose id was reused, a key restricted to other printers and an AutoQueue
  entry from a restricted key all publish nothing;
- bytes that are not the uploaded ones publish nothing;
- cancellation fences only unpublished entries;
- a writer that raised after its commit leaves the entry published, read back from the database.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import auth, database
from backend.app.models.api_key import APIKey
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.connector_submission import ConnectorSubmissionEntry
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.schemas.connector import SubmissionCreate
from backend.app.services import connector_submissions as cs, queue_sources
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _clean_spool_state():
    queue_sources._reset_state()
    yield
    queue_sources._reset_state()


@pytest.fixture
def sessions(test_engine, monkeypatch):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


@pytest.fixture
async def farm(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    source, printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    sha = hashlib.sha256(Path(source.file_path).read_bytes()).hexdigest()
    _raw, key_hash, prefix = auth.generate_api_key()
    key = APIKey(name="orca", key_hash=key_hash, key_prefix=prefix, enabled=True, can_queue=True, can_read_status=True)
    db_session.add(key)
    await db_session.commit()
    actor = cs.ConnectorActor(key_id=key.id, key_identity=key.connector_identity, owner_id=None)
    async with sessions() as db:
        server_id, epoch = await cs.server_identity(db)
    return {
        "source": source,
        "sha": sha,
        "printer": printer,
        "queue": queue,
        "key": key,
        "actor": actor,
        "server_id": server_id,
        "epoch": epoch,
    }


def _printer_entry(farm, **overrides):
    entry = {
        "entry_id": str(uuid.uuid4()),
        "plate_id": 15,
        "quantity": 2,
        "target": {
            "kind": "printer",
            "printer_id": farm["printer"].id,
            "printer_identity": farm["printer"].connector_identity,
        },
        "action": "queue_manual",
    }
    entry.update(overrides)
    return entry


def _body(farm, *entries, **overrides) -> SubmissionCreate:
    body = {
        "protocol_version": 1,
        "operation_id": str(uuid.uuid4()),
        "server_id": farm["server_id"],
        "storage_epoch": farm["epoch"],
        "library_file_id": farm["source"].id,
        "source_sha256": farm["sha"],
        "entries": list(entries) or [_printer_entry(farm)],
    }
    body.update(overrides)
    return SubmissionCreate.model_validate(body)


async def _count(sessions, model) -> int:
    async with sessions() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def _submit(farm, body):
    registered = await cs.register(farm["actor"], body)
    await cs.run(registered.submission_id, farm["actor"])
    return await cs.result(farm["actor"], body.operation_id)


async def test_an_entry_publishes_its_quantity_and_the_receipt_names_the_jobs(farm, sessions):
    result = await _submit(farm, _body(farm))

    assert result.state == "complete"
    [entry] = result.entries
    assert entry.state == "published" and len(entry.queue_item_ids) == 2 and entry.auto_item_ids == []
    async with sessions() as db:
        items = (await db.execute(select(PrintQueueItem))).scalars().all()
        receipt = await db.scalar(select(ConnectorSubmissionEntry.published_receipt))
    assert sorted(i.id for i in items) == sorted(entry.queue_item_ids)
    assert all(i.manual_start and not i.execute_swap_macros for i in items)
    assert receipt["source_sha256"] == farm["sha"] and receipt["target"]["printer_id"] == farm["printer"].id
    # The receipt names each job by its immutable identity too: a later reader follows that, never the id.
    by_id = {i.id: i.recovery_identity for i in items}
    assert receipt["jobs"] == [
        {"kind": "queue", "id": job_id, "identity": by_id[job_id]} for job_id in entry.queue_item_ids
    ]
    assert all(job["identity"] for job in receipt["jobs"])


async def test_running_a_published_operation_again_creates_no_job(farm, sessions):
    body = _body(farm)
    registered = await cs.register(farm["actor"], body)
    await cs.run(registered.submission_id, farm["actor"])
    again = await cs.register(farm["actor"], body)
    await cs.run(again.submission_id, farm["actor"])

    assert again.submission_id == registered.submission_id and not again.created
    assert await _count(sessions, PrintQueueItem) == 2


async def test_a_retryable_entry_that_published_meanwhile_is_not_published_twice(farm, sessions):
    body = _body(farm)
    registered = await cs.register(farm["actor"], body)
    await cs.run(registered.submission_id, farm["actor"])
    # A stale coordinator believes the entry failed: the receipt still decides.
    async with sessions() as db:
        await db.execute(update(ConnectorSubmissionEntry).values(state="retryable_failure"))
        await db.commit()
    await cs.run(registered.submission_id, farm["actor"])

    assert await _count(sessions, PrintQueueItem) == 2
    assert (await cs.result(farm["actor"], body.operation_id)).state == "complete"


async def test_the_same_operation_with_other_content_is_a_conflict(farm, sessions):
    body = _body(farm)
    await cs.register(farm["actor"], body)
    changed = _body(farm, _printer_entry(farm, quantity=3), operation_id=body.operation_id)

    with pytest.raises(cs.ConnectorRefusal) as refused:
        await cs.register(farm["actor"], changed)
    assert refused.value.code == "operation_conflict"


async def test_an_operation_made_for_another_server_is_refused(farm):
    with pytest.raises(cs.ConnectorRefusal) as refused:
        await cs.register(farm["actor"], _body(farm, server_id=str(uuid.uuid4())))
    assert refused.value.code == "context_changed"


async def test_a_printer_whose_id_was_reused_receives_nothing(farm, sessions):
    entry = _printer_entry(farm)
    entry["target"]["printer_identity"] = str(uuid.uuid4())

    result = await _submit(farm, _body(farm, entry))

    assert result.state == "rejected" and result.entries[0].error.error == "printer_not_found"
    assert await _count(sessions, PrintQueueItem) == 0


async def test_a_key_whose_id_was_reused_is_not_the_actor(farm, sessions):
    body = _body(farm)
    registered = await cs.register(farm["actor"], body)
    async with sessions() as db:
        await db.execute(update(APIKey).where(APIKey.id == farm["key"].id).values(connector_identity=str(uuid.uuid4())))
        await db.commit()

    await cs.run(registered.submission_id, farm["actor"])

    async with sessions() as db:
        state, code = (
            await db.execute(select(ConnectorSubmissionEntry.state, ConnectorSubmissionEntry.error_code))
        ).one()
    assert (state, code) == ("rejected", "invalid_key")
    assert await _count(sessions, PrintQueueItem) == 0


async def test_a_disabled_key_publishes_nothing(farm, sessions):
    body = _body(farm)
    registered = await cs.register(farm["actor"], body)
    async with sessions() as db:
        await db.execute(update(APIKey).where(APIKey.id == farm["key"].id).values(enabled=False))
        await db.commit()

    await cs.run(registered.submission_id, farm["actor"])

    result = await cs.result(farm["actor"], body.operation_id)
    assert result.entries[0].state == "rejected" and result.entries[0].error.error == "invalid_key"
    assert await _count(sessions, PrintQueueItem) == 0


async def test_a_key_restricted_to_other_printers_publishes_nothing(farm, sessions):
    async with sessions() as db:
        await db.execute(
            update(APIKey).where(APIKey.id == farm["key"].id).values(printer_ids=[farm["printer"].id + 99])
        )
        await db.commit()

    result = await _submit(farm, _body(farm))

    assert result.entries[0].error.error == "printer_scope"
    assert await _count(sessions, PrintQueueItem) == 0


async def test_an_auto_entry_publishes_into_the_auto_queue(farm, sessions):
    entry = {
        "entry_id": str(uuid.uuid4()),
        "plate_id": 15,
        "quantity": 3,
        "target": {"kind": "auto", "target_model": "P1P"},
        "action": "queue_asap",
    }

    result = await _submit(farm, _body(farm, entry))

    assert result.state == "complete" and len(result.entries[0].auto_item_ids) == 3
    assert await _count(sessions, AutoQueueItem) == 3 and await _count(sessions, PrintQueueItem) == 0
    async with sessions() as db:
        rows = {r.id: r.recovery_identity for r in (await db.execute(select(AutoQueueItem))).scalars().all()}
        receipt = await db.scalar(select(ConnectorSubmissionEntry.published_receipt))
    assert receipt["jobs"] == [
        {"kind": "auto", "id": job_id, "identity": rows[job_id]} for job_id in result.entries[0].auto_item_ids
    ]
    assert len({job["identity"] for job in receipt["jobs"]}) == 3


async def test_a_restricted_key_cannot_use_the_auto_queue(farm, sessions):
    async with sessions() as db:
        await db.execute(update(APIKey).where(APIKey.id == farm["key"].id).values(printer_ids=[farm["printer"].id]))
        await db.commit()
    entry = {
        "entry_id": str(uuid.uuid4()),
        "plate_id": 15,
        "target": {"kind": "auto", "target_model": "P1P"},
        "action": "queue_asap",
    }

    result = await _submit(farm, _body(farm, entry))

    assert result.entries[0].error.error == "auto_requires_unrestricted_key"
    assert await _count(sessions, AutoQueueItem) == 0


async def test_bytes_other_than_the_uploaded_ones_publish_nothing(farm, sessions):
    result = await _submit(farm, _body(farm, source_sha256="0" * 64))

    assert result.entries[0].error.error == "source_changed"
    assert await _count(sessions, PrintQueueItem) == 0


async def test_cancel_fences_unpublished_entries_only(farm, sessions):
    first, second = _printer_entry(farm, quantity=1), _printer_entry(farm, quantity=1)
    body = _body(farm, first, second)
    registered = await cs.register(farm["actor"], body)
    # Publish only the first entry, then cancel.
    async with sessions() as db:
        await db.execute(
            update(ConnectorSubmissionEntry)
            .where(ConnectorSubmissionEntry.entry_id == second["entry_id"])
            .values(state="preparing")
        )
        await db.commit()
    await cs.run(registered.submission_id, farm["actor"])
    await cs.cancel(registered.proof)
    await cs.run(registered.submission_id, farm["actor"])

    result = await cs.result(farm["actor"], body.operation_id)
    assert [e.state for e in result.entries] == ["published", "cancelled"]
    assert result.state == "partial"
    assert await _count(sessions, PrintQueueItem) == 1
    async with sessions() as db:
        stored = dict(
            (await db.execute(select(ConnectorSubmissionEntry.entry_id, ConnectorSubmissionEntry.state))).all()
        )
    assert stored == {first["entry_id"]: "published", second["entry_id"]: "cancelled"}


async def test_a_foreign_key_sees_no_operation_and_cannot_cancel_it(farm):
    body = _body(farm)
    await cs.register(farm["actor"], body)
    stranger = cs.ConnectorActor(key_id=farm["key"].id, key_identity=str(uuid.uuid4()), owner_id=None)

    assert await cs.result(stranger, body.operation_id) is None
    # A cancel is addressed by a proof of ownership, and a foreign key gets none: the same 404 as a missing one.
    with pytest.raises(cs.ConnectorRefusal) as refused:
        await cs.prove_own(stranger, body.operation_id, None, None)
    assert (refused.value.status, refused.value.code) == (404, "operation_not_found")


async def test_a_writer_that_raised_after_its_commit_leaves_the_entry_published(farm, sessions, monkeypatch):
    real_commit = AsyncSession.commit
    state = {"armed": True}

    async def commit_then_raise(self):
        await real_commit(self)
        published = await self.scalar(
            select(func.count())
            .select_from(ConnectorSubmissionEntry)
            .where(ConnectorSubmissionEntry.published_receipt.is_not(None))
        )
        if state["armed"] and published:
            state["armed"] = False
            raise ConnectionResetError("the commit's answer was lost")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_raise)
    body = _body(farm)
    registered = await cs.register(farm["actor"], body)
    await cs.run(registered.submission_id, farm["actor"])
    monkeypatch.setattr(AsyncSession, "commit", real_commit)
    await cs.run(registered.submission_id, farm["actor"])

    assert not state["armed"]
    result = await cs.result(farm["actor"], body.operation_id)
    assert result.state == "complete"
    assert await _count(sessions, PrintQueueItem) == 2


async def test_an_archived_printer_receives_nothing(farm, sessions):
    async with sessions() as db:
        await db.execute(update(Printer).where(Printer.id == farm["printer"].id).values(archived=True))
        await db.commit()

    result = await _submit(farm, _body(farm))

    assert result.entries[0].error.error == "printer_not_found"
    assert await _count(sessions, PrintQueueItem) == 0


async def _backdate(sessions, days):
    from datetime import datetime, timedelta, timezone

    from backend.app.models.connector_submission import ConnectorSubmission

    async with sessions() as db:
        await db.execute(
            update(ConnectorSubmission).values(
                created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
            )
        )
        await db.commit()


async def test_a_finished_operation_compacts_to_a_tombstone_that_still_answers(farm, sessions):
    from backend.app.models.connector_submission import ConnectorSubmission

    body = _body(farm)
    before = await _submit(farm, body)
    await _backdate(sessions, cs.FULL_RETENTION_DAYS + 1)

    assert await cs.compact() == 1
    assert await cs.compact() == 0

    after = await cs.result(farm["actor"], body.operation_id)
    assert after.entries == before.entries and after.state == "complete"
    async with sessions() as db:
        manifest = (await db.execute(select(ConnectorSubmission.manifest))).scalar_one()
    assert manifest["compacted"] and "options" not in manifest["entries"][0]
    assert manifest["entries"][0]["target"]["printer_id"] == farm["printer"].id
    replay = await cs.register(farm["actor"], body)
    assert not replay.created
    with pytest.raises(cs.ConnectorRefusal) as refused:
        await cs.ensure_resumable(replay.proof)
    assert refused.value.status == 410 and refused.value.code == "receipt_compacted"
    await cs.run(replay.submission_id, farm["actor"])
    assert await _count(sessions, PrintQueueItem) == 2


async def test_an_operation_with_work_left_keeps_its_full_record(farm, sessions):
    body = _body(farm)
    await cs.register(farm["actor"], body)
    await _backdate(sessions, cs.FULL_RETENTION_DAYS + 1)

    assert await cs.compact() == 0
    await cs.ensure_resumable((await cs.register(farm["actor"], body)).proof)


async def test_a_recent_operation_is_not_compacted(farm, sessions):
    await _submit(farm, _body(farm))

    assert await cs.compact() == 0
