"""C047 part 2 on a disposable embedded PostgreSQL: a base change and a print's first Spoolman facts.

S41/S44 and the acceptance's A01/A05. PostgreSQL is where the order matters: a fresh SELECT of
the durable predicate sees only committed rows, so a writer whose evidence is not committed yet
is held back by its activity record, never by the SELECT. The base change's SELECT is paused
inside the server (an advisory lock wrapped around the statement), so a writer arrives while
the flag is set — and a writer paused inside its own record meets a change that must refuse.
"""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select, text

from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]

KEY = 470033
A, B = "http://a.invalid:7912", "http://b.invalid:7912"


@pytest_asyncio.fixture
async def pg(test_engine, monkeypatch, tmp_path):  # noqa: F811
    from backend.app import main
    from backend.app.core import database
    from backend.app.core.config import settings
    from backend.app.models.settings import Settings
    from backend.app.services import print_usage_journal as journal

    factory = database.async_session
    monkeypatch.setattr(main, "async_session", factory)
    monkeypatch.setattr(settings, "database_url", test_engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "archive_dir", tmp_path / "archive")
    monkeypatch.setattr(
        "backend.app.services.location_service.maybe_sync_spoolman_locations", AsyncMock(return_value=0)
    )
    journal.forget_tail_state()
    main._active_prints.clear()
    main._timelapse_baselines.clear()
    async with factory() as db:
        db.add_all(
            [
                Settings(key="_credential_config_guard", value="1"),
                Settings(key="spoolman_url", value=A),
                Settings(key="spoolman_enabled", value="false"),
            ]
        )
        await db.commit()
    try:
        yield factory, test_engine
    finally:
        journal.forget_tail_state()
        main._active_prints.clear()


async def _change(factory, address=B):
    from backend.app.api.routes.settings import update_spoolman_settings

    async with factory() as db:
        return await update_spoolman_settings({"spoolman_url": address}, db=db, _=None)


async def _url(factory) -> str:
    async with factory() as db:
        return await db.scalar(text("SELECT value FROM settings WHERE key='spoolman_url'"))


async def _printer(factory):
    from backend.app.models.printer import Printer

    async with factory() as db:
        printer = Printer(
            name="C047",
            serial_number="C047-PG",
            ip_address="127.0.0.2",
            access_code="00000000",
            model="X1C",
            auto_archive=True,
            plate_detection_enabled=False,
            external_camera_enabled=False,
        )
        db.add(printer)
        await db.commit()
        return printer


async def _printing(factory, printer):
    from backend.app.models.archive import PrintArchive

    async with factory() as db:
        archive = PrintArchive(
            printer_id=printer.id, status="printing", filename="f.3mf", print_name="f", file_path="", file_size=0
        )
        db.add(archive)
        await db.commit()
        return archive.id


def _queue(printer_id, *, stamp, ids=(5, 17)):
    from backend.app.services import print_usage_journal as journal
    from backend.app.services.bambu_mqtt import ReplayBatch, ReplayFact

    batch = ReplayBatch(1, None, (ReplayFact("tail_start", 0, 10, False, ids),), bindings_stamp=stamp)
    journal.queue_replay(printer_id, [batch])


async def _holder(printer_id):
    from backend.app.services.print_usage_journal import journal_lock

    async with journal_lock(printer_id):
        pass


async def _events(factory):
    from backend.app.models.print_usage_event import PrintUsageEvent

    async with factory() as db:
        return (await db.execute(select(PrintUsageEvent.spool_id, PrintUsageEvent.spoolman_spool_id))).all()


