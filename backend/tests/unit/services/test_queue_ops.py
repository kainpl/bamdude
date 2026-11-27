"""Unit tests for queue_ops helpers (reorder / bump / clone / status).

Covers the batch-aware block operations exposed to the queue command
routes.  All tests hit an in-memory SQLite via the shared ``db_session``
fixture; no HTTP, no mocks of queue_ops itself.
"""

import uuid

import pytest
from sqlalchemy import select

# ⚠️ Side effect, not the name: Printer declares its PrinterLocation
# relationship by string, and SQLAlchemy cannot resolve it unless this module
# has been imported. Without it the file passes only when another test in the
# run imported it first.
import backend.app.models.printer_location  # noqa: F401
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import queue_ops


async def _make_printer_queue(db_session, counter: int) -> PrinterQueue:
    printer = Printer(
        name=f"Queue Test Printer {counter}",
        serial_number=f"QUEUE{counter:010d}",
        ip_address=f"192.168.42.{counter}",
        access_code="12345678",
        model="X1C",
    )
    db_session.add(printer)
    await db_session.commit()
    await db_session.refresh(printer)

    queue = PrinterQueue(printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    await db_session.refresh(queue)
    return queue


@pytest.fixture
async def queue(db_session):
    """A single fresh PrinterQueue for the test."""
    return await _make_printer_queue(db_session, counter=1)


@pytest.fixture
async def queue_b(db_session):
    """A second PrinterQueue — used to verify ops don't cross queue boundaries."""
    return await _make_printer_queue(db_session, counter=2)


async def _add_item(
    db_session,
    queue_id: int,
    position: int,
    *,
    status: str = "pending",
    batch_id: str | None = None,
    manual_start: bool = False,
    archive_id: int | None = None,
) -> PrintQueueItem:
    """Insert a queue item directly — bypasses the routes on purpose."""
    item = PrintQueueItem(
        queue_id=queue_id,
        archive_id=archive_id,
        position=position,
        status=status,
        batch_id=batch_id,
        manual_start=manual_start,
    )
    db_session.add(item)
    await db_session.commit()
    await db_session.refresh(item)
    return item


async def _positions(db_session, queue_id: int) -> list[tuple[int, int]]:
    """Return [(item_id, position), ...] ordered by position — pending only."""
    rows = (
        (
            await db_session.execute(
                select(PrintQueueItem)
                .where(PrintQueueItem.queue_id == queue_id)
                .where(PrintQueueItem.status == "pending")
                .order_by(PrintQueueItem.position)
            )
        )
        .scalars()
        .all()
    )
    return [(r.id, r.position) for r in rows]


class TestPlacePendingBlock:
    async def test_next_precedes_and_reindexes_only_pending_rows(self, db_session, queue):
        old_a = await _add_item(db_session, queue.id, position=4)
        old_b = await _add_item(db_session, queue.id, position=9)
        printing = await _add_item(db_session, queue.id, position=0, status="printing")
        urgent_a = PrintQueueItem(queue_id=queue.id, status="pending")
        urgent_b = PrintQueueItem(queue_id=queue.id, status="pending")

        async with queue_ops.queue_scope_lock(db_session, queue.id):
            await queue_ops.place_pending_block(db_session, queue.id, [urgent_a, urgent_b], enqueue_position="next")
            db_session.add_all([urgent_a, urgent_b])
            await db_session.commit()

        # Pending work starts at 1 for «next» too: 0 is the printer's claim (42634ea27 fixed only «end»).
        assert await _positions(db_session, queue.id) == [
            (urgent_a.id, 1),
            (urgent_b.id, 2),
            (old_a.id, 3),
            (old_b.id, 4),
        ]
        await db_session.refresh(printing)
        assert printing.position == 0

    async def test_next_after_the_runs_own_rows_keeps_the_run_in_order(self, db_session, queue):
        """N03 (upstream #3471): the second block of one run goes behind the first, not in front of it."""
        old = await _add_item(db_session, queue.id, position=1)
        first = PrintQueueItem(queue_id=queue.id, status="pending")
        async with queue_ops.queue_scope_lock(db_session, queue.id):
            await queue_ops.place_pending_block(db_session, queue.id, [first], enqueue_position="next")
            db_session.add(first)
            await db_session.commit()
        second_a = PrintQueueItem(queue_id=queue.id, status="pending")
        second_b = PrintQueueItem(queue_id=queue.id, status="pending")

        async with queue_ops.queue_scope_lock(db_session, queue.id):
            await queue_ops.place_pending_block(
                db_session, queue.id, [second_a, second_b], enqueue_position="next", after_item_ids=[first.id]
            )
            db_session.add_all([second_a, second_b])
            await db_session.commit()

        assert await _positions(db_session, queue.id) == [
            (first.id, 1),
            (second_a.id, 2),
            (second_b.id, 3),
            (old.id, 4),
        ]

    async def test_next_after_goes_behind_the_last_of_several_anchors(self, db_session, queue):
        old = await _add_item(db_session, queue.id, position=1)
        run_a = await _add_item(db_session, queue.id, position=2)
        run_b = await _add_item(db_session, queue.id, position=3)
        tail = await _add_item(db_session, queue.id, position=4)
        new = PrintQueueItem(queue_id=queue.id, status="pending")

        async with queue_ops.queue_scope_lock(db_session, queue.id):
            await queue_ops.place_pending_block(
                db_session, queue.id, [new], enqueue_position="next", after_item_ids=[run_b.id, run_a.id]
            )
            db_session.add(new)
            await db_session.commit()

        assert [item_id for item_id, _ in await _positions(db_session, queue.id)] == [
            old.id,
            run_a.id,
            run_b.id,
            new.id,
            tail.id,
        ]

    async def test_next_after_rows_no_longer_pending_goes_first(self, db_session, queue, queue_b):
        """An anchor that started printing, or sits in another queue, does not hold the block back."""
        old = await _add_item(db_session, queue.id, position=1)
        started = await _add_item(db_session, queue.id, position=0, status="printing")
        elsewhere = await _add_item(db_session, queue_b.id, position=1)
        new = PrintQueueItem(queue_id=queue.id, status="pending")

        async with queue_ops.queue_scope_lock(db_session, queue.id):
            await queue_ops.place_pending_block(
                db_session, queue.id, [new], enqueue_position="next", after_item_ids=[started.id, elsewhere.id]
            )
            db_session.add(new)
            await db_session.commit()

        assert await _positions(db_session, queue.id) == [(new.id, 1), (old.id, 2)]

    async def test_end_preserves_existing_positions(self, db_session, queue):
        old = await _add_item(db_session, queue.id, position=9)
        appended = PrintQueueItem(queue_id=queue.id, status="pending")

        async with queue_ops.queue_scope_lock(db_session, queue.id):
            await queue_ops.place_pending_block(db_session, queue.id, [appended])
            db_session.add(appended)
            await db_session.commit()

        assert await _positions(db_session, queue.id) == [(old.id, 9), (appended.id, 10)]


# ── resolve_block_ids / get_batch_pending_items ──────────────────────────────


class TestResolveBlockIds:
    async def test_solo_item_returns_single_id(self, db_session, queue):
        item = await _add_item(db_session, queue.id, position=0)

        qid, ids = await queue_ops.resolve_block_ids(db_session, item.id)

        assert qid == queue.id
        assert ids == [item.id]

    async def test_batch_item_returns_all_pending_siblings(self, db_session, queue):
        batch = str(uuid.uuid4())
        a = await _add_item(db_session, queue.id, position=0, batch_id=batch)
        b = await _add_item(db_session, queue.id, position=1, batch_id=batch)
        c = await _add_item(db_session, queue.id, position=2, batch_id=batch)

        qid, ids = await queue_ops.resolve_block_ids(db_session, b.id)

        assert qid == queue.id
        assert set(ids) == {a.id, b.id, c.id}

    async def test_batch_excludes_non_pending_siblings(self, db_session, queue):
        batch = str(uuid.uuid4())
        a = await _add_item(db_session, queue.id, position=0, batch_id=batch, status="completed")
        b = await _add_item(db_session, queue.id, position=1, batch_id=batch, status="pending")
        c = await _add_item(db_session, queue.id, position=2, batch_id=batch, status="failed")

        _, ids = await queue_ops.resolve_block_ids(db_session, b.id)

        assert ids == [b.id]
        assert a.id not in ids
        assert c.id not in ids

    async def test_missing_id_returns_empty(self, db_session):
        qid, ids = await queue_ops.resolve_block_ids(db_session, 999_999)

        assert qid == 0
        assert ids == []

    async def test_get_batch_pending_items_order_by_position(self, db_session, queue):
        batch = str(uuid.uuid4())
        # Inserted out of order — expect ordered-by-position output.
        b = await _add_item(db_session, queue.id, position=2, batch_id=batch)
        a = await _add_item(db_session, queue.id, position=0, batch_id=batch)
        c = await _add_item(db_session, queue.id, position=1, batch_id=batch)

        items = await queue_ops.get_batch_pending_items(db_session, batch)

        assert [it.id for it in items] == [a.id, c.id, b.id]


# ── reorder_block ─────────────────────────────────────────────────────────────


class TestReorderBlock:
    async def test_solo_move_up_swaps_with_predecessor(self, db_session, queue):
        a = await _add_item(db_session, queue.id, position=0)
        b = await _add_item(db_session, queue.id, position=1)
        c = await _add_item(db_session, queue.id, position=2)

        changed = await queue_ops.reorder_block(db_session, queue.id, [b.id], "up")

        assert changed >= 2  # b + swap partner moved
        assert await _positions(db_session, queue.id) == [(b.id, 0), (a.id, 1), (c.id, 2)]

    async def test_solo_move_down_swaps_with_successor(self, db_session, queue):
        a = await _add_item(db_session, queue.id, position=0)
        b = await _add_item(db_session, queue.id, position=1)
        c = await _add_item(db_session, queue.id, position=2)

        await queue_ops.reorder_block(db_session, queue.id, [b.id], "down")

        assert await _positions(db_session, queue.id) == [(a.id, 0), (c.id, 1), (b.id, 2)]

    async def test_solo_at_top_up_is_noop(self, db_session, queue):
        a = await _add_item(db_session, queue.id, position=0)
        b = await _add_item(db_session, queue.id, position=1)

        changed = await queue_ops.reorder_block(db_session, queue.id, [a.id], "up")

        assert changed == 0
        assert await _positions(db_session, queue.id) == [(a.id, 0), (b.id, 1)]

    async def test_solo_at_bottom_down_is_noop(self, db_session, queue):
        a = await _add_item(db_session, queue.id, position=0)
        b = await _add_item(db_session, queue.id, position=1)

        changed = await queue_ops.reorder_block(db_session, queue.id, [b.id], "down")

        assert changed == 0
        assert await _positions(db_session, queue.id) == [(a.id, 0), (b.id, 1)]

    async def test_batch_moves_as_unit_down_past_solo(self, db_session, queue):
        # Layout: [A_batch, B_batch, C_solo]  → down → [C_solo, A_batch, B_batch]
        batch = str(uuid.uuid4())
        a = await _add_item(db_session, queue.id, position=0, batch_id=batch)
        b = await _add_item(db_session, queue.id, position=1, batch_id=batch)
        c = await _add_item(db_session, queue.id, position=2)

        await queue_ops.reorder_block(db_session, queue.id, [a.id, b.id], "down")

        assert await _positions(db_session, queue.id) == [(c.id, 0), (a.id, 1), (b.id, 2)]

    async def test_batch_moves_as_unit_up_past_solo(self, db_session, queue):
        # Layout: [C_solo, A_batch, B_batch] → up → [A_batch, B_batch, C_solo]
        batch = str(uuid.uuid4())
        c = await _add_item(db_session, queue.id, position=0)
        a = await _add_item(db_session, queue.id, position=1, batch_id=batch)
        b = await _add_item(db_session, queue.id, position=2, batch_id=batch)

        await queue_ops.reorder_block(db_session, queue.id, [a.id, b.id], "up")

        assert await _positions(db_session, queue.id) == [(a.id, 0), (b.id, 1), (c.id, 2)]

    async def test_bad_direction_raises(self, db_session, queue):
        a = await _add_item(db_session, queue.id, position=0)

        with pytest.raises(ValueError):
            await queue_ops.reorder_block(db_session, queue.id, [a.id], "sideways")

    async def test_empty_block_ids_returns_zero(self, db_session, queue):
        await _add_item(db_session, queue.id, position=0)
        changed = await queue_ops.reorder_block(db_session, queue.id, [], "up")
        assert changed == 0

    async def test_only_non_pending_neighbours_is_noop(self, db_session, queue):
        """A pending solo item with no pending neighbours can't move."""
        a = await _add_item(db_session, queue.id, position=0)
        # A completed item exists at position 1 but queue_ops ignores non-pending.
        await _add_item(db_session, queue.id, position=1, status="completed")

        changed = await queue_ops.reorder_block(db_session, queue.id, [a.id], "down")

        assert changed == 0


# ── bump_block_to_top ─────────────────────────────────────────────────────────


class TestBumpBlockToTop:
    async def test_solo_bump_shifts_others_down(self, db_session, queue):
        a = await _add_item(db_session, queue.id, position=0)
        b = await _add_item(db_session, queue.id, position=1)
        c = await _add_item(db_session, queue.id, position=2)

        await queue_ops.bump_block_to_top(db_session, queue.id, [c.id])

        # From 1: position 0 is the printer's direct-print claim (42634ea27, N03).
        assert await _positions(db_session, queue.id) == [(c.id, 1), (a.id, 2), (b.id, 3)]

    async def test_batch_bump_preserves_intra_order(self, db_session, queue):
        batch = str(uuid.uuid4())
        solo = await _add_item(db_session, queue.id, position=0)
        a = await _add_item(db_session, queue.id, position=1, batch_id=batch)
        b = await _add_item(db_session, queue.id, position=2, batch_id=batch)

        await queue_ops.bump_block_to_top(db_session, queue.id, [a.id, b.id])

        assert await _positions(db_session, queue.id) == [(a.id, 1), (b.id, 2), (solo.id, 3)]

    @pytest.mark.parametrize("start", [0, 1])
    async def test_already_at_top_is_noop(self, db_session, queue, start):
        """Asked of the ORDER, as «already at the bottom» is: a block «Run next» placed first sits at 1,
        and a check for position 0 rewrote it and logged a bump that moved nothing."""
        a = await _add_item(db_session, queue.id, position=start)
        b = await _add_item(db_session, queue.id, position=start + 1)

        changed = await queue_ops.bump_block_to_top(db_session, queue.id, [a.id])

        assert changed == 0
        assert await _positions(db_session, queue.id) == [(a.id, start), (b.id, start + 1)]

    async def test_empty_block_ids_returns_zero(self, db_session, queue):
        await _add_item(db_session, queue.id, position=0)
        assert await queue_ops.bump_block_to_top(db_session, queue.id, []) == 0

    async def test_bump_does_not_touch_other_queue(self, db_session, queue, queue_b):
        # Two independent queues — bumping one must not reorder the other.
        a = await _add_item(db_session, queue.id, position=0)
        b = await _add_item(db_session, queue.id, position=1)
        x = await _add_item(db_session, queue_b.id, position=0)
        y = await _add_item(db_session, queue_b.id, position=1)

        await queue_ops.bump_block_to_top(db_session, queue.id, [b.id])

        assert await _positions(db_session, queue.id) == [(b.id, 1), (a.id, 2)]
        assert await _positions(db_session, queue_b.id) == [(x.id, 0), (y.id, 1)]


class TestBumpToBottom:
    async def test_a_block_just_above_the_last_row_moves_on_a_one_based_queue(self, db_session, queue):
        """Pending positions start at 1. The "already at the bottom" check assumed 0..n-1, so a block at
        2, 3 above a row at 4 counted as already there and the bump did nothing."""
        first = await _add_item(db_session, queue.id, position=1)
        a = await _add_item(db_session, queue.id, position=2)
        b = await _add_item(db_session, queue.id, position=3)
        last = await _add_item(db_session, queue.id, position=4)

        changed = await queue_ops.bump_block_to_bottom(db_session, queue.id, [a.id, b.id])

        assert changed == 4
        assert await _positions(db_session, queue.id) == [(first.id, 1), (last.id, 2), (a.id, 3), (b.id, 4)]

    @pytest.mark.parametrize("start", [0, 1])
    async def test_a_block_already_at_the_bottom_is_a_noop(self, db_session, queue, start):
        first = await _add_item(db_session, queue.id, position=start)
        a = await _add_item(db_session, queue.id, position=start + 1)
        b = await _add_item(db_session, queue.id, position=start + 2)

        assert await queue_ops.bump_block_to_bottom(db_session, queue.id, [a.id, b.id]) == 0
        assert await _positions(db_session, queue.id) == [(first.id, start), (a.id, start + 1), (b.id, start + 2)]


# ── clone_item / clone_batch ──────────────────────────────────────────────────


class TestCloneItem:
    async def test_clone_keep_batch_shares_batch_id(self, db_session, queue):
        batch = str(uuid.uuid4())
        src = await _add_item(db_session, queue.id, position=0, batch_id=batch, manual_start=True)

        clone = await queue_ops.clone_item(db_session, src.id, keep_batch=True)

        assert clone is not None
        assert clone.id != src.id
        assert clone.batch_id == batch
        assert clone.position == 1  # max+1
        assert clone.status == "pending"
        # Copied user-editable fields.
        assert clone.manual_start is True

    async def test_clone_drop_batch_creates_solo(self, db_session, queue):
        batch = str(uuid.uuid4())
        src = await _add_item(db_session, queue.id, position=0, batch_id=batch)

        clone = await queue_ops.clone_item(db_session, src.id, keep_batch=False)

        assert clone is not None
        assert clone.batch_id is None

    async def test_clone_missing_id_returns_none(self, db_session):
        result = await queue_ops.clone_item(db_session, 999_999)
        assert result is None

    async def test_clone_position_appended_past_all_pending(self, db_session, queue):
        a = await _add_item(db_session, queue.id, position=0)
        await _add_item(db_session, queue.id, position=5)  # non-contiguous positions
        await _add_item(db_session, queue.id, position=7)

        clone = await queue_ops.clone_item(db_session, a.id)

        assert clone is not None
        assert clone.position == 8  # max existing (7) + 1

    async def test_clone_ignores_non_pending_for_position(self, db_session, queue):
        # max()+1 query filters status='pending', so completed items don't
        # bump the target position.
        src = await _add_item(db_session, queue.id, position=0)
        await _add_item(db_session, queue.id, position=99, status="completed")

        clone = await queue_ops.clone_item(db_session, src.id)

        assert clone is not None
        assert clone.position == 1


class TestCloneBatch:
    async def test_clone_batch_assigns_fresh_uuid(self, db_session, queue):
        batch = str(uuid.uuid4())
        a = await _add_item(db_session, queue.id, position=0, batch_id=batch)
        b = await _add_item(db_session, queue.id, position=1, batch_id=batch)

        clones = await queue_ops.clone_batch(db_session, batch)

        assert len(clones) == 2
        new_batch_ids = {c.batch_id for c in clones}
        assert len(new_batch_ids) == 1  # all share one new batch_id
        new_batch = next(iter(new_batch_ids))
        assert new_batch != batch
        # Source rows untouched.
        await db_session.refresh(a)
        await db_session.refresh(b)
        assert a.batch_id == batch
        assert b.batch_id == batch

    async def test_clone_batch_preserves_intra_order(self, db_session, queue):
        batch = str(uuid.uuid4())
        a = await _add_item(db_session, queue.id, position=0, batch_id=batch, manual_start=True)
        b = await _add_item(db_session, queue.id, position=1, batch_id=batch, manual_start=False)

        clones = await queue_ops.clone_batch(db_session, batch)

        assert [c.position for c in clones] == [2, 3]  # appended, preserving order
        assert [c.manual_start for c in clones] == [a.manual_start, b.manual_start]

    async def test_clone_batch_empty_returns_empty(self, db_session, queue):
        # Unknown batch id = no siblings to clone.
        clones = await queue_ops.clone_batch(db_session, "no-such-batch-id")
        assert clones == []

    async def test_clone_batch_skips_non_pending_siblings(self, db_session, queue):
        batch = str(uuid.uuid4())
        await _add_item(db_session, queue.id, position=0, batch_id=batch, status="completed")
        await _add_item(db_session, queue.id, position=1, batch_id=batch, status="pending")
        await _add_item(db_session, queue.id, position=2, batch_id=batch, status="failed")

        clones = await queue_ops.clone_batch(db_session, batch)

        # Only the one pending sibling cloned.
        assert len(clones) == 1


# ── set_status / set_status_for_batch ─────────────────────────────────────────


class TestSetStatus:
    async def test_set_status_changes_and_returns_true(self, db_session, queue):
        item = await _add_item(db_session, queue.id, position=0)

        changed = await queue_ops.set_status(db_session, item.id, "skipped")

        assert changed is True
        await db_session.refresh(item)
        assert item.status == "skipped"

    async def test_set_status_same_returns_false(self, db_session, queue):
        item = await _add_item(db_session, queue.id, position=0, status="pending")
        assert await queue_ops.set_status(db_session, item.id, "pending") is False

    async def test_set_status_missing_id_returns_false(self, db_session):
        assert await queue_ops.set_status(db_session, 999_999, "skipped") is False


class TestSetStatusForBatch:
    async def test_applies_to_all_pending_siblings(self, db_session, queue):
        batch = str(uuid.uuid4())
        a = await _add_item(db_session, queue.id, position=0, batch_id=batch)
        b = await _add_item(db_session, queue.id, position=1, batch_id=batch)

        count = await queue_ops.set_status_for_batch(db_session, batch, "cancelled")

        assert count == 2
        await db_session.refresh(a)
        await db_session.refresh(b)
        assert a.status == "cancelled"
        assert b.status == "cancelled"

    async def test_ignores_non_pending_siblings(self, db_session, queue):
        batch = str(uuid.uuid4())
        done = await _add_item(db_session, queue.id, position=0, batch_id=batch, status="completed")
        pend = await _add_item(db_session, queue.id, position=1, batch_id=batch, status="pending")

        count = await queue_ops.set_status_for_batch(db_session, batch, "cancelled")

        assert count == 1
        await db_session.refresh(done)
        await db_session.refresh(pend)
        assert done.status == "completed"  # untouched
        assert pend.status == "cancelled"

    async def test_empty_batch_returns_zero(self, db_session):
        assert await queue_ops.set_status_for_batch(db_session, "no-such-batch", "cancelled") == 0


async def _claim_behind_the_sessions_back(db_session, item_id: int) -> None:
    """What the scheduler's claim does to a row the caller already read: ``printing``, with the session's
    loaded copy still saying ``pending``."""
    from sqlalchemy import update

    await db_session.execute(
        update(PrintQueueItem)
        .where(PrintQueueItem.id == item_id)
        .values(status="printing")
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()


class TestAStatusWriteIsACompareAndSet:
    """G03 E-2: skip / batch cancel / batch skip wrote the status from their own earlier read."""

    async def test_a_claimed_row_is_not_written_over(self, db_session, queue):
        item = await _add_item(db_session, queue.id, position=1)
        await _claim_behind_the_sessions_back(db_session, item.id)

        assert await queue_ops.set_status(db_session, item.id, "skipped") is False
        await db_session.refresh(item)
        assert item.status == "printing"

    async def test_a_claimed_batch_copy_is_not_written_over(self, db_session, queue):
        batch = str(uuid.uuid4())
        claimed = await _add_item(db_session, queue.id, position=1, batch_id=batch)
        other = await _add_item(db_session, queue.id, position=2, batch_id=batch)
        await _claim_behind_the_sessions_back(db_session, claimed.id)

        assert await queue_ops.set_status_for_batch(db_session, batch, "cancelled") == 1
        await db_session.refresh(claimed)
        await db_session.refresh(other)
        assert (claimed.status, other.status) == ("printing", "cancelled")

    async def test_the_end_is_stamped_when_asked(self, db_session, queue):
        from datetime import datetime, timezone

        batch = str(uuid.uuid4())
        item = await _add_item(db_session, queue.id, position=1, batch_id=batch)
        now = datetime.now(timezone.utc)

        assert await queue_ops.set_status_for_batch(db_session, batch, "cancelled", completed_at=now) == 1
        await db_session.refresh(item)
        assert item.completed_at is not None


class TestABatchBelongsToOneQueue:
    """G03 D1: AutoQueue promotion spreads one ``batch_id`` over the printers that took its copies."""

    async def test_a_spread_batch_names_no_queue_by_itself(self, db_session, queue, queue_b):
        batch = str(uuid.uuid4())
        await _add_item(db_session, queue.id, position=1, batch_id=batch)
        await _add_item(db_session, queue_b.id, position=1, batch_id=batch)

        with pytest.raises(queue_ops.BatchSpansQueues):
            await queue_ops.batch_queue_id(db_session, batch, None)
        assert await queue_ops.batch_queue_id(db_session, batch, queue_b.id) == queue_b.id

    async def test_finished_copies_do_not_spread_it(self, db_session, queue, queue_b):
        batch = str(uuid.uuid4())
        await _add_item(db_session, queue.id, position=1, batch_id=batch)
        await _add_item(db_session, queue_b.id, position=1, batch_id=batch, status="completed")

        assert await queue_ops.batch_queue_id(db_session, batch, None) == queue.id
        assert await queue_ops.batch_queue_id(db_session, "no-such-batch", None) is None

    async def test_a_status_write_reaches_one_queue(self, db_session, queue, queue_b):
        batch = str(uuid.uuid4())
        mine = await _add_item(db_session, queue.id, position=1, batch_id=batch)
        theirs = await _add_item(db_session, queue_b.id, position=1, batch_id=batch)

        with pytest.raises(queue_ops.BatchSpansQueues):
            await queue_ops.set_status_for_batch(db_session, batch, "skipped")
        assert await queue_ops.set_status_for_batch(db_session, batch, "skipped", queue_id=queue.id) == 1
        await db_session.refresh(mine)
        await db_session.refresh(theirs)
        assert (mine.status, theirs.status) == ("skipped", "pending")

    async def test_a_rows_block_is_its_own_queues_copies(self, db_session, queue, queue_b):
        batch = str(uuid.uuid4())
        a = await _add_item(db_session, queue.id, position=1, batch_id=batch)
        b = await _add_item(db_session, queue.id, position=2, batch_id=batch)
        await _add_item(db_session, queue_b.id, position=1, batch_id=batch)

        qid, ids = await queue_ops.resolve_block_ids(db_session, a.id)

        assert (qid, sorted(ids)) == (queue.id, sorted([a.id, b.id]))

    async def test_a_batch_clone_copies_one_queue_into_that_queue(self, db_session, queue, queue_b):
        batch = str(uuid.uuid4())
        await _add_item(db_session, queue.id, position=7, batch_id=batch)
        await _add_item(db_session, queue_b.id, position=1, batch_id=batch)
        await _add_item(db_session, queue_b.id, position=2, batch_id=batch)

        with pytest.raises(queue_ops.BatchSpansQueues):
            await queue_ops.clone_batch(db_session, batch)
        clones = await queue_ops.clone_batch(db_session, batch, queue_id=queue_b.id)

        assert [(c.queue_id, c.position) for c in clones] == [(queue_b.id, 3), (queue_b.id, 4)]


class TestACloneCarriesEveryPrintOption:
    """⚠️ A hand-written copy list rots, and this one had.

    ``_copy_item_fields`` listed 19 of the model's columns. Everything added
    since — the tri-state calibration modes (m106), the preheat overrides
    (m103), ``require_previous_success`` (m116), gcode injection, the H2C
    nozzle mapping, the selected macros — was silently dropped, so a cloned or
    retried job printed with different settings than the one it copied. Losing
    settings quietly is worse than refusing to clone.

    Behavioural on purpose: it sets every column, clones, and compares. A
    column added tomorrow and forgotten fails here rather than on somebody's
    printer.
    """

    # Set by the clone logic, or deliberately not carried over. Each entry is a
    # decision, which is the point of listing them by hand.
    NOT_COPIED = {
        "id",  # identity
        "recovery_identity",  # a clone is a new row; an old recovery preview must never name it
        "created_at",
        "queue_id",  # the clone's queue is chosen by the caller
        "position",  # appended to the end
        "batch_id",  # the caller decides sibling vs solo
        "status",  # a clone starts pending
        "started_at",  # lifecycle — this print has not run
        "completed_at",
        "error_message",
        "error_code",  # a failed row's class; a clone has not failed
        "waiting_reason",
        "gate_acknowledged",
        # The deferral proof of the SOURCE's own attempt (G03 B, A16): a clone starts with none.
        "deferred_claim_token",
        # ⚠️ The retry cap (m108) counts attempts at dispatching THIS row. A
        # fresh row starts at zero, or a clone would inherit a spent budget.
        "dispatch_attempts",
        # ⚠️ The auto-queue partner belongs to the row that was routed. Copying
        # it would give two queue items one router row, and deleting either
        # would take the other's link with it.
        "source_auto_item_id",
        # ⚠️ The lineage of the row that was promoted (m217). A clone is a new job, not a descendant of that
        # AutoQueue job: inheriting it would make an old connector receipt follow a print it never queued.
        "source_auto_identity",
        # The original AutoQueue promotion proved this placement. A clone has its own placement.
        "placement_basis",
        # The receipt of THIS row's return to AutoQueue (m226). A clone is a new job that was never
        # returned; carrying the mark would make it unrepeatable and count it as already returned.
        "returned_receipt_identity",
        # ⚠️ Deliberately dropped, both of them. A clone is a normal print, not
        # a second run of that calibration session: carrying the flag without
        # the session breaks the completion hook that looks for one, and
        # carrying both flips the same session twice.
        "is_calibration",
        "calibration_session_id",
        # A clone is a new incarnation of the work (swap-source-declaration spec r3 §3): a refresh listed against
        # the parent never reaches it.
        "swap_declaration_identity",
    }

    def _distinctive(self, column):
        """A value unlike any default, so a field that is not copied shows up."""
        import datetime as _dt

        from sqlalchemy import Boolean, DateTime, Integer

        if isinstance(column.type, Boolean):
            return not bool(column.default.arg) if column.default is not None else True
        if isinstance(column.type, DateTime):
            return _dt.datetime(2026, 8, 21, 12, 0)
        if isinstance(column.type, Integer):
            return 4242
        return "sentinel"

    def test_every_option_column_survives_a_clone(self):
        src = PrintQueueItem()
        columns = PrintQueueItem.__table__.columns
        for column in columns:
            setattr(src, column.key, self._distinctive(column))

        clone = queue_ops._copy_item_fields(src, "batch-1", 7)

        dropped = [
            column.key
            for column in columns
            if column.key not in self.NOT_COPIED and getattr(clone, column.key) != getattr(src, column.key)
        ]
        assert dropped == [], (
            "these columns are lost when a queue item is cloned or retried — "
            "add them to _copy_item_fields, or to NOT_COPIED with the reason"
        )

    def test_the_lifecycle_fields_are_not_carried(self):
        """The other half: a clone must not inherit the original's outcome."""
        src = PrintQueueItem()
        for column in PrintQueueItem.__table__.columns:
            setattr(src, column.key, self._distinctive(column))

        clone = queue_ops._copy_item_fields(src, None, 3)

        assert clone.status == "pending"
        assert clone.position == 3
        assert clone.batch_id is None
        assert clone.completed_at is None
        assert clone.error_message is None
        assert clone.dispatch_attempts in (0, None), "a clone must not inherit a spent retry budget"
        assert clone.source_auto_item_id is None
        assert clone.placement_basis is None
        assert clone.is_calibration in (False, None)

    def test_the_captured_bytes_travel_with_the_clone(self):
        """m173, named rather than left to the sweep above.

        The generic guard passes just as happily when a column is added to
        ``NOT_COPIED`` with a plausible sentence, and for these two that would be
        the bug: a clone that dropped ``queue_source_id`` would fall back to
        reading the original file — the one thing the queue spool exists to stop —
        and one that dropped ``source_snapshot`` would keep the right bytes under
        the wrong name, which ``source_display_filename`` then refuses to send to
        a printer. So the decision is pinned here in both directions.
        """
        src = PrintQueueItem(queue_id=1, queue_source_id=77, source_snapshot={"version": 1, "format": "3mf"})

        clone = queue_ops._copy_item_fields(src, None, 1)

        assert clone.queue_source_id == 77
        assert clone.source_snapshot == {"version": 1, "format": "3mf"}
        assert not {"queue_source_id", "source_snapshot"} & self.NOT_COPIED, (
            "a clone must print the same bytes under the same name — neither column may be dropped"
        )
