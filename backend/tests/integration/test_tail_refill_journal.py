"""The runout tail in the persisted usage journal (post-rebuild F, real database).

Spec: vault ``60-specs/ams-tail-refill-attribution-spec.md`` — F-R2 (``tail_start`` /
``feed_return``, one open episode, serialised check-and-append), F-R3 (an assignment during
the tail is identity only and never steals the old reel's id; the Spoolman assignment takes
the assignment lock, its remote I/O outside it), F-R4 (the seed of a joined print), F-R5
(the new reel's real runout still counts). Rows F01, F03, F04, F06, F07, F09, F10.
"""

import asyncio
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

import backend.app.models.printer_location  # noqa: F401 — resolves Printer's relationship
from backend.app.models.archive import PrintArchive
from backend.app.models.print_usage_event import (
    EVENT_FEED_RETURN,
    EVENT_RUNOUT,
    EVENT_SPOOL_LOADED,
    EVENT_START,
    EVENT_TAIL_START,
    EVENT_TRAY_CHANGE,
    KIND_GAP,
    KIND_PAUSE,
    PrintUsageEvent,
)
from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import print_usage_journal as journal
from backend.app.services.print_usage_journal import RunoutResult

pytestmark = pytest.mark.integration  # asyncio_mode = "auto" runs the coroutines

AMS, SLOT = 0, 2
TRAY = AMS * 4 + SLOT


async def a_print(db, printer_factory, *, subtask_id=None):
    printer = await printer_factory()
    archive = PrintArchive(
        printer_id=printer.id,
        filename="plate.gcode.3mf",
        print_name="plate",
        file_path="x/plate.gcode.3mf",
        file_size=1,
        status="printing",
        subtask_id=subtask_id,
    )
    db.add(archive)
    await db.commit()
    return SimpleNamespace(id=printer.id), SimpleNamespace(id=archive.id)


async def a_spool(db, name):
    spool = Spool(material="PLA", subtype="Basic", brand=name, color_name=name, rgba="FF0000FF", label_weight=1000)
    db.add(spool)
    await db.commit()
    return SimpleNamespace(id=spool.id)