@contextlib.asynccontextmanager
async def _paused_durable_select(engine):
    """Hold the base change's fresh SELECT inside PostgreSQL until the block ends."""
    started = asyncio.Event()
    pids: list[int] = []

    def pause(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("SELECT print_archives.id") and "active_print_spoolman" in statement:
            pids.append(conn.connection.driver_connection.get_server_pid())
            started.set()
            statement = (
                f"WITH gate AS MATERIALIZED (SELECT pg_advisory_xact_lock({KEY})) "
                f"SELECT checked.* FROM gate CROSS JOIN LATERAL ({statement}) AS checked"
            )
        return statement, parameters

    event.listen(engine.sync_engine, "before_cursor_execute", pause, retval=True)
    try:
        async with engine.connect() as blocker, engine.connect() as observer:
            await blocker.execute(text(f"SELECT pg_advisory_lock({KEY})"))

            async def wait_inside():
                await asyncio.wait_for(started.wait(), 5)
                async with asyncio.timeout(5):
                    while not await observer.scalar(
                        text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid=:pid AND wait_event='advisory')"),
                        {"pid": pids[0]},
                    ):
                        await asyncio.sleep(0.01)

            released = False

            async def release():
                nonlocal released
                if not released:
                    released = True
                    await blocker.execute(text(f"SELECT pg_advisory_unlock({KEY})"))

            try:
                yield wait_inside, release
            finally:
                await release()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", pause)


def _stock_event():
    return {"ams_id": 0, "tray_id": 0, "generation": 1, "sequence": 1}


# --- the change first: every first fact waits, the lane stays free (S41, S44, A05) ----------


@pytest.mark.parametrize("flow", ["start", "adoption", "replay", "stock_insertion"])
async def test_a_change_first_holds_the_first_facts_until_its_commit(pg, monkeypatch, tmp_path, flow):
    from backend.app import main
    from backend.app.models.archive import PrintArchive
    from backend.app.services import spoolman_activity, spoolman_source
    from backend.tests.integration.test_adopt_print_started_while_down import _drive_hook
    from backend.tests.integration.test_archive_row_at_print_start import _drive_print_start, _Recorder

    factory, engine = pg
    printer = await _printer(factory) if flow != "adoption" else None
    if flow == "replay":
        await _printing(factory, printer)
        _queue(printer.id, stamp=spoolman_source.base_generation())  # read under A
    claim = AsyncMock(return_value={"reason": "not_stock"})
    monkeypatch.setattr("backend.app.services.auto_stock_spool.claim_on_insertion", claim)
    if flow == "stock_insertion":
        main._stock_insertion_seen.pop((printer.id, 0, 0), None)
    entered = asyncio.Event()
    original = spoolman_activity.first_evidence

    @contextlib.asynccontextmanager
    async def observed(kind):
        entered.set()
        async with original(kind):
            yield

    monkeypatch.setattr(spoolman_activity, "first_evidence", observed)

    async def publisher():
        if flow == "replay":
            await _holder(printer.id)
        elif flow == "stock_insertion":
            await main.on_stock_spool_inserted(printer.id, _stock_event())
        else:
            async with factory() as db:
                if flow == "start":
                    await _drive_print_start(
                        db_session=db,
                        test_engine=engine,
                        tmp_path=tmp_path,
                        monkeypatch=monkeypatch,
                        printer=printer,
                        download_result=None,
                        recorder=_Recorder(),
                    )
                else:

                    async def printer_factory():
                        return await _printer(factory)

                    await _drive_hook(db_session=db, printer_factory=printer_factory)

    async with _paused_durable_select(engine) as (wait_inside, release):
        change = asyncio.create_task(_change(factory))
        await wait_inside()
        assert spoolman_source.base_change_in_flight()
        work = asyncio.create_task(publisher())
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.sleep(0.05)
        assert not work.done() and not spoolman_activity.records_open()
        if printer is not None:
            assert not main._get_ams_assignment_lock(printer.id).locked(), "no lane is held while waiting"
        if flow in ("start", "adoption"):
            async with factory() as db:
                assert not (await db.scalars(select(PrintArchive))).all()
        if flow == "replay":
            assert await _events(factory) == []
        await release()
        await asyncio.wait_for(asyncio.gather(change, work), 15)
    assert await _url(factory) == B
    if flow in ("start", "adoption"):
        async with factory() as db:
            assert (await db.scalars(select(PrintArchive).where(PrintArchive.status == "printing"))).all()
    if flow == "replay":
        assert await _events(factory) == [(5, None)], "the number read under A is not written under B"
    if flow == "stock_insertion":
        claim.assert_awaited_once()


