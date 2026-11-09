"""A reconnect/file alias must not invent a new physical execution."""

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def client():
    return BambuMQTTClient(ip_address="192.0.2.1", serial_number="SYNTHETIC-CLAIMS", access_code="test")


def push(c, **data):
    c._process_message({"print": data})


def memory(c):
    return (
        c.state.layer_num,
        c.state.total_layers,
        set(c._runout_fired),
        c._journal_generation,
        list(c.state.tray_change_log),
        c._finish_photo_captured,
        c._completion_triggered,
    )


def test_fresh_pause_recovers_once_and_can_finish_without_resume():
    c = client()
    c.on_print_running_observed = MagicMock()
    c.on_print_start = MagicMock()
    c.on_print_complete = MagicMock()
    push(c, gcode_state="PAUSE", gcode_file="job.3mf", subtask_id="100", layer_num=8)
    c.on_print_running_observed.assert_called_once()
    assert c._runout_seeded is False
    push(c, gcode_state="PAUSE")
    push(c, gcode_state="FAILED")
    c.on_print_running_observed.assert_called_once()
    c.on_print_start.assert_not_called()
    c.on_print_complete.assert_called_once()


def test_unproven_file_change_preserves_all_lifecycle_memory():
    c = client()
    push(c, gcode_state="RUNNING", gcode_file="old.3mf", layer_num=8, total_layer_num=90)
    c._runout_fired[1] = 1
    c.state.tray_change_log = [(1, 0), (2, 6)]
    c._finish_photo_captured = True
    before = memory(c)
    c.on_print_start = MagicMock()
    push(c, gcode_file="alias.3mf", subtask_id="200")
    assert memory(c) == before
    observation = c.on_print_start.call_args.args[0]["_print_observation"]
    assert observation.verdict == "file_change_unproven"
    for _ in range(3):
        push(c, subtask_id="200")
    assert c._last_known_run_id is None
    assert memory(c) == before


def test_reconnect_carries_id_before_overwrite_and_resets_new_run_once():
    prior = client()
    push(prior, gcode_state="RUNNING", gcode_file="a.3mf", subtask_id="100")
    c = client()
    c.carry_print_lifecycle_from(prior)
    c._runout_fired[1] = 1
    c.state.tray_change_log = [(1, 0), (2, 6)]
    c.on_print_start = MagicMock()
    push(c, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id="200", layer_num=1)
    observation = c.on_print_start.call_args.args[0]["_print_observation"]
    assert observation.verdict == "different_client_run_id"
    assert (observation.previous_id, observation.current_id) == ("100", "200")
    assert not c._runout_fired
    assert (2, 6) not in c.state.tray_change_log
    generation = c._journal_generation
    push(c, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id="200")
    assert c._journal_generation == generation
    c.on_print_start.assert_called_once()


@pytest.mark.parametrize("missing", ["", "0", None])
def test_empty_id_does_not_erase_known_id_but_new_start_does(missing):
    c = client()
    push(c, gcode_state="RUNNING", gcode_file="a.3mf", subtask_id="100")
    push(c, subtask_id=missing)
    assert c._last_known_run_id == "100"
    push(c, gcode_state="FINISH")
    push(c, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id=missing)
    assert c._last_known_run_id is None


def test_partial_pause_waits_for_file_then_recovers_without_start():
    c = client()
    c.on_print_running_observed = MagicMock()
    c.on_print_start = MagicMock()
    push(c, gcode_state="PAUSE", subtask_id="100")
    c.on_print_running_observed.assert_not_called()
    push(c, gcode_state="RUNNING", gcode_file="job.3mf")
    c.on_print_running_observed.assert_called_once()
    c.on_print_start.assert_not_called()


def test_first_pause_disarms_before_this_packets_runout():
    c = client()
    c.on_print_running_observed = MagicMock()
    c.on_usage_event = MagicMock()
    c.state.tray_now = 0
    c.state.raw_data = {"ams": {"ams": [{"id": 0, "tray": [{"id": 0, "tray_uuid": "A" * 32}]}]}}
    push(
        c,
        gcode_state="PAUSE",
        gcode_file="job.3mf",
        subtask_id="100",
        layer_num=8,
        hms=[{"attr": 0x07002000, "code": 0x00020001}],
    )
    c.on_usage_event.assert_not_called()
    c.seed_runout_fired({})
    push(c, hms=[{"attr": 0x07002000, "code": 0x00020001}])
    c.on_usage_event.assert_called_once_with("runout", "pause", 0, 8)


def test_unproven_identity_latch_survives_reconnect():
    c = client()
    push(c, gcode_state="RUNNING", gcode_file="a.3mf")
    push(c, gcode_file="alias.3mf", subtask_id="200")
    nxt = client()
    nxt.carry_print_lifecycle_from(c)
    for _ in range(3):
        push(nxt, gcode_state="RUNNING", subtask_id="200")
    assert nxt._last_known_run_id is None
