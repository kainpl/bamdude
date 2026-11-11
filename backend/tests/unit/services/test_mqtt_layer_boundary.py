"""Layer counters and callbacks belong to their own observed print boundary."""

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def client():
    return BambuMQTTClient(ip_address="127.0.0.1", serial_number="OFFLINE-LAYERS", access_code="synthetic")


def push(c, **fields):
    c._process_message({"print": fields})


def running(subtask_id="100"):
    c = client()
    # First observation mid-print is an existing supported catch-up path.
    push(c, gcode_state="RUNNING", gcode_file="old.gcode", subtask_id=subtask_id, layer_num=90, total_layer_num=100)
    return c


@pytest.mark.parametrize("terminal", ["IDLE", "FINISH", "FAILED"])
def test_new_print_preserves_first_frame_and_published_state(terminal):
    c = running()
    c.on_print_complete = MagicMock()
    push(c, gcode_state=terminal)
    c.on_print_start = MagicMock()
    observed = []
    c.on_state_change = lambda state: observed.append((state.layer_num, state.total_layers))
    push(c, gcode_state="RUNNING", gcode_file="new.gcode", subtask_id="200", layer_num=1, total_layer_num=483)
    assert observed[-1] == (1, 483)
    c.on_print_start.assert_called_once()


def test_total_only_on_start_survives_later_layer_only_updates():
    c = client()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="RUNNING", gcode_file="new.gcode", subtask_id="200", total_layer_num=483)
    push(c, layer_num=120)
    assert (c.state.layer_num, c.state.total_layers) == (120, 483)


def test_file_change_preserves_fresh_layer_and_total():
    c = running()
    c.on_print_start = MagicMock()
    push(c, gcode_file="new.gcode", subtask_id="200", layer_num=1, total_layer_num=483)
    assert (c.state.layer_num, c.state.total_layers) == (1, 483)
    c.on_print_start.assert_called_once()


def test_layer_only_on_new_print_does_not_inherit_old_total():
    c = running()
    push(c, gcode_file="new.gcode", subtask_id="200", layer_num=7)
    assert (c.state.layer_num, c.state.total_layers) == (7, 0)


@pytest.mark.parametrize("fields", [{}, {"total_layer_num": 0}])
def test_missing_or_zero_total_on_new_print_clears_old_counters(fields):
    c = running()
    push(c, gcode_file="new.gcode", subtask_id="200", **fields)
    assert (c.state.layer_num, c.state.total_layers) == (0, 0)


def test_no_last_layer_photo_from_previous_print_denominator():
    c = running()
    c.on_finish_photo_moment = MagicMock()
    push(c, gcode_file="new.gcode", subtask_id="200", layer_num=120, total_layer_num=483)
    c.on_finish_photo_moment.assert_not_called()


def test_layer_edge_at_new_print_does_not_compare_against_old_layer():
    c = running()
    edges = []
    c.on_layer_change = lambda new, previous: edges.append((new, previous))
    push(c, gcode_file="new.gcode", subtask_id="200", layer_num=1, total_layer_num=483)
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
    push(c, gcode_state="RUNNING", gcode_file="new.gcode", subtask_id="200", layer_num=1, total_layer_num=483)
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
    push(c, gcode_file="new.gcode", subtask_id="200", layer_num=1, total_layer_num=1, stg_cur=22, mc_percent=100)
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
    c = running(subtask_id=None)
    c.on_print_start = MagicMock()
    push(c, subtask_name="another label", subtask_id="another-id", layer_num=91)
    assert (c.state.layer_num, c.state.total_layers) == (91, 100)
    c.on_print_start.assert_not_called()


def test_file_change_boundary_edge_is_suppressed_even_when_new_layer_is_higher():
    c = running()
    c.on_layer_change = MagicMock()
    push(c, gcode_file="new.gcode", subtask_id="200", layer_num=120, total_layer_num=483)
    c.on_layer_change.assert_not_called()
    push(c, layer_num=121)
    c.on_layer_change.assert_called_once_with(121, 120)


def test_retained_start_total_allows_last_layer_photo_before_same_frame_stage_22():
    c = client()
    c.on_print_complete = MagicMock()
    c.on_finish_photo_moment = MagicMock()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="RUNNING", gcode_file="new.gcode", subtask_id="200", total_layer_num=100)
    push(c, layer_num=100, total_layer_num=100, stg_cur=22, mc_percent=100)
    push(c, gcode_state="FINISH")
    c.on_finish_photo_moment.assert_called_once()
    assert c.on_finish_photo_moment.call_args.args[0]["trigger"] == "last_layer"


