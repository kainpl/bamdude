"""Tests for phantom print investigation hardening (#374).

Tests the tightened archive matching (no ilike) and the
multiple-printing-items warning logic.

These are pure unit tests that test the changed logic directly,
NOT by calling the full on_print_start/on_print_complete callbacks
(which spawn background tasks and require heavy mocking).
"""

import logging

import pytest
from sqlalchemy import or_, select
from sqlalchemy.sql import ClauseElement

from backend.app.models.archive import PrintArchive


class TestArchiveMatchQueryShape:
    """Tests that the archive duplicate lookup query uses exact match, not ilike (#374).

    The old query used `ilike('%{name}%')` which caused "Clip" to match
    "Cable Clip", "Clip Stand", etc. The new query uses exact print_name
    match OR exact filename variants (.3mf, .gcode.3mf).
    """

    def _build_archive_query(self, check_name: str, printer_id: int = 1) -> ClauseElement:
        """Build the exact query used in on_print_start for archive dedup."""
        return (
            select(PrintArchive)
            .where(PrintArchive.printer_id == printer_id)
            .where(PrintArchive.status == "printing")
            .where(
                or_(
                    PrintArchive.print_name == check_name,
                    PrintArchive.filename.in_(
                        [
                            f"{check_name}.3mf",
                            f"{check_name}.gcode.3mf",
                        ]
                    ),
                )
            )
            .order_by(PrintArchive.created_at.desc())
            .limit(1)
        )

    def test_query_does_not_contain_ilike(self):
        """Verify the compiled query does NOT use LIKE/ILIKE."""
        query = self._build_archive_query("Clip")
        query_str = str(query.compile(compile_kwargs={"literal_binds": True}))

        assert "LIKE" not in query_str.upper(), f"Query should not use LIKE: {query_str}"

    def test_query_uses_exact_equality(self):
        """Verify the query uses = for print_name comparison."""
        query = self._build_archive_query("Benchy")
        query_str = str(query.compile(compile_kwargs={"literal_binds": True}))

        assert "print_name = " in query_str or "print_name ='" in query_str or "print_name =" in query_str

    def test_query_uses_in_for_filename_variants(self):
        """Verify the query uses IN for filename matching with .3mf variants."""
        query = self._build_archive_query("MyPrint")
        query_str = str(query.compile(compile_kwargs={"literal_binds": True}))

        assert "IN" in query_str.upper()
        assert "MyPrint.3mf" in query_str
        assert "MyPrint.gcode.3mf" in query_str

    def test_partial_name_not_in_query(self):
        """Verify 'Clip' does not produce a wildcard pattern."""
        query = self._build_archive_query("Clip")
        query_str = str(query.compile(compile_kwargs={"literal_binds": True}))

        # Should NOT contain %Clip% wildcard
        assert "%Clip%" not in query_str

    def test_check_name_derivation_from_subtask(self):
        """Verify check_name is derived correctly from subtask_name."""
        # Simulates: check_name = subtask_name or filename.split("/")[-1].replace(...)
        subtask_name = "Cable Clip"
        filename = "/sdcard/Cable Clip.gcode"
        check_name = subtask_name or filename.split("/")[-1].replace(".gcode", "").replace(".3mf", "")
        assert check_name == "Cable Clip"

        query = self._build_archive_query(check_name)
        query_str = str(query.compile(compile_kwargs={"literal_binds": True}))

        # Exact match should contain the full name, not a partial
        assert "Cable Clip" in query_str
        assert "%Cable Clip%" not in query_str

    def test_check_name_derivation_from_filename(self):
        """Verify check_name strips extensions correctly from filename."""
        subtask_name = None
        filename = "/sdcard/MyPrint.gcode"
        check_name = subtask_name or filename.split("/")[-1].replace(".gcode", "").replace(".3mf", "")
        assert check_name == "MyPrint"


