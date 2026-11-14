"""A finished run on a swap printer, driven through the real completion callback (plan T7).

The database is real, the run binding is real and so is ``_accept_bound_terminal_run``; only the hardware
(the macro executor), the outside world (WebSocket, relay, plugs, notifications, energy, the SD cleanup) and
the 3MF download are replaced. A test arranges an execution archive, its queue row and its binding, finishes
it, and reads the outcome off the rows: the plate gate, the queue, the held row, the archive's records and the
macros that were sent.

⚠️ On SQLite the rig runs on its OWN file database. The ordinary test engine shares one connection between
every session, and a completion opens several at once -- closing any of them rolls back the others' work, so
a write that committed can read back as never made.
"""

from __future__ import annotations

import asyncio
import itertools
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app import main
from backend.app.core.database import Base
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import swap_compatibility as sc
from backend.app.services.print_run_binding import bind_print_run
from backend.app.services.printer_manager import PrinterManager

CHANGE_TABLE = "swap_mode_change_table"
START = "swap_mode_start"

#: "not recorded at all": an archive from before the fix, with only its bool column.
LEGACY = object()
#: A record that contradicts its column -- unknown, whatever either says.
CONFLICT = object()

_serials = itertools.count(1)


@dataclass
class Rig:
    printer_id: int
    queue_id: int
    manager: PrinterManager
    factory: async_sessionmaker
    macro: AsyncMock
    tasks: list = field(default_factory=list)

    @asynccontextmanager
    async def db(self):
        async with self.factory() as session:
            yield session


@pytest.fixture
async def make_rig(test_engine, tmp_path, monkeypatch):
    """``await make_rig(require_plate_clear=..., swap_mode=...)`` -> a Rig; engines are disposed afterwards."""
    from backend.app.services import archive_write_scope

    # Ids restart with every rig's database, and a lock left held when an earlier test's loop ended is bound to
    # that dead loop.
    archive_write_scope._archive_locks.clear()
    archive_write_scope._archive_file_locks.clear()
    engines = []

    async def build(*, require_plate_clear=True, swap_mode=True, model="P1S") -> Rig:
        if test_engine.dialect.name == "sqlite":
            engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'swap-{len(engines)}.db'}")
            engines.append(engine)
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        else:
            engine = test_engine
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        serial = next(_serials)
        async with factory() as db:
            printer = Printer(
                name=f"Swap {serial}",
                serial_number=f"00M09B{serial:09d}",
                ip_address=f"192.0.2.{serial % 250 + 1}",
                access_code="12345678",
                is_active=True,
                auto_archive=True,
                model=model,
                plate_detection_enabled=False,
                require_plate_clear=require_plate_clear,
                swap_mode_enabled=swap_mode,
            )
            db.add(printer)
            await db.flush()
            queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="idle")
            db.add(queue)
            await db.commit()
            printer_id, queue_id = printer.id, queue.id

        manager = PrinterManager()
        macro = AsyncMock(return_value=(True, "ok"))
        manager.execute_macro_and_wait = macro
        rig = Rig(printer_id=printer_id, queue_id=queue_id, manager=manager, factory=factory, macro=macro)

        monkeypatch.setattr(main, "printer_manager", manager)
        monkeypatch.setattr("backend.app.services.printer_manager.printer_manager", manager)
        monkeypatch.setattr(main, "async_session", factory)
        monkeypatch.setattr("backend.app.core.database.async_session", factory)
        monkeypatch.setattr(main, "ws_manager", AsyncMock())
        monkeypatch.setattr(main, "mqtt_relay", AsyncMock())
        monkeypatch.setattr(main, "smart_plug_manager", AsyncMock())
        monkeypatch.setattr(main, "notification_service", AsyncMock())
        monkeypatch.setattr(main, "_record_print_energy", AsyncMock())
        monkeypatch.setattr(
            "backend.app.services.archive_download_retry.archive_download_retry.retry_archive",
            AsyncMock(return_value="already_has_file"),
        )
        monkeypatch.setattr("backend.app.services.macro_trigger.fire_event_macros", AsyncMock())
        monkeypatch.setattr("backend.app.services.usage_tracker.on_print_complete", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            "backend.app.services.macro_executor.find_swap_macro",
            AsyncMock(return_value=SimpleNamespace(name="Change table", gcode="G28 ; change table")),
        )
        real_spawn = main.spawn_background_task

        def spawn(coro, *, name):
            if name.startswith("post-print-cleanup-"):
                # The printer is not there: its SD cleanup would spend minutes on FTP.
                coro.close()
                done = asyncio.get_running_loop().create_future()
                done.set_result(None)
                return done
            task = real_spawn(coro, name=name)
            rig.tasks.append(task)
            return task

        monkeypatch.setattr(main, "spawn_background_task", spawn)
        main._active_swap_config.pop(printer_id, None)
        return rig

    yield build
    archive_write_scope._archive_locks.clear()
    archive_write_scope._archive_file_locks.clear()
    main._active_swap_config.clear()
    main._active_prints.clear()
    for engine in engines:
        await engine.dispose()


