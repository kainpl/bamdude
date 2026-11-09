"""Watched starts and same-lineage reconnects: independent review r1 regressions."""

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient

KINDS = ["current"]


def _make(kind):
    return BambuMQTTClient(ip_address="192.0.2.1", serial_number="OFFLINE-START", access_code="test")


def push(c, **data):
    c._process_message({"print": data})


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("new_id", ["200", "0"])
@pytest.mark.parametrize("setup_state", ["PREPARE", "SLICING"])
def test_watched_start_through_prepare_is_a_print_start(kind, new_id, setup_state):
    c = _make(kind)
    c.on_print_start = MagicMock()
    c.on_print_running_observed = MagicMock()
    c.on_print_complete = MagicMock()
    # print A watched from its start in this process
    push(c, gcode_state="IDLE", gcode_file="", subtask_id="0")
    push(c, gcode_state="RUNNING", gcode_file="a.3mf", subtask_id="100", layer_num=1, total_layer_num=10)
    push(c, gcode_state="FINISH")
    c.on_print_complete.assert_called_once()
    c.on_print_start.reset_mock()
    c.on_print_running_observed.reset_mock()
    # print B: the printer echoes the new id while still FINISH (CHANGELOG #1078),
    # then reports PREPARE while heating/downloading, then RUNNING.
    push(c, gcode_state="FINISH", subtask_id=new_id)
    push(c, gcode_state=setup_state, gcode_file="b.3mf", subtask_id=new_id)
    push(c, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id=new_id, layer_num=1, total_layer_num=20)
    assert c.on_print_start.call_count == 1, "a watched start became a restart-recovery"
    c.on_print_running_observed.assert_not_called()


@pytest.mark.parametrize("kind", KINDS)
def test_process_fresh_client_watches_idle_prepare_running(kind):
    c = _make(kind)
    c.on_print_start = MagicMock()
    c.on_print_running_observed = MagicMock()
    push(c, gcode_state="IDLE", gcode_file="", subtask_id="0")
    push(c, gcode_state="PREPARE", gcode_file="b.3mf", subtask_id="0")
    push(c, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id="0", layer_num=1, total_layer_num=20)
    assert c.on_print_start.call_count == 1
    c.on_print_running_observed.assert_not_called()


@pytest.mark.parametrize("kind", KINDS)
def test_prepare_start_resets_previous_print_memory(kind):
    """Second print of the session: the boundary reset must run for B."""
    c = _make(kind)
    c.on_print_start = MagicMock()
    c.on_print_running_observed = MagicMock()
    c.on_print_complete = MagicMock()
    push(c, gcode_state="IDLE", gcode_file="")
    push(c, gcode_state="RUNNING", gcode_file="a.3mf", subtask_id="100", layer_num=5, total_layer_num=10)
    c._runout_fired[1] = "pause"
    push(c, gcode_state="FINISH")
    push(c, gcode_state="PREPARE", gcode_file="b.3mf", subtask_id="200")
    push(c, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id="200", layer_num=1, total_layer_num=20)
    assert not c._runout_fired, "A's runout dedup leaked into B (no lifecycle reset)"


@pytest.mark.parametrize("kind", ["current"])
def test_id_only_boundary_orders_start_before_layer_edge(kind):
    prior = _make(kind)
    push(
        prior,
        gcode_state="RUNNING",
        gcode_file="/data/Metadata/plate_1.gcode",
        subtask_id="100",
        layer_num=50,
        total_layer_num=90,
    )
    c = _make(kind)
    c.carry_print_lifecycle_from(prior)
    c.state.layer_num = prior.state.layer_num
    c.state.total_layers = prior.state.total_layers
    order = []
    c.on_print_start = lambda data: order.append(("start", data["_print_observation"].verdict))
    c.on_layer_change = lambda new, old: order.append(("layer", new, old))
    # H2/X-series: gcode_file is the generic plate path for every job, only the id moves.
    push(c, gcode_state="RUNNING", gcode_file="/data/Metadata/plate_1.gcode", subtask_id="200", layer_num=1)
    assert order and order[0][0] == "start", f"layer edge delivered before the start callback: {order}"


