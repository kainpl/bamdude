"""A deleted printer's runtime is never inherited by the next printer with its id (spec gh109-o1 r2).

SQLite gives the highest deleted id -- of a printer AND of an archive -- to the next row created, so the
process-memory runtime the deletion left behind used to belong to the re-added printer: its first dispatch met the
old current run (the GH109 veto), its queue waited behind the old plate gate, and late work of the old record could
still connect, bind or publish under the id.

Every case runs the real route on the test database; MQTT, devices and files are never reached.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import print_run_binding as prb
from backend.app.services.printer_manager import printer_manager
from backend.tests.integration.test_dispatch_without_original import clean_spool_state, sessions  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing in this module may leave the machine: a connect anywhere but loopback fails the test's code path."""
    import ipaddress
    import socket

    def check(address):
        host = address[0] if isinstance(address, tuple) else address
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            if host == "localhost":
                return
        raise AssertionError(f"a test in this module tried to reach {address!r}")

    real_connect, real_connect_ex, real_create = (
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.create_connection,
    )

    def connect(sock, address):
        check(address)
        return real_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return real_connect_ex(sock, address)

    def create_connection(address, *args, **kwargs):
        check(address)
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)


async def _delete(async_client, printer_id: int, *, delete_archives: bool = True):
    return await async_client.delete(f"/api/v1/printers/{printer_id}?delete_archives={str(delete_archives).lower()}")


async def _seed_two_printers(printer_factory, archive_factory):
    """A kept printer with its own run, and the highest printer holding a current run, a finishing run on the
    highest archive id, an admission and a plate gate -- everything a deletion used to leave behind."""
    kept = await printer_factory(name="kept")
    kept_archive = await archive_factory(kept.id, status="printing")
    doomed = await printer_factory(name="doomed")
    # The finishing run holds the LOWER of the two ids: SQLite hands max(id) + 1 to the next archive, which is
    # exactly the id of the finishing lease's key once both are deleted -- (printer id, archive id) both reused.
    finishing_archive = await archive_factory(doomed.id, status="completed")
    current_archive = await archive_factory(doomed.id, status="printing")
    kept_run = prb.bind_print_run(printer_manager, printer_id=kept.id, archive_id=kept_archive.id, origin="dispatch")
    finishing = prb.bind_print_run(
        printer_manager, printer_id=doomed.id, archive_id=finishing_archive.id, origin="dispatch"
    )
    prb.begin_print_run_finishing(printer_manager, doomed.id, finishing_archive.id)
    current = prb.bind_print_run(
        printer_manager, printer_id=doomed.id, archive_id=current_archive.id, origin="dispatch"
    )
    prb.begin_print_start_resolution(printer_manager, doomed.id, {"subtask_id": "old-subtask"})
    prb.defer_matching_terminal_during_start(printer_manager, doomed.id, {"subtask_id": "old-subtask"})
    printer_manager._awaiting_plate_clear.add(doomed.id)
    printer_manager._last_trays[doomed.id] = {"ams": [{"id": "0"}]}
    printer_manager._current_print_user[doomed.id] = {"user_id": 1, "username": "old"}
    return kept, kept_run, doomed, current, finishing, finishing_archive.id


# --------------------------------------------------------------------------- #
# A01 -- the re-added printer starts its own runtime, ids of printer AND archive reused
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_readded_printer_with_reused_ids_starts_its_own_runtime(
    async_client, printer_factory, archive_factory, db_session
):
    kept, kept_run, doomed, current, finishing, finishing_archive_id = await _seed_two_printers(
        printer_factory, archive_factory
    )
    doomed_id = doomed.id
    old_sequence = printer_manager._print_run_binding_sequence

    response = await _delete(async_client, doomed_id)
    assert response.status_code == 200, response.text

    readded = await printer_factory(name="re-added")
    new_archive = await archive_factory(readded.id, status="printing")
    # Both ids really reused: the new run's (printer, archive) is the old finishing lease's key.
    assert readded.id == doomed_id and new_archive.id == finishing_archive_id

    assert prb.current_print_run(printer_manager, readded.id) is None
    assert prb.finishing_print_runs(printer_manager, readded.id) == ()
    assert not prb.has_runtime_context(printer_manager, readded.id)
    assert not printer_manager.is_awaiting_plate_clear(readded.id)
    assert printer_manager.last_tray_reading(readded.id) == {}
    assert readded.id not in printer_manager._current_print_user

    new = prb.bind_prepared_print_run(
        printer_manager,
        printer_id=readded.id,
        archive_id=new_archive.id,
        expected_submission_id="new-submission",
    )
    assert new is not None and new is not current
    assert new.effect_generation > current.effect_generation > 0
    assert prb.print_effect_is_current(printer_manager, prb.effect_token_for_run(new))
    assert not prb.print_effect_is_current(printer_manager, prb.effect_token_for_run(finishing))
    assert printer_manager._print_run_binding_sequence > old_sequence  # never reset (A06)

    # The new run finishes under the old lease's very key. A consumer of the old run that outlived its bounded stop
    # proves ownership with its own token -- never by finding the lease by (printer, archive) -- and gets nothing.
    assert prb.begin_print_run_finishing(printer_manager, readded.id, new_archive.id) is new
    assert prb.finishing_print_runs(printer_manager, readded.id) == (new,)
    assert not prb.print_effect_is_current(printer_manager, prb.effect_token_for_run(finishing))
    assert prb.print_effect_is_current(printer_manager, prb.effect_token_for_run(new))
    prb.discard_print_run(printer_manager, readded.id, new_archive.id)

    # The other printer keeps its run and its token.
    assert prb.current_print_run(printer_manager, kept.id) is kept_run
    assert prb.print_effect_is_current(printer_manager, prb.effect_token_for_run(kept_run))


# --------------------------------------------------------------------------- #
# A02 -- no confirmed commit, no retirement
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_missing_printer_changes_nothing(async_client):
    before = printer_manager.printer_life(9999)
    response = await _delete(async_client, 9999)
    assert response.status_code == 404
    assert printer_manager.printer_life(9999) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_archives", [True, False])
async def test_a_delete_that_fails_part_way_retires_no_runtime(
    async_client, printer_factory, archive_factory, db_session, monkeypatch, delete_archives
):
    _kept, _kept_run, doomed, current, _finishing, _ = await _seed_two_printers(printer_factory, archive_factory)
    doomed_id = doomed.id
    life_before = printer_manager.printer_life(doomed_id)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("refused part-way")

    monkeypatch.setattr("backend.app.api.routes.printers.scheduled_drying.forget_printer", boom)
    with pytest.raises(RuntimeError, match="refused part-way"):
        await _delete(async_client, doomed_id, delete_archives=delete_archives)

    db_session.expire_all()
    assert (await db_session.execute(select(Printer.id).where(Printer.id == doomed_id))).scalar() == doomed_id
    assert prb.current_print_run(printer_manager, doomed_id) is current  # left for the manual GH109 repair
    assert printer_manager.is_awaiting_plate_clear(doomed_id)
    life = printer_manager.printer_life(doomed_id)
    assert life > life_before and life % 2 == 0  # the deletion ended; work captured before it stays retired
    assert not printer_manager.printer_life_is_current(doomed_id, life_before)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("commit refused"), asyncio.CancelledError()])
async def test_a_commit_whose_outcome_is_not_confirmed_retires_nothing(
    printer_factory, archive_factory, test_engine, monkeypatch, failure
):
    """A commit that raised -- before or after it reached the database, cancellation included -- proves nothing; a
    SELECT by id could not prove it either, the id may already be reused. The binding stays for the manual repair
    (R3). The route function is called directly: the HTTP stack turns a cancellation into another error."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.api.routes.printers import delete_printer

    _kept, _kept_run, doomed, current, _finishing, _ = await _seed_two_printers(printer_factory, archive_factory)
    doomed_id = doomed.id
    real_commit = AsyncSession.commit

    async def commit(self):
        deleting_the_printer = any(isinstance(obj, Printer) for obj in self.deleted)
        await real_commit(self)
        if deleting_the_printer:
            raise failure

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    # The route's helpers (the Obico store among them) open their own sessions off the module factories.
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    monkeypatch.setattr("backend.app.main.async_session", factory)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    async with factory() as db:
        with pytest.raises(type(failure)):
            await delete_printer(doomed_id, delete_archives=False, _=None, db=db)

    assert prb.current_print_run(printer_manager, doomed_id) is current
    assert printer_manager.is_awaiting_plate_clear(doomed_id)
    assert printer_manager.printer_life(doomed_id) % 2 == 0


@pytest.mark.asyncio
async def test_a_cancellation_after_the_commit_still_retires_the_runtime(
    printer_factory, archive_factory, test_engine, monkeypatch
):
    """The cleanup runs with no await after the commit returned, so a cancellation at any later await -- the client
    went away during the virtual-printer sync -- cannot lose it (A07)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from backend.app.api.routes.printers import delete_printer
    from backend.app.services.virtual_printer import virtual_printer_manager

    _kept, _kept_run, doomed, _current, _finishing, _ = await _seed_two_printers(printer_factory, archive_factory)
    doomed_id = doomed.id
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    monkeypatch.setattr("backend.app.main.async_session", factory)

    async def cancelled_sync():
        raise asyncio.CancelledError

    monkeypatch.setattr(virtual_printer_manager, "sync_from_db", cancelled_sync)
    async with factory() as db:
        with pytest.raises(asyncio.CancelledError):
            await delete_printer(doomed_id, delete_archives=False, _=None, db=db)

    assert not prb.has_runtime_context(printer_manager, doomed_id)
    assert not printer_manager.is_awaiting_plate_clear(doomed_id)
    assert printer_manager.printer_life(doomed_id) % 2 == 0