async def test_a_replay_read_under_the_new_base_keeps_its_number(pg):
    from backend.app.services import spoolman_source

    factory, _ = pg
    printer = await _printer(factory)
    await _change(factory)
    await _printing(factory, printer)
    _queue(printer.id, stamp=spoolman_source.base_generation())  # read under B
    await _holder(printer.id)
    assert await _events(factory) == [(5, 17)]


# --- the writer first: its record refuses the change (S44) -------------------------------------


@pytest.mark.parametrize("flow", ["start", "replay"])
async def test_a_writer_inside_its_record_refuses_the_change(pg, monkeypatch, tmp_path, flow):
    from backend.app.services import print_execution, print_usage_journal as journal, spoolman_source
    from backend.tests.integration.test_archive_row_at_print_start import _drive_print_start, _Recorder

    factory, engine = pg
    printer = await _printer(factory)
    inside, resume = asyncio.Event(), asyncio.Event()
    if flow == "replay":
        await _printing(factory, printer)
        _queue(printer.id, stamp=spoolman_source.base_generation())
        original = journal._write_replay_batch

        async def hold(db, printer_id, batch):
            written = await original(db, printer_id, batch)
            inside.set()
            await resume.wait()
            return written

        monkeypatch.setattr(journal, "_write_replay_batch", hold)
        work = asyncio.create_task(_holder(printer.id))
    else:
        original = print_execution._publish_start_locked

        async def hold(*args, **kwargs):
            inside.set()
            await resume.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(print_execution, "_publish_start_locked", hold)

        async def start():
            async with factory() as db:
                await _drive_print_start(
                    db_session=db,
                    test_engine=engine,
                    tmp_path=tmp_path,
                    monkeypatch=monkeypatch,
                    printer=printer,
                    download_result=None,
                    recorder=_Recorder(),
                )

        work = asyncio.create_task(start())
    try:
        await asyncio.wait_for(inside.wait(), 10)
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(_change(factory), 5)
        assert (refused.value.status_code, refused.value.detail["reason"]) == (409, "print_accounting")
        assert not spoolman_source.base_change_in_flight()
    finally:
        resume.set()
        await asyncio.wait_for(work, 15)
    assert await _url(factory) == A
    if flow == "replay":
        assert await _events(factory) == [(5, 17)]


# --- A01: a cancelled holder, a rolled-back writer -------------------------------------------


async def test_a_cancelled_replay_caller_keeps_the_change_out_until_the_write_is_over(pg, monkeypatch):
    from backend.app.services import print_usage_journal as journal, spoolman_activity, spoolman_source

    factory, _ = pg
    printer = await _printer(factory)
    await _printing(factory, printer)
    _queue(printer.id, stamp=spoolman_source.base_generation())
    before_commit, resume = asyncio.Event(), asyncio.Event()
    original = journal._write_replay_batch

    async def pause_after_work(db, printer_id, batch):
        written = await original(db, printer_id, batch)
        before_commit.set()
        await resume.wait()
        return written

    monkeypatch.setattr(journal, "_write_replay_batch", pause_after_work)
    caller = asyncio.create_task(_holder(printer.id))
    await asyncio.wait_for(before_commit.wait(), 5)
    writer = journal._replay_writers[printer.id]
    try:
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not writer.done() and spoolman_activity.records_open()
        assert await _events(factory) == [], "the event is not committed yet"
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(_change(factory), 5)
        assert refused.value.detail["reason"] == "print_accounting"
    finally:
        resume.set()
        await asyncio.wait_for(writer, 5)
    assert not spoolman_activity.records_open()
    assert await _url(factory) == A
    assert await _events(factory) == [(5, 17)], "committed once"


