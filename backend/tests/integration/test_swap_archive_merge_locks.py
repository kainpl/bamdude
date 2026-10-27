"""Two writers of one print's metadata at once (swap-source-lifecycle spec r4 §3/§7; final review).

The download retry's attach finishes a print's swap declaration under the print's write scope while the energy
write, the sweep's close and its structural close of an accepted finish each merge their own keys into the same
JSON. Each test stops the writer right after its locked read and starts the attach beside it: the attach must not
land inside that window -- it waits, and lands after the writer's commit -- and both answers survive. A file
database on SQLite, one connection per session, so the database's own write lock takes part as in production.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app import main
from backend.app.core.database import Base, configure_sqlite_connection
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import archive_write_scope, swap_compatibility as sc

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

#: How long the writer keeps its window open with the attach running beside it.
WINDOW = 0.3


@pytest.fixture
async def factory(test_engine, tmp_path, monkeypatch):
    archive_write_scope._archive_locks.clear()
    archive_write_scope._archive_file_locks.clear()
    if test_engine.dialect.name == "sqlite":
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'locks.db'}")
        event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    else:
        engine = test_engine
    made = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", made)
    monkeypatch.setattr("backend.app.core.database.async_session", made)
    yield made
    archive_write_scope._archive_locks.clear()
    archive_write_scope._archive_file_locks.clear()
    if engine is not test_engine:
        await engine.dispose()


async def a_printer(factory) -> int:
    async with factory() as db:
        printer = Printer(
            name="Locks",
            serial_number="00M09L000000001",
            ip_address="192.0.2.91",
            access_code="12345678",
            is_active=True,
            model="P1S",
        )
        db.add(printer)
        await db.flush()
        db.add(PrinterQueue(id=printer.id, printer_id=printer.id, status="printing"))
        await db.commit()
        return printer.id


async def a_pending_print(factory, printer_id: int, **columns) -> int:
    """A print whose 3MF has not landed yet: its declaration waits for the attach."""
    async with factory() as db:
        archive = PrintArchive(
            printer_id=printer_id,
            filename="lamp.gcode.3mf",
            file_path="",
            file_size=0,
            print_name="lamp",
            status=columns.pop("status", "completed"),
            started_at=datetime.now(timezone.utc),
            extra_data={"original_subtask": "lamp"},
            **columns,
        )
        sc.write_archive_fact(archive, None, pending_external=True)
        db.add(archive)
        await db.commit()
        return archive.id


class Window:
    """Two windows the attach gets beside the writer, each ``WINDOW`` long: right after the writer's locked read,
    and right after the writer's first commit since that read. While the writer's transaction holds its read the
    attach must not land; the second window lets it land where the writer has let go -- after the write, or, were
    the writer to commit between its read and its write, before the write, which then erases the attach's answer
    from the copy it read."""

    def __init__(self, factory, archive_id: int, load):
        self.factory, self.archive_id, self.load = factory, archive_id, load
        self.writer = None
        self.attach: asyncio.Task | None = None
        self.landed_inside: bool | None = None
        self.committed_once = False

    async def attach_under_its_scope(self) -> None:
        """What the retry's attach does: its write scope, a fresh read, the declaration finished."""
        async with self.factory() as other, archive_write_scope.archive_write_scope(other, self.archive_id):
            row = await self.load(other, self.archive_id)
            sc.write_archive_fact(row, True)
            await other.commit()

    def around(self, read):
        async def wrapped(db, *args, **kwargs):
            found = await read(db, *args, **kwargs)
            if self.attach is None:
                self.writer = db
                self.attach = asyncio.ensure_future(self.attach_under_its_scope())
                done, _pending = await asyncio.wait({self.attach}, timeout=WINDOW)
                self.landed_inside = bool(done)
            return found

        return wrapped

    def install(self, monkeypatch) -> None:
        """Open the second window after the writer's first commit since its read."""
        real_commit = AsyncSession.commit
        window = self

        async def commit(session):
            await real_commit(session)
            if session is window.writer and not window.committed_once:
                window.committed_once = True
                await asyncio.wait({window.attach}, timeout=WINDOW)

        monkeypatch.setattr(AsyncSession, "commit", commit)

    async def settled(self) -> None:
        assert self.attach is not None, "the writer never took its locked read"
        await asyncio.wait_for(self.attach, timeout=30)
        assert self.landed_inside is False, "the attach wrote between the writer's read and its write"