async def bind(db, printer_id, spool_id):
    old = await db.scalar(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id, SpoolAssignment.ams_id == AMS, SpoolAssignment.tray_id == SLOT
        )
    )
    if old is not None:
        await db.delete(old)
        await db.flush()
    db.add(
        SpoolAssignment(
            spool_id=spool_id,
            printer_id=printer_id,
            ams_id=AMS,
            tray_id=SLOT,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    await db.commit()


async def rows(db, printer_id, archive_id):
    db.expire_all()
    return [(e.event, e.layer_num, e.spool_id, e.kind) for e in await journal.load_events(db, printer_id, archive_id)]


@pytest.fixture
async def incident(db_session, printer_factory):
    """Printer 10's X2D: reel 309 feeding tray 2, the replacement 313 on the shelf."""
    printer, archive = await a_print(db_session, printer_factory)
    old, new = await a_spool(db_session, "old"), await a_spool(db_session, "new")
    await bind(db_session, printer.id, old.id)
    await journal.record_event(
        db_session, printer_id=printer.id, archive_id=archive.id, layer_num=0, event=EVENT_START,
        global_tray_id=TRAY, spool_id=old.id,
    )  # fmt: skip
    return printer, archive, old, new


async def tail_fact(db, printer, event, layer, *, after_gap=False, subtask_id=None, **observed):
    """One queued tail callback; ``observed`` carries what the real one carries (``generation``)."""
    async with journal.journal_lock(printer.id):
        return await journal.handle_feed_event(
            db, printer_id=printer.id, event=event, origin=TRAY, layer_num=layer, after_gap=after_gap,
            subtask_id=subtask_id, **observed,
        )  # fmt: skip


def a_client_on_print(generation, subtask_id=None):
    """The printer's MQTT client as the journal reads it: the print it is on now."""
    client = MagicMock()
    client.journal_generation.return_value = generation
    client.journal_held.return_value = False
    client.open_tail.return_value = None
    client.state.subtask_id = subtask_id
    return client


# ── F-R2 · the two tail facts ────────────────────────────────────────────────


async def test_the_tail_freezes_the_reel_running_out_and_the_return_the_reel_bound_then(db_session, incident):
    printer, archive, old, new = incident
    assert await tail_fact(db_session, printer, "tail_start", 40)
    assert not await tail_fact(db_session, printer, "tail_start", 41)  # one open episode
    await bind(db_session, printer.id, new.id)
    assert await tail_fact(db_session, printer, "feed_return", 42)
    assert not await tail_fact(db_session, printer, "feed_return", 43)  # nothing open to close

    assert await rows(db_session, printer.id, archive.id) == [
        (EVENT_START, 0, old.id, None),
        (EVENT_TAIL_START, 40, old.id, None),
        (EVENT_FEED_RETURN, 42, new.id, None),
    ]


async def test_a_return_seen_after_a_gap_is_marked(db_session, incident):
    printer, archive, old, new = incident
    await tail_fact(db_session, printer, "tail_start", 40)
    await tail_fact(db_session, printer, "feed_return", 50, after_gap=True)
    assert (await rows(db_session, printer.id, archive.id))[-1] == (EVENT_FEED_RETURN, 50, old.id, KIND_GAP)


async def test_a_fact_of_another_print_never_lands_in_this_one(db_session, printer_factory):
    """F-R2: the observation names subtask 7, the printing archive another one."""
    printer, archive = await a_print(db_session, printer_factory, subtask_id="8")
    assert not await tail_fact(db_session, printer, "tail_start", 40, subtask_id="7")
    assert await rows(db_session, printer.id, archive.id) == []


async def test_the_assignment_writer_rejects_the_tail_of_another_known_print(db_session, printer_factory):
    """F-R2: both tail writers ask one owner — the printing archive names another subtask
    than the client the open tail was read from, so the assignment journals nothing."""
    printer, archive = await a_print(db_session, printer_factory, subtask_id="new-print")
    client = a_client_on_print(3, subtask_id="old-print")
    client.open_tail.return_value = (TRAY, 40)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        async with journal.journal_lock(printer.id):
            assert not await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
    assert await rows(db_session, printer.id, archive.id) == []


@pytest.mark.parametrize("bound", [True, False], ids=["next-print-bound", "nothing-bound-yet"])
async def test_a_late_callback_of_the_print_before_never_opens_an_episode_in_the_next(
    db_session, printer_factory, bound
):
    """F-R2: no device ids anywhere — the fact was observed in the client's previous print
    (journal generation 7); the client is on print 8 now, whose archive is printing, bound
    to it or not yet."""
    printer, archive = await a_print(db_session, printer_factory)
    if bound:
        journal.bind_tail_owner(printer.id, archive.id, 8)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = a_client_on_print(8)
        assert not await tail_fact(db_session, printer, "tail_start", 40, generation=7)
    assert await rows(db_session, printer.id, archive.id) == []


async def test_a_fact_of_the_new_print_never_lands_in_the_archive_of_the_last(db_session, printer_factory):
    """F-R2: print 8 started and its archive is not bound yet; the archive still printing
    belongs to print 7, so print 8's fact has no archive rather than the wrong one."""
    printer, previous = await a_print(db_session, printer_factory)
    journal.bind_tail_owner(printer.id, previous.id, 7)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = a_client_on_print(8)
        assert not await tail_fact(db_session, printer, "tail_start", 40, generation=8)
        client = a_client_on_print(8)
        client.open_tail.return_value = (TRAY, 40)
        pm.get_client.return_value = client
        async with journal.journal_lock(printer.id):
            assert not await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
    assert await rows(db_session, printer.id, previous.id) == []


async def test_the_feed_callback_carries_the_print_the_client_observed_it_in(printer_factory):
    """F-R2: the manager's wrapper reads the subtask and the journal generation on the MQTT
    thread, inside the observation — the queued handler may run after the next print began."""
    from backend.app.services.printer_manager import PrinterManager

    printer = await printer_factory()
    manager = PrinterManager()
    received = MagicMock()
    manager.set_feed_event_callback(received)
    with (
        patch("backend.app.services.printer_manager.BambuMQTTClient") as client_class,
        patch.object(manager, "_schedule_async"),
    ):
        client = client_class.return_value
        client.state.connected = True
        client.state.subtask_id = "577"
        client.journal_generation.return_value = 9
        await manager.connect_printer(printer)
        client_class.call_args.kwargs["on_feed_event"]("tail_start", TRAY, 40, False)
    received.assert_called_once_with(printer.id, "tail_start", TRAY, 40, False, "577", 9)


def test_the_run_lifecycle_binds_the_tail_owner_to_the_print_the_client_is_on(monkeypatch, tmp_path):
    """F-R2: the one choke point of own, external, adopted and recovered starts names the
    archive of the client's current journal generation."""
    from backend.app import main
    from backend.app.services.printer_manager import printer_manager

    class Manager:
        _uses_print_file_analysis_service = False

    monkeypatch.setattr(main, "printer_manager", Manager())
    monkeypatch.setattr(main.app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(printer_manager, "get_client", lambda pid: a_client_on_print(5) if pid == 7 else None)

    main._bind_print_file_analysis_context(7, SimpleNamespace(id=42, file_path=None, plate_index=1, subtask_id=None))

    assert journal._tail_owners[7] == (5, 42)


async def test_the_current_tail_lands_in_the_archive_its_print_was_bound_to(db_session, printer_factory):
    """F-R2: the owner, not the newest printing row, decides — a stray newer printing row
    does not take print 8's tail."""
    printer, own = await a_print(db_session, printer_factory)
    stray = PrintArchive(
        printer_id=printer.id, filename="stray.3mf", print_name="stray", file_path="", file_size=1, status="printing"
    )
    db_session.add(stray)
    await db_session.flush()
    stray_id = stray.id
    await db_session.commit()
    journal.bind_tail_owner(printer.id, own.id, 8)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = a_client_on_print(8)
        assert await tail_fact(db_session, printer, "tail_start", 40, generation=8)
    assert [r[0] for r in await rows(db_session, printer.id, own.id)] == [EVENT_TAIL_START]
    assert await rows(db_session, printer.id, stray_id) == []


# ── F-R3 · runouts and assignments inside the tail ───────────────────────────


async def test_the_late_runout_of_the_tail_is_the_old_reels(db_session, incident):
    """F01: the runout pause arrives after 313 was bound — it is still 309's tube running dry."""
    printer, archive, old, new = incident
    await tail_fact(db_session, printer, "tail_start", 40)
    await bind(db_session, printer.id, new.id)
    async with journal.journal_lock(printer.id):
        result = await journal.record_runout(
            db_session, printer_id=printer.id, archive_id=archive.id, layer_num=42, kind=KIND_PAUSE,
            global_tray_id=TRAY, spool_id=new.id,
        )  # fmt: skip
    assert result is RunoutResult.NEW
    assert (await rows(db_session, printer.id, archive.id))[-1] == (EVENT_RUNOUT, 42, old.id, KIND_PAUSE)


async def test_after_the_return_a_runout_is_the_new_reels_and_opens_its_own_episode(db_session, incident):
    """F06: the new reel really ran out — its runout is neither folded nor renamed."""
    printer, archive, old, new = incident
    await tail_fact(db_session, printer, "tail_start", 40)
    async with journal.journal_lock(printer.id):
        await journal.record_runout(
            db_session, printer_id=printer.id, archive_id=archive.id, layer_num=42, kind=KIND_PAUSE,
            global_tray_id=TRAY, spool_id=old.id,
        )  # fmt: skip
    await bind(db_session, printer.id, new.id)
    await tail_fact(db_session, printer, "feed_return", 42)
    async with journal.journal_lock(printer.id):
        result = await journal.record_runout(
            db_session, printer_id=printer.id, archive_id=archive.id, layer_num=80, kind=KIND_PAUSE,
            global_tray_id=TRAY, spool_id=new.id,
        )  # fmt: skip
    assert result is RunoutResult.NEW
    assert (await rows(db_session, printer.id, archive.id))[-1] == (EVENT_RUNOUT, 80, new.id, KIND_PAUSE)


async def test_an_assignment_during_the_tail_is_an_identity_note_corrected_by_the_next(db_session, incident):
    """F02/F07: no runout yet — the assignment still names the reel now in the slot; a later
    one corrects that note instead of adding a second; re-linking the old reel is nothing."""
    printer, archive, old, new = incident
    await tail_fact(db_session, printer, "tail_start", 40)
    other = await a_spool(db_session, "other")

    async with journal.journal_lock(printer.id):
        await journal.note_assignment_change(
            db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, spool_id=old.id, layer_num=40
        )
        await journal.note_assignment_change(
            db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, spool_id=other.id, layer_num=41
        )
        await journal.note_assignment_change(
            db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, spool_id=new.id, layer_num=41
        )
    assert [r for r in await rows(db_session, printer.id, archive.id) if r[0] == EVENT_SPOOL_LOADED] == [
        (EVENT_SPOOL_LOADED, 41, new.id, None)
    ]


async def test_a_correction_without_a_tail_or_a_runout_journals_no_replacement(db_session, incident):
    """F03: an RFID match or a fixed wrong link is not a physical replacement."""
    printer, archive, old, new = incident
    async with journal.journal_lock(printer.id):
        await journal.note_assignment_change(
            db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, spool_id=new.id, layer_num=30
        )
    assert await rows(db_session, printer.id, archive.id) == [(EVENT_START, 0, old.id, None)]


async def test_a_tail_without_a_pause_offers_no_running_replacement_prompt(db_session, incident):
    """F10 / F-R6: no manual running-tail question — the window stays shut while RUNNING."""
    printer, archive, old, new = incident
    await tail_fact(db_session, printer, "tail_start", 40)
    state = MagicMock(state="RUNNING", layer_num=41)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_status.return_value = state
        assert await journal.manual_replacement_window(db_session, printer.id, ams_id=AMS, tray_id=SLOT) is None


# ── F09 · the assignment writes the open tail first, atomically ──────────────


def latched(printer_manager_mock, origin=TRAY, layer=40):
    client = MagicMock()
    client.open_tail.return_value = (origin, layer)
    printer_manager_mock.get_client.return_value = client


async def test_an_assignment_racing_the_queued_tail_callback_keeps_the_old_reel_on_the_tail(db_session, incident):
    """F09: tail observed → callback queued → the operator binds 313 first. The assignment
    records the tail with 309 before it rewrites the binding; the callback then finds the
    episode open and writes nothing."""
    printer, archive, old, new = incident
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        latched(pm)
        async with journal.journal_lock(printer.id):
            assert await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
            await bind(db_session, printer.id, new.id)  # commits the tail_start with the binding
        assert not await tail_fact(db_session, printer, "tail_start", 40)

    assert [r for r in await rows(db_session, printer.id, archive.id) if r[0] == EVENT_TAIL_START] == [
        (EVENT_TAIL_START, 40, old.id, None)
    ]


async def test_a_failed_binding_write_takes_its_tail_start_with_it(db_session, incident):
    """F09: the tail_start and the binding commit together or not at all."""
    printer, archive, old, new = incident
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        latched(pm)
        async with journal.journal_lock(printer.id):
            assert await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
            await db_session.rollback()
    assert await rows(db_session, printer.id, archive.id) == [(EVENT_START, 0, old.id, None)]


async def test_another_slot_or_no_tail_writes_nothing(db_session, incident):
    printer, archive, old, new = incident
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        latched(pm, origin=TRAY + 1)
        async with journal.journal_lock(printer.id):
            assert not await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
        pm.get_client.return_value.open_tail.return_value = None
        async with journal.journal_lock(printer.id):
            assert not await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)


async def test_the_internal_assignment_route_records_the_tail_with_the_reel_going_out(
    async_client, db_session, incident
):
    """F09 through the real route: POST /inventory/assignments during an observed tail."""
    printer, archive, old, new = incident
    status = MagicMock(state="RUNNING", layer_num=41)
    status.raw_data = {"ams": {"ams": [{"id": AMS, "tray": [{"id": SLOT, "tray_type": "PLA"}]}]}}
    status.nozzles = [MagicMock(nozzle_diameter="0.4")]
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        latched(pm)
        pm.get_status.return_value = status
        pm.ensure_fresh_connection = AsyncMock(return_value=True)
        response = await async_client.post(
            "/api/v1/inventory/assignments",
            json={"spool_id": new.id, "printer_id": printer.id, "ams_id": AMS, "tray_id": SLOT},
        )
    assert response.status_code == 200, response.text
    assert [r for r in await rows(db_session, printer.id, archive.id) if r[0] != EVENT_START] == [
        (EVENT_TAIL_START, 40, old.id, None),
        (EVENT_SPOOL_LOADED, 41, new.id, None),
    ]


async def test_the_spoolman_assignment_waits_for_the_slot_lock_but_never_holds_it_over_the_network(
    async_client, db_session, printer_factory
):
    """F09 / F-R3: the remote spool lookup runs outside the assignment lock, the binding write
    inside it — a slow Spoolman blocks neither a runout callback nor another assignment."""
    from backend.app.main import _get_ams_assignment_lock
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    printer = await printer_factory()
    reached, release = asyncio.Event(), asyncio.Event()

    async def slow_get_spool(spool_id):
        reached.set()
        await release.wait()
        return {
            "id": spool_id,
            "remaining_weight": 800.0,
            "used_weight": 200.0,
            "registered": "2026-09-01T10:00:00",
            "filament": {"id": 5, "name": "PLA", "material": "PLA", "color_hex": "FF0000", "weight": 1000,
                         "spool_weight": 250, "vendor": {"id": 1, "name": "Bambu Lab"}},
            "extra": {},
        }  # fmt: skip

    spoolman = MagicMock(get_spool=slow_get_spool, get_spools=AsyncMock(return_value=[]))
    lock = _get_ams_assignment_lock(printer.id)
    with (
        patch("backend.app.api.routes.spoolman_inventory._get_client", new=AsyncMock(return_value=spoolman)),
        patch("backend.app.api.routes.spoolman_inventory.printer_manager") as route_pm,
        patch("backend.app.core.websocket.ws_manager.broadcast", new_callable=AsyncMock),
    ):
        route_pm.get_client.return_value = None
        request = asyncio.create_task(
            async_client.post(
                "/api/v1/spoolman/inventory/slot-assignments",
                json={"spoolman_spool_id": 12, "printer_id": printer.id, "ams_id": AMS, "tray_id": SLOT},
            )
        )
        await asyncio.wait_for(reached.wait(), 5)
        assert not lock.locked()  # the network wait holds no lock
        async with lock:
            release.set()
            # The route now either finishes (it took no lock) or queues on ours.
            for _ in range(500):
                if request.done() or lock._waiters:
                    break
                await asyncio.sleep(0.01)
            assert not request.done(), "the binding was written without the slot's assignment lock"
            db_session.expire_all()
            assert await db_session.scalar(select(SpoolmanSlotAssignment.spoolman_spool_id)) is None
        response = await asyncio.wait_for(request, 10)
    assert response.status_code == 200, response.text
    db_session.expire_all()
    assert await db_session.scalar(select(SpoolmanSlotAssignment.spoolman_spool_id)) == 12


# ── F04 · the seed of a joined print ────────────────────────────────────────


async def test_the_restart_seed_gives_the_joined_client_its_open_tail_back(db_session, incident):
    """F04: the journal holds an open tail; the restarted client's first report is the origin
    — the return happened while BamDude was down, so it is a gap return."""
    import logging

    from backend.app.main import _restore_usage_tracking_session
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    printer, archive, old, new = incident
    await tail_fact(db_session, printer, "tail_start", 40)
    client = BambuMQTTClient(ip_address="1.2.3.4", serial_number="SEED1", access_code="1")
    client.on_feed_event = MagicMock()
    client.on_print_running_observed = MagicMock()
    client._previous_gcode_state = "RUNNING"
    client._previous_gcode_file = "plate.3mf"
    client.state.raw_data = {"ams": {"ams": [{"id": AMS, "tray": [{"id": SLOT, "tray_type": "PLA"}]}]}}
    client._process_message(
        {"print": {"ams": {"tray_now": str(TRAY)}, "gcode_state": "RUNNING", "gcode_file": "plate.3mf",
                   "layer_num": 50, "total_layer_num": 200, "subtask_name": "plate"}}
    )  # fmt: skip
    generation = client.on_print_running_observed.call_args.args[0]["_journal_generation"]

    with patch("backend.app.main.printer_manager") as pm:
        pm.get_client.return_value = client
        await _restore_usage_tracking_session(
            printer.id, client.state, db_session, logging.getLogger(__name__), generation=generation
        )

    assert (EVENT_FEED_RETURN, 50, old.id, KIND_GAP) in await rows(db_session, printer.id, archive.id)
    assert client.state.last_loaded_tray == TRAY


def a_restarted_client_seeing_the_origin(layer=50):
    """The restarted client's first status: a print running, the feed already back on the origin."""
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient(ip_address="1.2.3.4", serial_number="SEED2", access_code="1")
    client.on_feed_event = MagicMock()
    client.on_tray_change = MagicMock()
    client.on_print_running_observed = MagicMock()
    client._previous_gcode_state = "RUNNING"
    client._previous_gcode_file = "plate.3mf"
    client.state.raw_data = {"ams": {"ams": [{"id": AMS, "tray": [{"id": SLOT, "tray_type": "PLA"}]}]}}
    client._process_message(
        {"print": {"ams": {"tray_now": str(TRAY)}, "gcode_state": "RUNNING", "gcode_file": "plate.3mf",
                   "layer_num": layer, "total_layer_num": 200, "subtask_name": "plate"}}
    )  # fmt: skip
    return client


@pytest.fixture
def seed_retries(monkeypatch, test_engine):
    """The retries run at once, on this database, and the test awaits the one it spawned."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app import main

    spawned = []
    monkeypatch.setattr(main, "_USAGE_SEED_RETRY_DELAYS", (0, 0, 0))
    monkeypatch.setattr(main, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))
    monkeypatch.setattr(main, "spawn_background_task", lambda coroutine, **_: spawned.append(coroutine))
    return spawned


def a_flaky_read(monkeypatch, failures):
    """The restore's first database read fails ``failures`` times (a locked database), then works."""
    from backend.app.services import usage_tracker

    real = usage_tracker.get_persisted_print_name
    calls = []

    async def read(db, printer_id):
        calls.append(printer_id)
        if len(calls) <= failures:
            raise RuntimeError("database is locked")
        return await real(db, printer_id)

    monkeypatch.setattr(usage_tracker, "get_persisted_print_name", read)
    return calls


async def test_a_failed_seed_is_retried_from_the_same_evidence(db_session, incident, monkeypatch, seed_retries):
    """M2: the restore's first read failed; the retry reads the journal and the held report
    again and seeds the print — the open tail closes at the observed return, nothing lost."""
    import logging

    from backend.app.main import _restore_usage_tracking_session

    printer, archive, old, new = incident
    await tail_fact(db_session, printer, "tail_start", 40)
    client = a_restarted_client_seeing_the_origin()
    a_flaky_read(monkeypatch, failures=1)

    with patch("backend.app.main.printer_manager") as pm:
        pm.get_client.return_value = client
        pm.get_status.return_value = client.state
        assert not await _restore_usage_tracking_session(
            printer.id, client.state, db_session, logging.getLogger(__name__), generation=client.journal_generation()
        )
        assert client.journal_held() and len(seed_retries) == 1
        await seed_retries.pop()

    assert not client.journal_held()
    assert (EVENT_FEED_RETURN, 50, old.id, KIND_GAP) in await rows(db_session, printer.id, archive.id)


async def test_a_seed_that_keeps_failing_ends_the_hold_with_the_feed_unknown(
    db_session, incident, monkeypatch, seed_retries
):
    """M2: every read fails — the hold ends visibly: the feed is unknown until the held
    report, which is journaled, instead of the completion reading the old chronology."""
    import logging

    from backend.app.main import _restore_usage_tracking_session
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    client = a_restarted_client_seeing_the_origin()
    calls = a_flaky_read(monkeypatch, failures=100)

    with patch("backend.app.main.printer_manager") as pm:
        pm.get_client.return_value = client
        pm.get_status.return_value = client.state
        await _restore_usage_tracking_session(
            printer.id, client.state, db_session, logging.getLogger(__name__), generation=client.journal_generation()
        )
        await seed_retries.pop()

    assert len(calls) == 4  # the first try and three retries, then no more
    assert not client.journal_held()
    assert client.state.tray_change_log == [(UNKNOWN_FEED, 0), (TRAY, 50)]
    db_session.expire_all()
    assert [
        (e.global_tray_id, e.layer_num)
        for e in await journal.load_events(db_session, printer.id, archive.id)
        if e.event == EVENT_TRAY_CHANGE
    ] == [(UNKNOWN_FEED, 0), (TRAY, 50)]


async def test_a_seed_retry_leaves_a_print_that_moved_on_alone(db_session, incident, monkeypatch, seed_retries):
    import logging

    from backend.app.main import _restore_usage_tracking_session

    printer, archive, old, new = incident
    client = a_restarted_client_seeing_the_origin()
    generation = client.journal_generation()
    calls = a_flaky_read(monkeypatch, failures=100)

    with patch("backend.app.main.printer_manager") as pm:
        pm.get_client.return_value = client
        pm.get_status.return_value = client.state
        await _restore_usage_tracking_session(
            printer.id, client.state, db_session, logging.getLogger(__name__), generation=generation
        )
        client._previous_gcode_state, client._was_running = "IDLE", False
        client._process_message({"print": {"gcode_state": "RUNNING", "gcode_file": "next.3mf"}})
        await seed_retries.pop()

    assert len(calls) == 1  # the retry of the old print read nothing
    assert client.journal_generation() != generation
    assert client.journal_held()  # the next print's own hold, not ended by the old print's retry


# ── F-R3 / F-R4 · an assignment while the journal waits for its seed ─────────


def slot_reads(client, tray, tray_type):
    client.state.raw_data = {
        "ams": {"ams": [{"id": tray // 4, "tray": [{"id": tray % 4, "tray_type": tray_type, "tray_color": ""}]}]},
        "vt_tray": [{"id": 254}, {"id": 255}],
    }


def a_joined_client_feeding_a_tail(tray=TRAY, layer=40):
    """A client that joined a running print whose first report is the external sentinel with
    ``tray`` empty — a runout tail, held with the rest until the restart seed."""
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient(ip_address="1.2.3.4", serial_number="HOLD1", access_code="1")
    client.on_feed_event = MagicMock()
    client.on_print_running_observed = MagicMock()
    client._previous_gcode_state = "RUNNING"
    client._previous_gcode_file = "plate.3mf"
    slot_reads(client, tray, "")
    client._process_message(
        {"print": {"ams": {"tray_now": "254"}, "gcode_state": "RUNNING", "gcode_file": "plate.3mf",
                   "layer_num": layer, "total_layer_num": 200}}
    )  # fmt: skip
    assert client.journal_held()
    return client


async def replay(db, printer_id, client):
    """What reaches the journal after the seed: the replay's batch, delivered by the next holder
    of the journal lock, then main's queued live feed callbacks in the order the client emitted
    them, then forgotten."""
    async with journal.journal_lock(printer_id):
        pass
    for event, origin, layer, after_gap in [c.args for c in client.on_feed_event.call_args_list]:
        async with journal.journal_lock(printer_id):
            await journal.handle_feed_event(
                db, printer_id=printer_id, event=event, origin=origin, layer_num=layer, after_gap=after_gap,
                generation=client.journal_generation(),
            )  # fmt: skip
    client.on_feed_event.reset_mock()


async def seed_and_replay(db, printer_id, client, tray=TRAY):
    assert client.seed_usage_journal(
        client.journal_generation(), log=[(tray, 0)], last_loaded_tray=tray, tail_origin=None
    )
    await replay(db, printer_id, client)


async def frozen(db, printer_id, archive_id, event):
    db.expire_all()
    return [
        (e.layer_num, e.spool_id, e.spoolman_spool_id)
        for e in await journal.load_events(db, printer_id, archive_id)
        if e.event == event
    ]


async def test_an_assignment_while_the_journal_waits_for_its_seed_keeps_the_old_reel_on_the_tail(db_session, incident):
    """F-R3: the tail was seen before the seed, so the assignment could not record it; the
    replay freezes the reel going out at that assignment, never the replacement."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        async with journal.journal_lock(printer.id):
            assert not await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
            await bind(db_session, printer.id, new.id)
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]


async def test_after_such_an_assignment_the_late_runout_stays_old_and_the_return_splits_the_tray(db_session, incident):
    """F-R3 / F-R4: the tube's late runout is the old reel's, the feed back names the new
    reel, and the completion boundaries split the tray at the return."""
    from backend.app.services.usage_tracker import journal_boundaries_for_tray

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
            await bind(db_session, printer.id, new.id)
        await seed_and_replay(db_session, printer.id, client)
        async with journal.journal_lock(printer.id):
            ids = await journal.freeze_spool_ids(db_session, printer.id, TRAY)
            await journal.record_runout(
                db_session, printer_id=printer.id, archive_id=archive.id, layer_num=42, kind=KIND_PAUSE,
                global_tray_id=TRAY, spool_id=ids[0], spoolman_spool_id=ids[1],
            )  # fmt: skip
        slot_reads(client, TRAY, "PLA")
        client._process_message({"print": {"ams": {"tray_now": str(TRAY)}, "layer_num": 45}})
        await replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_RUNOUT) == [(42, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, new.id, None)]
    events = await journal.load_events(db_session, printer.id, archive.id)
    assert [(layer, spool) for layer, spool, _ in journal_boundaries_for_tray(events, TRAY)] == [
        (0, old.id),
        (45, new.id),
    ]


async def test_an_unknown_reel_going_out_stays_unknown_never_the_new_one(db_session, printer_factory):
    """F-R3: nothing was bound to the slot when the assignment came — the tail's reel is not
    proven, and the replacement is not made up as its owner."""
    printer, archive = await a_print(db_session, printer_factory)
    new = await a_spool(db_session, "new")
    client = a_joined_client_feeding_a_tail()
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
            await bind(db_session, printer.id, new.id)
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, None, None)]


async def test_an_assignment_after_every_held_report_names_no_earlier_interval(db_session, incident):
    """Review r2 F2-R3: two tails and a return were held, the reel assigned only after all of
    them. Each was seen while the binding still named the old reel, so each freezes it — the
    later binding is never put on an earlier interval; the assignment itself is journaled in
    its place, after them, as the identity note it is (inside the open tail: no boundary)."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_report(client, TRAY, 45, "PLA")
    a_report(client, 254, 60, "")
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, new.id))
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [
        (40, old.id, None),
        (60, old.id, None),
    ]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_SPOOL_LOADED) == [(60, new.id, None)]


async def test_a_replay_whose_first_write_failed_names_every_tail_with_its_own_reel(db_session, incident, monkeypatch):
    """The replay's first write failed and rolled back whole (F3). The feed came back, the new
    reel was assigned, and it ran out again — written later by the next holder of the journal
    lock, the first tail and the return are still the old reel's and the later tail the new
    reel's: a failed write spends no name."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_report(client, TRAY, 45, "PLA")
    real, calls = journal._write_replay_fact, []

    async def first_write_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return await real(*args, **kwargs)

    monkeypatch.setattr(journal, "_write_replay_fact", first_write_fails)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, new.id))
        a_report(client, 254, 60, "")
        assert client.seed_usage_journal(
            client.journal_generation(), log=[(TRAY, 0)], last_loaded_tray=TRAY, tail_origin=None
        )
        await replay(db_session, printer.id, client)  # the write fails, the batch waits
        assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == []
        await replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [
        (40, old.id, None),
        (60, new.id, None),
    ]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, old.id, None)]


async def test_a_seed_that_comes_late_journals_every_held_transition_at_its_own_layer(db_session, incident):
    """M2: a long wait for the seed — three tails and their returns held while the print ran
    on; the late seed journals them in order, each at the layer its report carried, and the
    completion boundaries split the tray at the returns."""
    from backend.app.services.usage_tracker import journal_boundaries_for_tray

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail(layer=40)
    for back, out in ((45, 60), (66, 80), (85, 99)):
        slot_reads(client, TRAY, "PLA")
        client._process_message({"print": {"ams": {"tray_now": str(TRAY)}, "layer_num": back}})
        slot_reads(client, TRAY, "")
        client._process_message({"print": {"ams": {"tray_now": "254"}, "layer_num": out}})
    client.state.layer_num = 120  # the seed lands long after
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        await seed_and_replay(db_session, printer.id, client)

    tails = await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START)
    returns = await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN)
    assert [t[0] for t in tails] == [40, 60, 80, 99]
    assert [r[0] for r in returns] == [45, 66, 85]
    events = await journal.load_events(db_session, printer.id, archive.id)
    assert [layer for layer, _spool, _ in journal_boundaries_for_tray(events, TRAY)] == [0, 45, 66, 85]


async def test_an_assignment_held_without_a_tail_leaves_a_later_live_tail_to_the_binding(db_session, incident):
    """F03: the slot was only re-linked while the journal waited; the reel that runs out
    after the seed is the one bound then."""
    printer, archive, old, new = incident
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient(ip_address="1.2.3.4", serial_number="HOLD2", access_code="1")
    client.on_feed_event = MagicMock()
    client.on_print_running_observed = MagicMock()
    client._previous_gcode_state = "RUNNING"
    client._previous_gcode_file = "plate.3mf"
    slot_reads(client, TRAY, "PLA")
    client._process_message(
        {"print": {"ams": {"tray_now": str(TRAY)}, "gcode_state": "RUNNING", "gcode_file": "plate.3mf",
                   "layer_num": 40, "total_layer_num": 200}}
    )  # fmt: skip
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
            await bind(db_session, printer.id, new.id)
        await seed_and_replay(db_session, printer.id, client)
        slot_reads(client, TRAY, "")
        client._process_message({"print": {"ams": {"tray_now": "254"}, "layer_num": 80}})
        await replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(80, new.id, None)]


async def test_a_tail_held_after_the_held_assignment_is_the_new_reels(db_session, incident):
    """Review r1: the reel assigned while the journal waited runs out itself before the seed —
    its tail (seen after the assignment) is the new reel's, not the one the assignment replaced."""
    printer, archive, old, new = incident
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient(ip_address="1.2.3.4", serial_number="HOLD3", access_code="1")
    client.on_feed_event = MagicMock()
    client.on_print_running_observed = MagicMock()
    client._previous_gcode_state = "RUNNING"
    client._previous_gcode_file = "plate.3mf"
    slot_reads(client, TRAY, "PLA")
    client._process_message(
        {"print": {"ams": {"tray_now": str(TRAY)}, "gcode_state": "RUNNING", "gcode_file": "plate.3mf",
                   "layer_num": 40, "total_layer_num": 200}}
    )  # fmt: skip
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
            await bind(db_session, printer.id, new.id)
        slot_reads(client, TRAY, "")
        client._process_message({"print": {"ams": {"tray_now": "254"}, "layer_num": 60}})
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(60, new.id, None)]


async def test_a_runout_after_an_unknown_feed_is_not_handed_the_stale_tails_reel(db_session, incident):
    """Review r1 #2: the latch was dropped for lost reports; the unknown-feed row closes the
    journal's open episode, so a later runout of that tray keeps the reel bound to it."""
    from backend.app.services.usage_tracker import record_tray_change_event
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    await tail_fact(db_session, printer, "tail_start", 40)
    async with journal.journal_lock(printer.id):
        await record_tray_change_event(db_session, printer.id, UNKNOWN_FEED, 50)
    await bind(db_session, printer.id, new.id)
    async with journal.journal_lock(printer.id):
        await journal.record_runout(
            db_session, printer_id=printer.id, archive_id=archive.id, layer_num=90, kind=KIND_PAUSE,
            global_tray_id=TRAY, spool_id=new.id, spoolman_spool_id=None,
        )  # fmt: skip

    assert await frozen(db_session, printer.id, archive.id, EVENT_RUNOUT) == [(90, new.id, None)]


async def test_a_print_that_completes_before_its_seed_states_the_feed_unknown(monkeypatch):
    """Review r1 #1: the completion runs before anything else reads the chronology, and a
    journal still waiting for its seed is released first — the completion never reads the old
    or empty chronology as if it were the print's."""
    from backend.app import main

    order = []
    client = MagicMock()
    client.journal_held.return_value = True
    client.journal_generation.return_value = 4
    client.abandon_journal_hold.side_effect = lambda generation, **_: order.append(("release", generation)) or True

    async def completion(printer_id, data, analysis):
        order.append(("completion", printer_id))

    monkeypatch.setattr(main.printer_manager, "get_client", lambda _pid: client)
    monkeypatch.setattr(main, "_on_print_complete_impl", completion)

    await main.on_print_complete(7, {})

    assert order == [("release", 4), ("completion", 7)]


async def test_the_internal_assignment_route_during_the_seed_wait_keeps_the_old_reel_on_the_tail(
    async_client, db_session, incident
):
    """F09 through the real route (manual inventory) while the journal waits for its seed."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    status = MagicMock(state="RUNNING", layer_num=40)
    status.raw_data = {"ams": {"ams": [{"id": AMS, "tray": [{"id": SLOT, "tray_type": "PLA"}]}]}}
    status.nozzles = [MagicMock(nozzle_diameter="0.4")]
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        pm.get_status.return_value = status
        pm.ensure_fresh_connection = AsyncMock(return_value=True)
        response = await async_client.post(
            "/api/v1/inventory/assignments",
            json={"spool_id": new.id, "printer_id": printer.id, "ams_id": AMS, "tray_id": SLOT},
        )
        assert response.status_code == 200, response.text
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]


async def test_the_spoolman_assignment_during_the_seed_wait_keeps_the_old_reel_on_the_tail(
    async_client, db_session, printer_factory
):
    """F09 through the Spoolman route while the journal waits for its seed."""
    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    printer, archive = await a_print(db_session, printer_factory)
    db_session.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=AMS, tray_id=SLOT, spoolman_spool_id=11))
    await db_session.commit()
    spoolman = MagicMock(
        get_spool=AsyncMock(
            return_value={
                "id": 12, "remaining_weight": 800.0, "used_weight": 200.0, "registered": "2026-09-01T10:00:00",
                "filament": {"id": 5, "name": "PLA", "material": "PLA", "color_hex": "FF0000", "weight": 1000,
                             "spool_weight": 250, "vendor": {"id": 1, "name": "Bambu Lab"}},
                "extra": {},
            }
        ),
        get_spools=AsyncMock(return_value=[]),
    )  # fmt: skip
    client = a_joined_client_feeding_a_tail()
    with (
        patch("backend.app.api.routes.spoolman_inventory._get_client", new=AsyncMock(return_value=spoolman)),
        patch("backend.app.api.routes.spoolman_inventory.printer_manager") as route_pm,
        patch("backend.app.services.printer_manager.printer_manager") as pm,
        patch("backend.app.core.websocket.ws_manager.broadcast", new_callable=AsyncMock),
    ):
        route_pm.get_client.return_value = None
        pm.get_client.return_value = client
        response = await async_client.post(
            "/api/v1/spoolman/inventory/slot-assignments",
            json={"spoolman_spool_id": 12, "printer_id": printer.id, "ams_id": AMS, "tray_id": SLOT},
        )
        assert response.status_code == 200, response.text
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, None, 11)]


async def test_the_rfid_assignment_during_the_seed_wait_keeps_the_old_reel_on_the_tail(db_session, incident):
    """F09 through the RFID writer while the journal waits for its seed."""
    from backend.app.services.spool_tag_matcher import auto_assign_spool, create_spool_from_tray
    from backend.tests.unit.services.test_spool_tag_matcher import SAMPLE_TRAY

    printer, archive, old, _new = incident
    tagged = await create_spool_from_tray(db_session, SAMPLE_TRAY)
    client = a_joined_client_feeding_a_tail()
    writer_pm = MagicMock()
    writer_pm.get_status.return_value = None
    writer_pm.get_client.return_value = None
    writer_pm.ensure_fresh_connection = AsyncMock(return_value=True)
    writer_pm.ensure_fresh_connection_for_printer = AsyncMock(return_value=True)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        assignment = await auto_assign_spool(printer.id, AMS, SLOT, tagged, writer_pm, db_session)
        assert assignment.spool_id == tagged.id
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]


async def test_a_stock_insertion_during_the_seed_wait_keeps_the_old_reel_on_the_tail(
    db_session, printer_factory, tmp_path
):
    """F09 through the stock-insertion writer while the journal waits for its seed: the pause
    runout of the old reel was journaled before the restart, the first report after it is
    the tail."""
    from backend.app.services.auto_stock_spool import claim_on_insertion
    from backend.tests.unit.services.test_auto_stock_spool import event, manager, printer as stock_printer, spool
    from backend.tests.unit.services.test_usage_tracker_runout import _journal, _make_archive

    p = await stock_printer(printer_factory)
    archive = await _make_archive(db_session, p, tmp_path)
    old = await spool(db_session, weight_used=700)
    await spool(db_session)
    db_session.add(SpoolAssignment(printer_id=p.id, ams_id=0, tray_id=0, spool_id=old.id, fingerprint_type="PLA"))
    await db_session.commit()
    p_id, archive_id, old_id = p.id, archive.id, old.id
    await _journal(db_session, p, archive, [(EVENT_RUNOUT, KIND_PAUSE, 0, 140, old.id)])
    client = a_joined_client_feeding_a_tail(tray=0, layer=141)
    pm, _, _ = manager()
    with patch("backend.app.services.printer_manager.printer_manager") as live:
        live.get_client.return_value = client
        outcome = await claim_on_insertion(db_session, printer_id=p_id, event=event(), manager=pm)
        assert outcome["reason"] == "assigned"
        await seed_and_replay(db_session, p_id, client, tray=0)

    assert await frozen(db_session, p_id, archive_id, EVENT_TAIL_START) == [(141, old_id, None)]


async def test_a_stock_insertion_during_the_tail_records_the_tail_with_the_reel_going_out(
    db_session, printer_factory, tmp_path
):
    """F09: the insertion writer is an assignment writer too — the tail first, then the binding."""
    from backend.app.models.print_usage_event import KIND_PAUSE as PAUSE
    from backend.app.services.auto_stock_spool import claim_on_insertion
    from backend.tests.unit.services.test_auto_stock_spool import event, manager, printer as stock_printer, spool
    from backend.tests.unit.services.test_usage_tracker_runout import _journal, _make_archive

    p = await stock_printer(printer_factory)
    archive = await _make_archive(db_session, p, tmp_path)
    old = await spool(db_session, weight_used=700)
    await spool(db_session)
    db_session.add(SpoolAssignment(printer_id=p.id, ams_id=0, tray_id=0, spool_id=old.id, fingerprint_type="PLA"))
    await db_session.commit()
    p_id, archive_id, old_id = p.id, archive.id, old.id
    await _journal(db_session, p, archive, [(EVENT_RUNOUT, PAUSE, 0, 140, old.id)])
    pm, _, _ = manager()
    with patch("backend.app.services.printer_manager.printer_manager") as live:
        latched(live, origin=0, layer=138)
        outcome = await claim_on_insertion(db_session, printer_id=p_id, event=event(), manager=pm)
    assert outcome["reason"] == "assigned"
    assert [r for r in await rows(db_session, p_id, archive_id) if r[0] == EVENT_TAIL_START] == [
        (EVENT_TAIL_START, 138, old_id, None)
    ]


async def test_the_x2d_incident_replayed_end_to_end(db_session, incident):
    """F01: the real MQTT parser feeds the real callbacks, journal, assignment and projection —
    empty origin → tail 254 → a reel goes in (the slot reads loaded) → the operator binds 313 →
    the late pause-runout of 309's tube → the feed back on tray 2. No phantom tray change; the
    old reel to the return, the new one after it."""
    from backend.app.models.print_usage_event import EVENT_TRAY_CHANGE
    from backend.app.services.bambu_mqtt import BambuMQTTClient
    from backend.app.services.usage_tracker import journal_boundaries_for_tray, record_tray_change_event

    printer, archive, old, new = incident
    client = BambuMQTTClient(ip_address="1.2.3.4", serial_number="X2D-1006", access_code="1")
    client._was_running, client._completion_triggered = True, False
    client.state.state, client.state.layer_num, client.state.total_layers = "RUNNING", 40, 200
    client.state.tray_now = client.state.last_loaded_tray = TRAY
    client.state.tray_change_log = [(TRAY, 0)]

    def slot(tray_type):
        client.state.raw_data = {
            "ams": {"ams": [{"id": AMS, "tray": [{"id": SLOT, "tray_type": tray_type, "tray_color": ""}]}]},
            "vt_tray": [{"id": 254}, {"id": 255}],
        }

    queued = []
    client.on_feed_event = lambda *args: queued.append(("feed", args))
    client.on_usage_event = lambda *args: queued.append(("usage", args))
    client.on_tray_change = lambda *args: queued.append(("tray", args))

    async def drain():
        """What main.py's callbacks do, in the order the client queued them."""
        for kind, args in queued:
            async with journal.journal_lock(printer.id):
                if kind == "feed":
                    event, origin, layer, after_gap = args
                    await journal.handle_feed_event(
                        db_session, printer_id=printer.id, event=event, origin=origin, layer_num=layer,
                        after_gap=after_gap,
                    )  # fmt: skip
                elif kind == "tray":
                    await record_tray_change_event(db_session, printer.id, *args)
                else:
                    event, kind_, tray, layer = args
                    ids = await journal.freeze_spool_ids(db_session, printer.id, tray)
                    await journal.record_runout(
                        db_session, printer_id=printer.id, archive_id=archive.id, layer_num=layer, kind=kind_,
                        global_tray_id=tray, spool_id=ids[0], spoolman_spool_id=ids[1],
                    )  # fmt: skip
        queued.clear()

    def tray_now(tray, layer):
        client.state.layer_num = layer
        client._process_message({"print": {"ams": {"tray_now": str(tray)}}})

    slot("")
    tray_now(254, 40)
    await drain()
    slot("PLA")  # the new reel is in the slot (or our ams_filament_setting echoed)
    tray_now(254, 41)
    await drain()
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT)
            await bind(db_session, printer.id, new.id)
            await journal.note_assignment_change(
                db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, spool_id=new.id, layer_num=41
            )
    client.state.layer_num = 42
    client._process_message({"print": {"hms": [{"attr": 0x07002200, "code": 0x00020001}]}})
    await drain()
    tray_now(TRAY, 42)
    await drain()

    events = await journal.load_events(db_session, printer.id, archive.id)
    assert not [e for e in events if e.event == EVENT_TRAY_CHANGE]
    assert [(e.event, e.spool_id) for e in events if e.event in (EVENT_RUNOUT, EVENT_TAIL_START)] == [
        (EVENT_TAIL_START, old.id),
        (EVENT_RUNOUT, old.id),
    ]
    assert [(layer, spool) for layer, spool, _ in journal_boundaries_for_tray(events, TRAY)] == [
        (0, old.id),
        (42, new.id),
    ]


