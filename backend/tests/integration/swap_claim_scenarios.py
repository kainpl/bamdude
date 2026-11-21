"""Our owed change_table under two real PostgreSQL sessions (Codex review r1, SS-I-R1..R3; T9 plan, PG scenarios).

Run by ``postgres_scenario_runner`` in its own interpreter (``swap:<name>``) against the dedicated scratch server.
Every entry is the production one -- ``reconcile_printer_prints``, ``_resolve_pending_swaps``, the claim, the
dispatch lane's admission -- with the process-local asyncio locks replaced by no-ops, so what serializes two
sessions is PostgreSQL's own advisory locks and row locks, as between two processes. The hardware is replaced (the
macro executor, the macro lookup), and so are the effects a sweep must never repeat, which are counted (usage
booking, energy). A deadlock, an error or a timeout is reported as such, never folded into "ok".
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import text

CHANGE_TABLE = "swap_mode_change_table"
MACRO = SimpleNamespace(name="Change table", gcode="G28 ; change table")


class _NoopLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _process_locks_off() -> None:
    """Only PostgreSQL's fences may serialize the sessions (as between independent processes)."""
    from backend.app.services import archive_write_scope, queue_ops

    archive_write_scope._archive_locks = defaultdict(_NoopLock)
    queue_ops._queue_claim_locks = defaultdict(_NoopLock)


class Farm(SimpleNamespace):
    pass


async def _farm(*, current: str = "run") -> Farm:
    """A swap printer whose run of a plain source just ended; our change_table is owed."""
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer import Printer
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services import swap_compatibility as sc

    started = datetime.now(timezone.utc).replace(microsecond=0)
    async with async_session() as db:
        printer = Printer(
            name="swap-claim-printer",
            serial_number="SWAPCLAIM001",
            ip_address="192.0.2.31",
            access_code="00000000",
            model="P1S",
            is_active=True,
            auto_archive=True,
            swap_mode_enabled=True,
            require_plate_clear=False,
        )
        db.add(printer)
        await db.flush()
        queue = PrinterQueue(id=printer.id, printer_id=printer.id, status="printing")
        db.add(queue)
        archive = PrintArchive(
            printer_id=printer.id,
            filename="job.gcode.3mf",
            print_name="job",
            file_path="archive/1/job.gcode.3mf",
            file_size=1,
            status="printing",
            started_at=started,
            content_hash="ab" * 32,
            source_content_hash="ab" * 32,
            extra_data={"swap_macro_events_pending": [CHANGE_TABLE]},
        )
        sc.write_archive_fact(archive, False)
        db.add(archive)
        await db.flush()
        item = PrintQueueItem(
            queue_id=queue.id,
            archive_id=archive.id,
            status="printing",
            position=0,
            started_at=started,
            execute_swap_macros=True,
        )
        db.add(item)
        await db.flush()
        queue.current_item_id = item.id if current == "run" else None
        await db.commit()
        return Farm(
            printer_id=printer.id,
            queue_id=queue.id,
            archive_id=archive.id,
            archive_identity=archive.recovery_identity,
            item_id=item.id,
        )


def _count_effects(farm: Farm) -> None:
    """The hardware and the effects a second close must never repeat, replaced and counted."""
    from backend.app import main
    from backend.app.services import print_reconciliation, usage_tracker
    from backend.app.services.printer_manager import printer_manager

    farm.sent = []
    farm.seen_before_send = []
    farm.usage = 0
    farm.energy = 0
    farm.tasks = []
    farm.send_gate = None  # an Event the command waits on, when a scenario wants it to stay in flight
    farm.on_send = None  # a coroutine function run inside the command, before its answer
    farm.on_usage = None  # a coroutine function run inside the close's usage booking (its transaction open)

    async def usage(*_args, **_kwargs):
        farm.usage += 1
        if farm.on_usage is not None:
            await farm.on_usage()
        return []

    async def energy(*_args, **_kwargs):
        farm.energy += 1

    async def lookup(_db, _event, _printer):
        return MACRO

    async def send(printer_id, gcode, name, **kwargs):
        farm.sent.append(name)
        farm.seen_before_send.append(await _stage(farm.archive_id))  # a third session: only what was committed
        if farm.on_send is not None:
            await farm.on_send(kwargs.get("may_run"))
        if farm.send_gate is not None:
            await asyncio.wait_for(farm.send_gate.wait(), 20)
        return True, "ok"

    def spawn(coro, *, name):
        task = asyncio.ensure_future(coro)
        farm.tasks.append(task)
        return task

    usage_tracker.on_print_complete = usage
    usage_tracker.get_persisted_print_name = _none
    main._record_print_energy = energy
    print_reconciliation._grace_seconds = _zero
    print_reconciliation._load_objects_for_a_print_already_running = _none
    printer_manager.execute_macro_and_wait = send
    printer_manager.arm_awaiting_plate_clear = _none
    import backend.app.core.tasks as tasks
    import backend.app.services.macro_executor as macro_executor

    tasks.spawn_background_task = spawn
    macro_executor.find_swap_macro = lookup


