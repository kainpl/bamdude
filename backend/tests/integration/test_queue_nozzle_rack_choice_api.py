"""The rack-position pick has to survive the round trip (upstream #1784, 3954d3a7).

Upstream's first cut declared the field on the update and response schemas but
not on the create one, so Pydantic dropped it from every POST without a word:
the item carried no pick, the dispatcher assigned positions itself, and the
print ran from hotends the operator had not chosen. A silently-dropped field is
invisible at every layer above it, so it is pinned here at the layer it
crosses — HTTP in, database out — for the queue and for both direct-print
requests, which here reach the dispatcher without a queue row's help.
"""

import pytest
from httpx import AsyncClient

from backend.tests.fixtures.filament_routing_cases import write_routing_3mf

pytestmark = pytest.mark.integration


@pytest.fixture
async def queue(db_session):
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue

    printer = Printer(
        name="Rack-1",
        ip_address="192.168.1.210",
        serial_number="RACKCHOICE0001",
        access_code="12345678",
        model="X1C",
    )
    db_session.add(printer)
    await db_session.commit()
    await db_session.refresh(printer)
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    await db_session.refresh(queue)
    return queue


@pytest.fixture
async def archive(db_session, tmp_path):
    from backend.app.models.archive import PrintArchive

    path = write_routing_3mf(
        tmp_path / "benchy.3mf", {1: [{"id": 1, "type": "PLA", "color": "#FFFFFF", "used_g": "1"}]}, model="X1C"
    )
    archive = PrintArchive(
        filename="benchy.gcode.3mf",
        file_path=str(path),
        plate_index=1,
        file_size=path.stat().st_size,
        content_hash="rackchoicehash01",
        status="completed",
    )
    db_session.add(archive)
    await db_session.commit()
    await db_session.refresh(archive)
    return archive


async def _stored_choice(db_session, item_id):
    """What actually landed in the column, not what the response echoed."""
    from backend.app.models.print_queue import PrintQueueItem

    db_session.expire_all()
    item = await db_session.get(PrintQueueItem, item_id)
    return item.nozzle_rack_choice


async def _add(async_client, queue, archive, **extra):
    response = await async_client.post("/api/v1/queue/", json={"queue_id": queue.id, "archive_id": archive.id, **extra})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
class TestCreate:
    async def test_a_pick_posted_on_create_reaches_the_column(
        self, async_client: AsyncClient, queue, archive, db_session
    ):
        # Group 2 to rack position 1, group 1 to position 3 — the pick
        # BambuStudio dispatched as [16, 1, 18] on 2026-08-13.
        result = await _add(async_client, queue, archive, nozzle_rack_choice={"2": 1, "1": 3})

        assert result["nozzle_rack_choice"] == {"2": 1, "1": 3}
        assert await _stored_choice(db_session, result["id"]) is not None

    async def test_every_copy_of_a_quantity_carries_it(self, async_client: AsyncClient, queue, archive, db_session):
        result = await _add(async_client, queue, archive, quantity=2, nozzle_rack_choice={"2": 1})

        for item_id in result["created_item_ids"]:
            assert await _stored_choice(db_session, item_id) is not None

    async def test_creating_without_one_leaves_the_column_null(
        self, async_client: AsyncClient, queue, archive, db_session
    ):
        """Null is the signal to assign positions at dispatch."""
        result = await _add(async_client, queue, archive)

        assert result["nozzle_rack_choice"] is None
        assert await _stored_choice(db_session, result["id"]) is None


@pytest.mark.asyncio
class TestUpdate:
    async def test_editing_an_item_replaces_its_pick(self, async_client: AsyncClient, queue, archive):
        item_id = (await _add(async_client, queue, archive, nozzle_rack_choice={"2": 1, "1": 3}))["id"]

        response = await async_client.patch(f"/api/v1/queue/{item_id}", json={"nozzle_rack_choice": {"2": 1, "1": 2}})

        assert response.status_code == 200, response.text
        assert response.json()["nozzle_rack_choice"] == {"2": 1, "1": 2}

    async def test_clearing_the_pick_hands_the_choice_back_to_the_dispatcher(
        self, async_client: AsyncClient, queue, archive, db_session
    ):
        item_id = (await _add(async_client, queue, archive, nozzle_rack_choice={"2": 1, "1": 3}))["id"]

        response = await async_client.patch(f"/api/v1/queue/{item_id}", json={"nozzle_rack_choice": None})

        assert response.status_code == 200, response.text
        assert response.json()["nozzle_rack_choice"] is None
        assert await _stored_choice(db_session, item_id) is None

    async def test_an_unrelated_edit_does_not_disturb_the_pick(self, async_client: AsyncClient, queue, archive):
        item_id = (await _add(async_client, queue, archive, nozzle_rack_choice={"2": 1, "1": 3}))["id"]

        response = await async_client.patch(f"/api/v1/queue/{item_id}", json={"manual_start": True})

        assert response.status_code == 200, response.text
        assert response.json()["nozzle_rack_choice"] == {"2": 1, "1": 3}