class TestMultiplePrintingQueueItemsWarning:
    """Tests for the multiple-printing-items warning logic (#374).

    The code in on_print_complete now detects when multiple queue items
    are in 'printing' status for the same printer, which signals a bug.
    """

    def test_single_item_returns_item_no_warning(self, caplog):
        """Verify single item is returned without warning."""
        from unittest.mock import MagicMock

        items = [MagicMock(id=1, archive_id=10, library_file_id=None)]

        # Simulate the exact code from on_print_complete
        with caplog.at_level(logging.WARNING, logger="backend.app.main"):
            logger = logging.getLogger("backend.app.main")
            printer_id = 1
            printing_items = list(items)

            if len(printing_items) > 1:
                logger.warning(
                    "BUG: Multiple queue items in 'printing' status for printer %s: %s",
                    printer_id,
                    [(i.id, i.archive_id, i.library_file_id) for i in printing_items],
                )
            queue_item = printing_items[0] if printing_items else None

        assert queue_item is not None
        assert queue_item.id == 1
        bug_warnings = [r for r in caplog.records if "BUG: Multiple queue items" in r.message]
        assert len(bug_warnings) == 0

    def test_multiple_items_warns_and_returns_first(self, caplog):
        """Verify warning is logged and first item is returned when multiple exist."""
        from unittest.mock import MagicMock

        items = [
            MagicMock(id=1, archive_id=10, library_file_id=None),
            MagicMock(id=2, archive_id=20, library_file_id=None),
        ]

        with caplog.at_level(logging.WARNING, logger="backend.app.main"):
            logger = logging.getLogger("backend.app.main")
            printer_id = 1
            printing_items = list(items)

            if len(printing_items) > 1:
                logger.warning(
                    "BUG: Multiple queue items in 'printing' status for printer %s: %s",
                    printer_id,
                    [(i.id, i.archive_id, i.library_file_id) for i in printing_items],
                )
            queue_item = printing_items[0] if printing_items else None

        assert queue_item is not None
        assert queue_item.id == 1  # First item is used
        bug_warnings = [r for r in caplog.records if "BUG: Multiple queue items" in r.message]
        assert len(bug_warnings) == 1
        assert "printer 1" in bug_warnings[0].message
        # Warning should include item details
        assert "10" in bug_warnings[0].message  # archive_id of item 1
        assert "20" in bug_warnings[0].message  # archive_id of item 2

    def test_empty_list_returns_none_no_warning(self, caplog):
        """Verify None is returned and no warning when no items exist."""
        with caplog.at_level(logging.WARNING, logger="backend.app.main"):
            logger = logging.getLogger("backend.app.main")
            printer_id = 1
            printing_items = []

            if len(printing_items) > 1:
                logger.warning(
                    "BUG: Multiple queue items in 'printing' status for printer %s: %s",
                    printer_id,
                    [(i.id, i.archive_id, i.library_file_id) for i in printing_items],
                )
            queue_item = printing_items[0] if printing_items else None

        assert queue_item is None
        bug_warnings = [r for r in caplog.records if "BUG: Multiple queue items" in r.message]
        assert len(bug_warnings) == 0

    def test_three_items_warns_with_all_details(self, caplog):
        """Verify warning includes all item details when three items found."""
        from unittest.mock import MagicMock

        items = [
            MagicMock(id=1, archive_id=10, library_file_id=None),
            MagicMock(id=2, archive_id=None, library_file_id=5),
            MagicMock(id=3, archive_id=30, library_file_id=None),
        ]

        with caplog.at_level(logging.WARNING, logger="backend.app.main"):
            logger = logging.getLogger("backend.app.main")
            printer_id = 7
            printing_items = list(items)

            if len(printing_items) > 1:
                logger.warning(
                    "BUG: Multiple queue items in 'printing' status for printer %s: %s",
                    printer_id,
                    [(i.id, i.archive_id, i.library_file_id) for i in printing_items],
                )
            queue_item = printing_items[0] if printing_items else None

        assert queue_item.id == 1
        bug_warnings = [r for r in caplog.records if "BUG: Multiple queue items" in r.message]
        assert len(bug_warnings) == 1
        assert "printer 7" in bug_warnings[0].message


