"""C02/C07/C08 and actual doors from the first independent C execution review."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.auto_queue import AutoQueueItem
from backend.app.models.library import LibraryFile
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.queue_source import QueueSource
from backend.app.models.user import User
from backend.app.services import queue_sources
from backend.app.services.archive import ArchiveService
from backend.app.services.library_delete_scope import library_delete_scope
from backend.app.services.library_identity import LibraryIdentity
from backend.app.services.library_trash import library_trash_service
from backend.tests.integration.test_content_delete_protocol import independent_factory, source  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def captured_job(db, printer, *, status, archive=None):
    owner = User(username="finished-content-owner")
    lane = PrinterQueue(printer_id=printer.id, status="idle")
    blob = QueueSource(sha256="e" * 64, size_bytes=1, relative_path="queue/test.gcode", format="gcode", state="ready")
    db.add_all([owner, lane, blob])
    await db.flush()
    item = PrintQueueItem(
        queue_id=lane.id,
        status=status,
        created_by_id=owner.id,
        queue_source_id=blob.id,
        archive_id=archive.id if archive else None,
    )
    db.add(item)
    await db.commit()
    return owner, item, blob


@pytest.mark.parametrize("shape", ["cancelled", "failed", "completed_doomed", "completed_surviving"])
async def test_finished_captured_work_requires_bytes_only_for_its_surviving_archive(
    async_client, db_session, printer_factory, shape
):
    printer = await printer_factory()
    archive = None
    if shape.startswith("completed"):
        archive = PrintArchive(filename="finished.3mf", file_path="", file_size=0, status="completed")
        db_session.add(archive)
        await db_session.commit()
    owner, item, blob = await captured_job(
        db_session, printer, status="completed" if archive else shape, archive=archive
    )
    if shape == "completed_doomed":
        archive.created_by_id = owner.id
        await db_session.commit()
    owner_id, item_id, archive_id, blob_id = owner.id, item.id, archive.id if archive else None, blob.id
    response = await async_client.delete(f"/api/v1/users/{owner_id}?delete_items=true")
    db_session.expire_all()
    if shape == "completed_surviving":
        assert response.status_code == 409, response.text
        assert "archive_copy_missing" in response.json()["detail"]["reasons"]
        assert await db_session.get(User, owner_id) is not None
        assert await db_session.get(PrintQueueItem, item_id) is not None
    else:
        assert response.status_code == 200, response.text
        assert response.json()["counts"]["queue_items"] == 1
        assert await db_session.get(User, owner_id) is None
        assert await db_session.get(PrintQueueItem, item_id) is None
        if archive_id is not None:
            assert await db_session.get(PrintArchive, archive_id) is None
    assert await db_session.get(QueueSource, blob_id) is not None


async def test_queue_plate_gate_is_kept_when_its_archive_is_not_in_the_user_batch(
    async_client, db_session, printer_factory
):
    printer = await printer_factory()
    path = Path(settings.archive_dir) / "surviving" / "independent.3mf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"surviving archive bytes")
    archive = PrintArchive(filename=path.name, file_path=str(path), file_size=path.stat().st_size, status="completed")
    db_session.add(archive)
    await db_session.commit()
    owner, item, _blob = await captured_job(db_session, printer, status="completed", archive=archive)
    printer.awaiting_plate_clear_archive_id = archive.id
    await db_session.commit()
    owner_id, item_id, archive_id, printer_id = owner.id, item.id, archive.id, printer.id
    response = await async_client.delete(f"/api/v1/users/{owner_id}?delete_items=true")
    assert response.status_code == 409, response.text
    assert "plate_gate" in response.json()["detail"]["reasons"]
    db_session.expire_all()
    assert await db_session.get(User, owner_id) is not None
    assert await db_session.get(PrintQueueItem, item_id) is not None
    assert await db_session.get(PrintArchive, archive_id) is not None
    from backend.app.models.printer import Printer

    assert (await db_session.get(Printer, printer_id)).awaiting_plate_clear_archive_id == archive_id


@pytest.mark.parametrize("keep", ["plate_gate", "current_item"])
@pytest.mark.parametrize("endpoint", ["single", "all"])
async def test_trash_delete_reports_the_keep_instead_of_a_missing_archive(
    async_client, db_session, printer_factory, keep, endpoint
):
    printer = await printer_factory()
    archive = PrintArchive(
        filename="retained.3mf",
        file_path="",
        file_size=0,
        status="completed",
        deleted_at=datetime.now() - timedelta(days=30),
        printer_id=printer.id,
    )
    db_session.add(archive)
    await db_session.flush()
    lane = PrinterQueue(printer_id=printer.id, status="idle")
    db_session.add(lane)
    await db_session.flush()
    item = PrintQueueItem(queue_id=lane.id, archive_id=archive.id, status="completed")
    db_session.add(item)
    await db_session.flush()
    if keep == "plate_gate":
        printer.awaiting_plate_clear_archive_id = archive.id
    else:
        lane.current_item_id = item.id
    await db_session.commit()
    archive_id, item_id, lane_id = archive.id, item.id, lane.id
    route = f"/api/v1/archives/trash/{archive_id}" if endpoint == "single" else "/api/v1/archives/trash"
    response = await async_client.delete(route)
    reason = "live_job" if keep == "current_item" else keep
    if endpoint == "single":
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "archive_retained"
        assert reason in response.json()["detail"]["reasons"]
    else:
        assert response.status_code == 200, response.text
        assert response.json() == {"deleted": 0, "retained": [{"id": archive_id, "reasons": [reason]}]}
    db_session.expire_all()
    assert await db_session.get(PrintArchive, archive_id) is not None
    assert await db_session.get(PrintQueueItem, item_id) is not None
    if keep == "current_item":
        assert (await db_session.get(PrinterQueue, lane_id)).current_item_id == item_id


async def test_an_old_sqlite_reader_ends_before_the_new_plan_observes_a_foreign_commit(
    independent_factory, monkeypatch
):
    factory = independent_factory
    if factory.kw["bind"].dialect.name != "sqlite":
        pytest.skip("SQLite WAL reader/writer boundary")
    proof, _archive_id, _identity = await source(factory)
    original = queue_sources.storage_mutation

    @asynccontextmanager
    async def foreign_publication():
        async with original():
            async with factory() as other:
                await other.execute(
                    update(LibraryFile).where(LibraryFile.id == proof.id).values(file_path="changed.3mf")
                )
                await other.commit()
            yield

    monkeypatch.setattr(queue_sources, "storage_mutation", foreign_publication)
    async with factory() as db:
        await db.execute(text("BEGIN"))
        await db.get(LibraryFile, proof.id)  # a genuine old WAL snapshot, not just SQLAlchemy autobegin
        with pytest.raises(HTTPException) as refusal:
            async with library_delete_scope(db, [proof]):
                pytest.fail("the plan reused its preflight reader snapshot")
        assert refusal.value.status_code == 409
        await db.rollback()
    async with factory() as db:
        assert (await db.get(LibraryFile, proof.id)).file_path == "changed.3mf"


@pytest.mark.parametrize("retained_first", [True, False], ids=["retained-lower-id", "retained-higher-id"])
@pytest.mark.parametrize("keep", ["plate_gate", "current_item"])
async def test_empty_trash_reports_both_deleted_and_retained_in_either_id_order(
    async_client, db_session, printer_factory, retained_first, keep
):
    printer = await printer_factory()
    rows = [
        PrintArchive(
            filename=name,
            file_path="",
            file_size=0,
            status="completed",
            printer_id=printer.id,
            deleted_at=datetime.now() - timedelta(days=30),
        )
        for name in ("first.3mf", "second.3mf")
    ]
    db_session.add_all(rows)
    await db_session.flush()
    held, free = rows if retained_first else rows[::-1]
    lane = PrinterQueue(printer_id=printer.id, status="idle")
    db_session.add(lane)
    await db_session.flush()
    job = PrintQueueItem(queue_id=lane.id, archive_id=held.id, status="completed")
    db_session.add(job)
    await db_session.flush()
    if keep == "plate_gate":
        printer.awaiting_plate_clear_archive_id = held.id
    else:
        lane.current_item_id = job.id
    await db_session.commit()
    held_id, free_id, job_id, lane_id = held.id, free.id, job.id, lane.id
    response = await async_client.delete("/api/v1/archives/trash")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "deleted": 1,
        "retained": [{"id": held_id, "reasons": ["plate_gate" if keep == "plate_gate" else "live_job"]}],
    }
    db_session.expire_all()
    assert await db_session.get(PrintArchive, held_id) is not None
    assert await db_session.get(PrintArchive, free_id) is None
    assert await db_session.get(PrintQueueItem, job_id) is not None
    if keep == "current_item":
        assert (await db_session.get(PrinterQueue, lane_id)).current_item_id == job_id


@pytest.mark.parametrize("status,detail", [(409, {"error": "other_failure"}), (503, "temporarily unavailable")])
async def test_empty_trash_does_not_mask_an_unrelated_failure(async_client, db_session, monkeypatch, status, detail):
    from backend.app.services.archive_purge import ArchivePurgeService

    db_session.add(
        PrintArchive(filename="other.3mf", file_path="", file_size=0, status="completed", deleted_at=datetime.now())
    )
    await db_session.commit()

    async def fail(_archive_id):
        raise HTTPException(status, detail)

    monkeypatch.setattr(ArchivePurgeService, "hard_delete_now", staticmethod(fail))
    response = await async_client.delete("/api/v1/archives/trash")
    assert response.status_code == status, response.text
    assert response.json()["detail"] == detail


async def test_archive_cleanup_rechecks_a_reused_id_before_removing_its_owned_folders(independent_factory, monkeypatch):
    from backend.app.utils.archive_paths import fallback_dir_for

    factory = independent_factory
    async with factory() as db:
        archive = PrintArchive(filename="A.3mf", file_path="", file_size=0, status="completed")
        db.add(archive)
        await db.commit()
        archive_id = archive.id
        service = ArchiveService(db)
        original = service.cleanup_archive_removal
        owned = fallback_dir_for(archive_id) / "B-owned.txt"

        async def publish_replacement(removal, **kwargs):
            async with factory() as writer:
                replacement = PrintArchive(
                    id=archive_id, filename="B.3mf", file_path="", file_size=0, status="completed"
                )
                writer.add(replacement)
                await writer.commit()
            owned.parent.mkdir(parents=True, exist_ok=True)
            owned.write_bytes(b"new archive B")
            await original(removal, **kwargs)

        monkeypatch.setattr(service, "cleanup_archive_removal", publish_replacement)
        assert await service.delete_archive(archive_id)
    async with factory() as db:
        assert (await db.get(PrintArchive, archive_id)).filename == "B.3mf"
    assert owned.is_file(), "old cleanup removed the reused archive's owned directory"
    assert owned.read_bytes() == b"new archive B"


async def test_a_retained_library_file_cannot_starve_its_expired_neighbours(db_session, printer_factory):
    printer = await printer_factory()
    lane = PrinterQueue(printer_id=printer.id, status="idle")
    db_session.add(lane)
    await db_session.commit()

    async def expired(name):
        path = Path(settings.library_dir) / "files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"owned")
        row = LibraryFile(
            filename=name,
            file_path=str(path),
            file_type="gcode",
            file_size=5,
            deleted_at=datetime.now() - timedelta(days=400),
        )
        db_session.add(row)
        await db_session.commit()
        return row.id, path

    blocked_id, blocked_path = await expired("blocked.gcode")
    free_id, free_path = await expired("free.gcode")
    db_session.add(PrintQueueItem(queue_id=lane.id, library_file_id=blocked_id, status="printing"))
    await db_session.commit()
    assert await library_trash_service._sweep(db_session) == 1
    assert await db_session.get(LibraryFile, blocked_id, populate_existing=True) is not None
    assert await db_session.get(LibraryFile, free_id, populate_existing=True) is None
    assert blocked_path.is_file() and not free_path.exists()
    next_id, next_path = await expired("next.gcode")
    assert await library_trash_service._sweep(db_session) == 1
    assert await db_session.get(LibraryFile, next_id, populate_existing=True) is None
    assert blocked_path.is_file() and not next_path.exists()


async def test_queue_repair_detaches_verified_companions_and_calibration_history(db_session, printer_factory):
    from backend.app.models.calibration_session import CalibrationSession
    from backend.app.services.printer_queues import _detach_queue_children

    printer = await printer_factory()
    lane = PrinterQueue(printer_id=printer.id)
    proven, unproven = AutoQueueItem(status="assigned"), AutoQueueItem(status="assigned")
    db_session.add_all([lane, proven, unproven])
    await db_session.flush()
    job = PrintQueueItem(
        queue_id=lane.id,
        status="completed",
        source_auto_item_id=proven.id,
        source_auto_identity=proven.recovery_identity,
    )
    db_session.add(job)
    await db_session.flush()
    proven.assigned_to_item_id = unproven.assigned_to_item_id = job.id
    history = CalibrationSession(
        printer_id=printer.id,
        print_queue_item_id=job.id,
        cali_mode="flow_rate",
        method="manual",
        nozzle_diameter=0.4,
        nozzle_volume_type="standard",
        filaments_json="[]",
        status="completed",
    )
    db_session.add(history)
    await db_session.commit()
    job_id, proven_id, unproven_id, history_id = job.id, proven.id, unproven.id, history.id
    assert await _detach_queue_children(db_session, lane.id) == 1
    await db_session.commit()
    db_session.expire_all()
    assert await db_session.get(PrintQueueItem, job_id) is None
    assert await db_session.get(AutoQueueItem, proven_id) is None
    assert (await db_session.get(AutoQueueItem, unproven_id)).assigned_to_item_id is None
    assert (await db_session.get(CalibrationSession, history_id)).print_queue_item_id is None


async def test_facet_only_links_use_the_single_writer_and_follow_the_root_transaction(db_session):
    from backend.app.models.product import Product, ProductFacet
    from backend.app.services import product_facets

    product = Product(name="facet-only owner")
    file = LibraryFile(filename="old.gcode", file_path="", file_type="gcode", file_size=0)
    db_session.add_all([product, file])
    await db_session.flush()
    facet = ProductFacet(product_id=product.id, library_file_id=file.id, kind="material", value="PLA")
    db_session.add(facet)
    await db_session.commit()
    file_id, product_id = file.id, product.id
    facet_id = (product_id, file_id, "material", "PLA")
    await product_facets.delete_for_files(db_session, [file_id])
    await db_session.rollback()
    assert await db_session.get(ProductFacet, facet_id, populate_existing=True) is not None
    current = await db_session.get(LibraryFile, file_id, populate_existing=True)
    await library_trash_service.hard_delete_now(db_session, current)
    db_session.expire_all()
    assert await db_session.get(ProductFacet, facet_id) is None
    assert await db_session.get(Product, product_id) is not None
