"""Layer counters and callbacks belong to their own observed print boundary."""

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def client():
    return BambuMQTTClient(ip_address="127.0.0.1", serial_number="OFFLINE-LAYERS", access_code="synthetic")


def push(c, **fields):
    c._process_message({"print": fields})


def running():
    c = client()
    # First observation mid-print is an existing supported catch-up path.
    push(c, gcode_state="RUNNING", gcode_file="old.gcode", layer_num=90, total_layer_num=100)
    return c


@pytest.mark.parametrize("terminal", ["IDLE", "FINISH", "FAILED"])
def test_new_print_preserves_first_frame_and_published_state(terminal):
    c = running()
    c.on_print_complete = MagicMock()
    push(c, gcode_state=terminal)
    c.on_print_start = MagicMock()
    observed = []
    c.on_state_change = lambda state: observed.append((state.layer_num, state.total_layers))
    push(c, gcode_state="RUNNING", gcode_file="new.gcode", layer_num=1, total_layer_num=483)
    assert observed[-1] == (1, 483)
    c.on_print_start.assert_called_once()


def test_total_only_on_start_survives_later_layer_only_updates():
    c = client()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="RUNNING", gcode_file="new.gcode", total_layer_num=483)
    push(c, layer_num=120)
    assert (c.state.layer_num, c.state.total_layers) == (120, 483)


def test_file_change_preserves_fresh_layer_and_total():
    c = running()
    c.on_print_start = MagicMock()
    push(c, gcode_file="new.gcode", layer_num=1, total_layer_num=483)
    assert (c.state.layer_num, c.state.total_layers) == (1, 483)
    c.on_print_start.assert_called_once()


def test_layer_only_on_new_print_does_not_inherit_old_total():
    c = running()
    push(c, gcode_file="new.gcode", layer_num=7)
    assert (c.state.layer_num, c.state.total_layers) == (7, 0)


@pytest.mark.parametrize("fields", [{}, {"total_layer_num": 0}])
def test_missing_or_zero_total_on_new_print_clears_old_counters(fields):
    c = running()
    push(c, gcode_file="new.gcode", **fields)
    assert (c.state.layer_num, c.state.total_layers) == (0, 0)


def test_no_last_layer_photo_from_previous_print_denominator():
    c = running()
    c.on_finish_photo_moment = MagicMock()
    push(c, gcode_file="new.gcode", layer_num=120, total_layer_num=483)
    c.on_finish_photo_moment.assert_not_called()


def test_layer_edge_at_new_print_does_not_compare_against_old_layer():
    c = running()
    edges = []
    c.on_layer_change = lambda new, previous: edges.append((new, previous))
    push(c, gcode_file="new.gcode", layer_num=1, total_layer_num=483)
    push(c, layer_num=2)
    push(c, layer_num=2)
    assert edges == [(2, 1)]  # File-change boundary must not touch old consumers.


def test_zero_total_during_same_print_preserves_known_total():
    c = running()
    push(c, layer_num=91, total_layer_num=0)
    assert (c.state.layer_num, c.state.total_layers) == (91, 100)


def test_pause_resume_same_file_does_not_clear_layers_or_fire_start():
    c = running()
    c.on_print_start = MagicMock()
    push(c, gcode_state="PAUSE")
    push(c, gcode_state="RUNNING")
    assert (c.state.layer_num, c.state.total_layers) == (90, 100)
    c.on_print_start.assert_not_called()


def test_first_observation_mid_print_keeps_layers_without_new_start():
    c = client()
    c.on_print_start = MagicMock()
    push(c, gcode_state="RUNNING", gcode_file="current.gcode", layer_num=123, total_layer_num=483)
    assert (c.state.layer_num, c.state.total_layers) == (123, 483)
    c.on_print_start.assert_not_called()


def test_late_total_still_recovers_known_count():
    c = client()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="RUNNING", gcode_file="new.gcode")
    push(c, layer_num=120)
    push(c, total_layer_num=483)
    assert (c.state.layer_num, c.state.total_layers) == (120, 483)


def test_same_print_last_layer_photo_remains_one_shot():
    c = running()
    c.on_finish_photo_moment = MagicMock()
    push(c, layer_num=100)
    push(c, layer_num=100, total_layer_num=0)
    c.on_finish_photo_moment.assert_called_once()


def test_first_layer_callback_follows_the_new_print_start_callback():
    c = client()
    push(c, gcode_state="IDLE")
    events = []
    c.on_print_start = lambda _data: events.append("start")
    c.on_layer_change = lambda new, previous: events.append((new, previous))
    push(c, gcode_state="RUNNING", gcode_file="new.gcode", layer_num=1, total_layer_num=483)
    assert events == ["start", (1, 0)]
    push(c, layer_num=1)
    push(c, layer_num=2)
    push(c, layer_num=2)
    assert events == ["start", (1, 0), (2, 1)]