def test_reconnect_makes_an_already_admitted_start_observation_non_current():
    from dataclasses import replace

    from backend.app.services.print_observation import observation_is_current

    a = _make("current")
    seen = []
    a.on_print_start = lambda data: seen.append(data["_print_observation"])
    push(a, gcode_state="IDLE", gcode_file="")
    push(a, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id="200", layer_num=1)
    (obs,) = seen
    assert obs.verdict == "observed_new_start"
    manager = MagicMock()
    manager._clients = {7: a}
    assert observation_is_current(manager, 7, obs)
    # an ordinary stale-watchdog reconnect of the SAME lifecycle
    b = _make("current")
    b.carry_print_lifecycle_from(a)
    manager._clients = {7: b}
    push(b, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id="200", layer_num=2)
    assert b._run_boundary_generation == obs.generation
    assert observation_is_current(manager, 7, obs), "same lifecycle, same boundary: start's owns_start() now fails"
    assert observation_is_current(manager, 7, replace(obs)), "the frozen address is a value, not an object handle"


def test_display_alias_after_start_revokes_the_start_observation():
    from backend.app.services.print_observation import observation_is_current

    a = _make("current")
    seen = []
    a.on_print_start = lambda data: seen.append(data["_print_observation"])
    push(a, gcode_state="IDLE", gcode_file="")
    push(a, gcode_state="RUNNING", gcode_file="b.gcode.3mf", subtask_id="200", layer_num=1)
    first = seen[0]
    manager = MagicMock()
    manager._clients = {7: a}
    push(a, gcode_file="/data/Metadata/plate_1.gcode")
    assert seen[-1].verdict == "file_change_unproven"
    assert observation_is_current(manager, 7, first), "an alias frame revoked the proven start's observation"


def test_catch_up_lost_when_reconnect_fences_the_old_clients_callback():
    """main.on_print_running_observed drops a callback whose _bamdude_client_generation is
    not the current client's (main.py:6643). The replacement client inherits the hold
    but never asks for the seed again."""
    a = _make("current")
    a.on_print_running_observed = MagicMock()  # scheduled, not yet run when the reconnect lands
    push(a, gcode_state="PAUSE", gcode_file="job.3mf", subtask_id="100", layer_num=8, total_layer_num=90)
    a.on_print_running_observed.assert_called_once()
    b = _make("current")
    b.carry_print_lifecycle_from(a)
    b.on_print_running_observed = MagicMock()
    b.on_usage_event = MagicMock()
    b.state.layer_num, b.state.total_layers = 8, 90
    for _ in range(3):
        push(b, gcode_state="PAUSE", hms=[{"attr": 0x07002000, "code": 0x00020001}])
    push(b, gcode_state="RUNNING")
    assert b.journal_held(), "journal stays held for the rest of the print"
    assert b.on_print_running_observed.called, "nobody re-requests the seed for this lifecycle"
    b.on_usage_event.assert_not_called()


@pytest.mark.parametrize("kind", KINDS)
def test_prepare_start_does_not_inherit_finish_photo_guard_or_last_progress(kind):
    c = _make(kind)
    c.on_print_start = MagicMock()
    c.on_print_running_observed = MagicMock()
    c.on_finish_photo_moment = MagicMock()
    completions = []
    c.on_print_complete = lambda data: completions.append(data)
    push(c, gcode_state="IDLE", gcode_file="")
    push(c, gcode_state="RUNNING", gcode_file="a.3mf", subtask_id="100", layer_num=1, total_layer_num=10, mc_percent=1)
    push(c, layer_num=10, mc_percent=100)
    push(c, gcode_state="FINISH")
    assert c.on_finish_photo_moment.called
    push(c, gcode_state="PREPARE", gcode_file="b.3mf", subtask_id="200", mc_percent=0)
    push(c, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id="200", layer_num=0, total_layer_num=20, mc_percent=0)
    photo_spent = c._finish_photo_captured
    push(c, gcode_state="FAILED")  # cancelled before any progress
    facts = (photo_spent, completions[-1]["last_progress"], completions[-1]["last_layer_num"])
    assert facts == (False, 0.0, 0), f"(finish photo spent, last_progress, last_layer) inherited from A: {facts}"