# --------------------------------------------------------------------------- #
# A03 -- a new life that appears between the commit and the cleanup is left alone
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_new_life_between_commit_and_cleanup_is_left_alone(
    async_client, printer_factory, archive_factory, monkeypatch
):
    _kept, _kept_run, doomed, current, _finishing, _ = await _seed_two_printers(printer_factory, archive_factory)
    doomed_id = doomed.id
    real_commit = AsyncSession.commit
    seen = {}

    async def commit(self):
        deleting_the_printer = any(isinstance(obj, Printer) for obj in self.deleted)
        await real_commit(self)
        if deleting_the_printer:
            # The worst ordering SQLite allows: the reused id is created and connected, and its first run bound,
            # before the deletion's own continuation runs.
            client = object()
            monkeypatch.setitem(printer_manager._clients, doomed_id, client)
            printer_manager._client_generations[doomed_id] = printer_manager._client_generations.get(doomed_id, 0) + 1
            printer_manager._models[doomed_id] = "X1C"
            seen["client"] = client
            seen["run"] = prb.bind_print_run(
                printer_manager, printer_id=doomed_id, archive_id=880_001, origin="dispatch"
            )

    monkeypatch.setattr(AsyncSession, "commit", commit)
    response = await _delete(async_client, doomed_id, delete_archives=False)
    assert response.status_code == 200, response.text

    assert prb.current_print_run(printer_manager, doomed_id) is seen["run"]
    assert seen["run"] is not current
    assert prb.print_effect_is_current(printer_manager, prb.effect_token_for_run(seen["run"]))
    assert printer_manager._clients[doomed_id] is seen["client"]
    assert printer_manager._models[doomed_id] == "X1C"
    assert prb.finishing_print_runs(printer_manager, doomed_id) == ()


# --------------------------------------------------------------------------- #
# A10 -- main's aliases of the old record go, another printer's stay
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_main_state_of_the_deleted_record_goes_and_another_printers_stays(
    async_client, printer_factory, archive_factory
):
    from backend.app import main

    kept, _kept_run, doomed, _current, _finishing, _ = await _seed_two_printers(printer_factory, archive_factory)
    for pid in (kept.id, doomed.id):
        main._active_prints[(pid, "plate.3mf")] = 1
        main._expected_prints[(pid, "plate.3mf")] = 2
        main._expected_print_creators[(pid, "plate.3mf")] = 3
        main._active_macro_selection[pid] = [1]
        main._active_swap_config[pid] = {"mode": "x"}
        main._user_stopped_printers[pid] = 4
        main._expected_pause_reasons[pid] = "filament"
        main._last_printer_state[pid] = "RUNNING"
        main._last_printer_state_generation[pid] = 5
        main._inprint_frame_bank[(pid, 6)] = b"frame"
        main._ams_alarm_cooldown[f"{pid}:0:humidity"] = object()

    response = await _delete(async_client, doomed.id, delete_archives=False)
    assert response.status_code == 200, response.text

    for registry, key in (
        (main._active_prints, (doomed.id, "plate.3mf")),
        (main._expected_prints, (doomed.id, "plate.3mf")),
        (main._expected_print_creators, (doomed.id, "plate.3mf")),
        (main._active_macro_selection, doomed.id),
        (main._active_swap_config, doomed.id),
        (main._user_stopped_printers, doomed.id),
        (main._expected_pause_reasons, doomed.id),
        (main._last_printer_state, doomed.id),
        (main._last_printer_state_generation, doomed.id),
        (main._inprint_frame_bank, (doomed.id, 6)),
        (main._ams_alarm_cooldown, f"{doomed.id}:0:humidity"),
    ):
        assert key not in registry, (registry, key)
    assert main._active_prints[(kept.id, "plate.3mf")] == 1
    assert main._active_macro_selection[kept.id] == [1]
    assert main._last_printer_state[kept.id] == "RUNNING"
    assert f"{kept.id}:0:humidity" in main._ams_alarm_cooldown
    main._ams_alarm_cooldown.clear()


# --------------------------------------------------------------------------- #
# A04 / A08 -- a deleted life's queued callbacks never run; an ordinary reconnect changes nothing
# --------------------------------------------------------------------------- #