@pytest.mark.parametrize("finish", ["FINISH", "stage_22"])
def test_one_layer_print_has_no_boundary_photo_and_finishes_once(finish):
    c = client()
    c.on_print_complete = MagicMock()
    c.on_finish_photo_moment = MagicMock()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="RUNNING", gcode_file="short.gcode", layer_num=1, total_layer_num=1)
    c.on_finish_photo_moment.assert_not_called()
    if finish == "stage_22":
        push(c, layer_num=1, stg_cur=22, mc_percent=100)
    push(c, gcode_state="FINISH")
    c.on_finish_photo_moment.assert_called_once()
    c.on_print_complete.assert_called_once()


def test_file_change_stage_22_cannot_photograph_old_print():
    c = running()
    c.on_print_complete = MagicMock()
    c.on_finish_photo_moment = MagicMock()
    push(c, gcode_file="new.gcode", layer_num=1, total_layer_num=1, stg_cur=22, mc_percent=100)
    c.on_finish_photo_moment.assert_not_called()
    push(c, gcode_state="FINISH")
    c.on_finish_photo_moment.assert_called_once()


def test_terminal_reset_preserves_last_layer_and_own_total_for_completion():
    c = running()
    c.on_print_complete = MagicMock()
    push(c, layer_num=91)
    push(c, gcode_state="FAILED", layer_num=0, total_layer_num=0)
    event = c.on_print_complete.call_args.args[0]
    assert event["last_layer_num"] == 91
    assert event["terminal_layer_num"] == 0
    assert event["terminal_total_layers"] == 100
    assert c.state.total_layers == 100


def test_full_reconnect_snapshot_at_last_layer_does_not_add_photo():
    previous = running()
    c = client()
    c.carry_print_lifecycle_from(previous)
    c.on_print_start = MagicMock()
    c.on_first_status = MagicMock()
    c.on_finish_photo_moment = MagicMock()
    push(c, gcode_state="RUNNING", gcode_file="old.gcode", layer_num=100, total_layer_num=100)
    assert (c.state.layer_num, c.state.total_layers) == (100, 100)
    c.on_print_start.assert_not_called()
    c.on_first_status.assert_called_once()
    c.on_finish_photo_moment.assert_not_called()


def test_late_total_with_last_layer_does_not_reorder_ordinary_frame():
    c = client()
    push(c, gcode_state="RUNNING", gcode_file="old.gcode", layer_num=99)
    c.on_finish_photo_moment = MagicMock()
    push(c, layer_num=100, total_layer_num=100)
    assert (c.state.layer_num, c.state.total_layers) == (100, 100)
    c.on_finish_photo_moment.assert_not_called()


def test_next_print_of_same_file_preserves_only_fresh_counters():
    c = running()
    c.on_print_complete = MagicMock()
    push(c, gcode_state="FINISH")
    c.on_print_start = MagicMock()
    push(c, gcode_state="RUNNING", gcode_file="old.gcode", layer_num=1, total_layer_num=483)
    assert (c.state.layer_num, c.state.total_layers) == (1, 483)
    assert c._last_valid_layer_num == 0
    c.on_print_start.assert_called_once()


def test_subtask_only_change_does_not_invent_a_boundary():
    c = running()
    c.on_print_start = MagicMock()
    push(c, subtask_name="another label", subtask_id="another-id", layer_num=91)
    assert (c.state.layer_num, c.state.total_layers) == (91, 100)
    c.on_print_start.assert_not_called()


def test_file_change_boundary_edge_is_suppressed_even_when_new_layer_is_higher():
    c = running()
    c.on_layer_change = MagicMock()
    push(c, gcode_file="new.gcode", layer_num=120, total_layer_num=483)
    c.on_layer_change.assert_not_called()
    push(c, layer_num=121)
    c.on_layer_change.assert_called_once_with(121, 120)


def test_retained_start_total_allows_last_layer_photo_before_same_frame_stage_22():
    c = client()
    c.on_print_complete = MagicMock()
    c.on_finish_photo_moment = MagicMock()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="RUNNING", gcode_file="new.gcode", total_layer_num=100)
    push(c, layer_num=100, total_layer_num=100, stg_cur=22, mc_percent=100)
    push(c, gcode_state="FINISH")
    c.on_finish_photo_moment.assert_called_once()
    assert c.on_finish_photo_moment.call_args.args[0]["trigger"] == "last_layer"