async def an_execution(
    rig: Rig,
    *,
    fact=True,
    origin: str = "dispatch",
    bound: bool = True,
    pending: list[str] | None = None,
    in_memory: bool = False,
    start_effect: str | None = None,
    auto_eject: bool = False,
    file_path: str = "archive/1/job.gcode.3mf",
    name: str = "job",
) -> tuple[int, int]:
    """A run of ours in flight: its execution archive, its queue row holding the printer, and its binding.

    ``fact`` is the archive's declaration -- True / False / None as a record, ``LEGACY`` for no record (the bool
    column False) or ``(LEGACY, True)`` for a legacy True column, ``CONFLICT`` for a True record over a False
    column. ``pending`` is the swap events dispatch left for completion; ``in_memory`` also registers them as
    the live swap config. Returns ``(archive_id, item_id)``.
    """
    started = datetime.now(timezone.utc)
    extra: dict = {}
    if pending:
        extra["swap_macro_events_pending"] = list(pending)
    if start_effect:
        extra["swap_start_effect"] = start_effect
    if auto_eject:
        extra["dispatch_intent"] = {"auto_eject": True, "auto_eject_settings": {}}
    async with rig.db() as db:
        archive = PrintArchive(
            printer_id=rig.printer_id,
            filename=f"{name}.gcode.3mf",
            print_name=name,
            file_path=file_path,
            file_size=1,
            status="printing",
            started_at=started,
            content_hash="ab" * 32,
            source_content_hash="ab" * 32,
            extra_data=extra,
        )
        if fact is LEGACY:
            archive.swap_compatible = False
        elif isinstance(fact, tuple) and fact[0] is LEGACY:
            archive.swap_compatible = fact[1]
        elif fact is CONFLICT:
            sc.write_archive_fact(archive, True)
            archive.swap_compatible = False
        else:
            sc.write_archive_fact(archive, fact)
        db.add(archive)
        await db.flush()
        item = PrintQueueItem(
            queue_id=rig.queue_id,
            archive_id=archive.id,
            status="printing",
            position=0,
            started_at=started,
            execute_swap_macros=bool(pending),
        )
        db.add(item)
        await db.flush()
        queue = await db.get(PrinterQueue, rig.queue_id)
        queue.status, queue.current_item_id = "printing", item.id
        await db.commit()
        archive_id, item_id, archive_identity = archive.id, item.id, archive.recovery_identity
    if bound:
        bind_print_run(
            rig.manager,
            printer_id=rig.printer_id,
            archive_id=archive_id,
            archive_identity=archive_identity,
            queue_item_id=item_id,
            claim_started_at=started,
            observed_subtask_id="81",
            origin=origin,
        )
    if in_memory and pending:
        main._active_swap_config[rig.printer_id] = {"swap_macro_events": list(pending)}
    return archive_id, item_id


async def a_pending_job(rig: Rig) -> int:
    async with rig.db() as db:
        item = PrintQueueItem(queue_id=rig.queue_id, status="pending", position=5)
        db.add(item)
        await db.commit()
        return item.id


async def complete(rig: Rig, data: dict | None = None) -> None:
    payload = {"status": "completed", "subtask_id": "81"} if data is None else data
    await main.on_print_complete(rig.printer_id, payload)
    while rig.tasks:
        running, rig.tasks[:] = list(rig.tasks), []
        await asyncio.gather(*running, return_exceptions=True)


def gate_armed(rig: Rig) -> bool:
    """The gate the scheduler reads. Arming persists it too; a release is published to memory first."""
    return rig.manager.is_awaiting_plate_clear(rig.printer_id)


async def stand_gate(rig: Rig) -> None:
    """A plate-clear gate already standing before this run finishes."""
    async with rig.db() as db:
        printer = await db.get(Printer, rig.printer_id)
        printer.awaiting_plate_clear = True
        await db.commit()
    rig.manager._awaiting_plate_clear.add(rig.printer_id)