# ── F-R2 · every journal writer keeps the printer's order ────────────────────


async def test_the_pause_row_waits_for_the_journal_lock(db_session, test_engine, printer_factory, monkeypatch):
    """The pause row joins the journal in the order the printer reported it, like a tray
    change, a tail fact or the resume row: its writer takes the journal lock before its session."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app import main as main_mod
    from backend.app.models.print_usage_event import EVENT_PAUSE

    printer, archive = await a_print(db_session, printer_factory)
    state = MagicMock(hms_errors=[], subtask_name="plate", gcode_file="plate.3mf", layer_num=40)

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main_mod.printer_manager, "get_printer", lambda _pid: None)
    monkeypatch.setattr(main_mod, "_expected_pause_reasons", {})
    monkeypatch.setattr(main_mod.ws_manager, "send_print_paused", nothing)
    monkeypatch.setattr(main_mod.notification_service, "on_print_pause", nothing)
    monkeypatch.setattr(main_mod, "_current_tray_frozen", AsyncMock(return_value=(TRAY, None, None)))
    monkeypatch.setattr(main_mod, "async_session", async_sessionmaker(test_engine, expire_on_commit=False))

    lock = journal.journal_lock(printer.id)
    waiting = journal._journal_locks[printer.id][1]  # the asyncio lock under it
    async with lock:
        pause = asyncio.create_task(main_mod._handle_pause_edge(printer.id, state))
        for _ in range(200):
            if waiting._waiters or pause.done():
                break
            await asyncio.sleep(0.01)
        assert waiting._waiters, "the pause row was written without the journal lock"
        assert await rows(db_session, printer.id, archive.id) == []
    await pause

    assert [r[0] for r in await rows(db_session, printer.id, archive.id)] == [EVENT_PAUSE]


# ── F-R4 / M2 · an explicit unknown feed reaches the writers as not recorded ──


@pytest.mark.parametrize(
    "chronology, charged_some, unknown_some",
    [
        ("only_unknown", False, True),
        ("known_then_unknown_on_one_layer", False, True),
        ("unknown_then_known", True, True),
        ("empty", True, False),  # no evidence at all: the old ladder, as before
    ],
)
async def test_the_internal_writer_charges_no_reel_for_a_feed_nobody_saw(
    db_session, incident, monkeypatch, chronology, charged_some, unknown_some
):
    """Review r2 F2-R1: one unknown interval — alone, or left alone by normalization — goes
    through the same not-recorded attribution as an unknown interval among several, and its
    trays are closed to the later remain-delta fallback so nothing charges those grams again."""
    from backend.app.services.bambu_mqtt import BambuMQTTClient
    from backend.app.services.usage_tracker import _track_from_3mf
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    logs = {
        "only_unknown": [(UNKNOWN_FEED, 0)],
        "known_then_unknown_on_one_layer": [(TRAY, 0), (UNKNOWN_FEED, 0)],
        "unknown_then_known": [(UNKNOWN_FEED, 0), (TRAY, 100)],
        "empty": [],
    }
    client = BambuMQTTClient(ip_address="1.2.3.4", serial_number="WRITER", access_code="1")
    slot_reads(client, TRAY, "PLA")
    client.state.tray_change_log = logs[chronology]
    client.state.total_layers = 200
    pm = MagicMock()
    pm.get_status.return_value = client.state
    analysis = SimpleNamespace(
        filament_usage=[{"slot_id": 1, "used_g": 10.0}], layer_usage=None, filament_properties={}
    )
    monkeypatch.setattr("backend.app.services.usage_tracker._warn_if_low_stock", AsyncMock())
    handled, uncharged = set(), []

    result = await _track_from_3mf(
        printer_id=printer.id, archive_id=archive.id, status="completed", print_name="plate", handled_trays=handled,
        printer_manager=pm, db=db_session, ams_mapping=[TRAY], file_analysis=analysis, analysis_attempted=True,
        uncharged=uncharged, journal_events=await journal.load_events(db_session, printer.id, archive.id),
        completion=_completion_from(client.state),
    )  # fmt: skip

    charged = sum(r["weight_used"] for r in result)
    not_recorded = sum(grams for _tray, grams in uncharged)
    assert round(charged + not_recorded, 1) == 10.0
    assert (charged > 0) is charged_some and (not_recorded > 0) is unknown_some
    assert all(r["spool_id"] == old.id for r in result)
    if unknown_some:
        assert (AMS, SLOT) in handled  # the remain-delta fallback leaves this tray alone


def test_an_unknown_feed_is_named_as_unknown_in_the_not_recorded_notice():
    from backend.app.services.spool_assignment_notifications import _slot_label_from_global_tray
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    assert _slot_label_from_global_tray(UNKNOWN_FEED) == "?"


# ── F-R3 / F-R4 · several assignments while the seed waits (review r2 F2-R2, F2-R3) ──


async def bind_spoolman(db, printer_id, spoolman_id):
    from sqlalchemy import delete

    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment

    await db.execute(
        delete(SpoolmanSlotAssignment).where(
            SpoolmanSlotAssignment.printer_id == printer_id,
            SpoolmanSlotAssignment.ams_id == AMS,
            SpoolmanSlotAssignment.tray_id == SLOT,
        )
    )
    db.add(SpoolmanSlotAssignment(printer_id=printer_id, ams_id=AMS, tray_id=SLOT, spoolman_spool_id=spoolman_id))
    await db.commit()


async def held_assignment(db, printer_id, rebind):
    """An assignment writer while the journal waits: the tail check first, then the binding."""
    async with journal.journal_lock(printer_id):
        assert not await journal.ensure_tail_recorded(db, printer_id=printer_id, ams_id=AMS, tray_id=SLOT)
        await rebind()


def a_report(client, tray, layer, tray_type):
    slot_reads(client, TRAY, tray_type)
    client._process_message({"print": {"ams": {"tray_now": str(tray)}, "layer_num": layer}})


@pytest.mark.parametrize("backend", ["internal", "spoolman"])
async def test_two_assignments_while_the_seed_waits_keep_the_reel_between_them(db_session, printer_factory, backend):
    """F2-R3: A's tail at 40, B assigned, the feed back at 45, B's own tail at 60, C assigned —
    all before the seed. The return and B's tail are B's; C is never put on B's interval."""
    printer, archive = await a_print(db_session, printer_factory)
    if backend == "internal":
        reels = [(await a_spool(db_session, name)).id for name in ("A", "B", "C")]

        def rebind_to(reel):
            return lambda: bind(db_session, printer.id, reel)

        def ids(reel):
            return (reel, None)

    else:
        reels = [11, 12, 13]

        def rebind_to(reel):
            return lambda: bind_spoolman(db_session, printer.id, reel)

        def ids(reel):
            return (None, reel)

    first, second, third = reels
    await rebind_to(first)()
    client = a_joined_client_feeding_a_tail(layer=40)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        await held_assignment(db_session, printer.id, rebind_to(second))
        a_report(client, TRAY, 45, "PLA")
        a_report(client, 254, 60, "")
        await held_assignment(db_session, printer.id, rebind_to(third))
        await seed_and_replay(db_session, printer.id, client)

    tails = await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START)
    returns = await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN)
    assert tails == [(40, *ids(first)), (60, *ids(second))]
    assert returns == [(45, *ids(second))]