class _FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.state = type("State", (), {"connected": True, "raw_data": {}})()
        # The real client starts with no observed layer frame; the layer callback hands this to its receiver.
        self._layer_frame_observation = None

    def carry_print_lifecycle_from(self, _prior):
        pass

    def set_muted_hms_codes(self, _codes):
        pass

    def connect(self):
        pass

    def disconnect(self, timeout=0):
        pass

    def journal_generation(self):
        return 0


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
async def fake_mqtt(monkeypatch, test_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    made: list[_FakeClient] = []

    def make(**kwargs):
        client = _FakeClient(**kwargs)
        made.append(client)
        return client

    monkeypatch.setattr("backend.app.services.printer_manager.BambuMQTTClient", make)
    monkeypatch.setattr("backend.app.services.printer_manager.spawn_background_task", lambda coro, **_k: coro.close())
    monkeypatch.setattr(printer_manager, "_loop", asyncio.get_running_loop())
    return made


def _printer(pid: int):
    fields = {"id": pid, "name": "p", "ip_address": "10.0.0.9", "serial_number": "SN1", "access_code": "x"}
    return type("P", (), {**fields, "model": "X1C"})()


@pytest.mark.asyncio
async def test_queued_callbacks_of_a_deleted_life_never_reach_their_receivers(fake_mqtt, monkeypatch):
    received = []

    async def on_start(pid, data):
        received.append(("start", dict(data)))

    async def on_status(pid, state):
        received.append(("status", state))

    async def on_complete(pid, data):
        received.append(("complete", dict(data)))

    async def on_layer(pid, layer, previous, observation=None):
        received.append(("layer", layer))

    monkeypatch.setattr(printer_manager, "_on_print_start", on_start)
    monkeypatch.setattr(printer_manager, "_on_status_change", on_status)
    monkeypatch.setattr(printer_manager, "_on_print_complete", on_complete)
    monkeypatch.setattr(printer_manager, "_on_layer_change", on_layer)
    printer = _printer(4343)
    try:
        assert await printer_manager.connect_printer(printer)
        first = fake_mqtt[-1]
        first.kwargs["on_print_start"]({"subtask_id": "1"})
        await _settle()
        assert [kind for kind, _ in received] == ["start"]

        # An ordinary reconnect is not a deletion: the previous session's late callback still runs (R7).
        received.clear()
        assert await printer_manager.connect_printer(printer)
        first.kwargs["on_print_start"]({"subtask_id": "2"})
        await _settle()
        assert [kind for kind, _ in received] == ["start"]

        received.clear()
        second = fake_mqtt[-1]
        life = printer_manager.begin_printer_deletion(printer.id)
        second.kwargs["on_print_start"]({"subtask_id": "3"})
        second.kwargs["on_state_change"](object())
        second.kwargs["on_print_complete"]({"subtask_id": "3"})
        await _settle()
        assert received == []

        printer_manager.end_printer_deletion(printer.id, life)
        second.kwargs["on_print_start"]({"subtask_id": "4"})
        second.kwargs["on_layer_change"](5, 4)
        await _settle()
        assert received == []
    finally:
        printer_manager.disconnect_printer(printer.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["same-record-control", "next-record"])
async def test_a_queued_plate_gate_write_never_lands_on_the_next_record(
    printer_factory, db_session, reconcile_db, deleted
):
    """The old record armed its plate gate; the Boolean write was still queued when it was deleted (R5)."""
    printer_id = (await printer_factory(awaiting_plate_clear=False)).id
    life = printer_manager.printer_life(printer_id)
    if deleted:
        deletion = printer_manager.begin_printer_deletion(printer_id)
        printer_manager.end_printer_deletion(printer_id, deletion)

    await printer_manager._persist_awaiting_plate_clear(printer_id, True, life=life)

    db_session.expire_all()
    stored = (await db_session.execute(select(Printer.awaiting_plate_clear).where(Printer.id == printer_id))).scalar()
    assert stored is (not deleted)


@pytest.mark.asyncio
async def test_a_reconnect_leaves_a_finishing_run_and_its_effects_alone(fake_mqtt):
    """A08: a normal completion moved A into its finishing lease; the printer reconnects while A's consumers run.
    A reconnect is not a deletion: the lease and its effect token stay, and the record life does not move."""
    printer = _printer(4949)
    run = prb.bind_print_run(printer_manager, printer_id=printer.id, archive_id=31, origin="dispatch")
    assert prb.begin_print_run_finishing(printer_manager, printer.id, 31) is run
    life = printer_manager.printer_life(printer.id)
    try:
        assert await printer_manager.connect_printer(printer)
        assert await printer_manager.connect_printer(printer)  # and once more, replacing the session
        assert prb.finishing_print_runs(printer_manager, printer.id) == (run,)
        assert prb.print_effect_is_current(printer_manager, prb.effect_token_for_run(run))
        assert printer_manager.printer_life(printer.id) == life
    finally:
        printer_manager.disconnect_printer(printer.id)
        prb.discard_print_run(printer_manager, printer.id, 31)


@pytest.mark.asyncio
async def test_a_start_carrying_a_deleted_life_creates_nothing(printer_factory, reconcile_db, monkeypatch):
    from backend.app.main import on_print_start

    monkeypatch.setattr("backend.app.main.spawn_background_task", lambda coro, **_k: coro.close())

    printer = await printer_factory()
    life = printer_manager.printer_life(printer.id)
    deletion = printer_manager.begin_printer_deletion(printer.id)
    printer_manager.end_printer_deletion(printer.id, deletion)

    await on_print_start(printer.id, {"subtask_id": "77", "filename": "x.3mf", "_bamdude_printer_life": life})

    assert prb.start_admission(printer_manager, printer.id) is None
    assert prb.start_resolution(printer_manager, printer.id) is None
    assert prb.current_print_run(printer_manager, printer.id) is None


@pytest.mark.asyncio
async def test_a_deleted_sessions_terminal_is_ignored_after_the_deletion(printer_factory, caplog):
    """The client generation is advanced by the deletion, never removed -- with no entry every generation passes."""
    from backend.app.main import on_print_complete

    printer = await printer_factory()
    printer_manager._client_generations[printer.id] = 3
    run = prb.bind_print_run(printer_manager, printer_id=printer.id, archive_id=1, origin="dispatch")
    life = printer_manager.begin_printer_deletion(printer.id)
    assert printer_manager.current_client_generation(printer.id) == 4
    assert not printer_manager.accepts_client_callback_generation(printer.id, 3)

    with caplog.at_level("WARNING"):
        await on_print_complete(printer.id, {"subtask_id": "x", "_bamdude_client_generation": 3})
    printer_manager.end_printer_deletion(printer.id, life)

    assert prb.current_print_run(printer_manager, printer.id) is run  # not entered into finishing
    assert "stale MQTT client" in caplog.text


# --------------------------------------------------------------------------- #
# A04 / A05 -- reconciliation of a deleted life never touches the next record
# --------------------------------------------------------------------------- #


@pytest.fixture
def reconcile_db(monkeypatch, test_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    monkeypatch.setattr("backend.app.main.async_session", factory)
    return factory


async def _printing_archive(archive_factory, printer_id):
    from datetime import datetime, timedelta, timezone

    return await archive_factory(
        printer_id,
        status="printing",
        filename="plate.gcode.3mf",
        created_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )


async def _status(db_session, archive_id):
    from backend.app.models.archive import PrintArchive

    db_session.expire_all()
    return (await db_session.execute(select(PrintArchive.status).where(PrintArchive.id == archive_id))).scalar()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True], ids=["current-life-control", "deleted-life"])
async def test_a_first_status_of_a_deleted_life_closes_nothing(
    printer_factory, archive_factory, db_session, reconcile_db, stale
):
    from backend.app.services.print_reconciliation import reconcile_printer_prints

    printer = await printer_factory()
    archive = await _printing_archive(archive_factory, printer.id)
    life = printer_manager.printer_life(printer.id)
    if stale:
        deletion = printer_manager.begin_printer_deletion(printer.id)
        printer_manager.end_printer_deletion(printer.id, deletion)

    await reconcile_printer_prints(printer.id, "FINISH", "plate.gcode.3mf", printer_life=life)

    assert await _status(db_session, archive.id) == ("printing" if stale else "completed")


@pytest.mark.asyncio
async def test_a_sweep_overtaken_by_a_deletion_stops_before_closing(
    printer_factory, archive_factory, db_session, reconcile_db, monkeypatch
):
    from backend.app.services import print_reconciliation

    printer = await printer_factory()
    archive = await _printing_archive(archive_factory, printer.id)
    life = printer_manager.printer_life(printer.id)
    real_repair = print_reconciliation._repair_terminal_queue_items

    async def repair_then_deleted(*args, **kwargs):
        result = await real_repair(*args, **kwargs)
        deletion = printer_manager.begin_printer_deletion(printer.id)  # deleted and re-added meanwhile
        printer_manager.end_printer_deletion(printer.id, deletion)
        return result

    monkeypatch.setattr(print_reconciliation, "_repair_terminal_queue_items", repair_then_deleted)
    await print_reconciliation.reconcile_printer_prints(printer.id, "FINISH", "plate.gcode.3mf", printer_life=life)

    assert await _status(db_session, archive.id) == "printing"


# --------------------------------------------------------------------------- #
# A05 -- an adoption held before its archive insert is vetoed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_an_adoption_overtaken_by_a_deletion_inserts_no_archive(
    printer_factory, db_session, reconcile_db, monkeypatch
):
    from backend.app import main
    from backend.app.models.archive import PrintArchive

    printer_id = (await printer_factory()).id
    life = printer_manager.printer_life(printer_id)
    real = main._default_queue_id_for_printer

    async def queue_id_then_deleted(db, printer_id):
        value = await real(db, printer_id)
        deletion = printer_manager.begin_printer_deletion(printer_id)
        printer_manager.end_printer_deletion(printer_id, deletion)
        return value

    monkeypatch.setattr(main, "_default_queue_id_for_printer", queue_id_then_deleted)
    data = {
        "filename": "/data/Metadata/plate_1.gcode",
        "subtask_name": "Bracket",
        "subtask_id": "555",
        "remaining_time": 600,
        "raw_data": {},
        "_bamdude_printer_life": life,
    }
    assert await main._adopt_running_print(printer_id, data, main.logging.getLogger("test")) is None

    db_session.expire_all()
    assert (await db_session.execute(select(PrintArchive.id))).scalars().all() == []
    assert prb.current_print_run(printer_manager, printer_id) is None


# --------------------------------------------------------------------------- #
# A12 -- work holding the old Printer never connects, binds or publishes for the next record
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_stale_connect_never_replaces_the_next_records_client(fake_mqtt, monkeypatch):
    printer = _printer(4545)
    existing = object()
    monkeypatch.setitem(printer_manager._clients, printer.id, existing)
    life = printer_manager.printer_life(printer.id)
    deletion = printer_manager.begin_printer_deletion(printer.id)
    printer_manager.end_printer_deletion(printer.id, deletion)

    assert not await printer_manager.connect_printer(printer, life=life)
    assert printer_manager._clients[printer.id] is existing
    assert fake_mqtt == []
    assert not await printer_manager.ensure_fresh_connection_for_printer(printer, life=life)
    assert printer_manager._clients[printer.id] is existing
    assert not printer_manager.start_print(printer.id, "x.3mf", 1, printer_life=life)


@pytest.mark.asyncio
async def test_a_connect_overtaken_by_a_deletion_registers_no_client(fake_mqtt, monkeypatch):
    """A connect already running when the deletion starts must not register its client afterwards."""
    from backend.app.services import hms_mute

    printer = _printer(4646)

    async def muted_then_deleted(_db, printer_id):
        deletion = printer_manager.begin_printer_deletion(printer_id)
        printer_manager.end_printer_deletion(printer_id, deletion)
        return set()

    monkeypatch.setattr(hms_mute, "load_muted_codes", muted_then_deleted)
    assert not await printer_manager.connect_printer(printer)
    assert printer.id not in printer_manager._clients