@pytest.mark.parametrize("terminal", ["IDLE", "FINISH", "FAILED"])
@pytest.mark.parametrize("preparation", ["PREPARE", "SLICING"])
@pytest.mark.parametrize("sparse_total", [False, True])
def test_preparation_total_survives_running_boundary(terminal, preparation, sparse_total):
    c = running()
    push(c, gcode_state=terminal)
    fields = {} if sparse_total else {"total_layer_num": 693}
    push(c, gcode_state=preparation, gcode_file="new.gcode", subtask_id="200", **fields)
    if sparse_total:
        push(c, command="push_status", total_layer_num=693)
    observed = []
    c.on_state_change = lambda state: observed.append((state.layer_num, state.total_layers))
    push(c, gcode_state="RUNNING")
    push(c, layer_num=29)
    assert observed[-2:] == [(0, 693), (29, 693)]


@pytest.mark.parametrize("boundary_total", [483, 0])
def test_boundary_total_overrides_preparation_total(boundary_total):
    c = client()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="PREPARE", gcode_file="new.gcode", total_layer_num=693)
    push(c, gcode_state="RUNNING", layer_num=1, total_layer_num=boundary_total)
    assert (c.state.layer_num, c.state.total_layers) == (1, boundary_total)


@pytest.mark.parametrize("terminal", ["IDLE", "FINISH", "FAILED"])
def test_aborted_preparation_does_not_seed_next_attempt(terminal):
    c = client()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="PREPARE", gcode_file="new.gcode", total_layer_num=693)
    push(c, gcode_state=terminal)
    push(c, gcode_state="PREPARE")
    push(c, gcode_state="RUNNING", layer_num=1)
    assert (c.state.layer_num, c.state.total_layers) == (1, 0)


@pytest.mark.parametrize("terminal", ["IDLE", "FINISH", "FAILED"])
def test_total_in_terminal_frame_is_not_preparation_evidence(terminal):
    c = running()
    push(c, gcode_state=terminal, total_layer_num=999)
    push(c, gcode_state="PREPARE", gcode_file="new.gcode")
    push(c, gcode_state="RUNNING", layer_num=1)
    assert c.state.total_layers == 0


@pytest.mark.parametrize("known_total", [None, 693])
def test_ack_during_preparation_cannot_supply_or_replace_pending_total(known_total):
    c = client()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="PREPARE", gcode_file="new.gcode")
    if known_total is not None:
        push(c, total_layer_num=known_total)
    push(c, command="project_file", total_layer_num=999)
    push(c, gcode_state="RUNNING", layer_num=1)
    assert c.state.total_layers == (known_total or 0)


@pytest.mark.parametrize(
    "later_fields,expected", [({}, 693), ({"total_layer_num": 0}, 0), ({"total_layer_num": 483}, 483)]
)
def test_preparation_sparse_zero_and_new_positive(later_fields, expected):
    c = client()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="PREPARE", gcode_file="new.gcode", total_layer_num=693)
    push(c, gcode_state="SLICING", **later_fields)
    push(c, gcode_state="RUNNING", layer_num=1)
    assert c.state.total_layers == expected


@pytest.mark.parametrize("boundary_fields,expected", [({}, 693), ({"total_layer_num": 483}, 483)])
def test_preparation_total_survives_same_lifecycle_client_replacement(boundary_fields, expected):
    old = client()
    push(old, gcode_state="IDLE")
    push(old, gcode_state="PREPARE", gcode_file="new.gcode", subtask_id="200", total_layer_num=693)
    c = client()
    c.carry_print_lifecycle_from(old)
    push(c, gcode_state="RUNNING", gcode_file="new.gcode", subtask_id="200", layer_num=1, **boundary_fields)
    assert (c.state.layer_num, c.state.total_layers) == (1, expected)


def test_cold_preparation_does_not_invent_watched_start():
    c = client()
    c.on_print_start = MagicMock()
    push(c, gcode_state="PREPARE", gcode_file="new.gcode", total_layer_num=693)
    push(c, gcode_state="RUNNING", layer_num=29)
    assert c.state.total_layers == 693  # Ordinary catch-up already preserves this value.
    c.on_print_start.assert_not_called()


def test_old_run_id_cannot_supply_preparation_total():
    c = running()
    push(c, gcode_state="FINISH")
    push(c, gcode_state="PREPARE", gcode_file="new.gcode", subtask_id="100", total_layer_num=100)
    push(c, gcode_state="RUNNING", subtask_id="200", layer_num=1)
    assert c.state.total_layers == 0


@pytest.mark.parametrize("preparation", ["PREPARE", "SLICING"])
@pytest.mark.parametrize("had_candidate", [False, True])
def test_old_run_id_blocks_later_sparse_preparation_totals(preparation, had_candidate):
    c = running()
    push(c, gcode_state="FINISH")
    if had_candidate:
        push(c, gcode_state=preparation, total_layer_num=693)
    push(c, gcode_state=preparation, subtask_id="100", total_layer_num=100)
    push(c, total_layer_num=100)
    push(c, gcode_state="RUNNING", subtask_id="200", layer_num=1)
    assert (c.state.layer_num, c.state.total_layers) == (1, 0)