async def _none(*_args, **_kwargs):
    return None


async def _zero(*_args, **_kwargs):
    return 0


async def _stage(archive_id: int) -> str | None:
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.services import swap_terminal

    async with async_session() as db:
        archive = await db.get(PrintArchive, archive_id)
        return swap_terminal.claim_stage(archive.extra_data or {})


async def _state(farm: Farm) -> dict:
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services import swap_terminal

    async with async_session() as db:
        archive = await db.get(PrintArchive, farm.archive_id)
        queue = await db.get(PrinterQueue, farm.queue_id)
        extra = archive.extra_data or {}
        run = await db.get(PrintQueueItem, farm.item_id)
        return {
            "sent": len(farm.sent),
            "stage": swap_terminal.claim_stage(extra),
            "pending": CHANGE_TABLE in (extra.get("swap_macro_events_pending") or []),
            "hold": (extra.get("swap_hold") or {}).get("reason"),
            "queue": queue.status,
            "current": queue.current_item_id,
            "archive": archive.status,
            "run_kept": run is not None,
            "usage": farm.usage,
            "energy": farm.energy,
            "seen_before_send": farm.seen_before_send,
        }


async def _close(farm: Farm) -> None:
    """The sweep's ordinary close, committed, its resolver not run yet."""
    from backend.app.core.database import async_session
    from backend.app.models.archive import PrintArchive
    from backend.app.services.print_reconciliation import _reconcile_complete_archive

    async with async_session() as db:
        archive = await db.get(PrintArchive, farm.archive_id)
        await _reconcile_complete_archive(db, archive, status="completed", uncertain=False)
        await db.commit()


async def _a_new_job_takes_the_printer(farm: Farm) -> int:
    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.models.printer_queue import PrinterQueue

    async with async_session() as db:
        other = PrintQueueItem(
            queue_id=farm.queue_id, status="printing", position=1, started_at=datetime.now(timezone.utc)
        )
        db.add(other)
        await db.flush()
        queue = await db.get(PrinterQueue, farm.queue_id)
        queue.status, queue.current_item_id = "printing", other.id
        await db.commit()
        return other.id


async def _waiting_on_a_lock() -> int:
    from backend.app.core.database import engine

    async with engine.connect() as conn:
        return int(
            await conn.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                )
            )
        )


async def _outcome(coro) -> str:
    from backend.tests.integration.lock_barriers import sqlstate

    try:
        await asyncio.wait_for(coro, 60)
    except TimeoutError:
        return "timeout"
    except asyncio.CancelledError:
        return "cancelled"
    except Exception as exc:  # noqa: BLE001 — reported, never folded into "ok"
        return "deadlock" if sqlstate(exc) == "40P01" else f"error:{type(exc).__name__}:{exc}"
    return "ok"


# --- 1. two connect edges on one run ---------------------------------------------------------------------------------


async def scenario_two_edges() -> dict:
    """Two production connect edges read the same printing archive, both reach the close; one closes it, one claim
    is taken and committed before the command, which goes out once; usage and energy are booked once."""
    from backend.app.services import print_reconciliation

    _process_locks_off()
    farm = await _farm()
    _count_effects(farm)
    original_close = print_reconciliation._reconcile_complete_archive
    arrived = 0
    both = asyncio.Event()
    waited: list[int] = []

    async def close(*args, **kwargs):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 20)
        return await original_close(*args, **kwargs)

    print_reconciliation._reconcile_complete_archive = close

    async def hold_the_first_close_open():
        """The first close holds the archive's row lock here; it goes on once PostgreSQL shows the second waiting."""
        for _ in range(250):
            count = await _waiting_on_a_lock()
            if count:
                waited.append(count)
                return
            await asyncio.sleep(0.02)
        waited.append(0)

    farm.on_usage = hold_the_first_close_open
    edges = [
        _outcome(print_reconciliation.reconcile_printer_prints(farm.printer_id, "FINISH", "job.gcode.3mf"))
        for _ in range(2)
    ]
    outcomes = await asyncio.gather(*edges)
    resolved = await asyncio.gather(*[_outcome(task) for task in list(farm.tasks)])
    return {
        "outcomes": list(outcomes),
        "resolvers": list(resolved),
        "max_waiting": max(waited or [0]),
        **await _state(farm),
    }


