"""The manual credit and the 3MF attach of the same print meet on the archive, both orders (G03 B, A29; acceptance
TA36 (b)).

Both hold the archive before any part (A29.1): ``count-into-stock`` enters the archive's write scope and reads its two
409 conditions under it; the attach takes its row locks -- the credit of an order-less print included -- before its
file-path lock (A29.2). Whichever holds the archive, the other waits on it -- on PostgreSQL on the archive's database
guard (the in-process archive lock is stood aside so the database is what is waited on) -- and both finish.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.services import archive_write_scope, part_stock
from backend.tests.integration.lane_pairs import NoProcessLock, a_waiter_appears, held_after
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401
from backend.tests.unit.services.test_attach_3mf_field_parity import _multi_plate_3mf

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ANY_WAIT = ("transactionid", "tuple", "advisory")


async def a_print_without_its_file(db_session, printer_factory, tmp_path, monkeypatch) -> tuple[int, object]:
    from backend.app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive")
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="Plate.gcode.3mf",
        file_path="",
        file_size=0,
        print_name="Plate",
        status="completed",
    )
    db_session.add(archive)
    await db_session.commit()
    return archive.id, _multi_plate_3mf(tmp_path / "src.gcode.3mf")


def the_database_guard_is_what_waits(test_engine, monkeypatch, archive_id):
    if test_engine.dialect.name == "postgresql":
        monkeypatch.setitem(archive_write_scope._archive_locks, archive_id, NoProcessLock())


async def attach(factory, archive_id, source):
    from backend.app.services.archive import ArchiveService

    async with factory() as db:
        return await ArchiveService(db).attach_3mf_to_archive(archive_id, source, "Plate.gcode.3mf")


async def test_the_attach_meeting_a_manual_credit_waits_on_the_archive(
    test_engine, db_session, printer_factory, async_client, tmp_path, monkeypatch
):
    archive_id, source = await a_print_without_its_file(db_session, printer_factory, tmp_path, monkeypatch)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    the_database_guard_is_what_waits(test_engine, monkeypatch, archive_id)
    # The manual credit stopped under its archive lock, before its first part.
    reached, release = held_after(monkeypatch, archive_write_scope, "load_active_archive_for_write")
    crediting = asyncio.create_task(async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock"))
    attaching = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        attaching = asyncio.create_task(attach(factory, archive_id, source))
        await a_waiter_appears(test_engine, factory, attaching, on=ANY_WAIT, archive_id=archive_id)
        release.set()
        response = await asyncio.wait_for(crediting, 20)
        assert await asyncio.wait_for(attaching, 20)
    finally:
        release.set()
        await asyncio.gather(crediting, *([attaching] if attaching else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    async with factory() as db:
        assert (await db.get(PrintArchive, archive_id)).file_path, "the attach committed its file"


async def test_a_manual_credit_meeting_the_attach_waits_on_the_archive(
    test_engine, db_session, printer_factory, async_client, tmp_path, monkeypatch
):
    archive_id, source = await a_print_without_its_file(db_session, printer_factory, tmp_path, monkeypatch)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    the_database_guard_is_what_waits(test_engine, monkeypatch, archive_id)
    # The attach stopped after its credit, before its file-path lock.
    reached, release = held_after(monkeypatch, part_stock, "credit_if_unfiled")
    attaching = asyncio.create_task(attach(factory, archive_id, source))
    crediting = None
    try:
        await asyncio.wait_for(reached.wait(), 10)
        crediting = asyncio.create_task(async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock"))
        await a_waiter_appears(test_engine, factory, crediting, on=ANY_WAIT, archive_id=archive_id)
        release.set()
        assert await asyncio.wait_for(attaching, 20)
        response = await asyncio.wait_for(crediting, 20)
    finally:
        release.set()
        await asyncio.gather(attaching, *([crediting] if crediting else []), return_exceptions=True)
    assert response.status_code == 200, response.text
    async with factory() as db:
        assert (await db.get(PrintArchive, archive_id)).file_path
