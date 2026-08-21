"""A queue row must not outlive the file it would print.

Ported from upstream `02616f0c` (#2819), where the same fault wore two faces:
rows left pointing at a file that no longer existed — failing at the printer
with "Library file not found" days later — or rows deleted outright, with no
error and no history.

Both faces were here too, split across our two branches: a **managed** file is
soft-deleted and its queue rows were left pointing at a trashed file, while an
**external** file is hard-deleted and its rows were removed silently.

The row is now cancelled with a reason, which is what we already do when an
archive is trashed (``archive_purge._cancel_pending_queue_items``). A job that
vanished without a word is indistinguishable from one that was never queued.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services.library_trash import library_trash_service


async def _file(db, *, external: bool = False) -> LibraryFile:
    row = LibraryFile(
        filename="part.gcode.3mf",
        file_path="files/part.gcode.3mf",
        file_type="3mf",
        file_size=10,
        is_external=external,
    )
    db.add(row)
    await db.flush()
    return row


async def _queued(db, file_id: int, printer_id: int, *, status: str = "pending") -> PrintQueueItem:
    queue = PrinterQueue(printer_id=printer_id, status="idle")
    db.add(queue)
    await db.flush()
    item = PrintQueueItem(queue_id=queue.id, library_file_id=file_id, position=1, status=status)
    db.add(item)
    await db.flush()
    return item


@pytest.mark.asyncio
@pytest.mark.integration
async def test_trashing_a_file_cancels_the_jobs_queued_against_it(db_session, printer_factory):
    printer = await printer_factory(name="P1")
    file = await _file(db_session)
    item = await _queued(db_session, file.id, printer.id)
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    await db_session.refresh(item)
    assert item.status == "cancelled"
    assert item.waiting_reason == "Source file deleted"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_row_survives_so_the_operator_can_see_what_happened(db_session, printer_factory):
    """Cancelled, not deleted. A job that disappeared without a word looks like
    one that was never queued."""
    printer = await printer_factory(name="P2")
    file = await _file(db_session)
    item = await _queued(db_session, file.id, printer.id)
    item_id = item.id
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    db_session.expire_all()
    still_there = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
    assert still_there.scalar_one_or_none() is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printing_row_is_left_alone(db_session, printer_factory):
    """Mid-print is the printer's race to lose, and its fail path catches it.
    Cancelling underneath a running job would be the worse answer."""
    printer = await printer_factory(name="P3")
    file = await _file(db_session)
    item = await _queued(db_session, file.id, printer.id, status="printing")
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    await db_session.refresh(item)
    assert item.status == "printing"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_history_is_not_rewritten(db_session, printer_factory):
    """A completed row records what actually happened and must not become
    'cancelled' because somebody later tidied the library."""
    printer = await printer_factory(name="P4")
    file = await _file(db_session)
    item = await _queued(db_session, file.id, printer.id, status="completed")
    await db_session.commit()

    await library_trash_service.trash_or_purge(db_session, file)
    await db_session.commit()

    await db_session.refresh(item)
    assert item.status == "completed"
    assert item.waiting_reason is None


class TestTheSweeperLeavesNoDanglingLink:
    """Trashing cancels the row; the sweeper, later, must unhook it.

    ⚠️ The row is meant to OUTLIVE the file — ``PrintQueueItem.library_file_id``
    is declared ``ON DELETE SET NULL`` precisely so the operator keeps the
    history of what was queued. But SQLite runs with ``foreign_keys=OFF``, so
    that clause never fires and the nulling has to be done by hand. The sweeper
    already does exactly this for ``PrintArchive.library_file_id``, with a
    comment explaining why — and did not do it for the queue, so a cancelled row
    ended up pointing at a library file that no longer exists.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_purged_file_leaves_its_queue_row_unhooked(self, db_session, printer_factory, monkeypatch):
        from datetime import datetime, timedelta, timezone

        printer = await printer_factory(name="S1")
        file = await _file(db_session)
        item = await _queued(db_session, file.id, printer.id, status="cancelled")
        item_id = item.id
        file.deleted_at = datetime.now(timezone.utc) - timedelta(days=99)
        await db_session.commit()

        await library_trash_service._sweep(db_session)

        row = (
            await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == item_id))
        ).scalar_one_or_none()
        assert row is not None, "the row must survive its file — that is what SET NULL means"
        assert row.library_file_id is None, "it still points at a library file that is gone"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rows_for_files_that_stay_are_untouched(self, db_session, printer_factory):
        """⚠️ The unhook is scoped to the ids actually purged. Nulling by any
        wider rule would quietly disconnect live jobs from their files."""
        from datetime import datetime, timedelta, timezone

        printer = await printer_factory(name="S2")
        doomed = await _file(db_session)
        keeper = await _file(db_session)
        keeper.file_path = "files/keeper.gcode.3mf"
        doomed_item = await _queued(db_session, doomed.id, printer.id, status="cancelled")
        # Same queue: PrinterQueue is one row per printer.
        keeper_item = PrintQueueItem(
            queue_id=doomed_item.queue_id, library_file_id=keeper.id, position=2, status="pending"
        )
        db_session.add(keeper_item)
        await db_session.flush()
        keeper_id, keeper_file_id = keeper_item.id, keeper.id
        doomed.deleted_at = datetime.now(timezone.utc) - timedelta(days=99)
        await db_session.commit()

        await library_trash_service._sweep(db_session)

        await db_session.refresh(doomed_item)
        kept = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == keeper_id))).scalar_one()
        assert doomed_item.library_file_id is None
        assert kept.library_file_id == keeper_file_id


class TestDeletingAUserUnhooksOtherPeoplesRows:
    """⚠️ The same gap from the other side.

    Deleting a user "with their content" removes the queue rows THEY created and
    the library files they own — but a colleague can have queued one of those
    files. That row is not theirs to delete, and ``ON DELETE SET NULL`` never
    fires on SQLite, so it was left pointing at a file that no longer exists.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_colleagues_row_survives_unhooked(self, db_session, printer_factory):
        from backend.app.api.routes.users import delete_user
        from backend.app.models.user import User

        owner = User(username="leaver")
        colleague = User(username="stays")
        db_session.add_all([owner, colleague])
        await db_session.flush()

        printer = await printer_factory(name="U1")
        file = await _file(db_session)
        file.created_by_id = owner.id
        theirs = await _queued(db_session, file.id, printer.id)
        theirs.created_by_id = colleague.id
        theirs_id = theirs.id
        await db_session.commit()

        admin = User(username="admin", role="admin")
        db_session.add(admin)
        await db_session.flush()
        await delete_user(user_id=owner.id, delete_items=True, current_user=admin, db=db_session)

        row = (
            await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == theirs_id))
        ).scalar_one_or_none()
        assert row is not None, "another user's queue row is not ours to delete"
        assert row.library_file_id is None, "it still points at a library file that is gone"
        assert (
            await db_session.execute(select(LibraryFile).where(LibraryFile.id == file.id))
        ).scalar_one_or_none() is None