async def test_an_assignment_on_the_layer_of_a_report_is_ordered_by_arrival_not_by_layer(db_session, incident):
    """F2-R3: the tail at 40, the new reel assigned on layer 40, the feed back still on layer
    40 — the return came after the assignment, so it names the new reel; the tail the old one."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail(layer=40)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, new.id))
        a_report(client, TRAY, 40, "PLA")
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(40, new.id, None)]


async def test_reports_lost_to_the_bound_keep_the_reels_of_the_reports_kept(db_session, incident, monkeypatch):
    """F2-R3 with overflow: the oldest reports are lost (the feed is unknown from there). A
    sentinel seen while the feed is unknown proves no tail and names no external reel — it
    stays unknown until a tray is seen; the kept reports still carry the reel bound when
    each was seen."""
    import backend.app.services.bambu_mqtt as mqtt
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    third = await a_spool(db_session, "third")
    monkeypatch.setattr(mqtt, "_JOURNAL_PENDING_LIMIT", 3)
    client = a_joined_client_feeding_a_tail(layer=40)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        a_report(client, TRAY, 41, "PLA")
        a_report(client, 254, 42, "")
        a_report(client, TRAY, 43, "PLA")  # the report at 40 is lost from here
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, new.id))
        a_report(client, 254, 50, "")
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, third.id))
        await seed_and_replay(db_session, printer.id, client)

    assert client.state.tray_change_log == [(TRAY, 0), (UNKNOWN_FEED, 40), (TRAY, 43)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(50, new.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == []


@pytest.mark.parametrize("foreign", ["earlier_generation", "other_subtask"])
async def test_a_rejected_callback_of_another_print_changes_nothing_of_this_one(db_session, printer_factory, foreign):
    """F2-R2: a late feed_return of another print — known by its journal generation, or by a
    subtask contradicting the archive's — is refused, and leaves the reel captured for this
    print's held tail exactly as it was."""
    printer, archive = await a_print(db_session, printer_factory, subtask_id="this-print")
    old, new = await a_spool(db_session, "old"), await a_spool(db_session, "new")
    await bind(db_session, printer.id, old.id)
    client = a_joined_client_feeding_a_tail(layer=40)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, new.id))
        observed = (
            {"generation": client.journal_generation() - 1}
            if foreign == "earlier_generation"
            else {"generation": client.journal_generation(), "subtask_id": "an-older-print"}
        )
        async with journal.journal_lock(printer.id):
            assert not await journal.handle_feed_event(
                db_session, printer_id=printer.id, event="feed_return", origin=TRAY, layer_num=90, after_gap=False,
                **observed,
            )  # fmt: skip
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]


async def test_a_reel_named_for_a_held_report_survives_a_rolled_back_write(db_session, incident, monkeypatch):
    """F2-R2 under F3: the replay's facts were all written and its commit failed; the rollback
    leaves the batch to the next holder of the journal lock, which writes it with the reel the
    replay named — nothing was spent before a successful commit, nothing is written twice."""
    from backend.app.core import database

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail(layer=40)
    factory, commits = database.async_session, []

    def sessions_whose_first_commit_fails():
        session = factory()
        commit = session.commit

        async def failing_once():
            commits.append(1)
            if len(commits) == 1:
                raise RuntimeError("database is locked")
            await commit()

        session.commit = failing_once
        return session

    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, new.id))
        assert client.seed_usage_journal(
            client.journal_generation(), log=[(TRAY, 0)], last_loaded_tray=TRAY, tail_origin=None
        )
        monkeypatch.setattr(database, "async_session", sessions_whose_first_commit_fails)
        await replay(db_session, printer.id, client)
        assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == []
        await replay(db_session, printer.id, client)
        await replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]