# --- 2. an old sweep against a new printer claim ---------------------------------------------------------------------


async def scenario_new_owner_before_the_claim() -> dict:
    """A dispatch admission holds the printer's lane and takes the printer for a new job while the old run's resolver
    reaches its claim: the claim waits on PostgreSQL, then sees the new owner -- no command, the new claim intact."""
    from backend.app.core.database import async_session
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services import print_reconciliation
    from backend.app.services.printer_occupancy import read_queue_occupancy
    from backend.app.services.queue_ops import queue_claim_scope

    _process_locks_off()
    farm = await _farm()
    _count_effects(farm)
    await _close(farm)
    entered = asyncio.Event()
    release = asyncio.Event()
    new_job: list[int] = []

    async def admission():
        async with async_session() as db, queue_claim_scope(db, farm.queue_id):
            occupancy = await read_queue_occupancy(db, farm.queue_id, for_update=True)
            other = PrintQueueItem(
                queue_id=farm.queue_id, status="printing", position=1, started_at=datetime.now(timezone.utc)
            )
            db.add(other)
            await db.flush()
            occupancy.queue.status, occupancy.queue.current_item_id = "printing", other.id
            new_job.append(other.id)
            entered.set()
            await asyncio.wait_for(release.wait(), 20)
            await db.commit()

    held = asyncio.ensure_future(_outcome(admission()))
    await asyncio.wait_for(entered.wait(), 20)
    resolver = asyncio.ensure_future(
        _outcome(print_reconciliation._resolve_pending_swaps(farm.printer_id, [farm.archive_id]))
    )
    waited = 0
    for _ in range(100):
        waited = max(waited, await _waiting_on_a_lock())
        if waited:
            break
        await asyncio.sleep(0.02)
    release.set()
    outcomes = [await held, await resolver]
    return {"outcomes": outcomes, "waited": waited, "new_job": new_job[0], **await _state(farm)}


async def scenario_new_owner_during_the_command() -> dict:
    """The printer is taken by a new job while our command is in flight; the answer arrives: the old run neither
    releases nor pauses the new claim, and keeps its row."""
    from backend.app.services import print_reconciliation

    _process_locks_off()
    farm = await _farm()
    _count_effects(farm)
    await _close(farm)
    new_job: list[int] = []

    async def while_sending(_may_run):
        new_job.append(await _a_new_job_takes_the_printer(farm))

    farm.on_send = while_sending
    outcome = await _outcome(print_reconciliation._resolve_pending_swaps(farm.printer_id, [farm.archive_id]))
    return {"outcome": outcome, "new_job": new_job[0], **await _state(farm)}


# --- 3. an exception / cancellation after the claim, then a fresh connect edge ---------------------------------------


async def scenario_interrupted_then_reconnect() -> dict:
    """The process stops right after the command went out; a fresh connect edge finds the completed run still holding
    its printer: held with its reason, the command not repeated, usage and energy not booked again."""
    from backend.app.services import print_reconciliation

    _process_locks_off()
    farm = await _farm()
    _count_effects(farm)
    await _close(farm)

    async def stop(_may_run):
        raise asyncio.CancelledError("the process stops after the command went out")

    farm.on_send = stop
    first = await _outcome(print_reconciliation._resolve_pending_swaps(farm.printer_id, [farm.archive_id]))
    farm.on_send = None
    before = (farm.usage, farm.energy)
    edge = await _outcome(print_reconciliation.reconcile_printer_prints(farm.printer_id, "FINISH", "job.gcode.3mf"))
    resolved = await asyncio.gather(*[_outcome(task) for task in list(farm.tasks)])
    state = await _state(farm)
    return {"first": first, "edge": edge, "resolvers": list(resolved), "effects_before": list(before), **state}


