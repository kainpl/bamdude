"""Durable deduplication of HMS notices, fed by the normal status callback."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app import main
from backend.app.core import database
from backend.app.models.printer import Printer
from backend.app.services import printer_deletion
from backend.app.services.bambu_mqtt import BambuMQTTClient

ARRAY = "0500060000020005"
SCALAR = "07008011"


def fault(code):
    return SimpleNamespace(full_code=code, attr=0, code="0x4001", severity=2)


@pytest.fixture
async def hms_engine(test_engine, monkeypatch):
    url = os.environ.get("HMS_TEST_POSTGRES_URL")
    if not url:
        yield test_engine
        return
    schema = "hms_restart_" + uuid4().hex
    monkeypatch.setattr(database.settings, "database_url", url)
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    event.listen(engine.sync_engine, "before_cursor_execute", database._strip_tz_from_params, retval=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.run_sync(database.Base.metadata.create_all)
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest.fixture
async def session_factory(hms_engine, monkeypatch):
    factory = async_sessionmaker(hms_engine, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    async with factory.begin() as db:
        db.add(Printer(id=426, name="Test", serial_number="test-hms", ip_address="localhost", access_code="00000000"))
    return factory


@pytest.fixture
def restart():
    def clear_memory():
        main._notified_hms_errors.pop(426, None)
        main._hms_print_state.pop(426, None)
        main._hms_forget_when_gone.pop(426, None)
        getattr(main, "_hms_notification_storage", {}).pop(426, None)

    clear_memory()
    yield clear_memory
    clear_memory()


async def record(codes, *, reported=(True, True), state="PAUSE", now=1000.0):
    errors = [fault(code) for code in codes]
    record_durably = getattr(main, "_record_hms_faults", None)
    if record_durably is None:
        return main._take_new_hms_fault_keys(426, errors, state, now=now)
    return await record_durably(426, errors, state, reported, now=now)


async def test_standing_faults_survive_restart_and_partial_reports(session_factory, restart):
    assert await record([ARRAY, SCALAR]) == {ARRAY, SCALAR}
    restart()
    assert await record([], reported=(False, False), now=9000) == set()
    assert await record([ARRAY], reported=(True, False), now=13000) == set()
    assert await record([ARRAY, SCALAR], now=13001) == set()


async def test_a_fault_cleared_across_resume_can_notify_again_after_restart(session_factory, restart):
    assert await record([ARRAY, SCALAR]) == {ARRAY, SCALAR}
    assert await record([ARRAY], reported=(True, False), state="RUNNING", now=1001) == set()
    assert await record([ARRAY], state="RUNNING", now=1002) == set()
    restart()
    assert await record([ARRAY, SCALAR], state="PAUSE", now=1003) == {SCALAR}


async def test_grace_started_by_a_real_clear_survives_restart(session_factory, restart):
    assert await record([ARRAY, SCALAR]) == {ARRAY, SCALAR}
    assert await record([ARRAY], now=1001) == set()
    restart()
    assert await record([ARRAY], reported=(True, False), now=1300) == set()
    assert await record([ARRAY, SCALAR], now=1601) == {SCALAR}


async def test_a_new_printer_with_the_same_id_has_its_own_faults(session_factory, restart):
    assert await record([ARRAY]) == {ARRAY}
    deletion = printer_deletion.begin(426)
    async with session_factory.begin() as db:
        await db.delete(await db.get(Printer, 426))
    printer_deletion.finish(deletion, committed=True)
    async with session_factory.begin() as db:
        db.add(
            Printer(
                id=426,
                name="Another",
                serial_number="replaced-test-hms",
                ip_address="localhost",
                access_code="00000000",
            )
        )
    assert await record([ARRAY], now=1001) == {ARRAY}


async def test_overlapping_status_callbacks_claim_the_notification_once(session_factory, restart):
    first, second = await asyncio.gather(record([ARRAY]), record([ARRAY]))
    assert sorted(map(len, (first, second))) == [0, 1]


async def test_wire_reports_distinguish_partial_status_from_an_explicit_clear(session_factory, restart):
    def connect():
        client = BambuMQTTClient(ip_address="127.0.0.1", serial_number="test-hms", access_code="00000000")
        client.state.state = "PAUSE"
        return client

    async def packet(client, data, *, now):
        client._process_message({"print": data})
        status = client.state
        return await record(
            [error.full_code for error in status.hms_errors],
            reported=(getattr(status, "hms_array_reported", True), getattr(status, "hms_scalar_reported", True)),
            state=status.state,
            now=now,
        )

    array_packet = {"hms": [{"attr": int(ARRAY[:8], 16), "code": int(ARRAY[8:], 16)}]}
    assert await packet(connect(), {**array_packet, "print_error": int(SCALAR, 16)}, now=1000) == {ARRAY, SCALAR}
    restart()
    client = connect()
    assert await packet(client, {"mc_percent": 40}, now=4000) == set()
    assert await packet(client, array_packet, now=5000) == set()
    assert await packet(client, {"print_error": int(SCALAR, 16)}, now=6000) == set()
    assert await packet(client, {"print_error": 0}, now=6001) == set()
    assert [error.full_code for error in client.state.hms_errors] == [ARRAY]
    assert await packet(client, {"print_error": int(SCALAR, 16)}, now=6601) == {SCALAR}


async def test_migration_adds_nullable_storage_to_existing_printer_and_can_replay(hms_engine, session_factory):
    from backend.app.migrations import m256_hms_notified_faults

    async with hms_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE printers DROP COLUMN hms_notified_faults"))
        await m256_hms_notified_faults.upgrade(conn)
        await m256_hms_notified_faults.upgrade(conn)
        row = (await conn.execute(select(Printer.hms_notified_faults).where(Printer.id == 426))).one()
        assert row[0] is None


async def test_a_muted_standing_fault_has_not_cleared(session_factory, restart):
    assert await record([ARRAY]) == {ARRAY}
    restart()
    await main._record_hms_faults(426, [], "PAUSE", (True, True), muted_errors=[fault(ARRAY)], now=2000)
    await main._record_hms_faults(426, [], "PAUSE", (True, True), muted_errors=[fault(ARRAY)], now=3000)
    assert await record([ARRAY], now=3001) == set()


HMS_PACKET = {"hms": [{"attr": int(ARRAY[:8], 16), "code": int(ARRAY[8:], 16)}], "print_error": int(SCALAR, 16)}
HMS_CLEAR = {"hms": [], "print_error": 0}


def push(client, **data):
    client._process_message({"print": data})
    return client.state


async def stored_faults(session_factory):
    async with session_factory() as db:
        return await db.scalar(select(Printer.hms_notified_faults).where(Printer.id == 426))


async def test_hms_clear_and_new_episode_reach_status_callback_without_a_change_to_its_key(
    session_factory, restart, monkeypatch
):
    clock = SimpleNamespace(value=1000.0)
    monkeypatch.setattr(main, "time", SimpleNamespace(time=lambda: clock.value))
    for store in (
        main._last_status_broadcast,
        main._last_printer_state,
        main._last_printer_state_generation,
        main._printer_last_connected,
        main._projection_repeat_available,
    ):
        monkeypatch.setitem(store, 426, None)
    projection = AsyncMock()
    repeat = AsyncMock(return_value=False)
    notices = AsyncMock()
    monkeypatch.setattr(main, "_broadcast_printer_status_projection", projection)
    monkeypatch.setattr(main, "_repeat_available_for", repeat)
    monkeypatch.setattr(main, "_capture_snapshot_for_notification", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "notification_service", notices)

    client = BambuMQTTClient(ip_address="127.0.0.1", serial_number="test-hms", access_code="00000000")
    state = push(client, gcode_state="IDLE", **HMS_PACKET)
    key = main._printer_status_effect_key(state)
    await main.on_printer_status_change(426, state)
    assert await stored_faults(session_factory) == {ARRAY: None, SCALAR: None}
    notices.on_printer_error.assert_awaited_once()
    repeat.assert_awaited_once()

    clock.value = 1001
    state = push(client, **HMS_CLEAR)
    assert main._printer_status_effect_key(state) == key
    await main.on_printer_status_change(426, state)
    assert await stored_faults(session_factory) == {ARRAY: 1000.0, SCALAR: 1000.0}

    clock.value = 1601
    state = push(client, **HMS_PACKET)
    assert main._printer_status_effect_key(state) == key
    await main.on_printer_status_change(426, state)
    assert notices.on_printer_error.await_count == 2
    assert await stored_faults(session_factory) == {ARRAY: None, SCALAR: None}
    repeat.assert_awaited_once()
    assert projection.await_count == 3


@pytest.mark.parametrize(
    ("start_report", "clear_array", "clear_scalar"),
    [(HMS_CLEAR, True, True), ({"print_error": 0}, False, True), ({"hms": []}, True, False), ({}, False, False)],
)
async def test_print_start_uses_only_hms_sources_reported_in_that_frame(
    session_factory, restart, start_report, clear_array, clear_scalar
):
    client = BambuMQTTClient(ip_address="127.0.0.1", serial_number="test-hms", access_code="00000000")
    state = push(client, gcode_state="IDLE", **HMS_PACKET)
    assert await record([ARRAY, SCALAR], state=state.state) == {ARRAY, SCALAR}
    boundary = client._run_boundary_generation

    state = push(client, gcode_state="RUNNING", gcode_file="/cache/fixture.gcode.3mf", subtask_id="51", **start_report)
    assert client._run_boundary_generation == boundary + 1
    assert not state.hms_errors
    assert (state.hms_array_reported, state.hms_scalar_reported) == (clear_array, clear_scalar)
    await record([], reported=(state.hms_array_reported, state.hms_scalar_reported), state=state.state, now=1001)
    cleared = {code for code, reported in ((ARRAY, clear_array), (SCALAR, clear_scalar)) if reported}
    assert set(await stored_faults(session_factory)) == ({ARRAY, SCALAR} - cleared)

    state = push(client, **HMS_PACKET)
    assert await record([error.full_code for error in state.hms_errors], state=state.state, now=1702) == cleared


async def test_print_start_keeps_faults_reported_in_the_new_run():
    client = BambuMQTTClient(ip_address="127.0.0.1", serial_number="test-hms", access_code="00000000")
    push(client, gcode_state="IDLE")
    boundary = client._run_boundary_generation
    state = push(client, gcode_state="RUNNING", gcode_file="/cache/fixture.gcode.3mf", subtask_id="51", **HMS_PACKET)
    assert client._run_boundary_generation == boundary + 1
    assert (state.hms_array_reported, state.hms_scalar_reported) == (True, True)
    assert {error.full_code for error in state.hms_errors} == {ARRAY, SCALAR}