@pytest.mark.asyncio
async def test_a_printer_created_with_the_reused_id_right_after_the_commit_connects(fake_mqtt, monkeypatch):
    """The end of a deletion that was running at entry is not a new deletion (A03 for clients)."""
    from backend.app.services import hms_mute

    printer = _printer(4747)
    deletion = printer_manager.begin_printer_deletion(printer.id)

    async def muted_while_the_deletion_ends(_db, printer_id):
        printer_manager.end_printer_deletion(printer_id, deletion)
        return set()

    monkeypatch.setattr(hms_mute, "load_muted_codes", muted_while_the_deletion_ends)
    try:
        assert await printer_manager.connect_printer(printer)
        assert printer.id in printer_manager._clients
    finally:
        printer_manager.disconnect_printer(printer.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "deleted-during-the-boot-wait"])
async def test_a_power_on_wait_connects_only_the_record_it_started_for(monkeypatch, deleted):
    from backend.app.services import print_scheduler as ps

    scheduler = ps.PrintScheduler()
    calls = []
    pid = 4848

    async def connect(printer, *, life=None):
        calls.append(life)
        return True

    class _Service:
        async def get_status(self, _plug):
            return {"reachable": True, "state": "ON"}

        async def turn_on(self, _plug):
            return True

    async def service_for(_plug, _db):
        return _Service()

    async def boot_wait(_seconds):
        if deleted and not calls:
            life = printer_manager.begin_printer_deletion(pid)
            printer_manager.end_printer_deletion(pid, life)

    class _Db:
        async def execute(self, _stmt):
            return type("R", (), {"scalar_one_or_none": lambda self: object()})()

    started_life = printer_manager.printer_life(pid)
    monkeypatch.setattr(printer_manager, "connect_printer", connect)
    monkeypatch.setattr(ps.smart_plug_manager, "get_service_for_plug", service_for)
    monkeypatch.setattr(ps.asyncio, "sleep", boot_wait)

    result = await scheduler._power_on_and_wait(type("Plug", (), {"name": "plug"})(), pid, _Db())

    if deleted:
        assert result is False and calls == []
    else:
        assert result is True and calls == [started_life]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["library", "archive"])
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "deleted-mid-dispatch"])
async def test_a_dispatch_of_a_deleted_printer_never_binds_or_publishes(
    db_session, test_engine, tmp_path, printer_factory, monkeypatch, sessions, kind, deleted
):
    """Upload and preheat take minutes; the printer is deleted (and its id re-used) meanwhile (A12/M9)."""
    from unittest.mock import AsyncMock

    from backend.app.models.printer_queue import PrinterQueue
    from backend.app.services import preheat
    from backend.app.services.background_dispatch import RESPONSE_ACCEPTED
    from backend.tests.integration.test_dispatch_attempt_verdicts import a_job_and_its_dispatch, one_tick

    queue_id, mqtt, _item_id, service, scheduler, spawned = await a_job_and_its_dispatch(
        db_session, test_engine, tmp_path, printer_factory, monkeypatch, kind
    )
    printer_id = (await db_session.get(PrinterQueue, queue_id)).printer_id
    monkeypatch.setattr(service, "_await_print_response", AsyncMock(return_value=RESPONSE_ACCEPTED))
    real_preheat = preheat.preheat_and_soak

    async def preheat_then_deleted(*args, **kwargs):
        result = await real_preheat(*args, **kwargs)
        if deleted:
            life = printer_manager.begin_printer_deletion(printer_id)
            printer_manager.end_printer_deletion(printer_id, life)
        return result

    monkeypatch.setattr(preheat, "preheat_and_soak", preheat_then_deleted)

    await one_tick(scheduler, spawned)

    if deleted:
        mqtt._client.publish.assert_not_called()
        assert prb.current_print_run(printer_manager, printer_id) is None
    else:
        mqtt._client.publish.assert_called_once()


# --------------------------------------------------------------------------- #
# Codex acceptance r1 -- F1: a new life's registrations survive the old deletion; a failed one restores them
# --------------------------------------------------------------------------- #


def _registrations_of(pid, marker):
    """Every id-keyed registry a life fills, with values only this life holds."""
    from backend.app import main
    from backend.app.services import layer_timelapse, preheat, usage_tracker

    session = type("Session", (), {"cleanup": lambda self: None, "marker": marker})()
    main._active_swap_config[pid] = {"life": marker}
    main._active_macro_selection[pid] = [marker]
    main._active_prints[(pid, f"{marker}.3mf")] = marker
    main._last_printer_state[pid] = f"state-{marker}"
    printer_manager._awaiting_plate_clear.add(pid)
    printer_manager._current_print_user[pid] = {"username": marker}
    layer_timelapse._active_sessions[pid] = session
    preheat._pinned[pid] = {marker}
    usage_tracker._active_sessions[pid] = session
    return session


def _registrations_hold(pid, marker, session):
    from backend.app import main
    from backend.app.services import layer_timelapse, preheat, usage_tracker

    lost = []
    if main._active_swap_config.get(pid) != {"life": marker}:
        lost.append("swap config")
    if main._active_macro_selection.get(pid) != [marker]:
        lost.append("macro selection")
    if main._active_prints.get((pid, f"{marker}.3mf")) != marker:
        lost.append("print alias")
    if main._last_printer_state.get(pid) != f"state-{marker}":
        lost.append("last state")
    if not printer_manager.is_awaiting_plate_clear(pid):
        lost.append("plate gate")
    if printer_manager._current_print_user.get(pid, {}).get("username") != marker:
        lost.append("author")
    if layer_timelapse._active_sessions.get(pid) is not session:
        lost.append("timelapse session")
    if preheat._pinned.get(pid) != {marker}:
        lost.append("preheat pin")
    if usage_tracker._active_sessions.get(pid) is not session:
        lost.append("usage session")
    return lost


@pytest.mark.asyncio
async def test_a_new_lifes_registrations_survive_the_old_deletion(
    async_client, printer_factory, archive_factory, monkeypatch
):
    """The A03 seam for every id-keyed registry, not only the binding (Codex acceptance O1-F1)."""
    from backend.app import main
    from backend.app.services import layer_timelapse, preheat, usage_tracker

    _kept, _kept_run, doomed, _current, _finishing, _ = await _seed_two_printers(printer_factory, archive_factory)
    pid = doomed.id
    _registrations_of(pid, "old")
    real_commit = AsyncSession.commit
    seen = {}

    async def commit(self):
        deleting_the_printer = any(isinstance(obj, Printer) for obj in self.deleted)
        await real_commit(self)
        if deleting_the_printer:
            monkeypatch.setitem(printer_manager._clients, pid, object())
            seen["run"] = prb.bind_print_run(printer_manager, printer_id=pid, archive_id=880_001, origin="dispatch")
            seen["session"] = _registrations_of(pid, "new")

    monkeypatch.setattr(AsyncSession, "commit", commit)
    try:
        response = await _delete(async_client, pid, delete_archives=False)
        assert response.status_code == 200, response.text

        assert prb.current_print_run(printer_manager, pid) is seen["run"]
        assert _registrations_hold(pid, "new", seen["session"]) == []
        assert main._active_prints.get((pid, "old.3mf")) is None  # the old life's own went with it
    finally:
        for registry in (layer_timelapse._active_sessions, preheat._pinned, usage_tracker._active_sessions):
            registry.pop(pid, None)


@pytest.mark.asyncio
async def test_a_failed_deletion_gives_the_printer_back_its_registrations(
    async_client, printer_factory, archive_factory, monkeypatch
):
    """Taken out when the deletion begins, so nothing of a newer life is ever touched -- and put back when it fails."""
    from backend.app.services import layer_timelapse, preheat, usage_tracker

    _kept, _kept_run, doomed, current, _finishing, _ = await _seed_two_printers(printer_factory, archive_factory)
    pid = doomed.id
    session = _registrations_of(pid, "old")

    async def boom(*_args, **_kwargs):
        raise RuntimeError("refused part-way")

    monkeypatch.setattr("backend.app.api.routes.printers.scheduled_drying.forget_printer", boom)
    try:
        with pytest.raises(RuntimeError, match="refused part-way"):
            await _delete(async_client, pid, delete_archives=False)

        assert prb.current_print_run(printer_manager, pid) is current
        assert _registrations_hold(pid, "old", session) == []
    finally:
        for registry in (layer_timelapse._active_sessions, preheat._pinned, usage_tracker._active_sessions):
            registry.pop(pid, None)


