"""The next slicer pause is asked of the proven run's own layer and the print's own file."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.app import main
from backend.app.core.permissions import Permission
from backend.app.services import live_projection, printer_manager as printer_manager_module
from backend.app.services.live_projection import Entitlements
from backend.app.services.pause_forecast import forecast_next_pause, next_pause
from backend.app.services.print_file_analysis import PrintFileAnalysis
from backend.app.services.print_run_binding import claim_print_start
from backend.app.services.printer_manager import PrinterManager, printer_state_to_dict
from backend.tests.unit.services.test_layer_projection import archive, client, push

PAUSES = ((5, 30), (9, 10))


@pytest.fixture
def rig(monkeypatch, tmp_path):
    m, c, a = PrinterManager(), client(), archive(total=12, file_path="job.3mf")
    (tmp_path / "job.3mf").write_bytes(b"fixture")
    m._clients[3] = c
    m._uses_print_file_analysis_service = False
    points = {"value": PAUSES}

    def runner(_path, _plate):
        return PrintFileAnalysis([], {1: {0: 1.0}}, {}, pause_points=points["value"])

    m._print_file_analysis_runner = runner
    monkeypatch.setattr(main, "printer_manager", m)
    monkeypatch.setattr(printer_manager_module, "printer_manager", m)
    monkeypatch.setattr(main.app_settings, "base_dir", tmp_path)
    monkeypatch.setattr(main, "_last_projection_broadcast", {})
    monkeypatch.setattr(main, "_projection_repeat_available", {})
    monkeypatch.setattr(main, "_last_status_broadcast", {})
    for name in ("_last_printer_state", "_last_printer_state_generation", "_printer_last_connected"):
        monkeypatch.setattr(main, name, {})
    events = []
    c.on_print_start = events.append
    push(c, gcode_state="FINISH", subtask_id="100", total_layer_num=693, layer_num=693)
    push(c, gcode_state="IDLE", subtask_id="200")
    push(c, gcode_state="RUNNING", gcode_file="fixture.gcode", subtask_id="200", layer_num=0, mc_percent=10)
    data = events[-1]
    admission = claim_print_start(m, 3, data)
    return SimpleNamespace(m=m, c=c, a=a, data=data, generation=admission.generation, points=points, tmp=tmp_path)


def bind(r, **overrides):
    kwargs = {"expected_generation": r.generation, "origin": "dispatch", "observation": r.data["_print_observation"]}
    kwargs.update(overrides)
    main._bind_print_file_analysis_context(3, r.a, **kwargs)


async def analysed(r):
    for _ in range(300):
        context = r.m._print_file_analysis_contexts.get(3)
        if context is not None and context.state == "ready":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the print file was never analysed")


@pytest.mark.asyncio
async def test_the_indicator_follows_the_layers_and_moves_to_the_next_pause(rig):
    r = rig
    bind(r)
    assert next_pause(r.m, 3) is None  # bound, not analysed yet
    await analysed(r)
    push(r.c, mc_remaining_time=50)
    # The previous print ended on layer 693; that number never "passes" this print's pauses.
    assert next_pause(r.m, 3) == {"layer": 5, "minutes": 20}
    push(r.c, layer_num=4, mc_remaining_time=33)
    assert next_pause(r.m, 3) == {"layer": 5, "minutes": 3}
    # On the pause's own layer (waiting there, or resumed) that pause is behind.
    push(r.c, layer_num=5, mc_remaining_time=30)
    assert next_pause(r.m, 3) == {"layer": 9, "minutes": 20}
    push(r.c, layer_num=9, mc_remaining_time=10)
    assert next_pause(r.m, 3) is None


@pytest.mark.asyncio
async def test_the_time_is_never_negative(rig):
    bind(rig)
    await analysed(rig)
    push(rig.c, layer_num=4, mc_remaining_time=7)
    assert next_pause(rig.m, 3) == {"layer": 5, "minutes": 0}


@pytest.mark.asyncio
async def test_only_the_layer_outside_standard_speed_without_markers_or_without_a_reported_time(rig):
    r = rig
    bind(r)
    await analysed(r)
    push(r.c, layer_num=2)
    assert next_pause(r.m, 3) == {"layer": 5, "minutes": None}  # the printer reported no time left yet
    push(r.c, mc_remaining_time=50, spd_lvl=3)
    assert next_pause(r.m, 3) == {"layer": 5, "minutes": None}
    push(r.c, spd_lvl=2)
    assert next_pause(r.m, 3) == {"layer": 5, "minutes": 20}


@pytest.mark.asyncio
async def test_a_file_without_time_markers_gives_the_layer_only(rig):
    rig.points["value"] = ((5, None),)
    bind(rig)
    await analysed(rig)
    push(rig.c, layer_num=2, mc_remaining_time=50)
    assert next_pause(rig.m, 3) == {"layer": 5, "minutes": None}


@pytest.mark.asyncio
async def test_no_pause_in_the_file_or_incomparable_layers_show_nothing(rig):
    rig.points["value"] = ()
    bind(rig)
    await analysed(rig)
    push(rig.c, layer_num=2, mc_remaining_time=50)
    assert next_pause(rig.m, 3) is None


@pytest.mark.asyncio
async def test_an_unknown_total_hides_the_indicator(rig):
    r = rig
    r.a.extra_data = {}  # no file total, and the printer reports none
    bind(r)
    await analysed(r)
    push(r.c, layer_num=2, mc_remaining_time=50)
    assert next_pause(r.m, 3) is None
    push(r.c, total_layer_num=12)
    assert next_pause(r.m, 3) == {"layer": 5, "minutes": 20}


@pytest.mark.asyncio
async def test_a_paused_disconnected_or_unproven_print_shows_nothing(rig):
    r = rig
    bind(r)
    await analysed(r)
    push(r.c, layer_num=2, mc_remaining_time=50)
    assert next_pause(r.m, 3) is not None

    snapshot = r.c.get_layer_snapshot()
    r.c._layer_snapshot = replace(snapshot, unproven=True)
    assert next_pause(r.m, 3) is None
    r.c._layer_snapshot = replace(
        snapshot, address=replace(snapshot.address, generation=snapshot.address.generation + 1)
    )
    assert next_pause(r.m, 3) is None
    r.c._layer_snapshot = snapshot

    r.c.state.connected = False
    assert next_pause(r.m, 3) is None
    r.c.state.connected = True

    push(r.c, gcode_state="PAUSE")
    assert next_pause(r.m, 3) is None
    push(r.c, gcode_state="RUNNING")
    assert next_pause(r.m, 3) == {"layer": 5, "minutes": 20}


@pytest.mark.asyncio
async def test_a_run_that_is_only_observed_shows_nothing_until_it_is_proven(rig):
    """After a restart the adopted print is bound by name; the indicator waits for the proof."""
    r = rig
    bind(r, origin="observed", expected_generation=None)
    await analysed(r)
    push(r.c, layer_num=2, mc_remaining_time=50)
    assert next_pause(r.m, 3) is None

    bind(r, origin="exact_subtask")
    await analysed(r)
    assert next_pause(r.m, 3) == {"layer": 5, "minutes": 20}
    # Pauses already below the current layer are not offered again.
    push(r.c, layer_num=7, mc_remaining_time=18)
    assert next_pause(r.m, 3) == {"layer": 9, "minutes": 8}


@pytest.mark.asyncio
async def test_an_external_print_shows_it_once_its_3mf_is_attached(rig):
    r = rig
    r.a.file_path = ""
    bind(r, origin="external")
    push(r.c, layer_num=2, mc_remaining_time=50, total_layer_num=12)
    await asyncio.sleep(0.05)
    assert next_pause(r.m, 3) is None

    r.a.file_path = "job.3mf"
    main._notify_print_file_analysis_source_ready(3, r.a)
    await analysed(r)
    assert next_pause(r.m, 3) == {"layer": 5, "minutes": 20}


@pytest.mark.asyncio
async def test_the_forecast_names_the_run_it_belongs_to(rig):
    bind(rig)
    await analysed(rig)
    push(rig.c, layer_num=2, mc_remaining_time=50)
    forecast = forecast_next_pause(rig.m, 3)
    assert (forecast.archive_id, forecast.archive_identity, forecast.layer) == (1263, "synthetic-physical-print", 5)


@pytest.mark.asyncio
async def test_the_websocket_status_carries_the_field(rig):
    r = rig
    bind(r)
    await analysed(r)
    push(r.c, layer_num=2, mc_remaining_time=50)
    payload = printer_state_to_dict(r.c.state, 3)
    assert payload["next_pause"] == {"layer": 5, "minutes": 20}
    push(r.c, gcode_state="PAUSE")
    assert printer_state_to_dict(r.c.state, 3)["next_pause"] is None


def _access(*permissions, printers=None):
    return Entitlements(frozenset(permissions), None if printers is None else frozenset(printers), 7, "account")


def test_the_field_is_projected_only_to_a_viewer_who_may_read_that_printer():
    message = {"type": "printer_status", "printer_id": 3, "data": {"next_pause": {"layer": 5, "minutes": 20}}}
    event = live_projection.PreparedEvent(message)
    read = Permission.PRINTERS_READ.value
    assert live_projection.shape(event, _access(read))["data"]["next_pause"] == {"layer": 5, "minutes": 20}
    assert live_projection.shape(event, _access("websocket:connect")) is None
    assert live_projection.shape(event, _access(read, printers=[4])) is None


@pytest.mark.asyncio
async def test_the_analysed_file_is_published_although_no_other_status_field_moved(rig, monkeypatch):
    r = rig
    sent = []

    async def send(printer_id, payload):
        sent.append(payload["next_pause"])

    monkeypatch.setattr(main.ws_manager, "send_printer_status", send)
    monkeypatch.setattr(r.m, "session_still_lives", lambda _id: True)
    r.a.file_path = ""
    bind(r)
    push(r.c, layer_num=2, mc_remaining_time=50)
    await main._broadcast_printer_status_projection(3)
    r.a.file_path = "job.3mf"
    main._notify_print_file_analysis_source_ready(3, r.a)
    await analysed(r)
    await main._broadcast_printer_status_projection(3)
    await main._broadcast_printer_status_projection(3)
    assert sent == [None, {"layer": 5, "minutes": 20}]
