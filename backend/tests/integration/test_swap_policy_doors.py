"""Every door that creates work decides on the captured fact -- swap-source-lifecycle spec r4 §8, plan T4.1.

A check of the library or archive row before the copy is a preview, never the decision: the request's swap
fields are decided from the receipt's declaration, after the capture, for the printer the job is for. True mutes
our macros, False keeps exactly what was asked, and an unknown source with an effective request is refused
(409, ``swap_source_unknown``) before anything is published -- the staging is discarded and no row is left. A
default ``execute=True`` with no events asks for nothing, and a printer without swap mode turns a stored
preference into no request at all.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.queue_source import QueueSource
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import queue_sources, swap_compatibility as sc
from backend.tests.integration.test_dispatch_without_original import PLATE, a_sliced_file
from backend.tests.integration.test_filament_routing_dispatch import setup_source
from backend.tests.integration.test_order_print_options import mixed_order  # noqa: F401 - fixture

pytestmark = pytest.mark.integration

START = ["swap_mode_start"]


@pytest.fixture(autouse=True)
def clean_spool_state():
    queue_sources._reset_state()
    yield
    deadline = time.monotonic() + 10
    while queue_sources.active_captures() and time.monotonic() < deadline:  # pragma: no cover - drain
        time.sleep(0.01)
    queue_sources._reset_state()


@pytest.fixture
def sessions(test_engine, monkeypatch):
    from backend.app.core import database

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    # The dispatcher bound the name at import; a direct Print now opens its sessions through it.
    monkeypatch.setattr("backend.app.services.background_dispatch.async_session", factory)
    return factory


def staged_parts():
    root = queue_sources.staging_root()
    return sorted(root.glob("*.part")) if root.exists() else []


async def a_library(db, tmp_path, *, fact, name="lamp.gcode.3mf") -> LibraryFile:
    """A library row declaring ``fact`` -- unknown is a declared True whose hash no longer matches the bytes."""
    path = a_sliced_file(tmp_path / name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    row = LibraryFile(
        filename=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="gcode",
        file_hash=digest if fact is not None else "0" * 64,
        swap_compatible=True if fact is None else fact,
    )
    db.add(row)
    await db.commit()
    return row


async def a_swap_printer(db, tmp_path, printer_factory, monkeypatch, *, swap_mode=True):
    _unused, printer, queue, mqtt = await setup_source(db, tmp_path, printer_factory, monkeypatch)
    printer.swap_mode_enabled = swap_mode
    await db.commit()
    settings.archive_dir.mkdir(parents=True, exist_ok=True)
    return printer, queue


async def nothing_was_left(db) -> None:
    assert (await db.execute(select(PrintQueueItem))).scalars().all() == []
    assert (await db.execute(select(QueueSource))).scalars().all() == []
    assert staged_parts() == []


def refused_as_unknown(exc: HTTPException) -> None:
    assert exc.status_code == 409
    assert exc.detail["code"] == sc.UNKNOWN_SOURCE_REASON


# --- one add ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fact", "execute", "events", "swap_mode", "stored"),
    [
        (True, True, START, True, (False, None)),  # baked: ours muted
        (False, True, START, True, (True, START)),  # plain: exactly what was asked
        (None, True, None, True, (False, None)),  # default execute without events asks for nothing
        (None, True, START, False, (False, None)),  # a preference on a printer without swap mode is no request
        (False, False, START, True, (False, None)),  # an opt-out is never raised into a request
    ],
    ids=["baked", "plain", "default-without-events", "inactive-preference", "opt-out"],
)
async def test_queue_add_decides_on_the_captured_fact(
    fact, execute, events, swap_mode, stored, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch, swap_mode=swap_mode)
    library = await a_library(db_session, tmp_path, fact=fact)

    items, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(
            queue_id=queue.id,
            library_file_id=library.id,
            plate_id=PLATE,
            execute_swap_macros=execute,
            swap_macro_events=events,
        ),
        None,
    )

    (item,) = items
    assert (item.execute_swap_macros, json.loads(item.swap_macro_events) if item.swap_macro_events else None) == stored


async def test_queue_add_refuses_an_unknown_source_asked_for_our_macros(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    library = await a_library(db_session, tmp_path, fact=None)

    with pytest.raises(HTTPException) as refused:
        await add_items_to_printer_queue(
            db_session,
            PrintQueueItemCreate(
                queue_id=queue.id,
                library_file_id=library.id,
                plate_id=PLATE,
                quantity=3,
                execute_swap_macros=True,
                swap_macro_events=START,
            ),
            None,
        )

    refused_as_unknown(refused.value)
    await nothing_was_left(db_session)


async def test_next_block_refuses_an_unknown_source_asked_for_our_macros(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_next_block_to_printer_queue

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    library = await a_library(db_session, tmp_path, fact=None)

    with pytest.raises(HTTPException) as refused:
        await add_next_block_to_printer_queue(
            db_session,
            [
                PrintQueueItemCreate(
                    queue_id=queue.id,
                    library_file_id=library.id,
                    plate_id=PLATE,
                    enqueue_position="next",
                    execute_swap_macros=True,
                    swap_macro_events=START,
                )
            ],
            None,
        )

    refused_as_unknown(refused.value)
    await nothing_was_left(db_session)


@pytest.mark.parametrize(("parent_fact", "stored"), [(True, (False, None)), (False, (True, START)), (None, "refused")])
async def test_a_copy_of_a_job_decides_on_the_parents_own_fact(
    parent_fact, stored, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_add import add_items_to_printer_queue

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    # The parent was queued without asking for our macros, so it exists whatever its source declared.
    library = await a_library(db_session, tmp_path, fact=parent_fact)
    (parent,), _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=library.id, plate_id=PLATE, swap_macro_events=None),
        None,
    )
    # The library row changing its mind afterwards is not the copy's business: it copies the parent's answer.
    library.swap_compatible = not bool(parent_fact)
    await db_session.commit()

    request = PrintQueueItemCreate(
        queue_id=queue.id,
        source_queue_item_id=parent.id,
        plate_id=PLATE,
        execute_swap_macros=True,
        swap_macro_events=START,
    )
    if stored == "refused":
        with pytest.raises(HTTPException) as refused:
            await add_items_to_printer_queue(db_session, request, None)
        refused_as_unknown(refused.value)
        assert [row.id for row in (await db_session.execute(select(PrintQueueItem))).scalars()] == [parent.id]
        return
    items, _queue = await add_items_to_printer_queue(db_session, request, None)
    copy = next(item for item in items if item.id != parent.id)
    assert (copy.execute_swap_macros, json.loads(copy.swap_macro_events) if copy.swap_macro_events else None) == stored


# --- Print now: one copy dispatched directly, several through the queue ----------------------------------------------


async def test_print_now_hash_change_is_checked_after_capture(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """The row's hash matched when the request was read; the bytes changed before the copy. The copy decides."""
    from backend.app.services.background_dispatch import BackgroundDispatchService

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    library = await a_library(db_session, tmp_path, fact=True)
    import zipfile

    with zipfile.ZipFile(library.file_path, "a") as edited:  # same name, other bytes
        edited.writestr("Metadata/edited.txt", "changed after the row was hashed")

    with pytest.raises(HTTPException) as refused:
        await BackgroundDispatchService().dispatch_print_library_file(
            file_id=library.id,
            filename=library.filename,
            printer_id=printer.id,
            printer_name=printer.name,
            options={"plate_id": PLATE, "execute_swap_macros": True, "swap_macro_events": START},
            requested_by_user_id=None,
            requested_by_username=None,
        )

    refused_as_unknown(refused.value)
    await nothing_was_left(db_session)