@pytest.fixture
async def other_queue(db_session):
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue

    printer = Printer(
        name="Rack-2", ip_address="192.168.1.211", serial_number="RACKCHOICE0002", access_code="12345678", model="X1C"
    )
    db_session.add(printer)
    await db_session.flush()
    queue = PrinterQueue(id=printer.id, printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    await db_session.refresh(queue)
    return queue


@pytest.mark.asyncio
class TestAMoveLeavesThePickBehind:
    """G03 E6: the pick names rack positions of the printer the job was queued for. Moved through the API
    to another printer it fitted by chance or refused the print after the upload and halted that queue."""

    async def test_a_plain_move_clears_it(self, async_client, queue, other_queue, archive, db_session):
        item_id = (await _add(async_client, queue, archive, nozzle_rack_choice={"2": 1}))["id"]

        response = await async_client.patch(f"/api/v1/queue/{item_id}", json={"queue_id": other_queue.id})

        assert response.status_code == 200, response.text
        assert response.json()["nozzle_rack_choice"] is None
        assert await _stored_choice(db_session, item_id) is None

    async def test_a_move_that_picks_again_keeps_the_new_pick(self, async_client, queue, other_queue, archive):
        item_id = (await _add(async_client, queue, archive, nozzle_rack_choice={"2": 1}))["id"]

        response = await async_client.patch(
            f"/api/v1/queue/{item_id}", json={"queue_id": other_queue.id, "nozzle_rack_choice": {"2": 3}}
        )

        assert response.status_code == 200, response.text
        assert response.json()["nozzle_rack_choice"] == {"2": 3}

    async def test_a_bulk_move_clears_it(self, async_client, queue, other_queue, archive, db_session):
        item_id = (await _add(async_client, queue, archive, nozzle_rack_choice={"2": 1}))["id"]

        response = await async_client.patch(
            "/api/v1/queue/bulk", json={"item_ids": [item_id], "queue_id": other_queue.id}
        )

        assert response.status_code == 200, response.text
        assert response.json()["updated_count"] == 1
        assert await _stored_choice(db_session, item_id) is None

    async def test_a_batch_move_clears_it(self, async_client, queue, other_queue, archive, db_session):
        created = await _add(async_client, queue, archive, quantity=2, nozzle_rack_choice={"2": 1})

        response = await async_client.patch(
            f"/api/v1/queue/batch/{created['batch_id']}?queue_id={queue.id}", json={"queue_id": other_queue.id}
        )

        assert response.status_code == 200, response.text
        for item_id in created["created_item_ids"]:
            assert await _stored_choice(db_session, item_id) is None


class TestSchemaCoverage:
    def test_every_request_that_can_carry_a_pick_declares_it(self):
        """The original bug in one assertion: it was on two of the three.

        Here the direct prints count too — a reprint and a library print reach
        the dispatcher as job options, and a field missing from their request
        schema would be dropped just as silently.
        """
        from backend.app.schemas.archive import ReprintRequest
        from backend.app.schemas.library import FilePrintRequest
        from backend.app.schemas.print_queue import (
            PrintQueueItemCreate,
            PrintQueueItemResponse,
            PrintQueueItemUpdate,
        )

        for schema in (
            PrintQueueItemCreate,
            PrintQueueItemUpdate,
            PrintQueueItemResponse,
            ReprintRequest,
            FilePrintRequest,
        ):
            assert "nozzle_rack_choice" in schema.model_fields, schema.__name__

    def test_a_direct_print_hands_the_pick_to_the_dispatcher(self):
        """The routes pass ``body.model_dump(exclude_none=True)`` as the job's
        options; the pick must be in it under the name the dispatcher reads."""
        from backend.app.schemas.archive import ReprintRequest
        from backend.app.schemas.library import FilePrintRequest

        for schema in (ReprintRequest, FilePrintRequest):
            options = schema(nozzle_rack_choice={"2": 1, "1": 3}).model_dump(exclude_none=True)
            assert options["nozzle_rack_choice"] == {2: 1, 1: 3}, schema.__name__

    def test_the_create_schema_actually_keeps_a_posted_pick(self):
        from backend.app.schemas.print_queue import PrintQueueItemCreate

        parsed = PrintQueueItemCreate(queue_id=1, archive_id=1, nozzle_rack_choice={"2": 1, "1": 3})
        assert parsed.nozzle_rack_choice == {2: 1, 1: 3}


@pytest.mark.asyncio
class TestDirectPrint:
    """A direct print dispatches without a queue row of its own choosing, so the
    pick must reach the job options — and every queued copy of a quantity."""

    async def test_a_reprint_hands_the_pick_to_the_dispatcher(
        self, async_client: AsyncClient, archive_factory, printer_factory, tmp_path
    ):
        from unittest.mock import AsyncMock, patch

        printer = await printer_factory()
        archive = await archive_factory(printer.id, file_path="archives/rack/benchy.gcode.3mf")
        on_disk = tmp_path / archive.file_path
        on_disk.parent.mkdir(parents=True, exist_ok=True)
        on_disk.write_bytes(b"3mf-data")

        with (
            patch("backend.app.api.routes.archives.settings.base_dir", tmp_path),
            patch("backend.app.services.printer_manager.printer_manager.is_connected", return_value=True),
            patch(
                "backend.app.services.background_dispatch.background_dispatch.dispatch_reprint_archive",
                new=AsyncMock(return_value={"dispatch_job_id": 1, "dispatch_position": 1}),
            ) as dispatch,
        ):
            response = await async_client.post(
                f"/api/v1/archives/{archive.id}/reprint?printer_id={printer.id}",
                json={"nozzle_rack_choice": {"2": 1, "1": 3}},
            )

        assert response.status_code == 200, response.text
        assert dispatch.await_args.kwargs["options"]["nozzle_rack_choice"] == {2: 1, 1: 3}

    async def test_a_library_print_hands_the_pick_to_the_dispatcher(
        self, async_client: AsyncClient, printer_factory, db_session, tmp_path
    ):
        from unittest.mock import AsyncMock, patch

        from backend.app.models.library import LibraryFile

        printer = await printer_factory()
        lib = LibraryFile(
            filename="benchy.gcode.3mf",
            file_path="library/files/benchy.gcode.3mf",
            file_type="gcode",
            file_size=1024,
        )
        db_session.add(lib)
        await db_session.commit()
        on_disk = tmp_path / lib.file_path
        on_disk.parent.mkdir(parents=True, exist_ok=True)
        on_disk.write_bytes(b"library data")

        with (
            patch("backend.app.api.routes.library.app_settings.base_dir", tmp_path),
            patch("backend.app.services.printer_manager.printer_manager.is_connected", return_value=True),
            patch(
                "backend.app.services.background_dispatch.background_dispatch.dispatch_print_library_file",
                new=AsyncMock(return_value={"dispatch_job_id": 2, "dispatch_position": 1}),
            ) as dispatch,
        ):
            response = await async_client.post(
                f"/api/v1/library/files/{lib.id}/print?printer_id={printer.id}",
                json={"nozzle_rack_choice": {"2": 1}},
            )

        assert response.status_code == 200, response.text
        assert dispatch.await_args.kwargs["options"]["nozzle_rack_choice"] == {2: 1}

    async def test_every_queued_copy_of_a_quantity_carries_the_pick(self, db_session, queue, archive):
        from backend.app.services.queue_batch import enqueue_batch_copies

        items, _batch = await enqueue_batch_copies(
            db_session,
            printer_id=queue.printer_id,
            count=3,
            archive_id=archive.id,
            plate_id=1,
            nozzle_rack_choice={2: 1, 1: 3},
        )

        item_ids = [item.id for item in items]
        assert len(item_ids) == 3
        for item_id in item_ids:
            assert await _stored_choice(db_session, item_id) is not None
