"""F1: real MQTT frames, accepted binding, source fences and completed snapshots."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app import main
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.layer_projection import (
    LayerPair,
    bind_layer_grant,
    capture_first_layer_total,
    current_layer_grant,
    first_layer_total_is_current,
    notify_current_print_source_ready,
    resolve_layer_pair,
)
from backend.app.services.layer_source import MARKER, select_layer_total, write_layer_source
from backend.app.services.print_run_binding import (
    begin_print_run_finishing,
    bind_print_run,
    claim_print_start,
    current_print_run,
    discard_print_run,
    revoke_print_effects,
)
from backend.app.services.printer_manager import PrinterManager, printer_state_to_dict


def client():
    c = BambuMQTTClient(ip_address="127.0.0.1", serial_number="OFFLINE-F1", access_code="synthetic")
    c.state.connected = True
    return c


def push(c, **fields):
    c._process_message({"print": {"command": "push_status", **fields}})


def archive(total=693, **overrides):
    a = SimpleNamespace(
        id=1263,
        recovery_identity="synthetic-physical-print",
        file_path="archive/source.3mf",
        content_hash="patched-upload",
        source_content_hash="unpatched-source",
        applied_patches='["mesh_mode_fast_check_off"]',
        extra_data={},
        plate_index=1,
        subtask_id="200",
        total_layers=999,
    )
    a.__dict__.update(overrides)
    write_layer_source(a, [{"index": 1, "total_layers": total}, {"index": 2, "total_layers": 91}])
    return a


@pytest.fixture
def rig(monkeypatch):
    m, c, a = PrinterManager(), client(), archive()
    m._clients[3] = c
    monkeypatch.setattr(main, "printer_manager", m)
    monkeypatch.setattr(main, "_last_projection_broadcast", {})
    monkeypatch.setattr(main, "_last_status_broadcast", {})
    for name in ("_last_printer_state", "_last_printer_state_generation", "_printer_last_connected"):
        monkeypatch.setattr(main, name, {})
    events = []
    c.on_print_start = events.append
    # Sanitized natural 102 shape: old terminal total, IDLE new id, then
    # RUNNING and layer-only deltas. No PREPARE total ever arrived.
    push(c, gcode_state="FINISH", subtask_id="100", total_layer_num=693, layer_num=693)
    push(c, gcode_state="IDLE", subtask_id="200")
    push(c, gcode_state="RUNNING", gcode_file="fixture.gcode", subtask_id="200", layer_num=0, mc_percent=10)
    data = events[-1]
    admission = claim_print_start(m, 3, data)
    return SimpleNamespace(m=m, c=c, a=a, data=data, generation=admission.generation)


def bind(r, **overrides):
    kwargs = {"expected_generation": r.generation, "origin": "dispatch", "observation": r.data["_print_observation"]}
    kwargs.update(overrides)
    main._bind_print_file_analysis_context(3, r.a, **kwargs)


def test_f01_natural_102_accepted_bind_keeps_raw_and_projects_file(rig):
    r = rig
    bind(r)
    push(r.c, layer_num=14)
    assert (r.c.state.layer_num, r.c.state.total_layers) == (14, 0)
    assert resolve_layer_pair(r.m, 3) == LayerPair(14, 693, "file")
    pair = resolve_layer_pair(r.m, 3)
    payload = printer_state_to_dict(r.c.state, 3, layer_pair=pair)
    assert (payload["layer_num"], payload["total_layers"], payload["total_layers_source"]) == (14, 693, "file")
    push(r.c, total_layer_num=700)
    assert resolve_layer_pair(r.m, 3) == LayerPair(14, 700, "mqtt")


@pytest.mark.parametrize(
    "origin,generation,observation", [("observed", True, True), ("dispatch", False, True), ("dispatch", True, False)]
)
def test_f05_f08_prepared_observed_and_unaddressed_cannot_grant(rig, origin, generation, observation):
    bind(
        rig,
        origin=origin,
        expected_generation=rig.generation if generation else None,
        observation=rig.data["_print_observation"] if observation else None,
    )
    assert resolve_layer_pair(rig.m, 3).source == "unknown"
    assert current_layer_grant(rig.m, 3) is None


@pytest.mark.parametrize("field", ["file_path", "content_hash", "source_content_hash"])
def test_f17_each_source_component_is_fenced(field):
    a = archive()
    assert select_layer_total(a, 1) == 693
    setattr(a, field, "replaced")
    assert select_layer_total(a, 1) is None
    write_layer_source(a, [{"index": 1, "total_layers": 42}])
    assert select_layer_total(a, 1) == 42
    del a.extra_data[MARKER][field]
    assert select_layer_total(a, 1) is None


@pytest.mark.parametrize("value", [None, 0, -1, True, 1.0, "1"])
def test_f15_f16_plate_and_total_require_positive_integers(value):
    a = archive(value)
    assert select_layer_total(a, 1) is None
    assert select_layer_total(archive(), value) is None


def test_f15_f18_no_legacy_or_upper_level_or_other_plate_fallback():
    a = archive()
    assert select_layer_total(a, 2) == 91
    assert select_layer_total(a, 3) is None
    a.extra_data.pop(MARKER)
    a.extra_data["plates"] = [{"index": 1, "total_layers": 693}]
    assert select_layer_total(a, 1) is None
    a.extra_data[MARKER] = {"file_path": a.file_path, "plates": {"1": 693}}
    assert select_layer_total(a, 1) is None


def test_f16_duplicate_plate_and_noncanonical_keys_fail_closed():
    a = archive()
    write_layer_source(a, [{"index": 1, "total_layers": 693}, {"index": 1, "total_layers": 42}])
    assert select_layer_total(a, 1) is None
    write_layer_source(a, [{"index": 1, "total_layers": 693}])
    a.extra_data[MARKER]["plates"]["01"] = 42
    assert select_layer_total(a, 1) is None


def test_f10b_captured_revision_not_live_revision_at_delayed_bind(rig):
    push(rig.c, gcode_file="unproven.gcode", layer_num=14)
    bind(rig)
    assert current_layer_grant(rig.m, 3).address.revision == 0
    assert rig.c.get_layer_snapshot().address.revision == 1
    assert resolve_layer_pair(rig.m, 3) == LayerPair(14)


def test_f10_unproven_file_invalidates_without_boundary_change(rig):
    bind(rig)
    before = rig.c.get_layer_snapshot().address
    push(rig.c, gcode_file="different.gcode", layer_num=14)
    after = rig.c.get_layer_snapshot().address
    assert before.generation == after.generation and before.revision != after.revision
    assert resolve_layer_pair(rig.m, 3) == LayerPair(14)


def test_f07_f17_source_ready_can_fill_and_clear_but_cannot_grant(rig):
    notify_current_print_source_ready(rig.m, 3, rig.a)
    assert current_layer_grant(rig.m, 3) is None
    rig.a.extra_data = {}
    bind(rig)
    assert resolve_layer_pair(rig.m, 3).source == "unknown"
    write_layer_source(rig.a, [{"index": 1, "total_layers": 693}])
    notify_current_print_source_ready(rig.m, 3, rig.a)
    assert resolve_layer_pair(rig.m, 3).total_layers == 693
    rig.a.source_content_hash = "new-source"
    notify_current_print_source_ready(rig.m, 3, rig.a)
    assert resolve_layer_pair(rig.m, 3).source == "unknown"


@pytest.mark.parametrize("replacement", ["id", "identity"])
def test_f23b_same_unproven_bind_preserves_different_or_stale_does_not(rig, replacement):
    bind(rig)
    first = current_layer_grant(rig.m, 3)
    bind(rig, origin="observed", expected_generation=None, observation=None)
    assert current_layer_grant(rig.m, 3) == first
    if replacement == "id":
        rig.a.id += 1
    else:
        rig.a.recovery_identity = "new-incarnation"
    bind(rig, expected_generation=-1)
    assert current_layer_grant(rig.m, 3) == first  # rejected bind has no mutation
    bind(rig, origin="observed", expected_generation=None, observation=None)
    assert current_layer_grant(rig.m, 3) is None


@pytest.mark.parametrize("action", ["revoke", "restore", "finish", "discard", "delete", "new_binding"])
def test_f06_f09_f14_cleanup_never_lends_total_to_replacement(rig, action):
    bind(rig)
    if action == "revoke":
        revoke_print_effects(rig.m, 3)
    elif action == "restore":
        revoke_print_effects(rig.m)
    elif action == "finish":
        begin_print_run_finishing(rig.m, 3, rig.a.id)
    elif action == "discard":
        discard_print_run(rig.m, 3, rig.a.id)
    elif action == "delete":
        rig.m.begin_printer_deletion(3)
    else:
        bind_print_run(rig.m, printer_id=3, archive_id=999, archive_identity="B")
    notify_current_print_source_ready(rig.m, 3, rig.a)
    assert resolve_layer_pair(rig.m, 3).source == "unknown"


def test_f13_reconnect_requires_fresh_frame_and_preserves_lineage(rig):
    bind(rig)
    push(rig.c, gcode_state="PAUSE", layer_num=14)
    assert resolve_layer_pair(rig.m, 3).source == "file"
    c = client()
    c.carry_print_lifecycle_from(rig.c)
    rig.m._clients[3] = c
    assert resolve_layer_pair(rig.m, 3).source == "unknown"
    push(c, gcode_state="PAUSE", gcode_file="fixture.gcode", subtask_id="200", layer_num=15)
    assert resolve_layer_pair(rig.m, 3) == LayerPair(15, 693, "file")
    rig.m._clients[3] = client()
    push(rig.m._clients[3], gcode_state="RUNNING", gcode_file="fixture.gcode", subtask_id="200", layer_num=16)
    assert resolve_layer_pair(rig.m, 3).source == "unknown"


@pytest.mark.parametrize("fields", [{"gcode_state": "FINISH"}, {"gcode_state": "IDLE"}, {"layer_num": 694}])
def test_f16_f24_no_file_total_when_inactive_or_below_current(rig, fields):
    bind(rig)
    push(rig.c, **fields)
    assert resolve_layer_pair(rig.m, 3).source == "unknown"


def test_f24_disconnected_no_file_total(rig):
    bind(rig)
    rig.c.state.connected = False
    assert resolve_layer_pair(rig.m, 3).source == "unknown"


def test_f11_reader_sees_completed_snapshot_during_boundary_and_counter_update(rig):
    bind(rig)
    push(rig.c, layer_num=14)
    before = rig.c.get_layer_snapshot()
    entered, release = Event(), Event()

    def callback(_):
        entered.set()
        assert release.wait(5)

    rig.c.on_print_start = callback
    worker = Thread(target=lambda: push(rig.c, subtask_id="300", gcode_file="B.gcode", layer_num=1, total_layer_num=42))
    worker.start()
    try:
        assert entered.wait(5)
        assert rig.c._run_boundary_generation != before.address.generation
        assert rig.c.get_layer_snapshot() is before
        assert resolve_layer_pair(rig.m, 3) == LayerPair(14, 693, "file")
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert resolve_layer_pair(rig.m, 3) == LayerPair(1, 42, "mqtt")


def test_f12_exception_retains_completed_snapshot_until_success(rig):
    bind(rig)
    before = rig.c.get_layer_snapshot()
    rig.c.on_layer_change = MagicMock(side_effect=ValueError("injected midway"))
    with pytest.raises(ValueError, match="injected midway"):
        push(rig.c, layer_num=14, total_layer_num=42)
    assert rig.c.get_layer_snapshot() is before
    rig.c.on_layer_change = None
    push(rig.c, layer_num=15, total_layer_num=42)
    assert resolve_layer_pair(rig.m, 3) == LayerPair(15, 42, "mqtt")


def test_f12_accepted_bind_before_snapshot_is_not_lost(rig):
    # The same accepted-start callback can run while the MQTT thread still
    # holds the preceding completed frame. Reader activates only on matching publish.
    rig.c._layer_snapshot = replace(
        rig.c.get_layer_snapshot(), address=replace(rig.c.get_layer_snapshot().address, generation=0)
    )
    bind(rig)
    assert resolve_layer_pair(rig.m, 3).source == "unknown"
    push(rig.c, layer_num=14)
    assert resolve_layer_pair(rig.m, 3) == LayerPair(14, 693, "file")


def test_f21b_notification_capture_and_send_fences(rig):
    bind(rig)
    push(rig.c, layer_num=2)
    captured = capture_first_layer_total(rig.m, 3, rig.c._layer_frame_observation)
    assert captured[2] == 693
    assert first_layer_total_is_current(rig.m, 3, captured)
    assert capture_first_layer_total(rig.m, 3, None) is None
    write_layer_source(rig.a, [{"index": 1, "total_layers": 700}])
    notify_current_print_source_ready(rig.m, 3, rig.a)
    assert not first_layer_total_is_current(rig.m, 3, captured)


async def test_f02_f03_f21_display_publication_does_not_admit_or_mute_effects(rig, monkeypatch):
    from backend.app.services import interrupted_dispatch

    send, interrupted = AsyncMock(), MagicMock()
    monkeypatch.setattr(main.ws_manager, "send_printer_status", send)
    monkeypatch.setattr(interrupted_dispatch, "on_status", interrupted)
    monkeypatch.setattr(main, "_repeat_available_for", AsyncMock(return_value=False))
    await main.on_printer_status_change(3, rig.c.state)
    assert send.await_count == 1 and interrupted.call_count == 1
    bind(rig)
    await main._broadcast_printer_status_projection(3)
    assert send.await_count == 2 and interrupted.call_count == 1
    await main.on_printer_status_change(3, rig.c.state)
    assert send.await_count == 2 and interrupted.call_count == 2
    push(rig.c, total_layer_num=693)
    await main.on_printer_status_change(3, rig.c.state)
    assert send.await_count == 3
    assert [c.args[1]["total_layers_source"] for c in send.await_args_list] == ["unknown", "file", "mqtt"]
    assert rig.c.state.total_layers == 693


async def test_f04_source_ready_is_coalesced_and_time_does_not_expire(rig, monkeypatch):
    callback = MagicMock()
    rig.m._layer_projection_callback = callback
    bind(rig)
    notify_current_print_source_ready(rig.m, 3, rig.a)
    notify_current_print_source_ready(rig.m, 3, rig.a)
    await asyncio.sleep(0)
    callback.assert_called_once_with(3)
    for seconds in (30, 60, 600):
        monkeypatch.setattr("time.monotonic", lambda seconds=seconds: seconds)
        assert resolve_layer_pair(rig.m, 3).total_layers == 693
    callback.assert_called_once()


@pytest.mark.parametrize("case,expected", [("match", 693), ("changed_before_send", 0), ("wrong_event", 17)])
async def test_f21b_real_notification_branch_preserves_legacy_fallback_and_once(rig, monkeypatch, case, expected):
    from backend.app.services import layer_timelapse, macro_trigger

    bind(rig)
    push(rig.c, layer_num=2)
    event = rig.c._layer_frame_observation
    if case == "wrong_event":
        event = replace(event, layer_identity_revision=event.layer_identity_revision + 1)
        # This is exactly the legacy live read, not the immutable display pair.
        rig.c.state.total_layers = 17
    monkeypatch.setattr(main, "_first_layer_notified", {})
    monkeypatch.setattr(layer_timelapse, "on_layer_change", AsyncMock())
    monkeypatch.setattr(main, "_maybe_bank_inprint_frame", AsyncMock())
    monkeypatch.setattr(macro_trigger, "fire_layer_macros", AsyncMock())
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=lambda: SimpleNamespace(name="Fixture"))
    session = MagicMock()
    session.return_value.__aenter__ = AsyncMock(return_value=db)
    session.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(main, "async_session", session)

    async def photo(*_):
        if case == "changed_before_send":
            push(rig.c, gcode_file="unproven-before-send.gcode")
            rig.c.state.total_layers = 888  # Must not move the legacy expression here.
        return None

    monkeypatch.setattr(main, "_capture_snapshot_for_notification", photo)
    sent = AsyncMock()
    monkeypatch.setattr(main.notification_service, "on_first_layer_complete", sent)
    await main.on_printer_layer_change(3, 2, 1, event)
    await main.on_printer_layer_change(3, 3, 2, event)
    sent.assert_awaited_once()
    assert sent.await_args.args[3] == expected


async def test_f21_status_only_cannot_consume_next_real_mqtt_reset(rig, monkeypatch):
    from backend.app.services import macro_trigger

    monkeypatch.setattr(main.ws_manager, "send_printer_status", AsyncMock())
    monkeypatch.setattr(main, "_repeat_available_for", AsyncMock(return_value=False))
    reset = MagicMock()
    monkeypatch.setattr(macro_trigger, "clear_fired_layer_macros", reset)
    await main.on_printer_status_change(3, rig.c.state)
    rig.c.state.progress = 0
    monkeypatch.setattr(main, "_first_layer_notified", {3: True})
    await main._broadcast_printer_status_projection(3)
    assert main._first_layer_notified[3] is True
    reset.assert_not_called()
    await main.on_printer_status_change(3, rig.c.state)
    assert main._first_layer_notified[3] is False
    reset.assert_called_once_with(3)


def test_f04_getter_never_opens_source(rig, monkeypatch):
    bind(rig)
    monkeypatch.setattr("builtins.open", MagicMock(side_effect=AssertionError("hot file I/O")))
    monkeypatch.setattr("zipfile.ZipFile", MagicMock(side_effect=AssertionError("hot ZIP I/O")))
    for _ in range(20):
        assert resolve_layer_pair(rig.m, 3).total_layers == 693


def test_f11_ordinary_layer_callback_sees_previous_completed_pair(rig):
    bind(rig)
    push(rig.c, layer_num=14)
    seen = []
    rig.c.on_layer_change = lambda *_: seen.append(
        (rig.c.state.layer_num, rig.c.state.total_layers, resolve_layer_pair(rig.m, 3))
    )
    push(rig.c, layer_num=15, total_layer_num=700)
    assert seen == [(15, 0, LayerPair(14, 693, "file"))]
    assert resolve_layer_pair(rig.m, 3) == LayerPair(15, 700, "mqtt")


def test_f18_reslice_cannot_inherit_another_artifacts_marker():
    from backend.app.services.print_run_metadata import without_run_identity

    a = archive()
    cleaned = without_run_identity(a.extra_data)
    assert MARKER not in cleaned
    assert MARKER in a.extra_data


def test_f21_file_total_does_not_trigger_last_layer_photo_or_enter_terminal_accounting(rig):
    bind(rig)
    photo, terminal = MagicMock(), MagicMock()
    rig.c.on_finish_photo_moment = photo
    rig.c.on_print_complete = terminal
    push(rig.c, layer_num=693)
    assert resolve_layer_pair(rig.m, 3).total_layers == 693
    photo.assert_not_called()
    push(rig.c, gcode_state="FINISH")
    terminal.assert_called_once()
    data = terminal.call_args.args[0]
    assert data["terminal_total_layers"] == 0
    assert rig.c.state.total_layers == 0
