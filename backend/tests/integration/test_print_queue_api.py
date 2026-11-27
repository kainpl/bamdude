"""Integration tests for Print Queue API endpoints."""

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select

from backend.app.models.print_queue import PrintQueueItem
from backend.tests.fixtures.filament_routing_cases import write_routing_3mf


async def _create_printer_with_queue(db_session, **printer_kwargs):
    """Helper: create a Printer + its PrinterQueue, return (printer, queue)."""
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue

    printer = Printer(**printer_kwargs)
    db_session.add(printer)
    await db_session.commit()
    await db_session.refresh(printer)

    queue = PrinterQueue(printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    await db_session.refresh(queue)
    return printer, queue


def test_reorder_schema_rejects_duplicate_positions():
    """PrintQueueReorder rejects duplicate positions in the payload at the schema
    boundary (upstream #1625-followup) — a buggy drag-drop client that renumbers
    two items to the same position would leave the queue's ORDER BY ambiguous."""
    from pydantic import ValidationError

    from backend.app.schemas.print_queue import PrintQueueReorder

    # Unique positions → valid.
    PrintQueueReorder(items=[{"id": 1, "position": 1}, {"id": 2, "position": 2}])

    # Duplicate positions → rejected (surfaces as HTTP 422 on the route).
    with pytest.raises(ValidationError) as exc:
        PrintQueueReorder(items=[{"id": 1, "position": 1}, {"id": 2, "position": 1}])
    assert "Duplicate positions" in str(exc.value)


class TestPrintQueueAPI:
    """Integration tests for /api/v1/queue endpoints."""

    @pytest.fixture
    async def printer_factory(self, db_session):
        """Factory to create test printers with queues."""
        _counter = [0]

        async def _create_printer(**kwargs):
            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "name": f"Test Printer {counter}",
                "ip_address": f"192.168.1.{100 + counter}",
                "serial_number": f"TESTSERIAL{counter:04d}",
                "access_code": "12345678",
                "model": "X1C",
            }
            defaults.update(kwargs)
            printer, queue = await _create_printer_with_queue(db_session, **defaults)
            return printer, queue

        return _create_printer

    @pytest.fixture
    async def archive_factory(self, db_session, tmp_path):
        """Factory to create test archives."""
        _counter = [0]

        async def _create_archive(**kwargs):
            from backend.app.models.archive import PrintArchive

            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "filename": f"test_print_{counter}.3mf",
                "print_name": f"Test Print {counter}",
                "file_path": str(
                    write_routing_3mf(
                        tmp_path / f"archive-{counter}.3mf",
                        {i: [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": "1"}] for i in (1, 2, 3, 5)},
                        model="X1C",
                    )
                ),
                "plate_index": 1,
                "file_size": 1024,
                "content_hash": f"testhash{counter:08d}",
                "status": "completed",
            }
            defaults.update(kwargs)

            archive = PrintArchive(**defaults)
            db_session.add(archive)
            await db_session.commit()
            await db_session.refresh(archive)
            return archive

        return _create_archive

    @pytest.fixture
    async def queue_item_factory(self, db_session, printer_factory, archive_factory):
        """Factory to create test queue items."""
        _counter = [0]

        async def _create_queue_item(**kwargs):
            from backend.app.models.print_queue import PrintQueueItem

            _counter[0] += 1
            counter = _counter[0]

            # Create printer+queue if not provided
            if "queue_id" not in kwargs:
                _printer, queue = await printer_factory()
                kwargs["queue_id"] = queue.id

            if "archive_id" not in kwargs:
                archive = await archive_factory()
                kwargs["archive_id"] = archive.id

            defaults = {
                "status": "pending",
                "position": counter,
            }
            defaults.update(kwargs)

            item = PrintQueueItem(**defaults)
            db_session.add(item)
            await db_session.commit()
            await db_session.refresh(item)
            return item

        return _create_queue_item

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_queue_empty(self, async_client: AsyncClient):
        """Verify empty list when no queue items exist."""
        response = await async_client.get("/api/v1/queue/")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_summary_empty_is_a_successful_zero(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/queue/summary")
        assert response.status_code == 200, response.text
        assert response.json() == {"pending_count": 0, "groups": []}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_summary_counts_queue_rows_not_archive_history(
        self, async_client: AsyncClient, queue_item_factory, archive_factory
    ):
        pending = await queue_item_factory(archive_id=None)
        await queue_item_factory(queue_id=pending.queue_id, archive_id=None, status="failed")
        await queue_item_factory(queue_id=pending.queue_id, archive_id=None, status="skipped")
        await queue_item_factory(queue_id=pending.queue_id, archive_id=None, status="cancelled")
        await archive_factory(status="failed")  # Not a queue issue.

        response = await async_client.get("/api/v1/queue/summary")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "pending_count": 1,
            "groups": [
                {
                    "queue_id": pending.queue_id,
                    "printer_id": pending.queue_id,
                    "pending_count": 1,
                    "failed_count": 1,
                    "skipped_count": 1,
                    "cancelled_count": 1,
                }
            ],
        }

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_summary_is_one_grouped_select_for_many_rows(self, db_session, printer_factory):
        from backend.app.api.routes.print_queue import queue_summary

        _printer, queue = await printer_factory()
        db_session.add_all(PrintQueueItem(queue_id=queue.id, position=index, status="pending") for index in range(100))
        await db_session.commit()
        statements = []

        def count_select(_conn, _cursor, statement, _params, _context, _many):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(db_session.bind.sync_engine, "before_cursor_execute", count_select)
        try:
            result = await queue_summary(db_session, (None, True))
        finally:
            event.remove(db_session.bind.sync_engine, "before_cursor_execute", count_select)
        assert result.pending_count == 100
        assert len(statements) == 1, statements

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_issues_are_bounded_and_stably_paginated(self, async_client: AsyncClient, queue_item_factory):
        first = await queue_item_factory(archive_id=None, status="failed")
        second = await queue_item_factory(queue_id=first.queue_id, archive_id=None, status="skipped")
        third = await queue_item_factory(queue_id=first.queue_id, archive_id=None, status="cancelled")
        await queue_item_factory(queue_id=first.queue_id, archive_id=None, status="pending")

        page1 = await async_client.get(f"/api/v1/queue/issues?queue_id={first.queue_id}&limit=2")
        assert page1.status_code == 200, page1.text
        assert [item["id"] for item in page1.json()["items"]] == [first.id, second.id]
        assert page1.json()["next_cursor"] == second.id

        page2 = await async_client.get(f"/api/v1/queue/issues?queue_id={first.queue_id}&limit=2&cursor={second.id}")
        assert page2.status_code == 200, page2.text
        assert [item["id"] for item in page2.json()["items"]] == [third.id]
        assert page2.json()["next_cursor"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_group_and_ungroup_batch(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Group 2 pending items in one queue into a batch, then ungroup (upstream #1743)."""
        item1 = await queue_item_factory(status="pending")
        item2 = await queue_item_factory(queue_id=item1.queue_id, status="pending")

        # Group
        resp = await async_client.post("/api/v1/queue/batch", json={"item_ids": [item1.id, item2.id]})
        assert resp.status_code == 200
        batch_id = resp.json()["batch_id"]
        assert resp.json()["count"] == 2
        await db_session.refresh(item1)
        await db_session.refresh(item2)
        assert item1.batch_id == batch_id
        assert item2.batch_id == batch_id

        # Ungroup
        resp2 = await async_client.post(f"/api/v1/queue/batch/{batch_id}/ungroup")
        assert resp2.status_code == 200
        assert resp2.json()["ungrouped"] == 2
        await db_session.refresh(item1)
        await db_session.refresh(item2)
        assert item1.batch_id is None
        assert item2.batch_id is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_group_batch_rejects_cross_queue_and_too_few(self, async_client: AsyncClient, queue_item_factory):
        """A batch must be 2+ items in the same printer queue (upstream #1743)."""
        item1 = await queue_item_factory(status="pending")
        item_other_queue = await queue_item_factory(status="pending")  # its own new queue

        # Fewer than 2 → 400
        resp_few = await async_client.post("/api/v1/queue/batch", json={"item_ids": [item1.id]})
        assert resp_few.status_code == 400

        # Items in different queues → 400
        resp_cross = await async_client.post("/api/v1/queue/batch", json={"item_ids": [item1.id, item_other_queue.id]})
        assert resp_cross.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue(self, async_client: AsyncClient, printer_factory, archive_factory, db_session):
        """Verify item can be added to queue."""
        printer, queue = await printer_factory()
        archive = await archive_factory()

        data = {
            "queue_id": queue.id,
            "archive_id": archive.id,
        }
        response = await async_client.post("/api/v1/queue/", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["queue_id"] == queue.id
        assert result["printer_id"] == printer.id
        assert result["archive_id"] == archive.id
        assert result["status"] == "pending"
        assert result["manual_start"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_add_response_names_every_row_it_created(
        self, async_client: AsyncClient, printer_factory, archive_factory
    ):
        """⚠️ It used to name only the first.

        A quantity becomes ROWS at insert time — there is no ``quantity`` column
        on ``print_queue`` — but the handler serialised ``items[0]`` alone, so a
        caller that asked for three copies could not find the other two. Copying
        a queue and re-forming its batches on the target needs every id.
        """
        _printer, queue = await printer_factory()
        archive = await archive_factory()

        response = await async_client.post(
            "/api/v1/queue/", json={"queue_id": queue.id, "archive_id": archive.id, "quantity": 3}
        )

        assert response.status_code == 200, response.text
        result = response.json()
        created = result["created_item_ids"]
        assert len(created) == 3, "one id per row the call made"
        assert len(set(created)) == 3, "distinct rows"
        assert created[0] == result["id"], "the response's own item is the first of them"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_next_inserts_before_existing_pending_rows(
        self, async_client: AsyncClient, printer_factory, archive_factory, queue_item_factory, db_session
    ):
        """Urgent work shifts pending rows but never touches a current print."""
        _printer, queue = await printer_factory()
        existing_archive = await archive_factory()
        old_a = await queue_item_factory(queue_id=queue.id, archive_id=existing_archive.id, position=4)
        old_b = await queue_item_factory(queue_id=queue.id, archive_id=existing_archive.id, position=9)
        urgent = await archive_factory()

        response = await async_client.post(
            "/api/v1/queue/",
            json={"queue_id": queue.id, "archive_id": urgent.id, "enqueue_position": "next"},
        )

        assert response.status_code == 200, response.text
        urgent_id = response.json()["id"]
        ordered = list(
            (
                await db_session.execute(
                    select(PrintQueueItem.id)
                    .where(PrintQueueItem.queue_id == queue.id)
                    .where(PrintQueueItem.status == "pending")
                    .order_by(PrintQueueItem.position, PrintQueueItem.id)
                )
            ).scalars()
        )
        assert ordered == [urgent_id, old_a.id, old_b.id]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_next_batch_keeps_plates_contiguous(
        self, async_client: AsyncClient, printer_factory, archive_factory, queue_item_factory, db_session
    ):
        """A multi-plate urgent submit is one transaction, not two inserts."""
        _printer, queue = await printer_factory()
        existing_archive = await archive_factory()
        old = await queue_item_factory(queue_id=queue.id, archive_id=existing_archive.id, position=0)
        urgent = await archive_factory()
        payload = {
            "items": [
                {"queue_id": queue.id, "archive_id": urgent.id, "plate_id": 1, "enqueue_position": "next"},
                {"queue_id": queue.id, "archive_id": urgent.id, "plate_id": 2, "enqueue_position": "next"},
            ]
        }

        response = await async_client.post("/api/v1/queue/next-block", json=payload)

        assert response.status_code == 200, response.text
        created = response.json()
        assert [item["plate_id"] for item in created] == [1, 2]
        ordered = list(
            (
                await db_session.execute(
                    select(PrintQueueItem.id)
                    .where(PrintQueueItem.queue_id == queue.id)
                    .where(PrintQueueItem.status == "pending")
                    .order_by(PrintQueueItem.position, PrintQueueItem.id)
                )
            ).scalars()
        )
        assert ordered == [created[0]["id"], created[1]["id"], old.id]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_next_after_an_earlier_block_of_the_run(
        self, async_client: AsyncClient, printer_factory, archive_factory, queue_item_factory, db_session
    ):
        """N03 (upstream #3471): two files of one «run next» selection keep the order they were chosen in."""
        _printer, queue = await printer_factory()
        existing_archive = await archive_factory()
        old = await queue_item_factory(queue_id=queue.id, archive_id=existing_archive.id, position=1)
        first_file = await archive_factory()
        second_file = await archive_factory()

        first = await async_client.post(
            "/api/v1/queue/next-block",
            json={"items": [{"queue_id": queue.id, "archive_id": first_file.id, "enqueue_position": "next"}]},
        )
        assert first.status_code == 200, first.text
        first_ids = [item["id"] for item in first.json()]
        second = await async_client.post(
            "/api/v1/queue/next-block",
            json={
                "items": [{"queue_id": queue.id, "archive_id": second_file.id, "enqueue_position": "next"}],
                "after_item_ids": first_ids,
            },
        )

        assert second.status_code == 200, second.text
        ordered = list(
            (
                await db_session.execute(
                    select(PrintQueueItem.id)
                    .where(PrintQueueItem.queue_id == queue.id)
                    .where(PrintQueueItem.status == "pending")
                    .order_by(PrintQueueItem.position, PrintQueueItem.id)
                )
            ).scalars()
        )
        assert ordered == [*first_ids, second.json()[0]["id"], old.id]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_next_captures_auto_eject_like_the_queue_add(
        self, async_client: AsyncClient, printer_factory, archive_factory, db_session
    ):
        """G03 D7.1: ``POST /queue`` asks ``order_auto_eject.capture``; "Run next" never did, so its rows landed
        without the auto-eject the request, the order or the archive asked for."""
        _printer, queue = await printer_factory()
        urgent = await archive_factory()
        payload = {
            "items": [
                {
                    "queue_id": queue.id,
                    "archive_id": urgent.id,
                    "plate_id": plate,
                    "enqueue_position": "next",
                    "auto_eject_enabled": True,
                }
                for plate in (1, 2)
            ]
        }

        response = await async_client.post("/api/v1/queue/next-block", json=payload)

        assert response.status_code == 200, response.text
        assert [item["auto_eject"] for item in response.json()] == [True, True]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_batch_move_is_checked_like_a_single_move(
        self, async_client: AsyncClient, printer_factory, archive_factory, db_session
    ):
        """G03 follow-up: ``PATCH /queue/batch/{id}`` with a body ``queue_id`` moved every copy without the
        model check the single PATCH asks -- an X1C job landed on an A1 mini -- and counted nobody."""
        from backend.app.models.printer_queue import PrinterQueue

        _printer, home = await printer_factory()
        _a1, a1 = await printer_factory(model="A1MINI")
        _twin, twin = await printer_factory()
        archive = await archive_factory()
        created = await async_client.post(
            "/api/v1/queue/", json={"queue_id": home.id, "archive_id": archive.id, "quantity": 2}
        )
        assert created.status_code == 200, created.text
        batch, ids = created.json()["batch_id"], created.json()["created_item_ids"]

        refused = await async_client.patch(f"/api/v1/queue/batch/{batch}?queue_id={home.id}", json={"queue_id": a1.id})
        missing = await async_client.patch(f"/api/v1/queue/batch/{batch}?queue_id={home.id}", json={"queue_id": 9999})

        assert refused.status_code == 400, refused.text
        assert "cannot be dispatched to a A1MINI printer" in refused.json()["detail"]
        assert missing.status_code == 400, missing.text
        rows = (
            await db_session.execute(
                select(PrintQueueItem).where(PrintQueueItem.id.in_(ids)).execution_options(populate_existing=True)
            )
        ).scalars()
        assert {row.queue_id for row in rows} == {home.id}, "the whole batch stays"

        moved = await async_client.patch(f"/api/v1/queue/batch/{batch}?queue_id={home.id}", json={"queue_id": twin.id})

        assert moved.status_code == 200, moved.text
        counts = {
            queue.id: queue.pending_count
            for queue in (
                await db_session.execute(
                    select(PrinterQueue)
                    .where(PrinterQueue.id.in_([home.id, twin.id]))
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }
        assert counts == {home.id: 0, twin.id: 2}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_next_rejects_manual_or_scheduled_work(
        self, async_client: AsyncClient, printer_factory, archive_factory
    ):
        _printer, queue = await printer_factory()
        archive = await archive_factory()

        response = await async_client.post(
            "/api/v1/queue/",
            json={"queue_id": queue.id, "archive_id": archive.id, "enqueue_position": "next", "manual_start": True},
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_single_copy_add_lists_just_itself(
        self, async_client: AsyncClient, printer_factory, archive_factory
    ):
        """The field is not "the batch" — it is what this call created, which for
        an ordinary add is one row. A caller must not have to special-case it."""
        _printer, queue = await printer_factory()
        archive = await archive_factory()

        response = await async_client.post("/api/v1/queue/", json={"queue_id": queue.id, "archive_id": archive.id})

        result = response.json()
        assert result["created_item_ids"] == [result["id"]]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_paused_queue_still_accepts_a_hand_placed_item(
        self, async_client: AsyncClient, printer_factory, archive_factory, db_session
    ):
        """Pause stops dispatch, not planning (2026-09-01).

        The m067 pause feature refused new items with a 409, which left the two
        dialogs disagreeing: "Print now" dispatches through the background
        dispatcher and never asked about the pause, so the very same file could
        be pushed at a parked printer while merely QUEUEING it was refused. The
        item now lands and waits; the pause is enforced where it belongs — the
        scheduler skips it and the auto-queue won't route here.
        """
        _printer, queue = await printer_factory()
        archive = await archive_factory()

        queue.is_paused = True
        await db_session.commit()

        response = await async_client.post("/api/v1/queue/", json={"queue_id": queue.id, "archive_id": archive.id})

        assert response.status_code == 200
        assert response.json()["status"] == "pending"

        # Adding must not resume anything — the operator's pause survives.
        await db_session.refresh(queue)
        assert queue.is_paused is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_archived_printer_takes_no_new_work(
        self, async_client: AsyncClient, printer_factory, archive_factory, db_session
    ):
        """Archiving retires a printer everywhere (m105) — this door was the
        last one that still accepted work for one.

        404, and the same wording the plan's enqueue endpoint uses: an archived
        printer is not "unavailable", it is gone from every list the operator
        can see, so "not found" is what the answer means. The queue row survives
        archiving (history), which is exactly why the queue id alone was enough
        to get past this handler.
        """
        printer, queue = await printer_factory()
        archive = await archive_factory()

        printer.archived = True
        await db_session.commit()

        response = await async_client.post("/api/v1/queue/", json={"queue_id": queue.id, "archive_id": archive.id})

        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Printer not found"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue_with_macro_selection(self, async_client: AsyncClient, printer_factory, archive_factory):
        """The ticked macros ride on the item as JSON text and come back a list."""
        _printer, queue = await printer_factory()
        archive = await archive_factory()

        response = await async_client.post(
            "/api/v1/queue/",
            json={"queue_id": queue.id, "archive_id": archive.id, "selected_macro_ids": [7, 9]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["selected_macro_ids"] == [7, 9]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_item_with_no_macro_selection_reads_back_as_null(
        self, async_client: AsyncClient, printer_factory, archive_factory
    ):
        """Every path without a dialog leaves it unset, and that means "run none"."""
        _printer, queue = await printer_factory()
        archive = await archive_factory()

        response = await async_client.post(
            "/api/v1/queue/",
            json={"queue_id": queue.id, "archive_id": archive.id},
        )
        assert response.status_code == 200, response.text
        assert response.json()["selected_macro_ids"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_an_empty_macro_selection_survives_as_empty(
        self, async_client: AsyncClient, printer_factory, archive_factory
    ):
        """ "I ticked nothing" must not be normalised away into "never asked"."""
        _printer, queue = await printer_factory()
        archive = await archive_factory()

        created = await async_client.post(
            "/api/v1/queue/",
            json={"queue_id": queue.id, "archive_id": archive.id, "selected_macro_ids": []},
        )
        assert created.status_code == 200, created.text
        assert created.json()["selected_macro_ids"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue_with_manual_start(
        self, async_client: AsyncClient, printer_factory, archive_factory, db_session
    ):
        """Verify item can be added to queue with manual_start=True."""
        printer, queue = await printer_factory()
        archive = await archive_factory()

        data = {
            "queue_id": queue.id,
            "archive_id": archive.id,
            "manual_start": True,
        }
        response = await async_client.post("/api/v1/queue/", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["queue_id"] == queue.id
        assert result["printer_id"] == printer.id
        assert result["archive_id"] == archive.id
        assert result["status"] == "pending"
        assert result["manual_start"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue_with_ams_mapping(
        self, async_client: AsyncClient, printer_factory, archive_factory, db_session
    ):
        """Verify item can be added to queue with ams_mapping."""
        printer, queue = await printer_factory()
        archive = await archive_factory()

        data = {
            "queue_id": queue.id,
            "archive_id": archive.id,
            "ams_mapping": [5, -1, 2, -1],  # Slot 1 -> tray 5, slot 3 -> tray 2
        }
        response = await async_client.post("/api/v1/queue/", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["queue_id"] == queue.id
        assert result["printer_id"] == printer.id
        assert result["archive_id"] == archive.id
        assert result["ams_mapping"] == [5, -1, 2, -1]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue_with_plate_id(
        self, async_client: AsyncClient, printer_factory, archive_factory, db_session
    ):
        """Verify item can be added to queue with plate_id for multi-plate 3MF."""
        _printer, queue = await printer_factory()
        archive = await archive_factory()

        data = {
            "queue_id": queue.id,
            "archive_id": archive.id,
            "plate_id": 3,
        }
        response = await async_client.post("/api/v1/queue/", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["plate_id"] == 3

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue_with_print_options(
        self, async_client: AsyncClient, printer_factory, archive_factory, db_session
    ):
        """Verify item can be added to queue with print options."""
        _printer, queue = await printer_factory()
        archive = await archive_factory()

        data = {
            "queue_id": queue.id,
            "archive_id": archive.id,
            "bed_levelling": False,
            "flow_cali": True,
            "layer_inspect": True,
            "timelapse": True,
            "use_ams": False,
        }
        response = await async_client.post("/api/v1/queue/", json=data)
        assert response.status_code == 200
        result = response.json()
        # Calibration fields are tri-state strings in the response; a legacy bool
        # in the request coerces (False -> 'off', True -> 'on').
        assert result["bed_levelling"] == "off"
        assert result["flow_cali"] == "on"
        assert result["layer_inspect"] is True
        assert result["timelapse"] is True
        assert result["use_ams"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue_auto_calibration_mode(
        self, async_client: AsyncClient, printer_factory, archive_factory, db_session
    ):
        """Tri-state 'auto' persists through create -> response -> GET."""
        _printer, queue = await printer_factory()
        archive = await archive_factory()
        response = await async_client.post(
            "/api/v1/queue/",
            json={"queue_id": queue.id, "archive_id": archive.id, "bed_levelling": "auto", "flow_cali": "off"},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["bed_levelling"] == "auto"
        assert result["flow_cali"] == "off"
        got = (await async_client.get(f"/api/v1/queue/{result['id']}")).json()
        assert got["bed_levelling"] == "auto"
        assert got["flow_cali"] == "off"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_queue_item_plate_id(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify queue item plate_id can be updated."""
        item = await queue_item_factory()
        response = await async_client.patch(f"/api/v1/queue/{item.id}", json={"plate_id": 5, "ams_mapping": None})
        assert response.status_code == 200
        result = response.json()
        assert result["plate_id"] == 5

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_queue_item_print_options(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify queue item print options can be updated."""
        item = await queue_item_factory()
        response = await async_client.patch(
            f"/api/v1/queue/{item.id}",
            json={
                "bed_levelling": False,
                "timelapse": True,
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["bed_levelling"] == "off"
        assert result["timelapse"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_queue_item(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify single queue item can be retrieved."""
        item = await queue_item_factory()
        response = await async_client.get(f"/api/v1/queue/{item.id}")
        assert response.status_code == 200
        assert response.json()["id"] == item.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_queue_item_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent queue item."""
        response = await async_client.get("/api/v1/queue/9999")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_queue_item(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify queue item can be updated."""
        item = await queue_item_factory()
        response = await async_client.patch(f"/api/v1/queue/{item.id}", json={"auto_off_after": True})
        assert response.status_code == 200
        result = response.json()
        assert result["auto_off_after"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_queue_item_manual_start(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify queue item manual_start can be updated."""
        item = await queue_item_factory(manual_start=False)
        response = await async_client.patch(f"/api/v1/queue/{item.id}", json={"manual_start": True})
        assert response.status_code == 200
        result = response.json()
        assert result["manual_start"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_start_unowned_item_credits_the_operator(
        self, async_client: AsyncClient, queue_item_factory, db_session
    ):
        """#1670: starting a queue item that has no owner attributes it to the
        operator who clicked Start, so the dispatched archive (our print log)
        isn't left with an empty User column when auth is on."""
        from sqlalchemy import select

        from backend.app.models.user import User

        item = await queue_item_factory(created_by_id=None, manual_start=True)

        response = await async_client.post(f"/api/v1/queue/{item.id}/start")
        assert response.status_code == 200

        admin = (await db_session.execute(select(User).where(User.username == "test_admin"))).scalar_one()
        await db_session.refresh(item)
        assert item.created_by_id == admin.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_start_preserves_existing_owner(self, async_client: AsyncClient, queue_item_factory, db_session):
        """An item added through the UI already carries its uploader — starting
        it must NOT overwrite that with the operator who clicked Start."""
        from backend.app.core.auth import get_password_hash
        from backend.app.models.user import User

        uploader = User(
            username="original_uploader",
            password_hash=get_password_hash("Test_Uploader1!"),
            role="user",
            is_active=True,
        )
        db_session.add(uploader)
        await db_session.commit()
        await db_session.refresh(uploader)

        item = await queue_item_factory(created_by_id=uploader.id, manual_start=True)

        response = await async_client.post(f"/api/v1/queue/{item.id}/start")
        assert response.status_code == 200

        await db_session.refresh(item)
        assert item.created_by_id == uploader.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_queue_item(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify queue item can be deleted."""
        item = await queue_item_factory()
        response = await async_client.delete(f"/api/v1/queue/{item.id}")
        assert response.status_code == 200
        assert response.json()["message"] == "Queue item deleted"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_queue_item_not_found(self, async_client: AsyncClient):
        """Verify 404 for deleting non-existent queue item."""
        response = await async_client.delete("/api/v1/queue/9999")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_delete_removes_only_failed_and_cancelled(
        self, async_client: AsyncClient, queue_item_factory, printer_factory, db_session
    ):
        """The Issues button sends what it shows; the server keeps every row that
        is not a finished failure/cancellation, whatever the caller listed."""
        from sqlalchemy import select as _select

        from backend.app.models.print_queue import PrintQueueItem

        _printer, queue = await printer_factory()
        failed = await queue_item_factory(queue_id=queue.id, status="failed")
        cancelled = await queue_item_factory(queue_id=queue.id, status="cancelled")
        skipped = await queue_item_factory(queue_id=queue.id, status="skipped")
        pending = await queue_item_factory(queue_id=queue.id, status="pending")
        printing = await queue_item_factory(queue_id=queue.id, status="printing")

        response = await async_client.post(
            "/api/v1/queue/bulk-delete",
            json={"item_ids": [failed.id, cancelled.id, skipped.id, pending.id, printing.id, 999999]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["deleted_count"] == 2
        assert body["skipped_count"] == 4  # skipped + pending + printing + unknown id
        # Ids first: after expire_all an attribute read is a lazy load, which async refuses.
        queue_id, kept = queue.id, {skipped.id, pending.id, printing.id}
        db_session.expire_all()
        left = (
            (await db_session.execute(_select(PrintQueueItem.id).where(PrintQueueItem.queue_id == queue_id)))
            .scalars()
            .all()
        )
        assert set(left) == kept

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_delete_a_repeated_id_is_one_row_not_a_phantom_skip(
        self, async_client: AsyncClient, queue_item_factory
    ):
        failed = await queue_item_factory(status="failed")

        response = await async_client.post("/api/v1/queue/bulk-delete", json={"item_ids": [failed.id, failed.id]})

        assert response.status_code == 200
        assert response.json() == {"deleted_count": 1, "skipped_count": 0, "message": "Deleted 1 items"}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_delete_rechecks_the_status_in_the_write_itself(self, queue_item_factory, db_session):
        """The status the list was built from can be stale by the time the rows
        go — a retry that committed in between turned one back into ``pending``.
        The claim re-checks the status inside a write (which also takes the
        writer on SQLite and the row locks on PostgreSQL) and answers only the
        rows still failed/cancelled."""
        from backend.app.api.routes.print_queue import _claim_still_deletable

        failed = await queue_item_factory(status="failed")
        retried = await queue_item_factory(status="pending")  # was failed when the list was read

        claimed = await _claim_still_deletable(db_session, [failed.id, retried.id])

        assert claimed == {failed.id}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_delete_deletes_only_what_the_claim_kept(
        self, async_client: AsyncClient, queue_item_factory, monkeypatch, db_session
    ):
        from sqlalchemy import select as _select

        from backend.app.api.routes import print_queue as route_module
        from backend.app.models.print_queue import PrintQueueItem

        a = await queue_item_factory(status="failed")
        b = await queue_item_factory(status="failed")
        a_id, b_id = a.id, b.id

        async def lost_the_race(db, ids):
            return {a_id}  # b was retried between the read and the write

        monkeypatch.setattr(route_module, "_claim_still_deletable", lost_the_race)

        response = await async_client.post("/api/v1/queue/bulk-delete", json={"item_ids": [a_id, b_id]})

        assert response.json() == {"deleted_count": 1, "skipped_count": 1, "message": "Deleted 1 items, skipped 1"}
        db_session.expire_all()
        left = (
            await db_session.execute(_select(PrintQueueItem.id).where(PrintQueueItem.id.in_([a_id, b_id])))
        ).scalars()
        assert set(left) == {b_id}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_delete_empty_list_is_refused(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/queue/bulk-delete", json={"item_ids": []})
        assert response.status_code == 400
        assert response.json()["detail"] == "No item IDs provided"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_delete_detaches_refs_for_exactly_the_deleted_rows(
        self, async_client: AsyncClient, queue_item_factory, printer_factory, monkeypatch
    ):
        """SQLite runs no FK actions: the partner rows are cleaned in code, and
        only for the rows that really go — a skipped row keeps its links."""
        from backend.app.services import queue_counters

        seen: list[list[int]] = []
        real = queue_counters.detach_print_queue_refs

        async def spy(db, item_ids):
            seen.append(sorted(item_ids))
            await real(db, item_ids)

        monkeypatch.setattr(queue_counters, "detach_print_queue_refs", spy)

        _printer, queue = await printer_factory()
        failed = await queue_item_factory(queue_id=queue.id, status="failed")
        pending = await queue_item_factory(queue_id=queue.id, status="pending")

        response = await async_client.post("/api/v1/queue/bulk-delete", json={"item_ids": [failed.id, pending.id]})

        assert response.status_code == 200
        assert seen == [[failed.id]]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_delete_touches_the_queue_counters_once_per_queue(
        self, async_client: AsyncClient, queue_item_factory, printer_factory, monkeypatch
    ):
        from backend.app.services import queue_counters

        touched: list[int] = []
        real = queue_counters.update_queue_counters

        async def spy(db, queue_id):
            touched.append(queue_id)
            await real(db, queue_id)

        monkeypatch.setattr(queue_counters, "update_queue_counters", spy)

        _p1, q1 = await printer_factory()
        _p2, q2 = await printer_factory()
        a = await queue_item_factory(queue_id=q1.id, status="failed")
        b = await queue_item_factory(queue_id=q1.id, status="cancelled")
        c = await queue_item_factory(queue_id=q2.id, status="failed")

        response = await async_client.post("/api/v1/queue/bulk-delete", json={"item_ids": [a.id, b.id, c.id]})

        assert response.status_code == 200
        assert sorted(touched) == sorted([q1.id, q2.id])

    # --- Soft-deleted (trashed) source archive (#1348 follow-up) -----------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_soft_delete_archive_cancels_pending_queue_items(
        self, db_session, printer_factory, archive_factory, queue_item_factory
    ):
        """Trashing an archive cancels its pending queue items (they can never
        dispatch — the 3MF is gone) but leaves historical rows untouched."""
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.archive_purge import archive_purge_service

        _printer, queue = await printer_factory()
        archive = await archive_factory()
        pending = await queue_item_factory(queue_id=queue.id, archive_id=archive.id, status="pending")
        completed = await queue_item_factory(queue_id=queue.id, archive_id=archive.id, status="completed")

        await archive_purge_service.move_to_trash(db_session, archive)

        await db_session.refresh(pending)
        await db_session.refresh(completed)
        pending_db = await db_session.get(PrintQueueItem, pending.id)
        completed_db = await db_session.get(PrintQueueItem, completed.id)
        assert pending_db.status == "cancelled"
        assert pending_db.waiting_reason == "Source archive deleted"
        # Historical row is left exactly as it was.
        assert completed_db.status == "completed"
        assert completed_db.waiting_reason is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_enrich_response_suppresses_surface_when_archive_soft_deleted(
        self, db_session, printer_factory, archive_factory, queue_item_factory
    ):
        """The serializer flags a trashed-archive row and suppresses every
        archive-derived field so the queue never serves a stale thumbnail."""
        from sqlalchemy import select as _select
        from sqlalchemy.orm import selectinload

        from backend.app.api.routes.print_queue import _enrich_response
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.services.archive_purge import archive_purge_service

        _printer, queue = await printer_factory()
        archive = await archive_factory(thumbnail_path="archives/x/thumb.png", print_name="Will Be Trashed")
        item = await queue_item_factory(queue_id=queue.id, archive_id=archive.id, status="completed")
        await archive_purge_service.move_to_trash(db_session, archive)

        loaded = (
            await db_session.execute(
                _select(PrintQueueItem)
                .options(
                    selectinload(PrintQueueItem.archive),
                    selectinload(PrintQueueItem.library_file),
                    selectinload(PrintQueueItem.created_by),
                )
                .where(PrintQueueItem.id == item.id)
            )
        ).scalar_one()
        resp = _enrich_response(loaded)
        assert resp.archive_deleted is True
        assert resp.archive_name is None
        assert resp.archive_thumbnail is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_enrich_response_exposes_surface_when_archive_live(
        self, db_session, printer_factory, archive_factory, queue_item_factory
    ):
        """Sanity guard: a live archive's fields still flow through unchanged."""
        from sqlalchemy import select as _select
        from sqlalchemy.orm import selectinload

        from backend.app.api.routes.print_queue import _enrich_response
        from backend.app.models.print_queue import PrintQueueItem

        _printer, queue = await printer_factory()
        archive = await archive_factory(thumbnail_path="archives/y/thumb.png", print_name="Live Print")
        item = await queue_item_factory(queue_id=queue.id, archive_id=archive.id, status="pending")

        loaded = (
            await db_session.execute(
                _select(PrintQueueItem)
                .options(
                    selectinload(PrintQueueItem.archive),
                    selectinload(PrintQueueItem.library_file),
                    selectinload(PrintQueueItem.created_by),
                )
                .where(PrintQueueItem.id == item.id)
            )
        ).scalar_one()
        resp = _enrich_response(loaded)
        assert resp.archive_deleted is False
        assert resp.archive_name == "Live Print"
        assert resp.archive_thumbnail == "archives/y/thumb.png"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_queue_row_tells_its_orders_status(
        self, db_session, printer_factory, archive_factory, queue_item_factory
    ):
        """WS-13 E13 final review: a clone keeps a row's order only while it is open — the row says
        whether it is, or «Clone» of a closed order's row could only come back 409."""
        from sqlalchemy import select as _select

        from backend.app.api.routes.print_queue import _enrich_response, queue_item_load_options
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.project import Project

        _printer, queue = await printer_factory()
        archive = await archive_factory(print_name="Closed order's print")
        order = Project(name="Shipped", status="completed")
        db_session.add(order)
        await db_session.commit()
        item = await queue_item_factory(queue_id=queue.id, archive_id=archive.id, status="pending")
        item.project_id = order.id
        await db_session.commit()
        loaded = (
            await db_session.execute(
                _select(PrintQueueItem)
                .options(*queue_item_load_options())
                .where(PrintQueueItem.id == item.id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        resp = _enrich_response(loaded)
        assert resp.project_name == "Shipped"
        assert resp.project_status == "completed"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_enrich_response_skips_plate_parse_when_archive_has_no_file_yet(
        self, db_session, printer_factory, archive_factory, queue_item_factory, monkeypatch
    ):
        """An archive created at print start has ``file_path=""`` until its 3MF
        arrives, and an empty path resolves to ``base_dir`` — a DIRECTORY, which
        ``exists()`` confirms. The three plate parsers then each opened the data
        directory as a ZIP on every queue poll ("Failed to extract print time
        from /app/data: Is a directory"). The row must be served with the
        archive's own numbers and nothing parsed.
        """
        from sqlalchemy import select as _select
        from sqlalchemy.orm import selectinload

        from backend.app.api.routes import print_queue as pq
        from backend.app.models.print_queue import PrintQueueItem

        called: list[tuple] = []
        monkeypatch.setattr(
            pq, "plate_metadata_cached", lambda path, plate: called.append((path, plate)) or (None, 0.0, None)
        )

        _printer, queue = await printer_factory()
        archive = await archive_factory(file_path="", print_name="Started From The Slicer", print_time_seconds=957)
        item = await queue_item_factory(queue_id=queue.id, archive_id=archive.id, status="printing", plate_id=10)

        loaded = (
            await db_session.execute(
                _select(PrintQueueItem)
                .options(
                    selectinload(PrintQueueItem.archive),
                    selectinload(PrintQueueItem.library_file),
                    selectinload(PrintQueueItem.created_by),
                )
                .where(PrintQueueItem.id == item.id)
            )
        ).scalar_one()
        resp = pq._enrich_response(loaded)

        assert called == [], f"parsed a path that is not a file: {called}"
        assert resp.archive_name == "Started From The Slicer"
        assert resp.print_time_seconds == 957


class TestQueueStartEndpoint:
    """Tests for the /queue/{item_id}/start endpoint."""

    @pytest.fixture
    async def printer_factory(self, db_session):
        """Factory to create test printers with queues."""
        _counter = [0]

        async def _create_printer(**kwargs):
            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "name": f"Test Printer {counter}",
                "ip_address": f"192.168.1.{100 + counter}",
                "serial_number": f"TESTSERIAL{counter:04d}",
                "access_code": "12345678",
                "model": "X1C",
            }
            defaults.update(kwargs)
            printer, queue = await _create_printer_with_queue(db_session, **defaults)
            return printer, queue

        return _create_printer

    @pytest.fixture
    async def archive_factory(self, db_session, tmp_path):
        """Factory to create test archives."""
        _counter = [0]

        async def _create_archive(**kwargs):
            from backend.app.models.archive import PrintArchive

            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "filename": f"test_print_{counter}.3mf",
                "print_name": f"Test Print {counter}",
                "file_path": str(
                    write_routing_3mf(
                        tmp_path / f"archive-{counter}.3mf",
                        {i: [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": "1"}] for i in (1, 2, 3, 5)},
                        model="X1C",
                    )
                ),
                "plate_index": 1,
                "file_size": 1024,
                "content_hash": f"testhash{counter:08d}",
                "status": "completed",
            }
            defaults.update(kwargs)

            archive = PrintArchive(**defaults)
            db_session.add(archive)
            await db_session.commit()
            await db_session.refresh(archive)
            return archive

        return _create_archive

    @pytest.fixture
    async def queue_item_factory(self, db_session, printer_factory, archive_factory):
        """Factory to create test queue items."""
        _counter = [0]

        async def _create_queue_item(**kwargs):
            from backend.app.models.print_queue import PrintQueueItem

            _counter[0] += 1
            counter = _counter[0]

            if "queue_id" not in kwargs:
                _printer, queue = await printer_factory()
                kwargs["queue_id"] = queue.id

            if "archive_id" not in kwargs:
                archive = await archive_factory()
                kwargs["archive_id"] = archive.id

            defaults = {
                "status": "pending",
                "position": counter,
            }
            defaults.update(kwargs)

            item = PrintQueueItem(**defaults)
            db_session.add(item)
            await db_session.commit()
            await db_session.refresh(item)
            return item

        return _create_queue_item

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_start_staged_queue_item(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify starting a staged (manual_start=True) queue item clears the flag."""
        item = await queue_item_factory(manual_start=True)
        assert item.manual_start is True

        response = await async_client.post(f"/api/v1/queue/{item.id}/start")
        assert response.status_code == 200
        result = response.json()
        assert result["manual_start"] is False
        assert result["status"] == "pending"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_start_non_staged_queue_item(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify starting a non-staged queue item still works (idempotent)."""
        item = await queue_item_factory(manual_start=False)
        assert item.manual_start is False

        response = await async_client.post(f"/api/v1/queue/{item.id}/start")
        assert response.status_code == 200
        result = response.json()
        assert result["manual_start"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_start_queue_item_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent queue item."""
        response = await async_client.post("/api/v1/queue/9999/start")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_start_non_pending_queue_item(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify 400 error when trying to start a non-pending queue item."""
        item = await queue_item_factory(status="printing", manual_start=True)

        response = await async_client.post(f"/api/v1/queue/{item.id}/start")
        assert response.status_code == 400
        assert "pending" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_start_completed_queue_item(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify 400 error when trying to start a completed queue item."""
        item = await queue_item_factory(status="completed", manual_start=True)

        response = await async_client.post(f"/api/v1/queue/{item.id}/start")
        assert response.status_code == 400


class TestQueueCancelEndpoint:
    """Tests for the /queue/{item_id}/cancel endpoint."""

    @pytest.fixture
    async def printer_factory(self, db_session):
        """Factory to create test printers with queues."""

        async def _create_printer(**kwargs):
            defaults = {
                "name": "Cancel Test Printer",
                "ip_address": "192.168.1.200",
                "serial_number": "TESTCANCEL001",
                "access_code": "12345678",
                "model": "X1C",
            }
            defaults.update(kwargs)
            printer, queue = await _create_printer_with_queue(db_session, **defaults)
            return printer, queue

        return _create_printer

    @pytest.fixture
    async def archive_factory(self, db_session, tmp_path):
        """Factory to create test archives."""

        async def _create_archive(**kwargs):
            from backend.app.models.archive import PrintArchive

            defaults = {
                "filename": "cancel_test.3mf",
                "print_name": "Cancel Test Print",
                "file_path": "/tmp/cancel_test.3mf",
                "file_size": 1024,
                "content_hash": "cancelhash001",
                "status": "completed",
            }
            defaults.update(kwargs)

            archive = PrintArchive(**defaults)
            db_session.add(archive)
            await db_session.commit()
            await db_session.refresh(archive)
            return archive

        return _create_archive

    @pytest.fixture
    async def queue_item_factory(self, db_session, printer_factory, archive_factory):
        """Factory to create test queue items."""

        async def _create_queue_item(**kwargs):
            from backend.app.models.print_queue import PrintQueueItem

            if "queue_id" not in kwargs:
                _printer, queue = await printer_factory()
                kwargs["queue_id"] = queue.id

            if "archive_id" not in kwargs:
                archive = await archive_factory()
                kwargs["archive_id"] = archive.id

            defaults = {
                "status": "pending",
                "position": 1,
            }
            defaults.update(kwargs)

            item = PrintQueueItem(**defaults)
            db_session.add(item)
            await db_session.commit()
            await db_session.refresh(item)
            return item

        return _create_queue_item

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cancel_pending_queue_item(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify cancelling a pending queue item."""
        item = await queue_item_factory(status="pending")

        response = await async_client.post(f"/api/v1/queue/{item.id}/cancel")
        assert response.status_code == 200
        assert response.json()["message"] == "Queue item cancelled"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cancel_non_pending_queue_item(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify 400 error when trying to cancel a non-pending queue item."""
        item = await queue_item_factory(status="printing")

        response = await async_client.post(f"/api/v1/queue/{item.id}/cancel")
        assert response.status_code == 400


class TestQueueRetryEndpoint:
    """Tests for the /queue/{item_id}/retry endpoint.

    Previously the endpoint only accepted ``failed`` items. After the
    cancel-during-dispatch fix, ``cancelled`` items also need to be able
    to come back to ``pending`` via the same restart action — these tests
    pin both branches plus the rejection of in-flight statuses.
    """

    @pytest.fixture
    async def printer_factory(self, db_session):
        _counter = [0]

        async def _create_printer(**kwargs):
            _counter[0] += 1
            i = _counter[0]
            defaults = {
                "name": f"Retry Test Printer {i}",
                "ip_address": f"192.168.1.{200 + i}",
                "serial_number": f"TESTRETRY{i:04d}",
                "access_code": "12345678",
                "model": "X1C",
            }
            defaults.update(kwargs)
            printer, queue = await _create_printer_with_queue(db_session, **defaults)
            return printer, queue

        return _create_printer

    @pytest.fixture
    async def archive_factory(self, db_session, tmp_path):
        _counter = [0]

        async def _create_archive(**kwargs):
            from backend.app.models.archive import PrintArchive

            _counter[0] += 1
            i = _counter[0]
            defaults = {
                "filename": f"retry_test_{i}.3mf",
                "print_name": f"Retry Test Print {i}",
                "file_path": f"/tmp/retry_test_{i}.3mf",
                "file_size": 1024,
                "content_hash": f"retryhash{i:08d}",
                "status": "completed",
            }
            defaults.update(kwargs)
            archive = PrintArchive(**defaults)
            db_session.add(archive)
            await db_session.commit()
            await db_session.refresh(archive)
            return archive

        return _create_archive

    @pytest.fixture
    async def queue_item_factory(self, db_session, printer_factory, archive_factory):
        async def _create_queue_item(**kwargs):
            from backend.app.models.print_queue import PrintQueueItem

            if "queue_id" not in kwargs:
                _printer, queue = await printer_factory()
                kwargs["queue_id"] = queue.id

            if "archive_id" not in kwargs:
                archive = await archive_factory()
                kwargs["archive_id"] = archive.id

            defaults = {"status": "pending", "position": 1}
            defaults.update(kwargs)
            item = PrintQueueItem(**defaults)
            db_session.add(item)
            await db_session.commit()
            await db_session.refresh(item)
            return item

        return _create_queue_item

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_retry_failed_item_flips_to_pending(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Existing behaviour: a ``failed`` item retried returns to ``pending``."""
        from backend.app.models.print_queue import PrintQueueItem

        item = await queue_item_factory(status="failed", error_message="Some old error", position=5)

        response = await async_client.post(f"/api/v1/queue/{item.id}/retry")
        assert response.status_code == 200, response.text

        # Re-fetch and verify side effects.
        await db_session.refresh(item)
        assert item.status == "pending"
        assert item.error_message is None
        # Position is bumped to end-of-pending.
        assert item.position >= 1
        _ = PrintQueueItem  # quiet lint

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_retry_cancelled_item_flips_to_pending(
        self, async_client: AsyncClient, queue_item_factory, db_session
    ):
        """New behaviour after the cancel-during-dispatch fix: ``cancelled`` items
        accept ``/retry`` and become ``pending`` again, with ``error_message`` cleared.
        """
        item = await queue_item_factory(status="cancelled", error_message="Cancelled by user", position=3)

        response = await async_client.post(f"/api/v1/queue/{item.id}/retry")
        assert response.status_code == 200, response.text

        await db_session.refresh(item)
        assert item.status == "pending"
        assert item.error_message is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_retry_rejects_in_flight_statuses(self, async_client: AsyncClient, queue_item_factory):
        """``/retry`` must reject ``pending``, ``printing``, ``completed`` —
        anything that isn't ``failed`` or ``cancelled``."""
        for bad_status in ("pending", "printing", "completed", "skipped"):
            item = await queue_item_factory(status=bad_status)
            response = await async_client.post(f"/api/v1/queue/{item.id}/retry")
            assert response.status_code == 400, (
                f"Expected 400 for status={bad_status}, got {response.status_code}: {response.text}"
            )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_retry_unknown_id_returns_404(self, async_client: AsyncClient):
        response = await async_client.post("/api/v1/queue/999999/retry")
        assert response.status_code == 404


class TestQueueLibraryFileSupport:
    """Tests for queue items with library_file_id (instead of archive_id)."""

    @pytest.fixture
    async def printer_factory(self, db_session):
        """Factory to create test printers with queues."""
        _counter = [0]

        async def _create_printer(**kwargs):
            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "name": f"Library Test Printer {counter}",
                "ip_address": f"192.168.1.{150 + counter}",
                "serial_number": f"TESTLIB{counter:04d}",
                "access_code": "12345678",
                "model": "X1C",
            }
            defaults.update(kwargs)
            printer, queue = await _create_printer_with_queue(db_session, **defaults)
            return printer, queue

        return _create_printer

    @pytest.fixture
    async def library_file_factory(self, db_session, tmp_path):
        """Factory to create test library files."""
        _counter = [0]

        async def _create_library_file(**kwargs):
            from backend.app.models.library import LibraryFile

            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                # .gcode.3mf, not a bare .3mf: only a sliced file can be queued,
                # and every dispatch path has always refused the plain container.
                "filename": f"library_test_{counter}.gcode.3mf",
                "file_path": str(
                    write_routing_3mf(
                        tmp_path / f"library-{counter}.gcode.3mf",
                        {
                            i: [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": "1"}]
                            for i in kwargs.pop("plates", (1,))
                        },
                        model="X1C",
                    )
                ),
                "file_size": 2048,
                "file_type": "3mf",
                "file_metadata": {"print_name": f"Library Print {counter}", "print_time_seconds": 3600},
            }
            defaults.update(kwargs)

            lib_file = LibraryFile(**defaults)
            db_session.add(lib_file)
            await db_session.commit()
            await db_session.refresh(lib_file)
            return lib_file

        return _create_library_file

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue_with_library_file(
        self, async_client: AsyncClient, printer_factory, library_file_factory, db_session
    ):
        """Verify item can be added to queue using library_file_id instead of archive_id."""
        printer, queue = await printer_factory()
        lib_file = await library_file_factory()

        data = {
            "queue_id": queue.id,
            "library_file_id": lib_file.id,
        }
        response = await async_client.post("/api/v1/queue/", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["queue_id"] == queue.id
        assert result["printer_id"] == printer.id
        assert result["library_file_id"] == lib_file.id
        assert result["archive_id"] is None
        assert result["status"] == "pending"
        assert result["library_file_name"] == "Library Print 1"
        assert result["print_time_seconds"] == 3600

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue_library_file_with_options(
        self, async_client: AsyncClient, printer_factory, library_file_factory, db_session
    ):
        """Verify library file queue item can have all options set."""
        _printer, queue = await printer_factory()
        lib_file = await library_file_factory(plates=(1, 2, 3))

        data = {
            "queue_id": queue.id,
            "library_file_id": lib_file.id,
            "ams_mapping": [1, 2, -1, -1],
            "plate_id": 2,
            "bed_levelling": False,
            "timelapse": True,
            "manual_start": True,
        }
        response = await async_client.post("/api/v1/queue/", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["library_file_id"] == lib_file.id
        assert result["ams_mapping"] == [1, 2, -1, -1]
        assert result["plate_id"] == 2
        assert result["bed_levelling"] == "off"
        assert result["timelapse"] is True
        assert result["manual_start"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue_requires_archive_or_library_file(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        """Verify 400 error when neither archive_id nor library_file_id provided."""
        _printer, queue = await printer_factory()

        data = {
            "queue_id": queue.id,
        }
        response = await async_client.post("/api/v1/queue/", json=data)
        assert response.status_code == 400
        assert (
            "archive_id" in response.json()["detail"].lower() or "library_file_id" in response.json()["detail"].lower()
        )

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_queue_item_with_library_file(
        self, async_client: AsyncClient, printer_factory, library_file_factory, db_session
    ):
        """Verify queue item with library_file_id can be updated."""
        from backend.app.models.print_queue import PrintQueueItem

        _printer, queue = await printer_factory()
        lib_file = await library_file_factory(plates=(1, 2, 3))

        # Create queue item directly
        item = PrintQueueItem(
            queue_id=queue.id,
            library_file_id=lib_file.id,
            status="pending",
            position=1,
        )
        db_session.add(item)
        await db_session.commit()
        await db_session.refresh(item)

        # Update the item
        response = await async_client.patch(
            f"/api/v1/queue/{item.id}",
            json={"auto_off_after": True, "plate_id": 3, "ams_mapping": None},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["auto_off_after"] is True
        assert result["plate_id"] == 3
        assert result["library_file_id"] == lib_file.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_queue_includes_library_file_info(
        self, async_client: AsyncClient, printer_factory, library_file_factory, db_session
    ):
        """Verify queue list includes library file metadata."""
        from backend.app.models.print_queue import PrintQueueItem

        _printer, queue = await printer_factory()
        lib_file = await library_file_factory(
            file_metadata={"print_name": "Custom Print Name", "print_time_seconds": 7200}
        )

        item = PrintQueueItem(
            queue_id=queue.id,
            library_file_id=lib_file.id,
            status="pending",
            position=1,
        )
        db_session.add(item)
        await db_session.commit()

        response = await async_client.get("/api/v1/queue/")
        assert response.status_code == 200
        items = response.json()
        assert len(items) >= 1

        # Find our item
        our_item = next((i for i in items if i["library_file_id"] == lib_file.id), None)
        assert our_item is not None
        assert our_item["library_file_name"] == "Custom Print Name"
        assert our_item["print_time_seconds"] == 7200


class TestBulkUpdateEndpoint:
    """Tests for the /queue/bulk endpoint."""

    @pytest.fixture
    async def printer_factory(self, db_session):
        """Factory to create test printers with queues."""
        _counter = [0]

        async def _create_printer(**kwargs):
            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "name": f"Bulk Test Printer {counter}",
                "ip_address": f"192.168.1.{150 + counter}",
                "serial_number": f"TESTBULK{counter:04d}",
                "access_code": "12345678",
                "model": "X1C",
            }
            defaults.update(kwargs)
            printer, queue = await _create_printer_with_queue(db_session, **defaults)
            return printer, queue

        return _create_printer

    @pytest.fixture
    async def archive_factory(self, db_session, tmp_path):
        """Factory to create test archives."""
        _counter = [0]

        async def _create_archive(**kwargs):
            from backend.app.models.archive import PrintArchive

            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "filename": f"bulk_test_{counter}.3mf",
                "print_name": f"Bulk Test Print {counter}",
                "file_path": str(
                    write_routing_3mf(
                        tmp_path / f"bulk_test_{counter}.3mf",
                        {1: [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": "1"}]},
                        model="X1C",
                    )
                ),
                "file_size": 1024,
                "content_hash": f"bulkhash{counter:04d}",
                "status": "completed",
            }
            defaults.update(kwargs)

            archive = PrintArchive(**defaults)
            db_session.add(archive)
            await db_session.commit()
            await db_session.refresh(archive)
            return archive

        return _create_archive

    @pytest.fixture
    async def queue_item_factory(self, db_session, printer_factory, archive_factory):
        """Factory to create test queue items."""

        async def _create_item(**kwargs):
            from backend.app.models.print_queue import PrintQueueItem

            if "queue_id" not in kwargs:
                _printer, queue = await printer_factory()
                kwargs["queue_id"] = queue.id

            if "archive_id" not in kwargs:
                archive = await archive_factory()
                kwargs["archive_id"] = archive.id

            defaults = {
                "status": "pending",
                "position": 1,
                "bed_levelling": True,
                "flow_cali": False,
            }
            defaults.update(kwargs)

            item = PrintQueueItem(**defaults)
            db_session.add(item)
            await db_session.commit()
            await db_session.refresh(item)
            return item

        return _create_item

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_update_single_field(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify bulk update can change a single field on multiple items."""
        item1 = await queue_item_factory(bed_levelling=True)
        item2 = await queue_item_factory(bed_levelling=True)

        response = await async_client.patch(
            "/api/v1/queue/bulk",
            json={"item_ids": [item1.id, item2.id], "bed_levelling": False},
        )
        assert response.status_code == 200
        result = response.json()
        assert result["updated_count"] == 2
        assert result["skipped_count"] == 0

        # Verify items were updated
        await db_session.refresh(item1)
        await db_session.refresh(item2)
        assert item1.bed_levelling is False
        assert item2.bed_levelling is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_update_multiple_fields(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify bulk update can change multiple fields at once."""
        item1 = await queue_item_factory(bed_levelling=True, flow_cali=False, manual_start=False)
        item2 = await queue_item_factory(bed_levelling=True, flow_cali=False, manual_start=False)

        response = await async_client.patch(
            "/api/v1/queue/bulk",
            json={
                "item_ids": [item1.id, item2.id],
                "bed_levelling": False,
                "flow_cali": True,
                "manual_start": True,
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["updated_count"] == 2

        await db_session.refresh(item1)
        assert item1.bed_levelling is False
        assert item1.flow_cali is True
        assert item1.manual_start is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_update_skips_non_pending(self, async_client: AsyncClient, queue_item_factory, db_session):
        """Verify bulk update skips non-pending items."""
        pending_item = await queue_item_factory(status="pending", bed_levelling=True)
        printing_item = await queue_item_factory(status="printing", bed_levelling=True)
        completed_item = await queue_item_factory(status="completed", bed_levelling=True)

        response = await async_client.patch(
            "/api/v1/queue/bulk",
            json={
                "item_ids": [pending_item.id, printing_item.id, completed_item.id],
                "bed_levelling": False,
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert result["updated_count"] == 1
        assert result["skipped_count"] == 2

        # Only pending item should be updated
        await db_session.refresh(pending_item)
        await db_session.refresh(printing_item)
        await db_session.refresh(completed_item)
        assert pending_item.bed_levelling is False
        assert printing_item.bed_levelling is True
        assert completed_item.bed_levelling is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_update_change_queue(
        self, async_client: AsyncClient, queue_item_factory, printer_factory, db_session
    ):
        """Verify bulk update can reassign items to a different queue."""
        from backend.app.models.printer_queue import PrinterQueue

        _new_printer, new_queue = await printer_factory(name="New Target Printer")
        item1 = await queue_item_factory()
        item2 = await queue_item_factory()

        original_queue_id = item1.queue_id
        left = {item1.queue_id, item2.queue_id}
        # Sentinels: only a recount of a queue moves its cached counter (G03 follow-up).
        await db_session.execute(
            PrinterQueue.__table__.update().where(PrinterQueue.id.in_([*left, new_queue.id])).values(pending_count=99)
        )
        await db_session.commit()

        response = await async_client.patch(
            "/api/v1/queue/bulk",
            json={"item_ids": [item1.id, item2.id], "queue_id": new_queue.id, "ams_mapping": None},
        )
        assert response.status_code == 200, response.text

        await db_session.refresh(item1)
        await db_session.refresh(item2)
        assert item1.queue_id == new_queue.id
        assert item2.queue_id == new_queue.id
        assert item1.queue_id != original_queue_id
        counts = {
            queue.id: queue.pending_count
            for queue in (
                await db_session.execute(
                    select(PrinterQueue)
                    .where(PrinterQueue.id.in_([*left, new_queue.id]))
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }
        assert counts == {**dict.fromkeys(left, 0), new_queue.id: 2}, "both the queues left and the one joined"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_update_empty_item_ids(self, async_client: AsyncClient):
        """Verify 400 error when item_ids is empty."""
        response = await async_client.patch(
            "/api/v1/queue/bulk",
            json={"item_ids": [], "bed_levelling": False},
        )
        assert response.status_code == 400
        assert "no item" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_update_no_fields(self, async_client: AsyncClient, queue_item_factory):
        """Verify 400 error when no fields to update."""
        item = await queue_item_factory()

        response = await async_client.patch(
            "/api/v1/queue/bulk",
            json={"item_ids": [item.id]},
        )
        assert response.status_code == 400
        assert "no fields" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bulk_update_invalid_queue(self, async_client: AsyncClient, queue_item_factory):
        """Verify 400 error when queue_id doesn't exist."""
        item = await queue_item_factory()

        response = await async_client.patch(
            "/api/v1/queue/bulk",
            json={"item_ids": [item.id], "queue_id": 99999},
        )
        assert response.status_code == 400
        assert "queue not found" in response.json()["detail"].lower()


class TestAbortedStatusNormalisation:
    """Tests for issue #558: 'aborted' queue status causes 500 error."""

    @pytest.fixture
    async def printer_factory(self, db_session):
        """Factory to create test printers with queues."""
        _counter = [0]

        async def _create_printer(**kwargs):
            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "name": f"Abort Test Printer {counter}",
                "ip_address": f"192.168.1.{60 + counter}",
                "serial_number": f"TESTABORT{counter:04d}",
                "access_code": "12345678",
                "model": "P1S",
            }
            defaults.update(kwargs)
            printer, queue = await _create_printer_with_queue(db_session, **defaults)
            return printer, queue

        return _create_printer

    @pytest.fixture
    async def archive_factory(self, db_session, tmp_path):
        """Factory to create test archives."""
        _counter = [0]

        async def _create_archive(**kwargs):
            from backend.app.models.archive import PrintArchive

            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "filename": f"abort_test_{counter}.3mf",
                "print_name": f"Abort Test Print {counter}",
                "file_path": f"/tmp/abort_test_{counter}.3mf",
                "file_size": 1024,
                "content_hash": f"aborthash{counter:06d}",
                "status": "completed",
            }
            defaults.update(kwargs)

            archive = PrintArchive(**defaults)
            db_session.add(archive)
            await db_session.commit()
            await db_session.refresh(archive)
            return archive

        return _create_archive

    @pytest.fixture
    async def queue_item_factory(self, db_session, printer_factory, archive_factory):
        """Factory to create test queue items."""
        _counter = [0]

        async def _create_queue_item(**kwargs):
            from backend.app.models.print_queue import PrintQueueItem

            _counter[0] += 1
            counter = _counter[0]

            if "queue_id" not in kwargs:
                _printer, queue = await printer_factory()
                kwargs["queue_id"] = queue.id
            if "archive_id" not in kwargs:
                archive = await archive_factory()
                kwargs["archive_id"] = archive.id

            defaults = {
                "status": "pending",
                "position": counter,
            }
            defaults.update(kwargs)

            item = PrintQueueItem(**defaults)
            db_session.add(item)
            await db_session.commit()
            await db_session.refresh(item)
            return item

        return _create_queue_item

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_on_print_complete_normalises_aborted_to_cancelled(self, queue_item_factory, db_session, caplog):
        """Verify the completion handler maps 'aborted' → 'cancelled' for queue items."""
        from unittest.mock import AsyncMock, patch

        from backend.app.models.archive import PrintArchive as _ArchiveForGuard

        item = await queue_item_factory(status="printing")

        from sqlalchemy.ext.asyncio import async_sessionmaker

        archive = await db_session.get(_ArchiveForGuard, item.archive_id)
        archive.status = "printing"
        archive.printer_id = item.queue_id
        await db_session.commit()
        factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

        with (
            patch("backend.app.main.async_session", new=factory),
            patch(
                "backend.app.services.archive_download_retry.archive_download_retry.retry_archive",
                new=AsyncMock(return_value="already_has_file"),
            ),
            patch("backend.app.main._report_spoolman_usage", new=AsyncMock()),
            patch("backend.app.services.notification_inbox.has_subscriber", new=AsyncMock(return_value=False)),
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.mqtt_relay") as mock_relay,
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.printer_manager") as mock_pm,
            # Keep printer effects outside this queue-status regression; no
            # fire-and-forget task may race the fixture's in-memory connection.
            patch("backend.app.main.spawn_background_task", side_effect=lambda coro, **kw: coro.close()),
        ):
            mock_ws.send_print_complete = AsyncMock()
            mock_ws.send_archive_updated = AsyncMock()
            mock_ws.broadcast = AsyncMock()
            mock_relay.on_print_complete = AsyncMock()
            mock_relay.on_queue_job_completed = AsyncMock()
            mock_notif.on_print_complete = AsyncMock()
            mock_notif._get_providers_for_event = AsyncMock(return_value=[])
            mock_plug.on_print_complete = AsyncMock()
            mock_pm.get_printer.return_value = None
            mock_pm.get_current_print_user.return_value = None
            mock_pm.get_status.return_value = None
            mock_pm.get_finishing_print_runs.return_value = ()
            mock_pm.get_current_print_run.return_value = None

            from backend.app.main import on_print_complete

            caplog.clear()
            await on_print_complete(
                item.queue_id,  # queue_id == printer_id
                {
                    "status": "aborted",
                    "filename": archive.filename,
                    "subtask_name": archive.print_name,
                    "timelapse_was_active": False,
                },
            )

        # The item status should be normalised to 'cancelled', not 'aborted'
        await db_session.refresh(item)
        assert item.status == "cancelled"
        assert not [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= 30]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_startup_fixup_converts_aborted_to_cancelled(self, queue_item_factory, db_session):
        """Verify the startup fixup converts existing 'aborted' rows to 'cancelled'."""
        from sqlalchemy import select

        from backend.app.models.print_queue import PrintQueueItem

        # Create items with various statuses including 'aborted'
        item_aborted = await queue_item_factory(status="pending")
        item_pending = await queue_item_factory(status="pending")

        # Manually set the invalid status
        item_aborted.status = "aborted"
        db_session.add(item_aborted)
        await db_session.commit()

        # Run the fixup query (same logic as lifespan)
        result = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.status == "aborted"))
        aborted_items = result.scalars().all()
        for i in aborted_items:
            i.status = "cancelled"
        await db_session.commit()

        # Verify: no more 'aborted' items
        result = await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.status == "aborted"))
        assert len(result.scalars().all()) == 0

        # The previously aborted item should now be 'cancelled'
        await db_session.refresh(item_aborted)
        assert item_aborted.status == "cancelled"

        # The pending item should be unchanged
        await db_session.refresh(item_pending)
        assert item_pending.status == "pending"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_completed_status_passes_through_unchanged(self, queue_item_factory, db_session, caplog):
        """Verify normal statuses like 'completed' are not affected by normalisation."""
        from unittest.mock import AsyncMock, patch

        from backend.app.models.archive import PrintArchive as _ArchiveForGuard

        item = await queue_item_factory(status="printing")

        from sqlalchemy.ext.asyncio import async_sessionmaker

        archive = await db_session.get(_ArchiveForGuard, item.archive_id)
        archive.status = "printing"
        archive.printer_id = item.queue_id
        await db_session.commit()
        factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

        with (
            patch("backend.app.main.async_session", new=factory),
            patch(
                "backend.app.services.archive_download_retry.archive_download_retry.retry_archive",
                new=AsyncMock(return_value="already_has_file"),
            ),
            patch("backend.app.main._report_spoolman_usage", new=AsyncMock()),
            patch("backend.app.services.notification_inbox.has_subscriber", new=AsyncMock(return_value=False)),
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.mqtt_relay") as mock_relay,
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.printer_manager") as mock_pm,
            # Keep printer effects outside this queue-status regression; no
            # fire-and-forget task may race the fixture's in-memory connection.
            patch("backend.app.main.spawn_background_task", side_effect=lambda coro, **kw: coro.close()),
        ):
            mock_ws.send_print_complete = AsyncMock()
            mock_ws.send_archive_updated = AsyncMock()
            mock_ws.broadcast = AsyncMock()
            mock_relay.on_print_complete = AsyncMock()
            mock_relay.on_queue_job_completed = AsyncMock()
            mock_notif.on_print_complete = AsyncMock()
            mock_notif._get_providers_for_event = AsyncMock(return_value=[])
            mock_plug.on_print_complete = AsyncMock()
            mock_pm.get_printer.return_value = None
            mock_pm.get_current_print_user.return_value = None
            mock_pm.get_status.return_value = None
            mock_pm.get_finishing_print_runs.return_value = ()
            mock_pm.get_current_print_run.return_value = None

            from backend.app.main import on_print_complete

            caplog.clear()
            await on_print_complete(
                item.queue_id,  # queue_id == printer_id
                {
                    "status": "completed",
                    "filename": archive.filename,
                    "subtask_name": archive.print_name,
                    "timelapse_was_active": False,
                },
            )

        await db_session.refresh(item)
        assert item.status == "completed"
        assert not [(r.name, r.getMessage()) for r in caplog.records if r.levelno >= 30]

    # ========================================================================
    # Library file usage tracking on print completion (#1008)
    #
    # These exercise the _bump_library_file_usage_if_completed helper directly
    # rather than invoking the whole on_print_complete handler — that path
    # spawns background asyncio tasks (notifications, MQTT relay, smart-plug)
    # that are expensive to mock and have nothing to do with the bump logic.
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bump_library_file_usage_on_completed(self, printer_factory, db_session):
        """Successful completion increments print_count and stamps last_printed_at."""
        from datetime import datetime, timezone

        from backend.app.main import _bump_library_file_usage_if_completed
        from backend.app.models.library import LibraryFile
        from backend.app.models.print_queue import PrintQueueItem

        _printer, queue = await printer_factory()
        lib_file = LibraryFile(
            filename="benchy.gcode.3mf",
            file_path="/data/library/benchy.gcode.3mf",
            file_type="gcode.3mf",
            file_size=1024,
            print_count=0,
            last_printed_at=None,
        )
        db_session.add(lib_file)
        await db_session.commit()
        await db_session.refresh(lib_file)

        item = PrintQueueItem(
            queue_id=queue.id,
            library_file_id=lib_file.id,
            status="printing",
            position=1,
        )

        before = datetime.now(timezone.utc).replace(tzinfo=None)
        await _bump_library_file_usage_if_completed(db_session, item, "completed")
        await db_session.commit()
        await db_session.refresh(lib_file)

        assert lib_file.print_count == 1
        assert lib_file.last_printed_at is not None
        assert lib_file.last_printed_at >= before

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bump_library_file_usage_repeated_prints_increment_count(self, printer_factory, db_session):
        """Each successful completion bumps print_count cumulatively."""
        from backend.app.main import _bump_library_file_usage_if_completed
        from backend.app.models.library import LibraryFile
        from backend.app.models.print_queue import PrintQueueItem

        _printer, queue = await printer_factory()
        lib_file = LibraryFile(
            filename="repeat.gcode.3mf",
            file_path="/data/library/repeat.gcode.3mf",
            file_type="gcode.3mf",
            file_size=1024,
            print_count=0,
        )
        db_session.add(lib_file)
        await db_session.commit()
        await db_session.refresh(lib_file)

        item = PrintQueueItem(
            queue_id=queue.id,
            library_file_id=lib_file.id,
            status="printing",
            position=1,
        )

        for _ in range(3):
            await _bump_library_file_usage_if_completed(db_session, item, "completed")

        await db_session.commit()
        await db_session.refresh(lib_file)
        assert lib_file.print_count == 3

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
    async def test_bump_library_file_usage_skips_non_completed(self, printer_factory, db_session, terminal_status):
        """Failed and cancelled prints must NOT count as usage."""
        from backend.app.main import _bump_library_file_usage_if_completed
        from backend.app.models.library import LibraryFile
        from backend.app.models.print_queue import PrintQueueItem

        _printer, queue = await printer_factory()
        lib_file = LibraryFile(
            filename="broken.gcode.3mf",
            file_path="/data/library/broken.gcode.3mf",
            file_type="gcode.3mf",
            file_size=1024,
            print_count=0,
            last_printed_at=None,
        )
        db_session.add(lib_file)
        await db_session.commit()
        await db_session.refresh(lib_file)

        item = PrintQueueItem(
            queue_id=queue.id,
            library_file_id=lib_file.id,
            status="printing",
            position=1,
        )

        await _bump_library_file_usage_if_completed(db_session, item, terminal_status)
        await db_session.commit()
        await db_session.refresh(lib_file)

        assert lib_file.print_count == 0
        assert lib_file.last_printed_at is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bump_library_file_usage_skips_when_no_library_file_id(
        self, printer_factory, archive_factory, db_session
    ):
        """Queue items without library_file_id (e.g. archive reprints) are a no-op."""
        from backend.app.main import _bump_library_file_usage_if_completed
        from backend.app.models.print_queue import PrintQueueItem

        _printer, queue = await printer_factory()
        archive = await archive_factory()
        item = PrintQueueItem(
            queue_id=queue.id,
            library_file_id=None,
            archive_id=archive.id,
            status="printing",
            position=1,
        )

        # Must not raise.
        await _bump_library_file_usage_if_completed(db_session, item, "completed")

    # ------------------------------------------------------------------
    # Direct-by-id helper (m014 — also used by the on_print_complete
    # external/direct-print branch, which has no queue_item to consult).
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bump_library_file_usage_direct_by_id_increments(self, db_session):
        """_bump_library_file_usage(db, id) bumps the row without needing a queue item."""
        from datetime import datetime, timezone

        from backend.app.main import _bump_library_file_usage
        from backend.app.models.library import LibraryFile

        lib_file = LibraryFile(
            filename="direct.3mf",
            file_path="/data/library/direct.3mf",
            file_type="3mf",
            file_size=1024,
            print_count=0,
            last_printed_at=None,
        )
        db_session.add(lib_file)
        await db_session.commit()
        await db_session.refresh(lib_file)

        before = datetime.now(timezone.utc).replace(tzinfo=None)
        await _bump_library_file_usage(db_session, lib_file.id)
        await db_session.commit()
        await db_session.refresh(lib_file)

        assert lib_file.print_count == 1
        assert lib_file.last_printed_at is not None
        assert lib_file.last_printed_at >= before

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bump_library_file_usage_direct_by_id_noop_on_none(self, db_session):
        """Passing None is a safe no-op (external prints with no library link)."""
        from backend.app.main import _bump_library_file_usage

        # Must not raise.
        await _bump_library_file_usage(db_session, None)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_bump_library_file_usage_direct_by_id_noop_on_missing(self, db_session):
        """Deleted library file id is a safe no-op."""
        from backend.app.main import _bump_library_file_usage

        await _bump_library_file_usage(db_session, 999_999)


# ======================================================================
# Filing a queued print under its order line (spec pass 7, Decision 4a)
# ======================================================================
#
# The caller names the ORDER (the Print dialog's picker sends both ids, but the
# API, the Telegram bot and every older client send only the order — and a row
# with no ``project_line_id`` is counted by no line of the plan block, which is
# how "still needed: 5" survived four of them being queued). The writer
# therefore resolves the line itself when the plate points at exactly one, and
# leaves it NULL when it does not: guessing between two lines files somebody's
# print against work nobody ordered.


async def _order_catalog(db_session, tmp_path, *, materials):
    """A product with one sliced whole-file plate, and an order whose lines carry
    ``materials`` (one line per entry). Returns (library file, project, lines)."""
    from backend.app.models.library import LibraryFile
    from backend.app.models.product import Product, ProductPart, ProductPlate
    from backend.app.models.project import Project
    from backend.app.models.project_line import ProjectLine

    lib_file = LibraryFile(
        filename="lamp.gcode.3mf",
        file_path=str(
            write_routing_3mf(
                tmp_path / "lamp.gcode.3mf",
                {1: [{"id": 1, "type": "PETG", "color": "#FFFFFF", "used_g": "1"}]},
                model="X1C",
            )
        ),
        file_type="gcode",
        file_size=1,
        file_metadata={
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {"1": "shade"},
                    "print_time_seconds": 100,
                    "filaments": [{"slot_id": 1, "type": "PETG"}],
                }
            ]
        },
    )
    product = Product(name="Lamp")
    db_session.add_all([lib_file, product])
    await db_session.flush()
    db_session.add_all(
        [
            ProductPart(
                product_id=product.id,
                kind="printed",
                name="shade",
                name_key="shade",
                qty_per_unit=1,
                aliases=["shade"],
            ),
            ProductPlate(product_id=product.id, library_file_id=lib_file.id, plate_index=0),
        ]
    )
    order = Project(name="O", status="active", priority="normal")
    db_session.add(order)
    await db_session.flush()
    lines = [
        ProjectLine(project_id=order.id, product_id=product.id, quantity=2, material=material, sort_order=i)
        for i, material in enumerate(materials)
    ]
    db_session.add_all(lines)
    await db_session.commit()
    return lib_file, order, lines


@pytest.mark.asyncio
@pytest.mark.integration
async def test_queueing_with_only_an_order_files_the_unambiguous_line(async_client: AsyncClient, db_session, tmp_path):
    from backend.app.models.print_queue import PrintQueueItem

    _printer, queue = await _create_printer_with_queue(
        db_session, name="Filing", ip_address="192.168.9.1", serial_number="FILING0001", access_code="12345678"
    )
    lib_file, order, lines = await _order_catalog(db_session, tmp_path, materials=["PLA", "PETG"])

    r = await async_client.post(
        "/api/v1/queue/",
        json={"queue_id": queue.id, "library_file_id": lib_file.id, "project_id": order.id, "plate_id": 1},
    )
    assert r.status_code == 200, r.text

    item = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == r.json()["id"]))).scalar_one()
    assert item.project_id == order.id
    # The plate is PETG, so the PLA line is out and the PETG line is the answer.
    assert item.project_line_id == lines[1].id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_queueing_leaves_the_line_null_when_two_lines_are_alike(async_client: AsyncClient, db_session, tmp_path):
    from backend.app.models.print_queue import PrintQueueItem

    _printer, queue = await _create_printer_with_queue(
        db_session, name="Twins", ip_address="192.168.9.2", serial_number="FILING0002", access_code="12345678"
    )
    lib_file, order, _lines = await _order_catalog(db_session, tmp_path, materials=["PETG", "PETG"])

    r = await async_client.post(
        "/api/v1/queue/",
        json={"queue_id": queue.id, "library_file_id": lib_file.id, "project_id": order.id, "plate_id": 1},
    )
    assert r.status_code == 200, r.text

    item = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == r.json()["id"]))).scalar_one()
    assert item.project_id == order.id
    assert item.project_line_id is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_explicit_line_is_never_overridden(async_client: AsyncClient, db_session, tmp_path):
    """The operator's own answer outranks anything derived here — even when the
    material rule would have chosen the other line."""
    from backend.app.models.print_queue import PrintQueueItem

    _printer, queue = await _create_printer_with_queue(
        db_session, name="Explicit", ip_address="192.168.9.3", serial_number="FILING0003", access_code="12345678"
    )
    lib_file, order, lines = await _order_catalog(db_session, tmp_path, materials=["PLA", "PETG"])

    r = await async_client.post(
        "/api/v1/queue/",
        json={
            "queue_id": queue.id,
            "library_file_id": lib_file.id,
            "project_id": order.id,
            "project_line_id": lines[0].id,
            "plate_id": 1,
        },
    )
    assert r.status_code == 200, r.text

    item = (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id == r.json()["id"]))).scalar_one()
    assert item.project_line_id == lines[0].id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_batch_writer_files_the_line_too(db_session, tmp_path):
    """``enqueue_batch_copies`` is the third door — the extra copies a
    quantity>1 direct print leaves behind — and it must file the same line the
    first copy went out under, or half a batch is counted and half is not."""
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.queue_batch import enqueue_batch_copies

    printer, _queue = await _create_printer_with_queue(
        db_session, name="Batch", ip_address="192.168.9.4", serial_number="FILING0004", access_code="12345678"
    )
    lib_file, order, lines = await _order_catalog(db_session, tmp_path, materials=["PLA", "PETG"])

    items, _batch = await enqueue_batch_copies(
        db_session, printer_id=printer.id, count=2, library_file_id=lib_file.id, plate_id=1, project_id=order.id
    )
    assert len(items) == 2
    rows = (
        (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.id.in_([i.id for i in items]))))
        .scalars()
        .all()
    )
    assert {row.project_line_id for row in rows} == {lines[1].id}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_queue_row_says_which_order_it_is_filed_under(async_client: AsyncClient, db_session, tmp_path):
    """``project_name`` rides on the row so the copy-queue dialog can show where
    a copy will land without a second request — and stays None under no order."""
    _printer, queue = await _create_printer_with_queue(
        db_session, name="Named", ip_address="192.168.9.3", serial_number="NAMED00001", access_code="12345678"
    )
    lib_file, order, _lines = await _order_catalog(db_session, tmp_path, materials=["PETG"])

    filed = await async_client.post(
        "/api/v1/queue/",
        json={"queue_id": queue.id, "library_file_id": lib_file.id, "project_id": order.id, "plate_id": 1},
    )
    assert filed.status_code == 200, filed.text
    assert filed.json()["project_name"] == "O"

    unfiled = await async_client.post(
        "/api/v1/queue/",
        json={"queue_id": queue.id, "library_file_id": lib_file.id, "plate_id": 1},
    )
    assert unfiled.status_code == 200, unfiled.text
    assert unfiled.json()["project_name"] is None

    listed = await async_client.get(f"/api/v1/queue/?queue_id={queue.id}")
    assert listed.status_code == 200, listed.text
    by_id = {row["id"]: row for row in listed.json()}
    assert by_id[filed.json()["id"]]["project_name"] == "O"
    assert by_id[unfiled.json()["id"]]["project_name"] is None


# --------------------------------------------------------------------------- #
# G03 D1 — a batch action touches only the queue the operator is looking at
# --------------------------------------------------------------------------- #
#
# AutoQueue promotion copies the router row's ``batch_id`` onto every printer row it creates, so one
# batch normally sits in several printer queues. Every ``/queue/batch/{batch_id}/…`` route used to take
# the queue of the batch's first row and change every pending copy farm-wide.

SPREAD = "spread-batch"


async def _printer_and_queue(db_session, n: int):
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue

    printer = Printer(
        name=f"Spread {n}", ip_address=f"192.168.77.{n}", serial_number=f"SPREAD{n:04d}", access_code="12345678"
    )
    db_session.add(printer)
    await db_session.flush()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id, pending_count=99)
    db_session.add(queue)
    await db_session.flush()
    return queue


@pytest.fixture
async def spread_batch(db_session):
    """Two printers, each holding two copies of one batch between two jobs of its own.

    Positions 1..4: other, copy, copy, late. ``pending_count`` starts at a sentinel 99 so only a
    recount of THAT queue can move it.
    """
    queues = [await _printer_and_queue(db_session, 1), await _printer_and_queue(db_session, 2)]
    rows: dict[int, list[PrintQueueItem]] = {}
    for queue in queues:
        rows[queue.id] = [
            PrintQueueItem(queue_id=queue.id, status="pending", position=1),
            PrintQueueItem(queue_id=queue.id, status="pending", position=2, batch_id=SPREAD),
            PrintQueueItem(queue_id=queue.id, status="pending", position=3, batch_id=SPREAD),
            PrintQueueItem(queue_id=queue.id, status="pending", position=4),
        ]
        db_session.add_all(rows[queue.id])
    await db_session.commit()
    return queues[0], queues[1], rows


async def _state(db_session, queue_id: int):
    """(id, status, position, batch_id, manual_start) of every row of the queue, and its cached counter."""
    from backend.app.models.printer_queue import PrinterQueue

    rows = (
        await db_session.execute(
            select(PrintQueueItem)
            .where(PrintQueueItem.queue_id == queue_id)
            .order_by(PrintQueueItem.id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    state = [(r.id, r.status, r.position, r.batch_id, r.manual_start) for r in rows]
    queue = await db_session.get(PrinterQueue, queue_id, populate_existing=True)
    return state, queue.pending_count


_BATCH_DOORS = [
    ("post", "ungroup", None),
    ("post", "cancel", None),
    ("post", "skip", None),
    ("post", "reorder?direction=up", None),
    ("post", "bump", None),
    ("post", "bump-bottom", None),
    ("patch", "", {"manual_start": True}),
    ("post", "clone", None),
]


def _batch_url(suffix: str, queue_id: int | None = None) -> str:
    url = f"/api/v1/queue/batch/{SPREAD}" + (f"/{suffix}" if suffix else "")
    if queue_id is None:
        return url
    return url + ("&" if "?" in url else "?") + f"queue_id={queue_id}"


async def _call(client, method: str, url: str, body):
    return await (client.patch(url, json=body) if method == "patch" else client.post(url))


@pytest.mark.asyncio
@pytest.mark.integration
class TestABatchActsOnItsOwnQueue:
    @pytest.mark.parametrize(("method", "suffix", "body"), _BATCH_DOORS)
    async def test_a_batch_over_several_queues_needs_its_queue_named(
        self, async_client, db_session, spread_batch, method, suffix, body
    ):
        a, b, _rows = spread_batch
        before = (await _state(db_session, a.id), await _state(db_session, b.id))

        response = await _call(async_client, method, _batch_url(suffix), body)

        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "batch_spans_queues"
        assert (await _state(db_session, a.id), await _state(db_session, b.id)) == before

    async def test_cancel_touches_only_the_named_queue_and_dates_its_rows(self, async_client, db_session, spread_batch):
        a, b, rows = spread_batch
        b_before = await _state(db_session, b.id)

        response = await async_client.post(_batch_url("cancel", a.id))

        assert response.status_code == 200, response.text
        assert response.json()["cancelled"] == 2
        state, pending_count = await _state(db_session, a.id)
        assert [s[1] for s in state] == ["pending", "cancelled", "cancelled", "pending"]
        assert pending_count == 2, "the named queue is recounted"
        for row in rows[a.id][1:3]:
            await db_session.refresh(row)
            assert row.completed_at is not None, "a cancel is dated for the previous-success gate"
        assert await _state(db_session, b.id) == b_before, "another printer's copies and counter are untouched"

    async def test_cancel_leaves_a_copy_the_scheduler_claimed_meanwhile(self, async_client, db_session, spread_batch):
        """The write is a compare-and-set on ``pending``: a row claimed after the read stays printing."""
        from sqlalchemy import update

        a, _b, rows = spread_batch
        await db_session.execute(
            update(PrintQueueItem)
            .where(PrintQueueItem.id == rows[a.id][1].id)
            .values(status="printing")
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()

        response = await async_client.post(_batch_url("cancel", a.id))

        assert response.status_code == 200, response.text
        assert response.json()["cancelled"] == 1
        state, _count = await _state(db_session, a.id)
        assert [s[1] for s in state] == ["pending", "printing", "cancelled", "pending"]

    async def test_skip_touches_only_the_named_queue(self, async_client, db_session, spread_batch):
        a, b, _rows = spread_batch
        a_before = await _state(db_session, a.id)

        response = await async_client.post(_batch_url("skip", b.id))

        assert response.status_code == 200, response.text
        assert response.json()["skipped"] == 2
        state, pending_count = await _state(db_session, b.id)
        assert [s[1] for s in state] == ["pending", "skipped", "skipped", "pending"]
        assert pending_count == 2
        assert await _state(db_session, a.id) == a_before

    @pytest.mark.parametrize(
        ("suffixes", "positions"),
        [
            # other, copy, copy, late
            (["reorder?direction=up"], [3, 1, 2, 4]),
            # Bumps renumber from 1 too: 0 is the printer's direct-print claim.
            (["bump"], [3, 1, 2, 4]),
            # Straight from 1-based positions: the copies sit just above the last row.
            (["bump-bottom"], [1, 3, 4, 2]),
        ],
    )
    async def test_a_block_move_moves_the_named_queues_copies(
        self, async_client, db_session, spread_batch, suffixes, positions
    ):
        a, b, _rows = spread_batch
        a_before = await _state(db_session, a.id)

        for suffix in suffixes:
            response = await async_client.post(_batch_url(suffix, b.id))
            assert response.status_code == 200, response.text
            assert response.json()["batch_size"] == 2

        state, pending_count = await _state(db_session, b.id)
        assert [s[2] for s in state] == positions
        assert pending_count == 4
        assert await _state(db_session, a.id) == a_before

    async def test_patch_changes_only_the_named_queues_copies(self, async_client, db_session, spread_batch):
        a, b, _rows = spread_batch
        b_before = await _state(db_session, b.id)

        response = await async_client.patch(_batch_url("", a.id), json={"manual_start": True})

        assert response.status_code == 200, response.text
        assert response.json()["updated"] == 2
        state, pending_count = await _state(db_session, a.id)
        assert [s[4] for s in state] == [False, True, True, False]
        assert pending_count == 4, "the named queue is recounted"
        assert await _state(db_session, b.id) == b_before

    async def test_ungroup_releases_only_the_named_queues_copies(self, async_client, db_session, spread_batch):
        a, b, _rows = spread_batch

        response = await async_client.post(_batch_url("ungroup", a.id))

        assert response.status_code == 200, response.text
        assert response.json()["ungrouped"] == 2
        state, _count = await _state(db_session, a.id)
        assert [s[3] for s in state] == [None, None, None, None]
        state, pending_count = await _state(db_session, b.id)
        assert [s[3] for s in state] == [None, SPREAD, SPREAD, None]
        assert pending_count == 99

    async def test_clone_copies_only_the_named_queues_copies_into_that_queue(
        self, async_client, db_session, spread_batch
    ):
        a, b, _rows = spread_batch
        a_before = await _state(db_session, a.id)

        response = await async_client.post(_batch_url("clone", b.id))

        assert response.status_code == 200, response.text
        assert response.json()["cloned"] == 2
        new_batch = response.json()["new_batch_id"]
        state, pending_count = await _state(db_session, b.id)
        clones = [s for s in state if s[3] == new_batch]
        assert [s[2] for s in clones] == [5, 6], "appended after the queue's own last position"
        assert pending_count == 6
        assert await _state(db_session, a.id) == a_before

    async def test_a_batch_in_one_queue_needs_no_queue_named(self, async_client, db_session, spread_batch):
        """Finished copies elsewhere do not spread the batch: only active rows decide it."""
        a, b, rows = spread_batch
        for row in rows[b.id][1:3]:
            row.status = "completed"
        await db_session.commit()

        response = await async_client.post(_batch_url("cancel"))

        assert response.status_code == 200, response.text
        assert response.json() == {"cancelled": 2, "batch_id": SPREAD, "queue_id": a.id}

    async def test_a_queue_with_none_of_the_batch_is_a_404(self, async_client, db_session, spread_batch):
        stranger = await _printer_and_queue(db_session, 3)
        await db_session.commit()

        response = await async_client.post(_batch_url("bump", stranger.id))

        assert response.status_code == 404, response.text

    async def test_a_rows_own_batch_doors_stay_in_its_queue(self, async_client, db_session, spread_batch):
        """The per-row doors with batch cohesion: block moves, the manual-start toggle, clone scope=batch."""
        a, b, rows = spread_batch
        b_before = await _state(db_session, b.id)
        copy = rows[a.id][1]

        bumped = await async_client.post(f"/api/v1/queue/{copy.id}/bump")
        toggled = await async_client.patch(f"/api/v1/queue/{copy.id}/manual-start")
        cloned = await async_client.post(f"/api/v1/queue/{copy.id}/clone?scope=batch")

        assert bumped.status_code == 200, bumped.text
        assert bumped.json()["block_size"] == 2
        assert toggled.status_code == 200, toggled.text
        assert cloned.status_code == 200, cloned.text
        state, _count = await _state(db_session, a.id)
        assert len(state) == 6, "two clones, in this queue"
        assert [s[4] for s in state[:4]] == [False, True, True, False]
        assert await _state(db_session, b.id) == b_before


# --------------------------------------------------------------------------- #
# G03 D9 / E8 — the list takes several statuses and orders by position, then id
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.integration
class TestTheQueueListTakesSeveralStatuses:
    @pytest.fixture
    async def mixed(self, db_session):
        queue = await _printer_and_queue(db_session, 1)
        rows = [
            PrintQueueItem(queue_id=queue.id, status="pending", position=3),
            PrintQueueItem(queue_id=queue.id, status="failed", position=1),
            PrintQueueItem(queue_id=queue.id, status="printing", position=0),
            PrintQueueItem(queue_id=queue.id, status="pending", position=1),
        ]
        db_session.add_all(rows)
        await db_session.commit()
        return queue, rows

    async def test_a_comma_separated_status_is_a_set(self, async_client, mixed, monkeypatch):
        from unittest.mock import AsyncMock

        from backend.app.services import queue_virtual

        virtual = AsyncMock(return_value=[])
        monkeypatch.setattr(queue_virtual, "build_virtual_current_prints", virtual)
        _queue, rows = mixed

        response = await async_client.get("/api/v1/queue/?status=pending, failed")

        assert response.status_code == 200, response.text
        # Position, then id: the two rows at position 1 come in insertion order.
        assert [r["id"] for r in response.json()] == [rows[1].id, rows[3].id, rows[0].id]
        virtual.assert_not_awaited()

    async def test_printing_in_the_set_brings_the_current_prints(self, async_client, mixed, monkeypatch):
        from unittest.mock import AsyncMock

        from backend.app.services import queue_virtual

        virtual = AsyncMock(return_value=[])
        monkeypatch.setattr(queue_virtual, "build_virtual_current_prints", virtual)
        _queue, rows = mixed

        response = await async_client.get("/api/v1/queue/?status=pending,printing")

        assert response.status_code == 200, response.text
        assert [r["id"] for r in response.json()] == [rows[2].id, rows[3].id, rows[0].id]
        virtual.assert_awaited_once()

    async def test_a_single_status_answers_as_before(self, async_client, mixed):
        _queue, rows = mixed

        response = await async_client.get("/api/v1/queue/?status=failed")

        assert response.status_code == 200, response.text
        assert [r["id"] for r in response.json()] == [rows[1].id]


# --------------------------------------------------------------------------- #
# G03 D6 / D7 — the add doors
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("field", ["archive_id", "library_file_id"])
async def test_a_zero_source_id_is_refused_before_anything(async_client, db_session, field):
    queue = await _printer_and_queue(db_session, 1)
    await db_session.commit()
    other = "library_file_id" if field == "archive_id" else "archive_id"

    response = await async_client.post("/api/v1/queue/", json={"queue_id": queue.id, field: 0, other: 5})

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_trashed_library_file_is_refused_before_the_copy(async_client, db_session, tmp_path, monkeypatch):
    """The AutoQueue door refused a trashed file before copying it; the printer door paid a copy first."""
    from datetime import datetime, timezone

    from backend.app.models.library import LibraryFile
    from backend.app.services import queue_sources

    queue = await _printer_and_queue(db_session, 1)
    path = write_routing_3mf(
        tmp_path / "trashed.gcode.3mf", {1: [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": "1"}]}
    )
    lib = LibraryFile(
        filename="trashed.gcode.3mf",
        file_path=str(path),
        file_type="3mf",
        file_size=path.stat().st_size,
        deleted_at=datetime.now(timezone.utc),
    )
    db_session.add(lib)
    await db_session.commit()

    async def no_copy(_request):  # pragma: no cover - reaching it is the failure
        raise AssertionError("a trashed file must be refused before it is copied")

    monkeypatch.setattr(queue_sources, "capture", no_copy)

    response = await async_client.post("/api/v1/queue/", json={"queue_id": queue.id, "library_file_id": lib.id})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "Library file not found"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_unskip_reads_the_failure_by_its_end_then_its_id(db_session):
    """G03 E3a: the acknowledgement reads the row the gate reads -- ``completed_at DESC NULLS LAST`` (an
    undated cancel sorted first on PostgreSQL) and the id for rows that ended at the same moment."""
    from datetime import datetime, timezone

    from backend.app.api.routes.print_queue import _acknowledge_blocking_failure

    queue = await _printer_and_queue(db_session, 1)
    moment = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    failure = PrintQueueItem(queue_id=queue.id, status="failed", position=0, completed_at=moment)
    db_session.add(failure)
    await db_session.flush()
    db_session.add(PrintQueueItem(queue_id=queue.id, status="completed", position=0, completed_at=moment))
    await db_session.commit()

    statements: list[str] = []
    engine = db_session.bind.sync_engine

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        await _acknowledge_blocking_failure(db_session, queue.id)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    await db_session.commit()

    await db_session.refresh(failure)
    assert failure.gate_acknowledged is False, "the newer row by id is the success: nothing is gating"
    assert any("completed_at DESC NULLS LAST, print_queue.id DESC" in s for s in statements), statements


@pytest.mark.asyncio
@pytest.mark.integration
async def test_one_copy_from_the_batch_writer_is_no_batch(db_session, tmp_path):
    """Every other door batches only 2+ copies (G03 D7.5); the plan's single plate got batch chrome."""
    from backend.app.services.queue_batch import enqueue_batch_copies

    printer, _queue = await _create_printer_with_queue(
        db_session, name="Single", ip_address="192.168.9.8", serial_number="SINGLE0001", access_code="12345678"
    )
    lib_file, _order, _lines = await _order_catalog(db_session, tmp_path, materials=["PLA"])

    one, one_batch = await enqueue_batch_copies(
        db_session, printer_id=printer.id, count=1, library_file_id=lib_file.id, plate_id=1
    )
    two, two_batch = await enqueue_batch_copies(
        db_session, printer_id=printer.id, count=2, library_file_id=lib_file.id, plate_id=1
    )

    assert one_batch is None and [item.batch_id for item in one] == [None]
    assert two_batch and {item.batch_id for item in two} == {two_batch}