# --------------------------------------------------------------------------- #
# Codex acceptance r1 -- F2: a callback that awaited across the deletion stops before the next run's macros
# --------------------------------------------------------------------------- #


def _production_layer_callback(monkeypatch):
    """``main``'s layer callback as the manager receives it -- no lifespan, no devices, no frame banking.

    It was nested in the lifespan and compiled out of the source here; since the live layer total work it is the
    module-level ``on_printer_layer_change``, so the real function runs with only its frame bank stubbed.
    """
    from backend.app import main

    async def no_bank(*_args):
        return None

    monkeypatch.setattr(main, "_maybe_bank_inprint_frame", no_bank)
    return main.on_printer_layer_change


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "deleted-during-the-timelapse-await"])
async def test_a_layer_callback_overtaken_by_a_deletion_reaches_no_macro(monkeypatch, deleted):
    from backend.app.services import layer_timelapse, macro_trigger, printer_deletion

    callback = _production_layer_callback(monkeypatch)
    entered, proceed = asyncio.Event(), asyncio.Event()
    seen = []

    async def timelapse(*_args):
        entered.set()
        await proceed.wait()

    async def fire(pid, *_args):
        seen.append(prb.current_print_run(printer_manager, pid))

    monkeypatch.setattr(layer_timelapse, "on_layer_change", timelapse)
    monkeypatch.setattr(macro_trigger, "fire_layer_macros", fire)
    pid = 54_321
    old = prb.bind_print_run(printer_manager, printer_id=pid, archive_id=111, origin="external")
    life = printer_manager.printer_life(pid)
    task = asyncio.create_task(printer_manager._run_for_client(pid, life, callback(pid, 20, 19)))
    await asyncio.wait_for(entered.wait(), 2)
    if deleted:
        printer_deletion.finish(printer_deletion.begin(pid), committed=True)
        prb.bind_print_run(printer_manager, printer_id=pid, archive_id=222, origin="external")
    proceed.set()
    await asyncio.wait_for(task, 2)

    assert seen == ([] if deleted else [old])


@pytest.mark.asyncio
async def test_a_session_callback_of_a_deleted_record_gets_no_client_or_status(monkeypatch):
    """The readers every effect goes through refuse the stale session; outside a callback they answer as before."""
    pid = 54_322
    client = type("C", (), {"state": object(), "check_staleness": lambda self: None})()
    monkeypatch.setitem(printer_manager._clients, pid, client)
    seen = {}
    life = printer_manager.printer_life(pid)

    async def callback():
        seen["before"] = (printer_manager.get_client(pid), printer_manager.get_status(pid))
        printer_manager.end_printer_deletion(pid, printer_manager.begin_printer_deletion(pid))
        seen["after"] = (printer_manager.get_client(pid), printer_manager.get_status(pid))
        seen["other"] = printer_manager.session_still_lives(pid + 1)

    await printer_manager._run_for_client(pid, life, callback())

    assert seen["before"] == (client, client.state)
    assert seen["after"] == (None, None)
    assert seen["other"] is True
    assert printer_manager.get_client(pid) is client  # outside any session callback


# --------------------------------------------------------------------------- #
# Codex acceptance r1 -- F3: the closer proves the record and the archive again under the writer
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "deleted-while-locking"])
async def test_the_closer_rechecks_the_record_after_its_write_lock(
    db_session, printer_factory, archive_factory, monkeypatch, deleted, test_engine
):
    from backend.app.core import database
    from backend.app.models.archive import PrintArchive
    from backend.app.services import print_reconciliation
    from backend.tests.integration.test_reconcile_retires_its_own_run import a_bound_printing_run

    factory = async_sessionmaker_for(test_engine, monkeypatch)
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory)
    life = printer_manager.printer_life(a.printer_id)
    real = database.take_write_lock
    triggered = []

    async def lock(db, table, row_id=None):
        if table.name == "print_archives" and not triggered:
            triggered.append(True)
            if deleted:
                printer_manager.end_printer_deletion(a.printer_id, printer_manager.begin_printer_deletion(a.printer_id))
        return await real(db, table, row_id)

    monkeypatch.setattr(database, "take_write_lock", lock)
    await print_reconciliation.reconcile_printer_prints(a.printer_id, "FINISH", "cube.gcode.3mf", printer_life=life)

    assert triggered
    async with factory() as db:
        assert (await db.get(PrintArchive, a.archive_id)).status == ("printing" if deleted else "completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("life_moves", [True, False], ids=["record-deleted", "only-the-row-replaced"])
async def test_a_stale_closer_never_closes_the_archive_that_reused_the_id(
    db_session, printer_factory, archive_factory, monkeypatch, test_engine, life_moves
):
    """Real SQL on another session at the closer's lock seam: both ids reused for a new machine and a new run.
    The life check and the row identity each stop it on their own."""
    from sqlalchemy import insert

    from backend.app.core import database
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services import print_reconciliation
    from backend.tests.integration.test_reconcile_retires_its_own_run import a_bound_printing_run

    factory = async_sessionmaker_for(test_engine, monkeypatch)
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory)
    life = printer_manager.printer_life(a.printer_id)
    real = database.take_write_lock
    replaced = []

    async def lock(db, table, row_id=None):
        if table.name == "print_archives" and not replaced:
            replaced.append(True)
            mark = printer_manager.begin_printer_deletion(a.printer_id) if life_moves else None
            async with factory() as other:
                await other.execute(delete(PrintQueueItem).where(PrintQueueItem.queue_id == a.printer_id))
                await other.execute(delete(PrinterQueue).where(PrinterQueue.id == a.printer_id))
                await other.execute(delete(PrintArchive).where(PrintArchive.id == a.archive_id))
                await other.execute(delete(Printer).where(Printer.id == a.printer_id))
                await other.execute(
                    insert(Printer).values(
                        id=a.printer_id,
                        name="replacement-machine",
                        ip_address="192.0.2.99",
                        serial_number="REPLACEMENT",
                        access_code="test",
                    )
                )
                await other.execute(
                    insert(PrintArchive).values(
                        id=a.archive_id,
                        printer_id=a.printer_id,
                        filename="new-run.3mf",
                        file_path="",
                        file_size=0,
                        status="printing",
                    )
                )
                await other.commit()
            if mark is not None:
                printer_manager.end_printer_deletion(a.printer_id, mark)
        return await real(db, table, row_id)

    monkeypatch.setattr(database, "take_write_lock", lock)
    await print_reconciliation.reconcile_printer_prints(a.printer_id, "FINISH", "cube.gcode.3mf", printer_life=life)

    async with factory() as db:
        archive = await db.get(PrintArchive, a.archive_id)
        assert archive.filename == "new-run.3mf"
        assert archive.status == "printing"


def async_sessionmaker_for(test_engine, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    monkeypatch.setattr("backend.app.main.async_session", factory)
    monkeypatch.setattr("backend.app.core.tasks.spawn_background_task", lambda coro, **_k: coro.close())
    return factory


# --------------------------------------------------------------------------- #
# Codex recheck r1 -- R1-1: the status callback stops at every await, not only at its two ends
# --------------------------------------------------------------------------- #


def _status_barrier(monkeypatch, target_attr, owner):
    """Make ``owner.target_attr`` an await that holds until released."""
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def held(*_args, **_kwargs):
        entered.set()
        await proceed.wait()

    monkeypatch.setattr(owner, target_attr, held)
    return entered, proceed


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "deleted-during-the-relay"])
async def test_a_status_callback_overtaken_at_its_relay_leaves_the_next_life_alone(monkeypatch, deleted):
    from types import SimpleNamespace

    from backend.app import main
    from backend.app.services import interrupted_dispatch, macro_trigger, printer_deletion

    pid = 91_234
    entered, proceed = _status_barrier(monkeypatch, "on_printer_status", main.mqtt_relay)
    monkeypatch.setattr(interrupted_dispatch, "on_status", lambda *_a: None)
    monkeypatch.setattr(printer_manager, "get_printer", lambda _p: SimpleNamespace(name="t", serial_number="t"))

    async def no_ws(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main.ws_manager, "send_printer_status", no_ws)
    state = main.PrinterState()
    state.state = "IDLE"
    state.progress = 0
    life = printer_manager.printer_life(pid)
    task = asyncio.create_task(printer_manager._run_for_client(pid, life, main.on_printer_status_change(pid, state)))
    await asyncio.wait_for(entered.wait(), 3)
    if deleted:
        printer_deletion.finish(printer_deletion.begin(pid), committed=True)
    # The next life's registrations (or, in the control, the state an ordinary IDLE edge moves on from).
    main._last_printer_state[pid] = "RUNNING"
    main._first_layer_notified[pid] = True
    main._last_progress_milestone[pid] = 50
    macro_trigger._fired_layer_macros[pid] = {666}
    proceed.set()
    await asyncio.wait_for(task, 3)
    try:
        actual = (
            main._last_printer_state.get(pid),
            main._first_layer_notified.get(pid),
            main._last_progress_milestone.get(pid),
            macro_trigger._fired_layer_macros.get(pid),
        )
        assert actual == (("RUNNING", True, 50, {666}) if deleted else ("IDLE", False, 0, None))
    finally:
        macro_trigger._fired_layer_macros.pop(pid, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "deleted-during-the-relay"])