async def queue_status(rig: Rig) -> str:
    async with rig.db() as db:
        return await db.scalar(select(PrinterQueue.status).where(PrinterQueue.id == rig.queue_id))


async def item_of(rig: Rig, item_id: int) -> PrintQueueItem | None:
    async with rig.db() as db:
        return await db.get(PrintQueueItem, item_id)


async def archive_of(rig: Rig, archive_id: int) -> PrintArchive:
    async with rig.db() as db:
        return await db.get(PrintArchive, archive_id)


def change_tables_sent(rig: Rig) -> int:
    return sum(1 for call in rig.macro.await_args_list if "change table" in str(call.args[1]))


# --- the execution job's own captured source (§6) -------------------------------------------------------------------


async def give_job_a_snapshot(
    rig: Rig, item_id: int, *, fact="missing", library_file_id: int | None = None, sha: str = "ab" * 32
) -> None:
    """Attach a captured source to the job: ``fact`` True/False/None is recorded in its snapshot, ``"missing"``
    is a snapshot from before the declaration was captured (resolved only through the job's own links, §6)."""
    from backend.app.models.queue_source import QueueSource
    from backend.app.services.queue_source_descriptor import SOURCE_SNAPSHOT_VERSION

    async with rig.db() as db:
        source = QueueSource(sha256=sha, size_bytes=1, relative_path=f"queue-spool/objects/ab/{sha}.3mf", format="3mf")
        db.add(source)
        await db.flush()
        snapshot = {
            "version": SOURCE_SNAPSHOT_VERSION,
            "provenance": {"kind": "library_file", "id": library_file_id} if library_file_id else {},
            "display_filename": "job.gcode.3mf",
            "format": "3mf",
            "plate_fallback": None,
        }
        if fact != "missing":
            snapshot.update(sc.snapshot_fields(sc.SourceFact(fact) if fact is not None else sc.UNKNOWN_SOURCE))
        item = await db.get(PrintQueueItem, item_id)
        item.queue_source_id = source.id
        item.source_snapshot = snapshot
        item.library_file_id = library_file_id
        await db.commit()


async def a_library_file(rig: Rig, *, declared: bool, file_hash: str = "ab" * 32) -> int:
    from backend.app.models.library import LibraryFile

    async with rig.db() as db:
        row = LibraryFile(
            filename="job.gcode.3mf",
            file_path="library/job.gcode.3mf",
            file_type="gcode",
            file_size=1,
            file_hash=file_hash,
            swap_compatible=declared,
        )
        db.add(row)
        await db.commit()
        return row.id


async def another_printer(rig: Rig) -> int:
    """A second printer on the same farm, idle with its own queue."""
    serial = next(_serials)
    async with rig.db() as db:
        printer = Printer(
            name=f"Neighbour {serial}",
            serial_number=f"00M09C{serial:09d}",
            ip_address=f"198.51.100.{serial % 250 + 1}",
            access_code="12345678",
            is_active=True,
            model="P1S",
        )
        db.add(printer)
        await db.flush()
        db.add(PrinterQueue(id=printer.id, printer_id=printer.id, status="idle"))
        await db.commit()
        return printer.id


async def recover(rig: Rig, archive_id: int, *, uncertain: bool = False) -> None:
    """What the connect-edge sweep does for a print that ended while BamDude was down or disconnected."""
    from backend.app.services.print_reconciliation import _reconcile_complete_archive, _resolve_pending_swaps

    async with rig.db() as db:
        archive = await db.get(PrintArchive, archive_id)
        await _reconcile_complete_archive(db, archive, status="completed", uncertain=uncertain)
        await db.commit()
    await _resolve_pending_swaps(rig.printer_id, [archive_id])


async def close_only(rig: Rig, archive_id: int) -> None:
    """The sweep's close alone, before it settles what the swap still owes."""
    from backend.app.services.print_reconciliation import _reconcile_complete_archive

    async with rig.db() as db:
        archive = await db.get(PrintArchive, archive_id)
        await _reconcile_complete_archive(db, archive, status="completed", uncertain=False)
        await db.commit()


async def an_owed_run_closed(rig: Rig) -> tuple[int, int]:
    """A run of ours over a plain source whose change_table is owed, closed by the sweep -- the resolver not run yet
    (the process may have stopped right there). Returns ``(archive_id, item_id)``."""
    archive_id, item_id = await an_execution(rig, fact=False, pending=[CHANGE_TABLE], bound=False)
    await close_only(rig, archive_id)
    return archive_id, item_id