async def test_a_held_assignment_is_journaled_in_its_place_between_the_held_reports(db_session, incident):
    """Review r2 F2-R3: the feed came back (still the old reel's name), the new reel was
    assigned, then it ran out — the replay journals the assignment between the return and the
    next tail, as it happened, so the reel named after the return is adopted from the return's
    layer and the later tail is the new reel's."""
    from backend.app.services.usage_tracker import journal_boundaries_for_tray

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_report(client, TRAY, 45, "PLA")
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, new.id))
        a_report(client, 254, 60, "")
        await seed_and_replay(db_session, printer.id, client)

    db_session.expire_all()
    events = await journal.load_events(db_session, printer.id, archive.id)
    assert [(e.event, e.spool_id) for e in events if e.event != EVENT_START] == [
        (EVENT_TAIL_START, old.id),
        (EVENT_FEED_RETURN, old.id),
        (EVENT_SPOOL_LOADED, new.id),
        (EVENT_TAIL_START, new.id),
    ]
    assert [(layer, spool) for layer, spool, _ in journal_boundaries_for_tray(events, TRAY)][:2] == [
        (0, old.id),
        (45, new.id),
    ]


async def test_an_assignment_held_before_any_layer_takes_the_next_layer_seen_never_a_later_one(
    db_session, printer_factory
):
    """F2-R3: the client joined before the printer reported any layer; the reel was assigned
    then, before the feed came back at 45 and ran out again at 60. The assignment's layer is
    the next one a held report observed (45), as a report's own would be — never the layer the
    seed happens to find (60), which would put it after the facts that followed it."""
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    printer, archive = await a_print(db_session, printer_factory)
    old, new = await a_spool(db_session, "old"), await a_spool(db_session, "new")
    await bind(db_session, printer.id, old.id)
    client = BambuMQTTClient(ip_address="1.2.3.4", serial_number="HOLD1", access_code="1")
    client.on_feed_event = MagicMock()
    client.on_print_running_observed = MagicMock()
    client._previous_gcode_state = "RUNNING"
    client._previous_gcode_file = "plate.3mf"
    slot_reads(client, TRAY, "")
    client._process_message(
        {"print": {"ams": {"tray_now": "254"}, "gcode_state": "RUNNING", "gcode_file": "plate.3mf"}}
    )
    assert client.journal_held()
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, new.id))
        a_report(client, TRAY, 45, "PLA")
        a_report(client, 254, 60, "")
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_SPOOL_LOADED) == [(45, new.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [
        (45, old.id, None),
        (60, new.id, None),
    ]


async def test_two_assignments_of_one_tray_on_one_layer_leave_the_later_reel(db_session, printer_factory):
    """F2-R3, one layer: B and then C were assigned on layer 40 with no report between them.
    At the layer resolution the journal keeps, B fed nothing; the tail before them is A's, the
    reel named after them is C, and the next tail is C's."""
    printer, archive = await a_print(db_session, printer_factory)
    first, second, third = [(await a_spool(db_session, name)).id for name in ("A", "B", "C")]
    await bind(db_session, printer.id, first)
    client = a_joined_client_feeding_a_tail(layer=40)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, second))
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, third))
        a_report(client, TRAY, 45, "PLA")
        a_report(client, 254, 60, "")
        await seed_and_replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, first, None), (60, third, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, third, None)]
    assert [spool for _layer, spool, _sm in await frozen(db_session, printer.id, archive.id, EVENT_SPOOL_LOADED)] == [
        third
    ]


def queue_every_callback(client):
    """main's journal callbacks, queued in the order the client calls them."""
    queued = []
    client.on_feed_event = lambda *args: queued.append(("feed", args))
    client.on_tray_change = lambda *args: queued.append(("tray", args))
    return queued


async def drain_every_callback(db, printer_id, client, queued):
    """The replay's batch (written by the next holder of the journal lock), then main's live
    callbacks in the order the client queued them."""
    from backend.app.services.usage_tracker import record_tray_change_event

    async with journal.journal_lock(printer_id):
        pass
    for kind, args in queued:
        async with journal.journal_lock(printer_id):
            if kind == "feed":
                event, origin, layer, after_gap = args
                await journal.handle_feed_event(
                    db, printer_id=printer_id, event=event, origin=origin, layer_num=layer, after_gap=after_gap,
                    generation=client.journal_generation(),
                )  # fmt: skip
            else:
                await record_tray_change_event(db, printer_id, *args)
    queued.clear()


def a_joined_client_on_tray(tray, layer):
    """A client that joined a running print whose first report is ``tray`` feeding at ``layer``."""
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    client = BambuMQTTClient(ip_address="1.2.3.4", serial_number="HOLD1", access_code="1")
    client.on_print_running_observed = MagicMock()
    client._previous_gcode_state = "RUNNING"
    client._previous_gcode_file = "plate.3mf"
    slot_reads(client, TRAY, "PLA")
    client._process_message(
        {"print": {"ams": {"tray_now": str(tray)}, "gcode_state": "RUNNING", "gcode_file": "plate.3mf",
                   "layer_num": layer, "total_layer_num": 200}}
    )  # fmt: skip
    assert client.journal_held()
    return client


async def test_a_held_tray_change_keeps_the_reel_bound_when_it_was_seen(db_session, incident):
    """Review r2 F2-R3 through the ordinary tray change: the print moved from tray 1 to TRAY at
    40 and TRAY ran dry at 60, both while the journal waited; the new reel was assigned after
    that. The tray change at 40 is the old reel's — the reel the slot held when it was seen —
    and the writers charge 40→60 to it, never to the reel assigned later, which never fed."""
    from backend.app.models.print_usage_event import EVENT_TRAY_CHANGE
    from backend.app.utils.usage_attribution import feed_intervals

    printer, archive, old, new = incident
    client = a_joined_client_on_tray(1, 30)
    queued = queue_every_callback(client)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        a_report(client, TRAY, 40, "PLA")
        a_report(client, 254, 60, "")
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, new.id))
        assert client.seed_usage_journal(
            client.journal_generation(), log=[(1, 0)], last_loaded_tray=1, tail_origin=None
        )
        await drain_every_callback(db_session, printer.id, client, queued)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TRAY_CHANGE) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(60, old.id, None)]
    events = await journal.load_events(db_session, printer.id, archive.id)
    stretch = next(iv for iv in feed_intervals(client.state.tray_change_log, events) if iv.start_layer == 40)
    assert (stretch.tray, stretch.spool_id) == (TRAY, old.id)


async def test_tray_changes_kept_after_an_overflow_keep_the_reel_bound_when_each_was_seen(
    db_session, incident, monkeypatch
):
    """F2-R3 with overflow, through the ordinary tray change: a report kept after the unknown
    stretch is a plain tray change — the one at 43 is the old reel's, as it was bound then,
    never the reel assigned last."""
    import backend.app.services.bambu_mqtt as mqtt
    from backend.app.models.print_usage_event import EVENT_TRAY_CHANGE
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    third = await a_spool(db_session, "third")
    monkeypatch.setattr(mqtt, "_JOURNAL_PENDING_LIMIT", 3)
    client = a_joined_client_feeding_a_tail(layer=40)
    queued = queue_every_callback(client)
    with patch("backend.app.services.printer_manager.printer_manager") as pm:
        pm.get_client.return_value = client
        a_report(client, TRAY, 41, "PLA")
        a_report(client, 254, 42, "")
        a_report(client, TRAY, 43, "PLA")
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, new.id))
        a_report(client, 254, 50, "")
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, third.id))
        assert client.seed_usage_journal(
            client.journal_generation(), log=[(TRAY, 0)], last_loaded_tray=TRAY, tail_origin=None
        )
        await drain_every_callback(db_session, printer.id, client, queued)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TRAY_CHANGE) == [
        (40, None, None),
        (43, old.id, None),
    ]
    rows = await journal.load_events(db_session, printer.id, archive.id)
    assert [e.global_tray_id for e in rows if e.event == EVENT_TRAY_CHANGE] == [UNKNOWN_FEED, TRAY]


# ── F3-R1 · the replay reaches the journal as one ordered batch (review r3) ──


@contextmanager
def every_manager_names(client):
    """main's and the services' printer manager, both naming ``client``."""
    with ExitStack() as stack:
        for target in ("backend.app.main.printer_manager", "backend.app.services.printer_manager.printer_manager"):
            pm = stack.enter_context(patch(target))
            pm.get_client.return_value = client
            pm.get_status.return_value = client.state if client is not None else None
        yield


async def seed_through_main(printer_id, client):
    """The production seed: main's restore, on a session of its own."""
    import logging

    from backend.app.core import database
    from backend.app.main import _restore_usage_tracking_session

    async with database.async_session() as seed_db:
        return await _restore_usage_tracking_session(
            printer_id, client.state, seed_db, logging.getLogger(__name__), generation=client.journal_generation()
        )


def a_held_tail_and_return(client):
    """The joined print's chronology says TRAY; the tail at 40 is held, the return at 45 too."""
    client.state.tray_change_log = [(TRAY, 0)]
    a_report(client, TRAY, 45, "PLA")


def gate_on(monkeypatch, name):
    """Pause ``journal.<name>`` on its next call until the returned ``release`` is set."""
    real = getattr(journal, name)
    reached, release = asyncio.Event(), asyncio.Event()

    async def gated(*args, **kwargs):
        if not release.is_set():
            reached.set()
            await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(journal, name, gated)
    return reached, release


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def queued_on_the_lock(printer_id, task):
    """Wait until ``task`` waits for the printer's journal lock; fail if it finishes instead."""
    lock = journal._journal_locks[printer_id][1]
    for _ in range(300):
        if lock._waiters or task.done():
            break
        await asyncio.sleep(0.01)
    assert lock._waiters and not task.done(), "it ran without waiting for the journal lock"


async def test_the_seed_waits_for_an_assignment_reading_the_reel_going_out(db_session, incident, monkeypatch):
    """H2: an assignment holds the journal lock while it reads the reel going out; the seed
    started meanwhile waits for it, so the change is noted among the held reports and the
    tail and the return seen before it stay the old reel's."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_held_tail_and_return(client)
    reached, release = gate_on(monkeypatch, "freeze_spool_ids")
    with every_manager_names(client):

        async def assign():
            async with journal.journal_lock(printer.id):
                await journal.ensure_tail_recorded(
                    db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spool_id=new.id
                )
                await bind(db_session, printer.id, new.id)
                await journal.note_assignment_change(
                    db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, spool_id=new.id
                )

        assignment = asyncio.create_task(assign())
        await reached.wait()
        seed = asyncio.create_task(seed_through_main(printer.id, client))
        await queued_on_the_lock(printer.id, seed)
        assert client.journal_held()  # the seed waits for the assignment, it has not run
        release.set()
        await assignment
        assert await seed
        await replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, old.id, None)]
    assert [s for _l, s, _m in await frozen(db_session, printer.id, archive.id, EVENT_SPOOL_LOADED)] == [new.id]


async def test_a_lock_free_writer_whose_note_the_seed_overtook_is_a_live_change(db_session, incident, monkeypatch):
    """H2, the RFID writer, which takes no journal lock: the seed runs while it reads the reel
    going out and freezes the held facts with the binding it found; the writer's note is then
    refused and its change is a live one — after the return, so the reel it names is adopted
    from the return's layer, and the replayed facts keep the old reel."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_held_tail_and_return(client)
    reached, release = gate_on(monkeypatch, "freeze_spool_ids")
    with every_manager_names(client):

        async def rfid():
            await journal.ensure_tail_recorded(
                db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spool_id=new.id
            )
            await bind(db_session, printer.id, new.id)
            await journal.note_assignment_change(
                db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, spool_id=new.id
            )

        writer = asyncio.create_task(rfid())
        await reached.wait()
        assert await seed_through_main(printer.id, client)
        release.set()
        await writer
        await replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, old.id, None)]
    assert [s for _l, s, _m in await frozen(db_session, printer.id, archive.id, EVENT_SPOOL_LOADED)] == [new.id]


async def test_an_assignment_waits_for_the_batch_being_written(db_session, incident, monkeypatch):
    """H3, the other lock order: the seed holds the journal lock while it writes the batch;
    an assignment of C waits for it, so the return seen while B fed is B's."""
    printer, archive, old, middle = incident
    newest = await a_spool(db_session, "assigned after the replay")
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    with every_manager_names(client):
        await held_assignment(db_session, printer.id, lambda: bind(db_session, printer.id, middle.id))
        a_report(client, TRAY, 45, "PLA")
        reached, release = gate_on(monkeypatch, "record_feed_return")
        seed = asyncio.create_task(seed_through_main(printer.id, client))
        await reached.wait()

        async def assign():
            async with journal.journal_lock(printer.id):
                await journal.ensure_tail_recorded(
                    db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spool_id=newest.id
                )
                await bind(db_session, printer.id, newest.id)

        assignment = asyncio.create_task(assign())
        await settle()
        assert not assignment.done()
        release.set()
        assert await seed
        await assignment
        await replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, middle.id, None)]


@pytest.mark.parametrize("window", ["no_client", "recreated_client"])
async def test_a_batch_delivered_late_names_the_reels_of_the_snapshot(db_session, incident, monkeypatch, window):
    """H4: the batch's first write failed and it waits for the next holder of the journal lock;
    meanwhile the slot was re-linked by a writer that does no accounting, and the printer's
    client is missing (the reconnect window) or was recreated. The late delivery still names
    the reels the seed's snapshot froze — never the binding it finds then."""
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_held_tail_and_return(client)
    real = journal._write_replay_fact
    calls = []

    async def first_write_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return await real(*args, **kwargs)

    monkeypatch.setattr(journal, "_write_replay_fact", first_write_fails)
    with every_manager_names(client):
        assert await seed_through_main(printer.id, client)
    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == []  # rolled back, kept

    await bind(db_session, printer.id, new.id)  # e.g. an unassign + assign: no journal facts
    if window == "no_client":
        later = None
    else:
        later = BambuMQTTClient(ip_address="1.2.3.4", serial_number="HOLD1", access_code="1")
        later.carry_print_lifecycle_from(client)
    with every_manager_names(later):
        async with journal.journal_lock(printer.id):
            pass

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, old.id, None)]


@pytest.mark.parametrize("backend", ["internal", "spoolman"])
async def test_every_kind_of_replayed_fact_is_written_in_order_with_the_reel_of_its_moment(
    db_session, printer_factory, backend
):
    """H5: a tray change at 40, the tail at 60, an assignment, the return at 70 — held, then
    seeded by main. One batch, in arrival order: the tray change and the tail are the old
    reel's, the assignment names the new one, the return (seen after it) is the new reel's.
    Internal inventory and Spoolman freeze the same reels."""
    printer, archive = await a_print(db_session, printer_factory)
    if backend == "internal":
        first, second = [(await a_spool(db_session, name)).id for name in ("old", "new")]
        rebind, ids = (lambda reel: bind(db_session, printer.id, reel)), (lambda reel: (reel, None))
        assigning = {"new_spool_id": second}
    else:
        first, second = 11, 12
        rebind, ids = (lambda reel: bind_spoolman(db_session, printer.id, reel)), (lambda reel: (None, reel))
        assigning = {"new_spoolman_spool_id": second}
    await rebind(first)
    client = a_joined_client_on_tray(1, 30)
    client.state.tray_change_log = [(1, 0)]
    with every_manager_names(client):
        a_report(client, TRAY, 40, "PLA")
        a_report(client, 254, 60, "")
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, **assigning)
            await rebind(second)
        a_report(client, TRAY, 70, "PLA")
        assert await seed_through_main(printer.id, client)

    db_session.expire_all()
    events = [e for e in await journal.load_events(db_session, printer.id, archive.id) if e.event != EVENT_START]
    assert [(e.event, e.layer_num, (e.spool_id, e.spoolman_spool_id)) for e in events] == [
        (EVENT_TRAY_CHANGE, 40, ids(first)),
        (EVENT_TAIL_START, 60, ids(first)),
        (EVENT_SPOOL_LOADED, 60, ids(second)),
        (EVENT_FEED_RETURN, 70, ids(second)),
    ]


