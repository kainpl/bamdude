"""Moving or re-running a queued job reuses ITS bytes — spec §7, §9, A01/A08.

Spec: ``60-specs/queue-source-spool-spec.md``. Task 6 made every dispatch path
read the job's frozen copy; this file is about the operations that *move or reuse*
a job afterwards — clone, retry, unskip, the plate-repeat answer, and the edits
that change a job's printer, plate or schedule. The claim under test is the same
one twice over:

* **no new read of the original, ever** (A01/A08). The original file stays on disk
  and every door to it raises — ``forbid_reads`` from
  ``test_dispatch_without_original`` is reused rather than re-written, because a
  weaker trap (patching ``os.stat`` but not ``os.path.isfile``, say) would make
  every ``touched == []`` assertion below assert nothing on Windows;
* **an existing blob is attached under the storage guard** (§9, Task 3's C1). The
  writer asks the row's state under the guard and refuses anything but ``ready``,
  so the collector cannot unlink the bytes between the check and the write.

``test_the_state_is_decided_under_the_guard_not_before_it`` is the barrier test
for the second claim: it breaks the blob *while the writer waits on the guard*, so
a writer that had read ``ready`` before acquiring would go on and write a
reference to bytes that are gone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core import database
from backend.app.models.archive import PrintArchive
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import STATE_BROKEN, STATE_READY, QueueSource
from backend.app.schemas.print_queue import PrintQueueItemCreate
from backend.app.services import queue_ops, queue_sources
from backend.app.services.plate_hold import RepeatNotPossible, answer_by_repeating
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf
from backend.tests.integration.test_dispatch_without_original import forbid_reads
from backend.tests.integration.test_filament_routing_dispatch import setup_source

pytestmark = pytest.mark.integration

PLATE = 15
#: A second plate that really is in the captured file, so "the job was moved to
#: another plate" can be observed rather than asserted against the value it already
#: had. ``ABSENT_PLATE`` is in neither.
SECOND_PLATE = 18
ABSENT_PLATE = 22


@pytest.fixture(autouse=True)
def clean_spool_state():
    """Process-global capture state must not travel between tests."""
    queue_sources._reset_state()
    yield
    deadline = time.monotonic() + 10
    while queue_sources.active_captures() and time.monotonic() < deadline:  # pragma: no cover - drain
        time.sleep(0.01)
    queue_sources._reset_state()


@pytest.fixture
def sessions(test_engine, monkeypatch):
    """``queue_sources.publish`` owns its transaction, so it needs this engine."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", factory)
    return factory


def a_sliced_file(path: Path) -> Path:
    """A two-plate sliced 3MF — two, so a plate CHANGE has somewhere to go."""
    return write_routing_3mf(
        path,
        {
            PLATE: [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "1.5"}],
            SECOND_PLATE: [{"id": 3, "type": "PLA", "color": "#FF0000", "used_g": "2.5"}],
        },
        model="P1P",
        prediction=3600,
    )


async def a_library_source(db, tmp_path, *, name="lamp.gcode.3mf") -> LibraryFile:
    path = a_sliced_file(tmp_path / name)
    row = LibraryFile(
        filename=path.name,
        file_path=str(path),
        file_size=path.stat().st_size,
        file_type="gcode",
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    db.add(row)
    await db.commit()
    return row


async def blob_count(db) -> int:
    return int(await db.scalar(select(func.count()).select_from(QueueSource)) or 0)


async def a_captured_item(db, tmp_path, printer_factory, monkeypatch, *, name="lamp.gcode.3mf"):
    """One queued job that owns a copy of its source, and the original's path.

    Goes through the real ``queue_add`` door so the blob, the snapshot and the
    routing intent are the ones a farm would have.
    """
    _unused, printer, queue, _mqtt = await setup_source(db, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db, tmp_path, name=name)
    original = Path(source.file_path)

    from backend.app.services.queue_add import add_items_to_printer_queue

    items, _q = await add_items_to_printer_queue(
        db,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE),
        None,
    )
    item = items[0]
    assert item.queue_source_id is not None
    return item, printer, queue, source, original


