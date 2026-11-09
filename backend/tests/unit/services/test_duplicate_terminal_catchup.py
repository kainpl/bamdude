"""Desired-behavior regressions from independent fix review r1 (N1/N5)."""

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _make(serial="R2-START"):
    return BambuMQTTClient(ip_address="192.0.2.1", serial_number=serial, access_code="test")


def push(c, **data):
    c._process_message({"print": data})


def _wire(c):
    c.on_print_start = MagicMock()
    c.on_print_running_observed = MagicMock()
    c.on_print_complete = MagicMock()
    return c


def _watched_a(c):
    push(c, gcode_state="IDLE", gcode_file="", subtask_id="0")
    push(c, gcode_state="RUNNING", gcode_file="a.3mf", subtask_id="100", layer_num=3, total_layer_num=10)
    push(c, gcode_state="FINISH")
    assert c.on_print_complete.call_count == 1
    c.on_print_start.reset_mock()
    c.on_print_running_observed.reset_mock()
    c.on_print_complete.reset_mock()


def test_reconnect_during_prepare_carries_the_edge_one_watched_start():
    a = _wire(_make())
    _watched_a(a)
    push(a, gcode_state="PREPARE", gcode_file="b.3mf", subtask_id="200")
    b = _wire(_make("NEW"))
    b.carry_print_lifecycle_from(a)
    push(b, gcode_state="PREPARE", gcode_file="b.3mf", subtask_id="200")
    push(b, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id="200", layer_num=1, total_layer_num=20)
    assert b.on_print_start.call_count == 1
    obs = b.on_print_start.call_args[0][0]["_print_observation"]
    assert (obs.verdict, obs.previous_state, obs.current_id) == ("observed_new_start", "FINISH", "200")
    b.on_print_running_observed.assert_not_called()


def test_cancel_during_prepare_does_not_leak_the_cancelled_id_into_the_next_start():
    c = _wire(_make())
    _watched_a(c)
    push(c, gcode_state="PREPARE", gcode_file="b.3mf", subtask_id="200")
    push(c, gcode_state="IDLE")  # cancelled during setup
    c.on_print_complete.assert_not_called()
    push(c, gcode_state="PREPARE", gcode_file="c.3mf", subtask_id="0")
    push(c, gcode_state="RUNNING", gcode_file="c.3mf", subtask_id="0", layer_num=1)
    assert c.on_print_start.call_count == 1
    obs = c.on_print_start.call_args[0][0]["_print_observation"]
    assert obs.verdict == "observed_new_start" and obs.current_id is None


def test_finish_echoed_new_id_survives_finish_to_idle_before_setup():
    """#1078: the new id is echoed while FINISH; deltas need not repeat it."""
    c = _wire(_make())
    _watched_a(c)
    push(c, gcode_state="FINISH", subtask_id="200")
    push(c, gcode_state="IDLE")
    push(c, gcode_state="PREPARE", gcode_file="b.3mf")
    push(c, gcode_state="RUNNING", layer_num=1)
    assert c.on_print_start.call_count == 1
    assert c.on_print_start.call_args[0][0]["subtask_id"] == "200", "B's echoed id was dropped at FINISH->IDLE"


def test_reissued_catch_up_is_not_sent_for_a_terminal_frame_after_reconnect():
    """Process-fresh PAUSE joins (catch-up queued, seed not yet done) -> reconnect -> the
    replacement's first frame is FINISH (the print ended during the reconnect)."""
    a = _wire(_make())
    push(a, gcode_state="PAUSE", gcode_file="job.3mf", subtask_id="100", layer_num=8, total_layer_num=90)
    assert a.on_print_running_observed.call_count == 1
    b = _wire(_make("NEW"))
    b.carry_print_lifecycle_from(a)
    push(b, gcode_state="FINISH", gcode_file="job.3mf", subtask_id="100")
    assert b.on_print_complete.call_count == 1
    b.on_print_running_observed.assert_not_called()


def test_reissued_catch_up_is_not_sent_while_idle_after_the_print_ended():
    a = _wire(_make())
    push(a, gcode_state="PAUSE", gcode_file="job.3mf", subtask_id="100", layer_num=8, total_layer_num=90)
    push(a, gcode_state="RUNNING")
    push(a, gcode_state="FINISH")
    assert a.on_print_complete.call_count == 1
    b = _wire(_make("NEW"))
    b.carry_print_lifecycle_from(a)
    push(b, gcode_state="IDLE", gcode_file="job.3mf")
    b.on_print_running_observed.assert_not_called()
    assert not b._inflight_recovery_pending and not b._inflight_recovery_scheduled
    # The held accounting journal is released by main under the DB journal lock,
    # not discarded synchronously by this MQTT frame.


def _recovery_handoff(monkeypatch):
    from backend.app import main
    from backend.app.services import queue_sources
    from backend.app.services.printer_manager import printer_manager

    prior = _wire(_make())
    push(prior, gcode_state="PAUSE", gcode_file="job.3mf", subtask_id="100")
    generation = prior.journal_generation()
    prior.abandon_journal_hold(generation)
    prior.seed_runout_fired({})
    assert not prior._inflight_recovery_pending
    replacement = _wire(_make("NEW"))
    replacement.carry_print_lifecycle_from(prior)
    monkeypatch.setattr(printer_manager, "_clients", {1: replacement})
    monkeypatch.setattr(printer_manager, "_printer_lives", {1: 0})
    monkeypatch.setattr(queue_sources, "current_epoch", lambda: 1)
    return main, replacement, {"_journal_generation": generation, "_bamdude_printer_life": 0}


@pytest.mark.parametrize("changed", ["epoch", "life", "journal"])
def test_committed_handoff_refuses_a_different_context(monkeypatch, changed):
    from backend.app.services import queue_sources
    from backend.app.services.printer_manager import printer_manager

    main, replacement, data = _recovery_handoff(monkeypatch)
    if changed == "epoch":
        monkeypatch.setattr(queue_sources, "current_epoch", lambda: 2)
    elif changed == "life":
        monkeypatch.setattr(printer_manager, "_printer_lives", {1: 2})
    else:
        data["_journal_generation"] -= 1
    main._request_published_run_recovery(1, data, 1)
    assert not replacement._inflight_recovery_pending
    push(replacement, gcode_state="RUNNING", gcode_file="job.3mf", subtask_id="100")
    replacement.on_print_running_observed.assert_not_called()


@pytest.mark.parametrize("terminal", ["IDLE", "FINISH", "FAILED"])
@pytest.mark.parametrize("request_before_terminal", [False, True])
def test_committed_handoff_never_replays_on_terminal(monkeypatch, terminal, request_before_terminal):
    main, replacement, data = _recovery_handoff(monkeypatch)
    if request_before_terminal:
        main._request_published_run_recovery(1, data, 1)
        assert replacement._inflight_recovery_pending
    push(replacement, gcode_state=terminal, gcode_file="job.3mf", subtask_id="100")
    if not request_before_terminal:
        main._request_published_run_recovery(1, data, 1)
    assert not replacement._inflight_recovery_pending and not replacement._inflight_recovery_scheduled
    replacement.on_print_running_observed.assert_not_called()


def test_committed_handoff_waits_for_inflight_status(monkeypatch):
    main, replacement, data = _recovery_handoff(monkeypatch)
    main._request_published_run_recovery(1, data, 1)
    assert replacement._inflight_recovery_pending
    replacement.on_print_running_observed.assert_not_called()
    push(replacement, gcode_state="RUNNING", gcode_file="job.3mf", subtask_id="100")
    replacement.on_print_running_observed.assert_called_once()
