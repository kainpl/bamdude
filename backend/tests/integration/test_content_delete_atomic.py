"""C01/C02/C03/C05/C06: real HTTP doors and actual SQLite mutations."""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from backend.app.core.config import settings
from backend.app.models.archive import PrintArchive
from backend.app.models.archive_part import PrintArchivePart
from backend.app.models.library import LibraryFile
from backend.app.models.user import User

pytestmark = pytest.mark.integration


async def account(db):
    row = User(username="content-owner")
    db.add(row)
    await db.commit()
    return row


async def file_of(db, owner):
    path = Path(settings.library_dir) / "files" / "owned.gcode.3mf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"owned bytes")
    row = LibraryFile(
        filename=path.name,
        file_path=str(path.relative_to(settings.base_dir)),
        file_type="gcode",
        file_size=11,
        created_by_id=owner.id,
    )
    db.add(row)
    await db.commit()
    return row, path


async def archive_of(db, owner=None, **values):
    row = PrintArchive(
        filename="part.gcode.3mf",
        file_path="",
        file_size=0,
        status="completed",
        created_by_id=owner.id if owner else None,
        **values,
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.parametrize("delete_items", [True, False])
async def test_user_http_cleans_children_and_preserves_foreign_history(async_client, db_session, delete_items):
    owner = await account(db_session)
    file, path = await file_of(db_session, owner)
    foreign = await archive_of(db_session, library_file_id=file.id)
    owned = await archive_of(db_session, owner, library_file_id=file.id)
    old_archive_id = owned.id
    old_file_id = file.id
    user_id, foreign_id = owner.id, foreign.id
    from backend.app.models.spool import Spool
    from backend.app.models.spool_usage_history import SpoolUsageHistory
    from backend.app.models.spoolman_usage_history import SpoolmanUsageHistory

    spool = Spool(material="PLA", weight_used=123.0)
    db_session.add(spool)
    await db_session.flush()
    internal = SpoolUsageHistory(
        spool_id=spool.id, archive_id=owned.id, print_name="original A", weight_used=12.5, cost=4.0
    )
    external = SpoolmanUsageHistory(
        source_token="c" * 32,
        spoolman_spool_id=55,
        archive_id=owned.id,
        print_name="original A",
        weight_used=12.5,
        cost=4.0,
        created_at=datetime(2026, 1, 1),
    )
    db_session.add_all([internal, external])
    db_session.add(PrintArchivePart(archive_id=owned.id, name="old", name_key="old", quantity=1, defective=1))
    await db_session.commit()
    internal_id, external_id, spool_id = internal.id, external.id, spool.id

    response = await async_client.delete(f"/api/v1/users/{user_id}?delete_items={'true' if delete_items else 'false'}")
    assert response.status_code == (200 if delete_items else 204), response.text
    db_session.expire_all()
    assert await db_session.get(User, user_id) is None
    if delete_items:
        assert response.json()["deleted"] is True
        assert response.json()["counts"]["archives"] == 1
        assert await db_session.get(PrintArchive, old_archive_id) is None
        assert (
            await db_session.scalar(
                select(func.count()).select_from(PrintArchivePart).where(PrintArchivePart.archive_id == old_archive_id)
            )
            == 0
        )
        assert await db_session.get(LibraryFile, old_file_id) is None
        survivor = await db_session.get(PrintArchive, foreign_id)
        assert survivor.library_file_id is None
        assert not path.exists()
        new = await archive_of(
            db_session, **({"id": old_archive_id} if db_session.get_bind().dialect.name == "postgresql" else {})
        )
        assert new.id == old_archive_id
        assert (
            await db_session.scalar(
                select(func.count()).select_from(PrintArchivePart).where(PrintArchivePart.archive_id == new.id)
            )
            == 0
        )
    else:
        assert path.read_bytes() == b"owned bytes"
        assert (await db_session.get(LibraryFile, old_file_id)).created_by_id is None
        assert (await db_session.get(PrintArchive, old_archive_id)).created_by_id is None
    for model, row_id in ((SpoolUsageHistory, internal_id), (SpoolmanUsageHistory, external_id)):
        row = await db_session.get(model, row_id, populate_existing=True)
        assert (row.archive_id, row.weight_used, row.cost, row.print_name) == (
            None if delete_items else old_archive_id,
            12.5,
            4.0,
            "original A",
        )
    assert (await db_session.get(Spool, spool_id, populate_existing=True)).weight_used == 123.0


@pytest.mark.parametrize("keep", ["printing", "plate_gate", "swap_owed"])
async def test_user_http_refuses_physical_keep_without_account_or_row_mutation(
    async_client, db_session, printer_factory, keep
):
    from backend.app.services import swap_terminal

    owner = await account(db_session)
    printer = await printer_factory()
    archive = await archive_of(db_session, owner, printer_id=printer.id)
    if keep == "printing":
        archive.status = "printing"
    elif keep == "plate_gate":
        printer.awaiting_plate_clear_archive_id = archive.id
    else:
        archive.extra_data = swap_terminal.mark_owed({})
    await db_session.commit()
    user_id, archive_id, generation = owner.id, archive.id, owner.session_generation
    response = await async_client.delete(f"/api/v1/users/{user_id}?delete_items=true")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "user_content_delete_active"
    db_session.expire_all()
    assert (await db_session.get(User, user_id)).session_generation == generation
    assert await db_session.get(PrintArchive, archive_id) is not None


async def test_too_large_refuses_before_account_writer(async_client, db_session):
    from backend.app.services.user_content_delete import MAX_PARENTS

    owner = await account(db_session)
    db_session.add_all(
        [
            PrintArchive(filename=f"{i}.3mf", file_path="", file_size=0, status="completed", created_by_id=owner.id)
            for i in range(MAX_PARENTS + 1)
        ]
    )
    await db_session.commit()
    user_id, generation = owner.id, owner.session_generation
    response = await async_client.delete(f"/api/v1/users/{user_id}?delete_items=true")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "user_content_delete_too_large"
    db_session.expire_all()
    assert (await db_session.get(User, user_id)).session_generation == generation
    assert (
        await db_session.scalar(
            select(func.count()).select_from(PrintArchive).where(PrintArchive.created_by_id == user_id)
        )
        == MAX_PARENTS + 1
    )


@pytest.mark.parametrize(
    "phase", ["source_release", "usage_detach", "stock_detach", "product_detach", "photos", "account_detach"]
)
async def test_failure_after_content_flush_rolls_back_everything_and_unlinks_nothing(
    async_client, db_session, monkeypatch, phase
):
    from backend.app.services import content_delete_rows, user_content_delete

    owner = await account(db_session)
    file, path = await file_of(db_session, owner)
    archive = await archive_of(db_session, owner, library_file_id=file.id)
    db_session.add(PrintArchivePart(archive_id=archive.id, name="kept", name_key="kept"))
    await db_session.commit()
    user_id, file_id, archive_id = owner.id, file.id, archive.id

    module, name = {
        "source_release": (content_delete_rows.queue_source_release, "source_purged"),
        "usage_detach": (content_delete_rows.spool_usage_service, "detach_archives"),
        "stock_detach": (content_delete_rows.part_stock, "detach_archive"),
        "product_detach": (content_delete_rows, "purge_file_product_links"),
        "photos": (content_delete_rows.library_file_photos, "forget_files"),
        "account_detach": (user_content_delete, "detach_account_rows"),
    }[phase]
    original = getattr(module, name)

    async def refused(*args, **kwargs):
        await original(*args, **kwargs)
        raise HTTPException(409, "injected after " + phase)

    monkeypatch.setattr(module, name, refused)
    response = await async_client.delete(f"/api/v1/users/{user_id}?delete_items=true")
    assert response.status_code == 409, response.text
    db_session.expire_all()
    assert await db_session.get(User, user_id) is not None
    assert await db_session.get(LibraryFile, file_id) is not None
    assert await db_session.get(PrintArchive, archive_id) is not None
    assert (
        await db_session.scalar(
            select(func.count()).select_from(PrintArchivePart).where(PrintArchivePart.archive_id == archive_id)
        )
        == 1
    )
    assert path.read_bytes() == b"owned bytes"


@pytest.mark.parametrize("keep", ["current_item", "live_queue", "live_auto_execution"])
async def test_user_http_keeps_physical_queue_owners(async_client, db_session, keep):
    from backend.app.models.auto_queue import AutoQueueItem
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue

    owner = await account(db_session)
    printer = Printer(name="hold", serial_number="c-physical-" + keep, ip_address="127.0.0.1", access_code="00000000")
    db_session.add(printer)
    await db_session.flush()
    lane = PrinterQueue(printer_id=printer.id, status="idle")
    db_session.add(lane)
    await db_session.flush()
    if keep == "live_auto_execution":
        auto = AutoQueueItem(status="failed", created_by_id=owner.id)
        db_session.add(auto)
        await db_session.flush()
        item = PrintQueueItem(
            queue_id=lane.id,
            position=1,
            status="printing",
            source_auto_item_id=auto.id,
            source_auto_identity=auto.recovery_identity,
        )
    else:
        item = PrintQueueItem(
            queue_id=lane.id,
            position=1,
            status="printing" if keep == "live_queue" else "completed",
            created_by_id=owner.id,
        )
    db_session.add(item)
    await db_session.flush()
    if keep == "current_item":
        lane.current_item_id = item.id
    await db_session.commit()
    user_id, item_id, lane_id, current = owner.id, item.id, lane.id, lane.current_item_id
    response = await async_client.delete(f"/api/v1/users/{user_id}?delete_items=true")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "user_content_delete_active"
    db_session.expire_all()
    assert await db_session.get(User, user_id) is not None
    assert await db_session.get(PrintQueueItem, item_id) is not None
    assert (await db_session.get(PrinterQueue, lane_id)).current_item_id == current