async def lose_the_original(db, item, original: Path, *, row) -> None:
    """The case the feature exists for: the source row and its file are gone."""
    item.archive_id = None
    item.library_file_id = None
    await db.delete(row)
    await db.commit()
    original.unlink()


async def break_the_blob(db, item) -> QueueSource:
    blob = await db.get(QueueSource, item.queue_source_id)
    blob.state = STATE_BROKEN
    await db.commit()
    return blob


async def break_the_blob_elsewhere(sessions, blob_id: int) -> None:
    """Flip the blob to ``broken`` from ANOTHER session — the collector's position.

    The collector writes from its own session, so a writer that trusted the object
    already in its identity map would never see this. Breaking it through
    ``db_session`` instead (which the simple refusal tests above do, and which is
    fine for what they claim) leaves the caller's instance already ``broken``, and
    then a plain ``db.get`` answers correctly by accident.
    """
    async with sessions() as other:
        blob = await other.get(QueueSource, blob_id)
        blob.state = STATE_BROKEN
        await other.commit()


# --------------------------------------------------------------------------- #
# A08 — a clone is a second owner of the SAME bytes
# --------------------------------------------------------------------------- #


async def test_a_clone_carries_the_blob_and_never_reads_the_original(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    item, _printer, _queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    blob_id, snapshot = item.queue_source_id, dict(item.source_snapshot)
    await lose_the_original(db_session, item, original, row=source)

    touched = forbid_reads(monkeypatch, original)
    clone = await queue_ops.clone_item(db_session, item.id)

    assert touched == []
    assert clone is not None
    assert clone.queue_source_id == blob_id, "a clone prints the same bytes, it does not re-read anything"
    assert clone.source_snapshot == snapshot, "the display name and plate fallback travel with the copy"
    assert await blob_count(db_session) == 1, "a clone must not capture a second copy of the same bytes"


async def test_copy_queue_add_reuses_a_snapshot_after_its_original_is_gone(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Copy Queue attaches the source job's managed bytes, never its old path."""
    from backend.app.services.queue_add import add_items_to_printer_queue

    item, _printer, queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    blob_id, snapshot = item.queue_source_id, dict(item.source_snapshot)
    await lose_the_original(db_session, item, original, row=source)

    touched = forbid_reads(monkeypatch, original)
    copies, _queue = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, source_queue_item_id=item.id, plate_id=PLATE),
        None,
    )

    assert touched == []
    assert len(copies) == 1
    assert copies[0].queue_source_id == blob_id
    assert copies[0].source_snapshot == snapshot
    assert copies[0].archive_id is None and copies[0].library_file_id is None
    assert await blob_count(db_session) == 1


