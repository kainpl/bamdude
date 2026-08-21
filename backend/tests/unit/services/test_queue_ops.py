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

        assert await _positions(db_session, queue.id) == [(c.id, 0), (a.id, 1), (b.id, 2)]

    async def test_batch_bump_preserves_intra_order(self, db_session, queue):
        batch = str(uuid.uuid4())
        solo = await _add_item(db_session, queue.id, position=0)
        a = await _add_item(db_session, queue.id, position=1, batch_id=batch)
        b = await _add_item(db_session, queue.id, position=2, batch_id=batch)

        await queue_ops.bump_block_to_top(db_session, queue.id, [a.id, b.id])

        assert await _positions(db_session, queue.id) == [(a.id, 0), (b.id, 1), (solo.id, 2)]

    async def test_already_at_top_is_noop(self, db_session, queue):
        a = await _add_item(db_session, queue.id, position=0)
        b = await _add_item(db_session, queue.id, position=1)

        changed = await queue_ops.bump_block_to_top(db_session, queue.id, [a.id])

        assert changed == 0
        assert await _positions(db_session, queue.id) == [(a.id, 0), (b.id, 1)]

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

        assert await _positions(db_session, queue.id) == [(b.id, 0), (a.id, 1)]
        assert await _positions(db_session, queue_b.id) == [(x.id, 0), (y.id, 1)]


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
        "created_at",
        "queue_id",  # the clone's queue is chosen by the caller
        "position",  # appended to the end
        "batch_id",  # the caller decides sibling vs solo
        "status",  # a clone starts pending
        "started_at",  # lifecycle — this print has not run
        "completed_at",
        "error_message",
        "waiting_reason",
        "gate_acknowledged",
        # ⚠️ The retry cap (m108) counts attempts at dispatching THIS row. A
        # fresh row starts at zero, or a clone would inherit a spent budget.
        "dispatch_attempts",
        # ⚠️ The auto-queue partner belongs to the row that was routed. Copying
        # it would give two queue items one router row, and deleting either
        # would take the other's link with it.
        "source_auto_item_id",
        # ⚠️ Deliberately dropped, both of them. A clone is a normal print, not
        # a second run of that calibration session: carrying the flag without
        # the session breaks the completion hook that looks for one, and
        # carrying both flips the same session twice.
        "is_calibration",
        "calibration_session_id",
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
        assert clone.is_calibration in (False, None)