async def test_a_batch_interrupted_at_any_fact_is_written_once_and_whole(db_session, printer_factory, monkeypatch):
    """H6: the batch's write fails at its k-th fact, for every k; the next holder of the
    journal lock writes it again. Every run ends with the rows of an uninterrupted one —
    nothing lost, nothing twice — however far the first write got."""

    async def run(fail_at):
        printer, archive = await a_print(db_session, printer_factory)
        old, new = await a_spool(db_session, "old"), await a_spool(db_session, "new")
        await bind(db_session, printer.id, old.id)
        client = a_joined_client_on_tray(1, 30)
        client.state.tray_change_log = [(1, 0)]
        real = journal._write_replay_fact
        calls = []

        async def write(*args, **kwargs):
            calls.append(1)
            if len(calls) == fail_at:
                raise RuntimeError("database is locked")
            return await real(*args, **kwargs)

        with every_manager_names(client), patch.object(journal, "_write_replay_fact", write):
            a_report(client, TRAY, 40, "PLA")
            a_report(client, 254, 60, "")
            async with journal.journal_lock(printer.id):
                await journal.ensure_tail_recorded(
                    db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spool_id=new.id
                )
                await bind(db_session, printer.id, new.id)
            a_report(client, TRAY, 70, "PLA")
            assert await seed_through_main(printer.id, client)
            async with journal.journal_lock(printer.id):
                pass
        db_session.expire_all()
        named = {old.id: "old", new.id: "new"}
        return [
            (e.event, e.layer_num, named.get(e.spool_id))
            for e in await journal.load_events(db_session, printer.id, archive.id)
            if e.event != EVENT_START
        ]

    whole = await run(fail_at=0)
    assert len(whole) == 4
    for k in range(1, len(whole) + 1):
        assert await run(fail_at=k) == whole, k


async def test_a_cancelled_seed_leaves_its_batch_written_once(db_session, incident, monkeypatch):
    """H7, a cancelled owner: the seed is cancelled while its batch is being written; the write
    finishes on its own, and the next holder of the journal lock writes nothing twice."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_held_tail_and_return(client)
    real, started = journal._write_replay_batch, []

    async def counted(*args, **kwargs):
        started.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(journal, "_write_replay_batch", counted)
    with every_manager_names(client):
        reached, release = gate_on(monkeypatch, "record_feed_return")
        seed = asyncio.create_task(seed_through_main(printer.id, client))
        await reached.wait()
        seed.cancel()
        await settle()

        async def next_holder():
            async with journal.journal_lock(printer.id):
                pass

        waiting = asyncio.create_task(next_holder())  # enters while the cancelled seed's write runs
        await settle()
        assert not waiting.done()  # it waits for that write instead of starting its own
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await seed
        await waiting
        await next_holder()

    assert len(started) == 1  # finished by the write the seed started; nobody wrote it again
    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, old.id, None)]


async def test_a_completion_writes_the_batch_left_waiting_before_it_reads_the_journal(
    db_session, incident, monkeypatch
):
    """H7, completion during the handoff: the seed's write failed and the batch waits; the
    print completes. The completion's release writes it while the archive is still printing,
    before anything reads the chronology or the journal."""
    from backend.app.main import _release_unseeded_journal

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_held_tail_and_return(client)
    real = journal._write_replay_fact
    calls = []

    async def first_write_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return await real(*args, **kwargs)

    monkeypatch.setattr(journal, "_write_replay_fact", first_write_fails)
    with every_manager_names(client):
        assert await seed_through_main(printer.id, client)
        assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == []
        await _release_unseeded_journal(printer.id)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, old.id, None)]


async def test_a_completion_before_the_seed_writes_the_released_hold_while_the_archive_prints(db_session, incident):
    """H7: the print completes before its seed; the release states the feed unknown until the
    held reports and writes them — with the reels the slot held — while the archive is still
    the printing one, so nothing the completion reads is missing."""
    from backend.app.main import _release_unseeded_journal
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    client = a_joined_client_on_tray(TRAY, 30)
    with every_manager_names(client):
        a_report(client, 254, 40, "")
        await _release_unseeded_journal(printer.id)

    assert (EVENT_TRAY_CHANGE, 0, None, None) in await rows(db_session, printer.id, archive.id)
    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert client.state.tray_change_log[0] == (UNKNOWN_FEED, 0)


async def test_a_lock_free_writer_overtaken_while_the_batch_waits_records_the_open_tail_first(
    db_session, incident, monkeypatch
):
    """H2: the RFID writer (no journal lock) read the reel going out; the seed ran meanwhile, but
    its write failed, so the batch waits. The writer's note is refused and its change is a live
    one: the tail the replay left open is recorded with the reel going out before the binding
    moves, and the new reel is named in it; the late batch then adds nothing twice."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    real, calls = journal._write_replay_fact, []

    async def first_write_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return await real(*args, **kwargs)

    monkeypatch.setattr(journal, "_write_replay_fact", first_write_fails)
    reached, release = gate_on(monkeypatch, "freeze_spool_ids")
    with every_manager_names(client):

        async def rfid():
            await journal.ensure_tail_recorded(
                db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spool_id=new.id
            )
            await bind(db_session, printer.id, new.id)
            await journal.note_assignment_change(
                db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, spool_id=new.id
            )

        writer = asyncio.create_task(rfid())
        await reached.wait()
        assert await seed_through_main(printer.id, client)
        assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == []  # the batch waits
        release.set()
        await writer
        await replay(db_session, printer.id, client)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert [s for _l, s, _m in await frozen(db_session, printer.id, archive.id, EVENT_SPOOL_LOADED)] == [new.id]


async def test_a_change_noted_before_the_seed_but_written_after_it_names_its_own_reel(db_session, incident):
    """H2: a writer without the journal lock noted its change while the journal waited, and its
    binding commits only after the seed read its snapshot. The replay still names what the
    change put in — the writer said so — never the snapshot's older reel: the return seen after
    the change and the assignment itself are the new reel's, the tail before it the old one's."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    with every_manager_names(client):
        assert not await journal.ensure_tail_recorded(
            db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spool_id=new.id
        )
        a_report(client, TRAY, 45, "PLA")
        assert await seed_through_main(printer.id, client)  # the snapshot still says old
        await bind(db_session, printer.id, new.id)

    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, new.id, None)]
    assert [s for _l, s, _m in await frozen(db_session, printer.id, archive.id, EVENT_SPOOL_LOADED)] == [new.id]


@contextmanager
def every_manager_reconnectable(client):
    """Both printer managers naming ``client``; ``reconnect()`` swaps in a new client carrying
    its state, as ``printer_manager.connect_printer`` does."""
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    with ExitStack() as stack:
        managers = []
        for target in ("backend.app.main.printer_manager", "backend.app.services.printer_manager.printer_manager"):
            pm = stack.enter_context(patch(target))
            pm.get_client.return_value = client
            pm.get_status.return_value = client.state
            managers.append(pm)

        def reconnect():
            fresh = BambuMQTTClient(ip_address="1.2.3.4", serial_number="HOLD1", access_code="1")
            fresh.carry_print_lifecycle_from(managers[0].get_client.return_value)
            for pm in managers:
                pm.get_client.return_value = fresh
                pm.get_status.return_value = fresh.state
            return fresh

        yield reconnect


async def test_a_seed_whose_client_was_replaced_meanwhile_seeds_the_live_one(db_session, incident, monkeypatch):
    """Review of F3: a reconnect replaced the client while the seed read its binding snapshot.
    The seed applies to the client now registered — which carried the hold — never to the
    retired one; the live client is released, and the completion later restates nothing."""
    from backend.app.main import _release_unseeded_journal
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    retired = a_joined_client_feeding_a_tail()
    a_held_tail_and_return(retired)
    real, live = journal.binding_snapshot, {}
    with every_manager_reconnectable(retired) as reconnect:

        async def snapshot_during_a_reconnect(db, printer_id):
            live["client"] = reconnect()
            return await real(db, printer_id)

        monkeypatch.setattr(journal, "binding_snapshot", snapshot_during_a_reconnect)
        assert await seed_through_main(printer.id, retired)
        monkeypatch.setattr(journal, "binding_snapshot", real)
        client = live["client"]
        assert not client.journal_held()
        await _release_unseeded_journal(printer.id)

    assert client.state.tray_change_log[0] != (UNKNOWN_FEED, 0)
    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, old.id, None)]
    assert (EVENT_TRAY_CHANGE, 0, None, None) not in await rows(db_session, printer.id, archive.id)


async def test_a_completion_releases_the_client_registered_when_it_gets_the_lock(db_session, incident):
    """Review of F3: the completion saw a held journal and waited for the journal lock; a
    reconnect replaced the client meanwhile. The hold it ends is the live client's: the feed is
    unknown until the first held report (the sentinel at 40 proves no tail), then the return."""
    from backend.app.main import _release_unseeded_journal
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    retired = a_joined_client_feeding_a_tail()
    a_held_tail_and_return(retired)
    with every_manager_reconnectable(retired) as reconnect:
        lock = journal.journal_lock(printer.id)
        await lock.__aenter__()  # another holder: a live callback, an assignment
        release = asyncio.create_task(_release_unseeded_journal(printer.id))
        await queued_on_the_lock(printer.id, release)
        client = reconnect()
        await lock.__aexit__(None, None, None)
        await release

    assert not client.journal_held()
    assert client.state.tray_change_log == [(UNKNOWN_FEED, 0), (TRAY, 45)]
    db_session.expire_all()
    assert [
        (e.global_tray_id, e.layer_num)
        for e in await journal.load_events(db_session, printer.id, archive.id)
        if e.event == EVENT_TRAY_CHANGE
    ] == [(UNKNOWN_FEED, 0), (TRAY, 45)]


async def test_the_completion_release_names_the_replay_with_its_binding_snapshot(db_session, incident, monkeypatch):
    """The release at completion reads its binding snapshot on the database the journal uses —
    the replay is named with it, not left to be read when it is written."""
    from backend.app.main import _release_unseeded_journal

    printer, archive, old, new = incident
    real, snapshots = journal.binding_snapshot, []

    async def recorded(db, printer_id):
        snapshot = await real(db, printer_id)
        snapshots.append(snapshot)
        return snapshot

    monkeypatch.setattr(journal, "binding_snapshot", recorded)
    client = a_joined_client_on_tray(TRAY, 30)
    with every_manager_names(client):
        a_report(client, 254, 40, "")
        await _release_unseeded_journal(printer.id)

    assert snapshots == [{TRAY: (old.id, None)}]
    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]


async def test_the_reconnect_sweep_writes_a_waiting_replay_before_it_closes_the_print(
    db_session, incident, monkeypatch
):
    """A replay whose write failed waits; the printer reconnects with the print over, and the
    sweep that would close its archive and book its usage writes the replay first, while the
    archive is still the printing one."""
    from backend.app.services import print_reconciliation

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_held_tail_and_return(client)
    real, calls = journal._write_replay_fact, []

    async def first_write_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return await real(*args, **kwargs)

    monkeypatch.setattr(journal, "_write_replay_fact", first_write_fails)
    seen_when_closing = []

    async def close(db, printer_id, *args, **_kwargs):
        seen_when_closing.append(await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START))
        return []

    monkeypatch.setattr(print_reconciliation, "_repair_terminal_queue_items", AsyncMock(return_value=[]))
    monkeypatch.setattr(print_reconciliation, "_reconcile", close)
    with every_manager_names(client):
        assert await seed_through_main(printer.id, client)
        assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == []  # it waits
        await print_reconciliation.reconcile_printer_prints(printer.id, "FINISH", "plate.3mf")

    assert seen_when_closing == [[(40, old.id, None)]]


# ── F4-R1 · a replay that cannot be written is never charged as if it had been (review r4) ──


def failing_replay_writes(monkeypatch, failures):
    """The replay's first ``failures`` fact writes fail; later ones work."""
    real, calls = journal._write_replay_fact, []

    async def write(*args, **kwargs):
        calls.append(1)
        if len(calls) <= failures:
            raise RuntimeError("database is locked")
        return await real(*args, **kwargs)

    monkeypatch.setattr(journal, "_write_replay_fact", write)
    return calls


async def a_rejoined_print_with_a_reel_assigned_during_the_hold(db_session, printer, new, client):
    """A's tail at 40 held; B assigned during the hold; the feed back at 45 — then seeded."""
    async with journal.journal_lock(printer.id):
        await journal.ensure_tail_recorded(
            db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spool_id=new.id
        )
        await bind(db_session, printer.id, new.id)
    a_report(client, TRAY, 45, "PLA")
    assert await seed_through_main(printer.id, client)


def _completion_from(state, captured_layer=0):
    """The completion event's copy of the printer's terminal figures (None state: none)."""
    from backend.app.utils.tray_split import CompletionSnapshot

    return CompletionSnapshot(
        layer_num=(getattr(state, "layer_num", None) or None) if state is not None else None,
        total_layers=(getattr(state, "total_layers", None) or None) if state is not None else None,
        captured_layer=captured_layer or None,
    )


async def internal_charges(db_session, printer, archive, client, *, last_layer_num=0):
    """The internal writer at completion, as Codex's r4 probe runs it: grams per reel, not recorded.
    ``last_layer_num``: the completion's last layer, as ``on_print_complete`` passes it."""
    from backend.app.services.usage_tracker import _track_from_3mf

    archived = await db_session.get(PrintArchive, archive.id)
    archived.status = "completed"
    await db_session.commit()
    db_session.expire_all()
    events = await journal.load_events(db_session, printer.id, archive.id)
    uncharged = []
    # The completion event carries what the client reported at the terminal frame, beside the
    # captured last layer; with no client there is only the latter.
    completion = _completion_from(client.state, captured_layer=last_layer_num)
    result = await _track_from_3mf(
        printer_id=printer.id, archive_id=archive.id, status="completed", print_name="plate", handled_trays=set(),
        printer_manager=SimpleNamespace(get_status=lambda _pid: client.state), db=db_session, ams_mapping=[TRAY],
        file_analysis=SimpleNamespace(
            filament_usage=[{"slot_id": 1, "used_g": 10.0}], layer_usage=None, filament_properties={}
        ),
        analysis_attempted=True, uncharged=uncharged, journal_events=events, last_layer_num=last_layer_num,
        completion=completion,
    )  # fmt: skip
    charges = {}
    for row in result:
        charges[row["spool_id"]] = round(charges.get(row["spool_id"], 0) + row["weight_used"], 1)
    return charges, round(sum(grams for _tray, grams in uncharged), 1)