async def scenario_interrupted_then_another_job() -> dict:
    """The same interruption, but another job already holds the printer when the next edge comes: that job's queue is
    left alone, the old run's row kept for the operator, nothing sent again."""
    from backend.app.services import print_reconciliation

    _process_locks_off()
    farm = await _farm()
    _count_effects(farm)
    await _close(farm)

    async def stop(_may_run):
        raise asyncio.CancelledError("the process stops after the command went out")

    farm.on_send = stop
    first = await _outcome(print_reconciliation._resolve_pending_swaps(farm.printer_id, [farm.archive_id]))
    farm.on_send = None
    other = await _a_new_job_takes_the_printer(farm)
    edge = await _outcome(print_reconciliation.reconcile_printer_prints(farm.printer_id, "FINISH", "job.gcode.3mf"))
    resolved = await asyncio.gather(*[_outcome(task) for task in list(farm.tasks)])
    return {"first": first, "edge": edge, "resolvers": list(resolved), "new_job": other, **await _state(farm)}


async def scenario_lookup_error() -> dict:
    """The resolver's macro lookup raises: the run's own claim ends in an explained hold, never in silence."""
    import backend.app.services.macro_executor as macro_executor
    from backend.app.services import print_reconciliation

    _process_locks_off()
    farm = await _farm()
    _count_effects(farm)
    await _close(farm)

    async def broken(_db, _event, _printer):
        raise RuntimeError("lookup failed")

    macro_executor.find_swap_macro = broken
    outcome = await _outcome(print_reconciliation._resolve_pending_swaps(farm.printer_id, [farm.archive_id]))
    return {"outcome": outcome, **await _state(farm)}


# --- 4. no deadlock: the claim, an archive write and a dispatch admission at once ------------------------------------


async def scenario_no_deadlock() -> dict:
    """While the claim's transaction holds the lane and the print, an archive writer and a dispatch admission start:
    both wait on PostgreSQL, nothing deadlocks, the writer's key and the claim both survive, the admission is refused
    (the run still holds the printer), and the command goes out once."""
    from backend.app import main
    from backend.app.core.database import async_session
    from backend.app.services import print_reconciliation
    from backend.app.services.printer_occupancy import (
        PrinterOccupancyConflict,
        read_queue_occupancy,
        require_direct_admission,
    )
    from backend.app.services.queue_ops import queue_claim_scope

    _process_locks_off()
    farm = await _farm()
    _count_effects(farm)
    await _close(farm)
    in_claim = asyncio.Event()
    release = asyncio.Event()
    original_proof = print_reconciliation._run_holds_its_printer

    async def proof(*args, **kwargs):
        answer = await original_proof(*args, **kwargs)
        in_claim.set()
        await asyncio.wait_for(release.wait(), 20)
        return answer

    print_reconciliation._run_holds_its_printer = proof
    admission_code: list[str] = []

    async def admission():
        async with async_session() as db, queue_claim_scope(db, farm.queue_id):
            occupancy = await read_queue_occupancy(db, farm.queue_id, for_update=True)
            try:
                require_direct_admission(occupancy)
                admission_code.append("admitted")
            except PrinterOccupancyConflict as exc:
                admission_code.append(exc.code)
            await db.rollback()

    resolver = asyncio.ensure_future(
        _outcome(print_reconciliation._resolve_pending_swaps(farm.printer_id, [farm.archive_id]))
    )
    await asyncio.wait_for(in_claim.wait(), 20)
    writer = asyncio.ensure_future(
        _outcome(
            main._record_terminal_effect_stage(
                farm.archive_id, "finish_photo", "attempted", expected_identity=farm.archive_identity
            )
        )
    )
    door = asyncio.ensure_future(_outcome(admission()))
    waited = 0
    for _ in range(100):
        waited = max(waited, await _waiting_on_a_lock())
        if waited >= 2:
            break
        await asyncio.sleep(0.02)
    release.set()
    outcomes = [await resolver, await writer, await door]
    state = await _state(farm)
    async with async_session() as db:
        from backend.app.models.archive import PrintArchive

        effects = ((await db.get(PrintArchive, farm.archive_id)).extra_data or {}).get("bamdude_terminal_effects") or {}
    return {
        "outcomes": outcomes,
        "waited": waited,
        "admission": admission_code,
        "writer_key": "finish_photo" in effects,
        **state,
    }


SCENARIOS = {
    "two_edges": scenario_two_edges,
    "new_owner_before_the_claim": scenario_new_owner_before_the_claim,
    "new_owner_during_the_command": scenario_new_owner_during_the_command,
    "interrupted_then_reconnect": scenario_interrupted_then_reconnect,
    "interrupted_then_another_job": scenario_interrupted_then_another_job,
    "lookup_error": scenario_lookup_error,
    "no_deadlock": scenario_no_deadlock,
}
