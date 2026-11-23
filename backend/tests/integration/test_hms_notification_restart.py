"""Durable deduplication of HMS notices, fed by the normal status callback."""

import asyncio
import os
from types import SimpleNamespace
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