@pytest.mark.parametrize(
    "failures, expected",
    [
        (0, ("split", 2.2, 7.8, 0.0)),  # control: written at the seed
        (1, ("split", 2.2, 7.8, 0.0)),  # the seed's write failed, the completion's worked: the same split
        (2, ("unknown", 2.0, 0.0, 8.0)),  # it failed again at the completion: from layer 40 not recorded
        (5, ("unknown", 2.0, 0.0, 8.0)),  # however often it fails
    ],
)
async def test_a_completion_never_charges_a_replay_it_could_not_write(
    db_session, incident, monkeypatch, failures, expected
):
    """F4-R1: the completion either writes the replay it was left and splits A/B correctly, or —
    when the write fails again — states the feed unknown from the replay's first fact: what
    came before stays A's, nothing goes to B by the binding of the moment, the rest is not
    recorded. No replay is left waiting after the completion, and none is dropped silently."""
    from backend.app.main import _release_unseeded_journal
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    failing_replay_writes(monkeypatch, failures)
    monkeypatch.setattr("backend.app.services.usage_tracker._warn_if_low_stock", AsyncMock())
    with every_manager_names(client):
        await a_rejoined_print_with_a_reel_assigned_during_the_hold(db_session, printer, new, client)
        await _release_unseeded_journal(printer.id)
        assert not journal._pending_replays.get(printer.id)
        charges, not_recorded = await internal_charges(db_session, printer, archive, client)

    kind, a_grams, b_grams, unknown_grams = expected
    assert (charges.get(old.id, 0.0), charges.get(new.id, 0.0), not_recorded) == (a_grams, b_grams, unknown_grams)
    if kind == "unknown":
        assert client.state.tray_change_log == [(TRAY, 0), (UNKNOWN_FEED, 40)]
        assert (EVENT_TRAY_CHANGE, 40, None, None) in await rows(db_session, printer.id, archive.id)


async def test_spoolman_charges_no_reel_for_a_replay_the_completion_could_not_write(
    db_session, printer_factory, monkeypatch
):
    """F4-R1 on Spoolman: the same failure leaves the same chronology and journal, and the
    Spoolman writer charges A for the stretch before the replay and reports the rest as not
    recorded — never B for the whole print."""
    from backend.app.main import _release_unseeded_journal
    from backend.app.services.spoolman_tracking import report_usage
    from backend.tests.unit.services.test_spoolman_tray_split import _AsyncCtx, _make_db

    printer, archive = await a_print(db_session, printer_factory)
    await bind_spoolman(db_session, printer.id, 11)
    await journal.record_event(
        db_session, printer_id=printer.id, archive_id=archive.id, layer_num=0, event=EVENT_START,
        global_tray_id=TRAY, spoolman_spool_id=11,
    )  # fmt: skip
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    failing_replay_writes(monkeypatch, 2)
    with every_manager_names(client):
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(
                db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spoolman_spool_id=12
            )
            await bind_spoolman(db_session, printer.id, 12)
        a_report(client, TRAY, 45, "PLA")
        assert await seed_through_main(printer.id, client)
        await _release_unseeded_journal(printer.id)
    db_session.expire_all()
    events = await journal.load_events(db_session, printer.id, archive.id)

    tracking = SimpleNamespace(
        filament_usage=[{"slot_id": 1, "used_g": 10.0}],
        ams_trays={TRAY: {"tray_uuid": "", "tray_type": "PLA"}},
        slot_to_tray=[TRAY],
        tray_remain_start={},
        layer_usage={},
        filament_properties={},
    )
    spoolman = AsyncMock()
    spoolman.find_spool_by_tag = AsyncMock(return_value=None)
    spoolman.get_spool = AsyncMock(side_effect=lambda spool_id: {"id": spool_id, "filament": {}})
    spoolman.use_spool = AsyncMock()
    status = SimpleNamespace(tray_change_log=client.state.tray_change_log, total_layers=200, layer_num=200, raw_data={})
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(_make_db(tracking))),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=spoolman),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=events)),
        patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch(
            "backend.app.services.spoolman_tracking._apply_runout_zero_corrections_spoolman",
            AsyncMock(return_value=0),
        ),
        patch("backend.app.services.printer_manager.printer_manager") as pm,
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
        # The slot's binding now is B: what a writer without the replay would charge.
        patch(
            "backend.app.services.spoolman_tracking._resolve_spool_id_via_slot_assignment",
            AsyncMock(return_value=12),
        ),
    ):
        pm.get_status.return_value = status
        await report_usage(printer_id=printer.id, archive_id=archive.id, completion=_completion_from(status))

    assert {c.args[0]: c.args[1] for c in spoolman.use_spool.await_args_list} == {11: 2.0}
    assert notify.await_args.args[1] == [(UNKNOWN_FEED_ID, 8.0)]


UNKNOWN_FEED_ID = -1


async def test_the_reconnect_sweep_states_a_replay_it_could_not_write_unknown_before_it_closes_the_print(
    db_session, incident, monkeypatch
):
    """F4-R1 through the connect-edge sweep: the replay's write failed at the seed and fails
    again when the sweep would close the print — the sweep states the feed unknown from the
    replay's first fact before it closes the archive and books its usage; nothing waits after."""
    from backend.app.services import print_reconciliation
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    failing_replay_writes(monkeypatch, 2)
    seen = []

    async def close(db, printer_id, *args, **_kwargs):
        seen.append((list(client.state.tray_change_log), bool(journal._pending_replays.get(printer_id))))
        return []

    monkeypatch.setattr(print_reconciliation, "_repair_terminal_queue_items", AsyncMock(return_value=[]))
    monkeypatch.setattr(print_reconciliation, "_reconcile", close)
    with every_manager_names(client):
        await a_rejoined_print_with_a_reel_assigned_during_the_hold(db_session, printer, new, client)
        await print_reconciliation.reconcile_printer_prints(printer.id, "FINISH", "plate.3mf")

    assert seen == [([(TRAY, 0), (UNKNOWN_FEED, 40)], False)]
    assert (EVENT_TRAY_CHANGE, 40, None, None) in await rows(db_session, printer.id, archive.id)


async def test_a_replay_whose_commit_keeps_failing_is_stated_unknown_once(db_session, incident, monkeypatch):
    """F4-R1 with the COMMIT failing (every fact flushed): the second failure states the feed
    unknown from the replay's first fact — once — and a later holder of the journal lock, with
    the storage back, writes none of the replay twice nor the unknown twice."""
    import logging

    from backend.app.core import database
    from backend.app.main import _release_unseeded_journal, _restore_usage_tracking_session

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    factory, commits = database.async_session, []

    def sessions_whose_commits_fail_twice():
        session = factory()
        commit = session.commit

        async def failing():
            commits.append(1)
            if len(commits) <= 2:
                raise RuntimeError("database is locked")
            await commit()

        session.commit = failing
        return session

    with every_manager_names(client):
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(
                db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spool_id=new.id
            )
            await bind(db_session, printer.id, new.id)
        a_report(client, TRAY, 45, "PLA")
        seed_db = factory()  # the seed's own session works; only the replay's commits fail
        monkeypatch.setattr(database, "async_session", sessions_whose_commits_fail_twice)
        assert await _restore_usage_tracking_session(
            printer.id, client.state, seed_db, logging.getLogger(__name__), generation=client.journal_generation()
        )
        await seed_db.close()
        await _release_unseeded_journal(printer.id)
        monkeypatch.setattr(database, "async_session", factory)
        for _ in range(2):
            async with journal.journal_lock(printer.id):
                pass

    events = [r for r in await rows(db_session, printer.id, archive.id) if r[0] != EVENT_START]
    assert events == [(EVENT_TRAY_CHANGE, 40, None, None)]


async def test_a_replay_written_on_its_second_try_is_charged_once(db_session, incident, monkeypatch):
    """The seed's write failed; the next holder of the lock wrote it. Later holders and the
    completion write nothing of it again, and the completion charges the split once."""
    from backend.app.main import _release_unseeded_journal

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    failing_replay_writes(monkeypatch, 1)
    monkeypatch.setattr("backend.app.services.usage_tracker._warn_if_low_stock", AsyncMock())
    with every_manager_names(client):
        await a_rejoined_print_with_a_reel_assigned_during_the_hold(db_session, printer, new, client)
        for _ in range(3):
            async with journal.journal_lock(printer.id):
                pass
        await _release_unseeded_journal(printer.id)
        charges, not_recorded = await internal_charges(db_session, printer, archive, client)

    assert (charges.get(old.id), charges.get(new.id), not_recorded) == (2.2, 7.8, 0.0)
    assert await frozen(db_session, printer.id, archive.id, EVENT_TAIL_START) == [(40, old.id, None)]
    assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, new.id, None)]


@pytest.mark.parametrize("second_try", ["works", "fails"])
async def test_a_live_fact_never_lands_ahead_of_a_replay_still_waiting(db_session, incident, monkeypatch, second_try):
    """F4-R1, order: the seed's write failed; a live tray change arrives at 70. Its holder of
    the journal lock first writes the replay — or, when that fails again, states the feed
    unknown from the replay's first fact — and only then writes the live fact, after it."""
    from backend.app.services.usage_tracker import record_tray_change_event
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    failing_replay_writes(monkeypatch, 1 if second_try == "works" else 2)
    with every_manager_names(client):
        await a_rejoined_print_with_a_reel_assigned_during_the_hold(db_session, printer, new, client)
        async with journal.journal_lock(printer.id):  # main's live tray-change callback
            await record_tray_change_event(db_session, printer.id, 0, 70)

    events = [(e, layer) for e, layer, _s, _k in await rows(db_session, printer.id, archive.id) if e != EVENT_START]
    if second_try == "works":
        assert events == [
            (EVENT_TAIL_START, 40),
            (EVENT_SPOOL_LOADED, 40),
            (EVENT_FEED_RETURN, 45),
            (EVENT_TRAY_CHANGE, 70),
        ]
    else:
        assert events == [(EVENT_TRAY_CHANGE, 40), (EVENT_TRAY_CHANGE, 70)]
        assert client.state.tray_change_log[:2] == [(TRAY, 0), (UNKNOWN_FEED, 40)]


async def test_the_print_completes_whatever_the_replay_handoff_does(monkeypatch):
    """F4-R1: accounting failure is never a reason to hold the physical completion — with the
    replay's write failing and the unknown statement failing too, the completion runs."""
    from backend.app import main

    ran = []
    client = MagicMock()
    client.journal_held.return_value = False
    client.journal_generation.return_value = 4

    async def completion(printer_id, data, analysis):
        ran.append(printer_id)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("database is gone")

    monkeypatch.setattr(main.printer_manager, "get_client", lambda _pid: client)
    monkeypatch.setattr(main, "_on_print_complete_impl", completion)
    monkeypatch.setattr(journal, "deliver_pending_replay", broken)

    await main.on_print_complete(7, {})

    assert ran == [7]


async def test_a_replay_whose_print_was_closed_meanwhile_is_stated_unknown_in_that_print(
    db_session, incident, monkeypatch
):
    """F4-R1: the replay's write failed once and its print's archive was closed by a path that
    never took the journal lock. The next holder does not drop it for the archive's status: it
    states the feed unknown from the replay's first fact in that print's own journal."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_held_tail_and_return(client)
    failing_replay_writes(monkeypatch, 1)
    with every_manager_names(client):
        assert await seed_through_main(printer.id, client)
        closed = await db_session.get(PrintArchive, archive.id)
        closed.status = "completed"
        await db_session.commit()
        async with journal.journal_lock(printer.id):
            pass

    assert not journal._pending_replays.get(printer.id)
    assert [r for r in await rows(db_session, printer.id, archive.id) if r[0] != EVENT_START] == [
        (EVENT_TRAY_CHANGE, 40, None, None)
    ]


async def test_a_completion_before_the_seed_settles_the_replay_it_released_but_could_not_write(
    db_session, incident, monkeypatch
):
    """F4-R1: the print completes before its seed, with B assigned during the hold; the release's
    own write of what it released fails, and fails again — the completion does not charge the
    print by a chronology whose reels nobody wrote: the feed is unknown from the start, nothing
    goes to A or B, and nothing is left waiting."""
    from backend.app.main import _release_unseeded_journal
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    failing_replay_writes(monkeypatch, 2)
    monkeypatch.setattr("backend.app.services.usage_tracker._warn_if_low_stock", AsyncMock())
    with every_manager_names(client):
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(
                db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spool_id=new.id
            )
            await bind(db_session, printer.id, new.id)
        a_report(client, TRAY, 45, "PLA")
        await _release_unseeded_journal(printer.id)
        assert not journal._pending_replays.get(printer.id)
        charges, not_recorded = await internal_charges(db_session, printer, archive, client)

    assert client.state.tray_change_log == [(UNKNOWN_FEED, 0)]
    assert (charges, not_recorded) == ({}, 10.0)


@pytest.mark.parametrize("failures, written", [(1, True), (2, False)])
async def test_the_closing_sweep_writes_or_states_unknown_a_replay_never_tried(
    db_session, incident, monkeypatch, failures, written
):
    """F4-R1: a replay nobody has tried to write yet (the client was seeded and nothing took the
    journal lock since); the sweep that may close the print tries it, and tries once more before
    it closes — written, or stated unknown from its first fact; never left waiting."""
    from backend.app.services import print_reconciliation
    from backend.app.utils.usage_attribution import UNKNOWN_FEED

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    a_report(client, TRAY, 45, "PLA")
    failing_replay_writes(monkeypatch, failures)
    seen = []

    async def close(db, printer_id, *args, **_kwargs):
        seen.append((list(client.state.tray_change_log), bool(journal._pending_replays.get(printer_id))))
        return []

    monkeypatch.setattr(print_reconciliation, "_repair_terminal_queue_items", AsyncMock(return_value=[]))
    monkeypatch.setattr(print_reconciliation, "_reconcile", close)
    with every_manager_names(client):
        assert client.seed_usage_journal(
            client.journal_generation(), log=[(TRAY, 0)], last_loaded_tray=TRAY, tail_origin=None
        )
        await print_reconciliation.reconcile_printer_prints(printer.id, "FINISH", "plate.3mf")

    if written:
        assert seen == [([(TRAY, 0)], False)]
        assert await frozen(db_session, printer.id, archive.id, EVENT_FEED_RETURN) == [(45, old.id, None)]
    else:
        assert seen == [([(TRAY, 0), (UNKNOWN_FEED, 40)], False)]
        assert (EVENT_TRAY_CHANGE, 40, None, None) in await rows(db_session, printer.id, archive.id)


# ── F5-R1 · an unknown feed stated without a client still governs the accounting (review r5) ──


async def a_replay_settled_while_the_client_is_away(db_session, printer, new, client, monkeypatch):
    """The replay's write fails at the seed and again at the completion release, while the
    printer's client is missing: the unknown feed reaches the journal only."""
    from backend.app.main import _release_unseeded_journal

    failing_replay_writes(monkeypatch, 2)
    with every_manager_names(client):
        await a_rejoined_print_with_a_reel_assigned_during_the_hold(db_session, printer, new, client)
    with every_manager_names(None):
        await _release_unseeded_journal(printer.id)
    assert not journal._pending_replays.get(printer.id)