async def test_an_old_pause_report_after_the_relay_opens_no_pause_on_the_next_run(monkeypatch, deleted):
    """The next printer is RUNNING when the deleted one's PAUSE report resumes: no pause edge for the new run."""
    from types import SimpleNamespace

    from backend.app import main
    from backend.app.services import interrupted_dispatch, printer_deletion

    pid = 91_235
    entered, proceed = _status_barrier(monkeypatch, "on_printer_status", main.mqtt_relay)
    monkeypatch.setattr(interrupted_dispatch, "on_status", lambda *_a: None)
    monkeypatch.setattr(printer_manager, "get_printer", lambda _p: SimpleNamespace(name="t", serial_number="t"))
    edges = []

    async def pause_edge(printer_id, _state):
        edges.append(printer_id)

    async def no_ws(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main, "_handle_pause_edge", pause_edge)
    monkeypatch.setattr(main.ws_manager, "send_printer_status", no_ws)
    state = main.PrinterState()
    state.state = "PAUSE"
    life = printer_manager.printer_life(pid)
    task = asyncio.create_task(printer_manager._run_for_client(pid, life, main.on_printer_status_change(pid, state)))
    await asyncio.wait_for(entered.wait(), 3)
    if deleted:
        printer_deletion.finish(printer_deletion.begin(pid), committed=True)
    main._last_printer_state[pid] = "RUNNING"
    proceed.set()
    await asyncio.wait_for(task, 3)

    assert edges == ([] if deleted else [pid])


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", ["pause", "resume"])
async def test_a_pause_or_resume_edge_overtaken_by_a_deletion_records_nothing_on_the_next_run(
    monkeypatch, printer_factory, reconcile_db, edge
):
    """The edge helpers look the active archive up by id after an await: never the next printer's."""
    from backend.app import main
    from backend.app.services import print_usage_journal, printer_deletion

    pid = (await printer_factory()).id
    recorded, sent = [], []
    looked_up = asyncio.Event()

    async def active_archive_id(_db, _pid):
        # The await the deletion overtakes: the id now names the next printer, whose archive this would return.
        printer_deletion.finish(printer_deletion.begin(pid), committed=True)
        looked_up.set()
        return 777

    async def record_event(*_args, **kwargs):
        recorded.append(kwargs.get("archive_id"))

    async def ws(*_args, **_kwargs):
        sent.append(True)

    monkeypatch.setattr(print_usage_journal, "active_archive_id", active_archive_id)
    monkeypatch.setattr(print_usage_journal, "record_event", record_event)
    monkeypatch.setattr(main.ws_manager, "send_print_paused", ws)
    monkeypatch.setattr(main.ws_manager, "send_print_resumed", ws)
    state = main.PrinterState()
    state.state = "PAUSE" if edge == "pause" else "RUNNING"
    life = printer_manager.printer_life(pid)
    if edge == "pause":
        callback = main._handle_pause_edge(pid, state)
    else:
        main._pause_started_at[pid] = 1.0
        callback = main._handle_resume_edge(pid, state, stock_resume_witnessed=False)

    await printer_manager._run_for_client(pid, life, callback)

    assert looked_up.is_set()
    assert recorded == [] and sent == []


# --------------------------------------------------------------------------- #
# Codex recheck r1 -- R1-2: after its own archive commit the closer proves the record and the row again
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", ["after-the-archive-commit", "during-the-plate-gate"])
async def test_after_its_archive_commit_the_closer_touches_nothing_of_the_next_printer(
    db_session, printer_factory, archive_factory, monkeypatch, test_engine, seam
):
    """Both ids, the printer, its archive, queue and queue row replaced after the closer's own commit -- right after
    it, or while the plate gate is being armed. Each later write proves the record and the row again."""
    from sqlalchemy import insert, update

    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services import print_reconciliation, printer_deletion
    from backend.tests.integration.test_reconcile_retires_its_own_run import a_bound_printing_run

    factory = async_sessionmaker_for(test_engine, monkeypatch)
    a = await a_bound_printing_run(db_session, printer_factory, archive_factory)
    life = printer_manager.printer_life(a.printer_id)
    real_commit = AsyncSession.commit
    real_arm = printer_manager.arm_awaiting_plate_clear
    replaced = []

    async def commit(self):
        closing = any(
            isinstance(obj, PrintArchive) and obj.id == a.archive_id and obj.status == "completed" for obj in self.dirty
        )
        await real_commit(self)
        if seam == "after-the-archive-commit" and closing and not replaced:
            await replace()

    async def arm(printer_id, archive_id):
        if seam == "during-the-plate-gate" and not replaced:
            await replace()
        return await real_arm(printer_id, archive_id)

    async def replace():
        replaced.append(True)
        mark = printer_deletion.begin(a.printer_id)
        async with factory() as other:
            await other.execute(delete(PrintQueueItem).where(PrintQueueItem.queue_id == a.printer_id))
            await other.execute(delete(PrinterQueue).where(PrinterQueue.id == a.printer_id))
            await other.execute(delete(PrintArchive).where(PrintArchive.id == a.archive_id))
            await other.execute(delete(Printer).where(Printer.id == a.printer_id))
            await other.execute(
                insert(Printer).values(
                    id=a.printer_id,
                    name="replacement",
                    serial_number="NEW",
                    ip_address="192.0.2.99",
                    access_code="test",
                    require_plate_clear=True,
                )
            )
            await other.execute(
                insert(PrintArchive).values(
                    id=a.archive_id,
                    printer_id=a.printer_id,
                    filename="new-run.3mf",
                    file_path="",
                    file_size=0,
                    status="printing",
                )
            )
            await other.execute(
                insert(PrinterQueue).values(id=a.printer_id, printer_id=a.printer_id, status="printing")
            )
            await other.execute(
                insert(PrintQueueItem).values(
                    id=a.item_id, queue_id=a.printer_id, archive_id=a.archive_id, status="printing", position=0
                )
            )
            await other.execute(
                update(PrinterQueue).where(PrinterQueue.id == a.printer_id).values(current_item_id=a.item_id)
            )
            await other.commit()
        printer_deletion.finish(mark, committed=True)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(printer_manager, "arm_awaiting_plate_clear", arm)
    await print_reconciliation.reconcile_printer_prints(a.printer_id, "FINISH", "cube.gcode.3mf", printer_life=life)

    assert replaced
    async with factory() as db:
        printer = await db.get(Printer, a.printer_id)
        archive = await db.get(PrintArchive, a.archive_id)
        item = await db.get(PrintQueueItem, a.item_id)
        queue = await db.get(PrinterQueue, a.printer_id)
        assert (archive.filename, archive.status) == ("new-run.3mf", "printing")
        assert (bool(printer.awaiting_plate_clear), item.status, queue.status) == (False, "printing", "printing")


# --------------------------------------------------------------------------- #
# Codex recheck r2 -- R2-1/R2-2: a write after a read holds the record first and proves it under that hold; memory
# is published only for a record that still lives
# --------------------------------------------------------------------------- #


async def _replace_printer_and_run(factory, pid, aid, *, require_plate_clear=False, started=None):
    """Delete the printer and its run and re-add both under the same ids, in the deletion's own lock order: the
    printer's row first (``lock_sensor_target``, as ``_delete_printer_rows`` takes it), then the rows."""
    from sqlalchemy import insert

    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_usage_event import PrintUsageEvent
    from backend.app.services import printer_deletion
    from backend.app.services.sensor_target_lock import lock_sensor_target

    mark = printer_deletion.begin(pid)
    async with factory() as db:
        if started is not None:
            started.set()
        assert await lock_sensor_target(db, "printers", pid)
        await db.execute(delete(PrintUsageEvent).where(PrintUsageEvent.archive_id == aid))
        await db.execute(delete(PrintArchive).where(PrintArchive.id == aid))
        await db.execute(delete(Printer).where(Printer.id == pid))
        await db.execute(
            insert(Printer).values(
                id=pid,
                name="replacement",
                serial_number="NEW",
                ip_address="192.0.2.99",
                access_code="test",
                require_plate_clear=require_plate_clear,
            )
        )
        await db.execute(
            insert(PrintArchive).values(
                id=aid, printer_id=pid, filename="new.3mf", file_path="", file_size=0, status="printing"
            )
        )
        await db.commit()
    printer_deletion.finish(mark, committed=True)