@pytest.mark.parametrize("sparse_total", [False, True])
def test_new_pending_id_unblocks_preparation_total(sparse_total):
    c = running()
    push(c, gcode_state="FINISH")
    push(c, gcode_state="PREPARE", subtask_id="100", total_layer_num=100)
    push(c, total_layer_num=100)
    fields = {} if sparse_total else {"total_layer_num": 693}
    push(c, subtask_id="200", **fields)
    if sparse_total:
        push(c, total_layer_num=693)
    push(c, gcode_state="RUNNING", layer_num=1)
    assert (c.state.layer_num, c.state.total_layers) == (1, 693)


@pytest.mark.parametrize("terminal", ["IDLE", "FINISH", "FAILED"])
def test_terminal_clears_old_id_preparation_block(terminal):
    c = running()
    push(c, gcode_state="FINISH")
    push(c, gcode_state="PREPARE", subtask_id="100", total_layer_num=100)
    push(c, gcode_state=terminal)
    push(c, gcode_state="PREPARE", total_layer_num=693)
    push(c, gcode_state="RUNNING", subtask_id="200", layer_num=1)
    assert c.state.total_layers == 693


def test_old_id_preparation_block_survives_reconnect_and_pause():
    old = running()
    push(old, gcode_state="FINISH")
    push(old, gcode_state="PREPARE", subtask_id="100", total_layer_num=100)
    c = client()
    c.carry_print_lifecycle_from(old)
    push(c, gcode_state="PREPARE", gcode_file="new.gcode", total_layer_num=100)
    push(c, gcode_state="PAUSE")
    push(c, gcode_state="PREPARE", total_layer_num=100)
    push(c, gcode_state="RUNNING", subtask_id="200", layer_num=1)
    assert c.state.total_layers == 0


def test_ack_new_id_cannot_unblock_preparation_total():
    c = running()
    push(c, gcode_state="FINISH")
    push(c, gcode_state="PREPARE", subtask_id="100", total_layer_num=100)
    push(c, command="project_file", subtask_id="200")
    push(c, total_layer_num=100)
    push(c, gcode_state="RUNNING", subtask_id="200", layer_num=1)
    assert c.state.total_layers == 0


@pytest.mark.parametrize("replacement_total,expected", [(None, 0), (483, 483)])
def test_changed_pending_id_drops_earlier_preparation_total(replacement_total, expected):
    c = running()
    push(c, gcode_state="FINISH")
    push(c, gcode_state="PREPARE", gcode_file="new.gcode", subtask_id="200", total_layer_num=693)
    fields = {} if replacement_total is None else {"total_layer_num": replacement_total}
    push(c, subtask_id="300", **fields)
    push(c, gcode_state="RUNNING", layer_num=1)
    assert c.state.total_layers == expected


def test_first_pending_id_arriving_late_keeps_same_window_total():
    c = running()
    push(c, gcode_state="FINISH")
    push(c, gcode_state="PREPARE", gcode_file="new.gcode", total_layer_num=693)
    push(c, subtask_id="200")
    push(c, gcode_state="RUNNING", layer_num=1)
    assert c.state.total_layers == 693


def test_consumed_preparation_total_is_not_inherited_by_next_print_of_same_file():
    c = client()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="PREPARE", gcode_file="same.gcode", total_layer_num=693)
    push(c, gcode_state="RUNNING", layer_num=1)
    assert c.state.total_layers == 693
    push(c, gcode_state="FINISH")
    push(c, gcode_state="PREPARE")
    push(c, gcode_state="RUNNING", layer_num=1)
    assert c.state.total_layers == 0


def test_preparation_total_reaches_start_layer_photo_and_terminal_consumers():
    c = client()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="PREPARE", gcode_file="new.gcode", total_layer_num=3)
    events = []
    c.on_print_start = lambda _data: events.append(("start", c.state.total_layers))
    c.on_layer_change = lambda new, old: events.append(("layer", new, old, c.state.total_layers))
    c.on_finish_photo_moment = MagicMock()
    c.on_print_complete = MagicMock()
    push(c, gcode_state="RUNNING", layer_num=1)
    assert events == [("start", 3), ("layer", 1, 0, 3)]
    c.on_finish_photo_moment.assert_not_called()
    push(c, layer_num=3)
    c.on_finish_photo_moment.assert_called_once()
    assert c.on_finish_photo_moment.call_args.args[0]["trigger"] == "last_layer"
    push(c, gcode_state="FINISH", layer_num=0, total_layer_num=0)
    c.on_finish_photo_moment.assert_called_once()
    assert c.on_print_complete.call_args.args[0]["terminal_total_layers"] == 3