def a_client_carrying(prior):
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    successor = BambuMQTTClient(ip_address="1.2.3.4", serial_number="REJOIN", access_code="1")
    successor.carry_print_lifecycle_from(prior)
    successor.state.total_layers = prior.state.total_layers
    successor.state.layer_num = prior.state.layer_num
    successor.state.raw_data = prior.state.raw_data
    return successor


@pytest.mark.parametrize("which", ["retired", "successor"])
async def test_an_unknown_feed_journaled_without_a_client_governs_the_internal_writer(
    db_session, incident, monkeypatch, which
):
    """F5-R1: the unknown feed was stated while no client was registered — the memory of the
    retired client, or of a successor carrying it, still says what the lost replay applied.
    The journal's marker governs: A keeps the stretch before it, the rest is not recorded."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    monkeypatch.setattr("backend.app.services.usage_tracker._warn_if_low_stock", AsyncMock())
    await a_replay_settled_while_the_client_is_away(db_session, printer, new, client, monkeypatch)
    reader = client if which == "retired" else a_client_carrying(client)
    with every_manager_names(reader):
        charges, not_recorded = await internal_charges(db_session, printer, archive, reader)

    assert (charges.get(old.id, 0.0), charges.get(new.id, 0.0), not_recorded) == (2.0, 0.0, 8.0)


async def test_an_unknown_feed_journaled_without_a_client_governs_the_spoolman_writer(
    db_session, printer_factory, monkeypatch
):
    """F5-R1 on Spoolman: with the marker in the journal only, Spoolman charges A for the
    stretch before it and reports the rest as not recorded — not B for the whole print."""
    from backend.app.main import _release_unseeded_journal
    from backend.app.services.spoolman_tracking import report_usage
    from backend.app.utils.usage_attribution import UNKNOWN_FEED
    from backend.tests.unit.services.test_spoolman_tray_split import _AsyncCtx, _make_db

    printer, archive = await a_print(db_session, printer_factory)
    await bind_spoolman(db_session, printer.id, 11)
    await journal.record_event(
        db_session, printer_id=printer.id, archive_id=archive.id, layer_num=0, event=EVENT_START,
        global_tray_id=TRAY, spoolman_spool_id=11,
    )  # fmt: skip
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    failing_replay_writes(monkeypatch, 2)
    with every_manager_names(client):
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(
                db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spoolman_spool_id=12
            )
            await bind_spoolman(db_session, printer.id, 12)
        a_report(client, TRAY, 45, "PLA")
        assert await seed_through_main(printer.id, client)
    with every_manager_names(None):
        await _release_unseeded_journal(printer.id)
    assert client.state.tray_change_log == [(TRAY, 0)]  # the memory never heard of it
    db_session.expire_all()
    events = await journal.load_events(db_session, printer.id, archive.id)

    tracking = SimpleNamespace(
        filament_usage=[{"slot_id": 1, "used_g": 10.0}],
        ams_trays={TRAY: {"tray_uuid": "", "tray_type": "PLA"}},
        slot_to_tray=[TRAY],
        tray_remain_start={},
        layer_usage={},
        filament_properties={},
    )
    spoolman = AsyncMock()
    spoolman.find_spool_by_tag = AsyncMock(return_value=None)
    spoolman.get_spool = AsyncMock(side_effect=lambda spool_id: {"id": spool_id, "filament": {}})
    spoolman.use_spool = AsyncMock()
    status = SimpleNamespace(tray_change_log=client.state.tray_change_log, total_layers=200, layer_num=200, raw_data={})
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(_make_db(tracking))),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=spoolman),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=events)),
        patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch(
            "backend.app.services.spoolman_tracking._apply_runout_zero_corrections_spoolman",
            AsyncMock(return_value=0),
        ),
        patch("backend.app.services.printer_manager.printer_manager") as pm,
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
        patch(
            "backend.app.services.spoolman_tracking._resolve_spool_id_via_slot_assignment",
            AsyncMock(return_value=12),
        ),
    ):
        pm.get_status.return_value = status
        await report_usage(printer_id=printer.id, archive_id=archive.id, completion=_completion_from(status))

    assert {c.args[0]: c.args[1] for c in spoolman.use_spool.await_args_list} == {11: 2.0}
    assert notify.await_args.args[1] == [(UNKNOWN_FEED, 8.0)]


async def test_the_reconnect_sweep_without_a_client_leaves_an_unknown_feed_the_writer_honours(
    db_session, incident, monkeypatch
):
    """F5-R1 through the sweep: the client is missing when the sweep settles the replay that
    could not be written; the writer reading the memory a successor carried still honours the
    marker the sweep journaled."""
    from backend.app.services import print_reconciliation

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    failing_replay_writes(monkeypatch, 2)
    monkeypatch.setattr("backend.app.services.usage_tracker._warn_if_low_stock", AsyncMock())
    monkeypatch.setattr(print_reconciliation, "_repair_terminal_queue_items", AsyncMock(return_value=[]))
    monkeypatch.setattr(print_reconciliation, "_reconcile", AsyncMock(return_value=[]))
    with every_manager_names(client):
        await a_rejoined_print_with_a_reel_assigned_during_the_hold(db_session, printer, new, client)
    with every_manager_names(None):
        await print_reconciliation.reconcile_printer_prints(printer.id, "FINISH", "plate.3mf")
    successor = a_client_carrying(client)
    with every_manager_names(successor):
        charges, not_recorded = await internal_charges(db_session, printer, archive, successor)

    assert (charges.get(old.id, 0.0), charges.get(new.id, 0.0), not_recorded) == (2.0, 0.0, 8.0)


@pytest.mark.parametrize("next_report", ["another_tray", "the_same_tray"])
async def test_the_next_tray_report_after_an_unknown_feed_stated_without_a_client_closes_it(
    db_session, incident, monkeypatch, next_report
):
    """F5-R1: after the unknown feed was stated without a client, the client comes back
    (carrying the old memory) and the printer reports a tray at 100. That report is a feed
    fact again — another tray, or the very tray the memory last named — and from there the
    print is charged to the reel bound then; only 40–100 stays not recorded."""
    from backend.app.services.usage_tracker import record_tray_change_event
    from backend.app.utils.usage_attribution import UNKNOWN_FEED, chronology_with_journal

    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    monkeypatch.setattr("backend.app.services.usage_tracker._warn_if_low_stock", AsyncMock())
    await a_replay_settled_while_the_client_is_away(db_session, printer, new, client, monkeypatch)
    successor = a_client_carrying(client)
    queued = queue_every_callback(successor)
    with every_manager_names(successor):
        async with journal.journal_lock(printer.id):  # the next holder: the memory learns it
            pass
        slot_reads(successor, TRAY, "PLA")
        tray = TRAY if next_report == "the_same_tray" else TRAY + 1
        successor._process_message({"print": {"ams": {"tray_now": str(tray)}, "layer_num": 100}})
        for kind, args in queued:
            assert kind == "tray"
            async with journal.journal_lock(printer.id):
                await record_tray_change_event(db_session, printer.id, *args)
        db_session.expire_all()
        events = await journal.load_events(db_session, printer.id, archive.id)
        assert chronology_with_journal(successor.state.tray_change_log, events) == [
            (TRAY, 0),
            (UNKNOWN_FEED, 40),
            (tray, 100),
        ]
        charges, not_recorded = await internal_charges(db_session, printer, archive, successor)

    if next_report == "the_same_tray":  # B, bound since the held assignment, from 100
        assert (charges.get(old.id, 0.0), charges.get(new.id, 0.0), not_recorded) == (2.0, 5.0, 3.0)


# ── F6-R1 · no client at the writer either: the journal is the chronology (review r6) ──


NO_CLIENT = SimpleNamespace(state=None)


@pytest.mark.parametrize(
    "last_layer, later_feed, expected",
    [
        (200, False, (2.0, 0.0, 8.0)),  # the layer count is known: the start stays A's
        (0, False, (0.0, 0.0, 10.0)),  # nothing to divide by: nothing invented, all not recorded
        (200, True, (2.0, 5.0, 3.0)),  # a feed journaled after the marker is charged from there
    ],
)
async def test_the_internal_writer_reads_the_journal_when_there_is_no_client_at_all(
    db_session, incident, monkeypatch, last_layer, later_feed, expected
):
    """F6-R1: the unknown feed reached the journal while the client was away, and it is still
    away when the print is charged (no printer state at all). The journal is the chronology:
    the start's reel keeps what came before the marker, a feed journaled later keeps its own
    stretch, and when no layer count tells how to divide the grams nothing is invented — the
    slot goes not recorded, never to the reel in the slot now."""
    printer, archive, old, new = incident
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    monkeypatch.setattr("backend.app.services.usage_tracker._warn_if_low_stock", AsyncMock())
    await a_replay_settled_while_the_client_is_away(db_session, printer, new, client, monkeypatch)
    if later_feed:  # the client journaled the slot feeding again at 100, then went away
        await journal.record_tray_change(
            db_session, printer_id=printer.id, archive_id=archive.id, tray=TRAY, layer_num=100, ids=(new.id, None)
        )
        await db_session.commit()
    with every_manager_names(None):
        charges, not_recorded = await internal_charges(
            db_session, printer, archive, NO_CLIENT, last_layer_num=last_layer
        )

    assert (charges.get(old.id, 0.0), charges.get(new.id, 0.0), not_recorded) == expected


@pytest.mark.parametrize("measured", [True, False])
async def test_spoolman_reads_the_journal_when_there_is_no_client_at_all(
    db_session, printer_factory, monkeypatch, measured
):
    """F6-R1 on Spoolman's completion: no printer state at all. With the print's per-layer
    G-code the stretch before the marker is the start's reel and the rest not recorded; with
    nothing to divide by, the whole slot is not recorded — never charged to the slot's reel."""
    from backend.app.main import _release_unseeded_journal
    from backend.app.services.spoolman_tracking import report_usage
    from backend.app.utils.usage_attribution import UNKNOWN_FEED
    from backend.tests.unit.services.test_spoolman_tray_split import _AsyncCtx, _make_db

    printer, archive = await a_print(db_session, printer_factory)
    await bind_spoolman(db_session, printer.id, 11)
    await journal.record_event(
        db_session, printer_id=printer.id, archive_id=archive.id, layer_num=0, event=EVENT_START,
        global_tray_id=TRAY, spoolman_spool_id=11,
    )  # fmt: skip
    client = a_joined_client_feeding_a_tail()
    client.state.tray_change_log = [(TRAY, 0)]
    failing_replay_writes(monkeypatch, 2)
    with every_manager_names(client):
        async with journal.journal_lock(printer.id):
            await journal.ensure_tail_recorded(
                db_session, printer_id=printer.id, ams_id=AMS, tray_id=SLOT, new_spoolman_spool_id=12
            )
            await bind_spoolman(db_session, printer.id, 12)
        a_report(client, TRAY, 45, "PLA")
        assert await seed_through_main(printer.id, client)
    with every_manager_names(None):
        await _release_unseeded_journal(printer.id)
    db_session.expire_all()
    events = await journal.load_events(db_session, printer.id, archive.id)

    tracking = SimpleNamespace(
        filament_usage=[{"slot_id": 1, "used_g": 10.0}],
        ams_trays={TRAY: {"tray_uuid": "", "tray_type": "PLA"}},
        slot_to_tray=[TRAY],
        tray_remain_start={},
        # The G-code's cumulative extrusion per layer (one unit a layer), as the 3MF analysis stores it.
        layer_usage={str(layer): {"0": float(layer)} for layer in range(201)} if measured else {},
        filament_properties={},
    )
    spoolman = AsyncMock()
    spoolman.find_spool_by_tag = AsyncMock(return_value=None)
    spoolman.get_spool = AsyncMock(side_effect=lambda spool_id: {"id": spool_id, "filament": {}})
    spoolman.use_spool = AsyncMock()
    notify = AsyncMock()
    with (
        patch("backend.app.services.spoolman_tracking.async_session", lambda: _AsyncCtx(_make_db(tracking))),
        patch("backend.app.api.routes.settings.get_setting", AsyncMock(return_value="true")),
        patch(
            "backend.app.services.spoolman_tracking._get_spoolman_client_with_fallback",
            AsyncMock(return_value=spoolman),
        ),
        patch("backend.app.services.spoolman_tracking._get_printer_serial", AsyncMock(return_value="SERIAL")),
        patch("backend.app.services.spoolman_tracking._load_journal_events", AsyncMock(return_value=events)),
        patch("backend.app.services.spoolman_tracking._apply_spool_colors_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._apply_spool_types_to_archive", AsyncMock()),
        patch("backend.app.services.spoolman_tracking._settle_charges", AsyncMock()),
        patch(
            "backend.app.services.spoolman_tracking._apply_runout_zero_corrections_spoolman",
            AsyncMock(return_value=0),
        ),
        patch("backend.app.services.printer_manager.printer_manager") as pm,
        patch("backend.app.services.spool_assignment_notifications.notify_usage_not_recorded", notify),
        patch(
            "backend.app.services.spoolman_tracking._resolve_spool_id_via_slot_assignment",
            AsyncMock(return_value=12),
        ),
    ):
        pm.get_status.return_value = None
        await report_usage(printer_id=printer.id, archive_id=archive.id)

    charged = {c.args[0]: c.args[1] for c in spoolman.use_spool.await_args_list}
    if measured:
        assert charged == {11: 2.0}
        assert notify.await_args.args[1] == [(UNKNOWN_FEED, 8.0)]
    else:
        assert charged == {}
        assert notify.await_args.args[1] == [(UNKNOWN_FEED, 10.0)]
