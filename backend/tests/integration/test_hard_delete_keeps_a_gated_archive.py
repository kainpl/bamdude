"""An archive a printer's plate gate names is kept by the hard delete (G03 B, A19 / A26 H6-H7; acceptance TA41 (a), (c)).

(a) The ordinary hard delete of X while printer P's gate names X: ``archive_keep_reasons`` answers ``plate_gate``, and
the delete returns before the file-path reference lock -- it is never entered -- writing nothing.
(c) A gate armed on X between the delete's footprint read and its guards: the footprint read again under them has
grown (a gate printer not planned) -- 409 ``queue_selection_changed``, nothing written.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.models.archive import PrintArchive
from backend.app.models.printer import Printer
from backend.tests.integration.queue_test_database import test_engine  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def hard_delete(factory, archive_id, **kwargs):
    from backend.app.services.archive import ArchiveService

    async with factory() as db:
        return await ArchiveService(db).delete_archive(archive_id, **kwargs)


def path_lock_spy(monkeypatch) -> list[str]:
    from backend.app.services import archive_write_scope

    entered: list[str] = []
    real = archive_write_scope.archive_file_reference_scope

    @asynccontextmanager
    async def spy(db, path, *args, **kwargs):
        entered.append(path)
        async with real(db, path, *args, **kwargs) as value:
            yield value

    monkeypatch.setattr(archive_write_scope, "archive_file_reference_scope", spy)
    return entered


async def arm_the_gate(factory, printer_id, archive_id):
    async with factory() as db:
        await db.execute(
            update(Printer).where(Printer.id == printer_id).values(awaiting_plate_clear_archive_id=archive_id)
        )
        await db.commit()


@pytest.mark.parametrize("refuse_kept", [False, True], ids=["sweeper", "hard_delete_now"])
async def test_the_hard_delete_of_a_gated_archive_is_refused_before_the_path_lock(
    refuse_kept, test_engine, db_session, printer_factory, archive_factory, monkeypatch
):
    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="completed")
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    await arm_the_gate(factory, printer.id, archive.id)
    entered = path_lock_spy(monkeypatch)
    if refuse_kept:
        with pytest.raises(HTTPException) as refused:
            await hard_delete(factory, archive.id, refuse_kept=True)
        assert refused.value.status_code == 409
        assert "plate_gate" in refused.value.detail["reasons"]
    else:
        assert await hard_delete(factory, archive.id) is False
    assert entered == [], "the file-path reference lock is never entered for a kept archive"
    async with factory() as db:
        assert await db.get(PrintArchive, archive.id) is not None


async def test_a_gate_armed_between_the_footprint_and_the_guards_refuses_the_delete(
    test_engine, db_session, printer_factory, archive_factory, monkeypatch
):
    from backend.app.services import archive_delete_scope

    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="completed")
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    real = archive_delete_scope.footprint
    calls: list[int] = []

    async def footprint(db, archive_id, **kwargs):
        result = await real(db, archive_id, **kwargs)
        if not calls:
            calls.append(archive_id)
            await arm_the_gate(factory, printer.id, archive_id)  # after the preflight read, before the guards
        return result

    monkeypatch.setattr(archive_delete_scope, "footprint", footprint)
    entered = path_lock_spy(monkeypatch)
    with pytest.raises(HTTPException) as refused:
        await hard_delete(factory, archive.id)
    assert refused.value.status_code == 409
    assert refused.value.detail["code"] == "queue_selection_changed"
    assert entered == []
    async with factory() as db:
        assert await db.get(PrintArchive, archive.id) is not None
