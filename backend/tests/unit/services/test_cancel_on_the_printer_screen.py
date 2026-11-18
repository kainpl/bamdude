"""A print cancelled on the printer's own screen says so, and stays a failed print.

Hardware capture (A1 mini, firmware 01.08.01.00, 2026-11-18): the frame that says RUNNING -> FAILED carries
``print_error`` 0x0300400C, the firmware's "the task was canceled". It was only filtered out of the fault
list, so the archive read as a failure with no reason. The outcome is deliberately left ``failed`` -- the job
stays among the problem jobs, where the operator finds it -- and the echo becomes the archive's reason.
"""

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient

CANCELLED = 0x0300400C
A_REAL_FAULT = 0x0300800B


@pytest.fixture
def client():
    c = BambuMQTTClient(ip_address="192.0.2.1", serial_number="SCREEN-CANCEL", access_code="test")
    c.on_print_start = MagicMock()
    c.on_print_running_observed = MagicMock()
    c.on_print_complete = MagicMock()
    return c


def push(c, **data):
    c._process_message({"print": data})


def start(c, run="100"):
    push(c, gcode_state="IDLE", gcode_file="", subtask_id="0")
    push(c, gcode_state="RUNNING", gcode_file="a.3mf", subtask_id=run, layer_num=1, total_layer_num=10)


def outcome(c) -> dict:
    assert c.on_print_complete.call_count == 1
    return c.on_print_complete.call_args.args[0]


def test_the_captured_frame_is_a_cancel(client):
    start(client)
    push(client, gcode_state="FAILED", gcode_file="", print_error=CANCELLED)

    result = outcome(client)
    assert (result["status"], result["cancelled_on_printer"]) == ("failed", True)
    assert result["hms_errors"] == []


def test_the_echo_may_arrive_a_frame_before_the_state(client):
    start(client)
    push(client, print_error=CANCELLED)
    push(client, gcode_state="FAILED")

    assert outcome(client)["cancelled_on_printer"] is True


def test_a_cancel_from_a_pause_is_a_cancel_too(client):
    start(client)
    push(client, gcode_state="PAUSE")
    push(client, gcode_state="FAILED", print_error=CANCELLED)

    assert outcome(client)["cancelled_on_printer"] is True


def test_a_failure_without_the_echo_stays_a_failure(client):
    start(client)
    push(client, gcode_state="FAILED")

    result = outcome(client)
    assert (result["status"], result["cancelled_on_printer"]) == ("failed", False)


def test_a_fault_beside_the_echo_keeps_it_a_failure(client):
    """A firmware abort after a fatal fault carries the same echo; that print did fail."""
    start(client)
    push(client, hms=[{"attr": 0x03000100, "code": 0x00010001}])
    push(client, gcode_state="FAILED", print_error=CANCELLED)

    result = outcome(client)
    assert (result["status"], result["cancelled_on_printer"]) == ("failed", False)


def test_an_echo_the_register_has_left_is_forgotten(client):
    start(client)
    push(client, print_error=CANCELLED)
    push(client, print_error=0)
    push(client, gcode_state="FAILED")

    assert outcome(client)["cancelled_on_printer"] is False


def test_the_echo_of_one_print_does_not_follow_into_the_next(client):
    start(client)
    push(client, gcode_state="FAILED", print_error=CANCELLED)
    client.on_print_complete.reset_mock()

    push(client, gcode_state="IDLE")
    push(client, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id="200", layer_num=1, total_layer_num=10)
    push(client, gcode_state="FAILED")

    assert outcome(client)["cancelled_on_printer"] is False


def test_the_archive_gets_the_reason_and_keeps_its_outcome():
    from backend.app.main import derive_failure_reason
    from backend.app.utils.failure_reasons import USER_CANCELLED

    assert derive_failure_reason("failed", [], cancelled_on_printer=True) == USER_CANCELLED
    assert derive_failure_reason("failed", []) is None
    assert derive_failure_reason("completed", [], cancelled_on_printer=True) is None