def _quiet_edges(monkeypatch):
    from backend.app import main

    async def nothing(*_args, **_kwargs):
        return None

    for name in ("send_print_paused", "send_print_resumed"):
        monkeypatch.setattr(main.ws_manager, name, nothing)
    for name in ("on_print_pause", "on_print_resume"):
        monkeypatch.setattr(main.notification_service, name, nothing)


def _edge_callback(pid, edge):
    from backend.app import main

    state = main.PrinterState()
    state.tray_now = 0
    state.state = "PAUSE" if edge == "pause" else "RUNNING"
    if edge == "pause":
        return main._handle_pause_edge(pid, state)
    main._pause_started_at[pid] = 1.0
    return main._handle_resume_edge(pid, state, stock_resume_witnessed=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "replaced-in-the-auto-eject-read"])
async def test_a_plate_gate_overtaken_in_its_auto_eject_read_arms_nothing_on_the_next_printer(
    printer_factory, archive_factory, test_engine, monkeypatch, deleted
):
    """The gate reads the archive's auto-eject intent after its record check. A replacement made during that read
    leaves the loaded row naming the next printer: its gate is written only after the record is held and proved."""
    from backend.app.models.archive import PrintArchive

    factory = async_sessionmaker_for(test_engine, monkeypatch)
    monkeypatch.setattr(printer_manager, "_loop", None)
    printer = await printer_factory(require_plate_clear=False)
    archive = await archive_factory(
        printer.id, status="completed", extra_data={"dispatch_intent": {"auto_eject": True}}
    )
    pid, aid = printer.id, archive.id
    life = printer_manager.printer_life(pid)
    real_get = AsyncSession.get
    read = []

    async def get(self, model, key, **kwargs):
        value = await real_get(self, model, key, **kwargs)
        if model is PrintArchive and key == aid and not read:
            read.append(True)
            if deleted:
                await _replace_printer_and_run(factory, pid, aid)
        return value

    monkeypatch.setattr(AsyncSession, "get", get)
    with printer_manager.session_scope(pid, life):
        token = await printer_manager.arm_awaiting_plate_clear(pid, aid)

    assert read
    async with factory() as db:
        row = await real_get(db, Printer, pid)
        assert (row.name == "replacement", bool(row.awaiting_plate_clear)) == (deleted, not deleted)
    assert (token is None) is deleted
    assert printer_manager.is_awaiting_plate_clear(pid) is (not deleted)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "replaced-after-the-gate-commit"])
async def test_a_plate_gate_overtaken_after_its_commit_publishes_nothing_for_the_next_printer(
    printer_factory, archive_factory, test_engine, monkeypatch, deleted
):
    """The gate's commit and the session's close both await before the gate reaches memory: a replacement there must
    not find the deleted record's gate armed for it in process memory while its own row says no gate."""
    factory = async_sessionmaker_for(test_engine, monkeypatch)
    monkeypatch.setattr(printer_manager, "_loop", None)
    printer = await printer_factory(require_plate_clear=True)
    archive = await archive_factory(printer.id, status="completed")
    pid, aid = printer.id, archive.id
    life = printer_manager.printer_life(pid)
    real_commit = AsyncSession.commit
    committed = []

    async def commit(self):
        await real_commit(self)
        if not committed:
            committed.append(True)
            if deleted:
                await _replace_printer_and_run(factory, pid, aid, require_plate_clear=True)

    monkeypatch.setattr(AsyncSession, "commit", commit)
    with printer_manager.session_scope(pid, life):
        token = await printer_manager.arm_awaiting_plate_clear(pid, aid)

    assert committed
    async with factory() as db:
        row = await db.get(Printer, pid)
        assert (row.name == "replacement", bool(row.awaiting_plate_clear)) == (deleted, not deleted)
    assert (token is None) is deleted
    assert printer_manager.is_awaiting_plate_clear(pid) is (not deleted)


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", ["pause", "resume"])
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "replaced-in-the-tray-read"])
async def test_a_pause_or_resume_journal_overtaken_in_its_tray_read_records_nothing_on_the_next_run(
    printer_factory, archive_factory, test_engine, monkeypatch, edge, deleted
):
    """After the record check the edge reads the loaded tray's spool bindings. A replacement during that read gives
    the archive id to the next printer's run: the journal row is written only after the record is held and proved."""
    from backend.app import main
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_usage_event import PrintUsageEvent

    factory = async_sessionmaker_for(test_engine, monkeypatch)
    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="printing")
    pid, aid = printer.id, archive.id
    life = printer_manager.printer_life(pid)
    real_tray = main._current_tray_frozen
    read = []

    async def tray(*args):
        value = await real_tray(*args)
        read.append(True)
        if deleted:
            await _replace_printer_and_run(factory, pid, aid)
        return value

    monkeypatch.setattr(main, "_current_tray_frozen", tray)
    _quiet_edges(monkeypatch)
    await printer_manager._run_for_client(pid, life, _edge_callback(pid, edge))

    assert read
    async with factory() as db:
        row = await db.get(PrintArchive, aid)
        events = list((await db.scalars(select(PrintUsageEvent.event).where(PrintUsageEvent.archive_id == aid))).all())
    assert row.filename == ("new.3mf" if deleted else archive.filename)
    assert len(events) == (0 if deleted else 1), events


@asynccontextmanager
async def _separate_connections(test_engine, tmp_path):
    """An engine whose sessions hold connections of their own, so one can wait on another's lock. The in-memory test
    engine shares one connection between them; PostgreSQL's test engine already pools."""
    if test_engine.dialect.name != "sqlite":
        yield test_engine
        return
    from sqlalchemy import event as sql_event
    from sqlalchemy.ext.asyncio import create_async_engine

    from backend.app.core.database import Base, configure_sqlite_connection

    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "record-hold.sqlite"))
    sql_event.listen(engine.sync_engine, "connect", configure_sqlite_connection)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()


async def _seed_printer_and_run(factory, *, status, require_plate_clear=False):
    from backend.app.models.archive import PrintArchive

    async with factory() as db:
        printer = Printer(
            name="held",
            serial_number="HELD",
            ip_address="192.0.2.10",
            access_code="test",
            require_plate_clear=require_plate_clear,
        )
        db.add(printer)
        await db.flush()
        archive = PrintArchive(printer_id=printer.id, filename="old.3mf", file_path="", file_size=0, status=status)
        db.add(archive)
        await db.commit()
        return printer.id, archive.id


async def _blocked_or_done(engine, task, started):
    """Until the deletion waits on a lock or has finished. PostgreSQL names the waiter; SQLite's busy wait shows
    nothing, so a deletion that has not finished well after reaching its lock is taken as waiting."""
    await asyncio.wait_for(started.wait(), 10)
    if engine.dialect.name == "postgresql":
        from sqlalchemy import text

        for _ in range(1000):
            if task.done():
                return
            async with AsyncSession(engine) as observer:
                waiting = await observer.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock'"
                    )
                )
            if waiting:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the deletion neither waited nor finished")
    for _ in range(40):
        if task.done():
            return
        await asyncio.sleep(0.01)


def _deletion_at_the_first_commit(engine, factory, pid, aid, seen, **replace):
    """Patch ``AsyncSession.commit``: the operation's first commit starts a deletion of the record -- begun after the
    operation proved it -- lets it run until it waits or ends, notes which, then commits and lets it finish."""
    real_commit = AsyncSession.commit

    async def commit(self):
        if seen:
            return await real_commit(self)
        # Marked before anything awaits: the deletion's own commit comes through here too.
        seen["deletion_finished_first"] = None
        started = asyncio.Event()
        task = asyncio.create_task(
            _replace_printer_and_run(factory, pid, aid, started=started, **replace), context=contextvars.Context()
        )
        await _blocked_or_done(engine, task, started)
        seen["deletion_finished_first"] = task.done()
        await real_commit(self)
        await task

    return commit