async def test_copy_queue_run_next_keeps_two_plates_as_one_saved_source_block(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Urgent multi-plate copies reuse bytes and do not reverse their plates."""
    from backend.app.services.queue_add import add_next_block_to_printer_queue

    item, _printer, queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    blob_id = item.queue_source_id
    await lose_the_original(db_session, item, original, row=source)

    touched = forbid_reads(monkeypatch, original)
    copies, _queue = await add_next_block_to_printer_queue(
        db_session,
        [
            PrintQueueItemCreate(
                queue_id=queue.id,
                source_queue_item_id=item.id,
                plate_id=PLATE,
                enqueue_position="next",
            ),
            PrintQueueItemCreate(
                queue_id=queue.id,
                source_queue_item_id=item.id,
                plate_id=SECOND_PLATE,
                enqueue_position="next",
            ),
        ],
        None,
    )

    assert touched == []
    assert [copy.plate_id for copy in copies] == [PLATE, SECOND_PLATE]
    assert [copy.position for copy in copies] == [0, 1]
    assert all(copy.queue_source_id == blob_id for copy in copies)
    assert await blob_count(db_session) == 1


async def test_copy_source_profile_comes_from_snapshot_after_original_is_gone(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    item, _printer, _queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    await lose_the_original(db_session, item, original, row=source)

    response = await async_client.get(f"/api/v1/queue/{item.id}/copy-source")

    assert response.status_code == 200, response.text
    profile = response.json()
    assert profile["item_id"] == item.id
    assert profile["filename"] == "lamp.gcode.3mf"
    assert profile["sliced_for_model"] == "P1P"
    assert [plate["index"] for plate in profile["plates"]] == [PLATE, SECOND_PLATE]
    assert all(plate["thumbnail_url"] is None for plate in profile["plates"])


async def test_a_batch_clone_carries_every_siblings_blob(db_session, tmp_path, printer_factory, monkeypatch, sessions):
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    original = Path(source.file_path)
    items, _q = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE, quantity=2),
        None,
    )
    batch_id = items[0].batch_id
    assert batch_id and len(items) == 2
    blob_id = items[0].queue_source_id
    for item in items:
        item.archive_id = None
        item.library_file_id = None
    await db_session.delete(source)
    await db_session.commit()
    original.unlink()

    touched = forbid_reads(monkeypatch, original)
    clones = await queue_ops.clone_batch(db_session, batch_id)

    assert touched == []
    assert len(clones) == 2
    assert {clone.queue_source_id for clone in clones} == {blob_id}
    assert await blob_count(db_session) == 1


async def test_a_clone_is_refused_when_the_bytes_are_broken(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """§9: a writer that may not print those bytes may not hand them to a new row."""
    item, _printer, _queue, _source, _original = await a_captured_item(
        db_session, tmp_path, printer_factory, monkeypatch
    )
    await break_the_blob(db_session, item)

    with pytest.raises(queue_sources.QueueSourceError) as exc:
        await queue_ops.clone_item(db_session, item.id)

    assert exc.value.reason == "source_unreadable"
    rows = (await db_session.execute(select(PrintQueueItem))).scalars().all()
    assert len(rows) == 1, "nothing may be written when the bytes were refused"


async def test_the_clone_route_answers_the_taxonomys_status(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    item, _printer, _queue, _source, _original = await a_captured_item(
        db_session, tmp_path, printer_factory, monkeypatch
    )
    await break_the_blob(db_session, item)

    response = await async_client.post(f"/api/v1/queue/{item.id}/clone")

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "source_unreadable"


@pytest.mark.parametrize("scope", ["one", "batch"])
async def test_the_BATCH_clone_route_answers_the_taxonomy_too(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions, scope
):
    """The SECOND door onto the same two services, and both of its scopes.

    Unmapped, a known refusal left this route as a bare 500 — indistinguishable on
    the operator's screen from BamDude being broken, and the frontend branches on
    ``detail.code``. Parametrized because ``scope='one'`` goes through
    ``clone_item`` and ``scope='batch'`` through ``clone_batch``: two call sites,
    one mapping.
    """
    from backend.app.services.queue_add import add_items_to_printer_queue

    _unused, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    source = await a_library_source(db_session, tmp_path)
    items, _q = await add_items_to_printer_queue(
        db_session,
        PrintQueueItemCreate(queue_id=queue.id, library_file_id=source.id, plate_id=PLATE, quantity=2),
        None,
    )
    batch_id = items[0].batch_id
    assert batch_id
    await break_the_blob(db_session, items[0])

    response = await async_client.post(f"/api/v1/queue/batch/{batch_id}/clone?scope={scope}")

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "source_unreadable"
    rows = (await db_session.execute(select(PrintQueueItem))).scalars().all()
    assert len(rows) == 2, "nothing may be written when the bytes were refused"


async def test_a_legacy_row_is_cloned_exactly_as_before(db_session, tmp_path, printer_factory, monkeypatch):
    """No snapshot, no guard, no refusal — a farm that never captured pays nothing."""
    _unused, _printer, queue, _mqtt = await setup_source(db_session, tmp_path, printer_factory, monkeypatch)
    item = PrintQueueItem(queue_id=queue.id, status="pending", position=1, library_file_id=None, archive_id=None)
    db_session.add(item)
    await db_session.commit()

    clone = await queue_ops.clone_item(db_session, item.id)

    assert clone is not None and clone.queue_source_id is None


# --------------------------------------------------------------------------- #
# A08 — retry and unskip re-arm the row the bytes are already on
# --------------------------------------------------------------------------- #


async def test_a_retry_re_arms_without_reading_the_original(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    item, _printer, _queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    blob_id = item.queue_source_id
    await lose_the_original(db_session, item, original, row=source)
    item.status = "failed"
    item.error_message = "boom"
    await db_session.commit()

    touched = forbid_reads(monkeypatch, original)
    response = await async_client.post(f"/api/v1/queue/{item.id}/retry")

    assert touched == []
    assert response.status_code == 200, response.text
    assert response.json()["source_storage"] == "ready"
    db_session.expire_all()
    await db_session.refresh(item)
    assert (item.status, item.queue_source_id, item.error_message) == ("pending", blob_id, None)
    assert await blob_count(db_session) == 1


async def test_a_retry_is_refused_when_the_bytes_are_broken(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    item, _printer, _queue, _source, _original = await a_captured_item(
        db_session, tmp_path, printer_factory, monkeypatch
    )
    item.status = "failed"
    await db_session.commit()
    await break_the_blob(db_session, item)

    response = await async_client.post(f"/api/v1/queue/{item.id}/retry")

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "source_unreadable"
    db_session.expire_all()
    await db_session.refresh(item)
    assert item.status == "failed", "a refused retry leaves the row terminal, not half re-armed"


async def test_an_unskip_re_arms_without_reading_the_original(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    item, _printer, _queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    blob_id = item.queue_source_id
    await lose_the_original(db_session, item, original, row=source)
    item.status = "skipped"
    await db_session.commit()

    touched = forbid_reads(monkeypatch, original)
    response = await async_client.post(f"/api/v1/queue/{item.id}/unskip")

    assert touched == []
    assert response.status_code == 200, response.text
    db_session.expire_all()
    await db_session.refresh(item)
    assert (item.status, item.queue_source_id) == ("pending", blob_id)
    assert await blob_count(db_session) == 1


async def test_an_unskip_is_refused_when_the_bytes_are_broken(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    item, _printer, _queue, _source, _original = await a_captured_item(
        db_session, tmp_path, printer_factory, monkeypatch
    )
    item.status = "skipped"
    await db_session.commit()
    await break_the_blob(db_session, item)

    response = await async_client.post(f"/api/v1/queue/{item.id}/unskip")

    assert response.status_code == 422, response.text
    db_session.expire_all()
    await db_session.refresh(item)
    assert item.status == "skipped"


# --------------------------------------------------------------------------- #
# The plate answer — Repeat on a job whose only original row is gone
# --------------------------------------------------------------------------- #


async def a_waiting_row(db, tmp_path, printer_factory, monkeypatch, *, snapshot=True):
    """A completed row a printer is waiting to be asked about.

    Built through the real add door when it is snapshot-backed, so the row that
    answers Repeat is the one a farm would have, and then reduced to the state the
    refusal was written for: an archive with no file on disk and no library row.
    """
    item, printer, queue, source, original = await a_captured_item(db, tmp_path, printer_factory, monkeypatch)
    printer.require_plate_clear = True
    archive = PrintArchive(
        printer_id=printer.id, filename="lamp.gcode.3mf", file_path="", file_size=0, status="completed"
    )
    db.add(archive)
    await db.flush()
    item.status = "completed"
    item.archive_id = archive.id
    item.library_file_id = None
    if not snapshot:
        item.queue_source_id = None
        item.source_snapshot = None
    await db.delete(source)
    await db.commit()
    original.unlink()
    return item, printer, original


async def test_repeat_re_arms_a_snapshot_backed_row_whose_archive_has_no_file(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """The refusal was right when the archive was the only source of bytes.

    It is wrong now: the job owns a copy, and the dispatcher would print it. The
    operator is standing at the printer, so this must not answer "no file".
    """
    item, printer, original = await a_waiting_row(db_session, tmp_path, printer_factory, monkeypatch)
    blob_id = item.queue_source_id

    touched = forbid_reads(monkeypatch, original)
    again = await answer_by_repeating(db_session, printer.id)

    assert touched == []
    assert again is not None and again.id == item.id
    assert (again.status, again.queue_source_id) == ("pending", blob_id)
    assert again.dispatch_attempts == 0


async def test_repeat_still_refuses_a_legacy_row_with_no_bytes(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    item, printer, _original = await a_waiting_row(db_session, tmp_path, printer_factory, monkeypatch, snapshot=False)

    with pytest.raises(RepeatNotPossible):
        await answer_by_repeating(db_session, printer.id)

    await db_session.refresh(item)
    assert item.status == "completed", "the row must stay waiting, not be half re-armed"


async def test_repeat_refuses_a_snapshot_whose_bytes_are_broken(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    item, printer, _original = await a_waiting_row(db_session, tmp_path, printer_factory, monkeypatch)
    await break_the_blob(db_session, item)

    with pytest.raises(RepeatNotPossible):
        await answer_by_repeating(db_session, printer.id)

    await db_session.refresh(item)
    assert item.status == "completed"


# --------------------------------------------------------------------------- #
# §9 — the state is asked under the guard, then written
# --------------------------------------------------------------------------- #


async def test_the_state_is_decided_under_the_guard_not_before_it(
    db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Break the blob while the writer waits on the guard — **from another session**.

    Two claims in one, and the second is why the other session matters:

    * the writer must not have decided anything before it holds the guard. One that
      read ``state`` first would already have said ``ready`` and would write a
      reference to bytes the collector has released — the race §9 puts clone on the
      same guard to close;
    * and the read it does under the guard must reach the DATABASE. The caller's
      session is deliberately warmed with the blob as ``ready`` first, so a writer
      that trusted its identity map would be answered by that stale object. The
      collector writes from its own session, which is exactly this shape, and
      ``populate_existing=True`` is the thing under test.
    """
    item, _printer, _queue, _source, _original = await a_captured_item(
        db_session, tmp_path, printer_factory, monkeypatch
    )
    blob_id = item.queue_source_id
    cached = await db_session.get(QueueSource, blob_id)
    assert cached.state == STATE_READY

    async with queue_sources.storage_mutation():
        clone = asyncio.create_task(queue_ops.clone_item(db_session, item.id))
        for _ in range(20):
            await asyncio.sleep(0.005)
        assert not clone.done(), "the clone must wait for the guard before deciding anything"
        await break_the_blob_elsewhere(sessions, blob_id)
        assert cached.state == STATE_READY, "the caller's session must still be holding the stale value"

    with pytest.raises(queue_sources.QueueSourceError):
        await clone
    assert len((await db_session.execute(select(PrintQueueItem))).scalars().all()) == 1


# --------------------------------------------------------------------------- #
# A03 / A08 — an EDIT never re-captures, and a scope change reads the copy
# --------------------------------------------------------------------------- #


async def test_editing_the_schedule_never_re_captures(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    item, _printer, _queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    blob_id, snapshot = item.queue_source_id, dict(item.source_snapshot)
    await lose_the_original(db_session, item, original, row=source)

    touched = forbid_reads(monkeypatch, original)
    response = await async_client.patch(
        f"/api/v1/queue/{item.id}", json={"scheduled_time": "2030-01-01T10:00:00", "auto_off_after": True}
    )

    assert touched == []
    assert response.status_code == 200, response.text
    db_session.expire_all()
    await db_session.refresh(item)
    assert (item.queue_source_id, item.source_snapshot) == (blob_id, snapshot)
    assert await blob_count(db_session) == 1


async def test_moving_a_job_to_another_printer_keeps_its_bytes(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """A08: changing the printer is a routing question, not a new source."""
    item, _printer, _queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    blob_id = item.queue_source_id
    other = await printer_factory(model="P1P", name="Second")
    other_queue = PrinterQueue(id=other.id, printer_id=other.id)
    db_session.add(other_queue)
    await db_session.commit()
    other_queue_id = other_queue.id
    await lose_the_original(db_session, item, original, row=source)

    touched = forbid_reads(monkeypatch, original)
    response = await async_client.patch(f"/api/v1/queue/{item.id}", json={"queue_id": other_queue_id})

    assert touched == []
    assert response.status_code == 200, response.text
    db_session.expire_all()
    await db_session.refresh(item)
    assert (item.queue_id, item.queue_source_id) == (other_queue_id, blob_id)
    assert await blob_count(db_session) == 1


async def test_changing_the_plate_keeps_the_bytes_and_resolves_the_NEW_plate_from_the_copy(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Moved to a DIFFERENT plate the captured file has — not to the one it already had.

    The plate the row ends up with is whatever ``prepare_routing`` resolved out of
    the bytes it read, so patching to the value the row already carried could not
    fail. Here the job starts on ``PLATE`` and is moved to ``SECOND_PLATE``: if the
    copy were not read, or the plate were ignored, the row would keep ``PLATE`` (or
    the request would be refused for a plate that is in fact there).
    """
    item, _printer, _queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    blob_id = item.queue_source_id
    assert item.plate_id == PLATE
    await lose_the_original(db_session, item, original, row=source)

    touched = forbid_reads(monkeypatch, original)
    response = await async_client.patch(f"/api/v1/queue/{item.id}", json={"plate_id": SECOND_PLATE})

    assert touched == []
    assert response.status_code == 200, response.text
    assert response.json()["plate_id"] == SECOND_PLATE
    db_session.expire_all()
    await db_session.refresh(item)
    assert (item.plate_id, item.queue_source_id) == (SECOND_PLATE, blob_id)
    # The intent was re-written about the same bytes, and about the new plate.
    assert json.loads(item.filament_routing)["resolved_plate_id"] == SECOND_PLATE
    assert await blob_count(db_session) == 1


async def test_a_plate_the_captured_file_does_not_have_is_refused_from_the_copy(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """And the refusal comes out of the copy, with the original unreachable."""
    item, _printer, _queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    await lose_the_original(db_session, item, original, row=source)

    touched = forbid_reads(monkeypatch, original)
    response = await async_client.patch(f"/api/v1/queue/{item.id}", json={"plate_id": ABSENT_PLATE})

    assert touched == []
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] in ("plate_not_found", "plate_gcode_missing")


async def test_a_pinned_job_moved_to_another_queue_asks_for_a_mapping_review(
    async_client, db_session, tmp_path, printer_factory, monkeypatch, sessions
):
    """Changing the printer keeps the bytes AND runs the mapping review (A08)."""
    item, _printer, _queue, source, original = await a_captured_item(db_session, tmp_path, printer_factory, monkeypatch)
    stored = json.loads(item.filament_routing)
    stored["mode"] = "pinned"
    item.filament_routing = json.dumps(stored)
    item.ams_mapping = json.dumps([0])
    other = await printer_factory(model="P1P", name="Second")
    other_queue = PrinterQueue(id=other.id, printer_id=other.id)
    db_session.add(other_queue)
    await db_session.commit()
    await lose_the_original(db_session, item, original, row=source)

    touched = forbid_reads(monkeypatch, original)
    response = await async_client.patch(f"/api/v1/queue/{item.id}", json={"queue_id": other_queue.id})

    assert touched == []
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "mapping_review_required"
