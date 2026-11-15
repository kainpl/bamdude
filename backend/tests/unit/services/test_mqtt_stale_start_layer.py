"""The layer the previous print ended on is not the new print's layer (upstream a8dd164c7, #3317).

Right after a print starts, an H2S sends one frame that still carries the PREVIOUS print's
last ``layer_num`` (357 with ``mc_percent`` 0), then 0. The run boundary already cleared the
old counters, but that frame arrives after it: taken at face value it became the new print's
layer, fired the layer-change effects for layer 357, and the 0 that followed saved 357 as the
last valid layer — so a print failing before its first layer was charged as nearly whole
(both inventory writers divide by the completion event's captured layer).
"""

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.utils.tray_split import CompletionSnapshot, printed_bound

FILE = "/data/Metadata/plate_1.gcode"


def push(c, **fields):
    c._process_message({"print": fields})


def finished_print(layers: int, *, subtask_id="100"):
    """A client that watched a print run to ``layers`` and finish."""
    c = BambuMQTTClient(ip_address="127.0.0.1", serial_number="OFFLINE-STALE", access_code="synthetic")
    push(c, gcode_state="IDLE")
    push(c, gcode_state="RUNNING", gcode_file=FILE, subtask_id=subtask_id, total_layer_num=max(layers, 1))
    for layer in range(1, layers + 1) if layers <= 5 else (1, layers // 2, layers):
        push(c, layer_num=layer)
    push(c, gcode_state="FINISH")
    c.edges = []
    c.on_layer_change = lambda new, previous: c.edges.append((new, previous))
    c.completions = []
    c.on_print_complete = c.completions.append
    c.on_print_start = MagicMock()
    return c


def start(c, *, subtask_id="200", **fields):
    push(c, gcode_state="RUNNING", gcode_file=FILE, subtask_id=subtask_id, **fields)


def fail(c) -> dict:
    push(c, gcode_state="FAILED")
    assert len(c.completions) == 1
    return c.completions[0]


def test_a_failure_before_the_first_layer_reports_no_layer():
    """The reported sequence: same layer count as the previous print."""
    c = finished_print(357)
    start(c)
    push(c, layer_num=357, total_layer_num=357, mc_percent=0)
    assert c.state.layer_num == 0
    push(c, layer_num=0)

    event = fail(c)
    assert event["last_layer_num"] == 0
    assert c.edges == []
    # What both inventory writers divide by: no printed layer, not 357 of 357.
    assert printed_bound(completed=False, snapshot=CompletionSnapshot.from_event(event)).layer is None


def test_the_stale_layer_inside_the_start_frame_itself_is_not_the_new_prints():
    c = finished_print(357)
    start(c, layer_num=357, total_layer_num=357, mc_percent=0)
    assert (c.state.layer_num, c.state.total_layers) == (0, 357)
    push(c, layer_num=0)

    assert fail(c)["last_layer_num"] == 0
    assert c.edges == []


def test_a_stale_layer_above_the_new_plates_count_is_ignored_too():
    c = finished_print(357)
    start(c)
    push(c, layer_num=357, total_layer_num=55, mc_percent=0)
    push(c, layer_num=0)

    assert (c.state.layer_num, c.state.total_layers) == (0, 55)
    assert fail(c)["last_layer_num"] == 0


def test_the_new_print_can_reach_the_previous_prints_last_layer():
    """Once the print reports a layer of its own, the old value is a layer number again."""
    c = finished_print(3)
    start(c)
    for layer in (3, 0, 1, 2, 3, 4):
        push(c, layer_num=layer)
    assert c.state.layer_num == 4
    push(c, layer_num=0)

    assert fail(c)["last_layer_num"] == 4
    assert c.edges == [(1, 0), (2, 1), (3, 2), (4, 3)]


def test_a_previous_print_that_ended_on_layer_one_does_not_hide_layer_one():
    """A one-layer job printed again: its own layer 1 is the only layer it reports."""
    c = finished_print(1)
    start(c)
    push(c, layer_num=1, total_layer_num=1)
    assert c.state.layer_num == 1
    push(c, layer_num=0)

    assert fail(c)["last_layer_num"] == 1
    assert c.edges == [(1, 0)]


def test_a_cancel_mid_print_still_keeps_its_last_layer():
    """The firmware's reset to 0 on cancel is what the captured layer exists for."""
    c = finished_print(357)
    start(c)
    for layer in (1, 2, 50):
        push(c, layer_num=layer)
    push(c, layer_num=0)

    assert fail(c)["last_layer_num"] == 50


@pytest.mark.parametrize("first", [1, 7, 120])
def test_a_first_frame_with_any_other_layer_is_kept(first):
    """The boundary frame's fresh value stands — only the previous print's own last layer is refused."""
    c = finished_print(357)
    start(c, layer_num=first, total_layer_num=483)
    assert (c.state.layer_num, c.state.total_layers) == (first, 483)
    assert c.edges == [(first, 0)]


def test_a_print_first_seen_already_running_is_not_guarded():
    """A run we did not watch start (the id changed while RUNNING) reports the layer it is on —
    its frames right after the start are long past, so nothing of it is refused."""
    c = BambuMQTTClient(ip_address="127.0.0.1", serial_number="OFFLINE-STALE", access_code="synthetic")
    push(c, gcode_state="RUNNING", gcode_file="old.gcode", subtask_id="100", layer_num=90, total_layer_num=100)
    push(c, gcode_file="new.gcode", subtask_id="200", layer_num=90, total_layer_num=483)
    assert c.state.layer_num == 90