class TestBusyPrinterSeedingFromPrintingItems:
    """Regression coverage for upstream #950286ad / v0.2.3.2 re-release.

    ``check_queue`` must seed its ``busy_printers`` set from every queue row
    already in ``status='printing'`` before iterating pending items. Without
    this guard, the H2D / P1 state-transition lag (IDLE → RUNNING can take
    several seconds after the printer accepts the project_file) made the
    next scheduler tick see IDLE via MQTT and double-dispatch onto a printer
    that was already running a batch sibling.
    """

    def _build_busy_seed_query(self) -> ClauseElement:
        """Build the seed query check_queue uses before iterating pending items.

        Reads ``PrinterQueue.status='printing'`` directly — ``set_queue_busy``
        flips that atomically at dispatch time and it's the single marker that
        also covers external / direct prints without a corresponding
        ``PrintQueueItem`` row. Queue-per-printer, so ``printer_id`` uniquely
        identifies a printer's busy state.
        """
        from backend.app.models.print_queue import PrinterQueue

        return (
            select(PrinterQueue.printer_id)
            .where(PrinterQueue.status == "printing")
            .where(PrinterQueue.printer_id.is_not(None))
        )

    def test_seed_query_filters_to_printing_status(self):
        """The seed query must restrict to status='printing' — 'idle', 'paused',
        or 'error' queues don't hold the printer.
        """
        compiled = str(self._build_busy_seed_query().compile(compile_kwargs={"literal_binds": True}))
        assert "status = 'printing'" in compiled.lower() or "status='printing'" in compiled.lower()

    def test_seed_query_excludes_null_printer_id(self):
        """Rows with printer_id=NULL on the queue (unassigned / global) must
        never be seeded into the busy set — would skip every printer on the
        next tick.
        """
        compiled = str(self._build_busy_seed_query().compile(compile_kwargs={"literal_binds": True}))
        assert "is not null" in compiled.lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_seed_returns_only_printers_with_printing_queue(self, db_session, printer_factory):
        """End-to-end: only queues with status='printing' contribute to the
        busy set. Covers the batch-dispatch race and external-print cases
        (external prints also flip queue.status='printing' but without an
        item row, so an item-join query would miss them).
        """
        from backend.app.models.print_queue import PrinterQueue, PrintQueueItem

        printer1 = await printer_factory()
        printer2 = await printer_factory()
        printer3 = await printer_factory()

        # printer1: queue printing + pending sibling (batch quantity>1 case)
        # printer2: queue idle + pending item
        # printer3: queue printing with NO item (external / direct-print)
        queue1 = PrinterQueue(printer_id=printer1.id, status="printing")
        queue2 = PrinterQueue(printer_id=printer2.id, status="idle")
        queue3 = PrinterQueue(printer_id=printer3.id, status="printing")
        db_session.add_all([queue1, queue2, queue3])
        await db_session.commit()
        for q in (queue1, queue2, queue3):
            await db_session.refresh(q)

        db_session.add_all(
            [
                PrintQueueItem(queue_id=queue1.id, status="printing", position=1),
                PrintQueueItem(queue_id=queue1.id, status="pending", position=2),
                PrintQueueItem(queue_id=queue2.id, status="pending", position=1),
                # intentionally no item for queue3 — external/direct print case
            ]
        )
        await db_session.commit()

        result = await db_session.execute(self._build_busy_seed_query())
        busy = {pid for (pid,) in result.all() if pid is not None}

        assert busy == {printer1.id, printer3.id}, (
            f"Expected printer1 (batch) + printer3 (external, no item); got {busy}. Printer2 is idle — must not appear."
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_seed_empty_when_no_printing_queues(self, db_session, printer_factory):
        """Sanity: idle scheduler tick — seed query returns empty."""
        from backend.app.models.print_queue import PrinterQueue, PrintQueueItem

        printer = await printer_factory()
        queue = PrinterQueue(printer_id=printer.id, status="idle")
        db_session.add(queue)
        await db_session.commit()
        await db_session.refresh(queue)

        db_session.add(PrintQueueItem(queue_id=queue.id, status="pending", position=1))
        await db_session.commit()

        result = await db_session.execute(self._build_busy_seed_query())
        busy = {pid for (pid,) in result.all() if pid is not None}

        assert busy == set()