@pytest.mark.parametrize("changed_between", [False, True], ids=["same-base", "base-changed"])
async def test_a_rolled_back_replay_goes_to_the_next_holder(pg, monkeypatch, changed_between):
    from backend.app.services import print_usage_journal as journal, spoolman_activity, spoolman_source

    factory, _ = pg
    printer = await _printer(factory)
    await _printing(factory, printer)
    _queue(printer.id, stamp=spoolman_source.base_generation())
    original = journal._write_replay_batch
    calls = []

    async def fail_once(db, printer_id, batch):
        calls.append(1)
        written = await original(db, printer_id, batch)
        if len(calls) == 1:
            raise RuntimeError("the first write rolls back")
        return written

    monkeypatch.setattr(journal, "_write_replay_batch", fail_once)
    await _holder(printer.id)
    assert not spoolman_activity.records_open() and await _events(factory) == []
    if changed_between:
        await _change(factory)  # nothing committed names a Spoolman spool: the base may change
    await _holder(printer.id)
    assert not spoolman_activity.records_open()
    assert await _events(factory) == [(5, None if changed_between else 17)]


# --- a change rolled back after its predicate passed lets the waiting writer through ------------


async def test_a_rolled_back_change_releases_the_waiting_writer(pg, monkeypatch):
    from backend.app.api.routes import settings as settings_routes
    from backend.app.services import spoolman_activity, spoolman_source

    factory, engine = pg
    printer = await _printer(factory)
    await _printing(factory, printer)
    _queue(printer.id, stamp=spoolman_source.base_generation())

    async def fail_write(*args, **kwargs):
        raise RuntimeError("the change fails after its predicate")

    monkeypatch.setattr(settings_routes, "_write_spoolman_settings", fail_write)
    async with _paused_durable_select(engine) as (wait_inside, release):
        change = asyncio.create_task(_change(factory))
        await wait_inside()
        work = asyncio.create_task(_holder(printer.id))
        await asyncio.sleep(0.05)
        assert not work.done() and not spoolman_activity.records_open()
        await release()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(change, 10)
        await asyncio.wait_for(work, 10)
    assert not spoolman_source.base_change_in_flight()
    assert await _url(factory) == A
    # The base generation moved with the passed predicate; the rolled-back change leaves the stamp
    # erring towards "unknown", never towards a number of another base.
    assert await _events(factory) == [(5, None)]


# --- A07: the RFID doors and the shared journal writer ------------------------------------------


async def _rfid_rig(factory, monkeypatch):
    """A printing print, Spoolman off, a Spoolman binding still on file, a tail feeding from the slot."""
    from types import SimpleNamespace

    from backend.app.models.spoolman_slot_assignment import SpoolmanSlotAssignment
    from backend.app.services import print_usage_journal as journal
    from backend.app.services.printer_manager import printer_manager

    printer = await _printer(factory)
    archive_id = await _printing(factory, printer)
    async with factory() as db:
        db.add(SpoolmanSlotAssignment(printer_id=printer.id, ams_id=0, tray_id=0, spoolman_spool_id=17))
        await db.commit()
    tray = {"id": 0, "tray_type": "PLA", "tag_uid": "12345678ABCDEF00", "tray_color": "FFFFFFFF"}
    status = SimpleNamespace(raw_data={"ams": [{"id": 0, "tray": [tray]}]})
    tail = SimpleNamespace(journal_held=lambda: False, open_tail=lambda: (0, 10))
    monkeypatch.setattr(journal, "_observed_print", lambda _: (tail, 1, None))
    monkeypatch.setattr(printer_manager, "get_status", lambda *_: status)
    monkeypatch.setattr(printer_manager, "get_client", lambda *_: None)
    monkeypatch.setattr(printer_manager, "ensure_fresh_connection", AsyncMock())
    monkeypatch.setattr("backend.app.core.websocket.ws_manager.broadcast", AsyncMock())

    async def from_slot():
        from backend.app.api.routes.inventory import CreateSpoolFromSlotRequest, create_spool_from_slot

        async with factory() as db:
            return await create_spool_from_slot(
                CreateSpoolFromSlotRequest(printer_id=printer.id, ams_id=0, tray_id=0), db=db, _=None
            )

    return printer, archive_id, from_slot


async def _tail_numbers(factory, archive_id):
    from backend.app.models.print_usage_event import EVENT_TAIL_START, PrintUsageEvent

    async with factory() as db:
        return (
            await db.scalars(
                select(PrintUsageEvent.spoolman_spool_id).where(
                    PrintUsageEvent.archive_id == archive_id, PrintUsageEvent.event == EVENT_TAIL_START
                )
            )
        ).all()


