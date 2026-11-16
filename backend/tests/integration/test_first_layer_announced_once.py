"""The first-layer-complete notification is a fact of the RUN, not of the process.

``main._first_layer_notified`` is process memory: after a restart on layers 2-10 a process-fresh MQTT client
delivers layer edges again and the print's first layer was announced a second time. The announcement is now
claimed on the run's execution archive before it is sent. Each "process" below is a new ``PrinterManager`` with a
process-fresh ``BambuMQTTClient`` fed real frames; a layer edge reaches ``main.on_printer_layer_change`` the way
the manager schedules it (``_run_for_client`` with the session's record life).
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app import main
from backend.app.models.archive import PrintArchive
from backend.app.services import first_layer_announcement
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.print_run_binding import current_print_run
from backend.app.services.printer_manager import PrinterManager

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class _Process:
    """One BamDude process on one printer: its manager, its MQTT client and the layer callbacks it scheduled."""

    def __init__(self, monkeypatch, printer_id: int):
        self.printer_id = printer_id
        self.manager = PrinterManager()
        self.client = BambuMQTTClient(ip_address="127.0.0.1", serial_number="OFFLINE-FL", access_code="synthetic")
        self.client.state.connected = True
        self.manager._clients[printer_id] = self.client
        self.scheduled: list = []
        life = self.manager.printer_life(printer_id)

        def on_layer_change(layer_num: int, previous_layer: int):
            # What ``PrinterManager.connect_printer`` wires: the callback of this client session.
            self.scheduled.append(
                self.manager._run_for_client(
                    printer_id,
                    life,
                    main.on_printer_layer_change(
                        printer_id, layer_num, previous_layer, self.client._layer_frame_observation
                    ),
                )
            )

        self.client.on_layer_change = on_layer_change
        monkeypatch.setattr(main, "printer_manager", self.manager)
        # What a process start leaves of the previous process's memory: nothing.
        monkeypatch.setattr(main, "_first_layer_notified", {})
        monkeypatch.setattr(main, "_running_print_adoptions", {})

    def push(self, **fields):
        self.client._process_message({"print": {"command": "push_status", **fields}})

    def meets_a_running_print(self, layer: int):
        self.push(
            gcode_state="RUNNING",
            gcode_file="job.gcode",
            subtask_id="900",
            layer_num=layer,
            total_layer_num=100,
            mc_percent=12,
        )

    async def binds(self, factory, archive_id: int, origin: str = "external"):
        """The lifecycle choke point every start, adoption and recovery binds the run through."""
        async with factory() as db:
            archive = await db.get(PrintArchive, archive_id)
            main._bind_print_file_analysis_context(self.printer_id, archive, origin=origin)

    async def layer(self, layer: int):
        self.push(layer_num=layer)
        await self.settle()

    async def settle(self):
        pending, self.scheduled = self.scheduled, []
        await asyncio.gather(*pending)


@pytest.fixture
def factory(monkeypatch, test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(main, "async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    return factory


@pytest.fixture
def sent(monkeypatch):
    monkeypatch.setattr(main, "_maybe_bank_inprint_frame", AsyncMock())
    monkeypatch.setattr(main, "_capture_snapshot_for_notification", AsyncMock(return_value=None))
    monkeypatch.setattr("backend.app.services.layer_timelapse.on_layer_change", AsyncMock())
    monkeypatch.setattr("backend.app.services.macro_trigger.fire_layer_macros", AsyncMock())
    monkeypatch.setattr("backend.app.services.print_file_analysis.start_print_file_analysis", lambda *a, **k: None)
    sent = AsyncMock()
    monkeypatch.setattr(main.notification_service, "on_first_layer_complete", sent)
    return sent


async def _printing_archive(archive_factory, printer_id: int, **overrides) -> int:
    archive = await archive_factory(printer_id, status="printing", **overrides)
    assert archive.recovery_identity
    return archive.id


async def _marker(factory, archive_id: int):
    async with factory() as db:
        archive = await db.get(PrintArchive, archive_id)
        return (archive.extra_data or {}).get(first_layer_announcement.MARKER)


async def _first_process_announces(monkeypatch, factory, printer_id: int, archive_id: int) -> _Process:
    first = _Process(monkeypatch, printer_id)
    first.meets_a_running_print(1)
    await first.binds(factory, archive_id)
    await first.layer(2)
    return first


async def test_a_restart_inside_the_first_ten_layers_does_not_announce_again(
    monkeypatch, factory, sent, printer_factory, archive_factory
):
    printer = await printer_factory()
    archive_id = await _printing_archive(archive_factory, printer.id)
    await _first_process_announces(monkeypatch, factory, printer.id, archive_id)
    assert sent.await_count == 1

    restarted = _Process(monkeypatch, printer.id)
    restarted.meets_a_running_print(5)
    await restarted.binds(factory, archive_id, origin="observed")
    await restarted.layer(6)

    assert sent.await_count == 1
    # The adoption seeded the cache from the marker: the edge asked the database nothing.
    assert main._first_layer_notified[printer.id] is True


async def test_an_edge_that_overtakes_the_adoption_waits_for_its_binding(
    monkeypatch, factory, sent, printer_factory, archive_factory
):
    """The first layer edge after a restart can run while ``on_print_running_observed`` still looks the run up."""
    printer = await printer_factory()
    archive_id = await _printing_archive(archive_factory, printer.id)
    await _first_process_announces(monkeypatch, factory, printer.id, archive_id)

    restarted = _Process(monkeypatch, printer.id)
    restarted.meets_a_running_print(5)
    release = asyncio.Event()

    async def slow_recovery(printer_id, data):
        await release.wait()
        await restarted.binds(factory, archive_id, origin="observed")

    monkeypatch.setattr(main, "_recover_running_print", slow_recovery)
    adoption = asyncio.create_task(main.on_print_running_observed(printer.id, {}))
    await asyncio.sleep(0)
    restarted.push(layer_num=6)
    edge = asyncio.create_task(restarted.settle())
    for _ in range(20):
        await asyncio.sleep(0)
    assert sent.await_count == 1 and not edge.done()

    release.set()
    await asyncio.wait_for(asyncio.gather(adoption, edge), 5)
    assert sent.await_count == 1
    assert main._running_print_adoptions == {}


async def test_a_run_is_announced_once_without_a_restart(monkeypatch, factory, sent, printer_factory, archive_factory):
    printer = await printer_factory()
    archive_id = await _printing_archive(archive_factory, printer.id)
    process = await _first_process_announces(monkeypatch, factory, printer.id, archive_id)
    await process.layer(3)
    await process.layer(4)

    assert sent.await_count == 1
    assert await _marker(factory, archive_id)


async def test_a_long_print_still_at_zero_percent_is_not_announced_on_every_layer(
    monkeypatch, factory, sent, printer_factory, archive_factory
):
    """The status handler empties the cache on every frame below 5 % progress -- a long print's first layers."""
    printer = await printer_factory()
    archive_id = await _printing_archive(archive_factory, printer.id)
    process = await _first_process_announces(monkeypatch, factory, printer.id, archive_id)
    for layer in (3, 4, 5):
        main._first_layer_notified[printer.id] = False
        await process.layer(layer)

    assert sent.await_count == 1