async def a_row(factory, archive_id: int) -> PrintArchive:
    async with factory() as db:
        return await db.get(PrintArchive, archive_id)


async def test_the_energy_write_holds_its_window(factory, monkeypatch):
    printer_id = await a_printer(factory)
    archive_id = await a_pending_print(factory, printer_id, energy_start_kwh=10.0)
    window = Window(factory, archive_id, archive_write_scope.load_active_archive_for_write)
    window.install(monkeypatch)
    monkeypatch.setattr(archive_write_scope, "load_active_archive_for_write", window.around(window.load))
    # A plug from before m211: no measurement source -- but a real row, which the write checks it against.
    from backend.app.models.smart_plug import SmartPlug

    async with factory() as db:
        plug = SmartPlug(name="meter", plug_type="tasmota", ip_address="10.0.0.9", enabled=True)
        db.add(plug)
        await db.commit()
        plug_id = plug.id
    monkeypatch.setattr(
        main,
        "_energy_plug_for_printer",
        AsyncMock(return_value=SimpleNamespace(id=plug_id, measurement_source_id=None)),
    )
    monkeypatch.setattr(main, "_get_plug_energy", AsyncMock(return_value={"total": 11.5}))
    monkeypatch.setattr(main.smart_plug_manager, "record_energy_snapshot", AsyncMock())

    await main._record_print_energy(archive_id, printer_id, approximate=True)
    await window.settled()

    row = await a_row(factory, archive_id)
    assert row.energy_kwh == 1.5
    assert row.extra_data["energy_is_approximate"] is True
    assert sc.archive_fact(row) == sc.ArchiveFact(True, recorded=True)


async def test_the_sweeps_close_holds_its_window(factory, monkeypatch):
    from backend.app.services import print_reconciliation, usage_tracker
    from backend.app.services.printer_manager import printer_manager

    printer_id = await a_printer(factory)
    archive_id = await a_pending_print(factory, printer_id, status="printing")
    window = Window(factory, archive_id, archive_write_scope.load_active_archive_for_write)
    window.install(monkeypatch)
    monkeypatch.setattr(archive_write_scope, "locked_print", window.around(archive_write_scope.locked_print))
    monkeypatch.setattr(printer_manager, "arm_awaiting_plate_clear", AsyncMock())
    monkeypatch.setattr(usage_tracker, "get_persisted_print_name", AsyncMock(return_value=None))
    monkeypatch.setattr(usage_tracker, "on_print_complete", AsyncMock(return_value=[]))

    async with factory() as sweep:
        loaded = await sweep.get(PrintArchive, archive_id)
        await print_reconciliation._reconcile_complete_archive(sweep, loaded, status="completed", uncertain=False)
        await sweep.commit()
    await window.settled()

    row = await a_row(factory, archive_id)
    assert row.status == "completed"
    assert row.extra_data["recovered_by_startup_sweep"] is True
    assert sc.archive_fact(row) == sc.ArchiveFact(True, recorded=True)


async def test_the_structural_close_holds_its_window(factory, monkeypatch):
    from backend.app.services import print_reconciliation

    printer_id = await a_printer(factory)
    archive_id = await a_pending_print(factory, printer_id, status="printing")
    started = datetime.now(timezone.utc).replace(microsecond=0)
    async with factory() as db:
        item = PrintQueueItem(
            queue_id=printer_id, archive_id=archive_id, status="printing", position=1, started_at=started
        )
        db.add(item)
        await db.flush()
        (await db.get(PrinterQueue, printer_id)).current_item_id = item.id
        archive = await db.get(PrintArchive, archive_id)
        archive.extra_data = {
            **archive.extra_data,
            main._TERMINAL_ACCEPTANCE_KEY: main._terminal_acceptance_payload(item, "completed"),
        }
        await db.commit()
    window = Window(factory, archive_id, archive_write_scope.load_active_archive_for_write)
    window.install(monkeypatch)
    monkeypatch.setattr(archive_write_scope, "locked_print", window.around(archive_write_scope.locked_print))

    async with factory() as sweep:
        stale = await sweep.get(PrintArchive, archive_id)
        assert await print_reconciliation._recover_accepted_terminal_checkpoint(sweep, stale) == archive_id
        await sweep.commit()
    await window.settled()

    row = await a_row(factory, archive_id)
    assert row.extra_data["terminal_effects_uncertain"] is True
    assert sc.archive_fact(row) == sc.ArchiveFact(True, recorded=True)