async def test_the_rfid_from_slot_waits_for_its_admission_with_the_lane_free(pg, monkeypatch):
    from backend.app import main
    from backend.app.services import spoolman_activity, spoolman_source

    factory, engine = pg
    printer, archive_id, from_slot = await _rfid_rig(factory, monkeypatch)
    async with _paused_durable_select(engine) as (wait_inside, release):
        change = asyncio.create_task(_change(factory))
        await wait_inside()
        assert spoolman_source.base_change_in_flight()
        work = asyncio.create_task(from_slot())
        await asyncio.sleep(0.1)
        assert not work.done() and not spoolman_activity.records_open()
        assert not main._get_ams_assignment_lock(printer.id).locked(), "the lane stays free while it waits"
        assert await _tail_numbers(factory, archive_id) == [], "nothing written inside the old snapshot"
        await release()
        await asyncio.wait_for(asyncio.gather(change, work), 15)
    assert await _url(factory) == B
    # The tail is written after the change committed. Which Spoolman number a binding left over
    # from the old database names is the separate task on leftover bindings (not C047).
    assert len(await _tail_numbers(factory, archive_id)) == 1


async def test_an_rfid_writer_inside_its_transaction_refuses_the_change(pg, monkeypatch):
    from backend.app.services import spool_tag_matcher

    factory, _ = pg
    _printer_row, archive_id, from_slot = await _rfid_rig(factory, monkeypatch)
    inside, resume = asyncio.Event(), asyncio.Event()
    original = spool_tag_matcher.auto_assign_spool

    async def hold(*args, **kwargs):
        result = await original(*args, **kwargs)
        inside.set()
        await resume.wait()
        return result

    monkeypatch.setattr(spool_tag_matcher, "auto_assign_spool", hold)
    work = asyncio.create_task(from_slot())
    try:
        await asyncio.wait_for(inside.wait(), 10)
        with pytest.raises(HTTPException) as refused:
            await asyncio.wait_for(_change(factory), 5)
        assert (refused.value.status_code, refused.value.detail["reason"]) == (409, "print_accounting")
    finally:
        resume.set()
        await asyncio.wait_for(work, 15)
    assert await _url(factory) == A
    assert await _tail_numbers(factory, archive_id) == [17]


async def test_the_shared_writer_never_waits_and_writes_unknown_inside_a_base_change(pg, monkeypatch):
    """A07: a journal write with no admission, while the change's SELECT runs: no wait, no old number."""
    from backend.app.services import print_usage_journal as journal, spoolman_activity

    factory, engine = pg
    printer, archive_id, _from_slot = await _rfid_rig(factory, monkeypatch)
    async with _paused_durable_select(engine) as (wait_inside, release):
        change = asyncio.create_task(_change(factory))
        await wait_inside()
        async with factory() as db:
            await asyncio.wait_for(
                journal.ensure_tail_recorded(db, printer_id=printer.id, ams_id=0, tray_id=0, new_spool_id=99), 2
            )
            await db.commit()
        assert not spoolman_activity.records_open()
        await release()
        await asyncio.wait_for(change, 10)
    assert await _url(factory) == B
    assert await _tail_numbers(factory, archive_id) == [None]


async def test_the_built_in_ams_pass_waits_for_its_admission_with_the_lane_free(pg, monkeypatch):
    from backend.app import main
    from backend.app.services import spoolman_activity

    factory, engine = pg
    printer = await _printer(factory)
    await _printing(factory, printer)
    inner = AsyncMock()
    monkeypatch.setattr(main, "_on_ams_change", inner)
    monkeypatch.setattr("backend.app.services.spool_single_slot.release_elsewhere", AsyncMock())
    monkeypatch.setattr(main.printer_manager, "get_status", lambda *_: None)
    async with _paused_durable_select(engine) as (wait_inside, release):
        change = asyncio.create_task(_change(factory))
        await wait_inside()
        work = asyncio.create_task(main.on_ams_change(printer.id, []))
        await asyncio.sleep(0.1)
        assert not work.done() and not spoolman_activity.records_open()
        assert not main._get_ams_assignment_lock(printer.id).locked()
        inner.assert_not_awaited()
        await release()
        await asyncio.wait_for(asyncio.gather(change, work), 15)
    inner.assert_awaited_once()
    assert await _url(factory) == B


