"""A deleted printer's runtime is never inherited by the next printer with its id (spec gh109-o1 r2).

SQLite gives the highest deleted id -- of a printer AND of an archive -- to the next row created, so the
process-memory runtime the deletion left behind used to belong to the re-added printer: its first dispatch met the
old current run (the GH109 veto), its queue waited behind the old plate gate, and late work of the old record could
still connect, bind or publish under the id.

Every case runs the real route on the test database; MQTT, devices and files are never reached.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.printer import Printer
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

    async def on_layer(pid, layer, previous):
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