async def test_a_print_missed_while_down_is_still_announced_once(
    monkeypatch, factory, sent, printer_factory, archive_factory
):
    """BamDude was down across the layer 1 -> 2 edge: nothing was announced, so the adopted run still owes it."""
    printer = await printer_factory()
    archive_id = await _printing_archive(archive_factory, printer.id)

    restarted = _Process(monkeypatch, printer.id)
    restarted.meets_a_running_print(5)
    await restarted.binds(factory, archive_id, origin="observed")
    await restarted.layer(6)
    await restarted.layer(7)

    assert sent.await_count == 1


async def test_a_reprint_is_a_new_run_and_is_announced_again(
    monkeypatch, factory, sent, printer_factory, archive_factory
):
    printer = await printer_factory()
    first_id = await _printing_archive(archive_factory, printer.id)
    process = await _first_process_announces(monkeypatch, factory, printer.id, first_id)
    async with factory() as db:
        (await db.get(PrintArchive, first_id)).status = "completed"
        await db.commit()

    second_id = await _printing_archive(archive_factory, printer.id)
    process.push(gcode_state="FINISH")
    process.push(gcode_state="RUNNING", subtask_id="901", layer_num=0, mc_percent=0)
    main._first_layer_notified[printer.id] = False  # what the status handler does below 5 % progress
    await process.settle()
    await process.binds(factory, second_id)
    assert current_print_run(process.manager, printer.id).archive_id == second_id
    process.push(layer_num=1)
    await process.layer(2)

    assert sent.await_count == 2
    assert await _marker(factory, second_id)


