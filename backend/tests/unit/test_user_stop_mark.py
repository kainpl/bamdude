"""A Stop pressed on an idle printer marks nothing (upstream ab5ccc108 leg 1, owner's Р20).

The mark turns the NEXT ``failed`` terminal into ``cancelled``. With no run to
name it was ``None`` and matched any archive, so a print met mid-way after a
stray Stop reported its real failure as the operator's cancel.
"""

from types import SimpleNamespace

import pytest

from backend.app import main


@pytest.fixture
def manager(monkeypatch):
    state = SimpleNamespace(state="IDLE", connected=True)
    fake = SimpleNamespace(
        ACTIVE_PRINT_STATES=("RUNNING", "PAUSE", "PREPARE", "SLICING"),
        get_status=lambda _printer_id: fake.status,
        status=state,
    )
    monkeypatch.setattr(main, "printer_manager", fake)
    monkeypatch.setattr(main, "_user_stopped_printers", {})
    return fake


def _bind_run(monkeypatch, archive_id):
    run = None if archive_id is None else SimpleNamespace(archive_id=archive_id)
    monkeypatch.setattr("backend.app.services.print_run_binding.current_print_run", lambda _manager, _printer_id: run)


@pytest.mark.parametrize("state", ["IDLE", "FINISH", "FAILED"])
def test_a_stop_on_a_known_idle_printer_leaves_no_mark(manager, monkeypatch, state):
    _bind_run(monkeypatch, None)
    manager.status = SimpleNamespace(state=state, connected=True)

    main.mark_printer_stopped_by_user(7, unless_idle=True)

    assert 7 not in main._user_stopped_printers


@pytest.mark.parametrize("state", ["RUNNING", "PAUSE", "PREPARE", "SLICING"])
def test_a_stop_on_a_printing_printer_marks_the_runs_archive(manager, monkeypatch, state):
    _bind_run(monkeypatch, 41)
    manager.status = SimpleNamespace(state=state, connected=True)

    main.mark_printer_stopped_by_user(7, unless_idle=True)

    assert main._user_stopped_printers == {7: 41}


@pytest.mark.parametrize("status", [None, SimpleNamespace(state="unknown", connected=True), SimpleNamespace(state="")])
def test_an_unknown_state_still_marks(manager, monkeypatch, status):
    _bind_run(monkeypatch, None)
    manager.status = status

    main.mark_printer_stopped_by_user(7, unless_idle=True)

    assert main._user_stopped_printers == {7: None}


def test_the_queue_stop_marks_whatever_the_state(manager, monkeypatch):
    """The queue's Stop has a row behind it and marks an offline printer on purpose."""
    _bind_run(monkeypatch, None)

    main.mark_printer_stopped_by_user(7)

    assert main._user_stopped_printers == {7: None}


def test_a_mark_without_a_run_does_not_outlive_the_next_bound_run(manager):
    main._user_stopped_printers[7] = None

    main._forget_unbound_user_stop(7)

    assert 7 not in main._user_stopped_printers
    failed = main._normalise_user_stopped_terminal(7, 55, {"status": "failed"})
    assert failed["status"] == "failed"


def test_a_mark_that_names_its_archive_survives_a_bind(manager):
    main._user_stopped_printers[7] = 41

    main._forget_unbound_user_stop(7)

    assert main._user_stopped_printers == {7: 41}
    assert main._normalise_user_stopped_terminal(7, 41, {"status": "failed"})["status"] == "cancelled"


def test_binding_a_run_drops_the_unbound_mark(monkeypatch):
    """The choke point every start and adoption passes drops it."""
    from backend.app.services.printer_manager import PrinterManager

    monkeypatch.setattr(main, "printer_manager", PrinterManager())
    monkeypatch.setattr(main, "_user_stopped_printers", {7: None, 8: None})
    archive = SimpleNamespace(id=55, subtask_id=None, recovery_identity=None, file_path=None, plate_index=None)

    main._bind_print_file_analysis_context(7, archive)

    assert main._user_stopped_printers == {8: None}