def test_delayed_id_after_setup_does_not_create_a_second_start():
    c = _make("current")
    starts = []
    c.on_print_start = starts.append
    c.on_print_complete = MagicMock()
    push(c, gcode_state="IDLE", gcode_file="")
    push(c, gcode_state="RUNNING", gcode_file="a.3mf", subtask_id="100")
    push(c, gcode_state="FINISH")
    starts.clear()
    push(c, gcode_state="PREPARE", gcode_file="b.3mf")
    push(c, gcode_state="RUNNING", gcode_file="b.3mf", layer_num=0)
    assert len(starts) == 1
    assert starts[0]["subtask_id"] is None
    push(c, subtask_id="100")
    assert c._last_known_run_id is None
    push(c, subtask_id="200", layer_num=1)
    assert len(starts) == 1
    assert c._last_known_run_id == "200"


def test_a_new_lineage_cannot_reuse_the_old_start_generation():
    from backend.app.services.print_observation import observation_is_current

    a = _make("current")
    seen = []
    a.on_print_start = seen.append
    push(a, gcode_state="IDLE")
    push(a, gcode_state="RUNNING", gcode_file="a.3mf", subtask_id="100")
    b = _make("current")
    push(b, gcode_state="IDLE")
    push(b, gcode_state="RUNNING", gcode_file="b.3mf", subtask_id="200")
    assert a._run_boundary_generation == b._run_boundary_generation
    manager = MagicMock()
    manager._clients = {7: b}
    assert not observation_is_current(manager, 7, seen[0]["_print_observation"])


def test_watched_setup_pause_waits_for_running_without_catch_up():
    c = _make("current")
    c.on_print_start = MagicMock()
    c.on_print_running_observed = MagicMock()
    push(c, gcode_state="IDLE")
    push(c, gcode_state="PREPARE", gcode_file="b.3mf", subtask_id="200")
    push(c, gcode_state="PAUSE")
    c.on_print_start.assert_not_called()
    c.on_print_running_observed.assert_not_called()

    push(c, gcode_state="RUNNING")
    c.on_print_start.assert_called_once()
    c.on_print_running_observed.assert_not_called()


@pytest.mark.asyncio
async def test_old_catch_up_cannot_seed_the_new_run_on_the_same_connection(monkeypatch):
    from unittest.mock import AsyncMock

    from backend.app import main
    from backend.app.services.printer_manager import printer_manager

    c = _make("current")
    seen = []
    c.on_print_running_observed = seen.append
    c.on_print_complete = MagicMock()
    push(c, gcode_state="PAUSE", gcode_file="a.3mf", subtask_id="100")
    old = seen[0]
    push(c, gcode_state="IDLE")
    push(c, gcode_state="PREPARE", gcode_file="b.3mf", subtask_id="200")
    push(c, gcode_state="RUNNING")
    monkeypatch.setattr(printer_manager, "_clients", {7: c})
    monkeypatch.setattr(printer_manager, "_client_generations", {7: 1})
    monkeypatch.setattr(printer_manager, "_printer_lives", {7: 0})
    restore, seed = AsyncMock(), AsyncMock()
    monkeypatch.setattr(main, "_restore_usage_tracking_session", restore)
    monkeypatch.setattr(main, "_seed_runout_detector_from_journal", seed)
    await main.on_print_running_observed(7, {**old, "_bamdude_client_generation": 1, "_bamdude_printer_life": 0})
    restore.assert_not_awaited()
    seed.assert_not_awaited()
