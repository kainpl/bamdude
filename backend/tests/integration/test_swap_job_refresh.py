"""Applying a source's declaration to SELECTED jobs (swap-source-declaration spec r3 §6; plan T9.4).

A separate action after a successful declaration: the operator names jobs, each with the revision they saw. Only a
job whose own snapshot is unknown or missing, whose own live link and captured blob are the declared source and its
bytes, and which has provably never been dispatched, gets the declaration's value written into its snapshot -- and
nothing else of the job, the blob or the queue moves. A refresh is no Retry: a failed job stays failed. One refused
job refuses the whole group, with no change at all; the audit line names what really changed.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import QueueSource
from backend.app.models.swap_source_declaration_audit import SwapSourceDeclarationAudit
from backend.app.services import queue_sources, swap_compatibility as sc, swap_job_refresh as jobs
from backend.app.services.queue_source_capture import capture_staged, plan_capture
from backend.tests.integration.test_swap_declaration_service import (  # noqa: F401 -- fixtures
    ALL,
    a_3mf,
    a_user,
    actor_of,
    an_archive,
    clean_spool_state,
    declare,
    factory,
    sha,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

QUEUE_ALL = jobs.JobRights(read_all=True, update_all=True)


async def a_printer_queue(factory, serial: str = "SN1") -> int:
    async with factory() as db:
        printer = Printer(
            name=serial, serial_number=serial, ip_address="127.0.0.1", access_code="00000000", model="P1S"
        )
        db.add(printer)
        await db.flush()
        db.add(PrinterQueue(id=printer.id, printer_id=printer.id, status="idle"))
        await db.commit()
        return printer.id


async def a_job(factory, archive_id: int, *, kind: str = "queue", queue_id: int | None = None, fact="captured", **cols):
    """A job that captured this archive's bytes through the real spool BEFORE the declaration: its snapshot says
    what a legacy archive gave then (unknown). ``fact``: "captured" is that unknown, "missing" drops the key (a
    pre-fix snapshot), True/False pin a known one."""
    async with factory() as db:
        archive = await db.get(PrintArchive, archive_id)
        staged = await capture_staged(plan_capture(archive=archive))
    made: list[int] = []

    async def attach(session, source: QueueSource) -> None:
        snapshot = sc.snapshot_with_fact(queue_sources.snapshot_for(staged.receipt, source), sc.UNKNOWN_SOURCE)
        if fact == "missing":
            snapshot.pop(sc.SNAPSHOT_FACT_KEY, None)
            snapshot.pop(sc.SNAPSHOT_REASON_KEY, None)
        elif fact in (True, False):
            snapshot = sc.snapshot_with_fact(snapshot, sc.SourceFact(fact))
        columns = {"status": "pending", "archive_id": archive_id, "queue_source_id": source.id, **cols}
        if kind == "queue":
            row = PrintQueueItem(queue_id=queue_id, position=1, source_snapshot=snapshot, **columns)
        else:
            row = AutoQueueItem(source_snapshot=snapshot, **columns)
        session.add(row)
        await session.flush()
        made.append(row.id)

    await queue_sources.publish(staged.receipt, attach, session_factory=factory)
    return made[0]


async def a_declared_archive(factory, tmp_path, user, *, value=True, name="plate.gcode.3mf"):
    path = tmp_path / name
    a_3mf(path, name)
    archive_id = await an_archive(factory, path, owner=user)
    seen = await jobs_free_inspect(archive_id, user)
    await declare("archive", archive_id, seen, user, value=value)
    return archive_id


async def jobs_free_inspect(archive_id, user):
    from backend.app.services import swap_source_declarations as decl

    return await decl.inspect("archive", archive_id, actor=actor_of(user), rights=ALL)


async def listing(archive_id, user, rights=QUEUE_ALL):
    return await jobs.candidates("archive", archive_id, actor=actor_of(user), rights=ALL, job_rights=rights)


async def apply(archive_id, user, picked, seen, *, rights=QUEUE_ALL, operation_id=None):
    return await jobs.refresh(
        "archive",
        archive_id,
        declaration_id=seen["declaration_id"],
        source_revision=seen["source_revision"],
        jobs=picked,
        operation_id=operation_id or str(uuid.uuid4()),
        actor=actor_of(user),
        rights=ALL,
        job_rights=rights,
    )


def ref(entry) -> jobs.JobRef:
    return jobs.JobRef(entry["kind"], entry["id"], entry["revision"])


def by_id(seen, kind, job_id):
    return next(entry for entry in seen["jobs"] if entry["kind"] == kind and entry["id"] == job_id)


async def row(factory, kind, job_id):
    model = PrintQueueItem if kind == "queue" else AutoQueueItem
    async with factory() as db:
        return await db.get(model, job_id)


async def revision_of(factory, kind, job_id) -> str:
    return jobs.job_revision(kind, await row(factory, kind, job_id))


async def audits(factory):
    async with factory() as db:
        return (
            (await db.execute(select(SwapSourceDeclarationAudit).order_by(SwapSourceDeclarationAudit.id)))
            .scalars()
            .all()
        )


@pytest.mark.parametrize("proof,count", [({"receipt_identity": "retired"}, 1), ({}, 0), (None, 1)])
@pytest.mark.parametrize("kind", ["failed", "unsent"])
async def test_returned_auto_proof_refuses_refresh_after_assignment_fields_cleared(
    factory, tmp_path, proof, count, kind
):
    user = await a_user(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    auto_id = await a_job(
        factory,
        archive_id,
        kind="auto_queue",
        assigned_to_item_id=None,
        assigned_at=None,
        **{f"{kind}_return_proof": proof, f"{kind}_return_count": count},
    )
    seen = await listing(archive_id, user)
    entry = by_id(seen, "auto_queue", auto_id)
    assert (entry["eligible"], entry["reason"]) == (False, "not_before_dispatch")
    before = await row(factory, "auto_queue", auto_id)
    with pytest.raises(HTTPException) as error:
        await apply(archive_id, user, [ref(entry)], seen)
    assert error.value.status_code == 409
    after = await row(factory, "auto_queue", auto_id)
    assert after.source_snapshot == before.source_snapshot
    assert (getattr(after, f"{kind}_return_proof"), getattr(after, f"{kind}_return_count")) == (proof, count)


# --- what a refresh changes -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("fact", ["captured", "missing"])
@pytest.mark.parametrize("value", [True, False])
async def test_an_unknown_or_missing_snapshot_takes_the_declaration_only_for_the_named_jobs(
    fact, value, factory, tmp_path
):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user, value=value)
    named = await a_job(factory, archive_id, queue_id=queue_id, fact=fact)
    neighbour = await a_job(factory, archive_id, queue_id=queue_id, fact=fact)  # same blob, not named
    auto = await a_job(factory, archive_id, kind="auto_queue", fact=fact)
    before_neighbour = (await row(factory, "queue", neighbour)).source_snapshot

    seen = await listing(archive_id, user)
    assert {(e["kind"], e["id"]) for e in seen["jobs"] if e["eligible"]} == {
        ("queue", named),
        ("queue", neighbour),
        ("auto_queue", auto),
    }
    done = await apply(archive_id, user, [ref(by_id(seen, "queue", named)), ref(by_id(seen, "auto_queue", auto))], seen)

    assert sorted((j["kind"], j["id"]) for j in done["refreshed"]) == [("auto_queue", auto), ("queue", named)]
    for kind, job_id in (("queue", named), ("auto_queue", auto)):
        assert sc.snapshot_fact((await row(factory, kind, job_id)).source_snapshot) == sc.SourceFact(value)
    assert (await row(factory, "queue", neighbour)).source_snapshot == before_neighbour
    [_declare, line] = await audits(factory)
    assert line.action == "refresh_jobs" and line.new_value is value
    assert sorted((j["kind"], j["id"]) for j in line.jobs) == [("auto_queue", auto), ("queue", named)]


async def test_a_refresh_moves_nothing_but_the_snapshots_fact(factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user, value=True)
    job_id = await a_job(
        factory,
        archive_id,
        queue_id=queue_id,
        status="failed",
        error_message="Swap macros were asked for a source of unknown swap compatibility.",
        execute_swap_macros=True,
        swap_macro_events='["swap_mode_start"]',
        plate_id=2,
        ams_mapping="[1, -1]",
        bed_levelling=False,
    )
    before = await row(factory, "queue", job_id)
    async with factory() as db:
        blob_before = await db.get(QueueSource, before.queue_source_id)
        blob_before = {c.name: getattr(blob_before, c.name) for c in QueueSource.__table__.columns}

    seen = await listing(archive_id, user)
    await apply(archive_id, user, [ref(by_id(seen, "queue", job_id))], seen)

    after = await row(factory, "queue", job_id)
    columns = {c.name for c in PrintQueueItem.__table__.columns} - {"source_snapshot"}
    assert {c: getattr(after, c) for c in columns} == {c: getattr(before, c) for c in columns}
    assert after.status == "failed"  # a refresh is no Retry
    expected = {k: v for k, v in before.source_snapshot.items() if k != sc.SNAPSHOT_REASON_KEY}
    assert after.source_snapshot == {**expected, sc.SNAPSHOT_FACT_KEY: True}
    async with factory() as db:
        blob_after = await db.get(QueueSource, after.queue_source_id)
        assert {c.name: getattr(blob_after, c.name) for c in QueueSource.__table__.columns} == blob_before


# --- what a refresh refuses -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("known", [True, False])
async def test_a_known_snapshot_is_never_replaced(known, factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user, value=not known)
    job_id = await a_job(factory, archive_id, queue_id=queue_id, fact=known)

    seen = await listing(archive_id, user)
    entry = by_id(seen, "queue", job_id)
    assert (entry["eligible"], entry["reason"]) == (False, "snapshot_known")
    with pytest.raises(HTTPException) as refused:
        await apply(archive_id, user, [ref(entry)], seen)
    assert refused.value.status_code == 409 and refused.value.detail["error"] == "job_not_refreshable"
    assert sc.snapshot_fact((await row(factory, "queue", job_id)).source_snapshot) == sc.SourceFact(known)


@pytest.mark.parametrize(
    ("columns", "reason"),
    [
        ({"status": "printing", "started_at": "now"}, "status"),
        ({"status": "completed"}, "status"),
        ({"status": "cancelled"}, "status"),
        ({"status": "failed", "started_at": "now"}, "not_before_dispatch"),  # it was claimed: it may have run
        ({"status": "pending", "dispatch_attempts": 1}, "not_before_dispatch"),  # sent once, reverted by the watchdog
        ({"status": "failed", "origin": "direct"}, "not_before_dispatch"),
        ({"status": "pending", "is_calibration": True}, "not_before_dispatch"),
    ],
)
async def test_a_job_that_may_have_run_is_refused(columns, reason, factory, tmp_path):
    from datetime import datetime, timezone

    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    columns = {k: (datetime.now(timezone.utc) if v == "now" else v) for k, v in columns.items()}
    job_id = await a_job(factory, archive_id, queue_id=queue_id, **columns)
    snapshot = (await row(factory, "queue", job_id)).source_snapshot

    seen = await listing(archive_id, user)
    entry = next((e for e in seen["jobs"] if e["id"] == job_id), None)
    if entry is not None:  # only pending/failed work is listed at all
        assert (entry["eligible"], entry["reason"]) == (False, reason)
    revision = await revision_of(factory, "queue", job_id)
    with pytest.raises(HTTPException) as refused:
        await apply(archive_id, user, [jobs.JobRef("queue", job_id, revision)], seen)
    assert refused.value.status_code == 409
    assert refused.value.detail["jobs"] == [{"kind": "queue", "id": job_id, "reason": reason}]
    assert (await row(factory, "queue", job_id)).source_snapshot == snapshot


@pytest.mark.parametrize(
    ("columns", "reason"),
    [
        ({"status": "assigned", "assigned_to_item_id": 99}, "status"),
        ({"status": "pending", "cancelled_at": "now"}, "cancelled"),
        ({"status": "pending", "rebalanced_at": "now", "rebalanced_from_model": "X1C"}, "companion"),
    ],
)
async def test_an_auto_queue_job_already_routed_or_cancelled_is_refused(columns, reason, factory, tmp_path):
    from datetime import datetime, timezone

    user = await a_user(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    columns = {k: (datetime.now(timezone.utc) if v == "now" else v) for k, v in columns.items()}
    job_id = await a_job(factory, archive_id, kind="auto_queue", **columns)

    seen = await listing(archive_id, user)
    revision = await revision_of(factory, "auto_queue", job_id)
    with pytest.raises(HTTPException) as refused:
        await apply(archive_id, user, [jobs.JobRef("auto_queue", job_id, revision)], seen)
    assert refused.value.detail["jobs"] == [{"kind": "auto_queue", "id": job_id, "reason": reason}]


async def test_a_job_of_other_bytes_or_another_source_is_refused(factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    other_id = await a_declared_archive(factory, tmp_path, user, name="other.gcode.3mf")
    of_other_bytes = await a_job(factory, other_id, queue_id=queue_id)
    async with factory() as db:  # its link names the declared source, its blob holds the other's bytes
        (await db.get(PrintQueueItem, of_other_bytes)).archive_id = archive_id
        await db.commit()
    forged = await a_job(factory, archive_id, queue_id=queue_id)
    async with factory() as db:  # the provenance names another archive than the job's own link
        item = await db.get(PrintQueueItem, forged)
        item.source_snapshot = {**item.source_snapshot, "provenance": {"kind": "archive", "id": other_id}}
        await db.commit()
    gone = await a_job(factory, archive_id, queue_id=queue_id)
    async with factory() as db:  # the link went away (SET NULL)
        (await db.get(PrintQueueItem, gone)).archive_id = None
        await db.commit()
    unreadable = await a_job(factory, archive_id, queue_id=queue_id)
    async with factory() as db:
        item = await db.get(PrintQueueItem, unreadable)
        item.source_snapshot = {**item.source_snapshot, "version": 999}
        await db.commit()

    seen = await listing(archive_id, user)
    expected = {
        of_other_bytes: "hash_mismatch",
        forged: "source_mismatch",
        gone: "source_mismatch",
        unreadable: "snapshot_unreadable",
    }
    for job_id, reason in expected.items():
        revision = await revision_of(factory, "queue", job_id)
        with pytest.raises(HTTPException) as refused:
            await apply(archive_id, user, [jobs.JobRef("queue", job_id, revision)], seen)
        assert refused.value.detail["jobs"] == [{"kind": "queue", "id": job_id, "reason": reason}], job_id
    assert [e["id"] for e in seen["jobs"] if e["eligible"]] == []


async def test_a_job_without_a_captured_source_is_left_to_retry(factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    async with factory() as db:
        legacy = PrintQueueItem(queue_id=queue_id, position=1, status="failed", archive_id=archive_id)
        db.add(legacy)
        await db.commit()
        legacy_id = legacy.id

    seen = await listing(archive_id, user)
    entry = by_id(seen, "queue", legacy_id)
    assert (entry["eligible"], entry["reason"]) == (False, "no_captured_source")  # Retry captures it again
    with pytest.raises(HTTPException):
        await apply(archive_id, user, [ref(entry)], seen)
    assert (await row(factory, "queue", legacy_id)).source_snapshot is None  # nothing fabricated


async def test_one_refused_job_refuses_the_whole_group(factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    good = await a_job(factory, archive_id, queue_id=queue_id)
    known = await a_job(factory, archive_id, queue_id=queue_id, fact=False)
    seen = await listing(archive_id, user)

    with pytest.raises(HTTPException) as refused:
        await apply(archive_id, user, [ref(by_id(seen, "queue", good)), ref(by_id(seen, "queue", known))], seen)

    assert refused.value.detail["jobs"] == [{"kind": "queue", "id": known, "reason": "snapshot_known"}]
    assert sc.snapshot_fact((await row(factory, "queue", good)).source_snapshot).value is None
    assert [line.action for line in await audits(factory)] == ["declare"]


@pytest.mark.parametrize("problem", ["too_many", "duplicate", "empty"])
async def test_the_selection_is_explicit_bounded_and_unique(problem, factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    seen = await listing(archive_id, user)
    one = ref(by_id(seen, "queue", job_id))
    picked = {
        "too_many": [jobs.JobRef("queue", n, "0" * 64) for n in range(1, jobs.MAX_JOBS + 2)],
        "duplicate": [one, one],
        "empty": [],
    }[problem]

    with pytest.raises(HTTPException) as refused:
        await apply(archive_id, user, picked, seen)
    assert refused.value.status_code == 422


async def test_a_stale_job_revision_or_declaration_is_refused(factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    seen = await listing(archive_id, user)
    async with factory() as db:  # an edit the revision covers: the job moved to another printer's queue
        (await db.get(PrintQueueItem, job_id)).queue_id = await a_printer_queue(factory, "SN2")
        await db.commit()

    with pytest.raises(HTTPException) as moved:
        await apply(archive_id, user, [ref(by_id(seen, "queue", job_id))], seen)
    assert moved.value.status_code == 409 and moved.value.detail["error"] == "jobs_changed"

    fresh = await listing(archive_id, user)
    redeclared = await jobs_free_inspect(archive_id, user)
    await declare("archive", archive_id, redeclared, user, value=False)  # a newer declaration replaces it
    with pytest.raises(HTTPException) as replaced:
        await apply(archive_id, user, [ref(by_id(fresh, "queue", job_id))], fresh)
    assert replaced.value.detail["error"] == "source_changed"
    assert sc.snapshot_fact((await row(factory, "queue", job_id)).source_snapshot).value is None


async def test_a_reused_job_id_is_another_job(factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    seen = await listing(archive_id, user)
    async with factory() as db:  # deleted and inserted again under the very same id and payload
        old = await db.get(PrintQueueItem, job_id)
        payload = {c.name: getattr(old, c.name) for c in PrintQueueItem.__table__.columns}
        await db.delete(old)
        await db.flush()
        payload["swap_declaration_identity"] = str(uuid.uuid4())
        db.add(PrintQueueItem(**payload))
        await db.commit()

    with pytest.raises(HTTPException) as refused:
        await apply(archive_id, user, [ref(by_id(seen, "queue", job_id))], seen)
    assert refused.value.detail["error"] == "jobs_changed"


async def test_a_storage_restore_voids_the_listing(factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    seen = await listing(archive_id, user)
    queue_sources.invalidate_receipts()  # what a full restore does to the storage generation

    with pytest.raises(HTTPException) as refused:
        await apply(archive_id, user, [ref(by_id(seen, "queue", job_id))], seen)
    assert refused.value.detail["error"] == "source_changed"


async def test_each_job_asks_its_own_update_right(factory, tmp_path):
    owner = await a_user(factory, "owner")
    other = await a_user(factory, "other")
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, owner)
    mine = await a_job(factory, archive_id, queue_id=queue_id, created_by_id=owner.id)
    theirs = await a_job(factory, archive_id, queue_id=queue_id, created_by_id=other.id)
    auto = await a_job(factory, archive_id, kind="auto_queue", created_by_id=owner.id)
    own_only = jobs.JobRights(read_all=False, update_all=False)

    seen = await listing(archive_id, owner, rights=own_only)
    assert {(e["kind"], e["id"]) for e in seen["jobs"]} == {("queue", mine), ("auto_queue", auto)}  # theirs unlisted
    assert by_id(seen, "auto_queue", auto)["reason"] == "not_editable"  # the router's rows need update_all
    revision = await revision_of(factory, "queue", theirs)
    with pytest.raises(HTTPException) as refused:
        await apply(
            archive_id,
            owner,
            [ref(by_id(seen, "queue", mine)), jobs.JobRef("queue", theirs, revision)],
            seen,
            rights=own_only,
        )
    assert refused.value.detail["jobs"] == [{"kind": "queue", "id": theirs, "reason": "job_gone"}]  # never "not yours"
    assert sc.snapshot_fact((await row(factory, "queue", mine)).source_snapshot).value is None


async def test_a_replayed_operation_answers_its_first_result(factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    seen = await listing(archive_id, user)
    operation = str(uuid.uuid4())
    picked = [ref(by_id(seen, "queue", job_id))]

    first = await apply(archive_id, user, picked, seen, operation_id=operation)
    again = await apply(archive_id, user, picked, seen, operation_id=operation)

    assert {**again, "replayed": False} == first and len(await audits(factory)) == 2  # one declare, one refresh
    with pytest.raises(HTTPException) as reused:
        await apply(archive_id, user, [], seen, operation_id=operation)
    assert reused.value.status_code in (409, 422)


async def test_a_refresh_reads_no_file(factory, tmp_path, monkeypatch):
    from backend.app.services import queue_source_capture

    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    seen = await listing(archive_id, user)

    def no_file_io(*_args, **_kwargs):
        raise AssertionError("a refresh copies nothing")

    monkeypatch.setattr(queue_source_capture, "capture_for_dispatch", no_file_io)
    monkeypatch.setattr(queue_source_capture, "capture_staged", no_file_io)
    monkeypatch.setattr(queue_sources, "capture", no_file_io)
    await apply(archive_id, user, [ref(by_id(seen, "queue", job_id))], seen)


# --- what follows a refresh: Retry, then the runtime decision ---------------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
async def test_refresh_then_retry_then_the_runtime_decision_reads_the_new_value(value, factory, tmp_path):
    from backend.app.services.background_dispatch import BackgroundDispatchService
    from backend.app.services.filament_intake import item_descriptor

    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    async with factory() as db:
        (await db.get(Printer, queue_id)).swap_mode_enabled = True
        await db.commit()
    archive_id = await a_declared_archive(factory, tmp_path, user, value=value)
    job_id = await a_job(
        factory,
        archive_id,
        queue_id=queue_id,
        status="failed",
        execute_swap_macros=True,
        swap_macro_events='["swap_mode_start", "swap_mode_change_table"]',
    )
    seen = await listing(archive_id, user)
    await apply(archive_id, user, [ref(by_id(seen, "queue", job_id))], seen)
    async with factory() as db:  # the ordinary Retry, a separate action
        (await db.get(PrintQueueItem, job_id)).status = "pending"
        await db.commit()

    async with factory() as db:
        item = await db.get(PrintQueueItem, job_id)
        descriptor = await item_descriptor(db, item)

        class Job:
            printer_id = queue_id
            source = descriptor
            options = {"execute_swap_macros": True, "swap_macro_events": ["swap_mode_start", "swap_mode_change_table"]}

        await BackgroundDispatchService._apply_swap_source_policy(db, Job)

    assert descriptor.swap_fact == sc.SourceFact(value)
    # True: the source carries its own sequence, ours are muted. False: exactly what was asked.
    assert Job.options["execute_swap_macros"] is (not value)


async def test_a_library_source_refreshes_its_own_jobs_and_refuses_one_already_dispatched(factory, tmp_path):
    from backend.app.models.library import LibraryFile
    from backend.app.services import swap_source_declarations as decl
    from backend.tests.integration.test_swap_declaration_service import a_library_file

    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    path = tmp_path / "lib.gcode.3mf"
    a_3mf(path, "library")
    library_id = await a_library_file(factory, path, owner=user)
    archive_id = await an_archive(factory, tmp_path / "lib.gcode.3mf", owner=user)  # an execution archive

    made: list[int] = []
    async with factory() as db:
        staged = await capture_staged(plan_capture(library_file=await db.get(LibraryFile, library_id)))

    async def attach(session, source: QueueSource) -> None:
        snapshot = sc.snapshot_with_fact(queue_sources.snapshot_for(staged.receipt, source), sc.UNKNOWN_SOURCE)
        for wired in (None, archive_id):
            row = PrintQueueItem(
                queue_id=queue_id,
                position=1,
                status="pending",
                library_file_id=library_id,
                archive_id=wired,
                queue_source_id=source.id,
                source_snapshot=dict(snapshot),
            )
            session.add(row)
            await session.flush()
            made.append(row.id)

    await queue_sources.publish(staged.receipt, attach, session_factory=factory)
    plain, dispatched = made
    seen_bytes = await decl.inspect("library", library_id, actor=actor_of(user), rights=ALL)
    await declare("library", library_id, seen_bytes, user, value=True)

    seen = await jobs.candidates("library", library_id, actor=actor_of(user), rights=ALL, job_rights=QUEUE_ALL)
    assert by_id(seen, "queue", dispatched)["reason"] == "not_before_dispatch"
    done = await jobs.refresh(
        "library",
        library_id,
        declaration_id=seen["declaration_id"],
        source_revision=seen["source_revision"],
        jobs=[ref(by_id(seen, "queue", plain))],
        operation_id=str(uuid.uuid4()),
        actor=actor_of(user),
        rights=ALL,
        job_rights=QUEUE_ALL,
    )

    assert done["refreshed"] == [{"kind": "queue", "id": plain}]
    assert sc.snapshot_fact((await row(factory, "queue", plain)).source_snapshot) == sc.SourceFact(True)
    assert sc.snapshot_fact((await row(factory, "queue", dispatched)).source_snapshot).value is None


async def test_a_replay_answers_only_while_the_source_is_still_there(factory, tmp_path):
    from datetime import datetime, timezone

    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    seen = await listing(archive_id, user)
    operation = str(uuid.uuid4())
    picked = [ref(by_id(seen, "queue", job_id))]
    await apply(archive_id, user, picked, seen, operation_id=operation)
    async with factory() as db:  # the source went to the trash after the refresh
        (await db.get(PrintArchive, archive_id)).deleted_at = datetime.now(timezone.utc)
        await db.commit()

    with pytest.raises(HTTPException) as refused:
        await apply(archive_id, user, picked, seen, operation_id=operation)
    assert refused.value.status_code == 404


async def test_a_snapshot_whose_fact_is_malformed_is_not_repaired(factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    async with factory() as db:
        item = await db.get(PrintQueueItem, job_id)
        item.source_snapshot = {**item.source_snapshot, sc.SNAPSHOT_FACT_KEY: "yes"}
        await db.commit()

    seen = await listing(archive_id, user)
    entry = by_id(seen, "queue", job_id)
    assert (entry["eligible"], entry["reason"]) == (False, "snapshot_unreadable")


async def test_the_refresh_audit_is_named_as_the_spec_names_it(factory, tmp_path):
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    seen = await listing(archive_id, user)
    await apply(archive_id, user, [ref(by_id(seen, "queue", job_id))], seen)

    assert [line.action for line in await audits(factory)] == ["declare", "refresh_jobs"]


async def test_a_job_moved_between_the_preflight_and_the_lanes_is_changed_and_never_locked(
    factory, tmp_path, monkeypatch
):
    """G03 B, A31: a printer row is locked only inside the lanes the refresh holds; one that moved to another queue
    after the preflight read its lanes is read unlocked and answered jobs_changed, and nothing is written."""
    user = await a_user(factory)
    queue_id = await a_printer_queue(factory)
    other = await a_printer_queue(factory, "SN2")
    archive_id = await a_declared_archive(factory, tmp_path, user)
    job_id = await a_job(factory, archive_id, queue_id=queue_id)
    seen = await listing(archive_id, user)
    real = jobs._guard_actor

    async def moved_meanwhile(session, actor):
        async with factory() as db:
            (await db.get(PrintQueueItem, job_id)).queue_id = other
            await db.commit()
        return await real(session, actor)

    monkeypatch.setattr(jobs, "_guard_actor", moved_meanwhile)
    with pytest.raises(HTTPException) as moved:
        await apply(archive_id, user, [ref(by_id(seen, "queue", job_id))], seen)
    assert moved.value.status_code == 409 and moved.value.detail["error"] == "jobs_changed"
    current = await row(factory, "queue", job_id)
    assert current.queue_id == other
    assert sc.snapshot_fact(current.source_snapshot).value is None