@pytest.mark.parametrize(("fact", "stored"), [(True, (False, [])), (False, (True, START))])
async def test_print_now_direct_decides_the_options_it_dispatches(
    fact, stored, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.background_dispatch import BackgroundDispatchService

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    library = await a_library(db_session, tmp_path, fact=fact)
    service = BackgroundDispatchService()

    await service.dispatch_print_library_file(
        file_id=library.id,
        filename=library.filename,
        printer_id=printer.id,
        printer_name=printer.name,
        options={"plate_id": PLATE, "execute_swap_macros": True, "swap_macro_events": list(START)},
        requested_by_user_id=None,
        requested_by_username=None,
    )

    (job,) = service._queued_jobs
    assert (job.options["execute_swap_macros"], job.options["swap_macro_events"] or []) == (stored[0], stored[1])
    claim = (await db_session.execute(select(PrintQueueItem))).scalar_one()
    assert claim.execute_swap_macros is stored[0]


async def test_failed_batch_policy_leaves_no_items_or_staging(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_batch import enqueue_batch_copies

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    library = await a_library(db_session, tmp_path, fact=None)

    with pytest.raises(HTTPException) as refused:
        await enqueue_batch_copies(
            db_session,
            printer_id=printer.id,
            count=3,
            library_file_id=library.id,
            plate_id=PLATE,
            execute_swap_macros=True,
            swap_macro_events=START,
            library_file=library,
        )

    refused_as_unknown(refused.value)
    await nothing_was_left(db_session)


@pytest.mark.parametrize(("fact", "stored"), [(True, False), (False, True)])
async def test_reprint_batch_checks_final_receipt(
    fact, stored, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.queue_batch import enqueue_batch_copies
    from backend.tests.integration.test_dispatch_without_original import an_archive_source

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    archive = await an_archive_source(db_session, tmp_path, printer.id)
    sc.write_archive_fact(archive, fact)
    await db_session.commit()

    items, _batch = await enqueue_batch_copies(
        db_session,
        printer_id=printer.id,
        count=2,
        archive_id=archive.id,
        plate_id=PLATE,
        execute_swap_macros=True,
        swap_macro_events=START,
    )

    assert [item.execute_swap_macros for item in items] == [stored, stored]


async def test_the_print_route_no_longer_strips_the_request_by_the_library_row(
    async_client, db_session, tmp_path, printer_factory, monkeypatch
):
    """The route hands the request to the dispatcher whole; the decision is the copy's, after the capture."""
    from unittest.mock import AsyncMock, patch

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    library = await a_library(db_session, tmp_path, fact=True)
    with (
        patch("backend.app.services.printer_manager.printer_manager.is_connected", return_value=True),
        patch(
            "backend.app.services.background_dispatch.background_dispatch.dispatch_print_library_file",
            new=AsyncMock(return_value={"dispatch_job_id": 1, "dispatch_position": 1}),
        ) as dispatched,
    ):
        response = await async_client.post(
            f"/api/v1/library/files/{library.id}/print?printer_id={printer.id}",
            json={"plate_id": PLATE, "execute_swap_macros": True, "swap_macro_events": START},
        )

    assert response.status_code == 200, response.text
    options = dispatched.await_args.kwargs["options"]
    assert options["execute_swap_macros"] is True and options["swap_macro_events"] == START


# --- AutoQueue: the assignment decides per candidate, from the job's snapshot (§8, T4.2, T4.6) ---------------------


class _Profile:
    """The owner's saved print options for the winning printer's model: here, a request for our swap start."""

    def __init__(self, execute=True, events=START):
        self.options = {"execute_swap_macros": execute, "swap_macro_events": list(events) if events else None}

    def for_printer_queue(self):
        return dict(self.options)


async def an_auto_job(db, library, *, monkeypatch, profile=None):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from backend.app.schemas.auto_queue import AutoQueueItemCreate
    from backend.app.services.auto_queue_add import add_items_to_auto_queue

    (item,) = await add_items_to_auto_queue(
        db, AutoQueueItemCreate(library_file_id=library.id, plate_ids=[PLATE], target_model="P1P"), None
    )

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr("backend.app.services.auto_queue_scheduler.async_session", session)
    monkeypatch.setattr(
        "backend.app.services.auto_queue_scheduler.preference_options",
        AsyncMock(return_value=profile or _Profile()),
    )
    return item


async def a_second_printer(db, printer_factory, monkeypatch, mqtt, *, swap_mode):
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.printer_manager import printer_manager

    other = await printer_factory(model="P1P")
    other.swap_mode_enabled = swap_mode
    db.add(PrinterQueue(id=other.id, printer_id=other.id, auto_distribute_eligible=True))
    await db.commit()
    monkeypatch.setitem(printer_manager._clients, other.id, mqtt)
    monkeypatch.setitem(printer_manager._models, other.id, "P1P")
    return other


@pytest.mark.parametrize(("fact", "stored"), [(True, False), (False, True)])
async def test_auto_assignment_uses_snapshot_after_source_deletion(
    fact, stored, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    library = await a_library(db_session, tmp_path, fact=fact)
    await an_auto_job(db_session, library, monkeypatch=monkeypatch)
    # The library row goes: the job's own snapshot is the only authority left, and it is enough.
    await db_session.delete(library)
    await db_session.commit()

    await AutoQueueScheduler().tick()

    (placed,) = (await db_session.execute(select(PrintQueueItem))).scalars().all()
    assert placed.execute_swap_macros is stored
    assert (json.loads(placed.swap_macro_events) if placed.swap_macro_events else None) == (START if stored else None)


async def test_auto_policy_checks_all_candidates_before_failing(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A swap-mode printer refuses the unknown source's request; a plain printer takes the job instead."""
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    from backend.app.services.printer_manager import printer_manager

    mqtt = printer_manager._clients[printer.id]
    plain = await a_second_printer(db_session, printer_factory, monkeypatch, mqtt, swap_mode=False)
    library = await a_library(db_session, tmp_path, fact=None)
    auto = await an_auto_job(db_session, library, monkeypatch=monkeypatch)

    await AutoQueueScheduler().tick()

    (placed,) = (await db_session.execute(select(PrintQueueItem))).scalars().all()
    assert placed.queue_id == plain.id
    assert placed.execute_swap_macros is False  # no swap mode there: the preference asks for nothing
    await db_session.refresh(auto)
    assert auto.status == "assigned"


async def test_auto_item_fails_when_every_candidate_refuses_the_unknown_source(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
    from backend.app.services.filament_intake import routing_detail

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    library = await a_library(db_session, tmp_path, fact=None)
    auto = await an_auto_job(db_session, library, monkeypatch=monkeypatch)

    await AutoQueueScheduler().tick()

    assert (await db_session.execute(select(PrintQueueItem))).scalars().all() == []
    await db_session.refresh(auto)
    assert auto.status == "failed"
    assert auto.waiting_reason == routing_detail(sc.UNKNOWN_SOURCE_REASON)["message"]
    assert getattr(auto, "assigned_to_item_id", None) is None


async def test_a_busy_plain_printer_keeps_the_job_waiting_instead_of_failing(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """The idle swap printer refuses the unknown source's request; the plain printer that would print it as it is
    is busy. A refusal by policy does not close that candidate: the job waits for it instead of failing."""
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
    from backend.app.services.printer_manager import printer_manager

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    plain = await a_second_printer(
        db_session, printer_factory, monkeypatch, printer_manager._clients[printer.id], swap_mode=False
    )
    (await db_session.get(PrinterQueue, plain.id)).status = "printing"
    await db_session.commit()
    library = await a_library(db_session, tmp_path, fact=None)
    auto = await an_auto_job(db_session, library, monkeypatch=monkeypatch)

    await AutoQueueScheduler().tick()

    assert (await db_session.execute(select(PrintQueueItem))).scalars().all() == []
    await db_session.refresh(auto)
    assert auto.status == "pending"


async def test_a_busy_swap_printer_is_no_reason_to_wait(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    """Only swap printers would print it, the idle one refused and the busy one would refuse too: nothing to wait
    for, so the job fails with its reason and Retry."""
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler
    from backend.app.services.printer_manager import printer_manager

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    other = await a_second_printer(
        db_session, printer_factory, monkeypatch, printer_manager._clients[printer.id], swap_mode=True
    )
    (await db_session.get(PrinterQueue, other.id)).status = "printing"
    await db_session.commit()
    library = await a_library(db_session, tmp_path, fact=None)
    auto = await an_auto_job(db_session, library, monkeypatch=monkeypatch)

    await AutoQueueScheduler().tick()

    await db_session.refresh(auto)
    assert auto.status == "failed"


async def test_no_candidate_at_all_is_not_a_swap_failure(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.auto_queue_scheduler import AutoQueueScheduler

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    library = await a_library(db_session, tmp_path, fact=None)
    auto = await an_auto_job(db_session, library, monkeypatch=monkeypatch)
    queue.status = "printing"  # the only printer is busy: nothing to choose, nothing refused
    await db_session.commit()

    await AutoQueueScheduler().tick()

    await db_session.refresh(auto)
    assert auto.status == "pending"


# --- PATCH single / batch / bulk: the job's own snapshot decides (§8, T4.3) ------------------------------------------


async def pending_job(db, sessions, tmp_path, queue, *, fact, batch_id=None, name="lamp.gcode.3mf"):
    from backend.app.services.queue_source_descriptor import SOURCE_SNAPSHOT_VERSION

    library = await a_library(db, tmp_path, fact=not fact if fact is not None else False, name=name)

    async def attach(session, source):
        return None

    receipt = await queue_sources.capture(
        queue_sources.CaptureRequest(path=Path(library.file_path), format="3mf", display_filename=library.filename)
    )
    blob = await queue_sources.publish(receipt, attach, session_factory=sessions)
    item = PrintQueueItem(
        queue_id=queue.id,
        status="pending",
        position=1,
        library_file_id=library.id,
        queue_source_id=blob.id,
        source_snapshot={
            "version": SOURCE_SNAPSHOT_VERSION,
            "provenance": {"kind": "library_file", "id": library.id},
            "display_filename": library.filename,
            "format": "3mf",
            "plate_fallback": None,
            **sc.snapshot_fields(sc.SourceFact(fact) if fact is not None else sc.UNKNOWN_SOURCE),
        },
        plate_id=PLATE,
        batch_id=batch_id,
        execute_swap_macros=False,
        swap_macro_events=None,
    )
    db.add(item)
    await db.commit()
    return item


ASK = {"execute_swap_macros": True, "swap_macro_events": START}


@pytest.mark.parametrize(("fact", "status", "stored"), [(True, 200, False), (False, 200, True), (None, 409, None)])
async def test_patch_single_batch_bulk_uses_job_snapshot(
    fact, status, stored, committing_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    single = await pending_job(db_session, sessions, tmp_path, queue, fact=fact, name="single.gcode.3mf")
    batched = await pending_job(db_session, sessions, tmp_path, queue, fact=fact, batch_id="b-1", name="b.gcode.3mf")
    bulked = await pending_job(db_session, sessions, tmp_path, queue, fact=fact, name="bulk.gcode.3mf")

    responses = [
        await committing_client.patch(f"/api/v1/queue/{single.id}", json=ASK),
        await committing_client.patch("/api/v1/queue/batch/b-1", json=ASK),
        await committing_client.patch("/api/v1/queue/bulk", json={"item_ids": [bulked.id], **ASK}),
    ]

    assert [response.status_code for response in responses] == [status] * 3, [r.text for r in responses]
    for row in (single, batched, bulked):
        row = await db_session.get(PrintQueueItem, row.id, populate_existing=True)
        if status == 409:
            assert row.execute_swap_macros is False and row.swap_macro_events is None  # untouched
        else:
            assert row.execute_swap_macros is stored
            assert (json.loads(row.swap_macro_events) if row.swap_macro_events else None) == (START if stored else None)
    if status == 409:
        assert responses[0].json()["detail"]["code"] == sc.UNKNOWN_SOURCE_REASON


async def test_batch_patch_policy_is_atomic(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    plain = await pending_job(db_session, sessions, tmp_path, queue, fact=False, batch_id="b-2", name="p.gcode.3mf")
    unknown = await pending_job(db_session, sessions, tmp_path, queue, fact=None, batch_id="b-2", name="u.gcode.3mf")

    response = await committing_client.patch("/api/v1/queue/batch/b-2", json=ASK)

    assert response.status_code == 409, response.text
    for row in (plain, unknown):
        row = await db_session.get(PrintQueueItem, row.id, populate_existing=True)
        assert row.execute_swap_macros is False and row.swap_macro_events is None  # nobody was written


@pytest.mark.parametrize(
    ("fact", "codes", "stored"),
    [(False, [200, 200, 200], True), (None, [409], True), (True, [200, 200, 200], False)],
)
async def test_a_move_keeps_the_swap_request_for_the_way_back(
    fact, codes, stored, committing_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A move is not an edit of the request: on a printer without swap mode the job keeps what was asked (dispatch
    mutes it there, per printer), so moving it back asks again. Only the target may refuse it -- an unknown source on
    a swap printer -- and a baked source stays muted."""
    from backend.app.services.printer_manager import printer_manager

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    plain = await a_second_printer(
        db_session, printer_factory, monkeypatch, printer_manager._clients[printer.id], swap_mode=False
    )
    job = await pending_job(db_session, sessions, tmp_path, queue, fact=fact)
    job.queue_id = plain.id
    job.execute_swap_macros, job.swap_macro_events = True, json.dumps(START)
    await db_session.commit()

    async def move(target):
        # A job with no routing intent reads as pinned; moving it says the slots are to be chosen again.
        return await committing_client.patch(
            f"/api/v1/queue/{job.id}", json={"queue_id": target.id, "manual_mapping": False}
        )

    answers = [await move(queue)]
    if answers[-1].status_code == 200:
        answers += [await move(plain), await move(queue)]

    assert [answer.status_code for answer in answers] == codes, [answer.text for answer in answers]
    row = await db_session.get(PrintQueueItem, job.id, populate_existing=True)
    assert row.queue_id == (queue.id if codes[-1] == 200 else plain.id)
    assert row.execute_swap_macros is stored
    assert (json.loads(row.swap_macro_events) if row.swap_macro_events else None) == (START if stored else None)


@pytest.mark.parametrize(("fact", "code", "stored"), [(None, 409, True), (True, 200, False)])
async def test_a_batch_move_asks_the_targets_swap_rule_like_a_single_move(
    fact, code, stored, committing_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """G03 follow-up: the batch PATCH judged the swap columns without the target (``_swap_update(..., None)``),
    so copies asking for our macros over an unknown source moved onto a swap printer, and a baked source kept
    them asked for there. Now the target decides, for the whole batch or none of it."""
    from backend.app.services.printer_manager import printer_manager

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    plain = await a_second_printer(
        db_session, printer_factory, monkeypatch, printer_manager._clients[printer.id], swap_mode=False
    )
    jobs = [
        await pending_job(db_session, sessions, tmp_path, queue, fact=fact, batch_id="b-move", name=f"{n}.gcode.3mf")
        for n in ("one", "two")
    ]
    for job in jobs:
        job.queue_id = plain.id
        job.execute_swap_macros, job.swap_macro_events = True, json.dumps(START)
    await db_session.commit()

    response = await committing_client.patch(
        f"/api/v1/queue/batch/b-move?queue_id={plain.id}", json={"queue_id": queue.id, "manual_mapping": False}
    )

    assert response.status_code == code, response.text
    for job in jobs:
        row = await db_session.get(PrintQueueItem, job.id, populate_existing=True)
        assert row.queue_id == (queue.id if code == 200 else plain.id)
        assert row.execute_swap_macros is stored


# --- the order plan: printer and auto both go through the captured fact (§8, T4.1) ---------------------------------


class _PlanProfile:
    def for_printer_queue(self):
        return {"execute_swap_macros": True, "swap_macro_events": list(START)}

    def for_auto_queue(self):
        return {"execute_swap_macros": True, "swap_macro_events": list(START)}


@pytest.mark.parametrize("declared", ["unknown", "baked", "baked-then-changed"])
async def test_order_plan_checks_printer_and_auto_receipts(
    declared, committing_client, db_session, mixed_order, printer_factory, monkeypatch, sessions
):
    from unittest.mock import AsyncMock

    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.tests.integration.test_order_print_options import enqueue

    pid, plates = mixed_order
    if declared.startswith("baked"):
        # A real declaration: a valid hash over the very bytes, declaring a baked sequence.
        for row in (await db_session.execute(select(LibraryFile))).scalars():
            row.file_hash = hashlib.sha256(Path(row.file_path).read_bytes()).hexdigest()
            row.swap_compatible = True
            if declared == "baked-then-changed":
                # ...over bytes that have changed since: the row still says baked, the copy cannot confirm it.
                import zipfile

                with zipfile.ZipFile(row.file_path, "a") as edited:
                    edited.writestr("Metadata/edited.txt", "changed after the row was hashed")
        await db_session.commit()
    printer = await printer_factory(model="H2D")
    printer.swap_mode_enabled = True
    db_session.add(PrinterQueue(printer_id=printer.id))
    await db_session.commit()
    monkeypatch.setattr("backend.app.api.routes.projects.preference_options", AsyncMock(return_value=_PlanProfile()))

    response = await enqueue(committing_client, pid, plates, printer_id=printer.id, models={"H2D"})

    if declared != "baked":
        # Nothing proves the declaration of these bytes: the request is refused, nothing lands.
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == sc.UNKNOWN_SOURCE_REASON
        assert (await db_session.execute(select(PrintQueueItem))).scalars().all() == []
    else:
        assert response.status_code == 200, response.text
        rows = (await db_session.execute(select(PrintQueueItem))).scalars().all()
        assert rows and all(row.execute_swap_macros is False for row in rows)

    # The auto target creates its router rows: the decision is the assignment's, per printer, from the snapshot.
    response = await enqueue(committing_client, pid, plates, models={"P2S"})
    assert response.status_code == 200, response.text
    autos = (await db_session.execute(select(AutoQueueItem))).scalars().all()
    assert autos and all(auto.queue_source_id is not None for auto in autos)


async def test_an_explicit_edit_or_delete_ends_the_capture_cooldown(
    committing_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    from backend.app.services.print_scheduler import scheduler

    printer, queue = await a_swap_printer(db_session, tmp_path, printer_factory, monkeypatch)
    edited = await pending_job(db_session, sessions, tmp_path, queue, fact=False, name="e.gcode.3mf")
    deleted = await pending_job(db_session, sessions, tmp_path, queue, fact=False, name="d.gcode.3mf")
    monkeypatch.setattr(
        scheduler, "_capture_cooldowns", {edited.id: time.monotonic() + 60, deleted.id: time.monotonic() + 60}
    )

    assert (
        await committing_client.patch(f"/api/v1/queue/{edited.id}", json={"manual_start": False})
    ).status_code == 200
    assert (await committing_client.delete(f"/api/v1/queue/{deleted.id}")).status_code == 200

    assert scheduler._capture_cooldowns == {}