@pytest.mark.asyncio
async def test_a_deletion_begun_after_the_gates_proof_waits_for_its_commit(test_engine, tmp_path, monkeypatch):
    """The hold is what makes the proof last: a deletion that begins after it waits for the gate's commit, so the
    write lands on the record it was proved for -- and the gate never reaches memory for the record that follows."""
    async with _separate_connections(test_engine, tmp_path) as engine:
        factory = async_sessionmaker_for(engine, monkeypatch)
        monkeypatch.setattr(printer_manager, "_loop", None)
        pid, aid = await _seed_printer_and_run(factory, status="completed", require_plate_clear=True)
        life = printer_manager.printer_life(pid)
        seen = {}
        monkeypatch.setattr(
            AsyncSession,
            "commit",
            _deletion_at_the_first_commit(engine, factory, pid, aid, seen, require_plate_clear=True),
        )
        with printer_manager.session_scope(pid, life):
            token = await printer_manager.arm_awaiting_plate_clear(pid, aid)

        assert seen == {"deletion_finished_first": False}
        async with factory() as db:
            row = await db.get(Printer, pid)
            assert (row.name, bool(row.awaiting_plate_clear)) == ("replacement", False)
        assert token is None and not printer_manager.is_awaiting_plate_clear(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", ["pause", "resume"])
async def test_a_deletion_begun_after_the_journals_proof_waits_for_its_commit(test_engine, tmp_path, monkeypatch, edge):
    """The journal row is written under the record's hold: a deletion begun after the proof waits for it, and the row
    goes with the deleted run -- never onto the run that takes its id."""
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_usage_event import PrintUsageEvent

    async with _separate_connections(test_engine, tmp_path) as engine:
        factory = async_sessionmaker_for(engine, monkeypatch)
        pid, aid = await _seed_printer_and_run(factory, status="printing")
        life = printer_manager.printer_life(pid)
        seen = {}
        monkeypatch.setattr(AsyncSession, "commit", _deletion_at_the_first_commit(engine, factory, pid, aid, seen))
        _quiet_edges(monkeypatch)
        await printer_manager._run_for_client(pid, life, _edge_callback(pid, edge))

        assert seen == {"deletion_finished_first": False}
        async with factory() as db:
            row = await db.get(PrintArchive, aid)
            events = list((await db.scalars(select(PrintUsageEvent.id).where(PrintUsageEvent.archive_id == aid))).all())
        assert (row.filename, events) == ("new.3mf", [])


@pytest.mark.asyncio
async def test_a_deletion_begun_after_a_queued_gate_writes_proof_waits_for_its_commit(
    test_engine, tmp_path, monkeypatch
):
    """The gate's queued Boolean write (``set_awaiting_plate_clear``'s follow-up) proves the life it captured under
    the same hold: a deletion begun after that proof waits, so the write never lands on the row that follows."""
    async with _separate_connections(test_engine, tmp_path) as engine:
        factory = async_sessionmaker_for(engine, monkeypatch)
        pid, aid = await _seed_printer_and_run(factory, status="completed")
        life = printer_manager.printer_life(pid)
        seen = {}
        monkeypatch.setattr(AsyncSession, "commit", _deletion_at_the_first_commit(engine, factory, pid, aid, seen))
        await printer_manager._persist_awaiting_plate_clear(pid, True, publish=False, life=life)

        assert seen == {"deletion_finished_first": False}
        async with factory() as db:
            row = await db.get(Printer, pid)
            assert (row.name, bool(row.awaiting_plate_clear)) == ("replacement", False)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["gate", "queued-gate-write", "pause"])
async def test_a_deletion_that_takes_the_lock_first_is_seen_under_it(
    printer_factory, archive_factory, test_engine, monkeypatch, operation
):
    """The record is proved AFTER its lock is taken, never before: a deletion that holds the lock the write waits
    for -- and commits a replacement while it waits -- is seen by the proof made once the lock is ours."""
    from backend.app.core import database
    from backend.app.models.archive import PrintArchive
    from backend.app.models.print_usage_event import PrintUsageEvent

    factory = async_sessionmaker_for(test_engine, monkeypatch)
    monkeypatch.setattr(printer_manager, "_loop", None)
    printer = await printer_factory(require_plate_clear=True)
    archive = await archive_factory(printer.id, status="completed" if operation != "pause" else "printing")
    pid, aid = printer.id, archive.id
    life = printer_manager.printer_life(pid)
    real_lock = database.take_write_lock
    waited = []

    async def take_write_lock(db, table, row_id):
        if table.name == "printers" and row_id == pid and not waited:
            waited.append(True)
            await _replace_printer_and_run(factory, pid, aid, require_plate_clear=True)
        return await real_lock(db, table, row_id)

    monkeypatch.setattr(database, "take_write_lock", take_write_lock)
    if operation == "gate":
        with printer_manager.session_scope(pid, life):
            await printer_manager.arm_awaiting_plate_clear(pid, aid)
    elif operation == "queued-gate-write":
        await printer_manager._persist_awaiting_plate_clear(pid, True, publish=False, life=life)
    else:
        _quiet_edges(monkeypatch)
        await printer_manager._run_for_client(pid, life, _edge_callback(pid, "pause"))

    assert waited
    async with factory() as db:
        row = await db.get(Printer, pid)
        events = list((await db.scalars(select(PrintUsageEvent.id).where(PrintUsageEvent.archive_id == aid))).all())
        run = await db.get(PrintArchive, aid)
    assert (row.name, bool(row.awaiting_plate_clear), run.filename, events) == ("replacement", False, "new.3mf", [])
    assert not printer_manager.is_awaiting_plate_clear(pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "replaced-in-the-claim"])
async def test_a_resume_claim_overtaken_by_a_deletion_publishes_nothing_for_the_next_printer(
    printer_factory, archive_factory, test_engine, monkeypatch, deleted
):
    """The external stock claim commits under its own hold; what the edge does with its answer afterwards -- forget
    the advertised slot, tell the browsers, write the journal -- names the printer again, after the claim's awaits."""
    from backend.app import main
    from backend.app.models.print_usage_event import PrintUsageEvent
    from backend.app.services import ams_advertised_overlay, auto_stock_spool

    factory = async_sessionmaker_for(test_engine, monkeypatch)
    printer = await printer_factory()
    archive = await archive_factory(printer.id, status="printing")
    pid, aid = printer.id, archive.id
    life = printer_manager.printer_life(pid)
    published = []

    async def claim(_db, **_kwargs):
        if deleted:
            await _replace_printer_and_run(factory, pid, aid)
        return {"reason": "assigned", "spool_id": 5, "ams_id": 255, "tray_id": 0}

    async def broadcast(message):
        published.append(("broadcast", message["type"]))

    monkeypatch.setattr(auto_stock_spool, "claim_on_external_resume", claim)
    monkeypatch.setattr(ams_advertised_overlay, "forget", lambda *args: published.append(("forget", args)))
    monkeypatch.setattr(main.ws_manager, "broadcast", broadcast)
    _quiet_edges(monkeypatch)
    state = main.PrinterState()
    state.tray_now = 0
    state.state = "RUNNING"
    main._pause_started_at[pid] = 1.0
    await printer_manager._run_for_client(pid, life, main._handle_resume_edge(pid, state, stock_resume_witnessed=True))

    async with factory() as db:
        events = list((await db.scalars(select(PrintUsageEvent.event).where(PrintUsageEvent.archive_id == aid))).all())
    expected = [] if deleted else [("forget", (pid, 255, 0)), ("broadcast", "spool_auto_assigned")]
    assert (published, len(events)) == (expected, 0 if deleted else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [False, True], ids=["control", "replaced-after-its-commit"])
async def test_a_queued_gate_write_overtaken_after_its_commit_tells_the_relay_nothing(
    printer_factory, archive_factory, test_engine, monkeypatch, deleted
):
    """The relay names the printer by the name and serial held for its id: after the queued write's commit awaited,
    a replacement's would be published with the deleted record's gate."""
    factory = async_sessionmaker_for(test_engine, monkeypatch)
    pid = (await printer_factory(awaiting_plate_clear=False)).id
    aid = (await archive_factory(pid, status="completed")).id
    life = printer_manager.printer_life(pid)
    real_commit = AsyncSession.commit
    told = []

    async def commit(self):
        await real_commit(self)
        if deleted and not told:
            told.append("replaced")
            await _replace_printer_and_run(factory, pid, aid)

    async def emit(printer_id, awaiting):
        told.append((printer_id, awaiting))

    monkeypatch.setattr(AsyncSession, "commit", commit)
    monkeypatch.setattr(printer_manager, "_emit_plate_clear_state", emit)
    await printer_manager._persist_awaiting_plate_clear(pid, True, publish=True, life=life)

    assert told == (["replaced"] if deleted else [(pid, True)])