async def test_a_switch_to_spoolman_waits_for_the_rfid_from_slot_past_its_check(pg, monkeypatch):
    """A08 on PostgreSQL: the switch's DELETE would not see a row not committed yet — it waits for it."""
    from backend.app.api.routes.settings import update_spoolman_settings
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services import spool_tag_matcher, spoolman_source

    factory, _ = pg
    _printer_row, _archive_id, from_slot = await _rfid_rig(factory, monkeypatch)
    inside, resume = asyncio.Event(), asyncio.Event()
    original = spool_tag_matcher.auto_assign_spool

    async def hold(*args, **kwargs):
        result = await original(*args, **kwargs)
        inside.set()
        await resume.wait()
        return result

    monkeypatch.setattr(spool_tag_matcher, "auto_assign_spool", hold)

    async def switch_on():
        async with factory() as db:
            return await update_spoolman_settings({"spoolman_enabled": "true"}, db=db, _=None)

    work = asyncio.create_task(from_slot())
    try:
        await asyncio.wait_for(inside.wait(), 10)
        switch = asyncio.create_task(switch_on())
        await asyncio.sleep(0.2)
        assert spoolman_source.mode_change_in_flight()
        assert not switch.done(), "the switch waits for the row past its mode check"
    finally:
        resume.set()
    await asyncio.wait_for(asyncio.gather(work, switch), 15)
    async with factory() as db:
        assert await db.scalar(text("SELECT value FROM settings WHERE key='spoolman_enabled'")) == "true"
        assert (await db.scalars(select(SpoolAssignment.id))).all() == [], "the switch emptied it after the commit"


# --- A13: the Spoolman route stores the mode it decides ---------------------------------------


@pytest.mark.parametrize(
    "body",
    [{"spoolman_enabled": True}, {"spoolman_enabled": "true", "spoolman_disable_weight_sync": True}],
    ids=["json-true", "a-json-switch-beside-it"],
)
async def test_a_json_switch_is_stored_as_the_mode_the_change_decided(pg, body):
    """A13 on PostgreSQL: asyncpg refused a bool for the text column at the write, after ``decide``
    had emptied the built-in ledger and forgotten the overlay — a 500 that rolled the rows back and
    left the overlay empty. The route now stores the strings the change decided."""
    from backend.app.api.routes.settings import update_spoolman_settings
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment
    from backend.app.services import ams_advertised_overlay as overlay, spoolman_source

    factory, _ = pg
    printer = await _printer(factory)
    async with factory() as db:
        spool = Spool(material="PLA", subtype="Basic", brand="Generic", rgba="FF0000FF", label_weight=1000)
        db.add(spool)
        await db.commit()
        db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=1))
        await db.commit()
    entry = overlay.OverlayEntry(
        actual_material="PLA",
        actual_color="FF0000",
        actual_variant="GFL98",
        actual_cols=(),
        advertised_color="FF0000",
        advertised_variant="GFL99",
        source="internal",
    )
    overlay.replace_printer(printer.id, {(0, 1): entry})
    generation = spoolman_source.generation()
    try:
        async with factory() as db:
            result = await update_spoolman_settings(dict(body), db=db, _=None)
        assert result["spoolman_enabled"] == "true"
        async with factory() as db:
            rows = dict((await db.execute(text("SELECT key, value FROM settings"))).all())
            assert rows["spoolman_enabled"] == "true"
            if "spoolman_disable_weight_sync" in body:
                assert rows["spoolman_disable_weight_sync"] == "true"
            assert (await db.scalars(select(SpoolAssignment.id))).all() == []
        assert overlay.entries_for(printer.id) == {}
        assert spoolman_source.generation() == generation + 1
    finally:
        overlay.forget_all()