async def test_two_processes_racing_on_the_marker_send_once(
    monkeypatch, factory, sent, printer_factory, archive_factory
):
    printer = await printer_factory()
    archive_id = await _printing_archive(archive_factory, printer.id)
    process = _Process(monkeypatch, printer.id)
    process.meets_a_running_print(1)
    await process.binds(factory, archive_id)
    await process.settle()

    class Forgetful(dict):
        """Neither racer sees the other's memory -- only the marker stands between them."""

        def get(self, key, default=None):
            return default

    monkeypatch.setattr(main, "_first_layer_notified", Forgetful())
    process.push(layer_num=2)
    process.push(layer_num=3)
    assert len(process.scheduled) == 2
    await process.settle()

    assert sent.await_count == 1


async def test_an_external_print_without_its_3mf_is_announced_once(
    monkeypatch, factory, sent, printer_factory, archive_factory
):
    printer = await printer_factory()
    archive_id = await _printing_archive(archive_factory, printer.id, file_path="")
    await _first_process_announces(monkeypatch, factory, printer.id, archive_id)

    restarted = _Process(monkeypatch, printer.id)
    restarted.meets_a_running_print(3)
    await restarted.binds(factory, archive_id)
    await restarted.layer(4)

    assert sent.await_count == 1


async def test_a_run_with_no_archive_identity_yet_keeps_the_process_memory_rule(
    monkeypatch, factory, sent, printer_factory, archive_factory
):
    printer = await printer_factory()
    archive_id = await _printing_archive(archive_factory, printer.id)
    process = _Process(monkeypatch, printer.id)
    process.meets_a_running_print(1)
    assert current_print_run(process.manager, printer.id) is None

    await process.layer(2)
    await process.layer(3)

    assert sent.await_count == 1
    assert await _marker(factory, archive_id) is None


async def test_a_claim_that_fails_sends_nothing_and_the_next_edge_asks_again(
    monkeypatch, factory, sent, printer_factory, archive_factory
):
    printer = await printer_factory()
    archive_id = await _printing_archive(archive_factory, printer.id)
    process = _Process(monkeypatch, printer.id)
    process.meets_a_running_print(1)
    await process.binds(factory, archive_id)
    real = first_layer_announcement.claim
    monkeypatch.setattr(first_layer_announcement, "claim", AsyncMock(side_effect=RuntimeError("database is locked")))

    await process.layer(2)
    assert sent.await_count == 0

    monkeypatch.setattr(first_layer_announcement, "claim", real)
    await process.layer(3)
    assert sent.await_count == 1


async def test_the_claim_is_addressed_to_the_run_not_to_the_row_id(factory, printer_factory, archive_factory):
    """SQLite gives a deleted archive's id to the next one: a claim for the old run writes nothing on the new."""
    printer = await printer_factory()
    archive_id = await _printing_archive(archive_factory, printer.id)

    assert await first_layer_announcement.claim(factory, archive_id, "the-identity-of-a-deleted-run") is True
    assert await _marker(factory, archive_id) is None


async def test_a_reslice_does_not_inherit_the_marker():
    from backend.app.services.print_run_metadata import without_run_identity

    assert first_layer_announcement.MARKER not in without_run_identity({first_layer_announcement.MARKER: "x", "a": 1})
